"""Small, explicit validation tiers for feature, milestone, and release work."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .errors import ConveyorError
from .logging import atomic_write_json, utc_now
from .redaction import redact_text, redact_value
from .validation import SafetyPolicy

TIERS = ("feature", "milestone", "release")
LEGACY_GROUPS = ("build", "test", "lint", "package", "validate")
DEBT_OUTCOMES = ("failure", "error")

FEATURE_CORE_TESTS = (
    "tests.test_config.ConfigurationTests.test_home_expansion_and_validation",
    "tests.test_transactional_kernel.LeaseAndCommandTests.test_each_phase_uses_its_exact_typed_lease",
    "tests.test_prohibited_actions.ProhibitedActionTests.test_prohibited_git_and_external_operations_are_rejected",
    "tests.test_repository.RepositoryTests.test_identity_is_stable_and_state_is_detected",
    "tests.test_queue.QueueTests.test_selects_ready_feature_with_documented_order",
    "tests.test_state_machine.StateMachineTests.test_every_declared_edge_and_idempotent_transition",
)

MILESTONE_TESTS = (
    "tests.test_cycle_engine.CycleEngineTests.test_complete_one_feature_cycle_integrates_exactly_one_commit",
    "tests.test_deterministic_integration_handoff.DeterministicIntegrationHandoffTests.test_exact_plan_bypasses_live_ready_queue_and_adopts_one_controller_lease",
    "tests.test_deterministic_integration_handoff.DeterministicIntegrationHandoffTests.test_distinct_controller_and_adapter_ids_are_serialized_and_lease_bound",
    "tests.test_two_ref_integration_recovery.TwoRefIntegrationRecoveryTests.test_live_ready_queue_integrates_from_two_verified_refs_in_fresh_transaction",
    "tests.test_two_ref_integration_recovery.TwoRefIntegrationRecoveryTests.test_feature_and_milestone_ref_drift_fail_before_new_ledger_events",
    "tests.test_milestone_integration_result_contract.MilestoneIntegrationResultContractTests.test_valid_completed_integration_result_is_accepted",
    "tests.test_milestone_integration_result_contract.MilestoneIntegrationResultContractTests.test_terminal_failed_session_selects_fresh_recovery_integration",
    "tests.test_reconciliation_contract.ReconciliationContractTests.test_01_relative_queue_discovery",
    "tests.test_reconciliation_contract.ReconciliationContractTests.test_30_validation_failed_can_recover_to_feature_ready",
    "tests.test_post_integration_finalization.PostIntegrationFinalizationTests.test_01_required_integration_validator_failure_is_authoritative",
    "tests.test_post_integration_finalization.PostIntegrationFinalizationTests.test_20_recovery_does_not_duplicate_integration",
    "tests.test_cache_binding_recovery.CacheBindingRecoveryTests.test_stale_cache_routes_and_repairs_without_feature_dispatch",
    "tests.test_cache_binding_recovery.CacheBindingRecoveryTests.test_active_transaction_or_dirty_repository_fails_closed",
    "tests.test_feature_result_recovery.GeneralFeatureResultRecoveryTests.test_general_dry_run_binds_exact_retained_state_and_alias",
)

FOCUSED_VALIDATION_TESTS = (
    "tests.test_validation_tiers.ValidationTierTests.test_tiered_adapter_selects_only_requested_tier",
    "tests.test_validation_tiers.ValidationTierTests.test_legacy_adapter_preserves_flat_configured_behavior",
    "tests.test_validation_tiers.ValidationTierTests.test_partial_tier_configuration_fails_closed",
    "tests.test_validation_tiers.ValidationTierTests.test_fixed_core_is_six_distinct_safety_invariants",
    "tests.test_validation_tiers.ValidationTierTests.test_feature_selection_uses_changed_path_spec_and_core",
    "tests.test_validation_tiers.ValidationTierTests.test_feature_and_milestone_never_construct_full_discovery",
    "tests.test_validation_tiers.ValidationTierTests.test_release_repeat_is_explicit_and_feature_repeat_is_rejected",
    "tests.test_validation_tiers.ValidationTierTests.test_integration_plan_consumes_milestone_tier",
    "tests.test_validation_tiers.ValidationTierTests.test_integration_legacy_fallback_is_caller_visible",
    "tests.test_validation_tiers.ValidationTierTests.test_cycle_engine_legacy_fallback_is_caller_visible",
    "tests.test_validation_tiers.ValidationTierTests.test_sessions_legacy_fallback_is_caller_visible",
    "tests.test_validation_tiers.ValidationTierTests.test_recovery_legacy_fallback_is_caller_visible",
    "tests.test_validation_tiers.ValidationTierTests.test_retained_finalizer_legacy_fallback_is_unchanged",
    "tests.test_validation_tiers.ValidationTierTests.test_debt_parser_canonicalizes_verbose_and_summary_subtests",
    "tests.test_validation_tiers.ValidationTierTests.test_debt_parser_fails_closed_on_ambiguity",
    "tests.test_validation_tiers.ValidationTierTests.test_debt_reconciliation_requires_exact_identities_and_outcomes",
    "tests.test_validation_tiers.ValidationTierTests.test_output_tail_is_redacted_without_changing_raw_hash_or_debt",
    "tests.test_validation_tiers.ValidationTierTests.test_release_execution_provenance_binds_clean_commit_tree_and_branch",
    "tests.test_validation_tiers.ValidationTierTests.test_release_execution_provenance_rejects_dirty_or_drifting_worktree",
    "tests.test_validation_tiers.ValidationTierTests.test_prepared_parent_and_synthetic_candidate_debt_maps_are_exact",
    "tests.test_validation_tiers.ValidationTierTests.test_tiered_integration_runtime_has_one_authoritative_command_record",
)

SOURCE_TEST_OVERRIDES = {
    "validation_tiers.py": FOCUSED_VALIDATION_TESTS,
    "cli.py": FOCUSED_VALIDATION_TESTS,
    "cycle_engine.py": (
        "tests.test_validation_tiers.ValidationTierTests.test_cycle_engine_legacy_fallback_is_caller_visible",
    ),
    "feature_result_recovery.py": (
        "tests.test_validation_tiers.ValidationTierTests.test_retained_finalizer_legacy_fallback_is_unchanged",
    ),
    "integration_executor.py": (
        "tests.test_validation_tiers.ValidationTierTests.test_integration_plan_consumes_milestone_tier",
        "tests.test_validation_tiers.ValidationTierTests.test_integration_legacy_fallback_is_caller_visible",
        "tests.test_validation_tiers.ValidationTierTests.test_tiered_integration_runtime_has_one_authoritative_command_record",
    ),
    "sessions.py": (
        "tests.test_validation_tiers.ValidationTierTests.test_sessions_legacy_fallback_is_caller_visible",
    ),
    "recovery.py": (
        "tests.test_validation_tiers.ValidationTierTests.test_recovery_legacy_fallback_is_caller_visible",
    ),
}

EQUIVALENCE_TRIGGER_PATHS = {
    ".factory/project.yaml",
    "src/development_conveyor/validation_tiers.py",
    "src/development_conveyor/cli.py",
    "src/development_conveyor/integration_executor.py",
    "src/development_conveyor/sessions.py",
}


def _argument_arrays(value: Any, *, label: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise ConveyorError(f"{label} must be an array")
    commands: list[list[str]] = []
    for index, command in enumerate(value):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ConveyorError(f"{label}[{index}] must be a non-empty argument array")
        commands.append(list(command))
    return commands


def adapter_commands(
    adapter: dict[str, Any], tier: str
) -> tuple[list[dict[str, Any]], str]:
    """Return one declared tier, or the unchanged legacy flat command set."""

    if tier not in TIERS:
        raise ConveyorError(f"unsupported validation tier: {tier}")
    configured = adapter.get("validation_tiers")
    if configured is not None:
        if not isinstance(configured, dict):
            raise ConveyorError("validation_tiers must be an object")
        missing = [name for name in TIERS if name not in configured]
        if missing:
            raise ConveyorError(
                "validation_tiers requires feature, milestone, and release; "
                f"missing {', '.join(missing)}"
            )
        commands = _argument_arrays(
            configured[tier], label=f"validation_tiers.{tier}"
        )
        if not commands:
            raise ConveyorError(f"validation_tiers.{tier} must not be empty")
        return (
            [
                {"group": f"validation_tiers.{tier}", "argv": command}
                for command in commands
            ],
            "tiered",
        )

    commands = adapter.get("commands")
    if not isinstance(commands, dict):
        raise ConveyorError("legacy adapter commands must be an object")
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for group in LEGACY_GROUPS:
        entries = _argument_arrays(commands.get(group, []), label=f"commands.{group}")
        for entry in entries:
            identity = tuple(entry)
            if identity not in seen:
                seen.add(identity)
                values.append({"group": group, "argv": entry})
    return values, "legacy"


def adapter_command_tuples(
    adapter: dict[str, Any], tier: str
) -> tuple[tuple[str, ...], ...]:
    values, _ = adapter_commands(adapter, tier)
    return tuple(tuple(item["argv"]) for item in values)


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ConveyorError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result


def changed_paths(root: Path, base: str | None = None) -> tuple[str, ...]:
    # An uncommitted feature review compares against its current prepared HEAD.
    # Integration supplies an immutable prepared parent explicitly.
    reference = base or "HEAD"
    paths: set[str] = set()
    if reference and reference != "WORKTREE":
        result = _git(root, "diff", "--name-only", f"{reference}...HEAD", check=False)
        if result.returncode == 0:
            paths.update(line for line in result.stdout.splitlines() if line)
    for arguments in (
        ("diff", "--name-only"),
        ("diff", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        result = _git(root, *arguments, check=False)
        if result.returncode == 0:
            paths.update(line for line in result.stdout.splitlines() if line)
    return tuple(sorted(paths))


def _test_module(path: str) -> str | None:
    if not path.startswith("tests/test_") or not path.endswith(".py"):
        return None
    return path[:-3].replace("/", ".")


def tests_from_spec(root: Path, specification: str | None) -> tuple[str, ...]:
    if not specification:
        return ()
    path = (root / specification).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConveyorError("feature specification escapes the repository") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConveyorError(f"feature specification is unreadable: {specification}") from exc
    selected: set[str] = set()
    for token in re.findall(r"`([^`]+)`", text):
        candidate = token.strip().rstrip(".,:;")
        module = _test_module(candidate)
        if module:
            selected.add(module)
        elif candidate.startswith("tests.") and ".test_" in candidate:
            selected.add(candidate)
    return tuple(sorted(selected))


def _collapse_test_selection(selected: Iterable[str]) -> tuple[str, ...]:
    values = set(selected)
    modules = {
        item
        for item in values
        if item.startswith("tests.test_") and item.count(".") == 1
    }
    return tuple(
        sorted(
            item
            for item in values
            if item in modules
            or not any(item.startswith(module + ".") for module in modules)
        )
    )


def feature_tests(
    root: Path,
    *,
    base: str | None = None,
    specification: str | None = None,
    explicit: Iterable[str] = (),
) -> tuple[str, ...]:
    selected: set[str] = set(FEATURE_CORE_TESTS)
    selected.update(item for item in explicit if item)
    selected.update(tests_from_spec(root, specification))
    for path in changed_paths(root, base):
        if path == "tests/test_validation_tiers.py":
            selected.update(FOCUSED_VALIDATION_TESTS)
        else:
            module = _test_module(path)
            if module:
                selected.add(module)
        if path.startswith("src/development_conveyor/") and path.endswith(".py"):
            filename = Path(path).name
            overrides = SOURCE_TEST_OVERRIDES.get(filename)
            if overrides:
                selected.update(overrides)
            else:
                conventional = root / "tests" / f"test_{Path(path).stem}.py"
                if conventional.is_file():
                    selected.add(f"tests.test_{Path(path).stem}")
    return _collapse_test_selection(selected)


def equivalence_reasons(
    paths: Iterable[str], *, known_nonzero_debt: bool = False
) -> tuple[str, ...]:
    changed = set(paths)
    reasons: list[str] = []
    if any(path.startswith("tests/") for path in changed):
        reasons.append("tests_or_discovery_changed")
    if changed & EQUIVALENCE_TRIGGER_PATHS:
        reasons.append("validation_routing_changed")
    if any(
        path in {".factory/approved-content.yaml", "tests/helpers.py"}
        or path.startswith("config/")
        or "fixture" in path
        for path in changed
    ):
        reasons.append("environment_or_fixture_authority_changed")
    if known_nonzero_debt:
        reasons.append("milestone_suite_has_known_nonzero_debt")
    return tuple(reasons)


_SUMMARY_RESULT = re.compile(
    r"^(?P<outcome>FAIL|ERROR):\s+(?P<display>\S+)\s+"
    r"\((?P<identity>[^()]+)\)(?P<parameters>.*)$"
)
_VERBOSE_RESULT = re.compile(
    r"^(?P<display>\S+)\s+\((?P<identity>[^()]+)\)(?P<parameters>.*?)"
    r"\s+\.\.\.\s+(?P<outcome>FAIL|ERROR)$"
)
_VERBOSE_PREFIX = re.compile(
    r"^(?P<display>\S+)\s+\((?P<identity>[^()]+)\)(?P<parameters>.*?)"
    r"\s+\.\.\.(?P<tail>.*)$"
)
_VERBOSE_TERMINAL = re.compile(
    r"^(?:ok|FAIL|ERROR|expected failure|unexpected success|skipped(?:\s+.*)?)$"
)
_BRACKET_PARAMETERS = re.compile(r"(?:\s*(\[[^\]\r\n]+\]))")
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _split_subtest_parameters(value: str) -> list[str]:
    parameters: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            continue
        if character == "," and quote is None:
            parameters.append(value[start:index].strip())
            start = index + 1
    if quote is not None or escaped:
        raise ConveyorError("unparseable unittest subtest parameters")
    parameters.append(value[start:].strip())
    if not parameters or any(not item for item in parameters):
        raise ConveyorError("unparseable unittest subtest parameters")
    return parameters


def _canonical_subtest_parameters(value: str) -> str:
    canonical: list[str] = []
    for parameter in _split_subtest_parameters(value):
        name, separator, raw_value = parameter.partition("=")
        name = name.strip()
        raw_value = raw_value.strip()
        if (
            separator != "="
            or not _PARAMETER_NAME.fullmatch(name)
            or not raw_value
        ):
            raise ConveyorError("unparseable unittest subtest parameters")
        if raw_value[:1] in {"'", '"'}:
            if raw_value[-1:] != raw_value[:1]:
                raise ConveyorError("unparseable unittest subtest parameters")
            try:
                decoded = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError) as exc:
                raise ConveyorError(
                    "unparseable unittest subtest parameters"
                ) from exc
            if not isinstance(decoded, str) or any(
                character in decoded for character in "\r\n,[]"
            ):
                raise ConveyorError("ambiguous unittest subtest parameter value")
            rendered = decoded
        else:
            if any(character in raw_value for character in "\r\n,[]"):
                raise ConveyorError("ambiguous unittest subtest parameter value")
            rendered = raw_value
        canonical.append(f"{name}={rendered}")
    return "[" + ",".join(canonical) + "]"


def _canonical_result_identity(identity: str, parameters: str) -> str:
    identity = identity.strip()
    if (
        not identity
        or identity.startswith(".")
        or identity.endswith(".")
        or any(character.isspace() for character in identity)
    ):
        raise ConveyorError("unparseable unittest failure/error result heading")
    suffix = parameters.strip()
    if not suffix:
        return identity
    if suffix.startswith("["):
        groups = [match.group(1) for match in _BRACKET_PARAMETERS.finditer(suffix)]
        consumed = "".join(match.group(0) for match in _BRACKET_PARAMETERS.finditer(suffix))
        if not groups or consumed.strip() != suffix:
            raise ConveyorError("unparseable unittest failure/error result heading")
        return identity + "".join(groups)
    if suffix.startswith("(") and suffix.endswith(")"):
        return identity + _canonical_subtest_parameters(suffix[1:-1].strip())
    raise ConveyorError("unparseable unittest failure/error result heading")


def _parsed_result(
    match: re.Match[str], *, summary: bool
) -> dict[str, str]:
    outcome = match.group("outcome").lower()
    identity = match.group("identity")
    display = match.group("display")
    if not identity.endswith("." + display):
        identity += "." + display
    return {
        "id": _canonical_result_identity(
            identity, match.group("parameters")
        ),
        "outcome": "failure" if outcome == "fail" else outcome,
    }


def _incomplete_verbose_identity(line: str) -> str | None:
    match = _VERBOSE_PREFIX.fullmatch(line)
    if match is None:
        return None
    tail = match.group("tail").strip()
    if _VERBOSE_TERMINAL.fullmatch(tail):
        return None
    if tail:
        following = _VERBOSE_PREFIX.fullmatch(tail)
        if following is None or not _VERBOSE_TERMINAL.fullmatch(
            following.group("tail").strip()
        ):
            return None
    identity = match.group("identity")
    display = match.group("display")
    if not identity.endswith("." + display):
        identity += "." + display
    return _canonical_result_identity(identity, match.group("parameters"))


def _subtest_parent_identity(identity: str) -> str | None:
    marker = identity.find("[")
    if marker <= 0:
        return None
    suffix = identity[marker:]
    consumed = "".join(
        match.group(0) for match in _BRACKET_PARAMETERS.finditer(suffix)
    )
    if not consumed or consumed != suffix:
        return None
    return identity[:marker]


def _validate_unique_debt_records(
    records: list[dict[str, str]], *, source: str
) -> None:
    identities: set[str] = set()
    for record in records:
        identity = record["id"]
        if identity in identities:
            raise ConveyorError(
                f"duplicate normalized unittest debt identity in {source}: "
                f"{identity}"
            )
        identities.add(identity)


def _debt_records(output: str) -> list[dict[str, str]]:
    summary: list[dict[str, str]] = []
    verbose: list[dict[str, str]] = []
    incomplete_verbose: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        is_summary = stripped.startswith(("FAIL:", "ERROR:"))
        is_verbose = bool(re.search(r"\.\.\.\s+(?:FAIL|ERROR)$", stripped))
        if not is_summary and not is_verbose:
            incomplete = _incomplete_verbose_identity(stripped)
            if incomplete is not None:
                incomplete_verbose.add(incomplete)
            continue
        match = (
            _SUMMARY_RESULT.fullmatch(stripped)
            if is_summary
            else _VERBOSE_RESULT.fullmatch(stripped)
        )
        if match is None:
            raise ConveyorError(
                "unparseable unittest failure/error result heading"
            )
        (summary if is_summary else verbose).append(
            _parsed_result(match, summary=is_summary)
        )
    _validate_unique_debt_records(summary, source="summary headings")
    _validate_unique_debt_records(verbose, source="verbose results")
    if summary and verbose:
        summary_map = {
            record["id"]: record["outcome"] for record in summary
        }
        verbose_map = {
            record["id"]: record["outcome"] for record in verbose
        }
        if summary_map != verbose_map:
            verbose_only = set(verbose_map) - set(summary_map)
            classification_conflicts = {
                identity
                for identity in set(summary_map) & set(verbose_map)
                if summary_map[identity] != verbose_map[identity]
            }
            summary_only = set(summary_map) - set(verbose_map)
            summary_only_parents = {
                identity: _subtest_parent_identity(identity)
                for identity in summary_only
            }
            python39_subtests = (
                bool(summary_only)
                and not verbose_only
                and not classification_conflicts
                and all(
                    parent is not None and parent in incomplete_verbose
                    for parent in summary_only_parents.values()
                )
            )
            if not python39_subtests:
                raise ConveyorError(
                    "unittest verbose and summary failure/error maps differ"
                )
    return summary if summary else verbose


def _output_record(
    *,
    group: str,
    argv: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    duration: float,
) -> dict[str, Any]:
    combined = (stdout + "\n" + stderr).strip()
    count_match = re.search(r"Ran (\d+) tests?", combined)
    debt = _debt_records(combined)
    output_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    redacted_tail = redact_text(combined)[-2000:]
    return {
        "group": group,
        "argv": argv,
        "exit_status": returncode,
        "duration_seconds": round(duration, 3),
        "tests_run": int(count_match.group(1)) if count_match else None,
        "output_sha256": output_hash,
        "output_tail": redacted_tail,
        "debt": debt,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(redact_value(value), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


def _exclusive_raw_descriptor(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fsync(descriptor)
    _fsync_directory(path.parent)
    return descriptor


def _update_release_metadata(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)
    _fsync_directory(path.parent)


def _release_execution_paths(
    root: Path, execution_id: str
) -> tuple[Path, Path, Path]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    prefix = f"release-validation-{execution_id}"
    return (
        reports / f"{prefix}.stdout.raw",
        reports / f"{prefix}.stderr.raw",
        reports / f"{prefix}.execution.json",
    )


def _repository_provenance(root: Path) -> dict[str, Any]:
    """Capture immutable Git identity and cleanliness at one release boundary."""

    canonical_root = root.resolve()
    branch = _git(
        canonical_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    )
    commit = _git(canonical_root, "rev-parse", "HEAD", check=False)
    tree = _git(canonical_root, "rev-parse", "HEAD^{tree}", check=False)
    paths = changed_paths(canonical_root)
    return {
        "repository_path": str(canonical_root),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "tree": tree.stdout.strip() if tree.returncode == 0 else None,
        "worktree_clean": not paths,
        "changed_paths": list(paths),
        "observed_at": utc_now(),
    }


def _release_provenance_valid(
    execution: dict[str, Any], *, repository: Path
) -> bool:
    starting = execution.get("starting_provenance")
    final = execution.get("final_provenance")
    execution_id = execution.get("execution_id")
    if not isinstance(starting, dict) or not isinstance(final, dict):
        return False
    canonical = str(repository.resolve())
    return (
        isinstance(execution_id, str)
        and bool(execution_id)
        and isinstance(execution.get("created_at"), str)
        and bool(execution["created_at"])
        and isinstance(execution.get("completed_at"), str)
        and bool(execution["completed_at"])
        and starting.get("repository_path") == canonical
        and final.get("repository_path") == canonical
        and isinstance(starting.get("branch"), str)
        and bool(starting["branch"])
        and starting.get("branch") == final.get("branch")
        and isinstance(starting.get("commit"), str)
        and bool(starting["commit"])
        and starting.get("commit") == final.get("commit")
        and isinstance(starting.get("tree"), str)
        and bool(starting["tree"])
        and starting.get("tree") == final.get("tree")
        and starting.get("worktree_clean") is True
        and final.get("worktree_clean") is True
        and starting.get("changed_paths") == []
        and final.get("changed_paths") == []
    )


def _run_release_command_with_artifacts(
    root: Path, group: str, argv: list[str], *, timeout: int | None
) -> dict[str, Any]:
    execution_id = uuid.uuid4().hex
    starting_provenance = _repository_provenance(root)
    stdout_path, stderr_path, metadata_path = _release_execution_paths(
        root, execution_id
    )
    relative_stdout = stdout_path.relative_to(root).as_posix()
    relative_stderr = stderr_path.relative_to(root).as_posix()
    relative_metadata = metadata_path.relative_to(root).as_posix()
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": execution_id,
        "group": group,
        "raw_stdout_path": relative_stdout,
        "raw_stderr_path": relative_stderr,
        "metadata_path": relative_metadata,
        "child_exit_code": None,
        "stdout_sha256": None,
        "stderr_sha256": None,
        "completion_state": "prepared",
        "parser_status": "not_started",
        "created_at": utc_now(),
        "starting_provenance": starting_provenance,
        "final_provenance": None,
        "provenance_valid": False,
        "child_completed_at": None,
        "parser_completed_at": None,
        "completed_at": None,
    }
    _exclusive_write_json(metadata_path, metadata)
    stdout_descriptor = _exclusive_raw_descriptor(stdout_path)
    try:
        stderr_descriptor = _exclusive_raw_descriptor(stderr_path)
    except Exception:
        os.close(stdout_descriptor)
        raise

    started = time.monotonic()
    timed_out = False
    child_exit_code: int | None = None
    record_exit_status = 124
    command_error: BaseException | None = None
    try:
        with os.fdopen(stdout_descriptor, "wb") as stdout_handle, os.fdopen(
            stderr_descriptor, "wb"
        ) as stderr_handle:
            try:
                result = subprocess.run(
                    argv,
                    cwd=root,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    check=False,
                    timeout=timeout,
                )
                child_exit_code = result.returncode
                record_exit_status = result.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
            except BaseException as exc:
                command_error = exc
            finally:
                stdout_handle.flush()
                os.fsync(stdout_handle.fileno())
                stderr_handle.flush()
                os.fsync(stderr_handle.fileno())
    finally:
        duration = time.monotonic() - started

    stdout_bytes = stdout_path.read_bytes()
    stderr_bytes = stderr_path.read_bytes()
    final_provenance = _repository_provenance(root)
    metadata.update(
        {
            "child_exit_code": child_exit_code,
            "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            "completion_state": (
                "child_timed_out" if timed_out else "child_completed"
            ),
            "parser_status": "pending",
            "child_completed_at": utc_now(),
            "final_provenance": final_provenance,
            "completed_at": utc_now(),
        }
    )
    metadata["provenance_valid"] = _release_provenance_valid(
        metadata, repository=root
    )
    _update_release_metadata(metadata_path, metadata)
    if command_error is not None:
        metadata.update(
            {
                "completion_state": "child_failed",
                "parser_status": "not_started",
            }
        )
        _update_release_metadata(metadata_path, metadata)
        raise command_error

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if timed_out:
        stderr += "\nvalidation timeout"
    try:
        record = _output_record(
            group=group,
            argv=argv,
            returncode=record_exit_status,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
        )
    except Exception:
        metadata.update(
            {
                "completion_state": "parser_failed",
                "parser_status": "failed",
                "parser_completed_at": utc_now(),
            }
        )
        _update_release_metadata(metadata_path, metadata)
        raise
    metadata.update(
        {
            "completion_state": "complete",
            "parser_status": "succeeded",
            "parser_completed_at": utc_now(),
        }
    )
    _update_release_metadata(metadata_path, metadata)
    record["release_execution"] = metadata
    return record


def _run_command(
    root: Path,
    group: str,
    argv: list[str],
    *,
    timeout: int | None,
    preserve_release_artifacts: bool = False,
) -> dict[str, Any]:
    SafetyPolicy.validate_configured_command(
        argv, cwd=root, registered_repository=root
    )
    if preserve_release_artifacts:
        return _run_release_command_with_artifacts(
            root, group, argv, timeout=timeout
        )
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
        return _output_record(
            group=group,
            argv=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return _output_record(
            group=group,
            argv=argv,
            returncode=124,
            stdout=str(exc.stdout or ""),
            stderr="validation timeout",
            duration=time.monotonic() - started,
        )


def _repository_commands(
    tier: str, selected_tests: tuple[str, ...], *, repeat: int
) -> list[tuple[str, list[str], int | None]]:
    if tier == "release":
        commands: list[tuple[str, list[str], int | None]] = [
            (
                f"release_complete_suite_{index + 1}",
                ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                None,
            )
            for index in range(repeat)
        ]
    else:
        commands = [
            (
                f"{tier}_tests",
                ["python3", "-m", "unittest", *selected_tests, "-q"],
                480 if tier == "milestone" else 150,
            )
        ]
    commands.extend(
        [
            ("compile", ["python3", "-m", "compileall", "-q", "src", "scripts"], 120),
            ("validate_config", ["scripts/conveyor", "validate-config"], 120),
            ("diff_check", ["git", "diff", "--check"], 120),
        ]
    )
    return commands


def _safe_extract_archive(root: Path, ref: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if archive.returncode != 0:
        raise ConveyorError(archive.stderr.decode("utf-8", errors="replace").strip())
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ConveyorError("git archive contains an unsupported special entry")
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ConveyorError("git archive contains an unsafe path") from exc
        # Python 3.9 has no extraction filter parameter. Every member was
        # validated above against traversal, links, devices, and FIFOs.
        bundle.extractall(destination)


def _copy_worktree(root: Path, destination: Path) -> None:
    listed = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ).stdout.split("\0")
    for relative in sorted(item for item in listed if item):
        source = root / relative
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise ConveyorError("worktree observation contains an unsupported entry")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _available_tests(checkout: Path, tests: tuple[str, ...]) -> tuple[str, ...]:
    available: list[str] = []
    for test in tests:
        parts = test.split(".")
        if len(parts) >= 2 and (checkout / Path(*parts[:2]).with_suffix(".py")).is_file():
            available.append(test)
    return tuple(available)


def _observe_candidate(
    root: Path, ref: str, tests: tuple[str, ...]
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="conveyor-milestone-observation-") as temporary:
        checkout = Path(temporary)
        if ref == "WORKTREE":
            _copy_worktree(root, checkout)
            commit = "WORKTREE:" + hashlib.sha256(
                "\n".join(changed_paths(root)).encode("utf-8")
            ).hexdigest()
        else:
            _safe_extract_archive(root, ref, checkout)
            commit = _git(root, "rev-parse", ref).stdout.strip()
        selected = _available_tests(checkout, tests)
        record = _run_command(
            checkout,
            "milestone_ref_observation",
            ["python3", "-m", "unittest", *selected, "-q"],
            timeout=480,
        )
        return {
            "ref": ref,
            "commit": commit,
            "selected_tests": list(selected),
            "record": record,
        }


def _catalog_records(
    root: Path,
) -> tuple[list[dict[str, Any]], bool]:
    path = root / "docs/testing/KNOWN_TEST_DEBT.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConveyorError("known test debt catalog is unreadable") from exc
    records = catalog.get("records") if isinstance(catalog, dict) else None
    if not isinstance(records, list):
        raise ConveyorError("known test debt catalog records must be an array")
    pending = bool(
        isinstance(catalog, dict)
        and catalog.get("reconciliation_mode") == "single_release_observation_pending"
    )
    return records, pending


def reconcile_known_debt(
    observed: Iterable[dict[str, Any]],
    catalog: Iterable[dict[str, Any]],
    *,
    allow_pending_capture: bool = False,
) -> dict[str, Any]:
    def normalize(
        values: Iterable[dict[str, Any]],
        *,
        pending_allowed: bool,
        catalog_records: bool,
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]], dict[str, str], list[str]]:
        normalized: dict[str, str] = {}
        permitted: dict[str, tuple[str, ...]] = {}
        rationales: dict[str, str] = {}
        errors: list[str] = []
        for index, item in enumerate(values):
            identity = item.get("id") if isinstance(item, dict) else None
            outcome = item.get("outcome") if isinstance(item, dict) else None
            if not isinstance(identity, str) or not identity:
                errors.append(f"record {index} has invalid id")
                continue
            if identity in normalized:
                errors.append(f"duplicate identity: {identity}")
                continue
            if outcome not in DEBT_OUTCOMES:
                if pending_allowed and outcome == "pending_observation":
                    outcome = "pending_observation"
                else:
                    errors.append(f"{identity} has invalid outcome")
                    continue
            normalized[identity] = outcome
            if not catalog_records:
                continue
            declared = item.get("permitted_candidate_outcomes")
            rationale = item.get("rationale")
            if declared is None and rationale is None:
                permitted[identity] = (outcome,)
                continue
            if (
                not isinstance(declared, list)
                or not declared
                or any(value not in DEBT_OUTCOMES for value in declared)
                or len(set(declared)) != len(declared)
                or outcome not in declared
            ):
                errors.append(
                    f"{identity} has invalid permitted candidate outcomes"
                )
                permitted[identity] = (outcome,)
                continue
            if not isinstance(rationale, str) or not rationale.strip():
                errors.append(
                    f"{identity} lacks a rationale for permitted candidate outcomes"
                )
                permitted[identity] = (outcome,)
                continue
            permitted[identity] = tuple(declared)
            rationales[identity] = rationale.strip()
        return normalized, permitted, rationales, errors

    observed_map, _, _, observed_errors = normalize(
        observed, pending_allowed=False, catalog_records=False
    )
    catalog_map, permitted_map, rationale_map, catalog_errors = normalize(
        catalog,
        pending_allowed=allow_pending_capture,
        catalog_records=True,
    )
    missing = sorted(set(catalog_map) - set(observed_map))
    unexpected = sorted(set(observed_map) - set(catalog_map))
    outcome_mismatches = sorted(
        identity
        for identity in set(observed_map) & set(catalog_map)
        if observed_map[identity]
        not in {
            *permitted_map.get(identity, (catalog_map[identity],)),
            "pending_observation",
        }
    )
    permitted_outcome_shifts = [
        {
            "id": identity,
            "baseline_outcome": catalog_map[identity],
            "candidate_outcome": observed_map[identity],
            "permitted_candidate_outcomes": list(permitted_map[identity]),
            "rationale": rationale_map[identity],
        }
        for identity in sorted(set(observed_map) & set(catalog_map))
        if observed_map[identity] != catalog_map[identity]
        and observed_map[identity] in permitted_map.get(identity, ())
        and identity in rationale_map
    ]
    pending = sorted(
        identity
        for identity, outcome in catalog_map.items()
        if outcome == "pending_observation"
    )
    counts = {
        outcome: sum(value == outcome for value in observed_map.values())
        for outcome in DEBT_OUTCOMES
    }
    catalog_counts = {
        outcome: sum(value == outcome for value in catalog_map.values())
        for outcome in DEBT_OUTCOMES
    }
    exact = not (
        observed_errors
        or catalog_errors
        or missing
        or unexpected
        or outcome_mismatches
        or pending
    )
    capture_complete = exact
    return {
        "exact": exact,
        "capture_complete": capture_complete,
        "observed_count": len(observed_map),
        "catalog_count": len(catalog_map),
        "outcome_counts": counts,
        "catalog_outcome_counts": catalog_counts,
        "classification_exact_count": sum(
            observed_map.get(identity) == outcome
            for identity, outcome in catalog_map.items()
        ),
        "permitted_outcome_shifts": permitted_outcome_shifts,
        "missing_identities": missing,
        "unexpected_identities": unexpected,
        "outcome_mismatches": outcome_mismatches,
        "pending_identities": pending,
        "validation_errors": [*observed_errors, *catalog_errors],
        "observed_records": [
            {"id": identity, "outcome": observed_map[identity]}
            for identity in sorted(observed_map)
        ],
    }


def _release_commands_valid(
    records: Iterable[dict[str, Any]],
    reconciliation: dict[str, Any],
    *,
    repository: Path,
) -> bool:
    records = list(records)
    complete_suite = [
        record
        for record in records
        if str(record.get("group", "")).startswith("release_complete_suite")
    ]
    supporting = [record for record in records if record not in complete_suite]
    return bool(complete_suite) and bool(reconciliation.get("exact")) and all(
        record.get("exit_status") == 0 for record in supporting
    ) and all(
        record.get("exit_status") in {0, 1}
        and isinstance(record.get("release_execution"), dict)
        and record["release_execution"].get("completion_state") == "complete"
        and record["release_execution"].get("parser_status") == "succeeded"
        and record["release_execution"].get("child_exit_code")
        == record.get("exit_status")
        and record["release_execution"].get("provenance_valid") is True
        and _release_provenance_valid(
            record["release_execution"], repository=repository
        )
        for record in complete_suite
    )


def reconcile_release_artifacts(
    root: Path,
    *,
    release_json_path: Path,
    execution_metadata_path: Path,
    raw_stdout_path: Path,
    raw_stderr_path: Path,
    output_path: Path,
    implementation_commit: str,
) -> dict[str, Any]:
    """Reconcile one preserved release execution without executing commands."""

    root = root.resolve()

    def read_object(path: Path, *, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConveyorError(f"{label} is unreadable") from exc
        if not isinstance(value, dict):
            raise ConveyorError(f"{label} must be an object")
        return value

    release_document = read_object(
        release_json_path, label="release validation JSON"
    )
    execution = read_object(
        execution_metadata_path, label="release execution metadata"
    )
    execution_id = execution.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ConveyorError("release execution metadata lacks an execution ID")
    if (
        execution.get("completion_state") != "complete"
        or execution.get("parser_status") != "succeeded"
        or execution.get("child_exit_code") != 1
    ):
        raise ConveyorError("release execution metadata is not a completed debt run")

    expected_stdout = root / str(execution.get("raw_stdout_path") or "")
    expected_stderr = root / str(execution.get("raw_stderr_path") or "")
    expected_metadata = root / str(execution.get("metadata_path") or "")
    if (
        raw_stdout_path.resolve() != expected_stdout.resolve()
        or raw_stderr_path.resolve() != expected_stderr.resolve()
        or execution_metadata_path.resolve() != expected_metadata.resolve()
    ):
        raise ConveyorError("release artifact paths do not match execution metadata")

    stdout_bytes = raw_stdout_path.read_bytes()
    stderr_bytes = raw_stderr_path.read_bytes()
    stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
    stderr_sha256 = hashlib.sha256(stderr_bytes).hexdigest()
    if (
        execution.get("stdout_sha256") != stdout_sha256
        or execution.get("stderr_sha256") != stderr_sha256
    ):
        raise ConveyorError("release raw artifact hash does not match metadata")

    commands = release_document.get("commands")
    release_commands = [
        item
        for item in commands
        if isinstance(item, dict)
        and isinstance(item.get("release_execution"), dict)
        and item["release_execution"].get("execution_id") == execution_id
    ] if isinstance(commands, list) else []
    if len(release_commands) != 1:
        raise ConveyorError("release JSON does not bind exactly one execution")
    release_command = release_commands[0]
    if release_command.get("release_execution") != execution:
        raise ConveyorError("release JSON execution metadata differs from the raw metadata")
    release_provenance = release_document.get("release_provenance")
    if (
        not isinstance(release_provenance, dict)
        or release_provenance.get("execution_ids") != [execution_id]
        or release_provenance.get("executions") != [execution]
        or release_provenance.get("valid") is not True
        or execution.get("provenance_valid") is not True
        or not _release_provenance_valid(execution, repository=root)
    ):
        raise ConveyorError(
            "release JSON provenance does not bind the exact release execution"
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    combined = (stdout + "\n" + stderr).strip()
    combined_sha256 = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    observed = _debt_records(combined)
    count_match = re.search(r"Ran (\d+) tests?", combined)
    tests_run = int(count_match.group(1)) if count_match else None
    if (
        release_command.get("output_sha256") != combined_sha256
        or release_command.get("debt") != observed
        or release_command.get("tests_run") != tests_run
        or release_document.get("test_count") != tests_run
        or release_document.get("complete_suite_invocations") != 1
    ):
        raise ConveyorError("release JSON does not match preserved raw output")

    catalog_path = root / "docs/testing/KNOWN_TEST_DEBT.json"
    catalog_document = read_object(catalog_path, label="known test debt catalog")
    catalog = catalog_document.get("records")
    if not isinstance(catalog, list):
        raise ConveyorError("known test debt catalog records must be an array")
    reconciliation = reconcile_known_debt(observed, catalog)
    original_reconciliation = release_document.get("debt_reconciliation")
    if not isinstance(original_reconciliation, dict):
        raise ConveyorError("release JSON lacks debt reconciliation")
    if (
        original_reconciliation.get("observed_records")
        != reconciliation["observed_records"]
        or original_reconciliation.get("missing_identities")
        != reconciliation["missing_identities"]
        or original_reconciliation.get("unexpected_identities")
        != reconciliation["unexpected_identities"]
    ):
        raise ConveyorError("release JSON debt identities differ from raw output")
    if not reconciliation["exact"]:
        raise ConveyorError("release debt reconciliation remains invalid")

    commit_result = subprocess.run(
        ["git", "rev-parse", f"{implementation_commit}^{{commit}}"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    tree_result = subprocess.run(
        ["git", "rev-parse", f"{implementation_commit}^{{tree}}"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    resolved_commit = commit_result.stdout.strip()
    implementation_tree = tree_result.stdout.strip()
    if (
        commit_result.returncode != 0
        or tree_result.returncode != 0
        or resolved_commit != implementation_commit
        or not implementation_tree
    ):
        raise ConveyorError("implementation commit or tree cannot be authenticated")

    result = {
        "schema_version": 1,
        "kind": "offline_release_debt_reconciliation",
        "created_at": utc_now(),
        "release_execution_id": execution_id,
        "implementation": {
            "commit": resolved_commit,
            "tree": implementation_tree,
        },
        "bindings": {
            "raw_stdout_sha256": stdout_sha256,
            "raw_stderr_sha256": stderr_sha256,
            "combined_output_sha256": combined_sha256,
            "external_release_json_sha256": hashlib.sha256(
                release_json_path.read_bytes()
            ).hexdigest(),
            "debt_catalog_sha256": hashlib.sha256(
                catalog_path.read_bytes()
            ).hexdigest(),
            "reconciliation_code_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        },
        "release_observation": {
            "tests_run": tests_run,
            "candidate_failures": reconciliation["outcome_counts"]["failure"],
            "candidate_errors": reconciliation["outcome_counts"]["error"],
            "baseline_failures": reconciliation["catalog_outcome_counts"]["failure"],
            "baseline_errors": reconciliation["catalog_outcome_counts"]["error"],
        },
        "reconciliation": reconciliation,
        "reconciliation_valid": True,
        "valid": _release_commands_valid(
            commands, reconciliation, repository=root
        ),
    }
    _exclusive_write_json(output_path, result)
    return result


def run_repository_validation(
    root: Path,
    *,
    tier: str,
    base: str | None = None,
    specification: str | None = None,
    explicit_tests: Iterable[str] = (),
    prepared_parent: str | None = None,
    candidate: str | None = None,
    repeat: int = 1,
) -> dict[str, Any]:
    root = root.resolve()
    if tier not in TIERS:
        raise ConveyorError(f"unsupported validation tier: {tier}")
    if repeat < 1 or (tier != "release" and repeat != 1):
        raise ConveyorError("repeat is available only for explicit release validation")
    prepared_parent = prepared_parent or os.environ.get("CONVEYOR_PREPARED_PARENT")
    candidate = candidate or os.environ.get("CONVEYOR_CANDIDATE")
    effective_base = base or prepared_parent
    feature_selected = feature_tests(
        root,
        base=effective_base,
        specification=specification,
        explicit=explicit_tests,
    )
    selected = (
        _collapse_test_selection((*feature_selected, *MILESTONE_TESTS))
        if tier == "milestone"
        else feature_selected
    )
    paths = changed_paths(root, effective_base)
    reasons = equivalence_reasons(paths)
    if bool(prepared_parent) != bool(candidate):
        raise ConveyorError("prepared-parent and candidate must be supplied together")
    comparison: dict[str, Any] = {
        "required": tier == "milestone" and bool(reasons),
        "reasons": list(reasons),
        "performed": False,
        "observations_per_ref": 0,
    }
    if tier == "milestone" and prepared_parent and candidate:
        observation_tests = _collapse_test_selection(
            (*FEATURE_CORE_TESTS, *MILESTONE_TESTS)
        )
        observations = [
            _observe_candidate(root, prepared_parent, observation_tests),
            _observe_candidate(root, candidate, observation_tests),
        ]
        comparison.update(
            {
                "performed": True,
                "observations_per_ref": 1,
                "observations": observations,
                "equivalent_success": all(
                    item["record"]["exit_status"] == 0 for item in observations
                ),
            }
        )
    records = [
        _run_command(
            root,
            group,
            argv,
            timeout=timeout,
            preserve_release_artifacts=(
                tier == "release"
                and group.startswith("release_complete_suite")
            ),
        )
        for group, argv, timeout in _repository_commands(
            tier, selected, repeat=repeat
        )
    ]
    valid = all(record["exit_status"] == 0 for record in records)
    if comparison.get("performed"):
        valid = valid and comparison.get("equivalent_success") is True
    elif comparison["required"]:
        valid = False
    debt_reconciliation = None
    if tier == "release":
        catalog, pending = _catalog_records(root)
        observed = [
            item
            for record in records
            for item in record.get("debt", [])
        ]
        debt_reconciliation = reconcile_known_debt(
            observed, catalog, allow_pending_capture=pending
        )
        valid = _release_commands_valid(
            records, debt_reconciliation, repository=root
        )
    observation_records = [
        item["record"]
        for item in comparison.get("observations", [])
        if isinstance(item, dict) and isinstance(item.get("record"), dict)
    ]
    counted_records = [*records, *observation_records]
    release_executions = [
        record["release_execution"]
        for record in records
        if isinstance(record.get("release_execution"), dict)
    ]
    return {
        "schema_version": 1,
        "tier": tier,
        "valid": valid,
        "changed_paths": list(paths),
        "selected_tests": list(selected) if tier != "release" else [],
        "fixed_core_tests": list(FEATURE_CORE_TESTS),
        "feature_gate_included": tier in {"feature", "milestone"},
        "complete_suite_invocations": repeat if tier == "release" else 0,
        "nondeterminism_investigation_requested": tier == "release" and repeat > 1,
        "comparison": comparison,
        "commands": records,
        "release_provenance": (
            {
                "execution_ids": [
                    execution["execution_id"] for execution in release_executions
                ],
                "executions": release_executions,
                "valid": bool(release_executions)
                and all(
                    execution.get("provenance_valid") is True
                    and _release_provenance_valid(execution, repository=root)
                    for execution in release_executions
                ),
            }
            if tier == "release"
            else None
        ),
        "command_count": len(records),
        "test_count": sum(
            int(record.get("tests_run") or 0)
            for record in counted_records
            if str(record.get("group", "")).endswith("_tests")
            or str(record.get("group", "")).startswith("release_complete_suite")
            or record.get("group") == "milestone_ref_observation"
        ),
        "duration_seconds": round(
            sum(float(record.get("duration_seconds") or 0) for record in counted_records),
            3,
        ),
        "debt_reconciliation": debt_reconciliation,
    }
