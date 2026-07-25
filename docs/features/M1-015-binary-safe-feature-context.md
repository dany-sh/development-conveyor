# M1-015 — Binary-Safe Feature Context and Prelaunch Recovery

## Status

Review on `codex/m1-15-binary-safe-feature-context`.

## Failure trace

F070 context selection entered through the broad repository traversal in
`cost_policy._application_feature_context_pack`. That traversal treated any
filename whose stem contained `test` as a test path, including generated
screenshots below `output/`. It first selected
`output/native-final/test-call-setup-2.png`, followed by
`output/native-final/test-call-setup.png`.

The selection reader used `Path.read_text(..., errors="replace")`, which
lossily converted the PNG for relevance scoring. The later prompt reader,
`SessionLauncher._focused_feature_context`, used strict
`Path.read_text(encoding="utf-8")`; the first PNG therefore raised the raw
`UnicodeDecodeError`. These were two independent unsafe readers on one context
path: one silently lossy and one path-free on failure.

## Context contract

`context_pack.read_context_file` is the shared byte-first reader. It:

- recognizes known binary signatures including PNG, NUL-containing payloads,
  and control-heavy payloads before decoding;
- bounds textual files at 512,000 bytes and streams SHA-256 computation;
- decodes textual candidates only as strict UTF-8;
- raises `ContextReadError` with repository-relative path, phase,
  classification, and bounded diagnostic for invalid UTF-8;
- represents relevant binary assets only as path, detected type, byte size,
  and SHA-256 metadata; and
- never embeds binary bytes, base64, lossy replacement text, or oversized text.

Generated and historical directories including `output/`, `.build/`,
`DerivedData/`, `.git/`, caches, reports, and runtime output are excluded from
ordinary feature context before reads. Explicit legitimate assets remain
eligible for bounded binary metadata.

Every context pack reports included text paths, generated exclusions with
reasons, binary metadata, oversized paths, typed failures, total textual
bytes, approximate tokens, and a deterministic SHA-256 fingerprint.

## Prelaunch lifecycle and recovery

Autopilot emits `FEATURE_CONTEXT_STARTED`, then `FEATURE_CONTEXT_READY` only
after a complete fingerprinted pack exists. `FEATURE_SESSION_STARTED` is
emitted only after the launcher returns an authenticated session identity and
the workflow kernel records `SessionLaunched`.

A context failure terminalizes as
`FEATURE_PRELAUNCH_CONTEXT_FAILED` in `feature_preparing`, with no session,
implementation attempt, human gate, or feature commit. The dedicated
`recover-feature-prelaunch` route authenticates the failed Autopilot report,
exact six-event execution transaction, zero session/report/commit evidence,
clean prepared branch and HEAD, matching milestone ref, ready queue feature,
and absence of Git operations, leases, reservations, and ownership.

Dry-run is write-free. Apply uses one recovery writer lease, appends
zero-model deterministic recovery evidence, refreshes projection,
compatibility, and cycle caches, preserves the selected clean feature branch,
and returns to `feature_preparing` for normal feature execution. It does not
reconcile the queue, run an application command, or create a planning or
feature commit.

## Validation boundary

Focused context-pack, session-event, Autopilot, recovery, projection,
consistency, retained-result, cache, and post-integration planning regressions;
Python compilation; controller configuration and queue validation; CLI help
and exact-mode rejection; and `git diff --check`. The complete controller
suite, application tests/builds, live recovery apply, ordinary resume, and
Autopilot restart remain excluded.
