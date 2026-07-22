from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import WorkflowType, fingerprint
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ProjectionError
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from tests.helpers import (
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


class CapturingLauncher(SyntheticLauncher):
    def __init__(self):
        super().__init__()
        self.requests = []

    def launch(self, request, on_session_started=None):
        self.requests.append(request)
        return super().launch(request, on_session_started=on_session_started)


class TwoRefIntegrationRecoveryTests(unittest.TestCase):
    def _fixture(
        self, root: Path, *, snapshot_valid: bool = True,
        accepted_topology: str = "direct",
    ):
        repository, project = synthetic_repository(root)
        milestone_start = git(repository, "rev-parse", "codex/m0-foundation")
        feature_branch = "codex/F001-two-ref-recovery"
        git(repository, "switch", "-c", feature_branch, milestone_start)
        (repository / "app.txt").write_text(
            "baseline\naccepted two-ref behavior\n", encoding="utf-8"
        )
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "status": "integration_pending",
            "implementation_status": "Completed",
            "branch": feature_branch,
            "integration_base_commit": milestone_start,
            "accepted_commit": "SELF",
            "integration_status": "pending",
            "acceptance": {
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": snapshot_valid,
            },
        })
        write_json(queue_path, queue)
        git(repository, "add", "app.txt", project.queue_location)
        git(repository, "commit", "-m", "F001: accepted two-ref behavior")
        accepted = git(repository, "rev-parse", "HEAD")
        if accepted_topology == "multi_commit":
            (repository / "second-feature-commit.txt").write_text(
                "second feature commit\n", encoding="utf-8"
            )
            git(repository, "add", "second-feature-commit.txt")
            git(repository, "commit", "-m", "F001: second accepted commit")
            accepted = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", project.milestone_branch)
        if accepted_topology == "unrelated":
            accepted_tree = git(repository, "rev-parse", f"{accepted}^{{tree}}")
            milestone_tree = git(repository, "rev-parse", f"{milestone_start}^{{tree}}")
            unrelated_parent = git(repository, "commit-tree", milestone_tree, "-m", "unrelated root")
            accepted = git(
                repository, "commit-tree", accepted_tree, "-p", unrelated_parent,
                "-m", "F001: unrelated accepted history",
            )
            git(repository, "branch", "-f", feature_branch, accepted)
        if accepted_topology not in {"direct", "multi_commit", "unrelated"}:
            raise AssertionError(f"unsupported accepted topology: {accepted_topology}")
        live_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual("ready", live_queue["features"][0]["status"])

        configuration = controller_configuration(root, project)
        inspector = RepositoryInspector(repository)
        inspector.ensure_runtime_ignored()
        state_root = configuration.root / "state/projects/synthetic"
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            repository_path_fingerprint=inspector.identity()["path_fingerprint"],
        )
        transaction = "failed-two-ref-transaction"
        run_id = "failed-two-ref-run"
        session_id = "old-terminal-session"
        workflow = WorkflowType.MILESTONE_INTEGRATION
        ledger.append(
            event_type="TransactionStarted",
            transaction_id=transaction,
            workflow_type=workflow,
            payload={
                "run_id": run_id,
                "feature_id": "F001",
                "milestone": "M0",
                "starting_branch": project.milestone_branch,
                "starting_head": milestone_start,
                "allowed_mutation_policy": {},
            },
        )
        ledger.append(
            event_type="LeaseAcquired", transaction_id=transaction,
            workflow_type=workflow,
            payload={"lease_id": "old-integration-lease", "lease_type": "integration_writer"},
        )
        ledger.append(
            event_type="SnapshotCaptured", transaction_id=transaction,
            workflow_type=workflow,
            payload={"snapshot": {"branch": project.milestone_branch, "head": milestone_start}},
        )
        ledger.append(
            event_type="SessionLaunched", transaction_id=transaction,
            workflow_type=workflow, payload={"session_id": session_id},
        )
        ledger.append(
            event_type="ValidationStarted", transaction_id=transaction,
            workflow_type=workflow, payload={},
        )
        ledger.append(
            event_type="ValidationFailed", transaction_id=transaction,
            workflow_type=workflow,
            payload={"diagnostic": "pre-mutation structured result rejected"},
        )
        ledger.append(
            event_type="TransactionBlocked", transaction_id=transaction,
            workflow_type=workflow,
            payload={
                "classification": "VALIDATION_FAILED",
                "reference": "SessionError",
                "terminal_state": "terminal_failure",
                "next_state": "validation_failed",
                "terminal_snapshot": {
                    "branch": project.milestone_branch,
                    "head": milestone_start,
                    "clean": True,
                },
            },
        )
        ledger.append(
            event_type="LeaseReleased", transaction_id=transaction,
            workflow_type=workflow, payload={"lease_id": "old-integration-lease"},
        )
        ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        ).rebuild(persist_cache=True)
        write_json(
            configuration.root / "reports" / run_id / "milestone_integration.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": run_id,
                "action": "milestone_integration",
                "working_directory": str(repository),
                "session_id": session_id,
                "accepted_commit": accepted,
                "terminal_marker_found": True,
                "structured_output_validation": "semantic_invalid",
                "result_classification": "structured_output_invalid",
                "post_integration_commands": [
                    {"category": "status_observation", "exit_code": 0}
                ],
            },
        )
        launcher = CapturingLauncher()
        return (
            repository, project, configuration, CycleEngine(configuration, launcher),
            launcher, ledger, milestone_start, feature_branch, accepted, session_id,
        )

    def test_live_ready_queue_integrates_from_two_verified_refs_in_fresh_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository, project, _, engine, launcher, ledger, milestone_start,
                feature_branch, accepted, old_session,
            ) = self._fixture(Path(temporary))
            plan = engine.project_plan(project)
            self.assertEqual("integration_ready", plan["current_state"])
            self.assertEqual("milestone_integration", plan["next_action"])
            self.assertEqual(accepted, plan["accepted_feature_commit"])
            self.assertEqual(milestone_start, plan["feature_starting_commit"])
            self.assertEqual(feature_branch, plan["feature_branch"])
            self.assertFalse(plan["old_session_will_resume"])
            self.assertEqual("fresh", plan["transaction_mode"])

            result = engine.run_project(project, "milestone")
            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual("F001", result["feature"])
            self.assertEqual(accepted, result["accepted_commit"])
            self.assertEqual(accepted, git(repository, "rev-parse", feature_branch))
            self.assertNotEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertEqual(
                "baseline\naccepted two-ref behavior\n",
                (repository / "app.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual([], launcher.actions)
            self.assertEqual([], launcher.requests)
            self.assertFalse(result["model_session_launched"])
            self.assertNotEqual(old_session, result["kernel_projection"].get("session_id"))
            integrated_queue = json.loads(
                (repository / project.queue_location).read_text(encoding="utf-8")
            )
            self.assertEqual("integrated", integrated_queue["features"][0]["status"])
            self.assertEqual("passed", integrated_queue["features"][0]["integration_status"])
            self.assertTrue(Path(result["integration_plan"]).is_file())
            self.assertFalse(
                Path(result["integration_plan"]).is_relative_to(repository)
            )
            transactions = result["kernel_projection"]["transactions"]
            self.assertTrue(any(
                item.get("workflow_type") == "milestone_integration"
                and item.get("state") == "completed"
                and item.get("transaction_id") != "failed-two-ref-transaction"
                for item in transactions
            ))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertGreater(ledger.verify().sequence, 8)

    def test_mechanical_terminal_gate_reconstructs_a_fresh_nonresumable_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository, project, configuration, engine, launcher, ledger,
                milestone_start, feature_branch, accepted, _,
            ) = self._fixture(Path(temporary))
            transaction = "mechanical-terminal-gate"
            run_id = "mechanical-terminal-gate-run"
            session_id = "mechanical-terminal-gate-session"
            workflow = WorkflowType.MILESTONE_INTEGRATION
            snapshot = {
                "branch": project.milestone_branch,
                "head": milestone_start,
                "clean": True,
            }
            gate = {
                "gate_id": "gate-mechanical-handoff",
                "classification": "HUMAN_DECISION_REQUIRED",
                "reason": "Deterministic handoff prerequisites were absent.",
                "project_id": project.project_id,
                "repository_identity": RepositoryInspector(repository).identity()[
                    "repository_id"
                ],
                "repository_path_fingerprint": RepositoryInspector(repository).identity()[
                    "path_fingerprint"
                ],
                "transaction_id": transaction,
                "workflow_type": workflow.value,
                "approved_next_state": "queue_reconciliation",
                "blocker_categories": [
                    "accepted_state_identity_mismatch",
                    "runtime_ignore_policy",
                ],
            }
            envelope = {
                "schema_version": 1,
                "workflow_type": workflow.value,
                "classification": "HUMAN_DECISION_REQUIRED",
                "project_id": project.project_id,
                "repository_identity": gate["repository_identity"],
                "transaction_id": transaction,
                "run_id": run_id,
                "session_id": session_id,
                "starting_branch": project.milestone_branch,
                "starting_commit": milestone_start,
                "current_commit": milestone_start,
                "feature_id": "F001",
                "changed_paths": [],
                "evidence": {
                    "accepted_commit": accepted,
                    "human_decision": {
                        "transaction_id": transaction,
                        "blocker_categories": gate["blocker_categories"],
                    },
                },
                "next_state": "human_decision_required",
            }
            for event_type, payload in (
                (
                    "TransactionStarted",
                    {
                        "run_id": run_id,
                        "feature_id": "F001",
                        "milestone": "M0",
                        "starting_branch": project.milestone_branch,
                        "starting_head": milestone_start,
                        "allowed_mutation_policy": {},
                    },
                ),
                (
                    "LeaseAcquired",
                    {"lease_id": "mechanical-gate-lease", "lease_type": "integration_writer"},
                ),
                ("SnapshotCaptured", {"snapshot": snapshot}),
                ("SessionLaunched", {"session_id": session_id}),
                (
                    "SessionResultAccepted",
                    {
                        "classification": "HUMAN_DECISION_REQUIRED",
                        "session_id": session_id,
                        "changed_paths": [],
                        "envelope": envelope,
                    },
                ),
                (
                    "HumanGateRaised",
                    {
                        "classification": "HUMAN_DECISION_REQUIRED",
                        "gate_id": gate["gate_id"],
                        "gate": gate,
                        "gate_fingerprint": fingerprint(gate),
                        "next_state": "human_decision_required",
                        "terminal_state": "human_decision_required",
                        "terminal_snapshot": snapshot,
                    },
                ),
                ("LeaseReleased", {"lease_id": "mechanical-gate-lease"}),
                (
                    "ProjectionUpdated",
                    {"current_state": "human_decision_required", "current_feature": "F001"},
                ),
            ):
                ledger.append(
                    event_type=event_type,
                    transaction_id=transaction,
                    workflow_type=workflow,
                    payload=payload,
                )
            ProjectionEngine(
                ledger,
                configuration.root / "state/projects/synthetic/projection-cache.json",
            ).rebuild(persist_cache=True)
            write_json(
                configuration.root / f"reports/{run_id}/milestone_integration.json",
                {
                    "schema_version": 1,
                    "project_id": project.project_id,
                    "run_id": run_id,
                    "action": workflow.value,
                    "working_directory": str(repository),
                    "session_id": session_id,
                    "accepted_commit": accepted,
                    "terminal_marker_found": True,
                    "structured_output_validation": "valid",
                    "result_classification": "HUMAN_DECISION_REQUIRED",
                    "parsed_structured_result": envelope,
                    "post_integration_commands": [
                        {"category": "status_observation", "exit_code": 0}
                    ],
                },
            )

            plan = engine.project_plan(project)
            self.assertEqual("integration_ready", plan["current_state"])
            self.assertEqual("milestone_integration", plan["workflow_type"])
            self.assertEqual("fresh", plan["transaction_mode"])
            self.assertEqual(accepted, plan["accepted_feature_commit"])
            self.assertEqual(feature_branch, plan["feature_branch"])
            self.assertEqual(milestone_start, plan["feature_starting_commit"])
            self.assertFalse(plan["old_session_will_resume"])
            self.assertEqual([], plan["sessions_that_would_launch"])
            self.assertEqual([], launcher.actions)
            consistency = ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertEqual("CONSISTENT", consistency["classification"])
            self.assertEqual([], consistency["failed_invariants"])

    def test_feature_and_milestone_ref_drift_fail_before_new_ledger_events(self):
        for drift in ("feature", "milestone"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                (
                    repository, project, _, engine, _, ledger, _, feature_branch,
                    _, _,
                ) = self._fixture(Path(temporary))
                plan = engine._authoritative_execution_context(project)[1]
                ledger_before = ledger.path.read_bytes()
                branch = feature_branch if drift == "feature" else project.milestone_branch
                git(repository, "switch", branch)
                drift_path = repository / f"{drift}-drift.txt"
                drift_path.write_text(f"{drift} drift\n", encoding="utf-8")
                git(repository, "add", drift_path.name)
                git(repository, "commit", "-m", f"test: {drift} ref drift")
                git(repository, "switch", project.milestone_branch)
                with self.assertRaises(ProjectionError):
                    engine._execute_projected_integration(
                        project, "milestone", f"{drift}-drift-run", plan
                    )
                self.assertEqual(ledger_before, ledger.path.read_bytes())
                self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_queue_snapshot_acceptance_mismatch_does_not_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, ledger, _, _, _, _ = self._fixture(
                Path(temporary), snapshot_valid=False
            )
            ledger_before = ledger.path.read_bytes()
            plan = engine.project_plan(project)
            self.assertEqual("validation_failed", plan["current_state"])
            self.assertNotEqual("milestone_integration", plan["next_action"])
            self.assertEqual(ledger_before, ledger.path.read_bytes())

    def test_multi_commit_accepted_feature_ref_does_not_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, ledger, _, _, _, _ = self._fixture(
                Path(temporary), accepted_topology="multi_commit"
            )
            ledger_before = ledger.path.read_bytes()
            plan = engine.project_plan(project)
            self.assertEqual("validation_failed", plan["current_state"])
            self.assertNotEqual("milestone_integration", plan["next_action"])
            self.assertEqual(ledger_before, ledger.path.read_bytes())

    def test_unrelated_history_accepted_feature_ref_does_not_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, engine, _, ledger, _, _, _, _ = self._fixture(
                Path(temporary), accepted_topology="unrelated"
            )
            ledger_before = ledger.path.read_bytes()
            plan = engine.project_plan(project)
            self.assertEqual("validation_failed", plan["current_state"])
            self.assertNotEqual("milestone_integration", plan["next_action"])
            self.assertEqual(ledger_before, ledger.path.read_bytes())

    def test_status_dry_run_and_consistency_append_no_recovery_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, engine, _, ledger, _, _, _, _ = self._fixture(
                Path(temporary)
            )
            ledger_before = ledger.path.read_bytes()
            repository_before = git(repository, "status", "--porcelain=v1", "-uall")
            self.assertEqual("integration_ready", engine.project_plan(project)["current_state"])
            self.assertEqual(
                "integration_ready",
                engine.run_project(project, "milestone", dry_run=True)["current_state"],
            )
            consistency = ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: engine.project_plan(project),
            ).check()
            self.assertIn(consistency["classification"], {"CONSISTENT", "RECOVERABLE_INCONSISTENCY"})
            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(
                repository_before,
                git(repository, "status", "--porcelain=v1", "-uall"),
            )
            self.assertEqual([], engine.launcher.actions)


if __name__ == "__main__":
    unittest.main()
