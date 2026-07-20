# ADR 0001 — Append-only phase evidence and rebuildable projections

Status: accepted for M1 implementation

## Context

Mutable project, cycle, session, runtime, lease, queue, and Git evidence were interpreted independently by several workflow paths. A later dirty worktree or stale cache could incorrectly change a historical conclusion, while an interrupted commit could be duplicated or lost.

## Decision

Every writable phase receives one transaction and one exact typed writer lease. The controller appends canonical, hash-chained evidence for lifecycle boundaries and derives current state by replay. Projection JSON and legacy project/cycle state remain compatibility caches only. Corrupt ledger or identity evidence stops; only stale cache is rebuilt automatically.

Phase handlers return typed evidence. They do not own terminal success, final commit identity, lease release, or projection. Recovery appends new evidence and never rewrites history.

## Consequences

- Historical terminal snapshots remain independent of unrelated later worktree state.
- Interruption recovery can distinguish uncommitted work, a commit lacking evidence, a released lease lacking projection, and corrupt evidence.
- Compatibility paths must be migrated at whole-phase boundaries; wrapping individual cache writes is not sufficient.
- The ledger is controller state and requires backup/retention appropriate to its authority.
