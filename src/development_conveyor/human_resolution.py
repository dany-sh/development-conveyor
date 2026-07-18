"""Deterministic validation for explicit human-decision gate resolution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import load_json
from .locks import process_alive
from .queue import resolve_queue_path
from .registry import Project
from .repository import RepositoryInspector


def canonical_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gate_fingerprint(gate: dict[str, Any]) -> str:
    return canonical_fingerprint(gate)


def resolution_fingerprint(project_id: str, gate: dict[str, Any], reason: str) -> str:
    return canonical_fingerprint({
        "actor_classification": "explicit_user_approval",
        "gate_fingerprint": gate_fingerprint(gate),
        "project_id": project_id,
        "reason": reason.strip(),
    })


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _runtime_record_path(common_git_dir: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str):
        return None
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        return None
    common = common_git_dir.resolve()
    unresolved = common / value
    try:
        unresolved.absolute().relative_to(common)
    except ValueError:
        return None
    current = common
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            return None
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(common)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _load_pinned_record(
    common_git_dir: Path, relative: Any, expected_sha256: Any
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = _runtime_record_path(common_git_dir, relative)
    if path is None:
        return None, {"path": str(relative), "exists": False, "sha256_matches": False}
    try:
        payload = path.read_bytes()
    except OSError:
        return None, {"path": str(path), "exists": True, "readable": False, "sha256_matches": False}
    digest = hashlib.sha256(payload).hexdigest()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    return (
        value if isinstance(value, dict) else None,
        {
            "path": str(path),
            "exists": True,
            "regular_non_symlink": True,
            "sha256": digest,
            "sha256_matches": digest == expected_sha256,
            "object": isinstance(value, dict),
        },
    )


def _integration_record_valid(
    value: dict[str, Any] | None, project: Project, gate: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    value = value or {}
    validation = value.get("validation") if isinstance(value.get("validation"), dict) else {}
    commands = validation.get("commands") if isinstance(validation.get("commands"), list) else []
    command_results_valid = bool(commands) and all(
        isinstance(item, dict) and item.get("exit_code") == 0 for item in commands
    )
    facts = {
        "schema_version_supported": value.get("schema_version") == 1,
        "repository_matches": value.get("repository") == str(project.repository.resolve()),
        "feature_matches": value.get("feature_id") == gate.get("feature_id"),
        "milestone_matches": value.get("milestone_id") == gate.get("expected_milestone"),
        "branch_matches": value.get("milestone_branch") == gate.get("expected_branch"),
        "accepted_commit_matches": value.get("accepted_commit") == gate.get("accepted_commit"),
        "integrated_commit_matches": value.get("post_integration_head") == gate.get("integrated_commit"),
        "phase_validated": value.get("phase") == "validated",
        "validation_ok": validation.get("ok") is True,
        "validated_commit_matches": validation.get("validated_commit") == gate.get("integrated_commit"),
        "validation_feature_matches": validation.get("feature_id") == gate.get("feature_id"),
        "validation_milestone_matches": validation.get("milestone_id") == gate.get("expected_milestone"),
        "validation_branch_matches": validation.get("milestone_branch") == gate.get("expected_branch"),
        "validation_commands_passed": command_results_valid,
        "validation_clean_worktree": validation.get("clean_worktree") is True,
        "validation_blockers_absent": validation.get("blockers") == [],
    }
    return all(facts.values()), facts


def _recovery_record_valid(
    value: dict[str, Any] | None, project: Project, gate: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    value = value or {}
    lease = value.get("lease") if isinstance(value.get("lease"), dict) else {}
    investigation = value.get("investigation") if isinstance(value.get("investigation"), dict) else {}
    runtime = investigation.get("runtime") if isinstance(investigation.get("runtime"), dict) else {}
    validation = runtime.get("validation") if isinstance(runtime.get("validation"), dict) else {}
    commands = validation.get("commands") if isinstance(validation.get("commands"), list) else []
    command_results_valid = bool(commands) and all(
        isinstance(item, dict) and item.get("exit_code") == 0 for item in commands
    )
    facts = {
        "runtime_schema_version_supported": runtime.get("schema_version") == 1,
        "released_at_valid": _timestamp(value.get("released_at")),
        "release_reason_present": isinstance(value.get("reason"), str) and len(value["reason"].strip()) >= 8,
        "lease_repository_matches": lease.get("repository") == str(project.repository.resolve()),
        "lease_worktree_matches": lease.get("worktree") == str(project.repository.resolve()),
        "lease_feature_matches": lease.get("feature_id") == gate.get("feature_id"),
        "lease_branch_matches": lease.get("branch") == gate.get("expected_branch"),
        "lease_purpose_integration": lease.get("purpose") == "integration",
        "lease_pid_matches": lease.get("pid") == gate.get("retained_process_id"),
        "lease_host_local": lease.get("host") == socket.gethostname(),
        "lease_agent_run_present": isinstance(lease.get("agent_run"), str) and bool(lease["agent_run"]),
        "investigation_repository_matches": investigation.get("repository") == str(project.repository.resolve()),
        "investigation_branch_matches": investigation.get("branch") == gate.get("expected_branch"),
        "investigation_head_matches": investigation.get("head") == gate.get("expected_head"),
        "investigation_clean": investigation.get("dirty_entries") == [],
        "investigation_git_operations_absent": investigation.get("unfinished_operations") == [],
        "investigation_lease_matches": investigation.get("writer_lease") == lease,
        "investigation_process_recorded_dead": investigation.get("lease_process_alive") is False,
        "timestamp_alone_not_used": investigation.get("time_alone_proves_stale") is False,
        "destructive_actions_absent": investigation.get("destructive_actions_taken") is False,
        "runtime_feature_matches": runtime.get("feature_id") == gate.get("feature_id"),
        "runtime_repository_matches": runtime.get("repository") == str(project.repository.resolve()),
        "runtime_worktree_matches": runtime.get("worktree") == str(project.repository.resolve()),
        "runtime_milestone_matches": runtime.get("milestone_id") == gate.get("expected_milestone"),
        "runtime_branch_matches": runtime.get("milestone_branch") == gate.get("expected_branch"),
        "runtime_current_branch_matches": runtime.get("current_branch") == gate.get("expected_branch"),
        "runtime_accepted_commit_matches": runtime.get("accepted_commit") == gate.get("accepted_commit"),
        "runtime_integrated_commit_matches": runtime.get("post_integration_head") == gate.get("integrated_commit"),
        "runtime_phase_validated": runtime.get("phase") == "validated",
        "runtime_validation_ok": validation.get("ok") is True,
        "runtime_validation_feature_matches": validation.get("feature_id") == gate.get("feature_id"),
        "runtime_validation_milestone_matches": validation.get("milestone_id") == gate.get("expected_milestone"),
        "runtime_validation_branch_matches": validation.get("milestone_branch") == gate.get("expected_branch"),
        "runtime_validation_commit_matches": validation.get("validated_commit") == gate.get("integrated_commit"),
        "runtime_validation_commands_passed": command_results_valid,
        "runtime_validation_clean_worktree": validation.get("clean_worktree") is True,
        "runtime_validation_blockers_absent": validation.get("blockers") == [],
    }
    return all(facts.values()), facts


def _evaluate_planning_baseline_resolution(
    *,
    project: Project,
    persisted: dict[str, Any] | None,
    gate: dict[str, Any],
    reason: str,
    inspector: RepositoryInspector,
    writer_lock_exists: bool,
    controller_reservation_exists: bool,
    reason_rejected_sensitive: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: Any) -> None:
        checks.append({"validator": name, "passed": bool(passed), "evidence": evidence})

    reason = reason.strip()
    check("approval_reason_present", bool(reason), {"present": bool(reason)})
    check("approval_reason_sensitive_content_absent", not reason_rejected_sensitive, {
        "sensitive_content_detected": reason_rejected_sensitive,
    })
    fingerprint = gate_fingerprint(gate)
    active = (persisted or {}).get("human_decision_required")
    active_fingerprint = (
        active.get("gate_fingerprint") or gate_fingerprint(active)
        if isinstance(active, dict) else None
    )
    check("project_in_human_decision_state", (persisted or {}).get("current_state") == "human_decision_required", {
        "current_state": (persisted or {}).get("current_state"),
    })
    check("active_gate_matches_persisted_gate", isinstance(active, dict) and active.get("gate_id") == gate.get("gate_id") and active_fingerprint == fingerprint, {
        "gate_id": gate.get("gate_id"), "fingerprint_matches": active_fingerprint == fingerprint,
    })
    identity = inspector.identity()
    repository = inspector.inspect(
        baseline=project.validated_baseline_commit, milestone_branch=project.milestone_branch
    )
    check("repository_identity_matches", identity.get("repository_id") == gate.get("expected_repository_id"), {
        "matches": identity.get("repository_id") == gate.get("expected_repository_id"),
    })
    check("repository_path_fingerprint_matches", identity.get("path_fingerprint") == gate.get("expected_path_fingerprint"), {
        "matches": identity.get("path_fingerprint") == gate.get("expected_path_fingerprint"),
    })
    check("accepted_feature_branch_preserved", repository.get("branch") == gate.get("feature_branch"), {
        "branch": repository.get("branch"), "expected": gate.get("feature_branch"),
    })
    check("accepted_feature_head_preserved", repository.get("head") == gate.get("accepted_feature_commit"), {
        "head": repository.get("head"), "expected": gate.get("accepted_feature_commit"),
    })
    check("milestone_ref_preserved", repository.get("milestone_branch_head") == gate.get("milestone_head"), {
        "head": repository.get("milestone_branch_head"), "expected": gate.get("milestone_head"),
    })
    check("worktree_clean", repository.get("clean") is True, {"clean": repository.get("clean")})
    check("git_operations_absent", not any((repository.get("git_operations") or {}).values()), repository.get("git_operations"))
    check("repository_writer_lease_absent", not writer_lock_exists, {"exists": writer_lock_exists})
    check("controller_reservation_absent", not controller_reservation_exists, {"exists": controller_reservation_exists})
    candidate = gate.get("candidate_validated_planning_commit")
    previous = gate.get("previous_last_validated_commit")
    candidate_exists = isinstance(candidate, str) and inspector.ref_exists(candidate)
    check("planning_candidate_exists", candidate_exists, {"commit": candidate, "exists": candidate_exists})
    ancestry = bool(candidate_exists and isinstance(previous, str) and inspector.ref_exists(previous) and inspector.is_ancestor(previous, candidate))
    check("planning_candidate_descends_from_previous_validated_commit", ancestry, {"previous": previous, "candidate": candidate, "is_ancestor": ancestry})
    check("planning_candidate_is_milestone_head", candidate == repository.get("milestone_branch_head"), {
        "candidate": candidate, "milestone_head": repository.get("milestone_branch_head"),
    })
    approved_prefixes = (
        ".factory/project.yaml", "docs/FEATURE_QUEUE.yaml", "docs/FEATURE_CATALOG.md",
        "docs/CURRENT_STATUS.md", "docs/RUN_LOG.md", "docs/README.md", "docs/ROADMAP.md",
        "docs/roadmap/", "docs/features/", "docs/product/", "docs/architecture/",
        "docs/architecture.md", "docs/data-flow.md", "docs/testing/",
    )
    changed = inspector.changed_paths(str(candidate)) if candidate_exists else []
    paths_allowed = bool(changed) and all(
        path in approved_prefixes or any(path.startswith(prefix) for prefix in approved_prefixes if prefix.endswith("/"))
        for path in changed
    )
    product_tests_absent = not any(path.startswith(("Tests/", "tests/", "Sources/", "src/")) for path in changed)
    check("planning_paths_confined", paths_allowed, {"changed_paths": changed})
    check("production_and_product_test_implementation_absent", product_tests_absent, {"restricted_paths_present": not product_tests_absent})
    queue_path = resolve_queue_path(project.repository, project.queue_location)
    queue = load_json(queue_path)
    features = queue.get("features") if isinstance(queue.get("features"), list) else []
    matching = [item for item in features if isinstance(item, dict) and item.get("id") == gate.get("feature_id")]
    feature = matching[0] if len(matching) == 1 else {}
    queue_valid = len(matching) == 1 and feature.get("status") == "integration_pending" and feature.get("accepted_commit") == "SELF"
    check("integration_pending_queue_evidence_matches", queue_valid, {
        "match_count": len(matching), "status": feature.get("status"), "accepted_commit": feature.get("accepted_commit"),
    })
    start = gate.get("feature_starting_commit")
    accepted = gate.get("accepted_feature_commit")
    unique = bool(
        isinstance(start, str) and isinstance(accepted, str)
        and inspector.ref_exists(accepted) and inspector.commit_count(start, accepted) == 1
        and inspector.rev_parse(str(gate.get("feature_branch")), check=False) == accepted
    )
    check("self_accepted_commit_resolves_uniquely", unique, {"accepted_commit": accepted, "starting_commit": start})
    cycle: dict[str, Any] | None = None
    try:
        cycle_value = load_json(inspector.cycle_state_path())
        cycle = cycle_value if isinstance(cycle_value, dict) else None
    except (OSError, ValueError):
        cycle = None
    cycle_matches = bool(
        cycle
        and cycle.get("accepted_feature_commit") == accepted
        and cycle.get("feature_starting_commit") == candidate
        and cycle.get("milestone_pre_integration_commit") == candidate
        and cycle.get("human_decision_required", {}).get("gate_id") == gate.get("gate_id")
    )
    check("persisted_cycle_gate_and_commits_match", cycle_matches, {"matches": cycle_matches})
    evidence = gate.get("planning_baseline_evidence")
    evidence_valid = isinstance(evidence, dict) and evidence.get("commit") == candidate and evidence.get("previous_validated_commit") == previous and evidence.get("feature_starting_commit") == start and evidence.get("milestone_pre_integration_commit") == candidate
    evidence_fingerprint = canonical_fingerprint(evidence) if isinstance(evidence, dict) else None
    check("planning_baseline_evidence_fingerprint_matches", evidence_valid and evidence_fingerprint == gate.get("planning_baseline_evidence_fingerprint"), {
        "matches": evidence_valid and evidence_fingerprint == gate.get("planning_baseline_evidence_fingerprint"),
    })
    integrator = Path.home() / ".agents/skills/milestone-integrator/scripts/integrationctl.py"
    try:
        integrator_text = integrator.read_text(encoding="utf-8")
    except OSError:
        integrator_text = ""
    runtime_repaired = ".factory/runtime/milestone-integration" in integrator_text
    check("sandbox_runtime_repair_installed", runtime_repaired, {"runtime_location": ".factory/runtime/milestone-integration", "installed": runtime_repaired})
    provenance: dict[str, Any] | None = None
    if runtime_repaired:
        environment = dict(os.environ)
        environment.update({
            "CONVEYOR_PROJECT_ID": project.project_id,
            "CONVEYOR_RUN_ID": str((cycle or {}).get("conveyor_run_id") or "human-resolution-validation"),
        })
        completed = subprocess.run(
            ["python3", str(integrator.with_name("discover-integration.py")), "--root", str(project.repository), "--feature", str(gate.get("feature_id"))],
            cwd=project.repository, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )
        if completed.returncode == 0:
            try:
                discovered = json.loads(completed.stdout)
            except json.JSONDecodeError:
                discovered = {}
            value = discovered.get("planning_baseline_provenance") if isinstance(discovered, dict) else None
            provenance = value if isinstance(value, dict) else None
    provenance_valid = bool(
        provenance
        and provenance.get("valid") is True
        and provenance.get("commit") == candidate
        and provenance.get("previous_validated_commit") == previous
        and provenance.get("approval_required") is True
        and isinstance(provenance.get("evidence_fingerprint"), str)
    )
    check("full_integrator_planning_provenance_revalidated", provenance_valid, {
        "valid": provenance_valid,
        "evidence_fingerprint": (provenance or {}).get("evidence_fingerprint"),
    })
    check("approved_transition_is_queue_reconciliation", gate.get("approved_next_state") == "queue_reconciliation", {
        "approved_next_state": gate.get("approved_next_state"),
    })
    return {
        "accepted": all(item["passed"] for item in checks),
        "gate": gate,
        "gate_fingerprint": fingerprint,
        "checks": checks,
        "repository_identity": identity,
        "planning_baseline_provenance": provenance,
    }


def evaluate_human_resolution(
    *,
    project: Project,
    persisted: dict[str, Any] | None,
    reason: str,
    inspector: RepositoryInspector,
    writer_lock_exists: bool,
    controller_reservation_exists: bool,
    reason_rejected_sensitive: bool = False,
) -> dict[str, Any]:
    """Return complete, non-mutating gate evidence and explicit failed checks."""

    persisted_gate = (persisted or {}).get("human_decision_required")
    gate = (
        persisted_gate
        if isinstance(persisted_gate, dict)
        and persisted_gate.get("classification") == "integration_planning_baseline_approval"
        else (project.human_decision_gate if isinstance(project.human_decision_gate, dict) else None)
    )
    if isinstance(gate, dict) and gate.get("classification") == "integration_planning_baseline_approval":
        return _evaluate_planning_baseline_resolution(
            project=project, persisted=persisted, gate=gate, reason=reason, inspector=inspector,
            writer_lock_exists=writer_lock_exists,
            controller_reservation_exists=controller_reservation_exists,
            reason_rejected_sensitive=reason_rejected_sensitive,
        )
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, evidence: Any) -> None:
        checks.append({"validator": name, "passed": bool(passed), "evidence": evidence})

    reason = reason.strip()
    check("approval_reason_present", bool(reason), {"present": bool(reason)})
    check("approval_reason_sensitive_content_absent", not reason_rejected_sensitive, {
        "sensitive_content_detected": reason_rejected_sensitive,
    })
    check("gate_identity_present", gate is not None and bool(gate.get("gate_id")), {
        "gate_id": gate.get("gate_id") if gate else None,
        "classification": gate.get("classification") if gate else None,
    })
    if gate is None:
        return {"accepted": False, "gate": None, "checks": checks}

    fingerprint = gate_fingerprint(gate)
    persisted_state = persisted.get("current_state") if persisted else project.current_state
    active_gate = persisted.get("human_decision_required") if persisted else gate
    active_gate_id = active_gate.get("gate_id") if isinstance(active_gate, dict) else None
    active_gate_fingerprint = (
        active_gate.get("gate_fingerprint") or gate_fingerprint(active_gate)
        if isinstance(active_gate, dict)
        else None
    )
    check("project_in_human_decision_state", persisted_state == "human_decision_required", {
        "current_state": persisted_state,
    })
    check("active_gate_matches_registered_gate", active_gate_id == gate.get("gate_id") and active_gate_fingerprint == fingerprint, {
        "active_gate_id": active_gate_id,
        "registered_gate_id": gate.get("gate_id"),
        "gate_fingerprint_matches": active_gate_fingerprint == fingerprint,
    })

    identity = inspector.identity()
    repository = inspector.inspect(
        baseline=project.validated_baseline_commit,
        milestone_branch=project.milestone_branch,
    )
    check("repository_identity_matches", identity.get("repository_id") == gate.get("expected_repository_id"), {
        "repository_id": identity.get("repository_id"),
        "expected_repository_id": gate.get("expected_repository_id"),
    })
    check("repository_path_fingerprint_matches", identity.get("path_fingerprint") == gate.get("expected_path_fingerprint"), {
        "path_fingerprint": identity.get("path_fingerprint"),
        "expected_path_fingerprint": gate.get("expected_path_fingerprint"),
    })
    check("milestone_matches", project.active_milestone == gate.get("expected_milestone"), {
        "registered": project.active_milestone,
        "expected": gate.get("expected_milestone"),
    })
    check("branch_matches", repository.get("branch") == gate.get("expected_branch") == project.milestone_branch, {
        "branch": repository.get("branch"),
        "expected": gate.get("expected_branch"),
    })
    check("head_matches", repository.get("head") == gate.get("expected_head"), {
        "head": repository.get("head"),
        "expected": gate.get("expected_head"),
    })
    check("milestone_ref_matches_head", repository.get("milestone_branch_head") == gate.get("expected_head"), {
        "milestone_branch_head": repository.get("milestone_branch_head"),
        "expected": gate.get("expected_head"),
    })
    check("worktree_clean", repository.get("clean") is True, {
        "clean": repository.get("clean"),
        "dirty_entry_count": repository.get("dirty_entry_count"),
    })
    operations = repository.get("git_operations") or {}
    check("git_operations_absent", not any(operations.values()), operations)
    check("repository_cycle_state_absent", repository.get("cycle_state_exists") is False, {
        "exists": repository.get("cycle_state_exists"),
    })
    check("repository_writer_lease_absent", not writer_lock_exists, {"exists": writer_lock_exists})
    check("controller_reservation_absent", not controller_reservation_exists, {
        "exists": controller_reservation_exists,
    })
    recovery, recovery_file = _load_pinned_record(
        inspector.common_git_dir, gate.get("recovery_record"), gate.get("recovery_record_sha256")
    )
    check("recovery_record_pinned", recovery is not None and recovery_file["sha256_matches"], recovery_file)
    recovery_valid, recovery_facts = _recovery_record_valid(recovery, project, gate)
    check("structured_forced_lease_release_record_v1", recovery_valid, {
        "classification": "writer_lease_forced_release_recorded" if recovery_valid else "invalid_forced_lease_release_record",
        **recovery_facts,
    })
    recovery_lease = recovery.get("lease") if isinstance(recovery, dict) and isinstance(recovery.get("lease"), dict) else {}
    retained_host = recovery_lease.get("host")
    host_local = retained_host == socket.gethostname()
    check("retained_process_host_local", host_local, {
        "host_matches_current": host_local,
    })
    pid = gate.get("retained_process_id")
    alive = process_alive(pid) if host_local and isinstance(pid, int) else None
    check("retained_process_dead", alive is False, {"process_id": pid, "alive": alive})

    integration, integration_file = _load_pinned_record(
        inspector.common_git_dir, gate.get("integration_record"), gate.get("integration_record_sha256")
    )
    check("integration_record_pinned", integration is not None and integration_file["sha256_matches"], integration_file)
    integration_valid, integration_facts = _integration_record_valid(integration, project, gate)
    check("integration_validation_recorded_successful", integration_valid, integration_facts)

    accepted = gate.get("accepted_commit")
    integrated = gate.get("integrated_commit")
    accepted_present = isinstance(accepted, str) and inspector.ref_exists(accepted)
    integrated_present = isinstance(integrated, str) and inspector.ref_exists(integrated)
    check("accepted_commit_present", accepted_present, {"commit": accepted, "present": accepted_present})
    check("integrated_commit_present", integrated_present, {"commit": integrated, "present": integrated_present})
    integrated_on_milestone = bool(
        integrated_present
        and isinstance(integrated, str)
        and inspector.is_ancestor(integrated, str(gate.get("expected_head")))
    )
    check("integrated_commit_on_expected_history", integrated_on_milestone, {
        "integrated_commit": integrated,
        "expected_head": gate.get("expected_head"),
        "is_ancestor": integrated_on_milestone,
    })

    queue_path: Path | None = None
    queue_document: dict[str, Any] = {}
    committed_matches = False
    queue_error: str | None = None
    try:
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        queue_document = load_json(queue_path)
        committed_queue = inspector.file_at_commit(
            "HEAD", queue_path.relative_to(project.repository.resolve()).as_posix()
        )
        committed_matches = committed_queue == queue_path.read_text(encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        queue_error = type(exc).__name__
    check("queue_committed_at_head", committed_matches, {
        "path": str(queue_path) if queue_path else None,
        "committed_matches_worktree": committed_matches,
        "error_classification": queue_error,
    })
    features = queue_document.get("features") if isinstance(queue_document.get("features"), list) else []
    matches = [item for item in features if isinstance(item, dict) and item.get("id") == gate.get("feature_id")]
    feature = matches[0] if len(matches) == 1 else {}
    feature_valid = len(matches) == 1 and all((
        feature.get("status") in {"done", "integrated"},
        feature.get("accepted_commit") == accepted,
        feature.get("integrated_commit") == integrated,
        feature.get("integration_status") == "passed",
    ))
    check("feature_integration_queue_evidence_matches", feature_valid, {
        "match_count": len(matches),
        "status": feature.get("status"),
        "accepted_commit_matches": feature.get("accepted_commit") == accepted,
        "integrated_commit_matches": feature.get("integrated_commit") == integrated,
        "integration_status": feature.get("integration_status"),
    })
    milestones = queue_document.get("milestones") if isinstance(queue_document.get("milestones"), list) else []
    milestone_matches = [
        item for item in milestones
        if isinstance(item, dict) and item.get("id") == gate.get("expected_milestone")
    ]
    milestone = milestone_matches[0] if len(milestone_matches) == 1 else {}
    milestone_valid = len(milestone_matches) == 1 and all((
        gate.get("feature_id") in (milestone.get("integrated_features") or []),
        milestone.get("last_validated_commit") == integrated,
    ))
    check("milestone_integration_queue_evidence_matches", milestone_valid, {
        "match_count": len(milestone_matches),
        "feature_recorded_integrated": gate.get("feature_id") in (milestone.get("integrated_features") or []),
        "last_validated_commit_matches": milestone.get("last_validated_commit") == integrated,
    })
    transition_allowed = gate.get("approved_next_state") == "queue_reconciliation"
    check("approved_transition_is_queue_reconciliation", transition_allowed, {
        "previous_state": "human_decision_required",
        "approved_next_state": gate.get("approved_next_state"),
    })
    return {
        "accepted": all(item["passed"] for item in checks),
        "gate": gate,
        "gate_fingerprint": fingerprint,
        "checks": checks,
        "repository_identity": identity,
    }
