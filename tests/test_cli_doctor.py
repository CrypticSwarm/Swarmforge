#!/usr/bin/env python3
"""Tests for swarmforge.cli.doctor.

Doctor entry points must keep checkout and project resolution separate.
Run: python3 tests/test_cli_doctor.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).absolute().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarmforge import doctor
from swarmforge.cli.main import build_parser, main


class DoctorCliTests(unittest.TestCase):
    def test_setup_and_doctor_accept_the_same_readiness_options(self):
        parser = build_parser("/default")
        for command in ("doctor", "setup"):
            with self.subTest(command=command):
                options = parser.parse_args([
                    command, "--checkout", "/checkout", "--project", "/project",
                    "--harness", "claude", "--harness", "codex"])
                self.assertEqual(options.checkout, "/checkout")
                self.assertEqual(options.project, "/project")
                self.assertEqual(options.harness, ["claude", "codex"])

    def test_selected_harnesses_and_project_reach_the_checker(self):
        report = doctor.Report()
        report.add("test", "FAIL", "test failure")
        out = io.StringIO()
        with mock.patch("swarmforge.cli.doctor.doctor.inspect", return_value=report) as inspect:
            result = main(["doctor", "--harness", "claude", "--harness", "codex",
                           "--project", "/project", "--json"], out=out, err=io.StringIO(), checkout="/checkout")
        inspect.assert_called_once_with("/checkout", "/project", ["claude", "codex"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(out.getvalue())["exit_code"], 1)

    def test_symlinked_entrypoint_uses_its_checkout_from_another_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / "swarmforge"
            link.symlink_to(REPO_ROOT / "bin/swarmforge")
            result = subprocess.run([sys.executable, str(link), "doctor", "--json"],
                                    cwd=temp, env=dict(os.environ, PATH="/nonexistent"),
                                    capture_output=True, text=True)
            data = json.loads(result.stdout)
            checkout = next(c for c in data["checks"] if c["name"] == "checkout")
            self.assertEqual(checkout["detail"], str(REPO_ROOT))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "")

    def test_empty_project_is_a_usage_error(self):
        self.assertEqual(main(["doctor", "--project", ""], out=io.StringIO(), err=io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
