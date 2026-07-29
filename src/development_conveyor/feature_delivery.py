"""Thin validate, accept, integrate orchestration."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .acceptance import accept_feature
from .errors import ConveyorError
from .feature_integration import integrate_feature
from .feature_validation import validate_feature_candidate
from .registry import Project


def deliver_feature(
    *, controller_root: Path, project: Project, feature_id: str
) -> dict[str, Any]:
    """Stop after the first failed authority and pass only structured results."""

    phases: list[dict[str, Any]] = []
    try:
        validation = validate_feature_candidate(
            controller_root=controller_root, project=project, feature_id=feature_id
        )
    except ConveyorError as exc:
        phases.append(_failure("validation", exc))
        return _result(project.project_id, feature_id, "validation_failed", phases)
    phases.append(validation)
    if validation.get("outcome") != "validated":
        return _result(project.project_id, feature_id, "validation_failed", phases)
    try:
        acceptance = accept_feature(
            controller_root=controller_root,
            project=project,
            feature_id=feature_id,
            feature_branch=validation["feature_branch"],
            milestone_base=validation["milestone_base"],
            implementation_commit=validation["implementation_commit"],
            implementation_tree=validation["implementation_tree"],
            tier_evidence=validation["evidence"],
            run_id=f"deliver-accept-{uuid.uuid4().hex}",
            required_tier="feature",
        )
    except (ConveyorError, KeyError, TypeError) as exc:
        phases.append(_failure("acceptance", exc))
        return _result(project.project_id, feature_id, "acceptance_failed", phases)
    phases.append(acceptance)
    try:
        integration = integrate_feature(
            controller_root=controller_root,
            project=project,
            feature_id=feature_id,
            acceptance_result=acceptance,
            run_id=f"deliver-integrate-{uuid.uuid4().hex}",
        )
    except ConveyorError as exc:
        phases.append(_failure("integration", exc))
        return _result(project.project_id, feature_id, "integration_failed", phases)
    phases.append(integration)
    return _result(
        project.project_id,
        feature_id,
        str(integration.get("outcome") or "integration_failed"),
        phases,
    )


def _result(
    project_id: str, feature_id: str, outcome: str, phases: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": "deliver-feature",
        "project_id": project_id,
        "feature_id": feature_id,
        "outcome": outcome,
        "phases": phases,
        "complete_suite_invocations": sum(
            int(item.get("complete_suite_invocations", 0)) for item in phases
        ),
        "release_validation_invocations": sum(
            int(item.get("release_validation_invocations", 0)) for item in phases
        ),
    }


def _failure(phase: str, error: Exception) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": phase,
        "outcome": f"{phase}_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "complete_suite_invocations": 0,
        "release_validation_invocations": 0,
    }
