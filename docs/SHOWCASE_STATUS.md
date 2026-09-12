# Showcase Status

This repository is an archived engineering showcase, not an active controller
or a production release.

## Public snapshot

- Snapshot date: 2026-09-11
- Registered application repositories: none
- Included runtime state: none
- Included reports and logs: directory placeholders only
- Public Git history: audited canonical development lineage beginning July 17, 2026
- Commit attribution: normalized to the owner's GitHub noreply identity

## Validation

The public snapshot passed:

- Python compilation for `src` and `scripts`;
- JSON parsing for the factory adapter, approved-content registry, controller
  configuration, project registry, execution profiles, and feature queue;
- `scripts/conveyor validate-config` with zero registered projects;
- configuration-focused unit tests;
- redaction-focused unit tests;
- `git diff --check`;
- a public-content scan with no owner path, personal email, detected secret,
  credential, private key, or unexplained binary in the published tree.

The complete historical test suite is not green. On 2026-09-11 it executed
778 tests in 855.003 seconds and reported 18 failures and 31 errors. The
repository already records 37 unresolved baseline outcomes in
`docs/testing/KNOWN_TEST_DEBT.json`; the archived public configuration also
removes the live project registrations, controller state, and integration
worktrees that several historical tests expected.

Those failures are retained as engineering evidence. They are not waived,
converted into expected failures, or hidden by deleting assertions. Do not
treat this snapshot as production-qualified.

## Publication boundary

Only the audited canonical lineage and curated final `main` snapshot are
intended for GitHub. Experimental branches, stashes, local controller state,
linked application repositories, and the private all-refs archive remain
outside the public boundary.
