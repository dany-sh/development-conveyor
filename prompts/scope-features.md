# Bounded feature-scoping session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Conveyor run: `{run_id}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`
Authorized paths: `{allowed_paths}`
Parent sessions: `{parent_session_budget}`
Child sessions: `{child_session_budget}`

This is one planning-only feature-scoping transaction. Work directly in this
parent session. Do not invoke Feature Factory, Milestone Integrator, named
agents, delegating skills, or child sessions. Do not reconcile unrelated queue
state and do not begin feature implementation.

Read the explicit Markdown brief and the requested existing feature
specifications supplied in the prompt context. Author only the requested
feature specifications and the authorized inventory/status documents. Preserve
every existing feature ID. Create a requested new ID only when absent. Never
change production source, tests, runtime state, historical evidence, an
unrequested feature specification, branches, or commits.

Leave the planning changes uncommitted and unstaged. The controller owns
deterministic validation and the single planning-only commit. Stop before
feature preparation or execution.

Your final response must end with exactly one compact
`CONVEYOR_TRANSACTION_RESULT=` JSON line matching the generic session-result
schema. Bind it to transaction `{transaction_id}`, repository
`{repository_identity}`, run `{run_id}`, the actual session ID, starting branch
`{starting_branch}`, unchanged current commit `{starting_commit}`, null feature,
and the exact sorted changed paths. A successful scoping result uses
`workflow_type: queue_reconciliation`, classification
`RECONCILED_READY_WORK`, and next state `feature_ready`. Include the target
features, new features, selected ready feature, dependency changes, execution
policies, and a statement that feature execution did not start inside
`evidence`.

Never include secrets, hidden reasoning, raw provider payloads, or private
workspace data.

## Existing feature context

{focused_context}
