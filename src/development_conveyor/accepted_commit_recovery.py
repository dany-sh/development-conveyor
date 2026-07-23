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
)
from .contracts import MutationPolicy, WorkflowType, fingerprint
from .cycle_cache import write_terminal_cycle_cache
from .errors import RecoveryError
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


class AcceptedCommitRecovery:
    """Recover the exact protected F004 accepted-commit topology."""

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
                f"F004 recovery cannot read recorded blob {relative_path}"
            )
        return hashlib.sha256(result.stdout).hexdigest()

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
        expected_identity = {
            "project": "interview-companion",
            "feature": "F004",
            "candidate": F004_CANDIDATE,
            "base": F004_MILESTONE_BASE,
            "branch": F004_BRANCH,
            "feature_transaction": F004_FEATURE_TRANSACTION,
            "acceptance_transaction": F004_ACCEPTANCE_TRANSACTION,
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
                "accepted-commit recovery is limited to the protected F004 evidence"
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
        writer = inspect_repository_writer_lock(
            self.inspector.writer_lock_path(
                self.configuration["lock_policy"]["writer_lock_relative_path"]
            ),
            self.project.repository,
        )
        queue = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        feature = queue.feature(feature_id)
        candidate_parents = (
            self.inspector.git(
                ["show", "-s", "--format=%P", candidate_commit]
            ).stdout.strip().split()
        )
        metadata_paths = (
            acceptance_metadata_paths(self.project, feature)
            if isinstance(feature, dict)
            else ()
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
        checks = {
            "ledger_integrity": integrity.valid,
            "ledger_tail_is_acceptance_projection": bool(acceptance_events)
            and acceptance_events[-1]["event_type"] == "ProjectionUpdated"
            and acceptance_events[-1]["sequence"] == integrity.sequence,
            "feature_event_topology": self._event_types(
                events, feature_transaction_id
            )
            == [
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
                "CheckpointRecorded",
                "CheckpointRecorded",
                "CheckpointRecorded",
                "TransactionCompleted",
                "LeaseReleased",
                "ProjectionUpdated",
            ],
            "acceptance_event_topology": self._event_types(
                events, acceptance_transaction_id
            )
            == [
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
            ],
            "feature_start": (feature_start or {}).get("payload", {}).get(
                "starting_head"
            )
            == milestone_base
            and (feature_start or {}).get("payload", {}).get("feature_id")
            == feature_id,
            "feature_commit": (feature_commit or {}).get("payload", {}).get(
                "commit"
            )
            == candidate_commit
            and (feature_commit or {}).get("payload", {}).get("parent")
            == milestone_base,
            "feature_terminal": (feature_terminal or {}).get("payload", {}).get(
                "accepted_feature_commit"
            )
            == candidate_commit,
            "acceptance_start": (acceptance_start or {}).get("payload", {}).get(
                "starting_head"
            )
            == candidate_commit,
            "acceptance_no_change": (
                (acceptance_commit or {}).get("payload", {}).get("commit")
                == candidate_commit
                and (acceptance_commit or {}).get("payload", {}).get("no_change")
                is True
            ),
            "acceptance_terminal": (
                (acceptance_terminal or {}).get("payload", {}).get(
                    "accepted_feature_commit"
                )
                == candidate_commit
                and (acceptance_terminal or {}).get("payload", {}).get(
                    "next_state"
                )
                == "integration_ready"
            ),
            "projection_state": projection.get("current_state")
            == "integration_ready",
            "projection_feature": projection.get("current_feature") == feature_id,
            "projection_candidate": projection.get("accepted_feature_commit")
            == candidate_commit,
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
            "checked_out_candidate": self.inspector.current_branch
            == feature_branch
            and self.inspector.head == candidate_commit,
            "repository_clean": self.inspector.is_clean,
            "no_git_operation": not any(
                self.inspector.git_operation_state().values()
            ),
            "writer_lease_absent": not writer.exists,
            "candidate_has_implementation": bool(source_test_paths),
            "candidate_queue_is_unaccepted": isinstance(feature, dict)
            and feature.get("status") == "ready"
            and feature.get("implementation_status") == "Ready"
            and not feature.get("accepted_commit"),
            "no_prior_recovery": not recovery_events,
        }
        if not all(checks.values()):
            failed = ", ".join(
                key for key, passed in checks.items() if not passed
            )
            raise RecoveryError(
                "F004 accepted-commit recovery preflight failed: " + failed
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
            "candidate_changed_paths": list(candidate_paths),
            "source_test_paths": list(source_test_paths),
            "source_test_hashes": source_test_hashes,
            "authorized_metadata_paths": list(metadata_paths),
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
                "F004 compatibility cache is unavailable or invalid"
            ) from exc
        if not isinstance(state, dict):
            raise RecoveryError("F004 compatibility cache must be an object")
        state.update(
            {
                "schema_version": 1,
                "project_id": self.project.project_id,
                "active_milestone": self.project.active_milestone,
                "current_feature": "F004",
                "selected_feature": "F004",
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
        write_terminal_cycle_cache(
            path,
            state,
            ledger=self.ledger,
            projection_engine=self.projection,
            transaction_id=transaction_id,
            expected_feature="F004",
        )

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        unsigned = {
            key: value for key, value in plan.items() if key != "plan_fingerprint"
        }
        if plan.get("plan_fingerprint") != fingerprint(unsigned):
            raise RecoveryError("F004 recovery plan fingerprint is invalid")
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
                current_feature="F004",
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
                    "F004 recovery evidence changed under launch reservation"
                )
            metadata_paths = tuple(plan["authorized_metadata_paths"])
            policy = MutationPolicy(
                metadata_paths,
                commit_subject="F004: Navigation and Workspace Restoration",
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
                feature_id="F004",
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
                feature_id="F004",
                feature_branch=str(plan["feature_branch"]),
                milestone_base=str(plan["milestone_base"]),
                candidate_commit=str(plan["candidate_implementation_commit"]),
                recovery=True,
            )
            if materialized != metadata_paths:
                raise RecoveryError(
                    "F004 recovery metadata authorization changed"
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
                projection=completion["projection"],
                transaction_id=transaction.transaction_id,
                run_id=run_id,
                candidate_commit=str(plan["candidate_implementation_commit"]),
                accepted_commit=accepted,
            )
            immutable = inspect_two_refs(
                repository=self.project.repository,
                controller_project_id=self.project.project_id,
                feature_id="F004",
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
                    "F004 accepted-commit recovery postcondition failed: "
                    + failed
                )
            return {
                "schema_version": 1,
                "outcome": "integration_ready",
                "project_id": self.project.project_id,
                "feature_id": "F004",
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
