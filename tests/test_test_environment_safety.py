"""Regression tests for fail-closed pytest home isolation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestEnvironmentSafetyTests(unittest.TestCase):
    def test_missing_sandbox_contract_is_rejected_even_for_empty_home(self):
        from scripts.test_environment_safety import test_environment_issues

        with tempfile.TemporaryDirectory() as tmpdir:
            issues = test_environment_issues(
                environ={"HOME": str(Path(tmpdir) / "empty")},
                platform="posix",
            )

        self.assertIn("test_sandbox_contract_missing", issues)

    def test_valid_runner_sandbox_is_allowed(self):
        from scripts.test_environment_safety import (
            initialize_test_sandbox,
            test_environment_issues,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "hermes-agent-tests.case"
            root.mkdir()
            contract = initialize_test_sandbox(root)
            environ = {
                "HOME": str(root),
                "USERPROFILE": str(root),
                "HERMES_HOME": str(root / ".hermes"),
                "HERMES_TEST_SANDBOX": str(root),
                "HERMES_TEST_SANDBOX_TOKEN": contract["token"],
            }

            self.assertEqual([], test_environment_issues(environ=environ, platform="posix"))
            self.assertEqual([], test_environment_issues(environ=environ, platform="nt"))

            nested_hermes = root / "tmp" / "case" / "hermes"
            nested = dict(environ, HERMES_HOME=str(nested_hermes))
            self.assertEqual([], test_environment_issues(environ=nested, platform="posix"))

    def test_mismatched_home_or_token_is_rejected(self):
        from scripts.test_environment_safety import (
            initialize_test_sandbox,
            test_environment_issues,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "hermes-agent-tests.case"
            root.mkdir()
            contract = initialize_test_sandbox(root)
            base = {
                "HOME": str(root),
                "USERPROFILE": str(root),
                "HERMES_HOME": str(root / ".hermes"),
                "HERMES_TEST_SANDBOX": str(root),
                "HERMES_TEST_SANDBOX_TOKEN": contract["token"],
            }

            wrong_home = dict(base, HOME=str(Path(tmpdir) / "operator"))
            self.assertIn(
                "home_outside_test_sandbox",
                test_environment_issues(environ=wrong_home, platform="posix"),
            )
            wrong_token = dict(base, HERMES_TEST_SANDBOX_TOKEN="wrong")
            self.assertIn(
                "test_sandbox_token_mismatch",
                test_environment_issues(environ=wrong_token, platform="posix"),
            )

    def test_pytest_plugin_and_ci_use_canonical_runner(self):
        runner = (PROJECT_ROOT / "scripts/run_tests.sh").read_text(encoding="utf-8")
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (PROJECT_ROOT / ".github/workflows/tests.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("initialize --root", runner)
        self.assertIn('HOME="$TEST_HOME"', runner)
        self.assertIn('USERPROFILE="$TEST_HOME"', runner)
        self.assertIn('HERMES_HOME="$TEST_HOME/.hermes"', runner)
        self.assertIn('HERMES_TEST_SANDBOX="$TEST_HOME"', runner)
        self.assertIn('TMPDIR="$TEST_HOME/tmp"', runner)
        self.assertIn('TEMP="$TEST_HOME/tmp"', runner)
        self.assertNotIn("cleanup --root", runner)
        self.assertNotIn("rm -rf", runner)
        self.assertIn("-p scripts.test_environment_safety", pyproject)
        self.assertNotIn("python -m pytest tests/e2e/", workflow)
        self.assertIn("scripts/run_tests.sh tests/e2e/", workflow)


if __name__ == "__main__":
    unittest.main()
