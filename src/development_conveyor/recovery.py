"""Evidence-based interrupted-cycle reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import RecoveryError
from .registry import Project
from .repository import RepositoryInspector


@dataclass(frozen=True)
class RecoveryAssessment:
    outcome: str
    resume_phase: str | None
    evidence: dict[str, Any]
    human_decision: dict[str, Any] | None


@dataclass(frozen=True)
class StartupReconciliation:
    classification: str
    persisted_state: str
    derived_state: str
    transition_path: tuple[str, ...]
    would_persist: bool
    reason: str
    evidence: dict[str, Any]
    human_decision: dict[str, Any] | None = None


def _queue_fingerprint(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _gate_reopen_policy(project: Project) -> tuple[bool, str]:
    adapter = project.repository / project.validation_source
    try:
        value = json.loads(adapter.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "repository adapter could not be read as JSON-compatible YAML"
    integration = value.get("integration") if isinstance(value, dict) else None
    if isinstance(integration, dict) and integration.get("rerun_milestone_gates") is True:
        return True, "integration.rerun_milestone_gates is true"
    return False, "repository policy does not explicitly permit rerunning milestone gates"


def _timestamp_after(value: str | None, baseline: str | None) -> bool:
    if not value or not baseline:
        return False
    try:
        return datetime.fromisoformat(value).astimezone() > datetime.fromisoformat(baseline).astimezone()
    except (TypeError, ValueError):
        return False


def assess_startup_reconciliation(
    project: Project,
    persisted: dict[str, Any] | None,
    plan: dict[str, Any],
) -> StartupReconciliation:
    """Compare durable controller intent with current read-only repository evidence."""

    persisted_state = str((persisted or {}).get("current_state") or project.current_state)
    queue = plan.get("queue_status") or {}
    repository = plan.get("repository_state") or {}
    locks = plan.get("lock_status") or {}
    active_cycle = plan.get("existing_active_cycle")
    queue_classification = queue.get("reconciliation_classification", "invalid_queue")
    selected_feature = plan.get("selected_feature")
    milestone_complete = queue.get("milestone_complete") is True
    current_head = repository.get("head")
    state_evidence = (persisted or {}).get("state_evidence")
    evidence = {
        "queue_classification": queue_classification,
        "queue_valid": queue.get("valid") is True,
        "milestone_complete": milestone_complete,
        "selected_feature": selected_feature,
        "milestone_head": current_head,
        "queue_fingerprint": _queue_fingerprint(queue.get("resolved_queue_path")),
        "prior_state_decision_commit": (
            state_evidence.get("milestone_head")
            if isinstance(state_evidence, dict)
            else project.last_accepted_commit
        ),
        "prior_state_decision_checkpoint": (persisted or {}).get("last_checkpoint"),
        "prior_state_decision_timestamp": (persisted or {}).get("updated_at"),
        "prior_gate_evidence": state_evidence if isinstance(state_evidence, dict) else None,
        "git_operations": repository.get("git_operations"),
        "repository_clean": repository.get("clean"),
        "repository_branch": repository.get("branch"),
        "repository_cycle_state_exists": repository.get("cycle_state_exists"),
        "repository_worktrees": repository.get("worktrees"),
        "repository_local_branches": repository.get("local_branches"),
        "locks": locks,
        "stale_cycle_evidence": plan.get("stale_cycle_evidence"),
    }
    deterministic_failure = plan.get("stale_cycle_evidence")
    deterministic_failure = (
        deterministic_failure
        if isinstance(deterministic_failure, dict)
        and deterministic_failure.get("classification") == "deterministic_failed_cycle"
        else None
    )

    if active_cycle:
        if active_cycle.get("phase") == "invalid":
            return StartupReconciliation(
                "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
                "repository cycle state is unreadable or invalid", evidence,
                {"reason": "invalid repository cycle state", "safe_action": "inspect and preserve the cycle file"},
            )
        return StartupReconciliation(
            "active_cycle_resume", persisted_state, persisted_state, (), False,
            "a corroborated repository-local cycle must resume before new work", evidence,
        )

    if deterministic_failure and not deterministic_failure.get("environment_remediation_verified"):
        path = (persisted_state,) if persisted_state == "human_decision_required" else (
            persisted_state, "human_decision_required"
        )
        return StartupReconciliation(
            "deterministic_failure_human_gate",
            persisted_state,
            "human_decision_required",
            path,
            persisted_state != "human_decision_required",
            "the failed session is non-retryable until Codex compatibility remediation is verified",
            evidence,
            {
                "reason": "local Codex compatibility remediation is required",
                "classification": deterministic_failure.get("failure_classification"),
                "attempts_consumed": deterministic_failure.get("attempts_consumed"),
                "environment_remediation_verified": False,
                "safe_action": f"scripts/conveyor doctor --project {project.project_id}",
                "safe_continuation_command": f"scripts/conveyor resume --project {project.project_id}",
                "old_session_will_resume": False,
            },
        )

    writer = locks.get("repository_writer") or {}
    launch = locks.get("controller_launch") or {}
    if writer.get("exists") or launch.get("exists"):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "state repair is blocked by an existing writer or controller reservation", evidence,
            {"reason": "lock ownership must be reconciled before state repair"},
        )
    if (
        repository.get("clean") is not True
        or any((repository.get("git_operations") or {}).values())
        or repository.get("branch") != project.milestone_branch
        or repository.get("head") != repository.get("milestone_branch_head")
        or repository.get("baseline_exists") is not True
        or repository.get("milestone_branch_exists") is not True
        or repository.get("baseline_is_ancestor_of_milestone") is not True
    ):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "startup state repair requires a clean repository at the configured milestone HEAD with verified ancestry and no active Git operation",
            evidence,
            {"reason": "preserve repository work and reconcile milestone-branch Git evidence"},
        )
    if queue.get("valid") is not True or queue_classification == "invalid_queue":
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "validation_failed", (), False,
            "queue parsing did not produce valid deterministic evidence", evidence,
        )

    if persisted is None and persisted_state == "queue_reconciliation":
        return StartupReconciliation(
            "state_consistent", persisted_state, persisted_state, (persisted_state,), False,
            "no durable project state exists; the configured queue-reconciliation entry state remains authoritative",
            evidence,
        )

    targets = {
        "reconciled_ready_work": "feature_ready",
        "reconciled_no_ready_work": "paused",
        "milestone_complete": "milestone_gate",
        "legitimately_blocked": "paused",
        "human_decision_required": "human_decision_required",
    }
    derived_state = targets.get(str(queue_classification), "validation_failed")
    if (
        queue_classification == "reconciled_no_ready_work"
        and (queue.get("status_counts") or {}).get("proposed", 0)
        and persisted_state != "paused"
    ):
        derived_state = "queue_reconciliation"
    if derived_state == "feature_ready" and not selected_feature:
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
            "ready-work classification has no deterministic selected feature", evidence,
            {"reason": "queue classification and feature selection contradict each other"},
        )
    if milestone_complete and derived_state != "milestone_gate":
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
            "milestone completion contradicts the queue reconciliation classification", evidence,
            {"reason": "contradictory milestone evidence"},
        )

    if persisted_state == derived_state:
        return StartupReconciliation(
            "state_consistent", persisted_state, derived_state, (persisted_state,), False,
            "persisted state agrees with verified repository and queue evidence", evidence,
        )

    if persisted_state == "milestone_ready_for_merge":
        prior = state_evidence if isinstance(state_evidence, dict) else {}
        human_gate = (persisted or {}).get("human_decision_required")
        prior_head = prior.get("milestone_head") or prior.get("gate_evidence_commit")
        prior_queue = prior.get("queue_fingerprint")
        gate_timestamp = prior.get("gate_evidence_timestamp")
        inspector = RepositoryInspector(project.repository)
        head_advanced_after_gate = bool(
            isinstance(prior_head, str)
            and isinstance(current_head, str)
            and prior_head != current_head
            and inspector.ref_exists(prior_head)
            and inspector.is_ancestor(prior_head, current_head)
            and _timestamp_after(repository.get("head_commit_timestamp"), gate_timestamp)
        )
        queue_changed = bool(
            isinstance(prior_queue, str) and prior_queue != evidence["queue_fingerprint"]
        )
        inputs_changed = head_advanced_after_gate
        policy_allows, policy_reason = _gate_reopen_policy(project)
        provenance_valid = bool(
            (persisted or {}).get("last_checkpoint") == "milestone_gate_passed"
            and isinstance(human_gate, dict)
            and human_gate.get("milestone_branch") == project.milestone_branch
            and human_gate.get("default_branch_merge_performed") is False
            and prior.get("gate_evidence_commit") == prior.get("milestone_head")
            and prior.get("gate_evidence_timestamp")
        )
        evidence["milestone_inputs_changed_after_gate"] = inputs_changed
        evidence["milestone_head_advanced_after_gate"] = head_advanced_after_gate
        evidence["queue_changed_after_gate"] = queue_changed
        evidence["gate_reopen_policy"] = policy_reason
        evidence["gate_evidence_provenance_valid"] = provenance_valid
        if not inputs_changed or not policy_allows or not gate_timestamp or not provenance_valid:
            return StartupReconciliation(
                "human_decision_required", persisted_state, "human_decision_required", (), False,
                "a passed milestone gate cannot be reopened without changed recorded inputs, timestamped gate evidence, and policy authority",
                evidence,
                {
                    "reason": "milestone-ready state requires explicit gate-evidence validation",
                    "inputs_changed": inputs_changed,
                    "policy_allows_reopen": policy_allows,
                },
            )

    if (
        persisted_state == "human_decision_required"
        and not (deterministic_failure and deterministic_failure.get("environment_remediation_verified"))
    ):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "the recorded human decision has not been explicitly resolved", evidence,
            {"reason": "explicit resolution evidence is required"},
        )

    repairable = {
        "milestone_gate", "milestone_ready_for_merge", "feature_ready", "feature_running",
        "feature_accepted", "queue_reconciliation", "validation_failed", "human_decision_required",
        "repository_dirty", "architecture_decision_required", "conveyor_error", "paused",
    }
    if persisted_state not in repairable:
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            f"no safe startup-reconciliation route is defined from {persisted_state}", evidence,
            {"reason": "unsupported persisted state for automatic repair"},
        )

    path = [persisted_state]
    current_feature = (persisted or {}).get("current_feature")
    completed = set(queue.get("completed_features") or [])
    if persisted_state == "feature_running" and current_feature and current_feature in completed:
        path.append("feature_accepted")
    if deterministic_failure and path[-1] != "human_decision_required":
        path.append("human_decision_required")
    if path[-1] != "queue_reconciliation":
        path.append("queue_reconciliation")
    if derived_state != "queue_reconciliation":
        path.append(derived_state)
    return StartupReconciliation(
        "stale_state_repaired", persisted_state, derived_state, tuple(path), True,
        (
            f"persisted {persisted_state} no longer matches verified {queue_classification} evidence; "
            "the obsolete execution intent must be invalidated before scheduling"
        ),
        evidence,
    )


def assess_recovery(project: Project, state: dict[str, Any] | None) -> RecoveryAssessment:
    inspector = RepositoryInspector(project.repository)
    current = inspector.inspect(
        baseline=project.validated_baseline_commit,
        milestone_branch=project.milestone_branch,
    )
    if state is None:
        return RecoveryAssessment("no_active_cycle", None, {"git": current}, None)

    identity = current["identity"]
    conflicts: list[str] = []
    if state.get("repository_path_fingerprint") != identity["path_fingerprint"]:
        conflicts.append("repository path fingerprint disagrees with durable cycle state")
    recorded_identity = state.get("repository_identity", {})
    if isinstance(recorded_identity, dict) and recorded_identity.get("repository_id") != identity["repository_id"]:
        conflicts.append("repository identity disagrees with durable cycle state")
    accepted = state.get("accepted_feature_commit")
    if isinstance(accepted, str) and not inspector.ref_exists(accepted):
        conflicts.append("recorded accepted feature commit does not exist")

    phase = state.get("current_phase")
    operations = [name for name, active in current["git_operations"].items() if active]
    integration_phases = {"integration_pending", "integrating", "integration_validation"}
    if operations and phase not in integration_phases:
        conflicts.append("active Git operation is inconsistent with the recorded cycle phase")

    feature_phases = {"branch_preparing", "feature_in_progress", "feature_review", "feature_repair"}
    expected_feature_branch = state.get("feature_branch")
    if (
        not conflicts
        and phase in feature_phases
        and isinstance(expected_feature_branch, str)
        and (
            not inspector.ref_exists(expected_feature_branch)
            or current.get("branch") != expected_feature_branch
            or inspector.branch_worktree(expected_feature_branch) != project.repository.resolve()
        )
    ):
        return RecoveryAssessment(
            "branch_recovery_required",
            None,
            {"git": current, "state": state},
            {
                "reason": "persisted feature branch is not the actual repository session branch",
                "actual_branch": current.get("branch"),
                "expected_branch": expected_feature_branch,
                "branch_recovery_required": True,
                "resume_allowed": False,
                "safe_action": f"scripts/conveyor recover-feature-branch --project {project.project_id}",
            },
        )

    last_git = state.get("last_verified_git_state")
    if isinstance(last_git, dict):
        expected_branch = last_git.get("branch")
        if expected_branch and current["branch"] != expected_branch and not operations:
            conflicts.append("current branch differs from the last verified checkpoint")

    if conflicts:
        decision = {
            "conflicting_evidence": conflicts,
            "branches_and_commits": {
                "current_branch": current["branch"],
                "current_head": current["head"],
                "feature_branch": state.get("feature_branch"),
                "accepted_feature_commit": accepted,
                "milestone_branch": state.get("milestone_branch"),
                "milestone_pre_integration_commit": state.get("milestone_pre_integration_commit"),
                "milestone_post_integration_commit": state.get("milestone_post_integration_commit"),
            },
            "git_operations": operations,
            "affected_files": inspector.dirty_entries,
            "safe_actions": ["inspect", "preserve current work", "supply a non-destructive reconciliation decision"],
            "recommended_action": "Preserve all refs and worktrees, reconcile the listed evidence, then resume.",
            "resume_command": f"scripts/conveyor resume --project {project.project_id}",
        }
        return RecoveryAssessment("human_decision_required", None, {"git": current, "state": state}, decision)

    if phase == "completed":
        return RecoveryAssessment("already_completed", None, {"git": current, "state": state}, None)
    if not isinstance(phase, str):
        raise RecoveryError("cycle state does not contain a valid phase")
    return RecoveryAssessment("resume", phase, {"git": current, "state": state}, None)
