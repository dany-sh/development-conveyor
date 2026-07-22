# M1-002 — Cost-aware execution policy

## Objective

Plan workflows before mutable work so the controller uses deterministic routes, focused context and verification, and reusable evidence whenever those provide the required confidence.

## Acceptance criteria

1. Dry-run plans expose model routing, session budgets, context estimates, selected verification, skipped work, invalidation rules, and stopping criteria.
2. Deterministic status, consistency, queue parsing, dry-run planning, and complete deterministic integration select zero model sessions.
3. Model/reasoning selection is independently routed; Sol xhigh requires both a failed focused attempt and remaining high-risk ambiguity.
4. Child-session budgets fail closed before a launcher callback, and controller maintenance defaults to zero children.
5. Verification is changed-path and risk based; documentation and configuration changes do not trigger application builds or unrelated suites.
6. Evidence reuse requires a complete trusted identity match and rejects dirty or incomplete evidence.
7. Large command output is retained in a report file and represented in model context only by a summary.
8. Application feature execution binds one selected feature through the authoritative execution plan, branch preparation, kernel transaction, direct parent `SessionRequest`, exact prompt contract, terminal envelope, semantic validation, report, ledger, and projection.
9. Direct feature sessions have a parent budget of one and child budget of zero. Launcher preflight proves collaboration tools can be removed, then starts Codex with multi-agent features disabled; unsupported removal fails before process launch.
10. The feature result envelope carries exact project, repository, run, transaction, workflow, feature, launcher-observed session, branch, commit, changed-path, classification, next-state, and validation evidence. Null, legacy, placeholder, or mismatched identities are rejected.
11. A terminal structured-output failure may recover an exact existing implementation diff without another model only after immutable topology, report, branch, HEAD, path, queue, accepted-commit, and lease evidence agree. Host validation, acceptance metadata, the one feature commit, lease release, terminal projection, and compatibility cache materialization remain controller-owned.

## Scope

The policy is controller-only. It does not relax ledger, transaction, lease, Git, recovery, dry-run, or application-isolation requirements.

## Accepted implementation evidence

- `feature_cycle` now launches one bounded direct parent session rather than Feature Factory. The cost plan selects `gpt-5.6-sol` with `high` reasoning for application mutation, and the CLI invocation removes both multi-agent feature surfaces under strict configuration.
- The terminal contract is request-specific and semantically revalidated against the launcher-observed session UUID and live repository before the kernel accepts it.
- `recover-feature-result` implements read-only exact-evidence inspection and a separate zero-model apply path. The controller adopts only the proven dirty baseline, runs required host gates under a fresh `feature_writer` lease, preserves non-metadata implementation bytes, commits one direct-child feature result, terminalizes the transaction, releases the lease, and projects `integration_pending` without integrating.
- Live proof recovered Interview Companion F003 from original transaction `8e45c566-49eb-452e-b5cc-c10f40e762fa` and session `019f8add-bd2b-7662-aca1-a15fe0da1cf1` into accepted commit `543615bd0cd70e8cd56d70e4a508c076954e6cb9`. The original seven events remain byte-identical and the application now verifies `CONSISTENT` at `integration_pending`.
