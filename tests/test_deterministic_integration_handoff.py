from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from development_conveyor import cycle_engine as cycle_engine_module
from development_conveyor import integration_executor as executor_module
from development_conveyor.errors import IntegrationPlanError, RepositoryError, SafetyViolation
from development_conveyor.integration_executor import (
    LEASE_IDENTITY_FIELDS,
    PLAN_REQUIRED_FIELDS,
    RUNTIME_DESCENDANTS,
    RUNTIME_PATTERN,
    load_integration_plan,
    persist_integration_plan,
    validate_plan_document,
)
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import git, synthetic_repository, write_json
from tests.test_two_ref_integration_recovery import TwoRefIntegrationRecoveryTests

_fixture_builder = TwoRefIntegrationRecoveryTests(methodName="runTest")
del TwoRefIntegrationRecoveryTests


class DeterministicIntegrationHandoffTests(unittest.TestCase):
    def _fixture(self, root: Path, **kwargs):
        return _fixture_builder._fixture(root, **kwargs)

    @staticmethod
    def _git_identity(repository: Path) -> tuple[str, str, str]:
        return (
            git(repository, "rev-parse", "HEAD"),
            git(repository, "rev-parse", "HEAD^{tree}"),
            git(repository, "status", "--porcelain=v1", "-uall"),
        )

    def _transition_fixture(
        self,
        root: Path,
        *,
        status: str = "review",
        queued_commit: str | None = None,
        queued_branch: str | None = None,
    ):
        repository, project = synthetic_repository(root, feature_status=status)
        queue_path = repository / project.queue_location
        if queued_commit is not None or queued_branch is not None:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0]["accepted_commit"] = queued_commit
            queue["features"][0]["branch"] = queued_branch
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "test: conflicting queue projection")
        milestone_base = git(repository, "rev-parse", "HEAD")
        feature_branch = "codex/f001-ledger-accepted"
        git(repository, "switch", "-c", feature_branch)
        (repository / "app.txt").write_text(
            "baseline\nledger accepted\n", encoding="utf-8"
        )
        git(repository, "add", "app.txt")
        git(repository, "commit", "-m", "F001: ledger accepted")
        accepted_commit = git(repository, "rev-parse", "HEAD")
        accepted_tree = git(repository, "rev-parse", "HEAD^{tree}")
        git(repository, "switch", project.milestone_branch)
        plan = {
            "feature_id": "F001",
            "milestone_id": "M0",
            "milestone_branch": project.milestone_branch,
            "queue_path": project.queue_location,
            "metadata_paths": [
                "docs/CURRENT_STATUS.md",
                project.queue_location,
                "docs/RUN_LOG.md",
            ],
            "accepted_metadata_ref": (
                "ledger:acceptance-transaction:" + "a" * 64
            ),
            "feature_branch": feature_branch,
            "pre_integration_head": milestone_base,
            "accepted_commit": accepted_commit,
            "created_at": "2026-07-29T21:00:00Z",
        }
        authority = {
            "completed_acceptance_transaction": True,
            "transaction_id": "acceptance-transaction",
            "metadata_fingerprint": "a" * 64,
            "feature_id": "F001",
            "milestone_base": milestone_base,
            "implementation_commit": accepted_commit,
            "implementation_tree": accepted_tree,
            "feature_branch": feature_branch,
            "acceptance_tier": "feature",
            "tier_evidence_fingerprint": "b" * 64,
            "implementation_ref_unchanged": True,
            "integration_target_preflight_passed": True,
        }
        return repository, project, plan, authority

    def test_ledger_backed_acceptance_transitions_review_to_integrating(self):
        for status in ("review", "accepted", "integration_pending", "integrating"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                repository, project, plan, authority = self._transition_fixture(
                    Path(temporary), status=status
                )

                executor_module._mark_integrating(
                    repository,
                    plan,
                    ledger_transition_authority=authority,
                )

                queue = json.loads(
                    (repository / project.queue_location).read_text(encoding="utf-8")
                )
                feature = queue["features"][0]
                self.assertEqual("integrating", feature["status"])
                self.assertEqual(plan["accepted_commit"], feature["accepted_commit"])
                self.assertEqual(plan["feature_branch"], feature["branch"])

    def test_legacy_queue_backed_plan_still_rejects_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, plan, _ = self._transition_fixture(
                Path(temporary), status="review"
            )

            with self.assertRaisesRegex(
                IntegrationPlanError,
                "accepted feature cannot transition to integrating",
            ):
                executor_module._mark_integrating(repository, plan)

            queue = json.loads(
                (repository / project.queue_location).read_text(encoding="utf-8")
            )
            self.assertEqual("review", queue["features"][0]["status"])

    def test_ledger_transition_mismatched_authority_fails_closed(self):
        cases = {
            "transaction_id": "different-transaction",
            "implementation_commit": "c" * 40,
            "implementation_tree": "d" * 40,
        }
        for field, value in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                repository, _, plan, authority = self._transition_fixture(
                    Path(temporary), status="review"
                )
                authority[field] = value

                with self.assertRaisesRegex(
                    IntegrationPlanError,
                    "ledger integration transition authority failed",
                ):
                    executor_module._mark_integrating(
                        repository,
                        plan,
                        ledger_transition_authority=authority,
                    )

    def test_ledger_transition_terminal_or_conflicting_queue_state_is_rejected(self):
        cases = [
            {"status": status}
            for status in ("integrated", "completed", "cancelled", "rejected", "blocked")
        ]
        cases.extend(
            [
                {"status": "review", "queued_commit": "e" * 40},
                {"status": "review", "queued_branch": "codex/different-feature"},
            ]
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                repository, _, plan, authority = self._transition_fixture(
                    Path(temporary), **case
                )

                with self.assertRaises(IntegrationPlanError):
                    executor_module._mark_integrating(
                        repository,
                        plan,
                        ledger_transition_authority=authority,
                    )

    def test_exact_plan_bypasses_live_ready_queue_and_adopts_one_controller_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository, project, _, engine, launcher, _, milestone_start,
                feature_branch, accepted, _,
            ) = self._fixture(Path(temporary))
            captured: dict[str, object] = {}
            real_execute = cycle_engine_module.execute_integration_plan
            real_acquire = WorkflowWriterLease.acquire
            real_release = WorkflowWriterLease.release
            real_adopt = WorkflowWriterLease.revalidate_adopted
            calls = {"acquire": 0, "release": 0, "adopt": 0}

            def execute_spy(path: Path):
                captured["path"] = Path(path)
                captured["plan"] = load_integration_plan(Path(path))
                return real_execute(Path(path))

            def acquire_spy(lease, *args, **kwargs):
                calls["acquire"] += 1
                return real_acquire(lease, *args, **kwargs)

            def release_spy(lease, *args, **kwargs):
                calls["release"] += 1
                return real_release(lease, *args, **kwargs)

            def adopt_spy(lease, *args, **kwargs):
                calls["adopt"] += 1
                return real_adopt(lease, *args, **kwargs)

            with mock.patch.object(
                cycle_engine_module, "execute_integration_plan", side_effect=execute_spy
            ), mock.patch.object(
                WorkflowWriterLease, "acquire", new=acquire_spy
            ), mock.patch.object(
                WorkflowWriterLease, "release", new=release_spy
            ), mock.patch.object(
                WorkflowWriterLease, "revalidate_adopted", new=adopt_spy
            ):
                result = engine.run_project(project, "milestone")

            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual([], launcher.requests)
            self.assertEqual({"acquire": 1, "release": 1}, {
                "acquire": calls["acquire"], "release": calls["release"]
            })
            self.assertGreaterEqual(calls["adopt"], 3)
            plan = captured["plan"]
            self.assertEqual(PLAN_REQUIRED_FIELDS, set(plan))
            self.assertEqual(accepted, plan["accepted_metadata_ref"])
            self.assertEqual(feature_branch, plan["lease_identity"]["feature_branch"])
            self.assertEqual(accepted, plan["lease_identity"]["accepted_commit"])
            self.assertEqual(set(LEASE_IDENTITY_FIELDS), set(plan["lease_identity"]))
            self.assertEqual(milestone_start, plan["pre_integration_head"])
            self.assertFalse(Path(captured["path"]).is_relative_to(repository))
            cache = json.loads(
                RepositoryInspector(repository).cycle_state_path().read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result["kernel_projection"]["ledger_sequence"], cache["kernel_ledger_sequence"])
            self.assertEqual(
                result["kernel_projection"]["projection_fingerprint"],
                cache["kernel_projection_fingerprint"],
            )
            self.assertTrue(result["cycle_cache_finalized"])
            self.assertEqual([], launcher.requests)

    def test_cache_finalization_failure_preserves_integration_and_repairs_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                engine,
                launcher,
                ledger,
                _,
                _,
                accepted,
                _,
            ) = self._fixture(Path(temporary))
            with mock.patch.object(
                engine,
                "_finalize_completed_integration_cycle_cache",
                side_effect=OSError("simulated cache finalization failure"),
            ):
                result = engine.run_project(project, "milestone")

            self.assertEqual(
                "RECOVERABLE_DERIVED_CACHE_FINALIZATION_FAILURE",
                result["classification"],
            )
            self.assertTrue(result["integration_preserved"])
            self.assertTrue(result["integration_will_not_repeat"])
            self.assertTrue(result["writer_lease_released"])
            self.assertIsNone(result["human_gate"])
            self.assertEqual(
                (
                    "scripts/conveyor repair-cycle-cache "
                    f"--project {project.project_id} --apply"
                ),
                result["repair_command"],
            )
            terminal_head = git(repository, "rev-parse", "HEAD")
            terminal_branch = git(repository, "branch", "--show-current")
            self.assertEqual(project.milestone_branch, terminal_branch)
            self.assertEqual(
                terminal_head,
                result["kernel_projection"]["historical_integration_outcomes"][
                    -1
                ]["terminal_head"],
            )
            terminal = ledger.terminal_event(result["integration_transaction"])
            self.assertEqual("TransactionCompleted", terminal["event_type"])
            self.assertEqual("INTEGRATED", terminal["payload"]["classification"])
            self.assertEqual(accepted, terminal["payload"]["accepted_feature_commit"])
            self.assertEqual([], launcher.requests)

    def test_distinct_controller_and_adapter_ids_are_serialized_and_lease_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                _, project, _, engine, launcher, _, _, _, _, _,
            ) = self._fixture(
                Path(temporary),
                controller_project_id="interview-companion",
                adapter_project_id="live-interview-companion",
            )
            captured: dict[str, object] = {}
            real_execute = cycle_engine_module.execute_integration_plan

            def execute_spy(path: Path):
                captured["plan"] = load_integration_plan(Path(path))
                return real_execute(Path(path))

            with mock.patch.object(
                cycle_engine_module, "execute_integration_plan", side_effect=execute_spy
            ):
                result = engine.run_project(project, "milestone")

            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual([], launcher.requests)
            plan = captured["plan"]
            self.assertEqual(2, plan["schema_version"])
            self.assertEqual("interview-companion", plan["controller_project_id"])
            self.assertEqual("live-interview-companion", plan["adapter_project_id"])
            self.assertEqual("interview-companion", plan["project_id"])
            self.assertEqual(
                "interview-companion",
                plan["lease_identity"]["controller_project_id"],
            )
            self.assertEqual(
                "live-interview-companion",
                plan["lease_identity"]["adapter_project_id"],
            )

    def test_prevalidation_failure_recovers_existing_two_commit_topology_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                engine,
                launcher,
                ledger,
                milestone_start,
                _,
                accepted,
                _,
            ) = self._fixture(
                Path(temporary),
                controller_project_id="interview-companion",
                adapter_project_id="live-interview-companion",
            )
            with mock.patch.object(
                executor_module,
                "_run_validation",
                side_effect=SafetyViolation("synthetic pre-validation classification"),
            ):
                with self.assertRaises(SafetyViolation):
                    engine.run_project(project, "milestone")

            integrating_commit = git(repository, "rev-parse", "HEAD")
            resulting_feature_commit = git(repository, "rev-parse", "HEAD^")
            self.assertEqual(milestone_start, git(repository, "rev-parse", "HEAD^^"))
            self.assertEqual(
                executor_module.RepositoryInspector(repository).patch_fingerprint(accepted),
                executor_module.RepositoryInspector(repository).patch_fingerprint(
                    resulting_feature_commit
                ),
            )
            runtime_path = (
                repository / ".factory/runtime/milestone-integration/latest.json"
            )
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            for absent in (
                "pre_integration_head",
                "post_integration_head",
                "resulting_feature_commit",
                "validation",
            ):
                self.assertNotIn(absent, runtime)
            ledger_after_failure = ledger.path.read_bytes()
            dry_run = engine.run_project(project, "resume", dry_run=True)
            self.assertEqual(
                "integration_finalization_recovery", dry_run["proposed_next_action"]
            )
            self.assertEqual(integrating_commit, git(repository, "rev-parse", "HEAD"))
            self.assertEqual(ledger_after_failure, ledger.path.read_bytes())

            result = engine.run_project(project, "resume")

            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual(resulting_feature_commit, result["integrated_commit"])
            self.assertEqual([], launcher.requests)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())
            subjects = git(
                repository,
                "log",
                "--reverse",
                "--format=%s",
                f"{milestone_start}..HEAD",
            ).splitlines()
            self.assertEqual(
                [
                    "F001: accepted two-ref behavior",
                    "factory: mark F001 integrating",
                    "factory: record F001 integration passed",
                ],
                subjects,
            )
            self.assertEqual(
                resulting_feature_commit,
                git(repository, "rev-parse", f"{integrating_commit}^"),
            )

    def test_mismatched_accepted_adapter_fails_before_transaction_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, launcher, ledger, _, _, _, _ = self._fixture(
                Path(temporary),
                controller_project_id="interview-companion",
                adapter_project_id="live-interview-companion",
                accepted_adapter_project_id="wrong-application-adapter",
            )
            ledger_before = ledger.path.read_bytes()
            repository_before = self._git_identity(repository)

            with self.assertRaisesRegex(
                IntegrationPlanError,
                "accepted-commit adapter project identity does not match",
            ):
                engine.run_project(project, "milestone")

            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(repository_before, self._git_identity(repository))
            self.assertEqual([], launcher.requests)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_missing_live_or_accepted_adapter_id_fails_before_transaction(self):
        cases = (
            {
                "adapter_project_id": "",
                "accepted_adapter_project_id": "live-interview-companion",
                "message": "live adapter project.id is missing",
            },
            {
                "adapter_project_id": "live-interview-companion",
                "accepted_adapter_project_id": "",
                "message": "accepted-commit adapter project.id is missing",
            },
        )
        for case in cases:
            with self.subTest(message=case["message"]), tempfile.TemporaryDirectory() as temporary:
                repository, project, _, engine, launcher, ledger, _, _, _, _ = self._fixture(
                    Path(temporary),
                    controller_project_id="interview-companion",
                    adapter_project_id=case["adapter_project_id"],
                    accepted_adapter_project_id=case["accepted_adapter_project_id"],
                )
                ledger_before = ledger.path.read_bytes()
                repository_before = self._git_identity(repository)
                with self.assertRaisesRegex(IntegrationPlanError, case["message"]):
                    engine.run_project(project, "milestone")
                self.assertEqual(ledger_before, ledger.path.read_bytes())
                self.assertEqual(repository_before, self._git_identity(repository))
                self.assertEqual([], launcher.requests)
                self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_reserved_identity_change_fails_before_transaction_started(self):
        for field in ("controller_project_id", "adapter_project_id"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                repository, project, _, engine, launcher, ledger, _, _, _, _ = self._fixture(
                    Path(temporary),
                    controller_project_id="interview-companion",
                    adapter_project_id="live-interview-companion",
                )
                executable = engine._authoritative_execution_context(project)[1]
                ledger_before = ledger.path.read_bytes()
                repository_before = self._git_identity(repository)
                real_inspect = cycle_engine_module.inspect_two_refs
                calls = 0

                def changing_inspection(**kwargs):
                    nonlocal calls
                    calls += 1
                    evidence = real_inspect(**kwargs)
                    if calls == 2:
                        evidence[field] = f"changed-{field}"
                    return evidence

                with mock.patch.object(
                    cycle_engine_module,
                    "inspect_two_refs",
                    side_effect=changing_inspection,
                ):
                    with self.assertRaisesRegex(
                        IntegrationPlanError, "identity changed before TransactionStarted"
                    ):
                        engine._execute_projected_integration(
                            project, "milestone", f"changed-{field}", executable
                        )
                self.assertEqual(2, calls)
                self.assertEqual(ledger_before, ledger.path.read_bytes())
                self.assertEqual(repository_before, self._git_identity(repository))
                self.assertEqual([], launcher.requests)
                self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_mismatched_controller_ledger_id_fails_before_application_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _, _, _, launcher, ledger, _, _, _, _ = self._fixture(
                Path(temporary)
            )
            integrity = ledger.verify()
            ledger_before = ledger.path.read_bytes()
            repository_before = self._git_identity(repository)
            plan = {
                "schema_version": 2,
                "project_id": "different-controller-project",
                "controller_project_id": "different-controller-project",
                "adapter_project_id": "synthetic",
                "controller_ledger_path": str(ledger.path),
                "ledger_sequence": integrity.sequence,
                "ledger_fingerprint": integrity.fingerprint,
            }

            with self.assertRaisesRegex(
                IntegrationPlanError,
                "controller project identity does not match the controller ledger",
            ):
                executor_module._verify_ledger_binding(plan)

            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(repository_before, self._git_identity(repository))
            self.assertEqual([], launcher.requests)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_real_id_topology_reaches_transaction_boundary_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, launcher, ledger, _, _, _, _ = self._fixture(
                Path(temporary),
                controller_project_id="interview-companion",
                adapter_project_id="live-interview-companion",
            )
            executable = engine._authoritative_execution_context(project)[1]
            ledger_before = ledger.path.read_bytes()
            repository_before = self._git_identity(repository)

            with mock.patch.object(
                cycle_engine_module.WorkflowKernel,
                "begin_for_branch",
                side_effect=RuntimeError("transaction boundary reached"),
            ):
                with self.assertRaisesRegex(RuntimeError, "transaction boundary reached"):
                    engine._execute_projected_integration(
                        project, "milestone", "real-topology-boundary", executable
                    )

            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(repository_before, self._git_identity(repository))
            self.assertEqual([], launcher.requests)
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_legacy_plan_default_requires_identical_controller_and_adapter_ids(self):
        plan = {
            "schema_version": 1,
            "plan_fingerprint": "placeholder",
            "created_at": "2026-07-21T00:00:00Z",
            "project_id": "same-project",
            "adapter_project_id": "same-project",
            "repository": "/tmp/synthetic-repository",
            "repository_identity": "a" * 64,
            "repository_path_fingerprint": "b" * 64,
            "transaction_id": "transaction",
            "run_id": "run",
            "feature_id": "F001",
            "feature_branch": "codex/f001",
            "accepted_commit": "c" * 40,
            "accepted_metadata_ref": "c" * 40,
            "accepted_queue_fingerprint": "d" * 64,
            "accepted_changed_paths": ["app.txt"],
            "milestone_id": "M0",
            "milestone_branch": "codex/m0",
            "pre_integration_head": "e" * 40,
            "queue_path": "docs/FEATURE_QUEUE.yaml",
            "projection_fingerprint": "f" * 64,
            "ledger_sequence": 1,
            "ledger_fingerprint": "1" * 64,
            "controller_ledger_path": "/tmp/evidence-ledger.jsonl",
            "lease_identity": {},
            "runtime_exclusion": {},
            "validation_commands": [],
            "metadata_paths": [],
        }
        for field in executor_module.LEGACY_LEASE_IDENTITY_FIELDS:
            plan["lease_identity"][field] = None
        plan["lease_identity"].update({
            "lease_type": "integration_writer",
            "workflow_type": "milestone_integration",
            "repository_identity": plan["repository_identity"],
            "repository_path_fingerprint": plan["repository_path_fingerprint"],
            "repository_path": plan["repository"],
            "project_id": plan["project_id"],
            "transaction_id": plan["transaction_id"],
            "run_id": plan["run_id"],
            "feature_id": plan["feature_id"],
            "feature_branch": plan["feature_branch"],
            "accepted_commit": plan["accepted_commit"],
            "milestone": plan["milestone_id"],
            "starting_branch": plan["milestone_branch"],
            "starting_head": plan["pre_integration_head"],
            "session_id": None,
            "allowed_mutations": {
                "allowed_paths": ["app.txt"],
                "allowed_prefixes": [],
                "allow_untracked": False,
            },
        })
        plan["runtime_exclusion"] = {
            "pattern": RUNTIME_PATTERN,
            "repository_identity": plan["repository_identity"],
            "repository_path_fingerprint": plan["repository_path_fingerprint"],
            "verified_descendants": list(RUNTIME_DESCENDANTS),
        }
        plan["plan_fingerprint"] = executor_module._plan_fingerprint(plan)
        validate_plan_document(plan)
        plan["adapter_project_id"] = "arbitrary-alias"
        plan["plan_fingerprint"] = executor_module._plan_fingerprint(plan)
        with self.assertRaisesRegex(
            IntegrationPlanError, "may default controller_project_id only when"
        ):
            validate_plan_document(plan)

    def test_common_git_runtime_exclusion_is_exact_idempotent_and_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            inspector = RepositoryInspector(repository)
            gitignore = repository / ".gitignore"
            gitignore.write_text("/tracked-only/\n", encoding="utf-8")
            git(repository, "add", ".gitignore")
            git(repository, "commit", "-m", "test: tracked ignore baseline")
            before = gitignore.read_bytes()

            first = inspector.ensure_milestone_integration_runtime_ignored()
            second = inspector.ensure_milestone_integration_runtime_ignored()

            exclude = inspector.common_git_dir / "info/exclude"
            lines = exclude.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(RUNTIME_PATTERN))
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(list(RUNTIME_DESCENDANTS), first["verified_descendants"])
            self.assertEqual(before, gitignore.read_bytes())
            for descendant in RUNTIME_DESCENDANTS:
                self.assertEqual(
                    0,
                    inspector.git(
                        ["check-ignore", "-q", "--no-index", "--", descendant],
                        check=False,
                    ).returncode,
                )

    def test_exclusion_failure_precedes_transaction_and_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, ledger, milestone_start, _, _, _ = (
                self._fixture(Path(temporary))
            )
            executable = engine._authoritative_execution_context(project)[1]
            ledger_before = ledger.path.read_bytes()
            with mock.patch.object(
                RepositoryInspector,
                "ensure_milestone_integration_runtime_ignored",
                side_effect=RepositoryError("synthetic exclusion failure"),
            ):
                with self.assertRaisesRegex(RepositoryError, "exclusion failure"):
                    engine._execute_projected_integration(
                        project, "milestone", "exclude-failure", executable
                    )
            self.assertEqual(ledger_before, ledger.path.read_bytes())
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_mismatched_controller_lease_fails_before_application_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, milestone_start, _, _, _ = self._fixture(
                Path(temporary)
            )
            real_execute = cycle_engine_module.execute_integration_plan

            def replace_with_unrelated_lease(path: Path):
                plan = load_integration_plan(Path(path))
                lease_path = repository / ".factory/locks/writer.json"
                value = json.loads(lease_path.read_text(encoding="utf-8"))
                value["transaction_id"] = "unrelated-transaction"
                write_json(lease_path, value)
                return real_execute(Path(path))

            with mock.patch.object(
                cycle_engine_module,
                "execute_integration_plan",
                side_effect=replace_with_unrelated_lease,
            ):
                with self.assertRaisesRegex(
                    IntegrationPlanError, "lease identity mismatch"
                ):
                    engine.run_project(project, "milestone")
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertEqual("baseline\n", (repository / "app.txt").read_text(encoding="utf-8"))
            self.assertEqual(
                "unrelated-transaction",
                json.loads(
                    (repository / ".factory/locks/writer.json").read_text(encoding="utf-8")
                )["transaction_id"],
            )

    def test_feature_ref_race_is_detected_before_milestone_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, milestone_start, feature_branch, _, _ = (
                self._fixture(Path(temporary))
            )
            real_execute = cycle_engine_module.execute_integration_plan

            def advance_feature_ref(path: Path):
                git(repository, "switch", feature_branch)
                (repository / "race.txt").write_text("advanced\n", encoding="utf-8")
                git(repository, "add", "race.txt")
                git(repository, "commit", "-m", "test: feature ref race")
                git(repository, "switch", project.milestone_branch)
                return real_execute(Path(path))

            with mock.patch.object(
                cycle_engine_module,
                "execute_integration_plan",
                side_effect=advance_feature_ref,
            ):
                with self.assertRaisesRegex(
                    IntegrationPlanError, "feature branch HEAD"
                ):
                    engine.run_project(project, "milestone")
            self.assertEqual(milestone_start, git(repository, "rev-parse", project.milestone_branch))
            self.assertEqual("baseline\n", (repository / "app.txt").read_text(encoding="utf-8"))
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_semantic_conflict_is_preserved_as_a_human_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project, _, engine, _, _, _, _, accepted, _ = self._fixture(
                Path(temporary)
            )
            real_mutation = executor_module._git_mutation

            def force_racing_conflict(root, plan, argv, **kwargs):
                if argv == ["cherry-pick", accepted]:
                    (repository / "app.txt").write_text(
                        "baseline\ndivergent milestone behavior\n", encoding="utf-8"
                    )
                    git(repository, "add", "app.txt")
                    git(repository, "commit", "-m", "test: racing semantic conflict")
                return real_mutation(root, plan, argv, **kwargs)

            with mock.patch.object(
                executor_module, "_git_mutation", side_effect=force_racing_conflict
            ):
                result = engine.run_project(project, "milestone")
            self.assertEqual("human_decision_required", result["outcome"])
            self.assertEqual("SEMANTIC_CONFLICT", result["classification"])
            self.assertFalse(result["model_session_launched"])
            self.assertTrue((repository / ".git/CHERRY_PICK_HEAD").exists())
            self.assertIn("app.txt", result["human_gate"]["conflicting_paths"])


if __name__ == "__main__":
    unittest.main()
