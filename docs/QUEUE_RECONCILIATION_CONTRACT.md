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

## Accepted-work and integration priority

A unique feature in `accepted` or `integration_pending` with pending integration takes precedence over every ready feature. The queue summary reports the integration candidates separately; two or more candidates are an ambiguity, not permission to select by priority. Integration-pending evidence is not planning work and does not itself become a product human gate.

Milestone integration accepts exactly one final-line terminal assistant classification: `INTEGRATED`, `VALIDATION_FAILED`, `HUMAN_DECISION_REQUIRED`, `SEMANTIC_CONFLICT`, `RETRYABLE_INTEGRATION_FAILURE`, or `TERMINAL_INTEGRATION_FAILURE`. A missing, duplicate, non-final, or trailing marker is terminal failure. Only the last assistant result is authoritative; user, prompt, tool, and earlier assistant echoes are ignored. A zero process exit with `HUMAN_DECISION_REQUIRED` remains a gate only when the same terminal message contains one validated structured blocker descriptor. Descriptor-free historical synthesis is pinned to the exact pre-contract Case Manager project, run, report session, accepted commit, and candidate commit.

Integration runtime writes live only at `.factory/runtime/milestone-integration/`, use atomic replacement, and include repository, project, run, milestone, and feature identity. Status and recovery reads do not create the directory. Legacy `.git/factory-integration` data remains readable but is never newly written; disagreeing simultaneous records stop. The controller installs and verifies `.factory/runtime/` in local Git exclude before any integration workspace mutation. Every terminal stop releases only its owned integration lease after durable evidence is written; recovery must acquire a fresh matching lease.

## Controller-only reconciliation

`scripts/conveyor reconcile --project PROJECT --dry-run` validates the exact reconciliation prompt and read-only session plan without launching it, reports the resolved queue path and derived classification, and writes neither controller nor application state.

Without `--dry-run`, `reconcile` updates only Conveyor-owned project state from already validated queue and Git evidence. Recovery requires a clean configured milestone-branch checkout, no Git operation, verified baseline ancestry, HEAD equal to the milestone branch, and queue bytes committed at HEAD. It refuses existing application cycles or writer/launch locks. It does not launch a repository session, acquire the repository writer lease, edit application files, or change application branches or commits.

### Explicit resolution of a recorded human gate

`reconcile --project PROJECT --resolve-human-decision --reason REASON` is the only controller interface for clearing a pinned human-decision gate. The reason supplies explicit user authority; it does not replace deterministic evidence. Empty or sensitive-looking reasons fail without persistence.

The active gate must match the immutable structured descriptor in controller registration. Retained integration-writer-lease gates additionally require exact repository identity and path fingerprint, the registered milestone and branch, exact expected HEAD and milestone ref, clean committed queue bytes, no Git operation, application cycle, writer lease, or foreign controller reservation, local-host dead-process proof, hash-pinned forced-release and integration records, semantic validation of both records, successful integration commands, present accepted/integrated commits, and matching feature/milestone queue evidence. Missing, malformed, contradictory, symlinked, moved, or mutated evidence rejects the resolution.

An integration planning-baseline gate instead binds the candidate commit, its direct parent, milestone branch head, reconciliation subject, planning-only changed paths, absence of production and product-test changes, queue/catalog/roadmap/spec/status/architecture agreement, full feature-inventory validation from an extracted candidate snapshot, run-log reconciliation classification, selected feature integration base, cycle start/pre-integration commits, and a canonical evidence fingerprint. Approval is trusted only from the controller-owned applied resolution report outside the application repository; ignored cycle IDs alone cannot authorize integration. Approval records that fingerprint and the immutable resolution IDs as `validated_planning_baseline`, advances the durable cycle to `integration_ready`, and launches nothing during resolution. A partial project-state apply is replayable and repairs the still-pinned cycle before continuation. The next normal action is Milestone Integrator, never Product Architect, Feature Inventory, Feature Factory, or a new ready feature.

Dry-run returns the current gate, reason-presence result, every named validator and evidence result, acceptance decision, `human_decision_required -> queue_reconciliation` proposal, write flags, and next action. It writes no state, applied report, audit event, lock, branch, commit, or application file.

Apply revalidates after acquiring an exclusive controller reservation. Acceptance atomically records the normal state-machine transition, clears only the matching active gate, and appends the exact original gate snapshot plus explicit resolution evidence to controller history. The deterministic resolution fingerprint binds project ID, gate fingerprint, actor classification, and trimmed reason. Exact replay returns `already_resolved`; another reason cannot append a second record, and a later unrelated gate takes precedence over old history. Applied reports live under the configured report directory and audit events in the configured run-event log. Resolution never launches a repository session.

Idempotent replay validates the report byte-equivalent JSON object against persisted history and validates one complete audit provenance record. Deterministic report or audit disagreement is repaired atomically only while holding the matching controller reservation. The repair path reloads project state under that reservation and revalidates the active-gate and resolution identities before changing either artifact. Audit healing and every ordinary event append also use the same controller-wide event-log synchronization lock, preserving concurrent events from other projects. Malformed shared audit data, a newer interleaved gate, an acquisition race, ambiguous reservation ownership, or repository evidence that changes under the reservation produces a structured non-applied `resolution_rejected` result with exact failed validators and a status command; it never escapes as an unstructured lock or recovery error.

## Startup state reconciliation

Every write-capable run compares the persisted project state with the repository-local cycle, deterministic queue classification, active milestone completion, selected ready feature, milestone HEAD, prior state-decision commit, timestamped gate evidence when present, Git operations, and both lock classes. Results are classified as `state_consistent`, `stale_state_repaired`, `active_cycle_resume`, `human_decision_required`, or `invalid_state_evidence`.

Safe repairs use the portfolio state machine and durable checkpoints. In particular, stale milestone conclusions route through `milestone_gate -> queue_reconciliation -> feature_ready`; the direct edge to `feature_running` remains invalid. Dry runs report the repair without persisting it. Real runs serialize the controller-only repair, persist it before any repository session, and release the temporary controller reservation. Repository writer-lease acquisition remains owned by the repository-scoped feature workflow.

`milestone_ready_for_merge` is more restrictive: automatic reopening requires a genuine `milestone_gate_passed` checkpoint, matching unresolved merge-decision metadata, equal recorded gate and milestone commits, timestamped prior evidence, a later descendant milestone HEAD, and explicit `integration.rerun_milestone_gates: true` policy. Missing, fabricated, or contradictory evidence becomes a human decision. An exact orphaned pre-launch cycle may be ignored without editing it only when repository identity, selected feature, starting commit, branch set, and single-worktree evidence all match and no session, lock, branch preparation, Git operation, or repository mutation occurred.

The controller launch reservation covers reconciliation result processing, feature-cycle checkpoint creation, post-feature next-action transitions, milestone-gate preparation and finalization, and interrupted-cycle resume. Evidence is revalidated after reservation acquisition, including the cycle fingerprint, configured milestone branch and HEAD, committed queue bytes, selection, worktrees, local branches, Git operations, and writer-lock absence. A losing controller writes no cycle or project checkpoint.

## Feature branch, lease, and completion evidence

Before `feature_running` or any Feature Factory launch, the controller now creates or selects the metadata-derived feature branch at the verified feature starting commit, verifies that the registered repository worktree is executing on it, confirms the milestone ref is unchanged, persists the actual branch and worktree, and atomically acquires a Feature-Factory-compatible writer lease. The controller holds that lease through implementation, review, and accepted-commit verification. The feature session stops on the feature branch; milestone integration is launched separately only after exactly one accepted commit is corroborated and the feature-writer lease is released.

A zero process exit is classified from Git, worktree, lease, and queue evidence. Supported classifications include `accepted_feature`, `incomplete_feature_result`, `uncommitted_feature_work`, `feature_commit_missing`, `queue_evidence_missing`, `branch_invariant_violated`, and `session_claimed_completion_without_evidence`. Only `accepted_feature` authorizes the integration transition.

Legacy cycles whose successful session wrote dirty work on the milestone checkout require the explicit `recover-feature-branch` command. Recovery accepts only tracked modified regular files, records their SHA-256 hashes before the Git mutation, uses one non-forced `git switch -c FEATURE START`, verifies the same hashes and dirty entries afterward, verifies the milestone ref is unchanged, persists the actual feature worktree, and never launches a repository session or acquires the writer lease. Any ambiguous dirty status produces `human_decision_required` without a Git mutation.
