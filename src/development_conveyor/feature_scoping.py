"""Bounded, planning-only feature specification transactions."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .command_authority import CommandAuthority
from .config import Configuration
from .contracts import SessionResultEnvelope, TransactionState, WorkflowType
from .cycle_cache import (
    cycle_cache_semantics_from_projection,
    write_terminal_cycle_cache,
)
from .errors import ConveyorError, QueueError, RecoveryError, SessionError
from .execution_profiles import (
    markdown_execution_policy,
    validate_feature_execution_policy,
)
from .kernel import QueueReconciliationAdapter, RecoveryAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, make_lock_record
from .logging import atomic_write_json, utc_now
from .planning import persist_planning_transaction, stable_fingerprint
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .sessions import SessionRequest
from .workflow_lease import WorkflowWriterLease


SCOPING_MODEL = "gpt-5.6-sol"
SCOPING_REASONING = "medium"
SCOPING_PARENT_SESSIONS = 1
SCOPING_CHILD_SESSIONS = 0
SCOPING_METADATA_PATHS = (
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
)
SCOPING_VALIDATORS = (
    "structured session-result validation",
    "exact changed-path validation",
    "stable feature-ID validation",
    "new-feature conflict validation",
    "queue schema and milestone validation",
    "dependency existence and cycle validation",
    "single-ready-feature validation",
    "execution-profile validation",
    "feature specification identity validation",
    "git diff --check",
)
_FEATURE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_HEADING = re.compile(
    r"^#{1,6}[ \t]+(?P<id>[A-Za-z][A-Za-z0-9._-]{0,63})"
    r"[ \t]*(?:[—–:-]+)[ \t]*(?P<title>[^\r\n]*?[^ \t\r\n])[ \t]*\r?$",
    re.MULTILINE,
)
_DEPENDENCY_LINE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:dependencies|depends[_ -]?on)\s*:\s*(?P<ids>.*)$"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    items = tuple(values)
    if not items:
        raise QueueError(f"scope-features requires at least one {label}")
    if any(not isinstance(item, str) or not _FEATURE_ID.fullmatch(item) for item in items):
        raise QueueError(f"scope-features {label} contains an invalid feature ID")
    if len(items) != len(set(items)):
        raise QueueError(f"scope-features {label} contains duplicate feature IDs")
    return items


def _slug(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not value:
        raise QueueError("new feature title cannot produce a safe specification slug")
    return value


def _brief_sections(text: str) -> dict[str, tuple[str, str]]:
    matches = list(_HEADING.finditer(text))
    sections: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        feature_id = match.group("id")
        if feature_id in sections:
            raise QueueError(f"brief contains duplicate feature heading {feature_id}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[feature_id] = (match.group("title").strip(), text[match.end():end])
    return sections


def _brief_policy(feature_id: str, body: str) -> dict[str, Any]:
    try:
        policy = markdown_execution_policy(feature_id, body, required=True)
    except QueueError as exc:
        raise QueueError(
            str(exc).replace("feature contract", "brief")
        ) from exc
    if policy is None:  # pragma: no cover - required=True is exhaustive.
        raise QueueError(
            f"brief must contain exactly one fenced-YAML execution_policy block for {feature_id}"
        )
    return policy


def _brief_dependencies(body: str) -> tuple[str, ...] | None:
    match = _DEPENDENCY_LINE.search(body)
    if match is None:
        return None
    raw = match.group("ids").strip()
    if raw.lower() in {"", "none", "[]"}:
        return ()
    items = tuple(item for item in re.split(r"[\s,]+", raw.strip("[] ")) if item)
    if any(not _FEATURE_ID.fullmatch(item) for item in items) or len(items) != len(set(items)):
        raise QueueError("brief contains malformed or duplicate dependencies")
    return items


def _assert_acyclic(queue: FeatureQueue) -> None:
    dependencies = {
        str(feature["id"]): tuple(str(item) for item in feature.get("dependencies", []))
        for feature in queue.features
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(feature_id: str, path: tuple[str, ...]) -> None:
        if feature_id in visiting:
            raise QueueError("feature dependency cycle detected: " + " -> ".join((*path, feature_id)))
        if feature_id in visited:
            return
        visiting.add(feature_id)
        for dependency in dependencies[feature_id]:
            visit(dependency, (*path, feature_id))
        visiting.remove(feature_id)
        visited.add(feature_id)

    for feature_id in dependencies:
        visit(feature_id, ())


def _spec_identity(path: Path, feature_id: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QueueError(f"feature specification is unavailable: {path.name}") from exc
    heading = re.search(r"(?m)^#{1,6}\s+" + re.escape(feature_id) + r"(?:\s|$)", text)
    if heading is None:
        raise QueueError(f"feature specification does not preserve stable ID {feature_id}: {path.name}")


@dataclass(frozen=True)
class ScopeFeatureRequest:
    project: Project
    existing_features: tuple[str, ...]
    new_features: tuple[str, ...]
    ready_feature: str
    brief_path: Path
    brief_sha256: str
    starting_branch: str
    starting_head: str
    starting_queue_sha256: str
    target_specs: dict[str, str]
    execution_policies: dict[str, dict[str, Any]]
    dependency_expectations: dict[str, tuple[str, ...] | None]
    baseline_features: tuple[dict[str, Any], ...]
    authorized_paths: tuple[str, ...]

    @property
    def target_features(self) -> tuple[str, ...]:
        return (*self.existing_features, *self.new_features)

    def public_plan(self, *, dry_run: bool) -> dict[str, Any]:
        dependency_changes = {
            feature_id: list(dependencies)
            for feature_id, dependencies in self.dependency_expectations.items()
            if dependencies is not None
        }
        return {
            "schema_version": 1,
            "command": "scope-features",
            "project_id": self.project.project_id,
            "dry_run": dry_run,
            "planning_only": True,
            "target_features": list(self.target_features),
            "existing_features": list(self.existing_features),
            "new_features": list(self.new_features),
            "ready_feature": self.ready_feature,
            "authorized_paths": list(self.authorized_paths),
            "brief": {
                "path": str(self.brief_path),
                "sha256": self.brief_sha256,
            },
            "starting_identity": {
                "branch": self.starting_branch,
                "head": self.starting_head,
                "queue_sha256": self.starting_queue_sha256,
            },
            "execution": {
                "model": SCOPING_MODEL,
                "reasoning": SCOPING_REASONING,
                "parent_sessions": SCOPING_PARENT_SESSIONS,
                "child_sessions": SCOPING_CHILD_SESSIONS,
            },
            "feature_execution_policies": self.execution_policies,
            "dependency_changes": dependency_changes,
            "expected_validators": list(SCOPING_VALIDATORS),
            "application_repository_written": False,
            "controller_state_written": False,
            "model_sessions_that_would_launch": 1,
            "feature_execution_will_start": False,
            "feature_factory_would_launch": False,
            "milestone_integrator_would_launch": False,
            "stop_after": "feature_ready",
        }


class FeatureScoper:
    """Plan and apply one exact feature-scoping transaction."""

    def __init__(self, configuration: Configuration, launcher: Any):
        self.configuration = configuration
        self.root = configuration.root.resolve()
        self.launcher = launcher

    def inspect(
        self,
        *,
        project: Project,
        existing_features: Iterable[str],
        new_features: Iterable[str],
        ready_feature: str,
        brief_path: Path,
    ) -> ScopeFeatureRequest:
        existing = _unique(existing_features, "--feature")
        new = tuple(new_features)
        if new:
            new = _unique(new, "--new-feature")
        if set(existing) & set(new):
            raise QueueError("an ID cannot be both an existing and a new feature")
        if not _FEATURE_ID.fullmatch(ready_feature):
            raise QueueError("--ready must name one safe feature ID")
        targets = (*existing, *new)
        if ready_feature not in targets:
            raise QueueError("--ready must name exactly one requested feature")

        repository = project.repository.resolve()
        inspector = RepositoryInspector(repository)
        queue_path = repository / project.queue_location
        queue_bytes = queue_path.read_bytes()
        queue = FeatureQueue.from_location(repository, project.queue_location)
        _assert_acyclic(queue)
        active_milestone = queue.milestone(project.active_milestone or "")
        if active_milestone is None:
            raise QueueError("configured active milestone is missing or malformed")

        for feature_id in existing:
            feature = queue.feature(feature_id)
            if feature is None:
                raise QueueError(f"requested existing feature is absent: {feature_id}")
            if feature.get("milestone") != active_milestone["id"]:
                raise QueueError(f"requested feature is outside the active milestone: {feature_id}")
        for feature_id in new:
            if queue.feature(feature_id) is not None:
                raise QueueError(f"new feature conflicts with an existing stable ID: {feature_id}")

        brief = brief_path.expanduser().resolve()
        if not brief.is_file():
            raise QueueError(f"brief is not a readable Markdown file: {brief}")
        brief_bytes = brief.read_bytes()
        try:
            brief_text = brief_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise QueueError("brief must be UTF-8 Markdown") from exc
        if not brief_text.strip():
            raise QueueError("brief cannot be empty")
        sections = _brief_sections(brief_text)

        target_specs: dict[str, str] = {}
        policies: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, tuple[str, ...] | None] = {}
        for feature_id in targets:
            if feature_id not in sections:
                raise QueueError(f"brief lacks a feature heading for {feature_id}")
            title, body = sections[feature_id]
            policies[feature_id] = _brief_policy(feature_id, body)
            dependencies[feature_id] = _brief_dependencies(body)
            if feature_id in existing:
                feature = queue.feature(feature_id) or {}
                spec = feature.get("spec") or feature.get("specification") or feature.get("spec_path")
                if not isinstance(spec, str) or not spec:
                    raise QueueError(f"existing feature lacks a specification path: {feature_id}")
                path = Path(spec)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.parts[:2] != ("docs", "features")
                    or feature_id not in path.name
                ):
                    raise QueueError(f"existing feature has an unsafe or ambiguous specification path: {feature_id}")
                _spec_identity(repository / path, feature_id)
                target_specs[feature_id] = path.as_posix()
            else:
                target_specs[feature_id] = f"docs/features/{feature_id}-{_slug(title)}.md"

        authorized = tuple(sorted({*target_specs.values(), *SCOPING_METADATA_PATHS}))
        if any(
            path.startswith(("src/", "tests/", ".factory/"))
            or (path.startswith("docs/features/") and path not in target_specs.values())
            for path in authorized
        ):
            raise QueueError("scope-features authorized paths escaped the planning-only boundary")
        return ScopeFeatureRequest(
            project=project,
            existing_features=existing,
            new_features=new,
            ready_feature=ready_feature,
            brief_path=brief,
            brief_sha256=_sha256_bytes(brief_bytes),
            starting_branch=inspector.current_branch,
            starting_head=inspector.head,
            starting_queue_sha256=_sha256_bytes(queue_bytes),
            target_specs=target_specs,
            execution_policies=policies,
            dependency_expectations=dependencies,
            baseline_features=tuple(json.loads(json.dumps(item)) for item in queue.features),
            authorized_paths=authorized,
        )

    @staticmethod
    def _validate_result(request: ScopeFeatureRequest, changed_paths: tuple[str, ...]) -> dict[str, Any]:
        repository = request.project.repository
        queue = FeatureQueue.from_location(repository, request.project.queue_location)
        _assert_acyclic(queue)
        baseline = {str(item["id"]): item for item in request.baseline_features}
        after = {str(item["id"]): item for item in queue.features}
        expected_ids = [*baseline, *request.new_features]
        if len(after) != len(expected_ids) or set(after) != set(expected_ids):
            raise QueueError("feature IDs were removed, replaced, duplicated, or added outside scope")
        for feature_id, before in baseline.items():
            if feature_id not in request.target_features and after[feature_id] != before:
                raise QueueError(f"unrequested feature metadata changed: {feature_id}")

        ready = [
            str(item["id"])
            for item in queue.features_for_milestone(request.project.active_milestone or "")
            if item.get("status") == "ready"
        ]
        if ready != [request.ready_feature]:
            raise QueueError("scope-features must leave exactly the explicitly selected feature ready")
        for feature_id in request.target_features:
            feature = after[feature_id]
            expected_status = "ready" if feature_id == request.ready_feature else "proposed"
            if feature.get("status") != expected_status:
                raise QueueError(f"scoped feature has unexpected status: {feature_id}")
            if feature_id == request.ready_feature and feature.get("implementation_status") != "Ready":
                raise QueueError("ready feature must record implementation_status: Ready")
            expected_policy = request.execution_policies[feature_id]
            if feature.get("execution_policy") != expected_policy:
                raise QueueError(f"scoped feature execution policy disagrees with the brief: {feature_id}")
            validate_feature_execution_policy(
                feature["execution_policy"], path=f"$.features.{feature_id}.execution_policy"
            )
            expected_dependencies = request.dependency_expectations[feature_id]
            actual_dependencies = tuple(feature.get("dependencies") or ())
            if feature_id in baseline:
                preserved = tuple(baseline[feature_id].get("dependencies") or ())
                if not set(preserved).issubset(actual_dependencies):
                    raise QueueError(f"existing valid dependencies were removed: {feature_id}")
            if expected_dependencies is not None and actual_dependencies != expected_dependencies:
                raise QueueError(f"scoped feature dependencies disagree with the brief: {feature_id}")
            expected_spec = request.target_specs[feature_id]
            if feature.get("spec") != expected_spec:
                raise QueueError(f"scoped feature specification path changed unexpectedly: {feature_id}")
            criteria = feature.get("acceptance_criteria")
            if not isinstance(criteria, list) or not criteria:
                raise QueueError(f"scoped feature lacks acceptance criteria: {feature_id}")
            _spec_identity(repository / expected_spec, feature_id)

        changed = set(changed_paths)
        unauthorized = sorted(changed - set(request.authorized_paths))
        if unauthorized:
            raise QueueError("scope-features changed unauthorized paths: " + ", ".join(unauthorized))
        for feature_id in request.new_features:
            if request.target_specs[feature_id] not in changed:
                raise QueueError(f"new feature specification was not created exactly once: {feature_id}")
        result = subprocess.run(
            ["git", "diff", "--check"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise QueueError("scope-features git diff --check failed")
        return {
            "ready_feature": request.ready_feature,
            "target_features": list(request.target_features),
            "new_features": list(request.new_features),
            "changed_paths": list(changed_paths),
        }

    def apply(self, request: ScopeFeatureRequest, *, run_id: str | None = None) -> dict[str, Any]:
        project = request.project
        repository = project.repository.resolve()
        inspector = RepositoryInspector(repository)
        if not inspector.is_clean:
            raise RecoveryError("scope-features apply requires a clean repository")
        if inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ).exists():
            raise RecoveryError("scope-features apply requires no active writer lease")
        if project.milestone_branch and inspector.current_branch != project.milestone_branch:
            raise RecoveryError("scope-features apply requires the configured milestone branch")
        if (
            inspector.current_branch != request.starting_branch
            or inspector.head != request.starting_head
            or _sha256_bytes((repository / project.queue_location).read_bytes())
            != request.starting_queue_sha256
        ):
            raise RecoveryError("scope-features starting branch, HEAD, or queue identity changed")

        run_id = run_id or f"scope-{uuid.uuid4()}"
        identity = inspector.identity()
        state_root = self.configuration.owned_path(
            self.configuration.conveyor["state_directory"]
        ) / "projects" / project.project_id
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        ) / run_id
        report_path = report_root / "scope-features.json"
        transaction_path = report_root / "planning-transaction.json"
        ledger = EvidenceLedger(
            state_root / "evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        adapter = QueueReconciliationAdapter(
            allowed_paths=request.authorized_paths,
            allow_untracked=True,
            denied_paths=(".factory/conveyor-state.json", ".factory/locks/writer.json"),
            denied_prefixes=("src", "tests", ".factory"),
            commit_subject=(
                f"factory: scope {', '.join(request.target_features)} "
                f"and ready {request.ready_feature}"
            ),
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
            current_feature=request.ready_feature,
            current_phase="scope_features",
        ))
        planning_record: dict[str, Any] = {
            **request.public_plan(dry_run=False),
            "run_id": run_id,
            "status": "planning_transaction_started",
            "recovery_contract": {
                "existing_features": list(request.existing_features),
                "new_features": list(request.new_features),
                "ready_feature": request.ready_feature,
                "brief_path": str(request.brief_path),
                "brief_sha256": request.brief_sha256,
                "starting_branch": request.starting_branch,
                "starting_head": request.starting_head,
                "starting_queue_sha256": request.starting_queue_sha256,
                "target_specs": request.target_specs,
                "execution_policies": request.execution_policies,
                "dependency_expectations": {
                    key: list(value) if value is not None else None
                    for key, value in request.dependency_expectations.items()
                },
                "baseline_features": list(request.baseline_features),
                "authorized_paths": list(request.authorized_paths),
            },
            "created_at": utc_now(),
        }
        try:
            if projection.rebuild(persist_cache=False).get("active_transaction") is not None:
                raise RecoveryError("scope-features apply refuses an active controller transaction")
            transaction = kernel.begin(
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                milestone=project.active_milestone or "",
                feature_id=None,
                run_id=run_id,
                policy=adapter.policy,
                expected_starting_branch=request.starting_branch,
                expected_starting_head=request.starting_head,
            )
            planning_record["transaction_id"] = transaction.transaction_id
            kernel.acquire_lease()
            inspector.ensure_runtime_ignored()
            kernel.capture_snapshot()
            kernel.checkpoint("bounded_feature_scoping_reserved", {
                "target_features": list(request.target_features),
                "ready_feature": request.ready_feature,
                "brief_sha256": request.brief_sha256,
                "model": SCOPING_MODEL,
                "reasoning": SCOPING_REASONING,
                "parent_sessions": SCOPING_PARENT_SESSIONS,
                "child_sessions": SCOPING_CHILD_SESSIONS,
            })
            persist_planning_transaction(transaction_path, {
                **planning_record,
                "transaction_id": transaction.transaction_id,
                "planning_start_commit": request.starting_head,
            })
            result = self.launcher.launch(
                SessionRequest(
                    action="scope_features",
                    project=project,
                    run_id=run_id,
                    mode="apply",
                    transaction_id=transaction.transaction_id,
                    repository_identity=transaction.repository_identity,
                    starting_branch=transaction.starting_branch,
                    starting_commit=transaction.starting_head,
                    allowed_paths=request.authorized_paths,
                    parent_session_budget=SCOPING_PARENT_SESSIONS,
                    child_session_budget=SCOPING_CHILD_SESSIONS,
                    planned_model=SCOPING_MODEL,
                    planned_reasoning=SCOPING_REASONING,
                    model_plan_source="explicit_scope_features_policy",
                    selected_profile="generic_or_architectural",
                    context_files=(
                        *(request.target_specs[item] for item in request.existing_features),
                    ),
                    embedded_context=(
                        "# Scope manifest\n\n```json\n"
                        + json.dumps(
                            {
                                "existing_features": list(request.existing_features),
                                "new_features": list(request.new_features),
                                "ready_feature": request.ready_feature,
                                "target_specs": request.target_specs,
                                "execution_policies": request.execution_policies,
                                "dependency_expectations": {
                                    key: list(value) if value is not None else None
                                    for key, value in request.dependency_expectations.items()
                                },
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n```\n\n# Product brief\n\n"
                        + request.brief_path.read_text(encoding="utf-8")
                    ),
                ),
                on_session_started=kernel.session_launched,
            )
            session_report = {
                "schema_version": 1,
                "project_id": project.project_id,
                "run_id": run_id,
                "action": "scope_features",
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
                raise SessionError("scope-features session lacked an exact typed terminal result")
            envelope = SessionResultEnvelope.from_dict(result.transaction_envelope)
            if (
                envelope.workflow_type != WorkflowType.QUEUE_RECONCILIATION
                or envelope.classification != "RECONCILED_READY_WORK"
                or envelope.next_state != "feature_ready"
                or envelope.feature_id is not None
            ):
                raise SessionError("scope-features terminal result violated the planning-only contract")
            kernel.accept_result(envelope)
            kernel.record_file_mutation_boundary()
            changed_paths = tuple(sorted(
                set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())
            ))
            validation = self._validate_result(request, changed_paths)
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
                    "selected_feature": request.ready_feature,
                    "planning_result_commit": commit,
                    "scope_features": validation,
                    "feature_execution_started": False,
                },
            )
            queue = FeatureQueue.from_location(repository, project.queue_location)
            selected = queue.feature(request.ready_feature) or {}
            cycle_state = {
                "schema_version": 1,
                "conveyor_run_id": run_id,
                "project_id": project.project_id,
                "repository_identity": identity,
                "repository_path_fingerprint": identity["path_fingerprint"],
                "active_milestone": project.active_milestone,
                "current_feature": request.ready_feature,
                "selected_feature": request.ready_feature,
                "feature_dependencies": list(selected.get("dependencies") or ()),
                "dependency_evidence": {},
                "queue_fingerprint": _sha256_bytes(
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
                "last_successful_checkpoint": "scope_features_committed",
                "last_verified_git_state": {
                    "branch": inspector.current_branch,
                    "head": inspector.head,
                    "clean": inspector.is_clean,
                    "git_operations": inspector.git_operation_state(),
                },
                "stop_reason": "bounded feature scoping completed; feature execution did not start",
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
                expected_feature=request.ready_feature,
            )
            report = {
                **request.public_plan(dry_run=False),
                "run_id": run_id,
                "transaction_id": transaction.transaction_id,
                "session_id": result.session_id,
                "outcome": "reconciled_ready_work",
                "next_state": "feature_ready",
                "planning_result_commit": commit,
                "changed_paths": list(changed_paths),
                "diff_fingerprint": inspector.patch_fingerprint(str(commit)),
                "ledger_sequence": terminal["projection"]["ledger_sequence"],
                "ledger_fingerprint": terminal["projection"]["ledger_fingerprint"],
                "projection_fingerprint": terminal["projection"]["projection_fingerprint"],
                "compatibility_cache_fingerprint": cache["kernel_cache_fingerprint"],
                "application_repository_written": True,
                "controller_state_written": True,
                "model_sessions_launched": 1,
                "child_sessions_launched": 0,
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
            if kernel.transaction is None:
                if isinstance(exc, (ConveyorError, OSError, ValueError)):
                    raise
                raise RecoveryError(f"scope-features apply failed: {exc}") from exc
            changed_paths = sorted(
                set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())
            )
            retained = {
                **planning_record,
                "status": "planning_validation_failed",
                "error": str(exc),
                "changed_paths": changed_paths,
                "diff_fingerprint": inspector.planning_diff_fingerprint(),
                "recover_command": (
                    f"scripts/conveyor recover-scope-features "
                    f"--project {project.project_id} --run-id {run_id} --dry-run"
                ),
                "reconciliation_report": str(report_path),
            }
            persist_planning_transaction(transaction_path, retained)
            if kernel.transaction is not None and kernel.transaction.current_state not in {
                TransactionState.COMPLETED,
                TransactionState.BLOCKED,
                TransactionState.HUMAN_DECISION_REQUIRED,
                TransactionState.RETRYABLE_FAILURE,
                TransactionState.TERMINAL_FAILURE,
                TransactionState.SUPERSEDED,
            }:
                kernel.block(
                    state=TransactionState.TERMINAL_FAILURE,
                    classification="PLANNING_VALIDATION_FAILED",
                    next_state="validation_failed",
                )
            if isinstance(exc, (ConveyorError, OSError, ValueError)):
                raise
            raise RecoveryError(f"scope-features apply failed: {exc}") from exc
        finally:
            reservation.release(run_id)

    def inspect_recovery(self, *, project: Project, run_id: str) -> dict[str, Any]:
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        ) / run_id
        transaction_path = report_root / "planning-transaction.json"
        try:
            record = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError("scope-features recovery record is unavailable") from exc
        if not isinstance(record, dict) or record.get("status") != "planning_validation_failed":
            raise RecoveryError("scope-features recovery requires one failed retained transaction")
        contract = record.get("recovery_contract")
        if not isinstance(contract, dict):
            raise RecoveryError("scope-features recovery contract is missing")
        if record.get("project_id") != project.project_id:
            raise RecoveryError("scope-features recovery belongs to another project")
        brief_path = Path(str(contract.get("brief_path"))).expanduser().resolve()
        try:
            brief_sha256 = _sha256_bytes(brief_path.read_bytes())
        except OSError as exc:
            raise RecoveryError("scope-features recovery brief is unavailable") from exc
        if brief_sha256 != contract.get("brief_sha256"):
            raise RecoveryError("scope-features recovery brief changed")
        request = ScopeFeatureRequest(
            project=project,
            existing_features=tuple(contract["existing_features"]),
            new_features=tuple(contract["new_features"]),
            ready_feature=str(contract["ready_feature"]),
            brief_path=brief_path,
            brief_sha256=brief_sha256,
            starting_branch=str(contract["starting_branch"]),
            starting_head=str(contract["starting_head"]),
            starting_queue_sha256=str(contract["starting_queue_sha256"]),
            target_specs=dict(contract["target_specs"]),
            execution_policies=dict(contract["execution_policies"]),
            dependency_expectations={
                key: tuple(value) if value is not None else None
                for key, value in dict(contract["dependency_expectations"]).items()
            },
            baseline_features=tuple(contract["baseline_features"]),
            authorized_paths=tuple(contract["authorized_paths"]),
        )
        inspector = RepositoryInspector(project.repository)
        changed_paths = tuple(sorted(
            set(inspector.tracked_changed_paths()) | set(inspector.untracked_file_hashes())
        ))
        expected_paths = tuple(sorted(record.get("changed_paths") or ()))
        checks = {
            "branch": inspector.current_branch == request.starting_branch,
            "head": inspector.head == request.starting_head,
            "dirty": bool(changed_paths),
            "changed_paths": changed_paths == expected_paths,
            "diff_fingerprint": (
                inspector.planning_diff_fingerprint() == record.get("diff_fingerprint")
            ),
            "writer_lease_absent": not inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
            ).exists(),
            "authorized_paths": set(changed_paths).issubset(request.authorized_paths),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RecoveryError(
                "scope-features retained diff changed: " + ", ".join(failed)
            )
        validation = self._validate_result(request, changed_paths)
        plan = {
            "schema_version": 1,
            "command": "recover-scope-features",
            "project_id": project.project_id,
            "original_run_id": run_id,
            "original_transaction_id": record.get("transaction_id"),
            "starting_branch": request.starting_branch,
            "starting_head": request.starting_head,
            "changed_paths": list(changed_paths),
            "diff_fingerprint": record["diff_fingerprint"],
            "selected_feature": request.ready_feature,
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
        if not isinstance(request, ScopeFeatureRequest):
            raise RecoveryError("scope-features recovery plan lacks its exact request")
        inspector = RepositoryInspector(request.project.repository)
        identity = inspector.identity()
        recovery_run_id = f"scope-recovery-{uuid.uuid4()}"
        lock_directory = self.configuration.owned_path(
            self.configuration.conveyor["lock_policy"]["controller_launch_lock_directory"]
        )
        reservation = DurableLock(lock_directory / f"{identity['path_fingerprint']}.json")
        reservation.acquire(make_lock_record(
            project_id=request.project.project_id,
            repository_identity=identity["repository_id"],
            run_id=recovery_run_id,
            current_feature=request.ready_feature,
            current_phase="scope_features_recovery",
        ))
        try:
            return self._recover_reserved(
                plan, recovery_run_id=recovery_run_id
            )
        finally:
            reservation.release(recovery_run_id)

    def _recover_reserved(
        self, plan: dict[str, Any], *, recovery_run_id: str
    ) -> dict[str, Any]:
        request = plan.get("_request")
        if not isinstance(request, ScopeFeatureRequest):
            raise RecoveryError("scope-features recovery plan lacks its exact request")
        project = request.project
        inspector = RepositoryInspector(project.repository)
        refreshed = self.inspect_recovery(
            project=project, run_id=str(plan["original_run_id"])
        )
        if refreshed["plan_fingerprint"] != plan["plan_fingerprint"]:
            raise RecoveryError("scope-features recovery evidence changed")
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
        projection = ProjectionEngine(ledger, state_root / "projection-cache.json")
        lease = WorkflowWriterLease(inspector.writer_lock_path(
            self.configuration.conveyor["lock_policy"]["writer_lock_relative_path"]
        ))
        adapter = RecoveryAdapter(
            allowed_paths=tuple(plan["changed_paths"]),
            allow_untracked=True,
            require_clean_start=False,
            commit_subject=(
                f"factory: scope {', '.join(request.target_features)} "
                f"and ready {request.ready_feature}"
            ),
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project, ledger=ledger, projection=projection, lease=lease
        )
        transaction = kernel.begin(
            workflow_type=WorkflowType.RECOVERY,
            milestone=project.active_milestone,
            feature_id=None,
            run_id=recovery_run_id,
            policy=adapter.policy,
            start_evidence={
                "recovered_transaction_id": plan["original_transaction_id"],
                "recovery_classification": "scope_features_finalization_recovery",
                "model_sessions_planned": 0,
                "child_sessions_planned": 0,
            },
            expected_starting_branch=request.starting_branch,
            expected_starting_head=request.starting_head,
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        commit = kernel.finalize_deterministic_planning_recovery(
            original_transaction_id=str(plan["original_transaction_id"]),
            changed_paths=tuple(plan["changed_paths"]),
            expected_diff_fingerprint=str(plan["diff_fingerprint"]),
            plan_fingerprint=str(plan["plan_fingerprint"]),
            validation_evidence={"commands": [], "warnings": []},
            selected_feature=request.ready_feature,
            next_state="feature_ready",
        )
        completed = kernel.complete(
            classification="RECOVERY_APPLIED",
            evidence={
                "planning_status": "passed",
                "selected_feature": request.ready_feature,
                "planning_result_commit": commit,
                "recovered_transaction_id": plan["original_transaction_id"],
                "model_session_launched": False,
                "feature_execution_started": False,
            },
        )
        queue = FeatureQueue.from_location(project.repository, project.queue_location)
        selected = queue.feature(request.ready_feature) or {}
        cycle_state = {
            "schema_version": 1,
            "conveyor_run_id": recovery_run_id,
            "project_id": project.project_id,
            "repository_identity": identity,
            "repository_path_fingerprint": identity["path_fingerprint"],
            "active_milestone": project.active_milestone,
            "current_feature": request.ready_feature,
            "selected_feature": request.ready_feature,
            "feature_dependencies": list(selected.get("dependencies") or ()),
            "dependency_evidence": {},
            "queue_fingerprint": _sha256_bytes(
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
            "last_successful_checkpoint": "scope_features_recovered",
            "last_verified_git_state": {
                "branch": inspector.current_branch,
                "head": inspector.head,
                "clean": inspector.is_clean,
                "git_operations": inspector.git_operation_state(),
            },
            "stop_reason": "bounded feature scoping recovered; feature execution did not start",
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
            cycle_cache_semantics_from_projection(completed["projection"])
        )
        cache = write_terminal_cycle_cache(
            inspector.cycle_state_path(),
            cycle_state,
            ledger=ledger,
            projection_engine=projection,
            transaction_id=transaction.transaction_id,
            expected_feature=request.ready_feature,
        )
        return {
            **{key: value for key, value in plan.items() if key != "_request"},
            "outcome": "planning_recovery_committed",
            "recovery_run_id": recovery_run_id,
            "recovery_transaction_id": transaction.transaction_id,
            "planning_result_commit": commit,
            "ledger_sequence": completed["projection"]["ledger_sequence"],
            "compatibility_cache_fingerprint": cache["kernel_cache_fingerprint"],
            "application_repository_written": True,
            "repository_clean": inspector.is_clean,
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
            "feature_execution_started": False,
        }
