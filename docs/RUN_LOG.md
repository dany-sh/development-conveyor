# Development Conveyor run log

This log records model configuration and deterministic deployment evidence. It never contains prompts, hidden reasoning, credentials, authentication metadata, tokens, or raw sensitive output.

### Model execution — 2026-07-17T08:54:36+00:00

- Agent role: `principal-platform-architect`
- Effective model: `gpt-5.6-sol`
- Effective reasoning effort: `medium`
- Configuration source: `global_default`
- Event: `start`
- Reason code: `direct_user_wide_controller_implementation`
- Safety and autonomy contracts unchanged: `true`

### Model execution — 2026-07-17T08:54:36+00:00

- Agent role: `deterministic-validation`
- Effective model: `none (deterministic script)`
- Effective reasoning effort: `none`
- Configuration source: `deterministic_script`
- Event: `start`
- Reason code: `synthetic_repository_test_suite`
- Safety and autonomy contracts unchanged: `true`

### Deployment validation — 2026-07-17

- `python3 -m unittest discover -s tests -v`: 30 tests passed in disposable synthetic repositories.
- Both explicit state graphs were exercised edge-by-edge, including idempotent self-transitions and invalid-transition rejection.
- `scripts/conveyor validate-config`: PASS; two independent registrations; Goal Mode verified enabled.
- `scripts/conveyor run --project case-manager --mode milestone --dry-run`: PASS; next action `queue_reconciliation`; no application write.
- `scripts/conveyor run --mode portfolio --dry-run`: PASS; global concurrency `2`; no application write.
- Global skill: PASS using the skill-creator `quick_validate.py` validator with temporary, subsequently removed validator-only dependencies.
- Global agent: PASS; TOML parsed; model `gpt-5.6-terra`, reasoning `medium`, source `agent_file`.
- Application production repositories were not used by automated tests and were not modified by Conveyor setup or dry-run commands.
