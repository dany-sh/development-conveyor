from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import WorkflowType, fingerprint
from development_conveyor.cycle_cache import (
    CanonicalProjectionBinding,
    _bind_terminal_cycle_cache,
    validated_canonical_projection_binding,
    write_terminal_cycle_cache,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine, projection_fingerprint
from development_conveyor.repository import RepositoryInspector
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository, write_json


class CacheBindingRecoveryTests(unittest.TestCase):
    def _fixture(self, root: Path, *, feature_id: str = "F001"):
        repository, project = synthetic_repository(root)
        if feature_id != "F001":
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0]["id"] = feature_id
            queue["features"][0]["title"] = f"Synthetic {feature_id}"
            queue["features"][0]["spec"] = f"docs/features/{feature_id}.md"
            write_json(queue_path, queue)
            (repository / "docs/features/F001.md").rename(
                repository / f"docs/features/{feature_id}.md"
            )
            git(repository, "add", ".")
            git(repository, "commit", "-m", f"test: select {feature_id}")
            project = replace(
                project, validated_baseline_commit=git(repository, "rev-parse", "HEAD")
            )
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
                "run_id": "prior-recovery", "feature_id": feature_id, "milestone": "M0",
                "starting_branch": project.milestone_branch, "starting_head": inspector.head,
                "allowed_mutation_policy": {},
            }),
            ("LeaseAcquired", {"lease_id": "prior-recovery-lease", "lease_type": "recovery_writer"}),
            ("SnapshotCaptured", {"snapshot": {"branch": inspector.current_branch, "head": inspector.head, "clean": True}}),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            ("TransactionCompleted", {
                "classification": "RECOVERY_APPLIED", "next_state": "feature_ready",
                "feature_id": feature_id, "selected_feature": feature_id,
                "terminal_snapshot": {"branch": inspector.current_branch, "head": inspector.head, "clean": True},
            }),
            ("LeaseReleased", {"lease_id": "prior-recovery-lease"}),
        ):
            ledger.append(event_type=event_type, transaction_id=transaction_id,
                          workflow_type=WorkflowType.RECOVERY, payload=payload)
        ProjectionEngine(ledger, state_root / "projection-cache.json").rebuild(
            persist_cache=True
        )
        feature = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))["features"][0]
        cycle = engine._new_cycle_state(project, "stale-f003-run", inspector, feature)
        cycle.update({
            "current_phase": "feature_ready", "current_feature": feature_id,
            "selected_feature": feature_id,
            "last_successful_checkpoint": "old-terminal", "feature_session_id": "stale-session",
        })
        cycle_path = inspector.cycle_state_path()
        write_terminal_cycle_cache(
            cycle_path, cycle, ledger=ledger,
            projection_engine=ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            ),
                                   transaction_id=transaction_id, expected_feature=feature_id)
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
            projection_cache = (
                configuration.root
                / "state/projects"
                / project.project_id
                / "projection-cache.json"
            )
            before_projection_cache = projection_cache.read_bytes()
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
            self.assertEqual(
                plan["canonical_projection_fingerprint"],
                plan["projection_fingerprint"],
            )
            self.assertNotEqual(
                plan["canonical_projection_fingerprint"],
                plan["observed_projection_fingerprint"],
            )
            self.assertNotEqual(
                plan["canonical_projection_fingerprint"],
                plan["queue_bound_projection_fingerprint"],
            )
            self.assertFalse(plan["canonical_cache_rebuild_required"])
            self.assertEqual("0" * 64, json.loads(cycle_path.read_text())["kernel_cache_fingerprint"])
            with mock.patch.object(engine, "_prepare_feature_branch", side_effect=AssertionError("branch preparation must not run")):
                result = engine.run_project(project, "resume")
            self.assertTrue(result["stopped_after_cache_repair"])
            self.assertEqual([], engine.launcher.actions)
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_branch, git(repository, "branch", "--show-current"))
            self.assertEqual(before_queue, (repository / project.queue_location).read_bytes())
            self.assertEqual(before_projection_cache, projection_cache.read_bytes())
            cache = json.loads(cycle_path.read_text(encoding="utf-8"))
            unsigned = dict(cache)
            claimed = unsigned.pop("kernel_cache_fingerprint")
            self.assertEqual(claimed, fingerprint(unsigned))
            self.assertEqual(transaction_id, cache["kernel_transaction_id"])
            self.assertEqual("F001", cache["current_feature"])
            self.assertEqual("feature_ready", cache["current_phase"])
            self.assertEqual(
                plan["canonical_projection_fingerprint"],
                cache["kernel_projection_fingerprint"],
            )
            self.assertNotEqual(
                plan["observed_projection_fingerprint"],
                cache["kernel_projection_fingerprint"],
            )
            self.assertNotEqual(
                plan["queue_bound_projection_fingerprint"],
                cache["kernel_projection_fingerprint"],
            )
            self.assertEqual("cache_binding_recovery_terminal", cache["last_successful_checkpoint"])
            consistency = ConsistencyChecker(
                controller_root=configuration.root, project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertEqual("CONSISTENT", consistency["classification"], consistency["failed_invariants"])
            subsequent = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual("feature_execution", subsequent["workflow_type"])
            self.assertEqual("feature_cycle", subsequent["proposed_next_action"])

    def test_unchanged_canonical_rebuild_is_stable_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, _, transaction_id, _ = self._fixture(
                Path(temporary)
            )
            state_root = configuration.root / "state/projects" / project.project_id
            inspector = RepositoryInspector(repository)
            identity = inspector.identity()
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            projection_engine = ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            )
            before = projection_engine.cache_path.read_bytes()
            first = projection_engine.rebuild(persist_cache=False)
            second = projection_engine.rebuild(persist_cache=False)
            binding = validated_canonical_projection_binding(
                ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=transaction_id,
            )
            self.assertEqual(first, second)
            self.assertEqual(first, binding.canonical_projection)
            self.assertEqual(before, projection_engine.cache_path.read_bytes())

    def test_successful_f004_recovery_is_consistent_without_dispatch_or_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, engine, _, cycle_path = self._fixture(
                Path(temporary), feature_id="F004"
            )
            starting_head = git(repository, "rev-parse", "HEAD")
            plan = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual("F004", plan["selected_feature"])
            result = engine.run_project(project, "resume")
            self.assertEqual("feature_ready", result["current_state"])
            self.assertEqual("F004", result["selected_feature"])
            self.assertEqual(0, result["models_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertFalse(result["feature_branch_created"])
            self.assertEqual([], engine.launcher.actions)
            self.assertEqual(starting_head, git(repository, "rev-parse", "HEAD"))
            self.assertNotIn("codex/F004", git(repository, "branch", "--list"))
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
            self.assertEqual("feature_ready", cycle["current_phase"])
            self.assertEqual("F004", cycle["current_feature"])
            consistency = ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertEqual(
                "CONSISTENT", consistency["classification"],
                consistency["failed_invariants"],
            )

    def test_stale_canonical_cache_is_rebuilt_before_cycle_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, engine, _, cycle_path = self._fixture(
                Path(temporary)
            )
            projection_path = (
                configuration.root
                / "state/projects"
                / project.project_id
                / "projection-cache.json"
            )
            stale = json.loads(projection_path.read_text(encoding="utf-8"))
            stale["ledger_sequence"] -= 1
            stale["ledger_fingerprint"] = "stale-ledger-fingerprint"
            stale["projection_fingerprint"] = projection_fingerprint(stale)
            write_json(projection_path, stale)

            plan = engine.run_project(project, "resume", dry_run=True)
            self.assertTrue(plan["canonical_cache_rebuild_required"])
            with mock.patch.object(
                engine,
                "_prepare_feature_branch",
                side_effect=AssertionError("feature dispatch must not run"),
            ):
                result = engine.run_project(project, "resume")
            self.assertTrue(result["stopped_after_cache_repair"])
            self.assertEqual([], engine.launcher.actions)
            rebuilt = json.loads(projection_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["ledger_sequence"], rebuilt["ledger_sequence"])
            self.assertEqual(
                plan["canonical_projection_fingerprint"],
                rebuilt["projection_fingerprint"],
            )
            cache = json.loads(cycle_path.read_text(encoding="utf-8"))
            self.assertEqual(
                rebuilt["projection_fingerprint"],
                cache["kernel_projection_fingerprint"],
            )

    def test_corrupt_canonical_projection_or_ledger_binding_blocks_write(self):
        for mismatch in ("projection", "ledger"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                repository, project, configuration, _, transaction_id, cycle_path = self._fixture(
                    Path(temporary)
                )
                state_root = configuration.root / "state/projects" / project.project_id
                projection_path = state_root / "projection-cache.json"
                cached = json.loads(projection_path.read_text(encoding="utf-8"))
                if mismatch == "projection":
                    cached["warnings"] = ["semantically divergent cache"]
                else:
                    cached["ledger_fingerprint"] = "0" * 64
                cached["projection_fingerprint"] = projection_fingerprint(cached)
                write_json(projection_path, cached)
                before_cycle = cycle_path.read_bytes()
                inspector = RepositoryInspector(repository)
                identity = inspector.identity()
                ledger = EvidenceLedger(
                    state_root / "evidence-ledger.jsonl",
                    project_id=project.project_id,
                    repository_identity=identity["repository_id"],
                    repository_path_fingerprint=identity["path_fingerprint"],
                )
                state = json.loads(cycle_path.read_text(encoding="utf-8"))
                state["kernel_cache_fingerprint"] = "0" * 64
                with self.assertRaises(RecoveryError):
                    write_terminal_cycle_cache(
                        cycle_path,
                        state,
                        ledger=ledger,
                        projection_engine=ProjectionEngine(ledger, projection_path),
                        transaction_id=transaction_id,
                        expected_feature="F001",
                    )
                self.assertEqual(before_cycle, cycle_path.read_bytes())

    def test_current_topology_constants_bind_dfc_not_ephemeral_7e(self):
        canonical_fingerprint = (
            "dfc1c4ff9e2ebb5ea75c6f2721c9a970dc4fee622a258bc5c41d19ea453a5fb8"
        )
        ephemeral_fingerprint = (
            "7e17ae8e2a9996b944a8724cc25f07162aa70a52c094f06dc71e17249db791e1"
        )
        binding = CanonicalProjectionBinding(
            ledger_sequence=166,
            ledger_fingerprint=(
                "729a9a2bf40a7f58c749e1e1739e77eaf56a08a50969fc35bdbabc97360216e8"
            ),
            projection_fingerprint=canonical_fingerprint,
            canonical_projection={
                "current_state": "feature_ready",
                "current_feature": "F004",
                "selected_next_feature": "F004",
            },
        )
        ledger = mock.Mock()
        ledger.terminal_event.return_value = {
            "event_type": "TransactionCompleted",
            "payload": {"selected_feature": "F004"},
        }
        finalized = _bind_terminal_cycle_cache(
            {"current_phase": "feature_ready", "current_feature": "F004"},
            ledger=ledger,
            binding=binding,
            transaction_id="11a0e09a-9f38-4207-9665-3d202feba2cc",
            expected_feature="F004",
        )
        self.assertEqual(
            canonical_fingerprint, finalized["kernel_projection_fingerprint"]
        )
        self.assertNotEqual(
            ephemeral_fingerprint, finalized["kernel_projection_fingerprint"]
        )

    def test_recovery_and_consistency_share_canonical_binding_helper(self):
        import development_conveyor.consistency as consistency_module
        import development_conveyor.cycle_engine as cycle_engine_module

        self.assertIs(
            cycle_engine_module.validated_canonical_projection_binding,
            consistency_module.validated_canonical_projection_binding,
        )
