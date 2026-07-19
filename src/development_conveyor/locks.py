"""Durable launch reservation and repository writer-lock inspection."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AmbiguousLockError, LockError
from .logging import atomic_write_json, utc_now


def process_alive(process_id: int) -> bool | None:
    try:
        os.kill(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def read_lock(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AmbiguousLockError(f"lock is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AmbiguousLockError(f"lock is not an object: {path}")
    return value


@dataclass(frozen=True)
class LockStatus:
    exists: bool
    owned_by_run: bool
    process_alive: bool | None
    ambiguous: bool
    record: dict[str, Any] | None


class DurableLock:
    """Atomic controller launch reservation; not a replacement for the factory writer lease."""

    def __init__(self, path: Path):
        self.path = path

    def acquire(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise LockError(f"launch reservation already exists: {self.path}") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def heartbeat(self, run_id: str, phase: str) -> dict[str, Any]:
        record = read_lock(self.path)
        if record is None or record.get("run_id") != run_id:
            raise LockError("launch reservation belongs to another run")
        record["last_confirmed_time"] = utc_now()
        record["current_phase"] = phase
        atomic_write_json(self.path, record)
        return record

    def release(self, run_id: str) -> None:
        record = read_lock(self.path)
        if record is None:
            return
        if record.get("run_id") != run_id:
            raise LockError("refusing to release another run's launch reservation")
        self.path.unlink()

    def status(self, run_id: str | None = None) -> LockStatus:
        record = read_lock(self.path)
        if record is None:
            return LockStatus(False, False, None, False, None)
        host = record.get("host_identity") or record.get("host")
        pid = record.get("process_id") or record.get("pid")
        alive = process_alive(int(pid)) if host == socket.gethostname() and isinstance(pid, int) else None
        ambiguous = host != socket.gethostname() or alive is None
        return LockStatus(True, record.get("run_id") == run_id, alive, ambiguous, record)

    def recover_stale(self, *, expected_repository_identity: str) -> dict[str, Any]:
        record = read_lock(self.path)
        if record is None:
            raise LockError("no lock exists")
        if record.get("repository_identity") != expected_repository_identity:
            raise AmbiguousLockError("lock repository identity does not match")
        if record.get("host_identity") != socket.gethostname():
            raise AmbiguousLockError("foreign-host lock ownership is ambiguous")
        pid = record.get("process_id")
        if not isinstance(pid, int) or process_alive(pid) is not False:
            raise AmbiguousLockError("process identity does not prove the lock stale")
        recovered = self.path.with_name(f"{self.path.stem}.recovered-{record.get('run_id', 'unknown')}.json")
        if recovered.exists():
            raise AmbiguousLockError("a recovery evidence file already exists")
        os.replace(self.path, recovered)
        return {"recovered_lock": str(recovered), "record": record, "timestamp_alone_used": False}


class RepositoryWriterLease:
    """Feature-Factory-compatible lease held across controller evidence verification."""

    def __init__(self, path: Path, repository: Path):
        self.path = path
        self.repository = repository.expanduser().resolve()

    def _new_record(self, *, feature: str, branch: str, run_id: str) -> dict[str, Any]:
        stamp = utc_now()
        return {
            "repository": str(self.repository),
            "feature_id": feature,
            "branch": branch,
            "worktree": str(self.repository),
            "agent_run": run_id,
            "purpose": "development-conveyor-feature",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "start_time": stamp,
            "last_heartbeat": stamp,
        }

    @staticmethod
    def _matches(
        record: dict[str, Any], *, repository: Path, feature: str, branch: str, run_id: str
    ) -> bool:
        try:
            recorded_repository = Path(str(record.get("repository"))).expanduser().resolve()
            recorded_worktree = Path(str(record.get("worktree"))).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(
            recorded_repository == repository
            and recorded_worktree == repository
            and record.get("feature_id") == feature
            and record.get("branch") == branch
            and record.get("agent_run") == run_id
        )

    def acquire_or_resume(self, *, feature: str, branch: str, run_id: str) -> dict[str, Any]:
        existing = read_lock(self.path)
        if existing is not None:
            if not self._matches(
                existing,
                repository=self.repository,
                feature=feature,
                branch=branch,
                run_id=run_id,
            ):
                raise LockError("repository writer lease belongs to another feature or run")
            host = existing.get("host") or existing.get("host_identity")
            pid = existing.get("pid") or existing.get("process_id")
            alive = process_alive(int(pid)) if host == socket.gethostname() and isinstance(pid, int) else None
            if host != socket.gethostname() or alive is None:
                raise AmbiguousLockError("matching writer lease ownership is ambiguous")
            if alive and pid != os.getpid():
                raise LockError("matching writer lease is still owned by a live process")
            existing.update({
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "last_heartbeat": utc_now(),
            })
            atomic_write_json(self.path, existing)
            return existing

        record = self._new_record(feature=feature, branch=branch, run_id=run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise LockError("repository writer lease appeared concurrently") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def release(self, *, run_id: str) -> None:
        record = read_lock(self.path)
        if record is None:
            return
        if record.get("agent_run") != run_id:
            raise LockError("refusing to release another run's repository writer lease")
        self.path.unlink()


class PlanningWriterLease:
    """Planning-only repository writer ownership bound to one exact transaction."""

    def __init__(self, path: Path, repository: Path):
        self.path = path
        self.repository = repository.expanduser().resolve()

    def acquire(
        self,
        *,
        repository_identity: str,
        project_id: str,
        milestone: str,
        run_id: str,
        session_id: str | None,
        branch: str,
        head: str,
        worktree_fingerprint: str,
        allowed_paths: list[str],
    ) -> dict[str, Any]:
        if read_lock(self.path) is not None:
            raise LockError("repository writer lease already exists")
        stamp = utc_now()
        record = {
            "repository": str(self.repository),
            "repository_identity": repository_identity,
            "project_id": project_id,
            "milestone": milestone,
            "queue_reconciliation_run_id": run_id,
            "session_id": session_id,
            "branch": branch,
            "starting_head": head,
            "starting_worktree_fingerprint": worktree_fingerprint,
            "approved_planning_paths": allowed_paths,
            "phase": "queue_reconciliation",
            "purpose": "development-conveyor-planning",
            "agent_run": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "start_time": stamp,
            "last_heartbeat": stamp,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise LockError("repository writer lease appeared concurrently") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def bind_session(self, *, run_id: str, session_id: str) -> dict[str, Any]:
        record = self.revalidate(run_id=run_id)
        recorded = record.get("session_id")
        if recorded not in {None, session_id}:
            raise LockError("planning lease session identity mismatch")
        record["session_id"] = session_id
        record["last_heartbeat"] = utc_now()
        atomic_write_json(self.path, record)
        return record

    def revalidate(
        self,
        *,
        run_id: str,
        session_id: str | None = None,
        repository_identity: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        record = read_lock(self.path)
        if record is None:
            raise LockError("planning writer lease is absent")
        checks = {
            "purpose": record.get("purpose") == "development-conveyor-planning",
            "phase": record.get("phase") == "queue_reconciliation",
            "run": record.get("queue_reconciliation_run_id") == run_id,
            "repository": Path(str(record.get("repository"))).expanduser().resolve() == self.repository,
            "session": session_id is None or record.get("session_id") == session_id,
            "identity": repository_identity is None or record.get("repository_identity") == repository_identity,
            "branch": branch is None or record.get("branch") == branch,
            "head": head is None or record.get("starting_head") == head,
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise LockError(f"planning writer lease identity mismatch: {failed}")
        return record

    def release(self, *, run_id: str) -> None:
        record = read_lock(self.path)
        if record is None:
            return
        if (
            record.get("purpose") != "development-conveyor-planning"
            or record.get("queue_reconciliation_run_id") != run_id
        ):
            raise LockError("refusing to release another writer lease")
        self.path.unlink()


def make_lock_record(
    *, project_id: str, repository_identity: str, run_id: str, current_feature: str | None, current_phase: str
) -> dict[str, Any]:
    stamp = utc_now()
    return {
        "project_id": project_id,
        "repository_identity": repository_identity,
        "run_id": run_id,
        "process_id": os.getpid(),
        "host_identity": socket.gethostname(),
        "start_time": stamp,
        "last_confirmed_time": stamp,
        "current_feature": current_feature,
        "current_phase": current_phase,
    }


def inspect_repository_writer_lock(path: Path, repository: Path) -> LockStatus:
    record = read_lock(path)
    if record is None:
        return LockStatus(False, False, None, False, None)
    recorded_repository = record.get("repository")
    if recorded_repository is not None and Path(str(recorded_repository)).expanduser().resolve() != repository.expanduser().resolve():
        return LockStatus(True, False, None, True, record)
    host = record.get("host") or record.get("host_identity")
    pid = record.get("pid") or record.get("process_id")
    alive = process_alive(int(pid)) if host == socket.gethostname() and isinstance(pid, int) else None
    return LockStatus(True, False, alive, host != socket.gethostname() or alive is None, record)
