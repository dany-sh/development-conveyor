"""Typed executable routing derived from the authoritative kernel projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .contracts import WorkflowType, WORKFLOW_LEASE
from .errors import ProjectionError
from .projection import projection_fingerprint
from .queue import FeatureQueue


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


def bind_projection_to_queue(
    projection: dict[str, Any], queue: FeatureQueue, milestone_id: str
) -> dict[str, Any]:
    """Bind idle next-feature routing to the live validated milestone queue."""

    if projection.get("active_transaction") is not None:
        return projection
    action = projection.get("allowed_next_action")
    candidate = projection.get("current_feature") or projection.get(
        "selected_next_feature"
    )
    queued_candidate = queue.feature(str(candidate)) if candidate else None
    terminal_candidate = bool(
        isinstance(queued_candidate, dict)
        and queued_candidate.get("status") == "integrated"
        and queued_candidate.get("integration_status") == "passed"
    )
    if action not in {"feature_cycle", "queue_reconciliation"} and not terminal_candidate:
        return projection

    selected = queue.select_next(milestone_id)
    bound = dict(projection)
    bound.update(
        {
            "accepted_feature_commit": None,
            "integration_status": None,
            "required_lease": None,
            "session_resume_eligible": False,
            "human_gate": None,
        }
    )
    if selected is None:
        bound.update(
            {
                "current_state": "queue_reconciliation",
                "current_feature": None,
                "selected_next_feature": None,
                "feature_branch": None,
                "allowed_next_action": "queue_reconciliation",
            }
        )
    else:
        feature = queue.feature(selected.feature_id) or {}
        bound.update(
            {
                "current_state": "feature_ready",
                "current_feature": selected.feature_id,
                "selected_next_feature": selected.feature_id,
                "feature_branch": feature.get("branch"),
                "allowed_next_action": "feature_cycle",
            }
        )
    bound["projection_fingerprint"] = projection_fingerprint(bound)
    return bound


def integrated_feature_execution_checks(
    projection: dict[str, Any], executable: "ExecutionPlan", queue: FeatureQueue,
    milestone_id: str,
) -> dict[str, bool]:
    """Return fail-closed queue/plan checks for terminal feature identities."""

    summary = queue.summary(milestone_id)
    integrated = {
        str(item.get("id"))
        for item in queue.features_for_milestone(milestone_id)
        if item.get("status") == "integrated"
        and item.get("integration_status") == "passed"
    }
    historical = projection.get("historical_integration_outcomes") or []
    historical_commits = {
        item.get("accepted_commit")
        for item in historical
        if isinstance(item, dict) and isinstance(item.get("accepted_commit"), str)
    }
    executable_feature_workflow = executable.workflow in {
        WorkflowType.FEATURE_EXECUTION,
        WorkflowType.MILESTONE_INTEGRATION,
    }
    projected = projection.get("current_feature") or projection.get(
        "selected_next_feature"
    )
    return {
        "integrated_feature_not_executable": not (
            executable_feature_workflow and executable.feature_id in integrated
        ),
        "empty_ready_queue_has_no_feature_plan": not (
            not summary["ready_features"]
            and executable.workflow == WorkflowType.FEATURE_EXECUTION
        ),
        "projected_selection_matches_queue": (
            projection.get("allowed_next_action") != "feature_cycle"
            or projected == summary["selected_feature"]
        ),
        "historical_accepted_commit_not_reused": not (
            executable_feature_workflow
            and executable.accepted_commit in historical_commits
        ),
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
            WorkflowType.MILESTONE_INTEGRATION,
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
        historical = projection.get("historical_integration_outcomes") or []
        integrated_features = {
            item.get("feature_id")
            for item in historical
            if isinstance(item, dict) and item.get("classification") == "INTEGRATED"
        }
        historical_accepted_commits = {
            item.get("accepted_commit")
            for item in historical
            if isinstance(item, dict) and isinstance(item.get("accepted_commit"), str)
        }
        if workflow in {
            WorkflowType.FEATURE_EXECUTION,
            WorkflowType.MILESTONE_INTEGRATION,
        }:
            if not isinstance(feature_id, str) or not feature_id:
                raise ProjectionError("executable feature workflow lacks a selected feature")
            if feature_id in integrated_features:
                raise ProjectionError("integrated feature cannot receive a new executable plan")
            if accepted_commit in historical_accepted_commits:
                raise ProjectionError(
                    "historical accepted commit cannot create a fresh executable plan"
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
    effective_superseded = list(superseded_cycles)
    legacy_cycle = legacy_plan.get("existing_active_cycle")
    integrated_features = {
        item.get("feature_id")
        for item in projection.get("historical_integration_outcomes", [])
        if isinstance(item, dict) and item.get("classification") == "INTEGRATED"
    }
    if (
        isinstance(legacy_cycle, dict)
        and legacy_cycle.get("feature") in integrated_features
        and not any(
            item.get("run_id") == legacy_cycle.get("run_id")
            for item in effective_superseded
        )
    ):
        effective_superseded.append(
            {
                "workflow_type": WorkflowType.FEATURE_EXECUTION.value,
                "run_id": legacy_cycle.get("run_id"),
                "session_id": legacy_cycle.get("session_id"),
                "feature_id": legacy_cycle.get("feature"),
                "legacy_phase": legacy_cycle.get("phase"),
                "classification": "superseded",
                "reason": "completed integration is terminal for the legacy feature cycle",
            }
        )
    legacy_observations = {
        "persisted_compatibility_state": persisted_state,
        "legacy_proposed_next_action": legacy_plan.get("proposed_next_action"),
        "legacy_existing_cycle": legacy_plan.get("existing_active_cycle"),
        "legacy_stale_cycle_evidence": legacy_plan.get("stale_cycle_evidence"),
        "legacy_sessions_that_would_launch": legacy_plan.get("sessions_that_would_launch", []),
    }
    projection_gate = projection.get("human_gate")
    identity_gate = (
        projection_gate if isinstance(projection_gate, dict) else {}
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
        "superseded_legacy_cycles": effective_superseded,
        "existing_active_cycle": None,
        "stale_cycle_evidence": None,
        "branch_recovery_required": False,
        "cycle_phase": None,
        "cycle_stop_reason": None,
        "expected_branch": status_feature_branch,
        "expected_stop_condition": (
            "Reconcile the validated milestone queue without reusing terminal feature identity."
            if workflow == WorkflowType.QUEUE_RECONCILIATION
            else legacy_plan.get("expected_stop_condition")
        ),
        "session_resume_eligible": executable.session_resume_eligible,
        "old_session_will_resume": executable.session_resume_eligible,
        # Transaction descriptions are deliberately distinct from actual model
        # launches: a transaction may be deterministic.
        "transactions_that_would_start": executable.sessions_that_would_launch,
        "model_sessions_that_would_launch": (
            ["queue reconciliation parent session"]
            if workflow == WorkflowType.QUEUE_RECONCILIATION else executable.sessions_that_would_launch
        ),
        "child_sessions_that_would_launch": [],
        "sessions_that_would_launch": executable.sessions_that_would_launch,
        "fresh_transaction": executable.transaction_mode == "fresh",
        "ordinary_resume_allowed": ordinary_resume_allowed,
        "resume_allowed": ordinary_resume_allowed,
        "resume_allowed_after_lock": ordinary_resume_allowed,
        "writer_lock_required_before_resume": False,
        "writer_lock_would_be_acquired_before_mutation": executable.application_mutation_expected,
        "feature_factory_would_launch": workflow == WorkflowType.FEATURE_EXECUTION,
        "milestone_integrator_would_launch": False,
        "deterministic_integration_executor_would_run": workflow
        == WorkflowType.MILESTONE_INTEGRATION,
        "model_session_would_launch": workflow == WorkflowType.QUEUE_RECONCILIATION or bool(executable.sessions_that_would_launch),
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
