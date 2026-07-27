"""Read-only audit of effective Conveyor runtime policy."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from .capability_policy import build_capability_plan
from .compatibility import resolve_model_selection
from .cost_policy import build_run_plan
from .execution_profiles import DEFAULT_PROFILES, DEFAULT_WORKFLOW_FALLBACKS
from .redaction import redact_text
from .repository import RepositoryInspector


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    value: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if "=" not in stripped:
            continue
        key, raw = (part.strip() for part in stripped.split("=", 1))
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            value[key] = raw[1:-1]
    return value


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _model_catalog(executable: str, cwd: Path) -> tuple[str | None, list[str], str | None]:
    version = _run([executable, "--version"], cwd=cwd)
    models = _run([executable, "debug", "models"], cwd=cwd)
    detected_version = (version.stdout or version.stderr).strip() or None
    try:
        payload = json.loads(models.stdout)
        available = sorted(
            item["slug"]
            for item in payload.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("slug"), str)
        )
    except (json.JSONDecodeError, AttributeError):
        available = []
    diagnostic = None
    if version.returncode != 0 or models.returncode != 0 or not available:
        diagnostic = redact_text(
            (models.stderr or version.stderr or "Codex runtime catalog probe failed").strip()
        )
    return detected_version, available, diagnostic


def _model_policy_audit(codex_home: Path, cwd: Path) -> dict[str, Any]:
    script = codex_home / "scripts/model_policy.py"
    if not script.is_file():
        return {
            "ok": False,
            "errors": ["model-policy validator is missing"],
            "returncode": None,
        }
    try:
        result = _run(["python3", str(script)], cwd=cwd)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "errors": [redact_text(str(exc))],
            "returncode": None,
        }
    return {
        "ok": result.returncode == 0 and payload.get("ok") is True,
        "errors": list(payload.get("errors") or []),
        "returncode": result.returncode,
        "global_default": payload.get("global_default"),
        "development_conveyor": next(
            (
                item
                for item in payload.get("agents", [])
                if isinstance(item, dict)
                and item.get("role") == "development-conveyor"
            ),
            None,
        ),
    }


class RuntimeAuditor:
    def __init__(
        self,
        *,
        controller_root: Path,
        configuration: Any,
        project: Any,
        planner: Callable[[], dict[str, Any]],
    ):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration
        self.project = project
        self.planner = planner

    def audit(self) -> dict[str, Any]:
        """Inspect runtime policy without a model, lease, transaction, or write."""

        warnings: list[dict[str, str]] = []
        blockers: list[dict[str, str]] = []
        unsupported: list[dict[str, str]] = []
        violations: list[dict[str, str]] = []
        executable = str(self.configuration.conveyor["codex"]["executable"])
        version, available_models, catalog_error = _model_catalog(
            executable, self.controller_root
        )
        if catalog_error:
            blockers.append({"code": "catalog_probe_failed", "detail": catalog_error})

        plan = self.planner()
        cost_plan = build_run_plan(
            plan,
            self.controller_root,
            project=self.project,
            profile_configuration=self.configuration.execution_profiles,
        )
        deterministic = cost_plan["selected_model"] is None
        selected_model = cost_plan["selected_model"]
        exact_available = selected_model is None or selected_model in available_models
        if not exact_available:
            violations.append(
                {
                    "code": "exact_model_unavailable",
                    "detail": f"required exact model is unavailable: {selected_model}",
                }
            )

        environment = dict(os.environ)
        codex_home = Path(
            environment.get("CODEX_HOME")
            or (Path(environment.get("HOME") or Path.home()) / ".codex")
        ).expanduser().resolve()
        global_config_path = codex_home / "config.toml"
        global_config = _read_toml(global_config_path)
        global_default = {
            "model": global_config.get("model"),
            "reasoning": global_config.get("model_reasoning_effort"),
            "source": "global_default",
        }
        development_selection = resolve_model_selection(
            "human_decision_report", environment
        )
        development_assignment = {
            "model": development_selection.model,
            "reasoning": development_selection.reasoning,
            "source": development_selection.source,
            "policy_valid": development_selection.policy_valid,
            "policy_error": development_selection.policy_error,
        }
        if global_default != {
            "model": "gpt-5.6-sol",
            "reasoning": "medium",
            "source": "global_default",
        }:
            violations.append(
                {
                    "code": "global_interactive_default_mismatch",
                    "detail": "ordinary interactive default must be gpt-5.6-sol/medium",
                }
            )
        if (
            development_assignment["model"],
            development_assignment["reasoning"],
        ) != ("gpt-5.6-terra", "medium"):
            violations.append(
                {
                    "code": "development_conveyor_assignment_mismatch",
                    "detail": "Development Conveyor must be gpt-5.6-terra/medium",
                }
            )

        model_policy = _model_policy_audit(codex_home, self.controller_root)
        if not model_policy["ok"]:
            violations.append(
                {
                    "code": "canonical_model_policy_invalid",
                    "detail": "; ".join(model_policy.get("errors") or ["validator failed"]),
                }
            )

        capability = None
        if deterministic:
            capability_report = {
                "requested_capability_allowlist": [],
                "effective_capability_allowlist": [],
                "capability_enforcement_mechanism": "deterministic_zero_model",
                "capability_isolation_supported": True,
                "classification": "deterministic_zero_model",
                "unrelated_capabilities_detected": [],
                "skill_context_truncation_warnings": [],
                "unrelated_mcp_authentication_attempts": [],
            }
        else:
            capability = build_capability_plan(
                executable=executable,
                cwd=self.project.repository,
                action=str(cost_plan["workflow_type"]),
                relevant_macos_skills=tuple(
                    cost_plan.get("relevant_macos_skills") or ()
                ),
                environment=environment,
            )
            capability_report = capability.to_dict()
            if not capability.isolation_supported:
                unsupported.append(
                    {
                        "code": "capability_isolation_unsupported",
                        "detail": "the exact requested per-session allowlist was not proven",
                    }
                )
            for item in capability.skill_context_truncation_warnings:
                warnings.append(
                    {"code": "skill_context_truncation_warning", "detail": item}
                )
            for item in capability.unrelated_mcp_authentication_attempts:
                violations.append(
                    {"code": "unrelated_mcp_authentication_attempt", "detail": item}
                )

        inspector = RepositoryInspector(self.project.repository)
        application = {
            "branch": inspector.current_branch,
            "head": inspector.head,
            "clean": not inspector.dirty_entries,
            "writer_lease_present": inspector.writer_lock_path().exists(),
            "git_operations": inspector.git_operation_state(),
        }
        if not application["clean"]:
            blockers.append(
                {
                    "code": "application_repository_dirty",
                    "detail": "runtime audit requires a clean application repository",
                }
            )

        context = cost_plan["context_pack"]
        compact_contract_present = bool(
            self.configuration.conveyor.get("runtime_policy", {}).get(
                "compact_output_required"
            )
        )
        if not compact_contract_present:
            violations.append(
                {
                    "code": "compact_output_contract_missing",
                    "detail": "runtime configuration does not require compact output",
                }
            )
        passed = not blockers and not unsupported and not violations
        return {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "codex_executable": executable,
            "codex_version": version,
            "available_exact_models": available_models,
            "required_exact_model": selected_model,
            "exact_model_available": exact_available,
            "fallback_used": False,
            "canonical_model_policy": model_policy,
            "global_ordinary_interactive_default": global_default,
            "development_conveyor_assignment": development_assignment,
            "current_workflow_route": cost_plan["workflow_type"],
            "selected_profile": cost_plan["profile"],
            "selected_model": selected_model,
            "selected_reasoning": cost_plan["selected_reasoning_effort"],
            "policy_source": cost_plan["profile_resolution_source"],
            "parent_session_budget": cost_plan["parent_session_budget"],
            "child_session_budget": cost_plan["child_session_budget"],
            "child_delegation_justification": cost_plan[
                "child_agent_justification"
            ],
            "deterministic_zero_model": deterministic,
            "deterministic_zero_child": cost_plan["child_session_budget"] == 0,
            "escalation_trigger": cost_plan["escalation_trigger"],
            "escalation_profile": cost_plan["escalation_profile"],
            **capability_report,
            "context_pack_file_count": context["file_count"],
            "context_pack_approximate_bytes": context[
                "approximate_bytes_estimate"
            ],
            "context_pack_included_paths": context["files"],
            "context_pack_included_categories": sorted(
                set(context.get("included_reasons", {}).values())
            ),
            "context_pack_excluded_categories": context[
                "excluded_categories"
            ],
            "context_pack_inclusion_reasons": context.get(
                "included_reasons", {}
            ),
            "silent_fallback_detected": False,
            "unavailable_model_errors": [
                item for item in violations
                if item["code"] == "exact_model_unavailable"
            ],
            "compact_output_contract_present": compact_contract_present,
            "application_repository": application,
            "mutation_counters": {
                "models_launched": 0,
                "child_sessions_launched": 0,
                "writer_leases_acquired": 0,
                "transactions_created": 0,
                "ledger_events_appended": 0,
                "projection_updates": 0,
                "cycle_cache_updates": 0,
                "compatibility_cache_updates": 0,
                "application_files_modified": 0,
                "application_commits_created": 0,
                "controller_files_modified": 0,
                "unrelated_mcp_authentications": 0,
            },
            "warning": warnings,
            "blocker": blockers,
            "unsupported": unsupported,
            "violation": violations,
            "runtime_policy_passed": passed,
            "terminal_classification": (
                "runtime_policy_passed"
                if passed
                else unsupported[0]["code"]
                if unsupported
                else violations[0]["code"]
                if violations
                else blockers[0]["code"]
            ),
        }
