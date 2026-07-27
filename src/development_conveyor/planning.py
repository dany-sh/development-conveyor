"""Phase-scoped queue-reconciliation transactions and planning-only commits."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .contracts import extract_terminal_envelope, fingerprint
from .errors import ConfigurationError, QueueError, RecoveryError
from .execution_profiles import (
    markdown_execution_policy,
    render_markdown_execution_policy,
    resolve_feature_execution_policy,
    validate_feature_execution_policy,
)
from .logging import atomic_write_bytes, atomic_write_json, utc_now
from .queue import FeatureQueue, load_queue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import parse_reconciliation_result
from .warning_evidence import (
    WarningEvidenceError,
    compare_warning_evidence,
    normalize_warning_evidence,
)


PLANNING_CLASSIFICATIONS = {
    "reconciled_ready_work": "RECONCILED_READY_WORK",
    "reconciled_no_ready_work": "RECONCILED_NO_READY_WORK",
    "human_decision_required": "HUMAN_DECISION_REQUIRED",
    "milestone_complete": "MILESTONE_COMPLETE",
    "legitimately_blocked": "RECONCILED_NO_READY_WORK",
    "invalid_queue": "PLANNING_VALIDATION_FAILED",
    "structured_output_invalid": "PLANNING_VALIDATION_FAILED",
    "session_execution_failed": "RETRYABLE_PLANNING_FAILURE",
}

ALLOWED_PLANNING_FILES = {
    ".factory/project.yaml",
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/README.md",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/architecture.md",
    "docs/data-flow.md",
}

ALLOWED_PLANNING_PREFIXES = (
    "docs/roadmap/",
    "docs/features/",
    "docs/product/",
    "docs/architecture/",
    "docs/testing/",
)

FULL_INVENTORY_PATHS = (
    ".factory/project.yaml",
    ".factory/approved-content.yaml",
    ".factory/locks/.gitignore",
    "docs/PRODUCT_VISION.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/CURRENT_STATUS.md",
    "docs/AUTONOMY_CONTRACT.md",
    "docs/RUN_LOG.md",
    "docs/features",
    "docs/adr",
)

POST_INTEGRATION_PLANNING_RECOVERY_FEATURE = "F070"
PREINSPECTION_ROLE_LAUNCH_ERROR = (
    "Error: failed to initialize in-process app-server client: "
    "Operation not permitted (os error 1)"
)
PREINSPECTION_ROLE_LAUNCHES = {
    "product-architect",
    "feature-inventory-lead",
}

LEGACY_WARNING_SUMMARY_COMPATIBILITY = {
    "project_id": "interview-companion",
    "transaction_id": "13b0828a-68e7-48a6-8c74-af5d3d411d26",
    "run_id": "826d9612-0cb1-441d-91ca-7531e64295bd",
    "session_id": "019f8e4c-19b4-7341-b860-90a784efa190",
    "report_fingerprint": "bba3dd65f169fa6402425a7e7b62ed2b5ec779189170de6441afc58d232c2bac",
    "result_classification": "RECONCILED_READY_WORK",
    "failed_recovery_transaction_id": "66910386-790b-4c32-9e78-830e2b881faf",
    "failed_recovery_run_id": "recovery-cf70c7b4-bcc2-4e07-9060-eee52d8ff6c9",
    "failed_recovery_sequences": list(range(339, 346)),
    "summary": "M1-M9 preparation metadata warnings only",
    "error": "queue validation warnings is malformed",
    "changed_paths": [
        "docs/CURRENT_STATUS.md",
        "docs/FEATURE_CATALOG.md",
        "docs/FEATURE_QUEUE.yaml",
        "docs/ROADMAP.md",
        "docs/RUN_LOG.md",
        "docs/architecture.md",
        "docs/features/F008-audio-engine-abstraction.md",
        "docs/features/F009-transcription-engine-abstraction.md",
    ],
}


def normalize_corroborated_queue_reconciliation_report(
    report: dict[str, Any],
    *,
    project: Project,
    repository_identity: str,
    transaction_id: str,
    run_id: str,
    session_id: str,
    starting_branch: str,
    starting_commit: str,
) -> dict[str, Any]:
    """Reconstruct a compatible terminal result from immutable controller metadata."""

    if (
        report.get("structured_output_validation") == "valid"
        and isinstance(report.get("structured_result"), dict)
    ):
        return report
    envelope = extract_terminal_envelope(
        str(report.get("redacted_stdout") or ""),
        corroborated={
            "workflow_type": "queue_reconciliation",
            "project_id": project.project_id,
            "repository_identity": repository_identity,
            "transaction_id": transaction_id,
            "run_id": run_id,
            "session_id": session_id,
            "starting_branch": starting_branch,
            "starting_commit": starting_commit,
        },
    ).to_dict()
    child_attempts: list[dict[str, Any]] = []
    for line in str(report.get("redacted_stdout") or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
            or item.get("tool") not in {"spawn_agent", "resume_agent"}
        ):
            continue
        child_attempts.append({
            "event_type": event.get("type"),
            "tool": item.get("tool"),
            "receiver_thread_ids": list(item.get("receiver_thread_ids") or []),
            "status": item.get("status"),
        })
    return {
        **report,
        "structured_result": envelope,
        "parsed_structured_result": envelope,
        "structured_output_validation": "valid",
        "result_classification": envelope["classification"],
        "structured_output_errors": [],
        "failed_semantic_checks": [],
        "controller_terminal_normalization": {
            "source_structured_output_validation": report.get(
                "structured_output_validation"
            ),
            "source_result_classification": report.get("result_classification"),
            "classification": envelope["classification"],
            "next_state": envelope["next_state"],
            "child_session_attempts": child_attempts,
        },
    }


def stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def queue_fingerprint(project: Project) -> str:
    return hashlib.sha256((project.repository / project.queue_location).read_bytes()).hexdigest()


def allowed_planning_path(path: str) -> bool:
    return path in ALLOWED_PLANNING_FILES or path.startswith(ALLOWED_PLANNING_PREFIXES)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _planning_diff_fingerprint_with_replacements(
    inspector: RepositoryInspector,
    replacements: dict[str, bytes],
) -> str:
    """Predict the exact Git binary diff without writing the application repository."""

    changed_paths = sorted(
        set(inspector.tracked_changed_paths())
        | set(inspector.untracked_file_hashes())
        | set(replacements)
    )
    if not changed_paths:
        return hashlib.sha256(b"").hexdigest()
    with tempfile.TemporaryDirectory(prefix="conveyor-policy-diff-") as temporary:
        root = Path(temporary)
        index = root / "index"
        objects = root / "objects"
        objects.mkdir()
        environment = {
            **os.environ,
            "GIT_INDEX_FILE": str(index),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                inspector.common_git_dir / "objects"
            ),
        }

        def run(
            argv: list[str],
            *,
            input_bytes: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            result = subprocess.run(
                ["git", *argv],
                cwd=inspector.root,
                env=environment,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                raise RecoveryError(
                    "cannot predict normalized planning diff: "
                    + result.stderr.decode("utf-8", errors="replace").strip()
                )
            return result

        run(["read-tree", "HEAD"])
        run(["add", "--all", "--", *changed_paths])
        for relative, content in sorted(replacements.items()):
            stage = run(["ls-files", "-s", "--", relative]).stdout.decode(
                "utf-8", errors="strict"
            ).strip()
            if not stage:
                raise RecoveryError(
                    f"normalized planning path cannot be staged: {relative}"
                )
            mode = stage.split(maxsplit=1)[0]
            blob = run(["hash-object", "-w", "--stdin"], input_bytes=content).stdout.decode(
                "ascii"
            ).strip()
            run(["update-index", "--cacheinfo", f"{mode},{blob},{relative}"])
        tree = run(["write-tree"]).stdout.decode("ascii").strip()
        patch = run(["diff", "--binary", "--no-ext-diff", "HEAD", tree, "--"]).stdout
        return hashlib.sha256(patch).hexdigest()


def _feature_specification_policy(
    project: Project,
    feature: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None]:
    feature_id = str(feature.get("id") or "")
    relative = feature.get("spec")
    if not isinstance(relative, str) or not relative:
        raise RecoveryError(
            f"newly ready feature {feature_id} lacks an authoritative specification path"
        )
    path = project.repository / relative
    if not path.is_file():
        raise RecoveryError(
            f"newly ready feature {feature_id} specification is missing: {relative}"
        )
    try:
        text = path.read_text(encoding="utf-8")
        policy = markdown_execution_policy(feature_id, text, required=False)
    except (OSError, UnicodeError, QueueError) as exc:
        raise RecoveryError(
            f"newly ready feature {feature_id} specification policy is invalid"
        ) from exc
    return relative, text, policy


def plan_new_ready_execution_policy_completion(
    project: Project,
    inspector: RepositoryInspector,
    *,
    starting_head: str,
    profile_configuration: dict[str, Any] | None,
    required_missing_feature: str | None = None,
    authorized_retained_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve and predict canonical policy completion for newly ready work."""

    baseline_text = inspector.file_at_commit(starting_head, project.queue_location)
    try:
        baseline = FeatureQueue(json.loads(baseline_text or ""))
        document = load_queue(project.repository / project.queue_location)
        queue = FeatureQueue(
            document,
            project.repository / project.queue_location,
            project.repository,
        )
    except (json.JSONDecodeError, QueueError, ValueError) as exc:
        raise RecoveryError("execution-policy completion lacks authoritative queue evidence") from exc

    newly_ready = [
        feature
        for feature in queue.features
        if feature.get("status") == "ready"
        and (baseline.feature(str(feature.get("id") or "")) or {}).get("status")
        != "ready"
    ]
    if required_missing_feature is not None and (
        len(newly_ready) != 1
        or newly_ready[0].get("id") != required_missing_feature
    ):
        raise RecoveryError(
            "deterministic execution-policy completion requires exactly one "
            "matching newly ready feature"
        )
    if not newly_ready:
        if required_missing_feature is not None:
            raise RecoveryError(
                "deterministic execution-policy completion has no newly ready feature"
            )
        return {
            "required": False,
            "newly_ready_features": [],
            "normalization_paths": [],
            "replacements": {},
        }

    candidates: list[dict[str, Any]] = []
    for feature in newly_ready:
        feature_id = str(feature["id"])
        spec_relative, spec_text, spec_policy = _feature_specification_policy(
            project, feature
        )
        try:
            resolution = resolve_feature_execution_policy(
                feature=feature,
                project_id=project.project_id,
                specification_policy=spec_policy,
                configuration=profile_configuration,
            )
        except (ConfigurationError, QueueError) as exc:
            raise RecoveryError(
                f"newly ready feature {feature_id} execution policy is invalid or ambiguous"
            ) from exc
        missing_queue = "execution_policy" not in feature
        missing_spec = spec_policy is None
        candidates.append({
            **resolution,
            "specification_path": spec_relative,
            "specification_text": spec_text,
            "missing_queue_policy": missing_queue,
            "missing_specification_policy": missing_spec,
        })

    missing = [
        candidate
        for candidate in candidates
        if candidate["missing_queue_policy"]
        or candidate["missing_specification_policy"]
    ]
    if required_missing_feature is not None and (
        len(missing) != 1
        or missing[0]["feature_id"] != required_missing_feature
        or missing[0]["missing_queue_policy"] is not True
    ):
        raise RecoveryError(
            "recorded missing execution_policy failure does not match the "
            "authoritative newly ready feature"
        )
    if len(missing) > 1:
        raise RecoveryError(
            "multiple newly ready features require execution-policy judgment"
        )
    if not missing:
        return {
            "required": False,
            "newly_ready_features": sorted(
                candidate["feature_id"] for candidate in candidates
            ),
            "validated_policies": [
                {
                    key: candidate[key]
                    for key in (
                        "feature_id",
                        "execution_policy",
                        "resolved_execution_profile",
                        "policy_source",
                        "explicit_policy_preserved",
                    )
                }
                for candidate in candidates
            ],
            "normalization_paths": [],
            "replacements": {},
        }

    candidate = missing[0]
    feature_id = candidate["feature_id"]
    policy = candidate["execution_policy"]
    normalization_paths: list[str] = []
    replacements: dict[str, bytes] = {}
    if candidate["missing_queue_policy"]:
        raw_feature = next(
            item
            for item in document["features"]
            if item.get("id") == feature_id
        )
        raw_feature["execution_policy"] = policy
        normalization_paths.append(project.queue_location)
        replacements[project.queue_location] = _json_bytes(document)
    if candidate["missing_specification_policy"]:
        spec_relative = candidate["specification_path"]
        spec_text = candidate["specification_text"]
        normalized = spec_text.rstrip() + "\n\n" + render_markdown_execution_policy(policy)
        normalization_paths.append(spec_relative)
        replacements[spec_relative] = normalized.encode("utf-8")

    normalization_paths = sorted(set(normalization_paths))
    if authorized_retained_paths is not None:
        unauthorized = sorted(
            set(normalization_paths) - set(authorized_retained_paths)
        )
        if unauthorized:
            raise RecoveryError(
                "execution-policy normalization would introduce an "
                "unauthenticated retained path: "
                + ", ".join(unauthorized)
            )
    predicted = _planning_diff_fingerprint_with_replacements(
        inspector,
        replacements,
    )
    return {
        "required": True,
        "feature_id": feature_id,
        "newly_ready_features": sorted(
            item["feature_id"] for item in candidates
        ),
        "execution_policy": policy,
        "resolved_execution_profile": candidate["resolved_execution_profile"],
        "policy_source": candidate["policy_source"],
        "explicit_policy_preserved": candidate["explicit_policy_preserved"],
        "normalization_paths": normalization_paths,
        "predicted_final_diff_fingerprint": predicted,
        "replacements": replacements,
    }


def apply_new_ready_execution_policy_completion(
    project: Project,
    inspector: RepositoryInspector,
    *,
    starting_head: str,
    profile_configuration: dict[str, Any] | None,
    expected_plan: dict[str, Any],
    authorized_retained_paths: list[str],
    recorded_missing_policy_failure: bool = True,
) -> dict[str, Any]:
    """Revalidate and atomically apply one already-predicted policy completion."""

    feature_id = expected_plan.get("feature_id")
    if not isinstance(feature_id, str) or not feature_id:
        raise RecoveryError("execution-policy completion plan lacks a feature identity")
    refreshed = plan_new_ready_execution_policy_completion(
        project,
        inspector,
        starting_head=starting_head,
        profile_configuration=profile_configuration,
        required_missing_feature=(
            feature_id if recorded_missing_policy_failure else None
        ),
        authorized_retained_paths=authorized_retained_paths,
    )
    comparable = (
        "feature_id",
        "execution_policy",
        "resolved_execution_profile",
        "policy_source",
        "explicit_policy_preserved",
        "normalization_paths",
        "predicted_final_diff_fingerprint",
    )
    if any(refreshed.get(key) != expected_plan.get(key) for key in comparable):
        raise RecoveryError(
            "execution-policy completion changed after recovery authentication"
        )
    replacements = refreshed.pop("replacements")
    for relative in refreshed["normalization_paths"]:
        atomic_write_bytes(project.repository / relative, replacements[relative])
    if (
        inspector.planning_diff_fingerprint()
        != refreshed["predicted_final_diff_fingerprint"]
    ):
        raise RecoveryError(
            "execution-policy normalization produced an unexpected final fingerprint"
        )
    return refreshed


def worktree_fingerprint(inspector: RepositoryInspector) -> str:
    return stable_fingerprint({
        "tracked_diff": inspector.planning_diff_fingerprint(),
        "untracked": inspector.untracked_file_hashes(),
    })


def capture_planning_start(project: Project, inspector: RepositoryInspector, run_id: str) -> dict[str, Any]:
    if not inspector.is_clean:
        raise RecoveryError("a new planning transaction requires a clean repository")
    identity = inspector.identity()
    return {
        "schema_version": 1,
        "phase": "queue_reconciliation",
        "status": "planning_transaction_started",
        "repository": str(project.repository),
        "repository_identity": identity["repository_id"],
        "repository_path_fingerprint": identity["path_fingerprint"],
        "project_id": project.project_id,
        "milestone": project.active_milestone,
        "run_id": run_id,
        "session_ids": [],
        "branch": inspector.current_branch,
        "planning_start_commit": inspector.head,
        "clean": True,
        "tracked_diff_fingerprint": inspector.planning_diff_fingerprint(),
        "untracked_file_fingerprint": stable_fingerprint(inspector.untracked_file_hashes()),
        "starting_worktree_fingerprint": worktree_fingerprint(inspector),
        "queue_fingerprint": queue_fingerprint(project),
        "allowed_paths": sorted(ALLOWED_PLANNING_FILES) + list(ALLOWED_PLANNING_PREFIXES),
        "created_at": utc_now(),
    }


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"planning reconciliation report is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError("planning reconciliation report is not an object")
    return value


def validate_reconciliation_report(
    report: dict[str, Any],
    *,
    project: Project,
    run_id: str,
    expected_session_id: str | None = None,
    expected_transaction_id: str | None = None,
) -> dict[str, Any]:
    report_structured = report.get("structured_result")
    if isinstance(report_structured, dict) and report_structured.get("workflow_type") == "queue_reconciliation":
        canonical = report_structured.get("classification")
        reverse = {
            "RECONCILED_READY_WORK": "reconciled_ready_work",
            "RECONCILED_NO_READY_WORK": "reconciled_no_ready_work",
            "MILESTONE_COMPLETE": "milestone_complete",
            "HUMAN_DECISION_REQUIRED": "human_decision_required",
            "PLANNING_VALIDATION_FAILED": "invalid_queue",
            "RETRYABLE_PLANNING_FAILURE": "session_execution_failed",
            "TERMINAL_PLANNING_FAILURE": "session_execution_failed",
        }
        classification = reverse.get(canonical)
        evidence = report_structured.get("evidence")
        session_id = report_structured.get("session_id")
        checks = {
            "schema_version": report.get("schema_version") == 1,
            "project_id": report.get("project_id") == project.project_id,
            "run_id": report.get("run_id") == run_id,
            "action": report.get("action") == "queue_reconciliation",
            "working_directory": Path(str(report.get("working_directory") or "")).resolve() == project.repository.resolve(),
            "exit_status": report.get("exit_status") == 0,
            "typed_envelope": classification is not None,
            "envelope_project": report_structured.get("project_id") == project.project_id,
            "envelope_run": report_structured.get("run_id") == run_id,
            "session_id": isinstance(session_id, str) and bool(session_id),
            "expected_session_id": expected_session_id is None or session_id == expected_session_id,
            "expected_transaction_id": (
                expected_transaction_id is None
                or report_structured.get("transaction_id") == expected_transaction_id
            ),
            "evidence": isinstance(evidence, dict),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(f"planning typed result validation failed: {failed}")
        return {
            "session_id": session_id,
            "classification": classification,
            "terminal_classification": canonical,
            "structured_result": evidence,
            "checks": checks,
        }
    parsed, marker_validation = parse_reconciliation_result(str(report.get("redacted_stdout") or ""))
    session_id = report.get("session_id")
    checks = {
        "schema_version": report.get("schema_version") == 1,
        "project_id": report.get("project_id") == project.project_id,
        "run_id": report.get("run_id") == run_id,
        "action": report.get("action") == "queue_reconciliation",
        "working_directory": Path(str(report.get("working_directory") or "")).resolve()
        == project.repository.resolve(),
        "exit_status": report.get("exit_status") == 0,
        "single_terminal_marker": marker_validation == "valid",
        "structured_result_matches_terminal": parsed == report_structured,
        "structured_output_valid": report.get("structured_output_validation") == "valid",
        "session_id": isinstance(session_id, str) and bool(session_id),
        "expected_session_id": expected_session_id is None or session_id == expected_session_id,
        "expected_transaction_id": expected_transaction_id is None,
    }
    if not all(checks.values()):
        failed = ", ".join(key for key, passed in checks.items() if not passed)
        raise RecoveryError(f"planning reconciliation report validation failed: {failed}")
    classification = str((parsed or {}).get("classification") or "")
    terminal = PLANNING_CLASSIFICATIONS.get(classification)
    if terminal is None:
        raise RecoveryError(f"unsupported planning terminal classification: {classification!r}")
    return {
        "session_id": session_id,
        "classification": classification,
        "terminal_classification": terminal,
        "structured_result": parsed,
        "checks": checks,
    }


def _classify_inventory_validation(
    *,
    exit_code: int,
    value: Any,
    blocking_warning_patterns: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError("deterministic inventory validator returned a non-object result")
    errors = value.get("errors")
    warnings = value.get("warnings")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        raise RecoveryError("deterministic inventory validator returned malformed errors or warnings")
    if not all(isinstance(item, str) for item in errors + warnings):
        raise RecoveryError("deterministic inventory validator returned non-string diagnostics")
    blocking_warnings = [
        warning
        for warning in warnings
        if any(fnmatchcase(warning, pattern) for pattern in blocking_warning_patterns)
    ]
    if exit_code != 0 or errors or blocking_warnings:
        raise RecoveryError(
            "deterministic inventory validation failed: "
            + json.dumps(
                {
                    "exit_code": exit_code,
                    "errors": errors,
                    "warnings": warnings,
                    "blocking_warnings": blocking_warnings,
                },
                sort_keys=True,
            )
        )
    return {
        **value,
        "ok": True,
        "exit_code": exit_code,
        "errors": errors,
        "warnings": warnings,
        "nonfatal_warnings": warnings,
        "blocking_warnings": [],
        "warning_policy": {
            "blocking_patterns": list(blocking_warning_patterns),
            "warnings_are_nonfatal_by_default": True,
        },
    }


def authoritative_queue_validation_evidence(
    project: Project,
    queue: FeatureQueue | None = None,
) -> dict[str, Any]:
    """Return the one authoritative milestone-local/global queue count contract."""

    queue = queue or FeatureQueue.from_location(project.repository, project.queue_location)
    configured = project.active_milestone
    if not isinstance(configured, str) or not configured:
        raise RecoveryError("active milestone cannot be resolved for queue validation")
    milestone = queue.milestone(configured)
    if milestone is None:
        raise RecoveryError(
            f"active milestone {configured!r} cannot be resolved in the authoritative queue"
        )
    active = [item for item in queue.milestones if item.get("status") == "active"]
    if len(active) != 1 or active[0].get("id") != milestone.get("id"):
        raise RecoveryError(
            "authoritative queue active milestone differs from the configured active milestone"
        )
    summary = queue.summary(configured)
    return {
        "ok": True,
        "valid": True,
        "milestone_found": True,
        "active_milestone": milestone["id"],
        "feature_count": summary["feature_count"],
        "global_feature_count": len(queue.features),
        "global_milestone_count": len(queue.milestones),
        "ready": summary["ready_features"],
        "active": summary["active_features"],
        "warning_count": 0,
        "warnings": [],
        "blocking_warnings": [],
    }


def _inventory_validation(project: Project) -> dict[str, Any]:
    root = project.repository
    queue = FeatureQueue.from_location(root, project.queue_location)
    authoritative = authoritative_queue_validation_evidence(project, queue)
    if not all((root / relative).exists() for relative in FULL_INVENTORY_PATHS):
        return _classify_inventory_validation(
            exit_code=0,
            value={
                **authoritative,
                "errors": [],
                "warnings": [],
                "validator": "controller_queue_validator",
            },
            blocking_warning_patterns=project.inventory_blocking_warning_patterns,
        )
    validator = Path.home() / ".agents/skills/feature-inventory/scripts/validate_inventory.py"
    if not validator.is_file():
        raise RecoveryError("deterministic feature-inventory validator is unavailable")
    result = subprocess.run(
        ["python3", str(validator), "--root", str(root)],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryError("deterministic inventory validator returned malformed JSON") from exc
    classified = _classify_inventory_validation(
        exit_code=result.returncode,
        value=value,
        blocking_warning_patterns=project.inventory_blocking_warning_patterns,
    )
    reported_global_features = classified.get("global_feature_count", classified.get("feature_count"))
    reported_global_milestones = classified.get(
        "global_milestone_count", classified.get("milestone_count")
    )
    if reported_global_features != authoritative["global_feature_count"]:
        raise RecoveryError(
            "deterministic inventory global feature count disagrees with the authoritative queue"
        )
    if reported_global_milestones != authoritative["global_milestone_count"]:
        raise RecoveryError(
            "deterministic inventory global milestone count disagrees with the authoritative queue"
        )
    return {
        **classified,
        **authoritative,
        "errors": classified["errors"],
        "warnings": classified["warnings"],
        "warning_count": len(classified["warnings"]),
        "blocking_warnings": classified["blocking_warnings"],
        "raw_inventory_counts": {
            "feature_count": classified.get("feature_count"),
            "milestone_count": classified.get("milestone_count"),
            "global_feature_count": classified.get("global_feature_count"),
            "global_milestone_count": classified.get("global_milestone_count"),
        },
        "validator": str(validator),
    }


def _recorded_post_integration_validation_evidence(
    session_report: dict[str, Any],
    project: Project,
    queue: FeatureQueue,
) -> dict[str, Any]:
    """Authenticate already-recorded validators without executing application commands."""

    inventory_event: dict[str, Any] | None = None
    diff_event: dict[str, Any] | None = None
    for line in str(session_report.get("redacted_stdout") or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") != "item.completed"
            or not isinstance(item, dict)
            or item.get("type") != "command_execution"
            or item.get("exit_code") != 0
        ):
            continue
        command = str(item.get("command") or "")
        if (
            "feature-inventory/scripts/validate_inventory.py --root ." in command
            and inventory_event is None
        ):
            inventory_event = item
        if "git diff --check" in command and diff_event is None:
            diff_event = item
    if inventory_event is None or diff_event is None:
        raise RecoveryError(
            "post-integration planning recovery lacks recorded deterministic validation"
        )
    try:
        raw_inventory = json.loads(
            str(inventory_event.get("aggregated_output") or "")
        )
    except json.JSONDecodeError as exc:
        raise RecoveryError(
            "recorded post-integration inventory validation is malformed"
        ) from exc
    classified = _classify_inventory_validation(
        exit_code=0,
        value=raw_inventory,
        blocking_warning_patterns=project.inventory_blocking_warning_patterns,
    )
    authoritative = authoritative_queue_validation_evidence(project, queue)
    if (
        classified.get("feature_count")
        != authoritative["global_feature_count"]
        or classified.get("milestone_count")
        != authoritative["global_milestone_count"]
    ):
        raise RecoveryError(
            "recorded post-integration inventory counts disagree with the authoritative queue"
        )
    inventory = {
        **classified,
        **authoritative,
        "errors": classified["errors"],
        "warnings": classified["warnings"],
        "warning_count": len(classified["warnings"]),
        "blocking_warnings": classified["blocking_warnings"],
        "raw_inventory_counts": {
            "feature_count": classified.get("feature_count"),
            "milestone_count": classified.get("milestone_count"),
            "global_feature_count": classified.get("global_feature_count"),
            "global_milestone_count": classified.get("global_milestone_count"),
        },
        "validator": "recorded feature-inventory validator",
        "evidence_source": "authenticated_parent_session_report",
    }
    return {
        "inventory": inventory,
        "commands": [
            {
                "command": [
                    "python3",
                    "~/.agents/skills/feature-inventory/scripts/validate_inventory.py",
                    "--root",
                    ".",
                ],
                "exit_code": 0,
                "source": "authenticated_parent_session_report",
            },
            {
                "command": ["git", "diff", "--check"],
                "exit_code": 0,
                "source": "authenticated_parent_session_report",
            },
        ],
    }


def _diff_check(project: Project) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "diff", "--check"],
        cwd=project.repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RecoveryError(
            "planning git diff --check failed: " + (result.stdout.strip() or result.stderr.strip())
        )
    return {"ok": True, "exit_code": 0}


def _selected_feature_evidence(
    project: Project,
    queue: FeatureQueue,
    classification: str,
) -> dict[str, Any]:
    summary = queue.summary(project.active_milestone or "")
    ready = list(summary.get("ready_features") or [])
    selected = queue.select_next(project.active_milestone or "")
    selected_id = selected.feature_id if selected else None
    if classification == "reconciled_ready_work":
        if len(ready) != 1 or selected_id != ready[0]:
            raise RecoveryError(
                "RECONCILED_READY_WORK requires exactly one deterministically selected ready feature"
            )
        feature = queue.feature(selected_id)
        dependencies = list((feature or {}).get("dependencies") or [])
        statuses = {
            dependency: (queue.feature(dependency) or {}).get("status") for dependency in dependencies
        }
        if not all(status in {"done", "integrated"} for status in statuses.values()):
            raise RecoveryError("selected planning feature has incomplete dependencies")
        if (feature or {}).get("requires_human_decision") is True:
            raise RecoveryError("selected planning feature has an unresolved human decision")
        return {
            "selected_feature": selected_id,
            "ready_features": ready,
            "dependencies": dependencies,
            "dependency_statuses": statuses,
            "dependencies_complete": True,
            "human_decision_required": False,
        }
    if classification == "reconciled_no_ready_work" and ready:
        raise RecoveryError("RECONCILED_NO_READY_WORK contradicts ready queue entries")
    return {
        "selected_feature": None,
        "ready_features": ready,
        "dependencies": [],
        "dependency_statuses": {},
        "dependencies_complete": True,
        "human_decision_required": classification == "human_decision_required",
    }


def _resolve_reported_feature_identity(
    result_queue: dict[str, Any],
    selection: dict[str, Any],
    classification: str,
) -> str | None:
    """Corroborate an explicit selection or derive one sole ready feature."""

    ready = result_queue.get("ready_features", result_queue.get("ready"))
    if not isinstance(ready, list) or not all(
        isinstance(item, str) and item for item in ready
    ):
        raise RecoveryError("session queue readiness evidence is malformed")
    if len(ready) != len(set(ready)):
        raise RecoveryError("session queue readiness evidence contains duplicates")
    deterministic_ready = list(selection.get("ready_features") or [])
    if sorted(ready) != sorted(deterministic_ready):
        raise RecoveryError(
            "session queue evidence disagrees with deterministic recovery selection"
        )

    explicit = result_queue.get("selected_feature")
    explicit_present = "selected_feature" in result_queue and explicit is not None
    if explicit_present and (
        not isinstance(explicit, str) or not explicit.strip()
    ):
        raise RecoveryError("session selected-feature evidence is empty or malformed")

    selected = selection.get("selected_feature")
    if classification == "reconciled_ready_work":
        if len(ready) != 1 or len(deterministic_ready) != 1 or selected != ready[0]:
            raise RecoveryError(
                "RECONCILED_READY_WORK requires exactly one corroborated ready feature"
            )
        if explicit_present and explicit != selected:
            raise RecoveryError(
                "session selected-feature evidence conflicts with ready queue evidence"
            )
        return str(selected)

    if explicit_present and explicit != selected:
        raise RecoveryError(
            "session selected-feature evidence conflicts with deterministic queue evidence"
        )
    return selected


def _validate_transparent_planning_checkpoints(
    *,
    checkpoints: list[dict[str, Any]],
    transaction_id: str,
    workflow_type: str,
    run_id: str,
    session_id: str,
    starting_branch: str,
    starting_head: str,
    lease_payload: dict[str, Any],
    mutation_policy: dict[str, Any],
    source_paths: list[str],
    retained_paths: list[str],
    retained_diff_fingerprint: str,
    selected_feature: str | None,
) -> dict[str, Any]:
    """Authenticate checkpoint metadata without assigning protocol meaning to names."""

    allowed_paths = set(mutation_policy.get("allowed_paths") or [])
    allowed_prefixes = tuple(
        str(item).rstrip("/")
        for item in (mutation_policy.get("allowed_prefixes") or [])
    )

    def path_authorized(path: str) -> bool:
        return path in allowed_paths or any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in allowed_prefixes
        )

    for event in checkpoints:
        if (
            event.get("transaction_id") != transaction_id
            or event.get("workflow_type") != workflow_type
        ):
            raise RecoveryError("planning checkpoint workflow identity conflicts")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise RecoveryError("planning checkpoint payload is malformed")
        scalar_expectations = {
            "transaction_id": transaction_id,
            "run_id": run_id,
            "session_id": session_id,
            "starting_branch": starting_branch,
            "branch": starting_branch,
            "starting_head": starting_head,
            "starting_commit": starting_head,
            "head": starting_head,
            "current_commit": starting_head,
            "selected_feature": selected_feature,
        }
        for key, expected in scalar_expectations.items():
            if key in payload and payload.get(key) != expected:
                raise RecoveryError(
                    f"planning checkpoint {key} conflicts with authoritative identity"
                )
        for key in ("lease_id", "lease_type", "owner_pid", "owner_process_start"):
            if key in payload and payload.get(key) != lease_payload.get(key):
                raise RecoveryError(
                    f"planning checkpoint {key} conflicts with lease ownership"
                )
        if (
            "allowed_mutation_policy" in payload
            and payload.get("allowed_mutation_policy") != mutation_policy
        ):
            raise RecoveryError("planning checkpoint mutation authority conflicts")
        for key in ("mutation_authority", "authority"):
            if key in payload and payload.get(key) != mutation_policy:
                raise RecoveryError(
                    f"planning checkpoint {key} conflicts with mutation authority"
                )
        for key in (
            "allowed_paths",
            "authorized_paths",
            "allowed_prefixes",
            "denied_paths",
            "denied_prefixes",
            "allow_untracked",
            "commit_subject",
        ):
            policy_key = "allowed_paths" if key == "authorized_paths" else key
            if key in payload and payload.get(key) != mutation_policy.get(policy_key):
                raise RecoveryError(
                    f"planning checkpoint {key} conflicts with mutation authority"
                )

        path_expectations = {
            "source_paths": sorted(source_paths),
            "changed_paths": sorted(retained_paths),
            "final_paths": sorted(retained_paths),
        }
        for key, expected in path_expectations.items():
            if key in payload:
                value = payload.get(key)
                if not isinstance(value, list) or sorted(value) != expected:
                    raise RecoveryError(
                        f"planning checkpoint {key} conflicts with retained paths"
                    )
        if "normalization_paths" in payload:
            normalization = payload.get("normalization_paths")
            if (
                not isinstance(normalization, list)
                or not all(isinstance(path, str) for path in normalization)
                or len(normalization) != len(set(normalization))
                or not set(normalization).issubset(retained_paths)
                or not all(path_authorized(path) for path in normalization)
            ):
                raise RecoveryError(
                    "planning checkpoint normalization paths conflict with authority"
                )
        for key in (
            "diff_fingerprint",
            "final_diff_fingerprint",
            "tracked_diff_fingerprint",
            "retained_diff_fingerprint",
        ):
            if key in payload and payload.get(key) != retained_diff_fingerprint:
                raise RecoveryError(
                    f"planning checkpoint {key} conflicts with retained fingerprint"
                )
        if "session_ids" in payload and payload.get("session_ids") != [session_id]:
            raise RecoveryError("planning checkpoint session list conflicts")
        for key in (
            "models_planned",
            "model_sessions_planned",
            "model_session_count",
            "parent_sessions",
            "parent_sessions_used",
        ):
            if key in payload and payload.get(key) != 1:
                raise RecoveryError(
                    f"planning checkpoint {key} changes model-session identity"
                )
        for key in (
            "children_planned",
            "child_sessions_planned",
            "child_sessions",
            "child_sessions_used",
        ):
            if key in payload and payload.get(key) != 0:
                raise RecoveryError(
                    f"planning checkpoint {key} changes child-session authority"
                )
        if (
            "model_session_launched" in payload
            and not isinstance(payload.get("model_session_launched"), bool)
        ):
            raise RecoveryError("planning checkpoint model-session flag is malformed")
        if (
            "model_sessions_launched" in payload
            and payload.get("model_sessions_launched")
            not in ([], [session_id])
        ):
            raise RecoveryError(
                "planning checkpoint model session list conflicts"
            )
        if (
            "child_sessions_launched" in payload
            and payload.get("child_sessions_launched") != []
        ):
            raise RecoveryError(
                "planning checkpoint child session list conflicts"
            )

    return {
        "count": len(checkpoints),
        "transparent": True,
        "sequences": [event.get("sequence") for event in checkpoints],
    }


def _recoverable_planning_validation_failure(transaction: dict[str, Any]) -> dict[str, Any]:
    message = transaction.get("error")
    prefix = "deterministic inventory validation failed: "
    semantic_prefix = "queue validation evidence disagrees semantically: "
    policy_prefix = "newly readied feature "
    policy_suffix = " lacks required execution_policy"
    if (
        isinstance(message, str)
        and message.startswith(policy_prefix)
        and message.endswith(policy_suffix)
    ):
        feature_id = message[
            len(policy_prefix):len(message) - len(policy_suffix)
        ]
        if (
            not feature_id
            or feature_id.strip() != feature_id
            or any(character.isspace() for character in feature_id)
        ):
            raise RecoveryError(
                "blocked planning execution-policy failure identity is malformed"
            )
        if (
            transaction.get("failure_classification")
            != "PLANNING_VALIDATION_FAILED"
            or transaction.get("result_classification")
            != "RECONCILED_READY_WORK"
        ):
            raise RecoveryError(
                "blocked planning execution-policy failure classification is contradictory"
            )
        return {
            "classification": "deterministic_execution_policy_completion",
            "exit_code": 0,
            "errors": [],
            "warnings": [],
            "historical_disagreements": [],
            "missing_execution_policy_feature": feature_id,
        }
    if isinstance(message, str) and message.startswith(prefix):
        try:
            failure = json.loads(message.removeprefix(prefix))
        except json.JSONDecodeError as exc:
            raise RecoveryError("blocked planning inventory failure evidence is malformed") from exc
        if not isinstance(failure, dict):
            raise RecoveryError("blocked planning inventory failure evidence is malformed")
        errors = failure.get("errors")
        warnings = failure.get("warnings")
        if (
            failure.get("exit_code") != 0
            or errors != []
            or not isinstance(warnings, list)
            or not warnings
            or not all(isinstance(item, str) for item in warnings)
        ):
            raise RecoveryError(
                "blocked planning failure is not warning-only deterministic validation"
            )
        return {
            "classification": "warning_only_inventory_failure",
            "exit_code": 0,
            "errors": [],
            "warnings": warnings,
            "historical_disagreements": [],
        }
    if isinstance(message, str) and message.startswith(semantic_prefix):
        disagreements = sorted(
            {item.strip() for item in message.removeprefix(semantic_prefix).split(",") if item.strip()}
        )
        count_fields = {
            "feature_count",
            "global_feature_count",
            "global_milestone_count",
            "milestone_count",
            "configured_milestone_feature_count",
        }
        if not disagreements or not set(disagreements).issubset(count_fields):
            raise RecoveryError(
                "blocked planning semantic failure is not count-validation-only"
            )
        return {
            "classification": "historical_count_semantics_failure",
            "exit_code": 0,
            "errors": [],
            "warnings": [],
            "historical_disagreements": disagreements,
        }
    selected_spec_prefix = "planning semantic agreement failed for docs/features/"
    selected_spec_suffix = ": selected feature readiness is absent"
    if (
        isinstance(message, str)
        and message.startswith(selected_spec_prefix)
        and message.endswith(selected_spec_suffix)
    ):
        relative = message[
            len("planning semantic agreement failed for "):
            -len(selected_spec_suffix)
        ]
        return {
            "classification": "obsolete_selected_spec_readiness_check",
            "exit_code": 0,
            "errors": [],
            "warnings": [],
            "historical_disagreements": [],
            "selected_specification_path": relative,
        }
    if message == LEGACY_WARNING_SUMMARY_COMPATIBILITY["error"]:
        return {
            "classification": "historical_warning_summary_shape",
            "exit_code": 0,
            "errors": [],
            "warnings": [],
            "historical_disagreements": [],
        }
    raise RecoveryError(
        "blocked planning transaction is not a recoverable deterministic validation failure"
    )


def _preinspection_role_launch_failure_evidence(
    session_report: dict[str, Any],
) -> dict[str, Any]:
    """Authenticate nested role launches that never crossed process initialization."""

    events: list[dict[str, Any]] = []
    for line in str(session_report.get("redacted_stdout") or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    started: dict[str, tuple[int, str]] = {}
    failures: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        item_id = item.get("id")
        command = str(item.get("command") or "")
        shell_payload = command.partition("-lc")[2].lstrip().lstrip("\"'")
        first_command = shell_payload.split(";", 1)[0].strip()
        roles = sorted(
            role
            for role in PREINSPECTION_ROLE_LAUNCHES
            if role in command
        )
        is_direct_codex_exec = (
            first_command.startswith("codex exec ")
            or (
                first_command.startswith("CODEX_HOME=")
                and " codex exec " in first_command
            )
        )
        is_nested_launch = is_direct_codex_exec and len(roles) == 1
        if not is_nested_launch or not isinstance(item_id, str):
            continue
        if event.get("type") == "item.started":
            started[item_id] = (index, roles[0])
            continue
        if event.get("type") != "item.completed" or item_id not in started:
            continue
        start_index, role = started.pop(item_id)
        output = str(item.get("aggregated_output") or "")
        output_lines = [line.strip() for line in output.splitlines() if line.strip()]
        allowed_output = all(
            line == PREINSPECTION_ROLE_LAUNCH_ERROR
            or line == "Reading prompt from stdin..."
            or line == "Reading additional input from stdin..."
            or (
                line.startswith(
                    "WARNING: proceeding, even though we could not create PATH aliases:"
                )
                and line.endswith("Operation not permitted (os error 1)")
            )
            for line in output_lines
        )
        intervening = events[start_index + 1:index]
        role_mutation = any(
            isinstance(candidate.get("item"), dict)
            and (candidate["item"].get("type") == "file_change")
            for candidate in intervening
        )
        nested_session_started = any(
            '"type":"thread.started"' in line.replace(" ", "")
            or '"type": "thread.started"' in line
            for line in output_lines
        )
        checks = {
            "nonzero_exit": (
                isinstance(item.get("exit_code"), int)
                and item.get("exit_code") != 0
            ),
            "initialization_error": (
                PREINSPECTION_ROLE_LAUNCH_ERROR in output_lines
            ),
            "preinspection_output_only": bool(output_lines) and allowed_output,
            "no_model_session_started": not nested_session_started,
            "no_findings_attributed": allowed_output,
            "no_role_mutation": not role_mutation,
        }
        failures.append({
            "role": role,
            "command_event_id": item_id,
            "classification": "preinspection_role_launch_unavailable",
            "checks": checks,
        })

    roles = sorted({failure["role"] for failure in failures})
    checks = {
        "required_roles_attempted": roles == sorted(PREINSPECTION_ROLE_LAUNCHES),
        "all_attempts_preinspection": bool(failures)
        and all(all(failure["checks"].values()) for failure in failures),
        "no_incomplete_role_launch": not started,
    }
    if not all(checks.values()):
        raise RecoveryError(
            "nested role launch failure evidence is not pre-inspection and non-mutating"
        )
    return {
        "classification": "preinspection_role_launch_unavailable",
        "roles": roles,
        "attempts": failures,
        "checks": checks,
        "invalidates_parent_result": False,
    }


def _recoverable_post_integration_role_failure(
    transaction: dict[str, Any],
    session_report: dict[str, Any],
) -> dict[str, Any] | None:
    feature_id = POST_INTEGRATION_PLANNING_RECOVERY_FEATURE
    if transaction.get("error") != (
        f"newly readied feature {feature_id} lacks required execution_policy"
    ):
        return None
    if (
        transaction.get("failure_classification") != "PLANNING_VALIDATION_FAILED"
        or transaction.get("result_classification") != "RECONCILED_READY_WORK"
    ):
        raise RecoveryError(
            "post-integration planning failure classification is contradictory"
        )
    role_failures = _preinspection_role_launch_failure_evidence(session_report)
    return {
        "classification": "preinspection_role_launch_unavailable",
        "exit_code": 0,
        "errors": [],
        "warnings": [],
        "historical_disagreements": [],
        "missing_execution_policy_feature": feature_id,
        "role_launch_failures": role_failures,
    }


def normalize_queue_validation_evidence(
    value: dict[str, Any],
    *,
    source: str = "structured",
    legacy_warning_summary: str | None = None,
) -> dict[str, Any]:
    """Canonicalize equivalent structured and deterministic queue evidence."""
    if not isinstance(value, dict):
        raise RecoveryError("queue validation evidence is not an object")

    def count(name: str) -> int | None:
        item = value.get(name)
        if item is None:
            return None
        if isinstance(item, bool):
            raise RecoveryError(f"queue validation {name} is not a count")
        if isinstance(item, int) and item >= 0:
            return item
        if isinstance(item, list):
            return len(item)
        raise RecoveryError(f"queue validation {name} is malformed")

    def feature_ids(*names: str) -> list[str] | None:
        present = next((name for name in names if name in value), None)
        if present is None:
            return None
        items = value[present]
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            raise RecoveryError(f"queue validation {present} is malformed")
        if len(items) != len(set(items)):
            raise RecoveryError(f"queue validation {present} contains duplicate feature IDs")
        return sorted(items)

    def integer(*names: str) -> int | None:
        present = next((name for name in names if name in value), None)
        if present is None:
            return None
        item = value[present]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise RecoveryError(f"queue validation {present} is malformed")
        return item

    ok = value.get("ok", value.get("valid"))
    if ok is not None and not isinstance(ok, bool):
        raise RecoveryError("queue validation ok is malformed")
    usage = value.get("project_usage")
    if usage is not None and not isinstance(usage, str):
        raise RecoveryError("queue validation project_usage is malformed")
    milestone_name = next(
        (
            name
            for name in (
                "active_milestone",
                "resolved_milestone",
                "configured_milestone",
                "milestone",
            )
            if name in value
        ),
        None,
    )
    milestone = value.get(milestone_name) if milestone_name else None
    if milestone is not None and (not isinstance(milestone, str) or not milestone):
        raise RecoveryError(f"queue validation {milestone_name} is malformed")
    milestone_found = value.get("milestone_found")
    if milestone_found is not None and not isinstance(milestone_found, bool):
        raise RecoveryError("queue validation milestone_found is malformed")
    try:
        warnings = normalize_warning_evidence(
            value,
            source=source,
            legacy_summary=legacy_warning_summary,
        )
    except WarningEvidenceError as exc:
        raise RecoveryError(str(exc)) from exc
    return {
        "ok": ok,
        "milestone_found": milestone_found,
        "active_milestone": milestone,
        "errors": count("errors"),
        "warning_count": warnings["warning_count"],
        "warnings_scope": warnings["warnings_scope"],
        "blocking_warnings": warnings["blocking_warnings"],
        "explicit_warnings": warnings["explicit_warnings"],
        "legacy_warning_summary": warnings["legacy_summary"],
        "ready_features": feature_ids("ready_features", "ready"),
        "active_features": feature_ids("active_features", "active"),
        "feature_count": integer("feature_count"),
        "global_feature_count": integer("global_feature_count"),
        "global_milestone_count": integer("global_milestone_count", "milestone_count"),
        "project_usage": usage,
    }


def normalize_legacy_planning_warning_evidence(
    *,
    project: Project,
    original_transaction_id: str,
    run_id: str,
    session_id: str,
    report_fingerprint: str,
    result_classification: str,
    current_paths: list[str],
    planning_transaction: dict[str, Any],
    result_queue: dict[str, Any],
    deterministic_validation: dict[str, Any],
    recoverable_failure: dict[str, Any],
) -> dict[str, Any]:
    """Replace one identity-bound legacy warning summary before strict parsing."""

    if not isinstance(result_queue.get("warnings"), str):
        return dict(result_queue)
    compatibility = LEGACY_WARNING_SUMMARY_COMPATIBILITY
    checks = {
        "failure_classification": (
            recoverable_failure.get("classification")
            == "historical_warning_summary_shape"
        ),
        "project_id": project.project_id == compatibility["project_id"],
        "transaction_id": original_transaction_id == compatibility["transaction_id"],
        "run_id": run_id == compatibility["run_id"],
        "session_id": session_id == compatibility["session_id"],
        "report_fingerprint": (
            report_fingerprint == compatibility["report_fingerprint"]
        ),
        "result_classification": (
            result_classification == compatibility["result_classification"]
        ),
        "changed_paths": current_paths == compatibility["changed_paths"],
        "recorded_error": planning_transaction.get("error") == compatibility["error"],
        "warning_summary": result_queue.get("warnings") == compatibility["summary"],
    }
    if not all(checks.values()):
        failed = ", ".join(name for name, passed in checks.items() if not passed)
        raise RecoveryError(
            "historical warning-summary compatibility identity disagrees: " + failed
        )
    deterministic = normalize_queue_validation_evidence(
        deterministic_validation,
        source="deterministic",
    )
    canonical = dict(result_queue)
    canonical.pop("warnings", None)
    canonical.update({
        "warning_count": deterministic["warning_count"],
        "warnings_scope": deterministic["warnings_scope"],
        "blocking_warnings": deterministic["blocking_warnings"],
    })
    normalize_queue_validation_evidence(canonical, source="structured")
    return canonical


def compare_queue_validation_evidence(
    structured: dict[str, Any],
    deterministic: dict[str, Any],
    *,
    legacy_warning_summary: str | None = None,
) -> dict[str, Any]:
    """Fail closed only on a semantic disagreement, preserving both forms."""
    normalized_structured = normalize_queue_validation_evidence(
        structured,
        source="structured",
        legacy_warning_summary=legacy_warning_summary,
    )
    normalized_deterministic = normalize_queue_validation_evidence(
        deterministic,
        source="deterministic",
    )
    try:
        warning_comparison = compare_warning_evidence(
            structured,
            deterministic,
            legacy_summary=legacy_warning_summary,
        )
    except WarningEvidenceError as exc:
        raise RecoveryError(str(exc)) from exc
    disagreements: list[str] = []
    for name in (
        "ok",
        "milestone_found",
        "active_milestone",
        "errors",
        "warning_count",
        "blocking_warnings",
        "ready_features",
        "active_features",
        "feature_count",
        "global_feature_count",
        "global_milestone_count",
        "project_usage",
    ):
        left, right = normalized_structured[name], normalized_deterministic[name]
        if name == "warning_count" and normalized_structured["legacy_warning_summary"]:
            continue
        if left is not None and right is not None and left != right:
            disagreements.append(name)
    disagreements.extend(warning_comparison["disagreements"])
    for name in ("feature_count", "global_feature_count", "global_milestone_count"):
        if normalized_structured[name] is None:
            disagreements.append(f"missing_{name}")
        if normalized_deterministic[name] is None:
            disagreements.append(f"missing_deterministic_{name}")
    if normalized_structured["errors"] not in {None, 0} or normalized_deterministic["errors"] not in {None, 0}:
        disagreements.append("nonzero_errors")
    if disagreements:
        raise RecoveryError(
            "queue validation evidence disagrees semantically: " + ", ".join(sorted(set(disagreements)))
        )
    return {
        "raw_structured": structured,
        "raw_deterministic": deterministic,
        "normalized_structured": normalized_structured,
        "normalized_deterministic": normalized_deterministic,
        "warning_evidence": warning_comparison,
    }


def planning_recovery_evidence_fingerprint(
    *,
    original_transaction_id: str,
    original_run_id: str,
    original_session_id: str,
    planning_commit: str,
    queue_fingerprint_value: str,
    selected_feature: str | None,
    planning_transaction_fingerprint: str,
    normalized_terminal_fingerprint: str,
    normalized_queue_evidence: dict[str, Any],
) -> str:
    """Bind one committed planning recovery to its complete immutable evidence."""

    return fingerprint({
        "original_transaction_id": original_transaction_id,
        "original_run_id": original_run_id,
        "original_session_id": original_session_id,
        "planning_commit": planning_commit,
        "queue_fingerprint": queue_fingerprint_value,
        "selected_feature": selected_feature,
        "planning_transaction_fingerprint": planning_transaction_fingerprint,
        "normalized_terminal_fingerprint": normalized_terminal_fingerprint,
        "normalized_queue_evidence": normalized_queue_evidence,
    })


def _completed_integration_parent_evidence(
    *,
    ledger_events: list[dict[str, Any]],
    projection: dict[str, Any],
    planning_start_sequence: int,
    planning_parent: str,
) -> dict[str, Any] | None:
    """Authenticate the latest integration when planning directly follows one."""

    prior = [
        item
        for item in projection.get("transactions") or []
        if item.get("workflow_type") == "milestone_integration"
        and int(item.get("last_sequence") or 0) < planning_start_sequence
    ]
    if not prior:
        return None
    latest = max(prior, key=lambda item: int(item.get("last_sequence") or 0))
    if (
        latest.get("state") != "completed"
        or latest.get("terminal_classification") != "INTEGRATED"
    ):
        raise RecoveryError(
            "committed planning result follows an unfinished milestone integration"
        )
    transaction_id = latest.get("transaction_id")
    events = [
        event
        for event in ledger_events
        if event.get("transaction_id") == transaction_id
    ]
    terminal_types = [event.get("event_type") for event in events[-3:]]
    completed = events[-3].get("payload") if len(events) >= 3 else {}
    projected = events[-1].get("payload") if events else {}
    terminal_snapshot = latest.get("terminal_snapshot") or {}
    checks = {
        "terminal_events": terminal_types
        == ["TransactionCompleted", "LeaseReleased", "ProjectionUpdated"],
        "classification": isinstance(completed, dict)
        and completed.get("classification") == "INTEGRATED",
        "terminal_head": terminal_snapshot.get("head") == planning_parent,
        "terminal_branch": terminal_snapshot.get("branch") is not None,
        "terminal_clean": terminal_snapshot.get("clean") is True,
        "terminal_git_operation": not any(
            bool(value)
            for value in (terminal_snapshot.get("git_operations") or {}).values()
        ),
        "projected_ready": isinstance(projected, dict)
        and projected.get("current_state") == "feature_ready",
        "lineage": bool(events)
        and int(events[-1].get("sequence") or 0) < planning_start_sequence,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RecoveryError(
            "completed integration parent evidence disagrees: " + ", ".join(failed)
        )
    return {
        "transaction_id": transaction_id,
        "feature_id": latest.get("feature_id"),
        "terminal_milestone_head": planning_parent,
        "checks": checks,
    }


def _inspect_committed_planning_finalization_recovery(
    project: Project, inspector: RepositoryInspector, *, ledger_events: list[dict[str, Any]],
    transaction_events: list[dict[str, Any]], checkpoints: list[dict[str, Any]],
    projection: dict[str, Any],
    original_transaction_id: str, planning_transaction: dict[str, Any],
    session_report: dict[str, Any], writer_lease_exists: bool,
) -> dict[str, Any]:
    """Recognize a committed reconciliation blocked only before terminal semantics."""

    start = transaction_events[0]
    session_event = next(
        event
        for event in transaction_events
        if event.get("event_type") == "SessionLaunched"
    )
    finalized = next(
        event
        for event in transaction_events
        if event.get("event_type") == "CommitFinalized"
    )
    blocked = next(
        event
        for event in transaction_events
        if event.get("event_type") == "TransactionBlocked"
    )
    projected = transaction_events[-1]
    start_payload = start.get("payload") or {}
    final_payload = finalized.get("payload") or {}
    blocked_payload = blocked.get("payload") or {}
    snapshot = blocked_payload.get("terminal_snapshot") or {}
    run_id = start_payload.get("run_id")
    session_id = (session_event.get("payload") or {}).get("session_id")
    starting_head = start_payload.get("starting_head")
    starting_branch = start_payload.get("starting_branch")
    existing_commit = final_payload.get("commit")
    if not all(isinstance(item, str) and item for item in (run_id, session_id, starting_head, starting_branch, existing_commit)):
        raise RecoveryError("committed planning transaction identity is incomplete")
    report_evidence = validate_reconciliation_report(session_report, project=project, run_id=run_id,
        expected_session_id=session_id, expected_transaction_id=original_transaction_id)
    envelope = session_report.get("structured_result")
    if not isinstance(envelope, dict):
        raise RecoveryError("committed planning session lacks its typed result")
    paths = sorted(final_payload.get("changed_paths") or [])
    expected_paths = sorted(planning_transaction.get("changed_paths") or [])
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    selection = _selected_feature_evidence(
        project, queue, report_evidence["classification"]
    )
    selected_feature = selection.get("selected_feature")
    result_queue = (
        (report_evidence.get("structured_result") or {}).get(
            "queue_validation"
        )
        or {}
    )
    _resolve_reported_feature_identity(
        result_queue,
        selection,
        report_evidence["classification"],
    )
    current_queue_fingerprint = queue_fingerprint(project)
    expected_subjects = {
        planning_commit_subject(project, None),
        planning_commit_subject(project, selected_feature),
    }
    latest = next((item for item in reversed(projection.get("transactions") or [])
        if item.get("transaction_id") == original_transaction_id), {})
    post_integration_parent = _completed_integration_parent_evidence(
        ledger_events=ledger_events,
        projection=projection,
        planning_start_sequence=int(start.get("sequence") or 0),
        planning_parent=starting_head,
    )
    checks = {
        "latest_transaction_is_original": latest.get("transaction_id") == original_transaction_id,
        "terminal_planning_semantic_block": (
            latest.get("workflow_type") == "queue_reconciliation"
            and latest.get("state") == "terminal_failure"
            and latest.get("terminal_classification")
            == "PLANNING_SEMANTIC_CONFLICT"
            and blocked_payload.get("classification")
            == "PLANNING_SEMANTIC_CONFLICT"
            and blocked_payload.get("terminal_state") == "terminal_failure"
            and blocked_payload.get("next_state") == "validation_failed"
        ),
        "branch": inspector.current_branch == starting_branch == project.milestone_branch,
        "head": inspector.head == existing_commit,
        "parent": inspector.rev_parse(f"{existing_commit}^", check=False) == starting_head == final_payload.get("parent"),
        "subject": (
            inspector.commit_subject(existing_commit)
            == final_payload.get("commit_subject")
            and final_payload.get("commit_subject") in expected_subjects
        ),
        "changed_paths": paths == expected_paths == sorted(envelope.get("changed_paths") or []) == inspector.changed_paths(existing_commit),
        "planning_paths_only": bool(paths) and all(allowed_planning_path(path) for path in paths),
        "repository_clean": inspector.is_clean,
        "writer_lease_absent": writer_lease_exists is False,
        "terminal_snapshot": snapshot.get("branch") == starting_branch and snapshot.get("head") == existing_commit,
        "report_identity": (session_report.get("result_classification") == "RECONCILED_READY_WORK" and envelope.get("starting_commit") == starting_head),
        "planning_transaction_identity": (
            planning_transaction.get("run_id") == run_id
            and planning_transaction.get("session_id") == session_id
            and planning_transaction.get("planning_result_commit") == existing_commit
            and planning_transaction.get("selected_feature") == selected_feature
            and planning_transaction.get("queue_fingerprint")
            == current_queue_fingerprint
        ),
        "commit_fingerprint": (
            final_payload.get("diff_fingerprint")
            == planning_transaction.get("diff_fingerprint")
            == inspector.patch_fingerprint(existing_commit)
        ),
        "no_git_operation": not any(inspector.git_operation_state().values()),
        "terminal_projection": (projected.get("payload") or {}).get("current_state") == "validation_failed",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RecoveryError("committed planning finalization topology disagrees: " + ", ".join(failed))
    checkpoint_evidence = _validate_transparent_planning_checkpoints(
        checkpoints=checkpoints,
        transaction_id=original_transaction_id,
        workflow_type=str(start.get("workflow_type") or ""),
        run_id=str(run_id),
        session_id=str(session_id),
        starting_branch=str(starting_branch),
        starting_head=str(starting_head),
        lease_payload=next(
            event.get("payload") or {}
            for event in transaction_events
            if event.get("event_type") == "LeaseAcquired"
        ),
        mutation_policy=start_payload.get("allowed_mutation_policy") or {},
        source_paths=sorted(envelope.get("changed_paths") or []),
        retained_paths=paths,
        retained_diff_fingerprint=str(final_payload.get("diff_fingerprint") or ""),
        selected_feature=selected_feature,
    )
    newly_policy_bound = _require_new_ready_execution_policies(
        project, inspector, starting_head, queue
    )
    inventory = _inventory_validation(project)
    comparison = compare_queue_validation_evidence(
        (report_evidence.get("structured_result") or {}).get("queue_validation") or {}, inventory
    )
    if selected_feature is None or selection["ready_features"] != [selected_feature]:
        raise RecoveryError("committed planning recovery did not select the sole ready feature")
    normalized_terminal_fingerprint = fingerprint(session_report["structured_result"])
    evidence_fingerprint = planning_recovery_evidence_fingerprint(
        original_transaction_id=original_transaction_id,
        original_run_id=run_id,
        original_session_id=session_id,
        planning_commit=existing_commit,
        queue_fingerprint_value=current_queue_fingerprint,
        selected_feature=selected_feature,
        planning_transaction_fingerprint=fingerprint(planning_transaction),
        normalized_terminal_fingerprint=normalized_terminal_fingerprint,
        normalized_queue_evidence={
            "structured": comparison["normalized_structured"],
            "deterministic": comparison["normalized_deterministic"],
        },
    )
    return {
        "schema_version": 1, "current_state": "validation_failed",
        "workflow_type": "queue reconciliation committed-finalization recovery",
        "transaction_mode": "recovery", "original_transaction_id": original_transaction_id,
        "original_run_id": run_id, "original_session_id": session_id,
        "starting_branch": starting_branch, "starting_commit": starting_head,
        "existing_commit": existing_commit, "existing_planning_changes": {"count": len(paths), "paths": paths},
        "commit_subject": final_payload["commit_subject"],
        "mutation_fingerprint": final_payload["diff_fingerprint"],
        "queue_fingerprint": current_queue_fingerprint,
        "recovery_evidence_fingerprint": evidence_fingerprint,
        "normalized_terminal_fingerprint": normalized_terminal_fingerprint,
        "result_classification": report_evidence["terminal_classification"],
        "selected_feature": selected_feature,
        "ready_features": selection["ready_features"], "checks": checks,
        "checkpoint_evidence": checkpoint_evidence,
        "expected_final_state": "feature_ready",
        "expected_final_current_feature": selected_feature,
        "expected_final_selected_next_feature": selected_feature,
        "selected_feature_starting_commit": existing_commit,
        "post_integration_descendant": post_integration_parent,
        "newly_readied_execution_policies": newly_policy_bound,
        "queue_validation_evidence": comparison, "inventory_validation": inventory,
        "model_sessions_that_would_launch": [], "child_sessions_that_would_launch": [],
        "execution": {"models_planned": 0}, "deterministic_only": True,
        "application_mutation_expected": False,
        "expected_mutation": "ledger terminal recovery evidence and local cycle-cache rebinding only",
        "planning_commits_that_would_be_created": 0,
        "feature_factory_would_launch": False, "milestone_integrator_would_launch": False,
    }


def inspect_planning_finalization_recovery(
    project: Project,
    inspector: RepositoryInspector,
    *,
    ledger_events: list[dict[str, Any]],
    projection: dict[str, Any],
    original_transaction_id: str,
    planning_transaction: dict[str, Any],
    session_report: dict[str, Any],
    writer_lease_exists: bool,
    profile_configuration: dict[str, Any] | None = None,
    controller_reservation_safe: bool = True,
    autopilot_ownership_safe: bool = True,
    live_session_absent: bool = True,
) -> dict[str, Any]:
    """Prove a terminal warning-only planning diff can be finalized without a model."""

    transaction_events = [
        event for event in ledger_events
        if event.get("transaction_id") == original_transaction_id
    ]
    checkpoints = [
        event
        for event in transaction_events
        if event.get("event_type") == "CheckpointRecorded"
    ]
    protocol_events = [
        event
        for event in transaction_events
        if event.get("event_type") != "CheckpointRecorded"
    ]
    event_types = [event.get("event_type") for event in protocol_events]
    committed_event_types = [
        "TransactionStarted", "LeaseAcquired", "SnapshotCaptured", "SessionLaunched",
        "SessionResultAccepted", "ChangesDetected", "ValidationStarted", "ValidationPassed", "CommitFinalized",
        "TransactionBlocked", "LeaseReleased", "ProjectionUpdated",
    ]
    if event_types == committed_event_types:
        return _inspect_committed_planning_finalization_recovery(
            project, inspector, ledger_events=ledger_events,
            transaction_events=protocol_events, checkpoints=checkpoints,
            projection=projection,
            original_transaction_id=original_transaction_id, planning_transaction=planning_transaction,
            session_report=session_report, writer_lease_exists=writer_lease_exists,
        )
    expected_event_types = [
        "TransactionStarted",
        "LeaseAcquired",
        "SnapshotCaptured",
        "SessionLaunched",
        "TransactionBlocked",
        "LeaseReleased",
        "ProjectionUpdated",
    ]
    if event_types != expected_event_types:
        raise RecoveryError("blocked planning transaction does not match the finalization topology")
    start = protocol_events[0]
    lease_event = protocol_events[1]
    session_event = protocol_events[3]
    blocked = protocol_events[4]
    projected = protocol_events[6]
    start_payload = start.get("payload") or {}
    blocked_payload = blocked.get("payload") or {}
    terminal_snapshot = blocked_payload.get("terminal_snapshot") or {}
    run_id = start_payload.get("run_id")
    session_id = (session_event.get("payload") or {}).get("session_id")
    starting_head = start_payload.get("starting_head")
    starting_branch = start_payload.get("starting_branch")
    if not all(isinstance(item, str) and item for item in (run_id, session_id, starting_head, starting_branch)):
        raise RecoveryError("blocked planning transaction identity is incomplete")

    report_evidence = validate_reconciliation_report(
        session_report,
        project=project,
        run_id=run_id,
        expected_session_id=session_id,
        expected_transaction_id=original_transaction_id,
    )
    envelope = session_report.get("structured_result")
    if not isinstance(envelope, dict):
        raise RecoveryError("blocked planning session lacks its typed result")
    normalization = session_report.get("controller_terminal_normalization")
    compatible_terminal_recovered = (
        isinstance(normalization, dict)
        and normalization.get("source_structured_output_validation") == "invalid"
        and normalization.get("source_result_classification")
        == "structured_output_invalid"
        and normalization.get("classification") == "RECONCILED_READY_WORK"
        and normalization.get("next_state") == "feature_ready"
    )
    current_paths = sorted(
        set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())
    )
    recorded_paths = sorted(terminal_snapshot.get("tracked_changed_paths") or [])
    planning_paths = sorted(planning_transaction.get("changed_paths") or [])
    envelope_paths = sorted(envelope.get("changed_paths") or [])
    mutation_fingerprint = inspector.planning_diff_fingerprint()
    post_integration_role_failure = _recoverable_post_integration_role_failure(
        planning_transaction,
        session_report,
    )
    recoverable_failure = (
        post_integration_role_failure
        or (
            {
                "classification": "compatible_terminal_envelope_recovered",
                "exit_code": 0,
                "errors": [],
                "warnings": [],
                "historical_disagreements": [],
            }
            if compatible_terminal_recovered
            else _recoverable_planning_validation_failure(planning_transaction)
        )
    )
    policy = start_payload.get("allowed_mutation_policy") or {}
    commit_subject = policy.get("commit_subject")
    allowed_paths = set(policy.get("allowed_paths") or [])
    allowed_prefixes = tuple(str(item).rstrip("/") for item in policy.get("allowed_prefixes") or [])
    policy_authorized = all(
        path in allowed_paths
        or any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes)
        for path in current_paths
    )
    checkpoint_normalization_binding = any(
        isinstance(event.get("payload"), dict)
        and sorted((event.get("payload") or {}).get("source_paths") or [])
        == envelope_paths
        and sorted((event.get("payload") or {}).get("final_paths") or [])
        == current_paths
        and (
            set(envelope_paths)
            | set(
                (event.get("payload") or {}).get(
                    "normalization_paths"
                )
                or []
            )
        )
        == set(current_paths)
        and (event.get("payload") or {}).get("final_diff_fingerprint")
        == mutation_fingerprint
        for event in checkpoints
    )
    latest_transaction = max(
        projection.get("transactions") or [],
        key=lambda item: int(item.get("last_sequence") or 0),
        default={},
    )
    checks = {
        "projection_validation_failed": projection.get("current_state") == "validation_failed",
        "projection_has_no_active_transaction": projection.get("active_transaction") is None,
        "latest_transaction_is_original": latest_transaction.get("transaction_id") == original_transaction_id,
        "latest_transaction_is_terminal_planning_failure": (
            latest_transaction.get("workflow_type") == "queue_reconciliation"
            and (
                (
                    latest_transaction.get("state") == "terminal_failure"
                    and latest_transaction.get("terminal_classification")
                    == "PLANNING_VALIDATION_FAILED"
                )
                or (
                    compatible_terminal_recovered
                    and latest_transaction.get("state") == "retryable_failure"
                    and latest_transaction.get("terminal_classification")
                    == "RETRYABLE_PLANNING_FAILURE"
                )
            )
        ),
        "terminal_classification": (
            (
                blocked_payload.get("classification")
                == "PLANNING_VALIDATION_FAILED"
                and blocked_payload.get("terminal_state") == "terminal_failure"
                and blocked_payload.get("next_state") == "validation_failed"
            )
            or (
                compatible_terminal_recovered
                and blocked_payload.get("classification")
                == "RETRYABLE_PLANNING_FAILURE"
                and blocked_payload.get("terminal_state") == "retryable_failure"
                and blocked_payload.get("next_state") == "validation_failed"
            )
        ),
        "terminal_projection": (
            (projected.get("payload") or {}).get("current_state") == "validation_failed"
        ),
        "branch": inspector.current_branch == starting_branch == project.milestone_branch,
        "head": inspector.head == starting_head,
        "terminal_branch": terminal_snapshot.get("branch") == starting_branch,
        "terminal_head": terminal_snapshot.get("head") == starting_head,
        "no_git_operation": not any(inspector.git_operation_state().values()),
        "terminal_no_git_operation": not any(
            bool(value)
            for value in (terminal_snapshot.get("git_operations") or {}).values()
        ),
        "changed_paths": (
            current_paths == recorded_paths == planning_paths
            and (
                envelope_paths == current_paths
                or checkpoint_normalization_binding
            )
        ),
        "planning_paths_only": bool(current_paths) and all(allowed_planning_path(path) for path in current_paths),
        "original_policy_authorizes_paths": policy_authorized,
        "original_commit_subject": (
            isinstance(commit_subject, str) and bool(commit_subject)
        ),
        "no_production_or_test_paths": not any(
            path.startswith(("src/", "Sources/", "tests/", "Tests/")) for path in current_paths
        ),
        "no_untracked_paths": not inspector.untracked_file_hashes(),
        "mutation_fingerprint": (
            mutation_fingerprint
            == terminal_snapshot.get("tracked_diff_fingerprint")
            and (
                planning_transaction.get("diff_fingerprint")
                == mutation_fingerprint
                or checkpoint_normalization_binding
            )
        ),
        "planning_transaction_identity": (
            planning_transaction.get("status")
            in {"planning_validation_failed", "terminal_planning_failure"}
            and planning_transaction.get("run_id") == run_id
            and planning_transaction.get("session_id") == session_id
            and planning_transaction.get("planning_start_commit") == starting_head
            and (
                planning_transaction.get("result_classification")
                == report_evidence["terminal_classification"]
                or (
                    compatible_terminal_recovered
                    and planning_transaction.get("result_classification")
                    == "structured_output_invalid"
                    and planning_transaction.get("failure_classification")
                    == "structured_output_invalid"
                )
            )
        ),
        "session_report_identity": (
            session_report.get("structured_output_validation") == "valid"
            and session_report.get("result_classification")
            == report_evidence["terminal_classification"]
            and session_report.get("parsed_structured_result") == envelope
            and session_report.get("terminal_marker_found") is True
            and envelope.get("starting_branch") == starting_branch
            and envelope.get("starting_commit") == starting_head
            and envelope.get("current_commit") == starting_head
        ),
        "post_integration_parent_identity": (
            post_integration_role_failure is None
            or (
                session_report.get("exit_classification")
                == "structured_result_successfully_returned"
                and envelope.get("workflow_type") == "queue_reconciliation"
                and envelope.get("project_id") == project.project_id
                and envelope.get("repository_identity")
                == inspector.identity()["repository_id"]
                and envelope.get("transaction_id") == original_transaction_id
                and envelope.get("run_id") == run_id
                and envelope.get("session_id") == session_id
                and envelope.get("classification") == "RECONCILED_READY_WORK"
                and envelope.get("next_state") == "feature_ready"
            )
        ),
        "compatible_terminal_normalization": (
            not isinstance(normalization, dict) or compatible_terminal_recovered
        ),
        "writer_lease_absent": writer_lease_exists is False,
        "controller_reservation_absent_or_owned": controller_reservation_safe,
        "autopilot_ownership_absent_or_current_process": autopilot_ownership_safe,
        "live_session_absent": live_session_absent,
        "recoverable_validation_failure": bool(recoverable_failure["classification"]),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RecoveryError(
            "planning finalization recovery topology disagrees: " + ", ".join(failed)
        )

    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    selection = _selected_feature_evidence(project, queue, report_evidence["classification"])
    selected = selection.get("selected_feature")
    if commit_subject not in {
        planning_commit_subject(project, None),
        planning_commit_subject(project, selected),
    }:
        raise RecoveryError(
            "planning commit subject conflicts with original mutation authority"
        )
    missing_policy_feature = recoverable_failure.get(
        "missing_execution_policy_feature"
    )
    if missing_policy_feature is not None and (
        selected != missing_policy_feature
        or selection["ready_features"] != [missing_policy_feature]
    ):
        raise RecoveryError(
            "pre-inspection role failure recovery does not select the exact ready feature"
        )
    policy_completion = (
        plan_new_ready_execution_policy_completion(
            project,
            inspector,
            starting_head=starting_head,
            profile_configuration=profile_configuration,
            required_missing_feature=missing_policy_feature,
            authorized_retained_paths=current_paths,
        )
        if missing_policy_feature is not None
        else {
            "required": False,
            "normalization_paths": [],
            "replacements": {},
        }
    )
    if policy_completion.get("required") is True:
        newly_policy_bound = [str(policy_completion["feature_id"])]
    else:
        newly_policy_bound = _require_new_ready_execution_policies(
            project,
            inspector,
            starting_head,
            queue,
            recoverable_missing_policy_feature=missing_policy_feature,
        )
    result_queue = (report_evidence.get("structured_result") or {}).get("queue_validation") or {}
    _resolve_reported_feature_identity(
        result_queue,
        selection,
        report_evidence["classification"],
    )
    if missing_policy_feature is not None and (
        result_queue.get("ok", result_queue.get("valid")) is not True
        or result_queue.get("errors", []) != []
        or result_queue.get("blocking_warnings", []) != []
    ):
        raise RecoveryError(
            "missing execution-policy recovery requires passing structured queue validation"
        )
    recorded_validations = (
        _recorded_post_integration_validation_evidence(
            session_report,
            project,
            queue,
        )
        if missing_policy_feature is not None
        else None
    )
    inventory = (
        recorded_validations["inventory"]
        if recorded_validations is not None
        else _inventory_validation(project)
    )
    canonical_result_queue = normalize_legacy_planning_warning_evidence(
        project=project,
        original_transaction_id=original_transaction_id,
        run_id=run_id,
        session_id=session_id,
        report_fingerprint=fingerprint(session_report),
        result_classification=report_evidence["terminal_classification"],
        current_paths=current_paths,
        planning_transaction=planning_transaction,
        result_queue=result_queue,
        deterministic_validation=inventory,
        recoverable_failure=recoverable_failure,
    )
    queue_comparison = compare_queue_validation_evidence(
        canonical_result_queue,
        inventory,
    )
    historical_warning_compatibility = canonical_result_queue != result_queue
    nonfatal_warnings = list(
        dict.fromkeys(
            [
                *recoverable_failure["warnings"],
                *list(inventory.get("nonfatal_warnings") or []),
            ]
        )
    )
    reported_ready = result_queue.get("ready_features", result_queue.get("ready"))
    if (
        list(reported_ready or []) != selection["ready_features"]
        or (
            selected is not None
            and "dependencies_complete" in result_queue
            and result_queue.get("dependencies_complete") is not True
        )
    ):
        raise RecoveryError("session queue evidence disagrees with deterministic recovery selection")

    checkpoint_evidence = _validate_transparent_planning_checkpoints(
        checkpoints=checkpoints,
        transaction_id=original_transaction_id,
        workflow_type=str(start.get("workflow_type") or ""),
        run_id=str(run_id),
        session_id=str(session_id),
        starting_branch=str(starting_branch),
        starting_head=str(starting_head),
        lease_payload=lease_event.get("payload") or {},
        mutation_policy=policy,
        source_paths=envelope_paths,
        retained_paths=current_paths,
        retained_diff_fingerprint=mutation_fingerprint,
        selected_feature=selected,
    )
    obsolete_specification = recoverable_failure.get(
        "selected_specification_path"
    )
    if obsolete_specification is not None:
        expected_specification = (queue.feature(str(selected)) or {}).get("spec")
        normalized_paths = {
            path
            for event in checkpoints
            for path in (
                (event.get("payload") or {}).get("normalization_paths") or []
            )
            if isinstance(path, str)
        }
        if (
            obsolete_specification != expected_specification
            or obsolete_specification not in normalized_paths
            or obsolete_specification in envelope_paths
        ):
            raise RecoveryError(
                "obsolete selected-spec readiness failure lacks exact normalization identity"
            )

    baseline_queue: FeatureQueue | None = None
    if selected is not None:
        baseline_text = inspector.file_at_commit(starting_head, project.queue_location)
        try:
            baseline_queue = FeatureQueue(json.loads(baseline_text or ""))
        except (json.JSONDecodeError, QueueError, ValueError) as exc:
            raise RecoveryError("starting commit lacks authoritative queue evidence") from exc
        selected_before = baseline_queue.feature(str(selected))
        selected_after = queue.feature(str(selected))
        if (
            not isinstance(selected_before, dict)
            or not isinstance(selected_after, dict)
            or selected_before.get("status") != "proposed"
            or selected_after.get("status") != "ready"
            or any(
                selected_after.get(key)
                for key in ("branch", "accepted_commit", "integrated_commit")
            )
        ):
            raise RecoveryError("selected feature is not proposed-to-ready planning work only")
    dependency_evidence: dict[str, Any] = {}
    for dependency in selection["dependencies"]:
        current = queue.feature(dependency) or {}
        baseline = baseline_queue.feature(dependency) if baseline_queue else {}
        baseline = baseline or {}
        commit = baseline.get("integrated_commit") or baseline.get("commit")
        evidence = {
            "baseline_status": baseline.get("status"),
            "current_status": current.get("status"),
            "integration_status": baseline.get("integration_status"),
            "commit": commit,
            "commit_is_ancestor_of_start": (
                isinstance(commit, str) and inspector.is_ancestor(commit, starting_head)
            ),
        }
        if (
            evidence["baseline_status"] not in {"done", "integrated"}
            or evidence["current_status"] not in {"done", "integrated"}
            or evidence["integration_status"] != "passed"
            or evidence["commit_is_ancestor_of_start"] is not True
        ):
            raise RecoveryError(f"dependency {dependency} lacks authoritative integration evidence")
        dependency_evidence[dependency] = evidence

    return {
        "schema_version": 1,
        "current_state": "validation_failed",
        "workflow_type": "queue_reconciliation recovery/finalization",
        "transaction_mode": "recovery",
        "original_transaction_id": original_transaction_id,
        "original_run_id": run_id,
        "original_session_id": session_id,
        "starting_branch": starting_branch,
        "starting_commit": starting_head,
        "commit_subject": commit_subject,
        "existing_planning_changes": {"count": len(current_paths), "paths": current_paths},
        "mutation_fingerprint": mutation_fingerprint,
        "result_classification": report_evidence["terminal_classification"],
        "selected_feature": selected,
        "ready_features": selection["ready_features"],
        "checkpoint_evidence": checkpoint_evidence,
        "expected_final_state": (
            "feature_ready" if selected is not None else "paused"
        ),
        "expected_final_current_feature": None,
        "expected_final_selected_next_feature": selected,
        "dependencies": selection["dependencies"],
        "dependency_evidence": dependency_evidence,
        "newly_readied_execution_policies": newly_policy_bound,
        "execution_policy_completion": {
            key: value
            for key, value in policy_completion.items()
            if key != "replacements"
        },
        "resolved_execution_policy": policy_completion.get(
            "execution_policy"
        ),
        "resolved_execution_profile": policy_completion.get(
            "resolved_execution_profile"
        ),
        "policy_resolution_source": policy_completion.get("policy_source"),
        "policy_normalization_paths": policy_completion.get(
            "normalization_paths", []
        ),
        "authenticated_original_diff_fingerprint": mutation_fingerprint,
        "predicted_final_diff_fingerprint": policy_completion.get(
            "predicted_final_diff_fingerprint", mutation_fingerprint
        ),
        "completed_features_not_selected": sorted(
            item.get("id")
            for item in queue.features_for_milestone(project.active_milestone or "")
            if item.get("status") in {"done", "integrated"} and isinstance(item.get("id"), str)
        ),
        "inventory_validation": inventory,
        "recorded_validation_evidence": recorded_validations,
        "deterministic_validators_that_would_run": [
            [
                "python3",
                "~/.agents/skills/feature-inventory/scripts/validate_inventory.py",
                "--root",
                ".",
            ],
            ["git", "diff", "--check"],
        ],
        "queue_validation_evidence": queue_comparison,
        "recovered_failure_classification": recoverable_failure["classification"],
        "historical_count_disagreements": recoverable_failure[
            "historical_disagreements"
        ],
        "historical_warning_compatibility": historical_warning_compatibility,
        "terminal_normalization": normalization,
        "historical_child_session_attempts": (
            list(normalization.get("child_session_attempts") or [])
            if isinstance(normalization, dict)
            else []
        ),
        "nested_role_launch_failures": recoverable_failure.get(
            "role_launch_failures"
        ),
        "recoverable_missing_execution_policy_feature": missing_policy_feature,
        "warnings_scope": canonical_result_queue.get("warnings_scope"),
        "nonfatal_warnings": nonfatal_warnings,
        "checks": checks,
        "model_sessions_that_would_launch": [],
        "child_sessions_that_would_launch": [],
        "execution": {"models_planned": 0},
        "deterministic_only": True,
        "application_mutation_expected": True,
        "expected_mutation": (
            "deterministically complete execution policy in authenticated "
            "retained planning metadata, then create one planning commit"
            if policy_completion.get("required") is True
            else "commit existing validated planning metadata"
        ),
        "feature_factory_would_launch": False,
        "milestone_integrator_would_launch": False,
        "planning_content_regeneration_would_run": False,
        "planning_commits_that_would_be_created": 1,
    }


def _semantic_document_agreement(
    project: Project,
    changed_paths: list[str],
    selected_feature: str | None,
) -> dict[str, Any]:
    if selected_feature is None:
        return {"ok": True, "checked_paths": []}
    checked: list[str] = []
    for relative in (
        "docs/CURRENT_STATUS.md",
        "docs/FEATURE_CATALOG.md",
        "docs/ROADMAP.md",
    ):
        path = project.repository / relative
        if relative not in changed_paths or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        if selected_feature.lower() not in text or "ready" not in text:
            raise RecoveryError(
                f"planning semantic agreement failed for {relative}: selected feature readiness is absent"
            )
        checked.append(relative)
    return {"ok": True, "checked_paths": sorted(checked)}


def _require_new_ready_execution_policies(
    project: Project,
    inspector: RepositoryInspector,
    starting_head: str,
    queue: FeatureQueue,
    *,
    recoverable_missing_policy_feature: str | None = None,
) -> list[str]:
    """Require explicit policy only for features newly promoted to ready.

    Existing ready entries remain compatible with the validated workflow
    fallback or a controller-owned reconciled feature policy.
    """
    baseline_text = inspector.file_at_commit(starting_head, project.queue_location)
    try:
        baseline = FeatureQueue(json.loads(baseline_text or ""))
    except (json.JSONDecodeError, QueueError, ValueError) as exc:
        raise RecoveryError("starting commit lacks authoritative queue evidence") from exc
    required: list[str] = []
    for feature in queue.features:
        before = baseline.feature(str(feature.get("id") or ""))
        if feature.get("status") != "ready" or (before or {}).get("status") == "ready":
            continue
        if "execution_policy" not in feature:
            if feature.get("id") == recoverable_missing_policy_feature:
                continue
            raise RecoveryError(
                f"newly readied feature {feature['id']} lacks required execution_policy"
            )
        validate_feature_execution_policy(
            feature["execution_policy"],
            path=f"feature {feature['id']}.execution_policy",
        )
        required.append(str(feature["id"]))
    return sorted(required)


def validate_planning_changes(
    project: Project,
    inspector: RepositoryInspector,
    report: dict[str, Any],
    *,
    run_id: str,
    starting_head: str,
    expected_diff_fingerprint: str | None = None,
    expected_changed_paths: list[str] | None = None,
    expected_session_id: str | None = None,
    expected_transaction_id: str | None = None,
    recoverable_missing_execution_policy_feature: str | None = None,
) -> dict[str, Any]:
    report_evidence = validate_reconciliation_report(
        report,
        project=project,
        run_id=run_id,
        expected_session_id=expected_session_id,
        expected_transaction_id=expected_transaction_id,
    )
    if inspector.current_branch != project.milestone_branch:
        raise RecoveryError("planning branch no longer matches the configured milestone branch")
    if inspector.head != starting_head:
        raise RecoveryError("planning starting HEAD diverged before finalization")
    if any(inspector.git_operation_state().values()):
        raise RecoveryError("an unfinished Git operation blocks planning finalization")
    if inspector.staged_changed_paths():
        raise RecoveryError("pre-staged changes are not part of the planning transaction")
    tracked_paths = inspector.tracked_changed_paths()
    untracked = inspector.untracked_file_hashes()
    changed_paths = sorted(set(tracked_paths) | set(untracked))
    if not changed_paths:
        raise RecoveryError("planning result contains no tracked changes to finalize")
    if expected_changed_paths is not None and changed_paths != sorted(expected_changed_paths):
        raise RecoveryError("planning changed-path set differs from the recovery expectation")
    unauthorized = [path for path in changed_paths if not allowed_planning_path(path)]
    if unauthorized:
        raise RecoveryError(f"unauthorized planning changed paths: {', '.join(unauthorized)}")
    unauthorized_untracked = [path for path in untracked if not allowed_planning_path(path)]
    if unauthorized_untracked:
        raise RecoveryError(
            "untracked files are outside planning policy: " + ", ".join(unauthorized_untracked)
        )
    diff_fingerprint = inspector.planning_diff_fingerprint()
    if expected_diff_fingerprint is not None and diff_fingerprint != expected_diff_fingerprint:
        raise RecoveryError("planning diff fingerprint differs from the recovery expectation")
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    newly_policy_bound = _require_new_ready_execution_policies(
        project,
        inspector,
        starting_head,
        queue,
        recoverable_missing_policy_feature=recoverable_missing_execution_policy_feature,
    )
    classification = report_evidence["classification"]
    selection = _selected_feature_evidence(project, queue, classification)
    result_queue = (
        (report_evidence.get("structured_result") or {}).get(
            "queue_validation"
        )
        or {}
    )
    _resolve_reported_feature_identity(
        result_queue,
        selection,
        classification,
    )
    inventory = _inventory_validation(project)
    queue_comparison = compare_queue_validation_evidence(
        result_queue,
        inventory,
    )
    diff_check = _diff_check(project)
    semantic = _semantic_document_agreement(project, changed_paths, selection["selected_feature"])
    run_log = project.repository / "docs/RUN_LOG.md"
    model_records = {"required": False, "product_architect": False, "feature_inventory_lead": False}
    if all((project.repository / relative).exists() for relative in FULL_INVENTORY_PATHS):
        model_records["required"] = True
        text = run_log.read_text(encoding="utf-8") if run_log.is_file() else ""
        model_records["product_architect"] = "product-architect" in text
        model_records["feature_inventory_lead"] = "feature-inventory-lead" in text
        if not model_records["product_architect"] or not model_records["feature_inventory_lead"]:
            raise RecoveryError("planning model-evidence records are incomplete")
    return {
        "schema_version": 1,
        "phase": "queue_reconciliation",
        "status": "planning_changes_validated",
        "project_id": project.project_id,
        "milestone": project.active_milestone,
        "run_id": run_id,
        "session_id": report_evidence["session_id"],
        "result_classification": classification,
        "terminal_classification": report_evidence["terminal_classification"],
        "planning_start_commit": starting_head,
        "planning_result_commit": None,
        "planning_evidence_commit": None,
        "previous_validated_milestone_head": starting_head,
        "effective_milestone_head": starting_head,
        "changed_paths": changed_paths,
        "changed_path_count": len(changed_paths),
        "diff_fingerprint": diff_fingerprint,
        "queue_fingerprint": queue_fingerprint(project),
        "untracked_file_fingerprint": stable_fingerprint(untracked),
        "report_validation": report_evidence["checks"],
        "inventory_validation": inventory,
        "queue_validation_evidence": queue_comparison,
        "diff_check": diff_check,
        "semantic_agreement": semantic,
        "model_evidence": model_records,
        "newly_readied_execution_policies": newly_policy_bound,
        **selection,
        "planning_commit_would_be_created": True,
        "feature_factory_would_launch": False,
        "milestone_integrator_would_launch": False,
        "application_source_written": False,
        "validated_at": utc_now(),
    }


def validate_planning_noop(
    project: Project,
    inspector: RepositoryInspector,
    report: dict[str, Any],
    *,
    run_id: str,
    starting_head: str,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Validate a successful reconciliation whose deterministic result needs no commit."""

    report_evidence = validate_reconciliation_report(
        report,
        project=project,
        run_id=run_id,
        expected_session_id=expected_session_id,
    )
    if inspector.head != starting_head or not inspector.is_clean:
        raise RecoveryError("no-op planning validation requires the unchanged clean starting HEAD")
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    selection = _selected_feature_evidence(
        project, queue, report_evidence["classification"]
    )
    result_queue = (
        (report_evidence.get("structured_result") or {}).get(
            "queue_validation"
        )
        or {}
    )
    _resolve_reported_feature_identity(
        result_queue,
        selection,
        report_evidence["classification"],
    )
    inventory = _inventory_validation(project)
    queue_comparison = compare_queue_validation_evidence(
        result_queue,
        inventory,
    )
    return {
        "schema_version": 1,
        "phase": "queue_reconciliation",
        "status": "planning_no_changes",
        "project_id": project.project_id,
        "milestone": project.active_milestone,
        "run_id": run_id,
        "session_id": report_evidence["session_id"],
        "result_classification": report_evidence["classification"],
        "terminal_classification": report_evidence["terminal_classification"],
        "planning_start_commit": starting_head,
        "planning_result_commit": starting_head,
        "planning_evidence_commit": None,
        "previous_validated_milestone_head": starting_head,
        "effective_milestone_head": starting_head,
        "changed_paths": [],
        "changed_path_count": 0,
        "diff_fingerprint": inspector.planning_diff_fingerprint(),
        "queue_fingerprint": queue_fingerprint(project),
        "report_validation": report_evidence["checks"],
        "inventory_validation": inventory,
        "queue_validation_evidence": queue_comparison,
        **selection,
        "planning_commit_would_be_created": False,
        "planning_commit_status": "not_required",
        "repository_clean": True,
        "selected_feature_starting_commit": starting_head if selection.get("selected_feature") else None,
        "feature_factory_would_launch": False,
        "milestone_integrator_would_launch": False,
        "application_source_written": False,
        "validated_at": utc_now(),
    }


def planning_commit_subject(project: Project, selected_feature: str | None) -> str:
    milestone = project.active_milestone or "queue"
    if selected_feature:
        return f"factory: reconcile {milestone} queue and ready {selected_feature}"
    return f"factory: reconcile {milestone} queue"


def finalize_planning_commit(
    project: Project,
    inspector: RepositoryInspector,
    validation: dict[str, Any],
) -> dict[str, Any]:
    if inspector.head != validation.get("planning_start_commit"):
        raise RecoveryError("planning HEAD changed after validation")
    observed_paths = sorted(set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes()))
    if observed_paths != validation.get("changed_paths"):
        raise RecoveryError("planning paths changed after validation")
    if inspector.planning_diff_fingerprint() != validation.get("diff_fingerprint"):
        raise RecoveryError("planning diff changed after validation")
    if queue_fingerprint(project) != validation.get("queue_fingerprint"):
        raise RecoveryError("planning queue changed after validation")
    _inventory_validation(project)
    _diff_check(project)
    paths = list(validation["changed_paths"])
    subject = planning_commit_subject(project, validation.get("selected_feature"))
    inspector.stage_planning_paths(paths, commit_subject=subject)
    commit = inspector.commit_planning_paths(paths, commit_subject=subject)
    if not inspector.is_clean:
        raise RecoveryError("planning commit was created but the repository is not clean")
    return {
        **validation,
        "status": "planning_changes_committed",
        "planning_result_commit": commit,
        "planning_evidence_commit": None,
        "effective_milestone_head": commit,
        "planning_commit_status": "committed",
        "planning_commit_subject": subject,
        "planning_commit_would_be_created": False,
        "repository_clean": True,
        "selected_feature_starting_commit": commit if validation.get("selected_feature") else None,
        "committed_at": utc_now(),
    }


def planning_report_path(report_root: Path, run_id: str) -> Path:
    return report_root / run_id / "planning-transaction.json"


def persist_planning_transaction(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def load_planning_transaction(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return _read_report(path)
