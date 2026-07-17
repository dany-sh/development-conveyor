"""Configuration loading with JSON-compatible YAML and approved expansion."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .validation import validate_schema

VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path}: invalid JSON-compatible YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: top level must be an object")
    return value


def expand_value(value: Any, approved: set[str], environment: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in approved:
                raise ConfigurationError(f"environment variable {name} is not approved")
            resolved = environment.get(name)
            if resolved is None or not resolved:
                raise ConfigurationError(f"environment variable {name} is unresolved")
            return resolved

        expanded = VARIABLE.sub(replace, value)
        if "${" in expanded:
            raise ConfigurationError(f"unresolved environment variable in {value!r}")
        return expanded
    if isinstance(value, list):
        return [expand_value(item, approved, environment) for item in value]
    if isinstance(value, dict):
        return {key: expand_value(item, approved, environment) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class Configuration:
    root: Path
    conveyor: dict[str, Any]
    projects_document: dict[str, Any]

    @property
    def projects(self) -> list[dict[str, Any]]:
        return list(self.projects_document["projects"])

    def owned_path(self, configured: str) -> Path:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()


def load_configuration(root: Path, environment: dict[str, str] | None = None) -> Configuration:
    root = root.expanduser().resolve()
    env = dict(os.environ if environment is None else environment)
    raw_conveyor = load_json(root / "config/conveyor.yaml")
    approved_value = raw_conveyor.get("approved_environment_variables", [])
    if not isinstance(approved_value, list) or not all(isinstance(item, str) for item in approved_value):
        raise ConfigurationError("approved_environment_variables must be an array of names")
    approved = set(approved_value)
    conveyor = expand_value(raw_conveyor, approved, env)
    projects = expand_value(load_json(root / "config/projects.yaml"), approved, env)
    validate_schema(conveyor, load_json(root / "schemas/conveyor-config.schema.json"))
    validate_schema(projects, load_json(root / "schemas/projects.schema.json"))

    ids = [item["project_id"] for item in projects["projects"]]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("project IDs must be unique")
    repositories = [str(Path(item["repository"]).expanduser().resolve()) for item in projects["projects"]]
    if len(repositories) != len(set(repositories)):
        raise ConfigurationError("registered repository paths must be unique")
    return Configuration(root=root, conveyor=conveyor, projects_document=projects)


def discover_root(start: Path | None = None) -> Path:
    override = os.environ.get("DEVELOPMENT_CONVEYOR_HOME")
    if override:
        candidate = Path(override).expanduser().resolve()
    elif start is not None:
        candidate = start.expanduser().resolve()
    else:
        candidate = (Path.home() / "Developer/development-conveyor").resolve()
    if not (candidate / "config/conveyor.yaml").is_file():
        raise ConfigurationError(f"Development Conveyor configuration is missing under {candidate}")
    return candidate

