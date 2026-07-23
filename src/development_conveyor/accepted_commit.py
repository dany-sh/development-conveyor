"""Deterministic construction and validation of immutable accepted commits."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import IntegrationPlanError, QueueError, TransactionError
from .integration_executor import inspect_two_refs
from .logging import atomic_write_bytes, atomic_write_json
from .queue import FeatureQueue
from .redaction import redact_text
from .registry import Project
from .repository import RepositoryInspector


ACCEPTANCE_FLAGS = ("tests_passed", "review_passed", "documentation_current")


def acceptance_metadata_paths(project: Project, feature: dict[str, Any]) -> tuple[str, ...]:
    """Return the narrow repository metadata set used by accepted-feature convention."""

    specification = feature.get("spec") or feature.get("specification") or feature.get(
        "spec_path"
    )
    if not isinstance(specification, str) or not specification:
        raise TransactionError("accepted feature lacks a repository specification path")
    required = {
        project.queue_location,
        specification,
        "docs/CURRENT_STATUS.md",
        "docs/FEATURE_CATALOG.md",
        "docs/RUN_LOG.md",
    }
    missing = sorted(
        relative
        for relative in required
        if not (project.repository / relative).is_file()
    )
    if missing:
        raise TransactionError(
            "accepted-feature metadata paths are missing: " + ", ".join(missing)
        )
    return tuple(sorted(required))


def _replace_catalog_status(
    text: str, *, feature_id: str, accepted_status: str = "Accepted"
) -> str:
    lines = text.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith(f"| {feature_id} |")
    ]
    if len(matches) != 1:
        raise TransactionError(
            f"feature catalog must contain exactly one {feature_id} row"
        )
    index = matches[0]
    fields = [field.strip() for field in lines[index].strip().strip("|").split("|")]
    if len(fields) < 3 or fields[0] != feature_id:
        raise TransactionError(f"feature catalog row for {feature_id} is malformed")
    fields[2] = accepted_status
    lines[index] = "| " + " | ".join(fields) + " |"
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + suffix


def _replace_factory_status(text: str, *, feature_id: str) -> str:
    replacement = (
        "- Factory status: Integration pending — deterministic acceptance metadata "
        "is committed with the validated implementation"
    )
    rendered, count = re.subn(
        r"(?m)^- Factory status:.*$",
        replacement,
        text,
        count=1,
    )
    if count != 1:
        raise TransactionError(
            f"{feature_id} specification lacks exactly one Factory status line"
        )
    return rendered.replace("- [ ] ", "- [x] ")


def _replace_current_status(
    text: str, *, feature_id: str, title: str
) -> str:
    replacement = (
        f"- Active feature: {feature_id} — {title} "
        "(accepted; milestone integration pending)"
    )
    rendered, count = re.subn(
        rf"(?m)^- Active feature: {re.escape(feature_id)}\b.*$",
        replacement,
        text,
        count=1,
    )
    if count != 1:
        raise TransactionError(
            f"docs/CURRENT_STATUS.md lacks exactly one active {feature_id} line"
        )
    rendered = rendered.replace(
        f"{feature_id} implementation is pending controller acceptance; "
        "host-level staged-app relaunch and accessibility checks remain outside this session.",
        f"{feature_id} is accepted from the recorded controller validation; "
        "milestone integration is pending.",
    )
    return rendered


def materialize_acceptance_metadata(
    *,
    project: Project,
    feature_id: str,
    feature_branch: str,
    milestone_base: str,
    candidate_commit: str,
    recovery: bool,
) -> tuple[str, ...]:
    """Apply only deterministic accepted-feature metadata to a clean candidate tree."""

    inspector = RepositoryInspector(project.repository)
    if (
        inspector.current_branch != feature_branch
        or inspector.head != candidate_commit
        or inspector.rev_parse(feature_branch, check=False) != candidate_commit
        or inspector.tracked_changed_paths()
        or inspector.untracked_file_hashes()
        or any(inspector.git_operation_state().values())
    ):
        raise TransactionError(
            "accepted-feature metadata requires the exact clean candidate branch"
        )
    queue_path = project.repository / project.queue_location
    try:
        queue_document = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("candidate feature queue is unavailable or invalid") from exc
    queue = FeatureQueue(queue_document, queue_path, project.repository)
    feature = queue.feature(feature_id)
    if feature is None:
        raise TransactionError(f"candidate queue lacks feature {feature_id}")
    if feature.get("status") in {"integrated", "done"}:
        raise TransactionError("integrated feature cannot be finalized again")
    bindings = {
        "branch": feature.get("branch"),
        "integration_base_commit": feature.get("integration_base_commit"),
        "accepted_commit": feature.get("accepted_commit"),
    }
    if bindings["branch"] not in {None, feature_branch}:
        raise TransactionError("candidate queue feature branch conflicts with the plan")
    if bindings["integration_base_commit"] not in {None, milestone_base}:
        raise TransactionError("candidate queue integration base conflicts with the plan")
    if bindings["accepted_commit"] not in {None, "SELF", candidate_commit}:
        raise TransactionError("candidate queue accepted commit conflicts with the plan")

    raw_features = queue_document.get("features")
    matches = [
        item
        for item in raw_features
        if isinstance(item, dict) and item.get("id") == feature_id
    ] if isinstance(raw_features, list) else []
    if len(matches) != 1:
        raise TransactionError(
            f"candidate queue must contain exactly one feature {feature_id}"
        )
    raw_feature = matches[0]
    raw_feature.update(
        {
            "implementation_status": "Completed",
            "status": "integration_pending",
            "integration_status": "pending",
            "branch": feature_branch,
            "integration_base_commit": milestone_base,
            "accepted_commit": "SELF",
            "acceptance": {
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": True,
            },
        }
    )
    atomic_write_json(queue_path, queue_document)

    metadata_paths = acceptance_metadata_paths(project, feature)
    specification = str(
        feature.get("spec") or feature.get("specification") or feature.get("spec_path")
    )
    specification_path = project.repository / specification
    atomic_write_bytes(
        specification_path,
        _replace_factory_status(
            specification_path.read_text(encoding="utf-8"),
            feature_id=feature_id,
        ).encode("utf-8"),
    )

    catalog = project.repository / "docs/FEATURE_CATALOG.md"
    atomic_write_bytes(
        catalog,
        _replace_catalog_status(
            catalog.read_text(encoding="utf-8"), feature_id=feature_id
        ).encode("utf-8"),
    )

    current_status = project.repository / "docs/CURRENT_STATUS.md"
    atomic_write_bytes(
        current_status,
        _replace_current_status(
            current_status.read_text(encoding="utf-8"),
            feature_id=feature_id,
            title=str(feature.get("title") or feature_id),
        ).encode("utf-8"),
    )

    run_log = project.repository / "docs/RUN_LOG.md"
    existing_log = run_log.read_text(encoding="utf-8").rstrip()
    mode = "recovery" if recovery else "normal finalization"
    entry = [
        "",
        f"## {feature_id} deterministic accepted-commit {mode}",
        "",
        f"- Candidate implementation commit: `{candidate_commit}`.",
        f"- Milestone parent: `{milestone_base}`.",
        "- Accepted commit identity: `SELF` in the committed queue snapshot.",
        "- Acceptance flags: tests, review, and documentation passed from controller-owned validation evidence.",
        "- Finalization: zero model sessions; one direct-child accepted commit; milestone integration not performed.",
        "- Authorized acceptance metadata: "
        + ", ".join(f"`{path}`" for path in metadata_paths)
        + ".",
        "",
    ]
    atomic_write_bytes(
        run_log,
        (existing_log + "\n" + "\n".join(entry)).encode("utf-8"),
    )
    observed = tuple(inspector.tracked_changed_paths())
    if (
        not observed
        or not set(observed).issubset(metadata_paths)
        or inspector.untracked_file_hashes()
    ):
        raise TransactionError(
            "acceptance metadata mutation differs from the authorized path set"
        )
    return observed


def _run_git(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = redact_text(result.stderr.strip() or result.stdout.strip())
        raise TransactionError(diagnostic or f"accepted-commit Git operation failed: {arguments!r}")
    return result.stdout.strip()


def _parents(inspector: RepositoryInspector, commit: str) -> tuple[str, ...]:
    return tuple(
        inspector.git(["show", "-s", "--format=%P", commit]).stdout.strip().split()
    )


def _diff_paths(
    inspector: RepositoryInspector, older: str, newer: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            line
            for line in inspector.git(
                ["diff", "--name-only", older, newer, "--"]
            ).stdout.splitlines()
            if line
        )
    )


def _tree_fingerprint(
    inspector: RepositoryInspector, commit: str, excluded: tuple[str, ...]
) -> str:
    excluded_set = set(excluded)
    lines = _run_git(
        inspector.root, ["ls-tree", "-r", "--full-tree", commit]
    ).splitlines()
    digest = hashlib.sha256()
    for line in lines:
        metadata, separator, relative = line.partition("\t")
        if not separator or relative in excluded_set:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(metadata.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_proposed_accepted_metadata(
    *,
    inspector: RepositoryInspector,
    commit: str,
    feature_id: str,
    feature_branch: str,
    milestone_id: str,
    milestone_branch: str,
    milestone_base: str,
    queue_path: str,
) -> dict[str, bool]:
    queue_text = inspector.file_at_commit(commit, queue_path)
    if queue_text is None:
        raise TransactionError("finalized commit lacks the immutable feature queue")
    try:
        queue = FeatureQueue(json.loads(queue_text))
    except (json.JSONDecodeError, QueueError) as exc:
        raise TransactionError("finalized commit queue is invalid") from exc
    feature = queue.feature(feature_id)
    milestone = queue.milestone(milestone_id)
    acceptance = feature.get("acceptance") if isinstance(feature, dict) else None
    checks = {
        "feature": isinstance(feature, dict),
        "implementation_completed": isinstance(feature, dict)
        and feature.get("implementation_status") == "Completed",
        "status": isinstance(feature, dict)
        and feature.get("status") == "integration_pending",
        "integration_status": isinstance(feature, dict)
        and feature.get("integration_status") == "pending",
        "branch": isinstance(feature, dict)
        and feature.get("branch") == feature_branch,
        "integration_base": isinstance(feature, dict)
        and feature.get("integration_base_commit") == milestone_base,
        "accepted_self": isinstance(feature, dict)
        and feature.get("accepted_commit") == "SELF",
        "acceptance": isinstance(acceptance, dict)
        and all(acceptance.get(key) is True for key in ACCEPTANCE_FLAGS),
        "milestone_branch": isinstance(milestone, dict)
        and milestone.get("integration_branch") == milestone_branch,
    }
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        raise TransactionError(
            "proposed accepted commit failed immutable metadata validation: "
            + ", ".join(failed)
        )
    return checks


def finalize_accepted_commit(
    *,
    project: Project,
    controller_project_id: str,
    feature_id: str,
    feature_branch: str,
    milestone_id: str,
    milestone_branch: str,
    milestone_base: str,
    candidate_commit: str,
    metadata_paths: tuple[str, ...],
    commit_subject: str,
) -> dict[str, Any]:
    """Create one sibling commit containing candidate content plus authorized metadata."""

    inspector = RepositoryInspector(project.repository)
    if (
        inspector.current_branch != feature_branch
        or inspector.head != candidate_commit
        or inspector.rev_parse(feature_branch, check=False) != candidate_commit
    ):
        raise TransactionError("candidate branch identity changed before finalization")
    if inspector.rev_parse(milestone_branch, check=False) != milestone_base:
        raise TransactionError("milestone branch changed before accepted finalization")
    if _parents(inspector, candidate_commit) != (milestone_base,):
        raise TransactionError(
            "candidate implementation commit is not the exact direct child of the milestone base"
        )
    if any(inspector.git_operation_state().values()):
        raise TransactionError("Git operation is active before accepted finalization")
    if inspector.untracked_file_hashes():
        raise TransactionError("accepted finalization refuses untracked content")
    observed_metadata = tuple(inspector.tracked_changed_paths())
    if observed_metadata != metadata_paths:
        raise TransactionError(
            "worktree metadata differs from the authorized accepted path set"
        )

    candidate_paths = tuple(sorted(inspector.changed_paths(candidate_commit)))
    if not candidate_paths:
        raise TransactionError("candidate implementation commit has no content")
    candidate_tree_fingerprint = _tree_fingerprint(
        inspector, candidate_commit, metadata_paths
    )
    _run_git(project.repository, ["add", "--", *metadata_paths])
    if tuple(inspector.staged_changed_paths()) != metadata_paths:
        raise TransactionError("staged acceptance metadata differs from authorization")
    tree = _run_git(project.repository, ["write-tree"])
    finalized = _run_git(
        project.repository,
        ["commit-tree", tree, "-p", milestone_base, "-m", commit_subject],
    )
    if _parents(inspector, finalized) != (milestone_base,):
        raise TransactionError("finalized accepted commit has the wrong parent")
    metadata_difference = _diff_paths(inspector, candidate_commit, finalized)
    if metadata_difference != metadata_paths:
        raise TransactionError(
            "candidate and finalized trees differ outside authorized metadata"
        )
    finalized_tree_fingerprint = _tree_fingerprint(
        inspector, finalized, metadata_paths
    )
    if finalized_tree_fingerprint != candidate_tree_fingerprint:
        raise TransactionError(
            "implementation tree changed during accepted-commit finalization"
        )
    accepted_paths = tuple(sorted(inspector.changed_paths(finalized)))
    if not set(candidate_paths).issubset(accepted_paths):
        raise TransactionError(
            "finalized accepted commit does not preserve the candidate implementation"
        )
    proposed_metadata_checks = _validate_proposed_accepted_metadata(
        inspector=inspector,
        commit=finalized,
        feature_id=feature_id,
        feature_branch=feature_branch,
        milestone_id=milestone_id,
        milestone_branch=milestone_branch,
        milestone_base=milestone_base,
        queue_path=project.queue_location,
    )

    _run_git(
        project.repository,
        [
            "update-ref",
            f"refs/heads/{feature_branch}",
            finalized,
            candidate_commit,
        ],
    )
    if (
        inspector.current_branch != feature_branch
        or inspector.head != finalized
        or inspector.rev_parse(feature_branch, check=False) != finalized
        or not inspector.is_clean
    ):
        raise TransactionError(
            "feature branch did not move atomically to the finalized accepted commit"
        )
    try:
        immutable = inspect_two_refs(
            repository=project.repository,
            controller_project_id=controller_project_id,
            feature_id=feature_id,
            feature_branch=feature_branch,
            accepted_commit=finalized,
            milestone_id=milestone_id,
            milestone_branch=milestone_branch,
            pre_integration_head=milestone_base,
            queue_path=project.queue_location,
        )
    except IntegrationPlanError as exc:
        raise TransactionError(
            "finalized commit failed immutable integration metadata validation: "
            + str(exc)
        ) from exc
    return {
        "candidate_implementation_commit": candidate_commit,
        "finalized_accepted_commit": finalized,
        "parent": milestone_base,
        "candidate_changed_paths": list(candidate_paths),
        "accepted_changed_paths": list(accepted_paths),
        "authorized_metadata_paths": list(metadata_paths),
        "candidate_to_finalized_changed_paths": list(metadata_difference),
        "candidate_implementation_tree_fingerprint": candidate_tree_fingerprint,
        "finalized_implementation_tree_fingerprint": finalized_tree_fingerprint,
        "implementation_tree_equivalent": True,
        "branch_movement": {
            "branch": feature_branch,
            "from": candidate_commit,
            "to": finalized,
            "verified": True,
        },
        "immutable_metadata_validation": immutable,
        "proposed_metadata_checks": proposed_metadata_checks,
    }
