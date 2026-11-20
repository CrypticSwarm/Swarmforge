#!/usr/bin/env python3
"""Tests for swarmforge.cli.setup.

Setup must refuse unattended mutation and honor every declined action.
Run: python3 tests/test_cli_setup.py
"""

import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import doctor
from swarmforge.cli.main import build_parser, main
from swarmforge.cli import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.report = doctor.Report(settings={"codex": {"image": "test:local"}})

    def run_setup(self, answers, report=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(setup.doctor, "inspect", return_value=report or self.report), \
                mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(setup, "execute", return_value=True) as execute, \
                mock.patch.object(setup.doctor, "probe"):
            result = main(["setup", "--harness", "codex"], out=out, err=err, checkout=REPO_ROOT)
        return result, execute

    def test_noninteractive_setup_refuses_without_inspection_or_changes(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(setup.doctor, "inspect") as inspect:
            result = main(["setup"], out=io.StringIO(), err=io.StringIO())
        self.assertEqual(result, 2)
        inspect.assert_not_called()

    def test_declining_actions_does_not_run_commands(self):
        result, execute = self.run_setup(["n", "n"])
        self.assertEqual(result, 0)
        execute.assert_not_called()

    def test_eof_does_not_consent_to_changes(self):
        result, execute = self.run_setup(EOFError())
        self.assertEqual(result, 0)
        execute.assert_not_called()

    def test_prerequisite_failure_stops_before_any_prompt_or_action(self):
        self.report.add("docker.daemon", "FAIL", "unreachable")
        result, execute = self.run_setup([])
        self.assertEqual(result, 1)
        execute.assert_not_called()

    def test_missing_image_is_built_only_after_confirmation(self):
        self.report.add("codex.image", "FAIL", "missing")
        result, execute = self.run_setup(["n", "yes", "n"])
        self.assertEqual(result, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertIn("build_codex", execute.call_args.args[0])

    def test_smoke_test_is_explicit_read_only_and_never_pulls(self):
        result, execute = self.run_setup(["n", "y"])
        self.assertEqual(result, 0)
        argv = execute.call_args.args[0]
        self.assertIn("--pull=never", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("readonly", argv[argv.index("--mount") + 1])
        self.assertIn("--entrypoint", argv)

    def test_smoke_test_cleans_its_container_after_interrupt(self):
        with mock.patch.object(setup, "execute", side_effect=KeyboardInterrupt()), \
                mock.patch.object(setup.doctor, "probe") as cleanup:
            with self.assertRaises(KeyboardInterrupt):
                setup.smoke_test("test:local", "/project", "codex")
        self.assertEqual(cleanup.call_args.args[0][:3], ["docker", "rm", "-f"])
        self.assertTrue(cleanup.call_args.args[0][3].startswith("swarmforge-setup-"))

    def test_successful_build_is_rechecked(self):
        missing = doctor.Report(settings={"codex": {"image": "test:local"}})
        missing.add("codex.image", "FAIL", "missing")
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(setup.doctor, "inspect", side_effect=[missing, self.report]) as inspect, \
                mock.patch("builtins.input", side_effect=["n", "y", "n"]), \
                mock.patch.object(setup, "execute", return_value=True):
            result = main(["setup", "--harness", "codex"], out=io.StringIO(), err=io.StringIO(), checkout=REPO_ROOT)
        self.assertEqual(result, 0)
        self.assertEqual(inspect.call_count, 2)

    def test_checkout_option_overrides_entrypoint_default(self):
        options = build_parser("/default").parse_args(["doctor", "--checkout", "/chosen"])
        self.assertEqual(options.checkout, "/chosen")

    def test_harness_selection_retries_invalid_input(self):
        with mock.patch("builtins.input", side_effect=["invalid", "codex,claude"]), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(setup.choose_harnesses(), ["codex", "claude"])


if __name__ == "__main__":
    unittest.main()
