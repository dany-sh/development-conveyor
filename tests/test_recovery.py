import tempfile
import unittest
from pathlib import Path

from development_conveyor.recovery import assess_recovery
from development_conveyor.errors import ConveyorError, RetryExhausted
from development_conveyor.retries import RetryBudget
from development_conveyor.repository import RepositoryInspector
from tests.helpers import synthetic_repository


class RecoveryTests(unittest.TestCase):
    def _state(self, inspector, project, phase="feature_in_progress"):
        identity = inspector.identity()
        return {
            "repository_path_fingerprint": identity["path_fingerprint"],
            "repository_identity": identity,
            "accepted_feature_commit": None,
            "current_phase": phase,
            "feature_branch": "codex/f001-synthetic-feature",
            "milestone_branch": project.milestone_branch,
            "milestone_pre_integration_commit": project.validated_baseline_commit,
            "milestone_post_integration_commit": None,
            "last_verified_git_state": {"branch": inspector.current_branch, "head": inspector.head},
        }

    def test_identity_disagreement_requires_human_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            state = self._state(inspector, project)
            state["repository_path_fingerprint"] = "wrong"
            assessment = assess_recovery(project, state)
            self.assertEqual(assessment.outcome, "human_decision_required")
            self.assertIn("scripts/conveyor resume --project synthetic", assessment.human_decision["resume_command"])

    def test_matching_interrupted_integration_resumes_without_discard(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            state = self._state(inspector, project, phase="integrating")
            git_path = Path(inspector.git(["rev-parse", "--git-path", "CHERRY_PICK_HEAD"]).stdout.strip())
            if not git_path.is_absolute():
                git_path = repository / git_path
            git_path.write_text(inspector.head + "\n", encoding="utf-8")
            assessment = assess_recovery(project, state)
            self.assertEqual(assessment.outcome, "resume")
            self.assertTrue(git_path.exists())

    def test_retries_require_new_hypotheses_and_stop_at_limit(self):
        budget = RetryBudget(2)
        self.assertEqual(budget.record(hypothesis="missing import", evidence="compiler output"), 1)
        with self.assertRaises(ConveyorError):
            budget.record(hypothesis=" missing  import ", evidence="same output")
        self.assertEqual(budget.record(hypothesis="stale generated interface", evidence="fresh diff"), 2)
        with self.assertRaises(RetryExhausted):
            budget.record(hypothesis="third distinct cause", evidence="new output")
