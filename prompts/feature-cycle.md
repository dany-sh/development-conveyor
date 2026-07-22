# Direct bounded feature implementation session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Selected feature: `{feature}`
Conveyor run: `{run_id}`
Mode: `{mode}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`
Authorized paths: `{allowed_paths}`
Child-session budget: `0`

Implement exactly the selected feature directly in this parent session. Do not
read or invoke Feature Factory, Milestone Integrator, Product Architect,
Repository Explorer, Feature Worker, Test Engineer, Adversarial Reviewer, or
any named agent. Do not use collaboration, delegation, fan-out, or child-agent
tools. The launcher has mechanically removed those tools for this session.

The controller already created and verified the feature branch and holds the
matching typed writer lease. Do not acquire, replace, heartbeat, or release a
lease. Do not stage or commit. Leave acceptance, the one immutable feature
commit, and milestone integration to the controller.

Use only the focused context pack below plus files directly required to
implement `{feature}`. Preserve existing behavior outside the feature contract.
Run focused implementation-loop tests that can execute inside this model
sandbox. Do not run staged-application, LaunchServices, accessibility, signing,
notarization, release, or other host-level checks here; the controller runs all
final acceptance gates directly on the host after the session returns.

Keep HEAD exactly `{starting_commit}` and remain on `{starting_branch}`. The
final changed path array must be sorted, unique, and exactly match the live
tracked and untracked implementation diff. Record focused validation command,
exit status, and concise evidence in `evidence.focused_validation`. Set
`evidence.implementation_complete` and
`evidence.controller_acceptance_pending` to `true`.

Obtain the actual session UUID from `printenv CODEX_THREAD_ID` in this session.
It must equal the launcher's `thread.started` identity. Never return a null,
placeholder, inherited controller ID, or guessed session ID.

Your final assistant message must end with exactly one line
`CONVEYOR_TRANSACTION_RESULT=` followed immediately by one compact JSON object
matching this exact schema. The marker must be the final nonblank line. A
legacy payload, extra key, missing identity, placeholder, null feature/session,
changed HEAD, or mismatched path set is rejected.

```json
{terminal_schema}
```

Preserve interruptions, existing branches, worktrees, commits, and conflicts.
Never merge into the default branch, push, force-push, tag, publish, deploy,
release, notarize, reset, clean, auto-stash, force-checkout, delete
unintegrated work, or resolve a semantic conflict automatically. Stop at a
documented human-decision condition.

## Focused application context

{focused_context}
