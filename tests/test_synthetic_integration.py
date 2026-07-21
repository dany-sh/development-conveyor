import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.repository import RepositoryInspector
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository


class SyntheticIntegrationTests(unittest.TestCase):
    def test_complete_milestone_stops_before_main_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            baseline = git(repository, "rev-parse", "main")
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            result = engine.run_project(project, "milestone")
            self.assertEqual(result["outcome"], "milestone_ready_for_merge")
            self.assertEqual(
                launcher.actions,
                ["feature_cycle", "milestone_integration", "milestone_gate"],
            )
            self.assertEqual(git(repository, "rev-parse", "main"), baseline)
            self.assertNotEqual(git(repository, "rev-parse", project.milestone_branch), baseline)
            queue = json.loads((repository / project.queue_location).read_text())
            self.assertEqual(queue["milestones"][0]["status"], "gate_passed")
            self.assertFalse(result["human_gate"]["default_branch_merge_performed"])
            self.assertTrue(RepositoryInspector(repository).is_clean)
            status = engine.project_plan(project)
            self.assertEqual("milestone_ready_for_merge", status["current_state"])
            self.assertEqual("human_merge_approval", status["next_action"])
            self.assertEqual("human_merge_approval", status["workflow_type"])
            self.assertIsNone(status["required_lease"])
            self.assertFalse(status["application_mutation_expected"])
            self.assertEqual([], status["sessions_that_would_launch"])
            self.assertEqual(result["human_gate"], status["human_gate"])
            rerun = engine.run_project(project, "milestone")
            self.assertEqual("human_merge_approval", rerun["outcome"])
            self.assertEqual(result["human_gate"], rerun["human_gate"])
            self.assertEqual(
                launcher.actions,
                ["feature_cycle", "milestone_integration", "milestone_gate"],
            )

    def test_resume_after_verified_integration_does_not_repeat_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            engine.run_project(project, "one_feature")
            inspector = RepositoryInspector(repository)
            cycle = engine.cycle_store.read(inspector.cycle_state_path())
            cycle["current_phase"] = "feature_integrated"
            engine.cycle_store.write(inspector.cycle_state_path(), cycle)
            project_state = engine.load_project_state(project)
            project_state["current_state"] = "feature_accepted"
            engine.project_store.write(engine.project_state_path(project), project_state)
            result = engine.resume_project(project)
            self.assertEqual(result["outcome"], "milestone_ready_for_merge")
            self.assertEqual(
                launcher.actions,
                ["feature_cycle", "milestone_integration", "milestone_gate"],
            )
