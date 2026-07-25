from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.cli import _parser
from development_conveyor.errors import QueueError, RecoveryError, SessionError
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.product_plan import (
    PRODUCT_PLAN_MODEL,
    PRODUCT_PLAN_REASONING,
    REQUIRED_FEATURES,
    ProductPlanReconciler,
    brief_identity,
    planning_path_allowed,
    validate_compatibility_language,
)
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan, SessionResult

from tests.helpers import (
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


class ProductPlanLauncher:
    def __init__(self, *, unauthorized: str | None = None, malformed: bool = False):
        self.requests = []
        self.unauthorized = unauthorized
        self.malformed = malformed

    def launch(self, request, on_session_started=None):
        self.requests.append(request)
        session_id = "product-plan-session"
        if on_session_started:
            on_session_started(session_id)
        repository = request.project.repository
        queue_path = repository / request.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        for feature in queue["features"]:
            if feature["id"] == "F068":
                feature["status"] = "ready"
                feature["execution_policy"] = {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 0,
                }
            elif feature["id"] not in {"F001", "F097"}:
                feature["status"] = "proposed"
            if feature["id"] in {"F019", "F041"}:
                feature["dependencies"] = ["F078"]
        write_json(queue_path, queue)

        semantic = {
            "F019": "application round question",
            "F020": "guided practice with an AI interviewer",
            "F021": "question library for practice and live",
            "F029": "Suggested Answer Your Answer Feedback Improved Answer",
            "F041": "application round audio setup",
            "F044": "speaker separation for practice live and imported audio",
            "F058": "transcript highlights and comments",
            "F060": "Export Transcript action",
            "F068": "application owns every round and session",
            "F073": "application has multiple rounds and sessions",
            "F078": "application round linkage for each session",
        }
        for feature in queue["features"]:
            feature_id = feature["id"]
            if feature_id == "F097":
                continue
            text = semantic.get(feature_id, "Approved product planning.")
            if feature_id in {"F061", "F062", "F063", "F064", "F065", "F066"}:
                text += " score feedback improved answer retry"
            (repository / feature["spec"]).write_text(
                f"# {feature_id}\n\n{text}\n", encoding="utf-8"
            )

        ids = [feature["id"] for feature in queue["features"]]
        (repository / "docs/FEATURE_CATALOG.md").write_text(
            "# Feature Catalog\n\n" + "\n".join(f"| {feature_id} |" for feature_id in ids) + "\n",
            encoding="utf-8",
        )
        for relative in (
            "PRODUCT_VISION.md", "ROADMAP.md", "docs/ROADMAP.md",
            "docs/CURRENT_STATUS.md",
        ):
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# Approved direction\n\nPractice and Live sessions belong to an application and round.\n",
                encoding="utf-8",
            )
        (repository / "docs/RUN_LOG.md").write_text(
            "# Run Log\n\nproduct-plan reconciliation; "
            f"{PRODUCT_PLAN_MODEL}; {PRODUCT_PLAN_REASONING}; one parent; zero children.\n",
            encoding="utf-8",
        )
        if self.unauthorized:
            path = repository / self.unauthorized
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("forbidden\n", encoding="utf-8")

        inspector = RepositoryInspector(repository)
        changed = sorted(
            set(inspector.tracked_changed_paths())
            | set(inspector.untracked_file_hashes())
        )
        envelope = {
            "schema_version": 1,
            "workflow_type": "queue_reconciliation",
            "classification": "RECONCILED_READY_WORK",
            "project_id": request.project.project_id,
            "repository_identity": request.repository_identity,
            "transaction_id": request.transaction_id,
            "run_id": request.run_id,
            "session_id": session_id,
            "starting_branch": request.starting_branch,
            "starting_commit": request.starting_commit,
            "current_commit": request.starting_commit,
            "feature_id": None,
            "changed_paths": changed,
            "evidence": {
                "approved_brief": True,
                "f097_integrated": True,
                "ready_features": ["F068"],
                "feature_execution_started": False,
                "queue_validation": {
                    "valid": True,
                    "milestone_found": True,
                    "feature_count": len(queue["features"]),
                    "global_feature_count": len(queue["features"]),
                    "global_milestone_count": len(queue["milestones"]),
                    "warning_count": 0,
                },
            },
            "next_state": "feature_ready",
        }
        return SessionResult(
            action="reconcile_product_plan",
            returncode=0,
            session_id=session_id,
            redacted_output="",
            plan=SessionPlan(
                argv=("codex",), cwd=repository, prompt="product-plan",
                prompt_sha256="0" * 64, sandbox="workspace-write",
            ),
            structured_result=None if self.malformed else envelope,
            structured_output_validation="invalid" if self.malformed else "valid",
            result_classification=None if self.malformed else "RECONCILED_READY_WORK",
            transaction_envelope=None if self.malformed else envelope,
            terminal_marker_found=not self.malformed,
        )


def product_plan_fixture(root: Path):
    repository, project = synthetic_repository(root, feature_status="proposed")
    (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
    (repository / ".factory/locks/.gitignore").write_text("writer.json\n", encoding="utf-8")
    base = git(repository, "rev-parse", "HEAD")
    features = [{
        "id": "F001", "title": "Foundation", "status": "integrated",
        "priority": 1, "milestone": "M0", "dependencies": [],
        "spec": "docs/features/F001.md",
        "acceptance_criteria": ["Foundation."], "requires_human_decision": False,
        "accepted_commit": base, "integrated_commit": base,
        "integration_status": "passed",
    }]
    for priority, feature_id in enumerate(sorted(REQUIRED_FEATURES), 2):
        integrated = feature_id == "F097"
        dependencies = ["F001"]
        if feature_id == "F078":
            dependencies = ["F097"]
        feature = {
            "id": feature_id,
            "title": feature_id,
            "status": "integrated" if integrated else "proposed",
            "priority": priority,
            "milestone": "M0",
            "dependencies": dependencies,
            "spec": f"docs/features/{feature_id}.md",
            "acceptance_criteria": [f"{feature_id} planned."],
            "requires_human_decision": False,
            "integration_status": "passed" if integrated else "pending",
        }
        if integrated:
            feature.update({"accepted_commit": base, "integrated_commit": base})
        features.append(feature)
        (repository / feature["spec"]).write_text(
            f"# {feature_id}\n\nBaseline.\n", encoding="utf-8"
        )
    queue = {
        "schema_version": 1,
        "milestones": [{
            "id": "M0", "name": "Foundation", "status": "active",
            "base_commit": base, "integration_branch": "codex/m0-foundation",
            "integrated_features": ["F001", "F097"],
            "last_validated_commit": base, "human_gate": True,
        }],
        "features": features,
    }
    write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
    for relative in ("PRODUCT_VISION.md", "ROADMAP.md", "docs/ROADMAP.md"):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Baseline\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic post-F097 planning baseline")
    head = git(repository, "rev-parse", "HEAD")
    project = replace(
        project,
        validated_baseline_commit=head,
        current_state="queue_reconciliation",
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
    transaction_id = "post-f097-topology"
    ledger.append(
        event_type="TransactionStarted",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={"run_id": "post-f097", "milestone": "M0", "feature_id": None},
    )
    ledger.append(
        event_type="TransactionCompleted",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={
            "classification": "RECONCILED_NO_READY_WORK",
            "next_state": "queue_reconciliation",
            "feature_id": None,
            "selected_feature": None,
            "legacy_import": True,
        },
    )
    ledger.append(
        event_type="ProjectionUpdated",
        transaction_id=transaction_id,
        workflow_type="queue_reconciliation",
        payload={
            "current_state": "queue_reconciliation",
            "current_feature": None,
            "selected_feature": None,
        },
    )
    ProjectionEngine(
        ledger, state_root / "projection-cache.json"
    ).rebuild(persist_cache=True)
    brief = root / "approved-product-plan.md"
    brief.write_text(
        "# Approved product plan\n\nRemove Test Call. Preserve legacy simulation decoding.\n",
        encoding="utf-8",
    )
    return repository, project, configuration, brief


class ProductPlanReconciliationTests(unittest.TestCase):
    def test_00_cli_help_and_exact_mode_enforcement(self):
        common = [
            "reconcile-product-plan", "--project", "synthetic",
            "--brief", "/tmp/brief.md",
        ]
        with self.assertRaises(SystemExit):
            _parser().parse_args(common)
        with self.assertRaises(SystemExit):
            _parser().parse_args([*common, "--dry-run", "--apply"])
        parsed = _parser().parse_args([*common, "--dry-run"])
        self.assertTrue(parsed.dry_run)

    def test_01_brief_identity_hashes_exact_regular_file_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief = root / "brief.md"
            brief.write_bytes(b"approved\r\nbrief\n")
            path, payload, identity = brief_identity(brief)
            self.assertEqual(brief, path)
            self.assertEqual(b"approved\r\nbrief\n", payload)
            self.assertEqual(len(payload), identity["size"])
            self.assertNotEqual(identity["sha256"], identity["normalized_sha256"])
            link = root / "brief-link.md"
            os.symlink(brief, link)
            with self.assertRaisesRegex(QueueError, "non-symlink"):
                brief_identity(link)

    def test_02_dry_run_is_zero_model_and_reports_full_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            launcher = ProductPlanLauncher()
            before = git(repository, "status", "--porcelain=v1", "-uall")
            request = ProductPlanReconciler(configuration, launcher).inspect(
                project=project, brief_path=brief
            )
            plan = request.public_plan(dry_run=True)
            self.assertEqual(0, plan["model_sessions_that_would_launch"])
            self.assertEqual(0, plan["child_sessions_that_would_launch"])
            self.assertEqual(1, plan["maximum_parent_sessions_on_apply"])
            self.assertEqual(
                {"model": "gpt-5.6-sol", "reasoning": "medium",
                 "parent_sessions": 1, "child_sessions": 0, "planning_only": True},
                plan["apply_execution_policy"],
            )
            self.assertEqual(["queue_reconciliation", "feature_ready"],
                             plan["intended_transaction"]["state_path"])
            self.assertFalse(plan["application_tests_will_run"])
            self.assertEqual([], launcher.requests)
            self.assertEqual(before, git(repository, "status", "--porcelain=v1", "-uall"))

    def test_03_planning_allowlist_rejects_source_tests_and_runtime(self):
        self.assertTrue(planning_path_allowed("PRODUCT_VISION.md"))
        self.assertTrue(planning_path_allowed("docs/features/F019.md"))
        self.assertTrue(planning_path_allowed("docs/adr/0002-plan.md"))
        for path in ("Sources/App.swift", "Tests/AppTests.swift",
                     ".factory/conveyor-state.json", "Package.swift"):
            self.assertFalse(planning_path_allowed(path))

    def test_04_test_call_and_simulation_are_compatibility_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            path = repository / "docs/ROADMAP.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "Test Call is removed. Legacy .simulation decoding remains for compatibility.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                0,
                validate_compatibility_language(
                    repository, ("docs/ROADMAP.md",)
                )["test_call_user_facing_requirements"],
            )
            path.write_text("Build a Test Call simulator UI.\n", encoding="utf-8")
            with self.assertRaisesRegex(QueueError, "Test Call"):
                validate_compatibility_language(repository, ("docs/ROADMAP.md",))

    def test_05_apply_creates_one_planning_commit_and_transactional_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            launcher = ProductPlanLauncher()
            reconciler = ProductPlanReconciler(configuration, launcher)
            request = reconciler.inspect(project=project, brief_path=brief)
            result = reconciler.apply(request, run_id="product-plan-apply")
            self.assertEqual("feature_ready", result["next_state"])
            self.assertEqual("F068", result["selected_feature"])
            self.assertEqual(1, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertFalse(result["feature_execution_started"])
            self.assertFalse(result["integration_started"])
            self.assertEqual(0, result["application_tests_run"])
            self.assertEqual("reconcile_product_plan", launcher.requests[0].action)
            self.assertEqual(1, launcher.requests[0].parent_session_budget)
            self.assertEqual(0, launcher.requests[0].child_session_budget)
            commit = result["planning_result_commit"]
            self.assertEqual(request.starting_head, git(repository, "rev-parse", f"{commit}^"))
            self.assertEqual(
                1,
                int(git(repository, "rev-list", "--count", f"{request.starting_head}..{commit}")),
            )
            self.assertTrue((repository / ".factory/conveyor-state.json").is_file())
            self.assertTrue(
                (configuration.root / "reports/product-plan-apply/approved-brief.md").is_file()
            )
            self.assertGreater(result["ledger_sequence"], request.ledger_sequence)
            self.assertTrue(RepositoryInspector(repository).is_clean)
            queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
            ready = [item["id"] for item in queue["features"] if item["status"] == "ready"]
            self.assertEqual(["F068"], ready)

    def test_06_source_or_test_mutation_is_rejected_and_lease_released(self):
        for unauthorized in ("Sources/App.swift", "Tests/AppTests.swift"):
            with self.subTest(path=unauthorized), tempfile.TemporaryDirectory() as temporary:
                repository, project, configuration, brief = product_plan_fixture(Path(temporary))
                reconciler = ProductPlanReconciler(
                    configuration, ProductPlanLauncher(unauthorized=unauthorized)
                )
                request = reconciler.inspect(project=project, brief_path=brief)
                with self.assertRaisesRegex(Exception, "denied|unauthorized"):
                    reconciler.apply(request, run_id="product-plan-rejected")
                self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_07_f097_and_feature_ids_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            launcher = ProductPlanLauncher()
            reconciler = ProductPlanReconciler(configuration, launcher)
            request = reconciler.inspect(project=project, brief_path=brief)
            launcher.launch(
                type("Request", (), {
                    "project": project, "repository_identity": request.repository_identity["repository_id"],
                    "transaction_id": "t", "run_id": "r", "starting_branch": request.starting_branch,
                    "starting_commit": request.starting_head,
                })()
            )
            changed = tuple(sorted(RepositoryInspector(repository).tracked_changed_paths()))
            evidence = reconciler.validate_result(request, changed)
            self.assertTrue(evidence["f097_integrated_evidence_preserved"])
            self.assertEqual(["F068"], evidence["ready_features"])
            queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
            f097 = next(item for item in queue["features"] if item["id"] == "F097")
            f097["acceptance_criteria"].append("Broadened.")
            write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
            with self.assertRaisesRegex(QueueError, "F097"):
                reconciler.validate_result(
                    request,
                    tuple(sorted(RepositoryInspector(repository).tracked_changed_paths())),
                )

    def test_08_malformed_result_retains_exact_recovery_evidence_and_zero_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            launcher = ProductPlanLauncher(malformed=True)
            reconciler = ProductPlanReconciler(configuration, launcher)
            request = reconciler.inspect(project=project, brief_path=brief)
            with self.assertRaisesRegex(SessionError, "typed terminal result"):
                reconciler.apply(request, run_id="product-plan-malformed")
            record = json.loads(
                (configuration.root
                 / "reports/product-plan-malformed/planning-transaction.json").read_text()
            )
            self.assertEqual("planning_validation_failed", record["status"])
            self.assertTrue(record["diff_fingerprint"])
            self.assertTrue(record["changed_file_sha256"])
            self.assertIn("recover-product-plan", record["recover_command"])
            self.assertEqual(0, launcher.requests[0].child_session_budget)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            recovery_plan = reconciler.inspect_recovery(
                project=project, run_id="product-plan-malformed"
            )
            self.assertEqual(
                0, recovery_plan["model_sessions_that_would_launch"]
            )
            recovered = reconciler.recover(recovery_plan)
            self.assertEqual(
                "planning_recovery_committed", recovered["outcome"]
            )
            self.assertEqual(0, recovered["model_sessions_launched"])
            self.assertEqual(0, recovered["child_sessions_launched"])
            self.assertFalse(recovered["feature_execution_started"])
            self.assertTrue(recovered["repository_clean"])

    def test_09_unstable_topology_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            (repository / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            launcher = ProductPlanLauncher()
            with self.assertRaisesRegex(RecoveryError, "stable post-F097"):
                ProductPlanReconciler(configuration, launcher).inspect(
                    project=project, brief_path=brief
                )
            self.assertEqual([], launcher.requests)

    def test_10_dependency_cycle_and_brief_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            reconciler = ProductPlanReconciler(configuration, ProductPlanLauncher())
            request = reconciler.inspect(project=project, brief_path=brief)
            brief.write_text("# Changed approved brief\n", encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "brief identity changed"):
                reconciler.apply(request, run_id="brief-drift")

        with tempfile.TemporaryDirectory() as temporary:
            repository, project, configuration, brief = product_plan_fixture(Path(temporary))
            launcher = ProductPlanLauncher()
            reconciler = ProductPlanReconciler(configuration, launcher)
            request = reconciler.inspect(project=project, brief_path=brief)
            launcher.launch(
                type("Request", (), {
                    "project": project,
                    "repository_identity": request.repository_identity["repository_id"],
                    "transaction_id": "t", "run_id": "r",
                    "starting_branch": request.starting_branch,
                    "starting_commit": request.starting_head,
                })()
            )
            queue_path = repository / "docs/FEATURE_QUEUE.yaml"
            queue = json.loads(queue_path.read_text())
            by_id = {item["id"]: item for item in queue["features"]}
            by_id["F019"]["dependencies"] = ["F020"]
            by_id["F020"]["dependencies"] = ["F019"]
            write_json(queue_path, queue)
            with self.assertRaisesRegex(QueueError, "cycle"):
                reconciler.validate_result(
                    request,
                    tuple(sorted(RepositoryInspector(repository).tracked_changed_paths())),
                )


if __name__ == "__main__":
    unittest.main()
