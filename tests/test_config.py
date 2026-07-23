import json
import tempfile
import unittest
from pathlib import Path

from development_conveyor.config import load_configuration
from development_conveyor.errors import ConfigurationError, SchemaValidationError
from development_conveyor.registry import ProjectRegistry
from tests.helpers import REPOSITORY_ROOT


class ConfigurationTests(unittest.TestCase):
    def _root(self, variable="HOME", repository="${HOME}/repo"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "config").mkdir()
        (root / "schemas").mkdir()
        for name in ("conveyor-config.schema.json", "projects.schema.json", "execution-profiles.schema.json"):
            (root / "schemas" / name).write_text((REPOSITORY_ROOT / "schemas" / name).read_text(), encoding="utf-8")
        conveyor = json.loads((REPOSITORY_ROOT / "config/conveyor.yaml").read_text())
        conveyor["approved_environment_variables"] = [variable]
        (root / "config/conveyor.yaml").write_text(json.dumps(conveyor), encoding="utf-8")
        (root / "config/execution-profiles.yaml").write_text(
            (REPOSITORY_ROOT / "config/execution-profiles.yaml").read_text(), encoding="utf-8"
        )
        project = json.loads((REPOSITORY_ROOT / "config/projects.yaml").read_text())["projects"][0]
        project["repository"] = repository
        (root / "config/projects.yaml").write_text(json.dumps({"schema_version": 1, "projects": [project]}), encoding="utf-8")
        self.addCleanup(temporary.cleanup)
        return root

    def test_home_expansion_and_validation(self):
        root = self._root()
        config = load_configuration(root, {"HOME": "/tmp/synthetic-home"})
        self.assertEqual(config.projects[0]["repository"], "/tmp/synthetic-home/repo")

    def test_unresolved_variable_is_rejected(self):
        root = self._root()
        with self.assertRaises(ConfigurationError):
            load_configuration(root, {})

    def test_unapproved_variable_is_rejected(self):
        root = self._root(variable="HOME", repository="${TOKEN}/repo")
        with self.assertRaises(ConfigurationError):
            load_configuration(root, {"HOME": "/tmp", "TOKEN": "secret"})

    def test_schema_rejects_zero_concurrency(self):
        root = self._root()
        path = root / "config/conveyor.yaml"
        value = json.loads(path.read_text())
        value["maximum_parallel_projects"] = 0
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(SchemaValidationError):
            load_configuration(root, {"HOME": "/tmp"})

    def test_execution_profile_matrix_is_loaded_and_fixed(self):
        root = self._root()
        configuration = load_configuration(root, {"HOME": "/tmp/synthetic-home"})
        profile = configuration.execution_profiles["profiles"]["multi_module_precise"]
        self.assertEqual(profile, {"model": "gpt-5.6-terra", "reasoning": "high"})
        path = root / "config/execution-profiles.yaml"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["profiles"]["multi_module_precise"]["model"] = "gpt-5.6-sol"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_configuration(root, {"HOME": "/tmp/synthetic-home"})

    def test_inventory_blocking_warning_patterns_are_typed_registry_policy(self):
        root = self._root()
        path = root / "config/projects.yaml"
        value = json.loads(path.read_text())
        value["projects"][0]["inventory_blocking_warning_patterns"] = ["SECURITY:*", "POLICY:*"]
        path.write_text(json.dumps(value), encoding="utf-8")
        configuration = load_configuration(root, {"HOME": "/tmp/synthetic-home"})
        project = ProjectRegistry(configuration).all()[0]
        self.assertEqual(
            project.inventory_blocking_warning_patterns,
            ("SECURITY:*", "POLICY:*"),
        )
