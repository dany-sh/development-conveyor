import tempfile
import unittest
from pathlib import Path

from development_conveyor.cost_policy import (
    ChildSessionBudget, build_run_plan, contain_command_output, context_pack, reusable_evidence,
    select_model, ValidationEvidenceCache, validation_identity, verification_plan,
)
from development_conveyor.sessions import SessionLauncher, SessionRequest
from tests.helpers import synthetic_repository


class CostPolicyTests(unittest.TestCase):
    def test_deterministic_work_never_selects_a_model(self):
        for task in ("status", "consistency", "queue_parse", "dry_run", "integration"):
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

    def test_zero_child_budget_blocks_at_session_launcher_before_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            request = SessionRequest("queue_reconciliation", project, "run", "one_feature", session_kind="child", child_session_budget=0)
            launcher = SessionLauncher(Path(temporary), {"codex": {"executable": "definitely-not-called"}})
            with self.assertRaisesRegex(Exception, "child session budget exhausted"):
                launcher.launch(request)

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
