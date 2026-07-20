"""Immutable repository snapshots for workflow transaction starts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import RepositorySnapshot, fingerprint
from .queue import resolve_queue_path
from .registry import Project
from .repository import RepositoryInspector


def capture_repository_snapshot(project: Project) -> RepositorySnapshot:
    inspector = RepositoryInspector(project.repository)
    identity = inspector.identity()
    queue_fingerprint = None
    try:
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        queue_fingerprint = hashlib.sha256(queue_path.read_bytes()).hexdigest()
    except OSError:
        queue_fingerprint = None
    tracked_paths = tuple(inspector.tracked_changed_paths())
    untracked = inspector.untracked_file_hashes()
    return RepositorySnapshot(
        repository_identity=identity["repository_id"],
        repository_path_fingerprint=identity["path_fingerprint"],
        branch=inspector.current_branch or "DETACHED",
        head=inspector.head,
        queue_fingerprint=queue_fingerprint,
        tracked_diff_fingerprint=inspector.planning_diff_fingerprint(),
        untracked_fingerprint=fingerprint(untracked),
        tracked_changed_paths=tracked_paths,
        untracked_paths=tuple(sorted(untracked)),
        git_operations=inspector.git_operation_state(),
    )
