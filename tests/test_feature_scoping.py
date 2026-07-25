from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.cli import _parser
from development_conveyor.errors import QueueError, RecoveryError
from development_conveyor.feature_scoping import (
    FeatureScoper,
    _assert_acyclic,
    _brief_policy,
    _brief_sections,
)
from development_conveyor.kernel import WorkflowKernel
from development_conveyor.queue import FeatureQueue
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionPlan, SessionResult

from tests.helpers import controller_configuration, git, synthetic_repository, write_json


TARGETS = ("F006", "F007", "F008", "F009", "F010", "F011")
PROFILES = {
    "F006": "multi_module_precise",
    "F007": "generic_or_architectural",
    "F008": "multi_module_precise",
    "F009": "multi_module_precise",
    "F010": "bounded_precise",
    "F011": "generic_or_architectural",
    "F097": "multi_module_precise",
}
TITLES = {
    "F006": "Permission and Privacy Center",
    "F007": "AI Provider Abstraction",
    "F008": "Audio Engine Abstraction",
    "F009": "Transcription Engine Abstraction",
    "F010": "Diagnostics and Latency Instrumentation",
    "F011": "Automated Test Harness",
    "F097": "Imported Audio Transcription Workflow",
}


def scope_fixture(root: Path):
    repository, project = synthetic_repository(root, feature_status="proposed")
    queue = {
        "schema_version": 1,
        "milestones": [{
            "id": "M0",
            "name": "Foundation",
            "status": "active",
            "base_commit": None,
            "integration_branch": "codex/m0-foundation",
            "integrated_features": ["F005"],
            "last_validated_commit": None,
            "human_gate": True,
        }],
        "features": [{
            "id": "F005",
            "title": "Persistent Data Store",
            "status": "integrated",
            "priority": 5,
            "milestone": "M0",
            "dependencies": [],
            "spec": "docs/features/F005-persistent-data-store.md",
            "acceptance_criteria": ["Persistence is durable."],
            "requires_human_decision": False,
            "integrated_commit": git(repository, "rev-parse", "HEAD"),
            "integration_status": "passed",
        }],
    }
    (repository / "docs/features/F005-persistent-data-store.md").write_text(
        "# F005 — Persistent Data Store\n", encoding="utf-8"
    )
    for priority, feature_id in enumerate(TARGETS, 6):
        path = f"docs/features/{feature_id}-{TITLES[feature_id].lower().replace(' ', '-')}.md"
        queue["features"].append({
            "id": feature_id,
            "title": TITLES[feature_id],
            "status": "proposed",
            "priority": priority,
            "milestone": "M0",
            "dependencies": ["F005"] if feature_id == "F006" else [],
            "spec": path,
            "acceptance_criteria": [f"{feature_id} is specified."],
            "requires_human_decision": False,
            "integration_status": "pending",
        })
        (repository / path).write_text(
            f"# {feature_id} — {TITLES[feature_id]}\n\nBaseline.\n",
            encoding="utf-8",
        )
    write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
    (repository / "docs/ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    (repository / ".factory/locks").mkdir(parents=True, exist_ok=True)
    (repository / ".factory/locks/.gitignore").write_text(
        "writer.json\n", encoding="utf-8"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic scope baseline")
    baseline = git(repository, "rev-parse", "HEAD")
    project = project.__class__(
        **{
            **project.__dict__,
            "validated_baseline_commit": baseline,
            "current_state": "queue_reconciliation",
        }
    )
    brief = root / "interview-companion-m0-scope.md"
    blocks = []
    for feature_id in (*TARGETS, "F097"):
        dependencies = (
            "F005, F008, F009"
            if feature_id == "F097"
            else ("F005" if feature_id == "F006" else "none")
        )
        blocks.append(
            f"## {feature_id} — {TITLES[feature_id]}\n\n"
            "```yaml\n"
            "execution_policy:\n"
            f"  profile: {PROFILES[feature_id]}\n"
            "  parent_sessions: 1\n"
            "  child_sessions: 0\n"
            "```\n"
            f"Dependencies: {dependencies}\n\n"
            f"Define {TITLES[feature_id]}.\n"
        )
    brief.write_text("\n".join(blocks), encoding="utf-8")
    return repository, project, controller_configuration(root, project), brief


class ScopeLauncher:
    def __init__(self, *, unauthorized: str | None = None, omit_terminal: bool = False):
        self.requests = []
        self.unauthorized = unauthorized
        self.omit_terminal = omit_terminal

    def launch(self, request, on_session_started=None):
        self.requests.append(request)
        session_id = "scope-session"
        if on_session_started:
            on_session_started(session_id)
        repository = request.project.repository
        queue_path = repository / request.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        for feature in queue["features"]:
            feature_id = feature["id"]
            if feature_id not in PROFILES:
                continue
            feature["status"] = "ready" if feature_id == "F008" else "proposed"
            feature["execution_policy"] = {
                "profile": PROFILES[feature_id],
                "parent_sessions": 1,
                "child_sessions": 0,
            }
            if feature_id == "F008":
                feature["implementation_status"] = "Ready"
        queue["features"].append({
            "id": "F097",
            "title": TITLES["F097"],
            "status": "proposed",
            "priority": 97,
            "milestone": "M0",
            "dependencies": ["F005", "F008", "F009"],
            "spec": "docs/features/F097-imported-audio-transcription-workflow.md",
            "acceptance_criteria": ["Imported audio can be transcribed without live capture."],
            "requires_human_decision": False,
            "execution_policy": {
                "profile": PROFILES["F097"],
                "parent_sessions": 1,
                "child_sessions": 0,
            },
            "integration_status": "pending",
        })
        write_json(queue_path, queue)
        for feature_id in TARGETS:
            feature = next(item for item in queue["features"] if item["id"] == feature_id)
            (repository / feature["spec"]).write_text(
                f"# {feature_id} — {TITLES[feature_id]}\n\n"
                f"Execution policy: {PROFILES[feature_id]}\n",
                encoding="utf-8",
            )
        (repository / "docs/features/F097-imported-audio-transcription-workflow.md").write_text(
            "# F097 — Imported Audio Transcription Workflow\n\n"
            "Dependencies: F005, F008, F009\n",
            encoding="utf-8",
        )
        for relative in (
            "docs/CURRENT_STATUS.md",
            "docs/FEATURE_CATALOG.md",
            "docs/ROADMAP.md",
            "docs/RUN_LOG.md",
        ):
            (repository / relative).write_text(
                f"# Scoped inventory\n\nF008 Ready; F097 proposed.\n",
                encoding="utf-8",
            )
        if self.unauthorized:
            path = repository / self.unauthorized
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unauthorized\n", encoding="utf-8")
        changed = sorted(
            set(RepositoryInspector(repository).tracked_changed_paths())
            | set(RepositoryInspector(repository).untracked_file_hashes())
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
                "target_features": [*TARGETS, "F097"],
                "ready_feature": "F008",
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
            action="scope_features",
            returncode=0,
            session_id=session_id,
            redacted_output="",
            plan=SessionPlan(
                argv=("codex",),
                cwd=repository,
                prompt="scope",
                prompt_sha256="0" * 64,
                sandbox="workspace-write",
            ),
            structured_result=None if self.omit_terminal else envelope,
            structured_output_validation="invalid" if self.omit_terminal else "valid",
            result_classification=None if self.omit_terminal else "RECONCILED_READY_WORK",
            transaction_envelope=None if self.omit_terminal else envelope,
            terminal_marker_found=not self.omit_terminal,
            parsed_structured_result=None if self.omit_terminal else envelope,
        )


class FeatureScopingTests(unittest.TestCase):
    def test_00a_feature_headings_are_bounded_to_one_physical_line(self):
        brief = (
            "# F097 — Imported Audio Transcription Workflow\r\n\r\n"
            "## Dependencies\r\n\r\n"
            "- F005 — Persistent Data Store\r\n\r\n"
            "## Product scope\r\n"
            "Body\r\n"
            "## Acceptance criteria\r\n"
            "More body\r\n"
        )
        sections = _brief_sections(brief)
        self.assertEqual(["F097"], list(sections))
        self.assertEqual("Imported Audio Transcription Workflow", sections["F097"][0])
        self.assertIn("## Dependencies", sections["F097"][1])
        self.assertIn("## Product scope", sections["F097"][1])
        self.assertIn("## Acceptance criteria", sections["F097"][1])

    def test_00b_fenced_policy_preserves_budgets_and_optional_escalation(self):
        body = (
            "```yaml\n"
            "execution_policy:\n"
            "  profile: multi_module_precise\n"
            "  parent_sessions: 1\n"
            "  child_sessions: 0\n"
            "  escalation:\n"
            "    trigger: material_import_persistence_or_session_authority_ambiguity\n"
            "    profile: generic_or_architectural\n"
            "```\n"
        )
        self.assertEqual(
            {
                "profile": "multi_module_precise",
                "parent_sessions": 1,
                "child_sessions": 0,
                "escalation": {
                    "trigger": "material_import_persistence_or_session_authority_ambiguity",
                    "profile": "generic_or_architectural",
                },
            },
            _brief_policy("F097", body),
        )
        with self.assertRaisesRegex(QueueError, "exactly one"):
            _brief_policy("F097", body + "\n" + body)

    def test_00_cli_requires_exactly_one_mode(self):
        common = [
            "scope-features", "--project", "synthetic", "--feature", "F006",
            "--ready", "F006", "--brief", "/tmp/brief.md",
        ]
        with self.assertRaises(SystemExit):
            _parser().parse_args(common)
        with self.assertRaises(SystemExit):
            _parser().parse_args([*common, "--dry-run", "--apply"])

    def test_01_dry_run_is_exact_and_non_mutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, brief = scope_fixture(root)
            before = git(repository, "status", "--porcelain=v1", "-uall")
            controller_before = sorted(configuration.root.rglob("*"))
            scoper = FeatureScoper(configuration, ScopeLauncher())
            request = scoper.inspect(
                project=project,
                existing_features=TARGETS,
                new_features=("F097",),
                ready_feature="F008",
                brief_path=brief,
            )
            plan = request.public_plan(dry_run=True)
            self.assertEqual([*TARGETS, "F097"], plan["target_features"])
            self.assertEqual("F008", plan["ready_feature"])
            self.assertEqual(["F005", "F008", "F009"], plan["dependency_changes"]["F097"])
            self.assertEqual(
                {"model": "gpt-5.6-sol", "reasoning": "medium", "parent_sessions": 1, "child_sessions": 0},
                plan["execution"],
            )
            self.assertFalse(plan["feature_execution_will_start"])
            self.assertEqual(1, plan["model_sessions_that_would_launch"])
            self.assertEqual(before, git(repository, "status", "--porcelain=v1", "-uall"))
            self.assertEqual(controller_before, sorted(configuration.root.rglob("*")))

    def test_02_conflicting_new_id_and_malformed_dependencies_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, brief = scope_fixture(root)
            scoper = FeatureScoper(configuration, ScopeLauncher())
            queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
            queue["features"].append({
                "id": "F097", "title": "Conflict", "status": "proposed",
                "milestone": "M0", "dependencies": [], "acceptance_criteria": ["Conflict."],
            })
            write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
            with self.assertRaisesRegex(QueueError, "conflicts"):
                scoper.inspect(
                    project=project, existing_features=TARGETS,
                    new_features=("F097",), ready_feature="F008", brief_path=brief,
                )
            git(repository, "restore", "docs/FEATURE_QUEUE.yaml")
            invalid = brief.read_text(encoding="utf-8").replace(
                "profile: bounded_precise",
                "profile: invented_profile",
            )
            brief.write_text(invalid, encoding="utf-8")
            with self.assertRaisesRegex(QueueError, "unsupported profile"):
                scoper.inspect(
                    project=project, existing_features=TARGETS,
                    new_features=("F097",), ready_feature="F008", brief_path=brief,
                )

    def test_02b_dependency_cycles_and_missing_ids_fail_closed(self):
        document = {
            "schema_version": 1,
            "milestones": [{"id": "M0", "name": "M0", "status": "active"}],
            "features": [
                {
                    "id": "F006", "title": "Six", "status": "proposed",
                    "milestone": "M0", "dependencies": ["F008"],
                },
                {
                    "id": "F008", "title": "Eight", "status": "proposed",
                    "milestone": "M0", "dependencies": ["F006"],
                },
            ],
        }
        with self.assertRaisesRegex(QueueError, "cycle"):
            _assert_acyclic(FeatureQueue(document))
        document["features"][1]["dependencies"] = ["F999"]
        with self.assertRaisesRegex(QueueError, "unknown feature IDs"):
            FeatureQueue(document)

    def test_03_apply_commits_once_stops_ready_and_preserves_stable_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, brief = scope_fixture(root)
            launcher = ScopeLauncher()
            scoper = FeatureScoper(configuration, launcher)
            request = scoper.inspect(
                project=project, existing_features=TARGETS,
                new_features=("F097",), ready_feature="F008", brief_path=brief,
            )
            result = scoper.apply(request, run_id="scope-apply")
            self.assertEqual("feature_ready", result["next_state"])
            self.assertFalse(result["feature_execution_started"])
            self.assertEqual(1, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertTrue(result["repository_clean"])
            self.assertEqual("scope_features", launcher.requests[0].action)
            self.assertEqual(1, launcher.requests[0].parent_session_budget)
            self.assertEqual(0, launcher.requests[0].child_session_budget)
            self.assertEqual("gpt-5.6-sol", launcher.requests[0].planned_model)
            self.assertEqual("medium", launcher.requests[0].planned_reasoning)
            commit = result["planning_result_commit"]
            self.assertEqual(request.starting_head, git(repository, "rev-parse", f"{commit}^"))
            self.assertEqual(
                1,
                int(git(repository, "rev-list", "--count", f"{request.starting_head}..{commit}")),
            )
            queue = json.loads((repository / "docs/FEATURE_QUEUE.yaml").read_text())
            ids = [item["id"] for item in queue["features"]]
            self.assertEqual(1, ids.count("F097"))
            self.assertTrue(all(feature_id in ids for feature_id in TARGETS))
            ready = [item["id"] for item in queue["features"] if item["status"] == "ready"]
            self.assertEqual(["F008"], ready)
            self.assertTrue((repository / ".factory/conveyor-state.json").is_file())

    def test_04_production_and_test_paths_are_rejected(self):
        for unauthorized in (
            "app.txt",
            "tests/product_test.py",
            "docs/features/F012-unrequested.md",
        ):
            with self.subTest(unauthorized=unauthorized), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, project, configuration, brief = scope_fixture(root)
                scoper = FeatureScoper(configuration, ScopeLauncher(unauthorized=unauthorized))
                request = scoper.inspect(
                    project=project, existing_features=TARGETS,
                    new_features=("F097",), ready_feature="F008", brief_path=brief,
                )
                with self.assertRaisesRegex(Exception, "unauthorized|mutation"):
                    scoper.apply(request, run_id="scope-rejected")

    def test_05_unrelated_dirty_file_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, brief = scope_fixture(root)
            launcher = ScopeLauncher()
            scoper = FeatureScoper(configuration, launcher)
            request = scoper.inspect(
                project=project, existing_features=TARGETS,
                new_features=("F097",), ready_feature="F008", brief_path=brief,
            )
            (repository / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "clean repository"):
                scoper.apply(request)
            self.assertEqual([], launcher.requests)

    def test_06_exact_retained_diff_recovery_commits_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, configuration, brief = scope_fixture(root)
            scoper = FeatureScoper(configuration, ScopeLauncher())
            request = scoper.inspect(
                project=project, existing_features=TARGETS,
                new_features=("F097",), ready_feature="F008", brief_path=brief,
            )
            with patch.object(WorkflowKernel, "finalize", side_effect=RecoveryError("injected finalization stop")):
                with self.assertRaisesRegex(RecoveryError, "injected"):
                    scoper.apply(request, run_id="scope-retained")
            self.assertFalse(RepositoryInspector(repository).is_clean)
            recovery_plan = scoper.inspect_recovery(project=project, run_id="scope-retained")
            self.assertEqual(0, recovery_plan["model_sessions_that_would_launch"])
            recovered = scoper.recover(recovery_plan)
            self.assertEqual("planning_recovery_committed", recovered["outcome"])
            self.assertEqual(0, recovered["model_sessions_launched"])
            self.assertEqual(0, recovered["child_sessions_launched"])
            self.assertFalse(recovered["feature_execution_started"])
            self.assertTrue(recovered["repository_clean"])


if __name__ == "__main__":
    unittest.main()
