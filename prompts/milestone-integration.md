# Milestone integration session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Expected milestone branch: `{milestone_branch}`
Feature: `{feature}`
Conveyor run: `{run_id}`
Session identity: `{session_identity}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting milestone commit: `{starting_commit}`
Accepted commit: `{accepted_commit}`
Accepted-commit changed paths: `{allowed_paths}`

This is an execution workflow, not a preparation-only review. Use the focused context below for judgment. Do not search global memories, global skill directories, unrelated feature specifications, the complete roadmap, or unrelated queue entries.

After read-only preflight succeeds, invoke this exact deterministic milestone-integration mutation command and remain in the same session through its terminal result:

```text
{mutation_command}
```

Do not report success after preflight alone. `INTEGRATED` is permitted only after the accepted commit is integrated or an exact previously integrated outcome is proven, the milestone branch contains the accepted patch, combined validation passes, queue/status/run-log and runtime evidence are durable, the repository is clean, no Git operation remains, and the owned integration lease is released or proven safe for controller release.

Do not rewrite the accepted commit, discard conflicts, auto-resolve semantic conflicts, merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, delete unintegrated work, or begin application feature implementation. Stop on identity disagreement, semantic conflict, an unsafe repository state, or a documented human-decision condition.

## Allowed terminal classifications and required evidence

- `INTEGRATED`: `next_state` is `feature_integrated`; evidence names the exact accepted commit, exact mutation command, runtime record, successful validation, already-integrated status, and integrated or patch-equivalent commit. A new integration must advance the milestone branch and report nonempty changed paths.
- `VALIDATION_FAILED`: `next_state` is `validation_failed`; evidence identifies failed configured commands and the durable runtime failure record. Never claim `validation_passed`.
- `HUMAN_DECISION_REQUIRED`: `next_state` is `human_decision_required`; evidence contains a transaction-bound `human_decision` object with the reason and blocker categories.
- `SEMANTIC_CONFLICT`: `next_state` is `human_decision_required`; evidence identifies the preserved Git operation, conflicting paths, and durable runtime conflict record.
- `RETRYABLE_INTEGRATION_FAILURE`: `next_state` is `integration_ready`; evidence and the separate controller retry marker identify a repository-scoped retryable cause and materially different remediation.
- `TERMINAL_INTEGRATION_FAILURE`: `next_state` is `validation_failed`; evidence identifies the terminal cause and durable evidence available.

Preparation has no terminal success classification. Never emit `INTEGRATED` with the unchanged starting commit for a new integration, with empty `changed_paths`, without the accepted commit or a proven patch-equivalent in milestone history, without runtime evidence, while validation is incomplete or failed, while the queue remains `integration_pending`, while the repository is dirty, while a Git operation remains, or when the accepted commit differs from this execution plan.

## Exact terminal schema

The final nonblank line of the final assistant message must be exactly one compact `CONVEYOR_TRANSACTION_RESULT=` JSON object matching this schema:

```json
{terminal_schema}
```

Valid completed-new-integration example, already bound to this transaction and run:

```text
CONVEYOR_TRANSACTION_RESULT={terminal_example}
```

Replace only the example placeholders for actual session ID, resulting commit, changed paths, and patch-equivalent commit. The marker must be unique and final.

## Focused repository context

{focused_context}
