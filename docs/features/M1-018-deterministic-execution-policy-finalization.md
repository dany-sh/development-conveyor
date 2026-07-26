# M1-018 — Deterministic execution-policy finalization

## Status

Review.

## Problem

A valid queue-reconciliation parent may promote exactly one dependency-ready
feature while omitting the feature-owned `execution_policy`. The ordinary
planning validator correctly rejects that result, but the retained planning
diff, typed result, recorded validators, and queue selection can still be
complete and internally consistent. Re-running semantic reconciliation would
discard authenticated work and spend another model session to supply metadata
that the canonical application-feature resolver already determines.

The observed Interview Companion case is F072 on `codex/m0-foundation` at
`92b8a50e4343ac63ce1ea4fa664806cf9b627f9a`, with exactly seven retained
planning paths and no untracked files. Its terminal transaction records
`newly readied feature F072 lacks required execution_policy`.

## Contract

The controller may recover this failure only when all of the following remain
true:

- the terminal transaction is the authoritative queue-reconciliation failure,
  its typed result is `RECONCILED_READY_WORK`, and queue validation passed;
- run, transaction, session, repository, branch, starting HEAD, changed paths,
  original binary diff fingerprint, dependency integration evidence, and sole
  ready selection all agree;
- the writer lease is absent, the controller reservation is absent or owned by
  the recovery, no other Autopilot owner exists, no live transaction exists,
  and no Git operation or untracked content exists;
- the missing policy belongs to that exact newly ready feature, and both the
  queue and its authoritative feature specification are among the already
  retained paths;
- explicit valid queue, specification, or controller policy is preserved;
  disagreement, invalid shape, multiple missing-policy candidates, or any
  extra normalization path fails closed.

When no explicit policy exists, the controller calls the normal
`application_feature` execution-profile resolver. For F072 that canonical
fallback is:

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```

It resolves to `gpt-5.6-sol` with `medium` reasoning, one parent session, zero
children, and source `workflow_fallback`.

## Dry-run and apply

Dry-run reads only controller evidence and application metadata. It predicts
the exact normalized queue and specification bytes through an isolated
temporary Git index/object directory, reports both the authenticated original
fingerprint and predicted final fingerprint, and launches no model, child,
validator, transaction, lease, reconciliation, feature execution, or
integration.

Apply first re-authenticates the dry-run plan under the controller reservation,
checks the resolved model/reasoning pair through the normal feature-cycle
compatibility surface, acquires the planning writer lease, atomically writes
only the authenticated queue and specification normalization, reruns the
deterministic planning validators, and creates at most one direct-child
planning commit. Success projects `feature_ready`, leaves `current_feature`
null, selects F072, and exposes ordinary `feature_cycle` as the next action.
It never starts F072, repeats reconciliation, or integrates.

## Forward-path prevention

Ordinary future reconciliation resolves the same policy before planning
validation and finalization. Any deterministic queue/spec normalization is
ledger-bound to the session's original path set, the exact normalization
paths, and the final diff fingerprint. The next application-feature
compatibility gate therefore observes materialized authoritative policy
instead of rediscovering the same validation failure.

## Acceptance criteria

- Exact F072 dry-run is byte-for-byte non-mutating and reports the canonical
  policy, resolved model/reasoning/session budgets, two normalization paths,
  original/final fingerprints, zero models, and zero children.
- Synthetic apply normalizes queue and specification, passes deterministic
  validation, creates exactly one direct-child planning commit, and projects
  F072 as selected with no active feature.
- Wrong identity, branch, HEAD, path set, fingerprint, dependency, selection,
  lease, reservation, Autopilot owner, policy shape, policy agreement, or
  candidate cardinality fails before mutation.
- Existing post-integration planning recovery, queue reconciliation, Autopilot,
  execution-plan identity, and retained-result behavior remain covered by
  focused regression tests.

## Execution policy

```yaml
execution_policy:
  profile: ambiguous_or_authoritative
  parent_sessions: 1
  child_sessions: 0
```
