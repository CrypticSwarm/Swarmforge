#!/usr/bin/env python3
"""Unit tests for swarmforge.cli.clone. Run: python3 tests/test_cli_clone.py"""

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


class CloneCase(unittest.TestCase):
    """The layout work is faked out; what is under test is the wiring.

    The fakes go on `swarmforge.worktrees`, the module that owns these names,
    not on the command module that imported it: `cli.clone` reaches them
    through the module object, so one fake covers every caller.
    """

    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.calls = []

    def main(self, argv):
        return cli.main(argv, out=self.out, err=self.err)

    def fake_clone(self, result="/repos/r/main", error=None):
        def clone(url, dest, branch=None):
            self.calls.append((url, dest, branch))
            if error is not None:
                raise error
            return result
        return mock.patch.object(worktrees, "clone", clone)

    def fake_guess(self, result="guessed"):
        def guess_clone_dir(url):
            self.calls.append(("guess", url))
            return result
        return mock.patch.object(worktrees, "guess_clone_dir", guess_clone_dir)


class ArgumentTests(CloneCase):
    def test_path_defaults_to_the_name_git_would_pick(self):
        with self.fake_guess("r"), self.fake_clone():
            self.assertEqual(self.main(["clone", "git@host:o/r.git"]), 0)
        self.assertEqual(self.calls,
                         [("guess", "git@host:o/r.git"), ("git@host:o/r.git", "r", None)])

    def test_an_explicit_path_is_used_as_given(self):
        with self.fake_guess(), self.fake_clone():
            self.assertEqual(self.main(["clone", "https://h/o/r", "work/r"]), 0)
        self.assertEqual(self.calls, [("https://h/o/r", "work/r", None)])

    def test_branch_is_passed_through(self):
        with self.fake_guess("r"), self.fake_clone():
            self.assertEqual(
                self.main(["clone", "https://h/o/r", "--branch", "release"]), 0)
        self.assertEqual(self.calls[-1], ("https://h/o/r", "r", "release"))

    def test_url_is_required(self):
        self.assertEqual(self.main(["clone"]), 2)
        self.assertIn("usage: swarmforge clone", self.err.getvalue())

    def test_an_empty_argument_is_a_usage_error(self):
        """Empty is not a URL, a directory or a branch; git would take it as one."""
        for argv in (["clone", ""],
                     ["clone", "https://h/o/r", ""],
                     ["clone", "https://h/o/r", "--branch", ""]):
            with self.subTest(argv=argv):
                self.err = io.StringIO()
                self.assertEqual(self.main(argv), 2)
                self.assertIn("must not be empty", self.err.getvalue())
        self.assertEqual(self.calls, [])

    def test_help_names_the_default_for_path(self):
        self.assertEqual(self.main(["clone", "--help"]), 0)
        self.assertIn("the name git would pick", " ".join(self.out.getvalue().split()))


class OutputTests(CloneCase):
    def test_the_worktree_path_is_printed(self):
        with self.fake_guess("r"), self.fake_clone("/repos/r/main"):
            self.assertEqual(self.main(["clone", "https://h/o/r"]), 0)
        self.assertEqual(self.out.getvalue(), "/repos/r/main\n")
        self.assertEqual(self.err.getvalue(), "")

    def test_a_git_failure_is_one_line_on_err(self):
        error = worktrees.GitError("git fetch origin: repository not found")
        with self.fake_guess("r"), self.fake_clone(error=error):
            self.assertEqual(self.main(["clone", "https://h/o/r"]), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.err.getvalue(),
                         "swarmforge: git fetch origin: repository not found\n")

    def test_a_refused_destination_is_one_line_on_err(self):
        error = worktrees.LayoutError("/repos/r already exists")
        with self.fake_guess("r"), self.fake_clone(error=error):
            self.assertEqual(self.main(["clone", "https://h/o/r"]), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("/repos/r already exists", self.err.getvalue())

    def test_a_url_with_no_directory_name_is_one_line_on_err(self):
        """`guess_clone_dir` refuses rather than creating a nameless directory."""
        self.assertEqual(self.main(["clone", "/"]), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("swarmforge: ", self.err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
