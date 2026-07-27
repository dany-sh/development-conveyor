# M1-023 — Runtime policy and context discipline

## Status

Review.

## Problem

Conveyor launch plans already bind an exact model, reasoning effort, parent
budget, child budget, and policy source, but the generic application fallback
still selected a controller-oriented Sol profile and an older exact
application profile named a model absent from pinned Codex CLI 0.145.0.
Sessions also inherited the broad interactive skill, plugin, and MCP surface,
and output concision was advisory rather than a stable machine contract.

## Runtime contract

- Deterministic routes launch zero models and zero children.
- Mechanical and repository-aware preparation uses Luna.
- A bounded application implementation defaults to Terra/medium; a selected
  feature profile remains authoritative over the workflow fallback.
- Generic controller or architecture work uses Sol/medium. Sol/high is
  reserved for recorded ambiguity or durable authority. Xhigh requires a
  concrete failed or unresolved high-reasoning record and cannot be selected
  on the first attempt.
- Exact unavailable models fail closed without alias or family substitution.
- Child budget defaults to zero and is capped at one. A positive budget
  requires a bounded task, materially smaller context, a strictly cheaper
  configured model, compact output, exclusive file ownership or read-only
  scope, and a concrete cost-saving justification.

Every model-backed route receives a Conveyor-scoped capability allowlist. The
launcher disables every other model-visible skill by exact path, disables
unrelated plugins and the complete MCP server map through session config, and
uses the pinned CLI's model-free `debug prompt-input` surface to prove the
effective skill set before launch. Unsupported or incomplete isolation returns
`capability_isolation_unsupported` and launches no model. Global skills,
plugins, caches, and unrelated interactive sessions are unchanged.

Feature context includes the selected specification, exact dependency
specifications, repository instructions, adapter and architecture contracts,
directly relevant source and tests, execution profile, and terminal contract.
It records count, approximate bytes, paths, reasons, and exclusions. Queue
reconciliation deterministically limits semantic candidate specifications
instead of loading every unresolved feature.

Every launched request carries one compact JSON terminal contract with exact
workflow identity, model-policy identity, session counts, changed paths,
validation, errors, safety findings, blocker, commit, next state, and recovery
instruction. Successful command output is summarized by command, exit code,
duration, output hash, and bounded tail or summary.

## Runtime audit

`scripts/conveyor audit-runtime --project PROJECT` is read-only. It launches no
model or child, acquires no lease, creates no transaction, changes no
application or controller file, and reports the effective executable, catalog,
canonical model policy, selected route and profile, capability enforcement,
context pack, compact-output contract, repository identity, and zero mutation
counters. Policy failures still emit the complete report and exit nonzero.

## Acceptance evidence

- Focused model-policy, execution-profile, launcher, context, capability,
  compact-output, runtime-audit, M1-021, and M1-022 regressions pass.
- `validate-config` and the global model-policy validator pass.
- Live runtime audits for Interview Companion and Case Manager prove the
  requested/effective capability allowlists without a model launch.
- Interview Companion status and consistency remain read-only and
  `CONSISTENT`.
- Both application repositories and all protected controller-state hashes are
  unchanged.

## Execution policy

```yaml
execution_policy:
  profile: generic_or_architectural
  parent_sessions: 1
  child_sessions: 0
```
