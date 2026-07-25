from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from development_conveyor.autopilot import (
    AUTOPILOT_EVENTS,
    Autopilot,
    AutopilotOwnership,
    AutopilotPaths,
    autopilot_status,
    request_stop,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import AutopilotStopRequested, LockError
from development_conveyor.logging import atomic_write_json
from tests.helpers import controller_configuration, synthetic_repository


def plan(
    action: str,
    *,
    feature: str | None = "F001",
    state: str = "feature_ready",
    human_gate=None,
) -> dict:
    return {
        "project_id": "synthetic",
        "current_state": state,
        "selected_feature": feature,
        "proposed_next_action": action,
        "human_gate": human_gate,
        "executable_plan": {
            "feature_id": feature,
            "workflow_type": (
                "feature_execution" if action == "feature_cycle" else action
            ),
        },
        "kernel_projection": {
            "current_state": state,
            "current_feature": feature,
            "selected_next_feature": feature,
            "allowed_next_action": action,
            "active_transaction": None,
        },
        "cost_aware_run_plan": {
            "selected_model": "gpt-5.6-sol",
            "selected_reasoning_effort": "medium",
            "parent_session_budget": 1,
            "child_session_budget": 0,
            "deterministic_commands_planned": [["python3", "-m", "unittest"]],
            "context_pack": {"approximate_bytes_estimate": 4096},
        },
    }


class FakeEngine:
    def __init__(self, plans, results, after_run=None):
        self.plans = list(plans)
        self.results = list(results)
        self.index = 0
        self.calls = []
        self.after_run = after_run

    def project_plan(self, _project):
        return self.plans[min(self.index, len(self.plans) - 1)]

    def run_project(self, _project, mode, *, dry_run=False):
        self.calls.append((mode, dry_run))
        result = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        if self.after_run is not None:
            self.after_run(self.index)
        if isinstance(result, Exception):
            raise result
        return result


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository, self.project = synthetic_repository(self.root)
        self.configuration = controller_configuration(self.root, self.project)
        self.consistency = lambda: {"classification": "CONSISTENT"}

    def tearDown(self):
        self.temporary.cleanup()

    def make(self, engine, sink=None):
        return Autopilot(
            configuration=self.configuration,
            project=self.project,
            engine=engine,
            consistency_factory=lambda: self.consistency,
            event_sink=sink or (lambda _line: None),
        )

    def test_dry_run_is_zero_write_and_selects_first_feature(self):
        engine = FakeEngine([plan("feature_cycle")], [])
        controller = self.make(engine)
        before = sorted(str(item.relative_to(self.root)) for item in self.root.rglob("*"))
        result = controller.dry_run()
        after = sorted(str(item.relative_to(self.root)) for item in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(result["first_feature"], "F001")
        self.assertEqual(result["exact_next_route"], "feature_cycle")
        self.assertEqual(result["dry_run_guarantees"]["writes"], 0)
        self.assertEqual(result["dry_run_guarantees"]["models"], 0)
        self.assertEqual(result["dry_run_guarantees"]["children"], 0)
        self.assertEqual(engine.calls, [])

    def test_normal_feature_acceptance_integration_and_next_feature_loop(self):
        engine = FakeEngine(
            [
                plan("feature_cycle"),
                plan("milestone_integration", state="integration_pending"),
                plan("paused", feature=None, state="paused"),
            ],
            [
                {"outcome": "feature_accepted", "model_session_launched": True},
                {
                    "outcome": "feature_integrated",
                    "model_session_launched": False,
                    "child_sessions_launched": 0,
                },
            ],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_COMPLETED")
        self.assertEqual(engine.calls, [("one_feature", False), ("milestone", False)])
        events = [item["event"] for item in result["events"]]
        self.assertIn("FEATURE_ACCEPTED", events)
        self.assertIn("FEATURE_INTEGRATED", events)
        self.assertTrue(result["ownership_released"])

    def test_stop_request_is_durable_and_consumed_at_safe_checkpoint(self):
        engine = FakeEngine([plan("feature_cycle")], [])
        request = request_stop(self.configuration, self.project, "test stop")
        self.assertTrue(Path(request["stop_request_path"]).exists())
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_STOPPED")
        self.assertEqual(engine.calls, [])
        paths = AutopilotPaths.for_project(self.configuration, self.project.project_id)
        self.assertFalse(paths.stop_request.exists())
        self.assertTrue(paths.stop_request.with_name("last-stop-request.json").exists())

    def test_stop_between_features_prevents_next_model_route(self):
        paths = AutopilotPaths.for_project(self.configuration, self.project.project_id)

        def stop_after_first(index):
            if index == 1:
                atomic_write_json(paths.stop_request, {
                    "schema_version": 1,
                    "project_id": self.project.project_id,
                    "requested_at": "2026-07-24T12:00:00+00:00",
                    "reason": "between features",
                })

        engine = FakeEngine(
            [
                plan("feature_cycle"),
                plan("feature_cycle", feature="F002"),
            ],
            [{"outcome": "feature_accepted"}],
            after_run=stop_after_first,
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_STOPPED")
        self.assertEqual(engine.calls, [("one_feature", False)])

    def test_stop_before_integration_prevents_integration_route(self):
        paths = AutopilotPaths.for_project(self.configuration, self.project.project_id)

        def stop_after_feature(index):
            if index == 1:
                atomic_write_json(paths.stop_request, {
                    "schema_version": 1,
                    "project_id": self.project.project_id,
                    "requested_at": "2026-07-24T12:00:00+00:00",
                    "reason": "before integration",
                })

        engine = FakeEngine(
            [
                plan("feature_cycle"),
                plan("milestone_integration", state="integration_pending"),
            ],
            [{"outcome": "feature_accepted"}],
            after_run=stop_after_feature,
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_STOPPED")
        self.assertEqual(engine.calls, [("one_feature", False)])

    def test_registered_deterministic_recovery_is_reused(self):
        engine = FakeEngine(
            [
                plan("cache_binding_recovery", feature=None),
                plan("paused", feature=None, state="paused"),
            ],
            [{"outcome": "cache_binding_recovered"}],
        )
        result = self.make(engine).apply()
        self.assertEqual(engine.calls, [("resume", False)])
        events = [item["event"] for item in result["events"]]
        self.assertIn("RECOVERY_STARTED", events)
        self.assertIn("RECOVERY_APPLIED", events)

    def test_canonical_feature_execution_alias_uses_feature_route(self):
        engine = FakeEngine(
            [
                plan("feature_execution"),
                plan("paused", feature=None, state="paused"),
            ],
            [{"outcome": "feature_accepted"}],
        )
        self.make(engine).apply()
        self.assertEqual(engine.calls, [("one_feature", False)])

    def test_genuine_human_gate_stops_without_route(self):
        gate = {"gate_id": "gate-1", "reason": "product contradiction"}
        engine = FakeEngine(
            [plan("human_decision_resolution", human_gate=gate)],
            [],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_STOPPED")
        self.assertEqual(engine.calls, [])

    def test_repeated_identical_failure_is_bounded_and_blocked(self):
        engine = FakeEngine(
            [plan("feature_cycle"), plan("feature_cycle"), plan("feature_cycle")],
            [
                {"outcome": "validation_failed", "reason": "same evidence"},
                {"outcome": "validation_failed", "reason": "same evidence"},
            ],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_STOPPED")
        self.assertEqual(len(engine.calls), 2)
        self.assertIn("FEATURE_BLOCKED", [item["event"] for item in result["events"]])

    def test_blocked_feature_continues_when_projection_selects_independent_work(self):
        engine = FakeEngine(
            [
                plan("feature_cycle", feature="F001"),
                plan("feature_cycle", feature="F001"),
                plan("feature_cycle", feature="F002"),
                plan("paused", feature=None, state="paused"),
            ],
            [
                {"outcome": "validation_failed", "reason": "same evidence"},
                {"outcome": "validation_failed", "reason": "same evidence"},
                {"outcome": "feature_accepted"},
            ],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_COMPLETED")
        self.assertEqual(len(engine.calls), 3)
        blocked = next(item for item in result["features"] if item["feature"] == "F001")
        self.assertTrue(blocked["quarantined"])

    def test_ownership_releases_on_route_failure(self):
        engine = FakeEngine(
            [plan("feature_cycle")],
            [RuntimeError("synthetic failure")],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["classification"], "AUTOPILOT_FAILED")
        self.assertTrue(result["ownership_released"])

    def test_duplicate_live_ownership_is_rejected(self):
        paths = AutopilotPaths.for_project(self.configuration, self.project.project_id)
        first = AutopilotOwnership(paths, self.project)
        first.acquire("first-run")
        try:
            second = AutopilotOwnership(paths, self.project)
            with self.assertRaises(LockError):
                second.acquire("second-run")
        finally:
            first.release()

    def test_dead_exact_ownership_is_recovered(self):
        paths = AutopilotPaths.for_project(self.configuration, self.project.project_id)
        owner = AutopilotOwnership(paths, self.project)
        record = owner._record("dead-run", "AUTOPILOT_STARTED")
        record["process_id"] = 999_999_999
        record["process_start_identity"] = "authenticated-prior-start"
        atomic_write_json(paths.ownership, record)
        recovered = owner.acquire("replacement-run")
        try:
            self.assertIsNotNone(recovered["recovered_ownership"])
            self.assertTrue(Path(recovered["recovered_ownership"]).exists())
        finally:
            owner.release()

    def test_status_reports_live_owner_and_durable_report(self):
        engine = FakeEngine(
            [plan("paused", feature=None, state="paused")],
            [],
        )
        result = self.make(engine).apply()
        status = autopilot_status(self.configuration, self.project)
        self.assertFalse(status["active"])
        self.assertEqual(
            status["report"]["terminal_classification"],
            "AUTOPILOT_COMPLETED",
        )
        self.assertEqual(status["status_path"], result["status_path"])

    def test_structured_events_are_concise_and_complete(self):
        lines = []
        engine = FakeEngine(
            [plan("paused", feature=None, state="paused")],
            [],
        )
        result = self.make(engine, sink=lines.append).apply()
        self.assertTrue(lines)
        for line in lines:
            event = json.loads(line)
            self.assertIn(event["event"], AUTOPILOT_EVENTS)
            self.assertEqual(
                set(event),
                {
                    "event",
                    "project",
                    "feature",
                    "state",
                    "transaction",
                    "timestamp",
                    "diagnostic",
                },
            )
        self.assertNotIn("projection_history", json.dumps(result))

    def test_zero_children_remains_recorded(self):
        engine = FakeEngine(
            [
                plan("feature_cycle"),
                plan("paused", feature=None, state="paused"),
            ],
            [{"outcome": "feature_accepted", "child_sessions_launched": 0}],
        )
        result = self.make(engine).apply()
        self.assertEqual(result["features"][0]["child_sessions"], 0)

    def test_routes_never_request_prohibited_publish_operations(self):
        engine = FakeEngine(
            [
                plan("feature_cycle"),
                plan("milestone_integration", state="integration_pending"),
                plan("paused", feature=None, state="paused"),
            ],
            [
                {"outcome": "feature_accepted"},
                {"outcome": "feature_integrated"},
            ],
        )
        self.make(engine).apply()
        serialized = json.dumps(engine.calls)
        for prohibited in ("push", "tag", "publish", "deploy", "release", "merge_default_branch"):
            self.assertNotIn(prohibited, serialized)

    def test_cycle_engine_honors_inner_stop_before_model_or_mutation(self):
        engine = CycleEngine(
            self.configuration,
            lifecycle_observer=lambda boundary, _evidence: boundary
            in {"before_model", "before_application_mutation"},
        )
        with self.assertRaises(AutopilotStopRequested):
            engine._observe_lifecycle("before_model", {"project_id": "synthetic"})
        with self.assertRaises(AutopilotStopRequested):
            engine._observe_lifecycle(
                "before_application_mutation", {"project_id": "synthetic"}
            )


if __name__ == "__main__":
    unittest.main()
