# M1-017 — Retained Feature Terminal-State Transition

## Status

Review on `codex/m1-17-retained-feature-terminal-transition` from exact parent
`2ab5d29fb1439a663fad9668b02301b924cf39a2`.

## Failure trace

`StateMachine.transition` raised `InvalidTransition: unknown current state:
feature_preparing` through `CycleEngine._transition_project`. The
`project_state` compatibility document had been populated before feature
dispatch and still contained `feature_preparing`. The feature kernel had
already appended authenticated `SessionLaunched` evidence and could derive
`feature_running`, but the callback did not durably advance the cycle or
compatibility state before terminal handling.

The correct transition source is the rebuilt kernel projection for the active
feature-execution transaction. Compatibility state and the pre-dispatch cycle
phase are caches and cannot override it.

## Execution-state authority

After an authenticated launcher session ID is recorded, the kernel projection
must prove:

- current state is `feature_running`;
- current feature matches the selected feature;
- the active transaction is the invoked feature transaction;
- its state is `result_pending`; and
- its latest session is the authenticated session.

Only then may the cycle advance to `feature_in_progress` and compatibility
state advance to `feature_running`. A terminal result is consumed from that
same transaction.

Invalid structured output blocks the kernel transaction once, projects its
exact human gate, materializes the terminal cycle cache, transitions from
authenticated running state to `human_decision_required`, and returns a
structured retained-result outcome. It does not throw a second generic session
error or infer feature acceptance. Autopilot records the result once, emits
`FEATURE_RESULT_RETAINED`, and stops cleanly with application work preserved.

## Exact F070 recovery topology

The general retained-result route accepts the recorded F070 `in_progress`
topology only when it authenticates:

- completed preparation transaction
  `0637f3ba-d7a6-4dfb-afee-d68f3d637249`;
- zero-session prelaunch recovery
  `92ac7b0f-964f-47a6-83be-85b7f9c0f5fe` and its referenced failed
  predecessor;
- execution transaction `80fa16eb-ffd7-4acb-9433-77b4b201fb5c`, run
  `f67eab9e-9766-4c8d-ae95-53332aa3f7a4`, and session
  `019f9861-4ce9-79b3-9b5a-96e84d30a50f`;
- exact `SessionLaunched`, invalid structured terminal marker, and bound gate
  `gate-9669520e53b48b832572caa5e2526e7e`;
- execution-owned queue transition to `in_progress`;
- branch `codex/F070-applications-table` and unchanged HEAD
  `b22f89af7a03af1b26b768b640b680daa900dba9`;
- the exact 20 tracked paths and retained diff fingerprint
  `37308e99f8977aedb88b94502009372198564f72508ea4674b47a13a77b192e1`;
- no untracked path, accepted commit, live session, active transaction, writer
  lease, reservation, Autopilot ownership, or Git operation.

Preparation remains authoritative even though later recovery and execution
transactions exist and use a later run. This exception is limited to the exact
F070 lineage. Other `in_progress` queue entries or dirty worktrees fail closed.

## Recovery boundary

Dry-run discovers feature, transaction, and branch from the immutable report
and ledger, while explicit run, session, starting HEAD, fingerprint, and path
arguments remain independent expectations. It writes nothing, starts no
transaction or lease, launches zero models and children, and runs no
application command.

Apply remains unexecuted. It must revalidate every identity and fingerprint
under the feature-result recovery lease, run `DomainModelTests`,
`PersistentDomainStoreTests`, `WorkspaceRoutingTests`, `swift build`, and
`git diff --check`, then create at most one direct-child F070 commit. Only
successful validation may append recovery evidence, supersede the exact gate,
refresh projection and compatibility caches, and stop at
`integration_pending`. It cannot run queue reconciliation or integration.

## Validation boundary

Focused transition, session-result, retained-result, Autopilot, projection,
cache, status, consistency, binary-context, prepared-feature, F068, and F097
regressions; Python compilation; configuration and queue validation; CLI help
and required-mode rejection; the exact read-only live inspections and F070
dry-run; and `git diff --check`.

The complete controller suite, application tests/builds, recovery apply,
ordinary resume, Autopilot restart, generic gate resolution, application
mutation, reconciliation, integration, push, merge, tag, publication,
deployment, and release remain excluded.
