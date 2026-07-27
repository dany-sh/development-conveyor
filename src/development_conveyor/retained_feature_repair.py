"""Bounded model-backed repair for an authenticated retained feature result."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from .command_authority import CommandAuthority
from .contracts import (
    SessionResultEnvelope,
    TransactionState,
    WorkflowType,
    extract_terminal_envelope,
    fingerprint,
)
from .errors import RecoveryError, SessionError, TransactionError
from .feature_result_recovery import FeatureResultRecovery, _changed_paths
from .kernel import FeatureExecutionAdapter, WorkflowKernel
from .locks import DurableLock, inspect_repository_writer_lock, make_lock_record
from .logging import atomic_write_json, utc_now
from .redaction import redact_text
from .sessions import SessionLauncher, SessionRequest, SessionResult
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowWriterLease


LauncherFactory = Callable[[], SessionLauncher]


class RetainedFeatureValidationRepair(FeatureResultRecovery):
    """Repair validation for one exact failed retained-result recovery."""

    MAX_ATTEMPTS = 2
    MODEL = "gpt-5.6-terra"
    REASONING = "high"
    CHILD_SESSIONS = 0

    @staticmethod
    def _semantic_failure_signature(
        *,
        route: str,
        feature_id: str,
        invoked_workflow: str,
        emitted_workflow: str | None,
        diagnostic: str,
        envelope_classification: str | None,
        authorized_paths: list[str],
        environment_failure: bool,
    ) -> str:
        diagnostic_category = (
            "workflow_alias_conflict"
            if "workflow_type conflicts with invoked workflow"
            in diagnostic
            else hashlib.sha256(
                redact_text(diagnostic).encode("utf-8")
            ).hexdigest()
        )
        return fingerprint(
            {
                "route": route,
                "feature": feature_id,
                "invoked_workflow": invoked_workflow,
                "emitted_workflow": emitted_workflow,
                "diagnostic_category": diagnostic_category,
                "envelope_classification": envelope_classification,
                "authorized_paths": sorted(authorized_paths),
                "environment_failure": environment_failure,
            }
        )

    @staticmethod
    def _raw_terminal_envelope(stdout: str) -> dict[str, Any] | None:
        messages: list[str] = []
        for line in stdout.splitlines():
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
            return None
        markers = [
            line.split("=", 1)[1]
            for line in messages[-1].splitlines()
            if line.startswith("CONVEYOR_TRANSACTION_RESULT=")
        ]
        if len(markers) != 1:
            return None
        try:
            value = json.loads(markers[0])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def __init__(
        self,
        *,
        controller_root: Path,
        configuration: dict[str, Any],
        project,
        command_runner=None,
        launcher_factory: LauncherFactory | None = None,
    ):
        super().__init__(
            controller_root=controller_root,
            configuration=configuration,
            project=project,
            command_runner=command_runner,
        )
        self.launcher_factory = launcher_factory or (
            lambda: SessionLauncher(self.controller_root, self.configuration)
        )

    def _original_identity(self, transaction_id: str) -> dict[str, str]:
        events = [
            item
            for item in self.ledger.read()
            if item.get("transaction_id") == transaction_id
        ]
        start = next(
            (item for item in events if item.get("event_type") == "TransactionStarted"),
            None,
        )
        launch = next(
            (item for item in events if item.get("event_type") == "SessionLaunched"),
            None,
        )
        payload = (start or {}).get("payload") or {}
        session = (launch or {}).get("payload") or {}
        identity = {
            "original_run_id": payload.get("run_id"),
            "original_session_id": session.get("session_id"),
        }
        if not all(isinstance(value, str) and value for value in identity.values()):
            raise RecoveryError(
                "original execution transaction lacks one exact run/session identity"
            )
        return identity  # type: ignore[return-value]

    def _metadata_diagnostic(self, original_plan: dict[str, Any]) -> str:
        try:
            self._replace_factory_position(
                (
                    self.project.repository / "docs/CURRENT_STATUS.md"
                ).read_text(encoding="utf-8"),
                feature_id=str(original_plan["feature_id"]),
                title=str(original_plan["feature_title"]),
            )
        except (OSError, RecoveryError) as exc:
            return redact_text(str(exc))
        raise RecoveryError(
            "failed recovery diagnostic cannot be reproduced from retained bytes"
        )

    def inspect(
        self,
        *,
        feature_id: str,
        original_transaction_id: str,
        failed_recovery_transaction_id: str,
        expected_branch: str,
        expected_head: str,
        allowed_reservation_run_id: str | None = None,
    ) -> dict[str, Any]:
        identity = self._original_identity(original_transaction_id)
        original_plan = self._inspect_general(
            feature_id=feature_id,
            original_transaction_id=original_transaction_id,
            original_run_id=identity["original_run_id"],
            original_session_id=identity["original_session_id"],
            expected_branch=expected_branch,
            expected_head=expected_head,
            allowed_reservation_run_id=allowed_reservation_run_id,
        )
        events = self.ledger.read()
        failed_events = [
            item
            for item in events
            if item.get("transaction_id") == failed_recovery_transaction_id
        ]
        event_types = [item.get("event_type") for item in failed_events]
        start = next(
            (item for item in failed_events if item.get("event_type") == "TransactionStarted"),
            None,
        )
        captured = next(
            (item for item in failed_events if item.get("event_type") == "SnapshotCaptured"),
            None,
        )
        blocked = next(
            (item for item in failed_events if item.get("event_type") == "TransactionBlocked"),
            None,
        )
        released = next(
            (item for item in failed_events if item.get("event_type") == "LeaseReleased"),
            None,
        )
        projected = next(
            (item for item in failed_events if item.get("event_type") == "ProjectionUpdated"),
            None,
        )
        validation_failed = [
            item for item in failed_events if item.get("event_type") == "ValidationFailed"
        ]
        start_payload = (start or {}).get("payload") or {}
        start_snapshot = (captured or {}).get("payload", {}).get("snapshot") or {}
        terminal = (blocked or {}).get("payload") or {}
        terminal_snapshot = terminal.get("terminal_snapshot") or {}
        current = capture_repository_snapshot(self.project).to_dict()
        projection = self.projection.current()
        metadata_failure_topology = [
            "TransactionStarted",
            "LeaseAcquired",
            "SnapshotCaptured",
            "TransactionBlocked",
            "LeaseReleased",
            "ProjectionUpdated",
        ]
        command_failure_topology = [
            "TransactionStarted",
            "LeaseAcquired",
            "SnapshotCaptured",
            "ValidationFailed",
            "TransactionBlocked",
            "LeaseReleased",
            "ProjectionUpdated",
        ]
        checks = {
            "failed_recovery_exact_topology": event_types
            in [metadata_failure_topology, command_failure_topology],
            "failed_recovery_feature": start_payload.get("feature_id") == feature_id,
            "failed_recovery_original_transaction": start_payload.get(
                "recovered_transaction_id"
            )
            == original_transaction_id,
            "failed_recovery_plan": start_payload.get("plan_fingerprint")
            == original_plan["plan_fingerprint"],
            "failed_recovery_zero_models": start_payload.get(
                "model_sessions_launched"
            )
            == 0,
            "failed_recovery_zero_children": start_payload.get(
                "child_sessions_launched"
            )
            == 0,
            "failed_recovery_snapshot": all(
                start_snapshot.get(key) == current.get(key)
                for key in (
                    "branch",
                    "head",
                    "queue_fingerprint",
                    "repository_identity",
                    "repository_path_fingerprint",
                    "tracked_diff_fingerprint",
                    "untracked_fingerprint",
                    "git_operations",
                )
            )
            and tuple(start_snapshot.get("tracked_changed_paths") or ())
            == tuple(current.get("tracked_changed_paths") or ())
            and tuple(start_snapshot.get("untracked_paths") or ())
            == tuple(current.get("untracked_paths") or ()),
            "failed_recovery_terminal": terminal.get("classification")
            == "FEATURE_VALIDATION_FAILED"
            and terminal.get("terminal_state") == "terminal_failure",
            "failed_recovery_terminal_snapshot": all(
                terminal_snapshot.get(key) == current.get(key)
                for key in (
                    "branch",
                    "head",
                    "queue_fingerprint",
                    "repository_identity",
                    "repository_path_fingerprint",
                    "tracked_diff_fingerprint",
                    "untracked_fingerprint",
                    "git_operations",
                )
            )
            and tuple(terminal_snapshot.get("tracked_changed_paths") or ())
            == tuple(current.get("tracked_changed_paths") or ())
            and tuple(terminal_snapshot.get("untracked_paths") or ())
            == tuple(current.get("untracked_paths") or ()),
            "failed_recovery_lease_released": isinstance(
                (released or {}).get("payload", {}).get("lease_id"), str
            ),
            "failed_recovery_projected": (projected or {}).get(
                "payload", {}
            ).get("current_feature")
            == feature_id
            and projection.get("active_transaction") is None,
            "validation_evidence_tied": len(validation_failed) <= 1,
            "retained_paths_unchanged": original_plan["changed_paths"]
            == list(_changed_paths(self.inspector)),
            "untracked_paths_authenticated": original_plan["untracked_paths"]
            == list(current.get("untracked_paths") or ()),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(
                f"{feature_id} retained-feature repair preflight failed: {failed}"
            )
        if validation_failed:
            failed_payload = validation_failed[0].get("payload") or {}
            raw_commands = failed_payload.get("commands")
            if not isinstance(raw_commands, list) or not raw_commands:
                raise RecoveryError(
                    "failed recovery ValidationFailed event lacks command evidence"
                )
            commands = [
                {
                    **item,
                    "status": (
                        "passed" if item.get("exit_code") == 0 else "failed"
                    ),
                    "evidence_source": "failed recovery ValidationFailed event",
                    "stdout_report_location": None,
                    "stderr_report_location": None,
                }
                for item in raw_commands
                if isinstance(item, dict)
            ]
            if len(commands) != len(raw_commands):
                raise RecoveryError(
                    "failed recovery command evidence contains a malformed item"
                )
            failed_records = [
                item for item in commands if item.get("exit_code") != 0
            ]
            if len(failed_records) != 1:
                raise RecoveryError(
                    "failed recovery command evidence must identify one failure"
                )
            diagnostic = str(
                failed_records[0].get("output_summary")
                or "trusted-host validation command failed"
            )
            environment_failure = self._environment_failure(failed_records[0])
            failure_category = (
                "environment_failure"
                if environment_failure
                else "implementation_or_test_failure"
            )
            classification = "host_validation_failure"
        else:
            diagnostic = self._metadata_diagnostic(original_plan)
            commands = [
                {
                    "argv": argv,
                    "exit_code": 0,
                    "status": "passed",
                    "evidence_source": (
                        "authenticated control flow reached acceptance metadata; "
                        "the deterministic route returns immediately on nonzero exit"
                    ),
                    "stdout_report_location": None,
                    "stderr_report_location": None,
                }
                for argv in original_plan["host_validation_commands"]
            ]
            environment_failure = False
            failure_category = "controller_acceptance_metadata_failure"
            classification = "malformed_validation_evidence"
        failed_run_id = str(start_payload.get("run_id") or "")
        report_candidate = (
            self.controller_root
            / "reports"
            / failed_run_id
            / "feature-result-recovery.json"
        )
        evidence = {
            "classification": classification,
            "failure_category": failure_category,
            "environment_failure": environment_failure,
            "diagnostic": diagnostic,
            "diagnostic_path": "docs/CURRENT_STATUS.md",
            "commands": commands,
            "commands_passed_before_failure": len(
                [item for item in commands if item.get("exit_code") == 0]
            ),
            "failed_recovery_report_path": (
                str(report_candidate) if report_candidate.is_file() else None
            ),
            "original_execution_report_path": original_plan["report_path"],
            "stdout_report_locations": [],
            "stderr_report_locations": [],
            "validation_failed_event_present": bool(validation_failed),
            "note": (
                "the failed deterministic route did not persist per-command "
                "stdout/stderr or a recovery report"
            ),
        }
        plan = {
            "schema_version": 1,
            "repair_contract": "retained_feature_validation_repair",
            "project_id": self.project.project_id,
            "repository_identity": original_plan["repository_identity"],
            "repository_path_fingerprint": original_plan[
                "repository_path_fingerprint"
            ],
            "feature_id": feature_id,
            "feature_title": original_plan["feature_title"],
            "milestone_id": original_plan["milestone_id"],
            "milestone_branch": original_plan["milestone_branch"],
            "original_transaction_id": original_transaction_id,
            "original_run_id": identity["original_run_id"],
            "original_session_id": identity["original_session_id"],
            "failed_recovery_transaction_id": failed_recovery_transaction_id,
            "failed_recovery_run_id": failed_run_id,
            "branch": expected_branch,
            "head": expected_head,
            "tracked_paths": original_plan["tracked_paths"],
            "untracked_paths": original_plan["untracked_paths"],
            "authenticated_retained_paths": original_plan["changed_paths"],
            "allowed_paths": original_plan["final_changed_paths"],
            "acceptance_metadata_paths": original_plan[
                "acceptance_metadata_paths"
            ],
            "commit_subject": original_plan["commit_subject"],
            "original_gate_id": original_plan["original_gate_id"],
            "original_gate_fingerprint": original_plan[
                "original_gate_fingerprint"
            ],
            "tracked_diff_fingerprint": original_plan[
                "tracked_diff_fingerprint"
            ],
            "untracked_fingerprint": original_plan["untracked_fingerprint"],
            "pre_repair_content_fingerprint": original_plan[
                "retained_content_fingerprint"
            ],
            "failed_validation_evidence": evidence,
            "repair_profile": {
                "model": self.MODEL,
                "reasoning": self.REASONING,
                "parent_sessions_per_attempt": 1,
                "child_sessions": self.CHILD_SESSIONS,
                "source": "authoritative F068 multi_module_precise profile",
            },
            "host_validation_plan": original_plan["host_validation_commands"],
            "maximum_repair_attempts": self.MAX_ATTEMPTS,
            "candidate_commits_that_would_be_created": 1,
            "candidate_commit_created_only_after_all_validations": True,
            "expected_terminal_state": "integration_pending",
            "milestone_integration_performed": False,
            "queue_reconciliation_performed": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "application_repository_written": False,
            "checks": {**original_plan["checks"], **checks},
            "_original_plan": original_plan,
        }
        public = {key: value for key, value in plan.items() if key != "_original_plan"}
        plan["plan_fingerprint"] = fingerprint(public)
        return plan

    @staticmethod
    def _environment_failure(record: dict[str, Any]) -> bool:
        text = str(record.get("output_summary") or "").lower()
        return any(
            marker in text
            for marker in (
                "permission denied",
                "operation not permitted",
                "no such file or directory",
                "command not found",
                "unable to load standard library",
                "failed to clone",
                "network is unreachable",
            )
        )

    def _assert_live_identity(self, plan: dict[str, Any]) -> None:
        snapshot = capture_repository_snapshot(self.project)
        checks = {
            "branch": snapshot.branch == plan["branch"],
            "head": snapshot.head == plan["head"],
            "tracked_paths": list(snapshot.tracked_changed_paths)
            == plan["tracked_paths"],
            "untracked_paths": list(snapshot.untracked_paths)
            == plan["untracked_paths"],
            "tracked_fingerprint": snapshot.tracked_diff_fingerprint
            == plan["tracked_diff_fingerprint"],
            "untracked_fingerprint": snapshot.untracked_fingerprint
            == plan["untracked_fingerprint"],
            "no_git_operation": not any(snapshot.git_operations.values()),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(
                "retained-feature repair identity changed after lease acquisition: "
                + failed
            )

    def _repair_prompt(self, plan: dict[str, Any], attempt: int) -> str:
        diagnostics = json.dumps(
            plan["failed_validation_evidence"], indent=2, sort_keys=True
        )
        return f"""Retained F068 validation repair attempt {attempt}.

The exact authenticated retained implementation is the starting point. Fix only
the validation failure below; do not redesign F068 or refactor unrelated code.
The only authorized paths are:
{json.dumps(plan["allowed_paths"], indent=2)}

Never reset, stash, discard, switch branches, stage, commit, integrate, reconcile
the queue, or replace the retained implementation wholesale. Do not modify an
unlisted path. If one additional path appears necessary, stop and explain the
feature-contract proof instead of changing it.

Exact failed trusted-host evidence:
{diagnostics}
"""

    def _attributed_paths(self, stdout: str) -> list[str]:
        repository = self.project.repository.resolve()
        paths: set[str] = set()
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if (
                event.get("type") != "item.completed"
                or not isinstance(item, dict)
                or item.get("type") != "file_change"
                or item.get("status") != "completed"
            ):
                continue
            changes = item.get("changes")
            if not isinstance(changes, list):
                raise RecoveryError(
                    "repair session file-change evidence is malformed"
                )
            for change in changes:
                raw = change.get("path") if isinstance(change, dict) else None
                if not isinstance(raw, str) or not raw:
                    raise RecoveryError(
                        "repair session file-change evidence lacks one exact path"
                    )
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = repository / candidate
                try:
                    relative = candidate.resolve().relative_to(repository)
                except (OSError, ValueError) as exc:
                    raise RecoveryError(
                        "repair session file-change evidence escapes the repository"
                    ) from exc
                paths.add(str(relative))
        return sorted(paths)

    def _attempt_report(
        self,
        *,
        run_id: str,
        feature_id: str,
        transaction_id: str,
        attempt: int,
        result: SessionResult,
        pre_fingerprint: str,
        post_fingerprint: str,
        observed_paths: list[str],
        attributed_session_paths: list[str],
        authorized_paths: list[str],
    ) -> Path:
        path = (
            self.controller_root
            / "reports"
            / run_id
            / f"feature-repair-attempt-{attempt}.json"
        )
        atomic_write_json(
            path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "attempt": attempt,
                "feature_id": feature_id,
                "transaction_id": transaction_id,
                "session_id": result.session_id,
                "returncode": result.returncode,
                "result_classification": result.result_classification,
                "structured_output_validation": result.structured_output_validation,
                "structured_output_errors": list(result.structured_output_errors),
                "pre_repair_fingerprint": pre_fingerprint,
                "post_repair_fingerprint": post_fingerprint,
                "candidate_changed_during_session": (
                    pre_fingerprint != post_fingerprint
                ),
                "observed_changed_paths": observed_paths,
                "changed_paths_attributable_to_session": (
                    attributed_session_paths
                ),
                "authorized_paths": authorized_paths,
                "invoked_route": "retained-feature-validation-repair",
                "invoked_workflow_type": "feature_cycle",
                "emitted_workflow_type": (
                    (self._raw_terminal_envelope(result.redacted_stdout) or {}).get(
                        "workflow_type"
                    )
                ),
                "model": result.plan.launched_model,
                "reasoning": result.plan.launched_reasoning,
                "child_sessions": 0,
                "redacted_stdout": result.redacted_stdout,
                "redacted_stderr": result.redacted_stderr,
                "created_at": utc_now(),
            },
        )
        return path

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: value
            for key, value in plan.items()
            if key not in {"_original_plan", "plan_fingerprint"}
        }
        if plan.get("plan_fingerprint") != fingerprint(public):
            raise RecoveryError("retained-feature repair plan fingerprint is invalid")
        run_id = f"feature-repair-{uuid.uuid4()}"
        reservation = DurableLock(
            self.controller_root
            / self.configuration["lock_policy"][
                "controller_launch_lock_directory"
            ]
            / f"{plan['repository_path_fingerprint']}.json"
        )
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=plan["repository_identity"],
                run_id=run_id,
                current_feature=plan["feature_id"],
                current_phase="retained_feature_validation_repair",
            )
        )
        kernel: WorkflowKernel | None = None
        metadata_originals: dict[str, bytes] | None = None
        attempts: list[dict[str, Any]] = []
        try:
            self.inspector.ensure_runtime_ignored()
            revalidated = self.inspect(
                feature_id=plan["feature_id"],
                original_transaction_id=plan["original_transaction_id"],
                failed_recovery_transaction_id=plan[
                    "failed_recovery_transaction_id"
                ],
                expected_branch=plan["branch"],
                expected_head=plan["head"],
                allowed_reservation_run_id=run_id,
            )
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "retained-feature repair evidence changed under reservation"
                )
            adapter = FeatureExecutionAdapter(
                allowed_paths=tuple(plan["allowed_paths"]),
                commit_subject=str(plan["commit_subject"]),
                next_state="integration_pending",
                allow_untracked=bool(plan["untracked_paths"]),
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
                feature_id=str(plan["feature_id"]),
                run_id=run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovery_mode": "retained_feature_validation_repair",
                    "original_transaction_id": plan["original_transaction_id"],
                    "failed_recovery_transaction_id": plan[
                        "failed_recovery_transaction_id"
                    ],
                    "pre_repair_fingerprint": plan[
                        "pre_repair_content_fingerprint"
                    ],
                    "repair_profile": plan["repair_profile"],
                    "maximum_repair_attempts": plan[
                        "maximum_repair_attempts"
                    ],
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
                expected_starting_branch=str(plan["branch"]),
                expected_starting_head=str(plan["head"]),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            self._assert_live_identity(plan)
            previous_signature = None
            final_result: SessionResult | None = None
            commands: list[dict[str, Any]] = []
            for attempt in range(1, int(plan["maximum_repair_attempts"]) + 1):
                pre_fingerprint = self.inspector.content_diff_fingerprint(
                    tuple(plan["allowed_paths"])
                )
                request = SessionRequest(
                    action="feature_cycle",
                    project=self.project,
                    run_id=run_id,
                    mode="retained-feature-validation-repair",
                    feature=str(plan["feature_id"]),
                    transaction_id=transaction.transaction_id,
                    repository_identity=str(plan["repository_identity"]),
                    starting_branch=str(plan["branch"]),
                    starting_commit=str(plan["head"]),
                    allowed_paths=tuple(plan["allowed_paths"]),
                    parent_session_budget=1,
                    child_session_budget=0,
                    planned_model=self.MODEL,
                    planned_reasoning=self.REASONING,
                    model_plan_source="retained_feature_repair_profile",
                    selected_profile="multi_module_precise",
                    context_files=tuple(
                        path
                        for path in (
                            f"docs/features/{plan['feature_id']}-"
                            + str(plan["feature_title"]).lower().replace(" ", "-")
                            + ".md",
                            "docs/CURRENT_STATUS.md",
                        )
                        if (self.project.repository / path).is_file()
                    ),
                    embedded_context=self._repair_prompt(plan, attempt),
                    repair_attempt=attempt,
                    repair_evidence=fingerprint(
                        plan["failed_validation_evidence"]
                    ),
                    repair_hypothesis="repair the exact failed validation diagnostic only",
                    remediation_action="minimal authorized-path correction",
                    repair_supporting_evidence=plan[
                        "failed_validation_evidence"
                    ]["diagnostic"],
                )
                launcher = self.launcher_factory()
                result = launcher.launch(
                    request, on_session_started=kernel.session_launched
                )
                post_fingerprint = self.inspector.content_diff_fingerprint(
                    tuple(plan["allowed_paths"])
                )
                observed_paths = list(_changed_paths(self.inspector))
                attributed_session_paths = self._attributed_paths(
                    result.redacted_stdout
                )
                report_path = self._attempt_report(
                    run_id=run_id,
                    feature_id=str(plan["feature_id"]),
                    transaction_id=transaction.transaction_id,
                    attempt=attempt,
                    result=result,
                    pre_fingerprint=pre_fingerprint,
                    post_fingerprint=post_fingerprint,
                    observed_paths=observed_paths,
                    attributed_session_paths=attributed_session_paths,
                    authorized_paths=list(plan["allowed_paths"]),
                )
                unexpected = sorted(
                    set(_changed_paths(self.inspector)) - set(plan["allowed_paths"])
                )
                unexpected_untracked = sorted(
                    set(self.inspector.untracked_file_hashes())
                    - set(plan["untracked_paths"])
                )
                unexpected_attributed = sorted(
                    set(attributed_session_paths) - set(plan["allowed_paths"])
                )
                if unexpected or unexpected_untracked or unexpected_attributed:
                    raise RecoveryError(
                        "repair session changed unexpected paths: "
                        + ", ".join(
                            unexpected
                            or unexpected_untracked
                            or unexpected_attributed
                        )
                    )
                attempt_evidence: dict[str, Any] = {
                    "attempt": attempt,
                    "session_id": result.session_id,
                    "report_path": str(report_path),
                    "pre_repair_fingerprint": pre_fingerprint,
                    "post_repair_fingerprint": post_fingerprint,
                    "candidate_changed_during_session": (
                        pre_fingerprint != post_fingerprint
                    ),
                    "observed_changed_paths": observed_paths,
                    "changed_paths_attributable_to_session": (
                        attributed_session_paths
                    ),
                    "transaction_id": transaction.transaction_id,
                    "model": result.plan.launched_model,
                    "reasoning": result.plan.launched_reasoning,
                    "child_sessions": 0,
                    "commands": [],
                }
                self.ledger.append(
                    event_type="ValidationStarted",
                    transaction_id=transaction.transaction_id,
                    workflow_type=transaction.workflow_type,
                    payload={
                        "repair_attempt": attempt,
                        "changed_paths": list(_changed_paths(self.inspector)),
                        "executor": "retained_feature_repair",
                    },
                )
                failure_diagnostic = None
                environment_failure = False
                if result.returncode != 0 or not isinstance(
                    result.transaction_envelope, dict
                ):
                    failure_diagnostic = (
                        result.primary_terminal_error
                        or "; ".join(result.structured_output_errors)
                        or result.redacted_stderr[-1200:]
                        or "repair session did not return an authenticated result"
                    )
                    environment_failure = result.failure_classification in {
                        "cli_missing",
                        "cli_version_too_old",
                        "unsupported_model",
                        "unsupported_reasoning_effort",
                        "compatibility_unknown",
                    }
                else:
                    try:
                        metadata_originals = self._apply_general_acceptance_metadata(
                            plan=plan["_original_plan"],
                            run_id=run_id,
                            commands=[],
                        )
                    except RecoveryError as exc:
                        failure_diagnostic = str(exc)
                    if failure_diagnostic is None:
                        commands = []
                        for argv in plan["host_validation_plan"]:
                            record = self._run(list(argv), commands)
                            attempt_evidence["commands"].append(record)
                            if record["exit_code"] != 0:
                                failure_diagnostic = str(
                                    record.get("output_summary")
                                    or f"host validation failed: {' '.join(argv)}"
                                )
                                environment_failure = self._environment_failure(record)
                                break
                if failure_diagnostic is not None:
                    if metadata_originals is not None:
                        self._restore_metadata(metadata_originals)
                        metadata_originals = None
                    failure_fingerprint = hashlib.sha256(
                        redact_text(failure_diagnostic).encode("utf-8")
                    ).hexdigest()
                    raw_envelope = self._raw_terminal_envelope(
                        result.redacted_stdout
                    )
                    signature = self._semantic_failure_signature(
                        route="retained_feature_validation_repair",
                        feature_id=str(plan["feature_id"]),
                        invoked_workflow="feature_cycle",
                        emitted_workflow=(
                            raw_envelope.get("workflow_type")
                            if isinstance(raw_envelope, dict)
                            else None
                        ),
                        diagnostic=failure_diagnostic,
                        envelope_classification=(
                            raw_envelope.get("classification")
                            if isinstance(raw_envelope, dict)
                            else result.result_classification
                        ),
                        authorized_paths=list(plan["allowed_paths"]),
                        environment_failure=environment_failure,
                    )
                    attempt_evidence.update(
                        {
                            "outcome": "validation_failed",
                            "diagnostic": redact_text(failure_diagnostic),
                            "failure_fingerprint": failure_fingerprint,
                            "failure_signature": signature,
                            "environment_failure": environment_failure,
                            "identical_to_previous": signature == previous_signature,
                        }
                    )
                    attempts.append(attempt_evidence)
                    self.ledger.append(
                        event_type="ValidationFailed",
                        transaction_id=transaction.transaction_id,
                        workflow_type=transaction.workflow_type,
                        payload={
                            **attempt_evidence,
                            "diagnostic": attempt_evidence["diagnostic"],
                            "executor": "retained_feature_repair",
                        },
                    )
                    if environment_failure or signature == previous_signature:
                        break
                    previous_signature = signature
                    continue
                attempt_evidence["outcome"] = "validation_passed"
                attempts.append(attempt_evidence)
                self.ledger.append(
                    event_type="CheckpointRecorded",
                    transaction_id=transaction.transaction_id,
                    workflow_type=transaction.workflow_type,
                    payload={
                        **attempt_evidence,
                        "checkpoint": "retained_feature_repair_attempt_passed",
                        "executor": "retained_feature_repair",
                    },
                )
                final_result = result
                break
            if final_result is None:
                projection = kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="FEATURE_VALIDATION_FAILED",
                    next_state="review",
                )
                report = {
                    "schema_version": 1,
                    "project_id": self.project.project_id,
                    "feature_id": plan["feature_id"],
                    "outcome": "repair_exhausted",
                    "recoverable_technical_failure": True,
                    "human_gate_created": False,
                    "application_commit_created": False,
                    "retained_mutations_preserved": True,
                    "candidate_changed_during_repair": any(
                        item["pre_repair_fingerprint"]
                        != item["post_repair_fingerprint"]
                        for item in attempts
                    ),
                    "deterministic_recovery_command": (
                        "scripts/conveyor recover-feature-repair "
                        f"--project {self.project.project_id} "
                        f"--feature {plan['feature_id']} "
                        f"--repair-transaction-id "
                        f"{transaction.transaction_id} --dry-run"
                    ),
                    "attempts": attempts,
                    "model_sessions_launched": len(attempts),
                    "child_sessions_launched": 0,
                    "projection": projection,
                    "created_at": utc_now(),
                }
                report_path = (
                    self.controller_root
                    / "reports"
                    / run_id
                    / "retained-feature-repair.json"
                )
                atomic_write_json(report_path, report)
                report["report_path"] = str(report_path)
                return report
            final_envelope = dict(final_result.transaction_envelope)
            final_envelope["changed_paths"] = list(_changed_paths(self.inspector))
            final_envelope["next_state"] = "integration_pending"
            envelope = SessionResultEnvelope.from_dict(final_envelope)
            kernel.accept_result(envelope)
            kernel.record_file_mutation_boundary()
            authority = CommandAuthority(
                configured_required=plan["host_validation_plan"]
            )
            records = [
                authority.classify(
                    item["argv"],
                    item["exit_code"],
                    configured_source="retained_feature_contract",
                    diagnostic=item.get("output_summary"),
                )
                for item in commands
            ]
            kernel.validate(
                authority=authority,
                command_results=records,
                semantic_validator=adapter.semantic_validate,
            )
            accepted_commit = kernel.finalize()
            if not isinstance(accepted_commit, str):
                raise TransactionError("repair finalization created no feature commit")
            metadata_originals = None
            for checkpoint, superseded in (
                ("structured_output_invalid_transaction_superseded", plan["original_transaction_id"]),
                ("failed_recovery_transaction_superseded", plan["failed_recovery_transaction_id"]),
            ):
                self.ledger.append(
                    event_type="CheckpointRecorded",
                    transaction_id=transaction.transaction_id,
                    workflow_type=transaction.workflow_type,
                    payload={
                        "checkpoint": checkpoint,
                        "superseded_transaction_id": superseded,
                        "superseding_transaction_id": transaction.transaction_id,
                    },
                )
            self.ledger.append(
                event_type="HumanGateResolved",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "gate_id": plan["original_gate_id"],
                    "gate_fingerprint": plan["original_gate_fingerprint"],
                    "raised_transaction_id": plan["original_transaction_id"],
                    "resolution": "retained_feature_repaired_validated_and_accepted",
                },
            )
            completion = kernel.complete(
                classification="FEATURE_ACCEPTED",
                evidence={
                    "accepted_feature_commit": accepted_commit,
                    "candidate_implementation_commit": accepted_commit,
                    "integration_status": "pending",
                    "recovered_transaction_id": plan["original_transaction_id"],
                    "failed_recovery_transaction_id": plan[
                        "failed_recovery_transaction_id"
                    ],
                    "resolved_gate_id": plan["original_gate_id"],
                    "host_validation_passed": True,
                    "model_sessions_launched": len(attempts),
                    "child_sessions_launched": 0,
                },
            )
            projection = completion["projection"]
            self._materialize_general_compatibility_caches(
                feature_id=str(plan["feature_id"]),
                run_id=run_id,
                transaction_id=transaction.transaction_id,
                accepted_commit=accepted_commit,
                projection=projection,
            )
            report = {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "feature_id": plan["feature_id"],
                "outcome": "integration_pending",
                "run_id": run_id,
                "repair_transaction_id": transaction.transaction_id,
                "original_transaction_id": plan["original_transaction_id"],
                "failed_recovery_transaction_id": plan[
                    "failed_recovery_transaction_id"
                ],
                "accepted_feature_commit": accepted_commit,
                "pre_repair_fingerprint": plan["pre_repair_content_fingerprint"],
                "post_repair_fingerprint": attempts[-1][
                    "post_repair_fingerprint"
                ],
                "attempts": attempts,
                "commands": commands,
                "model_sessions_launched": len(attempts),
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
                / "retained-feature-repair.json"
            )
            atomic_write_json(report_path, report)
            report["report_path"] = str(report_path)
            return report
        except Exception:
            if metadata_originals is not None and self.inspector.head == plan["head"]:
                self._restore_metadata(metadata_originals)
            if kernel is not None and kernel.transaction is not None:
                try:
                    if kernel.lease.read() is not None:
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


class RetainedFeatureRepairRecovery(RetainedFeatureValidationRepair):
    """Deterministically accept one exhausted, fully attributed repair chain."""

    ROUTE = "retained_feature_repair_recovery"
    INVOKED_WORKFLOW = "feature_cycle"
    EMITTED_WORKFLOW = WorkflowType.FEATURE_EXECUTION.value

    def _attempt_envelope(
        self,
        *,
        report: dict[str, Any],
        transaction_id: str,
        run_id: str,
        branch: str,
        head: str,
        feature_id: str,
        allowed_paths: list[str],
    ) -> dict[str, Any]:
        stdout = report.get("redacted_stdout")
        session_id = report.get("session_id")
        if not isinstance(stdout, str) or not isinstance(session_id, str):
            raise RecoveryError("repair attempt lacks terminal session evidence")
        envelope = extract_terminal_envelope(
            stdout,
            corroborated={
                "workflow_type": self.EMITTED_WORKFLOW,
                "project_id": self.project.project_id,
                "repository_identity": self.inspector.identity()["repository_id"],
                "transaction_id": transaction_id,
                "run_id": run_id,
                "session_id": session_id,
                "starting_branch": branch,
                "starting_commit": head,
            },
        ).to_dict()
        checks = {
            "schema": envelope.get("schema_version") == 1,
            "classification": envelope.get("classification") == "FEATURE_ACCEPTED",
            "next_state": envelope.get("next_state") == "feature_accepted",
            "feature": envelope.get("feature_id") == feature_id,
            "current_commit": envelope.get("current_commit") == head,
            "authorized_path_set": tuple(envelope.get("changed_paths") or ())
            == tuple(allowed_paths),
            "implementation_complete": (
                (envelope.get("evidence") or {}).get("implementation_complete")
                is True
            ),
            "controller_acceptance_pending": (
                (envelope.get("evidence") or {}).get(
                    "controller_acceptance_pending"
                )
                is True
            ),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(
                "repair attempt terminal envelope failed exact normalization: "
                + failed
            )
        return envelope

    def inspect(
        self,
        *,
        feature_id: str,
        repair_transaction_id: str,
        allowed_reservation_run_id: str | None = None,
    ) -> dict[str, Any]:
        integrity = self.ledger.verify()
        events = self.ledger.read()
        transaction_events = [
            event
            for event in events
            if event.get("transaction_id") == repair_transaction_id
        ]
        event_types = [event.get("event_type") for event in transaction_events]
        expected_topology = [
            "TransactionStarted",
            "LeaseAcquired",
            "SnapshotCaptured",
            "SessionLaunched",
            "ValidationStarted",
            "ValidationFailed",
            "SessionLaunched",
            "ValidationStarted",
            "ValidationFailed",
            "TransactionBlocked",
            "LeaseReleased",
            "ProjectionUpdated",
        ]
        start = next(
            (
                event
                for event in transaction_events
                if event.get("event_type") == "TransactionStarted"
            ),
            None,
        )
        captured = next(
            (
                event
                for event in transaction_events
                if event.get("event_type") == "SnapshotCaptured"
            ),
            None,
        )
        blocked = next(
            (
                event
                for event in transaction_events
                if event.get("event_type") == "TransactionBlocked"
            ),
            None,
        )
        validation_failures = [
            event
            for event in transaction_events
            if event.get("event_type") == "ValidationFailed"
        ]
        session_events = [
            event
            for event in transaction_events
            if event.get("event_type") == "SessionLaunched"
        ]
        start_payload = (start or {}).get("payload") or {}
        starting_snapshot = (captured or {}).get("payload", {}).get("snapshot") or {}
        terminal_payload = (blocked or {}).get("payload") or {}
        terminal_snapshot = terminal_payload.get("terminal_snapshot") or {}
        policy = start_payload.get("allowed_mutation_policy") or {}
        allowed_paths = list(policy.get("allowed_paths") or ())
        branch = start_payload.get("starting_branch")
        head = start_payload.get("starting_head")
        run_id = start_payload.get("run_id")
        original_transaction_id = start_payload.get("original_transaction_id")
        failed_recovery_transaction_id = start_payload.get(
            "failed_recovery_transaction_id"
        )
        identity = self.inspector.identity()
        current = capture_repository_snapshot(self.project).to_dict()
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
        ownership_path = (
            self.controller_root
            / "state"
            / "autopilot"
            / self.project.project_id
            / "ownership.json"
        )
        repair_report_path = (
            self.controller_root
            / "reports"
            / str(run_id)
            / "retained-feature-repair.json"
        )
        try:
            repair_report = json.loads(
                repair_report_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "exhausted retained-feature repair report is unavailable"
            ) from exc

        attempts: list[dict[str, Any]] = []
        previous_post: str | None = None
        if len(validation_failures) != len(session_events):
            raise RecoveryError(
                "repair attempt sessions and failures do not form exact pairs"
            )
        for index, (failure, session) in enumerate(
            zip(validation_failures, session_events), start=1
        ):
            payload = failure.get("payload") or {}
            session_id = (session.get("payload") or {}).get("session_id")
            report_path = Path(str(payload.get("report_path") or "")).resolve()
            report_root = (self.controller_root / "reports").resolve()
            try:
                report_path.relative_to(report_root)
            except ValueError as exc:
                raise RecoveryError(
                    "repair attempt report escapes the controller report root"
                ) from exc
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RecoveryError("repair attempt report is unavailable") from exc
            report_checks = {
                "attempt": report.get("attempt") == index == payload.get("attempt"),
                "run": report.get("run_id") == run_id,
                "feature": report.get("feature_id") == feature_id,
                "session": report.get("session_id") == session_id,
                "returncode": report.get("returncode") == 0,
                "result_classification": (
                    report.get("result_classification")
                    == "structured_output_invalid"
                ),
                "structured_validation": (
                    report.get("structured_output_validation") == "invalid"
                ),
                "diagnostic": report.get("structured_output_errors")
                == [
                    "terminal session-result envelope workflow_type conflicts "
                    "with invoked workflow"
                ],
                "pre_fingerprint": report.get("pre_repair_fingerprint")
                == payload.get("pre_repair_fingerprint"),
                "post_fingerprint": report.get("post_repair_fingerprint")
                == payload.get("post_repair_fingerprint"),
                "zero_children": report.get("child_sessions") == 0,
            }
            if not all(report_checks.values()):
                failed = ", ".join(
                    key for key, passed in report_checks.items() if not passed
                )
                raise RecoveryError(
                    f"repair attempt {index} report authentication failed: {failed}"
                )
            envelope = self._attempt_envelope(
                report=report,
                transaction_id=repair_transaction_id,
                run_id=str(run_id),
                branch=str(branch),
                head=str(head),
                feature_id=feature_id,
                allowed_paths=allowed_paths,
            )
            attributed = self._attributed_paths(str(report["redacted_stdout"]))
            unauthorized = sorted(set(attributed) - set(allowed_paths))
            if unauthorized:
                raise RecoveryError(
                    "repair attempt changed unauthorized paths: "
                    + ", ".join(unauthorized)
                )
            pre_fingerprint = str(payload.get("pre_repair_fingerprint") or "")
            post_fingerprint = str(payload.get("post_repair_fingerprint") or "")
            continuous = (
                previous_post == pre_fingerprint
                if previous_post is not None
                else pre_fingerprint
                == start_payload.get("pre_repair_fingerprint")
            )
            attempts.append(
                {
                    "attempt": index,
                    "session_id": session_id,
                    "report_path": str(report_path),
                    "invoked_route": "retained-feature-validation-repair",
                    "invoked_workflow_type": self.INVOKED_WORKFLOW,
                    "emitted_workflow_type": envelope["workflow_type"],
                    "session_result_schema_version": envelope["schema_version"],
                    "session_result_classification": envelope[
                        "classification"
                    ],
                    "controller_result_classification": report[
                        "result_classification"
                    ],
                    "pre_repair_fingerprint": pre_fingerprint,
                    "post_repair_fingerprint": post_fingerprint,
                    "changed_paths_attributable_to_session": attributed,
                    "unauthorized_paths": unauthorized,
                    "continuous_from_previous": continuous,
                }
            )
            previous_post = post_fingerprint

        queue = json.loads(
            (self.project.repository / self.project.queue_location).read_text(
                encoding="utf-8"
            )
        )
        features = [
            item
            for item in queue.get("features", [])
            if isinstance(item, dict) and item.get("id") == feature_id
        ]
        if len(features) != 1:
            raise RecoveryError("exhausted repair feature is not unique in queue")
        feature = features[0]
        preparation = max(
            (
                event
                for event in events
                if event.get("event_type") == "TransactionStarted"
                and event.get("workflow_type")
                == WorkflowType.FEATURE_PREPARATION.value
                and (event.get("payload") or {}).get("feature_id") == feature_id
                and (event.get("payload") or {}).get("run_id")
                == (
                    next(
                        (
                            item
                            for item in events
                            if item.get("transaction_id")
                            == original_transaction_id
                            and item.get("event_type") == "TransactionStarted"
                        ),
                        {},
                    ).get("payload")
                    or {}
                ).get("run_id")
            ),
            key=lambda event: int(event.get("sequence") or 0),
            default=None,
        )
        gate_event = next(
            (
                event
                for event in events
                if event.get("transaction_id") == original_transaction_id
                and event.get("event_type") == "HumanGateRaised"
            ),
            None,
        )
        gate_payload = (gate_event or {}).get("payload") or {}
        gate = gate_payload.get("gate") or {}
        metadata_paths = sorted(
            {
                self.project.queue_location,
                "docs/CURRENT_STATUS.md",
                "docs/FEATURE_CATALOG.md",
                "docs/RUN_LOG.md",
                str(feature.get("spec") or ""),
            }
        )
        base_plan = {
            "feature_id": feature_id,
            "feature_title": feature.get("title"),
            "milestone_id": feature.get("milestone"),
            "branch": branch,
            "head": head,
            "original_transaction_id": original_transaction_id,
            "preparation_transaction_id": (
                preparation.get("transaction_id") if preparation else None
            ),
            "acceptance_metadata_paths": metadata_paths,
        }
        preview_run_id = "feature-repair-recovery-preview"
        rendered = self._render_general_acceptance_metadata(
            plan=base_plan,
            run_id=preview_run_id,
            commands=[],
        )
        normalization = [
            {
                "path": relative,
                "would_change": (
                    (self.project.repository / relative).read_bytes()
                    != rendered[relative]
                ),
                "rendered_sha256": hashlib.sha256(
                    rendered[relative]
                ).hexdigest(),
            }
            for relative in metadata_paths
        ]
        current_content_fingerprint = self.inspector.content_diff_fingerprint(
            tuple(allowed_paths)
        )
        preserved_paths = sorted(set(allowed_paths) - set(metadata_paths))
        preserved_fingerprint = self.inspector.content_diff_fingerprint(
            tuple(preserved_paths)
        )
        stable_signature = self._semantic_failure_signature(
            route="retained_feature_validation_repair",
            feature_id=feature_id,
            invoked_workflow=self.INVOKED_WORKFLOW,
            emitted_workflow=self.EMITTED_WORKFLOW,
            diagnostic=(
                "terminal session-result envelope workflow_type conflicts "
                "with invoked workflow"
            ),
            envelope_classification="FEATURE_ACCEPTED",
            authorized_paths=allowed_paths,
            environment_failure=False,
        )
        checks = {
            "ledger_integrity": integrity.valid,
            "exact_transaction_topology": event_types == expected_topology,
            "repair_route": start_payload.get("recovery_mode")
            == "retained_feature_validation_repair",
            "feature_identity": start_payload.get("feature_id") == feature_id,
            "two_exact_attempts": len(attempts) == 2,
            "zero_children": start_payload.get("repair_profile", {}).get(
                "child_sessions"
            )
            == 0
            and all(
                attempt.get("controller_result_classification")
                == "structured_output_invalid"
                for attempt in attempts
            ),
            "fingerprint_chain_continuous": all(
                attempt["continuous_from_previous"] for attempt in attempts
            ),
            "starting_tracked_fingerprint": starting_snapshot.get(
                "tracked_diff_fingerprint"
            )
            == start_payload.get("starting_tracked_diff_fingerprint"),
            "terminal_tracked_fingerprint": terminal_snapshot.get(
                "tracked_diff_fingerprint"
            )
            == current.get("tracked_diff_fingerprint"),
            "terminal_content_fingerprint": previous_post
            == current_content_fingerprint,
            "exact_path_set_unchanged": tuple(
                starting_snapshot.get("tracked_changed_paths") or ()
            )
            == tuple(allowed_paths)
            == tuple(terminal_snapshot.get("tracked_changed_paths") or ())
            == tuple(current.get("tracked_changed_paths") or ()),
            "no_untracked_paths": not starting_snapshot.get("untracked_paths")
            and not terminal_snapshot.get("untracked_paths")
            and not current.get("untracked_paths"),
            "same_repository": current.get("repository_identity")
            == starting_snapshot.get("repository_identity")
            == identity["repository_id"],
            "same_branch_head": current.get("branch") == branch
            and current.get("head") == head
            and terminal_snapshot.get("branch") == branch
            and terminal_snapshot.get("head") == head,
            "no_git_operation": not any(
                (current.get("git_operations") or {}).values()
            )
            and not any((terminal_snapshot.get("git_operations") or {}).values()),
            "writer_lease_absent": not writer.exists,
            "controller_reservation_absent_or_owned": (
                not reservation.exists or reservation.owned_by_run
            ),
            "autopilot_ownership_absent": not ownership_path.exists(),
            "repair_exhausted": repair_report.get("outcome")
            == "repair_exhausted"
            and repair_report.get("application_commit_created") is False,
            "projection_terminal_review": self.projection.current().get(
                "current_state"
            )
            == "review"
            and self.projection.current().get("active_transaction") is None,
            "original_gate_exact": gate.get("gate_id")
            == self.projection.current().get("human_gate", {}).get("gate_id"),
            "duplicate_signature_normalized": len(
                {
                    self._semantic_failure_signature(
                        route="retained_feature_validation_repair",
                        feature_id=feature_id,
                        invoked_workflow=self.INVOKED_WORKFLOW,
                        emitted_workflow=self.EMITTED_WORKFLOW,
                        diagnostic=(
                            "terminal session-result envelope workflow_type "
                            "conflicts with invoked workflow"
                        ),
                        envelope_classification="FEATURE_ACCEPTED",
                        authorized_paths=allowed_paths,
                        environment_failure=False,
                    )
                    for _ in attempts
                }
            )
            == 1,
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise RecoveryError(
                f"{feature_id} exhausted-repair recovery preflight failed: {failed}"
            )
        plan = {
            "schema_version": 1,
            "recovery_contract": self.ROUTE,
            "project_id": self.project.project_id,
            "repository_identity": identity["repository_id"],
            "repository_path_fingerprint": identity["path_fingerprint"],
            "feature_id": feature_id,
            "feature_title": feature["title"],
            "milestone_id": feature["milestone"],
            "branch": branch,
            "head": head,
            "repair_run_id": run_id,
            "repair_transaction_id": repair_transaction_id,
            "original_transaction_id": original_transaction_id,
            "failed_recovery_transaction_id": failed_recovery_transaction_id,
            "preparation_transaction_id": base_plan[
                "preparation_transaction_id"
            ],
            "original_gate_id": gate.get("gate_id"),
            "original_gate_fingerprint": gate_payload.get("gate_fingerprint"),
            "allowed_paths": allowed_paths,
            "acceptance_metadata_paths": metadata_paths,
            "preserved_implementation_paths": preserved_paths,
            "preserved_implementation_fingerprint": preserved_fingerprint,
            "original_retained_tracked_fingerprint": starting_snapshot[
                "tracked_diff_fingerprint"
            ],
            "repair_terminal_tracked_fingerprint": terminal_snapshot[
                "tracked_diff_fingerprint"
            ],
            "current_candidate_fingerprint": current_content_fingerprint,
            "current_tracked_fingerprint": current["tracked_diff_fingerprint"],
            "untracked_fingerprint": current["untracked_fingerprint"],
            "attempt_chain": attempts,
            "workflow_normalization": {
                "route": "retained-feature-validation-repair",
                "invoked_workflow_type": self.INVOKED_WORKFLOW,
                "emitted_workflow_type": self.EMITTED_WORKFLOW,
                "normalized_workflow_type": self.EMITTED_WORKFLOW,
                "alias_scope": self.ROUTE,
            },
            "semantic_failure_signature": stable_signature,
            "deterministic_metadata_normalization": normalization,
            "host_validation_commands": [
                ["swift", "test", "--filter", "DomainModelTests"],
                ["swift", "test", "--filter", "PersistentDomainStoreTests"],
                ["swift", "build"],
                ["git", "diff", "--check"],
            ],
            "commit_subject": policy.get("commit_subject"),
            "candidate_commits_that_would_be_created": 1,
            "gate_supersession": {
                "gate_id": gate.get("gate_id"),
                "mode": "append_only",
            },
            "failed_transaction_supersession": [
                original_transaction_id,
                failed_recovery_transaction_id,
                repair_transaction_id,
            ],
            "expected_terminal_state": "integration_pending",
            "milestone_integration_performed": False,
            "queue_reconciliation_performed": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "application_commands_that_would_run": 4,
            "checks": checks,
        }
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        expected = fingerprint(
            {key: value for key, value in plan.items() if key != "plan_fingerprint"}
        )
        if plan.get("plan_fingerprint") != expected:
            raise RecoveryError(
                "retained-feature repair recovery plan fingerprint is invalid"
            )
        run_id = f"feature-repair-recovery-{uuid.uuid4()}"
        reservation = DurableLock(
            self.controller_root
            / self.configuration["lock_policy"][
                "controller_launch_lock_directory"
            ]
            / f"{plan['repository_path_fingerprint']}.json"
        )
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=plan["repository_identity"],
                run_id=run_id,
                current_feature=plan["feature_id"],
                current_phase=self.ROUTE,
            )
        )
        kernel: WorkflowKernel | None = None
        metadata_originals: dict[str, bytes] | None = None
        commands: list[dict[str, Any]] = []
        try:
            self.inspector.ensure_runtime_ignored()
            revalidated = self.inspect(
                feature_id=str(plan["feature_id"]),
                repair_transaction_id=str(plan["repair_transaction_id"]),
                allowed_reservation_run_id=run_id,
            )
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "retained-feature repair recovery evidence changed "
                    "under reservation"
                )
            adapter = FeatureExecutionAdapter(
                allowed_paths=tuple(plan["allowed_paths"]),
                commit_subject=str(plan["commit_subject"]),
                next_state="integration_pending",
                allow_untracked=False,
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
                feature_id=str(plan["feature_id"]),
                run_id=run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovery_mode": self.ROUTE,
                    "recovered_repair_transaction_id": plan[
                        "repair_transaction_id"
                    ],
                    "original_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "failed_recovery_transaction_id": plan[
                        "failed_recovery_transaction_id"
                    ],
                    "workflow_normalization": plan[
                        "workflow_normalization"
                    ],
                    "attempt_chain": plan["attempt_chain"],
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
                expected_starting_branch=str(plan["branch"]),
                expected_starting_head=str(plan["head"]),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
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
                            "authenticated_repair_chain_preserved": True,
                            "model_sessions_launched": 0,
                            "child_sessions_launched": 0,
                        },
                    )
                    projection = kernel.block(
                        state=TransactionState.TERMINAL_FAILURE,
                        classification="FEATURE_VALIDATION_FAILED",
                        next_state="review",
                    )
                    return {
                        "schema_version": 1,
                        "outcome": "validation_failed",
                        "feature_id": plan["feature_id"],
                        "failed_command": argv,
                        "commands": commands,
                        "application_commit_created": False,
                        "authenticated_repair_chain_preserved": True,
                        "model_sessions_launched": 0,
                        "child_sessions_launched": 0,
                        "projection": projection,
                    }
            if self.inspector.content_diff_fingerprint(
                tuple(plan["allowed_paths"])
            ) != plan["current_candidate_fingerprint"]:
                raise RecoveryError(
                    "authenticated repair candidate changed during host validation"
                )
            metadata_originals = self._apply_general_acceptance_metadata(
                plan=plan,
                run_id=run_id,
                commands=commands,
            )
            if self.inspector.content_diff_fingerprint(
                tuple(plan["preserved_implementation_paths"])
            ) != plan["preserved_implementation_fingerprint"]:
                self._restore_metadata(metadata_originals)
                metadata_originals = None
                raise RecoveryError(
                    "production implementation changed during metadata normalization"
                )
            if _changed_paths(self.inspector) != tuple(plan["allowed_paths"]):
                self._restore_metadata(metadata_originals)
                metadata_originals = None
                raise RecoveryError(
                    "deterministic metadata normalization changed the path set"
                )
            accepted_commit = kernel.finalize_deterministic_feature_recovery(
                original_transaction_id=str(plan["repair_transaction_id"]),
                changed_paths=tuple(plan["allowed_paths"]),
                original_content_fingerprint=str(
                    plan["current_candidate_fingerprint"]
                ),
                plan_fingerprint=str(plan["plan_fingerprint"]),
                validation_evidence={
                    "all_required_passed": True,
                    "commands": [
                        {
                            key: record.get(key)
                            for key in (
                                "argv",
                                "exit_code",
                                "duration_seconds",
                                "output_sha256",
                            )
                        }
                        for record in commands
                    ],
                    "checks": [
                        {
                            "name": "authenticated_repair_attempt_chain",
                            "passed": True,
                            "evidence": plan["attempt_chain"],
                        },
                        {
                            "name": "route_specific_workflow_alias",
                            "passed": True,
                            "evidence": plan["workflow_normalization"],
                        },
                    ],
                    "warnings": [],
                },
            )
            metadata_originals = None
            for checkpoint, superseded in (
                (
                    "structured_output_invalid_transaction_superseded",
                    plan["original_transaction_id"],
                ),
                (
                    "failed_recovery_transaction_superseded",
                    plan["failed_recovery_transaction_id"],
                ),
                (
                    "exhausted_repair_transaction_superseded",
                    plan["repair_transaction_id"],
                ),
            ):
                self.ledger.append(
                    event_type="CheckpointRecorded",
                    transaction_id=transaction.transaction_id,
                    workflow_type=transaction.workflow_type,
                    payload={
                        "checkpoint": checkpoint,
                        "superseded_transaction_id": superseded,
                        "superseding_transaction_id": transaction.transaction_id,
                    },
                )
            self.ledger.append(
                event_type="HumanGateResolved",
                transaction_id=transaction.transaction_id,
                workflow_type=transaction.workflow_type,
                payload={
                    "gate_id": plan["original_gate_id"],
                    "gate_fingerprint": plan["original_gate_fingerprint"],
                    "raised_transaction_id": plan["original_transaction_id"],
                    "resolution": (
                        "authenticated_repair_chain_validated_and_accepted"
                    ),
                },
            )
            completion = kernel.complete(
                classification="FEATURE_ACCEPTED",
                evidence={
                    "accepted_feature_commit": accepted_commit,
                    "candidate_implementation_commit": accepted_commit,
                    "integration_status": "pending",
                    "recovered_transaction_id": plan[
                        "repair_transaction_id"
                    ],
                    "original_transaction_id": plan[
                        "original_transaction_id"
                    ],
                    "failed_recovery_transaction_id": plan[
                        "failed_recovery_transaction_id"
                    ],
                    "resolved_gate_id": plan["original_gate_id"],
                    "host_validation_passed": True,
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                },
            )
            projection = completion["projection"]
            self._materialize_general_compatibility_caches(
                feature_id=str(plan["feature_id"]),
                run_id=run_id,
                transaction_id=transaction.transaction_id,
                accepted_commit=accepted_commit,
                projection=projection,
            )
            final_checks = {
                "direct_parent": self.inspector.rev_parse(
                    f"{accepted_commit}^", check=False
                )
                == plan["head"],
                "repository_clean": self.inspector.is_clean,
                "projection_integration_pending": projection.get(
                    "current_state"
                )
                == "integration_pending",
                "accepted_commit_projected": projection.get(
                    "accepted_feature_commit"
                )
                == accepted_commit,
                "human_gate_resolved": projection.get("human_gate") is None,
                "writer_lease_released": not self.inspector.writer_lock_path(
                    self.configuration["lock_policy"][
                        "writer_lock_relative_path"
                    ]
                ).exists(),
                "no_integration": not any(
                    event.get("transaction_id") == transaction.transaction_id
                    and event.get("workflow_type")
                    == WorkflowType.MILESTONE_INTEGRATION.value
                    for event in self.ledger.read()
                ),
                "no_queue_reconciliation": not any(
                    event.get("transaction_id") == transaction.transaction_id
                    and event.get("workflow_type")
                    == WorkflowType.QUEUE_RECONCILIATION.value
                    for event in self.ledger.read()
                ),
            }
            if not all(final_checks.values()):
                failed = ", ".join(
                    key for key, passed in final_checks.items() if not passed
                )
                raise RecoveryError(
                    "retained-feature repair recovery postcondition failed: "
                    + failed
                )
            report = {
                "schema_version": 1,
                "outcome": "integration_pending",
                "project_id": self.project.project_id,
                "feature_id": plan["feature_id"],
                "run_id": run_id,
                "recovery_transaction_id": transaction.transaction_id,
                "repair_transaction_id": plan["repair_transaction_id"],
                "accepted_feature_commit": accepted_commit,
                "attempt_chain": plan["attempt_chain"],
                "workflow_normalization": plan["workflow_normalization"],
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
                / "feature-repair-recovery.json"
            )
            atomic_write_json(report_path, report)
            report["report_path"] = str(report_path)
            return report
        except Exception:
            if metadata_originals is not None and self.inspector.head == plan["head"]:
                self._restore_metadata(metadata_originals)
            if kernel is not None and kernel.transaction is not None:
                try:
                    if kernel.lease.read() is not None:
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
