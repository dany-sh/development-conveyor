# M1 Test Requirement Traceability

This table maps every required M1 scenario to executable evidence. The M1-specific tests live primarily in `tests/test_transactional_kernel.py`; established compatibility suites remain cited where they are the stronger end-to-end check.

## M1-002 cost-aware policy

`tests/test_cost_policy.py` covers deterministic zero-model routing; feature, override, fallback, and recorded-evidence profile precedence; the seven fixed model/reasoning profiles; parent and child launcher budgets; focused context discovery; planning-document-only launch rejection; complete feature acceptance gates; changed-path verification selection; trusted validation-evidence reuse; and command-output containment. All mutation-capable unit scenarios use temporary directories only.

| # | Required scenario | Executable evidence |
|---:|---|---|
| 1 | Ledger append and canonical serialization | `LedgerTests.test_append_is_canonical_and_monotonic` |
| 2 | Ledger hash-chain validation | `LedgerTests.test_hash_chain_validation` |
| 3 | Ledger truncation detection | `LedgerTests.test_truncation_detection`, `test_complete_tail_record_deletion_detected_by_durable_head`, `test_real_process_death_one_record_ahead_repairs_durable_head` (repairs only the exact durable-head-one-record-behind crash shape) |
| 4 | Ledger reordering detection | `LedgerTests.test_reordering_detection` |
| 5 | Duplicate event detection | `LedgerTests.test_duplicate_event_detection` |
| 6 | Repository identity mismatch | `LedgerTests.test_repository_identity_mismatch` |
| 7 | Project identity mismatch | `LedgerTests.test_project_identity_mismatch` |
| 8 | Transaction start | `ContractTests.test_transaction_start_and_completion` |
| 9 | Transaction completion | `ContractTests.test_transaction_start_and_completion` |
| 10 | Double-terminal rejection | `LedgerTests.test_double_terminal_rejected` |
| 11 | Completed transaction immutability | `LedgerTests.test_terminal_transaction_immutable`, `ContractTests.test_completed_transaction_cannot_reactivate` |
| 12 | Transaction supersession | `ContractTests.test_every_adapter_routes_every_non_success_classification_to_terminal_state` (`TRANSACTION_SUPERSEDED`) |
| 13 | Planning lease lifecycle | `LeaseAndCommandTests.test_each_phase_uses_its_exact_typed_lease`, `MigrationAndSimulatorTests.test_kernel_lease_revalidation_exact_matches_every_bound_field`, `test_migration_process_death_boundaries_recover_idempotently` |
| 14 | Feature lease lifecycle | `LeaseAndCommandTests.test_each_phase_uses_its_exact_typed_lease`, exact-field revalidation test |
| 15 | Integration lease lifecycle | `LeaseAndCommandTests.test_each_phase_uses_its_exact_typed_lease`, exact-field revalidation test |
| 16 | Recovery lease lifecycle | `LeaseAndCommandTests.test_each_phase_uses_its_exact_typed_lease`, `test_actual_recovery_planner_cross_process_boundary_matrix` |
| 17 | Cross-phase lease rejection | `LeaseAndCommandTests.test_cross_phase_lease_rejected` |
| 18 | Starting HEAD mismatch | `MigrationAndSimulatorTests.test_same_branch_head_advance_rejects_finalization_before_kernel_staging`, `test_planning_transaction_finalization.test_10_starting_head_divergence_rejects_recovery` |
| 19 | Starting branch mismatch | `test_feature_branch_recovery.test_02_session_on_milestone_branch_is_branch_invariant_violation` |
| 20 | Dirty worktree at new transaction start | `MigrationAndSimulatorTests.test_kernel_rejects_dirty_new_transaction_start`, `test_reconciliation_contract.test_29_controller_recovery_rejects_dirty_repository` |
| 21 | Exact recovery of a recorded dirty transaction | `MigrationAndSimulatorTests.test_dirty_recovery_requires_exact_recorded_prefix_diff`, `test_cli_resume_routes_clean_and_exact_dirty_interruptions_through_planner` (production CLI clean, dirty, validated, and terminal-failure subcases), `test_dirty_recovery_rejects_drift_before_observation_snapshot`, `test_dirty_recovery_rejects_drift_after_observation_snapshot`, `test_restored_validation_rejects_durable_boundary_drift_without_replacement`, `test_clean_commit_recovery_rejects_before_lease_drift_and_releases_lease`, and the full interruption matrix |
| 22 | Unauthorized path mutation | `test_planning_transaction_finalization.test_03_unauthorized_production_change_rejects_finalization_and_preserves_bytes`, `MigrationAndSimulatorTests.test_hard_linked_mutation_path_is_rejected_before_launch_and_finalization`, `test_post_launch_link_swap_is_rejected_before_external_content_read` |
| 23 | Production mutation during planning | `test_planning_transaction_finalization.test_04_unauthorized_product_test_change_rejects_finalization` |
| 24 | Planning mutation during feature execution | `CycleEngineTests.test_feature_execution_rejects_planning_control_plane_mutation_without_commit`, `test_feature_branch_recovery.test_16_queue_fingerprint_change_is_not_accepted_completion` |
| 25 | Required command failure | `LeaseAndCommandTests.test_required_command_failure_is_authoritative` |
| 26 | Optional diagnostic failure | `LeaseAndCommandTests.test_optional_diagnostic_failure_is_warning` |
| 27 | Unsupported command behavior | `LeaseAndCommandTests.test_unsupported_command_is_not_authoritative` |
| 28 | Structured terminal-envelope validation | `ContractTests.test_terminal_envelope_only_final_assistant_message`, `test_trailing_prose_rejected` |
| 29 | Duplicate terminal markers | `ContractTests.test_duplicate_terminal_markers_rejected` |
| 30 | Prompt-echo spoofing | `ContractTests.test_prompt_echo_spoofing_rejected` |
| 31 | Tool-output spoofing | `ContractTests.test_tool_output_marker_cannot_spoof_terminal_result` |
| 32 | Exit-zero human gate | `MigrationAndSimulatorTests.test_phase_bridge_terminalizes_exit_zero_human_and_nonzero_retryable` |
| 33 | Nonzero exit with valid retryable envelope | `MigrationAndSimulatorTests.test_phase_bridge_terminalizes_exit_zero_human_and_nonzero_retryable` |
| 34 | Projection cache rebuild | `ProjectionTests.test_cache_rebuild_when_stale` |
| 35 | Projection-cache corruption | `ProjectionTests.test_cache_sequence_ahead_rejected`, `test_cache_wrong_fingerprint_rejected`, `test_cache_target_symlink_and_hardlink_are_rejected_without_external_write`, `test_cache_is_confined_beside_ledger_and_rejects_symlinked_parent` |
| 36 | Ledger projection after interruption | `MigrationAndSimulatorTests.test_interruption_rebuilds_exact_projection_and_cache_evidence` asserts exact state, active transaction, ledger sequence/fingerprint, projection fingerprint, accepted/integrated commits, and cache agreement |
| 37 | Queue reconciliation happy path | `MigrationAndSimulatorTests.test_whole_phase_queue_bridge_has_one_lease_and_terminal`, `CycleEngineTests.test_queue_reconciliation_runs_before_feature_selection` |
| 38 | Feature acceptance happy path | `CycleEngineTests.test_complete_one_feature_cycle_integrates_exactly_one_commit` |
| 39 | Integration happy path | `MigrationAndSimulatorTests.test_actual_kernel_integrates_once` |
| 40 | Human-decision resolution | `test_human_decision_resolution.test_14_successful_transition_to_queue_reconciliation`, `MigrationAndSimulatorTests.test_process_death_after_human_terminal_replays_materialization_after_recovery`, `test_terminal_materialization_replays_once_until_acknowledged`, `test_human_terminal_requires_exact_unique_gate_identity` (derived unique ID plus exact transaction, project, repository, path, workflow, and approved continuation binding) |
| 41 | Milestone-gate happy path | `test_synthetic_integration.test_complete_milestone_stops_before_main_merge`, `MigrationAndSimulatorTests.test_milestone_gate_denies_source_and_uses_prelaunch_pinned_commands` |
| 42 | Duplicate-integration prevention | `MigrationAndSimulatorTests.test_actual_kernel_integrates_once`, twenty-cycle test |
| 43 | Stale Feature Factory session after accepted commit | `MigrationAndSimulatorTests.test_real_interview_dry_run_projection`, `test_post_migration_real_fixtures_route_only_fresh_kernel_actions`, `ProjectionTests.test_current_observations_are_reproducible_and_override_only_current_consistency` |
| 44 | Case Manager migration fixture | `MigrationAndSimulatorTests.test_real_case_manager_dry_run_projection`, `test_post_migration_real_fixtures_route_only_fresh_kernel_actions` |
| 45 | Interview Companion migration fixture | `MigrationAndSimulatorTests.test_real_interview_dry_run_projection`, `test_post_migration_real_fixtures_route_only_fresh_kernel_actions` |
| 46 | Migration dry-run writes nothing | Both real migration fixture tests compare HEAD, tree, index, refs, and status before/after |
| 47 | Applied synthetic migration idempotency | `MigrationAndSimulatorTests.test_applied_synthetic_migration_is_idempotent`, `test_migration_filters_future_milestone_and_rejects_active_ambiguity`, `test_migration_process_death_boundaries_recover_idempotently` |
| 48 | Twenty-feature synthetic milestone | `MigrationAndSimulatorTests.test_actual_twenty_cycle_milestone_has_no_duplicate_or_prohibited_operation` |
| 49 | Interruption at each transaction boundary | `MigrationAndSimulatorTests.test_actual_every_workflow_boundary_matrix_is_recovered` (8×15), `test_actual_recovery_planner_cross_process_boundary_matrix` (real process death at all 15 boundaries) |
| 50 | Global consistency checker on valid state | `MigrationAndSimulatorTests.test_consistency_checker_valid_recoverable_corrupt_and_unsafe` |
| 51 | Global consistency checker on recoverable state | Same consistency-checker test, missing projection-cache case |
| 52 | Global consistency checker on corrupt ledger | Same consistency-checker test, truncated-ledger case |
| 53 | Global consistency checker on unsafe Git state | Same consistency-checker test, active-Git-operation case |
| 54 | One-feature mode stop behavior | `test_startup_reconciliation.test_one_feature_mode_stops_with_second_feature_still_ready` |
| 55 | Milestone mode continuation behavior | `test_post_integration_finalization.test_19_milestone_mode_can_later_reconcile_the_queue` |
| 56 | No default-branch merge | Twenty-cycle and full interruption-matrix tests assert the default ref is unchanged |
| 57 | No push, tag, release, deployment, or notarization | `ProhibitedActionTests.test_prohibited_git_and_external_operations_are_rejected` observes and rejects push, tag, deploy, publish, release, and notarize categories at the common subprocess authority; `MigrationAndSimulatorTests.test_production_configured_executor_rejects_prohibited_action_before_launch` proves the production configured-command executor rejects before `subprocess.run`; the twenty-cycle simulator independently audits invoked Git argument arrays |
| 58 | Projection-authoritative planner fields | `ProjectionAuthorityTests.test_valid_projection_overrides_stale_project_and_repository_cycle`, `test_consistency_observes_planner_facing_routing_independently` |
| 59 | Superseded legacy cycle cannot resume | `ProjectionAuthorityTests.test_projection_dispatch_never_resumes_superseded_session` |
| 60 | Typed plan and projection agreement | `ProjectionAuthorityTests.test_execution_plan_disagreement_fails_closed_and_invariant_detects_it`, `test_every_projection_action_has_explicit_session_and_mutation_semantics` |
| 61 | Pre-session active transaction recovery | `ProjectionAuthorityTests.test_active_kernel_transaction_is_recovery_without_legacy_session_resume`, `test_pre_session_ledger_interruptions_are_recovery_without_resume` |
| 62 | Exact planned snapshot held through typed lease | `ProjectionAuthorityTests.test_feature_dispatch_rejects_post_validation_milestone_advance`, `test_integration_dispatch_rejects_post_validation_milestone_advance` |
| 63 | Compatibility cache identity and idempotency | `ProjectionAuthorityTests.test_compatibility_cache_reconciliation_is_atomic_idempotent_and_app_read_only`, `test_compatibility_cache_reconciliation_rejects_foreign_identity` |
| 64 | Controller runtime ignores are narrow and source-safe | `ProjectionAuthorityTests.test_runtime_ignores_are_root_anchored_and_narrow`, `test_migration_and_actual_kernel_execution_keep_controller_source_clean` |
| 65 | Failed integration recovery resolves acceptance from an immutable feature-ref queue snapshot, not the live queue or report | `TwoRefIntegrationRecoveryTests.test_live_ready_queue_integrates_from_two_verified_refs_in_fresh_transaction`, `MilestoneIntegrationResultContractTests.test_terminal_failed_session_selects_fresh_recovery_integration` |
| 66 | Recovered integration independently revalidates accepted feature and milestone refs before ledger mutation | `TwoRefIntegrationRecoveryTests.test_feature_and_milestone_ref_drift_fail_before_new_ledger_events` |
| 67 | Queue-snapshot branch, base, SELF, pending-integration, and acceptance evidence fail closed on mismatch | `TwoRefIntegrationRecoveryTests.test_queue_snapshot_acceptance_mismatch_does_not_recover` |
| 68 | Recovery status, dry-run, and consistency checks append no ledger event and do not mutate the application repository | `TwoRefIntegrationRecoveryTests.test_status_dry_run_and_consistency_append_no_recovery_events` |
| 69 | Accepted feature recovery proves one direct non-merge feature commit atop the verified milestone ref | `TwoRefIntegrationRecoveryTests.test_multi_commit_accepted_feature_ref_does_not_recover`, `test_unrelated_history_accepted_feature_ref_does_not_recover` |
| 70 | Deterministic integration binds the controller ledger namespace separately from the repository adapter namespace | `DeterministicIntegrationHandoffTests.test_distinct_controller_and_adapter_ids_are_serialized_and_lease_bound`, `test_mismatched_accepted_adapter_fails_before_transaction_started`, `test_missing_live_or_accepted_adapter_id_fails_before_transaction`, `test_reserved_identity_change_fails_before_transaction_started`, `test_mismatched_controller_ledger_id_fails_before_application_mutation`, `test_real_id_topology_reaches_transaction_boundary_without_mutation`, `test_legacy_plan_default_requires_identical_controller_and_adapter_ids` |

The common subprocess authority exposes a scoped, privacy-safe attempted-command observer that records executable, operation, prohibited category, authority, and working-directory fingerprint before rejection. The deterministic simulator separately records actual Git argument arrays. Negative assertions are therefore derived from observed authority attempts and subprocess operations, not hard-coded success flags.

Additional adversarial invariants are covered by `MigrationAndSimulatorTests.test_recovered_lease_archive_rejects_symlink_and_hardlink_targets`, `test_orphaned_prestart_lease_blocks_live_owner_and_recovers_dead_owner`, `test_terminal_completion_evidence_cannot_override_canonical_fields`, and `test_bridge_terminal_evidence_spoof_is_terminalized_as_failure`.

The final blocker closure also directly covers exact accepted-commit recovery after integration finalization (`test_integration_after_commit_recovery_preserves_exact_accepted_commit`), canonical ledger/cache routing after real fixture migration (`test_post_migration_real_fixtures_route_only_fresh_kernel_actions`), non-repeating legacy terminal replay (`LedgerTests.test_legacy_terminal_replay_does_not_repeat_mutation`), and projection-aware resume compatibility (`CycleEngineTests.test_complete_one_feature_cycle_integrates_exactly_one_commit`, `SyntheticIntegrationTests.test_resume_after_verified_integration_does_not_repeat_feature`).
