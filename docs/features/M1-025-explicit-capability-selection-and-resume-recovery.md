# M1-025 — Explicit capability selection and failed-resume recovery

## Status

Review.

## Problem

M1-023 treated every Swift application context as requiring three
build-macos-apps skills. Pinned Codex CLI 0.145.0 cannot load those cached
plugin skills into its standalone prompt, so otherwise valid feature sessions
failed closed before model launch. After that failure, historical planning and
integration recovery scans also intercepted the newer feature failure instead
of allowing its ordinary recovery route.

## Contract

- Feature execution always requests the core Conveyor and Feature Factory
  skills.
- A build-macos-apps skill becomes required only when the selected feature
  contract names its exact capability identifier. Source-language discovery
  alone never creates a capability requirement.
- Explicitly declared but unavailable skills continue to fail closed before
  model launch.
- Historical planning or integration failures are eligible for recovery only
  when they own the latest projected terminal transaction, except for the one
  existing authenticated failed-planning-recovery supersession.
- A later feature failure therefore cannot reopen unrelated historical
  recovery evidence.

## Acceptance evidence

- A Swift feature without an explicit capability declaration requests exactly
  `core:apply_patch`, `core:shell`, `development-conveyor`, and
  `feature-factory`.
- An exact build-macos-apps identifier in the feature contract is preserved in
  the requested allowlist and remains subject to pinned-CLI proof.
- The live F078 capability preflight proves the exact four-capability
  requested/effective allowlist without launching a model.
- Read-only status recognizes the stale compatibility cache as a deterministic
  zero-model recovery instead of raising historical planning or integration
  recovery errors.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
