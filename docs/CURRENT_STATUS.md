# Current Status

Last updated: 2026-07-22

## Summary

The M1-002 execution-profile increment is accepted on `codex/m1-2-feature-execution-profiles` from starting commit `2ce0a01cfd2b79e97921eca2a78a4210ec9614a5` and awaits milestone integration. The controller now resolves feature execution through deterministic zero-model routing, explicit run override, selected feature policy, validated workflow fallback, and recorded evidence-based escalation. Resolved model, reasoning, parent and child budgets, source, and escalation contract are inspectable before launch and bind the actual Codex argv.

The seven named profiles are schema-validated, newly readied features must provide an `execution_policy`, and historical ready features retain a validated fallback. Context discovery and verification planning remain independent from profile selection; planning-only application features fail before launch. No keyword scoring, per-run complexity arithmetic, or broad automatic escalation was added.

Interview Companion F004 has controller-owned reconciled metadata selecting `multi_module_precise` with one parent and zero children, and an optional escalation to `generic_or_architectural` only for recorded `material_architecture_or_authority_ambiguity`. Its dry-run resolves `gpt-5.6-terra` with high reasoning while retaining focused application context and all F004 acceptance criteria and final gates.

The dedicated zero-model feature-result recovery path has been exercised against Interview Companion F003. It preserved the original failed transaction `8e45c566-49eb-452e-b5cc-c10f40e762fa` and session `019f8add-bd2b-7662-aca1-a15fe0da1cf1`, recovered the exact existing 25-path implementation, and created accepted feature commit `543615bd0cd70e8cd56d70e4a508c076954e6cb9` on `codex/F003-single-window-application-shell`. The application worktree is clean, no writer lease remains, no model or child session ran during recovery, and milestone integration was not performed.

Host acceptance passed the three focused routing/lifecycle suites, debug build, all 112 tests, release build, staged-app verification, native Accessibility navigation across all seven destinations with exactly one resizable standard window, profile-aware content audit, feature inventory validation, documentation/ADR checks, and `git diff --check`. Interview Companion consistency now reports `CONSISTENT` with authoritative and compatibility state at `integration_pending`.

## Current milestone

M1 — Transactional workflow kernel

## Active feature

M1-002 — Cost-aware execution policy (`integration_pending`)

## Next boundary

Leave milestone integration for the configured human-reviewed workflow. Do not merge a default branch, push, tag, publish, deploy, release, run a real Conveyor resume, or begin F004 implementation from this task.

## Validation scope

Validation is limited to affected schema, profile-resolution, execution-plan, launcher, context, verification, planning-transaction, reconciliation, and CLI compatibility tests; `compileall`; configuration validation; both registered-project consistency checks; the Interview Companion resume dry-run; and `git diff --check`. The complete controller suite and any real `resume` command are intentionally excluded.
