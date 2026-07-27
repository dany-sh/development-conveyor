from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.cli import _parser
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import QueueError, SessionError
from development_conveyor.queue import FeatureQueue
from development_conveyor.queue_control import prioritize, queue_report
from tests.helpers import (
    SyntheticLauncher,
    controller_configuration,
    git,
    synthetic_repository,
)


def add_feature(
    repository: Path,
    project,
    *,
    feature_id: str = "F002",
    status: str = "ready",
    dependencies: list[str] | None = None,
) -> None:
    spec = f"docs/features/{feature_id}.md"
    (repository / spec).write_text(
        f"# {feature_id}\n\n- [ ] Complete {feature_id}.\n",
        encoding="utf-8",
    )
    path = repository / project.queue_location
    document = json.loads(path.read_text(encoding="utf-8"))
    document["features"].append(
        {
            "id": feature_id,
            "title": f"Synthetic {feature_id}",
            "status": status,
            "priority": 2,
            "milestone": "M0",
            "dependencies": dependencies or [],
            "spec": spec,
            "acceptance_criteria": [f"{feature_id} is complete"],
            "requires_human_decision": False,
            "branch": None,
            "integration_base_commit": None,
            "accepted_commit": None,
            "integrated_commit": None,
            "integration_status": "pending",
            "integration_fix_commits": [],
        }
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", f"add {feature_id} fixture")


class QueueControlTests(unittest.TestCase):
    def test_queue_listing_is_deterministic_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            add_feature(repository, project, status="blocked")
            configuration = controller_configuration(Path(temporary), project)
            before_head = git(repository, "rev-parse", "HEAD")
            before_status = git(repository, "status", "--porcelain=v1")
            first = queue_report(configuration, project, runtime={})
            second = queue_report(configuration, project, runtime={})
            self.assertEqual(first, second)
            self.assertEqual(["F001", "F002"], [
                item["feature_id"] for item in first["features"]
            ])
            self.assertEqual([1, 2], [
                item["priority_position"] for item in first["features"]
            ])
            self.assertEqual("F001", first["next_ready_feature"])
            self.assertEqual(0, first["model_sessions_launched"])
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_status, git(repository, "status", "--porcelain=v1"))

    def test_queue_order_is_the_stable_selection_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            add_feature(repository, project)
            path = repository / project.queue_location
            document = json.loads(path.read_text(encoding="utf-8"))
            document["features"][0]["priority"] = 999
            document["features"][1]["priority"] = 1
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            queue = FeatureQueue.from_location(repository, project.queue_location)
            self.assertEqual("F001", queue.select_next("M0").feature_id)

    def test_dependency_blocked_feature_reports_exact_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(
                Path(temporary), feature_status="proposed"
            )
            add_feature(repository, project, dependencies=["F001"])
            queue = FeatureQueue.from_location(repository, project.queue_location)
            readiness = queue.readiness(queue.feature("F002") or {})
            self.assertFalse(readiness["ready"])
            self.assertEqual(
                "dependencies incomplete: F001", readiness["blocked_reason"]
            )

    def test_explicit_eligible_selection_does_not_reorder_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            add_feature(repository, project)
            project = replace(project, current_state="queue_reconciliation")
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            before = (repository / project.queue_location).read_bytes()
            result = engine._deterministic_queue_selection(
                project,
                mode="one_feature",
                requested_feature="F002",
            )
            self.assertEqual("F002", result["selected_feature"])
            self.assertTrue(result["explicit_feature"])
            self.assertFalse(result["queue_reordered"])
            self.assertEqual(before, (repository / project.queue_location).read_bytes())
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertFalse(
                (repository / ".factory/locks/writer.json").exists()
            )

    def test_explicit_ineligible_selection_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(
                Path(temporary), feature_status="proposed"
            )
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            before_head = git(repository, "rev-parse", "HEAD")
            before_status = git(repository, "status", "--porcelain=v1")
            with self.assertRaisesRegex(QueueError, "ineligible"):
                engine._deterministic_queue_selection(
                    project,
                    mode="one_feature",
                    requested_feature="F001",
                )
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_status, git(repository, "status", "--porcelain=v1"))
            self.assertFalse(
                (configuration.root / "state/projects/synthetic.json").exists()
            )
            self.assertFalse(
                (repository / ".factory/locks/writer.json").exists()
            )

    def test_exact_model_and_capability_preflight_precedes_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            configuration = controller_configuration(Path(temporary), project)
            launcher = SyntheticLauncher()

            def reject_preflight(_request):
                raise SessionError("synthetic exact-capability rejection")

            launcher.plan = reject_preflight
            before_head = git(repository, "rev-parse", "HEAD")
            before_status = git(repository, "status", "--porcelain=v1")
            with self.assertRaisesRegex(
                SessionError, "synthetic exact-capability rejection"
            ):
                CycleEngine(configuration, launcher)._deterministic_queue_selection(
                    project,
                    mode="one_feature",
                    requested_feature="F001",
                )
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(before_status, git(repository, "status", "--porcelain=v1"))
            self.assertFalse(
                (configuration.root / "state/projects/synthetic.json").exists()
            )
            self.assertFalse(
                (repository / ".factory/locks/writer.json").exists()
            )

    def test_no_ready_work_starts_no_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(
                Path(temporary), feature_status="proposed"
            )
            configuration = controller_configuration(Path(temporary), project)
            engine = CycleEngine(configuration, SyntheticLauncher())
            result = engine._deterministic_queue_selection(
                project, mode="resume"
            )
            self.assertEqual("no_ready_work", result["outcome"])
            self.assertFalse(result["transaction_started"])
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertFalse(
                (configuration.root / "state/projects/synthetic/evidence-ledger.jsonl").exists()
            )

    def test_resume_selects_ready_work_without_launching_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            project = replace(project, current_state="queue_reconciliation")
            configuration = controller_configuration(Path(temporary), project)
            launcher = SyntheticLauncher()
            result = CycleEngine(configuration, launcher).run_project(
                project, "resume"
            )
            self.assertEqual("feature_ready", result["outcome"])
            self.assertEqual("F001", result["selected_feature"])
            self.assertEqual([], launcher.actions)
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])
            self.assertEqual("", git(repository, "status", "--porcelain=v1"))

    def test_prioritize_before_and_after_changes_only_queue_order(self):
        for placement, expected in (
            ("before", ["F002", "F001"]),
            ("after", ["F002", "F001"]),
        ):
            with self.subTest(placement=placement):
                with tempfile.TemporaryDirectory() as temporary:
                    repository, project = synthetic_repository(Path(temporary))
                    add_feature(repository, project)
                    configuration = controller_configuration(Path(temporary), project)
                    if placement == "after":
                        feature_id, relative_id = "F001", "F002"
                    else:
                        feature_id, relative_id = "F002", "F001"
                    before_document = json.loads(
                        (repository / project.queue_location).read_text(
                            encoding="utf-8"
                        )
                    )
                    result = prioritize(
                        configuration,
                        project,
                        feature_id=feature_id,
                        relative_id=relative_id,
                        placement=placement,
                    )
                    queue = json.loads(
                        (repository / project.queue_location).read_text(encoding="utf-8")
                    )
                    self.assertEqual(expected, [
                        item["id"] for item in queue["features"]
                    ])
                    self.assertEqual(
                        {
                            item["id"]: item
                            for item in before_document["features"]
                        },
                        {
                            item["id"]: item
                            for item in queue["features"]
                        },
                    )
                    self.assertEqual(
                        [project.queue_location],
                        git(repository, "status", "--porcelain=v1").split(maxsplit=1)[1:],
                    )
                    self.assertEqual(0, result["status_changes"])
                    self.assertFalse(
                        (repository / ".factory/locks/writer.json").exists()
                    )

    def test_cli_parser_accepts_queue_controls_and_explicit_run(self):
        self.assertEqual(
            "queue",
            _parser().parse_args(["queue", "--project", "synthetic"]).command,
        )
        prioritize_args = _parser().parse_args(
            [
                "prioritize",
                "--project",
                "synthetic",
                "--feature",
                "F002",
                "--before",
                "F001",
            ]
        )
        self.assertEqual("F001", prioritize_args.before)
        run_args = _parser().parse_args(
            ["run", "--project", "synthetic", "--feature", "F002"]
        )
        self.assertEqual("F002", run_args.feature)
