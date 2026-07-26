# M1-019 — GPT-5.3-Codex application-feature routing

## Status

Review.

## Problem

The controller's existing feature-owned profile matrix separates deterministic
work, Luna metadata/context work, and Sol controller or architectural work,
but it has no narrow route for well-scoped application implementation. A
stored `generic_or_architectural` policy also cannot be changed safely after a
feature becomes ready without either manual application metadata editing or a
broader reconciliation cycle.

## Profile contract

The canonical `application_feature_implementation` profile resolves exactly
to:

```yaml
execution_policy:
  profile: application_feature_implementation
  parent_sessions: 1
  child_sessions: 0
```

The resolved model is exact `gpt-5.3-codex` with `high` reasoning. It is
intended for well-scoped application features, focused application bugs,
application tests, and known-plan refactors. Deterministic workflows continue
to launch zero models. Queue reconciliation and context preparation retain
their Luna policies. Controller architecture, planning semantics,
state-machine changes, ledger recovery, and ambiguous cross-module repair
retain their Sol policies.

Explicit valid feature policies remain authoritative. The new profile does not
replace the `application_feature` fallback and does not mass-rewrite existing
`generic_or_architectural` entries.

## Local compatibility

Compatibility is authenticated through the exact configured Codex executable
using `--version` and `debug models`. Both the exact model slug and requested
reasoning level must appear in the local catalog. Missing `gpt-5.3-codex`,
including a catalog that exposes only a similarly named model, is
`unsupported_model`; no fallback or alias substitution is permitted.

## Ready-feature policy rebind

`rebind-ready-feature-policy` accepts an exact project, feature, branch, HEAD,
old profile and session budgets, target profile, and two expected metadata
paths. Inspection requires:

- canonical `feature_ready` projection and exact selected feature;
- a ready queue entry whose queue and specification policies exactly match the
  expected old policy;
- no active transaction, session, writer lease, controller reservation, or
  Autopilot ownership;
- a clean repository with no unfinished Git operation;
- exact queue and bound specification normalization paths; and
- locally compatible target model and reasoning.

Dry-run writes nothing, acquires no lease, starts no transaction, launches no
model or child, and performs no feature preparation or execution. It reports
the old and new stored policies, resolved model/reasoning/session budgets,
compatibility result, exact normalization paths, predicted Git binary-diff
fingerprint, and one metadata-commit maximum.

Apply repeats inspection, acquires the planning-writer lease, atomically
normalizes only the queue and feature specification, validates policy schema
and local compatibility, runs feature-inventory validation and
`git diff --check`, and creates exactly one direct-child metadata commit.
Kernel terminal evidence refreshes the canonical projection; the controller
compatibility cache and application cycle cache are rebound to that terminal
projection. The lease is released, the repository is clean, state remains
`feature_ready`, the feature remains selected, and no feature session starts.

## F072 authenticated target

The future Interview Companion operation binds exact feature F072 on
`codex/m0-foundation` and changes only:

- `docs/FEATURE_QUEUE.yaml`
- `docs/features/F072-job-application-detail-workspace.md`

Its old policy is `generic_or_architectural`, one parent, zero children. Its
new stored policy is `application_feature_implementation`, one parent, zero
children. The operation must remain a rejected dry-run until the pinned local
Codex catalog exposes exact `gpt-5.3-codex` with high reasoning.

## Acceptance criteria

- Exact profile, model, reasoning, and session budgets resolve through the
  normal feature-cycle plan.
- Deterministic work remains zero-model; Luna and Sol workflow boundaries do
  not change.
- Existing explicit policies remain unchanged without the rebind command.
- Wrong feature, branch, HEAD, state, selection, old policy, path, transaction,
  session, lease, reservation, ownership, dirty state, Git operation, model,
  or reasoning fails closed before mutation.
- Dry-run is byte-stable and zero-session.
- Synthetic apply creates one two-path metadata commit, refreshes projection
  and caches, retains the ready selection, releases ownership, and does not
  prepare or execute the feature.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
