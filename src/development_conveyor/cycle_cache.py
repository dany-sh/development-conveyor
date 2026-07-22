"""Atomic, terminal-evidence-bound compatibility cycle cache materialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import fingerprint
from .errors import ProjectionError, RecoveryError, StaleProjectionCache
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .logging import atomic_write_json
from .projection import ProjectionEngine, projection_fingerprint


@dataclass(frozen=True)
class CanonicalProjectionBinding:
    """Validated durable identity of the canonical persisted projection."""

    ledger_sequence: int
    ledger_fingerprint: str
    projection_fingerprint: str
    canonical_projection: dict[str, Any]
    cache_rebuild_required: bool = False
    cache_rebuilt: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_sequence": self.ledger_sequence,
            "ledger_fingerprint": self.ledger_fingerprint,
            "projection_fingerprint": self.projection_fingerprint,
        }


def validated_canonical_projection_binding(
    *,
    ledger: EvidenceLedger,
    projection_engine: ProjectionEngine,
    transaction_id: str,
    plan_stale_cache_rebuild: bool = False,
    repair_stale_cache: bool = False,
) -> CanonicalProjectionBinding:
    """Return the ledger-bound identity of one canonical persisted projection.

    Planning may identify a missing or stale cache without changing it. A
    writable recovery may replace only that missing/stale cache from the same
    valid ledger. Corrupt or semantically divergent cache evidence is never
    repaired implicitly.
    """

    if plan_stale_cache_rebuild and repair_stale_cache:
        raise RecoveryError("canonical projection cache policy is ambiguous")
    if projection_engine.ledger.path != ledger.path:
        raise RecoveryError("canonical projection engine belongs to another ledger")
    if projection_engine.cache_path is None:
        raise RecoveryError("canonical projection cache path is absent")

    integrity = ledger.verify()
    canonical_projection = projection_engine.rebuild(persist_cache=False)
    if canonical_projection.get("ledger_sequence") != integrity.sequence:
        raise RecoveryError("canonical projection ledger sequence disagrees with the ledger")
    if canonical_projection.get("ledger_fingerprint") != integrity.fingerprint:
        raise RecoveryError("canonical projection ledger fingerprint disagrees with the ledger")
    if canonical_projection.get("projection_fingerprint") != projection_fingerprint(
        canonical_projection
    ):
        raise RecoveryError("rebuilt canonical projection fingerprint is invalid")

    cache_rebuild_required = False
    try:
        persisted_projection = projection_engine.load_cache()
    except StaleProjectionCache:
        persisted_projection = None
        cache_rebuild_required = True
    except ProjectionError as exc:
        raise RecoveryError("canonical persisted projection evidence is invalid") from exc
    if persisted_projection is None:
        cache_rebuild_required = True

    cache_rebuilt = False
    if cache_rebuild_required:
        if repair_stale_cache:
            canonical_projection = projection_engine.rebuild(persist_cache=True)
            try:
                persisted_projection = projection_engine.load_cache()
            except ProjectionError as exc:
                raise RecoveryError("rebuilt canonical projection cache is invalid") from exc
            cache_rebuilt = True
            cache_rebuild_required = False
        elif not plan_stale_cache_rebuild:
            raise RecoveryError("canonical persisted projection cache is missing or stale")

    if persisted_projection is not None:
        if persisted_projection.get("ledger_sequence") != integrity.sequence:
            raise RecoveryError("canonical persisted projection ledger sequence is stale")
        if persisted_projection.get("ledger_fingerprint") != integrity.fingerprint:
            raise RecoveryError("canonical persisted projection ledger fingerprint disagrees")
        if persisted_projection.get("projection_fingerprint") != projection_fingerprint(
            persisted_projection
        ):
            raise RecoveryError("canonical persisted projection fingerprint is invalid")
        if persisted_projection != canonical_projection:
            raise RecoveryError(
                "canonical persisted projection differs from the unchanged ledger rebuild"
            )

    terminal = ledger.terminal_event(transaction_id)
    if terminal is None or terminal.get("event_type") not in TERMINAL_EVENT_TYPES:
        raise RecoveryError("cycle cache transaction is unknown or nonterminal")
    if terminal.get("sequence", integrity.sequence + 1) > integrity.sequence:
        raise RecoveryError("cycle cache terminal transaction is beyond ledger head")

    return CanonicalProjectionBinding(
        ledger_sequence=integrity.sequence,
        ledger_fingerprint=integrity.fingerprint,
        projection_fingerprint=canonical_projection["projection_fingerprint"],
        canonical_projection=canonical_projection,
        cache_rebuild_required=cache_rebuild_required,
        cache_rebuilt=cache_rebuilt,
    )


def _bind_terminal_cycle_cache(
    state: dict[str, Any],
    *,
    ledger: EvidenceLedger,
    binding: CanonicalProjectionBinding,
    transaction_id: str,
    expected_feature: str | None = None,
    require_semantic_state: bool = True,
) -> dict[str, Any]:
    """Return one finalized cycle cache from a validated canonical binding."""

    canonical_projection = binding.canonical_projection
    terminal = ledger.terminal_event(transaction_id)
    terminal_payload = (terminal or {}).get("payload", {})
    terminal_feature = terminal_payload.get("selected_feature") or terminal_payload.get(
        "feature_id"
    )
    selected_feature = canonical_projection.get(
        "selected_next_feature"
    ) or canonical_projection.get("current_feature")
    if require_semantic_state and expected_feature is not None and (
        terminal_feature != expected_feature or selected_feature != expected_feature
    ):
        raise RecoveryError("cycle cache selected feature disagrees with terminal projection")
    if require_semantic_state and (
        state.get("current_feature") != selected_feature
        or state.get("current_phase") != canonical_projection.get("current_state")
    ):
        raise RecoveryError("cycle cache semantic state disagrees with terminal projection")

    finalized = dict(state)
    finalized.update(
        {
            "kernel_transaction_id": transaction_id,
            "kernel_ledger_sequence": binding.ledger_sequence,
            "kernel_ledger_fingerprint": binding.ledger_fingerprint,
            "kernel_projection_fingerprint": binding.projection_fingerprint,
        }
    )
    finalized.pop("kernel_cache_fingerprint", None)
    signed = dict(finalized)
    finalized["kernel_cache_fingerprint"] = fingerprint(signed)
    return finalized


def write_terminal_cycle_cache(
    path: Path,
    state: dict[str, Any],
    *,
    ledger: EvidenceLedger,
    projection_engine: ProjectionEngine,
    transaction_id: str,
    expected_feature: str | None = None,
    require_semantic_state: bool = True,
) -> dict[str, Any]:
    """Atomically write and read back one canonical terminal cache binding."""

    binding = validated_canonical_projection_binding(
        ledger=ledger,
        projection_engine=projection_engine,
        transaction_id=transaction_id,
    )
    finalized = _bind_terminal_cycle_cache(
        state,
        ledger=ledger,
        binding=binding,
        transaction_id=transaction_id,
        expected_feature=expected_feature,
        require_semantic_state=require_semantic_state,
    )
    atomic_write_json(path, finalized)

    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("written cycle cache cannot be read back") from exc
    if not isinstance(persisted, dict) or persisted != finalized:
        raise RecoveryError("written cycle cache differs from the finalized cache")
    unsigned = dict(persisted)
    claimed_signature = unsigned.pop("kernel_cache_fingerprint", None)
    if claimed_signature != fingerprint(unsigned):
        raise RecoveryError("written cycle cache signature is invalid")

    post_write_binding = validated_canonical_projection_binding(
        ledger=ledger,
        projection_engine=projection_engine,
        transaction_id=transaction_id,
    )
    if post_write_binding.to_dict() != binding.to_dict():
        raise RecoveryError("canonical projection binding changed during cycle-cache write")
    if (
        persisted.get("kernel_ledger_sequence") != post_write_binding.ledger_sequence
        or persisted.get("kernel_ledger_fingerprint")
        != post_write_binding.ledger_fingerprint
        or persisted.get("kernel_projection_fingerprint")
        != post_write_binding.projection_fingerprint
    ):
        raise RecoveryError("written cycle cache disagrees with canonical projection binding")
    return persisted
