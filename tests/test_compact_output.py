import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.compact_output import (
    TERMINAL_REPORT_FIELDS,
    compact_output_contract,
    normalize_workflow_label,
    summarize_command_result,
    validate_terminal_workflow,
)
from development_conveyor.compatibility import CompatibilityResult
from development_conveyor.errors import SessionError
from development_conveyor.repository import RepositoryInspector
from development_conveyor.sessions import SessionLauncher, SessionRequest
from tests.helpers import REPOSITORY_ROOT, git, synthetic_repository


class CompactOutputTests(unittest.TestCase):
    def test_contract_contains_all_default_fields(self):
        contract = compact_output_contract(
            workflow_type="feature_cycle",
            task="F001",
            selected_profile="bounded_precise",
            exact_model="gpt-5.6-terra",
            reasoning_effort="medium",
            policy_source="selected_feature_profile",
        )
        self.assertEqual(contract["required_fields"], list(TERMINAL_REPORT_FIELDS))
        self.assertEqual(
            contract["identity"]["workflow_type"], "feature_execution"
        )

    def test_documented_feature_alias_normalizes(self):
        self.assertEqual(
            normalize_workflow_label("feature_cycle"), "feature_execution"
        )
        validate_terminal_workflow(
            invoked="feature_cycle", reported="feature_execution"
        )

    def test_conflicting_workflow_label_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            validate_terminal_workflow(
                invoked="queue_reconciliation",
                reported="feature_execution",
            )

    def test_successful_command_output_is_hash_addressed_and_bounded(self):
        def runner(command, *, cwd):
            return subprocess.CompletedProcess(command, 0, "x" * 5000, "")

        _, summary = summarize_command_result(
            ["python3", "-V"],
            runner=runner,
            cwd=Path("/tmp"),
        )
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(len(summary["output_hash"]), 64)
        self.assertEqual(len(summary["bounded_tail_or_summary"]), 1000)
        self.assertIn("duration", summary)

    def test_prompt_contains_stable_compact_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            launcher = SessionLauncher(
                REPOSITORY_ROOT,
                {"codex": {"executable": "codex", "session_timeout_seconds": 60}},
            )
            request = SessionRequest(
                "feature_cycle",
                project,
                "run",
                "one_feature",
                feature="F001",
                transaction_id="transaction",
                repository_identity=RepositoryInspector(repository).identity()[
                    "repository_id"
                ],
                starting_branch="codex/m0-foundation",
                starting_commit=git(repository, "rev-parse", "HEAD"),
                parent_session_budget=1,
                child_session_budget=0,
                planned_model="gpt-5.6-terra",
                planned_reasoning="medium",
                model_plan_source="selected_feature_profile",
                selected_profile="bounded_precise",
                context_files=("docs/features/F001.md",),
            )
            compatible = CompatibilityResult(
                classification="compatible",
                executable="codex",
                detected_version="0.145.0",
                required_minimum_version=None,
                effective_model="gpt-5.6-terra",
                effective_reasoning="medium",
                policy_source="selected_feature_profile",
                policy_role="direct-feature-session",
                compatible=True,
                diagnostic="ok",
                remediation="none",
                validation_command="scripts/conveyor doctor",
            )
            with (
                patch.object(launcher, "compatibility", return_value=compatible),
                patch.object(launcher, "_verify_zero_child_capability"),
            ):
                plan = launcher.plan(request)
        self.assertTrue(plan.compact_output_contract_present)
        self.assertIn("successful_command_summary_fields", plan.prompt)
        self.assertIn('"recovery_instruction"', plan.prompt)
        self.assertNotIn("be concise", plan.prompt.lower())

    def test_missing_policy_source_blocks_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(Path(temporary))
            launcher = SessionLauncher(
                REPOSITORY_ROOT,
                {"codex": {"executable": "codex", "session_timeout_seconds": 60}},
            )
            request = SessionRequest(
                "queue_reconciliation",
                project,
                "run",
                "one_feature",
                planned_model="gpt-5.6-luna",
                planned_reasoning="high",
            )
            with self.assertRaisesRegex(SessionError, "policy source"):
                launcher.plan(request)


if __name__ == "__main__":
    unittest.main()
