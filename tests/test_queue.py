import tempfile
import unittest
from pathlib import Path

from development_conveyor.queue import FeatureQueue
from tests.helpers import synthetic_repository


class QueueTests(unittest.TestCase):
    def test_selects_ready_feature_with_documented_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary))
            queue = FeatureQueue.from_path(repository / project.queue_location)
            selection = queue.select_next("M0")
            self.assertEqual(selection.feature_id, "F001")
            self.assertIn("queue order", selection.reason)

    def test_proposed_feature_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(Path(temporary), feature_status="proposed")
            queue = FeatureQueue.from_path(repository / project.queue_location)
            self.assertIsNone(queue.select_next("M0"))
            self.assertFalse(queue.milestone_complete("M0"))

