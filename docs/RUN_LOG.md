# Development Conveyor run log

This log records model configuration and deterministic deployment evidence. It never contains prompts, hidden reasoning, credentials, authentication metadata, tokens, or raw sensitive output.

### Model execution — 2026-07-17T08:54:36+00:00

- Agent role: `principal-platform-architect`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `direct_user_wide_controller_implementation`
- Safety and autonomy contracts unchanged: `true`

### Milestone-integration recovery contract repair — 2026-07-18

- Terminal contract: integration now requires exactly one authoritative terminal assistant classification and persists a valid zero-exit human gate instead of treating process success as integration success.
- State and routing: accepted or integration-pending work precedes new ready work; `integration_ready`, `integrating`, `integration_validation`, `integration_blocked`, and `feature_integrated` remain distinct from feature implementation state; planning-baseline approval launches no session and routes the next normal action to the role-pinned Milestone Integrator.
- Runtime and lease safety: new atomic runtime evidence is repository/project/run/milestone/feature bound under `.factory/runtime/milestone-integration`; legacy Git runtime is read-only; unignored or disagreeing runtime state stops; terminal evidence is written before release of only the owned integration lease.
- History provenance: commits beyond the recorded milestone validation point are structurally classified, and the Phase 0 planning baseline requires direct ancestry, exact milestone head and subject, planning-only paths, cross-document inventory agreement, validator success, cycle/base agreement, a canonical evidence fingerprint, and explicit bound approval.
- Isolation: the controller repair writes no Case Manager tracked file or Git ref. Historical-gate persistence is controller-only plus the local `.git/info/exclude` runtime pattern; integration and P0-003 remain prohibited until the explicit gate is resolved.
- Acceptance hardening: the final marker and structured blocker/retry descriptors are classification-specific; the legacy planning gate is exact-evidence pinned; planning inventory validation uses the candidate snapshot; applied approval requires a recomputed controller-owned resolution report; arbitrary ignored-cycle approval strings and arbitrary `factory:` queue rewrites are rejected; the integration lease precedes audit worktree creation; and partial resolution apply is replayable without launching a session.

### Model execution — 2026-07-17T08:54:36+00:00

- Agent role: `deterministic-validation`
- Effective model: `none (deterministic script)`
- Effective reasoning effort: `none`
- Configuration source: `deterministic_script`
- Event: `start`
- Reason code: `synthetic_repository_test_suite`
- Safety and autonomy contracts unchanged: `true`

### Deployment validation — 2026-07-17

- `python3 -m unittest discover -s tests -v`: 30 tests passed in disposable synthetic repositories.
- Both explicit state graphs were exercised edge-by-edge, including idempotent self-transitions and invalid-transition rejection.
- `scripts/conveyor validate-config`: PASS; two independent registrations; Goal Mode verified enabled.
- `scripts/conveyor run --project case-manager --mode milestone --dry-run`: PASS; next action `queue_reconciliation`; no application write.
- `scripts/conveyor run --mode portfolio --dry-run`: PASS; global concurrency `2`; no application write.
- Global skill: PASS using the skill-creator `quick_validate.py` validator with temporary, subsequently removed validator-only dependencies.
- Global agent: PASS; TOML parsed; model `gpt-5.6-terra`, reasoning `medium`, source `agent_file`.
- Application production repositories were not used by automated tests and were not modified by Conveyor setup or dry-run commands.

### Queue reconciliation repair — 2026-07-17

- Agent role: `principal-platform-architect`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `repair`
- Reason code: `case_manager_queue_reconciliation_contract`
- Scope: Development Conveyor queue discovery, parsing, corroborated `SELF` resolution, session result/exit handling, diagnostics, and controller-only recovery.
- Application boundary: Case Manager was inspected read-only; no application file, branch, commit, cycle state, or writer lease was modified.
- Deterministic coverage: 34 named reconciliation-contract tests plus the existing controller suite, using disposable synthetic repositories for all mutation cases.

### Adversarial review — 2026-07-17

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `review_and_correction`
- Initial findings: one High, four Medium, one Low.
- Corrected blocking findings: report paths now reject unsafe run IDs and prove confinement; controller-only recovery requires clean committed queue evidence on the configured milestone branch; `validation_failed` can recover to `feature_ready`; structured results come only from one marker on the terminal assistant-message line; specification paths use the explicit registered root and cannot escape through absolute, relative, symlink, queue-location, or repository-name ambiguity.
- Remaining Low: the controller repository has not undergone a Development Factory profile/bootstrap migration. This repair is a direct repository task rather than a feature or milestone Factory run; no factory lifecycle was started.

### Repair validation — 2026-07-17

- `python -m compileall src scripts`: passed in an isolated test virtual environment.
- `python3 -m unittest discover -s tests -v`: 65 tests passed directly from the source checkout.
- `pytest`: 65 tests passed in the isolated test virtual environment.
- `scripts/conveyor validate-config`: passed; both registered queues resolved and validated.
- `scripts/conveyor reconcile --project case-manager --dry-run`: `milestone_complete`, one P0 feature, no application write, read-only session plan.
- `scripts/conveyor run --project case-manager --mode milestone --dry-run`: P0 matched `phase-0`; P0-002 completed at `4c43aa5cd870ddb4962eceb1fbe35c648efa3e18`; no selected feature; next action `milestone_gate`.
- Controller-only state recovery moved Case Manager from the failed pilot state to `milestone_gate`; no application cycle or writer lock was created.
- Adversarial re-review: no Critical, High, or Medium findings remain.

### Model execution — 2026-07-17T20:26:16+00:00

- Agent role: `deterministic-validation`
- Effective model: `none (deterministic script)`
- Effective reasoning effort: `none`
- Configuration source: `deterministic_script`
- Event: `start`
- Reason code: `stale_state_synthetic_test_suite`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-17T20:26:39+00:00

- Agent role: `controller-repair-writer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `stale_state_reconciliation_repair`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-17T20:51:09+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `stale_state_reconciliation_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### Stale-state reconciliation repair — 2026-07-17

- Root cause: startup trusted a persisted `milestone_gate` state after repository evidence changed, so execution attempted an invalid direct `milestone_gate -> feature_running` transition even though milestone P0 was incomplete and P0-001 was dependency-ready.
- Repair: startup now derives controller state from queue, milestone, Git, cycle, gate-provenance, and lock evidence; stale execution state is repaired only through `queue_reconciliation`, with the persisted path `milestone_gate -> queue_reconciliation -> feature_ready` before any feature launch.
- Dry-run diagnostics now report persisted state, derived state, consistency classification, proposed repair path, execution path, and whether execution would persist the repair without mutating state.
- Reservation coverage now includes reconciliation, feature launch/result processing, resume, and milestone-gate launch/finalization, with under-lock revalidation of Git, queue, selection, cycle fingerprint, worktrees, branches, and writer-lock evidence.
- Synthetic validation: `python3 -m unittest discover -s tests -q` passed all 89 tests; isolated-environment `pytest` passed all 89 tests in 47.98 seconds.
- Compile validation: `python3 -m compileall src scripts` passed. The exact `python -m compileall src scripts` command was unavailable on this host because no `python` executable is installed.
- Configuration validation: `scripts/conveyor validate-config` passed with Goal Mode enabled and two registered projects.
- Live controller recovery: Case Manager moved from `milestone_gate` to `feature_ready` through `queue_reconciliation`; P0-001 remains selected, P0-002 remains completed, P0 remains incomplete, and no feature session was launched.
- Application isolation: exact before/after comparison of Case Manager tracked-file hashes, refs, branch/HEAD, cycle bytes, and writer-lock absence matched; no Case Manager file, branch, commit, cycle record, or lock changed.
- Adversarial re-review: no Critical, High, or Medium findings remain.

### Model execution — 2026-07-18T01:18:41+00:00

- Agent role: `controller-repair-writer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `cli_compatibility_recovery_repair`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T01:18:41+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `cli_compatibility_recovery_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### CLI compatibility and failed-cycle recovery — 2026-07-17

- Root cause: the failed P0-001 repository session reached a terminal `turn.failed` event because `gpt-5.6-sol` required a newer Codex CLI. Optional MCP authentication and model-cache warnings were secondary diagnostics, not the primary cause.
- Version evidence: the failed run did not persist `codex --version`; the retained pre-upgrade executable `/Users/dany/.codex/packages/standalone/releases/0.137.0-aarch64-apple-darwin/bin/codex` reconstructs the old version as `0.137.0`. The exact current Conveyor executable is `/Users/dany/.codex/packages/standalone/releases/0.144.5-aarch64-apple-darwin/bin/codex`, version `0.144.5`.
- Model policy: feature orchestration remains explicitly pinned to `gpt-5.6-sol` with `high` reasoning from the `feature-factory-orchestrator` agent policy. No model or reasoning fallback was introduced.
- Preflight: `scripts/conveyor doctor` and `scripts/conveyor doctor --project case-manager` both classified the current exact executable, model, and reasoning combination as `compatible` before recovery.
- Failure and retry disposition: run `555d458b-b52d-43ee-8aa2-dde097df4c77`, session `019f7264-c6e8-7591-b79f-253cb162cc65`, and its three focused retries are preserved as an exhausted, non-retryable `cli_upgrade_required` cycle. The old session will not resume.
- Repair: session plans now record and pass the exact executable, model, reasoning, and compatibility result; deterministic compatibility failures receive no automatic retry; repository-scoped retries require a validated allowlisted failure contract, a changed hypothesis, supporting evidence, and materially different remediation; exhaustion is explicit.
- Cycle safety: a null feature integration base resolves to verified milestone HEAD, launch invariants are validated before `feature_running`, action-specific session IDs prevent cross-role resume, and a replaced historical cycle is archived byte-for-byte with a SHA-256 record before the active-cycle slot changes.
- Live controller recovery: Case Manager moved from stale `feature_running` through the verified recovery path to `feature_ready`. P0-001 remains selected and its required start is `826de2ab2c517e4bba53ed54f0cf2ddee50f38ef`. No repository session launched.
- Application isolation: before/after Case Manager tracked manifest `4a62a08d58b47e9daffc32d98d294dce4c12aeff249ddcfe48177c2a945b26c9`, refs `6ecf48727b3f481c045f7bdcc33131edc9f4fc7679eff5d6c230b6fb1c0a18c1`, cycle bytes `1b0fdf9177a2d36edcaa5b6b3d0e2f91d89b4ee69bdba369afb56784ba07d679`, branch `codex/p0-foundation`, HEAD, single worktree, clean status, and writer-lock absence matched exactly. Historical report hashes also matched.
- Validation: `python3 -m compileall -q src scripts`, all 117 unittest cases, all 117 pytest cases in an isolated temporary environment, `scripts/conveyor validate-config`, both doctor commands, current status, and the final resume dry-run passed.
- Final dry-run: state is consistent at `feature_ready`; P0-001 is selected; the next real feature cycle would start a new repository-scoped session, never resume the failed session, and the dry-run performed no application write, lock acquisition, or Git operation.
- Adversarial re-review: no Critical, High, or Medium findings remain after correcting action-specific recovery, retry-contract classification, retry exhaustion, immutable cycle archival, and authentication-diagnostic redaction.

### Model execution — 2026-07-18T05:43:04+00:00

- Agent role: `controller-repair-writer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `feature_branch_invariant_recovery`
- Safety and autonomy contracts unchanged: `true`

### Feature-branch invariant and dirty-cycle recovery — 2026-07-17

- Root cause: run `99c2f421-6b5a-4f8a-ae43-a5aefc12657e` persisted the expected P0-001 branch name but launched the repository session while the registered worktree was still on `codex/p0-foundation`. A zero process exit then advanced the checkpoint without an accepted commit, while the writer lease was no longer present.
- Launch repair: the controller now derives or validates the expected branch, creates or selects it at the verified feature starting commit, verifies the actual branch/worktree and unchanged milestone ref, persists both, and acquires a Feature-Factory-compatible writer lease before `feature_running` or session launch. The lease remains through accepted-commit verification.
- Completion repair: a zero exit is classified from Git, worktree, lease, and queue evidence. Integration is authorized only after `accepted_feature` proves exactly one accepted commit; branch, dirty-work, missing-commit, missing-queue, incomplete-result, and unsupported completion claims remain recoverable without integration.
- Continuation repair: a recovered normal session may resume only after branch/worktree verification and writer-lease reacquisition. Its prompt states that prior completion was uncorroborated, requires implementation, validation, adversarial review, queue/documentation updates, and exactly one accepted commit, and forbids milestone integration until controller verification.
- Synthetic validation: `python3 -m compileall src scripts`, `git diff --check`, all 135 unittest cases, and all 135 pytest cases passed. Pytest ran from a disposable temporary virtual environment because the host has no installed `pytest` command or module.
- Configuration and live dry-run validation: `scripts/conveyor validate-config`, `scripts/conveyor status --project case-manager`, and `scripts/conveyor resume --project case-manager --dry-run` passed. Before recovery the dry-run classified `branch_invariant_violated` with `uncommitted_feature_work`, required branch recovery and a future writer lock, and denied resume.
- Controlled Case Manager recovery: `recover-feature-branch` recorded the three dirty-file hashes, used only non-forced `git switch -c`, created `codex/p0-001-decide-and-record-the-per-case-persistence-authority` at `826de2ab2c517e4bba53ed54f0cf2ddee50f38ef`, preserved all three hashes and dirty entries byte-for-byte, and left `codex/p0-foundation` at the same commit. Report: `reports/99c2f421-6b5a-4f8a-ae43-a5aefc12657e/feature-branch-recovery.json`.
- Final live state: P0-001 remains `ready`; `accepted_feature_commit` remains null; the three original files remain modified; no Git operation or writer lease exists; no repository session launched; `branch_recovery_required` is false; `writer_lock_required_before_resume` is true; and `resume_allowed_after_lock` is true.

### Model execution — 2026-07-18T06:44:37+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `explicit_human_decision_resolution_repair`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T07:05:26+00:00

- Agent role: `test-engineer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `human_resolution_test_evidence`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T07:05:26+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `human_resolution_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T07:19:21+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `human_resolution_second_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### Explicit human-decision resolution repair — accepted 2026-07-18

- Root cause: the controller could record `human_decision_required` but exposed no supported interface for attaching explicit user approval to the exact recorded gate. Normal reconciliation therefore continued to stop after the retained F002 integration writer lease had been safely recovered and removed.
- CLI and contract: `scripts/conveyor reconcile --project PROJECT --resolve-human-decision --reason REASON [--dry-run]` binds the normalized non-sensitive reason to a deterministic project-and-gate fingerprint. Resolution requires the pinned repository identity, milestone, branch, exact HEAD, clean worktree, no Git operation, application cycle, writer lease, or foreign controller reservation, local dead-process proof, hash-pinned structured forced-release and integration records, successful integration validation, present accepted/integrated commits, and matching committed queue evidence.
- Safety and idempotency: apply revalidates under the controller reservation, transitions only `human_decision_required -> queue_reconciliation`, clears only the matching active gate, and preserves the exact original gate and resolution in immutable history. Exact replay verifies and, when safe, atomically repairs deterministic report/audit artifacts. Normal event appends and healing share the controller-wide event-log lock, and a newer interleaved gate or evidence change returns structured rejection without partial state mutation.
- Validation: all 169 unittest cases and all 169 pytest cases passed. The final adversarial re-review reported 0 Critical, 0 High, and 0 Medium findings.
- Live dry-run: all 29 validators passed; controller state, application repository, locks, branches, commits, reports intended to represent applied resolution, and application runtime state were not written.
- Applied resolution: `human-resolution-2afcf2ce9e4ad9478ddc3772` recorded explicit user approval and the state transition `human_decision_required -> queue_reconciliation`.
- Final controller state: `queue_reconciliation`; selected feature `null`; next action `queue_reconciliation`; the next repository sessions are planning-only `product-architect` and `$feature-inventory`.
- Application isolation: the exact Interview Companion before/after snapshot matched. No application file, branch, commit, writer lock, controller cycle, or repository-local state changed during resolution.
- Registration/runtime distinction: `config/projects.yaml` intentionally retains `current_state: human_decision_required` as the immutable registration seed and original-gate context. The schema-validated runtime project state under `state/projects/` is authoritative after the accepted resolution; the seed is not rewritten as operational state.

### Model execution — 2026-07-18T21:18:57+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `integration_recovery_contract_repair`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T22:13:31+00:00

- Agent role: `repository-explorer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `medium`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `integration_recovery_readonly_map`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T22:13:31+00:00

- Agent role: `milestone_integrator`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `integration_repair_readonly_review`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T22:13:31+00:00

- Agent role: `test-engineer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `integration_recovery_validation`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T22:13:32+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `integration_recovery_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-18T23:32:23+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `post_integration_finalization_repair`
- Safety and autonomy contracts unchanged: `true`

### Post-integration durable-success finalization repair — 2026-07-18

- Root cause: one failed, unconfigured repository-local diagnostic remained embedded in otherwise successful milestone-integration output. The controller persisted the terminal failure conclusion even though the later terminal result, runtime record, queue, commits, validation commands, final model-evidence HEAD, and clean repository state all corroborated durable integration success.
- Command contract: post-integration command evidence is explicitly classified as `required_validation`, `required_evidence_finalization`, `optional_diagnostic`, `status_observation`, or `unsupported_command`. Required failures remain authoritative; missing unconfigured diagnostics are retained as warning-only evidence.
- Durable precedence: success requires terminal `INTEGRATED`, a compatible zero process exit, exact runtime identity, unique and patch-equivalent accepted/integrated commits, integrated/passed queue evidence, milestone membership, passing configured and integration validation commands, blocker-free runtime completion, the exact direct-parent integration/evidence/model-evidence commit chain, final model-evidence HEAD, clean/no-operation Git state, no writer lease, and a completed repository cycle. Integration-fix commits are accepted only as an exact queue/runtime-corroborated pre-validation chain.
- Finalization: stale cycle failure fields are cleared without changing `last_successful_checkpoint`; prior failure evidence is appended to `integration_finalization_history`. The controller transitions legally from `validation_failed` to `queue_reconciliation`, clears its operational stop reason, preserves historical integration reports, and does not rewrite application queue metadata. Effective `last_validated_commit` reporting means the final validated milestone HEAD.
- Queue behavior: dependency-satisfied non-human proposed work routes to planning-only queue reconciliation. Future human-gated proposals do not create a current gate until their dependencies are complete. One-feature recovery launches neither Feature Factory nor Milestone Integrator; milestone mode may reconcile later.
- Adversarial corrections: required markers dominate mixed optional commands; missing required exit status fails closed; mutable runtime pointers cannot bless an unvalidated production commit; finalized historical cycles tolerate later planning-only documentation but reject later production changes; unsafe, symbolic-link, and non-regular report paths fail closed; explicit integration-fix chains require command evidence produced against the final fix commit while normal runtime records need no duplicated plan validation object; authoritative runtime validation requires explicit clean-worktree evidence; and terminal reports are bound to exact schema, project, run, action, resolved working directory, and a nonblank exact session identity.
- Synthetic validation: all 35 required post-integration scenarios passed in disposable repositories. Full `unittest` discovery passed all 247 tests. `python3 -m compileall -q src scripts tests`, `git diff --check`, and `scripts/conveyor validate-config` passed.
- Live read-only evidence: Case Manager status and resume dry-run classify `stale_state_repaired`, derive `queue_reconciliation`, report no human gate, preserve P0-003 as proposed, suppress Feature Factory and Milestone Integrator relaunch, and separate the unconfigured-validator and queue-metadata warnings. No live Case Manager file, runtime state, branch, ref, commit, or lease was modified by this implementation run.

### Applied Case Manager recovery and final acceptance — 2026-07-18

- Applied controller recovery outcome: `state_repaired`; the stale operational state transitioned through the legal path `validation_failed -> queue_reconciliation`. The resulting persisted and derived states are both `queue_reconciliation`, and status reports `state_consistent`.
- Application isolation: Case Manager tracked-file manifest `4646bede7346ca93458009e4e1480cc61b8e2aee645e6306d8ed7c6648711339`, refs manifest `e86d688dffd6e9415742f4607b9a57da309cdbda9eccde03e96021b899758cf8`, and historical integration report SHA-256 `3149ca093a4da7c8bf9ee731696c60d7608cb1fe290e783a927cd27be7383a06` were unchanged across two post-apply resume dry-runs. No Case Manager tracked file, ref, branch, commit, or historical report was rewritten by controller finalization.
- Repository cycle finalization: `current_phase=completed`; `last_successful_checkpoint=resumed_feature_cycle_complete` remained preserved; `integration_status=passed`; `failure_classification=null`; `retry_exhausted=false`; `next_safe_action=queue_reconciliation`; `stop_reason=null`; `human_decision_required=null`; and `integration_gate=null`. One `integration_finalization_history` record preserves the prior failure evidence. `milestone_post_integration_commit` is `0f43ad23a3820ccd3e27f4a6a475e42dc70957bc`.
- Preserved warnings: `optional_diagnostic:unconfigured_repository_validator_missing:exit=2` and `queue_last_validated_commit_does_not_match_final_validated_milestone_head`. Both remain non-authoritative warnings.
- Final live status: feature `P0-001` is `integrated`; integration is `passed`; terminal classification is `INTEGRATED`; all durable-success checks pass; no human decision is required; and no new repository session would launch. Accepted feature commit: `dc4fe99a562d253dfba6b8eac1d2b3c81fc49b19`. Integrated commit: `1108649b63052ed03946cd840517f0b611c867bc`. Validated integration tree: `cf13a821d40378c491131cec2df14e7306976d6e`. Integration evidence commit: `0fc6b28301c532bd59c4a5d061d51061f9f4e1f2`. Final model-evidence and effective validated milestone HEAD: `0f43ad23a3820ccd3e27f4a6a475e42dc70957bc`.
- Idempotency: two consecutive `scripts/conveyor resume --project case-manager --dry-run` results were identical: current and derived state `queue_reconciliation`, next action `queue_reconciliation`, `would_persist_state_repair=false`, `new_session_would_launch=false`, and `human_decision_required=false`. The cycle SHA-256 remained `83d15273d83c742e67c6bbc5545cf481231bcfc94da579aa779823b8b6bc4ffb`.
- Final-tree validation: the focused post-integration suite passed `35/35`; full `python3 -m unittest discover -s tests -v` passed `247/247`; final-tree `python3 -m pytest` passed `247/247` in `187.93s` using a temporary isolated environment; and `python3 -m compileall -q src scripts tests`, `scripts/conveyor validate-config`, and `git diff --check` passed.
- Model execution evidence: `feature-worker` used `gpt-5.6-sol`, reasoning `high`, source `agent_file`; `test-engineer` used `gpt-5.6-terra`, reasoning `high`, source `agent_file`; and `adversarial-reviewer` used `gpt-5.6-sol`, reasoning `xhigh`, source `agent_file`.
- Final adversarial acceptance: `0 Critical`, `0 High`, and `0 Medium` findings remain. No default-branch merge, push, tag, publication, deployment, release, or application production-code mutation occurred.

### Queue-reconciliation finalization repair — 2026-07-19

- Root cause: durable P0-001 integration success reused live repository cleanliness, writer-lease, and Git-operation observations after the integration phase had already ended. The successful P0 queue-reconciliation session then left its authorized seven-file metadata transaction uncommitted, so live planning dirtiness incorrectly changed the historical integration conclusion to validation failure.
- Phase isolation: completed integration now uses its terminal clean-worktree and released-lease observations while continuing to require durable report, runtime, queue, commit, ancestry, and validation agreement. Current planning state is reported independently and cannot rewrite the terminal integration report.
- Planning transaction: queue reconciliation captures its clean starting snapshot, acquires a planning-specific writer lease before session mutation, binds the returned session, validates exact paths/diff/queue/semantics/inventory/model evidence, and creates one focused Factory planning commit. Controller state records non-self-referential start, result, evidence, previous validated, and effective milestone commit fields.
- Recovery: `recover-planning` requires an explicit run, session, starting HEAD, diff fingerprint, and exact changed paths. Dry-run writes nothing; apply commits only validated planning files, stops at `feature_ready`, and supports duplicate and post-commit interruption recognition.
- Synthetic coverage: added planning lease, authorized and unauthorized path, semantic agreement, exact seven-file recovery, fingerprint/head/path/queue divergence, commit, cleanliness, idempotency, non-self-reference, status separation, and no-launch tests. Historical integration dirtiness coverage verifies that pending planning changes do not retroactively invalidate a finalized integration.
- Final validation: `python3 -m unittest discover -s tests -q` passed all 266 tests in 178.417 seconds. Pytest passed the same 266 tests using the existing local `/Users/dany/Documents/planhat-takehome/.venv/bin/python` environment; the system and bundled Python runtimes did not contain the optional `pytest` module. `python3 -m compileall src scripts`, `scripts/conveyor validate-config`, and `git diff --check` passed.
- Live recovery dry-run: run `f2a11cf5-c3af-4737-89c2-96b017155d97`, session `019f77e1-c551-7f00-9409-2fff9f6ee79b`, starting HEAD `0f43ad23a3820ccd3e27f4a6a475e42dc70957bc`, seven exact planning paths, and diff fingerprint `e1b6f21eaa8b2254bf3080d800945aa05ff388ad88387e94c445d11fda5c1c81` validated without writes. P0-003 remained the sole ready feature; Feature Factory and Milestone Integrator launch flags were false.
- Live recovery apply: created exactly one Case Manager planning commit, `f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0` (`factory: reconcile P0 queue and ready P0-003`), on `codex/p0-foundation`. The worktree is clean, no writer lease or Git operation exists, P0-001 remains historically `INTEGRATED`, P0-003 remains ready, and its starting commit is the planning result commit. Post-apply milestone dry-run projects only `$feature-factory`; it writes nothing and does not launch Milestone Integrator.

### Model execution — 2026-07-19T02:10:25+00:00

- Agent role: `default-interactive`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `queue_reconciliation_finalization_repair`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-20T08:42:00+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_transactional_workflow_kernel`
- Safety and autonomy contracts unchanged: `true`

### M1 transactional workflow kernel focused validation — 2026-07-20

- Cutover: all eight writable workflow types use `WorkflowKernel`; feature and integration commits are kernel-owned, feature acceptance is a distinct exact-commit transaction, and duplicate integration fails before mutation.
- Durable side effects: compatibility materialization records a deterministic pending descriptor before terminal evidence, replays after interruption while unacknowledged, and appends one acknowledgment after idempotent persistence. A real child-process death after human-resolution terminal evidence recovered and materialized without a duplicate terminal.
- Filesystem safety: mutation paths reject symbolic links, non-regular files, and unsafe hard-link counts before session launch and at finalization. Projection cache and recovered-lease archive paths are controller-confined and reject symbolic-link or hard-link targets without changing external sentinels.
- Lease identity: kernel revalidation exact-matches lease type and ID, repository identity/path/fingerprint, project, milestone, feature, branch, HEAD, run, current session, mutation policy, host, PID, and process-start evidence.
- Focused evidence: all 34 human-resolution tests passed. Seven lifecycle/security compatibility tests passed. Four security test methods passed across 14 adversarial subcases. The genuine 15-boundary cross-process recovery test passed in 44.172 seconds. The aggregate eight-workflow by fifteen-boundary simulation passed with zero failures and errors in 282.821 seconds.
- Acceptance status: final adversarial recheck and configured broad validation remain pending. No commit, integration, default-branch merge, push, tag, publication, deployment, or release was performed.

### M1 direct-coverage closure — 2026-07-20

- Starting-HEAD invariant: a same-branch external commit after validation is rejected before kernel staging. The test proves the advanced HEAD and empty staged diff remain unchanged, the ledger sequence does not advance, and neither `CommitFinalized` nor successful terminal evidence appears.
- Interruption projection: after a real feature-execution `after_commit` interruption and recovery, the rebuilt projection exactly matches the persisted cache and returned projection, has no active transaction, binds the ledger sequence and fingerprint, validates its own projection fingerprint, and retains the exact accepted and integrated commits in historical integration evidence.
- Prohibited actions: the common subprocess authority now offers scoped privacy-safe observation before validation. Focused evidence observes and rejects push, tag, deploy, publish, release, and notarize categories; other destructive Git operations remain rejected separately.
- Traceability: requirements 18, 20, 21, 36, and 57 now cite direct kernel, recovery, projection, and common-authority evidence.
- Focused validation: `python3 -m compileall -q src tests` and six selected unittest methods passed in 12.169 seconds. Broad configured gates remain deferred until the final adversarial coverage recheck.

### M1 final-blocker repair — 2026-07-20

- Canonical continuation: completed ledger projection is authoritative for status, planning, run, and resume. Post-migration Case Manager routes to a fresh kernel feature transaction for P0-003; Interview Companion routes to a fresh kernel integration transaction for F005. The writable legacy SessionRequest/direct-reconciliation resume body was removed, and terminal legacy adapter replay returns without invoking its mutation again.
- Recovery and migration: `CommitFinalized` accepted-commit evidence survives finalization-incomplete recovery; migration resolves only the configured active milestone and rejects multiple active integration candidates; exact stale-lease proof supports idempotent recovery at lease, terminal, release, and projection boundaries; and ledger reads repair only the exact complete-one-record-ahead JSONL versus durable-head crash shape.
- Filesystem and gate safety: authorized mutation paths are descriptor-opened and validated as confined, regular, non-symlink, single-link files before any content read. Human terminals require one safe unique gate ID with exact fingerprint-bound resolution evidence. Milestone gates authorize only explicit gate documentation paths and execute adapter and required-command arrays pinned before launch.
- Focused evidence: the five resume/gate compatibility methods passed in 18.188 seconds. The compact blocker set produced nine substantive passes in 30.522 seconds; its sole invocation error was an incorrect unittest class qualifier, and the exact ledger method passed immediately under the correct class in 0.039 seconds. `LedgerTests.test_legacy_terminal_replay_does_not_repeat_mutation` passed in 0.199 seconds.
- Status: M1-001 remains `in_progress`. Final adversarial recheck, configured broad validation, content audit, acceptance metadata, and the immutable feature commit remain pending. No commit, integration, default-branch merge, push, tag, publication, deployment, or release was performed.

### M1 final-review closure — 2026-07-20

- Resolvable gates: `WorkflowKernel.block` is the single authoritative normalization boundary for human terminals. It derives a deterministic unique gate ID and binds the exact transaction ID, project ID, repository identity, repository path fingerprint, workflow type, nonempty classification/reason, and `queue_reconciliation` approved continuation before ledger or compatibility-cache persistence.
- Exact dirty recovery: a recovery transaction may observe only the exact dirty baseline authorized by the target transaction's immutable path and diff fingerprints. Matching baseline dirt is zero recovery-owned mutation and is never staged or committed by recovery. Any branch, HEAD, queue, tracked-diff, untracked-file, or content/path drift after the recovery snapshot fails closed. Continuation reacquires the original typed lease with its exact last bound session identity.
- Command safety: adapter-pinned build, test, lint, package, and validation argument arrays pass through `SafetyPolicy.validate_configured_command` before process launch. The policy records the common privacy-safe observation, confines execution to the registered repository, rejects prohibited release/publication/deployment/notarization tokens, rejects shell `-c` indirection, and limits configured Git to read-only operations.
- Focused validation: five production methods passed in 13.263 seconds: production CLI clean/dirty/validated/failure recovery, unexpected dirty-baseline drift rejection, exact human-gate identity, prohibited configured-command pre-launch rejection, and milestone-gate command pinning/authority.
- Status: M1-001 remains `in_progress`; final reviewer confirmation and configured broad gates are pending. No commit, integration, merge, push, tag, publication, deployment, or release was performed.

### M1 exact-dirty recovery window closure — 2026-07-20

- Durable plan binding: exact-dirty inspection returns the immutable mutation-boundary paths and content fingerprint together with the original starting branch and HEAD. After stale-lease takeover, recovery revalidates all four values immediately before beginning or snapshotting its observation-only transaction; mismatch releases the preacquired recovery lease and records no recovery transaction, commit, or terminal.
- No boundary replacement: each transaction may append exactly one `ChangesDetected` event. Validation and finalization independently compare current paths and content fingerprint to that original durable boundary. Restored execution cannot append a replacement fingerprint or bless drift discovered after recovery.
- Focused evidence: production CLI clean, dirty, validated, and terminal-failure recovery plus pre- and post-observation drift negatives passed together as three methods in 15.604 seconds. `test_restored_validation_rejects_durable_boundary_drift_without_replacement` passed separately in 3.162 seconds and proves the original boundary remains singular with no recovered feature commit or terminal.
- Status: M1-001 remains `in_progress`; no commit, integration, merge, push, tag, publication, deployment, or release was performed.

### M1 clean-commit recovery scope correction — 2026-07-20

- Scope correction: observation-only dirty mode now requires both a `resume` recovery classification and a currently dirty worktree with durable exact paths and fingerprint. `commit_succeeded_before_evidence` may carry changed commit paths while its worktree is clean; it stays on the clean recovery path and is authorized only by exact commit, ancestry, patch, and accepted-commit evidence.
- Focused evidence: `test_integration_after_commit_recovery_preserves_exact_accepted_commit`, `test_interruption_rebuilds_exact_projection_and_cache_evidence`, the production CLI clean/dirty/validated/failure matrix, both pre/post-observation drift negatives, and the restored durable-boundary no-replacement test passed 6/6 in 51.685 seconds.
- Status: M1-001 remains `in_progress`; no commit, integration, merge, push, tag, publication, deployment, or release was performed.

### M1 clean takeover-window cleanup — 2026-07-20

- Clean baseline revalidation: `commit_succeeded_before_evidence` recovery now pins and rechecks the observed branch, HEAD, clean worktree, exact commit paths, and content or patch fingerprint after takeover and before a recovery transaction begins.
- Prestart cleanup: revalidation, kernel begin, typed lease acquisition, and snapshot capture are enclosed by one failure cleanup. If any step fails, a matching preacquired recovery lease is released even when no durable recovery transaction exists yet.
- Focused evidence: the prior six clean/dirty recovery closure tests plus `test_clean_commit_recovery_rejects_before_lease_drift_and_releases_lease` passed 7/7 in 58.485 seconds. The new negative mutates the worktree at `before_lease`, then proves rejection, no recovery commit or terminal, and no residual physical writer lease.
- Status: M1-001 remains `in_progress`; no commit, integration, merge, push, tag, publication, deployment, or release was performed.

### Model execution — 2026-07-20T16:59:19+00:00

- Agent role: `repository-explorer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `medium`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_repository_evidence_map`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-20T16:59:19+00:00

- Agent role: `test-engineer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_test_traceability_and_validation`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-20T16:59:19+00:00

- Agent role: `product-architect`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_architecture_contract_review`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-20T16:59:19+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_final_adversarial_acceptance`
- Safety and autonomy contracts unchanged: `true`

### M1-001 final acceptance evidence — 2026-07-20

- Acceptance: M1-001 is `accepted` on `codex/m1-transactional-workflow-kernel`; the queue uses `accepted_commit: SELF`, `integration_status: pending`, and `integrated_commit: null`. Milestone M1 remains `active`, its integration branch is unchanged, and its human gate remains required.
- Configured validation: `python3 -m compileall src scripts` passed. `python3 -m unittest discover -s tests -q` passed `357/357` in `982.076s`. `python3 -m pytest` passed `357/357` in `1045.87s` in a disposable isolated environment. `scripts/conveyor validate-config` passed with two valid registered projects. The temporary pytest environment was moved to Trash after validation.
- Lifecycle simulation: the twenty-feature milestone completed with 20 unique accepted commits and 20 integrations, no duplicate commit or integration, no conflicting terminal, stale lease, prohibited Git command, push, release, deployment, tag, remote, or default-branch movement. The complete eight-workflow by fifteen-boundary matrix passed all 120 cases with one terminal per transaction, no stale lease, no duplicate commit or integration, no conflicting terminal, and an unchanged default branch.
- Real consistency: Case Manager and Interview Companion each returned `RECOVERABLE_INCONSISTENCY` solely because the canonical ledger has not yet been imported (`ledger_integrity`; migration required). Every other invariant passed, including Case Manager's configured `P0` alias resolving to queue milestone `phase-0` for projected feature P0-003.
- Exact Case Manager projection: `current_state=feature_ready`, `current_feature=P0-003`, `allowed_next_action=feature_cycle`, `milestone=phase-0`, `milestone_branch=codex/p0-foundation`, `selected_feature_starting_commit=f85f7dad2d1e1318bf36f277b5bd832b3cbf11a0`, and `old_session_resume=false`.
- Exact Interview Companion projection: `current_state=integration_ready`, `current_feature=F005`, `accepted_feature_commit=a1f2c8dd47aaa68580cd7dfc3dc6923e04469857`, `allowed_next_action=milestone_integration`, `feature_branch=codex/F005-persistent-data-store`, `milestone_branch=codex/m0-foundation`, `selected_feature_starting_commit=e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7`, `old_session_resume=false`, and `session_resume_eligible=false`.
- Migration safety: both application migrations were dry-run plans only. Neither was applied; both reported `application_repository_written=false` and no application Git mutations.
- Application isolation: the final pre/post snapshots for both registered applications matched exactly across HEAD, tree, refs, status, worktrees, index, writer and index locks, active Git operations, and the absence of controller ledger and projection-cache files. No application repository file, ref, branch, commit, staging semantic, worktree status, lock, controller ledger, or controller cache was changed by acceptance validation.
- Transparency: earlier in the run, ordinary read-only `git status` refreshed `.git/index` stat-cache bytes relative to the user's starting snapshot. No tracked content, refs, staging semantics, worktree status, or application behavior changed.
- Content audit: the deterministic profile-aware audit initially flagged a token-shaped redaction fixture. The fixture was replaced with a non-credential Authorization-header placeholder while preserving the test's purpose; the focused unittest and pytest checks passed, and the final audit reported no secret, sensitive-data, binary, destructive-operation, or other hard gate.
- Final adversarial acceptance: `0 Critical`, `0 High`, and `0 Medium` findings remain. No commit, milestone integration, default-branch merge, push, force-push, tag, publication, deployment, release, or application production-code mutation was performed.

### Model execution — 2026-07-21T19:32:08+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_projection_routing_adversarial_review`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-21T19:34:45+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_projection_routing_repair`
- Safety and autonomy contracts unchanged: `true`

### M1 milestone-integration result-contract repair — 2026-07-21

- Root cause: the milestone-integration prompt explicitly required read-only preparation, prohibited the actual cherry-pick and evidence workflow, and instructed the model to emit `INTEGRATED` with the unchanged milestone commit and no changed paths.
- Prompt contract: the session now receives the exact `integrate-feature.sh` command, transaction/run/session/commit identities, an exact JSON Schema and bound valid example, classification-specific evidence requirements, prohibited false-success combinations, and only focused AGENTS/adapter/queue/spec/status context.
- Semantic checks: a new integration cannot succeed with an unchanged milestone HEAD or empty changed paths. Success also requires the planned accepted commit or patch-equivalent in milestone history, exact command observation, complete runtime evidence, passing configured validation, integrated queue and milestone evidence, a clean repository, no Git operation, and released workflow evidence.
- Diagnostics: rejected structured results retain `terminal_marker_found`, `parsed_structured_result`, `structured_output_errors`, and `failed_semantic_checks` in the redacted controller report.
- Isolation: focused tests use disposable synthetic repositories and no model calls. The real F005 integration is prohibited during this repair; neither registered application repository is intentionally mutated.

### Model execution — 2026-07-21T22:54:33+00:00

- Agent role: `primary-agent`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `milestone_integration_result_contract_repair`
- Safety and autonomy contracts unchanged: `true`

### M1 milestone-integration result-contract repair validation — 2026-07-21

- Focused tests: `test_projection_authority.py` passed 17/17 in 35.897 seconds, `test_integration_gate_lifecycle.py` passed 4/4 in 12.928 seconds, and `test_milestone_integration_result_contract.py` passed 6/6 in 0.807 seconds (27/27 total). Tests used disposable synthetic repositories and made no model calls.
- Static and adapter checks: `python3 -m compileall src scripts` passed. `scripts/conveyor validate-config` returned `valid: true` for both registered projects.
- Live read-only consistency: Case Manager and Interview Companion both returned `CONSISTENT`, with no failed invariants and `application_repository_written: false`. The planner-facing fresh recovery projection is accepted only when its project and immutable ledger identity match, its fingerprint validates, and its feature branch and accepted commit are independently bound to the live repository.
- Recovery dry-run: Interview Companion plans a fresh F005 milestone-integration transaction from `e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7`, integrates accepted commit `a1f2c8dd47aaa68580cd7dfc3dc6923e04469857` from `codex/F005-persistent-data-store`, does not resume the terminal session, and exposes the exact mutation command and terminal JSON Schema.
- Scope: no full unittest suite or pytest run; no CLI upgrade, subagent, real F005 integration, application-repository mutation, push, tag, publish, deploy, or release.

### Model execution — 2026-07-21T23:57:27+00:00

- Agent role: `primary-agent`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `immutable_migration_fixture_followup`
- Safety and autonomy contracts unchanged: `true`

### Immutable migration-fixture follow-up — 2026-07-21

- Classification: `test_post_migration_real_fixtures_route_only_fresh_kernel_actions` had an incomplete test-controller execution seam. It could reach the production `SessionLauncher` even though its temporary controller had no prompt resources. The fixture now supplies a fail-closed fake launcher and asserts that neither prompt planning nor model launch occurs.
- Interview projection cause: `test_real_interview_dry_run_projection` read the mutable registered Interview Companion repository. Its live F005 queue state had changed to `ready`, so production migration correctly returned `feature_ready`; the assertion described a different scenario.
- Immutable scenario: the replacement fixture is entirely temporary and explicitly records F005 `integration_pending`, accepted commit `a1f2c8dd47aaa68580cd7dfc3dc6923e04469857`, milestone start `e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7`, the `codex/F005-persistent-data-store` feature ref identity, a terminal `VALIDATION_FAILED` integration transaction, a matching projection cache, no active transaction, no successful integration, and a non-resumable old session. Migration projects `integration_ready` with a fresh `milestone_integration` action.
- Targeted evidence: each regression passed independently in 2.050 and 0.587 seconds, then both passed together in 2.510 seconds. The existing focused suites passed 17/17 in 35.088 seconds, 4/4 in 12.848 seconds, and 6/6 in 0.844 seconds.
- Isolation: all new fixture repositories, queues, controller project state, ledgers, caches, refs, and terminal events live under `TemporaryDirectory`. No registered application repository or real controller `state/projects` input is read or mutated by these tests.

### M1-001 canonical-history reconciliation — 2026-07-21

- Immutable feature identity: `codex/m1-transactional-workflow-kernel` remains fixed at accepted commit `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`; no feature ref was moved and no commit was replayed.
- Exact integration ancestry: the accepted commit is an ancestor of canonical `codex/development-conveyor` head `96c5c4a85f54b183e7c6b798890b947bd1de782f`. The queue therefore records M1-001 as integrated with both accepted and integrated commit identity `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0`, and records the validated milestone head separately as `96c5c4a85f54b183e7c6b798890b947bd1de782f`.
- Post-integration history: `164d361c77a3b6adacdd7acf0fd934f2b6b6b045`, `c04a262dc796d5c34004d2287e24eb23e598cabd`, `01f3ddf0f724537013b45177431427f4eca2b256`, and `96c5c4a85f54b183e7c6b798890b947bd1de782f` are post-integration repair/finalization commits, not feature commits.
- Branch topology: milestone metadata identifies `codex/development-conveyor` as the canonical integration history. This reconciliation does not move the canonical branch, immutable feature branch, or any application ref.
- Writer coordination: the generic queue-bound writer preflight cannot represent an already-accepted post-integration repair. After exact branch, HEAD, cleanliness, Git-operation, and lease checks, agent run `m1-f005-two-ref-recovery-repair-20260721` acquired `.factory/locks/writer.json` atomically with `O_EXCL`; the lease remains held through implementation and review.

### Model execution — 2026-07-22T01:13:04+00:00

- Agent role: `repository-explorer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `medium`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_f005_two_ref_recovery_evidence_map`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-22T01:13:04+00:00

- Agent role: `product-architect`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_f005_two_ref_recovery_contract`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-22T01:13:04+00:00

- Agent role: `feature-worker`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_f005_two_ref_recovery_repair`
- Safety and autonomy contracts unchanged: `true`

### F005 two-ref failed-integration recovery repair — 2026-07-21

- Recovery authority: a terminal pre-mutation integration failure now resolves the accepted commit only from the unique accepted feature ref whose immutable queue snapshot self-identifies the feature as `integration_pending`, binds the exact branch and milestone integration base, retains `accepted_commit: SELF`, records pending integration, and has complete tests/review/documentation acceptance evidence.
- Independent refs: the configured milestone ref must equal the failed transaction's starting branch and HEAD. The accepted feature ref HEAD resolves `SELF` independently. The failed report corroborates transaction, session, repository, pre-mutation status, and accepted commit but cannot select the commit.
- Dispatch: the live milestone queue may record the recovered feature as `ready`. Projected integration admits only the verified recovered identity without `select_integration`, then revalidates both refs under the launch reservation before the kernel begins. The old terminal session remains non-resumable.
- Disposable integration evidence: a real synthetic two-ref topology with a live `ready` queue and accepted-ref `SELF` snapshot completed one fresh kernel integration through a fake launcher, preserved the immutable accepted ref, advanced only the disposable milestone ref, produced the combined accepted content, released its lease, and did not resume the old session.
- Negative evidence: accepted-feature and milestone-ref drift both failed before a new ledger event; incomplete snapshot acceptance failed to recover; status, milestone dry-run, and consistency appended no recovery event and did not change the disposable repository.
- Focused validation: `tests.test_projection_authority` passed 17/17, `tests.test_integration_gate_lifecycle` passed 4/4, `tests.test_milestone_integration_result_contract` passed 6/6, and `tests.test_two_ref_integration_recovery` passed 6/6, for 33/33 focused tests. Relevant `compileall` checks passed.
- Live read-only evidence: Interview Companion status and milestone dry-run select F005 as a fresh `milestone_integration` from milestone `e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7` and accepted feature ref `a1f2c8dd47aaa68580cd7dfc3dc6923e04469857`, with `old_session_resume=false`. Consistency returned `CONSISTENT` with no failed invariant. The application remained clean on `codex/m0-foundation`; both refs, HEAD, and the 21-line controller ledger SHA-256 remained unchanged. The real F005 integration was not run.

### F005 two-ref ancestry review correction — 2026-07-21

- Confirmed Medium finding: queue `integration_base_commit` agreement alone did not prove the accepted feature ref contained exactly one direct feature commit based on the verified milestone ref.
- Correction: recovery now reads the accepted ref HEAD's parent list and requires exactly one parent equal to the independently verified milestone ref HEAD. Merge commits, multi-commit feature histories, root commits, and unrelated-parent histories fail closed.
- Focused negatives: disposable multi-commit and unrelated-history feature refs each retained otherwise-valid SELF queue snapshots and matching reports, yet neither produced `integration_ready` nor appended a ledger event.
- Review disposition: the confirmed Medium finding is corrected; bounded recheck has no remaining Critical, High, or Medium finding.

### Model execution — 2026-07-22T01:40:51+00:00

- Agent role: `test-engineer`
- Effective model: `gpt-5.6-terra`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_f005_two_ref_ancestry_validation`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-22T01:40:51+00:00

- Agent role: `adversarial-reviewer`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `xhigh`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_f005_two_ref_bounded_medium_recheck`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-22T01:56:29+00:00

- Agent role: `milestone_integrator`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `high`
- Configuration source: `agent_file`
- Event: `start`
- Reason code: `m1_post_acceptance_integration_reconciliation`
- Safety and autonomy contracts unchanged: `true`

### M1 post-acceptance integration reconciliation evidence — 2026-07-21

- Classification: post-acceptance Factory integration evidence and finalization only; this record is not a feature commit and does not change M1-001 behavior, source, tests, queue state, or application state.
- Exact accepted and canonical identity: immutable accepted feature commit `6f97dcca8cb1c1e4b1560bda1b617f260995a9e0` is an exact ancestor of canonical `codex/development-conveyor` head `96c5c4a85f54b183e7c6b798890b947bd1de782f`. The separate repair branch remained at `f2494b41a2c42b18d71b39da91bc9aef657d4e0b` during validation; no ref was moved and no integration action or commit replay occurred.
- Deterministic discovery: `python3 ~/.agents/skills/milestone-integrator/scripts/discover-integration.py --root . --feature M1-001` stopped with the expected zero-candidate result, `expected exactly one accepted feature awaiting integration, found 0`; M1 has no pending accepted integration candidate.
- Combined-tree validation: the four focused suites passed 33/33 tests in `56.754s`; the relevant changed Python files compiled in memory; `scripts/conveyor validate-config` returned `valid: true` for both registered projects; `git diff --check`, worktree-diff, and index-diff checks were clean.
- Application isolation: Interview Companion remained clean on `codex/m0-foundation` at `e07d8803fbe56bbbfb7430aeb19e888f3d7d06a7`; its immutable F005 feature ref remained `a1f2c8dd47aaa68580cd7dfc3dc6923e04469857`. The controller ledger remained 21 lines with SHA-256 `87ec706e8df320e5f0203501e1e8179342246732d3a975890a9d6db4e3e9b520`. The real F005 integration remained unperformed.
- Safety boundary: canonical `codex/development-conveyor` remained fixed at `96c5c4a85f54b183e7c6b798890b947bd1de782f`; no default-branch merge, push, tag, release, deployment, publication, branch deletion, destructive reset, rebase, amend, application write, or ledger write was performed.
