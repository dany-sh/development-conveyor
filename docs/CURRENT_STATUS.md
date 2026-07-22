# Current Status

Last updated: 2026-07-21

## Summary

M1-001 is integrated in the canonical `codex/development-conveyor` history. Its immutable accepted feature ref remains `codex/m1-transactional-workflow-kernel` at `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`; that exact commit is an ancestor of the validated canonical milestone head `96c5c4a85f54b183e7c6b798890b947bd1de782f`.

Case Manager and Interview Companion have valid imported controller ledgers and matching projection caches. Interview Companion remains `integration_ready` with F005 and a fresh `milestone_integration` plan even though its live queue records F005 as `ready`: recovery resolves `SELF` only from the immutable accepted feature-ref queue snapshot, requires that feature HEAD's sole parent be the exact milestone HEAD, and independently binds both refs. The prior failed transaction and session are terminal corroboration only and are not resumable. The controller now executes exact conflict-free integration through its repository-owned immutable-plan executor with one adopted controller lease and no model session. The common-Git runtime exclusion is installed before transaction evidence or lease acquisition. The real F005 integration remains intentionally unperformed.

The queue records the resolved accepted and integrated feature commit as `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`, `integration_status: passed`, and `last_validated_commit: 96c5c4a85f54b183e7c6b798890b947bd1de782f`. Commits `164d361c77a3b6adacdd7acf0fd934f2b6b6b045`, `c04a262dc796d5c34004d2287e24eb23e598cabd`, `01f3ddf0f724537013b45177431427f4eca2b256`, and `96c5c4a85f54b183e7c6b798890b947bd1de782f` are post-integration repair/finalization commits, not feature commits. Milestone M1 remains active and its human gate remains in force.

## Current milestone

M1 — Transactional workflow kernel

## Active feature

M1-001 — Transactional workflow kernel and evidence ledger (`integrated`)

## Known blockers

The milestone human gate remains. The real Interview Companion F005 integration remains intentionally unperformed.

### Deterministic milestone-integration handoff repair — 2026-07-21

- Prior interface failure: the model/global skill reran live queue discovery and saw F005 as `ready`; the application lacked the exact local runtime exclusion; and the controller-owned lease collided with a second integrator lease acquisition.
- Execution boundary: a clean exact integration now invokes `scripts/conveyor execute-integration-plan --plan <absolute-controller-owned-plan-path>` directly. Status reports no milestone-integrator/model session for this path.
- Plan and refs: the immutable plan binds controller, repository, transaction, run, feature, accepted metadata ref, milestone start, queue, projection, ledger, validation commands, mutation paths, runtime exclusion, and typed lease identity. The executor independently validates the live milestone ref and immutable accepted ref, including the exact-child rule and dependency ancestry, without selecting from the live queue.
- Project identities: plan schema v2 explicitly binds `controller_project_id` to the controller ledger namespace and `adapter_project_id` to the live repository adapter. The accepted commit must declare the exact live adapter ID, but the adapter ID need not equal the controller ID. The integration lease adopts both identities. Schema-v1 compatibility defaults the controller ID only when its legacy `project_id` exactly equals `adapter_project_id`.
- Lease and runtime: the controller is the sole lease owner. The executor verifies and heartbeats the exact controller lease, including feature branch and accepted commit, and has no acquire/release path. `/.factory/runtime/milestone-integration/` is installed atomically and idempotently in common-Git `info/exclude`, verified against multiple descendants, and leaves tracked `.gitignore` byte-identical.
- Focused evidence: 44 tests passed across deterministic handoff, two-ref recovery, projection authority, integration lifecycle, result contract, synthetic integration, and repository modules in 90.077 seconds. The handoff cases cover one-lease adoption, live-ready queue bypass, immutable plan fields, common-Git exclusion, pre-transaction exclusion failure, mismatched lease, ref race, preserved semantic conflict, and sequence-29-style mechanical-gate recovery. No test launched a real model session or used a registered application repository.
- Live recovery: the sequence-29 gate now projects `integration_ready` with workflow `milestone_integration`, transaction mode `fresh`, accepted commit `a1f2c8dd47aaa68580cd7dfc3dc6923e04469857`, starting commit `e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7`, `old_session_will_resume: false`, `deterministic_integration_executor_would_run: true`, and empty session launches. The dry-run wrote no application data.
- Live invariants: both registered projects are `CONSISTENT`. Interview Companion remains clean on `codex/m0-foundation`; its head refs hash is `f6db54a901994a2770b76ba70dbc6131920a945f3f971caca2d7f5fb9eed5d37`, milestone tree is `85d02fc0707f7e78506dd26d7a817fd3baf2a003`, feature tree is `182311b8d9bcb8e944e10b86d9342b4d79dc4b37`, and its controller ledger remains sequence 29 with SHA-256 `6b3f2f5e68b5d00b204abbc2fc43b9847023f3e4996b0c0ecb4af73dce969843` and tail fingerprint `504867bcb3f3f2f338ce1e86df1dc810fb01c824fae6364e968936fd4811dd4d`.
- Scope: no full unittest suite or pytest run; no CLI upgrade, subagent, real F005 integration, application tracked-file mutation, push, tag, publication, deployment, or release.
