from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import WorkflowType, fingerprint
from development_conveyor.cycle_cache import write_terminal_cycle_cache
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository, write_json


class CacheBindingRecoveryTests(unittest.TestCase):
    def _fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration, SyntheticLauncher())
        inspector = RepositoryInspector(repository)
        inspector.ensure_runtime_ignored()
        state_root = configuration.root / "state/projects" / project.project_id
        identity = inspector.identity()
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl", project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        transaction_id = "terminal-cache-recovery"
        for event_type, payload in (
            ("TransactionStarted", {
                "run_id": "prior-recovery", "feature_id": "F001", "milestone": "M0",
                "starting_branch": project.milestone_branch, "starting_head": inspector.head,
                "allowed_mutation_policy": {},
            }),
            ("LeaseAcquired", {"lease_id": "prior-recovery-lease", "lease_type": "recovery_writer"}),
            ("SnapshotCaptured", {"snapshot": {"branch": inspector.current_branch, "head": inspector.head, "clean": True}}),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            ("TransactionCompleted", {
                "classification": "RECOVERY_APPLIED", "next_state": "feature_ready",
                "feature_id": "F001", "selected_feature": "F001",
                "terminal_snapshot": {"branch": inspector.current_branch, "head": inspector.head, "clean": True},
            }),
            ("LeaseReleased", {"lease_id": "prior-recovery-lease"}),
        ):
            ledger.append(event_type=event_type, transaction_id=transaction_id,
                          workflow_type=WorkflowType.RECOVERY, payload=payload)
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json").rebuild(persist_cache=True)
        feature = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))["features"][0]
        cycle = engine._new_cycle_state(project, "stale-f003-run", inspector, feature)
        cycle.update({
            "current_phase": "feature_ready", "current_feature": "F001", "selected_feature": "F001",
            "last_successful_checkpoint": "old-terminal", "feature_session_id": "stale-session",
        })
        cycle_path = inspector.cycle_state_path()
        write_terminal_cycle_cache(cycle_path, cycle, ledger=ledger, projection=projection,
                                   transaction_id=transaction_id, expected_feature="F001")
        corrupt = json.loads(cycle_path.read_text(encoding="utf-8"))
        corrupt["kernel_cache_fingerprint"] = "0" * 64
        write_json(cycle_path, corrupt)
        return repository, project, configuration, engine, transaction_id, cycle_path

    def test_corrupt_cache_routes_and_repairs_without_feature_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, engine, transaction_id, cycle_path = self._fixture(Path(temporary))
            before_head = git(repository, "rev-parse", "HEAD")
            before_branch = git(repository, "branch", "--show-current")
            before_queue = (repository / project.queue_location).read_bytes()
            plan = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual("cache_binding_recovery", plan["workflow_type"])
            self.assertEqual("recovery", plan["transaction_mode"])
            self.assertEqual("feature_ready", plan["current_state"])
            self.assertEqual("F001", plan["selected_feature"])
            self.assertEqual(transaction_id, plan["source_transaction"])
            self.assertEqual(0, plan["models_planned"])
            self.assertEqual(0, plan["child_sessions_planned"])
            self.assertEqual(0, plan["application_content_commits_planned"])
            self.assertFalse(plan["feature_branch_creation"])
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertEqual("ignored .factory/conveyor-state.json only", plan["application_mutation"])
            self.assertEqual("0" * 64, json.loads(cycle_path.read_text())["kernel_cache_fingerprint"])
            with mock.patch.object(engine, "_prepare_feature_branch", side_effect=AssertionError("branch preparation must not run")):
                result = engine.run_project(project, "resume")
            self.assertTrue(result["stopped_after_cache_repair"])
            self.assertEqual([], engine.launcher.actions)
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_branch, git(repository, "branch", "--show-current"))
            self.assertEqual(before_queue, (repository / project.queue_location).read_bytes())
            cache = json.loads(cycle_path.read_text(encoding="utf-8"))
            unsigned = dict(cache)
            claimed = unsigned.pop("kernel_cache_fingerprint")
            self.assertEqual(claimed, fingerprint(unsigned))
            self.assertEqual(transaction_id, cache["kernel_transaction_id"])
            self.assertEqual("F001", cache["current_feature"])
            self.assertEqual("feature_ready", cache["current_phase"])
            self.assertEqual("cache_binding_recovery_terminal", cache["last_successful_checkpoint"])
            consistency = ConsistencyChecker(
                controller_root=configuration.root, project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertEqual("CONSISTENT", consistency["classification"], consistency["failed_invariants"])
            subsequent = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual("feature_execution", subsequent["workflow_type"])
            self.assertEqual("feature_cycle", subsequent["proposed_next_action"])
