from __future__ import annotations

import json
import os
import hashlib
import subprocess
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from development_conveyor.command_authority import CommandAuthority
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.contracts import (
    CommandCategory,
    LeaseType,
    MutationPolicy,
    PhaseTransaction,
    RepositorySnapshot,
    SessionResultEnvelope,
    TransactionState,
    WorkflowType,
    extract_terminal_envelope,
    fingerprint,
)
from development_conveyor.errors import (
    CorruptEvidenceError,
    LockError,
    ProjectionError,
    RecoveryError,
    SafetyViolation,
    SchemaValidationError,
    TransactionError,
)
from development_conveyor.kernel import WorkflowKernel
from development_conveyor.ledger import EvidenceLedger, canonical_bytes, event_fingerprint
from development_conveyor.legacy_adapter import LegacyTransitionAdapter
from development_conveyor.migration import LegacyStateMigrator
from development_conveyor.projection import (
    ProjectionEngine, projection_fingerprint, build_projection_observations,
)
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.simulator import (
    DeterministicLifecycleSimulator, INTERRUPTION_BOUNDARIES,
    SIMULATED_WORKFLOWS, simulate_all_interruptions,
)
from development_conveyor.workflow_lease import WorkflowWriterLease
from development_conveyor.workflow_bridge import KernelWorkflowBridge, PhaseExecution
from development_conveyor.workflow_recovery import RecoveryPlanner
from development_conveyor.validation import SafetyPolicy
from development_conveyor.kernel import (
    QueueReconciliationAdapter, FeaturePreparationAdapter, FeatureExecutionAdapter,
    FeatureAcceptanceAdapter, MilestoneIntegrationAdapter, MilestoneGateAdapter,
    HumanDecisionResolutionAdapter, RecoveryAdapter,
)

from tests.helpers import controller_configuration, git, synthetic_repository


class LedgerTests(unittest.TestCase):
    def make_ledger(self, root: Path, project: str = "p", repository: str = "r") -> EvidenceLedger:
        return EvidenceLedger(
            root / "evidence.jsonl",
            project_id=project,
            repository_identity=repository * 64,
            repository_path_fingerprint="f" * 64,
        )

    def append_complete(self, ledger: EvidenceLedger, transaction: str = "t") -> None:
        ledger.append(event_type="TransactionStarted", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="LeaseAcquired", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="SnapshotCaptured", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="ValidationStarted", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="ValidationPassed", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="TransactionCompleted", transaction_id=transaction, workflow_type=WorkflowType.RECOVERY, payload={"classification": "RECOVERY_APPLIED", "next_state": "feature_ready"})

    def test_legacy_terminal_replay_does_not_repeat_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            adapter = LegacyTransitionAdapter(
                controller_root=root / "controller",
                repository=repository,
                project_id=project.project_id,
            )
            mutations: list[str] = []
            arguments = {
                "run_id": "legacy-replay",
                "feature_id": "F001",
                "milestone": "M1",
                "previous_state": "feature_in_progress",
                "next_state": "feature_review",
                "checkpoint": "legacy-review",
                "mutate": lambda: mutations.append("called"),
            }

            adapter.transition(**arguments)
            adapter.transition(**arguments)

            self.assertEqual(["called"], mutations)
            terminals = [
                event for event in adapter.ledger.read()
                if event["event_type"] == "TransactionCompleted"
            ]
            self.assertEqual(1, len(terminals))

    @staticmethod
    def rewrite_rehashed(ledger: EvidenceLedger, values: list[dict]) -> None:
        previous = "0" * 64
        for sequence, value in enumerate(values, start=1):
            value["sequence"] = sequence
            value["previous_fingerprint"] = previous
            value.pop("fingerprint", None)
            value["fingerprint"] = event_fingerprint(value)
            previous = value["fingerprint"]
        ledger.path.write_bytes(b"\n".join(canonical_bytes(item) for item in values) + b"\n")
        ledger.head_path.write_text(json.dumps({
            "schema_version": 1, "sequence": len(values), "fingerprint": previous,
        }), encoding="utf-8")

    def test_append_is_canonical_and_monotonic(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            lines = ledger.path.read_bytes().splitlines()
            self.assertEqual(lines, [canonical_bytes(json.loads(line)) for line in lines])
            self.assertEqual(list(range(1, 7)), [item["sequence"] for item in ledger.read()])

    def test_direct_started_to_completed_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            ledger.append(event_type="TransactionStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
            with self.assertRaises(CorruptEvidenceError):
                ledger.append(event_type="TransactionCompleted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={"classification": "RECOVERY_APPLIED"})

    def test_hash_chain_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            self.assertTrue(ledger.verify().valid)
            values = ledger.read()
            values[1]["previous_fingerprint"] = "1" * 64
            ledger.path.write_bytes(b"\n".join(canonical_bytes(item) for item in values) + b"\n")
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_truncation_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            ledger.path.write_bytes(ledger.path.read_bytes()[:-1])
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_real_process_death_one_record_ahead_repairs_durable_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.make_ledger(root)
            ledger.append(
                event_type="TransactionStarted", transaction_id="one-ahead",
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "run_id": "one-ahead", "starting_branch": "main",
                    "starting_head": "a" * 40,
                    "allowed_mutation_policy": MutationPolicy(tuple()).to_dict(),
                },
            )
            source = r'''
import os, sys
from pathlib import Path
import development_conveyor.ledger as module
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.contracts import WorkflowType
ledger=EvidenceLedger(Path(sys.argv[1]), project_id="p", repository_identity="r"*64, repository_path_fingerprint="f"*64)
module.atomic_write_json=lambda *args, **kwargs: os._exit(73)
ledger.append(event_type="LeaseAcquired", transaction_id="one-ahead", workflow_type=WorkflowType.RECOVERY, payload={"lease_id":"lease","lease_type":"recovery_writer"})
'''
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            child = subprocess.run(
                ["python3", "-c", source, str(ledger.path)],
                cwd=Path(__file__).resolve().parents[1], env=environment, check=False,
            )
            self.assertEqual(73, child.returncode)
            stale_head = json.loads(ledger.head_path.read_text(encoding="utf-8"))
            self.assertEqual(1, stale_head["sequence"])
            self.assertEqual(2, ledger.verify().sequence)
            repaired_head = json.loads(ledger.head_path.read_text(encoding="utf-8"))
            self.assertEqual(2, repaired_head["sequence"])

    def test_complete_tail_record_deletion_detected_by_durable_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            lines = ledger.path.read_bytes().splitlines()
            ledger.path.write_bytes(b"\n".join(lines[:-1]) + b"\n")
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_rehashed_extra_field_and_payload_type_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            values = ledger.read()
            values[0]["unexpected"] = True
            self.rewrite_rehashed(ledger, values)
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            values = ledger.read()
            values[0]["payload"]["starting_head"] = 17
            self.rewrite_rehashed(ledger, values)
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_reordering_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            lines = ledger.path.read_bytes().splitlines()
            ledger.path.write_bytes(lines[1] + b"\n" + lines[0] + b"\n")
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_duplicate_event_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            first = ledger.path.read_bytes().splitlines()[0]
            ledger.path.write_bytes(ledger.path.read_bytes() + first + b"\n")
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()

    def test_project_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            other = self.make_ledger(Path(temporary), project="other")
            with self.assertRaises(CorruptEvidenceError):
                other.verify()

    def test_repository_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            other = self.make_ledger(Path(temporary), repository="x")
            with self.assertRaises(CorruptEvidenceError):
                other.verify()

    def test_double_terminal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            with self.assertRaises(CorruptEvidenceError):
                ledger.append(event_type="TransactionBlocked", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})

    def test_terminal_transaction_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = self.make_ledger(Path(temporary))
            self.append_complete(ledger)
            with self.assertRaises(CorruptEvidenceError):
                ledger.append(event_type="ValidationStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})

    def test_symlink_ledger_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("", encoding="utf-8")
            link = root / "evidence.jsonl"
            link.symlink_to(target)
            ledger = EvidenceLedger(link, project_id="p", repository_identity="r" * 64, repository_path_fingerprint="f" * 64)
            with self.assertRaises(CorruptEvidenceError):
                ledger.verify()


class ContractTests(unittest.TestCase):
    def snapshot(self) -> RepositorySnapshot:
        return RepositorySnapshot("r" * 64, "p" * 64, "main", "a" * 40, None, "d" * 64, "u" * 64, (), (), {})

    def test_transaction_start_and_completion(self):
        transaction = PhaseTransaction.create(
            workflow_type=WorkflowType.RECOVERY, project_id="p", snapshot=self.snapshot(),
            milestone="M0", feature_id=None, run_id="run", policy=MutationPolicy(tuple()),
        )
        transaction.transition(TransactionState.LEASE_PENDING)
        transaction.transition(TransactionState.ACTIVE)
        transaction.transition(TransactionState.RESULT_PENDING)
        transaction.transition(TransactionState.VALIDATING)
        transaction.transition(TransactionState.FINALIZING)
        transaction.terminate(TransactionState.COMPLETED, "RECOVERY_APPLIED")
        self.assertEqual(TransactionState.COMPLETED, transaction.current_state)

    def test_completed_transaction_cannot_reactivate(self):
        transaction = PhaseTransaction.create(
            workflow_type=WorkflowType.RECOVERY, project_id="p", snapshot=self.snapshot(),
            milestone=None, feature_id=None, run_id="run", policy=MutationPolicy(tuple()),
        )
        transaction.terminate(TransactionState.BLOCKED, "blocked")
        with self.assertRaises(TransactionError):
            transaction.transition(TransactionState.ACTIVE)

    def test_cross_phase_classification_rejected(self):
        value = self.envelope()
        value["workflow_type"] = "milestone_integration"
        value["classification"] = "FEATURE_ACCEPTED"
        with self.assertRaises(SchemaValidationError):
            SessionResultEnvelope.from_dict(value)

    def test_every_adapter_routes_every_non_success_classification_to_terminal_state(self):
        adapters = (
            QueueReconciliationAdapter(allowed_paths=(), commit_subject="q", next_state="feature_ready"),
            FeaturePreparationAdapter(allowed_paths=(), commit_subject="p", next_state="feature_preparing"),
            FeatureExecutionAdapter(allowed_paths=(), commit_subject="f", next_state="feature_accepted"),
            FeatureAcceptanceAdapter(allowed_paths=(), commit_subject="a", next_state="integration_ready"),
            MilestoneIntegrationAdapter(allowed_paths=(), commit_subject="i", next_state="feature_integrated"),
            MilestoneGateAdapter(allowed_paths=(), commit_subject="g", next_state="milestone_ready_for_merge"),
            HumanDecisionResolutionAdapter(allowed_paths=(), commit_subject="h", next_state="milestone_gate"),
            RecoveryAdapter(allowed_paths=(), commit_subject="r", next_state="feature_ready"),
        )
        for adapter in adapters:
            for classification, terminal in adapter.CLASSIFICATION_STATES.items():
                if terminal is None:
                    continue
                next_state = adapter.CLASSIFICATION_NEXT_STATES[classification] or adapter.next_state
                envelope = SessionResultEnvelope.from_dict({
                    **self.envelope(), "workflow_type": adapter.workflow_type.value,
                    "classification": classification, "next_state": next_state,
                })
                self.assertIsNotNone(adapter.terminal_state(envelope), (adapter.workflow_type, classification))

    def test_feature_prompt_excludes_legacy_preflights_incompatible_with_kernel_lease(self):
        prompt = (Path(__file__).resolve().parents[1] / "prompts/feature-cycle.md").read_text(encoding="utf-8")
        self.assertIn("Do not run the legacy `queuectl preflight`", prompt)
        self.assertIn("`git_transition.py acceptance-preflight`", prompt)
        self.assertIn("verify the exact existing typed lease read-only", prompt)
        self.assertNotIn("Run acceptance preflight without releasing", prompt)

    def test_feature_policy_rejects_planning_mutation(self):
        adapter = FeatureExecutionAdapter(
            allowed_paths=("app.txt",), commit_subject="F1: feature", next_state="feature_accepted",
        )
        with self.assertRaises(TransactionError):
            adapter.policy.validate(("docs/FEATURE_QUEUE.yaml",))

    def envelope(self):
        return {
            "schema_version": 1, "workflow_type": "feature_execution",
            "classification": "FEATURE_ACCEPTED", "project_id": "p",
            "repository_identity": "r", "transaction_id": "t", "run_id": "run",
            "session_id": "s", "starting_branch": "main", "starting_commit": "a",
            "current_commit": "a", "feature_id": "F1", "changed_paths": [],
            "evidence": {}, "next_state": "feature_review",
        }

    def test_terminal_envelope_only_final_assistant_message(self):
        marker = "CONVEYOR_TRANSACTION_RESULT=" + json.dumps(self.envelope(), separators=(",", ":"))
        output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": marker}})
        self.assertEqual("FEATURE_ACCEPTED", extract_terminal_envelope(output).classification)

    def test_prompt_echo_spoofing_rejected(self):
        marker = "CONVEYOR_TRANSACTION_RESULT=" + json.dumps(self.envelope())
        output = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": marker}})
        with self.assertRaises(SchemaValidationError):
            extract_terminal_envelope(output)

    def test_trailing_prose_rejected(self):
        marker = "CONVEYOR_TRANSACTION_RESULT=" + json.dumps(self.envelope())
        output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": marker + "\ntrailing"}})
        with self.assertRaises(SchemaValidationError):
            extract_terminal_envelope(output)

    def test_duplicate_terminal_markers_rejected(self):
        marker = "CONVEYOR_TRANSACTION_RESULT=" + json.dumps(self.envelope())
        output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": marker + "\n" + marker}})
        with self.assertRaises(SchemaValidationError):
            extract_terminal_envelope(output)

    def test_tool_output_marker_cannot_spoof_terminal_result(self):
        marker = "CONVEYOR_TRANSACTION_RESULT=" + json.dumps(self.envelope())
        tool = json.dumps({"type": "item.completed", "item": {"type": "tool_output", "text": marker}})
        assistant = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "no terminal envelope"}})
        with self.assertRaises(SchemaValidationError):
            extract_terminal_envelope(tool + "\n" + assistant)


class LeaseAndCommandTests(unittest.TestCase):
    def test_writer_lease_rejects_symlink_target_and_hard_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.json"
            outside.write_text("preserve\n", encoding="utf-8")
            lock = root / "app/.factory/locks/writer.json"
            lock.parent.mkdir(parents=True)
            lock.symlink_to(outside)
            lease = WorkflowWriterLease(lock)
            with self.assertRaises(Exception):
                lease.acquire(
                    lease_type=LeaseType.FEATURE_WRITER,
                    repository_identity="r", repository_path_fingerprint="p", project_id="p",
                    transaction_id="t", workflow_type=WorkflowType.FEATURE_EXECUTION,
                    milestone="M", feature_id="F", starting_branch="main", starting_head="a",
                    run_id="run", session_id=None, policy=MutationPolicy(tuple()),
                )
            self.assertEqual("preserve\n", outside.read_text(encoding="utf-8"))

            lock.unlink()
            record = lease.acquire(
                lease_type=LeaseType.FEATURE_WRITER,
                repository_identity="r", repository_path_fingerprint="p", project_id="p",
                transaction_id="t", workflow_type=WorkflowType.FEATURE_EXECUTION,
                milestone="M", feature_id="F", starting_branch="main", starting_head="a",
                run_id="run", session_id=None, policy=MutationPolicy(tuple()),
            )
            os.link(lock, root / "lease-hardlink.json")
            with self.assertRaises(Exception):
                lease.read()
            self.assertEqual("t", record.transaction_id)

    def test_writer_lease_rejects_symlinked_factory_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "app"
            repository.mkdir()
            outside = root / "outside"
            (outside / "locks").mkdir(parents=True)
            (repository / ".factory").symlink_to(outside, target_is_directory=True)
            lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
            with self.assertRaises(Exception):
                lease.acquire(
                    lease_type=LeaseType.FEATURE_WRITER,
                    repository_identity="r", repository_path_fingerprint="p", project_id="p",
                    transaction_id="t", workflow_type=WorkflowType.FEATURE_EXECUTION,
                    milestone="M", feature_id="F", starting_branch="main", starting_head="a",
                    run_id="run", session_id=None, policy=MutationPolicy(tuple()),
                )
            self.assertFalse((outside / "locks/writer.json").exists())

    def test_each_phase_uses_its_exact_typed_lease(self):
        cases = (
            (WorkflowType.QUEUE_RECONCILIATION, LeaseType.PLANNING_WRITER),
            (WorkflowType.FEATURE_EXECUTION, LeaseType.FEATURE_WRITER),
            (WorkflowType.MILESTONE_INTEGRATION, LeaseType.INTEGRATION_WRITER),
            (WorkflowType.RECOVERY, LeaseType.RECOVERY_WRITER),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "writer.json"
            lease = WorkflowWriterLease(path)
            for index, (workflow, lease_type) in enumerate(cases):
                record = lease.acquire(
                    lease_type=lease_type,
                    repository_identity="r", repository_path_fingerprint="p", project_id="p",
                    transaction_id=f"t{index}", workflow_type=workflow,
                    milestone="M", feature_id=None, starting_branch="main", starting_head="a",
                    run_id=f"run{index}", session_id=None, policy=MutationPolicy(tuple()),
                )
                self.assertEqual(lease_type, record.lease_type)
                lease.release(
                    transaction_id=f"t{index}", workflow_type=workflow,
                    repository_identity="r", project_id="p",
                )
                self.assertFalse(path.exists())
    def test_cross_phase_lease_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            lease = WorkflowWriterLease(Path(temporary) / "writer.json")
            with self.assertRaises(Exception):
                lease.acquire(
                    lease_type=LeaseType.INTEGRATION_WRITER,
                    repository_identity="r", repository_path_fingerprint="p", project_id="p",
                    transaction_id="t", workflow_type=WorkflowType.FEATURE_EXECUTION,
                    milestone="M", feature_id="F", starting_branch="main", starting_head="a",
                    run_id="run", session_id=None, policy=MutationPolicy(tuple()),
                )

    def test_required_command_failure_is_authoritative(self):
        authority = CommandAuthority(configured_required=(("python3", "test.py"),))
        record = authority.classify(("python3", "test.py"), 1, configured_source="adapter")
        with self.assertRaises(TransactionError):
            authority.validate([record])

    def test_optional_diagnostic_failure_is_warning(self):
        authority = CommandAuthority(optional_diagnostics=(("diag",),))
        record = authority.classify(("diag",), 1)
        _, warnings = authority.validate([record])
        self.assertEqual(CommandCategory.OPTIONAL_DIAGNOSTIC, warnings[0].classification)

    def test_unsupported_command_is_not_authoritative(self):
        record = CommandAuthority().classify(("unknown",), 1)
        self.assertFalse(record.authoritative)
        self.assertEqual(CommandCategory.UNSUPPORTED_COMMAND, record.classification)

    def test_command_diagnostic_is_redacted_before_record_creation(self):
        record = CommandAuthority(configured_required=(("check",),)).classify(
            ("check",), 1, diagnostic="Authorization: Bearer fake-test-token-abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", record.diagnostic)
        self.assertIn("REDACTED", record.diagnostic)


class ProjectionTests(unittest.TestCase):
    def ledger(self, root: Path) -> EvidenceLedger:
        return EvidenceLedger(root / "ledger.jsonl", project_id="p", repository_identity="r" * 64, repository_path_fingerprint="f" * 64)

    def test_cache_rebuild_when_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            self.append_recovery(ledger)
            engine = ProjectionEngine(ledger, root / "cache.json")
            first = engine.current()
            ledger.append(event_type="LeaseReleased", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
            current = engine.current()
            self.assertGreater(current["ledger_sequence"], first["ledger_sequence"])

    @staticmethod
    def append_recovery(ledger: EvidenceLedger) -> None:
        ledger.append(event_type="TransactionStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="LeaseAcquired", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="SnapshotCaptured", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="ValidationStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="ValidationPassed", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
        ledger.append(event_type="TransactionCompleted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={"classification": "RECOVERY_APPLIED", "next_state": "feature_ready"})

    def test_cache_sequence_ahead_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            ledger.append(event_type="TransactionStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
            cache = ProjectionEngine(ledger, root / "cache.json").rebuild()
            cache["ledger_sequence"] += 1
            cache["projection_fingerprint"] = projection_fingerprint(cache)
            (root / "cache.json").write_text(json.dumps(cache), encoding="utf-8")
            with self.assertRaises(ProjectionError):
                ProjectionEngine(ledger, root / "cache.json").current()

    def test_cache_wrong_fingerprint_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            ledger.append(event_type="TransactionStarted", transaction_id="t", workflow_type=WorkflowType.RECOVERY, payload={})
            engine = ProjectionEngine(ledger, root / "cache.json")
            engine.rebuild()
            value = json.loads((root / "cache.json").read_text())
            value["current_state"] = "poisoned"
            (root / "cache.json").write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ProjectionError):
                engine.current()

    def test_cache_target_symlink_and_hardlink_are_rejected_without_external_write(self):
        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                ledger = self.ledger(root)
                self.append_recovery(ledger)
                external = root / "external.json"
                sentinel = b'{"sentinel":true}\n'
                external.write_bytes(sentinel)
                cache = root / "cache.json"
                if attack == "symlink":
                    cache.symlink_to(external)
                else:
                    os.link(external, cache)
                with self.assertRaises(ProjectionError):
                    ProjectionEngine(ledger, cache).rebuild(persist_cache=True)
                self.assertEqual(sentinel, external.read_bytes())

    def test_cache_is_confined_beside_ledger_and_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root / "state")
            with self.assertRaisesRegex(ProjectionError, "confined"):
                ProjectionEngine(ledger, root / "elsewhere/cache.json")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            ledger = self.ledger(alias)
            with self.assertRaisesRegex(ProjectionError, "parent"):
                ProjectionEngine(ledger, alias / "cache.json").load_cache()

    def test_current_observations_are_reproducible_and_override_only_current_consistency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            ledger.append(event_type="TransactionStarted", transaction_id="active", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"feature_id": "F1", "run_id": "r", "starting_branch": "feature", "starting_head": "a" * 40, "allowed_mutation_policy": {}})
            ledger.append(event_type="LeaseAcquired", transaction_id="active", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"lease_id": "l", "lease_type": "feature_writer"})
            ledger.append(event_type="SnapshotCaptured", transaction_id="active", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"snapshot": {}})
            ledger.append(event_type="SessionLaunched", transaction_id="active", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"session_id": "s1"})
            engine = ProjectionEngine(ledger, root / "cache.json")
            stale = build_projection_observations(
                branch="feature", head="a" * 40, clean=True, git_operations={},
                queue_feature="F1", queue_integration_status="pending",
                lease_transaction="active", lease_valid=True, live_session_id="old",
            )
            first = engine.rebuild(persist_cache=False, observations=stale)
            second = engine.rebuild(persist_cache=False, observations=stale)
            self.assertEqual(first, second)
            self.assertEqual("stale_session", first["current_repository_consistency"])
            self.assertFalse(first["session_resume_eligible"])

            mismatch = build_projection_observations(
                branch="feature", head="a" * 40, clean=True, git_operations={},
                queue_feature="F2", queue_integration_status="pending",
                lease_transaction="other", lease_valid=False, live_session_id="s1",
            )
            projected = engine.rebuild(persist_cache=False, observations=mismatch)
            self.assertEqual("lease_mismatch", projected["current_repository_consistency"])
            self.assertEqual("verify_consistency", projected["allowed_next_action"])
            queue_disagreement = build_projection_observations(
                branch="feature", head="a" * 40, clean=True, git_operations={},
                queue_feature="F2", queue_integration_status="pending",
                lease_transaction="active", lease_valid=True, live_session_id="s1",
            )
            self.assertEqual(
                "queue_projection_disagreement",
                engine.rebuild(persist_cache=False, observations=queue_disagreement)["current_repository_consistency"],
            )

    def test_current_dirtiness_does_not_rewrite_historical_integration_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            payload = {"feature_id": "F1", "run_id": "r", "starting_branch": "milestone", "starting_head": "a" * 40, "allowed_mutation_policy": {}}
            ledger.append(event_type="TransactionStarted", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload=payload)
            ledger.append(event_type="LeaseAcquired", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={"lease_id": "l", "lease_type": "integration_writer"})
            ledger.append(event_type="SnapshotCaptured", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={"snapshot": {}})
            ledger.append(event_type="ValidationStarted", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={})
            ledger.append(event_type="ValidationPassed", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={})
            ledger.append(event_type="TransactionCompleted", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={"classification": "INTEGRATED", "next_state": "feature_integrated", "feature_id": "F1", "accepted_feature_commit": "b" * 40, "integrated_commit": "c" * 40, "terminal_snapshot": {"head": "c" * 40, "clean": True}})
            ledger.append(event_type="LeaseReleased", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={"lease_id": "l"})
            ledger.append(event_type="ProjectionUpdated", transaction_id="i", workflow_type=WorkflowType.MILESTONE_INTEGRATION, payload={"current_state": "feature_integrated", "current_feature": "F1"})
            observations = build_projection_observations(
                branch="milestone", head="c" * 40, clean=False, git_operations={},
                queue_feature="F1", queue_integration_status="passed",
                lease_transaction=None, lease_valid=True,
            )
            projection = ProjectionEngine(ledger).rebuild(persist_cache=False, observations=observations)
            self.assertEqual("unexplained_dirty_worktree", projection["current_repository_consistency"])
            self.assertTrue(projection["historical_integration_outcomes"][0]["terminal_repository_clean"])

    def test_precommit_feature_accepted_result_is_not_projected_as_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = self.ledger(root)
            ledger.append(event_type="TransactionStarted", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"feature_id": "F1", "run_id": "r", "starting_branch": "feature", "starting_head": "a" * 40, "allowed_mutation_policy": {}})
            ledger.append(event_type="LeaseAcquired", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"lease_id": "l", "lease_type": "feature_writer"})
            ledger.append(event_type="SnapshotCaptured", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"snapshot": {}})
            ledger.append(event_type="SessionLaunched", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"session_id": "s"})
            ledger.append(event_type="SessionResultAccepted", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"classification": "FEATURE_ACCEPTED", "session_id": "s", "current_commit": "a" * 40, "changed_paths": [], "next_state": "feature_accepted", "envelope": {}})
            projection = ProjectionEngine(ledger).rebuild(persist_cache=False)
            self.assertIsNone(projection["accepted_feature_commit"])
            self.assertEqual("feature_running", projection["current_state"])
            self.assertEqual("f", projection["active_transaction"])
            ledger.append(event_type="ValidationStarted", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={})
            ledger.append(event_type="ValidationPassed", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={})
            ledger.append(event_type="CommitFinalized", transaction_id="f", workflow_type=WorkflowType.FEATURE_EXECUTION, payload={"commit": "b" * 40, "changed_paths": []})
            after_commit = ProjectionEngine(ledger).rebuild(persist_cache=False)
            self.assertIsNone(after_commit["accepted_feature_commit"])
            self.assertEqual("feature_running", after_commit["current_state"])
            self.assertEqual("finalizing", after_commit["transactions"][0]["state"])


class MigrationAndSimulatorTests(unittest.TestCase):
    INTERVIEW_ACCEPTED_COMMIT = "a1f2c8dd47aaa68580cd7dfc3dc6923e04469857"
    INTERVIEW_MILESTONE_START = "e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7"
    INTERVIEW_FAILED_RUN = "fd2e154b-f80a-4f95-8724-efe1f17925c0"
    INTERVIEW_FAILED_TRANSACTION = "ccad845b-7fdd-4250-9f64-f072d17c4e49"
    INTERVIEW_FAILED_SESSION = "019f86cc-d38c-7dc1-87de-294a89e163c4"

    @classmethod
    def immutable_migration_fixture(
        cls,
        root: Path,
        *,
        project_id: str,
        feature_id: str,
        integration_pending: bool,
        exact_interview_identity: bool = False,
        terminal_failure: bool = False,
    ):
        """Build all migration inputs under one disposable controller/repository root."""

        repository, project = synthetic_repository(root / "application")
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        adapter_path = repository / ".factory/project.yaml"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["project"]["id"] = f"fixture-{project_id}"
        adapter_path.write_text(json.dumps(adapter, indent=2) + "\n", encoding="utf-8")

        feature = queue["features"][0]
        feature.update({
            "id": feature_id,
            "title": f"Immutable {feature_id} fixture",
            "spec": f"docs/features/{feature_id}.md",
        })
        (repository / f"docs/features/{feature_id}.md").write_text(
            f"# {feature_id}\n\nImmutable migration fixture.\n", encoding="utf-8"
        )
        git(repository, "rm", project.queue_location)
        git(repository, "add", ".factory/project.yaml", f"docs/features/{feature_id}.md")
        git(repository, "commit", "-m", "test: create immutable migration fixture")
        milestone_start = git(repository, "rev-parse", "HEAD")

        feature_branch = None
        accepted_commit = None
        if integration_pending:
            feature_branch = (
                "codex/F005-persistent-data-store"
                if feature_id == "F005" else f"codex/{feature_id}-accepted"
            )
            git(repository, "switch", "-c", feature_branch)
            (repository / "app.txt").write_text(
                f"baseline\n{feature_id} accepted behavior\n", encoding="utf-8"
            )
            git(repository, "add", "app.txt")
            git(repository, "commit", "-m", f"{feature_id}: accepted fixture")
            generated_accepted = git(repository, "rev-parse", "HEAD")
            git(repository, "switch", "codex/m0-foundation")
            accepted_commit = (
                cls.INTERVIEW_ACCEPTED_COMMIT
                if exact_interview_identity else generated_accepted
            )
            feature.update({
                "status": "integration_pending",
                "branch": feature_branch,
                "integration_base_commit": (
                    cls.INTERVIEW_MILESTONE_START
                    if exact_interview_identity else milestone_start
                ),
                "accepted_commit": accepted_commit,
                "integrated_commit": None,
                "integration_status": "pending",
            })
        else:
            feature.update({
                "status": "ready",
                "branch": None,
                "integration_base_commit": None,
                "accepted_commit": None,
                "integrated_commit": None,
                "integration_status": "pending",
            })

        exclude = repository / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8")
            + "\ndocs/FEATURE_QUEUE.yaml\n.factory/conveyor-state.json\n",
            encoding="utf-8",
        )
        queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
        project = replace(
            project,
            project_id=project_id,
            repository=repository,
            last_accepted_feature=feature_id if integration_pending else None,
            last_accepted_commit=accepted_commit,
            current_state="validation_failed" if terminal_failure else (
                "integration_ready" if integration_pending else "feature_ready"
            ),
            registration_notes="Immutable disposable migration fixture.",
        )
        identity = RepositoryInspector(repository).identity()
        if integration_pending:
            cycle = {
                "schema_version": 1,
                "project_id": project_id,
                "repository_identity": identity,
                "current_feature": feature_id,
                "current_phase": "branch_preparing",
                "conveyor_run_id": "old-pre-migration-run",
                "feature_session_id": "old-pre-migration-session",
            }
            (repository / ".factory/conveyor-state.json").write_text(
                json.dumps(cycle, indent=2) + "\n", encoding="utf-8"
            )

        configuration = controller_configuration(root, project)
        state_root = configuration.root / "state/projects"
        state_root.mkdir(parents=True, exist_ok=True)

        if terminal_failure:
            (state_root / f"{project_id}.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "project_id": project_id,
                    "repository_fingerprint": identity["path_fingerprint"],
                    "current_state": "validation_failed",
                    "run_id": cls.INTERVIEW_FAILED_RUN,
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            project_state = state_root / project_id
            ledger = EvidenceLedger(
                project_state / "evidence-ledger.jsonl",
                project_id=project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            workflow = WorkflowType.MILESTONE_INTEGRATION
            transaction = cls.INTERVIEW_FAILED_TRANSACTION
            ledger.append(
                event_type="TransactionStarted", transaction_id=transaction,
                workflow_type=workflow, payload={
                    "run_id": cls.INTERVIEW_FAILED_RUN,
                    "feature_id": feature_id,
                    "milestone": "M0",
                    "starting_branch": "codex/m0-foundation",
                    "starting_head": cls.INTERVIEW_MILESTONE_START,
                    "allowed_mutation_policy": {},
                },
            )
            ledger.append(
                event_type="LeaseAcquired", transaction_id=transaction,
                workflow_type=workflow,
                payload={"lease_id": "failed-lease", "lease_type": "integration_writer"},
            )
            ledger.append(
                event_type="SnapshotCaptured", transaction_id=transaction,
                workflow_type=workflow, payload={"snapshot": {
                    "branch": "codex/m0-foundation",
                    "head": cls.INTERVIEW_MILESTONE_START,
                }},
            )
            ledger.append(
                event_type="SessionLaunched", transaction_id=transaction,
                workflow_type=workflow,
                payload={"session_id": cls.INTERVIEW_FAILED_SESSION},
            )
            ledger.append(
                event_type="ValidationStarted", transaction_id=transaction,
                workflow_type=workflow, payload={},
            )
            ledger.append(
                event_type="ValidationFailed", transaction_id=transaction,
                workflow_type=workflow,
                payload={"diagnostic": "fixture pre-mutation structured result rejected"},
            )
            ledger.append(
                event_type="TransactionBlocked", transaction_id=transaction,
                workflow_type=workflow, payload={
                    "classification": "VALIDATION_FAILED",
                    "reference": "SessionError",
                    "terminal_state": "terminal_failure",
                    "next_state": "validation_failed",
                    "terminal_snapshot": {
                        "branch": "codex/m0-foundation",
                        "head": cls.INTERVIEW_MILESTONE_START,
                        "clean": True,
                    },
                },
            )
            ledger.append(
                event_type="LeaseReleased", transaction_id=transaction,
                workflow_type=workflow, payload={"lease_id": "failed-lease"},
            )
            ProjectionEngine(
                ledger, project_state / "projection-cache.json"
            ).rebuild(persist_cache=True)
        if git(repository, "status", "--porcelain"):
            raise AssertionError("immutable migration fixture repository is dirty")
        return configuration, project, accepted_commit

    @staticmethod
    def start_kernel(
        root: Path, repository: Path, project, *, workflow: WorkflowType,
        policy: MutationPolicy, run_id: str, feature_id: str = "F001",
        launch_session: bool = True,
    ):
        exclude = repository / ".git/info/exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n", encoding="utf-8")
        identity = RepositoryInspector(repository).identity()
        ledger = EvidenceLedger(
            root / "controller/ledger.jsonl", project_id="synthetic",
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, root / "controller/projection.json")
        lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
        kernel = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease)
        transaction = kernel.begin(
            workflow_type=workflow, milestone="M0", feature_id=feature_id,
            run_id=run_id, policy=policy,
        )
        kernel.acquire_lease(); kernel.capture_snapshot()
        if launch_session:
            kernel.session_launched(f"{run_id}-session")
        return kernel, transaction, ledger, projection, lease, identity

    @staticmethod
    def accept_kernel(kernel, transaction, identity, *, classification: str, changed_paths: list[str], next_state: str):
        kernel.accept_result(SessionResultEnvelope.from_dict({
            "schema_version": 1, "workflow_type": transaction.workflow_type.value,
            "classification": classification, "project_id": "synthetic",
            "repository_identity": identity["repository_id"],
            "transaction_id": transaction.transaction_id, "run_id": transaction.run_id,
            "session_id": f"{transaction.run_id}-session",
            "starting_branch": transaction.starting_branch,
            "starting_commit": transaction.starting_head,
            "current_commit": transaction.starting_head, "feature_id": transaction.feature_id,
            "changed_paths": changed_paths, "evidence": {}, "next_state": next_state,
        }))

    @staticmethod
    def write_cli_configuration(root: Path, project) -> Path:
        configuration = controller_configuration(root, project)
        value = {
            key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
            for key, item in project.__dict__.items()
        }
        (configuration.root / "config").mkdir(parents=True, exist_ok=True)
        (configuration.root / "config/conveyor.yaml").write_text(
            json.dumps(configuration.conveyor, indent=2) + "\n", encoding="utf-8"
        )
        (configuration.root / "config/projects.yaml").write_text(
            json.dumps({"schema_version": 1, "projects": [value]}, indent=2) + "\n",
            encoding="utf-8",
        )
        return configuration.root

    def test_cli_resume_routes_clean_and_exact_dirty_interruptions_through_planner(self):
        child_source = r'''
import json, sys
from pathlib import Path
from development_conveyor.registry import Project
from development_conveyor.repository import RepositoryInspector
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.workflow_lease import WorkflowWriterLease
from development_conveyor.kernel import WorkflowKernel
from development_conveyor.contracts import WorkflowType, MutationPolicy, SessionResultEnvelope
value=json.loads(sys.argv[1]); value["repository"]=Path(value["repository"]); value["human_gates"]=tuple(value["human_gates"])
project=Project(**value); root=Path(sys.argv[2]); mode=sys.argv[3]; dirty=mode != "clean"
identity=RepositoryInspector(project.repository).identity()
ledger=EvidenceLedger(root / "state/projects/synthetic/evidence-ledger.jsonl", project_id="synthetic", repository_identity=identity["repository_id"], repository_path_fingerprint=identity["path_fingerprint"])
kernel=WorkflowKernel(project=project, ledger=ledger, projection=ProjectionEngine(ledger, root / "state/projects/synthetic/projection-cache.json"), lease=WorkflowWriterLease(project.repository / ".factory/locks/writer.json"))
tx=kernel.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0", feature_id="F001", run_id="cli-crash", policy=MutationPolicy(("app.txt",), commit_subject="F001: recovered"))
kernel.acquire_lease(); kernel.capture_snapshot()
if dirty:
    kernel.session_launched("fresh-typed-session")
    (project.repository / "app.txt").write_text("recovered feature\n", encoding="utf-8")
    classification="FEATURE_VALIDATION_FAILED" if mode == "failure" else "FEATURE_ACCEPTED"
    next_state="validation_failed" if mode == "failure" else "feature_accepted"
    kernel.accept_result(SessionResultEnvelope.from_dict({"schema_version":1,"workflow_type":"feature_execution","classification":classification,"project_id":"synthetic","repository_identity":identity["repository_id"],"transaction_id":tx.transaction_id,"run_id":"cli-crash","session_id":"fresh-typed-session","starting_branch":tx.starting_branch,"starting_commit":tx.starting_head,"current_commit":tx.starting_head,"feature_id":"F001","changed_paths":["app.txt"],"evidence":{},"next_state":next_state}))
    kernel.record_file_mutation_boundary()
    if mode == "validated":
        from development_conveyor.command_authority import CommandAuthority
        kernel.validate(authority=CommandAuthority(), command_results=())
'''
        for mode in ("clean", "dirty", "validated", "failure"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                exclude = repository / ".git/info/exclude"
                exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n", encoding="utf-8")
                controller = self.write_cli_configuration(root, project)
                project_value = {
                    key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                    for key, item in project.__dict__.items()
                }
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                subprocess.run(
                    ["python3", "-c", child_source, json.dumps(project_value), str(controller), mode],
                    cwd=Path(__file__).resolve().parents[1], env=environment, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                environment["DEVELOPMENT_CONVEYOR_HOME"] = str(controller)
                result = subprocess.run(
                    [str(Path(__file__).resolve().parents[1] / "scripts/conveyor"), "resume", "--project", "synthetic"],
                    cwd=Path(__file__).resolve().parents[1], env=environment, check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual("kernel_recovery_applied", output["outcome"])
                expected_action = {
                    "clean": "supersede_interrupted_transaction",
                    "dirty": "finalize_exact_dirty_transaction",
                    "validated": "finalize_exact_dirty_transaction",
                    "failure": "terminalize_exact_dirty_result",
                }[mode]
                self.assertEqual(expected_action, output["kernel_recovery"]["action"])
                inspector = RepositoryInspector(repository)
                self.assertEqual(mode != "failure", inspector.is_clean)
                self.assertFalse((repository / ".factory/locks/writer.json").exists())
                if mode in {"dirty", "validated"}:
                    self.assertEqual("recovered feature", (repository / "app.txt").read_text(encoding="utf-8").strip())
                    self.assertNotEqual(project.validated_baseline_commit, inspector.head)
                else:
                    self.assertEqual(project.validated_baseline_commit, inspector.head)

    def test_dirty_start_rejects_post_validation_prestart_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            exclude = repository / ".git/info/exclude"
            exclude.write_text(
                exclude.read_text(encoding="utf-8")
                + "\n.factory/locks/writer.json\n",
                encoding="utf-8",
            )
            (repository / "app.txt").write_text("allowed dirty baseline\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                root / "controller/ledger.jsonl",
                project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
            kernel = WorkflowKernel(
                project=project,
                ledger=ledger,
                projection=ProjectionEngine(ledger),
                lease=lease,
            )
            acquire = kernel._preacquire_lease

            def acquire_then_drift(transaction):
                acquire(transaction)
                (repository / "app.txt").write_text(
                    "drift after validation before transaction start\n", encoding="utf-8"
                )

            kernel._preacquire_lease = acquire_then_drift
            with self.assertRaisesRegex(
                TransactionError,
                "repository changed before the planned writer lease was acquired",
            ):
                kernel.begin(
                    workflow_type=WorkflowType.FEATURE_EXECUTION,
                    milestone="M0",
                    feature_id="F001",
                    run_id="dirty-start-drift",
                    policy=MutationPolicy(
                        ("app.txt",),
                        require_clean_start=False,
                        commit_subject="F001: dirty start",
                    ),
                )
            self.assertEqual([], ledger.read())
            self.assertIsNone(lease.read())

    def test_dirty_recovery_rejects_drift_after_observation_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project,
                workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001: recovered"),
                run_id="dirty-drift",
            )
            (repository / "app.txt").write_text("recorded dirty baseline\n", encoding="utf-8")
            self.accept_kernel(
                kernel, transaction, identity,
                classification="FEATURE_ACCEPTED", changed_paths=["app.txt"],
                next_state="feature_accepted",
            )
            kernel.record_file_mutation_boundary()

            def drift_after_snapshot(boundary, active):
                if boundary == "after_snapshot" and active.workflow_type == WorkflowType.RECOVERY:
                    (repository / "app.txt").write_text("unexpected drift\n", encoding="utf-8")

            planner = RecoveryPlanner(
                project=project, ledger=ledger, projection=projection, lease=lease,
                interruption_hook=drift_after_snapshot,
            )
            with self.assertRaisesRegex(TransactionError, "unauthorized path mutation"):
                planner.apply(allow_current_owner_for_simulation=True)
            recovery_events = [
                event for event in ledger.read()
                if event["workflow_type"] == WorkflowType.RECOVERY.value
            ]
            self.assertFalse(any(
                event["event_type"] == "TransactionCompleted"
                for event in recovery_events
            ))

    def test_dirty_recovery_rejects_drift_before_observation_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project,
                workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001: recovered"),
                run_id="dirty-pre-observation-drift",
            )
            (repository / "app.txt").write_text("recorded dirty baseline\n", encoding="utf-8")
            self.accept_kernel(
                kernel, transaction, identity,
                classification="FEATURE_ACCEPTED", changed_paths=["app.txt"],
                next_state="feature_accepted",
            )
            kernel.record_file_mutation_boundary()

            def drift_before_lease(boundary, active):
                if boundary == "before_lease" and active is None:
                    (repository / "app.txt").write_text("pre-observation drift\n", encoding="utf-8")

            planner = RecoveryPlanner(
                project=project, ledger=ledger, projection=projection, lease=lease,
                interruption_hook=drift_before_lease,
            )
            inspected = planner.inspect()
            self.assertEqual(["app.txt"], inspected["changed_paths"])
            self.assertIsInstance(inspected["diff_fingerprint"], str)
            with self.assertRaisesRegex(
                RecoveryError, "changed after exact dirty recovery inspection"
            ):
                planner.apply(allow_current_owner_for_simulation=True)
            recovery_events = [
                event for event in ledger.read()
                if event["workflow_type"] == WorkflowType.RECOVERY.value
            ]
            self.assertFalse(any(
                event["event_type"] in {"CommitFinalized", "TransactionCompleted"}
                for event in recovery_events
            ))

    def test_restored_validation_rejects_durable_boundary_drift_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project,
                workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001: recovered"),
                run_id="restored-boundary-drift",
            )
            (repository / "app.txt").write_text("recorded dirty baseline\n", encoding="utf-8")
            self.accept_kernel(
                kernel, transaction, identity,
                classification="FEATURE_ACCEPTED", changed_paths=["app.txt"],
                next_state="feature_accepted",
            )
            kernel.record_file_mutation_boundary()
            planner = RecoveryPlanner(
                project=project, ledger=ledger, projection=projection, lease=lease,
            )
            applied = planner.apply(allow_current_owner_for_simulation=True)
            self.assertEqual("resume_exact_transaction", applied["action"])
            (repository / "app.txt").write_text("drift before restored validation\n", encoding="utf-8")
            restored = WorkflowKernel(
                project=project, ledger=ledger, projection=projection, lease=lease,
            )
            restored.restore(transaction.transaction_id)
            with self.assertRaisesRegex(
                TransactionError, "durable mutation boundary"
            ):
                restored.validate(authority=CommandAuthority(), command_results=())
            original_events = [
                event for event in ledger.read()
                if event["transaction_id"] == transaction.transaction_id
            ]
            self.assertEqual(1, sum(
                event["event_type"] == "ChangesDetected" for event in original_events
            ))
            self.assertFalse(any(
                event["event_type"] in {"CommitFinalized", "TransactionCompleted"}
                for event in original_events
            ))

    def test_clean_commit_recovery_rejects_before_lease_drift_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")

            def stop_after_commit(boundary, active):
                if boundary == "after_commit":
                    raise RuntimeError("commit completed before durable evidence")

            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project,
                workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001: recovered"),
                run_id="clean-commit-drift",
            )
            kernel.interrupt = stop_after_commit
            (repository / "app.txt").write_text("committed feature\n", encoding="utf-8")
            self.accept_kernel(
                kernel, transaction, identity,
                classification="FEATURE_ACCEPTED", changed_paths=["app.txt"],
                next_state="feature_accepted",
            )
            kernel.record_file_mutation_boundary()
            kernel.validate(authority=CommandAuthority(), command_results=())
            with self.assertRaisesRegex(RuntimeError, "commit completed"):
                kernel.finalize()
            self.assertTrue(RepositoryInspector(repository).is_clean)

            def drift_before_takeover(boundary, active):
                if boundary == "before_lease" and active is None:
                    (repository / "app.txt").write_text("drift after clean inspection\n", encoding="utf-8")

            planner = RecoveryPlanner(
                project=project, ledger=ledger, projection=projection, lease=lease,
                interruption_hook=drift_before_takeover,
            )
            self.assertEqual(
                "commit_succeeded_before_evidence", planner.inspect()["classification"]
            )
            with self.assertRaisesRegex(
                RecoveryError, "changed after clean commit recovery inspection"
            ):
                planner.apply(allow_current_owner_for_simulation=True)
            self.assertIsNone(lease.read())
            recovery_events = [
                event for event in ledger.read()
                if event["workflow_type"] == WorkflowType.RECOVERY.value
            ]
            self.assertFalse(any(
                event["event_type"] in {"CommitFinalized", "TransactionCompleted"}
                for event in recovery_events
            ))

    def test_no_change_feature_execution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, _, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001"), run_id="no-change",
            )
            self.accept_kernel(kernel, transaction, identity, classification="FEATURE_ACCEPTED", changed_paths=[], next_state="feature_accepted")
            kernel.record_file_mutation_boundary(); kernel.validate(authority=CommandAuthority(), command_results=())
            with self.assertRaisesRegex(TransactionError, "nonempty diff"):
                kernel.finalize()

    def test_branch_switch_before_terminal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, _, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001"), run_id="branch-switch",
            )
            (repository / "app.txt").write_text("feature\n", encoding="utf-8")
            self.accept_kernel(kernel, transaction, identity, classification="FEATURE_ACCEPTED", changed_paths=["app.txt"], next_state="feature_accepted")
            kernel.record_file_mutation_boundary(); kernel.validate(authority=CommandAuthority(), command_results=()); kernel.finalize()
            git(repository, "switch", "main")
            with self.assertRaisesRegex(TransactionError, "branch or HEAD changed"):
                kernel.complete()

    def test_same_branch_head_advance_rejects_finalization_before_kernel_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                run_id="same-branch-head-advance",
            )
            (repository / "app.txt").write_text("feature content\n", encoding="utf-8")
            self.accept_kernel(
                kernel, transaction, identity, classification="FEATURE_ACCEPTED",
                changed_paths=["app.txt"], next_state="feature_accepted",
            )
            kernel.record_file_mutation_boundary()
            kernel.validate(authority=CommandAuthority(), command_results=())

            git(repository, "add", "app.txt")
            git(repository, "commit", "-m", "external same-branch advance")
            advanced_head = git(repository, "rev-parse", "HEAD")
            self.assertEqual(transaction.starting_branch, git(repository, "branch", "--show-current"))
            self.assertNotEqual(transaction.starting_head, advanced_head)
            sequence_before = ledger.verify().sequence
            with self.assertRaisesRegex(TransactionError, "branch or HEAD changed after validation"):
                kernel.finalize()
            self.assertEqual(advanced_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual("", git(repository, "diff", "--cached", "--name-only"))
            self.assertEqual(sequence_before, ledger.verify().sequence)
            transaction_events = [
                item for item in ledger.read()
                if item["transaction_id"] == transaction.transaction_id
            ]
            self.assertFalse(any(item["event_type"] == "CommitFinalized" for item in transaction_events))
            self.assertFalse(any(item["event_type"] == "TransactionCompleted" for item in transaction_events))
            kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="TERMINAL_FEATURE_FAILURE", next_state="validation_failed",
            )

    def test_cycle_cache_does_not_advance_between_commit_and_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, _, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(("app.txt",), commit_subject="F001"), run_id="cache-order",
            )
            (repository / "app.txt").write_text("feature\n", encoding="utf-8")
            self.accept_kernel(kernel, transaction, identity, classification="FEATURE_ACCEPTED", changed_paths=["app.txt"], next_state="feature_accepted")
            kernel.record_file_mutation_boundary(); kernel.validate(authority=CommandAuthority(), command_results=()); kernel.finalize()

            class RecordingStore:
                def __init__(self): self.writes = []
                def write(self, path, value): self.writes.append((path, dict(value)))

            engine = CycleEngine(controller_configuration(root, project))
            store = RecordingStore(); engine.cycle_store = store
            state = {"current_phase": "feature_in_progress"}
            engine._advance_cycle(Path("cycle.json"), state, "feature_review", RepositoryInspector(repository), "after_commit", kernel=kernel)
            self.assertEqual([], store.writes)
            completion = kernel.complete()
            state.update({
                "current_feature": completion["projection"].get("selected_feature") or completion["projection"].get("current_feature"),
                "current_phase": completion["projection"]["current_state"],
                "conveyor_run_id": "cache-order",
                "last_successful_checkpoint": "terminal",
                "updated_at": "2026-01-01T00:00:00+00:00",
            })
            engine._materialize_terminal_cycle_cache(Path("cycle.json"), state, transaction.transaction_id, completion, kernel.ledger)
            self.assertEqual(1, len(store.writes))
            terminal_sequence = next(
                event["sequence"] for event in kernel.ledger.read()
                if event["transaction_id"] == transaction.transaction_id
                and event["event_type"] == "TransactionCompleted"
            )
            self.assertGreaterEqual(store.writes[0][1]["kernel_ledger_sequence"], terminal_sequence)
            signed = store.writes[0][1]
            self.assertEqual(transaction.transaction_id, signed["kernel_transaction_id"])
            self.assertEqual(signed["kernel_cache_fingerprint"], fingerprint({
                key: value for key, value in signed.items() if key != "kernel_cache_fingerprint"
            }))
            signed["current_feature"] = "F999"
            self.assertNotEqual(signed["kernel_cache_fingerprint"], fingerprint({
                key: value for key, value in signed.items() if key != "kernel_cache_fingerprint"
            }))

    def test_terminal_completion_evidence_cannot_override_canonical_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.QUEUE_RECONCILIATION,
                policy=MutationPolicy(tuple(), commit_subject="queue"), run_id="terminal-spoof",
                feature_id=None,
            )
            self.accept_kernel(
                kernel, transaction, identity, classification="RECONCILED_READY_WORK",
                changed_paths=[], next_state="feature_ready",
            )
            kernel.record_file_mutation_boundary()
            kernel.validate(authority=CommandAuthority(), command_results=())
            kernel.finalize()
            with self.assertRaisesRegex(TransactionError, "reserved terminal fields"):
                kernel.complete(evidence={"next_state": "milestone_complete"})
            self.assertIsNone(ledger.terminal_event(transaction.transaction_id))
            kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="TERMINAL_PLANNING_FAILURE", next_state="validation_failed",
            )

    def test_human_terminal_requires_exact_unique_gate_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, _, _, _ = self.start_kernel(
                root, repository, project, workflow=WorkflowType.QUEUE_RECONCILIATION,
                policy=MutationPolicy(tuple(), commit_subject="queue"),
                run_id="missing-gate", feature_id=None, launch_session=False,
            )
            with self.assertRaisesRegex(TransactionError, "exact gate object"):
                kernel.block(
                    state=TransactionState.HUMAN_DECISION_REQUIRED,
                    classification="HUMAN_DECISION_REQUIRED",
                    next_state="human_decision_required",
                )
            self.assertIsNone(ledger.terminal_event(transaction.transaction_id))
            gate = {
                "gate_id": "unique-gate", "classification": "product_decision",
                "reason": "Choose the exact product behavior.",
                "approved_next_state": "human_decision_required",
            }
            kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required", human_gate=gate,
            )
            first_gate = next(
                event["payload"]["gate"] for event in ledger.read()
                if event["event_type"] == "HumanGateRaised"
            )
            self.assertEqual("queue_reconciliation", first_gate["approved_next_state"])
            self.assertEqual(transaction.transaction_id, first_gate["transaction_id"])
            self.assertEqual(project.project_id, first_gate["project_id"])
            self.assertEqual("queue_reconciliation", first_gate["workflow_type"])
            self.assertEqual(ledger.repository_identity, first_gate["repository_identity"])
            self.assertEqual(
                ledger.repository_path_fingerprint,
                first_gate["repository_path_fingerprint"],
            )
            second, _, _, _, _, _ = self.start_kernel(
                root, repository, project, workflow=WorkflowType.QUEUE_RECONCILIATION,
                policy=MutationPolicy(tuple(), commit_subject="queue"),
                run_id="duplicate-gate", feature_id=None, launch_session=False,
            )
            second.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required", human_gate=gate,
            )
            gate_ids = [
                event["payload"]["gate_id"] for event in ledger.read()
                if event["event_type"] == "HumanGateRaised"
            ]
            self.assertEqual(2, len(set(gate_ids)))

    def test_milestone_gate_denies_source_and_uses_prelaunch_pinned_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            engine = CycleEngine(controller_configuration(root, project))
            adapter, allowed = engine._milestone_gate_adapter(
                project, RepositoryInspector(repository)
            )
            self.assertIn(project.queue_location, allowed)
            with self.assertRaisesRegex(TransactionError, "denied control-plane mutation"):
                adapter.policy.validate((".factory/project.yaml",))
            with self.assertRaisesRegex(TransactionError, "unauthorized path mutation"):
                adapter.policy.validate(("app.txt",))
            adapter_path = repository / ".factory/project.yaml"
            initial = json.loads(adapter_path.read_text(encoding="utf-8"))
            initial["commands"]["validate"] = [["python3", "-c", "raise SystemExit(0)"]]
            adapter_path.write_text(json.dumps(initial, indent=2) + "\n", encoding="utf-8")
            pinned = engine._configured_kernel_commands(project)
            modified = dict(initial)
            modified["commands"] = dict(initial["commands"])
            modified["commands"]["validate"] = [["false"]]
            adapter_path.write_text(json.dumps(modified, indent=2) + "\n", encoding="utf-8")
            authority, records = engine._execute_kernel_commands(project, pinned)
            accepted, _ = authority.validate(records)
            self.assertEqual(pinned, tuple(record.command for record in accepted))
            self.assertNotIn(("false",), tuple(record.command for record in accepted))

    def test_production_configured_executor_rejects_prohibited_action_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            with SafetyPolicy.observe_command_attempts() as observed:
                with mock.patch(
                    "development_conveyor.cycle_engine.subprocess.run"
                ) as launched, self.assertRaisesRegex(SafetyViolation, "publication"):
                    CycleEngine._execute_kernel_commands(
                        project, (("npm", "publish"),)
                    )
            launched.assert_not_called()
            self.assertEqual("configured_validation", observed[0]["authority"])
            self.assertEqual(["publish"], observed[0]["prohibited_categories"])

    def test_bridge_terminal_evidence_spoof_is_terminalized_as_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                root / "controller/ledger.jsonl", project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            kernel = WorkflowKernel(
                project=project, ledger=ledger, projection=ProjectionEngine(ledger),
                lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
            )
            adapter = QueueReconciliationAdapter(
                allowed_paths=(), commit_subject="queue", next_state="feature_ready",
            )

            def execute(transaction):
                return PhaseExecution(SessionResultEnvelope.from_dict({
                    "schema_version": 1, "workflow_type": "queue_reconciliation",
                    "classification": "RECONCILED_READY_WORK", "project_id": "synthetic",
                    "repository_identity": identity["repository_id"],
                    "transaction_id": transaction.transaction_id, "run_id": "bridge-spoof",
                    "session_id": "bridge-spoof-session",
                    "starting_branch": transaction.starting_branch,
                    "starting_commit": transaction.starting_head,
                    "current_commit": transaction.starting_head, "feature_id": None,
                    "changed_paths": [], "evidence": {}, "next_state": "feature_ready",
                }), completion_evidence={"classification": "INTEGRATED"})

            result = KernelWorkflowBridge(kernel).run(
                adapter=adapter, run_id="bridge-spoof", milestone="M0", feature_id=None,
                session_id="bridge-spoof-session", execute=execute,
            )
            self.assertIn("reserved terminal fields", result["error"])
            terminal = ledger.terminal_event(result["transaction"]["transaction_id"])
            self.assertEqual("TransactionBlocked", terminal["event_type"])
            self.assertEqual("PLANNING_VALIDATION_FAILED", terminal["payload"]["classification"])

    def test_dirty_recovery_requires_exact_recorded_prefix_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                policy=MutationPolicy(tuple(), allowed_prefixes=("src",), allow_untracked=True, commit_subject="F001"),
                run_id="prefix-diff",
            )
            (repository / "src").mkdir(); (repository / "src/new.py").write_text("one\n", encoding="utf-8")
            self.accept_kernel(kernel, transaction, identity, classification="FEATURE_ACCEPTED", changed_paths=["src/new.py"], next_state="feature_accepted")
            kernel.record_file_mutation_boundary()
            planner = RecoveryPlanner(project=project, ledger=ledger, projection=projection, lease=lease)
            self.assertEqual("resume", planner.inspect()["classification"])
            (repository / "src/new.py").write_text("tampered\n", encoding="utf-8")
            tampered = planner.inspect()
            self.assertEqual("HUMAN_DECISION_REQUIRED", tampered["classification"])
            self.assertFalse(tampered["recoverable"])

    def test_hard_linked_mutation_path_is_rejected_before_launch_and_finalization(self):
        for boundary in ("before_launch", "before_finalize"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                app = repository / "app.txt"
                external = root / "external.txt"
                if boundary == "before_launch":
                    external.write_bytes(app.read_bytes())
                    app.unlink(); os.link(external, app)
                kernel, transaction, _, _, _, identity = self.start_kernel(
                    root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                    policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                    run_id=f"hardlink-{boundary}",
                    launch_session=False,
                )
                if boundary == "before_launch":
                    with self.assertRaisesRegex(TransactionError, "hard-link count"):
                        kernel.session_launched(f"{transaction.run_id}-session")
                else:
                    kernel.session_launched(f"{transaction.run_id}-session")
                    app.write_text("feature\n", encoding="utf-8")
                    self.accept_kernel(
                        kernel, transaction, identity, classification="FEATURE_ACCEPTED",
                        changed_paths=["app.txt"], next_state="feature_accepted",
                    )
                    kernel.record_file_mutation_boundary()
                    kernel.validate(authority=CommandAuthority(), command_results=())
                    os.link(app, external)
                    with self.assertRaisesRegex(TransactionError, "hard-link count"):
                        kernel.finalize()
                self.assertEqual(2, os.lstat(app).st_nlink)
                kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="TERMINAL_FEATURE_FAILURE", next_state="validation_failed",
                )

    def test_post_launch_link_swap_is_rejected_before_external_content_read(self):
        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                kernel, transaction, _, _, _, identity = self.start_kernel(
                    root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                    policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                    run_id=f"post-launch-{attack}",
                )
                external = root / "external.txt"; external.write_bytes(b"never read")
                target = repository / "app.txt"; target.unlink()
                target.symlink_to(external) if attack == "symlink" else os.link(external, target)
                envelope = SessionResultEnvelope.from_dict({
                    "schema_version": 1, "workflow_type": "feature_execution",
                    "classification": "FEATURE_ACCEPTED", "project_id": "synthetic",
                    "repository_identity": identity["repository_id"],
                    "transaction_id": transaction.transaction_id, "run_id": transaction.run_id,
                    "session_id": f"{transaction.run_id}-session",
                    "starting_branch": transaction.starting_branch,
                    "starting_commit": transaction.starting_head,
                    "current_commit": transaction.starting_head, "feature_id": "F001",
                    "changed_paths": ["app.txt"], "evidence": {},
                    "next_state": "feature_accepted",
                })
                with RepositoryInspector.observe_content_reads() as observed_reads:
                    with self.assertRaisesRegex(Exception, "symbolic link|hard-link count|securely open"):
                        kernel.accept_result(envelope)
                    self.assertEqual([], observed_reads)

    def test_kernel_lease_revalidation_exact_matches_every_bound_field(self):
        mutations = {
            "lease_id": "different-lease",
            "repository_path_fingerprint": "0" * 64,
            "repository_path": "/different/repository",
            "milestone": "M9",
            "feature_id": "F999",
            "starting_branch": "different-branch",
            "starting_head": "0" * 40,
            "run_id": "different-run",
            "session_id": "different-session",
            "allowed_mutations": {"allowed_paths": []},
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                kernel, _, _, _, lease, _ = self.start_kernel(
                    root, repository, project, workflow=WorkflowType.FEATURE_EXECUTION,
                    policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                    run_id=f"lease-{field}",
                )
                kernel.session_launched("expected-session")
                value = json.loads(lease.path.read_text(encoding="utf-8"))
                value[field] = replacement
                lease.path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
                with self.assertRaises(LockError):
                    kernel.capture_snapshot()

    def test_terminal_materialization_replays_once_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            kernel, transaction, ledger, _, _, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.QUEUE_RECONCILIATION,
                policy=MutationPolicy(tuple(), commit_subject="queue"),
                run_id="materialization-replay", feature_id=None,
            )
            self.accept_kernel(
                kernel, transaction, identity, classification="RECONCILED_READY_WORK",
                changed_paths=[], next_state="feature_ready",
            )
            kernel.record_file_mutation_boundary()
            kernel.validate(authority=CommandAuthority(), command_results=())
            kernel.finalize()
            calls = []
            materialized = root / "compatibility.json"

            def callback(_projection):
                calls.append(len(calls) + 1)
                if not materialized.exists():
                    materialized.write_text('{"state":"feature_ready"}\n', encoding="utf-8")
                if len(calls) == 1:
                    raise RuntimeError("crash after compatibility materialization")

            descriptor = {"kind": "test-cache", "path": str(materialized)}
            with self.assertRaisesRegex(RuntimeError, "crash after"):
                kernel.complete(terminal_callback=callback, materialization=descriptor)
            kernel.complete(terminal_callback=callback, materialization=descriptor)
            kernel.complete(terminal_callback=callback, materialization=descriptor)
            self.assertEqual([1, 2], calls)
            events = [item for item in ledger.read() if item["transaction_id"] == transaction.transaction_id]
            self.assertEqual(1, sum(item["event_type"] == "TransactionCompleted" for item in events))
            self.assertEqual(1, sum(
                item["event_type"] == "CheckpointRecorded"
                and item["payload"].get("checkpoint") == "compatibility_materialization_pending"
                for item in events
            ))
            self.assertEqual(1, sum(
                item["event_type"] == "CheckpointRecorded"
                and item["payload"].get("checkpoint") == "compatibility_materialization_acknowledged"
                for item in events
            ))

    def test_recovered_lease_archive_rejects_symlink_and_hardlink_targets(self):
        repository_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ); environment["PYTHONPATH"] = str(repository_root / "src")
        child = "\n".join((
            "import json, sys",
            "from pathlib import Path",
            "from development_conveyor.registry import Project",
            "from development_conveyor.repository import RepositoryInspector",
            "from development_conveyor.ledger import EvidenceLedger",
            "from development_conveyor.projection import ProjectionEngine",
            "from development_conveyor.workflow_lease import WorkflowWriterLease",
            "from development_conveyor.kernel import WorkflowKernel",
            "from development_conveyor.contracts import WorkflowType, MutationPolicy",
            "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
            "i=RepositoryInspector(p.repository).identity(); l=EvidenceLedger(Path(sys.argv[2]), project_id=p.project_id, repository_identity=i['repository_id'], repository_path_fingerprint=i['path_fingerprint'])",
            "k=WorkflowKernel(project=p, ledger=l, projection=ProjectionEngine(l), lease=WorkflowWriterLease(p.repository / '.factory/locks/writer.json'))",
            "k.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone='M0', feature_id='F001', run_id='archive-owner', policy=MutationPolicy(('app.txt',), commit_subject='F001'), transaction_id='archive-attack')",
            "k.acquire_lease(); k.capture_snapshot()",
        ))
        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                exclude = repository / ".git/info/exclude"
                exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n", encoding="utf-8")
                value = {
                    key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                    for key, item in project.__dict__.items()
                }
                ledger_path = root / "controller/evidence.jsonl"
                subprocess.run(
                    ["python3", "-c", child, json.dumps(value), str(ledger_path)],
                    cwd=repository_root, env=environment, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                identity = RepositoryInspector(repository).identity()
                ledger = EvidenceLedger(
                    ledger_path, project_id="synthetic",
                    repository_identity=identity["repository_id"],
                    repository_path_fingerprint=identity["path_fingerprint"],
                )
                lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
                outside = root / "outside"; outside.mkdir()
                sentinel = outside / "sentinel.txt"; sentinel.write_text("unchanged\n", encoding="utf-8")
                archive_root = ledger_path.parent / "recovered-leases"
                if attack == "symlink":
                    archive_root.symlink_to(outside, target_is_directory=True)
                else:
                    archive_root.mkdir()
                    external = outside / "lease.json"
                    external.write_bytes(lease.path.read_bytes())
                    archive_name = "writer.recovered-" + hashlib.sha256(b"archive-attack").hexdigest() + ".json"
                    os.link(external, archive_root / archive_name)
                planner = RecoveryPlanner(
                    project=project, ledger=ledger,
                    projection=ProjectionEngine(ledger, ledger_path.parent / "projection.json"),
                    lease=lease,
                )
                self.assertTrue(planner.inspect()["recoverable"])
                with self.assertRaisesRegex(Exception, "archive"):
                    planner.apply()
                self.assertEqual("unchanged\n", sentinel.read_text(encoding="utf-8"))
                self.assertTrue(lease.path.exists())

    def test_process_death_after_human_terminal_replays_materialization_after_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            exclude = repository / ".git/info/exclude"
            exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n", encoding="utf-8")
            value = {
                key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                for key, item in project.__dict__.items()
            }
            ledger_path = root / "controller/evidence.jsonl"
            cache_path = root / "controller/projection.json"
            materialized = root / "human-resolution.json"
            descriptor = {
                "kind": "human_decision_resolution", "resolution_id": "resolution-1",
                "path": str(materialized),
            }
            child = "\n".join((
                "import json, os, sys",
                "from pathlib import Path",
                "from development_conveyor.registry import Project",
                "from development_conveyor.repository import RepositoryInspector",
                "from development_conveyor.ledger import EvidenceLedger",
                "from development_conveyor.projection import ProjectionEngine",
                "from development_conveyor.workflow_lease import WorkflowWriterLease",
                "from development_conveyor.kernel import WorkflowKernel",
                "from development_conveyor.command_authority import CommandAuthority",
                "from development_conveyor.contracts import WorkflowType, MutationPolicy, SessionResultEnvelope",
                "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
                "i=RepositoryInspector(p.repository).identity(); l=EvidenceLedger(Path(sys.argv[2]), project_id=p.project_id, repository_identity=i['repository_id'], repository_path_fingerprint=i['path_fingerprint'])",
                "k=WorkflowKernel(project=p, ledger=l, projection=ProjectionEngine(l, Path(sys.argv[3])), lease=WorkflowWriterLease(p.repository / '.factory/locks/writer.json'))",
                "t=k.begin(workflow_type=WorkflowType.HUMAN_DECISION_RESOLUTION, milestone='M0', feature_id=None, run_id='resolution-1', policy=MutationPolicy((), commit_subject='resolve'), transaction_id='human-terminal-replay')",
                "k.acquire_lease(); k.capture_snapshot(); k.session_launched('resolution-session')",
                "k.accept_result(SessionResultEnvelope.from_dict({'schema_version':1,'workflow_type':'human_decision_resolution','classification':'HUMAN_GATE_RESOLVED','project_id':p.project_id,'repository_identity':i['repository_id'],'transaction_id':t.transaction_id,'run_id':t.run_id,'session_id':'resolution-session','starting_branch':t.starting_branch,'starting_commit':t.starting_head,'current_commit':t.starting_head,'feature_id':None,'changed_paths':[],'evidence':{'gate_id':'gate-1'},'next_state':'feature_ready'}))",
                "k.record_file_mutation_boundary(); k.validate(authority=CommandAuthority(), command_results=()); k.finalize()",
                "k.complete(evidence={'human_gate_resolution_id':'resolution-1'}, terminal_callback=lambda projection: os._exit(17), materialization=json.loads(sys.argv[4]))",
            ))
            environment = dict(os.environ); environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            crashed = subprocess.run(
                ["python3", "-c", child, json.dumps(value), str(ledger_path), str(cache_path), json.dumps(descriptor)],
                cwd=Path(__file__).resolve().parents[1], env=environment, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(17, crashed.returncode, crashed.stderr)
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                ledger_path, project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            projection = ProjectionEngine(ledger, cache_path)
            lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
            planner = RecoveryPlanner(project=project, ledger=ledger, projection=projection, lease=lease)
            plan = planner.inspect()
            self.assertEqual("terminal_before_lease_release", plan["classification"])
            self.assertTrue(plan["recoverable"])
            planner.apply()
            restored = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease)
            restored.restore("human-terminal-replay")

            def materialize(_projection):
                materialized.write_text('{"resolution":"applied"}\n', encoding="utf-8")

            restored.complete(
                evidence={"human_gate_resolution_id": "resolution-1"},
                terminal_callback=materialize, materialization=descriptor,
            )
            self.assertEqual('{"resolution":"applied"}\n', materialized.read_text(encoding="utf-8"))
            events = [item for item in ledger.read() if item["transaction_id"] == "human-terminal-replay"]
            self.assertEqual(1, sum(item["event_type"] == "TransactionCompleted" for item in events))
            self.assertEqual(1, sum(
                item["event_type"] == "CheckpointRecorded"
                and item["payload"].get("checkpoint") == "compatibility_materialization_acknowledged"
                for item in events
            ))
            self.assertFalse(lease.path.exists())

    def test_duplicate_cherry_pick_patch_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root / "app")
            git(repository, "switch", "-c", "codex/f001")
            (repository / "app.txt").write_text("accepted\n", encoding="utf-8")
            git(repository, "add", "app.txt"); git(repository, "commit", "-m", "F001")
            accepted = git(repository, "rev-parse", "HEAD")
            git(repository, "switch", "codex/m0-foundation")
            kernel, transaction, ledger, projection, lease, identity = self.start_kernel(
                root, repository, project, workflow=WorkflowType.MILESTONE_INTEGRATION,
                policy=MutationPolicy(("app.txt",), commit_subject="integrate F001"), run_id="integrate-1",
            )
            self.accept_kernel(kernel, transaction, identity, classification="INTEGRATED", changed_paths=["app.txt"], next_state="feature_integrated")
            kernel.prepare_integration_changes(accepted); kernel.record_file_mutation_boundary(); kernel.validate(authority=CommandAuthority(), command_results=())
            integrated = kernel.finalize(); kernel.complete(evidence={"accepted_feature_commit": accepted, "integrated_commit": integrated, "integration_status": "passed"})
            head = git(repository, "rev-parse", "HEAD")
            second = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease)
            second_transaction = second.begin(workflow_type=WorkflowType.MILESTONE_INTEGRATION, milestone="M0", feature_id="F001", run_id="integrate-2", policy=MutationPolicy(("app.txt",), commit_subject="integrate F001 again"))
            second.acquire_lease(); second.capture_snapshot(); second.session_launched("integrate-2-session")
            self.accept_kernel(second, second_transaction, identity, classification="INTEGRATED", changed_paths=["app.txt"], next_state="feature_integrated")
            with self.assertRaisesRegex(TransactionError, "already"):
                second.prepare_integration_changes(accepted)
            self.assertEqual(head, git(repository, "rev-parse", "HEAD"))
    @staticmethod
    def repository_immutability_snapshot(
        repository: Path, *, controller_root: Path | None = None,
        project_id: str | None = None,
    ) -> dict[str, object]:
        def observe(*arguments: str) -> str:
            environment = dict(os.environ)
            environment["GIT_OPTIONAL_LOCKS"] = "0"
            result = subprocess.run(
                ["git", *arguments], cwd=repository, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                env=environment,
            )
            return result.stdout.strip()

        inspector = RepositoryInspector(repository)
        index = inspector.common_git_dir / "index"
        index_lock = inspector.common_git_dir / "index.lock"
        writer_lock = repository / ".factory/locks/writer.json"
        def bytes_evidence(path: Path) -> dict[str, object]:
            return {
                "exists": path.exists() or path.is_symlink(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            }
        observed = {
            "head": observe("rev-parse", "HEAD"),
            "tree": observe("rev-parse", "HEAD^{tree}"),
            "refs": observe("show-ref"),
            "status": observe("status", "--porcelain=v2", "--branch"),
            "worktrees": observe("worktree", "list", "--porcelain"),
            "git_operations": inspector.git_operation_state(),
            "writer_lock_bytes": bytes_evidence(writer_lock),
            "index_lock_bytes": bytes_evidence(index_lock),
        }
        observed["index"] = bytes_evidence(index)
        if controller_root is not None and project_id is not None:
            state_root = controller_root / "state/projects" / project_id
            observed["controller_ledger_bytes"] = bytes_evidence(state_root / "evidence-ledger.jsonl")
            observed["controller_cache_bytes"] = bytes_evidence(state_root / "projection-cache.json")
        return observed

    def test_consistency_checker_valid_recoverable_corrupt_and_unsafe(self):
        with tempfile.TemporaryDirectory() as temporary:
            simulator = DeterministicLifecycleSimulator(Path(temporary))
            simulator.run(cycles=1)
            observer_engine = CycleEngine(
                controller_configuration(Path(temporary), simulator.project),
                launcher=object(),
            )
            checker = ConsistencyChecker(
                controller_root=simulator.controller,
                project=simulator.project,
                planner_observer=lambda: observer_engine.project_plan(simulator.project),
            )
            self.assertEqual("CONSISTENT", checker.check()["classification"])

            cache = simulator.controller / "state/projects/synthetic/projection-cache.json"
            cache.unlink()
            recoverable = checker.check()
            self.assertEqual("RECOVERABLE_INCONSISTENCY", recoverable["classification"])
            self.assertIn("projection_cache_agreement", {
                item["invariant"] for item in recoverable["failed_invariants"]
            })

            checker.projection.rebuild(persist_cache=True)
            merge_head = simulator.repository / ".git/MERGE_HEAD"
            merge_head.write_text("0" * 40 + "\n", encoding="utf-8")
            unsafe = checker.check()
            self.assertEqual("UNSAFE_REPOSITORY_STATE", unsafe["classification"])
            merge_head.unlink()

            ledger_path = simulator.controller / "state/projects/synthetic/evidence-ledger.jsonl"
            ledger_path.write_bytes(ledger_path.read_bytes()[:-1])
            corrupt = checker.check()
            self.assertEqual("CORRUPT_EVIDENCE", corrupt["classification"])

    def test_consistency_checker_resolves_configured_milestone_alias_for_queue_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["milestones"][0]["id"] = "phase-0"
            queue["features"][0]["milestone"] = "phase-0"
            queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: use canonical phase milestone")
            aliased = replace(project, active_milestone="P0")

            result = ConsistencyChecker(
                controller_root=root / "controller", project=aliased
            ).check()
            invariants = {
                item["invariant"]: item for item in result["invariants"]
            }
            self.assertTrue(invariants["queue_project_milestone_agreement"]["passed"])
            self.assertTrue(invariants["queue_feature_agreement"]["passed"])
            self.assertEqual(
                "phase-0",
                invariants["queue_feature_agreement"]["evidence"]["resolved_queue_milestone"],
            )

    def test_kernel_rejects_dirty_new_transaction_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / "app.txt").write_text("dirty\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                root / "controller/ledger.jsonl", project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            kernel = WorkflowKernel(
                project=project, ledger=ledger, projection=ProjectionEngine(ledger),
                lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
            )
            with self.assertRaisesRegex(TransactionError, "clean repository"):
                kernel.begin(
                    workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0", feature_id="F001",
                    run_id="dirty-start", policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                )

    def test_latest_lease_bound_session_is_the_only_acceptable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / ".git/info/exclude").write_text(".factory/locks/writer.json\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(root / "controller/ledger.jsonl", project_id="synthetic", repository_identity=identity["repository_id"], repository_path_fingerprint=identity["path_fingerprint"])
            kernel = WorkflowKernel(project=project, ledger=ledger, projection=ProjectionEngine(ledger), lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"))
            transaction = kernel.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0", feature_id="F001", run_id="session-order", policy=MutationPolicy((), commit_subject="F001"))
            kernel.acquire_lease(); kernel.capture_snapshot()
            kernel.session_launched("S1"); kernel.session_launched("S2")
            envelope = SessionResultEnvelope.from_dict({
                "schema_version": 1, "workflow_type": "feature_execution", "classification": "FEATURE_ACCEPTED",
                "project_id": "synthetic", "repository_identity": identity["repository_id"],
                "transaction_id": transaction.transaction_id, "run_id": "session-order", "session_id": "S1",
                "starting_branch": transaction.starting_branch, "starting_commit": transaction.starting_head,
                "current_commit": transaction.starting_head, "feature_id": "F001", "changed_paths": [],
                "evidence": {}, "next_state": "feature_accepted",
            })
            with self.assertRaisesRegex(TransactionError, "session_id"):
                kernel.accept_result(envelope)

    def test_new_file_commit_after_commit_interruption_is_exactly_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / ".git/info/exclude").write_text(".factory/locks/writer.json\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(root / "controller/ledger.jsonl", project_id="synthetic", repository_identity=identity["repository_id"], repository_path_fingerprint=identity["path_fingerprint"])
            projection = ProjectionEngine(ledger, root / "controller/projection.json")
            lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")

            def interrupt(boundary, transaction):
                if boundary == "after_commit":
                    raise RuntimeError("scripted after_commit interruption")

            kernel = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease, interruption_hook=interrupt)
            transaction = kernel.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0", feature_id="F001", run_id="new-file", policy=MutationPolicy(("new.txt",), allow_untracked=True, commit_subject="F001: new file"))
            kernel.acquire_lease(); kernel.capture_snapshot(); kernel.session_launched("new-file-session")
            (repository / "new.txt").write_text("new content\n", encoding="utf-8")
            kernel.accept_result(SessionResultEnvelope.from_dict({
                "schema_version": 1, "workflow_type": "feature_execution", "classification": "FEATURE_ACCEPTED",
                "project_id": "synthetic", "repository_identity": identity["repository_id"],
                "transaction_id": transaction.transaction_id, "run_id": "new-file", "session_id": "new-file-session",
                "starting_branch": transaction.starting_branch, "starting_commit": transaction.starting_head,
                "current_commit": transaction.starting_head, "feature_id": "F001", "changed_paths": ["new.txt"],
                "evidence": {}, "next_state": "feature_accepted",
            }))
            kernel.record_file_mutation_boundary(); kernel.validate(authority=CommandAuthority(), command_results=())
            with self.assertRaisesRegex(RuntimeError, "after_commit"):
                kernel.finalize()
            plan = RecoveryPlanner(project=project, ledger=ledger, projection=projection, lease=lease).inspect()
            self.assertEqual("commit_succeeded_before_evidence", plan["classification"])
            self.assertTrue(plan["recoverable"])

    def test_dead_process_preterminal_lease_is_taken_over_for_exact_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / ".git/info/exclude").write_text(".factory/locks/writer.json\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger_path = root / "controller/ledger.jsonl"
            project_value = {
                key: (str(value) if isinstance(value, Path) else list(value) if isinstance(value, tuple) else value)
                for key, value in project.__dict__.items()
            }
            child = r'''
import json, sys
from pathlib import Path
from development_conveyor.registry import Project
from development_conveyor.repository import RepositoryInspector
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.workflow_lease import WorkflowWriterLease
from development_conveyor.kernel import WorkflowKernel
from development_conveyor.contracts import WorkflowType, MutationPolicy
value=json.loads(sys.argv[1]); value["repository"]=Path(value["repository"]); value["human_gates"]=tuple(value["human_gates"])
project=Project(**value); identity=RepositoryInspector(project.repository).identity()
ledger=EvidenceLedger(Path(sys.argv[2]), project_id=project.project_id, repository_identity=identity["repository_id"], repository_path_fingerprint=identity["path_fingerprint"])
kernel=WorkflowKernel(project=project, ledger=ledger, projection=ProjectionEngine(ledger), lease=WorkflowWriterLease(project.repository / ".factory/locks/writer.json"))
kernel.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0", feature_id="F001", run_id="dead-child", policy=MutationPolicy((), commit_subject="F001"), transaction_id="dead-process-transaction")
kernel.acquire_lease(); kernel.capture_snapshot()
'''
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            subprocess.run(
                ["python3", "-c", child, json.dumps(project_value), str(ledger_path)],
                cwd=Path(__file__).resolve().parents[1], env=environment, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            ledger = EvidenceLedger(
                ledger_path, project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            projection = ProjectionEngine(ledger, root / "controller/projection.json")
            lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
            planner = RecoveryPlanner(project=project, ledger=ledger, projection=projection, lease=lease)
            self.assertEqual("resume", planner.inspect()["classification"])
            applied = planner.apply()
            self.assertEqual("resume_exact_transaction", applied["action"])
            restored = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease)
            restored.restore("dead-process-transaction")
            restored.session_launched("continued-session")
            self.assertEqual("continued-session", lease.read().session_id)
            restored.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="TERMINAL_FEATURE_FAILURE", next_state="validation_failed",
            )

    def test_concurrent_process_contender_cannot_append_second_transaction_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            exclude = repository / ".git/info/exclude"
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n",
                encoding="utf-8",
            )
            value = {
                key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                for key, item in project.__dict__.items()
            }
            ledger_path = root / "controller/ledger.jsonl"
            child = "\n".join((
                "import json, sys, time",
                "from pathlib import Path",
                "from development_conveyor.registry import Project",
                "from development_conveyor.repository import RepositoryInspector",
                "from development_conveyor.ledger import EvidenceLedger",
                "from development_conveyor.projection import ProjectionEngine",
                "from development_conveyor.workflow_lease import WorkflowWriterLease",
                "from development_conveyor.kernel import WorkflowKernel",
                "from development_conveyor.contracts import WorkflowType, MutationPolicy",
                "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
                "i=RepositoryInspector(p.repository).identity()",
                "l=EvidenceLedger(Path(sys.argv[2]), project_id=p.project_id, repository_identity=i['repository_id'], repository_path_fingerprint=i['path_fingerprint'])",
                "k=WorkflowKernel(project=p, ledger=l, projection=ProjectionEngine(l), lease=WorkflowWriterLease(p.repository / '.factory/locks/writer.json'))",
                "k.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone='M0', feature_id='F001', run_id='owner', policy=MutationPolicy(('app.txt',), commit_subject='F001'))",
                "print('READY', flush=True); time.sleep(10)",
            ))
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            owner = subprocess.Popen(
                ["python3", "-c", child, json.dumps(value), str(ledger_path)],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual("READY", owner.stdout.readline().strip())
                identity = RepositoryInspector(repository).identity()
                ledger = EvidenceLedger(
                    ledger_path, project_id="synthetic",
                    repository_identity=identity["repository_id"],
                    repository_path_fingerprint=identity["path_fingerprint"],
                )
                contender = WorkflowKernel(
                    project=project, ledger=ledger, projection=ProjectionEngine(ledger),
                    lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
                )
                with self.assertRaises(Exception):
                    contender.begin(
                        workflow_type=WorkflowType.FEATURE_EXECUTION, milestone="M0",
                        feature_id="F001", run_id="contender",
                        policy=MutationPolicy(("app.txt",), commit_subject="F001"),
                    )
                starts = [event for event in ledger.read() if event["event_type"] == "TransactionStarted"]
                self.assertEqual(1, len(starts))
                self.assertEqual(
                    starts[0]["transaction_id"],
                    ProjectionEngine(ledger).rebuild(persist_cache=False)["active_transaction"],
                )
            finally:
                owner.terminate()
                owner.wait(timeout=5)
                if owner.stdout is not None:
                    owner.stdout.close()
                if owner.stderr is not None:
                    owner.stderr.close()

    def test_orphaned_prestart_lease_blocks_live_owner_and_recovers_dead_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            exclude = repository / ".git/info/exclude"
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n",
                encoding="utf-8",
            )
            value = {
                key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                for key, item in project.__dict__.items()
            }
            ledger_path = root / "controller/evidence.jsonl"
            cache_path = root / "controller/projection.json"
            child = "\n".join((
                "import json, sys, time",
                "from pathlib import Path",
                "from development_conveyor.registry import Project",
                "from development_conveyor.repository import RepositoryInspector",
                "from development_conveyor.workflow_lease import WorkflowWriterLease",
                "from development_conveyor.contracts import WorkflowType, MutationPolicy, WORKFLOW_LEASE",
                "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
                "i=RepositoryInspector(p.repository); identity=i.identity()",
                "WorkflowWriterLease(p.repository / '.factory/locks/writer.json').acquire(lease_type=WORKFLOW_LEASE[WorkflowType.FEATURE_EXECUTION], repository_identity=identity['repository_id'], repository_path_fingerprint=identity['path_fingerprint'], project_id=p.project_id, transaction_id='orphan-prestart', workflow_type=WorkflowType.FEATURE_EXECUTION, milestone='M0', feature_id='F001', starting_branch=i.current_branch, starting_head=i.head, run_id='orphan-run', session_id=None, policy=MutationPolicy(('app.txt',), commit_subject='F001'))",
                "print('READY', flush=True); time.sleep(30)",
            ))
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            owner = subprocess.Popen(
                ["python3", "-c", child, json.dumps(value)],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual("READY", owner.stdout.readline().strip())
                identity = RepositoryInspector(repository).identity()
                ledger = EvidenceLedger(
                    ledger_path, project_id="synthetic",
                    repository_identity=identity["repository_id"],
                    repository_path_fingerprint=identity["path_fingerprint"],
                )
                projection = ProjectionEngine(ledger, cache_path)
                lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
                live = RecoveryPlanner(
                    project=project, ledger=ledger, projection=projection, lease=lease,
                ).inspect()
                self.assertEqual("orphaned_prestart_lease", live["classification"])
                self.assertFalse(live["recoverable"])
            finally:
                owner.terminate()
                owner.wait(timeout=5)
                if owner.stdout is not None:
                    owner.stdout.close()
                if owner.stderr is not None:
                    owner.stderr.close()

            planner = RecoveryPlanner(
                project=project, ledger=ledger, projection=projection, lease=lease,
            )
            dead = planner.inspect()
            self.assertEqual("orphaned_prestart_lease", dead["classification"])
            self.assertTrue(dead["recoverable"])
            applied = planner.apply()
            self.assertTrue(applied["mutation_performed"])
            self.assertFalse(lease.path.exists())
            events = ledger.read()
            starts = [item for item in events if item["event_type"] == "TransactionStarted"]
            self.assertEqual(1, len(starts))
            self.assertEqual("recovery", starts[0]["workflow_type"])
            self.assertEqual("orphan-prestart", starts[0]["payload"]["recovered_transaction_id"])
            self.assertEqual(1, sum(
                item["event_type"] == "TransactionCompleted"
                and item["transaction_id"] == starts[0]["transaction_id"]
                for item in events
            ))

    def test_whole_phase_queue_bridge_has_one_lease_and_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / ".git/info/exclude").write_text(
                ".factory/locks/writer.json\n", encoding="utf-8"
            )
            inspector = RepositoryInspector(repository)
            identity = inspector.identity()
            state = root / "controller/state/projects/synthetic"
            ledger = EvidenceLedger(
                state / "evidence-ledger.jsonl", project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            kernel = WorkflowKernel(
                project=project, ledger=ledger,
                projection=ProjectionEngine(ledger, state / "projection-cache.json"),
                lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
            )
            adapter = QueueReconciliationAdapter(
                allowed_paths=("docs/FEATURE_QUEUE.yaml",),
                commit_subject="factory: reconcile queue",
                next_state="feature_ready",
            )

            def execute(transaction):
                queue_path = repository / "docs/FEATURE_QUEUE.yaml"
                queue_path.write_text(queue_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                return PhaseExecution(SessionResultEnvelope.from_dict({
                    "schema_version": 1,
                    "workflow_type": "queue_reconciliation",
                    "classification": "RECONCILED_READY_WORK",
                    "project_id": "synthetic",
                    "repository_identity": identity["repository_id"],
                    "transaction_id": transaction.transaction_id,
                    "run_id": "bridge-run",
                    "session_id": "queue-session",
                    "starting_branch": transaction.starting_branch,
                    "starting_commit": transaction.starting_head,
                    "current_commit": transaction.starting_head,
                    "feature_id": None,
                    "changed_paths": ["docs/FEATURE_QUEUE.yaml"],
                    "evidence": {"adapter": "queue_reconciliation"},
                    "next_state": "feature_ready",
                }))

            KernelWorkflowBridge(kernel).run(
                adapter=adapter, run_id="bridge-run", milestone="M0", feature_id=None,
                session_id="queue-session", execute=execute,
            )
            events = ledger.read()
            self.assertEqual(1, sum(item["event_type"] == "LeaseAcquired" for item in events))
            self.assertEqual(1, sum(item["event_type"] == "TransactionCompleted" for item in events))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_phase_bridge_terminalizes_exit_zero_human_and_nonzero_retryable(self):
        cases = (
            ("HUMAN_DECISION_REQUIRED", "human_decision_required", 0, "HumanGateRaised"),
            ("RETRYABLE_PLANNING_FAILURE", "validation_failed", 9, "TransactionBlocked"),
        )
        for classification, next_state, returncode, event_type in cases:
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, project = synthetic_repository(root / "app")
                (repository / ".git/info/exclude").write_text(
                    ".factory/locks/writer.json\n", encoding="utf-8"
                )
                identity = RepositoryInspector(repository).identity()
                state = root / "controller/state/projects/synthetic"
                ledger = EvidenceLedger(
                    state / "evidence-ledger.jsonl", project_id="synthetic",
                    repository_identity=identity["repository_id"],
                    repository_path_fingerprint=identity["path_fingerprint"],
                )
                kernel = WorkflowKernel(
                    project=project, ledger=ledger,
                    projection=ProjectionEngine(ledger, state / "projection-cache.json"),
                    lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
                )
                adapter = QueueReconciliationAdapter(
                    allowed_paths=(), commit_subject="factory: queue outcome", next_state="feature_ready",
                )

                def execute(transaction):
                    return PhaseExecution(SessionResultEnvelope.from_dict({
                        "schema_version": 1, "workflow_type": "queue_reconciliation",
                        "classification": classification, "project_id": "synthetic",
                        "repository_identity": identity["repository_id"],
                        "transaction_id": transaction.transaction_id, "run_id": "bridge-terminal",
                        "session_id": "terminal-session", "starting_branch": transaction.starting_branch,
                        "starting_commit": transaction.starting_head, "current_commit": transaction.starting_head,
                        "feature_id": None, "changed_paths": [],
                        "evidence": {"human_decision": {"reason": "scripted"}},
                        "next_state": next_state,
                    }), process_returncode=returncode)

                result = KernelWorkflowBridge(kernel).run(
                    adapter=adapter, run_id="bridge-terminal", milestone="M0", feature_id=None,
                    session_id="terminal-session", execute=execute,
                )
                events = ledger.read()
                self.assertEqual(1, sum(item["event_type"] == event_type for item in events))
                self.assertEqual(next_state, result["projection"]["current_state"])
                self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_recovery_bridge_records_transaction_supersession(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root / "app")
            (repository / ".git/info/exclude").write_text(".factory/locks/writer.json\n", encoding="utf-8")
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                root / "controller/ledger.jsonl", project_id="synthetic",
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            kernel = WorkflowKernel(
                project=project, ledger=ledger, projection=ProjectionEngine(ledger),
                lease=WorkflowWriterLease(repository / ".factory/locks/writer.json"),
            )
            adapter = RecoveryAdapter(allowed_paths=(), commit_subject="recovery", next_state="feature_ready")

            def execute(transaction):
                return PhaseExecution(SessionResultEnvelope.from_dict({
                    "schema_version": 1, "workflow_type": "recovery",
                    "classification": "TRANSACTION_SUPERSEDED", "project_id": "synthetic",
                    "repository_identity": identity["repository_id"],
                    "transaction_id": transaction.transaction_id, "run_id": "supersede-run",
                    "session_id": "supersede-session", "starting_branch": transaction.starting_branch,
                    "starting_commit": transaction.starting_head, "current_commit": transaction.starting_head,
                    "feature_id": None, "changed_paths": [], "evidence": {},
                    "next_state": "feature_ready",
                }))

            KernelWorkflowBridge(kernel).run(
                adapter=adapter, run_id="supersede-run", milestone="M0", feature_id=None,
                session_id="supersede-session", execute=execute,
            )
            self.assertEqual(1, sum(
                event["event_type"] == "TransactionSuperseded" for event in ledger.read()
            ))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_real_case_manager_dry_run_projection(self):
        root = Path(__file__).resolve().parents[1]
        configuration = __import__("development_conveyor.config", fromlist=["load_configuration"]).load_configuration(root)
        project = __import__("development_conveyor.registry", fromlist=["ProjectRegistry"]).ProjectRegistry(configuration).get("case-manager")
        before = self.repository_immutability_snapshot(project.repository, controller_root=root, project_id=project.project_id)
        result = LegacyStateMigrator(controller_root=root, project=project).plan()
        after = self.repository_immutability_snapshot(project.repository, controller_root=root, project_id=project.project_id)
        projection = result["projected_state_after"]
        self.assertEqual("feature_ready", projection["current_state"])
        self.assertEqual("P0-003", projection["current_feature"])
        self.assertFalse(projection["old_session_resume"])
        self.assertEqual("codex/p0-foundation", projection["milestone_branch"])
        self.assertEqual("f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0", projection["selected_feature_starting_commit"])
        self.assertEqual("f2a11cf5-c3af-4737-89c2-96b017155d97", projection["transactions"][0]["run_id"])
        self.assertEqual("019f77e1-c551-7f00-9409-2fff9f6ee79b", projection["transactions"][0]["session_id"])
        self.assertEqual("f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0", projection["transactions"][0]["result_commit"])
        historical = next(item for item in projection["historical_integration_outcomes"] if item["feature_id"] == "P0-001")
        self.assertEqual({
            "accepted_commit": "dc4fe99a562d253dfba6b8eac1d2b3c81fc49b19",
            "classification": "INTEGRATED", "classification_source": "corroborated",
            "feature_id": "P0-001",
            "integrated_commit": "1108649b63052ed03946cd840517f0b611c867bc",
            "terminal_head": "0f43ad23a3820ccd3e27f4a6a475e42dc70957bc",
        }, historical)
        consistency = ConsistencyChecker(
            controller_root=root, project=project
        ).check()
        self.assertEqual("RECOVERABLE_INCONSISTENCY", consistency["classification"])
        self.assertEqual(
            ["execution_plan_projection_agreement"],
            [item["invariant"] for item in consistency["failed_invariants"]],
        )
        queue_agreement = next(
            item for item in consistency["invariants"]
            if item["invariant"] == "queue_feature_agreement"
        )
        self.assertTrue(queue_agreement["passed"])
        self.assertEqual("P0", queue_agreement["evidence"]["configured_milestone"])
        self.assertEqual(
            "phase-0", queue_agreement["evidence"]["resolved_queue_milestone"]
        )
        self.assertEqual("feature_cycle", projection["allowed_next_action"])
        self.assertIsNone(projection["human_gate"])
        self.assertFalse(any(
            item.get("feature_id") == "P0-001"
            for item in result["transactions_that_would_be_reconstructed"]
        ))
        self.assertFalse(result["application_repository_written"])
        self.assertEqual([], result["application_git_mutations"])
        self.assertEqual(before, after)

    def test_real_interview_dry_run_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration, project, _ = self.immutable_migration_fixture(
                root,
                project_id="interview-companion",
                feature_id="F005",
                integration_pending=True,
                exact_interview_identity=True,
                terminal_failure=True,
            )
            before = self.repository_immutability_snapshot(
                project.repository,
                controller_root=configuration.root,
                project_id=project.project_id,
            )
            migrator = LegacyStateMigrator(
                controller_root=configuration.root, project=project
            )
            original_rev_parse = migrator.inspector.rev_parse
            immutable_inspector = mock.Mock(wraps=migrator.inspector)
            immutable_inspector.head = self.INTERVIEW_MILESTONE_START
            immutable_inspector.current_branch = "codex/m0-foundation"
            immutable_inspector.is_clean = True

            def immutable_ref(ref, *, check=True):
                if ref == self.INTERVIEW_ACCEPTED_COMMIT:
                    return self.INTERVIEW_ACCEPTED_COMMIT
                if ref == "codex/F005-persistent-data-store":
                    return self.INTERVIEW_ACCEPTED_COMMIT
                if ref == "codex/m0-foundation":
                    return self.INTERVIEW_MILESTONE_START
                return original_rev_parse(ref, check=check)

            immutable_inspector.rev_parse.side_effect = immutable_ref
            migrator.inspector = immutable_inspector
            result = migrator.plan()
            after = self.repository_immutability_snapshot(
                project.repository,
                controller_root=configuration.root,
                project_id=project.project_id,
            )
            before_projection = result["projected_state_before"]
            projection = result["projected_state_after"]
            failed = next(
                item for item in before_projection["transactions"]
                if item["transaction_id"] == self.INTERVIEW_FAILED_TRANSACTION
            )
            self.assertEqual("validation_failed", before_projection["current_state"])
            self.assertIsNone(before_projection["active_transaction"])
            self.assertEqual("terminal_failure", failed["state"])
            self.assertEqual("VALIDATION_FAILED", failed["terminal_classification"])
            self.assertEqual([self.INTERVIEW_FAILED_SESSION], failed["session_ids"])
            self.assertFalse(before_projection["session_resume_eligible"])

            self.assertEqual("integration_ready", projection["current_state"])
            self.assertEqual("F005", projection["current_feature"])
            self.assertEqual(
                self.INTERVIEW_ACCEPTED_COMMIT,
                projection["accepted_feature_commit"],
            )
            self.assertEqual("superseded", projection["legacy_cycle_classification"])
            self.assertEqual(
                self.INTERVIEW_MILESTONE_START,
                projection["selected_feature_starting_commit"],
            )
            self.assertEqual(
                "codex/F005-persistent-data-store", projection["feature_branch"]
            )
            self.assertEqual("codex/m0-foundation", projection["milestone_branch"])
            self.assertEqual("milestone_integration", projection["allowed_next_action"])
            self.assertEqual("pending", projection["integration_status"])
            self.assertEqual([], projection["historical_integration_outcomes"])
            self.assertEqual(
                self.INTERVIEW_ACCEPTED_COMMIT,
                immutable_inspector.rev_parse("codex/F005-persistent-data-store"),
            )
            self.assertEqual(
                self.INTERVIEW_MILESTONE_START,
                immutable_inspector.rev_parse("codex/m0-foundation"),
            )
            queue = FeatureQueue.from_location(
                project.repository, project.queue_location
            )
            queued = queue.feature("F005")
            self.assertEqual("integration_pending", queued["status"])
            self.assertEqual(self.INTERVIEW_ACCEPTED_COMMIT, queued["accepted_commit"])
            self.assertEqual(
                self.INTERVIEW_MILESTONE_START,
                queued["integration_base_commit"],
            )
            self.assertFalse(projection["old_session_resume"])
            self.assertFalse(projection["session_resume_eligible"])
            self.assertFalse(any(
                item.get("workflow_type") == "milestone_integration"
                for item in result["transactions_that_would_be_reconstructed"]
            ))
            self.assertFalse(result["application_repository_written"])
            self.assertEqual([], result["application_git_mutations"])
            self.assertEqual(before, after)

    def test_post_migration_real_fixtures_route_only_fresh_kernel_actions(self):
        expectations = {
            "case-manager": (
                "feature_cycle", "P0-003", False, "_execute_feature"
            ),
            "interview-companion": (
                "milestone_integration", "F005", True,
                "_execute_projected_integration",
            ),
        }
        for project_id, (action, feature_id, integration_pending, method) in expectations.items():
            with self.subTest(project_id=project_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                controller, project, _ = self.immutable_migration_fixture(
                    root,
                    project_id=project_id,
                    feature_id=feature_id,
                    integration_pending=integration_pending,
                )
                LegacyStateMigrator(
                    controller_root=controller.root, project=project
                ).apply()
                launcher = mock.Mock()
                launcher.plan.side_effect = AssertionError(
                    "migration routing fixture must not render a prompt"
                )
                launcher.launch.side_effect = AssertionError(
                    "migration routing fixture must not launch a model"
                )
                engine = CycleEngine(controller, launcher=launcher)
                with mock.patch.object(
                    engine, method,
                    return_value={"outcome": "routed", "feature": feature_id},
                ) as routed, mock.patch.object(
                    engine, "resume_project",
                    side_effect=AssertionError("stale session resume must not run"),
                ):
                    result = engine.run_project(project, "milestone")
                self.assertEqual("routed", result["outcome"])
                routed.assert_called_once()
                launcher.plan.assert_not_called()
                launcher.launch.assert_not_called()
                authoritative = engine._authoritative_projection(project)
                self.assertEqual(action, authoritative["allowed_next_action"])
                self.assertEqual(feature_id, authoritative["current_feature"])

    def test_applied_synthetic_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            controller = root / "controller"
            before = (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "status", "--porcelain"),
                git(repository, "show-ref"),
            )
            migrator = LegacyStateMigrator(controller_root=controller, project=project)
            first = migrator.apply()
            sequence = migrator.ledger.verify().sequence
            second = migrator.apply()
            after = (
                git(repository, "rev-parse", "HEAD"),
                git(repository, "status", "--porcelain"),
                git(repository, "show-ref"),
            )
            self.assertTrue(first["migration_applied"])
            self.assertFalse(second["migration_applied"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(sequence, migrator.ledger.verify().sequence)
            self.assertEqual(before, after)

    def test_migration_filters_future_milestone_and_rejects_active_ambiguity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository, project = synthetic_repository(root)
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["milestones"].append({
                "id": "M1", "name": "Future", "status": "planned",
                "base_commit": None, "integration_branch": "codex/m1",
                "integrated_features": [], "last_validated_commit": None,
                "human_gate": True,
            })
            future = dict(queue["features"][0])
            future.update({
                "id": "F100", "title": "Future accepted", "milestone": "M1",
                "status": "integration_pending",
                "accepted_commit": git(repository, "rev-parse", "HEAD"),
            })
            queue["features"].append(future)
            queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
            projection, _, _ = LegacyStateMigrator(
                controller_root=root / "controller", project=project
            ).derive_projection()
            self.assertEqual("F001", projection["current_feature"])
            self.assertEqual("feature_cycle", projection["allowed_next_action"])

            queue["features"][0].update({
                "status": "integration_pending",
                "accepted_commit": git(repository, "rev-parse", "HEAD"),
            })
            second = dict(queue["features"][0])
            second.update({"id": "F002", "title": "Second active candidate"})
            queue["features"].append(second)
            queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "multiple accepted features"):
                LegacyStateMigrator(
                    controller_root=root / "controller", project=project
                ).apply()

    def test_migration_process_death_boundaries_recover_idempotently(self):
        child_source = r'''
import json, os, sys
from pathlib import Path
from development_conveyor.registry import Project
from development_conveyor.migration import LegacyStateMigrator
value=json.loads(sys.argv[1]); value["repository"]=Path(value["repository"]); value["human_gates"]=tuple(value["human_gates"])
project=Project(**value)
LegacyStateMigrator(controller_root=Path(sys.argv[2]), project=project, interruption_hook=lambda boundary: os._exit(74) if boundary == sys.argv[3] else None).apply()
'''
        for boundary in ("after_lease", "after_terminal", "after_release", "after_projection"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root)
                controller = root / "controller"
                project_value = {
                    key: (str(value) if isinstance(value, Path) else list(value) if isinstance(value, tuple) else value)
                    for key, value in project.__dict__.items()
                }
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                child = subprocess.run(
                    ["python3", "-c", child_source, json.dumps(project_value), str(controller), boundary],
                    cwd=Path(__file__).resolve().parents[1], env=environment, check=False,
                )
                self.assertEqual(74, child.returncode)
                migrator = LegacyStateMigrator(controller_root=controller, project=project)
                migrator.apply()
                sequence = migrator.ledger.verify().sequence
                repeated = migrator.apply()
                self.assertFalse((controller / "state/projects/synthetic/migration-writer.json").exists())
                self.assertEqual(1, sum(
                    event["event_type"] == "TransactionCompleted"
                    and event["payload"].get("migration_batch") is True
                    for event in migrator.ledger.read()
                ))
                self.assertEqual(sequence, migrator.ledger.verify().sequence)
                self.assertFalse(repeated["migration_applied"])
                self.assertTrue(repeated["idempotent"])

    def test_simulator_uses_all_boundaries(self):
        self.assertEqual(15, len(INTERRUPTION_BOUNDARIES))
        self.assertEqual(len(INTERRUPTION_BOUNDARIES), len(set(INTERRUPTION_BOUNDARIES)))

    def test_actual_kernel_interruption_after_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = DeterministicLifecycleSimulator(Path(temporary)).run(
                cycles=1, interrupt_at="after_commit", interrupt_workflow=WorkflowType.FEATURE_EXECUTION
            )
            self.assertTrue(result["interruption_recovered"])
            self.assertFalse(result["conflicting_terminal_events"])
            self.assertEqual([], result["stale_live_leases"])
            self.assertEqual(1, result["recovery_evidence"][0]["terminal_count"])

    def test_integration_after_commit_recovery_preserves_exact_accepted_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = DeterministicLifecycleSimulator(Path(temporary)).run(
                cycles=1, interrupt_at="after_commit",
                interrupt_workflow=WorkflowType.MILESTONE_INTEGRATION,
            )
            outcome = result["projection"]["historical_integration_outcomes"][0]
            self.assertTrue(result["interruption_recovered"])
            self.assertEqual(result["accepted_commits"][0], outcome["accepted_commit"])
            self.assertEqual(result["integrated_commits"][0], outcome["integrated_commit"])

    def test_interruption_rebuilds_exact_projection_and_cache_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            simulator = DeterministicLifecycleSimulator(Path(temporary))
            result = simulator.run(
                cycles=1, interrupt_at="after_commit",
                interrupt_workflow=WorkflowType.FEATURE_EXECUTION,
            )
            ledger, projection, lease = simulator._resources()
            integrity = ledger.verify()
            rebuilt = projection.rebuild(persist_cache=False)
            cached = projection.load_cache()

            self.assertTrue(result["interruption_recovered"])
            self.assertEqual("queue_reconciliation", rebuilt["current_state"])
            self.assertIsNone(rebuilt["active_transaction"])
            self.assertEqual(integrity.sequence, rebuilt["ledger_sequence"])
            self.assertEqual(integrity.fingerprint, rebuilt["ledger_fingerprint"])
            self.assertEqual(projection_fingerprint(rebuilt), rebuilt["projection_fingerprint"])
            self.assertEqual(rebuilt, result["projection"])
            self.assertEqual(rebuilt, cached)
            self.assertFalse(lease.path.exists())

            self.assertEqual(1, len(rebuilt["historical_integration_outcomes"]))
            outcome = rebuilt["historical_integration_outcomes"][0]
            self.assertEqual(result["accepted_commits"][0], outcome["accepted_commit"])
            self.assertEqual(result["integrated_commits"][0], outcome["integrated_commit"])
            self.assertTrue(outcome["terminal_repository_clean"])

    def test_actual_kernel_integrates_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = DeterministicLifecycleSimulator(Path(temporary)).run(cycles=1)
            self.assertEqual(1, result["accepted_commit_count"])
            self.assertEqual(1, result["integration_count"])
            self.assertFalse(result["duplicate_integration"])
            self.assertNotEqual(result["accepted_commits"][0], result["integrated_commits"][0])

    def test_actual_twenty_cycle_milestone_has_no_duplicate_or_prohibited_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = DeterministicLifecycleSimulator(Path(temporary)).run(cycles=20)
            self.assertEqual(20, result["cycles_completed"])
            self.assertEqual(20, result["accepted_commit_count"])
            self.assertEqual(20, result["integration_count"])
            self.assertFalse(result["duplicate_commits"])
            self.assertFalse(result["duplicate_integration"])
            self.assertFalse(result["conflicting_terminal_events"])
            self.assertTrue(result["default_branch_unchanged"])
            self.assertEqual([], result["stale_live_leases"])
            self.assertEqual([], result["prohibited_git_commands"])
            self.assertEqual([], result["remotes"])
            self.assertEqual([], result["tags_created"])

    def test_actual_recovery_planner_cross_process_boundary_matrix(self):
        repository_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository_root / "src")
        owner_source = "\n".join((
            "import json, sys",
            "from pathlib import Path",
            "from development_conveyor.registry import Project",
            "from development_conveyor.repository import RepositoryInspector",
            "from development_conveyor.ledger import EvidenceLedger",
            "from development_conveyor.projection import ProjectionEngine",
            "from development_conveyor.workflow_lease import WorkflowWriterLease",
            "from development_conveyor.kernel import WorkflowKernel",
            "from development_conveyor.contracts import WorkflowType, MutationPolicy",
            "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
            "i=RepositoryInspector(p.repository).identity(); l=EvidenceLedger(Path(sys.argv[2]), project_id=p.project_id, repository_identity=i['repository_id'], repository_path_fingerprint=i['path_fingerprint'])",
            "k=WorkflowKernel(project=p, ledger=l, projection=ProjectionEngine(l, Path(sys.argv[3])), lease=WorkflowWriterLease(p.repository / '.factory/locks/writer.json'))",
            "k.begin(workflow_type=WorkflowType.FEATURE_EXECUTION, milestone='M0', feature_id='F001', run_id='crashed-owner', policy=MutationPolicy(('app.txt',), commit_subject='F001'))",
            "k.acquire_lease(); k.capture_snapshot()",
        ))
        recovery_source = "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "from development_conveyor.registry import Project",
            "from development_conveyor.repository import RepositoryInspector",
            "from development_conveyor.ledger import EvidenceLedger",
            "from development_conveyor.projection import ProjectionEngine",
            "from development_conveyor.workflow_lease import WorkflowWriterLease",
            "from development_conveyor.workflow_recovery import RecoveryPlanner",
            "v=json.loads(sys.argv[1]); v['repository']=Path(v['repository']); v['human_gates']=tuple(v['human_gates']); p=Project(**v)",
            "i=RepositoryInspector(p.repository).identity(); l=EvidenceLedger(Path(sys.argv[2]), project_id=p.project_id, repository_identity=i['repository_id'], repository_path_fingerprint=i['path_fingerprint'])",
            "target=sys.argv[4]",
            "def interrupt(boundary, transaction):",
            "    if boundary == target: os._exit(17)",
            "RecoveryPlanner(project=p, ledger=l, projection=ProjectionEngine(l, Path(sys.argv[3])), lease=WorkflowWriterLease(p.repository / '.factory/locks/writer.json'), interruption_hook=interrupt).apply()",
        ))
        for boundary in INTERRUPTION_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); repository, project = synthetic_repository(root / "app")
                exclude = repository / ".git/info/exclude"
                exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.factory/locks/writer.json\n", encoding="utf-8")
                value = {
                    key: (str(item) if isinstance(item, Path) else list(item) if isinstance(item, tuple) else item)
                    for key, item in project.__dict__.items()
                }
                ledger_path = root / "controller/evidence.jsonl"
                cache_path = root / "controller/projection.json"
                subprocess.run(
                    ["python3", "-c", owner_source, json.dumps(value), str(ledger_path), str(cache_path)],
                    cwd=repository_root, env=environment, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                interrupted = subprocess.run(
                    ["python3", "-c", recovery_source, json.dumps(value), str(ledger_path), str(cache_path), boundary],
                    cwd=repository_root, env=environment, check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.assertEqual(17, interrupted.returncode)
                identity = RepositoryInspector(repository).identity()
                ledger = EvidenceLedger(ledger_path, project_id="synthetic", repository_identity=identity["repository_id"], repository_path_fingerprint=identity["path_fingerprint"])
                projection = ProjectionEngine(ledger, cache_path)
                lease = WorkflowWriterLease(repository / ".factory/locks/writer.json")
                for _ in range(8):
                    planner = RecoveryPlanner(project=project, ledger=ledger, projection=projection, lease=lease)
                    plan = planner.inspect()
                    if plan["classification"] in {"nothing_to_recover", "no_transaction"}:
                        break
                    self.assertTrue(plan.get("recoverable"), plan)
                    applied = planner.apply()
                    if applied.get("action") == "resume_exact_transaction":
                        kernel = WorkflowKernel(project=project, ledger=ledger, projection=projection, lease=lease)
                        transaction = kernel.restore(str(applied["transaction_id"]))
                        kernel.block(
                            state=TransactionState.SUPERSEDED,
                            classification="INTERRUPTED_TRANSACTION_SUPERSEDED",
                            next_state="feature_ready",
                            reference=applied.get("recovery_transaction_id"),
                        )
                else:
                    self.fail(f"recovery did not converge for {boundary}")
                self.assertFalse(lease.path.exists())
                events = ledger.read()
                recovery_transactions = {
                    event["transaction_id"] for event in events
                    if event["workflow_type"] == WorkflowType.RECOVERY.value
                }
                self.assertTrue(recovery_transactions)
                for transaction_id in recovery_transactions:
                    self.assertEqual(1, sum(
                        event["transaction_id"] == transaction_id
                        and event["event_type"] in {"TransactionCompleted", "TransactionBlocked", "TransactionSuperseded", "HumanGateRaised"}
                        for event in events
                    ))

    def test_actual_every_workflow_boundary_matrix_is_recovered(self):
        result = simulate_all_interruptions()
        self.assertEqual(len(SIMULATED_WORKFLOWS) * len(INTERRUPTION_BOUNDARIES), result["case_count"])
        self.assertTrue(result["all_recovered"])
        self.assertFalse(result["duplicate_commits"])
        self.assertFalse(result["duplicate_integration"])
        self.assertFalse(result["conflicting_terminal_events"])
        self.assertTrue(result["default_branch_unchanged"])
        self.assertEqual([], result["stale_live_leases"])
        self.assertTrue(all(item["terminal_count"] == 1 for item in result["results"]))
        self.assertTrue(all(item["lease_exists_after_recovery"] is False for item in result["results"]))


if __name__ == "__main__":
    unittest.main()
