from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from development_conveyor.execution_plan import (
    ExecutionPlan,
    authoritative_status_fields,
    execution_plan_projection_agreement,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ProjectionError
from development_conveyor.projection import projection_fingerprint


F004_COMMIT = "8ae5c94df59119d89d3c2ac6fd7508a47a426ff6"
F003_COMMIT = "543615bd0cd70e8cd56d70e4a508c076954e6cb9"
F005_COMMIT = "5" * 40
F073_START = "8" * 40
F073_TERMINAL = "1" * 40
F073_TRANSACTION = "c17a56d1-d261-418f-80ba-c19ecfbf2145"


def projection(**overrides):
    value = {
        "schema_version": 2,
        "project_id": "interview-companion",
        "ledger_sequence": 213,
        "ledger_fingerprint": "a" * 64,
        "current_state": "integration_ready",
        "active_transaction": None,
        "current_feature": "F004",
        "selected_next_feature": None,
        "selected_feature_starting_commit": "b" * 40,
        "accepted_feature_commit": F004_COMMIT,
        "feature_branch": "codex/F004-navigation-and-workspace-restoration",
        "milestone_branch": "codex/m0-foundation",
        "allowed_next_action": "milestone_integration",
        "session_resume_eligible": False,
        "required_lease": "integration_writer",
        "transactions": [],
        "human_gate": {
            "gate_id": "historical-f003-gate",
            "transaction_id": "8e45c566-49eb-452e-b5cc-c10f40e762fa",
            "feature_id": "F003",
            "accepted_feature_commit": F003_COMMIT,
            "feature_starting_commit": "3" * 40,
            "feature_branch": "codex/F003-single-window-application-shell",
            "resolved": False,
            "safe_continuation_command": "resume",
        },
        "historical_integration_outcomes": [
            {
                "classification": "INTEGRATED",
                "feature_id": "F003",
                "accepted_commit": F003_COMMIT,
            },
            {
                "classification": "INTEGRATED",
                "feature_id": "F005",
                "accepted_commit": F005_COMMIT,
            },
        ],
    }
    value.update(overrides)
    value["projection_fingerprint"] = projection_fingerprint(value)
    return value


def post_integration_projection(**overrides):
    value = projection(
        current_state="queue_reconciliation",
        active_transaction=None,
        current_feature=None,
        selected_next_feature=None,
        selected_feature_starting_commit=F073_START,
        accepted_feature_commit=None,
        feature_branch=None,
        milestone_branch="codex/m0-foundation",
        allowed_next_action="queue_reconciliation",
        session_resume_eligible=False,
        required_lease=None,
        human_gate=None,
        historical_integration_outcomes=[
            {
                "classification": "INTEGRATED",
                "feature_id": "F072",
                "accepted_commit": "7" * 40,
                "integrated_commit": "6" * 40,
                "sequence": 757,
                "terminal_head": "9" * 40,
                "terminal_repository_clean": True,
                "transaction_id": "f072-integration",
            },
            {
                "classification": "INTEGRATED",
                "feature_id": "F073",
                "accepted_commit": "a" * 40,
                "integrated_commit": "b" * 40,
                "sequence": 836,
                "terminal_head": F073_TERMINAL,
                "terminal_repository_clean": True,
                "transaction_id": F073_TRANSACTION,
            },
        ],
        transactions=[
            {
                "transaction_id": "f072-integration",
                "workflow_type": "milestone_integration",
                "feature_id": "F072",
                "state": "completed",
                "terminal_classification": "INTEGRATED",
                "terminal_reference": "9" * 40,
                "terminal_snapshot": {
                    "branch": "codex/m0-foundation",
                    "head": "9" * 40,
                    "clean": True,
                    "git_operations": {},
                },
            },
            {
                "transaction_id": F073_TRANSACTION,
                "workflow_type": "milestone_integration",
                "feature_id": "F073",
                "state": "completed",
                "terminal_classification": "INTEGRATED",
                "terminal_reference": F073_TERMINAL,
                "terminal_snapshot": {
                    "branch": "codex/m0-foundation",
                    "head": F073_TERMINAL,
                    "clean": True,
                    "git_operations": {
                        "merge": False,
                        "cherry_pick": False,
                        "rebase_apply": False,
                        "rebase_merge": False,
                    },
                },
            },
        ],
    )
    value.update(overrides)
    value["projection_fingerprint"] = projection_fingerprint(value)
    return value


class ExecutionPlanIdentityTests(unittest.TestCase):
    def test_integration_ready_ignores_historical_gate_identity(self):
        authoritative = projection()
        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual("milestone_integration", plan.workflow_type)
        self.assertEqual("integration_ready", plan.current_state)
        self.assertEqual("F004", plan.feature_id)
        self.assertEqual(F004_COMMIT, plan.accepted_commit)
        self.assertEqual("codex/m0-foundation", plan.starting_branch)
        self.assertNotEqual(F003_COMMIT, plan.accepted_commit)
        plan.validate_against(authoritative)

        stale_gate_plan = replace(
            plan, feature_id="F003", accepted_commit=F003_COMMIT
        )
        with self.assertRaisesRegex(
            ProjectionError, "feature_id, accepted_commit"
        ):
            stale_gate_plan.validate_against(authoritative)

    def test_feature_ready_ignores_stale_unrelated_gate(self):
        authoritative = projection(
            current_state="feature_ready",
            current_feature="F006",
            selected_next_feature="F006",
            selected_feature_starting_commit="6" * 40,
            accepted_feature_commit=None,
            feature_branch="codex/F006-ready",
            allowed_next_action="feature_cycle",
            required_lease="feature_writer",
        )

        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual("feature_execution", plan.workflow_type)
        self.assertEqual("F006", plan.feature_id)
        self.assertIsNone(plan.accepted_commit)
        self.assertEqual("codex/m0-foundation", plan.starting_branch)
        self.assertEqual("6" * 40, plan.starting_commit)

    def test_authenticated_preparation_uses_feature_starting_branch(self):
        authoritative = projection(
            current_state="feature_ready",
            current_feature="F006",
            selected_next_feature="F006",
            selected_feature_starting_commit="6" * 40,
            accepted_feature_commit=None,
            feature_branch="codex/F006-ready",
            allowed_next_action="feature_cycle",
            required_lease="feature_writer",
            prepared_feature_execution={
                "preparation_transaction_id": "prepared-F006",
                "feature_id": "F006",
                "feature_branch": "codex/F006-ready",
                "starting_commit": "6" * 40,
                "milestone_branch": "codex/m0-foundation",
            },
        )

        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual("feature_ready", plan.current_state)
        self.assertEqual("codex/F006-ready", plan.starting_branch)
        self.assertEqual("codex/m0-foundation", plan.milestone_branch)
        plan.validate_against(authoritative)

    def test_active_human_decision_uses_authoritative_gate(self):
        authoritative = projection(
            current_state="human_decision_required",
            current_feature="F004",
            accepted_feature_commit=F004_COMMIT,
            allowed_next_action="human_decision_resolution",
            required_lease="recovery_writer",
        )

        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual("human_decision_resolution", plan.workflow_type)
        self.assertEqual("F003", plan.feature_id)
        self.assertEqual(F003_COMMIT, plan.accepted_commit)
        self.assertEqual("3" * 40, plan.starting_commit)

    def test_duplicate_authoritative_feature_remains_rejected(self):
        authoritative = projection(
            historical_integration_outcomes=[
                {
                    "classification": "INTEGRATED",
                    "feature_id": "F004",
                    "accepted_commit": "different-accepted-commit",
                }
            ]
        )

        with self.assertRaisesRegex(
            ProjectionError, "integrated feature cannot receive"
        ):
            ExecutionPlan.from_projection(authoritative)

    def test_historical_accepted_commit_remains_rejected(self):
        authoritative = projection(
            accepted_feature_commit=F005_COMMIT,
            historical_integration_outcomes=[
                {
                    "classification": "INTEGRATED",
                    "feature_id": "F005",
                    "accepted_commit": F005_COMMIT,
                }
            ],
        )

        with self.assertRaisesRegex(
            ProjectionError, "historical accepted commit cannot create"
        ):
            ExecutionPlan.from_projection(authoritative)

    def test_status_and_consistency_use_plan_identity_without_mutation(self):
        authoritative = projection()
        before = copy.deepcopy(authoritative)
        plan = ExecutionPlan.from_projection(authoritative)

        status = authoritative_status_fields(
            authoritative,
            plan,
            legacy_plan={},
            persisted_state="integration_ready",
            superseded_cycles=[],
            persisted_projection_fingerprint=authoritative[
                "projection_fingerprint"
            ],
        )
        agreed, evidence = execution_plan_projection_agreement(
            authoritative, status, plan
        )

        self.assertTrue(agreed, evidence)
        self.assertEqual("F004", status["selected_feature"])
        self.assertEqual(F004_COMMIT, status["accepted_feature_commit"])
        self.assertEqual("F003", status["human_gate"]["feature_id"])
        self.assertEqual(before, authoritative)

    def test_advanced_projection_cannot_reenter_historical_planning_recovery(self):
        authoritative = projection()
        before = copy.deepcopy(authoritative)
        engine = object.__new__(CycleEngine)

        recovery = engine._planning_finalization_recovery_plan(
            None, authoritative
        )

        self.assertIsNone(recovery)
        self.assertEqual(before, authoritative)

    def test_completed_integration_uses_terminal_milestone_head_for_fresh_planning(self):
        authoritative = post_integration_projection()

        plan = ExecutionPlan.from_projection(
            authoritative, starting_commit="f" * 40
        )

        self.assertEqual("queue_reconciliation", plan.workflow_type)
        self.assertEqual("codex/m0-foundation", plan.starting_branch)
        self.assertEqual(F073_TERMINAL, plan.starting_commit)
        self.assertIsNone(plan.feature_id)
        self.assertIsNone(plan.accepted_commit)

    def test_cleared_integrated_feature_ignores_stale_selected_start(self):
        authoritative = post_integration_projection()

        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual(F073_START, authoritative["selected_feature_starting_commit"])
        self.assertEqual(F073_TERMINAL, plan.starting_commit)

    def test_post_integration_status_and_plan_agree_without_mutation(self):
        authoritative = post_integration_projection()
        before = copy.deepcopy(authoritative)
        plan = ExecutionPlan.from_projection(authoritative)

        status = authoritative_status_fields(
            authoritative,
            plan,
            legacy_plan={},
            persisted_state="integration_pending",
            superseded_cycles=[],
            persisted_projection_fingerprint="stale",
        )
        agreed, evidence = execution_plan_projection_agreement(
            authoritative, status, plan
        )

        self.assertTrue(agreed, evidence)
        self.assertEqual(F073_TERMINAL, status["starting_commit"])
        self.assertEqual(F073_TERMINAL, status["execution_plan"]["starting_commit"])
        self.assertEqual(before, authoritative)

    def test_mismatched_caller_start_cannot_override_terminal_milestone_head(self):
        authoritative = post_integration_projection()

        plan = ExecutionPlan.from_projection(
            authoritative, starting_commit="f" * 40
        )

        self.assertEqual(F073_TERMINAL, plan.starting_commit)
        self.assertNotEqual("f" * 40, plan.starting_commit)

    def test_incomplete_integration_completion_fails_closed(self):
        authoritative = post_integration_projection()
        authoritative["transactions"][-1]["terminal_snapshot"]["clean"] = False
        authoritative["projection_fingerprint"] = projection_fingerprint(authoritative)

        plan = ExecutionPlan.from_projection(
            authoritative, starting_commit="f" * 40
        )

        self.assertEqual(F073_START, plan.starting_commit)
        self.assertNotEqual(F073_TERMINAL, plan.starting_commit)

    def test_unfinished_terminal_git_operation_fails_closed(self):
        authoritative = post_integration_projection()
        authoritative["transactions"][-1]["terminal_snapshot"]["git_operations"][
            "merge"
        ] = True
        authoritative["projection_fingerprint"] = projection_fingerprint(authoritative)

        plan = ExecutionPlan.from_projection(
            authoritative, starting_commit="f" * 40
        )

        self.assertEqual(F073_START, plan.starting_commit)

    def test_active_selected_feature_retains_authenticated_start(self):
        authoritative = post_integration_projection(
            current_state="feature_ready",
            current_feature="F074",
            selected_next_feature="F074",
            selected_feature_starting_commit="4" * 40,
            feature_branch="codex/F074-active",
            allowed_next_action="feature_cycle",
            required_lease="feature_writer",
        )

        plan = ExecutionPlan.from_projection(authoritative)

        self.assertEqual("F074", plan.feature_id)
        self.assertEqual("4" * 40, plan.starting_commit)
        self.assertEqual("codex/F074-active", plan.feature_branch)

    def test_historical_f072_and_f073_outcomes_remain_unchanged(self):
        authoritative = post_integration_projection()
        historical_before = copy.deepcopy(
            authoritative["historical_integration_outcomes"]
        )

        ExecutionPlan.from_projection(authoritative)

        self.assertEqual(
            historical_before, authoritative["historical_integration_outcomes"]
        )


if __name__ == "__main__":
    unittest.main()
