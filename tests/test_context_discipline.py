import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.cost_policy import _queue_reconciliation_context_pack
from tests.helpers import synthetic_repository, write_json


class ContextDisciplineTests(unittest.TestCase):
    def test_queue_reconciliation_narrows_candidate_specs(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, project = synthetic_repository(
                Path(temporary), feature_status="proposed"
            )
            queue_path = repository / project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"] = []
            for index in range(1, 7):
                feature_id = f"F{index:03d}"
                spec = f"docs/features/{feature_id}.md"
                (repository / spec).write_text(
                    f"# {feature_id}\n", encoding="utf-8"
                )
                queue["features"].append(
                    {
                        "id": feature_id,
                        "title": f"Feature {index}",
                        "status": "proposed",
                        "priority": index,
                        "milestone": "M0",
                        "dependencies": [],
                        "spec": spec,
                        "acceptance_criteria": ["bounded"],
                        "requires_human_decision": False,
                        "branch": None,
                        "integration_base_commit": None,
                        "accepted_commit": None,
                        "integrated_commit": None,
                        "integration_status": "pending",
                        "integration_fix_commits": [],
                    }
                )
            write_json(queue_path, queue)
            pack = _queue_reconciliation_context_pack(project)
        self.assertEqual(len(pack["candidate_feature_ids"]), 3)
        self.assertEqual(pack["excluded_candidate_feature_count"], 3)
        included_specs = [
            path for path in pack["files"] if path.startswith("docs/features/")
        ]
        self.assertEqual(len(included_specs), 3)


if __name__ == "__main__":
    unittest.main()
