# Current Status

Last updated: 2026-07-22

## Summary

The transient cache-recovery provenance repair is prepared on `codex/m1-2-transient-cache-recovery-provenance` from starting commit `f85b84a9de80864dabb6105b8a3a152954817620`. Newly materialized compatibility cycle caches no longer persist `cache_binding_recovery`. Deterministic rebinding alone may normalize the one legacy object when it contains exactly non-empty `source_transaction` and `recovery_run_id` strings; every other unsupported top-level field and every malformed or extra legacy nested field still fails closed.

Completed Interview Companion planning recovery remains authoritative and is not repeated. Its paused/no-feature projection routes the stale compatibility cache to zero-model `cache_binding_recovery`; the later user-applied recovery will rewrite only ignored `.factory/conveyor-state.json` and stop.

The M1-002 accepted-commit finalization repair is prepared on `codex/m1-2-finalized-accepted-commit` from starting commit `b6d412553b3efc42637065dbba7d926aebc8aa8f`. Normal feature execution now preserves the model-produced commit as candidate evidence and deterministically reconstructs one authoritative direct-child accepted commit containing the validated implementation plus the established acceptance metadata. `accepted_commit: SELF`, exact parent/ref checks, authorized metadata-only tree comparison, and the existing immutable two-ref integration validation all pass before the controller may project `integration_ready`.

The seven named profiles are schema-validated, newly readied features must provide an `execution_policy`, and historical ready features retain a validated fallback. Context discovery and verification planning remain independent from profile selection; planning-only application features fail before launch. No keyword scoring, per-run complexity arithmetic, or broad automatic escalation was added.

Interview Companion F004 candidate `8ae5c94df59119d89d3c2ac6fd7508a47a426ff6` remains the complete historical implementation evidence. A protected zero-model recovery dry-run binds it to milestone base `7a754a4b4741b85ac51b8b516cebd25cae1eec69`, the exact feature ref, both completed transaction identities, all 213 existing ledger fingerprints, eight source/test paths, and five authorized metadata paths. Apply uses the normal finalizer, appends recovery evidence, refreshes projection/cache through the controller transaction, and stops before milestone integration.

The dedicated zero-model feature-result recovery path has been exercised against Interview Companion F003. It preserved the original failed transaction `8e45c566-49eb-452e-b5cc-c10f40e762fa` and session `019f8add-bd2b-7662-aca1-a15fe0da1cf1`, recovered the exact existing 25-path implementation, and created accepted feature commit `543615bd0cd70e8cd56d70e4a508c076954e6cb9` on `codex/F003-single-window-application-shell`. The application worktree is clean, no writer lease remains, no model or child session ran during recovery, and milestone integration was not performed.

Host acceptance passed the three focused routing/lifecycle suites, debug build, all 112 tests, release build, staged-app verification, native Accessibility navigation across all seven destinations with exactly one resizable standard window, profile-aware content audit, feature inventory validation, documentation/ADR checks, and `git diff --check`. Interview Companion consistency now reports `CONSISTENT` with authoritative and compatibility state at `integration_pending`.

## Current milestone

M1 — Transactional workflow kernel

## Active feature

M1-002 — Transient cache-recovery provenance (`controller repair prepared`)

## Next boundary

Commit the controller repair. The user may later run the already-planned cache-binding recovery, which must rewrite only the ignored compatibility cache and stop. Do not repeat planning recovery, merge a default branch, push, tag, publish, deploy, release, run a real Conveyor resume, or rerun F004 implementation.

## Validation scope

Validation is limited to affected cycle-cache, cache-binding recovery, planning-recovery, consistency, projection, and routing tests; `compileall`; configuration validation; Interview Companion consistency; the Interview Companion resume dry-run; and `git diff --check`. Application tests, the complete controller suite, a real `resume`, live cache recovery, and milestone integration are intentionally excluded.
