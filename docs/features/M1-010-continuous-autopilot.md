# M1-010 — Durable continuous feature-delivery Autopilot

## Goal

Provide one controller-owned loop that repeatedly evaluates the authoritative
ledger projection and invokes the existing queue, feature, acceptance,
integration, validation, and recovery routes until a durable stop, completion,
or genuine gate.

## Acceptance criteria

- `scripts/conveyor autopilot --project PROJECT (--dry-run | --apply)` requires
  exactly one mode; `stop-autopilot` writes a durable stop request and
  `autopilot-status` reads ownership, stop, and report state.
- Dry-run is byte-for-byte non-mutating, launches no model or child, and exposes
  only the first selected feature, exact next route, loop/retry/stop policy,
  deterministic recovery registry, and first-cycle estimate.
- Apply holds one process-start-authenticated project ownership record, rejects
  a live duplicate, authenticates dead ownership before recovery, emits concise
  lifecycle events, and releases ownership on success, stop, and failure.
- Every transition reuses `CycleEngine` and its registered deterministic
  recovery routes. Autopilot does not implement application feature,
  integration, validation, commit, ledger, projection, or cache logic.
- Durable stop requests are checked before transactions, models, application
  mutation, commits, integration, between validation tiers, and before the
  next transition. An atomic route is allowed to reach a coherent checkpoint;
  no process is killed during Git or evidence persistence.
- Retry budgets are conservative and configurable; identical evidence cannot
  repeat indefinitely; terminal evidence never silently marks a feature
  complete.
- Initial execution permits one project loop, one application mutation
  transaction, one fresh parent session per model-backed feature route, and
  zero children unless an authoritative future profile changes the budget.
- Reports include model/reasoning, parent/child counts, elapsed time, context
  estimate, validation commands, retries, terminal classification, and
  integration outcome per feature.

## Boundaries

- No default-branch merge, push, tag, publish, deploy, release, or notarization.
- No controller implementation of application production features.
- Crash recovery never treats elapsed time or PID death alone as sufficient
  ownership evidence.
- A genuine human gate remains terminal for the loop.

## Validation

Use `tests/test_autopilot.py` plus focused existing cycle-engine, recovery,
integration-finalization, status, and consistency regressions. Run Python
compilation, configuration validation, CLI help/mode checks,
`git diff --check`, and only the authorized Interview Companion Autopilot
dry-run. Do not run the complete controller suite or application tests.
