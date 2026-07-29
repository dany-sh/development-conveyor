"""Exact-match writer leases used by the transactional workflow kernel."""

from __future__ import annotations

import json
import hashlib
import os
import socket
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import LeaseType, MutationPolicy, WorkflowType, WORKFLOW_LEASE
from .errors import AmbiguousLockError, LockError
from .locks import process_alive
from .logging import utc_now


_UNSET = object()


def process_start_evidence(process_id: int) -> str:
    """Return stable local process-start evidence without treating time as liveness."""

    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(process_id)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = result.stdout.strip()
    return value or f"process-start-unavailable:{process_id}"


@dataclass(frozen=True)
class WorkflowLeaseRecord:
    schema_version: int
    lease_id: str
    lease_type: LeaseType
    repository_identity: str
    repository_path_fingerprint: str
    repository_path: str
    project_id: str
    controller_project_id: str | None
    adapter_project_id: str | None
    transaction_id: str
    workflow_type: WorkflowType
    milestone: str | None
    feature_id: str | None
    feature_branch: str | None
    accepted_commit: str | None
    starting_branch: str
    starting_head: str
    run_id: str
    session_id: str | None
    owner_pid: int
    owner_process_start: str
    owner_host: str
    created_at: str
    last_heartbeat: str
    allowed_mutations: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["lease_type"] = self.lease_type.value
        value["workflow_type"] = self.workflow_type.value
        value.update({
            "repository": self.repository_path,
            "worktree": self.repository_path,
            "branch": self.starting_branch,
            "agent_run": self.run_id,
            "phase": self.workflow_type.value,
            "purpose": {
                WorkflowType.QUEUE_RECONCILIATION: "development-conveyor-planning",
                WorkflowType.FEATURE_PREPARATION: "development-conveyor-feature-preparation",
                WorkflowType.FEATURE_EXECUTION: "development-conveyor-feature",
                WorkflowType.FEATURE_ACCEPTANCE: "development-conveyor-feature-acceptance",
                WorkflowType.MILESTONE_INTEGRATION: "development-conveyor-integration",
                WorkflowType.MILESTONE_GATE: "development-conveyor-milestone-gate",
                WorkflowType.HUMAN_DECISION_RESOLUTION: "development-conveyor-recovery",
                WorkflowType.RECOVERY: "development-conveyor-recovery",
            }[self.workflow_type],
        })
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowLeaseRecord":
        try:
            record = cls(
                schema_version=value["schema_version"],
                lease_id=value["lease_id"],
                lease_type=LeaseType(value["lease_type"]),
                repository_identity=value["repository_identity"],
                repository_path_fingerprint=value["repository_path_fingerprint"],
                repository_path=value.get("repository_path") or value.get("repository") or value.get("worktree"),
                project_id=value["project_id"],
                controller_project_id=value.get("controller_project_id"),
                adapter_project_id=value.get("adapter_project_id"),
                transaction_id=value["transaction_id"],
                workflow_type=WorkflowType(value["workflow_type"]),
                milestone=value.get("milestone"),
                feature_id=value.get("feature_id"),
                feature_branch=value.get("feature_branch"),
                accepted_commit=value.get("accepted_commit"),
                starting_branch=value["starting_branch"],
                starting_head=value["starting_head"],
                run_id=value["run_id"],
                session_id=value.get("session_id"),
                owner_pid=value["owner_pid"],
                owner_process_start=value["owner_process_start"],
                owner_host=value["owner_host"],
                created_at=value["created_at"],
                last_heartbeat=value["last_heartbeat"],
                allowed_mutations=value["allowed_mutations"],
            )
            if not record.repository_path:
                raise ValueError("repository path is absent")
            return record
        except (KeyError, TypeError, ValueError) as exc:
            raise AmbiguousLockError("writer lease does not satisfy the typed lease contract") from exc


class WorkflowWriterLease:
    def __init__(self, path: Path):
        # Preserve the lexical target. Resolving here would turn a hostile
        # symlink into an apparently legitimate out-of-repository write path.
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._confinement_root = self._derive_confinement_root()

    def _derive_confinement_root(self) -> Path:
        parts = self.path.parts
        if ".factory" in parts:
            index = parts.index(".factory")
            return Path(*parts[:index])
        return self.path.parent

    def _reject_symlinked_target_or_ancestors(self) -> None:
        current = self._confinement_root
        try:
            relative = self.path.relative_to(current)
        except ValueError as exc:
            raise LockError("writer lease path escapes its confinement root") from exc
        for component in relative.parts:
            current = current / component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise LockError("writer lease path contains a symbolic link")
            if current != self.path and not stat.S_ISDIR(metadata.st_mode):
                raise LockError("writer lease ancestor is not a directory")

    @staticmethod
    def _open_flags(base: int) -> int:
        return base | getattr(os, "O_NOFOLLOW", 0)

    @staticmethod
    def _validate_descriptor(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LockError("writer lease is not a regular file")
        if metadata.st_nlink != 1:
            raise LockError("writer lease has an unsafe hard-link count")

    def _write_existing(self, value: dict[str, Any]) -> None:
        self._reject_symlinked_target_or_ancestors()
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            existing = os.open(self.path, self._open_flags(os.O_RDONLY))
        except OSError as exc:
            raise LockError("writer lease cannot be securely opened for rewrite") from exc
        try:
            self._validate_descriptor(existing)
        finally:
            os.close(existing)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                self._open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
            )
            self._validate_descriptor(descriptor)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._reject_symlinked_target_or_ancestors()
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, LockError) as exc:
            raise LockError("writer lease atomic rewrite failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def read(self) -> WorkflowLeaseRecord | None:
        self._reject_symlinked_target_or_ancestors()
        try:
            descriptor = os.open(self.path, self._open_flags(os.O_RDONLY))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AmbiguousLockError("writer lease cannot be securely opened") from exc
        try:
            self._validate_descriptor(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = json.load(handle)
        except (OSError, json.JSONDecodeError, LockError) as exc:
            raise AmbiguousLockError("writer lease is unreadable or unsafe") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, dict):
            raise AmbiguousLockError("writer lease root must be an object")
        return WorkflowLeaseRecord.from_dict(value)

    def acquire(
        self,
        *,
        lease_type: LeaseType,
        repository_identity: str,
        repository_path_fingerprint: str,
        repository_path: str | Path | None = None,
        project_id: str,
        transaction_id: str,
        workflow_type: WorkflowType,
        milestone: str | None,
        feature_id: str | None,
        starting_branch: str,
        starting_head: str,
        run_id: str,
        session_id: str | None,
        policy: MutationPolicy,
    ) -> WorkflowLeaseRecord:
        if lease_type != WORKFLOW_LEASE[workflow_type]:
            raise LockError(f"{lease_type.value} cannot authorize {workflow_type.value}")
        self._reject_symlinked_target_or_ancestors()
        if self.path.exists() or self.path.is_symlink():
            raise LockError("repository writer lease already exists")
        invoking_worktree = (
            self._confinement_root.resolve()
            if repository_path is None
            else Path(repository_path).expanduser().resolve()
        )
        if (
            repository_path is not None
            and hashlib.sha256(str(invoking_worktree).encode()).hexdigest()
            != repository_path_fingerprint
        ):
            raise LockError("writer lease worktree path fingerprint mismatch")
        stamp = utc_now()
        record = WorkflowLeaseRecord(
            schema_version=1,
            lease_id=str(uuid.uuid4()),
            lease_type=lease_type,
            repository_identity=repository_identity,
            repository_path_fingerprint=repository_path_fingerprint,
            repository_path=str(invoking_worktree),
            project_id=project_id,
            controller_project_id=None,
            adapter_project_id=None,
            transaction_id=transaction_id,
            workflow_type=workflow_type,
            milestone=milestone,
            feature_id=feature_id,
            feature_branch=None,
            accepted_commit=None,
            starting_branch=starting_branch,
            starting_head=starting_head,
            run_id=run_id,
            session_id=session_id,
            owner_pid=os.getpid(),
            owner_process_start=process_start_evidence(os.getpid()),
            owner_host=socket.gethostname(),
            created_at=stamp,
            last_heartbeat=stamp,
            allowed_mutations=policy.to_dict(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlinked_target_or_ancestors()
        encoded = (json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor = -1
        try:
            descriptor = os.open(
                self.path,
                self._open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
            )
            self._validate_descriptor(descriptor)
        except (FileExistsError, OSError, LockError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise LockError("repository writer lease appeared concurrently") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def revalidate(
        self,
        *,
        transaction_id: str,
        workflow_type: WorkflowType,
        repository_identity: str,
        project_id: str,
        lease_type: LeaseType | None = None,
        repository_path_fingerprint: str | None = None,
        repository_path: str | Path | None = None,
        lease_id: str | None = None,
        milestone: str | None | object = _UNSET,
        feature_id: str | None | object = _UNSET,
        starting_branch: str | None = None,
        starting_head: str | None = None,
        run_id: str | None = None,
        session_id: str | None | object = _UNSET,
        policy: MutationPolicy | None = None,
    ) -> WorkflowLeaseRecord:
        record = self.read()
        if record is None:
            raise LockError("writer lease is absent")
        expected_type = lease_type or WORKFLOW_LEASE[workflow_type]
        checks = {
            "transaction": record.transaction_id == transaction_id,
            "workflow": record.workflow_type == workflow_type,
            "lease_type": record.lease_type == expected_type,
            "repository": record.repository_identity == repository_identity,
            "project": record.project_id == project_id,
            "repository_path_fingerprint": (
                repository_path_fingerprint is None
                or record.repository_path_fingerprint == repository_path_fingerprint
            ),
            "repository_path": (
                repository_path is None
                or Path(record.repository_path).resolve() == Path(repository_path).resolve()
            ),
            "lease_id": lease_id is None or record.lease_id == lease_id,
            "milestone": milestone is _UNSET or record.milestone == milestone,
            "feature": feature_id is _UNSET or record.feature_id == feature_id,
            "starting_branch": starting_branch is None or record.starting_branch == starting_branch,
            "starting_head": starting_head is None or record.starting_head == starting_head,
            "run_id": run_id is None or record.run_id == run_id,
            "session": session_id is _UNSET or record.session_id == session_id,
            "mutation_policy": (
                policy is None
                or record.allowed_mutations
                == json.loads(json.dumps(policy.to_dict(), sort_keys=True))
            ),
            "host": record.owner_host == socket.gethostname(),
            "pid": record.owner_pid == os.getpid(),
            "process_start": record.owner_process_start == process_start_evidence(os.getpid()),
        }
        if not all(checks.values()):
            failed = ", ".join(key for key, passed in checks.items() if not passed)
            raise LockError(f"writer lease identity mismatch: {failed}")
        return record

    def heartbeat(self, *, transaction_id: str, workflow_type: WorkflowType, repository_identity: str, project_id: str) -> WorkflowLeaseRecord:
        record = self.revalidate(
            transaction_id=transaction_id,
            workflow_type=workflow_type,
            repository_identity=repository_identity,
            project_id=project_id,
        )
        value = record.to_dict()
        value["last_heartbeat"] = utc_now()
        self._write_existing(value)
        return WorkflowLeaseRecord.from_dict(value)

    def revalidate_adopted(self, expected_identity: dict[str, Any]) -> WorkflowLeaseRecord:
        """Verify a controller-owned lease without transferring ownership.

        Deterministic integration executors may run in a child process.  They
        can verify and heartbeat the controller lease, but cannot acquire,
        replace, bind a session to, or release it.
        """

        record = self.read()
        if record is None:
            raise LockError("controller integration lease is absent")
        actual = json.loads(json.dumps(record.to_dict(), sort_keys=True))
        identity_fields = (
            "lease_id",
            "lease_type",
            "repository_identity",
            "repository_path_fingerprint",
            "repository_path",
            "project_id",
            "controller_project_id",
            "adapter_project_id",
            "transaction_id",
            "workflow_type",
            "milestone",
            "feature_id",
            "feature_branch",
            "accepted_commit",
            "starting_branch",
            "starting_head",
            "run_id",
            "session_id",
            "owner_pid",
            "owner_process_start",
            "owner_host",
            "allowed_mutations",
        )
        legacy_identity = (
            "controller_project_id" not in expected_identity
            and "adapter_project_id" not in expected_identity
            and actual.get("controller_project_id") is None
            and actual.get("adapter_project_id") is None
        )
        if legacy_identity:
            identity_fields = tuple(
                field
                for field in identity_fields
                if field not in {"controller_project_id", "adapter_project_id"}
            )
        missing = [field for field in identity_fields if field not in expected_identity]
        if missing:
            raise LockError(
                "controller integration plan has incomplete lease identity: "
                + ", ".join(missing)
            )
        mismatched = [
            field for field in identity_fields
            if actual.get(field) != expected_identity.get(field)
        ]
        if mismatched:
            raise LockError(
                "controller integration lease identity mismatch: "
                + ", ".join(mismatched)
            )
        if record.session_id is not None:
            raise LockError("deterministic integration lease must not be bound to a model session")
        if record.owner_host != socket.gethostname():
            raise LockError("controller integration lease owner host does not match")
        if not process_alive(record.owner_pid):
            raise LockError("controller integration lease owner process is not alive")
        if process_start_evidence(record.owner_pid) != record.owner_process_start:
            raise LockError("controller integration lease owner process-start evidence changed")
        return record

    def bind_controller_plan(
        self,
        *,
        transaction_id: str,
        repository_identity: str,
        controller_project_id: str,
        adapter_project_id: str,
        feature_branch: str,
        accepted_commit: str,
    ) -> WorkflowLeaseRecord:
        """Bind an owned integration lease to the controller's immutable refs."""

        record = self.revalidate(
            transaction_id=transaction_id,
            workflow_type=WorkflowType.MILESTONE_INTEGRATION,
            repository_identity=repository_identity,
            project_id=controller_project_id,
            session_id=None,
        )
        if not controller_project_id or not adapter_project_id:
            raise LockError("controller integration project identity binding is incomplete")
        if not feature_branch or not accepted_commit:
            raise LockError("controller integration ref binding is incomplete")
        if record.controller_project_id not in {None, controller_project_id}:
            raise LockError("controller integration controller-project binding changed")
        if record.adapter_project_id not in {None, adapter_project_id}:
            raise LockError("controller integration adapter-project binding changed")
        if record.feature_branch not in {None, feature_branch}:
            raise LockError("controller integration feature-branch binding changed")
        if record.accepted_commit not in {None, accepted_commit}:
            raise LockError("controller integration accepted-commit binding changed")
        value = record.to_dict()
        value["controller_project_id"] = controller_project_id
        value["adapter_project_id"] = adapter_project_id
        value["feature_branch"] = feature_branch
        value["accepted_commit"] = accepted_commit
        value["last_heartbeat"] = utc_now()
        self._write_existing(value)
        return WorkflowLeaseRecord.from_dict(value)

    def heartbeat_adopted(self, expected_identity: dict[str, Any]) -> WorkflowLeaseRecord:
        record = self.revalidate_adopted(expected_identity)
        value = record.to_dict()
        value["last_heartbeat"] = utc_now()
        self._write_existing(value)
        return WorkflowLeaseRecord.from_dict(value)

    def bind_session(self, *, transaction_id: str, workflow_type: WorkflowType, repository_identity: str, project_id: str, session_id: str) -> WorkflowLeaseRecord:
        record = self.revalidate(
            transaction_id=transaction_id,
            workflow_type=workflow_type,
            repository_identity=repository_identity,
            project_id=project_id,
        )
        # Repair continuations remain in the same transaction. The lease binds
        # the currently running session; the ledger retains all session IDs.
        value = record.to_dict()
        value["session_id"] = session_id
        value["last_heartbeat"] = utc_now()
        self._write_existing(value)
        return WorkflowLeaseRecord.from_dict(value)

    def release(
        self,
        *,
        transaction_id: str,
        workflow_type: WorkflowType,
        repository_identity: str,
        project_id: str,
        repository_path_fingerprint: str | None = None,
        repository_path: str | Path | None = None,
        lease_id: str | None = None,
        milestone: str | None | object = _UNSET,
        feature_id: str | None | object = _UNSET,
        starting_branch: str | None = None,
        starting_head: str | None = None,
        run_id: str | None = None,
        session_id: str | None | object = _UNSET,
        policy: MutationPolicy | None = None,
    ) -> None:
        record = self.read()
        if record is None:
            raise LockError("writer lease is absent")
        linked_worktree = (
            Path(record.repository_path).resolve()
            != self._confinement_root.resolve()
        )
        if linked_worktree and (
            repository_path_fingerprint is None
            or repository_path is None
            or run_id is None
        ):
            raise LockError(
                "linked-worktree lease release requires exact worktree and run identity"
            )
        self.revalidate(
            transaction_id=transaction_id,
            workflow_type=workflow_type,
            repository_identity=repository_identity,
            project_id=project_id,
            repository_path_fingerprint=repository_path_fingerprint,
            repository_path=repository_path,
            lease_id=lease_id,
            milestone=milestone,
            feature_id=feature_id,
            starting_branch=starting_branch,
            starting_head=starting_head,
            run_id=run_id,
            session_id=session_id,
            policy=policy,
        )
        self.path.unlink()

    def stale_evidence(self) -> dict[str, Any]:
        record = self.read()
        if record is None:
            return {"exists": False, "recoverable": False}
        same_host = record.owner_host == socket.gethostname()
        alive = process_alive(record.owner_pid) if same_host else None
        start_matches = (
            process_start_evidence(record.owner_pid) == record.owner_process_start
            if same_host and alive else None
        )
        return {
            "exists": True,
            "same_host": same_host,
            "process_alive": alive,
            "process_start_matches": start_matches,
            "recoverable": same_host and alive is False,
            "timestamp_used_as_stale_proof": False,
            "transaction_id": record.transaction_id,
            "workflow_type": record.workflow_type.value,
        }
