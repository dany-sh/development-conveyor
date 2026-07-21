# Current Status

Last updated: 2026-07-21

## Summary

M1-001 remains accepted and integration-pending. A focused repair on `codex/m1-authoritative-projection-routing-repair` makes the valid evidence-ledger projection the sole routing authority after migration. One typed execution plan now binds workflow, feature, accepted commit, starting branch and commit, session recovery, lease, mutation intent, and success state. Superseded legacy cycles remain historical diagnostics only; compatibility project state is a fingerprint-bound cache that is either reconciled atomically or explicitly ignored.

Case Manager and Interview Companion now have valid imported controller ledgers and matching projection caches. Read-only verification reports both `CONSISTENT`: Case Manager remains `feature_ready` with P0-003 and `feature_cycle`; Interview Companion remains `integration_ready` with F005 and a fresh `milestone_integration` plan. Their stale compatibility-cache values are exposed as observations and ignored for execution. The repair's adversarial findings are corrected, with the final reviewer focused suite passing 22/22 and no remaining code finding. Final configured broad gates are pending refresh on the repaired tree.

The feature queue records `accepted_commit: SELF` until the immutable feature commit is created. `integration_status` remains `pending`, `integrated_commit` remains null, milestone M1 remains active, and its human integration gate remains in force.

## Current milestone

M1 — Transactional workflow kernel

## Active feature

M1-001 — Transactional workflow kernel and evidence ledger (`accepted`, integration pending)

## Known blockers

Final broad validation of the projection-routing repair is pending. Milestone integration and its configured human gate remain the next product lifecycle stages; no application integration was performed by this repair.
