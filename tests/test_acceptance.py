from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from development_conveyor.acceptance import (
    accept_feature,
    inspect_acceptance_candidate,
    tier_evidence_from_records,
)
from development_conveyor.contracts import MutationPolicy, WorkflowType, WORKFLOW_LEASE
from development_conveyor.errors import IntegrationPlanError, LockError, TransactionError
from development_conveyor.integration_executor import _git_mutation, inspect_two_refs
from development_conveyor.ledger import EvidenceLedger
from development_conveyor.repository import RepositoryInspector
from development_conveyor.snapshots import capture_repository_snapshot
from development_conveyor.validation_tiers import reparse_release_artifacts
from development_conveyor.workflow_lease import WorkflowWriterLease
from tests.helpers import git, synthetic_repository, write_json


class CollapseAcceptanceCeremonyTests(unittest.TestCase):
    def _fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        git(repository, "switch", "-c", "codex/f001-preparation")
        (repository / "docs/features/F001.md").write_text(
            "# F001\n\n- Factory status: Prepared\n", encoding="utf-8"
        )
        git(repository, "add", "docs/features/F001.md")
        git(repository, "commit", "-m", "docs: prepare F001")
        branch = "codex/f001-synthetic-feature"
        git(repository, "switch", "-c", branch)
        (repository / "app.txt").write_text(
            "baseline\nimmutable implementation\n", encoding="utf-8"
        )
        git(repository, "add", "app.txt")
        git(repository, "commit", "-m", "F001: immutable implementation")
        candidate = git(repository, "rev-parse", "HEAD")
        tree = git(repository, "rev-parse", "HEAD^{tree}")
        base = git(repository, "rev-parse", "codex/m0-foundation")
        evidence = tier_evidence_from_records(
            project=project,
            implementation_commit=candidate,
            tier="feature",
            commands=[
                {
                    "argv": ["synthetic-feature-check"],
                    "exit_status": 0,
                    "group": "feature_tests",
                }
            ],
            valid=True,
        )
        return repository, project, branch, base, candidate, tree, evidence

    def _linked_fixture(self, root: Path):
        repository, project = synthetic_repository(root)
        linked = root / "linked-feature"
        branch = "codex/f001-linked-feature"
        git(
            repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(linked),
            "codex/m0-foundation",
        )
        (linked / "app.txt").write_text(
            "baseline\nlinked immutable implementation\n", encoding="utf-8"
        )
        git(linked, "add", "app.txt")
        git(linked, "commit", "-m", "F001: linked immutable implementation")
        linked_project = replace(project, repository=linked.resolve())
        candidate = git(linked, "rev-parse", "HEAD")
        tree = git(linked, "rev-parse", "HEAD^{tree}")
        base = git(linked, "rev-parse", "codex/m0-foundation")
        evidence = tier_evidence_from_records(
            project=linked_project,
            implementation_commit=candidate,
            tier="feature",
            commands=[
                {
                    "argv": ["synthetic-feature-check"],
                    "exit_status": 0,
                    "group": "feature_tests",
                }
            ],
            valid=True,
        )
        return (
            repository,
            linked,
            linked_project,
            branch,
            base,
            candidate,
            tree,
            evidence,
        )

    def test_acceptance_keeps_implementation_ref_and_stores_ledger_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            result = accept_feature(
                controller_root=root / "controller",
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                implementation_commit=candidate,
                implementation_tree=tree,
                tier_evidence=evidence,
                run_id="accept-f001",
            )
            self.assertEqual(candidate, git(repository, "rev-parse", branch))
            self.assertEqual(tree, git(repository, "rev-parse", f"{branch}^{{tree}}"))
            self.assertTrue(result["implementation_ref_unchanged"])
            self.assertEqual(
                "controller_evidence_ledger",
                result["acceptance_metadata"]["surface"],
            )
            self.assertEqual(0, result["complete_suite_invocations"])
            self.assertEqual(0, result["release_validation_invocations"])
            self.assertFalse((repository / ".factory/locks/writer.json").exists())

    def test_linked_worktree_acceptance_records_exact_worktree_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                repository,
                linked,
                project,
                branch,
                base,
                candidate,
                tree,
                evidence,
            ) = self._linked_fixture(root)
            primary_identity = RepositoryInspector(repository).identity()
            linked_identity = RepositoryInspector(linked).identity()

            result = accept_feature(
                controller_root=root / "controller",
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                implementation_commit=candidate,
                implementation_tree=tree,
                tier_evidence=evidence,
                run_id="accept-linked-f001",
            )

            self.assertEqual(
                primary_identity["repository_id"],
                linked_identity["repository_id"],
            )
            self.assertNotEqual(
                primary_identity["path_fingerprint"],
                linked_identity["path_fingerprint"],
            )
            self.assertEqual(
                str(linked.resolve()),
                result["acceptance_metadata"]["invoking_worktree"],
            )
            self.assertEqual(
                linked_identity["path_fingerprint"],
                result["acceptance_metadata"]["invoking_worktree_fingerprint"],
            )
            self.assertEqual(candidate, git(linked, "rev-parse", branch))
            self.assertFalse(
                (repository / ".factory/locks/writer.json").exists()
            )

    def test_different_worktree_cannot_release_linked_worktree_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project = synthetic_repository(root)
            owner = root / "lease-owner"
            foreign = root / "lease-foreign"
            git(
                repository,
                "worktree",
                "add",
                "-b",
                "codex/lease-owner",
                str(owner),
                "codex/m0-foundation",
            )
            git(
                repository,
                "worktree",
                "add",
                "-b",
                "codex/lease-foreign",
                str(foreign),
                "codex/m0-foundation",
            )
            owner_inspector = RepositoryInspector(owner)
            foreign_inspector = RepositoryInspector(foreign)
            owner_identity = owner_inspector.identity()
            foreign_identity = foreign_inspector.identity()
            lease = WorkflowWriterLease(owner_inspector.writer_lock_path())
            policy = MutationPolicy(())
            record = lease.acquire(
                lease_type=WORKFLOW_LEASE[WorkflowType.FEATURE_ACCEPTANCE],
                repository_identity=owner_identity["repository_id"],
                repository_path_fingerprint=owner_identity["path_fingerprint"],
                repository_path=owner,
                project_id=project.project_id,
                transaction_id="linked-owner-transaction",
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                milestone=project.active_milestone,
                feature_id="F001",
                starting_branch="codex/lease-owner",
                starting_head=owner_inspector.head,
                run_id="linked-owner-run",
                session_id=None,
                policy=policy,
            )
            try:
                with self.assertRaisesRegex(
                    LockError, "repository_path_fingerprint|repository_path"
                ):
                    lease.release(
                        transaction_id=record.transaction_id,
                        workflow_type=record.workflow_type,
                        repository_identity=foreign_identity["repository_id"],
                        project_id=record.project_id,
                        repository_path_fingerprint=foreign_identity[
                            "path_fingerprint"
                        ],
                        repository_path=foreign,
                        lease_id=record.lease_id,
                        milestone=record.milestone,
                        feature_id=record.feature_id,
                        starting_branch=record.starting_branch,
                        starting_head=record.starting_head,
                        run_id=record.run_id,
                        session_id=record.session_id,
                        policy=policy,
                    )
                self.assertIsNotNone(lease.read())
            finally:
                if lease.read() is not None:
                    lease.release(
                        transaction_id=record.transaction_id,
                        workflow_type=record.workflow_type,
                        repository_identity=record.repository_identity,
                        project_id=record.project_id,
                        repository_path_fingerprint=record.repository_path_fingerprint,
                        repository_path=owner,
                        lease_id=record.lease_id,
                        milestone=record.milestone,
                        feature_id=record.feature_id,
                        starting_branch=record.starting_branch,
                        starting_head=record.starting_head,
                        run_id=record.run_id,
                        session_id=record.session_id,
                        policy=policy,
                    )

    def test_acceptance_supersedes_exact_start_only_failure_before_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            inspector = RepositoryInspector(repository)
            identity = inspector.identity()
            ledger = EvidenceLedger(
                root
                / "controller/state/projects/synthetic/evidence-ledger.jsonl",
                project_id=project.project_id,
                repository_identity=identity["repository_id"],
                repository_path_fingerprint=identity["path_fingerprint"],
            )
            snapshot = capture_repository_snapshot(project)
            failed_transaction = "failed-start-only-acceptance"
            ledger.append(
                event_type="TransactionStarted",
                transaction_id=failed_transaction,
                workflow_type=WorkflowType.FEATURE_ACCEPTANCE,
                payload={
                    "run_id": "failed-linked-run",
                    "milestone": project.active_milestone,
                    "feature_id": "F001",
                    "starting_branch": branch,
                    "starting_head": candidate,
                    "starting_queue_fingerprint": snapshot.queue_fingerprint,
                    "starting_tracked_diff_fingerprint": (
                        snapshot.tracked_diff_fingerprint
                    ),
                    "starting_untracked_fingerprint": (
                        snapshot.untracked_fingerprint
                    ),
                    "allowed_mutation_policy": MutationPolicy(()).to_dict(),
                },
            )

            result = accept_feature(
                controller_root=root / "controller",
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                implementation_commit=candidate,
                implementation_tree=tree,
                tier_evidence=evidence,
                run_id="accept-retry-f001",
                recover_incomplete_transaction=failed_transaction,
            )

            failed_events = [
                event
                for event in ledger.read()
                if event["transaction_id"] == failed_transaction
            ]
            self.assertEqual(
                ["TransactionStarted", "TransactionSuperseded", "RecoveryApplied"],
                [event["event_type"] for event in failed_events],
            )
            self.assertEqual(
                "linked_worktree_lease_identity_validation",
                failed_events[1]["payload"]["failure_reason"],
            )
            self.assertFalse(
                failed_events[1]["payload"]["acceptance_record_created"]
            )
            self.assertEqual("accepted", result["status"])
            self.assertEqual("pending", result["integration_status"])
            self.assertEqual(
                failed_transaction,
                result["recovered_incomplete_acceptance"]["transaction_id"],
            )

    def test_tree_ref_and_changed_path_drift_fail_before_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            for label, kwargs in (
                ("tree", {"implementation_tree": "0" * 40}),
                (
                    "paths",
                    {
                        "implementation_tree": tree,
                        "tier_evidence": {
                            **evidence,
                            "changed_paths": ["another.txt"],
                        },
                    },
                ),
            ):
                with self.subTest(label=label):
                    with self.assertRaises(TransactionError):
                        inspect_acceptance_candidate(
                            project=project,
                            feature_id="F001",
                            feature_branch=branch,
                            milestone_base=base,
                            implementation_commit=candidate,
                            implementation_tree=kwargs["implementation_tree"],
                            tier_evidence=kwargs.get("tier_evidence", evidence),
                        )
            self.assertFalse((root / "controller/state").exists())
            self.assertEqual(candidate, git(repository, "rev-parse", branch))

    def test_foreign_identity_and_complete_suite_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, branch, base, candidate, tree, evidence = self._fixture(root)
            cases = (
                {**evidence, "repository_identity": "foreign"},
                {**evidence, "complete_suite_invocations": 1},
                {**evidence, "release_validation_invocations": 1},
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(
                        TransactionError, "tier evidence failed authentication"
                    ):
                        inspect_acceptance_candidate(
                            project=project,
                            feature_id="F001",
                            feature_branch=branch,
                            milestone_base=base,
                            implementation_commit=candidate,
                            implementation_tree=tree,
                            tier_evidence=changed,
                        )

    def test_duplicate_acceptance_fails_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, branch, base, candidate, tree, evidence = self._fixture(root)
            arguments = {
                "controller_root": root / "controller",
                "project": project,
                "feature_id": "F001",
                "feature_branch": branch,
                "milestone_base": base,
                "implementation_commit": candidate,
                "implementation_tree": tree,
                "tier_evidence": evidence,
                "run_id": "accept-f001",
            }
            accept_feature(**arguments)
            with self.assertRaisesRegex(TransactionError, "already has immutable"):
                accept_feature(**{**arguments, "run_id": "duplicate-f001"})

    def test_dependencies_are_cumulative_across_completed_milestones(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            queue_path = repository / project.queue_location
            git(repository, "switch", "codex/m0-foundation")
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["milestones"].insert(
                0,
                {
                    "id": "M-1",
                    "name": "Earlier",
                    "status": "completed",
                    "base_commit": base,
                    "integration_branch": "codex/m-1",
                    "integrated_features": ["F000"],
                    "last_validated_commit": base,
                    "human_gate": True,
                },
            )
            queue["features"].insert(
                0,
                {
                    "id": "F000",
                    "title": "Earlier dependency",
                    "status": "integrated",
                    "priority": 1,
                    "milestone": "M-1",
                    "dependencies": [],
                    "spec": "docs/features/F001.md",
                    "acceptance_criteria": ["Synthetic"],
                    "requires_human_decision": False,
                    "branch": "codex/f000",
                    "integration_base_commit": base,
                    "accepted_commit": base,
                    "integrated_commit": base,
                    "integration_status": "passed",
                    "integration_fix_commits": [],
                },
            )
            queue["features"][1]["dependencies"] = ["F000"]
            write_json(queue_path, queue)
            git(repository, "add", project.queue_location)
            git(repository, "commit", "-m", "factory: record earlier milestone")
            live_base = git(repository, "rev-parse", "HEAD")
            git(repository, "switch", branch)
            git(repository, "rebase", "--onto", live_base, base, branch)
            candidate = git(repository, "rev-parse", "HEAD")
            tree = git(repository, "rev-parse", "HEAD^{tree}")
            evidence = tier_evidence_from_records(
                project=project,
                implementation_commit=candidate,
                tier="feature",
                commands=[
                    {
                        "argv": ["check"],
                        "exit_status": 0,
                        "group": "feature_tests",
                    }
                ],
                valid=True,
            )
            authority = accept_feature(
                controller_root=root / "controller",
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=live_base,
                implementation_commit=candidate,
                implementation_tree=tree,
                tier_evidence=evidence,
                run_id="accept-cumulative",
            )["acceptance_metadata"]
            inspected = inspect_two_refs(
                repository=repository,
                controller_project_id=project.project_id,
                feature_id="F001",
                feature_branch=branch,
                accepted_commit=candidate,
                milestone_id="M0",
                milestone_branch="codex/m0-foundation",
                pre_integration_head=live_base,
                queue_path=project.queue_location,
                acceptance_metadata=authority,
            )
            self.assertEqual(["F000"], inspected["dependencies"])

    def test_active_milestone_only_dependency_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            authority = accept_feature(
                controller_root=root / "controller",
                project=project,
                feature_id="F001",
                feature_branch=branch,
                milestone_base=base,
                implementation_commit=candidate,
                implementation_tree=tree,
                tier_evidence=evidence,
                run_id="accept-invalid-dependency",
            )["acceptance_metadata"]
            authority["accepted_changed_paths"] = ["wrong.txt"]
            authority_without_fingerprint = dict(authority)
            authority_without_fingerprint.pop("metadata_fingerprint")
            from development_conveyor.contracts import fingerprint

            authority["metadata_fingerprint"] = fingerprint(
                authority_without_fingerprint
            )
            with self.assertRaisesRegex(
                IntegrationPlanError, "changed paths disagree"
            ):
                inspect_two_refs(
                    repository=repository,
                    controller_project_id=project.project_id,
                    feature_id="F001",
                    feature_branch=branch,
                    accepted_commit=candidate,
                    milestone_id="M0",
                    milestone_branch="codex/m0-foundation",
                    pre_integration_head=base,
                    queue_path=project.queue_location,
                    acceptance_metadata=authority,
                )

    def test_preserved_raw_evidence_reparses_without_suite_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, _, _, _, candidate, tree, _ = self._fixture(root)
            reports = repository / "reports"
            reports.mkdir()
            stdout_path = reports / "release-validation-1.stdout.raw"
            stderr_path = reports / "release-validation-1.stderr.raw"
            metadata_path = reports / "release-validation-1.execution.json"
            stdout = b"test_ok (tests.test_example.Example) ... ok\nRan 1 test\nOK\n"
            stderr = b""
            stdout_path.write_bytes(stdout)
            stderr_path.write_bytes(stderr)
            provenance = {
                "repository_path": str(repository.resolve()),
                "branch": git(repository, "branch", "--show-current"),
                "commit": candidate,
                "tree": tree,
                "worktree_clean": True,
                "changed_paths": [],
                "observed_at": "2026-07-29T00:00:00+00:00",
            }
            metadata = {
                "schema_version": 1,
                "execution_id": "1",
                "group": "release_complete_suite_1",
                "raw_stdout_path": stdout_path.relative_to(repository).as_posix(),
                "raw_stderr_path": stderr_path.relative_to(repository).as_posix(),
                "metadata_path": metadata_path.relative_to(repository).as_posix(),
                "child_exit_code": 0,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "completion_state": "parser_failed",
                "parser_status": "failed",
                "created_at": "2026-07-29T00:00:00+00:00",
                "starting_provenance": provenance,
                "final_provenance": provenance,
                "provenance_valid": True,
                "child_completed_at": "2026-07-29T00:00:01+00:00",
                "parser_completed_at": "2026-07-29T00:00:01+00:00",
                "completed_at": "2026-07-29T00:00:01+00:00",
            }
            write_json(metadata_path, metadata)
            result = reparse_release_artifacts(
                repository,
                execution_metadata_path=metadata_path,
                raw_stdout_path=stdout_path,
                raw_stderr_path=stderr_path,
                implementation_commit=candidate,
            )
            self.assertTrue(result["raw_artifacts_reused"])
            self.assertEqual(0, result["suite_invocations"])
            self.assertEqual(1, result["record"]["tests_run"])

    def test_application_identity_disagreement_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, project, branch, base, candidate, tree, evidence = (
                self._fixture(root)
            )
            adapter = json.loads(
                (repository / ".factory/project.yaml").read_text(encoding="utf-8")
            )
            adapter["project"]["id"] = "foreign"
            write_json(repository / ".factory/project.yaml", adapter)
            git(repository, "add", ".factory/project.yaml")
            git(repository, "commit", "--amend", "--no-edit")
            candidate = git(repository, "rev-parse", "HEAD")
            tree = git(repository, "rev-parse", "HEAD^{tree}")
            evidence["implementation"] = {"commit": candidate, "tree": tree}
            evidence["changed_paths"] = list(
                RepositoryInspector(repository).changed_paths(candidate)
            )
            with self.assertRaisesRegex(
                TransactionError, "application identity disagrees"
            ):
                inspect_acceptance_candidate(
                    project=project,
                    feature_id="F001",
                    feature_branch=branch,
                    milestone_base=base,
                    implementation_commit=candidate,
                    implementation_tree=tree,
                    tier_evidence=evidence,
                )

    def test_linear_preparation_range_integrates_the_exact_candidate_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, _, _, base, candidate, tree, _ = self._fixture(root)
            git(repository, "switch", "codex/m0-foundation")
            plan = {
                "milestone_branch": "codex/m0-foundation",
                "accepted_commit": candidate,
                "pre_integration_head": base,
                "feature_id": "F001",
                "metadata_paths": [],
            }
            _git_mutation(
                repository,
                plan,
                ["cherry-pick", "--no-commit", f"{base}..{candidate}"],
            )
            _git_mutation(
                repository,
                plan,
                ["commit", "-m", "factory: integrate F001 implementation"],
            )
            self.assertEqual(tree, git(repository, "rev-parse", "HEAD^{tree}"))
            self.assertEqual(base, git(repository, "rev-parse", "HEAD^"))
