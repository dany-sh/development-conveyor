from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import WorkflowType, bind_human_gate, fingerprint
from development_conveyor.cycle_cache_repair import (
    CacheRepairExpectation,
    CycleCacheRepair,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError
from development_conveyor.feature_result_recovery import (
    AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
    AUTHENTICATED_PREPARED_FEATURE_BRANCH_CHECKPOINT,
    CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
    FeatureResultRecovery,
    LEGACY_RETAINED_RESULT_TOPOLOGY,
    PREPARED_CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
    _authenticate_original_transaction_topology,
    _changed_paths,
)
from development_conveyor.retained_feature_repair import (
    RetainedFeatureValidationRepair,
)
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine, projection_fingerprint
from development_conveyor.repository import RepositoryInspector
from development_conveyor.snapshots import capture_repository_snapshot
from development_conveyor.sessions import SessionPlan, SessionResult
from development_conveyor.validation import validate_schema
from tests.helpers import (
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


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


class GeneralFeatureResultRecoveryTests(unittest.TestCase):
    FEATURE = "F097"
    BRANCH = "codex/F097-imported-audio-transcription-workflow"
    ORIGINAL_TRANSACTION = "b8b6c22f-7c1a-4124-9dee-135647905331"
    ORIGINAL_RUN = "8b4d5f27-7466-4430-9d12-51256a6e9f88"
    ORIGINAL_SESSION = "019f9628-e836-79f3-bb95-20ca8fa610ec"
    PREPARATION_TRANSACTION = "ec4e9bc3-d8f1-4462-860c-bdb1b455cc04"
    PRELAUNCH_TRANSACTION = "92ac7b0f-964f-47a6-83be-85b7f9c0f5fe"
    FAILED_PRELAUNCH_TRANSACTION = "7880273c-d584-45c2-89e9-291622568c2e"

    def _fixture(
        self,
        root: Path,
        *,
        prepared: bool = False,
        preparation_projection_feature: str | None = None,
        preparation_run_id: str | None = None,
        prelaunch_recovery: bool = False,
        prelaunch_failure_reference: str = "UnicodeDecodeError",
        malformed_factory_position: bool = False,
        execution_checkpoint_payload: dict | None = None,
        execution_checkpoint_payloads: tuple[dict, ...] | None = None,
    ):
        repository, project = synthetic_repository(root)
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        feature.update(
            {
                "id": self.FEATURE,
                "title": "Imported Audio Transcription Workflow",
                "status": (
                    "ready"
                    if prelaunch_recovery
                    else ("in_progress" if prepared else "ready")
                ),
                "implementation_status": "Proposed",
                "spec": "docs/features/F097-imported-audio-transcription-workflow.md",
                "branch": self.BRANCH if prepared else None,
                "integration_base_commit": None,
                "accepted_commit": None,
                "requires_human_decision": False,
                "decision_resolution": {
                    "recorded_question": "Which durable imported-audio identity is approved?",
                    "approved_resolution": "Use the additive canonical imported-audio contract.",
                    "selected_feature": True,
                    "decision_file_sha256": "a" * 64,
                },
            }
        )
        write_json(queue_path, queue)
        old_spec = repository / "docs/features/F001.md"
        old_spec.unlink()
        spec = repository / "docs/features/F097-imported-audio-transcription-workflow.md"
        spec.write_text(
            "# F097\n\n- Factory status: Ready\n\n- [ ] Import audio.\n",
            encoding="utf-8",
        )
        (repository / "docs/CURRENT_STATUS.md").write_text(
            "# Current Status\n\n## Factory position\n\n"
            "- Selected feature: F010 — Prior selection (`ready`)\n\n"
            "## Verified health\n\n- Synthetic fixture.\n",
            encoding="utf-8",
        )
        (repository / "docs/FEATURE_CATALOG.md").write_text(
            "# Feature Catalog\n\n"
            "| Feature ID | Name | Status | Milestone |\n"
            "| --- | --- | --- | --- |\n"
            "| F097 | Imported Audio Transcription Workflow | Ready | M0 |\n",
            encoding="utf-8",
        )
        source_root = repository / "Sources/LiveInterviewCompanion/Services"
        test_root = repository / "Tests/LiveInterviewCompanionTests"
        source_root.mkdir(parents=True)
        test_root.mkdir(parents=True)
        persistent = source_root / "PersistentDomainStore.swift"
        persistent.write_text("struct PersistentDomainStore {}\n", encoding="utf-8")
        (test_root / "PersistentDomainStoreTests.swift").write_text(
            "// persistent tests\n", encoding="utf-8"
        )
        if prelaunch_recovery:
            (
                source_root / "ImportedAudioTranscriptionService.swift"
            ).write_text(
                "struct ImportedAudioTranscriptionService { let ready = true }\n",
                encoding="utf-8",
            )
            (
                test_root / "ImportedAudioTranscriptionServiceTests.swift"
            ).write_text(
                "// baseline imported audio tests\n", encoding="utf-8"
            )
        git(repository, "add", ".")
        git(repository, "commit", "-m", "prepare F097 synthetic baseline")
        head = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "-c", self.BRANCH, head)
        prepared_queue_fingerprint = capture_repository_snapshot(
            project
        ).queue_fingerprint
        write_json(
            repository / ".factory/conveyor-state.json",
            {"schema_version": 1, "project_id": project.project_id},
        )
        if prelaunch_recovery:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0]["status"] = "in_progress"
            write_json(queue_path, queue)

        persistent.write_text(
            "struct PersistentDomainStore { let imported = true }\n",
            encoding="utf-8",
        )
        spec.write_text(
            spec.read_text(encoding="utf-8") + "\nImplementation complete.\n",
            encoding="utf-8",
        )
        current = repository / "docs/CURRENT_STATUS.md"
        current.write_text(
            current.read_text(encoding="utf-8")
            + "\nF097 implementation awaits controller acceptance.\n",
            encoding="utf-8",
        )
        if malformed_factory_position:
            current.write_text(
                current.read_text(encoding="utf-8").replace(
                    "- Selected feature: F010 — Prior selection (`ready`)",
                    "- Current feature: F097 — malformed retained evidence",
                ),
                encoding="utf-8",
            )
        imported = source_root / "ImportedAudioTranscriptionService.swift"
        imported.write_text(
            "struct ImportedAudioTranscriptionService {}\n", encoding="utf-8"
        )
        imported_tests = (
            test_root / "ImportedAudioTranscriptionServiceTests.swift"
        )
        imported_tests.write_text("// imported audio tests\n", encoding="utf-8")

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
        inspector = RepositoryInspector(repository)
        changed = tuple(
            sorted(
                {
                    *inspector.tracked_changed_paths(),
                    *inspector.untracked_file_hashes(),
                }
            )
        )
        policy = {
            "allowed_paths": list(changed),
            "allowed_prefixes": [],
            "denied_paths": [],
            "denied_prefixes": [],
            "allow_untracked": True,
            "require_clean_start": True,
            "commit_subject": "F097: Imported Audio Transcription Workflow",
        }
        if prepared:
            preparation_session = (
                "deterministic-feature-preparation:"
                + self.PREPARATION_TRANSACTION
            )
            ledger.append(
                event_type="TransactionStarted",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "run_id": preparation_run_id or self.ORIGINAL_RUN,
                    "milestone": "M0",
                    "feature_id": self.FEATURE,
                    "starting_branch": str(project.milestone_branch),
                    "starting_head": head,
                    "starting_queue_fingerprint": prepared_queue_fingerprint,
                    "allowed_mutation_policy": {},
                },
            )
            ledger.append(
                event_type="LeaseAcquired",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "lease_id": "preparation-lease",
                    "lease_type": "feature_writer",
                },
            )
            ledger.append(
                event_type="SnapshotCaptured",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "snapshot": {
                        "repository_identity": identity["repository_id"],
                        "repository_path_fingerprint": identity[
                            "path_fingerprint"
                        ],
                        "branch": str(project.milestone_branch),
                        "head": head,
                        "tracked_changed_paths": [],
                        "untracked_paths": [],
                    }
                },
            )
            ledger.append(
                event_type="SessionLaunched",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={"session_id": preparation_session},
            )
            ledger.append(
                event_type="SessionResultAccepted",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "session_id": preparation_session,
                    "classification": "FEATURE_PREPARED",
                    "next_state": "feature_preparing",
                },
            )
            ledger.append(
                event_type="ValidationStarted",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={},
            )
            ledger.append(
                event_type="ValidationPassed",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={},
            )
            ledger.append(
                event_type="TransactionCompleted",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "classification": "FEATURE_PREPARED",
                    "feature_id": self.FEATURE,
                    "next_state": "feature_preparing",
                    "terminal_snapshot": {
                        "branch": self.BRANCH,
                        "head": head,
                        "queue_fingerprint": prepared_queue_fingerprint,
                    },
                },
            )
            ledger.append(
                event_type="LeaseReleased",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={"lease_id": "preparation-lease"},
            )
            ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=self.PREPARATION_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                payload={
                    "current_state": "feature_preparing",
                    "current_feature": (
                        preparation_projection_feature or self.FEATURE
                    ),
                    "selected_feature": None,
                },
            )
        if prelaunch_recovery:
            clean_snapshot = {
                "repository_identity": identity["repository_id"],
                "repository_path_fingerprint": identity["path_fingerprint"],
                "branch": self.BRANCH,
                "head": head,
                "queue_fingerprint": prepared_queue_fingerprint,
                "tracked_diff_fingerprint": (
                    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                ),
                "untracked_fingerprint": fingerprint({}),
                "tracked_changed_paths": [],
                "untracked_paths": [],
                "git_operations": {
                    "cherry_pick": False,
                    "merge": False,
                    "rebase_apply": False,
                    "rebase_merge": False,
                },
                "clean": True,
            }
            for event_type, payload in (
                (
                    "TransactionStarted",
                    {
                        "run_id": "failed-prelaunch-run",
                        "milestone": "M0",
                        "feature_id": self.FEATURE,
                        "starting_branch": self.BRANCH,
                        "starting_head": head,
                        "starting_queue_fingerprint": prepared_queue_fingerprint,
                        "starting_tracked_diff_fingerprint": hashlib.sha256(
                            b""
                        ).hexdigest(),
                        "starting_untracked_fingerprint": fingerprint({}),
                    },
                ),
                ("LeaseAcquired", {"lease_id": "failed-prelaunch"}),
                ("SnapshotCaptured", {"snapshot": clean_snapshot}),
                (
                    "TransactionBlocked",
                    {
                        "classification": "FEATURE_VALIDATION_FAILED",
                        "terminal_state": "terminal_failure",
                        "next_state": "validation_failed",
                        "reference": prelaunch_failure_reference,
                        "terminal_snapshot": clean_snapshot,
                    },
                ),
                ("LeaseReleased", {"lease_id": "failed-prelaunch"}),
                (
                    "ProjectionUpdated",
                    {
                        "current_state": "validation_failed",
                        "current_feature": self.FEATURE,
                        "selected_feature": None,
                    },
                ),
            ):
                ledger.append(
                    event_type=event_type,
                    transaction_id=self.FAILED_PRELAUNCH_TRANSACTION,
                    workflow_type=WorkflowType.FEATURE_EXECUTION,
                    payload=payload,
                )
            if prelaunch_failure_reference == "SessionError":
                write_json(
                    configuration.root
                    / "reports/failed-prelaunch-run/"
                    "feature_cycle-launch-failure.json",
                    {
                        "schema_version": 1,
                        "project_id": project.project_id,
                        "run_id": "failed-prelaunch-run",
                        "action": "feature_cycle",
                        "failure_classification": "session_execution_failed",
                        "exit_classification": "session_execution_failed",
                        "result_classification": "session_execution_failed",
                        "exit_status": None,
                        "argv": [],
                        "session_id": None,
                        "launched_model": None,
                        "terminal_marker_found": False,
                        "context_pack_evidence": None,
                        "context_read_failure": None,
                        "structured_result": None,
                        "parsed_structured_result": None,
                        "redacted_stderr": (
                            "capability_isolation_unsupported: "
                            "missing requested=example"
                        ),
                        "working_directory": str(repository),
                    },
                )
            recovery_plan_fingerprint = "9" * 64
            for event_type, payload in (
                (
                    "TransactionStarted",
                    {
                        "run_id": "prelaunch-recovery-run",
                        "milestone": "M0",
                        "feature_id": self.FEATURE,
                        "starting_branch": self.BRANCH,
                        "starting_head": head,
                        "starting_queue_fingerprint": prepared_queue_fingerprint,
                        "starting_tracked_diff_fingerprint": hashlib.sha256(
                            b""
                        ).hexdigest(),
                        "starting_untracked_fingerprint": fingerprint({}),
                    },
                ),
                ("LeaseAcquired", {"lease_id": "prelaunch-recovery"}),
                ("SnapshotCaptured", {"snapshot": clean_snapshot}),
                (
                    "CheckpointRecorded",
                    {
                        "checkpoint": "feature_prelaunch_recovery_authenticated",
                        "failed_transaction_id": self.FAILED_PRELAUNCH_TRANSACTION,
                        "model_sessions_launched": 0,
                        "child_sessions_launched": 0,
                        "implementation_attempts_consumed": 0,
                    },
                ),
                (
                    "DeterministicExecutionStarted",
                    {
                        "execution_mode": "feature_prelaunch_recovery",
                        "plan_fingerprint": recovery_plan_fingerprint,
                        "model_session_launched": False,
                        "child_sessions_launched": 0,
                    },
                ),
                (
                    "DeterministicResultAccepted",
                    {
                        "classification": "RECOVERY_APPLIED",
                        "plan_fingerprint": recovery_plan_fingerprint,
                    },
                ),
                (
                    "ChangesDetected",
                    {
                        "changed_paths": [],
                        "diff_fingerprint": hashlib.sha256(b"").hexdigest(),
                    },
                ),
                ("ValidationStarted", {"changed_paths": []}),
                (
                    "ValidationPassed",
                    {
                        "checks": {
                            "application_unchanged": True,
                            "model_sessions_launched": 0,
                            "child_sessions_launched": 0,
                        }
                    },
                ),
                (
                    "CommitFinalized",
                    {"commit": head, "no_change": True, "changed_paths": []},
                ),
                (
                    "RecoveryApplied",
                    {
                        "classification": "FEATURE_PRELAUNCH_RECOVERY",
                        "selected_feature": self.FEATURE,
                    },
                ),
                (
                    "TransactionCompleted",
                    {
                        "classification": "RECOVERY_APPLIED",
                        "feature_id": self.FEATURE,
                        "next_state": "feature_preparing",
                        "feature_commit_created": False,
                        "model_session_launched": False,
                        "child_sessions_launched": 0,
                        "terminal_snapshot": clean_snapshot,
                    },
                ),
                ("LeaseReleased", {"lease_id": "prelaunch-recovery"}),
                (
                    "ProjectionUpdated",
                    {
                        "current_state": "feature_preparing",
                        "current_feature": self.FEATURE,
                        "selected_feature": self.FEATURE,
                    },
                ),
            ):
                ledger.append(
                    event_type=event_type,
                    transaction_id=self.PRELAUNCH_TRANSACTION,
                    workflow_type=WorkflowType.RECOVERY,
                    payload=payload,
                )
        ledger.append(
            event_type="TransactionStarted",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "run_id": self.ORIGINAL_RUN,
                "milestone": "M0",
                "feature_id": self.FEATURE,
                "starting_branch": self.BRANCH,
                "starting_head": head,
                "starting_queue_fingerprint": prepared_queue_fingerprint,
                "starting_tracked_diff_fingerprint": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "starting_untracked_fingerprint": fingerprint({}),
                "allowed_mutation_policy": policy,
            },
        )
        ledger.append(
            event_type="LeaseAcquired",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "old-lease", "lease_type": "feature_writer"},
        )
        ledger.append(
            event_type="SnapshotCaptured",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "snapshot": {
                    "repository_identity": identity["repository_id"],
                    "repository_path_fingerprint": identity[
                        "path_fingerprint"
                    ],
                    "branch": self.BRANCH,
                    "head": head,
                    "queue_fingerprint": prepared_queue_fingerprint,
                    "tracked_diff_fingerprint": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "untracked_fingerprint": fingerprint({}),
                    "tracked_changed_paths": [],
                    "untracked_paths": [],
                    "git_operations": {
                        "cherry_pick": False,
                        "merge": False,
                        "rebase_apply": False,
                        "rebase_merge": False,
                    },
                }
            },
        )
        ledger.append(
            event_type="SessionLaunched",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"session_id": self.ORIGINAL_SESSION},
        )
        execution_checkpoints = (
            execution_checkpoint_payloads
            if execution_checkpoint_payloads is not None
            else (
                (execution_checkpoint_payload,)
                if execution_checkpoint_payload is not None
                else ()
            )
        )
        for checkpoint_payload in execution_checkpoints:
            ledger.append(
                event_type="CheckpointRecorded",
                transaction_id=self.ORIGINAL_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=checkpoint_payload,
            )
        gate = bind_human_gate(
            {
                "classification": "structured_output_invalid",
                "reason": "repository session cannot continue safely",
                "feature": self.FEATURE,
                "run_id": self.ORIGINAL_RUN,
            },
            transaction_id=self.ORIGINAL_TRANSACTION,
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            approved_next_state="queue_reconciliation",
            terminal_classification="HUMAN_DECISION_REQUIRED",
        )
        terminal_snapshot = capture_repository_snapshot(project).to_dict()
        ledger.append(
            event_type="HumanGateRaised",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "classification": "HUMAN_DECISION_REQUIRED",
                "terminal_state": "human_decision_required",
                "next_state": "human_decision_required",
                "gate": gate,
                "gate_id": gate["gate_id"],
                "gate_fingerprint": fingerprint(gate),
                "terminal_snapshot": terminal_snapshot,
            },
        )
        ledger.append(
            event_type="LeaseReleased",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={"lease_id": "old-lease"},
        )
        ledger.append(
            event_type="ProjectionUpdated",
            transaction_id=self.ORIGINAL_TRANSACTION,
            workflow_type=WorkflowType.FEATURE_EXECUTION,
            payload={
                "current_state": "human_decision_required",
                "current_feature": self.FEATURE,
                "selected_feature": None,
            },
        )
        projection = ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        )
        projection.rebuild(persist_cache=True)
        terminal = {
            "schema_version": 1,
            "project_id": project.project_id,
            "repository_identity": identity["repository_id"],
            "run_id": self.ORIGINAL_RUN,
            "transaction_id": self.ORIGINAL_TRANSACTION,
            "workflow_type": "feature_execution",
            "feature_id": self.FEATURE,
            "session_id": self.ORIGINAL_SESSION,
            "classification": "FEATURE_ACCEPTED",
            "next_state": "feature_accepted",
            "starting_branch": self.BRANCH,
            "starting_commit": head,
            "current_commit": head,
            "changed_paths": list(changed),
            "evidence": {"controller_acceptance_pending": True},
        }
        output = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "done\nCONVEYOR_TRANSACTION_RESULT="
                    + json.dumps(terminal),
                },
            }
        )
        write_json(
            controller
            / "reports"
            / self.ORIGINAL_RUN
            / "feature_cycle.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": self.ORIGINAL_RUN,
                "session_id": self.ORIGINAL_SESSION,
                "action": "feature_cycle",
                "result_classification": "structured_output_invalid",
                "exit_classification": "structured_output_invalid",
                "structured_output_validation": "invalid",
                "structured_output_errors": [
                    "terminal session-result envelope workflow_type conflicts with invoked workflow"
                ],
                "terminal_marker_found": True,
                "redacted_stdout": output,
            },
        )
        write_json(
            controller / "state/projects" / f"{project.project_id}.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "current_state": "human_decision_required",
            },
        )
        return configuration, project, head, changed, gate

    @staticmethod
    def _passing_runner(argv: list[str], cwd: Path):
        return {
            "argv": argv,
            "exit_code": 0,
            "duration_seconds": 0.01,
            "output_sha256": "0" * 64,
            "output_summary": "passed",
        }

    def _recovery(self, configuration, project, runner=None):
        return FeatureResultRecovery(
            controller_root=configuration.root,
            configuration=configuration.conveyor,
            project=project,
            command_runner=runner or self._passing_runner,
        )

    def _inspect(self, recovery, head):
        return recovery.inspect(
            feature_id=self.FEATURE,
            original_transaction_id=self.ORIGINAL_TRANSACTION,
            original_run_id=self.ORIGINAL_RUN,
            original_session_id=self.ORIGINAL_SESSION,
            expected_branch=self.BRANCH,
            expected_head=head,
        )

    def _topology_events(
        self,
        event_types: tuple[str, ...],
        *,
        checkpoint_payload: dict | None = None,
        checkpoint_payloads: tuple[dict, ...] | None = None,
        checkpoint_transaction: str | None = None,
        broken_chain_at: int | None = None,
    ) -> list[dict]:
        events = []
        previous = "prior-ledger-fingerprint"
        checkpoint_index = 0
        for index, event_type in enumerate(event_types, start=1):
            payload = {}
            if event_type == "TransactionStarted":
                payload = {"run_id": self.ORIGINAL_RUN}
            elif event_type == "SessionLaunched":
                payload = {"session_id": self.ORIGINAL_SESSION}
            elif event_type == "CheckpointRecorded":
                selected_checkpoint = (
                    checkpoint_payloads[checkpoint_index]
                    if checkpoint_payloads is not None
                    else (
                        checkpoint_payload
                        if checkpoint_payload is not None
                        else AUTHENTICATED_FEATURE_SESSION_CHECKPOINT
                    )
                )
                payload = dict(
                    selected_checkpoint
                )
                checkpoint_index += 1
            event = {
                "sequence": index,
                "fingerprint": f"fingerprint-{index}",
                "previous_fingerprint": (
                    "broken-fingerprint"
                    if broken_chain_at == index
                    else previous
                ),
                "event_type": event_type,
                "transaction_id": (
                    checkpoint_transaction
                    if event_type == "CheckpointRecorded"
                    and checkpoint_transaction is not None
                    else self.ORIGINAL_TRANSACTION
                ),
                "project_id": "synthetic",
                "repository_identity": "repository-identity",
                "repository_path_fingerprint": "repository-path-fingerprint",
                "workflow_type": WorkflowType.FEATURE_EXECUTION.value,
                "payload": payload,
            }
            events.append(event)
            previous = event["fingerprint"]
        return events

    def _authenticate_topology(self, events: list[dict]) -> dict:
        return _authenticate_original_transaction_topology(
            events,
            transaction_id=self.ORIGINAL_TRANSACTION,
            project_id="synthetic",
            repository_identity="repository-identity",
            repository_path_fingerprint="repository-path-fingerprint",
            run_id=self.ORIGINAL_RUN,
            session_id=self.ORIGINAL_SESSION,
        )

    def test_legacy_and_checkpoint_aware_topologies_are_both_exact(self):
        legacy = self._authenticate_topology(
            self._topology_events(LEGACY_RETAINED_RESULT_TOPOLOGY)
        )
        checkpoint_aware = self._authenticate_topology(
            self._topology_events(CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY)
        )
        self.assertTrue(legacy["authenticated"])
        self.assertEqual("pre_m1_017", legacy["variant"])
        self.assertTrue(checkpoint_aware["authenticated"])
        self.assertEqual(
            "m1_017_authenticated_session_checkpoint",
            checkpoint_aware["variant"],
        )
        prepared = self._authenticate_topology(
            self._topology_events(
                PREPARED_CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
                checkpoint_payloads=(
                    AUTHENTICATED_PREPARED_FEATURE_BRANCH_CHECKPOINT,
                    AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                ),
            )
        )
        self.assertTrue(prepared["authenticated"])
        self.assertEqual(
            "prepared_branch_and_session_checkpoints",
            prepared["variant"],
        )

    def test_checkpoint_payload_is_exact_and_duplicate_or_misplaced_is_rejected(self):
        malformed_payloads = (
            {
                **AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                "checkpoint": "wrong",
            },
            {
                **AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                "previous_phase": "feature_preparing",
            },
            {
                **AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                "next_phase": "feature_running",
            },
            {
                **AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                "extra": True,
            },
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                authenticated = self._authenticate_topology(
                    self._topology_events(
                        CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
                        checkpoint_payload=payload,
                    )
                )
                self.assertFalse(authenticated["authenticated"])
                self.assertFalse(
                    authenticated["checks"]["checkpoint_payload_exact"]
                )

        invalid_topologies = (
            CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[:5]
            + ("CheckpointRecorded",)
            + CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[5:],
            (
                "TransactionStarted",
                "LeaseAcquired",
                "SnapshotCaptured",
                "CheckpointRecorded",
                "SessionLaunched",
                "HumanGateRaised",
                "LeaseReleased",
                "ProjectionUpdated",
            ),
            (
                "TransactionStarted",
                "LeaseAcquired",
                "SnapshotCaptured",
                "SessionLaunched",
                "HumanGateRaised",
                "CheckpointRecorded",
                "LeaseReleased",
                "ProjectionUpdated",
            ),
        )
        for topology in invalid_topologies:
            with self.subTest(topology=topology):
                authenticated = self._authenticate_topology(
                    self._topology_events(topology)
                )
                self.assertFalse(authenticated["authenticated"])

    def test_checkpoint_lineage_terminal_and_fingerprint_chain_fail_closed(self):
        foreign = self._authenticate_topology(
            self._topology_events(
                CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
                checkpoint_transaction="foreign-transaction",
            )
        )
        self.assertFalse(foreign["authenticated"])
        self.assertFalse(foreign["checks"]["event_lineage_exact"])

        invalid_topologies = (
            tuple(
                event
                for event in CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY
                if event != "SessionLaunched"
            ),
            tuple(
                event
                for event in CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY
                if event != "HumanGateRaised"
            ),
            CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[:6]
            + ("HumanGateRaised",)
            + CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[6:],
            CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[:5]
            + ("ValidationStarted",)
            + CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY[5:],
        )
        for topology in invalid_topologies:
            with self.subTest(topology=topology):
                self.assertFalse(
                    self._authenticate_topology(
                        self._topology_events(topology)
                    )["authenticated"]
                )

        broken = self._authenticate_topology(
            self._topology_events(
                CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY,
                broken_chain_at=5,
            )
        )
        self.assertFalse(broken["authenticated"])
        self.assertFalse(
            broken["checks"]["events_are_globally_contiguous"]
        )

    def test_checkpoint_aware_retained_result_authenticates_full_recovery_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, gate = self._fixture(
                Path(temporary),
                prepared=True,
                execution_checkpoint_payload=(
                    AUTHENTICATED_FEATURE_SESSION_CHECKPOINT
                ),
            )
            recovery = self._recovery(configuration, project)
            before = (
                git(project.repository, "status", "--porcelain=v1", "--branch"),
                recovery.ledger.path.read_bytes(),
                recovery.projection.cache_path.read_bytes(),
            )
            inspected = self._inspect(recovery, head)
            after = (
                git(project.repository, "status", "--porcelain=v1", "--branch"),
                recovery.ledger.path.read_bytes(),
                recovery.projection.cache_path.read_bytes(),
            )
            self.assertEqual(before, after)
            self.assertEqual(
                "m1_017_authenticated_session_checkpoint",
                inspected["original_transaction_topology"]["variant"],
            )
            self.assertEqual(list(changed), inspected["changed_paths"])
            self.assertEqual(gate["gate_id"], inspected["original_gate_id"])
            self.assertEqual(0, inspected["model_sessions_that_would_launch"])
            self.assertEqual(0, inspected["child_sessions_that_would_launch"])
            self.assertEqual(
                "integration_pending", inspected["final_projected_state"]
            )
            controller_cache = (
                configuration.root
                / "state/projects"
                / f"{project.project_id}.json"
            )
            controller_cache.unlink()
            engine = CycleEngine(configuration, SyntheticLauncher())
            status = engine.project_plan(project)
            self.assertEqual(
                "feature_result_recovery",
                status["proposed_next_action"],
            )
            self.assertEqual(
                "technical_recovery_required", status["current_state"]
            )
            self.assertFalse(status["ordinary_resume_allowed"])
            self.assertIsNone(status["human_gate"])

    def test_checkpoint_aware_synthetic_apply_creates_one_direct_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, gate = self._fixture(
                Path(temporary),
                prepared=True,
                execution_checkpoint_payload=(
                    AUTHENTICATED_FEATURE_SESSION_CHECKPOINT
                ),
            )
            recovery = self._recovery(configuration, project)
            result = recovery.apply(self._inspect(recovery, head))
            inspector = RepositoryInspector(project.repository)
            self.assertEqual("integration_pending", result["outcome"])
            self.assertEqual(head, inspector.rev_parse(f"{inspector.head}^"))
            self.assertEqual(
                result["accepted_feature_commit"], inspector.head
            )
            self.assertTrue(set(changed).issubset(
                inspector.changed_paths(inspector.head)
            ))
            self.assertEqual(gate["gate_id"], result["resolved_gate_id"])
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertFalse(result["milestone_integration_performed"])
            self.assertFalse(result["queue_reconciliation_performed"])
            self.assertFalse(
                (
                    project.repository / ".factory/locks/writer.json"
                ).exists()
            )

    def _failed_recovery_fixture(self, root: Path):
        configuration, project, head, changed, gate = self._fixture(
            root, prepared=True, malformed_factory_position=True
        )
        deterministic = self._recovery(configuration, project)
        original_plan = self._inspect(deterministic, head)
        failed_transaction = "33fbe92b-adab-4422-bcbc-23fcc7031e79"
        failed_run = "feature-recovery-failed"
        snapshot = capture_repository_snapshot(project).to_dict()
        for event_type, payload in (
            (
                "TransactionStarted",
                {
                    "run_id": failed_run,
                    "milestone": "M0",
                    "feature_id": self.FEATURE,
                    "starting_branch": self.BRANCH,
                    "starting_head": head,
                    "starting_queue_fingerprint": snapshot["queue_fingerprint"],
                    "starting_tracked_diff_fingerprint": snapshot[
                        "tracked_diff_fingerprint"
                    ],
                    "starting_untracked_fingerprint": snapshot[
                        "untracked_fingerprint"
                    ],
                    "allowed_mutation_policy": {
                        "allowed_paths": list(changed),
                        "allowed_prefixes": [],
                        "denied_paths": [],
                        "denied_prefixes": [],
                        "allow_untracked": True,
                        "require_clean_start": False,
                        "commit_subject": original_plan["commit_subject"],
                    },
                    "recovered_transaction_id": self.ORIGINAL_TRANSACTION,
                    "original_session_id": self.ORIGINAL_SESSION,
                    "original_gate_id": gate["gate_id"],
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                    "plan_fingerprint": original_plan["plan_fingerprint"],
                },
            ),
            (
                "LeaseAcquired",
                {"lease_id": "failed-recovery-lease", "lease_type": "feature_writer"},
            ),
            ("SnapshotCaptured", {"snapshot": snapshot}),
            (
                "TransactionBlocked",
                {
                    "classification": "FEATURE_VALIDATION_FAILED",
                    "terminal_state": "terminal_failure",
                    "next_state": "human_decision_required",
                    "gate": None,
                    "gate_id": None,
                    "gate_fingerprint": None,
                    "reference": None,
                    "terminal_snapshot": snapshot,
                },
            ),
            ("LeaseReleased", {"lease_id": "failed-recovery-lease"}),
            (
                "ProjectionUpdated",
                {
                    "current_state": "human_decision_required",
                    "current_feature": self.FEATURE,
                    "selected_feature": None,
                },
            ),
        ):
            deterministic.ledger.append(
                event_type=event_type,
                transaction_id=failed_transaction,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )
        deterministic.projection.rebuild(persist_cache=True)
        repair = RetainedFeatureValidationRepair(
            controller_root=configuration.root,
            configuration=configuration.conveyor,
            project=project,
            command_runner=self._passing_runner,
        )
        return (
            configuration,
            project,
            head,
            changed,
            gate,
            failed_transaction,
            repair,
        )

    def test_retained_repair_dry_run_authenticates_failed_evidence_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                _,
                project,
                head,
                changed,
                _,
                failed_transaction,
                repair,
            ) = self._failed_recovery_fixture(Path(temporary))
            before = (
                git(project.repository, "status", "--porcelain=v1"),
                repair.ledger.path.read_bytes(),
                repair.projection.cache_path.read_bytes(),
            )
            plan = repair.inspect(
                feature_id=self.FEATURE,
                original_transaction_id=self.ORIGINAL_TRANSACTION,
                failed_recovery_transaction_id=failed_transaction,
                expected_branch=self.BRANCH,
                expected_head=head,
            )
            after = (
                git(project.repository, "status", "--porcelain=v1"),
                repair.ledger.path.read_bytes(),
                repair.projection.cache_path.read_bytes(),
            )
            self.assertEqual(before, after)
            self.assertEqual(
                list(changed), plan["authenticated_retained_paths"]
            )
            self.assertTrue(set(changed).issubset(plan["allowed_paths"]))
            self.assertEqual("gpt-5.6-terra", plan["repair_profile"]["model"])
            self.assertEqual("high", plan["repair_profile"]["reasoning"])
            self.assertEqual(0, plan["repair_profile"]["child_sessions"])
            self.assertEqual(2, plan["maximum_repair_attempts"])
            self.assertIn(
                "Factory position must contain one feature-state line",
                plan["failed_validation_evidence"]["diagnostic"],
            )
            self.assertEqual(
                4,
                plan["failed_validation_evidence"][
                    "commands_passed_before_failure"
                ],
            )
            self.assertEqual(0, plan["model_sessions_that_would_launch"])

    def test_retained_repair_rejects_unexpected_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                _,
                project,
                head,
                _,
                _,
                failed_transaction,
                repair,
            ) = self._failed_recovery_fixture(Path(temporary))
            (project.repository / "unexpected.txt").write_text(
                "unexpected\n", encoding="utf-8"
            )
            with self.assertRaises(RecoveryError):
                repair.inspect(
                    feature_id=self.FEATURE,
                    original_transaction_id=self.ORIGINAL_TRANSACTION,
                    failed_recovery_transaction_id=failed_transaction,
                    expected_branch=self.BRANCH,
                    expected_head=head,
                )

    def _repair_launcher_factory(self, project, *, fix: bool, counter: list[object]):
        class Launcher:
            def launch(inner_self, request, on_session_started=None):
                counter.append(request)
                session_id = f"019f9707-712f-7ae3-824d-{len(counter):012d}"
                if on_session_started is not None:
                    on_session_started(session_id)
                current = project.repository / "docs/CURRENT_STATUS.md"
                if fix:
                    current.write_text(
                        current.read_text(encoding="utf-8").replace(
                            "- Current feature: F097 — malformed retained evidence",
                            "- Active feature: F097 — Imported Audio Transcription Workflow",
                        ),
                        encoding="utf-8",
                    )
                changed_paths = sorted(
                    {
                        *RepositoryInspector(
                            project.repository
                        ).tracked_changed_paths(),
                        *RepositoryInspector(
                            project.repository
                        ).untracked_file_hashes(),
                    }
                )
                envelope = {
                    "schema_version": 1,
                    "workflow_type": "feature_execution",
                    "classification": "FEATURE_ACCEPTED",
                    "project_id": project.project_id,
                    "repository_identity": request.repository_identity,
                    "transaction_id": request.transaction_id,
                    "run_id": request.run_id,
                    "session_id": session_id,
                    "starting_branch": request.starting_branch,
                    "starting_commit": request.starting_commit,
                    "current_commit": request.starting_commit,
                    "feature_id": request.feature,
                    "changed_paths": changed_paths,
                    "evidence": {
                        "implementation_complete": True,
                        "focused_validation": [],
                        "controller_acceptance_pending": True,
                    },
                    "next_state": "feature_accepted",
                }
                plan = SessionPlan(
                    argv=("codex",),
                    cwd=project.repository,
                    prompt="repair",
                    prompt_sha256="0" * 64,
                    sandbox="workspace-write",
                    effective_model="gpt-5.6-terra",
                    effective_reasoning="high",
                    launched_model="gpt-5.6-terra",
                    launched_reasoning="high",
                    collaboration_tools_removed=True,
                )
                return SessionResult(
                    action="feature_cycle",
                    returncode=0,
                    session_id=session_id,
                    redacted_output="repair completed",
                    plan=plan,
                    redacted_stdout="repair completed",
                    transaction_envelope=envelope,
                    parsed_structured_result=envelope,
                    structured_output_validation="valid",
                    result_classification="FEATURE_ACCEPTED",
                )

        return lambda: Launcher()

    def test_successful_retained_repair_validates_then_commits_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                configuration,
                project,
                head,
                _,
                gate,
                failed_transaction,
                _,
            ) = self._failed_recovery_fixture(Path(temporary))
            launches: list[object] = []
            repair = RetainedFeatureValidationRepair(
                controller_root=configuration.root,
                configuration=configuration.conveyor,
                project=project,
                command_runner=self._passing_runner,
                launcher_factory=self._repair_launcher_factory(
                    project, fix=True, counter=launches
                ),
            )
            plan = repair.inspect(
                feature_id=self.FEATURE,
                original_transaction_id=self.ORIGINAL_TRANSACTION,
                failed_recovery_transaction_id=failed_transaction,
                expected_branch=self.BRANCH,
                expected_head=head,
            )
            result = repair.apply(plan)
            inspector = RepositoryInspector(project.repository)
            self.assertEqual("integration_pending", result["outcome"])
            self.assertEqual(1, len(launches))
            request = launches[0]
            self.assertEqual("gpt-5.6-terra", request.planned_model)
            self.assertEqual("high", request.planned_reasoning)
            self.assertEqual(0, request.child_session_budget)
            self.assertIn(
                "Factory position must contain one feature-state line",
                request.embedded_context,
            )
            self.assertEqual(1, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertEqual(head, inspector.rev_parse(f"{inspector.head}^"))
            self.assertTrue(inspector.is_clean)
            self.assertEqual(
                result["accepted_feature_commit"], inspector.head
            )
            self.assertIsNone(result["projection"]["human_gate"])
            self.assertEqual(
                "integration_pending", result["projection"]["current_state"]
            )
            self.assertFalse(result["milestone_integration_performed"])
            self.assertFalse(result["queue_reconciliation_performed"])
            resolved = [
                event
                for event in repair.ledger.read()
                if event["event_type"] == "HumanGateResolved"
            ]
            self.assertEqual(gate["gate_id"], resolved[-1]["payload"]["gate_id"])

    def test_retained_repair_is_bounded_at_two_identical_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                configuration,
                project,
                head,
                _,
                _,
                failed_transaction,
                _,
            ) = self._failed_recovery_fixture(Path(temporary))
            launches: list[object] = []
            repair = RetainedFeatureValidationRepair(
                controller_root=configuration.root,
                configuration=configuration.conveyor,
                project=project,
                command_runner=self._passing_runner,
                launcher_factory=self._repair_launcher_factory(
                    project, fix=False, counter=launches
                ),
            )
            before = RepositoryInspector(
                project.repository
            ).content_diff_fingerprint(
                tuple(_changed_paths(RepositoryInspector(project.repository)))
            )
            human_gates_before = len(
                [
                    event
                    for event in repair.ledger.read()
                    if event["event_type"] == "HumanGateRaised"
                ]
            )
            result = repair.apply(
                repair.inspect(
                    feature_id=self.FEATURE,
                    original_transaction_id=self.ORIGINAL_TRANSACTION,
                    failed_recovery_transaction_id=failed_transaction,
                    expected_branch=self.BRANCH,
                    expected_head=head,
                )
            )
            self.assertEqual("repair_exhausted", result["outcome"])
            self.assertEqual(2, len(launches))
            self.assertTrue(result["attempts"][-1]["identical_to_previous"])
            self.assertFalse(result["application_commit_created"])
            self.assertEqual(head, RepositoryInspector(project.repository).head)
            self.assertEqual(
                before,
                RepositoryInspector(
                    project.repository
                ).content_diff_fingerprint(
                    tuple(_changed_paths(RepositoryInspector(project.repository)))
                ),
            )
            self.assertTrue(result["recoverable_technical_failure"])
            self.assertFalse(result["human_gate_created"])
            self.assertEqual("review", result["projection"]["current_state"])
            self.assertEqual(
                human_gates_before,
                len(
                    [
                        event
                        for event in repair.ledger.read()
                        if event["event_type"] == "HumanGateRaised"
                    ]
                ),
            )

    def test_cycle_engine_routes_failed_recovery_to_model_backed_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                configuration,
                project,
                head,
                _,
                _,
                failed_transaction,
                _,
            ) = self._failed_recovery_fixture(Path(temporary))
            controller_cache = (
                configuration.root
                / "state/projects"
                / f"{project.project_id}.json"
            )
            if controller_cache.exists():
                controller_cache.unlink()
            routed = CycleEngine(
                configuration, SyntheticLauncher()
            ).project_plan(project)
            self.assertEqual(
                "retained_feature_repair", routed["proposed_next_action"]
            )
            self.assertTrue(routed["recognized_technical_repair"])
            evidence = routed["retained_feature_repair"]
            self.assertTrue(evidence["evidence_authenticated"])
            self.assertEqual(failed_transaction, evidence[
                "failed_recovery_transaction_id"
            ])
            self.assertEqual(head, evidence["expected_head"])
            self.assertEqual(
                "gpt-5.6-terra", evidence["repair_profile"]["model"]
            )
            self.assertEqual("high", evidence["repair_profile"]["reasoning"])
            self.assertEqual(0, evidence["repair_profile"]["child_sessions"])

    def _repair_fixture(self, root: Path):
        configuration, project, head, _, _ = self._fixture(root)
        recovery = self._recovery(configuration, project)
        result = recovery.apply(self._inspect(recovery, head))
        inspector = RepositoryInspector(project.repository)
        controller_cache = (
            configuration.root / "state/projects" / f"{project.project_id}.json"
        )
        value = json.loads(controller_cache.read_text(encoding="utf-8"))
        projection = result["projection"]
        unsupported = (
            "accepted_feature_commit",
            "active_transaction",
            "current_run_id",
            "integration_status",
            "kernel_ledger_fingerprint",
            "kernel_ledger_sequence",
            "kernel_projection_fingerprint",
            "kernel_transaction_id",
            "selected_feature",
        )
        value.update(
            {
                "accepted_feature_commit": result["accepted_feature_commit"],
                "active_transaction": None,
                "current_run_id": result["run_id"],
                "integration_status": "pending",
                "kernel_ledger_fingerprint": projection["ledger_fingerprint"],
                "kernel_ledger_sequence": projection["ledger_sequence"],
                "kernel_projection_fingerprint": projection[
                    "projection_fingerprint"
                ],
                "kernel_transaction_id": result["recovery_transaction_id"],
                "selected_feature": None,
            }
        )
        write_json(controller_cache, value)
        identity = inspector.identity()
        expectation = CacheRepairExpectation(
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            branch=inspector.current_branch,
            head=inspector.head,
            parent=head,
            feature_id=self.FEATURE,
            milestone_branch=str(project.milestone_branch),
            recovery_transaction_id=result["recovery_transaction_id"],
            original_transaction_id=self.ORIGINAL_TRANSACTION,
            recovery_run_id=result["run_id"],
            ledger_sequence=projection["ledger_sequence"],
            ledger_fingerprint=projection["ledger_fingerprint"],
            projection_fingerprint=projection["projection_fingerprint"],
            malformed_cache_sha256=hashlib.sha256(
                controller_cache.read_bytes()
            ).hexdigest(),
            application_cache_sha256=hashlib.sha256(
                inspector.cycle_state_path().read_bytes()
            ).hexdigest(),
            unsupported_keys=unsupported,
        )
        repair = CycleCacheRepair(
            controller_root=configuration.root,
            configuration=configuration,
            project=project,
            expectation=expectation,
        )
        return configuration, project, repair, controller_cache

    def test_general_dry_run_binds_exact_retained_state_and_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, _ = self._fixture(
                Path(temporary)
            )
            plan = self._inspect(self._recovery(configuration, project), head)
            self.assertEqual(plan["changed_paths"], list(changed))
            self.assertTrue(plan["workflow_alias"]["alias_applied"])
            self.assertEqual(
                plan["host_validation_commands"],
                [
                    [
                        "swift",
                        "test",
                        "--filter",
                        "ImportedAudioTranscriptionServiceTests",
                    ],
                    [
                        "swift",
                        "test",
                        "--filter",
                        "PersistentDomainStoreTests",
                    ],
                    ["swift", "build"],
                    ["git", "diff", "--check"],
                ],
            )
            self.assertEqual(plan["model_sessions_that_would_launch"], 0)
            self.assertEqual(plan["child_sessions_that_would_launch"], 0)
            self.assertEqual(
                "supersede_and_resolve_after_host_validation",
                plan["gate_supersession"]["action"],
            )
            self.assertEqual(
                "integration_pending", plan["final_projected_state"]
            )

    def test_f070_preparation_prelaunch_execution_lineage_is_recoverable(self):
        original_identity = (
            self.FEATURE,
            self.BRANCH,
            self.PREPARATION_TRANSACTION,
        )
        self.FEATURE = "F070"
        self.BRANCH = "codex/F070-applications-table"
        self.PREPARATION_TRANSACTION = (
            "0637f3ba-d7a6-4dfb-afee-d68f3d637249"
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                configuration, project, head, changed, _ = self._fixture(
                    Path(temporary),
                    prepared=True,
                    preparation_run_id="distinct-preparation-run",
                    prelaunch_recovery=True,
                )
                recovery = self._recovery(configuration, project)
                before = git(
                    project.repository,
                    "status",
                    "--porcelain=v1",
                    "--branch",
                )
                plan = recovery.inspect_recorded(
                    original_run_id=self.ORIGINAL_RUN,
                    original_session_id=self.ORIGINAL_SESSION,
                    expected_head=head,
                    expected_paths=changed,
                )
                after = git(
                    project.repository,
                    "status",
                    "--porcelain=v1",
                    "--branch",
                )
                self.assertEqual(before, after)
                self.assertTrue(
                    plan["identity_discovered_from_report_and_ledger"]
                )
                self.assertEqual(
                    self.PREPARATION_TRANSACTION,
                    plan["preparation_transaction_id"],
                )
                self.assertEqual(
                    self.PRELAUNCH_TRANSACTION,
                    plan["prelaunch_recovery_transaction_id"],
                )
                self.assertTrue(plan["transaction_lineage"]["continuous"])
                self.assertTrue(
                    plan["checks"]["queue_in_progress_owned_by_execution"]
                )
                self.assertEqual(list(changed), plan["changed_paths"])
                self.assertEqual(
                    0, plan["model_sessions_that_would_launch"]
                )
                self.assertEqual(
                    0, plan["child_sessions_that_would_launch"]
                )
                self.assertEqual(
                    "integration_pending", plan["final_projected_state"]
                )
                (
                    configuration.root
                    / "state/projects"
                    / f"{project.project_id}.json"
                ).unlink()
                engine = CycleEngine(configuration, SyntheticLauncher())
                status = engine.project_plan(project)
                self.assertEqual(
                    "feature_result_recovery",
                    status["proposed_next_action"],
                )
                consistency = ConsistencyChecker(
                    controller_root=configuration.root,
                    project=project,
                    planner_observer=lambda: engine.project_plan(project),
                ).check()
                worktree_invariant = next(
                    item
                    for item in consistency["invariants"]
                    if item["invariant"] == "worktree_status"
                )
                self.assertTrue(worktree_invariant["passed"])
                self.assertTrue(
                    worktree_invariant["evidence"][
                        "feature_result_recovery"
                    ]
                )
                plan_invariant = next(
                    item
                    for item in consistency["invariants"]
                    if item["invariant"]
                    == "execution_plan_projection_agreement"
                )
                self.assertTrue(plan_invariant["passed"])
                self.assertEqual(
                    "feature_result_recovery_plan",
                    plan_invariant["evidence"]["observation_source"],
                )
                with self.assertRaisesRegex(
                    RecoveryError, "command expectations disagree"
                ):
                    recovery.inspect_recorded(
                        original_run_id=self.ORIGINAL_RUN,
                        original_session_id=self.ORIGINAL_SESSION,
                        expected_head=head,
                        expected_diff_fingerprint="0" * 64,
                        expected_paths=changed,
                    )
        finally:
            (
                self.FEATURE,
                self.BRANCH,
                self.PREPARATION_TRANSACTION,
            ) = original_identity

    def test_resume_dispatches_authenticated_feature_result_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, _, _, _ = self._fixture(
                Path(temporary),
                prepared=True,
                execution_checkpoint_payload=(
                    AUTHENTICATED_FEATURE_SESSION_CHECKPOINT
                ),
            )
            controller_cache = (
                configuration.root
                / "state/projects"
                / f"{project.project_id}.json"
            )
            controller_cache.unlink()
            engine = CycleEngine(configuration, SyntheticLauncher())
            planned = engine.project_plan(project)
            self.assertEqual(
                "feature_result_recovery",
                planned["proposed_next_action"],
            )
            self.assertTrue(
                planned["feature_result_recovery"][
                    "evidence_authenticated"
                ]
            )
            expected = {
                "outcome": "integration_pending",
                "accepted_feature_commit": "synthetic-accepted",
            }
            with mock.patch.object(
                FeatureResultRecovery, "apply", return_value=expected
            ) as apply:
                result = engine.run_project(project, "resume")
            self.assertEqual(result, expected)
            apply.assert_called_once()
            inspected = apply.call_args.args[0]
            self.assertEqual(inspected["feature_id"], self.FEATURE)
            self.assertEqual(
                inspected["original_transaction_id"],
                self.ORIGINAL_TRANSACTION,
            )

    def test_capability_prelaunch_and_two_checkpoint_lineage_is_recoverable(self):
        original_identity = (
            self.FEATURE,
            self.BRANCH,
            self.PREPARATION_TRANSACTION,
        )
        self.FEATURE = "F078"
        self.BRANCH = "codex/F078-session-to-application-linking"
        self.PREPARATION_TRANSACTION = (
            "9215fa8c-9509-42de-90b3-828847984528"
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                configuration, project, head, changed, _ = self._fixture(
                    Path(temporary),
                    prepared=True,
                    preparation_run_id="distinct-preparation-run",
                    prelaunch_recovery=True,
                    prelaunch_failure_reference="SessionError",
                    execution_checkpoint_payloads=(
                        AUTHENTICATED_PREPARED_FEATURE_BRANCH_CHECKPOINT,
                        AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
                    ),
                )
                plan = self._recovery(
                    configuration, project
                ).inspect_recorded(
                    original_run_id=self.ORIGINAL_RUN,
                    original_session_id=self.ORIGINAL_SESSION,
                    expected_head=head,
                    expected_paths=changed,
                )
                self.assertEqual(
                    "prepared_branch_and_session_checkpoints",
                    plan["original_transaction_topology"]["variant"],
                )
                self.assertEqual(
                    self.PRELAUNCH_TRANSACTION,
                    plan["prelaunch_recovery_transaction_id"],
                )
                self.assertTrue(plan["transaction_lineage"]["continuous"])
                self.assertTrue(
                    plan["checks"]["prelaunch_recovery_topology"]
                )
                self.assertTrue(
                    plan["checks"]["queue_in_progress_owned_by_execution"]
                )
        finally:
            (
                self.FEATURE,
                self.BRANCH,
                self.PREPARATION_TRANSACTION,
            ) = original_identity

    def test_consumed_selection_uses_preparation_and_current_feature_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, _ = self._fixture(
                Path(temporary), prepared=True
            )
            plan = self._inspect(self._recovery(configuration, project), head)
            self.assertEqual(
                self.PREPARATION_TRANSACTION,
                plan["preparation_transaction_id"],
            )
            self.assertTrue(plan["phase_feature_identity"]["selection_consumed"])
            self.assertEqual(
                self.FEATURE,
                plan["phase_feature_identity"]["transaction_feature_id"],
            )
            self.assertEqual(
                self.FEATURE,
                plan["phase_feature_identity"]["projection_current_feature"],
            )
            self.assertEqual(list(changed), plan["changed_paths"])

    def test_cycle_engine_routes_exact_technical_gate_to_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, gate = self._fixture(
                Path(temporary), prepared=True
            )
            controller_cache = (
                configuration.root
                / "state/projects"
                / f"{project.project_id}.json"
            )
            if controller_cache.exists():
                controller_cache.unlink()
            routed = CycleEngine(
                configuration, SyntheticLauncher()
            ).project_plan(project)
            self.assertEqual(
                "feature_result_recovery",
                routed["proposed_next_action"],
            )
            self.assertTrue(routed["recognized_technical_recovery"])
            self.assertIsNone(routed["human_gate"])
            self.assertEqual(
                gate["gate_id"],
                routed["technical_gate_to_supersede"]["gate_id"],
            )
            recovery = routed["feature_result_recovery"]
            self.assertEqual(self.FEATURE, recovery["feature_id"])
            self.assertTrue(recovery["evidence_authenticated"])
            self.assertEqual(
                self.PREPARATION_TRANSACTION,
                recovery["preparation_transaction_id"],
            )
            self.assertEqual(list(changed), recovery["changed_paths"])
            self.assertEqual(head, recovery["expected_head"])
            self.assertEqual(0, recovery["model_sessions_that_would_launch"])
            self.assertEqual(0, recovery["child_sessions_that_would_launch"])

    def test_unrelated_preparation_feature_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _, _ = self._fixture(
                Path(temporary),
                prepared=True,
                preparation_projection_feature="F999",
            )
            with self.assertRaisesRegex(Exception, "preparation_topology"):
                self._inspect(self._recovery(configuration, project), head)

    def test_unrelated_workflow_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _, _ = self._fixture(Path(temporary))
            report = (
                configuration.root
                / "reports"
                / self.ORIGINAL_RUN
                / "feature_cycle.json"
            )
            value = json.loads(report.read_text(encoding="utf-8"))
            value["action"] = "queue_reconciliation"
            write_json(report, value)
            with self.assertRaisesRegex(
                Exception, "workflow_type conflicts"
            ):
                self._inspect(self._recovery(configuration, project), head)

    def test_exact_run_session_transaction_and_gate_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _, _ = self._fixture(Path(temporary))
            recovery = self._recovery(configuration, project)
            for field, value in (
                ("original_run_id", "wrong-run"),
                ("original_session_id", "wrong-session"),
                ("original_transaction_id", "wrong-transaction"),
            ):
                arguments = {
                    "feature_id": self.FEATURE,
                    "original_transaction_id": self.ORIGINAL_TRANSACTION,
                    "original_run_id": self.ORIGINAL_RUN,
                    "original_session_id": self.ORIGINAL_SESSION,
                    "expected_branch": self.BRANCH,
                    "expected_head": head,
                }
                arguments[field] = value
                with self.subTest(field=field), self.assertRaises(Exception):
                    recovery.inspect(**arguments)

    def test_exact_gate_binding_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _, _ = self._fixture(Path(temporary))
            cache_path = (
                configuration.root
                / "state/projects"
                / project.project_id
                / "projection-cache.json"
            )
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["human_gate"]["gate_id"] = "gate-unrelated"
            cache["projection_fingerprint"] = projection_fingerprint(cache)
            write_json(cache_path, cache)
            with self.assertRaisesRegex(Exception, "gate_identity"):
                self._inspect(self._recovery(configuration, project), head)

    def test_tracked_untracked_and_path_drift_fail_closed(self):
        mutations = (
            (
                "tracked",
                lambda project: (
                    project.repository
                    / "Sources/LiveInterviewCompanion/Services/PersistentDomainStore.swift"
                ).write_text("changed again\n", encoding="utf-8"),
            ),
            (
                "untracked",
                lambda project: (
                    project.repository
                    / "Sources/LiveInterviewCompanion/Services/ImportedAudioTranscriptionService.swift"
                ).write_text("changed again\n", encoding="utf-8"),
            ),
            (
                "extra",
                lambda project: (
                    project.repository / "unexpected.txt"
                ).write_text("extra\n", encoding="utf-8"),
            ),
            (
                "missing",
                lambda project: (
                    project.repository
                    / "Tests/LiveInterviewCompanionTests/ImportedAudioTranscriptionServiceTests.swift"
                ).unlink(),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                configuration, project, head, _, _ = self._fixture(
                    Path(temporary)
                )
                mutate(project)
                with self.assertRaises(Exception):
                    self._inspect(self._recovery(configuration, project), head)

    def test_writer_reservation_and_active_transaction_refuse_recovery(self):
        for condition in ("writer", "reservation", "transaction"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                configuration, project, head, _, _ = self._fixture(
                    Path(temporary)
                )
                recovery = self._recovery(configuration, project)
                if condition == "writer":
                    write_json(
                        project.repository / ".factory/locks/writer.json", {}
                    )
                elif condition == "reservation":
                    identity = RepositoryInspector(project.repository).identity()
                    write_json(
                        configuration.root
                        / "state/launch-locks"
                        / f"{identity['path_fingerprint']}.json",
                        {"run_id": "other"},
                    )
                else:
                    recovery.ledger.append(
                        event_type="TransactionStarted",
                        transaction_id="active-transaction",
                        workflow_type=WorkflowType.RECOVERY,
                        payload={"run_id": "active"},
                    )
                    recovery.projection.rebuild(persist_cache=True)
                with self.assertRaisesRegex(Exception, "preflight failed"):
                    self._inspect(recovery, head)

    def test_validation_failure_preserves_diff_and_creates_no_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, gate = self._fixture(
                Path(temporary)
            )
            inspector = RepositoryInspector(project.repository)
            before = inspector.content_diff_fingerprint(changed)

            def failing_runner(argv, cwd):
                return {
                    "argv": argv,
                    "exit_code": 1,
                    "duration_seconds": 0.01,
                    "output_sha256": "f" * 64,
                    "output_summary": "failed",
                }

            recovery = self._recovery(
                configuration, project, runner=failing_runner
            )
            result = recovery.apply(self._inspect(recovery, head))
            self.assertEqual(result["outcome"], "validation_failed")
            self.assertFalse(result["application_commit_created"])
            self.assertEqual(RepositoryInspector(project.repository).head, head)
            self.assertEqual(
                RepositoryInspector(project.repository).content_diff_fingerprint(
                    changed
                ),
                before,
            )
            self.assertTrue(result["original_gate_preserved"])
            self.assertEqual(
                result["projection"]["human_gate"]["gate_id"], gate["gate_id"]
            )

    def test_successful_apply_commits_once_supersedes_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, changed, gate = self._fixture(
                Path(temporary)
            )
            recovery = self._recovery(configuration, project)
            result = recovery.apply(self._inspect(recovery, head))
            inspector = RepositoryInspector(project.repository)
            self.assertEqual(result["outcome"], "integration_pending")
            self.assertEqual(inspector.rev_parse(f"{inspector.head}^"), head)
            self.assertTrue(set(changed).issubset(inspector.changed_paths(inspector.head)))
            self.assertTrue(inspector.is_clean)
            self.assertEqual(
                result["candidate_implementation_commit"],
                result["accepted_feature_commit"],
            )
            self.assertEqual(result["resolved_gate_id"], gate["gate_id"])
            self.assertTrue(
                result["final_checks"]["original_transaction_superseded"]
            )
            self.assertTrue(result["final_checks"]["original_gate_resolved"])
            self.assertFalse(result["milestone_integration_performed"])
            self.assertFalse(result["queue_reconciliation_performed"])
            self.assertEqual(result["model_sessions_launched"], 0)
            self.assertEqual(result["child_sessions_launched"], 0)
            cycle = json.loads(inspector.cycle_state_path().read_text())
            validate_schema(cycle, recovery.cycle_schema)
            self.assertNotIn("current_state", cycle)
            self.assertNotIn("active_transaction", cycle)
            controller_cache = json.loads(
                (
                    configuration.root
                    / "state/projects"
                    / f"{project.project_id}.json"
                ).read_text()
            )
            validate_schema(controller_cache, recovery.project_schema)
            self.assertFalse(
                {
                    "accepted_feature_commit",
                    "active_transaction",
                    "current_run_id",
                    "integration_status",
                    "kernel_ledger_fingerprint",
                    "kernel_ledger_sequence",
                    "kernel_projection_fingerprint",
                    "kernel_transaction_id",
                    "selected_feature",
                }
                & set(controller_cache)
            )
            with self.assertRaises(Exception):
                self._inspect(recovery, head)

    def test_prepared_in_progress_apply_commits_once_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, head, _, gate = self._fixture(
                Path(temporary), prepared=True
            )
            recovery = self._recovery(configuration, project)
            result = recovery.apply(self._inspect(recovery, head))
            inspector = RepositoryInspector(project.repository)
            self.assertEqual("integration_pending", result["outcome"])
            self.assertEqual(head, inspector.rev_parse(f"{inspector.head}^"))
            self.assertTrue(inspector.is_clean)
            self.assertEqual(gate["gate_id"], result["resolved_gate_id"])
            self.assertEqual(
                self.PREPARATION_TRANSACTION,
                result["preparation_transaction_id"],
            )
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertFalse(result["milestone_integration_performed"])
            self.assertFalse(result["queue_reconciliation_performed"])

    def test_cycle_cache_repair_is_dry_run_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, repair, controller_cache = (
                self._repair_fixture(Path(temporary))
            )
            inspector = RepositoryInspector(project.repository)
            cache_before = controller_cache.read_bytes()
            application_before = inspector.cycle_state_path().read_bytes()
            ledger_path = (
                configuration.root
                / "state/projects"
                / project.project_id
                / "evidence-ledger.jsonl"
            )
            projection_path = ledger_path.with_name("projection-cache.json")
            ledger_before = ledger_path.read_bytes()
            projection_before = projection_path.read_bytes()
            git_before = git(project.repository, "status", "--porcelain=v1", "--branch")

            plan = repair.inspect()
            self.assertEqual("repair_ready", plan["outcome"])
            self.assertEqual(list(repair.expectation.unsupported_keys), plan["unsupported_keys"])
            self.assertEqual(cache_before, controller_cache.read_bytes())
            self.assertEqual(0, plan["model_sessions_that_would_launch"])
            self.assertEqual(0, plan["child_sessions_that_would_launch"])

            result = repair.apply(plan)
            self.assertEqual("repaired", result["outcome"])
            self.assertNotEqual(cache_before, controller_cache.read_bytes())
            self.assertEqual(application_before, inspector.cycle_state_path().read_bytes())
            self.assertEqual(ledger_before, ledger_path.read_bytes())
            self.assertEqual(projection_before, projection_path.read_bytes())
            self.assertEqual(
                git_before,
                git(project.repository, "status", "--porcelain=v1", "--branch"),
            )
            CycleEngine(configuration, SyntheticLauncher()).project_plan(project)
            ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: CycleEngine(
                    configuration, SyntheticLauncher()
                ).project_plan(project),
            ).check()
            second = repair.inspect()
            self.assertEqual("already_canonical", second["outcome"])
            second_apply = repair.apply(second)
            self.assertFalse(second_apply["controller_cache_written"])

    def test_cycle_cache_repair_refuses_stale_dirty_and_owned_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, repair, controller_cache = self._repair_fixture(
                Path(temporary)
            )
            stale = json.loads(controller_cache.read_text())
            stale["last_checkpoint"] = "changed"
            write_json(controller_cache, stale)
            with self.assertRaisesRegex(Exception, "SHA-256 changed"):
                repair.inspect()

        with tempfile.TemporaryDirectory() as temporary:
            _, project, repair, _ = self._repair_fixture(Path(temporary))
            (project.repository / "app.txt").write_text(
                "dirty\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "worktree is dirty"):
                repair.inspect()

        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, repair, _ = self._repair_fixture(
                Path(temporary)
            )
            projection_path = (
                configuration.root
                / "state/projects"
                / project.project_id
                / "projection-cache.json"
            )
            stale_projection = json.loads(projection_path.read_text())
            stale_projection["projection_fingerprint"] = "f" * 64
            write_json(projection_path, stale_projection)
            with self.assertRaisesRegex(
                Exception, "canonical persisted projection evidence is invalid"
            ):
                repair.inspect()

        with tempfile.TemporaryDirectory() as temporary:
            _, project, repair, _ = self._repair_fixture(Path(temporary))
            writer = project.repository / ".factory/locks/writer.json"
            write_json(writer, {"owner": "other"})
            with self.assertRaisesRegex(Exception, "writer lease"):
                repair.inspect()
            writer.unlink()
            write_json(repair._launch_reservation().path, {"run_id": "other"})
            with self.assertRaisesRegex(Exception, "launch reservation"):
                repair.inspect()

        with tempfile.TemporaryDirectory() as temporary:
            configuration, project, repair, _ = self._repair_fixture(
                Path(temporary)
            )
            identity = RepositoryInspector(project.repository).identity()
            ledger = EvidenceLedger(
                configuration.root
                / "state/projects"
                / project.project_id
                / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            ledger.append(
                event_type="TransactionStarted",
                transaction_id="unexpected-active-transaction",
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                payload={
                    "run_id": "unexpected-active-run",
                    "feature_id": self.FEATURE,
                    "milestone": "M0",
                    "starting_branch": repair.expectation.branch,
                    "starting_head": repair.expectation.head,
                    "allowed_mutation_policy": {},
                },
            )
            ProjectionEngine(
                ledger,
                ledger.path.with_name("projection-cache.json"),
            ).rebuild(persist_cache=True)
            with self.assertRaisesRegex(
                Exception, "authoritative projection identity changed"
            ):
                repair.inspect()

    def test_cycle_cache_repair_write_failure_restores_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, repair, controller_cache = self._repair_fixture(
                Path(temporary)
            )
            plan = repair.inspect()
            original = controller_cache.read_bytes()

            def failing_write(path, document, *, project_schema):
                path.write_text("{}\n", encoding="utf-8")
                raise RuntimeError("injected schema write failure")

            with mock.patch(
                "development_conveyor.cycle_cache_repair."
                "write_controller_compatibility_cache",
                side_effect=failing_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    repair.apply(plan)
            self.assertEqual(original, controller_cache.read_bytes())
            self.assertFalse(repair._launch_reservation().path.exists())


if __name__ == "__main__":
    unittest.main()
