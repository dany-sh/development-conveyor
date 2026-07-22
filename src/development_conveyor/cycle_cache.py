"""Atomic, terminal-evidence-bound compatibility cycle cache materialization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import fingerprint
from .errors import RecoveryError
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .logging import atomic_write_json
from .projection import projection_fingerprint


def bind_terminal_cycle_cache(
    state: dict[str, Any], *, ledger: EvidenceLedger, projection: dict[str, Any],
    transaction_id: str, expected_feature: str | None = None,
) -> dict[str, Any]:
    """Return one fully finalized, signed terminal cycle-cache object.

    The cache is compatibility-only, so its binding must be a complete snapshot
    of immutable ledger and projection evidence.  Nothing mutates the returned
    value after its signature is calculated.
    """
    integrity = ledger.verify()
    if projection.get("ledger_sequence") != integrity.sequence:
        raise RecoveryError("cycle cache ledger sequence disagrees with the ledger")
    if projection.get("ledger_fingerprint") != integrity.fingerprint:
        raise RecoveryError("cycle cache ledger fingerprint disagrees with the ledger")
    if projection.get("projection_fingerprint") != projection_fingerprint(projection):
        raise RecoveryError("cycle cache projection fingerprint is invalid")
    terminal = ledger.terminal_event(transaction_id)
    if terminal is None or terminal.get("event_type") not in TERMINAL_EVENT_TYPES:
        raise RecoveryError("cycle cache transaction is unknown or nonterminal")
    if terminal["sequence"] > integrity.sequence:
        raise RecoveryError("cycle cache terminal transaction is beyond ledger head")
    terminal_payload = terminal.get("payload", {})
    terminal_feature = terminal_payload.get("selected_feature") or terminal_payload.get("feature_id")
    selected_feature = projection.get("selected_next_feature") or projection.get("current_feature")
    if expected_feature is not None and (terminal_feature != expected_feature or selected_feature != expected_feature):
        raise RecoveryError("cycle cache selected feature disagrees with terminal projection")
    if state.get("current_feature") != selected_feature or state.get("current_phase") != projection.get("current_state"):
        raise RecoveryError("cycle cache semantic state disagrees with terminal projection")

    finalized = dict(state)
    finalized.update({
        "kernel_transaction_id": transaction_id,
        "kernel_ledger_sequence": integrity.sequence,
        "kernel_ledger_fingerprint": integrity.fingerprint,
        "kernel_projection_fingerprint": projection["projection_fingerprint"],
    })
    finalized.pop("kernel_cache_fingerprint", None)
    signed = dict(finalized)
    finalized["kernel_cache_fingerprint"] = fingerprint(signed)
    return finalized


def write_terminal_cycle_cache(
    path: Path, state: dict[str, Any], *, ledger: EvidenceLedger,
    projection: dict[str, Any], transaction_id: str, expected_feature: str | None = None,
) -> dict[str, Any]:
    """Sign the final object once, then atomically replace the compatibility cache."""
    finalized = bind_terminal_cycle_cache(
        state, ledger=ledger, projection=projection, transaction_id=transaction_id,
        expected_feature=expected_feature,
    )
    atomic_write_json(path, finalized)
    return finalized
