"""Repository-level execution engine with checkpoints and evidence validation."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Configuration
from .errors import ConveyorError, LockError, QueueError, RecoveryError, SessionError
from .locks import DurableLock, inspect_repository_writer_lock, make_lock_record
from .logging import EventLogger, JsonStateStore, atomic_write_json, run_event, utc_now
from .queue import FeatureQueue, resolve_queue_path
from .recovery import StartupReconciliation, assess_recovery, assess_startup_reconciliation
from .registry import Project
from .reporting import build_project_plan
from .repository import RepositoryInspector
from .retries import RetryBudget
from .sessions import SessionLauncher, SessionRequest, SessionResult
from .state_machine import CYCLE_MACHINE, PORTFOLIO_MACHINE

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CycleEngine:
    def __init__(self, configuration: Configuration, launcher: SessionLauncher | None = None):
        self.configuration = configuration
        self.root = configuration.root
        self.launcher = launcher or SessionLauncher(self.root, configuration.conveyor)
        self.project_store = JsonStateStore(self.root / "schemas/project-state.schema.json")
        self.cycle_store = JsonStateStore(self.root / "schemas/cycle-state.schema.json")
        self.events = EventLogger(
            configuration.owned_path(configuration.conveyor["log_directory"]) / "run-events.jsonl",
            self.root / "schemas/run-event.schema.json",
        )

    def project_state_path(self, project: Project) -> Path:
        return self.configuration.owned_path(self.configuration.conveyor["state_directory"]) / "projects" / f"{project.project_id}.json"

    def load_project_state(self, project: Project) -> dict[str, Any] | None:
        return self.project_store.read(self.project_state_path(project))

    def effective_project(self, project: Project) -> Project:
        state = self.load_project_state(project)
        return replace(project, current_state=state["current_state"]) if state else project

    def _project_document(self, project: Project, run_id: str | None, fingerprint: str) -> dict[str, Any]:
        existing = self.load_project_state(project)
        stamp = utc_now()
        if existing:
            if existing["repository_fingerprint"] != fingerprint:
                raise RecoveryError("portfolio project state belongs to a different repository fingerprint")
            existing["run_id"] = run_id
            existing["updated_at"] = stamp
            return existing
        return {
            "schema_version": 1,
            "project_id": project.project_id,
            "repository_fingerprint": fingerprint,
            "run_id": run_id,
            "current_state": project.current_state,
            "active_milestone": project.active_milestone,
            "current_feature": None,
            "last_checkpoint": None,
            "stop_reason": None,
            "human_decision_required": None,
            "state_evidence": None,
            "created_at": stamp,
            "updated_at": stamp,
        }

    def _transition_project(
        self,
        project: Project,
        document: dict[str, Any],
        target: str,
        *,
        run_id: str,
        checkpoint: str,
        feature: str | None = None,
        stop_reason: str | None = None,
        human_gate: dict[str, Any] | None = None,
        state_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transition = PORTFOLIO_MACHINE.transition(document["current_state"], target)
        document.update({
            "run_id": run_id,
            "current_state": target,
            "current_feature": feature,
            "last_checkpoint": checkpoint,
            "stop_reason": stop_reason,
            "human_decision_required": human_gate,
            "state_evidence": state_evidence,
            "updated_at": utc_now(),
        })
        self.project_store.write(self.project_state_path(project), document)
        self.events.append(run_event(
            run_id=run_id,
            project_id=project.project_id,
            repository_fingerprint=document["repository_fingerprint"],
            source="deterministic_script",
            milestone=project.active_milestone,
            feature=feature,
            previous_state=transition.previous,
            next_state=transition.current,
            result=checkpoint,
            stop_reason=stop_reason,
            human_gate=human_gate,
        ))
        return document

    @staticmethod
    def _reconciliation_fields(assessment: StartupReconciliation) -> dict[str, Any]:
        execution_path = list(assessment.transition_path)
        if (
            assessment.classification != "active_cycle_resume"
            and assessment.derived_state == "feature_ready"
            and (not execution_path or execution_path[-1] != "feature_running")
        ):
            execution_path.append("feature_running")
        return {
            "persisted_state": assessment.persisted_state,
            "derived_state": assessment.derived_state,
            "state_consistency": assessment.classification,
            "repair_transition_path": list(assessment.transition_path),
            "execution_state_path": execution_path,
            "would_persist_state_repair": assessment.would_persist,
            "state_reconciliation_reason": assessment.reason,
            "state_reconciliation_evidence": assessment.evidence,
            "state_reconciliation_human_decision": assessment.human_decision,
        }

    def project_plan(self, project: Project) -> dict[str, Any]:
        effective = self.effective_project(project)
        persisted = self.load_project_state(project)
        plan = build_project_plan(effective, self.configuration.conveyor, self.root)
        assessment = assess_startup_reconciliation(effective, persisted, plan)
        plan.update(self._reconciliation_fields(assessment))
        if assessment.classification in {"human_decision_required", "invalid_state_evidence"}:
            plan["proposed_next_action"] = "human_decision_required"
            plan["expected_stop_condition"] = assessment.reason
            plan["sessions_that_would_launch"] = []
        return plan

    def _persist_startup_reconciliation(
        self,
        project: Project,
        persisted: dict[str, Any],
        assessment: StartupReconciliation,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        if not assessment.would_persist:
            return persisted
        inspector = RepositoryInspector(project.repository)
        reservation = self._launch_lock(project, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            run_id=run_id,
            current_feature=assessment.evidence.get("selected_feature"),
            current_phase="startup_reconciliation",
        ))
        try:
            repository = inspector.inspect(
                baseline=project.validated_baseline_commit,
                milestone_branch=project.milestone_branch,
            )
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            queue_summary = queue.summary(project.active_milestone or "")
            selection = queue.select_next(project.active_milestone or "")
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            queue_path = resolve_queue_path(project.repository, project.queue_location)
            current_queue_fingerprint = hashlib.sha256(queue_path.read_bytes()).hexdigest()
            relative_queue = queue_path.relative_to(project.repository.resolve()).as_posix()
            committed_queue = inspector.file_at_commit("HEAD", relative_queue)
            stale_cycle = assessment.evidence.get("stale_cycle_evidence")
            recorded_cycle_fingerprint = (
                stale_cycle.get("cycle_fingerprint") if isinstance(stale_cycle, dict) else None
            )
            current_cycle_fingerprint = None
            if recorded_cycle_fingerprint:
                cycle_path = inspector.cycle_state_path()
                if cycle_path.exists():
                    current_cycle_fingerprint = hashlib.sha256(cycle_path.read_bytes()).hexdigest()
            if (
                repository.get("clean") is not True
                or any(repository.get("git_operations", {}).values())
                or repository.get("head") != assessment.evidence.get("milestone_head")
                or repository.get("branch") != project.milestone_branch
                or repository.get("head")
                != inspector.rev_parse(project.milestone_branch or "", check=False)
                or repository.get("baseline_exists") is not True
                or repository.get("milestone_branch_exists") is not True
                or repository.get("baseline_is_ancestor_of_milestone") is not True
                or repository.get("worktrees") != assessment.evidence.get("repository_worktrees")
                or repository.get("local_branches")
                != assessment.evidence.get("repository_local_branches")
                or repository.get("cycle_state_exists")
                != assessment.evidence.get("repository_cycle_state_exists")
                or current_queue_fingerprint != assessment.evidence.get("queue_fingerprint")
                or committed_queue is None
                or committed_queue != queue_path.read_text(encoding="utf-8")
                or (
                    recorded_cycle_fingerprint is not None
                    and current_cycle_fingerprint != recorded_cycle_fingerprint
                )
                or queue_summary.get("reconciliation_classification")
                != assessment.evidence.get("queue_classification")
                or (selection.feature_id if selection else None)
                != assessment.evidence.get("selected_feature")
                or writer.exists
            ):
                raise RecoveryError(
                    "repository, queue, selection, or writer-lock evidence changed during startup reconciliation"
                )
            current = self.load_project_state(project)
            if current is not None and (
                current.get("current_state") != persisted.get("current_state")
                or current.get("updated_at") != persisted.get("updated_at")
            ):
                raise RecoveryError("persisted project state changed concurrently during startup reconciliation")
            document = current or persisted
            evidence = {
                **assessment.evidence,
                "classification": assessment.classification,
                "persisted_state": assessment.persisted_state,
                "derived_state": assessment.derived_state,
                "repair_transition_path": list(assessment.transition_path),
                "reason": assessment.reason,
                "reconciled_at": utc_now(),
            }
            for index, target in enumerate(assessment.transition_path[1:], start=1):
                final = index == len(assessment.transition_path) - 1
                document = self._transition_project(
                    project,
                    document,
                    target,
                    run_id=run_id,
                    checkpoint=f"startup_state_reconciliation:{assessment.classification}:{target}",
                    feature=assessment.evidence.get("selected_feature") if target == "feature_ready" else None,
                    stop_reason=assessment.reason if final else None,
                    state_evidence=evidence,
                )
            return document
        finally:
            reservation.release(run_id)

    def _new_cycle_state(
        self, project: Project, run_id: str, inspector: RepositoryInspector, feature: dict[str, Any] | None
    ) -> dict[str, Any]:
        stamp = utc_now()
        identity = inspector.identity()
        milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False) if project.milestone_branch else None
        return {
            "schema_version": 1,
            "conveyor_run_id": run_id,
            "project_id": project.project_id,
            "repository_identity": identity,
            "repository_path_fingerprint": identity["path_fingerprint"],
            "active_milestone": project.active_milestone,
            "current_feature": feature.get("id") if feature else None,
            "feature_dependencies": list(feature.get("dependencies", [])) if feature else [],
            "feature_branch": feature.get("branch") if feature else None,
            "feature_worktree": None,
            "feature_starting_commit": feature.get("integration_base_commit") if feature else milestone_head,
            "accepted_feature_commit": None,
            "milestone_branch": project.milestone_branch,
            "milestone_pre_integration_commit": milestone_head,
            "milestone_post_integration_commit": None,
            "current_phase": "idle",
            "writer_lock_identity": None,
            "validation_attempts": [],
            "review_attempts": [],
            "integration_attempts": [],
            "last_successful_checkpoint": None,
            "last_verified_git_state": self._git_checkpoint(inspector),
            "stop_reason": None,
            "human_decision_required": None,
            "resume_instructions": f"scripts/conveyor resume --project {project.project_id}",
            "session_id": None,
            "created_at": stamp,
            "updated_at": stamp,
        }

    @staticmethod
    def _git_checkpoint(inspector: RepositoryInspector) -> dict[str, Any]:
        return {
            "branch": inspector.current_branch,
            "head": inspector.head,
            "clean": inspector.is_clean,
            "git_operations": inspector.git_operation_state(),
        }

    @staticmethod
    def _milestone_state_evidence(project: Project, inspector: RepositoryInspector, *, gate: bool = False) -> dict[str, Any]:
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        stamp = utc_now()
        evidence = {
            "milestone_head": inspector.rev_parse(project.milestone_branch or "", check=False),
            "queue_fingerprint": hashlib.sha256(queue_path.read_bytes()).hexdigest(),
            "recorded_at": stamp,
        }
        if gate:
            evidence.update({
                "gate_evidence_commit": evidence["milestone_head"],
                "gate_evidence_timestamp": stamp,
            })
        return evidence

    def _advance_cycle(
        self,
        path: Path,
        state: dict[str, Any],
        target: str,
        inspector: RepositoryInspector,
        checkpoint: str,
    ) -> dict[str, Any]:
        CYCLE_MACHINE.transition(state["current_phase"], target)
        state["current_phase"] = target
        state["last_successful_checkpoint"] = checkpoint
        state["last_verified_git_state"] = self._git_checkpoint(inspector)
        state["updated_at"] = utc_now()
        self.cycle_store.write(path, state)
        return state

    def _launch_lock(self, project: Project, inspector: RepositoryInspector) -> DurableLock:
        directory = self.configuration.owned_path(self.configuration.conveyor["lock_policy"]["controller_launch_lock_directory"])
        return DurableLock(directory / f"{inspector.identity()['path_fingerprint']}.json")

    def _launch_session(
        self,
        request: SessionRequest,
        inspector: RepositoryInspector,
        phase: str,
        *,
        reservation_held: bool = False,
    ) -> SessionResult:
        writer = inspect_repository_writer_lock(
            inspector.writer_lock_path(self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]),
            request.project.repository,
        )
        if writer.exists:
            record = writer.record or {}
            recorded_run = record.get("agent_run") or record.get("run_id")
            matching_resume = (
                request.mode in {"resume", "repair"}
                and recorded_run == request.run_id
                and writer.process_alive is False
                and not writer.ambiguous
            )
            if not matching_resume:
                raise LockError("repository writer lease exists; refusing duplicate production session")
        identity = inspector.identity()
        reservation = None if reservation_held else self._launch_lock(request.project, inspector)
        if reservation is not None:
            reservation.acquire(make_lock_record(
                project_id=request.project.project_id,
                repository_identity=identity["repository_id"],
                run_id=request.run_id,
                current_feature=request.feature,
                current_phase=phase,
            ))
        try:
            try:
                result = self.launcher.launch(request)
            except SessionError as exc:
                report_path = self._persist_launch_failure(request, phase, exc)
                raise SessionError(
                    f"project={request.project.project_id}; run_id={request.run_id}; action={request.action}; "
                    f"cwd={request.project.repository}; classification=session_execution_failed; "
                    f"error={exc}; report={report_path}; current_state={phase}; "
                    f"resume=scripts/conveyor resume --project {request.project.project_id}"
                ) from exc
        finally:
            if reservation is not None:
                reservation.release(request.run_id)
        report_path = self._persist_session_report(request, result, phase)
        result = replace(result, report_path=str(report_path))
        self.events.append(run_event(
            run_id=request.run_id,
            project_id=request.project.project_id,
            repository_fingerprint=identity["path_fingerprint"],
            source=request.action,
            milestone=request.project.active_milestone,
            feature=request.feature,
            command_category="codex_session",
            command=list(result.plan.argv),
            result=(
                f"exit={result.returncode}; exit_classification={result.exit_classification or 'exit_code_only'}; "
                f"classification={result.result_classification or 'exit_code_only'}; "
                f"structured_output={result.structured_output_validation}; "
                f"session_id={result.session_id or 'unavailable'}; report={report_path}"
            ),
        ))
        return result

    def _persist_launch_failure(self, request: SessionRequest, current_state: str, error: Exception) -> Path:
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        suffix = f"-repair-{request.repair_attempt}" if request.repair_attempt is not None else ""
        path = self._report_path(report_root, request.run_id, f"{request.action}{suffix}-launch-failure.json")
        try:
            plan = self.launcher.plan(request)
            argv = list(plan.argv)
            cwd = str(plan.cwd)
        except Exception:
            argv = []
            cwd = str(request.project.repository)
        message = str(error)
        classification = "process_launch_failure" if "process launch failed" in message else "session_execution_failed"
        atomic_write_json(path, {
            "schema_version": 1,
            "project_id": request.project.project_id,
            "run_id": request.run_id,
            "invoked_agent_or_skill": request.action,
            "action": request.action,
            "working_directory": cwd,
            "argv": argv,
            "exit_status": None,
            "exit_classification": classification,
            "result_classification": classification,
            "structured_output_validation": "not_available",
            "structured_result": None,
            "redacted_stdout": "",
            "redacted_stderr": str(error),
            "redacted_stderr_summary": str(error)[-2000:],
            "session_id": None,
            "current_state": current_state,
            "safe_resume_command": f"scripts/conveyor resume --project {request.project.project_id}",
            "created_at": utc_now(),
        })
        return path

    def _persist_session_report(
        self, request: SessionRequest, result: SessionResult, current_state: str
    ) -> Path:
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        suffix = f"-repair-{request.repair_attempt}" if request.repair_attempt is not None else ""
        path = self._report_path(report_root, request.run_id, f"{request.action}{suffix}.json")
        invoked = {
            "queue_reconciliation": "product-architect planning-only and feature-inventory",
            "feature_cycle": "feature-factory",
            "milestone_integration": "milestone-integrator",
            "milestone_gate": "milestone-gate",
            "human_decision_report": "human-decision-report",
        }.get(request.action, request.action)
        atomic_write_json(path, {
            "schema_version": 1,
            "project_id": request.project.project_id,
            "run_id": request.run_id,
            "invoked_agent_or_skill": invoked,
            "action": request.action,
            "working_directory": str(result.plan.cwd),
            "argv": list(result.plan.argv),
            "exit_status": result.returncode,
            "exit_classification": result.exit_classification or (
                "structured_result_successfully_returned" if result.returncode == 0 else "agent_or_skill_execution_failure"
            ),
            "result_classification": result.result_classification or (
                "structured_result_successfully_returned" if result.returncode == 0 else "agent_or_skill_execution_failure"
            ),
            "structured_output_validation": result.structured_output_validation,
            "structured_result": result.structured_result,
            "redacted_stdout": result.redacted_stdout or result.redacted_output,
            "redacted_stderr": result.redacted_stderr,
            "redacted_stderr_summary": (result.redacted_stderr or "")[-2000:],
            "session_id": result.session_id,
            "current_state": current_state,
            "safe_resume_command": f"scripts/conveyor resume --project {request.project.project_id}",
            "created_at": utc_now(),
        })
        return path

    @staticmethod
    def _report_path(report_root: Path, run_id: str, filename: str) -> Path:
        report_root = report_root.resolve()
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise SessionError(f"unsafe Conveyor run ID for report persistence: {run_id!r}")
        if Path(filename).name != filename:
            raise SessionError(f"unsafe report filename: {filename!r}")
        path = (report_root / run_id / filename).resolve(strict=False)
        try:
            path.relative_to(report_root)
        except ValueError as exc:
            raise SessionError("report path escapes the configured report directory") from exc
        return path

    @staticmethod
    def _session_requires_retry(result: SessionResult) -> bool:
        if result.action != "queue_reconciliation":
            return result.returncode != 0
        if result.result_classification in {"session_execution_failed", "structured_output_invalid"}:
            return result.structured_result is None or bool(result.structured_result.get("retryable"))
        return False

    @staticmethod
    def _session_failure_message(project: Project, run_id: str, result: SessionResult, current_state: str) -> str:
        stderr = (result.redacted_stderr or "").strip().replace("\n", " ")[-1000:] or "none captured"
        return (
            f"project={project.project_id}; run_id={run_id}; action={result.action}; cwd={result.plan.cwd}; "
            f"exit_status={result.returncode}; classification={result.result_classification}; "
            f"exit_classification={result.exit_classification}; "
            f"stderr={stderr}; structured_output={result.structured_output_validation}; "
            f"report={result.report_path}; current_state={current_state}; "
            f"resume=scripts/conveyor resume --project {project.project_id}"
        )

    def _launch_with_retries(
        self,
        request: SessionRequest,
        inspector: RepositoryInspector,
        phase: str,
        retry_key: str,
        *,
        reservation_held: bool = False,
    ) -> tuple[SessionResult, list[dict[str, Any]]]:
        configured = int(self.configuration.conveyor["retries"][retry_key])
        limit = request.project.maximum_retries if request.project.maximum_retries is not None else configured
        budget = RetryBudget(limit)
        attempts: list[dict[str, Any]] = []
        result = self._launch_session(request, inspector, phase, reservation_held=reservation_held)
        while self._session_requires_retry(result) and budget.remaining:
            evidence = hashlib.sha256(result.redacted_output[-4000:].encode()).hexdigest()
            attempt = budget.record(
                hypothesis=f"fresh repository-derived hypothesis for evidence {evidence}",
                evidence=f"session exit {result.returncode}; redacted output fingerprint {evidence}",
            )
            attempts.append({
                "attempt": attempt,
                "timestamp": utc_now(),
                "evidence_fingerprint": evidence,
                "previous_exit": result.returncode,
                "previous_classification": result.result_classification,
            })
            result = self._launch_session(replace(
                request,
                mode="repair",
                session_id=result.session_id,
                repair_attempt=attempt,
                repair_evidence=evidence,
            ), inspector, phase, reservation_held=reservation_held)
        return result, attempts

    def _resolve_accepted(self, inspector: RepositoryInspector, feature: dict[str, Any]) -> str:
        accepted = feature.get("accepted_commit")
        if accepted == "SELF":
            branch = feature.get("branch")
            if not isinstance(branch, str):
                raise QueueError("SELF accepted commit has no feature branch")
            accepted = inspector.rev_parse(branch)
        if not isinstance(accepted, str) or not inspector.ref_exists(accepted):
            raise QueueError("accepted feature commit cannot be resolved")
        return accepted

    def _verify_integrated_feature(
        self, project: Project, inspector: RepositoryInspector, state: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str]:
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        feature = queue.feature(str(state["current_feature"]))
        if feature is None or feature.get("status") != "integrated" or feature.get("integration_status") != "passed":
            raise QueueError("feature is not backed by integrated, passing queue evidence")
        accepted = self._resolve_accepted(inspector, feature)
        integrated = feature.get("integrated_commit")
        if not isinstance(integrated, str) or not inspector.ref_exists(integrated):
            raise QueueError("integrated commit evidence is missing")
        starting = feature.get("integration_base_commit") or state.get("feature_starting_commit")
        if not isinstance(starting, str) or not inspector.ref_exists(starting):
            raise QueueError("feature starting commit evidence is missing")
        if inspector.commit_count(starting, accepted) != 1:
            raise QueueError("feature does not have exactly one accepted commit relative to its recorded start")
        if str(feature["id"]).lower() not in inspector.commit_subject(accepted).lower():
            raise QueueError("accepted commit message does not identify the feature")
        if inspector.patch_fingerprint(accepted) != inspector.patch_fingerprint(integrated):
            raise QueueError("integrated commit is not patch-equivalent to the accepted feature commit")
        if not project.milestone_branch or not inspector.is_ancestor(integrated, project.milestone_branch):
            raise QueueError("integrated commit is not present on the configured milestone branch")
        if not inspector.is_clean:
            raise QueueError("repository is not clean after integration")
        return feature, accepted, integrated

    def _reconcile_feature_evidence(
        self,
        project: Project,
        inspector: RepositoryInspector,
        cycle_path: Path,
        state: dict[str, Any],
        result: SessionResult,
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        state["session_id"] = result.session_id
        if result.returncode != 0:
            state["stop_reason"] = "repository-scoped feature session returned non-zero"
            self._advance_cycle(cycle_path, state, "failed", inspector, "session_failed")
            raise SessionError(state["stop_reason"])

        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        feature = queue.feature(str(state["current_feature"]))
        if feature is None:
            raise QueueError("selected feature disappeared from the queue")

        if state["current_phase"] == "branch_preparing":
            state["feature_branch"] = feature.get("branch")
            state["feature_starting_commit"] = feature.get("integration_base_commit") or state["feature_starting_commit"]
            self._advance_cycle(cycle_path, state, "feature_in_progress", inspector, "feature_session_completed")
        if feature.get("status") in {"review", "accepted", "integration_pending", "integrated"}:
            self._advance_cycle(cycle_path, state, "feature_review", inspector, "review_evidence_observed")
        if feature.get("status") in {"accepted", "integration_pending", "integrated"}:
            self._advance_cycle(cycle_path, state, "feature_accepted", inspector, "acceptance_evidence_observed")
            self._advance_cycle(cycle_path, state, "integration_pending", inspector, "integration_pending_observed")

        if feature.get("status") in {"accepted", "integration_pending"}:
            integration_result, integration_repairs = self._launch_with_retries(SessionRequest(
                action="milestone_integration",
                project=project,
                run_id=state["conveyor_run_id"],
                mode="resume",
                feature=str(state["current_feature"]),
            ), inspector, "integrating", "integration_repairs", reservation_held=reservation_held)
            state["integration_attempts"].extend(integration_repairs)
            if integration_result.returncode != 0:
                self._advance_cycle(cycle_path, state, "integrating", inspector, "integration_session_started")
                self._advance_cycle(cycle_path, state, "failed", inspector, "integration_session_failed")
                raise SessionError("milestone integration session returned non-zero")
            result = integration_result
            feature = FeatureQueue.from_location(project.repository, project.queue_location).feature(str(state["current_feature"])) or feature

        if feature.get("status") == "integrated":
            if state["current_phase"] == "integration_pending":
                self._advance_cycle(cycle_path, state, "integrating", inspector, "integration_evidence_observed")
            self._advance_cycle(cycle_path, state, "integration_validation", inspector, "integration_validation_observed")
            verified, accepted, integrated = self._verify_integrated_feature(project, inspector, state)
            state["accepted_feature_commit"] = accepted
            state["milestone_post_integration_commit"] = inspector.rev_parse(project.milestone_branch or "", check=False)
            state["validation_attempts"].append({"timestamp": utc_now(), "outcome": "passed", "commit": integrated})
            state["review_attempts"].append({"timestamp": utc_now(), "outcome": "passed", "blocking_findings": 0})
            state["integration_attempts"].append({"timestamp": utc_now(), "outcome": "passed", "integrated_commit": integrated})
            self._advance_cycle(cycle_path, state, "feature_integrated", inspector, "feature_integrated_and_validated")
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]),
                project.repository,
            )
            if writer.exists:
                raise LockError("repository writer lease remains after verified integration")
            return {"feature": verified["id"], "accepted_commit": accepted, "integrated_commit": integrated}
        raise QueueError("session completion was not corroborated by accepted or integrated queue evidence")

    def _execute_feature(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        if not inspector.is_clean:
            raise ConveyorError("feature execution requires a clean repository")
        reservation = None if reservation_held else self._launch_lock(project, inspector)
        if reservation is not None:
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id=run_id,
                current_feature=None,
                current_phase="feature_preflight",
            ))
        try:
            repository = inspector.inspect(
                baseline=project.validated_baseline_commit,
                milestone_branch=project.milestone_branch,
            )
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            if (
                repository.get("clean") is not True
                or any(repository.get("git_operations", {}).values())
                or repository.get("branch") != project.milestone_branch
                or repository.get("head") != inspector.rev_parse(project.milestone_branch or "", check=False)
                or writer.exists
            ):
                raise RecoveryError(
                    "feature preflight requires the clean configured milestone HEAD and no writer lease"
                )
            inspector.ensure_runtime_ignored()
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            selection = queue.select_next(project.active_milestone or "")
            if selection is None:
                raise QueueError("no dependency-ready feature exists after queue reconciliation")
            cycle_path = inspector.cycle_state_path()
            state = self._new_cycle_state(project, run_id, inspector, selection.feature)
            self.cycle_store.write(cycle_path, state)
            self._advance_cycle(cycle_path, state, "preflight", inspector, "preflight_verified")
            self._advance_cycle(cycle_path, state, "feature_selected", inspector, selection.reason)
            self._advance_cycle(
                cycle_path,
                state,
                "branch_preparing",
                inspector,
                "repository_session_owns_branch_preparation",
            )
            self._transition_project(
                project,
                project_state,
                "feature_running",
                run_id=run_id,
                checkpoint="feature_session_launch",
                feature=selection.feature_id,
            )
            result, repairs = self._launch_with_retries(SessionRequest(
                action="feature_cycle", project=project, run_id=run_id, mode=mode, feature=selection.feature_id
            ), inspector, "feature_in_progress", "implementation_repairs", reservation_held=True)
            state["validation_attempts"].extend(repairs)
            self.cycle_store.write(cycle_path, state)
            evidence = self._reconcile_feature_evidence(
                project,
                inspector,
                cycle_path,
                state,
                result,
                reservation_held=True,
            )
            self._transition_project(
                project,
                project_state,
                "feature_accepted",
                run_id=run_id,
                checkpoint="feature_integrated",
                feature=selection.feature_id,
            )
            return evidence
        finally:
            if reservation is not None:
                reservation.release(run_id)

    def _execute_queue_reconciliation(
        self, project: Project, mode: str, run_id: str, project_state: dict[str, Any]
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        reservation = self._launch_lock(project, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            run_id=run_id,
            current_feature=None,
            current_phase="queue_reconciliation",
        ))
        try:
            return self._execute_queue_reconciliation_locked(
                project, mode, run_id, project_state, inspector
            )
        finally:
            reservation.release(run_id)

    def _execute_queue_reconciliation_locked(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        inspector: RepositoryInspector,
    ) -> dict[str, Any]:
        if not inspector.is_clean:
            raise ConveyorError("queue reconciliation cannot mutate a dirty repository")
        result, repairs = self._launch_with_retries(SessionRequest(
            action="queue_reconciliation", project=project, run_id=run_id, mode=mode
        ), inspector, "queue_reconciliation", "queue_reconciliation_repairs", reservation_held=True)
        classification = result.result_classification
        if classification in {"session_execution_failed", "structured_output_invalid", None}:
            message = self._session_failure_message(project, run_id, result, project_state["current_state"])
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="queue_reconciliation_failed", stop_reason=message,
            )
            raise SessionError(message)

        if classification == "invalid_queue":
            message = self._session_failure_message(project, run_id, result, project_state["current_state"])
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="invalid_queue", stop_reason=message,
            )
            return {"outcome": "invalid_queue", "next_state": "validation_failed", "report": result.report_path}

        if classification == "human_decision_required":
            human = result.structured_result.get("human_decision") if result.structured_result else None
            self._transition_project(
                project, project_state, "human_decision_required", run_id=run_id,
                checkpoint="queue_reconciliation_human_gate", stop_reason="queue reconciliation requires a human decision",
                human_gate=human,
            )
            return {"outcome": classification, "next_state": "human_decision_required", "human_decision": human}

        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        summary = queue.summary(project.active_milestone or "")
        derived = summary["reconciliation_classification"]
        if classification != derived:
            message = (
                f"structured reconciliation result {classification!r} contradicts deterministic queue "
                f"classification {derived!r}; report={result.report_path}"
            )
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="structured_result_contradicted", stop_reason=message,
            )
            raise SessionError(message)

        validation = result.structured_result.get("queue_validation", {}) if result.structured_result else {}
        if (
            validation.get("valid") is not True
            or validation.get("milestone_found") != summary["milestone_found"]
            or validation.get("feature_count") != summary["feature_count"]
        ):
            message = f"structured queue-validation evidence disagrees with deterministic parsing; report={result.report_path}"
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="structured_queue_validation_contradicted", stop_reason=message,
            )
            raise SessionError(message)

        targets = {
            "reconciled_ready_work": "feature_ready",
            "reconciled_no_ready_work": "paused",
            "milestone_complete": "milestone_gate",
            "legitimately_blocked": "paused",
        }
        target = targets[classification]
        stop_reasons = {
            "reconciled_no_ready_work": "queue validated successfully; no dependency-ready feature exists",
            "legitimately_blocked": "queue validated successfully; remaining work is legitimately blocked",
        }
        self._transition_project(
            project, project_state, target, run_id=run_id, checkpoint=classification,
            stop_reason=stop_reasons.get(classification),
        )
        return {
            "outcome": classification,
            "next_state": target,
            "queue_status": summary,
            "report": result.report_path,
            "repair_attempts": repairs,
        }

    def _execute_milestone_gate(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        reservation = None if reservation_held else self._launch_lock(project, inspector)
        if reservation is not None:
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id=run_id,
                current_feature=project_state.get("current_feature"),
                current_phase="milestone_gate_preflight",
            ))
        try:
            repository = inspector.inspect(
                baseline=project.validated_baseline_commit,
                milestone_branch=project.milestone_branch,
            )
            if (
                repository.get("clean") is not True
                or any(repository.get("git_operations", {}).values())
                or repository.get("branch") != project.milestone_branch
                or repository.get("head") != repository.get("milestone_branch_head")
            ):
                raise RecoveryError("milestone gate requires the clean configured milestone HEAD")
            inspector.ensure_runtime_ignored()
            cycle_path = inspector.cycle_state_path()
            state = self.cycle_store.read(cycle_path)
            if state is None or state.get("current_phase") == "completed":
                state = self._new_cycle_state(project, run_id, inspector, None)
                self.cycle_store.write(cycle_path, state)
                self._advance_cycle(cycle_path, state, "preflight", inspector, "milestone_preflight")
                self._advance_cycle(cycle_path, state, "milestone_gate", inspector, "milestone_gate_launch")
            elif state["current_phase"] == "feature_integrated":
                self._advance_cycle(cycle_path, state, "milestone_gate", inspector, "milestone_gate_launch")
            elif state["current_phase"] != "milestone_gate":
                raise ConveyorError(f"cycle phase {state['current_phase']} cannot enter milestone gate")
            self._transition_project(
                project,
                project_state,
                "milestone_gate",
                run_id=run_id,
                checkpoint="milestone_gate_launch",
            )
            result = self._launch_session(SessionRequest(
                action="milestone_gate", project=project, run_id=run_id, mode=mode
            ), inspector, "milestone_gate", reservation_held=True)
            return self._finalize_milestone_gate_result(
                project, run_id, project_state, inspector, state, result
            )
        finally:
            if reservation is not None:
                reservation.release(run_id)

    def _finalize_milestone_gate_result(
        self,
        project: Project,
        run_id: str,
        project_state: dict[str, Any],
        inspector: RepositoryInspector,
        state: dict[str, Any],
        result: SessionResult,
    ) -> dict[str, Any]:
        cycle_path = inspector.cycle_state_path()
        state["session_id"] = result.session_id
        if result.returncode != 0:
            self._advance_cycle(cycle_path, state, "failed", inspector, "milestone_gate_session_failed")
            raise SessionError("milestone gate session returned non-zero")
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        milestone = queue.milestone(project.active_milestone or "")
        if (
            milestone is None
            or milestone.get("status") != "gate_passed"
            or not queue.milestone_complete(project.active_milestone or "")
        ):
            raise QueueError("milestone gate response lacks corroborating gate_passed queue evidence")
        if not inspector.is_clean:
            raise ConveyorError("milestone branch is not clean after gate")
        self._advance_cycle(cycle_path, state, "completed", inspector, "milestone_gate_passed")
        gate = {
            "decision": "Approve or decline merge of the validated milestone branch into the default branch.",
            "milestone_branch": project.milestone_branch,
            "default_branch_merge_performed": False,
        }
        self._transition_project(
            project,
            project_state,
            "milestone_ready_for_merge",
            run_id=run_id,
            checkpoint="milestone_gate_passed",
            stop_reason="human milestone merge approval required",
            human_gate=gate,
            state_evidence=self._milestone_state_evidence(project, inspector, gate=True),
        )
        return {"outcome": "milestone_ready_for_merge", "human_gate": gate}

    def _finish_resumed_feature(
        self,
        project: Project,
        run_id: str,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        evidence: dict[str, Any],
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        cycle_path = inspector.cycle_state_path()
        project_state = self._project_document(project, run_id, inspector.identity()["path_fingerprint"])
        if project_state["current_state"] in {"feature_running", "feature_review"}:
            self._transition_project(
                project, project_state, "feature_accepted", run_id=run_id,
                checkpoint="resumed_feature_integrated", feature=str(state.get("current_feature")),
            )
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        if queue.milestone_complete(project.active_milestone or ""):
            if state["current_phase"] in {"feature_integrated", "next_feature_selection"}:
                self._advance_cycle(cycle_path, state, "milestone_gate", inspector, "resumed_milestone_complete")
            return {
                "project_id": project.project_id,
                **self._execute_milestone_gate(
                    project,
                    "resume",
                    run_id,
                    project_state,
                    reservation_held=reservation_held,
                ),
            }
        if state["current_phase"] == "feature_integrated":
            self._advance_cycle(cycle_path, state, "next_feature_selection", inspector, "resumed_next_feature_selection")
        if state["current_phase"] == "next_feature_selection":
            self._advance_cycle(cycle_path, state, "completed", inspector, "resumed_feature_cycle_complete")
        if project_state["current_state"] == "feature_accepted":
            self._transition_project(project, project_state, "feature_ready", run_id=run_id, checkpoint="resumed_next_feature_ready")
        if project.automation_mode == "one_feature":
            return {"project_id": project.project_id, "outcome": "feature_integrated", **evidence}
        if reservation_held:
            return {
                "project_id": project.project_id,
                "outcome": "feature_integrated",
                "_continue_mode": project.automation_mode,
                **evidence,
            }
        return self.run_project(project, project.automation_mode)

    def resume_project(self, project: Project, run_id: str | None = None) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        cycle_path = inspector.cycle_state_path()
        state = self.cycle_store.read(cycle_path)
        cycle_fingerprint = hashlib.sha256(cycle_path.read_bytes()).hexdigest() if cycle_path.exists() else None
        assessment = assess_recovery(project, state)
        if assessment.outcome != "resume" or state is None:
            return {"project_id": project.project_id, "outcome": assessment.outcome, "human_decision": assessment.human_decision}
        run_id = run_id or str(state["conveyor_run_id"])
        reservation = self._launch_lock(project, inspector)
        reservation_status = reservation.status(run_id)
        if reservation_status.exists:
            if not reservation_status.owned_by_run:
                return {"project_id": project.project_id, "outcome": "human_decision_required", "reason": "launch reservation belongs to another run"}
            if reservation_status.process_alive is True or reservation_status.ambiguous:
                return {"project_id": project.project_id, "outcome": "writer_locked", "reason": "launch reservation owner may still be active"}
            reservation.recover_stale(expected_repository_identity=inspector.identity()["repository_id"])
        phase = str(assessment.resume_phase)
        if phase == "human_decision_required":
            return {"project_id": project.project_id, "outcome": "human_decision_required", "human_decision": state.get("human_decision_required")}
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            run_id=run_id,
            current_feature=state.get("current_feature"),
            current_phase=f"resume:{phase}",
        ))
        outcome: dict[str, Any]
        try:
            current_cycle_fingerprint = (
                hashlib.sha256(cycle_path.read_bytes()).hexdigest() if cycle_path.exists() else None
            )
            current_assessment = assess_recovery(project, state)
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            if (
                current_cycle_fingerprint != cycle_fingerprint
                or current_assessment.outcome != "resume"
                or current_assessment.resume_phase != assessment.resume_phase
                or writer.exists
            ):
                raise RecoveryError("cycle, Git, or writer-lock evidence changed during resume acquisition")
            if phase in {"feature_integrated", "next_feature_selection"}:
                feature, accepted, integrated = self._verify_integrated_feature(project, inspector, state)
                evidence = {"feature": feature["id"], "accepted_commit": accepted, "integrated_commit": integrated}
                outcome = self._finish_resumed_feature(
                    project,
                    run_id,
                    inspector,
                    state,
                    evidence,
                    reservation_held=True,
                )
            else:
                if phase == "failed":
                    checkpoint = str(state.get("last_successful_checkpoint") or "")
                    if "milestone_gate" in checkpoint:
                        phase = "milestone_gate"
                    elif "integration" in checkpoint:
                        phase = "integration_pending"
                    else:
                        phase = "feature_in_progress"
                    self._advance_cycle(
                        inspector.cycle_state_path(), state, phase, inspector, "focused_resume_after_failure"
                    )
                if phase in {"integration_pending", "integrating", "integration_validation"}:
                    action = "milestone_integration"
                elif phase == "milestone_gate":
                    action = "milestone_gate"
                else:
                    action = "feature_cycle"
                result = self._launch_session(SessionRequest(
                    action=action,
                    project=project,
                    run_id=run_id,
                    mode="resume",
                    feature=state.get("current_feature"),
                    session_id=state.get("session_id"),
                ), inspector, phase, reservation_held=True)
                if action in {"feature_cycle", "milestone_integration"}:
                    evidence = self._reconcile_feature_evidence(
                        project,
                        inspector,
                        inspector.cycle_state_path(),
                        state,
                        result,
                        reservation_held=True,
                    )
                    outcome = self._finish_resumed_feature(
                        project,
                        run_id,
                        inspector,
                        state,
                        evidence,
                        reservation_held=True,
                    )
                else:
                    project_state = self._project_document(
                        project, run_id, inspector.identity()["path_fingerprint"]
                    )
                    outcome = {
                        "project_id": project.project_id,
                        **self._finalize_milestone_gate_result(
                            project, run_id, project_state, inspector, state, result
                        ),
                    }
        finally:
            reservation.release(run_id)
        continue_mode = outcome.pop("_continue_mode", None)
        if continue_mode:
            return self.run_project(project, str(continue_mode))
        return outcome

    def reconcile_controller_state(self, project: Project, *, dry_run: bool) -> dict[str, Any]:
        """Reconcile only Conveyor-owned state from read-only queue and Git evidence."""

        effective = self.effective_project(project)
        persisted = self.load_project_state(project)
        plan = self.project_plan(project)
        reconciliation_document = persisted or self._project_document(
            effective, None, plan["repository_path_fingerprint"]
        )
        assessment = assess_startup_reconciliation(effective, reconciliation_document, plan)
        queue_status = plan["queue_status"]
        classification = queue_status.get("reconciliation_classification", "invalid_queue")
        request = SessionRequest(
            action="queue_reconciliation",
            project=project,
            run_id="dry-run-validation",
            mode="dry-run-validation",
        )
        session_plan = self.launcher.plan(request)
        targets = {
            "reconciled_ready_work": "feature_ready",
            "reconciled_no_ready_work": "paused",
            "milestone_complete": "milestone_gate",
            "legitimately_blocked": "paused",
            "human_decision_required": "human_decision_required",
            "invalid_queue": "validation_failed",
        }
        result = {
            "project_id": project.project_id,
            "dry_run": dry_run,
            "classification": classification,
            "current_state": effective.current_state,
            "proposed_state": targets[classification],
            "resolved_queue_path": queue_status.get("resolved_queue_path"),
            "queue_status": queue_status,
            "session_validation_plan": {
                "action": request.action,
                "working_directory": str(session_plan.cwd),
                "argv": list(session_plan.argv),
                "sandbox": session_plan.sandbox,
                "prompt_sha256": session_plan.prompt_sha256,
                "launch_performed": False,
            },
            "application_repository_written": False,
            **self._reconciliation_fields(assessment),
        }
        if dry_run:
            return result
        if classification == "invalid_queue" or assessment.classification == "invalid_state_evidence":
            raise QueueError(str(queue_status.get("error") or "queue validation failed"))
        if assessment.classification == "human_decision_required":
            raise RecoveryError(assessment.reason)
        if assessment.classification == "active_cycle_resume":
            raise RecoveryError("controller-state reconciliation requires resuming the corroborated active cycle")
        locks = plan["lock_status"]
        if plan.get("existing_active_cycle"):
            raise RecoveryError("controller-state reconciliation refuses an existing application cycle")
        if locks["repository_writer"]["exists"] or locks["controller_launch"]["exists"]:
            raise LockError("controller-state reconciliation refuses while a writer or launch lock exists")
        inspector = RepositoryInspector(project.repository)
        repository_state = plan["repository_state"]
        if not repository_state["clean"] or any(repository_state["git_operations"].values()):
            raise RecoveryError("controller-state reconciliation requires a clean repository with no Git operation")
        if not project.milestone_branch or repository_state["branch"] != project.milestone_branch:
            raise RecoveryError("controller-state reconciliation requires the configured milestone branch checkout")
        milestone_head = inspector.rev_parse(project.milestone_branch)
        if milestone_head != repository_state["head"]:
            raise RecoveryError("controller-state reconciliation requires HEAD to match the milestone branch")
        if (
            repository_state.get("baseline_exists") is not True
            or repository_state.get("milestone_branch_exists") is not True
            or repository_state.get("baseline_is_ancestor_of_milestone") is not True
        ):
            raise RecoveryError("controller-state reconciliation requires verified baseline and milestone ancestry")
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        relative_queue = queue_path.relative_to(project.repository.resolve()).as_posix()
        committed_queue = inspector.file_at_commit("HEAD", relative_queue)
        if committed_queue is None or committed_queue != queue_path.read_text(encoding="utf-8"):
            raise RecoveryError("controller-state reconciliation requires queue evidence committed at HEAD")
        target = targets[classification]
        if assessment.would_persist:
            run_id = f"recovery-{uuid.uuid4()}"
            document = self._persist_startup_reconciliation(
                effective, reconciliation_document, assessment, run_id=run_id
            )
            result.update({"run_id": run_id, "current_state": document["current_state"], "state_recovered": True})
        else:
            result.update({"run_id": None, "current_state": target, "state_recovered": False})
        return result

    def run_project(self, project: Project, mode: str, *, dry_run: bool = False) -> dict[str, Any]:
        if mode == "resume":
            plan = self.project_plan(project)
            if dry_run:
                return plan
            effective = self.effective_project(project)
            persisted = self.load_project_state(project)
            assessment = assess_startup_reconciliation(effective, persisted, plan)
            if assessment.classification in {"human_decision_required", "invalid_state_evidence"}:
                return {"project_id": project.project_id, "outcome": "human_decision_required", "plan": plan}
            if assessment.would_persist:
                if persisted is None:
                    persisted = self._project_document(
                        effective, None, plan["repository_path_fingerprint"]
                    )
                run_id = f"recovery-{uuid.uuid4()}"
                document = self._persist_startup_reconciliation(
                    effective, persisted, assessment, run_id=run_id
                )
                return {
                    "project_id": project.project_id,
                    "outcome": "state_repaired",
                    "current_state": document["current_state"],
                    "selected_feature": assessment.evidence.get("selected_feature"),
                    "next_action": f"scripts/conveyor run --project {project.project_id} --mode {project.automation_mode}",
                }
            if plan.get("stale_cycle_evidence") and not plan.get("existing_active_cycle"):
                return {
                    "project_id": project.project_id,
                    "outcome": "no_active_cycle",
                    "current_state": assessment.derived_state,
                    "selected_feature": assessment.evidence.get("selected_feature"),
                    "next_action": f"scripts/conveyor run --project {project.project_id} --mode {project.automation_mode}",
                }
            return self.resume_project(self.effective_project(project))
        effective = self.effective_project(project)
        plan = self.project_plan(project)
        if dry_run or mode == "audit":
            return plan
        if plan["proposed_next_action"] in {"disabled", "repository_dirty", "writer_locked", "human_decision_required", "conveyor_error"}:
            return {"project_id": project.project_id, "outcome": plan["proposed_next_action"], "plan": plan}
        run_id = str(uuid.uuid4())
        inspector = RepositoryInspector(project.repository)
        persisted = self.load_project_state(project)
        assessment = assess_startup_reconciliation(effective, persisted, plan)
        if assessment.would_persist:
            persisted = persisted or self._project_document(
                effective, None, inspector.identity()["path_fingerprint"]
            )
            project_state = self._persist_startup_reconciliation(
                effective, persisted, assessment, run_id=run_id
            )
            effective = replace(effective, current_state=project_state["current_state"])
        else:
            project_state = self._project_document(effective, run_id, inspector.identity()["path_fingerprint"])
        completed_features = 0
        while True:
            effective = replace(effective, current_state=project_state["current_state"])
            plan = build_project_plan(effective, self.configuration.conveyor, self.root)
            action = plan["proposed_next_action"]
            if action == "resume":
                return self.resume_project(effective)
            if action == "queue_reconciliation":
                self._execute_queue_reconciliation(effective, mode, run_id, project_state)
                if mode == "one_feature":
                    continue
            elif action == "feature_cycle":
                cycle_reservation = self._launch_lock(effective, inspector)
                cycle_reservation.acquire(make_lock_record(
                    project_id=project.project_id,
                    repository_identity=inspector.identity()["repository_id"],
                    run_id=run_id,
                    current_feature=plan.get("selected_feature"),
                    current_phase="feature_execution",
                ))
                try:
                    evidence = self._execute_feature(
                        effective,
                        mode,
                        run_id,
                        project_state,
                        reservation_held=True,
                    )
                    completed_features += 1
                    queue = FeatureQueue.from_location(project.repository, project.queue_location)
                    inspector = RepositoryInspector(project.repository)
                    cycle_path = inspector.cycle_state_path()
                    cycle_state = self.cycle_store.read(cycle_path)
                    if cycle_state is None:
                        raise ConveyorError("feature integration completed without durable cycle state")
                    if queue.milestone_complete(project.active_milestone or ""):
                        if mode == "one_feature":
                            self._advance_cycle(cycle_path, cycle_state, "completed", inspector, "one_feature_stop")
                            self._transition_project(
                                project,
                                project_state,
                                "milestone_gate",
                                run_id=run_id,
                                checkpoint="milestone_complete",
                                feature=evidence["feature"],
                            )
                            return {"project_id": project.project_id, "outcome": "one_feature_integrated", **evidence}
                        return {
                            "project_id": project.project_id,
                            **self._execute_milestone_gate(
                                effective,
                                mode,
                                run_id,
                                project_state,
                                reservation_held=True,
                            ),
                        }
                    self._advance_cycle(
                        cycle_path, cycle_state, "next_feature_selection", inspector, "next_feature_recalculated"
                    )
                    self._advance_cycle(cycle_path, cycle_state, "completed", inspector, "feature_cycle_complete")
                    self._transition_project(
                        project,
                        project_state,
                        "feature_ready",
                        run_id=run_id,
                        checkpoint="next_feature_recalculated",
                        feature=None,
                    )
                    if mode == "one_feature":
                        return {"project_id": project.project_id, "outcome": "one_feature_integrated", **evidence}
                finally:
                    cycle_reservation.release(run_id)
            elif action == "milestone_gate":
                return {"project_id": project.project_id, **self._execute_milestone_gate(effective, mode, run_id, project_state)}
            else:
                return {"project_id": project.project_id, "outcome": action, "completed_features": completed_features, "plan": plan}
            if completed_features >= 100:
                raise ConveyorError("safety stop: feature-cycle bound reached")
