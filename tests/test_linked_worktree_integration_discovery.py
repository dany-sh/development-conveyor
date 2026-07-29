from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from development_conveyor.errors import ProjectionError
from development_conveyor.integration_discovery import discover_integration_target


MILESTONE_BRANCH = "codex/m2-integration"
FEATURE_BRANCH = "codex/m2-001-feature"


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class LinkedWorktreeIntegrationDiscoveryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, object]:
        root.mkdir(parents=True, exist_ok=True)
        feature = root / "feature"
        target = root / "integration"
        feature.mkdir()
        git(feature, "init", "-b", "main")
        git(feature, "config", "user.name", "Synthetic Test")
        git(feature, "config", "user.email", "synthetic@example.invalid")
        (feature / "base.txt").write_text("base\n", encoding="utf-8")
        git(feature, "add", "base.txt")
        git(feature, "commit", "-m", "base")
        base = git(feature, "rev-parse", "HEAD")
        git(feature, "branch", MILESTONE_BRANCH)
        git(feature, "switch", "-c", FEATURE_BRANCH)
        (feature / "feature.txt").write_text("accepted\n", encoding="utf-8")
        git(feature, "add", "feature.txt")
        git(feature, "commit", "-m", "accepted")
        accepted = git(feature, "rev-parse", "HEAD")
        tree = git(feature, "rev-parse", "HEAD^{tree}")
        git(feature, "worktree", "add", str(target), MILESTONE_BRANCH)
        transaction = "acceptance-transaction"
        events = [
            {
                "event_type": "CommitFinalized",
                "workflow_type": "feature_acceptance",
                "transaction_id": transaction,
                "payload": {
                    "commit": accepted,
                    "tree": tree,
                    "parent": base,
                },
            },
            {
                "event_type": "TransactionCompleted",
                "workflow_type": "feature_acceptance",
                "transaction_id": transaction,
                "payload": {
                    "acceptance_metadata_surface": "controller_evidence_ledger",
                    "feature_id": "M2-001",
                    "accepted_feature_commit": accepted,
                    "accepted_implementation_tree": tree,
                    "implementation_ref": FEATURE_BRANCH,
                    "implementation_ref_unchanged": True,
                    "integration_status": "pending",
                    "invoking_worktree": str(feature),
                },
            },
        ]
        return {
            "feature": feature,
            "target": target,
            "base": base,
            "accepted": accepted,
            "tree": tree,
            "events": events,
        }

    def _discover(self, fixture: dict[str, object]):
        return discover_integration_target(
            repository=fixture["feature"],
            feature_id="M2-001",
            feature_branch=FEATURE_BRANCH,
            accepted_commit=fixture["accepted"],
            milestone_branch=MILESTONE_BRANCH,
            expected_starting_commit=None,
            ledger_events=fixture["events"],
        )

    def test_linked_worktree_discovery_uses_configured_integration_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))

            result = self._discover(fixture)

            self.assertEqual(fixture["accepted"], result.accepted_commit)
            self.assertEqual(fixture["tree"], result.accepted_tree)
            self.assertEqual(
                str(fixture["target"].resolve()), result.integration_worktree
            )
            self.assertEqual(MILESTONE_BRANCH, result.integration_branch)
            self.assertEqual(fixture["base"], result.integration_starting_commit)

    def test_feature_and_integration_worktrees_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            result = self._discover(fixture)
            self.assertNotEqual(
                result.feature_worktree, result.integration_worktree
            )

            drifted = json.loads(json.dumps(fixture["events"]))
            drifted[-1]["payload"]["invoking_worktree"] = str(fixture["target"])
            fixture["events"] = drifted
            with self.assertRaisesRegex(
                ProjectionError, "feature worktree does not match"
            ):
                self._discover(fixture)

    def test_target_branch_and_head_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root / "head-drift")
            target = fixture["target"]
            (target / "drift.txt").write_text("drift\n", encoding="utf-8")
            git(target, "add", "drift.txt")
            git(target, "commit", "-m", "target drift")

            with self.assertRaisesRegex(ProjectionError, "expected_head"):
                self._discover(fixture)

            fixture = self._fixture(root / "dirty-target")
            target = fixture["target"]
            (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ProjectionError, "clean"):
                self._discover(fixture)

    def test_primary_worktree_compatibility_remains_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            feature = fixture["feature"]
            target = fixture["target"]
            git(feature, "worktree", "remove", str(target))
            git(feature, "switch", MILESTONE_BRANCH)

            result = discover_integration_target(
                repository=feature,
                feature_id="M2-001",
                feature_branch=FEATURE_BRANCH,
                accepted_commit=fixture["accepted"],
                milestone_branch=MILESTONE_BRANCH,
                expected_starting_commit=None,
                ledger_events=fixture["events"],
            )

            self.assertEqual(str(feature.resolve()), result.integration_worktree)
            self.assertIsNone(result.feature_worktree)
            self.assertEqual(fixture["base"], result.integration_starting_commit)
