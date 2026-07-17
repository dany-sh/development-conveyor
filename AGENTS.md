# Development Conveyor repository instructions

This repository is the authoritative implementation of the user-wide Development Conveyor. It coordinates application repositories; it does not contain or directly edit their production source.

## Boundaries

- Treat each registered application repository as an independent isolation boundary.
- The controller may read repository metadata and Git state, write only `.factory/conveyor-state.json` and the shared runtime lock through the repository-level execution engine, launch repository-scoped Codex sessions, and persist controller state, reports, and redacted events here.
- Application production code, feature documentation, queue changes, run-log changes, commits, and milestone integration must be performed by the existing repository-scoped factory skills and named agents.
- Never merge into a default branch, push, force-push, tag, publish, deploy, release, notarize, reset destructively, clean, auto-stash, force-checkout, delete unintegrated branches, or discard a conflict.
- Never begin a new milestone until the current milestone merge is verified and repository policy permits continuation.

## Development

- Keep configuration and queue files JSON-compatible YAML.
- Use only Python's standard library in production code unless a dependency is explicitly approved.
- Invoke subprocesses as argument arrays with `shell=False`.
- Redact subprocess output before persistence.
- Use atomic writes for state and lock files.
- Use disposable synthetic repositories for tests. Never point tests at registered production repositories.
- Run `python3 -m unittest discover -s tests -v` and `scripts/conveyor validate-config` before acceptance.
- Work on a non-default branch. Do not push, tag, publish, or deploy.

