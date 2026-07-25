# Current Status

Last updated: 2026-07-25

## Summary

Binary-safe feature context and deterministic prelaunch recovery are
implemented on `codex/m1-15-binary-safe-feature-context` from exact parent
`95cbafe54761590e24d12ac461852d73d4d505c4`. The F070 failure is traced to
the broad source/test scorer admitting
`output/native-final/test-call-setup-2.png`: the scorer first decoded it
lossily and the prompt renderer later attempted strict UTF-8 without path or
phase evidence.

One shared byte-first reader now excludes generated output, recognizes PNG and
other clear binary content, bounds text size, strictly decodes UTF-8
candidates, safely renders relevant binary metadata, and fingerprints explicit
context evidence. Autopilot separates context start/readiness from an
authenticated model-session start.

The new `recover-feature-prelaunch` route authenticates the exact clean F070
zero-session topology and preserves the prepared branch and HEAD. Dry-run is
write-free; apply remains unexecuted and would refresh projection and both
compatibility caches under one recovery lease, keep F070 selected, consume no
implementation attempt, and return to `feature_preparing` without queue
reconciliation, application validation, or a commit.

Post-integration planning-result recovery is implemented on
`codex/m1-14-post-integration-planning-recovery` from exact parent
`1b7e8d979b8e287fd40ce8203f5ce9af017003ff`. The existing
`recover-planning` route now recognizes only the authenticated F070 topology
where the valid queue-reconciliation parent completed, both named nested roles
failed at the app-server initialization boundary before inspection or
mutation, F070 is the sole ready feature, and the exact retained eight-file
diff remains unchanged.

Dry-run uses the immutable parent report's recorded inventory and diff-check
evidence instead of executing application commands. Apply remains
unexecuted; it would revalidate under the planning writer lease, run the
authoritative inventory validator and `git diff --check`, create at most one
direct-child planning commit, refresh projection and caches, and stop at
`feature_ready` with F070 selected. It cannot prepare or execute F070, repeat
queue reconciliation, or integrate.

Deterministic exhausted retained-feature repair recovery is implemented on
`codex/m1-13-retained-repair-envelope-recovery` from required starting commit
`36ab4a3ebb4dafce32887ef403923db015f6f467`. The new
`recover-feature-repair` route authenticates the original retained snapshot,
both exact repair-session mutation links, the exhausted terminal snapshot, and
the unchanged live branch, HEAD, 12-path set, empty untracked set, repository
identity, and ownership state before exposing a write-free plan.

The observed F068 sessions were invoked through `feature_cycle` in
`retained-feature-validation-repair` mode and emitted canonical
`feature_execution` schema-v1 `FEATURE_ACCEPTED` envelopes. That alias now
normalizes only for this exact route after transaction, session, feature,
repository, branch, HEAD, authorized paths, zero-child, and no-Git-operation
checks. Stable duplicate signatures exclude volatile run, session,
transaction, attempt, timestamp, and evolving candidate fingerprints.

Dry-run launches zero models and children, runs no application command, and
shows the complete chain, current candidate, five-path acceptance-metadata
normalization, four required host commands, one possible direct-child commit,
append-only gate/transaction supersession, and the expected
`integration_pending` stop. The exact live F068 dry-run passed with candidate
content fingerprint `c14c5f6d`, current tracked fingerprint `8f97cd0f`, all
chain and ownership checks true, and byte-identical before/after application
and controller runtime evidence. Apply remains unexecuted.

Controller-managed retained-feature validation repair is implemented on
`codex/m1-12-retained-feature-validation-repair` from required starting commit
`14c944a4d6d052983f979e6111a2214a9e09b19f`. The new
`repair-feature-result` route is explicitly model-backed: it authenticates the
original execution and failed deterministic recovery, retained branch/HEAD,
paths and fingerprints, repository identity, Git-operation and ownership
absence, and exact failed validation evidence before exposing a write-free
plan.

Apply uses the authoritative F068 `gpt-5.6-terra`/high profile with zero
children and at most two fresh focused parents. Each attempt starts from the
retained diff, receives the exact failed diagnostic, may change only
authenticated F068 paths, and is rejected on unexpected paths before trusted
host validation. One direct-child feature commit is possible only after all
required validation passes. Success supersedes the original technical gate and
failed recovery without deleting history, refreshes projection and
compatibility caches, and stops at `integration_pending`; exhaustion preserves
the diff and creates neither a commit nor a new product gate.

Autopilot now recognizes authenticated failed retained-result validation before
generic human-gate stopping, emits `RECOVERY_STARTED`,
`FEATURE_REPAIR_STARTED`, `FEATURE_REPAIR_VALIDATION_STARTED`, and on success
`RECOVERY_APPLIED` plus `FEATURE_ACCEPTED`, then reloads projection and can
continue through ordinary `FEATURE_INTEGRATED`.

Phase-aware retained feature-result recovery is implemented on
`codex/m1-11-autopilot-retained-feature-recovery` from required starting
commit `4b5cd4b9df2da1860047c520e7430a4a8047c74b`. Feature preparation,
execution, acceptance, and terminal cycle-cache binding now authenticate the
active feature from transaction and projection-current identity plus
run/session, branch, starting commit, and queue evidence. Consumed null
`selected_feature` and `selected_next_feature` fields no longer reject the
active result; genuine non-null contradictions still fail closed.

General retained-result recovery now binds an optional deterministic
preparation transaction to the execution transaction and permits an
`in_progress` queue feature only when that exact preparation topology is
authenticated. The execution start and terminal queue fingerprints are
validated at their own phases, allowing the authenticated retained acceptance
metadata diff without pretending the queue remained byte-identical throughout
execution.

Autopilot recognizes the exact structured-output-invalid retained-result
topology before generic human-gate stopping, invokes the registered
zero-model recovery, emits recovery lifecycle events only on success, and
reloads authoritative projection before integration. Identical deterministic
failures are bounded as recoverable technical failures, create no new human
gate, and release ownership.

The exact live F068 dry-run authenticated preparation transaction
`ec4e9bc3-d8f1-4462-860c-bdb1b455cc04`, execution transaction
`d13ab73a-d696-4689-9923-9172c89b3f0e`, session
`019f9707-712f-7ae3-824d-32808b6edaa5`, branch
`codex/F068-job-application-data-model`, starting HEAD `a56d0b1`, all 12
retained paths, tracked fingerprint `bdbb94f6`, and empty-untracked
fingerprint `44136fa3`. It planned the two focused Swift filters, `swift
build`, and `git diff --check`; one candidate commit only after validation;
technical-gate supersession; zero models and children; and a stop at
`integration_pending` without integration or queue reconciliation. Before
and after application diff, `.factory`, controller project state, and original
report hashes were identical. Apply was not run.

Durable continuous feature-delivery Autopilot is implemented on
`codex/m1-10-continuous-autopilot` from required starting commit
`d9ea135e1e6aee0dc990478e1b3dc6193986624a`. The controller now exposes
`autopilot`, `stop-autopilot`, and `autopilot-status`; owns one exact
process-start-authenticated loop per project; reloads authoritative projection
and consistency evidence between existing lifecycle routes; checks durable
stop requests at outer and inner safety boundaries; and writes concise
structured events plus an atomic monitoring report.

Autopilot reuses the existing feature, planning, validation, integration,
cache, lease, and recovery implementations. Configurable conservative budgets
bound implementation, validation, deterministic recovery, planning, and
integration-finalization attempts, and identical evidence cannot repeat
indefinitely. Initial execution preserves one parent at a time and zero
children. Default-branch merge, push, tag, publication, deployment, release,
and notarization remain prohibited.

The exact Interview Companion Autopilot dry-run is `CONSISTENT` at
`feature_ready`, selects F068 first through `feature_cycle`, estimates one
`gpt-5.6-terra`/high parent and zero children, and performs zero writes,
models, children, leases, transactions, tests, feature executions,
integrations, or application mutations. Before/after controller runtime,
Interview Companion, and Case Manager snapshots are identical.

Transactional approved product-plan reconciliation is implemented on
`codex/m1-9-product-plan-reconciliation` from required starting commit
`4a34f8eafa443c7c2ed3c51df02d9fdcbd42f497`. The new
`reconcile-product-plan` route pins a regular non-symlink brief, repository,
branch, HEAD, queue, ledger, and projection; dry-run is write-free; apply uses
one `gpt-5.6-sol`/medium planning parent and zero children under the existing
planning lease, kernel, commit, ledger, projection, and cache finalizers.

The route permits only the approved planning surface, preserves stable feature
IDs and F097 accepted/integrated evidence, requires compatibility-only legacy
simulation language, validates ownership, Practice, Live, question, transcript,
review, scoring, and export semantics, and selects F068 as the sole ready
feature. Exact retained diffs have a dedicated zero-model
`recover-product-plan` path that revalidates the brief, file hashes, diff,
semantics, and lease boundary before committing.

The exact Interview Companion dry-run passed on `codex/m0-foundation` at
`4eb4d69b5918644c6291cc466cac0678c1e9715a`, ledger sequence 508, with approved
brief SHA-256 `e95f55f73b75754966a7e5533f0705f4c36733ca910328b0d042bcc0494c3cc4`.
It reported the stable post-F097 topology, zero model and child launches, no
application source or tests, no integration or feature execution, and the
planned `queue_reconciliation` to `feature_ready` path with F068 selected.
Before/after controller runtime and application `.factory` hashes were
byte-identical, the application remained clean, and consistency remained
`CONSISTENT`.

Post-transition application cycle-cache finalization is implemented on
`codex/m1-8-post-transition-cycle-cache-finalization` from required starting
commit `470f0c3c7cd0894fc37009f053e916a704dd3926`. The repair keeps the M1-7
malformed controller compatibility-cache path while adding a generalized,
state-transition-aware application-cache path for completed integration
transactions.

The exact F097 live dry-run recognizes the schema-valid cache at ledger
sequence 497 as the immediately preceding accepted-feature representation,
authenticates integration transaction
`22d297a3-628a-4cc8-af18-bd9022929621`, and derives the canonical
`queue_reconciliation` replacement at ledger sequence 508 and terminal
milestone HEAD `4eb4d69b5918644c6291cc466cac0678c1e9715a`. It reports stale SHA-256
`9312a681abccea7a7b5e6bebfa93a46abda1dd279a1df5a0f89817aa96ed901c`,
zero model or child sessions, no ledger/projection mutation, and only
`.factory/conveyor-state.json` as a future ignored mutation. Live apply remains
unexecuted.

Successful deterministic milestone integration now finalizes the canonical
application cache after the terminal HEAD and final projection are durable.
If that derived-cache write fails, successful integration evidence and the
released writer lease remain intact; the controller reports a recoverable
derived-cache finalization failure with `repair-cycle-cache --apply` and never
creates a human product-decision gate or repeats the integration.

The retained feature-result recovery route is generalized on
`codex/m1-6-general-retained-feature-result-recovery` from starting commit
`a7f6fc7560edda5d823c9aee454330293556066b`. The protected F003 behavior and
regressions remain unchanged. Later recoveries authenticate the exact original
execution, terminal marker, gate, projection, queue decision contract, branch,
HEAD, tracked/untracked fingerprints, exact path set, and untracked file hashes
before offering a zero-model dry-run or apply.

The exact F097 dry-run accepts only the narrow `feature_cycle` to
`feature_execution` alias after every surrounding feature identity matches. It
binds the retained nine tracked and two untracked paths, tracked fingerprint
`2e9c0edd`, untracked fingerprint `84ce3c83`, and the two recorded untracked
SHA-256 values. Future apply will run the two focused Swift test filters,
`swift build`, and `git diff --check` on the controller host before creating
one candidate/accepted feature commit. Apply remains intentionally unexecuted;
no application repository, model session, child session, integration, or queue
reconciliation was started during this controller repair.

The deterministic queue-feature decision route is implemented on
`codex/m1-5-queue-feature-decision-resolution` from required starting commit
`1aec952ee358fbb642cb5100d919e2f2a44d8bf2`. The dedicated
`resolve-feature-decisions` command accepts strict JSON, requires exactly one
mode, authenticates the application adapter and repository plus branch, HEAD,
projection, queue questions, dependency, lease, reservation, and unstarted
feature evidence, and never invokes the pinned project-level gate resolver.

The exact Interview Companion dry-run passed at branch `codex/m0-foundation`,
HEAD `49ad74f12a1fd57e1b2fbe3dfe31d173c643b497`, and ledger sequence 445. It
preserved the historical F002 project-gate record, matched the F006, F011, and
F097 questions, validated F097's integrated F005/F008/F009 dependencies, and
planned F010 back to proposed with F097 as the sole ready feature. It wrote
nothing and planned zero model or child sessions. Apply remains intentionally
unexecuted.

The queue-result recovery and zero-child enforcement repair is prepared on
`codex/m1-4-queue-result-recovery-child-budget` from required starting commit
`d8db9a47a4f1428b83364b3dc3c09a0f16352382`. Compatible legacy queue terminal
fields normalize only under exact controller run, session, transaction,
repository, branch, and HEAD corroboration. `RECONCILED_READY_WORK` now forces
the canonical `feature_ready` transition.

Zero-child launches set `agents.enabled=false`, disable both multi-agent feature
variants, verify the model-visible prompt contains no collaboration tool
contract, and terminate on any observed collaboration call. The failed
Interview Companion run's historical child attempts remain diagnostic evidence
and are not relaunched.

The exact retained ten-file dry-run passed for run
`f5a92b3b-8aa8-4a62-9d05-fc5754351435`, session
`019f9056-3267-7181-acf9-5596767837c5`, transaction
`3250bdab-b2bb-4e46-9710-139f67509aa9`, starting HEAD `9a32da31`, and binary
diff `9997ec3d`. It selected F010, preserved F009 as integrated and
F006/F011/F097 as human-decision work, planned zero models and children, and
left both application repositories plus all controller runtime files
byte-identical. Apply remains intentionally unexecuted.

The accepted-feature semantic-status and partial-finalization recovery repair is
prepared on `codex/m1-3-semantic-accepted-status-recovery` from required
starting commit `a69aa12ac2be68c7b3a353a376f8d34d744b3595`. Acceptance now resolves
exactly one supported feature-state bullet inside `## Factory position`,
renders and validates all five authorized metadata files before writing, and
rolls the set back if a transactional replacement fails.

The protected F009 dry-run binds candidate `777ff201`, milestone parent
`639919c`, completed feature transaction
`3efad53f-fae1-459b-be84-b9d2d7a370f7`, failed acceptance transaction
`dd695fa7-9713-49da-92ef-f8b40c86ea59`, and exact retained three-file
fingerprint `23ec1510822e027d10c48fd5ea05122ef0f62de2f1cf1e0c8d1ea11c20743882`.
All preflight checks pass with zero model or child sessions and no application
write. Apply remains intentionally unexecuted.

The queue-reconciliation warning-evidence repair is prepared on
`codex/m1-3-warning-evidence-normalization` from required starting commit
`96391c4d7107e18beac272cfa7bcd6b5a218c6fb`. New structured results use
`warning_count`, optional `warnings_scope`, optional `blocking_warnings`, and
an optional string-array `warnings`; schema parsing and semantic comparison now
share the same normalizer. Exact historical compatibility is limited to
Interview Companion run `826d9612-0cb1-441d-91ca-7531e64295bd`.

Its live non-mutating recovery preflight validates the retained eight-file
planning diff, starting HEAD `2b48544aa1d9f5ae01aa16e189581405ac8ff317`,
session `019f8e4c-19b4-7341-b860-90a784efa190`, 18 deterministic nonblocking
M1-M9 preparation warnings, integrated F002/F008 dependencies, and F009 as the
sole ready feature. Apply remains intentionally unexecuted; the recovery plans
zero model and child sessions, one planning-only commit, atomic ledger,
projection, and compatibility-cache finalization, and a stop at
`feature_ready` before feature execution.

The bounded `scope-features` controller command is implemented on
`codex/m1-3-scope-features-command` from required starting commit `ebfd6e9`.
Dry-run exposes the exact target IDs, authorized paths, requested new features,
single ready feature, dependency and execution-policy changes, validators, and
the fixed `gpt-5.6-sol`/medium one-parent/zero-child plan without mutation.
Apply uses the existing typed planning lease, workflow kernel, ledger,
projection, and atomic compatibility-cache binder, creates one planning-only
commit, and stops at `feature_ready` before feature preparation or execution.
An exact retained valid diff can be finalized by the zero-model
`recover-scope-features` path.

The implementation was validated only with disposable synthetic application
repositories. No real `scope-features --apply`, ordinary resume, queue
reconciliation, application test, Feature Factory, Milestone Integrator, named
agent, or child session ran.

The transient cache-recovery provenance repair is prepared on `codex/m1-2-transient-cache-recovery-provenance` from starting commit `f85b84a9de80864dabb6105b8a3a152954817620`. Newly materialized compatibility cycle caches no longer persist `cache_binding_recovery`. Deterministic rebinding alone may normalize the one legacy object when it contains exactly non-empty `source_transaction` and `recovery_run_id` strings; every other unsupported top-level field and every malformed or extra legacy nested field still fails closed.

Completed Interview Companion planning recovery remains authoritative and is not repeated. Its paused/no-feature projection routes the stale compatibility cache to zero-model `cache_binding_recovery`; the later user-applied recovery will rewrite only ignored `.factory/conveyor-state.json` and stop.

The M1-002 accepted-commit finalization repair is prepared on `codex/m1-2-finalized-accepted-commit` from starting commit `b6d412553b3efc42637065dbba7d926aebc8aa8f`. Normal feature execution now preserves the model-produced commit as candidate evidence and deterministically reconstructs one authoritative direct-child accepted commit containing the validated implementation plus the established acceptance metadata. `accepted_commit: SELF`, exact parent/ref checks, authorized metadata-only tree comparison, and the existing immutable two-ref integration validation all pass before the controller may project `integration_ready`.

The seven named profiles are schema-validated, newly readied features must provide an `execution_policy`, and historical ready features retain a validated fallback. Context discovery and verification planning remain independent from profile selection; planning-only application features fail before launch. No keyword scoring, per-run complexity arithmetic, or broad automatic escalation was added.

Interview Companion F004 candidate `8ae5c94df59119d89d3c2ac6fd7508a47a426ff6` remains the complete historical implementation evidence. A protected zero-model recovery dry-run binds it to milestone base `7a754a4b4741b85ac51b8b516cebd25cae1eec69`, the exact feature ref, both completed transaction identities, all 213 existing ledger fingerprints, eight source/test paths, and five authorized metadata paths. Apply uses the normal finalizer, appends recovery evidence, refreshes projection/cache through the controller transaction, and stops before milestone integration.

The dedicated zero-model feature-result recovery path has been exercised against Interview Companion F003. It preserved the original failed transaction `8e45c566-49eb-452e-b5cc-c10f40e762fa` and session `019f8add-bd2b-7662-aca1-a15fe0da1cf1`, recovered the exact existing 25-path implementation, and created accepted feature commit `543615bd0cd70e8cd56d70e4a508c076954e6cb9` on `codex/F003-single-window-application-shell`. The application worktree is clean, no writer lease remains, no model or child session ran during recovery, and milestone integration was not performed.

Host acceptance passed the three focused routing/lifecycle suites, debug build, all 112 tests, release build, staged-app verification, native Accessibility navigation across all seven destinations with exactly one resizable standard window, profile-aware content audit, feature inventory validation, documentation/ADR checks, and `git diff --check`. Interview Companion consistency now reports `CONSISTENT` with authoritative and compatibility state at `integration_pending`.

## Current milestone

M1 — Transactional workflow kernel

## Active controller repair

Post-integration planning-result recovery (`review`)

## Next boundary

Commit the controller repair. Do not run `recover-planning --apply`, restart
Autopilot, run ordinary Conveyor resume, resolve the technical gate through
the generic human-decision route, integrate F068, reconcile its queue, merge,
push, tag, publish, deploy, or release.

## Validation scope

Validation is limited to focused planning recovery, Autopilot, cycle-cache,
projection, status, consistency, and execution-plan regressions, Python
compilation, configuration validation, command help/mode checks, the exact
Interview Companion F070 planning-recovery dry-run, and `git diff --check`.
Application tests, the complete controller suite, Autopilot apply, ordinary
resume, existing queue reconciliation, feature work, and milestone integration
are intentionally excluded.
