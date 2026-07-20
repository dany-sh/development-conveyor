"""Deterministic, idempotent import of legacy Conveyor evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import LeaseType, MutationPolicy, WorkflowType, fingerprint
from .errors import EvidenceError, RecoveryError
from .ledger import EvidenceLedger
from .projection import ProjectionEngine
from .queue import FeatureQueue, resolve_queue_path
from .registry import Project
from .repository import RepositoryInspector
from .snapshots import capture_repository_snapshot
from .workflow_lease import WorkflowWriterLease
from .workflow_recovery import RecoveryPlanner


MIGRATION_NAMESPACE = uuid.UUID("071ecea0-a044-4a32-a493-25aca509cc14")


@dataclass(frozen=True)
class LegacySource:
    source_id: str
    path: Path
    category: str
    content_sha256: str
    source_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "path": str(self.path),
            "category": self.category,
            "content_sha256": self.content_sha256,
            "source_fingerprint": self.source_fingerprint,
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _source(source_id: str, path: Path, category: str) -> LegacySource | None:
    if path.is_symlink() or not path.is_file():
        return None
    payload = path.read_bytes()
    content = hashlib.sha256(payload).hexdigest()
    return LegacySource(
        source_id=source_id,
        path=path.resolve(),
        category=category,
        content_sha256=content,
        source_fingerprint=fingerprint({"source_id": source_id, "content_sha256": content}),
    )


class LegacyStateMigrator:
    def __init__(
        self, *, controller_root: Path, project: Project,
        interruption_hook: Callable[[str], None] | None = None,
    ):
        self.controller_root = controller_root.expanduser().resolve()
        self.project = project
        self.inspector = RepositoryInspector(project.repository)
        identity = self.inspector.identity()
        project_root = self.controller_root / "state/projects" / project.project_id
        self.ledger = EvidenceLedger(
            project_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        self.projection = ProjectionEngine(self.ledger, project_root / "projection-cache.json")
        self.interrupt = interruption_hook or (lambda boundary: None)

    def discover(self) -> list[LegacySource]:
        sources: list[LegacySource] = []
        candidates = [
            ("controller_project_state", self.controller_root / "state/projects" / f"{self.project.project_id}.json", "project_state"),
            ("repository_cycle_state", self.project.repository / ".factory/conveyor-state.json", "cycle_state"),
            ("repository_project_adapter", self.project.repository / ".factory/project.yaml", "project_adapter"),
            ("repository_feature_queue", resolve_queue_path(self.project.repository, self.project.queue_location), "queue"),
        ]
        state = _read_json(candidates[0][1])
        cycle = _read_json(candidates[1][1])
        run_ids = {
            str(value) for value in (
                (state or {}).get("run_id"),
                (cycle or {}).get("conveyor_run_id"),
            ) if isinstance(value, str) and value
        }
        for run_id in sorted(run_ids):
            run_directory = self.controller_root / "reports" / run_id
            if run_directory.is_symlink() or not run_directory.is_dir():
                continue
            for report in sorted(run_directory.glob("*.json")):
                candidates.append((f"report:{run_id}:{report.name}", report, "session_report"))
        for source_id, path, category in candidates:
            item = _source(source_id, path, category)
            if item is not None:
                sources.append(item)
        return sources

    def _queue(self) -> dict[str, Any]:
        value = _read_json(resolve_queue_path(self.project.repository, self.project.queue_location))
        if value is None:
            raise RecoveryError("legacy queue is malformed")
        return value

    def _resolve_self_commit(self, feature: dict[str, Any]) -> str:
        branch = feature.get("branch")
        starting = feature.get("integration_base_commit")
        if not isinstance(branch, str) or not isinstance(starting, str):
            raise RecoveryError("SELF accepted commit lacks branch or integration base")
        head = self.inspector.rev_parse(branch, check=False)
        if head is None or not self.inspector.is_ancestor(starting, head):
            raise RecoveryError("SELF accepted commit branch does not descend from its recorded start")
        if self.inspector.commit_count(starting, head) != 1:
            raise RecoveryError("SELF accepted commit must resolve to exactly one feature commit")
        if self.inspector.current_branch == branch and self.inspector.head != head:
            raise RecoveryError("SELF accepted commit branch identity is contradictory")
        return head

    def derive_projection(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        queue = self._queue()
        queue_model = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        cycle = _read_json(self.project.repository / ".factory/conveyor-state.json") or {}
        controller = _read_json(
            self.controller_root / "state/projects" / f"{self.project.project_id}.json"
        ) or {}
        milestones = queue.get("milestones") if isinstance(queue.get("milestones"), list) else []
        features = queue.get("features") if isinstance(queue.get("features"), list) else []
        configured_milestone = self.project.active_milestone
        if not isinstance(configured_milestone, str) or not configured_milestone:
            active = [
                item for item in milestones
                if isinstance(item, dict) and item.get("status") == "active"
            ]
            if len(active) != 1:
                raise RecoveryError("legacy migration requires one resolved active milestone")
            configured_milestone = str(active[0].get("id") or "")
        active_milestone = queue_model.milestone(configured_milestone)
        if active_milestone is None:
            raise RecoveryError("configured active milestone is absent or ambiguous in the legacy queue")
        milestone_id = str(active_milestone["id"])
        milestone_features = queue_model.features_for_milestone(milestone_id)
        ready = [item for item in milestone_features if item.get("status") == "ready"]
        integration_selection = queue_model.select_integration(milestone_id)
        integration = [integration_selection.feature] if integration_selection is not None else []
        reconstructed: list[dict[str, Any]] = []
        superseded: list[dict[str, Any]] = []
        historical: list[dict[str, Any]] = []
        for feature in features:
            if not isinstance(feature, dict) or feature.get("status") not in {"integrated", "done"}:
                continue
            integrated = feature.get("integrated_commit") or feature.get("commit")
            if isinstance(integrated, str) and integrated and self.inspector.rev_parse(integrated, check=False):
                historical.append({
                    "feature_id": feature.get("id"),
                    "classification": "INTEGRATED",
                    "accepted_commit": feature.get("accepted_commit") or feature.get("commit"),
                    "integrated_commit": integrated,
                    "terminal_head": cycle.get("milestone_post_integration_commit") if cycle.get("current_feature") == feature.get("id") else None,
                    "classification_source": "corroborated",
                })
        selected_feature = None
        accepted_commit = None
        selected_start = None
        current_state = "queue_reconciliation"
        next_action = "queue_reconciliation"
        integration_status = None
        planning_status = None
        if integration:
            feature = integration[0]
            selected_feature = feature.get("id")
            accepted = feature.get("accepted_commit")
            if accepted == "SELF":
                accepted_commit = self._resolve_self_commit(feature)
            elif isinstance(accepted, str) and self.inspector.rev_parse(accepted, check=False):
                accepted_commit = accepted
            else:
                raise RecoveryError("integration-pending feature lacks a corroborated accepted commit")
            selected_start = feature.get("integration_base_commit")
            current_state = "integration_ready"
            next_action = "milestone_integration"
            integration_status = "pending"
            reconstructed.append({
                "workflow_type": "feature_acceptance",
                "feature_id": selected_feature,
                "classification": "FEATURE_ACCEPTED",
                "accepted_commit": accepted_commit,
                "classification_source": "corroborated",
            })
            if (
                cycle.get("current_feature") == selected_feature
                and cycle.get("current_phase") not in {"completed", "feature_accepted", "integration_pending", "integration_ready"}
            ):
                superseded.append({
                    "workflow_type": "feature_execution",
                    "run_id": cycle.get("conveyor_run_id"),
                    "session_id": cycle.get("feature_session_id") or cycle.get("session_id"),
                    "feature_id": selected_feature,
                    "legacy_phase": cycle.get("current_phase"),
                    "classification": "superseded",
                    "reason": "accepted commit and queue integration_pending evidence supersede stale feature cycle",
                })
        elif ready:
            feature = sorted(ready, key=lambda item: (str(item.get("priority", "")), str(item.get("id"))))[0]
            selected_feature = feature.get("id")
            selected_start = (
                ((controller.get("state_evidence") or {}).get("planning_transaction") or {}).get("selected_feature_starting_commit")
                or self.inspector.head
            )
            current_state = "feature_ready"
            next_action = "feature_cycle"
            planning_status = "passed"
            planning = (controller.get("state_evidence") or {}).get("planning_transaction") or {}
            if planning:
                reconstructed.append({
                    "workflow_type": "queue_reconciliation",
                    "run_id": planning.get("run_id") or controller.get("run_id"),
                    "session_id": planning.get("session_id"),
                    "classification": planning.get("terminal_classification") or "RECONCILED_READY_WORK",
                    "result_commit": planning.get("planning_result_commit"),
                    "classification_source": "corroborated",
                })
        projection = {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "repository_identity": self.inspector.identity()["repository_id"],
            "repository_path_fingerprint": self.inspector.identity()["path_fingerprint"],
            "current_state": current_state,
            "active_transaction": None,
            "current_feature": selected_feature,
            "selected_next_feature": selected_feature,
            "selected_feature_starting_commit": selected_start,
            "accepted_feature_commit": accepted_commit,
            "feature_branch": integration[0].get("branch") if integration else (ready[0].get("branch") if ready else None),
            "milestone": milestone_id,
            "milestone_branch": active_milestone.get("integration_branch") or self.project.milestone_branch,
            "integration_status": integration_status,
            "planning_status": planning_status,
            "human_gate": None,
            "allowed_next_action": next_action,
            "session_resume_eligible": False,
            "old_session_resume": False,
            "required_lease": "integration_writer" if integration else ("feature_writer" if ready else None),
            "historical_integration_outcomes": historical,
            "current_repository_consistency": "consistent" if self.inspector.is_clean else "unsafe_dirty",
            "legacy_cycle_classification": "superseded" if superseded else ("corroborated" if cycle else None),
            "transactions": reconstructed,
            "warnings": [],
        }
        projection["projection_fingerprint"] = fingerprint(projection)
        return projection, reconstructed, superseded

    def classify_sources(self, sources: list[LegacySource]) -> list[dict[str, Any]]:
        queue = self._queue()
        queue_model = FeatureQueue.from_location(
            self.project.repository, self.project.queue_location
        )
        active_milestone = queue_model.milestone(self.project.active_milestone or "")
        active_features = (
            queue_model.features_for_milestone(str(active_milestone["id"]))
            if active_milestone is not None else []
        )
        integration_pending = {
            item.get("id") for item in active_features
            if item.get("status") in {"accepted", "integration_pending", "integrating"}
        }
        results: list[dict[str, Any]] = []
        expected_path = self.inspector.identity()["path_fingerprint"]
        for source in sources:
            value = _read_json(source.path)
            classification = "corroborated"
            diagnostic = None
            if value is None:
                classification = "corrupt"
                diagnostic = "source is not a JSON object"
            elif source.category == "project_state":
                if value.get("project_id") != self.project.project_id:
                    classification = "contradictory"
                    diagnostic = "project identity mismatch"
                elif value.get("repository_fingerprint") not in {None, expected_path}:
                    classification = "contradictory"
                    diagnostic = "repository path fingerprint mismatch"
                elif not value.get("current_state"):
                    classification = "incomplete"
                    diagnostic = "project state lacks current_state"
            elif source.category == "cycle_state":
                if value.get("project_id") != self.project.project_id:
                    classification = "contradictory"
                    diagnostic = "cycle project identity mismatch"
                else:
                    recorded_identity = value.get("repository_identity")
                    if isinstance(recorded_identity, dict) and recorded_identity.get("repository_id") not in {
                        None, self.inspector.identity()["repository_id"]
                    }:
                        classification = "contradictory"
                        diagnostic = "cycle repository identity mismatch"
                    elif (
                        value.get("current_feature") in integration_pending
                        and value.get("current_phase") not in {"completed", "integration_pending", "integration_ready"}
                    ):
                        classification = "superseded"
                        diagnostic = "accepted queue evidence supersedes stale feature cycle"
                    elif not value.get("conveyor_run_id"):
                        classification = "incomplete"
                        diagnostic = "cycle lacks run identity"
            elif source.category == "queue":
                if value.get("schema_version") != 1 or not isinstance(value.get("features"), list):
                    classification = "corrupt"
                    diagnostic = "queue schema is malformed"
            elif source.category == "project_adapter":
                adapter_project = value.get("project") if isinstance(value.get("project"), dict) else {}
                adapter_id = adapter_project.get("id")
                if adapter_id is not None and not (
                    adapter_id == self.project.project_id
                    or (isinstance(adapter_id, str) and adapter_id.endswith(f"-{self.project.project_id}"))
                ):
                    classification = "contradictory"
                    diagnostic = "repository adapter project identity mismatch"
                elif value.get("schema_version") != 1:
                    classification = "corrupt"
                    diagnostic = "repository adapter schema is malformed"
            elif source.category == "session_report":
                if value.get("project_id") not in {None, self.project.project_id}:
                    classification = "contradictory"
                    diagnostic = "session report project identity mismatch"
                elif value.get("working_directory") not in {None, str(self.project.repository)}:
                    classification = "contradictory"
                    diagnostic = "session report repository path mismatch"
                elif not value.get("run_id"):
                    classification = "incomplete"
                    diagnostic = "session report lacks run identity"
            results.append({
                **source.to_dict(),
                "classification": classification,
                "diagnostic": diagnostic,
            })
        return results

    def plan(self) -> dict[str, Any]:
        sources = self.discover()
        after, reconstructed, superseded = self.derive_projection()
        ledger_exists = self.ledger.path.exists()
        before = self.projection.rebuild(persist_cache=False) if ledger_exists else {
            "current_state": (_read_json(self.controller_root / "state/projects" / f"{self.project.project_id}.json") or {}).get("current_state"),
            "source": "legacy_cache",
        }
        existing_sources = {
            event["payload"].get("source_fingerprint")
            for event in self.ledger.read()
            if event["event_type"] == "LegacyEvidenceImported"
        } if ledger_exists else set()
        would_import = [item for item in sources if item.source_fingerprint not in existing_sources]
        classified = self.classify_sources(sources)
        return {
            "schema_version": 1,
            "project_id": self.project.project_id,
            "mode": "dry-run",
            "legacy_sources_discovered": classified,
            "events_that_would_be_appended": [
                {"event_type": "LegacyEvidenceImported", "source_id": item.source_id, "source_fingerprint": item.source_fingerprint}
                for item in would_import
            ],
            "transactions_that_would_be_reconstructed": reconstructed,
            "transactions_that_would_be_superseded": superseded,
            "projected_state_before": before,
            "projected_state_after": after,
            "repository_mutations": [
                str(self.ledger.path),
                str(self.projection.cache_path),
            ] if would_import else [],
            "application_git_mutations": [],
            "application_repository_written": False,
            "migration_applied": False,
            "idempotent": not would_import,
        }

    def apply(self) -> dict[str, Any]:
        plan = self.plan()
        sources = self.discover()
        classified = {item["source_fingerprint"]: item for item in self.classify_sources(sources)}
        blocked = [
            item for item in classified.values()
            if item["classification"] in {"contradictory", "corrupt"}
        ]
        if blocked:
            raise RecoveryError(
                "legacy migration stopped on "
                + ", ".join(f"{item['source_id']}:{item['classification']}" for item in blocked)
            )
        target_projection, reconstructed, superseded = self.derive_projection()
        events = self.ledger.read() if self.ledger.path.exists() else []
        imported = {
            event["payload"].get("source_fingerprint")
            for event in events if event["event_type"] == "LegacyEvidenceImported"
        }
        pending = [source for source in sources if source.source_fingerprint not in imported]
        migration_transactions: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event["workflow_type"] == WorkflowType.RECOVERY.value and event["payload"].get("migration_batch"):
                migration_transactions.setdefault(event["transaction_id"], []).append(event)
        unfinished = [
            (transaction_id, items) for transaction_id, items in migration_transactions.items()
            if not any(item["event_type"] == "ProjectionUpdated" for item in items)
        ]
        if unfinished:
            transaction_id, prior_items = unfinished[-1]
            start_event = next(item for item in prior_items if item["event_type"] == "TransactionStarted")
            batch_fingerprints = start_event["payload"]["source_fingerprints"]
            pending = [source for source in sources if source.source_fingerprint in batch_fingerprints and source.source_fingerprint not in imported]
        else:
            batch_fingerprints = sorted(source.source_fingerprint for source in pending)
            transaction_id = str(uuid.uuid5(
                MIGRATION_NAMESPACE,
                f"{self.project.project_id}:" + fingerprint(batch_fingerprints),
            )) if pending else ""
            prior_items = []
        if not pending and not unfinished:
            projection = self.projection.rebuild(persist_cache=True)
            return {
                **plan,
                "mode": "apply",
                "events_appended": 0,
                "migration_applied": False,
                "projected_state_after": projection,
                "application_git_mutations": [],
                "application_repository_written": False,
                "idempotent": True,
            }
        started = any(item["event_type"] == "TransactionStarted" for item in prior_items)
        terminal = next((item for item in prior_items if item["event_type"] == "TransactionCompleted"), None)
        project_state_root = self.controller_root / "state/projects" / self.project.project_id
        lease = WorkflowWriterLease(project_state_root / "migration-writer.json")
        if not started:
            self.ledger.append(
                event_type="TransactionStarted",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "run_id": f"legacy-migration-{self.project.project_id}",
                    "legacy_import": True,
                    "migration_batch": True,
                    "source_fingerprints": batch_fingerprints,
                    "starting_branch": self.inspector.current_branch,
                    "starting_head": self.inspector.head,
                    "allowed_mutation_policy": MutationPolicy(tuple()).to_dict(),
                },
            )
            prior_items = self.ledger.read()
        lease_record = lease.read()
        if lease_record is not None:
            stale = lease.stale_evidence()
            if stale.get("recoverable") is not True:
                raise RecoveryError(
                    "legacy migration writer lease cannot be displaced without exact stale-process proof"
                )
            archived = RecoveryPlanner(
                project=self.project, ledger=self.ledger,
                projection=self.projection, lease=lease,
            )._archive_stale_lease(lease_record)
            self.ledger.append(
                event_type="RecoveryApplied",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "migration_batch": True,
                    "classification": "stale_migration_lease_recovered",
                    "archived_lease": str(archived),
                    "recovered_lease_id": lease_record.lease_id,
                },
            )
            lease_record = None
        if terminal is None and lease_record is None:
            lease_record = lease.acquire(
                lease_type=LeaseType.RECOVERY_WRITER,
                repository_identity=self.ledger.repository_identity,
                repository_path_fingerprint=self.ledger.repository_path_fingerprint,
                project_id=self.project.project_id,
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                milestone=self.project.active_milestone,
                feature_id=None,
                starting_branch=self.inspector.current_branch or "DETACHED",
                starting_head=self.inspector.head,
                run_id=f"legacy-migration-{self.project.project_id}",
                session_id=None,
                policy=MutationPolicy(tuple()),
            )
            self.ledger.append(
                event_type="LeaseAcquired",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "migration_batch": True,
                    "lease_id": lease_record.lease_id,
                    "lease_type": lease_record.lease_type.value,
                },
            )
            self.interrupt("after_lease")
            snapshot = capture_repository_snapshot(self.project)
            self.ledger.append(
                event_type="SnapshotCaptured",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={"migration_batch": True, "snapshot": snapshot.to_dict()},
            )
            self.ledger.append(
                event_type="ValidationStarted",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={"migration_batch": True, "source_count": len(batch_fingerprints)},
            )
            self.ledger.append(
                event_type="ValidationPassed",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "migration_batch": True,
                    "repository_identity": self.ledger.repository_identity,
                    "application_git_mutations": [],
                },
            )
        elif terminal is None:
            lease.revalidate(
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                repository_identity=self.ledger.repository_identity,
                project_id=self.project.project_id,
            )
        appended = 0
        for source in pending:
            if self.ledger.event_by_source_fingerprint(source.source_fingerprint) is not None:
                continue
            self.ledger.append(
                event_type="LegacyEvidenceImported",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    **source.to_dict(),
                    "legacy_import": True,
                    "migration_batch": True,
                    "classification": classified[source.source_fingerprint]["classification"],
                    "diagnostic": classified[source.source_fingerprint]["diagnostic"],
                },
            )
            appended += 1
        if terminal is None:
            self.ledger.append(
                event_type="ChangesDetected",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "migration_batch": True,
                    "controller_evidence_sources": batch_fingerprints,
                    "application_git_mutations": [],
                },
            )
            self.ledger.append(
                event_type="TransactionCompleted",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "classification": "RECOVERY_APPLIED",
                    "next_state": target_projection["current_state"],
                    "feature_id": target_projection.get("current_feature"),
                    "selected_feature": target_projection.get("selected_next_feature"),
                    "accepted_feature_commit": target_projection.get("accepted_feature_commit"),
                    "integration_status": target_projection.get("integration_status"),
                    "planning_status": target_projection.get("planning_status"),
                    "terminal_snapshot": {
                        "head": self.inspector.head,
                        "branch": self.inspector.current_branch,
                        "clean": self.inspector.is_clean,
                    },
                    "reconstructed": reconstructed,
                    "superseded": superseded,
                    "legacy_import": True,
                    "migration_batch": True,
                },
            )
            terminal = True
            self.interrupt("after_terminal")
        if lease.read() is not None:
            lease.release(
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                repository_identity=self.ledger.repository_identity,
                project_id=self.project.project_id,
            )
            self.ledger.append(
                event_type="LeaseReleased",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={"migration_batch": True, "lease_id": lease_record.lease_id if lease_record else None},
            )
            self.interrupt("after_release")
        has_projection = any(
            event["transaction_id"] == transaction_id and event["event_type"] == "ProjectionUpdated"
            for event in self.ledger.read()
        )
        if not has_projection:
            facts = {
                key: value for key, value in target_projection.items()
                if key not in {
                    "projection_fingerprint", "ledger_sequence", "ledger_fingerprint", "transactions"
                }
            }
            self.ledger.append(
                event_type="ProjectionUpdated",
                transaction_id=transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                payload={
                    "migration_batch": True,
                    "current_state": target_projection["current_state"],
                    "current_feature": target_projection.get("current_feature"),
                    "selected_feature": target_projection.get("selected_next_feature"),
                    "projection_facts": facts,
                },
            )
            self.interrupt("after_projection")
        projection = self.projection.rebuild(persist_cache=True)
        return {
            **plan,
            "mode": "apply",
            "events_appended": appended,
            "migration_applied": appended > 0 or bool(unfinished),
            "projected_state_after": projection,
            "application_git_mutations": [],
            "application_repository_written": False,
            "idempotent": appended == 0 and terminal is not None,
        }
