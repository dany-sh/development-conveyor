# M1-012 — Retained feature validation repair

## Status

Implementation prepared on
`codex/m1-12-retained-feature-validation-repair`.

## Problem

An authenticated retained feature result can pass deterministic identity
recovery yet fail trusted-host validation or controller acceptance-metadata
validation. The retained implementation and its provenance must remain intact,
but the controller previously had no canonical bounded model-backed route for
repairing only that exact failure.

## Contract

1. `repair-feature-result` requires the original execution transaction, failed
   deterministic recovery transaction, expected branch and HEAD, feature, and
   exactly one of `--dry-run` or `--apply`.
2. Dry-run is write-free, starts no transaction or lease, launches zero models
   and children, authenticates the exact retained paths and fingerprints, and
   displays the failed diagnostic, repair profile, allowed paths, host
   validation plan, two-attempt bound, and expected `integration_pending`
   terminal state.
3. Apply uses a fresh typed feature-writer transaction and a focused
   `gpt-5.6-terra`/high parent with zero children. The session starts from the
   retained diff, receives the exact failed diagnostic, and may only repair the
   authenticated feature paths plus contract-required acceptance metadata.
4. Unexpected paths, untracked drift, branch/HEAD drift, Git operations,
   repository-identity disagreement, conflicting ownership, or unauthenticated
   validation evidence fail closed before host validation or commit.
5. Trusted-host validation runs the evidence-derived focused tests, `swift
   build`, and `git diff --check`. Environment failures remain distinct from
   implementation/test failures.
6. At most two model-backed attempts are allowed. Identical diff, failure,
   command-outcome, and environment evidence is not repeated. Exhaustion
   preserves the diff, creates no commit or product gate, and releases all
   ownership.
7. Success creates one direct-child feature commit only after all validation
   passes, appends supersession and gate-resolution evidence without deleting
   history, refreshes projection and compatibility caches, and stops at
   `integration_pending` without integration or queue reconciliation.
8. Autopilot recognizes this route after deterministic retained-result
   validation failure and emits repair lifecycle events before continuing to
   ordinary milestone integration.

## Validation boundary

Focused repair, retained-result recovery, Autopilot, cycle-cache, projection,
status, and consistency tests; Python compilation; configuration and queue
validation; command help and invalid-mode checks; `git diff --check`; and one
live F068 `repair-feature-result --dry-run`. The complete controller suite,
application tests, live repair apply, ordinary resume, and Autopilot restart
are excluded.
