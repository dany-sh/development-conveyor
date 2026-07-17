"""Repository-scoped Codex session construction, launch, capture, and resume."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SessionError
from .redaction import redact_text
from .registry import Project
from .validation import SafetyPolicy

ACTION_PROMPTS = {
    "queue_reconciliation": "queue-reconciliation.md",
    "feature_cycle": "feature-cycle.md",
    "milestone_integration": "milestone-integration.md",
    "milestone_gate": "milestone-gate.md",
    "human_decision_report": "human-decision-report.md",
}


@dataclass(frozen=True)
class SessionRequest:
    action: str
    project: Project
    run_id: str
    mode: str
    feature: str | None = None
    session_id: str | None = None
    repair_attempt: int | None = None
    repair_evidence: str | None = None


@dataclass(frozen=True)
class SessionPlan:
    argv: tuple[str, ...]
    cwd: Path
    prompt: str
    prompt_sha256: str
    sandbox: str


@dataclass(frozen=True)
class SessionResult:
    action: str
    returncode: int
    session_id: str | None
    redacted_output: str
    plan: SessionPlan


class SessionLauncher:
    def __init__(self, controller_root: Path, configuration: dict[str, Any]):
        self.controller_root = controller_root.resolve()
        self.configuration = configuration

    def _render_prompt(self, request: SessionRequest) -> str:
        try:
            filename = ACTION_PROMPTS[request.action]
        except KeyError as exc:
            raise SessionError(f"unknown session action: {request.action}") from exc
        template = (self.controller_root / "prompts" / filename).read_text(encoding="utf-8")
        prompt = template.format(
            repository=request.project.repository,
            project_id=request.project.project_id,
            milestone=request.project.active_milestone or "UNRESOLVED",
            milestone_branch=request.project.milestone_branch or "UNRESOLVED",
            feature=request.feature or "NONE",
            run_id=request.run_id,
            mode=request.mode,
        )
        if request.repair_attempt is not None:
            prompt += (
                "\n## Focused repair continuation\n\n"
                f"This is focused repair attempt {request.repair_attempt}. The previous redacted failure evidence "
                f"fingerprint is `{request.repair_evidence}`. Reinspect current repository evidence, state a changed "
                "hypothesis in the repository run log, and do not repeat the failed approach. Stop if a changed "
                "hypothesis is not justified or a human-decision condition is reached.\n"
            )
        return prompt

    def plan(self, request: SessionRequest) -> SessionPlan:
        prompt = self._render_prompt(request)
        executable = str(self.configuration["codex"]["executable"])
        sandbox = "read-only" if request.action == "human_decision_report" else "workspace-write"
        if request.session_id:
            argv = (executable, "exec", "resume", "--json", request.session_id, "-")
        else:
            argv = (executable, "exec", "--cd", str(request.project.repository), "--json", "--sandbox", sandbox, "-")
        SafetyPolicy.validate_controller_command(
            list(argv), cwd=request.project.repository, registered_repository=request.project.repository, allow_codex=True
        )
        return SessionPlan(
            argv=argv,
            cwd=request.project.repository,
            prompt=prompt,
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            sandbox=sandbox,
        )

    def launch(self, request: SessionRequest) -> SessionResult:
        plan = self.plan(request)
        environment = dict(os.environ)
        environment.update({
            "CONVEYOR_RUN_ID": request.run_id,
            "CONVEYOR_PROJECT_ID": request.project.project_id,
            "CONVEYOR_MODE": request.mode,
            "CONVEYOR_FEATURE": request.feature or "",
        })
        try:
            result = subprocess.run(
                list(plan.argv), cwd=plan.cwd, env=environment, input=plan.prompt, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                timeout=int(self.configuration["codex"]["session_timeout_seconds"]),
            )
        except subprocess.TimeoutExpired as exc:
            output = redact_text(str(exc.stdout or ""))
            raise SessionError(f"repository session timed out; partial output: {output[-2000:]}") from exc
        session_id = self._session_id(result.stdout)
        return SessionResult(
            action=request.action,
            returncode=result.returncode,
            session_id=session_id or request.session_id,
            redacted_output=redact_text(result.stdout),
            plan=plan,
        )

    @staticmethod
    def _session_id(output: str) -> str | None:
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            for key in ("thread_id", "threadId", "session_id", "sessionId"):
                if isinstance(value.get(key), str):
                    return value[key]
            payload = value.get("payload")
            if isinstance(payload, dict):
                for key in ("thread_id", "threadId", "session_id", "sessionId"):
                    if isinstance(payload.get(key), str):
                        return payload[key]
        return None
