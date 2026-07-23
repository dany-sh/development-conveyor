# M1-003 — Bounded feature scoping command

## Objective

Author and validate an explicitly requested set of feature specifications from
one Markdown product brief without combining planning with ordinary queue
reconciliation, feature preparation, or feature execution.

## Acceptance criteria

1. `scripts/conveyor scope-features` requires an explicit project, existing
   feature IDs, optional new feature IDs, exactly one ready feature, a Markdown
   brief, and exactly one of `--dry-run` or `--apply`.
2. Dry-run writes no repository, controller state, ledger, cache, branch,
   report, or lock and exposes the exact targets, authorized paths, dependency
   changes, validators, model/reasoning pair, one-parent/zero-child budget, and
   planning-only stop.
3. Apply requires a clean repository and absent writer lease, then binds the
   starting branch, HEAD, queue, brief, diff, target IDs, paths, policies, and
   dependencies to one typed planning transaction.
4. The single direct planning session uses `gpt-5.6-sol` with medium reasoning,
   one parent, zero children, and mechanically disabled collaboration tools.
5. Deterministic validation preserves existing IDs and dependencies, creates
   each requested new ID exactly once, rejects conflicts and cycles, permits
   only requested feature specifications and inventory/status documents, and
   leaves exactly the user-selected feature ready.
6. Successful apply creates one planning-only commit, records ledger and
   projection evidence, atomically binds the compatibility cache, leaves the
   repository clean, and stops at `feature_ready` before feature execution.
7. A retained valid scoping diff is recoverable through an exact zero-model
   `recover-scope-features` transaction using the existing planning
   finalization authority.
8. Focused synthetic tests cover mutation bounds, stable IDs, new-feature
   conflicts, dependencies, execution profiles, parent/child budgets,
   commit/stop behavior, dirty-state rejection, and exact recovery.

## Non-goals

- Running a real scoping apply against a registered application during
  controller development.
- Reconciling unrelated queue state.
- Launching Feature Factory, Milestone Integrator, named agents, or child
  sessions.
- Editing application production source, tests, runtime state, or historical
  evidence.
