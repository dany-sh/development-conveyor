"""Typed executable routing derived from the authoritative kernel projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .contracts import WorkflowType, WORKFLOW_LEASE
from .errors import ProjectionError
from .projection import projection_fingerprint


HUMAN_MERGE_APPROVAL = "human_merge_approval"

ACTION_WORKFLOW: dict[str, WorkflowType | str] = {
    "queue_reconciliation": WorkflowType.QUEUE_RECONCILIATION,
    "feature_cycle": WorkflowType.FEATURE_EXECUTION,
    "milestone_integration": WorkflowType.MILESTONE_INTEGRATION,
    "milestone_gate": WorkflowType.MILESTONE_GATE,
    "human_decision_resolution": WorkflowType.HUMAN_DECISION_RESOLUTION,
    "verify_consistency": WorkflowType.RECOVERY,
    HUMAN_MERGE_APPROVAL: HUMAN_MERGE_APPROVAL,
}

WORKFLOW_NEXT_STATE: dict[WorkflowType, str] = {
    WorkflowType.QUEUE_RECONCILIATION: "feature_ready",
    WorkflowType.FEATURE_PREPARATION: "feature_preparing",
    WorkflowType.FEATURE_EXECUTION: "feature_accepted",
    WorkflowType.FEATURE_ACCEPTANCE: "integration_ready",
    WorkflowType.MILESTONE_INTEGRATION: "feature_integrated",
    WorkflowType.MILESTONE_GATE: "milestone_ready_for_merge",
    WorkflowType.HUMAN_DECISION_RESOLUTION: "queue_reconciliation",
    WorkflowType.RECOVERY: "verify_consistency",
}

SESSION_DESCRIPTION: dict[WorkflowType, str] = {
    WorkflowType.QUEUE_RECONCILIATION: "fresh queue-reconciliation transaction",
    WorkflowType.FEATURE_PREPARATION: "fresh feature-preparation transaction",
    WorkflowType.FEATURE_EXECUTION: "fresh feature transaction",
    WorkflowType.FEATURE_ACCEPTANCE: "fresh feature-acceptance transaction",
    WorkflowType.MILESTONE_INTEGRATION: "fresh milestone-integration transaction",
    WorkflowType.MILESTONE_GATE: "fresh milestone-gate transaction",
    WorkflowType.HUMAN_DECISION_RESOLUTION: "fresh human-decision transaction",
    WorkflowType.RECOVERY: "fresh recovery transaction",
}


@dataclass(frozen=True)
class ExecutionPlan:
    project_id: str
    ledger_sequence: int
    ledger_fingerprint: str
    projection_fingerprint: str
    current_state: str
    workflow_type: str
    feature_id: str | None
    accepted_commit: str | None
    starting_commit: str | None
    feature_branch: str | None
    milestone_branch: str | None
    transaction_mode: str
    session_resume_eligible: bool
    session_to_resume: str | None
    lease_type: str | None
    application_mutation_expected: bool
    next_state_on_success: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def workflow(self) -> WorkflowType | None:
        try:
            return WorkflowType(self.workflow_type)
        except ValueError:
            return None

    @property
    def sessions_that_would_launch(self) -> list[str]:
        if self.workflow_type == HUMAN_MERGE_APPROVAL:
            return []
        if self.workflow in {
            WorkflowType.HUMAN_DECISION_RESOLUTION,
            WorkflowType.RECOVERY,
        }:
            return []
        if self.transaction_mode == "recovery":
            if not self.session_resume_eligible:
                return []
            return ["resume exact kernel transaction"]
        workflow = self.workflow
        if workflow is None:
            raise ProjectionError("execution plan has an unsupported workflow type")
        return [SESSION_DESCRIPTION[workflow]]

    @classmethod
    def from_projection(
        cls,
        projection: dict[str, Any],
        *,
        starting_commit: str | None = None,
        feature_branch: str | None = None,
        milestone_branch: str | None = None,
    ) -> "ExecutionPlan":
        claimed = projection.get("projection_fingerprint")
        if not isinstance(claimed, str) or claimed != projection_fingerprint(projection):
            raise ProjectionError("authoritative projection fingerprint is invalid")
        action = str(projection.get("allowed_next_action") or "verify_consistency")
        if action not in ACTION_WORKFLOW:
            raise ProjectionError(f"projection has unsupported next action: {action}")
        active_id = projection.get("active_transaction")
        active = next(
            (
                item for item in projection.get("transactions", [])
                if isinstance(item, dict) and item.get("transaction_id") == active_id
            ),
            None,
        )
        if active is not None:
            try:
                workflow = WorkflowType(str(active["workflow_type"]))
            except (KeyError, ValueError) as exc:
                raise ProjectionError("active projection transaction has invalid workflow type") from exc
        else:
            workflow = ACTION_WORKFLOW.get(action, WorkflowType.RECOVERY)
        resume = bool(projection.get("session_resume_eligible"))
        sessions = list((active or {}).get("session_ids") or [])
        session_to_resume = sessions[-1] if resume and sessions else None
        if resume and session_to_resume is None:
            raise ProjectionError("resume-eligible projection lacks an exact kernel session")
        projected_lease = projection.get("required_lease")
        lease = WORKFLOW_LEASE[workflow].value if isinstance(workflow, WorkflowType) else None
        if projected_lease is not None and projected_lease != lease:
            raise ProjectionError("projection required lease disagrees with its workflow")
        transaction_mode = "recovery" if active is not None else "fresh"
        gate = projection.get("human_gate")
        gate_identity = gate if isinstance(gate, dict) else {}
        feature_id = (
            gate_identity.get("feature_id")
            or gate_identity.get("feature")
            or projection.get("current_feature")
            or projection.get("selected_next_feature")
        )
        accepted_commit = (
            gate_identity.get("accepted_feature_commit")
            or gate_identity.get("accepted_commit")
            or projection.get("accepted_feature_commit")
        )
        projected_starting_commit = (
            gate_identity.get("feature_starting_commit")
            or gate_identity.get("candidate_validated_planning_commit")
            or projection.get("selected_feature_starting_commit")
        )
        projected_feature_branch = (
            gate_identity.get("feature_branch")
            or projection.get("feature_branch")
        )
        projected_milestone_branch = (
            gate_identity.get("milestone_branch")
            or projection.get("milestone_branch")
        )
        plan = cls(
            project_id=str(projection.get("project_id") or ""),
            ledger_sequence=int(projection.get("ledger_sequence", -1)),
            ledger_fingerprint=str(projection.get("ledger_fingerprint") or ""),
            projection_fingerprint=claimed,
            current_state=str(projection.get("current_state") or ""),
            workflow_type=workflow.value if isinstance(workflow, WorkflowType) else workflow,
            feature_id=feature_id,
            accepted_commit=accepted_commit,
            starting_commit=projected_starting_commit or starting_commit,
            feature_branch=projected_feature_branch or feature_branch,
            milestone_branch=projected_milestone_branch or milestone_branch,
            transaction_mode=transaction_mode,
            session_resume_eligible=resume,
            session_to_resume=session_to_resume,
            lease_type=lease,
            application_mutation_expected=isinstance(workflow, WorkflowType) and workflow not in {
                WorkflowType.FEATURE_ACCEPTANCE,
                WorkflowType.HUMAN_DECISION_RESOLUTION,
                WorkflowType.RECOVERY,
            },
            next_state_on_success=(
                WORKFLOW_NEXT_STATE[workflow]
                if isinstance(workflow, WorkflowType) else "milestone_ready_for_merge"
            ),
        )
        plan.validate_against(projection)
        return plan

    def validate_against(self, projection: dict[str, Any]) -> None:
        action = str(projection.get("allowed_next_action") or "verify_consistency")
        expected_workflow = ACTION_WORKFLOW.get(action, WorkflowType.RECOVERY)
        active_id = projection.get("active_transaction")
        if active_id is not None:
            active = next(
                (item for item in projection.get("transactions", []) if item.get("transaction_id") == active_id),
                None,
            )
            if active is None:
                raise ProjectionError("execution plan names an absent active transaction")
            expected_workflow = WorkflowType(str(active["workflow_type"]))
        gate = projection.get("human_gate")
        gate_identity = gate if isinstance(gate, dict) else {}
        expected_feature = (
            gate_identity.get("feature_id")
            or gate_identity.get("feature")
            or projection.get("current_feature")
            or projection.get("selected_next_feature")
        )
        expected_accepted_commit = (
            gate_identity.get("accepted_feature_commit")
            or gate_identity.get("accepted_commit")
            or projection.get("accepted_feature_commit")
        )
        checks = {
            "project_id": self.project_id == projection.get("project_id"),
            "ledger_sequence": self.ledger_sequence == projection.get("ledger_sequence"),
            "ledger_fingerprint": self.ledger_fingerprint == projection.get("ledger_fingerprint"),
            "projection_fingerprint": self.projection_fingerprint == projection.get("projection_fingerprint"),
            "current_state": self.current_state == projection.get("current_state"),
            "workflow_type": self.workflow_type == (
                expected_workflow.value
                if isinstance(expected_workflow, WorkflowType) else expected_workflow
            ),
            "feature_id": self.feature_id == expected_feature,
            "accepted_commit": self.accepted_commit == expected_accepted_commit,
            "session_resume_eligible": self.session_resume_eligible
            == bool(projection.get("session_resume_eligible")),
            "lease_type": self.lease_type == (
                projection.get("required_lease")
                or (
                    WORKFLOW_LEASE[expected_workflow].value
                    if isinstance(expected_workflow, WorkflowType) else None
                )
            ),
        }
        projected_start = (
            gate_identity.get("feature_starting_commit")
            or gate_identity.get("candidate_validated_planning_commit")
            or projection.get("selected_feature_starting_commit")
        )
        projected_feature_branch = (
            gate_identity.get("feature_branch")
            or projection.get("feature_branch")
        )
        projected_milestone_branch = (
            gate_identity.get("milestone_branch")
            or projection.get("milestone_branch")
        )
        if projected_start is not None:
            checks["starting_commit"] = self.starting_commit == projected_start
        if projected_feature_branch is not None:
            checks["feature_branch"] = self.feature_branch == projected_feature_branch
        if projected_milestone_branch is not None:
            checks["milestone_branch"] = self.milestone_branch == projected_milestone_branch
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise ProjectionError(f"execution plan disagrees with authoritative projection: {failed}")


def superseded_legacy_cycles(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return preserved superseded-cycle diagnostics without treating them as runnable."""

    values: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for event in events:
        if event.get("event_type") != "TransactionCompleted":
            continue
        for item in event.get("payload", {}).get("superseded", []) or []:
            if not isinstance(item, dict):
                continue
            value = {
                "workflow_type": item.get("workflow_type"),
                "run_id": item.get("run_id"),
                "session_id": item.get("session_id"),
                "feature_id": item.get("feature_id"),
                "legacy_phase": item.get("legacy_phase"),
                "classification": "superseded",
                "reason": item.get("reason"),
            }
            identity = tuple(value.get(key) for key in (
                "workflow_type", "run_id", "session_id", "feature_id", "legacy_phase"
            ))
            if identity not in seen:
                seen.add(identity)
                values.append(value)
    return values


def authoritative_status_fields(
    projection: dict[str, Any],
    executable: ExecutionPlan,
    *,
    legacy_plan: dict[str, Any],
    persisted_state: str | None,
    superseded_cycles: list[dict[str, Any]],
    persisted_projection_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Normalize every executable status field to one projection-derived plan."""

    executable.validate_against(projection)
    action = str(projection.get("allowed_next_action") or "verify_consistency")
    workflow = executable.workflow
    legacy_observations = {
        "persisted_compatibility_state": persisted_state,
        "legacy_proposed_next_action": legacy_plan.get("proposed_next_action"),
        "legacy_existing_cycle": legacy_plan.get("existing_active_cycle"),
        "legacy_stale_cycle_evidence": legacy_plan.get("stale_cycle_evidence"),
        "legacy_sessions_that_would_launch": legacy_plan.get("sessions_that_would_launch", []),
    }
    legacy_gate = legacy_plan.get("integration_gate")
    projection_gate = projection.get("human_gate")
    identity_gate = (
        legacy_gate
        if isinstance(legacy_gate, dict)
        else (projection_gate if isinstance(projection_gate, dict) else {})
    )
    status_feature = (
        identity_gate.get("feature_id")
        or identity_gate.get("feature")
        or executable.feature_id
    )
    status_accepted_commit = (
        identity_gate.get("accepted_feature_commit")
        or identity_gate.get("accepted_commit")
        or executable.accepted_commit
    )
    status_starting_commit = (
        identity_gate.get("feature_starting_commit")
        or identity_gate.get("candidate_validated_planning_commit")
        or executable.starting_commit
    )
    status_feature_branch = (
        identity_gate.get("feature_branch") or executable.feature_branch
    )
    status_milestone_branch = (
        identity_gate.get("milestone_branch") or executable.milestone_branch
    )
    ordinary_resume_allowed = workflow is not None and workflow not in {
        WorkflowType.HUMAN_DECISION_RESOLUTION,
        WorkflowType.RECOVERY,
    }
    compatibility_stale = (
        persisted_state != projection["current_state"]
        or persisted_projection_fingerprint != projection["projection_fingerprint"]
    )
    return {
        "current_state": projection["current_state"],
        "derived_state": projection["current_state"],
        "persisted_state": projection["current_state"],
        "selected_feature": status_feature,
        "accepted_feature_commit": status_accepted_commit,
        "feature_starting_commit": status_starting_commit,
        "feature_branch": status_feature_branch,
        "milestone_branch": status_milestone_branch,
        "next_action": action,
        "proposed_next_action": action,
        "workflow_type": executable.workflow_type,
        "transaction_mode": executable.transaction_mode,
        "session_to_resume": executable.session_to_resume,
        "lease_type": executable.lease_type,
        "required_lease": executable.lease_type,
        "application_mutation_expected": executable.application_mutation_expected,
        "next_state_on_success": executable.next_state_on_success,
        "state_source": "evidence_ledger_projection",
        "human_gate": identity_gate or None,
        "human_decision_required": identity_gate or None,
        "kernel_projection": projection,
        "executable_plan": executable.to_dict(),
        "execution_plan": executable.to_dict(),
        "legacy_observations": legacy_observations,
        "superseded_legacy_cycles": superseded_cycles,
        "existing_active_cycle": None,
        "stale_cycle_evidence": None,
        "session_resume_eligible": executable.session_resume_eligible,
        "old_session_will_resume": executable.session_resume_eligible,
        "sessions_that_would_launch": executable.sessions_that_would_launch,
        "fresh_transaction": executable.transaction_mode == "fresh",
        "ordinary_resume_allowed": ordinary_resume_allowed,
        "resume_allowed": ordinary_resume_allowed,
        "resume_allowed_after_lock": ordinary_resume_allowed,
        "writer_lock_required_before_resume": False,
        "writer_lock_would_be_acquired_before_mutation": executable.application_mutation_expected,
        "feature_factory_would_launch": workflow == WorkflowType.FEATURE_EXECUTION,
        "milestone_integrator_would_launch": workflow == WorkflowType.MILESTONE_INTEGRATION,
        "dry_run_writes_application_repository": False,
        "state_consistency": "projection_authoritative",
        "repair_transition_path": [],
        "execution_state_path": [projection["current_state"], executable.next_state_on_success],
        "would_persist_state_repair": compatibility_stale,
        "state_reconciliation_reason": (
            "controller compatibility cache is stale and ignored for execution"
            if compatibility_stale
            else "controller compatibility cache agrees with the kernel projection"
        ),
        "compatibility_cache": {
            "observed_state": persisted_state,
            "observed_projection_fingerprint": persisted_projection_fingerprint,
            "projected_state": projection["current_state"],
            "stale": compatibility_stale,
            "ledger_sequence": projection["ledger_sequence"],
            "ledger_fingerprint": projection["ledger_fingerprint"],
            "projection_fingerprint": projection["projection_fingerprint"],
        },
    }


def execution_plan_projection_agreement(
    projection: dict[str, Any], status: dict[str, Any], executable: ExecutionPlan
) -> tuple[bool, dict[str, Any]]:
    """Compare all routing aliases and runnable-session fields to the projection."""

    action = projection.get("allowed_next_action")
    expected_sessions = executable.sessions_that_would_launch
    expected_lease = projection.get("required_lease") or executable.lease_type
    gate = status.get("integration_gate")
    if not isinstance(gate, dict):
        gate = status.get("human_gate")
    identity_gate = gate if isinstance(gate, dict) else {}
    expected_feature = (
        identity_gate.get("feature_id")
        or identity_gate.get("feature")
        or executable.feature_id
    )
    expected_accepted_commit = (
        identity_gate.get("accepted_feature_commit")
        or identity_gate.get("accepted_commit")
        or executable.accepted_commit
    )
    expected_starting_commit = (
        identity_gate.get("feature_starting_commit")
        or identity_gate.get("candidate_validated_planning_commit")
        or executable.starting_commit
    )
    expected_feature_branch = (
        identity_gate.get("feature_branch") or executable.feature_branch
    )
    expected_milestone_branch = (
        identity_gate.get("milestone_branch") or executable.milestone_branch
    )
    checks = {
        "kernel_current_state": status.get("current_state") == projection.get("current_state"),
        "derived_state": status.get("derived_state") == projection.get("current_state"),
        "persisted_compatibility_state": status.get("persisted_state") == projection.get("current_state"),
        "allowed_next_action": status.get("next_action") == action,
        "proposed_next_action": status.get("proposed_next_action") == action,
        "workflow_type": status.get("workflow_type") == executable.workflow_type,
        "session_resume_eligibility": status.get("session_resume_eligible")
        == bool(projection.get("session_resume_eligible")),
        "old_session_resume": status.get("old_session_will_resume")
        == bool(projection.get("session_resume_eligible")),
        "sessions_that_would_launch": status.get("sessions_that_would_launch") == expected_sessions,
        "executable_plan": status.get("executable_plan") == executable.to_dict(),
        "execution_plan": status.get("execution_plan") == executable.to_dict(),
        "selected_feature": status.get("selected_feature") == expected_feature,
        "accepted_commit": status.get("accepted_feature_commit")
        == expected_accepted_commit,
        "starting_commit": status.get("feature_starting_commit")
        == expected_starting_commit,
        "feature_branch": status.get("feature_branch") == expected_feature_branch,
        "milestone_branch": status.get("milestone_branch")
        == expected_milestone_branch,
        "required_lease": status.get("required_lease") == expected_lease,
        "executable_plan_lease": (status.get("executable_plan") or {}).get("lease_type")
        == expected_lease,
    }
    return all(checks.values()), {"checks": checks, "expected_sessions": expected_sessions}
