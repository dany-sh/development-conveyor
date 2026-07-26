"""Canonical warning evidence shared by queue-validation boundaries."""

from __future__ import annotations

from typing import Any


class WarningEvidenceError(ValueError):
    """Raised when warning evidence is malformed or internally inconsistent."""


def _string_array(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise WarningEvidenceError(f"queue validation {field} must be a string array")
    return list(value)


def normalize_warning_evidence(
    value: dict[str, Any],
    *,
    source: str,
    legacy_summary: str | None = None,
) -> dict[str, Any]:
    """Normalize canonical, deterministic, or one explicitly authorized legacy shape."""

    if not isinstance(value, dict):
        raise WarningEvidenceError("queue validation evidence must be an object")
    if source not in {"structured", "deterministic"}:
        raise WarningEvidenceError("warning evidence source is invalid")

    explicit_warnings: list[str] | None = None
    warning_summary: str | None = None
    if "warnings" in value:
        raw_warnings = value["warnings"]
        if isinstance(raw_warnings, list):
            explicit_warnings = _string_array(raw_warnings, field="warnings")
        elif (
            source == "structured"
            and isinstance(raw_warnings, str)
            and legacy_summary is not None
            and raw_warnings == legacy_summary
        ):
            warning_summary = raw_warnings
        else:
            raise WarningEvidenceError("queue validation warnings must be a string array")
    elif source == "deterministic":
        raise WarningEvidenceError(
            "deterministic queue validation warnings must be a string array"
        )

    raw_count = value.get("warning_count")
    if raw_count is None:
        if source == "structured" and warning_summary is None:
            raise WarningEvidenceError(
                "queue validation warning_count is required"
            )
        warning_count = (
            len(explicit_warnings) if explicit_warnings is not None else None
        )
    elif isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        raise WarningEvidenceError(
            "queue validation warning_count must be a non-negative integer"
        )
    else:
        warning_count = raw_count

    if (
        explicit_warnings is not None
        and warning_count is not None
        and warning_count != len(explicit_warnings)
    ):
        raise WarningEvidenceError(
            "queue validation warning_count disagrees with warnings"
        )

    scope = value.get("warnings_scope")
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        raise WarningEvidenceError(
            "queue validation warnings_scope must be a non-empty string"
        )
    if warning_summary is not None:
        scope = warning_summary

    if source == "deterministic" and "blocking_warnings" not in value:
        raise WarningEvidenceError(
            "deterministic queue validation blocking_warnings must be a string array"
        )
    blocking = _string_array(
        value.get("blocking_warnings", []),
        field="blocking_warnings",
    )
    if len(blocking) != len(set(blocking)):
        raise WarningEvidenceError(
            "queue validation blocking_warnings contains duplicates"
        )
    blocking.sort()
    if explicit_warnings is not None and not set(blocking).issubset(explicit_warnings):
        raise WarningEvidenceError(
            "queue validation blocking_warnings must be included in warnings"
        )

    return {
        "warning_count": warning_count,
        "warnings_scope": scope,
        "blocking_warnings": blocking,
        "explicit_warnings": explicit_warnings,
        "legacy_summary": warning_summary is not None,
    }


def compare_warning_evidence(
    structured: dict[str, Any],
    deterministic: dict[str, Any],
    *,
    legacy_summary: str | None = None,
) -> dict[str, Any]:
    """Compare structured warning claims with authoritative deterministic evidence."""

    normalized_structured = normalize_warning_evidence(
        structured,
        source="structured",
        legacy_summary=legacy_summary,
    )
    normalized_deterministic = normalize_warning_evidence(
        deterministic,
        source="deterministic",
    )
    disagreements: list[str] = []
    if (
        not normalized_structured["legacy_summary"]
        and normalized_structured["warning_count"]
        != normalized_deterministic["warning_count"]
    ):
        disagreements.append("warning_count")
    if (
        normalized_structured["blocking_warnings"]
        != normalized_deterministic["blocking_warnings"]
    ):
        disagreements.append("blocking_warnings")
    structured_warnings = normalized_structured["explicit_warnings"]
    deterministic_warnings = normalized_deterministic["explicit_warnings"]
    if (
        structured_warnings is not None
        and structured_warnings != deterministic_warnings
    ):
        disagreements.append("explicit_warnings")
    return {
        "structured": normalized_structured,
        "deterministic": normalized_deterministic,
        "disagreements": disagreements,
    }
