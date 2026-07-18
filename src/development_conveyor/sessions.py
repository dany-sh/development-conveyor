"""Repository-scoped Codex session construction, launch, capture, and resume."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compatibility import CompatibilityResult, check_compatibility, resolve_model_selection
from .errors import SessionError
from .redaction import redact_text
from .registry import Project
from .validation import SafetyPolicy

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


def parse_retry_contract(output: str) -> tuple[dict[str, Any] | None, str]:
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
    final_line = terminal.splitlines()[-1] if terminal else ""
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

    def _render_prompt(self, request: SessionRequest) -> str:
        try:
            filename = ACTION_PROMPTS[request.action]
        except KeyError as exc:
            raise SessionError(f"unknown session action: {request.action}") from exc
        template = (self.controller_root / "prompts" / filename).read_text(encoding="utf-8")
        prompt = template.format(
            repository=request.project.repository,
            project_id=request.project.project_id,
            milestone=request.project.active_milestone or "UNRESOLVED",
            milestone_branch=request.project.milestone_branch or "UNRESOLVED",
            feature=request.feature or "NONE",
            run_id=request.run_id,
            mode=request.mode,
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
                "justified by evidence, end the terminal assistant message with exactly one line: "
                "`CONVEYOR_RETRY={\"schema_version\":1,\"retryable\":true,"
                "\"failure_classification\":\"...\",\"hypothesis\":\"...\","
                "\"remediation_action\":\"...\",\"supporting_evidence\":\"...\"}`. "
                "The classification must be one of the controller's documented repository-scoped validation, "
                "build, test, lint, packaging, review, structured-output, or session-execution causes. "
                "Otherwise emit no retry marker and stop at the precise failure or human gate.\n"
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

    def launch(self, request: SessionRequest) -> SessionResult:
        plan = self.plan(request)
        environment = dict(os.environ)
        environment.update({
            "CONVEYOR_RUN_ID": request.run_id,
            "CONVEYOR_PROJECT_ID": request.project.project_id,
            "CONVEYOR_MODE": request.mode,
            "CONVEYOR_FEATURE": request.feature or "",
        })
        try:
            result = subprocess.run(
                list(plan.argv), cwd=plan.cwd, env=environment, input=plan.prompt, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=int(self.configuration["codex"]["session_timeout_seconds"]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = redact_text(str(exc.stdout or ""))
            stderr = redact_text(str(exc.stderr or ""))
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
        validation = "not_required"
        classification = None
        if request.action == "queue_reconciliation":
            structured, validation = parse_reconciliation_result(result.stdout)
            classification = classify_session_result(result.returncode, structured, validation)
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
        if failure["retryable"]:
            exit_classification = "retryable_failure"
        elif failure["classification"]:
            exit_classification = failure["classification"]
        else:
            exit_classification = (
                classify_exit_contract(result.returncode, structured, validation, classification)
                if classification is not None else
                ("structured_result_successfully_returned" if result.returncode == 0 else "agent_or_skill_execution_failure")
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
