from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from development_conveyor.errors import IntegrationPlanError, TransactionError
from development_conveyor.feature_delivery import deliver_feature
from development_conveyor.feature_integration import integrate_feature
from development_conveyor.registry import Project


def project(repository: Path = Path("/feature")) -> Project:
    return Project(
        project_id="synthetic",
        repository=repository,
        enabled=True,
        priority=1,
        active_milestone="M1",
        recovery_branch=None,
        milestone_branch="codex/m1-integration",
        validated_baseline_commit=None,
        queue_location="docs/FEATURE_QUEUE.yaml",
        autonomy_contract_location="docs/AUTONOMY_CONTRACT.md",
        validation_source=".factory/project.yaml",
        automation_mode="one_feature",
        maximum_retries=0,
        schedule=None,
        human_gates=(),
        last_accepted_feature=None,
        last_accepted_commit=None,
        current_state="feature_ready",
        registration_notes="synthetic",
    )


def validation() -> dict:
    return {
        "phase": "validation",
        "outcome": "validated",
        "feature_id": "F1",
        "feature_branch": "codex/f1",
        "milestone_base": "a" * 40,
        "implementation_commit": "b" * 40,
        "implementation_tree": "c" * 40,
        "evidence": {"schema_version": 1},
        "complete_suite_invocations": 0,
        "release_validation_invocations": 0,
    }


class FeatureDeliveryTests(unittest.TestCase):
    @patch("development_conveyor.feature_delivery.integrate_feature")
    @patch("development_conveyor.feature_delivery.accept_feature")
    @patch("development_conveyor.feature_delivery.validate_feature_candidate")
    def test_successful_validate_accept_integrate(self, validate, accept, integrate):
        validate.return_value = validation()
        accept.return_value = {
            "feature_id": "F1",
            "accepted_commit": "b" * 40,
            "transaction_id": "accept-1",
        }
        integrate.return_value = {"outcome": "integrated"}
        result = deliver_feature(
            controller_root=Path("/control"), project=project(), feature_id="F1"
        )
        self.assertEqual(result["outcome"], "integrated")
        self.assertEqual([item.get("phase") for item in result["phases"][:1]], ["validation"])
        accept.assert_called_once()
        integrate.assert_called_once()

    @patch("development_conveyor.feature_delivery.integrate_feature")
    @patch("development_conveyor.feature_delivery.accept_feature")
    @patch("development_conveyor.feature_delivery.validate_feature_candidate")
    def test_validation_failure_stops_delivery(self, validate, accept, integrate):
        validate.return_value = {
            **validation(),
            "outcome": "validation_failed",
        }
        result = deliver_feature(
            controller_root=Path("/control"), project=project(), feature_id="F1"
        )
        self.assertEqual(result["outcome"], "validation_failed")
        accept.assert_not_called()
        integrate.assert_not_called()

    @patch("development_conveyor.feature_delivery.integrate_feature")
    @patch("development_conveyor.feature_delivery.accept_feature")
    @patch("development_conveyor.feature_delivery.validate_feature_candidate")
    def test_acceptance_failure_stops_integration(self, validate, accept, integrate):
        validate.return_value = validation()
        accept.side_effect = TransactionError("candidate drift")
        result = deliver_feature(
            controller_root=Path("/control"), project=project(), feature_id="F1"
        )
        self.assertEqual(result["outcome"], "acceptance_failed")
        integrate.assert_not_called()

    @patch("development_conveyor.feature_delivery.integrate_feature")
    @patch("development_conveyor.feature_delivery.accept_feature")
    @patch("development_conveyor.feature_delivery.validate_feature_candidate")
    def test_ledger_only_acceptance_is_forwarded(self, validate, accept, integrate):
        validate.return_value = validation()
        authority = {
            "feature_id": "F1",
            "accepted_commit": "b" * 40,
            "transaction_id": "accept-ledger",
        }
        accept.return_value = authority
        integrate.return_value = {"outcome": "integrated"}
        deliver_feature(
            controller_root=Path("/control"), project=project(), feature_id="F1"
        )
        self.assertEqual(
            integrate.call_args.kwargs["acceptance_result"], authority
        )

    def _advanced_result(self, *, target_path: Path = Path("/integration")) -> dict:
        source = MagicMock()
        source.rev_parse.side_effect = lambda ref, check=False: (
            "b" * 40 if ref == "codex/f1" else "d" * 40
        )
        source.branch_worktree.return_value = target_path
        target = MagicMock()
        target.current_branch = "codex/m1-integration"
        target.is_clean = True
        target.git_operation_state.return_value = {}
        target.head = "d" * 40
        queue = MagicMock()
        queue.feature.return_value = {
            "id": "F1",
            "milestone": "M1",
            "branch": "codex/f1",
            "dependencies": [],
        }
        with tempfile.TemporaryDirectory() as control:
            with (
                patch(
                    "development_conveyor.feature_integration.RepositoryInspector",
                    side_effect=[source, target],
                ),
                patch(
                    "development_conveyor.feature_integration.FeatureQueue.from_location",
                    return_value=queue,
                ),
                patch(
                    "development_conveyor.feature_integration.acceptance_metadata_from_ledger",
                    return_value={
                        "transaction_id": "accept-1",
                        "milestone_base": "a" * 40,
                    },
                ),
            ):
                return integrate_feature(
                    controller_root=Path(control),
                    project=project(),
                    feature_id="F1",
                )

    def test_configured_integration_worktree_is_reported(self):
        result = self._advanced_result()
        self.assertEqual(result["integration_worktree"], "/integration")
        self.assertEqual(result["integration_branch"], "codex/m1-integration")

    def test_feature_and_integration_worktrees_are_distinct(self):
        with self.assertRaises(IntegrationPlanError):
            self._advanced_result(target_path=Path("/feature"))

    def test_advanced_target_returns_reconciliation_required(self):
        result = self._advanced_result()
        self.assertEqual(result["outcome"], "reconciliation_required")
        self.assertEqual(result["integration_head"], "d" * 40)

    @patch("development_conveyor.feature_delivery.integrate_feature")
    @patch("development_conveyor.feature_delivery.accept_feature")
    @patch("development_conveyor.feature_delivery.validate_feature_candidate")
    def test_delivery_reports_zero_complete_and_release_invocations(
        self, validate, accept, integrate
    ):
        validate.return_value = validation()
        accept.return_value = {
            "feature_id": "F1",
            "accepted_commit": "b" * 40,
            "transaction_id": "accept-1",
            "complete_suite_invocations": 0,
            "release_validation_invocations": 0,
        }
        integrate.return_value = {
            "outcome": "integrated",
            "complete_suite_invocations": 0,
            "release_validation_invocations": 0,
        }
        result = deliver_feature(
            controller_root=Path(tempfile.gettempdir()),
            project=project(),
            feature_id="F1",
        )
        self.assertEqual(result["complete_suite_invocations"], 0)
        self.assertEqual(result["release_validation_invocations"], 0)


if __name__ == "__main__":
    unittest.main()
