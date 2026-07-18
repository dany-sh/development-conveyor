# Milestone integration session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Feature: `{feature}`
Conveyor run: `{run_id}`

Use `$milestone-integrator` and the named `milestone_integrator` agent. Discover and verify the exact accepted feature commit from repository evidence. Preserve the pre-integration milestone HEAD, integrate only the accepted feature into `{milestone_branch}`, run every configured integration gate, and record post-integration evidence. Resume an existing matching integration instead of duplicating it.

Do not rewrite the accepted commit, discard conflicts, auto-resolve semantic conflicts, merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, or delete unintegrated work. Stop with a human-decision report when evidence disagrees.

## Terminal result contract

End the terminal assistant response with exactly one standalone heading selected from this list:

- `## INTEGRATED`
- `## VALIDATION_FAILED`
- `## HUMAN_DECISION_REQUIRED`
- `## SEMANTIC_CONFLICT`
- `## RETRYABLE_INTEGRATION_FAILURE`
- `## TERMINAL_INTEGRATION_FAILURE`

Use `INTEGRATED` only after the accepted commit is integrated and every configured validation gate passes. A safe stop that requires explicit user approval is `HUMAN_DECISION_REQUIRED`, even when no command failed. Immediately before that heading, emit exactly one single-line `CONVEYOR_INTEGRATION_GATE=` JSON object with schema version 1, a nonempty `gate_classification`, a concise non-sensitive `reason`, a nonempty string array `blocker_categories`, and `retryable:false`. Do not use that descriptor for another classification. The selected heading must be the final nonblank line. Do not repeat it elsewhere or emit more than one classification.
