import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.capability_policy import (
    CORE_CAPABILITIES,
    SkillCapability,
    build_capability_plan,
    requested_capability_allowlist,
)
from development_conveyor.errors import SessionError


def skill(name):
    return SkillCapability(name, f"/skills/{name.replace(':', '/')}/SKILL.md")


class CapabilityPolicyTests(unittest.TestCase):
    def test_queue_allowlist_excludes_unrelated_skills(self):
        requested = requested_capability_allowlist("queue_reconciliation")
        self.assertIn("development-conveyor", requested)
        self.assertIn("feature-inventory", requested)
        self.assertNotIn("gmail:gmail", requested)
        self.assertNotIn("figma:figma-use", requested)

    def test_milestone_routes_keep_required_skill(self):
        self.assertIn(
            "milestone-integrator",
            requested_capability_allowlist("milestone_integration"),
        )
        self.assertIn(
            "milestone-gate",
            requested_capability_allowlist("milestone_gate"),
        )

    def test_architecture_skill_is_not_default(self):
        self.assertNotIn(
            "architecture-audit",
            requested_capability_allowlist("feature_cycle"),
        )
        self.assertIn(
            "architecture-audit",
            requested_capability_allowlist("architecture"),
        )

    def test_only_build_macos_skill_prefix_is_accepted(self):
        with self.assertRaisesRegex(SessionError, "unsupported macOS"):
            requested_capability_allowlist(
                "feature_cycle", relevant_macos_skills=("gmail:gmail",)
            )

    def test_exact_prompt_visible_allowlist_is_enforced(self):
        baseline = (
            skill("development-conveyor"),
            skill("feature-inventory"),
            skill("gmail:gmail"),
        )
        effective = (
            skill("development-conveyor"),
            skill("feature-inventory"),
        )
        with (
            patch(
                "development_conveyor.capability_policy._run_prompt_probe",
                side_effect=[(baseline, (), ()), (effective, (), ())],
            ),
            patch(
                "development_conveyor.capability_policy._config_document",
                return_value={
                    "plugins": {"gmail@openai-curated": {}},
                    "mcp_servers": {"gmail": {}},
                },
            ),
        ):
            plan = build_capability_plan(
                executable="codex",
                cwd=Path("/tmp"),
                action="queue_reconciliation",
            )
        self.assertTrue(plan.isolation_supported)
        self.assertEqual(
            plan.effective_allowlist,
            (*CORE_CAPABILITIES, "development-conveyor", "feature-inventory"),
        )
        self.assertIn("gmail@openai-curated", plan.disabled_plugins)
        self.assertIn("mcp_servers={}", plan.config_args)

    def test_missing_required_skill_fails_closed(self):
        baseline = (skill("development-conveyor"), skill("gmail:gmail"))
        with patch(
            "development_conveyor.capability_policy._run_prompt_probe",
            return_value=(baseline, (), ()),
        ):
            plan = build_capability_plan(
                executable="codex",
                cwd=Path("/tmp"),
                action="queue_reconciliation",
            )
        self.assertFalse(plan.isolation_supported)
        self.assertEqual(plan.classification, "capability_isolation_unsupported")
        self.assertIn("feature-inventory", plan.unsupported_requested)
        self.assertEqual(plan.config_args, ())

    def test_unrelated_effective_skill_fails_closed(self):
        baseline = (
            skill("development-conveyor"),
            skill("feature-inventory"),
            skill("gmail:gmail"),
        )
        with (
            patch(
                "development_conveyor.capability_policy._run_prompt_probe",
                side_effect=[(baseline, (), ()), (baseline, (), ())],
            ),
            patch(
                "development_conveyor.capability_policy._config_document",
                return_value={},
            ),
        ):
            plan = build_capability_plan(
                executable="codex",
                cwd=Path("/tmp"),
                action="queue_reconciliation",
            )
        self.assertFalse(plan.isolation_supported)
        self.assertEqual(plan.unrelated_capabilities, ("gmail:gmail",))

    def test_unrelated_mcp_authentication_attempt_fails_closed(self):
        allowed = (skill("development-conveyor"), skill("feature-inventory"))
        with (
            patch(
                "development_conveyor.capability_policy._run_prompt_probe",
                side_effect=[
                    (allowed, (), ()),
                    (allowed, (), ("Gmail authentication required",)),
                ],
            ),
            patch(
                "development_conveyor.capability_policy._config_document",
                return_value={},
            ),
        ):
            plan = build_capability_plan(
                executable="codex",
                cwd=Path("/tmp"),
                action="queue_reconciliation",
            )
        self.assertFalse(plan.isolation_supported)
        self.assertEqual(
            plan.unrelated_mcp_authentication_attempts,
            ("Gmail authentication required",),
        )

    def test_prompt_only_policy_is_never_reported_as_enforced(self):
        baseline = (skill("development-conveyor"), skill("feature-inventory"))
        with (
            patch(
                "development_conveyor.capability_policy._run_prompt_probe",
                side_effect=[(baseline, (), ()), (baseline, (), ())],
            ),
            patch(
                "development_conveyor.capability_policy._config_document",
                return_value={},
            ),
        ):
            plan = build_capability_plan(
                executable="codex",
                cwd=Path("/tmp"),
                action="queue_reconciliation",
            )
        self.assertEqual(
            plan.enforcement_mechanism,
            "codex_cli_session_config_and_prompt_input_probe",
        )
        self.assertNotEqual(plan.enforcement_mechanism, "prompt_only")

    def test_global_skill_and_plugin_files_are_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text(
                '[plugins."gmail@openai-curated"]\nenabled = true\n',
                encoding="utf-8",
            )
            before = config.read_bytes()
            allowed = (skill("development-conveyor"), skill("feature-inventory"))
            with patch(
                "development_conveyor.capability_policy._run_prompt_probe",
                side_effect=[(allowed, (), ()), (allowed, (), ())],
            ):
                build_capability_plan(
                    executable="codex",
                    cwd=root,
                    action="queue_reconciliation",
                    environment={"HOME": str(root)},
                )
            self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
