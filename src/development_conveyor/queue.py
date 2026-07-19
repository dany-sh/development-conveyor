"""Deterministic feature-queue discovery, validation, normalization, and selection."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import QueueError

COMPLETE_STATUSES = {"integrated", "done"}
ACTIVE_STATUSES = {"in_progress", "review", "accepted", "integration_pending", "integrating"}
SUPPORTED_STATUSES = {
    "proposed", "ready", "in_progress", "review", "accepted", "integration_pending",
    "integrating", "integrated", "failed", "human_decision_required", "blocked", "done",
    "deferred", "rejected",
}
STATUS_ALIASES = {
    "complete": "done",
    "completed": "done",
    "inprogress": "in_progress",
    "pending_integration": "integration_pending",
    "awaiting_human_decision": "human_decision_required",
}


def _yaml_path(parent: str, key: str | int) -> str:
    return f"{parent}[{key}]" if isinstance(key, int) else f"{parent}.{key}"


def resolve_queue_path(repository: Path, configured: str) -> Path:
    """Resolve a registered queue path without permitting repository escape."""

    repository = repository.expanduser().resolve()
    if not isinstance(configured, str) or not configured.strip():
        raise QueueError("$.queue_location: expected a non-empty path")
    expanded = configured.replace("${HOME}", os.environ.get("HOME", ""))
    if "${" in expanded:
        raise QueueError(f"$.queue_location: unresolved environment variable in {configured!r}")
    path = Path(expanded).expanduser()
    candidate = path if path.is_absolute() else repository / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(repository)
    except ValueError as exc:
        raise QueueError(
            f"$.queue_location: resolved path escapes registered repository: {resolved}"
        ) from exc
    if not resolved.exists():
        raise QueueError(f"$.queue_location: queue file is missing: {resolved}")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise QueueError(f"$.queue_location: cannot inspect queue file {resolved}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise QueueError(f"$.queue_location: queue path is not a regular file: {resolved}")
    return resolved


def load_queue(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QueueError(f"$: cannot read queue {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(
            f"$: unsupported queue format in {path}; expected JSON-compatible YAML "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc
    if not isinstance(value, dict):
        raise QueueError("$: expected an object")
    for key in ("milestones", "features"):
        if key not in value:
            raise QueueError(f"$: missing required key: {key}")
        if not isinstance(value[key], list):
            raise QueueError(f"$.{key}: expected an array")
    return value


def normalize_status(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueueError(f"{path}: expected a non-empty status string")
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    normalized = STATUS_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_STATUSES:
        raise QueueError(f"{path}: unsupported status {value!r}")
    return normalized


def normalize_milestone_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    phase = re.fullmatch(r"(?:p|phase-?)(\d+)", normalized)
    if phase:
        return f"phase-{int(phase.group(1))}"
    milestone = re.fullmatch(r"(?:m|milestone-?)(\d+)", normalized)
    if milestone:
        return f"milestone-{int(milestone.group(1))}"
    return normalized


def _priority(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.upper().startswith("P") and value[1:].isdigit():
        return int(value[1:])
    return 1_000_000


@dataclass(frozen=True)
class Selection:
    feature_id: str
    title: str
    reason: str
    feature: dict[str, Any]


@dataclass(frozen=True)
class CommitResolution:
    feature_id: str
    commit: str
    source: str


class FeatureQueue:
    def __init__(
        self,
        document: dict[str, Any],
        path: Path | None = None,
        repository_root: Path | None = None,
    ):
        self.document = copy.deepcopy(document)
        self.path = path
        self.repository_root = repository_root.resolve() if repository_root else None
        self.features: list[dict[str, Any]] = []
        self.milestones: list[dict[str, Any]] = []
        self._validate_and_normalize()

    @classmethod
    def from_path(cls, path: Path, repository_root: Path | None = None) -> "FeatureQueue":
        path = path.resolve()
        return cls(load_queue(path), path, repository_root)

    @classmethod
    def from_location(cls, repository: Path, configured: str) -> "FeatureQueue":
        repository = repository.resolve()
        path = resolve_queue_path(repository, configured)
        return cls(load_queue(path), path, repository)

    def _validate_and_normalize(self) -> None:
        raw_milestones = self.document.get("milestones")
        raw_features = self.document.get("features")
        if not isinstance(raw_milestones, list):
            raise QueueError("$.milestones: expected an array")
        if not isinstance(raw_features, list):
            raise QueueError("$.features: expected an array")

        for index, raw in enumerate(raw_milestones):
            path = _yaml_path("$.milestones", index)
            if not isinstance(raw, dict):
                raise QueueError(f"{path}: expected an object")
            item = copy.deepcopy(raw)
            milestone_id = item.get("id")
            if not isinstance(milestone_id, str) or not milestone_id.strip():
                raise QueueError(f"{path}.id: expected a non-empty string")
            item["id"] = milestone_id.strip()
            item["normalized_id"] = normalize_milestone_name(item["id"])
            self.milestones.append(item)

        ids = [item["id"] for item in self.milestones]
        if len(ids) != len(set(ids)):
            raise QueueError("$.milestones: milestone IDs must be unique")
        normalized_ids = [item["normalized_id"] for item in self.milestones]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise QueueError("$.milestones: milestone IDs are ambiguous after normalization")

        for index, raw in enumerate(raw_features):
            path = _yaml_path("$.features", index)
            if not isinstance(raw, dict):
                raise QueueError(f"{path}: expected an object")
            item = copy.deepcopy(raw)
            feature_id = item.get("id")
            if not isinstance(feature_id, str) or not feature_id.strip():
                raise QueueError(f"{path}.id: expected a non-empty string")
            item["id"] = feature_id.strip()
            title = item.get("title", item.get("name", item["id"]))
            if not isinstance(title, str) or not title.strip():
                raise QueueError(f"{path}.title: expected a non-empty title or name")
            item["title"] = title.strip()
            item["status"] = normalize_status(item.get("status"), f"{path}.status")

            milestone_id = item.get("milestone")
            if not isinstance(milestone_id, str) or not milestone_id.strip():
                raise QueueError(f"{path}.milestone: expected a non-empty string")
            milestone = self.milestone(milestone_id)
            if milestone is None:
                raise QueueError(f"{path}.milestone: unknown milestone {milestone_id!r}")
            item["milestone"] = milestone["id"]

            dependencies = item.get("dependencies", item.get("depends_on", []))
            if "dependencies" in item and "depends_on" in item and item["dependencies"] != item["depends_on"]:
                raise QueueError(f"{path}: dependencies and depends_on disagree")
            if not isinstance(dependencies, list):
                raise QueueError(f"{path}.dependencies: expected an array of feature IDs")
            for dependency_index, dependency in enumerate(dependencies):
                if not isinstance(dependency, str) or not dependency.strip():
                    raise QueueError(
                        f"{path}.dependencies[{dependency_index}]: expected a non-empty feature ID"
                    )
            item["dependencies"] = [dependency.strip() for dependency in dependencies]

            commit = item.get("accepted_commit", item.get("commit"))
            if commit is not None and (not isinstance(commit, str) or not commit.strip()):
                raise QueueError(f"{path}.commit: expected a non-empty commit reference")
            if isinstance(commit, str):
                item["accepted_commit"] = commit.strip()
            legacy_commit = item.get("commit")
            if legacy_commit is not None and (not isinstance(legacy_commit, str) or not legacy_commit.strip()):
                raise QueueError(f"{path}.commit: expected a non-empty commit reference")

            criteria = item.get("acceptance_criteria")
            if criteria is not None and (
                not isinstance(criteria, list)
                or not all(isinstance(criterion, str) and criterion.strip() for criterion in criteria)
            ):
                raise QueueError(f"{path}.acceptance_criteria: expected non-empty strings")
            human = item.get("requires_human_decision")
            if human is not None and not isinstance(human, bool):
                raise QueueError(f"{path}.requires_human_decision: expected a boolean")
            specification = item.get("spec") or item.get("specification") or item.get("spec_path")
            if specification is not None:
                if not isinstance(specification, str) or not specification.strip():
                    raise QueueError(f"{path}.spec: expected a non-empty repository-relative path")
                spec_path = Path(specification)
                if spec_path.is_absolute() or ".." in spec_path.parts:
                    raise QueueError(f"{path}.spec: path must be repository-relative and cannot traverse")
                if self.repository_root is not None:
                    resolved_spec = (self.repository_root / spec_path).resolve(strict=False)
                    try:
                        resolved_spec.relative_to(self.repository_root)
                    except ValueError as exc:
                        raise QueueError(f"{path}.spec: resolved path escapes the registered repository") from exc
            self.features.append(item)

        feature_ids = [item["id"] for item in self.features]
        if len(feature_ids) != len(set(feature_ids)):
            raise QueueError("$.features: feature IDs must be unique")
        feature_set = set(feature_ids)
        for index, item in enumerate(self.features):
            missing = sorted(set(item["dependencies"]) - feature_set)
            if missing:
                raise QueueError(
                    f"$.features[{index}].dependencies: unknown feature IDs: {', '.join(missing)}"
                )

    def milestone(self, milestone_id: str) -> dict[str, Any] | None:
        exact = [item for item in self.milestones if item.get("id") == milestone_id]
        if len(exact) == 1:
            return exact[0]
        normalized = normalize_milestone_name(milestone_id)
        matches = [item for item in self.milestones if item.get("normalized_id") == normalized]
        return matches[0] if len(matches) == 1 else None

    def feature(self, feature_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.features if item.get("id") == feature_id]
        return matches[0] if len(matches) == 1 else None

    def features_for_milestone(self, milestone_id: str) -> list[dict[str, Any]]:
        milestone = self.milestone(milestone_id)
        if milestone is None:
            return []
        return [item for item in self.features if item.get("milestone") == milestone["id"]]

    def active_features(self, milestone_id: str | None = None) -> list[dict[str, Any]]:
        source = self.features if milestone_id is None else self.features_for_milestone(milestone_id)
        return [item for item in source if item.get("status") in ACTIVE_STATUSES]

    def dependencies_complete(self, feature: dict[str, Any]) -> bool:
        for dependency_id in feature.get("dependencies", []):
            dependency = self.feature(dependency_id)
            if dependency is None or dependency.get("status") not in COMPLETE_STATUSES:
                return False
            if dependency.get("status") == "integrated":
                if dependency.get("integration_status") != "passed" or not isinstance(dependency.get("integrated_commit"), str):
                    return False
                milestone = self.milestone(str(dependency.get("milestone")))
                integrated_ids = milestone.get("integrated_features", []) if milestone else []
                if dependency_id not in integrated_ids:
                    return False
        return True

    def _specified(self, feature: dict[str, Any]) -> bool:
        specification = feature.get("spec") or feature.get("specification") or feature.get("spec_path")
        criteria = feature.get("acceptance_criteria")
        if self.path is None:
            return bool(specification and isinstance(criteria, list) and criteria)
        if not isinstance(specification, str) or not specification:
            return False
        if self.repository_root is None:
            return False
        spec_path = Path(specification)
        if spec_path.is_absolute() or ".." in spec_path.parts:
            return False
        resolved = (self.repository_root / spec_path).resolve(strict=False)
        try:
            resolved.relative_to(self.repository_root)
        except ValueError:
            return False
        return resolved.is_file() and isinstance(criteria, list) and bool(criteria)

    def ready(self, milestone_id: str) -> list[dict[str, Any]]:
        candidates = []
        for index, feature in enumerate(self.features_for_milestone(milestone_id)):
            if feature.get("status") != "ready":
                continue
            if feature.get("requires_human_decision") is True:
                continue
            if not self.dependencies_complete(feature) or not self._specified(feature):
                continue
            candidates.append((feature, index))
        candidates.sort(key=lambda pair: (_priority(pair[0].get("priority")), -len(pair[0].get("dependencies", [])), pair[1], pair[0]["id"]))
        return [item for item, _ in candidates]

    def select_next(self, milestone_id: str) -> Selection | None:
        ready = self.ready(milestone_id)
        if not ready:
            return None
        feature = ready[0]
        reason = (
            "Selected deterministically by repository priority, dependency depth, queue order, "
            f"then feature ID; dependencies are complete for {feature['id']}."
        )
        return Selection(feature_id=feature["id"], title=feature["title"], reason=reason, feature=feature)

    def integration_candidates(self, milestone_id: str) -> list[dict[str, Any]]:
        """Return accepted work awaiting integration before any new ready work."""

        candidates = [
            item for item in self.features_for_milestone(milestone_id)
            if item.get("status") in {"accepted", "integration_pending"}
            and item.get("integration_status", "pending") == "pending"
        ]
        candidates.sort(key=lambda item: (_priority(item.get("priority")), self.features.index(item), item["id"]))
        return candidates

    def select_integration(self, milestone_id: str) -> Selection | None:
        candidates = self.integration_candidates(milestone_id)
        if len(candidates) > 1:
            raise QueueError(
                "multiple accepted features await integration; deterministic single-feature integration is required: "
                + ", ".join(item["id"] for item in candidates)
            )
        if not candidates:
            return None
        feature = candidates[0]
        return Selection(
            feature_id=feature["id"],
            title=feature["title"],
            reason="Accepted integration-pending work takes precedence over selection of any new ready feature.",
            feature=feature,
        )

    def milestone_complete(self, milestone_id: str) -> bool:
        features = [
            item for item in self.features_for_milestone(milestone_id)
            if item.get("status") not in {"deferred", "rejected"}
        ]
        return bool(features) and all(item.get("status") in COMPLETE_STATUSES for item in features)

    def reconciliation_classification(self, milestone_id: str) -> str:
        features = self.features_for_milestone(milestone_id)
        if self.milestone(milestone_id) is None:
            return "invalid_queue"
        if self.integration_candidates(milestone_id):
            return "reconciled_integration_pending"
        if self.ready(milestone_id):
            return "reconciled_ready_work"
        if self.milestone_complete(milestone_id):
            return "milestone_complete"
        if any(
            item.get("status") == "human_decision_required"
            or (
                item.get("requires_human_decision") is True
                and self.dependencies_complete(item)
            )
            for item in features
        ):
            return "human_decision_required"
        actionable = [item for item in features if item.get("status") not in COMPLETE_STATUSES | {"deferred", "rejected"}]
        if actionable and all(item.get("status") == "blocked" for item in actionable):
            return "legitimately_blocked"
        return "reconciled_no_ready_work"

    def summary(self, milestone_id: str) -> dict[str, Any]:
        milestone_features = self.features_for_milestone(milestone_id)
        counts: dict[str, int] = {}
        for item in milestone_features:
            status = str(item.get("status"))
            counts[status] = counts.get(status, 0) + 1
        selection = self.select_next(milestone_id)
        integration = self.select_integration(milestone_id)
        milestone = self.milestone(milestone_id)
        return {
            "milestone_found": milestone is not None,
            "configured_milestone": milestone_id,
            "resolved_milestone": milestone.get("id") if milestone else None,
            "feature_count": len(milestone_features),
            "total_feature_count": len(self.features),
            "status_counts": counts,
            "active_features": [item["id"] for item in self.active_features(milestone_id)],
            "completed_features": [item["id"] for item in milestone_features if item["status"] in COMPLETE_STATUSES],
            "ready_features": [item["id"] for item in self.ready(milestone_id)] if milestone else [],
            "selected_feature": selection.feature_id if selection else None,
            "integration_candidates": [item["id"] for item in self.integration_candidates(milestone_id)],
            "selected_integration_feature": integration.feature_id if integration else None,
            "milestone_complete": self.milestone_complete(milestone_id),
            "reconciliation_classification": self.reconciliation_classification(milestone_id),
        }


def resolve_feature_commit(
    *,
    feature: dict[str, Any],
    queue: FeatureQueue,
    repository: Any,
    milestone_branch: str | None,
    baseline: str | None,
    registered_commit: str | None,
    registered_feature: str | None,
) -> CommitResolution | None:
    """Resolve accepted commit evidence, including a corroborated SELF sentinel."""

    reference = feature.get("accepted_commit")
    if reference is None:
        return None
    if reference != "SELF":
        if not repository.ref_exists(reference):
            raise QueueError(f"feature {feature['id']}: accepted commit does not exist: {reference}")
        return CommitResolution(feature["id"], reference, "queue")

    if feature.get("status") not in COMPLETE_STATUSES:
        raise QueueError(f"feature {feature['id']}: SELF is permitted only for a completed feature")
    if not registered_commit or not registered_feature:
        raise QueueError(f"feature {feature['id']}: SELF has no registered accepted-commit association")
    label = registered_feature.casefold()
    if feature["id"].casefold() not in label:
        raise QueueError(f"feature {feature['id']}: registered accepted feature does not identify SELF")
    candidate = registered_commit
    if not repository.ref_exists(candidate):
        raise QueueError(f"feature {feature['id']}: registered SELF candidate does not exist")
    if not milestone_branch or not repository.ref_exists(milestone_branch):
        raise QueueError(f"feature {feature['id']}: configured milestone branch is missing")
    if not repository.is_ancestor(candidate, milestone_branch):
        raise QueueError(f"feature {feature['id']}: SELF candidate is not contained by the milestone branch")
    starting_point = feature.get("integration_base_commit") or baseline
    if not isinstance(starting_point, str) or not repository.ref_exists(starting_point):
        raise QueueError(f"feature {feature['id']}: SELF has no verified starting point")
    if candidate == starting_point or not repository.is_ancestor(starting_point, candidate):
        raise QueueError(f"feature {feature['id']}: SELF candidate is not a descendant of its starting point")

    if queue.path is None:
        raise QueueError(f"feature {feature['id']}: SELF requires a repository-backed queue")
    relative_queue = queue.path.resolve().relative_to(repository.root).as_posix()
    queue_text = repository.file_at_commit(candidate, relative_queue)
    if queue_text is None:
        raise QueueError(f"feature {feature['id']}: queue containing SELF is absent from candidate commit")
    try:
        candidate_queue = FeatureQueue(json.loads(queue_text))
    except (json.JSONDecodeError, QueueError) as exc:
        raise QueueError(f"feature {feature['id']}: candidate queue cannot verify SELF: {exc}") from exc
    candidate_feature = candidate_queue.feature(feature["id"])
    if (
        candidate_feature is None
        or candidate_feature.get("accepted_commit") != "SELF"
        or candidate_feature.get("status") not in COMPLETE_STATUSES
    ):
        raise QueueError(f"feature {feature['id']}: candidate commit does not contain matching completed SELF evidence")

    changed = set(repository.changed_paths(candidate))
    if relative_queue not in changed:
        raise QueueError(f"feature {feature['id']}: SELF candidate did not change the queue file")
    evidence_paths = [
        candidate_feature.get("spec") or candidate_feature.get("specification") or candidate_feature.get("spec_path"),
        "docs/CURRENT_STATUS.md",
        "docs/RUN_LOG.md",
    ]
    for evidence_path in evidence_paths:
        if not isinstance(evidence_path, str) or not evidence_path:
            raise QueueError(f"feature {feature['id']}: SELF lacks specification or status evidence")
        content = repository.file_at_commit(candidate, evidence_path)
        if content is None or feature["id"].casefold() not in content.casefold():
            raise QueueError(
                f"feature {feature['id']}: {evidence_path} does not corroborate the SELF association"
            )
    if not ({str(evidence_paths[0]), "docs/CURRENT_STATUS.md", "docs/RUN_LOG.md"} & changed):
        raise QueueError(f"feature {feature['id']}: SELF candidate lacks changed corroborating evidence")
    return CommitResolution(feature["id"], candidate, "SELF")
