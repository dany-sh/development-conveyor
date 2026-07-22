"""Repository-scoped Codex session construction, launch, capture, and resume."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .compatibility import CompatibilityResult, check_compatibility, resolve_model_selection
from .errors import SessionError
from .redaction import redact_text
from .registry import Project
from .repository import RepositoryInspector
from .queue import FeatureQueue
from .validation import SafetyPolicy
from .contracts import TERMINAL_ENVELOPE_MARKER, extract_terminal_envelope

ACTION_PROMPTS = {
    "queue_reconciliation": "queue-reconciliation.md",
    "feature_cycle": "feature-cycle.md",
    "milestone_integration": "milestone-integration.md",
    "milestone_gate": "milestone-gate.md",
    "human_decision_report": "human-decision-report.md",
}

RECONCILIATION_CLASSIFICATIONS = {
    "reconciled_ready_work",
    "reconciled_no_ready_work",
    "milestone_complete",
    "legitimately_blocked",
    "human_decision_required",
    "invalid_queue",
    "session_execution_failed",
    "structured_output_invalid",
}
RESULT_MARKER = "CONVEYOR_RESULT="
RETRY_MARKER = "CONVEYOR_RETRY="
INTEGRATION_TERMINAL_CLASSIFICATIONS = {
    "INTEGRATED",
    "VALIDATION_FAILED",
    "HUMAN_DECISION_REQUIRED",
    "SEMANTIC_CONFLICT",
    "RETRYABLE_INTEGRATION_FAILURE",
    "TERMINAL_INTEGRATION_FAILURE",
}
INTEGRATION_GATE_MARKER = "CONVEYOR_INTEGRATION_GATE="
INTEGRATION_SCRIPT = "~/.agents/skills/milestone-integrator/scripts/integrate-feature.sh"
POST_INTEGRATION_COMMAND_CATEGORIES = {
    "required_validation",
    "required_evidence_finalization",
    "optional_diagnostic",
    "status_observation",
    "unsupported_command",
}
RETRYABLE_FAILURE_CLASSIFICATIONS = {
    "build_failure",
    "implementation_validation_failure",
    "integration_validation_failure",
    "lint_failure",
    "packaging_failure",
    "review_findings",
    "session_execution_failed",
    "structured_output_invalid",
    "test_failure",
    "validation_failure",
}


@dataclass(frozen=True)
class SessionRequest:
    action: str
    project: Project
    run_id: str
    mode: str
    feature: str | None = None
    session_id: str | None = None
    repair_attempt: int | None = None
    repair_evidence: str | None = None
    repair_hypothesis: str | None = None
    remediation_action: str | None = None
    repair_supporting_evidence: str | None = None
    continuation_reason: str | None = None
    transaction_id: str | None = None
    repository_identity: str | None = None
    starting_branch: str | None = None
    starting_commit: str | None = None
    accepted_commit: str | None = None
    allowed_paths: tuple[str, ...] = ()
    session_kind: str = "parent"
    child_session_budget: int | None = None

    def __post_init__(self) -> None:
        if self.action == "milestone_integration" and self.accepted_commit == "SELF":
            raise SessionError("milestone integration requires a normalized accepted commit")
        if self.session_kind not in {"parent", "child"}:
            raise SessionError("session kind must be parent or child")


@dataclass(frozen=True)
class SessionPlan:
    argv: tuple[str, ...]
    cwd: Path
    prompt: str
    prompt_sha256: str
    sandbox: str
    effective_model: str | None = None
    effective_reasoning: str | None = None
    codex_executable: str | None = None
    compatibility: dict[str, Any] | None = None


@dataclass(frozen=True)
class SessionResult:
    action: str
    returncode: int
    session_id: str | None
    redacted_output: str
    plan: SessionPlan
    redacted_stdout: str = ""
    redacted_stderr: str = ""
    structured_result: dict[str, Any] | None = None
    structured_output_validation: str = "not_required"
    result_classification: str | None = None
    exit_classification: str | None = None
    report_path: str | None = None
    failure_classification: str | None = None
    retryable: bool = False
    primary_terminal_error: str | None = None
    secondary_diagnostics: tuple[str, ...] = ()
    retry_hypothesis: str | None = None
    remediation_action: str | None = None
    retry_evidence: str | None = None
    post_integration_commands: tuple[dict[str, Any], ...] = ()
    optional_warnings: tuple[str, ...] = ()
    transaction_envelope: dict[str, Any] | None = None
    terminal_marker_found: bool = False
    parsed_structured_result: dict[str, Any] | None = None
    structured_output_errors: tuple[str, ...] = ()
    failed_semantic_checks: tuple[str, ...] = ()


def milestone_integration_mutation_command(feature_id: str) -> str:
    """Return the exact deterministic mutation command supplied to the model."""

    return f"{INTEGRATION_SCRIPT} --root . --feature {feature_id}"


def milestone_integration_terminal_schema() -> dict[str, Any]:
    """Return the exact versioned envelope schema shown in integration prompts."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "workflow_type", "classification", "project_id",
            "repository_identity", "transaction_id", "run_id", "session_id",
            "starting_branch", "starting_commit", "current_commit", "feature_id",
            "changed_paths", "evidence", "next_state",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "workflow_type": {"const": "milestone_integration"},
            "classification": {"enum": sorted(INTEGRATION_TERMINAL_CLASSIFICATIONS)},
            "project_id": {"type": "string", "minLength": 1},
            "repository_identity": {"type": "string", "minLength": 1},
            "transaction_id": {"type": "string", "minLength": 1},
            "run_id": {"type": "string", "minLength": 1},
            "session_id": {"type": "string", "minLength": 1},
            "starting_branch": {"type": "string", "minLength": 1},
            "starting_commit": {"type": "string", "minLength": 1},
            "current_commit": {"type": "string", "minLength": 1},
            "feature_id": {"type": "string", "minLength": 1},
            "changed_paths": {
                "type": "array", "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "evidence": {
                "type": "object",
                "required": [
                    "accepted_commit", "integration_command", "runtime_record",
                    "validation_passed", "already_integrated", "patch_equivalent_commit",
                ],
                "properties": {
                    "accepted_commit": {"type": "string", "minLength": 1},
                    "integration_command": {"type": "string", "minLength": 1},
                    "runtime_record": {"const": ".factory/runtime/milestone-integration/latest.json"},
                    "validation_passed": {"type": "boolean"},
                    "already_integrated": {"type": "boolean"},
                    "patch_equivalent_commit": {"type": ["string", "null"]},
                },
            },
            "next_state": {
                "enum": ["feature_integrated", "validation_failed", "human_decision_required", "integration_ready"]
            },
        },
    }


def milestone_integration_terminal_example(request: "SessionRequest") -> dict[str, Any]:
    """Build a valid new-integration example bound to the current request."""

    return {
        "schema_version": 1,
        "workflow_type": "milestone_integration",
        "classification": "INTEGRATED",
        "project_id": request.project.project_id,
        "repository_identity": request.repository_identity or "EXACT_REPOSITORY_IDENTITY",
        "transaction_id": request.transaction_id or "EXACT_TRANSACTION_ID",
        "run_id": request.run_id,
        "session_id": request.session_id or "ACTUAL_CODEX_SESSION_ID",
        "starting_branch": request.starting_branch or request.project.milestone_branch or "EXPECTED_MILESTONE_BRANCH",
        "starting_commit": request.starting_commit or "PRE_INTEGRATION_COMMIT",
        "current_commit": "POST_INTEGRATION_VALIDATED_HEAD",
        "feature_id": request.feature,
        "changed_paths": ["PATH_CHANGED_BY_THE_INTEGRATION"],
        "evidence": {
            "accepted_commit": request.accepted_commit or "ACCEPTED_COMMIT",
            "integration_command": milestone_integration_mutation_command(request.feature or "FEATURE_ID"),
            "runtime_record": ".factory/runtime/milestone-integration/latest.json",
            "validation_passed": True,
            "already_integrated": False,
            "patch_equivalent_commit": "INTEGRATED_OR_PATCH_EQUIVALENT_COMMIT",
        },
        "next_state": "feature_integrated",
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _assistant_message(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    event_type = value.get("type")
    if event_type == "item.completed":
        item = value.get("item")
        if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message"}:
            text = item.get("text")
            return text if isinstance(text, str) else _content_text(item.get("content"))
        return None
    if event_type == "response_item":
        payload = value.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "assistant":
            return _content_text(payload.get("content"))
        return None
    if event_type == "message" and value.get("role") == "assistant":
        text = value.get("text")
        return text if isinstance(text, str) else _content_text(value.get("content"))
    return None


def _validate_reconciliation_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionError("structured result $: expected an object")
    required = {"schema_version", "classification", "summary", "next_action", "queue_validation", "retryable"}
    missing = sorted(required - set(value))
    if missing:
        raise SessionError(f"structured result $: missing keys: {', '.join(missing)}")
    if value.get("schema_version") != 1:
        raise SessionError("structured result $.schema_version: expected 1")
    classification = value.get("classification")
    if classification not in RECONCILIATION_CLASSIFICATIONS:
        raise SessionError(f"structured result $.classification: unsupported value {classification!r}")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise SessionError("structured result $.summary: expected a non-empty string")
    if not isinstance(value.get("next_action"), str) or not value["next_action"].strip():
        raise SessionError("structured result $.next_action: expected a non-empty string")
    validation = value.get("queue_validation")
    if not isinstance(validation, dict):
        raise SessionError("structured result $.queue_validation: expected an object")
    for key in ("valid", "milestone_found", "feature_count"):
        if key not in validation:
            raise SessionError(f"structured result $.queue_validation: missing key {key}")
    if not isinstance(validation["valid"], bool):
        raise SessionError("structured result $.queue_validation.valid: expected a boolean")
    if not isinstance(validation["milestone_found"], bool):
        raise SessionError("structured result $.queue_validation.milestone_found: expected a boolean")
    if not isinstance(validation["feature_count"], int) or isinstance(validation["feature_count"], bool) or validation["feature_count"] < 0:
        raise SessionError("structured result $.queue_validation.feature_count: expected a non-negative integer")
    if not isinstance(value.get("retryable"), bool):
        raise SessionError("structured result $.retryable: expected a boolean")
    human = value.get("human_decision")
    if human is not None and not isinstance(human, dict):
        raise SessionError("structured result $.human_decision: expected an object or null")
    if classification == "human_decision_required" and not isinstance(human, dict):
        raise SessionError("structured result $.human_decision: required for human_decision_required")
    return value


def parse_reconciliation_result(output: str) -> tuple[dict[str, Any] | None, str]:
    """Extract one marker from the terminal assistant message in Codex JSONL."""

    assistant_messages: list[str] = []
    for line in output.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = _assistant_message(decoded)
        if message is not None:
            assistant_messages.append(message)
    if not assistant_messages:
        return None, "missing_terminal_assistant_message"
    marker_count = sum(message.count(RESULT_MARKER) for message in assistant_messages)
    if marker_count == 0:
        return None, "missing_marker"
    if marker_count != 1:
        return None, "duplicate_marker"
    terminal = assistant_messages[-1].rstrip()
    final_line = terminal.splitlines()[-1] if terminal else ""
    if not final_line.startswith(RESULT_MARKER):
        return None, "marker_not_terminal"
    if terminal.count(RESULT_MARKER) != 1:
        return None, "duplicate_marker"
    payload = final_line.removeprefix(RESULT_MARKER).strip()
    try:
        decoded, end = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: line {exc.lineno}, column {exc.colno}: {exc.msg}"
    if payload[end:].strip():
        return None, "invalid_json: trailing content after structured result"
    try:
        return _validate_reconciliation_result(decoded), "valid"
    except SessionError as exc:
        return None, str(exc)


def parse_retry_contract(
    output: str, *, before_integration_terminal: bool = False
) -> tuple[dict[str, Any] | None, str]:
    """Parse a terminal, validated retry authorization from a production session."""

    assistant_messages: list[str] = []
    for line in output.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = _assistant_message(decoded)
        if message is not None:
            assistant_messages.append(message)
    if not assistant_messages:
        return None, "missing_terminal_assistant_message"
    terminal = assistant_messages[-1].rstrip()
    terminal_lines = terminal.splitlines()
    final_line = terminal_lines[-1] if terminal_lines else ""
    if before_integration_terminal and len(terminal_lines) >= 2:
        heading = re.compile(
            r"^\s*(?:#{1,6}\s*)?(" + "|".join(sorted(INTEGRATION_TERMINAL_CLASSIFICATIONS)) + r")\s*$"
        )
        if heading.fullmatch(final_line):
            final_line = terminal_lines[-2]
    if not final_line.startswith(RETRY_MARKER):
        return None, "missing_retry_marker"
    if sum(message.count(RETRY_MARKER) for message in assistant_messages) != 1:
        return None, "duplicate_retry_marker"
    try:
        value = json.loads(final_line.removeprefix(RETRY_MARKER).strip())
    except json.JSONDecodeError as exc:
        return None, f"invalid_retry_json: line {exc.lineno}, column {exc.colno}: {exc.msg}"
    if not isinstance(value, dict):
        return None, "retry contract must be an object"
    required = {
        "schema_version", "retryable", "failure_classification", "hypothesis",
        "remediation_action", "supporting_evidence",
    }
    if value.get("schema_version") != 1 or required - set(value):
        return None, "retry contract is missing required versioned fields"
    if value.get("retryable") is not True:
        return None, "retry contract must explicitly authorize retryable=true"
    for key in ("failure_classification", "hypothesis", "remediation_action", "supporting_evidence"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return None, f"retry contract {key} must be a non-empty string"
    if value["failure_classification"] not in RETRYABLE_FAILURE_CLASSIFICATIONS:
        return None, "retry contract failure_classification is not an allowed repository-scoped retry cause"
    return value, "retry_contract_valid"


def parse_integration_terminal_result(
    output: str, *, allow_legacy_human_gate: bool = False
) -> tuple[dict[str, Any] | None, str]:
    """Classify exactly one marker in the terminal assistant result only."""

    assistant_messages: list[str] = []
    for line in output.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = _assistant_message(decoded)
        if message is not None:
            assistant_messages.append(message)
    if not assistant_messages:
        return None, "missing_terminal_assistant_message"
    terminal = assistant_messages[-1]
    markers: list[str] = []
    pattern = re.compile(
        r"^\s*(?:#{1,6}\s*)?(" + "|".join(sorted(INTEGRATION_TERMINAL_CLASSIFICATIONS)) + r")\s*$"
    )
    terminal_lines = terminal.splitlines()
    for line in terminal_lines:
        match = pattern.fullmatch(line)
        if match:
            markers.append(match.group(1))
    if not markers:
        return None, "missing_integration_terminal_marker"
    if len(markers) != 1:
        return None, "duplicate_or_ambiguous_integration_terminal_marker"
    final_nonblank = next((line for line in reversed(terminal_lines) if line.strip()), "")
    if not pattern.fullmatch(final_nonblank) and not (
        allow_legacy_human_gate and markers[0] == "HUMAN_DECISION_REQUIRED"
    ):
        return None, "integration_terminal_marker_not_final"
    result: dict[str, Any] = {"schema_version": 1, "classification": markers[0]}
    if markers[0] == "HUMAN_DECISION_REQUIRED" and not allow_legacy_human_gate:
        descriptors = [line.removeprefix(INTEGRATION_GATE_MARKER) for line in terminal_lines if line.startswith(INTEGRATION_GATE_MARKER)]
        if len(descriptors) != 1:
            return None, "human_decision_descriptor_missing_or_duplicate"
        try:
            descriptor = json.loads(descriptors[0])
        except json.JSONDecodeError:
            return None, "human_decision_descriptor_invalid"
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("schema_version") != 1
            or not isinstance(descriptor.get("gate_classification"), str)
            or not descriptor.get("gate_classification")
            or not isinstance(descriptor.get("reason"), str)
            or not descriptor.get("reason").strip()
            or not isinstance(descriptor.get("blocker_categories"), list)
            or not descriptor.get("blocker_categories")
            or not all(isinstance(item, str) and item for item in descriptor["blocker_categories"])
            or descriptor.get("retryable") is not False
        ):
            return None, "human_decision_descriptor_invalid"
        result["human_decision"] = descriptor
    return result, "valid"


def _configured_required_commands(project: Project) -> tuple[tuple[str, ...], ...]:
    """Load only the adapter's verified command arrays; absent adapters configure none."""

    path = project.repository / project.validation_source
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ()
    commands = document.get("commands") if isinstance(document, dict) else None
    if not isinstance(commands, dict):
        return ()
    configured: list[tuple[str, ...]] = []
    for group in ("build", "test", "lint", "package", "validate"):
        values = commands.get(group, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, list) and value and all(isinstance(part, str) and part for part in value):
                configured.append(tuple(value))
    return tuple(configured)


def _runtime_integration_record(project: Project) -> dict[str, Any] | None:
    path = project.repository / ".factory/runtime/milestone-integration/latest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_milestone_integration_result(
    value: dict[str, Any], request: SessionRequest, *, command_observed: bool
) -> tuple[str, ...]:
    """Validate an INTEGRATED claim against current Git, queue, and runtime evidence."""

    if value.get("classification") != "INTEGRATED":
        return ()
    failures: list[str] = []
    inspector = RepositoryInspector(request.project.repository)
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    expected_command = milestone_integration_mutation_command(request.feature or "FEATURE_ID")
    identity_checks = {
        "project_id_matches_execution_plan": value.get("project_id") == request.project.project_id,
        "repository_identity_matches_execution_plan": value.get("repository_identity") == request.repository_identity,
        "transaction_id_matches_execution_plan": value.get("transaction_id") == request.transaction_id,
        "run_id_matches_execution_plan": value.get("run_id") == request.run_id,
        "starting_branch_matches_execution_plan": value.get("starting_branch") == request.starting_branch,
        "starting_commit_matches_execution_plan": value.get("starting_commit") == request.starting_commit,
        "feature_id_matches_execution_plan": value.get("feature_id") == request.feature,
        "accepted_commit_matches_execution_plan": evidence.get("accepted_commit") == request.accepted_commit,
        "integration_command_matches_execution_plan": evidence.get("integration_command") == expected_command,
        "integration_command_observed": command_observed,
        "next_state_is_feature_integrated": value.get("next_state") == "feature_integrated",
    }
    failures.extend(name for name, passed in identity_checks.items() if not passed)

    current_commit = value.get("current_commit")
    starting_commit = request.starting_commit
    already_integrated = evidence.get("already_integrated") is True
    new_integration = not already_integrated
    changed_paths = value.get("changed_paths") if isinstance(value.get("changed_paths"), list) else []
    if current_commit != inspector.head:
        failures.append("current_commit_matches_repository_head")
    if inspector.current_branch != request.starting_branch:
        failures.append("repository_on_expected_milestone_branch")
    if new_integration and current_commit == starting_commit:
        failures.append("milestone_branch_advanced_for_new_integration")
    if new_integration and not changed_paths:
        failures.append("changed_paths_nonempty_for_new_integration")
    if inspector.tracked_changed_paths() or inspector.untracked_file_hashes():
        failures.append("repository_clean")
    if any(inspector.git_operation_state().values()):
        failures.append("no_unfinished_git_operation")

    accepted = request.accepted_commit
    equivalent = evidence.get("patch_equivalent_commit")
    accepted_present = bool(
        isinstance(accepted, str)
        and inspector.ref_exists(accepted)
        and inspector.is_ancestor(accepted, request.project.milestone_branch or "")
    )
    equivalent_present = bool(
        isinstance(accepted, str)
        and isinstance(equivalent, str)
        and inspector.ref_exists(accepted)
        and inspector.ref_exists(equivalent)
        and inspector.is_ancestor(equivalent, request.project.milestone_branch or "")
        and inspector.patch_fingerprint(accepted) == inspector.patch_fingerprint(equivalent)
    )
    if not (accepted_present or equivalent_present):
        failures.append("accepted_commit_or_patch_equivalent_in_milestone_history")

    runtime = _runtime_integration_record(request.project)
    runtime_identity = runtime.get("runtime_identity") if isinstance(runtime, dict) else None
    runtime_validation = runtime.get("validation") if isinstance(runtime, dict) else None
    runtime_commands = (
        runtime_validation.get("commands")
        if isinstance(runtime_validation, dict) and isinstance(runtime_validation.get("commands"), list)
        else []
    )
    observed_required = {
        tuple(item.get("argv") or [])
        for item in runtime_commands
        if isinstance(item, dict) and item.get("exit_code") == 0
    }
    configured_required = set(_configured_required_commands(request.project))
    runtime_checks = {
        "integration_runtime_evidence_present": isinstance(runtime, dict),
        "integration_runtime_complete": isinstance(runtime, dict) and runtime.get("phase") == "complete",
        "integration_runtime_identity_matches": isinstance(runtime_identity, dict)
        and runtime_identity.get("repository_identity") == request.repository_identity
        and runtime_identity.get("project_id") == request.project.project_id
        and runtime_identity.get("run_id") == request.run_id
        and runtime_identity.get("feature_id") == request.feature,
        "integration_runtime_accepted_commit_matches": isinstance(runtime, dict)
        and (
            runtime.get("accepted_commit") == accepted
            or (isinstance(runtime.get("plan"), dict) and runtime["plan"].get("accepted_commit") == accepted)
        ),
        "required_validation_passed": evidence.get("validation_passed") is True
        and isinstance(runtime_validation, dict)
        and runtime_validation.get("ok") is True
        and runtime_validation.get("clean_worktree") is True
        and not runtime_validation.get("blockers")
        and configured_required.issubset(observed_required),
        "owned_integration_lease_released": isinstance(runtime, dict) and runtime.get("lease_released") is True,
    }
    failures.extend(name for name, passed in runtime_checks.items() if not passed)

    try:
        queue = FeatureQueue.from_location(request.project.repository, request.project.queue_location)
        feature = queue.feature(request.feature or "")
        milestone = queue.milestone(request.project.active_milestone or "")
    except Exception:
        feature = None
        milestone = None
    if not isinstance(feature, dict) or feature.get("status") != "integrated" or feature.get("integration_status") != "passed":
        failures.append("queue_records_integrated_and_passed")
    if isinstance(feature, dict) and feature.get("accepted_commit") != accepted:
        failures.append("queue_accepted_commit_matches_execution_plan")
    if not isinstance(milestone, dict) or request.feature not in (milestone.get("integrated_features") or []):
        failures.append("milestone_records_feature_membership")
    return tuple(dict.fromkeys(failures))


def classify_post_integration_commands(
    output: str,
    *,
    required_commands: tuple[tuple[str, ...], ...] = (),
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Classify terminal-session command events without promoting diagnostics to gates."""

    observations: list[dict[str, Any]] = []
    warnings: list[str] = []
    required_fragments = tuple(" ".join(command) for command in required_commands)
    for line in output.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict) or decoded.get("type") != "item.completed":
            continue
        item = decoded.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = str(item.get("command") or "")
        combined_output = str(item.get("aggregated_output") or "")
        exit_code = item.get("exit_code")
        configured_required = any(fragment and fragment in command for fragment in required_fragments)
        missing_repository_validator = bool(
            "scripts/validate_feature_inventory.py" in command
            and (
                "can't open file" in combined_output
                or "No such file or directory" in combined_output
            )
            and not any("scripts/validate_feature_inventory.py" in fragment for fragment in required_fragments)
        )
        if configured_required or "/feature-inventory/scripts/validate_inventory.py" in command:
            category = "required_validation"
            effect = "authoritative"
            diagnostic = None
        elif INTEGRATION_SCRIPT in command and " --root . --feature " in command:
            category = "required_evidence_finalization"
            effect = "authoritative"
            diagnostic = None
        elif any(
            marker in command
            for marker in (
                "integrationctl.py finalize",
                "integrationctl.py continue",
                "model_runlog.py",
            )
        ):
            category = "required_evidence_finalization"
            effect = "authoritative"
            diagnostic = None
        elif missing_repository_validator:
            category = "optional_diagnostic"
            effect = "warning_only"
            diagnostic = "unconfigured_repository_validator_missing"
        elif any(
            marker in command
            for marker in (
                "git status", "git rev-parse", "git log", "git diff --check",
                "git merge-base", "git worktree list", "check-ignore", "recover-integration.sh --status",
            )
        ):
            category = "status_observation"
            effect = "observation_only"
            diagnostic = None
        else:
            category = "unsupported_command"
            effect = "warning_only" if exit_code not in {0, None} else "observation_only"
            diagnostic = "unclassified_post_integration_command"
        observation = {
            "category": category,
            "command": command,
            "exit_code": exit_code,
            "status": item.get("status"),
            "effect": effect,
            "configured_required": configured_required,
            "diagnostic": diagnostic,
        }
        observations.append(observation)
        if exit_code != 0 and effect != "authoritative":
            rendered_exit = "missing" if exit_code is None else str(exit_code)
            warning = f"{category}:{diagnostic or 'non_authoritative_failure'}:exit={rendered_exit}"
            if warning not in warnings:
                warnings.append(warning)
    return tuple(observations), tuple(warnings)


def _integration_mutation_command_observed(output: str, feature_id: str) -> bool:
    expected = milestone_integration_mutation_command(feature_id)
    for line in output.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = decoded.get("item") if isinstance(decoded, dict) else None
        if (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and expected in str(item.get("command") or "")
            and item.get("exit_code") == 0
        ):
            return True
    return False


def classify_session_result(returncode: int, structured: dict[str, Any] | None, validation: str) -> str:
    if structured is not None and validation == "valid":
        return str(structured["classification"])
    if validation not in {"missing_marker", "missing_terminal_assistant_message"}:
        return "structured_output_invalid"
    return "session_execution_failed" if returncode != 0 else "structured_output_invalid"


def classify_exit_contract(
    returncode: int, structured: dict[str, Any] | None, validation: str, result_classification: str
) -> str:
    if structured is not None and validation == "valid":
        if result_classification == "reconciled_no_ready_work":
            return "successful_reconciliation_no_ready_work"
        if result_classification == "human_decision_required":
            return "human_decision_result"
        if result_classification == "invalid_queue":
            return "validation_failure"
        if result_classification in {"session_execution_failed", "structured_output_invalid"}:
            return "retryable_failure" if structured.get("retryable") else "terminal_failure"
        return "structured_result_successfully_returned"
    if returncode in {130, -2, -15}:
        return "interrupted_session"
    if validation not in {"missing_marker", "missing_terminal_assistant_message", "not_required"}:
        return "structured_output_invalid"
    if returncode != 0:
        return "agent_or_skill_execution_failure"
    return "structured_output_invalid"


def _nested_error_message(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("message", "error"):
            message = _nested_error_message(value.get(key))
            if message:
                return message
        return None
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value.strip() or None
    return _nested_error_message(decoded) or value.strip() or None


def classify_codex_failure(stdout: str, stderr: str) -> dict[str, Any]:
    """Prefer terminal structured Codex failure evidence over secondary warnings."""

    primary = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.failed":
            primary = _nested_error_message(event.get("error"))
    categories: list[str] = []
    for line in stderr.splitlines():
        lowered_line = line.lower()
        if "authrequired" in lowered_line or "www_authenticate" in lowered_line or "www-authenticate" in lowered_line:
            category = "optional_integration_authentication_required"
        elif "models cache" in lowered_line or "unknown variant `max`" in lowered_line:
            category = "model_catalog_parse_warning"
        else:
            category = "codex_stderr_warning"
        if category not in categories:
            categories.append(category)
    secondary = tuple(categories)
    lowered = (primary or "").lower()
    if "invalid_request_error" in lowered and "requires a newer version of codex" in lowered:
        return {
            "classification": "cli_upgrade_required",
            "retryable": False,
            "primary_terminal_error": primary,
            "secondary_diagnostics": secondary,
        }
    if "requires a newer version of codex" in lowered:
        return {
            "classification": "cli_upgrade_required",
            "retryable": False,
            "primary_terminal_error": primary,
            "secondary_diagnostics": secondary,
        }
    stderr_lower = stderr.lower()
    if primary is None and ("unknown variant `max`" in stderr_lower or "unknown variant 'max'" in stderr_lower):
        return {
            "classification": "cli_upgrade_required",
            "retryable": False,
            "primary_terminal_error": "installed Codex cannot parse the current model catalog reasoning variants",
            "secondary_diagnostics": secondary,
        }
    return {
        "classification": "session_execution_failed" if primary or stderr else None,
        "retryable": False,
        "primary_terminal_error": primary,
        "secondary_diagnostics": secondary,
    }


class SessionLauncher:
    def __init__(self, controller_root: Path, configuration: dict[str, Any]):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration

    def compatibility(self, action: str, *, project_id: str | None = None) -> CompatibilityResult:
        selection = resolve_model_selection(action)
        return check_compatibility(
            str(self.configuration["codex"]["executable"]),
            selection,
            project_id=project_id,
        )

    def _trusted_resolution_environment(self, request: SessionRequest) -> dict[str, str]:
        if request.action != "milestone_integration":
            return {}
        cycle_path = request.project.repository / ".factory/conveyor-state.json"
        try:
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        baseline = cycle.get("validated_planning_baseline") if isinstance(cycle, dict) else None
        resolution_id = baseline.get("approval_resolution_id") if isinstance(baseline, dict) else None
        if not isinstance(resolution_id, str) or not re.fullmatch(r"human-resolution-[0-9a-f]{24}", resolution_id):
            return {}
        report_root = Path(str(self.configuration["report_directory"]))
        if not report_root.is_absolute():
            report_root = self.controller_root / report_root
        report_root = report_root.resolve()
        report = (report_root / resolution_id / "human-decision-resolution.json").resolve()
        try:
            report.relative_to(report_root)
        except ValueError:
            return {}
        if not report.is_file():
            return {}
        return {
            "CONVEYOR_CONTROLLER_REPORT_ROOT": str(report_root),
            "CONVEYOR_PLANNING_RESOLUTION_REPORT": str(report),
        }

    @staticmethod
    def _relevant_status_context(text: str, feature_id: str, milestone_id: str) -> str:
        sections = re.split(r"(?=^##\s)", text, flags=re.MULTILINE)
        selected = [
            section for section in sections
            if section.startswith("# Current Status")
            or feature_id in section
            or milestone_id in section
        ]
        return "".join(selected).strip()

    def _focused_integration_context(self, request: SessionRequest) -> str:
        """Embed only the repository evidence needed for one integration."""

        repository = request.project.repository
        queue_path = repository / request.project.queue_location
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            queue = {}
        feature = next(
            (item for item in queue.get("features", []) if isinstance(item, dict) and item.get("id") == request.feature),
            None,
        )
        milestone = next(
            (
                item for item in queue.get("milestones", [])
                if isinstance(item, dict) and item.get("id") == request.project.active_milestone
            ),
            None,
        )
        spec_path = repository / str((feature or {}).get("spec") or "")
        sources = {
            "AGENTS.md": repository / "AGENTS.md",
            ".factory/project.yaml": repository / request.project.validation_source,
        }
        rendered: list[str] = []
        for label, path in sources.items():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = f"UNAVAILABLE: {label}"
            rendered.append(f"### {label}\n\n```text\n{content.rstrip()}\n```")
        rendered.append(
            "### Focused queue records\n\n```json\n"
            + json.dumps({"milestone": milestone, "feature": feature}, indent=2, sort_keys=True)
            + "\n```"
        )
        try:
            specification = spec_path.read_text(encoding="utf-8")
        except OSError:
            specification = "UNAVAILABLE: selected feature specification"
        rendered.append(f"### Selected feature specification\n\n```text\n{specification.rstrip()}\n```")
        status_path = repository / "docs/CURRENT_STATUS.md"
        try:
            status = self._relevant_status_context(
                status_path.read_text(encoding="utf-8"),
                request.feature or "NONE",
                request.project.active_milestone or "UNRESOLVED",
            )
        except OSError:
            status = "UNAVAILABLE: docs/CURRENT_STATUS.md"
        rendered.append(f"### Relevant current-status blocks\n\n```text\n{status.rstrip()}\n```")
        return "\n\n".join(rendered)

    def _render_prompt(self, request: SessionRequest) -> str:
        try:
            filename = ACTION_PROMPTS[request.action]
        except KeyError as exc:
            raise SessionError(f"unknown session action: {request.action}") from exc
        template = (self.controller_root / "prompts" / filename).read_text(encoding="utf-8")
        mutation_command = milestone_integration_mutation_command(request.feature or "FEATURE_ID")
        terminal_schema = json.dumps(milestone_integration_terminal_schema(), indent=2, sort_keys=True)
        terminal_example = json.dumps(milestone_integration_terminal_example(request), separators=(",", ":"))
        prompt = template.format(
            repository=request.project.repository,
            project_id=request.project.project_id,
            milestone=request.project.active_milestone or "UNRESOLVED",
            milestone_branch=request.project.milestone_branch or "UNRESOLVED",
            feature=request.feature or "NONE",
            run_id=request.run_id,
            mode=request.mode,
            transaction_id=request.transaction_id or "LEGACY_UNBOUND",
            repository_identity=request.repository_identity or "LEGACY_UNBOUND",
            starting_branch=request.starting_branch or "LEGACY_UNBOUND",
            starting_commit=request.starting_commit or "LEGACY_UNBOUND",
            accepted_commit=request.accepted_commit or "LEGACY_UNBOUND",
            allowed_paths=json.dumps(list(request.allowed_paths), separators=(",", ":")),
            session_identity=request.session_id or "ACTUAL_CODEX_SESSION_ID_FROM_THIS_LAUNCH",
            mutation_command=mutation_command,
            terminal_schema=terminal_schema,
            terminal_example=terminal_example,
            focused_context=(self._focused_integration_context(request) if request.action == "milestone_integration" else ""),
        )
        if request.repair_attempt is not None:
            prompt += (
                "\n## Focused repair continuation\n\n"
                f"This is focused repair attempt {request.repair_attempt}. The previous redacted failure evidence "
                f"fingerprint is `{request.repair_evidence}`. The authorized changed hypothesis is "
                f"`{request.repair_hypothesis}` and the materially different remediation is "
                f"`{request.remediation_action}`. Supporting evidence: `{request.repair_supporting_evidence}`. "
                "Reinspect current repository evidence, record the changed hypothesis in the repository run log, "
                "and do not repeat the failed approach. Stop if the authorized remediation is no longer justified "
                "or a human-decision condition is reached.\n"
            )
        if request.continuation_reason == "uncorroborated_completion":
            prompt += (
                "\n## Uncorroborated completion continuation\n\n"
                "The previous session exited normally, but its claimed completion was not corroborated by Git "
                "and queue evidence. The feature branch and worktree are now verified, and the controller has "
                "reacquired the matching repository writer lease before this continuation. Do not acquire a "
                "second lease and do not release the controller-owned lease. Continue this same session through "
                "implementation, repository-required validation, adversarial review, queue and documentation "
                "updates, and exactly one accepted feature commit. Run acceptance preflight with the existing "
                "agent-run identity. Do not invoke milestone integration and do not switch to the milestone "
                "branch; the controller will verify the accepted commit before integration is authorized.\n"
            )
        if request.action in {"feature_cycle", "milestone_integration"}:
            prompt += (
                "\n## Controller retry contract\n\n"
                "Do not request a retry for deterministic environment or configuration failures. If and only if "
                "the session fails with a retryable repository-scoped cause and a materially different repair is "
                "justified by evidence, emit exactly one line: "
                "`CONVEYOR_RETRY={\"schema_version\":1,\"retryable\":true,"
                "\"failure_classification\":\"...\",\"hypothesis\":\"...\","
                "\"remediation_action\":\"...\",\"supporting_evidence\":\"...\"}`. "
                "The classification must be one of the controller's documented repository-scoped validation, "
                "build, test, lint, packaging, review, structured-output, or session-execution causes. "
                + (
                    "For milestone integration, place this retry line immediately before the required final terminal heading. "
                    if request.action == "milestone_integration" else
                    "For a feature session, this retry line must be the final line. "
                )
                + "Otherwise emit no retry marker and stop at the precise failure or human gate.\n"
            )
        return prompt

    def plan(self, request: SessionRequest) -> SessionPlan:
        prompt = self._render_prompt(request)
        compatibility = self.compatibility(request.action, project_id=request.project.project_id)
        if not compatibility.compatible:
            raise SessionError(
                f"classification={compatibility.classification}; model={compatibility.effective_model}; "
                f"reasoning={compatibility.effective_reasoning}; executable={compatibility.executable}; "
                f"detected_version={compatibility.detected_version}; "
                f"required_minimum_version={compatibility.required_minimum_version}; "
                f"diagnostic={compatibility.diagnostic}; remediation={compatibility.remediation}; "
                f"validate={compatibility.validation_command}"
            )
        executable = str(compatibility.executable)
        sandbox = (
            "read-only"
            if request.action == "human_decision_report" or request.mode in {"audit", "dry-run", "dry-run-validation"}
            else "workspace-write"
        )
        policy_args = (
            "--model", str(compatibility.effective_model),
            "-c", f'model_reasoning_effort="{compatibility.effective_reasoning}"',
        )
        if request.session_id:
            argv = (executable, "exec", *policy_args, "resume", "--json", request.session_id, "-")
        else:
            argv = (
                executable, "exec", *policy_args, "--cd", str(request.project.repository),
                "--json", "--sandbox", sandbox, "-",
            )
        SafetyPolicy.validate_controller_command(
            list(argv), cwd=request.project.repository, registered_repository=request.project.repository, allow_codex=True
        )
        return SessionPlan(
            argv=argv,
            cwd=request.project.repository,
            prompt=prompt,
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            sandbox=sandbox,
            effective_model=compatibility.effective_model,
            effective_reasoning=compatibility.effective_reasoning,
            codex_executable=compatibility.executable,
            compatibility=compatibility.as_dict(),
        )

    def launch(
        self,
        request: SessionRequest,
        on_session_started: Callable[[str], None] | None = None,
    ) -> SessionResult:
        if request.session_kind == "child" and (request.child_session_budget is None or request.child_session_budget <= 0):
            raise SessionError("child session budget exhausted or prohibited before Codex launch")
        plan = self.plan(request)
        environment = dict(os.environ)
        environment.update({
            "CONVEYOR_RUN_ID": request.run_id,
            "CONVEYOR_PROJECT_ID": request.project.project_id,
            "CONVEYOR_MODE": request.mode,
            "CONVEYOR_FEATURE": request.feature or "",
            "CONVEYOR_ACCEPTED_COMMIT": request.accepted_commit or "",
        })
        environment.update(self._trusted_resolution_environment(request))
        process: subprocess.Popen[str] | None = None
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        callback_errors: list[BaseException] = []
        observed_session: list[str] = []
        observer_lock = threading.Lock()

        def consume(stream: Any, destination: list[str], observe: bool) -> None:
            try:
                for line in iter(stream.readline, ""):
                    destination.append(line)
                    if observe and on_session_started is not None:
                        session = self._session_id(line)
                        if session:
                            with observer_lock:
                                if not observed_session:
                                    observed_session.append(session)
                                    on_session_started(session)
                        elif len(destination) == 1:
                            raise SessionError(
                                "typed workflow did not expose a session identity before work began"
                            )
            except BaseException as exc:  # surfaced on the launching thread below
                callback_errors.append(exc)
                if process is not None and process.poll() is None:
                    process.kill()
            finally:
                stream.close()

        try:
            process = subprocess.Popen(
                list(plan.argv), cwd=plan.cwd, env=environment, text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise OSError("repository session pipes are unavailable")
            stdout_reader = threading.Thread(
                target=consume, args=(process.stdout, stdout_lines, True), daemon=True
            )
            stderr_reader = threading.Thread(
                target=consume, args=(process.stderr, stderr_lines, False), daemon=True
            )
            stdout_reader.start()
            stderr_reader.start()
            process.stdin.write(plan.prompt)
            process.stdin.close()
            returncode = process.wait(timeout=int(self.configuration["codex"]["session_timeout_seconds"]))
            stdout_reader.join()
            stderr_reader.join()
            if callback_errors:
                raise callback_errors[0]
            if on_session_started is not None and not observed_session:
                raise SessionError("typed workflow completed without an early session identity")
            result = subprocess.CompletedProcess(
                list(plan.argv), returncode, "".join(stdout_lines), "".join(stderr_lines)
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                process.wait()
            stdout = redact_text("".join(stdout_lines))
            stderr = redact_text("".join(stderr_lines))
            raise SessionError(
                f"repository session timed out; redacted stdout: {stdout[-1000:]}; "
                f"redacted stderr: {stderr[-1000:]}"
            ) from exc
        except OSError as exc:
            raise SessionError(f"repository session process launch failed: {exc}") from exc
        session_id = self._session_id(result.stdout + "\n" + result.stderr)
        redacted_stdout = redact_text(result.stdout)
        redacted_stderr = redact_text(result.stderr)
        combined = redacted_stdout + (("\n" + redacted_stderr) if redacted_stderr else "")
        failure = classify_codex_failure(result.stdout, result.stderr) if result.returncode != 0 else {
            "classification": None,
            "retryable": False,
            "primary_terminal_error": None,
            "secondary_diagnostics": (),
        }
        structured = None
        transaction_envelope = None
        parsed_structured_result = None
        terminal_marker_found = TERMINAL_ENVELOPE_MARKER in result.stdout
        structured_output_errors: tuple[str, ...] = ()
        failed_semantic_checks: tuple[str, ...] = ()
        validation = "not_required"
        classification = None
        if request.transaction_id and request.action in {"queue_reconciliation", "feature_cycle", "milestone_integration", "milestone_gate"}:
            try:
                envelope = extract_terminal_envelope(result.stdout)
                parsed_structured_result = envelope.to_dict()
                structured = parsed_structured_result
                if request.action == "milestone_integration":
                    failed_semantic_checks = validate_milestone_integration_result(
                        parsed_structured_result,
                        request,
                        command_observed=_integration_mutation_command_observed(
                            result.stdout, request.feature or "FEATURE_ID"
                        ),
                    )
                if failed_semantic_checks:
                    validation = "semantic_invalid"
                    classification = "structured_output_invalid"
                    structured_output_errors = tuple(
                        f"semantic check failed: {name}" for name in failed_semantic_checks
                    )
                else:
                    transaction_envelope = parsed_structured_result
                    validation = "valid"
                    classification = envelope.classification
            except Exception as exc:
                validation = "invalid"
                classification = "structured_output_invalid"
                structured_output_errors = (str(exc),)
        elif request.action == "queue_reconciliation":
            structured, validation = parse_reconciliation_result(result.stdout)
            classification = classify_session_result(result.returncode, structured, validation)
        elif request.action == "milestone_integration":
            structured, validation = parse_integration_terminal_result(result.stdout)
            if structured is not None:
                classification = str(structured["classification"])
                retry_contract = None
                if classification == "RETRYABLE_INTEGRATION_FAILURE":
                    retry_contract, retry_validation = parse_retry_contract(
                        result.stdout, before_integration_terminal=True
                    )
                    if retry_contract is not None:
                        structured.update(retry_contract)
                    else:
                        validation = retry_validation
                failure = {
                    **failure,
                    "classification": classification,
                    "retryable": bool(retry_contract),
                }
            else:
                classification = "TERMINAL_INTEGRATION_FAILURE"
                failure = {**failure, "classification": classification, "retryable": False}
        elif result.returncode != 0 and failure["classification"] not in {
            "cli_upgrade_required", "configuration_incompatible", "cli_missing",
            "cli_version_too_old", "unsupported_model", "unsupported_reasoning_effort",
            "model_policy_invalid", "compatibility_unknown",
        }:
            retry_contract, retry_validation = parse_retry_contract(result.stdout)
            if retry_contract is not None:
                structured = retry_contract
                validation = retry_validation
                classification = str(retry_contract["failure_classification"])
                failure = {
                    **failure,
                    "classification": classification,
                    "retryable": True,
                }
        if failure["classification"] == "cli_upgrade_required":
            classification = "cli_upgrade_required"
        if request.action == "milestone_integration" and structured is not None:
            exit_classification = classification
        elif failure["retryable"]:
            exit_classification = "retryable_failure"
        elif failure["classification"]:
            exit_classification = failure["classification"]
        else:
            exit_classification = (
                classify_exit_contract(result.returncode, structured, validation, classification)
                if classification is not None else
                ("structured_result_successfully_returned" if result.returncode == 0 else "agent_or_skill_execution_failure")
            )
        post_integration_commands: tuple[dict[str, Any], ...] = ()
        optional_warnings: tuple[str, ...] = ()
        if request.action == "milestone_integration":
            post_integration_commands, optional_warnings = classify_post_integration_commands(
                redacted_stdout,
                required_commands=_configured_required_commands(request.project),
            )
        return SessionResult(
            action=request.action,
            returncode=result.returncode,
            session_id=session_id or request.session_id,
            redacted_output=combined,
            plan=plan,
            redacted_stdout=redacted_stdout,
            redacted_stderr=redacted_stderr,
            structured_result=structured,
            structured_output_validation=validation,
            result_classification=classification,
            exit_classification=exit_classification,
            failure_classification=failure["classification"],
            retryable=bool(failure["retryable"]),
            primary_terminal_error=failure["primary_terminal_error"],
            secondary_diagnostics=tuple(failure["secondary_diagnostics"]),
            retry_hypothesis=(structured or {}).get("hypothesis") if failure["retryable"] else None,
            remediation_action=(structured or {}).get("remediation_action") if failure["retryable"] else None,
            retry_evidence=(structured or {}).get("supporting_evidence") if failure["retryable"] else None,
            post_integration_commands=post_integration_commands,
            optional_warnings=optional_warnings,
            transaction_envelope=transaction_envelope,
            terminal_marker_found=terminal_marker_found,
            parsed_structured_result=parsed_structured_result,
            structured_output_errors=structured_output_errors,
            failed_semantic_checks=failed_semantic_checks,
        )

    @staticmethod
    def _session_id(output: str) -> str | None:
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            for key in ("thread_id", "threadId", "session_id", "sessionId"):
                if isinstance(value.get(key), str):
                    return value[key]
            payload = value.get("payload")
            if isinstance(payload, dict):
                for key in ("thread_id", "threadId", "session_id", "sessionId"):
                    if isinstance(payload.get(key), str):
                        return payload[key]
        return None
