"""Generic transactional workflow coordinator for all writable phases."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .command_authority import CommandAuthority, CommandRecord
from .contracts import (
    LeaseType,
    MutationPolicy,
    PhaseTransaction,
    SessionResultEnvelope,
    TransactionState,
    WorkflowType,
    WORKFLOW_LEASE,
    WORKFLOW_SUCCESS_CLASSIFICATIONS,
    RepositorySnapshot,
    bind_human_gate, fingerprint,
)
from .errors import RepositoryError, TransactionError
from .ledger import EvidenceLedger
from .integration_executor import validate_integration_result
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowWriterLease
from .accepted_commit import finalize_accepted_commit


InterruptionHook = Callable[[str, PhaseTransaction], None]


def _noop_interruption(boundary: str, transaction: PhaseTransaction) -> None:
    return None


class WorkflowKernel:
    """Own identity, lease, evidence, validation, final commit, and projection.

    Workflow-specific decisions are supplied by phase adapters. This class has
    no feature/planning/integration conditional mutation paths.
    """

    def __init__(
        self,
        *,
        project: Project,
        ledger: EvidenceLedger,
        projection: ProjectionEngine,
        lease: WorkflowWriterLease,
        interruption_hook: InterruptionHook | None = None,
    ):
        self.project = project
        self.ledger = ledger
        self.projection = projection
        self.lease = lease
        self.inspector = RepositoryInspector(project.repository)
        self.interrupt = interruption_hook or _noop_interruption
        self.transaction: PhaseTransaction | None = None
        self.envelope: SessionResultEnvelope | None = None
        self.command_records: list[CommandRecord] = []
        self.final_commit: str | None = None
        self.validated_diff_fingerprint: str | None = None
        self.prepared_integration_paths: tuple[str, ...] | None = None
        self.prepared_integration_accepted_commit: str | None = None
        self._preacquired_lease_record: Any | None = None

    def restore(self, transaction_id: str) -> PhaseTransaction:
        """Rehydrate one interrupted transaction from immutable ledger evidence."""

        events = [
            event for event in self.ledger.read() if event["transaction_id"] == transaction_id
        ]
        if not events or events[0]["event_type"] != "TransactionStarted":
            raise TransactionError("cannot restore a transaction without its start event")
        start = events[0]
        payload = start["payload"]
        snapshot_event = next((event for event in events if event["event_type"] == "SnapshotCaptured"), None)
        snapshot_value = (snapshot_event or {}).get("payload", {}).get("snapshot") or {}
        policy_value = payload.get("allowed_mutation_policy") or {}
        policy = MutationPolicy(
            tuple(policy_value.get("allowed_paths") or ()),
            allowed_prefixes=tuple(policy_value.get("allowed_prefixes") or ()),
            denied_paths=tuple(policy_value.get("denied_paths") or ()),
            denied_prefixes=tuple(policy_value.get("denied_prefixes") or ()),
            allow_untracked=bool(policy_value.get("allow_untracked", False)),
            require_clean_start=bool(policy_value.get("require_clean_start", True)),
            commit_subject=policy_value.get("commit_subject"),
        )
        transaction = PhaseTransaction(
            schema_version=1,
            transaction_id=transaction_id,
            workflow_type=WorkflowType(start["workflow_type"]),
            project_id=self.project.project_id,
            repository_identity=self.ledger.repository_identity,
            repository_path_fingerprint=self.ledger.repository_path_fingerprint,
            milestone=payload.get("milestone"),
            feature_id=payload.get("feature_id"),
            run_id=payload.get("run_id"),
            session_ids=[
                event["payload"]["session_id"] for event in events
                if event["event_type"] == "SessionLaunched" and event["payload"].get("session_id")
            ],
            starting_branch=payload.get("starting_branch"),
            starting_head=payload.get("starting_head"),
            starting_queue_fingerprint=snapshot_value.get(
                "queue_fingerprint", payload.get("starting_queue_fingerprint")
            ),
            starting_tracked_diff_fingerprint=snapshot_value.get(
                "tracked_diff_fingerprint", payload.get("starting_tracked_diff_fingerprint", hashlib.sha256(b"").hexdigest())
            ),
            starting_untracked_fingerprint=snapshot_value.get(
                "untracked_fingerprint", payload.get("starting_untracked_fingerprint", hashlib.sha256(b"{}").hexdigest())
            ),
            allowed_mutation_policy=policy,
        )
        transaction.lease_identity = next((
            event["payload"].get("lease_id") for event in events
            if event["event_type"] == "LeaseAcquired"
        ), None)
        acquired_events = [event for event in events if event["event_type"] == "LeaseAcquired"]
        if acquired_events:
            transaction.lease_identity = acquired_events[-1]["payload"].get("lease_id")
        if any(event["event_type"] == "TransactionCompleted" for event in events):
            terminal = next(event for event in events if event["event_type"] == "TransactionCompleted")
            transaction.current_state = TransactionState.COMPLETED
            transaction.terminal_classification = terminal["payload"].get("classification")
            transaction.terminal_reference = terminal["payload"].get("reference")
            transaction.next_project_state = terminal["payload"].get("next_state")
        elif any(event["event_type"] in {"TransactionBlocked", "HumanGateRaised", "TransactionSuperseded"} for event in events):
            raise TransactionError("terminal non-completed transaction cannot be resumed")
        elif any(event["event_type"] in {"ValidationPassed", "CommitFinalized"} for event in events):
            transaction.current_state = TransactionState.FINALIZING
        elif any(event["event_type"] in {"SessionResultAccepted", "ValidationStarted", "ValidationFailed"} for event in events):
            transaction.current_state = TransactionState.VALIDATING
        elif any(event["event_type"] == "SessionLaunched" for event in events):
            transaction.current_state = TransactionState.RESULT_PENDING
        elif any(event["event_type"] == "LeaseAcquired" for event in events):
            transaction.current_state = TransactionState.ACTIVE
        else:
            transaction.current_state = TransactionState.LEASE_PENDING
        accepted = next((event for event in events if event["event_type"] == "SessionResultAccepted"), None)
        envelope_value = (accepted or {}).get("payload", {}).get("envelope")
        self.envelope = SessionResultEnvelope.from_dict(envelope_value) if isinstance(envelope_value, dict) else None
        validation = next((event for event in events if event["event_type"] == "ValidationPassed"), None)
        self.validated_diff_fingerprint = (validation or {}).get("payload", {}).get("diff_fingerprint")
        finalized = next((event for event in events if event["event_type"] == "CommitFinalized"), None)
        self.final_commit = (finalized or {}).get("payload", {}).get("commit")
        changes = next((event for event in events if event["event_type"] == "ChangesDetected"), None)
        if transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION and changes is not None:
            self.prepared_integration_paths = tuple(changes["payload"].get("changed_paths") or ())
        integration_target = next((
            event for event in events
            if event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "integration_target"
        ), None)
        if integration_target is not None:
            self.prepared_integration_accepted_commit = integration_target["payload"].get("accepted_commit")
        self.transaction = transaction
        return transaction

    def _require(self) -> PhaseTransaction:
        if self.transaction is None:
            raise TransactionError("workflow transaction has not begun")
        return self.transaction

    def begin(
        self,
        *,
        workflow_type: WorkflowType,
        milestone: str | None,
        feature_id: str | None,
        run_id: str,
        policy: MutationPolicy,
        transaction_id: str | None = None,
        start_evidence: dict[str, Any] | None = None,
        expected_starting_branch: str | None = None,
        expected_starting_head: str | None = None,
    ) -> PhaseTransaction:
        if self.transaction is not None:
            raise TransactionError("kernel instance already owns a transaction")
        self.interrupt("before_lease", PhaseTransaction.create(
            workflow_type=workflow_type,
            project_id=self.project.project_id,
            snapshot=capture_repository_snapshot(self.project),
            milestone=milestone,
            feature_id=feature_id,
            run_id=run_id,
            policy=policy,
            transaction_id=transaction_id,
        ))
        snapshot = capture_repository_snapshot(self.project)
        if any(snapshot.git_operations.values()):
            raise TransactionError("cannot begin a transaction during an active Git operation")
        if policy.require_clean_start and not snapshot.clean:
            raise TransactionError("new transaction requires a clean repository snapshot")
        if (
            expected_starting_branch is not None
            and snapshot.branch != expected_starting_branch
        ) or (
            expected_starting_head is not None
            and snapshot.head != expected_starting_head
        ):
            raise TransactionError("repository snapshot differs from the planned transaction start")
        transaction = PhaseTransaction.create(
            workflow_type=workflow_type,
            project_id=self.project.project_id,
            snapshot=snapshot,
            milestone=milestone,
            feature_id=feature_id,
            run_id=run_id,
            policy=policy,
            transaction_id=transaction_id,
        )
        self.transaction = transaction
        self._preacquire_lease(transaction)
        try:
            leased_snapshot = capture_repository_snapshot(self.project)
            if (
                leased_snapshot.repository_identity != transaction.repository_identity
                or leased_snapshot.repository_path_fingerprint
                != transaction.repository_path_fingerprint
                or leased_snapshot.branch != transaction.starting_branch
                or leased_snapshot.head != transaction.starting_head
                or leased_snapshot.queue_fingerprint
                != transaction.starting_queue_fingerprint
                or leased_snapshot.tracked_diff_fingerprint
                != transaction.starting_tracked_diff_fingerprint
                or leased_snapshot.untracked_fingerprint
                != transaction.starting_untracked_fingerprint
                or (policy.require_clean_start and not leased_snapshot.clean)
                or any(leased_snapshot.git_operations.values())
            ):
                raise TransactionError(
                    "repository changed before the planned writer lease was acquired"
                )
            extra_start = dict(start_evidence or {})
            if set(extra_start) & {
                "run_id", "milestone", "feature_id", "starting_branch", "starting_head",
                "starting_queue_fingerprint", "starting_tracked_diff_fingerprint",
                "starting_untracked_fingerprint", "allowed_mutation_policy",
            }:
                raise TransactionError("start evidence cannot override canonical transaction identity")
            self.ledger.append(
                event_type="TransactionStarted",
                transaction_id=transaction.transaction_id,
                workflow_type=workflow_type,
                payload={
                    "run_id": run_id,
                    "milestone": milestone,
                    "feature_id": feature_id,
                    "starting_branch": snapshot.branch,
                    "starting_head": snapshot.head,
                    "starting_queue_fingerprint": snapshot.queue_fingerprint,
                    "starting_tracked_diff_fingerprint": snapshot.tracked_diff_fingerprint,
                    "starting_untracked_fingerprint": snapshot.untracked_fingerprint,
                    "allowed_mutation_policy": policy.to_dict(),
                    **extra_start,
                },
            )
        except Exception:
            self._release_preacquired_lease(transaction)
            self.transaction = None
            raise
        transaction.transition(TransactionState.LEASE_PENDING)
        return transaction

    def begin_for_branch(
        self,
        *,
        workflow_type: WorkflowType,
        target_branch: str,
        milestone: str | None,
        feature_id: str | None,
        run_id: str,
        policy: MutationPolicy,
        transaction_id: str | None = None,
        expected_starting_branch: str | None = None,
        expected_starting_head: str | None = None,
    ) -> PhaseTransaction:
        """Begin against a target ref, then switch to it only under the lease."""

        if self.transaction is not None:
            raise TransactionError("kernel instance already owns a transaction")
        live = capture_repository_snapshot(self.project)
        target_head = self.inspector.rev_parse(target_branch, check=False)
        if target_head is None:
            raise TransactionError("target workflow branch does not exist")
        if (
            expected_starting_branch is not None
            and target_branch != expected_starting_branch
        ) or (
            expected_starting_head is not None
            and target_head != expected_starting_head
        ):
            raise TransactionError("target branch differs from the planned transaction start")
        if not live.clean or any(live.git_operations.values()):
            raise TransactionError("target-branch workflow requires a clean repository")
        queue_result = self.inspector.git(
            ["show", f"{target_head}:{self.project.queue_location}"], check=False
        )
        target_queue = (
            hashlib.sha256(queue_result.stdout.encode("utf-8")).hexdigest()
            if queue_result.returncode == 0 else None
        )
        target = replace(
            live, branch=target_branch, head=target_head,
            queue_fingerprint=target_queue,
        )
        transaction = PhaseTransaction.create(
            workflow_type=workflow_type, project_id=self.project.project_id,
            snapshot=target, milestone=milestone, feature_id=feature_id,
            run_id=run_id, policy=policy, transaction_id=transaction_id,
        )
        self.interrupt("before_lease", transaction)
        self.transaction = transaction
        self._preacquire_lease(transaction)
        try:
            leased_live = capture_repository_snapshot(self.project)
            leased_target_head = self.inspector.rev_parse(target_branch, check=False)
            leased_queue_result = self.inspector.git(
                ["show", f"{leased_target_head}:{self.project.queue_location}"],
                check=False,
            ) if leased_target_head is not None else None
            leased_target_queue = (
                hashlib.sha256(leased_queue_result.stdout.encode("utf-8")).hexdigest()
                if leased_queue_result is not None
                and leased_queue_result.returncode == 0
                else None
            )
            if (
                leased_live.repository_identity != transaction.repository_identity
                or leased_live.repository_path_fingerprint
                != transaction.repository_path_fingerprint
                or leased_target_head != transaction.starting_head
                or leased_target_queue != transaction.starting_queue_fingerprint
                or leased_live.tracked_diff_fingerprint
                != transaction.starting_tracked_diff_fingerprint
                or leased_live.untracked_fingerprint
                != transaction.starting_untracked_fingerprint
                or not leased_live.clean
                or any(leased_live.git_operations.values())
            ):
                raise TransactionError(
                    "repository changed before the planned branch writer lease was acquired"
                )
            self.ledger.append(
                event_type="TransactionStarted", transaction_id=transaction.transaction_id,
                workflow_type=workflow_type,
                payload={
                    "run_id": run_id, "milestone": milestone, "feature_id": feature_id,
                    "starting_branch": target_branch, "starting_head": target_head,
                    "starting_queue_fingerprint": target.queue_fingerprint,
                    "starting_tracked_diff_fingerprint": target.tracked_diff_fingerprint,
                    "starting_untracked_fingerprint": target.untracked_fingerprint,
                    "allowed_mutation_policy": policy.to_dict(),
                    "provisional_observed_branch": live.branch,
                    "provisional_observed_head": live.head,
                },
            )
        except Exception:
            self._release_preacquired_lease(transaction)
            self.transaction = None
            raise
        transaction.transition(TransactionState.LEASE_PENDING)
        return transaction

    def prepare_starting_branch(self) -> None:
        transaction = self._require()
        self._revalidate_lease()
        if not self.inspector.is_clean or any(self.inspector.git_operation_state().values()):
            raise TransactionError("cannot prepare workflow branch from a dirty repository")
        if self.inspector.current_branch != transaction.starting_branch:
            self._run_git(["switch", transaction.starting_branch])
        if self.inspector.head != transaction.starting_head:
            raise TransactionError("prepared workflow branch HEAD differs from transaction start")

    def checkpoint(self, name: str, payload: dict[str, Any] | None = None) -> None:
        transaction = self._require()
        self._revalidate_lease()
        self.ledger.append(
            event_type="CheckpointRecorded", transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"checkpoint": name, **(payload or {})},
        )

    def prepare_integration_changes(self, accepted_commit: str) -> tuple[str, ...]:
        transaction = self._require()
        if transaction.workflow_type != WorkflowType.MILESTONE_INTEGRATION:
            raise TransactionError("integration preparation requires milestone_integration")
        if transaction.current_state != TransactionState.VALIDATING:
            raise TransactionError("integration changes require an accepted typed result")
        self._revalidate_lease()
        if self.inspector.head != transaction.starting_head or self.inspector.current_branch != transaction.starting_branch:
            raise TransactionError("integration branch changed before applying accepted commit")
        accepted_commit = self.inspector.rev_parse(accepted_commit) or accepted_commit
        if self.inspector.is_ancestor(accepted_commit, transaction.starting_branch):
            raise TransactionError("accepted commit is already integrated by ancestry")
        prior_integrations = [
            event for event in self.ledger.read()
            if event["event_type"] == "TransactionCompleted"
            and event["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value
            and (
                event["payload"].get("feature_id") == transaction.feature_id
                or event["payload"].get("accepted_feature_commit") == accepted_commit
            )
        ]
        if prior_integrations:
            raise TransactionError("feature or accepted commit already has terminal integration evidence")
        if transaction.feature_id:
            feature = FeatureQueue.from_location(
                self.project.repository, self.project.queue_location
            ).feature(transaction.feature_id)
            if feature and (
                feature.get("status") == "integrated"
                or isinstance(feature.get("integrated_commit"), str)
            ):
                raise TransactionError("feature queue already records completed integration")
        accepted_patch = self.inspector.patch_fingerprint(accepted_commit)
        history = self.inspector.git([
            "rev-list", transaction.starting_head
        ]).stdout.splitlines()
        for historical_commit in history:
            if self.inspector.rev_parse(f"{historical_commit}^", check=False) is None:
                continue
            if self.inspector.patch_fingerprint(historical_commit) == accepted_patch:
                raise TransactionError("accepted patch is already present in milestone history")
        paths = tuple(sorted(self.inspector.git([
            "diff-tree", "--no-commit-id", "--name-only", "-r", accepted_commit
        ]).stdout.splitlines()))
        if not paths:
            raise TransactionError("accepted integration commit has no changed paths")
        transaction.allowed_mutation_policy.validate(paths)
        self.checkpoint("integration_target", {
            "accepted_commit": accepted_commit,
            "accepted_patch_fingerprint": accepted_patch,
            "expected_paths": list(paths),
        })
        self._run_git(["cherry-pick", "--no-commit", accepted_commit])
        prepared_paths = self._changed_paths()
        if not prepared_paths:
            raise TransactionError("integration preparation produced no repository diff")
        if prepared_paths != paths:
            raise TransactionError("prepared integration diff does not exactly match accepted paths")
        self.prepared_integration_paths = paths
        self.prepared_integration_accepted_commit = accepted_commit
        return paths

    def acquire_lease(self) -> dict[str, Any]:
        transaction = self._require()
        record = self._preacquired_lease_record
        if record is None:
            record = self.lease.acquire(
            lease_type=WORKFLOW_LEASE[transaction.workflow_type],
            repository_identity=transaction.repository_identity,
            repository_path_fingerprint=transaction.repository_path_fingerprint,
            project_id=transaction.project_id,
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            milestone=transaction.milestone,
            feature_id=transaction.feature_id,
            starting_branch=transaction.starting_branch,
            starting_head=transaction.starting_head,
            run_id=transaction.run_id,
            session_id=None,
                policy=transaction.allowed_mutation_policy,
            )
        else:
            self._revalidate_lease()
        self._preacquired_lease_record = None
        transaction.lease_identity = record.lease_id
        transaction.transition(TransactionState.ACTIVE)
        self.ledger.append(
            event_type="LeaseAcquired",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "lease_id": record.lease_id,
                "lease_type": record.lease_type.value,
                "owner_pid": record.owner_pid,
                "owner_process_start": record.owner_process_start,
                "starting_branch": record.starting_branch,
                "starting_head": record.starting_head,
            },
        )
        self.interrupt("after_lease", transaction)
        return record.to_dict()

    def _preacquire_lease(self, transaction: PhaseTransaction) -> None:
        if self._preacquired_lease_record is not None:
            record = self._revalidate_lease()
            transaction.lease_identity = record.lease_id
            return
        record = self.lease.acquire(
            lease_type=WORKFLOW_LEASE[transaction.workflow_type],
            repository_identity=transaction.repository_identity,
            repository_path_fingerprint=transaction.repository_path_fingerprint,
            project_id=transaction.project_id,
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            milestone=transaction.milestone,
            feature_id=transaction.feature_id,
            starting_branch=transaction.starting_branch,
            starting_head=transaction.starting_head,
            run_id=transaction.run_id,
            session_id=None,
            policy=transaction.allowed_mutation_policy,
        )
        transaction.lease_identity = record.lease_id
        self._preacquired_lease_record = record

    def _release_preacquired_lease(self, transaction: PhaseTransaction) -> None:
        if self._preacquired_lease_record is None:
            return
        self._revalidate_lease()
        self.lease.release(
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            repository_identity=transaction.repository_identity,
            project_id=transaction.project_id,
        )
        self._preacquired_lease_record = None

    def capture_snapshot(self) -> dict[str, Any]:
        transaction = self._require()
        self._revalidate_lease()
        snapshot = capture_repository_snapshot(self.project)
        if (
            snapshot.repository_identity != transaction.repository_identity
            or snapshot.repository_path_fingerprint != transaction.repository_path_fingerprint
            or snapshot.branch != transaction.starting_branch
            or snapshot.head != transaction.starting_head
            or snapshot.queue_fingerprint != transaction.starting_queue_fingerprint
            or snapshot.tracked_diff_fingerprint != transaction.starting_tracked_diff_fingerprint
            or snapshot.untracked_fingerprint != transaction.starting_untracked_fingerprint
        ):
            raise TransactionError(
                "repository changed between transaction start and snapshot capture: "
                f"branch={snapshot.branch == transaction.starting_branch} "
                f"head={snapshot.head == transaction.starting_head} "
                f"queue={snapshot.queue_fingerprint == transaction.starting_queue_fingerprint} "
                f"tracked={snapshot.tracked_diff_fingerprint == transaction.starting_tracked_diff_fingerprint} "
                f"untracked={snapshot.untracked_fingerprint == transaction.starting_untracked_fingerprint}"
            )
        self.ledger.append(
            event_type="SnapshotCaptured",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"snapshot": snapshot.to_dict()},
        )
        self.interrupt("after_snapshot", transaction)
        return snapshot.to_dict()

    def session_launched(self, session_id: str) -> None:
        transaction = self._require()
        self._revalidate_lease()
        self._validate_mutation_paths(transaction.allowed_mutation_policy.allowed_paths)
        if transaction.current_state not in {TransactionState.ACTIVE, TransactionState.RESULT_PENDING}:
            raise TransactionError("session launch requires an active or result-pending transaction")
        self.lease.bind_session(
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            repository_identity=transaction.repository_identity,
            project_id=transaction.project_id,
            session_id=session_id,
        )
        if session_id not in transaction.session_ids:
            transaction.session_ids.append(session_id)
        transaction.transition(TransactionState.RESULT_PENDING)
        self.ledger.append(
            event_type="SessionLaunched",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"session_id": session_id},
        )
        self.interrupt("after_session_launch", transaction)

    def accept_result(self, envelope: SessionResultEnvelope) -> None:
        transaction = self._require()
        lease_record = self._revalidate_lease()
        expected = {
            "workflow_type": envelope.workflow_type == transaction.workflow_type,
            "project_id": envelope.project_id == transaction.project_id,
            "repository_identity": envelope.repository_identity == transaction.repository_identity,
            "transaction_id": envelope.transaction_id == transaction.transaction_id,
            "run_id": envelope.run_id == transaction.run_id,
            "session_id": bool(
                transaction.session_ids
                and envelope.session_id == transaction.session_ids[-1]
                and envelope.session_id == lease_record.session_id
            ),
            "starting_branch": envelope.starting_branch == transaction.starting_branch,
            "starting_commit": envelope.starting_commit == transaction.starting_head,
            "current_commit": envelope.current_commit == transaction.starting_head == self.inspector.head,
            "feature_id": envelope.feature_id == transaction.feature_id,
        }
        if not all(expected.values()):
            failed = ", ".join(key for key, passed in expected.items() if not passed)
            raise TransactionError(f"session-result identity mismatch: {failed}")
        if self.envelope is not None:
            raise TransactionError("transaction already accepted a session result")
        transaction.allowed_mutation_policy.validate(envelope.changed_paths)
        observed_paths = self._changed_paths()
        self._validate_mutation_paths(observed_paths)
        observed_diff_fingerprint = (
            self.inspector.content_diff_fingerprint(observed_paths)
            if observed_paths == tuple(envelope.changed_paths) else None
        )
        self.envelope = envelope
        transaction.transition(TransactionState.VALIDATING)
        self.ledger.append(
            event_type="SessionResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": envelope.classification,
                "session_id": envelope.session_id,
                "current_commit": envelope.current_commit,
                "changed_paths": list(envelope.changed_paths),
                "next_state": envelope.next_state,
                "observed_diff_fingerprint": observed_diff_fingerprint,
                "envelope": envelope.to_dict(),
            },
        )
        self.interrupt("after_session_result", transaction)

    def accept_deterministic_integration_result(
        self, plan: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Accept controller-plan executor evidence without inventing a session."""

        transaction = self._require()
        if transaction.workflow_type != WorkflowType.MILESTONE_INTEGRATION:
            raise TransactionError(
                "deterministic integration results require milestone_integration"
            )
        if transaction.current_state != TransactionState.ACTIVE:
            raise TransactionError(
                "deterministic integration result requires an active transaction"
            )
        self._revalidate_lease()
        validate_integration_result(plan, result)
        recovery = result.get("recovery")
        if recovery is not None:
            if (
                not isinstance(recovery, dict)
                or recovery.get("blocked_transaction_id") != plan["transaction_id"]
                or recovery.get("integrating_metadata_commit")
                != transaction.starting_head
                or recovery.get("pre_integration_head")
                != plan["pre_integration_head"]
            ):
                raise TransactionError(
                    "deterministic integration recovery identity is inconsistent"
                )
        transaction.transition(TransactionState.RESULT_PENDING)
        self.ledger.append(
            event_type="DeterministicExecutionStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "plan_fingerprint": plan["plan_fingerprint"],
                "plan_path": plan.get("controller_plan_path"),
                "model_session_launched": False,
                "recovered_transaction_id": (
                    recovery.get("blocked_transaction_id")
                    if isinstance(recovery, dict) else None
                ),
            },
        )
        transaction.transition(TransactionState.VALIDATING)
        self.ledger.append(
            event_type="DeterministicResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": result["classification"],
                "plan_fingerprint": plan["plan_fingerprint"],
                "current_commit": result["current_commit"],
                "changed_paths": result["changed_paths"],
                "model_session_launched": False,
            },
        )
        classification = result["classification"]
        if classification == "SEMANTIC_CONFLICT":
            return
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": result["changed_paths"],
                "executor": "controller_plan",
            },
        )
        validation = result.get("validation")
        if classification == "VALIDATION_FAILED":
            blockers = validation.get("blockers", []) if isinstance(validation, dict) else []
            self.ledger.append(
                event_type="ValidationFailed",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "diagnostic": "deterministic integration validation failed",
                    "blockers": blockers,
                },
            )
            return
        if classification != "INTEGRATED" or not isinstance(validation, dict) or not validation.get("ok"):
            raise TransactionError(
                "deterministic integration success lacks passing validation evidence"
            )
        final_commit = result.get("evidence_commit")
        resulting_feature_commit = result.get("resulting_feature_commit")
        if (
            not isinstance(final_commit, str)
            or not isinstance(resulting_feature_commit, str)
            or self.inspector.rev_parse(final_commit, check=False) != final_commit
            or self.inspector.rev_parse(resulting_feature_commit, check=False)
            != resulting_feature_commit
        ):
            raise TransactionError(
                "deterministic integration result lacks exact final commit identities"
            )
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commands": validation.get("commands", []),
                "warnings": [],
                "validated_commit": validation.get("validated_commit"),
                "executor": "controller_plan",
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        self.final_commit = final_commit
        self.prepared_integration_accepted_commit = plan["accepted_commit"]
        final_paths = tuple(sorted(self.inspector.changed_paths(final_commit)))
        transaction.allowed_mutation_policy.validate(final_paths)
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": final_commit,
                "parent": self.inspector.rev_parse(f"{final_commit}^", check=False),
                "changed_paths": list(final_paths),
                "diff_fingerprint": self.inspector.patch_fingerprint(final_commit),
                "accepted_commit": plan["accepted_commit"],
                "resulting_feature_commit": resulting_feature_commit,
                "integration_execution_mode": (
                    "controller_plan_recovery"
                    if isinstance(recovery, dict) else "controller_plan"
                ),
                "recovered_transaction_id": (
                    recovery.get("blocked_transaction_id")
                    if isinstance(recovery, dict) else None
                ),
                "recovered_pre_integration_head": (
                    recovery.get("pre_integration_head")
                    if isinstance(recovery, dict) else None
                ),
                "integrating_metadata_commit": result.get("integrating_metadata_commit"),
                "integration_once": True,
                "plan_fingerprint": plan["plan_fingerprint"],
            },
        )
        transaction.next_project_state = (
            "milestone_gate" if result.get("milestone_complete") is True else "feature_ready"
        )

    def finalize_deterministic_planning_recovery(
        self,
        *,
        original_transaction_id: str,
        changed_paths: tuple[str, ...],
        expected_diff_fingerprint: str,
        plan_fingerprint: str,
        validation_evidence: dict[str, Any],
        selected_feature: str | None,
        next_state: str,
    ) -> str:
        """Commit one exact dirty planning baseline without inventing a model session."""

        transaction = self._require()
        if transaction.workflow_type != WorkflowType.RECOVERY:
            raise TransactionError("planning finalization requires a recovery transaction")
        if transaction.current_state != TransactionState.ACTIVE:
            raise TransactionError("planning recovery must finalize from an active transaction")
        if transaction.session_ids:
            raise TransactionError("deterministic planning recovery cannot own a model session")
        if transaction.allowed_mutation_policy.require_clean_start:
            raise TransactionError("planning recovery must adopt an exact dirty starting snapshot")
        self._revalidate_lease()
        observed_paths = tuple(sorted(
            set(self.inspector.tracked_changed_paths())
            | set(self.inspector.untracked_file_hashes())
        ))
        if observed_paths != tuple(sorted(changed_paths)):
            raise TransactionError("planning recovery changed paths differ from the reserved baseline")
        if self.inspector.untracked_file_hashes():
            raise TransactionError("planning recovery refuses untracked application content")
        if self.inspector.planning_diff_fingerprint() != expected_diff_fingerprint:
            raise TransactionError("planning recovery diff differs from the reserved baseline")
        if (
            transaction.starting_tracked_diff_fingerprint != expected_diff_fingerprint
            or transaction.starting_untracked_fingerprint
            != capture_repository_snapshot(self.project).untracked_fingerprint
        ):
            raise TransactionError("planning recovery starting snapshot changed")
        transaction.allowed_mutation_policy.validate(observed_paths)

        transaction.transition(TransactionState.RESULT_PENDING)
        self.ledger.append(
            event_type="DeterministicExecutionStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "plan_fingerprint": plan_fingerprint,
                "model_session_launched": False,
                "recovered_transaction_id": original_transaction_id,
            },
        )
        transaction.transition(TransactionState.VALIDATING)
        self.ledger.append(
            event_type="DeterministicResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": "RECOVERY_APPLIED",
                "plan_fingerprint": plan_fingerprint,
                "current_commit": transaction.starting_head,
                "changed_paths": list(observed_paths),
                "model_session_launched": False,
            },
        )
        self.ledger.append(
            event_type="ChangesDetected",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": list(observed_paths),
                "diff_fingerprint": expected_diff_fingerprint,
                "adopted_existing_planning_diff": True,
            },
        )
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": list(observed_paths),
                "executor": "deterministic_planning_finalization",
            },
        )
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commands": validation_evidence.get("commands", []),
                "warnings": validation_evidence.get("warnings", []),
                "diff_fingerprint": expected_diff_fingerprint,
                "executor": "deterministic_planning_finalization",
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        subject = transaction.allowed_mutation_policy.commit_subject
        if not subject:
            raise TransactionError("planning recovery lacks an exact commit subject")
        self.inspector.stage_planning_paths(list(observed_paths), commit_subject=subject)
        if tuple(self.inspector.staged_changed_paths()) != observed_paths:
            raise TransactionError("planning recovery staged paths differ from the reserved baseline")
        commit = self.inspector.commit_planning_paths(list(observed_paths), commit_subject=subject)
        if (
            self.inspector.rev_parse(f"{commit}^", check=False) != transaction.starting_head
            or tuple(self.inspector.changed_paths(commit)) != observed_paths
            or not self.inspector.is_clean
        ):
            raise TransactionError("planning recovery did not create one exact clean commit")
        self.final_commit = commit
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": commit,
                "parent": transaction.starting_head,
                "changed_paths": list(observed_paths),
                "diff_fingerprint": self.inspector.patch_fingerprint(commit),
                "source_diff_fingerprint": expected_diff_fingerprint,
                "commit_subject": subject,
                "recovered_transaction_id": original_transaction_id,
                "model_session_launched": False,
            },
        )
        self.ledger.append(
            event_type="RecoveryApplied",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "recovered_transaction_id": original_transaction_id,
                "classification": "planning_finalization_recovery",
                "selected_feature": selected_feature,
                "model_session_launched": False,
            },
        )
        transaction.next_project_state = next_state
        return commit

    def adopt_committed_planning_recovery(
        self, *, original_transaction_id: str, commit: str, expected_parent: str,
        expected_paths: tuple[str, ...], expected_subject: str, plan_fingerprint: str,
        selected_feature: str | None, next_state: str,
    ) -> str:
        """Record fresh terminal evidence for an existing, verified planning commit."""
        transaction = self._require()
        if transaction.workflow_type != WorkflowType.RECOVERY or transaction.current_state != TransactionState.ACTIVE:
            raise TransactionError("committed planning recovery requires an active recovery transaction")
        if transaction.session_ids or self.inspector.head != commit or self.inspector.current_branch != transaction.starting_branch:
            raise TransactionError("committed planning recovery topology changed")
        if self.inspector.rev_parse(f"{commit}^", check=False) != expected_parent:
            raise TransactionError("committed planning recovery parent changed")
        if self.inspector.commit_subject(commit) != expected_subject or not self.inspector.is_clean:
            raise TransactionError("committed planning recovery commit or cleanliness changed")
        paths = tuple(sorted(self.inspector.changed_paths(commit)))
        if paths != tuple(sorted(expected_paths)):
            raise TransactionError("committed planning recovery paths changed")
        self._revalidate_lease()
        transaction.transition(TransactionState.RESULT_PENDING)
        for event_type, payload in (
            ("DeterministicExecutionStarted", {"plan_fingerprint": plan_fingerprint, "model_session_launched": False, "recovered_transaction_id": original_transaction_id}),
            ("DeterministicResultAccepted", {"classification": "RECOVERY_APPLIED", "current_commit": commit, "changed_paths": list(paths), "model_session_launched": False}),
            ("ValidationStarted", {"changed_paths": list(paths), "executor": "committed_planning_finalization"}),
            ("ValidationPassed", {"commands": [], "preserved_existing_commit": commit}),
        ):
            if event_type == "DeterministicResultAccepted":
                transaction.transition(TransactionState.VALIDATING)
            self.ledger.append(event_type=event_type, transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type, payload=payload)
        transaction.transition(TransactionState.FINALIZING)
        self.final_commit = commit
        self.ledger.append(event_type="CommitFinalized", transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type, payload={"commit": commit, "parent": expected_parent,
                "changed_paths": list(paths), "diff_fingerprint": self.inspector.patch_fingerprint(commit),
                "commit_subject": expected_subject, "reused_existing_commit": True,
                "recovered_transaction_id": original_transaction_id})
        self.ledger.append(event_type="RecoveryApplied", transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type, payload={"recovered_transaction_id": original_transaction_id,
                "classification": "queue_reconciliation_committed_finalization_recovery",
                "selected_feature": selected_feature, "model_session_launched": False})
        transaction.next_project_state = next_state
        return commit

    def finalize_deterministic_feature_recovery(
        self,
        *,
        original_transaction_id: str,
        changed_paths: tuple[str, ...],
        original_content_fingerprint: str,
        plan_fingerprint: str,
        validation_evidence: dict[str, Any],
    ) -> str:
        """Validate and commit one preserved feature diff without a model session."""

        transaction = self._require()
        if transaction.workflow_type != WorkflowType.FEATURE_EXECUTION:
            raise TransactionError("feature-result recovery requires feature_execution")
        if transaction.current_state != TransactionState.ACTIVE:
            raise TransactionError("feature-result recovery must finalize from an active transaction")
        if transaction.session_ids:
            raise TransactionError("feature-result recovery cannot own a model session")
        if transaction.allowed_mutation_policy.require_clean_start:
            raise TransactionError("feature-result recovery must adopt an exact dirty baseline")
        self._revalidate_lease()
        observed_paths = tuple(sorted(
            set(self.inspector.tracked_changed_paths())
            | set(self.inspector.untracked_file_hashes())
        ))
        if observed_paths != tuple(sorted(changed_paths)):
            raise TransactionError(
                "feature-result recovery changed paths differ from the reserved baseline"
            )
        transaction.allowed_mutation_policy.validate(observed_paths)
        if not validation_evidence.get("all_required_passed"):
            raise TransactionError("feature-result recovery lacks passing host validation")

        transaction.transition(TransactionState.RESULT_PENDING)
        self.ledger.append(
            event_type="DeterministicExecutionStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "plan_fingerprint": plan_fingerprint,
                "model_session_launched": False,
                "recovered_transaction_id": original_transaction_id,
                "execution_mode": "preserved_feature_result_recovery",
            },
        )
        transaction.transition(TransactionState.VALIDATING)
        self.ledger.append(
            event_type="DeterministicResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": "FEATURE_ACCEPTED",
                "plan_fingerprint": plan_fingerprint,
                "current_commit": transaction.starting_head,
                "changed_paths": list(observed_paths),
                "model_session_launched": False,
            },
        )
        final_content_fingerprint = self.inspector.content_diff_fingerprint(observed_paths)
        self.ledger.append(
            event_type="ChangesDetected",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": list(observed_paths),
                "diff_fingerprint": final_content_fingerprint,
                "original_content_fingerprint": original_content_fingerprint,
                "adopted_existing_feature_diff": True,
            },
        )
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": list(observed_paths),
                "executor": "controller_host_feature_result_recovery",
            },
        )
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commands": validation_evidence.get("commands", []),
                "checks": validation_evidence.get("checks", []),
                "warnings": validation_evidence.get("warnings", []),
                "diff_fingerprint": final_content_fingerprint,
                "executor": "controller_host_feature_result_recovery",
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        subject = transaction.allowed_mutation_policy.commit_subject
        if not subject:
            raise TransactionError("feature-result recovery lacks an exact commit subject")
        self._run_git(["add", "--", *observed_paths])
        if tuple(self.inspector.staged_changed_paths()) != observed_paths:
            raise TransactionError("feature-result recovery staged paths differ from the baseline")
        self._run_git(["commit", "-m", subject, "--", *observed_paths])
        commit = self.inspector.head
        final_checks = {
            "direct_parent": self.inspector.rev_parse(f"{commit}^", check=False)
            == transaction.starting_head,
            "changed_paths": tuple(sorted(self.inspector.changed_paths(commit)))
            == observed_paths,
            "content_fingerprint": self.inspector.content_diff_fingerprint(
                observed_paths, commit=commit
            ) == final_content_fingerprint,
            "clean_repository": self.inspector.is_clean,
        }
        if not all(final_checks.values()):
            failed = ", ".join(key for key, passed in final_checks.items() if not passed)
            raise TransactionError(
                "feature-result recovery did not create one exact clean commit: " + failed
            )
        self.final_commit = commit
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": commit,
                "parent": transaction.starting_head,
                "changed_paths": list(observed_paths),
                "diff_fingerprint": self.inspector.patch_fingerprint(commit),
                "content_fingerprint": final_content_fingerprint,
                "commit_subject": subject,
                "recovered_transaction_id": original_transaction_id,
                "model_session_launched": False,
            },
        )
        self.ledger.append(
            event_type="RecoveryApplied",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "recovered_transaction_id": original_transaction_id,
                "classification": "preserved_feature_result_recovery",
                "selected_feature": transaction.feature_id,
                "accepted_feature_commit": commit,
                "model_session_launched": False,
            },
        )
        transaction.next_project_state = "integration_pending"
        return commit

    def finalize_deterministic_accepted_commit(
        self,
        *,
        candidate_commit: str,
        milestone_id: str,
        milestone_branch: str,
        milestone_base: str,
        feature_branch: str,
        metadata_paths: tuple[str, ...],
        validation_evidence: dict[str, Any],
        recovery_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconstruct one direct-child accepted commit without a model session."""

        transaction = self._require()
        if transaction.workflow_type not in {
            WorkflowType.FEATURE_ACCEPTANCE,
            WorkflowType.RECOVERY,
        }:
            raise TransactionError(
                "accepted-commit finalization requires feature_acceptance or recovery"
            )
        if transaction.current_state != TransactionState.ACTIVE:
            raise TransactionError(
                "accepted-commit finalization requires an active transaction"
            )
        if transaction.session_ids:
            raise TransactionError(
                "deterministic accepted-commit finalization cannot own a model session"
            )
        if transaction.starting_head != candidate_commit:
            raise TransactionError(
                "accepted-commit candidate differs from the transaction start"
            )
        if tuple(sorted(metadata_paths)) != metadata_paths:
            raise TransactionError(
                "accepted-commit metadata paths must be sorted and unique"
            )
        transaction.allowed_mutation_policy.validate(metadata_paths)
        if not all(
            validation_evidence.get(key) is True
            for key in ("tests_passed", "review_passed", "documentation_current")
        ):
            raise TransactionError(
                "accepted-commit finalization lacks complete validation evidence"
            )
        self._revalidate_lease()
        observed_paths = tuple(self.inspector.tracked_changed_paths())
        if observed_paths != metadata_paths or self.inspector.untracked_file_hashes():
            raise TransactionError(
                "accepted-commit metadata differs from the authorized worktree"
            )

        transaction.transition(TransactionState.RESULT_PENDING)
        self.ledger.append(
            event_type="DeterministicExecutionStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "execution_mode": "immutable_accepted_commit_finalization",
                "candidate_implementation_commit": candidate_commit,
                "model_session_launched": False,
                **(recovery_evidence or {}),
            },
        )
        transaction.transition(TransactionState.VALIDATING)
        self.ledger.append(
            event_type="DeterministicResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": (
                    "RECOVERY_APPLIED"
                    if transaction.workflow_type == WorkflowType.RECOVERY
                    else "FEATURE_ACCEPTED"
                ),
                "candidate_implementation_commit": candidate_commit,
                "changed_paths": list(metadata_paths),
                "model_session_launched": False,
            },
        )
        metadata_fingerprint = self.inspector.content_diff_fingerprint(metadata_paths)
        self.ledger.append(
            event_type="ChangesDetected",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "changed_paths": list(metadata_paths),
                "diff_fingerprint": metadata_fingerprint,
                "authorized_acceptance_metadata": True,
            },
        )
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "candidate_implementation_commit": candidate_commit,
                "changed_paths": list(metadata_paths),
                "executor": "deterministic_accepted_commit_finalizer",
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        finalization = finalize_accepted_commit(
            project=self.project,
            controller_project_id=transaction.project_id,
            feature_id=str(transaction.feature_id),
            feature_branch=feature_branch,
            milestone_id=milestone_id,
            milestone_branch=milestone_branch,
            milestone_base=milestone_base,
            candidate_commit=candidate_commit,
            metadata_paths=metadata_paths,
            commit_subject=str(
                transaction.allowed_mutation_policy.commit_subject
                or f"{transaction.feature_id}: accepted feature"
            ),
        )
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "executor": "deterministic_accepted_commit_finalizer",
                "validation_evidence": validation_evidence,
                "immutable_metadata_validation": finalization[
                    "immutable_metadata_validation"
                ],
                "implementation_tree_equivalent": finalization[
                    "implementation_tree_equivalent"
                ],
            },
        )
        finalized = str(finalization["finalized_accepted_commit"])
        self.final_commit = finalized
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": finalized,
                "parent": milestone_base,
                "changed_paths": finalization["accepted_changed_paths"],
                "diff_fingerprint": self.inspector.patch_fingerprint(finalized),
                "commit_subject": transaction.allowed_mutation_policy.commit_subject,
                "accepted_commit_finalization": True,
                **{
                    key: finalization[key]
                    for key in (
                        "candidate_implementation_commit",
                        "finalized_accepted_commit",
                        "candidate_changed_paths",
                        "authorized_metadata_paths",
                        "candidate_to_finalized_changed_paths",
                        "candidate_implementation_tree_fingerprint",
                        "finalized_implementation_tree_fingerprint",
                        "implementation_tree_equivalent",
                        "branch_movement",
                    )
                },
            },
        )
        if transaction.workflow_type == WorkflowType.RECOVERY:
            self.ledger.append(
                event_type="RecoveryApplied",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "classification": "accepted_commit_reconstruction",
                    "selected_feature": transaction.feature_id,
                    "candidate_implementation_commit": candidate_commit,
                    "finalized_accepted_commit": finalized,
                    "accepted_feature_commit": finalized,
                    "model_session_launched": False,
                    **(recovery_evidence or {}),
                },
            )
        transaction.next_project_state = "integration_ready"
        return finalization

    def record_file_mutation_boundary(self) -> None:
        transaction = self._require()
        if any(
            event["transaction_id"] == transaction.transaction_id
            and event["event_type"] == "ChangesDetected"
            for event in self.ledger.read()
        ):
            raise TransactionError("transaction already has a durable mutation boundary")
        paths = self._changed_paths()
        self._validate_mutation_paths(paths)
        self._validate_untracked_policy()
        transaction.allowed_mutation_policy.validate(paths)
        self.ledger.append(
            event_type="ChangesDetected",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"changed_paths": list(paths), "diff_fingerprint": self._diff_fingerprint()},
        )
        self.interrupt("after_file_mutation", transaction)

    def validate(
        self,
        *,
        authority: CommandAuthority,
        command_results: Iterable[CommandRecord],
        semantic_validator: Callable[[SessionResultEnvelope, tuple[str, ...]], None] | None = None,
    ) -> list[dict[str, Any]]:
        transaction = self._require()
        envelope = self.envelope
        if envelope is None or transaction.current_state != TransactionState.VALIDATING:
            raise TransactionError("validation requires one accepted session result")
        self.interrupt("before_validation", transaction)
        self._revalidate_lease()
        pre_snapshot_paths = self._changed_paths()
        self._validate_mutation_paths(pre_snapshot_paths)
        current = capture_repository_snapshot(self.project)
        if current.branch != transaction.starting_branch:
            raise TransactionError("starting branch changed before finalization")
        if current.head != transaction.starting_head:
            raise TransactionError("starting HEAD changed before kernel finalization")
        paths = pre_snapshot_paths
        self._validate_untracked_policy()
        transaction.allowed_mutation_policy.validate(paths)
        allowed_envelope_paths = {paths}
        if (
            transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION
            and self.prepared_integration_paths == paths
        ):
            # The integration session is normally read-only; compatibility
            # launchers may report the exact accepted paths it authorized.
            allowed_envelope_paths.add(())
        if tuple(envelope.changed_paths) not in allowed_envelope_paths:
            raise TransactionError(
                "session-result changed paths do not match repository evidence: "
                f"session={tuple(envelope.changed_paths)!r} allowed={sorted(allowed_envelope_paths)!r} "
                f"observed={paths!r} prepared={self.prepared_integration_paths!r}"
            )
        current_diff_fingerprint = self._diff_fingerprint()
        self._assert_durable_mutation_boundary(paths, current_diff_fingerprint)
        self.validated_diff_fingerprint = current_diff_fingerprint
        self.ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"changed_paths": list(paths)},
        )
        try:
            records, warnings = authority.validate(command_results)
            if envelope.classification not in WORKFLOW_SUCCESS_CLASSIFICATIONS[transaction.workflow_type]:
                raise TransactionError(
                    f"{envelope.classification} is a terminal non-success result and cannot finalize"
                )
            if semantic_validator is not None:
                semantic_validator(envelope, paths)
        except TransactionError as exc:
            self.ledger.append(
                event_type="ValidationFailed",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={"diagnostic": str(exc)},
            )
            raise
        self.command_records = list(records)
        self.ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commands": [item.to_dict() for item in records],
                "warnings": [item.to_dict() for item in warnings],
                "diff_fingerprint": self.validated_diff_fingerprint,
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        self.interrupt("after_validation", transaction)
        return [item.to_dict() for item in records]

    def finalize(self) -> str | None:
        transaction = self._require()
        if transaction.current_state != TransactionState.FINALIZING:
            raise TransactionError("finalization requires passed validation")
        self._revalidate_lease()
        self.interrupt("before_commit", transaction)
        paths = self._changed_paths()
        self._validate_mutation_paths(paths)
        current = capture_repository_snapshot(self.project)
        if current.branch != transaction.starting_branch or current.head != transaction.starting_head:
            raise TransactionError("branch or HEAD changed after validation")
        self._validate_untracked_policy()
        transaction.allowed_mutation_policy.validate(paths)
        current_diff_fingerprint = self._diff_fingerprint()
        self._assert_durable_mutation_boundary(paths, current_diff_fingerprint)
        if self.validated_diff_fingerprint is None or current_diff_fingerprint != self.validated_diff_fingerprint:
            raise TransactionError("repository diff changed after validation")
        if not paths:
            acceptance_reference = (
                (self.envelope.evidence or {}).get("accepted_feature_commit")
                if self.envelope is not None else None
            )
            read_only_acceptance = bool(
                transaction.workflow_type == WorkflowType.FEATURE_ACCEPTANCE
                and isinstance(acceptance_reference, str)
                and self.inspector.rev_parse(acceptance_reference, check=False) == transaction.starting_head
            )
            if transaction.workflow_type in {
                WorkflowType.FEATURE_EXECUTION,
                WorkflowType.MILESTONE_INTEGRATION,
            } or (
                transaction.workflow_type == WorkflowType.FEATURE_ACCEPTANCE
                and not read_only_acceptance
            ):
                raise TransactionError(
                    f"{transaction.workflow_type.value} cannot finalize without a nonempty diff"
                )
            self.final_commit = transaction.starting_head
            self.ledger.append(
                event_type="CommitFinalized",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "commit": transaction.starting_head,
                    "parent": None,
                    "changed_paths": [],
                    "diff_fingerprint": None,
                    "commit_subject": None,
                    "no_change": True,
                },
            )
            self.interrupt("after_commit", transaction)
            return self.final_commit
        subject = transaction.allowed_mutation_policy.commit_subject
        if not subject:
            raise TransactionError("writable transaction has no kernel commit subject")
        self._validate_mutation_paths(paths)
        self._run_git(["add", "--", *paths])
        staged = tuple(self.inspector.staged_changed_paths())
        if staged != paths:
            raise TransactionError("staged paths do not match the authorized mutation set")
        self._run_git(["commit", "-m", subject, "--", *paths])
        commit = self.inspector.head
        if commit == transaction.starting_head:
            raise TransactionError("kernel finalization did not create a commit")
        self.final_commit = commit
        # This boundary deliberately precedes durable commit evidence. Recovery
        # recognizes the exact one-child commit and never creates a duplicate.
        self.interrupt("after_commit", transaction)
        if transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION:
            if not self.prepared_integration_accepted_commit:
                raise TransactionError("integration finalization lacks accepted-commit identity")
            if self.inspector.rev_parse(f"{commit}^", check=False) != transaction.starting_head:
                raise TransactionError("integration commit is not a direct child of the milestone start")
            if self.inspector.patch_fingerprint(commit) != self.inspector.patch_fingerprint(
                self.prepared_integration_accepted_commit
            ):
                raise TransactionError("integration commit patch differs from accepted commit")
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": commit,
                "parent": transaction.starting_head,
                "changed_paths": list(paths),
                "diff_fingerprint": self.inspector.patch_fingerprint(commit),
                "commit_subject": subject,
                "accepted_commit": self.prepared_integration_accepted_commit,
                "integration_once": transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION,
            },
        )
        if not self.inspector.is_clean:
            raise TransactionError("repository is not clean after kernel finalization")
        return commit

    def finalize_feature_branch(self, branch: str) -> str:
        """Kernel-owned feature-preparation branch creation at the exact start."""

        transaction = self._require()
        if transaction.workflow_type != WorkflowType.FEATURE_PREPARATION:
            raise TransactionError("feature-branch finalization requires feature_preparation")
        if transaction.current_state != TransactionState.FINALIZING:
            raise TransactionError("feature-branch finalization requires passed validation")
        self._revalidate_lease()
        self.interrupt("before_commit", transaction)
        if self.inspector.current_branch != transaction.starting_branch or self.inspector.head != transaction.starting_head:
            raise TransactionError("feature branch preparation start changed")
        self.ledger.append(
            event_type="CheckpointRecorded", transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={"checkpoint": "feature_branch_target", "branch": branch},
        )
        if self.inspector.ref_exists(branch):
            if self.inspector.rev_parse(branch) != transaction.starting_head:
                raise TransactionError("existing feature branch does not match the transaction start")
            self._run_git(["switch", branch])
        else:
            self._run_git(["switch", "-c", branch, transaction.starting_head])
        self.final_commit = transaction.starting_head
        self.interrupt("after_commit", transaction)
        self.ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": transaction.starting_head,
                "parent": None,
                "changed_paths": [],
                "branch_created": branch,
                "no_change": True,
            },
        )
        return self.final_commit

    def complete(
        self, *, classification: str | None = None,
        evidence: dict[str, Any] | None = None,
        terminal_callback: Callable[[dict[str, Any]], None] | None = None,
        materialization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transaction = self._require()
        if transaction.current_state not in {TransactionState.FINALIZING, TransactionState.COMPLETED}:
            raise TransactionError("completion requires a finalized transaction")
        terminal = self.ledger.terminal_event(transaction.transaction_id)
        envelope = self.envelope
        terminal_classification = classification or (envelope.classification if envelope else "COMPLETED")
        next_state = (envelope.next_state if envelope else transaction.next_project_state) or "feature_ready"
        terminal_snapshot = capture_repository_snapshot(self.project).to_dict()
        terminal_snapshot["clean"] = not terminal_snapshot["tracked_changed_paths"] and not terminal_snapshot["untracked_paths"]
        reserved_terminal_fields = {
            "classification", "reference", "feature_id", "next_state", "terminal_snapshot",
        }
        forged = reserved_terminal_fields & set(evidence or {})
        if forged:
            raise TransactionError(
                "completion evidence attempts to override reserved terminal fields: "
                + ", ".join(sorted(forged))
            )
        transaction_events = [
            event for event in self.ledger.read()
            if event["transaction_id"] == transaction.transaction_id
        ]
        pending_materialization = next((
            event for event in transaction_events
            if event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "compatibility_materialization_pending"
        ), None)
        acknowledged_materialization = next((
            event for event in transaction_events
            if event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "compatibility_materialization_acknowledged"
        ), None)
        materialization_id = fingerprint(materialization) if materialization is not None else None
        if terminal_callback is not None and materialization is None:
            raise TransactionError("terminal callback requires a deterministic materialization descriptor")
        if pending_materialization is not None:
            expected_id = pending_materialization["payload"].get("materialization_id")
            if materialization_id is not None and materialization_id != expected_id:
                raise TransactionError("terminal materialization descriptor changed during replay")
            materialization_id = expected_id
        elif terminal_callback is not None:
            if terminal is not None:
                raise TransactionError("completed transaction lacks durable materialization intent")
            self.ledger.append(
                event_type="CheckpointRecorded",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "checkpoint": "compatibility_materialization_pending",
                    "materialization_id": materialization_id,
                    "descriptor": materialization,
                },
            )
            pending_materialization = True
        if pending_materialization is not None and acknowledged_materialization is None and terminal_callback is None:
            raise TransactionError("pending terminal materialization requires deterministic replay")
        payload = {
            "classification": terminal_classification,
            "reference": self.final_commit,
            "feature_id": transaction.feature_id,
            "next_state": next_state,
            "terminal_snapshot": terminal_snapshot,
            **(evidence or {}),
        }
        candidate_only = bool(
            transaction.workflow_type == WorkflowType.FEATURE_EXECUTION
            and (evidence or {}).get("candidate_implementation_commit")
        )
        if (
            transaction.workflow_type
            in {WorkflowType.FEATURE_EXECUTION, WorkflowType.FEATURE_ACCEPTANCE}
            and not candidate_only
        ):
            if not payload.get("accepted_feature_commit"):
                payload["accepted_feature_commit"] = self.final_commit
        if transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION:
            if not payload.get("integrated_commit"):
                payload["integrated_commit"] = self.final_commit
        terminal_appended = terminal is None
        if terminal is None:
            self._revalidate_terminal_repository()
            self.interrupt("before_terminal_event", transaction)
            self.ledger.append(
                event_type="TransactionCompleted",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload=payload,
            )
            transaction.terminate(TransactionState.COMPLETED, terminal_classification, self.final_commit)
            transaction.next_project_state = next_state
            self.interrupt("after_terminal_event", transaction)
        elif terminal["event_type"] != "TransactionCompleted":
            raise TransactionError("transaction already has a non-completed terminal outcome")
        if terminal_callback is not None and acknowledged_materialization is None:
            terminal_callback(self.projection.rebuild(persist_cache=False))
            self.ledger.append(
                event_type="CheckpointRecorded",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "checkpoint": "compatibility_materialization_acknowledged",
                    "materialization_id": materialization_id,
                },
            )
        self.interrupt("before_lease_release", transaction)
        if self.lease.read() is not None:
            self._revalidate_lease()
            self.lease.release(
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                repository_identity=transaction.repository_identity,
                project_id=transaction.project_id,
            )
            self.ledger.append(
                event_type="LeaseReleased",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={"lease_id": transaction.lease_identity},
            )
        self.interrupt("after_lease_release", transaction)
        self.interrupt("before_projection_update", transaction)
        has_projection_event = any(
            event["transaction_id"] == transaction.transaction_id
            and event["event_type"] == "ProjectionUpdated"
            for event in self.ledger.read()
        )
        if not has_projection_event:
            self.ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "current_state": next_state,
                    "current_feature": transaction.feature_id,
                    "selected_feature": (evidence or {}).get("selected_feature"),
                },
            )
        projection = self.projection.rebuild(persist_cache=True)
        return {"transaction": transaction.to_dict(), "projection": projection}

    def _revalidate_terminal_repository(self) -> None:
        """Prove the repository still represents this transaction's exact final result."""

        transaction = self._require()
        self._revalidate_lease()
        if any(self.inspector.git_operation_state().values()):
            raise TransactionError("Git operation appeared before transaction completion")
        if not self.inspector.is_clean:
            if (
                transaction.allowed_mutation_policy.require_clean_start
                or not self._observation_only_baseline_unchanged()
            ):
                raise TransactionError("repository is not clean before transaction completion")
        finalized = next((
            event for event in reversed(self.ledger.read())
            if event["transaction_id"] == transaction.transaction_id
            and event["event_type"] == "CommitFinalized"
        ), None)
        if finalized is None:
            raise TransactionError("completion requires durable commit-finalization evidence")
        payload = finalized["payload"]
        expected_head = payload.get("commit")
        expected_branch = transaction.starting_branch
        if transaction.workflow_type == WorkflowType.FEATURE_PREPARATION:
            expected_branch = payload.get("branch_created")
        if self.inspector.current_branch != expected_branch or self.inspector.head != expected_head:
            raise TransactionError("repository branch or HEAD changed before terminal evidence")
        changed_paths = tuple(payload.get("changed_paths") or ())
        if changed_paths and tuple(sorted(self.inspector.changed_paths(str(expected_head)))) != tuple(sorted(changed_paths)):
            raise TransactionError("final commit paths differ from durable finalization evidence")
        diff_fingerprint = payload.get("diff_fingerprint")
        if diff_fingerprint and self.inspector.patch_fingerprint(str(expected_head)) != diff_fingerprint:
            raise TransactionError("final commit diff differs from durable finalization evidence")
        if transaction.workflow_type == WorkflowType.MILESTONE_INTEGRATION:
            accepted_commit = payload.get("accepted_commit")
            if not accepted_commit or not changed_paths:
                raise TransactionError("integration terminal evidence lacks nonempty accepted diff")
            execution_mode = payload.get("integration_execution_mode")
            if execution_mode in {"controller_plan", "controller_plan_recovery"}:
                resulting = payload.get("resulting_feature_commit")
                if not isinstance(resulting, str):
                    raise TransactionError(
                        "controller-plan integration lacks its resulting feature commit"
                    )
                expected_feature_parent = transaction.starting_head
                if execution_mode == "controller_plan_recovery":
                    expected_feature_parent = payload.get("recovered_pre_integration_head")
                    integrating = payload.get("integrating_metadata_commit")
                    if (
                        not isinstance(expected_feature_parent, str)
                        or not isinstance(integrating, str)
                        or transaction.starting_head != integrating
                        or self.inspector.rev_parse(f"{integrating}^", check=False)
                        != resulting
                        or self.inspector.rev_parse(f"{expected_head}^", check=False)
                        != integrating
                    ):
                        raise TransactionError(
                            "controller-plan recovery chain differs from the reserved topology"
                        )
                if self.inspector.rev_parse(f"{resulting}^", check=False) != expected_feature_parent:
                    raise TransactionError(
                        "controller-plan feature commit is not a direct child of the milestone start"
                    )
                if self.inspector.patch_fingerprint(resulting) != self.inspector.patch_fingerprint(
                    str(accepted_commit)
                ):
                    raise TransactionError(
                        "controller-plan feature commit differs from the accepted patch"
                    )
                if not self.inspector.is_ancestor(resulting, str(expected_head)):
                    raise TransactionError(
                        "controller-plan final evidence does not descend from the feature commit"
                    )
            else:
                if self.inspector.rev_parse(f"{expected_head}^", check=False) != transaction.starting_head:
                    raise TransactionError("integration terminal commit is not a direct child")
                if self.inspector.patch_fingerprint(str(expected_head)) != self.inspector.patch_fingerprint(str(accepted_commit)):
                    raise TransactionError("integration terminal patch differs from accepted patch")

    def block(
        self,
        *,
        state: TransactionState,
        classification: str,
        next_state: str,
        reference: str | None = None,
        human_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transaction = self._require()
        if state not in {
            TransactionState.BLOCKED, TransactionState.HUMAN_DECISION_REQUIRED,
            TransactionState.RETRYABLE_FAILURE, TransactionState.TERMINAL_FAILURE,
            TransactionState.SUPERSEDED,
        }:
            raise TransactionError("block requires a non-completed terminal state")
        event_type = {
            TransactionState.HUMAN_DECISION_REQUIRED: "HumanGateRaised",
            TransactionState.SUPERSEDED: "TransactionSuperseded",
        }.get(state, "TransactionBlocked")
        self._revalidate_lease()
        validated_gate = None
        if state == TransactionState.HUMAN_DECISION_REQUIRED or classification == "SEMANTIC_CONFLICT":
            if not isinstance(human_gate, dict):
                raise TransactionError("human-decision terminal requires an exact gate object")
            validated_gate = bind_human_gate(
                human_gate,
                transaction_id=transaction.transaction_id,
                project_id=transaction.project_id,
                repository_identity=transaction.repository_identity,
                repository_path_fingerprint=transaction.repository_path_fingerprint,
                workflow_type=transaction.workflow_type,
                approved_next_state="queue_reconciliation",
                terminal_classification=classification,
            )
            if isinstance(human_gate, dict):
                human_gate.clear()
                human_gate.update(validated_gate)
            gate_id = str(validated_gate["gate_id"])
            raised = [
                event for event in self.ledger.read()
                if event["event_type"] == "HumanGateRaised"
                and (event["payload"].get("gate") or {}).get("gate_id") == gate_id
            ]
            if raised:
                raise TransactionError("human-decision gate_id already exists in durable evidence")
        payload = {
            "classification": classification,
            "terminal_state": state.value,
            "reference": reference,
            "next_state": next_state,
            "gate": validated_gate,
            "gate_id": validated_gate.get("gate_id") if validated_gate else None,
            "gate_fingerprint": fingerprint(validated_gate) if validated_gate else None,
            "terminal_snapshot": capture_repository_snapshot(self.project).to_dict(),
        }
        self.interrupt("before_terminal_event", transaction)
        self.ledger.append(
            event_type=event_type,
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload=payload,
        )
        transaction.terminate(state, classification, reference)
        transaction.next_project_state = next_state
        self.interrupt("after_terminal_event", transaction)
        self.interrupt("before_lease_release", transaction)
        if self.lease.read() is not None:
            self._revalidate_lease()
            self.lease.release(
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                repository_identity=transaction.repository_identity,
                project_id=transaction.project_id,
            )
            self.ledger.append(
                event_type="LeaseReleased",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={"lease_id": transaction.lease_identity},
            )
        self.interrupt("after_lease_release", transaction)
        self.interrupt("before_projection_update", transaction)
        self.ledger.append(
            event_type="ProjectionUpdated",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "current_state": next_state,
                "current_feature": transaction.feature_id,
                "selected_feature": None,
            },
        )
        return self.projection.rebuild(persist_cache=True)

    def _revalidate_lease(self) -> Any:
        transaction = self._require()
        return self.lease.revalidate(
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            repository_identity=transaction.repository_identity,
            project_id=transaction.project_id,
            lease_type=WORKFLOW_LEASE[transaction.workflow_type],
            repository_path_fingerprint=transaction.repository_path_fingerprint,
            repository_path=self.project.repository,
            lease_id=transaction.lease_identity,
            milestone=transaction.milestone,
            feature_id=transaction.feature_id,
            starting_branch=transaction.starting_branch,
            starting_head=transaction.starting_head,
            run_id=transaction.run_id,
            session_id=(transaction.session_ids[-1] if transaction.session_ids else None),
            policy=transaction.allowed_mutation_policy,
        )

    def _changed_paths(self) -> tuple[str, ...]:
        tracked = set(self.inspector.tracked_changed_paths())
        untracked = set(self.inspector.untracked_file_hashes())
        transaction = self._require()
        if not transaction.allowed_mutation_policy.require_clean_start:
            if self._observation_only_baseline_unchanged():
                # Recovery may observe an exact ledger-authorized dirty baseline,
                # but the recovery transaction owns none of those prior changes.
                return ()
            if not tracked and not untracked:
                raise TransactionError(
                    "observation-only dirty baseline changed after recovery start"
                )
        return tuple(sorted(tracked | untracked))

    def _observation_only_baseline_unchanged(self) -> bool:
        transaction = self._require()
        snapshot = capture_repository_snapshot(self.project)
        return bool(
            snapshot.repository_identity == transaction.repository_identity
            and snapshot.repository_path_fingerprint
            == transaction.repository_path_fingerprint
            and snapshot.branch == transaction.starting_branch
            and snapshot.head == transaction.starting_head
            and snapshot.queue_fingerprint == transaction.starting_queue_fingerprint
            and snapshot.tracked_diff_fingerprint
            == transaction.starting_tracked_diff_fingerprint
            and snapshot.untracked_fingerprint
            == transaction.starting_untracked_fingerprint
        )

    def _assert_durable_mutation_boundary(
        self, paths: tuple[str, ...], diff_fingerprint: str
    ) -> None:
        transaction = self._require()
        boundaries = [
            event["payload"] for event in self.ledger.read()
            if event["transaction_id"] == transaction.transaction_id
            and event["event_type"] == "ChangesDetected"
        ]
        if not boundaries:
            raise TransactionError("validation requires a durable mutation boundary")
        expected_paths = tuple(boundaries[0].get("changed_paths") or ())
        expected_fingerprint = boundaries[0].get("diff_fingerprint")
        if any(
            tuple(boundary.get("changed_paths") or ()) != expected_paths
            or boundary.get("diff_fingerprint") != expected_fingerprint
            for boundary in boundaries[1:]
        ):
            raise TransactionError("durable mutation boundary is contradictory")
        if paths != expected_paths or diff_fingerprint != expected_fingerprint:
            raise TransactionError(
                "repository diff does not match durable mutation boundary"
            )

    def _validate_untracked_policy(self) -> None:
        transaction = self._require()
        untracked = tuple(sorted(self.inspector.untracked_file_hashes()))
        if untracked and not transaction.allowed_mutation_policy.allow_untracked:
            raise TransactionError(
                "transaction policy does not allow untracked mutations: " + ", ".join(untracked)
            )

    def _diff_fingerprint(self) -> str:
        paths = self._changed_paths()
        self._validate_mutation_paths(paths)
        return self.inspector.content_diff_fingerprint(paths)

    def _validate_mutation_paths(self, paths: tuple[str, ...]) -> None:
        root = self.project.repository.resolve()
        for relative in paths:
            candidate = root / relative
            resolved_parent = candidate.parent.resolve()
            try:
                resolved_parent.relative_to(root)
            except ValueError as exc:
                raise TransactionError(f"mutation path escapes repository: {relative}") from exc
            if candidate.is_symlink():
                raise TransactionError(f"mutation path is a symbolic link: {relative}")
            if candidate.exists():
                metadata = os.lstat(candidate)
                if not stat.S_ISREG(metadata.st_mode):
                    raise TransactionError(f"mutation path is not a regular file: {relative}")
                if metadata.st_nlink != 1:
                    raise TransactionError(f"mutation path has an unsafe hard-link count: {relative}")

    def _run_git(self, arguments: list[str]) -> None:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.project.repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RepositoryError(result.stderr.strip() or result.stdout.strip() or "kernel Git finalization failed")


class PhaseAdapter:
    workflow_type: WorkflowType
    CLASSIFICATION_STATES: dict[str, TransactionState | None] = {}
    CLASSIFICATION_NEXT_STATES: dict[str, str | None] = {}
    HANDLED_FAILURE_CLASSIFICATION = "TERMINAL_FAILURE"
    HANDLED_FAILURE_NEXT_STATE = "validation_failed"

    def __init__(
        self, *, allowed_paths: Iterable[str], commit_subject: str, next_state: str,
        allowed_prefixes: Iterable[str] = (), denied_paths: Iterable[str] = (),
        denied_prefixes: Iterable[str] = (), allow_untracked: bool = False,
        require_clean_start: bool = True,
    ):
        self.policy = MutationPolicy(
            tuple(sorted(set(allowed_paths))),
            allowed_prefixes=tuple(sorted(set(allowed_prefixes))),
            denied_paths=tuple(sorted(set(denied_paths))),
            denied_prefixes=tuple(sorted(set(denied_prefixes))),
            allow_untracked=allow_untracked,
            require_clean_start=require_clean_start,
            commit_subject=commit_subject,
        )
        self.next_state = next_state

    def semantic_validate(self, envelope: SessionResultEnvelope, changed_paths: tuple[str, ...]) -> None:
        if envelope.classification not in self.CLASSIFICATION_STATES:
            raise TransactionError(
                f"{self.workflow_type.value} does not authorize classification {envelope.classification!r}"
            )
        expected = self.CLASSIFICATION_NEXT_STATES.get(envelope.classification)
        expected = self.next_state if expected is None else expected
        if envelope.next_state != expected:
            raise TransactionError(
                f"phase result next_state {envelope.next_state!r} does not match {expected!r}"
            )

    def terminal_state(self, envelope: SessionResultEnvelope) -> TransactionState | None:
        self.semantic_validate(envelope, envelope.changed_paths)
        return self.CLASSIFICATION_STATES[envelope.classification]


class QueueReconciliationAdapter(PhaseAdapter):
    workflow_type = WorkflowType.QUEUE_RECONCILIATION

    TERMINAL_NEXT_STATE = {
        "RECONCILED_READY_WORK": "feature_ready",
        "RECONCILED_NO_READY_WORK": "paused",
        "MILESTONE_COMPLETE": "milestone_gate",
        "HUMAN_DECISION_REQUIRED": "human_decision_required",
        "PLANNING_VALIDATION_FAILED": "validation_failed",
        "PLANNING_SEMANTIC_CONFLICT": "validation_failed",
        "RETRYABLE_PLANNING_FAILURE": "validation_failed",
        "TERMINAL_PLANNING_FAILURE": "validation_failed",
    }
    CLASSIFICATION_NEXT_STATES = TERMINAL_NEXT_STATE
    CLASSIFICATION_STATES = {
        "RECONCILED_READY_WORK": None, "RECONCILED_NO_READY_WORK": None,
        "MILESTONE_COMPLETE": None,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "PLANNING_VALIDATION_FAILED": TransactionState.TERMINAL_FAILURE,
        "PLANNING_SEMANTIC_CONFLICT": TransactionState.TERMINAL_FAILURE,
        "RETRYABLE_PLANNING_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_PLANNING_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    HANDLED_FAILURE_CLASSIFICATION = "PLANNING_VALIDATION_FAILED"

    def semantic_validate(self, envelope: SessionResultEnvelope, changed_paths: tuple[str, ...]) -> None:
        expected = self.TERMINAL_NEXT_STATE.get(envelope.classification)
        if expected is None or envelope.next_state != expected:
            raise TransactionError(
                "queue terminal classification does not authorize its projected next state"
            )


class FeaturePreparationAdapter(PhaseAdapter):
    workflow_type = WorkflowType.FEATURE_PREPARATION
    CLASSIFICATION_STATES = {
        "FEATURE_PREPARED": None, "FEATURE_VALIDATION_FAILED": TransactionState.TERMINAL_FAILURE,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "RETRYABLE_FEATURE_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_FEATURE_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "FEATURE_PREPARED": None, "FEATURE_VALIDATION_FAILED": "validation_failed",
        "HUMAN_DECISION_REQUIRED": "human_decision_required",
        "RETRYABLE_FEATURE_FAILURE": "validation_failed", "TERMINAL_FEATURE_FAILURE": "validation_failed",
    }
    HANDLED_FAILURE_CLASSIFICATION = "FEATURE_VALIDATION_FAILED"


class FeatureExecutionAdapter(PhaseAdapter):
    workflow_type = WorkflowType.FEATURE_EXECUTION
    CLASSIFICATION_STATES = {
        "FEATURE_ACCEPTED": None, "FEATURE_REJECTED": TransactionState.TERMINAL_FAILURE,
        "FEATURE_VALIDATION_FAILED": TransactionState.TERMINAL_FAILURE,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "RETRYABLE_FEATURE_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_FEATURE_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "FEATURE_ACCEPTED": None, "FEATURE_REJECTED": "validation_failed",
        "FEATURE_VALIDATION_FAILED": "validation_failed",
        "HUMAN_DECISION_REQUIRED": "human_decision_required",
        "RETRYABLE_FEATURE_FAILURE": "validation_failed", "TERMINAL_FEATURE_FAILURE": "validation_failed",
    }
    HANDLED_FAILURE_CLASSIFICATION = "FEATURE_VALIDATION_FAILED"


class FeatureAcceptanceAdapter(PhaseAdapter):
    workflow_type = WorkflowType.FEATURE_ACCEPTANCE
    CLASSIFICATION_STATES = FeatureExecutionAdapter.CLASSIFICATION_STATES
    CLASSIFICATION_NEXT_STATES = FeatureExecutionAdapter.CLASSIFICATION_NEXT_STATES
    HANDLED_FAILURE_CLASSIFICATION = "FEATURE_VALIDATION_FAILED"


class MilestoneIntegrationAdapter(PhaseAdapter):
    workflow_type = WorkflowType.MILESTONE_INTEGRATION
    CLASSIFICATION_STATES = {
        "INTEGRATED": None, "VALIDATION_FAILED": TransactionState.TERMINAL_FAILURE,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "SEMANTIC_CONFLICT": TransactionState.HUMAN_DECISION_REQUIRED,
        "RETRYABLE_INTEGRATION_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_INTEGRATION_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "INTEGRATED": None, "VALIDATION_FAILED": "validation_failed",
        "HUMAN_DECISION_REQUIRED": "human_decision_required", "SEMANTIC_CONFLICT": "human_decision_required",
        "RETRYABLE_INTEGRATION_FAILURE": "integration_ready",
        "TERMINAL_INTEGRATION_FAILURE": "validation_failed",
    }
    HANDLED_FAILURE_CLASSIFICATION = "VALIDATION_FAILED"


class MilestoneGateAdapter(PhaseAdapter):
    workflow_type = WorkflowType.MILESTONE_GATE
    CLASSIFICATION_STATES = {
        "MILESTONE_GATE_PASSED": None, "MILESTONE_GATE_FAILED": TransactionState.TERMINAL_FAILURE,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "RETRYABLE_GATE_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_GATE_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "MILESTONE_GATE_PASSED": None, "MILESTONE_GATE_FAILED": "validation_failed",
        "HUMAN_DECISION_REQUIRED": "human_decision_required", "RETRYABLE_GATE_FAILURE": "milestone_gate",
        "TERMINAL_GATE_FAILURE": "validation_failed",
    }
    HANDLED_FAILURE_CLASSIFICATION = "MILESTONE_GATE_FAILED"


class HumanDecisionResolutionAdapter(PhaseAdapter):
    workflow_type = WorkflowType.HUMAN_DECISION_RESOLUTION
    CLASSIFICATION_STATES = {
        "HUMAN_GATE_RESOLVED": None,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "TERMINAL_RESOLUTION_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "HUMAN_GATE_RESOLVED": None, "HUMAN_DECISION_REQUIRED": "human_decision_required",
        "TERMINAL_RESOLUTION_FAILURE": "human_decision_required",
    }
    HANDLED_FAILURE_CLASSIFICATION = "TERMINAL_RESOLUTION_FAILURE"
    HANDLED_FAILURE_NEXT_STATE = "human_decision_required"


class RecoveryAdapter(PhaseAdapter):
    workflow_type = WorkflowType.RECOVERY
    CLASSIFICATION_STATES = {
        "RECOVERY_APPLIED": None, "TRANSACTION_SUPERSEDED": TransactionState.SUPERSEDED,
        "HUMAN_DECISION_REQUIRED": TransactionState.HUMAN_DECISION_REQUIRED,
        "RETRYABLE_RECOVERY_FAILURE": TransactionState.RETRYABLE_FAILURE,
        "TERMINAL_RECOVERY_FAILURE": TransactionState.TERMINAL_FAILURE,
    }
    CLASSIFICATION_NEXT_STATES = {
        "RECOVERY_APPLIED": None, "TRANSACTION_SUPERSEDED": None,
        "HUMAN_DECISION_REQUIRED": "human_decision_required", "RETRYABLE_RECOVERY_FAILURE": None,
        "TERMINAL_RECOVERY_FAILURE": "human_decision_required",
    }
    HANDLED_FAILURE_CLASSIFICATION = "TERMINAL_RECOVERY_FAILURE"
    HANDLED_FAILURE_NEXT_STATE = "human_decision_required"
