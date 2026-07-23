# M1-002 — Cost-aware execution policy

## Objective

Plan workflows before mutable work so the controller uses deterministic routes, focused context and verification, and reusable evidence whenever those provide the required confidence.

## Acceptance criteria

1. Dry-run plans expose model routing, session budgets, context estimates, selected verification, skipped work, invalidation rules, and stopping criteria.
2. Deterministic status, consistency, queue parsing, dry-run planning, and complete deterministic integration select zero model sessions.
3. Model/reasoning selection follows deterministic route, explicit override, selected feature policy, workflow fallback, then recorded evidence-based escalation; Sol xhigh is available only through the validated `unresolved_after_high` profile.
4. Parent- and child-session budgets fail closed at the launcher boundary, and controller maintenance defaults to zero children.
5. Verification is changed-path and risk based; documentation and configuration changes do not trigger application builds or unrelated suites.
6. Evidence reuse requires a complete trusted identity match and rejects dirty or incomplete evidence.
7. Large command output is retained in a report file and represented in model context only by a summary.
8. Application feature execution binds one selected feature through the authoritative execution plan, branch preparation, kernel transaction, direct parent `SessionRequest`, exact prompt contract, terminal envelope, semantic validation, report, ledger, and projection.
9. Direct feature sessions receive their validated parent and child budgets from the resolved execution policy. Launcher preflight proves a zero-child policy can remove collaboration tools, then starts Codex with multi-agent features disabled; unsupported removal or an unenforceable positive child budget fails before process launch.
10. The feature result envelope carries exact project, repository, run, transaction, workflow, feature, launcher-observed session, branch, commit, changed-path, classification, next-state, and validation evidence. Null, legacy, placeholder, or mismatched identities are rejected.
11. A terminal structured-output failure may recover an exact existing implementation diff without another model only after immutable topology, report, branch, HEAD, path, queue, accepted-commit, and lease evidence agree. Host validation, acceptance metadata, the one feature commit, lease release, terminal projection, and compatibility cache materialization remain controller-owned.
12. Feature-owned `execution_policy` metadata validates profile, parent and child budgets, and an optional exact escalation trigger/profile. Newly readied features require it; historical entries may use a validated workflow fallback.
13. Dry-run exposes the resolved profile, resolution source, model, reasoning, budgets, escalation contract, focused source/test context, complete acceptance criteria, and final gates. Planning documents without relevant source/test context fail before launch.
14. Profile resolution never uses complexity arithmetic or risk keywords. Missing context, environment failures, and localized test omissions do not escalate; changing a profile requires recorded matching evidence.

## Scope

The policy is controller-only. It does not relax ledger, transaction, lease, Git, recovery, dry-run, or application-isolation requirements.

## Accepted implementation evidence

- `feature_cycle` launches a bounded direct parent session using the selected feature's validated execution profile. For Interview Companion F004, the controller-owned reconciled metadata resolves `multi_module_precise` to `gpt-5.6-terra` with high reasoning, one parent, and zero children; the CLI invocation removes both multi-agent feature surfaces under strict configuration.
- Named profiles and workflow fallbacks are schema-validated in `config/execution-profiles.yaml`; the attached governing policy is versioned separately under `docs/policies/` so explanatory scoring guidance cannot become runtime selection logic.
- The terminal contract is request-specific and semantically revalidated against the launcher-observed session UUID and live repository before the kernel accepts it.
- `recover-feature-result` implements read-only exact-evidence inspection and a separate zero-model apply path. The controller adopts only the proven dirty baseline, runs required host gates under a fresh `feature_writer` lease, preserves non-metadata implementation bytes, commits one direct-child feature result, terminalizes the transaction, releases the lease, and projects `integration_pending` without integrating.
- Live proof recovered Interview Companion F003 from original transaction `8e45c566-49eb-452e-b5cc-c10f40e762fa` and session `019f8add-bd2b-7662-aca1-a15fe0da1cf1` into accepted commit `543615bd0cd70e8cd56d70e4a508c076954e6cb9`. The original seven events remain byte-identical and the application now verifies `CONSISTENT` at `integration_pending`.
