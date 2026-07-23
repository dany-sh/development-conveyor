from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_conveyor.accepted_commit import (
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
    def _candidate_fixture(
        self,
        root: Path,
        *,
        project_id: str = "synthetic",
        feature_id: str = "F001",
        title: str = "Synthetic Feature",
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
            f"# Current Status\n\n- Active feature: {feature_id} — {title} "
            "(implementation complete; controller acceptance pending)\n",
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
