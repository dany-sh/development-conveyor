# Discovery record — 2026-07-17

## Initial state

- `${HOME}/Developer/development-conveyor` did not exist.
- The repository was initialized with default branch `main`; implementation proceeded on `codex/development-conveyor`.
- No application repository was used for implementation or automated tests.

## Existing Factory interfaces inspected

The implementation inspected the installed `app-bootstrap`, `feature-inventory`, `architecture-audit`, `feature-factory`, `milestone-integrator`, `milestone-gate`, and `portfolio-status` skills; their deterministic queue, Git transition, integration, recovery, gate, and portfolio scripts; and the installed `portfolio-lead`, `product-architect`, `repository-explorer`, `feature-worker`, `test-engineer`, `adversarial-reviewer`, `release-auditor`, `feature-factory-orchestrator`, and `milestone_integrator` agent definitions.

Key integration conclusions:

- Queue and adapter files are JSON-compatible YAML.
- The controller owns deterministic creation and verification of the selected feature branch plus the outer writer-lease lifetime. Feature Factory owns queue-state reconciliation on that branch, application implementation, review, acceptance, and lease heartbeats under the matching agent-run identity.
- Milestone Integrator owns accepted-commit discovery, cherry-pick integration, combined validation, and recovery.
- Milestone Gate owns deterministic gate evidence and requires a read-only release-auditor result.
- The controller must coordinate these surfaces, not embed a second application writer or integration implementation.

## Model policy and Codex configuration

- Global interactive default: `gpt-5.6-sol`, medium, from `~/.codex/config.toml`.
- Portfolio launcher agent: `gpt-5.6-terra`, medium, aligned with `portfolio-lead` policy.
- Role-pinned repository agents retain their installed model files.
- Goal Mode was verified enabled using `codex features list`: `goals stable true`.
- Repository-scoped launchers now inject the action's role-pinned model and reasoning explicitly after validating the exact resolved CLI against the installed model catalog. This prevents global-config drift and unsupported catalog variants from reaching a feature cycle.

## CLI compatibility repair evidence

- The failed P0-001 run did not persist `codex --version`. A retained pre-upgrade executable reported `codex-cli 0.137.0`, and the launcher symlink changed after the failed run to the retained `0.144.5` release.
- The current exact resolved Conveyor executable is the standalone `0.144.5` binary. It exposes `gpt-5.6-sol` with `high` reasoning through `codex debug models`.
- The terminal `turn.failed` event is authoritative: it classifies the old run as `cli_upgrade_required`. Optional MCP authentication warnings remain secondary diagnostics.
- P0-001 has no feature branch, worktree, writer lease, accepted commit, Git operation, or repository mutation. Its verified start and milestone pre-integration commit are both `826de2ab2c517e4bba53ed54f0cf2ddee50f38ef`.

## Registered repository evidence

### Case Manager

- Repository: `${HOME}/Developer/example-case-manager`
- Branches `codex/pre-factory-baseline` and `codex/p0-foundation` exist.
- Baseline `e4264c6539338320ace2245cc18b05d7150c1358` exists and is an ancestor of the milestone branch.
- Accepted P0-002 commit `4c43aa5cd870ddb4962eceb1fbe35c648efa3e18` exists and is the current milestone HEAD at deployment discovery.
- The repository was clean during the final pilot.
- The first pilot compared configured `P0` literally with legacy queue milestone `phase-0`. Queue parsing itself succeeded, but milestone filtering therefore reported zero features. Deterministic normalization now matches the identifiers without editing the application queue.
- The accepted P0-002 queue sentinel `SELF` is corroborated by registered commit `4c43aa5cd870ddb4962eceb1fbe35c648efa3e18`, milestone ancestry from baseline `e4264c6539338320ace2245cc18b05d7150c1358`, the queue stored in that commit, and feature specification/status/run-log evidence.

### Interview Companion

- Repository path was verified as `${HOME}/Developer/example-interview-companion` from its own factory adapter and product documentation.
- Active milestone `M0`, baseline `8f0d7e3111db316b677d098a621c019b7677c057`, and milestone branch `codex/m0-foundation` were verified independently.
- During read-only discovery, another repository workflow advanced F002 from an active dirty feature worktree to integrated state. No Development Conveyor command caused that change.
- Final discovery verified accepted F002 commit `3b30adaa1a9829d9e225728c2873a23512086e8b` and integrated commit `e229b116b2d0979b99449f11d2abb1e812479721`.
- A retained integration writer lease remained after its recorded process exited. Deployment preserved it and registered `human_decision_required`; no stale lock was removed on timestamp evidence.
