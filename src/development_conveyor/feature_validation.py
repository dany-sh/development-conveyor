"""Candidate-bound feature validation for registered repositories."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .acceptance import tier_evidence_from_records
from .errors import ConveyorError
from .logging import atomic_write_json
from .queue import FeatureQueue
from .redaction import redact_text
from .registry import Project
from .repository import RepositoryInspector
from .validation import SafetyPolicy
from .validation_tiers import adapter_commands


def validate_feature_candidate(
    *, controller_root: Path, project: Project, feature_id: str
) -> dict[str, Any]:
    """Run exactly the configured feature tier for one immutable candidate."""

    inspector = RepositoryInspector(project.repository)
    queue = FeatureQueue.from_location(project.repository, project.queue_location)
    feature = queue.feature(feature_id)
    branch = feature.get("branch") if isinstance(feature, dict) else None
    milestone_base = inspector.rev_parse(project.milestone_branch or "", check=False)
    if (
        not isinstance(feature, dict)
        or feature.get("milestone") != project.active_milestone
        or not isinstance(branch, str)
        or not branch
        or inspector.current_branch != branch
        or inspector.rev_parse(branch, check=False) != inspector.head
        or not isinstance(milestone_base, str)
        or not inspector.is_clean
        or any(inspector.git_operation_state().values())
    ):
        raise ConveyorError(
            "feature validation requires the exact clean registered candidate"
        )
    try:
        adapter = json.loads(
            inspector.safe_worktree_file_bytes(".factory/project.yaml")
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConveyorError("registered project adapter is malformed") from exc
    configured, source = adapter_commands(adapter, "feature")
    records: list[dict[str, Any]] = []
    validation_environment = dict(os.environ)
    validation_environment.pop("DEVELOPMENT_CONVEYOR_HOME", None)
    validation_environment.pop("CONVEYOR_CANDIDATE", None)
    validation_environment.pop("CONVEYOR_PREPARED_PARENT", None)
    for item in configured:
        argv = list(item["argv"])
        lowered = " ".join(argv).lower()
        if "validate-release" in lowered or (
            "unittest" in lowered and "discover" in argv
        ):
            raise ConveyorError(
                "feature tier cannot invoke release validation or complete discovery"
            )
        SafetyPolicy.validate_configured_command(
            argv, cwd=project.repository, registered_repository=project.repository
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=project.repository,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
                env=validation_environment,
            )
            exit_status = completed.returncode
            diagnostic = redact_text(
                (completed.stderr.strip() or completed.stdout.strip())[-2000:]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_status = 124
            diagnostic = type(exc).__name__
        records.append(
            {
                "group": item["group"],
                "argv": argv,
                "exit_status": exit_status,
                "classification": "configured_required_validation",
                "diagnostic_tail": diagnostic or None,
            }
        )
        if exit_status:
            break
    valid = bool(records) and all(item["exit_status"] == 0 for item in records)
    evidence = tier_evidence_from_records(
        project=project,
        implementation_commit=inspector.head,
        tier="feature",
        commands=records,
        valid=valid,
    )
    evidence_path = (
        controller_root
        / "state/projects"
        / project.project_id
        / "feature-evidence"
        / f"{feature_id}-{uuid.uuid4().hex}.json"
    )
    atomic_write_json(evidence_path, evidence)
    result = {
        "schema_version": 1,
        "phase": "validation",
        "outcome": "validated" if valid else "validation_failed",
        "project_id": project.project_id,
        "feature_id": feature_id,
        "feature_branch": branch,
        "milestone_base": milestone_base,
        "implementation_commit": inspector.head,
        "implementation_tree": inspector.rev_parse(f"{inspector.head}^{{tree}}"),
        "tier": "feature",
        "validation_source": source,
        "evidence_path": str(evidence_path),
        "evidence": evidence,
        "complete_suite_invocations": 0,
        "release_validation_invocations": 0,
    }
    if not valid:
        result["failed_command"] = records[-1] if records else None
    return result
