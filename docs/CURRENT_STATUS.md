# Current Status

Last updated: 2026-07-21

## Summary

M1-001 is integrated in the canonical `codex/development-conveyor` history. Its immutable accepted feature ref remains `codex/m1-transactional-workflow-kernel` at `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`; that exact commit is an ancestor of the validated canonical milestone head `96c5c4a85f54b183e7c6b798890b947bd1de782f`.

Case Manager and Interview Companion have valid imported controller ledgers and matching projection caches. Interview Companion remains `integration_ready` with F005 and a fresh `milestone_integration` plan even though its live queue now records F005 as `ready`: recovery resolves `SELF` only from the immutable accepted feature-ref queue snapshot, requires that feature HEAD's sole parent to be the exact milestone HEAD, and independently binds both refs. The prior failed session is terminal corroboration only and is not resumable. All 33 focused projection, integration-lifecycle, result-contract, and two-ref recovery tests pass. Live status and milestone dry-run select the fresh transaction, live consistency is `CONSISTENT`, and the Interview ledger remains 21 lines at its pre-run fingerprint. The real F005 integration remains intentionally unperformed.

The queue records the resolved accepted and integrated feature commit as `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`, `integration_status: passed`, and `last_validated_commit: 96c5c4a85f54b183e7c6b798890b947bd1de782f`. Commits `164d361c77a3b6adacdd7acf0fd934f2b6b6b045`, `c04a262dc796d5c34004d2287e24eb23e598cabd`, `01f3ddf0f724537013b45177431427f4eca2b256`, and `96c5c4a85f54b183e7c6b798890b947bd1de782f` are post-integration repair/finalization commits, not feature commits. Milestone M1 remains active and its human gate remains in force.

## Current milestone

M1 — Transactional workflow kernel

## Active feature

M1-001 — Transactional workflow kernel and evidence ledger (`integrated`)

## Known blockers

The milestone human gate remains. The real Interview Companion F005 integration remains intentionally unperformed.
