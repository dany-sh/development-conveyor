# M1-021 — Committed planning-result recovery finalization

## Status

Review.

## Problem

An Interview Companion queue-reconciliation transaction created the valid
planning commit `8a2f7fb93bb7ec6f0eaf160e971aac517542802f` and then blocked after
`CommitFinalized`. Structured validation included descriptive
`warnings_scope`; deterministic validation did not. Treating that metadata as
semantic disagreement left the ledger and projection at failure even though
both sides had the same 18 explicit warnings, count, empty blocking set,
validity, milestone, inventory, and sole ready feature F073.

Recovery then treated the existing Git commit as already finalized without
proving a matching recovered ledger/projection result. Consistency also
misclassified the valid direct-child planning commit as evidence that the
already completed F072 integration was interrupted.

## Warning evidence contract

Ordinary reconciliation and planning recovery use the same canonical warning
normalization. `warnings_scope` is descriptive and may be omitted by either
side. Semantic agreement requires matching:

- canonical explicit warning lists and normalized counts;
- blocking warnings and validity;
- active milestone and ready features; and
- feature and milestone inventory counts.

Every mismatch remains fail closed. Legacy prose-summary compatibility remains
restricted to its existing exact historical identity.

## Committed recovery contract

The exact committed-then-blocked topology contains:

```text
TransactionStarted
LeaseAcquired
SnapshotCaptured
CheckpointRecorded
SessionLaunched
CheckpointRecorded
SessionResultAccepted
ChangesDetected
ValidationStarted
ValidationPassed
CommitFinalized
TransactionBlocked
LeaseReleased
ProjectionUpdated
```

Recovery authenticates the original run, session, transaction, branch,
starting parent, existing direct-child commit and subject, exact eight paths
and diff fingerprint, queue fingerprint, selected feature, canonical warning
evidence, clean repository, absent writer and Git operation, and the completed
parent integration. Apply reuses the existing planning commit, launches no
model or child, writes no application source, creates no application commit,
and appends one deterministic recovery transaction. The projection becomes
`feature_ready`, current feature F073, with F073 starting at the existing
planning commit.

Already-recovered classification requires exact durable ledger and projection
evidence for the original run, session, transaction, commit, queue
fingerprint, selected feature, and recovery-evidence fingerprint. A second
exact apply returns `planning_transaction_already_recovered` without
validation or writes.

## Post-integration descendant contract

F072 integration transaction `44544202-7736-4953-bea2-d990c74db29c`
completed at sequence 757, released its lease at 758, and projected at 759.
Its clean terminal head
`9ee860f97bbf46ba6eeeaca744a32f4c9762d455` is the exact parent of the
authenticated planning commit. That direct-child planning-only topology is
valid and does not route through interrupted-integration recovery.

Missing integration completion, unresolved lease, unfinished Git operation,
malformed integrating metadata, unauthorized descendants, application-source
changes, or broken ancestry remain rejected.

## Acceptance criteria

- Matching canonical warning evidence passes despite one-sided
  `warnings_scope`; explicit-list, count, blocking, validity, selection,
  milestone, and inventory mismatches fail.
- Dry-run authenticates the committed result, predicts one deterministic
  recovery transition to `feature_ready`/F073, and performs zero writes,
  commits, models, or children.
- Apply reuses `8a2f7fb`, records one recovery transaction, projects F073 and
  its starting commit, and leaves no transaction, lease, or gate.
- A second exact apply appends no event, performs no validation, and creates no
  commit.
- Completed integration plus the authenticated planning child is consistent;
  malformed interrupted-integration variants fail closed.
- Existing F003, F068, F070, F072, and F097 retained-result and integration
  recovery behavior remains compatible.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
