#!/usr/bin/env python3
"""Tests for the Makefile's run_agent_container docker argv.

Run: python3 tests/test_run_agent_container.py

These drive `make run_opencode` / `make run_claude` for real against throwaway
git checkouts, with `docker` stubbed out on PATH and PYTHON pointed at a script
that records the argv the recipe hands the launcher. Nothing is started; the
assertions are about which `-v` flags the recipe builds.
"""

import json
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
# `$(PYTHON) bin/run-anvil <launcher flags> -- docker run ...`. The recipe also
# runs the git guard through PYTHON; that one is the code under test, so it is
# passed through to a real interpreter rather than recorded.
CAPTURE_STUB = """#!/bin/sh
case "$1" in
  */bin/git-guard) exec "$PYTHON_REAL" "$@" ;;
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

    def launcher_argv(self, target, project_dir):
        """The whole argv the recipe handed PYTHON, launcher path included."""
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
            return handle.read().split("\0")[:-1]

    def docker_argv(self, target, project_dir):
        """The argv after the launcher's `--`, i.e. the anvil's `docker run`."""
        recorded = self.launcher_argv(target, project_dir)
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


class LauncherEntryPoint(MakeRecipeCase):
    """The recipe reaches the launcher through the shim, not a module path.

    The shim is what puts the checkout's `swarmforge` package on the import
    path, so a recipe that named a module file directly would fail to import
    at launch -- and only on a machine where the package is not installed.
    """

    def test_recipe_invokes_the_run_anvil_shim(self):
        argv = self.launcher_argv("run_opencode", self.make_repo())
        self.assertEqual(argv[0], os.path.join(REPO_ROOT, "bin", "run-anvil"))


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


class ClaudeSettingsMount(MakeRecipeCase):
    """Claude's settings.json belongs to the container, not to the home.

    The entrypoint rebuilds it from the config layers every run, and
    `CLAUDE_HOME_DIR` is one directory every container shares -- so the
    recipe mounts a per-container host file over the path.
    """

    DEST = "/home/opencode/.claude/settings.json"

    def settings_mount(self, project_dir):
        """The one `-v` spec whose destination is Claude's settings.json."""
        return self.settings_mount_and_argv(project_dir)[0]

    def settings_mount_and_argv(self, project_dir):
        argv = self.docker_argv("run_claude", project_dir)
        found = [m for m in self.mounts(argv) if m.split(":")[1] == self.DEST]
        self.assertEqual(len(found), 1, "settings mounts: %r" % found)
        return found[0], argv

    def test_the_mount_lands_where_the_entrypoint_writes(self):
        """The recipe names the path twice and they have to agree.

        Pointed anywhere else, the container reads a file nothing ever wrote
        and comes up with none of its layers -- with nothing reporting it.
        """
        spec, argv = self.settings_mount_and_argv(self.make_repo())
        config_dest = next(
            word.split("=", 1)[1] for i, word in enumerate(argv)
            if word.startswith("SWARMFORGE_CONFIG_DEST=") and argv[i - 1] == "-e"
        )
        self.assertEqual(spec.split(":")[1], config_dest + "/settings.json")

    def test_the_file_handed_to_docker_is_valid_json(self):
        """An empty file is not.

        Any path that skips the rebuild leaves the container reading what the
        recipe put here. Absent is fine; present and unparseable is not.
        """
        source = self.settings_mount(self.make_repo()).split(":")[0]
        with open(source) as handle:
            self.assertEqual(json.load(handle), {})

    def test_a_previous_runs_settings_do_not_reach_the_next_container(self):
        """The host file outlives the container it was built for.

        Nothing reaps it, so a second run would otherwise start on the
        first's merged result, org layer and all.
        """
        repo = self.make_repo()
        source = self.settings_mount(repo).split(":")[0]
        with open(source, "w") as handle:
            handle.write('{"env": {"LEAKED": "from the last run"}}')

        self.settings_mount(repo)

        with open(source) as handle:
            self.assertEqual(json.load(handle), {})

    def test_settings_file_is_mounted_over_the_persistent_home(self):
        spec = self.settings_mount(self.make_repo())
        source = spec.split(":")[0]
        self.assertTrue(
            source.startswith(os.path.join(self.home, ".local", "share", "claude")),
            "settings file is not under CLAUDE_DATA_DIR: %s" % source,
        )
        self.assertNotIn(
            os.path.join("share", "claude", "home"), source,
            "settings file is inside the shared persistent home: %s" % source,
        )

    def test_the_recipe_creates_the_file_before_docker_is_handed_it(self):
        """Docker makes a *directory* out of a missing bind source.

        Claude expects a file at that path, so the container would come up
        unable to read or write its settings at all -- and nothing in the
        recipe would have failed.
        """
        source = self.settings_mount(self.make_repo()).split(":")[0]
        self.assertTrue(os.path.isfile(source), "%s was not created" % source)

    def test_the_settings_mount_is_writable(self):
        """Read-only would break editing settings from inside a session.

        The point is that the edit does not outlive the container, not that
        it cannot be made.
        """
        spec = self.settings_mount(self.make_repo())
        self.assertEqual(
            spec.count(":"), 1, "settings mount carries mount options: %s" % spec)

    def test_two_containers_do_not_share_one_settings_file(self):
        """The leak the mount exists for, stated as two projects.

        Concurrent sessions under different org layers each rebuild this
        file; sharing one path means the second rewrites what the first is
        reading.
        """
        first = self.settings_mount(self.make_repo("alpha")).split(":")[0]
        second = self.settings_mount(self.make_repo("beta")).split(":")[0]
        self.assertNotEqual(first, second)

    def test_opencode_gets_no_settings_mount(self):
        """settings.json is Claude's file; nothing else reads it."""
        mounts = self.mounts(self.docker_argv("run_opencode", self.make_repo()))
        self.assertEqual([m for m in mounts if "settings.json" in m], [])


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
        # In a linked worktree `.git` is a file naming the git dir; read-only
        # keeps the container from repointing it at one nothing here covers.
        self.assertTrue(os.path.isfile(os.path.join(self.worktree, ".git")))
        mounts = self.mounts(self.docker_argv("run_opencode", self.worktree))
        self.assertIn("%s/.git:/workspace/.git:ro" % self.worktree, mounts)
        self.assertNotIn("%s/.git:/workspace/.git" % self.worktree, mounts)


if __name__ == "__main__":
    if shutil.which("make") is None or shutil.which("git") is None:
        sys.stderr.write("make and git are required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
