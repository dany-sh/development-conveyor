# Queue reconciliation session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Conveyor run: `{run_id}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`
Authorized paths: `{allowed_paths}`

Work only in this repository. First invoke the named `product-architect` in planning-only, read-only mode to inspect completed work, dependencies, milestone scope, Git evidence, incomplete specifications, and current queue accuracy. Then use the existing `$feature-inventory` workflow for any evidence-supported queue or feature-specification correction. Do not edit application production source.

Validate the resulting JSON-compatible queue deterministically. Mark a feature ready only when its dependencies are integrated, its specification and acceptance criteria are sufficient, and no human decision remains. Preserve legitimate blocked or completed state. Do not reimplement completed work or rewrite accepted commits.

Stop for product or architecture judgment that cannot be proven from repository evidence. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, discard work, or begin feature implementation. Record every participating role's effective model and reasoning in the repository run log without hidden reasoning or secrets.

Leave authorized planning changes uncommitted and unstaged. The controller kernel owns the planning commit. Your final response must end with exactly one compact `CONVEYOR_TRANSACTION_RESULT=` JSON line matching the generic session-result schema and bound to transaction `{transaction_id}`, repository `{repository_identity}`, run `{run_id}`, actual session ID, starting branch `{starting_branch}`, unchanged current commit `{starting_commit}`, null feature, and the exact sorted changed paths. Map the result to `RECONCILED_READY_WORK`, `RECONCILED_NO_READY_WORK`, `MILESTONE_COMPLETE`, `HUMAN_DECISION_REQUIRED`, `PLANNING_VALIDATION_FAILED`, `RETRYABLE_PLANNING_FAILURE`, or `TERMINAL_PLANNING_FAILURE`, with the corresponding next state. Put the deterministic queue-validation facts, concise redacted summary, retryability, and optional human decision inside `evidence`. The marker must be unique and final.

Set `feature_count` to the number of features in the configured active milestone after deterministic milestone normalization. Set `global_feature_count` to the number of features in the complete queue and `global_milestone_count` to the number of milestones in the complete queue; these global counts are not aliases for `feature_count`. Include all three counts in `queue_validation`.

Use the canonical queue warning contract: `warning_count` is required and is a non-negative integer equal to the deterministic validator warning-list length; `warnings_scope` is an optional explanatory string; and `blocking_warnings` is an optional string array that must agree with deterministic blocking classification. If an explicit `warnings` field is included, it must be a string array. Never put prose in a `warnings` string.

A validated queue with no ready feature is `reconciled_no_ready_work`, not an execution failure. Use `human_decision_required` only with a non-null `human_decision` object. Never include secrets, hidden reasoning, raw provider payloads, or private workspace data.
