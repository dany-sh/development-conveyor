"""Fail-closed, Conveyor-scoped capability isolation for Codex sessions."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import SessionError
from .redaction import redact_text
from .validation import SafetyPolicy


CAPABILITY_POLICY_VERSION = 1
CORE_CAPABILITIES = ("core:apply_patch", "core:shell")
WORKFLOW_SKILLS: dict[str, tuple[str, ...]] = {
    "queue_reconciliation": ("development-conveyor", "feature-inventory"),
    "scope_features": ("development-conveyor", "feature-inventory"),
    "reconcile_product_plan": ("development-conveyor", "feature-inventory"),
    "feature_cycle": ("development-conveyor", "feature-factory"),
    "milestone_integration": ("development-conveyor", "milestone-integrator"),
    "milestone_gate": ("development-conveyor", "milestone-gate"),
    "architecture": ("development-conveyor", "architecture-audit"),
    "portfolio": ("development-conveyor", "portfolio-status"),
    "application_onboarding": ("development-conveyor", "app-bootstrap"),
    "human_decision_report": ("development-conveyor",),
}
MACOS_SKILL_PREFIX = "build-macos-apps:"
DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "enable_mcp_apps",
    "goals",
    "image_generation",
    "in_app_browser",
    "memories",
    "plugin_sharing",
    "realtime_conversation",
)
AUTHENTICATION_MARKERS = (
    "authentication required",
    "oauth",
    "sign in to",
    "login required",
)
SKILL_TRUNCATION_MARKERS = (
    "skill description",
    "skill context",
    "truncat",
    "context budget",
)
_SKILL_LINE = re.compile(
    r"^- (?P<name>[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?): "
    r".* \(file: (?P<path>/[^)\r\n]+/SKILL\.md)\)$"
)


@dataclass(frozen=True)
class SkillCapability:
    name: str
    path: str


@dataclass(frozen=True)
class CapabilityPlan:
    requested_allowlist: tuple[str, ...]
    effective_allowlist: tuple[str, ...]
    config_args: tuple[str, ...]
    enforcement_mechanism: str
    isolation_supported: bool
    classification: str
    unsupported_requested: tuple[str, ...]
    unrelated_capabilities: tuple[str, ...]
    disabled_plugins: tuple[str, ...]
    disabled_mcp_servers: tuple[str, ...]
    skill_context_truncation_warnings: tuple[str, ...]
    unrelated_mcp_authentication_attempts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_POLICY_VERSION,
            "requested_capability_allowlist": list(self.requested_allowlist),
            "effective_capability_allowlist": list(self.effective_allowlist),
            "capability_enforcement_mechanism": self.enforcement_mechanism,
            "capability_isolation_supported": self.isolation_supported,
            "classification": self.classification,
            "unsupported_requested_capabilities": list(self.unsupported_requested),
            "unrelated_capabilities_detected": list(self.unrelated_capabilities),
            "disabled_plugins": list(self.disabled_plugins),
            "disabled_mcp_servers": list(self.disabled_mcp_servers),
            "skill_context_truncation_warnings": list(
                self.skill_context_truncation_warnings
            ),
            "unrelated_mcp_authentication_attempts": list(
                self.unrelated_mcp_authentication_attempts
            ),
        }


def requested_capability_allowlist(
    action: str,
    *,
    relevant_macos_skills: Iterable[str] = (),
) -> tuple[str, ...]:
    skills = WORKFLOW_SKILLS.get(action, ("development-conveyor",))
    macos = tuple(dict.fromkeys(relevant_macos_skills))
    invalid = sorted(
        item for item in macos
        if not isinstance(item, str) or not item.startswith(MACOS_SKILL_PREFIX)
    )
    if invalid:
        raise SessionError(
            "Conveyor capability allowlist contains unsupported macOS skills: "
            + ", ".join(invalid)
        )
    return tuple(dict.fromkeys((*CORE_CAPABILITIES, *skills, *macos)))


def _codex_home(environment: dict[str, str]) -> Path:
    configured = environment.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path(environment.get("HOME") or Path.home()) / ".codex").resolve()
    )


def _config_document(environment: dict[str, str]) -> dict[str, Any]:
    path = _codex_home(environment) / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    plugins: dict[str, dict[str, Any]] = {}
    mcp_servers: dict[str, dict[str, Any]] = {}
    for line in lines:
        section = line.strip()
        plugin = re.fullmatch(r'\[plugins\."([^"]+)"\]', section)
        if plugin:
            plugins[plugin.group(1)] = {}
            continue
        mcp = re.fullmatch(r'\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\]', section)
        if mcp:
            mcp_servers[mcp.group(1) or mcp.group(2)] = {}
    return {"plugins": plugins, "mcp_servers": mcp_servers}


def _run_prompt_probe(
    executable: str,
    cwd: Path,
    config_args: tuple[str, ...],
) -> tuple[tuple[SkillCapability, ...], tuple[str, ...], tuple[str, ...]]:
    argv = [
        executable,
        *config_args,
        "debug",
        "prompt-input",
        "development-conveyor-capability-probe",
    ]
    SafetyPolicy.validate_controller_command(
        argv, cwd=cwd, registered_repository=cwd, allow_codex=True
    )
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionError(
            "capability_isolation_unsupported: prompt-input capability probe failed: "
            + redact_text(str(exc))
        ) from exc
    combined = "\n".join(item for item in (result.stdout, result.stderr) if item)
    if result.returncode != 0:
        raise SessionError(
            "capability_isolation_unsupported: prompt-input capability probe failed: "
            + redact_text(combined[-1000:] or "no diagnostic")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SessionError(
            "capability_isolation_unsupported: prompt-input probe did not return JSON"
        ) from exc
    if not isinstance(payload, list):
        raise SessionError(
            "capability_isolation_unsupported: prompt-input probe root is not a list"
        )
    visible_text = "\n".join(
        str(content.get("text") or "")
        for message in payload
        if isinstance(message, dict)
        for content in message.get("content", [])
        if isinstance(content, dict) and isinstance(content.get("text"), str)
    )
    skills = tuple(
        SkillCapability(match.group("name"), match.group("path"))
        for line in visible_text.splitlines()
        if (match := _SKILL_LINE.fullmatch(line))
    )
    diagnostics = result.stderr
    warnings = tuple(
        line.strip()
        for line in diagnostics.splitlines()
        if any(marker in line.lower() for marker in SKILL_TRUNCATION_MARKERS)
    )
    authentication = tuple(
        line.strip()
        for line in diagnostics.splitlines()
        if any(marker in line.lower() for marker in AUTHENTICATION_MARKERS)
    )
    return skills, warnings, authentication


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _skill_disable_argument(skills: Iterable[SkillCapability]) -> tuple[str, ...]:
    entries = ",".join(
        "{path=" + _toml_string(item.path) + ",enabled=false}"
        for item in skills
    )
    return ("-c", f"skills.config=[{entries}]")


def _plugin_for_skill(plugin_id: str, skill: SkillCapability) -> bool:
    short = plugin_id.split("@", 1)[0]
    return (
        skill.name == short
        or skill.name.startswith(short + ":")
        or f"/{short}/" in skill.path
    )


def build_capability_plan(
    *,
    executable: str,
    cwd: Path,
    action: str,
    relevant_macos_skills: Iterable[str] = (),
    environment: dict[str, str] | None = None,
) -> CapabilityPlan:
    """Build and prove an exact session-only capability allowlist."""

    env = dict(os.environ if environment is None else environment)
    requested = requested_capability_allowlist(
        action, relevant_macos_skills=relevant_macos_skills
    )
    requested_skills = frozenset(
        item for item in requested if item not in CORE_CAPABILITIES
    )
    baseline, baseline_warnings, baseline_auth = _run_prompt_probe(
        executable, cwd, ()
    )
    available = {item.name for item in baseline}
    missing = tuple(sorted(requested_skills - available))
    if missing:
        return CapabilityPlan(
            requested,
            CORE_CAPABILITIES,
            (),
            "codex_cli_session_config_and_prompt_input_probe",
            False,
            "capability_isolation_unsupported",
            missing,
            tuple(sorted(available - requested_skills)),
            (),
            (),
            baseline_warnings,
            baseline_auth,
        )

    disabled_skills = tuple(
        item for item in baseline if item.name not in requested_skills
    )
    config = _config_document(env)
    plugins = config.get("plugins")
    plugin_ids = sorted(plugins) if isinstance(plugins, dict) else []
    allowed_skill_records = tuple(
        item for item in baseline if item.name in requested_skills
    )
    disabled_plugins = tuple(
        plugin_id
        for plugin_id in plugin_ids
        if not any(_plugin_for_skill(plugin_id, item) for item in allowed_skill_records)
    )
    mcp = config.get("mcp_servers")
    disabled_mcp = tuple(sorted(mcp)) if isinstance(mcp, dict) else ()

    args: list[str] = []
    if disabled_skills:
        args.extend(_skill_disable_argument(disabled_skills))
    for plugin_id in disabled_plugins:
        args.extend(("-c", f'plugins.{_toml_string(plugin_id)}.enabled=false'))
    if disabled_mcp:
        # Replacing the session map is the pinned CLI's supported exact
        # isolation mechanism. Per-entry ``enabled=false`` overrides discard
        # required transport fields during layered config decoding.
        args.extend(("-c", "mcp_servers={}"))
    for feature in DISABLED_FEATURES:
        args.extend(("--disable", feature))
    exact_args = tuple(args)
    effective_skills, warnings, authentication = _run_prompt_probe(
        executable, cwd, exact_args
    )
    effective_names = frozenset(item.name for item in effective_skills)
    unrelated = tuple(sorted(effective_names - requested_skills))
    unsupported = tuple(sorted(requested_skills - effective_names))
    supported = not unrelated and not unsupported and not authentication
    effective = tuple(
        dict.fromkeys(
            (*CORE_CAPABILITIES, *(item.name for item in effective_skills))
        )
    )
    return CapabilityPlan(
        requested,
        effective,
        exact_args if supported else (),
        "codex_cli_session_config_and_prompt_input_probe",
        supported,
        "capability_isolation_enforced"
        if supported
        else "capability_isolation_unsupported",
        unsupported,
        unrelated,
        disabled_plugins,
        disabled_mcp,
        tuple(dict.fromkeys((*baseline_warnings, *warnings))),
        tuple(dict.fromkeys((*baseline_auth, *authentication))),
    )


def require_capability_isolation(plan: CapabilityPlan) -> None:
    if plan.isolation_supported:
        return
    details = []
    if plan.unsupported_requested:
        details.append(
            "missing requested=" + ",".join(plan.unsupported_requested)
        )
    if plan.unrelated_capabilities:
        details.append(
            "unrelated effective=" + ",".join(plan.unrelated_capabilities)
        )
    if plan.unrelated_mcp_authentication_attempts:
        details.append("unrelated MCP authentication attempted")
    raise SessionError(
        "capability_isolation_unsupported: "
        + ("; ".join(details) if details else "exact allowlist could not be proven")
    )
