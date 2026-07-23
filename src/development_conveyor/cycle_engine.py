"""Repository-level execution engine with checkpoints and evidence validation."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .config import Configuration
from .errors import (
    ConveyorError,
    IntegrationPlanError,
    LockError,
    ProjectionError,
    QueueError,
    RecoveryError,
    SchemaValidationError,
    SessionError,
    TransactionError,
)
from .locks import (
    DurableLock,
    PlanningWriterLease,
    RepositoryWriterLease,
    inspect_repository_writer_lock,
    make_lock_record,
)
from .planning import (
    ALLOWED_PLANNING_PREFIXES,
    PLANNING_CLASSIFICATIONS,
    allowed_planning_path,
    authoritative_queue_validation_evidence,
    capture_planning_start,
    compare_queue_validation_evidence,
    finalize_planning_commit,
    inspect_planning_finalization_recovery,
    load_planning_transaction,
    persist_planning_transaction,
    planning_commit_subject,
    planning_report_path,
    stable_fingerprint,
    validate_planning_changes,
    validate_planning_noop,
)
from .logging import EventLogger, JsonStateStore, atomic_write_bytes, atomic_write_json, run_event, utc_now
from .human_resolution import evaluate_human_resolution, gate_fingerprint, resolution_fingerprint
from .queue import FeatureQueue, resolve_queue_path
from .recovery import (
    StartupReconciliation,
    assess_durable_integration_success,
    assess_recovery,
    assess_startup_reconciliation,
)
from .redaction import redact_text
from .registry import Project
from .reporting import build_project_plan
from .repository import RepositoryInspector
from .retries import RetryBudget
from .sessions import (
    SessionLauncher,
    SessionPlan,
    SessionRequest,
    SessionResult,
)
from .state_machine import CYCLE_MACHINE, PORTFOLIO_MACHINE
from .legacy_adapter import LegacyTransitionAdapter
from .contracts import (
    MutationPolicy, SessionResultEnvelope, TransactionState, WorkflowType,
    TERMINAL_STATES, fingerprint,
)
from .kernel import (
    FeatureExecutionAdapter,
    FeatureAcceptanceAdapter,
    HumanDecisionResolutionAdapter,
    FeaturePreparationAdapter,
    MilestoneGateAdapter,
    MilestoneIntegrationAdapter,
    QueueReconciliationAdapter,
    RecoveryAdapter,
    WorkflowKernel,
)
from .ledger import EvidenceLedger
from .projection import ProjectionEngine, build_projection_observations, projection_fingerprint
from .execution_plan import (
    ExecutionPlan,
    authoritative_status_fields,
    bind_projection_to_queue,
    superseded_legacy_cycles,
)
from .integration_executor import (
    build_integration_plan,
    execute_integration_plan,
    inspect_integration_finalization_recovery,
    inspect_two_refs,
    load_integration_plan,
    persist_integration_plan,
    resume_integration_finalization,
)
from .workflow_lease import WorkflowWriterLease
from .command_authority import CommandAuthority
from .cycle_cache import (
    LEGACY_CACHE_BINDING_RECOVERY_FIELD,
    normalize_cycle_cache_for_rebinding,
    validated_canonical_projection_binding,
    write_terminal_cycle_cache,
)
from .consistency import ConsistencyChecker
from .workflow_recovery import RecoveryPlanner
from .validation import SafetyPolicy
from .cost_policy import build_run_plan
from .feature_branches import canonical_feature_branch
from .accepted_commit import (
    acceptance_metadata_paths,
    materialize_acceptance_metadata,
)

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
    def __init__(
        self,
        configuration: Configuration,
        launcher: SessionLauncher | None = None,
        *,
        execution_profile_override: str | None = None,
    ):
        self.configuration = configuration
        self.root = configuration.root
        self.launcher = launcher or SessionLauncher(self.root, configuration.conveyor)
        self.execution_profile_override = execution_profile_override
        self.project_store = JsonStateStore(self.root / "schemas/project-state.schema.json")
        self.cycle_store = JsonStateStore(self.root / "schemas/cycle-state.schema.json")
        self.events = EventLogger(
            configuration.owned_path(configuration.conveyor["log_directory"]) / "run-events.jsonl",
            self.root / "schemas/run-event.schema.json",
        )

    def project_state_path(self, project: Project) -> Path:
        return self.configuration.owned_path(self.configuration.conveyor["state_directory"]) / "projects" / f"{project.project_id}.json"

    @staticmethod
    def _attach_milestone_integration_contract(
        plan: dict[str, Any], project: Project
    ) -> None:
        if plan.get("proposed_next_action") != "milestone_integration" or not plan.get("selected_feature"):
            return
        plan["milestone_integration_contract"] = {
            "executor": "development_conveyor.integration_executor",
            "mutation_command": (
                "scripts/conveyor execute-integration-plan --plan "
                "<absolute-controller-owned-plan-path>"
            ),
            "model_session_required": False,
            "terminal_marker": None,
            "terminal_schema": {
                "schema_version": 1,
                "classifications": [
                    "INTEGRATED",
                    "VALIDATION_FAILED",
                    "SEMANTIC_CONFLICT",
                ],
            },
            "accepted_commit": plan.get("accepted_feature_commit"),
            "starting_commit": (
                (plan.get("execution_plan") or {}).get("starting_commit")
                or (plan.get("repository_state") or {}).get("milestone_branch_head")
            ),
            "expected_branch": project.milestone_branch,
            "transaction_mode": "fresh",
        }

    def _kernel_recovery_preflight(
        self, project: Project, *, apply: bool
    ) -> dict[str, Any] | None:
        """Route incomplete M1 ledger state through the canonical recovery planner."""

        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger_path = state_root / "evidence-ledger.jsonl"
        if not ledger_path.exists():
            return None
        ledger = EvidenceLedger(
            ledger_path, project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        planner = RecoveryPlanner(
            project=project, ledger=ledger, projection=projection, lease=lease
        )
        plan = planner.inspect()
        if plan.get("classification") in {"nothing_to_recover", "no_transaction"}:
            return None
        if not apply:
            return {
                "project_id": project.project_id,
                "outcome": "kernel_recovery_required",
                "kernel_recovery": plan,
            }
        if plan.get("recoverable") is not True:
            return {
                "project_id": project.project_id,
                "outcome": "human_decision_required",
                "kernel_recovery": plan,
            }
        applied = planner.apply()
        if applied.get("action") == "resume_exact_transaction":
            # Production recovery never resumes an old opaque model session.
            # It proves and reacquires the exact transaction, then terminalizes
            # that interrupted attempt so a subsequent command can start a
            # fresh typed transaction without competing authorities.
            recovered_id = str(applied["transaction_id"])
            kernel = WorkflowKernel(
                project=project, ledger=ledger, projection=projection, lease=lease
            )
            recovered = kernel.restore(recovered_id)
            if applied.get("changed_paths") and applied.get("session_result_recorded"):
                envelope = kernel.envelope
                policy = recovered.allowed_mutation_policy
                if envelope is None or not policy.commit_subject:
                    return {
                        "project_id": project.project_id,
                        "outcome": "human_decision_required",
                        "kernel_recovery": applied,
                        "reason": "exact dirty recovery lacks typed result or commit authority",
                    }
                adapter_class = {
                    WorkflowType.QUEUE_RECONCILIATION: QueueReconciliationAdapter,
                    WorkflowType.FEATURE_EXECUTION: FeatureExecutionAdapter,
                    WorkflowType.FEATURE_ACCEPTANCE: FeatureAcceptanceAdapter,
                    WorkflowType.MILESTONE_INTEGRATION: MilestoneIntegrationAdapter,
                    WorkflowType.MILESTONE_GATE: MilestoneGateAdapter,
                    WorkflowType.HUMAN_DECISION_RESOLUTION: HumanDecisionResolutionAdapter,
                    WorkflowType.RECOVERY: RecoveryAdapter,
                }.get(recovered.workflow_type)
                if adapter_class is None:
                    return {
                        "project_id": project.project_id,
                        "outcome": "human_decision_required",
                        "kernel_recovery": applied,
                        "reason": "workflow has no deterministic dirty-recovery adapter",
                    }
                adapter = adapter_class(
                    allowed_paths=policy.allowed_paths,
                    allowed_prefixes=policy.allowed_prefixes,
                    allow_untracked=policy.allow_untracked,
                    commit_subject=policy.commit_subject,
                    next_state=envelope.next_state,
                )
                terminal_state = adapter.terminal_state(envelope)
                if terminal_state is not None:
                    blocked = kernel.block(
                        state=terminal_state,
                        classification=envelope.classification,
                        next_state=envelope.next_state,
                        human_gate=envelope.evidence.get("human_decision"),
                    )
                    applied.update({
                        "action": "terminalize_exact_dirty_result",
                        "terminal_projection": blocked,
                    })
                    return {
                        "project_id": project.project_id,
                        "outcome": "kernel_recovery_applied",
                        "kernel_recovery": applied,
                    }
                if recovered.current_state == TransactionState.VALIDATING:
                    authority, commands = self._kernel_required_commands(project)
                    kernel.validate(
                        authority=authority, command_results=commands,
                        semantic_validator=adapter.semantic_validate,
                    )
                elif recovered.current_state != TransactionState.FINALIZING:
                    return {
                        "project_id": project.project_id,
                        "outcome": "human_decision_required",
                        "kernel_recovery": applied,
                        "reason": "dirty recovery is not at a deterministic validation boundary",
                    }
                commit = kernel.finalize()
                evidence = dict(envelope.evidence)
                if recovered.workflow_type == WorkflowType.MILESTONE_INTEGRATION:
                    evidence.update({
                        "accepted_feature_commit": kernel.prepared_integration_accepted_commit,
                        "integrated_commit": commit,
                        "integration_status": "passed",
                    })
                completion = kernel.complete(evidence=evidence)
                applied.update({
                    "action": "finalize_exact_dirty_transaction",
                    "final_commit": commit,
                    "terminal_projection": completion["projection"],
                })
                return {
                    "project_id": project.project_id,
                    "outcome": "kernel_recovery_applied",
                    "kernel_recovery": applied,
                    "next_action": (
                        f"scripts/conveyor run --project {project.project_id} "
                        f"--mode {project.automation_mode}"
                    ),
                }
            recovery_next_state = {
                WorkflowType.QUEUE_RECONCILIATION: "queue_reconciliation",
                WorkflowType.FEATURE_PREPARATION: "feature_ready",
                WorkflowType.FEATURE_EXECUTION: "feature_ready",
                WorkflowType.FEATURE_ACCEPTANCE: "feature_review",
                WorkflowType.MILESTONE_INTEGRATION: "integration_pending",
                WorkflowType.MILESTONE_GATE: "milestone_gate",
                WorkflowType.HUMAN_DECISION_RESOLUTION: "human_decision_required",
                WorkflowType.RECOVERY: "feature_ready",
            }[recovered.workflow_type]
            terminal = kernel.block(
                state=TransactionState.SUPERSEDED,
                classification="INTERRUPTED_TRANSACTION_SUPERSEDED",
                next_state=recovered.next_project_state or recovery_next_state,
                reference=applied.get("recovery_transaction_id"),
            )
            applied["superseded_projection"] = terminal
            applied["action"] = "supersede_interrupted_transaction"
        return {
            "project_id": project.project_id,
            "outcome": "kernel_recovery_applied",
            "kernel_recovery": applied,
            "next_action": (
                f"scripts/conveyor run --project {project.project_id} "
                f"--mode {project.automation_mode}"
            ),
        }

    @staticmethod
    def _configured_kernel_commands(project: Project) -> tuple[tuple[str, ...], ...]:
        adapter_path = project.repository / ".factory/project.yaml"
        try:
            adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"factory adapter is unreadable for kernel validation: {exc}") from exc
        commands: list[tuple[str, ...]] = []
        configured = adapter.get("commands") if isinstance(adapter, dict) else None
        for category in ("build", "test", "lint", "package", "validate"):
            values = (configured or {}).get(category, []) if isinstance(configured, dict) else []
            if not isinstance(values, list):
                raise RecoveryError(f"adapter command category {category} is not an array")
            for value in values:
                if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                    raise RecoveryError(f"adapter command category {category} contains an invalid argument array")
                command = tuple(value)
                if command not in commands:
                    commands.append(command)
        return tuple(commands)

    @staticmethod
    def _execute_kernel_commands(
        project: Project, commands: tuple[tuple[str, ...], ...]
    ) -> tuple[CommandAuthority, list[Any]]:
        authority = CommandAuthority(configured_required=commands)
        records = []
        for command in commands:
            SafetyPolicy.validate_configured_command(
                list(command), cwd=project.repository,
                registered_repository=project.repository,
            )
            result = subprocess.run(
                list(command), cwd=project.repository, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            records.append(authority.classify(
                command, result.returncode, configured_source=".factory/project.yaml",
                diagnostic=(result.stderr.strip() or result.stdout.strip())[-1000:] or None,
            ))
        return authority, records

    @staticmethod
    def _kernel_required_commands(project: Project) -> tuple[CommandAuthority, list[Any]]:
        return CycleEngine._execute_kernel_commands(
            project, CycleEngine._configured_kernel_commands(project)
        )

    @staticmethod
    def _milestone_gate_adapter(
        project: Project, inspector: RepositoryInspector
    ) -> tuple[MilestoneGateAdapter, tuple[str, ...]]:
        gate_exact_paths = {
            project.queue_location,
            "docs/CURRENT_STATUS.md",
            "docs/FEATURE_CATALOG.md",
            "docs/RUN_LOG.md",
        }
        gate_prefixes = ("docs/milestones", "docs/releases")
        tracked_paths = tuple(sorted(
            path for path in inspector.git(["ls-files", "-z"]).stdout.split("\0")
            if path and (
                path in gate_exact_paths
                or any(
                    path == prefix or path.startswith(prefix + "/")
                    for prefix in gate_prefixes
                )
            )
        ))
        return MilestoneGateAdapter(
            allowed_paths=tracked_paths,
            allowed_prefixes=gate_prefixes,
            allow_untracked=True,
            denied_paths=(".factory/project.yaml", ".factory/approved-content.yaml"),
            denied_prefixes=(".factory", "src", "tests"),
            commit_subject=f"factory: record {project.active_milestone} milestone gate",
            next_state="milestone_ready_for_merge",
        ), tracked_paths

    @staticmethod
    def _route_kernel_result(
        kernel: WorkflowKernel, adapter: Any, envelope: SessionResultEnvelope,
    ) -> dict[str, Any] | None:
        """Accept one envelope and terminalize every non-success disposition."""

        kernel.accept_result(envelope)
        terminal = adapter.terminal_state(envelope)
        if terminal is None:
            return None
        gate = envelope.evidence.get("human_decision") or envelope.evidence.get("gate")
        return kernel.block(
            state=terminal, classification=envelope.classification,
            next_state=envelope.next_state,
            human_gate=gate if isinstance(gate, dict) else None,
        )

    @staticmethod
    def _terminalize_handled_kernel_failure(kernel: WorkflowKernel, adapter: Any, exc: Exception) -> None:
        transaction = kernel.transaction
        if transaction is None or transaction.current_state in TERMINAL_STATES or kernel.final_commit is not None:
            return
        record = kernel.lease.read()
        if record is None or record.transaction_id != transaction.transaction_id:
            return
        kernel.block(
            state=TransactionState.TERMINAL_FAILURE,
            classification=adapter.HANDLED_FAILURE_CLASSIFICATION,
            next_state=adapter.HANDLED_FAILURE_NEXT_STATE, reference=type(exc).__name__,
        )

    def load_project_state(self, project: Project) -> dict[str, Any] | None:
        return self.project_store.read(self.project_state_path(project))

    def effective_project(self, project: Project) -> Project:
        projection = self._authoritative_projection(project)
        if projection is not None:
            return replace(project, current_state=str(projection["current_state"]))
        state = self.load_project_state(project)
        return replace(project, current_state=state["current_state"]) if state else project

    def _authoritative_projection(self, project: Project) -> dict[str, Any] | None:
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger_path = state_root / "evidence-ledger.jsonl"
        if not ledger_path.exists():
            return None
        ledger = EvidenceLedger(
            ledger_path, project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        engine = ProjectionEngine(ledger, state_root / "projection-cache.json")
        rebuilt = engine.rebuild(persist_cache=False)
        cache = engine.load_cache()
        if cache is None:
            raise ProjectionError(
                "authoritative execution requires a projection cache bound to the ledger head"
            )
        if (
            cache.get("ledger_sequence") != rebuilt.get("ledger_sequence")
            or cache.get("ledger_fingerprint") != rebuilt.get("ledger_fingerprint")
        ):
            raise ProjectionError(
                "authoritative execution requires a projection cache bound to the ledger head"
            )
        try:
            queue = FeatureQueue.from_location(
                project.repository, project.queue_location
            )
        except (QueueError, OSError):
            return rebuilt
        return bind_projection_to_queue(
            rebuilt, queue, str(project.active_milestone or "")
        )

    def _execution_plan(
        self, project: Project, projection: dict[str, Any]
    ) -> ExecutionPlan:
        inspector = RepositoryInspector(project.repository)
        active_transaction = next((
            item for item in projection.get("transactions", [])
            if item.get("transaction_id") == projection.get("active_transaction")
        ), None)
        completed_transactions = [
            item for item in projection.get("transactions", [])
            if item.get("state") in {"completed", "superseded", "terminal_failure"}
        ]
        authoritative_snapshot = (
            (active_transaction or {}).get("starting_snapshot")
            if active_transaction is not None
            else (
                (completed_transactions[-1].get("terminal_snapshot") or {})
                if completed_transactions else {}
            )
        ) or {}
        feature_id = projection.get("current_feature") or projection.get("selected_next_feature")
        if (
            projection.get("allowed_next_action") == "milestone_integration"
            and projection.get("selected_feature_starting_commit") is None
        ):
            # Canonical feature execution begins at the milestone base and ends
            # at the accepted feature commit.  Its starting snapshot therefore
            # supplies the exact integration target when no migrated projection
            # fact is present.
            feature_execution = next((
                item for item in reversed(projection.get("transactions", []))
                if item.get("workflow_type") == WorkflowType.FEATURE_EXECUTION.value
                and item.get("feature_id") == feature_id
                and isinstance(item.get("starting_snapshot"), dict)
            ), None)
            if feature_execution is not None:
                authoritative_snapshot = feature_execution["starting_snapshot"]
        feature_branch = None
        try:
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            feature = queue.feature(str(feature_id)) if feature_id else None
            feature_branch = self._expected_feature_branch(project, feature) if feature else None
        except (QueueError, OSError):
            feature_branch = None
        if feature_branch is None and feature_id is not None:
            feature_transaction = next((
                item for item in reversed(projection.get("transactions", []))
                if item.get("workflow_type") == WorkflowType.FEATURE_EXECUTION.value
                and item.get("feature_id") == feature_id
                and isinstance(item.get("starting_snapshot"), dict)
            ), None)
            feature_branch = (
                (feature_transaction or {}).get("starting_snapshot") or {}
            ).get("branch")
        return ExecutionPlan.from_projection(
            projection,
            # A live ref is an observation, not authority.  When the projection
            # does not carry a feature-specific start, bind the next transaction
            # to the last canonical repository snapshot instead of silently
            # accepting whatever commit the ref points to now.
            starting_commit=authoritative_snapshot.get("head"),
            feature_branch=feature_branch,
            milestone_branch=(
                projection.get("milestone_branch")
                or project.milestone_branch
            ),
        )

    def _authoritative_execution_context(
        self, project: Project
    ) -> tuple[dict[str, Any], ExecutionPlan, list[dict[str, Any]]] | None:
        projection = self._authoritative_projection(project)
        if projection is None:
            return None
        projection = self._fresh_failed_integration_projection(project, projection)
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        integrity = ledger.verify()
        if (
            projection.get("ledger_sequence") != integrity.sequence
            or projection.get("ledger_fingerprint") != integrity.fingerprint
        ):
            raise ProjectionError("authoritative projection is not bound to the current ledger head")
        executable = self._execution_plan(project, projection)
        return projection, executable, superseded_legacy_cycles(ledger.read())

    def _fresh_failed_integration_projection(
        self, project: Project, projection: dict[str, Any]
    ) -> dict[str, Any]:
        """Recover one terminal pre-mutation integration as a fresh transaction plan."""

        if (
            projection.get("active_transaction") is not None
            or projection.get("current_state")
            not in {"validation_failed", "human_decision_required"}
        ):
            return projection
        candidate = self._verified_failed_integration_recovery(project, projection)
        if candidate is None:
            return projection
        recovered = dict(projection)
        recovered.update({
            "current_state": "integration_ready",
            "current_feature": candidate["feature_id"],
            "selected_next_feature": candidate["feature_id"],
            "accepted_feature_commit": candidate["accepted_commit"],
            "selected_feature_starting_commit": candidate["starting_commit"],
            "feature_branch": candidate["feature_branch"],
            "milestone_branch": candidate["milestone_branch"],
            "allowed_next_action": "milestone_integration",
            "required_lease": "integration_writer",
            "session_resume_eligible": False,
            "failed_integration_recovery": candidate,
        })
        recovered["projection_fingerprint"] = projection_fingerprint(recovered)
        return recovered

    def _planning_finalization_recovery_plan(
        self,
        project: Project,
        projection: dict[str, Any],
    ) -> dict[str, Any] | None:
        if (
            projection.get("active_transaction") is not None
            or projection.get("current_state") != "validation_failed"
        ):
            return None
        latest = next((item for item in reversed(projection.get("transactions") or [])
            if item.get("workflow_type") == WorkflowType.QUEUE_RECONCILIATION.value
            and item.get("state") == "terminal_failure"
            and item.get("terminal_classification") in {"PLANNING_VALIDATION_FAILED", "PLANNING_SEMANTIC_CONFLICT"}), {})
        if (
            latest.get("workflow_type") != WorkflowType.QUEUE_RECONCILIATION.value
            or latest.get("state") != "terminal_failure"
            or latest.get("terminal_classification") not in {
                "PLANNING_VALIDATION_FAILED", "PLANNING_SEMANTIC_CONFLICT",
            }
        ):
            return None
        original_transaction_id = latest.get("transaction_id")
        run_id = latest.get("run_id")
        if not isinstance(original_transaction_id, str) or not isinstance(run_id, str):
            raise RecoveryError("terminal planning projection lacks recovery identity")
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        )
        planning_path = planning_report_path(report_root, run_id)
        planning_transaction = load_planning_transaction(planning_path)
        if planning_transaction is None:
            raise RecoveryError("terminal planning recovery lacks its persisted transaction")
        session_path = self._report_path(report_root, run_id, "queue_reconciliation.json")
        try:
            session_report = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError("terminal planning recovery lacks its session report") from exc
        recorded_report = planning_transaction.get("reconciliation_report")
        if recorded_report is not None and (
            not isinstance(recorded_report, str) or Path(recorded_report).resolve() != session_path
        ):
            raise RecoveryError("persisted planning transaction points to a different session report")
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        events = ledger.read()
        plan = inspect_planning_finalization_recovery(
            project,
            inspector,
            ledger_events=events,
            projection=projection,
            original_transaction_id=original_transaction_id,
            planning_transaction=planning_transaction,
            session_report=session_report,
            writer_lease_exists=inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ).exists(),
        )
        plan.update({
            "original_ledger_sequence": ledger.verify().sequence,
            "original_ledger_fingerprint": ledger.verify().fingerprint,
            "planning_transaction_path": str(planning_path),
            "session_report_path": str(session_path),
            "planning_transaction_fingerprint": fingerprint(planning_transaction),
            "session_report_fingerprint": fingerprint(session_report),
        })
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def _verified_failed_integration_recovery(
        self, project: Project, projection: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resolve a failed integration only from two independently verified refs."""

        if projection.get("active_transaction") is not None:
            return None
        latest = max(
            projection.get("transactions") or [],
            key=lambda item: int(item.get("last_sequence") or 0),
            default={},
        )
        if (
            latest.get("workflow_type") != WorkflowType.MILESTONE_INTEGRATION.value
            or latest.get("state") not in {"terminal_failure", "human_decision_required"}
        ):
            return None
        failed = next(
            (
                item for item in reversed(projection.get("transactions", []))
                if item.get("workflow_type") == WorkflowType.MILESTONE_INTEGRATION.value
                and item.get("state")
                in {"terminal_failure", "human_decision_required"}
            ),
            None,
        )
        if not isinstance(failed, dict):
            return None
        run_id = failed.get("run_id")
        feature_id = failed.get("feature_id")
        snapshot = failed.get("starting_snapshot")
        if not isinstance(run_id, str) or not isinstance(feature_id, str) or not isinstance(snapshot, dict):
            return None
        report_path = self._report_path(
            self.configuration.owned_path(self.configuration.conveyor["report_directory"]),
            run_id,
            "milestone_integration.json",
        )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        inspector = RepositoryInspector(project.repository)
        milestone_branch = project.milestone_branch
        milestone_head = (
            inspector.rev_parse(milestone_branch, check=False)
            if isinstance(milestone_branch, str) else None
        )

        snapshot_candidates: list[dict[str, Any]] = []
        for branch in inspector.local_branches():
            if branch == milestone_branch:
                continue
            branch_head = inspector.rev_parse(branch, check=False)
            if not isinstance(branch_head, str):
                continue
            parents = inspector.git([
                "rev-list", "--parents", "-n", "1", branch_head,
            ]).stdout.split()
            queue_text = inspector.file_at_commit(branch_head, project.queue_location)
            if queue_text is None:
                continue
            try:
                snapshot_queue = FeatureQueue(json.loads(queue_text))
            except (json.JSONDecodeError, QueueError):
                continue
            snapshot_feature = snapshot_queue.feature(feature_id)
            snapshot_milestone = snapshot_queue.milestone(project.active_milestone or "")
            acceptance = (
                snapshot_feature.get("acceptance")
                if isinstance(snapshot_feature, dict) else None
            )
            snapshot_checks = {
                "feature_status": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("status") == "integration_pending",
                "feature_branch": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("branch") == branch,
                "feature_milestone": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("milestone")
                == (snapshot_milestone or {}).get("id"),
                "integration_base": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("integration_base_commit") == milestone_head,
                "single_direct_parent": parents == [branch_head, milestone_head],
                "accepted_self": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("accepted_commit") == "SELF",
                "integration_pending": isinstance(snapshot_feature, dict)
                and snapshot_feature.get("integration_status") == "pending",
                "acceptance_evidence": isinstance(acceptance, dict)
                and all(
                    acceptance.get(key) is True
                    for key in ("tests_passed", "review_passed", "documentation_current")
                ),
                "milestone_branch": isinstance(snapshot_milestone, dict)
                and snapshot_milestone.get("integration_branch") == milestone_branch,
            }
            if all(snapshot_checks.values()):
                snapshot_candidates.append({
                    "feature_branch": branch,
                    "accepted_commit": branch_head,
                    "snapshot_checks": snapshot_checks,
                })

        if len(snapshot_candidates) != 1:
            return None
        snapshot_candidate = snapshot_candidates[0]
        accepted = snapshot_candidate["accepted_commit"]
        old_sessions = list(failed.get("session_ids") or [])
        report_session = report.get("session_id")
        report_commands = report.get("post_integration_commands") or []
        terminal_marker_found = bool(
            report.get("terminal_marker_found") is True
            or "CONVEYOR_TRANSACTION_RESULT=" in str(report.get("redacted_stdout") or "")
        )
        parsed = report.get("parsed_structured_result")
        parsed_evidence = parsed.get("evidence") if isinstance(parsed, dict) else None
        parsed_gate = (
            parsed_evidence.get("human_decision")
            if isinstance(parsed_evidence, dict) else None
        )
        gate_categories = (
            parsed_gate.get("blocker_categories")
            if isinstance(parsed_gate, dict) else None
        )
        mechanical_gate = (
            failed.get("state") == "human_decision_required"
            and failed.get("terminal_classification") == "HUMAN_DECISION_REQUIRED"
            and report.get("structured_output_validation") == "valid"
            and report.get("result_classification") == "HUMAN_DECISION_REQUIRED"
            and isinstance(parsed, dict)
            and parsed.get("classification") == "HUMAN_DECISION_REQUIRED"
            and parsed.get("changed_paths") == []
            and parsed.get("current_commit") == milestone_head
            and parsed.get("starting_branch") == milestone_branch
            and parsed.get("starting_commit") == milestone_head
            and parsed.get("transaction_id") == failed.get("transaction_id")
            and isinstance(parsed_evidence, dict)
            and parsed_evidence.get("accepted_commit") == accepted
            and isinstance(parsed_gate, dict)
            and parsed_gate.get("transaction_id") == failed.get("transaction_id")
            and sorted(gate_categories or [])
            == ["accepted_state_identity_mismatch", "runtime_ignore_policy"]
        )
        structured_failure = (
            failed.get("state") == "terminal_failure"
            and failed.get("terminal_classification")
            in {"VALIDATION_FAILED", "TERMINAL_INTEGRATION_FAILURE"}
            and report.get("structured_output_validation")
            in {"invalid", "semantic_invalid"}
            and report.get("result_classification") == "structured_output_invalid"
        )
        checks = {
            "terminal_pre_mutation_outcome": structured_failure or mechanical_gate,
            "report_identity": report.get("project_id") == project.project_id
            and report.get("run_id") == run_id
            and report.get("action") == "milestone_integration",
            "report_repository": Path(str(report.get("working_directory") or "")).resolve()
            == project.repository.resolve(),
            "session_identity": len(old_sessions) == 1 and report_session == old_sessions[0],
            "report_accepted_corroborates": report.get("accepted_commit") == accepted,
            "feature_ref_head": inspector.rev_parse(
                snapshot_candidate["feature_branch"], check=False
            ) == accepted,
            "milestone_ref_head": isinstance(milestone_branch, str)
            and snapshot.get("branch") == milestone_branch
            and snapshot.get("head") == milestone_head,
            "known_clean_checkout": (
                inspector.current_branch == milestone_branch
                and inspector.head == milestone_head
            ) or (
                inspector.current_branch == snapshot_candidate["feature_branch"]
                and inspector.head == accepted
            ),
            "repository_clean": inspector.is_clean,
            "no_git_operation": not any(inspector.git_operation_state().values()),
            "no_writer_lease": not inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ).exists(),
            "pre_mutation_failure": terminal_marker_found
            and (structured_failure or mechanical_gate)
            and not any(
                isinstance(item, dict)
                and item.get("category") == "required_evidence_finalization"
                and item.get("exit_code") == 0
                for item in report_commands
            ),
        }
        if not all(checks.values()):
            return None
        return {
            "classification": (
                "fresh_after_terminal_pre_mutation_gate"
                if mechanical_gate
                else "fresh_after_terminal_pre_mutation_failure"
            ),
            "failed_transaction_id": failed.get("transaction_id"),
            "failed_run_id": run_id,
            "failed_session_id": old_sessions[0],
            "feature_id": feature_id,
            "accepted_commit": accepted,
            "accepted_commit_source": "immutable_feature_ref_head",
            "feature_branch": snapshot_candidate["feature_branch"],
            "feature_queue_snapshot": project.queue_location,
            "starting_commit": milestone_head,
            "milestone_branch": milestone_branch,
            "fresh_transaction": True,
            "old_session_resume": False,
            "snapshot_checks": snapshot_candidate["snapshot_checks"],
            "checks": checks,
        }

    def _validate_projected_dispatch(
        self,
        project: Project,
        *,
        workflow_type: WorkflowType,
        expected: ExecutionPlan | None,
        allow_nonstarting_checkout: bool = False,
        allow_unmaterialized_integration_queue: bool = False,
    ) -> tuple[dict[str, Any], ExecutionPlan] | None:
        """Re-read and bind dispatch while the controller launch reservation is held."""

        context = self._authoritative_execution_context(project)
        if context is None:
            if expected is not None:
                raise ProjectionError("authoritative projection disappeared before dispatch")
            return None
        projection, executable, _ = context
        executable.validate_against(projection)
        if executable.workflow != workflow_type:
            raise ProjectionError(
                "authoritative execution plan no longer permits the requested workflow"
            )
        if expected is not None and executable.to_dict() != expected.to_dict():
            raise ProjectionError("execution plan changed before the reserved dispatch")
        if workflow_type in {
            WorkflowType.QUEUE_RECONCILIATION,
            WorkflowType.FEATURE_EXECUTION,
            WorkflowType.MILESTONE_INTEGRATION,
            WorkflowType.MILESTONE_GATE,
        }:
            inspector = RepositoryInspector(project.repository)
            if not executable.milestone_branch or not executable.starting_commit:
                raise ProjectionError("execution plan lacks an exact milestone starting snapshot")
            milestone_head = inspector.rev_parse(executable.milestone_branch, check=False)
            if (
                executable.milestone_branch != project.milestone_branch
                or milestone_head != executable.starting_commit
                or (
                    not allow_nonstarting_checkout
                    and workflow_type != WorkflowType.MILESTONE_INTEGRATION
                    and (
                        inspector.current_branch != executable.milestone_branch
                        or inspector.head != executable.starting_commit
                    )
                )
            ):
                raise ProjectionError(
                    "repository no longer matches the execution plan starting branch and commit"
                )
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            if workflow_type == WorkflowType.FEATURE_EXECUTION:
                selection = queue.select_next(project.active_milestone or "")
                planned_branch = (
                    canonical_feature_branch(project, selection.feature)
                    if selection is not None else None
                )
                if (
                    selection is None
                    or selection.feature_id != executable.feature_id
                    or planned_branch != executable.feature_branch
                ):
                    raise ProjectionError(
                        "ready feature identity or branch no longer matches the execution plan"
                    )
                self._validate_feature_branch_preparation_state(project, inspector, {
                    "feature_branch": executable.feature_branch,
                    "feature_starting_commit": executable.starting_commit,
                    "milestone_branch": executable.milestone_branch,
                    "milestone_pre_integration_commit": executable.starting_commit,
                })
            elif workflow_type == WorkflowType.MILESTONE_INTEGRATION:
                selection = queue.select_integration(project.active_milestone or "")
                recovery = projection.get("failed_integration_recovery")
                verified_recovery = (
                    self._verified_failed_integration_recovery(project, projection)
                    if isinstance(recovery, dict) else None
                )
                if selection is None and allow_unmaterialized_integration_queue:
                    if (
                        inspector.current_branch != executable.feature_branch
                        or inspector.head != executable.accepted_commit
                    ):
                        raise ProjectionError(
                            "accepted feature snapshot no longer matches the integration plan"
                        )
                    return projection, executable
                if selection is None and verified_recovery is not None:
                    recovered_identity = {
                        "feature_id": executable.feature_id,
                        "accepted_commit": executable.accepted_commit,
                        "feature_branch": executable.feature_branch,
                        "starting_commit": executable.starting_commit,
                        "milestone_branch": executable.milestone_branch,
                    }
                    if any(
                        verified_recovery.get(key) != value
                        for key, value in recovered_identity.items()
                    ):
                        raise ProjectionError(
                            "recovered integration refs no longer match the execution plan"
                        )
                    return projection, executable
                if (
                    selection is None
                    or selection.feature_id != executable.feature_id
                    or selection.feature.get("branch") != executable.feature_branch
                ):
                    raise ProjectionError(
                        "integration feature identity or branch no longer matches the execution plan"
                    )
                accepted = executable.accepted_commit
                queue_reference = selection.feature.get("accepted_commit")
                if (
                    not isinstance(accepted, str)
                    or accepted == "SELF"
                    or inspector.rev_parse(accepted, check=False) != accepted
                    or queue_reference not in {"SELF", accepted}
                ):
                    raise ProjectionError(
                        "accepted feature commit no longer matches the execution plan"
                    )
        return projection, executable

    def _project_document(self, project: Project, run_id: str | None, fingerprint: str) -> dict[str, Any]:
        existing = self.load_project_state(project)
        stamp = utc_now()
        if existing:
            if existing.get("project_id") != project.project_id:
                raise RecoveryError("portfolio project state belongs to a different project")
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
            "human_decision_required": (
                {
                    **project.human_decision_gate,
                    "gate_fingerprint": gate_fingerprint(project.human_decision_gate),
                }
                if project.current_state == "human_decision_required"
                and isinstance(project.human_decision_gate, dict)
                else None
            ),
            "human_decision_history": [],
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
        event_human_gate: dict[str, Any] | None = None,
        event_branch: str | None = None,
        event_commit: str | None = None,
        event_command_category: str | None = None,
        event_validation_outcome: str | None = None,
        kernel_owned: bool = False,
        kernel_projection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transition = PORTFOLIO_MACHINE.transition(document["current_state"], target)
        document.update({
            "run_id": run_id,
            "current_state": target,
            "current_feature": feature,
            "last_checkpoint": checkpoint,
            "stop_reason": stop_reason,
            "human_decision_required": human_gate,
            "state_evidence": (
                {
                    **(state_evidence or {}),
                    "kernel_transaction_id": (kernel_projection or {}).get("transaction_id"),
                    "ledger_sequence": (kernel_projection or {}).get("ledger_sequence"),
                    "ledger_fingerprint": (kernel_projection or {}).get("ledger_fingerprint"),
                    "projection_fingerprint": (kernel_projection or {}).get("projection_fingerprint"),
                }
                if kernel_owned else state_evidence
            ),
            "updated_at": utc_now(),
        })
        if kernel_owned:
            self.project_store.write(self.project_state_path(project), document)
        else:
            adapter = LegacyTransitionAdapter(
                controller_root=self.root,
                repository=project.repository,
                project_id=project.project_id,
            )
            adapter.transition(
                run_id=run_id,
                feature_id=feature,
                milestone=project.active_milestone,
                previous_state=transition.previous,
                next_state=transition.current,
                checkpoint=checkpoint,
                mutate=lambda: self.project_store.write(self.project_state_path(project), document),
            )
        self.events.append(run_event(
            run_id=run_id,
            project_id=project.project_id,
            repository_fingerprint=document["repository_fingerprint"],
            source="deterministic_script",
            milestone=project.active_milestone,
            feature=feature,
            branch=event_branch,
            commit=event_commit,
            command_category=event_command_category,
            validation_outcome=event_validation_outcome,
            previous_state=transition.previous,
            next_state=transition.current,
            result=checkpoint,
            stop_reason=stop_reason,
            human_gate=event_human_gate if event_human_gate is not None else human_gate,
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
        return self._project_plan(project, allow_cache_binding_recovery=True)

    def _project_plan(
        self, project: Project, *, allow_cache_binding_recovery: bool
    ) -> dict[str, Any]:
        effective = self.effective_project(project)
        persisted = self.load_project_state(project)
        plan = build_project_plan(effective, self.configuration.conveyor, self.root)
        persisted_evidence = (
            persisted.get("state_evidence") if isinstance(persisted, dict) else None
        )
        authoritative_planning_transaction = (
            persisted_evidence.get("planning_transaction")
            if isinstance(persisted_evidence, dict)
            and isinstance(persisted_evidence.get("planning_transaction"), dict)
            else None
        )
        context = self._authoritative_execution_context(project)
        if context is not None:
            authoritative, executable, superseded = context
            if allow_cache_binding_recovery:
                cache_recovery = self._cache_binding_recovery_plan(project)
                if cache_recovery is not None:
                    cache_recovery.update({
                        "kernel_projection": authoritative,
                        "executable_plan": executable.to_dict(),
                        "superseded_cycles": superseded,
                    })
                    return cache_recovery
            legacy_plan = dict(plan)
            plan.update(authoritative_status_fields(
                authoritative,
                executable,
                legacy_plan=legacy_plan,
                persisted_state=(persisted or {}).get("current_state"),
                superseded_cycles=superseded,
                persisted_projection_fingerprint=(
                    ((persisted or {}).get("state_evidence") or {}).get(
                        "projection_fingerprint"
                    )
                ),
            ))
            plan.update({
                "current_planning_transaction": authoritative_planning_transaction,
                "current_repository_state": {
                    "branch": (plan.get("repository_state") or {}).get("branch"),
                    "head": (plan.get("repository_state") or {}).get("head"),
                    "clean": (plan.get("repository_state") or {}).get("clean"),
                    "git_operations": (plan.get("repository_state") or {}).get("git_operations"),
                    "writer_lease": (plan.get("lock_status") or {}).get("repository_writer"),
                },
                "next_feature_selection": {
                    "selected_feature": plan.get("selected_feature"),
                    "selected_feature_starting_commit": (
                        plan.get("feature_starting_commit")
                        if plan.get("selected_feature") else None
                    ),
                },
            })
            planning_recovery = self._planning_finalization_recovery_plan(
                effective, authoritative
            )
            if planning_recovery is not None:
                plan.update({
                    "current_state": "validation_failed",
                    "workflow_type": planning_recovery["workflow_type"],
                    "transaction_mode": "recovery",
                    "proposed_next_action": "planning_finalization",
                    "next_action": "planning_finalization",
                    "original_transaction_id": planning_recovery["original_transaction_id"],
                    "starting_commit": planning_recovery["starting_commit"],
                    "existing_commit": planning_recovery.get("existing_commit"),
                    "existing_planning_changes": planning_recovery["existing_planning_changes"],
                    "selected_feature": planning_recovery["selected_feature"],
                    "next_feature_selection": {
                        "selected_feature": planning_recovery["selected_feature"],
                        "selected_feature_starting_commit": None,
                    },
                    "planning_finalization_recovery": planning_recovery,
                    "model_sessions_that_would_launch": [],
                    "child_sessions_that_would_launch": [],
                    "sessions_that_would_launch": [],
                    "execution": {"models_planned": 0},
                    "deterministic_only": True,
                    "application_mutation_expected": True,
                    "expected_mutation": planning_recovery["expected_mutation"],
                    "feature_factory_would_launch": False,
                    "milestone_integrator_would_launch": False,
                    "planning_content_regeneration_would_run": False,
                    "expected_stop_condition": (
                        "Finalize the exact existing planning diff without a model session or feature launch."
                    ),
                    "compatibility_preflight": None,
                })
                plan["cost_aware_run_plan"] = build_run_plan(
                    plan, self.root, project=effective,
                    profile_configuration=self.configuration.execution_profiles or None,
                    override_profile=self.execution_profile_override,
                )
                return plan
            self._attach_milestone_integration_contract(plan, project)
            cost_plan = build_run_plan(
                plan, self.root, project=project,
                profile_configuration=self.configuration.execution_profiles or None,
                override_profile=self.execution_profile_override,
            )
            compatibility = self._compatibility_snapshot(
                project,
                str(authoritative.get("allowed_next_action") or "verify_consistency"),
                execution_profile=cost_plan,
            )
            plan["compatibility_preflight"] = compatibility
            plan["cost_aware_run_plan"] = cost_plan
            return plan
        planning_transaction = None
        persisted_evidence = (
            persisted.get("state_evidence") if isinstance(persisted, dict) else None
        )
        if isinstance(persisted_evidence, dict) and isinstance(
            persisted_evidence.get("planning_transaction"), dict
        ):
            planning_transaction = persisted_evidence["planning_transaction"]
        persisted_run_id = str((persisted or {}).get("run_id") or "")
        if planning_transaction is None and SAFE_RUN_ID.fullmatch(persisted_run_id):
            report_root = self.configuration.owned_path(
                self.configuration.conveyor["report_directory"]
            )
            transaction = load_planning_transaction(
                planning_report_path(report_root, persisted_run_id)
            )
            if transaction is not None:
                planning_transaction = transaction
            else:
                reconciliation_report = self._report_path(
                    report_root, persisted_run_id, "queue_reconciliation.json"
                )
                inspector = RepositoryInspector(project.repository)
                if reconciliation_report.is_file() and not inspector.is_clean:
                    try:
                        report = json.loads(reconciliation_report.read_text(encoding="utf-8"))
                        planning_transaction = validate_planning_changes(
                            effective,
                            inspector,
                            report,
                            run_id=persisted_run_id,
                            starting_head=inspector.head,
                        )
                        planning_transaction["status"] = "planning_changes_pending_validation"
                        planning_transaction["planning_commit_status"] = "pending_finalization"
                        planning_transaction["recovery_exact_expectation_recorded"] = False
                    except (OSError, json.JSONDecodeError, ConveyorError) as exc:
                        planning_transaction = {
                            "status": "planning_recovery_gate",
                            "run_id": persisted_run_id,
                            "planning_validation_status": "failed",
                            "error": str(exc),
                            "changed_paths": inspector.tracked_changed_paths(),
                            "diff_fingerprint": inspector.planning_diff_fingerprint(),
                        }
        durable = plan.get("durable_integration_success") or {}
        plan.update({
            "historical_integration": {
                "integration_terminal_classification": (
                    "INTEGRATED" if durable.get("success") else None
                ),
                "historical_integration_success": durable.get("success") is True,
                "historical_integration_terminal_head": durable.get("terminal_commit"),
            },
            "current_planning_transaction": planning_transaction,
            "current_repository_state": {
                "branch": (plan.get("repository_state") or {}).get("branch"),
                "head": (plan.get("repository_state") or {}).get("head"),
                "clean": (plan.get("repository_state") or {}).get("clean"),
                "git_operations": (plan.get("repository_state") or {}).get("git_operations"),
                "writer_lease": (plan.get("lock_status") or {}).get("repository_writer"),
            },
            "next_feature_selection": {
                "selected_feature": plan.get("selected_feature"),
                "selected_feature_starting_commit": (
                    (planning_transaction or {}).get("selected_feature_starting_commit")
                    or (
                        (plan.get("repository_state") or {}).get("milestone_branch_head")
                        if plan.get("selected_feature")
                        else None
                    )
                ),
            },
            "planning_transaction_status": (planning_transaction or {}).get("status"),
            "planning_transaction_run_id": (planning_transaction or {}).get("run_id"),
            "planning_transaction_session_id": (planning_transaction or {}).get("session_id"),
            "planning_start_commit": (planning_transaction or {}).get("planning_start_commit"),
            "planning_result_commit": (planning_transaction or {}).get("planning_result_commit"),
            "planning_changed_paths": (planning_transaction or {}).get("changed_paths", []),
            "planning_diff_fingerprint": (planning_transaction or {}).get("diff_fingerprint"),
            "planning_validation_status": (
                "passed"
                if (planning_transaction or {}).get("status")
                in {"planning_changes_pending_validation", "planning_changes_validated", "planning_changes_committed"}
                else (planning_transaction or {}).get("planning_validation_status")
            ),
            "planning_commit_status": (planning_transaction or {}).get("planning_commit_status"),
            "repository_clean": (plan.get("repository_state") or {}).get("clean"),
        })
        if (
            plan.get("selected_feature")
            and isinstance(planning_transaction, dict)
            and planning_transaction.get("planning_result_commit")
        ):
            plan["feature_starting_commit"] = planning_transaction["planning_result_commit"]
        if (
            isinstance(planning_transaction, dict)
            and planning_transaction.get("status") == "planning_changes_pending_validation"
        ):
            plan["proposed_next_action"] = "planning_finalization"
            plan["next_action"] = "planning_finalization"
            plan["expected_stop_condition"] = (
                "Finalize the exact validated planning transaction before production scheduling."
            )
            plan["sessions_that_would_launch"] = []
            plan["feature_factory_would_launch"] = False
            plan["milestone_integrator_would_launch"] = False
        proposed = plan.get("proposed_next_action")
        self._attach_milestone_integration_contract(plan, project)
        compatibility_action = {
            "queue_reconciliation": "queue_reconciliation",
            "planning_refinement": "queue_reconciliation",
            "milestone_integration": "milestone_integration",
            "milestone_gate": "milestone_gate",
        }.get(str(proposed), "feature_cycle")
        preflight_cost_plan = build_run_plan(
            plan, self.root, project=project,
            profile_configuration=self.configuration.execution_profiles or None,
            override_profile=self.execution_profile_override,
        )
        compatibility = self._compatibility_snapshot(
            project, compatibility_action, execution_profile=preflight_cost_plan
        )
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
        plan["cost_aware_run_plan"] = build_run_plan(
            plan, self.root, project=project,
            profile_configuration=self.configuration.execution_profiles or None,
            override_profile=self.execution_profile_override,
        )
        return plan

    def _reconcile_projection_compatibility_cache(
        self,
        project: Project,
        projection: dict[str, Any],
        executable: ExecutionPlan,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically materialize one idempotent, fingerprint-bound legacy cache."""

        path = self.project_state_path(project)
        existing = self.load_project_state(project)
        stamp = utc_now()
        document = self._project_document(
            replace(project, current_state=str(projection["current_state"])),
            (existing or {}).get("run_id"),
            str(projection["repository_path_fingerprint"]),
        )
        evidence = {
            "source": "evidence_ledger_projection",
            "ledger_sequence": projection["ledger_sequence"],
            "ledger_fingerprint": projection["ledger_fingerprint"],
            "projection_fingerprint": projection["projection_fingerprint"],
            "execution_plan": executable.to_dict(),
        }
        desired = {
            "current_state": projection["current_state"],
            "current_feature": executable.feature_id,
            "last_checkpoint": "kernel_projection_reconciled",
            "stop_reason": None,
            "human_decision_required": projection.get("human_gate"),
            "state_evidence": evidence,
        }
        if existing is not None and all(
            existing.get(key) == value for key, value in desired.items()
        ):
            return existing, False
        document.update(desired)
        document["updated_at"] = stamp
        self.project_store.write(path, document)
        return document, True

    def _compatibility_snapshot(
        self,
        project: Project,
        action: str,
        *,
        execution_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        probe = getattr(self.launcher, "compatibility", None)
        if probe is None:
            return None
        kwargs: dict[str, Any] = {"project_id": project.project_id}
        parameters = inspect.signature(probe).parameters
        if (
            execution_profile
            and execution_profile.get("selected_model")
            and "planned_model" in parameters
        ):
            kwargs.update({
                "planned_model": execution_profile["selected_model"],
                "planned_reasoning": execution_profile["selected_reasoning_effort"],
                "model_plan_source": execution_profile["profile_resolution_source"],
            })
        result = probe(action, **kwargs)
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
            durable = assessment.evidence.get("durable_integration_success")
            if isinstance(durable, dict) and durable.get("success") is True:
                cycle_path = inspector.cycle_state_path()
                cycle = self.cycle_store.read(cycle_path)
                if cycle is None:
                    raise RecoveryError("durable integration finalization requires the preserved repository cycle")
                report_path = self._report_path(
                    self.configuration.owned_path(self.configuration.conveyor["report_directory"]),
                    str(cycle.get("conveyor_run_id") or ""),
                    "milestone_integration.json",
                )
                try:
                    integration_report = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RecoveryError("durable integration report changed or disappeared during finalization") from exc
                revalidated = assess_durable_integration_success(
                    project,
                    cycle,
                    queue,
                    inspector,
                    integration_report,
                    writer_exists=writer.exists,
                )
                if revalidated.get("success") is not True:
                    raise RecoveryError("durable integration evidence changed during finalization")
                history = list(cycle.get("integration_finalization_history") or [])
                fingerprint_source = {
                    "accepted_commit": revalidated.get("accepted_commit"),
                    "integrated_commit": revalidated.get("integrated_commit"),
                    "terminal_commit": revalidated.get("terminal_commit"),
                    "integration_session_id": cycle.get("integration_session_id"),
                }
                finalization_fingerprint = hashlib.sha256(
                    json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if not any(item.get("fingerprint") == finalization_fingerprint for item in history):
                    history.append({
                        "fingerprint": finalization_fingerprint,
                        "finalized_at": utc_now(),
                        "prior_stop_reason": cycle.get("stop_reason"),
                        "prior_failure_classification": cycle.get("failure_classification"),
                        "prior_integration_status": cycle.get("integration_status"),
                        "prior_checkpoint": cycle.get("last_successful_checkpoint"),
                        "terminal_commit": revalidated.get("terminal_commit"),
                        "integrated_commit": revalidated.get("integrated_commit"),
                    })
                cycle.update({
                    "milestone_post_integration_commit": revalidated.get("milestone_post_integration_head"),
                    "integration_status": "passed",
                    "failure_classification": None,
                    "retry_exhausted": False,
                    "next_safe_action": "queue_reconciliation",
                    "stop_reason": None,
                    "human_decision_required": None,
                    "integration_gate": None,
                    "integration_finalization_history": history,
                    "optional_warnings": list(revalidated.get("optional_warnings") or []),
                    "last_verified_git_state": self._git_checkpoint(inspector),
                    "updated_at": utc_now(),
                })
                self._write_cycle_cache(project, cycle_path, cycle, inspector, "durable_integration_finalization")
            for index, target in enumerate(assessment.transition_path[1:], start=1):
                final = index == len(assessment.transition_path) - 1
                document = self._transition_project(
                    project,
                    document,
                    target,
                    run_id=run_id,
                    checkpoint=f"startup_state_reconciliation:{assessment.classification}:{target}",
                    feature=assessment.evidence.get("selected_feature") if target == "feature_ready" else None,
                    stop_reason=(
                        None
                        if final and isinstance(durable, dict) and durable.get("success") is True
                        else (assessment.reason if final else None)
                    ),
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
            "prior_integration_session_ids": [],
            "integration_status": None,
            "integration_gate": None,
            "validated_planning_baseline": None,
            "milestone_gate_session_id": None,
            "created_at": stamp,
            "updated_at": stamp,
        }

    @staticmethod
    def _expected_feature_branch(project: Project, feature: dict[str, Any]) -> str:
        return canonical_feature_branch(project, feature)

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
        self._validate_feature_branch_preparation_state(project, inspector, state)
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
    def _validate_feature_branch_preparation_state(
        project: Project, inspector: RepositoryInspector, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate a fresh branch before a lease, transaction, or model launch."""

        branch = state.get("feature_branch")
        starting = state.get("feature_starting_commit")
        milestone = state.get("milestone_branch")
        milestone_start = state.get("milestone_pre_integration_commit")
        if not all(isinstance(item, str) and item for item in (branch, starting, milestone, milestone_start)):
            raise RecoveryError("feature branch preparation evidence is incomplete")
        milestone_head = inspector.rev_parse(milestone, check=False)
        if milestone_head != milestone_start or milestone_head != starting:
            raise RecoveryError("milestone branch changed before feature branch preparation")
        branch_head = inspector.rev_parse(branch, check=False)
        worktree = inspector.branch_worktree(branch) if branch_head else None
        if branch_head is not None and branch_head != starting:
            raise RecoveryError("existing feature branch does not point to the verified starting commit")
        if worktree not in {None, project.repository.resolve()}:
            raise RecoveryError("feature branch already belongs to another worktree")
        return {
            "branch": branch,
            "branch_head": branch_head,
            "branch_state": (
                "planned_branch_absent_and_ready_for_creation"
                if branch_head is None else "planned_branch_exists_at_starting_commit"
            ),
            "milestone_head": milestone_head,
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
        kernel: WorkflowKernel | None = None,
    ) -> dict[str, Any]:
        previous = state["current_phase"]
        CYCLE_MACHINE.transition(previous, target)
        state["current_phase"] = target
        state["last_successful_checkpoint"] = checkpoint
        state["last_verified_git_state"] = self._git_checkpoint(inspector)
        state["updated_at"] = utc_now()
        if kernel is not None:
            kernel.checkpoint(checkpoint, {"previous_phase": previous, "next_phase": target})
            return state
        self.cycle_store.write(path, state)
        return state

    @staticmethod
    def _bind_cycle_cache(state: dict[str, Any], kernel: WorkflowKernel) -> None:
        projection = kernel.projection.rebuild(persist_cache=True)
        state.update({
            "kernel_transaction_id": kernel.transaction.transaction_id,
            "kernel_ledger_sequence": projection["ledger_sequence"],
            "kernel_ledger_fingerprint": projection["ledger_fingerprint"],
            "kernel_projection_fingerprint": projection["projection_fingerprint"],
        })
        unsigned = dict(state)
        unsigned.pop("kernel_cache_fingerprint", None)
        state["kernel_cache_fingerprint"] = fingerprint(unsigned)

    def _materialize_terminal_cycle_cache(
        self,
        path: Path,
        state: dict[str, Any],
        transaction_id: str,
        completion: dict[str, Any],
        ledger: EvidenceLedger,
    ) -> None:
        write_terminal_cycle_cache(
            path, state, ledger=ledger,
            projection_engine=ProjectionEngine(
                ledger, ledger.path.parent / "projection-cache.json"
            ),
            transaction_id=transaction_id,
            expected_feature=state.get("current_feature"),
            require_semantic_state=False,
        )

    def _write_cycle_cache(
        self,
        project: Project,
        path: Path,
        state: dict[str, Any],
        inspector: RepositoryInspector,
        checkpoint: str,
        kernel: WorkflowKernel | None = None,
    ) -> None:
        """Materialize the compatibility cycle cache after a kernel checkpoint."""
        if kernel is not None:
            kernel.checkpoint(checkpoint, {"cycle_phase": state.get("current_phase")})
            return
        self.cycle_store.write(path, state)

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
        on_session_started: Callable[[str], None] | None = None,
    ) -> SessionResult:
        # The common controller launch boundary enforces child budgets before a
        # repository writer lease, Codex process, or mutation can be reached.
        if request.session_kind == "child" and (request.child_session_budget is None or request.child_session_budget <= 0):
            raise SessionError("child session budget exhausted or prohibited before session, lease, or mutation")
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
            matching_planning_lease = (
                request.action == "queue_reconciliation"
                and recorded_run == request.run_id
                and (
                    (
                        record.get("purpose") == "development-conveyor-planning"
                        and record.get("phase") == "queue_reconciliation"
                    )
                    or (
                        record.get("workflow_type") == "queue_reconciliation"
                        and record.get("lease_type") == "planning_writer"
                        and isinstance(record.get("transaction_id"), str)
                    )
                )
                and record.get("project_id") == request.project.project_id
                and record.get("repository_identity") == inspector.identity()["repository_id"]
                and (record.get("branch") or record.get("starting_branch")) == inspector.current_branch
                and record.get("starting_head") == inspector.head
                and (
                    record.get("repository") is None
                    or Path(str(record.get("repository"))).expanduser().resolve()
                    == request.project.repository.resolve()
                )
            )
            matching_typed_kernel_lease = (
                request.transaction_id is not None
                and record.get("transaction_id") == request.transaction_id
                and record.get("run_id") == request.run_id
                and record.get("project_id") == request.project.project_id
                and record.get("repository_identity") == inspector.identity()["repository_id"]
                and record.get("starting_branch") == request.starting_branch
                and record.get("starting_head") == request.starting_commit
                and record.get("workflow_type") in {
                    "queue_reconciliation", "feature_execution", "milestone_integration", "milestone_gate"
                }
            )
            if not matching_feature_lease and not matching_planning_lease and not matching_typed_kernel_lease:
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
                supports_observer = "on_session_started" in inspect.signature(self.launcher.launch).parameters
                if request.transaction_id is not None and not supports_observer:
                    raise SessionError("typed workflow launcher cannot expose early session identity")
                result = (
                    self.launcher.launch(request, on_session_started=on_session_started)
                    if supports_observer else self.launcher.launch(request)
                )
                if on_session_started is not None and not supports_observer:
                    if not result.session_id:
                        raise SessionError("session launch returned no observable session identity")
                    on_session_started(result.session_id)
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
                compatibility = self._compatibility_snapshot(
                    request.project,
                    request.action,
                    execution_profile={
                        "selected_model": request.planned_model,
                        "selected_reasoning_effort": request.planned_reasoning,
                        "profile_resolution_source": request.model_plan_source,
                    },
                ) or {
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
        compatibility = self._compatibility_snapshot(
            request.project,
            request.action,
            execution_profile={
                "selected_model": request.planned_model,
                "selected_reasoning_effort": request.planned_reasoning,
                "profile_resolution_source": request.model_plan_source,
            },
        )
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
            "terminal_marker_found": False,
            "parsed_structured_result": None,
            "structured_output_errors": [str(error)],
            "failed_semantic_checks": [],
            "redacted_stdout": "",
            "redacted_stderr": str(error),
            "redacted_stderr_summary": str(error)[-2000:],
            "session_id": None,
            "accepted_commit": request.accepted_commit,
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
            "planned_model": request.planned_model,
            "planned_reasoning": request.planned_reasoning,
            "launched_model": getattr(plan, "launched_model", None) if "plan" in locals() else None,
            "launched_reasoning": getattr(plan, "launched_reasoning", None) if "plan" in locals() else None,
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
            "feature_cycle": "direct-feature-session",
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
            "terminal_marker_found": result.terminal_marker_found,
            "parsed_structured_result": result.parsed_structured_result,
            "structured_output_errors": list(result.structured_output_errors),
            "failed_semantic_checks": list(result.failed_semantic_checks),
            "redacted_stdout": result.redacted_stdout or result.redacted_output,
            "redacted_stderr": result.redacted_stderr,
            "redacted_stderr_summary": (result.redacted_stderr or "")[-2000:],
            "session_id": result.session_id,
            "accepted_commit": request.accepted_commit,
            "current_state": current_state,
            "failure_classification": result.failure_classification,
            "retryable": result.retryable,
            "primary_terminal_error": result.primary_terminal_error,
            "secondary_diagnostics": list(result.secondary_diagnostics),
            "post_integration_commands": list(result.post_integration_commands),
            "optional_warnings": list(result.optional_warnings),
            "effective_model": result.plan.effective_model,
            "effective_reasoning": result.plan.effective_reasoning,
            "planned_model": result.plan.planned_model,
            "planned_reasoning": result.plan.planned_reasoning,
            "launched_model": result.plan.launched_model,
            "launched_reasoning": result.plan.launched_reasoning,
            "collaboration_tools_removed": result.plan.collaboration_tools_removed,
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
        run_directory = report_root / run_id
        if run_directory.is_symlink():
            raise SessionError("report run directory must not be a symbolic link")
        candidate = run_directory / filename
        if candidate.is_symlink():
            raise SessionError("report path must not be a symbolic link")
        if candidate.exists() and not candidate.is_file():
            raise SessionError("report path must be a regular file")
        path = candidate.resolve(strict=False)
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
        if result.result_classification in {
            "session_execution_failed", "structured_output_invalid",
            "RETRYABLE_PLANNING_FAILURE", "PLANNING_VALIDATION_FAILED",
        }:
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
        on_session_started: Callable[[str], None] | None = None,
    ) -> tuple[SessionResult, list[dict[str, Any]], dict[str, Any]]:
        configured = int(self.configuration.conveyor["retries"][retry_key])
        limit = request.project.maximum_retries if request.project.maximum_retries is not None else configured
        budget = RetryBudget(limit)
        attempts: list[dict[str, Any]] = []
        result = self._launch_session(
            request, inspector, phase, reservation_held=reservation_held,
            on_session_started=on_session_started,
        )
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
            ), inspector, phase, reservation_held=reservation_held,
                on_session_started=on_session_started)
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

    def _integration_human_gate(
        self,
        project: Project,
        inspector: RepositoryInspector,
        state: dict[str, Any],
        result: SessionResult,
    ) -> dict[str, Any]:
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        feature = queue.feature(str(state.get("current_feature") or "")) or {}
        descriptor = (
            (result.structured_result or {}).get("human_decision")
            if isinstance(result.structured_result, dict) else None
        )
        if not isinstance(descriptor, dict):
            raise RecoveryError("live integration human gate lacks a validated structured blocker descriptor")
        accepted = state.get("accepted_feature_commit")
        milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False)
        descriptor_fingerprint = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        gate = {
            "gate_id": f"{project.project_id}-{state.get('current_feature')}-integration-{descriptor_fingerprint[:12]}",
            "classification": descriptor["gate_classification"],
            "reason": descriptor["reason"],
            "project_id": project.project_id,
            "expected_repository_id": inspector.identity()["repository_id"],
            "expected_path_fingerprint": inspector.identity()["path_fingerprint"],
            "expected_milestone": project.active_milestone,
            "approved_next_state": "queue_reconciliation",
            "feature_id": state.get("current_feature"),
            "feature": state.get("current_feature"),
            "feature_branch": state.get("feature_branch"),
            "accepted_commit": accepted,
            "accepted_feature_commit": accepted,
            "feature_starting_commit": state.get("feature_starting_commit"),
            "milestone_branch": project.milestone_branch,
            "milestone_head": milestone_head,
            "queue_feature_status": feature.get("status"),
            "queue_accepted_commit": feature.get("accepted_commit"),
            "integration_session_id": result.session_id,
            "integration_report_path": result.report_path,
            "integration_terminal_classification": "HUMAN_DECISION_REQUIRED",
            "integration_runtime_location": ".factory/runtime/milestone-integration",
            "blocker_categories": list(descriptor["blocker_categories"]),
            "blocker_descriptor": descriptor,
            "blocker_descriptor_fingerprint": descriptor_fingerprint,
            "retryable": False,
            "ordinary_resume_allowed": False,
            "safe_continuation_command": (
                f"scripts/conveyor reconcile --project {project.project_id} "
                "--resolve-human-decision --reason <approved-reason>"
            ),
            "resolved": False,
        }
        return gate

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
        if (
            result.action == "milestone_integration"
            and result.result_classification == "HUMAN_DECISION_REQUIRED"
            and result.structured_output_validation == "valid"
        ):
            gate = self._integration_human_gate(project, inspector, state, result)
            state.update({
                "failure_classification": "HUMAN_DECISION_REQUIRED",
                "retry_exhausted": True,
                "next_safe_action": gate["safe_continuation_command"],
                "stop_reason": gate["reason"],
                "human_decision_required": gate,
                "integration_gate": gate,
                "integration_status": "blocked",
            })
            self._advance_cycle(
                cycle_path, state, "human_decision_required", inspector,
                "integration_terminal_human_decision_required",
            )
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            if writer.exists:
                raise LockError("terminal integration human gate retained a repository writer lease")
            project_state = self._project_document(
                project, str(state["conveyor_run_id"]), inspector.identity()["path_fingerprint"]
            )
            if project_state["current_state"] != "human_decision_required":
                if project_state["current_state"] in {"feature_running", "feature_accepted", "integration_pending", "integration_ready", "integrating"}:
                    self._transition_project(
                        project, project_state, "integration_blocked",
                        run_id=str(state["conveyor_run_id"]),
                        checkpoint="integration_blocked",
                        feature=str(state.get("current_feature")),
                        stop_reason=gate["reason"],
                        state_evidence={"integration_gate": gate},
                    )
                self._transition_project(
                    project, project_state, "human_decision_required",
                    run_id=str(state["conveyor_run_id"]),
                    checkpoint="integration_terminal_human_decision_required",
                    feature=str(state.get("current_feature")),
                    stop_reason=gate["reason"],
                    human_gate=gate,
                    state_evidence={"integration_gate": gate},
                )
            return {
                "outcome": "human_decision_required",
                "feature": state.get("current_feature"),
                "accepted_commit": accepted if (accepted := state.get("accepted_feature_commit")) else None,
                "human_decision": gate,
                "ordinary_resume_allowed": False,
                "retryable": False,
            }
        if result.action == "milestone_integration" and result.result_classification in {
            "VALIDATION_FAILED", "SEMANTIC_CONFLICT", "RETRYABLE_INTEGRATION_FAILURE",
            "TERMINAL_INTEGRATION_FAILURE",
        }:
            classification = str(result.result_classification)
            semantic = classification == "SEMANTIC_CONFLICT"
            retryable = bool(
                classification == "RETRYABLE_INTEGRATION_FAILURE"
                and result.retryable
                and result.retry_hypothesis
                and result.remediation_action
                and result.retry_evidence
                and not retry_status.get("exhausted")
            )
            outcome = {
                "VALIDATION_FAILED": "integration_validation_failed",
                "SEMANTIC_CONFLICT": "human_decision_required",
                "RETRYABLE_INTEGRATION_FAILURE": (
                    "retryable_integration_failure" if retryable else "terminal_integration_failure"
                ),
                "TERMINAL_INTEGRATION_FAILURE": "terminal_integration_failure",
            }[classification]
            continuation = (
                f"scripts/conveyor resume --project {project.project_id}"
                if retryable else f"scripts/conveyor status --project {project.project_id}"
            )
            state.update({
                "failure_classification": classification,
                "retry_exhausted": not retryable,
                "integration_status": "blocked" if semantic else "failed",
                "next_safe_action": continuation,
                "stop_reason": outcome,
            })
            project_state = self._project_document(
                project, str(state["conveyor_run_id"]), inspector.identity()["path_fingerprint"]
            )
            if semantic:
                gate = {
                    "gate_id": f"{project.project_id}-{state.get('current_feature')}-semantic-conflict-{str(result.session_id or 'unknown')}",
                    "classification": "semantic_integration_conflict",
                    "reason": "Milestone integration reported a semantic conflict that requires explicit human direction.",
                    "project_id": project.project_id,
                    "feature_id": state.get("current_feature"),
                    "accepted_feature_commit": state.get("accepted_feature_commit"),
                    "feature_starting_commit": state.get("feature_starting_commit"),
                    "integration_session_id": result.session_id,
                    "retryable": False,
                    "ordinary_resume_allowed": False,
                    "safe_continuation_command": continuation,
                    "resolved": False,
                }
                state["human_decision_required"] = gate
                state["integration_gate"] = gate
                self._advance_cycle(cycle_path, state, "human_decision_required", inspector, "integration_semantic_conflict")
                if project_state["current_state"] != "human_decision_required":
                    if project_state["current_state"] in {"feature_running", "feature_accepted", "integration_pending", "integration_ready", "integrating"}:
                        project_state = self._transition_project(
                            project, project_state, "integration_blocked", run_id=str(state["conveyor_run_id"]),
                            checkpoint="integration_semantic_conflict_blocked", feature=str(state.get("current_feature")),
                        )
                    self._transition_project(
                        project, project_state, "human_decision_required", run_id=str(state["conveyor_run_id"]),
                        checkpoint="integration_semantic_conflict", feature=str(state.get("current_feature")),
                        human_gate=gate, stop_reason=gate["reason"],
                    )
                return {"outcome": outcome, "human_decision": gate, "retryable": False}
            self._advance_cycle(cycle_path, state, "failed", inspector, f"integration_terminal_{classification.lower()}")
            if project_state["current_state"] != "validation_failed":
                self._transition_project(
                    project, project_state, "validation_failed", run_id=str(state["conveyor_run_id"]),
                    checkpoint=f"integration_terminal_{classification.lower()}",
                    feature=str(state.get("current_feature")), stop_reason=outcome,
                    state_evidence={"integration_terminal_classification": classification, "retryable": retryable},
                )
            return {"outcome": outcome, "retryable": retryable, "classification": classification}
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
            self._write_cycle_cache(project, cycle_path, state, inspector, "writer_lease_released")
            if state["current_phase"] == "integration_pending":
                self._advance_cycle(
                    cycle_path, state, "integrating", inspector, "integration_session_launch"
                )
            inspector.ensure_runtime_ignored()
            integration_result, integration_repairs, integration_retry = self._launch_with_retries(SessionRequest(
                action="milestone_integration",
                project=project,
                run_id=state["conveyor_run_id"],
                mode="resume",
                feature=str(state["current_feature"]),
            ), inspector, "integrating", "integration_repairs", reservation_held=reservation_held)
            state["integration_attempts"].extend(integration_repairs)
            self._write_cycle_cache(project, cycle_path, state, inspector, "integration_repairs_recorded")
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
        expected_execution_plan: ExecutionPlan | None = None,
        expected_feature_id: str | None = None,
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        authoritative_context = self._authoritative_execution_context(project)
        if authoritative_context is not None:
            projected, executable, _ = authoritative_context
            if executable.workflow_type != WorkflowType.FEATURE_EXECUTION.value:
                raise ProjectionError("authoritative execution plan does not permit a feature session")
            executable.validate_against(projected)
            if (
                expected_execution_plan is not None
                and expected_execution_plan.to_dict() != executable.to_dict()
            ):
                raise ProjectionError("feature execution plan changed before preflight")
            expected_execution_plan = executable
        bound_feature_id = (
            expected_execution_plan.feature_id
            if expected_execution_plan is not None else expected_feature_id
        )
        if not isinstance(bound_feature_id, str) or not bound_feature_id:
            raise ProjectionError("feature execution lacks an authoritative selected feature")
        if expected_feature_id is not None and expected_feature_id != bound_feature_id:
            raise ProjectionError(
                "queue selection disagrees with the authoritative feature identity"
            )
        if not inspector.is_clean:
            raise ConveyorError("feature execution requires a clean repository")
        cost_plan = build_run_plan(
            {
                "proposed_next_action": "feature_cycle",
                "selected_feature": bound_feature_id,
                "application_mutation_expected": True,
            },
            self.root,
            project=project,
            profile_configuration=self.configuration.execution_profiles or None,
            override_profile=self.execution_profile_override,
        )
        compatibility = self._compatibility_snapshot(
            project, "feature_cycle", execution_profile=cost_plan
        )
        if compatibility is not None and compatibility.get("compatible") is not True:
            if expected_execution_plan is not None:
                compatibility_reservation = self._launch_lock(project, inspector)
                compatibility_reservation.acquire(make_lock_record(
                    project_id=project.project_id,
                    repository_identity=inspector.identity()["repository_id"],
                    run_id=run_id,
                    current_feature=expected_execution_plan.feature_id,
                    current_phase="feature_compatibility_preflight",
                ))
                try:
                    self._validate_projected_dispatch(
                        project,
                        workflow_type=WorkflowType.FEATURE_EXECUTION,
                        expected=expected_execution_plan,
                    )
                finally:
                    compatibility_reservation.release(run_id)
                raise SessionError(
                    f"classification={compatibility.get('classification')}; "
                    f"remediation={compatibility.get('remediation')}; "
                    f"continue={compatibility.get('validation_command')}"
                )
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
            identity = inspector.identity()
            phase_root = self.root / "state/projects" / project.project_id
            compatibility_ledger = EvidenceLedger(
                phase_root / "evidence-ledger.jsonl", project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            compatibility_adapter = FeaturePreparationAdapter(
                allowed_paths=(), commit_subject="factory: compatibility gate",
                next_state="feature_preparing",
            )
            compatibility_kernel = WorkflowKernel(
                project=project, ledger=compatibility_ledger,
                projection=ProjectionEngine(
                    compatibility_ledger, phase_root / "projection-cache.json"
                ),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            compatibility_transaction = compatibility_kernel.begin(
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                milestone=project.active_milestone,
                feature_id=project_state.get("current_feature"), run_id=run_id,
                policy=compatibility_adapter.policy,
            )
            compatibility_kernel.acquire_lease()
            compatibility_kernel.capture_snapshot()
            blocked_projection = compatibility_kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required", human_gate=gate,
            )
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
                kernel_owned=True,
                kernel_projection={
                    "transaction_id": compatibility_transaction.transaction_id,
                    **blocked_projection,
                },
            )
            raise SessionError(
                f"classification={compatibility.get('classification')}; remediation={compatibility.get('remediation')}; "
                f"continue={compatibility.get('validation_command')}"
            )
        reservation = None if reservation_held else self._launch_lock(project, inspector)
        feature_writer_acquired = False
        session_attempted = False
        phase_kernels: list[tuple[WorkflowKernel, Any]] = []
        if reservation is not None:
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id=run_id,
                current_feature=None,
                current_phase="feature_preflight",
            ))
        try:
            self._validate_projected_dispatch(
                project,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                expected=expected_execution_plan,
            )
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
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            selection = queue.select_next(project.active_milestone or "")
            if selection is None:
                raise QueueError("no dependency-ready feature exists after queue reconciliation")
            if selection.feature_id != bound_feature_id:
                raise ProjectionError(
                    "queue selection disagrees with the authoritative feature identity"
                )
            cycle_path = inspector.cycle_state_path()
            state = self._new_cycle_state(
                project, run_id, inspector, selection.feature, compatibility=compatibility
            )
            state["superseded_cycle_archive"] = None
            self._validate_cycle_launch_invariants(project, inspector, state, selection.feature_id)
            identity = inspector.identity()
            phase_root = self.root / "state/projects" / project.project_id
            phase_ledger = EvidenceLedger(
                phase_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            preparation = FeaturePreparationAdapter(
                allowed_paths=(),
                commit_subject=f"factory: prepare {selection.feature_id}",
                next_state="feature_preparing",
            )
            preparation_kernel = WorkflowKernel(
                project=project,
                ledger=phase_ledger,
                projection=ProjectionEngine(phase_ledger, phase_root / "projection-cache.json"),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            phase_kernels.append((preparation_kernel, preparation))
            preparation_transaction = preparation_kernel.begin(
                workflow_type=WorkflowType.FEATURE_PREPARATION,
                milestone=project.active_milestone,
                feature_id=selection.feature_id,
                run_id=run_id,
                policy=preparation.policy,
                expected_starting_branch=(
                    expected_execution_plan.milestone_branch
                    if expected_execution_plan is not None else None
                ),
                expected_starting_head=(
                    expected_execution_plan.starting_commit
                    if expected_execution_plan is not None else None
                ),
            )
            preparation_kernel.acquire_lease()
            state["superseded_cycle_archive"] = (
                self._archive_superseded_cycle(project_state, cycle_path, run_id)
                if cycle_path.exists() else None
            )
            preparation_kernel.capture_snapshot()
            inspector.ensure_runtime_ignored()
            preparation_kernel.checkpoint("runtime_ignore_verified")
            self._write_cycle_cache(
                project, cycle_path, state, inspector, "cycle_initialized", kernel=preparation_kernel
            )
            self._advance_cycle(
                cycle_path, state, "preflight", inspector, "preflight_verified", kernel=preparation_kernel
            )
            self._advance_cycle(
                cycle_path, state, "feature_selected", inspector, selection.reason, kernel=preparation_kernel
            )
            self._advance_cycle(
                cycle_path, state, "branch_preparing", inspector,
                "feature_branch_preparation_started", kernel=preparation_kernel,
            )
            deterministic_session = f"deterministic-feature-preparation:{preparation_transaction.transaction_id}"
            preparation_kernel.session_launched(deterministic_session)
            preparation_envelope = SessionResultEnvelope.from_dict({
                "schema_version": 1,
                "workflow_type": WorkflowType.FEATURE_PREPARATION.value,
                "classification": "FEATURE_PREPARED",
                "project_id": project.project_id,
                "repository_identity": identity["repository_id"],
                "transaction_id": preparation_transaction.transaction_id,
                "run_id": run_id,
                "session_id": deterministic_session,
                "starting_branch": preparation_transaction.starting_branch,
                "starting_commit": preparation_transaction.starting_head,
                "current_commit": preparation_transaction.starting_head,
                "feature_id": selection.feature_id,
                "changed_paths": [],
                "evidence": {"deterministic_operation": "feature_branch_preparation"},
                "next_state": "feature_preparing",
            })
            if self._route_kernel_result(preparation_kernel, preparation, preparation_envelope) is not None:
                raise RecoveryError("deterministic feature preparation did not authorize success")
            preparation_kernel.record_file_mutation_boundary()
            preparation_kernel.validate(
                authority=CommandAuthority(), command_results=(),
                semantic_validator=preparation.semantic_validate,
            )
            preparation_kernel.finalize_feature_branch(str(state["feature_branch"]))
            preparation_completion = preparation_kernel.complete(
                evidence={"prepared_branch": state["feature_branch"]}
            )
            self._materialize_terminal_cycle_cache(
                cycle_path, state, preparation_transaction.transaction_id,
                preparation_completion, phase_ledger,
            )
            branch_evidence = self._verify_feature_branch_runtime(
                project, inspector, state, require_starting_head=True
            )
            state["session_completion_evidence"] = {"prelaunch_branch": branch_evidence}
            tracked_paths = tuple(sorted(path for path in inspector.git(["ls-files", "-z"]).stdout.split("\0") if path))
            denied_feature_paths = tuple(sorted({
                project.autonomy_contract_location,
                project.validation_source,
                ".factory/approved-content.yaml",
                ".factory/conveyor-state.json",
                ".factory/locks/writer.json",
            }))
            denied_feature_prefixes = (".factory", "factory-integration")
            feature_allowed_paths = tuple(
                path for path in tracked_paths
                if path not in denied_feature_paths
                and not any(
                    path == prefix or path.startswith(prefix + "/")
                    for prefix in denied_feature_prefixes
                )
            )
            if project.queue_location not in feature_allowed_paths:
                raise RecoveryError(
                    "feature execution must authorize its tracked acceptance queue snapshot"
                )
            top_level_prefixes = tuple(sorted(
                item.name for item in project.repository.iterdir()
                if item.is_dir()
                and item.name not in {".git", *denied_feature_prefixes}
            ))
            feature_adapter = FeatureExecutionAdapter(
                allowed_paths=feature_allowed_paths,
                allowed_prefixes=top_level_prefixes,
                denied_paths=denied_feature_paths,
                denied_prefixes=denied_feature_prefixes,
                allow_untracked=True,
                commit_subject=f"{selection.feature_id}: {selection.title}",
                next_state="feature_accepted",
            )
            feature_kernel = WorkflowKernel(
                project=project, ledger=phase_ledger,
                projection=ProjectionEngine(phase_ledger, phase_root / "projection-cache.json"),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            phase_kernels.append((feature_kernel, feature_adapter))
            feature_transaction = feature_kernel.begin(
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                milestone=project.active_milestone,
                feature_id=selection.feature_id,
                run_id=run_id,
                policy=feature_adapter.policy,
            )
            if feature_transaction.feature_id != bound_feature_id:
                raise TransactionError("kernel feature identity differs from the execution plan")
            feature_kernel.acquire_lease()
            feature_kernel.capture_snapshot()
            if (
                cost_plan["execution"]["models_planned"] < 1
                or cost_plan["execution"]["models_planned"] != cost_plan["parent_session_budget"]
                or not cost_plan.get("selected_model")
                or not cost_plan.get("selected_reasoning_effort")
            ):
                raise SessionError("feature execution requires one authoritative model execution plan")
            request = SessionRequest(
                action="feature_cycle", project=project, run_id=run_id, mode=mode,
                feature=selection.feature_id,
                transaction_id=feature_transaction.transaction_id,
                repository_identity=identity["repository_id"],
                starting_branch=feature_transaction.starting_branch,
                starting_commit=feature_transaction.starting_head,
                allowed_paths=feature_allowed_paths,
                parent_session_budget=int(cost_plan["parent_session_budget"]),
                child_session_budget=int(cost_plan["child_session_budget"]),
                planned_model=str(cost_plan["selected_model"]),
                planned_reasoning=str(cost_plan["selected_reasoning_effort"]),
                model_plan_source=str(cost_plan["profile_resolution_source"]),
                context_files=tuple(cost_plan["context_pack"]["files"]),
            )
            if request.feature != bound_feature_id:
                raise SessionError("session request feature differs from the execution plan")
            result, repairs, retry_status = self._launch_with_retries(
                request, inspector, "feature_in_progress", "implementation_repairs",
                reservation_held=True, on_session_started=feature_kernel.session_launched,
            )
            state["feature_session_id"] = result.session_id
            state["session_id"] = result.session_id
            state["validation_attempts"].extend(repairs)
            if result.transaction_envelope is None:
                message = self._session_failure_message(project, run_id, result, "feature_in_progress")
                gate = {
                    "reason": "repository session cannot continue safely",
                    "project": project.project_id, "run_id": run_id,
                    "feature": selection.feature_id,
                    "classification": result.failure_classification or result.result_classification,
                    "retryable": bool(result.retryable and not retry_status["exhausted"]),
                    "attempts_consumed": retry_status["attempts_consumed"],
                    "attempts_remaining": retry_status["attempts_remaining"],
                    "retry_exhausted": retry_status["exhausted"],
                    "safe_continuation_command": f"scripts/conveyor resume --project {project.project_id}",
                    "resolved": False,
                }
                state.update({
                    "current_phase": "human_decision_required",
                    "last_successful_checkpoint": "session_terminal_failure",
                    "last_verified_git_state": self._git_checkpoint(inspector),
                    "failure_classification": gate["classification"],
                    "retry_exhausted": retry_status["exhausted"],
                    "next_safe_action": gate["safe_continuation_command"],
                    "stop_reason": message, "human_decision_required": gate,
                    "updated_at": utc_now(),
                })
                terminal_projection = feature_kernel.block(
                    state=TransactionState.HUMAN_DECISION_REQUIRED,
                    classification="HUMAN_DECISION_REQUIRED",
                    next_state="human_decision_required", human_gate=gate,
                )
                self._materialize_terminal_cycle_cache(
                    cycle_path,
                    state,
                    feature_transaction.transaction_id,
                    {"projection": terminal_projection},
                    phase_ledger,
                )
                self._transition_project(
                    project, project_state, "human_decision_required", run_id=run_id,
                    checkpoint="session_terminal_failure", feature=selection.feature_id,
                    stop_reason=message, human_gate=gate, kernel_owned=True,
                    kernel_projection={
                        "transaction_id": feature_transaction.transaction_id,
                        **terminal_projection,
                    },
                )
                raise SessionError(message)
            feature_envelope = SessionResultEnvelope.from_dict(result.transaction_envelope)
            feature_blocked = self._route_kernel_result(feature_kernel, feature_adapter, feature_envelope)
            if feature_blocked is not None:
                self._transition_project(
                    project, project_state, feature_envelope.next_state, run_id=run_id,
                    checkpoint="kernel_feature_terminal", feature=selection.feature_id,
                    stop_reason=feature_envelope.classification,
                    human_gate=feature_envelope.evidence.get("human_decision"),
                    kernel_owned=True,
                    kernel_projection={"transaction_id": feature_transaction.transaction_id, **feature_blocked},
                )
                return {"outcome": feature_envelope.next_state, "classification": feature_envelope.classification}
            feature_authority, feature_commands = self._kernel_required_commands(project)
            feature_kernel.record_file_mutation_boundary()
            feature_kernel.validate(
                authority=feature_authority, command_results=feature_commands,
                semantic_validator=feature_adapter.semantic_validate,
            )
            candidate = feature_kernel.finalize()
            if not isinstance(candidate, str):
                raise TransactionError(
                    "feature execution did not create a candidate implementation commit"
                )
            self._advance_cycle(
                cycle_path, state, "feature_in_progress", inspector, "kernel_feature_commit",
                kernel=feature_kernel,
            )
            self._advance_cycle(
                cycle_path, state, "feature_review", inspector, "kernel_feature_validated",
                kernel=feature_kernel,
            )
            self._advance_cycle(
                cycle_path, state, "feature_accepted", inspector, "kernel_feature_accepted",
                kernel=feature_kernel,
            )
            state["candidate_implementation_commit"] = candidate
            state["accepted_feature_commit"] = None
            feature_completion = feature_kernel.complete(
                evidence={
                    "candidate_implementation_commit": candidate,
                    "controller_acceptance_pending": True,
                }
            )
            self._materialize_terminal_cycle_cache(
                cycle_path, state, feature_transaction.transaction_id, feature_completion, phase_ledger
            )

            metadata_paths = acceptance_metadata_paths(project, selection.feature)
            acceptance_adapter = FeatureAcceptanceAdapter(
                allowed_paths=metadata_paths,
                commit_subject=f"{selection.feature_id}: {selection.title}",
                next_state="integration_ready",
            )
            acceptance_kernel = WorkflowKernel(
                project=project, ledger=phase_ledger,
                projection=ProjectionEngine(phase_ledger, phase_root / "projection-cache.json"),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            phase_kernels.append((acceptance_kernel, acceptance_adapter))
            acceptance_transaction = acceptance_kernel.begin(
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                milestone=project.active_milestone, feature_id=selection.feature_id,
                run_id=run_id, policy=acceptance_adapter.policy,
            )
            acceptance_kernel.acquire_lease(); acceptance_kernel.capture_snapshot()
            feature_branch = str(
                state.get("feature_branch") or inspector.current_branch or ""
            )
            milestone_branch = str(project.milestone_branch or "")
            milestone_id = str(project.active_milestone or "")
            if not feature_branch or not milestone_branch or not milestone_id:
                raise TransactionError(
                    "accepted-commit finalization lacks exact feature and milestone refs"
                )
            materialized_paths = materialize_acceptance_metadata(
                project=project,
                feature_id=selection.feature_id,
                feature_branch=feature_branch,
                milestone_base=feature_transaction.starting_head,
                candidate_commit=candidate,
                recovery=False,
            )
            if (
                not materialized_paths
                or not set(materialized_paths).issubset(metadata_paths)
            ):
                raise TransactionError(
                    "accepted-feature metadata authorization changed during finalization"
                )
            finalization = acceptance_kernel.finalize_deterministic_accepted_commit(
                candidate_commit=candidate,
                milestone_id=milestone_id,
                milestone_branch=milestone_branch,
                milestone_base=feature_transaction.starting_head,
                feature_branch=feature_branch,
                metadata_paths=materialized_paths,
                validation_evidence={
                    "tests_passed": True,
                    "review_passed": True,
                    "documentation_current": True,
                },
            )
            accepted = str(finalization["finalized_accepted_commit"])
            state["accepted_feature_commit"] = accepted
            state["candidate_implementation_commit"] = candidate
            self._advance_cycle(
                cycle_path,
                state,
                "integration_pending",
                inspector,
                "kernel_integration_pending",
                kernel=acceptance_kernel,
            )
            self._advance_cycle(
                cycle_path, state, "integration_ready", inspector,
                "kernel_feature_acceptance", kernel=acceptance_kernel,
            )
            acceptance_completion = acceptance_kernel.complete(
                classification="FEATURE_ACCEPTED",
                evidence={
                    "accepted_feature_commit": accepted,
                    "candidate_implementation_commit": candidate,
                    "authorized_metadata_paths": list(materialized_paths),
                    "permitted_metadata_paths": list(metadata_paths),
                    "implementation_tree_equivalent": True,
                    "integration_status": "pending",
                },
            )
            self._materialize_terminal_cycle_cache(
                cycle_path, state, acceptance_transaction.transaction_id,
                acceptance_completion, phase_ledger,
            )

            integration_context = self._validate_projected_dispatch(
                project,
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                expected=None,
                allow_nonstarting_checkout=True,
                allow_unmaterialized_integration_queue=True,
            )
            if integration_context is None:
                raise ProjectionError(
                    "feature acceptance did not produce an authoritative integration plan"
                )
            _, integration_execution_plan = integration_context
            if (
                integration_execution_plan.transaction_mode != "fresh"
                or integration_execution_plan.feature_id != selection.feature_id
                or integration_execution_plan.accepted_commit != accepted
            ):
                raise ProjectionError(
                    "feature acceptance disagrees with the fresh milestone-integration plan"
                )

            integration_result = self._execute_projected_integration(
                project,
                mode,
                run_id,
                integration_execution_plan,
                reservation_held=True,
            )
            if integration_result.get("outcome") != "feature_integrated":
                return integration_result
            integrated = integration_result["integrated_commit"]
            state["integration_session_id"] = None
            self._advance_cycle(
                cycle_path, state, "integrating", inspector,
                "deterministic_controller_plan_integrated",
            )
            self._advance_cycle(
                cycle_path, state, "integration_validation", inspector,
                "deterministic_controller_plan_validated",
            )
            self._advance_cycle(
                cycle_path, state, "feature_integrated", inspector,
                "deterministic_controller_plan_completed",
            )
            state["milestone_post_integration_commit"] = inspector.head
            integration_projection = integration_result["kernel_projection"]
            integration_transaction_id = next(
                item["transaction_id"]
                for item in reversed(integration_projection["transactions"])
                if item.get("workflow_type") == "milestone_integration"
            )
            self._materialize_terminal_cycle_cache(
                cycle_path,
                state,
                integration_transaction_id,
                {"projection": integration_projection},
                phase_ledger,
            )
            summary_after = FeatureQueue.from_location(
                project.repository, project.queue_location
            ).summary(project.active_milestone or "")
            post_next = "milestone_gate" if summary_after["milestone_complete"] else "feature_ready"
            evidence = {
                "feature": selection.feature_id, "accepted_commit": accepted,
                "integrated_commit": integrated, "repair_attempts": repairs,
                "model_session_launched_for_integration": False,
            }
            project_path = (
                "feature_running", "feature_accepted", "integration_pending",
                "integrating", "feature_integrated", post_next,
            )
            for projected_state in project_path:
                self._transition_project(
                    project, project_state, projected_state, run_id=run_id,
                    checkpoint=f"kernel_projected_{projected_state}",
                    feature=selection.feature_id if projected_state != "feature_ready" else None,
                    kernel_owned=True,
                    kernel_projection={
                        "transaction_id": integration_transaction_id,
                        "ledger_sequence": integration_projection["ledger_sequence"],
                        "ledger_fingerprint": integration_projection["ledger_fingerprint"],
                        "projection_fingerprint": integration_projection["projection_fingerprint"],
                    },
                )
            return evidence
        except (ConveyorError, ValueError) as exc:
            for phase_kernel, phase_adapter in reversed(phase_kernels):
                self._terminalize_handled_kernel_failure(phase_kernel, phase_adapter, exc)
            raise
        finally:
            if feature_writer_acquired and not session_attempted:
                self._writer_lease(project, inspector).release(run_id=run_id)
            if reservation is not None:
                reservation.release(run_id)

    def _execute_queue_reconciliation(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        *,
        expected_execution_plan: ExecutionPlan | None = None,
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        planning_start = capture_planning_start(project, inspector, run_id)
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        transaction_path = planning_report_path(report_root, run_id)
        reservation = self._launch_lock(project, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"],
            run_id=run_id,
            current_feature=None,
            current_phase="queue_reconciliation",
        ))
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        workflow_lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        tracked = inspector.git(["ls-files", "-z"]).stdout.split("\0")
        allowed_paths = tuple(sorted(path for path in tracked if path and allowed_planning_path(path)))
        preselected = FeatureQueue.from_location(project.repository, project.queue_location).select_next(
            project.active_milestone or ""
        )
        cost_plan = build_run_plan(
            {
                "proposed_next_action": "queue_reconciliation",
                "selected_feature": preselected.feature_id if preselected else None,
            },
            self.root,
            project=project,
            profile_configuration=self.configuration.execution_profiles or None,
            override_profile=self.execution_profile_override,
        )
        if (
            cost_plan["execution"]["models_planned"] < 1
            or cost_plan["execution"]["models_planned"] != cost_plan["parent_session_budget"]
            or not cost_plan.get("selected_model")
            or not cost_plan.get("selected_reasoning_effort")
        ):
            raise RecoveryError(
                "queue reconciliation cannot launch without one authoritative model execution plan"
            )
        planning_start["cost_aware_execution_plan"] = cost_plan
        adapter = QueueReconciliationAdapter(
            allowed_paths=allowed_paths,
            allowed_prefixes=tuple(prefix.rstrip("/") for prefix in ALLOWED_PLANNING_PREFIXES),
            allow_untracked=True,
            commit_subject=planning_commit_subject(project, preselected.feature_id if preselected else None),
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project, ledger=ledger, projection=projection, lease=workflow_lease,
        )
        try:
            self._validate_projected_dispatch(
                project,
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                expected=expected_execution_plan,
            )
            kernel.begin(
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                milestone=project.active_milestone or "",
                run_id=run_id,
                feature_id=None,
                policy=adapter.policy,
                expected_starting_branch=(
                    expected_execution_plan.milestone_branch
                    if expected_execution_plan is not None else None
                ),
                expected_starting_head=(
                    expected_execution_plan.starting_commit
                    if expected_execution_plan is not None else None
                ),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            inspector.ensure_runtime_ignored()
            kernel.checkpoint("runtime_ignore_verified")
            persist_planning_transaction(transaction_path, planning_start)
            return self._execute_queue_reconciliation_locked(
                project,
                mode,
                run_id,
                project_state,
                inspector,
                planning_start=planning_start,
                transaction_path=transaction_path,
                kernel=kernel,
                adapter=adapter,
                cost_plan=cost_plan,
            )
        except (ConveyorError, ValueError) as exc:
            self._terminalize_handled_kernel_failure(kernel, adapter, exc)
            raise
        finally:
            reservation.release(run_id)

    def _execute_queue_reconciliation_locked(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        inspector: RepositoryInspector,
        *,
        planning_start: dict[str, Any],
        transaction_path: Path,
        kernel: WorkflowKernel,
        adapter: QueueReconciliationAdapter,
        cost_plan: dict[str, Any],
    ) -> dict[str, Any]:
        result, repairs, retry_status = self._launch_with_retries(SessionRequest(
            action="queue_reconciliation", project=project, run_id=run_id, mode=mode,
            transaction_id=kernel.transaction.transaction_id,
            repository_identity=kernel.transaction.repository_identity,
            starting_branch=kernel.transaction.starting_branch,
            starting_commit=kernel.transaction.starting_head,
            allowed_paths=kernel.transaction.allowed_mutation_policy.allowed_paths,
            parent_session_budget=int(cost_plan["parent_session_budget"]),
            child_session_budget=int(cost_plan["child_session_budget"]),
            planned_model=str(cost_plan["selected_model"]),
            planned_reasoning=str(cost_plan["selected_reasoning_effort"]),
            model_plan_source=str(cost_plan["profile_resolution_source"]),
        ), inspector, "queue_reconciliation", "queue_reconciliation_repairs", reservation_held=True,
            on_session_started=kernel.session_launched)
        if not result.session_id:
            raise SessionError("queue reconciliation returned no exact session identity")
        transaction = {
            **planning_start,
            "status": "planning_changes_pending_validation",
            "session_ids": [result.session_id] if result.session_id else [],
            "session_id": result.session_id,
            "reconciliation_report": result.report_path,
            "result_classification": result.result_classification,
            "changed_paths": sorted(set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())),
            "diff_fingerprint": inspector.planning_diff_fingerprint(),
            "updated_at": utc_now(),
        }
        persist_planning_transaction(transaction_path, transaction)
        canonical_to_legacy = {
            "RECONCILED_READY_WORK": "reconciled_ready_work",
            "RECONCILED_NO_READY_WORK": "reconciled_no_ready_work",
            "MILESTONE_COMPLETE": "milestone_complete",
            "HUMAN_DECISION_REQUIRED": "human_decision_required",
            "PLANNING_VALIDATION_FAILED": "invalid_queue",
            "RETRYABLE_PLANNING_FAILURE": "session_execution_failed",
            "TERMINAL_PLANNING_FAILURE": "session_execution_failed",
        }
        classification = canonical_to_legacy.get(str(result.result_classification), result.result_classification)
        queue_result_evidence = (
            result.structured_result.get("evidence", {})
            if isinstance(result.structured_result, dict)
            and result.structured_result.get("workflow_type") == "queue_reconciliation"
            else (result.structured_result or {})
        )

        def accept_typed_result(canonical: str, next_state: str) -> None:
            if kernel.envelope is not None:
                return
            if result.transaction_envelope is None:
                raise SessionError("new queue workflow requires one typed transaction result")
            envelope = SessionResultEnvelope.from_dict(result.transaction_envelope)
            if envelope.classification != canonical or envelope.next_state != next_state:
                raise SessionError("queue typed result disagrees with deterministic terminal mapping")
            kernel.accept_result(envelope)
            kernel.record_file_mutation_boundary()

        def projection_evidence(projection: dict[str, Any]) -> dict[str, Any]:
            return {
                "transaction_id": kernel.transaction.transaction_id,
                "ledger_sequence": projection.get("ledger_sequence"),
                "ledger_fingerprint": projection.get("ledger_fingerprint"),
                "projection_fingerprint": projection.get("projection_fingerprint"),
            }
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
            projected = kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required",
                human_gate=gate,
            )
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="queue_reconciliation_compatibility_gate",
                stop_reason=str(result.primary_terminal_error or result.failure_classification),
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility},
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            persist_planning_transaction(transaction_path, {
                **transaction,
                "status": "terminal_planning_failure",
                "failure_classification": result.failure_classification,
                "updated_at": utc_now(),
            })
            return {
                "outcome": "human_decision_required",
                "next_state": "human_decision_required",
                "human_decision": gate,
                "report": result.report_path,
            }
        if classification in {"session_execution_failed", "structured_output_invalid", None}:
            message = self._session_failure_message(project, run_id, result, project_state["current_state"])
            projected = kernel.block(
                state=TransactionState.RETRYABLE_FAILURE,
                classification="RETRYABLE_PLANNING_FAILURE",
                next_state="validation_failed",
            )
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="queue_reconciliation_failed", stop_reason=message,
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            persist_planning_transaction(transaction_path, {
                **transaction,
                "status": "terminal_planning_failure",
                "failure_classification": classification,
                "error": message,
                "updated_at": utc_now(),
            })
            raise SessionError(message)

        if classification == "invalid_queue":
            message = self._session_failure_message(project, run_id, result, project_state["current_state"])
            accept_typed_result("PLANNING_VALIDATION_FAILED", "validation_failed")
            projected = kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="PLANNING_VALIDATION_FAILED",
                next_state="validation_failed",
            )
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="invalid_queue", stop_reason=message,
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            persist_planning_transaction(transaction_path, {
                **transaction,
                "status": "planning_validation_failed",
                "failure_classification": "invalid_queue",
                "error": message,
                "updated_at": utc_now(),
            })
            return {"outcome": "invalid_queue", "next_state": "validation_failed", "report": result.report_path}

        if classification == "human_decision_required":
            human = queue_result_evidence.get("human_decision")
            accept_typed_result("HUMAN_DECISION_REQUIRED", "human_decision_required")
            projected = kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required",
                human_gate=human,
            )
            self._transition_project(
                project, project_state, "human_decision_required", run_id=run_id,
                checkpoint="queue_reconciliation_human_gate", stop_reason="queue reconciliation requires a human decision",
                human_gate=human,
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            persist_planning_transaction(transaction_path, {
                **transaction,
                "status": "human_decision_required",
                "human_decision": human,
                "updated_at": utc_now(),
            })
            return {"outcome": classification, "next_state": "human_decision_required", "human_decision": human}

        try:
            report = json.loads(Path(str(result.report_path)).read_text(encoding="utf-8"))
            if inspector.tracked_changed_paths() or inspector.untracked_file_hashes():
                validation = validate_planning_changes(
                    project,
                    inspector,
                    report,
                    run_id=run_id,
                    starting_head=planning_start["planning_start_commit"],
                    expected_session_id=result.session_id,
                )
            else:
                validation = validate_planning_noop(
                    project,
                    inspector,
                    report,
                    run_id=run_id,
                    starting_head=planning_start["planning_start_commit"],
                    expected_session_id=result.session_id,
                )
            terminal_classification = PLANNING_CLASSIFICATIONS[str(classification)]
            accept_typed_result(terminal_classification, {
                "reconciled_ready_work": "feature_ready",
                "reconciled_no_ready_work": "paused",
                "milestone_complete": "milestone_gate",
                "legitimately_blocked": "paused",
            }[str(classification)])
            kernel.validate(
                authority=CommandAuthority(), command_results=(),
                semantic_validator=adapter.semantic_validate,
            )
            planning_commit = kernel.finalize()
            committed = {
                **validation,
                "status": "planning_changes_committed" if validation.get("changed_paths") else "planning_no_changes",
                "planning_result_commit": planning_commit,
                "effective_milestone_head": planning_commit,
                "planning_commit_status": "committed" if validation.get("changed_paths") else "not_required",
                "repository_clean": True,
                "selected_feature_starting_commit": planning_commit if validation.get("selected_feature") else None,
                "committed_at": utc_now(),
            }
            persist_planning_transaction(transaction_path, committed)
        except (OSError, json.JSONDecodeError, ConveyorError) as exc:
            failure = {
                **transaction,
                "status": "planning_validation_failed",
                "failure_classification": "PLANNING_VALIDATION_FAILED",
                "error": str(exc),
                "changed_paths": inspector.tracked_changed_paths(),
                "updated_at": utc_now(),
            }
            persist_planning_transaction(transaction_path, failure)
            projected = kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="PLANNING_VALIDATION_FAILED",
                next_state="validation_failed",
            ) if kernel.transaction and kernel.transaction.current_state not in {
                TransactionState.BLOCKED, TransactionState.HUMAN_DECISION_REQUIRED,
                TransactionState.RETRYABLE_FAILURE, TransactionState.TERMINAL_FAILURE,
            } else kernel.projection.rebuild(persist_cache=True)
            self._transition_project(
                project,
                project_state,
                "validation_failed",
                run_id=run_id,
                checkpoint="planning_validation_failed",
                stop_reason=str(exc),
                state_evidence={"planning_transaction": failure},
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            return {
                "outcome": "planning_validation_failed",
                "next_state": "validation_failed",
                "failed_validator": str(exc),
                "changed_paths": failure["changed_paths"],
                "report": result.report_path,
            }

        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        summary = queue.summary(project.active_milestone or "")
        derived = summary["reconciliation_classification"]
        if classification != derived:
            message = (
                f"structured reconciliation result {classification!r} contradicts deterministic queue "
                f"classification {derived!r}; report={result.report_path}"
            )
            projected = kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="PLANNING_SEMANTIC_CONFLICT",
                next_state="validation_failed",
            )
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="structured_result_contradicted", stop_reason=message,
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            raise SessionError(message)

        validation = queue_result_evidence.get("queue_validation", {})
        try:
            compare_queue_validation_evidence(
                validation,
                authoritative_queue_validation_evidence(project, queue),
            )
        except RecoveryError as exc:
            message = f"structured queue-validation evidence disagrees with deterministic parsing; report={result.report_path}"
            projected = kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="PLANNING_SEMANTIC_CONFLICT",
                next_state="validation_failed",
            )
            self._transition_project(
                project, project_state, "validation_failed", run_id=run_id,
                checkpoint="structured_queue_validation_contradicted", stop_reason=message,
                kernel_owned=True,
                kernel_projection=projection_evidence(projected),
            )
            raise SessionError(f"{message}; {exc}") from exc

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
        completed = kernel.complete(
            classification=committed["terminal_classification"],
            evidence={
                "planning_status": "passed",
                "selected_feature": committed.get("selected_feature"),
                "planning_result_commit": committed.get("planning_result_commit"),
            },
        )
        self._transition_project(
            project, project_state, target, run_id=run_id, checkpoint=classification,
            stop_reason=stop_reasons.get(classification),
            feature=committed.get("selected_feature"),
            state_evidence={"planning_transaction": committed},
            event_commit=committed.get("planning_result_commit"),
            event_command_category="planning_commit",
            event_validation_outcome="passed",
            kernel_owned=True,
            kernel_projection=projection_evidence(completed["projection"]),
        )
        return {
            "outcome": classification,
            "next_state": target,
            "queue_status": summary,
            "report": result.report_path,
            "repair_attempts": repairs,
            "planning_transaction": committed,
            "planning_result_commit": committed.get("planning_result_commit"),
        }

    def _execute_milestone_gate(
        self,
        project: Project,
        mode: str,
        run_id: str,
        project_state: dict[str, Any],
        *,
        reservation_held: bool = False,
        expected_execution_plan: ExecutionPlan | None = None,
    ) -> dict[str, Any]:
        inspector = RepositoryInspector(project.repository)
        kernel: WorkflowKernel | None = None
        adapter: MilestoneGateAdapter | None = None
        initial_context = self._authoritative_execution_context(project)
        if initial_context is not None:
            initial_projection, initial_executable, _ = initial_context
            initial_executable.validate_against(initial_projection)
            if initial_executable.workflow != WorkflowType.MILESTONE_GATE:
                raise ProjectionError(
                    "authoritative execution plan does not permit a milestone-gate session"
                )
            if (
                expected_execution_plan is not None
                and initial_executable.to_dict() != expected_execution_plan.to_dict()
            ):
                raise ProjectionError("milestone-gate execution plan changed before preflight")
            expected_execution_plan = initial_executable
        compatibility = self._compatibility_snapshot(project, "milestone_gate")
        if compatibility is not None and compatibility.get("compatible") is not True:
            if expected_execution_plan is not None:
                compatibility_reservation = self._launch_lock(project, inspector)
                compatibility_reservation.acquire(make_lock_record(
                    project_id=project.project_id,
                    repository_identity=inspector.identity()["repository_id"],
                    run_id=run_id,
                    current_feature=expected_execution_plan.feature_id,
                    current_phase="milestone_gate_compatibility_preflight",
                ))
                try:
                    self._validate_projected_dispatch(
                        project,
                        workflow_type=WorkflowType.MILESTONE_GATE,
                        expected=expected_execution_plan,
                    )
                finally:
                    compatibility_reservation.release(run_id)
                raise SessionError(
                    f"classification={compatibility.get('classification')}; "
                    f"remediation={compatibility.get('remediation')}; "
                    f"continue={compatibility.get('validation_command')}"
                )
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
            identity = inspector.identity()
            phase_root = self.root / "state/projects" / project.project_id
            compatibility_ledger = EvidenceLedger(
                phase_root / "evidence-ledger.jsonl", project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            compatibility_adapter = MilestoneGateAdapter(
                allowed_paths=(), commit_subject="factory: milestone compatibility gate",
                next_state="milestone_ready_for_merge",
            )
            compatibility_kernel = WorkflowKernel(
                project=project, ledger=compatibility_ledger,
                projection=ProjectionEngine(
                    compatibility_ledger, phase_root / "projection-cache.json"
                ),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            compatibility_transaction = compatibility_kernel.begin(
                workflow_type=WorkflowType.MILESTONE_GATE,
                milestone=project.active_milestone, feature_id=None, run_id=run_id,
                policy=compatibility_adapter.policy,
            )
            compatibility_kernel.acquire_lease()
            compatibility_kernel.capture_snapshot()
            blocked_projection = compatibility_kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="HUMAN_DECISION_REQUIRED",
                next_state="human_decision_required", human_gate=gate,
            )
            self._transition_project(
                project,
                project_state,
                "human_decision_required",
                run_id=run_id,
                checkpoint="milestone_gate_compatibility_blocked",
                stop_reason=str(compatibility.get("diagnostic")),
                human_gate=gate,
                state_evidence={"compatibility_preflight": compatibility},
                kernel_owned=True,
                kernel_projection={
                    "transaction_id": compatibility_transaction.transaction_id,
                    **blocked_projection,
                },
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
            self._validate_projected_dispatch(
                project,
                workflow_type=WorkflowType.MILESTONE_GATE,
                expected=expected_execution_plan,
            )
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
            cycle_path = inspector.cycle_state_path()
            state = self.cycle_store.read(cycle_path)
            identity = inspector.identity()
            phase_root = self.root / "state/projects" / project.project_id
            ledger = EvidenceLedger(
                phase_root / "evidence-ledger.jsonl", project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            adapter, tracked_paths = self._milestone_gate_adapter(project, inspector)
            kernel = WorkflowKernel(
                project=project, ledger=ledger,
                projection=ProjectionEngine(ledger, phase_root / "projection-cache.json"),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            transaction = kernel.begin(
                workflow_type=WorkflowType.MILESTONE_GATE,
                milestone=project.active_milestone, feature_id=None,
                run_id=run_id, policy=adapter.policy,
                expected_starting_branch=(
                    expected_execution_plan.milestone_branch
                    if expected_execution_plan is not None else None
                ),
                expected_starting_head=(
                    expected_execution_plan.starting_commit
                    if expected_execution_plan is not None else None
                ),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            pinned_commands = self._configured_kernel_commands(project)
            pinned_command_fingerprint = fingerprint([list(item) for item in pinned_commands])
            adapter_bytes = inspector.safe_worktree_file_bytes(".factory/project.yaml")
            if adapter_bytes is None:
                raise RecoveryError("factory adapter disappeared before milestone-gate launch")
            kernel.checkpoint("required_commands_pinned", {
                "configured_source": ".factory/project.yaml",
                "configured_source_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "command_fingerprint": pinned_command_fingerprint,
                "commands": [list(item) for item in pinned_commands],
            })
            inspector.ensure_runtime_ignored()
            kernel.checkpoint("runtime_ignore_verified")
            if state is None or state.get("current_phase") == "completed":
                state = self._new_cycle_state(project, run_id, inspector, None)
                self._write_cycle_cache(
                    project, cycle_path, state, inspector, "milestone_cycle_initialized", kernel=kernel
                )
                self._advance_cycle(
                    cycle_path, state, "preflight", inspector, "milestone_preflight", kernel=kernel
                )
                self._advance_cycle(
                    cycle_path, state, "milestone_gate", inspector, "milestone_gate_launch", kernel=kernel
                )
            elif state["current_phase"] == "feature_integrated":
                self._advance_cycle(
                    cycle_path, state, "milestone_gate", inspector, "milestone_gate_launch", kernel=kernel
                )
            elif state["current_phase"] != "milestone_gate":
                raise ConveyorError(f"cycle phase {state['current_phase']} cannot enter milestone gate")
            request = SessionRequest(
                action="milestone_gate", project=project, run_id=run_id, mode=mode,
                transaction_id=transaction.transaction_id,
                repository_identity=identity["repository_id"],
                starting_branch=transaction.starting_branch,
                starting_commit=transaction.starting_head,
                allowed_paths=tracked_paths,
            )
            result = self._launch_session(
                request, inspector, "milestone_gate", reservation_held=True,
                on_session_started=kernel.session_launched,
            )
            if result.transaction_envelope is None:
                raise SessionError(self._session_failure_message(
                    project, run_id, result, "milestone_gate"
                ))
            gate_envelope = SessionResultEnvelope.from_dict(result.transaction_envelope)
            gate_blocked = self._route_kernel_result(kernel, adapter, gate_envelope)
            if gate_blocked is not None:
                self._transition_project(
                    project, project_state, gate_envelope.next_state, run_id=run_id,
                    checkpoint="kernel_milestone_gate_terminal",
                    stop_reason=gate_envelope.classification,
                    human_gate=gate_envelope.evidence.get("human_decision"),
                    kernel_owned=True,
                    kernel_projection={"transaction_id": transaction.transaction_id, **gate_blocked},
                )
                return {"outcome": gate_envelope.next_state, "classification": gate_envelope.classification}
            authority, commands = self._execute_kernel_commands(project, pinned_commands)
            kernel.record_file_mutation_boundary()
            kernel.validate(
                authority=authority, command_results=commands,
                semantic_validator=adapter.semantic_validate,
            )
            gate_commit = kernel.finalize()
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            milestone = queue.milestone(project.active_milestone or "")
            if milestone is None or milestone.get("status") != "gate_passed" or not queue.milestone_complete(project.active_milestone or ""):
                raise QueueError("milestone gate result lacks gate_passed queue evidence")
            self._advance_cycle(
                cycle_path, state, "completed", inspector, "milestone_gate_passed", kernel=kernel
            )
            gate = {
                "decision": "Approve or decline merge of the validated milestone branch into the default branch.",
                "milestone_branch": project.milestone_branch,
                "default_branch_merge_performed": False,
            }
            completed = kernel.complete(evidence={
                "gate_commit": gate_commit,
                "human_merge_gate": gate,
            })
            self._materialize_terminal_cycle_cache(
                cycle_path, state, transaction.transaction_id, completed, kernel.ledger
            )
            if project_state["current_state"] != "milestone_gate":
                self._transition_project(
                    project, project_state, "milestone_gate", run_id=run_id,
                    checkpoint="kernel_milestone_gate", kernel_owned=True,
                    kernel_projection={"transaction_id": transaction.transaction_id, **completed["projection"]},
                )
            self._transition_project(
                project, project_state, "milestone_ready_for_merge", run_id=run_id,
                checkpoint="milestone_gate_passed",
                stop_reason="human milestone merge approval required", human_gate=gate,
                state_evidence=self._milestone_state_evidence(project, inspector, gate=True),
                kernel_owned=True,
                kernel_projection={"transaction_id": transaction.transaction_id, **completed["projection"]},
            )
            return {"outcome": "milestone_ready_for_merge", "human_gate": gate}
        except (ConveyorError, ValueError) as exc:
            if kernel is not None and adapter is not None:
                self._terminalize_handled_kernel_failure(kernel, adapter, exc)
            raise
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
        if evidence.get("outcome") == "human_decision_required":
            return {"project_id": project.project_id, **evidence}
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
            target_project_state = "integrating"
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
            self._write_cycle_cache(project, inspector.cycle_state_path(), state, inspector, "integration_resume_repairs_recorded")
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

    def _integration_finalization_recovery_context(
        self, project: Project
    ) -> dict[str, Any] | None:
        projection = self._authoritative_projection(project)
        if not isinstance(projection, dict) or projection.get("current_state") != "validation_failed":
            return None
        identity = RepositoryInspector(project.repository).identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        for event in reversed(ledger.read()):
            if (
                event.get("event_type") == "TransactionBlocked"
                and event.get("workflow_type") == WorkflowType.MILESTONE_INTEGRATION.value
                and (event.get("payload") or {}).get("classification")
                == "VALIDATION_FAILED"
                and (event.get("payload") or {}).get("reference") == "SafetyViolation"
            ):
                plan_path = (
                    state_root / "integration-plans" / f"{event['transaction_id']}.json"
                )
                if not plan_path.exists():
                    raise RecoveryError(
                        "pre-validation integration failure lacks its immutable plan"
                    )
                evidence = inspect_integration_finalization_recovery(plan_path)
                return {"plan_path": plan_path, "evidence": evidence}
        return None

    def _cache_binding_recovery_plan(
        self, project: Project, *, allow_missing: bool = False
    ) -> dict[str, Any] | None:
        """Return a deterministic repair plan for one stale or missing cache.

        The repository-local cycle document is compatibility state, not an
        authority to start work. Its binding must be repaired from the valid
        ledger projection before ordinary resume dispatch.
        """
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger_path = state_root / "evidence-ledger.jsonl"
        if not ledger_path.exists():
            return None
        ledger = EvidenceLedger(
            ledger_path, project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection_engine = ProjectionEngine(ledger, state_root / "projection-cache.json")
        cycle_path = inspector.cycle_state_path()
        cycle_exists = cycle_path.exists()
        if not cycle_exists and not allow_missing:
            return None
        try:
            integrity = ledger.verify()
            canonical_projection = projection_engine.rebuild(persist_cache=False)
            loaded_cycle = (
                json.loads(cycle_path.read_text(encoding="utf-8"))
                if cycle_exists
                else {}
            )
            cycle = (
                normalize_cycle_cache_for_rebinding(
                    loaded_cycle,
                    cycle_schema=self.cycle_store.schema,
                )
                if cycle_exists and isinstance(loaded_cycle, dict)
                else loaded_cycle
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            ProjectionError,
            SchemaValidationError,
        ):
            return None
        if not isinstance(cycle, dict):
            return None
        legacy_provenance_present = (
            cycle_exists
            and LEGACY_CACHE_BINDING_RECOVERY_FIELD in loaded_cycle
        )
        unsigned = dict(loaded_cycle)
        claimed_fingerprint = unsigned.pop("kernel_cache_fingerprint", None)
        fingerprint_invalid = (
            cycle_exists
            and (
                not isinstance(claimed_fingerprint, str)
                or claimed_fingerprint != fingerprint(unsigned)
            )
        )
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        selected = canonical_projection.get(
            "selected_next_feature"
        ) or canonical_projection.get("current_feature")
        current_feature = canonical_projection.get("current_feature")
        current_state = canonical_projection.get("current_state")
        ordinary_next_action = canonical_projection.get("allowed_next_action")
        writer = inspect_repository_writer_lock(
            inspector.writer_lock_path(self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]),
            project.repository,
        )
        if (
            canonical_projection.get("active_transaction") is not None
            or not isinstance(current_state, str)
            or not isinstance(ordinary_next_action, str)
            or selected is not None and not isinstance(selected, str)
            or current_feature is not None and not isinstance(current_feature, str)
            or not inspector.is_clean
            or writer.exists
            or any(inspector.git_operation_state().values())
        ):
            return None
        selected_queue_feature = queue.feature(selected) if selected is not None else None
        observed_projection = projection_engine.rebuild(
            persist_cache=False,
            observations=build_projection_observations(
                branch=inspector.current_branch, head=inspector.head, clean=inspector.is_clean,
                git_operations=inspector.git_operation_state(),
                queue_feature=(selected_queue_feature or {}).get("id"),
                queue_integration_status=(selected_queue_feature or {}).get("integration_status"),
                lease_transaction=None, lease_valid=True, live_session_id=None,
            ),
        )
        queue_bound_projection = bind_projection_to_queue(
            observed_projection, queue, str(project.active_milestone or "")
        )
        terminal_states = {
            "completed", "blocked", "human_decision_required", "retryable_failure",
            "terminal_failure", "superseded",
        }
        terminals = [
            transaction
            for transaction in canonical_projection.get("transactions", [])
            if isinstance(transaction, dict)
            and transaction.get("state") in terminal_states
            and isinstance(transaction.get("transaction_id"), str)
            and isinstance(transaction.get("last_sequence"), int)
        ]
        if not terminals:
            return None
        source = max(terminals, key=lambda item: item["last_sequence"])
        source_transaction = source["transaction_id"]
        if ledger.terminal_event(source_transaction) is None:
            return None
        try:
            canonical_binding = validated_canonical_projection_binding(
                ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=source_transaction,
                plan_stale_cache_rebuild=True,
            )
        except RecoveryError:
            return None
        if canonical_binding.canonical_projection != canonical_projection:
            return None
        durable_binding_invalid = any(
            cycle.get(key) != expected
            for key, expected in {
                "kernel_transaction_id": source_transaction,
                "kernel_ledger_sequence": canonical_binding.ledger_sequence,
                "kernel_ledger_fingerprint": canonical_binding.ledger_fingerprint,
                "kernel_projection_fingerprint": canonical_binding.projection_fingerprint,
            }.items()
        )
        if (
            cycle_exists
            and not fingerprint_invalid
            and not durable_binding_invalid
            and not legacy_provenance_present
        ):
            return None
        if (
            queue_bound_projection.get("current_state") != current_state
            or queue_bound_projection.get("current_feature") != current_feature
            or queue_bound_projection.get("selected_next_feature")
            != canonical_projection.get("selected_next_feature")
        ):
            return None
        consistency = ConsistencyChecker(
            controller_root=self.root,
            project=project,
            planner_observer=lambda: self._project_plan(
                project, allow_cache_binding_recovery=False
            ),
        ).check()
        failed = consistency.get("failed_invariants") or []
        if (
            consistency.get("classification") != "RECOVERABLE_INCONSISTENCY"
            or len(failed) != 1
            or failed[0].get("invariant") != "cycle_cache_binding"
            or failed[0].get("classification") != "RECOVERABLE_INCONSISTENCY"
        ):
            return None
        queue_path = resolve_queue_path(project.repository, project.queue_location)
        selected_feature_execution_plan = (
            build_run_plan(
                {
                    "proposed_next_action": "feature_cycle",
                    "selected_feature": selected,
                    "application_mutation_expected": True,
                },
                self.root,
                project=project,
                profile_configuration=self.configuration.execution_profiles or None,
                override_profile=self.execution_profile_override,
            )
            if selected is not None
            else None
        )
        return {
            "project_id": project.project_id,
            "workflow_type": "cache_binding_recovery",
            "transaction_mode": "recovery",
            "proposed_next_action": "cache_binding_recovery",
            "next_action": "cache_binding_recovery",
            "current_state": current_state,
            "current_feature": current_feature,
            "selected_feature": selected,
            "ordinary_next_action": ordinary_next_action,
            "source_transaction": source_transaction,
            "ledger_sequence": canonical_binding.ledger_sequence,
            "ledger_fingerprint": canonical_binding.ledger_fingerprint,
            "projection_fingerprint": canonical_binding.projection_fingerprint,
            "canonical_projection_fingerprint": canonical_binding.projection_fingerprint,
            "observed_projection_fingerprint": observed_projection["projection_fingerprint"],
            "queue_bound_projection_fingerprint": queue_bound_projection["projection_fingerprint"],
            "canonical_cache_rebuild_required": canonical_binding.cache_rebuild_required,
            "repository_branch": inspector.current_branch,
            "repository_head": inspector.head,
            "queue_fingerprint": hashlib.sha256(queue_path.read_bytes()).hexdigest(),
            "models_planned": 0,
            "child_sessions_planned": 0,
            "application_content_commits_planned": 0,
            "application_mutation": "ignored .factory/conveyor-state.json only",
            "application_mutation_expected": True,
            "feature_branch_creation": False,
            "feature_factory_would_launch": False,
            "milestone_integrator_would_launch": False,
            "model_sessions_that_would_launch": [],
            "child_sessions_that_would_launch": [],
            "sessions_that_would_launch": [],
            "selected_feature_execution_plan": selected_feature_execution_plan,
            "execution": {"models_planned": 0},
            "cost_aware_run_plan": {
                "execution": {"models_planned": 0},
                "child_session_budget": 0,
                "expected_application_mutations": True,
                "deterministic_only": True,
            },
        }

    def _apply_cache_binding_recovery(self, project: Project, plan: dict[str, Any]) -> dict[str, Any]:
        """Atomically rebind exactly one corrupt cache, then stop dispatch."""
        run_id = f"cache-recovery-{uuid.uuid4()}"
        inspector = RepositoryInspector(project.repository)
        reservation = self._launch_lock(project, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=inspector.identity()["repository_id"], run_id=run_id,
            current_feature=plan.get("selected_feature"),
            current_phase="cache_binding_recovery",
        ))
        try:
            refreshed = self._cache_binding_recovery_plan(
                project, allow_missing=not inspector.cycle_state_path().exists()
            )
            immutable = (
                "source_transaction", "ledger_sequence", "ledger_fingerprint", "projection_fingerprint",
                "repository_branch", "repository_head", "queue_fingerprint", "current_state",
                "current_feature", "selected_feature", "ordinary_next_action",
            )
            if refreshed is None or any(refreshed.get(key) != plan.get(key) for key in immutable):
                raise RecoveryError("cache-binding recovery evidence changed before signing")
            queue = FeatureQueue.from_location(project.repository, project.queue_location)
            feature_identity = plan.get("selected_feature") or plan.get("current_feature")
            feature = queue.feature(str(feature_identity)) if feature_identity is not None else None
            if feature_identity is not None and feature is None:
                raise RecoveryError("cache-binding recovery selected feature disappeared")
            state = self._new_cycle_state(project, run_id, inspector, feature)
            state.update({
                "current_feature": plan["current_feature"],
                "selected_feature": plan["selected_feature"],
                "current_phase": plan["current_state"],
                "conveyor_run_id": run_id,
                "feature_session_id": None,
                "session_id": None,
                "last_successful_checkpoint": "cache_binding_recovery_terminal",
                "next_safe_action": plan["ordinary_next_action"],
                "last_verified_git_state": self._git_checkpoint(inspector),
                "updated_at": utc_now(),
            })
            identity = inspector.identity()
            state_root = self.root / "state/projects" / project.project_id
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl", project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            projection_engine = ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            )
            canonical_binding = validated_canonical_projection_binding(
                ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=str(plan["source_transaction"]),
                repair_stale_cache=True,
            )
            canonical_projection = canonical_binding.canonical_projection
            observed_projection = projection_engine.rebuild(
                persist_cache=False,
                observations=build_projection_observations(
                    branch=inspector.current_branch, head=inspector.head, clean=inspector.is_clean,
                    git_operations=inspector.git_operation_state(),
                    queue_feature=feature.get("id") if feature is not None else None,
                    queue_integration_status=(
                        feature.get("integration_status") if feature is not None else None
                    ),
                    lease_transaction=None, lease_valid=True, live_session_id=None,
                ),
            )
            queue_bound_projection = bind_projection_to_queue(
                observed_projection, queue, str(project.active_milestone or "")
            )
            if (
                canonical_projection.get("current_state") != plan["current_state"]
                or canonical_projection.get("current_feature") != plan["current_feature"]
                or canonical_projection.get("selected_next_feature")
                != plan["selected_feature"]
                or canonical_projection.get("allowed_next_action")
                != plan["ordinary_next_action"]
                or queue_bound_projection.get("current_state") != plan["current_state"]
                or queue_bound_projection.get("current_feature") != plan["current_feature"]
                or queue_bound_projection.get("selected_next_feature")
                != plan["selected_feature"]
            ):
                raise RecoveryError(
                    "planning projections disagree with the canonical recovery binding"
                )
            # A final pre-write comparison catches queue, branch, ledger, and
            # projection drift after the short-lived reservation was acquired.
            final_plan = self._cache_binding_recovery_plan(
                project, allow_missing=not inspector.cycle_state_path().exists()
            )
            if final_plan is None or any(final_plan.get(key) != plan.get(key) for key in immutable):
                raise RecoveryError("cache-binding recovery evidence changed before atomic write")
            write_terminal_cycle_cache(
                inspector.cycle_state_path(), state, ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=str(plan["source_transaction"]),
                expected_feature=plan["selected_feature"],
            )
            return {
                "project_id": project.project_id,
                "outcome": "cache_binding_recovered",
                "workflow_type": "cache_binding_recovery",
                "transaction_mode": "recovery",
                "current_state": plan["current_state"],
                "current_feature": plan["current_feature"],
                "selected_feature": plan["selected_feature"],
                "ordinary_next_action": plan["ordinary_next_action"],
                "source_transaction": plan["source_transaction"],
                "recovery_run_id": run_id,
                "models_launched": 0,
                "child_sessions_launched": 0,
                "application_content_commits_created": 0,
                "feature_branch_created": False,
                "application_mutation": "ignored .factory/conveyor-state.json only",
                "stopped_after_cache_repair": True,
            }
        finally:
            reservation.release(run_id)

    def _recover_integration_finalization(
        self, project: Project, run_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        plan_path = Path(context["plan_path"])
        initial = context["evidence"]
        plan = load_integration_plan(plan_path)
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        lease = WorkflowWriterLease(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            )
        )
        adapter = MilestoneIntegrationAdapter(
            allowed_paths=tuple(plan["metadata_paths"]),
            commit_subject=f"factory: record {plan['feature_id']} integration passed",
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=ProjectionEngine(ledger, state_root / "projection-cache.json"),
            lease=lease,
        )
        reservation = self._launch_lock(project, inspector)
        reservation.acquire(
            make_lock_record(
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                run_id=run_id,
                current_feature=plan["feature_id"],
                current_phase="integration_finalization_recovery",
            )
        )
        try:
            reserved = inspect_integration_finalization_recovery(plan_path)
            immutable_fields = (
                "topology_fingerprint",
                "controller_project_id",
                "adapter_project_id",
                "repository_identity",
                "repository_path_fingerprint",
                "resulting_feature_commit",
                "integrating_metadata_commit",
            )
            changed = [
                field for field in immutable_fields if reserved.get(field) != initial.get(field)
            ]
            if changed:
                raise RecoveryError(
                    "integration recovery identity changed under reservation: "
                    + ", ".join(changed)
                )
            transaction = kernel.begin_for_branch(
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                target_branch=plan["milestone_branch"],
                milestone=plan["milestone_id"],
                feature_id=plan["feature_id"],
                run_id=run_id,
                policy=adapter.policy,
                expected_starting_branch=plan["milestone_branch"],
                expected_starting_head=str(reserved["integrating_metadata_commit"]),
            )
            kernel.acquire_lease()
            kernel.prepare_starting_branch()
            kernel.capture_snapshot()
            lease_record = lease.bind_controller_plan(
                transaction_id=transaction.transaction_id,
                repository_identity=identity["repository_id"],
                controller_project_id=project.project_id,
                adapter_project_id=str(reserved["adapter_project_id"]),
                feature_branch=plan["feature_branch"],
                accepted_commit=plan["accepted_commit"],
            )
            if lease_record is None:
                raise LockError("integration recovery lease disappeared")
            result = resume_integration_finalization(plan_path)
            kernel.accept_deterministic_integration_result(plan, result)
            if result["classification"] == "VALIDATION_FAILED":
                blocked = kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="VALIDATION_FAILED",
                    next_state="validation_failed",
                    reference="deterministic_recovery_validation_failed",
                )
                return {
                    "project_id": project.project_id,
                    "outcome": "validation_failed",
                    "kernel_projection": blocked,
                    "model_session_launched": False,
                }
            integrated = str(result["resulting_feature_commit"])
            completion = kernel.complete(
                classification="INTEGRATED",
                evidence={
                    "accepted_feature_commit": plan["accepted_commit"],
                    "integrated_commit": integrated,
                    "integration_status": "passed",
                    "integration_plan_fingerprint": plan["plan_fingerprint"],
                    "recovered_transaction_id": plan["transaction_id"],
                    "integration_runtime": result["runtime"],
                    "model_session_launched": False,
                },
            )
            return {
                "project_id": project.project_id,
                "outcome": "feature_integrated",
                "feature": plan["feature_id"],
                "accepted_commit": plan["accepted_commit"],
                "integrated_commit": integrated,
                "recovered_transaction_id": plan["transaction_id"],
                "kernel_projection": completion["projection"],
                "next_action": (
                    "milestone_gate"
                    if result.get("milestone_complete") is True
                    else "feature_ready"
                ),
                "model_session_launched": False,
            }
        except (ConveyorError, ValueError) as exc:
            self._terminalize_handled_kernel_failure(kernel, adapter, exc)
            raise
        finally:
            reservation.release(run_id)

    def resume_project(self, project: Project, run_id: str | None = None) -> dict[str, Any]:
        planning_plan = self.project_plan(project)
        planning_recovery = planning_plan.get("planning_finalization_recovery")
        if (
            planning_plan.get("proposed_next_action") == "planning_finalization"
            and isinstance(planning_recovery, dict)
        ):
            return self._recover_terminal_planning_finalization(
                project,
                expected_plan=planning_recovery,
            )
        kernel_recovery = self._kernel_recovery_preflight(project, apply=True)
        if kernel_recovery is not None:
            return kernel_recovery
        finalization_recovery = self._integration_finalization_recovery_context(project)
        if finalization_recovery is not None:
            return self._recover_integration_finalization(
                project, run_id or str(uuid.uuid4()), finalization_recovery
            )
        context = self._authoritative_execution_context(project)
        if context is not None:
            projection, executable, _ = context
            action = projection.get("allowed_next_action")
            if action == "milestone_integration":
                return self._execute_projected_integration(
                    replace(project, current_state=str(projection["current_state"])),
                    "resume", run_id or str(uuid.uuid4()), executable,
                )
            if action == "milestone_gate":
                inspector = RepositoryInspector(project.repository)
                cycle = self.cycle_store.read(inspector.cycle_state_path())
                if isinstance(cycle, dict) and cycle.get("current_phase") == "completed":
                    return {
                        "project_id": project.project_id,
                        "outcome": "already_completed",
                        "current_state": projection.get("current_state"),
                        "kernel_projection": projection,
                    }
                effective = replace(project, current_state="milestone_gate")
                project_state = self._project_document(
                    effective, run_id, inspector.identity()["path_fingerprint"]
                )
                project_state["current_state"] = "milestone_gate"
                return {
                    "project_id": project.project_id,
                    **self._execute_milestone_gate(
                        effective,
                        "resume",
                        run_id or str(uuid.uuid4()),
                        project_state,
                        expected_execution_plan=executable,
                    ),
                }
            if action in {
                "queue_reconciliation",
                "feature_cycle",
                "human_decision_resolution",
                "human_merge_approval",
                "verify_consistency",
            }:
                return {
                    "project_id": project.project_id,
                    "outcome": "no_exact_transaction_to_resume",
                    "current_state": projection.get("current_state"),
                    "next_action": action,
                    "kernel_projection": projection,
                    "human_gate": projection.get("human_gate"),
                }
            raise ProjectionError(f"unsupported authoritative resume action: {action}")
        inspector = RepositoryInspector(project.repository)
        cycle_path = inspector.cycle_state_path()
        state = self.cycle_store.read(cycle_path)
        cycle_fingerprint = hashlib.sha256(cycle_path.read_bytes()).hexdigest() if cycle_path.exists() else None
        assessment = assess_recovery(project, state)
        if assessment.outcome != "resume" or state is None:
            return {"project_id": project.project_id, "outcome": assessment.outcome, "human_decision": assessment.human_decision}
        return {
            "project_id": project.project_id,
            "outcome": "human_decision_required",
            "reason": (
                "legacy cycle evidence has no exact kernel transaction; a writable session "
                "cannot be resumed outside RecoveryPlanner"
            ),
            "legacy_resume_blocked": True,
            "session_launched": False,
        }
    def _execute_projected_integration(
        self,
        project: Project,
        mode: str,
        run_id: str,
        execution_plan: ExecutionPlan,
        *,
        reservation_held: bool = False,
    ) -> dict[str, Any]:
        """Start a fresh exact integration transaction from canonical projection evidence."""

        context = self._authoritative_execution_context(project)
        if context is None:
            raise ProjectionError("projected integration requires an authoritative ledger projection")
        current_projection, executable, _ = context
        if (
            executable.to_dict() != execution_plan.to_dict()
            or executable.workflow_type != WorkflowType.MILESTONE_INTEGRATION.value
            or executable.transaction_mode != "fresh"
        ):
            raise ProjectionError("integration dispatch disagrees with the reloaded execution plan")
        executable.validate_against(current_projection)
        projection = current_projection
        inspector = RepositoryInspector(project.repository)
        feature_id = executable.feature_id
        if not isinstance(feature_id, str) or not feature_id:
            raise RecoveryError("projected integration has no exact feature identity")
        if projection.get("current_feature") not in {None, feature_id}:
            raise RecoveryError("projected feature contradicts the unique integration candidate")
        accepted = executable.accepted_commit
        if (
            not isinstance(accepted, str)
            or accepted == "SELF"
            or inspector.rev_parse(accepted, check=False) != accepted
        ):
            raise RecoveryError("projected integration lacks one exact accepted commit")
        milestone_head = inspector.rev_parse(project.milestone_branch or "", check=False)
        if (
            not inspector.is_clean
            or any(inspector.git_operation_state().values())
            or milestone_head != executable.starting_commit
        ):
            raise RecoveryError(
                "projected integration requires a clean worktree and exact milestone target"
            )

        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl", project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        two_ref = inspect_two_refs(
            repository=project.repository,
            controller_project_id=project.project_id,
            feature_id=feature_id,
            feature_branch=str(executable.feature_branch),
            accepted_commit=accepted,
            milestone_id=str(project.active_milestone),
            milestone_branch=str(project.milestone_branch),
            pre_integration_head=str(executable.starting_commit),
            queue_path=project.queue_location,
        )
        accepted_paths = tuple(two_ref["accepted_changed_paths"])
        authorized_paths = tuple(
            sorted(set(accepted_paths) | set(two_ref["metadata_paths"]))
        )
        adapter = MilestoneIntegrationAdapter(
            allowed_paths=authorized_paths,
            commit_subject=f"factory: integrate {feature_id}",
            next_state="feature_integrated",
        )
        kernel = WorkflowKernel(
            project=project, ledger=ledger,
            projection=ProjectionEngine(ledger, state_root / "projection-cache.json"),
            lease=lease,
        )
        reservation = None if reservation_held else self._launch_lock(project, inspector)
        if reservation is not None:
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=identity["repository_id"], run_id=run_id,
                current_feature=feature_id,
                current_phase="projected_integration",
            ))
        try:
            reserved_context = self._validate_projected_dispatch(
                project,
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                expected=executable,
            )
            if reserved_context is None:
                raise ProjectionError("authoritative projection disappeared before integration")
            projection, executable = reserved_context
            accepted = executable.accepted_commit
            if not isinstance(accepted, str) or accepted == "SELF":
                raise ProjectionError("reserved integration plan lacks a normalized accepted commit")
            if inspector.rev_parse(
                str(project.milestone_branch), check=False
            ) != executable.starting_commit:
                raise TransactionError(
                    "planned transaction start changed after reserved dispatch validation"
                )
            reserved_two_ref = inspect_two_refs(
                repository=project.repository,
                controller_project_id=project.project_id,
                feature_id=feature_id,
                feature_branch=str(executable.feature_branch),
                accepted_commit=accepted,
                milestone_id=str(project.active_milestone),
                milestone_branch=str(project.milestone_branch),
                pre_integration_head=str(executable.starting_commit),
                queue_path=project.queue_location,
            )
            reserved_identity_checks = {
                "controller_project_id": reserved_two_ref.get("controller_project_id")
                == two_ref.get("controller_project_id"),
                "adapter_project_id": reserved_two_ref.get("adapter_project_id")
                == two_ref.get("adapter_project_id"),
                "repository_identity": reserved_two_ref.get("repository_identity")
                == two_ref.get("repository_identity"),
                "repository_path_fingerprint": reserved_two_ref.get(
                    "repository_path_fingerprint"
                )
                == two_ref.get("repository_path_fingerprint"),
            }
            changed_identities = [
                field for field, matches in reserved_identity_checks.items() if not matches
            ]
            if changed_identities:
                raise IntegrationPlanError(
                    "reserved integration identity changed before TransactionStarted: "
                    + ", ".join(changed_identities)
                )
            two_ref = reserved_two_ref
            gitignore = project.repository / ".gitignore"
            gitignore_before = gitignore.read_bytes() if gitignore.exists() else None
            exclusion = inspector.ensure_milestone_integration_runtime_ignored()
            gitignore_after = gitignore.read_bytes() if gitignore.exists() else None
            if gitignore_after != gitignore_before:
                raise RecoveryError(
                    "local integration-runtime exclusion changed tracked .gitignore"
                )
            transaction = kernel.begin_for_branch(
                workflow_type=WorkflowType.MILESTONE_INTEGRATION,
                target_branch=str(project.milestone_branch),
                milestone=project.active_milestone, feature_id=feature_id,
                run_id=run_id, policy=adapter.policy,
                expected_starting_branch=executable.milestone_branch,
                expected_starting_head=executable.starting_commit,
            )
            kernel.acquire_lease(); kernel.prepare_starting_branch(); kernel.capture_snapshot()
            integrity = ledger.verify()
            lease_record = lease.bind_controller_plan(
                transaction_id=transaction.transaction_id,
                repository_identity=identity["repository_id"],
                controller_project_id=project.project_id,
                adapter_project_id=str(two_ref["adapter_project_id"]),
                feature_branch=str(executable.feature_branch),
                accepted_commit=accepted,
            )
            if lease_record is None:
                raise LockError("controller integration lease disappeared before plan creation")
            plan = build_integration_plan(
                repository=project.repository,
                controller_project_id=project.project_id,
                transaction_id=transaction.transaction_id,
                run_id=run_id,
                feature_id=feature_id,
                feature_branch=str(executable.feature_branch),
                accepted_commit=accepted,
                milestone_id=str(project.active_milestone),
                milestone_branch=str(project.milestone_branch),
                pre_integration_head=str(executable.starting_commit),
                queue_path=project.queue_location,
                projection_fingerprint=str(projection["projection_fingerprint"]),
                ledger_sequence=integrity.sequence,
                ledger_fingerprint=integrity.fingerprint,
                controller_ledger_path=ledger.path,
                lease_identity=lease_record.to_dict(),
                runtime_exclusion=exclusion,
                verified_evidence=two_ref,
            )
            plan_path = persist_integration_plan(
                state_root
                / "integration-plans"
                / f"{transaction.transaction_id}.json",
                plan,
            )
            result = execute_integration_plan(plan_path)
            kernel.accept_deterministic_integration_result(plan, result)
            if result["classification"] == "SEMANTIC_CONFLICT":
                gate = result.get("human_gate")
                blocked = kernel.block(
                    state=TransactionState.HUMAN_DECISION_REQUIRED,
                    classification="SEMANTIC_CONFLICT",
                    next_state="human_decision_required",
                    human_gate=gate if isinstance(gate, dict) else None,
                )
                return {
                    "project_id": project.project_id,
                    "outcome": "human_decision_required",
                    "classification": "SEMANTIC_CONFLICT",
                    "human_gate": gate,
                    "kernel_projection": blocked,
                    "model_session_launched": False,
                }
            if result["classification"] == "VALIDATION_FAILED":
                blocked = kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="VALIDATION_FAILED",
                    next_state="validation_failed",
                    reference="deterministic_validation_failed",
                )
                return {
                    "project_id": project.project_id,
                    "outcome": "validation_failed",
                    "classification": "VALIDATION_FAILED",
                    "kernel_projection": blocked,
                    "model_session_launched": False,
                }
            integrated = str(result["resulting_feature_commit"])
            completion = kernel.complete(classification="INTEGRATED", evidence={
                "accepted_feature_commit": accepted,
                "integrated_commit": integrated,
                "integration_status": "passed",
                "integration_plan_fingerprint": plan["plan_fingerprint"],
                "integration_plan_path": str(plan_path),
                "integration_runtime": result["runtime"],
                "model_session_launched": False,
            })
            return {
                "project_id": project.project_id,
                "outcome": "feature_integrated",
                "feature": feature_id,
                "accepted_commit": accepted,
                "integrated_commit": integrated,
                "kernel_projection": completion["projection"],
                "next_action": (
                    "milestone_gate"
                    if result.get("milestone_complete") is True
                    else "feature_ready"
                ),
                "integration_plan": str(plan_path),
                "model_session_launched": False,
            }
        except (ConveyorError, ValueError) as exc:
            self._terminalize_handled_kernel_failure(kernel, adapter, exc)
            raise
        finally:
            if reservation is not None:
                reservation.release(run_id)

    def _recover_terminal_planning_finalization(
        self,
        project: Project,
        *,
        expected_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Finalize one terminal warning-only planning result in a fresh recovery transaction."""

        effective = self.effective_project(project)
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        reservation = self._launch_lock(effective, inspector)
        recovery_run_id = f"recovery-{uuid.uuid4()}"
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            run_id=recovery_run_id,
            current_feature=expected_plan.get("selected_feature"),
            current_phase="planning_finalization_recovery",
        ))
        state_root = self.root / "state/projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        workflow_lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        selected_feature = expected_plan.get("selected_feature")
        if selected_feature is not None and (
            not isinstance(selected_feature, str) or not selected_feature
        ):
            raise RecoveryError("planning recovery selected-feature evidence is malformed")
        next_state = "feature_ready" if selected_feature else "paused"
        expected_paths = tuple(expected_plan["existing_planning_changes"]["paths"])
        existing_commit = expected_plan.get("existing_commit")
        committed_recovery = isinstance(existing_commit, str)
        adapter = RecoveryAdapter(
            allowed_paths=expected_paths,
            allow_untracked=False,
            commit_subject=planning_commit_subject(effective, selected_feature),
            next_state=next_state,
            require_clean_start=committed_recovery,
        )
        kernel = WorkflowKernel(
            project=effective,
            ledger=ledger,
            projection=projection,
            lease=workflow_lease,
        )
        try:
            refreshed_projection = self._authoritative_projection(project)
            if refreshed_projection is None:
                raise RecoveryError("planning recovery projection disappeared under reservation")
            refreshed = self._planning_finalization_recovery_plan(
                effective, refreshed_projection
            )
            if refreshed is None or refreshed.get("plan_fingerprint") != expected_plan.get("plan_fingerprint"):
                raise RecoveryError("planning recovery evidence changed under reservation")
            transaction = kernel.begin(
                workflow_type=WorkflowType.RECOVERY,
                milestone=project.active_milestone,
                feature_id=None,
                run_id=recovery_run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovered_transaction_id": expected_plan["original_transaction_id"],
                    "recovery_classification": "planning_finalization_recovery",
                    "original_run_id": expected_plan["original_run_id"],
                    "original_session_id": expected_plan["original_session_id"],
                    "original_ledger_sequence": expected_plan["original_ledger_sequence"],
                    "original_ledger_fingerprint": expected_plan["original_ledger_fingerprint"],
                    "planning_transaction_fingerprint": expected_plan["planning_transaction_fingerprint"],
                    "session_report_fingerprint": expected_plan["session_report_fingerprint"],
                    "model_sessions_planned": 0,
                    "child_sessions_planned": 0,
                },
                expected_starting_branch=expected_plan["starting_branch"],
                expected_starting_head=(existing_commit if committed_recovery else expected_plan["starting_commit"]),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            kernel.checkpoint("planning_finalization_recovery_verified", {
                "recovered_transaction_id": expected_plan["original_transaction_id"],
                "changed_paths": list(expected_paths),
                "diff_fingerprint": expected_plan.get("mutation_fingerprint") or expected_plan.get("existing_commit"),
                "selected_feature": selected_feature,
                "model_session_launched": False,
            })
            try:
                session_report = json.loads(
                    Path(expected_plan["session_report_path"]).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RecoveryError("planning recovery session report changed") from exc
            if committed_recovery:
                inventory = expected_plan.get("inventory_validation") or {}
                validation_warnings = list(inventory.get("nonfatal_warnings") or [])
                validation = {"inventory_validation": inventory, "queue_validation_evidence": expected_plan.get("queue_validation_evidence")}
                commit = kernel.adopt_committed_planning_recovery(
                    original_transaction_id=expected_plan["original_transaction_id"], commit=existing_commit,
                    expected_parent=expected_plan["starting_commit"], expected_paths=expected_paths,
                    expected_subject=planning_commit_subject(effective, selected_feature),
                    plan_fingerprint=expected_plan["plan_fingerprint"],
                    selected_feature=selected_feature, next_state=next_state,
                )
            else:
                validation = validate_planning_changes(
                    effective, inspector, session_report, run_id=expected_plan["original_run_id"],
                    starting_head=expected_plan["starting_commit"], expected_diff_fingerprint=expected_plan["mutation_fingerprint"],
                    expected_changed_paths=list(expected_paths), expected_session_id=expected_plan["original_session_id"],
                    expected_transaction_id=expected_plan["original_transaction_id"],
                )
                inventory = validation.get("inventory_validation") or {}
                validation_warnings = list(inventory.get("nonfatal_warnings") or [])
                commit = kernel.finalize_deterministic_planning_recovery(
                    original_transaction_id=expected_plan["original_transaction_id"], changed_paths=expected_paths,
                    expected_diff_fingerprint=expected_plan["mutation_fingerprint"], plan_fingerprint=expected_plan["plan_fingerprint"],
                    validation_evidence={"commands": [{"validator": inventory.get("validator"), "exit_code": inventory.get("exit_code")}, {"validator": "git diff --check", "exit_code": 0}], "warnings": validation_warnings},
                    selected_feature=selected_feature, next_state=next_state,
                )
            committed = {
                **validation,
                "status": "planning_changes_committed",
                "planning_result_commit": commit,
                "effective_milestone_head": commit,
                "planning_commit_status": "committed",
                "planning_commit_subject": inspector.commit_subject(commit),
                "planning_commit_would_be_created": False,
                "repository_clean": True,
                "selected_feature_starting_commit": commit if selected_feature else None,
                "recovery": True,
                "recovery_transaction_id": transaction.transaction_id,
                "original_transaction_id": expected_plan["original_transaction_id"],
                "model_sessions_launched": [],
                "child_sessions_launched": [],
                "nonfatal_warnings": validation_warnings,
                "next_state": next_state,
                "committed_at": utc_now(),
            }
            completed = kernel.complete(
                classification="RECOVERY_APPLIED",
                evidence={
                    "planning_status": "passed",
                    "selected_feature": selected_feature,
                    "planning_result_commit": commit,
                    "recovered_transaction_id": expected_plan["original_transaction_id"],
                    "model_session_launched": False,
                    "nonfatal_warnings": validation_warnings,
                },
            )
            recovery_report = self._report_path(
                self.configuration.owned_path(
                    self.configuration.conveyor["report_directory"]
                ),
                recovery_run_id,
                "planning-finalization.json",
            )
            atomic_write_json(recovery_report, {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": recovery_run_id,
                "workflow_type": "recovery",
                "transaction_id": transaction.transaction_id,
                "original_transaction_id": expected_plan["original_transaction_id"],
                "starting_branch": expected_plan["starting_branch"],
                "starting_commit": expected_plan["starting_commit"],
                "planning_result_commit": commit,
                "selected_feature": selected_feature,
                "changed_paths": list(expected_paths),
                "inventory_validation": inventory,
                "nonfatal_warnings": validation_warnings,
                "model_sessions_launched": [],
                "child_sessions_launched": [],
                "deterministic_only": True,
                "outcome": "planning_recovery_committed",
                "ordinary_queue_reconciliation_available": True,
                "created_at": utc_now(),
            })
            document = self.load_project_state(project) or self._project_document(
                effective, recovery_run_id, identity["path_fingerprint"]
            )
            projection_evidence = {
                "transaction_id": transaction.transaction_id,
                "ledger_sequence": completed["projection"]["ledger_sequence"],
                "ledger_fingerprint": completed["projection"]["ledger_fingerprint"],
                "projection_fingerprint": completed["projection"]["projection_fingerprint"],
            }
            self._transition_project(
                effective,
                document,
                next_state,
                run_id=recovery_run_id,
                checkpoint="planning_finalization_recovered",
                feature=selected_feature,
                state_evidence={"planning_transaction": committed},
                event_branch=inspector.current_branch,
                event_commit=commit,
                event_command_category="planning_finalization_recovery",
                event_validation_outcome="passed_with_nonfatal_warnings" if validation_warnings else "passed",
                kernel_owned=True,
                kernel_projection=projection_evidence,
            )
            cycle_path = inspector.cycle_state_path()
            cycle = self.cycle_store.read(cycle_path)
            if cycle is None:
                cycle = self._new_cycle_state(
                    project, recovery_run_id, inspector,
                    FeatureQueue.from_location(project.repository, project.queue_location).feature(selected_feature),
                )
            cycle.update({
                "current_feature": selected_feature,
                "selected_feature": selected_feature,
                "current_phase": next_state,
                "conveyor_run_id": recovery_run_id,
                "kernel_transaction_id": transaction.transaction_id,
                "kernel_ledger_sequence": completed["projection"]["ledger_sequence"],
                "kernel_ledger_fingerprint": completed["projection"]["ledger_fingerprint"],
                "kernel_projection_fingerprint": completed["projection"]["projection_fingerprint"],
                "last_successful_checkpoint": "committed_queue_reconciliation_recovery_terminal",
                "updated_at": utc_now(),
            })
            inspector.ensure_runtime_ignored()
            finalized = write_terminal_cycle_cache(
                cycle_path, cycle, ledger=ledger,
                projection_engine=ProjectionEngine(
                    ledger, ledger.path.parent / "projection-cache.json"
                ),
                transaction_id=transaction.transaction_id,
                expected_feature=selected_feature,
            )
            return {
                "project_id": project.project_id,
                "outcome": "planning_recovery_committed",
                "current_state": next_state,
                "selected_feature": selected_feature,
                "planning_result_commit": commit,
                "planning_transaction": committed,
                "recovery_transaction_id": transaction.transaction_id,
                "original_transaction_id": expected_plan["original_transaction_id"],
                "report": str(recovery_report),
                "nonfatal_warnings": validation_warnings,
                "model_sessions_launched": [],
                "child_sessions_launched": [],
                "ordinary_queue_reconciliation_available": True,
                "next_action_after_consistency": (
                    "feature_cycle" if selected_feature else "planning_refinement"
                ),
                "feature_factory_would_launch": False,
                "milestone_integrator_would_launch": False,
                "application_source_written": False,
            }
        except (ConveyorError, ValueError) as exc:
            self._terminalize_handled_kernel_failure(kernel, adapter, exc)
            raise
        finally:
            reservation.release(recovery_run_id)

    def recover_planning_transaction(
        self,
        project: Project,
        *,
        run_id: str,
        expected_starting_head: str,
        expected_diff_fingerprint: str,
        expected_changed_paths: list[str],
        expected_session_id: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Validate and optionally commit one exact previously recorded planning transaction."""

        effective = self.effective_project(project)
        inspector = RepositoryInspector(project.repository)
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        transaction_path = planning_report_path(report_root, run_id)
        existing = load_planning_transaction(transaction_path)
        expected_paths = sorted(set(expected_changed_paths))
        if len(expected_paths) != len(expected_changed_paths):
            raise RecoveryError("expected planning paths must be unique")
        ledger_path = (
            self.root
            / "state/projects"
            / project.project_id
            / "evidence-ledger.jsonl"
        )
        if ledger_path.exists() and not (
            existing and existing.get("planning_commit_status") == "committed"
        ):
            projection = self._authoritative_projection(project)
            if projection is None:
                raise RecoveryError("transactional planning recovery projection is unavailable")
            plan = self._planning_finalization_recovery_plan(effective, projection)
            if plan is None:
                raise RecoveryError(
                    "no deterministic transactional planning recovery is available"
                )
            checks = {
                "run_id": plan.get("original_run_id") == run_id,
                "session_id": plan.get("original_session_id") == expected_session_id,
                "starting_head": plan.get("starting_commit") == expected_starting_head,
                "diff_fingerprint": plan.get("mutation_fingerprint")
                == expected_diff_fingerprint,
                "changed_paths": (
                    (plan.get("existing_planning_changes") or {}).get("paths")
                    == expected_paths
                ),
            }
            if not all(checks.values()):
                failed = ", ".join(
                    key for key, passed in checks.items() if not passed
                )
                raise RecoveryError(
                    "transactional planning recovery expectation disagrees: " + failed
                )
            if dry_run:
                return {
                    "project_id": project.project_id,
                    "outcome": "planning_recovery_validated",
                    "applied": False,
                    "checks": checks,
                    "planning_finalization_recovery": plan,
                    "model_sessions_that_would_launch": [],
                    "child_sessions_that_would_launch": [],
                    "feature_factory_would_launch": False,
                    "milestone_integrator_would_launch": False,
                    "application_source_written": False,
                }
            return self._recover_terminal_planning_finalization(
                project,
                expected_plan=plan,
            )
        if existing and existing.get("planning_commit_status") == "committed":
            commit = existing.get("planning_result_commit")
            checks = {
                "run_id": existing.get("run_id") == run_id,
                "session_id": existing.get("session_id") == expected_session_id,
                "planning_start_commit": existing.get("planning_start_commit") == expected_starting_head,
                "diff_fingerprint": existing.get("diff_fingerprint") == expected_diff_fingerprint,
                "changed_paths": existing.get("changed_paths") == expected_paths,
                "commit_exists": isinstance(commit, str) and inspector.ref_exists(commit),
                "commit_is_head": commit == inspector.head,
                "commit_parent": isinstance(commit, str)
                and inspector.rev_parse(f"{commit}^", check=False) == expected_starting_head,
                "commit_paths": isinstance(commit, str)
                and inspector.changed_paths(commit) == expected_paths,
                "repository_clean": inspector.is_clean,
            }
            if not all(checks.values()):
                failed = ", ".join(key for key, passed in checks.items() if not passed)
                raise RecoveryError(f"finalized planning transaction evidence disagrees: {failed}")
            return {
                "project_id": project.project_id,
                "outcome": "planning_transaction_already_finalized",
                "applied": not dry_run,
                "idempotent": True,
                "planning_transaction": existing,
                "checks": checks,
                "feature_factory_would_launch": False,
                "milestone_integrator_would_launch": False,
                "application_source_written": False,
            }
        if (
            existing
            and existing.get("status") == "planning_changes_committing"
            and inspector.is_clean
        ):
            commit = inspector.head
            selected = existing.get("selected_feature")
            expected_subject = planning_commit_subject(effective, selected)
            checks = {
                "head_advanced_once": inspector.rev_parse(f"{commit}^", check=False)
                == expected_starting_head,
                "commit_subject": inspector.commit_subject(commit) == expected_subject,
                "commit_paths": inspector.changed_paths(commit) == expected_paths,
                "recorded_diff": existing.get("diff_fingerprint") == expected_diff_fingerprint,
                "recorded_session": existing.get("session_id") == expected_session_id,
            }
            if all(checks.values()):
                committed = {
                    **existing,
                    "status": "planning_changes_committed",
                    "planning_result_commit": commit,
                    "effective_milestone_head": commit,
                    "planning_commit_status": "committed",
                    "planning_commit_subject": expected_subject,
                    "repository_clean": True,
                    "selected_feature_starting_commit": commit if selected else None,
                    "recovered_after_commit_interruption": True,
                    "committed_at": utc_now(),
                }
                if not dry_run:
                    persist_planning_transaction(transaction_path, committed)
                    document = self._project_document(
                        effective, run_id, inspector.identity()["path_fingerprint"]
                    )
                    self._transition_project(
                        effective,
                        document,
                        "feature_ready" if selected else "paused",
                        run_id=run_id,
                        checkpoint="planning_commit_interruption_recovered",
                        feature=selected,
                        state_evidence={"planning_transaction": committed},
                        event_commit=commit,
                        event_command_category="planning_commit_recovery",
                        event_validation_outcome="passed",
                    )
                return {
                    "project_id": project.project_id,
                    "outcome": "planning_transaction_already_finalized",
                    "applied": not dry_run,
                    "idempotent": True,
                    "planning_transaction": committed,
                    "checks": checks,
                    "feature_factory_would_launch": False,
                    "milestone_integrator_would_launch": False,
                    "application_source_written": False,
                }

        report_path = self._report_path(report_root, run_id, "queue_reconciliation.json")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"cannot read recorded queue reconciliation report: {exc}") from exc
        try:
            validation = validate_planning_changes(
                effective,
                inspector,
                report,
                run_id=run_id,
                starting_head=expected_starting_head,
                expected_diff_fingerprint=expected_diff_fingerprint,
                expected_changed_paths=expected_paths,
                expected_session_id=expected_session_id,
            )
        except ConveyorError as exc:
            if dry_run:
                raise
            gate = {
                "schema_version": 1,
                "phase": "queue_reconciliation",
                "status": "planning_recovery_gate",
                "project_id": project.project_id,
                "milestone": project.active_milestone,
                "run_id": run_id,
                "session_id": expected_session_id,
                "planning_start_commit": expected_starting_head,
                "planning_result_commit": None,
                "changed_paths": inspector.tracked_changed_paths(),
                "diff_fingerprint": inspector.planning_diff_fingerprint(),
                "planning_validation_status": "failed",
                "planning_commit_status": "not_created",
                "failed_validator": str(exc),
                "historical_integration_invalidated": False,
                "feature_factory_would_launch": False,
                "milestone_integrator_would_launch": False,
                "application_source_written": False,
                "created_at": utc_now(),
            }
            persist_planning_transaction(transaction_path, gate)
            document = self._project_document(
                effective, run_id, inspector.identity()["path_fingerprint"]
            )
            self._transition_project(
                effective,
                document,
                "validation_failed",
                run_id=run_id,
                checkpoint="planning_recovery_gate",
                stop_reason=str(exc),
                state_evidence={"planning_transaction": gate},
            )
            return {
                "project_id": project.project_id,
                "outcome": "planning_recovery_gate",
                "applied": False,
                "planning_transaction": gate,
                "feature_factory_would_launch": False,
                "milestone_integrator_would_launch": False,
                "application_source_written": False,
            }
        recovery = {
            **validation,
            "status": "planning_changes_validated",
            "recovery": True,
            "recovery_expected_starting_head": expected_starting_head,
            "recovery_expected_diff_fingerprint": expected_diff_fingerprint,
            "recovery_expected_changed_paths": expected_paths,
            "recovery_expected_session_id": expected_session_id,
            "planning_commit_status": "would_commit" if dry_run else "pending",
            "repository_clean": inspector.is_clean,
            "planning_commit_would_be_created": True,
            "selected_feature_starting_commit": None,
            "next_state": "feature_ready" if validation.get("selected_feature") else "paused",
        }
        if dry_run:
            return {
                "project_id": project.project_id,
                "outcome": "planning_recovery_validated",
                "applied": False,
                "planning_transaction": recovery,
                "feature_factory_would_launch": False,
                "milestone_integrator_would_launch": False,
                "application_source_written": False,
            }

        inspector.ensure_runtime_ignored()
        identity = inspector.identity()
        reservation = self._launch_lock(effective, inspector)
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            run_id=run_id,
            current_feature=validation.get("selected_feature"),
            current_phase="queue_reconciliation_recovery",
        ))
        lease = PlanningWriterLease(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ),
            project.repository,
        )
        try:
            lease.acquire(
                repository_identity=identity["repository_id"],
                project_id=project.project_id,
                milestone=project.active_milestone or "",
                run_id=run_id,
                session_id=expected_session_id,
                branch=str(inspector.current_branch or ""),
                head=expected_starting_head,
                worktree_fingerprint=stable_fingerprint({
                    "diff": expected_diff_fingerprint,
                    "paths": expected_paths,
                }),
                allowed_paths=expected_paths,
            )
            try:
                lease.revalidate(
                    run_id=run_id,
                    session_id=expected_session_id,
                    repository_identity=identity["repository_id"],
                    branch=str(inspector.current_branch or ""),
                    head=expected_starting_head,
                )
                persist_planning_transaction(transaction_path, {
                    **recovery,
                    "status": "planning_changes_committing",
                    "planning_commit_status": "committing",
                    "updated_at": utc_now(),
                })
                committed = finalize_planning_commit(effective, inspector, validation)
                committed.update({
                    "recovery": True,
                    "recovery_expected_starting_head": expected_starting_head,
                    "recovery_expected_diff_fingerprint": expected_diff_fingerprint,
                    "recovery_expected_changed_paths": expected_paths,
                    "recovery_expected_session_id": expected_session_id,
                    "next_state": "feature_ready" if committed.get("selected_feature") else "paused",
                })
                persist_planning_transaction(transaction_path, committed)
                document = self._project_document(
                    effective, run_id, identity["path_fingerprint"]
                )
                target = committed["next_state"]
                self._transition_project(
                    effective,
                    document,
                    target,
                    run_id=run_id,
                    checkpoint="planning_recovery_committed",
                    feature=committed.get("selected_feature"),
                    state_evidence={"planning_transaction": committed},
                    event_branch=inspector.current_branch,
                    event_commit=committed.get("planning_result_commit"),
                    event_command_category="planning_commit_recovery",
                    event_validation_outcome="passed",
                )
                return {
                    "project_id": project.project_id,
                    "outcome": "planning_recovery_committed",
                    "applied": True,
                    "planning_transaction": committed,
                    "feature_factory_would_launch": False,
                    "milestone_integrator_would_launch": False,
                    "application_source_written": False,
                }
            finally:
                lease.release(run_id=run_id)
        finally:
            reservation.release(run_id)

    def recover_feature_branch(self, project: Project, *, dry_run: bool) -> dict[str, Any]:
        """Recover an exact dirty milestone checkout by creating its persisted feature branch."""

        if (self.root / "state/projects" / project.project_id / "evidence-ledger.jsonl").exists():
            raise RecoveryError(
                "legacy feature-branch recovery is disabled after transactional-ledger cutover; use resume"
            )

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

        context = self._authoritative_execution_context(project)
        if context is not None:
            projection, executable, superseded = context
            existing = self.load_project_state(project)
            stale = (existing or {}).get("current_state") != projection.get("current_state") or (
                (existing or {}).get("state_evidence") or {}
            ).get("projection_fingerprint") != projection.get("projection_fingerprint")
            result = {
                "project_id": project.project_id,
                "dry_run": dry_run,
                "classification": "projection_compatibility_cache_reconciliation",
                "current_state": projection["current_state"],
                "proposed_state": projection["current_state"],
                "next_action": projection["allowed_next_action"],
                "execution_plan": executable.to_dict(),
                "superseded_legacy_cycles": superseded,
                "compatibility_cache_stale": stale,
                "would_persist_controller_state": stale and not dry_run,
                "controller_state_written": False,
                "application_repository_written": False,
                "application_tracked_files_written": False,
                "git_refs_written": False,
                "session_launch_performed": False,
            }
            if dry_run:
                return result
            inspector = RepositoryInspector(project.repository)
            reservation = self._launch_lock(project, inspector)
            run_id = f"compatibility-{uuid.uuid4()}"
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id=run_id,
                current_feature=executable.feature_id,
                current_phase="projection_compatibility_cache_reconciliation",
            ))
            try:
                reloaded = self._authoritative_execution_context(project)
                if reloaded is None:
                    raise ProjectionError("authoritative projection disappeared during reconciliation")
                current_projection, current_executable, _ = reloaded
                if (
                    current_projection.get("projection_fingerprint")
                    != projection.get("projection_fingerprint")
                    or current_executable.to_dict() != executable.to_dict()
                ):
                    raise ProjectionError("projection changed during compatibility reconciliation")
                document, written = self._reconcile_projection_compatibility_cache(
                    project, current_projection, current_executable
                )
                result.update({
                    "controller_state_written": written,
                    "compatibility_cache_stale": False,
                    "current_state": document["current_state"],
                })
                return result
            finally:
                reservation.release(run_id)

        effective = self.effective_project(project)
        persisted = self.load_project_state(project)
        plan = self.project_plan(project)
        historical_gate = plan.get("integration_gate")
        if isinstance(historical_gate, dict):
            result = {
                "project_id": project.project_id,
                "dry_run": dry_run,
                "classification": "integration_human_gate_recovery",
                "current_state": effective.current_state,
                "proposed_state": "human_decision_required",
                "integration_gate": historical_gate,
                "ordinary_resume_allowed": False,
                "human_resolution_required": True,
                "human_resolution_command": historical_gate.get("safe_continuation_command"),
                "application_repository_written": False,
                "application_tracked_files_written": False,
                "git_refs_written": False,
                "session_launch_performed": False,
                "would_persist_controller_state": not dry_run,
                "would_persist_repository_runtime_state": not dry_run,
            }
            if dry_run:
                return result
            inspector = RepositoryInspector(project.repository)
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ), project.repository,
            )
            if writer.exists or any(inspector.git_operation_state().values()) or not inspector.is_clean:
                raise RecoveryError("integration-gate recovery requires a clean repository, no Git operation, and no writer lease")
            cycle_path = inspector.cycle_state_path()
            cycle = self.cycle_store.read(cycle_path)
            if cycle is None or cycle.get("accepted_feature_commit") != historical_gate.get("accepted_feature_commit"):
                raise RecoveryError("integration-gate recovery cannot corroborate the accepted feature cycle")
            existing_document = self.load_project_state(project)
            existing_gate = cycle.get("human_decision_required")
            existing_project_gate = (
                existing_document.get("human_decision_required")
                if isinstance(existing_document, dict) else None
            )
            if (
                cycle.get("current_phase") == "human_decision_required"
                and isinstance(existing_gate, dict)
                and existing_gate.get("gate_id") == historical_gate.get("gate_id")
                and isinstance(existing_project_gate, dict)
                and existing_project_gate.get("gate_id") == historical_gate.get("gate_id")
                and existing_document.get("current_state") == "human_decision_required"
            ):
                result.update({
                    "current_state": "human_decision_required",
                    "gate_persisted": True,
                    "already_persisted": True,
                    "would_persist_controller_state": False,
                    "would_persist_repository_runtime_state": False,
                })
                return result
            inspector.ensure_runtime_ignored()
            if inspector.git(
                ["check-ignore", "-q", ".factory/runtime/milestone-integration/latest.json"],
                check=False,
            ).returncode != 0:
                raise RecoveryError("integration runtime local exclude could not be verified")
            run_id = str(cycle.get("conveyor_run_id") or f"recovery-{uuid.uuid4()}")
            cycle.update({
                "integration_gate": historical_gate,
                "integration_status": "blocked",
                "human_decision_required": historical_gate,
                "failure_classification": "HUMAN_DECISION_REQUIRED",
                "retry_exhausted": True,
                "next_safe_action": historical_gate.get("safe_continuation_command"),
                "stop_reason": historical_gate.get("reason"),
                "integration_session_id": historical_gate.get("integration_session_id"),
            })
            self._advance_cycle(
                cycle_path, cycle, "human_decision_required", inspector,
                "integration_terminal_human_decision_required_recovered",
            )
            document = self.load_project_state(project) or self._project_document(
                project, run_id, inspector.identity()["path_fingerprint"]
            )
            if document["current_state"] != "human_decision_required":
                if document["current_state"] in {"feature_running", "feature_accepted", "integration_pending", "integration_ready", "integrating"}:
                    document = self._transition_project(
                        project, document, "integration_blocked", run_id=run_id,
                        checkpoint="integration_blocked_recovered",
                        feature=str(cycle.get("current_feature")),
                        stop_reason=str(historical_gate.get("reason")),
                        state_evidence={"integration_gate": historical_gate},
                    )
                document = self._transition_project(
                    project, document, "human_decision_required", run_id=run_id,
                    checkpoint="integration_terminal_human_decision_required_recovered",
                    feature=str(cycle.get("current_feature")),
                    stop_reason=str(historical_gate.get("reason")),
                    human_gate=historical_gate,
                    state_evidence={"integration_gate": historical_gate},
                )
            result.update({"current_state": "human_decision_required", "gate_persisted": True})
            return result
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

    def _ack_reconciled_human_materialization(
        self, project: Project, *, resolution_id: str
    ) -> bool:
        """Acknowledge a replayed terminal side effect only after artifact verification."""

        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        state_root = self.root / "state/projects" / project.project_id
        ledger_path = state_root / "evidence-ledger.jsonl"
        if not ledger_path.exists():
            return False
        ledger = EvidenceLedger(
            ledger_path, project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        events = ledger.read()
        transaction_id = next((
            event["transaction_id"] for event in events
            if event["event_type"] == "TransactionStarted"
            and event["workflow_type"] == WorkflowType.HUMAN_DECISION_RESOLUTION.value
            and event["payload"].get("run_id") == resolution_id
            and any(
                candidate["transaction_id"] == event["transaction_id"]
                and candidate["event_type"] == "TransactionCompleted"
                for candidate in events
            )
        ), None)
        if transaction_id is None:
            return False
        pending = next((
            event for event in events
            if event["transaction_id"] == transaction_id
            and event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "compatibility_materialization_pending"
        ), None)
        acknowledged = any(
            event["transaction_id"] == transaction_id
            and event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint") == "compatibility_materialization_acknowledged"
            for event in events
        )
        if pending is None or acknowledged:
            return acknowledged
        ledger.append(
            event_type="CheckpointRecorded", transaction_id=transaction_id,
            workflow_type=WorkflowType.HUMAN_DECISION_RESOLUTION,
            payload={
                "checkpoint": "compatibility_materialization_acknowledged",
                "materialization_id": pending["payload"].get("materialization_id"),
                "reconciled_after_artifact_verification": True,
            },
        )
        ProjectionEngine(ledger, state_root / "projection-cache.json").rebuild(
            persist_cache=True
        )
        lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        kernel = WorkflowKernel(
            project=project, ledger=ledger,
            projection=ProjectionEngine(ledger, state_root / "projection-cache.json"),
            lease=lease,
        )
        kernel.restore(transaction_id)
        kernel.complete()
        return True

    def _pending_human_materialization_transaction(
        self, project: Project, *, resolution_id: str
    ) -> str | None:
        """Return the exact completed resolution awaiting compatibility replay."""

        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        ledger_path = self.root / "state/projects" / project.project_id / "evidence-ledger.jsonl"
        if not ledger_path.exists():
            return None
        ledger = EvidenceLedger(
            ledger_path, project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        events = ledger.read()
        matches = []
        for event in events:
            transaction_id = event["transaction_id"]
            if not (
                event["event_type"] == "TransactionStarted"
                and event["workflow_type"] == WorkflowType.HUMAN_DECISION_RESOLUTION.value
                and event["payload"].get("run_id") == resolution_id
            ):
                continue
            transaction_events = [
                candidate for candidate in events
                if candidate["transaction_id"] == transaction_id
            ]
            completed = any(
                candidate["event_type"] == "TransactionCompleted"
                for candidate in transaction_events
            )
            pending = any(
                candidate["event_type"] == "CheckpointRecorded"
                and candidate["payload"].get("checkpoint")
                == "compatibility_materialization_pending"
                for candidate in transaction_events
            )
            acknowledged = any(
                candidate["event_type"] == "CheckpointRecorded"
                and candidate["payload"].get("checkpoint")
                == "compatibility_materialization_acknowledged"
                for candidate in transaction_events
            )
            if completed and pending and not acknowledged:
                matches.append(transaction_id)
        if len(matches) > 1:
            raise RecoveryError("multiple pending human-resolution materializations are contradictory")
        return matches[0] if matches else None

    def resolve_human_decision(
        self, project: Project, *, reason: str, dry_run: bool
    ) -> dict[str, Any]:
        """Resolve one pinned human gate after deterministic evidence revalidation."""

        supplied_reason = reason.strip()
        reason_rejected_sensitive = redact_text(supplied_reason) != supplied_reason
        reason = "" if reason_rejected_sensitive else supplied_reason
        initial_persisted = self.load_project_state(project)
        persisted_gate = (
            initial_persisted.get("human_decision_required")
            if isinstance(initial_persisted, dict)
            and initial_persisted.get("current_state") == "human_decision_required"
            else None
        )
        historical_gate = None
        if persisted_gate is None and isinstance(initial_persisted, dict):
            history = initial_persisted.get("human_decision_history")
            matches = [
                item.get("gate")
                for item in history or []
                if isinstance(item, dict)
                and isinstance(item.get("gate"), dict)
                and isinstance(item.get("resolution"), dict)
                and item["resolution"].get("user_provided_reason") == supplied_reason
            ] if isinstance(history, list) else []
            if len(matches) == 1:
                historical_gate = matches[0]
        gate = (
            persisted_gate
            if isinstance(persisted_gate, dict)
            else (
                project.human_decision_gate
                if isinstance(project.human_decision_gate, dict)
                else (historical_gate or {})
            )
        )
        fingerprint_reason = reason if not reason_rejected_sensitive else "[REJECTED_SENSITIVE_REASON]"
        fingerprint = resolution_fingerprint(project.project_id, gate, fingerprint_reason)
        resolution_id = f"human-resolution-{fingerprint[:24]}"
        pending_materialization = self._pending_human_materialization_transaction(
            project, resolution_id=resolution_id
        )
        if not dry_run and pending_materialization is None:
            recovery = self._kernel_recovery_preflight(project, apply=True)
            if recovery is not None and recovery.get("outcome") == "human_decision_required":
                return recovery
        report_root = self.configuration.owned_path(self.configuration.conveyor["report_directory"])
        accepted_report_path = self._report_path(
            report_root, resolution_id, "human-decision-resolution.json"
        )
        audit_log_location = str(self.events.path)

        def prior_resolution(document: dict[str, Any] | None) -> dict[str, Any] | None:
            history = document.get("human_decision_history") if isinstance(document, dict) else None
            if not isinstance(history, list):
                return None
            matches = [
                item for item in history
                if isinstance(item, dict)
                and isinstance(item.get("resolution"), dict)
                and item["resolution"].get("resolution_fingerprint") == fingerprint
            ]
            if len(matches) > 1:
                raise RecoveryError("duplicate applied human-resolution records are contradictory")
            return matches[0]["resolution"] if matches else None

        def active_gate_is_newer(document: dict[str, Any] | None) -> bool:
            if not isinstance(document, dict) or document.get("current_state") != "human_decision_required":
                return False
            active = document.get("human_decision_required")
            if not isinstance(active, dict):
                return True
            active_fingerprint = active.get("gate_fingerprint")
            if not isinstance(active_fingerprint, str):
                active_fingerprint = gate_fingerprint(active)
            return bool(
                active.get("gate_id") != gate.get("gate_id")
                or active_fingerprint != gate_fingerprint(gate)
            )

        def expected_resolution_event(prior: dict[str, Any]) -> dict[str, Any]:
            event = run_event(
                run_id=resolution_id,
                project_id=project.project_id,
                repository_fingerprint=(prior.get("expected_repository_identity") or {}).get("path_fingerprint"),
                source="deterministic_script",
                milestone=prior.get("expected_milestone"),
                branch=prior.get("expected_branch"),
                commit=(prior.get("original_gate") or {}).get("accepted_feature_commit")
                or prior.get("expected_milestone_head"),
                command_category="human_decision_resolution",
                result="explicit_human_decision_resolved",
                validation_outcome="passed",
                previous_state=prior.get("previous_state"),
                next_state=prior.get("approved_next_state"),
                stop_reason="Explicit human decision resolved; queue reconciliation is next.",
                human_gate={
                    "gate_id": (prior.get("original_gate") or {}).get("gate_id"),
                    "gate_fingerprint": prior.get("original_gate_fingerprint"),
                    "resolution_id": resolution_id,
                    "resolution_fingerprint": fingerprint,
                    "actor_classification": "explicit_user_approval",
                    "report_location": str(accepted_report_path),
                },
            )
            event["timestamp"] = str(prior.get("timestamp"))
            return event

        def audit_matches(value: dict[str, Any], expected: dict[str, Any]) -> bool:
            if not isinstance(value.get("timestamp"), str) or not value["timestamp"]:
                return False
            actual_provenance = {key: item for key, item in value.items() if key != "timestamp"}
            expected_provenance = {key: item for key, item in expected.items() if key != "timestamp"}
            return actual_provenance == expected_provenance

        def validate_and_repair_applied_artifacts(
            prior: dict[str, Any], *, repair: bool
        ) -> tuple[list[dict[str, Any]], str | None]:
            checks: list[dict[str, Any]] = []
            repair_failure: str | None = None
            report_valid = False
            try:
                stored_report = json.loads(accepted_report_path.read_text(encoding="utf-8"))
                report_valid = isinstance(stored_report, dict) and stored_report == prior
            except (OSError, json.JSONDecodeError):
                stored_report = None
            report_repair_started = not report_valid and repair
            if not report_valid and repair:
                try:
                    atomic_write_json(accepted_report_path, prior)
                    stored_report = json.loads(accepted_report_path.read_text(encoding="utf-8"))
                    report_valid = isinstance(stored_report, dict) and stored_report == prior
                except (OSError, json.JSONDecodeError, ConveyorError) as exc:
                    repair_failure = f"applied resolution report repair failed: {type(exc).__name__}"
            checks.append({
                "validator": "applied_resolution_report_matches_history",
                "passed": report_valid,
                "evidence": {
                    "path": str(accepted_report_path),
                    "exists": accepted_report_path.exists(),
                    "resolution_fingerprint_matches": isinstance(stored_report, dict)
                    and stored_report.get("resolution_fingerprint") == fingerprint,
                    "history_exact_match": report_valid,
                    "repair_attempted": report_repair_started,
                },
            })

            expected = expected_resolution_event(prior)
            parsed_lines: list[dict[str, Any]] = []
            malformed_log = False
            audit_repair_started = False
            matches: list[dict[str, Any]] = []
            audit_valid = False
            with self.events.synchronized():
                try:
                    raw_lines = self.events.path.read_text(encoding="utf-8").splitlines()
                except FileNotFoundError:
                    raw_lines = []
                except OSError:
                    raw_lines = []
                    malformed_log = True
                for line in raw_lines:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_log = True
                        break
                    if not isinstance(value, dict):
                        malformed_log = True
                        break
                    parsed_lines.append(value)
                matches = [item for item in parsed_lines if item.get("run_id") == resolution_id]
                audit_valid = (
                    not malformed_log
                    and len(matches) == 1
                    and audit_matches(matches[0], expected)
                )
                if not audit_valid and repair and not malformed_log:
                    audit_repair_started = True
                    try:
                        retained = [
                            item for item in parsed_lines if item.get("run_id") != resolution_id
                        ]
                        retained.append(expected)
                        encoded = b"".join(
                            (json.dumps(item, sort_keys=True) + "\n").encode("utf-8")
                            for item in retained
                        )
                        atomic_write_bytes(self.events.path, encoded)
                        repaired = [
                            json.loads(line)
                            for line in self.events.path.read_text(encoding="utf-8").splitlines()
                        ]
                        matches = [
                            item for item in repaired if item.get("run_id") == resolution_id
                        ]
                        audit_valid = len(matches) == 1 and audit_matches(matches[0], expected)
                    except (OSError, json.JSONDecodeError, ConveyorError) as exc:
                        repair_failure = repair_failure or (
                            f"applied resolution audit repair failed: {type(exc).__name__}"
                        )
                elif malformed_log and repair:
                    audit_repair_started = True
                    repair_failure = repair_failure or "applied resolution audit log is malformed"
            checks.append({
                "validator": "applied_resolution_audit_matches_history",
                "passed": audit_valid,
                "evidence": {
                    "path": str(self.events.path),
                    "matching_event_count": len(matches),
                    "full_provenance_matches": audit_valid,
                    "malformed_log": malformed_log,
                    "repair_attempted": audit_repair_started,
                },
            })
            original_gate = prior.get("original_gate") or {}
            if original_gate.get("classification") == "integration_planning_baseline_approval":
                cycle_path = RepositoryInspector(project.repository).cycle_state_path()
                cycle = self.cycle_store.read(cycle_path)
                provenance = prior.get("planning_baseline_provenance") or {}
                expected_baseline = {
                    "schema_version": 1,
                    "commit": original_gate.get("candidate_validated_planning_commit"),
                    "previous_validated_commit": original_gate.get("previous_last_validated_commit"),
                    "evidence_fingerprint": provenance.get("evidence_fingerprint"),
                    "reconciliation_run": (original_gate.get("planning_baseline_evidence") or {}).get("reconciliation_run"),
                    "validated_at": prior.get("timestamp"),
                    "approval_resolution_id": prior.get("resolution_id"),
                    "approval_resolution_fingerprint": prior.get("resolution_fingerprint"),
                }
                cycle_valid = bool(
                    cycle
                    and cycle.get("current_phase") == "integration_ready"
                    and cycle.get("validated_planning_baseline") == expected_baseline
                    and cycle.get("human_decision_required") is None
                    and cycle.get("integration_gate") is None
                )
                repair_attempted = False
                if not cycle_valid and repair and cycle and (
                    cycle.get("accepted_feature_commit") == original_gate.get("accepted_feature_commit")
                    and (cycle.get("human_decision_required") or {}).get("gate_id") == original_gate.get("gate_id")
                ):
                    repair_attempted = True
                    prior_session = cycle.get("integration_session_id")
                    prior_sessions = list(cycle.get("prior_integration_session_ids") or [])
                    if isinstance(prior_session, str) and prior_session not in prior_sessions:
                        prior_sessions.append(prior_session)
                    cycle.update({
                        "validated_planning_baseline": expected_baseline,
                        "integration_status": "ready",
                        "integration_gate": None,
                        "human_decision_required": None,
                        "failure_classification": None,
                        "retry_exhausted": False,
                        "next_safe_action": "milestone_integration",
                        "stop_reason": None,
                        "prior_integration_session_ids": prior_sessions,
                        "integration_session_id": None,
                    })
                    try:
                        self._advance_cycle(
                            cycle_path, cycle, "integration_ready", RepositoryInspector(project.repository),
                            "explicit_human_decision_replay_repaired_integration_ready",
                        )
                        cycle_valid = True
                    except (ConveyorError, OSError, ValueError) as exc:
                        repair_failure = repair_failure or f"integration cycle replay repair failed: {type(exc).__name__}"
                checks.append({
                    "validator": "applied_integration_cycle_matches_resolution",
                    "passed": cycle_valid,
                    "evidence": {"phase": (cycle or {}).get("current_phase"), "repair_attempted": repair_attempted},
                })
            return checks, repair_failure

        persisted = self.load_project_state(project)
        newer_active_gate = active_gate_is_newer(persisted)
        prior = None if newer_active_gate else prior_resolution(persisted)

        inspector = RepositoryInspector(project.repository)
        writer = inspect_repository_writer_lock(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ),
            project.repository,
        )
        reservation = self._launch_lock(project, inspector)
        reservation_status = reservation.status(None)
        if prior is not None and persisted.get("current_state") != "human_decision_required":
            artifact_checks: list[dict[str, Any]] = []
            repair_failure: str | None = None
            acquired_for_repair = False
            if dry_run:
                artifact_checks, repair_failure = validate_and_repair_applied_artifacts(
                    prior, repair=False
                )
            elif reservation_status.exists:
                repair_failure = "controller reservation appeared before applied-artifact repair"
                artifact_checks = [{
                    "validator": "applied_artifact_repair_reservation_acquired",
                    "passed": False,
                    "evidence": {"controller_reservation_exists": True},
                }]
            else:
                try:
                    reservation.acquire(make_lock_record(
                        project_id=project.project_id,
                        repository_identity=inspector.identity()["repository_id"],
                        run_id=resolution_id,
                        current_feature=None,
                        current_phase="human_decision_artifact_repair",
                    ))
                    acquired_for_repair = True
                    reloaded = self.load_project_state(project)
                    reloaded_newer_gate = active_gate_is_newer(reloaded)
                    try:
                        reloaded_prior = (
                            None if reloaded_newer_gate else prior_resolution(reloaded)
                        )
                    except RecoveryError:
                        reloaded_prior = None
                    state_identity_valid = bool(
                        isinstance(reloaded, dict)
                        and reloaded.get("current_state") != "human_decision_required"
                        and reloaded_prior == prior
                    )
                    if not state_identity_valid:
                        active_gate = (
                            reloaded.get("human_decision_required")
                            if isinstance(reloaded, dict)
                            else None
                        )
                        repair_failure = (
                            "project human-decision state changed before applied-artifact repair"
                        )
                        artifact_checks = [{
                            "validator": "applied_resolution_state_unchanged_under_reservation",
                            "passed": False,
                            "evidence": {
                                "state_exists": isinstance(reloaded, dict),
                                "current_state": (
                                    reloaded.get("current_state")
                                    if isinstance(reloaded, dict)
                                    else None
                                ),
                                "active_gate_id": (
                                    active_gate.get("gate_id")
                                    if isinstance(active_gate, dict)
                                    else None
                                ),
                                "newer_active_gate": reloaded_newer_gate,
                                "resolution_identity_matches": reloaded_prior == prior,
                            },
                        }]
                    else:
                        prior = reloaded_prior
                        artifact_checks, repair_failure = validate_and_repair_applied_artifacts(
                            prior, repair=True
                        )
                except LockError:
                    repair_failure = "controller reservation race prevented applied-artifact repair"
                    artifact_checks = [{
                        "validator": "applied_artifact_repair_reservation_acquired",
                        "passed": False,
                        "evidence": {"controller_reservation_race": True},
                    }]
                finally:
                    if acquired_for_repair:
                        reservation.release(resolution_id)
            artifacts_valid = bool(artifact_checks) and all(
                item.get("passed") is True for item in artifact_checks
            )
            if artifacts_valid and not dry_run:
                self._ack_reconciled_human_materialization(
                    project, resolution_id=resolution_id
                )
            report = dict(prior)
            report.update({
                "outcome": "already_resolved" if artifacts_valid else "resolution_rejected",
                "applied": False,
                "already_applied": True,
                "state_would_be_written": False,
                "state_written": False,
                "application_repository_written": False,
                "evidence_results": artifact_checks,
                "evidence_validators_executed": [item["validator"] for item in artifact_checks],
                "rejection_reasons": [
                    item["validator"] for item in artifact_checks if not item.get("passed")
                ],
                "artifact_repair_failure": repair_failure,
                "artifact_repair_required": not artifacts_valid,
                "dry_run": dry_run,
                "next_normal_action": (
                    "milestone_integration"
                    if artifacts_valid and (prior.get("original_gate") or {}).get("classification") == "integration_planning_baseline_approval"
                    else ("queue_reconciliation" if artifacts_valid else "human_resolution_artifact_repair")
                ),
                "safe_next_action": f"scripts/conveyor status --project {project.project_id}",
            })
            return report
        evaluation = evaluate_human_resolution(
            project=project,
            persisted=persisted,
            reason=reason,
            inspector=inspector,
            writer_lock_exists=writer.exists,
            controller_reservation_exists=reservation_status.exists,
            reason_rejected_sensitive=reason_rejected_sensitive,
        )
        if prior is not None and isinstance(persisted, dict) and persisted.get("current_state") == "human_decision_required":
            evaluation["checks"].append({
                "validator": "applied_resolution_not_reactivated",
                "passed": False,
                "evidence": {"resolution_already_recorded": True, "current_state": "human_decision_required"},
            })
            evaluation["accepted"] = False
        gate_value = evaluation.get("gate") or gate or None
        rejection_reasons = [
            item["validator"] for item in evaluation["checks"] if not item["passed"]
        ]
        timestamp = utc_now()
        report: dict[str, Any] = {
            "schema_version": 1,
            "outcome": "resolution_accepted" if evaluation["accepted"] else "resolution_rejected",
            "project_id": project.project_id,
            "resolution_id": resolution_id,
            "resolution_fingerprint": fingerprint,
            "timestamp": timestamp,
            "user_provided_reason": reason,
            "reason_present": bool(reason),
            "actor_classification": "explicit_user_approval",
            "original_gate": gate_value,
            "original_gate_fingerprint": evaluation.get("gate_fingerprint"),
            "planning_baseline_provenance": evaluation.get("planning_baseline_provenance"),
            "expected_repository_identity": (
                {
                    "repository_id": gate.get("expected_repository_id"),
                    "path_fingerprint": gate.get("expected_path_fingerprint"),
                }
                if gate
                else None
            ),
            "expected_milestone": gate.get("expected_milestone"),
            "expected_branch": gate.get("expected_branch") or gate.get("feature_branch"),
            "expected_milestone_head": gate.get("expected_head") or gate.get("milestone_head"),
            "evidence_validators_executed": [item["validator"] for item in evaluation["checks"]],
            "evidence_results": evaluation["checks"],
            "previous_state": (
                persisted.get("current_state") if persisted else project.current_state
            ),
            "approved_next_state": gate.get("approved_next_state"),
            "state_transition_path": [
                "human_decision_required",
                gate.get("approved_next_state", "queue_reconciliation"),
            ],
            "resolution_would_be_accepted": evaluation["accepted"],
            "rejection_reasons": rejection_reasons,
            "applied": False,
            "state_would_be_written": bool(evaluation["accepted"] and not dry_run),
            "state_written": False,
            "dry_run": dry_run,
            "application_repository_written": False,
            "audit_log_location": audit_log_location,
            "report_location": str(accepted_report_path),
            "next_normal_action": (
                "milestone_integration"
                if evaluation["accepted"] and gate.get("classification") == "integration_planning_baseline_approval"
                else ("queue_reconciliation" if evaluation["accepted"] else "human_decision_required")
            ),
            "next_sessions": (
                []
                if gate.get("classification") == "integration_planning_baseline_approval"
                else (["product-architect (planning-only)", "$feature-inventory"]
                if evaluation["accepted"] else [])
            ),
            "feature_factory_would_launch": False,
            "product_architect_would_launch": False if gate.get("classification") == "integration_planning_baseline_approval" else bool(evaluation["accepted"]),
            "feature_inventory_would_launch": False if gate.get("classification") == "integration_planning_baseline_approval" else bool(evaluation["accepted"]),
            "milestone_integrator_would_launch": bool(
                evaluation["accepted"] and gate.get("classification") == "integration_planning_baseline_approval"
            ),
            "session_launched_during_resolution": False,
            "safe_next_action": (
                f"scripts/conveyor reconcile --project {project.project_id} "
                "--resolve-human-decision --reason <approved-reason>"
                if dry_run and evaluation["accepted"]
                else f"scripts/conveyor status --project {project.project_id}"
            ),
        }

        def persist_structured_rejection(
            value: dict[str, Any], *, exact_reason: str | None = None
        ) -> dict[str, Any]:
            failed = [
                item["validator"]
                for item in value.get("evidence_results", [])
                if item.get("passed") is not True
            ]
            value.update({
                "outcome": "resolution_rejected",
                "resolution_would_be_accepted": False,
                "rejection_reasons": failed,
                "applied": False,
                "state_would_be_written": False,
                "state_written": False,
                "application_repository_written": False,
                "next_normal_action": "human_decision_required",
                "next_sessions": [],
                "safe_next_action": f"scripts/conveyor status --project {project.project_id}",
            })
            if exact_reason:
                value["rejection_detail"] = exact_reason
            rejected_run_id = f"{resolution_id}-rejected-{uuid.uuid4().hex[:8]}"
            rejected_path = self._report_path(
                report_root, rejected_run_id, "human-decision-resolution.json"
            )
            value["report_location"] = str(rejected_path)
            atomic_write_json(rejected_path, value)
            self.events.append(run_event(
                run_id=rejected_run_id,
                project_id=project.project_id,
                repository_fingerprint=inspector.identity()["path_fingerprint"],
                source="deterministic_script",
                milestone=project.active_milestone,
                branch=inspector.current_branch,
                commit=inspector.head,
                command_category="human_decision_resolution",
                result="resolution_rejected",
                validation_outcome="failed",
                previous_state=value["previous_state"],
                next_state=value["previous_state"],
                stop_reason=exact_reason or ", ".join(failed),
                human_gate={
                    "gate_id": gate.get("gate_id"),
                    "resolution_id": resolution_id,
                    "resolution_fingerprint": fingerprint,
                    "report_location": str(rejected_path),
                },
            ))
            return value

        if dry_run:
            return report

        if not evaluation["accepted"]:
            return persist_structured_rejection(report)

        try:
            reservation.acquire(make_lock_record(
                project_id=project.project_id,
                repository_identity=inspector.identity()["repository_id"],
                run_id=resolution_id,
                current_feature=None,
                current_phase="human_decision_resolution",
            ))
        except LockError:
            race_check = {
                "validator": "controller_resolution_reservation_acquired",
                "passed": False,
                "evidence": {"reservation_race": True},
            }
            report["evidence_results"].append(race_check)
            report["evidence_validators_executed"].append(race_check["validator"])
            return persist_structured_rejection(
                report,
                exact_reason="controller reservation appeared before human-resolution apply",
            )
        try:
            persisted = self.load_project_state(project)
            newer_active_gate = active_gate_is_newer(persisted)
            prior = None if newer_active_gate else prior_resolution(persisted)
            if prior is not None and persisted.get("current_state") != "human_decision_required":
                artifact_checks, repair_failure = validate_and_repair_applied_artifacts(
                    prior, repair=True
                )
                artifacts_valid = all(item.get("passed") is True for item in artifact_checks)
                result = dict(prior)
                result.update({
                    "outcome": "already_resolved" if artifacts_valid else "resolution_rejected",
                    "applied": False,
                    "already_applied": True,
                    "state_would_be_written": False,
                    "state_written": False,
                    "application_repository_written": False,
                    "evidence_results": artifact_checks,
                    "evidence_validators_executed": [
                        item["validator"] for item in artifact_checks
                    ],
                    "rejection_reasons": [
                        item["validator"] for item in artifact_checks if not item.get("passed")
                    ],
                    "artifact_repair_failure": repair_failure,
                    "artifact_repair_required": not artifacts_valid,
                })
                return result
            writer = inspect_repository_writer_lock(
                inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                ),
                project.repository,
            )
            owned_status = reservation.status(resolution_id)
            if not owned_status.exists or not owned_status.owned_by_run or owned_status.ambiguous:
                ownership_check = {
                    "validator": "controller_resolution_reservation_owned",
                    "passed": False,
                    "evidence": {
                        "exists": owned_status.exists,
                        "owned_by_run": owned_status.owned_by_run,
                        "ambiguous": owned_status.ambiguous,
                    },
                }
                report["evidence_results"].append(ownership_check)
                report["evidence_validators_executed"].append(ownership_check["validator"])
                return persist_structured_rejection(
                    report,
                    exact_reason="human-resolution controller reservation ownership became ambiguous",
                )
            revalidated = evaluate_human_resolution(
                project=project,
                persisted=persisted,
                reason=reason,
                inspector=inspector,
                writer_lock_exists=writer.exists,
                controller_reservation_exists=False,
                reason_rejected_sensitive=reason_rejected_sensitive,
            )
            if prior is not None and isinstance(persisted, dict) and persisted.get("current_state") == "human_decision_required":
                revalidated["checks"].append({
                    "validator": "applied_resolution_not_reactivated",
                    "passed": False,
                    "evidence": {"resolution_already_recorded": True, "current_state": "human_decision_required"},
                })
                revalidated["accepted"] = False
            if not revalidated["accepted"]:
                failed = [
                    item["validator"] for item in revalidated["checks"] if not item["passed"]
                ]
                report.update({
                    "evidence_results": revalidated["checks"],
                    "evidence_validators_executed": [
                        item["validator"] for item in revalidated["checks"]
                    ],
                })
                return persist_structured_rejection(
                    report,
                    exact_reason=(
                        "human-resolution evidence changed under controller reservation: "
                        + ", ".join(failed)
                    ),
                )
            document = persisted or self._project_document(
                project, None, inspector.identity()["path_fingerprint"]
            )
            if document.get("repository_fingerprint") != inspector.identity()["path_fingerprint"]:
                state_check = {
                    "validator": "controller_state_repository_fingerprint_matches",
                    "passed": False,
                    "evidence": {"matches": False},
                }
                report["evidence_results"].append(state_check)
                report["evidence_validators_executed"].append(state_check["validator"])
                return persist_structured_rejection(
                    report,
                    exact_reason="human-resolution state belongs to another repository fingerprint",
                )
            identity = inspector.identity()
            inspector.ensure_runtime_ignored()
            state_root = self.root / "state/projects" / project.project_id
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl", project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            kernel_resolution_next_state = (
                "integration_ready"
                if gate.get("classification") == "integration_planning_baseline_approval"
                else str(gate["approved_next_state"])
            )
            resolution_adapter = HumanDecisionResolutionAdapter(
                allowed_paths=(), commit_subject="factory: resolve human decision",
                next_state=kernel_resolution_next_state,
            )
            resolution_kernel = WorkflowKernel(
                project=project, ledger=ledger,
                projection=ProjectionEngine(ledger, state_root / "projection-cache.json"),
                lease=WorkflowWriterLease(inspector.writer_lock_path(
                    self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
                )),
            )
            ledger_events = ledger.read()
            replay_transaction_id = next((
                event["transaction_id"] for event in ledger_events
                if event["event_type"] == "TransactionStarted"
                and event["workflow_type"] == WorkflowType.HUMAN_DECISION_RESOLUTION.value
                and event["payload"].get("run_id") == resolution_id
                and any(
                    candidate["transaction_id"] == event["transaction_id"]
                    and candidate["event_type"] == "TransactionCompleted"
                    for candidate in ledger_events
                )
                and any(
                    candidate["transaction_id"] == event["transaction_id"]
                    and candidate["event_type"] == "CheckpointRecorded"
                    and candidate["payload"].get("checkpoint") == "compatibility_materialization_pending"
                    for candidate in ledger_events
                )
                and not any(
                    candidate["transaction_id"] == event["transaction_id"]
                    and candidate["event_type"] == "CheckpointRecorded"
                    and candidate["payload"].get("checkpoint") == "compatibility_materialization_acknowledged"
                    for candidate in ledger_events
                )
            ), None)
            if replay_transaction_id is not None:
                transaction = resolution_kernel.restore(replay_transaction_id)
            else:
                transaction = resolution_kernel.begin(
                    workflow_type=WorkflowType.HUMAN_DECISION_RESOLUTION,
                    milestone=project.active_milestone, feature_id=None,
                    run_id=resolution_id, policy=resolution_adapter.policy,
                    start_evidence={
                        "resolved_gate_id": str(gate["gate_id"]),
                        "resolved_gate_fingerprint": gate_fingerprint(gate),
                    },
                )
                resolution_kernel.acquire_lease(); resolution_kernel.capture_snapshot()
                session_id = f"deterministic-human-resolution:{transaction.transaction_id}"
                resolution_kernel.session_launched(session_id)
                envelope = SessionResultEnvelope.from_dict({
                    "schema_version": 1, "workflow_type": "human_decision_resolution",
                    "classification": "HUMAN_GATE_RESOLVED", "project_id": project.project_id,
                    "repository_identity": identity["repository_id"],
                    "transaction_id": transaction.transaction_id, "run_id": resolution_id,
                    "session_id": session_id, "starting_branch": transaction.starting_branch,
                    "starting_commit": transaction.starting_head, "current_commit": transaction.starting_head,
                    "feature_id": None, "changed_paths": [],
                    "evidence": {"gate_id": gate.get("gate_id"), "resolution_fingerprint": fingerprint},
                    "next_state": kernel_resolution_next_state,
                })
                resolution_kernel.accept_result(envelope)
                resolution_kernel.record_file_mutation_boundary()
                resolution_kernel.validate(
                    authority=CommandAuthority(), command_results=(),
                    semantic_validator=resolution_adapter.semantic_validate,
                )
                resolution_kernel.finalize()
                ledger.append(
                    event_type="HumanGateResolved",
                    transaction_id=transaction.transaction_id,
                    workflow_type=WorkflowType.HUMAN_DECISION_RESOLUTION,
                    payload={
                        "gate_id": str(gate["gate_id"]),
                        "gate_fingerprint": gate_fingerprint(gate),
                        "raised_transaction_id": None,
                        "resolution_id": resolution_id,
                        "resolution_fingerprint": fingerprint,
                    },
                )

            def persist_after_terminal(kernel_projection: dict[str, Any]) -> None:
                report.update({
                    "outcome": "resolution_accepted", "resolution_would_be_accepted": True,
                    "applied": True, "state_would_be_written": True, "state_written": True,
                    "dry_run": False, "rejection_reasons": [],
                    "evidence_validators_executed": [item["validator"] for item in revalidated["checks"]],
                    "evidence_results": revalidated["checks"],
                    "persisted_state": str(gate["approved_next_state"]),
                })
                history = document.setdefault("human_decision_history", [])
                if not isinstance(history, list):
                    raise RecoveryError("persisted human-decision history is malformed")
                if not any(
                    isinstance(item, dict)
                    and isinstance(item.get("resolution"), dict)
                    and item["resolution"].get("resolution_id") == resolution_id
                    for item in history
                ):
                    history.append({"gate": dict(gate), "gate_fingerprint": revalidated.get("gate_fingerprint"), "resolution": report})
                if (
                    document.get("current_state") != str(gate["approved_next_state"])
                    or document.get("human_decision_required") is not None
                ):
                    self._transition_project(
                        project, document, str(gate["approved_next_state"]), run_id=resolution_id,
                        checkpoint="explicit_human_decision_resolved", feature=None,
                        stop_reason="Explicit human decision resolved; queue reconciliation is next.",
                        human_gate=None,
                        state_evidence={
                            "classification": "explicit_human_decision_resolution",
                            "gate_id": gate.get("gate_id"), "gate_fingerprint": revalidated.get("gate_fingerprint"),
                            "resolution_id": resolution_id, "resolution_fingerprint": fingerprint,
                            "report_location": str(accepted_report_path), "audit_log_location": audit_log_location,
                        },
                        event_human_gate={
                            "gate_id": gate.get("gate_id"), "gate_fingerprint": revalidated.get("gate_fingerprint"),
                            "resolution_id": resolution_id, "resolution_fingerprint": fingerprint,
                            "actor_classification": "explicit_user_approval", "report_location": str(accepted_report_path),
                        },
                        event_branch=inspector.current_branch, event_commit=inspector.head,
                        event_command_category="human_decision_resolution", event_validation_outcome="passed",
                        kernel_owned=True,
                        kernel_projection={"transaction_id": transaction.transaction_id, **kernel_projection},
                    )
                if gate.get("classification") == "integration_planning_baseline_approval":
                    cycle_path = inspector.cycle_state_path()
                    cycle = self.cycle_store.read(cycle_path)
                    if cycle is None or cycle.get("human_decision_required", {}).get("gate_id") != gate.get("gate_id"):
                        raise RecoveryError("resolved integration gate is not bound to the persisted repository cycle")
                    prior_session = cycle.get("integration_session_id")
                    prior_sessions = list(cycle.get("prior_integration_session_ids") or [])
                    if isinstance(prior_session, str) and prior_session not in prior_sessions:
                        prior_sessions.append(prior_session)
                    cycle.update({
                        "validated_planning_baseline": {
                            "schema_version": 1, "commit": gate.get("candidate_validated_planning_commit"),
                            "previous_validated_commit": gate.get("previous_last_validated_commit"),
                            "evidence_fingerprint": (revalidated.get("planning_baseline_provenance") or {}).get("evidence_fingerprint"),
                            "reconciliation_run": (gate.get("planning_baseline_evidence") or {}).get("reconciliation_run"),
                            "validated_at": report["timestamp"], "approval_resolution_id": resolution_id,
                            "approval_resolution_fingerprint": fingerprint,
                        },
                        "integration_status": "ready", "integration_gate": None,
                        "human_decision_required": None, "failure_classification": None,
                        "retry_exhausted": False, "next_safe_action": "milestone_integration",
                        "stop_reason": None, "prior_integration_session_ids": prior_sessions,
                        "integration_session_id": None,
                        "resume_instructions": f"scripts/conveyor run --project {project.project_id} --mode {project.automation_mode}",
                    })
                    if cycle.get("current_phase") != "integration_ready":
                        self._advance_cycle(cycle_path, cycle, "integration_ready", inspector, "explicit_human_decision_resolved_integration_ready")
                atomic_write_json(accepted_report_path, report)

            resolution_kernel.complete(
                evidence={
                    "human_gate_resolution_id": resolution_id,
                    "selected_feature": gate.get("feature_id") or gate.get("feature"),
                    "accepted_feature_commit": gate.get("accepted_feature_commit"),
                },
                terminal_callback=persist_after_terminal,
                materialization={
                    "kind": "human_decision_resolution",
                    "resolution_id": resolution_id,
                    "resolution_fingerprint": fingerprint,
                    "gate_id": gate.get("gate_id"),
                    "approved_next_state": str(gate["approved_next_state"]),
                    "report_location": str(accepted_report_path),
                },
            )
            return report
        finally:
            reservation.release(resolution_id)

    def run_project(self, project: Project, mode: str, *, dry_run: bool = False) -> dict[str, Any]:
        cache_recovery = (
            self._cache_binding_recovery_plan(project, allow_missing=True)
            if mode in {"resume", "audit"}
            else None
        )
        if cache_recovery is not None:
            if dry_run or mode == "audit":
                return cache_recovery
            return self._apply_cache_binding_recovery(project, cache_recovery)
        if mode == "resume":
            planning_plan = self.project_plan(project)
            planning_recovery = planning_plan.get("planning_finalization_recovery")
            if (
                planning_plan.get("proposed_next_action") == "planning_finalization"
                and isinstance(planning_recovery, dict)
            ):
                if dry_run:
                    return planning_plan
                return self._recover_terminal_planning_finalization(
                    project,
                    expected_plan=planning_recovery,
                )
        kernel_recovery = self._kernel_recovery_preflight(
            project, apply=(mode == "resume" and not dry_run)
        )
        if kernel_recovery is not None:
            return kernel_recovery
        if mode == "resume":
            finalization_recovery = self._integration_finalization_recovery_context(project)
            if finalization_recovery is not None:
                evidence = finalization_recovery["evidence"]
                if dry_run:
                    return {
                        "project_id": project.project_id,
                        "proposed_next_action": "integration_finalization_recovery",
                        "feature": evidence["feature_id"],
                        "recovered_transaction_id": evidence["blocked_transaction_id"],
                        "resulting_feature_commit": evidence["resulting_feature_commit"],
                        "integrating_metadata_commit": evidence[
                            "integrating_metadata_commit"
                        ],
                        "model_session_launched": False,
                        "application_repository_written": False,
                    }
                return self._recover_integration_finalization(
                    project, str(uuid.uuid4()), finalization_recovery
                )
        context = self._authoritative_execution_context(project)
        if context is not None and not dry_run and mode != "audit":
            authoritative, executable, _ = context
            if authoritative.get("active_transaction") is not None:
                return {
                    "project_id": project.project_id,
                    "outcome": "human_decision_required",
                    "reason": "active kernel transaction requires exact recovery",
                    "kernel_projection": authoritative,
                }
            effective = replace(project, current_state=str(authoritative["current_state"]))
            action = authoritative.get("allowed_next_action")
            run_id = str(uuid.uuid4())
            inspector = RepositoryInspector(project.repository)
            project_state = self._project_document(
                effective, run_id, inspector.identity()["path_fingerprint"]
            )
            project_state["current_state"] = effective.current_state
            project_state["current_feature"] = authoritative.get("current_feature")
            reloaded = self._authoritative_execution_context(project)
            if reloaded is None:
                raise ProjectionError("authoritative projection disappeared before dispatch")
            reloaded_projection, reloaded_executable, _ = reloaded
            if (
                reloaded_projection.get("projection_fingerprint")
                != authoritative.get("projection_fingerprint")
                or reloaded_executable.to_dict() != executable.to_dict()
            ):
                raise ProjectionError("execution plan changed before dispatch")
            executable.validate_against(reloaded_projection)
            authoritative = reloaded_projection
            if action == "milestone_integration":
                return self._execute_projected_integration(
                    effective, mode, run_id, reloaded_executable
                )
            if action == "feature_cycle":
                evidence = self._execute_feature(
                    effective,
                    mode,
                    run_id,
                    project_state,
                    expected_execution_plan=reloaded_executable,
                )
                return {
                    "project_id": project.project_id,
                    "outcome": evidence.get("outcome") or (
                        "one_feature_integrated" if mode == "one_feature" else "feature_integrated"
                    ),
                    **evidence,
                }
            if action == "queue_reconciliation":
                return {
                    "project_id": project.project_id,
                    **self._execute_queue_reconciliation(
                        effective,
                        mode,
                        run_id,
                        project_state,
                        expected_execution_plan=executable,
                    ),
                }
            if action == "milestone_gate":
                return {
                    "project_id": project.project_id,
                    **self._execute_milestone_gate(
                        effective,
                        mode,
                        run_id,
                        project_state,
                        expected_execution_plan=executable,
                    ),
                }
            if action in {
                "human_decision_resolution",
                "human_merge_approval",
                "verify_consistency",
            }:
                return {
                    "project_id": project.project_id,
                    "outcome": action,
                    "kernel_projection": authoritative,
                    "human_gate": authoritative.get("human_gate"),
                    "session_launch_performed": False,
                    "application_repository_written": False,
                }
            raise ProjectionError(f"unsupported authoritative dispatch action: {action}")
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
            if assessment.classification == "required_integration_validation_failed":
                document = persisted
                if assessment.would_persist:
                    if document is None:
                        document = self._project_document(
                            effective, None, plan["repository_path_fingerprint"]
                        )
                    document = self._persist_startup_reconciliation(
                        effective,
                        document,
                        assessment,
                        run_id=f"recovery-{uuid.uuid4()}",
                    )
                return {
                    "project_id": project.project_id,
                    "outcome": "validation_failed",
                    "current_state": (document or {}).get("current_state", "validation_failed"),
                    "required_command_failures": (
                        assessment.evidence.get("durable_integration_success") or {}
                    ).get("required_command_failures", []),
                    "plan": plan,
                }
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
        if plan["proposed_next_action"] == "planning_finalization":
            kernel_recovery = plan.get("planning_finalization_recovery")
            if isinstance(kernel_recovery, dict):
                return self._recover_terminal_planning_finalization(
                    project,
                    expected_plan=kernel_recovery,
                )
            transaction = plan.get("current_planning_transaction") or {}
            if transaction.get("recovery_exact_expectation_recorded") is not False:
                return self.recover_planning_transaction(
                    project,
                    run_id=str(transaction["run_id"]),
                    expected_starting_head=str(transaction["planning_start_commit"]),
                    expected_diff_fingerprint=str(transaction["diff_fingerprint"]),
                    expected_changed_paths=list(transaction["changed_paths"]),
                    expected_session_id=str(transaction["session_id"]),
                    dry_run=False,
                )
            return {
                "project_id": project.project_id,
                "outcome": "planning_recovery_required",
                "plan": plan,
                "next_action": "scripts/conveyor recover-planning --help",
            }
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
            if action == "milestone_integration":
                return self.resume_project(effective, run_id=run_id)
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
                        expected_feature_id=str(plan.get("selected_feature") or ""),
                    )
                    if evidence.get("outcome") == "human_decision_required":
                        return {"project_id": project.project_id, **evidence}
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
