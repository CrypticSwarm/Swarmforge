#!/usr/bin/env python3
"""Unit tests for swarmforge.worktrees. Run: python3 tests/test_worktrees.py"""

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

# The CLI's entry-point shim puts the repo root on the path; standing in for it
# here keeps this file runnable on its own, not just under a discovery run that
# already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import gitguard
from swarmforge import worktrees


# git runs with the developer's global config otherwise, where init.defaultBranch
# or a signing key would change what these repos come out looking like. The module
# under test reads the process environment, so the tests install this there too.
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


def git_status(cwd, *args):
    """The exit code of a git command that is allowed to fail."""
    return subprocess.run(
        ["git", "-C", cwd] + list(args),
        capture_output=True, text=True, env=GIT_ENV).returncode


def config_value(path, key):
    """A single value out of one config file, or None if it is not set there."""
    completed = subprocess.run(
        ["git", "config", "--file", path, "--get", key],
        capture_output=True, text=True, env=GIT_ENV)
    return completed.stdout.strip() if completed.returncode == 0 else None


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


@contextlib.contextmanager
def stdout_to(path):
    """Collect whatever reaches fd 1 in `path`.

    The module hands git a raw fd, so a `sys.stdout` the test replaced would
    not see it.
    """
    with open(path, "w") as sink:
        saved = os.dup(1)
        os.dup2(sink.fileno(), 1)
        try:
            yield
        finally:
            os.dup2(saved, 1)
            os.close(saved)


class LayoutCase(unittest.TestCase):
    def setUp(self):
        # realpath: git reports resolved paths, and the guard compares them.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="worktrees-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
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

    def is_bare(self, path):
        return git(path, "rev-parse", "--is-bare-repository") == "true"

    def refs(self, git_dir, namespace):
        return git(git_dir, "for-each-ref", "--format=%(refname)",
                   namespace).splitlines()

    def assert_bare_layout(self, dest, default):
        """The shape both commands build, whatever filled it."""
        git_dir = os.path.join(dest, ".git")
        worktree = os.path.join(dest, default)
        self.assertTrue(os.path.isdir(git_dir))
        self.assertTrue(os.path.isdir(worktree))
        self.assertTrue(self.is_bare(git_dir))
        self.assertFalse(self.is_bare(worktree))
        self.assertEqual(git(git_dir, "symbolic-ref", "HEAD"),
                         "refs/heads/%s" % default)
        # core.bare is moved rather than copied: left in the shared config it
        # would make the linked worktrees read as bare once the extension is on.
        config = os.path.join(git_dir, "config")
        self.assertIsNone(config_value(config, "core.bare"))
        self.assertEqual(config_value(config, "extensions.worktreeConfig"),
                         "true")
        self.assertEqual(
            config_value(os.path.join(git_dir, "config.worktree"), "core.bare"),
            "true")


class GuessCloneDir(unittest.TestCase):
    def test_names_the_directory_git_would_clone_into(self):
        for url, expected in (
            ("git@github.com:o/r.git", "r"),
            # scp-like, the repository right after the colon: only splitting
            # on the colon too finds it.
            ("git@github.com:repo.git", "repo"),
            ("https://h/o/r.git ", "r"),
            ("https://h/o/r/", "r"),
            ("https://h/o/r//", "r"),
            ("https://h/o/r", "r"),
            ("ssh://git@host:2222/a/b.git", "b"),
            ("file:///x/y.git", "y"),
            ("/x/y/.git", "y"),
            ("/x/y/", "y"),
            ("r.git", "r"),
            ("r", "r"),
        ):
            with self.subTest(url=url):
                self.assertEqual(worktrees.guess_clone_dir(url), expected)

    def test_a_url_naming_nothing_is_rejected(self):
        for url in ("", "/", ".git", "/.git", "///"):
            with self.subTest(url=url):
                with self.assertRaises(worktrees.LayoutError):
                    worktrees.guess_clone_dir(url)


class Clone(LayoutCase):
    def setUp(self):
        super().setUp()
        self.url = self.origin()
        self.dest = self.path("work")

    def test_builds_a_bare_repo_with_the_default_branch_beside_it(self):
        path = self.clone(self.url, self.dest)
        self.assertEqual(path, os.path.join(self.dest, "main"))
        self.assert_bare_layout(self.dest, "main")

    def test_the_worktree_holds_the_only_local_branch(self):
        self.clone(self.url, self.dest)
        git_dir = os.path.join(self.dest, ".git")
        # `git clone --bare` would have copied every remote branch in here,
        # leaving the names the sibling worktrees want already taken.
        self.assertEqual(self.refs(git_dir, "refs/heads"), ["refs/heads/main"])
        self.assertEqual(self.refs(git_dir, "refs/remotes/origin"), [
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            "refs/remotes/origin/release",
            "refs/remotes/origin/topic",
        ])

    def test_the_worktree_tracks_the_branch_it_came_from(self):
        path = self.clone(self.url, self.dest)
        self.assertEqual(git(path, "config", "--get", "branch.main.remote"),
                         "origin")
        self.assertEqual(git(path, "config", "--get", "branch.main.merge"),
                         "refs/heads/main")
        self.assertEqual(git(path, "status", "--porcelain"), "")

    def test_fetching_from_the_worktree_works(self):
        path = self.clone(self.url, self.dest)
        # The refspec `git clone --bare` omits: without it a fetch here would
        # succeed and update nothing.
        git(self.url, "commit", "-q", "--allow-empty", "-m", "later")
        git(path, "fetch", "-q", "origin")
        self.assertEqual(
            git(path, "rev-parse", "origin/main"),
            git(self.url, "rev-parse", "main"))

    def test_a_default_branch_with_a_slash_keeps_its_whole_name(self):
        url = self.origin(default="release/1.0", others=(), name="slashed")
        dest = self.path("slashed-work")
        path = self.clone(url, dest)
        self.assertEqual(path, os.path.join(dest, "release", "1.0"))
        self.assert_bare_layout(dest, "release/1.0")
        self.assertEqual(
            git(path, "config", "--get", "branch.release/1.0.remote"),
            "origin")
        self.assertEqual(
            git(path, "config", "--get", "branch.release/1.0.merge"),
            "refs/heads/release/1.0")

    def test_an_existing_branch_is_checked_out_as_it_stands(self):
        self.clone(self.url, self.dest)
        git_dir = os.path.join(self.dest, ".git")
        git(git_dir, "branch", "topic", "origin/topic")
        path = os.path.join(self.dest, "topic")
        with quiet():
            worktrees.add_worktree(git_dir, "topic", path)
        self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), "topic")
        self.assertEqual(git(path, "rev-parse", "HEAD"),
                         git(git_dir, "rev-parse", "refs/heads/topic"))

    def test_branch_option_checks_out_that_branch_instead(self):
        path = self.clone(self.url, self.dest, branch="topic")
        self.assertEqual(path, os.path.join(self.dest, "topic"))
        self.assert_bare_layout(self.dest, "topic")
        git_dir = os.path.join(self.dest, ".git")
        self.assertEqual(self.refs(git_dir, "refs/heads"), ["refs/heads/topic"])

    def test_a_remote_with_no_branches_is_reported_as_such(self):
        empty = self.path("empty.git")
        os.makedirs(empty)
        git(empty, "init", "-q", "--bare")
        with self.assertRaises(worktrees.GitError) as caught:
            self.clone(empty, self.dest)
        self.assertIn("no branches", str(caught.exception))

    def test_a_failing_clone_leaves_nothing_behind(self):
        with self.assertRaises(worktrees.GitError):
            self.clone(self.path("nowhere"), self.dest)
        self.assertFalse(os.path.exists(self.dest))

    def test_a_failing_clone_removes_the_parents_it_created(self):
        with self.assertRaises(worktrees.GitError):
            self.clone(self.path("nowhere"), self.path("a", "b", "c"))
        self.assertFalse(os.path.exists(self.path("a")))

    def test_a_failing_clone_keeps_a_parent_it_found(self):
        os.makedirs(self.path("existing"))
        with self.assertRaises(worktrees.GitError):
            self.clone(self.path("nowhere"), self.path("existing", "c"))
        self.assertEqual(os.listdir(self.path("existing")), [])

    def test_nothing_the_build_prints_lands_on_stdout(self):
        # The path the CLI prints is all its caller should be able to pipe.
        log = self.path("stdout")
        with quiet():
            with stdout_to(log):
                worktrees.clone(self.url, self.dest)
        with open(log) as handle:
            self.assertEqual(handle.read(), "")


class Init(LayoutCase):
    def setUp(self):
        super().setUp()
        self.dest = self.path("work")

    def global_config(self, *settings):
        """Point git's global config at a temp file holding `settings`."""
        path = self.path("gitconfig")
        with open(path, "w") as handle:
            handle.write("".join(settings))
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": path})
        environment.start()
        self.addCleanup(environment.stop)

    def test_builds_the_layout_with_an_unborn_default_branch(self):
        self.global_config("[init]\n\tdefaultBranch = main\n")
        path = self.init(self.dest)
        self.assertEqual(path, os.path.join(self.dest, "main"))
        self.assert_bare_layout(self.dest, "main")
        self.assertTrue(os.path.isdir(
            os.path.join(self.dest, ".git", "worktrees", "main")))
        self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertNotEqual(git_status(path, "rev-parse", "--verify", "HEAD"), 0)
        self.assertEqual(git(path, "status", "--porcelain"), "")

    def test_the_first_commit_in_the_worktree_creates_the_branch(self):
        path = self.init(self.dest)
        default = os.path.basename(path)
        with open(os.path.join(path, "file"), "w") as handle:
            handle.write("hello\n")
        git(path, "add", "file")
        git(path, "commit", "-q", "-m", "root")
        self.assertEqual(git(path, "rev-parse", "HEAD"),
                         git(os.path.join(self.dest, ".git"), "rev-parse",
                             "refs/heads/%s" % default))

    def test_init_default_branch_names_the_branch(self):
        self.global_config("[init]\n\tdefaultBranch = trunk\n")
        path = self.init(self.dest)
        self.assertEqual(path, os.path.join(self.dest, "trunk"))
        self.assert_bare_layout(self.dest, "trunk")

    def test_branch_option_wins_over_init_default_branch(self):
        self.global_config("[init]\n\tdefaultBranch = trunk\n")
        path = self.init(self.dest, branch="dev")
        self.assertEqual(path, os.path.join(self.dest, "dev"))
        self.assert_bare_layout(self.dest, "dev")


class ExistingDestination(LayoutCase):
    def build(self, dest):
        with self.assertRaises(worktrees.LayoutError) as caught:
            self.init(dest)
        self.assertIn(dest, str(caught.exception))
        with self.assertRaises(worktrees.LayoutError):
            self.clone(self.origin(), dest)

    def test_a_file_is_refused_and_left_alone(self):
        dest = self.path("work")
        with open(dest, "w") as handle:
            handle.write("mine\n")
        self.build(dest)
        with open(dest) as handle:
            self.assertEqual(handle.read(), "mine\n")

    def test_an_empty_directory_is_refused_and_left_alone(self):
        dest = self.path("work")
        os.makedirs(dest)
        self.build(dest)
        self.assertEqual(os.listdir(dest), [])


class Claiming(LayoutCase):
    """What the build creates, and what it takes back when it fails."""

    def test_a_destination_under_a_file_is_reported_as_such(self):
        blocker = self.path("file")
        with open(blocker, "w") as handle:
            handle.write("mine\n")
        with self.assertRaises(worktrees.LayoutError) as caught:
            self.init(os.path.join(blocker, "work"))
        self.assertIn(blocker, str(caught.exception))

    def test_a_failing_build_keeps_a_parent_filled_while_it_ran(self):
        sibling = self.path("a", "other")

        def make_sibling_then_fail(git_dir, initial_branch=None):
            os.makedirs(sibling)
            raise worktrees.GitError("no")

        with mock.patch.object(worktrees, "init_bare",
                               make_sibling_then_fail):
            with self.assertRaises(worktrees.GitError):
                self.init(self.path("a", "b"))
        self.assertTrue(os.path.isdir(sibling))
        self.assertFalse(os.path.exists(self.path("a", "b")))


class InheritedGitEnvironment(LayoutCase):
    """The build lands where it was told, not where `GIT_DIR` points.

    `git -C` does not override the variables that name a repository, so a
    build run from a hook would otherwise configure and fetch into whichever
    repo invoked it.
    """

    def test_a_clone_ignores_an_inherited_git_dir(self):
        url = self.origin()
        dest = self.path("work")
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(url, ".git")}):
            path = self.clone(url, dest)
        self.assertEqual(path, os.path.join(dest, "main"))
        self.assert_bare_layout(dest, "main")
        self.assertEqual(git(url, "remote"), "")

    def test_a_clone_ignores_inherited_alternate_object_directories(self):
        url = self.origin()
        dest = self.path("work")
        objects = os.path.join(url, ".git", "objects")
        with mock.patch.dict(
                os.environ,
                {"GIT_ALTERNATE_OBJECT_DIRECTORIES": objects}):
            path = self.clone(url, dest)
        # The fetch would otherwise find the origin's objects already readable
        # and transfer nothing, leaving a clone that only works while the
        # variable is set. `git` here runs without it.
        self.assertEqual(git_status(path, "cat-file", "-e", "HEAD"), 0)

    def test_a_clone_ignores_an_inherited_namespace(self):
        url = self.origin()
        dest = self.path("work")
        with mock.patch.dict(os.environ, {"GIT_NAMESPACE": "ns"}):
            path = self.clone(url, dest)
        self.assertEqual(path, os.path.join(dest, "main"))
        self.assert_bare_layout(dest, "main")
        self.assertIn("refs/remotes/origin/HEAD",
                      self.refs(os.path.join(dest, ".git"), "refs/remotes"))


class Repair(LayoutCase):
    """`make_bare_by_worktree_config` over a repo that already has it."""

    def config_files(self, git_dir):
        contents = []
        for name in ("config", "config.worktree"):
            path = os.path.join(git_dir, name)
            with open(path) as handle:
                contents.append(handle.read())
        return contents

    def test_a_second_run_on_a_bare_dir_leaves_the_same_config(self):
        git_dir = self.path("bare.git")
        worktrees.init_bare(git_dir)
        worktrees.make_bare_by_worktree_config(git_dir)
        before = self.config_files(git_dir)
        # `config --unset core.bare` exits 5 the second time round, with the
        # key already gone; the run treats that as the work done.
        worktrees.make_bare_by_worktree_config(git_dir)
        self.assertEqual(self.config_files(git_dir), before)

    def test_a_second_run_on_a_built_layout_keeps_it_bare(self):
        dest = self.path("work")
        self.clone(self.origin(), dest)
        worktrees.make_bare_by_worktree_config(os.path.join(dest, ".git"))
        self.assert_bare_layout(dest, "main")


class GuardedByGitGuard(LayoutCase):
    """The layout survives the mounts an anvil session puts over it.

    The read-only `commondir` the guard plants in every git dir is what would
    otherwise flip the bare repository to reading as a checkout of `work/`.
    """

    def test_the_guard_has_nothing_to_warn_about(self):
        path = self.clone(self.origin(), self.path("work"))
        warnings = []
        gitguard.build_mounts(path, ["/workspace"], warn=warnings.append)
        self.assertFalse([w for w in warnings if "core.bare" in w], warnings)
        self.assertTrue(os.path.isfile(
            os.path.join(self.path("work"), ".git", "commondir")))
        self.assertTrue(self.is_bare(os.path.join(self.path("work"), ".git")))
        self.assertFalse(self.is_bare(path))


if __name__ == "__main__":
    if shutil.which("git") is None:
        sys.stderr.write("git is required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
