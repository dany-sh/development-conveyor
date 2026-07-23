"""Zero-model recovery for a completed feature whose accepted metadata is missing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .accepted_commit import (
    acceptance_metadata_paths,
    materialize_acceptance_metadata,
    render_acceptance_metadata,
)
from .contracts import MutationPolicy, WorkflowType, fingerprint
from .cycle_cache import (
    cycle_cache_semantics_from_projection,
    write_terminal_cycle_cache,
)
from .errors import QueueError, RecoveryError
from .integration_executor import inspect_two_refs
from .kernel import WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, inspect_repository_writer_lock, make_lock_record
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease


F004_FEATURE_TRANSACTION = "6886b13b-b655-40a9-ab9c-7f9115f9ad7f"
F004_ACCEPTANCE_TRANSACTION = "0d53e634-32c0-4c3d-97c0-9702fc513432"
F004_CANDIDATE = "8ae5c94df59119d89d3c2ac6fd7508a47a426ff6"
F004_MILESTONE_BASE = "7a754a4b4741b85ac51b8b516cebd25cae1eec69"
F004_BRANCH = "codex/F004-navigation-and-workspace-restoration"
F009_FEATURE_TRANSACTION = "3efad53f-fae1-459b-be84-b9d2d7a370f7"
F009_ACCEPTANCE_TRANSACTION = "dd695fa7-9713-49da-92ef-f8b40c86ea59"
F009_CANDIDATE = "777ff201d3752e4be034da87e6c046289b3883ac"
F009_MILESTONE_BASE = "639919c4a89537d9df8e3a80404780c32cb284c9"
F009_BRANCH = "codex/F009-transcription-engine-abstraction"
F009_RETAINED_PATHS = (
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/features/F009-transcription-engine-abstraction.md",
)
F009_RETAINED_DIFF_FINGERPRINT = (
    "23ec1510822e027d10c48fd5ea05122ef0f62de2f1cf1e0c8d1ea11c20743882"
)


class AcceptedCommitRecovery:
    """Recover an explicitly protected accepted-commit topology."""

    def __init__(
        self,
        *,
        controller_root: Path,
        configuration: dict[str, Any],
        project: Project,
    ):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration
        self.project = project
        self.inspector = RepositoryInspector(project.repository)
        identity = self.inspector.identity()
        self.state_root = (
            self.controller_root / "state/projects" / project.project_id
        )
        self.ledger = EvidenceLedger(
            self.state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(
            self.ledger, self.state_root / "projection-cache.json"
        )

    @staticmethod
    def _event_types(events: list[dict[str, Any]], transaction_id: str) -> list[str]:
        return [
            event["event_type"]
            for event in events
            if event["transaction_id"] == transaction_id
        ]

    @staticmethod
    def _event(
        events: list[dict[str, Any]], transaction_id: str, event_type: str
    ) -> dict[str, Any] | None:
        matches = [
            event
            for event in events
            if event["transaction_id"] == transaction_id
            and event["event_type"] == event_type
        ]
        return matches[0] if len(matches) == 1 else None

    def _blob_hash(self, commit: str, relative_path: str) -> str:
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative_path}"],
            cwd=self.project.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RecoveryError(
                f"accepted-commit recovery cannot read recorded blob {relative_path}"
            )
        return hashlib.sha256(result.stdout).hexdigest()

    def _protected_scenario(self, feature_id: str) -> dict[str, Any]:
        scenarios = {
            "F004": {
                "project": "interview-companion",
                "feature": "F004",
                "title": "Navigation and Workspace Restoration",
                "milestone": "M0",
                "milestone_branch": "codex/m0-foundation",
                "candidate": F004_CANDIDATE,
                "base": F004_MILESTONE_BASE,
                "branch": F004_BRANCH,
                "feature_transaction": F004_FEATURE_TRANSACTION,
                "acceptance_transaction": F004_ACCEPTANCE_TRANSACTION,
                "acceptance_outcome": "completed",
                "feature_checkpoint_count": 4,
                "projection_state": "integration_ready",
                "projection_commit": F004_CANDIDATE,
                "retained_paths": (),
                "retained_diff_fingerprint": None,
            },
            "F009": {
                "project": "interview-companion",
                "feature": "F009",
                "title": "Transcription Engine Abstraction",
                "milestone": "M0",
                "milestone_branch": "codex/m0-foundation",
                "candidate": F009_CANDIDATE,
                "base": F009_MILESTONE_BASE,
                "branch": F009_BRANCH,
                "feature_transaction": F009_FEATURE_TRANSACTION,
                "acceptance_transaction": F009_ACCEPTANCE_TRANSACTION,
                "acceptance_outcome": "failed_partial",
                "feature_checkpoint_count": 3,
                "projection_state": "validation_failed",
                "projection_commit": None,
                "retained_paths": F009_RETAINED_PATHS,
                "retained_diff_fingerprint": F009_RETAINED_DIFF_FINGERPRINT,
            },
        }
        scenario = scenarios.get(feature_id)
        if scenario is None:
            raise RecoveryError(
                "accepted-commit recovery is limited to explicitly protected evidence"
            )
        return scenario

    def inspect(
        self,
        *,
        feature_id: str,
        candidate_commit: str,
        milestone_base: str,
        feature_branch: str,
        feature_transaction_id: str,
        acceptance_transaction_id: str,
    ) -> dict[str, Any]:
        scenario = self._protected_scenario(feature_id)
        expected_identity = {
            key: scenario[key]
            for key in (
                "project",
                "feature",
                "candidate",
                "base",
                "branch",
                "feature_transaction",
                "acceptance_transaction",
            )
        }
        observed_identity = {
            "project": self.project.project_id,
            "feature": feature_id,
            "candidate": candidate_commit,
            "base": milestone_base,
            "branch": feature_branch,
            "feature_transaction": feature_transaction_id,
            "acceptance_transaction": acceptance_transaction_id,
        }
        if observed_identity != expected_identity:
            raise RecoveryError(
                "accepted-commit recovery identity differs from protected evidence"
            )
        integrity = self.ledger.verify()
        events = self.ledger.read()
        projection = self.projection.current()
        feature_events = [
            event
            for event in events
            if event["transaction_id"] == feature_transaction_id
        ]
        acceptance_events = [
            event
            for event in events
            if event["transaction_id"] == acceptance_transaction_id
        ]
        feature_start = self._event(
            events, feature_transaction_id, "TransactionStarted"
        )
        feature_commit = self._event(
            events, feature_transaction_id, "CommitFinalized"
        )
        feature_terminal = self._event(
            events, feature_transaction_id, "TransactionCompleted"
        )
        acceptance_start = self._event(
            events, acceptance_transaction_id, "TransactionStarted"
        )
        acceptance_commit = self._event(
            events, acceptance_transaction_id, "CommitFinalized"
        )
        acceptance_terminal = self._event(
            events, acceptance_transaction_id, "TransactionCompleted"
        )
        acceptance_blocked = self._event(
            events, acceptance_transaction_id, "TransactionBlocked"
        )
        writer = inspect_repository_writer_lock(
            self.inspector.writer_lock_path(
                self.configuration["lock_policy"]["writer_lock_relative_path"]
            ),
            self.project.repository,
        )
        queue_text = self.inspector.file_at_commit(
            candidate_commit, self.project.queue_location
        )
        try:
            candidate_queue = FeatureQueue(json.loads(queue_text or ""))
        except (json.JSONDecodeError, QueueError) as exc:
            raise RecoveryError("candidate feature queue is unavailable or invalid") from exc
        candidate_feature = candidate_queue.feature(feature_id)
        candidate_parents = (
            self.inspector.git(
                ["show", "-s", "--format=%P", candidate_commit]
            ).stdout.strip().split()
        )
        metadata_paths = (
            acceptance_metadata_paths(self.project, candidate_feature)
            if isinstance(candidate_feature, dict)
            else ()
        )
        try:
            _, expected_metadata, candidate_metadata = render_acceptance_metadata(
                project=self.project,
                feature_id=feature_id,
                feature_branch=feature_branch,
                milestone_base=milestone_base,
                candidate_commit=candidate_commit,
                recovery=True,
            )
        except Exception as exc:
            raise RecoveryError(
                "protected acceptance metadata cannot be rendered"
            ) from exc
        retained_paths = tuple(sorted(scenario["retained_paths"]))
        observed_paths = tuple(self.inspector.tracked_changed_paths())
        retained_prefix_valid = all(
            (self.project.repository / path).read_bytes()
            == (
                expected_metadata[path]
                if path in retained_paths
                else candidate_metadata[path]
            )
            for path in metadata_paths
        )
        candidate_paths = tuple(
            sorted(self.inspector.changed_paths(candidate_commit))
        )
        source_test_paths = tuple(
            path
            for path in candidate_paths
            if path.startswith("Sources/") or path.startswith("Tests/")
        )
        source_test_hashes = {
            path: self._blob_hash(candidate_commit, path)
            for path in source_test_paths
        }
        recovery_events = [
            event
            for event in events
            if event["event_type"] == "RecoveryApplied"
            and event["payload"].get("candidate_implementation_commit")
            == candidate_commit
        ]
        expected_acceptance_topology = (
            [
                "TransactionStarted",
                "LeaseAcquired",
                "SnapshotCaptured",
                "SessionLaunched",
                "SessionResultAccepted",
                "ChangesDetected",
                "ValidationStarted",
                "ValidationPassed",
                "CommitFinalized",
                "CheckpointRecorded",
                "TransactionCompleted",
                "LeaseReleased",
                "ProjectionUpdated",
            ]
            if scenario["acceptance_outcome"] == "completed"
            else [
                "TransactionStarted",
                "LeaseAcquired",
                "SnapshotCaptured",
                "TransactionBlocked",
                "LeaseReleased",
                "ProjectionUpdated",
            ]
        )
        acceptance_completed = (
            (acceptance_commit or {}).get("payload", {}).get("commit")
            == candidate_commit
            and (acceptance_commit or {}).get("payload", {}).get("no_change")
            is True
            and (acceptance_terminal or {}).get("payload", {}).get(
                "accepted_feature_commit"
            )
            == candidate_commit
            and (acceptance_terminal or {}).get("payload", {}).get("next_state")
            == "integration_ready"
        )
        blocked_snapshot = (acceptance_blocked or {}).get("payload", {}).get(
            "terminal_snapshot"
        )
        acceptance_failed_partial = (
            (acceptance_blocked or {}).get("payload", {}).get("classification")
            == "FEATURE_VALIDATION_FAILED"
            and (acceptance_blocked or {}).get("payload", {}).get("reference")
            == "TransactionError"
            and isinstance(blocked_snapshot, dict)
            and blocked_snapshot.get("branch") == feature_branch
            and blocked_snapshot.get("head") == candidate_commit
            and tuple(blocked_snapshot.get("tracked_changed_paths") or ())
            == retained_paths
            and blocked_snapshot.get("tracked_diff_fingerprint")
            == scenario["retained_diff_fingerprint"]
            and not blocked_snapshot.get("untracked_paths")
        )
        checks = {
            "ledger_integrity": integrity.valid,
            "ledger_tail_is_acceptance_projection": bool(acceptance_events)
            and acceptance_events[-1]["event_type"] == "ProjectionUpdated"
            and acceptance_events[-1]["sequence"] == integrity.sequence,
            "feature_event_topology": self._event_types(
                events, feature_transaction_id
            )
            == (
                [
                    "TransactionStarted",
                    "LeaseAcquired",
                    "SnapshotCaptured",
                    "SessionLaunched",
                    "SessionResultAccepted",
                    "ChangesDetected",
                    "ValidationStarted",
                    "ValidationPassed",
                    "CommitFinalized",
                ]
                + ["CheckpointRecorded"] * scenario["feature_checkpoint_count"]
                + [
                    "TransactionCompleted",
                    "LeaseReleased",
                    "ProjectionUpdated",
                ]
            ),
            "feature_workflow_identity": bool(feature_events)
            and all(
                event["workflow_type"] == WorkflowType.FEATURE_EXECUTION.value
                for event in feature_events
            ),
            "acceptance_event_topology": self._event_types(
                events, acceptance_transaction_id
            )
            == expected_acceptance_topology,
            "acceptance_workflow_identity": bool(acceptance_events)
            and all(
                event["workflow_type"] == WorkflowType.FEATURE_ACCEPTANCE.value
                for event in acceptance_events
            ),
            "feature_start": (feature_start or {}).get("payload", {}).get(
                "starting_head"
            )
            == milestone_base
            and (feature_start or {}).get("payload", {}).get("feature_id")
            == feature_id
            and (feature_start or {}).get("payload", {}).get("starting_branch")
            == feature_branch
            and (feature_start or {}).get("payload", {}).get("milestone")
            == scenario["milestone"],
            "feature_commit": (feature_commit or {}).get("payload", {}).get(
                "commit"
            )
            == candidate_commit
            and (feature_commit or {}).get("payload", {}).get("parent")
            == milestone_base,
            "feature_terminal": (feature_terminal or {}).get("payload", {}).get(
                "candidate_implementation_commit",
                (feature_terminal or {}).get("payload", {}).get(
                    "accepted_feature_commit"
                ),
            )
            == candidate_commit
            and (feature_terminal or {}).get("payload", {}).get("classification")
            == "FEATURE_ACCEPTED"
            and (feature_terminal or {}).get("payload", {}).get("next_state")
            == "feature_accepted",
            "acceptance_start": (acceptance_start or {}).get("payload", {}).get(
                "starting_head"
            )
            == candidate_commit
            and (acceptance_start or {}).get("payload", {}).get("feature_id")
            == feature_id
            and (acceptance_start or {}).get("payload", {}).get("starting_branch")
            == feature_branch
            and (acceptance_start or {}).get("payload", {}).get("milestone")
            == scenario["milestone"]
            and (acceptance_start or {}).get("payload", {}).get("run_id")
            == (feature_start or {}).get("payload", {}).get("run_id"),
            "acceptance_outcome": (
                acceptance_completed
                if scenario["acceptance_outcome"] == "completed"
                else acceptance_failed_partial
            ),
            "projection_state": projection.get("current_state")
            == scenario["projection_state"],
            "projection_feature": projection.get("current_feature") == feature_id,
            "projection_candidate": projection.get("accepted_feature_commit")
            == scenario["projection_commit"],
            "projection_inactive": projection.get("active_transaction") is None,
            "candidate_parent": candidate_parents == [milestone_base],
            "feature_ref": self.inspector.rev_parse(
                feature_branch, check=False
            )
            == candidate_commit,
            "milestone_ref": self.inspector.rev_parse(
                str(self.project.milestone_branch), check=False
            )
            == milestone_base,
            "milestone_identity": self.project.active_milestone
            == scenario["milestone"]
            and self.project.milestone_branch == scenario["milestone_branch"],
            "checked_out_candidate": self.inspector.current_branch
            == feature_branch
            and self.inspector.head == candidate_commit,
            "retained_path_set": observed_paths == retained_paths,
            "retained_diff_fingerprint": (
                self.inspector.planning_diff_fingerprint()
                == scenario["retained_diff_fingerprint"]
                if retained_paths
                else self.inspector.is_clean
            ),
            "retained_prefix_valid": retained_prefix_valid,
            "no_git_operation": not any(
                self.inspector.git_operation_state().values()
            ),
            "writer_lease_absent": not writer.exists,
            "candidate_has_implementation": bool(source_test_paths),
            "candidate_queue_is_unaccepted": isinstance(candidate_feature, dict)
            and candidate_feature.get("status") == "ready"
            and candidate_feature.get("implementation_status") == "Ready"
            and not candidate_feature.get("accepted_commit"),
            "no_prior_recovery": not recovery_events,
        }
        if not all(checks.values()):
            failed = ", ".join(
                key for key, passed in checks.items() if not passed
            )
            raise RecoveryError(
                f"{feature_id} accepted-commit recovery preflight failed: " + failed
            )
        plan = {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "feature_id": feature_id,
            "feature_branch": feature_branch,
            "milestone_id": self.project.active_milestone,
            "milestone_branch": self.project.milestone_branch,
            "milestone_base": milestone_base,
            "candidate_implementation_commit": candidate_commit,
            "feature_transaction_id": feature_transaction_id,
            "acceptance_transaction_id": acceptance_transaction_id,
            "ledger_sequence": integrity.sequence,
            "ledger_fingerprint": integrity.fingerprint,
            "preserved_event_fingerprints": [
                event["fingerprint"] for event in events
            ],
            "feature_event_fingerprints": [
                event["fingerprint"] for event in feature_events
            ],
            "acceptance_event_fingerprints": [
                event["fingerprint"] for event in acceptance_events
            ],
            "feature_title": str(
                candidate_feature.get("title") if isinstance(candidate_feature, dict)
                else scenario["title"]
            ),
            "candidate_changed_paths": list(candidate_paths),
            "source_test_paths": list(source_test_paths),
            "source_test_hashes": source_test_hashes,
            "authorized_metadata_paths": list(metadata_paths),
            "retained_metadata_paths": list(retained_paths),
            "retained_diff_fingerprint": scenario["retained_diff_fingerprint"],
            "checks": checks,
            "model_sessions_planned": 0,
            "child_sessions_planned": 0,
            "next_state_on_success": "integration_ready",
            "next_action_on_success": "milestone_integration",
        }
        plan["plan_fingerprint"] = fingerprint(plan)
        return plan

    def _materialize_cycle_cache(
        self,
        *,
        feature_id: str,
        projection: dict[str, Any],
        transaction_id: str,
        run_id: str,
        candidate_commit: str,
        accepted_commit: str,
    ) -> None:
        path = self.inspector.cycle_state_path()
        try:
            state = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else {}
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                f"{feature_id} compatibility cache is unavailable or invalid"
            ) from exc
        if not isinstance(state, dict):
            raise RecoveryError(f"{feature_id} compatibility cache must be an object")
        state.update(
            {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "active_milestone": self.project.active_milestone,
                "current_feature": feature_id,
                "selected_feature": feature_id,
                "current_phase": "integration_ready",
                "candidate_implementation_commit": candidate_commit,
                "accepted_feature_commit": accepted_commit,
                "integration_status": "pending",
                "conveyor_run_id": run_id,
                "session_id": None,
                "feature_session_id": None,
                "next_safe_action": "milestone_integration",
                "last_successful_checkpoint": (
                    "accepted_commit_reconstruction_terminal"
                ),
                "last_verified_git_state": {
                    "branch": self.inspector.current_branch,
                    "head": accepted_commit,
                    "clean": self.inspector.is_clean,
                    "git_operations": self.inspector.git_operation_state(),
                },
                "kernel_transaction_id": transaction_id,
                "kernel_ledger_sequence": projection["ledger_sequence"],
                "kernel_ledger_fingerprint": projection["ledger_fingerprint"],
                "kernel_projection_fingerprint": projection[
                    "projection_fingerprint"
                ],
            }
        )
        state.update(cycle_cache_semantics_from_projection(projection))
        write_terminal_cycle_cache(
            path,
            state,
            ledger=self.ledger,
            projection_engine=self.projection,
            transaction_id=transaction_id,
            expected_feature=feature_id,
        )

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        unsigned = {
            key: value for key, value in plan.items() if key != "plan_fingerprint"
        }
        feature_id = str(plan.get("feature_id") or "")
        if plan.get("plan_fingerprint") != fingerprint(unsigned):
            raise RecoveryError(
                f"{feature_id or 'accepted-commit'} recovery plan fingerprint is invalid"
            )
        run_id = f"accepted-commit-recovery-{uuid.uuid4()}"
        identity = self.inspector.identity()
        reservation = DurableLock(
            self.controller_root
            / self.configuration["lock_policy"][
                "controller_launch_lock_directory"
            ]
            / f"{identity['path_fingerprint']}.json"
        )
        reservation.acquire(
            make_lock_record(
                project_id=self.project.project_id,
                repository_identity=identity["repository_id"],
                run_id=run_id,
                current_feature=feature_id,
                current_phase="accepted_commit_recovery",
            )
        )
        try:
            self.inspector.ensure_runtime_ignored()
            revalidated = self.inspect(
                feature_id=str(plan["feature_id"]),
                candidate_commit=str(plan["candidate_implementation_commit"]),
                milestone_base=str(plan["milestone_base"]),
                feature_branch=str(plan["feature_branch"]),
                feature_transaction_id=str(plan["feature_transaction_id"]),
                acceptance_transaction_id=str(
                    plan["acceptance_transaction_id"]
                ),
            )
            if revalidated["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    f"{feature_id} recovery evidence changed under launch reservation"
                )
            metadata_paths = tuple(plan["authorized_metadata_paths"])
            retained_paths = tuple(plan.get("retained_metadata_paths") or ())
            policy = MutationPolicy(
                metadata_paths,
                commit_subject=f"{feature_id}: {plan['feature_title']}",
                require_clean_start=not retained_paths,
            )
            kernel = WorkflowKernel(
                project=self.project,
                ledger=self.ledger,
                projection=self.projection,
                lease=WorkflowWriterLease(
                    self.inspector.writer_lock_path(
                        self.configuration["lock_policy"][
                            "writer_lock_relative_path"
                        ]
                    )
                ),
            )
            transaction = kernel.begin(
                workflow_type=WorkflowType.RECOVERY,
                milestone=str(plan["milestone_id"]),
                feature_id=feature_id,
                run_id=run_id,
                policy=policy,
                start_evidence={
                    "recovery_mode": "accepted_commit_reconstruction",
                    "candidate_implementation_commit": plan[
                        "candidate_implementation_commit"
                    ],
                    "feature_transaction_id": plan["feature_transaction_id"],
                    "acceptance_transaction_id": plan[
                        "acceptance_transaction_id"
                    ],
                    "preserved_ledger_sequence": plan["ledger_sequence"],
                    "model_session_launched": False,
                    "child_sessions_launched": 0,
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
                expected_starting_branch=str(plan["feature_branch"]),
                expected_starting_head=str(
                    plan["candidate_implementation_commit"]
                ),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            materialized = materialize_acceptance_metadata(
                project=self.project,
                feature_id=feature_id,
                feature_branch=str(plan["feature_branch"]),
                milestone_base=str(plan["milestone_base"]),
                candidate_commit=str(plan["candidate_implementation_commit"]),
                recovery=True,
                retained_paths=retained_paths,
                retained_diff_fingerprint=plan.get("retained_diff_fingerprint"),
            )
            if materialized != metadata_paths:
                raise RecoveryError(
                    f"{feature_id} recovery metadata authorization changed"
                )
            finalization = kernel.finalize_deterministic_accepted_commit(
                candidate_commit=str(plan["candidate_implementation_commit"]),
                milestone_id=str(plan["milestone_id"]),
                milestone_branch=str(plan["milestone_branch"]),
                milestone_base=str(plan["milestone_base"]),
                feature_branch=str(plan["feature_branch"]),
                metadata_paths=metadata_paths,
                validation_evidence={
                    "tests_passed": True,
                    "review_passed": True,
                    "documentation_current": True,
                },
                recovery_evidence={
                    "feature_transaction_id": plan["feature_transaction_id"],
                    "acceptance_transaction_id": plan[
                        "acceptance_transaction_id"
                    ],
                    "preserved_ledger_sequence": plan["ledger_sequence"],
                    "plan_fingerprint": plan["plan_fingerprint"],
                },
            )
            accepted = str(finalization["finalized_accepted_commit"])
            completion = kernel.complete(
                classification="RECOVERY_APPLIED",
                evidence={
                    "accepted_feature_commit": accepted,
                    "candidate_implementation_commit": plan[
                        "candidate_implementation_commit"
                    ],
                    "authorized_metadata_paths": list(metadata_paths),
                    "implementation_tree_equivalent": True,
                    "integration_status": "pending",
                    "model_session_launched": False,
                    "child_sessions_launched": 0,
                    "next_action": "milestone_integration",
                },
            )
            self._materialize_cycle_cache(
                feature_id=feature_id,
                projection=completion["projection"],
                transaction_id=transaction.transaction_id,
                run_id=run_id,
                candidate_commit=str(plan["candidate_implementation_commit"]),
                accepted_commit=accepted,
            )
            immutable = inspect_two_refs(
                repository=self.project.repository,
                controller_project_id=self.project.project_id,
                feature_id=feature_id,
                feature_branch=str(plan["feature_branch"]),
                accepted_commit=accepted,
                milestone_id=str(plan["milestone_id"]),
                milestone_branch=str(plan["milestone_branch"]),
                pre_integration_head=str(plan["milestone_base"]),
                queue_path=self.project.queue_location,
            )
            old_events = self.ledger.read()[: int(plan["ledger_sequence"])]
            source_test_hashes = {
                path: self._blob_hash(accepted, path)
                for path in plan["source_test_paths"]
            }
            checks = {
                "old_ledger_events_preserved": [
                    event["fingerprint"] for event in old_events
                ]
                == plan["preserved_event_fingerprints"],
                "direct_parent": self.inspector.rev_parse(
                    f"{accepted}^", check=False
                )
                == plan["milestone_base"],
                "feature_branch_head": self.inspector.rev_parse(
                    str(plan["feature_branch"]), check=False
                )
                == accepted,
                "source_test_bytes_preserved": source_test_hashes
                == plan["source_test_hashes"],
                "only_authorized_metadata_differs": tuple(
                    sorted(
                        line
                        for line in self.inspector.git(
                            [
                                "diff",
                                "--name-only",
                                str(plan["candidate_implementation_commit"]),
                                accepted,
                                "--",
                            ]
                        ).stdout.splitlines()
                        if line
                    )
                )
                == metadata_paths,
                "immutable_metadata_valid": bool(immutable),
                "candidate_recorded": any(
                    event["payload"].get("candidate_implementation_commit")
                    == plan["candidate_implementation_commit"]
                    for event in self.ledger.read()
                ),
                "repository_clean": self.inspector.is_clean,
                "no_git_operation": not any(
                    self.inspector.git_operation_state().values()
                ),
                "writer_lease_released": not self.inspector.writer_lock_path(
                    self.configuration["lock_policy"][
                        "writer_lock_relative_path"
                    ]
                ).exists(),
                "projection_integration_ready": completion["projection"].get(
                    "current_state"
                )
                == "integration_ready",
                "projection_accepted_commit": completion["projection"].get(
                    "accepted_feature_commit"
                )
                == accepted,
                "next_action_milestone_integration": completion[
                    "projection"
                ].get("allowed_next_action")
                == "milestone_integration",
                "no_milestone_integration": not any(
                    event["transaction_id"] == transaction.transaction_id
                    and event["workflow_type"]
                    == WorkflowType.MILESTONE_INTEGRATION.value
                    for event in self.ledger.read()
                ),
                "zero_model_sessions": not any(
                    event["transaction_id"] == transaction.transaction_id
                    and event["event_type"] == "SessionLaunched"
                    for event in self.ledger.read()
                ),
            }
            if not all(checks.values()):
                failed = ", ".join(
                    key for key, passed in checks.items() if not passed
                )
                raise RecoveryError(
                    f"{feature_id} accepted-commit recovery postcondition failed: "
                    + failed
                )
            return {
                "schema_version": 1,
                "outcome": "integration_ready",
                "project_id": self.project.project_id,
                "feature_id": feature_id,
                "run_id": run_id,
                "recovery_transaction_id": transaction.transaction_id,
                "candidate_implementation_commit": plan[
                    "candidate_implementation_commit"
                ],
                "finalized_accepted_commit": accepted,
                "authorized_metadata_paths": list(metadata_paths),
                "checks": checks,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "milestone_integration_performed": False,
                "next_action": "milestone_integration",
                "projection": completion["projection"],
            }
        finally:
            reservation.release(run_id)
