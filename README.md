# Development Conveyor

Development Conveyor is an archived engineering showcase for a local-first,
multi-repository AI development controller. I built it to explore a difficult
question: how can autonomous coding sessions move real software forward without
confusing a model's claim, a passing process, or a mutable cache with durable
delivery evidence?

The result is a Python standard-library control plane that coordinates isolated
application repositories through explicit state machines, Git identities,
writer leases, append-only evidence, deterministic recovery, and human gates.
It does not contain or modify the applications it once coordinated. This public
snapshot ships with an empty project registry and is no longer operated as an
active controller.

## What this demonstrates

- **Evidence-first orchestration.** Every writable phase is bound to an exact
  repository, branch, starting commit, transaction, session, and mutation scope.
- **Fail-closed recovery.** Interrupted work is resumed only when Git, ledger,
  projection, lock, and queue evidence agree.
- **Repository isolation.** Application code remains owned by its own repository;
  the portfolio controller cannot become a cross-project production writer.
- **Safe concurrency.** Controller reservations prevent duplicate launches while
  repository-scoped typed leases preserve single-writer authority.
- **Deterministic integration.** Accepted candidates, integration commits,
  validation evidence, and milestone state are authenticated independently.
- **Human-controlled effects.** Default-branch merges, pushes, releases,
  deployments, and destructive recovery remain outside autonomous authority.
- **AI workflow engineering.** Model-backed judgment is separated from
  deterministic state transitions, validation, redaction, and audit logic.

## Architecture

```text
CLI / scheduler
      |
      v
execution planner -----> repository + queue + Git inspection
      |
      v
transactional kernel -----> typed writer lease
      |                           |
      |                           +-> mutation boundary
      v
append-only ledger -----> projection engine -----> rebuildable cache
      |
      +-> reports, validation evidence, and recovery decisions
```

The evidence ledger is canonical. Projection and compatibility files are
rebuildable views. A transition succeeds only when its preconditions and
terminal evidence match; process exit codes and assistant prose are never
sufficient on their own.

The controller includes dedicated flows for:

- queue reconciliation and feature selection;
- feature preparation, execution, review, repair, and acceptance;
- milestone integration and validation;
- retained-result and interrupted-transaction recovery;
- capability isolation and model-routing preflight;
- durable stop requests and bounded unattended operation;
- redacted reporting and consistency verification.

See [Architecture](docs/ARCHITECTURE.md),
[Product Vision](docs/PRODUCT_VISION.md), and
[Validation](docs/VALIDATION.md) for the deeper design record. The exact
archival validation boundary is recorded in
[Showcase Status](docs/SHOWCASE_STATUS.md).

## Why I built it with AI

This repository records an experiment in treating AI coding agents as bounded
participants in an engineering system—not as the source of truth. AI sessions
helped explore designs, implement features, review candidates, and repair
failures. Deterministic code retained authority over identities, state
transitions, allowed paths, validation, and integration.

Several design choices came directly from failures observed during development:

- stale sessions must not resume against a changed branch;
- a clean worktree does not prove that work was integrated;
- a model saying “done” does not prove accepted functionality;
- a dead PID does not by itself make a writer lease stale;
- cached state cannot override an append-only evidence history;
- retries need a new hypothesis, not another identical attempt.

The repository intentionally retains its architecture decisions, feature
specifications, test requirements, and run log as evidence of that iterative
AI-assisted engineering process.

## Repository map

```text
src/development_conveyor/   controller, state, recovery, and validation logic
scripts/conveyor            command-line entry point
schemas/                    JSON schemas for durable contracts
tests/                      synthetic repositories and failure-boundary tests
docs/                       architecture, ADRs, feature history, and run evidence
config/                     portable example controller configuration
examples/                   non-production example inputs
```

Runtime state, reports, logs, and application repositories are not included in
the public snapshot.

## Running locally

Requirements:

- Python 3.9 or newer
- Git
- Codex CLI available as `codex` for model-backed flows

```bash
python3 -m pip install -e .
scripts/conveyor --help
scripts/conveyor validate-config
python3 -m unittest discover -s tests -q
```

The checked-in registry is empty. To experiment with the controller, add only
disposable or explicitly authorized repositories to `config/projects.yaml` and
follow the adapter contracts documented under `.factory/` and `docs/`.

## Safety model

The controller rejects automatic pushes, releases, deployments, destructive
resets, automatic stashing, unapproved branch deletion, and production-source
editing by the portfolio layer. Tests use disposable synthetic repositories and
should never target real application repositories.

This code is an archived showcase rather than a supported production service.
Review configuration, permissions, and repository policy before adapting it to
another environment.

## Status

- Development period: 2026
- Current state: archived showcase
- Registered application repositories: none
- Runtime data included: none
- Public history: audited canonical development lineage with normalized GitHub attribution
- Full historical suite: known unresolved debt; see
  [Showcase Status](docs/SHOWCASE_STATUS.md)

## License

Released under the [MIT License](LICENSE).
