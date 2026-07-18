"""Focused retry budgets that reject repeated hypotheses."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .errors import ConveyorError, RetryExhausted


@dataclass
class RetryBudget:
    limit: int
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _normalize(value: Any) -> str:
        text = " ".join(str(value or "").lower().split())
        text = re.sub(r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})", "<timestamp>", text)
        text = re.sub(r"-repair-\d+\.json", "-repair-<n>.json", text)
        return text

    def record(
        self,
        *,
        hypothesis: str,
        evidence: str,
        failure_classification: str = "unknown",
        command: tuple[str, ...] = (),
        session_id: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        remediation_action: str | None = None,
    ) -> int:
        if not hypothesis.strip() or not evidence.strip() or not (remediation_action or "").strip():
            raise ConveyorError("retry requires a hypothesis, supporting evidence, and materially different remediation action")
        normalized = self._normalize(hypothesis)
        signature = {
            "failure_classification": self._normalize(failure_classification),
            "command": self._normalize(" ".join(command)),
            "session_id": self._normalize(session_id),
            "model": self._normalize(model),
            "reasoning": self._normalize(reasoning),
            "remediation_action": self._normalize(remediation_action),
        }
        if any(
            item["normalized_hypothesis"] == normalized
            or item.get("normalized_signature") == signature
            for item in self.attempts
        ):
            raise ConveyorError("refusing to repeat the same failed repair hypothesis or retry signature")
        if len(self.attempts) >= self.limit:
            raise RetryExhausted(f"focused retry limit exhausted after {self.limit} attempts")
        self.attempts.append({
            "hypothesis": hypothesis,
            "normalized_hypothesis": normalized,
            "normalized_signature": signature,
            "evidence": evidence,
            "remediation_action": remediation_action or "",
        })
        return len(self.attempts)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - len(self.attempts))
