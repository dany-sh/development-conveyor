# Queue reconciliation session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Conveyor run: `{run_id}`

Work only in this repository. First invoke the named `product-architect` in planning-only, read-only mode to inspect completed work, dependencies, milestone scope, Git evidence, incomplete specifications, and current queue accuracy. Then use the existing `$feature-inventory` workflow for any evidence-supported queue or feature-specification correction. Do not edit application production source.

Validate the resulting JSON-compatible queue deterministically. Mark a feature ready only when its dependencies are integrated, its specification and acceptance criteria are sufficient, and no human decision remains. Preserve legitimate blocked or completed state. Do not reimplement completed work or rewrite accepted commits.

Stop for product or architecture judgment that cannot be proven from repository evidence. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, discard work, or begin feature implementation. Record every participating role's effective model and reasoning in the repository run log without hidden reasoning or secrets.

Your final response must end with exactly one single-line structured result prefixed by `CONVEYOR_RESULT=`. Do not put the marker in a code fence. Use this contract:

`CONVEYOR_RESULT={{"schema_version":1,"classification":"reconciled_ready_work|reconciled_no_ready_work|milestone_complete|legitimately_blocked|human_decision_required|invalid_queue|session_execution_failed|structured_output_invalid","summary":"redacted concise result","next_action":"safe next controller action","queue_validation":{{"valid":true,"milestone_found":true,"feature_count":0}},"retryable":false,"human_decision":null}}`

Set `feature_count` to the actual number of features in the configured milestone after deterministic milestone normalization. A validated queue with no ready feature is `reconciled_no_ready_work`, not an execution failure. Use `human_decision_required` only with a non-null `human_decision` object. Never include secrets, hidden reasoning, raw provider payloads, or private workspace data.
