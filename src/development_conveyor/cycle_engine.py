"""Repository-level execution engine with checkpoints and evidence validation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Configuration
from .errors import ConveyorError, LockError, QueueError, RecoveryError, SessionError
from .locks import (
    DurableLock,
    RepositoryWriterLease,
    inspect_repository_writer_lock,
    make_lock_record,
)
from .logging import EventLogger, JsonStateStore, atomic_write_bytes, atomic_write_json, run_event, utc_now
from .queue import FeatureQueue, resolve_queue_path
from .recovery import StartupReconciliation, assess_recovery, assess_startup_reconciliation
from .registry import Project
from .reporting import build_project_plan
from .repository import RepositoryInspector
from .retries import RetryBudget
from .sessions import SessionLauncher, SessionPlan, SessionRequest, SessionResult
from .state_machine import CYCLE_MACHINE, PORTFOLIO_MACHINE

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DETERMINISTIC_COMPATIBILITY_FAILURES = {
    "cli_upgrade_required",
    "configuration_incompatible",
    "cli_missing",
    "cli_version_too_old",
    "unsupported_model",
    "unsupported_reasoning_effort",
    "model_policy_invalid",
    "compatibility_unknown",
}


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
        proposed = plan.get("proposed_next_action")
        compatibility_action = {
            "queue_reconciliation": "queue_reconciliation",
            "planning_refinement": "queue_reconciliation",
            "milestone_gate": "milestone_gate",
        }.get(str(proposed), "feature_cycle")
        compatibility = self._compatibility_snapshot(project, compatibility_action)
        stale = plan.get("stale_cycle_evidence")
        if isinstance(stale, dict) and stale.get("classification") == "deterministic_failed_cycle":
            stale["environment_remediation_verified"] = bool(
                compatibility and compatibility.get("compatible") is True
            )
            stale["compatibility_preflight"] = compatibility
        assessment = assess_startup_reconciliation(effective, persisted, plan)
        plan.update(self._reconciliation_fields(assessment))
        plan["compatibility_preflight"] = compatibility
        if assessment.classification in {
            "human_decision_required", "invalid_state_evidence", "deterministic_failure_human_gate"
        }:
            plan["proposed_next_action"] = "human_decision_required"
            plan["expected_stop_condition"] = assessment.reason
            plan["sessions_that_would_launch"] = []
        elif compatibility and compatibility.get("compatible") is not True:
            plan["proposed_next_action"] = "human_decision_required"
            plan["expected_stop_condition"] = compatibility.get("diagnostic")
            plan["sessions_that_would_launch"] = []
            plan["compatibility_human_gate"] = compatibility
        return plan

    def _compatibility_snapshot(self, project: Project, action: str) -> dict[str, Any] | None:
        probe = getattr(self.launcher, "compatibility", None)
        if probe is None:
            return None
        result = probe(action, project_id=project.project_id)
        return result.as_dict()

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
            stale = assessment.evidence.get("stale_cycle_evidence")
            if isinstance(stale, dict) and stale.get("classification") == "deterministic_failed_cycle":
                evidence["superseded_cycle"] = stale
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
                    human_gate=assessment.human_decision if final and target == "human_decision_required" else None,
                    state_evidence=evidence,
                )
            return document
        finally:
            reservation.release(run_id)

    def _new_cycle_state(
        self,
        project: Project,
        run_id: str,
        inspector: RepositoryInspector,
        feature: dict[str, Any] | None,
        *,
        compatibility: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stamp = utc_now()
        identity = inspector.identity()
        milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False) if project.milestone_branch else None
        starting_commit = (feature.get("integration_base_commit") or milestone_head) if feature else milestone_head
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        queue_fingerprint = hashlib.sha256(queue_path.read_bytes()).hexdigest()
        dependencies = list(feature.get("dependencies", [])) if feature else []
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        dependency_statuses = {
            dependency: ((queue.feature(dependency) or {}).get("status")) for dependency in dependencies
        }
        project_state = self.load_project_state(project) or {}
        prior_evidence = project_state.get("state_evidence") if isinstance(project_state.get("state_evidence"), dict) else {}
        superseded = prior_evidence.get("superseded_cycle") if isinstance(prior_evidence, dict) else None
        feature_branch = self._expected_feature_branch(project, feature) if feature else None
        return {
            "schema_version": 1,
            "conveyor_run_id": run_id,
            "project_id": project.project_id,
            "repository_identity": identity,
            "repository_path_fingerprint": identity["path_fingerprint"],
            "active_milestone": project.active_milestone,
            "current_feature": feature.get("id") if feature else None,
            "selected_feature": feature.get("id") if feature else None,
            "feature_dependencies": dependencies,
            "dependency_evidence": {
                "declared": dependencies,
                "statuses": dependency_statuses,
                "all_complete": all(value in {"done", "integrated"} for value in dependency_statuses.values()),
            },
            "queue_fingerprint": queue_fingerprint,
            "feature_branch": feature_branch,
            "feature_worktree": None,
            "session_completion_classification": None,
            "session_completion_flags": [],
            "session_completion_evidence": None,
            "branch_recovery": None,
            "feature_starting_commit": starting_commit,
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
            "preflight_compatibility": compatibility,
            "failure_classification": None,
            "retry_exhausted": False,
            "environment_remediation_verified": bool(compatibility and compatibility.get("compatible") is True),
            "next_safe_action": None,
            "supersedes_run_id": superseded.get("run_id") if isinstance(superseded, dict) else None,
            "supersedes_session_id": superseded.get("session_id") if isinstance(superseded, dict) else None,
            "superseded_cycle_archive": None,
            "stop_reason": None,
            "human_decision_required": None,
            "resume_instructions": f"scripts/conveyor resume --project {project.project_id}",
            "session_id": None,
            "feature_session_id": None,
            "integration_session_id": None,
            "milestone_gate_session_id": None,
            "created_at": stamp,
            "updated_at": stamp,
        }

    @staticmethod
    def _expected_feature_branch(project: Project, feature: dict[str, Any]) -> str:
        recorded = feature.get("branch")
        if isinstance(recorded, str) and recorded:
            return recorded
        try:
            adapter = json.loads((project.repository / project.validation_source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"cannot derive feature branch from repository adapter: {exc}") from exc
        pattern = (adapter.get("git") or {}).get("feature_branch_pattern")
        feature_id = feature.get("id")
        title = feature.get("name") or feature.get("title")
        if not isinstance(pattern, str) or not isinstance(feature_id, str) or not isinstance(title, str):
            raise RecoveryError("feature branch is absent and adapter branch metadata is incomplete")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        try:
            branch = pattern.format(feature_id=feature_id, feature_id_lower=feature_id.lower(), slug=slug)
        except (KeyError, ValueError) as exc:
            raise RecoveryError(f"feature branch pattern is unsupported: {pattern}") from exc
        if not branch:
            raise RecoveryError("derived feature branch is empty")
        return branch

    def _archive_superseded_cycle(
        self,
        project_state: dict[str, Any],
        cycle_path: Path,
        new_run_id: str,
    ) -> dict[str, Any] | None:
        evidence = project_state.get("state_evidence")
        superseded = evidence.get("superseded_cycle") if isinstance(evidence, dict) else None
        if not isinstance(superseded, dict):
            return None
        old_run_id = superseded.get("run_id")
        expected_fingerprint = superseded.get("cycle_fingerprint")
        if not isinstance(old_run_id, str) or not isinstance(expected_fingerprint, str):
            raise RecoveryError("superseded cycle evidence lacks run identity or fingerprint")
        try:
            cycle_bytes = cycle_path.read_bytes()
        except OSError as exc:
            raise RecoveryError(f"cannot read superseded cycle evidence: {exc}") from exc
        actual_fingerprint = hashlib.sha256(cycle_bytes).hexdigest()
        if actual_fingerprint != expected_fingerprint:
            raise RecoveryError("superseded cycle changed before immutable archival")
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        archive_path = self._report_path(report_root, old_run_id, "cycle-state-snapshot.json")
        if archive_path.exists():
            if archive_path.read_bytes() != cycle_bytes:
                raise RecoveryError("existing superseded cycle archive does not match exact source bytes")
        else:
            atomic_write_bytes(archive_path, cycle_bytes)
        metadata_path = self._report_path(report_root, old_run_id, "cycle-state-snapshot.meta.json")
        metadata = {
            "schema_version": 1,
            "project_id": project_state.get("project_id"),
            "superseded_run_id": old_run_id,
            "superseded_session_id": superseded.get("session_id"),
            "superseded_by_run_id": new_run_id,
            "sha256": actual_fingerprint,
            "archive_path": str(archive_path),
            "source_cycle_path": str(cycle_path),
            "archived_at": utc_now(),
        }
        if metadata_path.exists():
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            if existing.get("sha256") != actual_fingerprint:
                raise RecoveryError("existing superseded cycle archive metadata disagrees with source bytes")
            metadata = existing
        else:
            atomic_write_json(metadata_path, metadata)
        return metadata

    def _validate_cycle_launch_invariants(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        selected_feature: str,
    ) -> None:
        required = {
            "current_feature": state.get("current_feature"),
            "active_milestone": state.get("active_milestone"),
            "milestone_branch": state.get("milestone_branch"),
            "feature_starting_commit": state.get("feature_starting_commit"),
            "milestone_pre_integration_commit": state.get("milestone_pre_integration_commit"),
            "repository_identity": state.get("repository_identity"),
            "queue_fingerprint": state.get("queue_fingerprint"),
            "selected_feature": state.get("selected_feature"),
            "dependency_evidence": state.get("dependency_evidence"),
            "feature_branch": state.get("feature_branch"),
        }
        missing = sorted(key for key, value in required.items() if value is None or value == "")
        if missing:
            raise RecoveryError("feature cycle launch invariants are incomplete: " + ", ".join(missing))
        milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False)
        if state["feature_starting_commit"] != milestone_head:
            raise RecoveryError("feature starting commit must equal the verified milestone branch HEAD")
        if state["milestone_pre_integration_commit"] != milestone_head:
            raise RecoveryError("milestone pre-integration commit must equal the verified milestone branch HEAD")
        if state["selected_feature"] != selected_feature or state["current_feature"] != selected_feature:
            raise RecoveryError("selected feature evidence disagrees with cycle state")
        if state["last_verified_git_state"].get("clean") is not True:
            raise RecoveryError("feature cycle launch requires verified clean Git state")
        if state["dependency_evidence"].get("all_complete") is not True:
            raise RecoveryError("feature dependencies are not verified complete")
        compatibility = state.get("preflight_compatibility")
        if compatibility is not None and compatibility.get("compatible") is not True:
            raise RecoveryError("feature cycle launch requires compatible Codex preflight evidence")

    def _writer_lease(self, project: Project, inspector: RepositoryInspector) -> RepositoryWriterLease:
        return RepositoryWriterLease(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ),
            project.repository,
        )

    def _verify_feature_branch_runtime(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        *,
        require_starting_head: bool,
    ) -> dict[str, Any]:
        branch = state.get("feature_branch")
        starting = state.get("feature_starting_commit")
        milestone = state.get("milestone_branch")
        milestone_start = state.get("milestone_pre_integration_commit")
        if not all(isinstance(item, str) and item for item in (branch, starting, milestone, milestone_start)):
            raise RecoveryError("feature branch runtime evidence is incomplete")
        branch_head = inspector.rev_parse(str(branch), check=False)
        milestone_head = inspector.rev_parse(str(milestone), check=False)
        worktree = inspector.branch_worktree(str(branch)) if branch_head else None
        evidence = {
            "expected_branch": branch,
            "actual_branch": inspector.current_branch,
            "feature_branch_head": branch_head,
            "feature_starting_commit": starting,
            "actual_worktree": str(worktree) if worktree else None,
            "milestone_branch": milestone,
            "milestone_branch_head": milestone_head,
            "milestone_pre_integration_commit": milestone_start,
            "head": inspector.head,
            "git_operations": inspector.git_operation_state(),
        }
        if branch_head is None:
            raise RecoveryError("persisted feature branch is missing from Git")
        if worktree != project.repository.resolve() or inspector.current_branch != branch:
            raise RecoveryError("repository session is not executing in the verified feature worktree")
        if any(evidence["git_operations"].values()):
            raise RecoveryError("feature branch runtime has an unfinished Git operation")
        if milestone_head != milestone_start:
            raise RecoveryError("milestone branch ref changed during the feature cycle")
        if require_starting_head and (branch_head != starting or inspector.head != starting):
            raise RecoveryError("feature branch does not point to the verified feature starting commit")
        return evidence

    def _prepare_feature_branch(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        branch = str(state["feature_branch"])
        starting = str(state["feature_starting_commit"])
        milestone_head = inspector.rev_parse(str(state["milestone_branch"]), check=False)
        if milestone_head != state.get("milestone_pre_integration_commit") or milestone_head != starting:
            raise RecoveryError("milestone branch changed before feature branch preparation")
        branch_head = inspector.rev_parse(branch, check=False)
        if branch_head is None:
            inspector.switch_feature_branch(branch, starting_commit=starting)
        elif branch_head == starting:
            existing_worktree = inspector.branch_worktree(branch)
            if existing_worktree not in {None, project.repository.resolve()}:
                raise RecoveryError("feature branch already belongs to another worktree")
            if inspector.current_branch != branch:
                inspector.switch_feature_branch(branch)
        else:
            raise RecoveryError("existing feature branch does not point to the verified starting commit")
        evidence = self._verify_feature_branch_runtime(
            project, inspector, state, require_starting_head=True
        )
        state["feature_worktree"] = str(project.repository.resolve())
        return evidence

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
            matching_feature_lease = (
                request.action == "feature_cycle"
                and recorded_run == request.run_id
                and record.get("feature_id") == request.feature
                and record.get("branch") == inspector.current_branch
                and Path(str(record.get("worktree"))).expanduser().resolve()
                == request.project.repository.resolve()
            )
            if not matching_feature_lease:
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
            result: SessionResult
            try:
                result = self.launcher.launch(request)
            except SessionError as exc:
                report_path = self._persist_launch_failure(request, phase, exc)
                match = re.search(r"classification=([a-z_]+)", str(exc))
                classification = match.group(1) if match else "session_execution_failed"
                deterministic = classification in {
                    "cli_missing", "cli_version_too_old", "unsupported_model",
                    "unsupported_reasoning_effort", "model_policy_invalid", "compatibility_unknown",
                }
                if not deterministic:
                    raise SessionError(
                        f"project={request.project.project_id}; run_id={request.run_id}; action={request.action}; "
                        f"cwd={request.project.repository}; classification=session_execution_failed; "
                        f"error={exc}; report={report_path}; current_state={phase}; "
                        f"resume=scripts/conveyor resume --project {request.project.project_id}"
                    ) from exc
                compatibility = self._compatibility_snapshot(request.project, request.action) or {
                    "classification": classification,
                    "compatible": False,
                    "diagnostic": str(exc),
                }
                plan = SessionPlan(
                    (),
                    request.project.repository,
                    "",
                    hashlib.sha256(b"").hexdigest(),
                    "not_launched",
                    compatibility.get("effective_model"),
                    compatibility.get("effective_reasoning"),
                    compatibility.get("executable"),
                    compatibility,
                )
                result = SessionResult(
                    action=request.action,
                    returncode=2,
                    session_id=request.session_id,
                    redacted_output=str(exc),
                    plan=plan,
                    redacted_stderr=str(exc),
                    result_classification=classification,
                    exit_classification=classification,
                    report_path=str(report_path),
                    failure_classification=classification,
                    retryable=False,
                    primary_terminal_error=str(exc),
                )
        finally:
            if reservation is not None:
                reservation.release(request.run_id)
        if result.report_path:
            report_path = Path(result.report_path)
        else:
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
        compatibility = self._compatibility_snapshot(request.project, request.action)
        try:
            plan = self.launcher.plan(request)
            argv = list(plan.argv)
            cwd = str(plan.cwd)
        except Exception:
            argv = []
            cwd = str(request.project.repository)
        message = str(error)
        match = re.search(r"classification=([a-z_]+)", message)
        classification = (
            match.group(1)
            if match
            else ("process_launch_failure" if "process launch failed" in message else "session_execution_failed")
        )
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
            "failure_classification": classification,
            "retryable": False,
            "effective_model": (
                getattr(plan, "effective_model", None) if "plan" in locals() else (compatibility or {}).get("effective_model")
            ),
            "effective_reasoning": (
                getattr(plan, "effective_reasoning", None) if "plan" in locals() else (compatibility or {}).get("effective_reasoning")
            ),
            "codex_executable": (
                getattr(plan, "codex_executable", None) if "plan" in locals() else (compatibility or {}).get("executable")
            ),
            "compatibility": getattr(plan, "compatibility", None) if "plan" in locals() else compatibility,
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
            "failure_classification": result.failure_classification,
            "retryable": result.retryable,
            "primary_terminal_error": result.primary_terminal_error,
            "secondary_diagnostics": list(result.secondary_diagnostics),
            "effective_model": result.plan.effective_model,
            "effective_reasoning": result.plan.effective_reasoning,
            "codex_executable": result.plan.codex_executable,
            "compatibility": result.plan.compatibility,
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
        if result.retryable:
            return True
        if result.action != "queue_reconciliation":
            return False
        if result.result_classification in {"session_execution_failed", "structured_output_invalid"}:
            return result.structured_result is None or bool(result.structured_result.get("retryable"))
        return False

    @staticmethod
    def _session_failure_message(
        project: Project,
        run_id: str,
        result: SessionResult,
        current_state: str,
        feature: str | None = None,
    ) -> str:
        secondary = ",".join(result.secondary_diagnostics) or "none captured"
        return (
            f"project={project.project_id}; run_id={run_id}; action={result.action}; cwd={result.plan.cwd}; "
            f"feature={feature or 'none'}; "
            f"model={result.plan.effective_model}; reasoning={result.plan.effective_reasoning}; "
            f"codex_executable={result.plan.codex_executable}; "
            f"detected_version={(result.plan.compatibility or {}).get('detected_version')}; "
            f"exit_status={result.returncode}; classification={result.failure_classification or result.result_classification}; "
            f"exit_classification={result.exit_classification}; "
            f"primary_terminal_error={result.primary_terminal_error or 'none captured'}; "
            f"secondary_diagnostics={secondary}; retryable={result.retryable}; "
            f"structured_output={result.structured_output_validation}; "
            f"report={result.report_path}; current_state={current_state}; "
            f"remediation={((result.plan.compatibility or {}).get('remediation') or 'inspect the persisted report')}; "
            f"continue={((result.plan.compatibility or {}).get('validation_command') or f'scripts/conveyor resume --project {project.project_id}')}"
        )

    def _launch_with_retries(
        self,
        request: SessionRequest,
        inspector: RepositoryInspector,
        phase: str,
        retry_key: str,
        *,
        reservation_held: bool = False,
    ) -> tuple[SessionResult, list[dict[str, Any]], dict[str, Any]]:
        configured = int(self.configuration.conveyor["retries"][retry_key])
        limit = request.project.maximum_retries if request.project.maximum_retries is not None else configured
        budget = RetryBudget(limit)
        attempts: list[dict[str, Any]] = []
        result = self._launch_session(request, inspector, phase, reservation_held=reservation_held)
        while self._session_requires_retry(result) and budget.remaining:
            evidence = hashlib.sha256(result.redacted_output[-4000:].encode()).hexdigest()
            structured = result.structured_result or {}
            hypothesis = result.retry_hypothesis or structured.get("summary")
            remediation = result.remediation_action or structured.get("next_action")
            supporting = result.retry_evidence or (
                f"classification={result.failure_classification or result.result_classification}; "
                f"redacted output fingerprint={evidence}"
            )
            if not isinstance(hypothesis, str) or not isinstance(remediation, str):
                break
            attempt = budget.record(
                hypothesis=hypothesis,
                evidence=supporting,
                failure_classification=result.failure_classification or result.result_classification or "unknown",
                command=result.plan.argv,
                session_id=result.session_id,
                model=result.plan.effective_model,
                reasoning=result.plan.effective_reasoning,
                remediation_action=remediation,
            )
            attempts.append({
                "attempt": attempt,
                "timestamp": utc_now(),
                "evidence_fingerprint": evidence,
                "previous_exit": result.returncode,
                "previous_classification": result.result_classification,
                "failure_classification": result.failure_classification,
                "hypothesis": hypothesis,
                "supporting_evidence": supporting,
                "remediation_action": remediation,
                "model": result.plan.effective_model,
                "reasoning": result.plan.effective_reasoning,
                "session_id": result.session_id,
            })
            result = self._launch_session(replace(
                request,
                mode="repair",
                session_id=result.session_id,
                repair_attempt=attempt,
                repair_evidence=evidence,
                repair_hypothesis=hypothesis,
                remediation_action=remediation,
                repair_supporting_evidence=supporting,
            ), inspector, phase, reservation_held=reservation_held)
        still_retryable = self._session_requires_retry(result)
        retry_status = {
            "limit": limit,
            "attempts_consumed": len(attempts),
            "attempts_remaining": budget.remaining,
            "retryable": still_retryable,
            "exhausted": bool(still_retryable and budget.remaining == 0),
            "stopped_without_contract": bool(still_retryable and budget.remaining > 0),
        }
        return result, attempts, retry_status

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

    def _verify_accepted_feature(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        feature: dict[str, Any],
    ) -> str:
        if feature.get("status") not in {"accepted", "integration_pending"}:
            raise QueueError("feature is not backed by accepted queue evidence")
        if feature.get("integration_status") != "pending":
            raise QueueError("accepted feature integration status is not pending")
        acceptance = feature.get("acceptance")
        if not isinstance(acceptance, dict) or not all(
            acceptance.get(key) is True
            for key in ("tests_passed", "review_passed", "documentation_current")
        ):
            raise QueueError("accepted feature acceptance evidence is incomplete")
        accepted = self._resolve_accepted(inspector, feature)
        starting = feature.get("integration_base_commit") or state.get("feature_starting_commit")
        branch = state.get("feature_branch")
        if not isinstance(starting, str) or not inspector.ref_exists(starting):
            raise QueueError("feature starting commit evidence is missing")
        if not isinstance(branch, str) or inspector.rev_parse(branch, check=False) != accepted:
            raise QueueError("accepted commit is not the verified feature branch HEAD")
        if inspector.commit_count(starting, accepted) != 1:
            raise QueueError("feature does not have exactly one accepted commit relative to its recorded start")
        if str(feature["id"]).lower() not in inspector.commit_subject(accepted).lower():
            raise QueueError("accepted commit message does not identify the feature")
        self._verify_feature_branch_runtime(
            project, inspector, state, require_starting_head=False
        )
        if not inspector.is_clean:
            raise QueueError("accepted feature worktree is not clean")
        return accepted

    def _classify_successful_feature_result(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        feature: dict[str, Any] | None,
    ) -> tuple[str, list[str], dict[str, Any], str | None]:
        expected = state.get("feature_branch")
        branch_head = inspector.rev_parse(str(expected), check=False) if isinstance(expected, str) else None
        worktree = inspector.branch_worktree(str(expected)) if branch_head and isinstance(expected, str) else None
        milestone_head = inspector.rev_parse(str(state.get("milestone_branch")), check=False)
        dirty = inspector.dirty_entries
        evidence: dict[str, Any] = {
            "actual_branch": inspector.current_branch,
            "expected_branch": expected,
            "actual_worktree": str(worktree) if worktree else None,
            "expected_worktree": state.get("feature_worktree"),
            "feature_branch_head": branch_head,
            "feature_starting_commit": state.get("feature_starting_commit"),
            "milestone_branch_head": milestone_head,
            "milestone_pre_integration_commit": state.get("milestone_pre_integration_commit"),
            "dirty_entry_count": len(dirty),
            "queue_feature_status": feature.get("status") if feature else None,
            "queue_accepted_commit": feature.get("accepted_commit") if feature else None,
        }
        branch_violated = bool(
            not isinstance(expected, str)
            or branch_head is None
            or inspector.current_branch != expected
            or worktree != project.repository.resolve()
            or milestone_head != state.get("milestone_pre_integration_commit")
        )
        if branch_violated:
            flags = ["branch_invariant_violated"]
            if dirty:
                flags.append("uncommitted_feature_work")
            return "branch_invariant_violated", flags, evidence, None
        if feature is None:
            return "queue_evidence_missing", ["queue_evidence_missing"], evidence, None
        accepted: str | None = None
        if feature.get("status") in {"accepted", "integration_pending"}:
            try:
                accepted = self._verify_accepted_feature(project, inspector, state, feature)
            except QueueError as exc:
                evidence["accepted_verification_error"] = str(exc)
                flags = ["feature_commit_missing"]
                if dirty:
                    flags.append("uncommitted_feature_work")
                return "feature_commit_missing", flags, evidence, None
            return "accepted_feature", ["accepted_feature"], evidence, accepted
        if dirty:
            return (
                "uncommitted_feature_work",
                ["uncommitted_feature_work", "incomplete_feature_result"],
                evidence,
                None,
            )
        return (
            "session_claimed_completion_without_evidence",
            ["session_claimed_completion_without_evidence", "incomplete_feature_result"],
            evidence,
            None,
        )

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
        retry_status: dict[str, Any] | None = None,
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        retry_status = retry_status or {
            "attempts_consumed": 0,
            "attempts_remaining": 0,
            "retryable": result.retryable,
            "exhausted": False,
        }
        if result.action == "milestone_integration":
            state["integration_session_id"] = result.session_id
        else:
            state["feature_session_id"] = result.session_id
            state["session_id"] = result.session_id
        if result.returncode != 0:
            classification = result.failure_classification or result.result_classification or "session_execution_failed"
            deterministic = classification in {
                "cli_upgrade_required", "configuration_incompatible", "cli_missing",
                "cli_version_too_old", "unsupported_model", "unsupported_reasoning_effort",
                "model_policy_invalid", "compatibility_unknown",
            }
            compatibility = result.plan.compatibility or state.get("preflight_compatibility") or {}
            continuation = (
                f"scripts/conveyor doctor --project {project.project_id}"
                if deterministic else f"scripts/conveyor resume --project {project.project_id}"
            )
            message = self._session_failure_message(
                project,
                str(state["conveyor_run_id"]),
                result,
                state["current_phase"],
                str(state.get("current_feature") or ""),
            )
            gate = {
                "reason": "repository session reached a deterministic compatibility gate" if deterministic else "repository session cannot continue safely",
                "project": project.project_id,
                "run_id": state["conveyor_run_id"],
                "feature": state.get("current_feature"),
                "classification": classification,
                "effective_model": result.plan.effective_model,
                "effective_reasoning": result.plan.effective_reasoning,
                "codex_executable": result.plan.codex_executable,
                "detected_version": compatibility.get("detected_version"),
                "required_minimum_version": compatibility.get("required_minimum_version"),
                "primary_terminal_error": result.primary_terminal_error,
                "secondary_diagnostics": list(result.secondary_diagnostics),
                "retryable": bool(result.retryable and not retry_status.get("exhausted")),
                "originally_retryable": result.retryable,
                "attempts_consumed": retry_status.get("attempts_consumed", 0),
                "attempts_remaining": 0 if deterministic else retry_status.get("attempts_remaining", 0),
                "retry_exhausted": bool(deterministic or retry_status.get("exhausted")),
                "environment_remediation_verified": False,
                "report_path": result.report_path,
                "remediation": compatibility.get("remediation") or "inspect the persisted report and record a materially different repair hypothesis",
                "safe_continuation_command": continuation,
                "old_session_will_resume": False if deterministic else None,
                "resolved": False,
            }
            state.update({
                "failure_classification": classification,
                "retry_exhausted": bool(deterministic or retry_status.get("exhausted")),
                "environment_remediation_verified": False,
                "next_safe_action": continuation,
                "stop_reason": message,
                "human_decision_required": gate,
            })
            self._advance_cycle(cycle_path, state, "human_decision_required", inspector, "session_terminal_failure")
            project_state = self._project_document(
                project, str(state["conveyor_run_id"]), inspector.identity()["path_fingerprint"]
            )
            if project_state["current_state"] != "human_decision_required":
                self._transition_project(
                    project,
                    project_state,
                    "human_decision_required",
                    run_id=str(state["conveyor_run_id"]),
                    checkpoint="session_terminal_failure",
                    feature=str(state.get("current_feature")),
                    stop_reason=message,
                    human_gate=gate,
                    state_evidence={"compatibility_preflight": compatibility, "failure_classification": classification},
                )
            raise SessionError(message)

        try:
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
        except QueueError as exc:
            if result.action == "feature_cycle":
                state["session_completion_classification"] = "queue_evidence_missing"
                state["session_completion_flags"] = ["queue_evidence_missing"]
                if inspector.dirty_entries:
                    state["session_completion_flags"].append("uncommitted_feature_work")
                state["session_completion_evidence"] = {
                    "queue_error": str(exc),
                    "actual_branch": inspector.current_branch,
                    "expected_branch": state.get("feature_branch"),
                    "dirty_entry_count": len(inspector.dirty_entries),
                }
                self._advance_cycle(
                    cycle_path,
                    state,
                    str(state["current_phase"]),
                    inspector,
                    "feature_session_incomplete",
                )
                raise RecoveryError(
                    "successful feature session lacked queue evidence: "
                    "classification=queue_evidence_missing"
                ) from exc
            raise
        feature = queue.feature(str(state["current_feature"]))
        if result.action == "feature_cycle":
            classification, flags, completion_evidence, accepted_result = (
                self._classify_successful_feature_result(
                    project, inspector, state, feature
                )
            )
            state["session_completion_classification"] = classification
            state["session_completion_flags"] = flags
            state["session_completion_evidence"] = completion_evidence
            if classification != "accepted_feature" or accepted_result is None:
                state["next_safe_action"] = f"scripts/conveyor resume --project {project.project_id}"
                self._advance_cycle(
                    cycle_path,
                    state,
                    str(state["current_phase"]),
                    inspector,
                    "feature_session_incomplete",
                )
                raise RecoveryError(
                    "successful feature session lacked accepted evidence: "
                    f"classification={classification}; flags={','.join(flags)}"
                )
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            writer_record = writer.record or {}
            if (
                not writer.exists
                or (writer_record.get("agent_run") or writer_record.get("run_id"))
                != state["conveyor_run_id"]
            ):
                state["session_completion_classification"] = "incomplete_feature_result"
                state["session_completion_flags"] = ["incomplete_feature_result"]
                state["session_completion_evidence"]["writer_lease_verified"] = False
                self._advance_cycle(
                    cycle_path,
                    state,
                    str(state["current_phase"]),
                    inspector,
                    "feature_session_incomplete",
                )
                raise LockError("accepted evidence appeared after the feature writer lease was released")
            state["session_completion_evidence"]["writer_lease_verified"] = True
            state["accepted_feature_commit"] = accepted_result

        if result.action == "feature_cycle" and state["current_phase"] == "branch_preparing":
            self._advance_cycle(cycle_path, state, "feature_in_progress", inspector, "accepted_feature_observed")
        if feature is None:
            raise QueueError("selected feature disappeared from the queue")
        if result.action == "feature_cycle" and feature.get("status") in {"review", "accepted", "integration_pending", "integrated"}:
            self._advance_cycle(cycle_path, state, "feature_review", inspector, "review_evidence_observed")
        if result.action == "feature_cycle" and feature.get("status") in {"accepted", "integration_pending", "integrated"}:
            self._advance_cycle(cycle_path, state, "feature_accepted", inspector, "acceptance_evidence_observed")
            self._advance_cycle(cycle_path, state, "integration_pending", inspector, "integration_pending_observed")

        if result.action == "feature_cycle" and feature.get("status") in {"accepted", "integration_pending"}:
            self._writer_lease(project, inspector).release(run_id=str(state["conveyor_run_id"]))
            state["writer_lock_identity"] = None
            self.cycle_store.write(cycle_path, state)
            if state["current_phase"] == "integration_pending":
                self._advance_cycle(
                    cycle_path, state, "integrating", inspector, "integration_session_launch"
                )
            integration_result, integration_repairs, integration_retry = self._launch_with_retries(SessionRequest(
                action="milestone_integration",
                project=project,
                run_id=state["conveyor_run_id"],
                mode="resume",
                feature=str(state["current_feature"]),
            ), inspector, "integrating", "integration_repairs", reservation_held=reservation_held)
            state["integration_attempts"].extend(integration_repairs)
            self.cycle_store.write(cycle_path, state)
            return self._reconcile_feature_evidence(
                project,
                inspector,
                cycle_path,
                state,
                integration_result,
                retry_status=integration_retry,
                reservation_held=reservation_held,
            )

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
        compatibility = self._compatibility_snapshot(project, "feature_cycle")
        if compatibility is not None and compatibility.get("compatible") is not True:
            gate = {
                "reason": "Codex compatibility preflight blocked the feature session before cycle creation.",
                "classification": compatibility.get("classification"),
                "configured_model": compatibility.get("effective_model"),
                "configured_reasoning": compatibility.get("effective_reasoning"),
                "codex_executable": compatibility.get("executable"),
                "detected_version": compatibility.get("detected_version"),
                "required_minimum_version": compatibility.get("required_minimum_version"),
                "remediation": compatibility.get("remediation"),
                "compatibility_validation_command": compatibility.get("validation_command"),
                "resolved": False,
            }
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="compatibility_preflight_blocked",
                feature=project_state.get("current_feature"),
                stop_reason=str(compatibility.get("diagnostic")),
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility},
            )
            raise SessionError(
                f"classification={compatibility.get('classification')}; remediation={compatibility.get('remediation')}; "
                f"continue={compatibility.get('validation_command')}"
            )
        reservation = None if reservation_held else self._launch_lock(project, inspector)
        feature_writer_acquired = False
        session_attempted = False
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
            superseded_archive = self._archive_superseded_cycle(
                project_state, cycle_path, run_id
            ) if cycle_path.exists() else None
            state = self._new_cycle_state(
                project, run_id, inspector, selection.feature, compatibility=compatibility
            )
            state["superseded_cycle_archive"] = superseded_archive
            self._validate_cycle_launch_invariants(project, inspector, state, selection.feature_id)
            self.cycle_store.write(cycle_path, state)
            self._advance_cycle(cycle_path, state, "preflight", inspector, "preflight_verified")
            self._advance_cycle(cycle_path, state, "feature_selected", inspector, selection.reason)
            self._advance_cycle(
                cycle_path,
                state,
                "branch_preparing",
                inspector,
                "feature_branch_preparation_started",
            )
            branch_evidence = self._prepare_feature_branch(project, inspector, state)
            state["session_completion_evidence"] = {"prelaunch_branch": branch_evidence}
            writer_record = self._writer_lease(project, inspector).acquire_or_resume(
                feature=selection.feature_id,
                branch=str(state["feature_branch"]),
                run_id=run_id,
            )
            feature_writer_acquired = True
            state["writer_lock_identity"] = writer_record
            self._verify_feature_branch_runtime(
                project, inspector, state, require_starting_head=True
            )
            self._advance_cycle(
                cycle_path,
                state,
                "branch_preparing",
                inspector,
                "feature_branch_and_writer_lease_verified",
            )
            self._transition_project(
                project,
                project_state,
                "feature_running",
                run_id=run_id,
                checkpoint="feature_session_launch",
                feature=selection.feature_id,
                state_evidence={
                    "compatibility_preflight": compatibility,
                    "feature_starting_commit": state["feature_starting_commit"],
                    "milestone_pre_integration_commit": state["milestone_pre_integration_commit"],
                    "queue_fingerprint": state["queue_fingerprint"],
                    "dependency_evidence": state["dependency_evidence"],
                    "superseded_cycle_archive": superseded_archive,
                },
            )
            session_attempted = True
            result, repairs, retry_status = self._launch_with_retries(SessionRequest(
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
                retry_status=retry_status,
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
            if feature_writer_acquired and not session_attempted:
                self._writer_lease(project, inspector).release(run_id=run_id)
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
        result, repairs, retry_status = self._launch_with_retries(SessionRequest(
            action="queue_reconciliation", project=project, run_id=run_id, mode=mode
        ), inspector, "queue_reconciliation", "queue_reconciliation_repairs", reservation_held=True)
        classification = result.result_classification
        if result.failure_classification in {
            "cli_upgrade_required", "configuration_incompatible", "cli_missing",
            "cli_version_too_old", "unsupported_model", "unsupported_reasoning_effort",
            "model_policy_invalid", "compatibility_unknown",
        }:
            compatibility = result.plan.compatibility or {}
            gate = {
                "reason": "Codex compatibility blocked queue reconciliation.",
                "classification": result.failure_classification,
                "effective_model": result.plan.effective_model,
                "effective_reasoning": result.plan.effective_reasoning,
                "codex_executable": result.plan.codex_executable,
                "detected_version": compatibility.get("detected_version"),
                "primary_terminal_error": result.primary_terminal_error,
                "secondary_diagnostics": list(result.secondary_diagnostics),
                "retryable": False,
                "attempts_consumed": retry_status["attempts_consumed"],
                "attempts_remaining": 0,
                "remediation": compatibility.get("remediation"),
                "safe_continuation_command": f"scripts/conveyor doctor --project {project.project_id}",
                "resolved": False,
            }
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="queue_reconciliation_compatibility_gate",
                stop_reason=str(result.primary_terminal_error or result.failure_classification),
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility},
            )
            return {
                "outcome": "human_decision_required",
                "next_state": "human_decision_required",
                "human_decision": gate,
                "report": result.report_path,
            }
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
        compatibility = self._compatibility_snapshot(project, "milestone_gate")
        if compatibility is not None and compatibility.get("compatible") is not True:
            gate = {
                "reason": "Codex compatibility preflight blocked the milestone gate session.",
                "classification": compatibility.get("classification"),
                "effective_model": compatibility.get("effective_model"),
                "effective_reasoning": compatibility.get("effective_reasoning"),
                "codex_executable": compatibility.get("executable"),
                "detected_version": compatibility.get("detected_version"),
                "required_minimum_version": compatibility.get("required_minimum_version"),
                "remediation": compatibility.get("remediation"),
                "safe_continuation_command": compatibility.get("validation_command"),
                "resolved": False,
            }
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="milestone_gate_compatibility_blocked",
                stop_reason=str(compatibility.get("diagnostic")),
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility},
            )
            return {"outcome": "human_decision_required", "human_gate": gate}
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
        state["milestone_gate_session_id"] = result.session_id
        state["session_id"] = result.session_id
        if result.returncode != 0:
            classification = result.failure_classification or result.result_classification or "session_execution_failed"
            compatibility = result.plan.compatibility or {}
            message = self._session_failure_message(project, run_id, result, state["current_phase"])
            gate = {
                "reason": "milestone gate session cannot continue safely",
                "classification": classification,
                "effective_model": result.plan.effective_model,
                "effective_reasoning": result.plan.effective_reasoning,
                "primary_terminal_error": result.primary_terminal_error,
                "secondary_diagnostics": list(result.secondary_diagnostics),
                "retryable": result.retryable,
                "report_path": result.report_path,
                "remediation": compatibility.get("remediation"),
                "safe_continuation_command": (
                    f"scripts/conveyor doctor --project {project.project_id}"
                    if classification in {
                        "cli_upgrade_required", "configuration_incompatible", "cli_missing",
                        "cli_version_too_old", "unsupported_model", "unsupported_reasoning_effort",
                        "model_policy_invalid", "compatibility_unknown",
                    }
                    else f"scripts/conveyor resume --project {project.project_id}"
                ),
                "resolved": False,
            }
            state.update({
                "failure_classification": classification,
                "retry_exhausted": not result.retryable,
                "next_safe_action": gate["safe_continuation_command"],
                "stop_reason": message,
                "human_decision_required": gate,
            })
            self._advance_cycle(cycle_path, state, "human_decision_required", inspector, "milestone_gate_session_terminal_failure")
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="milestone_gate_session_terminal_failure",
                stop_reason=message,
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility, "failure_classification": classification},
            )
            raise SessionError(message)
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

    def _compatibility_remediation_plan(
        self,
        project: Project,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Authorize only a fresh action-specific session after a deterministic gate clears."""

        classification = state.get("failure_classification")
        if (
            state.get("current_phase") != "human_decision_required"
            or classification not in DETERMINISTIC_COMPATIBILITY_FAILURES
        ):
            return None
        checkpoint = str(state.get("last_successful_checkpoint") or "")
        if state.get("milestone_gate_session_id") or "milestone_gate" in checkpoint:
            action = "milestone_gate"
            target_phase = "milestone_gate"
            target_project_state = "milestone_gate"
            prior_session_id = state.get("milestone_gate_session_id") or state.get("session_id")
        elif state.get("integration_session_id") or "integration" in checkpoint:
            action = "milestone_integration"
            target_phase = "integrating"
            target_project_state = "feature_running"
            prior_session_id = state.get("integration_session_id")
        else:
            # Pre-feature deterministic failures are recovered by startup reconciliation and a
            # replacement cycle, which also archives the exact superseded cycle bytes.
            return None
        compatibility = self._compatibility_snapshot(project, action)
        return {
            "action": action,
            "target_phase": target_phase,
            "target_project_state": target_project_state,
            "prior_session_id": prior_session_id,
            "compatibility": compatibility,
            "verified": bool(compatibility and compatibility.get("compatible") is True),
        }

    def _resume_remediated_action(
        self,
        project: Project,
        run_id: str,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        remediation: dict[str, Any],
    ) -> dict[str, Any]:
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
            raise RecoveryError(
                "compatibility remediation requires the clean configured milestone branch with no Git operation"
            )
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        action = str(remediation["action"])
        if action == "milestone_integration":
            feature = queue.feature(str(state.get("current_feature") or ""))
            if feature is None or feature.get("status") not in {"accepted", "integration_pending"}:
                raise RecoveryError(
                    "fresh integration remediation requires corroborating accepted or integration-pending queue evidence"
                )
            expected_head = state.get("milestone_pre_integration_commit")
            if not isinstance(expected_head, str) or repository.get("head") != expected_head:
                raise RecoveryError(
                    "fresh integration remediation requires the unchanged milestone pre-integration commit"
                )
        elif not queue.milestone_complete(project.active_milestone or ""):
            raise RecoveryError("fresh milestone-gate remediation requires a complete milestone")

        compatibility = dict(remediation["compatibility"])
        state.update({
            "preflight_compatibility": compatibility,
            "failure_classification": None,
            "retry_exhausted": False,
            "environment_remediation_verified": True,
            "next_safe_action": None,
            "stop_reason": None,
            "human_decision_required": None,
        })
        attempt_record = {
            "timestamp": utc_now(),
            "outcome": "compatibility_remediation_verified",
            "classification": compatibility.get("classification"),
            "prior_session_id": remediation.get("prior_session_id"),
            "old_session_will_resume": False,
            "new_session_required": True,
        }
        if action == "milestone_integration":
            state["integration_attempts"].append(attempt_record)
        else:
            state["validation_attempts"].append(attempt_record)
        self._advance_cycle(
            inspector.cycle_state_path(),
            state,
            str(remediation["target_phase"]),
            inspector,
            "compatibility_remediation_verified_fresh_session",
        )
        project_state = self._project_document(
            project, run_id, inspector.identity()["path_fingerprint"]
        )
        self._transition_project(
            project,
            project_state,
            str(remediation["target_project_state"]),
            run_id=run_id,
            checkpoint="compatibility_remediation_verified_fresh_session",
            feature=str(state.get("current_feature") or "") or None,
            state_evidence={
                "compatibility_preflight": compatibility,
                "old_session_will_resume": False,
                "new_session_required": True,
                "prior_session_id": remediation.get("prior_session_id"),
            },
        )
        request = SessionRequest(
            action=action,
            project=project,
            run_id=run_id,
            mode="resume_after_compatibility_remediation",
            feature=state.get("current_feature"),
            session_id=None,
        )
        if action == "milestone_integration":
            result, repairs, retry_status = self._launch_with_retries(
                request,
                inspector,
                "integrating",
                "integration_repairs",
                reservation_held=True,
            )
            state["integration_attempts"].extend(repairs)
            self.cycle_store.write(inspector.cycle_state_path(), state)
            evidence = self._reconcile_feature_evidence(
                project,
                inspector,
                inspector.cycle_state_path(),
                state,
                result,
                retry_status=retry_status,
                reservation_held=True,
            )
            return self._finish_resumed_feature(
                project,
                run_id,
                inspector,
                state,
                evidence,
                reservation_held=True,
            )
        result = self._launch_session(
            request, inspector, "milestone_gate", reservation_held=True
        )
        return {
            "project_id": project.project_id,
            **self._finalize_milestone_gate_result(
                project, run_id, project_state, inspector, state, result
            ),
        }

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
        compatibility_remediation = None
        if phase == "human_decision_required":
            compatibility_remediation = self._compatibility_remediation_plan(project, state)
            if not compatibility_remediation or not compatibility_remediation["verified"]:
                return {
                    "project_id": project.project_id,
                    "outcome": "human_decision_required",
                    "human_decision": state.get("human_decision_required"),
                    "compatibility_preflight": (
                        compatibility_remediation or {}
                    ).get("compatibility"),
                }
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
            ):
                raise RecoveryError("cycle, Git, or writer-lock evidence changed during resume acquisition")
            if compatibility_remediation:
                outcome = self._resume_remediated_action(
                    project, run_id, inspector, state, compatibility_remediation
                )
            elif phase in {"feature_integrated", "next_feature_selection"}:
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
                uncorroborated_continuation = bool(
                    action == "feature_cycle"
                    and state.get("feature_session_id")
                    and state.get("failure_classification") is None
                    and (
                        state.get("last_successful_checkpoint") == "feature_branch_recovered"
                        or state.get("session_completion_classification")
                        in {
                            "branch_invariant_violated",
                            "uncommitted_feature_work",
                            "incomplete_feature_result",
                            "session_claimed_completion_without_evidence",
                        }
                    )
                )
                if action == "feature_cycle":
                    self._verify_feature_branch_runtime(
                        project, inspector, state, require_starting_head=False
                    )
                    lease_record = self._writer_lease(project, inspector).acquire_or_resume(
                        feature=str(state.get("current_feature")),
                        branch=str(state.get("feature_branch")),
                        run_id=run_id,
                    )
                    state["writer_lock_identity"] = lease_record
                    self._advance_cycle(
                        cycle_path,
                        state,
                        str(state["current_phase"]),
                        inspector,
                        "writer_lease_acquired_before_resume",
                    )
                elif writer.exists:
                    raise LockError("non-feature continuation cannot reuse a feature writer lease")
                if action == "milestone_integration":
                    resume_session_id = state.get("integration_session_id")
                elif action == "milestone_gate":
                    resume_session_id = state.get("milestone_gate_session_id") or state.get("session_id")
                else:
                    resume_session_id = state.get("feature_session_id") or state.get("session_id")
                result = self._launch_session(SessionRequest(
                    action=action,
                    project=project,
                    run_id=run_id,
                    mode="resume",
                    feature=state.get("current_feature"),
                    session_id=resume_session_id,
                    continuation_reason=(
                        "uncorroborated_completion" if uncorroborated_continuation else None
                    ),
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

    def recover_feature_branch(self, project: Project, *, dry_run: bool) -> dict[str, Any]:
        """Recover an exact dirty milestone checkout by creating its persisted feature branch."""

        inspector = RepositoryInspector(project.repository)
        cycle_path = inspector.cycle_state_path()
        state = self.cycle_store.read(cycle_path)
        if state is None:
            return {
                "project_id": project.project_id,
                "outcome": "human_decision_required",
                "reason": "no durable feature cycle exists",
            }
        expected_branch = state.get("feature_branch")
        starting = state.get("feature_starting_commit")
        milestone_branch = state.get("milestone_branch")
        run_id = str(state.get("conveyor_run_id") or "")
        preconditions = {
            "project_id": state.get("project_id") == project.project_id,
            "expected_branch_recorded": isinstance(expected_branch, str) and bool(expected_branch),
            "starting_commit_recorded": isinstance(starting, str) and bool(starting),
            "milestone_branch_matches": milestone_branch == project.milestone_branch,
            "current_branch_is_milestone": inspector.current_branch == project.milestone_branch,
            "head_is_starting_commit": inspector.head == starting,
            "milestone_ref_is_starting_commit": inspector.rev_parse(project.milestone_branch or "", check=False)
            == starting,
            "feature_branch_absent": not (
                isinstance(expected_branch, str) and inspector.ref_exists(expected_branch)
            ),
            "no_git_operation": not any(inspector.git_operation_state().values()),
            "no_writer_lease": not inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ).exists(),
            "no_accepted_commit": state.get("accepted_feature_commit") is None,
            "normal_session_completion": bool(
                state.get("feature_session_id")
                and state.get("failure_classification") is None
                and state.get("last_successful_checkpoint")
                in {"feature_session_completed", "feature_session_incomplete"}
            ),
        }
        try:
            before_hashes = inspector.modified_file_hashes()
        except ConveyorError as exc:
            before_hashes = {}
            preconditions["dirty_state_unambiguous"] = False
            ambiguity = str(exc)
        else:
            preconditions["dirty_state_unambiguous"] = bool(before_hashes)
            ambiguity = None
        before_status = inspector.dirty_entries
        if not all(preconditions.values()):
            return {
                "project_id": project.project_id,
                "outcome": "human_decision_required",
                "reason": ambiguity or "feature branch recovery preconditions do not match exactly",
                "preconditions": preconditions,
                "actual_branch": inspector.current_branch,
                "expected_branch": expected_branch,
                "resume_allowed": False,
            }
        evidence = {
            "schema_version": 1,
            "project_id": project.project_id,
            "run_id": run_id,
            "expected_branch": expected_branch,
            "milestone_branch": milestone_branch,
            "starting_commit": starting,
            "before": {
                "branch": inspector.current_branch,
                "head": inspector.head,
                "milestone_ref": inspector.rev_parse(str(milestone_branch), check=False),
                "dirty_entries": before_status,
                "dirty_file_sha256": before_hashes,
                "worktrees": inspector.worktrees(),
                "local_branches": inspector.local_branches(),
            },
            "prohibited_operations_used": [],
            "session_launched": False,
        }
        if dry_run:
            return {
                "project_id": project.project_id,
                "outcome": "branch_recovery_ready",
                "dry_run": True,
                "application_repository_written": False,
                "actual_branch": inspector.current_branch,
                "expected_branch": expected_branch,
                "branch_recovery_required": True,
                "writer_lock_required_before_resume": True,
                "writer_lock_currently_held": False,
                "resume_allowed": False,
                "evidence": evidence,
            }

        reservation = self._launch_lock(project, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            run_id=run_id,
            current_feature=str(state.get("current_feature")),
            current_phase="feature_branch_recovery",
        ))
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        )
        preflight_path = self._report_path(
            report_root, run_id, "feature-branch-recovery-preflight.json"
        )
        try:
            current_hashes = inspector.modified_file_hashes()
            if (
                inspector.current_branch != project.milestone_branch
                or inspector.head != starting
                or inspector.rev_parse(project.milestone_branch or "", check=False) != starting
                or inspector.ref_exists(str(expected_branch))
                or any(inspector.git_operation_state().values())
                or current_hashes != before_hashes
                or inspector.dirty_entries != before_status
                or inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ).exists()
            ):
                return {
                    "project_id": project.project_id,
                    "outcome": "human_decision_required",
                    "reason": "feature branch recovery evidence changed before mutation",
                    "resume_allowed": False,
                }
            atomic_write_json(preflight_path, evidence)
            inspector.switch_feature_branch(str(expected_branch), starting_commit=str(starting))
            after_hashes = inspector.modified_file_hashes()
            after_status = inspector.dirty_entries
            after_worktree = inspector.branch_worktree(str(expected_branch))
            postconditions = {
                "actual_branch_is_expected": inspector.current_branch == expected_branch,
                "head_unchanged": inspector.head == starting,
                "feature_ref_is_starting_commit": inspector.rev_parse(str(expected_branch), check=False)
                == starting,
                "milestone_ref_unchanged": inspector.rev_parse(str(milestone_branch), check=False)
                == starting,
                "feature_worktree_is_registered_root": after_worktree == project.repository.resolve(),
                "dirty_entries_unchanged": after_status == before_status,
                "dirty_file_hashes_unchanged": after_hashes == before_hashes,
                "no_git_operation": not any(inspector.git_operation_state().values()),
                "no_writer_lease": not inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ).exists(),
            }
            evidence["after"] = {
                "branch": inspector.current_branch,
                "head": inspector.head,
                "milestone_ref": inspector.rev_parse(str(milestone_branch), check=False),
                "feature_ref": inspector.rev_parse(str(expected_branch), check=False),
                "dirty_entries": after_status,
                "dirty_file_sha256": after_hashes,
                "worktrees": inspector.worktrees(),
                "local_branches": inspector.local_branches(),
            }
            evidence["postconditions"] = postconditions
            if not all(postconditions.values()):
                raise RecoveryError(
                    "feature branch was created but post-recovery evidence is ambiguous; preserve all work"
                )
            state["feature_worktree"] = str(project.repository.resolve())
            state["writer_lock_identity"] = None
            state["session_completion_classification"] = "branch_invariant_violated"
            state["session_completion_flags"] = [
                "branch_invariant_violated",
                "uncommitted_feature_work",
            ]
            state["session_completion_evidence"] = {
                "actual_branch_before_recovery": project.milestone_branch,
                "expected_branch": expected_branch,
                "dirty_file_hashes_preserved": True,
                "milestone_ref_preserved": True,
                "previous_completion_corroborated": False,
            }
            state["branch_recovery"] = evidence
            state["next_safe_action"] = f"scripts/conveyor resume --project {project.project_id}"
            self._advance_cycle(
                cycle_path,
                state,
                str(state["current_phase"]),
                inspector,
                "feature_branch_recovered",
            )
            final_path = self._report_path(
                report_root, run_id, "feature-branch-recovery.json"
            )
            atomic_write_json(final_path, evidence)
            return {
                "project_id": project.project_id,
                "outcome": "feature_branch_recovered",
                "actual_branch": inspector.current_branch,
                "expected_branch": expected_branch,
                "branch_recovery_required": False,
                "writer_lock_required_before_resume": True,
                "writer_lock_currently_held": False,
                "resume_allowed_after_lock": True,
                "session_launched": False,
                "report_path": str(final_path),
                "postconditions": postconditions,
            }
        finally:
            reservation.release(run_id)

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
            if assessment.classification == "deterministic_failure_human_gate":
                document = persisted
                if assessment.would_persist:
                    if document is None:
                        document = self._project_document(
                            effective, None, plan["repository_path_fingerprint"]
                        )
                    document = self._persist_startup_reconciliation(
                        effective, document, assessment, run_id=f"recovery-{uuid.uuid4()}"
                    )
                return {
                    "project_id": project.project_id,
                    "outcome": "human_decision_required",
                    "current_state": (document or {}).get("current_state", assessment.derived_state),
                    "human_decision": assessment.human_decision,
                    "plan": plan,
                }
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
