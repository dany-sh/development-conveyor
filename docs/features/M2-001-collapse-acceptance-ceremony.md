# M2-001: Collapse Acceptance Ceremony

## Status

- Factory status: Review — implementation and bounded feature evidence complete;
  milestone integration pending
- Planning base:
  `598f06837b89221fe37add2b7db5132f63acbb43`
- Proposed feature branch:
  `codex/m2-001-collapse-acceptance-ceremony-reconciled`
- Branch state: implementation candidate; leave unintegrated

## Objective

Replace the remaining bootstrap and duplicated acceptance paths with one
supported acceptance entry point that consumes tiered evidence while keeping
the accepted implementation ref immutable and separating controller-owned
acceptance metadata from implementation history.

## Bounded scope

1. Provide one supported acceptance entry point that consumes authenticated
   feature, milestone, and release-tier evidence appropriate to the requested
   transition.
2. Keep the accepted implementation ref exactly at the implementation commit.
   Acceptance must not create a metadata child on, advance, rewrite, or replace
   that immutable ref.
3. Store controller-owned acceptance metadata separately from the accepted
   implementation ref, with explicit implementation commit and tree binding.
4. Resolve feature dependencies cumulatively across completed milestones
   rather than only from the active milestone's `integrated_features`.
5. Derive the release test inventory and evidence identity from the exact
   candidate commit and tree. Do not encode hard-coded test-count
   expectations.
6. When parsing or evidence reconciliation fails after a suite completed,
   preserve and reuse the candidate-bound raw artifacts and execution metadata
   for offline repair. Do not rerun the suite merely to retry parsing or
   evidence assembly.
7. Keep ordinary feature acceptance on the feature tier; it must perform zero
   complete-suite runs. Complete discovery remains release-tier-only.
8. Remove the obsolete one-time bootstrap path and the duplicated acceptance
   ceremony after the supported entry point covers their authenticated
   transitions.
9. Preserve or strengthen all existing lease ownership, immutable-ref,
   implementation-tree, compare-and-swap, changed-path, prohibited-action, and
   application-repository isolation protections.

## Acceptance criteria

1. A single documented command/API accepts an exact implementation commit and
   tree using authenticated tiered evidence and produces one deterministic
   acceptance result.
2. A successful acceptance leaves the accepted implementation ref exactly at
   the implementation commit and records controller-owned metadata on a
   separate authority surface.
3. Ref drift, tree drift, stale or foreign leases, compare-and-swap failure,
   unauthorized changed paths, malformed evidence, and application identity
   disagreement fail closed before acceptance metadata changes.
4. Dependencies integrated in earlier milestones satisfy later-milestone
   features without duplication or active-milestone-only lookup.
5. Release inventory is computed from the exact candidate. Tests prove that
   inventory changes are handled without fixed aggregate-count assertions.
6. Preserved raw artifacts and execution metadata can be reparsed and
   reconciled after parser/evidence failure without launching another suite.
7. Ordinary feature acceptance records zero complete-suite and zero release
   validation invocations.
8. The obsolete bootstrap and duplicate acceptance paths are removed, with no
   remaining supported caller depending on them.
9. Focused regression coverage proves lease, ref, tree, CAS, changed-path, and
   application-isolation protections remain intact.

## Branch plan

1. Create `codex/m2-001-collapse-acceptance-ceremony-reconciled` directly from
   `598f06837b89221fe37add2b7db5132f63acbb43` only when reconciliation is
   explicitly authorized.
2. Before edits, verify the exact base, clean worktree, absent writer and
   integration leases, and unchanged accepted implementation ref.
3. Keep one production writer and one cohesive implementation commit for
   M2-001. Do not amend or add commits to the M2-000 accepted implementation
   ref.
4. Validate with focused M2-001 specification and feature-tier checks during
   ordinary acceptance. Run release-tier validation only under separate,
   explicit release authority.
5. Stop after accepted-feature evidence; integrate through the configured
   milestone workflow only under separate integration authority.

## Non-goals

- No M2-000 metadata rewrite.
- No changes to Interview Companion or Case Manager.
- No changes to the parked `4f6aa96` branch.
- No cleanup of old M2 branches or worktrees.
- No complete unittest discovery, release validation, baseline equivalence,
  application tests, push, publication, deployment, tag, or release.
