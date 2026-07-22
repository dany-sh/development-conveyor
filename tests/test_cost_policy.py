import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.compatibility import CompatibilityResult
from development_conveyor.cost_policy import (
    ChildSessionBudget, build_run_plan, contain_command_output, context_pack, reusable_evidence,
    select_model, ValidationEvidenceCache, validation_identity, verification_plan,
)
from development_conveyor.sessions import SessionLauncher, SessionRequest
from tests.helpers import REPOSITORY_ROOT, synthetic_repository, write_json


class CostPolicyTests(unittest.TestCase):
    def test_deterministic_work_never_selects_a_model(self):
        for task in ("status", "consistency", "queue_parse", "dry_run", "integration", "planning_finalization"):
            selected = select_model(task=task)
            self.assertIsNone(selected.model)
            self.assertIsNone(selected.reasoning)

    def test_model_and_reasoning_are_independent(self):
        self.assertEqual((select_model(task="metadata").model, select_model(task="metadata").reasoning), ("gpt-5.6-luna", "medium"))
        self.assertEqual((select_model(task="controller_repair").model, select_model(task="controller_repair").reasoning), ("gpt-5.6-terra", "medium"))
        self.assertEqual(select_model(task="controller_repair", ambiguity=True).reasoning, "high")
        self.assertEqual(select_model(task="recovery", risk="high").model, "gpt-5.6-sol")
        self.assertEqual(select_model(task="recovery", risk="high", ambiguity=True, focused_attempt_failed=True).reasoning, "xhigh")
        self.assertEqual(select_model(task="controller_repair", task_length=100).reasoning, "medium")

    def test_child_budget_blocks_before_callback(self):
        budget, called = ChildSessionBudget(0, "controller_maintenance"), []
        with self.assertRaises(PermissionError):
            budget.launch("reviewer", None, lambda: called.append(True))
        self.assertEqual(called, [])
        self.assertEqual(budget.launched, 0)
        allowed = ChildSessionBudget(1, "ordinary_feature")
        self.assertEqual(allowed.launch("one_question", "unresolved semantic question", lambda: "ok"), "ok")

    def test_queue_reconciliation_routes_by_ready_selection(self):
        semantic = build_run_plan({"proposed_next_action": "queue_reconciliation"}, Path.cwd())
        self.assertEqual(semantic["queue_reconciliation_route"], "semantic_queue_reconciliation")
        self.assertEqual((semantic["selected_model"], semantic["selected_reasoning_effort"]), ("gpt-5.6-terra", "medium"))
        self.assertEqual((semantic["usage_accounting"]["parent_sessions_planned"], semantic["usage_accounting"]["child_sessions_planned"]), (1, 0))
        self.assertFalse(semantic["usage_accounting"]["deterministic_only"])
        self.assertEqual((semantic["execution"]["models_planned"], semantic["dry_run"]["models"]), (1, 0))
        deterministic = build_run_plan({"proposed_next_action": "queue_reconciliation", "selected_feature": "F001"}, Path.cwd())
        self.assertEqual(deterministic["queue_reconciliation_route"], "deterministic_queue_selection")
        self.assertIsNone(deterministic["selected_model"])
        self.assertEqual(deterministic["execution"]["models_planned"], 0)

    def test_f003_feature_plan_is_application_focused(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0].update({"id": "F003", "title": "Single-Window Application Shell", "spec": "docs/features/F003.md"})
            (repository / "docs/features/F003.md").write_text("# F003\n", encoding="utf-8")
            write_json(queue_path, queue)
            plan = build_run_plan({"proposed_next_action": "feature_cycle", "selected_feature": "F003", "application_mutation_expected": True}, Path.cwd(), project=project)
            self.assertEqual((plan["task_classification"], plan["risk_classification"]), ("application_feature", "medium"))
            self.assertEqual((plan["selected_model"], plan["selected_reasoning_effort"]), ("gpt-5.6-terra", "medium"))
            self.assertGreater(plan["context_pack"]["file_count"], 0)
            self.assertIn("Tests/LiveInterviewCompanionTests/SessionLifecycleTests.swift", plan["selected_tests"])
            self.assertTrue(plan["final_feature_acceptance_gates"])

    def test_zero_child_budget_blocks_at_session_launcher_before_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            request = SessionRequest("queue_reconciliation", project, "run", "one_feature", session_kind="child", child_session_budget=0)
            launcher = SessionLauncher(Path(temporary), {"codex": {"executable": "definitely-not-called"}})
            with self.assertRaisesRegex(Exception, "child session budget exhausted"):
                launcher.launch(request)

    def test_authoritative_cost_plan_is_bound_to_launch_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            launcher = SessionLauncher(
                REPOSITORY_ROOT,
                {"codex": {"executable": "codex", "session_timeout_seconds": 60}},
            )
            compatible = CompatibilityResult(
                classification="compatible",
                executable="codex",
                detected_version="1.0.0",
                required_minimum_version=None,
                effective_model="gpt-5.6-terra",
                effective_reasoning="medium",
                policy_source="cost_aware_execution_plan",
                policy_role="feature-inventory-lead",
                compatible=True,
                diagnostic="ok",
                remediation="none",
                validation_command="scripts/conveyor doctor",
            )
            request = SessionRequest(
                "queue_reconciliation",
                project,
                "run",
                "one_feature",
                planned_model="gpt-5.6-terra",
                planned_reasoning="medium",
                model_plan_source="cost_aware_execution_plan",
            )
            with patch.object(launcher, "compatibility", return_value=compatible):
                plan = launcher.plan(request)
            self.assertIn("gpt-5.6-terra", plan.argv)
            self.assertIn('model_reasoning_effort="medium"', plan.argv)
            self.assertEqual(
                (plan.planned_model, plan.planned_reasoning),
                (plan.launched_model, plan.launched_reasoning),
            )
            self.assertEqual(plan.cwd, repository)

    def test_planned_launch_mismatch_fails_before_process_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            launcher = SessionLauncher(
                REPOSITORY_ROOT,
                {"codex": {"executable": "codex", "session_timeout_seconds": 60}},
            )
            compatible = CompatibilityResult(
                classification="compatible",
                executable="/usr/bin/true",
                detected_version="1.0.0",
                required_minimum_version=None,
                effective_model="gpt-5.6-terra",
                effective_reasoning="medium",
                policy_source="cost_aware_execution_plan",
                policy_role="feature-inventory-lead",
                compatible=True,
                diagnostic="ok",
                remediation="none",
                validation_command="scripts/conveyor doctor",
            )
            request = SessionRequest(
                "queue_reconciliation",
                project,
                "run",
                "one_feature",
                planned_model="gpt-5.6-terra",
                planned_reasoning="medium",
            )
            with (
                patch.object(launcher, "compatibility", return_value=compatible),
                patch.object(
                    launcher,
                    "_launch_policy_args",
                    return_value=("--model", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"'),
                ),
                patch("development_conveyor.sessions.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(Exception, "planned/launched model binding mismatch"):
                    launcher.launch(request)
            popen.assert_not_called()

    def test_context_pack_is_focused_and_high_risk_contracts_are_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir(); (root / "tests").mkdir(); (root / "docs").mkdir(); (root / ".factory").mkdir()
            (root / "src/a.py").write_text("x")
            (root / "tests/test_a.py").write_text("x")
            (root / "docs/AUTONOMY_CONTRACT.md").write_text("x")
            (root / ".factory/project.yaml").write_text("{}")
            pack = context_pack(root, ["src/a.py"], ["tests/test_a.py"], high_risk=True)
            self.assertEqual(pack["file_count"], 4)
            self.assertIn("full history", pack["excluded_categories"])

    def test_verification_selection_and_evidence_reuse(self):
        config = verification_plan(["config/conveyor.yaml"])
        self.assertEqual(config["tier"], "focused")
        self.assertEqual(config["builds"], [])
        projection = verification_plan(["src/development_conveyor/projection.py"])
        self.assertIn("tests/test_projection_authority.py", projection["tests"])
        docs = verification_plan(["docs/README.md"])
        self.assertEqual(docs["builds"], [])
        identity = validation_identity(["python3", "-m", "unittest"], source_tree_hash="a", configuration_hash="b", fixture_version="1")
        evidence = reusable_evidence({"status": "passed", "identity": identity, "complete": True, "trusted": True, "dirty": False, "run_id": "r1"}, identity)
        self.assertTrue(evidence["reused"])
        self.assertFalse(reusable_evidence({"status": "passed", "identity": identity, "complete": False, "trusted": True, "dirty": False}, identity)["reused"])
        with tempfile.TemporaryDirectory() as temporary:
            cache = ValidationEvidenceCache(Path(temporary) / "evidence.json")
            cache.store(identity, {"status": "passed", "complete": True, "trusted": True, "dirty": False, "run_id": "r1"})
            self.assertTrue(reusable_evidence(cache.lookup(identity), identity)["reused"])

    def test_large_output_is_contained_in_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.log"
            summary = contain_command_output(["python3", "-c", "print('x' * 5001)"], cwd=Path(temporary), report_path=path)
            self.assertTrue(path.is_file())
            self.assertTrue(summary["truncated_for_context"])
