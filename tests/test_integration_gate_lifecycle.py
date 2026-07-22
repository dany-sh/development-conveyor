from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.errors import QueueError
from development_conveyor.sessions import SessionLauncher, SessionPlan, SessionRequest, SessionResult
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


def assistant(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


class Compatibility:
    def as_dict(self):
        return {
            "compatible": True,
            "policy_role": "milestone-integrator",
            "effective_model": "gpt-5.6-sol",
            "effective_reasoning": "high",
        }


class NoLaunchLauncher:
    def __init__(self):
        self.compatibility_actions = []
        self.launches = []

    def compatibility(self, action, project_id=None):
        self.compatibility_actions.append((action, project_id))
        return Compatibility()

    def plan(self, request):
        return SessionPlan(("codex", "exec"), request.project.repository, "synthetic", "0" * 64, "read-only")

    def launch(self, request):
        self.launches.append(request.action)
        raise AssertionError("resolution and reconciliation must not launch a repository session")


class HistoricalIntegrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository, self.project = synthetic_repository(self.root)
        self.previous = git(self.repository, "rev-parse", "HEAD")

        queue_path = self.repository / self.project.queue_location
        queue = json.loads(queue_path.read_text())
        queue["milestones"][0]["last_validated_commit"] = self.previous
        queue["features"].append({
            "id": "P0-003", "title": "Must remain unstarted", "status": "ready", "priority": 20,
            "milestone": "M0", "dependencies": [], "spec": "docs/features/P0-003.md",
            "acceptance_criteria": ["Remains unstarted while F001 awaits integration."],
            "requires_human_decision": False, "integration_status": "pending",
        })
        write_json(queue_path, queue)
        (self.repository / "docs/features/P0-003.md").write_text("# P0-003\n")
        (self.repository / "docs/FEATURE_CATALOG.md").write_text("F001\nP0-003\n")
        (self.repository / "docs/ROADMAP.md").write_text("M0 F001 P0-003\n")
        (self.repository / "docs/CURRENT_STATUS.md").write_text("M0 F001 is next.\n")
        (self.repository / "docs/architecture.md").write_text("# Architecture planning\n")
        (self.repository / "docs/RUN_LOG.md").write_text(
            "M0 feature-inventory reconciliation\nClassified the bootstrap queue as incomplete.\n"
        )
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "docs(factory): reconcile M0 feature inventory")
        self.candidate = git(self.repository, "rev-parse", "HEAD")

        self.feature_branch = "codex/f001-synthetic-feature"
        git(self.repository, "switch", "-c", self.feature_branch)
        queue = json.loads(queue_path.read_text())
        queue["features"][0].update({
            "status": "integration_pending", "branch": self.feature_branch,
            "integration_base_commit": self.candidate, "accepted_commit": "SELF",
            "integration_status": "pending",
            "acceptance": {"tests_passed": True, "review_passed": True, "documentation_current": True},
        })
        write_json(queue_path, queue)
        (self.repository / "app.txt").write_text("baseline\nF001 accepted\n")
        git(self.repository, "add", "app.txt", self.project.queue_location)
        git(self.repository, "commit", "-m", "F001: accept synthetic integration feature")
        self.accepted = git(self.repository, "rev-parse", "HEAD")

        self.launcher = NoLaunchLauncher()
        self.engine = CycleEngine(controller_configuration(self.root, self.project), self.launcher)
        inspector = RepositoryInspector(self.repository)
        exclude = inspector.common_git_dir / "info/exclude"
        existing = exclude.read_text() if exclude.exists() else ""
        exclude.write_text(existing + ".factory/conveyor-state.json\n.factory/locks/writer.json\n")
        feature = json.loads(queue_path.read_text())["features"][0]
        cycle = self.engine._new_cycle_state(self.project, "synthetic-integration-run", inspector, feature)
        cycle.update({
            "current_phase": "integrating", "feature_worktree": str(self.repository),
            "accepted_feature_commit": self.accepted,
            "integration_session_id": None,
            "last_successful_checkpoint": "integration_session_launch",
        })
        self.engine.cycle_store.write(inspector.cycle_state_path(), cycle)
        project_state = self.engine._project_document(
            self.project, "synthetic-integration-run", inspector.identity()["path_fingerprint"]
        )
        project_state.update({"current_state": "feature_running", "current_feature": "F001"})
        self.engine.project_store.write(self.engine.project_state_path(self.project), project_state)

        report = self.engine.configuration.owned_path("reports") / "synthetic-integration-run" / "milestone_integration.json"
        write_json(report, {
            "schema_version": 1, "session_id": "synthetic-integration-session",
            "redacted_stdout": assistant("## HUMAN_DECISION_REQUIRED"),
        })
        self.report = report
        self.report_bytes = report.read_bytes()

        from development_conveyor import reporting
        self.pin = mock.patch.dict(reporting.LEGACY_PLANNING_GATE_PIN, {
            "project_id": "synthetic",
            "run_id": "synthetic-integration-run",
            "integration_session_id": "synthetic-integration-session",
            "accepted_commit": self.accepted,
            "candidate_commit": self.candidate,
        }, clear=True)
        self.pin.start()

    def tearDown(self):
        self.pin.stop()
        self.temporary.cleanup()

    def _resolution_process(self, fingerprint="f" * 64):
        original_run = subprocess.run
        def run(argv, *args, **kwargs):
            if any("discover-integration" in str(item) for item in argv):
                return subprocess.CompletedProcess(
                    argv, 0,
                    json.dumps({
                        "planning_baseline_provenance": {
                            "valid": True, "commit": self.candidate,
                            "previous_validated_commit": self.previous,
                            "approval_required": True,
                            "approved_by_explicit_resolution": False,
                            "evidence_fingerprint": fingerprint,
                        }
                    }), "",
                )
            return original_run(argv, *args, **kwargs)
        return mock.patch(
            "development_conveyor.human_resolution.subprocess.run",
            side_effect=run,
        )

    def test_historical_gate_dry_run_persistence_status_and_idempotency(self):
        cycle_path = self.repository / ".factory/conveyor-state.json"
        project_path = self.engine.project_state_path(self.project)
        exclude = Path(git(self.repository, "rev-parse", "--git-common-dir")) / "info/exclude"
        if not exclude.is_absolute():
            exclude = self.repository / exclude
        before = {
            "cycle": cycle_path.read_bytes(), "project": project_path.read_bytes(),
            "report": self.report.read_bytes(), "exclude": exclude.read_bytes(),
        }
        dry = self.engine.reconcile_controller_state(self.project, dry_run=True)
        self.assertEqual(dry["classification"], "integration_human_gate_recovery")
        self.assertFalse(dry["ordinary_resume_allowed"])
        self.assertEqual(before, {
            "cycle": cycle_path.read_bytes(), "project": project_path.read_bytes(),
            "report": self.report.read_bytes(), "exclude": exclude.read_bytes(),
        })

        applied = self.engine.reconcile_controller_state(self.project, dry_run=False)
        self.assertTrue(applied["gate_persisted"])
        cycle = self.engine.cycle_store.read(cycle_path)
        project = self.engine.load_project_state(self.project)
        self.assertEqual((cycle["current_phase"], project["current_state"]), ("human_decision_required", "human_decision_required"))
        self.assertEqual((cycle["accepted_feature_commit"], cycle["feature_starting_commit"]), (self.accepted, self.candidate))
        status = self.engine.project_plan(self.project)
        self.assertEqual((status["feature_status"], status["integration_status"]), ("accepted", "blocked"))
        self.assertFalse(status["ordinary_resume_allowed"])
        self.assertFalse(status["feature_factory_would_launch"])
        live_feature_head = git(self.repository, "rev-parse", "HEAD")
        self.assertNotEqual(live_feature_head, self.candidate)
        self.assertEqual(
            (
                status["selected_feature"],
                status["accepted_feature_commit"],
                status["feature_starting_commit"],
                status["feature_branch"],
                status["milestone_branch"],
                status["next_feature_selection"]["selected_feature_starting_commit"],
            ),
            (
                "F001",
                self.accepted,
                self.candidate,
                self.feature_branch,
                self.project.milestone_branch,
                self.candidate,
            ),
        )
        self.assertEqual(self.report.read_bytes(), self.report_bytes)

        persisted_bytes = (cycle_path.read_bytes(), project_path.read_bytes(), self.report.read_bytes(), exclude.read_bytes())
        replay = self.engine.reconcile_controller_state(self.project, dry_run=False)
        self.assertEqual(
            replay["classification"],
            "projection_compatibility_cache_reconciliation",
        )
        self.assertTrue(replay["controller_state_written"])
        self.assertEqual(
            persisted_bytes[0], cycle_path.read_bytes()
        )
        self.assertEqual(persisted_bytes[2], self.report.read_bytes())
        self.assertEqual(persisted_bytes[3], exclude.read_bytes())
        reconciled_bytes = project_path.read_bytes()
        idempotent = self.engine.reconcile_controller_state(self.project, dry_run=False)
        self.assertFalse(idempotent["controller_state_written"])
        self.assertEqual(reconciled_bytes, project_path.read_bytes())
        self.assertEqual(self.launcher.launches, [])

    def test_exact_resolution_idempotency_and_integrator_only_routing(self):
        self.engine.reconcile_controller_state(self.project, dry_run=False)
        gate = self.engine.load_project_state(self.project)["human_decision_required"]
        reason = f"Approve {self.candidate} as the validated M0 planning baseline for integration of accepted F001."
        cycle_path = self.repository / ".factory/conveyor-state.json"
        before = (cycle_path.read_bytes(), self.engine.project_state_path(self.project).read_bytes(), self.report.read_bytes())
        with self._resolution_process():
            dry = self.engine.resolve_human_decision(self.project, reason=reason, dry_run=True)
        self.assertTrue(dry["resolution_would_be_accepted"])
        self.assertFalse(dry["applied"])
        self.assertEqual(before, (cycle_path.read_bytes(), self.engine.project_state_path(self.project).read_bytes(), self.report.read_bytes()))

        with self._resolution_process():
            applied = self.engine.resolve_human_decision(self.project, reason=reason, dry_run=False)
        self.assertEqual(applied["outcome"], "resolution_accepted")
        self.assertFalse(applied["session_launched_during_resolution"])
        self.assertFalse(applied["product_architect_would_launch"])
        self.assertFalse(applied["feature_inventory_would_launch"])
        cycle = self.engine.cycle_store.read(cycle_path)
        self.assertEqual(cycle["current_phase"], "integration_ready")
        self.assertEqual(cycle["accepted_feature_commit"], self.accepted)
        self.assertEqual(cycle["feature_starting_commit"], self.candidate)
        self.assertEqual(cycle["validated_planning_baseline"]["evidence_fingerprint"], "f" * 64)
        self.assertEqual(cycle["prior_integration_session_ids"], ["synthetic-integration-session"])
        self.assertIsNone(cycle["integration_session_id"])
        plan = self.engine.project_plan(self.project)
        self.assertEqual(plan["proposed_next_action"], "milestone_integration")
        self.assertEqual(plan["selected_feature"], "F001")
        self.assertNotEqual(plan["selected_feature"], "P0-003")
        self.assertEqual(
            plan["sessions_that_would_launch"],
            [],
        )
        self.assertEqual(self.launcher.compatibility_actions[-1], ("milestone_integration", "synthetic"))
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(self.report.read_bytes(), self.report_bytes)

        real_launcher = SessionLauncher(self.engine.root, self.engine.configuration.conveyor)
        trusted_environment = real_launcher._trusted_resolution_environment(SessionRequest(
            action="milestone_integration", project=self.project,
            run_id="synthetic-integration-run", mode="resume", feature="F001",
        ))
        self.assertIn("CONVEYOR_CONTROLLER_REPORT_ROOT", trusted_environment)
        self.assertIn(applied["resolution_id"], trusted_environment["CONVEYOR_PLANNING_RESOLUTION_REPORT"])

        with self._resolution_process():
            replay = self.engine.resolve_human_decision(self.project, reason=reason, dry_run=False)
        self.assertEqual(replay["outcome"], "already_resolved")
        self.assertEqual(replay["resolution_id"], applied["resolution_id"])
        self.assertEqual(replay["next_normal_action"], "milestone_integration")

        document = self.engine.load_project_state(self.project)
        document.update({
            "current_state": "human_decision_required",
            "human_decision_required": {"gate_id": "later-gate", "classification": "product_decision", "reason": "Later decision"},
        })
        self.engine.project_store.write(self.engine.project_state_path(self.project), document)
        with self._resolution_process():
            raced = self.engine.resolve_human_decision(self.project, reason=reason, dry_run=False)
        self.assertEqual(raced["outcome"], "resolution_rejected")
        self.assertEqual(self.engine.load_project_state(self.project)["human_decision_required"]["gate_id"], "later-gate")
        self.assertEqual(len(self.engine.load_project_state(self.project)["human_decision_history"]), 1)
        self.assertEqual(gate["accepted_feature_commit"], self.accepted)

    def test_partial_apply_replay_repairs_cycle_without_launching(self):
        self.engine.reconcile_controller_state(self.project, dry_run=False)
        reason = f"Approve {self.candidate} as the validated M0 planning baseline for integration of accepted F001."
        original_write = self.engine.cycle_store.write
        failed = {"value": False}

        def fail_once(path, value):
            if value.get("current_phase") == "integration_ready" and not failed["value"]:
                failed["value"] = True
                raise OSError("injected cycle write failure")
            return original_write(path, value)

        with self._resolution_process(), mock.patch.object(self.engine.cycle_store, "write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected cycle write failure"):
                self.engine.resolve_human_decision(self.project, reason=reason, dry_run=False)
        self.assertEqual(self.engine.load_project_state(self.project)["current_state"], "queue_reconciliation")
        self.assertEqual(self.engine.cycle_store.read(self.repository / ".factory/conveyor-state.json")["current_phase"], "human_decision_required")

        with self._resolution_process():
            repaired = self.engine.resolve_human_decision(self.project, reason=reason, dry_run=False)
        self.assertEqual(repaired["outcome"], "already_resolved")
        self.assertEqual(self.engine.cycle_store.read(self.repository / ".factory/conveyor-state.json")["current_phase"], "integration_ready")
        self.assertEqual(self.launcher.launches, [])

    def test_all_terminal_classifications_have_explicit_non_generic_cycle_behavior(self):
        cycle_path = self.repository / ".factory/conveyor-state.json"
        project_path = self.engine.project_state_path(self.project)
        initial_cycle = cycle_path.read_bytes()
        initial_project = project_path.read_bytes()
        plan = SessionPlan(("codex", "exec"), self.repository, "synthetic", "0" * 64, "workspace-write")
        expected = {
            "VALIDATION_FAILED": "integration_validation_failed",
            "SEMANTIC_CONFLICT": "human_decision_required",
            "RETRYABLE_INTEGRATION_FAILURE": "retryable_integration_failure",
            "TERMINAL_INTEGRATION_FAILURE": "terminal_integration_failure",
        }
        for classification, outcome in expected.items():
            with self.subTest(classification=classification):
                cycle_path.write_bytes(initial_cycle)
                project_path.write_bytes(initial_project)
                state = self.engine.cycle_store.read(cycle_path)
                result = SessionResult(
                    "milestone_integration", 0, "terminal-session", classification, plan,
                    redacted_stdout=classification,
                    structured_result={"schema_version": 1, "classification": classification},
                    structured_output_validation="valid", result_classification=classification,
                    retryable=classification == "RETRYABLE_INTEGRATION_FAILURE",
                    retry_hypothesis="changed integration hypothesis" if classification == "RETRYABLE_INTEGRATION_FAILURE" else None,
                    remediation_action="perform a different focused repair" if classification == "RETRYABLE_INTEGRATION_FAILURE" else None,
                    retry_evidence="repository-scoped validation evidence" if classification == "RETRYABLE_INTEGRATION_FAILURE" else None,
                )
                observed = self.engine._reconcile_feature_evidence(
                    self.project, RepositoryInspector(self.repository), cycle_path, state, result,
                    retry_status={"attempts_consumed": 0, "attempts_remaining": 1, "exhausted": False},
                )
                self.assertEqual(observed["outcome"], outcome)

        cycle_path.write_bytes(initial_cycle)
        project_path.write_bytes(initial_project)
        state = self.engine.cycle_store.read(cycle_path)
        integrated = SessionResult(
            "milestone_integration", 0, "terminal-session", "INTEGRATED", plan,
            structured_result={"schema_version": 1, "classification": "INTEGRATED"},
            structured_output_validation="valid", result_classification="INTEGRATED",
        )
        with self.assertRaisesRegex(QueueError, "not corroborated"):
            self.engine._reconcile_feature_evidence(
                self.project, RepositoryInspector(self.repository), cycle_path, state, integrated
            )

        cycle_path.write_bytes(initial_cycle)
        project_path.write_bytes(initial_project)
        state = self.engine.cycle_store.read(cycle_path)
        descriptor = {
            "schema_version": 1, "gate_classification": "synthetic_product_decision",
            "reason": "Synthetic approval is required.", "blocker_categories": ["product_decision"],
            "retryable": False,
        }
        human = SessionResult(
            "milestone_integration", 0, "terminal-session", "HUMAN_DECISION_REQUIRED", plan,
            structured_result={"schema_version": 1, "classification": "HUMAN_DECISION_REQUIRED", "human_decision": descriptor},
            structured_output_validation="valid", result_classification="HUMAN_DECISION_REQUIRED",
        )
        observed = self.engine._reconcile_feature_evidence(
            self.project, RepositoryInspector(self.repository), cycle_path, state, human
        )
        self.assertEqual(observed["outcome"], "human_decision_required")
        self.assertEqual(observed["human_decision"]["classification"], "synthetic_product_decision")


if __name__ == "__main__":
    unittest.main()
