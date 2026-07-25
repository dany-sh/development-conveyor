"""Transactional product-plan reconciliation from an immutable approved brief."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .command_authority import CommandAuthority
from .config import Configuration
from .contracts import SessionResultEnvelope, TransactionState, WorkflowType
from .cycle_cache import cycle_cache_semantics_from_projection, write_terminal_cycle_cache
from .errors import ConveyorError, QueueError, RecoveryError, SessionError
from .feature_scoping import _assert_acyclic
from .kernel import QueueReconciliationAdapter, RecoveryAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, make_lock_record
from .logging import atomic_write_bytes, atomic_write_json, utc_now
from .planning import persist_planning_transaction, stable_fingerprint
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import SessionRequest
from .workflow_lease import WorkflowWriterLease


PRODUCT_PLAN_MODEL = "gpt-5.6-sol"
PRODUCT_PLAN_REASONING = "medium"
PRODUCT_PLAN_READY_FEATURE = "F068"
PRODUCT_PLAN_FILES = tuple(sorted((
    "PRODUCT_VISION.md",
    "ROADMAP.md",
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
    "docs/architecture.md",
    "docs/data-flow.md",
)))
PRODUCT_PLAN_PREFIXES = tuple(sorted(("docs/adr", "docs/decisions", "docs/features")))
PRODUCT_PLAN_DENIED_PREFIXES = tuple(sorted((
    ".build", ".factory", "App", "Package.swift", "Sources", "Tests",
    "build", "fixtures", "scripts", "src", "tests",
)))
PRODUCT_PLAN_VALIDATORS = (
    "queue/config schema validation",
    "feature-ID preservation",
    "dependency-cycle validation",
    "planning-only forbidden-path audit",
    "F097 integrated-evidence preservation",
    "Test Call and simulation compatibility policy",
    "application/round ownership sequencing",
    "Practice, Live, question, transcript, review, scoring, and export semantics",
    "catalog/queue/roadmap/status/specification consistency",
    "single dependency-ready F068 selection",
    "git diff --check",
)
REQUIRED_FEATURES = frozenset({
    "F011", "F019", "F020", "F021", "F022", "F023", "F027", "F029",
    "F041", "F042", "F043", "F044", "F045", "F050", "F051",
    "F057", "F058", "F059", "F060", "F061", "F062", "F063", "F064",
    "F065", "F066", "F068", "F070", "F072", "F073", "F078", "F097",
})
SEMANTIC_FEATURE_TERMS: dict[str, tuple[str, ...]] = {
    "F019": ("application", "round", "question"),
    "F020": ("guided", "ai interviewer"),
    "F021": ("question", "practice", "live"),
    "F029": ("suggested answer", "your answer", "feedback", "improved answer"),
    "F041": ("application", "round", "audio"),
    "F044": ("speaker", "practice", "live", "import"),
    "F058": ("transcript", "highlight", "comment"),
    "F060": ("export transcript",),
    "F068": ("application", "round", "session"),
    "F073": ("application", "round", "session"),
    "F078": ("application", "round", "session"),
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_brief(value: bytes) -> str:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QueueError("approved brief must be UTF-8 Markdown") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise QueueError("approved brief cannot be empty")
    return normalized


def brief_identity(path: Path) -> tuple[Path, bytes, dict[str, Any]]:
    """Capture a regular, non-linked immutable input identity."""

    lexical = Path(os.path.abspath(path.expanduser()))
    try:
        before = os.lstat(lexical)
    except OSError as exc:
        raise QueueError(f"approved brief is unavailable: {lexical}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise QueueError("approved brief must be a regular non-symlink file")
    if before.st_nlink != 1:
        raise QueueError("approved brief must have exactly one hard link")
    payload = lexical.read_bytes()
    after = os.lstat(lexical)
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise QueueError("approved brief changed while its identity was captured")
    normalized = _normalized_brief(payload)
    return lexical, payload, {
        "absolute_path": str(lexical),
        "device": before.st_dev,
        "inode": before.st_ino,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": _sha256(payload),
        "normalized_sha256": _sha256(normalized.encode("utf-8")),
    }


def planning_path_allowed(path: str) -> bool:
    return path in PRODUCT_PLAN_FILES or any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in PRODUCT_PLAN_PREFIXES
    )


def _spec_text(repository: Path, feature: dict[str, Any]) -> str:
    relative = feature.get("spec") or feature.get("specification") or feature.get("spec_path")
    if not isinstance(relative, str) or not relative:
        raise QueueError(f"feature lacks a specification path: {feature.get('id')}")
    candidate = (repository / relative).resolve()
    try:
        candidate.relative_to(repository.resolve())
    except ValueError as exc:
        raise QueueError(f"feature specification escapes the repository: {feature.get('id')}") from exc
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise QueueError(f"feature specification is unavailable: {feature.get('id')}") from exc


def validate_compatibility_language(
    repository: Path, changed_paths: tuple[str, ...]
) -> dict[str, int]:
    """Reject future product requirements while allowing explicit compatibility prose."""

    test_call_violations: list[str] = []
    simulation_violations: list[str] = []
    for relative in changed_paths:
        if not relative.endswith((".md", ".yaml", ".yml")):
            continue
        text = (repository / relative).read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()
            if "test call" in lowered and not any(
                marker in lowered
                for marker in ("remove", "removed", "no ", "not ", "legacy", "compatib", "prohibit")
            ):
                test_call_violations.append(f"{relative}:{line_number}")
            if any(marker in lowered for marker in (".simulation", "simulation", "simulator")) and not any(
                marker in lowered
                for marker in ("legacy", "compatib", "decode", "fixture", "synthetic", "no ", "not ", "remove")
            ):
                simulation_violations.append(f"{relative}:{line_number}")
    if test_call_violations:
        raise QueueError(
            "future user-facing Test Call requirement remains: " + ", ".join(test_call_violations)
        )
    if simulation_violations:
        raise QueueError(
            "simulation is not compatibility-only: " + ", ".join(simulation_violations)
        )
    return {
        "test_call_user_facing_requirements": 0,
        "simulation_noncompatibility_requirements": 0,
    }


@dataclass(frozen=True)
class ProductPlanRequest:
    project: Project
    brief_path: Path
    brief_bytes: bytes
    brief_identity: dict[str, Any]
    repository_identity: dict[str, Any]
    starting_branch: str
    starting_head: str
    starting_queue_sha256: str
    ledger_sequence: int
    ledger_fingerprint: str
    projection_fingerprint: str
    baseline_features: tuple[dict[str, Any], ...]
    baseline_f097: dict[str, Any]

    def public_plan(self, *, dry_run: bool) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": "reconcile-product-plan",
            "project_id": self.project.project_id,
            "dry_run": dry_run,
            "planning_only": True,
            "brief": dict(self.brief_identity),
            "starting_identity": {
                "repository_identity": self.repository_identity,
                "branch": self.starting_branch,
                "head": self.starting_head,
                "queue_sha256": self.starting_queue_sha256,
                "ledger_sequence": self.ledger_sequence,
                "ledger_fingerprint": self.ledger_fingerprint,
                "projection_fingerprint": self.projection_fingerprint,
            },
            "stable_topology": {
                "current_state": "queue_reconciliation",
                "current_feature": None,
                "active_transaction": None,
                "human_gate": None,
                "allowed_next_action": "queue_reconciliation",
                "f097_status": "integrated",
                "f097_integration_status": "passed",
            },
            "apply_execution_policy": {
                "model": PRODUCT_PLAN_MODEL,
                "reasoning": PRODUCT_PLAN_REASONING,
                "parent_sessions": 1,
                "child_sessions": 0,
                "planning_only": True,
            },
            "allowed_application_path_categories": [
                *PRODUCT_PLAN_FILES,
                *(prefix + "/**" for prefix in PRODUCT_PLAN_PREFIXES),
            ],
            "forbidden_application_path_categories": [
                "production source",
                "tests and fixtures",
                "build-system files",
                "runtime caches and .factory state",
                "controller evidence/state edited by the model",
            ],
            "expected_validators": list(PRODUCT_PLAN_VALIDATORS),
            "intended_transaction": {
                "workflow_type": "queue_reconciliation",
                "lease_type": "planning_writer",
                "state_path": ["queue_reconciliation", "feature_ready"],
                "expected_ready_features": [PRODUCT_PLAN_READY_FEATURE],
                "one_planning_commit": True,
            },
            "model_sessions_that_would_launch": 0 if dry_run else 1,
            "maximum_parent_sessions_on_apply": 1,
            "child_sessions_that_would_launch": 0,
            "application_repository_written": False,
            "controller_state_written": False,
            "application_source_will_run": False,
            "application_tests_will_run": False,
            "integration_will_run": False,
            "feature_implementation_will_run": False,
            "feature_execution_will_start": False,
            "stop_after": "feature_ready",
        }


class ProductPlanReconciler:
    """Inspect and apply one approved planning-only product reconciliation."""

    def __init__(self, configuration: Configuration, launcher: Any):
        self.configuration = configuration
        self.launcher = launcher

    def _kernel_state(
        self, project: Project, identity: dict[str, Any]
    ) -> tuple[EvidenceLedger, ProjectionEngine, dict[str, Any]]:
        state_root = self.configuration.owned_path(
            self.configuration.conveyor["state_directory"]
        ) / "projects" / project.project_id
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        return ledger, projection, projection.rebuild(persist_cache=False)

    @staticmethod
    def _assert_topology(
        project: Project, inspector: RepositoryInspector, projection: dict[str, Any]
    ) -> tuple[FeatureQueue, dict[str, Any]]:
        checks = {
            "milestone_branch": inspector.current_branch == project.milestone_branch,
            "clean": inspector.is_clean,
            "no_git_operation": not any(inspector.git_operation_state().values()),
            "state": projection.get("current_state") == "queue_reconciliation",
            "feature": projection.get("current_feature") is None,
            "transaction": projection.get("active_transaction") is None,
            "human_gate": projection.get("human_gate") is None,
            "next_action": projection.get("allowed_next_action") == "queue_reconciliation",
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RecoveryError(
                "product-plan topology is not stable post-F097: " + ", ".join(failed)
            )
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        _assert_acyclic(queue)
        f097 = queue.feature("F097")
        milestone = queue.milestone(str((f097 or {}).get("milestone") or ""))
        if (
            not isinstance(f097, dict)
            or f097.get("status") != "integrated"
            or f097.get("integration_status") != "passed"
            or not isinstance(f097.get("accepted_commit"), str)
            or not isinstance(f097.get("integrated_commit"), str)
            or not isinstance(milestone, dict)
            or "F097" not in (milestone.get("integrated_features") or [])
        ):
            raise RecoveryError("F097 is not durably integrated in the stable topology")
        return queue, f097

    def inspect(self, *, project: Project, brief_path: Path) -> ProductPlanRequest:
        repository = project.repository.resolve()
        inspector = RepositoryInspector(repository)
        identity = inspector.identity()
        ledger, _, projection = self._kernel_state(project, identity)
        queue, f097 = self._assert_topology(project, inspector, projection)
        brief, brief_bytes, captured_brief = brief_identity(brief_path)
        return ProductPlanRequest(
            project=project,
            brief_path=brief,
            brief_bytes=brief_bytes,
            brief_identity=captured_brief,
            repository_identity=identity,
            starting_branch=inspector.current_branch,
            starting_head=inspector.head,
            starting_queue_sha256=_sha256((repository / project.queue_location).read_bytes()),
            ledger_sequence=ledger.verify().sequence,
            ledger_fingerprint=ledger.verify().fingerprint,
            projection_fingerprint=str(projection["projection_fingerprint"]),
            baseline_features=tuple(json.loads(json.dumps(item)) for item in queue.features),
            baseline_f097=json.loads(json.dumps(f097)),
        )

    def _revalidate(
        self, request: ProductPlanRequest, *, leased_transaction_id: str | None = None
    ) -> dict[str, Any]:
        project = request.project
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        if identity != request.repository_identity:
            raise RecoveryError("application repository identity changed")
        if inspector.current_branch != request.starting_branch or inspector.head != request.starting_head:
            raise RecoveryError("application branch or HEAD changed")
        if not inspector.is_clean or any(inspector.git_operation_state().values()):
            raise RecoveryError("application repository is no longer a clean stable topology")
        if _sha256((project.repository / project.queue_location).read_bytes()) != request.starting_queue_sha256:
            raise RecoveryError("application queue identity changed")
        _, payload, captured_brief = brief_identity(request.brief_path)
        if payload != request.brief_bytes or captured_brief != request.brief_identity:
            raise RecoveryError("approved brief identity changed")
        ledger, _, projection = self._kernel_state(project, identity)
        if leased_transaction_id is None:
            if (
                projection.get("ledger_sequence") != request.ledger_sequence
                or projection.get("ledger_fingerprint") != request.ledger_fingerprint
                or projection.get("projection_fingerprint") != request.projection_fingerprint
            ):
                raise RecoveryError("ledger or projection identity changed")
            self._assert_topology(project, inspector, projection)
        else:
            events = ledger.read()
            if len(events) < request.ledger_sequence:
                raise RecoveryError("ledger prefix disappeared after lease acquisition")
            boundary = events[request.ledger_sequence - 1] if request.ledger_sequence else None
            if request.ledger_sequence and (
                boundary.get("sequence") != request.ledger_sequence
                or boundary.get("fingerprint") != request.ledger_fingerprint
            ):
                raise RecoveryError("ledger prefix identity changed after lease acquisition")
            if projection.get("active_transaction") != leased_transaction_id:
                raise RecoveryError("leased planning transaction is not projection-authoritative")
        return projection

    @staticmethod
    def validate_result(
        request: ProductPlanRequest, changed_paths: tuple[str, ...]
    ) -> dict[str, Any]:
        repository = request.project.repository
        unauthorized = [path for path in changed_paths if not planning_path_allowed(path)]
        if unauthorized:
            raise QueueError(
                "product-plan reconciliation changed forbidden paths: " + ", ".join(unauthorized)
            )
        if not changed_paths:
            raise QueueError("product-plan reconciliation produced no planning changes")
        queue = FeatureQueue.from_location(repository, request.project.queue_location)
        _assert_acyclic(queue)
        try:
            adapter = json.loads(
                (
                    repository / request.project.validation_source
                ).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise QueueError(
                "application factory configuration is not valid JSON-compatible YAML"
            ) from exc
        if (
            not isinstance(adapter, dict)
            or adapter.get("schema_version") != 1
            or not isinstance(adapter.get("project"), dict)
            or not isinstance(adapter.get("project_profile"), dict)
            or not isinstance(adapter.get("content_policy"), dict)
            or (adapter.get("factory") or {}).get("feature_queue")
            != request.project.queue_location
        ):
            raise QueueError(
                "application factory configuration disagrees with the registered project"
            )
        before = {str(item["id"]): item for item in request.baseline_features}
        after = {str(item["id"]): item for item in queue.features}
        if list(before) != list(after) or set(before) != set(after):
            raise QueueError("feature IDs were removed, renumbered, reordered, duplicated, or added")
        if after.get("F097") != request.baseline_f097:
            raise QueueError("F097 was reopened, broadened, or lost accepted/integrated evidence")
        missing = sorted(REQUIRED_FEATURES - set(after))
        if missing:
            raise QueueError("approved brief features are missing: " + ", ".join(missing))

        ready = [
            str(item["id"]) for item in queue.features
            if item.get("status") == "ready" and queue.dependencies_complete(item)
        ]
        if ready != [PRODUCT_PLAN_READY_FEATURE]:
            raise QueueError("revised queue must select F068 as the sole dependency-ready feature")
        for consumer in ("F019", "F041"):
            if "F078" not in set(after[consumer].get("dependencies") or ()):
                raise QueueError(f"{consumer} must depend on required application/round ownership")
        if (
            "F078" not in set(after["F097"].get("dependencies") or ())
            and "F097" not in set(after["F078"].get("dependencies") or ())
        ):
            raise QueueError("imported sessions lack an explicit application/round ownership dependency")

        semantic_checks: dict[str, list[str]] = {}
        for feature_id, terms in SEMANTIC_FEATURE_TERMS.items():
            lowered = _spec_text(repository, after[feature_id]).lower()
            absent = [term for term in terms if term not in lowered]
            if absent:
                raise QueueError(
                    f"{feature_id} specification disagrees with the approved brief: "
                    + ", ".join(absent)
                )
            semantic_checks[feature_id] = list(terms)
        scoring_text = "\n".join(
            _spec_text(repository, after[feature_id]).lower()
            for feature_id in ("F061", "F062", "F063", "F064", "F065", "F066")
        )
        for term in ("score", "feedback", "improved answer", "retry"):
            if term not in scoring_text:
                raise QueueError(f"scoring/coaching specifications omit required concept: {term}")

        policy = validate_compatibility_language(repository, changed_paths)
        catalog = (repository / "docs/FEATURE_CATALOG.md").read_text(encoding="utf-8")
        missing_catalog = sorted(feature_id for feature_id in after if feature_id not in catalog)
        if missing_catalog:
            raise QueueError("feature catalog omits stable IDs: " + ", ".join(missing_catalog))
        for relative in (
            "PRODUCT_VISION.md", "ROADMAP.md", "docs/ROADMAP.md", "docs/CURRENT_STATUS.md"
        ):
            lowered = (repository / relative).read_text(encoding="utf-8").lower()
            if "practice" not in lowered or "live" not in lowered or "application" not in lowered:
                raise QueueError(f"planning surface disagrees with the approved direction: {relative}")
        run_log = (repository / "docs/RUN_LOG.md").read_text(encoding="utf-8").lower()
        for marker in ("product-plan", PRODUCT_PLAN_MODEL, PRODUCT_PLAN_REASONING):
            if marker not in run_log:
                raise QueueError(f"run log lacks planning execution evidence: {marker}")
        diff = subprocess.run(
            ["git", "diff", "--check"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if diff.returncode != 0:
            raise QueueError("product-plan reconciliation git diff --check failed")
        return {
            "feature_ids_preserved": len(after),
            "f097_integrated_evidence_preserved": True,
            "ready_features": ready,
            "ownership_milestones": {
                feature_id: str(after[feature_id].get("milestone"))
                for feature_id in ("F068", "F070", "F073", "F078")
            },
            "semantic_feature_checks": semantic_checks,
            "policy_checks": policy,
            "dependency_graph_acyclic": True,
            "application_factory_configuration": "valid",
            "changed_paths": list(changed_paths),
            "git_diff_check": "passed",
        }

    def apply(
        self, request: ProductPlanRequest, *, run_id: str | None = None
    ) -> dict[str, Any]:
        project = request.project
        repository = project.repository.resolve()
        inspector = RepositoryInspector(repository)
        self._revalidate(request)
        writer_path = inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        )
        if writer_path.exists():
            raise RecoveryError("product-plan apply requires no active writer lease")

        run_id = run_id or f"product-plan-{uuid.uuid4()}"
        identity = inspector.identity()
        state_root = self.configuration.owned_path(
            self.configuration.conveyor["state_directory"]
        ) / "projects" / project.project_id
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        ) / run_id
        report_path = report_root / "product-plan-reconciliation.json"
        transaction_path = report_root / "planning-transaction.json"
        brief_copy = report_root / "approved-brief.md"
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        lease = WorkflowWriterLease(writer_path)
        adapter = QueueReconciliationAdapter(
            allowed_paths=PRODUCT_PLAN_FILES,
            allowed_prefixes=PRODUCT_PLAN_PREFIXES,
            allow_untracked=True,
            denied_paths=(".factory/conveyor-state.json", ".factory/locks/writer.json"),
            denied_prefixes=PRODUCT_PLAN_DENIED_PREFIXES,
            commit_subject="factory: reconcile approved product plan after F097",
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project, ledger=ledger, projection=projection, lease=lease
        )
        lock_directory = self.configuration.owned_path(
            self.configuration.conveyor["lock_policy"]["controller_launch_lock_directory"]
        )
        reservation = DurableLock(lock_directory / f"{identity['path_fingerprint']}.json")
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            run_id=run_id,
            current_feature=None,
            current_phase="product_plan_reconciliation",
        ))
        planning_record: dict[str, Any] = {
            **request.public_plan(dry_run=False),
            "run_id": run_id,
            "status": "planning_transaction_started",
            "brief_evidence_copy": str(brief_copy),
            "recovery_contract": {
                "brief": request.brief_identity,
                "starting_branch": request.starting_branch,
                "starting_head": request.starting_head,
                "starting_queue_sha256": request.starting_queue_sha256,
                "repository_identity": request.repository_identity,
                "ledger_sequence": request.ledger_sequence,
                "ledger_fingerprint": request.ledger_fingerprint,
                "projection_fingerprint": request.projection_fingerprint,
                "baseline_feature_ids": [
                    item["id"] for item in request.baseline_features
                ],
                "baseline_features": list(request.baseline_features),
                "baseline_f097": request.baseline_f097,
                "authorized_files": list(PRODUCT_PLAN_FILES),
                "authorized_prefixes": list(PRODUCT_PLAN_PREFIXES),
            },
            "created_at": utc_now(),
        }
        try:
            transaction = kernel.begin(
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                milestone=project.active_milestone or "",
                feature_id=None,
                run_id=run_id,
                policy=adapter.policy,
                start_evidence={
                    "approved_brief": request.brief_identity,
                    "pinned_ledger_sequence": request.ledger_sequence,
                    "pinned_ledger_fingerprint": request.ledger_fingerprint,
                    "pinned_projection_fingerprint": request.projection_fingerprint,
                    "model_sessions_planned": 1,
                    "child_sessions_planned": 0,
                    "planning_only": True,
                },
                expected_starting_branch=request.starting_branch,
                expected_starting_head=request.starting_head,
            )
            planning_record["transaction_id"] = transaction.transaction_id
            kernel.acquire_lease()
            self._revalidate(
                request, leased_transaction_id=transaction.transaction_id
            )
            inspector.ensure_runtime_ignored()
            kernel.capture_snapshot()
            atomic_write_bytes(brief_copy, request.brief_bytes)
            kernel.checkpoint("approved_product_plan_reserved", {
                "brief": request.brief_identity,
                "brief_evidence_copy": str(brief_copy),
                "model": PRODUCT_PLAN_MODEL,
                "reasoning": PRODUCT_PLAN_REASONING,
                "parent_sessions": 1,
                "child_sessions": 0,
            })
            persist_planning_transaction(transaction_path, {
                **planning_record,
                "planning_start_commit": request.starting_head,
            })
            context_files = tuple(
                path for path in PRODUCT_PLAN_FILES
                if (repository / path).is_file()
            )
            result = self.launcher.launch(
                SessionRequest(
                    action="reconcile_product_plan",
                    project=project,
                    run_id=run_id,
                    mode="apply",
                    transaction_id=transaction.transaction_id,
                    repository_identity=transaction.repository_identity,
                    starting_branch=transaction.starting_branch,
                    starting_commit=transaction.starting_head,
                    allowed_paths=(
                        *PRODUCT_PLAN_FILES,
                        *(prefix + "/**" for prefix in PRODUCT_PLAN_PREFIXES),
                    ),
                    parent_session_budget=1,
                    child_session_budget=0,
                    planned_model=PRODUCT_PLAN_MODEL,
                    planned_reasoning=PRODUCT_PLAN_REASONING,
                    model_plan_source="approved_product_plan_policy",
                    context_files=context_files,
                    embedded_context=(
                        "# Immutable brief identity\n\n```json\n"
                        + json.dumps(
                            request.brief_identity, indent=2, sort_keys=True
                        )
                        + "\n```\n\n# Approved brief\n\n"
                        + _normalized_brief(request.brief_bytes)
                    ),
                ),
                on_session_started=kernel.session_launched,
            )
            session_report = {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": run_id,
                "action": "reconcile_product_plan",
                "working_directory": str(repository),
                "exit_status": result.returncode,
                "structured_output_validation": result.structured_output_validation,
                "result_classification": result.result_classification,
                "structured_result": result.transaction_envelope,
                "parsed_structured_result": result.transaction_envelope,
                "terminal_marker_found": result.terminal_marker_found,
                "redacted_stdout": result.redacted_stdout,
                "redacted_stderr": result.redacted_stderr,
                "session_id": result.session_id,
            }
            atomic_write_json(report_path, session_report)
            planning_record["session_id"] = result.session_id
            if not result.session_id or result.transaction_envelope is None:
                raise SessionError(
                    "product-plan session lacked an exact typed terminal result"
                )
            envelope = SessionResultEnvelope.from_dict(
                result.transaction_envelope
            )
            if (
                envelope.workflow_type != WorkflowType.QUEUE_RECONCILIATION
                or envelope.classification != "RECONCILED_READY_WORK"
                or envelope.next_state != "feature_ready"
                or envelope.feature_id is not None
            ):
                raise SessionError(
                    "product-plan terminal result violated the planning-only contract"
                )
            kernel.accept_result(envelope)
            kernel.record_file_mutation_boundary()
            changed_paths = tuple(sorted(
                set(inspector.tracked_changed_paths())
                | set(inspector.untracked_file_hashes())
            ))
            validation = self.validate_result(request, changed_paths)
            kernel.validate(
                authority=CommandAuthority(),
                command_results=(),
                semantic_validator=adapter.semantic_validate,
            )
            commit = kernel.finalize()
            terminal = kernel.complete(
                classification="RECONCILED_READY_WORK",
                evidence={
                    "planning_status": "passed",
                    "selected_feature": PRODUCT_PLAN_READY_FEATURE,
                    "planning_result_commit": commit,
                    "product_plan_reconciliation": validation,
                    "approved_brief": request.brief_identity,
                    "feature_execution_started": False,
                },
            )
            queue = FeatureQueue.from_location(
                repository, project.queue_location
            )
            selected = queue.feature(PRODUCT_PLAN_READY_FEATURE) or {}
            cycle_state = {
                "schema_version": 1,
                "conveyor_run_id": run_id,
                "project_id": project.project_id,
                "repository_identity": identity,
                "repository_path_fingerprint": identity["path_fingerprint"],
                "active_milestone": project.active_milestone,
                "current_feature": PRODUCT_PLAN_READY_FEATURE,
                "selected_feature": PRODUCT_PLAN_READY_FEATURE,
                "feature_dependencies": list(selected.get("dependencies") or ()),
                "dependency_evidence": {},
                "queue_fingerprint": _sha256(
                    (repository / project.queue_location).read_bytes()
                ),
                "feature_branch": None,
                "feature_worktree": str(repository),
                "feature_starting_commit": commit,
                "accepted_feature_commit": None,
                "milestone_branch": project.milestone_branch,
                "milestone_pre_integration_commit": commit,
                "milestone_post_integration_commit": None,
                "current_phase": "feature_ready",
                "writer_lock_identity": None,
                "validation_attempts": [],
                "review_attempts": [],
                "integration_attempts": [],
                "last_successful_checkpoint":
                    "product_plan_reconciliation_committed",
                "last_verified_git_state": {
                    "branch": inspector.current_branch,
                    "head": inspector.head,
                    "clean": inspector.is_clean,
                    "git_operations": inspector.git_operation_state(),
                },
                "stop_reason":
                    "approved product plan reconciled; feature execution did not start",
                "human_decision_required": None,
                "resume_instructions": (
                    f"scripts/conveyor run --project {project.project_id} "
                    f"--mode {project.automation_mode}"
                ),
                "session_id": result.session_id,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            cycle_state.update(
                cycle_cache_semantics_from_projection(terminal["projection"])
            )
            cache = write_terminal_cycle_cache(
                inspector.cycle_state_path(),
                cycle_state,
                ledger=ledger,
                projection_engine=projection,
                transaction_id=transaction.transaction_id,
                expected_feature=PRODUCT_PLAN_READY_FEATURE,
            )
            report = {
                **request.public_plan(dry_run=False),
                "run_id": run_id,
                "transaction_id": transaction.transaction_id,
                "session_id": result.session_id,
                "outcome": "reconciled_ready_work",
                "next_state": "feature_ready",
                "selected_feature": PRODUCT_PLAN_READY_FEATURE,
                "planning_result_commit": commit,
                "changed_paths": list(changed_paths),
                "diff_fingerprint": inspector.patch_fingerprint(commit),
                "ledger_sequence": terminal["projection"]["ledger_sequence"],
                "ledger_fingerprint":
                    terminal["projection"]["ledger_fingerprint"],
                "projection_fingerprint":
                    terminal["projection"]["projection_fingerprint"],
                "compatibility_cache_fingerprint":
                    cache["kernel_cache_fingerprint"],
                "brief_evidence_copy": str(brief_copy),
                "application_repository_written": True,
                "controller_state_written": True,
                "model_sessions_launched": 1,
                "child_sessions_launched": 0,
                "application_builds_run": 0,
                "application_tests_run": 0,
                "integration_started": False,
                "feature_execution_started": False,
                "repository_clean": inspector.is_clean,
            }
            atomic_write_json(report_path, report)
            persist_planning_transaction(transaction_path, {
                **planning_record,
                **report,
                "status": "planning_changes_committed",
                "reconciliation_report": str(report_path),
            })
            return report
        except Exception as exc:
            changed_paths = sorted(
                set(inspector.tracked_changed_paths())
                | set(inspector.untracked_file_hashes())
            )
            diff_fingerprint = inspector.planning_diff_fingerprint()
            retained = {
                **planning_record,
                "status": "planning_validation_failed",
                "error": str(exc),
                "changed_paths": changed_paths,
                "diff_fingerprint": diff_fingerprint,
                "changed_file_sha256": {
                    path: _sha256((repository / path).read_bytes())
                    for path in changed_paths if (repository / path).is_file()
                },
                "recover_command": (
                    "scripts/conveyor recover-product-plan "
                    f"--project {project.project_id} --run-id {run_id} --dry-run"
                ),
                "reconciliation_report": str(report_path),
            }
            persist_planning_transaction(transaction_path, retained)
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
                    next_state="validation_failed",
                )
            if isinstance(exc, (ConveyorError, OSError, ValueError)):
                raise
            raise RecoveryError(f"product-plan apply failed: {exc}") from exc
        finally:
            reservation.release(run_id)

    def inspect_recovery(
        self, *, project: Project, run_id: str
    ) -> dict[str, Any]:
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        ) / run_id
        transaction_path = report_root / "planning-transaction.json"
        try:
            record = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "product-plan recovery record is unavailable"
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("status") != "planning_validation_failed"
            or record.get("project_id") != project.project_id
        ):
            raise RecoveryError(
                "product-plan recovery requires one failed retained transaction"
            )
        contract = record.get("recovery_contract")
        if not isinstance(contract, dict):
            raise RecoveryError("product-plan recovery contract is missing")
        expected_brief = contract.get("brief")
        if not isinstance(expected_brief, dict):
            raise RecoveryError("product-plan recovery brief identity is missing")
        brief_path = Path(str(expected_brief.get("absolute_path") or ""))
        captured_path, brief_bytes, captured_brief = brief_identity(brief_path)
        if captured_brief != expected_brief:
            raise RecoveryError("product-plan recovery brief changed")
        request = ProductPlanRequest(
            project=project,
            brief_path=captured_path,
            brief_bytes=brief_bytes,
            brief_identity=captured_brief,
            repository_identity=dict(contract["repository_identity"]),
            starting_branch=str(contract["starting_branch"]),
            starting_head=str(contract["starting_head"]),
            starting_queue_sha256=str(contract["starting_queue_sha256"]),
            ledger_sequence=int(contract["ledger_sequence"]),
            ledger_fingerprint=str(contract["ledger_fingerprint"]),
            projection_fingerprint=str(contract["projection_fingerprint"]),
            baseline_features=tuple(contract["baseline_features"]),
            baseline_f097=dict(contract["baseline_f097"]),
        )
        inspector = RepositoryInspector(project.repository)
        changed_paths = tuple(sorted(
            set(inspector.tracked_changed_paths())
            | set(inspector.untracked_file_hashes())
        ))
        expected_paths = tuple(sorted(record.get("changed_paths") or ()))
        expected_hashes = record.get("changed_file_sha256")
        actual_hashes = {
            path: _sha256((project.repository / path).read_bytes())
            for path in changed_paths if (project.repository / path).is_file()
        }
        checks = {
            "repository_identity":
                inspector.identity() == request.repository_identity,
            "branch": inspector.current_branch == request.starting_branch,
            "head": inspector.head == request.starting_head,
            "dirty": bool(changed_paths),
            "changed_paths": changed_paths == expected_paths,
            "diff_fingerprint":
                inspector.planning_diff_fingerprint()
                == record.get("diff_fingerprint"),
            "changed_file_sha256": actual_hashes == expected_hashes,
            "writer_lease_absent": not inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            ).exists(),
            "authorized_paths":
                all(planning_path_allowed(path) for path in changed_paths),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RecoveryError(
                "product-plan retained diff changed: " + ", ".join(failed)
            )
        validation = self.validate_result(request, changed_paths)
        plan = {
            "schema_version": 1,
            "command": "recover-product-plan",
            "project_id": project.project_id,
            "original_run_id": run_id,
            "original_transaction_id": record.get("transaction_id"),
            "original_session_id": record.get("session_id"),
            "starting_branch": request.starting_branch,
            "starting_head": request.starting_head,
            "brief": request.brief_identity,
            "changed_paths": list(changed_paths),
            "changed_file_sha256": actual_hashes,
            "diff_fingerprint": record["diff_fingerprint"],
            "selected_feature": PRODUCT_PLAN_READY_FEATURE,
            "validation": validation,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "feature_execution_will_start": False,
            "application_repository_written": False,
            "deterministic_only": True,
            "checks": checks,
        }
        plan["plan_fingerprint"] = stable_fingerprint(plan)
        plan["_request"] = request
        return plan

    def recover(self, plan: dict[str, Any]) -> dict[str, Any]:
        request = plan.get("_request")
        if not isinstance(request, ProductPlanRequest):
            raise RecoveryError(
                "product-plan recovery plan lacks its exact request"
            )
        project = request.project
        inspector = RepositoryInspector(project.repository)
        identity = inspector.identity()
        recovery_run_id = f"product-plan-recovery-{uuid.uuid4()}"
        lock_directory = self.configuration.owned_path(
            self.configuration.conveyor["lock_policy"][
                "controller_launch_lock_directory"
            ]
        )
        reservation = DurableLock(
            lock_directory / f"{identity['path_fingerprint']}.json"
        )
        reservation.acquire(make_lock_record(
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            run_id=recovery_run_id,
            current_feature=PRODUCT_PLAN_READY_FEATURE,
            current_phase="product_plan_recovery",
        ))
        kernel: WorkflowKernel | None = None
        try:
            refreshed = self.inspect_recovery(
                project=project, run_id=str(plan["original_run_id"])
            )
            if refreshed["plan_fingerprint"] != plan["plan_fingerprint"]:
                raise RecoveryError(
                    "product-plan recovery evidence changed"
                )
            state_root = self.configuration.owned_path(
                self.configuration.conveyor["state_directory"]
            ) / "projects" / project.project_id
            ledger = EvidenceLedger(
                state_root / "evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            projection = ProjectionEngine(
                ledger, state_root / "projection-cache.json"
            )
            lease = WorkflowWriterLease(inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            ))
            adapter = RecoveryAdapter(
                allowed_paths=tuple(plan["changed_paths"]),
                allow_untracked=True,
                require_clean_start=False,
                commit_subject=
                    "factory: reconcile approved product plan after F097",
                next_state="feature_ready",
            )
            kernel = WorkflowKernel(
                project=project,
                ledger=ledger,
                projection=projection,
                lease=lease,
            )
            transaction = kernel.begin(
                workflow_type=WorkflowType.RECOVERY,
                milestone=project.active_milestone,
                feature_id=None,
                run_id=recovery_run_id,
                policy=adapter.policy,
                start_evidence={
                    "recovered_transaction_id":
                        plan["original_transaction_id"],
                    "recovery_classification":
                        "product_plan_finalization_recovery",
                    "approved_brief": request.brief_identity,
                    "model_sessions_planned": 0,
                    "child_sessions_planned": 0,
                },
                expected_starting_branch=request.starting_branch,
                expected_starting_head=request.starting_head,
            )
            kernel.acquire_lease()
            lease.revalidate(
                transaction_id=transaction.transaction_id,
                workflow_type=WorkflowType.RECOVERY,
                repository_identity=transaction.repository_identity,
                project_id=project.project_id,
                starting_branch=request.starting_branch,
                starting_head=request.starting_head,
                run_id=recovery_run_id,
                session_id=None,
                policy=adapter.policy,
            )
            _, _, captured_brief = brief_identity(request.brief_path)
            observed_paths = tuple(sorted(
                set(inspector.tracked_changed_paths())
                | set(inspector.untracked_file_hashes())
            ))
            observed_hashes = {
                path: _sha256((project.repository / path).read_bytes())
                for path in observed_paths
                if (project.repository / path).is_file()
            }
            if (
                inspector.identity() != request.repository_identity
                or inspector.current_branch != request.starting_branch
                or inspector.head != request.starting_head
                or captured_brief != request.brief_identity
                or observed_paths != tuple(plan["changed_paths"])
                or observed_hashes != plan["changed_file_sha256"]
                or inspector.planning_diff_fingerprint()
                != plan["diff_fingerprint"]
            ):
                raise RecoveryError(
                    "product-plan recovery evidence changed after lease"
                )
            kernel.capture_snapshot()
            commit = kernel.finalize_deterministic_planning_recovery(
                original_transaction_id=str(
                    plan["original_transaction_id"]
                ),
                changed_paths=tuple(plan["changed_paths"]),
                expected_diff_fingerprint=str(plan["diff_fingerprint"]),
                plan_fingerprint=str(plan["plan_fingerprint"]),
                validation_evidence={
                    "commands": [
                        {"argv": ["git", "diff", "--check"], "exit_code": 0}
                    ],
                    "warnings": [],
                    "semantic_validation": plan["validation"],
                },
                selected_feature=PRODUCT_PLAN_READY_FEATURE,
                next_state="feature_ready",
            )
            completed = kernel.complete(
                classification="RECOVERY_APPLIED",
                evidence={
                    "planning_status": "passed",
                    "selected_feature": PRODUCT_PLAN_READY_FEATURE,
                    "planning_result_commit": commit,
                    "recovered_transaction_id":
                        plan["original_transaction_id"],
                    "approved_brief": request.brief_identity,
                    "model_session_launched": False,
                    "feature_execution_started": False,
                },
            )
            queue = FeatureQueue.from_location(
                project.repository, project.queue_location
            )
            selected = queue.feature(PRODUCT_PLAN_READY_FEATURE) or {}
            cycle_state = {
                "schema_version": 1,
                "conveyor_run_id": recovery_run_id,
                "project_id": project.project_id,
                "repository_identity": identity,
                "repository_path_fingerprint": identity["path_fingerprint"],
                "active_milestone": project.active_milestone,
                "current_feature": PRODUCT_PLAN_READY_FEATURE,
                "selected_feature": PRODUCT_PLAN_READY_FEATURE,
                "feature_dependencies": list(
                    selected.get("dependencies") or ()
                ),
                "dependency_evidence": {},
                "queue_fingerprint": _sha256(
                    (project.repository / project.queue_location).read_bytes()
                ),
                "feature_branch": None,
                "feature_worktree": str(project.repository),
                "feature_starting_commit": commit,
                "accepted_feature_commit": None,
                "milestone_branch": project.milestone_branch,
                "milestone_pre_integration_commit": commit,
                "milestone_post_integration_commit": None,
                "current_phase": "feature_ready",
                "writer_lock_identity": None,
                "validation_attempts": [],
                "review_attempts": [],
                "integration_attempts": [],
                "last_successful_checkpoint":
                    "product_plan_reconciliation_recovered",
                "last_verified_git_state": {
                    "branch": inspector.current_branch,
                    "head": inspector.head,
                    "clean": inspector.is_clean,
                    "git_operations": inspector.git_operation_state(),
                },
                "stop_reason":
                    "approved product plan recovered; feature execution did not start",
                "human_decision_required": None,
                "resume_instructions": (
                    f"scripts/conveyor run --project {project.project_id} "
                    f"--mode {project.automation_mode}"
                ),
                "session_id": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            cycle_state.update(
                cycle_cache_semantics_from_projection(
                    completed["projection"]
                )
            )
            cache = write_terminal_cycle_cache(
                inspector.cycle_state_path(),
                cycle_state,
                ledger=ledger,
                projection_engine=projection,
                transaction_id=transaction.transaction_id,
                expected_feature=PRODUCT_PLAN_READY_FEATURE,
            )
            result = {
                **{
                    key: value for key, value in plan.items()
                    if key != "_request"
                },
                "outcome": "planning_recovery_committed",
                "next_state": "feature_ready",
                "recovery_run_id": recovery_run_id,
                "recovery_transaction_id": transaction.transaction_id,
                "planning_result_commit": commit,
                "ledger_sequence":
                    completed["projection"]["ledger_sequence"],
                "ledger_fingerprint":
                    completed["projection"]["ledger_fingerprint"],
                "projection_fingerprint":
                    completed["projection"]["projection_fingerprint"],
                "compatibility_cache_fingerprint":
                    cache["kernel_cache_fingerprint"],
                "application_repository_written": True,
                "repository_clean": inspector.is_clean,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "application_tests_run": 0,
                "integration_started": False,
                "feature_execution_started": False,
            }
            original_path = (
                self.configuration.owned_path(
                    self.configuration.conveyor["report_directory"]
                )
                / str(plan["original_run_id"])
                / "planning-transaction.json"
            )
            persist_planning_transaction(original_path, {
                **json.loads(original_path.read_text(encoding="utf-8")),
                **result,
                "status": "planning_recovery_committed",
            })
            return result
        except Exception:
            if (
                kernel is not None
                and kernel.transaction is not None
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
                    classification="TERMINAL_RECOVERY_FAILURE",
                    next_state="validation_failed",
                )
            raise
        finally:
            reservation.release(recovery_run_id)
