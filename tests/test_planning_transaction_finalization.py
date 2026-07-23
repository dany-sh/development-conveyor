from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.contracts import (
    TransactionState,
    WorkflowType,
    bind_human_gate,
    fingerprint,
)
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import LockError, RecoveryError, SessionError
from development_conveyor.kernel import (
    QueueReconciliationAdapter,
    RecoveryAdapter,
    WorkflowKernel,
)
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.locks import PlanningWriterLease
from development_conveyor.planning import (
    LEGACY_WARNING_SUMMARY_COMPATIBILITY,
    PLANNING_CLASSIFICATIONS,
    _classify_inventory_validation,
    _selected_feature_evidence,
    authoritative_queue_validation_evidence,
    compare_queue_validation_evidence,
    finalize_planning_commit,
    validate_planning_changes,
)
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.projection import ProjectionEngine
from development_conveyor.sessions import SessionPlan, SessionResult
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


RUN_ID = "planning-recovery-run"
SESSION_ID = "019f77e1-c551-7f00-9409-2fff9f6ee79b"
ORIGINAL_TRANSACTION_ID = "50824b65-bfc2-4289-a4e7-3538ef892324"
NO_READY_TRANSACTION_ID = "e928db79-488e-4184-966d-2a32938fd91f"
EARLIER_CACHE_RECOVERY_TRANSACTION_ID = "earlier-cache-recovery-transaction"
SEVEN_PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/P0-001-per-case-persistence-authority.md",
    "docs/features/P0-003-versioned-application-registry.md",
]


def assistant_result(classification: str, feature_count: int) -> tuple[dict, str]:
    value = {
        "schema_version": 1,
        "classification": classification,
        "summary": "Synthetic planning result.",
        "next_action": "safe checkpoint",
        "queue_validation": {
            "valid": True,
            "milestone_found": True,
            "feature_count": feature_count,
            "global_feature_count": feature_count,
            "global_milestone_count": 1,
            "warning_count": 0,
        },
        "retryable": False,
        "human_decision": None,
    }
    marker = "CONVEYOR_RESULT=" + json.dumps(value, separators=(",", ":"))
    event = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": marker},
    })
    return value, event


class MutatingLauncher:
    def __init__(self, mutation):
        self.mutation = mutation
        self.lease_at_launch = None

    def launch(self, request, on_session_started=None):
        if on_session_started is not None:
            on_session_started(SESSION_ID)
        lock = request.project.repository / ".factory/locks/writer.json"
        self.lease_at_launch = json.loads(lock.read_text(encoding="utf-8"))
        self.mutation(request.project.repository, request.project.queue_location)
        queue = json.loads((request.project.repository / request.project.queue_location).read_text())
        value, output = assistant_result("reconciled_ready_work", len(queue["features"]))
        structured = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_READY_WORK",
            "project_id": request.project.project_id,
            "repository_identity": request.repository_identity,
            "transaction_id": request.transaction_id,
            "run_id": request.run_id,
            "session_id": SESSION_ID,
            "starting_branch": request.starting_branch,
            "starting_commit": request.starting_commit,
            "current_commit": request.starting_commit,
            "feature_id": None,
            "changed_paths": sorted(
                RepositoryInspector(request.project.repository).tracked_changed_paths()
            ),
            "evidence": value,
            "next_state": "feature_ready",
        }
        plan = SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64,
            "workspace-write",
        )
        return SessionResult(
            request.action,
            0,
            SESSION_ID,
            output,
            plan,
            redacted_stdout=output,
            structured_result=structured,
            structured_output_validation="valid",
            result_classification="RECONCILED_READY_WORK",
            transaction_envelope=structured,
        )


class PlanningTransactionTests(unittest.TestCase):
    def _engine_fixture(self, root: Path):
        repository, project = synthetic_repository(root, feature_status="proposed")
        project = replace(project, current_state="queue_reconciliation")
        return repository, project

    @staticmethod
    def _ready_queue(repository: Path, queue_location: str) -> None:
        path = repository / queue_location
        queue = json.loads(path.read_text())
        queue["features"][0]["status"] = "ready"
        queue["features"][0]["execution_policy"] = {
            "profile": "bounded_precise",
            "parent_sessions": 1,
            "child_sessions": 0,
        }
        write_json(path, queue)

    @staticmethod
    def _apply_case_reconciliation(repository: Path) -> None:
        queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
        queue["features"][2]["status"] = "ready"
        queue["features"][2]["execution_policy"] = {
            "profile": "multi_module_precise",
            "parent_sessions": 1,
            "child_sessions": 0,
        }
        queue["features"][2]["dependencies"] = ["P0-001", "P0-002"]
        queue["features"][2]["requires_human_decision"] = False
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        (repository / "docs/CURRENT_STATUS.md").write_text("P0-003 is ready.\n", encoding="utf-8")
        (repository / "docs/FEATURE_CATALOG.md").write_text("| P0-003 | Ready |\n", encoding="utf-8")
        (repository / "docs/ROADMAP.md").write_text("P0-003 is ready.\n", encoding="utf-8")
        (repository / "docs/RUN_LOG.md").write_text("Planning reconciliation only.\n", encoding="utf-8")
        (repository / "docs/features/P0-001-per-case-persistence-authority.md").write_text(
            "P0-001 integrated planning evidence.\n", encoding="utf-8"
        )
        (repository / "docs/features/P0-003-versioned-application-registry.md").write_text(
            "# P0-003\n\nStatus: Ready\n", encoding="utf-8"
        )

    def _case_recovery_fixture(self, root: Path, *, apply_changes: bool = True):
        repository, project = synthetic_repository(root, feature_status="proposed")
        baseline = git(repository, "rev-parse", "HEAD")
        git(repository, "branch", "codex/p0-foundation", baseline)
        git(repository, "switch", "codex/p0-foundation")
        project = replace(
            project,
            active_milestone="P0",
            milestone_branch="codex/p0-foundation",
            current_state="queue_reconciliation",
        )
        for relative in SEVEN_PATHS:
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative != "docs/FEATURE_QUEUE.yaml":
                path.write_text("Planning baseline.\n", encoding="utf-8")
        (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
        (repository / ".factory/locks/.gitignore").write_text("writer.json\n", encoding="utf-8")
        queue = {
            "schema_version": 1,
            "milestones": [{
                "id": "phase-0",
                "name": "Phase 0",
                "status": "active",
                "base_commit": baseline,
                "integration_branch": "codex/p0-foundation",
                "integrated_features": ["P0-001", "P0-002"],
                "last_validated_commit": baseline,
                "human_gate": True,
            }],
            "features": [],
        }
        for index in range(1, 15):
            feature_id = f"P0-{index:03d}"
            status = "integrated" if index == 1 else ("done" if index == 2 else "proposed")
            dependencies = [] if index <= 2 else [f"P0-{index - 1:03d}"]
            feature = {
                "id": feature_id,
                "name": f"Synthetic {feature_id}",
                "status": status,
                "priority": index,
                "milestone": "phase-0",
                "dependencies": dependencies,
                "spec": (
                    "docs/features/P0-001-per-case-persistence-authority.md"
                    if index == 1
                    else "docs/features/P0-003-versioned-application-registry.md"
                ),
                "acceptance_criteria": [f"{feature_id} acceptance"],
                "requires_human_decision": index in {11, 14},
                "branch": None,
                "accepted_commit": baseline if index == 1 else None,
                "integrated_commit": baseline if index == 1 else None,
                "integration_status": "passed" if index <= 2 else "pending",
                "integration_fix_commits": [],
            }
            if index == 2:
                feature["commit"] = baseline
            queue["features"].append(feature)
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        git(repository, "add", ".")
        git(repository, "commit", "-m", "synthetic Phase 0 planning baseline")
        starting_head = git(repository, "rev-parse", "HEAD")

        if apply_changes:
            self._apply_case_reconciliation(repository)
        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration)
        value, output = assistant_result("reconciled_ready_work", 14)
        report = {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "action": "queue_reconciliation",
            "working_directory": str(repository),
            "exit_status": 0,
            "structured_output_validation": "valid",
            "structured_result": value,
            "redacted_stdout": output,
            "session_id": SESSION_ID,
        }
        report_path = configuration.root / "reports" / RUN_ID / "queue_reconciliation.json"
        write_json(report_path, report)
        inspector = RepositoryInspector(repository)
        return repository, project, engine, starting_head, inspector.planning_diff_fingerprint()

    def _terminal_kernel_recovery_fixture(self, root: Path):
        repository, project, engine, starting_head, _ = self._case_recovery_fixture(
            root, apply_changes=False
        )
        inspector = RepositoryInspector(repository)
        identity = inspector.identity()
        state_root = engine.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": "earlier-cache-recovery",
                    "feature_id": None,
                    "milestone": "P0",
                    "starting_branch": project.milestone_branch,
                    "starting_head": inspector.head,
                    "allowed_mutation_policy": {},
                },
            ),
            (
                "LeaseAcquired",
                {
                    "lease_id": "earlier-cache-recovery-lease",
                    "lease_type": "recovery_writer",
                },
            ),
            (
                "SnapshotCaptured",
                {
                    "snapshot": {
                        "branch": inspector.current_branch,
                        "head": inspector.head,
                        "clean": True,
                    }
                },
            ),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            (
                "TransactionCompleted",
                {
                    "classification": "RECOVERY_APPLIED",
                    "next_state": "queue_reconciliation",
                    "selected_feature": None,
                },
            ),
            (
                "LeaseReleased",
                {"lease_id": "earlier-cache-recovery-lease"},
            ),
        ):
            ledger.append(
                event_type=event_type,
                transaction_id=EARLIER_CACHE_RECOVERY_TRANSACTION_ID,
                workflow_type=WorkflowType.RECOVERY,
                payload=payload,
            )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
        adapter = QueueReconciliationAdapter(
            allowed_paths=SEVEN_PATHS,
            commit_subject="factory: reconcile P0 queue and ready P0-003",
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection,
            lease=lease,
        )
        kernel.begin(
            workflow_type=WorkflowType.QUEUE_RECONCILIATION,
            milestone="P0",
            feature_id=None,
            run_id=RUN_ID,
            policy=adapter.policy,
            transaction_id=ORIGINAL_TRANSACTION_ID,
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        kernel.checkpoint("planning_session_reserved", {"models_planned": 1})
        kernel.session_launched(SESSION_ID)
        self._apply_case_reconciliation(repository)
        changed_paths = sorted(inspector.tracked_changed_paths())
        diff_fingerprint = inspector.planning_diff_fingerprint()
        evidence = {
            "schema_version": 1,
            "classification": "reconciled_ready_work",
            "summary": "Synthetic queue reconciled.",
            "next_action": "feature_cycle",
            "queue_validation": {
                "valid": True,
                "milestone_found": True,
                "feature_count": 14,
                "global_feature_count": 14,
                "global_milestone_count": 1,
                "warning_count": 1,
                "selected_feature": "P0-003",
                "ready_features": ["P0-003"],
                "dependencies_complete": True,
            },
            "retryable": False,
            "human_decision": None,
        }
        envelope = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_READY_WORK",
            "project_id": project.project_id,
            "repository_identity": identity["repository_id"],
            "transaction_id": ORIGINAL_TRANSACTION_ID,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "starting_branch": project.milestone_branch,
            "starting_commit": starting_head,
            "current_commit": starting_head,
            "feature_id": None,
            "changed_paths": changed_paths,
            "evidence": evidence,
            "next_state": "feature_ready",
        }
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
        report = {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "action": "queue_reconciliation",
            "working_directory": str(repository),
            "exit_status": 0,
            "structured_output_validation": "valid",
            "result_classification": "RECONCILED_READY_WORK",
            "structured_result": envelope,
            "parsed_structured_result": envelope,
            "terminal_marker_found": True,
            "redacted_stdout": "typed terminal result",
            "session_id": SESSION_ID,
        }
        write_json(report_path, report)
        warnings = ["M1: base_commit is required before feature preparation or integration"]
        planning_transaction = {
            "schema_version": 1,
            "status": "planning_validation_failed",
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "planning_start_commit": starting_head,
            "changed_paths": changed_paths,
            "changed_path_count": len(changed_paths),
            "diff_fingerprint": diff_fingerprint,
            "result_classification": "RECONCILED_READY_WORK",
            "reconciliation_report": str(report_path),
            "error": "deterministic inventory validation failed: " + json.dumps({
                "exit_code": 0,
                "errors": [],
                "warnings": warnings,
                "blocking_warnings": [],
            }, sort_keys=True),
        }
        write_json(
            engine.root / "reports" / RUN_ID / "planning-transaction.json",
            planning_transaction,
        )
        kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification="PLANNING_VALIDATION_FAILED",
            next_state="validation_failed",
            reference="RecoveryError",
        )
        return repository, project, engine, starting_head, ledger, warnings

    def _failed_legacy_recovery_fixture(self, root: Path):
        (
            repository,
            project,
            engine,
            starting_head,
            ledger,
            warnings,
        ) = self._terminal_kernel_recovery_fixture(root)
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
        report = json.loads(report_path.read_text())
        envelope = report["structured_result"]
        queue_validation = envelope["evidence"]["queue_validation"]
        queue_validation.pop("warning_count", None)
        queue_validation["warnings"] = "legacy planning warnings"
        report["parsed_structured_result"] = envelope
        write_json(report_path, report)
        transaction_path = (
            engine.root / "reports" / RUN_ID / "planning-transaction.json"
        )
        planning_transaction = json.loads(transaction_path.read_text())
        planning_transaction["error"] = "legacy warning parse failure"
        write_json(transaction_path, planning_transaction)

        original_events = ledger.read()
        for path in (ledger.path, ledger.head_path, ledger.lock_path):
            path.unlink(missing_ok=True)
        projection_path = ledger.path.parent / "projection-cache.json"
        projection_path.unlink(missing_ok=True)
        identity = RepositoryInspector(repository).identity()
        ledger = EvidenceLedger(
            ledger.path,
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        historical_transaction = "historical-f003-transaction"
        historical_gate = bind_human_gate(
            {
                "classification": "structured_output_invalid",
                "reason": "historical F003 gate",
                "feature": "F003",
            },
            transaction_id=historical_transaction,
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            approved_next_state="queue_reconciliation",
            terminal_classification="HUMAN_DECISION_REQUIRED",
        )
        ledger.append(
            event_type="TransactionStarted",
            transaction_id=historical_transaction,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "run_id": "historical-f003-run",
                "feature_id": "F003",
                "milestone": "P0",
                "starting_branch": project.milestone_branch,
                "starting_head": starting_head,
                "allowed_mutation_policy": {},
            },
        )
        ledger.append(
            event_type="HumanGateRaised",
            transaction_id=historical_transaction,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "classification": "HUMAN_DECISION_REQUIRED",
                "terminal_state": "human_decision_required",
                "reference": historical_gate["gate_id"],
                "next_state": "human_decision_required",
                "gate": historical_gate,
                "gate_id": historical_gate["gate_id"],
                "gate_fingerprint": fingerprint(historical_gate),
                "terminal_snapshot": {
                    "branch": project.milestone_branch,
                    "head": starting_head,
                    "clean": True,
                },
            },
        )
        for event in original_events:
            ledger.append(
                event_type=event["event_type"],
                transaction_id=event["transaction_id"],
                workflow_type=WorkflowType(event["workflow_type"]),
                payload=event["payload"],
            )
        projection = ProjectionEngine(ledger, projection_path)
        projection.rebuild(persist_cache=True)
        inspector = RepositoryInspector(repository)
        failed_adapter = RecoveryAdapter(
            allowed_paths=SEVEN_PATHS,
            commit_subject="factory: reconcile P0 queue and ready P0-003",
            next_state="feature_ready",
            require_clean_start=False,
        )
        failed_kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection,
            lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
        )
        report_fingerprint = fingerprint(report)
        failed_transaction = "failed-legacy-warning-recovery"
        failed_kernel.begin(
            workflow_type=WorkflowType.RECOVERY,
            milestone="P0",
            feature_id="P0-003",
            run_id="failed-legacy-warning-run",
            policy=failed_adapter.policy,
            transaction_id=failed_transaction,
            start_evidence={
                "recovered_transaction_id": ORIGINAL_TRANSACTION_ID,
                "original_run_id": RUN_ID,
                "original_session_id": SESSION_ID,
                "planning_transaction_fingerprint": fingerprint(
                    planning_transaction
                ),
                "session_report_fingerprint": report_fingerprint,
                "model_sessions_planned": 0,
                "child_sessions_planned": 0,
            },
        )
        failed_kernel.acquire_lease()
        failed_kernel.capture_snapshot()
        failed_kernel.checkpoint(
            "planning_finalization_recovery_verified",
            {
                "recovered_transaction_id": ORIGINAL_TRANSACTION_ID,
                "changed_paths": SEVEN_PATHS,
                "diff_fingerprint": inspector.planning_diff_fingerprint(),
                "selected_feature": "P0-003",
                "model_session_launched": False,
            },
        )
        failed_kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification="TERMINAL_RECOVERY_FAILURE",
            next_state="human_decision_required",
            reference="RecoveryError",
        )
        compatibility = {
            "project_id": project.project_id,
            "transaction_id": ORIGINAL_TRANSACTION_ID,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "report_fingerprint": report_fingerprint,
            "result_classification": "RECONCILED_READY_WORK",
            "failed_recovery_transaction_id": failed_transaction,
            "failed_recovery_run_id": "failed-legacy-warning-run",
            "failed_recovery_sequences": [
                event["sequence"]
                for event in ledger.read()
                if event["transaction_id"] == failed_transaction
            ],
            "summary": "legacy planning warnings",
            "error": "legacy warning parse failure",
            "changed_paths": sorted(SEVEN_PATHS),
        }
        return (
            repository,
            project,
            engine,
            starting_head,
            ledger,
            warnings,
            failed_transaction,
            historical_gate,
            compatibility,
        )

    def _no_ready_count_recovery_fixture(self, root: Path):
        repository, project = synthetic_repository(root, feature_status="proposed")
        baseline = git(repository, "rev-parse", "HEAD")
        project = replace(
            project,
            active_milestone="M0",
            milestone_branch="codex/m0-foundation",
            current_state="queue_reconciliation",
        )
        milestones = [
            {
                "id": f"M{index}",
                "name": f"Milestone {index}",
                "status": "active" if index == 0 else "planned",
                "base_commit": baseline if index == 0 else None,
                "integration_branch": "codex/m0-foundation" if index == 0 else None,
                "integrated_features": [f"F{number:03d}" for number in range(4)]
                if index == 0
                else [],
                "last_validated_commit": baseline if index == 0 else None,
                "human_gate": index == 0,
            }
            for index in range(10)
        ]
        features = []
        for number in range(97):
            milestone = "M0" if number < 12 else f"M{1 + ((number - 12) % 9)}"
            status = (
                "integrated"
                if number < 4
                else ("done" if number < 6 else "proposed")
            )
            features.append(
                {
                    "id": f"F{number:03d}",
                    "title": f"Synthetic F{number:03d}",
                    "status": status,
                    "priority": number,
                    "milestone": milestone,
                    "dependencies": [],
                    "requires_human_decision": False,
                    "integration_status": "passed" if number < 6 else "pending",
                    "integrated_commit": baseline if number < 4 else None,
                }
            )
        write_json(
            repository / "docs/FEATURE_QUEUE.yaml",
            {
                "schema_version": 1,
                "milestones": milestones,
                "features": features,
            },
        )
        retained_paths = [
            "docs/CURRENT_STATUS.md",
            "docs/FEATURE_CATALOG.md",
            "docs/ROADMAP.md",
            "docs/RUN_LOG.md",
            "docs/features/F004-navigation-and-workspace-restoration.md",
        ]
        (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
        (repository / ".factory/locks/.gitignore").write_text(
            "writer.json\n", encoding="utf-8"
        )
        for relative in retained_paths:
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Planning baseline.\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "synthetic 97-feature planning baseline")
        starting_head = git(repository, "rev-parse", "HEAD")

        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration)
        inspector = RepositoryInspector(repository)
        identity = inspector.identity()
        state_root = engine.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection,
            lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
        )
        adapter = QueueReconciliationAdapter(
            allowed_paths=retained_paths,
            commit_subject="factory: reconcile M0 queue",
            next_state="paused",
        )
        kernel.begin(
            workflow_type=WorkflowType.QUEUE_RECONCILIATION,
            milestone="M0",
            feature_id=None,
            run_id=RUN_ID,
            policy=adapter.policy,
            transaction_id=NO_READY_TRANSACTION_ID,
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        kernel.checkpoint("planning_session_reserved", {"models_planned": 1})
        kernel.session_launched(SESSION_ID)
        for relative in retained_paths:
            (repository / relative).write_text(
                "F004 is integrated; F006-F011 remain proposed.\n",
                encoding="utf-8",
            )
        changed_paths = sorted(inspector.tracked_changed_paths())
        evidence = {
            "schema_version": 1,
            "classification": "reconciled_no_ready_work",
            "summary": "Queue valid with no ready work.",
            "next_action": "pause",
            "queue_validation": {
                "valid": True,
                "milestone_found": True,
                "active_milestone": "M0",
                "feature_count": 12,
                "global_feature_count": 97,
                "global_milestone_count": 10,
                "warning_count": 0,
                "ready": [],
            },
            "retryable": False,
            "human_decision": None,
        }
        envelope = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_NO_READY_WORK",
            "project_id": project.project_id,
            "repository_identity": identity["repository_id"],
            "transaction_id": NO_READY_TRANSACTION_ID,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "starting_branch": project.milestone_branch,
            "starting_commit": starting_head,
            "current_commit": starting_head,
            "feature_id": None,
            "changed_paths": changed_paths,
            "evidence": evidence,
            "next_state": "paused",
        }
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
        write_json(
            report_path,
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": RUN_ID,
                "action": "queue_reconciliation",
                "working_directory": str(repository),
                "exit_status": 0,
                "structured_output_validation": "valid",
                "result_classification": "RECONCILED_NO_READY_WORK",
                "structured_result": envelope,
                "parsed_structured_result": envelope,
                "terminal_marker_found": True,
                "redacted_stdout": "typed terminal result",
                "session_id": SESSION_ID,
            },
        )
        diff_fingerprint = inspector.planning_diff_fingerprint()
        write_json(
            engine.root / "reports" / RUN_ID / "planning-transaction.json",
            {
                "schema_version": 1,
                "status": "planning_validation_failed",
                "project_id": project.project_id,
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "planning_start_commit": starting_head,
                "changed_paths": changed_paths,
                "changed_path_count": len(changed_paths),
                "diff_fingerprint": diff_fingerprint,
                "result_classification": "RECONCILED_NO_READY_WORK",
                "reconciliation_report": str(report_path),
                "error": "queue validation evidence disagrees semantically: feature_count",
            },
        )
        kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification="PLANNING_VALIDATION_FAILED",
            next_state="validation_failed",
        )
        return (
            repository,
            project,
            engine,
            starting_head,
            diff_fingerprint,
            retained_paths,
            ledger,
        )

    def test_01_planning_lease_exists_before_session_mutation_and_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            launcher = MutatingLauncher(self._ready_queue)
            engine = CycleEngine(controller_configuration(root, project), launcher)
            state = engine._project_document(
                project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"]
            )
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(launcher.lease_at_launch["phase"], "queue_reconciliation")
            self.assertEqual(launcher.lease_at_launch["purpose"], "development-conveyor-planning")
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertEqual(result["planning_transaction"]["planning_commit_status"], "committed")

    def test_02_planning_commit_is_focused_and_repository_is_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(self._ready_queue))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            commit = result["planning_result_commit"]
            self.assertEqual(RepositoryInspector(repository).changed_paths(commit), ["docs/FEATURE_QUEUE.yaml"])
            # Kernel planning commits describe the exact phase mutation; feature
            # selection remains projection evidence rather than commit-message authority.
            self.assertEqual(RepositoryInspector(repository).commit_subject(commit), "factory: reconcile M0 queue")
            self.assertTrue(RepositoryInspector(repository).is_clean)

    def test_03_unauthorized_production_change_rejects_finalization_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "app.txt").write_text("unauthorized production edit\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(result["outcome"], "planning_validation_failed")
            self.assertIn("app.txt", result["changed_paths"])
            self.assertEqual((repository / "app.txt").read_text(), "unauthorized production edit\n")
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_04_unauthorized_product_test_change_rejects_finalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            (repository / "tests").mkdir()
            (repository / "tests/product_test.py").write_text("baseline\n", encoding="utf-8")
            git(repository, "add", "tests/product_test.py")
            git(repository, "commit", "-m", "add baseline product test")
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "tests/product_test.py").write_text("implementation\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(result["outcome"], "planning_validation_failed")
            self.assertIn("tests/product_test.py", result["changed_paths"])

    def test_05_allowed_path_without_semantic_agreement_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            (repository / "docs/CURRENT_STATUS.md").write_text("F001 proposed.\n", encoding="utf-8")
            git(repository, "add", "docs/CURRENT_STATUS.md")
            git(repository, "commit", "-m", "status baseline")
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "docs/CURRENT_STATUS.md").write_text("F001 remains proposed.\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertIn("semantic agreement", result["failed_validator"])

    def test_06_planning_lease_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            lease = PlanningWriterLease(repository / ".factory/locks/writer.json", repository)
            lease.acquire(
                repository_identity=inspector.identity()["repository_id"], project_id="synthetic",
                milestone="M0", run_id=RUN_ID, session_id=SESSION_ID,
                branch=str(inspector.current_branch), head=inspector.head,
                worktree_fingerprint="f" * 64, allowed_paths=["docs/FEATURE_QUEUE.yaml"],
            )
            with self.assertRaisesRegex(LockError, "identity mismatch"):
                lease.revalidate(run_id="other-run")
            lease.release(run_id=RUN_ID)

    def test_07_current_seven_file_recovery_dry_run_is_write_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            before = git(repository, "status", "--porcelain=v1", "--branch")
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=True,
            )
            after = git(repository, "status", "--porcelain=v1", "--branch")
            self.assertEqual(before, after)
            self.assertEqual(result["planning_transaction"]["changed_path_count"], 7)
            self.assertFalse(result["feature_factory_would_launch"])
            self.assertFalse(result["milestone_integrator_would_launch"])

    def test_08_current_seven_file_recovery_creates_exactly_one_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            commit = result["planning_transaction"]["planning_result_commit"]
            self.assertEqual(git(repository, "rev-list", "--count", f"{head}..{commit}"), "1")
            self.assertEqual(RepositoryInspector(repository).changed_paths(commit), sorted(SEVEN_PATHS))
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertEqual(result["planning_transaction"]["selected_feature"], "P0-003")
            self.assertEqual(result["planning_transaction"]["selected_feature_starting_commit"], commit)

    def test_09_repeated_applied_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            arguments = dict(
                project=project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            first = engine.recover_planning_transaction(**arguments)
            second = engine.recover_planning_transaction(**arguments)
            self.assertEqual(second["outcome"], "planning_transaction_already_finalized")
            self.assertEqual(
                first["planning_transaction"]["planning_result_commit"],
                second["planning_transaction"]["planning_result_commit"],
            )
            self.assertEqual(git(repository, "rev-list", "--count", f"{head}..HEAD"), "1")

    def test_10_starting_head_divergence_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, _, diff = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "starting HEAD diverged"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head="0" * 40,
                    expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_11_diff_fingerprint_mismatch_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, _ = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "diff fingerprint"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head=head,
                    expected_diff_fingerprint="0" * 64, expected_changed_paths=SEVEN_PATHS,
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_newly_readied_feature_without_execution_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, _ = self._case_recovery_fixture(root)
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][2].pop("execution_policy")
            write_json(queue_path, queue)
            diff = RepositoryInspector(repository).planning_diff_fingerprint()
            with self.assertRaisesRegex(RecoveryError, "lacks required execution_policy"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head=head,
                    expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_12_changed_path_mismatch_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "changed-path set"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head=head,
                    expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS[:-1],
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_13_queue_fingerprint_change_after_validation_rejects_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            report = json.loads((engine.root / "reports" / RUN_ID / "queue_reconciliation.json").read_text())
            inspector = RepositoryInspector(repository)
            validation = validate_planning_changes(
                project, inspector, report, run_id=RUN_ID, starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID,
            )
            queue_path = repository / "docs/FEATURE_QUEUE.yaml"
            queue_path.write_text(queue_path.read_text() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "diff changed|queue changed"):
                finalize_planning_commit(project, inspector, validation)

    def test_14_planning_baseline_fields_are_non_self_referential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )["planning_transaction"]
            self.assertEqual(result["previous_validated_milestone_head"], head)
            self.assertNotEqual(result["planning_result_commit"], head)
            self.assertIsNone(result["planning_evidence_commit"])

    def test_15_status_separates_planning_from_repository_and_next_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            status = engine.project_plan(project)
            self.assertEqual(status["current_planning_transaction"]["planning_commit_status"], "committed")
            self.assertTrue(status["current_repository_state"]["clean"])
            self.assertEqual(status["next_feature_selection"]["selected_feature"], "P0-003")
            self.assertEqual(status["proposed_next_action"], "feature_cycle")
            self.assertEqual(
                status["sessions_that_would_launch"],
                ["fresh feature transaction"],
            )
            self.assertFalse(status["milestone_integrator_would_launch"])

    def test_16_recovery_never_launches_selected_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            self.assertFalse(result["feature_factory_would_launch"])
            self.assertFalse(result["milestone_integrator_would_launch"])
            self.assertFalse(result["application_source_written"])

    def test_17_explicit_terminal_classification_contract_is_exposed(self):
        self.assertEqual(PLANNING_CLASSIFICATIONS["reconciled_ready_work"], "RECONCILED_READY_WORK")
        self.assertEqual(PLANNING_CLASSIFICATIONS["reconciled_no_ready_work"], "RECONCILED_NO_READY_WORK")
        self.assertEqual(PLANNING_CLASSIFICATIONS["human_decision_required"], "HUMAN_DECISION_REQUIRED")
        self.assertEqual(PLANNING_CLASSIFICATIONS["invalid_queue"], "PLANNING_VALIDATION_FAILED")

    def test_18_recovery_commit_does_not_change_application_or_test_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            app_before = (repository / "app.txt").read_bytes()
            engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertFalse(any(path.startswith("tests/") for path in RepositoryInspector(repository).changed_paths("HEAD")))

    def test_19_inventory_warnings_are_nonfatal_by_default(self):
        result = _classify_inventory_validation(
            exit_code=0,
            value={"ok": True, "errors": [], "warnings": ["future branch is not derived yet"]},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["nonfatal_warnings"], ["future branch is not derived yet"])
        self.assertEqual(result["blocking_warnings"], [])

    def test_20_inventory_nonzero_errors_and_configured_blocking_warnings_fail(self):
        cases = [
            (1, {"errors": [], "warnings": []}, ()),
            (0, {"errors": ["queue is invalid"], "warnings": []}, ()),
            (0, {"errors": [], "warnings": ["SECURITY: review required"]}, ("SECURITY:*",)),
        ]
        for exit_code, value, patterns in cases:
            with self.subTest(exit_code=exit_code, value=value, patterns=patterns):
                with self.assertRaisesRegex(RecoveryError, "inventory validation failed"):
                    _classify_inventory_validation(
                        exit_code=exit_code,
                        value={"ok": False, **value},
                        blocking_warning_patterns=patterns,
                    )

    def test_21_terminal_planning_dry_run_is_exact_and_write_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, starting_head, ledger, warnings = (
                self._terminal_kernel_recovery_fixture(root)
            )
            status_before = git(repository, "status", "--porcelain=v1", "--branch")
            ledger_before = ledger.path.read_bytes()
            inventory = {
                "ok": True,
                "exit_code": 0,
                "errors": [],
                "warnings": warnings,
                "nonfatal_warnings": warnings,
                "blocking_warnings": [],
                "feature_count": 14,
                "global_feature_count": 14,
                "global_milestone_count": 1,
                "validator": "synthetic inventory validator",
            }
            with (
                patch.object(engine, "_compatibility_snapshot", return_value={}) as compatibility,
                patch(
                    "development_conveyor.planning._inventory_validation",
                    return_value=inventory,
                ),
            ):
                plan = engine.run_project(project, "resume", dry_run=True)
            compatibility.assert_not_called()
            self.assertEqual(git(repository, "status", "--porcelain=v1", "--branch"), status_before)
            self.assertEqual(ledger.path.read_bytes(), ledger_before)
            self.assertEqual(plan["current_state"], "validation_failed")
            self.assertEqual(plan["workflow_type"], "queue_reconciliation recovery/finalization")
            self.assertEqual(plan["transaction_mode"], "recovery")
            self.assertEqual(plan["original_transaction_id"], ORIGINAL_TRANSACTION_ID)
            self.assertEqual(plan["starting_commit"], starting_head)
            self.assertEqual(plan["existing_planning_changes"]["paths"], sorted(SEVEN_PATHS))
            self.assertEqual(plan["selected_feature"], "P0-003")
            self.assertEqual(plan["planning_finalization_recovery"]["dependencies"], ["P0-001", "P0-002"])
            self.assertEqual(plan["planning_finalization_recovery"]["nonfatal_warnings"], warnings)
            self.assertEqual(plan["model_sessions_that_would_launch"], [])
            self.assertEqual(plan["child_sessions_that_would_launch"], [])
            self.assertEqual(plan["cost_aware_run_plan"]["execution"]["models_planned"], 0)
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertFalse(plan["milestone_integrator_would_launch"])
            self.assertFalse(plan["planning_content_regeneration_would_run"])

    def test_22_terminal_planning_topology_rejects_branch_head_path_and_production_drift(self):
        mutations = {
            "branch": lambda repository, project: replace(project, milestone_branch="codex/other"),
            "head": lambda repository, project: (
                git(repository, "commit", "--allow-empty", "-m", "advance head"), project
            )[1],
            "path": lambda repository, project: (
                (repository / "docs/README.md").write_text("extra planning path\n", encoding="utf-8"),
                project,
            )[1],
            "production": lambda repository, project: (
                (repository / "app.txt").write_text("production drift\n", encoding="utf-8"),
                project,
            )[1],
        }
        for classification, mutate in mutations.items():
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, project, engine, _, _, _ = self._terminal_kernel_recovery_fixture(root)
                changed_project = mutate(repository, project)
                with patch.object(engine, "_compatibility_snapshot", return_value={}):
                    with self.assertRaisesRegex(RecoveryError, "topology disagrees"):
                        engine.project_plan(changed_project)

    def test_23_terminal_planning_recovery_uses_fresh_transaction_and_one_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, starting_head, ledger, warnings = (
                self._terminal_kernel_recovery_fixture(root)
            )
            app_before = (repository / "app.txt").read_bytes()
            original_events = [
                event for event in ledger.read()
                if event["transaction_id"] == ORIGINAL_TRANSACTION_ID
            ]
            inventory = {
                "ok": True,
                "exit_code": 0,
                "errors": [],
                "warnings": warnings,
                "nonfatal_warnings": warnings,
                "blocking_warnings": [],
                "feature_count": 14,
                "global_feature_count": 14,
                "global_milestone_count": 1,
                "validator": "synthetic inventory validator",
            }
            with (
                patch.object(engine, "_compatibility_snapshot", return_value={}),
                patch("development_conveyor.planning._inventory_validation", return_value=inventory),
            ):
                expected_plan = engine.run_project(project, "resume", dry_run=True)
                result = engine._recover_terminal_planning_finalization(
                    project,
                    expected_plan=expected_plan["planning_finalization_recovery"],
                )
            self.assertEqual(result["outcome"], "planning_recovery_committed")
            self.assertEqual(result["selected_feature"], "P0-003")
            self.assertEqual(result["nonfatal_warnings"], warnings)
            self.assertEqual(git(repository, "rev-list", "--count", f"{starting_head}..HEAD"), "1")
            self.assertEqual(RepositoryInspector(repository).changed_paths("HEAD"), sorted(SEVEN_PATHS))
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            all_events = ledger.read()
            self.assertEqual(
                [event for event in all_events if event["transaction_id"] == ORIGINAL_TRANSACTION_ID],
                original_events,
            )
            recovery_events = [
                event for event in all_events
                if event["transaction_id"] == result["recovery_transaction_id"]
            ]
            self.assertNotEqual(result["recovery_transaction_id"], ORIGINAL_TRANSACTION_ID)
            self.assertEqual(recovery_events[0]["workflow_type"], "recovery")
            self.assertEqual(
                len([event for event in recovery_events if event["event_type"] == "CommitFinalized"]),
                1,
            )
            self.assertTrue(any(event["event_type"] == "RecoveryApplied" for event in recovery_events))
            self.assertTrue(any(event["event_type"] == "TransactionCompleted" for event in recovery_events))
            earlier_terminal = ledger.terminal_event(
                EARLIER_CACHE_RECOVERY_TRANSACTION_ID
            )
            planning_terminal = ledger.terminal_event(
                result["recovery_transaction_id"]
            )
            self.assertIsNotNone(earlier_terminal)
            self.assertIsNotNone(planning_terminal)
            self.assertLess(
                earlier_terminal["sequence"],
                planning_terminal["sequence"],
            )
            cycle = json.loads((repository / ".factory/conveyor-state.json").read_text())
            self.assertEqual(result["recovery_transaction_id"], cycle["kernel_transaction_id"])
            self.assertEqual("P0-003", cycle["current_feature"])
            self.assertEqual("feature_ready", cycle["current_phase"])
            self.assertEqual("committed_queue_reconciliation_recovery_terminal", cycle["last_successful_checkpoint"])
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual(report["nonfatal_warnings"], warnings)
            with self.assertRaisesRegex(RecoveryError, "projection disappeared|evidence changed"):
                engine._recover_terminal_planning_finalization(
                    project,
                    expected_plan=expected_plan["planning_finalization_recovery"],
                )
            self.assertEqual(git(repository, "rev-list", "--count", f"{starting_head}..HEAD"), "1")
            subsequent = engine.run_project(project, "resume", dry_run=True)
            self.assertNotEqual(
                "planning_finalization",
                subsequent.get("proposed_next_action"),
            )
            self.assertNotIn("planning_finalization_recovery", subsequent)

    def test_24_milestone_local_and_global_counts_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                _,
                _,
                _,
                _,
            ) = self._no_ready_count_recovery_fixture(Path(temporary))
            queue = FeatureQueue.from_location(repository, project.queue_location)
            authoritative = authoritative_queue_validation_evidence(project, queue)
            self.assertEqual(authoritative["feature_count"], 12)
            self.assertEqual(authoritative["global_feature_count"], 97)
            self.assertEqual(authoritative["global_milestone_count"], 10)
            structured = {
                "valid": True,
                "milestone_found": True,
                "active_milestone": "M0",
                "feature_count": 12,
                "global_feature_count": 97,
                "global_milestone_count": 10,
                "warning_count": 0,
                "ready": [],
            }
            comparison = compare_queue_validation_evidence(
                structured, authoritative
            )
            self.assertEqual(
                comparison["normalized_structured"]["feature_count"], 12
            )
            self.assertEqual(
                comparison["normalized_structured"]["global_feature_count"], 97
            )
            self.assertIsNone(
                _selected_feature_evidence(
                    project, queue, "reconciled_no_ready_work"
                )["selected_feature"]
            )

    def test_25_global_count_cannot_be_used_as_milestone_count(self):
        deterministic = {
            "ok": True,
            "milestone_found": True,
            "active_milestone": "M0",
            "feature_count": 12,
            "global_feature_count": 97,
            "global_milestone_count": 10,
            "warning_count": 0,
            "warnings": [],
            "blocking_warnings": [],
            "ready": [],
        }
        cases = {
            "global_as_local": {**deterministic, "feature_count": 97},
            "wrong_local": {**deterministic, "feature_count": 11},
            "wrong_global": {**deterministic, "global_feature_count": 96},
            "wrong_global_milestones": {
                **deterministic,
                "global_milestone_count": 9,
            },
        }
        for name, structured in cases.items():
            with self.subTest(name=name), self.assertRaises(RecoveryError):
                compare_queue_validation_evidence(structured, deterministic)
        with self.assertRaisesRegex(RecoveryError, "active_milestone"):
            compare_queue_validation_evidence(
                {**deterministic, "active_milestone": "M1"},
                deterministic,
            )

    def test_26_no_ready_count_recovery_dry_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                engine,
                starting_head,
                diff_fingerprint,
                retained_paths,
                ledger,
            ) = self._no_ready_count_recovery_fixture(Path(temporary))
            status_before = git(repository, "status", "--porcelain=v1", "--branch")
            ledger_before = ledger.path.read_bytes()
            projection_path = ledger.path.parent / "projection-cache.json"
            projection_before = projection_path.read_bytes()
            result = engine.recover_planning_transaction(
                project,
                run_id=RUN_ID,
                expected_starting_head=starting_head,
                expected_diff_fingerprint=diff_fingerprint,
                expected_changed_paths=retained_paths,
                expected_session_id=SESSION_ID,
                dry_run=True,
            )
            self.assertEqual(result["outcome"], "planning_recovery_validated")
            self.assertEqual(
                result["planning_finalization_recovery"]["selected_feature"], None
            )
            self.assertEqual(
                git(repository, "status", "--porcelain=v1", "--branch"),
                status_before,
            )
            self.assertEqual(ledger.path.read_bytes(), ledger_before)
            self.assertEqual(projection_path.read_bytes(), projection_before)
            self.assertEqual(result["model_sessions_that_would_launch"], [])
            self.assertEqual(result["child_sessions_that_would_launch"], [])
            consistency = ConsistencyChecker(
                controller_root=engine.root,
                project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertNotEqual(
                consistency["classification"], "UNSAFE_REPOSITORY_STATE"
            )
            worktree = next(
                item
                for item in consistency["invariants"]
                if item["invariant"] == "worktree_status"
            )
            self.assertTrue(worktree["passed"])
            self.assertTrue(
                worktree["evidence"]["planning_finalization_recovery"]
            )

    def test_27_no_ready_count_recovery_commits_only_retained_planning_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                engine,
                starting_head,
                diff_fingerprint,
                retained_paths,
                ledger,
            ) = self._no_ready_count_recovery_fixture(Path(temporary))
            app_before = (repository / "app.txt").read_bytes()
            result = engine.recover_planning_transaction(
                project,
                run_id=RUN_ID,
                expected_starting_head=starting_head,
                expected_diff_fingerprint=diff_fingerprint,
                expected_changed_paths=retained_paths,
                expected_session_id=SESSION_ID,
                dry_run=False,
            )
            commit = result["planning_result_commit"]
            self.assertEqual(result["current_state"], "paused")
            self.assertIsNone(result["selected_feature"])
            self.assertTrue(result["ordinary_queue_reconciliation_available"])
            self.assertEqual(
                result["next_action_after_consistency"], "planning_refinement"
            )
            self.assertEqual(
                RepositoryInspector(repository).changed_paths(commit),
                sorted(retained_paths),
            )
            self.assertEqual(
                git(repository, "rev-list", "--count", f"{starting_head}..{commit}"),
                "1",
            )
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertFalse(
                any(
                    path.startswith(("src/", "tests/", "Sources/", "Tests/"))
                    for path in RepositoryInspector(repository).changed_paths(commit)
                )
            )
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            recovery_events = [
                event
                for event in ledger.read()
                if event["transaction_id"] == result["recovery_transaction_id"]
            ]
            self.assertEqual(
                len(
                    [
                        event
                        for event in recovery_events
                        if event["event_type"] == "CommitFinalized"
                    ]
                ),
                1,
            )
            self.assertFalse(
                any(event["event_type"] == "SessionLaunched" for event in recovery_events)
            )
            plan = engine.project_plan(project)
            self.assertEqual(plan["proposed_next_action"], "verify_consistency")
            self.assertEqual(
                FeatureQueue.from_location(
                    repository, project.queue_location
                ).summary("M0")["reconciliation_classification"],
                "reconciled_no_ready_work",
            )
            cycle_path = repository / ".factory/conveyor-state.json"
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
            cycle["cache_binding_recovery"] = {
                "source_transaction": "earlier-cache-recovery",
                "recovery_run_id": "cache-recovery-earlier",
            }
            unsigned = dict(cycle)
            unsigned.pop("kernel_cache_fingerprint")
            cycle["kernel_cache_fingerprint"] = fingerprint(unsigned)
            write_json(cycle_path, cycle)
            head_before_dry_run = git(repository, "rev-parse", "HEAD")
            ledger_before_dry_run = ledger.path.read_bytes()
            completed_recovery_events = [
                event
                for event in ledger.read()
                if event["transaction_id"] == result["recovery_transaction_id"]
            ]

            dry_run = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual("cache_binding_recovery", dry_run["workflow_type"])
            self.assertEqual("paused", dry_run["current_state"])
            self.assertIsNone(dry_run["current_feature"])
            self.assertIsNone(dry_run["selected_feature"])
            self.assertEqual(0, dry_run["models_planned"])
            self.assertEqual(0, dry_run["child_sessions_planned"])
            self.assertEqual([], dry_run["model_sessions_that_would_launch"])
            self.assertEqual([], dry_run["child_sessions_that_would_launch"])
            self.assertEqual(
                head_before_dry_run,
                git(repository, "rev-parse", "HEAD"),
            )
            self.assertEqual(ledger_before_dry_run, ledger.path.read_bytes())
            self.assertEqual(
                completed_recovery_events,
                [
                    event
                    for event in ledger.read()
                    if event["transaction_id"] == result["recovery_transaction_id"]
                ],
            )

    def test_28_exact_failed_legacy_warning_recovery_is_retried_without_historical_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                engine,
                starting_head,
                ledger,
                warnings,
                failed_transaction,
                historical_gate,
                compatibility,
            ) = self._failed_legacy_recovery_fixture(Path(temporary))
            inventory = {
                "ok": True,
                "valid": True,
                "milestone_found": True,
                "active_milestone": "P0",
                "exit_code": 0,
                "errors": [],
                "warnings": warnings,
                "nonfatal_warnings": warnings,
                "blocking_warnings": [],
                "warning_count": len(warnings),
                "feature_count": 14,
                "global_feature_count": 14,
                "global_milestone_count": 1,
                "ready": ["P0-003"],
                "active": [],
                "validator": "synthetic inventory validator",
            }
            inspector = RepositoryInspector(repository)
            diff_fingerprint = inspector.planning_diff_fingerprint()
            status_before = git(
                repository, "status", "--porcelain=v1", "--branch"
            )
            ledger_before = ledger.path.read_bytes()
            projection_path = ledger.path.parent / "projection-cache.json"
            projection_before = projection_path.read_bytes()
            app_before = (repository / "app.txt").read_bytes()
            arguments = {
                "run_id": RUN_ID,
                "expected_starting_head": starting_head,
                "expected_diff_fingerprint": diff_fingerprint,
                "expected_changed_paths": SEVEN_PATHS,
                "expected_session_id": SESSION_ID,
            }
            with (
                patch.dict(
                    LEGACY_WARNING_SUMMARY_COMPATIBILITY,
                    compatibility,
                    clear=True,
                ),
                patch(
                    "development_conveyor.planning._inventory_validation",
                    return_value=inventory,
                ),
            ):
                dry = engine.recover_planning_transaction(
                    project, **arguments, dry_run=True
                )
                canonical = dry["planning_finalization_recovery"][
                    "queue_validation_evidence"
                ]["raw_structured"]
                self.assertNotIn("warnings", canonical)
                self.assertEqual(canonical["warning_count"], len(warnings))
                self.assertEqual(canonical["blocking_warnings"], [])
                self.assertEqual(
                    dry["planning_finalization_recovery"][
                        "failed_recovery_supersession"
                    ]["transaction_id"],
                    failed_transaction,
                )
                self.assertEqual(
                    git(repository, "status", "--porcelain=v1", "--branch"),
                    status_before,
                )
                self.assertEqual(ledger.path.read_bytes(), ledger_before)
                self.assertEqual(projection_path.read_bytes(), projection_before)
                consistency = ConsistencyChecker(
                    controller_root=engine.root,
                    project=project,
                    planner_observer=lambda: engine.project_plan(project),
                ).check()
                self.assertNotIn(
                    consistency["classification"],
                    {"UNSAFE_REPOSITORY_STATE", "HUMAN_DECISION_REQUIRED"},
                )
                result = engine.recover_planning_transaction(
                    project, **arguments, dry_run=False
                )
            self.assertEqual(result["outcome"], "planning_recovery_committed")
            self.assertEqual(result["selected_feature"], "P0-003")
            self.assertEqual(result["model_sessions_launched"], [])
            self.assertEqual(result["child_sessions_launched"], [])
            self.assertEqual(
                RepositoryInspector(repository).changed_paths(
                    result["planning_result_commit"]
                ),
                sorted(SEVEN_PATHS),
            )
            self.assertEqual(
                git(
                    repository,
                    "rev-list",
                    "--count",
                    f"{starting_head}..{result['planning_result_commit']}",
                ),
                "1",
            )
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertTrue(RepositoryInspector(repository).is_clean)
            projection = ProjectionEngine(
                ledger, projection_path
            ).rebuild(persist_cache=False)
            self.assertEqual(projection["current_state"], "feature_ready")
            self.assertEqual(projection["current_feature"], "P0-003")
            self.assertEqual(projection["selected_next_feature"], "P0-003")
            self.assertEqual(projection["allowed_next_action"], "feature_cycle")
            self.assertIsNone(projection["human_gate"])
            self.assertFalse(historical_gate.get("resolved", False))
            accounted = [
                event
                for event in ledger.read()
                if event["transaction_id"] == failed_transaction
                and event["event_type"] == "RecoveryApplied"
            ]
            self.assertEqual(
                accounted[0]["payload"]["classification"],
                "FAILED_LEGACY_WARNING_RECOVERY_SUPERSEDED",
            )


if __name__ == "__main__":
    unittest.main()
