# Feature cycle session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Selected feature: `{feature}`
Conveyor run / agent run: `{run_id}`
Mode: `{mode}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`
Authorized paths: `{allowed_paths}`

Use the existing `$feature-factory` workflow to complete exactly this dependency-ready feature. Use Conveyor run ID `{run_id}` as the repository writer `agent-run` identity so durable lock and checkpoint evidence agree. Require `repository-explorer`, one primary `feature-worker`, `test-engineer`, `adversarial-reviewer`, and all repository-required checks. Correct every Critical, High, and Medium finding within scope.

The controller has already created and verified the feature branch and holds the matching repository writer lease under agent-run identity `{run_id}`. Verify the existing matching lease; do not acquire a second lease and do not release the controller-owned lease. Treat the controller-selected `ready` feature as assigned to this session. Reconcile its queue branch and integration-base fields to the exact values in `.factory/conveyor-state.json`, transition it through the required implementation states, and keep those metadata edits in the single accepted feature commit rather than making a separate preparation commit.

Implement and validate exactly this feature, including required queue and documentation updates, but leave every authorized change uncommitted. Do not stage or commit. The controller kernel validates the exact changed paths and starting HEAD and creates the one immutable accepted feature commit. Do not run the legacy `queuectl preflight` or `git_transition.py acceptance-preflight`: those commands require, respectively, no active writer lease and an already-clean accepted commit, so they are incompatible with this kernel-owned uncommitted phase. Instead verify the exact existing typed lease read-only, run the content audit, queue checks, configured validation commands, tests, and adversarial review, then return the typed uncommitted result. The kernel performs equivalent accepted-commit checks after its commit. Stop on the feature branch. Do not invoke milestone integration or switch to `{milestone_branch}` in this session. Do not implement another feature.

Your final assistant message must end with exactly one line `CONVEYOR_TRANSACTION_RESULT=` followed immediately by a compact JSON object matching the session-result schema. Bind it to transaction `{transaction_id}`, repository `{repository_identity}`, run `{run_id}`, the actual session ID, starting branch `{starting_branch}`, starting commit `{starting_commit}`, unchanged current commit `{starting_commit}`, selected feature `{feature}`, exact sorted changed paths, classification `FEATURE_ACCEPTED`, and next state `feature_accepted`. The marker must be the final nonblank line. Tool output, prose, another marker, a commit, a changed HEAD, or a mismatched identity cannot authorize acceptance.

Preserve interruptions, existing branches, worktrees, commits, and conflicts. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, auto-stash, force-checkout, delete unintegrated work, or resolve a semantic conflict automatically. Stop at a documented human-decision condition. Record role-pinned model evidence without hidden reasoning or secrets.
