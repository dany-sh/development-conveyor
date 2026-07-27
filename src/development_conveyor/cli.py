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
from .consistency import ConsistencyChecker
from .migration import LegacyStateMigrator
from .integration_executor import execute_integration_plan
from .feature_result_recovery import FeatureResultRecovery
from .retained_feature_repair import (
    RetainedFeatureRepairRecovery,
    RetainedFeatureValidationRepair,
)
from .accepted_commit_recovery import AcceptedCommitRecovery
from .execution_profiles import PROFILE_NAMES
from .feature_scoping import FeatureScoper
from .feature_decisions import FeatureDecisionResolver
from .cycle_cache_repair import CycleCacheRepair
from .product_plan import ProductPlanReconciler
from .autopilot import Autopilot, autopilot_status, request_stop
from .feature_prelaunch_recovery import FeaturePrelaunchRecovery
from .policy_rebind import ReadyFeaturePolicyRebinder
from .runtime_audit import RuntimeAuditor
from .queue_control import (
    prioritize as prioritize_queue,
    queue_report,
    set_operator_paused,
    transition_backlog,
    transition_ready,
)

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
    queue = subparsers.add_parser(
        "queue", help="inspect a read-only feature queue scope without mutation"
    )
    queue.add_argument("--project", required=True)
    queue.add_argument(
        "--scope",
        choices=("active", "unfinished", "all"),
        default="active",
        help="active milestone (default), all unfinished work, or all known features",
    )
    queue.add_argument(
        "--milestone",
        help="optional read-only milestone filter; unknown IDs return structured JSON",
    )
    queue.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (the default output format)",
    )
    prioritize = subparsers.add_parser(
        "prioritize", help="move one feature before or after another queue entry"
    )
    prioritize.add_argument("--project", required=True)
    prioritize.add_argument("--feature", required=True)
    relative = prioritize.add_mutually_exclusive_group(required=False)
    relative.add_argument("--before")
    relative.add_argument("--after")
    prioritize.add_argument("--priority", choices=("P1", "P2", "P3"))
    ready = subparsers.add_parser("ready", help="mark one eligible proposed feature ready without starting work")
    ready.add_argument("--project", required=True)
    ready.add_argument("--feature", required=True)
    backlog = subparsers.add_parser("backlog", help="move one ready feature back to the proposed backlog")
    backlog.add_argument("--project", required=True)
    backlog.add_argument("--feature", required=True)
    pause = subparsers.add_parser(
        "pause", help="prevent a project from starting another controller cycle"
    )
    pause.add_argument("--project", required=True)
    unpause = subparsers.add_parser(
        "unpause", help="clear only the project operator-pause flag"
    )
    unpause.add_argument("--project", required=True)
    audit_runtime = subparsers.add_parser(
        "audit-runtime",
        help="audit effective model, capability, context, and output policy without launching a model",
    )
    audit_runtime.add_argument("--project", required=True)

    plan = subparsers.add_parser("plan", help="produce a read-only project plan")
    plan.add_argument("--project", required=True)
    plan.add_argument("--dry-run", action="store_true", help="accepted for command symmetry; planning is always read-only")
    plan.add_argument("--execution-profile", choices=PROFILE_NAMES)

    run = subparsers.add_parser("run", help="run or dry-run a project or portfolio cycle")
    run.add_argument("--project")
    run.add_argument("--mode", choices=MODES, default=None)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--execution-profile", choices=PROFILE_NAMES)
    run.add_argument("--feature")

    resume = subparsers.add_parser("resume", help="resume a persisted active cycle")
    resume.add_argument("--project", required=True)
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--execution-profile", choices=PROFILE_NAMES)

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
    mode = recover_planning.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_feature_result = subparsers.add_parser(
        "recover-feature-result",
        help="validate and finalize one exact terminal feature-session implementation without a model",
    )
    recover_feature_result.add_argument("--project", required=True)
    recover_feature_result.add_argument("--feature")
    recover_feature_result.add_argument("--original-transaction-id")
    recover_feature_result.add_argument(
        "--original-run-id", "--run-id", dest="original_run_id", required=True
    )
    recover_feature_result.add_argument(
        "--original-session-id",
        "--session-id",
        dest="original_session_id",
        required=True,
    )
    recover_feature_result.add_argument("--expected-branch")
    recover_feature_result.add_argument(
        "--expected-head", "--starting-head", dest="expected_head", required=True
    )
    recover_feature_result.add_argument("--diff-fingerprint")
    recover_feature_result.add_argument(
        "--expected-path",
        action="append",
        default=[],
        help="exact retained changed path; repeat once per expected file",
    )
    mode = recover_feature_result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_feature_prelaunch = subparsers.add_parser(
        "recover-feature-prelaunch",
        help=(
            "authenticate and recover one clean feature context failure that "
            "launched no model"
        ),
    )
    recover_feature_prelaunch.add_argument("--project", required=True)
    recover_feature_prelaunch.add_argument("--feature", required=True)
    recover_feature_prelaunch.add_argument("--autopilot-run-id", required=True)
    recover_feature_prelaunch.add_argument("--expected-branch", required=True)
    recover_feature_prelaunch.add_argument("--expected-head", required=True)
    mode = recover_feature_prelaunch.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    repair_feature_result = subparsers.add_parser(
        "repair-feature-result",
        help=(
            "repair and validate one exact retained feature result after a "
            "failed deterministic recovery"
        ),
    )
    repair_feature_result.add_argument("--project", required=True)
    repair_feature_result.add_argument("--feature", required=True)
    repair_feature_result.add_argument("--original-transaction-id", required=True)
    repair_feature_result.add_argument(
        "--failed-recovery-transaction-id", required=True
    )
    repair_feature_result.add_argument("--expected-branch", required=True)
    repair_feature_result.add_argument("--expected-head", required=True)
    mode = repair_feature_result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_feature_repair = subparsers.add_parser(
        "recover-feature-repair",
        help=(
            "deterministically validate and finalize one exact exhausted "
            "retained-feature repair chain"
        ),
    )
    recover_feature_repair.add_argument("--project", required=True)
    recover_feature_repair.add_argument("--feature", required=True)
    recover_feature_repair.add_argument(
        "--repair-transaction-id", required=True
    )
    mode = recover_feature_repair.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_accepted = subparsers.add_parser(
        "recover-accepted-commit",
        help=(
            "reconstruct one exact direct-child accepted commit from a completed "
            "candidate without launching a model"
        ),
    )
    recover_accepted.add_argument("--project", required=True)
    recover_accepted.add_argument("--feature", required=True)
    recover_accepted.add_argument("--candidate-commit", required=True)
    recover_accepted.add_argument("--milestone-base", required=True)
    recover_accepted.add_argument("--feature-branch", required=True)
    recover_accepted.add_argument("--feature-transaction-id", required=True)
    recover_accepted.add_argument("--acceptance-transaction-id", required=True)
    mode = recover_accepted.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    verify = subparsers.add_parser(
        "verify-consistency", help="verify ledger, projection, Git, queue, lease, and legacy evidence"
    )
    verify.add_argument("--project", required=True)
    verify.add_argument("--json", action="store_true", help="emit machine-readable JSON (the default output format)")
    migrate = subparsers.add_parser(
        "migrate-state", help="dry-run or apply deterministic legacy-evidence migration"
    )
    migrate.add_argument("--project", required=True)
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    execute_integration = subparsers.add_parser(
        "execute-integration-plan",
        help="execute one immutable controller-owned milestone integration plan",
    )
    execute_integration.add_argument("--plan", required=True)
    scope = subparsers.add_parser(
        "scope-features",
        help="author and validate one bounded planning-only feature set from an explicit brief",
    )
    scope.add_argument("--project", required=True)
    scope.add_argument("--feature", action="append", required=True)
    scope.add_argument("--new-feature", action="append", default=[])
    scope.add_argument("--ready", required=True)
    scope.add_argument("--brief", required=True)
    mode = scope.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_scope = subparsers.add_parser(
        "recover-scope-features",
        help="validate or finalize one exact retained scope-features planning diff",
    )
    recover_scope.add_argument("--project", required=True)
    recover_scope.add_argument("--run-id", required=True)
    mode = recover_scope.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    product_plan = subparsers.add_parser(
        "reconcile-product-plan",
        help=(
            "transactionally reconcile application planning from an immutable "
            "approved brief"
        ),
    )
    product_plan.add_argument("--project", required=True)
    product_plan.add_argument("--brief", required=True)
    mode = product_plan.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    recover_product_plan = subparsers.add_parser(
        "recover-product-plan",
        help=(
            "validate or finalize one exact retained approved product-plan diff "
            "without launching a model"
        ),
    )
    recover_product_plan.add_argument("--project", required=True)
    recover_product_plan.add_argument("--run-id", required=True)
    mode = recover_product_plan.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    resolve_features = subparsers.add_parser(
        "resolve-feature-decisions",
        help="resolve exact queue-feature decisions and select one ready feature without launching a model",
    )
    resolve_features.add_argument("--project", required=True)
    resolve_features.add_argument("--decision-file", required=True)
    resolve_features.add_argument("--select-feature", required=True)
    mode = resolve_features.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    repair_cycle_cache = subparsers.add_parser(
        "repair-cycle-cache",
        help=(
            "authenticate and deterministically rebuild one stale or malformed "
            "projection-derived cycle cache"
        ),
    )
    repair_cycle_cache.add_argument("--project", required=True)
    mode = repair_cycle_cache.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    rebind_policy = subparsers.add_parser(
        "rebind-ready-feature-policy",
        help=(
            "deterministically rebind one exact ready, selected, and unstarted "
            "feature execution policy"
        ),
    )
    rebind_policy.add_argument("--project", required=True)
    rebind_policy.add_argument("--feature", required=True)
    rebind_policy.add_argument("--expected-branch", required=True)
    rebind_policy.add_argument("--expected-head", required=True)
    rebind_policy.add_argument("--expected-old-profile", required=True)
    rebind_policy.add_argument(
        "--expected-old-parent-sessions", required=True, type=int
    )
    rebind_policy.add_argument(
        "--expected-old-child-sessions", required=True, type=int
    )
    rebind_policy.add_argument("--expected-old-escalation-trigger")
    rebind_policy.add_argument(
        "--expected-old-escalation-profile", choices=PROFILE_NAMES
    )
    rebind_policy.add_argument(
        "--target-profile", required=True, choices=PROFILE_NAMES
    )
    rebind_policy.add_argument(
        "--expected-path",
        action="append",
        required=True,
        help="exact metadata normalization path; repeat for queue and feature specification",
    )
    mode = rebind_policy.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    autopilot = subparsers.add_parser(
        "autopilot",
        help="continuously execute authoritative project transitions until safely stopped",
    )
    autopilot.add_argument("--project", required=True)
    mode = autopilot.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    stop_autopilot = subparsers.add_parser(
        "stop-autopilot",
        help="write a durable stop request for one project autopilot",
    )
    stop_autopilot.add_argument("--project", required=True)
    stop_autopilot.add_argument("--reason")
    autopilot_status_parser = subparsers.add_parser(
        "autopilot-status",
        help="show durable autopilot ownership, stop, and report state",
    )
    autopilot_status_parser.add_argument("--project", required=True)
    autopilot_status_parser.add_argument("--json", action="store_true")
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
    if args.command == "execute-integration-plan":
        return execute_integration_plan(Path(args.plan).expanduser().resolve())
    controller_root = discover_root(root)
    configuration = load_configuration(controller_root)
    registry = ProjectRegistry(configuration)
    if args.command == "stop-autopilot":
        return request_stop(
            configuration,
            registry.get(args.project),
            reason=args.reason,
        )
    if args.command == "autopilot-status":
        return autopilot_status(configuration, registry.get(args.project))
    if args.command == "reconcile-product-plan":
        reconciler = ProductPlanReconciler(
            configuration,
            SessionLauncher(controller_root, configuration.conveyor),
        )
        request = reconciler.inspect(
            project=registry.get(args.project),
            brief_path=Path(args.brief),
        )
        return (
            reconciler.apply(request)
            if args.apply
            else request.public_plan(dry_run=True)
        )
    if args.command == "recover-product-plan":
        reconciler = ProductPlanReconciler(
            configuration,
            SessionLauncher(controller_root, configuration.conveyor),
        )
        plan = reconciler.inspect_recovery(
            project=registry.get(args.project),
            run_id=args.run_id,
        )
        if args.apply:
            return reconciler.recover(plan)
        return {
            key: value for key, value in plan.items()
            if key != "_request"
        }
    if args.command == "repair-cycle-cache":
        repair = CycleCacheRepair(
            controller_root=controller_root,
            configuration=configuration,
            project=registry.get(args.project),
        )
        plan = repair.inspect()
        return repair.apply(plan) if args.apply else plan
    if args.command == "resolve-feature-decisions":
        resolver = FeatureDecisionResolver(configuration)
        request = resolver.inspect(
            project=registry.get(args.project),
            decision_path=Path(args.decision_file),
            selected_feature=args.select_feature,
        )
        return resolver.apply(request) if args.apply else request.public_plan(dry_run=True)

    launcher = SessionLauncher(controller_root, configuration.conveyor)
    engine = CycleEngine(
        configuration,
        launcher,
        execution_profile_override=getattr(args, "execution_profile", None),
    )
    if args.command == "audit-runtime":
        project = registry.get(args.project)
        return RuntimeAuditor(
            controller_root=controller_root,
            configuration=configuration,
            project=project,
            planner=lambda: engine.project_plan(project),
        ).audit()
    if args.command == "rebind-ready-feature-policy":
        if bool(args.expected_old_escalation_trigger) != bool(
            args.expected_old_escalation_profile
        ):
            raise ConveyorError(
                "expected old escalation trigger and profile must be supplied together"
            )
        expected_old_policy = {
            "profile": args.expected_old_profile,
            "parent_sessions": args.expected_old_parent_sessions,
            "child_sessions": args.expected_old_child_sessions,
        }
        if args.expected_old_escalation_trigger:
            expected_old_policy["escalation"] = {
                "trigger": args.expected_old_escalation_trigger,
                "profile": args.expected_old_escalation_profile,
            }
        rebinder = ReadyFeaturePolicyRebinder(configuration, launcher, engine)
        request = rebinder.inspect(
            project=registry.get(args.project),
            feature_id=args.feature,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
            expected_old_policy=expected_old_policy,
            target_profile=args.target_profile,
            expected_paths=tuple(args.expected_path),
        )
        return (
            rebinder.apply(request)
            if args.apply
            else request.public_plan(dry_run=True)
        )
    if args.command == "autopilot":
        controller = Autopilot(
            configuration=configuration,
            project=registry.get(args.project),
            engine=engine,
        )
        return controller.apply() if args.apply else controller.dry_run()

    if args.command == "scope-features":
        scoper = FeatureScoper(configuration, launcher)
        scope_request = scoper.inspect(
            project=registry.get(args.project),
            existing_features=args.feature,
            new_features=args.new_feature,
            ready_feature=args.ready,
            brief_path=Path(args.brief),
        )
        return (
            scoper.apply(scope_request)
            if args.apply
            else scope_request.public_plan(dry_run=True)
        )
    if args.command == "recover-scope-features":
        scoper = FeatureScoper(configuration, launcher)
        recovery_plan = scoper.inspect_recovery(
            project=registry.get(args.project), run_id=args.run_id
        )
        if args.apply:
            return scoper.recover(recovery_plan)
        return {key: value for key, value in recovery_plan.items() if key != "_request"}

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
                    "global_feature_count": len(queue.features),
                    "global_milestone_count": len(queue.milestones),
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

    if args.command == "queue":
        project = registry.get(args.project)
        return queue_report(
            configuration,
            project,
            runtime=engine.project_plan(project),
            scope=args.scope,
            requested_milestone=args.milestone,
        )

    if args.command == "prioritize":
        project = registry.get(args.project)
        relative_feature = args.before or args.after
        return prioritize_queue(
            configuration,
            project,
            feature_id=args.feature,
            relative_id=relative_feature,
            placement="before" if args.before else ("after" if args.after else None),
            priority=args.priority,
        )

    if args.command in {"ready", "backlog"}:
        project = registry.get(args.project)
        projection = engine._authoritative_projection(project)
        active_transaction = projection.get("active_transaction") if isinstance(projection, dict) else None
        transition = transition_ready if args.command == "ready" else transition_backlog
        return transition(
            configuration,
            project,
            feature_id=args.feature,
            active_transaction=active_transaction,
        )

    if args.command in {"pause", "unpause"}:
        project = registry.get(args.project)
        projection = engine._authoritative_projection(project)
        return set_operator_paused(
            configuration,
            project,
            paused=args.command == "pause",
            active_transaction=(
                projection.get("active_transaction")
                if isinstance(projection, dict)
                else None
            ),
        )

    if args.command == "verify-consistency":
        project = registry.get(args.project)
        return ConsistencyChecker(
            controller_root=controller_root,
            project=project,
            planner_observer=lambda: engine.project_plan(project),
        ).check()

    if args.command == "migrate-state":
        migrator = LegacyStateMigrator(
            controller_root=controller_root,
            project=registry.get(args.project),
        )
        return migrator.apply() if args.apply else migrator.plan()

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
        return engine.recover_planning_transaction(
            registry.get(args.project),
            run_id=args.run_id,
            expected_starting_head=args.starting_head,
            expected_diff_fingerprint=args.diff_fingerprint,
            expected_changed_paths=args.expected_path,
            expected_session_id=args.session_id,
            dry_run=not args.apply,
        )

    if args.command == "recover-feature-result":
        recovery = FeatureResultRecovery(
            controller_root=controller_root,
            configuration=configuration.conveyor,
            project=registry.get(args.project),
        )
        plan = recovery.inspect_recorded(
            feature_id=args.feature,
            original_transaction_id=args.original_transaction_id,
            original_run_id=args.original_run_id,
            original_session_id=args.original_session_id,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
            expected_diff_fingerprint=args.diff_fingerprint,
            expected_paths=tuple(args.expected_path),
        )
        return recovery.apply(plan) if args.apply else {
            **plan,
            "outcome": "recovery_ready",
            "application_repository_written": False,
        }

    if args.command == "recover-feature-prelaunch":
        recovery = FeaturePrelaunchRecovery(
            controller_root=controller_root,
            configuration=configuration,
            project=registry.get(args.project),
        )
        plan = recovery.inspect(
            feature_id=args.feature,
            autopilot_run_id=args.autopilot_run_id,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
        )
        return recovery.apply(plan) if args.apply else {
            **plan,
            "outcome": "prelaunch_recovery_ready",
            "application_repository_written": False,
        }

    if args.command == "repair-feature-result":
        repair = RetainedFeatureValidationRepair(
            controller_root=controller_root,
            configuration=configuration.conveyor,
            project=registry.get(args.project),
        )
        plan = repair.inspect(
            feature_id=args.feature,
            original_transaction_id=args.original_transaction_id,
            failed_recovery_transaction_id=args.failed_recovery_transaction_id,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
        )
        if args.apply:
            return repair.apply(plan)
        return {
            **{key: value for key, value in plan.items() if key != "_original_plan"},
            "outcome": "repair_ready",
            "application_repository_written": False,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
        }

    if args.command == "recover-feature-repair":
        recovery = RetainedFeatureRepairRecovery(
            controller_root=controller_root,
            configuration=configuration.conveyor,
            project=registry.get(args.project),
        )
        plan = recovery.inspect(
            feature_id=args.feature,
            repair_transaction_id=args.repair_transaction_id,
        )
        if args.apply:
            return recovery.apply(plan)
        return {
            **plan,
            "outcome": "recovery_ready",
            "application_repository_written": False,
            "application_commands_run": 0,
            "model_sessions_that_would_launch": 0,
            "child_sessions_that_would_launch": 0,
        }

    if args.command == "recover-accepted-commit":
        recovery = AcceptedCommitRecovery(
            controller_root=controller_root,
            configuration=configuration.conveyor,
            project=registry.get(args.project),
        )
        recovery_plan = recovery.inspect(
            feature_id=args.feature,
            candidate_commit=args.candidate_commit,
            milestone_base=args.milestone_base,
            feature_branch=args.feature_branch,
            feature_transaction_id=args.feature_transaction_id,
            acceptance_transaction_id=args.acceptance_transaction_id,
        )
        return recovery.apply(recovery_plan) if args.apply else {
            **recovery_plan,
            "outcome": "recovery_ready",
            "application_repository_written": False,
            "model_sessions_launched": 0,
            "child_sessions_launched": 0,
        }

    if args.command == "run":
        mode = args.mode or configuration.conveyor["default_mode"]
        if mode == "portfolio":
            if args.project:
                raise ConveyorError("--project cannot be combined with --mode portfolio")
            if args.feature:
                raise ConveyorError("--feature cannot be combined with --mode portfolio")
            return _portfolio_run(engine, registry, mode, args.dry_run)
        if not args.project:
            raise ConveyorError("--project is required unless --mode portfolio is used")
        return engine.run_project(
            registry.get(args.project),
            mode,
            dry_run=args.dry_run,
            feature_id=args.feature,
        )
    raise ConveyorError(f"unsupported command: {args.command}")


def main(arguments: list[str] | None = None) -> int:
    try:
        result = execute(arguments)
        print(render_json(result))
        return 2 if (
            result.get("launch_allowed") is False
            or result.get("runtime_policy_passed") is False
            or result.get("outcome") == "resolution_rejected"
            or result.get("classification") == "POLICY_REBIND_REJECTED"
            or result.get("classification") == "AUTOPILOT_FAILED"
            or result.get("classification") in {
                "HUMAN_DECISION_REQUIRED", "CORRUPT_EVIDENCE", "UNSAFE_REPOSITORY_STATE",
                "unknown_milestone",
            }
        ) else 0
    except (ConveyorError, OSError, ValueError) as exc:
        print(f"development-conveyor: {redact_text(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
