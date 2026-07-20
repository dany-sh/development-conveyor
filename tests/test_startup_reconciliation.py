from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import InvalidTransition, RecoveryError
from development_conveyor.recovery import assess_startup_reconciliation
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan
from development_conveyor.state_machine import PORTFOLIO_MACHINE
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository, write_json


class PlanOnlyLauncher:
    def plan(self, request):
        return SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64, "read-only"
        )


class ObservingLauncher(SyntheticLauncher):
    def __init__(self):
        super().__init__()
        self.engine: CycleEngine | None = None
        self.state_at_feature_launch: str | None = None
        self.event_states_at_feature_launch: list[tuple[str | None, str | None]] = []
        self.reservation_at_feature_launch = False

    def launch(self, request, on_session_started=None):
        if on_session_started is not None:
            on_session_started(f"session-{len(self.actions) + 1}")
        if request.action == "feature_cycle":
            assert self.engine is not None
            document = self.engine.load_project_state(request.project)
            self.state_at_feature_launch = document["current_state"] if document else None
            inspector = RepositoryInspector(request.project.repository)
            self.reservation_at_feature_launch = self.engine._launch_lock(
                request.project, inspector
            ).status(request.run_id).exists
            event_path = self.engine.root / "logs/run-events.jsonl"
            if event_path.exists():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    self.event_states_at_feature_launch.append(
                        (event.get("previous_state"), event.get("next_state"))
                    )
        return super().launch(request, on_session_started=None)


class ContentionLauncher(SyntheticLauncher):
    def __init__(self):
        super().__init__()
        self.competitor: CycleEngine | None = None
        self.competitor_result = None
        self.competitor_wrote_state = None

    def launch(self, request, on_session_started=None):
        if on_session_started is not None:
            on_session_started(f"session-{len(self.actions) + 1}")
        if request.action == "feature_cycle" and self.competitor_result is None:
            assert self.competitor is not None
            project_path = self.competitor.project_state_path(request.project)
            cycle_path = RepositoryInspector(request.project.repository).cycle_state_path()
            before = (project_path.read_bytes(), cycle_path.read_bytes())
            self.competitor_result = self.competitor.run_project(request.project, "one_feature")
            after = (project_path.read_bytes(), cycle_path.read_bytes())
            self.competitor_wrote_state = before != after
        return super().launch(request, on_session_started=None)


class StartupReconciliationTests(unittest.TestCase):
    def _engine(self, root: Path, *, status: str = "ready", launcher=None):
        repository, project = synthetic_repository(root, feature_status=status)
        engine = CycleEngine(controller_configuration(root, project), launcher or PlanOnlyLauncher())
        return repository, project, engine

    @staticmethod
    def _persist_state(
        engine: CycleEngine,
        project,
        state: str,
        *,
        feature=None,
        evidence=None,
        decision=None,
        checkpoint="prior_state_decision",
    ):
        inspector = RepositoryInspector(project.repository)
        document = engine._project_document(project, "prior-run", inspector.identity()["path_fingerprint"])
        document.update({
            "current_state": state,
            "current_feature": feature,
            "last_checkpoint": checkpoint,
            "human_decision_required": decision,
            "state_evidence": evidence,
        })
        engine.project_store.write(engine.project_state_path(project), document)
        return document

    @staticmethod
    def _add_ready_feature(repository: Path, feature_id: str = "F002", *, priority: int = 2):
        path = repository / "docs/FEATURE_QUEUE.yaml"
        queue = json.loads(path.read_text(encoding="utf-8"))
        queue["features"].append({
            "id": feature_id,
            "title": f"Synthetic {feature_id}",
            "status": "ready",
            "priority": priority,
            "milestone": "M0",
            "dependencies": [],
            "spec": f"docs/features/{feature_id}.md",
            "acceptance_criteria": [f"{feature_id} is integrated"],
            "requires_human_decision": False,
            "branch": None,
            "integration_base_commit": None,
            "accepted_commit": None,
            "integrated_commit": None,
            "integration_status": "pending",
            "integration_fix_commits": [],
        })
        write_json(path, queue)
        (repository / f"docs/features/{feature_id}.md").write_text(f"# {feature_id}\n", encoding="utf-8")
        git(repository, "add", "docs")
        git(repository, "commit", "-m", f"test: add {feature_id}")

    def test_milestone_gate_with_ready_work_derives_repair_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary))
            self._persist_state(engine, project, "milestone_gate")
            plan = engine.run_project(project, "one_feature", dry_run=True)
            self.assertEqual(plan["state_consistency"], "stale_state_repaired")
            self.assertEqual(plan["persisted_state"], "milestone_gate")
            self.assertEqual(plan["derived_state"], "feature_ready")
            self.assertEqual(
                plan["repair_transition_path"],
                ["milestone_gate", "queue_reconciliation", "feature_ready"],
            )

    def test_direct_milestone_gate_to_feature_running_remains_rejected(self):
        with self.assertRaisesRegex(InvalidTransition, "milestone_gate -> feature_running"):
            PORTFOLIO_MACHINE.transition("milestone_gate", "feature_running")

    def test_real_run_persists_repair_before_feature_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher = ObservingLauncher()
            _, project, engine = self._engine(Path(temporary), launcher=launcher)
            launcher.engine = engine
            self._persist_state(engine, project, "milestone_gate")
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            # Compatibility state remains at the last completed kernel
            # projection until the feature transaction terminalizes.
            self.assertEqual(launcher.state_at_feature_launch, "feature_ready")
            self.assertTrue(launcher.reservation_at_feature_launch)
            self.assertIn(("milestone_gate", "queue_reconciliation"), launcher.event_states_at_feature_launch)
            self.assertIn(("queue_reconciliation", "feature_ready"), launcher.event_states_at_feature_launch)
            self.assertNotIn(("feature_ready", "feature_running"), launcher.event_states_at_feature_launch)

    def test_competing_controller_cannot_write_during_feature_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            configuration = controller_configuration(root, project)
            launcher = ContentionLauncher()
            engine = CycleEngine(configuration, launcher)
            launcher.competitor = CycleEngine(configuration, PlanOnlyLauncher())
            self._persist_state(engine, project, "milestone_gate")
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            self.assertEqual(launcher.competitor_result["outcome"], "kernel_recovery_required")
            self.assertFalse(launcher.competitor_wrote_state)
            self.assertEqual(git(repository, "branch", "--show-current"), project.milestone_branch)

    def test_dry_run_reports_repair_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            self._persist_state(engine, project, "milestone_gate")
            state_path = engine.project_state_path(project)
            before_state = state_path.read_bytes()
            before_repo = git(repository, "status", "--porcelain=v1", "--branch")
            plan = engine.run_project(project, "one_feature", dry_run=True)
            self.assertTrue(plan["would_persist_state_repair"])
            self.assertEqual(before_state, state_path.read_bytes())
            self.assertEqual(before_repo, git(repository, "status", "--porcelain=v1", "--branch"))
            self.assertFalse((engine.root / "state/launch-locks").exists())

    def test_orphaned_prelaunch_cycle_is_not_resumed_or_modified_by_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            self._persist_state(engine, project, "milestone_gate")
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            cycle = engine._new_cycle_state(project, "failed-prelaunch", inspector, queue["features"][0])
            path = inspector.cycle_state_path()
            engine.cycle_store.write(path, cycle)
            engine._advance_cycle(path, cycle, "preflight", inspector, "preflight_verified")
            engine._advance_cycle(path, cycle, "feature_selected", inspector, "selected")
            engine._advance_cycle(
                path, cycle, "branch_preparing", inspector, "repository_session_owns_branch_preparation"
            )
            before_cycle = path.read_bytes()
            result = engine.reconcile_controller_state(project, dry_run=False)
            self.assertTrue(result["state_recovered"])
            self.assertEqual(before_cycle, path.read_bytes())
            plan = engine.project_plan(project)
            self.assertIsNone(plan["existing_active_cycle"])
            self.assertFalse(plan["lock_status"]["controller_launch"]["exists"])
            self.assertFalse(plan["lock_status"]["repository_writer"]["exists"])

    def test_orphan_repair_rejects_cycle_change_after_assessment(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            persisted = self._persist_state(engine, project, "milestone_gate")
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            cycle = engine._new_cycle_state(project, "failed-prelaunch", inspector, queue["features"][0])
            path = inspector.cycle_state_path()
            engine.cycle_store.write(path, cycle)
            engine._advance_cycle(path, cycle, "preflight", inspector, "preflight_verified")
            engine._advance_cycle(path, cycle, "feature_selected", inspector, "selected")
            engine._advance_cycle(
                path, cycle, "branch_preparing", inspector, "repository_session_owns_branch_preparation"
            )
            plan = engine.project_plan(project)
            assessment = assess_startup_reconciliation(project, persisted, plan)
            cycle["current_phase"] = "feature_in_progress"
            cycle["last_successful_checkpoint"] = "external_session_started"
            engine.cycle_store.write(path, cycle)
            with self.assertRaisesRegex(RecoveryError, "evidence changed"):
                engine._persist_startup_reconciliation(
                    project, persisted, assessment, run_id="racing-repair"
                )
            self.assertEqual(engine.load_project_state(project)["current_state"], "milestone_gate")

    def test_extra_worktree_prevents_orphan_cycle_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine = self._engine(root)
            self._persist_state(engine, project, "milestone_gate")
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            cycle = engine._new_cycle_state(project, "failed-prelaunch", inspector, queue["features"][0])
            path = inspector.cycle_state_path()
            engine.cycle_store.write(path, cycle)
            engine._advance_cycle(path, cycle, "preflight", inspector, "preflight_verified")
            engine._advance_cycle(path, cycle, "feature_selected", inspector, "selected")
            engine._advance_cycle(
                path, cycle, "branch_preparing", inspector, "repository_session_owns_branch_preparation"
            )
            worktree = root / "other-worktree"
            git(repository, "worktree", "add", "-b", "codex/unrelated-worktree", str(worktree), "main")
            plan = engine.project_plan(project)
            self.assertIsNotNone(plan["existing_active_cycle"])
            self.assertEqual(plan["state_consistency"], "active_cycle_resume")

    def test_milestone_ready_requires_timestamped_changed_gate_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            previous = git(repository, "rev-parse", "HEAD")
            self._add_ready_feature(repository)
            self._persist_state(
                engine, project, "milestone_ready_for_merge", evidence={"milestone_head": previous}
            )
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "human_decision_required")
            self.assertFalse(plan["would_persist_state_repair"])

    def test_fabricated_gate_evidence_cannot_clear_merge_ready_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            previous = git(repository, "rev-parse", "HEAD")
            self._add_ready_feature(repository)
            self._persist_state(engine, project, "milestone_ready_for_merge", evidence={
                "milestone_head": previous,
                "gate_evidence_commit": previous,
                "queue_fingerprint": "0" * 64,
                "gate_evidence_timestamp": "2026-01-01T00:00:00+00:00",
            })
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "human_decision_required")
            self.assertFalse(plan["state_reconciliation_evidence"]["gate_evidence_provenance_valid"])

    def test_changed_milestone_head_invalidates_gate_when_policy_allows_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            previous = git(repository, "rev-parse", "HEAD")
            self._add_ready_feature(repository)
            self._persist_state(engine, project, "milestone_ready_for_merge", evidence={
                "milestone_head": previous,
                "gate_evidence_commit": previous,
                "queue_fingerprint": "0" * 64,
                "gate_evidence_timestamp": "2026-01-01T00:00:00+00:00",
            }, decision={
                "milestone_branch": project.milestone_branch,
                "default_branch_merge_performed": False,
            }, checkpoint="milestone_gate_passed")
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "stale_state_repaired")
            self.assertEqual(
                plan["repair_transition_path"],
                ["milestone_ready_for_merge", "queue_reconciliation", "feature_ready"],
            )

    def test_clean_descendant_side_branch_cannot_reopen_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            previous = git(repository, "rev-parse", "HEAD")
            self._persist_state(engine, project, "milestone_ready_for_merge", evidence={
                "milestone_head": previous,
                "queue_fingerprint": "0" * 64,
                "gate_evidence_timestamp": "2026-01-01T00:00:00+00:00",
            })
            git(repository, "switch", "-c", "codex/side-branch")
            (repository / "side.txt").write_text("side\n", encoding="utf-8")
            git(repository, "add", "side.txt")
            git(repository, "commit", "-m", "test: descendant side branch")
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "human_decision_required")
            self.assertFalse(plan["would_persist_state_repair"])

    def test_complete_milestone_remains_at_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary), status="done")
            self._persist_state(engine, project, "milestone_gate")
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "state_consistent")
            self.assertEqual(plan["derived_state"], "milestone_gate")

    def test_repeated_reconciliation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary))
            self._persist_state(engine, project, "milestone_gate")
            first = engine.reconcile_controller_state(project, dry_run=False)
            second = engine.reconcile_controller_state(project, dry_run=False)
            self.assertTrue(first["state_recovered"])
            self.assertFalse(second["state_recovered"])
            self.assertEqual(second["state_consistency"], "state_consistent")
            self.assertEqual(second["current_state"], "feature_ready")

    def test_resume_command_repairs_stale_state_without_launching_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary))
            self._persist_state(engine, project, "milestone_gate")
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            cycle = engine._new_cycle_state(project, "failed-prelaunch", inspector, queue["features"][0])
            path = inspector.cycle_state_path()
            engine.cycle_store.write(path, cycle)
            engine._advance_cycle(path, cycle, "preflight", inspector, "preflight_verified")
            engine._advance_cycle(path, cycle, "feature_selected", inspector, "selected")
            engine._advance_cycle(
                path, cycle, "branch_preparing", inspector, "repository_session_owns_branch_preparation"
            )
            result = engine.run_project(project, "resume")
            self.assertEqual(result["outcome"], "state_repaired")
            self.assertEqual(result["current_state"], "feature_ready")
            self.assertEqual(result["selected_feature"], "F001")
            repeated = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual(repeated["proposed_next_action"], "feature_cycle")

    def test_live_controller_reservation_prevents_racing_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary))
            original = self._persist_state(engine, project, "milestone_gate")
            inspector = RepositoryInspector(project.repository)
            lock = engine._launch_lock(project, inspector)
            from development_conveyor.locks import make_lock_record

            lock.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id="other-controller",
                current_feature=None,
                current_phase="startup_reconciliation",
            ))
            try:
                with self.assertRaises(RecoveryError):
                    engine.reconcile_controller_state(project, dry_run=False)
                self.assertEqual(engine.load_project_state(project)["updated_at"], original["updated_at"])
            finally:
                lock.release("other-controller")

    def test_completed_p0_002_is_preserved_while_p0_001_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary), status="done")
            path = repository / project.queue_location
            queue = json.loads(path.read_text(encoding="utf-8"))
            queue["features"][0]["id"] = "P0-002"
            queue["features"][0]["spec"] = "docs/features/P0-002.md"
            (repository / "docs/features/F001.md").rename(repository / "docs/features/P0-002.md")
            queue["features"].append({
                **queue["features"][0],
                "id": "P0-001",
                "title": "Ready P0 feature",
                "status": "ready",
                "spec": "docs/features/P0-001.md",
            })
            (repository / "docs/features/P0-001.md").write_text("# P0-001\n", encoding="utf-8")
            write_json(path, queue)
            git(repository, "add", "docs")
            git(repository, "commit", "-m", "test: preserve P0-002 and ready P0-001")
            self._persist_state(engine, project, "milestone_gate")
            plan = engine.project_plan(project)
            self.assertIn("P0-002", plan["queue_status"]["completed_features"])
            self.assertEqual(plan["selected_feature"], "P0-001")

    def test_one_feature_mode_stops_with_second_feature_still_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            self._add_ready_feature(repository, priority=2)
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(Path(temporary), project), launcher)
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "one_feature_integrated")
            self.assertEqual(launcher.actions, ["feature_cycle", "milestone_integration"])
            queue = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            self.assertEqual(queue["features"][1]["status"], "ready")

    def test_feature_ready_blocked_routes_through_queue_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary), status="blocked")
            self._persist_state(engine, project, "feature_ready")
            plan = engine.project_plan(project)
            self.assertEqual(
                plan["repair_transition_path"],
                ["feature_ready", "queue_reconciliation", "paused"],
            )

    def test_stale_running_state_with_completed_integrated_cycle_is_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            engine = CycleEngine(controller_configuration(root, project), SyntheticLauncher())
            result = engine.run_project(project, "one_feature")
            project = replace(
                project,
                last_accepted_feature="F001 Synthetic Feature",
                last_accepted_commit=result["accepted_commit"],
            )
            # The kernel integration transaction already canonicalizes accepted
            # commit evidence; no compatibility-only follow-up commit is needed.
            document = engine.load_project_state(project)
            document.update({"current_state": "feature_running", "current_feature": "F001"})
            engine.project_store.write(engine.project_state_path(project), document)
            plan = engine.project_plan(project)
            self.assertEqual(plan["stale_cycle_evidence"]["classification"], "completed_cycle_evidence")
            self.assertEqual(
                plan["repair_transition_path"],
                ["feature_running", "feature_accepted", "queue_reconciliation", "milestone_gate"],
            )
            self.assertEqual(git(repository, "branch", "--show-current"), project.milestone_branch)

    def test_resumed_milestone_gate_launches_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = SyntheticLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            engine.run_project(project, "one_feature")
            inspector = RepositoryInspector(repository)
            cycle = engine.cycle_store.read(inspector.cycle_state_path())
            cycle["current_phase"] = "milestone_gate"
            cycle["last_successful_checkpoint"] = "milestone_gate_launch"
            engine.cycle_store.write(inspector.cycle_state_path(), cycle)
            launcher.actions.clear()
            result = engine.resume_project(project)
            self.assertEqual(result["outcome"], "milestone_ready_for_merge")
            self.assertEqual(launcher.actions, ["milestone_gate"])

    def test_bare_resolution_marker_cannot_bypass_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary))
            self._persist_state(
                engine, project, "human_decision_required", decision={"resolved": True}
            )
            human_plan = engine.project_plan(project)
            self.assertEqual(human_plan["state_consistency"], "human_decision_required")
            self.assertEqual(human_plan["repair_transition_path"], [])
            self._persist_state(engine, project, "validation_failed")
            validation_plan = engine.project_plan(project)
            self.assertEqual(
                validation_plan["repair_transition_path"],
                ["validation_failed", "queue_reconciliation", "feature_ready"],
            )

    def test_contradictory_ready_evidence_requires_human_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine = self._engine(Path(temporary))
            persisted = self._persist_state(engine, project, "milestone_gate")
            plan = engine.project_plan(project)
            plan["selected_feature"] = None
            assessment = assess_startup_reconciliation(project, persisted, plan)
            self.assertEqual(assessment.classification, "invalid_state_evidence")
            self.assertEqual(assessment.derived_state, "human_decision_required")

    def test_invalid_cycle_without_controller_state_is_not_hidden_by_configured_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine = self._engine(Path(temporary), status="proposed")
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            inspector.cycle_state_path().write_text("{invalid", encoding="utf-8")
            plan = engine.project_plan(project)
            self.assertEqual(plan["state_consistency"], "invalid_state_evidence")
            self.assertEqual(plan["proposed_next_action"], "human_decision_required")


if __name__ == "__main__":
    unittest.main()
