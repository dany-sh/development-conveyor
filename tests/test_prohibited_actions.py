import tempfile
import unittest
from pathlib import Path

from development_conveyor.errors import SafetyViolation
from development_conveyor.validation import SafetyPolicy


class ProhibitedActionTests(unittest.TestCase):
    def test_prohibited_git_and_external_operations_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = [
                ["git", "push", "origin", "main"],
                ["git", "tag", "v1"],
                ["npm", "run", "deploy"],
                ["npm", "publish"],
                ["gh", "release", "create", "v1"],
                ["xcrun", "notarytool", "submit"],
            ]
            with SafetyPolicy.observe_command_attempts() as observed:
                for command in commands:
                    with self.subTest(command=command), self.assertRaises(SafetyViolation):
                        SafetyPolicy.validate_controller_command(
                            command, cwd=root, registered_repository=root
                        )
            self.assertEqual(len(commands), len(observed))
            self.assertEqual(
                {"push", "tag", "deploy", "publish", "release", "notarize"},
                {
                    category for item in observed
                    for category in item["prohibited_categories"]
                },
            )
            self.assertTrue(all(item["authority"] == "controller" for item in observed))

    def test_other_destructive_git_operations_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = [
                ["git", "merge", "feature"],
                ["git", "reset", "--hard", "HEAD"],
                ["git", "stash"],
                ["git", "clean", "-fd"],
                ["git", "checkout", "-f", "main"],
            ]
            for command in commands:
                with self.subTest(command=command), self.assertRaises(SafetyViolation):
                    SafetyPolicy.validate_controller_command(
                        command, cwd=root, registered_repository=root
                    )

    def test_read_only_git_and_safe_codex_launcher_are_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            SafetyPolicy.validate_controller_command(["git", "status", "--porcelain"], cwd=root, registered_repository=root)
            SafetyPolicy.validate_controller_command(["codex", "exec", "--json", "-"], cwd=root, registered_repository=root, allow_codex=True)

    def test_operation_outside_registered_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with self.assertRaises(SafetyViolation):
                SafetyPolicy.validate_controller_command(["git", "status"], cwd=Path(first), registered_repository=Path(second))
