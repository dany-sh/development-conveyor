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

The feature remains accepted, with a 2026-07-21 projection-routing repair pending its refreshed broad gates. The repair removes legacy planner authority after migration, introduces one projection-bound typed execution plan, isolates superseded cycles and stale compatibility caches, and revalidates exact feature, commit, branch, HEAD, queue, and lease evidence immediately before every dispatch. The kernel now holds the planned target across typed lease acquisition before `TransactionStarted`; in-cycle feature acceptance obtains a fresh integration plan; imported routing facts expire when canonical transactions advance; and the post-gate state remains a non-writable `human_merge_approval` control action. Final adversarial code re-review passed 22/22 focused checks with all code findings closed. Milestone integration remains pending; `integrated_commit` is intentionally unset and the milestone human gate remains active.
