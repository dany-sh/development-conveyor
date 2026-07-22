# M1-001 — Transactional workflow kernel and evidence ledger

## Objective

Replace independent writable lifecycle implementations with one phase-scoped kernel whose append-only evidence is authoritative and whose current-state files are rebuildable projections.

## Scope

- Canonical transaction, workflow, lease, session-result, mutation, and command-authority contracts.
- Canonical JSONL evidence ledger with sequence and SHA-256 hash-chain integrity.
- Deterministic projection and cache validation.
- Exact recovery and idempotent legacy-state migration.
- Global consistency verification and disposable lifecycle simulation.
- Backward-compatible CLI routing through phase adapters.

## Acceptance criteria

1. All eight writable workflow types use the canonical lifecycle and exact phase lease.
2. One transaction has at most one terminal outcome, one feature cycle has one immutable accepted commit, and integration occurs at most once.
3. Session, repository, project, branch, HEAD, path, queue, runtime, lease, and human-gate identities are exact.
4. Cache poisoning, ledger corruption, stale sessions, duplicate commits/integration, and every documented interruption boundary fail closed or recover idempotently.
5. Twenty synthetic feature cycles pass without default-branch movement or prohibited operations.
6. Migration dry-runs for Case Manager and Interview Companion do not change their files, refs, worktrees, or locks.
7. The configured compile, unittest, pytest, adapter, consistency, migration, and diff checks pass.
8. Adversarial review has no unresolved Critical, High, or Medium finding.

## Current implementation note

All eight writable workflows are cut over to the canonical kernel lifecycle. Feature execution leaves exact product mutations for kernel validation and commit creation; feature acceptance is a distinct read-only transaction bound to the accepted commit; milestone integration prepares and commits the exact accepted patch once; and human-resolution compatibility materialization is durably pending before terminal evidence, replayable after interruption, and acknowledged only after idempotent persistence.

The feature is integrated as immutable commit `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`, which is an exact ancestor of validated canonical milestone head `96c5c4a85f54b183e7c6b798890b947bd1de782f`. The accepted feature ref remains fixed at that feature commit. Later commits `164d361c77a3b6adacdd7acf0fd934f2b6b6b045`, `c04a262dc796d5c34004d2287e24eb23e598cabd`, `01f3ddf0f724537013b45177431427f4eca2b256`, and `96c5c4a85f54b183e7c6b798890b947bd1de782f` are post-integration repair/finalization commits, not feature commits. The milestone human gate remains active.
