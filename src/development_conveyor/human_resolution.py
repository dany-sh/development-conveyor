"""Deterministic validation for explicit human-decision gate resolution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import socket
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

    gate = project.human_decision_gate if isinstance(project.human_decision_gate, dict) else None
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
