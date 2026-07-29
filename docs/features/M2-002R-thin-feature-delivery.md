# M2-002R — Thin Feature Delivery Orchestrator

## Objective

Provide one bounded `deliver-feature` command that invokes the existing feature
validation, acceptance, and deterministic integration authorities in order.
Delivery is orchestration only: it owns no lease, ledger, transaction, Git, or
validation behavior.

## Acceptance criteria

- `scripts/conveyor deliver-feature --project PROJECT --feature FEATURE`
  invokes registered feature validation, `accept-feature`, and
  `integrate-feature` exactly once and in that order.
- `integrate-feature` consumes one exact completed ledger acceptance, resolves
  the configured distinct integration worktree, verifies dependencies, creates
  an immutable plan, invokes the deterministic executor, records integration
  completion, and releases its lease.
- Ledger-backed acceptance does not require queue acceptance materialization;
  the queue remains a deterministic projection.
- A dirty, missing, or drifting target fails closed. An advanced target returns
  `reconciliation_required` without Git mutation.
- Feature and milestone tiers never invoke complete unittest discovery or
  release validation.

## Focused validation

- `tests.test_feature_delivery.FeatureDeliveryTests.test_successful_validate_accept_integrate`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_validation_failure_stops_delivery`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_acceptance_failure_stops_integration`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_ledger_only_acceptance_is_forwarded`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_configured_integration_worktree_is_reported`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_feature_and_integration_worktrees_are_distinct`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_advanced_target_returns_reconciliation_required`
- `tests.test_feature_delivery.FeatureDeliveryTests.test_delivery_reports_zero_complete_and_release_invocations`

## Boundaries

No application tests, complete discovery, release validation, recovery, or
automatic reconciliation are part of this feature. The implementation does
not modify kernel, ledger, workflow lease/recovery, acceptance, or deterministic
integration executor semantics.
