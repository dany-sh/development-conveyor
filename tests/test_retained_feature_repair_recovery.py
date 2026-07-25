from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.contracts import WorkflowType, bind_human_gate, fingerprint
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.retained_feature_repair import (
    RetainedFeatureRepairRecovery,
    RetainedFeatureValidationRepair,
)
from development_conveyor.snapshots import capture_repository_snapshot
from tests.helpers import (
    controller_configuration,
    git,
    SyntheticLauncher,
    synthetic_repository,
    write_json,
)


class RetainedFeatureRepairRecoveryTests(unittest.TestCase):
    FEATURE = "F068"
    BRANCH = "codex/F068-job-application-data-model"
    HEAD_PARENT = "a" * 40
    ORIGINAL_TRANSACTION = "d13ab73a-d696-4689-9923-9172c89b3f0e"
    FAILED_RECOVERY_TRANSACTION = "33fbe92b-adab-4422-bcbc-23fcc7031e79"
    REPAIR_TRANSACTION = "4cd04e84-92a1-4ed8-9c37-37358c7a2db0"
    REPAIR_RUN = "feature-repair-7c906c0e-3ff5-4261-a264-2c88b148f47a"
    SESSIONS = (
        "019f979d-1309-7e61-8931-c29151f88361",
        "019f979e-66f7-7972-8671-d0817e4cc871",
    )
    PATHS = (
        "Sources/LiveInterviewCompanion/Models/DomainModels.swift",
        "Tests/LiveInterviewCompanionTests/DomainModelTests.swift",
        "Tests/LiveInterviewCompanionTests/PersistentDomainStoreTests.swift",
        "docs/CURRENT_STATUS.md",
        "docs/FEATURE_CATALOG.md",
        "docs/FEATURE_QUEUE.yaml",
        "docs/RUN_LOG.md",
        "docs/architecture.md",
        "docs/data-flow.md",
        "docs/decisions/0016-application-round-session-ownership-and-product-modes.md",
        "docs/decisions/README.md",
        "docs/features/F068-job-application-data-model.md",
    )

    @staticmethod
    def _passing_runner(argv: list[str], cwd: Path):
        return {
            "argv": argv,
            "exit_code": 0,
            "duration_seconds": 0.01,
            "output_sha256": fingerprint(argv),
            "output_summary": "passed",
        }

    def _terminal_output(
        self,
        *,
        project,
        identity: str,
        head: str,
        session_id: str,
        emitted_workflow: str = "feature_execution",
        attributed_paths: tuple[str, ...] = (
            "docs/CURRENT_STATUS.md",
            "docs/RUN_LOG.md",
        ),
    ) -> str:
        file_change = {
            "type": "item.completed",
            "item": {
                "id": "item_file",
                "type": "file_change",
                "status": "completed",
                "changes": [
                    {
                        "path": str(project.repository / relative),
                        "kind": "update",
                    }
                    for relative in attributed_paths
                ],
            },
        }
        envelope = {
            "schema_version": 1,
            "project_id": project.project_id,
            "repository_identity": identity,
            "run_id": self.REPAIR_RUN,
            "transaction_id": self.REPAIR_TRANSACTION,
            "workflow_type": emitted_workflow,
            "feature_id": self.FEATURE,
            "session_id": session_id,
            "classification": "FEATURE_ACCEPTED",
            "next_state": "feature_accepted",
            "starting_branch": self.BRANCH,
            "starting_commit": head,
            "current_commit": head,
            "changed_paths": list(self.PATHS),
            "evidence": {
                "implementation_complete": True,
                "focused_validation": [],
                "controller_acceptance_pending": True,
            },
        }
        terminal = {
            "type": "item.completed",
            "item": {
                "id": "item_terminal",
                "type": "agent_message",
                "text": (
                    "done\nCONVEYOR_TRANSACTION_RESULT="
                    + json.dumps(envelope, sort_keys=True)
                ),
            },
        }
        return json.dumps(file_change) + "\n" + json.dumps(terminal) + "\n"

    def _fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        for relative in self.PATHS:
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "docs/FEATURE_QUEUE.yaml":
                write_json(
                    path,
                    {
                        "schema_version": 1,
                        "milestones": [
                            {
                                "id": "M0",
                                "status": "active",
                                "features": [self.FEATURE],
                            }
                        ],
                        "features": [
                            {
                                "id": self.FEATURE,
                                "title": "Job Application Data Model",
                                "status": "ready",
                                "implementation_status": "Proposed",
                                "milestone": "M0",
                                "dependencies": [],
                                "spec": (
                                    "docs/features/"
                                    "F068-job-application-data-model.md"
                                ),
                                "branch": self.BRANCH,
                                "integration_base_commit": None,
                                "accepted_commit": None,
                                "integration_status": "pending",
                                "requires_human_decision": False,
                            }
                        ],
                    },
                )
            elif relative == "docs/CURRENT_STATUS.md":
                path.write_text(
                    "# Current Status\n\n## Factory position\n\n"
                    "- Active feature: F068 — Job Application Data Model "
                    "(implementation complete; controller acceptance pending)\n\n"
                    "## Health\n\n- Stable.\n",
                    encoding="utf-8",
                )
            elif relative == "docs/FEATURE_CATALOG.md":
                path.write_text(
                    "# Feature Catalog\n\n"
                    "| Feature ID | Name | Status | Milestone |\n"
                    "| --- | --- | --- | --- |\n"
                    "| F068 | Job Application Data Model | In Progress | M0 |\n",
                    encoding="utf-8",
                )
            elif relative.endswith("F068-job-application-data-model.md"):
                path.write_text(
                    "# F068\n\n"
                    "- Factory status: Implemented — controller acceptance pending\n\n"
                    "- [ ] Model is durable.\n",
                    encoding="utf-8",
                )
            else:
                path.write_text(f"baseline {relative}\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "prepare F068 baseline")
        head = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "-c", self.BRANCH, head)
        write_json(
            repository / ".factory/conveyor-state.json",
            {"schema_version": 1, "project_id": project.project_id},
        )
        for relative in self.PATHS:
            if relative == "docs/FEATURE_QUEUE.yaml":
                continue
            path = repository / relative
            path.write_text(
                path.read_text(encoding="utf-8") + "retained implementation\n",
                encoding="utf-8",
            )
        queue_path = repository / "docs/FEATURE_QUEUE.yaml"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0]["implementation_status"] = "Implemented"
        write_json(queue_path, queue)

        configuration = controller_configuration(root, project)
        write_json(
            configuration.root
            / "state/projects"
            / f"{project.project_id}.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "current_state": "review",
            },
        )
        identity = RepositoryInspector(repository).identity()
        state_root = configuration.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        original_snapshot = capture_repository_snapshot(project).to_dict()
        gate = bind_human_gate(
            {
                "classification": "structured_output_invalid",
                "reason": "repository session cannot continue safely",
                "feature": self.FEATURE,
                "run_id": "original-run",
            },
            transaction_id=self.ORIGINAL_TRANSACTION,
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            approved_next_state="queue_reconciliation",
            terminal_classification="HUMAN_DECISION_REQUIRED",
        )
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": "original-run",
                    "feature_id": self.FEATURE,
                    "starting_head": head,
                },
            ),
            ("SessionLaunched", {"session_id": "original-session"}),
            (
                "HumanGateRaised",
                {
                    "classification": "HUMAN_DECISION_REQUIRED",
                    "terminal_state": "human_decision_required",
                    "next_state": "human_decision_required",
                    "gate": gate,
                    "gate_id": gate["gate_id"],
                    "gate_fingerprint": fingerprint(gate),
                    "terminal_snapshot": original_snapshot,
                },
            ),
            ("LeaseReleased", {"lease_id": "original-lease"}),
            (
                "ProjectionUpdated",
                {
                    "current_state": "human_decision_required",
                    "current_feature": self.FEATURE,
                    "selected_feature": None,
                },
            ),
        ):
            ledger.append(
                event_type=event_type,
                transaction_id=self.ORIGINAL_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )
        ledger.append(
            event_type="TransactionStarted",
            transaction_id=self.FAILED_RECOVERY_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "run_id": "failed-recovery",
                "feature_id": self.FEATURE,
                "recovered_transaction_id": self.ORIGINAL_TRANSACTION,
            },
        )
        ledger.append(
            event_type="TransactionBlocked",
            transaction_id=self.FAILED_RECOVERY_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "classification": "FEATURE_VALIDATION_FAILED",
                "terminal_state": "terminal_failure",
                "terminal_snapshot": original_snapshot,
            },
        )

        allowed_policy = {
            "allowed_paths": list(self.PATHS),
            "allowed_prefixes": [],
            "denied_paths": [],
            "denied_prefixes": [],
            "allow_untracked": False,
            "require_clean_start": False,
            "commit_subject": "F068: Job Application Data Model",
        }
        initial_content = RepositoryInspector(repository).content_diff_fingerprint(
            self.PATHS
        )
        ledger.append(
            event_type="TransactionStarted",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "run_id": self.REPAIR_RUN,
                "milestone": "M0",
                "feature_id": self.FEATURE,
                "starting_branch": self.BRANCH,
                "starting_head": head,
                "starting_queue_fingerprint": original_snapshot[
                    "queue_fingerprint"
                ],
                "starting_tracked_diff_fingerprint": original_snapshot[
                    "tracked_diff_fingerprint"
                ],
                "starting_untracked_fingerprint": original_snapshot[
                    "untracked_fingerprint"
                ],
                "allowed_mutation_policy": allowed_policy,
                "original_transaction_id": self.ORIGINAL_TRANSACTION,
                "failed_recovery_transaction_id": (
                    self.FAILED_RECOVERY_TRANSACTION
                ),
                "pre_repair_fingerprint": initial_content,
                "recovery_mode": "retained_feature_validation_repair",
                "repair_profile": {
                    "model": "gpt-5.6-terra",
                    "reasoning": "high",
                    "child_sessions": 0,
                },
            },
        )
        ledger.append(
            event_type="LeaseAcquired",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "repair-lease", "lease_type": "feature_writer"},
        )
        ledger.append(
            event_type="SnapshotCaptured",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"snapshot": original_snapshot},
        )

        report_directory = configuration.root / "reports" / self.REPAIR_RUN
        before = initial_content
        attempt_values = []
        for attempt, session_id in enumerate(self.SESSIONS, start=1):
            ledger.append(
                event_type="SessionLaunched",
                transaction_id=self.REPAIR_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload={"session_id": session_id},
            )
            for relative in ("docs/CURRENT_STATUS.md", "docs/RUN_LOG.md"):
                path = repository / relative
                path.write_text(
                    path.read_text(encoding="utf-8")
                    + f"repair attempt {attempt}\n",
                    encoding="utf-8",
                )
            after = RepositoryInspector(repository).content_diff_fingerprint(
                self.PATHS
            )
            output = self._terminal_output(
                project=project,
                identity=identity["repository_id"],
                head=head,
                session_id=session_id,
            )
            report_path = (
                report_directory
                / f"feature-repair-attempt-{attempt}.json"
            )
            report = {
                "schema_version": 1,
                "run_id": self.REPAIR_RUN,
                "attempt": attempt,
                "feature_id": self.FEATURE,
                "session_id": session_id,
                "returncode": 0,
                "result_classification": "structured_output_invalid",
                "structured_output_validation": "invalid",
                "structured_output_errors": [
                    "terminal session-result envelope workflow_type conflicts "
                    "with invoked workflow"
                ],
                "pre_repair_fingerprint": before,
                "post_repair_fingerprint": after,
                "model": "gpt-5.6-terra",
                "reasoning": "high",
                "child_sessions": 0,
                "redacted_stdout": output,
                "redacted_stderr": "",
            }
            write_json(report_path, report)
            ledger.append(
                event_type="ValidationStarted",
                transaction_id=self.REPAIR_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload={
                    "repair_attempt": attempt,
                    "changed_paths": list(self.PATHS),
                    "executor": "retained_feature_repair",
                },
            )
            failure_payload = {
                "attempt": attempt,
                "session_id": session_id,
                "report_path": str(report_path),
                "pre_repair_fingerprint": before,
                "post_repair_fingerprint": after,
                "diagnostic": (
                    "terminal session-result envelope workflow_type conflicts "
                    "with invoked workflow"
                ),
                "failure_fingerprint": "b" * 64,
                "failure_signature": f"{attempt}" * 64,
                "environment_failure": False,
                "child_sessions": 0,
            }
            ledger.append(
                event_type="ValidationFailed",
                transaction_id=self.REPAIR_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=failure_payload,
            )
            attempt_values.append(failure_payload)
            before = after
        terminal_snapshot = capture_repository_snapshot(project).to_dict()
        ledger.append(
            event_type="TransactionBlocked",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "classification": "FEATURE_VALIDATION_FAILED",
                "terminal_state": "terminal_failure",
                "next_state": "review",
                "gate": None,
                "gate_id": None,
                "gate_fingerprint": None,
                "reference": None,
                "terminal_snapshot": terminal_snapshot,
            },
        )
        ledger.append(
            event_type="LeaseReleased",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "repair-lease"},
        )
        ledger.append(
            event_type="ProjectionUpdated",
            transaction_id=self.REPAIR_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "current_state": "review",
                "current_feature": self.FEATURE,
                "selected_feature": None,
            },
        )
        projection = ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        )
        projection.rebuild(persist_cache=True)
        write_json(
            report_directory / "retained-feature-repair.json",
            {
                "schema_version": 1,
                "feature_id": self.FEATURE,
                "outcome": "repair_exhausted",
                "recoverable_technical_failure": True,
                "human_gate_created": False,
                "application_commit_created": False,
                "attempts": attempt_values,
            },
        )
        recovery = RetainedFeatureRepairRecovery(
            controller_root=configuration.root,
            configuration=configuration.conveyor,
            project=project,
            command_runner=self._passing_runner,
        )
        return configuration, project, head, recovery

    def test_exact_two_attempt_chain_and_route_alias_are_authenticated(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _, recovery = self._fixture(Path(temporary))
            before = git(project.repository, "status", "--porcelain=v1")
            plan = recovery.inspect(
                feature_id=self.FEATURE,
                repair_transaction_id=self.REPAIR_TRANSACTION,
            )
            self.assertEqual(
                before, git(project.repository, "status", "--porcelain=v1")
            )
            self.assertEqual(2, len(plan["attempt_chain"]))
            self.assertTrue(
                all(
                    item["continuous_from_previous"]
                    for item in plan["attempt_chain"]
                )
            )
            self.assertEqual(
                {"docs/CURRENT_STATUS.md", "docs/RUN_LOG.md"},
                set(
                    plan["attempt_chain"][0][
                        "changed_paths_attributable_to_session"
                    ]
                ),
            )
            self.assertEqual(
                "feature_cycle",
                plan["workflow_normalization"]["invoked_workflow_type"],
            )
            self.assertEqual(
                "feature_execution",
                plan["workflow_normalization"]["emitted_workflow_type"],
            )
            self.assertEqual(0, plan["model_sessions_that_would_launch"])
            self.assertEqual(0, plan["child_sessions_that_would_launch"])

    def test_broken_chain_and_unauthorized_path_fail_closed(self):
        for mutation in ("chain", "path"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                configuration, project, _, recovery = self._fixture(
                    Path(temporary)
                )
                report_path = (
                    configuration.root
                    / "reports"
                    / self.REPAIR_RUN
                    / "feature-repair-attempt-2.json"
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if mutation == "chain":
                    report["pre_repair_fingerprint"] = "0" * 64
                else:
                    report["redacted_stdout"] = self._terminal_output(
                        project=project,
                        identity=RepositoryInspector(project.repository).identity()[
                            "repository_id"
                        ],
                        head=RepositoryInspector(project.repository).head,
                        session_id=self.SESSIONS[1],
                        attributed_paths=("unauthorized.txt",),
                    )
                write_json(report_path, report)
                with self.assertRaises(Exception):
                    recovery.inspect(
                        feature_id=self.FEATURE,
                        repair_transaction_id=self.REPAIR_TRANSACTION,
                    )

    def test_unrelated_workflow_alias_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, recovery = self._fixture(
                Path(temporary)
            )
            report_path = (
                configuration.root
                / "reports"
                / self.REPAIR_RUN
                / "feature-repair-attempt-1.json"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["redacted_stdout"] = self._terminal_output(
                project=project,
                identity=RepositoryInspector(project.repository).identity()[
                    "repository_id"
                ],
                head=head,
                session_id=self.SESSIONS[0],
                emitted_workflow="queue_reconciliation",
            )
            write_json(report_path, report)
            with self.assertRaisesRegex(Exception, "workflow_type conflicts"):
                recovery.inspect(
                    feature_id=self.FEATURE,
                    repair_transaction_id=self.REPAIR_TRANSACTION,
                )

    def test_semantic_failure_signature_excludes_volatile_identity(self):
        arguments = {
            "route": "retained_feature_validation_repair",
            "feature_id": self.FEATURE,
            "invoked_workflow": "feature_cycle",
            "emitted_workflow": "feature_execution",
            "diagnostic": (
                "terminal session-result envelope workflow_type conflicts "
                "with invoked workflow"
            ),
            "envelope_classification": "FEATURE_ACCEPTED",
            "authorized_paths": list(self.PATHS),
            "environment_failure": False,
        }
        first = RetainedFeatureValidationRepair._semantic_failure_signature(
            **arguments
        )
        second = RetainedFeatureValidationRepair._semantic_failure_signature(
            **arguments
        )
        self.assertEqual(first, second)

    def test_cycle_engine_discovers_zero_model_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, _, _ = self._fixture(Path(temporary))
            (
                configuration.root
                / "state/projects"
                / f"{project.project_id}.json"
            ).unlink()
            routed = CycleEngine(
                configuration, SyntheticLauncher()
            ).project_plan(project)
            self.assertEqual(
                "retained_feature_repair_recovery",
                routed["proposed_next_action"],
            )
            self.assertTrue(routed["recognized_technical_recovery"])
            self.assertTrue(routed["deterministic_only"])
            self.assertEqual([], routed["model_sessions_that_would_launch"])
            self.assertEqual(
                self.REPAIR_TRANSACTION,
                routed["retained_feature_repair_recovery"][
                    "repair_transaction_id"
                ],
            )

    def test_zero_model_recovery_validates_then_commits_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, head, recovery = self._fixture(Path(temporary))
            result = recovery.apply(
                recovery.inspect(
                    feature_id=self.FEATURE,
                    repair_transaction_id=self.REPAIR_TRANSACTION,
                )
            )
            inspector = RepositoryInspector(project.repository)
            self.assertEqual("integration_pending", result["outcome"])
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertEqual(head, inspector.rev_parse(f"{inspector.head}^"))
            self.assertTrue(inspector.is_clean)
            self.assertEqual(
                result["accepted_feature_commit"], inspector.head
            )
            self.assertEqual(
                [
                    ["swift", "test", "--filter", "DomainModelTests"],
                    [
                        "swift",
                        "test",
                        "--filter",
                        "PersistentDomainStoreTests",
                    ],
                    ["swift", "build"],
                    ["git", "diff", "--check"],
                ],
                [item["argv"] for item in result["commands"]],
            )
            self.assertEqual(
                "integration_pending",
                result["projection"]["current_state"],
            )
            self.assertIsNone(result["projection"]["human_gate"])
            self.assertFalse(result["milestone_integration_performed"])
            self.assertFalse(result["queue_reconciliation_performed"])

    def test_validation_failure_creates_no_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _ = self._fixture(Path(temporary))

            def failing_runner(argv: list[str], cwd: Path):
                return {
                    "argv": argv,
                    "exit_code": 1 if argv == ["swift", "build"] else 0,
                    "duration_seconds": 0.01,
                    "output_sha256": fingerprint(argv),
                    "output_summary": "failed" if argv == ["swift", "build"] else "passed",
                }

            recovery = RetainedFeatureRepairRecovery(
                controller_root=configuration.root,
                configuration=configuration.conveyor,
                project=project,
                command_runner=failing_runner,
            )
            result = recovery.apply(
                recovery.inspect(
                    feature_id=self.FEATURE,
                    repair_transaction_id=self.REPAIR_TRANSACTION,
                )
            )
            self.assertEqual("validation_failed", result["outcome"])
            self.assertEqual(head, RepositoryInspector(project.repository).head)
            self.assertFalse(result["application_commit_created"])


if __name__ == "__main__":
    unittest.main()
