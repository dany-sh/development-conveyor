import tempfile
import unittest
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import SessionError
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan, SessionResult
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository


class OneFailureLauncher(SyntheticLauncher):
    def __init__(self):
        super().__init__()
        self.failed = False
        self.repair_request = None

    def launch(self, request):
        if not self.failed and request.action == "feature_cycle":
            self.failed = True
            self.actions.append(request.action)
            plan = SessionPlan(("codex", "exec"), request.project.repository, "synthetic", "0" * 64, "workspace-write")
            return SessionResult(
                request.action,
                1,
                "retryable-session",
                "compiler failure: missing generated interface",
                plan,
                retryable=True,
                failure_classification="validation_failure",
                retry_hypothesis="the generated interface is stale",
                remediation_action="regenerate the interface before rebuilding",
                retry_evidence="compiler reported a missing generated interface",
            )
        if request.action == "feature_cycle":
            self.repair_request = request
        return super().launch(request)


class ExhaustingLauncher(SyntheticLauncher):
    def launch(self, request):
        self.actions.append(request.action)
        attempt = len(self.actions)
        plan = SessionPlan(("codex", "exec"), request.project.repository, "synthetic", "0" * 64, "workspace-write")
        return SessionResult(
            request.action,
            1,
            "failing-session",
            f"distinct failure evidence {attempt}",
            plan,
            retryable=True,
            failure_classification="validation_failure",
            retry_hypothesis=f"distinct synthetic hypothesis {attempt}",
            remediation_action=f"distinct synthetic remediation {attempt}",
            retry_evidence=f"distinct synthetic evidence {attempt}",
        )


class CycleEngineTests(unittest.TestCase):
    def test_complete_one_feature_cycle_integrates_exactly_one_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            self.assertEqual(launcher.actions, ["feature_cycle"])
            inspector = RepositoryInspector(repository)
            self.assertTrue(inspector.is_clean)
            self.assertEqual(inspector.patch_fingerprint(result["accepted_commit"]), inspector.patch_fingerprint(result["integrated_commit"]))
            self.assertEqual(git(repository, "rev-parse", "main"), project.validated_baseline_commit)
            state = engine.cycle_store.read(inspector.cycle_state_path())
            self.assertEqual(state["current_phase"], "completed")
            action_count = len(launcher.actions)
            resumed = engine.resume_project(project)
            self.assertEqual(resumed["outcome"], "already_completed")
            self.assertEqual(len(launcher.actions), action_count)

    def test_queue_reconciliation_runs_before_feature_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root, feature_status="proposed")
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            self.assertEqual(launcher.actions[:2], ["queue_reconciliation", "feature_cycle"])

    def test_dry_run_does_not_write_application_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            before = git(repository, "status", "--porcelain=v1", "--branch")
            engine = CycleEngine(controller_configuration(root, project), SyntheticLauncher())
            plan = engine.run_project(project, "milestone", dry_run=True)
            after = git(repository, "status", "--porcelain=v1", "--branch")
            self.assertEqual(plan["proposed_next_action"], "feature_cycle")
            self.assertEqual(before, after)

    def test_focused_session_repair_resumes_and_records_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = OneFailureLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            self.assertEqual(launcher.actions, ["feature_cycle", "feature_cycle"])
            self.assertEqual(launcher.repair_request.repair_hypothesis, "the generated interface is stale")
            self.assertEqual(
                launcher.repair_request.remediation_action,
                "regenerate the interface before rebuilding",
            )
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["validation_attempts"][0]["attempt"], 1)

    def test_validation_failure_stops_after_configured_repairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = ExhaustingLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            with self.assertRaisesRegex(SessionError, "project=synthetic"):
                engine.run_project(project, "one_feature")
            self.assertEqual(len(launcher.actions), 4)
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["current_phase"], "human_decision_required")
            self.assertTrue(state["retry_exhausted"])
            self.assertEqual(state["human_decision_required"]["attempts_remaining"], 0)
            self.assertFalse(state["human_decision_required"]["retryable"])
