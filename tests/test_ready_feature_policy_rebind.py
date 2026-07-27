from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from development_conveyor.autopilot import AutopilotPaths
from development_conveyor.compatibility import CompatibilityResult
from development_conveyor.contracts import WorkflowType
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor.errors import RecoveryError
from development_conveyor.execution_profiles import (
    DEFAULT_PROFILES,
    DEFAULT_WORKFLOW_FALLBACKS,
    resolve_execution_profile,
)
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.logging import utc_now
from development_conveyor.policy_rebind import ReadyFeaturePolicyRebinder
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector

from tests.helpers import (
    controller_configuration,
    git,
    synthetic_repository,
    write_json,
)


OLD_POLICY = {
    "profile": "generic_or_architectural",
    "parent_sessions": 1,
    "child_sessions": 0,
}
NEW_PROFILE = "application_feature_implementation"
EXPECTED_PATHS = (
    "docs/FEATURE_QUEUE.yaml",
    "docs/features/F001.md",
)


class CompatibilityLauncher:
    def __init__(self, compatible: bool = True):
        self.compatible = compatible
        self.plan_calls = 0
        self.launch_calls = 0

    def compatibility(
        self,
        action,
        *,
        project_id=None,
        planned_model=None,
        planned_reasoning=None,
        model_plan_source=None,
    ):
        classification = "compatible" if self.compatible else "unsupported_model"
        return CompatibilityResult(
            classification=classification,
            executable="/synthetic/codex",
            detected_version="0.145.0",
            required_minimum_version=None,
            effective_model=planned_model,
            effective_reasoning=planned_reasoning,
            policy_source=model_plan_source or "selected_feature_profile",
            policy_role="direct-feature-session",
            compatible=self.compatible,
            diagnostic=(
                "exact Codex executable, model, and reasoning policy are compatible"
                if self.compatible
                else "installed Codex catalog does not expose configured model gpt-5.3-codex"
            ),
            remediation=(
                "No remediation required."
                if self.compatible
                else "Upgrade Codex until gpt-5.3-codex is supported."
            ),
            validation_command="scripts/conveyor doctor --project synthetic",
        )

    def plan(self, request):
        self.plan_calls += 1
        raise AssertionError("policy rebind must not plan a model session")

    def launch(self, request, **kwargs):
        self.launch_calls += 1
        raise AssertionError("policy rebind must not launch a model session")


def rebind_fixture(root: Path, *, compatible: bool = True):
    repository, project = synthetic_repository(root)
    queue_path = repository / "docs/FEATURE_QUEUE.yaml"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["features"][0]["execution_policy"] = dict(OLD_POLICY)
    write_json(queue_path, queue)
    (repository / "docs/features/F001.md").write_text(
        "# F001\n\n"
        "- Factory status: Ready\n\n"
        "## Acceptance criteria\n\n"
        "- Add a synthetic feature.\n\n"
        "## Execution policy\n\n"
        "```yaml\n"
        "execution_policy:\n"
        "  profile: generic_or_architectural\n"
        "  parent_sessions: 1\n"
        "  child_sessions: 0\n"
        "```\n",
        encoding="utf-8",
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "bind old policy")
    head = git(repository, "rev-parse", "HEAD")
    project = replace(project, validated_baseline_commit=head)
    configuration = replace(
        controller_configuration(root, project),
        execution_profiles={
            "profiles": DEFAULT_PROFILES,
            "workflow_fallbacks": DEFAULT_WORKFLOW_FALLBACKS,
            "feature_policies": [],
        },
    )
    launcher = CompatibilityLauncher(compatible)
    engine = CycleEngine(configuration, launcher)
    inspector = RepositoryInspector(repository)
    identity = inspector.identity()
    state_root = configuration.owned_path(
        configuration.conveyor["state_directory"]
    ) / "projects" / project.project_id
    ledger = EvidenceLedger(
        state_root / "evidence-ledger.jsonl",
        project_id=project.project_id,
        repository_identity=identity["repository_id"],
        repository_path_fingerprint=identity["path_fingerprint"],
    )
    transaction_id = "synthetic-ready-policy"
    ledger.append(
        event_type="TransactionStarted",
        transaction_id=transaction_id,
        workflow_type=WorkflowType.QUEUE_RECONCILIATION,
        payload={"run_id": "synthetic-ready", "milestone": "M0", "feature_id": None},
    )
    ledger.append(
        event_type="TransactionCompleted",
        transaction_id=transaction_id,
        workflow_type=WorkflowType.QUEUE_RECONCILIATION,
        payload={
            "classification": "RECONCILED_READY_WORK",
            "next_state": "feature_ready",
            "feature_id": None,
            "selected_feature": "F001",
            "legacy_import": True,
        },
    )
    ledger.append(
        event_type="ProjectionUpdated",
        transaction_id=transaction_id,
        workflow_type=WorkflowType.QUEUE_RECONCILIATION,
        payload={
            "current_state": "feature_ready",
            "current_feature": None,
            "selected_feature": "F001",
        },
    )
    projection = ProjectionEngine(
        ledger, state_root / "projection-cache.json"
    )
    projection.rebuild(persist_cache=True)
    exclude = repository / ".git/info/exclude"
    exclude.write_text(
        ".factory/conveyor-state.json\n.factory/locks/writer.json\n",
        encoding="utf-8",
    )
    cycle = engine._new_cycle_state(
        project,
        "synthetic-ready",
        inspector,
        queue["features"][0],
        compatibility=launcher.compatibility(
            "feature_cycle",
            project_id=project.project_id,
            planned_model="gpt-5.6-sol",
            planned_reasoning="medium",
            model_plan_source="selected_feature_profile",
        ).as_dict(),
    )
    cycle.update(
        {
            "current_phase": "feature_ready",
            "current_feature": "F001",
            "selected_feature": "F001",
            "last_successful_checkpoint": "synthetic_ready",
            "last_verified_git_state": {
                "branch": "codex/m0-foundation",
                "head": head,
                "clean": True,
                "git_operations": {
                    "cherry_pick": False,
                    "merge": False,
                    "rebase_apply": False,
                    "rebase_merge": False,
                },
            },
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    write_json(repository / ".factory/conveyor-state.json", cycle)
    rebinder = ReadyFeaturePolicyRebinder(configuration, launcher, engine)
    return repository, project, configuration, launcher, engine, ledger, rebinder, head


def inspect(rebinder, project, head, **overrides):
    values = {
        "project": project,
        "feature_id": "F001",
        "expected_branch": "codex/m0-foundation",
        "expected_head": head,
        "expected_old_policy": OLD_POLICY,
        "target_profile": NEW_PROFILE,
        "expected_paths": EXPECTED_PATHS,
    }
    values.update(overrides)
    return rebinder.inspect(**values)


class ReadyFeaturePolicyRebindTests(unittest.TestCase):
    def test_profile_routes_only_application_implementation_to_exact_codex_model(self):
        feature = {"id": "F001", "execution_policy": {
            "profile": NEW_PROFILE,
            "parent_sessions": 1,
            "child_sessions": 0,
        }}
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
            feature=feature,
        )
        self.assertEqual(
            (
                resolved.profile,
                resolved.model,
                resolved.reasoning,
                resolved.parent_sessions,
                resolved.child_sessions,
            ),
            (NEW_PROFILE, "gpt-5.6-terra", "medium", 1, 0),
        )
        deterministic = resolve_execution_profile(
            workflow="application_feature",
            deterministic=True,
            feature=feature,
        )
        self.assertEqual(
            (deterministic.model, deterministic.reasoning, deterministic.parent_sessions),
            (None, None, 0),
        )
        for workflow, expected in (
            ("queue_reconciliation", ("gpt-5.6-luna", "high")),
            ("controller_repair", ("gpt-5.6-sol", "medium")),
        ):
            with self.subTest(workflow=workflow):
                fallback = resolve_execution_profile(
                    workflow=workflow, deterministic=False
                )
                self.assertEqual(
                    (fallback.model, fallback.reasoning), expected
                )

    def test_explicit_existing_policy_is_preserved_without_rebind(self):
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
            feature={"id": "F001", "execution_policy": OLD_POLICY},
        )
        self.assertEqual(
            (
                resolved.profile,
                resolved.model,
                resolved.reasoning,
                resolved.resolution_source,
            ),
            (
                "generic_or_architectural",
                "gpt-5.6-sol",
                "medium",
                "selected_feature_profile",
            ),
        )

    def test_dry_run_is_write_free_and_reports_exact_policy_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, launcher, _, ledger, rebinder, head = (
                rebind_fixture(Path(temporary))
            )
            before = {
                "head": head,
                "queue": (repository / EXPECTED_PATHS[0]).read_bytes(),
                "spec": (repository / EXPECTED_PATHS[1]).read_bytes(),
                "ledger": ledger.path.read_bytes(),
                "cycle": (repository / ".factory/conveyor-state.json").read_bytes(),
            }
            request = inspect(rebinder, project, head)
            plan = request.public_plan(dry_run=True)
            self.assertEqual(plan["classification"], "POLICY_REBIND_READY")
            self.assertEqual(plan["normalization_paths"], list(EXPECTED_PATHS))
            self.assertEqual(plan["new_stored_policy"]["profile"], NEW_PROFILE)
            self.assertEqual(
                (
                    plan["resolved_execution_profile"]["model"],
                    plan["resolved_execution_profile"]["reasoning"],
                ),
                ("gpt-5.6-terra", "medium"),
            )
            self.assertRegex(plan["predicted_final_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                (
                    plan["model_sessions_that_would_launch"],
                    plan["child_sessions_that_would_launch"],
                    plan["leases_that_would_be_acquired"],
                    plan["transactions_that_would_start"],
                ),
                (0, 0, 0, 0),
            )
            self.assertFalse(plan["feature_preparation_will_run"])
            self.assertFalse(plan["feature_execution_will_run"])
            self.assertEqual(git(repository, "rev-parse", "HEAD"), before["head"])
            self.assertEqual((repository / EXPECTED_PATHS[0]).read_bytes(), before["queue"])
            self.assertEqual((repository / EXPECTED_PATHS[1]).read_bytes(), before["spec"])
            self.assertEqual(ledger.path.read_bytes(), before["ledger"])
            self.assertEqual(
                (repository / ".factory/conveyor-state.json").read_bytes(),
                before["cycle"],
            )
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertEqual((launcher.plan_calls, launcher.launch_calls), (0, 0))

    def test_local_compatibility_failure_rejects_apply_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, launcher, _, _, rebinder, head = (
                rebind_fixture(Path(temporary), compatible=False)
            )
            request = inspect(rebinder, project, head)
            plan = request.public_plan(dry_run=True)
            self.assertEqual(plan["classification"], "POLICY_REBIND_REJECTED")
            self.assertFalse(plan["apply_allowed"])
            with self.assertRaisesRegex(RecoveryError, "not locally compatible"):
                rebinder.apply(request)
            self.assertEqual(git(repository, "rev-parse", "HEAD"), head)
            self.assertTrue(RepositoryInspector(repository).is_clean)
            self.assertEqual((launcher.plan_calls, launcher.launch_calls), (0, 0))

    def test_wrong_identity_policy_or_path_is_rejected(self):
        cases = {
            "feature": {"feature_id": "F999"},
            "branch": {"expected_branch": "codex/wrong"},
            "head": {"expected_head": "0" * 40},
            "old_policy": {"expected_old_policy": {
                "profile": "multi_module_precise",
                "parent_sessions": 1,
                "child_sessions": 0,
            }},
            "path": {"expected_paths": (
                "docs/FEATURE_QUEUE.yaml",
                "Sources/Unauthorized.swift",
            )},
        }
        for name, override in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                _, project, _, _, _, _, rebinder, head = rebind_fixture(
                    Path(temporary)
                )
                with self.assertRaises(RecoveryError):
                    inspect(rebinder, project, head, **override)

    def test_state_ownership_and_repository_gates_reject(self):
        mutations = ("dirty", "lease", "session", "ownership", "git_operation")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository, project, configuration, _, _, _, rebinder, head = (
                    rebind_fixture(Path(temporary))
                )
                if mutation == "dirty":
                    (repository / "app.txt").write_text("dirty\n", encoding="utf-8")
                elif mutation == "lease":
                    write_json(repository / ".factory/locks/writer.json", {})
                elif mutation == "session":
                    cycle_path = repository / ".factory/conveyor-state.json"
                    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
                    cycle["session_id"] = "session-active"
                    write_json(cycle_path, cycle)
                elif mutation == "ownership":
                    write_json(
                        AutopilotPaths.for_project(
                            configuration, project.project_id
                        ).ownership,
                        {"project_id": project.project_id},
                    )
                else:
                    git_dir = Path(git(repository, "rev-parse", "--git-dir"))
                    (repository / git_dir / "MERGE_HEAD").write_text(
                        head + "\n", encoding="ascii"
                    )
                with self.assertRaises(RecoveryError):
                    inspect(rebinder, project, head)

    def test_active_transaction_and_wrong_projection_state_reject(self):
        for state in ("active_transaction", "wrong_state"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                _, project, _, _, _, ledger, rebinder, head = rebind_fixture(
                    Path(temporary)
                )
                if state == "active_transaction":
                    ledger.append(
                        event_type="TransactionStarted",
                        transaction_id="active-policy-rebind-conflict",
                        workflow_type=WorkflowType.FEATURE_EXECUTION,
                        payload={
                            "run_id": "active-conflict",
                            "milestone": "M0",
                            "feature_id": "F001",
                        },
                    )
                else:
                    ledger.append(
                        event_type="ProjectionUpdated",
                        transaction_id="synthetic-ready-policy",
                        workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                        payload={
                            "current_state": "queue_reconciliation",
                            "current_feature": None,
                            "selected_feature": "F001",
                        },
                    )
                with self.assertRaises(RecoveryError):
                    inspect(rebinder, project, head)

    def test_apply_creates_one_metadata_commit_and_stops_feature_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, launcher, engine, _, rebinder, head = (
                rebind_fixture(Path(temporary))
            )
            request = inspect(rebinder, project, head)
            with patch.object(
                engine,
                "_reconcile_projection_compatibility_cache",
                wraps=engine._reconcile_projection_compatibility_cache,
            ) as cache_refresh:
                result = rebinder.apply(request, run_id="synthetic-policy-rebind")
            commit = result["planning_result_commit"]
            self.assertEqual(git(repository, "rev-parse", f"{commit}^"), head)
            self.assertEqual(
                set(git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()),
                set(EXPECTED_PATHS),
            )
            self.assertEqual(git(repository, "rev-list", "--count", f"{head}..{commit}"), "1")
            self.assertEqual(result["final_state"], "feature_ready")
            self.assertEqual(result["selected_feature"], "F001")
            self.assertEqual(
                (result["model_sessions_launched"], result["child_sessions_launched"]),
                (0, 0),
            )
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            self.assertTrue(RepositoryInspector(repository).is_clean)
            queue = json.loads(
                (repository / "docs/FEATURE_QUEUE.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                queue["features"][0]["execution_policy"]["profile"], NEW_PROFILE
            )
            self.assertIn(
                "profile: application_feature_implementation",
                (repository / "docs/features/F001.md").read_text(encoding="utf-8"),
            )
            self.assertEqual((launcher.plan_calls, launcher.launch_calls), (0, 0))
            cache_refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
