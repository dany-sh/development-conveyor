"""Bounded model-backed repair for an authenticated retained feature result."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from .command_authority import CommandAuthority
from .contracts import SessionResultEnvelope, TransactionState, WorkflowType, fingerprint
from .errors import RecoveryError, SessionError, TransactionError
from .feature_result_recovery import FeatureResultRecovery, _changed_paths
from .kernel import FeatureExecutionAdapter, WorkflowKernel
from .locks import DurableLock, make_lock_record
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

    def _attempt_report(
        self,
        *,
        run_id: str,
        feature_id: str,
        attempt: int,
        result: SessionResult,
        pre_fingerprint: str,
        post_fingerprint: str,
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
                "session_id": result.session_id,
                "returncode": result.returncode,
                "result_classification": result.result_classification,
                "structured_output_validation": result.structured_output_validation,
                "structured_output_errors": list(result.structured_output_errors),
                "pre_repair_fingerprint": pre_fingerprint,
                "post_repair_fingerprint": post_fingerprint,
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
                report_path = self._attempt_report(
                    run_id=run_id,
                    feature_id=str(plan["feature_id"]),
                    attempt=attempt,
                    result=result,
                    pre_fingerprint=pre_fingerprint,
                    post_fingerprint=post_fingerprint,
                )
                unexpected = sorted(
                    set(_changed_paths(self.inspector)) - set(plan["allowed_paths"])
                )
                unexpected_untracked = sorted(
                    set(self.inspector.untracked_file_hashes())
                    - set(plan["untracked_paths"])
                )
                if unexpected or unexpected_untracked:
                    raise RecoveryError(
                        "repair session changed unexpected paths: "
                        + ", ".join(unexpected or unexpected_untracked)
                    )
                attempt_evidence: dict[str, Any] = {
                    "attempt": attempt,
                    "session_id": result.session_id,
                    "report_path": str(report_path),
                    "pre_repair_fingerprint": pre_fingerprint,
                    "post_repair_fingerprint": post_fingerprint,
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
                    outcomes = [
                        {
                            "argv": item.get("argv"),
                            "exit_code": item.get("exit_code"),
                            "output_sha256": item.get("output_sha256"),
                        }
                        for item in attempt_evidence["commands"]
                    ]
                    signature = fingerprint(
                        {
                            "diff_fingerprint": post_fingerprint,
                            "failure_fingerprint": failure_fingerprint,
                            "command_outcomes": outcomes,
                            "environment_failure": environment_failure,
                        }
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
                    "implementation_preserved": True,
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
