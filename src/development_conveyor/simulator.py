"""Actual-kernel, no-LLM lifecycle and interruption simulator."""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from .command_authority import CommandAuthority
from .contracts import MutationPolicy, SessionResultEnvelope, TransactionState, WorkflowType
from .kernel import WorkflowKernel
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .projection import ProjectionEngine
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease
from .workflow_recovery import RecoveryPlanner


INTERRUPTION_BOUNDARIES = (
    "before_lease", "after_lease", "after_snapshot", "after_session_launch",
    "after_session_result", "after_file_mutation", "before_validation", "after_validation",
    "before_commit", "after_commit", "before_terminal_event", "after_terminal_event",
    "before_lease_release", "after_lease_release", "before_projection_update",
)

SIMULATED_WORKFLOWS = (
    WorkflowType.QUEUE_RECONCILIATION,
    WorkflowType.FEATURE_PREPARATION,
    WorkflowType.FEATURE_EXECUTION,
    WorkflowType.FEATURE_ACCEPTANCE,
    WorkflowType.MILESTONE_INTEGRATION,
    WorkflowType.MILESTONE_GATE,
    WorkflowType.HUMAN_DECISION_RESOLUTION,
    WorkflowType.RECOVERY,
)


class InjectedInterruption(RuntimeError):
    pass


_GIT_AUDIT: dict[str, list[tuple[str, ...]]] = {}


def _git(root: Path, *arguments: str) -> str:
    _GIT_AUDIT.setdefault(str(root.resolve()), []).append(tuple(arguments))
    result = subprocess.run(
        ["git", *arguments], cwd=root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


class DeterministicLifecycleSimulator:
    """Drive real ``WorkflowKernel`` transactions in a disposable Git repo."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.repository = self.root / "synthetic-app"
        self.controller = self.root / "controller"
        self.project: Project | None = None
        self.inject_boundary: str | None = None
        self.inject_workflow: WorkflowType | None = None
        self.injected = False
        self.recovery_evidence: list[dict[str, Any]] = []
        self.pending_interruptions: list[tuple[WorkflowType, str]] = []
        self._ledger_instance: EvidenceLedger | None = None
        self._projection_instance: ProjectionEngine | None = None
        self._lease_instance: WorkflowWriterLease | None = None

    def initialize(self, feature_count: int = 20) -> None:
        self.repository.mkdir(parents=True, exist_ok=True)
        _git(self.repository, "init", "-b", "main")
        _git(self.repository, "config", "user.name", "Conveyor Simulator")
        _git(self.repository, "config", "user.email", "simulator@example.invalid")
        exclude = self.repository / ".git/info/exclude"
        exclude.write_text(".factory/locks/writer.json\n", encoding="utf-8")
        (self.repository / ".factory/locks").mkdir(parents=True)
        (self.repository / "docs").mkdir()
        (self.repository / "application.txt").write_text("baseline\n", encoding="utf-8")
        (self.repository / "integration.txt").write_text("", encoding="utf-8")
        (self.repository / "preparation.json").write_text('{"feature":null}\n', encoding="utf-8")
        queue = {
            "schema_version": 1,
            "milestones": [{
                "id": "M0", "name": "Synthetic", "status": "active", "base_commit": None,
                "integration_branch": "codex/m0-kernel", "integrated_features": [],
                "last_validated_commit": None, "human_gate": True,
            }],
            "features": [{
                "id": f"F{number:03d}", "title": f"Synthetic {number}",
                "status": "proposed", "priority": number, "milestone": "M0",
                "dependencies": [] if number == 1 else [f"F{number - 1:03d}"],
                "spec": f"docs/F{number:03d}.md", "acceptance_criteria": ["kernel completes"],
                "requires_human_decision": False, "branch": None,
                "integration_base_commit": None, "accepted_commit": None,
                "integrated_commit": None, "integration_status": "pending",
                "integration_fix_commits": [],
            } for number in range(1, feature_count + 1)],
        }
        (self.repository / "docs/FEATURE_QUEUE.yaml").write_text(
            json.dumps(queue, indent=2) + "\n", encoding="utf-8"
        )
        (self.repository / "docs/AUTONOMY_CONTRACT.md").write_text(
            "# Synthetic autonomy\n", encoding="utf-8"
        )
        for number in range(1, feature_count + 1):
            (self.repository / f"docs/F{number:03d}.md").write_text(
                f"# F{number:03d}\n", encoding="utf-8"
            )
        _git(self.repository, "add", ".")
        _git(self.repository, "commit", "-m", "synthetic baseline")
        baseline = _git(self.repository, "rev-parse", "HEAD")
        _git(self.repository, "branch", "codex/m0-kernel")
        _git(self.repository, "switch", "codex/m0-kernel")
        self.project = Project(
            project_id="synthetic", repository=self.repository, enabled=True, priority=1,
            active_milestone="M0", recovery_branch="main", milestone_branch="codex/m0-kernel",
            validated_baseline_commit=baseline, queue_location="docs/FEATURE_QUEUE.yaml",
            autonomy_contract_location="docs/AUTONOMY_CONTRACT.md", validation_source=".factory/project.yaml",
            automation_mode="milestone", maximum_retries=None, schedule=None,
            human_gates=("milestone_merge",), last_accepted_feature=None,
            last_accepted_commit=None, current_state="queue_reconciliation",
            registration_notes="Disposable kernel simulator.",
        )

    def _resources(self) -> tuple[EvidenceLedger, ProjectionEngine, WorkflowWriterLease]:
        assert self.project is not None
        if self._ledger_instance is not None:
            return self._ledger_instance, self._projection_instance, self._lease_instance
        identity = RepositoryInspector(self.repository).identity()
        state = self.controller / "state/projects/synthetic"
        ledger = EvidenceLedger(
            state / "evidence-ledger.jsonl", project_id="synthetic",
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self._ledger_instance = ledger
        self._projection_instance = ProjectionEngine(ledger, state / "projection-cache.json")
        self._lease_instance = WorkflowWriterLease(self.repository / ".factory/locks/writer.json")
        return self._ledger_instance, self._projection_instance, self._lease_instance

    def _hook(self, boundary: str, transaction) -> None:
        if self.pending_interruptions:
            expected_workflow, expected_boundary = self.pending_interruptions[0]
            if transaction.workflow_type == expected_workflow and boundary == expected_boundary:
                self.pending_interruptions.pop(0)
                raise InjectedInterruption(f"{transaction.workflow_type.value}:{boundary}")
        if (
            not self.injected
            and boundary == self.inject_boundary
            and transaction.workflow_type == self.inject_workflow
        ):
            self.injected = True
            raise InjectedInterruption(f"{transaction.workflow_type.value}:{boundary}")

    def _phase_events(self, ledger: EvidenceLedger, transaction_id: str) -> list[dict[str, Any]]:
        return [event for event in ledger.read() if event["transaction_id"] == transaction_id]

    def _continue_phase(
        self,
        *,
        transaction_id: str,
        workflow: WorkflowType,
        feature_id: str | None,
        allowed_paths: tuple[str, ...],
        commit_subject: str,
        classification: str,
        next_state: str,
        mutate: Callable[[], None],
        completion_evidence: dict[str, Any],
        use_hook: bool,
        session_changed_paths: tuple[str, ...] | None = None,
        feature_branch: str | None = None,
        accepted_commit_to_integrate: str | None = None,
    ) -> dict[str, Any]:
        assert self.project is not None
        ledger, projection, lease = self._resources()
        kernel = WorkflowKernel(
            project=self.project, ledger=ledger, projection=projection, lease=lease,
            interruption_hook=self._hook if use_hook else None,
        )
        events = self._phase_events(ledger, transaction_id) if ledger.path.exists() else []
        names = {event["event_type"] for event in events}
        if not events:
            policy = MutationPolicy(
                allowed_paths, allow_untracked=False, commit_subject=commit_subject,
            )
            if workflow == WorkflowType.MILESTONE_INTEGRATION:
                kernel.begin_for_branch(
                    workflow_type=workflow, target_branch=self.project.milestone_branch,
                    milestone="M0", feature_id=feature_id,
                    run_id=f"sim-{transaction_id[:12]}", policy=policy,
                    transaction_id=transaction_id,
                )
            else:
                kernel.begin(
                    workflow_type=workflow, milestone="M0", feature_id=feature_id,
                    run_id=f"sim-{transaction_id[:12]}", policy=policy,
                    transaction_id=transaction_id,
                )
        else:
            kernel.restore(transaction_id)
        events = self._phase_events(ledger, transaction_id)
        names = {event["event_type"] for event in events}
        if "LeaseAcquired" not in names:
            kernel.acquire_lease()
        if "SnapshotCaptured" not in names:
            if workflow == WorkflowType.MILESTONE_INTEGRATION:
                kernel.prepare_starting_branch()
            kernel.capture_snapshot()
        if "SessionLaunched" not in names:
            kernel.session_launched(f"session-{transaction_id}")
        events = self._phase_events(ledger, transaction_id)
        names = {event["event_type"] for event in events}
        if "SessionResultAccepted" not in names:
            mutate()
            envelope = SessionResultEnvelope.from_dict({
                "schema_version": 1,
                "workflow_type": workflow.value,
                "classification": classification,
                "project_id": "synthetic",
                "repository_identity": ledger.repository_identity,
                "transaction_id": transaction_id,
                "run_id": f"sim-{transaction_id[:12]}",
                "session_id": f"session-{transaction_id}",
                "starting_branch": kernel.transaction.starting_branch,
                "starting_commit": kernel.transaction.starting_head,
                "current_commit": kernel.transaction.starting_head,
                "feature_id": feature_id,
                "changed_paths": list(session_changed_paths if session_changed_paths is not None else allowed_paths),
                "evidence": {
                    "scripted": True, "no_llm": True,
                    "milestone_branch": self.project.milestone_branch,
                    "milestone_head": RepositoryInspector(self.repository).rev_parse(self.project.milestone_branch),
                    "accepted_commit": accepted_commit_to_integrate,
                    **completion_evidence,
                },
                "next_state": next_state,
            })
            kernel.accept_result(envelope)
        events = self._phase_events(ledger, transaction_id)
        if accepted_commit_to_integrate is not None and not any(
            event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "integration_target"
            for event in events
        ):
            kernel.prepare_integration_changes(accepted_commit_to_integrate)
        if not any(event["event_type"] == "ChangesDetected" for event in events):
            kernel.record_file_mutation_boundary()
        events = self._phase_events(ledger, transaction_id)
        names = {event["event_type"] for event in events}
        if "ValidationPassed" not in names:
            kernel.validate(authority=CommandAuthority(), command_results=[])
        events = self._phase_events(ledger, transaction_id)
        names = {event["event_type"] for event in events}
        if "CommitFinalized" not in names:
            if feature_branch is not None:
                kernel.finalize_feature_branch(feature_branch)
            else:
                kernel.finalize()
        events = self._phase_events(ledger, transaction_id)
        names = {event["event_type"] for event in events}
        if not any(name in TERMINAL_EVENT_TYPES for name in names):
            return kernel.complete(evidence=completion_evidence)
        return {"projection": projection.current()}

    def _run_phase(self, **kwargs) -> dict[str, Any]:
        transaction_id = kwargs["transaction_id"]
        try:
            return self._continue_phase(**kwargs, use_hook=True)
        except InjectedInterruption as interruption:
            ledger, projection, lease = self._resources()
            planner = RecoveryPlanner(
                project=self.project, ledger=ledger, projection=projection, lease=lease
            )
            plan = planner.inspect()
            evidence = {
                "interruption": str(interruption),
                "plan": plan.get("classification"),
                "fresh_kernel": True,
            }
            if plan.get("classification") == "resume":
                result = self._continue_phase(**kwargs, use_hook=False)
            elif plan.get("classification") in {
                "commit_succeeded_before_evidence", "finalization_incomplete",
                "terminal_before_lease_release", "projection_update_incomplete",
            }:
                result = planner.apply(allow_current_owner_for_simulation=True)
            elif plan.get("classification") in {"no_transaction", "nothing_to_recover"}:
                result = self._continue_phase(**kwargs, use_hook=False)
            else:
                raise RuntimeError(f"simulated interruption was not recoverable: {plan}")
            evidence["terminal_count"] = sum(
                1 for event in ledger.read()
                if event["transaction_id"] == transaction_id and event["event_type"] in TERMINAL_EVENT_TYPES
            )
            evidence["lease_exists_after_recovery"] = lease.path.exists()
            self.recovery_evidence.append(evidence)
            return result

    def _run_actual_recovery_phase(self, *, transaction_id: str, boundary: str | None) -> dict[str, Any]:
        """Exercise real RecoveryPlanner takeover, not a generic recovery no-op."""

        ledger, projection, lease = self._resources()
        target = WorkflowKernel(
            project=self.project, ledger=ledger, projection=projection, lease=lease
        )
        target.begin(
            workflow_type=WorkflowType.QUEUE_RECONCILIATION,
            milestone=self.project.active_milestone, feature_id=None,
            run_id=f"recovery-target-{transaction_id}",
            policy=MutationPolicy(tuple(), commit_subject="factory: recovery target"),
            transaction_id=transaction_id,
        )
        target.acquire_lease(); target.capture_snapshot()
        injected = False

        def interrupt(observed: str, transaction) -> None:
            nonlocal injected
            if boundary == observed and not injected:
                injected = True
                raise InjectedInterruption(f"{WorkflowType.RECOVERY.value}:{observed}")

        planner = RecoveryPlanner(
            project=self.project, ledger=ledger, projection=projection, lease=lease,
            interruption_hook=interrupt,
        )
        initial_applied = None
        try:
            initial_applied = planner.apply(allow_current_owner_for_simulation=True)
        except InjectedInterruption as interruption:
            self.recovery_evidence.append({
                "interruption": str(interruption), "plan": "actual_recovery_takeover",
                "fresh_kernel": True,
            })
        if initial_applied and initial_applied.get("action") == "resume_exact_transaction":
            resumed = WorkflowKernel(
                project=self.project, ledger=ledger, projection=projection, lease=lease
            )
            resumed.restore(str(initial_applied["transaction_id"]))
            resumed.block(
                state=TransactionState.SUPERSEDED,
                classification="INTERRUPTED_TRANSACTION_SUPERSEDED",
                next_state="queue_reconciliation",
                reference=initial_applied.get("recovery_transaction_id"),
            )
        for _ in range(8):
            plan = RecoveryPlanner(
                project=self.project, ledger=ledger, projection=projection, lease=lease
            ).inspect()
            if plan["classification"] in {"nothing_to_recover", "no_transaction"}:
                break
            applied = RecoveryPlanner(
                project=self.project, ledger=ledger, projection=projection, lease=lease
            ).apply(allow_current_owner_for_simulation=True)
            if applied.get("action") == "resume_exact_transaction":
                resumed = WorkflowKernel(
                    project=self.project, ledger=ledger, projection=projection, lease=lease
                )
                resumed.restore(str(applied["transaction_id"]))
                resumed.block(
                    state=TransactionState.SUPERSEDED,
                    classification="INTERRUPTED_TRANSACTION_SUPERSEDED",
                    next_state="queue_reconciliation",
                    reference=applied.get("recovery_transaction_id"),
                )
        else:
            raise RuntimeError("actual recovery takeover did not converge")
        terminal_count = sum(
            1 for event in ledger.read()
            if event["transaction_id"] == transaction_id
            and event["event_type"] in TERMINAL_EVENT_TYPES
        )
        if boundary is not None:
            evidence = self.recovery_evidence[-1]
            evidence["terminal_count"] = terminal_count
            evidence["lease_exists_after_recovery"] = lease.path.exists()
        return {"terminal_count": terminal_count, "lease_exists": lease.path.exists()}

    def run(
        self,
        *,
        cycles: int = 20,
        interrupt_at: str | None = None,
        interrupt_workflow: WorkflowType | None = None,
        interruption_matrix: bool = False,
    ) -> dict[str, Any]:
        if cycles < 1:
            raise ValueError("the simulator requires at least one feature cycle")
        if interrupt_at is not None and interrupt_at not in INTERRUPTION_BOUNDARIES:
            raise ValueError(f"unknown interruption boundary: {interrupt_at}")
        if not (self.repository / ".git").exists():
            self.initialize(cycles)
        self.inject_boundary = interrupt_at
        self.inject_workflow = interrupt_workflow or WorkflowType.FEATURE_EXECUTION
        self.injected = False
        if interruption_matrix:
            # Order by workflow so each workflow's successive transactions stop
            # at progressively later mutation boundaries.
            cycle_workflows = (
                WorkflowType.QUEUE_RECONCILIATION,
                WorkflowType.FEATURE_PREPARATION,
                WorkflowType.FEATURE_EXECUTION,
                WorkflowType.FEATURE_ACCEPTANCE,
                WorkflowType.MILESTONE_INTEGRATION,
            )
            final_workflows = (
                WorkflowType.MILESTONE_GATE,
                WorkflowType.HUMAN_DECISION_RESOLUTION,
                WorkflowType.RECOVERY,
            )
            self.pending_interruptions = [
                (workflow, boundary)
                for boundary in INTERRUPTION_BOUNDARIES
                for workflow in cycle_workflows
            ] + [
                (workflow, boundary)
                for workflow in final_workflows
                for boundary in INTERRUPTION_BOUNDARIES
            ]
        default_before = _git(self.repository, "rev-parse", "main")
        accepted: list[str] = []
        integrated: list[str] = []
        for number in range(1, cycles + 1):
            feature = f"F{number:03d}"
            queue_path = self.repository / "docs/FEATURE_QUEUE.yaml"

            def plan_mutation(feature_id=feature):
                queue = json.loads(queue_path.read_text(encoding="utf-8"))
                item = next(value for value in queue["features"] if value["id"] == feature_id)
                item["status"] = "ready"
                queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")

            self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:planning")),
                workflow=WorkflowType.QUEUE_RECONCILIATION, feature_id=feature,
                allowed_paths=("docs/FEATURE_QUEUE.yaml",),
                commit_subject=f"factory: plan {feature}", classification="RECONCILED_READY_WORK",
                next_state="feature_ready", mutate=plan_mutation,
                completion_evidence={"selected_feature": feature, "planning_status": "passed"},
            )

            def prepare_mutation(feature_id=feature):
                (self.repository / "preparation.json").write_text(
                    json.dumps({"feature": feature_id}) + "\n", encoding="utf-8"
                )

            self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:preparation")),
                workflow=WorkflowType.FEATURE_PREPARATION, feature_id=feature,
                allowed_paths=(), commit_subject=f"factory: prepare {feature}",
                classification="FEATURE_PREPARED", next_state="feature_preparing",
                mutate=lambda: None, completion_evidence={},
                session_changed_paths=(), feature_branch=f"codex/{feature.lower()}-synthetic",
            )

            def feature_mutation(feature_id=feature):
                path = self.repository / "application.txt"
                value = path.read_text(encoding="utf-8")
                if f"{feature_id}\n" not in value:
                    path.write_text(value + f"{feature_id}\n", encoding="utf-8")

            result = self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:execution")),
                workflow=WorkflowType.FEATURE_EXECUTION, feature_id=feature,
                allowed_paths=("application.txt",), commit_subject=f"{feature}: accepted feature",
                classification="FEATURE_ACCEPTED", next_state="feature_review",
                mutate=feature_mutation, completion_evidence={},
            )
            accepted_commit = _git(self.repository, "rev-parse", "HEAD")
            accepted.append(accepted_commit)

            self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:acceptance")),
                workflow=WorkflowType.FEATURE_ACCEPTANCE, feature_id=feature,
                allowed_paths=(), commit_subject=f"factory: accept {feature}",
                classification="FEATURE_ACCEPTED", next_state="integration_ready",
                mutate=lambda: None,
                completion_evidence={"accepted_feature_commit": accepted_commit, "integration_status": "pending"},
            )

            def integration_mutation(feature_id=feature):
                path = self.repository / "integration.txt"
                value = path.read_text(encoding="utf-8")
                if f"{feature_id}\n" not in value:
                    path.write_text(value + f"{feature_id}\n", encoding="utf-8")

            self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:integration")),
                workflow=WorkflowType.MILESTONE_INTEGRATION, feature_id=feature,
                allowed_paths=("application.txt",), commit_subject=f"factory: integrate {feature}",
                classification="INTEGRATED", next_state="feature_integrated",
                mutate=lambda: None,
                completion_evidence={
                    "accepted_feature_commit": accepted_commit,
                    "integrated_commit": None,
                    "integration_status": "passed",
                },
                session_changed_paths=(), accepted_commit_to_integrate=accepted_commit,
            )
            integrated.append(_git(self.repository, "rev-parse", "HEAD"))

            def post_plan_mutation(feature_id=feature):
                queue = json.loads(queue_path.read_text(encoding="utf-8"))
                item = next(value for value in queue["features"] if value["id"] == feature_id)
                item.update({
                    "status": "integrated", "accepted_commit": accepted_commit,
                    "integrated_commit": integrated[-1], "integration_status": "passed",
                })
                queue["milestones"][0]["integrated_features"].append(feature_id)
                queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")

            self._run_phase(
                transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{feature}:post-planning")),
                workflow=WorkflowType.QUEUE_RECONCILIATION, feature_id=feature,
                allowed_paths=("docs/FEATURE_QUEUE.yaml",),
                commit_subject=f"factory: post-integration plan {feature}",
                classification="RECONCILED_READY_WORK", next_state="feature_ready",
                mutate=post_plan_mutation,
                completion_evidence={"planning_status": "passed"},
            )
        # Exercise the remaining kernel handlers without adding application behavior.
        final_workflows = (
            (WorkflowType.MILESTONE_GATE, "MILESTONE_GATE_PASSED", "milestone_complete"),
            (WorkflowType.HUMAN_DECISION_RESOLUTION, "HUMAN_GATE_RESOLVED", "milestone_complete"),
            (WorkflowType.RECOVERY, "RECOVERY_APPLIED", "milestone_complete"),
        )
        repeats = len(INTERRUPTION_BOUNDARIES) if interruption_matrix else 1
        for workflow, classification, state in final_workflows:
            for repeat in range(repeats):
                final_transaction_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"final:{workflow.value}:{repeat}"))
                if workflow == WorkflowType.RECOVERY:
                    recovery_boundary = (
                        INTERRUPTION_BOUNDARIES[repeat] if interruption_matrix else self.inject_boundary
                    )
                    self._run_actual_recovery_phase(
                        transaction_id=final_transaction_id, boundary=recovery_boundary,
                    )
                    if interruption_matrix and self.pending_interruptions and self.pending_interruptions[0] == (workflow, recovery_boundary):
                        self.pending_interruptions.pop(0)
                else:
                    self._run_phase(
                        transaction_id=final_transaction_id,
                        workflow=workflow, feature_id=None, allowed_paths=(),
                        commit_subject=f"factory: {workflow.value}", classification=classification,
                        next_state=state, mutate=lambda: None, completion_evidence={},
                    )
        ledger, projection, lease = self._resources()
        events = ledger.read()
        audited_commands = _GIT_AUDIT.get(str(self.repository.resolve()), [])
        prohibited = [
            list(command) for command in audited_commands
            if command and (
                command[0] in {"push", "tag", "clean", "reset"}
                or command[:2] in {("remote", "add"), ("remote", "remove")}
                or (command[0] in {"merge", "rebase"} and "main" in command)
            )
        ]
        terminal_counts: dict[str, int] = {}
        integration_transactions: set[str] = set()
        for event in events:
            if event["event_type"] in TERMINAL_EVENT_TYPES:
                terminal_counts[event["transaction_id"]] = terminal_counts.get(event["transaction_id"], 0) + 1
            if event["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value and event["event_type"] == "TransactionCompleted":
                integration_transactions.add(event["transaction_id"])
        return {
            "schema_version": 1,
            "cycles_completed": cycles,
            "accepted_commits": accepted,
            "integrated_commits": integrated,
            "accepted_commit_count": len(set(accepted)),
            "integration_count": len(integration_transactions),
            "duplicate_commits": len(accepted) != len(set(accepted)),
            "duplicate_integration": len(integration_transactions) != cycles,
            "conflicting_terminal_events": any(value != 1 for value in terminal_counts.values()),
            "default_branch_unchanged": default_before == _git(self.repository, "rev-parse", "main"),
            "stale_live_leases": [str(lease.path)] if lease.path.exists() else [],
            "prohibited_git_commands": prohibited,
            "push_attempted": any(command and command[0] == "push" for command in audited_commands),
            "release_or_deployment_attempted": any(
                command and command[0] in {"push", "tag"} for command in audited_commands
            ),
            "tags_created": _git(self.repository, "tag", "--list").splitlines(),
            "remotes": _git(self.repository, "remote").splitlines(),
            "ledger_integrity": ledger.verify().to_dict(),
            "projection": projection.current(),
            "interruption_requested": (
                f"{self.inject_workflow.value}:{interrupt_at}" if interrupt_at else None
            ),
            "interruption_recovered": interrupt_at is None or self.injected,
            "recovery_evidence": self.recovery_evidence,
            "interruption_matrix_complete": not self.pending_interruptions,
        }


def simulate_twenty_feature_milestone() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="conveyor-kernel-simulator-") as temporary:
        return DeterministicLifecycleSimulator(Path(temporary)).run(cycles=20)


def simulate_all_interruptions() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="conveyor-kernel-interruption-matrix-") as temporary:
        simulation = DeterministicLifecycleSimulator(Path(temporary))
        result = simulation.run(
            cycles=len(INTERRUPTION_BOUNDARIES), interruption_matrix=True
        )
        recovered = {
            item["interruption"]: item for item in result["recovery_evidence"]
        }
        cases = [
            {
                "workflow_type": workflow.value,
                "boundary": boundary,
                "recovered": f"{workflow.value}:{boundary}" in recovered,
                "terminal_count": recovered.get(f"{workflow.value}:{boundary}", {}).get("terminal_count"),
                "lease_exists_after_recovery": recovered.get(f"{workflow.value}:{boundary}", {}).get("lease_exists_after_recovery"),
            }
            for workflow in SIMULATED_WORKFLOWS
            for boundary in INTERRUPTION_BOUNDARIES
        ]
        return {
            "schema_version": 1,
            "workflow_count": len(SIMULATED_WORKFLOWS),
            "boundary_count": len(INTERRUPTION_BOUNDARIES),
            "case_count": len(cases),
            "all_recovered": result["interruption_matrix_complete"] and all(item["recovered"] for item in cases),
            "duplicate_commits": result["duplicate_commits"],
            "duplicate_integration": result["duplicate_integration"],
            "conflicting_terminal_events": result["conflicting_terminal_events"],
            "stale_live_leases": result["stale_live_leases"],
            "default_branch_unchanged": result["default_branch_unchanged"],
            "results": cases,
        }
