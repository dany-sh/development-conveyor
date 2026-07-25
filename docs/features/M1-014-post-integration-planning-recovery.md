# M1-014 — Post-Integration Planning-Result Recovery

## Status

Review on `codex/m1-14-post-integration-planning-recovery`.

## Problem

After F068 integration, the authenticated queue-reconciliation parent returned
a valid `RECONCILED_READY_WORK` result selecting F070 and retained an exact
eight-file planning diff. Both named nested roles were unavailable at the
Codex app-server initialization boundary, before a nested model session,
repository inspection, findings, or mutation. The controller persisted
`PLANNING_VALIDATION_FAILED` because newly-ready F070 lacked an explicit
execution policy, and the existing recovery classifier rejected that failure
before it could use the valid parent result and recorded deterministic
validation.

## Contract

1. Recovery accepts only the exact queue-reconciliation topology with matching
   project, repository, run, transaction, session, branch, starting HEAD,
   terminal marker, parsed result, retained paths, diff fingerprint, and
   `RECONCILED_READY_WORK` to `feature_ready` transition.
2. The F070 missing-policy exception is available only when F070 is the sole
   deterministic ready selection and every directly executed
   `product-architect` and `feature-inventory-lead` launch ends with the known
   pre-inspection app-server initialization error, no nested thread, no
   findings, and no role-attributable file change. Other missing-policy or role
   failures remain invalid.
3. Dry-run authenticates the already-recorded inventory validator and
   `git diff --check` results from the parent report. It runs no application
   command, acquires no lease, starts no transaction, writes nothing, launches
   no model or child, and reports at most one future planning commit.
4. Apply revalidates under the planning recovery reservation and writer lease,
   reruns the inventory validator and `git diff --check`, creates exactly one
   direct-child planning commit, appends recovery evidence, refreshes
   projection and both compatibility caches, releases ownership, and stops at
   `feature_ready` with F070 selected.
5. Recovery never prepares or executes F070, repeats queue reconciliation,
   reopens F068, performs integration, or launches a model. Autopilot uses its
   existing deterministic recovery budget and then continues through normal
   F070 selection.

## Validation boundary

Focused planning recovery, Autopilot, cache binding, projection, status,
consistency, and execution-plan tests; Python compilation; controller
configuration and queue validation; CLI help and mode rejection; Git diff
validation; and the one live F070 `recover-planning --dry-run`. Application
tests, application builds, the complete controller suite, live recovery apply,
ordinary resume, queue reconciliation, Autopilot restart, integration, merge,
push, tag, publication, deployment, and release are excluded.
