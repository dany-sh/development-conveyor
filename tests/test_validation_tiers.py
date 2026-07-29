from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from development_conveyor.errors import ConveyorError, SafetyViolation
from development_conveyor.feature_result_recovery import FeatureResultRecovery
from development_conveyor.integration_executor import (
    _commands_from_adapter,
    _run_validation,
)
from development_conveyor.cycle_engine import CycleEngine
from development_conveyor import cli as cli_module
from development_conveyor import recovery as recovery_module
from development_conveyor.sessions import _configured_required_commands
from development_conveyor.validation import SafetyPolicy
from development_conveyor.validation_tiers import (
    FEATURE_CORE_TESTS,
    _debt_records,
    _output_record,
    _release_commands_valid,
    _release_provenance_valid,
    _repository_commands,
    _run_command,
    adapter_command_tuples,
    adapter_commands,
    equivalence_reasons,
    feature_tests,
    reconcile_known_debt,
    reconcile_release_artifacts,
    run_repository_validation,
)
from tests.helpers import git, synthetic_repository, write_json
from tests import test_two_ref_integration_recovery as two_ref_module


class ValidationTierTests(unittest.TestCase):
    @staticmethod
    def _legacy_adapter() -> dict[str, object]:
        return {
            "commands": {
                "build": [["build"]],
                "test": [["test"]],
                "lint": [],
                "package": [],
                "validate": [["validate"]],
            }
        }

    @staticmethod
    def _tiered_adapter() -> dict[str, object]:
        return {
            "validation_tiers": {
                "feature": [["feature-check"]],
                "milestone": [["milestone-check"]],
                "release": [["release-check"]],
            }
        }

    def test_tiered_adapter_selects_only_requested_tier(self):
        values, source = adapter_commands(self._tiered_adapter(), "feature")
        self.assertEqual("tiered", source)
        self.assertEqual(
            [{"group": "validation_tiers.feature", "argv": ["feature-check"]}],
            values,
        )
        self.assertEqual(
            (("milestone-check",),),
            adapter_command_tuples(self._tiered_adapter(), "milestone"),
        )

    def test_legacy_adapter_preserves_flat_configured_behavior(self):
        feature, source = adapter_commands(self._legacy_adapter(), "feature")
        milestone, _ = adapter_commands(self._legacy_adapter(), "milestone")
        release, _ = adapter_commands(self._legacy_adapter(), "release")
        self.assertEqual("legacy", source)
        self.assertEqual(feature, milestone)
        self.assertEqual(milestone, release)
        self.assertEqual(
            [["build"], ["test"], ["validate"]],
            [item["argv"] for item in feature],
        )

    def test_partial_tier_configuration_fails_closed(self):
        with self.assertRaisesRegex(ConveyorError, "requires feature"):
            adapter_commands(
                {"validation_tiers": {"feature": [["feature-check"]]}},
                "feature",
            )

    def test_fixed_core_is_six_distinct_safety_invariants(self):
        self.assertEqual(6, len(FEATURE_CORE_TESTS))
        self.assertEqual(6, len(set(FEATURE_CORE_TESTS)))
        joined = "\n".join(FEATURE_CORE_TESTS)
        for marker in (
            "ConfigurationTests",
            "LeaseAndCommandTests",
            "ProhibitedActionTests",
            "RepositoryTests",
            "QueueTests",
            "StateMachineTests",
        ):
            self.assertIn(marker, joined)

    def test_feature_selection_uses_changed_path_spec_and_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            source = repository / "src/development_conveyor/example.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            test = repository / "tests/test_example.py"
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text("import unittest\n", encoding="utf-8")
            spec = repository / "docs/features/F001.md"
            spec.write_text("Focused: `tests/test_explicit.py`\n", encoding="utf-8")
            selected = feature_tests(
                repository, specification="docs/features/F001.md"
            )
            self.assertTrue(set(FEATURE_CORE_TESTS).issubset(selected))
            self.assertIn("tests.test_example", selected)
            self.assertIn("tests.test_explicit", selected)

    def test_equivalence_is_conditional_on_authority_changes(self):
        self.assertEqual((), equivalence_reasons(["docs/README.md"]))
        self.assertEqual(
            ("tests_or_discovery_changed", "validation_routing_changed"),
            equivalence_reasons(
                [".factory/project.yaml", "tests/test_validation_tiers.py"]
            ),
        )

    def test_feature_and_milestone_never_construct_full_discovery(self):
        selected = ("tests.test_validation_tiers",)
        for tier in ("feature", "milestone"):
            commands = _repository_commands(tier, selected, repeat=1)
            self.assertNotIn(
                "discover",
                [part for _, argv, _ in commands for part in argv],
            )
        release = _repository_commands("release", (), repeat=1)
        self.assertEqual(1, sum("discover" in argv for _, argv, _ in release))
        with mock.patch.object(
            cli_module, "discover_root", return_value=Path("/resolved")
        ) as discover, mock.patch.object(
            cli_module,
            "run_repository_validation",
            return_value={"tier": "feature", "valid": True},
        ):
            result = cli_module.execute(["validate-feature"])
        self.assertTrue(result["valid"])
        discover.assert_called_once_with(Path.cwd())

    def test_release_repeat_is_explicit_and_feature_repeat_is_rejected(self):
        release = _repository_commands("release", (), repeat=3)
        self.assertEqual(
            3,
            sum(
                group.startswith("release_complete_suite")
                for group, _, _ in release
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            with self.assertRaisesRegex(
                ConveyorError, "only for explicit release"
            ):
                run_repository_validation(repository, tier="feature", repeat=2)

    def test_integration_plan_consumes_milestone_tier(self):
        self.assertEqual(
            [
                {
                    "group": "validation_tiers.milestone",
                    "argv": ["milestone-check"],
                }
            ],
            _commands_from_adapter(self._tiered_adapter()),
        )

    def test_integration_legacy_fallback_is_caller_visible(self):
        self.assertEqual(
            [
                {"group": "build", "argv": ["build"]},
                {"group": "test", "argv": ["test"]},
                {"group": "validate", "argv": ["validate"]},
            ],
            _commands_from_adapter(self._legacy_adapter()),
        )

    def test_cycle_engine_selects_feature_and_milestone_tiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._tiered_adapter())
            project = SimpleNamespace(repository=repository)
            self.assertEqual(
                (("feature-check",),),
                CycleEngine._configured_kernel_commands(project),
            )
            self.assertEqual(
                (("milestone-check",),),
                CycleEngine._configured_kernel_commands(
                    project, tier="milestone"
                ),
            )

    def test_cycle_engine_legacy_fallback_is_caller_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._legacy_adapter())
            project = SimpleNamespace(repository=repository)
            expected = (("build",), ("test",), ("validate",))
            self.assertEqual(
                expected, CycleEngine._configured_kernel_commands(project)
            )
            self.assertEqual(
                expected,
                CycleEngine._configured_kernel_commands(
                    project, tier="milestone"
                ),
            )

    def test_session_result_validation_consumes_milestone_tier(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._tiered_adapter())
            project = SimpleNamespace(
                repository=repository,
                validation_source=".factory/project.yaml",
            )
            self.assertEqual(
                (("milestone-check",),),
                _configured_required_commands(project),
            )

    def test_sessions_legacy_fallback_is_caller_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._legacy_adapter())
            project = SimpleNamespace(
                repository=repository,
                validation_source=".factory/project.yaml",
            )
            self.assertEqual(
                (("build",), ("test",), ("validate",)),
                _configured_required_commands(project),
            )

    def test_recovery_legacy_fallback_is_caller_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._legacy_adapter())
            project = SimpleNamespace(
                repository=repository,
                validation_source=".factory/project.yaml",
            )
            self.assertEqual(
                (("build",), ("test",), ("validate",)),
                recovery_module._configured_required_commands(project),
            )

    def test_retained_finalizer_consumes_feature_tier_when_declared(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._tiered_adapter())
            recovery = FeatureResultRecovery.__new__(FeatureResultRecovery)
            recovery.project = SimpleNamespace(
                repository=repository,
                validation_source=".factory/project.yaml",
            )
            self.assertEqual(
                [["feature-check"]],
                recovery._validation_commands(
                    tracked_paths=("Sources/App.swift",),
                    untracked_paths=(),
                ),
            )

    def test_retained_finalizer_legacy_fallback_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            adapter = repository / ".factory/project.yaml"
            adapter.parent.mkdir(parents=True)
            write_json(adapter, self._legacy_adapter())
            swift_test = repository / "Tests/AppTests/AppTests.swift"
            swift_test.parent.mkdir(parents=True)
            swift_test.write_text("// synthetic\n", encoding="utf-8")
            recovery = FeatureResultRecovery.__new__(FeatureResultRecovery)
            recovery.project = SimpleNamespace(
                repository=repository,
                validation_source=".factory/project.yaml",
            )
            self.assertEqual(
                [
                    ["swift", "test", "--filter", "AppTests"],
                    ["swift", "build"],
                    ["git", "diff", "--check"],
                ],
                recovery._validation_commands(
                    tracked_paths=("Sources/App.swift",),
                    untracked_paths=(),
                ),
            )

    def test_milestone_comparison_defaults_to_one_observation_per_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            head = git(repository, "rev-parse", "HEAD")
            observation = {
                "ref": head,
                "commit": head,
                "selected_tests": [],
                "record": {"exit_status": 0},
            }
            command_record = {
                "group": "milestone_tests",
                "argv": ["python3"],
                "exit_status": 0,
                "duration_seconds": 0.1,
                "tests_run": 1,
                "output_sha256": "0" * 64,
                "output_tail": "",
                "debt": [],
            }
            with mock.patch(
                "development_conveyor.validation_tiers._observe_candidate",
                return_value=observation,
            ) as observe, mock.patch(
                "development_conveyor.validation_tiers._run_command",
                return_value=command_record,
            ):
                result = run_repository_validation(
                    repository,
                    tier="milestone",
                    prepared_parent=head,
                    candidate=head,
                )
            self.assertTrue(result["comparison"]["performed"])
            self.assertEqual(1, result["comparison"]["observations_per_ref"])
            self.assertEqual(2, observe.call_count)

    def test_milestone_comparison_reads_integration_refs_from_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            head = git(repository, "rev-parse", "HEAD")
            observation = {
                "ref": head,
                "commit": head,
                "selected_tests": [],
                "record": {"exit_status": 0},
            }
            command_record = {
                "group": "milestone_tests",
                "argv": ["python3"],
                "exit_status": 0,
                "duration_seconds": 0.1,
                "tests_run": 1,
                "output_sha256": "0" * 64,
                "output_tail": "",
                "debt": [],
            }
            with mock.patch.dict(
                os.environ,
                {
                    "CONVEYOR_PREPARED_PARENT": head,
                    "CONVEYOR_CANDIDATE": head,
                },
            ), mock.patch(
                "development_conveyor.validation_tiers._observe_candidate",
                return_value=observation,
            ) as observe, mock.patch(
                "development_conveyor.validation_tiers._run_command",
                return_value=command_record,
            ):
                result = run_repository_validation(repository, tier="milestone")
            self.assertTrue(result["comparison"]["performed"])
            self.assertEqual(2, observe.call_count)

    def test_required_milestone_comparison_fails_without_both_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _ = synthetic_repository(Path(temporary))
            routing = repository / ".factory/project.yaml"
            routing.write_text(
                routing.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            command_record = {
                "group": "milestone_tests",
                "argv": ["python3"],
                "exit_status": 0,
                "duration_seconds": 0.1,
                "tests_run": 1,
                "output_sha256": "0" * 64,
                "output_tail": "",
                "debt": [],
            }
            with mock.patch(
                "development_conveyor.validation_tiers._run_command",
                return_value=command_record,
            ):
                result = run_repository_validation(
                    repository,
                    tier="milestone",
                    base="HEAD^",
                )
            self.assertTrue(result["comparison"]["required"])
            self.assertFalse(result["comparison"]["performed"])
            self.assertFalse(result["valid"])

    def test_safety_policy_observes_invocation_and_blocks_prohibition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with SafetyPolicy.observe_command_attempts() as observed:
                record = _run_command(
                    root,
                    "safe",
                    ["python3", "-c", "print('safe')"],
                    timeout=30,
                )
                with self.assertRaises(SafetyViolation):
                    _run_command(
                        root,
                        "prohibited",
                        ["gh", "release", "create", "v1"],
                        timeout=30,
                    )
            self.assertEqual(0, record["exit_status"])
            self.assertEqual(
                ["configured_validation", "configured_validation"],
                [item["authority"] for item in observed],
            )
            self.assertEqual(
                ["release"], observed[-1]["prohibited_categories"]
            )

    def test_debt_parser_canonicalizes_verbose_and_summary_subtests(self):
        output = (
            "test_one (test_alpha.Example.test_one) ... FAIL\n"
            "test_two (test_alpha.Example) ... ERROR\n"
            "test_modes (test_beta.Example) "
            "(mode='clean', classification=\"BLOCKED\") ... ERROR\n"
        )
        self.assertEqual(
            [
                {"id": "test_alpha.Example.test_one", "outcome": "failure"},
                {"id": "test_alpha.Example.test_two", "outcome": "error"},
                {
                    "id": (
                        "test_beta.Example.test_modes"
                        "[mode=clean,classification=BLOCKED]"
                    ),
                    "outcome": "error",
                },
            ],
            _debt_records(output),
        )
        summary = (
            "FAIL: test_16_cycle_checkpoint_precedes_session_launch "
            "(test_cli_compatibility_repair.CompatibilityRepairTests)\n"
            "ERROR: "
            "test_phase_bridge_terminalizes_exit_zero_human_and_nonzero_retryable "
            "(test_transactional_kernel.MigrationAndSimulatorTests) "
            "(classification=\"HUMAN_DECISION_REQUIRED\")\n"
        )
        self.assertEqual(
            [
                {
                    "id": (
                        "test_cli_compatibility_repair.CompatibilityRepairTests."
                        "test_16_cycle_checkpoint_precedes_session_launch"
                    ),
                    "outcome": "failure",
                },
                {
                    "id": (
                        "test_transactional_kernel.MigrationAndSimulatorTests."
                        "test_phase_bridge_terminalizes_exit_zero_human_and_nonzero_retryable"
                        "[classification=HUMAN_DECISION_REQUIRED]"
                    ),
                    "outcome": "error",
                },
            ],
            _debt_records(summary),
        )
        bracket = (
            "test_bracket (test_delta.Example) "
            "[classification=blocked] ... ERROR\n"
        )
        self.assertEqual(
            [
                {
                    "id": (
                        "test_delta.Example.test_bracket"
                        "[classification=blocked]"
                    ),
                    "outcome": "error",
                }
            ],
            _debt_records(bracket),
        )
        python39_subtests = (
            "test_one (test_alpha.Example) ... FAIL\n"
            "test_modes (test_beta.Example) ... "
            "test_after (test_gamma.Example) ... ok\n"
            "FAIL: test_one (test_alpha.Example)\n"
            "ERROR: test_modes (test_beta.Example) (mode='clean')\n"
            "ERROR: test_modes (test_beta.Example) (mode='dirty')\n"
        )
        self.assertEqual(
            [
                {"id": "test_alpha.Example.test_one", "outcome": "failure"},
                {
                    "id": "test_beta.Example.test_modes[mode=clean]",
                    "outcome": "error",
                },
                {
                    "id": "test_beta.Example.test_modes[mode=dirty]",
                    "outcome": "error",
                },
            ],
            _debt_records(python39_subtests),
        )

    def test_debt_parser_fails_closed_on_ambiguity(self):
        with self.assertRaisesRegex(ConveyorError, "unparseable"):
            _debt_records("FAIL: an invalid heading\n")
        with self.assertRaisesRegex(ConveyorError, "unparseable"):
            _debt_records(
                "test_mode (test_alpha.Example.test_mode) "
                "(mode='unterminated) ... FAIL\n"
            )
        with self.assertRaisesRegex(ConveyorError, "duplicate normalized"):
            _debt_records(
                "FAIL: test_mode (test_alpha.Example) (mode='clean')\n"
                "FAIL: test_mode (test_alpha.Example) (mode=\"clean\")\n"
            )
        ordinary_and_summary = (
            "test_one (test_alpha.Example.test_one) ... FAIL\n"
            "FAIL: test_one (test_alpha.Example)\n"
        )
        self.assertEqual(
            [{"id": "test_alpha.Example.test_one", "outcome": "failure"}],
            _debt_records(ordinary_and_summary),
        )
        divergent_forms = (
            (
                "partial_map",
                "test_one (test_alpha.Example) ... FAIL\n"
                "test_two (test_alpha.Example) ... ERROR\n"
                "FAIL: test_one (test_alpha.Example)\n",
            ),
            (
                "outcome_mismatch",
                "test_one (test_alpha.Example) ... FAIL\n"
                "ERROR: test_one (test_alpha.Example)\n",
            ),
            (
                "summary_subtest_without_verbose_parent",
                "test_one (test_alpha.Example) ... FAIL\n"
                "FAIL: test_one (test_alpha.Example)\n"
                "ERROR: test_modes (test_beta.Example) (mode='clean')\n",
            ),
        )
        for case, output in divergent_forms:
            with self.subTest(case=case), self.assertRaisesRegex(
                ConveyorError, "maps differ"
            ):
                _debt_records(output)

    def test_debt_reconciliation_requires_exact_identities_and_outcomes(self):
        observed = [
            {"id": "test_alpha.Example.test_one", "outcome": "failure"},
            {"id": "test_beta.Example.test_two", "outcome": "error"},
        ]
        exact = reconcile_known_debt(observed, list(observed))
        self.assertTrue(exact["exact"])
        self.assertTrue(exact["capture_complete"])
        self.assertEqual({"failure": 1, "error": 1}, exact["outcome_counts"])
        mismatch = reconcile_known_debt(
            observed,
            [
                {"id": "test_alpha.Example.test_one", "outcome": "error"},
                {"id": "test_gamma.Example.test_three", "outcome": "error"},
            ],
        )
        self.assertFalse(mismatch["exact"])
        self.assertEqual(
            ["test_alpha.Example.test_one"], mismatch["outcome_mismatches"]
        )
        self.assertEqual(
            ["test_gamma.Example.test_three"], mismatch["missing_identities"]
        )
        self.assertEqual(
            ["test_beta.Example.test_two"], mismatch["unexpected_identities"]
        )
        permitted = reconcile_known_debt(
            [{"id": "test_alpha.Example.test_one", "outcome": "error"}],
            [{
                "id": "test_alpha.Example.test_one",
                "outcome": "failure",
                "permitted_candidate_outcomes": ["failure", "error"],
                "rationale": "environment-sensitive generated state dependency",
            }],
        )
        self.assertTrue(permitted["exact"])
        self.assertTrue(permitted["capture_complete"])
        self.assertEqual(0, permitted["classification_exact_count"])
        self.assertEqual(
            [{
                "id": "test_alpha.Example.test_one",
                "baseline_outcome": "failure",
                "candidate_outcome": "error",
                "permitted_candidate_outcomes": ["failure", "error"],
                "rationale": "environment-sensitive generated state dependency",
            }],
            permitted["permitted_outcome_shifts"],
        )
        undeclared = reconcile_known_debt(
            [{"id": "test_alpha.Example.test_one", "outcome": "error"}],
            [{"id": "test_alpha.Example.test_one", "outcome": "failure"}],
        )
        self.assertFalse(undeclared["exact"])
        self.assertEqual(
            ["test_alpha.Example.test_one"],
            undeclared["outcome_mismatches"],
        )

    def test_pending_debt_capture_never_counts_as_exact_acceptance(self):
        observed = [{"id": "test_alpha.Example.test_one", "outcome": "failure"}]
        result = reconcile_known_debt(
            observed,
            [{"id": "test_alpha.Example.test_one", "outcome": "pending_observation"}],
            allow_pending_capture=True,
        )
        self.assertFalse(result["exact"])
        self.assertFalse(result["capture_complete"])
        self.assertEqual(
            observed, result["observed_records"]
        )

    def test_output_tail_is_redacted_without_changing_raw_hash_or_debt(self):
        sensitive_values = {
            "bearer": "-".join(("synthetic", "bearer", "credential")),
            "key": "-".join(("synthetic", "api", "credential")),
            "password": "-".join(("synthetic", "password", "credential")),
        }
        raw = (
            f"Authorization: Bearer {sensitive_values['bearer']}\n"
            f"api_key={sensitive_values['key']}\n"
            f"password={sensitive_values['password']}\n"
            "/Users/person/Library/CloudStorage/Provider/private/file.txt\n"
            "FAIL: test_one (test_alpha.Example)\n"
        )
        record = _output_record(
            group="release_complete_suite_1",
            argv=["python3", "-m", "unittest"],
            returncode=1,
            stdout=raw,
            stderr="",
            duration=0.1,
        )
        combined = (raw + "\n").strip()
        self.assertEqual(
            hashlib.sha256(combined.encode("utf-8")).hexdigest(),
            record["output_sha256"],
        )
        self.assertEqual(
            [{"id": "test_alpha.Example.test_one", "outcome": "failure"}],
            record["debt"],
        )
        for value in sensitive_values.values():
            self.assertNotIn(value, record["output_tail"])
        self.assertNotIn(
            "/Users/person/Library/CloudStorage",
            record["output_tail"],
        )
        self.assertIn("[REDACTED]", record["output_tail"])
        self.assertIn("[REDACTED_USER_DATA_PATH]", record["output_tail"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Synthetic Conveyor")
            git(root, "config", "user.email", "synthetic@example.invalid")
            (root / ".gitignore").write_text("reports/\n", encoding="utf-8")
            git(root, "add", ".gitignore")
            git(root, "commit", "-m", "synthetic release root")
            command = [
                "python3",
                "-c",
                (
                    "import sys; "
                    "print('short synthetic stdout'); "
                    "print('short synthetic stderr', file=sys.stderr)"
                ),
            ]
            first = _run_command(
                root,
                "release_complete_suite_1",
                command,
                timeout=30,
                preserve_release_artifacts=True,
            )
            second = _run_command(
                root,
                "release_complete_suite_1",
                command,
                timeout=30,
                preserve_release_artifacts=True,
            )
            first_execution = first["release_execution"]
            second_execution = second["release_execution"]
            self.assertNotEqual(
                first_execution["execution_id"],
                second_execution["execution_id"],
            )
            self.assertNotEqual(
                first_execution["raw_stdout_path"],
                second_execution["raw_stdout_path"],
            )
            for execution in (first_execution, second_execution):
                self.assertEqual("complete", execution["completion_state"])
                self.assertEqual("succeeded", execution["parser_status"])
                self.assertEqual(0, execution["child_exit_code"])
                self.assertTrue(execution["provenance_valid"])
                self.assertTrue(
                    _release_provenance_valid(execution, repository=root)
                )
                self.assertEqual(
                    execution["starting_provenance"]["branch"],
                    execution["final_provenance"]["branch"],
                )
                self.assertEqual(
                    execution["starting_provenance"]["commit"],
                    execution["final_provenance"]["commit"],
                )
                self.assertEqual(
                    execution["starting_provenance"]["tree"],
                    execution["final_provenance"]["tree"],
                )
                for key in (
                    "raw_stdout_path",
                    "raw_stderr_path",
                    "metadata_path",
                ):
                    artifact = root / execution[key]
                    self.assertTrue(artifact.is_file())
                    self.assertEqual(0o600, artifact.stat().st_mode & 0o777)
                metadata = json.loads(
                    (root / execution["metadata_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(execution, metadata)
                self.assertEqual(
                    hashlib.sha256(
                        (root / execution["raw_stdout_path"]).read_bytes()
                    ).hexdigest(),
                    metadata["stdout_sha256"],
                )
                self.assertEqual(
                    hashlib.sha256(
                        (root / execution["raw_stderr_path"]).read_bytes()
                    ).hexdigest(),
                    metadata["stderr_sha256"],
                )

            fixed_id = "f" * 32
            with mock.patch(
                "development_conveyor.validation_tiers.uuid.uuid4",
                return_value=SimpleNamespace(hex=fixed_id),
            ):
                fixed = _run_command(
                    root,
                    "release_complete_suite_1",
                    command,
                    timeout=30,
                    preserve_release_artifacts=True,
                )
                fixed_stdout = (
                    root / fixed["release_execution"]["raw_stdout_path"]
                )
                original = fixed_stdout.read_bytes()
                with self.assertRaises(FileExistsError):
                    _run_command(
                        root,
                        "release_complete_suite_1",
                        command,
                        timeout=30,
                        preserve_release_artifacts=True,
                    )
                self.assertEqual(original, fixed_stdout.read_bytes())

            invalid_command = [
                "python3",
                "-c",
                "import sys; print('FAIL: malformed'); sys.exit(1)",
            ]
            before = set((root / "reports").glob("*.execution.json"))
            with self.assertRaisesRegex(ConveyorError, "unparseable"):
                _run_command(
                    root,
                    "release_complete_suite_1",
                    invalid_command,
                    timeout=30,
                    preserve_release_artifacts=True,
                )
            after = set((root / "reports").glob("*.execution.json"))
            failed_path = (after - before).pop()
            failed = json.loads(failed_path.read_text(encoding="utf-8"))
            self.assertEqual("parser_failed", failed["completion_state"])
            self.assertEqual("failed", failed["parser_status"])
            self.assertEqual(1, failed["child_exit_code"])
            self.assertTrue(failed["stdout_sha256"])
            self.assertTrue(failed["stderr_sha256"])
            self.assertTrue(failed["provenance_valid"])
            self.assertTrue(failed["completed_at"])
            self.assertEqual(
                failed["starting_provenance"]["commit"],
                failed["final_provenance"]["commit"],
            )
            self.assertTrue((root / failed["raw_stdout_path"]).is_file())
            self.assertTrue((root / failed["raw_stderr_path"]).is_file())

    def test_release_execution_provenance_binds_clean_commit_tree_and_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-b", "release-candidate")
            git(root, "config", "user.name", "Synthetic Conveyor")
            git(root, "config", "user.email", "synthetic@example.invalid")
            (root / ".gitignore").write_text("reports/\n", encoding="utf-8")
            git(root, "add", ".gitignore")
            git(root, "commit", "-m", "synthetic release candidate")
            record = _run_command(
                root,
                "release_complete_suite_1",
                ["python3", "-c", "print('short synthetic command')"],
                timeout=30,
                preserve_release_artifacts=True,
            )
            execution = record["release_execution"]
            expected_commit = git(root, "rev-parse", "HEAD")
            expected_tree = git(root, "rev-parse", "HEAD^{tree}")
            self.assertTrue(
                _release_provenance_valid(execution, repository=root)
            )
            self.assertEqual(str(root.resolve()), execution[
                "starting_provenance"
            ]["repository_path"])
            self.assertEqual(
                "release-candidate", execution["starting_provenance"]["branch"]
            )
            self.assertEqual(
                expected_commit, execution["starting_provenance"]["commit"]
            )
            self.assertEqual(
                expected_tree, execution["starting_provenance"]["tree"]
            )
            self.assertEqual([], execution["starting_provenance"]["changed_paths"])
            self.assertEqual([], execution["final_provenance"]["changed_paths"])

    def test_release_execution_provenance_rejects_dirty_or_drifting_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-b", "release-candidate")
            git(root, "config", "user.name", "Synthetic Conveyor")
            git(root, "config", "user.email", "synthetic@example.invalid")
            (root / ".gitignore").write_text("reports/\n", encoding="utf-8")
            tracked = root / "tracked.txt"
            tracked.write_text("clean\n", encoding="utf-8")
            git(root, "add", ".gitignore", "tracked.txt")
            git(root, "commit", "-m", "synthetic release candidate")

            tracked.write_text("dirty\n", encoding="utf-8")
            dirty = _run_command(
                root,
                "release_complete_suite_1",
                ["python3", "-c", "print('dirty synthetic command')"],
                timeout=30,
                preserve_release_artifacts=True,
            )
            self.assertFalse(dirty["release_execution"]["provenance_valid"])
            self.assertFalse(_release_commands_valid(
                [dirty], {"exact": True}, repository=root
            ))

            tracked.write_text("clean\n", encoding="utf-8")
            drift = _run_command(
                root,
                "release_complete_suite_2",
                [
                    "python3",
                    "-c",
                    (
                        "import subprocess; "
                        "subprocess.run(['git', 'switch', '-c', 'drift'], check=True)"
                    ),
                ],
                timeout=30,
                preserve_release_artifacts=True,
            )
            self.assertFalse(drift["release_execution"]["provenance_valid"])
            self.assertFalse(_release_commands_valid(
                [drift], {"exact": True}, repository=root
            ))

    def test_prepared_parent_and_synthetic_candidate_debt_maps_are_exact(self):
        catalog_path = (
            Path(__file__).resolve().parents[1]
            / "docs/testing/KNOWN_TEST_DEBT.json"
        )
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        baseline = catalog["records"]
        identities = [record["id"] for record in baseline]
        outcomes = [record["outcome"] for record in baseline]
        self.assertEqual(37, len(baseline))
        self.assertEqual(37, len(set(identities)))
        self.assertEqual(21, outcomes.count("failure"))
        self.assertEqual(16, outcomes.count("error"))
        self.assertTrue(all(record["classification"] for record in baseline))
        baseline_reconciliation = reconcile_known_debt(baseline, baseline)
        self.assertTrue(baseline_reconciliation["exact"])

        transition = catalog["isolated_reconstruction_observation"][
            "outcome_transition"
        ]
        candidate = [
            {
                **record,
                "outcome": (
                    transition["isolated_outcome"]
                    if record["id"] == transition["id"]
                    else record["outcome"]
                ),
            }
            for record in baseline
        ]
        candidate_identities = [record["id"] for record in candidate]
        candidate_outcomes = [record["outcome"] for record in candidate]
        self.assertEqual(37, len(set(candidate_identities)))
        self.assertEqual(set(identities), set(candidate_identities))
        self.assertEqual(20, candidate_outcomes.count("failure"))
        self.assertEqual(17, candidate_outcomes.count("error"))
        changed = [
            record["id"]
            for record, observed in zip(baseline, candidate)
            if record["outcome"] != observed["outcome"]
        ]
        self.assertEqual([transition["id"]], changed)
        self.assertEqual("failure", transition["baseline_outcome"])
        self.assertEqual("error", transition["isolated_outcome"])
        self.assertIn("excluded", transition["reason"])

        candidate_reconciliation = reconcile_known_debt(candidate, baseline)
        self.assertTrue(candidate_reconciliation["exact"])
        self.assertEqual([], candidate_reconciliation["missing_identities"])
        self.assertEqual([], candidate_reconciliation["unexpected_identities"])
        self.assertEqual([], candidate_reconciliation["validation_errors"])
        self.assertEqual([], candidate_reconciliation["outcome_mismatches"])
        self.assertEqual(
            {"failure": 20, "error": 17},
            candidate_reconciliation["outcome_counts"],
        )
        self.assertEqual(
            {"failure": 21, "error": 16},
            candidate_reconciliation["catalog_outcome_counts"],
        )
        self.assertEqual(36, candidate_reconciliation["classification_exact_count"])
        self.assertEqual(
            [{
                "id": transition["id"],
                "baseline_outcome": "failure",
                "candidate_outcome": "error",
                "permitted_candidate_outcomes": ["failure", "error"],
                "rationale": (
                    "Environment-sensitive generated controller state dependency: "
                    "the authenticated release worktree intentionally lacks the "
                    "excluded evidence-ledger fixture."
                ),
            }],
            candidate_reconciliation["permitted_outcome_shifts"],
        )

        strict_baseline = [
            {
                key: value
                for key, value in record.items()
                if key not in {"permitted_candidate_outcomes", "rationale"}
            }
            for record in baseline
        ]
        strict_reconciliation = reconcile_known_debt(candidate, strict_baseline)
        self.assertFalse(strict_reconciliation["exact"])
        self.assertEqual(
            [transition["id"]],
            strict_reconciliation["outcome_mismatches"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Synthetic Conveyor")
            git(root, "config", "user.email", "synthetic@example.invalid")
            debt_path = root / "docs/testing/KNOWN_TEST_DEBT.json"
            write_json(
                debt_path,
                {
                    "records": [
                        {
                            "id": "test_alpha.Example.test_one",
                            "outcome": "failure",
                            "permitted_candidate_outcomes": ["failure", "error"],
                            "rationale": "environment-sensitive generated state dependency",
                        },
                        {
                            "id": "test_beta.Example.test_two",
                            "outcome": "error",
                        },
                    ]
                },
            )
            git(root, "add", ".")
            git(root, "commit", "-m", "synthetic release baseline")
            implementation_commit = git(root, "rev-parse", "HEAD")
            implementation_tree = git(root, "rev-parse", "HEAD^{tree}")
            execution_id = "a" * 32
            reports = root / "reports"
            reports.mkdir()
            stdout_path = reports / f"release-validation-{execution_id}.stdout.raw"
            stderr_path = reports / f"release-validation-{execution_id}.stderr.raw"
            metadata_path = reports / f"release-validation-{execution_id}.execution.json"
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(
                "ERROR: test_one (test_alpha.Example)\n"
                "ERROR: test_two (test_beta.Example)\n"
                "Ran 2 tests in 0.001s\n"
                "FAILED (errors=2)\n",
                encoding="utf-8",
            )
            execution = {
                "schema_version": 1,
                "execution_id": execution_id,
                "group": "release_complete_suite_1",
                "raw_stdout_path": stdout_path.relative_to(root).as_posix(),
                "raw_stderr_path": stderr_path.relative_to(root).as_posix(),
                "metadata_path": metadata_path.relative_to(root).as_posix(),
                "child_exit_code": 1,
                "stdout_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
                "completion_state": "complete",
                "parser_status": "succeeded",
                "created_at": "2026-07-29T00:00:00+00:00",
                "completed_at": "2026-07-29T00:00:01+00:00",
                "starting_provenance": {
                    "repository_path": str(root.resolve()),
                    "branch": "main",
                    "commit": implementation_commit,
                    "tree": implementation_tree,
                    "worktree_clean": True,
                    "changed_paths": [],
                    "observed_at": "2026-07-29T00:00:00+00:00",
                },
                "final_provenance": {
                    "repository_path": str(root.resolve()),
                    "branch": "main",
                    "commit": implementation_commit,
                    "tree": implementation_tree,
                    "worktree_clean": True,
                    "changed_paths": [],
                    "observed_at": "2026-07-29T00:00:01+00:00",
                },
                "provenance_valid": True,
            }
            write_json(metadata_path, execution)
            raw_record = _output_record(
                group="release_complete_suite_1",
                argv=["python3", "-m", "unittest"],
                returncode=1,
                stdout=stdout_path.read_text(encoding="utf-8"),
                stderr=stderr_path.read_text(encoding="utf-8"),
                duration=0.001,
            )
            raw_record["release_execution"] = execution
            release_json_path = root / "release-validation.json"
            write_json(
                release_json_path,
                {
                    "schema_version": 1,
                    "tier": "release",
                    "valid": False,
                    "complete_suite_invocations": 1,
                    "test_count": 2,
                    "commands": [raw_record],
                    "release_provenance": {
                        "execution_ids": [execution_id],
                        "executions": [execution],
                        "valid": True,
                    },
                    "debt_reconciliation": reconcile_known_debt(
                        raw_record["debt"],
                        [
                            {
                                "id": "test_alpha.Example.test_one",
                                "outcome": "failure",
                            },
                            {
                                "id": "test_beta.Example.test_two",
                                "outcome": "error",
                            },
                        ],
                    ),
                },
            )
            artifact_path = root / "release-reconciliation.json"
            artifact = reconcile_release_artifacts(
                root,
                release_json_path=release_json_path,
                execution_metadata_path=metadata_path,
                raw_stdout_path=stdout_path,
                raw_stderr_path=stderr_path,
                output_path=artifact_path,
                implementation_commit=implementation_commit,
            )
            self.assertTrue(artifact["reconciliation_valid"])
            self.assertTrue(artifact["reconciliation"]["capture_complete"])
            self.assertTrue(artifact["valid"])
            self.assertEqual(1, len(
                artifact["reconciliation"]["permitted_outcome_shifts"]
            ))
            self.assertEqual(0o600, artifact_path.stat().st_mode & 0o777)
            stderr_path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ConveyorError, "raw artifact hash"):
                reconcile_release_artifacts(
                    root,
                    release_json_path=release_json_path,
                    execution_metadata_path=metadata_path,
                    raw_stdout_path=stdout_path,
                    raw_stderr_path=stderr_path,
                    output_path=root / "tampered-reconciliation.json",
                    implementation_commit=implementation_commit,
                )

    def test_tiered_integration_runtime_has_one_authoritative_command_record(self):
        fixture = two_ref_module.TwoRefIntegrationRecoveryTests(
            methodName="runTest"
        )
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                configuration,
                engine,
                launcher,
                _,
                _,
                feature_branch,
                old_accepted,
                _,
            ) = fixture._fixture(Path(temporary))
            git(repository, "switch", feature_branch)
            adapter_path = repository / ".factory/project.yaml"
            adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
            milestone_command = [
                "python3",
                "-c",
                "print('tiered milestone command')",
            ]
            adapter["validation_tiers"] = {
                "feature": [["python3", "-c", "print('feature')"]],
                "milestone": [milestone_command],
                "release": [["python3", "-c", "print('release')"]],
            }
            write_json(adapter_path, adapter)
            git(repository, "add", ".factory/project.yaml")
            git(repository, "commit", "--amend", "--no-edit")
            accepted = git(repository, "rev-parse", "HEAD")
            self.assertNotEqual(old_accepted, accepted)
            report_path = (
                configuration.root
                / "reports/failed-two-ref-run/milestone_integration.json"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["accepted_commit"] = accepted
            write_json(report_path, report)
            git(repository, "switch", project.milestone_branch)

            with SafetyPolicy.observe_command_attempts() as observed:
                result = engine.run_project(project, "milestone")

            self.assertEqual("feature_integrated", result["outcome"])
            self.assertEqual([], launcher.requests)
            runtime_path = (
                repository
                / ".factory/runtime/milestone-integration/latest.json"
            )
            self.assertTrue(runtime_path.is_file())
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            command_records = runtime["evidence"]["validation"]["commands"]
            self.assertEqual(
                milestone_command, command_records[0]["argv"]
            )
            self.assertEqual(
                "validation_tiers.milestone", command_records[0]["group"]
            )
            self.assertEqual(0, command_records[0]["exit_code"])
            self.assertEqual(
                1,
                sum(item.get("argv") == milestone_command for item in command_records),
            )
            self.assertTrue(
                any(
                    item["authority"] == "configured_validation"
                    and item["operation"] == "-c"
                    for item in observed
                )
            )


if __name__ == "__main__":
    unittest.main()
