"""Deterministic operator controls for queue order and project pausing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Configuration
from .errors import ConveyorError, QueueError
from .execution_profiles import resolve_execution_profile
from .locks import RepositoryWriterLease, inspect_repository_writer_lock
from .logging import JsonStateStore, atomic_write_bytes, utc_now
from .queue import (
    COMPLETE_STATUSES,
    FeatureQueue,
    PRIORITY_LABELS,
    priority_label,
    resolve_queue_path,
)
from .registry import Project
from .repository import RepositoryInspector


def project_state_path(configuration: Configuration, project: Project) -> Path:
    return (
        configuration.owned_path(configuration.conveyor["state_directory"])
        / "projects"
        / f"{project.project_id}.json"
    )


def project_state_store(configuration: Configuration) -> JsonStateStore:
    return JsonStateStore(configuration.root / "schemas/project-state.schema.json")


@contextmanager
def project_authority_lock(
    configuration: Configuration, project: Project
) -> Iterator[None]:
    """Serialize project-state updates with the existing transaction ledger."""

    path = (
        configuration.owned_path(configuration.conveyor["state_directory"])
        / "projects"
        / project.project_id
        / "evidence-ledger.jsonl.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def operator_paused(configuration: Configuration, project: Project) -> bool:
    state = project_state_store(configuration).read(
        project_state_path(configuration, project)
    )
    return bool(state and state.get("operator_paused") is True)


def set_operator_paused(
    configuration: Configuration,
    project: Project,
    *,
    paused: bool,
    active_transaction: str | None,
) -> dict[str, Any]:
    store = project_state_store(configuration)
    path = project_state_path(configuration, project)
    with project_authority_lock(configuration, project):
        state = store.read(path)
        if state is None:
            identity = RepositoryInspector(project.repository).identity()
            stamp = utc_now()
            state = {
                "schema_version": 1,
                "project_id": project.project_id,
                "repository_fingerprint": identity["path_fingerprint"],
                "run_id": None,
                "current_state": project.current_state,
                "active_milestone": project.active_milestone,
                "current_feature": None,
                "last_checkpoint": None,
                "stop_reason": None,
                "human_decision_required": (
                    project.human_decision_gate
                    if project.current_state == "human_decision_required"
                    else None
                ),
                "human_decision_history": [],
                "state_evidence": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        previous = bool(state.get("operator_paused") is True)
        state["operator_paused"] = paused
        state["updated_at"] = utc_now()
        store.write(path, state)
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "classification": "project_paused" if paused else "project_unpaused",
        "paused": paused,
        "previously_paused": previous,
        "active_transaction": active_transaction,
        "pause_after_current": bool(paused and active_transaction),
        "automatic_cycle_started": False,
        "model_sessions_launched": 0,
        "child_sessions_launched": 0,
    }


def queue_report(
    configuration: Configuration,
    project: Project,
    *,
    runtime: dict[str, Any] | None = None,
    scope: str = "active",
    requested_milestone: str | None = None,
) -> dict[str, Any]:
    if scope not in {"active", "unfinished", "all"}:
        raise QueueError("queue scope must be active, unfinished, or all")
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    milestone_id = project.active_milestone or ""
    active_milestone = queue.milestone(milestone_id)
    requested = requested_milestone.strip() if isinstance(requested_milestone, str) else None
    requested = requested or None
    requested_resolved = queue.milestone(requested) if requested else None
    if requested and requested_resolved is None:
        return {
            "schema_version": 1,
            "project_id": project.project_id,
            "classification": "unknown_milestone",
            "scope": scope,
            "requested_milestone": requested,
            "active_milestone": milestone_id,
            "error": {
                "code": "unknown_milestone",
                "message": f"Unknown milestone: {requested}",
                "milestone": requested,
            },
            "read_only": True,
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
        }

    if scope == "active":
        scoped_features = queue.features_for_milestone(milestone_id)
    elif scope == "unfinished":
        scoped_features = [feature for feature in queue.features if feature["status"] not in COMPLETE_STATUSES]
    else:
        scoped_features = list(queue.features)
    visible_features = scoped_features
    if requested_resolved is not None:
        visible_features = [
            feature for feature in scoped_features
            if feature["milestone"] == requested_resolved["id"]
        ]
    runtime = runtime or {}
    current = runtime.get("current_feature")
    selected = runtime.get("selected_next_feature")
    if selected is None:
        selected = runtime.get("selected_feature")
    rows: list[dict[str, Any]] = []
    queue_positions = {feature["id"]: position for position, feature in enumerate(queue.features, start=1)}
    for feature in visible_features:
        readiness = queue.readiness(feature)
        ready_transition_reason = _ready_transition_reason(queue, feature)
        active_member = feature["milestone"] == (active_milestone or {}).get("id")
        execution_eligible = bool(active_member and readiness["ready"])
        if not active_member:
            execution_reason = (
                f"Feature is in milestone {feature['milestone']}; execution is restricted to "
                f"active milestone {milestone_id}."
            )
            ready_transition_reason = execution_reason
        else:
            execution_reason = readiness["blocked_reason"]
        profile = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
            feature=feature,
            project_id=project.project_id,
            configuration=configuration.execution_profiles or None,
        )
        rows.append(
            {
                "feature_id": feature["id"],
                "title": feature["title"],
                "milestone": feature["milestone"],
                "status": feature["status"],
                "description": feature.get("description") or feature.get("summary") or "",
                "specification_path": str(
                    project.repository / str(feature.get("spec") or feature.get("specification") or "")
                ) if (feature.get("spec") or feature.get("specification")) else None,
                "derived_column": _kanban_column(feature, readiness, current=current),
                "kanban_column": _kanban_column(feature, readiness, current=current),
                "priority": priority_label(feature.get("priority")),
                "queue_position": queue_positions[feature["id"]],
                "dependencies": list(feature.get("dependencies", [])),
                "dependencies_complete": readiness["dependencies_complete"],
                "readiness": "ready" if readiness["ready"] else "not_ready",
                "blocked_reason": readiness["blocked_reason"],
                "ready_transition_eligible": active_member and ready_transition_reason is None,
                "ready_transition_reason": ready_transition_reason,
                "active_milestone_member": active_member,
                "execution_eligible": execution_eligible,
                "execution_ineligible_reason": None if execution_eligible else execution_reason,
                "selected": feature["id"] == selected,
                "current": feature["id"] == current,
                "execution_profile": profile.to_dict(),
                "execution_model": profile.model,
                "reasoning": profile.reasoning,
                "branch": feature.get("branch"),
                "commit": feature.get("integrated_commit") or feature.get("accepted_commit"),
                "latest_terminal_result": _latest_terminal_result(runtime, feature["id"]),
                "priority_position": queue_positions[feature["id"]],
            }
        )
    next_feature = queue.select_next(milestone_id)
    milestones = []
    for milestone in queue.milestones:
        milestone_features = queue.features_for_milestone(milestone["id"])
        milestones.append({
            "milestone_id": milestone["id"],
            "title": milestone.get("title") or milestone.get("name"),
            "total_count": len(milestone_features),
            "unfinished_count": sum(feature["status"] not in COMPLETE_STATUSES for feature in milestone_features),
            "ready_count": sum(queue.readiness(feature)["ready"] for feature in milestone_features),
            "blocked_count": sum(
                _kanban_column(feature, queue.readiness(feature), current=current) == "Blocked"
                for feature in milestone_features
            ),
            "completed_count": sum(feature["status"] in COMPLETE_STATUSES for feature in milestone_features),
            "active": milestone["id"] == (active_milestone or {}).get("id"),
        })
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "scope": scope,
        "requested_milestone": requested_resolved["id"] if requested_resolved else None,
        "active_milestone": milestone_id,
        "paused": operator_paused(configuration, project),
        "active_feature": current,
        "selected_feature": selected,
        "next_ready_feature": next_feature.feature_id if next_feature else None,
        "deterministic_selection_reason": (
            next_feature.reason
            if next_feature
            else "no_ready_work: no active-milestone feature satisfies every deterministic readiness rule"
        ),
        "classification": "ready_work" if next_feature else "no_ready_work",
        "no_ready_reasons": [] if next_feature else queue.no_ready_reasons(milestone_id),
        "features": rows,
        "total_feature_count": len(queue.features),
        "scoped_feature_count": len(scoped_features),
        "visible_nonterminal_count": sum(feature["status"] not in COMPLETE_STATUSES for feature in visible_features),
        "terminal_feature_count": sum(feature["status"] in COMPLETE_STATUSES for feature in scoped_features),
        "milestones": milestones,
        "queue_path": str(resolve_queue_path(project.repository, project.queue_location)),
        "read_only": True,
        "model_sessions_launched": 0,
        "child_sessions_launched": 0,
    }


def _latest_terminal_result(runtime: dict[str, Any], feature_id: str) -> Any:
    results = runtime.get("latest_terminal_results")
    if isinstance(results, dict):
        return results.get(feature_id)
    terminal = runtime.get("latest_terminal_result")
    if isinstance(terminal, dict) and terminal.get("feature_id") in {None, feature_id}:
        return terminal
    return None


def _kanban_column(
    feature: dict[str, Any], readiness: dict[str, Any], *, current: str | None
) -> str:
    if feature["id"] == current:
        return "Running"
    status = str(feature.get("status"))
    if status in COMPLETE_STATUSES:
        return "Done"
    if status == "proposed":
        if (
            not readiness.get("dependencies_complete")
            or feature.get("requires_human_decision") is True
            or bool(feature.get("blocked_reason") or feature.get("blocking_reason"))
        ):
            return "Blocked"
        return "Backlog"
    if status == "ready" and readiness["ready"]:
        return "Ready"
    if readiness.get("blocked_reason") or status != "ready":
        return "Blocked"
    return "Backlog"


def _ready_transition_reason(queue: FeatureQueue, feature: dict[str, Any]) -> str | None:
    if feature.get("status") != "proposed":
        return "Feature is not in Backlog."
    if feature.get("requires_human_decision") is True:
        return "Feature requires a human decision."
    explicit_blocker = feature.get("blocked_reason") or feature.get("blocking_reason")
    if isinstance(explicit_blocker, str) and explicit_blocker.strip():
        return explicit_blocker.strip()
    if not queue.dependencies_complete(feature):
        incomplete = [
            dependency_id for dependency_id in feature.get("dependencies", [])
            if not queue.dependencies_complete({"dependencies": [dependency_id]})
        ]
        return "Dependencies incomplete: " + ", ".join(incomplete)
    if not queue._specified(feature):
        return "Feature specification or acceptance criteria are incomplete."
    return None


def _mutation_preflight(
    configuration: Configuration,
    project: Project,
) -> tuple[RepositoryInspector, Path, str, bytes, dict[str, Any], FeatureQueue]:
    inspector = RepositoryInspector(project.repository)
    if any(inspector.git_operation_state().values()):
        raise ConveyorError("queue metadata control is unavailable during an unfinished Git operation")
    if inspector.current_branch != project.milestone_branch:
        raise ConveyorError("queue metadata control requires the configured milestone branch")
    queue_path = resolve_queue_path(project.repository, project.queue_location)
    relative_queue = queue_path.relative_to(project.repository.resolve()).as_posix()
    changed = set(inspector.tracked_changed_paths())
    untracked = set(inspector.untracked_file_hashes())
    if changed - {relative_queue} or untracked:
        raise ConveyorError("queue metadata control requires a clean repository except for the queue file")
    writer_path = inspector.writer_lock_path(
        configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
    )
    if inspect_repository_writer_lock(writer_path, project.repository).exists:
        raise ConveyorError("queue metadata control is unavailable while another writer lease exists")
    original = queue_path.read_bytes()
    document = json.loads(original.decode("utf-8"))
    queue = FeatureQueue(document, queue_path, project.repository)
    return inspector, queue_path, relative_queue, original, document, queue


def _write_queue_mutation(
    configuration: Configuration,
    project: Project,
    *,
    inspector: RepositoryInspector,
    queue_path: Path,
    relative_queue: str,
    original: bytes,
    document: dict[str, Any],
    feature_id: str,
) -> str:
    writer_path = inspector.writer_lock_path(
        configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
    )
    lease_run = f"queue-metadata-{uuid.uuid4()}"
    lease = RepositoryWriterLease(writer_path, project.repository)
    lease.acquire_or_resume(
        feature=feature_id,
        branch=inspector.current_branch or "DETACHED",
        run_id=lease_run,
    )
    try:
        rendered = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes(queue_path, rendered, mode=queue_path.stat().st_mode & 0o777)
        FeatureQueue.from_location(project.repository, project.queue_location)
        if set(inspector.tracked_changed_paths()) - {relative_queue}:
            raise ConveyorError("queue metadata control changed a path other than FEATURE_QUEUE.yaml")
    except Exception:
        atomic_write_bytes(queue_path, original, mode=queue_path.stat().st_mode & 0o777)
        raise
    finally:
        lease.release(run_id=lease_run)
    return hashlib.sha256(queue_path.read_bytes()).hexdigest()


def prioritize(
    configuration: Configuration,
    project: Project,
    *,
    feature_id: str,
    relative_id: str | None = None,
    placement: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    if placement is not None and placement not in {"before", "after"}:
        raise ConveyorError("prioritize placement must be before or after")
    if (relative_id is None) != (placement is None):
        raise ConveyorError("prioritize requires both a relative feature and placement")
    if relative_id is None and priority is None:
        raise ConveyorError("prioritize requires a priority or before/after placement")
    if priority is not None and priority.upper() not in PRIORITY_LABELS:
        raise QueueError("priority must be one of P1, P2, or P3")
    if relative_id is not None and feature_id == relative_id:
        raise QueueError("a feature cannot be prioritized relative to itself")
    inspector, queue_path, relative_queue, original, document, queue = _mutation_preflight(
        configuration, project
    )
    features = document.get("features")
    assert isinstance(features, list)
    feature_matches = [
        index for index, item in enumerate(features)
        if isinstance(item, dict) and item.get("id") == feature_id
    ]
    relative_matches = [
        index for index, item in enumerate(features)
        if isinstance(item, dict) and item.get("id") == relative_id
    ]
    if len(feature_matches) != 1:
        raise QueueError(f"unknown or duplicate feature: {feature_id}")
    if relative_id is not None and len(relative_matches) != 1:
        raise QueueError(f"unknown or duplicate relative feature: {relative_id}")
    feature = queue.feature(feature_id)
    relative = queue.feature(relative_id) if relative_id else None
    if (
        feature is None
        or feature.get("milestone") != project.active_milestone
        or (relative is not None and relative.get("milestone") != project.active_milestone)
    ):
        raise QueueError("prioritize features must belong to the active milestone")

    before_order = [str(item.get("id")) for item in features]
    previous_priority = priority_label(feature.get("priority"))
    if priority is not None:
        features[feature_matches[0]]["priority"] = priority.upper()
    if relative_id is not None:
        moved = features.pop(feature_matches[0])
        target_index = next(
            index
            for index, item in enumerate(features)
            if isinstance(item, dict) and item.get("id") == relative_id
        )
        if placement == "after":
            target_index += 1
        features.insert(target_index, moved)
    after_order = [str(item.get("id")) for item in features]
    if before_order == after_order and previous_priority == priority_label(priority):
        return {
            "schema_version": 1,
            "project_id": project.project_id,
            "classification": "priority_unchanged",
            "feature_id": feature_id,
            "placement": placement,
            "relative_feature_id": relative_id,
            "changed_paths": [],
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
        }

    queue_sha256 = _write_queue_mutation(
        configuration,
        project,
        inspector=inspector,
        queue_path=queue_path,
        relative_queue=relative_queue,
        original=original,
        document=document,
        feature_id=feature_id,
    )
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "classification": "priority_updated",
        "feature_id": feature_id,
        "placement": placement,
        "relative_feature_id": relative_id,
        "priority": priority.upper() if priority else previous_priority,
        "previous_priority": previous_priority,
        "before_order": before_order,
        "after_order": after_order,
        "changed_paths": [relative_queue],
        "queue_sha256": queue_sha256,
        "status_changes": 0,
        "model_sessions_launched": 0,
        "child_sessions_launched": 0,
    }


def transition_ready(
    configuration: Configuration,
    project: Project,
    *,
    feature_id: str,
    active_transaction: str | None,
) -> dict[str, Any]:
    if active_transaction:
        raise ConveyorError("ready is unavailable while an active transaction exists")
    inspector, queue_path, relative_queue, original, document, queue = _mutation_preflight(
        configuration, project
    )
    feature = queue.feature(feature_id)
    if feature is None:
        raise QueueError(f"unknown feature: {feature_id}")
    if feature.get("milestone") != project.active_milestone:
        raise QueueError("ready feature must belong to the active milestone")
    if feature.get("status") != "proposed":
        raise QueueError("ready may change only proposed features")
    if reason := _ready_transition_reason(queue, feature):
        raise QueueError(f"ready is blocked: {reason}")
    features = document["features"]
    target = next(item for item in features if item.get("id") == feature_id)
    target["status"] = "ready"
    queue_sha256 = _write_queue_mutation(
        configuration, project, inspector=inspector, queue_path=queue_path,
        relative_queue=relative_queue, original=original, document=document, feature_id=feature_id,
    )
    return _transition_result(project, feature_id, "ready", queue_sha256, relative_queue)


def transition_backlog(
    configuration: Configuration,
    project: Project,
    *,
    feature_id: str,
    active_transaction: str | None,
) -> dict[str, Any]:
    if active_transaction:
        raise ConveyorError("backlog is unavailable while an active transaction exists")
    inspector, queue_path, relative_queue, original, document, queue = _mutation_preflight(
        configuration, project
    )
    feature = queue.feature(feature_id)
    if feature is None:
        raise QueueError(f"unknown feature: {feature_id}")
    if feature.get("milestone") != project.active_milestone:
        raise QueueError("backlog feature must belong to the active milestone")
    if feature.get("status") != "ready":
        raise QueueError("backlog may change only ready features")
    target = next(item for item in document["features"] if item.get("id") == feature_id)
    target["status"] = "proposed"
    queue_sha256 = _write_queue_mutation(
        configuration, project, inspector=inspector, queue_path=queue_path,
        relative_queue=relative_queue, original=original, document=document, feature_id=feature_id,
    )
    return _transition_result(project, feature_id, "backlog", queue_sha256, relative_queue)


def _transition_result(
    project: Project,
    feature_id: str,
    transition: str,
    queue_sha256: str,
    relative_queue: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "classification": f"feature_{transition}",
        "feature_id": feature_id,
        "changed_paths": [relative_queue],
        "queue_sha256": queue_sha256,
        "model_sessions_launched": 0,
        "child_sessions_launched": 0,
        "automatic_cycle_started": False,
    }
