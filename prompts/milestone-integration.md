# Milestone integration session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Feature: `{feature}`
Conveyor run: `{run_id}`
Transaction: `{transaction_id}`
Repository identity: `{repository_identity}`
Starting branch: `{starting_branch}`
Starting commit: `{starting_commit}`

Use the named milestone integration workflow in read-only preparation mode. Discover and verify the exact accepted feature commit, milestone branch, ancestry, and configured validation plan, but do not switch branches, cherry-pick, stage, commit, or write runtime evidence. The controller kernel performs the exact-once cherry-pick and owns integration evidence after accepting your typed result.

Do not rewrite the accepted commit, discard conflicts, auto-resolve semantic conflicts, merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, or delete unintegrated work. Stop with a human-decision report when evidence disagrees.

## Terminal result contract

End the final assistant message with exactly one compact `CONVEYOR_TRANSACTION_RESULT=` JSON line bound to transaction `{transaction_id}`, repository `{repository_identity}`, run `{run_id}`, the actual session ID, starting branch `{starting_branch}`, unchanged current commit `{starting_commit}`, feature `{feature}`, no changed paths, classification `INTEGRATED`, and next state `feature_integrated`. `INTEGRATED` means the exact accepted commit and validation plan are ready for kernel finalization; it does not claim that you performed the cherry-pick. Use the other typed integration classifications only for their documented stop conditions. The marker must be final and unique.
