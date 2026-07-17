"""Typed portfolio registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Configuration
from .errors import ConfigurationError


@dataclass(frozen=True)
class Project:
    project_id: str
    repository: Path
    enabled: bool
    priority: int
    active_milestone: str | None
    recovery_branch: str | None
    milestone_branch: str | None
    validated_baseline_commit: str | None
    queue_location: str
    autonomy_contract_location: str
    validation_source: str
    automation_mode: str
    maximum_retries: int | None
    schedule: dict[str, Any] | None
    human_gates: tuple[str, ...]
    last_accepted_feature: str | None
    last_accepted_commit: str | None
    current_state: str
    registration_notes: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Project":
        return cls(
            project_id=value["project_id"],
            repository=Path(value["repository"]).expanduser().resolve(),
            enabled=value["enabled"],
            priority=value["priority"],
            active_milestone=value["active_milestone"],
            recovery_branch=value["recovery_branch"],
            milestone_branch=value["milestone_branch"],
            validated_baseline_commit=value["validated_baseline_commit"],
            queue_location=value["queue_location"],
            autonomy_contract_location=value["autonomy_contract_location"],
            validation_source=value["validation_source"],
            automation_mode=value["automation_mode"],
            maximum_retries=value["maximum_retries"],
            schedule=value["schedule"],
            human_gates=tuple(value["human_gates"]),
            last_accepted_feature=value["last_accepted_feature"],
            last_accepted_commit=value["last_accepted_commit"],
            current_state=value["current_state"],
            registration_notes=value["registration_notes"],
        )


class ProjectRegistry:
    def __init__(self, configuration: Configuration):
        self.configuration = configuration
        self._projects = {item.project_id: item for item in map(Project.from_dict, configuration.projects)}

    def get(self, project_id: str) -> Project:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown project: {project_id}") from exc

    def enabled(self) -> list[Project]:
        return sorted((item for item in self._projects.values() if item.enabled), key=lambda item: (-item.priority, item.project_id))

    def all(self) -> list[Project]:
        return sorted(self._projects.values(), key=lambda item: (-item.priority, item.project_id))

