import tempfile
import unittest
from pathlib import Path

from development_conveyor.repository import RepositoryInspector
from tests.helpers import synthetic_repository


class RepositoryTests(unittest.TestCase):
    def test_identity_is_stable_and_state_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            first = RepositoryInspector(repository)
            second = RepositoryInspector(repository)
            self.assertEqual(first.identity(), second.identity())
            evidence = first.inspect(baseline=project.validated_baseline_commit, milestone_branch=project.milestone_branch)
            self.assertTrue(evidence["baseline_exists"])
            self.assertTrue(evidence["baseline_is_ancestor_of_milestone"])
            self.assertTrue(evidence["clean"])
            (repository / "app.txt").write_text("dirty\n", encoding="utf-8")
            self.assertFalse(first.is_clean)

    def test_runtime_ignore_does_not_edit_tracked_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            additions = inspector.ensure_runtime_ignored()
            self.assertIn(".factory/conveyor-state.json", additions)
            self.assertTrue(inspector.is_clean)

    def test_recovery_runtime_artifacts_are_root_anchored_ignored(self):
        root_ignore = Path(__file__).resolve().parents[1] / ".gitignore"
        patterns = root_ignore.read_text(encoding="utf-8").splitlines()
        self.assertIn("/state/projects/*/.recovery-takeover.lock", patterns)
        self.assertIn("/state/projects/*/recovered-leases/", patterns)
