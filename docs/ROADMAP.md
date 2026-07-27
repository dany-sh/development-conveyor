# Roadmap

## Current milestone

M1 replaces phase-specific mutable-state authority with an append-only transaction ledger and rebuildable projections. M1-001 is the sole integrated feature; its post-integration repair/finalization commits remain distinct from the immutable accepted feature commit.

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

## Later milestones

- M2 may simplify or remove compatibility caches after all registered repositories have migrated.
- Default-branch integration remains a separate explicit human workflow and is not part of M1.
