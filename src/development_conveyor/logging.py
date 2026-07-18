"""Atomic state persistence and redacted structured run events."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import load_json
from .redaction import redact_value
from .validation import validate_schema


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(redact_value(value), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    """Atomically preserve exact immutable evidence bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class JsonStateStore:
    def __init__(self, schema_path: Path):
        self.schema = load_json(schema_path)

    def read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        value = load_json(path)
        validate_schema(value, self.schema)
        return value

    def write(self, path: Path, value: dict[str, Any]) -> None:
        validate_schema(value, self.schema)
        atomic_write_json(path, value)


class EventLogger:
    def __init__(self, path: Path, schema_path: Path):
        self.path = path
        self.schema = load_json(schema_path)

    def append(self, event: dict[str, Any]) -> None:
        value = redact_value(event)
        validate_schema(value, self.schema)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def run_event(
    *,
    run_id: str,
    project_id: str | None,
    source: str,
    repository_fingerprint: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    milestone: str | None = None,
    feature: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    command_category: str | None = None,
    command: list[str] | None = None,
    result: str | None = None,
    validation_outcome: str | None = None,
    review_findings: dict[str, int] | None = None,
    repair_attempt: int | None = None,
    integration_outcome: str | None = None,
    previous_state: str | None = None,
    next_state: str | None = None,
    stop_reason: str | None = None,
    human_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "timestamp": utc_now(),
        "run_id": run_id,
        "project_id": project_id,
        "repository_fingerprint": repository_fingerprint,
        "source": source,
        "model": model,
        "reasoning": reasoning,
        "milestone": milestone,
        "feature": feature,
        "branch": branch,
        "commit": commit,
        "command_category": command_category,
        "command": command,
        "result": result,
        "validation_outcome": validation_outcome,
        "review_findings": review_findings or {},
        "repair_attempt": repair_attempt,
        "integration_outcome": integration_outcome,
        "previous_state": previous_state,
        "next_state": next_state,
        "stop_reason": stop_reason,
        "human_gate": human_gate,
    }
