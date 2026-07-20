# Autonomy Contract

## Default mode

- Implement at most one accepted feature per run.
- Integrate an accepted feature into its milestone branch and prepare the next dependency-ready branch.
- Do not begin the next feature's production implementation unless this contract is explicitly changed to allow automatic continuation.
- Stop at every milestone boundary and human-decision gate.
- Stop before destructive migrations, external side effects, releases, scope changes, or a hard content-policy gate.

## Allowed without additional confirmation

- Read repository files and Git metadata.
- Preserve and use intentional personalization permitted by `project_profile` and `content_policy`.
- Create milestone and feature branches derived from factory metadata.
- Acquire and heartbeat one repository-shared writer lease.
- Edit files required by the active feature.
- Run commands verified in `.factory/project.yaml`.
- Update factory documentation, create one accepted feature commit, cherry-pick it into the milestone integration branch, record evidence, and prepare the next feature branch.

## Never automatic

- Merge into the default branch.
- Push, deploy, publish, tag, release, rotate credentials, or delete branches or worktrees containing unintegrated work.
- Force-push, rewrite an accepted feature commit, or auto-resolve semantic conflicts.
- Add unapproved production dependencies or broaden product scope.
- Print, commit, or approve a secret.
- Silently sanitize or delete personal, sensitive, fixture, migration, or generated content.
