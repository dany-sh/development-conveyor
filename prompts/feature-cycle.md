# Feature cycle session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Selected feature: `{feature}`
Conveyor run / agent run: `{run_id}`
Mode: `{mode}`

Use the existing `$feature-factory` workflow to complete exactly this dependency-ready feature. Use Conveyor run ID `{run_id}` as the repository writer `agent-run` identity so durable lock and checkpoint evidence agree. Require `repository-explorer`, one primary `feature-worker`, `test-engineer`, `adversarial-reviewer`, and all repository-required checks. Correct every Critical, High, and Medium finding within scope.

Create exactly one immutable accepted feature commit relative to the recorded feature base. Then invoke the existing `$milestone-integrator`, integrate the accepted commit into `{milestone_branch}`, and run the configured combined-tree integration gates. The feature is not complete until integration validation passes. Do not implement another feature in this session; branch preparation alone is allowed only when existing policy requires it.

Preserve interruptions, existing branches, worktrees, commits, and conflicts. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, auto-stash, force-checkout, delete unintegrated work, or resolve a semantic conflict automatically. Stop at a documented human-decision condition. Record role-pinned model evidence without hidden reasoning or secrets.

