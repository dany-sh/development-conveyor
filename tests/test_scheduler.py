import threading
import time
import unittest

from development_conveyor.registry import Project
from development_conveyor.scheduler import PortfolioScheduler, order_projects


def project(project_id, priority):
    from pathlib import Path
    return Project(project_id, Path(f"/tmp/{project_id}"), True, priority, "M0", "main", "codex/m0", "a" * 40, "queue", "contract", "adapter", "milestone", None, None, (), None, None, "feature_ready", "")


class SchedulerTests(unittest.TestCase):
    def test_resume_first_then_priority(self):
        projects = [project("high", 100), project("resume", 1), project("middle", 50)]
        ordered = order_projects(projects, lambda item: item.project_id == "resume")
        self.assertEqual([item.project_id for item in ordered], ["resume", "high", "middle"])

    def test_global_concurrency_and_project_isolation(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def worker(item):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            if item.project_id == "blocked":
                return {"project_id": item.project_id, "outcome": "blocked"}
            return {"project_id": item.project_id, "outcome": "complete"}

        projects = [project("one", 3), project("blocked", 2), project("two", 1)]
        results = PortfolioScheduler(2).run(projects, has_active_cycle=lambda _: False, worker=worker)
        self.assertLessEqual(maximum, 2)
        self.assertEqual({item["project_id"] for item in results}, {"one", "blocked", "two"})
        self.assertIn("complete", {item["outcome"] for item in results})

