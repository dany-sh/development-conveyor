"""Evidence-driven recovery planning for interrupted kernel transactions."""

from __future__ import annotations

import json
import os
import stat
import uuid
import fcntl
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .contracts import (
    MutationPolicy, SAFE_IDENTIFIER, SessionResultEnvelope, WorkflowType,
    WORKFLOW_LEASE, fingerprint,
)
from .command_authority import CommandAuthority
from .errors import CorruptEvidenceError, RecoveryError, StaleProjectionCache
from .ledger import EvidenceLedger, TERMINAL_EVENT_TYPES
from .projection import ProjectionEngine
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease
from .kernel import RecoveryAdapter, WorkflowKernel


class RecoveryPlanner:
    def __init__(
        self,
        *,
        project: Project,
        ledger: EvidenceLedger,
        projection: ProjectionEngine,
        lease: WorkflowWriterLease,
        interruption_hook: Any | None = None,
    ):
        self.project = project
        self.ledger = ledger
        self.projection = projection
        self.lease = lease
        self.inspector = RepositoryInspector(project.repository)
        self.interrupt = interruption_hook or (lambda boundary, transaction: None)

    def _archive_stale_lease(self, record: Any) -> Path:
        """Move one proven-stale lease into controller-confined immutable evidence."""

        if not SAFE_IDENTIFIER.fullmatch(record.transaction_id):
            raise RecoveryError("stale lease has an unsafe transaction identity")
        controller_state = self.ledger.path.parent
        archive_root = Path(os.path.abspath(str(controller_state / "recovered-leases")))
        if archive_root.parent != controller_state:
            raise RecoveryError("recovered-lease archive escapes controller state")
        try:
            root_metadata = os.lstat(archive_root)
        except FileNotFoundError:
            try:
                archive_root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            root_metadata = os.lstat(archive_root)
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise RecoveryError("recovered-lease archive root is unsafe")

        archive_name = (
            "writer.recovered-"
            + hashlib.sha256(record.transaction_id.encode("utf-8")).hexdigest()
            + ".json"
        )
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(archive_root, directory_flags)
        try:
            try:
                descriptor = os.open(
                    archive_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
            except FileNotFoundError:
                descriptor = -1
            if descriptor >= 0:
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise RecoveryError("recovered-lease archive target is unsafe")
                    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                        descriptor = -1
                        prior = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    raise RecoveryError("recovered-lease archive target is unreadable") from exc
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                if prior != record.to_dict():
                    raise RecoveryError("recovered lease evidence already exists with different content")
                if self.lease.path.exists():
                    self.lease.read()
                    os.unlink(self.lease.path)
            else:
                self.lease.read()
                os.rename(self.lease.path, archive_name, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)
        return archive_root / archive_name

    def inspect(self) -> dict[str, Any]:
        try:
            events = self.ledger.read()
            integrity = self.ledger.verify()
        except CorruptEvidenceError as exc:
            return {
                "classification": "CORRUPT_EVIDENCE",
                "recoverable": False,
                "diagnostic": str(exc),
                "mutation_performed": False,
            }
        if not events:
            orphan = self.lease.read()
            if orphan is not None:
                stale = self.lease.stale_evidence()
                return {
                    "classification": "orphaned_prestart_lease",
                    "recoverable": stale.get("recoverable") is True,
                    "transaction_id": orphan.transaction_id,
                    "workflow_type": orphan.workflow_type.value,
                    "lease_evidence": stale,
                    "mutation_performed": False,
                }
            return {
                "classification": "no_transaction",
                "recoverable": False,
                "mutation_performed": False,
                "ledger_integrity": integrity.to_dict(),
            }
        transactions: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            transactions.setdefault(event["transaction_id"], []).append(event)
        incomplete = [
            (transaction_id, items)
            for transaction_id, items in transactions.items()
            if not any(item["event_type"] in TERMINAL_EVENT_TYPES for item in items)
        ]
        if len(incomplete) > 1:
            recovery_candidates = [
                item for item in incomplete
                if item[1][0]["workflow_type"] == WorkflowType.RECOVERY.value
                and item[1][0]["payload"].get("recovered_transaction_id")
                in {candidate[0] for candidate in incomplete if candidate[0] != item[0]}
            ]
            if len(incomplete) == 2 and len(recovery_candidates) == 1:
                incomplete = recovery_candidates
            else:
                return {
                    "classification": "HUMAN_DECISION_REQUIRED",
                    "recoverable": False,
                    "diagnostic": "multiple incomplete transactions violate single-writer admission",
                    "incomplete_transaction_ids": sorted(item[0] for item in incomplete),
                    "mutation_performed": False,
                }
        active_lease = self.lease.read()
        if active_lease is not None and active_lease.transaction_id not in {
            item[0] for item in incomplete
        }:
            lease_events = transactions.get(active_lease.transaction_id, [])
            if any(item["event_type"] in TERMINAL_EVENT_TYPES for item in lease_events):
                stale = self.lease.stale_evidence()
                return {
                    "classification": "terminal_before_lease_release",
                    "recoverable": stale.get("recoverable") is True,
                    "transaction_id": active_lease.transaction_id,
                    "workflow_type": active_lease.workflow_type.value,
                    "lease_evidence": stale,
                    "mutation_performed": False,
                }
        if not incomplete:
            lease = self.lease.read()
            if lease is not None:
                terminal = transactions.get(lease.transaction_id, [])
                if any(item["event_type"] in TERMINAL_EVENT_TYPES for item in terminal):
                    stale = self.lease.stale_evidence()
                    return {
                        "classification": "terminal_before_lease_release",
                        "recoverable": stale.get("recoverable") is True,
                        "transaction_id": lease.transaction_id,
                        "workflow_type": lease.workflow_type.value,
                        "lease_evidence": stale,
                        "mutation_performed": False,
                    }
                return {
                    "classification": "orphaned_prestart_lease",
                    "recoverable": self.lease.stale_evidence().get("recoverable") is True,
                    "transaction_id": lease.transaction_id,
                    "workflow_type": lease.workflow_type.value,
                    "lease_evidence": self.lease.stale_evidence(),
                    "mutation_performed": False,
                }
            try:
                cache = self.projection.load_cache()
            except StaleProjectionCache:
                return {
                    "classification": "projection_update_incomplete",
                    "recoverable": True, "mutation_performed": False,
                }
            except Exception as exc:
                return {
                    "classification": "projection_cache_requires_decision",
                    "recoverable": False,
                    "diagnostic": str(exc),
                    "mutation_performed": False,
                }
            current = self.projection.rebuild(persist_cache=False)
            if cache is None or cache.get("ledger_sequence") != current.get("ledger_sequence"):
                return {
                    "classification": "projection_update_incomplete",
                    "recoverable": True,
                    "mutation_performed": False,
                }
            return {
                "classification": "nothing_to_recover",
                "recoverable": False,
                "mutation_performed": False,
            }
        transaction_id, items = incomplete[-1]
        physical_lease = self.lease.read()
        if physical_lease is not None and physical_lease.transaction_id != transaction_id:
            return {
                "classification": "HUMAN_DECISION_REQUIRED",
                "recoverable": False,
                "diagnostic": "physical writer lease does not match the only incomplete transaction",
                "transaction_id": transaction_id,
                "lease_transaction_id": physical_lease.transaction_id,
                "mutation_performed": False,
            }
        start = next((item for item in items if item["event_type"] == "TransactionStarted"), None)
        if start is None:
            return {
                "classification": "HUMAN_DECISION_REQUIRED",
                "recoverable": False,
                "diagnostic": "incomplete transaction has no start event",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        payload = start["payload"]
        policy_value = payload.get("allowed_mutation_policy") or {}
        policy = MutationPolicy(
            tuple(policy_value.get("allowed_paths") or ()),
            allowed_prefixes=tuple(policy_value.get("allowed_prefixes") or ()),
            denied_paths=tuple(policy_value.get("denied_paths") or ()),
            denied_prefixes=tuple(policy_value.get("denied_prefixes") or ()),
            allow_untracked=bool(policy_value.get("allow_untracked", False)),
            require_clean_start=bool(policy_value.get("require_clean_start", True)),
            commit_subject=policy_value.get("commit_subject"),
        )
        def policy_authorizes(paths: set[str] | tuple[str, ...]) -> bool:
            try:
                return policy.validate(paths) == tuple(sorted(paths))
            except Exception:
                return False
        starting_head = payload.get("starting_head")
        starting_branch = payload.get("starting_branch")
        accepted_result = next(
            (item for item in items if item["event_type"] == "SessionResultAccepted"), None
        )
        result_envelope = (accepted_result or {}).get("payload", {}).get("envelope") or {}
        integration_evidence = result_envelope.get("evidence") or {}
        integration_target_event = next((
            item for item in items
            if item["event_type"] == "CheckpointRecorded"
            and item["payload"].get("checkpoint") == "integration_target"
        ), None)
        integration_target = (integration_target_event or {}).get("payload", {})
        integration_branch = integration_evidence.get("milestone_branch") or starting_branch
        branch_matches = self.inspector.current_branch == starting_branch
        integration_branch_matches = bool(
            start["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value
            and self.inspector.current_branch == integration_branch
        )
        operations = self.inspector.git_operation_state()
        if any(operations.values()):
            return {
                "classification": "HUMAN_DECISION_REQUIRED",
                "recoverable": False,
                "diagnostic": "active Git operation prevents deterministic recovery",
                "transaction_id": transaction_id,
                "mutation_performed": False,
            }
        commit_event = next((item for item in items if item["event_type"] == "CommitFinalized"), None)
        validation_passed = any(item["event_type"] == "ValidationPassed" for item in items)
        result_accepted = any(item["event_type"] == "SessionResultAccepted" for item in items)
        if commit_event:
            commit_payload = commit_event["payload"]
            commit = commit_payload.get("commit")
            finalized_branch = (
                commit_payload.get("branch_created")
                or commit_payload.get("milestone_branch")
            )
            accepted_commit = (
                commit_payload.get("accepted_commit")
                or integration_target.get("accepted_commit")
                or integration_evidence.get("accepted_commit")
            ) if start["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value else None
            exact = commit == self.inspector.head and (
                branch_matches or self.inspector.current_branch == finalized_branch
            )
            return {
                "classification": "finalization_incomplete" if exact else "HUMAN_DECISION_REQUIRED",
                "recoverable": exact,
                "transaction_id": transaction_id,
                "workflow_type": start["workflow_type"],
                "commit": commit,
                "accepted_commit": accepted_commit,
                "mutation_performed": False,
            }
        if validation_passed and (branch_matches or integration_branch_matches) and isinstance(starting_head, str):
            if self.inspector.head != starting_head:
                changed = set(
                    self.inspector.git([
                        "diff-tree", "--no-commit-id", "--name-only", "-r", self.inspector.head
                    ]).stdout.splitlines()
                )
                diff = subprocess.run(
                    ["git", "diff", "--binary", "--no-ext-diff", starting_head, self.inspector.head, "--"],
                    cwd=self.project.repository,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                validation_event = next(
                    (item for item in items if item["event_type"] == "ValidationPassed"), {}
                )
                expected_diff = validation_event.get("payload", {}).get("diff_fingerprint")
                actual_diff = (
                    self.inspector.content_diff_fingerprint(tuple(sorted(changed)), commit=self.inspector.head)
                    if diff.returncode == 0 else None
                )
                if integration_branch_matches:
                    accepted_commit = (
                        integration_target.get("accepted_commit")
                        or integration_evidence.get("accepted_commit")
                    )
                    milestone_head = starting_head
                    exact_child = bool(
                        isinstance(accepted_commit, str)
                        and isinstance(milestone_head, str)
                        and self.inspector.rev_parse(f"{self.inspector.head}^") == milestone_head
                        and self.inspector.patch_fingerprint(self.inspector.head)
                        == self.inspector.patch_fingerprint(accepted_commit)
                        and self.inspector.is_clean
                        and policy_authorizes(changed)
                    )
                    actual_diff = self.inspector.patch_fingerprint(self.inspector.head)
                else:
                    exact_child = (
                        self.inspector.is_ancestor(starting_head, self.inspector.head)
                        and self.inspector.commit_count(starting_head, self.inspector.head) == 1
                        and self.inspector.is_clean
                        and policy_authorizes(changed)
                        and expected_diff == actual_diff
                    )
                return {
                    "classification": "commit_succeeded_before_evidence" if exact_child else "HUMAN_DECISION_REQUIRED",
                    "recoverable": exact_child,
                    "transaction_id": transaction_id,
                    "workflow_type": start["workflow_type"],
                    "commit": self.inspector.head if exact_child else None,
                    "observed_branch": self.inspector.current_branch,
                    "observed_head": self.inspector.head,
                    "observed_clean": self.inspector.is_clean,
                    "changed_paths": sorted(changed),
                    "diff_fingerprint": actual_diff,
                    "accepted_commit": (
                        integration_target.get("accepted_commit")
                        if start["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value
                        else None
                    ),
                    "mutation_performed": False,
                }
        feature_branch_target = next((
            item["payload"].get("branch") for item in items
            if item["event_type"] == "CheckpointRecorded"
            and item["payload"].get("checkpoint") == "feature_branch_target"
        ), None)
        if (
            start["workflow_type"] == WorkflowType.FEATURE_PREPARATION.value
            and validation_passed and isinstance(feature_branch_target, str)
            and self.inspector.current_branch == feature_branch_target
            and self.inspector.head == starting_head and self.inspector.is_clean
        ):
            return {
                "classification": "commit_succeeded_before_evidence", "recoverable": True,
                "transaction_id": transaction_id, "workflow_type": start["workflow_type"],
                "commit": starting_head, "changed_paths": [],
                "feature_branch_created": feature_branch_target,
                "observed_branch": self.inspector.current_branch,
                "observed_head": self.inspector.head,
                "observed_clean": self.inspector.is_clean,
                "diff_fingerprint": self.inspector.content_diff_fingerprint(
                    (), commit=starting_head
                ),
                "mutation_performed": False,
            }
        if branch_matches and self.inspector.head == starting_head:
            changed = tuple(sorted(set(self.inspector.tracked_changed_paths()) | set(self.inspector.untracked_file_hashes())))
            untracked = set(self.inspector.untracked_file_hashes())
            changes_event = next(
                (item for item in items if item["event_type"] == "ChangesDetected"), None
            )
            exact_dirty = False
            immutable_change_evidence = changes_event or accepted_result
            if changed and immutable_change_evidence is not None:
                evidence_payload = immutable_change_evidence["payload"]
                recorded_paths = tuple(evidence_payload.get("changed_paths") or ())
                recorded_fingerprint = (
                    evidence_payload.get("diff_fingerprint")
                    or evidence_payload.get("observed_diff_fingerprint")
                )
                authorized = policy_authorizes(changed)
                exact_dirty = bool(
                    authorized
                    and (policy.allow_untracked or not untracked)
                    and changed == recorded_paths
                    and self.inspector.content_diff_fingerprint(changed) == recorded_fingerprint
                )
            return {
                "classification": "resume" if (not changed or exact_dirty) else "HUMAN_DECISION_REQUIRED",
                "recoverable": not changed or exact_dirty,
                "transaction_id": transaction_id,
                "workflow_type": start["workflow_type"],
                "changed_paths": list(changed),
                "diff_fingerprint": recorded_fingerprint if exact_dirty else None,
                "starting_branch": starting_branch,
                "starting_head": starting_head,
                "session_result_recorded": result_accepted,
                "mutation_performed": False,
            }
        snapshot_recorded = any(item["event_type"] == "SnapshotCaptured" for item in items)
        provisional_branch = payload.get("provisional_observed_branch")
        provisional_head = payload.get("provisional_observed_head")
        if (
            not snapshot_recorded
            and start["workflow_type"] == WorkflowType.MILESTONE_INTEGRATION.value
            and self.inspector.current_branch == provisional_branch
            and self.inspector.head == provisional_head
            and self.inspector.is_clean
        ):
            return {
                "classification": "resume", "recoverable": True,
                "transaction_id": transaction_id, "workflow_type": start["workflow_type"],
                "changed_paths": [], "session_result_recorded": result_accepted,
                "provisional_target_branch_pending": True, "mutation_performed": False,
            }
        return {
            "classification": "HUMAN_DECISION_REQUIRED",
            "recoverable": False,
            "diagnostic": "repository identity, branch, or HEAD contradicts incomplete transaction",
            "transaction_id": transaction_id,
            "mutation_performed": False,
        }

    def apply(self, *, allow_current_owner_for_simulation: bool = False) -> dict[str, Any]:
        plan = self.inspect()
        current_owner_recovery = bool(
            allow_current_owner_for_simulation
            and plan.get("classification") in {"terminal_before_lease_release", "commit_succeeded_before_evidence"}
        )
        if plan.get("recoverable") is not True and not current_owner_recovery:
            raise RecoveryError(f"recovery is not deterministically authorized: {plan.get('classification')}")
        classification = plan["classification"]
        observation_only_dirty = bool(
            classification == "resume"
            and plan.get("changed_paths")
            and not self.inspector.is_clean
        )
        recovery_policy = MutationPolicy(
            tuple(),
            require_clean_start=not observation_only_dirty,
            commit_subject="factory: apply recovery",
        )
        if classification == "projection_update_incomplete":
            return self._repair_projection(plan)
        old = self.lease.read()
        self.interrupt("before_lease", None)
        archived_lease = None
        recovery_transaction_id = str(uuid.uuid4())
        recovery_record = None
        if old is not None:
            stale = self.lease.stale_evidence()
            owned_simulation = old.owner_pid == os.getpid() and allow_current_owner_for_simulation
            if stale.get("recoverable") is not True and not owned_simulation:
                raise RecoveryError("old writer lease cannot be displaced without exact stale-process proof")
            takeover = self.ledger.path.parent / ".recovery-takeover.lock"
            takeover.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(takeover, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                archived_lease = self._archive_stale_lease(old)
                recovery_record = self.lease.acquire(
                    lease_type=self._recovery_lease_type(),
                    repository_identity=self.ledger.repository_identity,
                    repository_path_fingerprint=self.ledger.repository_path_fingerprint,
                    project_id=self.project.project_id,
                    transaction_id=recovery_transaction_id,
                    workflow_type=WorkflowType.RECOVERY,
                    milestone=self.project.active_milestone, feature_id=None,
                    starting_branch=self.inspector.current_branch or "DETACHED",
                    starting_head=self.inspector.head,
                    run_id=f"recovery-{recovery_transaction_id}", session_id=None,
                    policy=recovery_policy,
                )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        transaction_id = recovery_transaction_id
        recovered = plan.get("transaction_id")
        recovered_events = [
            event for event in self.ledger.read() if event["transaction_id"] == str(recovered)
        ]
        prior_terminal = next(
            (event for event in recovered_events if event["event_type"] in TERMINAL_EVENT_TYPES), None
        )
        prior_result = next(
            (event for event in recovered_events if event["event_type"] == "SessionResultAccepted"), None
        )
        recovered_envelope = (prior_result or {}).get("payload", {}).get("envelope") or {}
        resolved_next_state = (
            (prior_terminal or {}).get("payload", {}).get("next_state")
            or recovered_envelope.get("next_state")
            or {
                WorkflowType.QUEUE_RECONCILIATION.value: "queue_reconciliation",
                WorkflowType.FEATURE_PREPARATION.value: "feature_preparing",
                WorkflowType.FEATURE_EXECUTION.value: "feature_running",
                WorkflowType.FEATURE_ACCEPTANCE.value: "feature_review",
                WorkflowType.MILESTONE_INTEGRATION.value: "integrating",
                WorkflowType.MILESTONE_GATE.value: "milestone_gate",
                WorkflowType.HUMAN_DECISION_RESOLUTION.value: "human_decision_required",
                WorkflowType.RECOVERY.value: "queue_reconciliation",
            }.get(str(plan.get("workflow_type")), "human_decision_required")
        )
        recovery_adapter = RecoveryAdapter(
            allowed_paths=(), commit_subject="factory: apply recovery",
            next_state=resolved_next_state,
            require_clean_start=recovery_policy.require_clean_start,
        )
        recovery_kernel = WorkflowKernel(
            project=self.project, ledger=self.ledger, projection=self.projection,
            lease=self.lease,
            interruption_hook=lambda boundary, transaction: (
                None if boundary == "before_lease" else self.interrupt(boundary, transaction)
            ),
        )
        recovery_kernel._preacquired_lease_record = recovery_record
        try:
            self._revalidate_planned_recovery_baseline(plan)
            recovery_transaction = recovery_kernel.begin(
                workflow_type=WorkflowType.RECOVERY,
                milestone=self.project.active_milestone, feature_id=None,
                run_id=f"recovery-{transaction_id}", policy=recovery_adapter.policy,
                transaction_id=transaction_id,
                start_evidence={
                    "recovered_transaction_id": recovered,
                    "recovery_classification": classification,
                },
            )
            record = recovery_kernel.acquire_lease()
            recovery_kernel.capture_snapshot()
        except Exception:
            active_recovery_lease = self.lease.read()
            if (
                active_recovery_lease is not None
                and active_recovery_lease.transaction_id == recovery_transaction_id
            ):
                self.lease.release(
                    transaction_id=recovery_transaction_id,
                    workflow_type=WorkflowType.RECOVERY,
                    repository_identity=self.ledger.repository_identity,
                    project_id=self.project.project_id,
                )
            raise
        recovery_session = f"deterministic-recovery:{transaction_id}"
        recovery_kernel.session_launched(recovery_session)
        recovery_kernel.accept_result(SessionResultEnvelope.from_dict({
            "schema_version": 1, "workflow_type": "recovery",
            "classification": "RECOVERY_APPLIED", "project_id": self.project.project_id,
            "repository_identity": self.ledger.repository_identity,
            "transaction_id": recovery_transaction.transaction_id,
            "run_id": recovery_transaction.run_id, "session_id": recovery_session,
            "starting_branch": recovery_transaction.starting_branch,
            "starting_commit": recovery_transaction.starting_head,
            "current_commit": recovery_transaction.starting_head,
            "feature_id": None, "changed_paths": [],
            "evidence": {"recovered_transaction_id": recovered, "classification": classification},
            "next_state": resolved_next_state,
        }))
        recovery_kernel.record_file_mutation_boundary()
        recovery_kernel.validate(
            authority=CommandAuthority(), command_results=(),
            semantic_validator=recovery_adapter.semantic_validate,
        )
        recovery_kernel.finalize()
        if classification == "commit_succeeded_before_evidence":
            self.ledger.append(
                event_type="CommitFinalized",
                transaction_id=str(recovered),
                workflow_type=plan["workflow_type"],
                payload={
                    "commit": plan["commit"],
                    "recovered": True,
                    "changed_paths": plan.get("changed_paths", []),
                    "diff_fingerprint": plan.get("diff_fingerprint"),
                    "branch_created": plan.get("feature_branch_created"),
                    "accepted_commit": plan.get("accepted_commit"),
                    "integration_once": (
                        plan.get("workflow_type") == WorkflowType.MILESTONE_INTEGRATION.value
                    ),
                },
            )
        if classification in {"terminal_before_lease_release", "finalization_incomplete", "commit_succeeded_before_evidence"}:
            terminal = self.ledger.terminal_event(str(recovered))
            if terminal is None:
                envelope = recovered_envelope
                self.ledger.append(
                    event_type="TransactionCompleted",
                    transaction_id=str(recovered),
                    workflow_type=plan["workflow_type"],
                    payload={
                        "classification": envelope.get("classification", "RECOVERED_COMPLETION"),
                        "reference": plan.get("commit"),
                        "feature_id": envelope.get("feature_id"),
                        "accepted_feature_commit": (
                            plan.get("commit") if envelope.get("classification") == "FEATURE_ACCEPTED"
                            else plan.get("accepted_commit") if envelope.get("classification") == "INTEGRATED"
                            else None
                        ),
                        "integration_status": (
                            "passed" if envelope.get("classification") == "INTEGRATED" else None
                        ),
                        "integrated_commit": (
                            plan.get("commit") if envelope.get("classification") == "INTEGRATED" else None
                        ),
                        "next_state": envelope.get("next_state", "human_decision_required"),
                        "terminal_snapshot": {
                            "branch": self.inspector.current_branch,
                            "head": self.inspector.head,
                            "clean": self.inspector.is_clean,
                        },
                    },
                )
            self.ledger.append(
                event_type="RecoveryApplied",
                transaction_id=str(recovered),
                workflow_type=plan["workflow_type"],
                payload={
                    "recovery_transaction_id": transaction_id,
                    "classification": classification,
                    "archived_lease": str(archived_lease) if archived_lease else None,
                },
            )
        if classification == "resume":
            self.ledger.append(
                event_type="RecoveryApplied", transaction_id=str(recovered),
                workflow_type=plan["workflow_type"],
                payload={
                    "recovery_transaction_id": transaction_id,
                    "classification": "stale_lease_takeover_for_resume",
                    "archived_lease": str(archived_lease) if archived_lease else None,
                },
            )
            if plan.get("workflow_type") == WorkflowType.RECOVERY.value:
                self.ledger.append(
                    event_type="TransactionSuperseded",
                    transaction_id=str(recovered),
                    workflow_type=WorkflowType.RECOVERY,
                    payload={
                        "classification": "INTERRUPTED_RECOVERY_SUPERSEDED",
                        "terminal_state": "superseded",
                        "superseded_by": transaction_id,
                        "reference": transaction_id,
                        "next_state": resolved_next_state,
                        "terminal_snapshot": {
                            "branch": self.inspector.current_branch,
                            "head": self.inspector.head,
                            "clean": self.inspector.is_clean,
                        },
                    },
                )
        recovery_completion = recovery_kernel.complete(
            classification="RECOVERY_APPLIED",
            evidence={"recovered_transaction_id": recovered},
        )
        continuation_record = None
        if classification == "resume" and plan.get("workflow_type") != WorkflowType.RECOVERY.value:
            recovered_start = next(
                event for event in recovered_events if event["event_type"] == "TransactionStarted"
            )["payload"]
            policy_value = recovered_start.get("allowed_mutation_policy") or {}
            continuation_policy = MutationPolicy(
                tuple(policy_value.get("allowed_paths") or ()),
                allowed_prefixes=tuple(policy_value.get("allowed_prefixes") or ()),
                denied_paths=tuple(policy_value.get("denied_paths") or ()),
                denied_prefixes=tuple(policy_value.get("denied_prefixes") or ()),
                allow_untracked=bool(policy_value.get("allow_untracked", False)),
                require_clean_start=bool(policy_value.get("require_clean_start", True)),
                commit_subject=policy_value.get("commit_subject"),
            )
            workflow = WorkflowType(plan["workflow_type"])
            continuation_session_id = next((
                event["payload"].get("session_id")
                for event in reversed(recovered_events)
                if event["event_type"] == "SessionLaunched"
                and event["payload"].get("session_id")
            ), None)
            continuation_record = self.lease.acquire(
                lease_type=WORKFLOW_LEASE[workflow],
                repository_identity=self.ledger.repository_identity,
                repository_path_fingerprint=self.ledger.repository_path_fingerprint,
                project_id=self.project.project_id,
                transaction_id=str(recovered), workflow_type=workflow,
                milestone=recovered_start.get("milestone"),
                feature_id=recovered_start.get("feature_id"),
                starting_branch=str(recovered_start.get("starting_branch")),
                starting_head=str(recovered_start.get("starting_head")),
                run_id=str(recovered_start.get("run_id")),
                session_id=continuation_session_id,
                policy=continuation_policy,
            )
            self.ledger.append(
                event_type="LeaseAcquired", transaction_id=str(recovered),
                workflow_type=workflow,
                payload={
                    "lease_id": continuation_record.lease_id,
                    "lease_type": continuation_record.lease_type.value,
                    "recovery_transaction_id": transaction_id,
                    "continuation": True,
                },
            )
        projection = (
            self.projection.rebuild(persist_cache=True)
            if continuation_record is not None
            else recovery_completion["projection"]
        )
        return {
            **plan,
            "mutation_performed": True,
            "recovery_transaction_id": transaction_id,
            "archived_lease": str(archived_lease) if archived_lease else None,
            "action": (
                "resume_exact_transaction"
                if classification == "resume"
                and plan.get("workflow_type") != WorkflowType.RECOVERY.value
                else None
            ),
            "continuation_lease_id": continuation_record.lease_id if continuation_record else None,
            "projection": projection,
        }

    def _revalidate_planned_recovery_baseline(self, plan: dict[str, Any]) -> None:
        """Close inspect-to-observation drift before starting recovery evidence."""

        if plan.get("classification") == "commit_succeeded_before_evidence":
            commit = plan.get("commit")
            planned_paths = tuple(plan.get("changed_paths") or ())
            feature_branch = plan.get("feature_branch_created")
            if (
                plan.get("workflow_type") == WorkflowType.FEATURE_PREPARATION.value
                and isinstance(feature_branch, str)
            ):
                current_fingerprint = self.inspector.content_diff_fingerprint(
                    (), commit=str(commit)
                )
                if (
                    plan.get("observed_clean") is not True
                    or not self.inspector.is_clean
                    or self.inspector.current_branch != plan.get("observed_branch")
                    or self.inspector.current_branch != feature_branch
                    or self.inspector.head != plan.get("observed_head")
                    or self.inspector.head != commit
                    or self.inspector.rev_parse(feature_branch, check=False) != commit
                    or planned_paths
                    or current_fingerprint != plan.get("diff_fingerprint")
                ):
                    raise RecoveryError(
                        "repository changed after feature-branch recovery inspection"
                    )
                return
            current_paths = tuple(sorted(
                self.inspector.git([
                    "diff-tree", "--no-commit-id", "--name-only", "-r", str(commit)
                ]).stdout.splitlines()
            ))
            if plan.get("workflow_type") == WorkflowType.MILESTONE_INTEGRATION.value:
                current_fingerprint = self.inspector.patch_fingerprint(str(commit))
            else:
                current_fingerprint = self.inspector.content_diff_fingerprint(
                    current_paths, commit=str(commit)
                )
            if (
                plan.get("observed_clean") is not True
                or not self.inspector.is_clean
                or self.inspector.current_branch != plan.get("observed_branch")
                or self.inspector.head != plan.get("observed_head")
                or self.inspector.head != commit
                or current_paths != planned_paths
                or current_fingerprint != plan.get("diff_fingerprint")
            ):
                raise RecoveryError(
                    "repository changed after clean commit recovery inspection"
                )
            return

        if plan.get("classification") != "resume":
            return
        planned_paths = tuple(plan.get("changed_paths") or ())
        planned_fingerprint = plan.get("diff_fingerprint")
        if not planned_paths:
            return
        current_paths = tuple(sorted(
            set(self.inspector.tracked_changed_paths())
            | set(self.inspector.untracked_file_hashes())
        ))
        if (
            self.inspector.current_branch != plan.get("starting_branch")
            or self.inspector.head != plan.get("starting_head")
            or current_paths != planned_paths
            or not isinstance(planned_fingerprint, str)
            or self.inspector.content_diff_fingerprint(current_paths)
            != planned_fingerprint
        ):
            raise RecoveryError(
                "repository changed after exact dirty recovery inspection"
            )

    def _repair_projection(self, plan: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(uuid.uuid4())
        current = self.projection.rebuild(persist_cache=False)
        adapter = RecoveryAdapter(
            allowed_paths=(), commit_subject="factory: repair projection",
            next_state=current["current_state"],
        )
        kernel = WorkflowKernel(
            project=self.project, ledger=self.ledger, projection=self.projection,
            lease=self.lease,
        )
        transaction = kernel.begin(
            workflow_type=WorkflowType.RECOVERY, milestone=self.project.active_milestone,
            feature_id=None, run_id=f"recovery-{transaction_id}",
            policy=adapter.policy, transaction_id=transaction_id,
        )
        kernel.acquire_lease(); kernel.capture_snapshot()
        session_id = f"deterministic-recovery:{transaction_id}"
        kernel.session_launched(session_id)
        kernel.accept_result(SessionResultEnvelope.from_dict({
            "schema_version": 1, "workflow_type": "recovery",
            "classification": "RECOVERY_APPLIED", "project_id": self.project.project_id,
            "repository_identity": self.ledger.repository_identity,
            "transaction_id": transaction.transaction_id, "run_id": transaction.run_id,
            "session_id": session_id, "starting_branch": transaction.starting_branch,
            "starting_commit": transaction.starting_head, "current_commit": transaction.starting_head,
            "feature_id": None, "changed_paths": [],
            "evidence": {"repair": "projection_cache", "prior_ledger_sequence": current["ledger_sequence"]},
            "next_state": current["current_state"],
        }))
        kernel.record_file_mutation_boundary()
        kernel.validate(
            authority=CommandAuthority(), command_results=(),
            semantic_validator=adapter.semantic_validate,
        )
        kernel.finalize()
        projection = kernel.complete(classification="RECOVERY_APPLIED")["projection"]
        return {**plan, "mutation_performed": True, "recovery_transaction_id": transaction_id, "projection": projection}

    @staticmethod
    def _recovery_lease_type():
        from .contracts import LeaseType
        return LeaseType.RECOVERY_WRITER
