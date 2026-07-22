"""Global deterministic consistency checker for one registered project."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import ConsistencyClassification, WorkflowType, fingerprint
from .errors import CorruptEvidenceError, ProjectionError, QueueError
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .locks import inspect_repository_writer_lock
from .migration import LegacyStateMigrator
from .projection import ProjectionEngine, cache_agrees, build_projection_observations
from .queue import FeatureQueue, resolve_feature_commit
from .registry import Project
from .repository import RepositoryInspector
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowLeaseRecord
from .errors import AmbiguousLockError
from .execution_plan import (
    ExecutionPlan,
    bind_projection_to_queue,
    execution_plan_projection_agreement,
    integrated_feature_execution_checks,
)
from .feature_branches import canonical_feature_branch


SEVERITY_ORDER = {
    ConsistencyClassification.CONSISTENT: 0,
    ConsistencyClassification.RECOVERABLE_INCONSISTENCY: 1,
    ConsistencyClassification.HUMAN_DECISION_REQUIRED: 2,
    ConsistencyClassification.UNSAFE_REPOSITORY_STATE: 3,
    ConsistencyClassification.CORRUPT_EVIDENCE: 4,
}


@dataclass(frozen=True)
class InvariantResult:
    invariant: str
    passed: bool
    classification: ConsistencyClassification
    evidence: dict[str, Any]
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "passed": self.passed,
            "classification": self.classification.value,
            "evidence": self.evidence,
            "diagnostic": self.diagnostic,
        }


class ConsistencyChecker:
    def __init__(
        self,
        *,
        controller_root: Path,
        project: Project,
        planner_observer: Callable[[], dict[str, Any]] | None = None,
    ):
        self.controller_root = controller_root.expanduser().resolve()
        self.project = project
        self.planner_observer = planner_observer
        self.inspector = RepositoryInspector(project.repository)
        identity = self.inspector.identity()
        project_root = self.controller_root / "state/projects" / project.project_id
        self.ledger = EvidenceLedger(
            project_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(self.ledger, project_root / "projection-cache.json")

    def check(self) -> dict[str, Any]:
        results: list[InvariantResult] = []

        def add(
            name: str,
            passed: bool,
            classification: ConsistencyClassification = ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
            *,
            evidence: dict[str, Any] | None = None,
            diagnostic: str | None = None,
        ) -> None:
            results.append(InvariantResult(
                invariant=name,
                passed=passed,
                classification=ConsistencyClassification.CONSISTENT if passed else classification,
                evidence=evidence or {},
                diagnostic=diagnostic,
            ))

        ledger_exists = self.ledger.path.exists()
        ledger_projection: dict[str, Any] | None = None
        cache_projection: dict[str, Any] | None = None
        integrity = None
        ledger_events: list[dict[str, Any]] = []
        if ledger_exists:
            try:
                integrity = self.ledger.verify()
                ledger_events = self.ledger.read()
                ledger_projection = self.projection.rebuild(persist_cache=False)
                cache_projection = ledger_projection
                add("ledger_integrity", True, evidence=integrity.to_dict())
            except CorruptEvidenceError as exc:
                add(
                    "ledger_integrity", False, ConsistencyClassification.CORRUPT_EVIDENCE,
                    evidence={"ledger_path": str(self.ledger.path)}, diagnostic=str(exc),
                )
        else:
            add(
                "ledger_integrity", False, ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
                evidence={"ledger_exists": False}, diagnostic="legacy state has not been imported",
            )

        identity = self.inspector.identity()
        add(
            "repository_identity",
            ledger_projection is None or ledger_projection.get("repository_identity") == identity["repository_id"],
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"repository_identity": identity["repository_id"]},
            diagnostic="ledger projection belongs to another repository",
        )
        add(
            "project_identity",
            ledger_projection is None or ledger_projection.get("project_id") == self.project.project_id,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"project_id": self.project.project_id},
            diagnostic="ledger projection belongs to another project",
        )

        try:
            queue = FeatureQueue.from_location(self.project.repository, self.project.queue_location)
            queue_summary = queue.summary(self.project.active_milestone or "")
            add("queue_validity", True, evidence=queue_summary)
        except (QueueError, OSError) as exc:
            queue = None
            add(
                "queue_validity", False, ConsistencyClassification.HUMAN_DECISION_REQUIRED,
                diagnostic=str(exc),
            )

        milestone = queue.milestone(self.project.active_milestone or "") if queue is not None else None
        add(
            "queue_project_milestone_agreement",
            queue is None or milestone is not None,
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={"configured_milestone": self.project.active_milestone, "queue_has_milestone": milestone is not None},
            diagnostic="configured active milestone is absent from the queue",
        )

        git_operations = self.inspector.git_operation_state()
        add(
            "git_operations",
            not any(git_operations.values()),
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence=git_operations,
            diagnostic="active Git operation requires explicit repository recovery",
        )
        add(
            "git_branch",
            self.inspector.current_branch is not None,
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence={"branch": self.inspector.current_branch},
            diagnostic="detached HEAD is not a writable workflow state",
        )
        add(
            "git_head",
            bool(self.inspector.head),
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence={"head": self.inspector.head},
        )

        writer = inspect_repository_writer_lock(
            self.inspector.writer_lock_path(), self.project.repository
        )
        active_transaction = (ledger_projection or {}).get("active_transaction")
        writer_record = writer.record or {}
        writer_transaction = writer_record.get("transaction_id")
        typed_lease: WorkflowLeaseRecord | None = None
        typed_lease_error: str | None = None
        if writer.exists:
            try:
                typed_lease = WorkflowLeaseRecord.from_dict(writer_record)
            except AmbiguousLockError as exc:
                typed_lease_error = str(exc)
        projected_transaction = next(
            (item for item in (ledger_projection or {}).get("transactions", []) if item.get("transaction_id") == active_transaction),
            None,
        )
        expected_start = next(
            (event for event in ledger_events if event["transaction_id"] == active_transaction and event["event_type"] == "TransactionStarted"),
            None,
        )
        lease_matches = (
            (not writer.exists and active_transaction is None)
            or (
                typed_lease is not None
                and active_transaction is not None
                and typed_lease.transaction_id == active_transaction
                and typed_lease.repository_identity == identity["repository_id"]
                and typed_lease.repository_path_fingerprint == identity["path_fingerprint"]
                and typed_lease.project_id == self.project.project_id
                and typed_lease.workflow_type.value == (projected_transaction or {}).get("workflow_type")
                and typed_lease.starting_branch == (expected_start or {}).get("payload", {}).get("starting_branch")
                and typed_lease.starting_head == (expected_start or {}).get("payload", {}).get("starting_head")
            )
        )
        add(
            "lease_ownership",
            lease_matches,
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={
                "exists": writer.exists,
                "active_transaction": active_transaction,
                "lease_transaction": writer_transaction,
                "process_alive": writer.process_alive,
                "ambiguous": writer.ambiguous,
                "typed_contract": typed_lease is not None,
            },
            diagnostic=typed_lease_error or "writer lease is not bound to the exact projected transaction and phase",
        )

        if ledger_projection is not None:
            projected_feature = ledger_projection.get("current_feature")
            queue_feature = queue.feature(str(projected_feature)) if queue is not None and projected_feature else None
            observations = build_projection_observations(
                branch=self.inspector.current_branch, head=self.inspector.head,
                clean=self.inspector.is_clean, git_operations=git_operations,
                queue_feature=(queue_feature or {}).get("id"),
                queue_integration_status=(queue_feature or {}).get("integration_status"),
                lease_transaction=writer_transaction,
                lease_valid=lease_matches,
                live_session_id=writer_record.get("session_id"),
            )
            ledger_projection = self.projection.rebuild(
                persist_cache=False, observations=observations,
            )
            active_transaction = ledger_projection.get("active_transaction")
            if queue is not None:
                ledger_projection = bind_projection_to_queue(
                    ledger_projection,
                    queue,
                    str(self.project.active_milestone or ""),
                )
                cache_projection = ledger_projection

        active_transactions = [
            item for item in (ledger_projection or {}).get("transactions", [])
            if item.get("state") not in {
                "completed", "blocked", "human_decision_required", "retryable_failure",
                "terminal_failure", "superseded",
            }
        ]
        add(
            "active_transaction_lifecycle",
            len(active_transactions) <= 1 and (
                (not active_transactions and active_transaction is None)
                or (len(active_transactions) == 1 and active_transactions[0].get("transaction_id") == active_transaction)
            ),
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"active_transaction": active_transaction, "active_transaction_ids": [item.get("transaction_id") for item in active_transactions]},
            diagnostic="projection contains multiple or incoherent active transactions",
        )

        dirty = not self.inspector.is_clean
        recoverable_dirty = bool(active_transaction and (ledger_projection or {}).get("session_resume_eligible"))
        add(
            "worktree_status",
            not dirty or recoverable_dirty,
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence={
                "clean": not dirty,
                "dirty_entries": len(self.inspector.dirty_entries),
                "exact_recorded_transaction": recoverable_dirty,
            },
            diagnostic="dirty worktree is not explained by an active transaction",
        )

        # An active transaction is recoverable only when its exact repository
        # snapshot or its recorded mutation boundary matches the live worktree.
        active_events = [event for event in ledger_events if event["transaction_id"] == active_transaction]
        start_snapshot = next(
            (event.get("payload", {}).get("snapshot") for event in active_events if event["event_type"] == "SnapshotCaptured"),
            None,
        )
        mutation_event = next(
            (event for event in reversed(active_events) if event["event_type"] == "ChangesDetected"),
            None,
        )
        live_snapshot = capture_repository_snapshot(self.project) if active_transaction is not None else None
        mutation_exact = True
        mutation_evidence: dict[str, Any] = {"active_transaction": active_transaction}
        if live_snapshot is not None:
            live_paths = tuple(sorted((*live_snapshot.tracked_changed_paths, *live_snapshot.untracked_paths)))
            expected_paths = tuple((mutation_event or {}).get("payload", {}).get("changed_paths", []))
            if mutation_event is not None:
                mutation_exact = live_paths == expected_paths
                mutation_evidence.update({"recorded_paths": list(expected_paths), "observed_paths": list(live_paths)})
            elif isinstance(start_snapshot, dict):
                mutation_exact = (
                    live_snapshot.branch == start_snapshot.get("branch")
                    and live_snapshot.head == start_snapshot.get("head")
                    and live_snapshot.tracked_diff_fingerprint == start_snapshot.get("tracked_diff_fingerprint")
                    and live_snapshot.untracked_fingerprint == start_snapshot.get("untracked_fingerprint")
                )
                mutation_evidence.update({
                    "starting_branch": start_snapshot.get("branch"),
                    "observed_branch": live_snapshot.branch,
                    "starting_head": start_snapshot.get("head"),
                    "observed_head": live_snapshot.head,
                })
        add(
            "active_transaction_repository_snapshot",
            mutation_exact,
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence=mutation_evidence,
            diagnostic="live repository does not match the active transaction's exact recorded boundary",
        )

        finalization = next(
            (event for event in reversed(active_events) if event["event_type"] == "CommitFinalized"),
            None,
        )
        expected_branch_head = True
        expected_branch = (start_snapshot or {}).get("branch") if isinstance(start_snapshot, dict) else None
        expected_head = (start_snapshot or {}).get("head") if isinstance(start_snapshot, dict) else None
        if live_snapshot is not None and finalization is not None:
            expected_head = finalization["payload"].get("commit")
            expected_branch = finalization["payload"].get("milestone_branch", expected_branch)
        if live_snapshot is not None and expected_branch and expected_head:
            expected_branch_head = live_snapshot.branch == expected_branch and live_snapshot.head == expected_head
        add(
            "active_transaction_branch_head",
            expected_branch_head,
            ConsistencyClassification.UNSAFE_REPOSITORY_STATE,
            evidence={
                "expected_branch": expected_branch,
                "observed_branch": self.inspector.current_branch,
                "expected_head": expected_head,
                "observed_head": self.inspector.head,
            },
            diagnostic="active transaction branch or HEAD differs from its recorded phase boundary",
        )

        migration = LegacyStateMigrator(controller_root=self.controller_root, project=self.project)
        try:
            legacy_projection, reconstructed, superseded = migration.derive_projection()
            classifications = migration.classify_sources(migration.discover())
            source_blockers = [
                item for item in classifications if item["classification"] in {"contradictory", "corrupt"}
            ]
            add(
                "legacy_evidence_identity",
                not source_blockers,
                ConsistencyClassification.CORRUPT_EVIDENCE,
                evidence={
                    "source_count": len(classifications),
                    "classifications": [
                        {"source_id": item["source_id"], "classification": item["classification"]}
                        for item in classifications
                    ],
                },
                diagnostic="legacy evidence contains contradictory or corrupt identity",
            )
        except Exception as exc:
            legacy_projection = None
            reconstructed = []
            superseded = []
            add(
                "legacy_evidence_identity", False, ConsistencyClassification.HUMAN_DECISION_REQUIRED,
                diagnostic=str(exc),
            )

        effective_projection = ledger_projection or legacy_projection or {}
        transactions = effective_projection.get("transactions") or []
        terminal_conflicts = [
            item for item in transactions
            if isinstance(item, dict) and isinstance(item.get("terminal_classification"), list)
            and len(item["terminal_classification"]) > 1
        ]
        add(
            "transaction_state",
            not terminal_conflicts,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"active_transaction": effective_projection.get("active_transaction")},
            diagnostic="transaction has conflicting terminal outcomes",
        )

        session_identity_failures: list[dict[str, Any]] = []
        starts = {
            event["transaction_id"]: event for event in ledger_events if event["event_type"] == "TransactionStarted"
        }
        launches = {
            (event["transaction_id"], event["payload"].get("session_id"))
            for event in ledger_events if event["event_type"] == "SessionLaunched"
        }
        for event in ledger_events:
            if event["event_type"] != "SessionResultAccepted":
                continue
            envelope = event["payload"].get("envelope") or {}
            start = starts.get(event["transaction_id"], {}).get("payload", {})
            checks = {
                "transaction_id": envelope.get("transaction_id") == event["transaction_id"],
                "project_id": envelope.get("project_id") == self.project.project_id,
                "repository_identity": envelope.get("repository_identity") == identity["repository_id"],
                "workflow_type": envelope.get("workflow_type") == event["workflow_type"],
                "run_id": envelope.get("run_id") == start.get("run_id"),
                "starting_branch": envelope.get("starting_branch") == start.get("starting_branch"),
                "starting_commit": envelope.get("starting_commit") == start.get("starting_head"),
                "session_launch": (event["transaction_id"], envelope.get("session_id")) in launches,
            }
            if not all(checks.values()):
                session_identity_failures.append({
                    "transaction_id": event["transaction_id"],
                    "failed_fields": sorted(key for key, passed in checks.items() if not passed),
                })
        add(
            "session_report_identity",
            not session_identity_failures,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"failures": session_identity_failures},
            diagnostic="accepted session result does not exactly bind to its transaction and launch",
        )

        accepted = effective_projection.get("accepted_feature_commit")
        accepted_exists = accepted is None or self.inspector.rev_parse(str(accepted), check=False) is not None
        add(
            "commit_ancestry",
            accepted_exists,
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={"accepted_feature_commit": accepted, "commit_exists": accepted_exists},
            diagnostic="projected accepted feature commit does not exist",
        )

        accepted_by_feature: dict[str, set[str]] = {}
        integrations_by_feature: dict[str, list[dict[str, Any]]] = {}
        for transaction in effective_projection.get("transactions", []):
            transaction_id = transaction.get("transaction_id")
            feature_id = transaction.get("feature_id")
            if not feature_id:
                continue
            tx_events = [event for event in ledger_events if event["transaction_id"] == transaction_id]
            for event in tx_events:
                payload = event["payload"]
                candidate = None
                if event["event_type"] == "CommitFinalized" and event["workflow_type"] in {"feature_execution", "feature_acceptance"}:
                    candidate = payload.get("commit")
                if event["event_type"] == "TransactionCompleted":
                    candidate = payload.get("accepted_feature_commit") or candidate
                    if event["workflow_type"] == "milestone_integration":
                        integrations_by_feature.setdefault(feature_id, []).append(payload)
                if candidate:
                    accepted_by_feature.setdefault(feature_id, set()).add(candidate)
        immutable_acceptance = all(len(commits) <= 1 for commits in accepted_by_feature.values())
        add(
            "accepted_commit_immutability",
            immutable_acceptance,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"commit_counts": {feature: len(commits) for feature, commits in sorted(accepted_by_feature.items())}},
            diagnostic="one feature cycle records more than one accepted commit",
        )
        duplicate_integrations = {feature: values for feature, values in integrations_by_feature.items() if len(values) > 1}
        add(
            "integration_at_most_once",
            not duplicate_integrations,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={"integration_counts": {feature: len(values) for feature, values in sorted(integrations_by_feature.items())}},
            diagnostic="feature has more than one completed integration transaction",
        )
        integration_ancestry_failures: list[dict[str, Any]] = []
        milestone_head = self.inspector.rev_parse(self.project.milestone_branch or "", check=False) if self.project.milestone_branch else None
        for feature_id, values in integrations_by_feature.items():
            payload = values[0]
            integrated_commit = payload.get("integrated_commit")
            accepted_commit = payload.get("accepted_feature_commit") or next(iter(accepted_by_feature.get(feature_id, [])), None)
            exists = bool(integrated_commit and self.inspector.rev_parse(str(integrated_commit), check=False))
            ancestor = bool(exists and milestone_head and self.inspector.is_ancestor(str(integrated_commit), milestone_head))
            patch_equal = True
            if accepted_commit and integrated_commit and accepted_commit != integrated_commit:
                patch_equal = self.inspector.patch_fingerprint(str(accepted_commit)) == self.inspector.patch_fingerprint(str(integrated_commit))
            if not exists or not ancestor or not patch_equal:
                integration_ancestry_failures.append({
                    "feature_id": feature_id,
                    "integrated_commit_exists": exists,
                    "integrated_commit_on_milestone": ancestor,
                    "accepted_patch_matches": patch_equal,
                })
        add(
            "integration_commit_ancestry",
            not integration_ancestry_failures,
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={"failures": integration_ancestry_failures, "milestone_head": milestone_head},
            diagnostic="integrated commit is missing, off milestone history, or differs from the accepted patch",
        )
        add(
            "feature_status",
            bool(effective_projection.get("current_state")),
            evidence={
                "current_feature": effective_projection.get("current_feature"),
                "current_state": effective_projection.get("current_state"),
            },
        )
        projected_feature = effective_projection.get("current_feature")
        queue_feature = queue.feature(projected_feature) if queue is not None and projected_feature else None
        resolved_milestone_id = milestone.get("id") if milestone is not None else None
        add(
            "queue_feature_agreement",
            projected_feature is None or (
                queue_feature is not None
                and queue_feature.get("milestone") == resolved_milestone_id
            ),
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={
                "projected_feature": projected_feature,
                "queue_feature_status": (queue_feature or {}).get("status"),
                "queue_feature_milestone": (queue_feature or {}).get("milestone"),
                "configured_milestone": self.project.active_milestone,
                "resolved_queue_milestone": resolved_milestone_id,
            },
            diagnostic="projected feature is absent from the configured milestone queue",
        )
        add(
            "integration_status",
            not (
                effective_projection.get("current_state") == "integration_ready"
                and not effective_projection.get("accepted_feature_commit")
            ),
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={
                "status": effective_projection.get("integration_status"),
                "historical_outcomes": effective_projection.get("historical_integration_outcomes", []),
            },
            diagnostic="integration-ready projection lacks an accepted commit",
        )
        add(
            "planning_status",
            effective_projection.get("planning_status") != "failed",
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={"planning_status": effective_projection.get("planning_status")},
        )
        add(
            "human_gate_status",
            not (
                effective_projection.get("current_state") == "human_decision_required"
                and not effective_projection.get("human_gate")
            ),
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence={"human_gate": effective_projection.get("human_gate")},
            diagnostic="human-decision state lacks an exact active gate",
        )
        raised_gates: dict[str, dict[str, Any]] = {}
        duplicate_gate_ids: list[str] = []
        resolution_bindings: dict[str, tuple[str, str]] = {}
        resolved_gate_ids: set[str] = set()
        gate_resolution_failures: list[dict[str, Any]] = []
        for event in ledger_events:
            payload = event["payload"]
            if (
                event["event_type"] == "TransactionStarted"
                and event["workflow_type"] == WorkflowType.HUMAN_DECISION_RESOLUTION.value
                and isinstance(payload.get("resolved_gate_id"), str)
                and isinstance(payload.get("resolved_gate_fingerprint"), str)
            ):
                resolution_bindings[event["transaction_id"]] = (
                    payload["resolved_gate_id"], payload["resolved_gate_fingerprint"]
                )
            elif event["event_type"] == "HumanGateRaised":
                gate = payload.get("gate") or {}
                gate_id = gate.get("gate_id") or payload.get("gate_id")
                if gate_id:
                    if str(gate_id) in raised_gates:
                        duplicate_gate_ids.append(str(gate_id))
                    raised_gates[str(gate_id)] = gate
            elif event["event_type"] == "HumanGateResolved":
                gate_id = payload.get("gate_id")
                gate_fingerprint_value = payload.get("gate_fingerprint")
                raised = raised_gates.get(str(gate_id))
                bound = resolution_bindings.get(event["transaction_id"])
                if (
                    not gate_id
                    or str(gate_id) in resolved_gate_ids
                    or (
                        raised is not None
                        and fingerprint(raised) != gate_fingerprint_value
                    )
                    or (
                        raised is None
                        and bound != (str(gate_id), gate_fingerprint_value)
                    )
                ):
                    gate_resolution_failures.append({"transaction_id": event["transaction_id"], "gate_id": gate_id})
                resolved_gate_ids.add(str(gate_id))
        add(
            "human_gate_identity",
            not gate_resolution_failures and not duplicate_gate_ids,
            ConsistencyClassification.CORRUPT_EVIDENCE,
            evidence={
                "failures": gate_resolution_failures,
                "duplicate_gate_ids": duplicate_gate_ids,
            },
            diagnostic="human resolution is not bound to an exact previously raised gate",
        )

        runtime_path = self.project.repository / ".factory/runtime/milestone-integration/latest.json"
        runtime_failure: str | None = None
        runtime_evidence: dict[str, Any] = {"path": str(runtime_path), "exists": runtime_path.exists()}
        if runtime_path.exists():
            try:
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                if not isinstance(runtime, dict):
                    raise ValueError("runtime evidence is not an object")
                runtime_evidence.update({
                    "project_id": runtime.get("project_id"),
                    "feature_id": runtime.get("feature_id") or runtime.get("current_feature"),
                    "transaction_id": runtime.get("transaction_id"),
                })
                if runtime.get("project_id") not in {None, self.project.project_id}:
                    runtime_failure = "runtime evidence project identity mismatch"
                runtime_repository = runtime.get("repository_identity")
                if runtime_repository not in {None, identity["repository_id"]}:
                    runtime_failure = "runtime evidence repository identity mismatch"
                runtime_transaction = runtime.get("transaction_id")
                known_transactions = {event["transaction_id"] for event in ledger_events}
                if runtime_transaction is not None and ledger_exists and runtime_transaction not in known_transactions:
                    runtime_failure = "runtime evidence transaction identity is unknown"
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                runtime_failure = str(exc)
        add(
            "runtime_evidence_identity",
            runtime_failure is None,
            ConsistencyClassification.HUMAN_DECISION_REQUIRED,
            evidence=runtime_evidence,
            diagnostic=runtime_failure,
        )

        cycle_path = self.inspector.cycle_state_path()
        cycle_binding: dict[str, Any] = {"path": str(cycle_path), "exists": cycle_path.exists()}
        cycle_binding_failure: str | None = None
        cycle_binding_classification = ConsistencyClassification.RECOVERABLE_INCONSISTENCY
        if cycle_path.exists() and integrity is not None:
            try:
                cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
                keys = {
                    "kernel_transaction_id", "kernel_ledger_sequence", "kernel_ledger_fingerprint",
                    "kernel_projection_fingerprint", "kernel_cache_fingerprint",
                }
                present = keys & set(cycle)
                if present and present != keys:
                    raise ValueError("cycle cache has a partial kernel binding")
                if present:
                    unsigned = dict(cycle)
                    claimed_cache = unsigned.pop("kernel_cache_fingerprint")
                    if claimed_cache != fingerprint(unsigned):
                        cycle_binding_failure = "cycle cache fingerprint is invalid"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif cycle["kernel_ledger_sequence"] > integrity.sequence:
                        cycle_binding_failure = "cycle cache claims a ledger sequence from the future"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif cycle["kernel_ledger_sequence"] < integrity.sequence:
                        cycle_binding_failure = "cycle cache is older than the ledger"
                    elif cycle["kernel_ledger_fingerprint"] != integrity.fingerprint:
                        cycle_binding_failure = "cycle cache contradicts the ledger fingerprint"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif ledger_projection is None or cycle["kernel_projection_fingerprint"] != ledger_projection.get("projection_fingerprint"):
                        cycle_binding_failure = "cycle cache contradicts the projection fingerprint"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif (
                        cycle.get("current_phase") != ledger_projection.get("current_state")
                        or cycle.get("current_feature") not in {
                            ledger_projection.get("selected_next_feature"), ledger_projection.get("current_feature"),
                        }
                    ):
                        cycle_binding_failure = "cycle cache semantic state disagrees with the projection"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif cycle["kernel_transaction_id"] not in {event["transaction_id"] for event in ledger_events}:
                        cycle_binding_failure = "cycle cache names an unknown kernel transaction"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    elif not any(
                        event["transaction_id"] == cycle["kernel_transaction_id"]
                        and event["event_type"] in TERMINAL_EVENT_TYPES
                        and event["sequence"] <= cycle["kernel_ledger_sequence"]
                        for event in ledger_events
                    ):
                        cycle_binding_failure = "cycle cache is bound to a nonterminal kernel transaction"
                        cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
                    cycle_binding.update({key: cycle.get(key) for key in sorted(keys)})
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                cycle_binding_failure = str(exc)
                cycle_binding_classification = ConsistencyClassification.CORRUPT_EVIDENCE
        add(
            "cycle_cache_binding", cycle_binding_failure is None,
            cycle_binding_classification, evidence=cycle_binding,
            diagnostic=cycle_binding_failure,
        )

        cache_path = self.projection.cache_path
        if ledger_exists and cache_path is not None and cache_path.exists() and ledger_projection is not None:
            try:
                cache = self.projection.load_cache()
                agreement = bool(
                    cache is not None
                    and cache.get("ledger_sequence")
                    == ledger_projection.get("ledger_sequence")
                    and cache.get("ledger_fingerprint")
                    == ledger_projection.get("ledger_fingerprint")
                )
                add(
                    "projection_cache_agreement", agreement,
                    ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
                    evidence={
                        "cache_sequence": (cache or {}).get("ledger_sequence"),
                        "ledger_sequence": ledger_projection.get("ledger_sequence"),
                        "semantic_cache_match": bool(
                            cache is not None
                            and cache_projection is not None
                            and cache_agrees(cache, cache_projection)
                        ),
                    },
                    diagnostic="projection cache is stale",
                )
            except ProjectionError as exc:
                add(
                    "projection_cache_agreement", False, ConsistencyClassification.CORRUPT_EVIDENCE,
                    evidence={"cache_path": str(cache_path)}, diagnostic=str(exc),
                )
        else:
            add(
                "projection_cache_agreement", not ledger_exists,
                ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
                evidence={"cache_exists": bool(cache_path and cache_path.exists())},
                diagnostic="projection cache has not been built",
            )

        routing_projection = cache_projection or ledger_projection
        if routing_projection is not None:
            projected_feature = (
                routing_projection.get("current_feature")
                or routing_projection.get("selected_next_feature")
            )
            queue_feature = (
                queue.feature(str(projected_feature))
                if queue is not None and projected_feature else None
            )
            canonical_queue_branch = (
                canonical_feature_branch(self.project, queue_feature)
                if isinstance(queue_feature, dict) else None
            )
            executable = ExecutionPlan.from_projection(
                routing_projection,
                starting_commit=self.inspector.rev_parse(
                    self.project.milestone_branch or "", check=False
                ),
                feature_branch=canonical_queue_branch,
                milestone_branch=self.project.milestone_branch,
            )
            terminal_plan_checks = (
                integrated_feature_execution_checks(
                    routing_projection,
                    executable,
                    queue,
                    str(self.project.active_milestone or ""),
                )
                if queue is not None
                else {}
            )
            compatibility_path = (
                self.controller_root / "state/projects" / f"{self.project.project_id}.json"
            )
            compatibility_state = None
            compatibility_projection_fingerprint = None
            if compatibility_path.is_file():
                try:
                    compatibility_value = json.loads(compatibility_path.read_text(encoding="utf-8"))
                    if isinstance(compatibility_value, dict):
                        compatibility_state = compatibility_value.get("current_state")
                        compatibility_projection_fingerprint = (
                            (compatibility_value.get("state_evidence") or {}).get(
                                "projection_fingerprint"
                            )
                        )
                except (OSError, json.JSONDecodeError):
                    compatibility_state = None
            if self.planner_observer is not None:
                observation_source = "planner_observer"
                try:
                    status = self.planner_observer()
                except (CorruptEvidenceError, OSError, ProjectionError, QueueError):
                    agreement = False
                    agreement_evidence = {
                        "checks": {"planner_observation_succeeded": False},
                        "observer_error_category": "authoritative_planner_unavailable",
                    }
                else:
                    observed_projection = status.get("kernel_projection")
                    if not isinstance(observed_projection, dict):
                        observed_projection = routing_projection
                    if queue is not None:
                        raw_plan = status.get("executable_plan") or {}
                        summary = queue.summary(str(self.project.active_milestone or ""))
                        integrated_ids = {
                            str(item.get("id"))
                            for item in queue.features_for_milestone(
                                str(self.project.active_milestone or "")
                            )
                            if item.get("status") == "integrated"
                            and item.get("integration_status") == "passed"
                        }
                        historical_commits = {
                            item.get("accepted_commit")
                            for item in observed_projection.get(
                                "historical_integration_outcomes", []
                            )
                            if isinstance(item, dict)
                        }
                        raw_feature_workflow = raw_plan.get("workflow_type") in {
                            WorkflowType.FEATURE_EXECUTION.value,
                            WorkflowType.MILESTONE_INTEGRATION.value,
                        }
                        observed_selected = observed_projection.get(
                            "current_feature"
                        ) or observed_projection.get("selected_next_feature")
                        terminal_plan_checks.update(
                            {
                                "observed_integrated_feature_not_executable": not (
                                    raw_feature_workflow
                                    and raw_plan.get("feature_id") in integrated_ids
                                ),
                                "observed_empty_ready_queue_has_no_feature_plan": not (
                                    not summary["ready_features"]
                                    and raw_plan.get("workflow_type")
                                    == WorkflowType.FEATURE_EXECUTION.value
                                ),
                                "observed_selection_matches_queue": (
                                    observed_projection.get("allowed_next_action")
                                    != "feature_cycle"
                                    or observed_selected == summary["selected_feature"]
                                ),
                                "observed_historical_commit_not_reused": not (
                                    raw_feature_workflow
                                    and raw_plan.get("accepted_commit")
                                    in historical_commits
                                ),
                            }
                        )
                    projection_binding = {
                        "project_id": observed_projection.get("project_id")
                        == routing_projection.get("project_id"),
                        "ledger_sequence": observed_projection.get("ledger_sequence")
                        == routing_projection.get("ledger_sequence"),
                        "ledger_fingerprint": observed_projection.get("ledger_fingerprint")
                        == routing_projection.get("ledger_fingerprint"),
                    }
                    try:
                        observed_executable = ExecutionPlan.from_projection(
                            observed_projection,
                            starting_commit=status.get("feature_starting_commit"),
                            feature_branch=status.get("feature_branch"),
                            milestone_branch=status.get("milestone_branch"),
                        )
                    except ProjectionError:
                        observed_executable = executable
                        projection_binding["valid_projection_fingerprint"] = False
                    else:
                        projection_binding["valid_projection_fingerprint"] = True
                    agreement, agreement_evidence = execution_plan_projection_agreement(
                        observed_projection, status, observed_executable
                    )
                    agreement_evidence["checks"].update(projection_binding)
                    agreement = agreement and all(projection_binding.values())
                    cache_stale = (
                        compatibility_state != observed_projection.get("current_state")
                        or compatibility_projection_fingerprint
                        != observed_projection.get("projection_fingerprint")
                    )
                    reported_cache = status.get("compatibility_cache") or {}
                    legacy_observations = status.get("legacy_observations") or {}
                    stale_but_ignored = (
                        reported_cache.get("observed_state") == compatibility_state
                        and reported_cache.get("observed_projection_fingerprint")
                        == compatibility_projection_fingerprint
                        and reported_cache.get("stale") == cache_stale
                        and legacy_observations.get("persisted_compatibility_state")
                        == compatibility_state
                        and status.get("persisted_state")
                        == observed_projection.get("current_state")
                    )
                    agreement_evidence["checks"][
                        "compatibility_cache_stale_but_ignored"
                    ] = stale_but_ignored
                    agreement = agreement and stale_but_ignored
                    agreement_evidence["compatibility_cache"] = {
                        "actual_state": compatibility_state,
                        "actual_projection_fingerprint": compatibility_projection_fingerprint,
                        "stale": cache_stale,
                        "ignored_for_execution": stale_but_ignored,
                    }
                    live_checks: dict[str, bool] = {}
                    if observed_executable.application_mutation_expected:
                        live_checks["planned_milestone_ref"] = bool(
                            observed_executable.milestone_branch
                            and observed_executable.starting_commit
                            and self.inspector.rev_parse(
                                observed_executable.milestone_branch, check=False
                            ) == observed_executable.starting_commit
                        )
                    if (
                        observed_executable.application_mutation_expected
                        and observed_executable.feature_id is not None
                        and queue is not None
                    ):
                        planned_feature = queue.feature(observed_executable.feature_id)
                        recovery = observed_projection.get("failed_integration_recovery")
                        recovery_checks = (
                            recovery.get("checks")
                            if isinstance(recovery, dict) else None
                        )
                        recovery_snapshot_checks = (
                            recovery.get("snapshot_checks")
                            if isinstance(recovery, dict) else None
                        )
                        verified_fresh_recovery = bool(
                            isinstance(recovery, dict)
                            and recovery.get("classification")
                            in {
                                "fresh_after_terminal_pre_mutation_failure",
                                "fresh_after_terminal_pre_mutation_gate",
                            }
                            and recovery.get("fresh_transaction") is True
                            and recovery.get("old_session_resume") is False
                            and isinstance(recovery_checks, dict)
                            and recovery_checks
                            and all(recovery_checks.values())
                            and isinstance(recovery_snapshot_checks, dict)
                            and recovery_snapshot_checks
                            and all(recovery_snapshot_checks.values())
                        )
                        recovered_branch_binding = bool(
                            verified_fresh_recovery
                            and observed_executable.feature_branch
                            and observed_executable.accepted_commit
                            and self.inspector.rev_parse(
                                observed_executable.feature_branch, check=False
                            ) == observed_executable.accepted_commit
                            and [
                                branch for branch in self.inspector.local_branches()
                                if branch != observed_executable.milestone_branch
                                and self.inspector.rev_parse(branch, check=False)
                                == observed_executable.accepted_commit
                            ] == [observed_executable.feature_branch]
                        )
                        canonical_planned_branch = (
                            canonical_feature_branch(self.project, planned_feature)
                            if isinstance(planned_feature, dict) else None
                        )
                        fresh_absent_branch = bool(
                            observed_executable.workflow_type
                            == WorkflowType.FEATURE_EXECUTION.value
                            and observed_executable.transaction_mode == "fresh"
                            and canonical_planned_branch == observed_executable.feature_branch
                            and canonical_planned_branch
                            and not self.inspector.ref_exists(canonical_planned_branch)
                            and self.inspector.current_branch == observed_executable.milestone_branch
                            and self.inspector.head == observed_executable.starting_commit
                        )
                        live_checks["planned_feature_branch"] = bool(
                            canonical_planned_branch == observed_executable.feature_branch
                            and (fresh_absent_branch or recovered_branch_binding or bool(
                                canonical_planned_branch
                                and self.inspector.ref_exists(canonical_planned_branch)
                            ))
                        )
                        live_checks["planned_feature_branch_state"] = fresh_absent_branch or bool(
                            canonical_planned_branch and self.inspector.ref_exists(canonical_planned_branch)
                        )
                        agreement_evidence["planned_feature_branch_state"] = (
                            "planned_branch_absent_and_ready_for_creation"
                            if fresh_absent_branch else "planned_branch_present"
                        )
                        if (
                            observed_executable.workflow_type
                            == WorkflowType.MILESTONE_INTEGRATION.value
                        ):
                            projected_accepted = observed_projection.get(
                                "accepted_feature_commit"
                            )
                            if verified_fresh_recovery:
                                live_checks["planned_accepted_commit"] = bool(
                                    recovered_branch_binding
                                    and observed_executable.accepted_commit
                                    == projected_accepted
                                    == recovery.get("accepted_commit")
                                )
                            elif (
                                planned_feature
                                and planned_feature.get("accepted_commit") == "SELF"
                                and isinstance(projected_accepted, str)
                            ):
                                live_checks["planned_accepted_commit"] = bool(
                                    observed_executable.accepted_commit == projected_accepted
                                    and self.inspector.ref_exists(projected_accepted)
                                    and observed_executable.feature_branch
                                    and self.inspector.rev_parse(
                                        observed_executable.feature_branch, check=False
                                    )
                                    == projected_accepted
                                )
                            else:
                                resolution = resolve_feature_commit(
                                    feature=planned_feature or {},
                                    queue=queue,
                                    repository=self.inspector,
                                    milestone_branch=self.project.milestone_branch,
                                    baseline=self.project.validated_baseline_commit,
                                    registered_commit=self.project.last_accepted_commit,
                                    registered_feature=self.project.last_accepted_feature,
                                )
                                live_checks["planned_accepted_commit"] = bool(
                                    resolution
                                    and resolution.commit == observed_executable.accepted_commit
                                    or planned_feature
                                    and not planned_feature.get("accepted_commit")
                                    and recovered_branch_binding
                                )
                    agreement_evidence["checks"].update(live_checks)
                    live_agreement = all(live_checks.values())
                    agreement_evidence["live_repository_binding"] = live_checks
                    agreement = agreement and live_agreement
            else:
                observation_source = "planner_observer_unavailable"
                agreement = False
                agreement_evidence = {
                    "checks": {"planner_observer_available": False},
                    "compatibility_cache": {
                        "actual_state": compatibility_state,
                        "actual_projection_fingerprint": compatibility_projection_fingerprint,
                    },
                }
            agreement_evidence["observation_source"] = observation_source
            add(
                "execution_plan_projection_agreement",
                agreement,
                ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
                evidence=agreement_evidence,
                diagnostic="status or executable routing disagrees with the kernel projection",
            )
            add(
                "integrated_feature_not_executable",
                bool(terminal_plan_checks) and all(terminal_plan_checks.values()),
                ConsistencyClassification.HUMAN_DECISION_REQUIRED,
                evidence={"checks": terminal_plan_checks},
                diagnostic=(
                    "integrated or historically accepted feature was selected by an "
                    "executable plan"
                ),
            )
        else:
            add(
                "execution_plan_projection_agreement",
                not ledger_exists,
                ConsistencyClassification.RECOVERABLE_INCONSISTENCY,
                evidence={"ledger_exists": ledger_exists},
                diagnostic="no authoritative projection is available for executable routing",
            )
            add(
                "integrated_feature_not_executable",
                not ledger_exists,
                ConsistencyClassification.HUMAN_DECISION_REQUIRED,
                evidence={"ledger_exists": ledger_exists},
                diagnostic="no authoritative executable plan is available",
            )

        failed = [item for item in results if not item.passed]
        classification = max(
            (item.classification for item in failed),
            key=lambda item: SEVERITY_ORDER[item],
            default=ConsistencyClassification.CONSISTENT,
        )
        return {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "classification": classification.value,
            "consistent": classification == ConsistencyClassification.CONSISTENT,
            "failed_invariants": [item.to_dict() for item in failed],
            "invariants": [item.to_dict() for item in results],
            "projection": effective_projection,
            "recovery": {
                "transactions_reconstructed": reconstructed,
                "transactions_superseded": superseded,
                "migration_required": not ledger_exists,
            },
            "application_repository_written": False,
        }
