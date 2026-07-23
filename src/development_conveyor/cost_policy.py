"""Cost-aware routing, verification, context, and evidence policy.

The policy is deliberately deterministic: it describes a workflow before a
session or mutation starts and never changes transaction or lease authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .queue import FeatureQueue
from .errors import SessionError
from .execution_profiles import (
    DEFAULT_PROFILES,
    resolve_execution_profile,
)


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
    """Compatibility wrapper for fixed workflow fallbacks.

    Risk words, task length, ambiguity flags, and failure flags do not alter
    routing.  Profile changes require ``resolve_execution_profile`` and
    recorded escalation evidence.
    """
    deterministic = {
        "status", "consistency", "queue_parse", "dry_run", "integration",
        "git_inspection", "cleanliness", "hash_comparison", "test_execution",
        "validation_selection", "evidence_lookup", "report_generation",
        "planning_finalization",
    }
    if task in deterministic:
        return ModelSelection(None, None, task, risk, "deterministic implementation available", None, ())
    if task in {"metadata", "formatting", "mechanical_config", "fixture"}:
        profile = DEFAULT_PROFILES["mechanical"]
        return ModelSelection(profile["model"], profile["reasoning"], task, risk,
                              "no model only when exact structured transformation exists",
                              "semantic transformation remains", ())
    profile_name = "repository_aware" if task == "queue_reconciliation" else "generic_or_architectural"
    profile = DEFAULT_PROFILES[profile_name]
    return ModelSelection(
        profile["model"], profile["reasoning"], task, risk,
        "deterministic path cannot perform the required semantic work",
        "workflow fallback applies only when feature metadata and an explicit override are absent",
        (),
    )


def verification_plan(changed_paths: list[str], *, risk: str = "low",
                      application_runtime_changed: bool = False,
                      feature_id: str | None = None,
                      feature: dict[str, Any] | None = None,
                      adapter: dict[str, Any] | None = None,
                      discovered_tests: list[str] | None = None) -> dict[str, Any]:
    if application_runtime_changed and feature is not None:
        commands = adapter.get("commands", {}) if isinstance(adapter, dict) else {}
        groups = ("build", "test", "lint", "package", "validate")
        final_gates = [
            list(command)
            for group in groups
            for command in (commands.get(group, []) if isinstance(commands, dict) else [])
            if isinstance(command, list) and all(isinstance(part, str) for part in command)
        ]
        criteria = list(feature.get("acceptance_criteria") or [])
        if any("accessibility" in item.lower() and "relaunch" in item.lower() for item in criteria):
            final_gates.append(["accessibility", "relaunch", "smoke"])
        final_gates.extend([
            ["deterministic", "queue/inventory", "validation"],
            ["documentation", "architecture/ADR", "validation"],
            ["git", "diff", "--check"],
        ])
        focused_commands = [
            list(command)
            for group in ("build", "test")
            for command in (commands.get(group, []) if isinstance(commands, dict) else [])
            if isinstance(command, list) and all(isinstance(part, str) for part in command)
        ]
        return {
            "tier": "focused_application_feature", "tests": list(discovered_tests or []),
            "commands": focused_commands,
            "builds": [list(item) for item in (commands.get("build", []) if isinstance(commands, dict) else [])],
            "final_acceptance_gates": final_gates,
            "feature_acceptance_criteria": criteria,
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


def _application_feature_context_pack(project: Any, feature: dict[str, Any]) -> dict[str, Any]:
    """Discover focused source/test context without influencing model choice."""
    repository = project.repository
    spec = str(feature.get("spec") or "")
    spec_text = (repository / spec).read_text(encoding="utf-8") if spec and (repository / spec).is_file() else ""
    criteria = "\n".join(str(item) for item in feature.get("acceptance_criteria", []))
    contract = "\n".join((str(feature.get("title") or ""), spec_text, criteria))
    symbols = {
        token for token in re.findall(r"`([A-Za-z][A-Za-z0-9_.-]{2,})`", contract)
        if "/" not in token and not token.endswith((".md", ".py", ".swift"))
    }
    words = {
        word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", contract)
        if word.lower() not in {
            "acceptance", "criteria", "feature", "implementation", "application", "required",
            "current", "without", "should", "would", "could", "under", "through", "between",
        }
    }
    ignored = {".git", ".build", ".factory", "build", "dist", "DerivedData", "node_modules"}
    candidates: list[tuple[int, str, bool]] = []
    for path in repository.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        relative = path.relative_to(repository).as_posix()
        is_test = relative.startswith(("Tests/", "tests/")) or "test" in path.stem.lower()
        is_source = relative.startswith(("Sources/", "src/", "App/", "app/"))
        if not is_source and not is_test:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:500_000]
        except OSError:
            continue
        relative_lower = relative.lower()
        text_lower = text.lower()
        score = sum(5 for symbol in symbols if symbol.lower() in text_lower)
        score += sum(3 for symbol in symbols if symbol.lower() in relative_lower)
        score += sum(2 for word in words if word in relative_lower)
        if score:
            candidates.append((score, relative, is_test))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    source_files = [path for _, path, is_test in candidates if not is_test][:12]
    test_files = [path for _, path, is_test in candidates if is_test][:10]
    if not source_files and not test_files:
        fallback = []
        for path in sorted(repository.iterdir()):
            if path.is_file() and path.name not in {"README.md"} and not path.name.startswith("."):
                fallback.append(path.relative_to(repository).as_posix())
        source_files = fallback[:5]
    if not source_files and not test_files:
        raise SessionError(
            f"application feature {feature.get('id')} has planning documents but no relevant source/test context"
        )
    planning_paths = [
        project.queue_location, "docs/CURRENT_STATUS.md", spec,
        ".factory/project.yaml", "docs/architecture.md", "docs/data-flow.md",
    ]
    direct_paths = [
        token for token in re.findall(r"`([^`]+)`", contract)
        if "/" in token and (repository / token).is_file()
    ]
    files = list(dict.fromkeys(
        path for path in [*planning_paths, *direct_paths, *source_files, *test_files]
        if path and (repository / path).is_file()
    ))
    reasons = {
        path: (
            "relevant focused test discovered from the feature contract" if path in test_files
            else "relevant application source discovered from the feature contract" if path in source_files
            else "selected feature, architecture, or executable adapter contract"
        )
        for path in files
    }
    return {
        "files": files, "file_count": len(files),
        "approximate_bytes_estimate": sum((repository / path).stat().st_size for path in files),
        "included_reasons": reasons,
        "source_files": source_files,
        "test_files": test_files,
        "excluded_categories": ["unrelated feature specifications", "unrelated milestones", "full Git history", "global memory", "unrelated skills/plugins", "full test logs"],
        "truncation": "none; bounded to the selected feature and direct contracts",
    }


def build_run_plan(
    plan: dict[str, Any],
    root: Path,
    *,
    project: Any | None = None,
    profile_configuration: dict[str, Any] | None = None,
    override_profile: str | None = None,
) -> dict[str, Any]:
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
    risk = "high" if application_feature else "low"
    changed: list[str] = []
    selected_feature = None
    if application_feature:
        selected_feature = FeatureQueue.from_location(project.repository, project.queue_location).feature(ready_feature)
        if selected_feature is None:
            raise SessionError(f"selected application feature {ready_feature} is absent from the queue")
    deterministic = task in {"status", "planning_finalization"}
    resolved = resolve_execution_profile(
        workflow=task,
        deterministic=deterministic,
        feature=selected_feature,
        project_id=project.project_id if project is not None else None,
        configuration=profile_configuration,
        override_profile=override_profile,
        escalation_evidence=plan.get("execution_profile_escalation_evidence"),
    )
    pack = (
        _queue_reconciliation_context_pack(project)
        if semantic_queue_reconciliation and project is not None
        else _application_feature_context_pack(project, selected_feature)
        if application_feature and selected_feature is not None
        else context_pack(root, changed, [])
    )
    adapter = None
    if application_feature:
        adapter_path = project.repository / project.validation_source
        try:
            adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError("application feature adapter is missing or invalid") from exc
    verify = verification_plan(
        changed,
        risk=risk,
        application_runtime_changed=application_feature,
        feature_id=ready_feature if application_feature else None,
        feature=selected_feature,
        adapter=adapter,
        discovered_tests=pack.get("test_files", []),
    )
    parent_sessions_planned = resolved.parent_sessions if resolved.model is not None else 0
    child_sessions_planned = resolved.child_sessions if resolved.model is not None else 0
    return {"schema_version": POLICY_VERSION, "workflow_type": action, "task_classification": task,
            "queue_reconciliation_route": (
                "deterministic_queue_selection" if deterministic_queue_selection
                else "semantic_queue_reconciliation" if semantic_queue_reconciliation else None
            ),
            "risk_classification": risk,
            "deterministic_alternative_considered": "deterministic route evaluated before profile resolution",
            "execution_profile": resolved.to_dict(),
            "profile": resolved.profile,
            "profile_resolution_source": resolved.resolution_source,
            "selected_model": resolved.model, "selected_reasoning_effort": resolved.reasoning,
            "parent_session_budget": resolved.parent_sessions,
            "child_session_budget": resolved.child_sessions,
            "child_agent_justification": None,
            "context_pack": pack, "deterministic_commands_planned": verify["commands"], "selected_tests": verify["tests"],
            "implementation_loop_verification": {"tests": verify["tests"], "commands": verify["commands"], "builds": verify["builds"]},
            "final_feature_acceptance_gates": verify.get("final_acceptance_gates", []),
            "feature_acceptance_criteria": verify.get("feature_acceptance_criteria", []),
            "test_tier": verify["tier"], "skipped_validations": verify.get("skipped", []), "reusable_prior_evidence": [],
            "evidence_invalidation_conditions": reusable_evidence(None, "unavailable")["invalidation_conditions"],
            "application_builds_planned": verify["builds"], "expected_application_mutations": bool(plan.get("application_mutation_expected")),
            "expected_cost_class": risk,
            "escalation_trigger": resolved.escalation_trigger,
            "escalation_profile": resolved.escalation_profile,
            "stopping_criteria": ["selected validations pass", "no unresolved safety risk", "acceptance criteria are proven"],
            "execution": {"models_planned": parent_sessions_planned},
            "usage_accounting": {"deterministic_only": resolved.model is None, "parent_sessions_planned": parent_sessions_planned,
                "parent_sessions_launched": 0, "child_sessions_planned": child_sessions_planned, "child_sessions_launched": 0,
                "context_pack_file_count": pack["file_count"], "context_bytes_estimate": pack["approximate_bytes_estimate"],
                "builds_run": [], "elapsed_time_seconds": None, "report_files": [], "escalated": resolved.escalated,
                "lower_cost_deterministic_alternative_existed": resolved.model is None, "stopping_criteria_met": False},
            "dry_run": {"models": 0, "child_agents": 0, "mutations": 0, "tests": 0, "builds": 0, "leases": 0, "transactions": 0}}
