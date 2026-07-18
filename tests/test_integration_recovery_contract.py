from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import QueueError
from development_conveyor.queue import FeatureQueue
from development_conveyor.sessions import parse_integration_terminal_result
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


INTEGRATIONCTL = Path.home() / ".agents/skills/milestone-integrator/scripts/integrationctl.py"


def load_integrationctl():
    spec = importlib.util.spec_from_file_location("tested_integrationctl", INTEGRATIONCTL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assistant(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def human_terminal() -> str:
    descriptor = json.dumps({
        "schema_version": 1,
        "gate_classification": "synthetic_integration_gate",
        "reason": "Synthetic approval is required.",
        "blocker_categories": ["product_decision"],
        "retryable": False,
    }, separators=(",", ":"))
    return f"CONVEYOR_INTEGRATION_GATE={descriptor}\n## HUMAN_DECISION_REQUIRED"


class IntegrationTerminalContractTests(unittest.TestCase):
    def test_integration_result_schema_matches_human_and_retry_shapes_without_self_reference(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas/integration-result.schema.json").read_text())
        self.assertIn("human_decision", schema["properties"])
        self.assertIn("retryable", schema["properties"])
        rendered = json.dumps(schema, sort_keys=True)
        self.assertNotIn('"$ref"', rendered)
        conditionals = schema["allOf"]
        required_by_classification = {
            item["if"]["properties"]["classification"]["const"]: set(item["then"]["required"])
            for item in conditionals
        }
        self.assertEqual(required_by_classification["HUMAN_DECISION_REQUIRED"], {"human_decision"})
        self.assertTrue({"retryable", "failure_classification", "hypothesis", "remediation_action", "supporting_evidence"}.issubset(
            required_by_classification["RETRYABLE_INTEGRATION_FAILURE"]
        ))

    def test_every_terminal_classification_is_exact(self):
        for marker in (
            "INTEGRATED", "VALIDATION_FAILED", "HUMAN_DECISION_REQUIRED",
            "SEMANTIC_CONFLICT", "RETRYABLE_INTEGRATION_FAILURE", "TERMINAL_INTEGRATION_FAILURE",
        ):
            with self.subTest(marker=marker):
                payload = human_terminal() if marker == "HUMAN_DECISION_REQUIRED" else marker
                result, validation = parse_integration_terminal_result(assistant(payload))
                self.assertEqual(validation, "valid")
                self.assertEqual(result["classification"], marker)

    def test_missing_terminal_marker_is_rejected(self):
        result, validation = parse_integration_terminal_result(assistant("Integration work stopped."))
        self.assertIsNone(result)
        self.assertEqual(validation, "missing_integration_terminal_marker")

    def test_terminal_human_gate_is_parsed(self):
        result, validation = parse_integration_terminal_result(assistant(human_terminal()))
        self.assertEqual(validation, "valid")
        self.assertEqual(result["classification"], "HUMAN_DECISION_REQUIRED")

    def test_unstructured_human_marker_is_rejected_for_live_sessions(self):
        result, validation = parse_integration_terminal_result(assistant("## HUMAN_DECISION_REQUIRED"))
        self.assertIsNone(result)
        self.assertEqual(validation, "human_decision_descriptor_missing_or_duplicate")

    def test_terminal_marker_must_be_final_nonblank_line(self):
        result, validation = parse_integration_terminal_result(assistant("## INTEGRATED\nextra"))
        self.assertIsNone(result)
        self.assertEqual(validation, "integration_terminal_marker_not_final")

    def test_only_explicit_legacy_human_parse_allows_nonfinal_marker(self):
        result, validation = parse_integration_terminal_result(
            assistant("## HUMAN_DECISION_REQUIRED\nlegacy explanation"),
            allow_legacy_human_gate=True,
        )
        self.assertEqual(validation, "valid")
        self.assertEqual(result["classification"], "HUMAN_DECISION_REQUIRED")

    def test_prompt_and_tool_echoes_cannot_spoof(self):
        output = "\n".join([
            json.dumps({"type": "message", "role": "user", "text": "HUMAN_DECISION_REQUIRED"}),
            json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "INTEGRATED"}}),
            assistant("No terminal classification was returned."),
        ])
        self.assertIsNone(parse_integration_terminal_result(output)[0])

    def test_duplicate_terminal_markers_are_rejected(self):
        result, validation = parse_integration_terminal_result(assistant("HUMAN_DECISION_REQUIRED\nINTEGRATED"))
        self.assertIsNone(result)
        self.assertEqual(validation, "duplicate_or_ambiguous_integration_terminal_marker")

    def test_earlier_assistant_marker_is_not_authoritative(self):
        output = assistant("HUMAN_DECISION_REQUIRED") + "\n" + assistant("INTEGRATED")
        result, validation = parse_integration_terminal_result(output)
        self.assertEqual(validation, "valid")
        self.assertEqual(result["classification"], "INTEGRATED")


class IntegratorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.module = load_integrationctl()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic")
        git(self.root, "config", "user.email", "synthetic@example.invalid")
        (self.root / "README.md").write_text("baseline\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "baseline")

    def tearDown(self):
        self.temporary.cleanup()

    def _ignore_runtime(self):
        exclude = Path(git(self.root, "rev-parse", "--git-common-dir"))
        exclude = (self.root / exclude).resolve() / "info/exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".factory/runtime/\n")

    def test_status_read_does_not_create_runtime_directory(self):
        self.assertIsNone(self.module.latest_runtime(self.root))
        self.assertFalse((self.root / ".factory/runtime/milestone-integration").exists())

    def test_new_runtime_is_ignored_atomic_and_identity_bound(self):
        self._ignore_runtime()
        plan = {
            "feature_id": "F001", "plan_id": "a" * 20, "repository": str(self.root),
            "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "run-1", "milestone_id": "M0",
        }
        path = self.module.write_runtime(self.root, plan, "discovered")
        self.assertTrue(path.is_file())
        self.assertEqual(self.module.latest_runtime(self.root)["runtime_identity"]["run_id"], "run-1")
        self.assertFalse(self.module.legacy_runtime_dir(self.root).exists())
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_identity_mismatch_is_rejected(self):
        self._ignore_runtime()
        directory = self.module.runtime_dir(self.root, create=True)
        write_json(directory / "latest.json", {
            "runtime_identity": {
                "repository": str(self.root), "repository_identity": "wrong",
                "project_id": "synthetic", "run_id": "run-1", "milestone_id": "M0", "feature_id": "F001",
            }
        })
        with self.assertRaisesRegex(self.module.IntegrationError, "identity mismatch"):
            self.module.latest_runtime(self.root)

    def test_runtime_context_mismatches_are_rejected_per_identity_dimension(self):
        self._ignore_runtime()
        plan = {
            "feature_id": "F001", "plan_id": "d" * 20, "repository": str(self.root),
            "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "run-1", "milestone_id": "M0",
        }
        self.module.write_runtime(self.root, plan, "discovered")
        cases = {
            "project_id": ({"CONVEYOR_PROJECT_ID": "other", "CONVEYOR_RUN_ID": "run-1", "CONVEYOR_FEATURE": "F001"}, {}),
            "run_id": ({"CONVEYOR_PROJECT_ID": "synthetic", "CONVEYOR_RUN_ID": "other", "CONVEYOR_FEATURE": "F001"}, {}),
            "milestone_id": ({"CONVEYOR_PROJECT_ID": "synthetic", "CONVEYOR_RUN_ID": "run-1", "CONVEYOR_FEATURE": "F001"}, {"active_milestone": "M1"}),
            "feature_id": ({"CONVEYOR_PROJECT_ID": "synthetic", "CONVEYOR_RUN_ID": "run-1", "CONVEYOR_FEATURE": "other"}, {}),
        }
        for dimension, (environment, cycle) in cases.items():
            with self.subTest(dimension=dimension):
                if cycle:
                    write_json(self.root / ".factory/conveyor-state.json", cycle)
                elif (self.root / ".factory/conveyor-state.json").exists():
                    (self.root / ".factory/conveyor-state.json").unlink()
                with mock.patch.dict(os.environ, environment, clear=False):
                    with self.assertRaisesRegex(self.module.IntegrationError, dimension):
                        self.module.latest_runtime(self.root)

    def test_current_p0_runtime_matches_phase_zero_queue_alias(self):
        self._ignore_runtime()
        plan = {
            "feature_id": "P0-001", "plan_id": "e" * 20, "repository": str(self.root),
            "repository_identity": self.module.repository_identity(self.root),
            "project_id": "case-manager", "run_id": "run-current", "milestone_id": "phase-0",
        }
        self.module.write_runtime(self.root, plan, "discovered")
        write_json(self.root / ".factory/conveyor-state.json", {
            "project_id": "case-manager", "conveyor_run_id": "run-current",
            "active_milestone": "P0", "current_feature": "P0-001",
        })
        (self.root / ".factory/project.yaml").write_text("{}\n")
        queue = {
            "milestones": [{"id": "phase-0"}],
            "features": [{"id": "P0-001", "milestone": "phase-0"}],
        }
        environment = {
            "CONVEYOR_PROJECT_ID": "case-manager", "CONVEYOR_RUN_ID": "run-current",
            "CONVEYOR_FEATURE": "P0-001",
        }
        with mock.patch.object(self.module, "load_context", return_value=({}, self.root / "docs/FEATURE_QUEUE.yaml", queue)):
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertEqual(self.module.latest_runtime(self.root)["runtime_identity"]["milestone_id"], "phase-0")

    def test_legacy_git_runtime_remains_readable_without_new_write(self):
        legacy = self.module.legacy_runtime_dir(self.root)
        legacy.mkdir()
        write_json(legacy / "latest.json", {"schema_version": 1, "phase": "validated"})
        self.assertEqual(self.module.latest_runtime(self.root)["phase"], "validated")
        self.assertFalse(self.module.runtime_dir(self.root).exists())

    def test_dual_runtime_locations_are_ambiguous(self):
        self._ignore_runtime()
        new = self.module.runtime_dir(self.root, create=True)
        legacy = self.module.legacy_runtime_dir(self.root)
        legacy.mkdir()
        write_json(new / "latest.json", {
            "schema_version": 1,
            "runtime_identity": {
                "repository": str(self.root),
                "repository_identity": self.module.repository_identity(self.root),
                "project_id": "synthetic", "run_id": "run-1",
                "milestone_id": "M0", "feature_id": "F001",
            },
        })
        write_json(legacy / "latest.json", {"schema_version": 1})
        with self.assertRaisesRegex(self.module.IntegrationError, "disagree"):
            self.module.latest_runtime(self.root)

    def test_unignored_runtime_write_is_refused_without_creating_directory(self):
        plan = {
            "feature_id": "F001", "plan_id": "b" * 20, "repository": str(self.root),
            "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "run-1", "milestone_id": "M0",
        }
        with self.assertRaisesRegex(self.module.IntegrationError, "controller must install"):
            self.module.write_runtime(self.root, plan, "discovered")
        self.assertFalse(self.module.runtime_dir(self.root).exists())

    def test_narrow_latest_only_ignore_cannot_authorize_plan_record_write(self):
        exclude = Path(git(self.root, "rev-parse", "--git-common-dir"))
        exclude = (self.root / exclude).resolve() / "info/exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".factory/runtime/milestone-integration/latest.json\n")
        plan = {
            "feature_id": "F001", "plan_id": "c" * 20, "repository": str(self.root),
            "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "run-1", "milestone_id": "M0",
        }
        with self.assertRaisesRegex(self.module.IntegrationError, "runtime path is not ignored"):
            self.module.write_runtime(self.root, plan, "discovered")
        self.assertFalse(self.module.runtime_dir(self.root).exists())

    def test_foreign_lease_cannot_be_released_or_adopted(self):
        (self.root / ".factory/locks").mkdir(parents=True)
        foreign = {
            "repository": str(self.root), "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "other-run", "feature_id": "F001",
            "accepted_commit": self.module.full_commit(self.root, "HEAD"), "branch": "main",
            "agent_run": "foreign", "purpose": "integration", "integration_phase": "integration",
        }
        write_json(self.module.lease_path(self.root), foreign)
        with self.assertRaisesRegex(self.module.IntegrationError, "owned by another"):
            self.module.release_lease(self.root, {**foreign, "agent_run": "local"})
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "local"}):
            with self.assertRaisesRegex(self.module.IntegrationError, "another agent run"):
                self.module.recovery_lease(self.root, {
                    "repository": str(self.root), "repository_identity": foreign["repository_identity"],
                    "project_id": "synthetic", "run_id": "other-run", "feature_id": "F001",
                    "accepted_commit": foreign["accepted_commit"], "milestone_branch": "main",
                }, "integration_recovery")

    def test_abort_never_unlinks_a_foreign_lease(self):
        (self.root / "conflict.txt").write_text("base\n")
        git(self.root, "add", "conflict.txt")
        git(self.root, "commit", "-m", "add conflict fixture")
        git(self.root, "switch", "-c", "topic")
        (self.root / "conflict.txt").write_text("topic\n")
        git(self.root, "commit", "-am", "topic change")
        topic = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "main")
        (self.root / "conflict.txt").write_text("main\n")
        git(self.root, "commit", "-am", "main change")
        subprocess.run(["git", "cherry-pick", topic], cwd=self.root, check=False, capture_output=True)
        cherry_pick_head = Path(git(self.root, "rev-parse", "--git-path", "CHERRY_PICK_HEAD"))
        if not cherry_pick_head.is_absolute():
            cherry_pick_head = self.root / cherry_pick_head
        self.assertTrue(cherry_pick_head.exists())
        foreign = {
            "repository": str(self.root), "repository_identity": self.module.repository_identity(self.root),
            "project_id": "synthetic", "run_id": "foreign-run", "feature_id": "F001",
            "accepted_commit": topic, "branch": "main", "agent_run": "foreign",
            "purpose": "integration", "integration_phase": "integration",
        }
        write_json(self.module.lease_path(self.root), foreign)
        with mock.patch("builtins.print"):
            self.assertEqual(self.module.main(["recover", "--root", str(self.root), "--abort-cherry-pick"]), 0)
        self.assertEqual(self.module.read_lease(self.root)["agent_run"], "foreign")


class PlanningBaselineProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_integrationctl()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic")
        git(self.root, "config", "user.email", "synthetic@example.invalid")
        (self.root / "base.txt").write_text("base\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        (self.root / "fixture.txt").write_text("integrated\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "feat: P0-002")
        self.previous = git(self.root, "rev-parse", "HEAD")
        (self.root / ".factory").mkdir()
        (self.root / "docs/features").mkdir(parents=True)
        (self.root / "docs/roadmap").mkdir(parents=True)
        write_json(self.root / ".factory/project.yaml", {"schema_version": 1})
        feature = {
            "id": "P0-001", "status": "ready", "milestone": "phase-0", "dependencies": [],
            "spec": "docs/features/P0-001.md", "acceptance_criteria": ["criterion"],
            "requires_human_decision": False,
        }
        milestone = {
            "id": "phase-0", "base_commit": self.base, "integration_branch": "codex/p0-foundation",
            "integrated_features": ["P0-002"], "last_validated_commit": self.previous,
        }
        write_json(self.root / "docs/FEATURE_QUEUE.yaml", {"schema_version": 1, "milestones": [milestone], "features": [feature]})
        (self.root / "docs/features/P0-001.md").write_text("# P0-001\n")
        (self.root / "docs/FEATURE_CATALOG.md").write_text("P0-001\n")
        (self.root / "docs/ROADMAP.md").write_text("P0-001 through P0-014\n")
        (self.root / "docs/roadmap/DEVELOPMENT_ROADMAP.md").write_text("P0-001 P0-014\n")
        (self.root / "docs/CURRENT_STATUS.md").write_text("Phase 0 P0-001\n")
        (self.root / "docs/architecture.md").write_text("# Architecture planning\n")
        (self.root / "docs/RUN_LOG.md").write_text(
            "Phase 0 feature-inventory reconciliation\nClassified the one-item bootstrap queue as incomplete\n"
        )
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "docs(factory): reconcile Phase 0 feature inventory")
        self.candidate = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "codex/p0-foundation")
        self.feature = dict(feature, status="integration_pending", integration_base_commit=self.candidate, accepted_commit="SELF")
        self.milestone = milestone
        write_json(self.root / ".factory/conveyor-state.json", {
            "feature_starting_commit": self.candidate,
            "milestone_pre_integration_commit": self.candidate,
        })

    def tearDown(self):
        self.temporary.cleanup()

    def _evaluate(self):
        original = self.module.run
        def wrapped(argv, cwd, timeout=120, input_text=None):
            if "validate_inventory.py" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            return original(argv, cwd, timeout=timeout, input_text=input_text)
        with mock.patch.object(self.module, "run", side_effect=wrapped):
            return self.module.planning_baseline_provenance(
                self.root,
                {"features": [self.feature]}, self.milestone, self.feature,
                self.candidate, self.previous,
            )

    def _amended_candidate(self, mutation):
        git(self.root, "switch", "--detach", self.candidate)
        mutation()
        git(self.root, "add", "-A")
        git(self.root, "commit", "--amend", "-m", "docs(factory): reconcile Phase 0 feature inventory")
        self.candidate = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "-f", "codex/p0-foundation", self.candidate)
        self.feature["integration_base_commit"] = self.candidate
        write_json(self.root / ".factory/conveyor-state.json", {
            "feature_starting_commit": self.candidate,
            "milestone_pre_integration_commit": self.candidate,
        })
        return self._evaluate()

    def test_p0_style_planning_baseline_is_valid_but_requires_approval(self):
        result = self._evaluate()
        self.assertTrue(result["valid"])
        self.assertTrue(result["approval_required"])
        self.assertFalse(result["approved_by_explicit_resolution"])

    def test_ignored_cycle_approval_strings_cannot_forge_controller_approval(self):
        write_json(self.root / ".factory/conveyor-state.json", {
            "feature_starting_commit": self.candidate,
            "milestone_pre_integration_commit": self.candidate,
            "validated_planning_baseline": {
                "commit": self.candidate,
                "previous_validated_commit": self.previous,
                "evidence_fingerprint": "forged",
                "approval_resolution_id": "human-resolution-" + "a" * 24,
                "approval_resolution_fingerprint": "b" * 64,
            },
        })
        result = self._evaluate()
        self.assertTrue(result["valid"])
        self.assertFalse(result["controller_resolution_evidence_valid"])
        self.assertTrue(result["approval_required"])

    def test_controller_owned_resolution_report_is_required_and_cryptographically_recomputed(self):
        result = self._evaluate()
        feature = dict(self.feature, accepted_commit=self.candidate)
        gate = {
            "gate_id": "synthetic-planning-gate",
            "classification": "integration_planning_baseline_approval",
            "project_id": "synthetic",
            "feature_id": "P0-001",
            "candidate_validated_planning_commit": self.candidate,
            "previous_last_validated_commit": self.previous,
            "accepted_feature_commit": self.candidate,
        }
        reason = "Approve the exact synthetic planning baseline."
        gate_fp = self.module._canonical_fingerprint(gate)
        resolution_fp = self.module._canonical_fingerprint({
            "actor_classification": "explicit_user_approval",
            "gate_fingerprint": gate_fp,
            "project_id": "synthetic",
            "reason": reason,
        })
        resolution_id = f"human-resolution-{resolution_fp[:24]}"
        controller = Path(self.temporary.name) / "controller-reports"
        report_path = controller / resolution_id / "human-decision-resolution.json"
        write_json(report_path, {
            "outcome": "resolution_accepted", "applied": True, "state_written": True,
            "actor_classification": "explicit_user_approval", "project_id": "synthetic",
            "original_gate": gate, "original_gate_fingerprint": gate_fp,
            "resolution_fingerprint": resolution_fp, "resolution_id": resolution_id,
            "user_provided_reason": reason,
        })
        baseline = {
            "commit": self.candidate, "previous_validated_commit": self.previous,
            "evidence_fingerprint": result["evidence_fingerprint"],
            "approval_resolution_id": resolution_id,
            "approval_resolution_fingerprint": resolution_fp,
        }
        environment = {
            "CONVEYOR_CONTROLLER_REPORT_ROOT": str(controller),
            "CONVEYOR_PLANNING_RESOLUTION_REPORT": str(report_path),
            "CONVEYOR_PROJECT_ID": "synthetic",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            approved, facts = self.module.trusted_planning_approval(
                self.root, self.candidate, self.previous, feature, baseline,
                result["evidence_fingerprint"],
            )
        self.assertTrue(approved)
        self.assertTrue(facts["resolution_fingerprint_matches"])
        report = json.loads(report_path.read_text())
        report["resolution_fingerprint"] = "forged"
        write_json(report_path, report)
        with mock.patch.dict(os.environ, environment, clear=False):
            approved, _ = self.module.trusted_planning_approval(
                self.root, self.candidate, self.previous, feature, baseline,
                result["evidence_fingerprint"],
            )
        self.assertFalse(approved)

    def test_missing_reconciliation_report_is_rejected(self):
        result = self._amended_candidate(
            lambda: (self.root / "docs/RUN_LOG.md").write_text("missing classification\n")
        )
        self.assertFalse(result["valid"])
        self.assertFalse(result["reconciliation_report_classifies_incomplete"])

    def test_arbitrary_docs_subject_is_rejected(self):
        git(self.root, "switch", "--detach", self.candidate)
        (self.root / "docs/RUN_LOG.md").write_text(
            "Phase 0 feature-inventory reconciliation\nClassified the one-item bootstrap queue as incomplete\nextra\n"
        )
        git(self.root, "add", "docs/RUN_LOG.md")
        git(self.root, "commit", "--amend", "-m", "docs: arbitrary reconciliation")
        self.candidate = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "-f", "codex/p0-foundation", self.candidate)
        self.feature["integration_base_commit"] = self.candidate
        write_json(self.root / ".factory/conveyor-state.json", {
            "feature_starting_commit": self.candidate,
            "milestone_pre_integration_commit": self.candidate,
        })
        result = self._evaluate()
        self.assertFalse(result["valid"])
        self.assertFalse(result["subject_classified_planning_reconciliation"])

    def test_queue_roadmap_or_spec_mismatch_is_rejected(self):
        mutations = {
            "roadmap": lambda: (self.root / "docs/ROADMAP.md").write_text("P0-001 only\n"),
            "spec": lambda: (self.root / "docs/features/P0-001.md").unlink(),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                result = self._amended_candidate(mutation)
                self.assertFalse(result["valid"])

    def test_source_or_test_divergence_is_rejected(self):
        for directory in ("Sources", "tests"):
            with self.subTest(directory=directory):
                result = self._amended_candidate(
                    lambda directory=directory: (
                        (self.root / directory).mkdir(exist_ok=True),
                        (self.root / directory / "divergence.txt").write_text("not planning\n"),
                    )
                )
                self.assertFalse(result["valid"])
                self.assertFalse(result["production_and_product_tests_absent"])

    def test_invalid_direct_ancestry_is_rejected(self):
        git(self.root, "switch", "--detach", self.candidate)
        (self.root / "docs/RUN_LOG.md").write_text(
            "Phase 0 feature-inventory reconciliation\nClassified the one-item bootstrap queue as incomplete\nintermediate\n"
        )
        git(self.root, "add", "docs/RUN_LOG.md")
        git(self.root, "commit", "-m", "docs: intermediate")
        (self.root / "docs/RUN_LOG.md").write_text(
            "Phase 0 feature-inventory reconciliation\nClassified the one-item bootstrap queue as incomplete\nfinal\n"
        )
        git(self.root, "add", "docs/RUN_LOG.md")
        git(self.root, "commit", "-m", "docs(factory): reconcile Phase 0 feature inventory")
        self.candidate = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "-f", "codex/p0-foundation", self.candidate)
        self.feature["integration_base_commit"] = self.candidate
        write_json(self.root / ".factory/conveyor-state.json", {
            "feature_starting_commit": self.candidate,
            "milestone_pre_integration_commit": self.candidate,
        })
        result = self._evaluate()
        self.assertFalse(result["valid"])
        self.assertFalse(result["direct_parent_matches"])

    def test_current_worktree_cannot_repair_inconsistent_candidate_evidence(self):
        result = self._amended_candidate(
            lambda: (
                (self.root / "docs/ROADMAP.md").write_text("unrelated roadmap\n"),
                (self.root / "docs/roadmap/DEVELOPMENT_ROADMAP.md").write_text("unrelated roadmap\n"),
            )
        )
        self.assertFalse(result["roadmap_agrees"])
        (self.root / "docs/ROADMAP.md").write_text("P0-001 through P0-014\n")
        (self.root / "docs/roadmap/DEVELOPMENT_ROADMAP.md").write_text("P0-001 P0-014\n")
        self.assertFalse(self._evaluate()["roadmap_agrees"])

    def test_inventory_validator_executes_against_detached_candidate_snapshot(self):
        observed = {}

        def validate(argv, cwd, timeout=120, input_text=None):
            observed["cwd"] = Path(cwd)
            observed["candidate_subject"] = (Path(cwd) / "docs/RUN_LOG.md").read_text()
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.object(self.module, "run", side_effect=validate):
            self.assertTrue(self.module.validate_inventory_at_commit(self.root, self.candidate))
        self.assertNotEqual(observed["cwd"], self.root)
        self.assertIn("feature-inventory reconciliation", observed["candidate_subject"])

    def test_arbitrary_factory_queue_rewrite_is_unexplained(self):
        queue_path = self.root / "docs/FEATURE_QUEUE.yaml"
        queue = json.loads(queue_path.read_text())
        queue["features"][0]["title"] = "Arbitrary rewrite"
        write_json(queue_path, queue)
        git(self.root, "add", "docs/FEATURE_QUEUE.yaml")
        git(self.root, "commit", "-m", "factory: arbitrary queue rewrite")
        commit = git(self.root, "rev-parse", "HEAD")
        self.assertFalse(self.module.factory_metadata_commit(self.root, commit, "docs/FEATURE_QUEUE.yaml"))

    def test_exact_integrating_subject_cannot_hide_queue_or_milestone_rewrites(self):
        queue_path = self.root / "docs/FEATURE_QUEUE.yaml"
        queue = json.loads(queue_path.read_text())
        queue["features"][0].update({
            "id": "F1", "title": "Original title", "status": "accepted",
            "integration_status": "pending", "accepted_commit": self.previous,
        })
        queue["milestones"][0].update({
            "id": "M0", "status": "active", "last_validated_commit": self.previous,
        })
        queue["features"][0]["milestone"] = "M0"
        write_json(queue_path, queue)
        git(self.root, "add", "docs/FEATURE_QUEUE.yaml")
        git(self.root, "commit", "-m", "test: establish F1 queue state")

        queue = json.loads(queue_path.read_text())
        queue["features"][0].update({
            "title": "Hidden rewrite", "status": "integrating", "integration_status": "integrating",
            "integration_attempted_at": "2026-07-18T00:00:00+00:00", "accepted_commit": self.base,
        })
        queue["milestones"][0].update({
            "status": "gate_passed", "last_validated_commit": self.base,
        })
        write_json(queue_path, queue)
        git(self.root, "add", "docs/FEATURE_QUEUE.yaml")
        git(self.root, "commit", "-m", "factory: mark F1 integrating")
        commit = git(self.root, "rev-parse", "HEAD")
        self.assertIsNone(self.module.factory_commit_semantics(self.root, commit, "docs/FEATURE_QUEUE.yaml"))
        self.assertFalse(self.module.factory_metadata_commit(self.root, commit, "docs/FEATURE_QUEUE.yaml"))


class IntegrationSelectionTests(unittest.TestCase):
    @staticmethod
    def _document(statuses):
        return {
            "milestones": [{"id": "M0"}],
            "features": [
                {
                    "id": feature_id, "title": feature_id, "status": status, "milestone": "M0",
                    "priority": priority, "dependencies": [], "integration_status": "pending",
                    "spec": f"docs/{feature_id}.md", "acceptance_criteria": ["done"],
                }
                for feature_id, status, priority in statuses
            ],
        }

    def test_accepted_work_precedes_new_ready_work(self):
        queue = FeatureQueue(self._document([("P0-001", "integration_pending", 10), ("P0-003", "ready", 20)]))
        self.assertEqual(queue.select_integration("M0").feature_id, "P0-001")
        self.assertEqual(queue.select_next("M0").feature_id, "P0-003")
        self.assertEqual(queue.reconciliation_classification("M0"), "reconciled_integration_pending")

    def test_multiple_integration_candidates_are_a_hard_ambiguity(self):
        queue = FeatureQueue(self._document([("F1", "accepted", 1), ("F2", "integration_pending", 2)]))
        with self.assertRaisesRegex(QueueError, "multiple accepted features"):
            queue.select_integration("M0")

    def test_project_plan_routes_integration_to_integrator_policy(self):
        class Compatibility:
            def as_dict(self):
                return {
                    "compatible": True, "policy_role": "milestone-integrator",
                    "effective_model": "gpt-5.6-sol", "effective_reasoning": "high",
                }

        class Launcher:
            def __init__(self):
                self.actions = []

            def compatibility(self, action, project_id=None):
                self.actions.append((action, project_id))
                return Compatibility()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text())
            queue["features"][0].update({"status": "integration_pending", "integration_status": "pending"})
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: accepted feature awaits integration")
            project = replace(project, current_state="queue_reconciliation")
            launcher = Launcher()
            plan = CycleEngine(controller_configuration(root, project), launcher).project_plan(project)
            self.assertEqual(plan["proposed_next_action"], "milestone_integration")
            self.assertEqual(launcher.actions[-1], ("milestone_integration", "synthetic"))
            self.assertEqual(plan["compatibility_preflight"]["policy_role"], "milestone-integrator")


class IntegratorLeaseOrderingTests(unittest.TestCase):
    def setUp(self):
        self.module = load_integrationctl()
        self.root = Path(tempfile.mkdtemp())
        self.plan = {
            "plan_id": "plan", "feature_id": "F001", "milestone_id": "M0",
            "milestone_branch": "codex/m0", "accepted_commit": "a" * 40,
            "project_id": "synthetic", "run_id": "run-1",
            "planning_baseline_provenance": None,
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root)

    def test_lease_precedes_audit_runtime_and_git_mutation_and_releases_on_failure(self):
        events = []
        lease = {"agent_run": "owner", "purpose": "integration"}
        with (
            mock.patch.object(self.module, "discover", side_effect=lambda *args, **kwargs: (events.append("discover") or dict(self.plan))),
            mock.patch.object(self.module, "acquire_lease", side_effect=lambda *args, **kwargs: (events.append("acquire") or lease)),
            mock.patch.object(self.module, "audit_accepted_commit", side_effect=lambda *args, **kwargs: (events.append("audit") or {})),
            mock.patch.object(self.module, "write_runtime", side_effect=lambda *args, **kwargs: events.append("runtime")),
            mock.patch.object(self.module, "ensure_milestone_branch", side_effect=lambda *args, **kwargs: (_ for _ in ()).throw(self.module.IntegrationError("stop"))),
            mock.patch.object(self.module, "release_lease", side_effect=lambda *args, **kwargs: events.append("release")),
        ):
            with self.assertRaisesRegex(self.module.IntegrationError, "stop"):
                self.module.integrate_one(self.root, "F001", "M0", 10, False)
        self.assertLess(events.index("acquire"), events.index("audit"))
        self.assertLess(events.index("acquire"), events.index("runtime"))
        self.assertEqual(events[-1], "release")

    def test_concurrent_lease_refusal_occurs_before_audit_or_runtime(self):
        with (
            mock.patch.object(self.module, "discover", return_value=dict(self.plan)),
            mock.patch.object(self.module, "acquire_lease", side_effect=self.module.IntegrationError("writer lease already exists")),
            mock.patch.object(self.module, "audit_accepted_commit") as audit,
            mock.patch.object(self.module, "write_runtime") as runtime,
        ):
            with self.assertRaisesRegex(self.module.IntegrationError, "already exists"):
                self.module.integrate_one(self.root, "F001", "M0", 10, False)
        audit.assert_not_called()
        runtime.assert_not_called()

    def test_exact_duplicate_is_reconciled_without_cherry_pick(self):
        plan = dict(self.plan, action="reconcile_exact", equivalent_commit="a" * 40)
        lease = {"agent_run": "owner", "purpose": "integration"}
        git_calls = []

        def git_call(root, argv, **kwargs):
            git_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch.object(self.module, "discover", return_value=plan),
            mock.patch.object(self.module, "acquire_lease", return_value=lease),
            mock.patch.object(self.module, "audit_accepted_commit", return_value={}),
            mock.patch.object(self.module, "write_runtime"),
            mock.patch.object(self.module, "ensure_milestone_branch"),
            mock.patch.object(self.module, "heartbeat_lease"),
            mock.patch.object(self.module, "status_lines", return_value=[]),
            mock.patch.object(self.module, "git", side_effect=git_call),
            mock.patch.object(self.module, "record_integrating_state"),
            mock.patch.object(self.module, "finalize_integration", return_value={"validation": {"ok": True}}),
        ):
            result = self.module.integrate_one(self.root, "F001", "M0", 10, False)
        self.assertTrue(result["validation"]["ok"])
        self.assertFalse(any(call and call[0] == "cherry-pick" for call in git_calls))


if __name__ == "__main__":
    unittest.main()
