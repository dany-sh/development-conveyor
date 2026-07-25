# M1-013 — Exhausted Retained-Feature Repair Recovery

## Status

Review on `codex/m1-13-retained-repair-envelope-recovery`.

## Problem

Retained-feature validation repair invokes a focused application-editing
session through the `feature_cycle` launcher route, while the canonical
terminal transaction envelope identifies the workflow as
`feature_execution`. The generic launcher comparison rejected that valid,
fully corroborated alias before host validation. Both authorized repair
sessions then changed the retained candidate, and volatile diff/session
identity in the retry signature prevented the repeated semantic failure from
being recognized.

## Contract

1. The session launcher accepts `feature_execution` as an alias for
   `feature_cycle` only for the exact
   `retained-feature-validation-repair` mode with a bound transaction,
   feature, repository, branch, starting HEAD, non-empty authorized path
   allowlist, zero children, no Git operation, and no identity conflict.
   Unrelated workflow mismatches remain rejected.
2. Repair failure signatures contain only stable route, feature, invoked and
   emitted workflow, normalized diagnostic category, envelope classification,
   authorized paths, and environment classification. Run, session,
   transaction, attempt, timestamp, and changing candidate fingerprints are
   excluded.
3. `recover-feature-repair` authenticates the original retained fingerprint,
   both exact repair-session pre/post links, the exhausted terminal snapshot,
   current live candidate, unchanged path set, empty untracked set, branch,
   HEAD, ownership absence, and append-only controller evidence.
4. Dry-run writes nothing, runs no application command, launches no model or
   child, and reports the complete mutation chain, current candidate,
   five-path canonical metadata plan, four host commands, one possible direct
   child commit, append-only supersession, and expected
   `integration_pending` result.
5. Apply revalidates under the exact launch reservation and writer lease, runs
   both focused Swift filters, `swift build`, and `git diff --check` on the
   final candidate, canonically renders only the five acceptance-metadata
   paths, creates exactly one direct-child feature commit, resolves the
   original technical gate append-only, supersedes both failed recovery
   transactions, refreshes projection and compatibility caches, releases
   ownership, and stops before integration or queue reconciliation.
6. Autopilot discovers the exhausted authenticated chain as a deterministic
   recovery route and can continue from the resulting
   `integration_pending` projection without another application-editing
   model.

## Validation boundary

Focused retained-repair recovery, feature-result recovery, Autopilot,
projection, compatibility/cycle-cache, status, and consistency tests; Python
compilation; configuration and queue validation; CLI help and exact-mode
rejection; `git diff --check`; and one live F068
`recover-feature-repair --dry-run`. The complete controller suite,
application tests, live apply, ordinary resume, Autopilot restart, feature
integration, merge, and push are excluded.
