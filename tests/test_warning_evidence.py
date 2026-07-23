from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.contracts import SessionResultEnvelope
from development_conveyor.errors import RecoveryError, SchemaValidationError
from development_conveyor.planning import (
    LEGACY_WARNING_SUMMARY_COMPATIBILITY,
    _legacy_warning_summary_for_recovery,
    compare_queue_validation_evidence,
)
from tests.helpers import synthetic_repository


def queue_envelope(queue_validation: dict) -> dict:
    return {
        "schema_version": 1,
        "workflow_type": "queue_reconciliation",
        "classification": "RECONCILED_READY_WORK",
        "project_id": "synthetic",
        "repository_identity": "repository-identity",
        "transaction_id": "transaction-id",
        "run_id": "run-id",
        "session_id": "session-id",
        "starting_branch": "codex/m0-foundation",
        "starting_commit": "starting-commit",
        "current_commit": "starting-commit",
        "feature_id": None,
        "changed_paths": [],
        "evidence": {
            "queue_validation": queue_validation,
        },
        "next_state": "feature_ready",
    }


def counts(**warnings) -> dict:
    return {
        "valid": True,
        "milestone_found": True,
        "active_milestone": "M0",
        "feature_count": 13,
        "global_feature_count": 98,
        "global_milestone_count": 10,
        "ready": ["F009"],
        **warnings,
    }


class WarningEvidenceTests(unittest.TestCase):
    def test_canonical_warning_count_and_explanatory_scope_pass(self):
        structured = counts(
            warning_count=2,
            warnings_scope="Preparation metadata outside the active milestone.",
            blocking_warnings=[],
            warnings=["M1 warning", "M2 warning"],
        )
        deterministic = counts(
            warnings=["M1 warning", "M2 warning"],
            blocking_warnings=[],
        )
        envelope = SessionResultEnvelope.from_dict(queue_envelope(structured))
        comparison = compare_queue_validation_evidence(structured, deterministic)
        self.assertEqual(envelope.evidence["queue_validation"]["warning_count"], 2)
        self.assertEqual(
            comparison["normalized_structured"]["warnings_scope"],
            "Preparation metadata outside the active milestone.",
        )

    def test_warning_count_mismatch_fails(self):
        with self.assertRaisesRegex(RecoveryError, "warning_count"):
            compare_queue_validation_evidence(
                counts(warning_count=1),
                counts(
                    warnings=["M1 warning", "M2 warning"],
                    blocking_warnings=[],
                ),
            )

    def test_malformed_warning_string_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            before = (repository / "app.txt").read_bytes()
            with self.assertRaisesRegex(
                SchemaValidationError,
                "warnings must be a string array",
            ):
                SessionResultEnvelope.from_dict(
                    queue_envelope(
                        counts(
                            warning_count=1,
                            warnings="prose is not an exact warning list",
                        )
                    )
                )
            self.assertEqual((repository / "app.txt").read_bytes(), before)

    def test_blocking_warning_disagreement_fails(self):
        with self.assertRaisesRegex(RecoveryError, "blocking_warnings"):
            compare_queue_validation_evidence(
                counts(
                    warning_count=1,
                    blocking_warnings=[],
                ),
                counts(
                    warnings=["SECURITY: review required"],
                    blocking_warnings=["SECURITY: review required"],
                ),
            )

    def test_historical_summary_requires_exact_compatibility_identity(self):
        compatibility = LEGACY_WARNING_SUMMARY_COMPATIBILITY
        with tempfile.TemporaryDirectory() as temporary:
            _, project = synthetic_repository(
                Path(temporary),
                controller_project_id="interview-companion",
            )
            project = replace(project, current_state="validation_failed")
            failure = {
                "classification": "historical_warning_summary_shape",
            }
            summary = _legacy_warning_summary_for_recovery(
                project=project,
                original_transaction_id=compatibility["transaction_id"],
                run_id=compatibility["run_id"],
                session_id=compatibility["session_id"],
                current_paths=compatibility["changed_paths"],
                planning_transaction={"error": compatibility["error"]},
                result_queue={"warnings": compatibility["summary"]},
                recoverable_failure=failure,
            )
            self.assertEqual(summary, compatibility["summary"])
            comparison = compare_queue_validation_evidence(
                counts(warnings=summary),
                counts(
                    warnings=["M1 warning", "M2 warning"],
                    blocking_warnings=[],
                ),
                legacy_warning_summary=summary,
            )
            self.assertTrue(
                comparison["normalized_structured"]["legacy_warning_summary"]
            )
            mismatches = {
                "project": {
                    "project": replace(project, project_id="other-project"),
                },
                "transaction": {
                    "original_transaction_id": "different-transaction",
                },
                "run": {"run_id": "different-run"},
                "session": {"session_id": "different-session"},
                "paths": {"current_paths": compatibility["changed_paths"][:-1]},
                "error": {"planning_transaction": {"error": "different error"}},
                "summary": {"result_queue": {"warnings": "different summary"}},
            }
            base = {
                "project": project,
                "original_transaction_id": compatibility["transaction_id"],
                "run_id": compatibility["run_id"],
                "session_id": compatibility["session_id"],
                "current_paths": compatibility["changed_paths"],
                "planning_transaction": {"error": compatibility["error"]},
                "result_queue": {"warnings": compatibility["summary"]},
                "recoverable_failure": failure,
            }
            for name, change in mismatches.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                    RecoveryError,
                    "compatibility identity",
                ):
                    _legacy_warning_summary_for_recovery(**{**base, **change})
            with self.assertRaisesRegex(RecoveryError, "warnings must be a string array"):
                compare_queue_validation_evidence(
                    counts(warnings=summary),
                    counts(
                        warnings=["M1 warning", "M2 warning"],
                        blocking_warnings=[],
                    ),
                )


if __name__ == "__main__":
    unittest.main()
