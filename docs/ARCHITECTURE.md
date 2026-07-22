# Architecture

## System context

Development Conveyor is a Python standard-library controller. It reads registered repositories, launches repository-scoped Codex sessions, and owns controller evidence. Each application repository remains an isolation boundary and exposes only its adapter, queue, Git state, runtime evidence, and shared writer lease.

## Components and data flow

The transactional path is:

```text
CLI -> phase adapter -> WorkflowKernel -> typed writer lease
                         |       |
                         |       +-> exact Git/snapshot/command validation
                         +-> append-only evidence ledger -> ProjectionEngine -> cache
```

`PhaseTransaction` supplies one lifecycle for queue reconciliation, feature preparation, feature execution, feature acceptance, milestone integration, milestone gate, human-decision resolution, and recovery. `EvidenceLedger` is canonical JSONL with monotonic sequence numbers, per-record SHA-256 fingerprints, and a previous-fingerprint chain. `ProjectionEngine` rebuilds current state from the ledger; the JSON projection is a disposable cache bound to the ledger sequence, ledger fingerprint, and its own content fingerprint.

`WorkflowWriterLease` is the sole typed repository writer lease. It binds and revalidates repository and path identity, project, transaction and lease identity, workflow and lease type, milestone, feature, branch, HEAD, run, current session, process start, host, and mutation policy. Lease and mutation targets reject symbolic links, non-regular files, and unsafe hard-link counts. `DurableLock` remains only a controller launch reservation.

`ExecutionPlan` is the only writable-routing contract once a valid ledger and matching projection cache exist. It binds the ledger and projection fingerprints, workflow, feature and accepted commit, starting branch and commit, session recovery eligibility, typed lease, and success state. Planner-facing status is derived from that plan; legacy cycles remain diagnostics only. Immediately before a writable transaction, the controller reloads the projection under its launch reservation, validates the exact queue and Git identities, and passes the planned branch and HEAD into `WorkflowKernel`. The kernel preacquires the typed writer lease, recaptures the repository and target ref while leased, and appends `TransactionStarted` only if the leased snapshot still matches the plan. Compatibility project state is a fingerprint-bound, atomic, idempotent cache and never overrides routing.

The `KernelWorkflowBridge` is phase-sized. Its handler may produce mutations and typed evidence, but the kernel owns validation, final commit, terminal event, lease release, and projection. Compatibility materialization intent is recorded before terminal evidence. The deterministic callback remains replayable while that intent is unacknowledged, including after recovery, and a post-terminal append-only acknowledgment is permitted only after idempotent materialization succeeds.

Recovery first inspects immutable evidence and exact repository state. It can resume, finalize one already-created exact commit, supersede an interrupted recovery, block, or require a human decision. Stale lease archives use safe transaction identities, a controller-confined directory, no-follow descriptor access, and reject unsafe archive roots or targets. Migration imports legacy source bytes by fingerprint into a deterministic recovery transaction; dry-run never writes, and apply is idempotent.

A terminal pre-mutation milestone-integration failure can produce a fresh integration plan only through a two-ref recovery contract. The configured milestone ref must still equal the failed transaction's exact starting snapshot, while exactly one self-identifying accepted feature ref must carry an immutable queue snapshot with `integration_pending`, matching branch and integration base, `accepted_commit: SELF`, pending integration, and complete acceptance evidence. The feature ref HEAD must be a non-merge commit whose sole parent is that exact milestone ref HEAD, proving one direct feature commit; it then resolves `SELF`. A failed session report may only corroborate that commit. Live queue reconciliation may meanwhile show the feature as `ready`, so recovered dispatch does not use the live queue as acceptance authority. Both refs are revalidated under the controller launch reservation immediately before the kernel starts a new writable transaction, and the terminal session is never resumed.

## Constraints

- Production code uses only the Python standard library.
- Subprocesses use argument arrays with `shell=False`.
- Tests mutate only disposable synthetic repositories.
- No workflow may merge the default branch, push, tag, publish, deploy, release, or notarize.
- Cache state never overrides ledger state; corrupt evidence is not auto-repaired.

## Decisions

See `docs/adr/0001-append-only-phase-evidence.md`.
