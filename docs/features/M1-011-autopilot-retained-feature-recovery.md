# M1-011 — Phase-aware retained feature-result recovery

## Status

Implementation prepared on `codex/m1-11-autopilot-retained-feature-recovery`.

## Problem

Deterministic feature preparation consumes `selected_next_feature` before
feature execution. A retained structured feature result therefore cannot use
the now-empty queue selection as its active-feature identity. Treating that
consumed selection as mandatory converts an exact technical recovery condition
into a human product gate and stops continuous Autopilot.

## Contract

1. During feature preparation, execution, and acceptance, active identity is
   authenticated from the exact transaction feature, projection
   `current_feature`, run/session, feature branch, starting commit, and queue
   feature. Null consumed selection fields are allowed; non-null
   contradictions fail closed.
2. General retained-result recovery authenticates the exact preparation and
   execution transactions, session, structured-output gate, branch, starting
   commit, tracked/untracked fingerprints, retained paths, queue identity, and
   absence of leases, reservations, and Git operations.
3. Dry-run is write-free, launches zero models and children, lists every host
   validation command, plans at most one post-validation feature commit, and
   stops at `integration_pending` without integration or queue reconciliation.
4. Apply revalidates under reservation and writer lease, runs trusted host
   validation before any commit, preserves implementation bytes, appends
   supersession and gate-resolution evidence, refreshes compatibility caches,
   and releases all ownership.
5. Autopilot recognizes the exact structured-output-invalid topology as a
   registered deterministic recovery, emits `RECOVERY_STARTED` and
   `RECOVERY_APPLIED` only on success, reloads projection, and may integrate
   only after recovery reaches `integration_pending`.
6. Deterministic failure preserves the retained diff, creates no new human
   gate, is bounded by identical-evidence retry policy, and releases Autopilot
   ownership.

## Validation boundary

Focused feature-result recovery, cycle-cache, projection, status,
consistency, and Autopilot tests; Python compilation; configuration
validation; command help and invalid-mode checks; `git diff --check`; and one
live F068 `recover-feature-result --dry-run`. The complete controller suite,
application tests, live recovery apply, and Autopilot restart are excluded.
