from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.sessions import (
    SessionLauncher,
    SessionPlan,
    SessionRequest,
    SessionResult,
    milestone_integration_mutation_command,
    validate_milestone_integration_result,
)
from tests.helpers import (
    REPOSITORY_ROOT,
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


class _Inspector:
    def __init__(self, *, starting: str, head: str, branch: str, equivalent: str):
        self.head = head
        self.current_branch = branch
        self._starting = starting
        self._equivalent = equivalent

    def tracked_changed_paths(self):
        return []

    def untracked_file_hashes(self):
        return {}

    def git_operation_state(self):
        return {"cherry_pick": False, "merge": False, "rebase": False, "revert": False}

    def ref_exists(self, value):
        return value in {"accepted", self._equivalent, self.head, self._starting}

    def is_ancestor(self, value, _branch):
        return value == self._equivalent

    def patch_fingerprint(self, value):
        return "accepted-patch" if value in {"accepted", self._equivalent} else value


class _Queue:
    def feature(self, _feature_id):
        return {
            "status": "integrated",
            "integration_status": "passed",
            "accepted_commit": "accepted",
        }

    def milestone(self, _milestone_id):
        return {"integrated_features": ["F001"]}


class MilestoneIntegrationResultContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository, self.project = synthetic_repository(root)
        self.configuration = controller_configuration(root, self.project)
        self.request = SessionRequest(
            action="milestone_integration",
            project=self.project,
            run_id="fresh-run",
            mode="milestone",
            feature="F001",
            transaction_id="fresh-transaction",
            repository_identity="repository-identity",
            starting_branch="codex/m0-foundation",
            starting_commit="starting",
            accepted_commit="accepted",
            allowed_paths=("app.txt",),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _runtime(self):
        return {
            "phase": "complete",
            "accepted_commit": "accepted",
            "lease_released": True,
            "runtime_identity": {
                "repository_identity": "repository-identity",
                "project_id": "synthetic",
                "run_id": "fresh-run",
                "feature_id": "F001",
            },
            "validation": {"ok": True, "clean_worktree": True, "blockers": []},
        }

    def _result(self, *, current_commit="terminal", changed_paths=None):
        return {
            "schema_version": 1,
            "workflow_type": "milestone_integration",
            "classification": "INTEGRATED",
            "project_id": "synthetic",
            "repository_identity": "repository-identity",
            "transaction_id": "fresh-transaction",
            "run_id": "fresh-run",
            "session_id": "fresh-session",
            "starting_branch": "codex/m0-foundation",
            "starting_commit": "starting",
            "current_commit": current_commit,
            "feature_id": "F001",
            "changed_paths": ["app.txt"] if changed_paths is None else changed_paths,
            "evidence": {
                "accepted_commit": "accepted",
                "integration_command": milestone_integration_mutation_command("F001"),
                "runtime_record": ".factory/runtime/milestone-integration/latest.json",
                "validation_passed": True,
                "already_integrated": False,
                "patch_equivalent_commit": "equivalent",
            },
            "next_state": "feature_integrated",
        }

    def _validate(self, result, *, head="terminal"):
        inspector = _Inspector(
            starting="starting",
            head=head,
            branch="codex/m0-foundation",
            equivalent="equivalent",
        )
        with mock.patch(
            "development_conveyor.sessions.RepositoryInspector", return_value=inspector
        ), mock.patch(
            "development_conveyor.sessions.FeatureQueue.from_location", return_value=_Queue()
        ), mock.patch(
            "development_conveyor.sessions._runtime_integration_record", return_value=self._runtime()
        ):
            return validate_milestone_integration_result(
                result, self.request, command_observed=True
            )

    def test_read_only_preparation_and_unchanged_commit_cannot_claim_integrated(self):
        failures = self._validate(
            self._result(current_commit="starting", changed_paths=[]), head="starting"
        )
        self.assertIn("milestone_branch_advanced_for_new_integration", failures)
        self.assertIn("changed_paths_nonempty_for_new_integration", failures)

    def test_empty_changed_paths_rejects_new_integrated_result(self):
        failures = self._validate(self._result(changed_paths=[]))
        self.assertIn("changed_paths_nonempty_for_new_integration", failures)

    def test_valid_completed_integration_result_is_accepted(self):
        self.assertEqual((), self._validate(self._result()))

    def test_marker_rejection_report_exposes_exact_semantic_diagnostics(self):
        failures = self._validate(
            self._result(current_commit="starting", changed_paths=[]), head="starting"
        )
        engine = CycleEngine(self.configuration)
        plan = SessionPlan(
            argv=("codex",), cwd=self.repository, prompt="focused", prompt_sha256="f" * 64,
            sandbox="workspace-write",
        )
        parsed = self._result(current_commit="starting", changed_paths=[])
        result = SessionResult(
            action="milestone_integration",
            returncode=0,
            session_id="fresh-session",
            redacted_output="",
            plan=plan,
            structured_result=parsed,
            structured_output_validation="semantic_invalid",
            result_classification="structured_output_invalid",
            terminal_marker_found=True,
            parsed_structured_result=parsed,
            structured_output_errors=tuple(f"semantic check failed: {item}" for item in failures),
            failed_semantic_checks=failures,
        )
        path = engine._persist_session_report(self.request, result, "integrating")
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(report["terminal_marker_found"])
        self.assertEqual(parsed, report["parsed_structured_result"])
        self.assertEqual(list(failures), report["failed_semantic_checks"])
        self.assertIn(
            "semantic check failed: milestone_branch_advanced_for_new_integration",
            report["structured_output_errors"],
        )

    def test_prompt_contains_exact_schema_command_and_no_preparation_only_instruction(self):
        prompt = SessionLauncher(
            REPOSITORY_ROOT, self.configuration.conveyor
        )._render_prompt(self.request)
        self.assertIn(milestone_integration_mutation_command("F001"), prompt)
        self.assertIn('"classification":"INTEGRATED"', prompt)
        self.assertIn("CONVEYOR_TRANSACTION_RESULT=", prompt)
        self.assertIn('"transaction_id":"fresh-transaction"', prompt)
        self.assertIn('"accepted_commit":"accepted"', prompt)
        self.assertNotIn("read-only preparation mode", prompt)
        self.assertNotIn("do not switch branches, cherry-pick", prompt)
        self.assertIn("Do not search global memories", prompt)

    def test_terminal_failed_session_selects_fresh_recovery_integration(self):
        starting = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "switch", "-c", "codex/F001-accepted")
        (self.repository / "app.txt").write_text("baseline\naccepted\n", encoding="utf-8")
        queue_path = self.repository / self.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integration_pending",
            "branch": "codex/F001-accepted",
            "integration_base_commit": starting,
            "accepted_commit": "SELF",
            "integration_status": "pending",
            "acceptance": {
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": True,
            },
        })
        write_json(queue_path, queue)
        git(self.repository, "add", "app.txt", self.project.queue_location)
        git(self.repository, "commit", "-m", "F001: accepted")
        accepted = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "switch", "codex/m0-foundation")
        run_id = "failed-integration-run"
        session_id = "old-terminal-session"
        report = {
            "schema_version": 1,
            "project_id": "synthetic",
            "run_id": run_id,
            "action": "milestone_integration",
            "working_directory": str(self.repository),
            "session_id": session_id,
            "accepted_commit": accepted,
            "structured_output_validation": "invalid",
            "result_classification": "structured_output_invalid",
            "redacted_stdout": "CONVEYOR_TRANSACTION_RESULT={}",
            "post_integration_commands": [
                {"category": "status_observation", "exit_code": 0}
            ],
        }
        report_path = (
            self.configuration.root / "reports" / run_id / "milestone_integration.json"
        )
        write_json(report_path, report)
        projection = {
            "schema_version": 1,
            "project_id": "synthetic",
            "current_state": "validation_failed",
            "active_transaction": None,
            "current_feature": "F001",
            "transactions": [{
                "transaction_id": "failed-transaction",
                "workflow_type": "milestone_integration",
                "state": "terminal_failure",
                "terminal_classification": "VALIDATION_FAILED",
                "feature_id": "F001",
                "run_id": run_id,
                "session_ids": [session_id],
                "starting_snapshot": {
                    "branch": "codex/m0-foundation", "head": starting,
                },
            }],
        }
        engine = CycleEngine(self.configuration)
        recovered = engine._fresh_failed_integration_projection(self.project, projection)
        self.assertEqual("integration_ready", recovered["current_state"])
        self.assertEqual("milestone_integration", recovered["allowed_next_action"])
        self.assertEqual(accepted, recovered["accepted_feature_commit"])
        self.assertEqual(starting, recovered["selected_feature_starting_commit"])
        self.assertFalse(recovered["session_resume_eligible"])
        evidence = recovered["failed_integration_recovery"]
        self.assertTrue(evidence["fresh_transaction"])
        self.assertFalse(evidence["old_session_resume"])
        self.assertEqual(session_id, evidence["failed_session_id"])
        self.assertEqual("immutable_feature_ref_head", evidence["accepted_commit_source"])
        self.assertTrue(all(evidence["snapshot_checks"].values()))


if __name__ == "__main__":
    unittest.main()
