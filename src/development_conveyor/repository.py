"""Read-only, exact-identity Git repository inspection."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RepositoryError
from .validation import SafetyPolicy


@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class RepositoryInspector:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise RepositoryError(f"repository does not exist: {self.root}")
        discovered = self.git(["rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(discovered).resolve() != self.root:
            raise RepositoryError(f"registered path is not the exact Git root: {self.root}")

    def git(self, arguments: list[str], check: bool = True) -> GitResult:
        argv = ["git", *arguments]
        SafetyPolicy.validate_controller_command(argv, cwd=self.root, registered_repository=self.root)
        result = subprocess.run(argv, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if check and result.returncode != 0:
            raise RepositoryError(result.stderr.strip() or result.stdout.strip() or f"Git command failed: {arguments!r}")
        return GitResult(tuple(argv), result.stdout, result.stderr, result.returncode)

    def rev_parse(self, value: str, check: bool = True) -> str | None:
        result = self.git(["rev-parse", "--verify", value], check=check)
        return result.stdout.strip() if result.returncode == 0 else None

    @property
    def common_git_dir(self) -> Path:
        value = self.git(["rev-parse", "--git-common-dir"]).stdout.strip()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    @property
    def primary_worktree(self) -> Path:
        output = self.git(["worktree", "list", "--porcelain"]).stdout
        for line in output.splitlines():
            if line.startswith("worktree "):
                return Path(line.removeprefix("worktree ")).resolve()
        return self.root

    @property
    def current_branch(self) -> str | None:
        value = self.git(["branch", "--show-current"]).stdout.strip()
        return value or None

    @property
    def head(self) -> str:
        value = self.rev_parse("HEAD")
        if value is None:
            raise RepositoryError("repository has no HEAD")
        return value

    @property
    def dirty_entries(self) -> list[str]:
        return [line for line in self.git(["status", "--porcelain"]).stdout.splitlines() if line]

    @property
    def is_clean(self) -> bool:
        return not self.dirty_entries

    def ref_exists(self, value: str) -> bool:
        return self.rev_parse(f"{value}^{{commit}}", check=False) is not None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.git(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        return result.returncode == 0

    def commit_count(self, start: str, end: str) -> int:
        value = self.git(["rev-list", "--count", f"{start}..{end}"]).stdout.strip()
        return int(value)

    def git_operation_state(self) -> dict[str, bool]:
        names = {
            "cherry_pick": "CHERRY_PICK_HEAD",
            "merge": "MERGE_HEAD",
            "rebase_merge": "rebase-merge",
            "rebase_apply": "rebase-apply",
        }
        result: dict[str, bool] = {}
        for key, name in names.items():
            value = self.git(["rev-parse", "--git-path", name]).stdout.strip()
            path = Path(value)
            resolved = path if path.is_absolute() else self.root / path
            result[key] = resolved.exists()
        return result

    def identity(self) -> dict[str, Any]:
        roots = [line.strip() for line in self.git(["rev-list", "--max-parents=0", "HEAD"]).stdout.splitlines() if line.strip()]
        identity_material = {"common_git_dir": str(self.common_git_dir), "root_commits": sorted(roots)}
        repository_id = hashlib.sha256(json.dumps(identity_material, sort_keys=True).encode()).hexdigest()
        path_fingerprint = hashlib.sha256(str(self.root).encode()).hexdigest()
        return {
            "repository_id": repository_id,
            "path_fingerprint": path_fingerprint,
            "root": str(self.root),
            "common_git_dir": str(self.common_git_dir),
            "root_commits": sorted(roots),
        }

    def writer_lock_path(self, relative: str = ".factory/locks/writer.json") -> Path:
        return self.primary_worktree / relative

    def cycle_state_path(self) -> Path:
        return self.primary_worktree / ".factory/conveyor-state.json"

    def ensure_runtime_ignored(self) -> list[str]:
        """Ignore runtime-only state in local Git metadata, never tracked source."""

        patterns = [".factory/conveyor-state.json", ".factory/locks/writer.json"]
        exclude = self.common_git_dir / "info/exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        lines = existing.splitlines()
        additions = [pattern for pattern in patterns if pattern not in lines]
        if additions:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(separator + "\n".join(additions) + "\n")
                handle.flush()
        return additions

    def patch_fingerprint(self, commit: str) -> str:
        parent = self.rev_parse(f"{commit}^")
        if parent is None:
            raise RepositoryError(f"accepted commit has no parent: {commit}")
        payload = self.git(["diff", "--binary", parent, commit]).stdout.encode()
        return hashlib.sha256(payload).hexdigest()

    def commit_subject(self, commit: str) -> str:
        return self.git(["show", "-s", "--format=%s", commit]).stdout.strip()

    def file_at_commit(self, commit: str, relative_path: str) -> str | None:
        """Return a repository file at a commit without reading outside the tree."""

        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise RepositoryError(f"unsafe repository-relative path: {relative_path}")
        result = self.git(["show", f"{commit}:{path.as_posix()}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def changed_paths(self, commit: str) -> list[str]:
        output = self.git(["show", "--format=", "--name-only", commit]).stdout
        return [line.strip() for line in output.splitlines() if line.strip()]

    def inspect(self, *, baseline: str | None = None, milestone_branch: str | None = None) -> dict[str, Any]:
        identity = self.identity()
        operations = self.git_operation_state()
        baseline_exists = self.ref_exists(baseline) if baseline else None
        milestone_exists = self.ref_exists(milestone_branch) if milestone_branch else None
        baseline_is_ancestor = (
            self.is_ancestor(baseline, milestone_branch)
            if baseline and milestone_branch and baseline_exists and milestone_exists
            else None
        )
        return {
            "identity": identity,
            "head": self.head,
            "branch": self.current_branch,
            "clean": self.is_clean,
            "dirty_entry_count": len(self.dirty_entries),
            "git_operations": operations,
            "baseline_exists": baseline_exists,
            "milestone_branch_exists": milestone_exists,
            "baseline_is_ancestor_of_milestone": baseline_is_ancestor,
            "cycle_state_exists": self.cycle_state_path().exists(),
            "writer_lock_exists": self.writer_lock_path().exists(),
        }
