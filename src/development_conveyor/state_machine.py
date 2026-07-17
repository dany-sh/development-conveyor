"""Explicit portfolio and feature-cycle state machines."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidTransition

PORTFOLIO_STATES = (
    "discover", "bootstrap_required", "baseline_required", "queue_reconciliation",
    "feature_ready", "feature_running", "feature_review", "feature_accepted",
    "milestone_gate", "milestone_ready_for_merge", "human_merge_approval",
    "next_milestone", "human_decision_required", "repository_dirty",
    "validation_failed", "architecture_decision_required", "destructive_change_required",
    "conveyor_error", "paused", "disabled",
)

CYCLE_STATES = (
    "idle", "preflight", "feature_selected", "branch_preparing", "feature_in_progress",
    "feature_review", "feature_repair", "feature_accepted", "integration_pending",
    "integrating", "integration_validation", "feature_integrated", "next_feature_selection",
    "milestone_gate", "completed", "blocked", "human_decision_required", "failed",
)

PORTFOLIO_TRANSITIONS = {
    "discover": {"bootstrap_required", "baseline_required", "queue_reconciliation", "feature_ready", "repository_dirty", "human_decision_required", "conveyor_error", "paused", "disabled"},
    "bootstrap_required": {"discover", "baseline_required", "queue_reconciliation", "human_decision_required", "conveyor_error", "paused", "disabled"},
    "baseline_required": {"queue_reconciliation", "repository_dirty", "human_decision_required", "conveyor_error", "paused", "disabled"},
    "queue_reconciliation": {"feature_ready", "milestone_gate", "validation_failed", "architecture_decision_required", "human_decision_required", "conveyor_error", "paused", "disabled"},
    "feature_ready": {"feature_running", "repository_dirty", "human_decision_required", "conveyor_error", "paused", "disabled"},
    "feature_running": {"feature_review", "feature_accepted", "feature_ready", "milestone_gate", "repository_dirty", "validation_failed", "architecture_decision_required", "destructive_change_required", "human_decision_required", "conveyor_error", "paused"},
    "feature_review": {"feature_running", "feature_accepted", "validation_failed", "architecture_decision_required", "destructive_change_required", "human_decision_required", "conveyor_error", "paused"},
    "feature_accepted": {"feature_ready", "milestone_gate", "validation_failed", "human_decision_required", "conveyor_error", "paused"},
    "milestone_gate": {"milestone_ready_for_merge", "validation_failed", "architecture_decision_required", "human_decision_required", "conveyor_error", "paused"},
    "milestone_ready_for_merge": {"human_merge_approval", "human_decision_required", "paused"},
    "human_merge_approval": {"next_milestone", "human_decision_required", "paused"},
    "next_milestone": {"queue_reconciliation", "feature_ready", "human_decision_required", "paused", "disabled"},
    "human_decision_required": {"discover", "queue_reconciliation", "feature_ready", "feature_running", "milestone_gate", "paused", "disabled"},
    "repository_dirty": {"discover", "queue_reconciliation", "feature_ready", "human_decision_required", "paused", "disabled"},
    "validation_failed": {"queue_reconciliation", "feature_running", "milestone_gate", "human_decision_required", "paused", "disabled"},
    "architecture_decision_required": {"queue_reconciliation", "feature_ready", "human_decision_required", "paused", "disabled"},
    "destructive_change_required": {"human_decision_required", "paused", "disabled"},
    "conveyor_error": {"discover", "queue_reconciliation", "feature_ready", "feature_running", "human_decision_required", "paused", "disabled"},
    "paused": {"discover", "queue_reconciliation", "feature_ready", "feature_running", "milestone_gate", "human_decision_required", "disabled"},
    "disabled": {"discover", "paused"},
}

CYCLE_TRANSITIONS = {
    "idle": {"preflight"},
    "preflight": {"feature_selected", "milestone_gate", "blocked", "human_decision_required", "failed"},
    "feature_selected": {"branch_preparing", "human_decision_required", "failed"},
    "branch_preparing": {"feature_in_progress", "human_decision_required", "failed"},
    "feature_in_progress": {"feature_review", "feature_repair", "feature_accepted", "human_decision_required", "failed"},
    "feature_review": {"feature_repair", "feature_accepted", "human_decision_required", "failed"},
    "feature_repair": {"feature_in_progress", "feature_review", "integration_validation", "human_decision_required", "failed"},
    "feature_accepted": {"integration_pending", "human_decision_required", "failed"},
    "integration_pending": {"integrating", "human_decision_required", "failed"},
    "integrating": {"integration_validation", "human_decision_required", "failed"},
    "integration_validation": {"feature_integrated", "feature_repair", "human_decision_required", "failed"},
    "feature_integrated": {"next_feature_selection", "milestone_gate", "completed"},
    "next_feature_selection": {"feature_selected", "milestone_gate", "blocked", "human_decision_required", "completed"},
    "milestone_gate": {"completed", "human_decision_required", "failed"},
    "completed": set(),
    "blocked": {"preflight", "human_decision_required", "failed"},
    "human_decision_required": {"preflight", "branch_preparing", "feature_in_progress", "integration_pending", "integrating", "milestone_gate", "failed"},
    "failed": {"preflight", "feature_in_progress", "integration_pending", "milestone_gate", "human_decision_required"},
}


@dataclass(frozen=True)
class Transition:
    previous: str
    current: str
    changed: bool


class StateMachine:
    def __init__(self, states: tuple[str, ...], transitions: dict[str, set[str]]):
        self.states = states
        self.transitions = transitions
        if set(states) != set(transitions):
            raise ValueError("every state must have an explicit transition set")

    def transition(self, current: str, target: str) -> Transition:
        if current not in self.transitions:
            raise InvalidTransition(f"unknown current state: {current}")
        if target not in self.transitions:
            raise InvalidTransition(f"unknown target state: {target}")
        if current == target:
            return Transition(previous=current, current=target, changed=False)
        if target not in self.transitions[current]:
            raise InvalidTransition(f"invalid transition: {current} -> {target}")
        return Transition(previous=current, current=target, changed=True)


PORTFOLIO_MACHINE = StateMachine(PORTFOLIO_STATES, PORTFOLIO_TRANSITIONS)
CYCLE_MACHINE = StateMachine(CYCLE_STATES, CYCLE_TRANSITIONS)
