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
                and cycle_document.get("feature_branch") is None
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
