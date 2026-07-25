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

`Autopilot` is a controller-level coordinator above `CycleEngine`; it is not a
second workflow engine. It reads the same authoritative plan and consistency
classification, dispatches one existing normal or deterministic recovery route
at a time, and then reloads projection evidence before advancing. Each
model-backed feature therefore receives a fresh focused session and its
authoritative execution profile, while deterministic transitions launch no
model.

Feature context is a separate authenticated prelaunch phase. The shared
byte-first context reader excludes generated output, bounds file size,
classifies binaries before strict UTF-8 decoding, renders relevant binary
assets as metadata only, and emits a deterministic evidence fingerprint.
Autopilot records context start and readiness before launch, and records a
feature session start only after the launcher exposes an authenticated session
identity. A zero-session context failure remains a recoverable
`feature_preparing` topology rather than an implementation failure.

One controller-owned ownership record under
`state/autopilot/<project>/ownership.json` binds project, repository identity
and path fingerprint, host, PID, process-start evidence, heartbeat, and last
completed lifecycle event. A live record rejects duplicate startup. A dead
record is recoverable only after exact repository, process, Git, last-event,
and writer-lease authentication; elapsed time is never sufficient. The loop
writes monitoring state atomically to
`reports/autopilot/<project>/latest.json` and removes ownership on every
terminal path.

`stop-autopilot` writes an atomic durable request independent of the running
process. The loop checks it at the outer transition boundary and through
CycleEngine lifecycle observation before transaction open, model launch,
application mutation, commit, deterministic integration, between validation
tiers, and before the next transition. A request never kills an in-flight Git,
commit, ledger, projection, or cache operation; the current existing route
reaches a coherent checkpoint, the request is acknowledged durably, and the
ownership record is released.

`ExecutionPlan` is the only writable-routing contract once a valid ledger and matching projection cache exist. It binds the ledger and projection fingerprints, workflow, feature and accepted commit, starting branch and commit, session recovery eligibility, typed lease, and success state. Planner-facing status is derived from that plan; legacy cycles remain diagnostics only. Immediately before a writable transaction, the controller reloads the projection under its launch reservation, validates the exact queue and Git identities, and passes the planned branch and HEAD into `WorkflowKernel`. The kernel preacquires the typed writer lease, recaptures the repository and target ref while leased, and appends `TransactionStarted` only if the leased snapshot still matches the plan. Compatibility project state is a fingerprint-bound, atomic, idempotent cache and never overrides routing.

Application feature mutation uses one direct bounded parent Codex session. The selected feature identity is rechecked at queue selection, kernel creation, `SessionRequest` construction, prompt rendering, terminal parsing, semantic validation, reporting, and projection. A zero child-session budget sets `agents.enabled=false`, disables both multi-agent feature variants, and runs a model-free prompt-input capability probe before fresh or resumed invocation. The launcher also treats any observed collaboration tool event as a hard budget violation and terminates the parent process. Failure to prove the tools absent blocks before model launch. The exact terminal JSON schema binds every execution identity and requires the actual launcher-observed session UUID, unchanged branch and HEAD, the exact live changed-path set, implementation evidence, and controller-owned acceptance acknowledgment. Feature Factory and named child roles are not part of this execution path.

The model-produced feature commit is candidate implementation evidence, not necessarily the immutable accepted commit. After validation, controller-owned deterministic acceptance renders the queue, feature specification, feature catalog, current status, and run log completely in memory before any write, then replaces the validated set transactionally with rollback on a write failure. The current-status transformation resolves exactly one supported feature-state bullet inside `## Factory position`, verifies its feature identity, and normalizes it to the accepted integration-pending representation without changing references elsewhere. The queue uses `accepted_commit: SELF` to avoid a circular SHA dependency. Git plumbing then reconstructs one authoritative accepted commit whose sole parent is the planned milestone start and whose tree combines the validated candidate implementation with those metadata changes; the feature ref moves atomically from candidate to accepted commit. Before any accepted terminal or `integration_ready` projection, the controller runs the same two-ref immutable metadata validation used by milestone integration, proves the exact parent and branch head, and compares implementation-tree fingerprints with the authorized metadata excluded. Candidate and accepted identities, authorized paths, comparison evidence, and ref movement remain separate ledger evidence. A metadata-only child on top of a candidate remains invalid.

Feature execution profile selection is metadata-owned and independent from context discovery or verification planning. `config/execution-profiles.yaml` validates the seven named model/reasoning pairs, workflow fallbacks, and controller-owned reconciled feature policies. The resolution order is deterministic zero-model work, explicit run override, selected feature policy, historical workflow fallback, then recorded evidence-based escalation. The resolved pair is bound to Codex argv; parent-session counts and the zero-child capability are enforced at the launcher boundary. Newly promoted ready features must carry an `execution_policy`, while existing ready entries may continue through a validated fallback without mass editing.

Bounded feature scoping is an explicit planning surface rather than a queue-reconciliation side effect. `scope-features` binds one brief, the requested existing and new IDs, exact specification and metadata paths, one ready feature, dependencies, execution policies, branch, HEAD, queue, and session budgets before mutation. It reuses the planning writer lease, workflow kernel, append-only ledger, projection, atomic compatibility-cache binding, and deterministic planning finalizer, but it never dispatches the ordinary cycle engine. Successful apply stops at `feature_ready`; exact retained-diff recovery is zero-model and cannot start feature preparation or execution.

Queue-feature decision resolution is a separate zero-model planning surface. `resolve-feature-decisions` binds strict JSON containing the exact recorded questions, approved resolutions, dependency changes, desired sole ready feature, application adapter identity, repository identity, branch, HEAD, and current projection selection. It requires a clean, operation-free worktree, no writer lease or controller reservation, no active transaction, integrated selected-feature dependencies, an existing specification and acceptance criteria, and proof that the superseded ready feature was neither prepared nor started. Apply changes only the enumerated planning documents, creates one direct planning commit, preserves the historical project-gate record without invoking its resolver, and records the prior and resulting selection in the ledger, projection, compatibility cycle cache, and report. It launches no session and stops at `feature_ready`.

Queue-validation warnings use one shared evidence contract at schema parsing, session semantics, deterministic comparison, planning recovery, consistency classification, and report comparison. New structured results require a non-negative `warning_count`; may add an explanatory `warnings_scope`; may add `blocking_warnings`; and may include `warnings` only as a string array. The deterministic validator's warning list owns the actual count and blocking classification. The one recorded Interview Companion prose `warnings` summary is accepted only by its exact run, session, transaction, path set, and recovery identity; it is never treated as an exact warning list or generalized to live results.

The `KernelWorkflowBridge` is phase-sized. Its handler may produce mutations and typed evidence, but the kernel owns validation, final commit, terminal event, lease release, and projection. Compatibility materialization intent is recorded before terminal evidence. The deterministic callback remains replayable while that intent is unacknowledged, including after recovery, and a post-terminal append-only acknowledgment is permitted only after idempotent materialization succeeds.

Recovery first inspects immutable evidence and exact repository state. It can resume, finalize one already-created exact commit, supersede an interrupted recovery, block, or require a human decision. A queue-reconciliation terminal marker may normalize `repository` to `repository_identity`, `result` to `classification`, and controller-owned project/workflow fields only when the invoked run, session, transaction, branch, HEAD, and repository identity corroborate exactly; any canonical/legacy or controller identity conflict fails closed. `RECONCILED_READY_WORK` canonically transitions to `feature_ready`, so legacy `feature_preparation` text cannot override kernel state. Stale lease archives use safe transaction identities, a controller-confined directory, no-follow descriptor access, and reject unsafe archive roots or targets. Migration imports legacy source bytes by fingerprint into a deterministic recovery transaction; dry-run never writes, and apply is idempotent.

A valid post-integration planning parent remains recoverable when both named
planning roles are provably unavailable at the nested app-server
initialization boundary. This exception is bound to the exact F070
missing-policy topology, authenticated role-launch command events with no
nested thread, findings, or file mutation, the valid parent envelope, recorded
inventory and diff-check evidence, the sole ready selection, and the exact
retained planning diff. Dry-run reuses those immutable validation records and
runs no application command. Apply reruns the authoritative validators before
one possible planning commit, refreshes projection and caches, and stops at
`feature_ready`; ordinary missing execution policy and post-inspection role
failure remain invalid.

Cache-binding recovery provenance is transient. The recovery result, append-only evidence, and controller reports retain the applicable source and run identities, while the rewritten compatibility cycle cache contains only the common canonical schema. Deterministic rebinding may load the one historical top-level `cache_binding_recovery` object only when it contains exactly non-empty `source_transaction` and `recovery_run_id` strings; normalization validates the remaining document against the common schema and never rewrites that legacy field.

Terminal feature-result recovery is a distinct zero-model transaction. The protected F003 contract remains unchanged. Later retained feature results use a general fail-closed contract that proves the exact original event topology, report and terminal-marker identity, repository, run, transaction, session, branch, HEAD, clean starting snapshot, retained tracked/untracked fingerprints and paths, untracked file hashes, current gate and projection, queue and approved-decision state, absent lease/reservation/active transaction, and absence of any prior recovery or manual feature commit. The sole workflow alias is `feature_cycle` to `feature_execution`, and it is accepted only after every surrounding feature-execution identity matches; arbitrary workflow conflicts still fail. Focused host tests are derived from retained changed test paths and matching source-test pairs, followed by the configured build and diff gates. Apply revalidates under the controller reservation and a fresh typed `feature_writer` lease, runs every host validation before committing, preserves implementation content, transitions acceptance metadata, creates exactly one direct-child candidate/accepted feature commit, durably records supersession of the structured-output-invalid transaction, resolves only its bound gate, and refreshes the ledger projection plus controller and repository compatibility caches. Validation failure creates no commit, preserves the retained diff and original gate, records explicit failure evidence, and releases the lease. Recovery never launches a model, performs milestone integration, or begins queue reconciliation.

Retained-feature validation repair is the explicit model-backed successor to
that deterministic route, not an extension of deterministic validation.
`repair-feature-result` authenticates both the original feature execution and
the failed recovery transaction, including exact event topology, branch, HEAD,
repository identity, retained paths, tracked/untracked fingerprints, absence of
Git operations and conflicting ownership, and validation evidence tied to the
failed transaction. A read-only plan exposes the exact diagnostic, allowed
paths, `gpt-5.6-terra`/high zero-child profile, trusted-host command arrays,
two-attempt bound, and expected terminal state without acquiring a lease or
launching a model. Apply revalidates after acquiring the typed feature-writer
lease, launches one fresh focused parent per attempt, rejects unexpected paths
before validation, records pre/post fingerprints and redacted session reports,
and runs host validation. It never resets, stashes, discards, switches branches,
commits from the model, integrates, or reconciles the queue. A passing attempt
uses the kernel to create one direct-child feature commit and append
supersession/gate-resolution evidence before refreshing projection and both
compatibility caches. Environment failures are separate from implementation
failures; identical evidence is not repeated; exhaustion preserves the diff,
creates no product gate or commit, and releases ownership.

Feature identity in preparation, execution, acceptance, and retained-result
recovery is phase-aware. The exact transaction `feature_id` and projection
`current_feature` remain authoritative after selection is consumed, followed
by corroborating run/session, branch, starting commit, and queue feature
identity. `selected_feature` and `selected_next_feature` may therefore be null
after deterministic preparation; any non-null disagreement still fails
closed. Autopilot recognizes an exact structured-output-invalid result as this
registered deterministic route before generic human-gate stopping, emits
recovery lifecycle events, reloads projection, and can proceed to integration
only from the recovered `integration_pending` state. Identical technical
failures are bounded without creating another human gate.

Accepted-commit reconstruction is a narrower protected recovery for a completed candidate whose immutable accepted metadata is missing. Its read-only plan binds the exact historical feature and acceptance transactions, ledger tail and fingerprints, candidate, milestone parent, refs, absent lease, source/test hashes, and candidate queue snapshot. A protected failed acceptance may additionally bind one exact retained metadata-prefix path set and binary diff fingerprint; every retained file must equal the deterministic final rendering while every not-yet-written metadata file must remain byte-identical to the candidate. Extra, altered, source, test, or untracked paths fail closed. Apply revalidates the same plan under the launch reservation, adopts the exact dirty prefix when present, uses the normal deterministic accepted-commit finalizer, appends rather than rewrites ledger evidence, refreshes projection and compatibility state through the recovery transaction, and stops at `integration_ready` with `milestone_integration` as the next action. It launches no session and never integrates.

A terminal pre-mutation milestone-integration failure can produce a fresh integration plan only through a two-ref recovery contract. The configured milestone ref must still equal the failed transaction's exact starting snapshot, while exactly one self-identifying accepted feature ref must carry an immutable queue snapshot with `integration_pending`, matching branch and integration base, `accepted_commit: SELF`, pending integration, and complete acceptance evidence. The feature ref HEAD must be a non-merge commit whose sole parent is that exact milestone ref HEAD, proving one direct feature commit; it then resolves `SELF`. A failed session report may only corroborate that commit. Live queue reconciliation may meanwhile show the feature as `ready`, so recovered dispatch does not use the live queue as acceptance authority. Both refs are revalidated under the controller launch reservation immediately before the kernel starts a new writable transaction, and the terminal session is never resumed.

Fresh exact milestone integration uses a repository-owned deterministic executor, not a Codex session. Before `TransactionStarted` or lease acquisition, the controller atomically installs and verifies the root-anchored `/.factory/runtime/milestone-integration/` pattern in the repository common Git directory's `info/exclude`; tracked `.gitignore` is never changed. After the launch reservation and exact two-ref revalidation, the kernel acquires the single typed `integration_writer` lease and binds it to the immutable feature branch and accepted commit. The controller then persists an exclusive-create plan beneath its own ignored state directory. The plan binds project and adapter identity, repository identity and path fingerprint, transaction and run, feature and both refs, milestone and exact start, queue, projection and ledger fingerprints, validation commands, runtime exclusion evidence, mutation paths, and the complete adopted lease identity.

`scripts/conveyor execute-integration-plan --plan <absolute-plan-path>` is the explicit compatibility boundary. The executor never selects from the live queue, acquires a lease, binds a model session, or releases the controller lease. It validates the plan fingerprint, unchanged controller ledger, common-Git exclusion, exact live milestone ref, immutable accepted-ref metadata, dependency ancestry, accepted direct-child topology, commit audit, and exact lease owner/process/start snapshot before application mutation. It may then switch to the milestone branch, cherry-pick only the planned commit, preserve a real conflict, record integrating metadata, run the accepted adapter's configured command arrays, write ignored identity-bound runtime evidence, and create final metadata evidence. Its machine result is revalidated by the kernel before terminal ledger events; only the controller releases the lease. Successful milestone completion projects directly to `milestone_gate`, while remaining ready work projects to `feature_ready`. A model may be considered only after a deterministic semantic conflict, ambiguous evidence, or another configured human gate.

After a successful deterministic integration reaches its terminal milestone
HEAD, the kernel completes the ledger transaction, releases the integration
writer lease, persists the final projection, and then atomically finalizes the
ignored application cycle cache from that canonical projection. A cache-write
failure does not reverse or repeat the integration: it is a recoverable derived
cache finalization failure, leaves the successful transaction authoritative,
and routes only to `repair-cycle-cache`. That repair authenticates the
immediately preceding cache binding, the completed integration plan and ledger
transition, the current clean milestone branch and HEAD, and the unchanged
cache hash before replacing only `.factory/conveyor-state.json`.

## Constraints

- Production code uses only the Python standard library.
- Subprocesses use argument arrays with `shell=False`.
- Tests mutate only disposable synthetic repositories.
- No workflow may merge the default branch, push, tag, publish, deploy, release, or notarize.
- Cache state never overrides ledger state; corrupt evidence is not auto-repaired.

## Decisions

See `docs/adr/0001-append-only-phase-evidence.md`.
