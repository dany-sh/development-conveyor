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
    execution_plan_projection_agreement,
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
    def _integration_fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        milestone_start = git(repository, "rev-parse", "HEAD")
        feature_branch = "codex/f001-authoritative"
        git(repository, "switch", "-c", feature_branch)
        (repository / "app.txt").write_text(
            "baseline\nF001 accepted behavior\n", encoding="utf-8"
        )
        git(repository, "add", "app.txt")
        git(repository, "commit", "-m", "F001: accepted behavior")
        accepted = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", project.milestone_branch)
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integration_pending",
            "branch": feature_branch,
            "integration_base_commit": milestone_start,
            "accepted_commit": accepted,
            "integration_status": "pending",
        })
        write_json(queue_path, queue)
        git(repository, "add", project.queue_location)
        git(repository, "commit", "-m", "factory: record F001 acceptance")

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
        return repository, project, configuration, engine, accepted, inspector.cycle_state_path()

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
            self.assertEqual(
                ["fresh milestone-integration transaction"],
                plan["sessions_that_would_launch"],
            )
            self.assertEqual("integration_writer", plan["execution_plan"]["lease_type"])
            self.assertEqual("fresh", plan["execution_plan"]["transaction_mode"])
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertTrue(plan["milestone_integrator_would_launch"])
            contract = plan["milestone_integration_contract"]
            self.assertEqual(
                "~/.agents/skills/milestone-integrator/scripts/integrate-feature.sh --root . --feature F001",
                contract["mutation_command"],
            )
            self.assertEqual("CONVEYOR_TRANSACTION_RESULT=", contract["terminal_marker"])
            self.assertEqual(
                "milestone_integration",
                contract["terminal_schema"]["properties"]["workflow_type"]["const"],
            )
            self.assertEqual(accepted, contract["accepted_commit"])
            self.assertEqual("fresh", contract["transaction_mode"])
            self.assertEqual("legacy-session", plan["superseded_legacy_cycles"][0]["session_id"])
            self.assertEqual("superseded", plan["superseded_legacy_cycles"][0]["classification"])
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
                        project, "one_feature", "stale-feature-dispatch", state
                    )
            self.assertEqual(ledger_before, migrator.ledger.path.read_bytes())
            self.assertEqual(advanced_tree, [git(repository, "rev-parse", "HEAD^{tree}")])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_integration_dispatch_rejects_post_validation_milestone_advance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, _, engine, _, _ = self._integration_fixture(root)
            inspector = RepositoryInspector(repository)
            state_root = root / "controller/state/projects/synthetic"
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                repository_path_fingerprint=inspector.identity()["path_fingerprint"],
            )
            migration_transaction = next(
                event["transaction_id"] for event in reversed(ledger.read())
                if event["event_type"] == "ProjectionUpdated"
            )
            ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=migration_transaction,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "current_state": "integration_ready",
                    "current_feature": "F001",
                    "projection_facts": {
                        "selected_feature_starting_commit": inspector.head,
                    },
                },
            )
            ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            ).rebuild(persist_cache=True)
            projection = engine._authoritative_projection(project)
            self.assertIsNotNone(projection)
            planned = engine._execution_plan(project, projection)
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
            state_root = root / "controller/state/projects/synthetic"
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                repository_path_fingerprint=inspector.identity()["path_fingerprint"],
            )
            migration_transaction = next(
                event["transaction_id"] for event in reversed(ledger.read())
                if event["event_type"] == "ProjectionUpdated"
            )
            ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=migration_transaction,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "current_state": "integration_ready",
                    "current_feature": "F001",
                    "projection_facts": {
                        "selected_feature_starting_commit": inspector.head,
                    },
                },
            )
            projection = ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            ).rebuild(persist_cache=True)
            git(repository, "switch", "codex/f001-authoritative")
            git(
                repository,
                "checkout",
                project.milestone_branch,
                "--",
                project.queue_location,
            )
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: materialize accepted queue state")
            self.assertTrue(RepositoryInspector(repository).is_clean)

            result = engine._execute_projected_integration(
                project,
                "milestone",
                "feature-checkout-integration",
                engine._execution_plan(project, projection),
            )
            self.assertEqual("feature_integrated", result["outcome"])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_actual_integration_dispatch_uses_plan_commit_when_queue_contains_self(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, engine, accepted, _ = self._integration_fixture(root)
            inspector = RepositoryInspector(repository)
            state_root = configuration.root / "state/projects/synthetic"
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                repository_path_fingerprint=inspector.identity()["path_fingerprint"],
            )
            migration_transaction = next(
                event["transaction_id"] for event in reversed(ledger.read())
                if event["event_type"] == "ProjectionUpdated"
            )
            ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=migration_transaction,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "current_state": "integration_ready",
                    "current_feature": "F001",
                    "projection_facts": {
                        "selected_feature_starting_commit": inspector.head,
                    },
                },
            )
            ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            ).rebuild(persist_cache=True)
            git(repository, "switch", "codex/f001-authoritative")
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0].update({
                "status": "integration_pending",
                "branch": "codex/f001-authoritative",
                "integration_base_commit": project.validated_baseline_commit,
                "accepted_commit": "SELF",
                "integration_status": "pending",
            })
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: preserve SELF queue sentinel")

            projection = engine._authoritative_projection(project)
            self.assertIsNotNone(projection)
            execution_plan = engine._execution_plan(project, projection)
            self.assertEqual(accepted, execution_plan.accepted_commit)
            self.assertEqual(
                "SELF",
                FeatureQueue.from_location(repository, project.queue_location)
                .feature("F001")["accepted_commit"],
            )

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
            self.assertEqual(accepted, launcher.requests[-1].accepted_commit)
            prompt = SessionLauncher(
                REPOSITORY_ROOT, configuration.conveyor
            )._render_prompt(launcher.requests[-1])
            self.assertIn(f"Accepted commit: `{accepted}`", prompt)
            self.assertNotIn("Accepted commit: `SELF`", prompt)

            report = json.loads((
                configuration.root
                / f"reports/{launcher.requests[-1].run_id}/milestone_integration.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(accepted, report["accepted_commit"])
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
            "milestone_integration": ("milestone_integration", True, True),
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
            self.assertEqual(["feature_cycle", "milestone_integration"], launcher.actions)
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
                    "milestone_integration",
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
