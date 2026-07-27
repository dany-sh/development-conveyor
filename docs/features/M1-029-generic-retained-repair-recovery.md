# M1-029: Generic retained repair recovery

## Status

Ready. The exact feature branch is
`codex/m1-29-generic-retained-repair-recovery`; deterministic feature
preparation must bind its integration base before implementation.

## Goal

Recover a passing retained candidate deterministically after bounded repair
attempts without relying on one feature's identifiers, paths, command arrays,
or diagnostic wording.

## Evidence contract

- Ledger `ValidationFailed` events and immutable repair reports must support an
  initial structured-result-invalid attempt followed by a valid
  `FEATURE_ACCEPTED` result, a chain of host-validation-failed attempts, and a
  mixed chain.
- Every attempt must remain linked through the authenticated repository,
  project, feature, run, transaction, session, branch, HEAD, retained paths,
  candidate fingerprint, and ordered event topology.
- The trusted-host command plan is derived from authenticated evidence. The
  recovery implementation may not supply feature-specific IDs, paths, command
  arrays, or diagnostic strings.
- The final passing retained candidate is the only candidate eligible for
  deterministic validation, one direct-child accepted feature commit, bound
  gate supersession, and projection to `integration_pending`.

## Decision gates

- No product decision is outstanding; M1-028 is integrated and this feature is
  dependency-ready.
- Any repository identity, path, fingerprint, topology, ownership, or command
  mismatch fails closed before validation, commit creation, gate supersession,
  or projection mutation.
- Missing, reordered, unauthenticated, contradictory, or feature-specific
  command evidence remains a technical recovery gate and preserves the
  retained candidate.
- Recovery launches zero models and zero child sessions and never performs
  milestone integration.

## Acceptance criteria

1. Structured-result-invalid plus valid `FEATURE_ACCEPTED`,
   host-validation-failed, and mixed attempt chains are covered by focused
   deterministic regressions.
2. The ordered host-validation command plan is derived only from authenticated
   ledger and report evidence.
3. Identity, path, fingerprint, topology, ownership, and command drift each
   fail closed before mutation.
4. A passing retained candidate completes deterministic recovery and stops at
   `integration_pending`.
5. The production recovery implementation contains no live feature-specific
   IDs, paths, command arrays, or diagnostic strings; focused regressions use
   disposable synthetic evidence.
6. Every configured validation command in `.factory/project.yaml` passes,
   together with the focused retained-repair recovery suite and
   `git diff --check`.

## Execution policy

```yaml
execution_policy:
  profile: ambiguous_or_authoritative
  parent_sessions: 1
  child_sessions: 0
```
