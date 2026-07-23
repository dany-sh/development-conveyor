"""Typed contracts shared by every writable Conveyor workflow."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import SchemaValidationError, TransactionError
from .warning_evidence import WarningEvidenceError, normalize_warning_evidence


class WorkflowType(str, Enum):
    QUEUE_RECONCILIATION = "queue_reconciliation"
    FEATURE_PREPARATION = "feature_preparation"
    FEATURE_EXECUTION = "feature_execution"
    FEATURE_ACCEPTANCE = "feature_acceptance"
    MILESTONE_INTEGRATION = "milestone_integration"
    MILESTONE_GATE = "milestone_gate"
    HUMAN_DECISION_RESOLUTION = "human_decision_resolution"
    RECOVERY = "recovery"


class TransactionState(str, Enum):
    CREATED = "created"
    LEASE_PENDING = "lease_pending"
    ACTIVE = "active"
    RESULT_PENDING = "result_pending"
    VALIDATING = "validating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    SUPERSEDED = "superseded"


TERMINAL_STATES = frozenset({
    TransactionState.COMPLETED,
    TransactionState.BLOCKED,
    TransactionState.HUMAN_DECISION_REQUIRED,
    TransactionState.RETRYABLE_FAILURE,
    TransactionState.TERMINAL_FAILURE,
    TransactionState.SUPERSEDED,
})


ALLOWED_TRANSITIONS: dict[TransactionState, frozenset[TransactionState]] = {
    TransactionState.CREATED: frozenset({TransactionState.LEASE_PENDING, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    TransactionState.LEASE_PENDING: frozenset({TransactionState.ACTIVE, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    TransactionState.ACTIVE: frozenset({TransactionState.RESULT_PENDING, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    TransactionState.RESULT_PENDING: frozenset({TransactionState.VALIDATING, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    TransactionState.VALIDATING: frozenset({TransactionState.FINALIZING, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    TransactionState.FINALIZING: frozenset({TransactionState.COMPLETED, *TERMINAL_STATES - {TransactionState.COMPLETED}}),
    **{state: frozenset() for state in TERMINAL_STATES},
}


class LeaseType(str, Enum):
    PLANNING_WRITER = "planning_writer"
    FEATURE_WRITER = "feature_writer"
    INTEGRATION_WRITER = "integration_writer"
    MILESTONE_GATE_WRITER = "milestone_gate_writer"
    RECOVERY_WRITER = "recovery_writer"


WORKFLOW_LEASE: dict[WorkflowType, LeaseType] = {
    WorkflowType.QUEUE_RECONCILIATION: LeaseType.PLANNING_WRITER,
    WorkflowType.FEATURE_PREPARATION: LeaseType.FEATURE_WRITER,
    WorkflowType.FEATURE_EXECUTION: LeaseType.FEATURE_WRITER,
    WorkflowType.FEATURE_ACCEPTANCE: LeaseType.FEATURE_WRITER,
    WorkflowType.MILESTONE_INTEGRATION: LeaseType.INTEGRATION_WRITER,
    WorkflowType.MILESTONE_GATE: LeaseType.MILESTONE_GATE_WRITER,
    WorkflowType.HUMAN_DECISION_RESOLUTION: LeaseType.RECOVERY_WRITER,
    WorkflowType.RECOVERY: LeaseType.RECOVERY_WRITER,
}


class CommandCategory(str, Enum):
    CONFIGURED_REQUIRED_VALIDATION = "configured_required_validation"
    KERNEL_REQUIRED_FINALIZATION = "kernel_required_finalization"
    WORKFLOW_REQUIRED_EVIDENCE = "workflow_required_evidence"
    OPTIONAL_DIAGNOSTIC = "optional_diagnostic"
    STATUS_OBSERVATION = "status_observation"
    UNSUPPORTED_COMMAND = "unsupported_command"


AUTHORITATIVE_COMMAND_CATEGORIES = frozenset({
    CommandCategory.CONFIGURED_REQUIRED_VALIDATION,
    CommandCategory.KERNEL_REQUIRED_FINALIZATION,
    CommandCategory.WORKFLOW_REQUIRED_EVIDENCE,
})


class ConsistencyClassification(str, Enum):
    CONSISTENT = "CONSISTENT"
    RECOVERABLE_INCONSISTENCY = "RECOVERABLE_INCONSISTENCY"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    CORRUPT_EVIDENCE = "CORRUPT_EVIDENCE"
    UNSAFE_REPOSITORY_STATE = "UNSAFE_REPOSITORY_STATE"


WORKFLOW_CLASSIFICATIONS: dict[WorkflowType, frozenset[str]] = {
    WorkflowType.QUEUE_RECONCILIATION: frozenset({
        "RECONCILED_READY_WORK", "RECONCILED_NO_READY_WORK", "HUMAN_DECISION_REQUIRED",
        "MILESTONE_COMPLETE",
        "PLANNING_VALIDATION_FAILED", "PLANNING_SEMANTIC_CONFLICT",
        "RETRYABLE_PLANNING_FAILURE", "TERMINAL_PLANNING_FAILURE",
    }),
    WorkflowType.FEATURE_PREPARATION: frozenset({
        "FEATURE_PREPARED", "FEATURE_VALIDATION_FAILED", "HUMAN_DECISION_REQUIRED",
        "RETRYABLE_FEATURE_FAILURE", "TERMINAL_FEATURE_FAILURE",
    }),
    WorkflowType.FEATURE_EXECUTION: frozenset({
        "FEATURE_ACCEPTED", "FEATURE_REJECTED", "FEATURE_VALIDATION_FAILED",
        "HUMAN_DECISION_REQUIRED", "RETRYABLE_FEATURE_FAILURE", "TERMINAL_FEATURE_FAILURE",
    }),
    WorkflowType.FEATURE_ACCEPTANCE: frozenset({
        "FEATURE_ACCEPTED", "FEATURE_REJECTED", "FEATURE_VALIDATION_FAILED",
        "HUMAN_DECISION_REQUIRED", "RETRYABLE_FEATURE_FAILURE", "TERMINAL_FEATURE_FAILURE",
    }),
    WorkflowType.MILESTONE_INTEGRATION: frozenset({
        "INTEGRATED", "VALIDATION_FAILED", "HUMAN_DECISION_REQUIRED", "SEMANTIC_CONFLICT",
        "RETRYABLE_INTEGRATION_FAILURE", "TERMINAL_INTEGRATION_FAILURE",
    }),
    WorkflowType.MILESTONE_GATE: frozenset({
        "MILESTONE_GATE_PASSED", "MILESTONE_GATE_FAILED", "HUMAN_DECISION_REQUIRED",
        "RETRYABLE_GATE_FAILURE", "TERMINAL_GATE_FAILURE",
    }),
    WorkflowType.HUMAN_DECISION_RESOLUTION: frozenset({
        "HUMAN_GATE_RESOLVED", "HUMAN_DECISION_REQUIRED", "TERMINAL_RESOLUTION_FAILURE",
    }),
    WorkflowType.RECOVERY: frozenset({
        "RECOVERY_APPLIED", "TRANSACTION_SUPERSEDED", "HUMAN_DECISION_REQUIRED",
        "RETRYABLE_RECOVERY_FAILURE", "TERMINAL_RECOVERY_FAILURE",
    }),
}

WORKFLOW_SUCCESS_CLASSIFICATIONS: dict[WorkflowType, frozenset[str]] = {
    WorkflowType.QUEUE_RECONCILIATION: frozenset({
        "RECONCILED_READY_WORK", "RECONCILED_NO_READY_WORK", "MILESTONE_COMPLETE",
    }),
    WorkflowType.FEATURE_PREPARATION: frozenset({"FEATURE_PREPARED"}),
    WorkflowType.FEATURE_EXECUTION: frozenset({"FEATURE_ACCEPTED"}),
    WorkflowType.FEATURE_ACCEPTANCE: frozenset({"FEATURE_ACCEPTED"}),
    WorkflowType.MILESTONE_INTEGRATION: frozenset({"INTEGRATED"}),
    WorkflowType.MILESTONE_GATE: frozenset({"MILESTONE_GATE_PASSED"}),
    WorkflowType.HUMAN_DECISION_RESOLUTION: frozenset({"HUMAN_GATE_RESOLVED"}),
    WorkflowType.RECOVERY: frozenset({"RECOVERY_APPLIED"}),
}


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def bind_human_gate(
    value: Any,
    *,
    transaction_id: str,
    project_id: str,
    repository_identity: str,
    repository_path_fingerprint: str,
    workflow_type: WorkflowType,
    approved_next_state: str,
    terminal_classification: str,
) -> dict[str, Any]:
    """Normalize an untrusted blocker into one transaction-bound gate."""

    source = dict(value) if isinstance(value, dict) else {}
    reason = source.get("reason") or source.get("decision")
    classification = (
        source.get("classification")
        or source.get("gate_classification")
        or terminal_classification
    )
    if not isinstance(reason, str) or not reason.strip():
        reason = "workflow requires an explicit human decision"
    if not isinstance(classification, str) or not classification.strip():
        classification = "HUMAN_DECISION_REQUIRED"
    bindings = {
        "transaction_id": transaction_id,
        "project_id": project_id,
        "repository_identity": repository_identity,
        "repository_path_fingerprint": repository_path_fingerprint,
        "workflow_type": workflow_type.value,
        "approved_next_state": approved_next_state,
    }
    gate_id = "gate-" + fingerprint({
        **bindings,
        "classification": classification,
        "reason": reason,
    })[:32]
    source.update({
        **bindings,
        "gate_id": gate_id,
        "classification": classification,
        "reason": reason,
    })
    return validate_human_gate(source)


def validate_human_gate(value: Any, *, next_state: str | None = None) -> dict[str, Any]:
    """Validate the minimum durable identity of a resolvable human gate."""

    if not isinstance(value, dict):
        raise TransactionError("human-decision terminal requires an exact gate object")
    gate = dict(value)
    gate_id = gate.get("gate_id")
    if not isinstance(gate_id, str) or not SAFE_IDENTIFIER.fullmatch(gate_id):
        raise TransactionError("human-decision gate requires a safe unique gate_id")
    for field in ("classification", "reason"):
        if not isinstance(gate.get(field), str) or not gate[field].strip():
            raise TransactionError(f"human-decision gate requires nonempty {field}")
    approved = gate.get("approved_next_state")
    if not isinstance(approved, str) or not approved:
        raise TransactionError("human-decision gate requires approved_next_state")
    for field in (
        "transaction_id", "project_id", "repository_identity",
        "repository_path_fingerprint", "workflow_type",
    ):
        if not isinstance(gate.get(field), str) or not gate[field]:
            raise TransactionError(f"human-decision gate requires transaction-bound {field}")
    try:
        WorkflowType(str(gate["workflow_type"]))
    except ValueError as exc:
        raise TransactionError("human-decision gate has invalid workflow_type") from exc
    return gate


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise TransactionError(f"unsafe repository-relative path: {value!r}")
    return value


@dataclass(frozen=True)
class MutationPolicy:
    allowed_paths: tuple[str, ...]
    allowed_prefixes: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    denied_prefixes: tuple[str, ...] = ()
    allow_untracked: bool = False
    require_clean_start: bool = True
    commit_subject: str | None = None

    def __post_init__(self) -> None:
        normalized = tuple(sorted(set(safe_relative_path(item) for item in self.allowed_paths)))
        if normalized != self.allowed_paths:
            raise TransactionError("allowed paths must be sorted and unique")
        prefixes = tuple(sorted(set(safe_relative_path(item.rstrip("/")) for item in self.allowed_prefixes)))
        if prefixes != self.allowed_prefixes:
            raise TransactionError("allowed prefixes must be normalized, sorted, and unique")
        denied = tuple(sorted(set(safe_relative_path(item) for item in self.denied_paths)))
        if denied != self.denied_paths:
            raise TransactionError("denied paths must be normalized, sorted, and unique")
        denied_prefixes = tuple(sorted(set(safe_relative_path(item.rstrip("/")) for item in self.denied_prefixes)))
        if denied_prefixes != self.denied_prefixes:
            raise TransactionError("denied prefixes must be normalized, sorted, and unique")

    def validate(self, paths: Iterable[str]) -> tuple[str, ...]:
        observed = tuple(sorted(set(safe_relative_path(item) for item in paths)))
        denied = sorted(
            path for path in observed
            if path in self.denied_paths
            or any(path == prefix or path.startswith(prefix + "/") for prefix in self.denied_prefixes)
        )
        if denied:
            raise TransactionError(f"denied control-plane mutation: {', '.join(denied)}")
        unauthorized = sorted(
            path for path in observed
            if path not in self.allowed_paths
            and not any(path == prefix or path.startswith(prefix + "/") for prefix in self.allowed_prefixes)
        )
        if unauthorized:
            raise TransactionError(f"unauthorized path mutation: {', '.join(unauthorized)}")
        return observed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_identity: str
    repository_path_fingerprint: str
    branch: str
    head: str
    queue_fingerprint: str | None
    tracked_diff_fingerprint: str
    untracked_fingerprint: str
    tracked_changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    git_operations: dict[str, bool]

    @property
    def clean(self) -> bool:
        return not self.tracked_changed_paths and not self.untracked_paths

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PhaseTransaction:
    schema_version: int
    transaction_id: str
    workflow_type: WorkflowType
    project_id: str
    repository_identity: str
    repository_path_fingerprint: str
    milestone: str | None
    feature_id: str | None
    run_id: str
    session_ids: list[str]
    starting_branch: str
    starting_head: str
    starting_queue_fingerprint: str | None
    starting_tracked_diff_fingerprint: str
    starting_untracked_fingerprint: str
    allowed_mutation_policy: MutationPolicy
    lease_identity: str | None = None
    current_state: TransactionState = TransactionState.CREATED
    terminal_classification: str | None = None
    terminal_reference: str | None = None
    next_project_state: str | None = None

    @classmethod
    def create(
        cls,
        *,
        workflow_type: WorkflowType,
        project_id: str,
        snapshot: RepositorySnapshot,
        milestone: str | None,
        feature_id: str | None,
        run_id: str,
        policy: MutationPolicy,
        transaction_id: str | None = None,
    ) -> "PhaseTransaction":
        for label, value in (("project_id", project_id), ("run_id", run_id)):
            if not SAFE_IDENTIFIER.fullmatch(value):
                raise TransactionError(f"invalid {label}: {value!r}")
        return cls(
            schema_version=1,
            transaction_id=transaction_id or str(uuid.uuid4()),
            workflow_type=workflow_type,
            project_id=project_id,
            repository_identity=snapshot.repository_identity,
            repository_path_fingerprint=snapshot.repository_path_fingerprint,
            milestone=milestone,
            feature_id=feature_id,
            run_id=run_id,
            session_ids=[],
            starting_branch=snapshot.branch,
            starting_head=snapshot.head,
            starting_queue_fingerprint=snapshot.queue_fingerprint,
            starting_tracked_diff_fingerprint=snapshot.tracked_diff_fingerprint,
            starting_untracked_fingerprint=snapshot.untracked_fingerprint,
            allowed_mutation_policy=policy,
        )

    def transition(self, target: TransactionState) -> None:
        if target == self.current_state:
            return
        if target not in ALLOWED_TRANSITIONS[self.current_state]:
            raise TransactionError(f"invalid transaction transition: {self.current_state.value} -> {target.value}")
        if self.current_state in TERMINAL_STATES:
            raise TransactionError("completed or terminal transaction is immutable")
        self.current_state = target

    def terminate(self, state: TransactionState, classification: str, reference: str | None = None) -> None:
        if state not in TERMINAL_STATES:
            raise TransactionError("terminal outcome requires a terminal state")
        if self.terminal_classification is not None or self.current_state in TERMINAL_STATES:
            raise TransactionError("transaction already has a terminal outcome")
        self.transition(state)
        self.terminal_classification = classification
        self.terminal_reference = reference

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["workflow_type"] = self.workflow_type.value
        value["current_state"] = self.current_state.value
        return value


@dataclass(frozen=True)
class SessionResultEnvelope:
    schema_version: int
    workflow_type: WorkflowType
    classification: str
    project_id: str
    repository_identity: str
    transaction_id: str
    run_id: str
    session_id: str
    starting_branch: str
    starting_commit: str
    current_commit: str
    feature_id: str | None
    changed_paths: tuple[str, ...]
    evidence: dict[str, Any]
    next_state: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionResultEnvelope":
        required = {
            "schema_version", "workflow_type", "classification", "project_id", "repository_identity",
            "transaction_id", "run_id", "session_id", "starting_branch", "starting_commit",
            "current_commit", "changed_paths", "evidence", "next_state",
        }
        missing = sorted(required - set(value))
        if missing:
            raise SchemaValidationError(f"session-result envelope missing: {', '.join(missing)}")
        try:
            workflow = WorkflowType(value["workflow_type"])
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError("session-result workflow_type is invalid") from exc
        if value["schema_version"] != 1:
            raise SchemaValidationError("session-result schema_version must be 1")
        classification = value["classification"]
        if classification not in WORKFLOW_CLASSIFICATIONS[workflow]:
            raise SchemaValidationError(
                f"classification {classification!r} is not valid for {workflow.value}"
            )
        changed = value["changed_paths"]
        if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
            raise SchemaValidationError("session-result changed_paths must be a string array")
        normalized = tuple(sorted(set(safe_relative_path(item) for item in changed)))
        if tuple(changed) != normalized:
            raise SchemaValidationError("session-result changed_paths must be sorted and unique")
        evidence = value["evidence"]
        if not isinstance(evidence, dict):
            raise SchemaValidationError("session-result evidence must be an object")
        if workflow == WorkflowType.QUEUE_RECONCILIATION:
            queue_validation = evidence.get("queue_validation")
            if not isinstance(queue_validation, dict):
                raise SchemaValidationError(
                    "queue-reconciliation evidence.queue_validation must be an object"
                )
            try:
                normalize_warning_evidence(
                    queue_validation,
                    source="structured",
                )
            except WarningEvidenceError as exc:
                raise SchemaValidationError(str(exc)) from exc
        strings = (
            "project_id", "repository_identity", "transaction_id", "run_id", "session_id",
            "starting_branch", "starting_commit", "current_commit", "next_state",
        )
        if any(not isinstance(value.get(key), str) or not value[key] for key in strings):
            raise SchemaValidationError("session-result identity fields must be non-empty strings")
        feature = value.get("feature_id")
        if feature is not None and (not isinstance(feature, str) or not feature):
            raise SchemaValidationError("session-result feature_id must be null or a non-empty string")
        if workflow in {
            WorkflowType.FEATURE_PREPARATION,
            WorkflowType.FEATURE_EXECUTION,
            WorkflowType.FEATURE_ACCEPTANCE,
            WorkflowType.MILESTONE_INTEGRATION,
        } and not feature:
            raise SchemaValidationError(
                f"session-result feature_id is required for {workflow.value}"
            )
        return cls(
            schema_version=1,
            workflow_type=workflow,
            classification=classification,
            project_id=value["project_id"],
            repository_identity=value["repository_identity"],
            transaction_id=value["transaction_id"],
            run_id=value["run_id"],
            session_id=value["session_id"],
            starting_branch=value["starting_branch"],
            starting_commit=value["starting_commit"],
            current_commit=value["current_commit"],
            feature_id=feature,
            changed_paths=normalized,
            evidence=evidence,
            next_state=value["next_state"],
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["workflow_type"] = self.workflow_type.value
        value["changed_paths"] = list(self.changed_paths)
        return value


TERMINAL_ENVELOPE_MARKER = "CONVEYOR_TRANSACTION_RESULT="


def extract_terminal_envelope(output: str) -> SessionResultEnvelope:
    """Accept one envelope only from the final assistant result in a JSON event stream."""

    assistant_messages: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            assistant_messages.append(item["text"])
    if not assistant_messages:
        raise SchemaValidationError("terminal assistant result is absent")
    terminal_lines = assistant_messages[-1].splitlines()
    markers = [
        line[len(TERMINAL_ENVELOPE_MARKER):]
        for line in terminal_lines
        if line.startswith(TERMINAL_ENVELOPE_MARKER)
    ]
    if len(markers) != 1:
        raise SchemaValidationError("terminal assistant result must contain exactly one envelope marker")
    final_nonblank = next((line for line in reversed(terminal_lines) if line.strip()), "")
    if not final_nonblank.startswith(TERMINAL_ENVELOPE_MARKER):
        raise SchemaValidationError("terminal session-result envelope marker must be the final nonblank line")
    try:
        value = json.loads(markers[0])
    except json.JSONDecodeError as exc:
        raise SchemaValidationError("terminal session-result envelope is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SchemaValidationError("terminal session-result envelope must be an object")
    return SessionResultEnvelope.from_dict(value)


def path_fingerprint(path: Path) -> str:
    return hashlib.sha256(str(path.expanduser().resolve()).encode("utf-8")).hexdigest()
