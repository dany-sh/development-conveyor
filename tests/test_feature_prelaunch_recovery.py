from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.contracts import (
    MutationPolicy,
    TransactionState,
    WorkflowType,
    WORKFLOW_LEASE,
)
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ProjectionError, RecoveryError
from development_conveyor.feature_prelaunch_recovery import FeaturePrelaunchRecovery
from development_conveyor.kernel import FeatureExecutionAdapter, WorkflowKernel
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.logging import atomic_write_json
from development_conveyor.projection import ProjectionEngine
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import (
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
)

class InvalidStructuredFeatureLauncher(SyntheticLauncher):
    def launch(self, request, on_session_started=None):
        result = super().launch(
            request, on_session_started=on_session_started
        )
        if request.action != "feature_cycle":
            return result
        return replace(
            result,
            structured_result=None,
            transaction_envelope=None,
            structured_output_validation="invalid",
            result_classification="structured_output_invalid",
            exit_classification="structured_output_invalid",
            failure_classification="structured_output_invalid",
            terminal_marker_found=True,
            structured_output_errors=("synthetic invalid envelope",),
        )


class FeaturePrelaunchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository, self.project = synthetic_repository(self.root)
        self.configuration = controller_configuration(self.root, self.project)
        queue_path = self.repository / self.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0]["branch"] = "codex/F001-synthetic-feature"
        atomic_write_json(queue_path, queue)
        git(self.repository, "add", self.project.queue_location)
        git(self.repository, "commit", "-m", "plan F001")
        self.head = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "branch", "codex/F001-synthetic-feature", self.head)
        git(self.repository, "switch", "codex/F001-synthetic-feature")

        identity = RepositoryInspector(self.repository).identity()
        state_root = (
            self.configuration.root / "state/projects" / self.project.project_id
        )
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        queue_fingerprint = hashlib.sha256(queue_path.read_bytes()).hexdigest()
        preparation_id = "deterministic-feature-preparation"
        starting_snapshot = {
            "branch": self.project.milestone_branch,
            "head": self.head,
            "clean": True,
            "git_operations": {
                "merge": False,
                "cherry_pick": False,
                "rebase_apply": False,
                "rebase_merge": False,
            },
            "queue_fingerprint": queue_fingerprint,
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
        }
        terminal_snapshot = {
            **starting_snapshot,
            "branch": "codex/F001-synthetic-feature",
        }
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": "deterministic-preparation-run",
                    "feature_id": "F001",
                    "milestone": "M0",
                    "starting_branch": self.project.milestone_branch,
                    "starting_head": self.head,
                    "allowed_mutation_policy": {},
                },
            ),
            (
                "LeaseAcquired",
                {"lease_id": "deterministic-preparation-lease"},
            ),
            ("SnapshotCaptured", {"snapshot": starting_snapshot}),
            (
                "SessionLaunched",
                {"session_id": f"deterministic-feature-preparation:{preparation_id}"},
            ),
            ("ValidationStarted", {"changed_paths": []}),
            ("ValidationPassed", {"commands": []}),
            (
                "TransactionCompleted",
                {
                    "classification": "FEATURE_PREPARED",
                    "feature_id": "F001",
                    "next_state": "feature_preparing",
                    "terminal_snapshot": terminal_snapshot,
                },
            ),
            (
                "LeaseReleased",
                {"lease_id": "deterministic-preparation-lease"},
            ),
            (
                "ProjectionUpdated",
                {
                    "current_state": "feature_preparing",
                    "current_feature": "F001",
                },
            ),
        ):
            ledger.append(
                event_type=event_type,
                transaction_id=preparation_id,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload=payload,
            )
        projection = ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        )
        adapter = FeatureExecutionAdapter(
            allowed_paths=(),
            commit_subject="F001: Synthetic Feature",
            next_state="feature_accepted",
        )
        kernel = WorkflowKernel(
            project=self.project,
            ledger=ledger,
            projection=projection,
            lease=WorkflowWriterLease(
                self.repository / ".factory/locks/writer.json"
            ),
        )
        transaction = kernel.begin(
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            milestone="M0",
            feature_id="F001",
            run_id="inner-feature-run",
            policy=MutationPolicy(()),
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification="FEATURE_PRELAUNCH_CONTEXT_FAILED",
            next_state="feature_preparing",
            reference="ContextReadError",
        )
        self.failed_transaction = transaction.transaction_id
        self.autopilot_run = "autopilot-prelaunch-run"
        atomic_write_json(
            self.configuration.root
            / "reports/autopilot/synthetic/latest.json",
            {
                "schema_version": 1,
                "project": "synthetic",
                "run_id": self.autopilot_run,
                "terminal_classification": "AUTOPILOT_FAILED",
                "events": [
                    {"event": "FEATURE_CONTEXT_STARTED", "diagnostic": "context"}
                ],
            },
        )
        engine = CycleEngine(self.configuration)
        selection = FeatureQueue.from_location(
            self.repository, self.project.queue_location
        ).select_next("M0")
        state = engine._new_cycle_state(
            self.project,
            "inner-feature-run",
            RepositoryInspector(self.repository),
            selection.feature,
            compatibility=None,
        )
        state.update({
            "current_phase": "feature_prelaunch_failed",
            "feature_branch": "codex/F001-synthetic-feature",
            "feature_starting_commit": self.head,
            "milestone_pre_integration_commit": self.head,
            "last_successful_checkpoint": "feature_context_failed",
        })
        (self.repository / ".git/info/exclude").write_text(
            ".factory/conveyor-state.json\n.factory/locks/writer.json\n",
            encoding="utf-8",
        )
        atomic_write_json(
            self.repository / ".factory/conveyor-state.json", state
        )

    def tearDown(self):
        self.temporary.cleanup()

    def recovery(self):
        return FeaturePrelaunchRecovery(
            controller_root=self.configuration.root,
            configuration=self.configuration,
            project=self.project,
        )

    def _append_capability_isolation_prelaunch_failure(self):
        state_root = (
            self.configuration.root / "state/projects" / self.project.project_id
        )
        identity = RepositoryInspector(self.repository).identity()
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        run_id = "capability-isolation-prelaunch-run"
        transaction_id = "capability-isolation-prelaunch-transaction"
        snapshot = {
            "branch": "codex/F001-synthetic-feature",
            "head": self.head,
            "clean": True,
            "git_operations": {
                "merge": False,
                "cherry_pick": False,
                "rebase_apply": False,
                "rebase_merge": False,
            },
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
        }
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": run_id,
                    "feature_id": "F001",
                    "milestone": "M0",
                    "starting_branch": snapshot["branch"],
                    "starting_head": snapshot["head"],
                    "allowed_mutation_policy": {},
                },
            ),
            ("LeaseAcquired", {"lease_id": "capability-isolation-lease"}),
            ("SnapshotCaptured", {"snapshot": snapshot}),
            (
                "TransactionBlocked",
                {
                    "classification": "FEATURE_VALIDATION_FAILED",
                    "next_state": "validation_failed",
                    "reference": "SessionError",
                    "terminal_state": "terminal_failure",
                    "terminal_snapshot": snapshot,
                },
            ),
            ("LeaseReleased", {"lease_id": "capability-isolation-lease"}),
            (
                "ProjectionUpdated",
                {
                    "current_state": "validation_failed",
                    "current_feature": "F001",
                    "selected_feature": None,
                },
            ),
        ):
            ledger.append(
                event_type=event_type,
                transaction_id=transaction_id,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )
        ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        ).rebuild(persist_cache=True)
        atomic_write_json(
            self.configuration.root
            / f"reports/{run_id}/feature_cycle-launch-failure.json",
            {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "run_id": run_id,
                "action": "feature_cycle",
                "failure_classification": "session_execution_failed",
                "exit_classification": "session_execution_failed",
                "result_classification": "session_execution_failed",
                "exit_status": None,
                "argv": [],
                "session_id": None,
                "launched_model": None,
                "terminal_marker_found": False,
                "context_pack_evidence": None,
                "context_read_failure": None,
                "structured_result": None,
                "parsed_structured_result": None,
                "redacted_stderr": (
                    "capability_isolation_unsupported: missing requested=example"
                ),
                "working_directory": str(self.repository),
            },
        )
        return run_id

    def test_run_scoped_capability_failure_routes_to_prelaunch_recovery(self):
        run_id = self._append_capability_isolation_prelaunch_failure()
        engine = CycleEngine(self.configuration)
        projected = engine.project_plan(self.project)
        self.assertEqual(
            projected["proposed_next_action"], "feature_prelaunch_recovery"
        )
        evidence = projected["feature_prelaunch_recovery"]
        self.assertEqual(evidence["autopilot_run_id"], run_id)
        self.assertEqual(
            evidence["failure_report_kind"], "run_scoped_launch_failure"
        )
        result = self.recovery().apply(evidence)
        self.assertEqual(result["outcome"], "prelaunch_recovery_applied")
        self.assertEqual(
            result["kernel_projection"]["allowed_next_action"], "feature_cycle"
        )

    def test_run_scoped_capability_failure_rejects_session_or_report_drift(self):
        run_id = self._append_capability_isolation_prelaunch_failure()
        report_path = (
            self.configuration.root
            / f"reports/{run_id}/feature_cycle-launch-failure.json"
        )
        baseline = json.loads(report_path.read_text(encoding="utf-8"))
        mutations = {
            "argv": {"argv": ["codex", "exec"]},
            "session": {"session_id": "session-was-launched"},
            "model": {"launched_model": "gpt-5.6-terra"},
            "terminal": {"terminal_marker_found": True},
            "classification": {"failure_classification": "process_failed"},
            "capability_error": {"redacted_stderr": "unrelated failure"},
            "repository": {"working_directory": str(self.root / "other")},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                atomic_write_json(report_path, {**baseline, **mutation})
                with self.assertRaisesRegex(
                    RecoveryError, "prelaunch_failure"
                ):
                    self.recovery().inspect(
                        feature_id="F001",
                        autopilot_run_id=run_id,
                        expected_branch="codex/F001-synthetic-feature",
                        expected_head=self.head,
                    )
        atomic_write_json(report_path, baseline)

    def test_dry_run_authenticates_and_writes_nothing(self):
        before = {
            path: path.read_bytes()
            for path in (
                self.configuration.root
                / "state/projects/synthetic"
            ).glob("*")
            if path.is_file()
        }
        plan = self.recovery().inspect(
            feature_id="F001",
            autopilot_run_id=self.autopilot_run,
            expected_branch="codex/F001-synthetic-feature",
            expected_head=self.head,
        )
        after = {path: path.read_bytes() for path in before}
        self.assertEqual(before, after)
        self.assertEqual(plan["model_sessions_that_would_launch"], 0)
        self.assertEqual(plan["implementation_attempts_consumed"], 0)
        self.assertEqual(plan["feature_commits_that_would_be_created"], 0)

    def test_apply_refreshes_bindings_and_preserves_prepared_feature(self):
        plan = self.recovery().inspect(
            feature_id="F001",
            autopilot_run_id=self.autopilot_run,
            expected_branch="codex/F001-synthetic-feature",
            expected_head=self.head,
        )
        result = self.recovery().apply(plan)
        projection = result["kernel_projection"]
        self.assertEqual(result["model_sessions_launched"], 0)
        self.assertEqual(result["child_sessions_launched"], 0)
        self.assertEqual(projection["current_state"], "feature_preparing")
        self.assertEqual(projection["current_feature"], "F001")
        self.assertEqual(projection["allowed_next_action"], "feature_cycle")
        engine = CycleEngine(self.configuration)
        retry_plan = engine.project_plan(self.project)
        self.assertEqual(
            retry_plan["proposed_next_action"], "feature_cycle"
        )
        consistency = ConsistencyChecker(
            controller_root=self.configuration.root,
            project=self.project,
            planner_observer=lambda: engine.project_plan(self.project),
        ).check()
        self.assertEqual(consistency["classification"], "CONSISTENT")
        self.assertEqual(
            git(self.repository, "rev-parse", "HEAD"), self.head
        )
        self.assertEqual(
            git(self.repository, "branch", "--show-current"),
            "codex/F001-synthetic-feature",
        )
        cycle = json.loads(
            (self.repository / ".factory/conveyor-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            cycle["last_verified_git_state"]["branch"],
            "codex/F001-synthetic-feature",
        )
        self.assertEqual(cycle["last_verified_git_state"]["head"], self.head)
        self.assertEqual(
            cycle["feature_branch"], "codex/F001-synthetic-feature"
        )
        self.assertEqual(cycle["feature_starting_commit"], self.head)
        self.assertEqual(cycle["milestone_branch"], self.project.milestone_branch)
        self.assertFalse(
            (self.repository / ".factory/locks/writer.json").exists()
        )

    def _apply_and_engine(self):
        plan = self.recovery().inspect(
            feature_id="F001",
            autopilot_run_id=self.autopilot_run,
            expected_branch="codex/F001-synthetic-feature",
            expected_head=self.head,
        )
        self.recovery().apply(plan)
        return CycleEngine(self.configuration)

    def test_prepared_feature_plan_binds_distinct_feature_and_milestone_roles(self):
        engine = self._apply_and_engine()
        projected = engine.project_plan(self.project)
        execution = projected["execution_plan"]
        self.assertEqual(execution["current_state"], "feature_preparing")
        self.assertEqual(
            execution["starting_branch"], "codex/F001-synthetic-feature"
        )
        self.assertEqual(execution["starting_commit"], self.head)
        self.assertEqual(
            execution["feature_branch"], "codex/F001-synthetic-feature"
        )
        self.assertEqual(
            execution["milestone_branch"], self.project.milestone_branch
        )
        validated = engine._validate_projected_dispatch(
            self.project,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            expected=None,
        )
        self.assertIsNotNone(validated)

    def test_authenticated_session_replaces_stale_preparing_transition_source(self):
        plan = self.recovery().inspect(
            feature_id="F001",
            autopilot_run_id=self.autopilot_run,
            expected_branch="codex/F001-synthetic-feature",
            expected_head=self.head,
        )
        self.recovery().apply(plan)
        engine = CycleEngine(
            self.configuration, InvalidStructuredFeatureLauncher()
        )
        result = engine.run_project(self.project, "one_feature")
        self.assertEqual("human_decision_required", result["outcome"])
        self.assertEqual(
            "structured_output_invalid",
            result["retained_result"]["result_classification"],
        )
        self.assertTrue(result["implementation_preserved"])
        run_events = [
            json.loads(line)
            for line in engine.events.path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        transitions = [
            (
                event.get("previous_state"),
                event.get("next_state"),
                event.get("result"),
            )
            for event in run_events
        ]
        self.assertIn(
            (
                "feature_preparing",
                "feature_running",
                "authenticated_feature_session_launched",
            ),
            transitions,
        )
        self.assertIn(
            (
                "feature_running",
                "human_decision_required",
                "session_terminal_failure",
            ),
            transitions,
        )
        ledger_path = (
            self.configuration.root
            / "state/projects/synthetic/evidence-ledger.jsonl"
        )
        events = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        feature_transactions = [
            event["transaction_id"]
            for event in events
            if event["event_type"] == "SessionLaunched"
            and event["workflow_type"] == "feature_execution"
        ]
        terminal = [
            event
            for event in events
            if event["transaction_id"] == feature_transactions[-1]
            and event["event_type"] == "HumanGateRaised"
        ]
        self.assertEqual(1, len(terminal))
        self.assertFalse(
            (self.repository / ".factory/locks/writer.json").exists()
        )

    def test_prepared_feature_rejects_same_commit_on_milestone_branch(self):
        engine = self._apply_and_engine()
        git(self.repository, "switch", self.project.milestone_branch)
        with self.assertRaisesRegex(
            ProjectionError,
            "repository no longer matches the execution plan starting branch and commit",
        ):
            engine._validate_projected_dispatch(
                self.project,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                expected=None,
            )

    def test_prepared_feature_rejects_correct_branch_at_wrong_commit(self):
        engine = self._apply_and_engine()
        git(self.repository, "commit", "--allow-empty", "-m", "synthetic drift")
        with self.assertRaisesRegex(
            ProjectionError,
            "repository no longer matches the execution plan starting branch and commit",
        ):
            engine._validate_projected_dispatch(
                self.project,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                expected=None,
            )

    def test_prepared_feature_rejects_dirty_repository_and_writer_lease(self):
        engine = self._apply_and_engine()
        app = self.repository / "app.txt"
        app.write_text(app.read_text(encoding="utf-8") + "dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectionError, "requires a clean repository"):
            engine._validate_projected_dispatch(
                self.project,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                expected=None,
            )
        git(self.repository, "restore", "app.txt")

        inspector = RepositoryInspector(self.repository)
        identity = inspector.identity()
        lease = WorkflowWriterLease(
            self.repository / ".factory/locks/writer.json"
        )
        transaction_id = "active-prepared-feature-test"
        lease.acquire(
            lease_type=WORKFLOW_LEASE[WorkflowType.FEATURE_EXECUTION],
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            project_id=self.project.project_id,
            transaction_id=transaction_id,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            milestone=self.project.active_milestone,
            feature_id="F001",
            starting_branch=inspector.current_branch,
            starting_head=inspector.head,
            run_id="active-prepared-feature-test",
            session_id=None,
            policy=MutationPolicy(()),
        )
        try:
            with self.assertRaisesRegex(ProjectionError, "writer lease"):
                engine._validate_projected_dispatch(
                    self.project,
                    workflow_type=WorkflowType.FEATURE_EXECUTION,
                    expected=None,
                )
        finally:
            lease.release(
                transaction_id=transaction_id,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                repository_identity=identity["repository_id"],
                project_id=self.project.project_id,
            )
