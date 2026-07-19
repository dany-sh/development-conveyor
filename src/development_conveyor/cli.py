"""Stable Development Conveyor command-line interface."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import discover_root, load_configuration
from .cycle_engine import CycleEngine
from .errors import ConveyorError
from .redaction import redact_text
from .registry import ProjectRegistry
from .reporting import render_json
from .queue import FeatureQueue, resolve_queue_path
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

    doctor = subparsers.add_parser("doctor", help="validate Codex CLI, model, and reasoning compatibility")
    doctor.add_argument("--project")

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

    reconcile = subparsers.add_parser(
        "reconcile", help="validate queue reconciliation and optionally recover Conveyor-owned project state"
    )
    reconcile.add_argument("--project", required=True)
    reconcile.add_argument(
        "--resolve-human-decision",
        action="store_true",
        help="resolve the project's pinned human gate after deterministic evidence validation",
    )
    reconcile.add_argument(
        "--reason",
        help="explicit user-approval reason; required with --resolve-human-decision",
    )
    reconcile.add_argument("--dry-run", action="store_true")
    recover_branch = subparsers.add_parser(
        "recover-feature-branch",
        help="move an exact dirty milestone worktree onto its persisted feature branch without launching a session",
    )
    recover_branch.add_argument("--project", required=True)
    recover_branch.add_argument("--dry-run", action="store_true")
    recover_planning = subparsers.add_parser(
        "recover-planning",
        help="validate and optionally finalize one exact recorded planning-only transaction",
    )
    recover_planning.add_argument("--project", required=True)
    recover_planning.add_argument("--run-id", required=True)
    recover_planning.add_argument("--session-id", required=True)
    recover_planning.add_argument("--starting-head", required=True)
    recover_planning.add_argument("--diff-fingerprint", required=True)
    recover_planning.add_argument(
        "--expected-path",
        action="append",
        required=True,
        help="exact changed path; repeat once per authorized planning file",
    )
    recover_planning.add_argument("--apply", action="store_true")
    recover_planning.add_argument("--dry-run", action="store_true")
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
            item = {
                "project_id": project.project_id,
                "repository_exists": project.repository.is_dir(),
                "enabled": project.enabled,
                "configured_state": project.current_state,
            }
            try:
                queue_path = resolve_queue_path(project.repository, project.queue_location)
                queue = FeatureQueue.from_location(project.repository, project.queue_location)
                item.update({
                    "queue_valid": True,
                    "resolved_queue_path": str(queue_path),
                    "milestone_found": queue.milestone(project.active_milestone or "") is not None,
                    "feature_count": len(queue.features_for_milestone(project.active_milestone or "")),
                })
            except ConveyorError as exc:
                item.update({"queue_valid": False, "queue_error": str(exc), "resolved_queue_path": None})
            projects.append(item)
        return {
            "valid": all(item["repository_exists"] and item.get("queue_valid") for item in projects),
            "schema_version": configuration.conveyor["schema_version"],
            "project_count": len(projects),
            "projects": projects,
            "goal_mode": goal_mode_status(configuration.conveyor["codex"]["executable"], controller_root),
        }

    if args.command == "doctor":
        project = registry.get(args.project) if args.project else None
        action = "feature_cycle"
        compatibility = launcher.compatibility(action, project_id=project.project_id if project else None)
        result = {
            "schema_version": 1,
            "project_id": project.project_id if project else None,
            "action": action,
            "compatibility": compatibility.as_dict(),
            "launch_allowed": compatibility.compatible,
            "application_repository_written": False,
        }
        if project:
            plan = engine.project_plan(project)
            result.update({
                "selected_feature": plan.get("selected_feature"),
                "feature_starting_commit": (plan.get("repository_state") or {}).get("milestone_branch_head"),
                "old_session_will_resume": plan.get("old_session_will_resume", False),
            })
        if not compatibility.compatible:
            result["human_decision_required"] = compatibility.human_gate(project.project_id if project else None)
        return result

    if args.command == "status":
        if args.project:
            return engine.project_plan(registry.get(args.project))
        projects = [engine.project_plan(item) for item in registry.all()]
        counts: dict[str, int] = {}
        for item in projects:
            action = str(item.get("proposed_next_action"))
            counts[action] = counts.get(action, 0) + 1
        return {
            "schema_version": 1,
            "project_count": len(projects),
            "action_counts": counts,
            "projects": projects,
        }

    if args.command == "plan":
        return engine.run_project(registry.get(args.project), "audit", dry_run=True)

    if args.command == "resume":
        return engine.run_project(registry.get(args.project), "resume", dry_run=args.dry_run)

    if args.command == "reconcile":
        if args.reason is not None and not args.resolve_human_decision:
            raise ConveyorError("--reason requires --resolve-human-decision")
        if args.resolve_human_decision:
            return engine.resolve_human_decision(
                registry.get(args.project), reason=args.reason or "", dry_run=args.dry_run
            )
        return engine.reconcile_controller_state(registry.get(args.project), dry_run=args.dry_run)

    if args.command == "recover-feature-branch":
        return engine.recover_feature_branch(registry.get(args.project), dry_run=args.dry_run)

    if args.command == "recover-planning":
        if args.apply and args.dry_run:
            raise ConveyorError("--apply and --dry-run are mutually exclusive")
        return engine.recover_planning_transaction(
            registry.get(args.project),
            run_id=args.run_id,
            expected_starting_head=args.starting_head,
            expected_diff_fingerprint=args.diff_fingerprint,
            expected_changed_paths=args.expected_path,
            expected_session_id=args.session_id,
            dry_run=not args.apply,
        )

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
        result = execute(arguments)
        print(render_json(result))
        return 2 if (
            result.get("launch_allowed") is False
            or result.get("outcome") == "resolution_rejected"
        ) else 0
    except (ConveyorError, OSError, ValueError) as exc:
        print(f"development-conveyor: {redact_text(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
