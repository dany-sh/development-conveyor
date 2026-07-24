"""Canonical controller compatibility-state construction and persistence."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from .logging import atomic_write_json
from .validation import validate_schema


def build_controller_compatibility_cache(
    existing: dict[str, Any],
    *,
    project_schema: dict[str, Any],
    project_id: str,
    repository_fingerprint: str,
    active_milestone: str | None,
    run_id: str,
    projection: dict[str, Any],
    execution_plan: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    """Return a fresh schema-valid compatibility projection.

    This intentionally does not merge the projection or the previous cache.
    Only stable history owned by the compatibility schema is retained.
    """

    history = existing.get("human_decision_history")
    if not isinstance(history, list):
        history = []
    created_at = existing.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = updated_at
    document = {
        "schema_version": 1,
        "project_id": project_id,
        "repository_fingerprint": repository_fingerprint,
        "run_id": run_id,
        "current_state": projection["current_state"],
        "active_milestone": active_milestone,
        "current_feature": execution_plan.get("feature_id"),
        "last_checkpoint": "kernel_projection_reconciled",
        "stop_reason": None,
        "human_decision_required": projection.get("human_gate"),
        "human_decision_history": history,
        "state_evidence": {
            "source": "evidence_ledger_projection",
            "ledger_sequence": projection["ledger_sequence"],
            "ledger_fingerprint": projection["ledger_fingerprint"],
            "projection_fingerprint": projection["projection_fingerprint"],
            "execution_plan": execution_plan,
        },
        "created_at": created_at,
        "updated_at": updated_at,
    }
    validate_schema(document, project_schema)
    return document


def write_controller_compatibility_cache(
    path: Path,
    document: dict[str, Any],
    *,
    project_schema: dict[str, Any],
) -> None:
    """Atomically replace one validated cache while preserving permissions."""

    validate_schema(document, project_schema)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    atomic_write_json(path, document, mode=mode)
    persisted = path.read_text(encoding="utf-8")
    # Parse through the same schema path used by normal status.
    value = json.loads(persisted)
    validate_schema(value, project_schema)
    if value != document:
        raise ValueError("persisted controller compatibility cache changed during write")
