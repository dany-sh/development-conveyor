# ADR 0003 — Tiered validation authority

Status: accepted for M2-000 review

## Context

A flat configured command set makes ordinary feature work pay the same cost as
repository release validation. The controller needs a smaller delivery
boundary without weakening mutation, identity, or integration protections.

## Decision

An adapter may declare complete `feature`, `milestone`, and `release` command
arrays. Declaring any tier requires all three; malformed or empty tiers fail
closed. An adapter without `validation_tiers` keeps its exact legacy flat
build, test, lint, package, and validation command order.

Feature validation selects affected production tests, tests identified by the
feature specification, and six fixed invariants covering configuration,
writer leases, prohibited actions, repository/ref state, queue selection, and
workflow transitions. It also compiles source, validates configuration, and
checks the diff.

Milestone validation adds fixed cycle, integration, two-ref recovery,
reconciliation, result-contract, post-integration, cache, and retained-result
coverage. When comparison is required, the prepared parent and candidate are
each observed exactly once. Integration passes immutable refs through the
environment without changing configured command identity.

Release validation alone runs complete discovery. An explicit `--repeat`
value is reserved for a deliberate nondeterminism investigation. Release debt
must reconcile exact record identity and exact failure/error outcome against
the catalog.

Every release complete-suite execution writes a new UUID-bound raw stdout and
stderr pair with exclusive creation and restrictive permissions. Its separate
metadata is durable before launch and records child completion plus raw hashes
before parsing. Parser success or failure is then persisted independently;
failure cannot overwrite the raw evidence.

## Consequences

- Feature and milestone gates invoke zero complete suites.
- Known debt remains visible and non-accepting.
- Release evidence is no-overwrite and parser-failure durable.
- Legacy adapters retain caller-level behavior.
- Typed leases, path authorization, prohibited-action enforcement,
  repository/ref and candidate checks, compare-and-swap semantics, and
  application isolation are unchanged.
