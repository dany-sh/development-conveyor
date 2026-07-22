"""Read-only, exact-identity Git repository inspection."""

from __future__ import annotations

import hashlib
import json
import subprocess
import os
import stat
import contextvars
import fcntl
import tempfile
from contextlib import contextmanager
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
    _content_read_observer: contextvars.ContextVar[list[str] | None] = (
        contextvars.ContextVar("repository_content_read_observer", default=None)
    )

    @classmethod
    @contextmanager
    def observe_content_reads(cls):
        observed: list[str] = []
        token = cls._content_read_observer.set(observed)
        try:
            yield observed
        finally:
            cls._content_read_observer.reset(token)

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
        environment = dict(os.environ)
        # Read-only inspection must never refresh application index stat data.
        # Mandatory locks for explicit Git mutations still work normally.
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            argv, cwd=self.root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, env=environment,
        )
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

    def worktrees(self) -> list[dict[str, str | None]]:
        entries: list[dict[str, str | None]] = []
        current: dict[str, str | None] = {}
        for line in self.git(["worktree", "list", "--porcelain"]).stdout.splitlines() + [""]:
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                current[key] = value or None
        return entries

    def local_branches(self) -> list[str]:
        output = self.git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"]).stdout
        return [line for line in output.splitlines() if line]

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

    def modified_file_hashes(self) -> dict[str, str]:
        """Hash an unambiguous dirty set consisting only of tracked modified files."""

        raw = self.git(["status", "--porcelain=v1", "-z"]).stdout
        hashes: dict[str, str] = {}
        for entry in (item for item in raw.split("\0") if item):
            if len(entry) < 4 or entry[:2] not in {" M", "M ", "MM"} or entry[2] != " ":
                raise RepositoryError(
                    "dirty-state recovery supports only tracked modified files; "
                    f"ambiguous status entry: {entry[:2]!r}"
                )
            relative = entry[3:]
            candidate = (self.root / relative).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError as exc:
                raise RepositoryError("dirty path escapes the registered repository") from exc
            if not candidate.is_file():
                raise RepositoryError(f"dirty path is not a regular file: {relative}")
            hashes[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return dict(sorted(hashes.items()))

    def planning_diff(self) -> bytes:
        """Return the exact tracked worktree delta from HEAD for planning evidence."""

        argv = ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"]
        SafetyPolicy.validate_controller_command(
            argv, cwd=self.root, registered_repository=self.root
        )
        result = subprocess.run(
            argv,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RepositoryError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "cannot capture planning diff"
            )
        return result.stdout

    def planning_diff_fingerprint(self) -> str:
        return hashlib.sha256(self.planning_diff()).hexdigest()

    def content_diff_fingerprint(
        self, paths: list[str] | tuple[str, ...], *, commit: str | None = None,
    ) -> str:
        """Fingerprint resulting path contents identically before/after commit."""

        digest = hashlib.sha256()
        for relative in sorted(set(paths)):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if commit is None:
                content = self.safe_worktree_file_bytes(relative, allow_missing=True)
            else:
                result = subprocess.run(
                    ["git", "show", f"{commit}:{relative}"], cwd=self.root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                )
                content = result.stdout if result.returncode == 0 else None
            if content is None:
                digest.update(b"deleted\0")
            else:
                digest.update(b"file\0")
                digest.update(hashlib.sha256(content).digest())
            digest.update(b"\0")
        return digest.hexdigest()

    def safe_worktree_file_bytes(
        self, relative: str, *, allow_missing: bool = False
    ) -> bytes | None:
        """Read one repository file through a no-follow, single-link descriptor."""

        candidate = self.root / relative
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(self.root)
        except ValueError as exc:
            raise RepositoryError(f"worktree path escapes the registered repository: {relative}") from exc
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise RepositoryError(f"worktree path is missing: {relative}")
        except OSError as exc:
            raise RepositoryError(f"cannot securely open worktree path: {relative}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RepositoryError(f"worktree path is not a regular file: {relative}")
            if metadata.st_nlink != 1:
                raise RepositoryError(f"worktree path has an unsafe hard-link count: {relative}")
            observer = self._content_read_observer.get()
            if observer is not None:
                observer.append(relative)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def tracked_changed_paths(self) -> list[str]:
        output = self.git(["diff", "--name-only", "HEAD", "--"]).stdout
        return sorted(line for line in output.splitlines() if line)

    def staged_changed_paths(self) -> list[str]:
        output = self.git(["diff", "--cached", "--name-only", "--"]).stdout
        return sorted(line for line in output.splitlines() if line)

    def untracked_file_hashes(self) -> dict[str, str]:
        output = self.git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        ).stdout
        hashes: dict[str, str] = {}
        entries = [item for item in output.split("\0") if item]
        for entry in entries:
            if not entry.startswith("?? "):
                continue
            relative = entry[3:]
            if relative in {
                ".factory/locks/writer.json",
                ".factory/conveyor-state.json",
            } or relative.startswith(".factory/runtime/"):
                continue
            payload = self.safe_worktree_file_bytes(relative)
            assert payload is not None
            hashes[relative] = hashlib.sha256(payload).hexdigest()
        return dict(sorted(hashes.items()))

    def stage_planning_paths(self, paths: list[str], *, commit_subject: str) -> None:
        argv = ["git", "add", "--", *paths]
        SafetyPolicy.validate_planning_git_mutation(
            argv,
            cwd=self.root,
            registered_repository=self.root,
            changed_paths=paths,
            commit_subject=commit_subject,
        )
        result = subprocess.run(
            argv, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
        if result.returncode != 0:
            raise RepositoryError(result.stderr.strip() or "planning-path staging failed")

    def commit_planning_paths(self, paths: list[str], *, commit_subject: str) -> str:
        argv = ["git", "commit", "-m", commit_subject, "--", *paths]
        SafetyPolicy.validate_planning_git_mutation(
            argv,
            cwd=self.root,
            registered_repository=self.root,
            changed_paths=paths,
            commit_subject=commit_subject,
        )
        result = subprocess.run(
            argv, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
        if result.returncode != 0:
            raise RepositoryError(result.stderr.strip() or result.stdout.strip() or "planning commit failed")
        return self.head

    @property
    def is_clean(self) -> bool:
        return not self.dirty_entries

    def ref_exists(self, value: str) -> bool:
        return self.rev_parse(f"{value}^{{commit}}", check=False) is not None

    def branch_worktree(self, branch: str) -> Path | None:
        reference = f"refs/heads/{branch}"
        matches = [
            Path(str(item["worktree"])).resolve()
            for item in self.worktrees()
            if item.get("branch") == reference and item.get("worktree")
        ]
        if len(matches) > 1:
            raise RepositoryError(f"feature branch is checked out in multiple worktrees: {branch}")
        return matches[0] if matches else None

    def switch_feature_branch(self, branch: str, *, starting_commit: str | None = None) -> GitResult:
        """Switch without force, checkout, stash, reset, clean, or content regeneration."""

        valid = self.git(["check-ref-format", "--branch", branch], check=False)
        if valid.returncode != 0:
            raise RepositoryError(f"invalid feature branch name: {branch}")
        argv = (
            ["git", "switch", "-c", branch, starting_commit]
            if starting_commit is not None
            else ["git", "switch", branch]
        )
        SafetyPolicy.validate_feature_branch_switch(
            argv,
            cwd=self.root,
            registered_repository=self.root,
            branch=branch,
            starting_commit=starting_commit,
        )
        result = subprocess.run(
            argv,
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RepositoryError(
                result.stderr.strip() or result.stdout.strip() or "feature branch switch failed"
            )
        return GitResult(tuple(argv), result.stdout, result.stderr, result.returncode)

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

        patterns = [
            ".factory/conveyor-state.json",
            ".factory/locks/writer.json",
            ".factory/runtime/",
        ]
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

    def ensure_milestone_integration_runtime_ignored(self) -> dict[str, Any]:
        """Install and verify the exact local integration-runtime exclusion.

        The root-anchored pattern lives only in the repository's common Git
        directory.  A repository-identity check and an advisory lock make the
        operation fail closed if the target changes while it is installed.
        """

        pattern = "/.factory/runtime/milestone-integration/"
        descendants = (
            ".factory/runtime/milestone-integration/latest.json",
            ".factory/runtime/milestone-integration/F005-plan.json",
        )
        initial_identity = self.identity()
        common = self.common_git_dir
        tracked_ignore = self.root / ".gitignore"
        tracked_ignore_before = tracked_ignore.read_bytes() if tracked_ignore.exists() else None
        info = common / "info"
        info.mkdir(parents=True, exist_ok=True)
        lock_path = info / ".development-conveyor-runtime-exclude.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        original: bytes | None = None
        changed = False
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RepositoryError("integration-runtime exclude lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current_identity = self.identity()
            if (
                current_identity != initial_identity
                or Path(current_identity["common_git_dir"]).resolve() != common
            ):
                raise RepositoryError(
                    "repository identity changed while installing the integration-runtime exclusion"
                )

            exclude = info / "exclude"
            if exclude.exists() or exclude.is_symlink():
                target = os.lstat(exclude)
                if (
                    stat.S_ISLNK(target.st_mode)
                    or not stat.S_ISREG(target.st_mode)
                    or target.st_nlink != 1
                ):
                    raise RepositoryError("common Git info/exclude is unsafe")
                original = exclude.read_bytes()
            else:
                original = b""
            try:
                existing = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RepositoryError("common Git info/exclude is not UTF-8") from exc

            if pattern not in existing.splitlines():
                separator = "" if not existing or existing.endswith("\n") else "\n"
                rendered = (existing + separator + pattern + "\n").encode("utf-8")
                temporary_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".exclude.", dir=info
                )
                try:
                    os.fchmod(temporary_descriptor, 0o644)
                    os.write(temporary_descriptor, rendered)
                    os.fsync(temporary_descriptor)
                    os.close(temporary_descriptor)
                    temporary_descriptor = -1
                    os.replace(temporary_name, exclude)
                    directory_descriptor = os.open(info, os.O_RDONLY)
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
                finally:
                    if temporary_descriptor >= 0:
                        os.close(temporary_descriptor)
                    if os.path.exists(temporary_name):
                        os.unlink(temporary_name)
                changed = True

            verified: list[str] = []
            for relative in descendants:
                result = self.git(
                    ["check-ignore", "-q", "--no-index", "--", relative],
                    check=False,
                )
                if result.returncode != 0:
                    if changed and original is not None:
                        exclude.write_bytes(original)
                    raise RepositoryError(
                        f"integration-runtime exclusion did not cover {relative}"
                    )
                verified.append(relative)
            if self.identity() != initial_identity:
                if changed and original is not None:
                    exclude.write_bytes(original)
                raise RepositoryError(
                    "repository identity changed after installing the integration-runtime exclusion"
                )
            tracked_ignore_after = (
                tracked_ignore.read_bytes() if tracked_ignore.exists() else None
            )
            if tracked_ignore_after != tracked_ignore_before:
                if changed and original is not None:
                    exclude.write_bytes(original)
                raise RepositoryError(
                    "local integration-runtime exclusion changed tracked .gitignore"
                )
            return {
                "pattern": pattern,
                "common_git_dir": str(common),
                "changed": changed,
                "verified_descendants": verified,
                "repository_identity": initial_identity["repository_id"],
                "repository_path_fingerprint": initial_identity["path_fingerprint"],
                "tracked_gitignore_sha256_before": (
                    hashlib.sha256(tracked_ignore_before).hexdigest()
                    if tracked_ignore_before is not None
                    else None
                ),
                "tracked_gitignore_sha256_after": (
                    hashlib.sha256(tracked_ignore_after).hexdigest()
                    if tracked_ignore_after is not None
                    else None
                ),
            }
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def patch_fingerprint(self, commit: str) -> str:
        parent = self.rev_parse(f"{commit}^")
        if parent is None:
            raise RepositoryError(f"accepted commit has no parent: {commit}")
        payload = self.git(["diff", "--binary", parent, commit]).stdout.encode()
        return hashlib.sha256(payload).hexdigest()

    def commit_subject(self, commit: str) -> str:
        return self.git(["show", "-s", "--format=%s", commit]).stdout.strip()

    def commit_timestamp(self, commit: str) -> str:
        return self.git(["show", "-s", "--format=%cI", commit]).stdout.strip()

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
            "head_commit_timestamp": self.commit_timestamp(self.head),
            "branch": self.current_branch,
            "clean": self.is_clean,
            "dirty_entry_count": len(self.dirty_entries),
            "git_operations": operations,
            "baseline_exists": baseline_exists,
            "milestone_branch_exists": milestone_exists,
            "milestone_branch_head": self.rev_parse(milestone_branch, check=False) if milestone_branch else None,
            "baseline_is_ancestor_of_milestone": baseline_is_ancestor,
            "cycle_state_exists": self.cycle_state_path().exists(),
            "writer_lock_exists": self.writer_lock_path().exists(),
            "worktrees": self.worktrees(),
            "local_branches": self.local_branches(),
        }
