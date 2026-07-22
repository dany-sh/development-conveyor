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

from .queue import FeatureQueue


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
        "planning_finalization",
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
                      application_runtime_changed: bool = False,
                      feature_id: str | None = None) -> dict[str, Any]:
    if feature_id == "F003":
        focused_tests = [
            "Tests/LiveInterviewCompanionTests/CoreTests.swift",
            "Tests/LiveInterviewCompanionTests/SessionLifecycleTests.swift",
            "Tests/LiveInterviewCompanionTests/SessionRunOrchestratorTests.swift",
        ]
        focused_commands = [["swift", "build"], ["git", "diff", "--check"]]
        final_gates = [
            ["swift", "build"], ["swift", "test"], ["swift", "build", "-c", "release"],
            ["./script/build_and_run.sh", "--verify"],
            ["deterministic", "queue/inventory", "validation"],
            ["accessibility", "single-window", "smoke"],
            ["documentation", "architecture/ADR", "validation"], ["git", "diff", "--check"],
        ]
        return {
            "tier": "focused_application_feature", "tests": focused_tests,
            "commands": focused_commands, "builds": [["swift", "build"]],
            "final_acceptance_gates": final_gates,
            "skipped": ["controller full suite: controller implementation is not being changed by the application feature", "application full build/test during dry-run: dry-runs perform no validation commands"],
        }
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


def _queue_reconciliation_context_pack(project: Any) -> dict[str, Any]:
    """Return the small planning context needed by one reconciliation parent."""
    repository = project.repository
    queue = FeatureQueue.from_location(repository, project.queue_location)
    milestone = project.active_milestone or ""
    selected = queue.select_next(milestone)
    candidates = [] if selected is not None else queue.features_for_milestone(milestone)
    paths = [project.queue_location, "docs/CURRENT_STATUS.md", "docs/FEATURE_CATALOG.md"]
    # Candidate specs carry the only feature-level semantic context permitted.
    paths.extend(
        item.get("spec") or item.get("specification") or item.get("spec_path")
        for item in candidates
        if isinstance(item.get("spec") or item.get("specification") or item.get("spec_path"), str)
    )
    paths.extend(path for path in ("docs/roadmap/DEVELOPMENT_ROADMAP.md", "docs/roadmap/IMPLEMENTATION_TASKS.md") if (repository / path).is_file())
    files = list(dict.fromkeys(path for path in paths if (repository / path).is_file()))
    sizes = {path: (repository / path).stat().st_size for path in files}
    return {
        "files": files,
        "file_count": len(files),
        "approximate_bytes_estimate": sum(sizes.values()),
        "included_reasons": {
            project.queue_location: "compact validated queue and active milestone metadata",
            **{path: "candidate specification or roadmap ordering constraint" for path in files if path != project.queue_location},
        },
        "excluded_categories": ["all milestones", "unrelated feature specifications", "full Git history", "global memory", "application source"],
        "output_contract": "queue reconciliation terminal result contract",
    }


def _application_feature_context_pack(project: Any, feature: dict[str, Any], tests: list[str]) -> dict[str, Any]:
    """Build the bounded application-side context for a fresh feature session."""
    repository = project.repository
    feature_id = str(feature.get("id") or "")
    paths = [project.queue_location, "docs/CURRENT_STATUS.md", str(feature.get("spec") or "")]
    if feature_id == "F003":
        paths.extend([
            "Sources/LiveInterviewCompanion/App/LiveInterviewCompanionApp.swift",
            "Sources/LiveInterviewCompanion/Views/RootView.swift",
            "Sources/LiveInterviewCompanion/Views/SetupView.swift",
            "Sources/LiveInterviewCompanion/Views/SessionView.swift",
            "Sources/LiveInterviewCompanion/Views/SessionHistoryView.swift",
            "Sources/LiveInterviewCompanion/Views/SettingsView.swift",
            "Sources/LiveInterviewCompanion/Views/CompanionMenuView.swift",
            "Sources/LiveInterviewCompanion/Views/TransientCueView.swift",
            "Sources/LiveInterviewCompanion/Stores/AppStore.swift",
            "Sources/LiveInterviewCompanion/Models/SessionLifecycle.swift",
            "Sources/LiveInterviewCompanion/Support/TransientCuePanelController.swift",
            "Sources/LiveInterviewCompanion/Support/CuePresentationPolicy.swift",
            "docs/architecture.md", "docs/data-flow.md",
            "docs/features/F002-unified-session-lifecycle.md",
            "docs/features/F001-product-domain-model.md",
            "docs/features/F005-persistent-data-store.md",
            *tests,
        ])
    files = list(dict.fromkeys(path for path in paths if path and (repository / path).is_file()))
    reasons = {path: "selected feature contract, route, lifecycle boundary, focused test, or architecture contract" for path in files}
    return {
        "files": files, "file_count": len(files),
        "approximate_bytes_estimate": sum((repository / path).stat().st_size for path in files),
        "included_reasons": reasons,
        "excluded_categories": ["unrelated feature specifications", "unrelated milestones", "full Git history", "global memory", "unrelated skills/plugins", "full test logs"],
        "truncation": "none; bounded to the selected feature and direct contracts",
    }


def build_run_plan(plan: dict[str, Any], root: Path, *, project: Any | None = None) -> dict[str, Any]:
    action = str(plan.get("proposed_next_action") or "status")
    ready_feature = plan.get("selected_feature")
    deterministic_queue_selection = action == "queue_reconciliation" and isinstance(ready_feature, str) and bool(ready_feature)
    semantic_queue_reconciliation = action == "queue_reconciliation" and not deterministic_queue_selection
    deterministic_actions = {"verify_consistency", "milestone_integration", "planning_finalization"}
    application_feature = action == "feature_cycle" and project is not None and isinstance(ready_feature, str)
    task = (
        "application_feature" if application_feature
        else
        "queue_reconciliation" if semantic_queue_reconciliation
        else "planning_finalization" if action == "planning_finalization"
        else "status" if action in deterministic_actions or deterministic_queue_selection
        else "controller_repair"
    )
    risk = "medium" if application_feature else "low"
    selection = select_model(task=task, risk=risk)
    changed: list[str] = []
    selected_feature = None
    if application_feature:
        selected_feature = FeatureQueue.from_location(project.repository, project.queue_location).feature(ready_feature)
    verify = verification_plan(changed, risk=risk, application_runtime_changed=application_feature,
                               feature_id=ready_feature if application_feature else None)
    pack = (
        _queue_reconciliation_context_pack(project)
        if semantic_queue_reconciliation and project is not None
        else _application_feature_context_pack(project, selected_feature, verify["tests"])
        if application_feature and selected_feature is not None
        else context_pack(root, changed, verify["tests"])
    )
    parent_sessions_planned = 1 if selection.model is not None else 0
    return {"schema_version": POLICY_VERSION, "workflow_type": action, "task_classification": selection.task_classification,
            "queue_reconciliation_route": (
                "deterministic_queue_selection" if deterministic_queue_selection
                else "semantic_queue_reconciliation" if semantic_queue_reconciliation else None
            ),
            "risk_classification": selection.risk_classification, "deterministic_alternative_considered": selection.deterministic_alternative,
            "selected_model": selection.model, "selected_reasoning_effort": selection.reasoning,
            "parent_session_budget": 1, "child_session_budget": 0, "child_agent_justification": None,
            "context_pack": pack, "deterministic_commands_planned": verify["commands"], "selected_tests": verify["tests"],
            "implementation_loop_verification": {"tests": verify["tests"], "commands": verify["commands"], "builds": verify["builds"]},
            "final_feature_acceptance_gates": verify.get("final_acceptance_gates", []),
            "test_tier": verify["tier"], "skipped_validations": verify.get("skipped", []), "reusable_prior_evidence": [],
            "evidence_invalidation_conditions": reusable_evidence(None, "unavailable")["invalidation_conditions"],
            "application_builds_planned": verify["builds"], "expected_application_mutations": bool(plan.get("application_mutation_expected")),
            "expected_cost_class": risk, "escalation_triggers": list(selection.escalation_triggers),
            "stopping_criteria": ["selected validations pass", "no unresolved safety risk", "acceptance criteria are proven"],
            "execution": {"models_planned": parent_sessions_planned},
            "usage_accounting": {"deterministic_only": selection.model is None, "parent_sessions_planned": parent_sessions_planned,
                "parent_sessions_launched": 0, "child_sessions_planned": 0, "child_sessions_launched": 0,
                "context_pack_file_count": pack["file_count"], "context_bytes_estimate": pack["approximate_bytes_estimate"],
                "builds_run": [], "elapsed_time_seconds": None, "report_files": [], "escalated": False,
                "lower_cost_deterministic_alternative_existed": selection.model is None, "stopping_criteria_met": False},
            "dry_run": {"models": 0, "child_agents": 0, "mutations": 0, "tests": 0, "builds": 0, "leases": 0, "transactions": 0}}
