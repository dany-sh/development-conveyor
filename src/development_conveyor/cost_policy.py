"""Cost-aware routing, verification, context, and evidence policy.

The policy is deliberately deterministic: it describes a workflow before a
session or mutation starts and never changes transaction or lease authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


POLICY_VERSION = 1


@dataclass(frozen=True)
class ModelSelection:
    model: str | None
    reasoning: str | None
    task_classification: str
    risk_classification: str
    deterministic_alternative: str
    cheaper_insufficient_reason: str | None
    escalation_triggers: tuple[str, ...]


def select_model(*, task: str, risk: str = "low", ambiguity: bool = False,
                 focused_attempt_failed: bool = False, task_length: int = 1) -> ModelSelection:
    """Select model and reasoning independently; task length is not a signal."""
    deterministic = {
        "status", "consistency", "queue_parse", "dry_run", "integration",
        "git_inspection", "cleanliness", "hash_comparison", "test_execution",
        "validation_selection", "evidence_lookup", "report_generation",
    }
    if task in deterministic:
        return ModelSelection(None, None, task, risk, "deterministic implementation available", None, ())
    if task in {"metadata", "formatting", "mechanical_config", "fixture"}:
        return ModelSelection("gpt-5.6-luna", "medium", task, risk,
                              "no model only when exact structured transformation exists",
                              "semantic transformation remains", ("ambiguous output",))
    if risk == "high":
        if focused_attempt_failed and ambiguity:
            return ModelSelection("gpt-5.6-sol", "xhigh", task, risk,
                                  "lower-effort focused attempt failed", "written escalation evidence required",
                                  ("new high-risk ambiguity",))
        return ModelSelection("gpt-5.6-sol", "high", task, risk,
                              "deterministic path cannot resolve high-risk semantics", "persistent or Git safety semantics",
                              ("semantic conflict", "unresolved recovery evidence"))
    if ambiguity or focused_attempt_failed:
        return ModelSelection("gpt-5.6-terra", "high", task, "moderate",
                              "focused deterministic inspection was insufficient", "multi-module ambiguity",
                              ("unresolved focused repair", "architecture tradeoff"))
    return ModelSelection("gpt-5.6-terra", "medium", task, risk,
                          "deterministic path cannot perform implementation reasoning", "controller implementation required",
                          ("focused repair failure", "material ambiguity"))


def verification_plan(changed_paths: list[str], *, risk: str = "low",
                      application_runtime_changed: bool = False) -> dict[str, Any]:
    paths = set(changed_paths)
    docs_only = bool(paths) and all(path.startswith("docs/") or path.endswith(".md") for path in paths)
    config_only = bool(paths) and all(path.startswith("config/") for path in paths)
    integration = any("integration_executor.py" in path for path in paths)
    projection = any(path.endswith(("projection.py", "execution_plan.py")) for path in paths)
    safety = risk == "high" or any(any(token in path for token in ("ledger", "lease", "migration", "recovery", "git")) for path in paths)
    if docs_only:
        return {"tier": "focused", "tests": [], "commands": [["git", "diff", "--check"]], "builds": [],
                "skipped": ["controller suites and application builds: documentation-only change"]}
    if config_only:
        return {"tier": "focused", "tests": ["tests/test_config.py", "tests/test_cli_compatibility_repair.py"],
                "commands": [["scripts/conveyor", "validate-config"]], "builds": [],
                "skipped": ["unrelated controller and application tests: executable configuration only"]}
    if integration:
        return {"tier": "integration", "tests": ["tests/test_deterministic_integration_handoff.py", "tests/test_prohibited_actions.py", "tests/test_integration_recovery_contract.py"],
                "commands": [["disposable-repository", "integration-scenario"]], "builds": [],
                "skipped": []}
    if projection:
        return {"tier": "focused", "tests": ["tests/test_projection_authority.py", "tests/test_planning_transaction_finalization.py"],
                "commands": [["disposable-repository", "dry-run"]], "builds": [], "skipped": ["unrelated persistent-store and application tests"]}
    if safety:
        return {"tier": "integration", "tests": ["direct safety tests", "relevant consistency tests"],
                "commands": [["disposable-repository", "end-to-end"]], "builds": [], "skipped": ["full suite unless a qualifying cross-module failure appears"]}
    return {"tier": "focused", "tests": ["directly affected tests"], "commands": [], "builds": [] if not application_runtime_changed else ["affected application only"], "skipped": ["full suite: no qualifying risk trigger"]}


def context_pack(root: Path, changed_paths: list[str], tests: list[str], *, high_risk: bool = False) -> dict[str, Any]:
    files = [path for path in [*changed_paths, *tests] if (root / path).is_file()]
    if high_risk:
        files.extend(path for path in ("docs/AUTONOMY_CONTRACT.md", ".factory/project.yaml") if (root / path).is_file())
    files = list(dict.fromkeys(files))
    sizes = {path: (root / path).stat().st_size for path in files}
    return {"files": files, "file_count": len(files), "approximate_bytes_estimate": sum(sizes.values()),
            "included_reasons": {path: "directly affected source, test, or required high-risk contract" for path in files},
            "excluded_categories": ["application roadmaps", "unrelated feature specifications", "full history", "skills/plugins", "raw terminal logs"],
            "truncation": "none; files are focused"}


def validation_identity(command: list[str], *, source_tree_hash: str, configuration_hash: str,
                        fixture_version: str, cli_version: str | None = None,
                        toolchain_version: str | None = None, environment: dict[str, str] | None = None) -> str:
    value = {"command": command, "source_tree_hash": source_tree_hash, "configuration_hash": configuration_hash,
             "fixture_version": fixture_version, "cli_version": cli_version, "toolchain_version": toolchain_version,
             "environment": environment or {}, "planner_version": POLICY_VERSION}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reusable_evidence(record: dict[str, Any] | None, identity: str) -> dict[str, Any]:
    reusable = bool(record and record.get("status") == "passed" and record.get("identity") == identity
                    and record.get("complete") is True and record.get("trusted") is True and not record.get("dirty"))
    return {"reused": reusable, "originating_run": record.get("run_id") if reusable else None,
            "matching_identity": identity if reusable else None,
            "reason": "complete trusted identity matches" if reusable else "missing, stale, dirty, incomplete, or failed evidence",
            "invalidation_conditions": ["relevant source/config/fixture change", "CLI or toolchain change", "dirty/untrusted result", "external-state dependency", "fresh safety policy"]}


class ValidationEvidenceCache:
    """Small controller-owned cache; entries are reusable only through ``reusable_evidence``."""
    def __init__(self, path: Path):
        self.path = path

    def lookup(self, identity: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        record = value.get(identity) if isinstance(value, dict) else None
        return record if isinstance(record, dict) else None

    def store(self, identity: str, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        current[identity] = {**record, "identity": identity}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(current, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class ChildSessionBudget:
    def __init__(self, maximum: int, workflow: str):
        self.maximum, self.workflow, self.launched = maximum, workflow, 0

    def launch(self, role: str, justification: str | None, callback: Callable[[], Any]) -> Any:
        if self.maximum <= self.launched or not justification:
            raise PermissionError(json.dumps({"requested_role": role, "workflow": self.workflow,
                "child_sessions_launched": self.launched, "reason": "child session budget exhausted or prohibited"}, sort_keys=True))
        self.launched += 1
        return callback()


def contain_command_output(command: list[str], *, cwd: Path, report_path: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    output = result.stdout + result.stderr
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(output, encoding="utf-8")
    return {"command": command, "exit_status": result.returncode, "report_path": str(report_path),
            "output_bytes": len(output.encode()), "truncated_for_context": len(output) > 4000,
            "failure_excerpt": output[-1000:] if result.returncode else None}


def build_run_plan(plan: dict[str, Any], root: Path) -> dict[str, Any]:
    action = str(plan.get("proposed_next_action") or "status")
    deterministic_actions = {"verify_consistency", "milestone_integration", "queue_reconciliation"}
    selection = select_model(task="integration" if action == "milestone_integration" else ("status" if action in deterministic_actions else "controller_repair"), risk="low")
    changed: list[str] = []
    verify = verification_plan(changed)
    pack = context_pack(root, changed, verify["tests"])
    return {"schema_version": POLICY_VERSION, "workflow_type": action, "task_classification": selection.task_classification,
            "risk_classification": selection.risk_classification, "deterministic_alternative_considered": selection.deterministic_alternative,
            "selected_model": selection.model, "selected_reasoning_effort": selection.reasoning,
            "parent_session_budget": 1, "child_session_budget": 0, "child_agent_justification": None,
            "context_pack": pack, "deterministic_commands_planned": verify["commands"], "selected_tests": verify["tests"],
            "test_tier": verify["tier"], "skipped_validations": verify.get("skipped", []), "reusable_prior_evidence": [],
            "evidence_invalidation_conditions": reusable_evidence(None, "unavailable")["invalidation_conditions"],
            "application_builds_planned": verify["builds"], "expected_application_mutations": bool(plan.get("application_mutation_expected")),
            "expected_cost_class": "low", "escalation_triggers": list(selection.escalation_triggers),
            "stopping_criteria": ["selected validations pass", "no unresolved safety risk", "acceptance criteria are proven"],
            "usage_accounting": {"deterministic_only": selection.model is None, "parent_sessions_planned": 1,
                "parent_sessions_launched": 0, "child_sessions_planned": 0, "child_sessions_launched": 0,
                "context_pack_file_count": pack["file_count"], "context_bytes_estimate": pack["approximate_bytes_estimate"],
                "builds_run": [], "elapsed_time_seconds": None, "report_files": [], "escalated": False,
                "lower_cost_deterministic_alternative_existed": selection.model is None, "stopping_criteria_met": False},
            "dry_run": {"models": 0, "child_agents": 0, "mutations": 0, "tests": 0, "builds": 0, "leases": 0, "transactions": 0}}
