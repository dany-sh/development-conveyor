"""Whole-phase bridge from command handlers to the transactional kernel.

The bridge is intentionally phase-sized: one invocation owns one transaction,
one typed repository lease, and one terminal event.  Compatibility code may
perform several cache/checkpoint updates inside ``execute``, but it cannot
create independent transactions for those internal updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .command_authority import CommandAuthority, CommandRecord
from .contracts import PhaseTransaction, SessionResultEnvelope, TransactionState, TERMINAL_STATES
from .errors import TransactionError
from .kernel import PhaseAdapter, WorkflowKernel


@dataclass(frozen=True)
class PhaseExecution:
    """Evidence returned by a phase handler after its one session/work unit."""

    envelope: SessionResultEnvelope
    command_records: tuple[CommandRecord, ...] = ()
    completion_evidence: dict[str, Any] | None = None
    process_returncode: int = 0


PhaseHandler = Callable[[PhaseTransaction], PhaseExecution]


class KernelWorkflowBridge:
    """Drive one complete writable phase through ``WorkflowKernel``.

    The handler may write only paths authorized by the adapter.  It never owns
    the lease, validation state, commit, terminal event, cache projection, or
    lease release; all of those remain kernel operations.
    """

    def __init__(self, kernel: WorkflowKernel):
        self.kernel = kernel

    def run(
        self,
        *,
        adapter: PhaseAdapter,
        run_id: str,
        milestone: str | None,
        feature_id: str | None,
        session_id: str,
        execute: PhaseHandler,
        authority: CommandAuthority | None = None,
    ) -> dict[str, Any]:
        transaction = self.kernel.begin(
            workflow_type=adapter.workflow_type,
            milestone=milestone,
            feature_id=feature_id,
            run_id=run_id,
            policy=adapter.policy,
        )
        try:
            self.kernel.acquire_lease()
            self.kernel.capture_snapshot()
            self.kernel.session_launched(session_id)
            result = execute(transaction)
            if result.envelope.session_id != session_id:
                raise ValueError("phase handler returned evidence for another session")
            self.kernel.accept_result(result.envelope)
            terminal_state = adapter.terminal_state(result.envelope)
            if terminal_state is not None:
                gate = result.envelope.evidence.get("human_decision") or result.envelope.evidence.get("gate")
                projection = self.kernel.block(
                    state=terminal_state,
                    classification=result.envelope.classification,
                    next_state=result.envelope.next_state,
                    human_gate=gate if isinstance(gate, dict) else None,
                )
                return {"transaction": transaction.to_dict(), "projection": projection}
            self.kernel.record_file_mutation_boundary()
            self.kernel.validate(
                authority=authority or CommandAuthority(),
                command_results=result.command_records,
                semantic_validator=adapter.semantic_validate,
            )
            self.kernel.finalize()
            return self.kernel.complete(evidence=result.completion_evidence)
        except (TransactionError, ValueError) as exc:
            # Deterministic identity/schema/validation failures are handled
            # terminal outcomes. Runtime interruption exceptions deliberately
            # remain outside this clause so recovery retains the live lease.
            if transaction.current_state not in TERMINAL_STATES and self.kernel.lease.read() is not None:
                projection = self.kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification=adapter.HANDLED_FAILURE_CLASSIFICATION,
                    next_state=adapter.HANDLED_FAILURE_NEXT_STATE,
                    reference=type(exc).__name__,
                )
                return {"transaction": transaction.to_dict(), "projection": projection, "error": str(exc)}
            raise

    def block(
        self,
        *,
        state: TransactionState,
        classification: str,
        next_state: str,
        reference: str | None = None,
        human_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.kernel.block(
            state=state,
            classification=classification,
            next_state=next_state,
            reference=reference,
            human_gate=human_gate,
        )
