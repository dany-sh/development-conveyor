import tempfile
import unittest
import json
from pathlib import Path

from development_conveyor.queue import FeatureQueue
from tests.helpers import synthetic_repository


class QueueTests(unittest.TestCase):
    def test_selects_ready_feature_with_documented_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            queue = FeatureQueue.from_location(repository, project.queue_location)
            selection = queue.select_next("M0")
            self.assertEqual(selection.feature_id, "F001")
            self.assertIn("queue order", selection.reason)

    def test_proposed_feature_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            queue = FeatureQueue.from_location(repository, project.queue_location)
            self.assertIsNone(queue.select_next("M0"))
            self.assertFalse(queue.milestone_complete("M0"))

    def test_canonical_accepted_commit_wins_over_legacy_commit_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            path = repository / project.queue_location
            document = json.loads(path.read_text())
            document["features"][0]["commit"] = "SELF"
            document["features"][0]["accepted_commit"] = "a" * 40
            path.write_text(json.dumps(document), encoding="utf-8")
            feature = FeatureQueue.from_location(repository, project.queue_location).feature("F001")
            self.assertEqual(feature["accepted_commit"], "a" * 40)
