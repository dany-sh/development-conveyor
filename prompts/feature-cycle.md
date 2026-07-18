# Feature cycle session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Selected feature: `{feature}`
Conveyor run / agent run: `{run_id}`
Mode: `{mode}`

Use the existing `$feature-factory` workflow to complete exactly this dependency-ready feature. Use Conveyor run ID `{run_id}` as the repository writer `agent-run` identity so durable lock and checkpoint evidence agree. Require `repository-explorer`, one primary `feature-worker`, `test-engineer`, `adversarial-reviewer`, and all repository-required checks. Correct every Critical, High, and Medium finding within scope.

The controller has already created and verified the feature branch and holds the matching repository writer lease under agent-run identity `{run_id}`. Verify the existing matching lease; do not acquire a second lease and do not release the controller-owned lease. Treat the controller-selected `ready` feature as assigned to this session. Reconcile its queue branch and integration-base fields to the exact values in `.factory/conveyor-state.json`, transition it through the required implementation states, and keep those metadata edits in the single accepted feature commit rather than making a separate preparation commit.

Create exactly one immutable accepted feature commit relative to the recorded feature base, including required queue and documentation updates. Run acceptance preflight, then stop on the feature branch. Do not invoke milestone integration or switch to `{milestone_branch}` in this session. The controller will independently verify the accepted commit, release the feature-writer lease, and only then authorize the existing `$milestone-integrator`. Do not implement another feature in this session.

Preserve interruptions, existing branches, worktrees, commits, and conflicts. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, auto-stash, force-checkout, delete unintegrated work, or resolve a semantic conflict automatically. Stop at a documented human-decision condition. Record role-pinned model evidence without hidden reasoning or secrets.
