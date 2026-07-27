import unittest

from development_conveyor.compact_output import TERMINAL_REPORT_FIELDS
from development_conveyor.errors import ConfigurationError, QueueError
from development_conveyor.execution_profiles import (
    DEFAULT_PROFILES,
    DEFAULT_WORKFLOW_FALLBACKS,
    resolve_execution_profile,
    validate_authenticated_profile_binding,
    validate_feature_execution_policy,
)


def child_delegation(**overrides):
    value = {
        "role": "metadata-inspector",
        "profile": "mechanical",
        "cost_saving_justification": (
            "Inspecting three named metadata files avoids loading the parent source context"
        ),
        "task_boundary": "Summarize three named metadata files",
        "expected_input_context_bytes": 1000,
        "parent_context_bytes": 5000,
        "expected_output_contract": "JSON summary with paths and findings",
        "read_only": True,
        "owned_paths": [],
        "parent_owned_paths": ["src/runtime.py"],
    }
    value.update(overrides)
    return value


class RuntimeModelPolicyTests(unittest.TestCase):
    def test_development_conveyor_canonical_assignment(self):
        self.assertEqual(
            DEFAULT_PROFILES["bounded_precise"],
            {"model": "gpt-5.6-terra", "reasoning": "medium"},
        )

    def test_global_interactive_default_is_not_controller_fallback(self):
        self.assertEqual(
            DEFAULT_PROFILES["generic_or_architectural"],
            {"model": "gpt-5.6-sol", "reasoning": "medium"},
        )

    def test_generic_application_feature_default_is_terra_medium(self):
        self.assertEqual(
            DEFAULT_WORKFLOW_FALLBACKS["application_feature"],
            "bounded_precise",
        )
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
        )
        self.assertEqual(
            (resolved.model, resolved.reasoning),
            ("gpt-5.6-terra", "medium"),
        )

    def test_deterministic_route_has_zero_model_and_children(self):
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=True,
            override_profile="ambiguous_or_authoritative",
        )
        self.assertEqual(
            (resolved.model, resolved.parent_sessions, resolved.child_sessions),
            (None, 0, 0),
        )

    def test_child_budget_defaults_to_zero(self):
        resolved = resolve_execution_profile(
            workflow="controller_repair",
            deterministic=False,
        )
        self.assertEqual(resolved.child_sessions, 0)
        self.assertIsNone(resolved.child_delegation)

    def test_one_justified_cheaper_child_is_permitted(self):
        feature = {
            "id": "F001",
            "execution_policy": {
                "profile": "generic_or_architectural",
                "parent_sessions": 1,
                "child_sessions": 1,
                "child_delegation": child_delegation(),
            },
        }
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
            feature=feature,
        )
        self.assertEqual(resolved.child_sessions, 1)
        self.assertEqual(resolved.child_delegation["model"], "gpt-5.6-luna")

    def test_positive_child_without_justification_is_rejected(self):
        with self.assertRaisesRegex(QueueError, "child_delegation"):
            validate_feature_execution_policy(
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 1,
                }
            )

    def test_generic_child_justification_is_rejected(self):
        with self.assertRaisesRegex(QueueError, "generic justification"):
            validate_feature_execution_policy(
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 1,
                    "child_delegation": child_delegation(
                        cost_saving_justification="cheaper child to save tokens"
                    ),
                }
            )

    def test_child_context_must_be_materially_smaller(self):
        with self.assertRaisesRegex(QueueError, "materially smaller"):
            validate_feature_execution_policy(
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 1,
                    "child_delegation": child_delegation(
                        expected_input_context_bytes=3000
                    ),
                }
            )

    def test_child_file_ownership_cannot_overlap(self):
        with self.assertRaisesRegex(QueueError, "ownership overlaps"):
            validate_feature_execution_policy(
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 1,
                    "child_delegation": child_delegation(
                        read_only=False,
                        owned_paths=["src/runtime.py"],
                    ),
                }
            )

    def test_more_than_one_child_is_rejected(self):
        with self.assertRaisesRegex(QueueError, "at most one child"):
            validate_feature_execution_policy(
                {
                    "profile": "generic_or_architectural",
                    "parent_sessions": 1,
                    "child_sessions": 2,
                }
            )

    def test_equal_cost_child_model_is_rejected(self):
        feature = {
            "id": "F001",
            "execution_policy": {
                "profile": "mechanical",
                "parent_sessions": 1,
                "child_sessions": 1,
                "child_delegation": child_delegation(profile="mechanical"),
            },
        }
        with self.assertRaisesRegex(ConfigurationError, "strictly cheaper"):
            resolve_execution_profile(
                workflow="application_feature",
                deterministic=False,
                feature=feature,
            )

    def test_xhigh_is_not_available_on_first_attempt(self):
        feature = {
            "id": "M1",
            "execution_policy": {
                "profile": "ambiguous_or_authoritative",
                "parent_sessions": 1,
                "child_sessions": 0,
                "escalation": {
                    "trigger": "focused_high_attempt_failed",
                    "profile": "unresolved_after_high",
                },
            },
        }
        evidence = {
            "recorded": True,
            "context_complete": True,
            "trigger": "focused_high_attempt_failed",
            "evidence_id": "evidence-1",
            "previous_profile": "ambiguous_or_authoritative",
            "previous_model": "gpt-5.6-sol",
            "previous_reasoning": "high",
            "failed_command_or_unresolved_evidence": "focused high attempt failed",
            "escalation_trigger": "focused_high_attempt_failed",
            "new_profile": "unresolved_after_high",
            "new_model": "gpt-5.6-sol",
            "new_reasoning": "xhigh",
            "expected_resolution": "resolve contradictory recovery authority",
            "attempt_number": 1,
        }
        first = resolve_execution_profile(
            workflow="controller_repair",
            deterministic=False,
            feature=feature,
            escalation_evidence=evidence,
        )
        self.assertEqual(first.profile, "ambiguous_or_authoritative")
        second = resolve_execution_profile(
            workflow="controller_repair",
            deterministic=False,
            feature=feature,
            escalation_evidence={**evidence, "attempt_number": 2},
        )
        self.assertEqual(second.profile, "unresolved_after_high")

    def test_authenticated_profile_cannot_change_without_escalation(self):
        current = resolve_execution_profile(
            workflow="controller_repair",
            deterministic=False,
        )
        with self.assertRaisesRegex(ConfigurationError, "without an escalation"):
            validate_authenticated_profile_binding(
                authenticated={
                    "profile": "bounded_precise",
                    "model": "gpt-5.6-terra",
                    "reasoning": "medium",
                    "parent_sessions": 1,
                    "child_sessions": 0,
                },
                current=current,
                escalation_record=None,
            )

    def test_compact_contract_preserves_required_terminal_evidence(self):
        for field in (
            "errors",
            "safety_findings",
            "changed_paths",
            "validation_result",
            "accepted_commit",
            "recovery_instruction",
        ):
            self.assertIn(field, TERMINAL_REPORT_FIELDS)


if __name__ == "__main__":
    unittest.main()
