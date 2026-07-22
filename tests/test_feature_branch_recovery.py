import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError, SessionError
from development_conveyor.recovery import assess_recovery
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan, SessionResult
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


class SuccessfulNoEvidenceLauncher:
    def __init__(self, mutate=None):
        self.actions = []
        self.requests = []
        self.mutate = mutate

    def launch(self, request, on_session_started=None):
        self.actions.append(request.action)
        self.requests.append(request)
        session_id = request.session_id or "normal-session"
        if on_session_started is not None:
            on_session_started(session_id)
        if self.mutate:
            self.mutate(request)
        plan = SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64,
            "workspace-write",
        )
        return SessionResult(request.action, 0, session_id, "done", plan)


class FeatureBranchRecoveryTests(unittest.TestCase):
    def test_fresh_branch_is_derived_from_feature_id_and_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            engine = CycleEngine(controller_configuration(Path(temporary), project), SuccessfulNoEvidenceLauncher())
            feature = {"id": "F003", "title": "Single-Window Application Shell", "branch": None}
            self.assertEqual(engine._expected_feature_branch(project, feature), "codex/f003-single-window-application-shell")

    def test_invalid_explicit_feature_branch_is_rejected_before_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            engine = CycleEngine(controller_configuration(Path(temporary), project), SuccessfulNoEvidenceLauncher())
            with self.assertRaisesRegex(RecoveryError, "allowed codex/"):
                engine._expected_feature_branch(project, {"id": "F003", "title": "Safe", "branch": "bad branch"})

    def test_fresh_branch_preparation_uses_the_exact_starting_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            engine = CycleEngine(controller_configuration(Path(temporary), project), SuccessfulNoEvidenceLauncher())
            inspector = RepositoryInspector(repository)
            feature = {"id": "F003", "title": "Single-Window Application Shell", "branch": None}
            state = engine._new_cycle_state(project, "fresh-run", inspector, feature)
            engine._prepare_feature_branch(project, inspector, state)
            self.assertEqual(git(repository, "branch", "--show-current"), "codex/f003-single-window-application-shell")
            self.assertEqual(git(repository, "rev-parse", "HEAD"), state["feature_starting_commit"])
            self.assertEqual(git(repository, "rev-parse", project.milestone_branch), state["milestone_pre_integration_commit"])

    def test_fresh_dry_plan_never_creates_the_derived_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            engine = CycleEngine(controller_configuration(Path(temporary), project), SuccessfulNoEvidenceLauncher())
            before = git(repository, "show-ref", "--heads")
            engine.project_plan(project)
            self.assertEqual(git(repository, "show-ref", "--heads"), before)

    def _interrupted_cycle(self, root: Path, *, three_files: bool = False):
        repository, project = synthetic_repository(root)
        engine = CycleEngine(controller_configuration(root, project), SuccessfulNoEvidenceLauncher())
        inspector = RepositoryInspector(repository)
        inspector.ensure_runtime_ignored()
        queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
        feature = queue["features"][0]
        state = engine._new_cycle_state(project, "recovery-run", inspector, feature)
        state.update({
            "current_phase": "feature_in_progress",
            "feature_session_id": "completed-session",
            "session_id": "completed-session",
            "last_successful_checkpoint": "feature_session_completed",
        })
        engine.cycle_store.write(inspector.cycle_state_path(), state)
        paths = [repository / "app.txt"]
        if three_files:
            paths.extend([
                repository / ".factory/project.yaml",
                repository / "docs/AUTONOMY_CONTRACT.md",
            ])
        for path in paths:
            path.write_text(path.read_text(encoding="utf-8") + "\npartial session work\n", encoding="utf-8")
        return repository, project, engine, state

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_01_persisted_feature_branch_missing_from_git_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, state = self._interrupted_cycle(Path(temporary))
            assessment = assess_recovery(project, state)
            self.assertEqual(assessment.outcome, "branch_recovery_required")
            self.assertFalse(assessment.human_decision["resume_allowed"])
            self.assertEqual(git(repository, "branch", "--show-current"), project.milestone_branch)

    def test_02_session_on_milestone_branch_is_branch_invariant_violation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _ = self._interrupted_cycle(Path(temporary))
            plan = engine.project_plan(project)
            self.assertEqual(plan["session_completion_classification"], "branch_invariant_violated")
            self.assertIn("uncommitted_feature_work", plan["session_completion_flags"])

    def test_03_successful_exit_without_accepted_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            engine = CycleEngine(controller_configuration(Path(temporary), project), SuccessfulNoEvidenceLauncher())
            # Typed feature execution rejects a zero-exit result without the
            # required final transaction envelope.
            with self.assertRaises(SessionError):
                engine.run_project(project, "one_feature")
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["current_phase"], "human_decision_required")
            self.assertIsNone(state["accepted_feature_commit"])

    def test_04_dirty_worktree_after_success_is_uncommitted_feature_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)

            def mutate(request):
                path = request.project.repository / "app.txt"
                path.write_text(path.read_text(encoding="utf-8") + "partial\n", encoding="utf-8")

            engine = CycleEngine(controller_configuration(root, project), SuccessfulNoEvidenceLauncher(mutate))
            with self.assertRaises(SessionError):
                engine.run_project(project, "one_feature")
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["current_phase"], "human_decision_required")
            self.assertIn("partial", (repository / "app.txt").read_text(encoding="utf-8"))

    def test_05_absent_writer_lease_has_explicit_resume_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _ = self._interrupted_cycle(Path(temporary))
            plan = engine.project_plan(project)
            self.assertTrue(plan["writer_lock_required_before_resume"])
            self.assertFalse(plan["writer_lock_currently_held"])
            self.assertFalse(plan["resume_allowed"])

    def test_06_completion_rejection_preserves_dirty_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _ = self._interrupted_cycle(Path(temporary), three_files=True)
            before = RepositoryInspector(repository).modified_file_hashes()
            plan = engine.project_plan(project)
            self.assertFalse(plan["resume_allowed"])
            self.assertEqual(RepositoryInspector(repository).modified_file_hashes(), before)

    def test_07_safe_branch_creation_preserves_dirty_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, state = self._interrupted_cycle(Path(temporary), three_files=True)
            before = RepositoryInspector(repository).modified_file_hashes()
            result = engine.recover_feature_branch(project, dry_run=False)
            self.assertEqual(result["outcome"], "feature_branch_recovered")
            self.assertEqual(RepositoryInspector(repository).modified_file_hashes(), before)
            self.assertEqual(git(repository, "branch", "--show-current"), state["feature_branch"])

    def test_08_milestone_ref_remains_unchanged_during_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, state = self._interrupted_cycle(Path(temporary))
            engine.recover_feature_branch(project, dry_run=False)
            self.assertEqual(git(repository, "rev-parse", project.milestone_branch), state["feature_starting_commit"])

    def test_09_recovery_uses_only_nonforced_switch_and_no_regeneration(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, state = self._interrupted_cycle(Path(temporary))
            result = engine.recover_feature_branch(project, dry_run=False)
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["prohibited_operations_used"], [])
            self.assertEqual(git(repository, "reflog", "-1", "--format=%gs"),
                             f"checkout: moving from {project.milestone_branch} to {state['feature_branch']}")

    def test_10_resume_is_blocked_before_branch_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _ = self._interrupted_cycle(Path(temporary))
            result = engine.resume_project(project)
            self.assertEqual(result["outcome"], "branch_recovery_required")
            self.assertEqual(engine.launcher.actions, [])

    def test_11_resume_acquires_writer_lease_before_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, _ = self._interrupted_cycle(root)
            engine.recover_feature_branch(project, dry_run=False)

            def verify_lease(request):
                record = json.loads((repository / ".factory/locks/writer.json").read_text(encoding="utf-8"))
                self.assertEqual(record["agent_run"], "recovery-run")
                self.assertEqual(record["branch"], git(repository, "branch", "--show-current"))

            launcher = SuccessfulNoEvidenceLauncher(verify_lease)
            engine.launcher = launcher
            # Legacy cycle JSON has no exact kernel transaction and therefore
            # cannot authorize a writable continuation.
            result = engine.resume_project(project)
            self.assertEqual(result["outcome"], "human_decision_required")
            self.assertTrue(result["legacy_resume_blocked"])
            self.assertEqual(launcher.actions, [])

    def test_12_same_session_continues_only_after_branch_and_lock_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, _ = self._interrupted_cycle(root)
            engine.recover_feature_branch(project, dry_run=False)
            launcher = SuccessfulNoEvidenceLauncher()
            engine.launcher = launcher
            result = engine.resume_project(project)
            self.assertEqual(result["outcome"], "human_decision_required")
            self.assertTrue(result["legacy_resume_blocked"])
            self.assertEqual(launcher.requests, [])

    def test_13_integration_is_not_launched_without_accepted_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project = synthetic_repository(root)
            launcher = SuccessfulNoEvidenceLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            with self.assertRaises(SessionError):
                engine.run_project(project, "one_feature")
            self.assertEqual(launcher.actions, ["feature_cycle"])

    def test_14_recovery_dry_run_performs_no_repository_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _ = self._interrupted_cycle(Path(temporary), three_files=True)
            inspector = RepositoryInspector(repository)
            before_cycle = inspector.cycle_state_path().read_bytes()
            before_refs = git(repository, "show-ref", "--heads")
            before_hashes = inspector.modified_file_hashes()
            result = engine.recover_feature_branch(project, dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertEqual(inspector.cycle_state_path().read_bytes(), before_cycle)
            self.assertEqual(git(repository, "show-ref", "--heads"), before_refs)
            self.assertEqual(inspector.modified_file_hashes(), before_hashes)

    def test_15_ambiguous_dirty_state_produces_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, state = self._interrupted_cycle(Path(temporary))
            (repository / "untracked.txt").write_text("ambiguous\n", encoding="utf-8")
            result = engine.recover_feature_branch(project, dry_run=False)
            self.assertEqual(result["outcome"], "human_decision_required")
            self.assertFalse(result["preconditions"]["dirty_state_unambiguous"])
            self.assertFalse(RepositoryInspector(repository).ref_exists(state["feature_branch"]))

    def test_16_queue_fingerprint_change_is_not_accepted_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)

            def commit_queue_only(request):
                path = request.project.repository / request.project.queue_location
                queue = json.loads(path.read_text(encoding="utf-8"))
                queue["features"][0]["title"] = "Changed fingerprint only"
                write_json(path, queue)
                git(request.project.repository, "add", request.project.queue_location)
                git(request.project.repository, "commit", "-m", "F001: queue fingerprint only")

            engine = CycleEngine(
                controller_configuration(root, project), SuccessfulNoEvidenceLauncher(commit_queue_only)
            )
            with self.assertRaises(SessionError):
                engine.run_project(project, "one_feature")
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertIsNone(state["accepted_feature_commit"])

    def test_17_branch_recorded_only_in_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, state = self._interrupted_cycle(Path(temporary))
            self.assertIsInstance(state["feature_branch"], str)
            self.assertTrue(engine.project_plan(project)["branch_recovery_required"])

    def test_18_invalid_queue_after_success_is_queue_evidence_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)

            def invalidate_queue(request):
                (request.project.repository / request.project.queue_location).write_text(
                    "not valid json\n", encoding="utf-8"
                )

            engine = CycleEngine(
                controller_configuration(root, project), SuccessfulNoEvidenceLauncher(invalidate_queue)
            )
            with self.assertRaises(SessionError):
                engine.run_project(project, "one_feature")
            state = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["current_phase"], "human_decision_required")


if __name__ == "__main__":
    unittest.main()
