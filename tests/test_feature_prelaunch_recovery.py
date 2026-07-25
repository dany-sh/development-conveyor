from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.contracts import MutationPolicy, TransactionState, WorkflowType
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.cycle_engine import CycleEngine
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
        self.assertFalse(
            (self.repository / ".factory/locks/writer.json").exists()
        )
