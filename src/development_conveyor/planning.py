"""Phase-scoped queue-reconciliation transactions and planning-only commits."""

from __future__ import annotations

import hashlib
import json
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .errors import QueueError, RecoveryError
from .logging import atomic_write_json, utc_now
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import parse_reconciliation_result


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


def stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def queue_fingerprint(project: Project) -> str:
    return hashlib.sha256((project.repository / project.queue_location).read_bytes()).hexdigest()


def allowed_planning_path(path: str) -> bool:
    return path in ALLOWED_PLANNING_FILES or path.startswith(ALLOWED_PLANNING_PREFIXES)


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


def _inventory_validation(project: Project) -> dict[str, Any]:
    root = project.repository
    if not all((root / relative).exists() for relative in FULL_INVENTORY_PATHS):
        queue = FeatureQueue.from_location(root, project.queue_location)
        summary = queue.summary(project.active_milestone or "")
        return _classify_inventory_validation(
            exit_code=0,
            value={
            "ok": summary.get("milestone_found") is True,
            "errors": [] if summary.get("milestone_found") is True else ["active milestone is absent"],
            "warnings": [],
            "feature_count": summary.get("feature_count"),
            "ready": summary.get("ready_features", []),
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
    return {**classified, "validator": str(validator)}


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


def _warning_only_inventory_failure(transaction: dict[str, Any]) -> dict[str, Any]:
    message = transaction.get("error")
    prefix = "deterministic inventory validation failed: "
    if not isinstance(message, str) or not message.startswith(prefix):
        raise RecoveryError("blocked planning transaction is not a deterministic inventory failure")
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
        raise RecoveryError("blocked planning failure is not warning-only deterministic validation")
    return {"exit_code": 0, "errors": [], "warnings": warnings}


def normalize_queue_validation_evidence(value: dict[str, Any]) -> dict[str, Any]:
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
        return sorted(set(items))

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
    return {
        "ok": ok,
        "errors": count("errors"),
        "warnings": count("warnings"),
        "ready_features": feature_ids("ready_features", "ready"),
        "active_features": feature_ids("active_features", "active"),
        "feature_count": integer("feature_count"),
        "milestone_count": integer("milestone_count"),
        "configured_milestone_feature_count": integer("feature_count_in_configured_milestone"),
        "project_usage": usage,
    }


def compare_queue_validation_evidence(
    structured: dict[str, Any], deterministic: dict[str, Any]
) -> dict[str, Any]:
    """Fail closed only on a semantic disagreement, preserving both forms."""
    normalized_structured = normalize_queue_validation_evidence(structured)
    normalized_deterministic = normalize_queue_validation_evidence(deterministic)
    disagreements: list[str] = []
    for name in (
        "ok", "errors", "warnings", "ready_features", "active_features",
        "feature_count", "milestone_count", "configured_milestone_feature_count",
        "project_usage",
    ):
        left, right = normalized_structured[name], normalized_deterministic[name]
        if left is not None and right is not None and left != right:
            disagreements.append(name)
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
    }


def _inspect_committed_planning_finalization_recovery(
    project: Project, inspector: RepositoryInspector, *, transaction_events: list[dict[str, Any]],
    projection: dict[str, Any], original_transaction_id: str, planning_transaction: dict[str, Any],
    session_report: dict[str, Any], writer_lease_exists: bool,
) -> dict[str, Any]:
    """Recognize a committed reconciliation blocked only before terminal semantics."""
    start, session_event, finalized, blocked, projected = (
        transaction_events[0], transaction_events[4], transaction_events[9], transaction_events[10], transaction_events[12]
    )
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
    latest = next((item for item in reversed(projection.get("transactions") or [])
        if item.get("transaction_id") == original_transaction_id), {})
    checks = {
        "latest_transaction_is_original": latest.get("transaction_id") == original_transaction_id,
        "terminal_planning_semantic_block": (latest.get("workflow_type") == "queue_reconciliation" and latest.get("state") == "terminal_failure"),
        "branch": inspector.current_branch == starting_branch == project.milestone_branch,
        "head": inspector.head == existing_commit,
        "parent": inspector.rev_parse(f"{existing_commit}^", check=False) == starting_head == final_payload.get("parent"),
        "subject": inspector.commit_subject(existing_commit) == "factory: reconcile M0 queue" == final_payload.get("commit_subject"),
        "changed_paths": paths == expected_paths == sorted(envelope.get("changed_paths") or []) == inspector.changed_paths(existing_commit),
        "exact_allowed_path_count": len(paths) == 7,
        "planning_paths_only": bool(paths) and all(allowed_planning_path(path) for path in paths),
        "repository_clean": inspector.is_clean,
        "writer_lease_absent": writer_lease_exists is False,
        "terminal_snapshot": snapshot.get("branch") == starting_branch and snapshot.get("head") == existing_commit,
        "report_identity": (session_report.get("result_classification") == "RECONCILED_READY_WORK" and envelope.get("starting_commit") == starting_head),
        "planning_transaction_identity": (planning_transaction.get("run_id") == run_id and planning_transaction.get("session_id") == session_id and planning_transaction.get("planning_result_commit") == existing_commit),
        "terminal_projection": (projected.get("payload") or {}).get("current_state") == "validation_failed",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RecoveryError("committed planning finalization topology disagrees: " + ", ".join(failed))
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    selection = _selected_feature_evidence(project, queue, report_evidence["classification"])
    inventory = _inventory_validation(project)
    comparison = compare_queue_validation_evidence(
        (report_evidence.get("structured_result") or {}).get("queue_validation") or {}, inventory
    )
    if selection["selected_feature"] != "F004":
        raise RecoveryError("committed planning recovery did not select the sole ready feature")
    return {
        "schema_version": 1, "current_state": "validation_failed",
        "workflow_type": "queue reconciliation committed-finalization recovery",
        "transaction_mode": "recovery", "original_transaction_id": original_transaction_id,
        "original_run_id": run_id, "original_session_id": session_id,
        "starting_branch": starting_branch, "starting_commit": starting_head,
        "existing_commit": existing_commit, "existing_planning_changes": {"count": len(paths), "paths": paths},
        "result_classification": report_evidence["terminal_classification"], "selected_feature": "F004",
        "ready_features": selection["ready_features"], "checks": checks,
        "queue_validation_evidence": comparison, "inventory_validation": inventory,
        "model_sessions_that_would_launch": [], "child_sessions_that_would_launch": [],
        "execution": {"models_planned": 0}, "deterministic_only": True,
        "application_mutation_expected": False,
        "expected_mutation": "ledger terminal recovery evidence and local cycle-cache rebinding only",
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
) -> dict[str, Any]:
    """Prove a terminal warning-only planning diff can be finalized without a model."""

    transaction_events = [
        event for event in ledger_events
        if event.get("transaction_id") == original_transaction_id
    ]
    event_types = [event.get("event_type") for event in transaction_events]
    committed_event_types = [
        "TransactionStarted", "LeaseAcquired", "SnapshotCaptured", "CheckpointRecorded", "SessionLaunched",
        "SessionResultAccepted", "ChangesDetected", "ValidationStarted", "ValidationPassed", "CommitFinalized",
        "TransactionBlocked", "LeaseReleased", "ProjectionUpdated",
    ]
    if event_types == committed_event_types:
        return _inspect_committed_planning_finalization_recovery(
            project, inspector, transaction_events=transaction_events, projection=projection,
            original_transaction_id=original_transaction_id, planning_transaction=planning_transaction,
            session_report=session_report, writer_lease_exists=writer_lease_exists,
        )
    expected_event_types = [
        "TransactionStarted",
        "LeaseAcquired",
        "SnapshotCaptured",
        "CheckpointRecorded",
        "SessionLaunched",
        "TransactionBlocked",
        "LeaseReleased",
        "ProjectionUpdated",
    ]
    if event_types != expected_event_types:
        raise RecoveryError("blocked planning transaction does not match the finalization topology")
    start = transaction_events[0]
    session_event = transaction_events[4]
    blocked = transaction_events[5]
    projected = transaction_events[7]
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
    current_paths = sorted(
        set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())
    )
    recorded_paths = sorted(terminal_snapshot.get("tracked_changed_paths") or [])
    planning_paths = sorted(planning_transaction.get("changed_paths") or [])
    envelope_paths = sorted(envelope.get("changed_paths") or [])
    mutation_fingerprint = inspector.planning_diff_fingerprint()
    warning_failure = _warning_only_inventory_failure(planning_transaction)
    policy = start_payload.get("allowed_mutation_policy") or {}
    allowed_paths = set(policy.get("allowed_paths") or [])
    allowed_prefixes = tuple(str(item).rstrip("/") for item in policy.get("allowed_prefixes") or [])
    policy_authorized = all(
        path in allowed_paths
        or any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes)
        for path in current_paths
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
            and latest_transaction.get("state") == "terminal_failure"
            and latest_transaction.get("terminal_classification") == "PLANNING_VALIDATION_FAILED"
        ),
        "terminal_classification": (
            blocked_payload.get("classification") == "PLANNING_VALIDATION_FAILED"
            and blocked_payload.get("terminal_state") == "terminal_failure"
            and blocked_payload.get("next_state") == "validation_failed"
        ),
        "terminal_projection": (
            (projected.get("payload") or {}).get("current_state") == "validation_failed"
        ),
        "branch": inspector.current_branch == starting_branch == project.milestone_branch,
        "head": inspector.head == starting_head,
        "terminal_branch": terminal_snapshot.get("branch") == starting_branch,
        "terminal_head": terminal_snapshot.get("head") == starting_head,
        "changed_paths": current_paths == recorded_paths == planning_paths == envelope_paths,
        "exact_allowed_path_count": len(current_paths) == 7,
        "planning_paths_only": bool(current_paths) and all(allowed_planning_path(path) for path in current_paths),
        "original_policy_authorizes_paths": policy_authorized,
        "no_production_or_test_paths": not any(
            path.startswith(("src/", "Sources/", "tests/", "Tests/")) for path in current_paths
        ),
        "no_untracked_paths": not inspector.untracked_file_hashes(),
        "mutation_fingerprint": (
            mutation_fingerprint
            == terminal_snapshot.get("tracked_diff_fingerprint")
            == planning_transaction.get("diff_fingerprint")
        ),
        "planning_transaction_identity": (
            planning_transaction.get("status") == "planning_validation_failed"
            and planning_transaction.get("run_id") == run_id
            and planning_transaction.get("session_id") == session_id
            and planning_transaction.get("planning_start_commit") == starting_head
            and planning_transaction.get("result_classification") == "RECONCILED_READY_WORK"
        ),
        "session_report_identity": (
            session_report.get("structured_output_validation") == "valid"
            and session_report.get("result_classification") == "RECONCILED_READY_WORK"
            and session_report.get("parsed_structured_result") == envelope
            and session_report.get("terminal_marker_found") is True
            and envelope.get("starting_branch") == starting_branch
            and envelope.get("starting_commit") == starting_head
            and envelope.get("current_commit") == starting_head
        ),
        "writer_lease_absent": writer_lease_exists is False,
        "warning_only_failure": bool(warning_failure["warnings"]),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RecoveryError(
            "planning finalization recovery topology disagrees: " + ", ".join(failed)
        )

    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    selection = _selected_feature_evidence(project, queue, report_evidence["classification"])
    selected = selection.get("selected_feature")
    result_queue = (report_evidence.get("structured_result") or {}).get("queue_validation") or {}
    if (
        result_queue.get("selected_feature") != selected
        or list(result_queue.get("ready_features") or []) != selection["ready_features"]
        or result_queue.get("dependencies_complete") is not True
    ):
        raise RecoveryError("session queue evidence disagrees with deterministic recovery selection")

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
        or any(selected_after.get(key) for key in ("branch", "accepted_commit", "integrated_commit"))
    ):
        raise RecoveryError("selected feature is not proposed-to-ready planning work only")
    dependency_evidence: dict[str, Any] = {}
    for dependency in selection["dependencies"]:
        current = queue.feature(dependency) or {}
        baseline = baseline_queue.feature(dependency) or {}
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
        "existing_planning_changes": {"count": len(current_paths), "paths": current_paths},
        "mutation_fingerprint": mutation_fingerprint,
        "result_classification": report_evidence["terminal_classification"],
        "selected_feature": selected,
        "ready_features": selection["ready_features"],
        "dependencies": selection["dependencies"],
        "dependency_evidence": dependency_evidence,
        "completed_features_not_selected": sorted(
            item.get("id")
            for item in queue.features_for_milestone(project.active_milestone or "")
            if item.get("status") in {"done", "integrated"} and isinstance(item.get("id"), str)
        ),
        "nonfatal_warnings": warning_failure["warnings"],
        "checks": checks,
        "model_sessions_that_would_launch": [],
        "child_sessions_that_would_launch": [],
        "execution": {"models_planned": 0},
        "deterministic_only": True,
        "application_mutation_expected": True,
        "expected_mutation": "commit existing validated planning metadata",
        "feature_factory_would_launch": False,
        "milestone_integrator_would_launch": False,
        "planning_content_regeneration_would_run": False,
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
        f"docs/features/{selected_feature}.md",
    ):
        candidates = [relative]
        if relative.startswith("docs/features/"):
            candidates = [path for path in changed_paths if path.startswith("docs/features/") and selected_feature in path]
        for candidate in candidates:
            path = project.repository / candidate
            if candidate not in changed_paths or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").lower()
            if selected_feature.lower() not in text or "ready" not in text:
                raise RecoveryError(
                    f"planning semantic agreement failed for {candidate}: selected feature readiness is absent"
                )
            checked.append(candidate)
    return {"ok": True, "checked_paths": sorted(checked)}


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
    classification = report_evidence["classification"]
    selection = _selected_feature_evidence(project, queue, classification)
    inventory = _inventory_validation(project)
    queue_comparison = compare_queue_validation_evidence(
        (report_evidence.get("structured_result") or {}).get("queue_validation") or {},
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
    inventory = _inventory_validation(project)
    queue_comparison = compare_queue_validation_evidence(
        (report_evidence.get("structured_result") or {}).get("queue_validation") or {},
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
