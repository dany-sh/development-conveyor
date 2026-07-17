"""Priority, resume-first, bounded multi-project scheduling."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .registry import Project


def order_projects(projects: list[Project], has_active_cycle: Callable[[Project], bool]) -> list[Project]:
    return sorted(projects, key=lambda project: (not has_active_cycle(project), -project.priority, project.project_id))


class PortfolioScheduler:
    def __init__(self, maximum_parallel_projects: int):
        if maximum_parallel_projects < 1:
            raise ValueError("maximum_parallel_projects must be positive")
        self.maximum_parallel_projects = maximum_parallel_projects

    def run(
        self,
        projects: list[Project],
        *,
        has_active_cycle: Callable[[Project], bool],
        worker: Callable[[Project], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = order_projects([item for item in projects if item.enabled], has_active_cycle)
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.maximum_parallel_projects) as executor:
            futures = {executor.submit(worker, project): project for project in ordered}
            for future in as_completed(futures):
                project = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # project isolation: one failure must not starve healthy projects
                    results.append({"project_id": project.project_id, "outcome": "conveyor_error", "error": str(exc)})
        order = {project.project_id: index for index, project in enumerate(ordered)}
        return sorted(results, key=lambda item: order.get(str(item.get("project_id")), len(order)))

