from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ProjectionError
from development_conveyor.queue import FeatureQueue
from development_conveyor.recovery import assess_durable_integration_success
from development_conveyor.reporting import build_project_plan
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import classify_post_integration_commands
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


def assistant(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def command_event(command: str, exit_code: int | None, output: str = "") -> str:
    return json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": command,
            "aggregated_output": output,
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
        },
    })


class RecordingPlanLauncher:
    def __init__(self):
        self.actions: list[str] = []

    def plan(self, request):
        self.actions.append(request.action)
        from development_conveyor.sessions import SessionPlan
        return SessionPlan(("codex", "exec"), request.project.repository, "synthetic", "0" * 64, "read-only")


class DurableIntegrationFixture:
    def __init__(self, root: Path):
        self.repository, self.project = synthetic_repository(root)
        self.configuration = controller_configuration(root, self.project)
        self.launcher = RecordingPlanLauncher()
        self.engine = CycleEngine(self.configuration, self.launcher)
        exclude = self.repository / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8")
            + "\n.factory/conveyor-state.json\n.factory/runtime/\n.factory/locks/\n",
            encoding="utf-8",
        )
        queue_path = self.repository / self.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"].append({
            "id": "F002", "title": "Dependency-satisfied proposal", "status": "proposed",
            "priority": 2, "milestone": "M0", "dependencies": ["F001"],
            "spec": "docs/features/F002.md", "acceptance_criteria": ["F002 is planned"],
            "requires_human_decision": False, "branch": None, "integration_base_commit": None,
            "accepted_commit": None, "integrated_commit": None, "integration_status": "pending",
            "integration_fix_commits": [],
        })
        queue["features"].append({
            "id": "F003", "title": "Later gated proposal", "status": "proposed",
            "priority": 3, "milestone": "M0", "dependencies": ["F002"],
            "spec": "docs/features/F003.md", "acceptance_criteria": ["A later decision is made"],
            "requires_human_decision": True, "branch": None, "integration_base_commit": None,
            "accepted_commit": None, "integrated_commit": None, "integration_status": "pending",
            "integration_fix_commits": [],
        })
        (self.repository / "docs/features/F002.md").write_text("# F002\n", encoding="utf-8")
        (self.repository / "docs/features/F003.md").write_text("# F003\n", encoding="utf-8")
        write_json(queue_path, queue)
        git(self.repository, "add", "docs")
        git(self.repository, "commit", "-m", "docs: record M0 planning proposals")
        baseline = git(self.repository, "rev-parse", self.project.milestone_branch)
        branch = "codex/f001-synthetic-feature"
        git(self.repository, "switch", "-c", branch, baseline)
        (self.repository / "app.txt").write_text("baseline\nF001 integrated behavior\n", encoding="utf-8")
        git(self.repository, "add", "app.txt")
        git(self.repository, "commit", "-m", "F001: implement synthetic feature")
        self.accepted = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "switch", self.project.milestone_branch)
        git(self.repository, "commit", "--allow-empty", "-m", "factory: pre-integration checkpoint")
        self.pre_integration = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "cherry-pick", self.accepted)
        self.integrated = git(self.repository, "rev-parse", "HEAD")

        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        feature.update({
            "status": "integrating",
            "branch": branch,
            "integration_base_commit": baseline,
            "accepted_commit": self.accepted,
            "integrated_commit": None,
            "integration_status": "integrating",
            "acceptance": {"tests_passed": True, "review_passed": True, "documentation_current": True},
        })
        write_json(queue_path, queue)
        git(self.repository, "add", "docs/FEATURE_QUEUE.yaml")
        git(self.repository, "commit", "-m", "factory: mark F001 integrating")
        self.integrating_commit = git(self.repository, "rev-parse", "HEAD")
        self.validated_tree = self.integrating_commit
        self.integration_fix_commits: list[str] = []

        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integrated", "integrated_commit": self.integrated, "integration_status": "passed",
        })
        queue["milestones"][0].update({
            "integrated_features": ["F001"], "last_validated_commit": self.integrating_commit,
        })
        write_json(queue_path, queue)
        (self.repository / "docs/CURRENT_STATUS.md").write_text("# Status\n\nF001 integrated.\n", encoding="utf-8")
        (self.repository / "docs/RUN_LOG.md").write_text("# Run log\n\nF001 validation passed.\n", encoding="utf-8")
        git(self.repository, "add", "docs/CURRENT_STATUS.md", "docs/FEATURE_QUEUE.yaml", "docs/RUN_LOG.md")
        git(self.repository, "commit", "-m", "factory: record F001 integration passed")
        self.evidence_commit = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "docs/RUN_LOG.md").write_text(
            "# Run log\n\nF001 validation passed.\n\nModel evidence recorded.\n", encoding="utf-8"
        )
        git(self.repository, "add", "docs/RUN_LOG.md")
        git(self.repository, "commit", "-m", "factory: record F001 integration model-evidence")
        self.final_head = git(self.repository, "rev-parse", "HEAD")

        self.run_id = "durable-integration-run"
        self.session_id = "durable-integration-session"
        inspector = RepositoryInspector(self.repository)
        feature = FeatureQueue.from_location(self.repository, self.project.queue_location).feature("F001")
        cycle = self.engine._new_cycle_state(self.project, self.run_id, inspector, feature)
        cycle.update({
            "accepted_feature_commit": self.accepted,
            "milestone_pre_integration_commit": self.pre_integration,
            "milestone_post_integration_commit": self.final_head,
            "current_phase": "completed",
            "last_successful_checkpoint": "resumed_feature_cycle_complete",
            "stop_reason": "terminal_integration_failure",
            "failure_classification": "TERMINAL_INTEGRATION_FAILURE",
            "integration_status": "failed",
            "integration_session_id": self.session_id,
            "validation_attempts": [{"outcome": "passed", "commit": self.integrated}],
            "review_attempts": [{"outcome": "passed", "blocking_findings": 0}],
            "integration_attempts": [{"outcome": "passed", "integrated_commit": self.integrated}],
            "next_safe_action": "scripts/conveyor status --project synthetic",
        })
        self.engine.cycle_store.write(inspector.cycle_state_path(), cycle)
        state = self.engine._project_document(self.project, self.run_id, inspector.identity()["path_fingerprint"])
        state.update({
            "current_state": "validation_failed",
            "current_feature": "F001",
            "last_checkpoint": "integration_terminal_terminal_integration_failure",
            "state_evidence": {"integration_terminal_classification": "TERMINAL_INTEGRATION_FAILURE"},
        })
        self.engine.project_store.write(self.engine.project_state_path(self.project), state)
        self.write_runtime()
        self.write_report()

    def runtime_document(self) -> dict:
        inspector = RepositoryInspector(self.repository)
        return {
            "schema_version": 1,
            "phase": "complete",
            "accepted_commit": self.accepted,
            "resulting_feature_commit": self.integrated,
            "post_integration_head": self.integrated,
            "integrating_state_commit": self.integrating_commit,
            "evidence_commit": self.evidence_commit,
            "model_evidence_commit": self.final_head,
            "final_head": self.final_head,
            "stop_reason": "next_feature_selection",
            "lease_released": True,
            "integration_fix_commits": list(self.integration_fix_commits),
            "runtime_identity": {
                "repository": str(self.repository),
                "repository_identity": inspector.identity()["repository_id"],
                "project_id": self.project.project_id,
                "run_id": self.run_id,
                "milestone_id": "M0",
                "feature_id": "F001",
            },
            "plan": {
                "integration_fix_commits": list(self.integration_fix_commits),
                "validation": {"validated_commit": self.validated_tree},
            },
            "validation": {
                "ok": True,
                "clean_worktree": True,
                "validated_commit": (
                    self.validated_tree if self.integration_fix_commits else self.final_head
                ),
                "commands": [{
                    "group": "feature_queue",
                    "argv": ["python3", "/factory/feature-inventory/scripts/validate_inventory.py", "--root", str(self.repository)],
                    "exit_code": 0,
                }],
                "blockers": [],
            },
        }

    def write_runtime(self, mutate=None) -> None:
        value = self.runtime_document()
        if mutate:
            mutate(value)
        write_json(self.repository / ".factory/runtime/milestone-integration/latest.json", value)

    def report_document(self) -> dict:
        optional = command_event(
            "python3 scripts/validate_feature_inventory.py",
            2,
            "can't open file 'scripts/validate_feature_inventory.py': No such file or directory",
        )
        installed = command_event(
            f"python3 /factory/feature-inventory/scripts/validate_inventory.py --root {self.repository}", 0, "ok"
        )
        return {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "run_id": self.run_id,
            "action": "milestone_integration",
            "working_directory": str(self.repository),
            "exit_status": 0,
            "session_id": self.session_id,
            "result_classification": "INTEGRATED",
            "structured_output_validation": "valid",
            "redacted_stdout": "\n".join([optional, installed, assistant("Integration complete.\n\n## INTEGRATED")]),
        }

    def write_report(self, mutate=None) -> None:
        value = self.report_document()
        if mutate:
            mutate(value)
        write_json(self.configuration.root / "reports" / self.run_id / "milestone_integration.json", value)

    def rebuild_with_integration_fix(self) -> str:
        git(self.repository, "switch", "-c", "codex/f001-integration-fix", self.integrating_commit)
        (self.repository / "app.txt").write_text(
            "baseline\nF001 integrated behavior\nvalidated integration fix\n", encoding="utf-8"
        )
        git(self.repository, "add", "app.txt")
        git(self.repository, "commit", "-m", "fix: F001 integration repair")
        fix_commit = git(self.repository, "rev-parse", "HEAD")
        self.validated_tree = fix_commit
        self.integration_fix_commits = [fix_commit]

        queue_path = self.repository / self.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integrated",
            "integrated_commit": self.integrated,
            "integration_status": "passed",
            "integration_fix_commits": [fix_commit],
        })
        queue["milestones"][0].update({
            "integrated_features": ["F001"], "last_validated_commit": fix_commit,
        })
        write_json(queue_path, queue)
        (self.repository / "docs/CURRENT_STATUS.md").write_text("# Status\n\nF001 integrated.\n", encoding="utf-8")
        (self.repository / "docs/RUN_LOG.md").write_text("# Run log\n\nF001 fix validation passed.\n", encoding="utf-8")
        git(self.repository, "add", "docs/CURRENT_STATUS.md", "docs/FEATURE_QUEUE.yaml", "docs/RUN_LOG.md")
        git(self.repository, "commit", "-m", "factory: record F001 integration passed")
        self.evidence_commit = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "docs/RUN_LOG.md").write_text(
            "# Run log\n\nF001 fix validation passed.\n\nModel evidence recorded.\n", encoding="utf-8"
        )
        git(self.repository, "add", "docs/RUN_LOG.md")
        git(self.repository, "commit", "-m", "factory: record F001 integration model-evidence")
        self.final_head = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "branch", "-f", self.project.milestone_branch, self.final_head)
        git(self.repository, "switch", self.project.milestone_branch)

        inspector = RepositoryInspector(self.repository)
        cycle_path = inspector.cycle_state_path()
        cycle = self.engine.cycle_store.read(cycle_path)
        cycle["milestone_post_integration_commit"] = self.final_head
        cycle["last_verified_git_state"] = self.engine._git_checkpoint(inspector)
        self.engine.cycle_store.write(cycle_path, cycle)
        self.write_runtime()
        self.write_report()
        return fix_commit

    def assessment(self) -> dict:
        inspector = RepositoryInspector(self.repository)
        cycle = self.engine.cycle_store.read(inspector.cycle_state_path())
        queue = FeatureQueue.from_location(self.repository, self.project.queue_location)
        report = json.loads((self.configuration.root / "reports" / self.run_id / "milestone_integration.json").read_text())
        return assess_durable_integration_success(
            self.project, cycle, queue, inspector, report, writer_exists=False
        )

    def plan(self) -> dict:
        return build_project_plan(self.project, self.configuration.conveyor, self.configuration.root)


class PostIntegrationFinalizationTests(unittest.TestCase):
    def fixture(self, temporary: str) -> DurableIntegrationFixture:
        return DurableIntegrationFixture(Path(temporary))

    def test_01_required_integration_validator_failure_is_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.write_runtime(lambda value: value["validation"]["commands"][0].update({"exit_code": 1}))
            result = fixture.assessment()
            self.assertFalse(result["success"])
            self.assertFalse(result["checks"]["required_integration_commands_passed"])
            self.assertEqual(result["required_command_failures"][0]["category"], "required_validation")
            plan = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            self.assertEqual(plan["proposed_next_action"], "validation_failed")
            self.assertEqual(plan["derived_state"], "validation_failed")
            self.assertFalse(plan["would_persist_state_repair"])
            outcome = fixture.engine.run_project(fixture.project, "resume")
            self.assertEqual(outcome["outcome"], "validation_failed")
            self.assertEqual(outcome["current_state"], "validation_failed")

    def test_02_optional_diagnostic_failure_follows_durable_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.fixture(temporary).assessment()
            self.assertTrue(result["success"])
            self.assertIn("optional_diagnostic:unconfigured_repository_validator_missing:exit=2", result["optional_warnings"])

    def test_03_missing_unconfigured_repository_validator_is_non_authoritative(self):
        output = command_event(
            "python3 scripts/validate_feature_inventory.py", 2,
            "can't open file 'scripts/validate_feature_inventory.py': No such file or directory",
        )
        observations, warnings = classify_post_integration_commands(output)
        self.assertEqual(observations[0]["category"], "optional_diagnostic")
        self.assertEqual(observations[0]["effect"], "warning_only")
        self.assertEqual(warnings, ("optional_diagnostic:unconfigured_repository_validator_missing:exit=2",))

    def test_04_correct_installed_validator_passes_as_required_validation(self):
        output = command_event("python3 /factory/feature-inventory/scripts/validate_inventory.py --root /tmp/app", 0)
        observations, warnings = classify_post_integration_commands(output)
        self.assertEqual(observations[0]["category"], "required_validation")
        self.assertEqual(observations[0]["effect"], "authoritative")
        self.assertEqual(warnings, ())

    def test_05_terminal_integrated_plus_optional_failure_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.fixture(temporary).assessment()
            self.assertTrue(result["checks"]["terminal_integrated"])
            self.assertTrue(result["checks"]["zero_compatible_exit"])
            self.assertTrue(result["success"])

    def test_06_terminal_integrated_plus_required_failure_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.write_runtime(lambda value: value["validation"].update({"ok": False, "blockers": ["required validator failed"]}))
            result = fixture.assessment()
            self.assertTrue(result["checks"]["terminal_integrated"])
            self.assertFalse(result["checks"]["runtime_validation_passed"])
            self.assertFalse(result["success"])

    def test_07_durable_success_precedence_uses_all_independent_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.fixture(temporary).assessment()
            self.assertTrue(result["success"])
            self.assertTrue(all(result["checks"].values()))
            self.assertIsNotNone(result["accepted_commit"])
            self.assertIsNotNone(result["integrated_commit"])
            self.assertIsNotNone(result["terminal_commit"])
        classified, _ = classify_post_integration_commands("\n".join([
            command_event("python3 /factory/feature-inventory/scripts/validate_inventory.py --root /tmp/app", 0),
            command_event("python3 integrationctl.py finalize", 0),
            command_event(
                "python3 scripts/validate_feature_inventory.py", 2,
                "can't open file 'scripts/validate_feature_inventory.py': No such file or directory",
            ),
            command_event("git status --short", 0),
            command_event("echo unclassified", 0),
        ]))
        self.assertEqual(
            {item["category"] for item in classified},
            {
                "required_validation", "required_evidence_finalization", "optional_diagnostic",
                "status_observation", "unsupported_command",
            },
        )

    def test_08_optional_warning_is_persisted_in_finalized_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            outcome = fixture.engine.run_project(fixture.project, "resume")
            cycle = fixture.engine.cycle_store.read(RepositoryInspector(fixture.repository).cycle_state_path())
            self.assertEqual(outcome["outcome"], "state_repaired")
            self.assertIn("optional_diagnostic:unconfigured_repository_validator_missing:exit=2", cycle["optional_warnings"])

    def test_09_completed_cycle_stale_failure_fields_are_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.engine.run_project(fixture.project, "resume")
            cycle = fixture.engine.cycle_store.read(RepositoryInspector(fixture.repository).cycle_state_path())
            self.assertEqual(cycle["current_phase"], "completed")
            self.assertIsNone(cycle["stop_reason"])
            self.assertIsNone(cycle["failure_classification"])
            self.assertEqual(cycle["integration_status"], "passed")
            self.assertEqual(cycle["last_successful_checkpoint"], "resumed_feature_cycle_complete")

    def test_10_validation_failed_controller_recovers_to_queue_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            outcome = fixture.engine.run_project(fixture.project, "resume")
            state = fixture.engine.load_project_state(fixture.project)
            self.assertEqual(outcome["outcome"], "state_repaired")
            self.assertEqual(outcome["current_state"], "queue_reconciliation")
            self.assertEqual(state["current_state"], "queue_reconciliation")
            self.assertIsNone(state["stop_reason"])

    def test_11_corroborated_integration_creates_no_false_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            self.assertFalse(plan["human_decision_required"])
            self.assertIsNone(plan["state_reconciliation_human_decision"])
            self.assertNotEqual(plan["proposed_next_action"], "human_decision_required")

    def test_12_queue_and_git_evidence_corroborate_distinct_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            result = fixture.assessment()
            self.assertEqual(result["accepted_commit"], fixture.accepted)
            self.assertEqual(result["integrated_commit"], fixture.integrated)
            self.assertNotEqual(fixture.accepted, fixture.integrated)
            self.assertTrue(result["checks"]["patch_equivalent"])
            self.assertTrue(result["checks"]["milestone_membership"])

    def test_13_last_validated_commit_means_final_validated_milestone_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            result = fixture.assessment()
            self.assertEqual(result["last_validated_commit_semantics"], "final_validated_milestone_head")
            self.assertEqual(result["effective_last_validated_commit"], fixture.final_head)
            self.assertEqual(result["queue_last_validated_commit_observed"], fixture.integrating_commit)
            self.assertFalse(result["application_metadata_mutated"])

    def test_14_final_model_evidence_commit_is_the_terminal_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            result = fixture.assessment()
            self.assertTrue(result["checks"]["final_validated_head_corroborated"])
            self.assertEqual(result["terminal_commit"], fixture.final_head)
            self.assertEqual(git(fixture.repository, "rev-parse", "HEAD"), fixture.final_head)

    def test_15_no_ready_feature_after_integration_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.fixture(temporary).plan()
            self.assertEqual(plan["queue_status"]["ready_features"], [])
            self.assertIsNone(plan["selected_feature"])

    def test_16_dependency_satisfied_proposal_routes_to_queue_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = fixture.plan()
            self.assertEqual(plan["queue_status"]["reconciliation_classification"], "reconciled_no_ready_work")
            self.assertEqual(plan["proposed_next_action"], "queue_reconciliation")
            self.assertFalse(plan["human_decision_required"])
            self.assertEqual(plan["integration_terminal_classification"], "INTEGRATED")
            self.assertEqual(plan["accepted_feature_commit"], fixture.accepted)
            self.assertEqual(plan["integrated_feature_commit"], fixture.integrated)
            self.assertEqual(plan["milestone_post_integration_commit"], fixture.final_head)
            self.assertEqual(plan["cycle_phase"], "completed")
            self.assertEqual(plan["cycle_stop_reason"], "terminal_integration_failure")
            self.assertEqual(plan["next_action"], "queue_reconciliation")

    def test_17_genuine_dependency_ready_human_decision_still_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            path = fixture.repository / fixture.project.queue_location
            queue = json.loads(path.read_text())
            queue["features"][1].update({"status": "done", "integration_status": "passed", "integrated_commit": fixture.final_head})
            queue["milestones"][0]["integrated_features"].append("F002")
            write_json(path, queue)
            parsed = FeatureQueue.from_location(fixture.repository, fixture.project.queue_location)
            self.assertTrue(parsed.dependencies_complete(parsed.feature("F003")))
            self.assertEqual(parsed.reconciliation_classification("M0"), "human_decision_required")

    def test_18_one_feature_resume_stops_after_state_repair_without_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            outcome = fixture.engine.run_project(fixture.project, "resume")
            self.assertEqual(outcome["outcome"], "state_repaired")
            self.assertEqual(fixture.launcher.actions, [])
            self.assertNotIn("feature_cycle", fixture.launcher.actions)

    def test_19_milestone_mode_can_later_reconcile_the_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.engine.run_project(fixture.project, "resume")
            plan = fixture.engine.run_project(fixture.project, "milestone", dry_run=True)
            self.assertEqual(plan["current_state"], "queue_reconciliation")
            self.assertEqual(plan["proposed_next_action"], "queue_reconciliation")
            self.assertIn("fresh queue-reconciliation transaction", plan["sessions_that_would_launch"])

    def test_20_recovery_does_not_duplicate_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            before = git(fixture.repository, "rev-parse", fixture.project.milestone_branch)
            fixture.engine.run_project(fixture.project, "resume")
            after = git(fixture.repository, "rev-parse", fixture.project.milestone_branch)
            self.assertEqual(before, after)
            self.assertFalse(fixture.engine.project_plan(fixture.project)["milestone_integrator_would_launch"])

    def test_21_integrated_work_does_not_relaunch_feature_factory(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            self.assertFalse(plan["feature_factory_would_launch"])
            self.assertNotIn("$feature-factory", plan["sessions_that_would_launch"])

    def test_22_integrated_work_does_not_relaunch_milestone_integrator(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            plan = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            self.assertFalse(plan["milestone_integrator_would_launch"])
            self.assertFalse(plan["new_or_recovered_integration_session"])

    def test_23_dry_run_writes_no_application_repository_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            cycle_path = RepositoryInspector(fixture.repository).cycle_state_path()
            before = (cycle_path.read_bytes(), git(fixture.repository, "show-ref"), git(fixture.repository, "status", "--porcelain=v1"))
            plan = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            after = (cycle_path.read_bytes(), git(fixture.repository, "show-ref"), git(fixture.repository, "status", "--porcelain=v1"))
            self.assertTrue(plan["would_persist_state_repair"])
            self.assertEqual(before, after)

    def test_24_repeated_recovery_inspection_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.engine.run_project(fixture.project, "resume")
            cycle_path = RepositoryInspector(fixture.repository).cycle_state_path()
            before = cycle_path.read_bytes()
            second = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            third = fixture.engine.run_project(fixture.project, "resume", dry_run=True)
            self.assertTrue(second["would_persist_state_repair"])
            self.assertTrue(third["would_persist_state_repair"])
            self.assertEqual(second["repair_transition_path"], third["repair_transition_path"])
            self.assertEqual(before, cycle_path.read_bytes())

    def test_25_historical_integration_report_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            path = fixture.configuration.root / "reports" / fixture.run_id / "milestone_integration.json"
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            fixture.engine.run_project(fixture.project, "resume")
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            cycle = fixture.engine.cycle_store.read(RepositoryInspector(fixture.repository).cycle_state_path())
            self.assertEqual(before, after)
            self.assertEqual(cycle["integration_finalization_history"][0]["prior_stop_reason"], "terminal_integration_failure")

    def test_26_required_mixed_commands_dominate_optional_diagnostics(self):
        mixed_validation = command_event(
            "swift test; python3 scripts/validate_feature_inventory.py", 2,
            "can't open file 'scripts/validate_feature_inventory.py': No such file or directory",
        )
        observations, warnings = classify_post_integration_commands(
            mixed_validation, required_commands=(("swift", "test"),)
        )
        self.assertEqual(observations[0]["category"], "required_validation")
        self.assertEqual(observations[0]["effect"], "authoritative")
        self.assertEqual(warnings, ())

        mixed_finalization = command_event(
            "python3 model_runlog.py; python3 scripts/validate_feature_inventory.py", 2,
            "can't open file 'scripts/validate_feature_inventory.py': No such file or directory",
        )
        observations, warnings = classify_post_integration_commands(mixed_finalization)
        self.assertEqual(observations[0]["category"], "required_evidence_finalization")
        self.assertEqual(observations[0]["effect"], "authoritative")
        self.assertEqual(warnings, ())

    def test_27_missing_required_exit_status_cannot_authorize_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.write_report(lambda value: value.update({
                "redacted_stdout": "\n".join([
                    command_event(
                        f"python3 /factory/feature-inventory/scripts/validate_inventory.py --root {fixture.repository}",
                        None,
                    ),
                    assistant("Integration complete.\n\n## INTEGRATED"),
                ]),
            }))
            result = fixture.assessment()
            self.assertFalse(result["success"])
            self.assertEqual(result["required_command_failures"][0]["exit_code"], None)

    def test_28_unvalidated_production_commit_cannot_move_runtime_success_pointers(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            (fixture.repository / "app.txt").write_text(
                "baseline\nF001 integrated behavior\nunvalidated production mutation\n", encoding="utf-8"
            )
            git(fixture.repository, "add", "app.txt")
            git(fixture.repository, "commit", "-m", "factory: record F001 integration model-evidence")
            unvalidated = git(fixture.repository, "rev-parse", "HEAD")

            def move_pointers(value):
                value["model_evidence_commit"] = unvalidated
                value["final_head"] = unvalidated
                value["validation"]["validated_commit"] = unvalidated

            fixture.write_runtime(move_pointers)
            result = fixture.assessment()
            self.assertFalse(result["success"])
            self.assertFalse(result["checks"]["factory_evidence_direct_parent_chain"])
            self.assertFalse(result["checks"]["factory_evidence_paths_only"])
            outcome = fixture.engine.run_project(fixture.project, "resume")
            self.assertEqual(outcome["outcome"], "validation_failed")

    def test_29_finalized_cycle_allows_later_planning_only_milestone_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            finalized = fixture.engine.run_project(fixture.project, "resume")
            self.assertEqual(finalized["outcome"], "state_repaired")
            (fixture.repository / "docs/ROADMAP.md").write_text(
                "# Roadmap\n\nRefine the next proposed feature.\n", encoding="utf-8"
            )
            git(fixture.repository, "add", "docs/ROADMAP.md")
            git(fixture.repository, "commit", "-m", "docs: refine M0 planning")
            later_head = git(fixture.repository, "rev-parse", "HEAD")
            plan = fixture.engine.run_project(fixture.project, "milestone", dry_run=True)
            self.assertTrue(plan["durable_integration_success"]["success"])
            self.assertTrue(plan["durable_integration_success"]["historical_finalization"])
            self.assertEqual(plan["durable_integration_success"]["later_planning_commits"], [later_head])
            self.assertEqual(plan["proposed_next_action"], "queue_reconciliation")
            self.assertEqual(plan["current_state"], "queue_reconciliation")
            (fixture.repository / "app.txt").write_text(
                "baseline\nF001 integrated behavior\nlate unvalidated production change\n", encoding="utf-8"
            )
            git(fixture.repository, "add", "app.txt")
            git(fixture.repository, "commit", "-m", "docs: misleading planning label")
            rejected = fixture.engine.run_project(fixture.project, "milestone", dry_run=True)
            self.assertFalse(rejected["durable_integration_success"]["success"])
            self.assertEqual(rejected["proposed_next_action"], "queue_reconciliation")
            self.assertNotEqual(
                rejected["executable_plan"]["starting_commit"],
                git(fixture.repository, "rev-parse", "HEAD"),
            )
            with self.assertRaisesRegex(
                ProjectionError,
                "repository no longer matches the execution plan starting branch and commit",
            ):
                fixture.engine.run_project(fixture.project, "milestone")
            self.assertEqual(fixture.launcher.actions, [])

    def test_30_report_reads_reject_traversal_symlink_and_non_regular_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            cycle_path = RepositoryInspector(fixture.repository).cycle_state_path()
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
            cycle["conveyor_run_id"] = "../foreign"
            write_json(cycle_path, cycle)
            foreign = fixture.configuration.root / "foreign/milestone_integration.json"
            write_json(foreign, fixture.report_document())
            traversal = fixture.plan()
            self.assertFalse((traversal["durable_integration_success"] or {}).get("success", False))
            self.assertIsNone(traversal["integration_terminal_classification"])

        for kind in ("symlink", "directory", "run_symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(temporary)
                report = fixture.configuration.root / "reports" / fixture.run_id / "milestone_integration.json"
                if kind == "run_symlink":
                    run_directory = report.parent
                    moved = report.parent.parent / f"{fixture.run_id}-target"
                    run_directory.rename(moved)
                    run_directory.symlink_to(moved, target_is_directory=True)
                else:
                    report.unlink()
                if kind == "symlink":
                    foreign = fixture.configuration.root / "foreign.json"
                    write_json(foreign, fixture.report_document())
                    report.symlink_to(foreign)
                elif kind == "directory":
                    report.mkdir()
                plan = fixture.plan()
                self.assertFalse((plan["durable_integration_success"] or {}).get("success", False))
                self.assertIsNone(plan["integration_terminal_classification"])

        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            report_root = fixture.configuration.owned_path(
                fixture.configuration.conveyor["report_directory"]
            )
            from development_conveyor.errors import SessionError
            with self.assertRaises(SessionError):
                fixture.engine._report_path(report_root, "../foreign", "milestone_integration.json")

    def test_31_explicit_integration_fix_chain_binds_the_validated_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fix_commit = fixture.rebuild_with_integration_fix()
            result = fixture.assessment()
            self.assertTrue(result["success"])
            self.assertEqual(result["integration_fix_commits"], [fix_commit])
            self.assertEqual(result["validated_tree_commit"], fix_commit)
            self.assertTrue(result["checks"]["validated_tree_bound_to_integration_chain"])
            self.assertTrue(result["checks"]["factory_evidence_direct_parent_chain"])
            fixture.write_runtime(
                lambda value: value["validation"].update({"validated_commit": fixture.integrating_commit})
            )
            stale_commands = fixture.assessment()
            self.assertFalse(stale_commands["success"])
            self.assertFalse(stale_commands["checks"]["validated_tree_bound_to_integration_chain"])
            fixture.write_runtime()
            fixture.write_runtime(lambda value: value.update({"integration_fix_commits": []}))
            mismatched = fixture.assessment()
            self.assertFalse(mismatched["success"])
            self.assertFalse(mismatched["checks"]["validated_tree_bound_to_integration_chain"])

    def test_32_normal_runtime_validation_record_needs_no_plan_validation_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)

            def normal_record(value):
                value["plan"].pop("validation", None)
                value["validation"]["validated_commit"] = fixture.validated_tree

            fixture.write_runtime(normal_record)
            result = fixture.assessment()
            self.assertTrue(result["success"])
            self.assertTrue(result["checks"]["validated_tree_bound_to_integration_chain"])

    def test_33_runtime_validation_requires_explicit_clean_worktree(self):
        for value in (False, None):
            with self.subTest(clean_worktree=value), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(temporary)

                def mutate(runtime):
                    if value is None:
                        runtime["validation"].pop("clean_worktree", None)
                    else:
                        runtime["validation"]["clean_worktree"] = value

                fixture.write_runtime(mutate)
                result = fixture.assessment()
                self.assertFalse(result["success"])
                self.assertFalse(result["checks"]["runtime_validation_passed"])

    def test_34_terminal_report_identity_must_match_every_expected_field(self):
        mismatches = {
            "schema_version": 2,
            "project_id": "other-project",
            "run_id": "other-run",
            "action": "feature_factory",
            "working_directory": "/tmp/not-the-registered-repository",
            "session_id": "other-session",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(temporary)
                fixture.write_report(lambda report: report.update({field: value}))
                result = fixture.assessment()
                self.assertTrue(result["checks"]["terminal_integrated"])
                self.assertFalse(result["checks"]["report_identity_matches"])
                self.assertFalse(result["success"])

    def test_35_missing_or_empty_session_identity_never_matches(self):
        cases = (
            ("report_missing", "valid", None),
            ("report_empty", "valid", ""),
            ("cycle_missing", None, "valid"),
            ("cycle_empty", "", "valid"),
            ("both_missing", None, None),
            ("both_empty", "", ""),
        )
        for label, cycle_value, report_value in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(temporary)
                cycle_path = RepositoryInspector(fixture.repository).cycle_state_path()
                cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
                if cycle_value is None:
                    cycle.pop("integration_session_id", None)
                elif cycle_value != "valid":
                    cycle["integration_session_id"] = cycle_value
                write_json(cycle_path, cycle)

                def mutate_report(report):
                    if report_value is None:
                        report.pop("session_id", None)
                    elif report_value != "valid":
                        report["session_id"] = report_value

                fixture.write_report(mutate_report)
                result = fixture.assessment()
                self.assertFalse(result["checks"]["integration_session_matches"])
                self.assertFalse(result["checks"]["report_identity_matches"])
                self.assertFalse(result["success"])

    def test_36_authorized_later_planning_dirtiness_does_not_rewrite_terminal_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(temporary)
            fixture.engine.run_project(fixture.project, "resume")
            status = fixture.repository / "docs/CURRENT_STATUS.md"
            status.write_text(status.read_text(encoding="utf-8") + "\nF002 planning pending.\n", encoding="utf-8")
            result = fixture.assessment()
            self.assertTrue(result["success"])
            self.assertTrue(result["checks"]["clean_repository"])
            self.assertTrue(result["historical_finalization"])
            self.assertFalse(result["current_repository_clean"])


if __name__ == "__main__":
    unittest.main()
