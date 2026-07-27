import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.capability_policy import CapabilityPlan
from development_conveyor.compatibility import ModelSelection
from development_conveyor.runtime_audit import RuntimeAuditor
from tests.helpers import (
    REPOSITORY_ROOT,
    controller_configuration,
    git,
    synthetic_repository,
)


def capability(supported=True):
    return CapabilityPlan(
        requested_allowlist=(
            "core:apply_patch",
            "core:shell",
            "development-conveyor",
            "feature-factory",
        ),
        effective_allowlist=(
            "core:apply_patch",
            "core:shell",
            "development-conveyor",
            "feature-factory",
        )
        if supported
        else ("core:apply_patch", "core:shell"),
        config_args=("-c", "mcp_servers={}") if supported else (),
        enforcement_mechanism="codex_cli_session_config_and_prompt_input_probe",
        isolation_supported=supported,
        classification=(
            "capability_isolation_enforced"
            if supported
            else "capability_isolation_unsupported"
        ),
        unsupported_requested=() if supported else ("feature-factory",),
        unrelated_capabilities=(),
        disabled_plugins=("gmail@openai-curated",),
        disabled_mcp_servers=("gmail",),
        skill_context_truncation_warnings=(),
        unrelated_mcp_authentication_attempts=(),
    )


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository, self.project = synthetic_repository(root)
        configuration = controller_configuration(root, self.project)
        configuration.conveyor["runtime_policy"] = {
            "capability_isolation_required": True,
            "compact_output_required": True,
            "default_child_session_budget": 0,
            "maximum_child_session_budget": 1,
        }
        profiles = json.loads(
            (REPOSITORY_ROOT / "config/execution-profiles.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.configuration = replace(
            configuration, execution_profiles=profiles
        )
        self.planner = lambda: {
            "proposed_next_action": "feature_cycle",
            "selected_feature": "F001",
            "application_mutation_expected": True,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_audit(self, *, models=None, capability_plan=None, planner=None):
        selected = ModelSelection(
            "gpt-5.6-terra",
            "medium",
            "agent_file",
            "development-conveyor",
            "/policy",
        )
        with (
            patch(
                "development_conveyor.runtime_audit._model_catalog",
                return_value=(
                    "codex-cli 0.145.0",
                    models
                    or [
                        "gpt-5.6-luna",
                        "gpt-5.6-sol",
                        "gpt-5.6-terra",
                    ],
                    None,
                ),
            ),
            patch(
                "development_conveyor.runtime_audit._model_policy_audit",
                return_value={"ok": True, "errors": [], "returncode": 0},
            ),
            patch(
                "development_conveyor.runtime_audit.resolve_model_selection",
                return_value=selected,
            ),
            patch(
                "development_conveyor.runtime_audit._read_toml",
                return_value={
                    "model": "gpt-5.6-sol",
                    "model_reasoning_effort": "medium",
                },
            ),
            patch(
                "development_conveyor.runtime_audit.build_capability_plan",
                return_value=capability_plan or capability(),
            ),
        ):
            return RuntimeAuditor(
                controller_root=REPOSITORY_ROOT,
                configuration=self.configuration,
                project=self.project,
                planner=planner or self.planner,
            ).audit()

    def test_read_only_audit_passes_and_reports_zero_mutations(self):
        before_head = git(self.repository, "rev-parse", "HEAD")
        before_status = git(self.repository, "status", "--porcelain")
        result = self.run_audit()
        self.assertTrue(result["runtime_policy_passed"])
        self.assertEqual(result["selected_profile"], "bounded_precise")
        self.assertEqual(result["selected_model"], "gpt-5.6-terra")
        self.assertTrue(result["exact_model_available"])
        self.assertFalse(result["fallback_used"])
        self.assertTrue(result["compact_output_contract_present"])
        self.assertTrue(
            all(value == 0 for value in result["mutation_counters"].values())
        )
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), before_head)
        self.assertEqual(git(self.repository, "status", "--porcelain"), before_status)

    def test_unavailable_exact_model_is_a_violation_without_fallback(self):
        result = self.run_audit(models=["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertFalse(result["runtime_policy_passed"])
        self.assertFalse(result["exact_model_available"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(
            result["terminal_classification"], "exact_model_unavailable"
        )

    def test_unsupported_capability_isolation_blocks(self):
        result = self.run_audit(capability_plan=capability(False))
        self.assertFalse(result["runtime_policy_passed"])
        self.assertEqual(
            result["terminal_classification"],
            "capability_isolation_unsupported",
        )
        self.assertEqual(result["mutation_counters"]["models_launched"], 0)

    def test_deterministic_audit_route_launches_no_probe_or_model(self):
        with patch(
            "development_conveyor.runtime_audit.build_capability_plan"
        ) as capability_probe:
            result = self.run_audit(
                planner=lambda: {"proposed_next_action": "verify_consistency"}
            )
        capability_probe.assert_not_called()
        self.assertTrue(result["deterministic_zero_model"])
        self.assertTrue(result["deterministic_zero_child"])
        self.assertEqual(result["mutation_counters"]["models_launched"], 0)


if __name__ == "__main__":
    unittest.main()
