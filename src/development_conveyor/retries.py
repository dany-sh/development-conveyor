"""Focused retry budgets that reject repeated hypotheses."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConveyorError, RetryExhausted


@dataclass
class RetryBudget:
    limit: int
    attempts: list[dict[str, str]] = field(default_factory=list)

    def record(self, *, hypothesis: str, evidence: str) -> int:
        normalized = " ".join(hypothesis.lower().split())
        if any(item["normalized_hypothesis"] == normalized for item in self.attempts):
            raise ConveyorError("refusing to repeat the same failed repair hypothesis")
        if len(self.attempts) >= self.limit:
            raise RetryExhausted(f"focused retry limit exhausted after {self.limit} attempts")
        self.attempts.append({"hypothesis": hypothesis, "normalized_hypothesis": normalized, "evidence": evidence})
        return len(self.attempts)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - len(self.attempts))

