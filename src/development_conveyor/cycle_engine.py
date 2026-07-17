"""Repository-level execution engine with checkpoints and evidence validation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Configuration
from .errors import ConveyorError, LockError, QueueError, RecoveryError, SessionError
from .locks import DurableLock, inspect_repository_writer_lock, make_lock_record
from .logging import EventLogger, JsonStateStore, run_event, utc_now
from .queue import FeatureQueue
from .recovery import assess_recovery
from .registry import Project
from .reporting import build_project_plan
from .repository import RepositoryInspector
from .retries import RetryBudget
from .sessions import SessionLauncher, SessionRequest, SessionResult
from .state_machine import CYCLE_MACHINE, PORTFOLIO_MACHINE


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
    ) -> dict[str, Any]:
        transition = PORTFOLIO_MACHINE.transition(document["current_state"], target)
        document.update({
            "run_id": run_id,
            "current_state": target,
            "current_feature": feature,
            "last_checkpoint": checkpoint,
            "stop_reason": stop_reason,
            "human_decision_required": human_gate,
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
        reservation = self._launch_lock(request.project, inspector)
        reservation.acquire(make_lock_record(
            project_id=request.project.project_id,
            repository_identity=identity["repository_id"],
            run_id=request.run_id,
            current_feature=request.feature,
            current_phase=phase,
        ))
        try:
            result = self.launcher.launch(request)
        finally:
            reservation.release(request.run_id)
        self.events.append(run_event(
            run_id=request.run_id,
            project_id=request.project.project_id,
            repository_fingerprint=identity["path_fingerprint"],
            source=request.action,
            milestone=request.project.active_milestone,
            feature=request.feature,
            command_category="codex_session",
            command=list(result.plan.argv),
            result=f"exit={result.returncode}; session_id={result.session_id or 'unavailable'}",
        ))
        return result

    def _launch_with_retries(
        self,
        request: SessionRequest,
        inspector: RepositoryInspector,
        phase: str,
        retry_key: str,
    ) -> tuple[SessionResult, list[dict[str, Any]]]:
        configured = int(self.configuration.conveyor["retries"][retry_key])
        limit = request.project.maximum_retries if request.project.maximum_retries is not None else configured
        budget = RetryBudget(limit)
        attempts: list[dict[str, Any]] = []
        result = self._launch_session(request, inspector, phase)
        while result.returncode != 0 and budget.remaining:
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
            })
            result = self._launch_session(replace(
                request,
                mode="repair",
                session_id=result.session_id,
                repair_attempt=attempt,
                repair_evidence=evidence,
            ), inspector, phase)
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
        queue = FeatureQueue.from_path(project.repository / project.queue_location)
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
    ) -> dict[str, Any]:
        state["session_id"] = result.session_id
        if result.returncode != 0:
            state["stop_reason"] = "repository-scoped feature session returned non-zero"
            self._advance_cycle(cycle_path, state, "failed", inspector, "session_failed")
            raise SessionError(state["stop_reason"])

        queue = FeatureQueue.from_path(project.repository / project.queue_location)
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
            ), inspector, "integrating", "integration_repairs")
            state["integration_attempts"].extend(integration_repairs)
            if integration_result.returncode != 0:
                self._advance_cycle(cycle_path, state, "integrating", inspector, "integration_session_started")
                self._advance_cycle(cycle_path, state, "failed", inspector, "integration_session_failed")
                raise SessionError("milestone integration session returned non-zero")
            result = integration_result
            feature = FeatureQueue.from_path(project.repository / project.queue_location).feature(str(state["current_feature"])) or feature

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

    def _execute_feature(self, project: Project, mode: str, run_id: str, project_state: dict[str, Any]) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        if not inspector.is_clean:
            raise ConveyorError("feature execution requires a clean repository")
        inspector.ensure_runtime_ignored()
        queue = FeatureQueue.from_path(project.repository / project.queue_location)
        selection = queue.select_next(project.active_milestone or "")
        if selection is None:
            raise QueueError("no dependency-ready feature exists after queue reconciliation")
        cycle_path = inspector.cycle_state_path()
        state = self._new_cycle_state(project, run_id, inspector, selection.feature)
        self.cycle_store.write(cycle_path, state)
        self._advance_cycle(cycle_path, state, "preflight", inspector, "preflight_verified")
        self._advance_cycle(cycle_path, state, "feature_selected", inspector, selection.reason)
        self._advance_cycle(cycle_path, state, "branch_preparing", inspector, "repository_session_owns_branch_preparation")
        self._transition_project(project, project_state, "feature_running", run_id=run_id, checkpoint="feature_session_launch", feature=selection.feature_id)
        result, repairs = self._launch_with_retries(SessionRequest(
            action="feature_cycle", project=project, run_id=run_id, mode=mode, feature=selection.feature_id
        ), inspector, "feature_in_progress", "implementation_repairs")
        state["validation_attempts"].extend(repairs)
        self.cycle_store.write(cycle_path, state)
        evidence = self._reconcile_feature_evidence(project, inspector, cycle_path, state, result)
        self._transition_project(project, project_state, "feature_accepted", run_id=run_id, checkpoint="feature_integrated", feature=selection.feature_id)
        return evidence

    def _execute_queue_reconciliation(
        self, project: Project, mode: str, run_id: str, project_state: dict[str, Any]
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        if not inspector.is_clean:
            raise ConveyorError("queue reconciliation cannot mutate a dirty repository")
        result, repairs = self._launch_with_retries(SessionRequest(
            action="queue_reconciliation", project=project, run_id=run_id, mode=mode
        ), inspector, "queue_reconciliation", "queue_reconciliation_repairs")
        if result.returncode != 0:
            self._transition_project(project, project_state, "validation_failed", run_id=run_id, checkpoint="queue_reconciliation_failed", stop_reason="queue reconciliation session failed")
            raise SessionError("queue reconciliation session returned non-zero")
        queue = FeatureQueue.from_path(project.repository / project.queue_location)
        if queue.milestone(project.active_milestone or "") is None:
            raise QueueError("queue reconciliation did not establish the configured active milestone")
        if queue.select_next(project.active_milestone or "") is None and not queue.milestone_complete(project.active_milestone or ""):
            raise QueueError("queue remains without justified ready work or completed milestone evidence")
        target = "milestone_gate" if queue.milestone_complete(project.active_milestone or "") else "feature_ready"
        self._transition_project(project, project_state, target, run_id=run_id, checkpoint="queue_reconciled")
        return {"outcome": "queue_reconciled", "next_state": target}

    def _execute_milestone_gate(
        self, project: Project, mode: str, run_id: str, project_state: dict[str, Any]
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
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
        self._transition_project(project, project_state, "milestone_gate", run_id=run_id, checkpoint="milestone_gate_launch")
        result = self._launch_session(SessionRequest(
            action="milestone_gate", project=project, run_id=run_id, mode=mode
        ), inspector, "milestone_gate")
        state["session_id"] = result.session_id
        if result.returncode != 0:
            self._advance_cycle(cycle_path, state, "failed", inspector, "milestone_gate_session_failed")
            raise SessionError("milestone gate session returned non-zero")
        queue = FeatureQueue.from_path(project.repository / project.queue_location)
        milestone = queue.milestone(project.active_milestone or "")
        if milestone is None or milestone.get("status") != "gate_passed" or not queue.milestone_complete(project.active_milestone or ""):
            raise QueueError("milestone gate response lacks corroborating gate_passed queue evidence")
        if not inspector.is_clean:
            raise ConveyorError("milestone branch is not clean after gate")
        self._advance_cycle(cycle_path, state, "completed", inspector, "milestone_gate_passed")
        gate = {
            "decision": "Approve or decline merge of the validated milestone branch into the default branch.",
            "milestone_branch": project.milestone_branch,
            "default_branch_merge_performed": False,
        }
        self._transition_project(project, project_state, "milestone_ready_for_merge", run_id=run_id, checkpoint="milestone_gate_passed", stop_reason="human milestone merge approval required", human_gate=gate)
        return {"outcome": "milestone_ready_for_merge", "human_gate": gate}

    def _finish_resumed_feature(
        self,
        project: Project,
        run_id: str,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        cycle_path = inspector.cycle_state_path()
        project_state = self._project_document(project, run_id, inspector.identity()["path_fingerprint"])
        if project_state["current_state"] in {"feature_running", "feature_review"}:
            self._transition_project(
                project, project_state, "feature_accepted", run_id=run_id,
                checkpoint="resumed_feature_integrated", feature=str(state.get("current_feature")),
            )
        queue = FeatureQueue.from_path(project.repository / project.queue_location)
        if queue.milestone_complete(project.active_milestone or ""):
            if state["current_phase"] in {"feature_integrated", "next_feature_selection"}:
                self._advance_cycle(cycle_path, state, "milestone_gate", inspector, "resumed_milestone_complete")
            return {"project_id": project.project_id, **self._execute_milestone_gate(project, "resume", run_id, project_state)}
        if state["current_phase"] == "feature_integrated":
            self._advance_cycle(cycle_path, state, "next_feature_selection", inspector, "resumed_next_feature_selection")
        if state["current_phase"] == "next_feature_selection":
            self._advance_cycle(cycle_path, state, "completed", inspector, "resumed_feature_cycle_complete")
        if project_state["current_state"] == "feature_accepted":
            self._transition_project(project, project_state, "feature_ready", run_id=run_id, checkpoint="resumed_next_feature_ready")
        if project.automation_mode == "one_feature":
            return {"project_id": project.project_id, "outcome": "feature_integrated", **evidence}
        return self.run_project(project, project.automation_mode)

    def resume_project(self, project: Project, run_id: str | None = None) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        state = self.cycle_store.read(inspector.cycle_state_path())
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
        if phase in {"feature_integrated", "next_feature_selection"}:
            feature, accepted, integrated = self._verify_integrated_feature(project, inspector, state)
            evidence = {"feature": feature["id"], "accepted_commit": accepted, "integrated_commit": integrated}
            return self._finish_resumed_feature(project, run_id, inspector, state, evidence)
        if phase == "failed":
            checkpoint = str(state.get("last_successful_checkpoint") or "")
            if "milestone_gate" in checkpoint:
                phase = "milestone_gate"
            elif "integration" in checkpoint:
                phase = "integration_pending"
            else:
                phase = "feature_in_progress"
            self._advance_cycle(inspector.cycle_state_path(), state, phase, inspector, "focused_resume_after_failure")
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
        ), inspector, phase)
        if action in {"feature_cycle", "milestone_integration"}:
            evidence = self._reconcile_feature_evidence(project, inspector, inspector.cycle_state_path(), state, result)
            return self._finish_resumed_feature(project, run_id, inspector, state, evidence)
        project_state = self._project_document(project, run_id, inspector.identity()["path_fingerprint"])
        return {"project_id": project.project_id, **self._execute_milestone_gate(project, "resume", run_id, project_state)}

    def run_project(self, project: Project, mode: str, *, dry_run: bool = False) -> dict[str, Any]:
        if mode == "resume":
            if dry_run:
                return build_project_plan(self.effective_project(project), self.configuration.conveyor, self.root)
            return self.resume_project(self.effective_project(project))
        effective = self.effective_project(project)
        plan = build_project_plan(effective, self.configuration.conveyor, self.root)
        if dry_run or mode == "audit":
            return plan
        if plan["proposed_next_action"] in {"disabled", "repository_dirty", "writer_locked", "human_decision_required", "conveyor_error"}:
            return {"project_id": project.project_id, "outcome": plan["proposed_next_action"], "plan": plan}
        run_id = str(uuid.uuid4())
        inspector = RepositoryInspector(project.repository)
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
                evidence = self._execute_feature(effective, mode, run_id, project_state)
                completed_features += 1
                queue = FeatureQueue.from_path(project.repository / project.queue_location)
                inspector = RepositoryInspector(project.repository)
                cycle_path = inspector.cycle_state_path()
                cycle_state = self.cycle_store.read(cycle_path)
                if cycle_state is None:
                    raise ConveyorError("feature integration completed without durable cycle state")
                if queue.milestone_complete(project.active_milestone or ""):
                    if mode == "one_feature":
                        self._advance_cycle(cycle_path, cycle_state, "completed", inspector, "one_feature_stop")
                        self._transition_project(project, project_state, "milestone_gate", run_id=run_id, checkpoint="milestone_complete", feature=evidence["feature"])
                        return {"project_id": project.project_id, "outcome": "one_feature_integrated", **evidence}
                    return {"project_id": project.project_id, **self._execute_milestone_gate(effective, mode, run_id, project_state)}
                else:
                    self._advance_cycle(cycle_path, cycle_state, "next_feature_selection", inspector, "next_feature_recalculated")
                    self._advance_cycle(cycle_path, cycle_state, "completed", inspector, "feature_cycle_complete")
                    self._transition_project(project, project_state, "feature_ready", run_id=run_id, checkpoint="next_feature_recalculated", feature=None)
                if mode == "one_feature":
                    return {"project_id": project.project_id, "outcome": "one_feature_integrated", **evidence}
            elif action == "milestone_gate":
                return {"project_id": project.project_id, **self._execute_milestone_gate(effective, mode, run_id, project_state)}
            else:
                return {"project_id": project.project_id, "outcome": action, "completed_features": completed_features, "plan": plan}
            if completed_features >= 100:
                raise ConveyorError("safety stop: feature-cycle bound reached")
