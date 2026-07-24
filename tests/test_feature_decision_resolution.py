from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.cli import _parser
from development_conveyor.errors import QueueError, RecoveryError
from development_conveyor.feature_decisions import FeatureDecisionResolver
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector

from tests.helpers import (
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


QUESTIONS = {
    "F006": (
        "Should F006 include recording consent, data retention, and "
        "session-data deletion, or remain limited to centralized permission "
        "and privacy visibility while F052 and F086 own those workflows?"
    ),
    "F011": (
        "Should F011 formally depend on F006 and F007 and run after them, "
        "or may test-owned contract fakes satisfy the F006 and F007 harness "
        "requirements before those production contracts are implemented?"
    ),
    "F097": (
        "What canonical schema and identity contract will durably store the "
        "imported-audio fingerprint, managed-copy provenance, and "
        "imported-audio artifact identity?"
    ),
}
RESOLUTIONS = {
    "F006": (
        "Keep F006 limited to centralized permission state and privacy "
        "visibility. F052 owns recording consent. F086 owns data retention "
        "and deletion."
    ),
    "F011": (
        "Add F006 and F007 as formal dependencies. Run F011 only after F006 "
        "and F007 are integrated."
    ),
    "F097": (
        "Approve an additive canonical imported-audio schema and identity "
        "contract with provider-neutral provenance, content-fingerprint "
        "deduplication, atomic publication after transcription, task-owned "
        "staging cleanup, deterministic interrupted-job handling, and "
        "legacy archive preservation."
    ),
}
SPECS = {
    "F006": "docs/features/F006-permission-and-privacy-center.md",
    "F010": "docs/features/F010-diagnostics-and-latency-instrumentation.md",
    "F011": "docs/features/F011-automated-test-harness.md",
    "F097": "docs/features/F097-imported-audio-transcription-workflow.md",
}


def _feature(
    feature_id: str,
    *,
    status: str,
    dependencies: list[str],
    integrated_commit: str | None = None,
) -> dict:
    decision = (
        {"question": QUESTIONS[feature_id], "evidence": ["synthetic evidence"]}
        if feature_id in QUESTIONS
        else None
    )
    value = {
        "id": feature_id,
        "title": f"Synthetic {feature_id}",
        "status": status,
        "priority": int(feature_id[1:]),
        "milestone": "M0",
        "dependencies": dependencies,
        "spec": SPECS.get(feature_id, f"docs/features/{feature_id}.md"),
        "acceptance_criteria": [f"{feature_id} remains specified."],
        "requires_human_decision": feature_id in QUESTIONS,
        "implementation_status": "Partial",
    }
    if decision:
        value["human_decision"] = decision
    if integrated_commit:
        value.update(
            {
                "status": "integrated",
                "requires_human_decision": False,
                "integrated_commit": integrated_commit,
                "integration_status": "passed",
            }
        )
    return value


def decision_fixture(root: Path):
    repository, project = synthetic_repository(root, feature_status="proposed")
    head = git(repository, "rev-parse", "HEAD")
    features = [
        _feature("F001", status="integrated", dependencies=[], integrated_commit=head),
        _feature("F002", status="integrated", dependencies=[], integrated_commit=head),
        _feature("F005", status="integrated", dependencies=[], integrated_commit=head),
        _feature(
            "F006",
            status="human_decision_required",
            dependencies=["F002", "F005"],
        ),
        _feature("F007", status="proposed", dependencies=["F001"]),
        _feature("F008", status="integrated", dependencies=[], integrated_commit=head),
        _feature("F009", status="integrated", dependencies=[], integrated_commit=head),
        _feature("F010", status="ready", dependencies=["F002"]),
        _feature(
            "F011",
            status="human_decision_required",
            dependencies=["F001", "F002"],
        ),
        _feature(
            "F097",
            status="human_decision_required",
            dependencies=["F005", "F008", "F009"],
        ),
    ]
    queue = {
        "schema_version": 1,
        "milestones": [
            {
                "id": "M0",
                "name": "Foundation",
                "status": "active",
                "base_commit": head,
                "integration_branch": "codex/m0-foundation",
                "integrated_features": ["F001", "F002", "F005", "F008", "F009"],
                "last_validated_commit": head,
                "human_gate": True,
            }
        ],
        "features": features,
    }
    write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
    for feature in features:
        path = repository / feature["spec"]
        path.parent.mkdir(parents=True, exist_ok=True)
        status = {
            "ready": "Ready",
            "human_decision_required": "Human decision required",
            "integrated": "Integrated",
        }.get(feature["status"], "Proposed")
        path.write_text(
            f"# {feature['id']} — Synthetic\n\n"
            f"- Factory status: {status}\n\n"
            "## Acceptance criteria\n\n- Synthetic criterion.\n",
            encoding="utf-8",
        )
    for relative in ("docs/ROADMAP.md",):
        (repository / relative).write_text("# Roadmap\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic decision baseline")
    head = git(repository, "rev-parse", "HEAD")
    project = replace(
        project,
        validated_baseline_commit=head,
        current_state="feature_ready",
        human_decision_gate={
            "gate_id": "historical-f002-gate",
            "reason": "Preserve this historical project-level record.",
        },
    )
    configuration = controller_configuration(root, project)
    inspector = RepositoryInspector(repository)
    identity = inspector.identity()
    state_root = configuration.owned_path(
        configuration.conveyor["state_directory"]
    ) / "projects" / project.project_id
    ledger = EvidenceLedger(
        state_root / "evidence-ledger.jsonl",
        project_id=project.project_id,
        repository_identity=identity["repository_id"],
        repository_path_fingerprint=identity["path_fingerprint"],
    )
    transaction_id = "synthetic-ready-transaction"
    ledger.append(
        event_type="TransactionStarted",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={
            "run_id": "synthetic-ready",
            "milestone": "M0",
            "feature_id": None,
        },
    )
    ledger.append(
        event_type="TransactionCompleted",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={
            "classification": "RECONCILED_READY_WORK",
            "next_state": "feature_ready",
            "feature_id": None,
            "selected_feature": "F010",
            "legacy_import": True,
        },
    )
    ledger.append(
        event_type="ProjectionUpdated",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={
            "current_state": "feature_ready",
            "current_feature": None,
            "selected_feature": "F010",
        },
    )
    projection = ProjectionEngine(
        ledger, state_root / "projection-cache.json"
    )
    projection.rebuild(persist_cache=True)
    decision = {
        "schema_version": 1,
        "project_id": project.project_id,
        "repository": {
            "path": str(repository.resolve()),
            "repository_id": identity["repository_id"],
            "path_fingerprint": identity["path_fingerprint"],
            "adapter_project_id": "synthetic",
        },
        "expected": {
            "branch": "codex/m0-foundation",
            "head": head,
            "projection_state": "feature_ready",
            "selected_feature": "F010",
        },
        "desired": {
            "selected_feature": "F097",
            "sole_ready_feature": "F097",
        },
        "features": [
            {
                "feature_id": "F006",
                "recorded_question": QUESTIONS["F006"],
                "approved_resolution": RESOLUTIONS["F006"],
                "target_status": "proposed",
            },
            {
                "feature_id": "F011",
                "recorded_question": QUESTIONS["F011"],
                "approved_resolution": RESOLUTIONS["F011"],
                "target_status": "proposed",
                "dependency_change": {
                    "expected": ["F001", "F002"],
                    "approved": ["F001", "F002", "F006", "F007"],
                },
            },
            {
                "feature_id": "F097",
                "recorded_question": QUESTIONS["F097"],
                "approved_resolution": RESOLUTIONS["F097"],
                "target_status": "ready",
                "required_integrated_dependencies": ["F005", "F008", "F009"],
            },
        ],
        "commit_subject": "factory: resolve M0 decisions and ready F097",
    }
    decision_path = root / "feature-decisions.json"
    write_json(decision_path, decision)
    return repository, project, configuration, ledger, decision_path


class FeatureDecisionResolutionTests(unittest.TestCase):
    def test_00_cli_requires_exactly_one_mode(self):
        common = [
            "resolve-feature-decisions",
            "--project",
            "synthetic",
            "--decision-file",
            "/tmp/decision.json",
            "--select-feature",
            "F097",
        ]
        with self.assertRaises(SystemExit):
            _parser().parse_args(common)
        with self.assertRaises(SystemExit):
            _parser().parse_args([*common, "--dry-run", "--apply"])

    def test_01_dry_run_makes_no_writes_and_launches_zero_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            resolver = FeatureDecisionResolver(configuration)
            repository_before = git(
                repository, "status", "--porcelain=v1", "-uall"
            )
            controller_before = {
                path: path.read_bytes()
                for path in configuration.root.rglob("*")
                if path.is_file()
            }
            request = resolver.inspect(
                project=project,
                decision_path=decision,
                selected_feature="F097",
            )
            plan = request.public_plan(dry_run=True)
            self.assertEqual("F097", plan["selected_feature"])
            self.assertEqual(0, plan["model_sessions_that_would_launch"])
            self.assertEqual(0, plan["child_sessions_that_would_launch"])
            self.assertTrue(plan["historical_project_gate_preserved"])
            self.assertEqual(
                repository_before,
                git(repository, "status", "--porcelain=v1", "-uall"),
            )
            self.assertEqual(
                controller_before,
                {
                    path: path.read_bytes()
                    for path in configuration.root.rglob("*")
                    if path.is_file()
                },
            )

    def test_02_apply_creates_one_planning_commit_and_final_consistency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, ledger, decision = decision_fixture(root)
            resolver = FeatureDecisionResolver(configuration)
            request = resolver.inspect(
                project=project,
                decision_path=decision,
                selected_feature="F097",
            )
            result = resolver.apply(request, run_id="decision-apply")
            commit = result["planning_result_commit"]
            self.assertEqual(request.expected_head, git(repository, "rev-parse", f"{commit}^"))
            self.assertEqual(
                1,
                int(git(repository, "rev-list", "--count", f"{request.expected_head}..{commit}")),
            )
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertFalse(result["feature_execution_started"])
            self.assertTrue(result["repository_clean"])
            queue = json.loads(
                (repository / "docs/FEATURE_QUEUE.yaml").read_text()
            )
            by_id = {item["id"]: item for item in queue["features"]}
            self.assertEqual("proposed", by_id["F006"]["status"])
            self.assertFalse(by_id["F006"]["requires_human_decision"])
            self.assertEqual(
                ["F001", "F002", "F006", "F007"],
                by_id["F011"]["dependencies"],
            )
            self.assertEqual("proposed", by_id["F010"]["status"])
            self.assertEqual("ready", by_id["F097"]["status"])
            self.assertEqual(
                ["F097"],
                [item["id"] for item in queue["features"] if item["status"] == "ready"],
            )
            projection = ProjectionEngine(
                ledger,
                configuration.owned_path("state")
                / "projects/synthetic/projection-cache.json",
            ).current()
            self.assertEqual("feature_ready", projection["current_state"])
            self.assertEqual("F097", projection["current_feature"])
            self.assertEqual("F097", projection["selected_next_feature"])
            self.assertIsNone(projection["active_transaction"])
            self.assertTrue(
                (repository / ".factory/conveyor-state.json").is_file()
            )
            event_types = [item["event_type"] for item in ledger.read()]
            self.assertIn("DeterministicExecutionStarted", event_types)
            self.assertNotIn("SessionLaunched", event_types)

    def test_02b_dry_run_refuses_stale_ledger_head_without_healing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, configuration, ledger, decision = decision_fixture(root)
            events = ledger.path.read_text(encoding="utf-8").splitlines()
            prior = json.loads(events[-2])
            write_json(
                ledger.head_path,
                {
                    "schema_version": 1,
                    "sequence": prior["sequence"],
                    "fingerprint": prior["fingerprint"],
                },
            )
            before = ledger.head_path.read_bytes()
            with self.assertRaisesRegex(RecoveryError, "stale durable ledger head"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )
            self.assertEqual(before, ledger.head_path.read_bytes())

    def test_03_exact_question_and_decision_gate_are_required(self):
        for mutation, diagnostic in (
            (
                lambda doc: doc["features"][0].update(
                    {"recorded_question": "Almost the same question?"}
                ),
                "question does not match",
            ),
            (
                lambda doc: doc["features"][0].update(
                    {"feature_id": "F007"}
                ),
                "not decision-gated",
            ),
        ):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, project, configuration, _, decision = decision_fixture(root)
                document = json.loads(decision.read_text())
                mutation(document)
                write_json(decision, document)
                with self.assertRaisesRegex(RecoveryError, diagnostic):
                    FeatureDecisionResolver(configuration).inspect(
                        project=project,
                        decision_path=decision,
                        selected_feature="F097",
                    )

    def test_04_stale_head_and_dirty_worktree_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            (repository / "later.txt").write_text("later\n", encoding="utf-8")
            git(repository, "add", "later.txt")
            git(repository, "commit", "-m", "later")
            with self.assertRaisesRegex(RecoveryError, "head"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            (repository / "app.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "clean"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )

    def test_05_active_transaction_or_writer_lease_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, configuration, ledger, decision = decision_fixture(root)
            ledger.append(
                event_type="TransactionStarted",
                transaction_id="active-transaction",
                workflow_type="feature_execution",
                payload={
                    "run_id": "active",
                    "milestone": "M0",
                    "feature_id": "F010",
                },
            )
            with self.assertRaisesRegex(RecoveryError, "active_transaction"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            lock = repository / ".factory/locks/writer.json"
            lock.parent.mkdir(parents=True, exist_ok=True)
            write_json(lock, {"synthetic": True})
            with self.assertRaisesRegex(RecoveryError, "writer_lease"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )

    def test_06_selected_feature_dependencies_must_be_integrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            queue_path = repository / "docs/FEATURE_QUEUE.yaml"
            queue = json.loads(queue_path.read_text())
            next(item for item in queue["features"] if item["id"] == "F009")[
                "status"
            ] = "proposed"
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "make dependency incomplete")
            document = json.loads(decision.read_text())
            document["expected"]["head"] = git(repository, "rev-parse", "HEAD")
            write_json(decision, document)
            with self.assertRaisesRegex(RecoveryError, "not integrated"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )

    def test_06b_previous_selected_feature_must_be_unstarted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, _, decision = decision_fixture(root)
            queue_path = repository / "docs/FEATURE_QUEUE.yaml"
            queue = json.loads(queue_path.read_text())
            next(item for item in queue["features"] if item["id"] == "F010")[
                "branch"
            ] = "codex/f010-started"
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "mark F010 prepared")
            document = json.loads(decision.read_text())
            document["expected"]["head"] = git(repository, "rev-parse", "HEAD")
            write_json(decision, document)
            with self.assertRaisesRegex(RecoveryError, "started or been prepared"):
                FeatureDecisionResolver(configuration).inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )

    def test_07_partial_failure_rolls_back_files_and_preserves_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, ledger, decision = decision_fixture(root)
            resolver = FeatureDecisionResolver(configuration)
            request = resolver.inspect(
                project=project,
                decision_path=decision,
                selected_feature="F097",
            )
            before = {
                path: (repository / path).read_bytes()
                for path in request.authorized_paths
            }
            with patch.object(
                resolver,
                "_validate_rendered",
                side_effect=QueueError("synthetic partial failure"),
            ):
                with self.assertRaisesRegex(QueueError, "partial failure"):
                    resolver.apply(request, run_id="decision-rollback")
            self.assertEqual(request.expected_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual("", git(repository, "status", "--porcelain=v1", "-uall"))
            self.assertEqual(
                before,
                {
                    path: (repository / path).read_bytes()
                    for path in request.authorized_paths
                },
            )
            projection = ProjectionEngine(
                ledger,
                configuration.owned_path("state")
                / "projects/synthetic/projection-cache.json",
            ).current()
            self.assertEqual("feature_ready", projection["current_state"])
            self.assertEqual("F010", projection["selected_next_feature"])
            self.assertIsNone(projection["active_transaction"])

    def test_08_successful_apply_is_not_idempotently_reapplied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, configuration, _, decision = decision_fixture(root)
            resolver = FeatureDecisionResolver(configuration)
            request = resolver.inspect(
                project=project,
                decision_path=decision,
                selected_feature="F097",
            )
            resolver.apply(request, run_id="decision-once")
            with self.assertRaisesRegex(RecoveryError, "head|decision-gated"):
                resolver.inspect(
                    project=project,
                    decision_path=decision,
                    selected_feature="F097",
                )

    def test_09_project_gate_resolver_cli_remains_separate(self):
        parsed = _parser().parse_args(
            [
                "reconcile",
                "--project",
                "synthetic",
                "--resolve-human-decision",
                "--reason",
                "Explicit historical gate approval",
                "--dry-run",
            ]
        )
        self.assertTrue(parsed.resolve_human_decision)
        self.assertFalse(
            hasattr(parsed, "decision_file") and parsed.decision_file
        )
