"""Stable machine-oriented output contracts for Conveyor-launched sessions."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable


COMPACT_OUTPUT_POLICY_VERSION = 1
TERMINAL_REPORT_FIELDS = (
    "workflow_type",
    "task",
    "selected_profile",
    "exact_model",
    "reasoning_effort",
    "policy_source",
    "parent_sessions_used",
    "child_sessions_used",
    "changed_paths",
    "validation_commands",
    "validation_result",
    "errors",
    "safety_findings",
    "blocker",
    "accepted_commit",
    "next_state",
    "recovery_instruction",
    "terminal_classification",
)
WORKFLOW_ALIASES = {
    "feature_cycle": "feature_execution",
}


def normalize_workflow_label(value: str) -> str:
    return WORKFLOW_ALIASES.get(value, value)


def validate_terminal_workflow(*, invoked: str, reported: str) -> None:
    expected = normalize_workflow_label(invoked)
    observed = normalize_workflow_label(reported)
    if observed != expected:
        raise ValueError(
            f"terminal workflow label conflicts with invoked workflow: "
            f"expected={expected}; reported={reported}"
        )


def compact_output_contract(
    *,
    workflow_type: str,
    task: str,
    selected_profile: str | None,
    exact_model: str | None,
    reasoning_effort: str | None,
    policy_source: str | None,
) -> dict[str, Any]:
    """Return the exact compact report contract embedded in every prompt."""

    canonical_workflow = normalize_workflow_label(workflow_type)
    return {
        "schema_version": COMPACT_OUTPUT_POLICY_VERSION,
        "format": "single_compact_json_object",
        "required_fields": list(TERMINAL_REPORT_FIELDS),
        "identity": {
            "workflow_type": canonical_workflow,
            "task": task,
            "selected_profile": selected_profile,
            "exact_model": exact_model,
            "reasoning_effort": reasoning_effort,
            "policy_source": policy_source,
        },
        "successful_command_summary_fields": [
            "command",
            "exit_code",
            "duration",
            "output_hash",
            "bounded_tail_or_summary",
        ],
        "suppress": [
            "repeated status narration",
            "long introductions",
            "conversational acknowledgements",
            "large copied logs",
            "full skill descriptions",
            "full Git history",
            "speculative progress",
        ],
        "preserve": [
            "errors",
            "safety findings",
            "failed commands",
            "changed paths",
            "validation failures",
            "model or capability mismatch",
            "recovery instructions",
            "terminal identity",
            "exact commit evidence",
            "explicit blockers",
        ],
        "workflow_label_policy": "exact_after_documented_alias_normalization",
    }


def render_compact_output_instructions(contract: dict[str, Any]) -> str:
    return (
        "\n## Compact terminal output contract\n\n"
        "Return one compact JSON terminal report matching this controller-owned "
        "contract. Do not replace it with prose. Successful command output must "
        "be summarized; failures, safety findings, changed paths, validation "
        "evidence, blockers, commit identity, and recovery instructions must remain.\n\n"
        "```json\n"
        + json.dumps(contract, indent=2, sort_keys=True)
        + "\n```\n"
    )


def summarize_command_result(
    command: list[str],
    *,
    runner: Callable[..., Any],
    cwd: Path,
    tail_characters: int = 1000,
) -> tuple[Any, dict[str, Any]]:
    """Run one command and return its bounded, hash-addressed summary."""

    started = time.monotonic()
    result = runner(command, cwd=cwd)
    duration = round(time.monotonic() - started, 6)
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    output = stdout + stderr
    return result, {
        "command": list(command),
        "exit_code": int(getattr(result, "returncode", 1)),
        "duration": duration,
        "output_hash": hashlib.sha256(output.encode()).hexdigest(),
        "bounded_tail_or_summary": output[-tail_characters:]
        if output
        else "no command output",
        "output_bytes": len(output.encode()),
    }
