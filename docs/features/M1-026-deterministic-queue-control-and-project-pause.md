# M1-026: Deterministic queue control and project pause

## Objective

Let an operator inspect, reorder, select, pause, and resume an application
feature queue without invoking a planning model for ordinary validated queue
selection.

## Authority

- `FEATURE_QUEUE.yaml` remains the sole backlog and ordering authority.
- Git, the existing current transaction, and the repository writer lease
  remain mutation authorities.
- The existing per-project controller state stores one `operator_paused`
  boolean. It is not a workflow state, cache, gate, or queue authority.
- Status, queue output, reports, projections, and checkpoints remain
  diagnostic.

## Behavior

`queue --project PROJECT` reports active-milestone entries in file order with
identity, status, dependencies, dependency completion, readiness, blocking
reason, selected/current state, execution profile, and priority position. It
also reports pause state, active feature, next ready feature, and the
deterministic selection reason. The command is read-only and launches zero
models and children.

Routine `run` and `resume` select the next eligible feature from the active
milestone using queue order, feature status, dependency completion, blocked
state, and feature ID as the stable final tie-breaker. They stop at
`feature_ready` or `no_ready_work` without a model-backed planning transaction.
Exceptional product-plan reconciliation remains an explicit workflow.

`run --feature FEATURE` selects only the requested active-milestone feature
after proving it is incomplete, dependency-complete, unblocked, transaction
safe, unpaused, and backed by a valid execution profile whose exact model and
capability preflight passes. Failure occurs before transaction, application
lease, branch, or application mutation. Explicit selection does not reorder the
queue.

`prioritize` moves one active-milestone entry before or after another. It
atomically writes only `FEATURE_QUEUE.yaml`, preserves every feature field,
launches no model, and starts no feature. Unknown, duplicate, self-relative,
cross-milestone, and invalid queue requests fail closed.

`pause` sets the operator flag. Idle projects start no new cycle while paused;
an active transaction reaches its safe cycle boundary before another cycle may
start. Active Git operations are never interrupted. `unpause` clears only the
flag and starts no cycle. Paused `run` and `resume` return
`project_paused` without mutation.

## Acceptance criteria

- Queue inspection is deterministic, ordered, read-only, and zero-model.
- Ordinary next-feature selection launches zero models and returns exact
  no-ready reasons when no feature is eligible.
- Explicit feature selection respects milestone, completion, dependencies,
  blocked state, transaction safety, pause, execution-profile, exact-model, and
  capability requirements before mutation.
- Reprioritization changes only queue order in `FEATURE_QUEUE.yaml`.
- Pause prevents new cycles without interrupting active Git work, and unpause
  starts no work.
- Status remains read-only and no new gate, workflow state, cache, database,
  daemon, event protocol, or recovery command is introduced.
- Focused queue, pause, CLI, M1-024, and M1-025 regressions pass in disposable
  repositories.
- Live queue inspection leaves both application repositories and protected
  controller state unchanged.
