"""Deterministic repair of an authenticated malformed compatibility cache."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .compatibility_cache import (
    build_controller_compatibility_cache,
    write_controller_compatibility_cache,
)
from .config import Configuration, load_json
from .contracts import fingerprint
from .cycle_cache import (
    _bind_terminal_cycle_cache,
    build_canonical_cycle_cache,
    completed_transition_cycle_updates,
    cycle_cache_semantic_mismatches,
    validated_canonical_projection_binding,
    write_terminal_cycle_cache,
)
from .errors import RecoveryError, SchemaValidationError
from .execution_plan import ExecutionPlan
from .feature_branches import canonical_feature_branch
from .integration_executor import load_integration_plan
from .ledger import EvidenceLedger
from .locks import DurableLock, make_lock_record
from .logging import atomic_write_bytes
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .validation import validate_schema


@dataclass(frozen=True)
class CacheRepairExpectation:
    project_id: str
    repository_identity: str
    repository_path_fingerprint: str
    branch: str
    head: str
    parent: str
    feature_id: str
    milestone_branch: str
    recovery_transaction_id: str
    original_transaction_id: str
    recovery_run_id: str
    ledger_sequence: int
    ledger_fingerprint: str
    projection_fingerprint: str
    malformed_cache_sha256: str
    application_cache_sha256: str
    unsupported_keys: tuple[str, ...]


INTERVIEW_COMPANION_F097_REPAIR = CacheRepairExpectation(
    project_id="interview-companion",
    repository_identity=(
        "9022b4a8018866b7a9c0862efb2b8664b7e0288afd5e3fbd22b107fa640ab299"
    ),
    repository_path_fingerprint=(
        "dd395b40fd8c69fffcde93bf15963740d4f914c01bffc69cd8d47a46c8d908e2"
    ),
    branch="codex/F097-imported-audio-transcription-workflow",
    head="560214ec85d149a46029976987e3ac5604c54217",
    parent="65c32f8f10d6569dafbcdee9a0fe0a230e0ecfba",
    feature_id="F097",
    milestone_branch="codex/m0-foundation",
    recovery_transaction_id="f5b7f4e8-ba7d-48f5-a6fc-03348000fc25",
    original_transaction_id="b8b6c22f-7c1a-4124-9dee-135647905331",
    recovery_run_id="feature-recovery-256e4092-9408-4fcc-be6e-628d9d7b1ccf",
    ledger_sequence=497,
    ledger_fingerprint=(
        "0a9638c7762b2f16188883c50c50d584e973ea1ead10051d64ab0127f17ca8c0"
    ),
    projection_fingerprint=(
        "922f90cf2e105827d1b8fa6aa25f4293eae119663361b171659f3729e2c0e28c"
    ),
    malformed_cache_sha256=(
        "cb62760034b638c551ddb04d22fed8bf28a0a7ab40cf20803307da599a506d4a"
    ),
    application_cache_sha256=(
        "9312a681abccea7a7b5e6bebfa93a46abda1dd279a1df5a0f89817aa96ed901c"
    ),
    unsupported_keys=(
        "accepted_feature_commit",
        "active_transaction",
        "current_run_id",
        "integration_status",
        "kernel_ledger_fingerprint",
        "kernel_ledger_sequence",
        "kernel_projection_fingerprint",
        "kernel_transaction_id",
        "selected_feature",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _prove_unreserved(path: Path) -> Iterator[None]:
    if not path.exists():
        yield
        return
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("controller recovery reservation is active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class CycleCacheRepair:
    """Inspect or repair one identity-pinned derived compatibility cache."""

    def __init__(
        self,
        *,
        controller_root: Path,
        configuration: Configuration,
        project: Project,
        expectation: CacheRepairExpectation | None = None,
    ):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration
        self.project = project
        self.expectation = expectation or INTERVIEW_COMPANION_F097_REPAIR
        self.inspector = RepositoryInspector(project.repository)
        self.state_root = (
            self.controller_root / "state/projects" / self.project.project_id
        )
        self.controller_cache_path = (
            self.controller_root
            / "state/projects"
            / f"{self.project.project_id}.json"
        )
        self.application_cache_path = self.inspector.cycle_state_path()
        self.project_schema = load_json(
            self.controller_root / "schemas/project-state.schema.json"
        )
        self.cycle_schema = load_json(
            self.controller_root / "schemas/cycle-state.schema.json"
        )

    def _launch_reservation(
        self, repository_path_fingerprint: str | None = None
    ) -> DurableLock:
        directory = self.configuration.owned_path(
            self.configuration.conveyor["lock_policy"][
                "controller_launch_lock_directory"
            ]
        )
        return DurableLock(
            directory
            / (
                f"{repository_path_fingerprint or self.expectation.repository_path_fingerprint}"
                ".json"
            )
        )

    def _load_raw_cache(self, path: Path, label: str) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RecoveryError(f"{label} is unavailable") from exc
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise RecoveryError(f"{label} is not a safe regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"{label} is unavailable or invalid JSON") from exc
        if not isinstance(value, dict):
            raise RecoveryError(f"{label} is not an object")
        return value

    def _authoritative_context(
        self,
    ) -> tuple[dict[str, Any], ExecutionPlan, EvidenceLedger]:
        expected = self.expectation
        identity = self.inspector.identity()
        if identity["repository_id"] != expected.repository_identity:
            raise RecoveryError("application repository identity changed")
        if identity["path_fingerprint"] != expected.repository_path_fingerprint:
            raise RecoveryError("application repository path fingerprint changed")
        ledger = EvidenceLedger(
            self.state_root / "evidence-ledger.jsonl",
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection_engine = ProjectionEngine(
            ledger, self.state_root / "projection-cache.json"
        )
        binding = validated_canonical_projection_binding(
            ledger=ledger,
            projection_engine=projection_engine,
            transaction_id=expected.recovery_transaction_id,
        )
        projection = binding.canonical_projection
        exact_projection = {
            "project_id": expected.project_id,
            "current_state": "integration_pending",
            "current_feature": expected.feature_id,
            "accepted_feature_commit": expected.head,
            "integration_status": "pending",
            "active_transaction": None,
            "human_gate": None,
            "selected_next_feature": None,
            "allowed_next_action": "milestone_integration",
            "session_resume_eligible": False,
            "ledger_sequence": expected.ledger_sequence,
            "ledger_fingerprint": expected.ledger_fingerprint,
            "projection_fingerprint": expected.projection_fingerprint,
        }
        changed = sorted(
            key for key, value in exact_projection.items()
            if projection.get(key) != value
        )
        if changed:
            raise RecoveryError(
                "authoritative projection identity changed: " + ", ".join(changed)
            )
        execution_plan = ExecutionPlan.from_projection(
            projection,
            starting_commit=expected.parent,
            feature_branch=expected.branch,
            milestone_branch=expected.milestone_branch,
        )
        if (
            execution_plan.feature_id != expected.feature_id
            or execution_plan.accepted_commit != expected.head
            or execution_plan.starting_commit != expected.parent
            or execution_plan.feature_branch != expected.branch
            or execution_plan.milestone_branch != expected.milestone_branch
        ):
            raise RecoveryError("authoritative execution identity changed")
        transactions = {
            item.get("transaction_id"): item
            for item in projection.get("transactions", [])
            if isinstance(item, dict)
        }
        recovery = transactions.get(expected.recovery_transaction_id) or {}
        original = transactions.get(expected.original_transaction_id) or {}
        if (
            recovery.get("run_id") != expected.recovery_run_id
            or recovery.get("feature_id") != expected.feature_id
            or recovery.get("state") != "completed"
            or recovery.get("terminal_reference") != expected.head
            or (recovery.get("starting_snapshot") or {}).get("head")
            != expected.parent
            or (recovery.get("terminal_snapshot") or {}).get("head")
            != expected.head
            or original.get("feature_id") != expected.feature_id
            or original.get("state") != "human_decision_required"
        ):
            raise RecoveryError("feature-result recovery transaction identity changed")
        recovered_links = [
            event
            for event in ledger.read()
            if event.get("transaction_id") == expected.recovery_transaction_id
            and (event.get("payload") or {}).get("recovered_transaction_id")
            == expected.original_transaction_id
        ]
        if not recovered_links:
            raise RecoveryError("recovery transaction is not bound to the original failure")
        return projection, execution_plan, ledger

    def _repository_checks(self, *, allowed_reservation_run_id: str | None) -> None:
        expected = self.expectation
        if self.project.project_id != expected.project_id:
            raise RecoveryError("repair command is not pinned to this project")
        if self.inspector.current_branch != expected.branch:
            raise RecoveryError("application branch changed")
        if self.inspector.head != expected.head:
            raise RecoveryError("application HEAD changed")
        if self.inspector.rev_parse(f"{expected.head}^") != expected.parent:
            raise RecoveryError("accepted feature commit parent changed")
        if self.inspector.rev_parse(expected.milestone_branch, check=False) != expected.parent:
            raise RecoveryError("milestone branch no longer identifies the feature parent")
        if not self.inspector.is_clean:
            raise RecoveryError("application worktree is dirty")
        if any(self.inspector.git_operation_state().values()):
            raise RecoveryError("application repository has an unfinished Git operation")
        writer = self.inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"][
                "writer_lock_relative_path"
            ]
        )
        if writer.exists() or writer.is_symlink():
            raise RecoveryError("application writer lease is present")
        reservation = self._launch_reservation().status(allowed_reservation_run_id)
        if reservation.exists and not reservation.owned_by_run:
            raise RecoveryError("controller launch reservation is present")

    def _inspect_controller_compatibility_cache(
        self, *, allowed_reservation_run_id: str | None = None
    ) -> dict[str, Any]:
        expected = self.expectation
        self._repository_checks(
            allowed_reservation_run_id=allowed_reservation_run_id
        )
        with _prove_unreserved(self.state_root / ".recovery-takeover.lock"):
            projection, execution_plan, _ = self._authoritative_context()
            raw = self._load_raw_cache(
                self.controller_cache_path, "controller compatibility cache"
            )
            application = self._load_raw_cache(
                self.application_cache_path, "application cycle cache"
            )
            validate_schema(application, self.cycle_schema)
            application_sha = _sha256(self.application_cache_path)
            if application_sha != expected.application_cache_sha256:
                raise RecoveryError("application cycle-cache SHA-256 changed")
            application_binding = {
                "project_id": application.get("project_id"),
                "current_feature": application.get("current_feature"),
                "current_phase": application.get("current_phase"),
                "feature_branch": application.get("feature_branch"),
                "feature_starting_commit": application.get(
                    "feature_starting_commit"
                ),
                "accepted_feature_commit": application.get(
                    "accepted_feature_commit"
                ),
                "milestone_branch": application.get("milestone_branch"),
                "next_safe_action": application.get("next_safe_action"),
                "kernel_transaction_id": application.get("kernel_transaction_id"),
                "kernel_ledger_sequence": application.get(
                    "kernel_ledger_sequence"
                ),
                "kernel_ledger_fingerprint": application.get(
                    "kernel_ledger_fingerprint"
                ),
                "kernel_projection_fingerprint": application.get(
                    "kernel_projection_fingerprint"
                ),
                "human_decision_required": application.get(
                    "human_decision_required"
                ),
                "session_id": application.get("session_id"),
                "conveyor_run_id": application.get("conveyor_run_id"),
            }
            expected_application_binding = {
                "project_id": expected.project_id,
                "current_feature": expected.feature_id,
                "current_phase": "integration_pending",
                "feature_branch": expected.branch,
                "feature_starting_commit": expected.parent,
                "accepted_feature_commit": expected.head,
                "milestone_branch": expected.milestone_branch,
                "next_safe_action": "milestone_integration",
                "kernel_transaction_id": expected.recovery_transaction_id,
                "kernel_ledger_sequence": expected.ledger_sequence,
                "kernel_ledger_fingerprint": expected.ledger_fingerprint,
                "kernel_projection_fingerprint": expected.projection_fingerprint,
                "human_decision_required": None,
                "session_id": None,
                "conveyor_run_id": expected.recovery_run_id,
            }
            if application_binding != expected_application_binding:
                raise RecoveryError("application cycle-cache binding changed")

            malformed_sha = _sha256(self.controller_cache_path)
            unsupported = tuple(
                sorted(
                    set(raw)
                    - set((self.project_schema.get("properties") or {}).keys())
                )
            )
            normalized = {
                key: value
                for key, value in raw.items()
                if key not in expected.unsupported_keys
            }
            already_canonical = False
            try:
                validate_schema(raw, self.project_schema)
            except SchemaValidationError:
                if malformed_sha != expected.malformed_cache_sha256:
                    raise RecoveryError("malformed cache SHA-256 changed")
                if unsupported != expected.unsupported_keys:
                    raise RecoveryError("malformed cache unsupported-key set changed")
                validate_schema(normalized, self.project_schema)
                if (
                    raw.get("kernel_transaction_id")
                    != expected.recovery_transaction_id
                    or raw.get("current_run_id") != expected.recovery_run_id
                ):
                    raise RecoveryError("malformed cache recovery provenance changed")
            else:
                unsupported = ()
                already_canonical = True

            replacement = build_controller_compatibility_cache(
                raw,
                project_schema=self.project_schema,
                project_id=expected.project_id,
                repository_fingerprint=expected.repository_path_fingerprint,
                active_milestone=self.project.active_milestone,
                run_id=expected.recovery_run_id,
                projection=projection,
                execution_plan=execution_plan.to_dict(),
                updated_at=str(raw.get("updated_at")),
            )
            if already_canonical and raw != replacement:
                raise RecoveryError(
                    "schema-valid controller cache differs from canonical projection"
                )
            app_tracked = self.inspector.git(
                ["ls-files", "--error-unmatch", ".factory/conveyor-state.json"],
                check=False,
            ).returncode == 0
            if app_tracked:
                raise RecoveryError("application cycle cache unexpectedly became Git-tracked")
            app_ignored = self.inspector.git(
                ["check-ignore", "-q", ".factory/conveyor-state.json"],
                check=False,
            ).returncode == 0
            if not app_ignored:
                raise RecoveryError("application cycle cache is not runtime-ignored")
            plan = {
                "schema_version": 1,
                "project_id": expected.project_id,
                "cache_kind": "controller_compatibility_cache",
                "mutation_target": "controller_compatibility_cache",
                "outcome": (
                    "already_canonical" if already_canonical else "repair_ready"
                ),
                "dry_run": True,
                "malformed_cache_path": str(self.controller_cache_path),
                "malformed_cache_sha256": malformed_sha,
                "unsupported_keys": list(unsupported),
                "application_cycle_cache": {
                    "path": str(self.application_cache_path),
                    "sha256": application_sha,
                    "schema_valid": True,
                    "will_change": False,
                },
                "authoritative_projection": {
                    "current_state": projection["current_state"],
                    "current_feature": projection["current_feature"],
                    "accepted_feature_commit": projection[
                        "accepted_feature_commit"
                    ],
                    "integration_status": projection["integration_status"],
                    "active_transaction": projection["active_transaction"],
                    "human_gate": projection["human_gate"],
                    "selected_next_feature": projection[
                        "selected_next_feature"
                    ],
                    "ledger_sequence": projection["ledger_sequence"],
                    "ledger_fingerprint": projection["ledger_fingerprint"],
                    "projection_fingerprint": projection[
                        "projection_fingerprint"
                    ],
                },
                "canonical_replacement": replacement,
                "replacement_schema_valid": True,
                "status_would_parse": True,
                "verify_consistency_would_parse": True,
                "ledger_will_change": False,
                "projection_will_change": False,
                "application_git_tracked_paths_will_change": [],
                "controller_paths_that_would_change": (
                    [] if already_canonical else [str(self.controller_cache_path)]
                ),
                "model_sessions_that_would_launch": 0,
                "child_sessions_that_would_launch": 0,
                "integration_would_begin": False,
            }
            plan["plan_fingerprint"] = fingerprint(plan)
            return plan

    def _common_repository_checks(
        self,
        *,
        identity: dict[str, Any],
        allowed_reservation_run_id: str | None,
    ) -> None:
        if not self.inspector.is_clean:
            raise RecoveryError("application worktree is dirty")
        if any(self.inspector.git_operation_state().values()):
            raise RecoveryError("application repository has an unfinished Git operation")
        writer = self.inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"][
                "writer_lock_relative_path"
            ]
        )
        if writer.exists() or writer.is_symlink():
            raise RecoveryError("application writer lease is present")
        reservation = self._launch_reservation(
            identity["path_fingerprint"]
        ).status(allowed_reservation_run_id)
        if reservation.exists and not reservation.owned_by_run:
            raise RecoveryError("controller launch reservation is present")

    def _inspect_application_transition(
        self, *, allowed_reservation_run_id: str | None = None
    ) -> dict[str, Any] | None:
        """Recognize a schema-valid cache immediately preceding integration."""

        identity = self.inspector.identity()
        self._common_repository_checks(
            identity=identity,
            allowed_reservation_run_id=allowed_reservation_run_id,
        )
        application = self._load_raw_cache(
            self.application_cache_path, "application cycle cache"
        )
        validate_schema(application, self.cycle_schema)
        unsigned = dict(application)
        claimed_signature = unsigned.pop("kernel_cache_fingerprint", None)
        if claimed_signature != fingerprint(unsigned):
            raise RecoveryError("application cycle-cache signature is invalid")
        app_tracked = self.inspector.git(
            ["ls-files", "--error-unmatch", ".factory/conveyor-state.json"],
            check=False,
        ).returncode == 0
        if app_tracked:
            raise RecoveryError("application cycle cache unexpectedly became Git-tracked")
        app_ignored = self.inspector.git(
            ["check-ignore", "-q", ".factory/conveyor-state.json"],
            check=False,
        ).returncode == 0
        if not app_ignored:
            raise RecoveryError("application cycle cache is not runtime-ignored")

        ledger = EvidenceLedger(
            self.state_root / "evidence-ledger.jsonl",
            project_id=self.project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        events = ledger.read()
        projection_engine = ProjectionEngine(
            ledger, self.state_root / "projection-cache.json"
        )
        projection = projection_engine.rebuild(persist_cache=False)
        transactions = [
            item
            for item in projection.get("transactions", [])
            if isinstance(item, dict)
            and isinstance(item.get("transaction_id"), str)
            and isinstance(item.get("last_sequence"), int)
        ]
        if not transactions:
            return None
        latest = max(transactions, key=lambda item: item["last_sequence"])
        latest_transaction = str(latest["transaction_id"])
        terminal = ledger.terminal_event(latest_transaction)
        stale_sequence = application.get("kernel_ledger_sequence")
        latest_is_completed_integration = bool(
            latest.get("workflow_type") == "milestone_integration"
            and latest.get("state") == "completed"
            and latest.get("terminal_classification") == "INTEGRATED"
            and isinstance(terminal, dict)
            and terminal.get("event_type") == "TransactionCompleted"
        )
        if stale_sequence == projection.get("ledger_sequence"):
            if not latest_is_completed_integration:
                return None
            binding = validated_canonical_projection_binding(
                ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=latest_transaction,
            )
            if (
                application.get("kernel_transaction_id") != latest_transaction
                or application.get("kernel_ledger_fingerprint")
                != binding.ledger_fingerprint
                or application.get("kernel_projection_fingerprint")
                != binding.projection_fingerprint
                or cycle_cache_semantic_mismatches(application, projection)
            ):
                raise RecoveryError(
                    "schema-valid application cache differs from canonical projection"
                )
            plan = {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "cache_kind": "application_cycle_cache",
                "mutation_target": "application_cycle_cache",
                "outcome": "already_canonical",
                "dry_run": True,
                "stale_cache_path": str(self.application_cache_path),
                "stale_cache_sha256": _sha256(self.application_cache_path),
                "old_ledger_sequence": stale_sequence,
                "new_ledger_sequence": binding.ledger_sequence,
                "completed_integration_transaction": latest_transaction,
                "terminal_milestone_head": self.inspector.head,
                "canonical_replacement": application,
                "replacement_schema_valid": True,
                "future_mutations": [],
                "application_git_tracked_paths_will_change": [],
                "ledger_will_change": False,
                "projection_will_change": False,
                "model_sessions_that_would_launch": 0,
                "child_sessions_that_would_launch": 0,
                "queue_reconciliation_would_begin": False,
            }
            plan["plan_fingerprint"] = fingerprint(plan)
            return plan
        if not isinstance(stale_sequence, int) or stale_sequence >= int(
            projection.get("ledger_sequence", -1)
        ):
            return None
        if (
            not latest_is_completed_integration
        ):
            raise RecoveryError(
                "stale application cache is not followed by a completed integration"
            )
        terminal_payload = terminal.get("payload") or {}
        terminal_snapshot = terminal_payload.get("terminal_snapshot") or {}
        if (
            projection.get("active_transaction") is not None
            or projection.get("required_lease") is not None
            or projection.get("human_gate") is not None
            or terminal_snapshot.get("clean") is not True
            or terminal_snapshot.get("branch") != self.project.milestone_branch
            or terminal_snapshot.get("head") != self.inspector.head
            or self.inspector.current_branch != self.project.milestone_branch
        ):
            raise RecoveryError(
                "completed integration terminal repository state is not canonical"
            )

        integration_events = [
            event
            for event in events
            if event.get("transaction_id") == latest_transaction
        ]
        started = next(
            (
                event
                for event in integration_events
                if event.get("event_type") == "TransactionStarted"
            ),
            None,
        )
        if not isinstance(started, dict):
            raise RecoveryError("completed integration has no starting evidence")
        start_payload = started.get("payload") or {}
        preceding = [
            item
            for item in transactions
            if item["transaction_id"] != latest_transaction
            and item["last_sequence"] < started["sequence"]
        ]
        if not preceding:
            raise RecoveryError("completed integration has no immediately preceding state")
        previous = max(preceding, key=lambda item: item["last_sequence"])
        previous_terminal = ledger.terminal_event(str(previous["transaction_id"]))
        previous_payload = (previous_terminal or {}).get("payload") or {}
        previous_snapshot = previous_payload.get("terminal_snapshot") or {}
        accepted_commit = terminal_payload.get("accepted_feature_commit")
        feature_id = terminal_payload.get("feature_id")
        if (
            application.get("kernel_transaction_id")
            != previous.get("transaction_id")
            or stale_sequence != previous.get("last_sequence")
            or previous.get("state") != "completed"
            or previous.get("terminal_classification") != "FEATURE_ACCEPTED"
            or previous_payload.get("feature_id") != feature_id
            or previous_payload.get("accepted_feature_commit") != accepted_commit
            or previous_snapshot.get("branch") != application.get("feature_branch")
            or previous_snapshot.get("head") != accepted_commit
            or application.get("accepted_feature_commit") != accepted_commit
            or application.get("current_feature") != feature_id
            or application.get("current_phase") != "integration_pending"
        ):
            raise RecoveryError(
                "stale application cache does not match the immediately preceding state"
            )
        prior_event = next(
            (event for event in events if event.get("sequence") == stale_sequence),
            None,
        )
        if (
            not isinstance(prior_event, dict)
            or prior_event.get("fingerprint")
            != application.get("kernel_ledger_fingerprint")
        ):
            raise RecoveryError("stale cache ledger binding is not in the evidence chain")

        plan_path = self.state_root / "integration-plans" / f"{latest_transaction}.json"
        integration_plan = load_integration_plan(plan_path)
        if (
            integration_plan.get("transaction_id") != latest_transaction
            or integration_plan.get("feature_id") != feature_id
            or integration_plan.get("feature_branch")
            != application.get("feature_branch")
            or integration_plan.get("accepted_commit") != accepted_commit
            or integration_plan.get("milestone_branch")
            != self.project.milestone_branch
            or integration_plan.get("pre_integration_head")
            != start_payload.get("starting_head")
            or terminal_payload.get("integrated_commit")
            != (
                next(
                    (
                        event.get("payload", {}).get("resulting_feature_commit")
                        for event in integration_events
                        if event.get("event_type") == "CommitFinalized"
                    ),
                    None,
                )
            )
        ):
            raise RecoveryError("completed integration transaction is contradictory")
        binding = validated_canonical_projection_binding(
            ledger=ledger,
            projection_engine=projection_engine,
            transaction_id=latest_transaction,
        )
        selected_id = projection.get("selected_next_feature")
        queue = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        selected_feature = queue.feature(selected_id) if selected_id else None
        selected_branch = (
            canonical_feature_branch(self.project, selected_feature)
            if selected_feature is not None
            else None
        )
        updates = completed_transition_cycle_updates(
            projection=projection,
            run_id=str(latest.get("run_id")),
            milestone_branch=str(self.project.milestone_branch),
            pre_transition_head=str(start_payload.get("starting_head")),
            terminal_snapshot=terminal_snapshot,
            queue_fingerprint=str(terminal_snapshot.get("queue_fingerprint")),
            selected_feature=selected_feature,
            selected_feature_branch=selected_branch,
            dependency_statuses={
                dependency: (queue.feature(dependency) or {}).get("status")
                for dependency in (
                    selected_feature.get("dependencies", [])
                    if selected_feature is not None
                    else []
                )
            },
            checkpoint="milestone_integration_terminal",
            updated_at=str(terminal.get("timestamp")),
        )
        canonical = build_canonical_cycle_cache(
            application,
            cycle_schema=self.cycle_schema,
            updates=updates,
            projection=projection,
        )
        replacement = _bind_terminal_cycle_cache(
            canonical,
            ledger=ledger,
            binding=binding,
            transaction_id=latest_transaction,
            expected_feature=selected_id,
        )
        validate_schema(replacement, self.cycle_schema)
        stale_sha = _sha256(self.application_cache_path)
        plan = {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "cache_kind": "application_cycle_cache",
            "mutation_target": "application_cycle_cache",
            "outcome": "repair_ready",
            "dry_run": True,
            "stale_cache_path": str(self.application_cache_path),
            "stale_cache_sha256": stale_sha,
            "old_ledger_sequence": stale_sequence,
            "new_ledger_sequence": binding.ledger_sequence,
            "completed_integration_transaction": latest_transaction,
            "completed_feature": feature_id,
            "accepted_feature_commit": accepted_commit,
            "integrated_implementation_commit": terminal_payload.get(
                "integrated_commit"
            ),
            "terminal_milestone_head": terminal_snapshot.get("head"),
            "authoritative_projection": {
                key: projection.get(key)
                for key in (
                    "current_state",
                    "current_feature",
                    "accepted_feature_commit",
                    "integration_status",
                    "active_transaction",
                    "human_gate",
                    "selected_next_feature",
                    "ledger_sequence",
                    "allowed_next_action",
                )
            },
            "canonical_replacement": replacement,
            "replacement_schema_valid": True,
            "future_mutations": [".factory/conveyor-state.json"],
            "application_git_tracked_paths_will_change": [],
            "controller_paths_that_would_change": [],
            "ledger_will_change": False,
            "projection_will_change": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "queue_reconciliation_would_begin": False,
        }
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def inspect(
        self, *, allowed_reservation_run_id: str | None = None
    ) -> dict[str, Any]:
        if (
            self.inspector.current_branch == self.expectation.branch
            and self.inspector.head == self.expectation.head
        ):
            return self._inspect_controller_compatibility_cache(
                allowed_reservation_run_id=allowed_reservation_run_id
            )
        with _prove_unreserved(self.state_root / ".recovery-takeover.lock"):
            application_plan = self._inspect_application_transition(
                allowed_reservation_run_id=allowed_reservation_run_id
            )
        if application_plan is not None:
            return application_plan
        raise RecoveryError(
            "no recognized canonical cycle-cache repair topology exists"
        )

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        claimed = plan.get("plan_fingerprint")
        if claimed != fingerprint(
            {key: value for key, value in plan.items() if key != "plan_fingerprint"}
        ):
            raise RecoveryError("cycle-cache repair plan fingerprint is invalid")
        if plan.get("mutation_target") == "application_cycle_cache":
            return self._apply_application_transition(plan)
        if plan.get("outcome") == "already_canonical":
            return {
                **plan,
                "dry_run": False,
                "outcome": "already_canonical",
                "controller_cache_written": False,
            }
        run_id = f"cycle-cache-repair-{uuid.uuid4()}"
        reservation = self._launch_reservation()
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=self.expectation.repository_identity,
                run_id=run_id,
                current_feature=self.expectation.feature_id,
                current_phase="cycle_cache_repair",
            )
        )
        original = self.controller_cache_path.read_bytes()
        original_mode = stat.S_IMODE(self.controller_cache_path.stat().st_mode)
        ledger_before = (
            self.state_root / "evidence-ledger.jsonl"
        ).read_bytes()
        projection_before = (
            self.state_root / "projection-cache.json"
        ).read_bytes()
        application_before = self.application_cache_path.read_bytes()
        git_before = self.inspector.git(
            ["status", "--porcelain=v1", "--branch"]
        ).stdout
        written = False
        try:
            revalidated = self.inspect(allowed_reservation_run_id=run_id)
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "cycle-cache repair evidence changed under reservation"
                )
            replacement = revalidated["canonical_replacement"]
            try:
                write_controller_compatibility_cache(
                    self.controller_cache_path,
                    replacement,
                    project_schema=self.project_schema,
                )
                written = True
            except Exception:
                if self.controller_cache_path.read_bytes() != original:
                    atomic_write_bytes(
                        self.controller_cache_path, original, mode=original_mode
                    )
                raise
            if (
                (self.state_root / "evidence-ledger.jsonl").read_bytes()
                != ledger_before
                or (self.state_root / "projection-cache.json").read_bytes()
                != projection_before
            ):
                raise RecoveryError("ledger or projection changed during cache repair")
            if self.application_cache_path.read_bytes() != application_before:
                raise RecoveryError("application cycle cache changed during cache repair")
            if (
                self.inspector.git(["status", "--porcelain=v1", "--branch"]).stdout
                != git_before
            ):
                raise RecoveryError("application Git state changed during cache repair")
            return {
                **revalidated,
                "dry_run": False,
                "outcome": "repaired",
                "controller_cache_written": True,
                "preserved_mode": oct(original_mode),
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "integration_started": False,
            }
        except Exception:
            if written and self.controller_cache_path.read_bytes() != original:
                atomic_write_bytes(
                    self.controller_cache_path, original, mode=original_mode
                )
            raise
        finally:
            reservation.release(run_id)

    def _apply_application_transition(
        self, plan: dict[str, Any]
    ) -> dict[str, Any]:
        if plan.get("outcome") == "already_canonical":
            return {
                **plan,
                "dry_run": False,
                "outcome": "already_canonical",
                "application_cache_written": False,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
            }
        identity = self.inspector.identity()
        run_id = f"runtime-cache-repair-{uuid.uuid4()}"
        reservation = self._launch_reservation(identity["path_fingerprint"])
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=identity["repository_id"],
                run_id=run_id,
                current_feature=plan.get("completed_feature"),
                current_phase="runtime_cache_repair",
            )
        )
        original = self.application_cache_path.read_bytes()
        original_mode = stat.S_IMODE(self.application_cache_path.stat().st_mode)
        ledger_path = self.state_root / "evidence-ledger.jsonl"
        projection_path = self.state_root / "projection-cache.json"
        ledger_before = ledger_path.read_bytes()
        projection_before = projection_path.read_bytes()
        controller_before = self.controller_cache_path.read_bytes()
        git_before = self.inspector.git(
            ["status", "--porcelain=v1", "--branch"]
        ).stdout
        try:
            revalidated = self.inspect(allowed_reservation_run_id=run_id)
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "cycle-cache repair evidence changed under reservation"
                )
            if _sha256(self.application_cache_path) != plan.get(
                "stale_cache_sha256"
            ):
                raise RecoveryError("application cycle-cache SHA-256 changed")
            ledger = EvidenceLedger(
                ledger_path,
                project_id=self.project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            replacement = revalidated["canonical_replacement"]
            persisted = write_terminal_cycle_cache(
                self.application_cache_path,
                replacement,
                ledger=ledger,
                projection_engine=ProjectionEngine(ledger, projection_path),
                transaction_id=str(
                    revalidated["completed_integration_transaction"]
                ),
                expected_feature=(
                    revalidated["authoritative_projection"].get(
                        "selected_next_feature"
                    )
                ),
                cycle_schema=self.cycle_schema,
                mode=original_mode,
            )
            if persisted != replacement:
                raise RecoveryError(
                    "written application cache differs from canonical replacement"
                )
            if (
                ledger_path.read_bytes() != ledger_before
                or projection_path.read_bytes() != projection_before
            ):
                raise RecoveryError("ledger or projection changed during cache repair")
            if self.controller_cache_path.read_bytes() != controller_before:
                raise RecoveryError(
                    "controller compatibility cache changed during application cache repair"
                )
            if (
                self.inspector.git(["status", "--porcelain=v1", "--branch"]).stdout
                != git_before
            ):
                raise RecoveryError(
                    "application Git state changed during cache repair"
                )
            if stat.S_IMODE(self.application_cache_path.stat().st_mode) != original_mode:
                raise RecoveryError(
                    "application cycle-cache permissions changed during repair"
                )
            final = self.inspect(allowed_reservation_run_id=run_id)
            if final.get("outcome") != "already_canonical":
                raise RecoveryError("application cycle cache is not canonical after repair")
            return {
                **final,
                "dry_run": False,
                "outcome": "repaired",
                "application_cache_written": True,
                "preserved_mode": oct(original_mode),
                "runtime_cache_repair_reservation": run_id,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "queue_reconciliation_started": False,
                "application_commits_created": 0,
            }
        except Exception:
            if self.application_cache_path.read_bytes() != original:
                atomic_write_bytes(
                    self.application_cache_path, original, mode=original_mode
                )
            raise
        finally:
            reservation.release(run_id)
