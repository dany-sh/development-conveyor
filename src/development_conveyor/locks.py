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

