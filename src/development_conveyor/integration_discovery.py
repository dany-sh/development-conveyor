"""Read-only separation of accepted-feature and integration-target authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import ProjectionError
from .repository import RepositoryInspector


@dataclass(frozen=True)
class IntegrationDiscovery:
    accepted_commit: str
    accepted_tree: str
    feature_branch: str
    feature_worktree: str | None
    acceptance_transaction: str | None
    integration_worktree: str
    integration_branch: str
    integration_starting_commit: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _controller_acceptance(
    events: Iterable[dict[str, Any]],
    *,
    feature_id: str,
    accepted_commit: str,
) -> dict[str, Any] | None:
    values = list(events)
    completions = [
        event
        for event in values
        if event.get("event_type") == "TransactionCompleted"
        and event.get("workflow_type") == "feature_acceptance"
        and (event.get("payload") or {}).get("acceptance_metadata_surface")
        == "controller_evidence_ledger"
        and (event.get("payload") or {}).get("feature_id") == feature_id
        and (event.get("payload") or {}).get("accepted_feature_commit")
        == accepted_commit
    ]
    if not completions:
        return None
    if len(completions) != 1:
        raise ProjectionError(
            "integration discovery requires one exact controller acceptance transaction"
        )
    completion = completions[0]
    transaction_id = completion.get("transaction_id")
    finalized = [
        event
        for event in values
        if event.get("transaction_id") == transaction_id
        and event.get("event_type") == "CommitFinalized"
    ]
    if len(finalized) != 1:
        raise ProjectionError(
            "controller acceptance lacks one exact finalized implementation"
        )
    payload = completion.get("payload") or {}
    commit = finalized[0].get("payload") or {}
    checks = {
        "commit": commit.get("commit") == accepted_commit,
        "tree": payload.get("accepted_implementation_tree") == commit.get("tree"),
        "ref_unchanged": payload.get("implementation_ref_unchanged") is True,
        "integration_pending": payload.get("integration_status") == "pending",
        "milestone_base": isinstance(commit.get("parent"), str)
        and bool(commit.get("parent")),
        "feature_branch": isinstance(payload.get("implementation_ref"), str)
        and bool(payload.get("implementation_ref")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ProjectionError(
            "controller acceptance/candidate mismatch: " + ", ".join(failed)
        )
    return {
        "transaction_id": transaction_id,
        "accepted_tree": commit["tree"],
        "feature_branch": payload["implementation_ref"],
        "feature_worktree": payload.get("invoking_worktree"),
        "milestone_base": commit["parent"],
    }


def discover_integration_target(
    *,
    repository: Path,
    feature_id: str,
    feature_branch: str,
    accepted_commit: str,
    milestone_branch: str,
    expected_starting_commit: str | None,
    ledger_events: Iterable[dict[str, Any]] = (),
) -> IntegrationDiscovery:
    """Bind an accepted ref to the distinct clean worktree owning the target ref."""

    feature_root = repository.expanduser().resolve()
    source = RepositoryInspector(feature_root)
    acceptance = _controller_acceptance(
        ledger_events,
        feature_id=feature_id,
        accepted_commit=accepted_commit,
    )
    accepted_tree = source.rev_parse(f"{accepted_commit}^{{tree}}", check=False)
    if not isinstance(accepted_tree, str):
        raise ProjectionError("accepted implementation commit is missing")
    if source.rev_parse(feature_branch, check=False) != accepted_commit:
        raise ProjectionError("acceptance ledger/candidate feature ref mismatch")
    if acceptance is not None:
        if (
            acceptance["accepted_tree"] != accepted_tree
            or acceptance["feature_branch"] != feature_branch
        ):
            raise ProjectionError("acceptance ledger/candidate mismatch")
        if (
            expected_starting_commit is not None
            and expected_starting_commit != acceptance["milestone_base"]
        ):
            raise ProjectionError(
                "projection and acceptance ledger disagree on the milestone baseline"
            )
        expected_starting_commit = acceptance["milestone_base"]

    target_path = source.branch_worktree(milestone_branch)
    if target_path is None:
        raise ProjectionError(
            "configured milestone integration worktree is missing"
        )
    target = RepositoryInspector(target_path)
    target_identity = target.identity()
    source_identity = source.identity()
    checks = {
        "repository": target_identity["repository_id"]
        == source_identity["repository_id"],
        "branch": target.current_branch == milestone_branch,
        "clean": target.is_clean,
        "git_operation": not any(target.git_operation_state().values()),
        "branch_head": source.rev_parse(milestone_branch, check=False) == target.head,
        "expected_head": expected_starting_commit in {None, target.head},
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ProjectionError(
            "milestone integration target rejected: " + ", ".join(failed)
        )

    accepted_from_worktree = (
        Path(str(acceptance["feature_worktree"])).expanduser().resolve()
        if acceptance is not None and acceptance.get("feature_worktree")
        else None
    )
    feature_worktree = source.branch_worktree(feature_branch)
    if accepted_from_worktree is not None and feature_worktree is not None:
        if accepted_from_worktree != feature_worktree:
            raise ProjectionError(
                "acceptance ledger feature worktree does not match the live feature ref"
            )
    if feature_worktree is not None:
        if feature_worktree == target_path:
            raise ProjectionError(
                "accepted feature worktree cannot be the integration target"
            )
    elif target_path == feature_root and target.current_branch != milestone_branch:
        raise ProjectionError(
            "feature worktree cannot substitute for the configured integration target"
        )

    return IntegrationDiscovery(
        accepted_commit=accepted_commit,
        accepted_tree=accepted_tree,
        feature_branch=feature_branch,
        feature_worktree=str(feature_worktree) if feature_worktree else None,
        acceptance_transaction=(
            str(acceptance["transaction_id"]) if acceptance is not None else None
        ),
        integration_worktree=str(target_path),
        integration_branch=milestone_branch,
        integration_starting_commit=target.head,
    )
