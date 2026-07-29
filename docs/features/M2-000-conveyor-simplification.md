# M2-000: Conveyor simplification and validation reduction

## Status

- Factory status: Accepted by one-time operator bootstrap; milestone integration pending
- Accepted implementation branch: `codex/m2-000-accepted-implementation`
- Historical bootstrap metadata branch:
  `codex/m2-000-conveyor-simplification-final-v2`
- Prepared parent: `abf511f7381cc9042958691485966e055070ac4a`
- Accepted implementation: `e9b9e9aca00352fbd0fa137511d342803d7951a4`
  (tree `90ba7e397b3ac43edd7279bbd1f9ec1ba538572c`)
- Integration status: pending; no integration is authorized in this feature run.

## Objective

Replace flat repository-wide acceptance with explicit feature, milestone, and
release validation tiers so ordinary delivery proves the affected behavior and
fixed safety invariants without paying release-suite cost.

## Acceptance criteria

1. Feature validation selects affected tests, specification tests, and six
   fixed invariants; it invokes no complete discovery and completes in under
   three minutes.
2. Cycle execution and retained-feature finalization consume feature-tier
   authority, while milestone integration, recovery, and session-result
   validation consume milestone-tier authority.
3. Milestone validation adds combined controller, integration,
   reconciliation, recovery, lease, result-contract, and ref coverage; it
   invokes no complete discovery and completes in under ten minutes.
4. Immutable prepared-parent and candidate observations occur once per ref
   only when tests, routing, environment, fixture authority, or known
   milestone debt makes comparison necessary.
5. Release validation alone runs complete discovery. Repetition requires an
   explicit release-only `--repeat` value.
6. Adapters without `validation_tiers` preserve their existing flat configured
   command order at every caller.
7. Release debt reconciliation requires exactly 37 unique record identities.
   Each record remains classification-exact unless that individual record
   declares a non-empty rationale and an explicit permitted candidate outcome
   set containing its historical baseline. Baseline and candidate outcomes
   remain separately visible; classification flexibility never waives, skips,
   expects, or accepts a nonzero record.
8. Writer leases, authorized paths, prohibited actions, repository and ref
   identity, candidate identity, compare-and-swap behavior, and application
   isolation remain unchanged or stricter.

## Focused coverage

The focused validation-tier module covers tier selection, legacy fallback at
each caller, affected-test routing, conditional ref observation, SafetyPolicy
invocation and prohibition, authoritative runtime command binding, and exact
debt identity/outcome reconciliation. It also covers real verbose and summary
unittest headings, quoted multi-parameter subtests, accepted bracket
identities, fail-closed parsing and duplicate detection, raw hash/debt
authority with redacted persisted tails, and exact prepared-parent versus
synthetic isolated-candidate debt maps. Existing test methods also cover
Python 3.9 verbose class-only containers and UUID-bound, exclusive,
parser-failure-durable release artifacts without increasing discovered test
count. When verbose and summary failure/error forms coexist, their normalized
identity-to-outcome maps must match exactly.

## Debt evidence disposition

`docs/testing/KNOWN_TEST_DEBT.json` reconciles exactly to the prepared-parent
baseline artifact
`/private/tmp/m1-029-base.jQRS70/feature-full.out` (SHA-256
`dbdac38970df795ca6c409876829138280f110e1bfe24de09cb3979811fbcffe`):
723 tests, 21 failures, 16 errors, and 37 unique exact identities/outcomes.

The sole isolated reconstruction release observation is separate and rejected:
745 tests, 20 failures, 17 errors, `valid: false`, and exactly one complete
suite. One identity transitioned from baseline `failure` to isolated `error`:
`test_post_transition_cycle_cache_repair.PostTransitionCycleCacheRepairTests.test_unrelated_branch_and_changed_hash_fail_closed`.
The isolated worktree intentionally lacks the excluded
`state/projects/interview-companion/evidence-ledger.jsonl` controller fixture.
No fixture was copied or repaired, and the release was not rerun.

Strict acceptance remains blocked because the isolated observation does not
reproduce the catalog's authoritative 21/16 prepared-parent baseline.

The post-review feature and milestone gates pass without complete discovery.
The final release observation is reserved for the orchestrator and was not run
by the feature worker.

A user-supplied manual result of 748 tests in 945.536 seconds, 20 failures,
17 errors, and exit code 1 is historical unauthenticated context only: its raw
output was overwritten. M2-000 stays in review until one current release run
authenticates the fixed parser, raw artifacts, metadata lifecycle, and exact
debt reconciliation.

Post-manual focused validation passed 28 tests in 4.759 seconds. Feature
validation passed 25 tests in 4.630 seconds; milestone validation passed 39
main tests plus one 20-test observation per ref in 125.273 seconds. Both tiers
ran zero complete suites.

## Final candidate evidence

- Exact parser/debt/redaction checks: 5 of 5 passed.
- Validation-tier module: 28 of 28 discovered tests passed; the final
  parser-only repair added zero discovered test IDs.
- Feature tier: 25 tests, four commands, 5.318 seconds, zero complete suites.
- Milestone tier: 79 total tests (39 main plus 20 per ref), 125.273 seconds,
  zero complete suites.
- Final review: zero unresolved Critical, High, or Medium findings.
- Release at preflight: authenticated evidence was pending. The later
  authenticated execution and offline reconciliation are recorded below.

The rejected isolated count was 745 tests. The later user-supplied manual count
was 748 tests and predates the final parser-only repair. Because its raw output
was overwritten and the final capture is empty, the exact three added test
identities and the reason for the increase cannot be reconciled from preserved
read-only evidence and remain unresolved. The manual result is not acceptance
evidence.

## Authenticated release reconciliation follow-up

The authenticated release execution
`0b90e31ee17a46eaacf0ff8d2fca1f64` ran against implementation commit
`029d0a08b1b00c151840e6f268a3c66103c1aae4` and tree
`ea0d11135f7e81dff1d6cc7b6edbbc9978fa8e11`. Reflog, worktree, timestamp,
and ancestry evidence place the complete 15:27–15:45 PDT execution after that
commit and before any follow-up mutation.

Offline reconciliation of the unchanged raw artifacts and external release
JSON is valid: 748 tests, 37 exact identities, candidate totals of 20 failures
and 17 errors, historical baseline totals of 21 failures and 16 errors, and
zero missing, added, duplicate, pending, or unparseable identities. Exactly
one classification shift is explicit:
`test_post_transition_cycle_cache_repair.PostTransitionCycleCacheRepairTests.test_unrelated_branch_and_changed_hash_fail_closed`
remains historically `failure` and permits candidate `failure` or `error`
because the authenticated worktree intentionally lacks the excluded generated
evidence-ledger fixture. The other 36 records remain classification-exact.

The immutable offline artifact
`release-reconciliation-0b90e31ee17a46eaacf0ff8d2fca1f64.json` has SHA-256
`df95608b65baf709c55083ee0e2d8612ef68801ced00bdce9552b8c58450ecd6`.
It binds the release execution, raw stdout and stderr hashes, external JSON
hash, implementation commit and tree, debt-catalog fingerprint,
reconciliation-code fingerprint, and exact permitted shift. It contains no
local user or temporary absolute paths. The original release JSON, execution
metadata, and raw artifacts remain unchanged.

Acceptance remains a human decision. The supported deterministic
accepted-commit finalizer requires its candidate implementation commit to be
the exact direct child of the configured milestone base. This reconstructed
branch already contains the authenticated implementation at `029d0a08`, and
the requested reconciliation-only follow-up must be its direct child. The
current finalizer cannot bind that two-commit evidence split without changing
the configured integration base or rewriting accepted history. Neither is
authorized. No additional release suite was run.

## Execution policy

```yaml
execution_policy:
  profile: ambiguous_or_authoritative
  parent_sessions: 1
  child_sessions: 0
```
