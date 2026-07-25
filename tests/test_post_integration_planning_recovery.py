from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.contracts import TransactionState, WorkflowType
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError, SchemaValidationError
from development_conveyor.kernel import QueueReconciliationAdapter, WorkflowKernel
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.planning import planning_report_path
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


RUN_ID = "899b8fbd-edce-4547-a8a9-7d9bdb82da7a"
TRANSACTION_ID = "f4027903-dfd5-4f5d-83af-b7a15792cf07"
SESSION_ID = "019f97c7-629e-7de3-b01b-37eaee3e385e"
EIGHT_PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/F010-diagnostics-and-latency-instrumentation.md",
    "docs/features/F068-job-application-data-model.md",
    "docs/features/F070-applications-table.md",
]


def role_failure_events() -> list[dict]:
    events: list[dict] = []
    for index, role in enumerate(("product-architect", "feature-inventory-lead"), 1):
        item_id = f"role-{index}"
        command = (
            "/bin/zsh -lc \"codex exec --json -C . -s read-only "
            f"'You are the named {role} role.'\""
        )
        events.extend([
            {
                "type": "item.started",
                "item": {
                    "id": item_id,
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": item_id,
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": (
                        "WARNING: proceeding, even though we could not create PATH aliases: "
                        "Operation not permitted (os error 1)\n"
                        "Reading additional input from stdin...\n"
                        "Error: failed to initialize in-process app-server client: "
                        "Operation not permitted (os error 1)\n"
                    ),
                    "exit_code": 1,
                    "status": "failed",
                },
            },
        ])
    return events


class PostIntegrationPlanningRecoveryTests(unittest.TestCase):
    def _fixture(self, root: Path):
        repository, project = synthetic_repository(
            root,
            feature_status="proposed",
            controller_project_id="interview-companion",
        )
        project = replace(project, current_state="queue_reconciliation")
        integrated = git(repository, "rev-parse", "HEAD")
        (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
        (repository / ".factory/locks/.gitignore").write_text(
            "writer.json\n",
            encoding="utf-8",
        )
        queue = {
            "schema_version": 1,
            "milestones": [{
                "id": "M0",
                "name": "Synthetic M0",
                "status": "active",
                "base_commit": integrated,
                "integration_branch": "codex/m0-foundation",
                "integrated_features": ["F068"],
                "last_validated_commit": integrated,
                "human_gate": True,
            }],
            "features": [
                {
                    "id": "F068",
                    "title": "Job Application Data Model",
                    "status": "integrated",
                    "priority": 68,
                    "milestone": "M0",
                    "dependencies": [],
                    "spec": "docs/features/F068-job-application-data-model.md",
                    "acceptance_criteria": ["F068 remains integrated exactly once."],
                    "requires_human_decision": False,
                    "branch": "codex/F068-job-application-data-model",
                    "integration_base_commit": integrated,
                    "accepted_commit": integrated,
                    "integrated_commit": integrated,
                    "integration_status": "passed",
                    "integration_fix_commits": [],
                },
                {
                    "id": "F070",
                    "title": "Applications Table",
                    "status": "proposed",
                    "priority": 70,
                    "milestone": "M0",
                    "dependencies": ["F068"],
                    "spec": "docs/features/F070-applications-table.md",
                    "acceptance_criteria": ["F070 provides a sortable applications table."],
                    "requires_human_decision": False,
                    "branch": None,
                    "integration_base_commit": None,
                    "accepted_commit": None,
                    "integrated_commit": None,
                    "integration_status": "pending",
                    "integration_fix_commits": [],
                },
            ],
        }
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        for relative in EIGHT_PATHS:
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative == "docs/FEATURE_QUEUE.yaml":
                continue
            target.write_text(
                "# Planning baseline\n\nF068 integrated. F070 proposed.\n",
                encoding="utf-8",
            )
        git(repository, "add", "docs", ".factory/locks/.gitignore")
        git(repository, "commit", "-m", "synthetic post-integration baseline")
        starting_head = git(repository, "rev-parse", "HEAD")

        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration)
        inspector = RepositoryInspector(repository)
        identity = inspector.identity()
        state_root = engine.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=ProjectionEngine(ledger, state_root / "projection-cache.json"),
            lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
        )
        adapter = QueueReconciliationAdapter(
            allowed_paths=EIGHT_PATHS,
            commit_subject="factory: reconcile M0 queue and ready F070",
            next_state="feature_ready",
        )
        kernel.begin(
            workflow_type=WorkflowType.QUEUE_RECONCILIATION,
            milestone="M0",
            feature_id=None,
            run_id=RUN_ID,
            policy=adapter.policy,
            transaction_id=TRANSACTION_ID,
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        kernel.checkpoint(
            "planning_session_reserved",
            {"models_planned": 1, "child_sessions_planned": 0},
        )
        kernel.session_launched(SESSION_ID)

        queue["features"][1]["status"] = "ready"
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        for relative in EIGHT_PATHS:
            if relative == "docs/FEATURE_QUEUE.yaml":
                continue
            (repository / relative).write_text(
                "# Reconciled planning\n\nF068 integrated. F070 ready.\n"
                "product-architect launch unavailable before inspection.\n"
                "feature-inventory-lead launch unavailable before inspection.\n",
                encoding="utf-8",
            )
        changed_paths = inspector.tracked_changed_paths()
        diff_fingerprint = inspector.planning_diff_fingerprint()
        envelope = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_READY_WORK",
            "project_id": project.project_id,
            "repository_identity": identity["repository_id"],
            "transaction_id": TRANSACTION_ID,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "starting_branch": project.milestone_branch,
            "starting_commit": starting_head,
            "current_commit": starting_head,
            "feature_id": None,
            "changed_paths": changed_paths,
            "evidence": {
                "queue_validation": {
                    "valid": True,
                    "milestone_found": True,
                    "active_milestone": "M0",
                    "feature_count": 2,
                    "global_feature_count": 2,
                    "global_milestone_count": 1,
                    "ready_features": ["F070"],
                    "active_features": [],
                    "warning_count": 0,
                    "blocking_warnings": [],
                },
                "summary": "F068 synchronized; F070 marked ready.",
                "retryable": False,
                "human_decision": None,
            },
            "next_state": "feature_ready",
        }
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
        stdout_events = [
            {"type": "thread.started", "thread_id": SESSION_ID},
            *role_failure_events(),
            {
                "type": "item.completed",
                "item": {
                    "id": "inventory-validation",
                    "type": "command_execution",
                    "command": (
                        "/bin/zsh -lc 'python3 ~/.agents/skills/feature-inventory/"
                        "scripts/validate_inventory.py --root .'"
                    ),
                    "aggregated_output": json.dumps({
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "feature_count": 2,
                        "milestone_count": 1,
                        "active": [],
                        "ready": ["F070"],
                        "project_usage": "personal_private",
                    }),
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "diff-validation",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'git diff --check'",
                    "aggregated_output": "",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "terminal",
                    "type": "agent_message",
                    "text": "authenticated terminal result",
                },
            },
        ]
        report = {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "action": "queue_reconciliation",
            "working_directory": str(repository),
            "exit_status": 0,
            "exit_classification": "structured_result_successfully_returned",
            "structured_output_validation": "valid",
            "result_classification": "RECONCILED_READY_WORK",
            "structured_result": envelope,
            "parsed_structured_result": envelope,
            "terminal_marker_found": True,
            "redacted_stdout": "\n".join(
                json.dumps(event, separators=(",", ":"))
                for event in stdout_events
            ),
            "session_id": SESSION_ID,
        }
        write_json(report_path, report)
        transaction_path = planning_report_path(engine.root / "reports", RUN_ID)
        transaction = {
            "schema_version": 1,
            "status": "planning_validation_failed",
            "phase": "queue_reconciliation",
            "project_id": project.project_id,
            "repository": str(repository),
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "session_ids": [SESSION_ID],
            "branch": project.milestone_branch,
            "planning_start_commit": starting_head,
            "changed_paths": changed_paths,
            "diff_fingerprint": diff_fingerprint,
            "result_classification": "RECONCILED_READY_WORK",
            "failure_classification": "PLANNING_VALIDATION_FAILED",
            "reconciliation_report": str(report_path),
            "error": "newly readied feature F070 lacks required execution_policy",
        }
        write_json(transaction_path, transaction)
        kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification="PLANNING_VALIDATION_FAILED",
            next_state="validation_failed",
        )
        inventory = {
            "ok": True,
            "valid": True,
            "milestone_found": True,
            "active_milestone": "M0",
            "exit_code": 0,
            "errors": [],
            "warnings": [],
            "nonfatal_warnings": [],
            "blocking_warnings": [],
            "warning_count": 0,
            "feature_count": 2,
            "global_feature_count": 2,
            "global_milestone_count": 1,
            "ready": ["F070"],
            "active": [],
            "validator": "synthetic inventory validator",
        }
        return {
            "repository": repository,
            "project": project,
            "engine": engine,
            "ledger": ledger,
            "head": starting_head,
            "diff": diff_fingerprint,
            "inventory": inventory,
            "report_path": report_path,
            "transaction_path": transaction_path,
        }

    @staticmethod
    def _arguments(fixture: dict) -> dict:
        return {
            "run_id": RUN_ID,
            "expected_starting_head": fixture["head"],
            "expected_diff_fingerprint": fixture["diff"],
            "expected_changed_paths": EIGHT_PATHS,
            "expected_session_id": SESSION_ID,
        }

    def _dry_run(self, fixture: dict):
        with patch(
            "development_conveyor.planning._inventory_validation",
            side_effect=AssertionError(
                "dry-run must not execute the application inventory validator"
            ),
        ):
            return fixture["engine"].recover_planning_transaction(
                fixture["project"],
                **self._arguments(fixture),
                dry_run=True,
            )

    def test_exact_post_f068_topology_is_recoverable_without_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            repository = fixture["repository"]
            ledger_before = fixture["ledger"].path.read_bytes()
            status_before = git(repository, "status", "--porcelain=v1", "--branch")
            result = self._dry_run(fixture)
            plan = result["planning_finalization_recovery"]
            self.assertEqual(plan["selected_feature"], "F070")
            self.assertEqual(plan["ready_features"], ["F070"])
            self.assertEqual(plan["expected_final_state"], "feature_ready")
            self.assertIsNone(plan["expected_final_current_feature"])
            self.assertEqual(
                plan["expected_final_selected_next_feature"],
                "F070",
            )
            self.assertEqual(plan["result_classification"], "RECONCILED_READY_WORK")
            self.assertEqual(
                plan["recovered_failure_classification"],
                "preinspection_role_launch_unavailable",
            )
            self.assertEqual(
                plan["nested_role_launch_failures"]["roles"],
                ["feature-inventory-lead", "product-architect"],
            )
            self.assertEqual(plan["existing_planning_changes"]["paths"], EIGHT_PATHS)
            self.assertEqual(result["model_sessions_that_would_launch"], [])
            self.assertEqual(result["child_sessions_that_would_launch"], [])
            self.assertEqual(git(repository, "status", "--porcelain=v1", "--branch"), status_before)
            self.assertEqual(fixture["ledger"].path.read_bytes(), ledger_before)

    def test_post_integration_recovery_commits_once_and_refreshes_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            repository = fixture["repository"]
            app_before = (repository / "app.txt").read_bytes()
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ):
                result = fixture["engine"].recover_planning_transaction(
                    fixture["project"],
                    **self._arguments(fixture),
                    dry_run=False,
                )
            commit = result["planning_result_commit"]
            self.assertEqual(result["current_state"], "feature_ready")
            self.assertEqual(result["selected_feature"], "F070")
            self.assertEqual(git(repository, "rev-parse", f"{commit}^"), fixture["head"])
            self.assertEqual(git(repository, "rev-list", "--count", f"{fixture['head']}..{commit}"), "1")
            self.assertEqual(RepositoryInspector(repository).changed_paths(commit), EIGHT_PATHS)
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            projection = ProjectionEngine(
                fixture["ledger"],
                fixture["ledger"].path.parent / "projection-cache.json",
            ).rebuild(persist_cache=False)
            self.assertEqual(projection["current_state"], "feature_ready")
            self.assertIsNone(projection["current_feature"])
            self.assertEqual(projection["selected_next_feature"], "F070")
            self.assertIsNone(projection["active_transaction"])
            cycle = json.loads(
                (repository / ".factory/conveyor-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(cycle["selected_feature"], "F070")
            self.assertIsNone(cycle["current_feature"])
            self.assertEqual(cycle["current_phase"], "feature_ready")
            self.assertEqual(cycle["kernel_ledger_sequence"], projection["ledger_sequence"])
            self.assertEqual(
                cycle["kernel_projection_fingerprint"],
                projection["projection_fingerprint"],
            )
            recovery_events = [
                event
                for event in fixture["ledger"].read()
                if event["transaction_id"] == result["recovery_transaction_id"]
            ]
            self.assertEqual(
                len([event for event in recovery_events if event["event_type"] == "CommitFinalized"]),
                1,
            )
            self.assertFalse(
                any(event["event_type"] == "SessionLaunched" for event in recovery_events)
            )

    def test_invalid_parent_or_role_failure_evidence_is_rejected(self):
        mutations = {
            "role_failure_after_inspection": lambda report, transaction: report.update({
                "redacted_stdout": report["redacted_stdout"].replace(
                    "Reading additional input from stdin...",
                    "repository inspection completed",
                    1,
                )
            }),
            "role_mutation": lambda report, transaction: report.update({
                "redacted_stdout": report["redacted_stdout"].replace(
                    json.dumps(role_failure_events()[1], separators=(",", ":")),
                    json.dumps({
                        "type": "item.completed",
                        "item": {
                            "id": "mutation",
                            "type": "file_change",
                            "changes": [{"path": "docs/FEATURE_QUEUE.yaml"}],
                        },
                    }, separators=(",", ":"))
                    + "\n"
                    + json.dumps(role_failure_events()[1], separators=(",", ":")),
                    1,
                )
            }),
            "invalid_structured_result": lambda report, transaction: report.update({
                "structured_output_validation": "invalid",
            }),
            "missing_terminal_marker": lambda report, transaction: report.update({
                "terminal_marker_found": False,
            }),
            "wrong_workflow": lambda report, transaction: (
                report["structured_result"].update({"workflow_type": "feature_execution"}),
                report["parsed_structured_result"].update({"workflow_type": "feature_execution"}),
            ),
            "wrong_project": lambda report, transaction: (
                report["structured_result"].update({"project_id": "wrong"}),
                report["parsed_structured_result"].update({"project_id": "wrong"}),
            ),
            "wrong_repository": lambda report, transaction: (
                report["structured_result"].update({"repository_identity": "wrong"}),
                report["parsed_structured_result"].update({"repository_identity": "wrong"}),
            ),
            "wrong_starting_branch": lambda report, transaction: (
                report["structured_result"].update({"starting_branch": "codex/wrong"}),
                report["parsed_structured_result"].update({"starting_branch": "codex/wrong"}),
            ),
            "wrong_starting_head": lambda report, transaction: (
                report["structured_result"].update({"starting_commit": "0" * 40}),
                report["parsed_structured_result"].update({"starting_commit": "0" * 40}),
            ),
            "wrong_transaction": lambda report, transaction: (
                report["structured_result"].update({"transaction_id": "wrong"}),
                report["parsed_structured_result"].update({"transaction_id": "wrong"}),
            ),
            "wrong_session": lambda report, transaction: (
                report["structured_result"].update({"session_id": "wrong"}),
                report["parsed_structured_result"].update({"session_id": "wrong"}),
            ),
            "wrong_run": lambda report, transaction: (
                report["structured_result"].update({"run_id": "wrong"}),
                report["parsed_structured_result"].update({"run_id": "wrong"}),
            ),
            "contradictory_classification": lambda report, transaction: transaction.update({
                "result_classification": "RECONCILED_NO_READY_WORK",
            }),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = self._fixture(Path(temporary))
                report = json.loads(fixture["report_path"].read_text(encoding="utf-8"))
                transaction = json.loads(
                    fixture["transaction_path"].read_text(encoding="utf-8")
                )
                mutate(report, transaction)
                write_json(fixture["report_path"], report)
                write_json(fixture["transaction_path"], transaction)
                with self.assertRaises((RecoveryError, SchemaValidationError)):
                    self._dry_run(fixture)

    def test_boundary_diff_and_ready_selection_drift_is_rejected(self):
        mutations = {
            "wrong_branch": lambda fixture: setattr(
                fixture["project"], "milestone_branch", "codex/other"
            ),
            "wrong_head": lambda fixture: git(
                fixture["repository"], "commit", "--allow-empty", "-m", "head drift"
            ),
            "unauthorized_ninth_path": lambda fixture: (
                fixture["repository"] / "docs/README.md"
            ).write_text("extra planning path\n", encoding="utf-8"),
            "source_path": lambda fixture: (
                fixture["repository"] / "app.txt"
            ).write_text("source drift\n", encoding="utf-8"),
            "untracked_path": lambda fixture: (
                fixture["repository"] / "docs/untracked.md"
            ).write_text("untracked\n", encoding="utf-8"),
            "multiple_ready_features": lambda fixture: self._add_ready_feature(fixture),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                fixture = self._fixture(Path(temporary))
                if label == "wrong_branch":
                    fixture["project"] = replace(
                        fixture["project"], milestone_branch="codex/other"
                    )
                else:
                    mutate(fixture)
                with self.assertRaises(RecoveryError):
                    self._dry_run(fixture)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            arguments = self._arguments(fixture)
            arguments["expected_diff_fingerprint"] = "0" * 64
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ), self.assertRaisesRegex(RecoveryError, "diff_fingerprint"):
                fixture["engine"].recover_planning_transaction(
                    fixture["project"], **arguments, dry_run=True
                )

    @staticmethod
    def _add_ready_feature(fixture: dict) -> None:
        path = fixture["repository"] / "docs/FEATURE_QUEUE.yaml"
        queue = json.loads(path.read_text(encoding="utf-8"))
        queue["features"].append({
            "id": "F071",
            "title": "Second ready feature",
            "status": "ready",
            "priority": 71,
            "milestone": "M0",
            "dependencies": ["F068"],
            "spec": "docs/features/F070-applications-table.md",
            "acceptance_criteria": ["Synthetic second ready feature."],
            "requires_human_decision": False,
            "branch": None,
            "integration_base_commit": None,
            "accepted_commit": None,
            "integrated_commit": None,
            "integration_status": "pending",
            "integration_fix_commits": [],
        })
        write_json(path, queue)

    def test_validation_failure_creates_no_commit_and_preserves_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            repository = fixture["repository"]
            status_before = git(repository, "status", "--porcelain=v1")
            with (
                patch(
                    "development_conveyor.planning._inventory_validation",
                    return_value=fixture["inventory"],
                ),
                patch(
                    "development_conveyor.planning._diff_check",
                    side_effect=RecoveryError("synthetic diff validation failure"),
                ),
                self.assertRaisesRegex(RecoveryError, "synthetic diff validation failure"),
            ):
                fixture["engine"].recover_planning_transaction(
                    fixture["project"],
                    **self._arguments(fixture),
                    dry_run=False,
                )
            self.assertEqual(git(repository, "rev-parse", "HEAD"), fixture["head"])
            self.assertEqual(git(repository, "status", "--porcelain=v1"), status_before)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())


if __name__ == "__main__":
    unittest.main()
