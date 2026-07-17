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

The controller owns deterministic parsing, selection, state transitions, identity checks, checkpointing, retry accounting, safety validation, redaction, reports, and scheduling. Judgment and application writes remain inside the existing repository-scoped skills and role-pinned agents.

## State models

Portfolio project states:

```text
discover bootstrap_required baseline_required queue_reconciliation
feature_ready feature_running feature_review feature_accepted
milestone_gate milestone_ready_for_merge human_merge_approval next_milestone
human_decision_required repository_dirty validation_failed
architecture_decision_required destructive_change_required conveyor_error
paused disabled
```

Repository cycle phases:

```text
idle preflight feature_selected branch_preparing feature_in_progress
feature_review feature_repair feature_accepted integration_pending integrating
integration_validation feature_integrated next_feature_selection milestone_gate
completed blocked human_decision_required failed
```

Every allowed edge is explicit in `state_machine.py`; unknown and invalid edges fail. Reapplying the current state is an idempotent no-op.

Portfolio project state is stored under `state/projects/<project-id>.json`. Active repository cycle state is stored under `<application-repository>/.factory/conveyor-state.json`. State writes are atomic and schema-validated. Repository-local runtime paths are ignored through local Git metadata, not tracked application files.

## Repository writer ownership

Two related mechanisms prevent duplicate work without competing with the installed Factory:

- A controller launch reservation under `state/launch-locks/` prevents two Conveyor processes from launching production work for the same repository.
- The existing repository-shared `.factory/locks/writer.json` lease remains the only production writer lease. `$feature-factory` and `$milestone-integrator` acquire and release it with their existing deterministic scripts.

Read-only status remains available while either lock exists. A timestamp never proves staleness. Recovery requires matching repository identity, matching host, and proof that the recorded process is dead; ambiguous ownership becomes a human gate and the lock is preserved.

## Feature cycle

For one selected feature, the execution engine:

1. Verifies exact repository identity, clean state, queue validity, active milestone, baseline, milestone branch, dependencies, active cycles, and locks.
2. Selects exactly one ready feature by numeric/P-level priority, dependency depth, queue order, then feature ID.
3. Persists the preflight, selection, and branch-preparation checkpoints.
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

## Interruption and resume

Startup reconciliation compares portfolio state, repository cycle state, writer lock, identity and path fingerprint, HEAD, branch, worktrees, Git operations, queue evidence, and accepted/integrated commits. Matching evidence resumes from the last verified checkpoint. A material disagreement produces a human-decision report with safe options and an exact resume command.

Resume never resets, cleans, stashes, force-checks out, abandons a conflict, duplicates a branch, creates a second accepted commit, or repeats integration.

## Retry policy

Repository autonomy limits take precedence. The global defaults are three implementation repair attempts, three integration repair attempts, two additional adversarial-review passes, and one queue-reconciliation repair attempt. Each focused retry must record a new hypothesis and evidence; repeating the same failed hypothesis is rejected.

Product decisions, destructive migrations, data loss, semantic conflicts, major architecture replacement, security/privacy policy changes, new paid production dependencies, out-of-scope changes, user-data workspace access, and exhausted budgets stop at a human gate.

## Safety

Controller commands use argument arrays with no implicit shell. Git inspection is allowlisted. The controller rejects push, tag, merge, reset, clean, stash, rebase, force/discard checkout, unapproved branch deletion, release, publication, deployment, notarization, operation outside a registered repository, and unsafe Codex launcher flags.

Repository-scoped prompts repeat the same prohibitions and inherit the global/repository factory policies. The controller never merges into `main`, pushes, tags, publishes, deploys, releases, notarizes, or begins another milestone before a verified merge and policy authorization.

Sensitive subprocess output is redacted before persistence. Access tokens, API keys, authorization headers, cookies, private keys, embedded credentials, sensitive query parameters, secret fields, and user-data workspace paths have deterministic redaction coverage. Raw subprocess output is not stored.

## Registered projects

| Project | Repository | Active milestone | Current registration | Next safe action |
| --- | --- | --- | --- | --- |
| `case-manager` | `${HOME}/Developer/conan-case-manager` | `P0` | Reconciled from legacy failed pilot state | `P0` resolves to queue milestone `phase-0`; P0-002 is completed at the protected accepted commit, so the next action is the milestone gate, not feature implementation. |
| `interview-companion` | `${HOME}/Developer/Live_Interview_Companion` | `M0` | `human_decision_required` | Preserve and reconcile the retained integration writer lease before scheduling. F002 metadata was independently verified. |

Registration is independent: no Case Manager milestone, branch, feature, or commit is copied into Interview Companion.

## CLI

From `${HOME}/Developer/development-conveyor`:

```bash
scripts/conveyor validate-config
scripts/conveyor status
scripts/conveyor status --project case-manager
scripts/conveyor plan --project case-manager
scripts/conveyor reconcile --project case-manager --dry-run
scripts/conveyor run --project case-manager --mode one_feature
scripts/conveyor run --project case-manager --mode milestone
scripts/conveyor resume --project case-manager
scripts/conveyor run --mode portfolio
```

Add `--dry-run` to `run` or `resume` to prevent application writes and session launches. `status` and `plan` are always read-only. `reconcile --dry-run` validates the exact read-only reconciliation session plan; `reconcile` without that flag updates only Conveyor-owned project state from deterministic evidence.

The exact pilot dry run is:

```bash
scripts/conveyor run --project case-manager --mode milestone --dry-run
```

It must resolve `docs/FEATURE_QUEUE.yaml`, match configured `P0` to queue milestone `phase-0`, report the actual feature count, recognize completed P0-002 without selecting it, and propose the milestone gate.

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
scripts/conveyor validate-config
scripts/conveyor status
scripts/conveyor reconcile --project case-manager --dry-run
scripts/conveyor run --project case-manager --mode milestone --dry-run
```

Tests use disposable synthetic Git repositories. They cover both state machines, configuration and path expansion, safe queue-location resolution, precise malformed-queue diagnostics, milestone/status/dependency normalization, nonempty queue preservation, corroborated and rejected `SELF` cases, every reconciliation classification, exit/result disagreement, report persistence, launch-lock cleanup, idempotent retry, stable repository identity, writer contention and identity-checked stale recovery, multi-project concurrency, output redaction, prohibited actions, accepted-feature integration, milestone gating, idempotent resume, and protection of `main`.

## Human gates

The Conveyor always stops for milestone merge approval. It also stops for ambiguous state or lock ownership, product/architecture decisions, semantic conflicts, destructive changes, data loss, policy changes, paid dependencies, out-of-scope work, hard content gates, and retry exhaustion. Default-branch integration remains an explicit human-authorized workflow outside the Conveyor.
