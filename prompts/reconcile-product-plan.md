# Approved product-plan reconciliation

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Milestone branch: `{milestone_branch}`
Conveyor run: `{run_id}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`
Authorized paths: `{allowed_paths}`
Parent sessions: `{parent_session_budget}`
Child sessions: `{child_session_budget}`

This is one planning-only reconciliation from an immutable approved brief.
Work directly in this parent session. Do not invoke Feature Factory, Milestone
Integrator, named agents, delegating skills, or child sessions. Do not begin
feature implementation.

Treat the embedded approved brief as authoritative product input. Reconcile the
product vision, both roadmaps, catalog, queue, current status, run log, relevant
feature specifications, and only necessary planning architecture or decision
records. Preserve every stable feature ID. Preserve F097 exactly as integrated
with its accepted and integrated evidence; do not reopen, broaden, or rewrite
it. Remove Test Call from future user-facing requirements and retain simulation
decoding only as explicitly documented backward compatibility.

Revise scope, dependencies, milestones, sequencing, and acceptance criteria so
application/round ownership precedes new Practice, Live, and imported sessions;
Practice, Live, question management, suggested answers, transcripts, review,
scoring, comments, highlights, improved answers, and simple transcript export
agree with the brief. Select F068 as the sole dependency-ready feature.

Never change production source, tests, fixtures, build-system files, runtime
state, controller state, historical evidence, branches, or commits. Do not run
Swift builds or application tests. Leave planning changes uncommitted and
unstaged; the controller owns host validation and the single planning commit.

Your final response must end with exactly one compact
`CONVEYOR_TRANSACTION_RESULT=` JSON line matching the generic session-result
schema. Bind it to transaction `{transaction_id}`, repository
`{repository_identity}`, run `{run_id}`, the actual session ID, starting branch
`{starting_branch}`, unchanged current commit `{starting_commit}`, null feature,
and the exact sorted changed paths. Success uses
`workflow_type: queue_reconciliation`, classification
`RECONCILED_READY_WORK`, and next state `feature_ready`. Evidence must state
that the approved brief governed the reconciliation, F097 stayed integrated,
F068 is the sole ready feature, and feature execution did not start.
Include a structured `queue_validation` object with `valid`, milestone and
feature counts, global counts, and `warning_count`.

Never include secrets, hidden reasoning, raw provider payloads, or private
workspace data.

## Pinned planning context

{focused_context}
