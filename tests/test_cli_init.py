#!/usr/bin/env python3
"""Unit tests for swarmforge.cli.init. Run: python3 tests/test_cli_init.py"""

import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The command's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import cli
from swarmforge import worktrees


class InitCase(unittest.TestCase):
    """The layout work is faked out; what is under test is the wiring.

    The fake goes on `swarmforge.worktrees`, the module that owns the name,
    not on the command module that imported it: `cli.init` reaches it through
    the module object, so one fake covers every caller.
    """

    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.calls = []

    def main(self, argv):
        return cli.main(argv, out=self.out, err=self.err)

    def fake_init(self, result="/repos/new/main", error=None):
        def init(dest, branch=None):
            self.calls.append((dest, branch))
            if error is not None:
                raise error
            return result
        return mock.patch.object(worktrees, "init", init)


class ArgumentTests(InitCase):
    def test_path_is_passed_through_with_no_branch(self):
        with self.fake_init():
            self.assertEqual(self.main(["init", "work/new"]), 0)
        self.assertEqual(self.calls, [("work/new", None)])

    def test_branch_is_passed_through(self):
        with self.fake_init():
            self.assertEqual(self.main(["init", "new", "--branch", "trunk"]), 0)
        self.assertEqual(self.calls, [("new", "trunk")])

    def test_path_is_required(self):
        self.assertEqual(self.main(["init"]), 2)
        self.assertIn("usage: swarmforge init", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_an_empty_argument_is_a_usage_error(self):
        """Empty is not a directory or a branch; git would take it as one."""
        for argv in (["init", ""], ["init", "new", "--branch", ""]):
            with self.subTest(argv=argv):
                self.err = io.StringIO()
                self.assertEqual(self.main(argv), 2)
                self.assertIn("must not be empty", self.err.getvalue())
        self.assertEqual(self.calls, [])


class OutputTests(InitCase):
    def test_the_worktree_path_is_printed(self):
        with self.fake_init("/repos/new/main"):
            self.assertEqual(self.main(["init", "new"]), 0)
        self.assertEqual(self.out.getvalue(), "/repos/new/main\n")
        self.assertEqual(self.err.getvalue(), "")

    def test_a_git_failure_is_one_line_on_err(self):
        error = worktrees.GitError("git worktree add --orphan exited 129")
        with self.fake_init(error=error):
            self.assertEqual(self.main(["init", "new"]), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.err.getvalue(),
                         "swarmforge: git worktree add --orphan exited 129\n")

    def test_a_refused_destination_is_one_line_on_err(self):
        error = worktrees.LayoutError("/repos/new already exists")
        with self.fake_init(error=error):
            self.assertEqual(self.main(["init", "new"]), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("/repos/new already exists", self.err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
