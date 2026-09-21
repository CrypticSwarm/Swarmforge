#!/usr/bin/env python3
"""Unit tests for swarmforge.cli.main. Run: python3 tests/test_cli_main.py"""

import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the command's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import cli
from swarmforge import worktrees
from swarmforge.cli import clone as clone_command


class CliCase(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()

    def main(self, argv):
        return cli.main(argv, out=self.out, err=self.err)


class RegistryTests(CliCase):
    def test_every_registered_command_exposes_the_two_hooks(self):
        """The registry is the whole contract between main and a subcommand."""
        for name, module in cli.COMMANDS.items():
            self.assertTrue(callable(getattr(module, "configure", None)),
                            "%s has no configure()" % name)
            self.assertTrue(callable(getattr(module, "run", None)),
                            "%s has no run()" % name)
            # build_parser hands it to argparse as the subcommand's help.
            self.assertTrue((module.__doc__ or "").strip(),
                            "%s has no docstring" % name)

    def test_the_parser_carries_a_subparser_for_each_command(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["--help"])
        for name in cli.COMMANDS:
            self.assertIn(name, out.getvalue())


class UsageTests(CliCase):
    def test_no_subcommand_is_a_usage_error(self):
        self.assertEqual(self.main([]), 2)
        self.assertIn("usage: swarmforge", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_unknown_subcommand_is_a_usage_error(self):
        self.assertEqual(self.main(["bogus"]), 2)
        self.assertIn("bogus", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_help_exits_zero_and_lists_the_subcommands(self):
        self.assertEqual(self.main(["--help"]), 0)
        printed = self.out.getvalue()
        self.assertIn("clone", printed)
        self.assertIn("init", printed)
        self.assertEqual(self.err.getvalue(), "")

    def test_argparse_output_goes_to_the_streams_it_was_given(self):
        """Argparse's help and errors go to the given streams, not the process's.

        Only what the CLI writes in python: a subprocess it starts is still
        handed the real fds, which is how git reaches the terminal.
        """
        process_out, process_err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", process_out), \
                mock.patch.object(sys, "stderr", process_err):
            self.main(["--help"])
            self.main([])
        self.assertEqual(process_out.getvalue(), "")
        self.assertEqual(process_err.getvalue(), "")


class DispatchTests(CliCase):
    def test_dispatch_reaches_the_command_modules_run(self):
        seen = []

        def fake_run(options):
            seen.append(options)
            return 7

        with mock.patch.object(clone_command, "run", fake_run):
            self.assertEqual(self.main(["clone", "git@host:o/r.git"]), 7)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].command, "clone")
        self.assertEqual(seen[0].url, "git@host:o/r.git")

    def test_a_commands_exit_code_is_the_commands_own(self):
        with mock.patch.object(clone_command, "run", lambda options: 0):
            self.assertEqual(self.main(["clone", "url"]), 0)


class FailureTests(CliCase):
    def fake_clone(self, error):
        def clone(url, dest, branch=None):
            raise error
        return mock.patch.object(worktrees, "clone", clone)

    def test_an_interrupt_is_one_line_and_exit_130(self):
        with self.fake_clone(KeyboardInterrupt()):
            self.assertEqual(self.main(["clone", "https://h/o/r"]), 130)
        self.assertEqual(self.err.getvalue(), "swarmforge: interrupted\n")
        self.assertEqual(self.out.getvalue(), "")

    def test_anything_else_keeps_its_traceback(self):
        """One line is for a failure the caller can act on; a bug is not one."""
        with self.fake_clone(RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                self.main(["clone", "https://h/o/r"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
