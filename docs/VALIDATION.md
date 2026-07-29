# Validation

Development Conveyor uses feature, milestone, and release validation tiers.
Tiered adapters must declare all three. Adapters without tier configuration
retain the existing flat configured command set.

## Commands

```text
scripts/conveyor validate-feature --spec docs/features/M2-000-conveyor-simplification.md
scripts/conveyor validate-milestone --spec docs/features/M2-000-conveyor-simplification.md --prepared-parent COMMIT --candidate WORKTREE
scripts/conveyor validate-release
scripts/conveyor validate-release --repeat 2
```

`WORKTREE` is the explicit uncommitted candidate observation used during
feature review. Integration supplies immutable commit identities through
`CONVEYOR_PREPARED_PARENT` and `CONVEYOR_CANDIDATE`.

## Feature tier

The feature tier takes the union of affected source tests, specification or
explicit tests, and six fixed safety invariants. It then compiles `src` and
`scripts`, validates configuration, and runs `git diff --check`. It never
constructs `unittest discover`.

## Milestone tier

The milestone tier adds fixed controller-cycle, deterministic integration,
two-ref recovery, reconciliation, post-integration, cache, retained-result,
lease, and result-contract checks. Comparison is required only when test or
discovery inputs, validation routing, environment or fixture authority, or
known nonzero milestone debt changes. Each supplied ref is observed once.

## Release tier

Release is the sole complete-discovery route. Ordinary release runs once.
Additional observations require an explicit repeat count. Its output is
reconciled against `docs/testing/KNOWN_TEST_DEBT.json` by exact test identity
and exact `failure` or `error` outcome. A classified record remains unresolved;
classification is not a waiver, skip, expected failure, or acceptance.

Failure/error extraction accepts ordinary verbose results and unittest summary
headings. Real parenthesized subtests normalize quoted or unquoted parameters
to the catalog's bracket identity, while already accepted bracket identities
are preserved. Unparseable result headings and duplicate normalized identities
fail closed.

When a run contains both verbose result lines and summary headings, their
normalized identity-to-outcome maps must be exactly equal. A partial map or
outcome disagreement fails closed instead of preferring either form.

Python 3.9 verbose headings may place only `module.Class` in parentheses. The
display method is appended in that form; an identity already ending in the
same method remains unchanged.

The M2 catalog source is the prepared-parent baseline artifact, whose SHA-256,
test count, duration, summary, and exact record outcomes are stored with the
catalog. The isolated reconstruction observation is recorded separately and
never overwrites that baseline. Its one complete-suite run was rejected at
20 failures and 17 errors because an excluded controller fixture changed one
record from baseline failure to isolated error. No second release observation
was run during the focused repair; the final release observation is reserved
for the orchestrator after all pre-release gates pass.

A later user-supplied manual result reported 748 tests in 945.536 seconds,
20 failures, 17 errors, and child exit code 1. Its raw output was overwritten,
so it is historical unauthenticated context only and is not acceptance
evidence.

## Evidence

Each command record contains its argument array, exit status, duration, test
count when available, output hash, bounded diagnostic tail, and parsed
failure/error identities. Tier evidence also records changed paths, selected
tests, fixed core tests, comparison reasons and identities, command and test
counts, duration, and complete-suite invocation count.

The output hash and debt parser use the raw subprocess text. Only the bounded
persisted diagnostic tail is redacted, so authentication material, credential
fields, and documented absolute user-data paths are not retained there.

Each release complete-suite command also creates three unique repository-local
generated evidence paths:

```text
reports/release-validation-EXECUTION_ID.stdout.raw
reports/release-validation-EXECUTION_ID.stderr.raw
reports/release-validation-EXECUTION_ID.execution.json
```

The raw files and initial metadata use exclusive creation and mode `0600`.
Metadata is durable before child launch, then records child exit code and
per-stream SHA-256 hashes before debt parsing. Its final `parser_status` is
`succeeded` or `failed`; raw output and failure metadata survive even when no
final validation JSON can be emitted. Metadata stores no command arguments or
child output.

One current full release run is still necessary if strict M2 acceptance is
pursued because neither rejected/overwritten historical result authenticates
the fixed parser and evidence lifecycle. The feature and milestone gates do
not need complete discovery.
