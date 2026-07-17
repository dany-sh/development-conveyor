"""Stable Development Conveyor command-line interface."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import discover_root, load_configuration
from .cycle_engine import CycleEngine
from .errors import ConveyorError
from .redaction import redact_text
from .registry import ProjectRegistry
from .reporting import build_project_plan, portfolio_status, render_json
from .scheduler import PortfolioScheduler
from .sessions import SessionLauncher
from .validation import SafetyPolicy

MODES = ("audit", "one_feature", "until_blocked", "milestone", "portfolio", "resume")


def goal_mode_status(executable: str, root: Path) -> dict[str, Any]:
    argv = [executable, "features", "list"]
    SafetyPolicy.validate_controller_command(argv, cwd=root, allow_codex=True)
    result = subprocess.run(argv, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        return {
            "verified": False,
            "enabled": None,
            "evidence": redact_text(result.stderr.strip() or "codex features list failed"),
            "enable_command": "codex features enable goals",
        }
    for line in result.stdout.splitlines():
        columns = line.split()
        if columns and columns[0] == "goals":
            enabled = columns[-1].lower() == "true"
            return {
                "verified": True,
                "enabled": enabled,
                "evidence": line.strip(),
                "enable_command": None if enabled else "codex features enable goals",
            }
    return {
        "verified": False,
        "enabled": None,
        "evidence": "goals feature was not listed by the installed Codex CLI",
        "enable_command": "codex features enable goals",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conveyor", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config", help="validate controller and project configuration")

    status = subparsers.add_parser("status", help="show read-only portfolio or project status")
    status.add_argument("--project")

    plan = subparsers.add_parser("plan", help="produce a read-only project plan")
    plan.add_argument("--project", required=True)
    plan.add_argument("--dry-run", action="store_true", help="accepted for command symmetry; planning is always read-only")

    run = subparsers.add_parser("run", help="run or dry-run a project or portfolio cycle")
    run.add_argument("--project")
    run.add_argument("--mode", choices=MODES, default=None)
    run.add_argument("--dry-run", action="store_true")

    resume = subparsers.add_parser("resume", help="resume a persisted active cycle")
    resume.add_argument("--project", required=True)
    resume.add_argument("--dry-run", action="store_true")
    return parser


def _portfolio_run(engine: CycleEngine, registry: ProjectRegistry, mode: str, dry_run: bool) -> dict[str, Any]:
    scheduler = PortfolioScheduler(engine.configuration.conveyor["maximum_parallel_projects"])

    def active(project):
        try:
            state = engine.cycle_store.read(project.repository / ".factory/conveyor-state.json")
            return bool(state and state.get("current_phase") != "completed")
        except ConveyorError:
            return True

    def worker(project):
        project_mode = "audit" if dry_run else (project.automation_mode if mode == "portfolio" else mode)
        return engine.run_project(project, project_mode, dry_run=dry_run)

    return {
        "schema_version": 1,
        "mode": mode,
        "dry_run": dry_run,
        "maximum_parallel_projects": scheduler.maximum_parallel_projects,
        "results": scheduler.run(registry.enabled(), has_active_cycle=active, worker=worker),
    }


def execute(arguments: list[str] | None = None, *, root: Path | None = None) -> dict[str, Any]:
    args = _parser().parse_args(arguments)
    controller_root = discover_root(root)
    configuration = load_configuration(controller_root)
    registry = ProjectRegistry(configuration)
    launcher = SessionLauncher(controller_root, configuration.conveyor)
    engine = CycleEngine(configuration, launcher)

    if args.command == "validate-config":
        projects = []
        for project in registry.all():
            projects.append({
                "project_id": project.project_id,
                "repository_exists": project.repository.is_dir(),
                "enabled": project.enabled,
                "configured_state": project.current_state,
            })
        return {
            "valid": True,
            "schema_version": configuration.conveyor["schema_version"],
            "project_count": len(projects),
            "projects": projects,
            "goal_mode": goal_mode_status(configuration.conveyor["codex"]["executable"], controller_root),
        }

    if args.command == "status":
        if args.project:
            project = engine.effective_project(registry.get(args.project))
            return build_project_plan(project, configuration.conveyor, controller_root)
        return portfolio_status([engine.effective_project(item) for item in registry.all()], configuration.conveyor, controller_root)

    if args.command == "plan":
        return engine.run_project(registry.get(args.project), "audit", dry_run=True)

    if args.command == "resume":
        return engine.run_project(registry.get(args.project), "resume", dry_run=args.dry_run)

    if args.command == "run":
        mode = args.mode or configuration.conveyor["default_mode"]
        if mode == "portfolio":
            if args.project:
                raise ConveyorError("--project cannot be combined with --mode portfolio")
            return _portfolio_run(engine, registry, mode, args.dry_run)
        if not args.project:
            raise ConveyorError("--project is required unless --mode portfolio is used")
        return engine.run_project(registry.get(args.project), mode, dry_run=args.dry_run)
    raise ConveyorError(f"unsupported command: {args.command}")


def main(arguments: list[str] | None = None) -> int:
    try:
        print(render_json(execute(arguments)))
        return 0
    except (ConveyorError, OSError, ValueError) as exc:
        print(f"development-conveyor: {redact_text(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
