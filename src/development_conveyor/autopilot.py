"""Durable, controller-owned continuous feature-delivery coordination."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Configuration
from .consistency import ConsistencyChecker
from .cycle_engine import CycleEngine
from .cycle_cache_repair import CycleCacheRepair
from .feature_result_recovery import FeatureResultRecovery
from .retained_feature_repair import (
    RetainedFeatureRepairRecovery,
    RetainedFeatureValidationRepair,
)
from .accepted_commit_recovery import AcceptedCommitRecovery
from .feature_prelaunch_recovery import FeaturePrelaunchRecovery
from .errors import (
    AmbiguousLockError,
    AutopilotStopRequested,
    ConveyorError,
    LockError,
    RecoveryError,
)
from .locks import process_alive, read_lock
from .logging import atomic_write_json, utc_now
from .redaction import redact_text
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import process_start_evidence


AUTOPILOT_EVENTS = frozenset({
    "AUTOPILOT_STARTED",
    "FEATURE_SELECTED",
    "FEATURE_CONTEXT_STARTED",
    "FEATURE_CONTEXT_READY",
    "FEATURE_SESSION_STARTED",
    "FEATURE_ACCEPTED",
    "FEATURE_INTEGRATED",
    "FEATURE_BLOCKED",
    "RECOVERY_STARTED",
    "RECOVERY_APPLIED",
    "FEATURE_REPAIR_STARTED",
    "FEATURE_REPAIR_VALIDATION_STARTED",
    "STOP_REQUESTED",
    "AUTOPILOT_STOPPED",
    "AUTOPILOT_COMPLETED",
    "AUTOPILOT_FAILED",
})

CANONICAL_ACTION_ALIASES = {
    "feature_execution": "feature_cycle",
    "integration_ready": "milestone_integration",
    "planning_recovery": "planning_finalization",
}

DETERMINISTIC_RECOVERY_ROUTES = (
    "cache_binding_recovery",
    "cycle_cache_repair",
    "planning_finalization",
    "feature_result_recovery",
    "retained_feature_repair_recovery",
    "accepted_commit_recovery",
    "feature_prelaunch_recovery",
    "integration_finalization_recovery",
    "kernel_recovery",
    "authenticated_writer_lease_recovery",
    "trusted_host_validation",
)

MODEL_BACKED_REPAIR_ROUTES = ("retained_feature_repair",)

DEFAULT_RETRY_BUDGETS = {
    "implementation_retries_per_feature": 3,
    "validation_retries": 2,
    "deterministic_recovery_attempts": 3,
    "planning_retries": 2,
    "integration_finalization_retries": 2,
}


@dataclass(frozen=True)
class AutopilotPaths:
    root: Path
    ownership: Path
    stop_request: Path
    status: Path

    @classmethod
    def for_project(cls, configuration: Configuration, project_id: str) -> "AutopilotPaths":
        state = configuration.owned_path(configuration.conveyor["state_directory"])
        reports = configuration.owned_path(configuration.conveyor["report_directory"])
        root = state / "autopilot" / project_id
        return cls(
            root=root,
            ownership=root / "ownership.json",
            stop_request=root / "stop-request.json",
            status=reports / "autopilot" / project_id / "latest.json",
        )


class AutopilotOwnership:
    """One exact local process owns a project's continuous loop."""

    def __init__(self, paths: AutopilotPaths, project: Project):
        self.paths = paths
        self.project = project
        self.run_id: str | None = None

    def _record(self, run_id: str, last_event: str | None) -> dict[str, Any]:
        inspector = RepositoryInspector(self.project.repository)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": self.project.project_id,
            "repository": str(self.project.repository.resolve()),
            "repository_identity": inspector.identity()["repository_id"],
            "repository_path_fingerprint": inspector.identity()["path_fingerprint"],
            "repository_branch": inspector.current_branch,
            "repository_head": inspector.head,
            "process_id": os.getpid(),
            "process_start_identity": process_start_evidence(os.getpid()),
            "host": socket.gethostname(),
            "created_at": utc_now(),
            "last_heartbeat": utc_now(),
            "last_completed_event": last_event,
            "ledger_sequence": None,
            "ledger_fingerprint": None,
            "projection_fingerprint": None,
        }

    def _authenticate_dead_owner(self, record: dict[str, Any]) -> None:
        inspector = RepositoryInspector(self.project.repository)
        identity = inspector.identity()
        checks = {
            "project": record.get("project_id") == self.project.project_id,
            "repository": record.get("repository") == str(self.project.repository.resolve()),
            "repository_identity": record.get("repository_identity") == identity["repository_id"],
            "path_fingerprint": record.get("repository_path_fingerprint") == identity["path_fingerprint"],
            "host": record.get("host") == socket.gethostname(),
            "run_id": isinstance(record.get("run_id"), str) and bool(record.get("run_id")),
            "process_start": isinstance(record.get("process_start_identity"), str)
            and bool(record.get("process_start_identity")),
            "last_event": record.get("last_completed_event") is None
            or record.get("last_completed_event") in AUTOPILOT_EVENTS,
            "repository_branch": isinstance(record.get("repository_branch"), str)
            and bool(record.get("repository_branch")),
            "repository_head": isinstance(record.get("repository_head"), str)
            and bool(record.get("repository_head")),
            "repository_clean": inspector.is_clean,
            "git_operation_absent": not any(inspector.git_operation_state().values()),
        }
        pid = record.get("process_id")
        checks["pid"] = isinstance(pid, int) and not isinstance(pid, bool)
        checks["process_dead"] = checks["pid"] and process_alive(pid) is False
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise AmbiguousLockError(
                f"existing autopilot ownership cannot be authenticated as recoverable: {failed}"
            )
        writer_path = inspector.writer_lock_path(".factory/locks/writer.json")
        writer = read_lock(writer_path)
        if writer is None:
            return
        writer_pid = writer.get("owner_pid") or writer.get("pid")
        writer_host = writer.get("owner_host") or writer.get("host")
        writer_project = writer.get("project_id")
        writer_repository = writer.get("repository_path") or writer.get("repository")
        writer_start = writer.get("owner_process_start") or writer.get("start_time")
        authenticated = bool(
            writer_host == socket.gethostname()
            and writer_project == self.project.project_id
            and writer_repository == str(self.project.repository.resolve())
            and isinstance(writer_pid, int)
            and writer_pid == record.get("process_id")
            and process_alive(writer_pid) is False
            and isinstance(writer_start, str)
            and writer_start
            and writer_start == record.get("process_start_identity")
        )
        if not authenticated:
            raise AmbiguousLockError(
                "dead autopilot ownership has an unauthenticated application writer lease"
            )

    def acquire(self, run_id: str) -> dict[str, Any]:
        self.paths.ownership.parent.mkdir(parents=True, exist_ok=True)
        existing = read_lock(self.paths.ownership)
        recovered = None
        if existing is not None:
            host = existing.get("host")
            pid = existing.get("process_id")
            alive = (
                process_alive(pid)
                if host == socket.gethostname() and isinstance(pid, int)
                else None
            )
            if alive is True:
                raise LockError(
                    f"autopilot is already active for project {self.project.project_id}"
                )
            self._authenticate_dead_owner(existing)
            recovered = self.paths.ownership.with_name(
                f"ownership.recovered-{existing['run_id']}.json"
            )
            if recovered.exists():
                prior = json.loads(recovered.read_text(encoding="utf-8"))
                if prior != existing:
                    raise AmbiguousLockError(
                        "autopilot crash-recovery evidence already differs"
                    )
                self.paths.ownership.unlink()
            else:
                os.replace(self.paths.ownership, recovered)
        record = self._record(run_id, None)
        encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(
                self.paths.ownership,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise LockError(
                f"autopilot ownership appeared concurrently for {self.project.project_id}"
            ) from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self.run_id = run_id
        return {
            "record": record,
            "recovered_ownership": str(recovered) if recovered else None,
        }

    def heartbeat(
        self, event: str, *, projection: dict[str, Any] | None = None
    ) -> None:
        record = read_lock(self.paths.ownership)
        if (
            record is None
            or self.run_id is None
            or record.get("run_id") != self.run_id
            or record.get("process_id") != os.getpid()
            or record.get("process_start_identity") != process_start_evidence(os.getpid())
        ):
            raise LockError("autopilot ownership changed during execution")
        record["last_heartbeat"] = utc_now()
        record["last_completed_event"] = event
        inspector = RepositoryInspector(self.project.repository)
        record["repository_branch"] = inspector.current_branch
        record["repository_head"] = inspector.head
        projection = projection or {}
        for field in (
            "ledger_sequence",
            "ledger_fingerprint",
            "projection_fingerprint",
        ):
            if projection.get(field) is not None:
                record[field] = projection[field]
        atomic_write_json(self.paths.ownership, record)

    def release(self) -> None:
        record = read_lock(self.paths.ownership)
        if record is None:
            return
        if self.run_id is None or record.get("run_id") != self.run_id:
            raise LockError("refusing to release another autopilot owner's record")
        self.paths.ownership.unlink()


class Autopilot:
    """Evaluate one authoritative transition at a time until a real stop."""

    def __init__(
        self,
        *,
        configuration: Configuration,
        project: Project,
        engine: CycleEngine,
        consistency_factory: Callable[[], Any] | None = None,
        event_sink: Callable[[str], None] | None = None,
    ):
        self.configuration = configuration
        self.project = project
        self.engine = engine
        self.paths = AutopilotPaths.for_project(configuration, project.project_id)
        self.ownership = AutopilotOwnership(self.paths, project)
        self.consistency_factory = consistency_factory or (
            lambda: ConsistencyChecker(
                controller_root=configuration.root,
                project=project,
                planner_observer=lambda: engine.project_plan(project),
            )
        )
        self.event_sink = event_sink or print
        configured = configuration.conveyor.get("autopilot") or {}
        configured_budgets = configured.get("retry_budgets") or {}
        self.retry_budgets = {
            key: int(configured_budgets.get(key, value))
            for key, value in DEFAULT_RETRY_BUDGETS.items()
        }
        self.max_identical_failures = int(configured.get("max_identical_failures", 1))
        self._events: list[dict[str, Any]] = []
        self._feature_records: dict[str, dict[str, Any]] = {}
        self._failure_signatures: dict[str, int] = {}
        self._retry_counts: dict[tuple[str | None, str], int] = {}
        self._run_id: str | None = None
        self._stop_observed_at: str | None = None
        self.engine.lifecycle_observer = self._observe_lifecycle

    def _observe_lifecycle(
        self, boundary: str, evidence: dict[str, Any]
    ) -> bool:
        """Check a durable stop request at each inner lifecycle boundary.

        A route remains the smallest atomic safety unit. A request observed
        during validation, a Git operation, a commit, or a ledger append is
        honored immediately after that route returns to a coherent checkpoint.
        """

        lifecycle_event = {
            "feature_context_started": (
                "FEATURE_CONTEXT_STARTED",
                "building bounded binary-safe feature context",
            ),
            "feature_context_ready": (
                "FEATURE_CONTEXT_READY",
                "feature context fingerprint="
                + str((evidence.get("context_pack") or {}).get(
                    "context_pack_fingerprint"
                ) or "unavailable"),
            ),
            "feature_session_started": (
                "FEATURE_SESSION_STARTED",
                "authenticated model session="
                + str(evidence.get("session_id") or "unavailable"),
            ),
        }.get(boundary)
        if lifecycle_event is not None:
            self._emit(
                lifecycle_event[0],
                plan={
                    "selected_feature": evidence.get("feature_id"),
                    "current_state": (
                        "feature_running"
                        if boundary == "feature_session_started"
                        else "feature_preparing"
                    ),
                },
                diagnostic=lifecycle_event[1],
                transaction=(
                    evidence.get("transaction_id")
                    if isinstance(evidence.get("transaction_id"), str)
                    else None
                ),
            )
        if boundary not in {
            "before_transaction",
            "before_model",
            "feature_context_started",
            "feature_context_ready",
            "feature_session_started",
            "before_application_mutation",
            "before_commit",
            "before_integration",
            "between_validation_tiers",
            "before_next_feature",
        }:
            return False
        if self._stop_requested() is not None:
            self._stop_observed_at = boundary
            return True
        return False

    def _projection(self, plan: dict[str, Any]) -> dict[str, Any]:
        projection = plan.get("kernel_projection")
        return projection if isinstance(projection, dict) else {}

    def _action(self, plan: dict[str, Any]) -> str:
        raw = (
            plan.get("proposed_next_action")
            or plan.get("next_action")
            or self._projection(plan).get("allowed_next_action")
            or "verify_consistency"
        )
        return CANONICAL_ACTION_ALIASES.get(str(raw), str(raw))

    def _feature(self, plan: dict[str, Any]) -> str | None:
        value = (
            plan.get("selected_feature")
            or (plan.get("executable_plan") or {}).get("feature_id")
            or self._projection(plan).get("selected_next_feature")
            or self._projection(plan).get("current_feature")
        )
        return value if isinstance(value, str) and value else None

    def _state(self, plan: dict[str, Any]) -> str:
        return str(
            plan.get("current_state")
            or self._projection(plan).get("current_state")
            or "unknown"
        )

    def _emit(
        self,
        event: str,
        *,
        plan: dict[str, Any] | None = None,
        diagnostic: str,
        transaction: str | None = None,
    ) -> dict[str, Any]:
        if event not in AUTOPILOT_EVENTS:
            raise ConveyorError(f"unsupported autopilot event: {event}")
        plan = plan or {}
        projection = self._projection(plan)
        item = {
            "event": event,
            "project": self.project.project_id,
            "feature": self._feature(plan),
            "state": self._state(plan),
            "transaction": transaction or (
                (projection.get("active_transaction") or {}).get("transaction_id")
                if isinstance(projection.get("active_transaction"), dict)
                else None
            ),
            "timestamp": utc_now(),
            "diagnostic": redact_text(diagnostic),
        }
        self._events.append(item)
        self.event_sink(json.dumps(item, sort_keys=True))
        if self._run_id is not None and self.paths.ownership.exists():
            self.ownership.heartbeat(event, projection=projection)
            self._write_status(terminal=False)
        return item

    def _write_status(
        self,
        *,
        terminal: bool,
        classification: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        atomic_write_json(self.paths.status, {
            "schema_version": 1,
            "project": self.project.project_id,
            "run_id": self._run_id,
            "active": not terminal,
            "terminal_classification": classification,
            "diagnostic": diagnostic,
            "updated_at": utc_now(),
            "ownership_path": str(self.paths.ownership),
            "stop_request_path": str(self.paths.stop_request),
            "events": self._events[-50:],
            "features": list(self._feature_records.values()),
            "retry_budgets": self.retry_budgets,
        })

    def _stop_requested(self) -> dict[str, Any] | None:
        value = read_lock(self.paths.stop_request)
        if value is None:
            return None
        if (
            value.get("project_id") != self.project.project_id
            or not isinstance(value.get("requested_at"), str)
        ):
            raise AmbiguousLockError("autopilot stop request has invalid project identity")
        return value

    def _acknowledge_stop(self, request: dict[str, Any]) -> None:
        acknowledged = self.paths.stop_request.with_name("last-stop-request.json")
        atomic_write_json(acknowledged, {
            **request,
            "acknowledged_at": utc_now(),
            "acknowledged_by_run": self._run_id,
        })
        self.paths.stop_request.unlink()

    def _consistency(self) -> dict[str, Any]:
        checker = self.consistency_factory()
        value = checker.check() if hasattr(checker, "check") else checker()
        if not isinstance(value, dict):
            raise ConveyorError("consistency checker returned invalid evidence")
        return value

    def dry_run(self) -> dict[str, Any]:
        plan = self.engine.project_plan(self.project)
        consistency = self._consistency()
        action = self._action(plan)
        feature = self._feature(plan)
        return {
            "schema_version": 1,
            "mode": "dry-run",
            "project": self.project.project_id,
            "current_state": self._state(plan),
            "first_feature": feature,
            "exact_next_route": action,
            "consistency": consistency.get("classification"),
            "loop_policy": [
                "queue_reconciliation",
                "feature_ready",
                "feature_preparation",
                "feature_execution",
                "feature_acceptance",
                "integration_pending",
                "milestone_integration",
                "queue_reconciliation",
                "next_feature",
            ],
            "retry_budgets": self.retry_budgets,
            "stop_behavior": {
                "durable_request": str(self.paths.stop_request),
                "safe_checkpoints": [
                    "before_transaction",
                    "before_model",
                    "before_application_mutation",
                    "before_commit",
                    "before_integration",
                    "between_validation_tiers",
                    "before_next_feature",
                ],
                "atomic_operations_are_not_interrupted": True,
                "continuation_command": (
                    f"scripts/conveyor autopilot --project {self.project.project_id} --apply"
                ),
            },
            "deterministic_recovery_routes": list(DETERMINISTIC_RECOVERY_ROUTES),
            "model_backed_repair_routes": list(MODEL_BACKED_REPAIR_ROUTES),
            "first_cycle_estimate": plan.get("cost_aware_run_plan"),
            "dry_run_guarantees": {
                "writes": 0,
                "models": 0,
                "children": 0,
                "feature_executions": 0,
                "integrations": 0,
                "leases": 0,
                "transactions": 0,
                "tests": 0,
                "application_mutations": 0,
            },
            "structured_report_path_on_apply": str(self.paths.status),
        }

    @staticmethod
    def _recovery_fields(
        plan: dict[str, Any], key: str, required: tuple[str, ...]
    ) -> dict[str, Any]:
        value = plan.get(key)
        if not isinstance(value, dict):
            raise ConveyorError(f"{key} route lacks exact recovery evidence")
        missing = [
            field for field in required
            if not isinstance(value.get(field), str) or not value[field]
        ]
        if missing:
            raise ConveyorError(
                f"{key} route lacks exact fields: {', '.join(missing)}"
            )
        return value

    def _route(self, action: str, plan: dict[str, Any]) -> dict[str, Any]:
        if action == "feature_prelaunch_recovery":
            evidence = self._recovery_fields(
                plan,
                "feature_prelaunch_recovery",
                (
                    "feature_id",
                    "autopilot_run_id",
                    "expected_branch",
                    "expected_head",
                ),
            )
            recovery = FeaturePrelaunchRecovery(
                controller_root=self.configuration.root,
                configuration=self.configuration,
                project=self.project,
            )
            inspected = recovery.inspect(
                feature_id=evidence["feature_id"],
                autopilot_run_id=evidence["autopilot_run_id"],
                expected_branch=evidence["expected_branch"],
                expected_head=evidence["expected_head"],
            )
            return recovery.apply(inspected)
        if action == "feature_result_recovery":
            evidence = self._recovery_fields(
                plan,
                "feature_result_recovery",
                (
                    "feature_id",
                    "original_transaction_id",
                    "original_run_id",
                    "original_session_id",
                    "expected_branch",
                    "expected_head",
                ),
            )
            recovery = FeatureResultRecovery(
                controller_root=self.configuration.root,
                configuration=self.configuration.conveyor,
                project=self.project,
            )
            inspected = recovery.inspect(**{
                field: evidence[field]
                for field in (
                    "feature_id",
                    "original_transaction_id",
                    "original_run_id",
                    "original_session_id",
                    "expected_branch",
                    "expected_head",
                )
            })
            return recovery.apply(inspected)
        if action == "retained_feature_repair":
            evidence = self._recovery_fields(
                plan,
                "retained_feature_repair",
                (
                    "feature_id",
                    "original_transaction_id",
                    "failed_recovery_transaction_id",
                    "expected_branch",
                    "expected_head",
                ),
            )
            repair = RetainedFeatureValidationRepair(
                controller_root=self.configuration.root,
                configuration=self.configuration.conveyor,
                project=self.project,
            )
            inspected = repair.inspect(
                **{
                    field: evidence[field]
                    for field in (
                        "feature_id",
                        "original_transaction_id",
                        "failed_recovery_transaction_id",
                        "expected_branch",
                        "expected_head",
                    )
                }
            )
            return repair.apply(inspected)
        if action == "retained_feature_repair_recovery":
            evidence = self._recovery_fields(
                plan,
                "retained_feature_repair_recovery",
                ("feature_id", "repair_transaction_id"),
            )
            recovery = RetainedFeatureRepairRecovery(
                controller_root=self.configuration.root,
                configuration=self.configuration.conveyor,
                project=self.project,
            )
            inspected = recovery.inspect(
                feature_id=evidence["feature_id"],
                repair_transaction_id=evidence["repair_transaction_id"],
            )
            return recovery.apply(inspected)
        if action == "accepted_commit_recovery":
            evidence = self._recovery_fields(
                plan,
                "accepted_commit_recovery",
                (
                    "feature_id",
                    "candidate_commit",
                    "milestone_base",
                    "feature_branch",
                    "feature_transaction_id",
                    "acceptance_transaction_id",
                ),
            )
            recovery = AcceptedCommitRecovery(
                controller_root=self.configuration.root,
                configuration=self.configuration.conveyor,
                project=self.project,
            )
            inspected = recovery.inspect(**{
                field: evidence[field]
                for field in (
                    "feature_id",
                    "candidate_commit",
                    "milestone_base",
                    "feature_branch",
                    "feature_transaction_id",
                    "acceptance_transaction_id",
                )
            })
            return recovery.apply(inspected)
        if action == "cycle_cache_repair":
            recovery = CycleCacheRepair(
                controller_root=self.configuration.root,
                configuration=self.configuration,
                project=self.project,
            )
            return recovery.apply(recovery.inspect())
        if action in {
            "cache_binding_recovery",
            "planning_finalization",
            "integration_finalization_recovery",
            "kernel_recovery",
            "verify_consistency",
        }:
            return self.engine.run_project(self.project, "resume", dry_run=False)
        if action == "feature_cycle":
            return self.engine.run_project(self.project, "one_feature", dry_run=False)
        if action in {"queue_reconciliation", "milestone_integration", "milestone_gate"}:
            return self.engine.run_project(self.project, "milestone", dry_run=False)
        raise ConveyorError(f"autopilot has no registered route for {action}")

    @staticmethod
    def _terminal_result(result: dict[str, Any]) -> str | None:
        outcome = str(result.get("outcome") or result.get("classification") or "")
        if outcome in {
            "human_decision_required",
            "human_merge_approval",
            "HUMAN_DECISION_REQUIRED",
            "SEMANTIC_CONFLICT",
        } or result.get("human_gate"):
            return "human_gate"
        if outcome in {
            "milestone_complete",
            "portfolio_complete",
            "already_completed",
            "AUTOPILOT_COMPLETED",
        }:
            return "completed"
        return None

    def _record_result(
        self,
        plan: dict[str, Any],
        result: dict[str, Any],
        *,
        elapsed: float,
    ) -> None:
        feature = self._feature(plan)
        if feature is None:
            return
        cost = plan.get("cost_aware_run_plan") or {}
        usage = cost.get("usage_accounting") or {}
        record = self._feature_records.setdefault(feature, {
            "feature": feature,
            "model": cost.get("selected_model"),
            "reasoning": cost.get("selected_reasoning_effort"),
            "parent_sessions": 0,
            "child_sessions": 0,
            "elapsed_seconds": 0.0,
            "context_estimate": usage.get("context_bytes_estimate")
            or (cost.get("context_pack") or {}).get("approximate_bytes_estimate"),
            "validation_commands": cost.get("deterministic_commands_planned", []),
            "retry_count": 0,
            "terminal_classification": None,
            "integration_result": None,
        })
        record["elapsed_seconds"] = round(float(record["elapsed_seconds"]) + elapsed, 3)
        record["parent_sessions"] += int(
            result.get("parent_sessions_launched")
            or result.get("model_session_launched") is True
        )
        record["child_sessions"] += int(result.get("child_sessions_launched") or 0)
        record["retry_count"] += len(result.get("repair_attempts") or [])
        outcome = result.get("outcome") or result.get("classification")
        if outcome:
            record["terminal_classification"] = outcome
        if outcome in {"feature_integrated", "one_feature_integrated", "INTEGRATED"}:
            record["integration_result"] = outcome

    def _failure_signature(
        self, action: str, plan: dict[str, Any], result: dict[str, Any]
    ) -> str:
        evidence = {
            "action": action,
            "feature": self._feature(plan),
            "state": self._state(plan),
            "outcome": result.get("outcome"),
            "classification": result.get("classification"),
            "reason": result.get("reason") or result.get("diagnostic"),
        }
        return hashlib.sha256(
            json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _retry_budget_key(self, action: str, outcome: str) -> str:
        if "validation" in outcome.lower():
            return "validation_retries"
        if action == "feature_cycle":
            return "implementation_retries_per_feature"
        if action == "queue_reconciliation":
            return "planning_retries"
        if action in {
            "milestone_integration",
            "integration_finalization_recovery",
        }:
            return "integration_finalization_retries"
        return "deterministic_recovery_attempts"

    def apply(self) -> dict[str, Any]:
        self._run_id = str(uuid.uuid4())
        ownership = self.ownership.acquire(self._run_id)
        terminal = "AUTOPILOT_FAILED"
        diagnostic = "autopilot ended unexpectedly"
        try:
            initial = self.engine.project_plan(self.project)
            self._emit(
                "AUTOPILOT_STARTED",
                plan=initial,
                diagnostic=(
                    "continuous controller loop acquired exclusive project ownership"
                ),
            )
            while True:
                stop = self._stop_requested()
                if stop is not None:
                    self._emit(
                        "STOP_REQUESTED",
                        diagnostic=(
                            str(stop.get("reason") or "durable stop requested")
                            + (
                                f"; first observed at {self._stop_observed_at}"
                                if self._stop_observed_at else ""
                            )
                        ),
                    )
                    self._acknowledge_stop(stop)
                    terminal = "AUTOPILOT_STOPPED"
                    diagnostic = "stopped at a safe transition checkpoint"
                    break

                plan = self.engine.project_plan(self.project)
                consistency = self._consistency()
                classification = consistency.get("classification")
                action = self._action(plan)
                recognized_recovery = bool(
                    (
                        action == "feature_result_recovery"
                        and plan.get("recognized_technical_recovery") is True
                    )
                    or (
                        action == "feature_prelaunch_recovery"
                        and plan.get("recognized_technical_recovery") is True
                    )
                    or (
                        action == "retained_feature_repair"
                        and plan.get("recognized_technical_repair") is True
                    )
                    or (
                        action == "retained_feature_repair_recovery"
                        and plan.get("recognized_technical_recovery") is True
                    )
                )
                if (
                    classification
                    not in {"CONSISTENT", "RECOVERABLE_INCONSISTENCY"}
                    and not recognized_recovery
                ):
                    terminal = "AUTOPILOT_FAILED"
                    diagnostic = (
                        "consistency requires a genuine gate: "
                        f"{classification or 'unknown'}"
                    )
                    break
                feature = self._feature(plan)
                if (
                    action
                    in {"human_decision_resolution", "human_merge_approval"}
                    or (plan.get("human_gate") and not recognized_recovery)
                ):
                    terminal = "AUTOPILOT_STOPPED"
                    diagnostic = "authoritative projection contains a genuine human gate"
                    break
                if action in {"disabled", "paused", "portfolio_complete", "completed"}:
                    terminal = "AUTOPILOT_COMPLETED"
                    diagnostic = f"authoritative projection reached {action}"
                    break

                if feature:
                    self._emit(
                        "FEATURE_SELECTED",
                        plan=plan,
                        diagnostic=f"{feature} selected through {action}",
                    )
                recovery = action in {
                    "cache_binding_recovery",
                    "cycle_cache_repair",
                    "planning_finalization",
                    "integration_finalization_recovery",
                    "kernel_recovery",
                    "verify_consistency",
                    "feature_result_recovery",
                    "retained_feature_repair_recovery",
                    "retained_feature_repair",
                    "accepted_commit_recovery",
                    "feature_prelaunch_recovery",
                }
                if recovery:
                    self._emit(
                        "RECOVERY_STARTED",
                        plan=plan,
                        diagnostic=(
                            f"invoking registered {'model-backed repair' if action in MODEL_BACKED_REPAIR_ROUTES else 'deterministic recovery'} route {action}"
                        ),
                    )
                    if action == "retained_feature_repair":
                        self._emit(
                            "FEATURE_REPAIR_STARTED",
                            plan=plan,
                            diagnostic=(
                                "launching bounded Terra/high retained-feature "
                                "repair with zero children"
                            ),
                        )
                        self._emit(
                            "FEATURE_REPAIR_VALIDATION_STARTED",
                            plan=plan,
                            diagnostic=(
                                "repair will run the authenticated trusted-host "
                                "validation plan before any commit"
                            ),
                        )
                # A route is the smallest safe controller-owned atomic unit.
                # Stop requests arriving during it are observed immediately
                # after the route returns, never by killing a Git/ledger write.
                started = time.monotonic()
                try:
                    result = self._route(action, plan)
                except RecoveryError as exc:
                    if not recovery:
                        raise
                    result = {
                        "outcome": "deterministic_recovery_failed",
                        "reason": redact_text(str(exc)),
                        "recoverable_technical_failure": True,
                        "human_gate_created": False,
                        "model_sessions_launched": 0,
                        "child_sessions_launched": 0,
                    }
                elapsed = time.monotonic() - started
                self._record_result(plan, result, elapsed=elapsed)

                recovery_failed = result.get(
                    "recoverable_technical_failure"
                ) is True or str(
                    result.get("outcome") or result.get("classification") or ""
                ) in {
                    "deterministic_recovery_failed",
                    "validation_failed",
                    "VALIDATION_FAILED",
                    "repair_exhausted",
                }
                if recovery and not recovery_failed:
                    self._emit(
                        "RECOVERY_APPLIED",
                        plan=plan,
                        diagnostic=str(
                            result.get("outcome")
                            or result.get("classification")
                            or "deterministic recovery route completed"
                        ),
                    )
                    if (
                        action
                        in {
                            "retained_feature_repair",
                            "retained_feature_repair_recovery",
                        }
                        and result.get("accepted_feature_commit")
                    ):
                        self._emit(
                            "FEATURE_ACCEPTED",
                            plan=plan,
                            diagnostic="retained feature repaired and accepted",
                        )
                outcome = str(result.get("outcome") or result.get("classification") or "")
                if outcome in {
                    "feature_accepted",
                    "one_feature_accepted",
                    "FEATURE_ACCEPTED",
                }:
                    self._emit(
                        "FEATURE_ACCEPTED",
                        plan=plan,
                        diagnostic=outcome,
                    )
                if outcome in {
                    "feature_integrated",
                    "one_feature_integrated",
                    "INTEGRATED",
                }:
                    self._emit(
                        "FEATURE_INTEGRATED",
                        plan=plan,
                        diagnostic=outcome,
                    )

                result_terminal = self._terminal_result(result)
                if result_terminal == "human_gate":
                    next_plan = self.engine.project_plan(self.project)
                    if (
                        (
                            self._action(next_plan)
                            in {
                                "feature_result_recovery",
                                "retained_feature_repair_recovery",
                            }
                            and next_plan.get("recognized_technical_recovery")
                            is True
                        )
                        or (
                            self._action(next_plan)
                            == "retained_feature_repair"
                            and next_plan.get("recognized_technical_repair")
                            is True
                        )
                    ):
                        continue
                    terminal = "AUTOPILOT_STOPPED"
                    diagnostic = "normal route produced a genuine human gate"
                    break
                if result_terminal == "completed":
                    terminal = "AUTOPILOT_COMPLETED"
                    diagnostic = "portfolio or milestone work is complete"
                    break

                failed = outcome in {
                    "validation_failed",
                    "planning_validation_failed",
                    "invalid_queue",
                    "retry_exhausted",
                    "TERMINAL_FEATURE_FAILURE",
                    "TERMINAL_PLANNING_FAILURE",
                    "VALIDATION_FAILED",
                    "deterministic_recovery_failed",
                    "repair_exhausted",
                }
                if failed:
                    signature = self._failure_signature(action, plan, result)
                    count = self._failure_signatures.get(signature, 0) + 1
                    self._failure_signatures[signature] = count
                    budget_key = self._retry_budget_key(action, outcome)
                    counter_key = (feature, budget_key)
                    attempts = self._retry_counts.get(counter_key, 0) + 1
                    self._retry_counts[counter_key] = attempts
                    exhausted = attempts > self.retry_budgets[budget_key]
                    if result.get("outcome") == "repair_exhausted":
                        exhausted = True
                    repeated = count > self.max_identical_failures
                    if exhausted or repeated:
                        self._emit(
                            "FEATURE_BLOCKED",
                            plan=plan,
                            diagnostic=(
                                (
                                    "identical evidence repeated"
                                    if repeated else f"{budget_key} exhausted"
                                )
                                + "; feature quarantined in "
                                "autopilot evidence without being marked complete"
                            ),
                        )
                        if feature is not None:
                            blocked = self._feature_records.setdefault(
                                feature, {"feature": feature}
                            )
                            blocked["quarantined"] = True
                            blocked["terminal_classification"] = (
                                "bounded_recovery_exhausted"
                            )
                        next_plan = self.engine.project_plan(self.project)
                        next_feature = self._feature(next_plan)
                        if (
                            next_feature is not None
                            and next_feature != feature
                            and self._action(next_plan) == "feature_cycle"
                        ):
                            continue
                        terminal = "AUTOPILOT_STOPPED"
                        diagnostic = (
                            "recoverable technical failure: bounded recovery "
                            "exhausted and no newly projected "
                            "dependency-independent feature is safe"
                        )
                        break
                # Safe boundary before beginning the next feature/transition.
                self._observe_lifecycle("before_next_feature", {})
                stop = self._stop_requested()
                if stop is not None:
                    self._emit(
                        "STOP_REQUESTED",
                        plan=plan,
                        diagnostic=(
                            str(stop.get("reason") or "durable stop requested")
                            + (
                                f"; first observed at {self._stop_observed_at}"
                                if self._stop_observed_at else ""
                            )
                        ),
                    )
                    self._acknowledge_stop(stop)
                    terminal = "AUTOPILOT_STOPPED"
                    diagnostic = "stopped before beginning the next transition"
                    break
        except Exception as exc:
            stop = self._stop_requested()
            if isinstance(exc, AutopilotStopRequested) and stop is not None:
                self._acknowledge_stop(stop)
                terminal = "AUTOPILOT_STOPPED"
                diagnostic = (
                    "stopped at safe inner lifecycle boundary "
                    f"{self._stop_observed_at or 'unknown'}"
                )
            else:
                terminal = "AUTOPILOT_FAILED"
                diagnostic = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                event_plan = {}
                try:
                    event_plan = self.engine.project_plan(self.project)
                except Exception:
                    pass
                self._emit(terminal, plan=event_plan, diagnostic=diagnostic)
                self._write_status(
                    terminal=True,
                    classification=terminal,
                    diagnostic=diagnostic,
                )
            finally:
                self.ownership.release()
        return {
            "schema_version": 1,
            "project": self.project.project_id,
            "run_id": self._run_id,
            "classification": terminal,
            "diagnostic": diagnostic,
            "status_path": str(self.paths.status),
            "ownership_recovered": ownership["recovered_ownership"],
            "ownership_released": not self.paths.ownership.exists(),
            "writer_lease_remaining": RepositoryInspector(
                self.project.repository
            ).writer_lock_path(".factory/locks/writer.json").exists(),
            "events": self._events,
            "features": list(self._feature_records.values()),
        }


def request_stop(configuration: Configuration, project: Project, reason: str | None = None) -> dict[str, Any]:
    paths = AutopilotPaths.for_project(configuration, project.project_id)
    request = {
        "schema_version": 1,
        "project_id": project.project_id,
        "requested_at": utc_now(),
        "requested_by_pid": os.getpid(),
        "requested_by_host": socket.gethostname(),
        "reason": reason or "user requested a durable autopilot stop",
    }
    atomic_write_json(paths.stop_request, request)
    return {
        "classification": "STOP_REQUESTED",
        "project": project.project_id,
        "stop_request_path": str(paths.stop_request),
        "request": request,
    }


def autopilot_status(configuration: Configuration, project: Project) -> dict[str, Any]:
    paths = AutopilotPaths.for_project(configuration, project.project_id)
    ownership = read_lock(paths.ownership)
    stop = read_lock(paths.stop_request)
    report = read_lock(paths.status)
    alive = None
    if ownership is not None:
        pid = ownership.get("process_id")
        host = ownership.get("host")
        if host == socket.gethostname() and isinstance(pid, int):
            alive = process_alive(pid)
    return {
        "schema_version": 1,
        "project": project.project_id,
        "active": ownership is not None and alive is True,
        "ownership": ownership,
        "owner_process_alive": alive,
        "stop_requested": stop is not None,
        "stop_request": stop,
        "status_path": str(paths.status),
        "report": report,
    }
