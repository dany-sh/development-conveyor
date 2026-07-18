"""Deterministic project planning and portfolio reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import ConveyorError, QueueError
from .locks import DurableLock, inspect_repository_writer_lock
from .queue import FeatureQueue, resolve_feature_commit, resolve_queue_path
from .registry import Project
from .repository import RepositoryInspector
from .sessions import classify_codex_failure


DETERMINISTIC_CONFIGURATION_FAILURES = {
    "cli_upgrade_required",
    "configuration_incompatible",
    "unsupported_model",
    "unsupported_reasoning_effort",
    "model_policy_invalid",
}


def _failed_cycle_reports(controller_root: Path, configuration: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    directory = Path(configuration["report_directory"])
    if not directory.is_absolute():
        directory = controller_root / directory
    run_directory = directory.resolve() / run_id
    reports: list[dict[str, Any]] = []
    if not run_directory.is_dir():
        return reports
    for path in sorted(run_directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        failure = value.get("failure_classification")
        parsed = classify_codex_failure(
            str(value.get("redacted_stdout") or ""), str(value.get("redacted_stderr") or "")
        )
        reports.append({
            "path": str(path),
            "created_at": value.get("created_at"),
            "classification": failure or parsed.get("classification"),
            "primary_terminal_error": value.get("primary_terminal_error") or parsed.get("primary_terminal_error"),
            "secondary_diagnostics": value.get("secondary_diagnostics") or list(parsed.get("secondary_diagnostics") or ()),
            "session_id": value.get("session_id"),
            "argv": value.get("argv"),
        })
    return sorted(reports, key=lambda item: (str(item.get("created_at") or ""), item["path"]))


def build_project_plan(
    project: Project, configuration: dict[str, Any], controller_root: Path | None = None
) -> dict[str, Any]:
    inspector = RepositoryInspector(project.repository)
    repository = inspector.inspect(
        baseline=project.validated_baseline_commit,
        milestone_branch=project.milestone_branch,
    )
    lock = inspect_repository_writer_lock(
        inspector.writer_lock_path(configuration["lock_policy"]["writer_lock_relative_path"]),
        project.repository,
    )
    queue_path: Path | None = None
    queue_error: str | None = None
    queue_summary: dict[str, Any]
    selection = None
    try:
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        queue_summary = queue.summary(project.active_milestone or "")
        queue_summary.update({
            "configured_queue_location": project.queue_location,
            "resolved_queue_path": str(queue_path),
            "source_format": "json-compatible-yaml",
            "valid": True,
        })
        resolved_commits = []
        for feature in queue.features_for_milestone(project.active_milestone or ""):
            if feature.get("status") not in {"done", "integrated"}:
                continue
            resolution = resolve_feature_commit(
                feature=feature,
                queue=queue,
                repository=inspector,
                milestone_branch=project.milestone_branch,
                baseline=project.validated_baseline_commit,
                registered_commit=project.last_accepted_commit,
                registered_feature=project.last_accepted_feature,
            )
            if resolution:
                resolved_commits.append({
                    "feature_id": resolution.feature_id,
                    "commit": resolution.commit,
                    "source": resolution.source,
                })
        queue_summary["resolved_commits"] = resolved_commits
        selection = queue.select_next(project.active_milestone or "") if queue_summary["milestone_found"] else None
    except (QueueError, OSError) as exc:
        queue_error = str(exc)
        queue_summary = {
            "valid": False,
            "error": queue_error,
            "configured_queue_location": project.queue_location,
            "resolved_queue_path": str(queue_path) if queue_path else None,
            "reconciliation_classification": "invalid_queue",
        }

    active_cycle: dict[str, Any] | None = None
    cycle_document: dict[str, Any] | None = None
    cycle_fingerprint: str | None = None
    cycle_path = inspector.cycle_state_path()
    if cycle_path.exists():
        try:
            cycle_bytes = cycle_path.read_bytes()
            value = json.loads(cycle_bytes)
            if isinstance(value, dict):
                cycle_document = value
                cycle_fingerprint = hashlib.sha256(cycle_bytes).hexdigest()
                active_cycle = {
                    "run_id": value.get("conveyor_run_id"),
                    "phase": value.get("current_phase"),
                    "feature": value.get("current_feature"),
                    "checkpoint": value.get("last_successful_checkpoint"),
                }
        except (OSError, json.JSONDecodeError):
            active_cycle = {"phase": "invalid", "requires_reconciliation": True}

    launch_status = None
    if controller_root is not None:
        directory = Path(configuration["lock_policy"]["controller_launch_lock_directory"])
        if not directory.is_absolute():
            directory = controller_root / directory
        launch_status = DurableLock(directory / f"{repository['identity']['path_fingerprint']}.json").status(
            active_cycle.get("run_id") if active_cycle else None
        )

    git_operation_active = any(repository["git_operations"].values())
    writer_record = lock.record or {}
    writer_run = writer_record.get("agent_run") or writer_record.get("run_id")
    writer_owned_by_active_cycle = bool(active_cycle and writer_run == active_cycle.get("run_id"))
    stale_cycle_evidence: dict[str, Any] | None = None
    expected_branch = cycle_document.get("feature_branch") if cycle_document else None
    actual_branch = repository.get("branch")
    expected_branch_exists = bool(
        isinstance(expected_branch, str) and inspector.ref_exists(expected_branch)
    )
    expected_branch_worktree = (
        inspector.branch_worktree(expected_branch)
        if expected_branch_exists and isinstance(expected_branch, str)
        else None
    )
    production_resume_phase = bool(
        active_cycle
        and active_cycle.get("phase")
        in {"branch_preparing", "feature_in_progress", "feature_review", "feature_repair"}
    )
    branch_recovery_required = bool(
        production_resume_phase
        and isinstance(expected_branch, str)
        and (
            not expected_branch_exists
            or actual_branch != expected_branch
            or expected_branch_worktree != project.repository.resolve()
        )
    )
    writer_lock_required_before_resume = bool(
        production_resume_phase
        and cycle_document
        and cycle_document.get("accepted_feature_commit") is None
    )
    if active_cycle and cycle_document:
        if active_cycle.get("phase") == "completed":
            stale_cycle_evidence = {
                **active_cycle,
                "classification": "completed_cycle_evidence",
                "reason": "The repository-local cycle is complete and is not active work.",
            }
            active_cycle = None
        if active_cycle is not None:
            expected_head = cycle_document.get("feature_starting_commit") or cycle_document.get(
                "milestone_pre_integration_commit"
            )
            recorded_identity = cycle_document.get("repository_identity")
            last_git = cycle_document.get("last_verified_git_state")
            feature_id = str(active_cycle.get("feature") or "")
            feature_branch_exists = any(
                feature_id.lower() in branch.lower() for branch in repository.get("local_branches", [])
            )
            worktrees = repository.get("worktrees", [])
            exact_single_worktree = (
                len(worktrees) == 1
                and Path(str(worktrees[0].get("worktree"))).resolve() == project.repository.resolve()
            )
            orphaned_prelaunch = (
                active_cycle.get("phase") == "branch_preparing"
                and active_cycle.get("checkpoint") == "repository_session_owns_branch_preparation"
                and cycle_document.get("session_id") is None
                and not lock.exists
                and not (launch_status and launch_status.exists)
                and repository["clean"]
                and not git_operation_active
                and repository["branch"] == project.milestone_branch
                and exact_single_worktree
                and not feature_branch_exists
                and isinstance(expected_head, str)
                and repository["head"] == expected_head
                and cycle_document.get("milestone_pre_integration_commit") == repository["head"]
                and cycle_document.get("project_id") == project.project_id
                and cycle_document.get("active_milestone") == project.active_milestone
                and cycle_document.get("milestone_branch") == project.milestone_branch
                and cycle_document.get("repository_path_fingerprint")
                == repository["identity"]["path_fingerprint"]
                and isinstance(recorded_identity, dict)
                and recorded_identity.get("repository_id") == repository["identity"]["repository_id"]
                and cycle_document.get("feature_worktree") is None
                and cycle_document.get("writer_lock_identity") is None
                and cycle_document.get("validation_attempts") == []
                and cycle_document.get("review_attempts") == []
                and cycle_document.get("integration_attempts") == []
                and cycle_document.get("stop_reason") is None
                and cycle_document.get("human_decision_required") is None
                and isinstance(last_git, dict)
                and last_git.get("branch") == repository["branch"]
                and last_git.get("head") == repository["head"]
                and last_git.get("clean") is True
                and not any((last_git.get("git_operations") or {}).values())
                and selection is not None
                and active_cycle.get("feature") == selection.feature_id
            )
            if orphaned_prelaunch:
                stale_cycle_evidence = {
                    **active_cycle,
                    "classification": "orphaned_prelaunch_cycle",
                    "reason": (
                        "The cycle stopped after its pre-launch checkpoint, before a session, lock, "
                        "branch change, or repository mutation."
                    ),
                    "cycle_fingerprint": cycle_fingerprint,
                }
                active_cycle = None
            elif (
                controller_root is not None
                and active_cycle.get("phase") in {"failed", "human_decision_required"}
                and active_cycle.get("checkpoint") in {"session_failed", "session_terminal_failure"}
                and cycle_document.get("feature_worktree") is None
                and cycle_document.get("writer_lock_identity") is None
                and cycle_document.get("accepted_feature_commit") is None
                and cycle_document.get("milestone_post_integration_commit") is None
                and not lock.exists
                and not (launch_status and launch_status.exists)
                and repository["clean"]
                and not git_operation_active
                and repository["branch"] == project.milestone_branch
                and repository["head"] == cycle_document.get("milestone_pre_integration_commit")
                and exact_single_worktree
                and not feature_branch_exists
                and cycle_document.get("project_id") == project.project_id
                and cycle_document.get("active_milestone") == project.active_milestone
                and cycle_document.get("milestone_branch") == project.milestone_branch
                and cycle_document.get("repository_path_fingerprint")
                == repository["identity"]["path_fingerprint"]
                and isinstance(recorded_identity, dict)
                and recorded_identity.get("repository_id") == repository["identity"]["repository_id"]
                and selection is not None
                and active_cycle.get("feature") == selection.feature_id
            ):
                reports = _failed_cycle_reports(
                    controller_root, configuration, str(active_cycle.get("run_id") or "")
                )
                terminal = reports[-1] if reports else None
                if terminal is not None and terminal.get("classification") in DETERMINISTIC_CONFIGURATION_FAILURES:
                    stale_cycle_evidence = {
                        **active_cycle,
                        "classification": "deterministic_failed_cycle",
                        "failure_classification": terminal["classification"],
                        "reason": "The repository session failed deterministically before any repository work and must not be resumed.",
                        "cycle_fingerprint": cycle_fingerprint,
                        "attempts_consumed": len(cycle_document.get("validation_attempts") or []),
                        "environment_remediation_verified": False,
                        "feature_starting_commit": repository["head"],
                        "recorded_feature_starting_commit": cycle_document.get("feature_starting_commit"),
                        "milestone_pre_integration_commit": cycle_document.get("milestone_pre_integration_commit"),
                        "session_id": terminal.get("session_id") or cycle_document.get("session_id"),
                        "old_session_will_resume": False,
                        "new_session_required": True,
                        "historical_reports": [item["path"] for item in reports],
                        "primary_terminal_error": terminal.get("primary_terminal_error"),
                        "secondary_diagnostics": terminal.get("secondary_diagnostics"),
                    }
                    active_cycle = None
    if not project.enabled:
        action = "disabled"
        stop = "Project is disabled."
    elif lock.exists and lock.process_alive is True:
        action = "writer_locked"
        stop = "A live repository writer lock exists; read-only inspection remains available."
    elif lock.exists and writer_owned_by_active_cycle and not lock.ambiguous:
        action = "resume"
        stop = "Resume the matching interrupted cycle without creating duplicate work."
    elif lock.exists:
        action = "human_decision_required"
        stop = "Repository writer-lock ownership cannot be reconciled to an active Conveyor cycle."
    elif launch_status and launch_status.exists and launch_status.process_alive is True:
        action = "writer_locked"
        stop = "A live controller launch reservation exists; duplicate launch is refused."
    elif launch_status and launch_status.exists and launch_status.ambiguous:
        action = "human_decision_required"
        stop = "Controller launch-lock ownership cannot be proven safely."
    elif launch_status and launch_status.exists and not launch_status.owned_by_run:
        action = "human_decision_required"
        stop = "Controller launch-lock ownership does not match the active cycle."
    elif branch_recovery_required:
        action = "branch_recovery_required"
        stop = "Recover the persisted feature branch without changing dirty file bytes before any session resume."
    elif active_cycle and active_cycle.get("phase") not in {"completed", None}:
        action = "resume"
        stop = "Resume until the current cycle reaches its configured stop condition."
    elif git_operation_active:
        action = "human_decision_required"
        stop = "An unfinished Git operation requires reconciliation."
    elif not repository["clean"] or project.current_state == "repository_dirty":
        action = "repository_dirty"
        stop = "Preserve existing work and reconcile the dirty repository before production scheduling."
    elif queue_error or not queue_summary.get("milestone_found", False):
        action = "queue_reconciliation"
        stop = "Continue only when deterministic queue validation finds justified ready work or a genuine gate."
    elif project.current_state == "paused" and queue_summary.get("reconciliation_classification") == "reconciled_no_ready_work":
        action = "planning_refinement"
        stop = "The last reconciliation validated the queue and found no dependency-ready work."
    elif queue_summary.get("active_features"):
        action = "resume_existing_factory_work"
        stop = "Resume the existing repository feature state; do not select duplicate work."
    elif selection is not None:
        action = "feature_cycle"
        stop = "Stop according to the requested run mode after integration validation."
    elif queue_summary.get("milestone_complete"):
        action = "milestone_gate"
        stop = "Stop for human milestone merge approval after a passing gate."
    elif queue_summary.get("reconciliation_classification") == "human_decision_required":
        action = "human_decision_required"
        stop = "Queue evidence identifies an unresolved human decision."
    elif queue_summary.get("reconciliation_classification") == "legitimately_blocked":
        action = "legitimately_blocked"
        stop = "Queue evidence contains only legitimate blockers and no dependency-ready work."
    elif queue_summary.get("status_counts", {}).get("proposed", 0):
        action = "queue_reconciliation"
        stop = "Use planning-only reconciliation to refine proposed work without beginning implementation."
    else:
        action = "planning_refinement"
        stop = "The queue is valid but has no dependency-ready feature; remain at a safe planning checkpoint."

    session_actions = {
        "queue_reconciliation": ["product-architect (planning-only)", "$feature-inventory"],
        "planning_refinement": ["product-architect (planning-only)", "$feature-inventory (read-only validation)"],
        "feature_cycle": ["$feature-factory", "$milestone-integrator"],
        "resume_existing_factory_work": ["resume repository-scoped Codex session"],
        "milestone_gate": ["$milestone-gate", "release-auditor (read-only)"],
        "resume": ["resume persisted Conveyor cycle"],
    }
    identity = repository["identity"]
    historical_failure = bool(
        stale_cycle_evidence
        and stale_cycle_evidence.get("classification") == "deterministic_failed_cycle"
    )
    if historical_failure and action == "feature_cycle":
        session_actions["feature_cycle"] = ["launch a new repository-scoped session; do not resume the failed session"]
    completion_classification = (
        cycle_document.get("session_completion_classification") if cycle_document else None
    )
    completion_flags = list(cycle_document.get("session_completion_flags") or []) if cycle_document else []
    if branch_recovery_required:
        completion_classification = "branch_invariant_violated"
        completion_flags = ["branch_invariant_violated"]
        if not repository.get("clean"):
            completion_flags.append("uncommitted_feature_work")
    resume_allowed_after_lock = bool(
        production_resume_phase
        and not branch_recovery_required
        and expected_branch_exists
        and actual_branch == expected_branch
        and expected_branch_worktree == project.repository.resolve()
        and not any(repository.get("git_operations", {}).values())
        and repository.get("milestone_branch_head")
        == (cycle_document or {}).get("milestone_pre_integration_commit")
    )
    resume_allowed = bool(
        action == "resume"
        and not writer_lock_required_before_resume
        or (
            action == "resume"
            and writer_lock_required_before_resume
            and lock.exists
            and writer_owned_by_active_cycle
            and not lock.ambiguous
        )
    )
    return {
        "project_id": project.project_id,
        "repository_path": str(project.repository),
        "repository_identity": identity["repository_id"],
        "repository_path_fingerprint": identity["path_fingerprint"],
        "current_state": project.current_state,
        "existing_active_cycle": active_cycle,
        "stale_cycle_evidence": stale_cycle_evidence,
        "lock_status": {
            "repository_writer": {
                "exists": lock.exists,
                "owned_by_active_cycle": writer_owned_by_active_cycle,
                "process_alive": lock.process_alive,
                "ambiguous": lock.ambiguous,
            },
            "controller_launch": {
                "exists": bool(launch_status and launch_status.exists),
                "owned_by_active_cycle": bool(launch_status and launch_status.owned_by_run),
                "process_alive": launch_status.process_alive if launch_status else None,
                "ambiguous": launch_status.ambiguous if launch_status else False,
            },
        },
        "active_milestone": project.active_milestone,
        "milestone_branch": project.milestone_branch,
        "validated_baseline": project.validated_baseline_commit,
        "repository_state": repository,
        "queue_status": queue_summary,
        "selected_feature": selection.feature_id if selection else None,
        "selection_reason": selection.reason if selection else None,
        "proposed_next_action": action,
        "sessions_that_would_launch": session_actions.get(action, []),
        "prohibited_actions": list(configuration["prohibited_operations"]),
        "expected_stop_condition": stop,
        "dry_run_writes_application_repository": False,
        "feature_starting_commit": repository.get("milestone_branch_head") if selection else None,
        "old_session_will_resume": False if historical_failure else action == "resume",
        "new_session_would_launch": bool(historical_failure and action == "feature_cycle"),
        "actual_branch": actual_branch,
        "expected_branch": expected_branch,
        "branch_recovery_required": branch_recovery_required,
        "writer_lock_required_before_resume": writer_lock_required_before_resume,
        "writer_lock_currently_held": lock.exists,
        "resume_allowed": resume_allowed,
        "resume_allowed_after_lock": resume_allowed_after_lock,
        "session_completion_classification": completion_classification,
        "session_completion_flags": completion_flags,
    }


def portfolio_status(
    projects: list[Project], configuration: dict[str, Any], controller_root: Path | None = None
) -> dict[str, Any]:
    values = []
    for project in projects:
        try:
            values.append(build_project_plan(project, configuration, controller_root))
        except ConveyorError as exc:
            values.append({
                "project_id": project.project_id,
                "repository": str(project.repository),
                "proposed_next_action": "conveyor_error",
                "error": str(exc),
            })
    counts: dict[str, int] = {}
    for item in values:
        action = str(item.get("proposed_next_action"))
        counts[action] = counts.get(action, 0) + 1
    return {"schema_version": 1, "project_count": len(values), "action_counts": counts, "projects": values}


def render_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
