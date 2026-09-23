#!/usr/bin/env python3
"""Tests for the Makefile's run_agent_container docker argv.

Run: python3 tests/test_run_agent_container.py

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

import make_argv_fixtures

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
MAKEFILE = os.path.join(REPO_ROOT, "Makefile")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import names, tongs  # noqa: E402  (needs the path above)

DOCKER_STUB = "#!/bin/sh\nexit 0\n"

# Stands in for PYTHON, except for the git guard and name helper: those are code under test.
CAPTURE_STUB = """#!/bin/sh
case "$1" in
  */bin/git-guard|*/bin/project-name) exec "$PYTHON_REAL" "$@" ;;
esac
: > "$CAPTURE_FILE"
for arg in "$@"; do printf '%s\\0' "$arg" >> "$CAPTURE_FILE"; done
"""


def _write_exec(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


# Otherwise a developer's signing key, templateDir, or hooksPath shapes these repos.
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
        # realpath: make resolves CURDIR and git the worktree root, so a symlinked TMPDIR misses.
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

    def make_repo(self, name=make_argv_fixtures.PROJECT_SUBDIR):
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
        # Minimal env: inherited SWARMFORGE_* vars would shadow the target defaults under test.
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

    def make_output(self, target, project_dir):
        """The stdout of a target that only prints, with no docker to stub."""
        completed = subprocess.run(
            ["make", "-s", "-C", project_dir, "-f", MAKEFILE, target],
            env={
                "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
                "HOME": self.home,
            },
            capture_output=True, text=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            "make failed:\n%s\n%s" % (completed.stdout, completed.stderr),
        )
        return completed.stdout.strip()

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
        # run_claude mounts the workspace twice; a read-only mount on one leaves the other open.
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
        # In a linked worktree `.git` is a file; read-only stops the container repointing it.
        self.assertTrue(os.path.isfile(os.path.join(self.worktree, ".git")))
        mounts = self.mounts(self.docker_argv("run_opencode", self.worktree))
        self.assertIn("%s/.git:/workspace/.git:ro" % self.worktree, mounts)
        self.assertNotIn("%s/.git:/workspace/.git" % self.worktree, mounts)


class ClaudeSharedHomeMounts(MakeRecipeCase):
    """Nothing under the shared .claude is mounted except plugins/, read-only:
    a session must not rewrite what the next container executes."""

    def shared(self, *parts):
        return os.path.join(
            self.home, ".local", "share", "claude", "home", ".claude", *parts)

    def test_plugins_are_bound_read_only(self):
        mounts = self.mounts(self.docker_argv("run_claude", self.make_repo()))
        self.assertIn(
            "%s:/home/anvil/.claude/plugins:ro" % self.shared("plugins"),
            mounts)

    def test_nothing_else_under_the_shared_claude_dir_reaches_the_container(self):
        argv = self.docker_argv("run_claude", self.make_repo())
        targets = [m.split(":")[1] for m in self.mounts(argv)]
        self.assertEqual(
            [t for t in targets if t.startswith("/home/anvil/.claude")],
            ["/home/anvil/.claude/plugins"])
        self.assertNotIn("--tmpfs", argv)


class MaskedAssetDirs(MakeRecipeCase):
    """A persistent-home harness masks the dirs the entrypoint fills with assets.

    Those destinations are native to the harness, so they sit inside the home
    mount rather than in a config dir rebuilt each run. Without the mask, one
    repo's skills would be left in the home for every later session, against
    any other repo.
    """

    def masked(self, argv):
        image = next(
            i for i, word in enumerate(argv) if word.endswith(":local"))
        return [argv[i + 1].split(":")[0]
                for i, word in enumerate(argv[:image]) if word == "--tmpfs"]

    def test_grok_masks_its_native_skills_and_commands(self):
        masked = self.masked(self.docker_argv("run_grok", self.make_repo()))
        self.assertIn("/home/anvil/.grok/skills", masked)
        self.assertIn("/home/anvil/.grok/commands", masked)

    def test_codex_masks_the_dotagents_skills_dir_it_reads(self):
        # Codex reads the harness-neutral user skills location, not a codex-named one.
        masked = self.masked(self.docker_argv("run_codex", self.make_repo()))
        self.assertIn("/home/anvil/.agents/skills", masked)


class CodexConfigMounts(MakeRecipeCase):
    """Codex config and native state use the writable persistent home."""

    def test_config_is_not_an_individual_bind_mount(self):
        mounts = self.mounts(self.docker_argv("run_codex", self.make_repo()))
        home = os.path.join(self.home, ".local", "share", "codex", "home")
        self.assertIn("%s:/home/anvil" % home, mounts)
        targets = [mount.split(":", 2)[1] for mount in mounts]
        self.assertNotIn("/home/anvil/.codex/config.toml", targets)

    def test_native_state_paths_are_not_individually_mounted(self):
        mounts = self.mounts(self.docker_argv("run_codex", self.make_repo()))
        targets = [mount.split(":", 2)[1] for mount in mounts]
        for state in ("auth.json", "sessions", "history.jsonl", "log"):
            self.assertNotIn("/home/anvil/.codex/%s" % state, targets)

class HostTerminalEnv(MakeRecipeCase):
    """run_* forwards host TERM/COLORTERM via docker `-e NAME` passthrough.

    Values are not baked into argv; docker copies them from run-anvil's
    environment at start time.
    """

    def env_flags(self, argv):
        image = next(
            i for i, word in enumerate(argv) if word.endswith(":local"))
        return [argv[i + 1] for i, word in enumerate(argv[:image])
                if word == "-e"]

    def test_term_and_colorterm_are_passthrough_flags(self):
        repo = self.make_repo()
        for target in ("run_opencode", "run_claude", "run_grok", "run_codex"):
            flags = self.env_flags(self.docker_argv(target, repo))
            self.assertIn("TERM", flags, target)
            self.assertIn("COLORTERM", flags, target)
            self.assertFalse(
                any(flag.startswith("TERM=") for flag in flags), target)
            self.assertFalse(
                any(flag.startswith("COLORTERM=") for flag in flags), target)


class GeneratedNamesIdentifyOneDirectory(MakeRecipeCase):
    """Two checkouts with the same basename get different docker names.

    The worktree layout makes `repo1/master` and `repo2/master` share a
    basename, and the run recipe removes a container of the name it is about to
    use, so a shared name means the second session kills the first. The
    per-session network and each session tong are built from this name, so they
    are checked here too, and the same `PROJECT_DIR` has to reproduce it.
    """

    def container_name(self, target, project_dir):
        argv = self.docker_argv(target, project_dir)
        self.assertIn("--name", argv)
        return argv[argv.index("--name") + 1]

    def derived_names(self, name):
        """The docker names the launcher builds out of a container name."""
        return [
            tongs.session_network_name(name),
            tongs.session_container_name(name, "github"),
        ]

    def test_same_basename_under_different_parents_gets_distinct_names(self):
        first = self.container_name("run_claude", self.make_repo("repo1/master"))
        second = self.container_name("run_claude", self.make_repo("repo2/master"))
        self.assertNotEqual(first, second)
        # Every other session name carries the container name, so it separates with it.
        for name in (first, second):
            for derived in self.derived_names(name):
                self.assertIn(name, derived)
        self.assertEqual(
            set(self.derived_names(first)) & set(self.derived_names(second)),
            set())

    def test_the_name_still_reads_as_the_directory_it_is_for(self):
        name = self.container_name("run_claude", self.make_repo("repo1/master"))
        self.assertTrue(
            name.startswith("claude-master-"),
            "%s does not name the harness and the directory" % name)

    def test_a_subdirectory_of_a_repo_is_named_for_that_subdirectory(self):
        # PROJECT_DIR is what the name identifies, not the workspace root the recipe resolves.
        nested = os.path.join(self.make_repo("repo1/master"), "src")
        os.makedirs(nested)
        self.assertTrue(
            self.container_name("run_claude", nested).startswith("claude-master-src-"))

    def test_a_directory_inside_a_repository_is_named_for_that_repository_too(self):
        # A `master` in several repositories is unreadable without the repository name.
        self.make_repo("repo1")
        project = self.make_repo("repo1/master")
        self.assertTrue(
            self.container_name("run_claude", project).startswith("claude-repo1-master-"))

    def test_a_path_a_shell_would_reparse_names_its_own_directory(self):
        # make derives the name through $(shell), which must not rewrite the path first.
        for branch in ("dol$lar", "back`tick", "quo'te", "sub$(id)", "a b"):
            project = self.make_repo(os.path.join("repo1", branch))
            self.assertEqual(
                self.make_output("name_claude", project),
                "claude-%s" % names.project_token(project),
                branch)

    def test_the_same_directory_gets_the_same_name_every_run(self):
        project = self.make_repo("repo1/master")
        self.assertEqual(
            self.container_name("run_claude", project),
            self.container_name("run_claude", project))

    def test_the_name_target_prints_the_name_the_run_target_used(self):
        project = self.make_repo("repo1/master")
        started = self.container_name("run_claude", project)
        self.assertEqual(self.make_output("name_claude", project), started)

    def test_every_harness_names_its_own_container_for_the_directory(self):
        # Read off the recording, so a harness added later is held to this for free.
        harnesses = [target[len("run_"):] for target in make_argv_fixtures.RUN_ARGV]
        self.assertTrue(harnesses)
        project = self.make_repo("repo1/master")
        names = {
            harness: self.container_name("run_%s" % harness, project)
            for harness in harnesses
        }
        self.assertEqual(len(set(names.values())), len(names))
        for harness, name in names.items():
            self.assertTrue(name.startswith("%s-master-" % harness), name)


class RunArgvBaseline(MakeRecipeCase):
    """Every word of every run_* target's launcher argv, against a recording.

    The classes above pin the properties that carry a rationale; this one
    pins everything else -- flag order, env values, mount list, image name --
    so an accidental change to any recipe or shared block surfaces as a diff
    against `make_argv_fixtures.RUN_ARGV` rather than passing silently.
    """

    maxDiff = None

    def assert_argv_matches_recording(self, target):
        argv = self.launcher_argv(target, self.make_repo())
        self.assertEqual(
            make_argv_fixtures.normalize(argv, self.tmp),
            make_argv_fixtures.RUN_ARGV[target],
        )

    def test_run_opencode_argv_matches_recording(self):
        self.assert_argv_matches_recording("run_opencode")

    def test_run_claude_argv_matches_recording(self):
        self.assert_argv_matches_recording("run_claude")

    def test_run_grok_argv_matches_recording(self):
        self.assert_argv_matches_recording("run_grok")

    def test_run_codex_argv_matches_recording(self):
        self.assert_argv_matches_recording("run_codex")


if __name__ == "__main__":
    if shutil.which("make") is None or shutil.which("git") is None:
        sys.stderr.write("make and git are required for these tests\n")
        sys.exit(1)
    unittest.main(verbosity=2)
