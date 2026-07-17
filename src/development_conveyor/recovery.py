"""Evidence-based interrupted-cycle reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
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

