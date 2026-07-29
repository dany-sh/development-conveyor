"""One deterministic acceptance path for immutable implementation commits."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    TERMINAL_STATES,
    MutationPolicy,
    TransactionState,
    WorkflowType,
    fingerprint,
)
from .errors import TransactionError
from .kernel import WorkflowKernel
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowWriterLease


SUPPORTED_ACCEPTANCE_TIERS = ("feature", "milestone", "release")


def _implementation_identity(
    inspector: RepositoryInspector, commit: str
) -> dict[str, str]:
    resolved = inspector.rev_parse(commit, check=False)
    tree = inspector.rev_parse(f"{commit}^{{tree}}", check=False)
    if resolved != commit or not isinstance(tree, str) or not tree:
        raise TransactionError("acceptance implementation commit or tree is invalid")
    return {"commit": commit, "tree": tree}


def tier_evidence_from_records(
    *,
    project: Project,
    implementation_commit: str,
    tier: str,
    commands: Iterable[dict[str, Any]],
    valid: bool,
    complete_suite_invocations: int = 0,
    release_validation_invocations: int = 0,
) -> dict[str, Any]:
    """Build the canonical candidate-bound evidence consumed by acceptance."""

    if tier not in SUPPORTED_ACCEPTANCE_TIERS:
        raise TransactionError(f"unsupported acceptance evidence tier: {tier}")
    inspector = RepositoryInspector(project.repository)
    identity = inspector.identity()
    implementation = _implementation_identity(inspector, implementation_commit)
    milestone = inspector.rev_parse(project.milestone_branch or "", check=False)
    if not isinstance(milestone, str):
        raise TransactionError("acceptance evidence lacks the milestone ref")
    changed_paths = tuple(
        sorted(
            line
            for line in inspector.git(
                [
                    "diff",
                    "--name-only",
                    milestone,
                    implementation_commit,
                    "--",
                ]
            ).stdout.splitlines()
            if line
        )
    )
    return {
        "schema_version": 1,
        "tier": tier,
        "valid": bool(valid),
        "project_id": project.project_id,
        "repository_identity": identity["repository_id"],
        "repository_path_fingerprint": identity["path_fingerprint"],
        "implementation": implementation,
        "changed_paths": list(changed_paths),
        "commands": list(commands),
        "complete_suite_invocations": complete_suite_invocations,
        "release_validation_invocations": release_validation_invocations,
    }


def validate_tier_evidence(
    *,
    project: Project,
    implementation_commit: str,
    implementation_tree: str,
    tier_evidence: dict[str, Any],
    required_tier: str,
) -> dict[str, Any]:
    """Authenticate tier evidence against the exact candidate without execution."""

    if required_tier not in SUPPORTED_ACCEPTANCE_TIERS:
        raise TransactionError(f"unsupported acceptance transition tier: {required_tier}")
    if not isinstance(tier_evidence, dict):
        raise TransactionError("acceptance tier evidence must be an object")
    inspector = RepositoryInspector(project.repository)
    identity = inspector.identity()
    implementation = _implementation_identity(inspector, implementation_commit)
    commands = tier_evidence.get("commands")
    checks = {
        "schema": tier_evidence.get("schema_version") == 1,
        "tier": tier_evidence.get("tier") == required_tier,
        "valid": tier_evidence.get("valid") is True,
        "project": tier_evidence.get("project_id") == project.project_id,
        "repository": tier_evidence.get("repository_identity")
        == identity["repository_id"],
        "path": tier_evidence.get("repository_path_fingerprint")
        == identity["path_fingerprint"],
        "commit": (tier_evidence.get("implementation") or {}).get("commit")
        == implementation_commit,
        "tree": (tier_evidence.get("implementation") or {}).get("tree")
        == implementation_tree
        == implementation["tree"],
        "commands": isinstance(commands, list) and bool(commands),
        "complete_suite_count": type(
            tier_evidence.get("complete_suite_invocations")
        )
        is int,
        "release_validation_count": type(
            tier_evidence.get("release_validation_invocations")
        )
        is int,
    }
    if required_tier != "release":
        checks["ordinary_no_complete_suite"] = (
            tier_evidence.get("complete_suite_invocations") == 0
        )
        checks["ordinary_no_release_validation"] = (
            tier_evidence.get("release_validation_invocations") == 0
        )
    if isinstance(commands, list):
        checks["command_records"] = all(
            isinstance(record, dict)
            and isinstance(record.get("argv") or record.get("command"), list)
            and type(record.get("exit_status")) is int
            for record in commands
        )
        if required_tier != "release":
            checks["commands_passed"] = all(
                record.get("exit_status") == 0 for record in commands
            )
        authoritative_classifications = {
            "configured_required_validation",
            "kernel_required_finalization",
            "workflow_required_evidence",
        }
        configured_groups = {
            f"{required_tier}_tests",
            "compile",
            "validate_config",
            "diff_check",
        }
        if required_tier == "release":
            configured_groups.add("release_complete_suite_1")
        checks["tier_command_authority"] = (
            all(
                record.get("classification") in authoritative_classifications
                for record in commands
            )
            or all(record.get("group") in configured_groups for record in commands)
        )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise TransactionError(
            "candidate-bound tier evidence failed authentication: "
            + ", ".join(failed)
        )
    return {
        "tier": required_tier,
        "fingerprint": fingerprint(tier_evidence),
        "complete_suite_invocations": tier_evidence[
            "complete_suite_invocations"
        ],
        "release_validation_invocations": tier_evidence[
            "release_validation_invocations"
        ],
        "command_count": len(commands),
    }


def inspect_acceptance_candidate(
    *,
    project: Project,
    feature_id: str,
    feature_branch: str,
    milestone_base: str,
    implementation_commit: str,
    implementation_tree: str,
    tier_evidence: dict[str, Any],
    required_tier: str = "feature",
) -> dict[str, Any]:
    """Fail closed on every repository and evidence binding without mutation."""

    inspector = RepositoryInspector(project.repository)
    identity = inspector.identity()
    if (
        not inspector.is_clean
        or any(inspector.git_operation_state().values())
        or inspector.current_branch != feature_branch
        or inspector.head != implementation_commit
        or inspector.rev_parse(feature_branch, check=False) != implementation_commit
        or inspector.rev_parse(project.milestone_branch or "", check=False)
        != milestone_base
    ):
        raise TransactionError(
            "acceptance requires the exact clean candidate and milestone refs"
        )
    if not inspector.is_ancestor(milestone_base, implementation_commit):
        raise TransactionError(
            "acceptance candidate does not descend from the milestone base"
        )
    merges = inspector.git(
        ["rev-list", "--merges", f"{milestone_base}..{implementation_commit}"]
    ).stdout.splitlines()
    if merges:
        raise TransactionError("acceptance candidate history must be linear")
    implementation = _implementation_identity(inspector, implementation_commit)
    if implementation["tree"] != implementation_tree:
        raise TransactionError("acceptance implementation tree changed")
    queue_text = inspector.file_at_commit(
        implementation_commit, project.queue_location
    )
    adapter_text = inspector.file_at_commit(
        implementation_commit, ".factory/project.yaml"
    )
    milestone_adapter_text = inspector.file_at_commit(
        milestone_base, ".factory/project.yaml"
    )
    if queue_text is None or adapter_text is None or milestone_adapter_text is None:
        raise TransactionError("candidate lacks queue or adapter identity")
    try:
        queue = FeatureQueue(json.loads(queue_text))
        adapter = json.loads(adapter_text)
        milestone_adapter = json.loads(milestone_adapter_text)
        live_adapter = json.loads(
            inspector.safe_worktree_file_bytes(".factory/project.yaml")
        )
    except (ValueError, TypeError) as exc:
        raise TransactionError("candidate queue or adapter is malformed") from exc
    adapter_ids = [
        (document.get("project") or {}).get("id")
        if isinstance(document, dict)
        else None
        for document in (adapter, milestone_adapter, live_adapter)
    ]
    if (
        any(not isinstance(value, str) or not value for value in adapter_ids)
        or len(set(adapter_ids)) != 1
    ):
        raise TransactionError("candidate application identity disagrees")
    feature = queue.feature(feature_id)
    if not isinstance(feature, dict):
        raise TransactionError("candidate queue lacks the accepted feature")
    if feature.get("milestone") != project.active_milestone:
        raise TransactionError("candidate feature milestone disagrees")
    candidate_paths = tuple(
        sorted(
            line
            for line in inspector.git(
                [
                    "diff",
                    "--name-only",
                    milestone_base,
                    implementation_commit,
                    "--",
                ]
            ).stdout.splitlines()
            if line
        )
    )
    evidence_paths = tier_evidence.get("changed_paths")
    if evidence_paths != list(candidate_paths):
        raise TransactionError(
            "candidate changed paths disagree with authenticated tier evidence"
        )
    evidence = validate_tier_evidence(
        project=project,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        tier_evidence=tier_evidence,
        required_tier=required_tier,
    )
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "repository_identity": identity["repository_id"],
        "repository_path_fingerprint": identity["path_fingerprint"],
        "feature_id": feature_id,
        "feature_branch": feature_branch,
        "milestone_id": project.active_milestone,
        "milestone_branch": project.milestone_branch,
        "milestone_base": milestone_base,
        "implementation": implementation,
        "changed_paths": list(candidate_paths),
        "tier_evidence": evidence,
    }


def supersede_incomplete_acceptance_start(
    *,
    project: Project,
    ledger: EvidenceLedger,
    lease: WorkflowWriterLease,
    transaction_id: str,
    retry_transaction_id: str,
    retry_run_id: str,
    feature_id: str,
    feature_branch: str,
    implementation_commit: str,
) -> dict[str, Any]:
    """Terminalize one exact start-only acceptance that failed before mutation."""

    ledger.verify()
    events = ledger.read()
    transaction_events = [
        event for event in events if event["transaction_id"] == transaction_id
    ]
    terminal = next(
        (
            event
            for event in transaction_events
            if event["event_type"] in TERMINAL_EVENT_TYPES
        ),
        None,
    )
    if terminal is not None:
        payload = terminal["payload"]
        if (
            terminal["event_type"] == "TransactionSuperseded"
            and payload.get("classification")
            == "ACCEPTANCE_PRELEASE_FAILURE_SUPERSEDED"
            and payload.get("superseded_by") == retry_transaction_id
            and payload.get("retry_run_id") == retry_run_id
        ):
            return {
                "transaction_id": transaction_id,
                "terminal_event": terminal["event_type"],
                "terminal_sequence": terminal["sequence"],
                "terminal_fingerprint": terminal["fingerprint"],
                "idempotent": True,
            }
        raise TransactionError(
            "incomplete acceptance recovery target already has another terminal outcome"
        )
    incomplete = {
        event["transaction_id"]
        for event in events
        if not any(
            candidate["transaction_id"] == event["transaction_id"]
            and candidate["event_type"] in TERMINAL_EVENT_TYPES
            for candidate in events
        )
    }
    if incomplete != {transaction_id}:
        raise TransactionError(
            "incomplete acceptance recovery requires one exact active transaction"
        )
    if (
        len(transaction_events) != 1
        or transaction_events[0]["event_type"] != "TransactionStarted"
        or transaction_events[0]["workflow_type"]
        != WorkflowType.FEATURE_ACCEPTANCE.value
    ):
        raise TransactionError(
            "incomplete acceptance recovery requires one start-only acceptance"
        )
    if lease.read() is not None:
        raise TransactionError(
            "incomplete acceptance recovery requires an absent writer lease"
        )
    start = transaction_events[0]
    payload = start["payload"]
    inspector = RepositoryInspector(project.repository)
    snapshot = capture_repository_snapshot(project)
    previous_commit = payload.get("starting_head")
    same_amended_lineage = bool(
        isinstance(previous_commit, str)
        and (
            previous_commit == implementation_commit
            or inspector.rev_parse(f"{previous_commit}^", check=False)
            == inspector.rev_parse(f"{implementation_commit}^", check=False)
        )
    )
    checks = {
        "project": start["project_id"] == project.project_id,
        "repository": start["repository_identity"]
        == ledger.repository_identity,
        "worktree": start["repository_path_fingerprint"]
        == ledger.repository_path_fingerprint
        == snapshot.repository_path_fingerprint,
        "feature": payload.get("feature_id") == feature_id,
        "branch": payload.get("starting_branch")
        == feature_branch
        == snapshot.branch,
        "candidate_lineage": same_amended_lineage,
        "current_candidate": snapshot.head == implementation_commit,
        "queue": payload.get("starting_queue_fingerprint")
        == snapshot.queue_fingerprint,
        "tracked_diff": payload.get("starting_tracked_diff_fingerprint")
        == snapshot.tracked_diff_fingerprint,
        "untracked": payload.get("starting_untracked_fingerprint")
        == snapshot.untracked_fingerprint,
        "clean": snapshot.clean,
        "git_operation": not any(snapshot.git_operations.values()),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise TransactionError(
            "incomplete acceptance recovery identity failed: "
            + ", ".join(failed)
        )
    superseded = ledger.append(
        event_type="TransactionSuperseded",
        transaction_id=transaction_id,
        workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
        payload={
            "classification": "ACCEPTANCE_PRELEASE_FAILURE_SUPERSEDED",
            "terminal_state": TransactionState.SUPERSEDED.value,
            "superseded_by": retry_transaction_id,
            "reference": retry_run_id,
            "retry_run_id": retry_run_id,
            "next_state": "feature_review",
            "failure_stage": "lease_acquisition_revalidation",
            "failure_reason": "linked_worktree_lease_identity_validation",
            "acceptance_record_created": False,
            "previous_implementation_commit": previous_commit,
            "retry_implementation_commit": implementation_commit,
            "terminal_snapshot": snapshot.to_dict(),
        },
    )
    ledger.append(
        event_type="RecoveryApplied",
        transaction_id=transaction_id,
        workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
        payload={
            "recovery_transaction_id": retry_transaction_id,
            "classification": "pre_acceptance_start_superseded",
            "retry_run_id": retry_run_id,
            "mutation_performed": False,
        },
    )
    return {
        "transaction_id": transaction_id,
        "terminal_event": superseded["event_type"],
        "terminal_sequence": superseded["sequence"],
        "terminal_fingerprint": superseded["fingerprint"],
        "idempotent": False,
    }


def accept_feature(
    *,
    controller_root: Path,
    project: Project,
    feature_id: str,
    feature_branch: str,
    milestone_base: str,
    implementation_commit: str,
    implementation_tree: str,
    tier_evidence: dict[str, Any],
    run_id: str,
    required_tier: str = "feature",
    recover_incomplete_transaction: str | None = None,
) -> dict[str, Any]:
    """Record acceptance in controller authority without changing an app ref."""

    candidate = inspect_acceptance_candidate(
        project=project,
        feature_id=feature_id,
        feature_branch=feature_branch,
        milestone_base=milestone_base,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        tier_evidence=tier_evidence,
        required_tier=required_tier,
    )
    inspector = RepositoryInspector(project.repository)
    inspector.ensure_runtime_ignored()
    state_root = controller_root / "state/projects" / project.project_id
    ledger = EvidenceLedger(
        state_root / "evidence-ledger.jsonl",
        project_id=project.project_id,
        repository_identity=candidate["repository_identity"],
        repository_path_fingerprint=candidate["repository_path_fingerprint"],
    )
    lease = WorkflowWriterLease(inspector.writer_lock_path())
    retry_transaction_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"{candidate['repository_identity']}:{feature_id}:"
                f"{implementation_commit}:{run_id}:feature-acceptance"
            ),
        )
    )
    recovered = None
    if recover_incomplete_transaction is not None:
        recovered = supersede_incomplete_acceptance_start(
            project=project,
            ledger=ledger,
            lease=lease,
            transaction_id=recover_incomplete_transaction,
            retry_transaction_id=retry_transaction_id,
            retry_run_id=run_id,
            feature_id=feature_id,
            feature_branch=feature_branch,
            implementation_commit=implementation_commit,
        )
    existing = [
        event
        for event in ledger.read()
        if event["event_type"] == "TransactionCompleted"
        and event["workflow_type"] == WorkflowType.FEATURE_ACCEPTANCE.value
        and event["payload"].get("feature_id") == feature_id
        and event["payload"].get("accepted_feature_commit")
        == implementation_commit
    ]
    if existing:
        raise TransactionError(
            "candidate already has immutable controller acceptance metadata"
        )
    projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
    kernel = WorkflowKernel(
        project=project,
        ledger=ledger,
        projection=projection,
        lease=lease,
    )
    transaction = kernel.begin(
        workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
        milestone=str(candidate["milestone_id"]),
        feature_id=feature_id,
        run_id=run_id,
        policy=MutationPolicy(()),
        transaction_id=retry_transaction_id,
        expected_starting_branch=feature_branch,
        expected_starting_head=implementation_commit,
    )
    kernel.acquire_lease()
    try:
        kernel.capture_snapshot()
        plan_fingerprint = fingerprint(candidate)
        transaction.transition(TransactionState.RESULT_PENDING)
        ledger.append(
            event_type="DeterministicExecutionStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "execution_mode": "immutable_implementation_acceptance",
                "plan_fingerprint": plan_fingerprint,
                "implementation": candidate["implementation"],
                "tier_evidence": candidate["tier_evidence"],
                "model_session_launched": False,
            },
        )
        transaction.transition(TransactionState.VALIDATING)
        ledger.append(
            event_type="DeterministicResultAccepted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "classification": "FEATURE_ACCEPTED",
                "plan_fingerprint": plan_fingerprint,
                "implementation": candidate["implementation"],
                "changed_paths": candidate["changed_paths"],
                "model_session_launched": False,
            },
        )
        ledger.append(
            event_type="ValidationStarted",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "implementation": candidate["implementation"],
                "tier": required_tier,
            },
        )
        ledger.append(
            event_type="ValidationPassed",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "implementation": candidate["implementation"],
                "tier_evidence": candidate["tier_evidence"],
                "commands": tier_evidence["commands"],
                "warnings": [],
            },
        )
        transaction.transition(TransactionState.FINALIZING)
        kernel.final_commit = implementation_commit
        ledger.append(
            event_type="CommitFinalized",
            transaction_id=transaction.transaction_id,
            workflow_type=transaction.workflow_type,
            payload={
                "commit": implementation_commit,
                "tree": implementation_tree,
                "parent": milestone_base,
                "changed_paths": [],
                "no_change": True,
                "implementation_ref_unchanged": True,
            },
        )
        transaction.next_project_state = "integration_pending"
        completion = kernel.complete(
            classification="FEATURE_ACCEPTED",
            evidence={
                "accepted_feature_commit": implementation_commit,
                "candidate_implementation_commit": implementation_commit,
                "accepted_implementation_tree": implementation_tree,
                "implementation_ref": feature_branch,
                "implementation_ref_unchanged": True,
                "invoking_worktree": str(project.repository),
                "invoking_worktree_fingerprint": candidate[
                    "repository_path_fingerprint"
                ],
                "acceptance_metadata_surface": "controller_evidence_ledger",
                "acceptance_tier": required_tier,
                "tier_evidence_fingerprint": candidate["tier_evidence"][
                    "fingerprint"
                ],
                "accepted_changed_paths": candidate["changed_paths"],
                "integration_status": "pending",
            },
        )
    except Exception:
        if transaction.current_state not in TERMINAL_STATES:
            try:
                kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="FEATURE_VALIDATION_FAILED",
                    next_state="validation_failed",
                    reference="deterministic_acceptance_failed",
                )
            except Exception:
                if kernel.lease.read() is not None:
                    kernel._release_owned_lease(transaction)
        raise
    if (
        inspector.rev_parse(feature_branch, check=False) != implementation_commit
        or inspector.head != implementation_commit
        or not inspector.is_clean
    ):
        raise TransactionError("implementation ref changed during acceptance")
    integrity = ledger.verify()
    terminal_event = next(
        event
        for event in reversed(ledger.read())
        if event["transaction_id"] == transaction.transaction_id
        and event["event_type"] == "TransactionCompleted"
    )
    authority = {
        "surface": "controller_evidence_ledger",
        "transaction_id": transaction.transaction_id,
        "ledger_sequence": terminal_event["sequence"],
        "ledger_fingerprint": terminal_event["fingerprint"],
        "feature_id": feature_id,
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "feature_branch": feature_branch,
        "implementation_ref_unchanged": True,
        "invoking_worktree": str(project.repository),
        "invoking_worktree_fingerprint": candidate[
            "repository_path_fingerprint"
        ],
        "integration_status": "pending",
        "acceptance_tier": required_tier,
        "tier_evidence_fingerprint": candidate["tier_evidence"]["fingerprint"],
        "accepted_changed_paths": candidate["changed_paths"],
    }
    authority["metadata_fingerprint"] = fingerprint(authority)
    return {
        "schema_version": 1,
        "outcome": "feature_accepted",
        "status": "accepted",
        "integration_status": "pending",
        "feature_id": feature_id,
        "accepted_implementation_commit": implementation_commit,
        "accepted_implementation_tree": implementation_tree,
        "implementation_ref": feature_branch,
        "implementation_ref_unchanged": True,
        "acceptance_metadata": authority,
        "recovered_incomplete_acceptance": recovered,
        "complete_suite_invocations": candidate["tier_evidence"][
            "complete_suite_invocations"
        ],
        "release_validation_invocations": candidate["tier_evidence"][
            "release_validation_invocations"
        ],
        "projection": completion["projection"],
    }
