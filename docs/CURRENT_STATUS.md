# Current Status

Last updated: 2026-07-29

## Factory position

- Active feature: none. M2-001 — Collapse Acceptance Ceremony is integrated;
  M2-002 was not started.
- Acceptance interface: `scripts/conveyor accept-feature` is the sole supported
  deterministic entry point. It leaves the implementation branch at the exact
  candidate commit/tree and records acceptance authority in the controller
  evidence ledger.
- Validation boundary: ordinary feature acceptance consumes feature-tier
  evidence directly and invokes zero complete suites and zero release
  validations. Release inventory and reparsing remain candidate-bound.
- Dependency behavior: integration resolves completed dependencies
  cumulatively across all milestones.
- Integration result: the M2 branch fast-forwarded from
  `598f06837b89221fe37add2b7db5132f63acbb43` to accepted candidate
  `1413d37ca57ba03c16e358802d5db2fe883fcc52`, tree
  `61e17ddc532828dddcff147de4edd2c71eebd8f0`; acceptance transaction
  `54c8de75-be07-5af2-a192-6e2d669bbefe` remains the acceptance authority.
- Bootstrap boundary: no tests were run. This one-time, non-reusable path was
  required because the pre-M2-001 dispatcher required queue-materialized
  acceptance and could not consume the completed ledger-only acceptance.
- Next action: review the integrated M2 head; do not start M2-002 without
  separate authority.

## Integrated M2-000 baseline

- M2-000 status: Conveyor simplification and validation reduction was
  integrated by one-time operator bootstrap.
- Accepted implementation: `e9b9e9aca00352fbd0fa137511d342803d7951a4`;
  tree `90ba7e397b3ac43edd7279bbd1f9ec1ba538572c`.
- Integration authority: exact implementation ref
  `codex/m2-000-accepted-implementation`; bootstrap metadata commit
  `0d640258836b3c2f73d2c3ee1975ebd65bab9bb6` remains historical evidence.
- Acceptance evidence: provenance-bound release execution
  `b4e80527738c4e498933e8fbeeee7693`, external invocation
  `8289d8335400407cb6100d9263740471`, 750 tests, and 37 exact debt
  identities with 20 failures and 17 errors.
- Acceptance mode: explicitly authorized, non-reusable operator bootstrap
  because the pre-M2 interface could not consume tiered release evidence.
- Integration result: target `codex/m2-validation-simplification-integration`
  fast-forwarded from `abf511f7381cc9042958691485966e055070ac4a` to control head
  `b98ffaae48a2911f7c43475657f1f55d30c45515`; final integration metadata is
  committed separately.
- Dependency finding: M1-028 was integrated in M1 as
  `b7ac3641564c9827e426bc08b8cfe131917faa74` and is reachable from the target's
  prior head. The pre-M2 integrator failed because it checked only active-M2
  `integrated_features`.

## Historical M2 reconstruction and review evidence

Before the one-time bootstrap acceptance, M2-000 was in review on
`codex/m2-000-conveyor-simplification-final-v2` from pristine parent
`abf511f7381cc9042958691485966e055070ac4a`. M1 is paused for this isolated
reconstruction, and the existing M1-029 feature entry remains unchanged and
in progress under that paused milestone.

The M2 implementation adds explicit feature, milestone, and release validation
tiers with caller-level legacy fallback. Feature and milestone routes construct
no complete discovery command; release owns the one complete-suite observation
and any explicit repeats.

The authenticated release execution
`0b90e31ee17a46eaacf0ff8d2fca1f64` is bound to implementation commit
`029d0a08b1b00c151840e6f268a3c66103c1aae4` and tree
`ea0d11135f7e81dff1d6cc7b6edbbc9978fa8e11`. It observed 748 tests, 20
failures, 17 errors, and the exact 37 catalog identities. Offline
reconciliation preserves the historical 21-failure/16-error baseline and
reports one explicitly permitted classification shift:
`test_post_transition_cycle_cache_repair.PostTransitionCycleCacheRepairTests.test_unrelated_branch_and_changed_hash_fail_closed`,
from baseline `failure` to candidate `error`, because the authenticated
worktree intentionally omits the excluded generated evidence-ledger fixture.
The other 36 records remain classification-exact, with zero missing, added,
duplicate, pending, or unparseable identities.

The immutable reconciliation artifact has SHA-256
`df95608b65baf709c55083ee0e2d8612ef68801ced00bdce9552b8c58450ecd6`
and binds the unchanged raw artifacts, external release JSON, implementation
commit/tree, catalog, reconciliation code, and exact shift. No release suite
was repeated.

M2-000 remains in review pending one explicit human acceptance decision. The
supported deterministic accepted-commit finalizer requires the candidate to be
the exact direct child of the configured milestone base; it cannot represent
the authenticated implementation at `029d0a08` plus a direct reconciliation-
only child without changing integration authority or rewriting history.
Integration remains prohibited.

The review repairs now parse real unittest summary and parenthesized-subtest
headings into canonical catalog identities, reject unparseable or duplicate
normalized failure/error identities, and redact persisted subprocess tails
without changing raw output hashes or raw debt parsing. The prepared-parent
artifact parses to exactly 37 unique records with 21 failures and 16 errors.
A separate synthetic candidate preserves all 37 identities and changes only
the documented excluded-fixture record, producing 20 failures and 17 errors.

Post-repair pre-release gates pass: the validation-tier module passed 28 tests;
feature validation passed 25 selected tests across four commands in 4.411
seconds; milestone validation passed 39 main tests plus exactly one 20-test
observation per ref in 132.344 seconds. Both tiers invoked complete discovery
zero times. These are new repair-validation results and do not replace the
earlier supplied 21-test feature and 35-test milestone evidence. The final
release observation was then reserved for the orchestrator; the authenticated
execution and offline reconciliation above supersede that pre-release state.

A later user-supplied manual execution reported 748 tests in 945.536 seconds,
20 failures, 17 errors, and exit code 1. Its raw output was overwritten, so the
result is historical unauthenticated context only and cannot support
acceptance. M2-000 remains in review and unaccepted.

The bounded follow-up now canonicalizes Python 3.9 verbose
`display_method (module.Class) ... FAIL|ERROR` headings without duplicating a
method already present in the identity. Release children now create unique,
exclusive, mode-`0600` raw stdout/stderr artifacts and independently durable
metadata before launch, after child completion, and after parser success or
failure. No complete suite was run to exercise this repair; only short
synthetic child commands were used.

Current post-manual validation passes: 28 focused tests in 4.759 seconds;
25 feature tests across four commands in 4.630 seconds; and 39 milestone main
tests plus one 20-test observation per ref in 125.273 seconds. Feature and
milestone complete-suite invocation counts remain zero.

Final candidate evidence supersedes those timing snapshots where they differ:
five exact parser/debt/redaction checks passed; the validation-tier module
passed all 28 discovered tests; feature validation passed 25 tests across four
commands in 5.318 seconds with zero complete suites; milestone validation
passed 79 total tests (39 main plus one 20-test observation per ref) in 125.273
seconds with zero complete suites. The final adversarial review reports zero
unresolved Critical, High, or Medium findings. The parser-only repair added
zero discovered test IDs.

Release-count history is 745 tests in the rejected isolated JSON followed by
the user-supplied 748-test manual result. The three-test increase predates the
final parser-only repair, but its exact identities and cause are unresolved:
the later raw output was overwritten and its final capture is empty. The
manual result remains historical unauthenticated context. The later
authenticated release evidence is reconciled above. M2-000 remains in review
only because the supported accepted-commit path cannot bind the implementation
commit plus its reconciliation-only child without a human authority decision.

This section supersedes any later planning statement that describes M1-029 as
the currently selected controller work. No integration, default-branch merge,
push, publish, deploy, external release, or application-repository mutation is
part of the M2 feature run.

## Summary

M1-029, Generic retained repair recovery, is the sole dependency-ready
feature. It depends on integrated M1-028 and is assigned to
`codex/m1-29-generic-retained-repair-recovery`; deterministic preparation will
bind its exact integration base before production implementation begins. Its
acceptance boundary covers structured-result-invalid plus valid
`FEATURE_ACCEPTED`, host-validation-failed, and mixed repair chains; generic
evidence-derived validation commands; fail-closed identity, path, fingerprint,
topology, ownership, and command checks; and complete configured validation.

Historical M1-028, Complete queue browsing scopes, is reconciled as integrated
commit `b7ac3641564c9827e426bc08b8cfe131917faa74` on
`codex/development-conveyor`. It adds active, unfinished, and all queue scopes,
optional milestone filtering, complete read-only feature rows and milestone
summaries, future-milestone execution-ineligibility reasons, and a structured
unknown-milestone result without changing active execution selection.

Native backlog control operations are implemented on
`codex/m1-27-backlog-control-plane` from exact parent
`7e8e24af6fa4260404bbb653bea42b249f26e332`.

Controller queue selection now defaults an absent priority to P2 without
rewriting the queue, selects ready work by P1/P2/P3 then explicit queue order
then feature ID, and still requires complete dependencies and all existing
readiness checks. The `ready`, `backlog`, and expanded `prioritize` commands
are queue-only metadata controls: they use a short writer lease, launch zero
models and child sessions, preserve unrelated fields and order, and never
start execution. Queue JSON now provides the presentation-only Kanban column,
priority, position, dependency/readiness, execution, Git, specification, and
terminal-result fields consumed by Factory Desktop.

Focused queue-control coverage (16 tests) passes in disposable synthetic
repositories. The full controller suite and application tests remain
intentionally unrun.

Deterministic queue control and project pause are implemented on
`codex/m1-26-deterministic-queue-control` from exact parent
`5dff933769374943dcad003eb42039c98d2472da`.

`queue` now reports the active milestone in file order with exact readiness,
dependency, blocking, current/selected, execution-profile, and priority
evidence. Routine validated-queue selection uses queue order and eligibility
without a planning model; explicit selection proves exact feature eligibility
and model/capability availability before mutation. `prioritize` atomically
changes only queue entry order, while `pause` and `unpause` update one flag in
the existing project runtime authority without adding a workflow state or
starting work.

All 19 focused M1-026 queue, pause, and CLI tests pass. All 140 focused
M1-024/M1-025 planning, capability, recovery, cache-binding, and projection
regressions pass. Changed-module compilation, JSON-compatible parsing,
`validate-config`, and `git diff --check` pass. The complete controller suite
and application tests were intentionally not run.

The single permitted live `queue --project interview-companion` inspection
reported `no_ready_work`, `paused: false`, no current or selected feature, and
zero model or child launches. It did not start F079. Interview Companion
remains clean on `codex/m0-foundation` at
`860769e814c6d3a8c07cd7e84fad06a3eefe50ee`; Case Manager remains clean on
`codex/p0-foundation` at `f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0`.
All 28 protected controller state, report, and log files remained
byte-for-byte unchanged during the live check.

Explicit capability selection and failed-resume recovery are implemented on
`codex/m1-25-capability-selection-and-resume-recovery` from exact parent
`b03469dc11294c49d53be7d5665fa02c647d60dc`.

Swift source discovery no longer makes cached build-macos-apps plugin skills
mandatory. Feature execution requests the proven Conveyor and Feature Factory
core; additional macOS skills are required only when the selected feature
contract names their exact identifier. Historical planning and integration
failures no longer intercept a later feature failure.

The live F078 repository remains clean on
`codex/F078-session-to-application-linking` at `98590888`. Read-only status
now selects deterministic `cache_binding_recovery` with zero model and child
sessions instead of raising an unrelated historical recovery error.

The deterministic cache rebind has now completed with zero models, children,
or application commits and only the ignored `.factory/conveyor-state.json`
updated. Consistency reports `CONSISTENT`. Read-only status now authenticates
the exact run-scoped, zero-session capability failure and selects deterministic
`feature_prelaunch_recovery`; after that zero-model repair, the existing F078
feature execution can resume with the exact four-capability allowlist. Both
ordinary resume dispatch paths now execute the authenticated recovery before
the raw failed projection's generic consistency fallback.

The corrected F078 feature session launched on Terra/medium with the exact
four-capability allowlist and produced a retained 10-file implementation diff.
Its terminal envelope was `structured_output_invalid`; no application commit
was created and the writer lease was released. Controller status now fully
authenticates zero-model feature-result recovery across the prepared-branch and
session checkpoints plus the run-scoped capability-prelaunch lineage, with two
focused Swift test filters, `swift build`, and `git diff --check` required
before one accepted commit.

Live retained-result recovery then passed
`PersistentDomainStoreTests`, `SessionLifecycleTests`, `swift build`, and
`git diff --check` without another model. It created accepted commit
`d1a2c00257a5c620109d61a5aacc891444440579`; deterministic milestone
integration produced `a265d0a49625581bc44b823def804d4f81161a8a` and the
integration-evidence commit `860769e`. F078 is now `integrated` with
`integration_status: passed`. Final consistency is `CONSISTENT`, both writer
leases are absent, and the clean application repository is stopped on
`codex/m0-foundation` at queue reconciliation without starting F079.

The existing focused capability, routing, runtime-audit, planning, recovery,
and execution-identity validation passes, plus all eleven feature-prelaunch
recovery tests. Compilation, configuration and queue validation, the global
model-policy validator, pinned-CLI capability proof, runtime audit, and
`git diff --check` pass.

Atomic checkpoint-tolerant planning finalization is implemented on
`codex/m1-24-atomic-planning-finalization` from exact parent
`95f30353424aee165616d62d4d1e9a1605ff6914`.

Planning recovery now treats zero or more `CheckpointRecorded` events as
transparent audit metadata after independently authenticating their
transaction/workflow, run/session, branch/HEAD, lease, mutation authority,
paths, retained fingerprint, selected feature, and model/child authority.
Checkpoint names and positions do not define a recovery protocol. A
checkpoint-bound normalization may distinguish source and final paths only
when their authorized union exactly covers the retained result.

`RECONCILED_READY_WORK` uses a matching explicit non-empty selected feature or
derives one only from a sole corroborated ready feature with complete
dependency evidence. New reconciliation validates the model-produced diff
semantically before deterministic execution-policy metadata is materialized,
so a semantic failure adds no controller mutation.

The live Interview Companion recovery dry-run authenticates run
`940611fd-48c5-49ab-b641-3fd3d6535cee`, session
`019fa19f-1e02-7523-a4ec-f81793723d06`, transaction
`c08f3fb3-ac92-4490-8fca-c19f777f3ced`, starting HEAD `12bee2fc`, and retained
diff `a53f0e984d93dd2220e3e2e6852c71dead900c79f7c26d8b9eb48536d2b529e3`.
It returns `recovery_ready`, derives F078, predicts `feature_ready`, one
`factory: reconcile M0 queue` commit, and zero model or child launches. Apply
was not run. Both application repositories and protected controller state
remain byte-for-byte unchanged.

One hundred one focused M1-021, M1-022, M1-023, and M1-024 regressions pass.
Python compilation, `validate-config`, JSON-compatible configuration and queue
parsing, and `git diff --check` pass. The complete controller suite and all
application builds/tests remain intentionally unexecuted.

Post-integration planning baseline binding is implemented on
`codex/m1-22-post-integration-planning-baseline-binding` from exact parent
`6d5210eefce3dad5c957787b4692ba4d0fa33683`.

A fresh `queue_reconciliation` plan now recognizes only a fully corroborated
completed integration with cleared feature, accepted-commit, transaction,
gate, lease, and resume identity. In that state the completed integration's
terminal milestone HEAD replaces the preserved historical
`selected_feature_starting_commit`. Active selected features continue to use
their authenticated feature starting commits, and malformed or incomplete
integration completion remains fail closed.

For Interview Companion, read-only status now binds both starting and
milestone branch to `codex/m0-foundation` and starting commit to
`12bee2fc3b3668091474dc288067cd3aced0ee08`. Read-only consistency returns
`CONSISTENT`, `planned_milestone_ref: true`, and no failed invariant. The
ledger, projection cache, absent cycle cache, application HEADs, branches,
worktrees, Git-operation state, and application writer-lease state remain
unchanged. Status and consistency launched zero models and zero children.

Eighty-six focused execution-plan, planner-observer, projection,
post-integration, M1-014, and M1-021 regressions pass. The complete controller
suite and application builds/tests remain intentionally unexecuted. The four
previously documented broader baseline failures were not rerun: two
mutable-live-ledger F097 cache fixtures and two legacy CLI expectations.

Committed planning-result recovery finalization is implemented on
`codex/m1-21-finalize-committed-planning-recovery` from exact parent
`2c6cdd368d2f4f4991c565f4de1840f9520e4069`.

Queue semantic comparison now uses the shared canonical warning evidence:
matching explicit warnings, counts, blocking status, validity, active
milestone, ready features, and inventory counts pass even when only the
structured result has descriptive `warnings_scope`. Explicit-list, count,
blocking, validity, selection, milestone, and inventory disagreements still
fail closed.

Committed planning recovery no longer treats the Git commit alone as durable
finalization. It authenticates the exact original transaction, run, session,
commit, queue fingerprint, selected feature, recovery-evidence fingerprint,
repository state, and completed parent integration. Successful apply appends
one deterministic recovery transaction, projects `feature_ready` with F073
current and starting at `8a2f7fb93bb7ec6f0eaf160e971aac517542802f`,
and leaves no active transaction, lease, or human gate. The completed F072
integration at sequences 757-759 is recognized as the valid parent of the
direct-child planning commit rather than an interrupted integration.

The exact live dry-run authenticated all eight planning paths, reused commit
`8a2f7fb93bb7ec6f0eaf160e971aac517542802f`, and predicted one deterministic
transition to `feature_ready`/F073 with zero application writes, commits,
models, or children. Live apply appended sequences 774-786 once under recovery
transaction `fd922458-662c-4111-b57e-81140ac78267`; a second exact apply
returned `planning_transaction_already_recovered`, performed no validations,
and left all runtime hashes and the 786-event ledger unchanged.
`verify-consistency` now returns `CONSISTENT` with no failed invariant.

One hundred eighty-six focused controller tests pass: 180 planning,
post-integration, projection, routing, policy, and recovery regressions plus
six exact checkpoint and Autopilot cases. Python compilation,
`validate-config`, CLI required-mode rejection, and `git diff --check` pass.
The complete controller suite and all application builds/tests remain
intentionally unexecuted. The four previously documented broader baseline
failures remain excluded: two mutable-live-ledger F097 cache fixtures and two
legacy CLI expectations.

Interview Companion remains clean on `codex/m0-foundation` at
`8a2f7fb93bb7ec6f0eaf160e971aac517542802f`; only its ignored
controller-owned compatibility cache was refreshed by the authorized recovery.
Case Manager remains clean on `codex/p0-foundation` at
`f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0` with byte-identical controller
cache. Neither application repository received a tracked file change, commit,
branch change, build, test, preparation, execution, reconciliation, or
integration.

Checkpoint-aware retained feature-result recovery is implemented on
`codex/m1-20-checkpoint-aware-feature-result-recovery` from exact parent
`693ef372ab5f8a875934c086d1a9ef091b532570`.

The original-transaction validator now accepts only the exact pre-M1-017
seven-event topology or the exact M1-017 eight-event topology containing one
`authenticated_feature_session_launched` checkpoint with
`branch_preparing` to `feature_in_progress` phases. Transaction, project,
repository, workflow, run, session, global ordering, and fingerprint-chain
lineage remain exact. Malformed, duplicate, misplaced, foreign, unrelated, or
broken-chain variants fail closed. F003 uses the same matcher as the general
F068/F070/F072/F097 path, and workflow alias normalization is unchanged.

Autopilot now fingerprints complete deterministic recovery evidence and
reloads projection after one failed preflight. If route, capability,
diagnostic, projection, gate, and repository evidence are unchanged, it stops
at `technical_recovery_required` without invoking the route twice,
quarantining the feature, or claiming bounded recovery exhaustion. Changed
evidence or a changed capability version permits a later bounded retry.

The live F072 consistency result is `CONSISTENT`; the 12-path dirty worktree is
authenticated as `feature_result_recovery`, execution-plan agreement passes,
and status prioritizes that route over human-decision resolution. The exact
write-free dry-run authenticates the M1-017 checkpoint, original run,
transaction, session, gate, branch, HEAD, all 12 paths, tracked fingerprint
`59d0c8b8af72054fef8ff207f33dc08a70f783b99c4c7c3006560b198d7c5e62`,
empty untracked set, and untracked fingerprint
`44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`.
It plans `swift test --filter DomainModelTests`, `swift build`, and
`git diff --check`, one possible direct-child commit, and
`integration_pending`, with zero models, children, writes, leases,
transactions, application commands, reconciliation, or integration. Live
apply remains unexecuted.

One hundred eighty-five distinct focused controller tests pass across
checkpoint-aware and legacy retained-result recovery, F003/F068/F070/F097
regressions, Autopilot, binary context, prelaunch recovery, execution-plan
identity, state transitions, execution-policy finalization, policy rebinding,
projection authority, and cache binding. A broader diagnostic run passed 210
of 214 tests; the four failures are pre-existing stale fixtures, including the
two already documented mutable-live-ledger F097 cache tests and two legacy CLI
expectations reproduced unchanged from the M1-020 starting commit. Python
compilation, configuration validation, JSON-compatible queue/config parsing,
CLI help and required-mode rejection, profile-aware content audit, and
`git diff --check` pass. The complete controller suite and all application
tests/builds remain intentionally unexecuted.

GPT-5.3-Codex application-feature routing is implemented on
`codex/m1-19-gpt-5-3-codex-application-routing` from exact parent
`c3969af5d0ec75514aaca02e7129a16c1ca64fda`.

The new explicit `application_feature_implementation` profile resolves to
exact `gpt-5.3-codex`/high with one parent and zero children. Existing
workflow fallbacks are unchanged: deterministic work remains zero-model,
queue reconciliation retains Luna/high, and controller repair retains
Sol/medium. Existing valid queue/specification policies remain authoritative
and are not rewritten implicitly.

The new zero-model `rebind-ready-feature-policy` command authenticates one
exact ready, selected, unstarted feature; branch and HEAD; matching old
queue/specification policy; absent transaction, session, lease, reservation,
Autopilot owner, and Git operation; clean repository; exact two-path metadata
boundary; and local target-model compatibility. Dry-run predicts replacement
bytes and the final Git binary-diff fingerprint through an isolated temporary
index. Apply uses one planning-writer transaction, validates inventory and
`git diff --check`, creates one direct-child metadata commit, refreshes
projection and compatibility caches, and stops at `feature_ready` without
preparation or execution.

The pinned Codex `0.145.0` executable currently fails exact compatibility:
its refreshed catalog exposes `gpt-5.3-codex-spark` but not
`gpt-5.3-codex`; its bundled catalog exposes neither. The live F072 rebind
therefore correctly reports `POLICY_REBIND_REJECTED` and `apply_allowed:
false`, while still showing the predicted new policy, exact two metadata
paths, final fingerprint
`9f01753e15f8d529d134e36657dd25870c7ac5b96b39f6507f7523d0bb0081ea`,
and zero model, child, lease, transaction, or write. A read-only feature-cycle
plan with the target profile selects F072 at exact starting commit
`b745d648f406ab5a2024d9ea07198c187024e135`, `gpt-5.3-codex`/high,
one parent, zero children, retains context fingerprint
`8ef60d7a3791583f261eb402b480cabdd9a1eb8b2c14fd1f3abf9b599a1239de`,
and excludes 100 generated paths before decoding. Compatibility blocks launch.
No live apply ran and F072 did not start.

Deterministic execution-policy finalization is implemented on
`codex/m1-18-deterministic-execution-policy-finalization` from exact parent
`3540447bffe01b43d5084a6b925c09e952723926`.

The existing missing-policy planning failure now has a fail-closed,
zero-session recovery path. It authenticates the terminal reconciliation,
recorded passing queue validation, repository/run/transaction/session
identities, exact branch and HEAD, retained path set and original binary diff,
sole ready selection, integrated dependencies, and absence of conflicting
writer, reservation, Autopilot, transaction, Git-operation, or untracked
state. It preserves explicit valid policy and otherwise uses the normal
`application_feature` resolver. F072 resolves to
`generic_or_architectural`, `gpt-5.6-sol`/medium, one parent, zero children,
source `workflow_fallback`.

Dry-run predicts canonical queue and feature-specification normalization and
the exact final binary diff through an isolated temporary Git index, without
writing the application. Apply remains intentionally unexecuted live; it
would recheck feature compatibility, atomically normalize only the two
already-retained metadata paths, rerun deterministic planning validation, and
create at most one direct-child planning commit before projecting
`feature_ready` with no current feature and F072 selected.

Ordinary future reconciliation materializes the same canonical policy before
finalization. The workflow kernel binds that controller normalization to the
session's original changed paths, normalization paths, and final fingerprint,
so this omission cannot recur without weakening mutation evidence.

The exact live dry-run authenticated all seven retained paths, original
fingerprint
`0bc3c1a1dfef738200c5cbc2ff457d26061ecf3cda4a11abb24f13d3e4cc4c9d`,
empty untracked set, F070 integrated dependency, F072 singleton selection,
and every ownership boundary. It predicts normalization only in
`docs/FEATURE_QUEUE.yaml` and
`docs/features/F072-job-application-detail-workspace.md`, with final
fingerprint
`0bdb9a52a52227d9e9e8eb1eeb2e53b37d4f16236f12b3ad32694a333b552c8e`.
Status proposes `planning_finalization`, not resume, with zero sessions and
models. Consistency authenticates the seven-file worktree and recovery plan;
its only remaining failed invariant is the intentionally stale cycle cache,
which apply would refresh.

Two hundred three focused controller tests pass across execution policy,
normal and retained planning finalization, Autopilot, projection/consistency,
cache binding, binary context, prepared-feature execution binding, and
retained-result recovery. Python compilation, configuration and
JSON-compatible queue validation, CLI help and required-mode rejection, and
`git diff --check` pass. The complete controller suite and all application
tests/builds remain intentionally unexecuted.

Retained feature terminal-state transition and deterministic F070 recovery are
implemented on `codex/m1-17-retained-feature-terminal-transition` from exact
parent `2ab5d29fb1439a663fad9668b02301b924cf39a2`.

`StateMachine.transition` raised the observed `InvalidTransition` when
`CycleEngine._transition_project` received `feature_preparing` from the
pre-dispatch compatibility document after the feature kernel had already
recorded authenticated `SessionLaunched` evidence. The session callback now
rebuilds the kernel projection, proves the active transaction is
`result_pending`, advances both cycle and compatibility state to
`feature_running`, and processes the terminal result from that authenticated
transaction.

Invalid structured output now returns one retained-result outcome with its
bound append-only human gate, emits `FEATURE_RESULT_RETAINED`, preserves the
application worktree, and stops cleanly. Exact F070 recovery additionally
authenticates completed preparation, the intervening zero-session prelaunch
recovery and its failed predecessor, the owning execution transaction and
session, queue-fingerprint ownership of `in_progress`, the exact terminal gate,
branch, HEAD, 20 tracked paths, empty untracked set, and retained fingerprint.
It does not infer acceptance from the invalid model envelope.

The live write-free dry-run authenticated transaction
`80fa16eb-ffd7-4acb-9433-77b4b201fb5c`, session
`019f9861-4ce9-79b3-9b5a-96e84d30a50f`, gate
`gate-9669520e53b48b832572caa5e2526e7e`, exact starting HEAD
`b22f89af7a03af1b26b768b640b680daa900dba9`, all 20 retained paths, and
tracked fingerprint
`37308e99f8977aedb88b94502009372198564f72508ea4674b47a13a77b192e1`.
It launched zero models and children, ran no application command, acquired no
lease, planned the three focused Swift test filters, `swift build`, and
`git diff --check`, and stopped before its one possible commit,
gate supersession, queue reconciliation, or integration. Apply remains
intentionally unexecuted and would finish at `integration_pending`.

Ninety-four distinct focused controller tests pass across retained-result
recovery, prelaunch recovery, Autopilot, projection/consistency, execution-plan
binding, state transitions, binary context, and retry exhaustion. The two
previously documented live-ledger-coupled F097 post-transition cache tests
remain excluded; F068 and F097 retained-result regressions pass. Compilation,
configuration, JSON-compatible queue parsing, CLI help/mode rejection, and
`git diff --check` pass. Live consistency is `CONSISTENT` with no failed
invariant, and status proposes `feature_result_recovery`.

Prepared-feature execution-plan binding is implemented on
`codex/m1-16-prepared-feature-plan-binding` from exact parent
`b19549cff80f416f4698db97d3ea33feb3820101`. Feature execution now carries an
explicit workflow-scoped starting branch. An authenticated completed
deterministic preparation binds the exact feature branch and feature starting
commit, while the milestone branch remains the distinct integration
destination even when both refs point to the same commit.

The exact F070 failure was raised by
`CycleEngine._validate_projected_dispatch`: it expected
`execution_plan.milestone_branch` and `execution_plan.starting_commit` but
observed the live repository branch and HEAD. The stale application-cache
`last_verified_git_state.branch` was a separate finalization defect, not the
raising authority. Prelaunch recovery now refreshes that checkpoint from the
preserved live feature checkout.

Focused prepared-feature, execution-plan, projection, context, Autopilot,
cache, retained-result, post-integration planning, and integration regressions
passed 213 tests. The two known F097 post-transition cache tests remain
excluded because they copy the mutable live ledger while asserting the old
sequence-508 topology; this is unchanged from M1-015. Compilation,
configuration, inventory/queue validation, CLI boundaries, content audit, and
`git diff --check` pass. Live read-only consistency is `CONSISTENT`, and the
execution plan now binds F070 to `codex/F070-applications-table` at
`b22f89af7a03af1b26b768b640b680daa900dba9` while retaining
`codex/m0-foundation` as the milestone destination.

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

Explicit capability selection and failed-resume recovery (`review`)

## Next boundary

Resume the existing F078 cycle with the exact feature capability allowlist.
Do not create another feature, merge a default branch, push, tag, publish,
deploy, or release.

## Validation scope

Validation is limited to focused capability, cost-policy, runtime-audit,
planning-finalization, post-integration, status, consistency, and prepared
feature regressions; Python compilation; configuration and queue validation;
pinned-CLI prompt-input proof; live read-only status; and `git diff --check`.
The complete controller suite is excluded.

<!-- FACTORY_INTEGRATION_STATUS_BEGIN -->
## Prepared feature

- Feature: M1-029
- Branch: `codex/m1-29-generic-retained-repair-recovery`
- Milestone branch: `codex/m1-generic-retained-repair-integration`
- Writer lease required before implementation.
<!-- FACTORY_INTEGRATION_STATUS_END -->
