"""Evidence-based interrupted-cycle reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import RecoveryError
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import classify_post_integration_commands, parse_integration_terminal_result


@dataclass(frozen=True)
class RecoveryAssessment:
    outcome: str
    resume_phase: str | None
    evidence: dict[str, Any]
    human_decision: dict[str, Any] | None


@dataclass(frozen=True)
class StartupReconciliation:
    classification: str
    persisted_state: str
    derived_state: str
    transition_path: tuple[str, ...]
    would_persist: bool
    reason: str
    evidence: dict[str, Any]
    human_decision: dict[str, Any] | None = None


def _queue_fingerprint(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _gate_reopen_policy(project: Project) -> tuple[bool, str]:
    adapter = project.repository / project.validation_source
    try:
        value = json.loads(adapter.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "repository adapter could not be read as JSON-compatible YAML"
    integration = value.get("integration") if isinstance(value, dict) else None
    if isinstance(integration, dict) and integration.get("rerun_milestone_gates") is True:
        return True, "integration.rerun_milestone_gates is true"
    return False, "repository policy does not explicitly permit rerunning milestone gates"


def _timestamp_after(value: str | None, baseline: str | None) -> bool:
    if not value or not baseline:
        return False
    try:
        return datetime.fromisoformat(value).astimezone() > datetime.fromisoformat(baseline).astimezone()
    except (TypeError, ValueError):
        return False


def assess_durable_integration_success(
    project: Project,
    cycle: dict[str, Any] | None,
    queue: FeatureQueue | None,
    inspector: RepositoryInspector,
    session_report: dict[str, Any] | None,
    *,
    writer_exists: bool,
) -> dict[str, Any]:
    """Corroborate terminal integration success from independent durable evidence."""

    runtime_path = project.repository / ".factory/runtime/milestone-integration/latest.json"
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        runtime = None
    try:
        adapter = json.loads((project.repository / project.validation_source).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        adapter = None
    configured_commands: list[tuple[str, ...]] = []
    command_groups = (adapter or {}).get("commands") if isinstance(adapter, dict) else None
    if isinstance(command_groups, dict):
        for group in ("build", "test", "lint", "package", "validate"):
            for command in command_groups.get(group, []):
                if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
                    configured_commands.append(tuple(command))

    report_output = str((session_report or {}).get("redacted_stdout") or "")
    terminal, terminal_validation = parse_integration_terminal_result(report_output)
    report_working_directory = (session_report or {}).get("working_directory")
    report_working_directory_matches = False
    if isinstance(report_working_directory, str) and report_working_directory:
        try:
            report_working_directory_matches = (
                Path(report_working_directory).resolve() == project.repository.resolve()
            )
        except (OSError, RuntimeError):
            report_working_directory_matches = False
    cycle_session_id = (cycle or {}).get("integration_session_id")
    report_session_id = (session_report or {}).get("session_id")
    integration_session_identity_matches = bool(
        isinstance(cycle_session_id, str)
        and cycle_session_id.strip()
        and isinstance(report_session_id, str)
        and report_session_id.strip()
        and report_session_id == cycle_session_id
    )
    observations, optional_warnings = classify_post_integration_commands(
        report_output, required_commands=tuple(configured_commands)
    )
    runtime_validation = runtime.get("validation") if isinstance(runtime, dict) else None
    runtime_plan = runtime.get("plan") if isinstance(runtime, dict) else None
    plan_validation = runtime_plan.get("validation") if isinstance(runtime_plan, dict) else None
    runtime_commands = (
        runtime_validation.get("commands")
        if isinstance(runtime_validation, dict) and isinstance(runtime_validation.get("commands"), list)
        else []
    )
    runtime_argv = {
        tuple(item.get("argv") or [])
        for item in runtime_commands
        if isinstance(item, dict) and isinstance(item.get("argv"), list)
    }
    runtime_required_observations = [
        {
            "category": "required_validation",
            "command": list(item.get("argv") or []),
            "exit_code": item.get("exit_code"),
            "status": "completed" if item.get("exit_code") == 0 else "failed",
            "effect": "authoritative",
            "configured_required": tuple(item.get("argv") or []) in configured_commands,
            "diagnostic": None,
        }
        for item in runtime_commands
        if isinstance(item, dict)
    ]
    final_head = runtime.get("final_head") if isinstance(runtime, dict) else None
    integrating_commit = runtime.get("integrating_state_commit") if isinstance(runtime, dict) else None
    evidence_commit = runtime.get("evidence_commit") if isinstance(runtime, dict) else None
    model_evidence_commit = runtime.get("model_evidence_commit") if isinstance(runtime, dict) else None
    runtime_identity = runtime.get("runtime_identity") if isinstance(runtime, dict) else None
    feature_id = str((cycle or {}).get("current_feature") or "")
    feature = queue.feature(feature_id) if queue is not None and feature_id else None
    milestone = queue.milestone(project.active_milestone or "") if queue is not None else None
    accepted = feature.get("accepted_commit") if isinstance(feature, dict) else None
    integrated = feature.get("integrated_commit") if isinstance(feature, dict) else None
    milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False)
    git_operations = inspector.git_operation_state()
    matching_features = [
        item for item in (queue.features if queue is not None else [])
        if item.get("accepted_commit") == accepted and item.get("integrated_commit") == integrated
    ] if accepted and integrated else []
    required_command_failures = [
        item for item in runtime_required_observations if item.get("exit_code") != 0
    ] + [
        item for item in observations
        if item.get("effect") == "authoritative" and item.get("exit_code") != 0
    ]
    integration_fix_commits = (
        list(feature.get("integration_fix_commits") or []) if isinstance(feature, dict) else []
    )
    runtime_fix_commits = list((runtime or {}).get("integration_fix_commits") or [])
    nested_runtime_plan = (runtime or {}).get("plan")
    nested_runtime_fix_commits = list(
        nested_runtime_plan.get("integration_fix_commits") or []
    ) if isinstance(nested_runtime_plan, dict) else []
    integration_fix_records_match = bool(
        integration_fix_commits == runtime_fix_commits == nested_runtime_fix_commits
    )
    validated_tree = integration_fix_commits[-1] if integration_fix_commits else integrating_commit
    runtime_validation_commit = (
        runtime_validation.get("validated_commit") if isinstance(runtime_validation, dict) else None
    )
    plan_validation_commit = (
        plan_validation.get("validated_commit") if isinstance(plan_validation, dict) else None
    )
    validation_evidence_matches_tree = bool(
        runtime_validation_commit == validated_tree
        if integration_fix_commits
        else validated_tree in {runtime_validation_commit, plan_validation_commit}
    )
    pre_validation_chain = [integrated, integrating_commit, *integration_fix_commits]
    pre_validation_chain_valid = bool(
        all(isinstance(commit, str) and inspector.ref_exists(commit) for commit in pre_validation_chain)
        and all(
            inspector.rev_parse(f"{current}^", check=False) == previous
            for previous, current in zip(pre_validation_chain, pre_validation_chain[1:])
        )
    )
    direct_chain = bool(
        isinstance(integrated, str)
        and isinstance(integrating_commit, str)
        and isinstance(evidence_commit, str)
        and isinstance(model_evidence_commit, str)
        and isinstance(validated_tree, str)
        and pre_validation_chain_valid
        and inspector.rev_parse(f"{evidence_commit}^", check=False) == validated_tree
        and inspector.rev_parse(f"{model_evidence_commit}^", check=False) == evidence_commit
        and final_head == model_evidence_commit
    )
    factory_subjects = bool(
        direct_chain
        and inspector.commit_subject(integrating_commit) == f"factory: mark {feature_id} integrating"
        and inspector.commit_subject(evidence_commit) == f"factory: record {feature_id} integration passed"
        and inspector.commit_subject(model_evidence_commit)
        == f"factory: record {feature_id} integration model-evidence"
    )
    factory_paths = bool(
        direct_chain
        and set(inspector.changed_paths(integrating_commit)) == {"docs/FEATURE_QUEUE.yaml"}
        and set(inspector.changed_paths(evidence_commit)) == {
            "docs/CURRENT_STATUS.md", "docs/FEATURE_QUEUE.yaml", "docs/RUN_LOG.md",
        }
        and set(inspector.changed_paths(model_evidence_commit)) == {"docs/RUN_LOG.md"}
    )
    finalization_history = list((cycle or {}).get("integration_finalization_history") or [])
    historical_finalized = bool(
        finalization_history
        and (cycle or {}).get("current_phase") == "completed"
        and (cycle or {}).get("integration_status") == "passed"
        and (cycle or {}).get("failure_classification") is None
        and (cycle or {}).get("stop_reason") is None
        and (cycle or {}).get("human_decision_required") is None
        and (cycle or {}).get("integration_gate") is None
        and finalization_history[-1].get("terminal_commit") == final_head
    )
    later_commits: list[str] = []
    later_planning_only = False
    if (
        historical_finalized
        and isinstance(final_head, str)
        and final_head != inspector.head
        and inspector.is_ancestor(final_head, inspector.head)
    ):
        later_commits = [
            line for line in inspector.git(["rev-list", "--reverse", f"{final_head}..{inspector.head}"]).stdout.splitlines()
            if line
        ]
        later_planning_only = bool(later_commits) and all(
            inspector.changed_paths(commit)
            and all(path.startswith("docs/") for path in inspector.changed_paths(commit))
            and (
                inspector.commit_subject(commit).startswith("docs:")
                or "planning" in inspector.commit_subject(commit).lower()
                or "reconcile" in inspector.commit_subject(commit).lower()
            )
            for commit in later_commits
        )
    current_head_is_allowed = final_head == milestone_head == inspector.head or bool(
        historical_finalized
        and later_planning_only
        and milestone_head == inspector.head
    )
    integration_terminal_clean = bool(
        isinstance(runtime_validation, dict)
        and runtime_validation.get("clean_worktree") is True
    )
    integration_terminal_lease_released = bool((runtime or {}).get("lease_released") is True)
    # Once terminal integration evidence is durably finalized, later repository
    # state belongs to later phases. Live planning dirtiness, a planning lease,
    # or a planning Git operation cannot rewrite the integration observation.
    phase_scoped_clean = integration_terminal_clean if historical_finalized else inspector.is_clean
    phase_scoped_no_git_operation = (
        integration_terminal_clean if historical_finalized else not any(git_operations.values())
    )
    phase_scoped_no_writer_lease = (
        integration_terminal_lease_released
        if historical_finalized
        else not writer_exists and integration_terminal_lease_released
    )
    checks = {
        "report_identity_matches": isinstance(session_report, dict)
        and type(session_report.get("schema_version")) is int
        and session_report.get("schema_version") == 1
        and session_report.get("project_id") == project.project_id
        and session_report.get("run_id") == (cycle or {}).get("conveyor_run_id")
        and session_report.get("action") == "milestone_integration"
        and report_working_directory_matches
        and integration_session_identity_matches,
        "terminal_integrated": terminal_validation == "valid" and (terminal or {}).get("classification") == "INTEGRATED",
        "zero_compatible_exit": (session_report or {}).get("exit_status") == 0,
        "runtime_record_complete": isinstance(runtime, dict) and runtime.get("phase") == "complete",
        "repository_cycle_complete": (cycle or {}).get("current_phase") == "completed",
        "runtime_identity_matches": isinstance(runtime_identity, dict)
        and runtime_identity.get("repository_identity") == inspector.identity()["repository_id"]
        and runtime_identity.get("repository") == str(project.repository)
        and runtime_identity.get("project_id") == project.project_id
        and runtime_identity.get("run_id") == (cycle or {}).get("conveyor_run_id")
        and runtime_identity.get("milestone_id") == (milestone or {}).get("id")
        and runtime_identity.get("feature_id") == feature_id,
        "integration_session_matches": integration_session_identity_matches,
        "accepted_commit_corroborated": isinstance(accepted, str)
        and accepted == (cycle or {}).get("accepted_feature_commit")
        and accepted == (runtime or {}).get("accepted_commit")
        and inspector.ref_exists(accepted),
        "integrated_commit_corroborated": isinstance(integrated, str)
        and integrated == (runtime or {}).get("resulting_feature_commit")
        and inspector.ref_exists(integrated),
        "unique_commit_corroboration": len(matching_features) == 1,
        "patch_equivalent": isinstance(accepted, str)
        and isinstance(integrated, str)
        and inspector.ref_exists(accepted)
        and inspector.ref_exists(integrated)
        and inspector.patch_fingerprint(accepted) == inspector.patch_fingerprint(integrated),
        "queue_integrated_and_passed": isinstance(feature, dict)
        and feature.get("status") == "integrated"
        and feature.get("integration_status") == "passed",
        "milestone_membership": isinstance(milestone, dict)
        and feature_id in (milestone.get("integrated_features") or [])
        and isinstance(integrated, str)
        and inspector.is_ancestor(integrated, project.milestone_branch or ""),
        "configured_validation_commands_present": all(command in runtime_argv for command in configured_commands),
        "required_integration_commands_passed": bool(runtime_commands) and not required_command_failures,
        "validated_tree_bound_to_integration_chain": validation_evidence_matches_tree
        and integration_fix_records_match,
        "factory_evidence_direct_parent_chain": direct_chain,
        "factory_evidence_subjects": factory_subjects,
        "factory_evidence_paths_only": factory_paths,
        "runtime_validation_passed": isinstance(runtime_validation, dict)
        and runtime_validation.get("ok") is True
        and runtime_validation.get("clean_worktree") is True
        and not (runtime_validation.get("blockers") or []),
        "final_validated_head_corroborated": (
            isinstance(runtime_validation, dict)
            and isinstance(final_head, str)
            and final_head == (runtime or {}).get("model_evidence_commit")
            and runtime_validation_commit in {validated_tree, final_head}
        ),
        "milestone_head_matches_final_validation": current_head_is_allowed,
        "clean_repository": phase_scoped_clean,
        "integration_terminal_repository_clean": integration_terminal_clean,
        "no_git_operation": phase_scoped_no_git_operation,
        "integration_terminal_no_unfinished_git_operation": phase_scoped_no_git_operation,
        "no_writer_lease": phase_scoped_no_writer_lease,
        "integration_terminal_lease_released": integration_terminal_lease_released,
        "runtime_has_no_blockers": not ((runtime_validation or {}).get("blockers") or []),
    }
    warning_list = list(optional_warnings)
    observed_last_validated = milestone.get("last_validated_commit") if isinstance(milestone, dict) else None
    if isinstance(final_head, str) and observed_last_validated != final_head:
        warning_list.append("queue_last_validated_commit_does_not_match_final_validated_milestone_head")
    return {
        "success": all(checks.values()),
        "checks": checks,
        "feature_id": feature_id or None,
        "accepted_commit": accepted,
        "integrated_commit": integrated,
        "terminal_commit": final_head,
        "milestone_post_integration_head": final_head,
        "runtime_phase": (runtime or {}).get("phase"),
        "runtime_stop_reason": (runtime or {}).get("stop_reason"),
        "required_command_failures": required_command_failures,
        "post_integration_commands": runtime_required_observations + list(observations),
        "optional_warnings": warning_list,
        "last_validated_commit_semantics": "final_validated_milestone_head",
        "effective_last_validated_commit": final_head,
        "queue_last_validated_commit_observed": observed_last_validated,
        "application_metadata_mutated": False,
        "runtime_record_path": str(runtime_path),
        "historical_finalization": historical_finalized,
        "current_repository_clean": inspector.is_clean,
        "current_repository_git_operations": git_operations,
        "current_repository_writer_lease": writer_exists,
        "later_planning_commits": later_commits,
        "validated_tree_commit": validated_tree,
        "integration_fix_commits": integration_fix_commits,
    }
def assess_startup_reconciliation(
    project: Project,
    persisted: dict[str, Any] | None,
    plan: dict[str, Any],
) -> StartupReconciliation:
    """Compare durable controller intent with current read-only repository evidence."""

    persisted_state = str((persisted or {}).get("current_state") or project.current_state)
    queue = plan.get("queue_status") or {}
    repository = plan.get("repository_state") or {}
    locks = plan.get("lock_status") or {}
    active_cycle = plan.get("existing_active_cycle")
    queue_classification = queue.get("reconciliation_classification", "invalid_queue")
    selected_feature = plan.get("selected_feature")
    milestone_complete = queue.get("milestone_complete") is True
    current_head = repository.get("head")
    state_evidence = (persisted or {}).get("state_evidence")
    evidence = {
        "queue_classification": queue_classification,
        "queue_valid": queue.get("valid") is True,
        "milestone_complete": milestone_complete,
        "selected_feature": selected_feature,
        "milestone_head": current_head,
        "queue_fingerprint": _queue_fingerprint(queue.get("resolved_queue_path")),
        "prior_state_decision_commit": (
            state_evidence.get("milestone_head")
            if isinstance(state_evidence, dict)
            else project.last_accepted_commit
        ),
        "prior_state_decision_checkpoint": (persisted or {}).get("last_checkpoint"),
        "prior_state_decision_timestamp": (persisted or {}).get("updated_at"),
        "prior_gate_evidence": state_evidence if isinstance(state_evidence, dict) else None,
        "git_operations": repository.get("git_operations"),
        "repository_clean": repository.get("clean"),
        "repository_branch": repository.get("branch"),
        "repository_cycle_state_exists": repository.get("cycle_state_exists"),
        "repository_worktrees": repository.get("worktrees"),
        "repository_local_branches": repository.get("local_branches"),
        "locks": locks,
        "stale_cycle_evidence": plan.get("stale_cycle_evidence"),
        "durable_integration_success": plan.get("durable_integration_success"),
    }
    deterministic_failure = plan.get("stale_cycle_evidence")
    deterministic_failure = (
        deterministic_failure
        if isinstance(deterministic_failure, dict)
        and deterministic_failure.get("classification") == "deterministic_failed_cycle"
        else None
    )
    durable_integration = plan.get("durable_integration_success")
    authoritative_integration_failure = bool(
        isinstance(durable_integration, dict)
        and (durable_integration.get("checks") or {}).get("terminal_integrated") is True
        and durable_integration.get("success") is not True
    )

    if active_cycle:
        if active_cycle.get("phase") == "invalid":
            return StartupReconciliation(
                "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
                "repository cycle state is unreadable or invalid", evidence,
                {"reason": "invalid repository cycle state", "safe_action": "inspect and preserve the cycle file"},
            )
        return StartupReconciliation(
            "active_cycle_resume", persisted_state, persisted_state, (), False,
            "a corroborated repository-local cycle must resume before new work", evidence,
        )

    if authoritative_integration_failure:
        path = (persisted_state,) if persisted_state == "validation_failed" else (
            persisted_state, "validation_failed"
        )
        return StartupReconciliation(
            "required_integration_validation_failed",
            persisted_state,
            "validation_failed",
            path,
            persisted_state != "validation_failed",
            "required post-integration validation or evidence finalization has not passed",
            evidence,
        )

    if deterministic_failure and not deterministic_failure.get("environment_remediation_verified"):
        path = (persisted_state,) if persisted_state == "human_decision_required" else (
            persisted_state, "human_decision_required"
        )
        return StartupReconciliation(
            "deterministic_failure_human_gate",
            persisted_state,
            "human_decision_required",
            path,
            persisted_state != "human_decision_required",
            "the failed session is non-retryable until Codex compatibility remediation is verified",
            evidence,
            {
                "reason": "local Codex compatibility remediation is required",
                "classification": deterministic_failure.get("failure_classification"),
                "attempts_consumed": deterministic_failure.get("attempts_consumed"),
                "environment_remediation_verified": False,
                "safe_action": f"scripts/conveyor doctor --project {project.project_id}",
                "safe_continuation_command": f"scripts/conveyor resume --project {project.project_id}",
                "old_session_will_resume": False,
            },
        )

    planning = plan.get("current_planning_transaction")
    if (
        isinstance(planning, dict)
        and planning.get("status") in {
            "planning_changes_pending_validation",
            "planning_changes_validated",
            "planning_changes_committing",
        }
        and repository.get("branch") == project.milestone_branch
        and repository.get("head") == repository.get("milestone_branch_head")
        and not any((repository.get("git_operations") or {}).values())
    ):
        evidence["current_planning_transaction"] = planning
        return StartupReconciliation(
            "planning_transaction_pending",
            persisted_state,
            persisted_state,
            (persisted_state,),
            False,
            "authorized planning changes are pending phase-scoped finalization",
            evidence,
        )

    writer = locks.get("repository_writer") or {}
    launch = locks.get("controller_launch") or {}
    if writer.get("exists") or launch.get("exists"):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "state repair is blocked by an existing writer or controller reservation", evidence,
            {"reason": "lock ownership must be reconciled before state repair"},
        )
    if (
        repository.get("clean") is not True
        or any((repository.get("git_operations") or {}).values())
        or repository.get("branch") != project.milestone_branch
        or repository.get("head") != repository.get("milestone_branch_head")
        or repository.get("baseline_exists") is not True
        or repository.get("milestone_branch_exists") is not True
        or repository.get("baseline_is_ancestor_of_milestone") is not True
    ):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "startup state repair requires a clean repository at the configured milestone HEAD with verified ancestry and no active Git operation",
            evidence,
            {"reason": "preserve repository work and reconcile milestone-branch Git evidence"},
        )
    if queue.get("valid") is not True or queue_classification == "invalid_queue":
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "validation_failed", (), False,
            "queue parsing did not produce valid deterministic evidence", evidence,
        )

    if persisted is None and persisted_state == "queue_reconciliation":
        return StartupReconciliation(
            "state_consistent", persisted_state, persisted_state, (persisted_state,), False,
            "no durable project state exists; the configured queue-reconciliation entry state remains authoritative",
            evidence,
        )

    targets = {
        "reconciled_ready_work": "feature_ready",
        "reconciled_no_ready_work": "paused",
        "milestone_complete": "milestone_gate",
        "legitimately_blocked": "paused",
        "human_decision_required": "human_decision_required",
    }
    derived_state = targets.get(str(queue_classification), "validation_failed")
    if (
        queue_classification == "reconciled_no_ready_work"
        and (queue.get("status_counts") or {}).get("proposed", 0)
        and persisted_state != "paused"
    ):
        derived_state = "queue_reconciliation"
    if derived_state == "feature_ready" and not selected_feature:
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
            "ready-work classification has no deterministic selected feature", evidence,
            {"reason": "queue classification and feature selection contradict each other"},
        )
    if milestone_complete and derived_state != "milestone_gate":
        return StartupReconciliation(
            "invalid_state_evidence", persisted_state, "human_decision_required", (), False,
            "milestone completion contradicts the queue reconciliation classification", evidence,
            {"reason": "contradictory milestone evidence"},
        )

    if persisted_state == derived_state:
        return StartupReconciliation(
            "state_consistent", persisted_state, derived_state, (persisted_state,), False,
            "persisted state agrees with verified repository and queue evidence", evidence,
        )

    if persisted_state == "milestone_ready_for_merge":
        prior = state_evidence if isinstance(state_evidence, dict) else {}
        human_gate = (persisted or {}).get("human_decision_required")
        prior_head = prior.get("milestone_head") or prior.get("gate_evidence_commit")
        prior_queue = prior.get("queue_fingerprint")
        gate_timestamp = prior.get("gate_evidence_timestamp")
        inspector = RepositoryInspector(project.repository)
        head_advanced_after_gate = bool(
            isinstance(prior_head, str)
            and isinstance(current_head, str)
            and prior_head != current_head
            and inspector.ref_exists(prior_head)
            and inspector.is_ancestor(prior_head, current_head)
            and _timestamp_after(repository.get("head_commit_timestamp"), gate_timestamp)
        )
        queue_changed = bool(
            isinstance(prior_queue, str) and prior_queue != evidence["queue_fingerprint"]
        )
        inputs_changed = head_advanced_after_gate
        policy_allows, policy_reason = _gate_reopen_policy(project)
        provenance_valid = bool(
            (persisted or {}).get("last_checkpoint") == "milestone_gate_passed"
            and isinstance(human_gate, dict)
            and human_gate.get("milestone_branch") == project.milestone_branch
            and human_gate.get("default_branch_merge_performed") is False
            and prior.get("gate_evidence_commit") == prior.get("milestone_head")
            and prior.get("gate_evidence_timestamp")
        )
        evidence["milestone_inputs_changed_after_gate"] = inputs_changed
        evidence["milestone_head_advanced_after_gate"] = head_advanced_after_gate
        evidence["queue_changed_after_gate"] = queue_changed
        evidence["gate_reopen_policy"] = policy_reason
        evidence["gate_evidence_provenance_valid"] = provenance_valid
        if not inputs_changed or not policy_allows or not gate_timestamp or not provenance_valid:
            return StartupReconciliation(
                "human_decision_required", persisted_state, "human_decision_required", (), False,
                "a passed milestone gate cannot be reopened without changed recorded inputs, timestamped gate evidence, and policy authority",
                evidence,
                {
                    "reason": "milestone-ready state requires explicit gate-evidence validation",
                    "inputs_changed": inputs_changed,
                    "policy_allows_reopen": policy_allows,
                },
            )

    if (
        persisted_state == "human_decision_required"
        and not (deterministic_failure and deterministic_failure.get("environment_remediation_verified"))
    ):
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            "the recorded human decision has not been explicitly resolved", evidence,
            {"reason": "explicit resolution evidence is required"},
        )

    repairable = {
        "milestone_gate", "milestone_ready_for_merge", "feature_ready", "feature_running",
        "feature_accepted", "queue_reconciliation", "validation_failed", "human_decision_required",
        "repository_dirty", "architecture_decision_required", "conveyor_error", "paused",
    }
    if persisted_state not in repairable:
        return StartupReconciliation(
            "human_decision_required", persisted_state, "human_decision_required", (), False,
            f"no safe startup-reconciliation route is defined from {persisted_state}", evidence,
            {"reason": "unsupported persisted state for automatic repair"},
        )

    path = [persisted_state]
    current_feature = (persisted or {}).get("current_feature")
    completed = set(queue.get("completed_features") or [])
    if persisted_state == "feature_running" and current_feature and current_feature in completed:
        path.append("feature_accepted")
    if deterministic_failure and path[-1] != "human_decision_required":
        path.append("human_decision_required")
    if path[-1] != "queue_reconciliation":
        path.append("queue_reconciliation")
    if derived_state != "queue_reconciliation":
        path.append(derived_state)
    return StartupReconciliation(
        "stale_state_repaired", persisted_state, derived_state, tuple(path), True,
        (
            f"persisted {persisted_state} no longer matches verified {queue_classification} evidence; "
            "the obsolete execution intent must be invalidated before scheduling"
        ),
        evidence,
    )


def assess_recovery(project: Project, state: dict[str, Any] | None) -> RecoveryAssessment:
    inspector = RepositoryInspector(project.repository)
    current = inspector.inspect(
        baseline=project.validated_baseline_commit,
        milestone_branch=project.milestone_branch,
    )
    if state is None:
        return RecoveryAssessment("no_active_cycle", None, {"git": current}, None)

    identity = current["identity"]
    conflicts: list[str] = []
    if state.get("repository_path_fingerprint") != identity["path_fingerprint"]:
        conflicts.append("repository path fingerprint disagrees with durable cycle state")
    recorded_identity = state.get("repository_identity", {})
    if isinstance(recorded_identity, dict) and recorded_identity.get("repository_id") != identity["repository_id"]:
        conflicts.append("repository identity disagrees with durable cycle state")
    accepted = state.get("accepted_feature_commit")
    if isinstance(accepted, str) and not inspector.ref_exists(accepted):
        conflicts.append("recorded accepted feature commit does not exist")

    phase = state.get("current_phase")
    operations = [name for name, active in current["git_operations"].items() if active]
    integration_phases = {"integration_pending", "integration_ready", "integrating", "integration_validation"}
    if operations and phase not in integration_phases:
        conflicts.append("active Git operation is inconsistent with the recorded cycle phase")

    feature_phases = {"branch_preparing", "feature_in_progress", "feature_review", "feature_repair"}
    expected_feature_branch = state.get("feature_branch")
    if (
        not conflicts
        and phase in feature_phases
        and isinstance(expected_feature_branch, str)
        and (
            not inspector.ref_exists(expected_feature_branch)
            or current.get("branch") != expected_feature_branch
            or inspector.branch_worktree(expected_feature_branch) != project.repository.resolve()
        )
    ):
        return RecoveryAssessment(
            "branch_recovery_required",
            None,
            {"git": current, "state": state},
            {
                "reason": "persisted feature branch is not the actual repository session branch",
                "actual_branch": current.get("branch"),
                "expected_branch": expected_feature_branch,
                "branch_recovery_required": True,
                "resume_allowed": False,
                "safe_action": f"scripts/conveyor recover-feature-branch --project {project.project_id}",
            },
        )

    last_git = state.get("last_verified_git_state")
    if isinstance(last_git, dict):
        expected_branch = last_git.get("branch")
        if expected_branch and current["branch"] != expected_branch and not operations:
            conflicts.append("current branch differs from the last verified checkpoint")

    if conflicts:
        decision = {
            "conflicting_evidence": conflicts,
            "branches_and_commits": {
                "current_branch": current["branch"],
                "current_head": current["head"],
                "feature_branch": state.get("feature_branch"),
                "accepted_feature_commit": accepted,
                "milestone_branch": state.get("milestone_branch"),
                "milestone_pre_integration_commit": state.get("milestone_pre_integration_commit"),
                "milestone_post_integration_commit": state.get("milestone_post_integration_commit"),
            },
            "git_operations": operations,
            "affected_files": inspector.dirty_entries,
            "safe_actions": ["inspect", "preserve current work", "supply a non-destructive reconciliation decision"],
            "recommended_action": "Preserve all refs and worktrees, reconcile the listed evidence, then resume.",
            "resume_command": f"scripts/conveyor resume --project {project.project_id}",
        }
        return RecoveryAssessment("human_decision_required", None, {"git": current, "state": state}, decision)

    if phase == "completed":
        return RecoveryAssessment("already_completed", None, {"git": current, "state": state}, None)
    if not isinstance(phase, str):
        raise RecoveryError("cycle state does not contain a valid phase")
    return RecoveryAssessment("resume", phase, {"git": current, "state": state}, None)
