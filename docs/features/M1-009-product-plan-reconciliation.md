# M1-009 — Transactional approved product-plan reconciliation

## Objective

Apply one immutable approved product brief to an application's complete
planning graph before ordinary queue reconciliation, without combining product
planning with application implementation.

## Acceptance criteria

1. `scripts/conveyor reconcile-product-plan` requires a registered project, a
   regular non-symlink brief, and exactly one of `--dry-run` or `--apply`.
2. The command pins the brief path, file identity, size, exact and normalized
   SHA-256, repository identity, branch, HEAD, queue, ledger sequence and
   fingerprint, and projection fingerprint.
3. Dry-run writes nothing, launches nothing, proves the stable post-F097
   topology, shows the `gpt-5.6-sol`/medium one-parent/zero-child apply policy,
   lists allowed and forbidden path categories, and stops before
   implementation, tests, integration, or feature execution.
4. Apply reserves the repository, acquires the typed planning-writer lease,
   revalidates every pinned identity, copies the approved brief into
   controller-owned evidence, launches exactly one planning parent with
   collaboration disabled, and permits only approved planning paths.
5. Host validation preserves every feature ID and F097's accepted/integrated
   evidence; rejects production, test, fixture, build, and runtime-state
   changes; validates the queue and acyclic dependencies; enforces
   compatibility-only simulation decoding; and reconciles Practice, Live,
   question, transcript, review, scoring, ownership, and export semantics.
6. A successful apply creates one planning-only commit, transactionally
   advances ledger, projection, controller compatibility state, and application
   cycle state to `feature_ready` with F068 selected, and starts no feature.
7. A malformed or invalid model result preserves the exact diff fingerprint
   and file hashes, releases its lease, creates no partial commit, and exposes
   `recover-product-plan`.
8. Recovery reauthenticates the brief, repository, retained paths, hashes,
   semantic result, and diff after acquiring a recovery lease, then creates the
   one planning commit with zero model and zero child sessions.

## Non-goals

- Running ordinary Conveyor resume or existing queue reconciliation.
- Applying the live Interview Companion product plan during controller
  development.
- Modifying or testing application production code.
- Starting a feature, integrating a feature, merging, pushing, publishing,
  deploying, or releasing.
