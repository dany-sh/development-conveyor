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

<!-- CODEX_FACTORY_ADAPTER_BEGIN -->
# Development Factory repository adapter

- Read `.factory/project.yaml`, its `project_profile` and `content_policy`, `.factory/approved-content.yaml`, and `docs/AUTONOMY_CONTRACT.md` before factory work.
- Read `~/.codex/MODEL_POLICY.md`; use named role-pinned agents and record model execution evidence in `docs/RUN_LOG.md` without hidden reasoning.
- Treat repository product, architecture, feature, command, and Git documents as authoritative for this project.
- Run only commands verified in `.factory/project.yaml`; command entries are argument arrays, not shell strings.
- Use one primary production-code writer. Read-only exploration, test analysis, and review may run in parallel.
- Acquire the repository-shared writer lease before production edits. Create one immutable commit per accepted feature, then integrate it through the milestone branch with deterministic scripts.
- Never merge or push to the configured default branch automatically. Never publish or deploy automatically.
- Preserve intentional personalization under the repository profile. Personal names, company names, career vocabulary, personal workflows, prompts, test identities, bundle identifiers, and documented local paths are allowed by default for `personal_private` projects.
- Secrets are unconditional blockers and must be reported by path and category only, never by value. Do not silently sanitize or delete personal or sensitive content.
- Repository-local instructions override user-wide defaults where they are more specific.
<!-- CODEX_FACTORY_ADAPTER_END -->
