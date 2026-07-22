from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor import cycle_engine as cycle_engine_module
from development_conveyor import integration_executor as executor_module
from development_conveyor.errors import IntegrationPlanError, RepositoryError
from development_conveyor.integration_executor import (
    LEASE_IDENTITY_FIELDS,
    PLAN_REQUIRED_FIELDS,
    RUNTIME_DESCENDANTS,
    RUNTIME_PATTERN,
    load_integration_plan,
)
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import git, synthetic_repository, write_json
from tests.test_two_ref_integration_recovery import TwoRefIntegrationRecoveryTests

_fixture_builder = TwoRefIntegrationRecoveryTests(methodName="runTest")
del TwoRefIntegrationRecoveryTests


class DeterministicIntegrationHandoffTests(unittest.TestCase):
    def _fixture(self, root: Path):
        return _fixture_builder._fixture(root)

    def test_exact_plan_bypasses_live_ready_queue_and_adopts_one_controller_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository, project, _, engine, launcher, _, milestone_start,
                feature_branch, accepted, _,
            ) = self._fixture(Path(temporary))
            captured: dict[str, object] = {}
            real_execute = cycle_engine_module.execute_integration_plan
            real_acquire = WorkflowWriterLease.acquire
            real_release = WorkflowWriterLease.release
            real_adopt = WorkflowWriterLease.revalidate_adopted
            calls = {"acquire": 0, "release": 0, "adopt": 0}

            def execute_spy(path: Path):
                captured["path"] = Path(path)
                captured["plan"] = load_integration_plan(Path(path))
                return real_execute(Path(path))

            def acquire_spy(lease, *args, **kwargs):
                calls["acquire"] += 1
                return real_acquire(lease, *args, **kwargs)

            def release_spy(lease, *args, **kwargs):
                calls["release"] += 1
                return real_release(lease, *args, **kwargs)

            def adopt_spy(lease, *args, **kwargs):
                calls["adopt"] += 1
                return real_adopt(lease, *args, **kwargs)

            with mock.patch.object(
                cycle_engine_module, "execute_integration_plan", side_effect=execute_spy
            ), mock.patch.object(
                WorkflowWriterLease, "acquire", new=acquire_spy
            ), mock.patch.object(
                WorkflowWriterLease, "release", new=release_spy
            ), mock.patch.object(
                WorkflowWriterLease, "revalidate_adopted", new=adopt_spy
            ):
                result = engine.run_project(project, "milestone")

            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual([], launcher.requests)
            self.assertEqual({"acquire": 1, "release": 1}, {
                "acquire": calls["acquire"], "release": calls["release"]
            })
            self.assertGreaterEqual(calls["adopt"], 3)
            plan = captured["plan"]
            self.assertEqual(PLAN_REQUIRED_FIELDS, set(plan))
            self.assertEqual(accepted, plan["accepted_metadata_ref"])
            self.assertEqual(feature_branch, plan["lease_identity"]["feature_branch"])
            self.assertEqual(accepted, plan["lease_identity"]["accepted_commit"])
            self.assertEqual(set(LEASE_IDENTITY_FIELDS), set(plan["lease_identity"]))
            self.assertEqual(milestone_start, plan["pre_integration_head"])
            self.assertFalse(Path(captured["path"]).is_relative_to(repository))

    def test_common_git_runtime_exclusion_is_exact_idempotent_and_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            gitignore = repository / ".gitignore"
            gitignore.write_text("/tracked-only/\n", encoding="utf-8")
            git(repository, "add", ".gitignore")
            git(repository, "commit", "-m", "test: tracked ignore baseline")
            before = gitignore.read_bytes()

            first = inspector.ensure_milestone_integration_runtime_ignored()
            second = inspector.ensure_milestone_integration_runtime_ignored()

            exclude = inspector.common_git_dir / "info/exclude"
            lines = exclude.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(RUNTIME_PATTERN))
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(list(RUNTIME_DESCENDANTS), first["verified_descendants"])
            self.assertEqual(before, gitignore.read_bytes())
            for descendant in RUNTIME_DESCENDANTS:
                self.assertEqual(
                    0,
                    inspector.git(
                        ["check-ignore", "-q", "--no-index", "--", descendant],
                        check=False,
                    ).returncode,
                )

    def test_exclusion_failure_precedes_transaction_and_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, ledger, milestone_start, _, _, _ = (
                self._fixture(Path(temporary))
            )
            executable = engine._authoritative_execution_context(project)[1]
            ledger_before = ledger.path.read_bytes()
            with mock.patch.object(
                RepositoryInspector,
                "ensure_milestone_integration_runtime_ignored",
                side_effect=RepositoryError("synthetic exclusion failure"),
            ):
                with self.assertRaisesRegex(RepositoryError, "exclusion failure"):
                    engine._execute_projected_integration(
                        project, "milestone", "exclude-failure", executable
                    )
            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_mismatched_controller_lease_fails_before_application_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, milestone_start, _, _, _ = self._fixture(
                Path(temporary)
            )
            real_execute = cycle_engine_module.execute_integration_plan

            def replace_with_unrelated_lease(path: Path):
                plan = load_integration_plan(Path(path))
                lease_path = repository / ".factory/locks/writer.json"
                value = json.loads(lease_path.read_text(encoding="utf-8"))
                value["transaction_id"] = "unrelated-transaction"
                write_json(lease_path, value)
                return real_execute(Path(path))

            with mock.patch.object(
                cycle_engine_module,
                "execute_integration_plan",
                side_effect=replace_with_unrelated_lease,
            ):
                with self.assertRaisesRegex(
                    IntegrationPlanError, "lease identity mismatch"
                ):
                    engine.run_project(project, "milestone")
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertEqual("baseline\n", (repository / "app.txt").read_text(encoding="utf-8"))
            self.assertEqual(
                "unrelated-transaction",
                json.loads(
                    (repository / ".factory/locks/writer.json").read_text(encoding="utf-8")
                )["transaction_id"],
            )

    def test_feature_ref_race_is_detected_before_milestone_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, milestone_start, feature_branch, _, _ = (
                self._fixture(Path(temporary))
            )
            real_execute = cycle_engine_module.execute_integration_plan

            def advance_feature_ref(path: Path):
                git(repository, "switch", feature_branch)
                (repository / "race.txt").write_text("advanced\n", encoding="utf-8")
                git(repository, "add", "race.txt")
                git(repository, "commit", "-m", "test: feature ref race")
                git(repository, "switch", project.milestone_branch)
                return real_execute(Path(path))

            with mock.patch.object(
                cycle_engine_module,
                "execute_integration_plan",
                side_effect=advance_feature_ref,
            ):
                with self.assertRaisesRegex(
                    IntegrationPlanError, "feature branch HEAD"
                ):
                    engine.run_project(project, "milestone")
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertEqual("baseline\n", (repository / "app.txt").read_text(encoding="utf-8"))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_semantic_conflict_is_preserved_as_a_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, _, _, accepted, _ = self._fixture(
                Path(temporary)
            )
            real_mutation = executor_module._git_mutation

            def force_racing_conflict(root, plan, argv, **kwargs):
                if argv == ["cherry-pick", accepted]:
                    (repository / "app.txt").write_text(
                        "baseline\ndivergent milestone behavior\n", encoding="utf-8"
                    )
                    git(repository, "add", "app.txt")
                    git(repository, "commit", "-m", "test: racing semantic conflict")
                return real_mutation(root, plan, argv, **kwargs)

            with mock.patch.object(
                executor_module, "_git_mutation", side_effect=force_racing_conflict
            ):
                result = engine.run_project(project, "milestone")
            self.assertEqual("human_decision_required", result["outcome"])
            self.assertEqual("SEMANTIC_CONFLICT", result["classification"])
            self.assertFalse(result["model_session_launched"])
            self.assertTrue((repository / ".git/CHERRY_PICK_HEAD").exists())
            self.assertIn("app.txt", result["human_gate"]["conflicting_paths"])


if __name__ == "__main__":
    unittest.main()
