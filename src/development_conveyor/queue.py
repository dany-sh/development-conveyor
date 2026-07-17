"""Deterministic feature-queue validation, readiness, and selection."""

from __future__ import annotations

import json
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


def load_queue(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QueueError(f"cannot read queue {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"{path}: invalid JSON-compatible YAML: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("features"), list) or not isinstance(value.get("milestones"), list):
        raise QueueError("queue must contain milestone and feature arrays")
    return value


def _priority(value: Any) -> int:
    if isinstance(value, int):
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


class FeatureQueue:
    def __init__(self, document: dict[str, Any], path: Path | None = None):
        self.document = document
        self.path = path
        self.features = [item for item in document.get("features", []) if isinstance(item, dict)]
        self.milestones = [item for item in document.get("milestones", []) if isinstance(item, dict)]
        self._validate_structure()

    @classmethod
    def from_path(cls, path: Path) -> "FeatureQueue":
        return cls(load_queue(path), path)

    def _validate_structure(self) -> None:
        ids = [item.get("id") for item in self.features]
        if not all(isinstance(item, str) and item for item in ids):
            raise QueueError("every feature requires a non-empty string ID")
        if len(ids) != len(set(ids)):
            raise QueueError("feature IDs must be unique")
        milestone_ids = [item.get("id") for item in self.milestones]
        if not all(isinstance(item, str) and item for item in milestone_ids):
            raise QueueError("every milestone requires a non-empty string ID")
        if len(milestone_ids) != len(set(milestone_ids)):
            raise QueueError("milestone IDs must be unique")
        feature_set = set(ids)
        for item in self.features:
            status = item.get("status")
            if status not in SUPPORTED_STATUSES:
                raise QueueError(f"feature {item['id']}: unsupported status {status!r}")
            dependencies = item.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
                raise QueueError(f"feature {item['id']}: dependencies must be string IDs")
            missing = sorted(set(dependencies) - feature_set)
            if missing:
                raise QueueError(f"feature {item['id']}: unknown dependencies: {', '.join(missing)}")

    def milestone(self, milestone_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.milestones if item.get("id") == milestone_id]
        return matches[0] if len(matches) == 1 else None

    def feature(self, feature_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.features if item.get("id") == feature_id]
        return matches[0] if len(matches) == 1 else None

    def active_features(self) -> list[dict[str, Any]]:
        return [item for item in self.features if item.get("status") in ACTIVE_STATUSES]

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
        spec_path = Path(specification)
        resolved = spec_path if spec_path.is_absolute() else self.path.parent.parent / spec_path
        return resolved.is_file() and isinstance(criteria, list) and bool(criteria)

    def ready(self, milestone_id: str) -> list[dict[str, Any]]:
        candidates = []
        for index, feature in enumerate(self.features):
            if feature.get("milestone") != milestone_id or feature.get("status") != "ready":
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
        return Selection(feature_id=feature["id"], title=str(feature.get("title") or feature["id"]), reason=reason, feature=feature)

    def milestone_complete(self, milestone_id: str) -> bool:
        features = [item for item in self.features if item.get("milestone") == milestone_id and item.get("status") not in {"deferred", "rejected"}]
        return bool(features) and all(item.get("status") in COMPLETE_STATUSES for item in features)

    def summary(self, milestone_id: str) -> dict[str, Any]:
        milestone_features = [item for item in self.features if item.get("milestone") == milestone_id]
        counts: dict[str, int] = {}
        for item in milestone_features:
            status = str(item.get("status"))
            counts[status] = counts.get(status, 0) + 1
        selection = self.select_next(milestone_id)
        return {
            "milestone_found": self.milestone(milestone_id) is not None,
            "feature_count": len(milestone_features),
            "status_counts": counts,
            "active_features": [item["id"] for item in self.active_features()],
            "ready_features": [item["id"] for item in self.ready(milestone_id)] if self.milestone(milestone_id) else [],
            "selected_feature": selection.feature_id if selection else None,
            "milestone_complete": self.milestone_complete(milestone_id),
        }
