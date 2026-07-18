# Development Conveyor

Development Conveyor is the standalone, portfolio-level controller for the user-wide Codex Development Factory. It coordinates registered application repositories through their existing factory skills and named agents. It does not contain application source and does not directly implement application features.

The normal human loop is intentionally small:

1. Start or schedule the Conveyor.
2. Review and approve a milestone merge after the milestone gate passes.

The controller selects dependency-ready work, resumes interrupted cycles, launches repository-scoped factory sessions, verifies their externally visible results, integrates accepted features into milestone branches, and stops at a verified milestone gate or documented human decision.

## Architecture

The standalone repository is authoritative. The global `$development-conveyor` skill and `development-conveyor` agent are thin launchers.

```text
Global skill / agent
        |
        v
scripts/conveyor -> config + registry -> scheduler
        |                                |
        v                                v
portfolio state                 one launch reservation/project
        |
        v
repository cycle engine -> ignored .factory/conveyor-state.json
        |
        +-> queue reconciliation session
        |      product-architect (planning-only) + $feature-inventory
        +-> $feature-factory session
        |      repository-explorer + feature-worker + test-engineer
        |      + adversarial-reviewer
        +-> $milestone-integrator session
        +-> $milestone-gate + release-auditor
```

The controller owns deterministic parsing, selection, state transitions, identity checks, Codex compatibility preflight, checkpointing, retry accounting, safety validation, redaction, reports, and scheduling. Judgment and application writes remain inside the existing repository-scoped skills and role-pinned agents.

## Codex compatibility preflight

Before an active feature cycle is created, `feature_running` is persisted, or a repository session is launched, the Conveyor resolves the exact Codex executable, parses its version, resolves the action's role-pinned model and reasoning from the global Factory policy, and validates that pair against `codex debug models`. Known missing, old, unsupported, or policy-invalid combinations stop at a human gate. The controller never silently lowers a model or reasoning effort.

Repository launches receive explicit `--model` and `model_reasoning_effort` arguments from the authoritative role policy. `scripts/conveyor doctor --project PROJECT-ID` reports the executable, detected and policy-minimum versions, model, reasoning, compatibility classification, remediation, and exact validation command without writing an application repository.

## State models

Portfolio project states:

```text
discover bootstrap_required baseline_required queue_reconciliation
feature_ready feature_running feature_review feature_accepted
integration_pending integration_ready integrating integration_validation
integration_blocked feature_integrated
milestone_gate milestone_ready_for_merge human_merge_approval next_milestone
human_decision_required repository_dirty validation_failed
architecture_decision_required destructive_change_required conveyor_error
paused disabled
```

Repository cycle phases:

```text
idle preflight feature_selected branch_preparing feature_in_progress
feature_review feature_repair feature_accepted integration_pending integration_ready integrating
integration_validation feature_integrated next_feature_selection milestone_gate
completed blocked human_decision_required failed
```

Every allowed edge is explicit in `state_machine.py`; unknown and invalid edges fail. Reapplying the current state is an idempotent no-op.

Portfolio project state is stored under `state/projects/<project-id>.json`. Active repository cycle state is stored under `<application-repository>/.factory/conveyor-state.json`. Milestone-integration runtime evidence is stored under `<application-repository>/.factory/runtime/milestone-integration/`; legacy `.git/factory-integration` records are read-only recovery evidence. Reads never create either directory, simultaneous disagreeing records stop, and new runtime writes require the controller to verify `.factory/runtime/` in local Git exclude first. State writes are atomic and schema-validated.

### Explicit human-decision resolution

A registered human gate is never cleared from repository cleanliness or an approval reason alone. A resolvable gate has a structured controller registration containing its stable gate ID, classification, original reason, expected repository identity and path fingerprint, milestone, branch, exact HEAD, approved next state, and gate-specific pinned evidence.

Use the existing `reconcile` surface:

```bash
scripts/conveyor reconcile \
  --project PROJECT-ID \
  --resolve-human-decision \
  --reason "Specific explicit approval and investigation basis"
```

Add `--dry-run` to execute every validator without writing controller state, an applied-resolution report, an audit event, an application file, a lock, a branch, or a commit. A supplied reason that is empty or matches sensitive-output redaction is rejected and is not persisted.

For a retained milestone-integration writer lease, validation requires the pinned repository identity, exact milestone branch and HEAD, clean worktree, no Git operation or repository cycle, no writer lease or foreign controller reservation, local-host proof that the retained PID is dead, a hash-pinned and structurally valid forced-release investigation record, a hash-pinned successful integration record, present accepted and integrated commits, and matching committed queue evidence. The derived recovery classification is `writer_lease_forced_release_recorded`; it is not trusted as a free-form string from command output.

An accepted resolution transitions only through `human_decision_required -> queue_reconciliation`. It clears the active gate, appends the exact original gate and resolution to immutable controller history, writes one deterministic report, and records one audit event. Repeating the exact gate-and-reason fingerprint reports `already_resolved` without duplicating the transition or history. A different reason or a later unrelated active gate is rejected and cannot be cleared by replay.

`already_resolved` is returned only after the deterministic report exactly matches persisted history and the single audit event matches the complete project, repository, gate, resolution, branch, commit, validation, and state-transition provenance. Missing or mismatched deterministic artifacts are repaired atomically under a controller reservation; normal event appends and healing share a controller-wide event-log lock so another project's concurrent append cannot be lost. After acquiring the repair reservation, the controller reloads state and rechecks the active gate and exact resolution identity before healing. Unsafe repair, a newer interleaved gate, a reservation race, or evidence change after reservation acquisition returns `resolution_rejected` with the failed validator and safe diagnostic action, never a raw exception or partial state transition.

## Repository writer ownership

Two related mechanisms prevent duplicate work without competing with the installed Factory:

- A controller launch reservation under `state/launch-locks/` prevents two Conveyor processes from launching production work for the same repository.
- The existing repository-shared `.factory/locks/writer.json` lease remains the only production writer lease. `$feature-factory` and `$milestone-integrator` acquire and release it with their existing deterministic scripts.

Read-only status remains available while either lock exists. A timestamp never proves staleness. Recovery requires matching repository identity, matching host, and proof that the recorded process is dead; ambiguous ownership becomes a human gate and the lock is preserved.

## Feature cycle

For one selected feature, the execution engine:

1. Verifies exact repository identity, clean state, queue validity, active milestone, baseline, milestone branch, dependencies, active cycles, and locks.
2. Selects a unique `accepted` or `integration_pending` feature before any new ready feature. Only when no integration candidate exists does it select ready work by numeric/P-level priority, dependency depth, queue order, then feature ID.
3. Resolves a null queue integration base to the verified milestone HEAD, validates repository identity, clean Git state, queue fingerprint, selected feature, dependency evidence, feature starting commit, and milestone pre-integration commit, then persists the preflight, selection, and branch-preparation checkpoints.
4. Launches a repository-scoped `$feature-factory` session with the exact project, milestone, feature, run identity, mode, and prohibitions.
5. Requires the installed role-pinned exploration, implementation, test, and adversarial-review workflow.
6. Resolves accepted commit evidence; completed legacy `commit: SELF` values use the corroborated registration, queue-at-commit, milestone ancestry, specification, status, and run-log policy documented in `docs/QUEUE_RECONCILIATION_CONTRACT.md`.
7. Launches `$milestone-integrator` when acceptance exists without integration.
8. Verifies the accepted and integrated commits are patch-equivalent, the integrated commit is on the milestone branch, queue integration evidence passed, and the repository is clean.
9. Recalculates readiness and continues according to the requested mode.

An isolated feature-branch pass is never treated as completion.

## Queue reconciliation

No ready feature is classified as planning refinement, a complete milestone, a legitimate blocker, or a human decision from deterministic evidence. When repository reconciliation is required, its session first invokes `product-architect` in planning-only mode, then uses `$feature-inventory` for evidence-supported queue or feature-specification edits. The session must return the structured contract in `docs/QUEUE_RECONCILIATION_CONTRACT.md`; a valid no-ready result succeeds at the controller level and stops at a safe planning checkpoint.

Accepted or integrated work is never reimplemented. The Case Manager registration explicitly preserves P0-002 and commit `4c43aa5cd870ddb4962eceb1fbe35c648efa3e18`.

Milestone integration has its own terminal contract. The final nonblank line of the terminal assistant result must contain exactly one of `INTEGRATED`, `VALIDATION_FAILED`, `HUMAN_DECISION_REQUIRED`, `SEMANTIC_CONFLICT`, `RETRYABLE_INTEGRATION_FAILURE`, or `TERMINAL_INTEGRATION_FAILURE`. Prompt/user/tool echoes, earlier assistant messages, missing markers, trailing text, and duplicates cannot authorize success. A live human gate additionally requires one validated `CONVEYOR_INTEGRATION_GATE=` descriptor; the one pinned legacy Case Manager report is the only descriptor-free recovery exception. A valid human gate is persisted as `integration_blocked -> human_decision_required`, releases its owned integration lease, and refuses ordinary resume.

## Interruption and resume

Startup reconciliation compares portfolio state, repository cycle state, writer and controller locks, identity and path fingerprint, HEAD, branch, Git operations, queue evidence, selected ready work, accepted/integrated commits, and timestamped milestone-gate evidence. Matching evidence resumes from the last verified checkpoint. A safely repairable stale conclusion is checkpointed through `queue_reconciliation` before scheduling; material disagreement produces a human-decision report with safe options and an exact resume command.

A pre-launch `branch_preparing` checkpoint is treated as an orphan rather than resumable work only when there is no session ID, lock, branch change, Git operation, or repository mutation and the recorded feature and starting commit still match deterministic selection. A deterministic failed session is likewise treated as preserved historical evidence only when the report's terminal structured event proves the configuration failure and Git, branch, worktree, lock, queue, selection, and cycle evidence prove that no repository work occurred. After compatibility remediation is verified, the portfolio transitions through `human_decision_required` and `queue_reconciliation` to `feature_ready`; the old thread is never resumed, and the next real run creates a new linked session. Portfolio-state recovery leaves the historical repository cycle and reports untouched. Immediately before a later new session claims the repository-local active-cycle slot, the controller verifies the old cycle fingerprint and atomically archives its exact bytes plus hash under the old run's report directory. A previously passed `milestone_ready_for_merge` gate is reopened only when timestamped gate inputs changed and `.factory/project.yaml` explicitly permits rerunning milestone gates.

Resume never resets, cleans, stashes, force-checks out, abandons a conflict, duplicates a branch, creates a second accepted commit, or repeats integration.

## Retry policy

Repository autonomy limits take precedence. The global defaults are three implementation repair attempts, three integration repair attempts, two additional adversarial-review passes, and one queue-reconciliation repair attempt. A retry occurs only when the failure is classified retryable and the result supplies a new hypothesis, supporting evidence, and materially different remediation. Normalized classification, command, session, model, reasoning, and remediation signatures reject repetition; timestamp and generated-report differences do not create a new hypothesis. CLI, model, reasoning, and policy incompatibilities are deterministic and receive zero automatic retries.

Product decisions, destructive migrations, data loss, semantic conflicts, major architecture replacement, security/privacy policy changes, new paid production dependencies, out-of-scope changes, user-data workspace access, and exhausted budgets stop at a human gate.

## Safety

Controller commands use argument arrays with no implicit shell. Git inspection is allowlisted. The controller rejects push, tag, merge, reset, clean, stash, rebase, force/discard checkout, unapproved branch deletion, release, publication, deployment, notarization, operation outside a registered repository, and unsafe Codex launcher flags.

Repository-scoped prompts repeat the same prohibitions and inherit the global/repository factory policies. The controller never merges into `main`, pushes, tags, publishes, deploys, releases, notarizes, or begins another milestone before a verified merge and policy authorization.

Sensitive subprocess output is redacted before persistence. Access tokens, API keys, authorization headers, cookies, private keys, embedded credentials, sensitive query parameters, secret fields, and user-data workspace paths have deterministic redaction coverage. Raw subprocess output is not stored.

## Registered projects

| Project | Repository | Active milestone | Current registration | Next safe action |
| --- | --- | --- | --- | --- |
| `case-manager` | `${HOME}/Developer/conan-case-manager` | `P0` | Startup reconciliation preserves P0-002 and selects P0-001 | `P0` resolves to queue milestone `phase-0`; P0-002 remains completed and P0-001 is the deterministic next feature. |
| `interview-companion` | `${HOME}/Developer/Live_Interview_Companion` | `M0` | `human_decision_required` | Preserve and reconcile the retained integration writer lease before scheduling. F002 metadata was independently verified. |

Registration is independent: no Case Manager milestone, branch, feature, or commit is copied into Interview Companion.

## CLI

From `${HOME}/Developer/development-conveyor`:

```bash
scripts/conveyor validate-config
scripts/conveyor doctor
scripts/conveyor doctor --project case-manager
scripts/conveyor status
scripts/conveyor status --project case-manager
scripts/conveyor plan --project case-manager
scripts/conveyor reconcile --project case-manager --dry-run
scripts/conveyor reconcile --project PROJECT-ID --resolve-human-decision --reason "Specific approval" --dry-run
scripts/conveyor run --project case-manager --mode one_feature
scripts/conveyor run --project case-manager --mode milestone
scripts/conveyor resume --project case-manager
scripts/conveyor run --mode portfolio
```

Add `--dry-run` to `run` or `resume` to prevent application writes and session launches. `status` and `plan` are always read-only. Dry-run output reports `persisted_state`, `derived_state`, `state_consistency`, `repair_transition_path`, `execution_state_path`, and `would_persist_state_repair`. `reconcile --dry-run` validates the exact read-only reconciliation session plan; `reconcile` without that flag updates only Conveyor-owned project state from deterministic evidence.

Human-resolution output includes `resolution_accepted`, `resolution_rejected`, or `already_resolved`; the deterministic resolution ID and fingerprint; original gate; expected identity, milestone, branch, and HEAD; every validator and result; previous and approved next state; transition path; application/write flags; report and audit locations; and the next safe action. Rejections exit nonzero. Resolution does not launch Product Architect, Feature Inventory, Feature Factory, or Milestone Integrator. A planning-baseline integration approval durably enters `integration_ready`; the next normal run launches a fresh role-pinned Milestone Integrator session, while other gate types continue through queue reconciliation.

The exact pilot dry run is:

```bash
scripts/conveyor run --project case-manager --mode milestone --dry-run
```

It must resolve `docs/FEATURE_QUEUE.yaml`, match configured `P0` to queue milestone `phase-0`, report the actual feature count, preserve completed P0-002, select ready P0-001, and show `feature_ready` before `feature_running` in the proposed execution path.

## Goal Mode

Goal Mode was verified enabled on 2026-07-17 through `codex features list` (`goals stable true`). If a future validation reports it disabled, the installed CLI's safe enable command is:

```bash
codex features enable goals
```

Use this invocation exactly:

```text
/goal Use $development-conveyor to complete the active milestone without stopping until the milestone gate passes or a documented human-decision condition is reached.
```

## Scheduled execution

Use this exact task prompt:

```text
Use $development-conveyor to resume or begin the active repository cycle.

Continue through dependency-ready features in the active milestone.

Stop only at milestone completion or a documented human-decision condition.

Never merge, push, publish, deploy, tag, release, notarize, or begin another milestone.
```

Every scheduled run checks for an active cycle and writer lock before selecting new work, persists its state and stop reason, and keeps one blocked project from starving another healthy registered project.

## Validation

Run:

```bash
python3 -m unittest discover -s tests -v
pytest
scripts/conveyor doctor
scripts/conveyor doctor --project case-manager
scripts/conveyor validate-config
scripts/conveyor status
scripts/conveyor reconcile --project case-manager --dry-run
scripts/conveyor run --project case-manager --mode milestone --dry-run
```

Tests use disposable synthetic Git repositories. They cover both state machines, configuration and path expansion, safe queue-location resolution, precise malformed-queue diagnostics, milestone/status/dependency normalization, nonempty queue preservation, corroborated and rejected `SELF` cases, every reconciliation classification, exit/result disagreement, report persistence, launch-lock cleanup, idempotent retry, stable repository identity, writer contention and identity-checked stale recovery, multi-project concurrency, output redaction, prohibited actions, accepted-feature integration, milestone gating, idempotent resume, and protection of `main`.

## Human gates

The Conveyor always stops for milestone merge approval. It also stops for ambiguous state or lock ownership, product/architecture decisions, semantic conflicts, destructive changes, data loss, policy changes, paid dependencies, out-of-scope work, hard content gates, and retry exhaustion. Default-branch integration remains an explicit human-authorized workflow outside the Conveyor.
