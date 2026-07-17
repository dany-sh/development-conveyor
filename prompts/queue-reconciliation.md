# Queue reconciliation session

Repository: `{repository}`
Project: `{project_id}`
Active milestone: `{milestone}`
Conveyor run: `{run_id}`

Work only in this repository. First invoke the named `product-architect` in planning-only, read-only mode to inspect completed work, dependencies, milestone scope, Git evidence, incomplete specifications, and current queue accuracy. Then use the existing `$feature-inventory` workflow for any evidence-supported queue or feature-specification correction. Do not edit application production source.

Validate the resulting JSON-compatible queue deterministically. Mark a feature ready only when its dependencies are integrated, its specification and acceptance criteria are sufficient, and no human decision remains. Preserve legitimate blocked or completed state. Do not reimplement completed work or rewrite accepted commits.

Stop for product or architecture judgment that cannot be proven from repository evidence. Never merge into the default branch, push, force-push, tag, publish, deploy, release, notarize, reset, clean, stash, discard work, or begin feature implementation. Record every participating role's effective model and reasoning in the repository run log without hidden reasoning or secrets.

