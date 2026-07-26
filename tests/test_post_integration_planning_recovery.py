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
from development_conveyor.locks import make_lock_record
from development_conveyor.planning import (
    plan_new_ready_execution_policy_completion,
    planning_report_path,
)
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
SEVEN_PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/F070-applications-table.md",
    "docs/features/F072-job-application-detail-workspace.md",
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
    def _fixture(
        self,
        root: Path,
        *,
        completed_feature: str = "F068",
        selected_feature: str = "F070",
        paths: list[str] = EIGHT_PATHS,
    ):
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
                "integrated_features": [completed_feature],
                "last_validated_commit": integrated,
                "human_gate": True,
            }],
            "features": [
                {
                    "id": completed_feature,
                    "title": (
                        "Job Application Data Model"
                        if completed_feature == "F068"
                        else "Applications Table"
                    ),
                    "status": "integrated",
                    "priority": 68,
                    "milestone": "M0",
                    "dependencies": [],
                    "spec": (
                        "docs/features/F068-job-application-data-model.md"
                        if completed_feature == "F068"
                        else "docs/features/F070-applications-table.md"
                    ),
                    "acceptance_criteria": [
                        f"{completed_feature} remains integrated exactly once."
                    ],
                    "requires_human_decision": False,
                    "branch": f"codex/{completed_feature.lower()}-accepted",
                    "integration_base_commit": integrated,
                    "accepted_commit": integrated,
                    "integrated_commit": integrated,
                    "integration_status": "passed",
                    "integration_fix_commits": [],
                },
                {
                    "id": selected_feature,
                    "title": (
                        "Applications Table"
                        if selected_feature == "F070"
                        else "Applications Table Interactions and Detail View"
                    ),
                    "status": "proposed",
                    "priority": 70,
                    "milestone": "M0",
                    "dependencies": [completed_feature],
                    "spec": (
                        "docs/features/F070-applications-table.md"
                        if selected_feature == "F070"
                        else "docs/features/F072-job-application-detail-workspace.md"
                    ),
                    "acceptance_criteria": [
                        f"{selected_feature} provides bounded application behavior."
                    ],
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
        for relative in paths:
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative == "docs/FEATURE_QUEUE.yaml":
                continue
            target.write_text(
                (
                    "# Planning baseline\n\n"
                    f"{completed_feature} integrated. {selected_feature} proposed.\n"
                ),
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
            allowed_paths=paths,
            commit_subject=(
                f"factory: reconcile M0 queue and ready {selected_feature}"
            ),
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
        for relative in paths:
            if relative == "docs/FEATURE_QUEUE.yaml":
                continue
            (repository / relative).write_text(
                (
                    "# Reconciled planning\n\n"
                    f"{completed_feature} integrated. {selected_feature} ready.\n"
                )
                +
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
                    "ready_features": [selected_feature],
                    "active_features": [],
                    "selected_feature": selected_feature,
                    "dependencies_complete": True,
                    "warning_count": 0,
                    "blocking_warnings": [],
                },
                "summary": (
                    f"{completed_feature} synchronized; "
                    f"{selected_feature} marked ready."
                ),
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
                        "ready": [selected_feature],
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
        if selected_feature == "F072":
            envelope["evidence"]["queue_validation"].pop(
                "selected_feature", None
            )
            envelope["evidence"]["queue_validation"].pop(
                "dependencies_complete", None
            )
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
            "error": (
                f"newly readied feature {selected_feature} "
                "lacks required execution_policy"
            ),
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
            "ready": [selected_feature],
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
            "paths": paths,
            "completed_feature": completed_feature,
            "selected_feature": selected_feature,
        }

    @staticmethod
    def _arguments(fixture: dict) -> dict:
        return {
            "run_id": RUN_ID,
            "expected_starting_head": fixture["head"],
            "expected_diff_fingerprint": fixture["diff"],
            "expected_changed_paths": fixture["paths"],
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

    def test_f072_missing_policy_dry_run_predicts_exact_normalization_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                completed_feature="F070",
                selected_feature="F072",
                paths=SEVEN_PATHS,
            )
            repository = fixture["repository"]
            status_before = git(repository, "status", "--porcelain=v1", "--branch")
            queue_before = (
                repository / "docs/FEATURE_QUEUE.yaml"
            ).read_bytes()
            spec_path = (
                repository
                / "docs/features/F072-job-application-detail-workspace.md"
            )
            spec_before = spec_path.read_bytes()

            result = self._dry_run(fixture)
            plan = result["planning_finalization_recovery"]
            completion = plan["execution_policy_completion"]

            self.assertEqual(plan["selected_feature"], "F072")
            self.assertEqual(plan["ready_features"], ["F072"])
            self.assertEqual(
                plan["recovered_failure_classification"],
                "deterministic_execution_policy_completion",
            )
            self.assertEqual(plan["existing_planning_changes"]["paths"], SEVEN_PATHS)
            self.assertTrue(completion["required"])
            self.assertEqual(
                completion["execution_policy"],
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 0,
                },
            )
            self.assertEqual(
                completion["normalization_paths"],
                [
                    "docs/FEATURE_QUEUE.yaml",
                    "docs/features/F072-job-application-detail-workspace.md",
                ],
            )
            self.assertEqual(
                (
                    completion["resolved_execution_profile"]["model"],
                    completion["resolved_execution_profile"]["reasoning"],
                    completion["resolved_execution_profile"]["parent_sessions"],
                    completion["resolved_execution_profile"]["child_sessions"],
                    completion["policy_source"],
                ),
                ("gpt-5.6-sol", "medium", 1, 0, "workflow_fallback"),
            )
            self.assertNotEqual(
                plan["authenticated_original_diff_fingerprint"],
                plan["predicted_final_diff_fingerprint"],
            )
            self.assertEqual(result["model_sessions_that_would_launch"], [])
            self.assertEqual(result["child_sessions_that_would_launch"], [])
            self.assertEqual(
                git(repository, "status", "--porcelain=v1", "--branch"),
                status_before,
            )
            self.assertEqual(
                (repository / "docs/FEATURE_QUEUE.yaml").read_bytes(),
                queue_before,
            )
            self.assertEqual(spec_path.read_bytes(), spec_before)

    def test_f072_missing_policy_apply_normalizes_and_commits_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                completed_feature="F070",
                selected_feature="F072",
                paths=SEVEN_PATHS,
            )
            repository = fixture["repository"]
            app_before = (repository / "app.txt").read_bytes()
            with (
                patch(
                    "development_conveyor.planning._inventory_validation",
                    return_value=fixture["inventory"],
                ),
                patch.object(
                    fixture["engine"],
                    "_compatibility_snapshot",
                    return_value={
                        "compatible": True,
                        "effective_model": "gpt-5.6-sol",
                        "effective_reasoning": "medium",
                        "policy_source": "workflow_fallback",
                    },
                ) as compatibility,
            ):
                result = fixture["engine"].recover_planning_transaction(
                    fixture["project"],
                    **self._arguments(fixture),
                    dry_run=False,
                )

            commit = result["planning_result_commit"]
            self.assertEqual(
                git(repository, "rev-parse", f"{commit}^"),
                fixture["head"],
            )
            self.assertEqual(
                git(repository, "rev-list", "--count", f"{fixture['head']}..{commit}"),
                "1",
            )
            self.assertEqual(
                RepositoryInspector(repository).changed_paths(commit),
                SEVEN_PATHS,
            )
            queue = json.loads(
                (repository / "docs/FEATURE_QUEUE.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                queue["features"][1]["execution_policy"],
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 0,
                },
            )
            specification = (
                repository
                / "docs/features/F072-job-application-detail-workspace.md"
            ).read_text(encoding="utf-8")
            self.assertIn("## Execution policy", specification)
            self.assertIn("profile: generic_or_architectural", specification)
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertEqual(result["current_state"], "feature_ready")
            self.assertEqual(result["selected_feature"], "F072")
            self.assertEqual(result["model_sessions_launched"], [])
            self.assertEqual(result["child_sessions_launched"], [])
            projection = ProjectionEngine(
                fixture["ledger"],
                fixture["ledger"].path.parent / "projection-cache.json",
            ).rebuild(persist_cache=False)
            self.assertIsNone(projection["current_feature"])
            self.assertEqual(projection["selected_next_feature"], "F072")
            compatibility.assert_called_once()

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

    def test_f072_live_ownership_boundaries_fail_closed(self):
        cases = ("writer_lease", "controller_reservation", "autopilot_owner")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = self._fixture(
                    Path(temporary),
                    completed_feature="F070",
                    selected_feature="F072",
                    paths=SEVEN_PATHS,
                )
                repository = fixture["repository"]
                reservation = None
                if case == "writer_lease":
                    write_json(
                        repository / ".factory/locks/writer.json",
                        {"foreign": True},
                    )
                elif case == "controller_reservation":
                    inspector = RepositoryInspector(repository)
                    reservation = fixture["engine"]._launch_lock(
                        fixture["project"], inspector
                    )
                    reservation.acquire(make_lock_record(
                        project_id=fixture["project"].project_id,
                        repository_identity=inspector.identity()["repository_id"],
                        run_id="foreign-run",
                        current_feature="F072",
                        current_phase="foreign",
                    ))
                else:
                    state_root = fixture["engine"].configuration.owned_path(
                        fixture["engine"].configuration.conveyor[
                            "state_directory"
                        ]
                    )
                    write_json(
                        state_root
                        / "autopilot"
                        / fixture["project"].project_id
                        / "ownership.json",
                        {
                            "project_id": fixture["project"].project_id,
                            "process_id": -1,
                        },
                    )
                try:
                    with self.assertRaises(RecoveryError):
                        self._dry_run(fixture)
                finally:
                    if reservation is not None:
                        reservation.release("foreign-run")

    def test_policy_completion_rejects_no_ready_and_multiple_missing_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(
                root,
                feature_status="proposed",
                controller_project_id="interview-companion",
            )
            inspector = RepositoryInspector(repository)
            starting_head = inspector.head
            with self.assertRaisesRegex(RecoveryError, "matching newly ready"):
                plan_new_ready_execution_policy_completion(
                    project,
                    inspector,
                    starting_head=starting_head,
                    profile_configuration=None,
                    required_missing_feature="F001",
                )

            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"].append({
                "id": "F002",
                "title": "Second candidate",
                "status": "proposed",
                "priority": 2,
                "milestone": "M0",
                "dependencies": [],
                "spec": "docs/features/F002.md",
                "acceptance_criteria": ["Synthetic candidate."],
                "requires_human_decision": False,
                "branch": None,
                "integration_base_commit": None,
                "accepted_commit": None,
                "integrated_commit": None,
                "integration_status": "pending",
                "integration_fix_commits": [],
            })
            write_json(queue_path, queue)
            (repository / "docs/features/F002.md").write_text(
                "# F002\n",
                encoding="utf-8",
            )
            git(repository, "add", "docs")
            git(repository, "commit", "-m", "add second proposed feature")
            starting_head = git(repository, "rev-parse", "HEAD")
            queue["features"][0]["status"] = "ready"
            queue["features"][1]["status"] = "ready"
            write_json(queue_path, queue)
            with self.assertRaisesRegex(
                RecoveryError,
                "multiple newly ready features require execution-policy judgment",
            ):
                plan_new_ready_execution_policy_completion(
                    project,
                    RepositoryInspector(repository),
                    starting_head=starting_head,
                    profile_configuration=None,
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
