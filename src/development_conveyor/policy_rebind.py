"""Deterministic execution-policy rebinding for one ready, unstarted feature."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .autopilot import AutopilotPaths
from .config import Configuration, load_json
from .contracts import TransactionState, WorkflowType, fingerprint
from .cycle_cache import (
    cycle_cache_semantics_from_projection,
    write_terminal_cycle_cache,
)
from .errors import ConveyorError, QueueError, RecoveryError
from .execution_profiles import (
    FeatureExecutionPolicy,
    markdown_execution_policy,
    render_markdown_execution_policy,
    resolve_execution_profile,
    validate_feature_execution_policy,
)
from .kernel import QueueReconciliationAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, make_lock_record
from .logging import atomic_write_bytes, utc_now
from .planning import (
    _diff_check,
    _inventory_validation,
    _planning_diff_fingerprint_with_replacements,
)
from .projection import ProjectionEngine
from .queue import FeatureQueue, load_queue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import SessionLauncher
from .workflow_lease import WorkflowWriterLease

if TYPE_CHECKING:
    from .cycle_engine import CycleEngine


POLICY_REBIND_VALIDATORS = (
    "exact project and repository identity",
    "exact expected branch and HEAD",
    "feature_ready projection with exact selected feature",
    "no active transaction or model session",
    "no writer lease, controller reservation, or Autopilot ownership",
    "clean worktree and no unfinished Git operation",
    "ready queue feature and matching specification",
    "exact expected old execution policy",
    "exact queue and specification normalization paths",
    "target execution profile schema",
    "exact local Codex model and reasoning compatibility",
    "feature inventory validation",
    "git diff --check",
)
_POLICY_SECTION = (
    r"(?ms)^## Execution policy[ \t]*\r?\n"
    r"\r?\n"
    r"```(?:yaml|yml)[ \t]*\r?\n"
    r"execution_policy:[ \t]*\r?\n"
    r".*?"
    r"^```[ \t]*\r?\n?"
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _replace_specification_policy(
    feature_id: str,
    text: str,
    *,
    expected: dict[str, Any],
    replacement: dict[str, Any],
) -> str:
    observed = markdown_execution_policy(feature_id, text, required=True)
    if observed != expected:
        raise RecoveryError(
            f"feature specification execution policy changed for {feature_id}"
        )
    import re

    normalized, count = re.subn(
        _POLICY_SECTION,
        render_markdown_execution_policy(replacement),
        text,
        count=1,
    )
    if count != 1:
        raise RecoveryError(
            f"feature specification execution-policy section is ambiguous for {feature_id}"
        )
    return normalized.rstrip() + "\n"


@dataclass(frozen=True)
class ReadyFeaturePolicyRebind:
    project: Project
    feature_id: str
    expected_branch: str
    expected_head: str
    expected_repository_id: str
    expected_path_fingerprint: str
    expected_old_policy: dict[str, Any]
    new_policy: dict[str, Any]
    target_profile: str
    specification_path: str
    normalization_paths: tuple[str, ...]
    replacements: dict[str, bytes]
    predicted_final_fingerprint: str
    starting_queue_sha256: str
    starting_ledger_sequence: int
    starting_ledger_fingerprint: str
    starting_projection_fingerprint: str
    resolved_profile: dict[str, Any]
    compatibility: dict[str, Any]

    @property
    def plan_fingerprint(self) -> str:
        return fingerprint(
            {
                "project_id": self.project.project_id,
                "feature_id": self.feature_id,
                "branch": self.expected_branch,
                "head": self.expected_head,
                "old_policy": self.expected_old_policy,
                "new_policy": self.new_policy,
                "normalization_paths": self.normalization_paths,
                "predicted_final_fingerprint": self.predicted_final_fingerprint,
                "ledger_sequence": self.starting_ledger_sequence,
                "ledger_fingerprint": self.starting_ledger_fingerprint,
                "projection_fingerprint": self.starting_projection_fingerprint,
            }
        )

    def public_plan(self, *, dry_run: bool) -> dict[str, Any]:
        compatible = self.compatibility.get("compatible") is True
        return {
            "schema_version": 1,
            "command": "rebind-ready-feature-policy",
            "classification": (
                "POLICY_REBIND_READY" if compatible else "POLICY_REBIND_REJECTED"
            ),
            "project_id": self.project.project_id,
            "feature_id": self.feature_id,
            "dry_run": dry_run,
            "apply_allowed": compatible,
            "plan_fingerprint": self.plan_fingerprint,
            "starting_identity": {
                "repository": str(self.project.repository),
                "repository_id": self.expected_repository_id,
                "path_fingerprint": self.expected_path_fingerprint,
                "branch": self.expected_branch,
                "head": self.expected_head,
                "queue_sha256": self.starting_queue_sha256,
                "projection_state": "feature_ready",
                "selected_feature": self.feature_id,
                "ledger_sequence": self.starting_ledger_sequence,
                "ledger_fingerprint": self.starting_ledger_fingerprint,
                "projection_fingerprint": self.starting_projection_fingerprint,
            },
            "old_stored_policy": self.expected_old_policy,
            "new_stored_policy": self.new_policy,
            "resolved_execution_profile": self.resolved_profile,
            "compatibility": self.compatibility,
            "normalization_paths": list(self.normalization_paths),
            "predicted_final_fingerprint": self.predicted_final_fingerprint,
            "metadata_commits_maximum": 1,
            "final_state": "feature_ready",
            "selected_feature": self.feature_id,
            "feature_preparation_will_run": False,
            "feature_execution_will_run": False,
            "feature_factory_would_launch": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "leases_that_would_be_acquired": 0 if dry_run else 1,
            "transactions_that_would_start": 0 if dry_run else 1,
            "application_repository_written": False,
            "controller_state_written": False,
            "expected_validators": list(POLICY_REBIND_VALIDATORS),
        }


class ReadyFeaturePolicyRebinder:
    """Inspect and optionally apply one exact metadata-only policy rebind."""

    def __init__(
        self,
        configuration: Configuration,
        launcher: SessionLauncher,
        engine: CycleEngine,
    ):
        self.configuration = configuration
        self.launcher = launcher
        self.engine = engine
        self.root = configuration.root.resolve()

    def _state(
        self,
        project: Project,
        inspector: RepositoryInspector,
    ) -> tuple[dict[str, Any], EvidenceLedger, ProjectionEngine, dict[str, Any]]:
        identity = inspector.identity()
        state_root = self.configuration.owned_path(
            self.configuration.conveyor["state_directory"]
        ) / "projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection_engine = ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        )
        projection = projection_engine.rebuild(persist_cache=False)
        return identity, ledger, projection_engine, projection

    def _reservation_path(self, identity: dict[str, Any]) -> Path:
        directory = self.configuration.owned_path(
            self.configuration.conveyor["lock_policy"][
                "controller_launch_lock_directory"
            ]
        )
        return directory / f"{identity['path_fingerprint']}.json"

    def inspect(
        self,
        *,
        project: Project,
        feature_id: str,
        expected_branch: str,
        expected_head: str,
        expected_old_policy: dict[str, Any],
        target_profile: str,
        expected_paths: tuple[str, ...],
    ) -> ReadyFeaturePolicyRebind:
        inspector = RepositoryInspector(project.repository)
        identity, ledger, _, projection = self._state(project, inspector)
        cycle_path = inspector.cycle_state_path()
        cycle = load_json(cycle_path) if cycle_path.exists() else {}
        ownership = AutopilotPaths.for_project(
            self.configuration, project.project_id
        ).ownership
        writer = inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"][
                "writer_lock_relative_path"
            ]
        )
        session_fields = (
            "session_id",
            "feature_session_id",
            "integration_session_id",
        )
        checks = {
            "branch": inspector.current_branch == expected_branch,
            "head": inspector.head == expected_head,
            "clean": inspector.is_clean,
            "no_git_operation": not any(inspector.git_operation_state().values()),
            "no_writer_lease": not writer.exists(),
            "no_controller_reservation": not self._reservation_path(identity).exists(),
            "no_autopilot_ownership": not ownership.exists(),
            "no_active_transaction": projection.get("active_transaction") is None,
            "no_session": all(cycle.get(field) is None for field in session_fields),
            "projection_state": projection.get("current_state") == "feature_ready",
            "selected_feature": projection.get("selected_next_feature") == feature_id,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RecoveryError(
                "ready-feature policy rebind preflight failed: " + ", ".join(failed)
            )

        queue_document = load_queue(project.repository / project.queue_location)
        queue = FeatureQueue(
            queue_document,
            project.repository / project.queue_location,
            project.repository,
        )
        feature = queue.feature(feature_id)
        if feature is None or feature.get("status") != "ready":
            raise RecoveryError("policy rebind requires the exact ready queue feature")
        queue_summary = queue.summary(project.active_milestone or "")
        if queue_summary.get("selected_feature") != feature_id:
            raise RecoveryError("policy rebind feature is not the deterministic ready selection")
        old_policy = validate_feature_execution_policy(
            feature.get("execution_policy"),
            path=f"feature {feature_id}.execution_policy",
        ).to_dict()
        normalized_expected = validate_feature_execution_policy(
            expected_old_policy,
            path="expected_old_policy",
        ).to_dict()
        if old_policy != normalized_expected:
            raise RecoveryError("existing execution policy does not match the expected old policy")

        specification_path = feature.get("spec")
        if not isinstance(specification_path, str) or not specification_path:
            raise RecoveryError("ready feature lacks an authoritative specification")
        if not specification_path.startswith("docs/features/"):
            raise RecoveryError("ready-feature policy specification path is unauthorized")
        specification = project.repository / specification_path
        try:
            specification_text = specification.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RecoveryError("ready feature specification is unreadable") from exc
        specification_policy = markdown_execution_policy(
            feature_id, specification_text, required=True
        )
        if specification_policy != old_policy:
            raise RecoveryError("queue and specification execution policies disagree")

        new_policy = FeatureExecutionPolicy(
            profile=target_profile,
            parent_sessions=1,
            child_sessions=0,
        ).to_dict()
        if new_policy == old_policy:
            raise RecoveryError(
                "target execution policy must differ from the expected old policy"
            )
        validate_feature_execution_policy(
            new_policy,
            path="target_execution_policy",
        )
        candidate = dict(feature)
        candidate["execution_policy"] = new_policy
        resolved = resolve_execution_profile(
            workflow="application_feature",
            deterministic=False,
            feature=candidate,
            project_id=project.project_id,
            configuration=self.configuration.execution_profiles,
        )
        if (
            resolved.profile != target_profile
            or resolved.parent_sessions != 1
            or resolved.child_sessions != 0
            or resolved.resolution_source != "selected_feature_profile"
        ):
            raise RecoveryError("target execution profile did not resolve canonically")
        compatibility = self.launcher.compatibility(
            "feature_cycle",
            project_id=project.project_id,
            planned_model=resolved.model,
            planned_reasoning=resolved.reasoning,
            model_plan_source=resolved.resolution_source,
        ).as_dict()

        raw_feature = next(
            item
            for item in queue_document["features"]
            if item.get("id") == feature_id
        )
        raw_feature["execution_policy"] = new_policy
        normalized_paths = tuple(sorted((project.queue_location, specification_path)))
        if (
            len(set(expected_paths)) != 2
            or tuple(sorted(expected_paths)) != normalized_paths
        ):
            raise RecoveryError(
                "policy rebind expected paths do not match the exact authenticated metadata paths"
            )
        replacements = {
            project.queue_location: _json_bytes(queue_document),
            specification_path: _replace_specification_policy(
                feature_id,
                specification_text,
                expected=old_policy,
                replacement=new_policy,
            ).encode("utf-8"),
        }
        predicted = _planning_diff_fingerprint_with_replacements(
            inspector, replacements
        )
        return ReadyFeaturePolicyRebind(
            project=project,
            feature_id=feature_id,
            expected_branch=expected_branch,
            expected_head=expected_head,
            expected_repository_id=identity["repository_id"],
            expected_path_fingerprint=identity["path_fingerprint"],
            expected_old_policy=old_policy,
            new_policy=new_policy,
            target_profile=target_profile,
            specification_path=specification_path,
            normalization_paths=normalized_paths,
            replacements=replacements,
            predicted_final_fingerprint=predicted,
            starting_queue_sha256=_sha256(
                (project.repository / project.queue_location).read_bytes()
            ),
            starting_ledger_sequence=int(projection["ledger_sequence"]),
            starting_ledger_fingerprint=str(projection["ledger_fingerprint"]),
            starting_projection_fingerprint=str(
                projection["projection_fingerprint"]
            ),
            resolved_profile=resolved.to_dict(),
            compatibility=compatibility,
        )

    def apply(
        self,
        request: ReadyFeaturePolicyRebind,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if request.compatibility.get("compatible") is not True:
            raise RecoveryError(
                "target execution profile is not locally compatible; refusing policy rebind"
            )
        refreshed = self.inspect(
            project=request.project,
            feature_id=request.feature_id,
            expected_branch=request.expected_branch,
            expected_head=request.expected_head,
            expected_old_policy=request.expected_old_policy,
            target_profile=request.target_profile,
            expected_paths=request.normalization_paths,
        )
        if refreshed.plan_fingerprint != request.plan_fingerprint:
            raise RecoveryError("policy rebind plan changed before apply")

        project = request.project
        inspector = RepositoryInspector(project.repository)
        identity, ledger, projection_engine, projection = self._state(
            project, inspector
        )
        if (
            projection["ledger_sequence"] != request.starting_ledger_sequence
            or projection["ledger_fingerprint"]
            != request.starting_ledger_fingerprint
            or projection["projection_fingerprint"]
            != request.starting_projection_fingerprint
        ):
            raise RecoveryError("controller evidence changed before policy rebind")
        run_id = run_id or f"policy-rebind-{uuid.uuid4()}"
        lease = WorkflowWriterLease(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            )
        )
        adapter = QueueReconciliationAdapter(
            allowed_paths=request.normalization_paths,
            allow_untracked=False,
            denied_paths=(
                ".factory/conveyor-state.json",
                ".factory/locks/writer.json",
            ),
            denied_prefixes=("Sources", "Tests", "src", "tests", ".factory"),
            commit_subject=f"factory: rebind {request.feature_id} execution policy",
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection_engine,
            lease=lease,
        )
        reservation = DurableLock(self._reservation_path(identity))
        reservation.acquire(
            make_lock_record(
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                run_id=run_id,
                current_feature=request.feature_id,
                current_phase="ready_feature_policy_rebind",
            )
        )
        originals = {
            path: (project.repository / path).read_bytes()
            for path in request.normalization_paths
        }
        commit_created = False
        try:
            transaction = kernel.begin(
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                milestone=project.active_milestone or "",
                feature_id=request.feature_id,
                run_id=run_id,
                policy=adapter.policy,
                expected_starting_branch=request.expected_branch,
                expected_starting_head=request.expected_head,
                start_evidence={
                    "command": "rebind-ready-feature-policy",
                    "policy_rebind_plan_fingerprint": request.plan_fingerprint,
                    "old_execution_policy": request.expected_old_policy,
                    "new_execution_policy": request.new_policy,
                    "model_sessions_planned": 0,
                    "child_sessions_planned": 0,
                },
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            for relative in request.normalization_paths:
                atomic_write_bytes(
                    project.repository / relative,
                    request.replacements[relative],
                )
            if (
                inspector.planning_diff_fingerprint()
                != request.predicted_final_fingerprint
            ):
                raise RecoveryError(
                    "policy rebind produced an unexpected final fingerprint"
                )
            queue = FeatureQueue.from_location(
                project.repository, project.queue_location
            )
            rebound = queue.feature(request.feature_id) or {}
            validate_feature_execution_policy(
                rebound.get("execution_policy"),
                path=f"feature {request.feature_id}.execution_policy",
            )
            specification_policy = markdown_execution_policy(
                request.feature_id,
                (project.repository / request.specification_path).read_text(
                    encoding="utf-8"
                ),
                required=True,
            )
            if specification_policy != request.new_policy:
                raise RecoveryError(
                    "rebound queue and specification policies disagree"
                )
            compatibility = self.launcher.compatibility(
                "feature_cycle",
                project_id=project.project_id,
                planned_model=request.resolved_profile["model"],
                planned_reasoning=request.resolved_profile["reasoning"],
                model_plan_source=request.resolved_profile["resolution_source"],
            ).as_dict()
            if compatibility.get("compatible") is not True:
                raise RecoveryError(
                    "target execution policy failed compatibility revalidation"
                )
            inventory = _inventory_validation(project)
            diff_check = _diff_check(project)
            if inventory.get("exit_code") != 0 or diff_check.get("exit_code") != 0:
                raise RecoveryError("policy rebind deterministic validation failed")
            kernel.finalize_deterministic_planning(
                changed_paths=request.normalization_paths,
                plan_fingerprint=request.plan_fingerprint,
                validation_evidence={
                    "commands": [
                        [str(inventory.get("validator"))],
                        ["git", "diff", "--check"],
                    ],
                    "warnings": inventory.get("nonfatal_warnings", []),
                    "checks": {
                        "execution_policy_schema": True,
                        "model_compatibility": True,
                        "feature_inventory": True,
                        "git_diff_check": True,
                    },
                },
                selected_feature=request.feature_id,
                execution_mode="ready_feature_policy_rebind",
            )
            commit = kernel.finalize()
            commit_created = True
            terminal = kernel.complete(
                classification="RECONCILED_READY_WORK",
                evidence={
                    "planning_status": "passed",
                    "selected_feature": request.feature_id,
                    "policy_rebind_plan_fingerprint": request.plan_fingerprint,
                    "old_execution_policy": request.expected_old_policy,
                    "new_execution_policy": request.new_policy,
                    "resolved_execution_profile": request.resolved_profile,
                    "feature_preparation_started": False,
                    "feature_execution_started": False,
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                },
            )
            cycle_path = inspector.cycle_state_path()
            cycle = load_json(cycle_path)
            cycle.update(
                {
                    "conveyor_run_id": run_id,
                    "current_feature": request.feature_id,
                    "selected_feature": request.feature_id,
                    "queue_fingerprint": _sha256(
                        (project.repository / project.queue_location).read_bytes()
                    ),
                    "feature_starting_commit": commit,
                    "milestone_pre_integration_commit": commit,
                    "writer_lock_identity": None,
                    "last_successful_checkpoint": "ready_feature_policy_rebound",
                    "last_verified_git_state": {
                        "branch": inspector.current_branch,
                        "head": inspector.head,
                        "clean": inspector.is_clean,
                        "git_operations": inspector.git_operation_state(),
                    },
                    "preflight_compatibility": compatibility,
                    "stop_reason": (
                        "execution policy rebound; feature preparation and execution did not start"
                    ),
                    "human_decision_required": None,
                    "session_id": None,
                    "feature_session_id": None,
                    "integration_session_id": None,
                    "updated_at": utc_now(),
                    **cycle_cache_semantics_from_projection(
                        terminal["projection"]
                    ),
                }
            )
            cycle_cache = write_terminal_cycle_cache(
                cycle_path,
                cycle,
                ledger=ledger,
                projection_engine=projection_engine,
                transaction_id=transaction.transaction_id,
                expected_feature=request.feature_id,
            )
            execution_plan = self.engine._execution_plan(
                project, terminal["projection"]
            )
            _, compatibility_cache_written = (
                self.engine._reconcile_projection_compatibility_cache(
                    project, terminal["projection"], execution_plan
                )
            )
            return {
                **request.public_plan(dry_run=False),
                "classification": "POLICY_REBOUND",
                "outcome": "policy_rebound",
                "run_id": run_id,
                "transaction_id": transaction.transaction_id,
                "planning_result_commit": commit,
                "planning_result_parent": request.expected_head,
                "changed_paths": list(request.normalization_paths),
                "final_state": terminal["projection"]["current_state"],
                "selected_feature": terminal["projection"][
                    "selected_next_feature"
                ],
                "projection_fingerprint": terminal["projection"][
                    "projection_fingerprint"
                ],
                "cycle_cache_fingerprint": cycle_cache[
                    "kernel_cache_fingerprint"
                ],
                "compatibility_cache_written": compatibility_cache_written,
                "application_repository_written": True,
                "controller_state_written": True,
                "repository_clean": inspector.is_clean,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
            }
        except Exception as exc:
            if not commit_created and inspector.head == request.expected_head:
                for relative, value in originals.items():
                    atomic_write_bytes(project.repository / relative, value)
                inspector.stage_planning_paths(
                    list(request.normalization_paths),
                    commit_subject=f"factory: rebind {request.feature_id} execution policy",
                )
            if (
                kernel.transaction is not None
                and kernel.transaction.current_state
                not in {
                    TransactionState.COMPLETED,
                    TransactionState.BLOCKED,
                    TransactionState.HUMAN_DECISION_REQUIRED,
                    TransactionState.RETRYABLE_FAILURE,
                    TransactionState.TERMINAL_FAILURE,
                    TransactionState.SUPERSEDED,
                }
            ):
                kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="PLANNING_VALIDATION_FAILED",
                    next_state="feature_ready",
                )
            if isinstance(exc, (ConveyorError, OSError, ValueError)):
                raise
            raise RecoveryError(f"ready-feature policy rebind failed: {exc}") from exc
        finally:
            record = lease.read()
            if record is not None and kernel.transaction is not None:
                try:
                    lease.release(
                        transaction_id=kernel.transaction.transaction_id,
                        workflow_type=kernel.transaction.workflow_type,
                        repository_identity=kernel.transaction.repository_identity,
                        project_id=kernel.transaction.project_id,
                    )
                except Exception:
                    pass
            reservation.release(run_id)
