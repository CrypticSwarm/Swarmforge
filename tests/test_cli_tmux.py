#!/usr/bin/env python3
"""Unit tests for swarmforge.cli.tmux. Run: python3 tests/test_cli_tmux.py"""

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
from swarmforge import tmux


class TmuxCommandCase(unittest.TestCase):
    """The session work is faked out on `swarmforge.tmux`; what is under test is the wiring."""

    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.calls = []

        def open_session(path, name=None):
            self.calls.append(("open", path, name))
            return name or "repo"

        def attach(session):
            self.calls.append(("attach", session))

        for name, fake in (("open_session", open_session), ("attach", attach)):
            patch = mock.patch.object(tmux, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def main(self, argv):
        return cli.main(argv, out=self.out, err=self.err)


class OpenTests(TmuxCommandCase):
    def test_path_defaults_to_the_current_directory_and_attaches(self):
        self.assertEqual(self.main(["tmux", "open"]), 0)
        self.assertEqual(self.calls, [("open", ".", None), ("attach", "repo")])
        self.assertEqual(self.out.getvalue(), "")

    def test_path_and_name_are_passed_through(self):
        self.assertEqual(self.main(["tmux", "open", "~/r", "--name", "work"]), 0)
        self.assertEqual(self.calls[0], ("open", "~/r", "work"))

    def test_detach_prints_the_session_instead_of_attaching(self):
        for flag in ("-d", "--detach"):
            with self.subTest(flag=flag):
                self.out, self.calls[:] = io.StringIO(), []
                self.assertEqual(self.main(["tmux", "open", flag]), 0)
                self.assertEqual(self.calls, [("open", ".", None)])
                self.assertEqual(self.out.getvalue(), "repo\n")

    def test_a_tmux_failure_is_one_line_and_exit_1(self):
        with mock.patch.object(tmux, "open_session",
                               side_effect=tmux.TmuxError("/x is not a directory")):
            self.assertEqual(self.main(["tmux", "open", "/x"]), 1)
        self.assertEqual(self.err.getvalue(), "swarmforge: /x is not a directory\n")

    def test_an_empty_argument_is_a_usage_error(self):
        for argv in (["tmux", "open", ""], ["tmux", "open", "--name", ""]):
            with self.subTest(argv=argv):
                self.err = io.StringIO()
                self.assertEqual(self.main(argv), 2)
                self.assertIn("must not be empty", self.err.getvalue())
        self.assertEqual(self.calls, [])

    def test_an_action_is_required(self):
        self.assertEqual(self.main(["tmux"]), 2)
        self.assertIn("usage: swarmforge tmux", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
