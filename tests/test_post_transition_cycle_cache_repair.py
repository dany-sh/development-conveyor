from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from development_conveyor.config import load_configuration
from development_conveyor.contracts import fingerprint
from development_conveyor.cycle_cache_repair import CycleCacheRepair
from development_conveyor.locks import DurableLock
from development_conveyor.registry import ProjectRegistry
from tests.helpers import git, write_json


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "interview-companion"
FEATURE = "F097"
FEATURE_BRANCH = "codex/F097-imported-audio-transcription-workflow"
ACCEPTED = "560214ec85d149a46029976987e3ac5604c54217"
MILESTONE_BRANCH = "codex/m0-foundation"
MILESTONE_HEAD = "4eb4d69b5918644c6291cc466cac0678c1e9715a"
INTEGRATED = "82dc61fabe5ec09dca940f626dad06a543bb448d"
PRE_INTEGRATION = "65c32f8f10d6569dafbcdee9a0fe0a230e0ecfba"
PREVIOUS_TRANSACTION = "f5b7f4e8-ba7d-48f5-a6fc-03348000fc25"
INTEGRATION_TRANSACTION = "22d297a3-628a-4cc8-af18-bd9022929621"
REPOSITORY_ID = "9022b4a8018866b7a9c0862efb2b8664b7e0288afd5e3fbd22b107fa640ab299"
PATH_FINGERPRINT = "dd395b40fd8c69fffcde93bf15963740d4f914c01bffc69cd8d47a46c8d908e2"
OLD_LEDGER_FINGERPRINT = "0a9638c7762b2f16188883c50c50d584e973ea1ead10051d64ab0127f17ca8c0"
OLD_PROJECTION_FINGERPRINT = "922f90cf2e105827d1b8fa6aa25f4293eae119663361b171659f3729e2c0e28c"


class _Inspector:
    def __init__(self, root: Path):
        self.root = root
        self.current_branch = MILESTONE_BRANCH
        self.head = MILESTONE_HEAD
        self.is_clean = True

    @staticmethod
    def identity():
        return {
            "repository_id": REPOSITORY_ID,
            "path_fingerprint": PATH_FINGERPRINT,
        }

    @staticmethod
    def git_operation_state():
        return {
            "cherry_pick": False,
            "merge": False,
            "rebase_merge": False,
            "rebase_apply": False,
        }

    def writer_lock_path(self, relative: str) -> Path:
        return self.root / relative

    @staticmethod
    def git(arguments, check=True):
        if arguments[:2] == ["ls-files", "--error-unmatch"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if arguments[:2] == ["check-ignore", "-q"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if arguments[:2] == ["status", "--porcelain=v1"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"## {MILESTONE_BRANCH}\n",
                stderr="",
            )
        raise AssertionError(f"unexpected Git command: {arguments}")


class PostTransitionCycleCacheRepairTests(unittest.TestCase):
    def _fixture(self, root: Path):
        application = root / "application"
        (application / ".factory").mkdir(parents=True)
        (application / "docs").mkdir()
        write_json(
            application / "docs/FEATURE_QUEUE.yaml",
            {
                "schema_version": 1,
                "milestones": [
                    {
                        "id": "M0",
                        "name": "Foundation",
                        "status": "active",
                        "base_commit": PRE_INTEGRATION,
                        "integration_branch": MILESTONE_BRANCH,
                        "integrated_features": [FEATURE],
                        "last_validated_commit": MILESTONE_HEAD,
                        "human_gate": True,
                    }
                ],
                "features": [],
            },
        )
        git(application, "init", "-b", MILESTONE_BRANCH)
        git(application, "config", "user.name", "Synthetic Conveyor")
        git(application, "config", "user.email", "synthetic@example.invalid")
        git(application, "add", ".")
        git(application, "commit", "-m", "synthetic post-transition fixture")
        configuration = load_configuration(ROOT)
        registered = ProjectRegistry(configuration).get(PROJECT_ID)
        project = replace(registered, repository=application)
        state_root = root / "state"
        (state_root / "integration-plans").mkdir(parents=True)
        source_root = ROOT / "state/projects" / PROJECT_ID
        for name in (
            "evidence-ledger.jsonl",
            "evidence-ledger.jsonl.head",
            "projection-cache.json",
        ):
            shutil.copy2(source_root / name, state_root / name)
        shutil.copy2(
            source_root
            / "integration-plans"
            / f"{INTEGRATION_TRANSACTION}.json",
            state_root
            / "integration-plans"
            / f"{INTEGRATION_TRANSACTION}.json",
        )
        stale = {
            "schema_version": 1,
            "conveyor_run_id": "feature-recovery-256e4092-9408-4fcc-be6e-628d9d7b1ccf",
            "project_id": PROJECT_ID,
            "repository_identity": {
                "repository_id": REPOSITORY_ID,
                "path_fingerprint": PATH_FINGERPRINT,
            },
            "repository_path_fingerprint": PATH_FINGERPRINT,
            "active_milestone": "M0",
            "current_feature": FEATURE,
            "selected_feature": None,
            "feature_dependencies": ["F005", "F008", "F009"],
            "feature_branch": FEATURE_BRANCH,
            "feature_worktree": None,
            "feature_starting_commit": PRE_INTEGRATION,
            "accepted_feature_commit": ACCEPTED,
            "milestone_branch": MILESTONE_BRANCH,
            "milestone_pre_integration_commit": PRE_INTEGRATION,
            "milestone_post_integration_commit": None,
            "current_phase": "integration_pending",
            "writer_lock_identity": None,
            "validation_attempts": [],
            "review_attempts": [],
            "integration_attempts": [],
            "last_successful_checkpoint": "feature_result_recovery_terminal",
            "last_verified_git_state": {
                "branch": FEATURE_BRANCH,
                "head": ACCEPTED,
                "clean": True,
            },
            "integration_status": "pending",
            "next_safe_action": "milestone_integration",
            "stop_reason": None,
            "human_decision_required": None,
            "resume_instructions": (
                "scripts/conveyor run --project interview-companion --mode milestone"
            ),
            "session_id": None,
            "kernel_transaction_id": PREVIOUS_TRANSACTION,
            "kernel_ledger_sequence": 497,
            "kernel_ledger_fingerprint": OLD_LEDGER_FINGERPRINT,
            "kernel_projection_fingerprint": OLD_PROJECTION_FINGERPRINT,
            "created_at": "2026-07-24T22:04:47+00:00",
            "updated_at": "2026-07-24T22:48:18+00:00",
        }
        stale["kernel_cache_fingerprint"] = fingerprint(stale)
        cycle_path = application / ".factory/conveyor-state.json"
        write_json(cycle_path, stale)
        controller_cache = root / "controller-cache.json"
        write_json(controller_cache, {"schema_version": 1})
        repair = CycleCacheRepair(
            controller_root=ROOT,
            configuration=configuration,
            project=project,
        )
        repair.inspector = _Inspector(application)
        repair.state_root = state_root
        repair.application_cache_path = cycle_path
        repair.controller_cache_path = controller_cache
        repair._launch_reservation = lambda repository_path_fingerprint=None: DurableLock(
            root / "runtime-cache-repair-reservation.json"
        )
        return repair, cycle_path, state_root

    def test_exact_f097_transition_dry_run_and_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            repair, cycle_path, state_root = self._fixture(Path(temporary))
            before = cycle_path.read_bytes()
            ledger_before = (state_root / "evidence-ledger.jsonl").read_bytes()
            projection_before = (state_root / "projection-cache.json").read_bytes()
            plan = repair.inspect()
            self.assertEqual("application_cycle_cache", plan["cache_kind"])
            self.assertEqual("repair_ready", plan["outcome"])
            self.assertEqual(497, plan["old_ledger_sequence"])
            self.assertEqual(508, plan["new_ledger_sequence"])
            self.assertEqual(
                INTEGRATION_TRANSACTION,
                plan["completed_integration_transaction"],
            )
            self.assertEqual(INTEGRATED, plan["integrated_implementation_commit"])
            self.assertEqual(MILESTONE_HEAD, plan["terminal_milestone_head"])
            self.assertEqual([".factory/conveyor-state.json"], plan["future_mutations"])
            self.assertEqual([], plan["application_git_tracked_paths_will_change"])
            self.assertEqual(0, plan["model_sessions_that_would_launch"])
            self.assertEqual(0, plan["child_sessions_that_would_launch"])
            self.assertEqual(before, cycle_path.read_bytes())

            result = repair.apply(plan)
            self.assertEqual("repaired", result["outcome"])
            self.assertEqual(ledger_before, (state_root / "evidence-ledger.jsonl").read_bytes())
            self.assertEqual(
                projection_before,
                (state_root / "projection-cache.json").read_bytes(),
            )
            replacement = json.loads(cycle_path.read_text(encoding="utf-8"))
            self.assertEqual(508, replacement["kernel_ledger_sequence"])
            self.assertEqual(
                INTEGRATION_TRANSACTION, replacement["kernel_transaction_id"]
            )
            self.assertEqual("queue_reconciliation", replacement["current_phase"])
            self.assertIsNone(replacement["current_feature"])
            self.assertEqual("already_canonical", repair.inspect()["outcome"])

    def test_unrelated_branch_and_changed_hash_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repair, cycle_path, _ = self._fixture(Path(temporary))
            repair.inspector.current_branch = "codex/unrelated"
            with self.assertRaisesRegex(Exception, "terminal repository state"):
                repair.inspect()
            repair.inspector.current_branch = MILESTONE_BRANCH
            plan = repair.inspect()
            changed = json.loads(cycle_path.read_text(encoding="utf-8"))
            changed["updated_at"] = "changed"
            changed["kernel_cache_fingerprint"] = fingerprint(
                {
                    key: value
                    for key, value in changed.items()
                    if key != "kernel_cache_fingerprint"
                }
            )
            write_json(cycle_path, changed)
            with self.assertRaisesRegex(Exception, "evidence changed"):
                repair.apply(plan)


if __name__ == "__main__":
    unittest.main()
