# Milestone gate session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Milestone branch: `{milestone_branch}`
Conveyor run: `{run_id}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`

Use `$milestone-gate`, run the complete repository-configured validation matrix, and invoke the named `release-auditor` in read-only mode. Verify all active-milestone features are integrated, accepted-feature commit boundaries remain intact, documentation is current, Git state is clean, and no Critical, High, or Medium finding remains.

Record deterministic gate evidence only when the technical gate and release audit pass. Stop for human milestone merge approval. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, begin the next milestone, reset, clean, stash, or discard work.

Leave any authorized gate-documentation changes uncommitted. The controller kernel owns the final gate commit. End the final assistant message with one unique compact `CONVEYOR_TRANSACTION_RESULT=` JSON line bound to transaction `{transaction_id}`, repository `{repository_identity}`, run `{run_id}`, actual session ID, starting branch `{starting_branch}`, unchanged current commit `{starting_commit}`, exact sorted changed paths, classification `MILESTONE_GATE_PASSED`, and next state `milestone_ready_for_merge`. The marker must be the final nonblank line.
