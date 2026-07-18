from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from development_conveyor.compatibility import (
    CompatibilityResult,
    ModelSelection,
    check_compatibility,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import ConveyorError, RecoveryError
from development_conveyor.repository import RepositoryInspector
from development_conveyor.retries import RetryBudget
from development_conveyor.sessions import (
    SessionPlan,
    SessionResult,
    classify_codex_failure,
    parse_retry_contract,
)
from tests.helpers import SyntheticLauncher, controller_configuration, git, synthetic_repository, write_json


MODEL = "gpt-5.6-sol"
REASONING = "high"


def selection(
    *,
    model: str = MODEL,
    reasoning: str = REASONING,
    minimum: str | None = None,
    valid: bool = True,
    error: str | None = None,
    manual: bool = False,
) -> ModelSelection:
    return ModelSelection(
        model,
        reasoning,
        "agent_file",
        "feature-factory-orchestrator",
        "/synthetic/MODEL_POLICY.md",
        minimum_cli_version=minimum,
        policy_valid=valid,
        policy_error=error,
        manual_reasoning_authorization=manual,
    )


def fake_codex(
    root: Path,
    *,
    version: str = "0.144.5",
    models: dict[str, list[str]] | None = None,
    catalog_error: str | None = None,
) -> Path:
    executable = root / "codex"
    catalog = {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
            }
            for model, efforts in (models or {MODEL: ["low", "medium", "high", "xhigh"]}).items()
        ]
    }
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"version = {version!r}\n"
        f"catalog = {catalog!r}\n"
        f"catalog_error = {catalog_error!r}\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli ' + version)\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:] == ['debug', 'models']:\n"
        "    if catalog_error:\n"
        "        print(catalog_error, file=sys.stderr)\n"
        "        raise SystemExit(2)\n"
        "    print(json.dumps(catalog))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def upgrade_stdout(session_id: str = "old-session") -> str:
    error = json.dumps({
        "type": "error",
        "status": 400,
        "error": {
            "type": "invalid_request_error",
            "message": "The 'gpt-5.6-sol' model requires a newer version of Codex. Please upgrade.",
        },
    })
    return "\n".join((
        json.dumps({"type": "thread.started", "thread_id": session_id}),
        json.dumps({"type": "turn.failed", "error": {"message": error}}),
    ))


class CompatibilityGateLauncher:
    def __init__(self, compatible: bool):
        self.launch_count = 0
        self.result = CompatibilityResult(
            "compatible" if compatible else "cli_version_too_old",
            "/synthetic/codex",
            "0.144.5" if compatible else "0.140.0",
            "0.144.5",
            MODEL,
            REASONING,
            "agent_file",
            "feature-factory-orchestrator",
            compatible,
            "synthetic compatibility result",
            "Upgrade Codex to 0.144.5 or newer.",
            "scripts/conveyor doctor --project synthetic",
        )

    def compatibility(self, action, *, project_id=None):
        return self.result

    def launch(self, request):
        self.launch_count += 1
        raise AssertionError("incompatible launch must not execute")


class UpgradeFailureLauncher:
    def __init__(self):
        self.actions: list[str] = []
        self.requests = []

    def compatibility(self, action, *, project_id=None):
        return CompatibilityResult(
            "compatible",
            "/synthetic/codex",
            "0.144.5",
            "0.144.5",
            MODEL,
            REASONING,
            "agent_file",
            "feature-factory-orchestrator",
            True,
            "synthetic compatibility result",
            "No remediation required.",
            f"scripts/conveyor doctor --project {project_id or 'synthetic'}",
        )

    def launch(self, request):
        self.actions.append(request.action)
        self.requests.append(request)
        plan = SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64,
            "workspace-write", MODEL, REASONING, "/synthetic/codex",
            {
                "classification": "compatible",
                "compatible": True,
                "detected_version": "0.144.5",
                "required_minimum_version": None,
                "remediation": "No remediation required.",
                "validation_command": "scripts/conveyor doctor --project synthetic",
            },
        )
        return SessionResult(
            request.action,
            1,
            "old-session",
            upgrade_stdout(),
            plan,
            redacted_stdout=upgrade_stdout(),
            redacted_stderr="optional MCP authentication warning",
            result_classification="cli_upgrade_required",
            exit_classification="cli_upgrade_required",
            failure_classification="cli_upgrade_required",
            retryable=False,
            primary_terminal_error="The model requires a newer version of Codex.",
            secondary_diagnostics=("optional_integration_authentication_required",),
        )


class IntegrationUpgradeFailureLauncher:
    def __init__(self):
        self.actions: list[str] = []

    @staticmethod
    def _plan(request):
        return SessionPlan(
            ("codex", "exec"), request.project.repository, "synthetic", "0" * 64,
            "workspace-write", MODEL, REASONING, "/synthetic/codex",
            {
                "classification": "compatible",
                "compatible": True,
                "detected_version": "0.144.5",
                "remediation": "Upgrade Codex.",
                "validation_command": "scripts/conveyor doctor --project synthetic",
            },
        )

    def launch(self, request):
        self.actions.append(request.action)
        plan = self._plan(request)
        if request.action == "feature_cycle":
            SyntheticLauncher()._accept_feature(request.project)
            return SessionResult(request.action, 0, "feature-session", "accepted", plan)
        return SessionResult(
            request.action,
            1,
            "integration-session",
            upgrade_stdout("integration-session"),
            plan,
            redacted_stdout=upgrade_stdout("integration-session"),
            result_classification="cli_upgrade_required",
            exit_classification="cli_upgrade_required",
            failure_classification="cli_upgrade_required",
            retryable=False,
            primary_terminal_error="The model requires a newer version of Codex.",
        )


class CompatibilityRepairTests(unittest.TestCase):
    def test_00_production_retry_contract_is_validated(self):
        contract = {
            "schema_version": 1,
            "retryable": True,
            "failure_classification": "validation_failure",
            "hypothesis": "generated interface is stale",
            "remediation_action": "regenerate the interface",
            "supporting_evidence": "compiler referenced the prior interface hash",
        }
        output = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "CONVEYOR_RETRY=" + json.dumps(contract)},
        })
        parsed, validation = parse_retry_contract(output)
        self.assertEqual(validation, "retry_contract_valid")
        self.assertEqual(parsed, contract)

    def test_00a_deterministic_failure_cannot_authorize_retry(self):
        contract = {
            "schema_version": 1,
            "retryable": True,
            "failure_classification": "cli_upgrade_required",
            "hypothesis": "retry the same incompatible CLI",
            "remediation_action": "launch the same command again",
            "supporting_evidence": "the model rejected the installed CLI",
        }
        output = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "CONVEYOR_RETRY=" + json.dumps(contract)},
        })
        parsed, validation = parse_retry_contract(output)
        self.assertIsNone(parsed)
        self.assertIn("not an allowed", validation)

    def test_01_compatible_cli_and_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary))
            result = check_compatibility(str(executable), selection())
            self.assertEqual(result.classification, "compatible")
            self.assertTrue(result.compatible)
            self.assertEqual(result.detected_version, "0.144.5")

    def test_02_missing_cli(self):
        result = check_compatibility("/definitely/missing/codex", selection())
        self.assertEqual(result.classification, "cli_missing")
        self.assertFalse(result.compatible)

    def test_03_cli_below_model_minimum(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary), version="0.143.0")
            result = check_compatibility(str(executable), selection(minimum="0.144.5"))
            self.assertEqual(result.classification, "cli_version_too_old")
            self.assertEqual(result.required_minimum_version, "0.144.5")

    def test_04_unsupported_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary))
            result = check_compatibility(str(executable), selection(model="gpt-unknown"))
            self.assertEqual(result.classification, "unsupported_model")

    def test_05_unsupported_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary), models={MODEL: ["low", "medium"]})
            result = check_compatibility(str(executable), selection())
            self.assertEqual(result.classification, "unsupported_reasoning_effort")

    def test_06_max_requires_cli_and_policy_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary), models={MODEL: ["high", "max"]})
            permitted = check_compatibility(str(executable), selection(reasoning="max", manual=True))
            denied = check_compatibility(
                str(executable),
                selection(reasoning="max", valid=False, error="max requires explicit authorization"),
            )
            self.assertEqual(permitted.classification, "compatible")
            self.assertEqual(denied.classification, "model_policy_invalid")

    def test_07_structured_model_upgrade_failure_is_non_retryable(self):
        result = classify_codex_failure(upgrade_stdout(), "")
        self.assertEqual(result["classification"], "cli_upgrade_required")
        self.assertFalse(result["retryable"])

    def test_08_optional_mcp_errors_remain_secondary(self):
        stderr = "Figma MCP AuthRequired\nOptional trading MCP AuthRequired\n"
        result = classify_codex_failure(upgrade_stdout(), stderr)
        self.assertEqual(result["classification"], "cli_upgrade_required")
        self.assertEqual(result["secondary_diagnostics"], ("optional_integration_authentication_required",))
        self.assertIn("requires a newer", result["primary_terminal_error"])

    def test_09_deterministic_incompatibility_receives_no_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = UpgradeFailureLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            with self.assertRaisesRegex(Exception, "cli_upgrade_required"):
                engine.run_project(project, "one_feature")
            self.assertEqual(launcher.actions, ["feature_cycle"])
            cycle = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(cycle["validation_attempts"], [])

    def test_10_repeated_identical_hypothesis_is_rejected(self):
        budget = RetryBudget(3)
        values = {
            "hypothesis": "the compiler cache is stale",
            "evidence": "cache mismatch",
            "failure_classification": "validation_failure",
            "command": ("codex", "exec"),
            "session_id": "session-1",
            "model": MODEL,
            "reasoning": REASONING,
            "remediation_action": "clear only the generated cache",
        }
        budget.record(**values)
        with self.assertRaisesRegex(ConveyorError, "repeat"):
            budget.record(**values)

    def test_11_timestamp_differences_do_not_create_hypothesis(self):
        budget = RetryBudget(3)
        base = {
            "evidence": "same compiler output",
            "failure_classification": "validation_failure",
            "command": ("codex", "exec"),
            "session_id": "session-1",
            "model": MODEL,
            "reasoning": REASONING,
            "remediation_action": "regenerate one interface",
        }
        budget.record(hypothesis="failed at 2026-07-17T10:00:00Z", **base)
        with self.assertRaisesRegex(ConveyorError, "repeat"):
            budget.record(hypothesis="failed at 2026-07-17T10:01:00Z", **base)

    def test_12_terminal_failure_exits_feature_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            engine = CycleEngine(controller_configuration(root, project), UpgradeFailureLauncher())
            with self.assertRaises(Exception):
                engine.run_project(project, "one_feature")
            state = engine.load_project_state(project)
            cycle = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(state["current_state"], "human_decision_required")
            self.assertEqual(cycle["current_phase"], "human_decision_required")

    def test_13_human_gate_contains_exact_remediation(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = fake_codex(Path(temporary), version="0.140.0")
            result = check_compatibility(
                str(executable), selection(minimum="0.144.5"), project_id="synthetic"
            )
            gate = result.human_gate("synthetic")
            self.assertIn("0.144.5", gate["remediation"])
            self.assertEqual(gate["compatibility_validation_command"], "scripts/conveyor doctor --project synthetic")

    def test_14_null_feature_starting_commit_refuses_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            engine = CycleEngine(controller_configuration(root, project), UpgradeFailureLauncher())
            inspector = RepositoryInspector(repository)
            queue = json.loads((repository / project.queue_location).read_text())
            state = engine._new_cycle_state(project, "run-1", inspector, queue["features"][0])
            state["feature_starting_commit"] = None
            with self.assertRaisesRegex(RecoveryError, "feature_starting_commit"):
                engine._validate_cycle_launch_invariants(project, inspector, state, "F001")

    def test_15_feature_start_resolves_to_milestone_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            path = repository / project.queue_location
            queue = json.loads(path.read_text())
            queue["features"][0]["id"] = "P0-001"
            queue["features"][0]["integration_base_commit"] = None
            write_json(path, queue)
            engine = CycleEngine(controller_configuration(root, project), UpgradeFailureLauncher())
            state = engine._new_cycle_state(
                project, "run-1", RepositoryInspector(repository), queue["features"][0]
            )
            self.assertEqual(state["feature_starting_commit"], git(repository, "rev-parse", "HEAD"))

    def test_16_cycle_checkpoint_precedes_session_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)

            class ObservingFailure(UpgradeFailureLauncher):
                checkpoint = None

                def launch(inner, request):
                    inner.checkpoint = json.loads(
                        (request.project.repository / ".factory/conveyor-state.json").read_text()
                    )
                    return super().launch(request)

            launcher = ObservingFailure()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            with self.assertRaises(Exception):
                engine.run_project(project, "one_feature")
            self.assertEqual(launcher.checkpoint["current_phase"], "branch_preparing")
            self.assertEqual(launcher.checkpoint["feature_starting_commit"], git(repository, "rev-parse", "HEAD"))

    def _historical_failure(self, root: Path):
        repository, project = synthetic_repository(root)
        configuration = controller_configuration(root, project)
        engine = CycleEngine(configuration, CompatibilityGateLauncher(True))
        inspector = RepositoryInspector(repository)
        inspector.ensure_runtime_ignored()
        queue = json.loads((repository / project.queue_location).read_text())
        cycle = engine._new_cycle_state(project, "old-run", inspector, queue["features"][0])
        cycle.update({
            "current_phase": "failed",
            "last_successful_checkpoint": "session_failed",
            "feature_starting_commit": None,
            "session_id": "old-session",
            "stop_reason": "repository-scoped feature session returned non-zero",
            "validation_attempts": [{"attempt": 1}, {"attempt": 2}, {"attempt": 3}],
        })
        engine.cycle_store.write(inspector.cycle_state_path(), cycle)
        project_state = engine._project_document(project, "old-run", inspector.identity()["path_fingerprint"])
        project_state.update({
            "current_state": "feature_running",
            "current_feature": "F001",
            "last_checkpoint": "feature_session_launch",
        })
        engine.project_store.write(engine.project_state_path(project), project_state)
        report_path = configuration.root / "reports/old-run/feature_cycle.json"
        write_json(report_path, {
            "redacted_stdout": upgrade_stdout(),
            "redacted_stderr": "optional MCP AuthRequired",
            "session_id": "old-session",
            "argv": ["codex", "exec", "resume", "old-session"],
        })
        return repository, project, engine, report_path

    def test_17_verified_repair_uses_new_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _ = self._historical_failure(Path(temporary))
            plan = engine.run_project(project, "resume", dry_run=True)
            self.assertTrue(plan["new_session_would_launch"])

    def test_18_old_failed_session_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, _ = self._historical_failure(Path(temporary))
            plan = engine.run_project(project, "resume", dry_run=True)
            self.assertFalse(plan["old_session_will_resume"])
            self.assertNotIn("resume persisted", " ".join(plan["sessions_that_would_launch"]))

    def test_19_historical_reports_remain_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, engine, report = self._historical_failure(Path(temporary))
            before = report.read_bytes()
            result = engine.run_project(project, "resume")
            self.assertEqual(result["outcome"], "state_repaired")
            self.assertEqual(report.read_bytes(), before)

    def test_20_dry_run_writes_no_repository_or_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, engine, _ = self._historical_failure(Path(temporary))
            before = git(repository, "status", "--porcelain=v1", "--branch")
            plan = engine.run_project(project, "resume", dry_run=True)
            after = git(repository, "status", "--porcelain=v1", "--branch")
            self.assertEqual(before, after)
            self.assertFalse(plan["dry_run_writes_application_repository"])
            self.assertFalse((engine.root / "state/launch-locks").exists())

    def test_21_real_launch_is_blocked_until_preflight_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = CompatibilityGateLauncher(False)
            engine = CycleEngine(controller_configuration(root, project), launcher)
            result = engine.run_project(project, "one_feature")
            self.assertEqual(result["outcome"], "human_decision_required")
            self.assertEqual(launcher.launch_count, 0)
            self.assertFalse((repository / ".factory/conveyor-state.json").exists())

    def test_22_integration_failure_uses_separate_session_and_precise_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            launcher = IntegrationUpgradeFailureLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            with self.assertRaisesRegex(Exception, "cli_upgrade_required"):
                engine.run_project(project, "one_feature")
            cycle = engine.cycle_store.read(RepositoryInspector(repository).cycle_state_path())
            self.assertEqual(cycle["feature_session_id"], "feature-session")
            self.assertEqual(cycle["integration_session_id"], "integration-session")
            self.assertEqual(cycle["current_phase"], "human_decision_required")
            self.assertEqual(cycle["failure_classification"], "cli_upgrade_required")
            before = list(launcher.actions)
            resumed = engine.resume_project(project)
            self.assertEqual(resumed["outcome"], "human_decision_required")
            self.assertEqual(launcher.actions, before)

    def test_23_superseded_cycle_is_archived_exactly_before_new_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, engine, _ = self._historical_failure(root)
            cycle_path = repository / ".factory/conveyor-state.json"
            historical_bytes = cycle_path.read_bytes()
            self.assertEqual(engine.run_project(project, "resume")["outcome"], "state_repaired")
            engine.launcher = UpgradeFailureLauncher()
            with self.assertRaises(Exception):
                engine.run_project(project, "one_feature")
            archive = engine.root / "reports/old-run/cycle-state-snapshot.json"
            metadata = json.loads(
                (engine.root / "reports/old-run/cycle-state-snapshot.meta.json").read_text()
            )
            self.assertEqual(archive.read_bytes(), historical_bytes)
            self.assertEqual(metadata["sha256"], hashlib.sha256(historical_bytes).hexdigest())
            new_cycle = engine.cycle_store.read(cycle_path)
            self.assertEqual(new_cycle["supersedes_run_id"], "old-run")
            self.assertEqual(new_cycle["supersedes_session_id"], "old-session")

    def test_24_remediated_integration_uses_fresh_action_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text())
            queue["features"][0].update({
                "status": "accepted",
                "branch": "codex/f001-synthetic-feature",
                "integration_base_commit": git(repository, "rev-parse", "HEAD"),
            })
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: record accepted feature evidence")

            launcher = UpgradeFailureLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            state = engine._new_cycle_state(
                project, "integration-run", inspector, queue["features"][0]
            )
            state.update({
                "current_phase": "human_decision_required",
                "last_successful_checkpoint": "integration_session_terminal_failure",
                "integration_session_id": "failed-integration-session",
                "failure_classification": "cli_upgrade_required",
                "retry_exhausted": True,
                "environment_remediation_verified": False,
                "human_decision_required": {"classification": "cli_upgrade_required", "resolved": False},
            })
            engine.cycle_store.write(inspector.cycle_state_path(), state)
            project_state = engine._project_document(
                project, "integration-run", inspector.identity()["path_fingerprint"]
            )
            project_state.update({
                "current_state": "human_decision_required",
                "current_feature": "F001",
                "last_checkpoint": "integration_session_terminal_failure",
                "human_decision_required": {"classification": "cli_upgrade_required", "resolved": False},
            })
            engine.project_store.write(engine.project_state_path(project), project_state)

            with self.assertRaisesRegex(Exception, "cli_upgrade_required"):
                engine.resume_project(project)
            self.assertEqual(launcher.actions, ["milestone_integration"])
            self.assertIsNone(launcher.requests[0].session_id)
            refreshed = engine.cycle_store.read(inspector.cycle_state_path())
            self.assertEqual(refreshed["integration_session_id"], "old-session")
            self.assertEqual(refreshed["current_phase"], "human_decision_required")

    def test_25_remediated_milestone_gate_uses_fresh_action_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text())
            queue["features"][0]["status"] = "integrated"
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: complete synthetic milestone")

            launcher = UpgradeFailureLauncher()
            engine = CycleEngine(controller_configuration(root, project), launcher)
            inspector = RepositoryInspector(repository)
            inspector.ensure_runtime_ignored()
            state = engine._new_cycle_state(project, "gate-run", inspector, None)
            state.update({
                "current_phase": "human_decision_required",
                "last_successful_checkpoint": "milestone_gate_session_terminal_failure",
                "integration_session_id": "completed-integration-session",
                "milestone_gate_session_id": "failed-gate-session",
                "session_id": "failed-gate-session",
                "failure_classification": "cli_upgrade_required",
                "retry_exhausted": True,
                "environment_remediation_verified": False,
                "human_decision_required": {"classification": "cli_upgrade_required", "resolved": False},
            })
            engine.cycle_store.write(inspector.cycle_state_path(), state)
            project_state = engine._project_document(
                project, "gate-run", inspector.identity()["path_fingerprint"]
            )
            project_state.update({
                "current_state": "human_decision_required",
                "last_checkpoint": "milestone_gate_session_terminal_failure",
                "human_decision_required": {"classification": "cli_upgrade_required", "resolved": False},
            })
            engine.project_store.write(engine.project_state_path(project), project_state)

            with self.assertRaisesRegex(Exception, "cli_upgrade_required"):
                engine.resume_project(project)
            self.assertEqual(launcher.actions, ["milestone_gate"])
            self.assertIsNone(launcher.requests[0].session_id)
            refreshed = engine.cycle_store.read(inspector.cycle_state_path())
            self.assertEqual(refreshed["milestone_gate_session_id"], "old-session")
            self.assertEqual(refreshed["current_phase"], "human_decision_required")


if __name__ == "__main__":
    unittest.main()
