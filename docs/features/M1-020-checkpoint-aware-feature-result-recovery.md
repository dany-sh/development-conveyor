# M1-020 — Checkpoint-aware retained feature-result recovery

## Status

Review.

## Problem

M1-017 records `CheckpointRecorded` immediately after an authenticated
`SessionLaunched` event so feature execution advances from branch preparation
to in-progress execution before terminal output is consumed. Retained
feature-result recovery still accepted only the earlier seven-event topology,
so canonical F072 evidence failed
`original_transaction_exact_event_topology`. Autopilot then invoked the same
deterministic route again against unchanged evidence and mislabeled the
repetition as bounded recovery exhaustion.

## Exact topology contract

The retained-result validator accepts exactly two original feature-execution
topologies:

```text
TransactionStarted
LeaseAcquired
SnapshotCaptured
SessionLaunched
HumanGateRaised
LeaseReleased
ProjectionUpdated
```

```text
TransactionStarted
LeaseAcquired
SnapshotCaptured
SessionLaunched
CheckpointRecorded
HumanGateRaised
LeaseReleased
ProjectionUpdated
```

The second form is valid only when the checkpoint payload is exactly:

```yaml
checkpoint: authenticated_feature_session_launched
previous_phase: branch_preparing
next_phase: feature_in_progress
```

Every event must retain the exact transaction, project, repository,
repository-path, workflow, run, and session lineage. The events must be
globally contiguous and each event must point to the immediately preceding
event fingerprint. Missing, duplicated, misplaced, foreign-transaction,
extra-field, wrong-phase, unrelated-event, missing-terminal,
duplicate-terminal, or broken-chain variants fail closed. The pre-M1-017
topology remains valid only as the exact checkpoint-free sequence.

The shared validator is used by the protected F003 path and the general
retained-result path used by F068, F070, F072, and F097. Existing
feature-cycle to feature-execution workflow alias normalization is unchanged.

## Autopilot unchanged-evidence suppression

After a deterministic recovery preflight failure, Autopilot fingerprints the
complete route evidence, recovery capability version, exact diagnostic,
canonical projection identity, technical gate, and current repository
snapshot. It reloads the authoritative plan once:

- unchanged route and evidence stop at `technical_recovery_required`;
- the route is not invoked a second time;
- exactly one deterministic attempt is recorded;
- the feature is preserved and not quarantined or marked complete; and
- no false bounded-exhaustion claim is emitted.

A later retry is permitted only when the recovery evidence fingerprint changes,
including a changed recovery capability version. Distinct changed evidence
continues to use the existing deterministic retry budget.

## F072 live boundary

The exact F072 dry-run binds:

- branch `codex/F072-job-application-detail-workspace`;
- starting HEAD `b745d648f406ab5a2024d9ea07198c187024e135`;
- original run `e5a1e74f-dde2-4874-832e-64303a54f44b`;
- original transaction `6fb8645d-740e-4acc-9f5c-973964daf01a`;
- original session `019f9cd5-e590-7800-a1a9-9b220c191419`;
- gate `gate-3035b9a4d00f239afcbbb39d672ddf0f`;
- 12 exact tracked paths and tracked fingerprint
  `59d0c8b8af72054fef8ff207f33dc08a70f783b99c4c7c3006560b198d7c5e62`;
  and
- no untracked paths and untracked fingerprint
  `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`.

Dry-run runs no application command and predicts `swift test --filter
DomainModelTests`, `swift build`, and `git diff --check` for apply. Apply
remains explicit and unexecuted; it may create exactly one direct-child
accepted F072 commit whose parent is the starting HEAD, supersede the bound
technical gate append-only, and stop at `integration_pending` without queue
reconciliation or integration.

## Acceptance criteria

- Both exact topologies authenticate and every malformed checkpoint or
  terminal variant fails closed.
- Wrong run, transaction, session, branch, HEAD, gate, path, fingerprint, or
  ownership state remains rejected by the existing recovery contract.
- Consistency recognizes the exact dirty F072 worktree and status prioritizes
  `feature_result_recovery` over human-decision resolution; ordinary resume is
  unavailable.
- Dry-run writes nothing and launches no model or child.
- Synthetic apply creates one direct-child commit and stops at
  `integration_pending`.
- F003, F068, F070, and F097 recovery behavior remains compatible.
- Unchanged deterministic preflight failure is invoked once, records its exact
  diagnostic and complete evidence fingerprint, and does not quarantine.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
