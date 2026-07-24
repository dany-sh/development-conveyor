"""Deterministic queue-feature human-decision resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Configuration, load_json
from .contracts import TransactionState, WorkflowType, fingerprint
from .cycle_cache import (
    cycle_cache_semantics_from_projection,
    write_terminal_cycle_cache,
)
from .errors import ConveyorError, QueueError, RecoveryError
from .feature_scoping import _assert_acyclic, _spec_identity
from .kernel import QueueReconciliationAdapter, WorkflowKernel
from .ledger import EvidenceLedger
from .locks import DurableLock, make_lock_record
from .logging import atomic_write_bytes, atomic_write_json, utc_now
from .projection import ProjectionEngine
from .queue import FeatureQueue
from .registry import Project
from .repository import RepositoryInspector
from .workflow_lease import WorkflowWriterLease


DECISION_METADATA_PATHS = (
    "docs/CURRENT_STATUS.md",
    "docs/FEATURE_CATALOG.md",
    "docs/FEATURE_QUEUE.yaml",
    "docs/ROADMAP.md",
    "docs/RUN_LOG.md",
)
DECISION_SPEC_PATHS = (
    "docs/features/F006-permission-and-privacy-center.md",
    "docs/features/F010-diagnostics-and-latency-instrumentation.md",
    "docs/features/F011-automated-test-harness.md",
    "docs/features/F097-imported-audio-transcription-workflow.md",
)
DECISION_AUTHORIZED_PATHS = tuple(
    sorted((*DECISION_METADATA_PATHS, *DECISION_SPEC_PATHS))
)
DECISION_VALIDATORS = (
    "exact project and repository identity",
    "expected branch and HEAD",
    "clean worktree and no unfinished Git operation",
    "no writer lease or controller reservation",
    "feature_ready projection with no active transaction",
    "exact current and desired feature selection",
    "exact recorded feature-question matching",
    "decision-gated feature status",
    "integrated selected-feature dependencies",
    "selected-feature specification and acceptance criteria",
    "previous feature not started or prepared",
    "dependency graph and queue schema",
    "sole-ready-feature invariant",
    "planning-only changed-path boundary",
    "git diff --check",
)
_TOP_LEVEL_KEYS = {
    "schema_version",
    "project_id",
    "repository",
    "expected",
    "desired",
    "features",
    "commit_subject",
}
_FEATURE_KEYS = {
    "feature_id",
    "recorded_question",
    "approved_resolution",
    "target_status",
    "dependency_change",
    "required_integrated_dependencies",
}
_MARKER_BEGIN = "<!-- CONVEYOR_FEATURE_DECISION_RESOLUTION_BEGIN -->"
_MARKER_END = "<!-- CONVEYOR_FEATURE_DECISION_RESOLUTION_END -->"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_exact_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise QueueError(f"{path}: unsupported keys: {', '.join(extra)}")


def _nonempty(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueueError(f"{path}: expected a non-empty string")
    return value.strip()


def _replace_marker(text: str, block: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(_MARKER_BEGIN)}.*?{re.escape(_MARKER_END)}\n?",
        re.DOTALL,
    )
    base = pattern.sub("\n", text).rstrip()
    return f"{base}\n\n{block.rstrip()}\n"


def _replace_factory_status(text: str, status: str, feature_id: str) -> str:
    rendered, count = re.subn(
        r"(?m)^(- Factory status:\s*).+$",
        rf"\1{status}",
        text,
        count=1,
    )
    if count != 1:
        raise QueueError(
            f"feature specification lacks one Factory status line: {feature_id}"
        )
    return rendered


def _status_label(status: str) -> str:
    return {"ready": "Ready", "proposed": "Proposed"}[status]


def _require_current_ledger_head(ledger: EvidenceLedger) -> None:
    """Refuse stale anchors so inspection cannot perform ledger healing."""

    try:
        lines = ledger.path.read_text(encoding="utf-8").splitlines()
        head = json.loads(ledger.head_path.read_text(encoding="utf-8"))
        tail = json.loads(lines[-1])
    except (OSError, IndexError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "feature-decision inspection requires current durable ledger evidence"
        ) from exc
    if (
        not isinstance(head, dict)
        or not isinstance(tail, dict)
        or head.get("sequence") != tail.get("sequence")
        or head.get("fingerprint") != tail.get("fingerprint")
    ):
        raise RecoveryError(
            "feature-decision inspection refuses a stale durable ledger head"
        )


@dataclass(frozen=True)
class FeatureDecision:
    feature_id: str
    recorded_question: str
    approved_resolution: str
    target_status: str
    expected_dependencies: tuple[str, ...] | None
    approved_dependencies: tuple[str, ...] | None
    required_integrated_dependencies: tuple[str, ...]

    def evidence(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "feature_id": self.feature_id,
            "recorded_question": self.recorded_question,
            "approved_resolution": self.approved_resolution,
            "target_status": self.target_status,
        }
        if self.expected_dependencies is not None:
            value["dependency_change"] = {
                "expected": list(self.expected_dependencies),
                "approved": list(self.approved_dependencies or ()),
            }
        if self.required_integrated_dependencies:
            value["required_integrated_dependencies"] = list(
                self.required_integrated_dependencies
            )
        return value


@dataclass(frozen=True)
class FeatureDecisionRequest:
    project: Project
    decision_path: Path
    decision_sha256: str
    project_repository: str
    expected_repository_id: str
    expected_path_fingerprint: str
    expected_branch: str
    expected_head: str
    expected_projection_state: str
    expected_selected_feature: str
    selected_feature: str
    sole_ready_feature: str
    commit_subject: str
    decisions: tuple[FeatureDecision, ...]
    starting_queue_sha256: str
    starting_ledger_sequence: int
    starting_ledger_fingerprint: str
    starting_projection_fingerprint: str
    baseline_features: tuple[dict[str, Any], ...]
    authorized_paths: tuple[str, ...]
    historical_project_gate: dict[str, Any] | None

    @property
    def decision_fingerprint(self) -> str:
        return fingerprint(
            {
                "project_id": self.project.project_id,
                "decision_sha256": self.decision_sha256,
                "expected_branch": self.expected_branch,
                "expected_head": self.expected_head,
                "selected_feature": self.selected_feature,
                "decisions": [item.evidence() for item in self.decisions],
            }
        )

    def public_plan(self, *, dry_run: bool) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": "resolve-feature-decisions",
            "project_id": self.project.project_id,
            "dry_run": dry_run,
            "planning_only": True,
            "decision_file": {
                "path": str(self.decision_path),
                "sha256": self.decision_sha256,
                "fingerprint": self.decision_fingerprint,
            },
            "starting_identity": {
                "repository": self.project_repository,
                "repository_id": self.expected_repository_id,
                "path_fingerprint": self.expected_path_fingerprint,
                "branch": self.expected_branch,
                "head": self.expected_head,
                "queue_sha256": self.starting_queue_sha256,
                "projection_state": self.expected_projection_state,
                "selected_feature": self.expected_selected_feature,
                "ledger_sequence": self.starting_ledger_sequence,
                "ledger_fingerprint": self.starting_ledger_fingerprint,
                "projection_fingerprint": self.starting_projection_fingerprint,
            },
            "feature_decisions": [item.evidence() for item in self.decisions],
            "selected_feature": self.selected_feature,
            "sole_ready_feature": self.sole_ready_feature,
            "authorized_paths": list(self.authorized_paths),
            "expected_validators": list(DECISION_VALIDATORS),
            "historical_project_gate_preserved": (
                self.historical_project_gate is not None
            ),
            "application_repository_written": False,
            "controller_state_written": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
            "feature_execution_will_start": False,
            "feature_factory_would_launch": False,
            "milestone_integrator_would_launch": False,
            "stop_after": "feature_ready",
        }


class FeatureDecisionResolver:
    """Inspect and apply one exact, zero-model queue-decision transaction."""

    def __init__(self, configuration: Configuration):
        self.configuration = configuration
        self.root = configuration.root.resolve()

    def _state(
        self, project: Project, inspector: RepositoryInspector
    ) -> tuple[
        dict[str, Any], EvidenceLedger, ProjectionEngine, dict[str, Any]
    ]:
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
        projection = ProjectionEngine(
            ledger, state_root / "projection-cache.json"
        )
        _require_current_ledger_head(ledger)
        return identity, ledger, projection, projection.rebuild(persist_cache=False)

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
        decision_path: Path,
        selected_feature: str,
    ) -> FeatureDecisionRequest:
        repository = project.repository.resolve()
        inspector = RepositoryInspector(repository)
        decision_file = decision_path.expanduser().resolve()
        try:
            metadata = os.stat(decision_file, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise QueueError(
                    "decision file must be one regular single-link file"
                )
            decision_bytes = decision_file.read_bytes()
            document = json.loads(decision_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueueError("decision file must be readable UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise QueueError("decision file root must be an object")
        _require_exact_keys(document, _TOP_LEVEL_KEYS, "$")
        if document.get("schema_version") != 1:
            raise QueueError("$.schema_version: expected 1")
        if _nonempty(document.get("project_id"), "$.project_id") != project.project_id:
            raise QueueError("decision file project does not match the selected project")

        repository_contract = document.get("repository")
        expected = document.get("expected")
        desired = document.get("desired")
        raw_decisions = document.get("features")
        if not isinstance(repository_contract, dict):
            raise QueueError("$.repository: expected an object")
        if not isinstance(expected, dict) or not isinstance(desired, dict):
            raise QueueError("$.expected and $.desired must be objects")
        if not isinstance(raw_decisions, list) or not raw_decisions:
            raise QueueError("$.features: expected a non-empty array")
        _require_exact_keys(
            repository_contract,
            {"path", "repository_id", "path_fingerprint", "adapter_project_id"},
            "$.repository",
        )
        _require_exact_keys(
            expected,
            {"branch", "head", "projection_state", "selected_feature"},
            "$.expected",
        )
        _require_exact_keys(
            desired,
            {"selected_feature", "sole_ready_feature"},
            "$.desired",
        )

        identity, ledger, _, projection = self._state(project, inspector)
        adapter = load_json(repository / ".factory/project.yaml")
        adapter_project = adapter.get("project") if isinstance(adapter, dict) else None
        if not isinstance(adapter_project, dict) or adapter_project.get(
            "id"
        ) != _nonempty(
            repository_contract.get("adapter_project_id"),
            "$.repository.adapter_project_id",
        ):
            raise RecoveryError(
                "application adapter project identity disagrees with decision file"
            )
        expected_path = str(repository)
        checks = {
            "repository_path": _nonempty(
                repository_contract.get("path"), "$.repository.path"
            )
            == expected_path,
            "repository_id": _nonempty(
                repository_contract.get("repository_id"),
                "$.repository.repository_id",
            )
            == identity["repository_id"],
            "path_fingerprint": _nonempty(
                repository_contract.get("path_fingerprint"),
                "$.repository.path_fingerprint",
            )
            == identity["path_fingerprint"],
            "branch": _nonempty(expected.get("branch"), "$.expected.branch")
            == inspector.current_branch,
            "head": _nonempty(expected.get("head"), "$.expected.head")
            == inspector.head,
            "clean": inspector.is_clean,
            "no_git_operation": not any(inspector.git_operation_state().values()),
            "no_writer_lease": not inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            ).exists(),
            "no_controller_reservation": not self._reservation_path(identity).exists(),
            "no_active_transaction": projection.get("active_transaction") is None,
            "projection_state": _nonempty(
                expected.get("projection_state"), "$.expected.projection_state"
            )
            == "feature_ready"
            == projection.get("current_state"),
            "selected_feature": _nonempty(
                expected.get("selected_feature"), "$.expected.selected_feature"
            )
            == projection.get("selected_next_feature"),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RecoveryError(
                "feature-decision preflight failed: " + ", ".join(failed)
            )

        desired_selected = _nonempty(
            desired.get("selected_feature"), "$.desired.selected_feature"
        )
        sole_ready = _nonempty(
            desired.get("sole_ready_feature"), "$.desired.sole_ready_feature"
        )
        if selected_feature != desired_selected or desired_selected != sole_ready:
            raise QueueError(
                "--select-feature must equal the decision file's sole desired ready feature"
            )

        queue_path = repository / project.queue_location
        queue_bytes = queue_path.read_bytes()
        queue = FeatureQueue.from_location(repository, project.queue_location)
        _assert_acyclic(queue)
        decisions: list[FeatureDecision] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_decisions):
            path = f"$.features[{index}]"
            if not isinstance(raw, dict):
                raise QueueError(f"{path}: expected an object")
            _require_exact_keys(raw, _FEATURE_KEYS, path)
            feature_id = _nonempty(raw.get("feature_id"), f"{path}.feature_id")
            if feature_id in seen:
                raise QueueError(f"{path}: duplicate feature decision")
            seen.add(feature_id)
            feature = queue.feature(feature_id)
            if feature is None:
                raise QueueError(f"decision feature is absent: {feature_id}")
            if (
                feature.get("status") != "human_decision_required"
                or feature.get("requires_human_decision") is not True
            ):
                raise RecoveryError(f"feature is not decision-gated: {feature_id}")
            question = _nonempty(
                raw.get("recorded_question"), f"{path}.recorded_question"
            )
            recorded = feature.get("human_decision")
            if not isinstance(recorded, dict) or recorded.get("question") != question:
                raise RecoveryError(
                    f"recorded decision question does not match exactly: {feature_id}"
                )
            target_status = _nonempty(
                raw.get("target_status"), f"{path}.target_status"
            )
            if target_status not in {"proposed", "ready"}:
                raise QueueError(
                    f"{path}.target_status: expected proposed or ready"
                )
            change = raw.get("dependency_change")
            expected_dependencies = approved_dependencies = None
            if change is not None:
                if not isinstance(change, dict):
                    raise QueueError(
                        f"{path}.dependency_change: expected an object"
                    )
                _require_exact_keys(
                    change,
                    {"expected", "approved"},
                    f"{path}.dependency_change",
                )
                before = change.get("expected")
                after = change.get("approved")
                if (
                    not isinstance(before, list)
                    or not isinstance(after, list)
                    or not all(
                        isinstance(item, str) and item for item in before + after
                    )
                    or len(before) != len(set(before))
                    or len(after) != len(set(after))
                ):
                    raise QueueError(
                        f"{path}.dependency_change: expected unique feature-ID arrays"
                    )
                expected_dependencies = tuple(before)
                approved_dependencies = tuple(after)
                if tuple(feature.get("dependencies") or ()) != expected_dependencies:
                    raise RecoveryError(
                        f"feature dependencies changed before resolution: {feature_id}"
                    )
            required = raw.get("required_integrated_dependencies", [])
            if (
                not isinstance(required, list)
                or not all(isinstance(item, str) and item for item in required)
                or len(required) != len(set(required))
            ):
                raise QueueError(
                    f"{path}.required_integrated_dependencies: expected unique IDs"
                )
            decisions.append(
                FeatureDecision(
                    feature_id=feature_id,
                    recorded_question=question,
                    approved_resolution=_nonempty(
                        raw.get("approved_resolution"),
                        f"{path}.approved_resolution",
                    ),
                    target_status=target_status,
                    expected_dependencies=expected_dependencies,
                    approved_dependencies=approved_dependencies,
                    required_integrated_dependencies=tuple(required),
                )
            )

        selected_decision = next(
            (
                item
                for item in decisions
                if item.feature_id == desired_selected
            ),
            None,
        )
        if selected_decision is None or selected_decision.target_status != "ready":
            raise QueueError(
                "selected feature must have one ready decision resolution"
            )
        selected_queue_feature = queue.feature(desired_selected) or {}
        if tuple(selected_queue_feature.get("dependencies") or ()) != (
            selected_decision.required_integrated_dependencies
        ):
            raise RecoveryError(
                "selected feature integrated-dependency contract does not match its queue"
            )
        if not queue.dependencies_complete(selected_queue_feature):
            raise RecoveryError("selected feature dependencies are not integrated")
        criteria = selected_queue_feature.get("acceptance_criteria")
        specification = selected_queue_feature.get("spec")
        if (
            not isinstance(criteria, list)
            or not criteria
            or not isinstance(specification, str)
        ):
            raise RecoveryError(
                "selected feature lacks specification or acceptance criteria"
            )
        _spec_identity(repository / specification, desired_selected)

        previous_id = str(expected["selected_feature"])
        previous = queue.feature(previous_id) or {}
        lifecycle_events = [
            event
            for event in ledger.read()
            if event["event_type"] == "TransactionStarted"
            and event["payload"].get("feature_id") == previous_id
            and event["workflow_type"]
            in {
                "feature_preparation",
                "feature_execution",
                "feature_acceptance",
                "milestone_integration",
            }
        ]
        if (
            previous.get("status") != "ready"
            or previous.get("branch")
            or previous.get("accepted_commit")
            or previous.get("integrated_commit")
            or previous.get("integration_base_commit")
            or lifecycle_events
        ):
            raise RecoveryError(
                "previous selected feature has started or been prepared"
            )

        return FeatureDecisionRequest(
            project=project,
            decision_path=decision_file,
            decision_sha256=_sha256(decision_bytes),
            project_repository=expected_path,
            expected_repository_id=identity["repository_id"],
            expected_path_fingerprint=identity["path_fingerprint"],
            expected_branch=str(inspector.current_branch),
            expected_head=inspector.head,
            expected_projection_state="feature_ready",
            expected_selected_feature=previous_id,
            selected_feature=desired_selected,
            sole_ready_feature=sole_ready,
            commit_subject=_nonempty(
                document.get("commit_subject"), "$.commit_subject"
            ),
            decisions=tuple(decisions),
            starting_queue_sha256=_sha256(queue_bytes),
            starting_ledger_sequence=int(projection["ledger_sequence"]),
            starting_ledger_fingerprint=str(projection["ledger_fingerprint"]),
            starting_projection_fingerprint=str(
                projection["projection_fingerprint"]
            ),
            baseline_features=tuple(
                json.loads(json.dumps(item)) for item in queue.features
            ),
            authorized_paths=DECISION_AUTHORIZED_PATHS,
            historical_project_gate=(
                dict(project.human_decision_gate)
                if isinstance(project.human_decision_gate, dict)
                else None
            ),
        )

    def _render(self, request: FeatureDecisionRequest) -> dict[str, bytes]:
        repository = request.project.repository
        inspector = RepositoryInspector(repository)
        originals: dict[str, bytes] = {}
        for path in request.authorized_paths:
            payload = inspector.safe_worktree_file_bytes(path)
            assert payload is not None
            originals[path] = payload
        queue_document = json.loads(
            originals["docs/FEATURE_QUEUE.yaml"].decode("utf-8")
        )
        features = {
            str(item["id"]): item for item in queue_document["features"]
        }
        resolutions = {
            item.feature_id: item for item in request.decisions
        }
        for decision in request.decisions:
            feature = features[decision.feature_id]
            before_dependencies = list(feature.get("dependencies") or ())
            if decision.approved_dependencies is not None:
                feature["dependencies"] = list(decision.approved_dependencies)
            feature["status"] = decision.target_status
            feature["requires_human_decision"] = False
            feature["decision_resolution"] = {
                "recorded_question": decision.recorded_question,
                "approved_resolution": decision.approved_resolution,
                "dependency_change": {
                    "before": before_dependencies,
                    "after": list(feature.get("dependencies") or ()),
                },
                "selected_feature": (
                    decision.feature_id == request.selected_feature
                ),
                "decision_file_sha256": request.decision_sha256,
            }
        previous = features[request.expected_selected_feature]
        previous["status"] = "proposed"
        previous["selection_resolution"] = {
            "previous_status": "ready",
            "resulting_status": "proposed",
            "implementation_started": False,
            "preparation_started": False,
            "selected_feature": request.selected_feature,
            "decision_file_sha256": request.decision_sha256,
        }
        rendered: dict[str, bytes] = dict(originals)
        rendered["docs/FEATURE_QUEUE.yaml"] = (
            json.dumps(queue_document, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        lines = [
            _MARKER_BEGIN,
            "## Queue feature decision resolution",
            "",
            f"- Previous selection: {request.expected_selected_feature} "
            "(returned to Proposed; implementation and preparation did not start).",
            f"- Selected ready feature: {request.selected_feature}.",
            f"- Decision evidence: `{request.decision_sha256}`.",
        ]
        for decision in request.decisions:
            dependency = ""
            if decision.approved_dependencies is not None:
                dependency = (
                    " Dependencies: "
                    + ", ".join(decision.approved_dependencies)
                    + "."
                )
            lines.append(
                f"- {decision.feature_id}: "
                f"{_status_label(decision.target_status)}; "
                f"human decision resolved.{dependency}"
            )
        lines.append(_MARKER_END)
        summary_block = "\n".join(lines)
        for path in (
            "docs/CURRENT_STATUS.md",
            "docs/FEATURE_CATALOG.md",
            "docs/ROADMAP.md",
        ):
            rendered[path] = _replace_marker(
                originals[path].decode("utf-8"), summary_block
            ).encode("utf-8")

        for path in DECISION_SPEC_PATHS:
            feature_id = Path(path).name.split("-", 1)[0]
            status = (
                "proposed"
                if feature_id == request.expected_selected_feature
                else resolutions[feature_id].target_status
            )
            text = _replace_factory_status(
                originals[path].decode("utf-8"),
                _status_label(status),
                feature_id,
            )
            if feature_id in resolutions:
                decision = resolutions[feature_id]
                detail = [
                    _MARKER_BEGIN,
                    "## Approved decision resolution",
                    "",
                    f"- Recorded question: {decision.recorded_question}",
                    f"- Approved resolution: {decision.approved_resolution}",
                    f"- Resulting queue status: `{decision.target_status}`.",
                ]
                if decision.approved_dependencies is not None:
                    detail.append(
                        "- Resulting dependencies: "
                        + ", ".join(decision.approved_dependencies)
                        + "."
                    )
                detail.append(_MARKER_END)
            else:
                detail = [
                    _MARKER_BEGIN,
                    "## Selection transition",
                    "",
                    f"{feature_id} returned from Ready to Proposed without "
                    "feature preparation or implementation. "
                    f"{request.selected_feature} is the sole ready feature.",
                    _MARKER_END,
                ]
            rendered[path] = _replace_marker(
                text, "\n".join(detail)
            ).encode("utf-8")

        run_log = originals["docs/RUN_LOG.md"].decode("utf-8").rstrip()
        run_entry = [
            _MARKER_BEGIN,
            "## Deterministic queue-feature decision resolution",
            "",
            f"- Decision file SHA-256: `{request.decision_sha256}`.",
            f"- Previous selection preserved: "
            f"{request.expected_selected_feature}.",
            f"- Sole ready and selected feature: {request.selected_feature}.",
            "- Execution: deterministic controller route; zero model sessions; "
            "zero child sessions; no feature preparation or implementation.",
            "- Historical project-level gate evidence was preserved and was not resolved.",
            _MARKER_END,
        ]
        rendered["docs/RUN_LOG.md"] = (
            run_log + "\n\n" + "\n".join(run_entry) + "\n"
        ).encode("utf-8")
        return rendered

    def _validate_rendered(
        self,
        request: FeatureDecisionRequest,
        changed_paths: tuple[str, ...],
    ) -> dict[str, Any]:
        repository = request.project.repository
        queue = FeatureQueue.from_location(
            repository, request.project.queue_location
        )
        _assert_acyclic(queue)
        baseline = {
            str(item["id"]): json.loads(json.dumps(item))
            for item in request.baseline_features
        }
        after = {str(item["id"]): item for item in queue.features}
        if set(after) != set(baseline):
            raise QueueError(
                "feature IDs changed during decision resolution"
            )
        expected_features = json.loads(json.dumps(baseline))
        for decision in request.decisions:
            expected = expected_features[decision.feature_id]
            before_dependencies = list(expected.get("dependencies") or ())
            if decision.approved_dependencies is not None:
                expected["dependencies"] = list(decision.approved_dependencies)
            expected["status"] = decision.target_status
            expected["requires_human_decision"] = False
            expected["decision_resolution"] = {
                "recorded_question": decision.recorded_question,
                "approved_resolution": decision.approved_resolution,
                "dependency_change": {
                    "before": before_dependencies,
                    "after": list(expected.get("dependencies") or ()),
                },
                "selected_feature": (
                    decision.feature_id == request.selected_feature
                ),
                "decision_file_sha256": request.decision_sha256,
            }
        expected_previous = expected_features[
            request.expected_selected_feature
        ]
        expected_previous["status"] = "proposed"
        expected_previous["selection_resolution"] = {
            "previous_status": "ready",
            "resulting_status": "proposed",
            "implementation_started": False,
            "preparation_started": False,
            "selected_feature": request.selected_feature,
            "decision_file_sha256": request.decision_sha256,
        }
        changed_metadata = sorted(
            feature_id
            for feature_id in after
            if after[feature_id] != expected_features[feature_id]
        )
        if changed_metadata:
            raise QueueError(
                "feature metadata changed outside the exact decision contract: "
                + ", ".join(changed_metadata)
            )
        ready = [
            str(item["id"])
            for item in queue.features_for_milestone(
                request.project.active_milestone or ""
            )
            if item.get("status") == "ready"
        ]
        if ready != [request.selected_feature]:
            raise QueueError(
                "decision resolution must leave exactly one ready feature"
            )
        for decision in request.decisions:
            feature = queue.feature(decision.feature_id) or {}
            if (
                feature.get("status") != decision.target_status
                or feature.get("requires_human_decision") is not False
            ):
                raise QueueError(
                    "decision resolution did not clear feature gate: "
                    + decision.feature_id
                )
            if (
                decision.approved_dependencies is not None
                and tuple(feature.get("dependencies") or ())
                != decision.approved_dependencies
            ):
                raise QueueError(
                    f"dependency resolution disagrees for {decision.feature_id}"
                )
            evidence = feature.get("decision_resolution")
            if (
                not isinstance(evidence, dict)
                or evidence.get("recorded_question")
                != decision.recorded_question
                or evidence.get("approved_resolution")
                != decision.approved_resolution
            ):
                raise QueueError(
                    "durable decision evidence is incomplete: "
                    + decision.feature_id
                )
        previous = queue.feature(request.expected_selected_feature) or {}
        if previous.get("status") != "proposed":
            raise QueueError(
                "previous selected feature did not return to proposed"
            )
        unauthorized = sorted(
            set(changed_paths) - set(request.authorized_paths)
        )
        if unauthorized:
            raise QueueError(
                "feature-decision resolution changed unauthorized paths: "
                + ", ".join(unauthorized)
            )
        check = RepositoryInspector(repository).git(
            ["diff", "--check"], check=False
        )
        if check.returncode != 0:
            raise QueueError(
                "feature-decision resolution git diff --check failed"
            )
        return {
            "ready_features": ready,
            "selected_feature": request.selected_feature,
            "previous_selected_feature": request.expected_selected_feature,
            "changed_paths": list(changed_paths),
            "dependency_changes": {
                item.feature_id: {
                    "before": list(item.expected_dependencies),
                    "after": list(item.approved_dependencies or ()),
                }
                for item in request.decisions
                if item.expected_dependencies is not None
            },
        }

    @staticmethod
    def _write_rendered(
        repository: Path,
        rendered: dict[str, bytes],
        originals: dict[str, bytes],
    ) -> tuple[str, ...]:
        written: list[str] = []
        try:
            for relative, value in rendered.items():
                if value == originals[relative]:
                    continue
                target = repository / relative
                mode = target.stat().st_mode & 0o777
                atomic_write_bytes(target, value, mode=mode)
                written.append(relative)
        except Exception:
            for relative in reversed(written):
                target = repository / relative
                mode = target.stat().st_mode & 0o777
                atomic_write_bytes(target, originals[relative], mode=mode)
            raise
        return tuple(sorted(written))

    def apply(
        self,
        request: FeatureDecisionRequest,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        project = request.project
        repository = project.repository.resolve()
        refreshed = self.inspect(
            project=project,
            decision_path=request.decision_path,
            selected_feature=request.selected_feature,
        )
        if refreshed.decision_fingerprint != request.decision_fingerprint:
            raise RecoveryError(
                "feature-decision plan changed before apply"
            )
        inspector = RepositoryInspector(repository)
        identity, ledger, projection, current = self._state(
            project, inspector
        )
        if (
            current["ledger_sequence"] != request.starting_ledger_sequence
            or current["ledger_fingerprint"]
            != request.starting_ledger_fingerprint
            or current["projection_fingerprint"]
            != request.starting_projection_fingerprint
        ):
            raise RecoveryError("controller evidence changed before apply")

        run_id = run_id or f"feature-decisions-{uuid.uuid4()}"
        report_root = self.configuration.owned_path(
            self.configuration.conveyor["report_directory"]
        ) / run_id
        report_path = report_root / "feature-decision-resolution.json"
        lease = WorkflowWriterLease(
            inspector.writer_lock_path(
                self.configuration.conveyor["lock_policy"][
                    "writer_lock_relative_path"
                ]
            )
        )
        adapter = QueueReconciliationAdapter(
            allowed_paths=request.authorized_paths,
            allow_untracked=False,
            denied_paths=(
                ".factory/conveyor-state.json",
                ".factory/locks/writer.json",
            ),
            denied_prefixes=(
                "Sources",
                "Tests",
                "src",
                "tests",
                ".factory",
            ),
            commit_subject=request.commit_subject,
            next_state="feature_ready",
        )
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection,
            lease=lease,
        )
        reservation = DurableLock(self._reservation_path(identity))
        reservation.acquire(
            make_lock_record(
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                run_id=run_id,
                current_feature=request.selected_feature,
                current_phase="resolve_feature_decisions",
            )
        )
        original_bytes: dict[str, bytes] = {}
        for path in request.authorized_paths:
            payload = inspector.safe_worktree_file_bytes(path)
            assert payload is not None
            original_bytes[path] = payload
        commit_created = False
        try:
            transaction = kernel.begin(
                workflow_type=WorkflowType.QUEUE_RECONCILIATION,
                milestone=project.active_milestone or "",
                feature_id=request.selected_feature,
                run_id=run_id,
                policy=adapter.policy,
                expected_starting_branch=request.expected_branch,
                expected_starting_head=request.expected_head,
                start_evidence={
                    "command": "resolve-feature-decisions",
                    "decision_fingerprint": request.decision_fingerprint,
                    "previous_selected_feature": (
                        request.expected_selected_feature
                    ),
                    "selected_feature": request.selected_feature,
                    "historical_project_gate_preserved": (
                        request.historical_project_gate is not None
                    ),
                    "historical_project_gate_fingerprint": (
                        fingerprint(request.historical_project_gate)
                        if request.historical_project_gate
                        else None
                    ),
                },
            )
            kernel.acquire_lease()
            inspector.ensure_runtime_ignored()
            kernel.capture_snapshot()
            rendered = self._render(request)
            changed_paths = self._write_rendered(
                repository, rendered, original_bytes
            )
            validation = self._validate_rendered(request, changed_paths)
            kernel.finalize_deterministic_planning(
                changed_paths=changed_paths,
                plan_fingerprint=request.decision_fingerprint,
                validation_evidence={
                    "commands": [["git", "diff", "--check"]],
                    "warnings": [],
                    "checks": validation,
                },
                selected_feature=request.selected_feature,
                execution_mode="queue_feature_decision_resolution",
            )
            commit = kernel.finalize()
            commit_created = True
            terminal = kernel.complete(
                classification="RECONCILED_READY_WORK",
                evidence={
                    "planning_status": "passed",
                    "selected_feature": request.selected_feature,
                    "previous_selected_feature": (
                        request.expected_selected_feature
                    ),
                    "decision_fingerprint": request.decision_fingerprint,
                    "feature_decision_resolutions": [
                        item.evidence() for item in request.decisions
                    ],
                    "dependency_changes": validation["dependency_changes"],
                    "historical_project_gate_preserved": (
                        request.historical_project_gate is not None
                    ),
                    "historical_project_gate_fingerprint": (
                        fingerprint(request.historical_project_gate)
                        if request.historical_project_gate
                        else None
                    ),
                    "feature_execution_started": False,
                    "model_sessions_launched": 0,
                    "child_sessions_launched": 0,
                },
            )
            cycle_path = inspector.cycle_state_path()
            cycle_state = (
                load_json(cycle_path)
                if cycle_path.exists()
                else self._new_cycle_state(
                    request=request,
                    identity=identity,
                    run_id=run_id,
                    commit=str(commit),
                )
            )
            cycle_state.update(
                {
                    "conveyor_run_id": run_id,
                    "current_feature": request.selected_feature,
                    "selected_feature": request.selected_feature,
                    "feature_dependencies": list(
                        (
                            FeatureQueue.from_location(
                                repository, project.queue_location
                            ).feature(request.selected_feature)
                            or {}
                        ).get("dependencies", [])
                    ),
                    "dependency_evidence": {},
                    "queue_fingerprint": _sha256(
                        (repository / project.queue_location).read_bytes()
                    ),
                    "feature_branch": None,
                    "feature_worktree": str(repository),
                    "feature_starting_commit": commit,
                    "accepted_feature_commit": None,
                    "milestone_pre_integration_commit": commit,
                    "milestone_post_integration_commit": None,
                    "writer_lock_identity": None,
                    "last_successful_checkpoint": (
                        "feature_decisions_committed"
                    ),
                    "last_verified_git_state": {
                        "branch": inspector.current_branch,
                        "head": inspector.head,
                        "clean": inspector.is_clean,
                        "git_operations": inspector.git_operation_state(),
                    },
                    "stop_reason": (
                        "queue-feature decisions resolved; "
                        "feature execution did not start"
                    ),
                    "human_decision_required": None,
                    "session_id": None,
                    "updated_at": utc_now(),
                    **cycle_cache_semantics_from_projection(
                        terminal["projection"]
                    ),
                }
            )
            cache = write_terminal_cycle_cache(
                cycle_path,
                cycle_state,
                ledger=ledger,
                projection_engine=projection,
                transaction_id=transaction.transaction_id,
                expected_feature=request.selected_feature,
            )
            report = {
                **request.public_plan(dry_run=False),
                "run_id": run_id,
                "transaction_id": transaction.transaction_id,
                "outcome": "feature_decisions_resolved",
                "next_state": "feature_ready",
                "planning_result_commit": commit,
                "planning_result_parent": request.expected_head,
                "changed_paths": list(changed_paths),
                "dependency_changes": validation["dependency_changes"],
                "ready_features": validation["ready_features"],
                "ledger_sequence": terminal["projection"]["ledger_sequence"],
                "ledger_fingerprint": terminal["projection"][
                    "ledger_fingerprint"
                ],
                "projection_fingerprint": terminal["projection"][
                    "projection_fingerprint"
                ],
                "compatibility_cache_fingerprint": cache[
                    "kernel_cache_fingerprint"
                ],
                "application_repository_written": True,
                "controller_state_written": True,
                "model_sessions_launched": 0,
                "child_sessions_launched": 0,
                "feature_execution_started": False,
                "repository_clean": inspector.is_clean,
            }
            atomic_write_json(report_path, report)
            return report
        except Exception as exc:
            if not commit_created and inspector.head == request.expected_head:
                changed = tuple(
                    sorted(
                        set(inspector.tracked_changed_paths())
                        | set(inspector.untracked_file_hashes())
                    )
                )
                for relative, value in original_bytes.items():
                    target = repository / relative
                    mode = target.stat().st_mode & 0o777
                    atomic_write_bytes(target, value, mode=mode)
                if changed:
                    inspector.stage_planning_paths(
                        list(changed),
                        commit_subject=request.commit_subject,
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
                ledger.append(
                    event_type="ProjectionUpdated",
                    transaction_id=kernel.transaction.transaction_id,
                    workflow_type=kernel.transaction.workflow_type,
                    payload={
                        "current_state": "feature_ready",
                        "current_feature": None,
                        "selected_feature": request.expected_selected_feature,
                        "rollback_restored_starting_selection": True,
                    },
                )
                projection.rebuild(persist_cache=True)
            if isinstance(exc, (ConveyorError, OSError, ValueError)):
                raise
            raise RecoveryError(
                f"feature-decision resolution apply failed: {exc}"
            ) from exc
        finally:
            reservation.release(run_id)

    @staticmethod
    def _new_cycle_state(
        *,
        request: FeatureDecisionRequest,
        identity: dict[str, Any],
        run_id: str,
        commit: str,
    ) -> dict[str, Any]:
        project = request.project
        repository = project.repository
        selected = FeatureQueue.from_location(
            repository, project.queue_location
        ).feature(request.selected_feature) or {}
        now = utc_now()
        return {
            "schema_version": 1,
            "conveyor_run_id": run_id,
            "project_id": project.project_id,
            "repository_identity": identity,
            "repository_path_fingerprint": identity["path_fingerprint"],
            "active_milestone": project.active_milestone,
            "current_feature": request.selected_feature,
            "selected_feature": request.selected_feature,
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
            "last_successful_checkpoint": "feature_decisions_committed",
            "last_verified_git_state": {},
            "stop_reason": (
                "queue-feature decisions resolved; feature execution did not start"
            ),
            "human_decision_required": None,
            "resume_instructions": (
                f"scripts/conveyor run --project {project.project_id} "
                f"--mode {project.automation_mode}"
            ),
            "session_id": None,
            "created_at": now,
            "updated_at": now,
        }
