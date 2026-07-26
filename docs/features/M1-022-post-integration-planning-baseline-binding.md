# M1-022 — Post-integration planning baseline binding

## Status

Review.

## Problem

F073 completed milestone integration at terminal HEAD
`12bee2fc3b3668091474dc288067cd3aced0ee08`, and the kernel correctly cleared
the selected feature, accepted commit, active transaction, and human gate
before returning to `queue_reconciliation`. The projection intentionally
preserved `selected_feature_starting_commit`
`8a2f7fb93bb7ec6f0eaf160e971aac517542802f` as historical planning evidence.

Execution identity gave that historical value precedence over the completed
integration terminal snapshot. Status therefore planned fresh reconciliation
from the previous F073 planning baseline even though the live milestone ref
and clean repository HEAD both resolved to `12bee2fc`. Consistency failed only
`execution_plan_projection_agreement` with `planned_milestone_ref: false`.

## Binding contract

Fresh post-integration queue reconciliation uses the integration terminal HEAD
only when the projection proves all of the following:

- current state and allowed action are both `queue_reconciliation`;
- selected and current feature, accepted commit, active transaction, human
  gate, required lease, and resume eligibility are cleared;
- the latest sequenced historical integration outcome is `INTEGRATED` and
  records a clean terminal repository;
- the matching milestone-integration transaction is completed, names the same
  feature and terminal reference, and contains a clean, operation-free terminal
  snapshot on the configured milestone branch.

The configured milestone branch remains an authenticated project input when
the projection does not duplicate it. The preserved
`selected_feature_starting_commit` remains visible as historical projection
evidence but cannot route this fresh transaction.

If any completion evidence is missing or malformed, the new binding is not
used. Live consistency continues to reject a missing or mismatched milestone
ref, a dirty repository, an unfinished Git operation, an active writer lease,
an unauthorized descendant after the terminal HEAD, or broken lineage. An
active selected feature still uses its exact authenticated feature starting
commit.

## Acceptance evidence

- Interview Companion status reports `queue_reconciliation`, no selected
  feature or accepted commit, milestone and starting branch
  `codex/m0-foundation`, and starting commit `12bee2fc`.
- `verify-consistency --json` reports `CONSISTENT`, `consistent: true`, no
  failed invariant, and `planned_milestone_ref: true`.
- Focused execution-plan, planner-observer, projection, M1-014, M1-021, and
  post-integration regression tests pass.
- Read-only live checks preserve the evidence ledger, projection cache, absent
  cycle cache, both application repositories, Git-operation state, and writer
  lease state while launching zero models and zero children.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
