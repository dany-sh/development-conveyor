"""Phase-scoped queue-reconciliation transactions and planning-only commits."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import RecoveryError
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


def _inventory_validation(project: Project) -> dict[str, Any]:
    root = project.repository
    if not all((root / relative).exists() for relative in FULL_INVENTORY_PATHS):
        queue = FeatureQueue.from_location(root, project.queue_location)
        summary = queue.summary(project.active_milestone or "")
        return {
            "ok": summary.get("milestone_found") is True,
            "errors": [] if summary.get("milestone_found") is True else ["active milestone is absent"],
            "warnings": [],
            "feature_count": summary.get("feature_count"),
            "ready": summary.get("ready_features", []),
            "validator": "controller_queue_validator",
        }
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
    if (
        result.returncode != 0
        or not isinstance(value, dict)
        or value.get("ok") is not True
        or value.get("errors")
        or value.get("warnings")
    ):
        raise RecoveryError(
            "deterministic inventory validation failed: "
            + json.dumps({
                "exit_code": result.returncode,
                "errors": (value or {}).get("errors") if isinstance(value, dict) else None,
                "warnings": (value or {}).get("warnings") if isinstance(value, dict) else None,
            }, sort_keys=True)
        )
    return {**value, "validator": str(validator)}


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
) -> dict[str, Any]:
    report_evidence = validate_reconciliation_report(
        report,
        project=project,
        run_id=run_id,
        expected_session_id=expected_session_id,
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
