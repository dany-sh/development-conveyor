from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from development_conveyor.config import Configuration
from development_conveyor.registry import Project
from development_conveyor.sessions import SessionPlan, SessionResult

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def synthetic_repository(root: Path, feature_status: str = "ready") -> tuple[Path, Project]:
    repository = root / "synthetic-app"
    repository.mkdir(parents=True)
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Synthetic Conveyor")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    (repository / ".factory").mkdir()
    (repository / "docs/features").mkdir(parents=True)
    (repository / "app.txt").write_text("baseline\n", encoding="utf-8")
    (repository / "docs/features/F001.md").write_text("# F001\n\nAdd a synthetic feature.\n", encoding="utf-8")
    (repository / "docs/AUTONOMY_CONTRACT.md").write_text("# Autonomy\n\nContinue through M0.\n", encoding="utf-8")
    adapter = {
        "schema_version": 1,
        "project": {"id": "synthetic", "name": "Synthetic"},
        "project_profile": {"usage": "personal_private"},
        "factory": {"feature_queue": "docs/FEATURE_QUEUE.yaml"},
        "git": {
            "default_branch": "main",
            "automatic_merge_to_main": False,
            "automatic_push": False,
            "one_commit_per_feature": True,
            "feature_branch_pattern": "codex/{feature_id_lower}-{slug}",
            "milestone_branch_pattern": "codex/{milestone_id_lower}-{slug}",
            "branch_after_integration": "milestone",
        },
        "integration": {"enabled": True, "strategy": "cherry_pick", "rerun_milestone_gates": True},
        "commands": {"build": [], "test": [], "lint": [], "package": [], "validate": []},
    }
    queue = {
        "schema_version": 1,
        "milestones": [{
            "id": "M0", "name": "Synthetic milestone", "status": "active", "base_commit": None,
            "integration_branch": "codex/m0-foundation", "integrated_features": [],
            "last_validated_commit": None, "human_gate": True,
        }],
        "features": [{
            "id": "F001", "title": "Synthetic Feature", "status": feature_status, "priority": 1,
            "milestone": "M0", "dependencies": [], "spec": "docs/features/F001.md",
            "acceptance_criteria": ["app.txt records the feature"], "requires_human_decision": False,
            "branch": None, "integration_base_commit": None, "accepted_commit": None,
            "integrated_commit": None, "integration_status": "pending", "integration_fix_commits": [],
        }],
    }
    write_json(repository / ".factory/project.yaml", adapter)
    write_json(repository / "docs/FEATURE_QUEUE.yaml", queue)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic baseline")
    baseline = git(repository, "rev-parse", "HEAD")
    git(repository, "branch", "codex/m0-foundation")
    git(repository, "switch", "codex/m0-foundation")
    project = Project(
        project_id="synthetic",
        repository=repository,
        enabled=True,
        priority=100,
        active_milestone="M0",
        recovery_branch="main",
        milestone_branch="codex/m0-foundation",
        validated_baseline_commit=baseline,
        queue_location="docs/FEATURE_QUEUE.yaml",
        autonomy_contract_location="docs/AUTONOMY_CONTRACT.md",
        validation_source=".factory/project.yaml",
        automation_mode="milestone",
        maximum_retries=None,
        schedule=None,
        human_gates=("milestone_merge",),
        last_accepted_feature=None,
        last_accepted_commit=None,
        current_state="feature_ready" if feature_status == "ready" else "queue_reconciliation",
        registration_notes="Disposable synthetic fixture.",
    )
    return repository, project


def controller_configuration(root: Path, project: Project) -> Configuration:
    controller = root / "controller"
    (controller / "schemas").mkdir(parents=True)
    for schema in (REPOSITORY_ROOT / "schemas").glob("*.json"):
        shutil.copy2(schema, controller / "schemas" / schema.name)
    conveyor = {
        "schema_version": 1,
        "approved_environment_variables": ["HOME"],
        "maximum_parallel_projects": 2,
        "default_mode": "milestone",
        "state_directory": "state",
        "report_directory": "reports",
        "log_directory": "logs",
        "codex": {"executable": "codex", "session_timeout_seconds": 60, "capture_json_events": True, "resume_sessions": True},
        "lock_policy": {
            "writer_lock_relative_path": ".factory/locks/writer.json",
            "controller_launch_lock_directory": "state/launch-locks",
            "heartbeat_seconds": 1,
            "require_process_identity": True,
            "timestamp_alone_is_stale": False,
        },
        "retries": {"implementation_repairs": 3, "integration_repairs": 3, "additional_adversarial_reviews": 2, "queue_reconciliation_repairs": 1},
        "logging": {"structured_events": True, "redact_sensitive_output": True, "persist_raw_subprocess_output": False},
        "scheduling": {"resume_before_new_work": True, "skip_foreign_writer_locks": True, "blocked_projects_do_not_block_portfolio": True},
        "goal_mode": {"required_for_unattended_milestone_runs": True, "feature_name": "goals"},
        "prohibited_operations": ["push", "tag", "merge_default_branch", "deploy", "release"],
    }
    return Configuration(root=controller, conveyor=conveyor, projects_document={"schema_version": 1, "projects": []})


class SyntheticLauncher:
    def __init__(self):
        self.actions: list[str] = []

    def launch(self, request):
        self.actions.append(request.action)
        structured = None
        structured_validation = "not_required"
        classification = None
        if request.action == "queue_reconciliation":
            queue_path = request.project.repository / request.project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["features"][0]["status"] = "ready"
            write_json(queue_path, queue)
            structured = {
                "schema_version": 1,
                "classification": "reconciled_ready_work",
                "summary": "Synthetic queue reconciled.",
                "next_action": "feature_cycle",
                "queue_validation": {"valid": True, "milestone_found": True, "feature_count": 1},
                "retryable": False,
                "human_decision": None,
            }
            structured_validation = "valid"
            classification = "reconciled_ready_work"
        elif request.action == "feature_cycle":
            self._accept_feature(request.project)
        elif request.action == "milestone_integration":
            self._integrate_feature(request.project)
        elif request.action == "milestone_gate":
            queue_path = request.project.repository / request.project.queue_location
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["milestones"][0]["status"] = "gate_passed"
            write_json(queue_path, queue)
            git(request.project.repository, "add", request.project.queue_location)
            git(request.project.repository, "commit", "-m", "factory: M0 gate passed")
        plan = SessionPlan(
            argv=("codex", "exec"), cwd=request.project.repository, prompt="synthetic", prompt_sha256="0" * 64,
            sandbox="workspace-write",
        )
        redacted_stdout = "synthetic"
        if structured is not None:
            marker = "CONVEYOR_RESULT=" + json.dumps(structured, separators=(",", ":"))
            redacted_stdout = json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": marker},
            })
        return SessionResult(
            request.action,
            0,
            f"session-{len(self.actions)}",
            "synthetic",
            plan,
            redacted_stdout=redacted_stdout,
            structured_result=structured,
            structured_output_validation=structured_validation,
            result_classification=classification,
        )

    def _accept_feature(self, project: Project) -> None:
        repository = project.repository
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        if feature.get("status") in {"accepted", "integration_pending", "integrated"}:
            return
        base = git(repository, "rev-parse", project.milestone_branch)
        branch = "codex/f001-synthetic-feature"
        feature.update({
            "status": "integration_pending",
            "branch": branch,
            "integration_base_commit": base,
            "accepted_commit": "SELF",
            "integration_status": "pending",
            "acceptance": {"tests_passed": True, "review_passed": True, "documentation_current": True},
        })
        write_json(queue_path, queue)
        (repository / "app.txt").write_text("baseline\nF001 integrated behavior\n", encoding="utf-8")
        git(repository, "add", "app.txt", project.queue_location)
        git(repository, "commit", "-m", "F001: implement synthetic feature")

    def _integrate_feature(self, project: Project) -> None:
        repository = project.repository
        queue_path = repository / project.queue_location
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        if feature.get("status") == "integrated":
            return
        accepted = git(repository, "rev-parse", str(feature["branch"]))
        git(repository, "switch", project.milestone_branch)
        git(repository, "cherry-pick", accepted)
        integrated = git(repository, "rev-parse", "HEAD")
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        feature = queue["features"][0]
        feature.update({"status": "integrated", "integration_status": "passed", "integrated_commit": integrated})
        queue["milestones"][0]["integrated_features"] = ["F001"]
        queue["milestones"][0]["last_validated_commit"] = integrated
        queue["milestones"][0]["status"] = "integration_complete"
        write_json(queue_path, queue)
        git(repository, "add", project.queue_location)
        git(repository, "commit", "-m", "factory: record F001 integration")
