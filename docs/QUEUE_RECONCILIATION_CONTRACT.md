# Queue discovery and reconciliation contract

## Queue location

`queue_location` is expanded only through the controller's approved environment-variable list. `${HOME}` remains portable in registration. A relative path is resolved from the exact registered Git root; an absolute path must still resolve inside that root. Resolution rejects unresolved variables, `..` or symlink traversal outside the repository, missing paths, and non-regular files.

Status and dry-run output include both `configured_queue_location` and `resolved_queue_path`. A missing queue and an unsupported JSON-compatible YAML document are distinct validation errors. Structural errors identify the JSON/YAML path, such as `$.milestones` or `$.features[0].dependencies[1]`. Malformed input never becomes an empty queue.

## Queue normalization

The parser requires milestone and feature arrays. Every array member must be an object. Milestone and feature IDs are unique, feature dependencies refer to known IDs, and feature status, milestone membership, commit reference, specification, acceptance criteria, and human-decision flag are type-checked.

Status strings are normalized by case and separators. Only explicit aliases are accepted: `complete` and `completed` become legacy `done`, `inprogress` becomes `in_progress`, `pending_integration` becomes `integration_pending`, and `awaiting_human_decision` becomes `human_decision_required`. Unknown statuses remain validation failures.

Milestone matching is exact first, then normalized and ambiguity-checked. `P0` and `phase-0` share the canonical identifier `phase-0`; `M0` and `milestone-0` share `milestone-0`. This does not change the repository's stored spelling.

## `SELF` accepted-commit sentinel

`SELF` is allowed only for a completed `done` or `integrated` feature. The controller never resolves it from current `HEAD` alone. A candidate must be the registration's accepted commit and the registered accepted-feature label must identify the feature ID. The controller then verifies all of the following:

- the candidate exists and is contained by the configured milestone branch;
- it is a descendant of, and not equal to, the feature integration base or validated milestone baseline;
- the queue file exists in the candidate, still marks the same feature completed, and contains `SELF`;
- the candidate changed the queue file;
- the feature specification, `docs/CURRENT_STATUS.md`, and `docs/RUN_LOG.md` exist in the candidate and identify the feature;
- at least one corroborating specification, status, or run-log path changed in the candidate.

Missing, ambiguous, contradictory, or arbitrary-HEAD evidence rejects the sentinel. Case Manager P0-002 resolves through this policy to `4c43aa5cd870ddb4962eceb1fbe35c648efa3e18`.

## Structured reconciliation result

The queue-reconciliation session must end its terminal assistant message with exactly one single-line `CONVEYOR_RESULT=` JSON object. Prompt echoes, user messages, tool output, earlier assistant messages, duplicate markers, and content after the marker line are not authoritative. Schema version 1 requires a classification, concise redacted summary, safe next action, deterministic queue-validation facts, retryability, and an optional human-decision object.

Supported classifications are:

- `reconciled_ready_work`
- `reconciled_no_ready_work`
- `milestone_complete`
- `legitimately_blocked`
- `human_decision_required`
- `invalid_queue`
- `session_execution_failed`
- `structured_output_invalid`

The controller validates the structured result and then corroborates its milestone, feature count, and classification by reparsing the repository queue. Contradiction is a validation failure. A valid no-ready result succeeds at the controller level and moves to a safe paused planning checkpoint; it is not reduced to a generic process failure.

## Exit-code policy

Process status and semantic result are separate evidence:

- a process launch exception is `process_launch_failure` and no semantic result is assumed;
- nonzero status without a valid marker is `session_execution_failed`;
- malformed marked output is `structured_output_invalid`;
- a valid structured result is classified by its contract, including a valid nonzero human gate;
- success classifications are accepted only after deterministic queue and repository corroboration;
- a nonzero status is never ignored when the returned result or safety state is invalid or contradictory;
- interrupted and explicitly retryable results use the bounded focused-retry policy.

Every launched session report stores the project ID, run ID, invoked agent or skill, working directory, argument array, exit status, classification, structured-output validation, redacted stdout, redacted stderr and summary, session ID, current state, report path, and safe resume command. Raw subprocess output is not persisted.

## Controller-only reconciliation

`scripts/conveyor reconcile --project PROJECT --dry-run` validates the exact reconciliation prompt and read-only session plan without launching it, reports the resolved queue path and derived classification, and writes neither controller nor application state.

Without `--dry-run`, `reconcile` updates only Conveyor-owned project state from already validated queue and Git evidence. Recovery requires a clean configured milestone-branch checkout, no Git operation, verified baseline ancestry, HEAD equal to the milestone branch, and queue bytes committed at HEAD. It refuses existing application cycles or writer/launch locks. It does not launch a repository session, acquire the repository writer lease, edit application files, or change application branches or commits.

## Startup state reconciliation

Every write-capable run compares the persisted project state with the repository-local cycle, deterministic queue classification, active milestone completion, selected ready feature, milestone HEAD, prior state-decision commit, timestamped gate evidence when present, Git operations, and both lock classes. Results are classified as `state_consistent`, `stale_state_repaired`, `active_cycle_resume`, `human_decision_required`, or `invalid_state_evidence`.

Safe repairs use the portfolio state machine and durable checkpoints. In particular, stale milestone conclusions route through `milestone_gate -> queue_reconciliation -> feature_ready`; the direct edge to `feature_running` remains invalid. Dry runs report the repair without persisting it. Real runs serialize the controller-only repair, persist it before any repository session, and release the temporary controller reservation. Repository writer-lease acquisition remains owned by the repository-scoped feature workflow.

`milestone_ready_for_merge` is more restrictive: automatic reopening requires a genuine `milestone_gate_passed` checkpoint, matching unresolved merge-decision metadata, equal recorded gate and milestone commits, timestamped prior evidence, a later descendant milestone HEAD, and explicit `integration.rerun_milestone_gates: true` policy. Missing, fabricated, or contradictory evidence becomes a human decision. An exact orphaned pre-launch cycle may be ignored without editing it only when repository identity, selected feature, starting commit, branch set, and single-worktree evidence all match and no session, lock, branch preparation, Git operation, or repository mutation occurred.

The controller launch reservation covers reconciliation result processing, feature-cycle checkpoint creation, post-feature next-action transitions, milestone-gate preparation and finalization, and interrupted-cycle resume. Evidence is revalidated after reservation acquisition, including the cycle fingerprint, configured milestone branch and HEAD, committed queue bytes, selection, worktrees, local branches, Git operations, and writer-lock absence. A losing controller writes no cycle or project checkpoint.
