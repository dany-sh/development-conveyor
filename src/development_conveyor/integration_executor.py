"""Controller-plan milestone integration without a model-session handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .contracts import fingerprint
from .errors import IntegrationPlanError, LockError
from .logging import utc_now
from .queue import COMPLETE_STATUSES, FeatureQueue
from .redaction import redact_text
from .repository import RepositoryInspector
from .validation import SafetyPolicy
from .workflow_lease import WorkflowWriterLease


PLAN_SCHEMA_VERSION = 2
LEGACY_PLAN_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
RUNTIME_PATTERN = "/.factory/runtime/milestone-integration/"
RUNTIME_DESCENDANTS = (
    ".factory/runtime/milestone-integration/latest.json",
    ".factory/runtime/milestone-integration/F005-plan.json",
)
VALIDATION_GROUPS = ("build", "test", "lint", "package", "validate")
LEASE_IDENTITY_FIELDS = (
    "lease_id",
    "lease_type",
    "repository_identity",
    "repository_path_fingerprint",
    "repository_path",
    "project_id",
    "controller_project_id",
    "adapter_project_id",
    "transaction_id",
    "workflow_type",
    "milestone",
    "feature_id",
    "feature_branch",
    "accepted_commit",
    "starting_branch",
    "starting_head",
    "run_id",
    "session_id",
    "owner_pid",
    "owner_process_start",
    "owner_host",
    "allowed_mutations",
)
LEGACY_LEASE_IDENTITY_FIELDS = tuple(
    field
    for field in LEASE_IDENTITY_FIELDS
    if field not in {"controller_project_id", "adapter_project_id"}
)
PLAN_REQUIRED_FIELDS = {
    "schema_version",
    "plan_fingerprint",
    "created_at",
    "project_id",
    "controller_project_id",
    "adapter_project_id",
    "repository",
    "repository_identity",
    "repository_path_fingerprint",
    "transaction_id",
    "run_id",
    "feature_id",
    "feature_branch",
    "accepted_commit",
    "accepted_metadata_ref",
    "accepted_queue_fingerprint",
    "accepted_changed_paths",
    "milestone_id",
    "milestone_branch",
    "pre_integration_head",
    "queue_path",
    "projection_fingerprint",
    "ledger_sequence",
    "ledger_fingerprint",
    "controller_ledger_path",
    "lease_identity",
    "runtime_exclusion",
    "validation_commands",
    "metadata_paths",
}
LEGACY_PLAN_REQUIRED_FIELDS = PLAN_REQUIRED_FIELDS - {"controller_project_id"}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plan_fingerprint(value: dict[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("plan_fingerprint", None)
    return _sha256(_canonical(unsigned))


def _safe_relative(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise IntegrationPlanError(f"{label} is not a safe repository-relative path")
    return value


def _one(document: dict[str, Any], collection: str, item_id: str) -> dict[str, Any]:
    values = document.get(collection)
    if not isinstance(values, list):
        raise IntegrationPlanError(f"queue {collection} must be an array")
    matches = [item for item in values if isinstance(item, dict) and item.get("id") == item_id]
    if len(matches) != 1:
        raise IntegrationPlanError(
            f"accepted metadata must contain exactly one {collection[:-1]} {item_id}"
        )
    return matches[0]


def _git(
    root: Path,
    arguments: Iterable[str],
    *,
    check: bool = True,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    argv = ["git", *list(arguments)]
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationPlanError(f"Git operation failed safely: {argv!r}") from exc
    if check and result.returncode != 0:
        diagnostic = redact_text(result.stderr.strip() or result.stdout.strip())
        raise IntegrationPlanError(diagnostic or f"Git operation failed: {argv!r}")
    return result


def _git_output(root: Path, *arguments: str) -> str:
    return _git(root, arguments).stdout.strip()


def _json_at(root: Path, commit: str, relative: str) -> tuple[dict[str, Any], bytes]:
    _safe_relative(relative, "queue path")
    result = _git(root, ["show", f"{commit}:{relative}"], check=False)
    if result.returncode != 0:
        raise IntegrationPlanError(f"immutable metadata is missing {relative}")
    payload = result.stdout.encode("utf-8")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IntegrationPlanError(
            f"immutable metadata {relative} is not JSON-compatible YAML"
        ) from exc
    if not isinstance(value, dict):
        raise IntegrationPlanError(f"immutable metadata {relative} must be an object")
    return value, payload


def _commands_from_adapter(adapter: dict[str, Any]) -> list[dict[str, Any]]:
    commands = adapter.get("commands")
    if not isinstance(commands, dict):
        raise IntegrationPlanError("accepted adapter commands must be an object")
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for group in VALIDATION_GROUPS:
        entries = commands.get(group, [])
        if not isinstance(entries, list):
            raise IntegrationPlanError(f"accepted adapter commands.{group} must be an array")
        for entry in entries:
            if (
                not isinstance(entry, list)
                or not entry
                or not all(isinstance(item, str) and item for item in entry)
            ):
                raise IntegrationPlanError(
                    f"accepted adapter commands.{group} contains an invalid argument array"
                )
            argv = tuple(entry)
            if argv not in seen:
                seen.add(argv)
                values.append({"group": group, "argv": list(argv)})
    return values


def _adapter_project_id(adapter: dict[str, Any], *, source: str) -> str:
    project = adapter.get("project")
    value = project.get("id") if isinstance(project, dict) else None
    if not isinstance(value, str) or not value:
        raise IntegrationPlanError(f"{source} adapter project.id is missing")
    return value


def _controller_project_id(plan: dict[str, Any]) -> str:
    value = plan.get("controller_project_id")
    if isinstance(value, str) and value:
        return value
    project_id = plan.get("project_id")
    adapter_project_id = plan.get("adapter_project_id")
    if (
        plan.get("schema_version") == LEGACY_PLAN_SCHEMA_VERSION
        and isinstance(project_id, str)
        and project_id
        and project_id == adapter_project_id
    ):
        return project_id
    raise IntegrationPlanError(
        "legacy integration plan may default controller_project_id only when "
        "project_id exactly equals adapter_project_id"
    )


def _metadata_paths(adapter: dict[str, Any], queue_path: str) -> list[str]:
    factory = adapter.get("factory")
    run_log = factory.get("run_log") if isinstance(factory, dict) else None
    paths = {
        _safe_relative(queue_path, "queue path"),
        "docs/CURRENT_STATUS.md",
        _safe_relative(run_log, "factory run-log path")
        if isinstance(run_log, str) and run_log
        else "docs/RUN_LOG.md",
    }
    return sorted(paths)


def inspect_two_refs(
    *,
    repository: Path,
    controller_project_id: str,
    feature_id: str,
    feature_branch: str,
    accepted_commit: str,
    milestone_id: str,
    milestone_branch: str,
    pre_integration_head: str,
    queue_path: str,
) -> dict[str, Any]:
    """Validate the immutable accepted ref and live milestone ref without writes."""

    root = repository.expanduser().resolve()
    inspector = RepositoryInspector(root)
    identity = inspector.identity()
    if not inspector.is_clean or any(inspector.git_operation_state().values()):
        raise IntegrationPlanError("integration requires a clean repository and no Git operation")
    if inspector.rev_parse(milestone_branch, check=False) != pre_integration_head:
        raise IntegrationPlanError("live milestone ref does not match the planned starting HEAD")
    if inspector.rev_parse(feature_branch, check=False) != accepted_commit:
        raise IntegrationPlanError("feature branch HEAD does not match the accepted commit")
    parents = _git_output(root, "show", "-s", "--format=%P", accepted_commit).split()
    if parents != [pre_integration_head]:
        raise IntegrationPlanError(
            "accepted commit is not the exact permitted single child of the milestone HEAD"
        )

    accepted_queue, accepted_queue_bytes = _json_at(root, accepted_commit, queue_path)
    accepted_adapter, _ = _json_at(root, accepted_commit, ".factory/project.yaml")
    accepted_adapter_project_id = _adapter_project_id(
        accepted_adapter, source="accepted-commit"
    )
    live_adapter_bytes = inspector.safe_worktree_file_bytes(".factory/project.yaml")
    try:
        live_adapter = json.loads(live_adapter_bytes)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationPlanError("live adapter is unreadable or malformed") from exc
    if not isinstance(live_adapter, dict):
        raise IntegrationPlanError("live adapter must be an object")
    live_adapter_project_id = _adapter_project_id(live_adapter, source="live")
    milestone_adapter, _ = _json_at(
        root, pre_integration_head, ".factory/project.yaml"
    )
    milestone_adapter_project_id = _adapter_project_id(
        milestone_adapter, source="live milestone"
    )
    if live_adapter_project_id != milestone_adapter_project_id:
        raise IntegrationPlanError(
            "live worktree adapter project identity does not match the live milestone ref"
        )
    if accepted_adapter_project_id != live_adapter_project_id:
        raise IntegrationPlanError(
            "accepted-commit adapter project identity does not match the live milestone repository"
        )
    live_queue, _ = _json_at(root, pre_integration_head, queue_path)
    accepted_feature = _one(accepted_queue, "features", feature_id)
    accepted_milestone = _one(accepted_queue, "milestones", milestone_id)
    live_milestone = _one(live_queue, "milestones", milestone_id)
    checks = {
        "accepted_feature_milestone": accepted_feature.get("milestone") == milestone_id,
        "accepted_implementation_completed": str(
            accepted_feature.get("implementation_status", "")
        ).strip().lower()
        == "completed",
        "accepted_status": accepted_feature.get("status") == "integration_pending",
        "accepted_integration_pending": accepted_feature.get("integration_status") == "pending",
        "accepted_feature_branch": accepted_feature.get("branch") == feature_branch,
        "accepted_integration_base": accepted_feature.get("integration_base_commit")
        == pre_integration_head,
        "accepted_commit_identity": accepted_feature.get("accepted_commit")
        in {"SELF", accepted_commit},
        "accepted_milestone_branch": accepted_milestone.get("integration_branch")
        == milestone_branch,
        "live_milestone_branch": live_milestone.get("integration_branch")
        == milestone_branch,
    }
    acceptance = accepted_feature.get("acceptance")
    checks["acceptance_flags"] = isinstance(acceptance, dict) and all(
        acceptance.get(key) is True
        for key in ("tests_passed", "review_passed", "documentation_current")
    )
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        raise IntegrationPlanError(
            "immutable accepted metadata failed two-ref validation: " + ", ".join(failed)
        )

    live_features = {
        item.get("id"): item
        for item in live_queue.get("features", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    integrated_ids = live_milestone.get("integrated_features", [])
    if not isinstance(integrated_ids, list):
        raise IntegrationPlanError("live milestone integrated_features must be an array")
    dependencies = accepted_feature.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise IntegrationPlanError("accepted feature dependencies must be an ID array")
    for dependency_id in dependencies:
        dependency = live_features.get(dependency_id)
        if (
            not isinstance(dependency, dict)
            or dependency.get("status") not in COMPLETE_STATUSES
            or dependency_id not in integrated_ids
        ):
            raise IntegrationPlanError(
                f"integrated dependency is absent from the live milestone ref: {dependency_id}"
            )
        integrated_commit = dependency.get("integrated_commit")
        if not isinstance(integrated_commit, str) or not inspector.is_ancestor(
            integrated_commit, pre_integration_head
        ):
            raise IntegrationPlanError(
                f"dependency commit is absent from the live milestone history: {dependency_id}"
            )

    changed_paths = tuple(
        sorted(
            line
            for line in _git_output(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                accepted_commit,
            ).splitlines()
            if line
        )
    )
    if not changed_paths:
        raise IntegrationPlanError("accepted commit has no changed paths")
    return {
        "repository_identity": identity["repository_id"],
        "repository_path_fingerprint": identity["path_fingerprint"],
        "controller_project_id": controller_project_id,
        "adapter_project_id": live_adapter_project_id,
        "accepted_queue_fingerprint": _sha256(accepted_queue_bytes),
        "accepted_changed_paths": list(changed_paths),
        "validation_commands": _commands_from_adapter(accepted_adapter),
        "metadata_paths": _metadata_paths(accepted_adapter, queue_path),
        "dependencies": list(dependencies),
        "project_id": controller_project_id,
    }


def build_integration_plan(
    *,
    repository: Path,
    controller_project_id: str,
    transaction_id: str,
    run_id: str,
    feature_id: str,
    feature_branch: str,
    accepted_commit: str,
    milestone_id: str,
    milestone_branch: str,
    pre_integration_head: str,
    queue_path: str,
    projection_fingerprint: str,
    ledger_sequence: int,
    ledger_fingerprint: str,
    controller_ledger_path: Path,
    lease_identity: dict[str, Any],
    runtime_exclusion: dict[str, Any],
    verified_evidence: dict[str, Any],
) -> dict[str, Any]:
    evidence = json.loads(json.dumps(verified_evidence, sort_keys=True))
    if evidence.get("controller_project_id") != controller_project_id:
        raise IntegrationPlanError(
            "verified integration evidence changed controller project identity"
        )
    adapter_project_id = evidence.get("adapter_project_id")
    if not isinstance(adapter_project_id, str) or not adapter_project_id:
        raise IntegrationPlanError("verified integration evidence lacks adapter project identity")
    lease_subset = json.loads(
        json.dumps(
            {field: lease_identity.get(field) for field in LEASE_IDENTITY_FIELDS},
            sort_keys=True,
        )
    )
    if any(field not in lease_identity for field in LEASE_IDENTITY_FIELDS):
        raise IntegrationPlanError("controller lease identity is incomplete")
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "created_at": utc_now(),
        "project_id": controller_project_id,
        "controller_project_id": controller_project_id,
        "adapter_project_id": adapter_project_id,
        "repository": str(repository.expanduser().resolve()),
        "repository_identity": evidence["repository_identity"],
        "repository_path_fingerprint": evidence["repository_path_fingerprint"],
        "transaction_id": transaction_id,
        "run_id": run_id,
        "feature_id": feature_id,
        "feature_branch": feature_branch,
        "accepted_commit": accepted_commit,
        "accepted_metadata_ref": accepted_commit,
        "accepted_queue_fingerprint": evidence["accepted_queue_fingerprint"],
        "accepted_changed_paths": evidence["accepted_changed_paths"],
        "milestone_id": milestone_id,
        "milestone_branch": milestone_branch,
        "pre_integration_head": pre_integration_head,
        "queue_path": _safe_relative(queue_path, "queue path"),
        "projection_fingerprint": projection_fingerprint,
        "ledger_sequence": ledger_sequence,
        "ledger_fingerprint": ledger_fingerprint,
        "controller_ledger_path": str(controller_ledger_path.expanduser().resolve()),
        "lease_identity": lease_subset,
        "runtime_exclusion": runtime_exclusion,
        "validation_commands": evidence["validation_commands"],
        "metadata_paths": evidence["metadata_paths"],
    }
    plan["plan_fingerprint"] = _plan_fingerprint(plan)
    validate_plan_document(plan)
    return plan


def validate_plan_document(plan: dict[str, Any]) -> None:
    schema_version = plan.get("schema_version")
    required_fields = (
        PLAN_REQUIRED_FIELDS
        if schema_version == PLAN_SCHEMA_VERSION
        else LEGACY_PLAN_REQUIRED_FIELDS
        if schema_version == LEGACY_PLAN_SCHEMA_VERSION
        else None
    )
    if required_fields is None:
        raise IntegrationPlanError("unsupported integration plan schema version")
    if set(plan) != required_fields:
        missing = sorted(required_fields - set(plan))
        extras = sorted(set(plan) - required_fields)
        raise IntegrationPlanError(
            f"integration plan fields disagree; missing={missing!r} extras={extras!r}"
        )
    controller_project_id = _controller_project_id(plan)
    if plan.get("project_id") != controller_project_id:
        raise IntegrationPlanError(
            "integration plan project_id compatibility alias must equal controller_project_id"
        )
    for field in (
        "plan_fingerprint",
        "created_at",
        "project_id",
        "repository",
        "repository_identity",
        "repository_path_fingerprint",
        "transaction_id",
        "run_id",
        "feature_id",
        "feature_branch",
        "accepted_commit",
        "accepted_metadata_ref",
        "accepted_queue_fingerprint",
        "milestone_id",
        "milestone_branch",
        "pre_integration_head",
        "queue_path",
        "projection_fingerprint",
        "ledger_fingerprint",
        "controller_ledger_path",
    ):
        if not isinstance(plan.get(field), str) or not plan[field]:
            raise IntegrationPlanError(f"integration plan {field} must be a non-empty string")
    for field in ("controller_project_id", "adapter_project_id"):
        value = controller_project_id if field == "controller_project_id" else plan.get(field)
        if not isinstance(value, str) or not value:
            raise IntegrationPlanError(f"integration plan {field} must be a non-empty string")
    if type(plan.get("ledger_sequence")) is not int or plan["ledger_sequence"] < 0:
        raise IntegrationPlanError("integration plan ledger_sequence must be non-negative")
    if plan["accepted_metadata_ref"] != plan["accepted_commit"]:
        raise IntegrationPlanError("accepted metadata ref must be the immutable accepted commit")
    if plan["plan_fingerprint"] != _plan_fingerprint(plan):
        raise IntegrationPlanError("integration plan fingerprint is invalid")
    if not isinstance(plan.get("accepted_changed_paths"), list) or not plan[
        "accepted_changed_paths"
    ]:
        raise IntegrationPlanError("integration plan accepted_changed_paths must be non-empty")
    if plan["accepted_changed_paths"] != sorted(set(plan["accepted_changed_paths"])):
        raise IntegrationPlanError("integration plan accepted_changed_paths must be sorted and unique")
    for path in [*plan["accepted_changed_paths"], *plan.get("metadata_paths", [])]:
        if not isinstance(path, str):
            raise IntegrationPlanError("integration plan mutation paths must be strings")
        _safe_relative(path, "integration mutation path")
    lease = plan.get("lease_identity")
    expected_lease_fields = (
        set(LEASE_IDENTITY_FIELDS)
        if schema_version == PLAN_SCHEMA_VERSION
        else set(LEGACY_LEASE_IDENTITY_FIELDS)
    )
    if not isinstance(lease, dict) or set(lease) != expected_lease_fields:
        raise IntegrationPlanError("integration plan lease identity is incomplete")
    lease_controller_project_id = lease.get("controller_project_id")
    lease_adapter_project_id = lease.get("adapter_project_id")
    if schema_version == LEGACY_PLAN_SCHEMA_VERSION:
        lease_controller_project_id = controller_project_id
        lease_adapter_project_id = controller_project_id
    lease_checks = {
        "lease_type": lease.get("lease_type") == "integration_writer",
        "workflow_type": lease.get("workflow_type") == "milestone_integration",
        "repository_identity": lease.get("repository_identity")
        == plan["repository_identity"],
        "repository_path_fingerprint": lease.get("repository_path_fingerprint")
        == plan["repository_path_fingerprint"],
        "repository_path": Path(str(lease.get("repository_path"))).resolve()
        == Path(plan["repository"]).resolve(),
        "project_id": lease.get("project_id") == controller_project_id,
        "controller_project_id": lease_controller_project_id == controller_project_id,
        "adapter_project_id": lease_adapter_project_id == plan["adapter_project_id"],
        "transaction_id": lease.get("transaction_id") == plan["transaction_id"],
        "run_id": lease.get("run_id") == plan["run_id"],
        "feature_id": lease.get("feature_id") == plan["feature_id"],
        "feature_branch": lease.get("feature_branch") == plan["feature_branch"],
        "accepted_commit": lease.get("accepted_commit") == plan["accepted_commit"],
        "milestone": lease.get("milestone") == plan["milestone_id"],
        "starting_branch": lease.get("starting_branch") == plan["milestone_branch"],
        "starting_head": lease.get("starting_head") == plan["pre_integration_head"],
        "session_id": lease.get("session_id") is None,
    }
    failed = [field for field, passed in lease_checks.items() if not passed]
    if failed:
        raise IntegrationPlanError(
            "integration plan lease binding failed: " + ", ".join(failed)
        )
    allowed_mutations = lease.get("allowed_mutations")
    expected_paths = sorted(
        set(plan["accepted_changed_paths"]) | set(plan["metadata_paths"])
    )
    if (
        not isinstance(allowed_mutations, dict)
        or allowed_mutations.get("allowed_paths") != expected_paths
        or allowed_mutations.get("allowed_prefixes") != []
        or allowed_mutations.get("allow_untracked") is not False
    ):
        raise IntegrationPlanError(
            "controller lease mutation policy does not match the immutable integration plan"
        )
    exclusion = plan.get("runtime_exclusion")
    if (
        not isinstance(exclusion, dict)
        or exclusion.get("pattern") != RUNTIME_PATTERN
        or exclusion.get("repository_identity") != plan["repository_identity"]
        or exclusion.get("repository_path_fingerprint")
        != plan["repository_path_fingerprint"]
        or exclusion.get("verified_descendants") != list(RUNTIME_DESCENDANTS)
    ):
        raise IntegrationPlanError("integration plan runtime exclusion binding is invalid")
    commands = plan.get("validation_commands")
    if not isinstance(commands, list):
        raise IntegrationPlanError("integration plan validation_commands must be an array")
    for command in commands:
        if (
            not isinstance(command, dict)
            or set(command) != {"group", "argv"}
            or command.get("group") not in VALIDATION_GROUPS
            or not isinstance(command.get("argv"), list)
            or not command["argv"]
            or not all(isinstance(item, str) and item for item in command["argv"])
        ):
            raise IntegrationPlanError("integration plan contains an invalid validation command")


def persist_integration_plan(path: Path, plan: dict[str, Any]) -> Path:
    validate_plan_document(plan)
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise IntegrationPlanError("immutable integration plan already exists")
    payload = json.dumps(plan, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = -1
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise IntegrationPlanError("immutable integration plan target is unsafe")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if target.exists():
            target.unlink()
        raise
    return target


def load_integration_plan(path: Path) -> dict[str, Any]:
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        metadata = os.lstat(target)
    except FileNotFoundError as exc:
        raise IntegrationPlanError("integration plan does not exist") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise IntegrationPlanError("integration plan path is unsafe")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationPlanError("integration plan is unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise IntegrationPlanError("integration plan must be an object")
    validate_plan_document(value)
    return value


def _verify_runtime_exclusion(root: Path, plan: dict[str, Any]) -> None:
    inspector = RepositoryInspector(root)
    common = Path(plan["runtime_exclusion"]["common_git_dir"]).resolve()
    if inspector.common_git_dir != common:
        raise IntegrationPlanError("runtime exclusion common Git directory changed")
    exclude = common / "info/exclude"
    try:
        lines = exclude.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise IntegrationPlanError("runtime exclusion cannot be read") from exc
    if RUNTIME_PATTERN not in lines:
        raise IntegrationPlanError("exact milestone-integration runtime exclusion is absent")
    for relative in RUNTIME_DESCENDANTS:
        if _git(
            root,
            ["check-ignore", "-q", "--no-index", "--", relative],
            check=False,
        ).returncode != 0:
            raise IntegrationPlanError(f"runtime exclusion does not cover {relative}")


def _verify_ledger_binding(plan: dict[str, Any]) -> None:
    path = Path(plan["controller_ledger_path"])
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise IntegrationPlanError("controller ledger cannot be read for plan verification") from exc
    if len(lines) != plan["ledger_sequence"]:
        raise IntegrationPlanError("controller ledger sequence changed after plan creation")
    if not lines:
        raise IntegrationPlanError("controller ledger is empty after transaction start")
    try:
        events = [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        raise IntegrationPlanError("controller ledger is malformed") from exc
    controller_project_id = _controller_project_id(plan)
    if any(event.get("project_id") != controller_project_id for event in events):
        raise IntegrationPlanError(
            "integration plan controller project identity does not match the controller ledger"
        )
    tail = events[-1]
    if tail.get("sequence") != plan["ledger_sequence"] or tail.get("fingerprint") != plan[
        "ledger_fingerprint"
    ]:
        raise IntegrationPlanError("controller ledger fingerprint changed after plan creation")


def _audit_accepted_commit(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    adapter, _ = _json_at(
        root, plan["accepted_commit"], ".factory/project.yaml"
    )
    profile = adapter.get("project_profile")
    content_policy = adapter.get("content_policy")
    supported_profiles = {
        "personal_private",
        "team_internal",
        "commercial_private",
        "public_distribution",
        "open_source",
    }
    if (
        not isinstance(profile, dict)
        or profile.get("usage") not in supported_profiles
        or not isinstance(content_policy, dict)
    ):
        raise IntegrationPlanError(
            "accepted commit lacks a supported project profile or content policy"
        )
    approved, _ = _json_at(
        root, plan["accepted_commit"], ".factory/approved-content.yaml"
    )
    if approved.get("schema_version") != 1 or not isinstance(
        approved.get("approved"), list
    ):
        raise IntegrationPlanError(
            "accepted commit approved-content metadata is invalid"
        )
    raw = _git_output(
        root,
        "diff-tree",
        "--no-commit-id",
        "--raw",
        "-r",
        plan["accepted_commit"],
    )
    risky_modes = []
    for line in raw.splitlines():
        header = line.split("\t", 1)[0]
        modes = header.split()
        if any(mode in {"120000", "160000"} for mode in modes[:2]):
            risky_modes.append(line.split("\t", 1)[-1])
    if risky_modes:
        raise IntegrationPlanError(
            "accepted commit contains a symlink or submodule path requiring a human gate"
        )
    diff = _git(
        root,
        ["diff", "--unified=0", plan["pre_integration_head"], plan["accepted_commit"]],
    ).stdout
    secret_categories: set[str] = set()
    patterns = {
        "private_key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
        "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    }
    added_text = "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    for category, pattern in patterns.items():
        if pattern.search(added_text):
            secret_categories.add(category)
    if secret_categories:
        raise IntegrationPlanError(
            "accepted commit content audit found suspected secret categories: "
            + ", ".join(sorted(secret_categories))
        )
    return {
        "accepted_commit": plan["accepted_commit"],
        "changed_path_count": len(plan["accepted_changed_paths"]),
        "risky_mode_paths": 0,
        "suspected_secret_categories": [],
        "profile_checked": True,
        "profile_usage": profile["usage"],
        "approved_content_checked": True,
    }


def _runtime_directory(root: Path) -> Path:
    return root / ".factory/runtime/milestone-integration"


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise IntegrationPlanError(f"unsafe JSON evidence target: {path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_runtime(
    root: Path, plan: dict[str, Any], phase: str, evidence: dict[str, Any]
) -> dict[str, str]:
    _verify_runtime_exclusion(root, plan)
    directory = _runtime_directory(root)
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise IntegrationPlanError("integration runtime directory is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "phase": phase,
        "updated_at": utc_now(),
        "plan_fingerprint": plan["plan_fingerprint"],
        "runtime_identity": {
            "project_id": _controller_project_id(plan),
            "controller_project_id": _controller_project_id(plan),
            "adapter_project_id": plan["adapter_project_id"],
            "repository_identity": plan["repository_identity"],
            "repository_path_fingerprint": plan["repository_path_fingerprint"],
            "transaction_id": plan["transaction_id"],
            "run_id": plan["run_id"],
            "feature_id": plan["feature_id"],
            "milestone_id": plan["milestone_id"],
            "milestone_branch": plan["milestone_branch"],
            "accepted_commit": plan["accepted_commit"],
            "pre_integration_head": plan["pre_integration_head"],
            "lease_id": plan["lease_identity"]["lease_id"],
        },
        "evidence": evidence,
    }
    named = directory / f"{plan['feature_id']}-{plan['plan_fingerprint'][:20]}.json"
    latest = directory / "latest.json"
    _atomic_json(named, payload)
    _atomic_json(latest, payload)
    return {
        "record": str(named.relative_to(root)),
        "latest": str(latest.relative_to(root)),
    }


def _load_worktree_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / _safe_relative(relative, "metadata path")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationPlanError(f"metadata file is unreadable: {relative}") from exc
    if not isinstance(value, dict):
        raise IntegrationPlanError(f"metadata file must be an object: {relative}")
    return value


def _write_worktree_json(root: Path, relative: str, value: dict[str, Any]) -> None:
    _atomic_json(root / _safe_relative(relative, "metadata path"), value, mode=0o644)


def _git_mutation(
    root: Path,
    plan: dict[str, Any],
    argv: list[str],
    *,
    check: bool = True,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    allowed = [
        ["switch", plan["milestone_branch"]],
        ["cherry-pick", plan["accepted_commit"]],
    ]
    metadata = list(plan["metadata_paths"])
    subjects = {
        f"factory: mark {plan['feature_id']} integrating",
        f"factory: record {plan['feature_id']} integration passed",
        f"factory: record {plan['feature_id']} integration failed",
    }
    if argv[:2] == ["add", "--"]:
        if argv[2:] != metadata:
            raise IntegrationPlanError("metadata staging paths disagree with the immutable plan")
    elif argv[:2] == ["commit", "-m"]:
        if len(argv) != 3 or argv[2] not in subjects:
            raise IntegrationPlanError("integration metadata commit is not authorized")
    elif argv not in allowed:
        raise IntegrationPlanError(f"unapproved deterministic Git mutation: {argv!r}")
    return _git(root, argv, check=check, timeout=timeout)


def _ensure_metadata_files(root: Path, plan: dict[str, Any]) -> None:
    for relative in plan["metadata_paths"]:
        path = root / relative
        if path.exists():
            continue
        if relative == plan["queue_path"]:
            raise IntegrationPlanError("integration queue disappeared after cherry-pick")
        heading = "# Current Status\n" if relative.endswith("CURRENT_STATUS.md") else "# Factory Run Log\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(heading, encoding="utf-8")


def _commit_metadata(root: Path, plan: dict[str, Any], subject: str) -> str:
    metadata = list(plan["metadata_paths"])
    _git_mutation(root, plan, ["add", "--", *metadata])
    staged = _git(root, ["diff", "--cached", "--quiet"], check=False)
    if staged.returncode != 0:
        _git_mutation(root, plan, ["commit", "-m", subject])
    return _git_output(root, "rev-parse", "HEAD")


def _replace_status_block(path: Path, content: str) -> None:
    begin = "<!-- FACTORY_INTEGRATION_STATUS_BEGIN -->"
    end = "<!-- FACTORY_INTEGRATION_STATUS_END -->"
    block = f"{begin}\n{content.rstrip()}\n{end}"
    existing = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    rendered = pattern.sub(block, existing) if pattern.search(existing) else existing.rstrip() + "\n\n" + block + "\n"
    path.write_text(rendered, encoding="utf-8")


def _append_run_log(path: Path, content: str) -> None:
    existing = path.read_text(encoding="utf-8")
    path.write_text(existing.rstrip() + "\n\n" + content.rstrip() + "\n", encoding="utf-8")


def _mark_integrating(root: Path, plan: dict[str, Any]) -> str:
    _ensure_metadata_files(root, plan)
    queue = _load_worktree_json(root, plan["queue_path"])
    feature = _one(queue, "features", plan["feature_id"])
    if feature.get("status") not in {"accepted", "integration_pending", "integrating"}:
        raise IntegrationPlanError("accepted feature cannot transition to integrating")
    feature.update(
        {
            "status": "integrating",
            "implementation_status": "Completed",
            "branch": plan["feature_branch"],
            "integration_base_commit": plan["pre_integration_head"],
            "accepted_commit": plan["accepted_commit"],
            "integration_status": "integrating",
            "integration_attempted_at": plan["created_at"],
        }
    )
    _write_worktree_json(root, plan["queue_path"], queue)
    return _commit_metadata(
        root, plan, f"factory: mark {plan['feature_id']} integrating"
    )


def _run_validation(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    blockers: list[str] = []
    for item in plan["validation_commands"]:
        argv = list(item["argv"])
        SafetyPolicy.validate_configured_command(
            argv, cwd=root, registered_repository=root
        )
        try:
            result = subprocess.run(
                argv,
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=1800,
            )
            exit_code = result.returncode
            diagnostic = redact_text(
                (result.stderr.strip() or result.stdout.strip())[-2000:]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_code = 124
            diagnostic = redact_text(type(exc).__name__)
        results.append(
            {
                "group": item["group"],
                "argv": argv,
                "exit_code": exit_code,
                "diagnostic_tail": diagnostic or None,
            }
        )
        if exit_code != 0:
            blockers.append(
                f"{item['group']} configured command failed at index {len(results) - 1}"
            )
            break
    diff_check = _git(root, ["diff", "--check"], check=False)
    results.append(
        {
            "group": "git_diff_check",
            "argv": ["git", "diff", "--check"],
            "exit_code": diff_check.returncode,
            "diagnostic_tail": redact_text(
                (diff_check.stderr.strip() or diff_check.stdout.strip())[-2000:]
            )
            or None,
        }
    )
    if diff_check.returncode != 0:
        blockers.append("git diff --check failed")
    if not RepositoryInspector(root).is_clean:
        blockers.append("validation commands left the integration worktree dirty")
    return {
        "ok": not blockers,
        "validated_commit": _git_output(root, "rev-parse", "HEAD"),
        "commands": results,
        "blockers": blockers,
    }


def _record_final_metadata(
    root: Path,
    plan: dict[str, Any],
    validation: dict[str, Any],
    resulting_feature_commit: str,
) -> dict[str, Any]:
    success = bool(validation["ok"])
    queue = _load_worktree_json(root, plan["queue_path"])
    feature = _one(queue, "features", plan["feature_id"])
    milestone = _one(queue, "milestones", plan["milestone_id"])
    feature.update(
        {
            "status": "integrated" if success else "failed",
            "implementation_status": "Completed",
            "branch": plan["feature_branch"],
            "integration_base_commit": plan["pre_integration_head"],
            "accepted_commit": plan["accepted_commit"],
            "integrated_commit": resulting_feature_commit,
            "integration_status": "passed" if success else "failed",
            "integration_attempted_at": plan["created_at"],
        }
    )
    fixes = feature.setdefault("integration_fix_commits", [])
    if not isinstance(fixes, list):
        raise IntegrationPlanError("integration_fix_commits must be an array")
    if success:
        feature.pop("integration_failure", None)
        integrated = milestone.setdefault("integrated_features", [])
        if not isinstance(integrated, list):
            raise IntegrationPlanError("milestone integrated_features must be an array")
        if plan["feature_id"] not in integrated:
            integrated.append(plan["feature_id"])
        milestone["last_validated_commit"] = validation["validated_commit"]
        milestone["last_validation_at"] = plan["created_at"]
    else:
        feature["integration_failure"] = {
            "validated_commit": validation["validated_commit"],
            "blockers": validation["blockers"],
        }
    scoped = [
        item
        for item in queue.get("features", [])
        if isinstance(item, dict) and item.get("milestone") == plan["milestone_id"]
    ]
    milestone_complete = bool(scoped) and all(
        item.get("status") in COMPLETE_STATUSES for item in scoped
    )
    milestone["status"] = "integration_complete" if milestone_complete else "active"
    _write_worktree_json(root, plan["queue_path"], queue)

    status_path = root / "docs/CURRENT_STATUS.md"
    result_word = "passed" if success else "failed"
    stop_reason = (
        "milestone_gate_required"
        if milestone_complete and success
        else ("next_feature_selection" if success else "integration_validation_failed")
    )
    _replace_status_block(
        status_path,
        "\n".join(
            (
                "## Milestone integration",
                "",
                f"- Feature: {plan['feature_id']}",
                f"- Milestone: {plan['milestone_id']}",
                f"- Branch: `{plan['milestone_branch']}`",
                f"- Integration: {result_word}",
                f"- Validated commit: `{validation['validated_commit']}`",
                f"- Next action: {stop_reason}.",
            )
        ),
    )
    run_log_path = next(
        root / relative
        for relative in plan["metadata_paths"]
        if relative.endswith("RUN_LOG.md")
    )
    command_lines = [
        f"- `{item['group']}` `{json.dumps(item['argv'], separators=(',', ':'))}`: exit {item['exit_code']}"
        for item in validation["commands"]
    ]
    _append_run_log(
        run_log_path,
        "\n".join(
            (
                f"## {plan['created_at']} — Deterministic integration {plan['feature_id']}",
                "",
                "- Executor: `development-conveyor controller plan`",
                "- Model session launched: `false`",
                f"- Accepted commit: `{plan['accepted_commit']}`",
                f"- Milestone start: `{plan['pre_integration_head']}`",
                f"- Resulting feature commit: `{resulting_feature_commit}`",
                f"- Integration status: `{result_word}`",
                f"- Stop reason: `{stop_reason}`",
                "",
                "### Validation evidence",
                "",
                *command_lines,
            )
        ),
    )
    evidence_commit = _commit_metadata(
        root,
        plan,
        f"factory: record {plan['feature_id']} integration {result_word}",
    )
    return {
        "evidence_commit": evidence_commit,
        "milestone_complete": milestone_complete,
        "stop_reason": stop_reason,
    }


def _result_base(plan: dict[str, Any], classification: str) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "classification": classification,
        "plan_fingerprint": plan["plan_fingerprint"],
        "project_id": _controller_project_id(plan),
        "repository_identity": plan["repository_identity"],
        "repository_path_fingerprint": plan["repository_path_fingerprint"],
        "transaction_id": plan["transaction_id"],
        "run_id": plan["run_id"],
        "feature_id": plan["feature_id"],
        "feature_branch": plan["feature_branch"],
        "accepted_commit": plan["accepted_commit"],
        "milestone_id": plan["milestone_id"],
        "milestone_branch": plan["milestone_branch"],
        "starting_commit": plan["pre_integration_head"],
        "lease_id": plan["lease_identity"]["lease_id"],
        "model_session_launched": False,
    }


def execute_integration_plan(path: Path) -> dict[str, Any]:
    """Execute one exact immutable plan while adopting its controller lease."""

    plan = load_integration_plan(path)
    root = Path(plan["repository"]).expanduser().resolve()
    inspector = RepositoryInspector(root)
    identity = inspector.identity()
    if (
        identity["repository_id"] != plan["repository_identity"]
        or identity["path_fingerprint"] != plan["repository_path_fingerprint"]
    ):
        raise IntegrationPlanError("planned repository identity or path fingerprint changed")
    _verify_ledger_binding(plan)
    _verify_runtime_exclusion(root, plan)
    lease_path = inspector.writer_lock_path()
    lease = WorkflowWriterLease(lease_path)
    try:
        lease.revalidate_adopted(plan["lease_identity"])
    except LockError as exc:
        raise IntegrationPlanError(str(exc)) from exc
    evidence = inspect_two_refs(
        repository=root,
        controller_project_id=_controller_project_id(plan),
        feature_id=plan["feature_id"],
        feature_branch=plan["feature_branch"],
        accepted_commit=plan["accepted_commit"],
        milestone_id=plan["milestone_id"],
        milestone_branch=plan["milestone_branch"],
        pre_integration_head=plan["pre_integration_head"],
        queue_path=plan["queue_path"],
    )
    exact_checks = {
        "controller_project_id": evidence["controller_project_id"]
        == _controller_project_id(plan),
        "accepted_queue_fingerprint": evidence["accepted_queue_fingerprint"]
        == plan["accepted_queue_fingerprint"],
        "accepted_changed_paths": evidence["accepted_changed_paths"]
        == plan["accepted_changed_paths"],
        "adapter_project_id": evidence["adapter_project_id"]
        == plan["adapter_project_id"],
        "validation_commands": evidence["validation_commands"]
        == plan["validation_commands"],
        "metadata_paths": evidence["metadata_paths"] == plan["metadata_paths"],
    }
    failed = [field for field, passed in exact_checks.items() if not passed]
    if failed:
        raise IntegrationPlanError(
            "immutable integration evidence changed before mutation: " + ", ".join(failed)
        )
    audit = _audit_accepted_commit(root, plan)
    try:
        lease.heartbeat_adopted(plan["lease_identity"])
    except LockError as exc:
        raise IntegrationPlanError(str(exc)) from exc

    runtime = _write_runtime(
        root,
        plan,
        "validated",
        {"two_ref_validation": exact_checks, "accepted_commit_audit": audit},
    )
    if inspector.current_branch != plan["milestone_branch"]:
        _git_mutation(root, plan, ["switch", plan["milestone_branch"]])
    if (
        _git_output(root, "rev-parse", "HEAD") != plan["pre_integration_head"]
        or not RepositoryInspector(root).is_clean
        or any(RepositoryInspector(root).git_operation_state().values())
    ):
        raise IntegrationPlanError("milestone ref changed immediately before cherry-pick")
    try:
        lease.heartbeat_adopted(plan["lease_identity"])
    except LockError as exc:
        raise IntegrationPlanError(str(exc)) from exc
    picked = _git_mutation(
        root,
        plan,
        ["cherry-pick", plan["accepted_commit"]],
        check=False,
    )
    if picked.returncode != 0:
        conflicts = _git(
            root, ["diff", "--name-only", "--diff-filter=U"], check=False
        ).stdout.splitlines()
        result = {
            **_result_base(plan, "SEMANTIC_CONFLICT"),
            "current_commit": _git_output(root, "rev-parse", "HEAD"),
            "resulting_feature_commit": None,
            "evidence_commit": None,
            "changed_paths": sorted(set(conflicts)),
            "validation": None,
            "runtime": runtime,
            "human_gate": {
                "gate_id": (
                    f"{_controller_project_id(plan)}-{plan['feature_id']}-"
                    f"semantic-conflict-{plan['plan_fingerprint'][:12]}"
                ),
                "classification": "semantic_integration_conflict",
                "reason": "The exact accepted commit produced a preserved cherry-pick conflict.",
                "feature_id": plan["feature_id"],
                "accepted_feature_commit": plan["accepted_commit"],
                "milestone_branch": plan["milestone_branch"],
                "milestone_head": plan["pre_integration_head"],
                "conflicting_paths": sorted(set(conflicts)),
                "ordinary_resume_allowed": False,
                "resolved": False,
            },
        }
        result["runtime"] = _write_runtime(root, plan, "conflict", result)
        return result

    resulting_feature_commit = _git_output(root, "rev-parse", "HEAD")
    if _git_output(root, "rev-parse", f"{resulting_feature_commit}^") != plan[
        "pre_integration_head"
    ]:
        raise IntegrationPlanError("cherry-picked feature commit is not a direct child of the plan start")
    if inspector.patch_fingerprint(resulting_feature_commit) != inspector.patch_fingerprint(
        plan["accepted_commit"]
    ):
        raise IntegrationPlanError("cherry-picked feature patch differs from the accepted commit")
    integrating_commit = _mark_integrating(root, plan)
    _write_runtime(
        root,
        plan,
        "integrating",
        {
            "resulting_feature_commit": resulting_feature_commit,
            "integrating_metadata_commit": integrating_commit,
        },
    )
    try:
        lease.heartbeat_adopted(plan["lease_identity"])
    except LockError as exc:
        raise IntegrationPlanError(str(exc)) from exc
    validation = _run_validation(root, plan)
    final = _record_final_metadata(root, plan, validation, resulting_feature_commit)
    classification = "INTEGRATED" if validation["ok"] else "VALIDATION_FAILED"
    current_commit = _git_output(root, "rev-parse", "HEAD")
    result = {
        **_result_base(plan, classification),
        "current_commit": current_commit,
        "resulting_feature_commit": resulting_feature_commit,
        "integrating_metadata_commit": integrating_commit,
        "evidence_commit": final["evidence_commit"],
        "changed_paths": sorted(
            set(
                _git_output(
                    root,
                    "diff",
                    "--name-only",
                    plan["pre_integration_head"],
                    current_commit,
                ).splitlines()
            )
        ),
        "validation": validation,
        "milestone_complete": final["milestone_complete"],
        "stop_reason": final["stop_reason"],
        "runtime": runtime,
        "human_gate": None,
    }
    result["runtime"] = _write_runtime(
        root,
        plan,
        "complete" if validation["ok"] else "validation_failed",
        result,
    )
    if not RepositoryInspector(root).is_clean:
        raise IntegrationPlanError("deterministic integration did not leave a clean worktree")
    lease.revalidate_adopted(plan["lease_identity"])
    return result


def validate_integration_result(
    plan: dict[str, Any], result: dict[str, Any]
) -> None:
    """Validate machine-readable executor output before terminal ledger evidence."""

    required = {
        "schema_version",
        "classification",
        "plan_fingerprint",
        "project_id",
        "repository_identity",
        "repository_path_fingerprint",
        "transaction_id",
        "run_id",
        "feature_id",
        "feature_branch",
        "accepted_commit",
        "milestone_id",
        "milestone_branch",
        "starting_commit",
        "lease_id",
        "model_session_launched",
        "current_commit",
        "resulting_feature_commit",
        "evidence_commit",
        "changed_paths",
        "validation",
        "runtime",
        "human_gate",
    }
    missing = sorted(required - set(result))
    if missing:
        raise IntegrationPlanError(
            "deterministic integration result is missing: " + ", ".join(missing)
        )
    identity = {
        "schema_version": result.get("schema_version") == RESULT_SCHEMA_VERSION,
        "plan_fingerprint": result.get("plan_fingerprint") == plan["plan_fingerprint"],
        "project_id": result.get("project_id") == _controller_project_id(plan),
        "repository_identity": result.get("repository_identity")
        == plan["repository_identity"],
        "repository_path_fingerprint": result.get("repository_path_fingerprint")
        == plan["repository_path_fingerprint"],
        "transaction_id": result.get("transaction_id") == plan["transaction_id"],
        "run_id": result.get("run_id") == plan["run_id"],
        "feature_id": result.get("feature_id") == plan["feature_id"],
        "feature_branch": result.get("feature_branch") == plan["feature_branch"],
        "accepted_commit": result.get("accepted_commit") == plan["accepted_commit"],
        "milestone_id": result.get("milestone_id") == plan["milestone_id"],
        "milestone_branch": result.get("milestone_branch") == plan["milestone_branch"],
        "starting_commit": result.get("starting_commit") == plan["pre_integration_head"],
        "lease_id": result.get("lease_id") == plan["lease_identity"]["lease_id"],
        "model_session": result.get("model_session_launched") is False,
    }
    failed = [field for field, passed in identity.items() if not passed]
    if failed:
        raise IntegrationPlanError(
            "deterministic integration result identity mismatch: " + ", ".join(failed)
        )
    if result.get("classification") not in {
        "INTEGRATED",
        "VALIDATION_FAILED",
        "SEMANTIC_CONFLICT",
    }:
        raise IntegrationPlanError("deterministic integration classification is unsupported")
    if not isinstance(result.get("changed_paths"), list) or result[
        "changed_paths"
    ] != sorted(set(result["changed_paths"])):
        raise IntegrationPlanError("deterministic integration changed_paths are invalid")
