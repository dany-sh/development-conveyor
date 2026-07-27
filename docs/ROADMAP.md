# Roadmap

## Current milestone

M1 replaces phase-specific mutable-state authority with an append-only
transaction ledger and rebuildable projections. M1-001 and the historical
M1-028 queue-browsing work are recorded as integrated; M1-001's
post-integration repair/finalization commits remain distinct from its immutable
accepted feature commit.

M1-014 adds a fail-closed recovery boundary for a valid post-integration
planning parent whose named nested roles were unavailable before inspection.
It authenticates recorded validation and the retained planning diff in
dry-run, reruns validators only on apply, and stops at the recovered
feature-ready selection.

M1-009 adds an approved product-plan reconciliation boundary before ordinary
queue reconciliation. It reuses the transactional kernel for a brief-bound,
planning-only one-parent/zero-child session, exact semantic validation, one
planning commit, transactional cache finalization, and zero-model retained-diff
recovery.

M1-026 makes ordinary validated-queue selection deterministic and adds
read-only queue inspection, queue-only reprioritization, exact eligible feature
selection, and an operator pause flag in the existing project runtime
authority. Exceptional model-backed product-plan reconciliation remains
explicit rather than part of routine run or resume.

M1-029 is the next dependency-ready controller feature. It generalizes retained
repair recovery across structured-result-invalid, host-validation-failed, and
mixed attempt chains while preserving evidence-derived commands and fail-closed
identity, topology, ownership, path, fingerprint, and command checks.

## Later milestones

- M2 may simplify or remove compatibility caches after all registered repositories have migrated.
- Default-branch integration remains a separate explicit human workflow and is not part of M1.
