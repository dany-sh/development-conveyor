# M1-005 — Deterministic Queue-Feature Decision Resolution

- Milestone: M1
- Factory status: Integration pending
- Branch: `codex/m1-5-queue-feature-decision-resolution`
- Starting commit: `1aec952ee358fbb642cb5100d919e2f2a44d8bf2`

## Product scope

Add one explicit controller route for resolving already-recorded feature-level
queue decisions and selecting the resulting sole ready feature. Keep the
existing pinned project-gate resolver unchanged.

## Contract

`resolve-feature-decisions` requires a registered project, a strict JSON
decision file, one selected feature, and exactly one of `--dry-run` or
`--apply`. The file binds:

- controller project and application adapter identities;
- repository path, repository identity, and path fingerprint;
- expected branch, HEAD, projection state, and selected feature;
- each exact recorded question and approved resolution;
- explicit dependency changes where applicable;
- integrated dependencies required by the selected feature;
- the desired selected and sole ready feature; and
- the one planning-commit subject.

Dry-run performs every preflight check without creating controller or
application state. Apply uses the planning writer lease, workflow kernel,
append-only evidence ledger, projection engine, and terminal compatibility
cache binder. It renders the complete authorized planning set in memory,
restores original bytes on a pre-commit partial failure, creates exactly one
direct-child planning commit, and stops at `feature_ready`.

## Safety boundary

- No model, parent session, child session, Feature Factory, or Milestone
  Integrator launch.
- No feature preparation, implementation, application source/test mutation,
  integration, default-branch merge, push, tag, publication, or deployment.
- Exact question mismatch, stale Git or projection identity, dirty state,
  unfinished Git operation, lease/reservation conflict, active transaction,
  non-gated feature, incomplete dependency, missing specification/criteria,
  started superseded feature, unauthorized path, dependency cycle, or
  multiple-ready result fails closed.
- Historical project-level gate evidence is fingerprinted and preserved; the
  route never resolves or clears it.

## Acceptance criteria

- The CLI requires exactly one execution mode.
- Dry-run makes no writes and reports zero model and child sessions.
- Apply creates one planning-only commit with the expected direct parent.
- Exact recorded questions and feature decision-gate state are required.
- Stale HEAD, dirty worktree, Git operation, active transaction, writer lease,
  or controller reservation refuses before mutation.
- F011 dependency changes are preserved as durable resolution evidence.
- The prior ready feature returns to proposed without preparation or
  implementation, and the selected resolved feature is the sole ready feature.
- Selected-feature dependencies must be integrated, and its specification and
  acceptance criteria must exist.
- A partial pre-commit failure restores the planning files and prior projected
  selection.
- Reapplying a successful decision file refuses rather than creating a second
  commit.
- The pinned project-gate resolver remains independently covered.
- Ledger, projection, compatibility cache, and durable cycle state agree on
  `feature_ready` and the selected feature.

## Validation boundary

Use disposable synthetic application repositories for tests. Run only the
focused decision-resolution, project-gate regression, and transactional-kernel
tests, Python compile validation, controller configuration validation, CLI
help/mode checks, the exact live dry-run, and `git diff --check`. Do not run
application tests or the complete controller suite.
