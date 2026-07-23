from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.accepted_commit import (
    _replace_current_status,
    acceptance_metadata_paths,
    materialize_acceptance_metadata,
)
from development_conveyor.accepted_commit_recovery import AcceptedCommitRecovery
from development_conveyor.contracts import MutationPolicy, WorkflowType
from development_conveyor.consistency import ConsistencyChecker
from development_conveyor.errors import (
    IntegrationPlanError,
    RecoveryError,
    TransactionError,
)
from development_conveyor.integration_executor import inspect_two_refs
from development_conveyor.kernel import WorkflowKernel
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.projection import ProjectionEngine
from development_conveyor.repository import RepositoryInspector
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import controller_configuration, git, synthetic_repository, write_json


class AcceptedCommitFinalizationTests(unittest.TestCase):
    def test_factory_position_feature_labels_are_replaced_semantically(self):
        for label in ("Selected next feature", "Selected feature", "Active feature"):
            with self.subTest(label=label):
                source = (
                    "# Current Status\n\n"
                    "- Selected feature: F009 — historical text outside the section\n\n"
                    "## Factory position\n\n"
                    f"- {label}: F009 — Transcription Engine Abstraction "
                    "(implemented; controller acceptance pending)\n"
                    "- Queue state: F009 remains otherwise documented.\n\n"
                    "## Next boundary\n\n"
                    "- Active feature: F009 — another historical reference\n"
                )
                rendered = _replace_current_status(
                    source,
                    feature_id="F009",
                    title="Transcription Engine Abstraction",
                )
                self.assertIn(
                    "- Active feature: F009 — Transcription Engine Abstraction "
                    "(accepted; milestone integration pending)",
                    rendered,
                )
                self.assertIn(
                    "- Selected feature: F009 — historical text outside the section",
                    rendered,
                )
                self.assertIn(
                    "- Active feature: F009 — another historical reference",
                    rendered,
                )

    def test_factory_position_missing_duplicate_and_wrong_feature_fail_closed(self):
        missing = "# Current Status\n\n## Summary\n\n- Active feature: F009\n"
        duplicate = (
            "# Current Status\n\n## Factory position\n\n"
            "- Selected feature: F009 — One\n"
            "- Active feature: F009 — Two\n"
        )
        wrong = (
            "# Current Status\n\n## Factory position\n\n"
            "- Selected feature: F008 — Audio Engine Abstraction\n"
        )
        for text, diagnostic in (
            (missing, "Factory position section"),
            (duplicate, "exactly one authoritative"),
            (wrong, "another feature"),
        ):
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaisesRegex(TransactionError, diagnostic):
                    _replace_current_status(
                        text,
                        feature_id="F009",
                        title="Transcription Engine Abstraction",
                    )

    def _candidate_fixture(
        self,
        root: Path,
        *,
        project_id: str = "synthetic",
        feature_id: str = "F001",
        title: str = "Synthetic Feature",
        status_label: str = "Active feature",
    ):
        repository, project = synthetic_repository(
            root, controller_project_id=project_id
        )
        RepositoryInspector(repository).ensure_runtime_ignored()
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        feature.update(
            {
                "id": feature_id,
                "title": title,
                "status": "ready",
                "implementation_status": "Ready",
                "spec": f"docs/features/{feature_id}.md",
                "branch": None,
                "integration_base_commit": None,
                "accepted_commit": None,
            }
        )
        write_json(queue_path, queue)
        old_spec = repository / "docs/features/F001.md"
        specification = repository / f"docs/features/{feature_id}.md"
        if specification != old_spec:
            specification.write_text(
                f"# {feature_id}\n\n"
                "- Factory status: Ready\n\n"
                "- [ ] Implement the feature.\n",
                encoding="utf-8",
            )
            old_spec.unlink()
        else:
            specification.write_text(
                f"# {feature_id}\n\n"
                "- Factory status: Ready\n\n"
                "- [ ] Implement the feature.\n",
                encoding="utf-8",
            )
        (repository / "docs/CURRENT_STATUS.md").write_text(
            "# Current Status\n\n"
            "## Factory position\n\n"
            f"- {status_label}: {feature_id} — {title} "
            "(implementation complete; controller acceptance pending)\n\n"
            "## Verified health\n\n"
            f"- Historical reference: {feature_id} remains documented here.\n",
            encoding="utf-8",
        )
        (repository / "docs/FEATURE_CATALOG.md").write_text(
            "# Feature Catalog\n\n"
            "| Feature ID | Name | Status | Milestone |\n"
            "| --- | --- | --- | --- |\n"
            f"| {feature_id} | {title} | Ready | M0 |\n",
            encoding="utf-8",
        )
        (repository / "docs/RUN_LOG.md").write_text(
            "# Run Log\n", encoding="utf-8"
        )
        git(repository, "add", ".")
        git(repository, "commit", "-m", "prepare candidate metadata")
        base = git(repository, "rev-parse", "HEAD")
        branch = f"codex/{feature_id}-candidate"
        git(repository, "switch", "-c", branch)
        (repository / "app.txt").write_text(
            "baseline\nvalidated implementation\n", encoding="utf-8"
        )
        git(repository, "add", "app.txt")
        git(repository, "commit", "-m", f"{feature_id}: {title}")
        candidate = git(repository, "rev-parse", "HEAD")
        project = project.__class__(
            **{
                **project.__dict__,
                "milestone_branch": "codex/m0-foundation",
                "validated_baseline_commit": base,
            }
        )
        configuration = controller_configuration(root, project)
        return repository, project, configuration, base, branch, candidate

    def _finalize(self, root: Path):
        (
            repository,
            project,
            configuration,
            base,
            branch,
            candidate,
        ) = self._candidate_fixture(root)
        identity = RepositoryInspector(repository).identity()
        ledger = EvidenceLedger(
            configuration.root / "state/projects/synthetic/evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        projection = ProjectionEngine(
            ledger,
            configuration.root
            / "state/projects/synthetic/projection-cache.json",
        )
        feature = FeatureQueueProxy(repository, project).feature("F001")
        metadata_paths = acceptance_metadata_paths(project, feature)
        kernel = WorkflowKernel(
            project=project,
            ledger=ledger,
            projection=projection,
            lease=WorkflowWriterLease(
                repository / ".factory/locks/writer.json"
            ),
        )
        transaction = kernel.begin(
            workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
            milestone="M0",
            feature_id="F001",
            run_id="normal-finalization",
            policy=MutationPolicy(
                metadata_paths, commit_subject="F001: Synthetic Feature"
            ),
            expected_starting_branch=branch,
            expected_starting_head=candidate,
        )
        kernel.acquire_lease()
        kernel.capture_snapshot()
        self.assertEqual(
            materialize_acceptance_metadata(
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                candidate_commit=candidate,
                recovery=False,
            ),
            metadata_paths,
        )
        finalization = kernel.finalize_deterministic_accepted_commit(
            candidate_commit=candidate,
            milestone_id="M0",
            milestone_branch="codex/m0-foundation",
            milestone_base=base,
            feature_branch=branch,
            metadata_paths=metadata_paths,
            validation_evidence={
                "tests_passed": True,
                "review_passed": True,
                "documentation_current": True,
            },
        )
        completion = kernel.complete(
            classification="FEATURE_ACCEPTED",
            evidence={
                "accepted_feature_commit": finalization[
                    "finalized_accepted_commit"
                ],
                "candidate_implementation_commit": candidate,
                "integration_status": "pending",
            },
        )
        return {
            "repository": repository,
            "project": project,
            "configuration": configuration,
            "base": base,
            "branch": branch,
            "candidate": candidate,
            "metadata_paths": metadata_paths,
            "finalization": finalization,
            "completion": completion,
            "ledger": ledger,
            "transaction": transaction,
        }

    def test_late_metadata_render_error_leaves_candidate_worktree_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                base,
                branch,
                _,
            ) = self._candidate_fixture(Path(temporary))
            current_status = repository / "docs/CURRENT_STATUS.md"
            current_status.write_text(
                "# Current Status\n\n## Summary\n\n"
                "- Selected feature: F001 — outside Factory position\n",
                encoding="utf-8",
            )
            git(repository, "add", "docs/CURRENT_STATUS.md")
            git(repository, "commit", "--amend", "--no-edit")
            candidate = git(repository, "rev-parse", "HEAD")
            paths = (
                project.queue_location,
                "docs/FEATURE_CATALOG.md",
                "docs/features/F001.md",
                "docs/CURRENT_STATUS.md",
                "docs/RUN_LOG.md",
            )
            before = {
                path: (repository / path).read_bytes()
                for path in paths
            }
            with self.assertRaisesRegex(TransactionError, "Factory position"):
                materialize_acceptance_metadata(
                    project=project,
                    feature_id="F001",
                    feature_branch=branch,
                    milestone_base=base,
                    candidate_commit=candidate,
                    recovery=False,
                )
            self.assertEqual(git(repository, "status", "--porcelain"), "")
            self.assertEqual(
                {path: (repository / path).read_bytes() for path in paths},
                before,
            )

    def test_transactional_metadata_write_rolls_back_a_mid_set_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                base,
                branch,
                candidate,
            ) = self._candidate_fixture(Path(temporary))
            before = {
                relative: (repository / relative).read_bytes()
                for relative in (
                    project.queue_location,
                    "docs/CURRENT_STATUS.md",
                    "docs/FEATURE_CATALOG.md",
                    "docs/RUN_LOG.md",
                    "docs/features/F001.md",
                )
            }
            real_replace = os.replace
            replacement_count = 0

            def fail_third_acceptance_replace(source, target):
                nonlocal replacement_count
                if ".acceptance-" in str(source):
                    replacement_count += 1
                    if replacement_count == 3:
                        raise OSError("synthetic transactional write failure")
                return real_replace(source, target)

            with patch(
                "development_conveyor.accepted_commit.os.replace",
                side_effect=fail_third_acceptance_replace,
            ):
                with self.assertRaisesRegex(
                    OSError, "synthetic transactional write failure"
                ):
                    materialize_acceptance_metadata(
                        project=project,
                        feature_id="F001",
                        feature_branch=branch,
                        milestone_base=base,
                        candidate_commit=candidate,
                        recovery=False,
                    )
            self.assertEqual(git(repository, "status", "--porcelain"), "")
            self.assertEqual(
                {
                    relative: (repository / relative).read_bytes()
                    for relative in before
                },
                before,
            )

    def test_normal_finalization_creates_one_direct_child_and_records_both_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._finalize(Path(temporary))
            inspector = RepositoryInspector(result["repository"])
            finalized = result["finalization"]["finalized_accepted_commit"]
            self.assertEqual(
                inspector.rev_parse(f"{finalized}^"), result["base"]
            )
            self.assertEqual(inspector.rev_parse(result["branch"]), finalized)
            self.assertEqual(
                result["finalization"][
                    "candidate_implementation_tree_fingerprint"
                ],
                result["finalization"][
                    "finalized_implementation_tree_fingerprint"
                ],
            )
            self.assertEqual(
                tuple(
                    sorted(
                        inspector.git(
                            [
                                "diff",
                                "--name-only",
                                result["candidate"],
                                finalized,
                                "--",
                            ]
                        ).stdout.splitlines()
                    )
                ),
                result["metadata_paths"],
            )
            queue = json.loads(
                inspector.file_at_commit(
                    finalized, result["project"].queue_location
                )
            )
            feature = queue["features"][0]
            self.assertEqual(feature["accepted_commit"], "SELF")
            self.assertEqual(feature["status"], "integration_pending")
            self.assertTrue(all(feature["acceptance"].values()))
            events = result["ledger"].read()
            finalized_event = next(
                event
                for event in events
                if event["event_type"] == "CommitFinalized"
            )
            self.assertEqual(
                finalized_event["payload"]["candidate_implementation_commit"],
                result["candidate"],
            )
            self.assertEqual(
                finalized_event["payload"]["finalized_accepted_commit"],
                finalized,
            )
            self.assertEqual(
                result["completion"]["projection"]["current_state"],
                "integration_ready",
            )
            consistency = ConsistencyChecker(
                controller_root=result["configuration"].root,
                project=result["project"],
            ).check()
            immutable = next(
                item
                for item in consistency["invariants"]
                if item["invariant"] == "accepted_commit_immutability"
            )
            self.assertTrue(immutable["passed"])

    def test_missing_acceptance_metadata_cannot_reach_integration_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                repository,
                project,
                configuration,
                base,
                branch,
                candidate,
            ) = self._candidate_fixture(root)
            identity = RepositoryInspector(repository).identity()
            ledger = EvidenceLedger(
                configuration.root
                / "state/projects/synthetic/evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            feature = FeatureQueueProxy(repository, project).feature("F001")
            metadata_paths = acceptance_metadata_paths(project, feature)
            kernel = WorkflowKernel(
                project=project,
                ledger=ledger,
                projection=ProjectionEngine(ledger),
                lease=WorkflowWriterLease(
                    repository / ".factory/locks/writer.json"
                ),
            )
            kernel.begin(
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                milestone="M0",
                feature_id="F001",
                run_id="missing-metadata",
                policy=MutationPolicy(
                    metadata_paths, commit_subject="F001: Synthetic Feature"
                ),
            )
            kernel.acquire_lease()
            kernel.capture_snapshot()
            materialize_acceptance_metadata(
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                candidate_commit=candidate,
                recovery=False,
            )
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0]["acceptance"]["review_passed"] = False
            write_json(queue_path, queue)
            with self.assertRaisesRegex(
                TransactionError, "immutable metadata validation"
            ):
                kernel.finalize_deterministic_accepted_commit(
                    candidate_commit=candidate,
                    milestone_id="M0",
                    milestone_branch="codex/m0-foundation",
                    milestone_base=base,
                    feature_branch=branch,
                    metadata_paths=metadata_paths,
                    validation_evidence={
                        "tests_passed": True,
                        "review_passed": True,
                        "documentation_current": True,
                    },
                )
            self.assertNotEqual(
                ProjectionEngine(ledger).rebuild(
                    persist_cache=False
                )["current_state"],
                "integration_ready",
            )
            self.assertEqual(
                RepositoryInspector(repository).rev_parse(branch), candidate
            )

    def test_conflicting_candidate_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, _, base, branch, candidate = (
                self._candidate_fixture(root)
            )
            feature = FeatureQueueProxy(repository, project).feature("F001")
            metadata_paths = acceptance_metadata_paths(project, feature)
            materialize_acceptance_metadata(
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                candidate_commit=candidate,
                recovery=False,
            )
            (repository / "app.txt").write_text(
                "conflicting implementation\n", encoding="utf-8"
            )
            from development_conveyor.accepted_commit import finalize_accepted_commit

            with self.assertRaisesRegex(
                TransactionError, "authorized accepted path set"
            ):
                finalize_accepted_commit(
                    project=project,
                    controller_project_id=project.project_id,
                    feature_id="F001",
                    feature_branch=branch,
                    milestone_id="M0",
                    milestone_branch="codex/m0-foundation",
                    milestone_base=base,
                    candidate_commit=candidate,
                    metadata_paths=metadata_paths,
                    commit_subject="F001: Synthetic Feature",
                )
            self.assertEqual(
                RepositoryInspector(repository).rev_parse(branch), candidate
            )

    def test_metadata_only_child_on_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._finalize(Path(temporary))
            repository = result["repository"]
            finalized = result["finalization"]["finalized_accepted_commit"]
            (repository / "docs/RUN_LOG.md").write_text(
                (repository / "docs/RUN_LOG.md").read_text(encoding="utf-8")
                + "\nsecond metadata commit\n",
                encoding="utf-8",
            )
            git(repository, "add", "docs/RUN_LOG.md")
            git(repository, "commit", "-m", "metadata-only child")
            child = git(repository, "rev-parse", "HEAD")
            with self.assertRaisesRegex(
                IntegrationPlanError, "exact permitted single child"
            ):
                inspect_two_refs(
                    repository=repository,
                    controller_project_id=result["project"].project_id,
                    feature_id="F001",
                    feature_branch=result["branch"],
                    accepted_commit=child,
                    milestone_id="M0",
                    milestone_branch="codex/m0-foundation",
                    pre_integration_head=result["base"],
                    queue_path=result["project"].queue_location,
                )
            self.assertEqual(git(repository, "rev-parse", f"{child}^"), finalized)


class FeatureQueueProxy:
    def __init__(self, repository: Path, project):
        self.queue = json.loads(
            (repository / project.queue_location).read_text(encoding="utf-8")
        )

    def feature(self, feature_id: str):
        return next(
            item for item in self.queue["features"] if item["id"] == feature_id
        )


class AcceptedCommitRecoveryTests(unittest.TestCase):
    FEATURE_TRANSACTION = "feature-transaction"
    ACCEPTANCE_TRANSACTION = "acceptance-transaction"

    def _fixture(self, root: Path):
        helper = AcceptedCommitFinalizationTests()
        (
            repository,
            project,
            configuration,
            base,
            branch,
            candidate,
        ) = helper._candidate_fixture(
            root,
            project_id="interview-companion",
            feature_id="F004",
            title="Navigation and Workspace Restoration",
        )
        sources = repository / "Sources/App"
        tests = repository / "Tests/AppTests"
        sources.mkdir(parents=True)
        tests.mkdir(parents=True)
        (sources / "Workspace.swift").write_text(
            "struct Workspace {}\n", encoding="utf-8"
        )
        (tests / "WorkspaceTests.swift").write_text(
            "// workspace tests\n", encoding="utf-8"
        )
        git(repository, "add", "Sources", "Tests")
        git(repository, "commit", "--amend", "--no-edit")
        candidate = git(repository, "rev-parse", "HEAD")
        identity = RepositoryInspector(repository).identity()
        ledger = EvidenceLedger(
            configuration.root
            / "state/projects/interview-companion/evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        feature_events = [
            ("TransactionStarted", {
                "run_id": "feature-run",
                "milestone": "M0",
                "feature_id": "F004",
                "starting_branch": branch,
                "starting_head": base,
            }),
            ("LeaseAcquired", {}),
            ("SnapshotCaptured", {"snapshot": {"branch": branch, "head": base}}),
            ("SessionLaunched", {"session_id": "feature-session"}),
            ("SessionResultAccepted", {"classification": "FEATURE_ACCEPTED"}),
            ("ChangesDetected", {}),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            ("CommitFinalized", {"commit": candidate, "parent": base}),
            ("CheckpointRecorded", {}),
            ("CheckpointRecorded", {}),
            ("CheckpointRecorded", {}),
            ("CheckpointRecorded", {}),
            ("TransactionCompleted", {
                "classification": "FEATURE_ACCEPTED",
                "feature_id": "F004",
                "accepted_feature_commit": candidate,
                "next_state": "feature_accepted",
            }),
            ("LeaseReleased", {}),
            ("ProjectionUpdated", {
                "current_state": "feature_accepted",
                "current_feature": "F004",
            }),
        ]
        for event_type, payload in feature_events:
            ledger.append(
                event_type=event_type,
                transaction_id=self.FEATURE_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )
        acceptance_events = [
            ("TransactionStarted", {
                "run_id": "feature-run",
                "milestone": "M0",
                "feature_id": "F004",
                "starting_branch": branch,
                "starting_head": candidate,
            }),
            ("LeaseAcquired", {}),
            ("SnapshotCaptured", {
                "snapshot": {"branch": branch, "head": candidate}
            }),
            ("SessionLaunched", {"session_id": "deterministic-acceptance"}),
            ("SessionResultAccepted", {"classification": "FEATURE_ACCEPTED"}),
            ("ChangesDetected", {"changed_paths": []}),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            ("CommitFinalized", {
                "commit": candidate,
                "parent": None,
                "no_change": True,
            }),
            ("CheckpointRecorded", {}),
            ("TransactionCompleted", {
                "classification": "FEATURE_ACCEPTED",
                "feature_id": "F004",
                "accepted_feature_commit": candidate,
                "integration_status": "pending",
                "next_state": "integration_ready",
            }),
            ("LeaseReleased", {}),
            ("ProjectionUpdated", {
                "current_state": "integration_ready",
                "current_feature": "F004",
            }),
        ]
        for event_type, payload in acceptance_events:
            ledger.append(
                event_type=event_type,
                transaction_id=self.ACCEPTANCE_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                payload=payload,
            )
        ProjectionEngine(
            ledger,
            configuration.root
            / "state/projects/interview-companion/projection-cache.json",
        ).rebuild()
        exclude = repository / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8")
            + "\n.factory/conveyor-state.json\n.factory/locks/writer.json\n",
            encoding="utf-8",
        )
        write_json(
            repository / ".factory/conveyor-state.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "current_feature": "F004",
                "current_phase": "integration_ready",
                "accepted_feature_commit": candidate,
            },
        )
        recovery = AcceptedCommitRecovery(
            controller_root=configuration.root,
            configuration=configuration.conveyor,
            project=project,
        )
        constants = {
            "F004_CANDIDATE": candidate,
            "F004_MILESTONE_BASE": base,
            "F004_BRANCH": branch,
            "F004_FEATURE_TRANSACTION": self.FEATURE_TRANSACTION,
            "F004_ACCEPTANCE_TRANSACTION": self.ACCEPTANCE_TRANSACTION,
        }
        return repository, project, configuration, ledger, recovery, constants

    def _partial_f009_fixture(self, root: Path):
        helper = AcceptedCommitFinalizationTests()
        (
            repository,
            project,
            configuration,
            base,
            branch,
            candidate,
        ) = helper._candidate_fixture(
            root,
            project_id="interview-companion",
            feature_id="F009",
            title="Transcription Engine Abstraction",
            status_label="Selected feature",
        )
        sources = repository / "Sources/App"
        tests = repository / "Tests/AppTests"
        sources.mkdir(parents=True)
        tests.mkdir(parents=True)
        (sources / "Transcription.swift").write_text(
            "protocol TranscriptionEngine {}\n", encoding="utf-8"
        )
        (tests / "TranscriptionTests.swift").write_text(
            "// transcription tests\n", encoding="utf-8"
        )
        git(repository, "add", "Sources", "Tests")
        git(repository, "commit", "--amend", "--no-edit")
        candidate = git(repository, "rev-parse", "HEAD")

        identity = RepositoryInspector(repository).identity()
        ledger = EvidenceLedger(
            configuration.root
            / "state/projects/interview-companion/evidence-ledger.jsonl",
            project_id=project.project_id,
            repository_identity=identity["repository_id"],
            repository_path_fingerprint=identity["path_fingerprint"],
        )
        feature_events = [
            ("TransactionStarted", {
                "run_id": "f009-run",
                "milestone": "M0",
                "feature_id": "F009",
                "starting_branch": branch,
                "starting_head": base,
            }),
            ("LeaseAcquired", {}),
            ("SnapshotCaptured", {"snapshot": {"branch": branch, "head": base}}),
            ("SessionLaunched", {"session_id": "f009-session"}),
            ("SessionResultAccepted", {"classification": "FEATURE_ACCEPTED"}),
            ("ChangesDetected", {}),
            ("ValidationStarted", {}),
            ("ValidationPassed", {}),
            ("CommitFinalized", {"commit": candidate, "parent": base}),
            ("CheckpointRecorded", {}),
            ("CheckpointRecorded", {}),
            ("CheckpointRecorded", {}),
            ("TransactionCompleted", {
                "classification": "FEATURE_ACCEPTED",
                "feature_id": "F009",
                "candidate_implementation_commit": candidate,
                "next_state": "feature_accepted",
            }),
            ("LeaseReleased", {}),
            ("ProjectionUpdated", {
                "current_state": "feature_accepted",
                "current_feature": "F009",
            }),
        ]
        for event_type, payload in feature_events:
            ledger.append(
                event_type=event_type,
                transaction_id=self.FEATURE_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_EXECUTION,
                payload=payload,
            )

        feature = FeatureQueueProxy(repository, project).feature("F009")
        metadata_paths = acceptance_metadata_paths(project, feature)
        materialize_acceptance_metadata(
            project=project,
            feature_id="F009",
            feature_branch=branch,
            milestone_base=base,
            candidate_commit=candidate,
            recovery=False,
        )
        inspector = RepositoryInspector(repository)
        for relative in ("docs/CURRENT_STATUS.md", "docs/RUN_LOG.md"):
            original = inspector.file_at_commit(candidate, relative)
            self.assertIsNotNone(original)
            (repository / relative).write_text(original, encoding="utf-8")
        retained_paths = tuple(inspector.tracked_changed_paths())
        retained_fingerprint = inspector.planning_diff_fingerprint()

        acceptance_events = [
            ("TransactionStarted", {
                "run_id": "f009-run",
                "milestone": "M0",
                "feature_id": "F009",
                "starting_branch": branch,
                "starting_head": candidate,
            }),
            ("LeaseAcquired", {}),
            ("SnapshotCaptured", {
                "snapshot": {"branch": branch, "head": candidate}
            }),
            ("TransactionBlocked", {
                "classification": "FEATURE_VALIDATION_FAILED",
                "reference": "TransactionError",
                "next_state": "validation_failed",
                "terminal_snapshot": {
                    "branch": branch,
                    "head": candidate,
                    "tracked_changed_paths": list(retained_paths),
                    "tracked_diff_fingerprint": retained_fingerprint,
                },
            }),
            ("LeaseReleased", {}),
            ("ProjectionUpdated", {
                "current_state": "validation_failed",
                "current_feature": "F009",
            }),
        ]
        for event_type, payload in acceptance_events:
            ledger.append(
                event_type=event_type,
                transaction_id=self.ACCEPTANCE_TRANSACTION,
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                payload=payload,
            )
        ProjectionEngine(
            ledger,
            configuration.root
            / "state/projects/interview-companion/projection-cache.json",
        ).rebuild()
        exclude = repository / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8")
            + "\n.factory/conveyor-state.json\n.factory/locks/writer.json\n",
            encoding="utf-8",
        )
        write_json(
            repository / ".factory/conveyor-state.json",
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "current_feature": "F009",
                "current_phase": "validation_failed",
                "accepted_feature_commit": None,
            },
        )
        recovery = AcceptedCommitRecovery(
            controller_root=configuration.root,
            configuration=configuration.conveyor,
            project=project,
        )
        constants = {
            "F009_CANDIDATE": candidate,
            "F009_MILESTONE_BASE": base,
            "F009_BRANCH": branch,
            "F009_FEATURE_TRANSACTION": self.FEATURE_TRANSACTION,
            "F009_ACCEPTANCE_TRANSACTION": self.ACCEPTANCE_TRANSACTION,
            "F009_RETAINED_PATHS": retained_paths,
            "F009_RETAINED_DIFF_FINGERPRINT": retained_fingerprint,
        }
        self.assertEqual(metadata_paths, tuple(sorted(metadata_paths)))
        return repository, project, configuration, ledger, recovery, constants

    def _inspect_partial_f009(self, recovery, constants):
        with patch.multiple(
            "development_conveyor.accepted_commit_recovery", **constants
        ):
            return recovery.inspect(
                feature_id="F009",
                candidate_commit=constants["F009_CANDIDATE"],
                milestone_base=constants["F009_MILESTONE_BASE"],
                feature_branch=constants["F009_BRANCH"],
                feature_transaction_id=self.FEATURE_TRANSACTION,
                acceptance_transaction_id=self.ACCEPTANCE_TRANSACTION,
            )

    def _inspect(self, recovery, constants):
        with patch.multiple(
            "development_conveyor.accepted_commit_recovery", **constants
        ):
            return recovery.inspect(
                feature_id="F004",
                candidate_commit=constants["F004_CANDIDATE"],
                milestone_base=constants["F004_MILESTONE_BASE"],
                feature_branch=constants["F004_BRANCH"],
                feature_transaction_id=self.FEATURE_TRANSACTION,
                acceptance_transaction_id=self.ACCEPTANCE_TRANSACTION,
            )

    def test_dry_run_is_non_mutating_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, _, _, ledger, recovery, constants = self._fixture(
                Path(temporary)
            )
            before_head = git(repository, "rev-parse", "HEAD")
            before_ledger = ledger.path.read_bytes()
            before_cache = (
                repository / ".factory/conveyor-state.json"
            ).read_bytes()
            plan = self._inspect(recovery, constants)
            self.assertEqual(plan["model_sessions_planned"], 0)
            self.assertEqual(git(repository, "rev-parse", "HEAD"), before_head)
            self.assertEqual(ledger.path.read_bytes(), before_ledger)
            self.assertEqual(
                (repository / ".factory/conveyor-state.json").read_bytes(),
                before_cache,
            )
            with self.assertRaises(RecoveryError):
                with patch.multiple(
                    "development_conveyor.accepted_commit_recovery", **constants
                ):
                    recovery.inspect(
                        feature_id="F004",
                        candidate_commit=constants["F004_CANDIDATE"],
                        milestone_base="0" * 40,
                        feature_branch=constants["F004_BRANCH"],
                        feature_transaction_id=self.FEATURE_TRANSACTION,
                        acceptance_transaction_id=self.ACCEPTANCE_TRANSACTION,
                    )
            with self.assertRaises(RecoveryError):
                with patch.multiple(
                    "development_conveyor.accepted_commit_recovery", **constants
                ):
                    recovery.inspect(
                        feature_id="F004",
                        candidate_commit=constants["F004_CANDIDATE"],
                        milestone_base=constants["F004_MILESTONE_BASE"],
                        feature_branch="codex/F004-wrong-branch",
                        feature_transaction_id=self.FEATURE_TRANSACTION,
                        acceptance_transaction_id=self.ACCEPTANCE_TRANSACTION,
                    )

            ledger.append(
                event_type="TransactionStarted",
                transaction_id="unexpected-ledger-change",
                workflow_type=WorkflowType.RECOVERY,
                payload={},
            )
            with patch.multiple(
                "development_conveyor.accepted_commit_recovery", **constants
            ):
                with self.assertRaisesRegex(
                    RecoveryError, "recovery preflight failed"
                ):
                    recovery.apply(plan)
            self.assertEqual(git(repository, "rev-parse", "HEAD"), before_head)
            self.assertEqual(
                (repository / ".factory/conveyor-state.json").read_bytes(),
                before_cache,
            )

    def test_recovery_reconstructs_f004_without_model_or_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                ledger,
                recovery,
                constants,
            ) = self._fixture(Path(temporary))
            plan = self._inspect(recovery, constants)
            with patch.multiple(
                "development_conveyor.accepted_commit_recovery", **constants
            ):
                result = recovery.apply(plan)
            accepted = result["finalized_accepted_commit"]
            self.assertNotEqual(accepted, constants["F004_CANDIDATE"])
            self.assertEqual(
                git(repository, "rev-parse", f"{accepted}^"),
                constants["F004_MILESTONE_BASE"],
            )
            self.assertEqual(
                git(repository, "rev-parse", constants["F004_BRANCH"]),
                accepted,
            )
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["model_sessions_launched"], 0)
            self.assertEqual(result["child_sessions_launched"], 0)
            self.assertFalse(result["milestone_integration_performed"])
            self.assertEqual(result["next_action"], "milestone_integration")
            self.assertFalse(
                (repository / ".factory/locks/writer.json").exists()
            )
            self.assertEqual(git(repository, "status", "--porcelain"), "")
            events = ledger.read()
            recovery_events = [
                event
                for event in events
                if event["event_type"] == "RecoveryApplied"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(
                recovery_events[0]["payload"][
                    "candidate_implementation_commit"
                ],
                constants["F004_CANDIDATE"],
            )
            self.assertFalse(
                any(
                    event["workflow_type"] == "milestone_integration"
                    for event in events[len(plan["preserved_event_fingerprints"]):]
                )
            )
            queue = json.loads(
                RepositoryInspector(repository).file_at_commit(
                    accepted, project.queue_location
                )
            )
            feature = next(
                item for item in queue["features"] if item["id"] == "F004"
            )
            self.assertEqual(feature["accepted_commit"], "SELF")
            self.assertEqual(feature["implementation_status"], "Completed")
            self.assertTrue(all(feature["acceptance"].values()))
            consistency = ConsistencyChecker(
                controller_root=recovery.controller_root,
                project=project,
            ).check()
            immutable = next(
                item
                for item in consistency["invariants"]
                if item["invariant"] == "accepted_commit_immutability"
            )
            reconstruction = next(
                item
                for item in consistency["invariants"]
                if item["invariant"]
                == "accepted_commit_reconstruction_evidence"
            )
            self.assertTrue(immutable["passed"])
            self.assertTrue(reconstruction["passed"])

    def test_exact_f009_partial_prefix_dry_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                _,
                _,
                ledger,
                recovery,
                constants,
            ) = self._partial_f009_fixture(Path(temporary))
            inspector = RepositoryInspector(repository)
            before = {
                "head": inspector.head,
                "diff": inspector.planning_diff(),
                "ledger": ledger.path.read_bytes(),
                "projection": recovery.projection.cache_path.read_bytes(),
                "cache": (
                    repository / ".factory/conveyor-state.json"
                ).read_bytes(),
            }
            plan = self._inspect_partial_f009(recovery, constants)
            self.assertEqual(plan["model_sessions_planned"], 0)
            self.assertEqual(plan["child_sessions_planned"], 0)
            self.assertEqual(
                tuple(plan["retained_metadata_paths"]),
                constants["F009_RETAINED_PATHS"],
            )
            self.assertEqual(inspector.head, before["head"])
            self.assertEqual(inspector.planning_diff(), before["diff"])
            self.assertEqual(ledger.path.read_bytes(), before["ledger"])
            self.assertEqual(
                recovery.projection.cache_path.read_bytes(),
                before["projection"],
            )
            self.assertEqual(
                (repository / ".factory/conveyor-state.json").read_bytes(),
                before["cache"],
            )

    def test_exact_f009_partial_prefix_recovers_one_direct_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                repository,
                project,
                _,
                _,
                recovery,
                constants,
            ) = self._partial_f009_fixture(Path(temporary))
            plan = self._inspect_partial_f009(recovery, constants)
            with patch.multiple(
                "development_conveyor.accepted_commit_recovery", **constants
            ):
                result = recovery.apply(plan)
            accepted = result["finalized_accepted_commit"]
            inspector = RepositoryInspector(repository)
            self.assertEqual(
                inspector.rev_parse(f"{accepted}^"),
                constants["F009_MILESTONE_BASE"],
            )
            self.assertEqual(
                inspector.rev_parse(constants["F009_BRANCH"]), accepted
            )
            self.assertEqual(inspector.head, accepted)
            self.assertTrue(inspector.is_clean)
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["model_sessions_launched"], 0)
            self.assertEqual(result["child_sessions_launched"], 0)
            self.assertFalse(result["milestone_integration_performed"])
            self.assertEqual(result["outcome"], "integration_ready")
            self.assertEqual(
                result["projection"]["current_feature"], "F009"
            )
            self.assertEqual(
                result["projection"]["accepted_feature_commit"], accepted
            )
            self.assertEqual(
                git(repository, "cat-file", "-t", constants["F009_CANDIDATE"]),
                "commit",
            )
            for relative, expected_hash in plan["source_test_hashes"].items():
                self.assertEqual(
                    recovery._blob_hash(accepted, relative), expected_hash
                )
            queue = json.loads(
                inspector.file_at_commit(accepted, project.queue_location)
            )
            feature = next(
                item for item in queue["features"] if item["id"] == "F009"
            )
            self.assertEqual(feature["accepted_commit"], "SELF")

    def test_f009_partial_extra_or_altered_paths_fail_closed(self):
        mutations = {
            "extra": (
                "Sources/App/Transcription.swift",
                "protocol TranscriptionEngine {}\n// altered source\n",
            ),
            "altered": (
                "docs/FEATURE_CATALOG.md",
                "\nmalformed retained suffix\n",
            ),
        }
        for name, (relative, content) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                (
                    repository,
                    _,
                    _,
                    _,
                    recovery,
                    constants,
                ) = self._partial_f009_fixture(Path(temporary))
                target = repository / relative
                if name == "altered":
                    target.write_text(
                        target.read_text(encoding="utf-8") + content,
                        encoding="utf-8",
                    )
                else:
                    target.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(
                    RecoveryError, "recovery preflight failed"
                ):
                    self._inspect_partial_f009(recovery, constants)
                self.assertEqual(
                    RepositoryInspector(repository).head,
                    constants["F009_CANDIDATE"],
                )
