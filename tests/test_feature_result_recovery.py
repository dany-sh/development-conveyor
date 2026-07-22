from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.contracts import WorkflowType, bind_human_gate, fingerprint
from development_conveyor.feature_result_recovery import FeatureResultRecovery
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.repository import RepositoryInspector
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


class FeatureResultRecoveryTests(unittest.TestCase):
    ORIGINAL_TRANSACTION = "8e45c566-49eb-452e-b5cc-c10f40e762fa"
    ORIGINAL_RUN = "2d129697-ee84-4815-936c-2dc691bc7f08"
    ORIGINAL_SESSION = "019f8add-bd2b-7662-aca1-a15fe0da1cf1"
    BRANCH = "codex/F003-single-window-application-shell"

    def _fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "id": "F003",
            "title": "Single-Window Application Shell",
            "status": "review",
            "implementation_status": "Completed",
            "spec": "docs/features/F003-single-window-application-shell.md",
            "branch": self.BRANCH,
            "accepted_commit": None,
            "integration_status": "pending",
        })
        write_json(queue_path, queue)
        (repository / "Sources/LiveInterviewCompanion/Models").mkdir(parents=True)
        (repository / "Tests/LiveInterviewCompanionTests").mkdir(parents=True)
        (repository / "docs/decisions").mkdir(parents=True)
        (repository / "CURRENT_STATUS.md").write_text(
            "- Status: Implementation and deterministic validation complete on `codex/F003-single-window-application-shell`; adversarial review pending\n"
            "- Build: debug compilation passes; release and staged-app verification are required before F003 leaves review\n"
            "- Git: F003 remains uncommitted and unintegrated pending adversarial review and acceptance\n"
            "Run the named adversarial review for F003. Resolve every Critical, High, and Medium finding before acceptance.\n",
            encoding="utf-8",
        )
        (repository / "docs/CURRENT_STATUS.md").write_text(
            "- Active feature: F003 — Single-Window Application Shell (`review`)\n"
            "Its implementation and deterministic tests are complete but uncommitted in review; exact environment gates and adversarial review remain pending.\n"
            "F003 implementation and deterministic validation are complete on its isolated branch and remain uncommitted pending adversarial review. Resolve every Critical, High, and Medium finding before acceptance.\n",
            encoding="utf-8",
        )
        (repository / "docs/FEATURE_CATALOG.md").write_text(
            "| F003 | Single-Window Application Shell | Review | Partial | M0 | F001 | spec |\n",
            encoding="utf-8",
        )
        (repository / "docs/RUN_LOG.md").write_text("# Run Log\n", encoding="utf-8")
        (repository / "docs/architecture.md").write_text("WorkspaceRouter owns routing.\n", encoding="utf-8")
        (repository / "docs/data-flow.md").write_text("WorkspaceRouter routes modes.\n", encoding="utf-8")
        (repository / "docs/decisions/README.md").write_text(
            "[0015](0015-single-window-workspace-and-session-routing.md)\n",
            encoding="utf-8",
        )
        spec = repository / "docs/features/F003-single-window-application-shell.md"
        spec.write_text(
            "# F003\n\n"
            "- Factory status: Review — implementation complete; exact environment gates and adversarial review pending\n\n"
            "- [ ] One window.\n",
            encoding="utf-8",
        )
        (repository / "docs/decisions/0015-single-window-workspace-and-session-routing.md").write_text(
            "# Single window\n\nWorkspaceRouter owns the single window route.\n",
            encoding="utf-8",
        )
        router = repository / "Sources/LiveInterviewCompanion/Models/WorkspaceRouter.swift"
        router.write_text(
            "enum WorkspaceSection {\n"
            " case dashboard\n case applications\n case practice\n case liveInterview\n"
            " case sessions\n case knowledge\n case settings\n}\n",
            encoding="utf-8",
        )
        (repository / "Tests/LiveInterviewCompanionTests/WorkspaceRoutingTests.swift").write_text(
            "// F003 routing tests\n", encoding="utf-8"
        )
        (repository / "Tests/LiveInterviewCompanionTests/AppStoreWorkspaceRoutingTests.swift").write_text(
            "// F003 AppStore routing tests\n", encoding="utf-8"
        )
        git(repository, "add", ".")
        git(repository, "commit", "-m", "prepare synthetic F003 baseline")
        head = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "-c", self.BRANCH)
        exclude = repository / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\n/.factory/conveyor-state.json\n",
            encoding="utf-8",
        )
        write_json(
            repository / ".factory/conveyor-state.json",
            {"schema_version": 1, "project_id": project.project_id},
        )

        # Preserve a dirty implementation result whose queue remains in review.
        router.write_text(router.read_text() + "struct WorkspaceRouter {}\n", encoding="utf-8")
        (repository / "Tests/LiveInterviewCompanionTests/WorkspaceRoutingTests.swift").write_text(
            "// F003 implemented routing tests\n", encoding="utf-8"
        )
        (repository / "Tests/LiveInterviewCompanionTests/AppStoreWorkspaceRoutingTests.swift").write_text(
            "// F003 implemented AppStore tests\n", encoding="utf-8"
        )
        (repository / "docs/decisions/0015-single-window-workspace-and-session-routing.md").write_text(
            "# Single window implemented\n\nWorkspaceRouter owns the single window route.\n",
            encoding="utf-8",
        )
        spec.write_text(spec.read_text() + "\nImplementation complete.\n", encoding="utf-8")
        for relative in (
            "CURRENT_STATUS.md", "docs/CURRENT_STATUS.md", "docs/FEATURE_CATALOG.md",
            "docs/RUN_LOG.md",
        ):
            path = repository / relative
            path.write_text(path.read_text(encoding="utf-8") + "\nF003 review evidence.\n", encoding="utf-8")
        queue["features"][0]["updated_at"] = "2026-07-22T17:55:32+00:00"
        write_json(queue_path, queue)
        changed = tuple(sorted({
            *RepositoryInspector(repository).tracked_changed_paths(),
            *RepositoryInspector(repository).untracked_file_hashes(),
        }))

        configuration = controller_configuration(root, project)
        controller = configuration.root
        identity = RepositoryInspector(repository).identity()
        state_root = controller / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        policy = {
            "allowed_paths": list(changed), "allowed_prefixes": [],
            "denied_paths": [], "denied_prefixes": [], "allow_untracked": True,
            "require_clean_start": True,
            "commit_subject": "F003: Single-Window Application Shell",
        }
        ledger.append(
            event_type="TransactionStarted", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "run_id": self.ORIGINAL_RUN, "milestone": "M0", "feature_id": "F003",
                "starting_branch": self.BRANCH, "starting_head": head,
                "allowed_mutation_policy": policy,
            },
        )
        ledger.append(
            event_type="LeaseAcquired", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "old-lease", "lease_type": "feature_writer"},
        )
        ledger.append(
            event_type="SnapshotCaptured", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"snapshot": {"branch": self.BRANCH, "head": head}},
        )
        ledger.append(
            event_type="SessionLaunched", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"session_id": self.ORIGINAL_SESSION},
        )
        gate = bind_human_gate(
            {"classification": "structured_output_invalid", "reason": "invalid legacy result"},
            transaction_id=self.ORIGINAL_TRANSACTION,
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            approved_next_state="queue_reconciliation",
            terminal_classification="HUMAN_DECISION_REQUIRED",
        )
        ledger.append(
            event_type="HumanGateRaised", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "classification": "HUMAN_DECISION_REQUIRED", "terminal_state": "human_decision_required",
                "next_state": "human_decision_required", "gate": gate,
                "gate_id": gate["gate_id"], "gate_fingerprint": fingerprint(gate),
                "terminal_snapshot": {"branch": self.BRANCH, "head": head},
            },
        )
        ledger.append(
            event_type="LeaseReleased", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "old-lease"},
        )
        ledger.append(
            event_type="ProjectionUpdated", transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"current_state": "human_decision_required", "current_feature": "F003"},
        )
        legacy = {
            "schema_version": 1, "transaction_id": self.ORIGINAL_TRANSACTION,
            "repository_id": identity["repository_id"], "agent_run_id": self.ORIGINAL_RUN,
            "session_id": None, "starting_branch": self.BRANCH, "starting_commit": head,
            "current_commit": head, "selected_feature": "F003",
            "changed_paths": list(changed), "classification": "FEATURE_BLOCKED",
            "next_state": "review",
        }
        output = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done\nCONVEYOR_TRANSACTION_RESULT=" + json.dumps(legacy)},
        })
        report = {
            "schema_version": 1, "project_id": project.project_id,
            "run_id": self.ORIGINAL_RUN, "session_id": self.ORIGINAL_SESSION,
            "redacted_stdout": output,
        }
        write_json(controller / "reports" / self.ORIGINAL_RUN / "feature_cycle.json", report)
        return configuration, project, head, changed

    @staticmethod
    def _passing_runner(argv: list[str], cwd: Path):
        return {
            "argv": argv, "exit_code": 0, "duration_seconds": 0.01,
            "output_sha256": "0" * 64, "output_summary": "passed",
            "content_audit_hard_gate": False,
        }

    def test_inspect_proves_exact_terminal_diff_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed = self._fixture(Path(temporary))
            recovery = FeatureResultRecovery(
                controller_root=configuration.root,
                configuration=configuration.conveyor,
                project=project,
                command_runner=self._passing_runner,
            )
            plan = recovery.inspect(
                feature_id="F003", original_transaction_id=self.ORIGINAL_TRANSACTION,
                original_run_id=self.ORIGINAL_RUN, original_session_id=self.ORIGINAL_SESSION,
                expected_branch=self.BRANCH, expected_head=head,
            )
            self.assertEqual(plan["changed_paths"], list(changed))
            self.assertTrue(all(plan["checks"].values()))
            self.assertFalse(plan["model_session_launched"])
            self.assertEqual(RepositoryInspector(project.repository).head, head)

    def test_apply_creates_one_accepted_commit_and_preserves_original_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed = self._fixture(Path(temporary))
            recovery = FeatureResultRecovery(
                controller_root=configuration.root,
                configuration=configuration.conveyor,
                project=project,
                command_runner=self._passing_runner,
            )
            plan = recovery.inspect(
                feature_id="F003", original_transaction_id=self.ORIGINAL_TRANSACTION,
                original_run_id=self.ORIGINAL_RUN, original_session_id=self.ORIGINAL_SESSION,
                expected_branch=self.BRANCH, expected_head=head,
            )
            result = recovery.apply(plan)
            inspector = RepositoryInspector(project.repository)
            self.assertEqual(result["outcome"], "integration_pending")
            self.assertFalse(result["model_session_launched"])
            self.assertFalse(result["milestone_integration_performed"])
            self.assertEqual(inspector.rev_parse(f"{inspector.head}^"), head)
            self.assertEqual(tuple(inspector.changed_paths(inspector.head)), changed)
            self.assertTrue(inspector.is_clean)
            cycle = json.loads(inspector.cycle_state_path().read_text())
            self.assertEqual(cycle["current_phase"], "integration_pending")
            self.assertEqual(cycle["accepted_feature_commit"], inspector.head)
            self.assertIsNone(cycle["session_id"])
            queue = json.loads((project.repository / project.queue_location).read_text())
            feature = next(item for item in queue["features"] if item["id"] == "F003")
            self.assertEqual(feature["status"], "integration_pending")
            self.assertEqual(feature["accepted_commit"], "SELF")
            events = recovery.ledger.read()
            old = [event["fingerprint"] for event in events if event["transaction_id"] == self.ORIGINAL_TRANSACTION]
            self.assertEqual(old, plan["original_event_fingerprints"])
            new = [event for event in events if event["transaction_id"] == result["recovery_transaction_id"]]
            self.assertFalse(any(event["event_type"] == "SessionLaunched" for event in new))
            self.assertTrue(any(event["event_type"] == "RecoveryApplied" for event in new))


if __name__ == "__main__":
    unittest.main()
