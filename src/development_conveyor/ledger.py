"""Controller-owned append-only, hash-chained workflow evidence ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .contracts import WorkflowType, fingerprint, validate_human_gate
from .errors import CorruptEvidenceError, EvidenceError
from .logging import utc_now
from .logging import atomic_write_json
from .redaction import redact_value


GENESIS_FINGERPRINT = "0" * 64
EVENT_TYPES = frozenset({
    "TransactionStarted", "LeaseAcquired", "LeaseReleased", "SnapshotCaptured",
    "SessionLaunched", "SessionResultAccepted", "ChangesDetected", "ValidationStarted",
    "ValidationPassed", "ValidationFailed", "CommitFinalized", "HumanGateRaised",
    "HumanGateResolved", "TransactionBlocked", "TransactionCompleted",
    "TransactionSuperseded", "RecoveryApplied", "ProjectionUpdated", "LegacyEvidenceImported",
    "CheckpointRecorded",
})
TERMINAL_EVENT_TYPES = frozenset({
    "TransactionBlocked", "TransactionCompleted", "TransactionSuperseded", "HumanGateRaised",
})

PAYLOAD_CONTRACTS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "TransactionStarted": {
        "run_id": str, "starting_branch": str, "starting_head": str,
        "allowed_mutation_policy": dict,
    },
    "LeaseAcquired": {"lease_id": str, "lease_type": str},
    "SnapshotCaptured": {"snapshot": dict},
    "SessionLaunched": {"session_id": str},
    "SessionResultAccepted": {
        "classification": str, "session_id": str, "changed_paths": list,
        "envelope": dict,
    },
    "ChangesDetected": {"changed_paths": list, "diff_fingerprint": str},
    "ValidationFailed": {"diagnostic": str},
    "CommitFinalized": {"commit": str, "changed_paths": list},
    "LeaseReleased": {"lease_id": (str, type(None))},
    "ProjectionUpdated": {"current_state": str},
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_fingerprint(event_without_fingerprint: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(event_without_fingerprint)).hexdigest()


@dataclass(frozen=True)
class LedgerIntegrity:
    valid: bool
    sequence: int
    fingerprint: str
    event_count: int
    transactions: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "sequence": self.sequence,
            "fingerprint": self.fingerprint,
            "event_count": self.event_count,
            "transactions": self.transactions,
            "errors": list(self.errors),
        }


class EvidenceLedger:
    """One project's single-writer evidence authority.

    Each append is one canonical JSON line written with ``O_APPEND`` while an
    advisory process lock is held. The previous fingerprint and sequence bind
    ordering; event IDs and terminal transaction checks bind uniqueness.
    """

    def __init__(
        self,
        path: Path,
        *,
        project_id: str,
        repository_identity: str,
        repository_path_fingerprint: str,
    ):
        self.path = Path(os.path.abspath(path.expanduser()))
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.head_path = self.path.with_name(f"{self.path.name}.head")
        self.project_id = project_id
        self.repository_identity = repository_identity
        self.repository_path_fingerprint = repository_path_fingerprint
        self._append_cache_signature: tuple[int, int, int] | None = None
        self._append_cache_events: list[dict[str, Any]] | None = None
        self._append_cache_integrity: LedgerIntegrity | None = None

    def _signature(self) -> tuple[int, int, int] | None:
        if not self.path.exists():
            return None
        value = self.path.lstat()
        if not stat.S_ISREG(value.st_mode):
            raise CorruptEvidenceError("evidence ledger path is not a regular file")
        return (value.st_ino, value.st_size, value.st_mtime_ns)

    @staticmethod
    def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags | nofollow, mode)
        except OSError as exc:
            raise CorruptEvidenceError(f"cannot securely open evidence path {path.name}: {exc}") from exc
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            os.close(descriptor)
            raise CorruptEvidenceError(f"evidence path {path.name} is not a regular file")
        return descriptor

    def _head(self) -> dict[str, Any] | None:
        if not self.head_path.exists():
            return None
        descriptor = self._open_regular(self.head_path, os.O_RDONLY)
        try:
            raw = b""
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptEvidenceError("ledger durable head anchor is malformed") from exc
        if (
            not isinstance(value, dict) or set(value) != {"schema_version", "sequence", "fingerprint"}
            or value.get("schema_version") != 1 or type(value.get("sequence")) is not int
            or not isinstance(value.get("fingerprint"), str)
        ):
            raise CorruptEvidenceError("ledger durable head anchor violates its contract")
        return value

    def _verify_head(self, events: list[dict[str, Any]], *, repair_one_ahead: bool = False) -> None:
        head = self._head()
        if not events:
            if head is not None:
                raise CorruptEvidenceError("ledger is shorter than its durable head anchor")
            return
        if head is None:
            raise CorruptEvidenceError("non-empty ledger lacks its durable head anchor")
        tail = events[-1]
        if head["sequence"] == tail["sequence"] and head["fingerprint"] == tail["fingerprint"]:
            return
        if (
            repair_one_ahead and tail["sequence"] == head["sequence"] + 1
            and tail["previous_fingerprint"] == head["fingerprint"]
        ):
            atomic_write_json(self.head_path, {
                "schema_version": 1, "sequence": tail["sequence"],
                "fingerprint": tail["fingerprint"],
            })
            return
        raise CorruptEvidenceError("ledger tail contradicts its durable head anchor")

    def _read_for_append(self) -> list[dict[str, Any]]:
        signature = self._signature()
        if signature == self._append_cache_signature and self._append_cache_events is not None:
            return list(self._append_cache_events)
        events = self._decode(self._read_bytes())
        integrity = self._validate(events)
        self._verify_head(events, repair_one_ahead=True)
        self._append_cache_signature = signature
        self._append_cache_events = list(events)
        self._append_cache_integrity = integrity
        return events

    def _validate_append(self, existing: list[dict[str, Any]], event: dict[str, Any]) -> None:
        transaction_id = event["transaction_id"]
        history = [item["event_type"] for item in existing if item["transaction_id"] == transaction_id]
        if event["event_id"] in {item["event_id"] for item in existing}:
            raise CorruptEvidenceError("duplicate ledger event ID")
        if event["event_type"] == "TransactionStarted":
            if history:
                raise CorruptEvidenceError("duplicate transaction start")
            return
        if not history:
            raise CorruptEvidenceError("event precedes transaction start")
        terminals = [name for name in history if name in TERMINAL_EVENT_TYPES]
        if event["event_type"] == "LeaseAcquired" and "LeaseAcquired" in history:
            if "RecoveryApplied" not in history:
                raise CorruptEvidenceError("reacquired lease requires prior recovery authorization")
        if event["event_type"] in TERMINAL_EVENT_TYPES and terminals:
            raise CorruptEvidenceError("transaction has conflicting terminal events")
        if terminals and event["event_type"] not in {
            "LeaseReleased", "RecoveryApplied", "ProjectionUpdated", "LegacyEvidenceImported",
            "CheckpointRecorded",
        }:
            raise CorruptEvidenceError("terminal transaction is immutable")
        if (
            terminals
            and event["event_type"] == "CheckpointRecorded"
            and event["payload"].get("checkpoint")
            != "compatibility_materialization_acknowledged"
        ):
            raise CorruptEvidenceError("terminal transaction has an invalid post-terminal checkpoint")
        required_predecessors = {
            "SnapshotCaptured": "LeaseAcquired",
            "ValidationStarted": "SnapshotCaptured",
            "SessionResultAccepted": "SessionLaunched",
            "ValidationPassed": "ValidationStarted",
            "CommitFinalized": "ValidationPassed",
            "ProjectionUpdated": next((name for name in terminals), None),
        }
        predecessor = required_predecessors.get(event["event_type"])
        if predecessor is not None and predecessor not in history:
            raise CorruptEvidenceError(
                f"{event['event_type']} requires prior {predecessor}"
            )
        if event["event_type"] == "TransactionCompleted":
            required = {"LeaseAcquired", "SnapshotCaptured", "ValidationStarted", "ValidationPassed"}
            missing = sorted(required - set(history))
            if missing and not bool(event["payload"].get("legacy_import")):
                raise CorruptEvidenceError(
                    "completed transaction skips canonical lifecycle events: " + ", ".join(missing)
                )

    def _validate_storage_path(self) -> None:
        current = self.path
        while True:
            if current.exists() and current.is_symlink() and current not in {Path("/var"), Path("/tmp")}:
                raise CorruptEvidenceError(f"evidence ledger path traverses a symbolic link: {current}")
            if current == current.parent:
                break
            current = current.parent
        if self.path.exists() and not self.path.is_file():
            raise CorruptEvidenceError("evidence ledger path is not a regular file")
        for auxiliary in (self.lock_path, self.head_path):
            if auxiliary.exists() and (auxiliary.is_symlink() or not auxiliary.is_file()):
                raise CorruptEvidenceError(f"evidence ledger auxiliary path is unsafe: {auxiliary.name}")

    @contextmanager
    def synchronized(self) -> Iterator[None]:
        self._validate_storage_path()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = self._open_regular(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_bytes(self) -> bytes:
        self._validate_storage_path()
        try:
            if not self.path.exists():
                return b""
            descriptor = self._open_regular(self.path, os.O_RDONLY)
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CorruptEvidenceError(f"cannot read evidence ledger: {exc}") from exc

    def _decode(self, payload: bytes) -> list[dict[str, Any]]:
        if not payload:
            return []
        if not payload.endswith(b"\n"):
            raise CorruptEvidenceError("evidence ledger is truncated: final record lacks newline")
        events: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.splitlines(), start=1):
            if not raw:
                raise CorruptEvidenceError(f"evidence ledger has an empty record at line {index}")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CorruptEvidenceError(f"malformed evidence ledger record at line {index}") from exc
            if not isinstance(event, dict):
                raise CorruptEvidenceError(f"evidence ledger record {index} is not an object")
            if canonical_bytes(event) != raw:
                raise CorruptEvidenceError(f"evidence ledger record {index} is not canonically serialized")
            events.append(event)
        return events

    def read(self) -> list[dict[str, Any]]:
        # A process may die after the append itself is fsynced but before the
        # durable head anchor is replaced. Repair only that exact one-record
        # continuation while holding the same lock used by append; every other
        # divergence remains corrupt evidence.
        with self.synchronized():
            events = self._decode(self._read_bytes())
            self._validate(events)
            self._verify_head(events, repair_one_ahead=True)
            return events

    def _validate(self, events: list[dict[str, Any]]) -> LedgerIntegrity:
        previous = GENESIS_FINGERPRINT
        event_ids: set[str] = set()
        terminals: dict[str, str] = {}
        started: set[str] = set()
        workflows: dict[str, str] = {}
        histories: dict[str, list[str]] = {}
        raised_gate_ids: dict[str, tuple[str, str]] = {}
        resolution_bindings: dict[str, tuple[str, str]] = {}
        resolved_gate_ids: set[str] = set()
        for expected, event in enumerate(events, start=1):
            required = {
                "schema_version", "sequence", "event_id", "event_type", "timestamp", "project_id",
                "repository_identity", "repository_path_fingerprint", "transaction_id", "workflow_type",
                "previous_fingerprint", "fingerprint", "payload",
            }
            missing = sorted(required - set(event))
            if missing:
                raise CorruptEvidenceError(f"ledger sequence {expected} is missing {', '.join(missing)}")
            extra = sorted(set(event) - required)
            if extra:
                raise CorruptEvidenceError(f"ledger sequence {expected} has unexpected fields: {', '.join(extra)}")
            typed_strings = (
                "event_id", "event_type", "timestamp", "project_id", "repository_identity",
                "repository_path_fingerprint", "transaction_id", "workflow_type",
                "previous_fingerprint", "fingerprint",
            )
            if type(event["schema_version"]) is not int or type(event["sequence"]) is not int:
                raise CorruptEvidenceError(f"ledger sequence {expected} has invalid integer fields")
            if any(not isinstance(event[name], str) or not event[name] for name in typed_strings):
                raise CorruptEvidenceError(f"ledger sequence {expected} has invalid string fields")
            if not isinstance(event["payload"], dict):
                raise CorruptEvidenceError(f"ledger sequence {expected} payload is not an object")
            for name, expected_type in PAYLOAD_CONTRACTS.get(event["event_type"], {}).items():
                if name in event["payload"] and not isinstance(event["payload"][name], expected_type):
                    raise CorruptEvidenceError(
                        f"ledger sequence {expected} has invalid {event['event_type']} payload field {name}"
                    )
            if any(len(event[name]) < 64 for name in ("repository_identity", "repository_path_fingerprint")):
                raise CorruptEvidenceError(f"ledger sequence {expected} has short repository identity fields")
            hex_fields = ("previous_fingerprint", "fingerprint")
            if any(len(event[name]) != 64 or any(char not in "0123456789abcdef" for char in event[name]) for name in hex_fields):
                raise CorruptEvidenceError(f"ledger sequence {expected} has invalid fingerprint fields")
            if event["schema_version"] != 1 or event["sequence"] != expected:
                raise CorruptEvidenceError(f"ledger sequence mismatch at record {expected}")
            if event["event_id"] in event_ids:
                raise CorruptEvidenceError(f"duplicate ledger event ID at sequence {expected}")
            event_ids.add(event["event_id"])
            if event["event_type"] not in EVENT_TYPES:
                raise CorruptEvidenceError(f"unsupported ledger event type at sequence {expected}")
            if event["project_id"] != self.project_id:
                raise CorruptEvidenceError(f"project identity mismatch at sequence {expected}")
            if event["repository_identity"] != self.repository_identity:
                raise CorruptEvidenceError(f"repository identity mismatch at sequence {expected}")
            if event["repository_path_fingerprint"] != self.repository_path_fingerprint:
                raise CorruptEvidenceError(f"repository path fingerprint mismatch at sequence {expected}")
            try:
                WorkflowType(event["workflow_type"])
            except (TypeError, ValueError) as exc:
                raise CorruptEvidenceError(f"invalid workflow type at sequence {expected}") from exc
            transaction_id = event["transaction_id"]
            if not isinstance(transaction_id, str) or not transaction_id:
                raise CorruptEvidenceError(f"invalid transaction identity at sequence {expected}")
            known = workflows.get(transaction_id)
            if known is not None and known != event["workflow_type"]:
                raise CorruptEvidenceError(f"transaction workflow changed at sequence {expected}")
            workflows[transaction_id] = event["workflow_type"]
            history = histories.setdefault(transaction_id, [])
            if event["event_type"] == "TransactionStarted":
                if transaction_id in started:
                    raise CorruptEvidenceError(f"duplicate transaction start at sequence {expected}")
                started.add(transaction_id)
                bound_gate_id = event["payload"].get("resolved_gate_id")
                bound_gate_fingerprint = event["payload"].get("resolved_gate_fingerprint")
                if bound_gate_id is not None or bound_gate_fingerprint is not None:
                    if (
                        event["workflow_type"] != WorkflowType.HUMAN_DECISION_RESOLUTION.value
                        or not isinstance(bound_gate_id, str)
                        or not isinstance(bound_gate_fingerprint, str)
                    ):
                        raise CorruptEvidenceError(
                            f"invalid human-resolution binding at sequence {expected}"
                        )
                    resolution_bindings[transaction_id] = (
                        bound_gate_id, bound_gate_fingerprint
                    )
            elif transaction_id not in started and not bool(event["payload"].get("legacy_import")):
                raise CorruptEvidenceError(f"event precedes transaction start at sequence {expected}")
            if event["event_type"] in TERMINAL_EVENT_TYPES:
                if transaction_id in terminals:
                    raise CorruptEvidenceError(
                        f"transaction {transaction_id} has conflicting terminal events"
                    )
                terminals[transaction_id] = event["event_type"]
            if event["event_type"] == "HumanGateRaised":
                try:
                    gate = validate_human_gate(event["payload"].get("gate"))
                except Exception as exc:
                    raise CorruptEvidenceError(
                        f"human gate violates its durable contract at sequence {expected}"
                    ) from exc
                gate_id = str(gate["gate_id"])
                if gate_id in raised_gate_ids:
                    raise CorruptEvidenceError(f"duplicate human gate identity at sequence {expected}")
                claimed_gate_fingerprint = event["payload"].get("gate_fingerprint")
                actual_gate_fingerprint = fingerprint(gate)
                if (
                    event["payload"].get("gate_id") != gate_id
                    or claimed_gate_fingerprint != actual_gate_fingerprint
                ):
                    raise CorruptEvidenceError(f"human gate identity mismatch at sequence {expected}")
                raised_gate_ids[gate_id] = (transaction_id, actual_gate_fingerprint)
            elif event["event_type"] == "HumanGateResolved":
                gate_id = event["payload"].get("gate_id")
                binding = raised_gate_ids.get(str(gate_id))
                transaction_binding = resolution_bindings.get(transaction_id)
                if (
                    str(gate_id) in resolved_gate_ids
                    or (
                        binding is not None
                        and (
                            event["payload"].get("gate_fingerprint") != binding[1]
                            or event["payload"].get("raised_transaction_id") != binding[0]
                        )
                    )
                    or (
                        binding is None
                        and transaction_binding != (
                            str(gate_id), event["payload"].get("gate_fingerprint")
                        )
                    )
                ):
                    raise CorruptEvidenceError(
                        f"human gate resolution is not exactly bound at sequence {expected}"
                    )
                resolved_gate_ids.add(str(gate_id))
            elif (
                transaction_id in terminals
                and event["event_type"] not in TERMINAL_EVENT_TYPES
                and event["event_type"] not in {
                "LeaseReleased", "RecoveryApplied", "ProjectionUpdated", "LegacyEvidenceImported",
                "CheckpointRecorded",
                }
            ):
                raise CorruptEvidenceError(
                    f"terminal transaction {transaction_id} was mutated at sequence {expected}"
                )
            if (
                transaction_id in terminals
                and event["event_type"] == "CheckpointRecorded"
                and event["payload"].get("checkpoint")
                != "compatibility_materialization_acknowledged"
            ):
                raise CorruptEvidenceError(
                    f"terminal transaction {transaction_id} has an invalid post-terminal checkpoint"
                )
            if event["previous_fingerprint"] != previous:
                raise CorruptEvidenceError(f"broken ledger fingerprint chain at sequence {expected}")
            claimed = event["fingerprint"]
            unsigned = dict(event)
            unsigned.pop("fingerprint", None)
            actual = event_fingerprint(unsigned)
            if claimed != actual:
                raise CorruptEvidenceError(f"ledger fingerprint mismatch at sequence {expected}")
            previous = claimed
            history.append(event["event_type"])
        for transaction_id, history in histories.items():
            if not history or history[0] != "TransactionStarted":
                raise CorruptEvidenceError(f"transaction {transaction_id} does not begin with TransactionStarted")
            positions: dict[str, int] = {}
            for index, name in enumerate(history):
                positions.setdefault(name, index)
            if history.count("LeaseAcquired") > 1:
                second_lease = [index for index, name in enumerate(history) if name == "LeaseAcquired"][1]
                recovery_positions = [index for index, name in enumerate(history) if name == "RecoveryApplied"]
                if not recovery_positions or recovery_positions[-1] > second_lease:
                    raise CorruptEvidenceError(
                        f"transaction {transaction_id} reacquired a lease without prior recovery authorization"
                    )
            ordered_pairs = (
                ("TransactionStarted", "LeaseAcquired"),
                ("LeaseAcquired", "SnapshotCaptured"),
                ("SnapshotCaptured", "SessionLaunched"),
                ("SessionLaunched", "SessionResultAccepted"),
                ("ValidationStarted", "ValidationPassed"),
                ("ValidationPassed", "CommitFinalized"),
                ("CommitFinalized", "TransactionCompleted"),
                ("TransactionCompleted", "LeaseReleased"),
                ("LeaseReleased", "ProjectionUpdated"),
            )
            for earlier, later in ordered_pairs:
                if earlier in positions and later in positions and positions[earlier] > positions[later]:
                    raise CorruptEvidenceError(
                        f"transaction {transaction_id} has invalid event order: {earlier} after {later}"
                    )
            if "SessionResultAccepted" in positions and "SessionLaunched" not in positions:
                raise CorruptEvidenceError(f"transaction {transaction_id} accepted a result without a session launch")
            if "ValidationPassed" in positions and "ValidationStarted" not in positions:
                raise CorruptEvidenceError(f"transaction {transaction_id} passed validation without starting validation")
            if "CommitFinalized" in positions and "ValidationPassed" not in positions:
                raise CorruptEvidenceError(f"transaction {transaction_id} finalized a commit without validation")
            if "ProjectionUpdated" in positions and not any(name in positions for name in TERMINAL_EVENT_TYPES):
                raise CorruptEvidenceError(f"transaction {transaction_id} projected state before a terminal outcome")
            if "TransactionCompleted" in positions:
                completed = next(
                    event for event in events
                    if event["transaction_id"] == transaction_id
                    and event["event_type"] == "TransactionCompleted"
                )
                required = {"LeaseAcquired", "SnapshotCaptured", "ValidationStarted", "ValidationPassed"}
                missing = sorted(required - set(positions))
                if missing and not bool(completed["payload"].get("legacy_import")):
                    raise CorruptEvidenceError(
                        f"transaction {transaction_id} skips canonical lifecycle events: "
                        + ", ".join(missing)
                    )
        return LedgerIntegrity(
            valid=True,
            sequence=len(events),
            fingerprint=previous,
            event_count=len(events),
            transactions=len(started),
        )

    def verify(self) -> LedgerIntegrity:
        with self.synchronized():
            events = self._decode(self._read_bytes())
            integrity = self._validate(events)
            self._verify_head(events, repair_one_ahead=True)
            return integrity

    def append(
        self,
        *,
        event_type: str,
        transaction_id: str,
        workflow_type: WorkflowType | str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise EvidenceError(f"unsupported evidence event type: {event_type}")
        workflow = WorkflowType(workflow_type)
        with self.synchronized():
            signature_before = self._signature()
            cache_hit = (
                signature_before == self._append_cache_signature
                and self._append_cache_events is not None
                and self._append_cache_integrity is not None
            )
            existing = self._read_for_append()
            integrity = self._append_cache_integrity if cache_hit else self._validate(existing)
            value = redact_value(payload or {})
            if not isinstance(value, dict):
                raise EvidenceError("ledger event payload must be an object")
            event = {
                "schema_version": 1,
                "sequence": integrity.sequence + 1,
                "event_id": event_id or str(uuid.uuid4()),
                "event_type": event_type,
                "timestamp": timestamp or utc_now(),
                "project_id": self.project_id,
                "repository_identity": self.repository_identity,
                "repository_path_fingerprint": self.repository_path_fingerprint,
                "transaction_id": transaction_id,
                "workflow_type": workflow.value,
                "previous_fingerprint": integrity.fingerprint,
                "payload": value,
            }
            event["fingerprint"] = event_fingerprint(event)
            self._validate_append(existing, event)
            candidate = [*existing, event]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = canonical_bytes(event) + b"\n"
            descriptor = self._open_regular(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise EvidenceError("short evidence-ledger append")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            atomic_write_json(self.head_path, {
                "schema_version": 1, "sequence": event["sequence"],
                "fingerprint": event["fingerprint"],
            })
            self._append_cache_signature = self._signature()
            self._append_cache_events = candidate
            self._append_cache_integrity = LedgerIntegrity(
                valid=True,
                sequence=event["sequence"],
                fingerprint=event["fingerprint"],
                event_count=len(candidate),
                transactions=integrity.transactions + (1 if event_type == "TransactionStarted" else 0),
            )
            return event

    def terminal_event(self, transaction_id: str) -> dict[str, Any] | None:
        terminal = [
            event for event in self.read()
            if event["transaction_id"] == transaction_id and event["event_type"] in TERMINAL_EVENT_TYPES
        ]
        if len(terminal) > 1:
            raise CorruptEvidenceError("transaction has multiple terminal events")
        return terminal[0] if terminal else None

    def event_by_source_fingerprint(self, source_fingerprint: str) -> dict[str, Any] | None:
        matches = [
            event for event in self.read()
            if event["event_type"] == "LegacyEvidenceImported"
            and event["payload"].get("source_fingerprint") == source_fingerprint
        ]
        if len(matches) > 1:
            raise CorruptEvidenceError("legacy evidence source was imported more than once")
        return matches[0] if matches else None
