# M1-027: Native Conveyor backlog control plane

## Goal

Expose safe, deterministic backlog metadata operations and typed queue data to
the native Factory Desktop operator console while keeping each application
repository's `FEATURE_QUEUE.yaml` as the sole backlog authority.

## Scope

- Support P1, P2, and P3 priority labels; absent priorities present as P2
  without an automatic rewrite.
- Select only ready features by priority, explicit queue order, and feature ID.
- Add queue-only `ready` and `backlog` transitions and allow priority plus
  before/after ordering in one `prioritize` operation.
- Return stable, UI-oriented queue JSON without creating persisted Kanban
  states, local caches, or a controller service.

## Guardrails

- Metadata commands require a valid queue, configured milestone branch, clean
  repository except for the queue file, no active writer lease, and no active
  transaction where a transition could race execution.
- They modify only `FEATURE_QUEUE.yaml`, release their temporary lease, launch
  zero models and zero children, and never begin a feature cycle.
- Derived Kanban columns are presentation data only. Dependencies, blockers,
  pause behavior, execution preflight, validation, and destructive Git
  protections remain controller-owned.

## Validation

- Disposable focused queue-control tests cover priority ordering, default P2,
  transitions, queue-only mutations, structured queue JSON, and zero-session
  metadata operations.
- Registered application projects are inspected only with read-only `queue`
  commands.
