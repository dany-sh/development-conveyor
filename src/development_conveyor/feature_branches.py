"""Canonical, fail-closed feature branch identity helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import RecoveryError
from .registry import Project


def canonical_feature_branch(project: Project, feature: dict[str, Any]) -> str:
    """Return the one safe branch identity for a queued feature.

    A queue may record an explicit branch, but it must still identify the same
    feature.  An absent queue value is deliberately normal for a fresh feature
    and is derived from the repository adapter.
    """

    try:
        adapter = json.loads(
            (project.repository / project.validation_source).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"cannot derive feature branch from repository adapter: {exc}"
        ) from exc
    pattern = (adapter.get("git") or {}).get("feature_branch_pattern")
    feature_id = feature.get("id")
    title = feature.get("name") or feature.get("title")
    if not isinstance(pattern, str) or not isinstance(feature_id, str) or not isinstance(title, str):
        raise RecoveryError("feature branch is absent and adapter branch metadata is incomplete")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", feature_id) or not slug:
        raise RecoveryError("feature ID or title cannot produce a safe feature branch")
    try:
        derived = pattern.format(
            feature_id=feature_id, feature_id_lower=feature_id.lower(), slug=slug
        )
    except (KeyError, ValueError) as exc:
        raise RecoveryError(f"feature branch pattern is unsupported: {pattern}") from exc

    recorded = feature.get("branch")
    branch = recorded if isinstance(recorded, str) and recorded else derived
    if not isinstance(branch, str) or not branch.startswith("codex/") or any(char.isspace() for char in branch):
        raise RecoveryError("feature branch must use the allowed codex/ prefix and contain no whitespace")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch or branch.endswith("/"):
        raise RecoveryError("feature branch contains invalid ref characters")
    # An explicit branch is allowed for an already-established feature, but it
    # cannot silently redirect the selected feature to another feature's ref.
    branch_stem = branch.removeprefix("codex/").lower()
    feature_stem = feature_id.lower()
    if not (branch_stem == feature_stem or branch_stem.startswith(feature_stem + "-")):
        raise RecoveryError("explicit feature branch does not identify the selected feature")
    return branch
