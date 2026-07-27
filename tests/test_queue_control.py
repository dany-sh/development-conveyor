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
from development_conveyor.errors import ConveyorError
from development_conveyor.queue_control import (
    prioritize,
    queue_report,
    transition_backlog,
    transition_ready,
)
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
    milestone: str = "M0",
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
            "milestone": milestone,
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
    def _add_second_milestone(self, repository: Path, project) -> None:
        path = repository / project.queue_location
        document = json.loads(path.read_text(encoding="utf-8"))
        document["milestones"].append({
            "id": "M1",
            "name": "Future milestone",
            "status": "planned",
            "base_commit": None,
            "integration_branch": "codex/m1-foundation",
            "integrated_features": [],
            "last_validated_commit": None,
            "human_gate": True,
        })
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

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

    def test_queue_scopes_include_all_features_without_changing_active_execution_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            self._add_second_milestone(repository, project)
            add_feature(repository, project, feature_id="F002", status="done")
            add_feature(repository, project, feature_id="F003", status="ready", milestone="M1")
            add_feature(repository, project, feature_id="F004", status="integrated", milestone="M1")
            configuration = controller_configuration(Path(temporary), project)
            before = (repository / project.queue_location).read_bytes()

            active = queue_report(configuration, project, scope="active")
            unfinished = queue_report(configuration, project, scope="unfinished")
            all_features = queue_report(configuration, project, scope="all")

            self.assertEqual(["F001", "F002"], [item["feature_id"] for item in active["features"]])
            self.assertEqual(["F001", "F003"], [item["feature_id"] for item in unfinished["features"]])
            self.assertEqual(["F001", "F002", "F003", "F004"], [item["feature_id"] for item in all_features["features"]])
            self.assertEqual("F001", all_features["next_ready_feature"])
            self.assertEqual(4, all_features["total_feature_count"])
            self.assertEqual(2, all_features["terminal_feature_count"])
            self.assertEqual(0, all_features["model_sessions_launched"])
            self.assertEqual(before, (repository / project.queue_location).read_bytes())

    def test_queue_scope_milestone_filter_and_future_execution_reason_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            self._add_second_milestone(repository, project)
            add_feature(repository, project, feature_id="F002", milestone="M1")
            configuration = controller_configuration(Path(temporary), project)
            before_head = git(repository, "rev-parse", "HEAD")
            report = queue_report(configuration, project, scope="all", requested_milestone="M1")
            feature = report["features"][0]

            self.assertEqual("M1", report["requested_milestone"])
            self.assertEqual("M1", feature["milestone"])
            self.assertFalse(feature["active_milestone_member"])
            self.assertFalse(feature["execution_eligible"])
            self.assertIn("active milestone M0", feature["execution_ineligible_reason"])
            self.assertEqual("M1", next(item["milestone_id"] for item in report["milestones"] if item["active"] is False))
            self.assertEqual(before_head, git(repository, "rev-parse", "HEAD"))

    def test_unknown_milestone_is_a_stable_structured_read_only_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            report = queue_report(
                controller_configuration(Path(temporary), project), project,
                scope="all", requested_milestone="missing",
            )
            self.assertEqual("unknown_milestone", report["classification"])
            self.assertEqual("unknown_milestone", report["error"]["code"])
            self.assertEqual(0, report["model_sessions_launched"])

    def test_priority_then_queue_order_is_the_stable_selection_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            add_feature(repository, project)
            path = repository / project.queue_location
            document = json.loads(path.read_text(encoding="utf-8"))
            document["features"][0]["priority"] = "P3"
            document["features"][1]["priority"] = "P1"
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            queue = FeatureQueue.from_location(repository, project.queue_location)
            self.assertEqual("F002", queue.select_next("M0").feature_id)

    def test_missing_priority_defaults_to_p2_without_rewriting_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            path = repository / project.queue_location
            document = json.loads(path.read_text(encoding="utf-8"))
            document["features"][0].pop("priority")
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            before = path.read_bytes()
            report = queue_report(controller_configuration(Path(temporary), project), project)
            self.assertEqual("P2", report["features"][0]["priority"])
            self.assertEqual(before, path.read_bytes())

    def test_priority_does_not_bypass_incomplete_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            add_feature(repository, project, feature_id="F002", status="proposed", dependencies=["F001"])
            path = repository / project.queue_location
            document = json.loads(path.read_text(encoding="utf-8"))
            document["features"][1]["priority"] = "P1"
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            queue = FeatureQueue.from_location(repository, project.queue_location)
            self.assertIsNone(queue.select_next("M0"))

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

    def test_prioritize_updates_priority_and_order_in_one_queue_only_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            add_feature(repository, project)
            configuration = controller_configuration(Path(temporary), project)
            result = prioritize(
                configuration,
                project,
                feature_id="F002",
                relative_id="F001",
                placement="before",
                priority="P1",
            )
            document = json.loads((repository / project.queue_location).read_text(encoding="utf-8"))
            self.assertEqual(["F002", "F001"], [item["id"] for item in document["features"]])
            self.assertEqual("P1", document["features"][0]["priority"])
            self.assertEqual([project.queue_location], result["changed_paths"])
            self.assertEqual(0, result["model_sessions_launched"])
            self.assertEqual(0, result["child_sessions_launched"])

    def test_ready_and_backlog_transitions_change_only_the_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            configuration = controller_configuration(Path(temporary), project)
            ready = transition_ready(
                configuration, project, feature_id="F001", active_transaction=None
            )
            self.assertEqual("ready", json.loads((repository / project.queue_location).read_text())["features"][0]["status"])
            self.assertEqual([project.queue_location], ready["changed_paths"])
            self.assertEqual(0, ready["model_sessions_launched"])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            backlog = transition_backlog(
                configuration, project, feature_id="F001", active_transaction=None
            )
            self.assertEqual("proposed", json.loads((repository / project.queue_location).read_text())["features"][0]["status"])
            self.assertEqual("feature_backlog", backlog["classification"])

    def test_ready_rejects_incomplete_dependencies_and_active_or_terminal_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            add_feature(repository, project, feature_id="F002", status="proposed", dependencies=["F001"])
            configuration = controller_configuration(Path(temporary), project)
            with self.assertRaisesRegex(QueueError, "Dependencies incomplete"):
                transition_ready(configuration, project, feature_id="F002", active_transaction=None)
            with self.assertRaisesRegex(ConveyorError, "active transaction"):
                transition_ready(configuration, project, feature_id="F001", active_transaction="tx-1")
            path = repository / project.queue_location
            document = json.loads(path.read_text(encoding="utf-8"))
            document["features"][0]["status"] = "done"
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(QueueError, "only ready"):
                transition_backlog(configuration, project, feature_id="F001", active_transaction=None)

    def test_queue_json_exposes_native_board_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            report = queue_report(
                controller_configuration(Path(temporary), project),
                project,
                runtime={"current_feature": "F001", "latest_terminal_result": {"feature_id": "F001", "result": "passed"}},
            )
            feature = report["features"][0]
            self.assertEqual("Running", feature["kanban_column"])
            self.assertEqual("P1", feature["priority"])
            self.assertEqual(1, feature["queue_position"])
            self.assertEqual("passed", feature["latest_terminal_result"]["result"])
            self.assertIn("execution_model", feature)
            self.assertIn("reasoning", feature)

    def test_queue_json_derives_dependency_blocked_column_without_persisting_a_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            add_feature(repository, project, feature_id="F002", status="proposed", dependencies=["F001"])
            report = queue_report(controller_configuration(Path(temporary), project), project)
            feature = next(item for item in report["features"] if item["feature_id"] == "F002")
            self.assertEqual("Blocked", feature["kanban_column"])
            self.assertEqual("proposed", feature["status"])

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
        ready_args = _parser().parse_args(["ready", "--project", "synthetic", "--feature", "F001"])
        self.assertEqual("F001", ready_args.feature)
        run_args = _parser().parse_args(
            ["run", "--project", "synthetic", "--feature", "F002"]
        )
        self.assertEqual("F002", run_args.feature)
