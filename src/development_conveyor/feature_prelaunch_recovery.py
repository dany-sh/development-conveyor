"""Deterministic recovery for a feature context failure before model launch."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .command_authority import CommandAuthority
from .contracts import MutationPolicy, WorkflowType, fingerprint
from .errors import RecoveryError
from .kernel import WorkflowKernel
from .ledger import EvidenceLedger
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease


def authenticates_run_scoped_capability_isolation_report(
    report: dict[str, Any],
    *,
    project_id: str,
    run_id: str,
    repository: Path,
) -> bool:
    return bool(
        report.get("run_id") == run_id
        and report.get("project_id") == project_id
        and report.get("action") == "feature_cycle"
        and report.get("failure_classification") == "session_execution_failed"
        and report.get("exit_classification") == "session_execution_failed"
        and report.get("result_classification") == "session_execution_failed"
        and report.get("exit_status") is None
        and report.get("argv") == []
        and report.get("session_id") is None
        and report.get("launched_model") is None
        and report.get("terminal_marker_found") is False
        and report.get("context_pack_evidence") is None
        and report.get("context_read_failure") is None
        and report.get("structured_result") is None
        and report.get("parsed_structured_result") is None
        and str(report.get("redacted_stderr") or "").startswith(
            "capability_isolation_unsupported:"
        )
        and Path(str(report.get("working_directory") or "")).resolve()
        == repository.resolve()
    )


class FeaturePrelaunchRecovery:
    """Authenticate and recover only an exact clean zero-session prelaunch."""

    def __init__(self, *, controller_root: Path, configuration: Any, project: Project):
        self.root = controller_root.resolve()
        self.configuration = configuration
        self.project = project
        self.inspector = RepositoryInspector(project.repository)
        identity = self.inspector.identity()
        self.state_root = self.root / "state/projects" / project.project_id
        self.ledger = EvidenceLedger(
            self.state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(
            self.ledger, self.state_root / "projection-cache.json"
        )

    @property
    def _autopilot_report(self) -> Path:
        configured = self.configuration.conveyor
        reports = Path(str(configured["report_directory"]))
        if not reports.is_absolute():
            reports = self.root / reports
        return reports.resolve() / "autopilot" / self.project.project_id / "latest.json"

    @property
    def _report_root(self) -> Path:
        reports = Path(str(self.configuration.conveyor["report_directory"]))
        if not reports.is_absolute():
            reports = self.root / reports
        return reports.resolve()

    def _failure_report(
        self, run_id: str
    ) -> tuple[dict[str, Any], str]:
        run_report = (
            self._report_root
            / run_id
            / "feature_cycle-launch-failure.json"
        ).resolve()
        try:
            run_report.relative_to(self._report_root)
        except ValueError:
            run_report = self._report_root / ".invalid-run-id"
        candidates = (
            (self._autopilot_report, "autopilot"),
            (run_report, "run_scoped_launch_failure"),
        )
        for path, kind in candidates:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(report, dict) and report.get("run_id") == run_id:
                return report, kind
        raise RecoveryError("prelaunch recovery lacks its exact failure report")

    def inspect(
        self,
        *,
        feature_id: str,
        autopilot_run_id: str,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        report, report_kind = self._failure_report(autopilot_run_id)
        projection = self.projection.rebuild(persist_cache=False)
        latest = max(
            (
                item for item in projection.get("transactions") or []
                if item.get("workflow_type") == WorkflowType.FEATURE_EXECUTION.value
            ),
            key=lambda item: int(item.get("last_sequence") or 0),
            default={},
        )
        transaction_id = latest.get("transaction_id")
        ledger_events = self.ledger.read()
        events = [
            event for event in ledger_events
            if event.get("transaction_id") == transaction_id
        ]
        payloads = [event.get("payload") or {} for event in events]
        start = payloads[0] if payloads else {}
        snapshot = payloads[2].get("snapshot") if len(payloads) > 2 else {}
        blocked = payloads[3] if len(payloads) > 3 else {}
        terminal = blocked.get("terminal_snapshot") or {}
        queue = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        feature = queue.feature(feature_id) or {}
        milestone_head = self.inspector.rev_parse(
            self.project.milestone_branch or "", check=False
        )
        event_names = [event.get("event_type") for event in events]
        legacy_failure = (
            blocked.get("classification") == "FEATURE_VALIDATION_FAILED"
            and blocked.get("reference") == "UnicodeDecodeError"
        )
        typed_failure = (
            blocked.get("classification") == "FEATURE_PRELAUNCH_CONTEXT_FAILED"
            and blocked.get("reference") == "ContextReadError"
        )
        isolation_failure = (
            blocked.get("classification") == "FEATURE_VALIDATION_FAILED"
            and blocked.get("reference") == "SessionError"
            and report_kind == "run_scoped_launch_failure"
            and start.get("run_id") == report.get("run_id")
            and authenticates_run_scoped_capability_isolation_report(
                report,
                project_id=self.project.project_id,
                run_id=str(start.get("run_id")),
                repository=self.project.repository,
            )
        )
        report_events = report.get("events") if isinstance(report, dict) else []
        authenticated_session_events = [
            event for event in report_events or []
            if event.get("event") == "FEATURE_SESSION_STARTED"
            and "authenticated model session=" in str(event.get("diagnostic") or "")
        ]
        checks = {
            "project": (
                report.get("project") == self.project.project_id
                if report_kind == "autopilot"
                else report.get("project_id") == self.project.project_id
            ),
            "feature": start.get("feature_id") == feature_id == latest.get("feature_id"),
            "autopilot_run": report.get("run_id") == autopilot_run_id,
            "failure_report": (
                report.get("terminal_classification") == "AUTOPILOT_FAILED"
                if report_kind == "autopilot"
                else report.get("failure_classification")
                == "session_execution_failed"
            ),
            "exact_transaction_shape": event_names == [
                "TransactionStarted", "LeaseAcquired", "SnapshotCaptured",
                "TransactionBlocked", "LeaseReleased", "ProjectionUpdated",
            ],
            "prelaunch_failure": legacy_failure or typed_failure or isolation_failure,
            "zero_session_events": not any(
                event.get("event_type") == "SessionLaunched" for event in events
            ),
            "zero_authenticated_autopilot_sessions": not authenticated_session_events,
            "no_feature_report": not (
                self._report_root
                .joinpath(str(start.get("run_id")), "feature_cycle.json")
                .exists()
            ),
            "no_commit": not any(
                event.get("event_type") == "CommitFinalized" for event in events
            ),
            "no_active_transaction": projection.get("active_transaction") is None,
            "not_already_recovered": not any(
                event.get("event_type") == "RecoveryApplied"
                and (event.get("payload") or {}).get("classification")
                == "FEATURE_PRELAUNCH_RECOVERY"
                and (event.get("payload") or {}).get(
                    "failed_transaction_id"
                ) == transaction_id
                for event in ledger_events
            ),
            "clean_repository": self.inspector.is_clean,
            "branch": self.inspector.current_branch == expected_branch,
            "head": self.inspector.head == expected_head,
            "snapshot_identity": (
                snapshot.get("branch") == terminal.get("branch") == expected_branch
                and snapshot.get("head") == terminal.get("head") == expected_head
            ),
            "same_milestone_head": milestone_head == expected_head,
            "feature_ref": self.inspector.rev_parse(expected_branch, check=False)
            == expected_head,
            "queue_ready": feature.get("status") == "ready",
            "git_operation_absent": not any(
                self.inspector.git_operation_state().values()
            ),
            "writer_lease_absent": not self.inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            ).exists(),
            "ownership_absent": not (
                self.root / "state/autopilot" / self.project.project_id
                / "ownership.json"
            ).exists(),
            "reservation_absent": not (
                self.root
                / self.configuration.conveyor["lock_policy"][
                    "controller_launch_lock_directory"
                ]
                / f"{self.inspector.identity()['path_fingerprint']}.json"
            ).exists(),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(
                "feature prelaunch recovery authentication failed: " + failed
            )
        plan = {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "feature_id": feature_id,
            "autopilot_run_id": autopilot_run_id,
            "failure_report_kind": report_kind,
            "failed_feature_run_id": start["run_id"],
            "failed_transaction_id": transaction_id,
            "failure_classification": blocked["classification"],
            "expected_branch": expected_branch,
            "expected_head": expected_head,
            "milestone_branch": self.project.milestone_branch,
            "milestone_head": milestone_head,
            "queue_status": feature.get("status"),
            "checks": checks,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "implementation_attempts_consumed": 0,
            "application_commands_that_would_run": 0,
            "application_changes_that_would_be_created": [],
            "planning_commits_that_would_be_created": 0,
            "feature_commits_that_would_be_created": 0,
            "queue_reconciliation_performed": False,
            "next_state_on_apply": "feature_preparing",
            "selected_feature_on_apply": feature_id,
        }
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        current = self.inspect(
            feature_id=plan["feature_id"],
            autopilot_run_id=plan["autopilot_run_id"],
            expected_branch=plan["expected_branch"],
            expected_head=plan["expected_head"],
        )
        if current["plan_fingerprint"] != plan["plan_fingerprint"]:
            raise RecoveryError("feature prelaunch recovery plan changed before apply")
        kernel = WorkflowKernel(
            project=self.project,
            ledger=self.ledger,
            projection=self.projection,
            lease=WorkflowWriterLease(self.inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            )),
        )
        transaction = kernel.begin(
            workflow_type=WorkflowType.RECOVERY,
            milestone=self.project.active_milestone,
            feature_id=plan["feature_id"],
            run_id="prelaunch-recovery-" + uuid.uuid4().hex,
            policy=MutationPolicy(()),
            expected_starting_branch=plan["expected_branch"],
            expected_starting_head=plan["expected_head"],
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        kernel.checkpoint("feature_prelaunch_recovery_authenticated", {
            "failed_transaction_id": plan["failed_transaction_id"],
            "failed_autopilot_run_id": plan["autopilot_run_id"],
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
            "implementation_attempts_consumed": 0,
        })
        kernel.finalize_clean_prelaunch_recovery(
            failed_transaction_id=plan["failed_transaction_id"],
            failed_autopilot_run_id=plan["autopilot_run_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            feature_id=plan["feature_id"],
        )
        completion = kernel.complete(
            classification="RECOVERY_APPLIED",
            evidence={
                "selected_feature": plan["feature_id"],
                "failed_transaction_id": plan["failed_transaction_id"],
                "failed_autopilot_run_id": plan["autopilot_run_id"],
                "model_session_launched": False,
                "child_sessions_launched": 0,
                "implementation_attempts_consumed": 0,
                "planning_commit_created": False,
                "feature_commit_created": False,
            },
        )
        projection = completion["projection"]
        # Refresh both compatibility surfaces only after canonical recovery
        # evidence and lease release are durable.
        from .cycle_engine import CycleEngine

        engine = CycleEngine(self.configuration)
        executable = engine._execution_plan(self.project, projection)
        engine._reconcile_projection_compatibility_cache(
            self.project, projection, executable
        )
        cycle_path = self.inspector.cycle_state_path()
        try:
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "prelaunch recovery cannot refresh the application cycle cache"
            ) from exc
        cycle.update({
            "current_phase": "feature_preparing",
            "feature_branch": plan["expected_branch"],
            "feature_starting_commit": plan["expected_head"],
            "milestone_branch": plan["milestone_branch"],
            "milestone_pre_integration_commit": plan["expected_head"],
            "feature_worktree": str(self.project.repository.resolve()),
            "last_verified_git_state": engine._git_checkpoint(self.inspector),
            "last_successful_checkpoint": "feature_prelaunch_recovery_applied",
            "failure_classification": None,
            "feature_session_id": None,
            "session_id": None,
            "human_decision_required": None,
            "retry_exhausted": False,
            "next_safe_action": (
                f"scripts/conveyor run --project {self.project.project_id} "
                "--mode one_feature"
            ),
            "stop_reason": None,
            "validation_attempts": [],
        })
        engine._materialize_terminal_cycle_cache(
            cycle_path,
            cycle,
            transaction.transaction_id,
            completion,
            self.ledger,
        )
        return {
            **plan,
            "outcome": "prelaunch_recovery_applied",
            "recovery_transaction_id": transaction.transaction_id,
            "kernel_projection": projection,
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
            "application_commands_run": 0,
            "feature_branch_preserved": self.inspector.current_branch,
            "feature_head_preserved": self.inspector.head,
        }
