from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import WorkflowType
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.planning import (
    _completed_integration_parent_evidence,
    compare_queue_validation_evidence,
    planning_report_path,
    queue_fingerprint,
)
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from tests.helpers import (
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


RUN_ID = "committed-planning-run"
SESSION_ID = "019f9d55-3a57-7a32-89ef-87d23418a06d"
PLANNING_TRANSACTION_ID = "04c22c36-3954-4e5a-b3d2-af7822eeffb4"
INTEGRATION_TRANSACTION_ID = "44544202-7736-4953-bea2-d990c74db29c"
PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/F006.md",
    "docs/features/F010.md",
    "docs/features/F073.md",
]
WARNINGS = ["M1 warning", "M2 warning"]


class CommittedPlanningRecoveryTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        checkpoints: list[dict] | None = None,
    ) -> dict:
        repository, project = synthetic_repository(
            root,
            controller_project_id="synthetic",
            feature_status="proposed",
        )
        project = replace(
            project,
            active_milestone="M0",
            milestone_branch="codex/m0-foundation",
            current_state="validation_failed",
        )
        integrated_commit = git(repository, "rev-parse", "HEAD")
        queue = {
            "schema_version": 1,
            "milestones": [
                {
                    "id": "M0",
                    "name": "Foundation",
                    "status": "active",
                    "base_commit": integrated_commit,
                    "integration_branch": "codex/m0-foundation",
                    "integrated_features": ["F072"],
                    "last_validated_commit": integrated_commit,
                    "human_gate": True,
                }
            ],
            "features": [
                {
                    "id": "F072",
                    "title": "Integrated feature",
                    "status": "integrated",
                    "priority": 72,
                    "milestone": "M0",
                    "dependencies": [],
                    "spec": "docs/features/F072.md",
                    "acceptance_criteria": ["F072 is integrated."],
                    "requires_human_decision": False,
                    "branch": "codex/F072",
                    "integration_base_commit": integrated_commit,
                    "accepted_commit": integrated_commit,
                    "integrated_commit": integrated_commit,
                    "integration_status": "passed",
                    "integration_fix_commits": [],
                },
                {
                    "id": "F073",
                    "title": "Ready feature",
                    "status": "proposed",
                    "priority": 73,
                    "milestone": "M0",
                    "dependencies": ["F072"],
                    "spec": "docs/features/F073.md",
                    "acceptance_criteria": ["F073 is ready."],
                    "requires_human_decision": False,
                    "execution_policy": {
                        "profile": "generic_or_architectural",
                        "parent_sessions": 1,
                        "child_sessions": 0,
                    },
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
        (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
        (repository / ".factory/locks/.gitignore").write_text(
            "writer.json\n", encoding="utf-8"
        )
        for relative in PATHS:
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative != "docs/FEATURE_QUEUE.yaml":
                target.write_text("Planning baseline.\n", encoding="utf-8")
        (repository / "docs/features/F072.md").write_text(
            "# F072\n\nIntegrated.\n", encoding="utf-8"
        )
        git(repository, "add", "docs", ".factory/locks/.gitignore")
        git(repository, "commit", "-m", "synthetic integrated baseline")
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
        terminal_snapshot = {
            "branch": project.milestone_branch,
            "head": starting_head,
            "clean": True,
            "git_operations": {
                "cherry_pick": False,
                "merge": False,
                "rebase_apply": False,
                "rebase_merge": False,
            },
        }
        integration_events = [
            ("TransactionStarted", {
                "run_id": "integration-run",
                "feature_id": "F072",
                "milestone": "M0",
                "starting_branch": project.milestone_branch,
                "starting_head": integrated_commit,
                "allowed_mutation_policy": {},
            }),
            ("LeaseAcquired", {
                "lease_id": "integration-lease",
                "lease_type": "integration_writer",
            }),
            ("SnapshotCaptured", {"snapshot": terminal_snapshot}),
            ("DeterministicExecutionStarted", {"model_session_launched": False}),
            ("DeterministicResultAccepted", {
                "classification": "INTEGRATED",
                "current_commit": starting_head,
            }),
            ("ValidationStarted", {}),
            ("ValidationPassed", {"commands": []}),
            ("CommitFinalized", {
                "commit": starting_head,
                "parent": integrated_commit,
                "accepted_commit": integrated_commit,
                "changed_paths": PATHS,
                "diff_fingerprint": inspector.patch_fingerprint(starting_head),
            }),
            ("TransactionCompleted", {
                "classification": "INTEGRATED",
                "feature_id": "F072",
                "accepted_feature_commit": integrated_commit,
                "integrated_commit": integrated_commit,
                "integration_status": "passed",
                "next_state": "feature_ready",
                "reference": starting_head,
                "terminal_snapshot": terminal_snapshot,
            }),
            ("LeaseReleased", {"lease_id": "integration-lease"}),
            ("ProjectionUpdated", {
                "current_state": "feature_ready",
                "current_feature": "F072",
                "selected_feature": None,
            }),
        ]
        for event_type, payload in integration_events:
            ledger.append(
                event_type=event_type,
                transaction_id=INTEGRATION_TRANSACTION_ID,
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                payload=payload,
            )

        queue["features"][1]["status"] = "ready"
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        for relative in PATHS:
            if relative != "docs/FEATURE_QUEUE.yaml":
                (repository / relative).write_text(
                    "F072 integrated. F073 ready.\n", encoding="utf-8"
                )
        changed_paths = inspector.tracked_changed_paths()
        envelope = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_READY_WORK",
            "project_id": project.project_id,
            "repository_identity": identity["repository_id"],
            "transaction_id": PLANNING_TRANSACTION_ID,
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
                    "ready_features": ["F073"],
                    "active_features": [],
                    "warning_count": 2,
                    "warnings": WARNINGS,
                    "warnings_scope": "Descriptive future milestone warnings.",
                    "blocking_warnings": [],
                }
            },
            "next_state": "feature_ready",
        }
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
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
            "redacted_stdout": "typed terminal result",
            "session_id": SESSION_ID,
        }
        write_json(report_path, report)
        git(repository, "add", *PATHS)
        git(repository, "commit", "-m", "factory: reconcile M0 queue")
        planning_commit = git(repository, "rev-parse", "HEAD")
        diff_fingerprint = inspector.patch_fingerprint(planning_commit)
        inventory = {
            "ok": True,
            "valid": True,
            "milestone_found": True,
            "active_milestone": "M0",
            "errors": [],
            "warnings": WARNINGS,
            "nonfatal_warnings": WARNINGS,
            "blocking_warnings": [],
            "warning_count": 2,
            "feature_count": 2,
            "global_feature_count": 2,
            "global_milestone_count": 1,
            "ready": ["F073"],
            "active": [],
            "exit_code": 0,
            "validator": "synthetic inventory validator",
        }
        comparison = compare_queue_validation_evidence(
            envelope["evidence"]["queue_validation"],
            inventory,
        )
        planning_transaction = {
            "schema_version": 1,
            "status": "planning_changes_committed",
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "planning_start_commit": starting_head,
            "planning_result_commit": planning_commit,
            "planning_commit_status": "committed",
            "selected_feature": "F073",
            "selected_feature_starting_commit": planning_commit,
            "changed_paths": changed_paths,
            "diff_fingerprint": diff_fingerprint,
            "queue_fingerprint": queue_fingerprint(project),
            "queue_validation_evidence": comparison,
            "result_classification": "RECONCILED_READY_WORK",
            "reconciliation_report": str(report_path),
        }
        write_json(
            planning_report_path(engine.root / "reports", RUN_ID),
            planning_transaction,
        )
        planning_snapshot = {
            **terminal_snapshot,
            "head": planning_commit,
        }
        checkpoint_payloads = (
            [
                {"checkpoint": "runtime_ignore_verified"},
                {"checkpoint": "planning_session_reserved"},
            ]
            if checkpoints is None
            else checkpoints
        )
        planning_events = [
            ("TransactionStarted", {
                "run_id": RUN_ID,
                "feature_id": None,
                "milestone": "M0",
                "starting_branch": project.milestone_branch,
                "starting_head": starting_head,
                "allowed_mutation_policy": {
                    "allowed_paths": PATHS,
                    "allowed_prefixes": [],
                },
            }),
            ("LeaseAcquired", {
                "lease_id": "planning-lease",
                "lease_type": "planning_writer",
            }),
            ("SnapshotCaptured", {"snapshot": terminal_snapshot}),
            *[
                ("CheckpointRecorded", payload)
                for payload in checkpoint_payloads[:1]
            ],
            ("SessionLaunched", {"session_id": SESSION_ID}),
            *[
                ("CheckpointRecorded", payload)
                for payload in checkpoint_payloads[1:]
            ],
            ("SessionResultAccepted", {
                "classification": "RECONCILED_READY_WORK",
                "next_state": "feature_ready",
                "changed_paths": changed_paths,
                "envelope": envelope,
            }),
            ("ChangesDetected", {
                "changed_paths": changed_paths,
                "diff_fingerprint": diff_fingerprint,
            }),
            ("ValidationStarted", {"changed_paths": changed_paths}),
            ("ValidationPassed", {"diff_fingerprint": diff_fingerprint}),
            ("CommitFinalized", {
                "commit": planning_commit,
                "parent": starting_head,
                "changed_paths": changed_paths,
                "diff_fingerprint": diff_fingerprint,
                "commit_subject": "factory: reconcile M0 queue",
            }),
            ("TransactionBlocked", {
                "classification": "PLANNING_SEMANTIC_CONFLICT",
                "terminal_state": "terminal_failure",
                "next_state": "validation_failed",
                "terminal_snapshot": planning_snapshot,
            }),
            ("LeaseReleased", {"lease_id": "planning-lease"}),
            ("ProjectionUpdated", {
                "current_state": "validation_failed",
                "current_feature": None,
                "selected_feature": None,
            }),
        ]
        for event_type, payload in planning_events:
            ledger.append(
                event_type=event_type,
                transaction_id=PLANNING_TRANSACTION_ID,
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                payload=payload,
            )
        ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        ).rebuild(persist_cache=True)
        return {
            "repository": repository,
            "project": project,
            "engine": engine,
            "ledger": ledger,
            "starting_head": starting_head,
            "planning_commit": planning_commit,
            "diff_fingerprint": diff_fingerprint,
            "inventory": inventory,
        }

    def test_checkpoint_metadata_is_optional_and_repeatable(self):
        variants = (
            [],
            [{"checkpoint": "one"}],
            [
                {"checkpoint": "one"},
                {"checkpoint": "two"},
                {"checkpoint": "three"},
            ],
        )
        for checkpoints in variants:
            with self.subTest(count=len(checkpoints)):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = self._fixture(
                        Path(temporary),
                        checkpoints=checkpoints,
                    )
                    with patch(
                        "development_conveyor.planning._inventory_validation",
                        return_value=fixture["inventory"],
                    ):
                        result = fixture[
                            "engine"
                        ].recover_planning_transaction(
                            **self._arguments(fixture),
                            dry_run=True,
                        )
                    evidence = result["planning_finalization_recovery"][
                        "checkpoint_evidence"
                    ]
                    self.assertEqual(evidence["count"], len(checkpoints))
                    self.assertTrue(evidence["transparent"])

    def test_conflicting_checkpoint_identity_is_rejected(self):
        conflicts = {
            "identity": {"run_id": "conflicting-run"},
            "paths": {"final_paths": ["docs/not-authorized.md"]},
            "fingerprint": {"diff_fingerprint": "0" * 64},
            "session": {"session_ids": ["conflicting-session"]},
            "authority": {"allowed_paths": ["docs/not-authorized.md"]},
            "session_count": {"model_session_count": 2},
        }
        for name, conflict in conflicts.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = self._fixture(
                        Path(temporary),
                        checkpoints=[
                            {
                                "checkpoint": "arbitrary-name",
                                **conflict,
                            }
                        ],
                    )
                    with patch(
                        "development_conveyor.planning._inventory_validation",
                        return_value=fixture["inventory"],
                    ):
                        with self.assertRaisesRegex(
                            RecoveryError,
                            "planning checkpoint",
                        ):
                            fixture[
                                "engine"
                            ].recover_planning_transaction(
                                **self._arguments(fixture),
                                dry_run=True,
                            )

    def test_singleton_ready_feature_is_derived_when_selection_is_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            report_path = (
                fixture["engine"].root
                / "reports"
                / RUN_ID
                / "queue_reconciliation.json"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["structured_result"]["evidence"][
                "queue_validation"
            ].pop("selected_feature", None)
            report["parsed_structured_result"] = report["structured_result"]
            write_json(report_path, report)
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ):
                result = fixture["engine"].recover_planning_transaction(
                    **self._arguments(fixture),
                    dry_run=True,
                )
            self.assertEqual(
                result["planning_finalization_recovery"]["selected_feature"],
                "F073",
            )

    def test_empty_or_multiple_ready_identity_is_rejected(self):
        variants = (
            {"selected_feature": ""},
            {"ready_features": ["F073", "F999"]},
        )
        for mutation in variants:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = self._fixture(Path(temporary))
                    report_path = (
                        fixture["engine"].root
                        / "reports"
                        / RUN_ID
                        / "queue_reconciliation.json"
                    )
                    report = json.loads(
                        report_path.read_text(encoding="utf-8")
                    )
                    report["structured_result"]["evidence"][
                        "queue_validation"
                    ].update(mutation)
                    report["parsed_structured_result"] = report[
                        "structured_result"
                    ]
                    write_json(report_path, report)
                    with patch(
                        "development_conveyor.planning._inventory_validation",
                        return_value=fixture["inventory"],
                    ):
                        with self.assertRaisesRegex(
                            RecoveryError,
                            "selected-feature|ready|selection",
                        ):
                            fixture[
                                "engine"
                            ].recover_planning_transaction(
                                **self._arguments(fixture),
                                dry_run=True,
                            )

    @staticmethod
    def _arguments(fixture: dict) -> dict:
        return {
            "project": fixture["project"],
            "run_id": RUN_ID,
            "expected_starting_head": fixture["starting_head"],
            "expected_diff_fingerprint": fixture["diff_fingerprint"],
            "expected_changed_paths": PATHS,
            "expected_session_id": SESSION_ID,
        }

    def test_committed_then_blocked_dry_run_reuses_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            before = fixture["ledger"].path.read_bytes()
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ):
                result = fixture["engine"].recover_planning_transaction(
                    **self._arguments(fixture),
                    dry_run=True,
                )
            plan = result["planning_finalization_recovery"]
            self.assertEqual(plan["existing_commit"], fixture["planning_commit"])
            self.assertEqual(plan["selected_feature"], "F073")
            self.assertEqual(plan["expected_final_state"], "feature_ready")
            self.assertEqual(plan["planning_commits_that_would_be_created"], 0)
            self.assertEqual(plan["post_integration_descendant"]["feature_id"], "F072")
            self.assertFalse(plan["application_mutation_expected"])
            self.assertEqual(fixture["ledger"].path.read_bytes(), before)

    def test_apply_projects_ready_and_second_apply_is_validation_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            repository = fixture["repository"]
            app_before = (repository / "app.txt").read_bytes()
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ):
                first = fixture["engine"].recover_planning_transaction(
                    **self._arguments(fixture),
                    dry_run=False,
                )
            ledger_after_first = fixture["ledger"].path.read_bytes()
            with patch(
                "development_conveyor.planning._inventory_validation",
                side_effect=AssertionError("idempotent apply must not validate"),
            ):
                second = fixture["engine"].recover_planning_transaction(
                    **self._arguments(fixture),
                    dry_run=False,
                )
            self.assertEqual(first["current_state"], "feature_ready")
            self.assertEqual(first["selected_feature"], "F073")
            self.assertEqual(
                first["planning_result_commit"], fixture["planning_commit"]
            )
            self.assertEqual(
                second["outcome"], "planning_transaction_already_recovered"
            )
            self.assertEqual(second["validations_performed"], [])
            self.assertEqual(fixture["ledger"].path.read_bytes(), ledger_after_first)
            self.assertEqual(git(repository, "rev-parse", "HEAD"), fixture["planning_commit"])
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertTrue(RepositoryInspector(repository).is_clean)
            projection = ProjectionEngine(
                fixture["ledger"],
                fixture["ledger"].path.parent / "projection-cache.json",
            ).rebuild(persist_cache=False)
            self.assertEqual(projection["current_state"], "feature_ready")
            self.assertEqual(projection["current_feature"], "F073")
            self.assertEqual(projection["selected_next_feature"], "F073")
            self.assertEqual(
                projection["selected_feature_starting_commit"],
                fixture["planning_commit"],
            )
            self.assertIsNone(projection["active_transaction"])
            self.assertIsNone(projection["human_gate"])

    def test_completed_integration_descendant_is_not_interrupted_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=fixture["inventory"],
            ):
                plan = fixture["engine"].project_plan(fixture["project"])
                context = fixture[
                    "engine"
                ]._integration_finalization_recovery_context(
                    fixture["project"]
                )
            self.assertEqual(plan["proposed_next_action"], "planning_finalization")
            self.assertIsNone(context)
            consistency = ConsistencyChecker(
                controller_root=fixture["engine"].root,
                project=fixture["project"],
                planner_observer=lambda: plan,
            ).check()
            failures = {
                item["invariant"]: item.get("diagnostic")
                for item in consistency["failed_invariants"]
            }
            self.assertNotIn("execution_plan_agreement", failures)
            self.assertNotIn("integration_commit_ancestry", failures)

    def test_unfinished_or_malformed_integration_parent_is_rejected(self):
        parent = "a" * 40
        incomplete_projection = {
            "transactions": [
                {
                    "transaction_id": INTEGRATION_TRANSACTION_ID,
                    "workflow_type": "milestone_integration",
                    "state": "terminal_failure",
                    "terminal_classification": "VALIDATION_FAILED",
                    "last_sequence": 4,
                }
            ]
        }
        with self.assertRaisesRegex(RecoveryError, "unfinished"):
            _completed_integration_parent_evidence(
                ledger_events=[],
                projection=incomplete_projection,
                planning_start_sequence=5,
                planning_parent=parent,
            )

        completed_projection = {
            "transactions": [
                {
                    "transaction_id": INTEGRATION_TRANSACTION_ID,
                    "workflow_type": "milestone_integration",
                    "state": "completed",
                    "terminal_classification": "INTEGRATED",
                    "last_sequence": 3,
                    "terminal_snapshot": {
                        "branch": "codex/m0-foundation",
                        "head": parent,
                        "clean": True,
                        "git_operations": {},
                    },
                }
            ]
        }
        malformed_events = [
            {
                "sequence": 1,
                "transaction_id": INTEGRATION_TRANSACTION_ID,
                "event_type": "TransactionCompleted",
                "payload": {"classification": "INTEGRATED"},
            },
            {
                "sequence": 2,
                "transaction_id": INTEGRATION_TRANSACTION_ID,
                "event_type": "ProjectionUpdated",
                "payload": {"current_state": "feature_ready"},
            },
        ]
        with self.assertRaisesRegex(RecoveryError, "terminal_events"):
            _completed_integration_parent_evidence(
                ledger_events=malformed_events,
                projection=completed_projection,
                planning_start_sequence=4,
                planning_parent=parent,
            )


if __name__ == "__main__":
    unittest.main()
