# M1-024 — Atomic checkpoint-tolerant planning finalization

## Status

Review.

## Problem

Interview Companion reconciliation run
`940611fd-48c5-49ab-b641-3fd3d6535cee` returned valid
`RECONCILED_READY_WORK` evidence with the sole ready feature F078 but omitted
`selected_feature`. The controller derived the queue result late, after it had
already materialized execution-policy metadata. A subsequent feature-spec
readiness check failed, leaving a valid nine-path retained planning diff.

Recovery rejected the same transaction because runtime-policy work added a
second harmless `CheckpointRecorded` event. Exact checkpoint positions and
names incorrectly acted as a second recovery protocol.

## Identity and atomicity contract

- An explicit non-empty `selected_feature` is authoritative only when it
  matches the validated queue and ready-feature evidence.
- When it is omitted or null, `RECONCILED_READY_WORK` derives an identity only
  from exactly one structured ready feature that equals the deterministic
  queue selection and whose dependencies are authoritatively integrated.
- Empty, multiple, duplicate, conflicting, decision-gated, or
  dependency-incomplete readiness remains rejected.
- Normal reconciliation validates the model-produced diff and all semantic
  evidence before deterministic execution-policy metadata is written.
  Semantic failure therefore adds no controller mutation.

Planning recovery removes checkpoints before matching the authoritative event
topology. Zero, one, or several checkpoints are accepted without inspecting
their names only when every checkpoint agrees with the transaction/workflow,
run/session, branch and starting HEAD, lease, mutation policy, authorized
paths, retained fingerprint, selected feature, and model/child authority.
Conflicting checkpoint metadata remains a hard failure.

When deterministic normalization expanded the original source paths, the
checkpoint must prove that the source paths plus authorized normalization
paths exactly cover the terminal retained paths and bind the terminal
fingerprint. This preserves the original session result and the final retained
diff as separate authenticated identities.

## Retained F078 evidence

- Run: `940611fd-48c5-49ab-b641-3fd3d6535cee`
- Session: `019fa19f-1e02-7523-a4ec-f81793723d06`
- Transaction: `c08f3fb3-ac92-4490-8fca-c19f777f3ced`
- Starting HEAD: `12bee2fc3b3668091474dc288067cd3aced0ee08`
- Retained diff: `a53f0e984d93dd2220e3e2e6852c71dead900c79f7c26d8b9eb48536d2b529e3`

The live dry-run returns `recovery_ready`, derives F078, predicts
`feature_ready`, one candidate commit with subject
`factory: reconcile M0 queue`, and zero model or child launches. Apply is not
part of M1-024.

## Acceptance evidence

- Focused tests cover checkpoint absence, repetition, conflict, singleton
  derivation, empty/multiple rejection, semantic-failure immutability,
  normalized retained-result authentication, and idempotent repeated
  recovery.
- M1-021, M1-022, and M1-023 focused regressions pass.
- Python compilation, configuration and JSON parsing, and
  `git diff --check` pass.
- Live dry-run preserves both application repositories and protected
  controller state byte-for-byte.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
