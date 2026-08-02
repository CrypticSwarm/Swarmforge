#!/usr/bin/env python3
"""Tests for the Makefile's run_agent_container docker argv.

Run: python3 scripts/test_run_agent_container.py

These drive `make run_opencode` / `make run_claude` for real against throwaway
git checkouts, with `docker` stubbed out on PATH and PYTHON pointed at a script
that records the argv the recipe hands the launcher. Nothing is started; the
assertions are about which `-v` flags the recipe builds.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
MAKEFILE = os.path.join(REPO_ROOT, "Makefile")

DOCKER_STUB = "#!/bin/sh\nexit 0\n"

# Stands in for PYTHON in the recipe, which invokes it as
# `$(PYTHON) scripts/run_anvil.py <launcher flags> -- docker run ...`. The
# recipe also runs the git guard through PYTHON; that one is the code under
# test, so it is passed through to a real interpreter rather than recorded.
CAPTURE_STUB = """#!/bin/sh
case "$1" in
  *git_guard.py) exec "$PYTHON_REAL" "$@" ;;
esac
: > "$CAPTURE_FILE"
for arg in "$@"; do printf '%s\\0' "$arg" >> "$CAPTURE_FILE"; done
"""


def _write_exec(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


# A developer's global git config (a signing key, a templateDir, a hooksPath)
# would otherwise decide what these throwaway repos come out looking like.
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL="/dev/null",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_AUTHOR_NAME="Test",
    GIT_AUTHOR_EMAIL="test@example.com",
    GIT_COMMITTER_NAME="Test",
    GIT_COMMITTER_EMAIL="test@example.com",
)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", cwd] + list(args),
        check=True, capture_output=True, text=True, env=GIT_ENV,
    )


class MakeRecipeCase(unittest.TestCase):
    """Runs a make target and exposes the docker argv it assembled."""

    def setUp(self):
        # realpath: make resolves CURDIR and git resolves the worktree root, so
        # a symlinked TMPDIR (the default on macOS) would not match otherwise.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-make-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        _write_exec(os.path.join(self.bin, "docker"), DOCKER_STUB)
        self.capture_path = os.path.join(self.tmp, "argv")
        self.capture = os.path.join(self.bin, "capture-argv")
        _write_exec(self.capture, CAPTURE_STUB)

    def make_repo(self, name="proj"):
        """A git checkout with one commit, so `git worktree add` works."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        _git(path, "init", "-q")
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "Test")
        _git(path, "commit", "-q", "--allow-empty", "-m", "root")
        return path

    def docker_argv(self, target, project_dir):
        """The argv after the launcher's `--`, i.e. the anvil's `docker run`."""
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
            "CAPTURE_FILE": self.capture_path,
            "PYTHON_REAL": sys.executable,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        # A deliberately minimal env: SWARMFORGE_* vars inherited from an outer
        # session would shadow the target-specific defaults under test.
        completed = subprocess.run(
            ["make", "-C", project_dir, "-f", MAKEFILE, target,
             "PYTHON=" + self.capture],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            "make failed:\n%s\n%s" % (completed.stdout, completed.stderr),
        )
        with open(self.capture_path) as handle:
            recorded = handle.read().split("\0")[:-1]
        self.assertIn("--", recorded, "launcher argv had no `--` separator")
        return recorded[recorded.index("--") + 1:]

    def mounts(self, argv):
        """Every `-v` value passed to `docker run`, in order.

        Stops at the image, so a mount that drifted past it -- where it would
        be an argument to the harness rather than to docker -- is not counted.
        """
        image = next(
            i for i, word in enumerate(argv) if word.endswith(":local"))
        return [argv[i + 1] for i, word in enumerate(argv[:image])
                if word == "-v"]


class GitDirMounts(MakeRecipeCase):
    """The git guard's mounts reach the docker argv, for every workspace path."""

    def test_workspace_git_dir_is_anchored_with_its_readonly_paths(self):
        repo = self.make_repo()
        mounts = self.mounts(self.docker_argv("run_opencode", repo))
        self.assertIn("%s:/workspace" % repo, mounts)
        self.assertIn("%s/.git:/workspace/.git" % repo, mounts)
        for name in ("config", "hooks", "commondir"):
            self.assertIn(
                "%s/.git/%s:/workspace/.git/%s:ro" % (repo, name, name),
                mounts,
            )

    def test_repo_slug_path_gets_its_own_readonly_paths(self):
        # run_claude mounts the workspace a second time at /repos/<slug>; a
        # read-only mount on one says nothing about the other, so the guard is
        # told about every target the recipe mounts.
        repo = self.make_repo()
        mounts = self.mounts(self.docker_argv("run_claude", repo))
        slug_path = "/repos/proj"
        self.assertIn("%s:%s" % (repo, slug_path), mounts)
        self.assertIn("%s/.git:%s/.git" % (repo, slug_path), mounts)
        self.assertIn(
            "%s/.git/config:%s/.git/config:ro" % (repo, slug_path), mounts)

    def test_subdirectory_project_dir_still_guards_the_repo_root(self):
        repo = self.make_repo()
        nested = os.path.join(repo, "src", "deep")
        os.makedirs(nested)
        mounts = self.mounts(self.docker_argv("run_opencode", nested))
        self.assertIn("%s/.git/config:/workspace/.git/config:ro" % repo, mounts)

    def test_non_git_project_dir_gets_no_git_dir_mounts(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        mounts = self.mounts(self.docker_argv("run_opencode", plain))
        self.assertIn("%s:/workspace" % plain, mounts)
        self.assertEqual([m for m in mounts if "/.git" in m], [])


class WorktreeGitDirMounts(MakeRecipeCase):
    """A linked worktree keeps config/hooks in the shared common git dir."""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.worktree = os.path.join(self.tmp, "wt")
        _git(self.repo, "worktree", "add", "-q", "-b", "topic", self.worktree)
        self.common = os.path.join(self.repo, ".git")

    def test_common_dir_config_and_hooks_are_readonly(self):
        mounts = self.mounts(self.docker_argv("run_opencode", self.worktree))
        self.assertIn("%s:%s" % (self.common, self.common), mounts)
        self.assertIn(
            "%s/config:%s/config:ro" % (self.common, self.common), mounts)
        self.assertIn(
            "%s/hooks:%s/hooks:ro" % (self.common, self.common), mounts)

    def test_gitdir_pointer_file_is_readonly(self):
        # In a linked worktree `.git` is a file holding `gitdir: <path>`.
        # Read-only keeps the container from repointing itself at a git dir
        # whose config and hooks are not covered by the overlays above.
        self.assertTrue(os.path.isfile(os.path.join(self.worktree, ".git")))
        mounts = self.mounts(self.docker_argv("run_opencode", self.worktree))
        self.assertIn("%s/.git:/workspace/.git:ro" % self.worktree, mounts)
        self.assertNotIn("%s/.git:/workspace/.git" % self.worktree, mounts)


if __name__ == "__main__":
    if shutil.which("make") is None or shutil.which("git") is None:
        sys.stderr.write("make and git are required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
