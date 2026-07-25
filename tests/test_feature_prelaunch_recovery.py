from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.contracts import (
    MutationPolicy,
    TransactionState,
    WorkflowType,
    WORKFLOW_LEASE,
)
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ProjectionError
from development_conveyor.feature_prelaunch_recovery import FeaturePrelaunchRecovery
from development_conveyor.kernel import FeatureExecutionAdapter, WorkflowKernel
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.logging import atomic_write_json
from development_conveyor.projection import ProjectionEngine
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import (
    controller_configuration,
    git,
    synthetic_repository,
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
