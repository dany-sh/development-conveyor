from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import MutationPolicy, WorkflowType
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.execution_plan import (
    ACTION_WORKFLOW,
    ExecutionPlan,
    authoritative_status_fields,
    bind_projection_to_queue,
    execution_plan_projection_agreement,
    integrated_feature_execution_checks,
)
from development_conveyor.errors import ProjectionError, TransactionError
from development_conveyor.migration import LegacyStateMigrator
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine, cache_agrees, projection_fingerprint
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionLauncher
from tests.helpers import (
    REPOSITORY_ROOT,
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


class ProjectionAuthorityTests(unittest.TestCase):
    def _integration_fixture(
        self, root: Path, *, next_feature_status: str | None = None
    ):
        repository, project = synthetic_repository(root)
        if next_feature_status is not None:
            (repository / "docs/features/F002.md").write_text(
                "# F002\n\nAdd a distinct synthetic feature.\n", encoding="utf-8"
            )
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"].append(
                {
                    "id": "F002",
                    "title": "Distinct next feature",
                    "status": next_feature_status,
                    "priority": 2,
                    "milestone": "M0",
                    "dependencies": ["F001"],
                    "spec": "docs/features/F002.md",
                    "acceptance_criteria": ["app.txt records F002"],
                    "requires_human_decision": False,
                    "branch": "codex/f002-distinct",
                    "integration_base_commit": None,
                    "accepted_commit": None,
                    "integrated_commit": None,
                    "integration_status": "pending",
                    "integration_fix_commits": [],
                }
            )
            write_json(queue_path, queue)
            git(repository, "add", "docs/features/F002.md", project.queue_location)
            git(repository, "commit", "-m", "docs: register future F002")
        milestone_start = git(repository, "rev-parse", "HEAD")
        feature_branch = "codex/f001-authoritative"
        git(repository, "switch", "-c", feature_branch)
        (repository / "app.txt").write_text(
            "baseline\nF001 accepted behavior\n", encoding="utf-8"
        )
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integration_pending",
            "implementation_status": "Completed",
            "branch": feature_branch,
            "integration_base_commit": milestone_start,
            "accepted_commit": "SELF",
            "integration_status": "pending",
            "acceptance": {
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": True,
            },
        })
        write_json(queue_path, queue)
        git(repository, "add", "app.txt", project.queue_location)
        git(repository, "commit", "-m", "F001: accepted behavior")
        accepted = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", project.milestone_branch)

        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration, SyntheticLauncher())
        inspector = RepositoryInspector(repository)
        inspector.ensure_runtime_ignored()
        cycle = engine._new_cycle_state(
            project, "legacy-feature-run", inspector, queue["features"][0]
        )
        cycle.update({
            "current_phase": "branch_preparing",
            "last_successful_checkpoint": "feature_session_incomplete",
            "feature_session_id": "legacy-session",
            "session_id": "legacy-session",
        })
        engine.cycle_store.write(inspector.cycle_state_path(), cycle)
        compatibility = engine._project_document(
            project, "legacy-feature-run", inspector.identity()["path_fingerprint"]
        )
        compatibility.update({"current_state": "feature_running", "current_feature": "F001"})
        engine.project_store.write(engine.project_state_path(project), compatibility)
        migration = LegacyStateMigrator(
            controller_root=configuration.root, project=project
        ).apply()
        self.assertTrue(migration["migration_applied"])
        state_root = configuration.root / "state/projects/synthetic"
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            repository_path_fingerprint=inspector.identity()["path_fingerprint"],
        )
        failed_transaction = "failed-two-ref-integration"
        failed_run = "failed-two-ref-run"
        workflow = WorkflowType.MILESTONE_INTEGRATION
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": failed_run,
                    "feature_id": "F001",
                    "milestone": "M0",
                    "starting_branch": project.milestone_branch,
                    "starting_head": milestone_start,
                    "allowed_mutation_policy": {},
                },
            ),
            (
                "LeaseAcquired",
                {"lease_id": "old-integration-lease", "lease_type": "integration_writer"},
            ),
            (
                "SnapshotCaptured",
                {"snapshot": {"branch": project.milestone_branch, "head": milestone_start}},
            ),
            ("SessionLaunched", {"session_id": "legacy-session"}),
            ("ValidationStarted", {}),
            ("ValidationFailed", {"diagnostic": "synthetic pre-mutation failure"}),
            (
                "TransactionBlocked",
                {
                    "classification": "VALIDATION_FAILED",
                    "reference": "SessionError",
                    "terminal_state": "terminal_failure",
                    "next_state": "validation_failed",
                    "terminal_snapshot": {
                        "branch": project.milestone_branch,
                        "head": milestone_start,
                        "clean": True,
                    },
                },
            ),
            ("LeaseReleased", {"lease_id": "old-integration-lease"}),
        ):
            ledger.append(
                event_type=event_type,
                transaction_id=failed_transaction,
                workflow_type=workflow,
                payload=payload,
            )
        ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        ).rebuild(persist_cache=True)
        write_json(
            configuration.root / f"reports/{failed_run}/milestone_integration.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": failed_run,
                "action": "milestone_integration",
                "working_directory": str(repository),
                "session_id": "legacy-session",
                "accepted_commit": accepted,
                "terminal_marker_found": True,
                "structured_output_validation": "semantic_invalid",
                "result_classification": "structured_output_invalid",
                "post_integration_commands": [
                    {"category": "status_observation", "exit_code": 0}
                ],
            },
        )
        return repository, project, configuration, engine, accepted, inspector.cycle_state_path()

    def _completed_integration_fixture(
        self, root: Path, *, next_feature_status: str = "proposed"
    ):
        fixture = self._integration_fixture(
            root, next_feature_status=next_feature_status
        )
        repository, project, configuration, engine, accepted, cycle_path = fixture
        result = engine.run_project(project, "milestone")
        self.assertEqual("feature_integrated", result["outcome"])
        terminal = git(repository, "rev-parse", "HEAD")
        integrating = git(repository, "rev-parse", "HEAD^")
        integrated = str(result["integrated_commit"])
        return (
            repository,
            project,
            configuration,
            engine,
            accepted,
            integrated,
            integrating,
            terminal,
            cycle_path,
        )

    def test_valid_projection_overrides_stale_project_and_repository_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, accepted, cycle_path = self._integration_fixture(
                Path(temporary)
            )
            cycle_before = cycle_path.read_bytes()
            plan = engine.project_plan(project)
            self.assertEqual("integration_ready", plan["current_state"])
            self.assertEqual("integration_ready", plan["derived_state"])
            self.assertEqual("integration_ready", plan["persisted_state"])
            self.assertEqual("milestone_integration", plan["next_action"])
            self.assertEqual("milestone_integration", plan["proposed_next_action"])
            self.assertEqual("milestone_integration", plan["workflow_type"])
            self.assertEqual("fresh", plan["transaction_mode"])
            self.assertEqual("integration_writer", plan["required_lease"])
            self.assertEqual("F001", plan["selected_feature"])
            self.assertEqual(accepted, plan["accepted_feature_commit"])
            self.assertIsNone(plan["existing_active_cycle"])
            self.assertFalse(plan["old_session_will_resume"])
            self.assertFalse(plan["session_resume_eligible"])
            self.assertEqual([], plan["sessions_that_would_launch"])
            self.assertEqual("integration_writer", plan["execution_plan"]["lease_type"])
            self.assertEqual("fresh", plan["execution_plan"]["transaction_mode"])
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertFalse(plan["milestone_integrator_would_launch"])
            contract = plan["milestone_integration_contract"]
            self.assertEqual(
                "scripts/conveyor execute-integration-plan --plan <absolute-controller-owned-plan-path>",
                contract["mutation_command"],
            )
            self.assertFalse(contract["model_session_required"])
            self.assertEqual(accepted, contract["accepted_commit"])
            self.assertEqual("fresh", contract["transaction_mode"])
            self.assertFalse(plan["old_session_will_resume"])
            self.assertIsNotNone(plan["legacy_observations"]["legacy_existing_cycle"])
            self.assertEqual(cycle_before, cycle_path.read_bytes())
            self.assertTrue(RepositoryInspector(repository).is_clean)

    def test_projection_dispatch_never_resumes_superseded_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, _ = self._integration_fixture(Path(temporary))
            with mock.patch.object(
                engine,
                "_execute_projected_integration",
                return_value={"outcome": "routed"},
            ) as integration, mock.patch.object(
                engine,
                "resume_project",
                side_effect=AssertionError("superseded legacy session must not resume"),
            ):
                result = engine.run_project(project, "milestone")
            self.assertEqual("routed", result["outcome"])
            integration.assert_called_once()

    def test_completed_integration_clears_terminal_feature_from_empty_queue_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                configuration,
                engine,
                accepted,
                integrated,
                integrating,
                terminal,
                cycle_path,
            ) = self._completed_integration_fixture(Path(temporary))
            ledger_path = (
                configuration.root
                / "state/projects/synthetic/evidence-ledger.jsonl"
            )
            before = (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "rev-parse", "HEAD^{tree}"),
                git(repository, "status", "--porcelain=v1", "-uall"),
                ledger_path.read_bytes(),
                cycle_path.read_bytes(),
            )

            plan = engine.run_project(project, "resume", dry_run=True)

            after = (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "rev-parse", "HEAD^{tree}"),
                git(repository, "status", "--porcelain=v1", "-uall"),
                ledger_path.read_bytes(),
                cycle_path.read_bytes(),
            )
            self.assertEqual(before, after)
            self.assertEqual(terminal, before[0])
            self.assertEqual(integrating, git(repository, "rev-parse", "HEAD^"))
            self.assertEqual(integrated, git(repository, "rev-parse", "HEAD^^"))
            self.assertEqual("queue_reconciliation", plan["current_state"])
            self.assertEqual("queue_reconciliation", plan["workflow_type"])
            self.assertIsNone(plan["selected_feature"])
            self.assertIsNone(plan["accepted_feature_commit"])
            self.assertIsNone(plan["execution_plan"]["feature_id"])
            self.assertIsNone(plan["executable_plan"]["feature_id"])
            self.assertNotEqual("feature_writer", plan["required_lease"])
            self.assertFalse(plan["old_session_will_resume"])
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertEqual([], engine.launcher.actions)
            historical = plan["kernel_projection"]["historical_integration_outcomes"]
            self.assertEqual("F001", historical[-1]["feature_id"])
            self.assertEqual(accepted, historical[-1]["accepted_commit"])
            self.assertEqual(integrated, historical[-1]["integrated_commit"])
            superseded = plan["superseded_legacy_cycles"]
            self.assertTrue(any(
                item.get("feature_id") == "F001"
                and item.get("classification") == "superseded"
                for item in superseded
            ))

    def test_distinct_ready_feature_remains_selectable_after_completed_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                configuration,
                engine,
                _,
                _,
                _,
                _,
                _,
            ) = self._completed_integration_fixture(
                Path(temporary), next_feature_status="ready"
            )
            ledger_path = (
                configuration.root
                / "state/projects/synthetic/evidence-ledger.jsonl"
            )
            before = (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "rev-parse", "HEAD^{tree}"),
                ledger_path.read_bytes(),
            )

            plan = engine.run_project(project, "resume", dry_run=True)

            self.assertEqual(before, (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "rev-parse", "HEAD^{tree}"),
                ledger_path.read_bytes(),
            ))
            self.assertEqual("feature_ready", plan["current_state"])
            self.assertEqual("F002", plan["selected_feature"])
            self.assertEqual("F002", plan["execution_plan"]["feature_id"])
            self.assertEqual("F002", plan["executable_plan"]["feature_id"])
            self.assertNotEqual("F001", plan["selected_feature"])
            self.assertEqual([], engine.launcher.actions)

    def test_consistency_invariant_rejects_reselected_integrated_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                _, project, configuration, engine, accepted, _, _, _, _,
            ) = self._completed_integration_fixture(Path(temporary))
            good = engine.project_plan(project)
            malicious_projection = dict(good["kernel_projection"])
            malicious_projection.update(
                {
                    "current_state": "feature_ready",
                    "current_feature": "F001",
                    "selected_next_feature": "F001",
                    "accepted_feature_commit": accepted,
                    "allowed_next_action": "feature_cycle",
                }
            )
            malicious_projection["projection_fingerprint"] = projection_fingerprint(
                malicious_projection
            )
            malicious_status = dict(good)
            malicious_status["kernel_projection"] = malicious_projection
            malicious_status["executable_plan"] = {
                **good["executable_plan"],
                "workflow_type": "feature_execution",
                "feature_id": "F001",
                "accepted_commit": accepted,
                "lease_type": "feature_writer",
            }
            checker = ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: malicious_status,
            )

            result = checker.check()

            invariant = next(
                item for item in result["invariants"]
                if item["invariant"] == "integrated_feature_not_executable"
            )
            self.assertFalse(invariant["passed"])
            self.assertIn(
                "observed_integrated_feature_not_executable",
                invariant["evidence"]["checks"],
            )

    def test_feature_cycle_revalidates_projection_before_internal_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            migrator = LegacyStateMigrator(
                controller_root=configuration.root, project=project
            )
            migrator.apply()
            launcher = SyntheticLauncher()
            engine = CycleEngine(configuration, launcher)
            original_validate = engine._validate_projected_dispatch

            def block_integration(*args, **kwargs):
                if kwargs.get("workflow_type") == WorkflowType.MILESTONE_INTEGRATION:
                    raise ProjectionError("integration projection changed after acceptance")
                return original_validate(*args, **kwargs)

            with mock.patch.object(
                engine, "_validate_projected_dispatch", side_effect=block_integration
            ):
                with self.assertRaisesRegex(ProjectionError, "changed after acceptance"):
                    engine.run_project(project, "one_feature")
            self.assertEqual(["feature_cycle"], launcher.actions)
            events = migrator.ledger.read()
            self.assertFalse(any(
                event["event_type"] == "TransactionStarted"
                and event["workflow_type"] == "milestone_integration"
                for event in events
            ))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertTrue(RepositoryInspector(repository).is_clean)

    def test_reserved_dispatch_revalidation_fails_before_kernel_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            migrator = LegacyStateMigrator(
                controller_root=configuration.root, project=project
            )
            migrator.apply()
            engine = CycleEngine(configuration, SyntheticLauncher())
            inspector = RepositoryInspector(repository)
            ledger_before = migrator.ledger.path.read_bytes()
            app_before = (repository / "app.txt").read_bytes()
            launch_lock = engine._launch_lock(project, inspector).path

            def reject_changed_plan(*args, **kwargs):
                self.assertTrue(launch_lock.is_file())
                raise ProjectionError("execution plan changed under reservation")

            with mock.patch.object(
                engine, "_validate_projected_dispatch", side_effect=reject_changed_plan
            ):
                with self.assertRaisesRegex(ProjectionError, "changed under reservation"):
                    engine.run_project(project, "one_feature")
            self.assertFalse(launch_lock.exists())
            self.assertEqual(ledger_before, migrator.ledger.path.read_bytes())
            self.assertEqual(app_before, (repository / "app.txt").read_bytes())
            self.assertTrue(inspector.is_clean)

    def test_feature_dispatch_rejects_post_validation_milestone_advance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            migrator = LegacyStateMigrator(
                controller_root=configuration.root, project=project
            )
            migrator.apply()
            engine = CycleEngine(configuration, SyntheticLauncher())
            planned = engine.project_plan(project)
            self.assertEqual(git(repository, "rev-parse", "HEAD"), planned["feature_starting_commit"])
            ledger_before = migrator.ledger.path.read_bytes()
            state = engine._project_document(
                project, "stale-feature-dispatch", RepositoryInspector(repository).identity()["path_fingerprint"]
            )
            original_validate = engine._validate_projected_dispatch
            advanced_tree: list[str] = []

            def advance_after_validation(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                (repository / "after-plan.txt").write_text("advanced\n", encoding="utf-8")
                git(repository, "add", "after-plan.txt")
                git(repository, "commit", "-m", "test: advance after validation")
                advanced_tree.append(git(repository, "rev-parse", "HEAD^{tree}"))
                return result

            with mock.patch.object(
                engine,
                "_validate_projected_dispatch",
                side_effect=advance_after_validation,
            ):
                with self.assertRaisesRegex(TransactionError, "planned transaction start"):
                    engine._execute_feature(
                        project,
                        "one_feature",
                        "stale-feature-dispatch",
                        state,
                        expected_feature_id=str(planned["selected_feature"]),
                    )
            self.assertEqual(ledger_before, migrator.ledger.path.read_bytes())
            self.assertEqual(advanced_tree, [git(repository, "rev-parse", "HEAD^{tree}")])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_feature_dispatch_rejects_feature_identity_mismatch_before_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            migrator = LegacyStateMigrator(
                controller_root=configuration.root, project=project
            )
            migrator.apply()
            engine = CycleEngine(configuration, SyntheticLauncher())
            ledger_before = migrator.ledger.path.read_bytes()
            state = engine._project_document(
                project,
                "feature-identity-mismatch",
                RepositoryInspector(repository).identity()["path_fingerprint"],
            )

            with self.assertRaisesRegex(
                ProjectionError,
                "queue selection disagrees with the authoritative feature identity",
            ):
                engine._execute_feature(
                    project,
                    "one_feature",
                    "feature-identity-mismatch",
                    state,
                    expected_feature_id="F999",
                )

            self.assertEqual(ledger_before, migrator.ledger.path.read_bytes())
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_integration_dispatch_rejects_post_validation_milestone_advance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, _, engine, _, _ = self._integration_fixture(root)
            inspector = RepositoryInspector(repository)
            context = engine._authoritative_execution_context(project)
            self.assertIsNotNone(context)
            _, planned, _ = context
            self.assertEqual(git(repository, "rev-parse", "HEAD"), planned.starting_commit)
            ledger_path = root / "controller/state/projects/synthetic/evidence-ledger.jsonl"
            ledger_before = ledger_path.read_bytes()
            original_validate = engine._validate_projected_dispatch
            advanced_tree: list[str] = []

            def advance_after_validation(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                (repository / "after-plan.txt").write_text("advanced\n", encoding="utf-8")
                git(repository, "add", "after-plan.txt")
                git(repository, "commit", "-m", "test: advance after validation")
                advanced_tree.append(git(repository, "rev-parse", "HEAD^{tree}"))
                return result

            with mock.patch.object(
                engine,
                "_validate_projected_dispatch",
                side_effect=advance_after_validation,
            ):
                with self.assertRaisesRegex(TransactionError, "planned transaction start"):
                    engine._execute_projected_integration(
                        project, "milestone", "stale-integration-dispatch", planned
                    )
            self.assertEqual(ledger_before, ledger_path.read_bytes())
            self.assertEqual(advanced_tree, [git(repository, "rev-parse", "HEAD^{tree}")])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_projected_integration_allows_clean_feature_branch_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, _, engine, _, _ = self._integration_fixture(root)
            inspector = RepositoryInspector(repository)
            context = engine._authoritative_execution_context(project)
            self.assertIsNotNone(context)
            projection, execution_plan, _ = context
            git(repository, "switch", "codex/f001-authoritative")
            self.assertTrue(RepositoryInspector(repository).is_clean)

            result = engine._execute_projected_integration(
                project,
                "milestone",
                "feature-checkout-integration",
                execution_plan,
            )
            self.assertEqual("feature_integrated", result["outcome"])
            self.assertFalse(result["model_session_launched"])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_actual_integration_dispatch_uses_plan_commit_when_queue_contains_self(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, engine, accepted, _ = self._integration_fixture(root)
            inspector = RepositoryInspector(repository)
            context = engine._authoritative_execution_context(project)
            self.assertIsNotNone(context)
            projection, execution_plan, _ = context
            self.assertEqual(accepted, execution_plan.accepted_commit)
            live_feature = FeatureQueue.from_location(
                repository, project.queue_location
            ).feature("F001")
            self.assertEqual("ready", live_feature["status"])
            self.assertIsNone(live_feature["accepted_commit"])

            class CapturingLauncher(SyntheticLauncher):
                def __init__(self):
                    super().__init__()
                    self.requests = []

                def launch(self, request, on_session_started=None):
                    self.requests.append(request)
                    return super().launch(
                        request, on_session_started=on_session_started
                    )

            launcher = CapturingLauncher()
            engine.launcher = launcher
            result = engine.run_project(project, "resume")
            self.assertEqual(accepted, result["accepted_commit"])
            self.assertEqual([], launcher.requests)
            self.assertFalse(result["model_session_launched"])
            persisted_plan = json.loads(
                Path(result["integration_plan"]).read_text(encoding="utf-8")
            )
            self.assertEqual(accepted, persisted_plan["accepted_commit"])
            self.assertEqual(accepted, persisted_plan["accepted_metadata_ref"])
            ledger_text = (
                configuration.root / "state/projects/synthetic/evidence-ledger.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn('\"accepted_commit\":\"SELF\"', ledger_text)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_execution_plan_disagreement_fails_closed_and_invariant_detects_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, _ = self._integration_fixture(Path(temporary))
            projection = engine._authoritative_projection(project)
            self.assertIsNotNone(projection)
            executable = engine._execution_plan(project, projection)
            status = authoritative_status_fields(
                projection,
                executable,
                legacy_plan={},
                persisted_state="feature_running",
                superseded_cycles=[],
            )
            passed, _ = execution_plan_projection_agreement(projection, status, executable)
            self.assertTrue(passed)
            status["sessions_that_would_launch"] = ["resume persisted Conveyor cycle"]
            passed, evidence = execution_plan_projection_agreement(
                projection, status, executable
            )
            self.assertFalse(passed)
            self.assertFalse(evidence["checks"]["sessions_that_would_launch"])
            mismatched = {**projection, "required_lease": "feature_writer"}
            mismatched["projection_fingerprint"] = projection_fingerprint(mismatched)
            with self.assertRaisesRegex(Exception, "required lease disagrees"):
                ExecutionPlan.from_projection(mismatched)

    def test_active_kernel_transaction_is_recovery_without_legacy_session_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, _ = self._integration_fixture(Path(temporary))
            projection = engine._authoritative_projection(project)
            self.assertIsNotNone(projection)
            active = {
                "transaction_id": "active-kernel-transaction",
                "workflow_type": "feature_execution",
                "session_ids": [],
            }
            recovering = {
                **projection,
                "active_transaction": active["transaction_id"],
                "transactions": [*projection["transactions"], active],
                "session_resume_eligible": False,
                "required_lease": "feature_writer",
            }
            recovering["projection_fingerprint"] = projection_fingerprint(recovering)
            plan = ExecutionPlan.from_projection(recovering)
            self.assertEqual("recovery", plan.transaction_mode)
            self.assertFalse(plan.session_resume_eligible)
            self.assertIsNone(plan.session_to_resume)
            self.assertEqual([], plan.sessions_that_would_launch)

    def test_pre_session_ledger_interruptions_are_recovery_without_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = EvidenceLedger(
                root / "evidence-ledger.jsonl",
                project_id="synthetic",
                repository_identity="r" * 64,
                repository_path_fingerprint="p" * 64,
            )
            payload = {
                "run_id": "interrupted-run",
                "feature_id": "F001",
                "starting_branch": "codex/m0-foundation",
                "starting_head": "a" * 40,
                "allowed_mutation_policy": MutationPolicy(tuple()).to_dict(),
            }
            ledger.append(
                event_type="TransactionStarted",
                transaction_id="interrupted",
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )
            for event_type, event_payload in (
                (
                    "LeaseAcquired",
                    {"lease_id": "lease", "lease_type": "feature_writer"},
                ),
                ("SnapshotCaptured", {"snapshot": {}}),
            ):
                ledger.append(
                    event_type=event_type,
                    transaction_id="interrupted",
                    workflow_type=WorkflowType.FEATURE_EXECUTION,
                    payload=event_payload,
                )
                projection = ProjectionEngine(ledger).rebuild(persist_cache=False)
                self.assertFalse(projection["session_resume_eligible"])
                plan = ExecutionPlan.from_projection(projection)
                self.assertEqual("recovery", plan.transaction_mode)
                self.assertIsNone(plan.session_to_resume)
                self.assertEqual([], plan.sessions_that_would_launch)

    def test_every_projection_action_has_explicit_session_and_mutation_semantics(self):
        expected = {
            "queue_reconciliation": ("queue_reconciliation", True, True),
            "feature_cycle": ("feature_execution", True, True),
            "milestone_integration": ("milestone_integration", True, False),
            "milestone_gate": ("milestone_gate", True, True),
            "human_decision_resolution": (
                "human_decision_resolution", False, False,
            ),
            "verify_consistency": ("recovery", False, False),
            "human_merge_approval": ("human_merge_approval", False, False),
        }
        self.assertEqual(set(ACTION_WORKFLOW), set(expected))
        for action, (workflow, mutates, launches) in expected.items():
            with self.subTest(action=action):
                projection = {
                    "schema_version": 1,
                    "project_id": "synthetic",
                    "ledger_sequence": 0,
                    "ledger_fingerprint": "0" * 64,
                    "current_state": "feature_ready",
                    "active_transaction": None,
                    "current_feature": "F001",
                    "selected_next_feature": "F001",
                    "accepted_feature_commit": None,
                    "allowed_next_action": action,
                    "session_resume_eligible": False,
                    "required_lease": None,
                    "transactions": [],
                }
                projection["projection_fingerprint"] = projection_fingerprint(projection)
                plan = ExecutionPlan.from_projection(projection)
                self.assertEqual(workflow, plan.workflow_type)
                self.assertEqual(mutates, plan.application_mutation_expected)
                self.assertEqual(launches, bool(plan.sessions_that_would_launch))
        unknown = {**projection, "allowed_next_action": "unknown_action"}
        unknown["projection_fingerprint"] = projection_fingerprint(unknown)
        with self.assertRaisesRegex(ProjectionError, "unsupported next action"):
            ExecutionPlan.from_projection(unknown)

    def test_consistency_observes_planner_facing_routing_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, configuration, engine, _, _ = self._integration_fixture(
                Path(temporary)
            )
            observed = engine.project_plan(project)
            observed.update({
                "derived_state": "feature_running",
                "persisted_state": "feature_running",
                "next_action": "resume",
                "proposed_next_action": "milestone_integration",
                "old_session_will_resume": True,
                "sessions_that_would_launch": ["resume persisted Conveyor cycle"],
                "required_lease": "feature_writer",
            })
            result = ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: observed,
            ).check()
            self.assertEqual("RECOVERABLE_INCONSISTENCY", result["classification"])
            invariant = next(
                item for item in result["invariants"]
                if item["invariant"] == "execution_plan_projection_agreement"
            )
            self.assertFalse(invariant["passed"])
            self.assertEqual(
                "planner_observer", invariant["evidence"]["observation_source"]
            )

    def test_compatibility_cache_reconciliation_is_atomic_idempotent_and_app_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, cycle_path = self._integration_fixture(
                Path(temporary)
            )
            refs_before = git(repository, "show-ref")
            status_before = git(repository, "status", "--porcelain=v1", "--branch")
            cycle_before = cycle_path.read_bytes()
            first = engine.reconcile_controller_state(project, dry_run=False)
            cache_path = engine.project_state_path(project)
            first_bytes = cache_path.read_bytes()
            second = engine.reconcile_controller_state(project, dry_run=False)
            self.assertTrue(first["controller_state_written"])
            self.assertFalse(second["controller_state_written"])
            self.assertEqual(first_bytes, cache_path.read_bytes())
            self.assertEqual(cycle_before, cycle_path.read_bytes())
            self.assertEqual(refs_before, git(repository, "show-ref"))
            self.assertEqual(status_before, git(repository, "status", "--porcelain=v1", "--branch"))

    def test_compatibility_cache_reconciliation_rejects_foreign_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _ = self._integration_fixture(
                Path(temporary)
            )
            cache_path = engine.project_state_path(project)
            original = json.loads(cache_path.read_text(encoding="utf-8"))
            app_head = git(repository, "rev-parse", "HEAD")
            app_status = git(repository, "status", "--porcelain=v1", "--branch")

            foreign_project = {**original, "project_id": "foreign-project"}
            write_json(cache_path, foreign_project)
            with self.assertRaisesRegex(Exception, "different project"):
                engine.reconcile_controller_state(project, dry_run=False)

            foreign_repository = {
                **original,
                "repository_fingerprint": "f" * 64,
            }
            write_json(cache_path, foreign_repository)
            with self.assertRaisesRegex(Exception, "different repository fingerprint"):
                engine.reconcile_controller_state(project, dry_run=False)

            self.assertEqual(app_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(
                app_status, git(repository, "status", "--porcelain=v1", "--branch")
            )
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_migration_and_actual_kernel_execution_keep_controller_source_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            controller = configuration.root
            shutil.copy2(REPOSITORY_ROOT / ".gitignore", controller / ".gitignore")
            git(controller, "init", "-b", "main")
            git(controller, "config", "user.name", "Synthetic Conveyor")
            git(controller, "config", "user.email", "synthetic@example.invalid")
            git(controller, "add", ".gitignore", "schemas")
            git(controller, "commit", "-m", "controller source baseline")
            controller_head = git(controller, "rev-parse", "HEAD")

            migrator = LegacyStateMigrator(controller_root=controller, project=project)
            first = migrator.apply()
            migration_sequence = migrator.ledger.verify().sequence
            ledger_path = migrator.ledger.path
            cache_path = migrator.projection.cache_path
            self.assertTrue(first["migration_applied"])
            self.assertEqual("", git(controller, "status", "--porcelain"))
            self.assertTrue(ledger_path.read_text(encoding="utf-8").strip())
            self.assertIsNotNone(migrator.projection.load_cache())
            second = migrator.apply()
            self.assertFalse(second["migration_applied"])
            self.assertEqual(migration_sequence, migrator.ledger.verify().sequence)

            main_before = git(repository, "rev-parse", "main")
            launcher = SyntheticLauncher()
            result = CycleEngine(configuration, launcher).run_project(
                project, "one_feature"
            )
            self.assertEqual("one_feature_integrated", result["outcome"])
            self.assertEqual(["feature_cycle"], launcher.actions)
            integrity = migrator.ledger.verify()
            rebuilt = migrator.projection.rebuild(persist_cache=False)
            cache = migrator.projection.load_cache()
            self.assertGreater(integrity.sequence, migration_sequence)
            self.assertTrue(cache_agrees(cache, rebuilt))
            self.assertTrue(ledger_path.read_text(encoding="utf-8").strip())
            self.assertTrue(cache_path.read_text(encoding="utf-8").strip())
            self.assertEqual("", git(controller, "status", "--porcelain"))
            self.assertEqual(controller_head, git(controller, "rev-parse", "HEAD"))
            self.assertEqual(main_before, git(repository, "rev-parse", "main"))
            next_plan = CycleEngine(configuration, launcher).project_plan(project)
            self.assertEqual(
                git(repository, "rev-parse", project.milestone_branch),
                next_plan["feature_starting_commit"],
            )
            continuation = CycleEngine(configuration, launcher).run_project(
                project, "milestone"
            )
            self.assertIn(
                continuation["outcome"],
                {
                    "milestone_complete",
                    "reconciled_no_ready_work",
                    "milestone_ready_for_merge",
                },
            )
            self.assertEqual(
                [
                    "feature_cycle",
                    "milestone_gate",
                ],
                launcher.actions,
            )

    def test_runtime_ignores_are_root_anchored_and_narrow(self):
        ignored = (
            "state/projects/example.json",
            "state/projects/example/evidence-ledger.jsonl",
            "state/projects/example/evidence-ledger.jsonl.head",
            "state/projects/example/evidence-ledger.jsonl.lock",
            "state/projects/example/projection-cache.json",
        )
        for relative in ignored:
            result = subprocess.run(
                ["git", "check-ignore", "-q", relative], cwd=REPOSITORY_ROOT,
                check=False,
            )
            self.assertEqual(0, result.returncode, relative)
        for relative in (
            "state/projects/example/source.py",
            "nested/state/projects/example/projection-cache.json",
        ):
            result = subprocess.run(
                ["git", "check-ignore", "-q", relative], cwd=REPOSITORY_ROOT,
                check=False,
            )
            self.assertNotEqual(0, result.returncode, relative)

if __name__ == "__main__":
    unittest.main()
