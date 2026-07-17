# Milestone gate session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Milestone branch: `{milestone_branch}`
Conveyor run: `{run_id}`

Use `$milestone-gate`, run the complete repository-configured validation matrix, and invoke the named `release-auditor` in read-only mode. Verify all active-milestone features are integrated, accepted-feature commit boundaries remain intact, documentation is current, Git state is clean, and no Critical, High, or Medium finding remains.

Record deterministic gate evidence only when the technical gate and release audit pass. Stop for human milestone merge approval. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, begin the next milestone, reset, clean, stash, or discard work.

