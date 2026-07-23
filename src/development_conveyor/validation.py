"""Small deterministic validators and command safety enforcement."""

from __future__ import annotations

import contextvars
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import SafetyViolation, SchemaValidationError


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError(f"unsupported schema type: {expected}")


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON-Schema subset used by Conveyor-owned documents."""

    expected = schema.get("type")
    if expected is not None:
        types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, item) for item in types):
            raise SchemaValidationError(f"{path}: expected {' or '.join(types)}")
        if value is None:
            return

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value {value!r} is not in enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required keys: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise SchemaValidationError(f"{path}: unsupported keys: {', '.join(extras)}")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                validate_schema(child, child_schema, f"{path}.{key}")

    if isinstance(value, list):
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            raise SchemaValidationError(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                validate_schema(child, item_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise SchemaValidationError(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise SchemaValidationError(f"{path}: value does not match {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise SchemaValidationError(f"{path}: value is below {minimum}")
        if maximum is not None and value > maximum:
            raise SchemaValidationError(f"{path}: value is above {maximum}")


class SafetyPolicy:
    """Reject controller-owned commands outside a narrow structured boundary."""

    PROHIBITED_WORDS = {
        "deploy",
        "deployment",
        "publish",
        "publication",
        "release",
        "notarize",
        "notarization",
    }

    SAFE_GIT_READ_COMMANDS = {
        "branch",
        "check-ref-format",
        "check-ignore",
        "diff",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "merge-base",
        "rev-list",
        "rev-parse",
        "show",
        "status",
        "symbolic-ref",
        "worktree",
    }

    _observation_sink: contextvars.ContextVar[list[dict[str, Any]] | None] = (
        contextvars.ContextVar("conveyor_subprocess_observation_sink", default=None)
    )

    @staticmethod
    def _is_swift_release_build(executable: str, lowered: list[str]) -> bool:
        """Recognize Swift's build configuration without authorizing releases."""

        return executable == "swift" and lowered in (
            ["build", "-c", "release"],
            ["build", "--configuration", "release"],
        )

    @classmethod
    @contextmanager
    def observe_command_attempts(cls) -> Iterator[list[dict[str, Any]]]:
        """Capture privacy-safe classifications at the common command authority."""

        observed: list[dict[str, Any]] = []
        token = cls._observation_sink.set(observed)
        try:
            yield observed
        finally:
            cls._observation_sink.reset(token)

    @classmethod
    def _observe_command_attempt(
        cls, argv: list[str], *, cwd: Path, authority: str
    ) -> None:
        sink = cls._observation_sink.get()
        if sink is None or not argv:
            return
        executable = Path(argv[0]).name.lower() if isinstance(argv[0], str) else "invalid"
        lowered = [item.lower() for item in argv[1:] if isinstance(item, str)]
        tokens = {
            part for item in lowered
            for part in re.split(r"[^a-z0-9_-]+", item) if part
        }
        categories: set[str] = set()
        if executable == "git" and lowered:
            if lowered[0] in {"push", "tag"}:
                categories.add(lowered[0])
        category_tokens = {
            "deploy": "deploy", "deployment": "deploy",
            "publish": "publish", "publication": "publish",
            "release": "release",
            "notarize": "notarize", "notarization": "notarize",
            "notarytool": "notarize",
        }
        categories.update(
            category for token, category in category_tokens.items() if token in tokens
        )
        if (
            authority == "configured_validation"
            and cls._is_swift_release_build(executable, lowered)
        ):
            categories.discard("release")
        sink.append({
            "authority": authority,
            "executable": executable,
            "operation": lowered[0] if lowered else None,
            "prohibited_categories": sorted(categories),
            "cwd_fingerprint": hashlib.sha256(
                str(cwd.expanduser().resolve()).encode("utf-8")
            ).hexdigest(),
        })

    @classmethod
    def validate_controller_command(
        cls,
        argv: list[str],
        *,
        cwd: Path,
        registered_repository: Path | None = None,
        allow_codex: bool = False,
    ) -> None:
        cls._observe_command_attempt(argv, cwd=cwd, authority="controller")
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise SafetyViolation("commands must be non-empty argument arrays")
        resolved_cwd = cwd.expanduser().resolve()
        if registered_repository is not None and resolved_cwd != registered_repository.expanduser().resolve():
            raise SafetyViolation("operation outside the registered repository")

        executable = Path(argv[0]).name.lower()
        lowered = [item.lower() for item in argv[1:]]
        all_tokens = {part for item in lowered for part in re.split(r"[^a-z0-9_-]+", item) if part}
        if cls.PROHIBITED_WORDS & all_tokens:
            raise SafetyViolation("release, publication, deployment, and notarization operations are prohibited")

        if executable == "git":
            if not lowered:
                raise SafetyViolation("unstructured git invocation is prohibited")
            operation = lowered[0]
            if operation in {"push", "tag", "merge", "stash", "clean", "reset", "rebase"}:
                raise SafetyViolation(f"git {operation} is prohibited for the controller")
            if operation == "checkout" or "--force" in lowered or "-f" in lowered or "-d" in lowered or "-D" in argv[1:]:
                raise SafetyViolation("discarding checkout, force, or branch deletion is prohibited")
            if operation not in cls.SAFE_GIT_READ_COMMANDS:
                raise SafetyViolation(f"git {operation} is not on the controller allowlist")
            return

        if executable == "codex" and allow_codex:
            if "--dangerously-bypass-approvals-and-sandbox" in lowered or "--ignore-rules" in lowered:
                raise SafetyViolation("unsafe Codex launcher options are prohibited")
            return

        raise SafetyViolation(f"controller executable is not allowlisted: {executable}")

    @classmethod
    def validate_configured_command(
        cls,
        argv: list[str],
        *,
        cwd: Path,
        registered_repository: Path,
    ) -> None:
        """Authorize one adapter-pinned validation command before subprocess launch."""

        cls._observe_command_attempt(argv, cwd=cwd, authority="configured_validation")
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise SafetyViolation("configured commands must be non-empty argument arrays")
        if cwd.expanduser().resolve() != registered_repository.expanduser().resolve():
            raise SafetyViolation("configured command is outside the registered repository")
        executable = Path(argv[0]).name.lower()
        lowered = [item.lower() for item in argv[1:]]
        tokens = {
            part
            for item in [executable, *lowered]
            for part in re.split(r"[^a-z0-9_-]+", item)
            if part
        }
        if cls._is_swift_release_build(executable, lowered):
            tokens.discard("release")
        if cls.PROHIBITED_WORDS & tokens or "notarytool" in tokens:
            raise SafetyViolation(
                "configured release, publication, deployment, and notarization operations are prohibited"
            )
        if executable in {"sh", "bash", "zsh", "dash", "fish"} and "-c" in lowered:
            raise SafetyViolation("configured shell command strings are prohibited")
        if executable == "git":
            if not lowered or lowered[0] not in cls.SAFE_GIT_READ_COMMANDS:
                operation = lowered[0] if lowered else "<missing>"
                raise SafetyViolation(f"configured git {operation} is not a read-only operation")

    @classmethod
    def validate_feature_branch_switch(
        cls,
        argv: list[str],
        *,
        cwd: Path,
        registered_repository: Path,
        branch: str,
        starting_commit: str | None,
    ) -> None:
        """Allow only a non-forced feature-branch switch at an exact repository root."""

        cls._observe_command_attempt(argv, cwd=cwd, authority="feature_branch_switch")

        resolved_cwd = cwd.expanduser().resolve()
        if resolved_cwd != registered_repository.expanduser().resolve():
            raise SafetyViolation("feature-branch recovery is outside the registered repository")
        expected = (
            ["git", "switch", "-c", branch, starting_commit]
            if starting_commit is not None
            else ["git", "switch", branch]
        )
        if argv != expected:
            raise SafetyViolation("only the exact non-forced feature-branch switch is allowed")

    @classmethod
    def validate_planning_git_mutation(
        cls,
        argv: list[str],
        *,
        cwd: Path,
        registered_repository: Path,
        changed_paths: list[str],
        commit_subject: str,
    ) -> None:
        """Allow only exact-path staging and one non-amending planning commit."""

        cls._observe_command_attempt(argv, cwd=cwd, authority="planning_git_mutation")

        if cwd.expanduser().resolve() != registered_repository.expanduser().resolve():
            raise SafetyViolation("planning Git mutation is outside the registered repository")
        if not changed_paths or changed_paths != sorted(set(changed_paths)):
            raise SafetyViolation("planning paths must be a non-empty sorted unique list")
        for relative in changed_paths:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
                raise SafetyViolation(f"unsafe planning path: {relative}")
        allowed = ["git", "add", "--", *changed_paths]
        commit = ["git", "commit", "-m", commit_subject, "--", *changed_paths]
        if tuple(argv) not in {tuple(allowed), tuple(commit)}:
            raise SafetyViolation("only exact planning-path staging and commit are allowed")
        if argv[:2] == ["git", "commit"] and (
            not commit_subject.startswith(("factory: reconcile ", "factory: scope "))
            or "--amend" in argv
            or "--no-verify" in argv
        ):
            raise SafetyViolation("planning commit subject or options are not authorized")
