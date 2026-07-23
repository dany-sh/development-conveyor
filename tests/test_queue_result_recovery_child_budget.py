from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.contracts import (
    TERMINAL_ENVELOPE_MARKER,
    TransactionState,
    WorkflowType,
    extract_terminal_envelope,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError, SchemaValidationError, SessionError
from development_conveyor.kernel import QueueReconciliationAdapter, WorkflowKernel
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.planning import planning_report_path
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionLauncher
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


RUN_ID = "f5a92b3b-8aa8-4a62-9d05-fc5754351435"
SESSION_ID = "019f9056-3267-7181-acf9-5596767837c5"
TRANSACTION_ID = "3250bdab-b2bb-4e46-9710-139f67509aa9"
TEN_PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/F006-permission-and-privacy-center.md",
    "docs/features/F009-transcription-engine-abstraction.md",
    "docs/features/F010-diagnostics-and-latency-instrumentation.md",
    "docs/features/F011-automated-test-harness.md",
    "docs/features/F097-imported-audio-transcription-workflow.md",
]


def assistant_event(text: str) -> str:
    return json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    })


class QueueResultRecoveryChildBudgetTests(unittest.TestCase):
    def _corroboration(self) -> dict[str, str]:
        return {
            "workflow_type": "queue_reconciliation",
            "project_id": "interview-companion",
            "repository_identity": "repository-identity",
            "transaction_id": TRANSACTION_ID,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "starting_branch": "codex/m0-foundation",
            "starting_commit": "9" * 40,
        }

    def _legacy_envelope(self) -> dict:
        return {
            "schema_version": 1,
            "transaction_id": TRANSACTION_ID,
            "repository": "repository-identity",
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "starting_branch": "codex/m0-foundation",
            "starting_commit": "9" * 40,
            "current_commit": "9" * 40,
            "feature": None,
            "result": "RECONCILED_READY_WORK",
            "next_state": "feature_preparation",
            "changed_paths": TEN_PATHS,
            "evidence": {
                "queue_validation": {
                    "ok": True,
                    "warning_count": 18,
                    "warnings_scope": "Known non-blocking future milestone warnings.",
                    "blocking_warnings": [],
                    "feature_count": 5,
                    "global_feature_count": 98,
                    "global_milestone_count": 10,
                    "ready": ["F010"],
                }
            },
        }

    def test_compatible_legacy_fields_and_missing_controller_fields_normalize(self):
        output = assistant_event(
            TERMINAL_ENVELOPE_MARKER
            + json.dumps(self._legacy_envelope(), separators=(",", ":"))
        )
        envelope = extract_terminal_envelope(
            output, corroborated=self._corroboration()
        )
        self.assertEqual(envelope.project_id, "interview-companion")
        self.assertEqual(envelope.repository_identity, "repository-identity")
        self.assertEqual(envelope.workflow_type, WorkflowType.QUEUE_RECONCILIATION)
        self.assertEqual(envelope.classification, "RECONCILED_READY_WORK")
        self.assertEqual(envelope.next_state, "feature_ready")

    def test_identity_conflicts_refuse_normalization(self):
        cases = {
            "repository": {"repository_identity": "other"},
            "project_id": {"project_id": "other-project"},
            "workflow_type": {"workflow_type": "feature_execution"},
            "classification": {"classification": "RECONCILED_NO_READY_WORK"},
        }
        for label, addition in cases.items():
            with self.subTest(label=label):
                value = {**self._legacy_envelope(), **addition}
                output = assistant_event(
                    TERMINAL_ENVELOPE_MARKER
                    + json.dumps(value, separators=(",", ":"))
                )
                with self.assertRaises(SchemaValidationError):
                    extract_terminal_envelope(
                        output, corroborated=self._corroboration()
                    )

    def test_zero_child_runtime_contract_disables_agents_and_rejects_calls(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"type": "collab_tool_call", "tool": "spawn_agent"},
        })
        self.assertEqual(
            SessionLauncher._collaboration_tool_call(line), "spawn_agent"
        )
        with self.assertRaisesRegex(
            SessionError, "zero-child execution plan observed prohibited"
        ):
            SessionLauncher._enforce_child_session_budget(line, 0)
        SessionLauncher._enforce_child_session_budget(line, 1)
        self.assertIsNone(
            SessionLauncher._collaboration_tool_call(
                json.dumps({"type": "item.started", "item": {"type": "command_execution"}})
            )
        )

    def _retained_fixture(self, root: Path):
        repository, project = synthetic_repository(
            root,
            feature_status="proposed",
            controller_project_id="interview-companion",
        )
        project = replace(project, current_state="queue_reconciliation")
        initial = git(repository, "rev-parse", "HEAD")
        (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
        (repository / ".factory/locks/.gitignore").write_text(
            "writer.json\n", encoding="utf-8"
        )
        features = []
        for feature_id in ("F006", "F009", "F010", "F011", "F097"):
            spec = next(path for path in TEN_PATHS if f"/{feature_id}-" in path)
            features.append({
                "id": feature_id,
                "title": feature_id,
                "status": "integrated" if feature_id == "F009" else "proposed",
                "priority": int(feature_id[1:]),
                "milestone": "M0",
                "dependencies": ["F009"] if feature_id == "F010" else [],
                "spec": spec,
                "acceptance_criteria": [f"{feature_id} remains evidence-backed"],
                "requires_human_decision": feature_id in {"F006", "F011", "F097"},
                "branch": None,
                "integration_base_commit": initial if feature_id == "F009" else None,
                "accepted_commit": initial if feature_id == "F009" else None,
                "integrated_commit": initial if feature_id == "F009" else None,
                "integration_status": "passed" if feature_id == "F009" else "pending",
                "integration_fix_commits": [],
            })
        queue = {
            "schema_version": 1,
            "milestones": [{
                "id": "M0",
                "name": "Synthetic milestone",
                "status": "active",
                "base_commit": initial,
                "integration_branch": "codex/m0-foundation",
                "integrated_features": ["F009"],
                "last_validated_commit": initial,
                "human_gate": True,
            }],
            "features": features,
        }
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        for path in TEN_PATHS:
            target = repository / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(f"# {path}\n\nBaseline proposed state.\n", encoding="utf-8")
        git(repository, "add", "docs", ".factory/locks/.gitignore")
        git(repository, "commit", "-m", "synthetic retained planning baseline")
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
            allowed_paths=TEN_PATHS,
            commit_subject="factory: reconcile M0 queue",
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

        queue["features"][2].update({
            "status": "ready",
            "execution_policy": {
                "profile": "bounded_precise",
                "parent_sessions": 1,
                "child_sessions": 0,
            },
        })
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        for path in TEN_PATHS:
            if path == "docs/FEATURE_QUEUE.yaml":
                continue
            feature = "F010" if "F010" in path or "/" not in path.removeprefix("docs/") else Path(path).stem.split("-")[0]
            (repository / path).write_text(
                f"# {feature}\n\nF009 integrated. F010 ready. "
                "F006 F011 F097 human_decision_required.\n",
                encoding="utf-8",
            )
        changed_paths = inspector.tracked_changed_paths()
        diff_fingerprint = inspector.planning_diff_fingerprint()
        envelope = {
            **self._legacy_envelope(),
            "repository": identity["repository_id"],
            "starting_commit": starting_head,
            "current_commit": starting_head,
            "changed_paths": changed_paths,
        }
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": SESSION_ID}),
            json.dumps({
                "type": "item.started",
                "item": {"type": "collab_tool_call", "tool": "spawn_agent"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "receiver_thread_ids": ["historical-child-session"],
                    "status": "completed",
                },
            }),
            assistant_event(
                TERMINAL_ENVELOPE_MARKER
                + json.dumps(envelope, separators=(",", ":"))
            ),
        ])
        report_path = engine.root / "reports" / RUN_ID / "queue_reconciliation.json"
        write_json(report_path, {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "action": "queue_reconciliation",
            "working_directory": str(repository),
            "exit_status": 0,
            "structured_output_validation": "invalid",
            "result_classification": "structured_output_invalid",
            "structured_result": None,
            "parsed_structured_result": None,
            "terminal_marker_found": True,
            "redacted_stdout": stdout,
            "session_id": SESSION_ID,
        })
        write_json(planning_report_path(engine.root / "reports", RUN_ID), {
            "schema_version": 1,
            "status": "terminal_planning_failure",
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "branch": project.milestone_branch,
            "planning_start_commit": starting_head,
            "changed_paths": changed_paths,
            "diff_fingerprint": diff_fingerprint,
            "result_classification": "structured_output_invalid",
            "failure_classification": "structured_output_invalid",
            "reconciliation_report": str(report_path),
            "error": "structured output was rejected before compatible normalization",
        })
        kernel.block(
            state=TransactionState.RETRYABLE_FAILURE,
            classification="RETRYABLE_PLANNING_FAILURE",
            next_state="validation_failed",
        )
        inventory = {
            "ok": True,
            "valid": True,
            "milestone_found": True,
            "active_milestone": "M0",
            "exit_code": 0,
            "errors": [],
            "warnings": [f"future warning {index}" for index in range(18)],
            "nonfatal_warnings": [f"future warning {index}" for index in range(18)],
            "blocking_warnings": [],
            "warning_count": 18,
            "feature_count": 5,
            "global_feature_count": 98,
            "global_milestone_count": 10,
            "ready": ["F010"],
            "active": [],
            "validator": "synthetic inventory validator",
        }
        return (
            repository, project, engine, ledger, starting_head,
            diff_fingerprint, inventory,
        )

    def test_exact_retained_diff_recovery_and_idempotent_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._retained_fixture(Path(temporary))
            repository, project, engine, ledger, head, diff, inventory = fixture
            ledger_before = ledger.path.read_bytes()
            with patch(
                "development_conveyor.planning._inventory_validation",
                return_value=inventory,
            ):
                dry = engine.recover_planning_transaction(
                    project,
                    run_id=RUN_ID,
                    expected_starting_head=head,
                    expected_diff_fingerprint=diff,
                    expected_changed_paths=TEN_PATHS,
                    expected_session_id=SESSION_ID,
                    dry_run=True,
                )
                plan = dry["planning_finalization_recovery"]
                self.assertEqual(plan["selected_feature"], "F010")
                self.assertEqual(plan["result_classification"], "RECONCILED_READY_WORK")
                self.assertEqual(plan["terminal_normalization"]["next_state"], "feature_ready")
                self.assertTrue(plan["historical_child_session_attempts"])
                self.assertEqual(ledger.path.read_bytes(), ledger_before)
                result = engine.recover_planning_transaction(
                    project,
                    run_id=RUN_ID,
                    expected_starting_head=head,
                    expected_diff_fingerprint=diff,
                    expected_changed_paths=TEN_PATHS,
                    expected_session_id=SESSION_ID,
                    dry_run=False,
                )
            self.assertEqual(result["current_state"], "feature_ready")
            self.assertEqual(result["selected_feature"], "F010")
            self.assertEqual(
                RepositoryInspector(repository).changed_paths(
                    result["planning_result_commit"]
                ),
                TEN_PATHS,
            )
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            committed_head = git(repository, "rev-parse", "HEAD")
            ledger_after = ledger.path.read_bytes()
            with self.assertRaisesRegex(
                RecoveryError, "no deterministic transactional planning recovery"
            ):
                engine.recover_planning_transaction(
                    project,
                    run_id=RUN_ID,
                    expected_starting_head=head,
                    expected_diff_fingerprint=diff,
                    expected_changed_paths=TEN_PATHS,
                    expected_session_id=SESSION_ID,
                    dry_run=False,
                )
            self.assertEqual(git(repository, "rev-parse", "HEAD"), committed_head)
            self.assertEqual(ledger.path.read_bytes(), ledger_after)

    def test_partial_recovery_failure_preserves_retained_diff_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._retained_fixture(Path(temporary))
            repository, project, engine, ledger, head, diff, inventory = fixture
            status_before = git(repository, "status", "--porcelain=v1")
            with (
                patch(
                    "development_conveyor.planning._inventory_validation",
                    return_value=inventory,
                ),
                patch.object(
                    WorkflowKernel,
                    "finalize_deterministic_planning_recovery",
                    side_effect=RecoveryError("synthetic partial recovery failure"),
                ),
            ):
                with self.assertRaisesRegex(
                    RecoveryError, "synthetic partial recovery failure"
                ):
                    engine.recover_planning_transaction(
                        project,
                        run_id=RUN_ID,
                        expected_starting_head=head,
                        expected_diff_fingerprint=diff,
                        expected_changed_paths=TEN_PATHS,
                        expected_session_id=SESSION_ID,
                        dry_run=False,
                    )
            self.assertEqual(git(repository, "rev-parse", "HEAD"), head)
            self.assertEqual(git(repository, "status", "--porcelain=v1"), status_before)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            projection = ProjectionEngine(
                ledger, ledger.path.parent / "projection-cache.json"
            ).rebuild(persist_cache=False)
            self.assertIsNone(projection["active_transaction"])


if __name__ == "__main__":
    unittest.main()
