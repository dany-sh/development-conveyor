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
from .validation import validate_schema


LEGACY_CACHE_BINDING_RECOVERY_FIELD = "cache_binding_recovery"
_LEGACY_CACHE_BINDING_RECOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_transaction", "recovery_run_id"],
    "properties": {
        "source_transaction": {"type": "string", "minLength": 1},
        "recovery_run_id": {"type": "string", "minLength": 1},
    },
}

_SEMANTIC_FIELD_BINDINGS = {
    "current_phase": "current_state",
    "current_feature": "current_feature",
    "selected_feature": "selected_next_feature",
    "accepted_feature_commit": "accepted_feature_commit",
    "integration_status": "integration_status",
    "next_safe_action": "allowed_next_action",
}


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


def cycle_cache_semantics_from_projection(
    projection: dict[str, Any],
) -> dict[str, Any]:
    """Return the active compatibility semantics owned by one projection."""

    return {
        cache_field: projection.get(projection_field)
        for cache_field, projection_field in _SEMANTIC_FIELD_BINDINGS.items()
    }


def cycle_cache_semantic_mismatches(
    state: dict[str, Any],
    projection: dict[str, Any],
) -> tuple[str, ...]:
    """Return active cache fields that disagree with canonical projection."""

    expected = cycle_cache_semantics_from_projection(projection)
    return tuple(
        field for field, value in expected.items() if state.get(field) != value
    )


def build_canonical_cycle_cache(
    state: dict[str, Any],
    *,
    cycle_schema: dict[str, Any],
    updates: dict[str, Any],
    projection: dict[str, Any],
) -> dict[str, Any]:
    """Build one cycle cache through the registered schema boundary.

    Recovery callers may retain supported cycle fields and may update only
    fields registered by the cycle-state schema. The final complete document,
    rather than an arbitrary legacy input, is schema validated. Projection
    semantics are translated through the explicit compatibility mapping above;
    projection dictionaries are never merged into cycle state.
    """

    supported = set((cycle_schema.get("properties") or {}).keys())
    unsupported_updates = sorted(set(updates) - supported)
    if unsupported_updates:
        raise RecoveryError(
            "canonical cycle-cache update contains unsupported fields: "
            + ", ".join(unsupported_updates)
        )
    canonical = {key: value for key, value in state.items() if key in supported}
    canonical.update(updates)
    canonical.update(cycle_cache_semantics_from_projection(projection))
    canonical.pop("kernel_cache_fingerprint", None)
    validate_schema(canonical, cycle_schema)
    return canonical


def completed_transition_cycle_updates(
    *,
    projection: dict[str, Any],
    run_id: str,
    milestone_branch: str,
    pre_transition_head: str,
    terminal_snapshot: dict[str, Any],
    queue_fingerprint: str,
    selected_feature: dict[str, Any] | None,
    selected_feature_branch: str | None,
    dependency_statuses: dict[str, str | None] | None,
    checkpoint: str,
    updated_at: str,
) -> dict[str, Any]:
    """Return deterministic cache fields after one completed branch transition.

    These fields deliberately describe the repository after the completed
    transaction. Historical feature and session bindings from the preceding
    cache must not survive merely because the cache document is derived.
    Projection-owned semantics are applied separately by
    :func:`build_canonical_cycle_cache`.
    """

    terminal_branch = terminal_snapshot.get("branch")
    terminal_head = terminal_snapshot.get("head")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(milestone_branch, str)
        or not milestone_branch
        or terminal_branch != milestone_branch
        or not isinstance(terminal_head, str)
        or not terminal_head
        or terminal_snapshot.get("clean") is not True
        or not isinstance(queue_fingerprint, str)
        or not queue_fingerprint
        or not isinstance(updated_at, str)
        or not updated_at
    ):
        raise RecoveryError("completed transition cache evidence is incomplete")
    selected_id = projection.get("selected_next_feature")
    if selected_id is not None:
        if (
            not isinstance(selected_feature, dict)
            or selected_feature.get("id") != selected_id
            or not isinstance(selected_feature_branch, str)
            or not selected_feature_branch
        ):
            raise RecoveryError(
                "completed transition selected-feature evidence is incomplete"
            )
    elif selected_feature is not None or selected_feature_branch is not None:
        raise RecoveryError(
            "completed transition unexpectedly supplied selected-feature evidence"
        )
    dependencies = list(selected_feature.get("dependencies", [])) if selected_feature else []
    statuses = dict(dependency_statuses or {})
    if set(statuses) != set(dependencies):
        raise RecoveryError(
            "completed transition dependency evidence is incomplete"
        )
    return {
        "conveyor_run_id": run_id,
        "current_feature": projection.get("current_feature"),
        "selected_feature": selected_id,
        "feature_dependencies": dependencies,
        "dependency_evidence": {
            "declared": dependencies,
            "statuses": statuses,
            "all_complete": all(
                value in {"done", "integrated"} for value in statuses.values()
            ),
        },
        "queue_fingerprint": queue_fingerprint,
        "feature_branch": selected_feature_branch,
        "feature_worktree": None,
        "feature_starting_commit": terminal_head,
        "accepted_feature_commit": projection.get("accepted_feature_commit"),
        "milestone_branch": milestone_branch,
        "milestone_pre_integration_commit": pre_transition_head,
        "milestone_post_integration_commit": terminal_head,
        "session_completion_classification": None,
        "session_completion_flags": [],
        "session_completion_evidence": None,
        "branch_recovery": None,
        "writer_lock_identity": None,
        "validation_attempts": [],
        "review_attempts": [],
        "integration_attempts": [],
        "last_successful_checkpoint": checkpoint,
        "last_verified_git_state": terminal_snapshot,
        "failure_classification": None,
        "retry_exhausted": False,
        "stop_reason": None,
        "human_decision_required": None,
        "session_id": None,
        "feature_session_id": None,
        "integration_session_id": None,
        "prior_integration_session_ids": [],
        "integration_status": projection.get("integration_status"),
        "integration_gate": None,
        "milestone_gate_session_id": None,
        "next_safe_action": projection.get("allowed_next_action"),
        "updated_at": updated_at,
    }


def normalize_cycle_cache_for_rebinding(
    state: dict[str, Any],
    *,
    cycle_schema: dict[str, Any],
) -> dict[str, Any]:
    """Validate one legacy cache and return its canonical durable form.

    The one historical cache-binding recovery provenance field is accepted
    only on this deterministic normalization path. Every other top-level and
    nested field remains governed by the common cycle-cache schema.
    """

    normalized = dict(state)
    if LEGACY_CACHE_BINDING_RECOVERY_FIELD in normalized:
        validate_schema(
            normalized.pop(LEGACY_CACHE_BINDING_RECOVERY_FIELD),
            _LEGACY_CACHE_BINDING_RECOVERY_SCHEMA,
            f"$.{LEGACY_CACHE_BINDING_RECOVERY_FIELD}",
        )
    validate_schema(normalized, cycle_schema)
    return normalized


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
    if expected_feature is not None and (
        terminal_feature != expected_feature or selected_feature != expected_feature
    ):
        raise RecoveryError("cycle cache selected feature disagrees with terminal projection")
    finalized = dict(state)
    finalized.pop(LEGACY_CACHE_BINDING_RECOVERY_FIELD, None)
    finalized.update(
        {
            "kernel_transaction_id": transaction_id,
            "kernel_ledger_sequence": binding.ledger_sequence,
            "kernel_ledger_fingerprint": binding.ledger_fingerprint,
            "kernel_projection_fingerprint": binding.projection_fingerprint,
        }
    )
    semantic_mismatches = cycle_cache_semantic_mismatches(
        finalized, canonical_projection
    )
    identity_mismatches = tuple(
        field
        for field, expected in {
            "kernel_transaction_id": transaction_id,
            "kernel_ledger_sequence": binding.ledger_sequence,
            "kernel_ledger_fingerprint": binding.ledger_fingerprint,
            "kernel_projection_fingerprint": binding.projection_fingerprint,
        }.items()
        if finalized.get(field) != expected
    )
    if semantic_mismatches:
        raise RecoveryError(
            "cycle cache semantic state disagrees with terminal projection: "
            + ", ".join(semantic_mismatches)
        )
    if identity_mismatches:
        raise RecoveryError(
            "cycle cache binding disagrees with terminal projection: "
            + ", ".join(identity_mismatches)
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
    cycle_schema: dict[str, Any] | None = None,
    mode: int = 0o600,
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
    )
    if cycle_schema is not None:
        validate_schema(finalized, cycle_schema)
    atomic_write_json(path, finalized, mode=mode)

    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("written cycle cache cannot be read back") from exc
    if not isinstance(persisted, dict) or persisted != finalized:
        raise RecoveryError("written cycle cache differs from the finalized cache")
    if cycle_schema is not None:
        validate_schema(persisted, cycle_schema)
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
    semantic_mismatches = cycle_cache_semantic_mismatches(
        persisted, post_write_binding.canonical_projection
    )
    if semantic_mismatches:
        raise RecoveryError(
            "written cycle cache semantic state disagrees with canonical projection: "
            + ", ".join(semantic_mismatches)
        )
    return persisted
