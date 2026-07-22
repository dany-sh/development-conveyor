"""Deterministic Codex CLI, model-policy, and reasoning compatibility checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact_text


ACTION_POLICY_ROLES = {
    "queue_reconciliation": "feature-inventory-lead",
    "feature_cycle": "direct-feature-session",
    "milestone_integration": "milestone-integrator",
    "milestone_gate": "release-auditor",
    "human_decision_report": "development-conveyor",
}

STRING_ASSIGNMENT = re.compile(r'^([A-Za-z0-9_.-]+)\s*=\s*"([^"\r\n]*)"\s*$')
BOOLEAN_ASSIGNMENT = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*(true|false)\s*$", re.IGNORECASE)
VERSION = re.compile(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)")
MINIMUM_VERSION = re.compile(
    r"codex-minimum-version\s+model=(?P<model>[A-Za-z0-9_.-]+)\s+version=(?P<version>\d+\.\d+\.\d+)"
)
NEWER_CLI_ERRORS = (
    "requires a newer version of codex",
    "unknown variant `max`",
    "unknown variant 'max'",
)


@dataclass(frozen=True)
class ModelSelection:
    model: str | None
    reasoning: str | None
    source: str
    role: str
    policy_path: str
    minimum_cli_version: str | None = None
    policy_valid: bool = True
    policy_error: str | None = None
    manual_reasoning_authorization: bool = False


@dataclass(frozen=True)
class CompatibilityResult:
    classification: str
    executable: str | None
    detected_version: str | None
    required_minimum_version: str | None
    effective_model: str | None
    effective_reasoning: str | None
    policy_source: str
    policy_role: str
    compatible: bool
    diagnostic: str
    remediation: str
    validation_command: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "executable": self.executable,
            "detected_version": self.detected_version,
            "required_minimum_version": self.required_minimum_version,
            "effective_model": self.effective_model,
            "effective_reasoning": self.effective_reasoning,
            "policy_source": self.policy_source,
            "policy_role": self.policy_role,
            "compatible": self.compatible,
            "diagnostic": self.diagnostic,
            "remediation": self.remediation,
            "validation_command": self.validation_command,
        }

    def human_gate(self, project_id: str | None = None) -> dict[str, Any]:
        return {
            "reason": "Codex CLI and model policy compatibility must be verified before launch.",
            "project": project_id,
            "classification": self.classification,
            "configured_model": self.effective_model,
            "configured_reasoning": self.effective_reasoning,
            "codex_executable": self.executable,
            "detected_version": self.detected_version,
            "required_minimum_version": self.required_minimum_version,
            "remediation": self.remediation,
            "compatibility_validation_command": self.validation_command,
            "resolved": False,
        }


def _read_assignments(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = STRING_ASSIGNMENT.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
            continue
        boolean = BOOLEAN_ASSIGNMENT.match(line.strip())
        if boolean:
            values[boolean.group(1)] = boolean.group(2).lower()
    return values


def _minimum_versions(policy_path: Path) -> dict[str, str]:
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {match.group("model"): match.group("version") for match in MINIMUM_VERSION.finditer(text)}


def resolve_model_selection(action: str, environment: dict[str, str] | None = None) -> ModelSelection:
    """Resolve the role-pinned selection declared by the global model policy."""

    env = dict(os.environ if environment is None else environment)
    role = ACTION_POLICY_ROLES.get(action, "development-conveyor")
    codex_home = Path(env.get("CODEX_HOME") or (Path(env.get("HOME", str(Path.home()))) / ".codex")).expanduser()
    policy_path = codex_home / "MODEL_POLICY.md"
    agent_path = codex_home / "agents" / f"{role}.toml"
    if not policy_path.is_file():
        return ModelSelection(
            None, None, "agent_file", role, str(policy_path), policy_valid=False,
            policy_error=f"authoritative global model policy is missing: {policy_path}",
        )
    try:
        values = _read_assignments(agent_path)
    except OSError as exc:
        return ModelSelection(
            None, None, "agent_file", role, str(policy_path), policy_valid=False,
            policy_error=f"cannot read role policy {agent_path}: {exc}",
        )
    model = values.get("model")
    reasoning = values.get("model_reasoning_effort")
    if not model or not reasoning:
        return ModelSelection(
            model, reasoning, "agent_file", role, str(policy_path), policy_valid=False,
            policy_error=f"role policy {agent_path} does not declare model and model_reasoning_effort",
        )
    manual = values.get("manual_reasoning_authorization") == "true"
    if reasoning in {"max", "ultra"} and not manual:
        return ModelSelection(
            model, reasoning, "agent_file", role, str(policy_path),
            minimum_cli_version=_minimum_versions(policy_path).get(model),
            policy_valid=False,
            policy_error=f"reasoning effort {reasoning} requires explicit human policy authorization",
        )
    return ModelSelection(
        model,
        reasoning,
        "agent_file",
        role,
        str(policy_path),
        minimum_cli_version=_minimum_versions(policy_path).get(model),
        manual_reasoning_authorization=manual,
    )


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    match = VERSION.search(value or "")
    return tuple(int(match.group(index)) for index in range(1, 4)) if match else None


def _resolved_executable(configured: str, environment: dict[str, str]) -> str | None:
    candidate = shutil.which(configured, path=environment.get("PATH"))
    if not candidate:
        return None
    return str(Path(candidate).resolve())


def _run(argv: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )


def check_compatibility(
    configured_executable: str,
    selection: ModelSelection,
    *,
    environment: dict[str, str] | None = None,
    project_id: str | None = None,
) -> CompatibilityResult:
    """Check the exact executable, version, catalog model, and reasoning pair."""

    env = dict(os.environ if environment is None else environment)
    validation_command = "scripts/conveyor doctor" + (f" --project {project_id}" if project_id else "")
    executable = _resolved_executable(configured_executable, env)
    common = {
        "required_minimum_version": selection.minimum_cli_version,
        "effective_model": selection.model,
        "effective_reasoning": selection.reasoning,
        "policy_source": selection.source,
        "policy_role": selection.role,
        "validation_command": validation_command,
    }
    if executable is None:
        return CompatibilityResult(
            "cli_missing", None, None, compatible=False,
            diagnostic=f"configured Codex executable {configured_executable!r} could not be resolved",
            remediation="Install or configure the required Codex CLI, then rerun compatibility validation.",
            **common,
        )
    if not selection.policy_valid or not selection.model or not selection.reasoning:
        return CompatibilityResult(
            "model_policy_invalid", executable, None, compatible=False,
            diagnostic=selection.policy_error or "model policy is incomplete",
            remediation="Correct the authoritative global model policy; do not substitute a fallback model.",
            **common,
        )
    try:
        version_result = _run([executable, "--version"], env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CompatibilityResult(
            "cli_missing", executable, None, compatible=False,
            diagnostic=f"Codex version probe failed: {redact_text(str(exc))}",
            remediation="Repair or reinstall the configured Codex executable, then rerun compatibility validation.",
            **common,
        )
    version_text = (version_result.stdout or version_result.stderr).strip()
    detected_tuple = _version_tuple(version_text)
    detected_version = ".".join(str(item) for item in detected_tuple) if detected_tuple else None
    if version_result.returncode != 0 or detected_tuple is None:
        return CompatibilityResult(
            "compatibility_unknown", executable, detected_version, compatible=False,
            diagnostic=f"Codex version probe did not produce a supported semantic version: {version_text or 'no output'}",
            remediation="Repair or upgrade Codex until `codex --version` returns a parseable version, then rerun validation.",
            **common,
        )
    required_tuple = _version_tuple(selection.minimum_cli_version)
    if required_tuple is not None and detected_tuple < required_tuple:
        return CompatibilityResult(
            "cli_version_too_old", executable, detected_version, compatible=False,
            diagnostic=f"Codex {detected_version} is below the policy minimum {selection.minimum_cli_version}",
            remediation=f"Upgrade the configured Codex CLI to {selection.minimum_cli_version} or newer; do not downgrade the model or reasoning.",
            **common,
        )
    try:
        catalog = _run([executable, "debug", "models"], env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CompatibilityResult(
            "compatibility_unknown", executable, detected_version, compatible=False,
            diagnostic=f"Codex model-catalog probe failed: {redact_text(str(exc))}",
            remediation="Repair the Codex model-catalog probe, then rerun compatibility validation.",
            **common,
        )
    if catalog.returncode != 0:
        message = redact_text((catalog.stderr or catalog.stdout).strip())
        classification = "cli_version_too_old" if any(item in message.lower() for item in NEWER_CLI_ERRORS) else "compatibility_unknown"
        remediation = (
            "Upgrade the configured Codex CLI to a version that can load the current model catalog, then rerun validation."
            if classification == "cli_version_too_old"
            else "Repair the Codex model-catalog probe, then rerun compatibility validation."
        )
        return CompatibilityResult(
            classification, executable, detected_version, compatible=False,
            diagnostic=message or "Codex model-catalog probe failed",
            remediation=remediation,
            **common,
        )
    try:
        value = json.loads(catalog.stdout)
        models = value["models"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return CompatibilityResult(
            "compatibility_unknown", executable, detected_version, compatible=False,
            diagnostic="Codex model catalog was not valid JSON with a models array",
            remediation="Upgrade or repair Codex until `codex debug models` returns a valid catalog, then rerun validation.",
            **common,
        )
    match = next((item for item in models if isinstance(item, dict) and item.get("slug") == selection.model), None)
    if match is None:
        return CompatibilityResult(
            "unsupported_model", executable, detected_version, compatible=False,
            diagnostic=f"installed Codex catalog does not expose configured model {selection.model}",
            remediation=f"Upgrade Codex until {selection.model} is supported, or obtain explicit model-policy authorization for a fallback.",
            **common,
        )
    supported = {
        item.get("effort")
        for item in match.get("supported_reasoning_levels", [])
        if isinstance(item, dict) and isinstance(item.get("effort"), str)
    }
    if selection.reasoning not in supported:
        return CompatibilityResult(
            "unsupported_reasoning_effort", executable, detected_version, compatible=False,
            diagnostic=f"model {selection.model} does not support configured reasoning effort {selection.reasoning}",
            remediation="Upgrade Codex until the policy-selected reasoning effort is supported; do not silently lower it.",
            **common,
        )
    return CompatibilityResult(
        "compatible", executable, detected_version, compatible=True,
        diagnostic="exact Codex executable, model, and reasoning policy are compatible",
        remediation="No remediation required.",
        **common,
    )
