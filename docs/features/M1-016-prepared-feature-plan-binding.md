# M1-016 — Prepared-Feature Execution-Plan Binding

## Status

Review on `codex/m1-16-prepared-feature-plan-binding` from exact parent
`b19549cff80f416f4698db97d3ea33feb3820101`.

## Failure trace

`CycleEngine._validate_projected_dispatch` raised
`ProjectionError: repository no longer matches the execution plan starting
branch and commit` before feature context construction. The validator used
`execution_plan.milestone_branch` as the expected live checkout and
`execution_plan.starting_commit` as the expected commit. It observed
`RepositoryInspector.current_branch` and `RepositoryInspector.head`.

The commit was correct. The false expected branch was the milestone branch,
derived because the execution plan had no explicit workflow-scoped
`starting_branch`. The stale cycle-cache
`last_verified_git_state.branch` corroborated the defect but did not raise the
exception; compatibility state did not own the rejection.

## Authority contract

A sole ready feature with one completed `FEATURE_PREPARED` deterministic
transaction is bound to that transaction only when its starting and terminal
snapshots match the current queue fingerprint, repository identity, path
fingerprint, configured milestone branch, canonical feature branch, and exact
feature starting commit.

The typed execution plan now carries a distinct `starting_branch`:

- ordinary fresh feature preparation starts on the milestone branch;
- authenticated prepared feature execution starts on the feature branch;
- the starting commit remains the authenticated feature starting commit;
- milestone integration continues to start on the milestone branch; and
- equal ref targets never make branch identities interchangeable.

The live checkout must match the planned starting branch and commit exactly.
Dirty worktrees, unfinished Git operations, active transactions, and writer
leases fail before feature context construction or model launch.

`feature_ready` is normalized to `feature_preparing` only when the live clean
checkout and exact feature ref also match the authenticated preparation
terminal snapshot and no writer lease exists. Even when live evidence drifts,
the authenticated preparation still fixes the required execution branch so a
milestone checkout cannot trigger duplicate preparation.

## Recovery cache finalization

Successful prelaunch recovery rewrites the canonical application cycle cache
with the preserved feature branch, feature starting commit, milestone
destination, feature worktree, and a fresh `last_verified_git_state` captured
from the actual live feature checkout. It performs no branch switch and does
not edit application content.

## Validation boundary

Focused execution-plan, prepared-feature, prelaunch-recovery, feature-cycle,
Autopilot, projection, consistency/cache, retained-result, post-integration
planning, and integration-routing regressions; Python compilation;
configuration and queue validation; CLI help and exact-mode rejection; the
read-only live consistency and status inspections; content audit; and
`git diff --check`.

The complete controller suite, application tests/builds, recovery apply,
ordinary resume, Autopilot restart, application mutation, push, merge, tag,
publication, deployment, and release remain excluded.
