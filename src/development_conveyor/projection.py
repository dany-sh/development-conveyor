"""Rebuildable current-state projection over immutable workflow evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from .contracts import WORKFLOW_LEASE, WorkflowType, fingerprint
from .errors import ProjectionError, StaleProjectionCache
from .ledger import EvidenceLedger, LedgerIntegrity
from .logging import atomic_write_json


WORKFLOW_ACTIVE_STATE = {
    "queue_reconciliation": "queue_reconciliation",
    "feature_preparation": "feature_preparing",
    "feature_execution": "feature_running",
    "feature_acceptance": "feature_review",
    "milestone_integration": "integrating",
    "milestone_gate": "milestone_gate",
    "human_decision_resolution": "human_decision_required",
    "recovery": "queue_reconciliation",
}

NEXT_ACTION = {
    "feature_ready": "feature_cycle",
    "feature_accepted": "milestone_integration",
    "integration_pending": "milestone_integration",
    "integration_ready": "milestone_integration",
    "feature_integrated": "queue_reconciliation",
    "queue_reconciliation": "queue_reconciliation",
    "milestone_gate": "milestone_gate",
    "human_decision_required": "human_decision_resolution",
    "milestone_complete": "human_merge_approval",
}


def projection_fingerprint(value: dict[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("projection_fingerprint", None)
    return fingerprint(unsigned)


def build_projection_observations(
    *, branch: str | None, head: str | None, clean: bool,
    git_operations: dict[str, bool], queue_feature: str | None,
    queue_integration_status: str | None, lease_transaction: str | None,
    lease_valid: bool, live_session_id: str | None = None,
) -> dict[str, Any]:
    """Build canonical, read-only current evidence without altering history."""

    value = {
        "branch": branch, "head": head, "clean": bool(clean),
        "git_operations": {key: bool(git_operations[key]) for key in sorted(git_operations)},
        "queue_feature": queue_feature,
        "queue_integration_status": queue_integration_status,
        "lease_transaction": lease_transaction, "lease_valid": bool(lease_valid),
        "live_session_id": live_session_id,
    }
    value["observation_fingerprint"] = fingerprint(value)
    return value


class ProjectionEngine:
    def __init__(self, ledger: EvidenceLedger, cache_path: Path | None = None):
        self.ledger = ledger
        self.cache_path = (
            Path(os.path.abspath(os.path.expanduser(str(cache_path))))
            if cache_path else None
        )
        if self.cache_path is not None and self.cache_path.parent != self.ledger.path.parent:
            raise ProjectionError("projection cache must be confined beside its evidence ledger")

    def _validate_cache_path(self) -> None:
        if self.cache_path is None:
            return
        try:
            parent = os.lstat(self.cache_path.parent)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise ProjectionError("projection cache parent is unsafe")
        try:
            target = os.lstat(self.cache_path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(target.st_mode) or not stat.S_ISREG(target.st_mode):
            raise ProjectionError("projection cache target is unsafe")
        if target.st_nlink != 1:
            raise ProjectionError("projection cache has an unsafe hard-link count")

    def rebuild(
        self, *, persist_cache: bool = True,
        observations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self.ledger.read()
        integrity = self.ledger.verify()
        transactions: dict[str, dict[str, Any]] = {}
        transaction_order: list[str] = []
        current_state = "discover"
        current_feature = None
        selected_next_feature = None
        accepted_feature_commit = None
        integration_status = None
        planning_status = None
        human_gate = None
        feature_commits: dict[str, str] = {}
        integrated_features: set[str] = set()
        historical_integration_outcomes: list[dict[str, Any]] = []
        warnings: list[str] = []
        projection_facts: dict[str, Any] = {}
        for event in events:
            transaction_id = event["transaction_id"]
            workflow = event["workflow_type"]
            payload = event["payload"]
            transaction = transactions.setdefault(transaction_id, {
                "transaction_id": transaction_id,
                "workflow_type": workflow,
                "state": "created",
                "feature_id": payload.get("feature_id"),
                "milestone": payload.get("milestone"),
                "run_id": payload.get("run_id"),
                "session_ids": [],
                "terminal_classification": None,
                "terminal_reference": None,
                "starting_snapshot": None,
                "terminal_snapshot": None,
                "last_sequence": event["sequence"],
            })
            if transaction_id not in transaction_order:
                transaction_order.append(transaction_id)
            transaction["last_sequence"] = event["sequence"]
            kind = event["event_type"]
            if kind == "TransactionStarted":
                transaction.update({
                    "state": "lease_pending",
                    "feature_id": payload.get("feature_id"),
                    "milestone": payload.get("milestone"),
                    "run_id": payload.get("run_id"),
                })
                current_state = WORKFLOW_ACTIVE_STATE.get(workflow, current_state)
                event_feature = payload.get("feature_id")
                if event_feature:
                    current_feature = event_feature
                    accepted_feature_commit = feature_commits.get(event_feature)
                    integration_status = "passed" if event_feature in integrated_features else None
            elif kind == "LeaseAcquired":
                transaction["state"] = "active"
            elif kind == "SnapshotCaptured":
                transaction["starting_snapshot"] = payload.get("snapshot")
                transaction["state"] = "active"
            elif kind == "SessionLaunched":
                session = payload.get("session_id")
                if session and session not in transaction["session_ids"]:
                    transaction["session_ids"].append(session)
                transaction["state"] = "result_pending"
            elif kind == "SessionResultAccepted":
                transaction["state"] = "validating"
            elif kind == "ValidationStarted":
                transaction["state"] = "validating"
            elif kind == "ValidationPassed":
                transaction["state"] = "finalizing"
            elif kind == "ValidationFailed":
                warnings.append(f"validation_failed:{transaction_id}")
            elif kind == "CommitFinalized":
                transaction["state"] = "finalizing"
            elif kind == "HumanGateRaised":
                transaction.update({
                    "state": "human_decision_required",
                    "terminal_classification": payload.get("classification", "HUMAN_DECISION_REQUIRED"),
                    "terminal_reference": payload.get("gate_id"),
                    "terminal_snapshot": payload.get("terminal_snapshot"),
                })
                human_gate = payload.get("gate") or payload
                current_state = "human_decision_required"
            elif kind == "HumanGateResolved":
                human_gate = None
            elif kind == "TransactionBlocked":
                transaction.update({
                    "state": payload.get("terminal_state", "blocked"),
                    "terminal_classification": payload.get("classification"),
                    "terminal_reference": payload.get("reference"),
                    "terminal_snapshot": payload.get("terminal_snapshot"),
                })
                current_state = payload.get("next_state", current_state)
            elif kind == "TransactionSuperseded":
                transaction.update({
                    "state": "superseded",
                    "terminal_classification": payload.get("classification", "TRANSACTION_SUPERSEDED"),
                    "terminal_reference": payload.get("superseded_by"),
                    "terminal_snapshot": payload.get("terminal_snapshot"),
                })
                current_state = payload.get("next_state", current_state)
            elif kind == "TransactionCompleted":
                transaction.update({
                    "state": "completed",
                    "terminal_classification": payload.get("classification"),
                    "terminal_reference": payload.get("reference"),
                    "terminal_snapshot": payload.get("terminal_snapshot"),
                })
                current_state = payload.get("next_state", current_state)
                current_feature = payload.get("feature_id", current_feature)
                selected_next_feature = payload.get("selected_feature", selected_next_feature)
                accepted_feature_commit = payload.get("accepted_feature_commit", accepted_feature_commit)
                if transaction.get("feature_id") and payload.get("accepted_feature_commit"):
                    feature_commits[transaction["feature_id"]] = payload["accepted_feature_commit"]
                integration_status = payload.get("integration_status", integration_status)
                planning_status = payload.get("planning_status", planning_status)
                if workflow == "milestone_integration":
                    integrated_feature = payload.get("feature_id") or transaction.get("feature_id")
                    if integrated_feature:
                        integrated_features.add(integrated_feature)
                    historical_integration_outcomes.append({
                        "transaction_id": transaction_id,
                        "classification": payload.get("classification"),
                        "feature_id": payload.get("feature_id"),
                        "accepted_commit": payload.get("accepted_feature_commit"),
                        "integrated_commit": payload.get("integrated_commit"),
                        "terminal_head": (payload.get("terminal_snapshot") or {}).get("head"),
                        "terminal_repository_clean": (payload.get("terminal_snapshot") or {}).get("clean"),
                        "sequence": event["sequence"],
                    })
            elif kind == "ProjectionUpdated":
                current_state = payload.get("current_state", current_state)
                selected_next_feature = payload.get("selected_feature", selected_next_feature)
                current_feature = payload.get("current_feature", current_feature)
                facts = payload.get("projection_facts")
                if isinstance(facts, dict):
                    projection_facts.update(facts)
        active_ids = [
            transaction_id for transaction_id in transaction_order
            if transactions[transaction_id]["state"] not in {
                "completed", "blocked", "human_decision_required", "retryable_failure",
                "terminal_failure", "superseded",
            }
        ]
        if len(active_ids) > 1:
            raise ProjectionError(
                "multiple incomplete transactions violate single-writer projection invariants"
            )
        active_transaction = active_ids[-1] if active_ids else None
        current_accepted_commit = feature_commits.get(current_feature) if current_feature else accepted_feature_commit
        if current_accepted_commit and current_state == "feature_running" and current_feature not in integrated_features:
            current_state = "integration_ready"
        session_resume_eligible = bool(
            active_transaction
            and transactions[active_transaction]["state"] in {"active", "result_pending"}
            and not current_accepted_commit
        )
        repository_consistency = "not_observed"
        observation_value = None
        if observations is not None:
            observation_value = dict(observations)
            claimed_observation = observation_value.pop("observation_fingerprint", None)
            if claimed_observation != fingerprint(observation_value):
                raise ProjectionError("current observation fingerprint is invalid")
            observation_value["observation_fingerprint"] = claimed_observation
            if any(observation_value.get("git_operations", {}).values()) or observation_value.get("branch") is None:
                repository_consistency = "unsafe_git_state"
            elif active_transaction and (
                not observation_value.get("lease_valid")
                or observation_value.get("lease_transaction") != active_transaction
            ):
                repository_consistency = "lease_mismatch"
            elif not active_transaction and observation_value.get("lease_transaction") is not None:
                repository_consistency = "orphaned_lease"
            elif (
                current_feature is not None and observation_value.get("queue_feature") is not None
                and observation_value.get("queue_feature") != current_feature
            ):
                repository_consistency = "queue_projection_disagreement"
            elif not observation_value.get("clean") and not active_transaction:
                repository_consistency = "unexplained_dirty_worktree"
            else:
                repository_consistency = "consistent"
            if active_transaction and transactions[active_transaction]["session_ids"]:
                latest_session = transactions[active_transaction]["session_ids"][-1]
                if observation_value.get("live_session_id") not in {None, latest_session}:
                    session_resume_eligible = False
                    repository_consistency = "stale_session"
            observed_integration = observation_value.get("queue_integration_status")
            if current_feature and observed_integration is not None:
                integration_status = observed_integration
        projection = {
            "schema_version": 1,
            "project_id": self.ledger.project_id,
            "repository_identity": self.ledger.repository_identity,
            "repository_path_fingerprint": self.ledger.repository_path_fingerprint,
            "ledger_sequence": integrity.sequence,
            "ledger_fingerprint": integrity.fingerprint,
            "current_state": current_state,
            "active_transaction": active_transaction,
            "current_feature": current_feature,
            "selected_next_feature": selected_next_feature,
            "accepted_feature_commit": current_accepted_commit,
            "integration_status": integration_status,
            "planning_status": planning_status,
            "human_gate": human_gate,
            "allowed_next_action": NEXT_ACTION.get(current_state, "verify_consistency"),
            "session_resume_eligible": session_resume_eligible,
            "required_lease": (
                WORKFLOW_LEASE[WorkflowType(transactions[active_transaction]["workflow_type"])].value
                if active_transaction else None
            ),
            "historical_integration_outcomes": historical_integration_outcomes,
            "current_repository_consistency": repository_consistency,
            "current_observations": observation_value,
            "transactions": [transactions[item] for item in transaction_order],
            "warnings": warnings,
        }
        projection.update(projection_facts)
        if repository_consistency not in {"not_observed", "consistent"}:
            projection["allowed_next_action"] = "verify_consistency"
        projection["projection_fingerprint"] = projection_fingerprint(projection)
        if persist_cache and self.cache_path is not None:
            self._validate_cache_path()
            atomic_write_json(self.cache_path, projection)
        return projection

    def load_cache(self) -> dict[str, Any] | None:
        if self.cache_path is None:
            return None
        self._validate_cache_path()
        if not self.cache_path.exists():
            return None
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectionError("projection cache is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ProjectionError("projection cache has an invalid schema")
        claimed = value.get("projection_fingerprint")
        if claimed != projection_fingerprint(value):
            raise ProjectionError("projection cache fingerprint is invalid")
        integrity = self.ledger.verify()
        sequence = value.get("ledger_sequence")
        if not isinstance(sequence, int):
            raise ProjectionError("projection cache ledger sequence is invalid")
        if sequence > integrity.sequence:
            raise ProjectionError("projection cache claims a later sequence than the ledger")
        if sequence < integrity.sequence:
            raise StaleProjectionCache("projection cache is older than the ledger")
        if sequence == integrity.sequence and value.get("ledger_fingerprint") != integrity.fingerprint:
            raise ProjectionError("projection cache does not match the ledger fingerprint")
        return value

    def current(self) -> dict[str, Any]:
        try:
            cache = self.load_cache()
        except StaleProjectionCache:
            cache = None
        integrity = self.ledger.verify()
        if (
            cache is not None
            and cache["ledger_sequence"] == integrity.sequence
            and cache["ledger_fingerprint"] == integrity.fingerprint
        ):
            return cache
        return self.rebuild(persist_cache=True)


def cache_agrees(cache: dict[str, Any], projection: dict[str, Any]) -> bool:
    return (
        cache.get("ledger_sequence") == projection.get("ledger_sequence")
        and cache.get("ledger_fingerprint") == projection.get("ledger_fingerprint")
        and cache.get("projection_fingerprint") == projection.get("projection_fingerprint")
    )
