#!/usr/bin/env python3
"""Behavior tests for swarmforge.harness.execute, the pre-exec driver.

The driver is the last thing that runs before the session's harness does, and
what it hands to execve is the whole of what that harness knows about itself:
the environment carries claude's config directory and its credential store, and
the argv carries the settings file that decides whose permissions, hooks, and
env the session runs under. An env delta lost here is a session that cannot
find its secrets or silently runs on the checkout's own settings.

The other half of the contract is that no harness inherits claude's plumbing.
Every harness but claude keeps the default hook, so its exec has to stay
byte-identical to the direct one the entrypoint used to perform -- the same
file, the same argv, and the same environment save for the home and the two
variables the driver's own launch adds. Those two are asserted absent for every
harness: they exist only because this driver is a python module, and a harness
that inherits them is one running with an import root pointing at Swarmforge's
own package.

Every guarantee here is checked by running the driver with a recording execve
and reading the call it made, so a dropped variable or a reordered argv fails a
test rather than surfacing as a session that lost its history.

Nothing here may write outside the temporary directory, and nothing may read
the live /run/swarmforge the development host has: claude's settings file, its
wrapper directory, and its pinned config destination are all replaced for the
duration of each run.

Run: python3 tests/test_harness_execute.py
"""

import contextlib
import dataclasses
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import harness
from swarmforge.harness import claude, execute, init, spec

# Every registered harness that starts exactly as it was invoked.
PLAIN = ("codex", "grok", "opencode")


def write_file(path, text, mode=None):
    """Write `text` at `path`, creating the parent directories."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


class ExecuteCase(unittest.TestCase):
    """Runs the driver with a recording execve over a staged container.

    Claude pins the settings file, the wrapper directory, and the config
    destination its hook names, so all three are replaced for the run and a
    test never reads the live /run/swarmforge the development host has. The
    execve stub returns instead of replacing the process, which is the one
    thing the real call cannot do.
    """

    ARGS = ["--model", "sonnet", "run the thing"]

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-exec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.shared = os.path.join(self.home, ".claude")
        self.dest = os.path.join(self.tmp, "dest")
        self.wrapper = os.path.join(self.tmp, "wrapper")
        self.settings = os.path.join(self.tmp, "claude-settings.json")
        self.libdir = os.path.join(self.tmp, "lib")
        self.recorded = []

    def env(self, **overrides):
        """The environment the container hands the driver's launch.

        The two PYTHON* variables are the launch's own: the entrypoint sets
        them so the module can be imported and so interpreter startup leaves
        the locale alone.
        """
        environ = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
            "TERM": "xterm-256color",
            "SWARMFORGE_CONFIG_ORG_DIR": "/run/swarmforge/config/org",
            "SWARMFORGE_TONG_MCP_FILE": "/run/swarmforge/tong-mcp.json",
            "PYTHONPATH": self.libdir,
            "PYTHONCOERCECLOCALE": "0",
        }
        environ.update(overrides)
        return {name: value for name, value in environ.items() if value is not None}

    def passed_through(self, environ):
        """The container environment as the harness must inherit it."""
        expected = dict(environ)
        for var in execute.LAUNCH_VARS:
            expected.pop(var, None)
        expected["HOME"] = self.home
        return expected

    @contextlib.contextmanager
    def redirected(self, name):
        """Every path `name` pins, replaced by one under the staging tree."""
        module = harness.get(name)
        self.assertIsNotNone(module, "no harness registered as %s" % name)
        with contextlib.ExitStack() as stack:
            # Both belong to claude's module and both are read by its hook,
            # so they move for every run.
            stack.enter_context(
                mock.patch.object(claude, "WRAPPER_DIR", self.wrapper))
            stack.enter_context(
                mock.patch.object(claude, "SETTINGS_FILE", self.settings))
            if name == "claude":
                stack.enter_context(mock.patch.object(
                    module, "SPEC",
                    dataclasses.replace(module.SPEC, config_dest=self.dest)))
            yield

    def execute(self, name, args=None, environ=None):
        """Run the driver for `name`, recording the exec it performs."""
        environ = self.env() if environ is None else environ
        with self.redirected(name):
            status = execute.run(
                name, self.home, self.ARGS if args is None else args,
                environ, execv=self.record)
        self.assertEqual(status, 0)
        self.assertEqual(len(self.recorded), 1)
        return self.recorded[-1]

    def record(self, file, argv, env):
        """Stand in for execve, which never returns to its caller."""
        self.recorded.append((file, argv, env))

    def stage_settings(self):
        return write_file(self.settings, "{}\n")

    def stage_wrapper(self, mode=0o755):
        return write_file(
            os.path.join(self.wrapper, "git"), "#!/bin/sh\nexec git \"$@\"\n",
            mode=mode)


class HarnessPassthrough(ExecuteCase):
    """The harnesses that keep the default hook start as they were invoked.

    The driver stands between the container and the binary for every harness,
    not just the one with an exec to shape, so the identity hook has to leave
    the exec indistinguishable from a direct one. Anything the driver adds here
    is claude's plumbing reaching a harness that never asked for it, and the
    environment is compared whole so a stray variable fails rather than
    surviving unnoticed.
    """

    def test_every_plain_harness_execs_its_own_binary_unchanged(self):
        for name in PLAIN:
            with self.subTest(harness=name):
                self.setUp()
                environ = self.env()
                binary = "/usr/local/bin/" + harness.get(name).SPEC.binary

                file, argv, env = self.execute(name, environ=environ)

                self.assertEqual(file, binary)
                self.assertEqual(argv, [binary] + self.ARGS)
                self.assertEqual(env, self.passed_through(environ))

    def test_no_harness_inherits_the_variables_of_the_launch(self):
        """They exist only because the driver is a python module: an import
        root pointing at Swarmforge's own package and a startup knob for a
        locale the harness never asked about."""
        for name in harness.names():
            with self.subTest(harness=name):
                self.setUp()
                _, _, env = self.execute(name)
                for var in ("PYTHONPATH", "PYTHONCOERCECLOCALE"):
                    self.assertNotIn(var, env)


class ClaudeSettingsArgv(ExecuteCase):
    """The settings file and the scopes claude reads it beside.

    Settings named on the command line outrank every file, which is how the
    org layer beats the checkout's own .claude/settings.json. The flags lead
    the session's arguments, and the sources are the three scopes claude may
    still discover for itself -- `user` among them because that scope carries
    skills, commands, and agents discovery. A run whose settings file was never
    built has to be left alone rather than pointed at a path that is not there.
    """

    def test_the_settings_flags_lead_the_session_arguments(self):
        self.stage_settings()

        _, argv, _ = self.execute("claude")

        self.assertEqual(argv, [
            "/usr/local/bin/claude",
            "--settings", self.settings,
            "--setting-sources", "user,project,local",
        ] + self.ARGS)

    def test_the_setting_sources_name_exactly_three_scopes(self):
        """One word, comma-separated: a stray space would make it two
        arguments and claude would read the rest as a prompt."""
        self.stage_settings()

        _, argv, _ = self.execute("claude")

        self.assertEqual(
            argv[argv.index("--setting-sources") + 1].split(","),
            ["user", "project", "local"])

    def test_a_run_without_a_settings_file_keeps_its_argv(self):
        _, argv, _ = self.execute("claude")

        self.assertEqual(argv, ["/usr/local/bin/claude"] + self.ARGS)


class ClaudeGitWrapperPath(ExecuteCase):
    """The wrapper directory leads PATH exactly when a wrapper stands there.

    Every git command the session runs goes through whatever stands first on
    PATH, so the directory is prepended only for the workspace whose worktree
    paths the root phase decided to rewrite. A directory placed ahead of the
    real git with nothing runnable in it is worse than useless: it is a PATH
    entry the session pays for on every lookup and a rewrite nobody asked for
    the moment something lands there.
    """

    def test_an_installed_wrapper_leads_the_path(self):
        self.stage_wrapper()

        _, _, env = self.execute("claude")

        self.assertEqual(env["PATH"], self.wrapper + ":" + self.env()["PATH"])

    def test_a_run_without_a_wrapper_keeps_its_path(self):
        _, _, env = self.execute("claude")

        self.assertEqual(env["PATH"], self.env()["PATH"])

    def test_a_wrapper_nothing_may_run_keeps_its_path(self):
        """The shell reaches a file on PATH only if it may execute it, so a
        wrapper without the bits is a rewrite that never happens."""
        self.stage_wrapper(mode=0o644)

        _, _, env = self.execute("claude")

        self.assertEqual(env["PATH"], self.env()["PATH"])


class ClaudeEnvironment(ExecuteCase):
    """The two directories claude is told about, and nothing besides.

    One names the config destination the root phases merged, the other the
    credential store. The store is named rather than linked because
    credentials are written by rename, which replaces a link with a
    container-local file, and claude's token-refresh lock sits in the same
    directory, so concurrent containers rotate the shared token one at a time.
    A session that inherits neither reads an unmerged config and logs in again.
    """

    def test_the_environment_gains_exactly_the_two_directories(self):
        environ = self.env()

        _, _, env = self.execute("claude", environ=environ)

        expected = self.passed_through(environ)
        expected["CLAUDE_CONFIG_DIR"] = self.dest
        expected["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = self.shared
        self.assertEqual(env, expected)

    def test_the_credential_store_is_where_the_state_links_point(self):
        """The links the root phase makes and the directory the exec names are
        the same shared home; a disagreement puts the session's history in one
        directory and its token in another."""
        with self.redirected("claude"):
            init.link_state("claude", self.home, {})
        _, _, env = self.execute("claude")

        parents = {
            os.path.dirname(os.readlink(os.path.join(self.dest, entry)))
            for entry in claude.STATE_DIRS + claude.STATE_FILES
        }
        self.assertEqual(parents, {env["CLAUDE_SECURESTORAGE_CONFIG_DIR"]})


class DriverArgv(ExecuteCase):
    """The entrypoint's invocation is unguarded, so its argv has to hold.

    The separator is what keeps the session's own arguments out of the
    driver's: a harness invoked with none is the ordinary interactive case, and
    a driver that mistook an argument for its own would exec the wrong binary
    or none at all.
    """

    def test_too_few_arguments_are_refused(self):
        for argv in ([], ["one"], ["one", "two"]):
            with self.subTest(argv=argv):
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured):
                    self.assertEqual(execute.main(argv), 2)
                self.assertIn(execute.USAGE, captured.getvalue())

    def test_arguments_without_the_separator_are_refused(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.assertEqual(execute.main(["grok", self.home, "-x"]), 2)
        self.assertIn(execute.USAGE, captured.getvalue())

    def test_a_separator_with_nothing_after_it_starts_a_bare_harness(self):
        seen = []
        with mock.patch.object(
                execute, "run", lambda *call: seen.append(call) or 0):
            self.assertEqual(execute.main(["grok", self.home, "--"]), 0)

        name, home, args, environ = seen[0]
        self.assertEqual((name, home, args), ("grok", self.home, []))
        self.assertIs(environ, os.environ)

        _, argv, _ = self.execute("grok", args=[])
        self.assertEqual(argv, ["/usr/local/bin/grok"])

    def test_an_unregistered_harness_is_refused(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status = execute.run(
                "nosuch", self.home, [], self.env(), execv=self.record)

        self.assertEqual(status, 2)
        self.assertIn("unknown harness: nosuch", captured.getvalue())
        self.assertEqual(self.recorded, [])

    def test_the_driver_runs_against_the_staged_package(self):
        """The image copies the package under an import root and invokes the
        driver out of it with `python3 -P -m`. This stages that shape, with the
        checkout off both sys.path and the working directory, so a module that
        only resolves its imports from the repo fails here. It is run with no
        arguments, which is refused before anything is exec'd."""
        os.makedirs(self.libdir)
        shutil.copytree(
            os.path.join(REPO_ROOT, "swarmforge"),
            os.path.join(self.libdir, "swarmforge"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        completed = subprocess.run(
            [sys.executable, "-P", "-m", "swarmforge.harness.execute"],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.libdir},
            cwd=self.tmp, capture_output=True, text=True,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn(execute.USAGE, completed.stderr)


class PreExecDescriptor(unittest.TestCase):
    """Which harnesses shape their own exec at all.

    The hook is the only place a harness can speak for the process it becomes,
    and it is claude's alone: a harness that picked one up would start with an
    environment built for somebody else's config directory.
    """

    def test_every_harness_declares_a_callable_hook(self):
        for name in harness.names():
            with self.subTest(harness=name):
                self.assertTrue(callable(harness.get(name).SPEC.pre_exec))

    def test_claude_shapes_its_own_exec(self):
        self.assertIs(harness.get("claude").SPEC.pre_exec, claude.pre_exec)

    def test_every_other_harness_starts_as_invoked(self):
        for name in PLAIN:
            with self.subTest(harness=name):
                self.assertIs(harness.get(name).SPEC.pre_exec, spec.pre_exec)


if __name__ == "__main__":
    unittest.main()
