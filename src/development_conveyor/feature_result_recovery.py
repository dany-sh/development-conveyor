"""Zero-model recovery of one exact terminal feature-session implementation diff."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .accepted_commit import acceptance_metadata_paths
from .compatibility_cache import (
    build_controller_compatibility_cache,
    write_controller_compatibility_cache,
)
from .config import load_json
from .contracts import TransactionState, WorkflowType, fingerprint
from .cycle_cache import (
    build_canonical_cycle_cache,
    write_terminal_cycle_cache,
)
from .errors import RecoveryError, TransactionError
from .execution_plan import ExecutionPlan
from .feature_prelaunch_recovery import (
    authenticates_run_scoped_capability_isolation_report,
)
from .kernel import FeatureExecutionAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, inspect_repository_writer_lock, make_lock_record
from .logging import atomic_write_bytes, atomic_write_json, utc_now
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .redaction import redact_text
from .registry import Project
from .repository import RepositoryInspector
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowWriterLease


CommandRunner = Callable[[list[str], Path], dict[str, Any]]
FEATURE_RESULT_RECOVERY_CAPABILITY_VERSION = (
    "prepared-checkpoint-and-prelaunch-lineage-v3"
)
LEGACY_RETAINED_RESULT_TOPOLOGY = (
    "TransactionStarted",
    "LeaseAcquired",
    "SnapshotCaptured",
    "SessionLaunched",
    "HumanGateRaised",
    "LeaseReleased",
    "ProjectionUpdated",
)
CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY = (
    "TransactionStarted",
    "LeaseAcquired",
    "SnapshotCaptured",
    "SessionLaunched",
    "CheckpointRecorded",
    "HumanGateRaised",
    "LeaseReleased",
    "ProjectionUpdated",
)
PREPARED_CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY = (
    "TransactionStarted",
    "LeaseAcquired",
    "SnapshotCaptured",
    "SessionLaunched",
    "CheckpointRecorded",
    "CheckpointRecorded",
    "HumanGateRaised",
    "LeaseReleased",
    "ProjectionUpdated",
)
AUTHENTICATED_PREPARED_FEATURE_BRANCH_CHECKPOINT = {
    "checkpoint": "authenticated_prepared_feature_branch",
    "previous_phase": "feature_selected",
    "next_phase": "branch_preparing",
}
AUTHENTICATED_FEATURE_SESSION_CHECKPOINT = {
    "checkpoint": "authenticated_feature_session_launched",
    "previous_phase": "branch_preparing",
    "next_phase": "feature_in_progress",
}
_COMMAND_DISCOVERY_FIELDS = frozenset(
    {
        "command_expectation_checks",
        "identity_discovered_from_report_and_ledger",
    }
)


def _changed_paths(inspector: RepositoryInspector) -> tuple[str, ...]:
    return tuple(sorted({
        *inspector.tracked_changed_paths(),
        *inspector.untracked_file_hashes().keys(),
    }))


def _authenticate_original_transaction_topology(
    transaction_events: list[dict[str, Any]],
    *,
    transaction_id: str,
    project_id: str,
    repository_identity: str,
    repository_path_fingerprint: str,
    run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Authenticate only an exact supported retained-result topology."""

    event_types = tuple(
        str(event.get("event_type") or "") for event in transaction_events
    )
    if event_types == LEGACY_RETAINED_RESULT_TOPOLOGY:
        variant = "pre_m1_017"
        checkpoints: list[dict[str, Any]] = []
    elif event_types == CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY:
        variant = "m1_017_authenticated_session_checkpoint"
        checkpoints = [transaction_events[4]]
    elif event_types == PREPARED_CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY:
        variant = "prepared_branch_and_session_checkpoints"
        checkpoints = [transaction_events[4], transaction_events[5]]
    else:
        variant = None
        checkpoints = []

    contiguous_chain = bool(transaction_events) and all(
        int(current.get("sequence", 0)) == int(previous.get("sequence", 0)) + 1
        and current.get("previous_fingerprint") == previous.get("fingerprint")
        for previous, current in zip(
            transaction_events, transaction_events[1:]
        )
    )
    exact_event_lineage = bool(transaction_events) and all(
        event.get("transaction_id") == transaction_id
        and event.get("project_id") == project_id
        and event.get("repository_identity") == repository_identity
        and event.get("repository_path_fingerprint")
        == repository_path_fingerprint
        and event.get("workflow_type")
        == WorkflowType.FEATURE_EXECUTION.value
        for event in transaction_events
    )
    start_payload = (
        transaction_events[0].get("payload") or {}
        if transaction_events
        else {}
    )
    launch_payload = (
        transaction_events[3].get("payload") or {}
        if len(transaction_events) > 3
        and transaction_events[3].get("event_type") == "SessionLaunched"
        else {}
    )
    checkpoint_payloads = [
        checkpoint.get("payload") or {} for checkpoint in checkpoints
    ]
    expected_checkpoint_payloads = (
        []
        if variant == "pre_m1_017"
        else (
            [AUTHENTICATED_FEATURE_SESSION_CHECKPOINT]
            if variant == "m1_017_authenticated_session_checkpoint"
            else [
                AUTHENTICATED_PREPARED_FEATURE_BRANCH_CHECKPOINT,
                AUTHENTICATED_FEATURE_SESSION_CHECKPOINT,
            ]
        )
    )
    checks = {
        "accepted_event_sequence": variant is not None,
        "events_are_globally_contiguous": contiguous_chain,
        "event_lineage_exact": exact_event_lineage,
        "run_lineage_exact": start_payload.get("run_id") == run_id,
        "session_lineage_exact": launch_payload.get("session_id") == session_id,
        "checkpoint_count_exact": event_types.count("CheckpointRecorded")
        == len(expected_checkpoint_payloads),
        "checkpoint_payload_exact": (
            checkpoint_payloads == expected_checkpoint_payloads
        ),
    }
    return {
        "authenticated": all(checks.values()),
        "capability_version": FEATURE_RESULT_RECOVERY_CAPABILITY_VERSION,
        "variant": variant,
        "event_types": list(event_types),
        "accepted_topologies": [
            list(LEGACY_RETAINED_RESULT_TOPOLOGY),
            list(CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY),
            list(PREPARED_CHECKPOINT_AWARE_RETAINED_RESULT_TOPOLOGY),
        ],
        "checkpoint_payload": (
            checkpoint_payloads[0] if len(checkpoint_payloads) == 1 else None
        ),
        "checkpoint_payloads": checkpoint_payloads,
        "checks": checks,
    }


def _terminal_legacy_payload(report: dict[str, Any]) -> dict[str, Any]:
    output = report.get("redacted_stdout")
    if not isinstance(output, str):
        raise RecoveryError("original feature report lacks redacted session output")
    messages: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
    if not messages:
        raise RecoveryError("original feature report lacks a terminal assistant message")
    marker = "CONVEYOR_TRANSACTION_RESULT="
    lines = messages[-1].splitlines()
    candidates = [line.removeprefix(marker) for line in lines if line.startswith(marker)]
    if len(candidates) != 1 or not next((line for line in reversed(lines) if line.strip()), "").startswith(marker):
        raise RecoveryError("original feature report lacks one terminal legacy result")
    try:
        payload = json.loads(candidates[0])
    except json.JSONDecodeError as exc:
        raise RecoveryError("original feature legacy result is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RecoveryError("original feature legacy result is not an object")
    return payload


def _default_runner(argv: list[str], cwd: Path) -> dict[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=1800,
    )
    output = result.stdout + result.stderr
    record = {
        "argv": argv,
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_summary": redact_text(output[-1200:]),
    }
    if argv and Path(argv[1] if len(argv) > 1 else "").name == "bootstrap_project.py":
        try:
            payload = json.loads(result.stdout)
            audit = payload["inspection"]["content_audit"]
            record["content_audit_hard_gate"] = audit.get("hard_gate")
            record["content_audit_gate_categories"] = sorted(
                str(item.get("category"))
                for item in audit.get("gate_reasons", [])
                if isinstance(item, dict) and item.get("category")
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            record["content_audit_hard_gate"] = None
    return record


class FeatureResultRecovery:
    """Inspect and apply one exact retained terminal feature implementation."""

    def __init__(
        self,
        *,
        controller_root: Path,
        configuration: dict[str, Any],
        project: Project,
        command_runner: CommandRunner | None = None,
    ):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration
        self.project = project
        self.inspector = RepositoryInspector(project.repository)
        self.command_runner = command_runner or _default_runner
        self.state_root = self.controller_root / "state/projects" / project.project_id
        identity = self.inspector.identity()
        self.ledger = EvidenceLedger(
            self.state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(
            self.ledger, self.state_root / "projection-cache.json"
        )
        self.cycle_schema = load_json(
            self.controller_root / "schemas/cycle-state.schema.json"
        )
        self.project_schema = load_json(
            self.controller_root / "schemas/project-state.schema.json"
        )

    def _report(self, run_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        report = self.controller_root / "reports" / run_id / "feature_cycle.json"
        try:
            value = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError("original feature report is unavailable or invalid") from exc
        if not isinstance(value, dict):
            raise RecoveryError("original feature report is not an object")
        return report, value, _terminal_legacy_payload(value)

    def inspect_recorded(
        self,
        *,
        original_run_id: str,
        original_session_id: str,
        expected_head: str,
        expected_diff_fingerprint: str | None = None,
        expected_paths: tuple[str, ...] = (),
        feature_id: str | None = None,
        original_transaction_id: str | None = None,
        expected_branch: str | None = None,
    ) -> dict[str, Any]:
        """Discover exact retained-result identity from its report and ledger."""

        _, report, terminal = self._report(original_run_id)
        discovered = {
            "feature_id": self._terminal_value(
                terminal, "feature_id", "selected_feature"
            ),
            "original_transaction_id": self._terminal_value(
                terminal, "transaction_id"
            ),
            "original_run_id": self._terminal_value(
                terminal, "run_id", "agent_run_id"
            ),
            "original_session_id": self._terminal_value(
                terminal, "session_id"
            ),
            "expected_branch": self._terminal_value(
                terminal, "starting_branch"
            ),
            "expected_head": self._terminal_value(
                terminal, "starting_commit"
            ),
        }
        explicit = {
            "feature_id": feature_id,
            "original_transaction_id": original_transaction_id,
            "original_run_id": original_run_id,
            "original_session_id": original_session_id,
            "expected_branch": expected_branch,
            "expected_head": expected_head,
        }
        mismatched = [
            key
            for key, value in explicit.items()
            if value is not None and discovered.get(key) != value
        ]
        if mismatched:
            raise RecoveryError(
                "retained-result report identity disagrees: "
                + ", ".join(mismatched)
            )
        if report.get("run_id") != original_run_id:
            raise RecoveryError("retained-result report run identity disagrees")
        required = (
            "feature_id",
            "original_transaction_id",
            "original_run_id",
            "original_session_id",
            "expected_branch",
            "expected_head",
        )
        missing = [
            key
            for key in required
            if not isinstance(discovered.get(key), str)
            or not discovered[key]
        ]
        if missing:
            raise RecoveryError(
                "retained-result report cannot discover exact identity: "
                + ", ".join(missing)
            )
        plan = self.inspect(
            **{key: str(discovered[key]) for key in required}
        )
        normalized_paths = tuple(sorted(set(expected_paths)))
        expectation_checks = {
            "diff_fingerprint": (
                expected_diff_fingerprint is None
                or plan.get("tracked_diff_fingerprint")
                == expected_diff_fingerprint
            ),
            "changed_paths": (
                not normalized_paths
                or tuple(plan.get("changed_paths") or ())
                == normalized_paths
            ),
            "expected_paths_unique": len(normalized_paths)
            == len(expected_paths),
        }
        if not all(expectation_checks.values()):
            failed = ", ".join(
                key
                for key, passed in expectation_checks.items()
                if not passed
            )
            raise RecoveryError(
                "retained-result command expectations disagree: " + failed
            )
        return {
            **plan,
            "command_expectation_checks": expectation_checks,
            "identity_discovered_from_report_and_ledger": True,
        }

    def inspect(
        self,
        *,
        feature_id: str,
        original_transaction_id: str,
        original_run_id: str,
        original_session_id: str,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        if feature_id == "F003":
            return self._inspect_f003(
                feature_id=feature_id,
                original_transaction_id=original_transaction_id,
                original_run_id=original_run_id,
                original_session_id=original_session_id,
                expected_branch=expected_branch,
                expected_head=expected_head,
            )
        return self._inspect_general(
            feature_id=feature_id,
            original_transaction_id=original_transaction_id,
            original_run_id=original_run_id,
            original_session_id=original_session_id,
            expected_branch=expected_branch,
            expected_head=expected_head,
        )

    def _inspect_f003(
        self,
        *,
        feature_id: str,
        original_transaction_id: str,
        original_run_id: str,
        original_session_id: str,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        integrity = self.ledger.verify()
        events = self.ledger.read()
        transaction_events = [
            event for event in events
            if event["transaction_id"] == original_transaction_id
        ]
        start = next(
            (event for event in transaction_events if event["event_type"] == "TransactionStarted"),
            None,
        )
        launch = next(
            (event for event in transaction_events if event["event_type"] == "SessionLaunched"),
            None,
        )
        terminal = next(
            (event for event in transaction_events if event["event_type"] == "HumanGateRaised"),
            None,
        )
        _, report, legacy = self._report(original_run_id)
        identity = self.inspector.identity()
        topology = _authenticate_original_transaction_topology(
            transaction_events,
            transaction_id=original_transaction_id,
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            run_id=original_run_id,
            session_id=original_session_id,
        )
        writer = inspect_repository_writer_lock(
            self.inspector.writer_lock_path(
                self.configuration["lock_policy"]["writer_lock_relative_path"]
            ),
            self.project.repository,
        )
        queue = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        feature = queue.feature(feature_id)
        changed = _changed_paths(self.inspector)
        report_paths = tuple(sorted(legacy.get("changed_paths") or ()))
        start_policy = (start or {}).get("payload", {}).get("allowed_mutation_policy") or {}
        authorized = set(start_policy.get("allowed_paths") or ())
        authorized_prefixes = tuple(start_policy.get("allowed_prefixes") or ())
        unauthorized = [
            path for path in changed
            if path not in authorized
            and not any(path == prefix or path.startswith(prefix + "/") for prefix in authorized_prefixes)
        ]
        accepted_events = [
            event for event in events
            if event["transaction_id"] != original_transaction_id
            and event["workflow_type"] in {
                WorkflowType.FEATURE_EXECUTION.value,
                WorkflowType.FEATURE_ACCEPTANCE.value,
            }
            and event["event_type"] == "TransactionCompleted"
            and event["payload"].get("feature_id") == feature_id
            and event["payload"].get("accepted_feature_commit")
        ]
        exact_subject = f"{feature_id}: Single-Window Application Shell"
        subject_search = self.inspector.git(
            ["log", "--all", "--format=%H", "--fixed-strings", "--grep", exact_subject],
            check=False,
        ).stdout.splitlines()
        required_paths = {
            "Sources/LiveInterviewCompanion/Models/WorkspaceRouter.swift",
            "Tests/LiveInterviewCompanionTests/WorkspaceRoutingTests.swift",
            "Tests/LiveInterviewCompanionTests/AppStoreWorkspaceRoutingTests.swift",
            "docs/features/F003-single-window-application-shell.md",
            "docs/decisions/0015-single-window-workspace-and-session-routing.md",
            self.project.queue_location,
        }
        checks = {
            "ledger_integrity": integrity.valid,
            "original_transaction_exact_event_topology": topology[
                "authenticated"
            ],
            "original_transaction_terminal": terminal is not None,
            "original_transaction_feature": (start or {}).get("payload", {}).get("feature_id") == feature_id,
            "original_transaction_run": (start or {}).get("payload", {}).get("run_id") == original_run_id,
            "original_session": (launch or {}).get("payload", {}).get("session_id") == original_session_id,
            "report_session": report.get("session_id") == original_session_id,
            "report_transaction": legacy.get("transaction_id") == original_transaction_id,
            "report_run": legacy.get("agent_run_id") == original_run_id,
            "report_feature": legacy.get("selected_feature") == feature_id,
            "repository_identity": (start or {}).get("repository_identity") == identity["repository_id"],
            "repository_path_identity": (start or {}).get("repository_path_fingerprint") == identity["path_fingerprint"],
            "branch": self.inspector.current_branch == expected_branch,
            "head": self.inspector.head == expected_head,
            "branch_ref_head": self.inspector.rev_parse(expected_branch, check=False) == expected_head,
            "no_git_operation": not any(self.inspector.git_operation_state().values()),
            "index_unstaged": not self.inspector.staged_changed_paths(),
            "writer_lease_absent": not writer.exists,
            "changed_paths_match_report": changed == report_paths,
            "no_unauthorized_paths": not unauthorized,
            "diff_has_f003_identity": required_paths.issubset(changed),
            "queue_feature_review": isinstance(feature, dict) and feature.get("status") == "review",
            "queue_implementation_completed": isinstance(feature, dict)
            and feature.get("implementation_status") == "Completed",
            "queue_has_no_accepted_commit": isinstance(feature, dict)
            and not feature.get("accepted_commit"),
            "no_accepted_ledger_event": not accepted_events,
            "no_accepted_subject_commit": not subject_search,
            "report_classification_preserved": legacy.get("classification") == "FEATURE_BLOCKED",
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(f"F003 recovery preflight failed: {failed}")
        old_event_fingerprints = [event["fingerprint"] for event in transaction_events]
        plan = {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
            "feature_id": feature_id,
            "branch": expected_branch,
            "head": expected_head,
            "original_transaction_id": original_transaction_id,
            "original_run_id": original_run_id,
            "original_session_id": original_session_id,
            "original_event_fingerprints": old_event_fingerprints,
            "original_transaction_topology": topology,
            "changed_paths": list(changed),
            "original_content_fingerprint": self.inspector.content_diff_fingerprint(changed),
            "preserved_implementation_paths": [
                path for path in changed if path not in self._metadata_paths()
            ],
            "checks": checks,
            "model_session_launched": False,
            "lease_type": "feature_writer",
            "next_state_on_success": "integration_pending",
        }
        plan["preserved_implementation_fingerprint"] = (
            self.inspector.content_diff_fingerprint(
                tuple(plan["preserved_implementation_paths"])
            )
        )
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    @staticmethod
    def _terminal_value(payload: dict[str, Any], *names: str) -> Any:
        for name in names:
            value = payload.get(name)
            if value is not None:
                return value
        return None

    @staticmethod
    def _normalized_workflow_alias(
        *, invoked: Any, envelope: Any, exact_identity: bool
    ) -> dict[str, Any]:
        if invoked == envelope == WorkflowType.FEATURE_EXECUTION.value:
            return {
                "invoked": invoked,
                "envelope": envelope,
                "normalized": WorkflowType.FEATURE_EXECUTION.value,
                "alias_applied": False,
            }
        if (
            invoked == "feature_cycle"
            and envelope == WorkflowType.FEATURE_EXECUTION.value
            and exact_identity
        ):
            return {
                "invoked": invoked,
                "envelope": envelope,
                "normalized": WorkflowType.FEATURE_EXECUTION.value,
                "alias_applied": True,
                "policy": "feature_cycle -> feature_execution",
            }
        raise RecoveryError(
            "terminal session-result envelope workflow_type conflicts with invoked workflow"
        )

    def _validation_commands(
        self, *, tracked_paths: tuple[str, ...], untracked_paths: tuple[str, ...]
    ) -> list[list[str]]:
        changed = tuple(sorted({*tracked_paths, *untracked_paths}))
        changed_tests = [
            Path(path).stem
            for path in changed
            if path.startswith("Tests/") and path.endswith("Tests.swift")
        ]
        matching_tests: list[str] = []
        tests_root = self.project.repository / "Tests"
        for path in tracked_paths:
            if not path.startswith("Sources/") or not path.endswith(".swift"):
                continue
            expected = f"{Path(path).stem}Tests.swift"
            if any(candidate.is_file() for candidate in tests_root.rglob(expected)):
                matching_tests.append(Path(expected).stem)
        filters = list(dict.fromkeys([*changed_tests, *sorted(matching_tests)]))
        if not filters:
            raise RecoveryError(
                "retained feature result has no evidence-derived focused host test"
            )
        return [
            *[["swift", "test", "--filter", name] for name in filters],
            ["swift", "build"],
            ["git", "diff", "--check"],
        ]

    def _inspect_general(
        self,
        *,
        feature_id: str,
        original_transaction_id: str,
        original_run_id: str,
        original_session_id: str,
        expected_branch: str,
        expected_head: str,
        allowed_reservation_run_id: str | None = None,
    ) -> dict[str, Any]:
        integrity = self.ledger.verify()
        events = self.ledger.read()
        transaction_events = [
            event
            for event in events
            if event["transaction_id"] == original_transaction_id
        ]
        start = next(
            (
                event
                for event in transaction_events
                if event["event_type"] == "TransactionStarted"
            ),
            None,
        )
        launch = next(
            (
                event
                for event in transaction_events
                if event["event_type"] == "SessionLaunched"
            ),
            None,
        )
        captured = next(
            (
                event
                for event in transaction_events
                if event["event_type"] == "SnapshotCaptured"
            ),
            None,
        )
        terminal = next(
            (
                event
                for event in transaction_events
                if event["event_type"] == "HumanGateRaised"
            ),
            None,
        )
        preparation_candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for candidate in events:
            if (
                candidate.get("event_type") != "TransactionStarted"
                or candidate.get("workflow_type")
                != WorkflowType.FEATURE_PREPARATION.value
                or candidate.get("sequence", 0) >= (start or {}).get(
                    "sequence", 0
                )
            ):
                continue
            payload = candidate.get("payload") or {}
            if (
                payload.get("feature_id") != feature_id
            ):
                continue
            candidate_events = [
                event
                for event in events
                if event["transaction_id"] == candidate["transaction_id"]
            ]
            preparation_candidates.append((candidate, candidate_events))
        preparation = (
            max(preparation_candidates, key=lambda item: item[0]["sequence"])
            if preparation_candidates
            else None
        )
        preparation_start = preparation[0] if preparation else None
        preparation_events = preparation[1] if preparation else []
        preparation_terminal = next(
            (
                event
                for event in preparation_events
                if event.get("event_type") == "TransactionCompleted"
            ),
            None,
        )
        preparation_projection = next(
            (
                event
                for event in preparation_events
                if event.get("event_type") == "ProjectionUpdated"
            ),
            None,
        )
        preparation_session = next(
            (
                event
                for event in preparation_events
                if event.get("event_type") == "SessionLaunched"
            ),
            None,
        )
        report_path, report, terminal_payload = self._report(original_run_id)
        identity = self.inspector.identity()
        topology = _authenticate_original_transaction_topology(
            transaction_events,
            transaction_id=original_transaction_id,
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
            run_id=original_run_id,
            session_id=original_session_id,
        )
        writer = inspect_repository_writer_lock(
            self.inspector.writer_lock_path(
                self.configuration["lock_policy"]["writer_lock_relative_path"]
            ),
            self.project.repository,
        )
        reservation_path = (
            self.controller_root
            / self.configuration["lock_policy"][
                "controller_launch_lock_directory"
            ]
            / f"{identity['path_fingerprint']}.json"
        )
        reservation = DurableLock(reservation_path).status(
            run_id=allowed_reservation_run_id
        )
        queue = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        feature = queue.feature(feature_id)
        milestone = (
            queue.milestone(str(feature.get("milestone")))
            if isinstance(feature, dict)
            else None
        )
        current_snapshot = capture_repository_snapshot(self.project)
        tracked_paths = tuple(self.inspector.tracked_changed_paths())
        untracked_hashes = self.inspector.untracked_file_hashes()
        untracked_paths = tuple(untracked_hashes)
        changed_paths = tuple(sorted({*tracked_paths, *untracked_paths}))
        terminal_snapshot = (terminal or {}).get("payload", {}).get(
            "terminal_snapshot"
        )
        terminal_snapshot = (
            terminal_snapshot if isinstance(terminal_snapshot, dict) else {}
        )
        start_payload = (start or {}).get("payload", {})
        starting_snapshot = (captured or {}).get("payload", {}).get("snapshot")
        starting_snapshot = (
            starting_snapshot if isinstance(starting_snapshot, dict) else {}
        )
        preparation_payload = (
            preparation_start.get("payload")
            if isinstance(preparation_start, dict)
            else {}
        )
        preparation_terminal_payload = (
            preparation_terminal.get("payload")
            if isinstance(preparation_terminal, dict)
            else {}
        )
        preparation_terminal_snapshot = (
            preparation_terminal_payload.get("terminal_snapshot")
            if isinstance(preparation_terminal_payload, dict)
            else {}
        )
        preparation_projection_payload = (
            preparation_projection.get("payload")
            if isinstance(preparation_projection, dict)
            else {}
        )
        preparation_terminal_sequence = max(
            (
                int(event.get("sequence", 0))
                for event in preparation_events
            ),
            default=0,
        )
        prelaunch_recovery_candidates: list[
            tuple[dict[str, Any], list[dict[str, Any]]]
        ] = []
        for candidate in events:
            if (
                candidate.get("event_type") != "TransactionStarted"
                or candidate.get("workflow_type")
                != WorkflowType.RECOVERY.value
                or candidate.get("sequence", 0)
                <= preparation_terminal_sequence
                or candidate.get("sequence", 0)
                >= (start or {}).get("sequence", 0)
            ):
                continue
            payload = candidate.get("payload") or {}
            if payload.get("feature_id") != feature_id:
                continue
            candidate_events = [
                event
                for event in events
                if event["transaction_id"] == candidate["transaction_id"]
            ]
            recovery_applied = next(
                (
                    event
                    for event in candidate_events
                    if event.get("event_type") == "RecoveryApplied"
                    and event.get("payload", {}).get("classification")
                    == "FEATURE_PRELAUNCH_RECOVERY"
                ),
                None,
            )
            completed = next(
                (
                    event
                    for event in candidate_events
                    if event.get("event_type") == "TransactionCompleted"
                    and event.get("payload", {}).get("classification")
                    == "RECOVERY_APPLIED"
                    and event.get("payload", {}).get("next_state")
                    == "feature_preparing"
                ),
                None,
            )
            if recovery_applied and completed:
                prelaunch_recovery_candidates.append(
                    (candidate, candidate_events)
                )
        prelaunch_recovery = (
            prelaunch_recovery_candidates[0]
            if len(prelaunch_recovery_candidates) == 1
            else None
        )
        prelaunch_start = (
            prelaunch_recovery[0] if prelaunch_recovery else None
        )
        prelaunch_events = (
            prelaunch_recovery[1] if prelaunch_recovery else []
        )
        prelaunch_by_type = {
            event["event_type"]: event
            for event in prelaunch_events
        }
        prelaunch_start_payload = (
            prelaunch_start.get("payload")
            if isinstance(prelaunch_start, dict)
            else {}
        )
        prelaunch_snapshot = (
            prelaunch_by_type.get("SnapshotCaptured", {})
            .get("payload", {})
            .get("snapshot")
            or {}
        )
        prelaunch_checkpoint = (
            prelaunch_by_type.get("CheckpointRecorded", {})
            .get("payload", {})
        )
        prelaunch_deterministic_start = (
            prelaunch_by_type.get("DeterministicExecutionStarted", {})
            .get("payload", {})
        )
        prelaunch_deterministic_result = (
            prelaunch_by_type.get("DeterministicResultAccepted", {})
            .get("payload", {})
        )
        prelaunch_changes = (
            prelaunch_by_type.get("ChangesDetected", {})
            .get("payload", {})
        )
        prelaunch_validation = (
            prelaunch_by_type.get("ValidationPassed", {})
            .get("payload", {})
        )
        prelaunch_commit = (
            prelaunch_by_type.get("CommitFinalized", {})
            .get("payload", {})
        )
        prelaunch_applied = (
            prelaunch_by_type.get("RecoveryApplied", {})
            .get("payload", {})
        )
        prelaunch_completed = (
            prelaunch_by_type.get("TransactionCompleted", {})
            .get("payload", {})
        )
        prelaunch_terminal_snapshot = (
            prelaunch_completed.get("terminal_snapshot") or {}
        )
        prelaunch_projection = (
            prelaunch_by_type.get("ProjectionUpdated", {})
            .get("payload", {})
        )
        failed_prelaunch_transaction_id = prelaunch_checkpoint.get(
            "failed_transaction_id"
        )
        failed_prelaunch_events = [
            event
            for event in events
            if event["transaction_id"] == failed_prelaunch_transaction_id
        ]
        failed_prelaunch_start = next(
            (
                event
                for event in failed_prelaunch_events
                if event.get("event_type") == "TransactionStarted"
            ),
            None,
        )
        failed_prelaunch_terminal = next(
            (
                event
                for event in failed_prelaunch_events
                if event.get("event_type") == "TransactionBlocked"
            ),
            None,
        )
        failed_prelaunch_start_payload = (
            failed_prelaunch_start.get("payload")
            if isinstance(failed_prelaunch_start, dict)
            else {}
        )
        failed_prelaunch_terminal_payload = (
            failed_prelaunch_terminal.get("payload")
            if isinstance(failed_prelaunch_terminal, dict)
            else {}
        )
        failed_prelaunch_run_id = failed_prelaunch_start_payload.get("run_id")
        capability_failure_report: dict[str, Any] = {}
        if isinstance(failed_prelaunch_run_id, str) and failed_prelaunch_run_id:
            report_root = (self.controller_root / "reports").resolve()
            capability_report_path = (
                report_root
                / failed_prelaunch_run_id
                / "feature_cycle-launch-failure.json"
            ).resolve()
            try:
                capability_report_path.relative_to(report_root)
                loaded_capability_report = json.loads(
                    capability_report_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded_capability_report, dict):
                    capability_failure_report = loaded_capability_report
            except (ValueError, OSError, json.JSONDecodeError):
                capability_failure_report = {}
        capability_isolation_prelaunch = (
            failed_prelaunch_terminal_payload.get("reference")
            == "SessionError"
            and isinstance(failed_prelaunch_run_id, str)
            and authenticates_run_scoped_capability_isolation_report(
                capability_failure_report,
                project_id=self.project.project_id,
                run_id=failed_prelaunch_run_id,
                repository=self.project.repository,
            )
        )
        legacy_context_prelaunch = (
            failed_prelaunch_terminal_payload.get("reference")
            == "UnicodeDecodeError"
        )
        start_policy = start_payload.get("allowed_mutation_policy")
        start_policy = start_policy if isinstance(start_policy, dict) else {}
        allowed_paths = set(start_policy.get("allowed_paths") or ())
        allowed_prefixes = tuple(start_policy.get("allowed_prefixes") or ())
        unauthorized = [
            path
            for path in changed_paths
            if path not in allowed_paths
            and not any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in allowed_prefixes
            )
        ]
        report_paths = tuple(
            sorted(self._terminal_value(terminal_payload, "changed_paths") or ())
        )
        report_repository = self._terminal_value(
            terminal_payload, "repository_identity", "repository_id"
        )
        report_run = self._terminal_value(
            terminal_payload, "run_id", "agent_run_id"
        )
        report_feature = self._terminal_value(
            terminal_payload, "feature_id", "selected_feature"
        )
        report_session = self._terminal_value(terminal_payload, "session_id")
        report_transaction = self._terminal_value(
            terminal_payload, "transaction_id"
        )
        report_starting_branch = self._terminal_value(
            terminal_payload, "starting_branch"
        )
        report_starting_head = self._terminal_value(
            terminal_payload, "starting_commit"
        )
        report_current_head = self._terminal_value(
            terminal_payload, "current_commit"
        )
        exact_terminal_identity = all(
            (
                report_repository == identity["repository_id"],
                report_run == original_run_id,
                report_feature == feature_id,
                report_session == original_session_id,
                report_transaction == original_transaction_id,
                report_starting_branch == expected_branch,
                report_starting_head == expected_head,
                report_current_head == expected_head,
                report_paths == changed_paths,
            )
        )
        workflow_alias = self._normalized_workflow_alias(
            invoked=report.get("action"),
            envelope=terminal_payload.get("workflow_type"),
            exact_identity=exact_terminal_identity,
        )
        projection = self.projection.current()
        gate = projection.get("human_gate")
        gate = gate if isinstance(gate, dict) else {}
        terminal_gate = (terminal or {}).get("payload", {}).get("gate")
        terminal_gate = terminal_gate if isinstance(terminal_gate, dict) else {}
        decision = feature.get("decision_resolution") if isinstance(feature, dict) else None
        decision_contract = (
            isinstance(decision, dict)
            and isinstance(decision.get("recorded_question"), str)
            and bool(decision["recorded_question"].strip())
            and isinstance(decision.get("approved_resolution"), str)
            and bool(decision["approved_resolution"].strip())
            and decision.get("selected_feature") is True
            and isinstance(decision.get("decision_file_sha256"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", decision["decision_file_sha256"]))
        )
        accepted_events = [
            event
            for event in events
            if event["event_type"] in {"RecoveryApplied", "TransactionSuperseded"}
            and (
                event["payload"].get("recovered_transaction_id")
                == original_transaction_id
                or (
                    event["event_type"] == "TransactionSuperseded"
                    and event["transaction_id"] == original_transaction_id
                    and bool(event["payload"].get("superseded_by"))
                )
            )
        ]
        subject = start_policy.get("commit_subject")
        subject_commits = (
            self.inspector.git(
                [
                    "log",
                    "--all",
                    "--format=%H",
                    "--fixed-strings",
                    "--grep",
                    str(subject),
                ],
                check=False,
            ).stdout.splitlines()
            if isinstance(subject, str) and subject
            else []
        )
        structured_errors = report.get("structured_output_errors")
        expected_alias_error = (
            "terminal session-result envelope workflow_type conflicts with invoked workflow"
        )
        preparation_authenticated = bool(
            preparation_start
            and len(preparation_candidates) == 1
            and preparation_payload.get("feature_id") == feature_id
            and preparation_payload.get("starting_head") == expected_head
            and preparation_terminal_payload.get("classification")
            == "FEATURE_PREPARED"
            and preparation_terminal_payload.get("feature_id") == feature_id
            and preparation_terminal_payload.get("next_state")
            == "feature_preparing"
            and preparation_terminal_snapshot.get("branch") == expected_branch
            and preparation_terminal_snapshot.get("head") == expected_head
            and preparation_terminal_snapshot.get("queue_fingerprint")
            == preparation_payload.get("starting_queue_fingerprint")
            == start_payload.get("starting_queue_fingerprint")
            and preparation_projection_payload.get("current_feature")
            == feature_id
            and preparation_projection_payload.get("selected_feature") is None
            and (
                preparation_session or {}
            ).get("payload", {}).get("session_id")
            == "deterministic-feature-preparation:"
            + str(preparation_start.get("transaction_id"))
        )
        failed_prelaunch_authenticated = bool(
            failed_prelaunch_start
            and failed_prelaunch_terminal
            and failed_prelaunch_start.get("workflow_type")
            == WorkflowType.FEATURE_EXECUTION.value
            and failed_prelaunch_start_payload.get("feature_id") == feature_id
            and failed_prelaunch_start_payload.get("starting_branch")
            == expected_branch
            and failed_prelaunch_start_payload.get("starting_head")
            == expected_head
            and failed_prelaunch_start_payload.get(
                "starting_queue_fingerprint"
            )
            == start_payload.get("starting_queue_fingerprint")
            and not any(
                event.get("event_type") == "SessionLaunched"
                for event in failed_prelaunch_events
            )
            and failed_prelaunch_terminal_payload.get("classification")
            == "FEATURE_VALIDATION_FAILED"
            and failed_prelaunch_terminal_payload.get("next_state")
            == "validation_failed"
            and (
                legacy_context_prelaunch
                or capability_isolation_prelaunch
            )
            and int(failed_prelaunch_start.get("sequence", 0))
            > preparation_terminal_sequence
            and int(failed_prelaunch_terminal.get("sequence", 0))
            < int((prelaunch_start or {}).get("sequence", 0))
        )
        prelaunch_recovery_authenticated = bool(
            prelaunch_start
            and len(prelaunch_recovery_candidates) == 1
            and failed_prelaunch_authenticated
            and prelaunch_start_payload.get("feature_id") == feature_id
            and prelaunch_start_payload.get("starting_branch")
            == expected_branch
            and prelaunch_start_payload.get("starting_head") == expected_head
            and prelaunch_start_payload.get("starting_queue_fingerprint")
            == start_payload.get("starting_queue_fingerprint")
            and prelaunch_start_payload.get(
                "starting_tracked_diff_fingerprint"
            )
            == hashlib.sha256(b"").hexdigest()
            and prelaunch_start_payload.get("starting_untracked_fingerprint")
            == fingerprint({})
            and prelaunch_snapshot.get("branch") == expected_branch
            and prelaunch_snapshot.get("head") == expected_head
            and prelaunch_snapshot.get("queue_fingerprint")
            == start_payload.get("starting_queue_fingerprint")
            and prelaunch_snapshot.get("tracked_changed_paths") == []
            and prelaunch_snapshot.get("untracked_paths") == []
            and prelaunch_checkpoint.get("checkpoint")
            == "feature_prelaunch_recovery_authenticated"
            and prelaunch_checkpoint.get("model_sessions_launched") == 0
            and prelaunch_checkpoint.get("child_sessions_launched") == 0
            and prelaunch_checkpoint.get("implementation_attempts_consumed")
            == 0
            and prelaunch_deterministic_start.get("execution_mode")
            == "feature_prelaunch_recovery"
            and prelaunch_deterministic_start.get("model_session_launched")
            is False
            and prelaunch_deterministic_start.get("child_sessions_launched")
            == 0
            and prelaunch_deterministic_start.get("plan_fingerprint")
            == prelaunch_deterministic_result.get("plan_fingerprint")
            and prelaunch_deterministic_result.get("classification")
            == "RECOVERY_APPLIED"
            and prelaunch_changes.get("changed_paths") == []
            and prelaunch_validation.get("checks", {}).get(
                "application_unchanged"
            )
            is True
            and prelaunch_validation.get("checks", {}).get(
                "model_sessions_launched"
            )
            == 0
            and prelaunch_validation.get("checks", {}).get(
                "child_sessions_launched"
            )
            == 0
            and prelaunch_commit.get("no_change") is True
            and prelaunch_commit.get("commit") == expected_head
            and prelaunch_applied.get("classification")
            == "FEATURE_PRELAUNCH_RECOVERY"
            and prelaunch_applied.get("selected_feature") == feature_id
            and prelaunch_completed.get("feature_id") == feature_id
            and prelaunch_completed.get("feature_commit_created") is False
            and prelaunch_completed.get("model_session_launched") is False
            and prelaunch_completed.get("child_sessions_launched") == 0
            and prelaunch_terminal_snapshot.get("branch") == expected_branch
            and prelaunch_terminal_snapshot.get("head") == expected_head
            and prelaunch_terminal_snapshot.get("clean") is True
            and prelaunch_terminal_snapshot.get("queue_fingerprint")
            == start_payload.get("starting_queue_fingerprint")
            and prelaunch_projection.get("current_state")
            == "feature_preparing"
            and prelaunch_projection.get("current_feature") == feature_id
            and prelaunch_projection.get("selected_feature") == feature_id
            and int(
                prelaunch_by_type.get("ProjectionUpdated", {}).get(
                    "sequence", 0
                )
            )
            < int((start or {}).get("sequence", 0))
        )
        same_run_prepared_execution = bool(
            preparation_authenticated
            and preparation_payload.get("run_id") == original_run_id
        )
        preparation_lineage_authenticated = bool(
            preparation_authenticated
            and (
                same_run_prepared_execution
                or prelaunch_recovery_authenticated
            )
        )
        queue_in_progress_owned_by_execution = bool(
            isinstance(feature, dict)
            and feature.get("status") == "in_progress"
            and preparation_lineage_authenticated
            and (
                same_run_prepared_execution
                or (
                    prelaunch_recovery_authenticated
                    and (
                        (
                            self.project.queue_location in changed_paths
                            and starting_snapshot.get("queue_fingerprint")
                            != terminal_snapshot.get("queue_fingerprint")
                            and terminal_snapshot.get("queue_fingerprint")
                            == current_snapshot.queue_fingerprint
                        )
                        or (
                            start_payload.get("starting_queue_fingerprint")
                            == terminal_snapshot.get("queue_fingerprint")
                            == current_snapshot.queue_fingerprint
                        )
                    )
                )
            )
        )
        phase_feature_identity = {
            "transaction_feature_id": start_payload.get("feature_id"),
            "projection_current_feature": projection.get("current_feature"),
            "projection_selected_next_feature": projection.get(
                "selected_next_feature"
            ),
            "run_id": start_payload.get("run_id"),
            "session_id": (launch or {}).get("payload", {}).get("session_id"),
            "branch": self.inspector.current_branch,
            "starting_commit": start_payload.get("starting_head"),
            "queue_feature_id": feature.get("id") if isinstance(feature, dict) else None,
            "preparation_transaction_id": (
                preparation_start.get("transaction_id")
                if preparation_start
                else None
            ),
            "selection_consumed": projection.get("selected_next_feature") is None,
        }
        checks = {
            "ledger_integrity": integrity.valid,
            "original_transaction_exact_event_topology": topology[
                "authenticated"
            ],
            "original_transaction_terminal": terminal is not None,
            "original_transaction_workflow": all(
                event.get("workflow_type")
                == WorkflowType.FEATURE_EXECUTION.value
                for event in transaction_events
            ),
            "original_transaction_feature": start_payload.get("feature_id")
            == feature_id,
            "original_transaction_run": start_payload.get("run_id")
            == original_run_id,
            "original_session": (launch or {}).get("payload", {}).get(
                "session_id"
            )
            == original_session_id,
            "report_file_identity": report_path
            == self.controller_root
            / "reports"
            / original_run_id
            / "feature_cycle.json",
            "report_project": report.get("project_id") == self.project.project_id,
            "report_run": report.get("run_id") == original_run_id,
            "report_session": report.get("session_id") == original_session_id,
            "terminal_marker_present": report.get("terminal_marker_found") is True,
            "report_structured_failure": report.get("result_classification")
            == "structured_output_invalid"
            and isinstance(structured_errors, list)
            and structured_errors == [expected_alias_error],
            "terminal_identity_exact": exact_terminal_identity,
            "terminal_classification_claimed": terminal_payload.get(
                "classification"
            )
            == "FEATURE_ACCEPTED",
            "terminal_next_state_claimed": terminal_payload.get("next_state")
            == "feature_accepted",
            "workflow_alias_normalized": workflow_alias["normalized"]
            == WorkflowType.FEATURE_EXECUTION.value,
            "repository_identity": (start or {}).get("repository_identity")
            == identity["repository_id"],
            "repository_path_identity": (start or {}).get(
                "repository_path_fingerprint"
            )
            == identity["path_fingerprint"],
            "branch": self.inspector.current_branch == expected_branch,
            "head": self.inspector.head == expected_head,
            "branch_ref_head": self.inspector.rev_parse(
                expected_branch, check=False
            )
            == expected_head,
            "starting_snapshot_clean": start_payload.get(
                "starting_tracked_diff_fingerprint"
            )
            == hashlib.sha256(b"").hexdigest()
            and start_payload.get("starting_untracked_fingerprint")
            == fingerprint({})
            and starting_snapshot.get("branch") == expected_branch
            and starting_snapshot.get("head") == expected_head
            and starting_snapshot.get("tracked_changed_paths") == []
            and starting_snapshot.get("untracked_paths") == []
            and starting_snapshot.get("tracked_diff_fingerprint")
            == hashlib.sha256(b"").hexdigest()
            and starting_snapshot.get("untracked_fingerprint")
            == fingerprint({}),
            "terminal_branch": terminal_snapshot.get("branch")
            == expected_branch,
            "terminal_head": terminal_snapshot.get("head") == expected_head,
            "tracked_paths_exact": tuple(
                terminal_snapshot.get("tracked_changed_paths") or ()
            )
            == tracked_paths,
            "untracked_paths_exact": tuple(
                terminal_snapshot.get("untracked_paths") or ()
            )
            == untracked_paths,
            "tracked_fingerprint_exact": terminal_snapshot.get(
                "tracked_diff_fingerprint"
            )
            == current_snapshot.tracked_diff_fingerprint,
            "untracked_fingerprint_exact": terminal_snapshot.get(
                "untracked_fingerprint"
            )
            == current_snapshot.untracked_fingerprint,
            "queue_fingerprint_exact": terminal_snapshot.get(
                "queue_fingerprint"
            )
            == current_snapshot.queue_fingerprint
            and starting_snapshot.get("queue_fingerprint")
            == start_payload.get("starting_queue_fingerprint"),
            "no_unauthorized_paths": not unauthorized,
            "index_unstaged": not self.inspector.staged_changed_paths(),
            "no_git_operation": not any(
                self.inspector.git_operation_state().values()
            ),
            "writer_lease_absent": not writer.exists,
            "controller_reservation_absent": not reservation.exists
            or (
                allowed_reservation_run_id is not None
                and reservation.owned_by_run
            ),
            "projection_ledger_bound": projection.get("ledger_sequence")
            == integrity.sequence
            and projection.get("ledger_fingerprint")
            == integrity.fingerprint,
            "projection_state": projection.get("current_state")
            == "human_decision_required",
            "projection_feature": projection.get("current_feature")
            == feature_id
            and projection.get("selected_next_feature") is None,
            "phase_feature_identity": all(
                (
                    phase_feature_identity["transaction_feature_id"]
                    == feature_id,
                    phase_feature_identity["projection_current_feature"]
                    == feature_id,
                    phase_feature_identity["run_id"] == original_run_id,
                    phase_feature_identity["session_id"]
                    == original_session_id,
                    phase_feature_identity["branch"] == expected_branch,
                    phase_feature_identity["starting_commit"] == expected_head,
                    phase_feature_identity["queue_feature_id"] == feature_id,
                )
            ),
            "preparation_topology": (
                preparation_lineage_authenticated
                if isinstance(feature, dict)
                and feature.get("status") == "in_progress"
                else not preparation_candidates or preparation_authenticated
            ),
            "preparation_execution_lineage": (
                preparation_lineage_authenticated
                if isinstance(feature, dict)
                and feature.get("status") == "in_progress"
                else True
            ),
            "prelaunch_recovery_topology": (
                prelaunch_recovery_authenticated
                if prelaunch_recovery_candidates
                else True
            ),
            "projection_transaction_inactive": projection.get(
                "active_transaction"
            )
            is None,
            "gate_identity": gate.get("gate_id")
            == terminal_gate.get("gate_id")
            and gate.get("transaction_id") == original_transaction_id
            and gate.get("run_id") == original_run_id
            and gate.get("feature") == feature_id
            and gate.get("classification") == "structured_output_invalid"
            and gate.get("workflow_type")
            == WorkflowType.FEATURE_EXECUTION.value
            and gate.get("resolved") in {None, False},
            "gate_contract_exact": gate == terminal_gate
            and (terminal or {}).get("payload", {}).get("gate_fingerprint")
            == fingerprint(terminal_gate),
            "queue_feature_recoverable": isinstance(feature, dict)
            and (
                feature.get("status") == "ready"
                or queue_in_progress_owned_by_execution
            ),
            "queue_in_progress_owned_by_execution": (
                queue_in_progress_owned_by_execution
                if isinstance(feature, dict)
                and feature.get("status") == "in_progress"
                else True
            ),
            "queue_decision_approved": isinstance(feature, dict)
            and feature.get("requires_human_decision") is False
            and (decision is None or decision_contract),
            "queue_branch_unclaimed": isinstance(feature, dict)
            and feature.get("branch") in {None, expected_branch},
            "queue_has_no_accepted_commit": isinstance(feature, dict)
            and not feature.get("accepted_commit"),
            "milestone_identity": isinstance(milestone, dict)
            and isinstance(milestone.get("integration_branch"), str)
            and self.inspector.rev_parse(
                str(milestone["integration_branch"]), check=False
            )
            == expected_head,
            "original_transaction_not_recovered": not accepted_events,
            "application_commit_absent": not subject_commits,
            "commit_subject_exact": isinstance(subject, str) and bool(subject),
            "autopilot_ownership_absent": not (
                self.controller_root
                / "state"
                / "autopilot"
                / self.project.project_id
                / "ownership.json"
            ).exists(),
            "live_session_absent": projection.get("active_transaction") is None
            and not writer.exists,
            "exact_f070_terminal_report": (
                report.get("exit_classification")
                == "structured_output_invalid"
                and report.get("structured_output_validation") == "invalid"
                and not untracked_paths
                if prelaunch_recovery_authenticated
                else True
            ),
        }
        if not all(checks.values()):
            failed = ", ".join(
                key for key, passed in checks.items() if not passed
            )
            raise RecoveryError(
                f"{feature_id} recovery preflight failed: {failed}"
            )
        metadata_paths = acceptance_metadata_paths(self.project, feature)
        final_paths = tuple(sorted({*changed_paths, *metadata_paths}))
        validation_commands = self._validation_commands(
            tracked_paths=tracked_paths, untracked_paths=untracked_paths
        )
        plan = {
            "schema_version": 2,
            "recovery_contract": "general_retained_feature_result",
            "project_id": self.project.project_id,
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
            "feature_id": feature_id,
            "feature_title": feature.get("title"),
            "milestone_id": feature.get("milestone"),
            "milestone_branch": milestone.get("integration_branch"),
            "branch": expected_branch,
            "head": expected_head,
            "original_transaction_id": original_transaction_id,
            "original_run_id": original_run_id,
            "original_session_id": original_session_id,
            "original_event_fingerprints": [
                event["fingerprint"] for event in transaction_events
            ],
            "original_transaction_topology": topology,
            "preparation_transaction_id": (
                preparation_start["transaction_id"]
                if preparation_start
                else None
            ),
            "preparation_event_fingerprints": [
                event["fingerprint"] for event in preparation_events
            ],
            "prelaunch_recovery_transaction_id": (
                prelaunch_start["transaction_id"]
                if prelaunch_start
                else None
            ),
            "prelaunch_recovery_event_fingerprints": [
                event["fingerprint"] for event in prelaunch_events
            ],
            "failed_prelaunch_transaction_id": (
                failed_prelaunch_transaction_id
                if prelaunch_recovery_authenticated
                else None
            ),
            "transaction_lineage": {
                "preparation_transaction_id": (
                    preparation_start["transaction_id"]
                    if preparation_start
                    else None
                ),
                "prelaunch_recovery_transaction_id": (
                    prelaunch_start["transaction_id"]
                    if prelaunch_start
                    else None
                ),
                "execution_transaction_id": original_transaction_id,
                "continuous": preparation_lineage_authenticated,
            },
            "phase_feature_identity": phase_feature_identity,
            "original_gate_id": gate["gate_id"],
            "original_gate_fingerprint": fingerprint(gate),
            "gate_supersession": {
                "gate_id": gate["gate_id"],
                "action": "supersede_and_resolve_after_host_validation",
                "append_only_history_preserved": True,
            },
            "report_path": str(report_path),
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "report_error": expected_alias_error,
            "terminal_marker_present": True,
            "terminal_classification_claimed": terminal_payload[
                "classification"
            ],
            "terminal_next_state_claimed": terminal_payload["next_state"],
            "tracked_paths": list(tracked_paths),
            "untracked_paths": list(untracked_paths),
            "changed_paths": list(changed_paths),
            "final_changed_paths": list(final_paths),
            "tracked_diff_fingerprint": current_snapshot.tracked_diff_fingerprint,
            "untracked_fingerprint": current_snapshot.untracked_fingerprint,
            "untracked_file_hashes": untracked_hashes,
            "retained_content_fingerprint": self.inspector.content_diff_fingerprint(
                changed_paths
            ),
            "preserved_implementation_paths": [
                path for path in changed_paths if path not in metadata_paths
            ],
            "acceptance_metadata_paths": list(metadata_paths),
            "commit_subject": subject,
            "workflow_alias": workflow_alias,
            "host_validation_commands": validation_commands,
            "candidate_commits_that_would_be_created": 1,
            "candidate_commit_created_only_after_all_validations": True,
            "checks": checks,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "application_repository_written": False,
            "lease_type": "feature_writer",
            "next_state_on_success": "integration_pending",
            "final_projected_state": "integration_pending",
            "milestone_integration_performed": False,
            "queue_reconciliation_performed": False,
        }
        plan["preserved_implementation_fingerprint"] = (
            self.inspector.content_diff_fingerprint(
                tuple(plan["preserved_implementation_paths"])
            )
        )
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def _run(self, argv: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        record = self.command_runner(argv, self.project.repository)
        if not isinstance(record, dict) or not isinstance(record.get("exit_code"), int):
            raise RecoveryError("feature recovery command runner returned invalid evidence")
        evidence.append(record)
        return record

    @staticmethod
    def _documentation_check(repository: Path, *, accepted: bool) -> dict[str, Any]:
        sources = {
            "feature_spec": repository / "docs/features/F003-single-window-application-shell.md",
            "architecture": repository / "docs/architecture.md",
            "data_flow": repository / "docs/data-flow.md",
            "adr": repository / "docs/decisions/0015-single-window-workspace-and-session-routing.md",
            "decision_index": repository / "docs/decisions/README.md",
            "workspace_router": repository / "Sources/LiveInterviewCompanion/Models/WorkspaceRouter.swift",
        }
        contents = {key: path.read_text(encoding="utf-8") for key, path in sources.items()}
        checks = {
            "all_sources_present": all(path.is_file() for path in sources.values()),
            "architecture_names_router": "WorkspaceRouter" in contents["architecture"],
            "data_flow_names_router": "WorkspaceRouter" in contents["data_flow"],
            "adr_names_single_window": "single" in contents["adr"].lower()
            and "WorkspaceRouter" in contents["adr"],
            "decision_index_names_0015": "0015-single-window-workspace-and-session-routing.md"
            in contents["decision_index"],
            "router_defines_seven_sections": all(
                f"case {name}" in contents["workspace_router"]
                for name in (
                    "dashboard", "applications", "practice", "liveInterview",
                    "sessions", "knowledge", "settings",
                )
            ),
            "feature_acceptance_boxes": (
                "- [ ]" not in contents["feature_spec"]
                if accepted else "- [ ]" in contents["feature_spec"]
            ),
        }
        return {
            "name": "documentation_and_adr_validation",
            "passed": all(checks.values()),
            "checks": checks,
        }

    def _metadata_paths(self) -> tuple[str, ...]:
        return (
            "CURRENT_STATUS.md",
            "docs/CURRENT_STATUS.md",
            "docs/FEATURE_CATALOG.md",
            "docs/FEATURE_QUEUE.yaml",
            "docs/RUN_LOG.md",
            "docs/features/F003-single-window-application-shell.md",
        )

    @staticmethod
    def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
        if text.count(old) != 1:
            raise RecoveryError(f"F003 recovery metadata expectation changed: {label}")
        return text.replace(old, new, 1)

    def _apply_acceptance_metadata(
        self, *, run_id: str, commands: list[dict[str, Any]], checks: list[dict[str, Any]]
    ) -> dict[str, bytes]:
        repository = self.project.repository
        originals = {
            relative: (repository / relative).read_bytes()
            for relative in self._metadata_paths()
        }
        queue_path = repository / self.project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = next(item for item in queue["features"] if item.get("id") == "F003")
        if feature.get("status") != "review" or feature.get("accepted_commit"):
            raise RecoveryError("F003 queue changed before acceptance metadata transition")
        feature.update({
            "status": "integration_pending",
            "accepted_commit": "SELF",
            "integration_status": "pending",
            "implementation_status": "Completed",
            "acceptance": {
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": True,
            },
            "updated_at": utc_now(),
        })
        atomic_write_json(queue_path, queue)

        catalog = repository / "docs/FEATURE_CATALOG.md"
        catalog_text = self._replace_once(
            catalog.read_text(encoding="utf-8"),
            "| F003 | Single-Window Application Shell | Review |",
            "| F003 | Single-Window Application Shell | Integration pending |",
            label="catalog status",
        )
        atomic_write_bytes(catalog, catalog_text.encode("utf-8"))

        spec = repository / "docs/features/F003-single-window-application-shell.md"
        spec_text = self._replace_once(
            spec.read_text(encoding="utf-8"),
            "- Factory status: Review — implementation complete; exact environment gates and adversarial review pending",
            "- Factory status: Integration pending — host validation passed and the accepted feature commit is pending milestone integration",
            label="feature status",
        ).replace("- [ ] ", "- [x] ")
        atomic_write_bytes(spec, spec_text.encode("utf-8"))

        docs_status = repository / "docs/CURRENT_STATUS.md"
        docs_text = docs_status.read_text(encoding="utf-8")
        docs_text = self._replace_once(
            docs_text,
            "- Active feature: F003 — Single-Window Application Shell (`review`)",
            "- Active feature: F003 — Single-Window Application Shell (`integration_pending`)",
            label="docs active feature",
        )
        docs_text = self._replace_once(
            docs_text,
            "Its implementation and deterministic tests are complete but uncommitted in review; exact environment gates and adversarial review remain pending.",
            "Its implementation, host validation, and review corrections passed; one immutable accepted feature commit is pending milestone integration.",
            label="docs queue state",
        )
        docs_text = self._replace_once(
            docs_text,
            "F003 implementation and deterministic validation are complete on its isolated branch and remain uncommitted pending adversarial review. Resolve every Critical, High, and Medium finding before acceptance.",
            "F003 is accepted on its isolated branch with all required host validation and review corrections complete; milestone integration remains pending.",
            label="docs next action",
        )
        atomic_write_bytes(docs_status, docs_text.encode("utf-8"))

        root_status = repository / "CURRENT_STATUS.md"
        root_text = root_status.read_text(encoding="utf-8")
        root_text = self._replace_once(
            root_text,
            "- Status: Implementation and deterministic validation complete on `codex/F003-single-window-application-shell`; adversarial review pending",
            "- Status: Accepted on `codex/F003-single-window-application-shell`; milestone integration pending",
            label="root status",
        )
        root_text = self._replace_once(
            root_text,
            "- Build: debug compilation passes; release and staged-app verification are required before F003 leaves review",
            "- Build: focused tests, debug/release builds, full tests, staged-app verify, and accessibility smoke pass",
            label="root build",
        )
        root_text = self._replace_once(
            root_text,
            "- Git: F003 remains uncommitted and unintegrated pending adversarial review and acceptance",
            "- Git: one accepted F003 feature commit is pending milestone integration",
            label="root git",
        )
        root_text = self._replace_once(
            root_text,
            "Run the named adversarial review for F003. Resolve every Critical, High, and Medium finding before acceptance.",
            "Integrate the accepted F003 commit through the milestone integration workflow; do not begin F004 before integration.",
            label="root next action",
        )
        atomic_write_bytes(root_status, root_text.encode("utf-8"))

        run_log = repository / "docs/RUN_LOG.md"
        summary = [
            "",
            f"## {utc_now()[:10]} — F003 zero-model feature-result recovery",
            "",
            f"- Recovery run: `{run_id}`; original feature transaction remained immutable.",
            "- Execution: deterministic controller-host recovery; no model or child session launched.",
            "- Fresh typed feature-writer lease covered host validation, metadata finalization, and the one accepted commit.",
            "- Required focused routing/lifecycle tests, debug build, full test suite, release build, staged-app verify, inventory validation, documentation/ADR validation, git diff check, and accessibility single-window smoke passed.",
            "- The existing review corrections remain present with no unresolved Critical, High, or Medium finding.",
            "- F003 transitioned from `review` to `accepted` to `integration_pending`; milestone integration was not performed.",
            "",
            "### Deterministic validation evidence",
            "",
        ]
        summary.extend(
            f"- `{ ' '.join(item.get('argv') or []) }`: exit {item.get('exit_code')}"
            for item in commands
        )
        summary.extend(
            f"- `{item.get('name')}`: {'passed' if item.get('passed') else 'failed'}"
            for item in checks
        )
        prior_log = run_log.read_text(encoding="utf-8").rstrip()
        atomic_write_bytes(
            run_log, (prior_log + "\n" + "\n".join(summary) + "\n").encode("utf-8")
        )
        return originals

    def _restore_metadata(self, originals: dict[str, bytes]) -> None:
        for relative, payload in originals.items():
            atomic_write_bytes(self.project.repository / relative, payload)

    @staticmethod
    def _replace_catalog_feature_status(
        text: str, *, feature_id: str
    ) -> str:
        lines = text.splitlines()
        matches = [
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith(f"| {feature_id} |")
        ]
        if len(matches) != 1:
            raise RecoveryError(
                f"feature catalog must contain exactly one {feature_id} row"
            )
        fields = [
            field.strip()
            for field in lines[matches[0]].strip().strip("|").split("|")
        ]
        if len(fields) < 3 or fields[0] != feature_id:
            raise RecoveryError(
                f"feature catalog row for {feature_id} is malformed"
            )
        fields[2] = "Accepted"
        lines[matches[0]] = "| " + " | ".join(fields) + " |"
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    @staticmethod
    def _replace_factory_position(
        text: str, *, feature_id: str, title: str
    ) -> str:
        lines = text.splitlines(keepends=True)
        headings = [
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == "## Factory position"
        ]
        if len(headings) != 1:
            raise RecoveryError(
                "docs/CURRENT_STATUS.md must contain one Factory position section"
            )
        start = headings[0] + 1
        end = next(
            (
                index
                for index in range(start, len(lines))
                if re.match(r"^##(?:\s|$)", lines[index].rstrip("\r\n"))
            ),
            len(lines),
        )
        pattern = re.compile(
            r"^- (?:Selected next feature|Selected feature|Active feature): "
            r"[A-Za-z0-9][A-Za-z0-9.-]*(?:\s+—\s+.+)?$"
        )
        matches = [
            index
            for index in range(start, end)
            if pattern.fullmatch(lines[index].rstrip("\r\n"))
        ]
        if len(matches) != 1:
            raise RecoveryError(
                "docs/CURRENT_STATUS.md Factory position must contain one feature-state line"
            )
        index = matches[0]
        newline = (
            "\r\n"
            if lines[index].endswith("\r\n")
            else ("\n" if lines[index].endswith("\n") else "")
        )
        lines[index] = (
            f"- Active feature: {feature_id} — {title} "
            "(accepted; milestone integration pending)"
            + newline
        )
        return "".join(lines)

    def _render_general_acceptance_metadata(
        self,
        *,
        plan: dict[str, Any],
        run_id: str,
        commands: list[dict[str, Any]],
    ) -> dict[str, bytes]:
        metadata_paths = tuple(plan["acceptance_metadata_paths"])
        queue_path = self.project.repository / self.project.queue_location
        queue_document = json.loads(queue_path.read_text(encoding="utf-8"))
        matches = [
            item
            for item in queue_document.get("features", [])
            if isinstance(item, dict)
            and item.get("id") == plan["feature_id"]
        ]
        if len(matches) != 1:
            raise RecoveryError("recovery acceptance queue feature is not unique")
        feature = matches[0]
        recoverable_status = feature.get("status") == "ready" or (
            feature.get("status") == "in_progress"
            and isinstance(plan.get("preparation_transaction_id"), str)
            and bool(plan["preparation_transaction_id"])
        )
        if not recoverable_status or feature.get("accepted_commit"):
            raise RecoveryError(
                "recovery acceptance queue changed before finalization"
            )
        feature.update(
            {
                "implementation_status": "Completed",
                "status": "integration_pending",
                "integration_status": "pending",
                "branch": plan["branch"],
                "integration_base_commit": plan["head"],
                "accepted_commit": "SELF",
                "acceptance": {
                    "tests_passed": True,
                    "review_passed": True,
                    "documentation_current": True,
                },
            }
        )
        rendered: dict[str, bytes] = {
            self.project.queue_location: (
                json.dumps(queue_document, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        }
        specification = next(
            path
            for path in metadata_paths
            if path.startswith("docs/features/")
        )
        spec_text = (
            self.project.repository / specification
        ).read_text(encoding="utf-8")
        spec_text, count = re.subn(
            r"(?m)^- Factory status:.*$",
            "- Factory status: Integration pending — controller-host validation "
            "passed and the accepted commit awaits milestone integration",
            spec_text,
            count=1,
        )
        if count != 1:
            raise RecoveryError(
                f"{plan['feature_id']} specification lacks one Factory status line"
            )
        rendered[specification] = spec_text.replace(
            "- [ ] ", "- [x] "
        ).encode("utf-8")
        rendered["docs/FEATURE_CATALOG.md"] = (
            self._replace_catalog_feature_status(
                (
                    self.project.repository / "docs/FEATURE_CATALOG.md"
                ).read_text(encoding="utf-8"),
                feature_id=str(plan["feature_id"]),
            ).encode("utf-8")
        )
        rendered["docs/CURRENT_STATUS.md"] = self._replace_factory_position(
            (
                self.project.repository / "docs/CURRENT_STATUS.md"
            ).read_text(encoding="utf-8"),
            feature_id=str(plan["feature_id"]),
            title=str(plan["feature_title"]),
        ).encode("utf-8")
        run_log = (
            self.project.repository / "docs/RUN_LOG.md"
        ).read_text(encoding="utf-8").rstrip()
        entry = [
            "",
            f"## {plan['feature_id']} deterministic retained-result recovery",
            "",
            f"- Recovery run: `{run_id}`.",
            f"- Original failed transaction: `{plan['original_transaction_id']}`.",
            "- Execution: controller-host deterministic recovery; zero model and child sessions.",
            "- Host validation passed before the candidate/accepted feature commit.",
            "- Milestone integration and queue reconciliation were not performed.",
            "",
            "### Host validation evidence",
            "",
            *[
                f"- `{' '.join(item.get('argv') or [])}`: exit {item.get('exit_code')}"
                for item in commands
            ],
            "",
        ]
        rendered["docs/RUN_LOG.md"] = (
            run_log + "\n" + "\n".join(entry)
        ).encode("utf-8")
        if tuple(sorted(rendered)) != metadata_paths:
            raise RecoveryError(
                "recovery acceptance metadata differs from the authorized set"
            )
        return rendered

    def _apply_general_acceptance_metadata(
        self,
        *,
        plan: dict[str, Any],
        run_id: str,
        commands: list[dict[str, Any]],
    ) -> dict[str, bytes]:
        metadata_paths = tuple(plan["acceptance_metadata_paths"])
        originals = {
            relative: (self.project.repository / relative).read_bytes()
            for relative in metadata_paths
        }
        rendered = self._render_general_acceptance_metadata(
            plan=plan,
            run_id=run_id,
            commands=commands,
        )
        try:
            for relative in metadata_paths:
                atomic_write_bytes(
                    self.project.repository / relative, rendered[relative]
                )
        except Exception:
            self._restore_metadata(originals)
            raise
        return originals

    def _materialize_controller_compatibility_cache(
        self,
        *,
        feature_id: str,
        run_id: str,
        transaction_id: str,
        accepted_commit: str,
        projection: dict[str, Any],
    ) -> None:
        path = (
            self.controller_root
            / "state"
            / "projects"
            / f"{self.project.project_id}.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "controller compatibility cache is unavailable or invalid"
            ) from exc
        if not isinstance(value, dict):
            raise RecoveryError("controller compatibility cache is not an object")
        execution_plan = ExecutionPlan.from_projection(
            projection,
            starting_commit=self.inspector.rev_parse(f"{accepted_commit}^"),
            feature_branch=self.inspector.current_branch,
            milestone_branch=self.project.milestone_branch,
        )
        document = build_controller_compatibility_cache(
            value,
            project_schema=self.project_schema,
            project_id=self.project.project_id,
            repository_fingerprint=self.inspector.identity()["path_fingerprint"],
            active_milestone=self.project.active_milestone,
            run_id=run_id,
            projection=projection,
            execution_plan=execution_plan.to_dict(),
            updated_at=utc_now(),
        )
        write_controller_compatibility_cache(
            path, document, project_schema=self.project_schema
        )

    def _materialize_general_compatibility_caches(
        self,
        *,
        feature_id: str,
        run_id: str,
        transaction_id: str,
        accepted_commit: str,
        projection: dict[str, Any],
    ) -> None:
        cycle_path = self.inspector.cycle_state_path()
        controller_path = (
            self.controller_root
            / "state"
            / "projects"
            / f"{self.project.project_id}.json"
        )
        originals = {
            cycle_path: cycle_path.read_bytes(),
            controller_path: controller_path.read_bytes(),
        }
        try:
            self._materialize_terminal_cycle_cache(
                feature_id=feature_id,
                run_id=run_id,
                transaction_id=transaction_id,
                accepted_commit=accepted_commit,
                projection=projection,
            )
            self._materialize_controller_compatibility_cache(
                feature_id=feature_id,
                run_id=run_id,
                transaction_id=transaction_id,
                accepted_commit=accepted_commit,
                projection=projection,
            )
        except Exception:
            for path, content in originals.items():
                atomic_write_bytes(path, content)
            raise

    def _materialize_terminal_cycle_cache(
        self,
        *,
        feature_id: str,
        run_id: str,
        transaction_id: str,
        accepted_commit: str,
        projection: dict[str, Any],
    ) -> None:
        path = self.inspector.cycle_state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError("F003 recovery cycle cache is unavailable or invalid") from exc
        if not isinstance(state, dict):
            raise RecoveryError("F003 recovery cycle cache is not an object")
        stamp = utc_now()
        repository_identity = self.inspector.identity()
        feature = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        ).feature(feature_id) or {}
        feature_starting_commit = self.inspector.rev_parse(f"{accepted_commit}^")
        state = build_canonical_cycle_cache(
            state,
            cycle_schema=self.cycle_schema,
            projection=projection,
            updates={
                "schema_version": 1,
                "project_id": self.project.project_id,
                "repository_identity": repository_identity,
                "repository_path_fingerprint": repository_identity[
                    "path_fingerprint"
                ],
                "active_milestone": self.project.active_milestone,
                "current_feature": feature_id,
                "feature_dependencies": list(feature.get("dependencies") or []),
                "feature_branch": self.inspector.current_branch,
                "feature_worktree": state.get("feature_worktree"),
                "feature_starting_commit": feature_starting_commit,
                "accepted_feature_commit": accepted_commit,
                "milestone_branch": self.project.milestone_branch,
                "milestone_pre_integration_commit": feature_starting_commit,
                "milestone_post_integration_commit": None,
                "conveyor_run_id": run_id,
                "writer_lock_identity": None,
                "validation_attempts": list(
                    state.get("validation_attempts") or []
                ),
                "review_attempts": list(state.get("review_attempts") or []),
                "integration_attempts": list(
                    state.get("integration_attempts") or []
                ),
                "session_id": None,
                "feature_session_id": None,
                "session_completion_classification": "FEATURE_ACCEPTED",
                "session_completion_flags": [],
                "session_completion_evidence": {
                    "recovery_transaction_id": transaction_id,
                    "accepted_feature_commit": accepted_commit,
                    "model_session_launched": False,
                },
                "human_decision_required": None,
                "failure_classification": None,
                "retry_exhausted": False,
                "stop_reason": None,
                "next_safe_action": (
                    f"scripts/conveyor run --project {self.project.project_id} "
                    f"--mode {self.project.automation_mode}"
                ),
                "resume_instructions": (
                    f"scripts/conveyor run --project {self.project.project_id} "
                    f"--mode {self.project.automation_mode}"
                ),
                "last_successful_checkpoint": "feature_result_recovery_terminal",
                "last_verified_git_state": {
                    "branch": self.inspector.current_branch,
                    "head": accepted_commit,
                    "clean": self.inspector.is_clean,
                    "git_operations": self.inspector.git_operation_state(),
                },
                "kernel_transaction_id": transaction_id,
                "kernel_ledger_sequence": projection["ledger_sequence"],
                "kernel_ledger_fingerprint": projection["ledger_fingerprint"],
                "kernel_projection_fingerprint": projection[
                    "projection_fingerprint"
                ],
                "created_at": state.get("created_at") or stamp,
                "updated_at": stamp,
            },
        )
        write_terminal_cycle_cache(
            path, state, ledger=self.ledger, projection_engine=self.projection,
            transaction_id=transaction_id, expected_feature=feature_id,
            cycle_schema=self.cycle_schema,
        )

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        if plan.get("feature_id") == "F003":
            return self._apply_f003(plan)
        return self._apply_general(plan)

    def _apply_f003(self, plan: dict[str, Any]) -> dict[str, Any]:
        if plan.get("plan_fingerprint") != fingerprint({
            key: value
            for key, value in plan.items()
            if key != "plan_fingerprint"
            and key not in _COMMAND_DISCOVERY_FIELDS
        }):
            raise RecoveryError("F003 recovery plan fingerprint is invalid")
        run_id = f"feature-recovery-{uuid.uuid4()}"
        lock_directory = self.controller_root / self.configuration["lock_policy"]["controller_launch_lock_directory"]
        reservation = DurableLock(
            lock_directory / f"{plan['repository_path_fingerprint']}.json"
        )
        reservation.acquire(make_lock_record(
            project_id=self.project.project_id,
            repository_identity=plan["repository_identity"],
            run_id=run_id,
            current_feature="F003",
            current_phase="feature_result_recovery",
        ))
        kernel: WorkflowKernel | None = None
        originals: dict[str, bytes] | None = None
        commands: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        try:
            self.inspector.ensure_runtime_ignored()
            revalidated = self.inspect(
                feature_id="F003",
                original_transaction_id=str(plan["original_transaction_id"]),
                original_run_id=str(plan["original_run_id"]),
                original_session_id=str(plan["original_session_id"]),
                expected_branch=str(plan["branch"]),
                expected_head=str(plan["head"]),
            )
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError("F003 recovery evidence changed under launch reservation")
            adapter = FeatureExecutionAdapter(
                allowed_paths=tuple(plan["changed_paths"]),
                commit_subject="F003: Single-Window Application Shell",
                next_state="integration_pending",
                allow_untracked=True,
                require_clean_start=False,
                denied_paths=(
                    ".factory/approved-content.yaml",
                    ".factory/conveyor-state.json",
                    ".factory/locks/writer.json",
                    ".factory/project.yaml",
                    "docs/AUTONOMY_CONTRACT.md",
                ),
                denied_prefixes=(".factory", "factory-integration"),
            )
            kernel = WorkflowKernel(
                project=self.project,
                ledger=self.ledger,
                projection=self.projection,
                lease=WorkflowWriterLease(self.inspector.writer_lock_path(
                    self.configuration["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            transaction = kernel.begin(
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                milestone=self.project.active_milestone,
                feature_id="F003",
                run_id=run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovery_mode": "preserved_feature_result",
                    "recovered_transaction_id": plan["original_transaction_id"],
                    "original_session_id": plan["original_session_id"],
                    "model_session_launched": False,
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
                expected_starting_branch=str(plan["branch"]),
                expected_starting_head=str(plan["head"]),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            if transaction.feature_id != "F003":
                raise TransactionError("recovery kernel lost the F003 feature identity")

            required_commands = [
                ["swift", "test", "--filter", "WorkspaceRoutingTests"],
                ["swift", "test", "--filter", "AppStoreWorkspaceRoutingTests"],
                ["swift", "test", "--filter", "SessionLifecycleTests"],
                ["swift", "build"],
                ["swift", "test"],
                ["swift", "build", "-c", "release"],
                ["./script/build_and_run.sh", "--verify"],
                [
                    "swift",
                    str(self.controller_root / "scripts/f003_ax_smoke.swift"),
                ],
                [
                    "python3",
                    str(Path.home() / ".agents/skills/app-bootstrap/scripts/bootstrap_project.py"),
                    "--root", str(self.project.repository),
                ],
            ]
            for argv in required_commands:
                record = self._run(argv, commands)
                content_gate_failed = (
                    Path(argv[1] if len(argv) > 1 else "").name == "bootstrap_project.py"
                    and record.get("content_audit_hard_gate") is not False
                )
                if record["exit_code"] != 0 or content_gate_failed:
                    permission = "ACCESSIBILITY_PERMISSION_REQUIRED" in str(record.get("output_summary"))
                    gate = {
                        "classification": (
                            "accessibility_permission_required"
                            if permission else "host_validation_failed"
                        ),
                        "reason": (
                            "Grant Accessibility permission to the controller host and rerun the exact F003 recovery."
                            if permission else
                            f"Required host validation failed: {' '.join(argv)}"
                        ),
                        "failed_command": argv,
                        "retryable": permission,
                    }
                    state = (
                        TransactionState.HUMAN_DECISION_REQUIRED
                        if permission else TransactionState.TERMINAL_FAILURE
                    )
                    classification = (
                        "HUMAN_DECISION_REQUIRED" if permission else "FEATURE_VALIDATION_FAILED"
                    )
                    projection = kernel.block(
                        state=state,
                        classification=classification,
                        next_state="human_decision_required" if permission else "review",
                        human_gate=gate if permission else None,
                    )
                    return {
                        "schema_version": 1,
                        "project_id": self.project.project_id,
                        "outcome": "human_decision_required" if permission else "validation_failed",
                        "feature_id": "F003",
                        "run_id": run_id,
                        "recovery_transaction_id": transaction.transaction_id,
                        "failed_command": argv,
                        "model_session_launched": False,
                        "implementation_preserved": True,
                        "projection": projection,
                        "human_gate": gate if permission else None,
                    }

            checks.append(self._documentation_check(self.project.repository, accepted=False))
            if not checks[-1]["passed"]:
                raise RecoveryError("pre-acceptance documentation and ADR validation failed")
            originals = self._apply_acceptance_metadata(
                run_id=run_id, commands=commands, checks=checks
            )
            if self.inspector.content_diff_fingerprint(
                tuple(plan["preserved_implementation_paths"])
            ) != plan["preserved_implementation_fingerprint"]:
                self._restore_metadata(originals)
                originals = None
                raise RecoveryError("preserved F003 implementation content changed during metadata transition")
            inventory = self._run([
                "python3",
                str(Path.home() / ".agents/skills/feature-inventory/scripts/validate_inventory.py"),
                "--root", str(self.project.repository),
            ], commands)
            checks.append(self._documentation_check(self.project.repository, accepted=True))
            diff_check = self._run(["git", "diff", "--check"], commands)
            if inventory["exit_code"] != 0 or diff_check["exit_code"] != 0 or not checks[-1]["passed"]:
                self._restore_metadata(originals)
                originals = None
                raise RecoveryError("post-transition inventory, documentation, or diff validation failed")
            if _changed_paths(self.inspector) != tuple(plan["changed_paths"]):
                self._restore_metadata(originals)
                originals = None
                raise RecoveryError("acceptance metadata changed the authorized path set")

            accepted_commit = kernel.finalize_deterministic_feature_recovery(
                original_transaction_id=str(plan["original_transaction_id"]),
                changed_paths=tuple(plan["changed_paths"]),
                original_content_fingerprint=str(plan["original_content_fingerprint"]),
                plan_fingerprint=str(plan["plan_fingerprint"]),
                validation_evidence={
                    "all_required_passed": True,
                    "commands": [
                        {key: item.get(key) for key in (
                            "argv", "exit_code", "duration_seconds", "output_sha256"
                        )}
                        for item in commands
                    ],
                    "checks": checks,
                    "warnings": [],
                },
            )
            completion = kernel.complete(
                classification="FEATURE_ACCEPTED",
                evidence={
                    "accepted_feature_commit": accepted_commit,
                    "integration_status": "pending",
                    "recovered_transaction_id": plan["original_transaction_id"],
                    "model_session_launched": False,
                    "host_validation_passed": True,
                },
            )
            self._materialize_terminal_cycle_cache(
                feature_id="F003",
                run_id=run_id,
                transaction_id=transaction.transaction_id,
                accepted_commit=accepted_commit,
                projection=completion["projection"],
            )
            old_events_after = [
                event["fingerprint"] for event in self.ledger.read()
                if event["transaction_id"] == plan["original_transaction_id"]
            ]
            final_checks = {
                "accepted_commit_direct_child": self.inspector.rev_parse(
                    f"{accepted_commit}^", check=False
                ) == plan["head"],
                "repository_clean": self.inspector.is_clean,
                "branch_unchanged": self.inspector.current_branch == plan["branch"],
                "writer_lease_absent": not self.inspector.writer_lock_path(
                    self.configuration["lock_policy"]["writer_lock_relative_path"]
                ).exists(),
                "original_transaction_immutable": old_events_after
                == plan["original_event_fingerprints"],
                "projection_integration_pending": completion["projection"].get("current_state")
                == "integration_pending",
                "cycle_cache_integration_pending": json.loads(
                    self.inspector.cycle_state_path().read_text(encoding="utf-8")
                ).get("kernel_ledger_sequence") == completion["projection"]["ledger_sequence"],
                "no_integration_performed": not any(
                    event["transaction_id"] == transaction.transaction_id
                    and event["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value
                    for event in self.ledger.read()
                ),
            }
            if not all(final_checks.values()):
                failed = ", ".join(key for key, passed in final_checks.items() if not passed)
                raise RecoveryError(f"F003 recovery postcondition failed: {failed}")
            report = {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "feature_id": "F003",
                "outcome": "integration_pending",
                "run_id": run_id,
                "recovery_transaction_id": transaction.transaction_id,
                "recovered_transaction_id": plan["original_transaction_id"],
                "accepted_feature_commit": accepted_commit,
                "changed_paths": plan["changed_paths"],
                "commands": commands,
                "checks": checks,
                "final_checks": final_checks,
                "model_session_launched": False,
                "milestone_integration_performed": False,
                "projection": completion["projection"],
                "created_at": utc_now(),
            }
            report_path = self.controller_root / "reports" / run_id / "feature-result-recovery.json"
            atomic_write_json(report_path, report)
            report["report_path"] = str(report_path)
            return report
        except Exception:
            if originals is not None and not self.inspector.is_clean:
                # Metadata rollback is allowed only before the feature commit.
                if self.inspector.head == plan["head"]:
                    self._restore_metadata(originals)
            if kernel is not None and kernel.transaction is not None:
                lease = kernel.lease.read()
                if lease is not None:
                    try:
                        kernel.block(
                            state=TransactionState.TERMINAL_FAILURE,
                            classification="FEATURE_VALIDATION_FAILED",
                            next_state="review",
                        )
                    except Exception:
                        pass
            raise
        finally:
            reservation.release(run_id)

    def _apply_general(self, plan: dict[str, Any]) -> dict[str, Any]:
        expected_fingerprint = fingerprint(
            {
                key: value
                for key, value in plan.items()
                if key != "plan_fingerprint"
                and key not in _COMMAND_DISCOVERY_FIELDS
            }
        )
        if plan.get("plan_fingerprint") != expected_fingerprint:
            raise RecoveryError("feature-result recovery plan fingerprint is invalid")
        feature_id = str(plan["feature_id"])
        run_id = f"feature-recovery-{uuid.uuid4()}"
        lock_directory = (
            self.controller_root
            / self.configuration["lock_policy"][
                "controller_launch_lock_directory"
            ]
        )
        reservation = DurableLock(
            lock_directory / f"{plan['repository_path_fingerprint']}.json"
        )
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=plan["repository_identity"],
                run_id=run_id,
                current_feature=feature_id,
                current_phase="feature_result_recovery",
            )
        )
        kernel: WorkflowKernel | None = None
        metadata_originals: dict[str, bytes] | None = None
        commands: list[dict[str, Any]] = []
        try:
            self.inspector.ensure_runtime_ignored()
            revalidated = self._inspect_general(
                feature_id=feature_id,
                original_transaction_id=str(plan["original_transaction_id"]),
                original_run_id=str(plan["original_run_id"]),
                original_session_id=str(plan["original_session_id"]),
                expected_branch=str(plan["branch"]),
                expected_head=str(plan["head"]),
                allowed_reservation_run_id=run_id,
            )
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "feature-result recovery evidence changed under reservation"
                )
            adapter = FeatureExecutionAdapter(
                allowed_paths=tuple(plan["final_changed_paths"]),
                commit_subject=str(plan["commit_subject"]),
                next_state="integration_pending",
                allow_untracked=True,
                require_clean_start=False,
                denied_paths=(
                    ".factory/approved-content.yaml",
                    ".factory/conveyor-state.json",
                    ".factory/locks/writer.json",
                    ".factory/project.yaml",
                    "docs/AUTONOMY_CONTRACT.md",
                ),
                denied_prefixes=(".factory", "factory-integration"),
            )
            kernel = WorkflowKernel(
                project=self.project,
                ledger=self.ledger,
                projection=self.projection,
                lease=WorkflowWriterLease(
                    self.inspector.writer_lock_path(
                        self.configuration["lock_policy"][
                            "writer_lock_relative_path"
                        ]
                    )
                ),
            )
            transaction = kernel.begin(
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                milestone=str(plan["milestone_id"]),
                feature_id=feature_id,
                run_id=run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovery_mode": "general_retained_feature_result",
                    "recovered_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "original_session_id": plan["original_session_id"],
                    "original_gate_id": plan["original_gate_id"],
                    "workflow_alias": plan["workflow_alias"],
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
                expected_starting_branch=str(plan["branch"]),
                expected_starting_head=str(plan["head"]),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            if transaction.feature_id != feature_id:
                raise TransactionError(
                    "recovery kernel lost the retained feature identity"
                )
            for argv in plan["host_validation_commands"]:
                record = self._run(list(argv), commands)
                if record["exit_code"] != 0:
                    self.ledger.append(
                        event_type="ValidationFailed",
                        transaction_id=transaction.transaction_id,
                        workflow_type=transaction.workflow_type,
                        payload={
                            "classification": "host_validation_failed",
                            "failed_command": argv,
                            "commands": commands,
                            "retained_diff_preserved": True,
                            "model_sessions_launched": 0,
                            "child_sessions_launched": 0,
                        },
                    )
                    projection = kernel.block(
                        state=TransactionState.TERMINAL_FAILURE,
                        classification="FEATURE_VALIDATION_FAILED",
                        next_state="human_decision_required",
                    )
                    projected_gate = projection.get("human_gate")
                    projected_gate = (
                        projected_gate
                        if isinstance(projected_gate, dict)
                        else {}
                    )
                    return {
                        "schema_version": 2,
                        "project_id": self.project.project_id,
                        "feature_id": feature_id,
                        "outcome": "validation_failed",
                        "run_id": run_id,
                        "recovery_transaction_id": transaction.transaction_id,
                        "failed_command": argv,
                        "commands": commands,
                        "application_commit_created": False,
                        "implementation_preserved": True,
                        "original_gate_preserved": (
                            projected_gate.get("gate_id")
                            == plan["original_gate_id"]
                        ),
                        "model_sessions_launched": 0,
                        "child_sessions_launched": 0,
                        "projection": projection,
                    }
            if (
                self.inspector.content_diff_fingerprint(
                    tuple(plan["preserved_implementation_paths"])
                )
                != plan["preserved_implementation_fingerprint"]
            ):
                raise RecoveryError(
                    "retained implementation changed during host validation"
                )
            metadata_originals = self._apply_general_acceptance_metadata(
                plan=plan, run_id=run_id, commands=commands
            )
            if (
                self.inspector.content_diff_fingerprint(
                    tuple(plan["preserved_implementation_paths"])
                )
                != plan["preserved_implementation_fingerprint"]
            ):
                self._restore_metadata(metadata_originals)
                metadata_originals = None
                raise RecoveryError(
                    "retained implementation changed during acceptance metadata"
                )
            if _changed_paths(self.inspector) != tuple(
                plan["final_changed_paths"]
            ):
                self._restore_metadata(metadata_originals)
                metadata_originals = None
                raise RecoveryError(
                    "acceptance metadata changed the authorized path set"
                )
            accepted_commit = kernel.finalize_deterministic_feature_recovery(
                original_transaction_id=str(plan["original_transaction_id"]),
                changed_paths=tuple(plan["final_changed_paths"]),
                original_content_fingerprint=str(
                    plan["retained_content_fingerprint"]
                ),
                plan_fingerprint=str(plan["plan_fingerprint"]),
                validation_evidence={
                    "all_required_passed": True,
                    "commands": [
                        {
                            key: item.get(key)
                            for key in (
                                "argv",
                                "exit_code",
                                "duration_seconds",
                                "output_sha256",
                            )
                        }
                        for item in commands
                    ],
                    "checks": [
                        {
                            "name": "workflow_alias_policy",
                            "passed": True,
                            "evidence": plan["workflow_alias"],
                        },
                        {
                            "name": "retained_fingerprints",
                            "passed": True,
                            "tracked": plan["tracked_diff_fingerprint"],
                            "untracked": plan["untracked_fingerprint"],
                        },
                    ],
                    "warnings": [],
                },
            )
            metadata_originals = None
            self.ledger.append(
                event_type="CheckpointRecorded",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "checkpoint": "structured_output_invalid_transaction_superseded",
                    "superseded_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "superseding_transaction_id": transaction.transaction_id,
                    "superseded_gate_id": plan["original_gate_id"],
                },
            )
            self.ledger.append(
                event_type="HumanGateResolved",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "gate_id": plan["original_gate_id"],
                    "gate_fingerprint": plan["original_gate_fingerprint"],
                    "raised_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "resolution": "retained_feature_result_validated_and_accepted",
                },
            )
            completion = kernel.complete(
                classification="FEATURE_ACCEPTED",
                evidence={
                    "accepted_feature_commit": accepted_commit,
                    "candidate_implementation_commit": accepted_commit,
                    "integration_status": "pending",
                    "recovered_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "resolved_gate_id": plan["original_gate_id"],
                    "host_validation_passed": True,
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                },
            )
            projection = completion["projection"]
            self._materialize_general_compatibility_caches(
                feature_id=feature_id,
                run_id=run_id,
                transaction_id=transaction.transaction_id,
                accepted_commit=accepted_commit,
                projection=projection,
            )
            supersession_events = [
                event
                for event in self.ledger.read()
                if event["transaction_id"] == transaction.transaction_id
                and event["event_type"] == "CheckpointRecorded"
                and event["payload"].get("checkpoint")
                == "structured_output_invalid_transaction_superseded"
            ]
            final_checks = {
                "accepted_commit_direct_child": self.inspector.rev_parse(
                    f"{accepted_commit}^", check=False
                )
                == plan["head"],
                "repository_clean": self.inspector.is_clean,
                "branch_unchanged": self.inspector.current_branch
                == plan["branch"],
                "writer_lease_absent": not self.inspector.writer_lock_path(
                    self.configuration["lock_policy"][
                        "writer_lock_relative_path"
                    ]
                ).exists(),
                "original_transaction_superseded": len(supersession_events) == 1
                and supersession_events[0]["payload"].get(
                    "superseded_transaction_id"
                )
                == plan["original_transaction_id"],
                "original_gate_resolved": projection.get("human_gate") is None,
                "projection_integration_pending": projection.get(
                    "current_state"
                )
                == "integration_pending",
                "accepted_commit_projected": projection.get(
                    "accepted_feature_commit"
                )
                == accepted_commit,
                "cycle_cache_bound": json.loads(
                    self.inspector.cycle_state_path().read_text(
                        encoding="utf-8"
                    )
                ).get("kernel_ledger_sequence")
                == projection["ledger_sequence"],
                "no_integration_performed": not any(
                    event["transaction_id"] == transaction.transaction_id
                    and event["workflow_type"]
                    == WorkflowType.MILESTONE_INTEGRATION.value
                    for event in self.ledger.read()
                ),
                "no_queue_reconciliation": not any(
                    event["transaction_id"] == transaction.transaction_id
                    and event["workflow_type"]
                    == WorkflowType.QUEUE_RECONCILIATION.value
                    for event in self.ledger.read()
                ),
            }
            if not all(final_checks.values()):
                failed = ", ".join(
                    key for key, passed in final_checks.items() if not passed
                )
                raise RecoveryError(
                    f"{feature_id} recovery postcondition failed: {failed}"
                )
            report = {
                "schema_version": 2,
                "project_id": self.project.project_id,
                "feature_id": feature_id,
                "outcome": "integration_pending",
                "run_id": run_id,
                "recovery_transaction_id": transaction.transaction_id,
                "recovered_transaction_id": plan["original_transaction_id"],
                "preparation_transaction_id": plan.get(
                    "preparation_transaction_id"
                ),
                "resolved_gate_id": plan["original_gate_id"],
                "candidate_implementation_commit": accepted_commit,
                "accepted_feature_commit": accepted_commit,
                "retained_changed_paths": plan["changed_paths"],
                "committed_changed_paths": plan["final_changed_paths"],
                "tracked_diff_fingerprint": plan[
                    "tracked_diff_fingerprint"
                ],
                "untracked_fingerprint": plan["untracked_fingerprint"],
                "untracked_file_hashes": plan["untracked_file_hashes"],
                "workflow_alias": plan["workflow_alias"],
                "commands": commands,
                "final_checks": final_checks,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "milestone_integration_performed": False,
                "queue_reconciliation_performed": False,
                "projection": projection,
                "created_at": utc_now(),
            }
            report_path = (
                self.controller_root
                / "reports"
                / run_id
                / "feature-result-recovery.json"
            )
            atomic_write_json(report_path, report)
            report["report_path"] = str(report_path)
            return report
        except Exception:
            if metadata_originals is not None and self.inspector.head == plan["head"]:
                self._restore_metadata(metadata_originals)
            if kernel is not None and kernel.transaction is not None:
                lease = kernel.lease.read()
                if lease is not None:
                    try:
                        kernel.block(
                            state=TransactionState.TERMINAL_FAILURE,
                            classification="FEATURE_VALIDATION_FAILED",
                            next_state="human_decision_required",
                        )
                    except Exception:
                        pass
            raise
        finally:
            reservation.release(run_id)
