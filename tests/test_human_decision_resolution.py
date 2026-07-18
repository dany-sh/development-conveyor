from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.config import load_configuration
from development_conveyor.cli import _parser, execute, main
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ConveyorError, LockError
from development_conveyor.human_resolution import gate_fingerprint
from development_conveyor.locks import DurableLock, make_lock_record
from development_conveyor.logging import run_event
from development_conveyor.registry import ProjectRegistry
from development_conveyor.repository import RepositoryInspector

from tests.helpers import controller_configuration, git, synthetic_repository, write_json


APPROVAL = (
    "Approved stale F002 integration-writer lease release after validated integration, "
    "dead-process verification, recovery recording, and lock-absence verification"
)


class HumanDecisionResolutionTests(unittest.TestCase):
    def _fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["features"][0].update({
            "id": "F002",
            "title": "Integrated synthetic feature",
            "status": "ready",
            "spec": "docs/features/F002.md",
        })
        (repository / "docs/features/F001.md").rename(repository / "docs/features/F002.md")
        write_json(queue_path, queue)
        git(repository, "add", "docs")
        git(repository, "commit", "-m", "test: prepare F002")

        integration_base = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "-c", "codex/f002-integrated-synthetic")
        (repository / "app.txt").write_text("baseline\nF002 accepted\n", encoding="utf-8")
        git(repository, "add", "app.txt")
        git(repository, "commit", "-m", "feat(F002): synthetic accepted feature")
        accepted = git(repository, "rev-parse", "HEAD")
        git(repository, "switch", "codex/m0-foundation")
        git(repository, "merge", "--ff-only", integration_base)
        git(repository, "cherry-pick", accepted)
        integrated = git(repository, "rev-parse", "HEAD")

        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        feature.update({
            "status": "integrated",
            "accepted_commit": accepted,
            "integrated_commit": integrated,
            "integration_status": "passed",
            "branch": "codex/f002-integrated-synthetic",
            "integration_base_commit": integration_base,
        })
        queue["milestones"][0].update({
            "integrated_features": ["F002"],
            "last_validated_commit": integrated,
        })
        write_json(queue_path, queue)
        git(repository, "add", project.queue_location)
        git(repository, "commit", "-m", "factory: record F002 integration")
        expected_head = git(repository, "rev-parse", "HEAD")

        runtime = {
            "schema_version": 1,
            "repository": str(repository.resolve()),
            "worktree": str(repository.resolve()),
            "queue": project.queue_location,
            "feature_id": "F002",
            "feature_branch": "codex/f002-integrated-synthetic",
            "accepted_commit": accepted,
            "milestone_id": "M0",
            "milestone_branch": "codex/m0-foundation",
            "current_branch": "codex/m0-foundation",
            "post_integration_head": integrated,
            "phase": "validated",
            "validation": {
                "ok": True,
                "feature_id": "F002",
                "milestone_id": "M0",
                "milestone_branch": "codex/m0-foundation",
                "validated_commit": integrated,
                "commands": [{"group": "test", "argv": ["synthetic"], "exit_code": 0}],
                "clean_worktree": True,
                "blockers": [],
            },
        }
        common = Path(git(repository, "rev-parse", "--git-common-dir"))
        common = common if common.is_absolute() else (repository / common).resolve()
        runtime_path = common / "factory-integration/F002-synthetic.json"
        write_json(runtime_path, runtime)
        lease = {
            "repository": str(repository.resolve()),
            "worktree": str(repository.resolve()),
            "feature_id": "F002",
            "branch": "codex/m0-foundation",
            "agent_run": "synthetic-integration-run",
            "purpose": "integration",
            "pid": 999999,
            "host": socket.gethostname(),
        }
        recovery = {
            "released_at": "2026-07-18T05:00:25+00:00",
            "reason": "Synthetic explicit stale integration lease release.",
            "lease": lease,
            "investigation": {
                "repository": str(repository.resolve()),
                "branch": "codex/m0-foundation",
                "head": expected_head,
                "dirty_entries": [],
                "unfinished_operations": [],
                "runtime": runtime,
                "writer_lease": lease,
                "lease_process_alive": False,
                "time_alone_proves_stale": False,
                "destructive_actions_taken": False,
            },
        }
        recovery_path = common / "factory-integration/forced-lease-release-20260718T050025Z.json"
        write_json(recovery_path, recovery)
        identity = RepositoryInspector(repository).identity()
        gate = {
            "gate_id": "synthetic-f002-retained-integration-writer-lease",
            "classification": "retained_integration_writer_lease",
            "reason": "F002 integration passed but its integration writer lease remained after process exit.",
            "expected_repository_id": identity["repository_id"],
            "expected_path_fingerprint": identity["path_fingerprint"],
            "expected_milestone": "M0",
            "expected_branch": "codex/m0-foundation",
            "expected_head": expected_head,
            "approved_next_state": "queue_reconciliation",
            "retained_process_id": 999999,
            "feature_id": "F002",
            "accepted_commit": accepted,
            "integrated_commit": integrated,
            "recovery_record": "factory-integration/forced-lease-release-20260718T050025Z.json",
            "recovery_record_sha256": hashlib.sha256(recovery_path.read_bytes()).hexdigest(),
            "integration_record": "factory-integration/F002-synthetic.json",
            "integration_record_sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
        }
        project = replace(
            project,
            current_state="human_decision_required",
            last_accepted_feature="F002",
            last_accepted_commit=accepted,
            human_decision_gate=gate,
        )
        engine = CycleEngine(controller_configuration(root, project))
        return repository, project, engine, runtime_path, recovery_path

    def _resolve(self, engine: CycleEngine, project, *, reason=APPROVAL, dry_run=True, alive=False):
        with patch("development_conveyor.human_resolution.process_alive", return_value=alive):
            return engine.resolve_human_decision(project, reason=reason, dry_run=dry_run)

    def test_01_valid_stale_writer_lease_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project, dry_run=False)
            self.assertEqual(result["outcome"], "resolution_accepted")
            self.assertTrue(result["applied"])

    def test_02_dry_run_performs_no_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project)
            self.assertTrue(result["resolution_would_be_accepted"])
            self.assertFalse(engine.project_state_path(project).exists())
            self.assertFalse(Path(result["report_location"]).exists())
            self.assertFalse(engine.events.path.exists())

    def test_03_empty_reason_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project, reason="")
            self.assertIn("approval_reason_present", result["rejection_reasons"])

    def test_04_project_not_in_human_decision_state_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            project = replace(project, current_state="queue_reconciliation")
            result = self._resolve(engine, project)
            self.assertIn("project_in_human_decision_state", result["rejection_reasons"])

    def test_05_wrong_repository_identity_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            project.human_decision_gate["expected_repository_id"] = "0" * 64
            result = self._resolve(engine, project)
            self.assertIn("repository_identity_matches", result["rejection_reasons"])

    def test_06_unexpected_head_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            (repository / "later.txt").write_text("later\n", encoding="utf-8")
            git(repository, "add", "later.txt")
            git(repository, "commit", "-m", "test: unexpected newer head")
            result = self._resolve(engine, project)
            self.assertIn("head_matches", result["rejection_reasons"])

    def test_07_dirty_repository_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            result = self._resolve(engine, project)
            self.assertIn("worktree_clean", result["rejection_reasons"])

    def test_08_active_git_operation_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            path = Path(git(repository, "rev-parse", "--git-path", "MERGE_HEAD"))
            path = path if path.is_absolute() else repository / path
            path.write_text(project.human_decision_gate["expected_head"] + "\n", encoding="utf-8")
            result = self._resolve(engine, project)
            self.assertIn("git_operations_absent", result["rejection_reasons"])

    def test_09_existing_writer_lease_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            write_json(repository / ".factory/locks/writer.json", {"repository": str(repository)})
            result = self._resolve(engine, project)
            self.assertIn("repository_writer_lease_absent", result["rejection_reasons"])

    def test_10_live_retained_pid_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project, alive=True)
            self.assertIn("retained_process_dead", result["rejection_reasons"])

    def test_11_missing_recovery_record_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, recovery = self._fixture(Path(temporary))
            recovery.unlink()
            result = self._resolve(engine, project)
            self.assertIn("recovery_record_pinned", result["rejection_reasons"])

    def test_12_contradictory_recovery_record_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, recovery = self._fixture(Path(temporary))
            value = json.loads(recovery.read_text())
            value["investigation"]["destructive_actions_taken"] = True
            write_json(recovery, value)
            project.human_decision_gate["recovery_record_sha256"] = hashlib.sha256(recovery.read_bytes()).hexdigest()
            result = self._resolve(engine, project)
            self.assertIn("structured_forced_lease_release_record_v1", result["rejection_reasons"])

    def test_13_missing_integration_validation_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, runtime, _ = self._fixture(Path(temporary))
            value = json.loads(runtime.read_text())
            value["validation"]["ok"] = False
            write_json(runtime, value)
            project.human_decision_gate["integration_record_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
            result = self._resolve(engine, project)
            self.assertIn("integration_validation_recorded_successful", result["rejection_reasons"])

    def test_14_successful_transition_to_queue_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            self._resolve(engine, project, dry_run=False)
            state = engine.load_project_state(project)
            self.assertEqual(state["current_state"], "queue_reconciliation")
            self.assertIsNone(state["human_decision_required"])

    def test_15_original_gate_preserved_as_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            expected = dict(project.human_decision_gate)
            self._resolve(engine, project, dry_run=False)
            history = engine.load_project_state(project)["human_decision_history"]
            self.assertEqual(history[0]["gate"], expected)
            self.assertEqual(history[0]["gate_fingerprint"], gate_fingerprint(expected))

    def test_16_explicit_resolution_evidence_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project, dry_run=False)
            state = engine.load_project_state(project)
            resolution = state["human_decision_history"][0]["resolution"]
            self.assertEqual(resolution["resolution_id"], result["resolution_id"])
            self.assertEqual(resolution["actor_classification"], "explicit_user_approval")
            self.assertTrue(Path(result["report_location"]).is_file())

    def test_17_repeated_identical_resolution_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            first = self._resolve(engine, project, dry_run=False)
            second = self._resolve(engine, project, dry_run=False)
            self.assertEqual(second["outcome"], "already_resolved")
            self.assertEqual(len(engine.load_project_state(project)["human_decision_history"]), 1)
            events = [json.loads(line) for line in engine.events.path.read_text().splitlines()]
            self.assertEqual(sum(item["run_id"] == first["resolution_id"] for item in events), 1)

    def test_18_later_unrelated_gate_is_not_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            self._resolve(engine, project, dry_run=False)
            state = engine.load_project_state(project)
            state["current_state"] = "human_decision_required"
            state["human_decision_required"] = {
                "gate_id": "later-unrelated-gate",
                "classification": "product_decision",
                "gate_fingerprint": "f" * 64,
            }
            engine.project_store.write(engine.project_state_path(project), state)
            result = self._resolve(engine, project)
            self.assertEqual(result["outcome"], "resolution_rejected")
            self.assertEqual(engine.load_project_state(project)["human_decision_required"]["gate_id"], "later-unrelated-gate")

    def test_19_never_transitions_directly_to_feature_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            result = self._resolve(engine, project)
            self.assertEqual(result["state_transition_path"], ["human_decision_required", "queue_reconciliation"])
            self.assertNotIn("feature_running", result["state_transition_path"])

    def test_20_controller_registration_has_pinned_legacy_gate(self):
        root = Path(__file__).resolve().parents[1]
        configuration = load_configuration(root)
        project = ProjectRegistry(configuration).get("interview-companion")
        gate = project.human_decision_gate
        self.assertEqual(gate["classification"], "retained_integration_writer_lease")
        self.assertEqual(gate["expected_head"], "eb8be6f60628e2547ecb0a5d14a23a8477866c7a")
        self.assertRegex(gate["recovery_record_sha256"], r"^[0-9a-f]{64}$")

    def test_21_repository_cycle_state_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            write_json(repository / ".factory/conveyor-state.json", {"schema_version": 1})
            result = self._resolve(engine, project)
            self.assertIn("repository_cycle_state_absent", result["rejection_reasons"])

    def test_22_same_gate_different_reason_does_not_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            self._resolve(engine, project, dry_run=False)
            result = self._resolve(engine, project, reason="A different approval reason that is still specific.")
            self.assertEqual(result["outcome"], "resolution_rejected")
            self.assertEqual(len(engine.load_project_state(project)["human_decision_history"]), 1)

    def test_23_sensitive_reason_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            sensitive_reason = "password" + "=synthetic-sensitive-value"
            result = self._resolve(engine, project, reason=sensitive_reason)
            self.assertIn("approval_reason_sensitive_content_absent", result["rejection_reasons"])
            self.assertEqual(result["user_provided_reason"], "")

    def test_24_corrupt_applied_report_is_atomically_healed(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            first = self._resolve(engine, project, dry_run=False)
            history = engine.load_project_state(project)["human_decision_history"][0]["resolution"]
            write_json(Path(first["report_location"]), {"resolution_fingerprint": "0" * 64})
            repeated = self._resolve(engine, project, dry_run=False)
            self.assertEqual(repeated["outcome"], "already_resolved")
            self.assertEqual(json.loads(Path(first["report_location"]).read_text()), history)
            self.assertTrue(all(item["passed"] for item in repeated["evidence_results"]))
            report_check = next(
                item for item in repeated["evidence_results"]
                if item["validator"] == "applied_resolution_report_matches_history"
            )
            self.assertTrue(report_check["evidence"]["repair_attempted"])

    def test_25_mismatched_applied_audit_is_atomically_healed(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            first = self._resolve(engine, project, dry_run=False)
            events = [json.loads(line) for line in engine.events.path.read_text().splitlines()]
            for event in events:
                if event.get("run_id") == first["resolution_id"]:
                    event["human_gate"]["resolution_fingerprint"] = "0" * 64
            engine.events.path.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
                encoding="utf-8",
            )
            repeated = self._resolve(engine, project, dry_run=False)
            self.assertEqual(repeated["outcome"], "already_resolved")
            repaired = [json.loads(line) for line in engine.events.path.read_text().splitlines()]
            matches = [item for item in repaired if item.get("run_id") == first["resolution_id"]]
            self.assertEqual(len(matches), 1)
            self.assertEqual(
                matches[0]["human_gate"]["resolution_fingerprint"],
                first["resolution_fingerprint"],
            )
            audit_check = next(
                item for item in repeated["evidence_results"]
                if item["validator"] == "applied_resolution_audit_matches_history"
            )
            self.assertTrue(audit_check["evidence"]["repair_attempted"])

    def test_26_reservation_acquire_race_is_structured_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            with patch.object(DurableLock, "acquire", side_effect=LockError("synthetic race")):
                result = self._resolve(engine, project, dry_run=False)
            self.assertEqual(result["outcome"], "resolution_rejected")
            self.assertIn("controller_resolution_reservation_acquired", result["rejection_reasons"])
            self.assertIn("appeared before", result["rejection_detail"])
            self.assertFalse(result["applied"])
            self.assertIsNone(engine.load_project_state(project))

    def test_27_evidence_change_under_reservation_is_structured_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))
            original_acquire = DurableLock.acquire

            def acquire_and_mutate(lock, record):
                original_acquire(lock, record)
                (repository / "appeared-under-lock.txt").write_text("changed\n", encoding="utf-8")

            with patch.object(DurableLock, "acquire", new=acquire_and_mutate):
                result = self._resolve(engine, project, dry_run=False)
            self.assertEqual(result["outcome"], "resolution_rejected")
            self.assertIn("worktree_clean", result["rejection_reasons"])
            self.assertIn("changed under controller reservation", result["rejection_detail"])
            self.assertFalse(result["applied"])
            self.assertIsNone(engine.load_project_state(project))

    def test_28_cli_flags_reason_contract_and_rejected_exit(self):
        parsed = _parser().parse_args([
            "reconcile", "--project", "synthetic", "--resolve-human-decision",
            "--reason", APPROVAL, "--dry-run",
        ])
        self.assertTrue(parsed.resolve_human_decision)
        self.assertEqual(parsed.reason, APPROVAL)
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            configuration = engine.configuration
            with (
                patch("development_conveyor.cli.discover_root", return_value=configuration.root),
                patch("development_conveyor.cli.load_configuration", return_value=configuration),
                patch("development_conveyor.cli.SessionLauncher"),
                patch("development_conveyor.cli.CycleEngine"),
            ):
                with self.assertRaisesRegex(ConveyorError, "--reason requires"):
                    execute(["reconcile", "--project", "synthetic", "--reason", APPROVAL])
        with patch("development_conveyor.cli.execute", return_value={"outcome": "resolution_rejected"}):
            self.assertEqual(main([]), 2)

    def test_29_existing_controller_reservation_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            inspector = RepositoryInspector(project.repository)
            reservation = engine._launch_lock(project, inspector)
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id="other-controller",
                current_feature=None,
                current_phase="synthetic",
            ))
            try:
                result = self._resolve(engine, project)
            finally:
                reservation.release("other-controller")
            self.assertEqual(result["outcome"], "resolution_rejected")
            self.assertIn("controller_reservation_absent", result["rejection_reasons"])

    def test_30_dry_run_preserves_synthetic_application_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _, _ = self._fixture(Path(temporary))

            def snapshot():
                files = {
                    path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in repository.rglob("*")
                    if path.is_file() and ".git" not in path.parts
                }
                return {
                    "files": files,
                    "head": git(repository, "rev-parse", "HEAD"),
                    "branch": git(repository, "branch", "--show-current"),
                    "refs": git(repository, "show-ref"),
                    "status": git(repository, "status", "--porcelain=v1"),
                    "cycle_exists": (repository / ".factory/conveyor-state.json").exists(),
                    "writer_exists": (repository / ".factory/locks/writer.json").exists(),
                }

            before = snapshot()
            result = self._resolve(engine, project, dry_run=True)
            after = snapshot()
            self.assertTrue(result["resolution_would_be_accepted"])
            self.assertEqual(after, before)

    def test_31_concurrent_other_project_append_survives_audit_healing(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            first = self._resolve(engine, project, dry_run=False)
            events = [json.loads(line) for line in engine.events.path.read_text().splitlines()]
            events[0]["human_gate"]["resolution_fingerprint"] = "0" * 64
            engine.events.path.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
                encoding="utf-8",
            )
            concurrent = run_event(
                run_id="other-project-concurrent-event",
                project_id="other-project",
                source="deterministic_script",
                result="other_project_progress",
            )
            threads: list[threading.Thread] = []
            from development_conveyor import cycle_engine as cycle_engine_module

            original_atomic_write = cycle_engine_module.atomic_write_bytes

            def write_while_other_project_appends(path, payload, mode=0o600):
                thread = threading.Thread(target=engine.events.append, args=(concurrent,))
                threads.append(thread)
                thread.start()
                time.sleep(0.1)
                self.assertTrue(thread.is_alive())
                return original_atomic_write(path, payload, mode)

            with patch(
                "development_conveyor.cycle_engine.atomic_write_bytes",
                side_effect=write_while_other_project_appends,
            ):
                repeated = self._resolve(engine, project, dry_run=False)
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(repeated["outcome"], "already_resolved")
            repaired = [json.loads(line) for line in engine.events.path.read_text().splitlines()]
            self.assertEqual(
                sum(item.get("run_id") == "other-project-concurrent-event" for item in repaired),
                1,
            )
            self.assertEqual(
                sum(item.get("run_id") == first["resolution_id"] for item in repaired),
                1,
            )

    def test_32_newer_gate_interleaving_blocks_artifact_healing(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            first = self._resolve(engine, project, dry_run=False)
            report_path = Path(first["report_location"])
            corrupt = {"resolution_fingerprint": "0" * 64}
            write_json(report_path, corrupt)
            original_acquire = DurableLock.acquire

            def acquire_then_install_newer_gate(lock, record):
                original_acquire(lock, record)
                state = engine.load_project_state(project)
                state["current_state"] = "human_decision_required"
                state["human_decision_required"] = {
                    "gate_id": "newer-interleaved-gate",
                    "classification": "product_decision",
                    "gate_fingerprint": "f" * 64,
                }
                engine.project_store.write(engine.project_state_path(project), state)

            with patch.object(DurableLock, "acquire", new=acquire_then_install_newer_gate):
                repeated = self._resolve(engine, project, dry_run=False)
            self.assertEqual(repeated["outcome"], "resolution_rejected")
            self.assertIn(
                "applied_resolution_state_unchanged_under_reservation",
                repeated["rejection_reasons"],
            )
            self.assertEqual(json.loads(report_path.read_text()), corrupt)
            state = engine.load_project_state(project)
            self.assertEqual(
                state["human_decision_required"]["gate_id"], "newer-interleaved-gate"
            )

    def test_33_idempotent_dry_run_reports_current_dry_run_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _, _ = self._fixture(Path(temporary))
            self._resolve(engine, project, dry_run=False)
            repeated = self._resolve(engine, project, dry_run=True)
            self.assertEqual(repeated["outcome"], "already_resolved")
            self.assertTrue(repeated["dry_run"])

    def test_34_controller_event_log_lock_is_git_ignored(self):
        root = Path(__file__).resolve().parents[1]
        evidence = git(root, "check-ignore", "-v", "logs/run-events.jsonl.lock")
        self.assertIn("logs/*.lock", evidence)


if __name__ == "__main__":
    unittest.main()
