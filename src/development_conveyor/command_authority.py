"""Global command classification and transaction-authority contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .contracts import AUTHORITATIVE_COMMAND_CATEGORIES, CommandCategory
from .errors import TransactionError
from .redaction import redact_text


@dataclass(frozen=True)
class CommandRecord:
    command: tuple[str, ...]
    classification: CommandCategory
    configured_source: str | None
    exit_status: int
    effect_on_transaction: str
    diagnostic: str | None = None

    @property
    def authoritative(self) -> bool:
        return self.classification in AUTHORITATIVE_COMMAND_CATEGORIES

    @property
    def passed(self) -> bool:
        return self.exit_status == 0

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["command"] = list(self.command)
        value["classification"] = self.classification.value
        return value


class CommandAuthority:
    def __init__(
        self,
        *,
        configured_required: Iterable[Iterable[str]] = (),
        kernel_required: Iterable[Iterable[str]] = (),
        workflow_evidence: Iterable[Iterable[str]] = (),
        optional_diagnostics: Iterable[Iterable[str]] = (),
        status_observations: Iterable[Iterable[str]] = (),
    ):
        self.configured_required = {tuple(item) for item in configured_required}
        self.kernel_required = {tuple(item) for item in kernel_required}
        self.workflow_evidence = {tuple(item) for item in workflow_evidence}
        self.optional_diagnostics = {tuple(item) for item in optional_diagnostics}
        self.status_observations = {tuple(item) for item in status_observations}

    def classify(
        self,
        command: Iterable[str],
        exit_status: int,
        *,
        configured_source: str | None = None,
        diagnostic: str | None = None,
    ) -> CommandRecord:
        argv = tuple(command)
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise TransactionError("command authority requires a non-empty argument array")
        if argv in self.configured_required:
            category = CommandCategory.CONFIGURED_REQUIRED_VALIDATION
            effect = "passed" if exit_status == 0 else "transaction_failure"
        elif argv in self.kernel_required:
            category = CommandCategory.KERNEL_REQUIRED_FINALIZATION
            effect = "passed" if exit_status == 0 else "transaction_failure"
        elif argv in self.workflow_evidence:
            category = CommandCategory.WORKFLOW_REQUIRED_EVIDENCE
            effect = "passed" if exit_status == 0 else "transaction_failure"
        elif argv in self.optional_diagnostics:
            category = CommandCategory.OPTIONAL_DIAGNOSTIC
            effect = "warning_only"
        elif argv in self.status_observations:
            category = CommandCategory.STATUS_OBSERVATION
            effect = "observation_only"
        else:
            category = CommandCategory.UNSUPPORTED_COMMAND
            effect = "not_authoritative"
            diagnostic = diagnostic or "unsupported command cannot determine transaction success"
        return CommandRecord(
            command=argv,
            classification=category,
            configured_source=configured_source,
            exit_status=exit_status,
            effect_on_transaction=effect,
            diagnostic=redact_text(diagnostic) if diagnostic is not None else None,
        )

    @staticmethod
    def validate(records: Iterable[CommandRecord]) -> tuple[list[CommandRecord], list[CommandRecord]]:
        records = list(records)
        failures = [item for item in records if item.authoritative and not item.passed]
        warnings = [
            item for item in records
            if item.classification in {CommandCategory.OPTIONAL_DIAGNOSTIC, CommandCategory.UNSUPPORTED_COMMAND}
            and not item.passed
        ]
        if failures:
            commands = [" ".join(item.command) for item in failures]
            raise TransactionError(f"authoritative command failed: {', '.join(commands)}")
        return records, warnings
