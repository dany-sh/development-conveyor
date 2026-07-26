"""Compatibility facade converting legacy cache transitions into kernel evidence."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from .contracts import LeaseType, MutationPolicy, WorkflowType, WORKFLOW_LEASE
from .ledger import EvidenceLedger
from .projection import ProjectionEngine
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease


LEGACY_NAMESPACE = uuid.UUID("d7747380-ac2b-48e8-a85f-5c6a69f67d92")


def workflow_for_state(state: str) -> WorkflowType:
    if state in {"queue_reconciliation", "feature_ready", "next_feature_selection"}:
        return WorkflowType.QUEUE_RECONCILIATION
    if state in {"preflight", "feature_selected", "branch_preparing"}:
        return WorkflowType.FEATURE_PREPARATION
    if state in {"feature_in_progress", "feature_review", "feature_repair"}:
        return WorkflowType.FEATURE_EXECUTION
    if state in {"feature_accepted", "integration_pending", "integration_ready"}:
        return WorkflowType.FEATURE_ACCEPTANCE
    if state in {"integrating", "integration_validation", "feature_integrated"}:
        return WorkflowType.MILESTONE_INTEGRATION
    if state in {"milestone_gate", "milestone_ready_for_merge", "milestone_complete"}:
        return WorkflowType.MILESTONE_GATE
    if state == "human_decision_required":
        return WorkflowType.HUMAN_DECISION_RESOLUTION
    return WorkflowType.RECOVERY


class LegacyTransitionAdapter:
    """Temporary adapter; every legacy state/cache write crosses this boundary."""

    def __init__(self, *, controller_root: Path, repository: Path, project_id: str):
        self.controller_root = controller_root.expanduser().resolve()
        self.inspector = RepositoryInspector(repository)
        self.project_id = project_id
        identity = self.inspector.identity()
        state_root = self.controller_root / "state/projects" / project_id
        self.ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(self.ledger, state_root / "projection-cache.json")
        self.lease = WorkflowWriterLease(state_root / "legacy-transition-writer.json")

    def transition(
        self,
        *,
        run_id: str,
        feature_id: str | None,
        milestone: str | None,
        previous_state: str,
        next_state: str,
        checkpoint: str,
        mutate: Callable[[], None],
        terminal_evidence: dict[str, Any] | None = None,
        projection_facts: dict[str, Any] | None = None,
    ) -> None:
        workflow = workflow_for_state(next_state)
        transaction_id = str(uuid.uuid5(
            LEGACY_NAMESPACE,
            f"{self.project_id}:{run_id}:{previous_state}:{next_state}:{checkpoint}",
        ))
        events = self.ledger.read() if self.ledger.path.exists() else []
        terminal = next((
            item for item in events
            if item["transaction_id"] == transaction_id and item["event_type"] == "TransactionCompleted"
        ), None)
        if terminal is not None:
            # The terminal event proves that this exact compatibility write
            # already happened. Replaying the mutation would create a second
            # writer outside the immutable transaction history.
            return
        branch = self.inspector.current_branch or "DETACHED"
        head = self.inspector.head
        policy = MutationPolicy(tuple())
        self.ledger.append(
            event_type="TransactionStarted",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={
                "run_id": run_id,
                "feature_id": feature_id,
                "milestone": milestone,
                "starting_branch": branch,
                "starting_head": head,
                "allowed_mutation_policy": policy.to_dict(),
                "legacy_adapter": True,
            },
        )
        lease = self.lease.acquire(
            lease_type=WORKFLOW_LEASE[workflow],
            repository_identity=self.ledger.repository_identity,
            repository_path_fingerprint=self.ledger.repository_path_fingerprint,
            project_id=self.project_id,
            transaction_id=transaction_id,
            workflow_type=workflow,
            milestone=milestone,
            feature_id=feature_id,
            starting_branch=branch,
            starting_head=head,
            run_id=run_id,
            session_id=None,
            policy=policy,
        )
        self.ledger.append(
            event_type="LeaseAcquired",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={"lease_id": lease.lease_id, "lease_type": lease.lease_type.value, "legacy_adapter": True},
        )
        self.ledger.append(
            event_type="SnapshotCaptured",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={
                "snapshot": {
                    "repository_identity": self.ledger.repository_identity,
                    "repository_path_fingerprint": self.ledger.repository_path_fingerprint,
                    "branch": branch,
                    "head": head,
                    "tracked_diff_fingerprint": self.inspector.planning_diff_fingerprint(),
                    "untracked_file_hashes": self.inspector.untracked_file_hashes(),
                    "git_operations": self.inspector.git_operation_state(),
                },
                "legacy_adapter": True,
            },
        )
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={"legacy_adapter": True, "cache_transition": [previous_state, next_state]},
        )
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={"legacy_adapter": True, "application_source_written": False},
        )
        mutate()
        self.ledger.append(
            event_type="TransactionCompleted",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={
                "classification": "LEGACY_ADAPTER_TRANSITION",
                "feature_id": feature_id,
                "next_state": next_state,
                "terminal_snapshot": {
                    "branch": self.inspector.current_branch,
                    "head": self.inspector.head,
                    "clean": self.inspector.is_clean,
                },
                **(terminal_evidence or {}),
                "legacy_adapter": True,
            },
        )
        self.lease.release(
            transaction_id=transaction_id,
            workflow_type=workflow,
            repository_identity=self.ledger.repository_identity,
            project_id=self.project_id,
        )
        self.ledger.append(
            event_type="LeaseReleased",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={"lease_id": lease.lease_id, "legacy_adapter": True},
        )
        self.ledger.append(
            event_type="ProjectionUpdated",
            transaction_id=transaction_id,
            workflow_type=workflow,
            payload={
                "current_state": next_state,
                "current_feature": feature_id,
                "selected_feature": (
                    (terminal_evidence or {}).get("selected_feature")
                ),
                "projection_facts": projection_facts or {},
                "legacy_adapter": True,
            },
        )
        self.projection.rebuild(persist_cache=True)
