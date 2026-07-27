from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.queue_control import (
    operator_paused,
    set_operator_paused,
)
from tests.helpers import (
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
)


class PauseControlTests(unittest.TestCase):
    def test_pause_while_idle_blocks_run_and_resume_without_application_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            before_head = git(repository, "rev-parse", "HEAD")
            before_status = git(repository, "status", "--porcelain=v1")
            paused = set_operator_paused(
                configuration,
                project,
                paused=True,
                active_transaction=None,
            )
            self.assertEqual("project_paused", paused["classification"])
            self.assertFalse(paused["pause_after_current"])
            for mode in ("one_feature", "resume"):
                result = engine.run_project(project, mode)
                self.assertEqual("project_paused", result["classification"])
                self.assertFalse(result["application_repository_written"])
                self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_status, git(repository, "status", "--porcelain=v1"))

    def test_pause_after_current_uses_the_same_operator_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            result = set_operator_paused(
                configuration,
                project,
                paused=True,
                active_transaction="transaction-1",
            )
            self.assertTrue(result["pause_after_current"])
            self.assertTrue(operator_paused(configuration, project))

    def test_unpause_clears_only_flag_and_does_not_start_a_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            set_operator_paused(
                configuration,
                project,
                paused=True,
                active_transaction=None,
            )
            before_head = git(repository, "rev-parse", "HEAD")
            before_status = git(repository, "status", "--porcelain=v1")
            result = set_operator_paused(
                configuration,
                project,
                paused=False,
                active_transaction=None,
            )
            self.assertEqual("project_unpaused", result["classification"])
            self.assertFalse(result["paused"])
            self.assertFalse(result["automatic_cycle_started"])
            self.assertFalse(operator_paused(configuration, project))
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_status, git(repository, "status", "--porcelain=v1"))

    def test_status_plan_reports_pause_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            set_operator_paused(
                configuration,
                project,
                paused=True,
                active_transaction=None,
            )
            state_path = configuration.root / "state/projects/synthetic.json"
            before = state_path.read_bytes()
            plan = engine.project_plan(project)
            self.assertTrue(plan["paused"])
            self.assertEqual("project_paused", plan["proposed_next_action"])
            self.assertEqual(before, state_path.read_bytes())

    def test_project_transition_write_cannot_lose_or_restore_a_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            state_path = configuration.root / "state/projects/synthetic.json"

            set_operator_paused(
                configuration,
                project,
                paused=True,
                active_transaction="transaction-1",
            )
            stale_paused = json.loads(state_path.read_text(encoding="utf-8"))
            stale_unpaused = dict(stale_paused)
            stale_unpaused["operator_paused"] = False
            engine._write_project_state_preserving_pause(project, stale_unpaused)
            self.assertTrue(operator_paused(configuration, project))

            set_operator_paused(
                configuration,
                project,
                paused=False,
                active_transaction=None,
            )
            engine._write_project_state_preserving_pause(project, stale_paused)
            self.assertFalse(operator_paused(configuration, project))
