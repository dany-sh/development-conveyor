# Milestone integration session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Feature: `{feature}`
Conveyor run: `{run_id}`

Use `$milestone-integrator` and the named `milestone_integrator` agent. Discover and verify the exact accepted feature commit from repository evidence. Preserve the pre-integration milestone HEAD, integrate only the accepted feature into `{milestone_branch}`, run every configured integration gate, and record post-integration evidence. Resume an existing matching integration instead of duplicating it.

Do not rewrite the accepted commit, discard conflicts, auto-resolve semantic conflicts, merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, or delete unintegrated work. Stop with a human-decision report when evidence disagrees.

