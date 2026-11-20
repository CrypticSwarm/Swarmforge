#!/usr/bin/env python3
"""Fixtures shared by the test modules that build real git repositories."""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the CLI's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import worktrees


# Otherwise a developer's init.defaultBranch or signing key shapes these repos.
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL="/dev/null",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_AUTHOR_NAME="Test",
    GIT_AUTHOR_EMAIL="test@example.com",
    GIT_COMMITTER_NAME="Test",
    GIT_COMMITTER_EMAIL="test@example.com",
)


def git(cwd, *args):
    """Run a git command the test depends on, returning its stdout stripped."""
    return subprocess.run(
        ["git", "-C", cwd] + list(args), check=True,
        capture_output=True, text=True, env=GIT_ENV).stdout.strip()


@contextlib.contextmanager
def quiet():
    """Send the output the module's git commands inherit to /dev/null.

    Under the test runner that output is the runner's own, where a fatal error
    a test went looking for reads as something having gone wrong.
    """
    with open(os.devnull, "w") as sink:
        saved = [os.dup(1), os.dup(2)]
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            yield
        finally:
            for fd, original in enumerate(saved, start=1):
                os.dup2(original, fd)
                os.close(original)


class LayoutCase(unittest.TestCase):
    """A scratch directory to build repositories in, with git's config neutered."""

    def setUp(self):
        # realpath: git reports resolved paths, and the guard compares them.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="worktrees-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # worktrees.git() filters os.environ itself, so the neutering has to land there.
        environment = mock.patch.dict(os.environ, GIT_ENV)
        environment.start()
        self.addCleanup(environment.stop)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def origin(self, default="main", others=("topic", "release"),
               name="origin"):
        """A repository to clone: one commit on `default`, plus `others`."""
        path = self.path(name)
        os.makedirs(path)
        git(path, "init", "-q", "--initial-branch", default)
        git(path, "commit", "-q", "--allow-empty", "-m", "root")
        for name in others:
            git(path, "branch", name)
        return path

    def clone(self, url, dest, branch=None):
        with quiet():
            return worktrees.clone(url, dest, branch=branch)

    def init(self, dest, branch=None):
        with quiet():
            return worktrees.init(dest, branch=branch)
