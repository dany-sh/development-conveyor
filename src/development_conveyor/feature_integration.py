"""Ledger-authenticated adapter for deterministic feature integration."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .contracts import TransactionState, WorkflowType
from .errors import ConveyorError, IntegrationPlanError
from .integration_executor import (
    acceptance_metadata_from_ledger,
    build_integration_plan,
    execute_integration_plan,
    inspect_two_refs,
    persist_integration_plan,
)
from .kernel import MilestoneIntegrationAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease


def _dependency_preflight(
    *, project: Project, target: RepositoryInspector, feature: dict[str, Any]
) -> list[dict[str, str]]:
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    milestone = queue.milestone(project.active_milestone or "")
    integrated = set((milestone or {}).get("integrated_features") or [])
    verified: list[dict[str, str]] = []
    for dependency_id in feature.get("dependencies") or []:
        dependency = queue.feature(str(dependency_id))
        commit = dependency.get("integrated_commit") if dependency else None
        if (
            not dependency
            or dependency.get("status") != "integrated"
            or dependency_id not in integrated
            or not isinstance(commit, str)
            or target.rev_parse(commit, check=False) != commit
            or not target.is_ancestor(commit, target.head)
        ):
            raise IntegrationPlanError(
                f"unresolved cumulative integration dependency: {dependency_id}"
            )
        verified.append({"feature_id": str(dependency_id), "integrated_commit": commit})
    return verified


def integrate_feature(
    *,
    controller_root: Path,
    project: Project,
    feature_id: str,
    acceptance_result: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Authenticate acceptance, resolve the configured target, and integrate once."""

    source = RepositoryInspector(project.repository)
    feature_queue = FeatureQueue.from_location(
        project.repository, project.queue_location
    )
    feature = feature_queue.feature(feature_id)
    branch = feature.get("branch") if isinstance(feature, dict) else None
    if (
        not isinstance(feature, dict)
        or feature.get("milestone") != project.active_milestone
        or not isinstance(branch, str)
        or not branch
        or not project.milestone_branch
    ):
        raise IntegrationPlanError("integration feature identity is incomplete")
    accepted_commit = source.rev_parse(branch, check=False)
    if not isinstance(accepted_commit, str):
        raise IntegrationPlanError("accepted feature ref is missing")
    acceptance_ledger_path = (
        controller_root / "state/projects" / project.project_id / "evidence-ledger.jsonl"
    )
    source_identity = source.identity()
    acceptance_ledger = EvidenceLedger(
        acceptance_ledger_path,
        project_id=project.project_id,
        repository_identity=source_identity["repository_id"],
        repository_path_fingerprint=source_identity["path_fingerprint"],
    )
    acceptance_ledger.verify()
    acceptance = acceptance_metadata_from_ledger(
        ledger_path=acceptance_ledger_path,
        feature_id=feature_id,
        accepted_commit=accepted_commit,
    )
    if acceptance_result is not None:
        checks = {
            "transaction": acceptance_result.get("transaction_id")
            == acceptance["transaction_id"],
            "commit": acceptance_result.get("accepted_commit") == accepted_commit,
            "feature": acceptance_result.get("feature_id") == feature_id,
        }
        if not all(checks.values()):
            raise IntegrationPlanError(
                "structured acceptance result disagrees with ledger authority"
            )
    target_path = source.branch_worktree(project.milestone_branch)
    if target_path is None:
        raise IntegrationPlanError(
            "configured milestone integration worktree is missing"
        )
    target_path = target_path.resolve()
    if target_path == project.repository.resolve():
        raise IntegrationPlanError(
            "feature and integration worktrees must remain distinct"
        )
    target = RepositoryInspector(target_path)
    if (
        target.current_branch != project.milestone_branch
        or not target.is_clean
        or any(target.git_operation_state().values())
        or source.rev_parse(project.milestone_branch, check=False) != target.head
    ):
        raise IntegrationPlanError(
            "configured integration worktree is dirty, drifting, or unregistered"
        )
    if acceptance.get("milestone_base") != target.head:
        return {
            "schema_version": 1,
            "phase": "integration",
            "outcome": "reconciliation_required",
            "classification": "RECONCILIATION_REQUIRED",
            "project_id": project.project_id,
            "feature_id": feature_id,
            "accepted_commit": accepted_commit,
            "accepted_milestone_base": acceptance.get("milestone_base"),
            "integration_worktree": str(target_path),
            "integration_branch": project.milestone_branch,
            "integration_head": target.head,
        }
    dependencies = _dependency_preflight(
        project=replace(project, repository=target_path),
        target=target,
        feature=feature,
    )
    target_project = replace(project, repository=target_path)
    target_identity = target.identity()
    state_root = controller_root / "state/projects" / project.project_id
    integration_ledger = EvidenceLedger(
        state_root / "integration-evidence-ledger.jsonl",
        project_id=project.project_id,
        repository_identity=target_identity["repository_id"],
        repository_path_fingerprint=target_identity["path_fingerprint"],
    )
    projection = ProjectionEngine(
        integration_ledger, state_root / "integration-projection-cache.json"
    )
    lease = WorkflowWriterLease(target.writer_lock_path())
    evidence = inspect_two_refs(
        repository=target_path,
        controller_project_id=project.project_id,
        feature_id=feature_id,
        feature_branch=branch,
        accepted_commit=accepted_commit,
        milestone_id=str(project.active_milestone),
        milestone_branch=project.milestone_branch,
        pre_integration_head=target.head,
        queue_path=project.queue_location,
        acceptance_metadata=acceptance,
    )
    authorized_paths = tuple(
        sorted(set(evidence["accepted_changed_paths"]) | set(evidence["metadata_paths"]))
    )
    adapter = MilestoneIntegrationAdapter(
        allowed_paths=authorized_paths,
        commit_subject=f"factory: integrate {feature_id}",
        next_state="feature_integrated",
    )
    kernel = WorkflowKernel(
        project=target_project,
        ledger=integration_ledger,
        projection=projection,
        lease=lease,
    )
    exclusion = target.ensure_milestone_integration_runtime_ignored()
    transaction = None
    try:
        transaction = kernel.begin_for_branch(
            workflow_type=WorkflowType.MILESTONE_INTEGRATION,
            target_branch=project.milestone_branch,
            milestone=project.active_milestone,
            feature_id=feature_id,
            run_id=run_id or f"integrate-{uuid.uuid4().hex}",
            policy=adapter.policy,
            expected_starting_branch=project.milestone_branch,
            expected_starting_head=target.head,
        )
        kernel.acquire_lease()
        kernel.prepare_starting_branch()
        kernel.capture_snapshot()
        lease_record = lease.bind_controller_plan(
            transaction_id=transaction.transaction_id,
            repository_identity=target_identity["repository_id"],
            controller_project_id=project.project_id,
            adapter_project_id=str(evidence["adapter_project_id"]),
            feature_branch=branch,
            accepted_commit=accepted_commit,
        )
        acceptance_lines = acceptance_ledger_path.read_bytes().splitlines()
        acceptance_tail = json.loads(acceptance_lines[-1])
        current_projection = projection.rebuild(persist_cache=False)
        plan = build_integration_plan(
            repository=target_path,
            controller_project_id=project.project_id,
            transaction_id=transaction.transaction_id,
            run_id=transaction.run_id,
            feature_id=feature_id,
            feature_branch=branch,
            accepted_commit=accepted_commit,
            milestone_id=str(project.active_milestone),
            milestone_branch=project.milestone_branch,
            pre_integration_head=target.head,
            queue_path=project.queue_location,
            projection_fingerprint=current_projection["projection_fingerprint"],
            ledger_sequence=len(acceptance_lines),
            ledger_fingerprint=acceptance_tail["fingerprint"],
            controller_ledger_path=acceptance_ledger_path,
            lease_identity=lease_record.to_dict(),
            runtime_exclusion=exclusion,
            verified_evidence=evidence,
        )
        plan_path = persist_integration_plan(
            state_root / "integration-plans" / f"{transaction.transaction_id}.json",
            plan,
        )
        result = execute_integration_plan(plan_path)
        kernel.accept_deterministic_integration_result(plan, result)
        if result["classification"] == "SEMANTIC_CONFLICT":
            kernel.block(
                state=TransactionState.HUMAN_DECISION_REQUIRED,
                classification="SEMANTIC_CONFLICT",
                next_state="human_decision_required",
                human_gate=result.get("human_gate"),
            )
        elif result["classification"] == "VALIDATION_FAILED":
            kernel.block(
                state=TransactionState.TERMINAL_FAILURE,
                classification="VALIDATION_FAILED",
                next_state="validation_failed",
                reference="deterministic_validation_failed",
            )
        else:
            kernel.complete(
                classification="INTEGRATED",
                evidence={
                    "accepted_feature_commit": accepted_commit,
                    "accepted_transaction_id": acceptance["transaction_id"],
                    "integrated_commit": result["resulting_feature_commit"],
                    "integration_status": "passed",
                    "integration_plan_fingerprint": plan["plan_fingerprint"],
                    "integration_plan_path": str(plan_path),
                    "integration_runtime": result["runtime"],
                    "verified_dependencies": dependencies,
                    "model_session_launched": False,
                },
            )
        return {
            "schema_version": 1,
            "phase": "integration",
            "outcome": (
                "integrated"
                if result["classification"] == "INTEGRATED"
                else result["classification"].lower()
            ),
            "project_id": project.project_id,
            "feature_id": feature_id,
            "acceptance_transaction": acceptance["transaction_id"],
            "integration_transaction": transaction.transaction_id,
            "integration_worktree": str(target_path),
            "integration_branch": project.milestone_branch,
            "plan_path": str(plan_path),
            "verified_dependencies": dependencies,
            "complete_suite_invocations": 0,
            "release_validation_invocations": 0,
            **result,
        }
    except Exception:
        if transaction is not None and lease.read() is not None:
            try:
                kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="TERMINAL_INTEGRATION_FAILURE",
                    next_state="validation_failed",
                    reference="integrate_feature_adapter_failure",
                )
            except Exception:
                pass
        raise
