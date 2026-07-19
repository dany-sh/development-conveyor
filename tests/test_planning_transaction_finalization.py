from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import LockError, RecoveryError, SessionError
from development_conveyor.locks import PlanningWriterLease
from development_conveyor.planning import (
    PLANNING_CLASSIFICATIONS,
    finalize_planning_commit,
    validate_planning_changes,
)
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan, SessionResult
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


RUN_ID = "planning-recovery-run"
SESSION_ID = "019f77e1-c551-7f00-9409-2fff9f6ee79b"
SEVEN_PATHS = [
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/features/P0-001-per-case-persistence-authority.md",
    "docs/features/P0-003-versioned-application-registry.md",
]


def assistant_result(classification: str, feature_count: int) -> tuple[dict, str]:
    value = {
        "schema_version": 1,
        "classification": classification,
        "summary": "Synthetic planning result.",
        "next_action": "safe checkpoint",
        "queue_validation": {
            "valid": True,
            "milestone_found": True,
            "feature_count": feature_count,
        },
        "retryable": False,
        "human_decision": None,
    }
    marker = "CONVEYOR_RESULT=" + json.dumps(value, separators=(",", ":"))
    event = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": marker},
    })
    return value, event


class MutatingLauncher:
    def __init__(self, mutation):
        self.mutation = mutation
        self.lease_at_launch = None

    def launch(self, request):
        lock = request.project.repository / ".factory/locks/writer.json"
        self.lease_at_launch = json.loads(lock.read_text(encoding="utf-8"))
        self.mutation(request.project.repository, request.project.queue_location)
        queue = json.loads((request.project.repository / request.project.queue_location).read_text())
        value, output = assistant_result("reconciled_ready_work", len(queue["features"]))
        plan = SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64,
            "workspace-write",
        )
        return SessionResult(
            request.action,
            0,
            SESSION_ID,
            output,
            plan,
            redacted_stdout=output,
            structured_result=value,
            structured_output_validation="valid",
            result_classification="reconciled_ready_work",
        )


class PlanningTransactionTests(unittest.TestCase):
    def _engine_fixture(self, root: Path):
        repository, project = synthetic_repository(root, feature_status="proposed")
        project = replace(project, current_state="queue_reconciliation")
        return repository, project

    @staticmethod
    def _ready_queue(repository: Path, queue_location: str) -> None:
        path = repository / queue_location
        queue = json.loads(path.read_text())
        queue["features"][0]["status"] = "ready"
        write_json(path, queue)

    def _case_recovery_fixture(self, root: Path):
        repository, project = synthetic_repository(root, feature_status="proposed")
        baseline = git(repository, "rev-parse", "HEAD")
        git(repository, "branch", "codex/p0-foundation", baseline)
        git(repository, "switch", "codex/p0-foundation")
        project = replace(
            project,
            active_milestone="P0",
            milestone_branch="codex/p0-foundation",
            current_state="queue_reconciliation",
        )
        for relative in SEVEN_PATHS:
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative != "docs/FEATURE_QUEUE.yaml":
                path.write_text("Planning baseline.\n", encoding="utf-8")
        queue = {
            "schema_version": 1,
            "milestones": [{
                "id": "phase-0",
                "name": "Phase 0",
                "status": "active",
                "base_commit": baseline,
                "integration_branch": "codex/p0-foundation",
                "integrated_features": ["P0-001", "P0-002"],
                "last_validated_commit": baseline,
                "human_gate": True,
            }],
            "features": [],
        }
        for index in range(1, 15):
            feature_id = f"P0-{index:03d}"
            status = "integrated" if index == 1 else ("done" if index == 2 else "proposed")
            dependencies = [] if index <= 2 else [f"P0-{index - 1:03d}"]
            feature = {
                "id": feature_id,
                "name": f"Synthetic {feature_id}",
                "status": status,
                "priority": index,
                "milestone": "phase-0",
                "dependencies": dependencies,
                "spec": (
                    "docs/features/P0-001-per-case-persistence-authority.md"
                    if index == 1
                    else "docs/features/P0-003-versioned-application-registry.md"
                ),
                "acceptance_criteria": [f"{feature_id} acceptance"],
                "requires_human_decision": index in {11, 14},
                "branch": None,
                "accepted_commit": baseline if index == 1 else None,
                "integrated_commit": baseline if index == 1 else None,
                "integration_status": "passed" if index <= 2 else "pending",
                "integration_fix_commits": [],
            }
            if index == 2:
                feature["commit"] = baseline
            queue["features"].append(feature)
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        git(repository, "add", ".")
        git(repository, "commit", "-m", "synthetic Phase 0 planning baseline")
        starting_head = git(repository, "rev-parse", "HEAD")

        queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
        queue["features"][2]["status"] = "ready"
        queue["features"][2]["dependencies"] = ["P0-001", "P0-002"]
        queue["features"][2]["requires_human_decision"] = False
        write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
        (repository / "docs/CURRENT_STATUS.md").write_text("P0-003 is ready.\n", encoding="utf-8")
        (repository / "docs/FEATURE_CATALOG.md").write_text("| P0-003 | Ready |\n", encoding="utf-8")
        (repository / "docs/ROADMAP.md").write_text("P0-003 is ready.\n", encoding="utf-8")
        (repository / "docs/RUN_LOG.md").write_text("Planning reconciliation only.\n", encoding="utf-8")
        (repository / "docs/features/P0-001-per-case-persistence-authority.md").write_text(
            "P0-001 integrated planning evidence.\n", encoding="utf-8"
        )
        (repository / "docs/features/P0-003-versioned-application-registry.md").write_text(
            "# P0-003\n\nStatus: Ready\n", encoding="utf-8"
        )
        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration)
        value, output = assistant_result("reconciled_ready_work", 14)
        report = {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": RUN_ID,
            "action": "queue_reconciliation",
            "working_directory": str(repository),
            "exit_status": 0,
            "structured_output_validation": "valid",
            "structured_result": value,
            "redacted_stdout": output,
            "session_id": SESSION_ID,
        }
        report_path = configuration.root / "reports" / RUN_ID / "queue_reconciliation.json"
        write_json(report_path, report)
        inspector = RepositoryInspector(repository)
        return repository, project, engine, starting_head, inspector.planning_diff_fingerprint()

    def test_01_planning_lease_exists_before_session_mutation_and_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            launcher = MutatingLauncher(self._ready_queue)
            engine = CycleEngine(controller_configuration(root, project), launcher)
            state = engine._project_document(
                project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"]
            )
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(launcher.lease_at_launch["phase"], "queue_reconciliation")
            self.assertEqual(launcher.lease_at_launch["purpose"], "development-conveyor-planning")
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertEqual(result["planning_transaction"]["planning_commit_status"], "committed")

    def test_02_planning_commit_is_focused_and_repository_is_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(self._ready_queue))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            commit = result["planning_result_commit"]
            self.assertEqual(RepositoryInspector(repository).changed_paths(commit), ["docs/FEATURE_QUEUE.yaml"])
            self.assertEqual(RepositoryInspector(repository).commit_subject(commit), "factory: reconcile M0 queue and ready F001")
            self.assertTrue(RepositoryInspector(repository).is_clean)

    def test_03_unauthorized_production_change_rejects_finalization_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "app.txt").write_text("unauthorized production edit\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(result["outcome"], "planning_validation_failed")
            self.assertIn("app.txt", result["changed_paths"])
            self.assertEqual((repository / "app.txt").read_text(), "unauthorized production edit\n")
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_04_unauthorized_product_test_change_rejects_finalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            (repository / "tests").mkdir()
            (repository / "tests/product_test.py").write_text("baseline\n", encoding="utf-8")
            git(repository, "add", "tests/product_test.py")
            git(repository, "commit", "-m", "add baseline product test")
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "tests/product_test.py").write_text("implementation\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertEqual(result["outcome"], "planning_validation_failed")
            self.assertIn("tests/product_test.py", result["changed_paths"])

    def test_05_allowed_path_without_semantic_agreement_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = self._engine_fixture(root)
            (repository / "docs/CURRENT_STATUS.md").write_text("F001 proposed.\n", encoding="utf-8")
            git(repository, "add", "docs/CURRENT_STATUS.md")
            git(repository, "commit", "-m", "status baseline")
            def mutate(repo, queue):
                self._ready_queue(repo, queue)
                (repo / "docs/CURRENT_STATUS.md").write_text("F001 remains proposed.\n", encoding="utf-8")
            engine = CycleEngine(controller_configuration(root, project), MutatingLauncher(mutate))
            state = engine._project_document(project, RUN_ID, RepositoryInspector(repository).identity()["path_fingerprint"])
            result = engine._execute_queue_reconciliation(project, "one_feature", RUN_ID, state)
            self.assertIn("semantic agreement", result["failed_validator"])

    def test_06_planning_lease_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            lease = PlanningWriterLease(repository / ".factory/locks/writer.json", repository)
            lease.acquire(
                repository_identity=inspector.identity()["repository_id"], project_id="synthetic",
                milestone="M0", run_id=RUN_ID, session_id=SESSION_ID,
                branch=str(inspector.current_branch), head=inspector.head,
                worktree_fingerprint="f" * 64, allowed_paths=["docs/FEATURE_QUEUE.yaml"],
            )
            with self.assertRaisesRegex(LockError, "identity mismatch"):
                lease.revalidate(run_id="other-run")
            lease.release(run_id=RUN_ID)

    def test_07_current_seven_file_recovery_dry_run_is_write_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            before = git(repository, "status", "--porcelain=v1", "--branch")
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=True,
            )
            after = git(repository, "status", "--porcelain=v1", "--branch")
            self.assertEqual(before, after)
            self.assertEqual(result["planning_transaction"]["changed_path_count"], 7)
            self.assertFalse(result["feature_factory_would_launch"])
            self.assertFalse(result["milestone_integrator_would_launch"])

    def test_08_current_seven_file_recovery_creates_exactly_one_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            commit = result["planning_transaction"]["planning_result_commit"]
            self.assertEqual(git(repository, "rev-list", "--count", f"{head}..{commit}"), "1")
            self.assertEqual(RepositoryInspector(repository).changed_paths(commit), sorted(SEVEN_PATHS))
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertEqual(result["planning_transaction"]["selected_feature"], "P0-003")
            self.assertEqual(result["planning_transaction"]["selected_feature_starting_commit"], commit)

    def test_09_repeated_applied_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            arguments = dict(
                project=project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            first = engine.recover_planning_transaction(**arguments)
            second = engine.recover_planning_transaction(**arguments)
            self.assertEqual(second["outcome"], "planning_transaction_already_finalized")
            self.assertEqual(
                first["planning_transaction"]["planning_result_commit"],
                second["planning_transaction"]["planning_result_commit"],
            )
            self.assertEqual(git(repository, "rev-list", "--count", f"{head}..HEAD"), "1")

    def test_10_starting_head_divergence_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, _, diff = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "starting HEAD diverged"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head="0" * 40,
                    expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_11_diff_fingerprint_mismatch_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, _ = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "diff fingerprint"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head=head,
                    expected_diff_fingerprint="0" * 64, expected_changed_paths=SEVEN_PATHS,
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_12_changed_path_mismatch_rejects_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            with self.assertRaisesRegex(RecoveryError, "changed-path set"):
                engine.recover_planning_transaction(
                    project, run_id=RUN_ID, expected_starting_head=head,
                    expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS[:-1],
                    expected_session_id=SESSION_ID, dry_run=True,
                )

    def test_13_queue_fingerprint_change_after_validation_rejects_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            report = json.loads((engine.root / "reports" / RUN_ID / "queue_reconciliation.json").read_text())
            inspector = RepositoryInspector(repository)
            validation = validate_planning_changes(
                project, inspector, report, run_id=RUN_ID, starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID,
            )
            queue_path = repository / "docs/FEATURE_QUEUE.yaml"
            queue_path.write_text(queue_path.read_text() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "diff changed|queue changed"):
                finalize_planning_commit(project, inspector, validation)

    def test_14_planning_baseline_fields_are_non_self_referential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )["planning_transaction"]
            self.assertEqual(result["previous_validated_milestone_head"], head)
            self.assertNotEqual(result["planning_result_commit"], head)
            self.assertIsNone(result["planning_evidence_commit"])

    def test_15_status_separates_planning_from_repository_and_next_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            status = engine.project_plan(project)
            self.assertEqual(status["current_planning_transaction"]["planning_commit_status"], "committed")
            self.assertTrue(status["current_repository_state"]["clean"])
            self.assertEqual(status["next_feature_selection"]["selected_feature"], "P0-003")
            self.assertEqual(status["proposed_next_action"], "feature_cycle")
            self.assertEqual(status["sessions_that_would_launch"], ["$feature-factory"])
            self.assertFalse(status["milestone_integrator_would_launch"])

    def test_16_recovery_never_launches_selected_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, engine, head, diff = self._case_recovery_fixture(root)
            result = engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            self.assertFalse(result["feature_factory_would_launch"])
            self.assertFalse(result["milestone_integrator_would_launch"])
            self.assertFalse(result["application_source_written"])

    def test_17_explicit_terminal_classification_contract_is_exposed(self):
        self.assertEqual(PLANNING_CLASSIFICATIONS["reconciled_ready_work"], "RECONCILED_READY_WORK")
        self.assertEqual(PLANNING_CLASSIFICATIONS["reconciled_no_ready_work"], "RECONCILED_NO_READY_WORK")
        self.assertEqual(PLANNING_CLASSIFICATIONS["human_decision_required"], "HUMAN_DECISION_REQUIRED")
        self.assertEqual(PLANNING_CLASSIFICATIONS["invalid_queue"], "PLANNING_VALIDATION_FAILED")

    def test_18_recovery_commit_does_not_change_application_or_test_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, head, diff = self._case_recovery_fixture(root)
            app_before = (repository / "app.txt").read_bytes()
            engine.recover_planning_transaction(
                project, run_id=RUN_ID, expected_starting_head=head,
                expected_diff_fingerprint=diff, expected_changed_paths=SEVEN_PATHS,
                expected_session_id=SESSION_ID, dry_run=False,
            )
            self.assertEqual((repository / "app.txt").read_bytes(), app_before)
            self.assertFalse(any(path.startswith("tests/") for path in RepositoryInspector(repository).changed_paths("HEAD")))


if __name__ == "__main__":
    unittest.main()
