# M1-002 — Cost-aware execution policy

## Objective

Plan workflows before mutable work so the controller uses deterministic routes, focused context and verification, and reusable evidence whenever those provide the required confidence.

## Acceptance criteria

1. Dry-run plans expose model routing, session budgets, context estimates, selected verification, skipped work, invalidation rules, and stopping criteria.
2. Deterministic status, consistency, queue parsing, dry-run planning, and complete deterministic integration select zero model sessions.
3. Model/reasoning selection is independently routed; Sol xhigh requires both a failed focused attempt and remaining high-risk ambiguity.
4. Child-session budgets fail closed before a launcher callback, and controller maintenance defaults to zero children.
5. Verification is changed-path and risk based; documentation and configuration changes do not trigger application builds or unrelated suites.
6. Evidence reuse requires a complete trusted identity match and rejects dirty or incomplete evidence.
7. Large command output is retained in a report file and represented in model context only by a summary.

## Scope

The policy is controller-only. It does not relax ledger, transaction, lease, Git, recovery, dry-run, or application-isolation requirements.
