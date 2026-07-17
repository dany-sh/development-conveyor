import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from development_conveyor.errors import AmbiguousLockError, LockError
from development_conveyor.locks import DurableLock, make_lock_record


class LockTests(unittest.TestCase):
    def test_concurrent_second_start_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = DurableLock(Path(temporary) / "writer.json")
            record = make_lock_record(project_id="p", repository_identity="repo", run_id="run-1", current_feature="F1", current_phase="feature")
            winners = []
            barrier = threading.Barrier(2)

            def acquire(run_id):
                barrier.wait()
                candidate = dict(record, run_id=run_id)
                try:
                    lock.acquire(candidate)
                    winners.append(run_id)
                except LockError:
                    pass

            threads = [threading.Thread(target=acquire, args=(f"run-{index}",)) for index in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(winners), 1)

    def test_stale_recovery_requires_identity_and_dead_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "writer.json"
            path.write_text(json.dumps({
                "project_id": "p", "repository_identity": "repo", "run_id": "old",
                "process_id": 99999999, "host_identity": socket.gethostname(),
                "start_time": "2026-01-01T00:00:00+00:00", "last_confirmed_time": "2026-01-01T00:00:00+00:00",
                "current_feature": "F1", "current_phase": "feature",
            }), encoding="utf-8")
            lock = DurableLock(path)
            with self.assertRaises(AmbiguousLockError):
                lock.recover_stale(expected_repository_identity="wrong")
            evidence = lock.recover_stale(expected_repository_identity="repo")
            self.assertFalse(path.exists())
            self.assertFalse(evidence["timestamp_alone_used"])
            self.assertTrue(Path(evidence["recovered_lock"]).exists())

    def test_live_lock_is_never_recovered_by_age(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "writer.json"
            path.write_text(json.dumps({
                "project_id": "p", "repository_identity": "repo", "run_id": "live",
                "process_id": os.getpid(), "host_identity": socket.gethostname(),
                "start_time": "2000-01-01T00:00:00+00:00", "last_confirmed_time": "2000-01-01T00:00:00+00:00",
                "current_feature": "F1", "current_phase": "feature",
            }), encoding="utf-8")
            with self.assertRaises(AmbiguousLockError):
                DurableLock(path).recover_stale(expected_repository_identity="repo")

