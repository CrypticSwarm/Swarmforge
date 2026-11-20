#!/usr/bin/env python3
"""Unit tests for swarmforge.tmux. Run: python3 tests/test_tmux.py"""

import os
import re
import shutil
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the CLI's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from swarmforge import command
from swarmforge import tmux
from swarmforge import worktrees

from git_fixtures import LayoutCase, git

# Flags of the tmux commands the module runs that take a value.
VALUED_FLAGS = {"-t", "-s", "-n", "-c", "-F"}

# Flags whose value tmux expands as a format; targets are taken literally.
FORMAT_FLAGS = {"-s", "-c"}

# A `#` that is not part of a `##` escape.
_LONE_HASH = re.compile(r"(?<!#)#(?!#)")


class FakeServer:
    """A tmux server holding sessions as ordered (window, directory) lists.

    It answers only the commands `swarmforge.tmux` is meant to run, refuses a
    format value tmux would expand, and records every call.
    """

    def __init__(self):
        self.sessions = {}
        self.calls = []

    def __call__(self, *args, capture=False, quiet=False):
        self.calls.append(args)
        command_name, flags = args[0], self.flags(args[1:])
        target = flags.get("-t", "")
        if command_name == "has-session":
            # Absence is the expected answer; real tmux would say so on stderr.
            assert quiet, "has-session must be quiet"
            if target[1:] not in self.sessions:
                raise tmux.TmuxError("no such session", 1)
        elif command_name == "new-session":
            self.sessions[flags["-s"]] = [(flags["-n"], flags["-c"])]
        elif command_name == "list-windows":
            return "".join("%s\n" % name
                           for name, _ in self.sessions[target[1:]])
        elif command_name == "new-window":
            self.sessions[target[1:].rstrip(":")].append(
                (flags["-n"], flags["-c"]))
        else:
            raise AssertionError("unexpected tmux command: %r" % (args,))
        return "" if capture else None

    @staticmethod
    def flags(words):
        flags, words = {}, list(words)
        while words:
            word = words.pop(0)
            if word not in VALUED_FLAGS:
                flags[word] = True
                continue
            value = words.pop(0)
            if word in FORMAT_FLAGS:
                if _LONE_HASH.search(value):
                    raise AssertionError("%s %r would be expanded" % (word, value))
                value = value.replace("##", "#")
            flags[word] = value
        return flags

    def window_list(self, session="repo"):
        return [name for name, _ in self.sessions[session]]


class SessionCase(LayoutCase):
    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        fake = mock.patch.object(tmux, "tmux", self.server)
        fake.start()
        self.addCleanup(fake.stop)

    def layout(self, default="main", others=("feature/tmux-tui",), name="repo"):
        """A `swarmforge clone` with a worktree for each of `others`."""
        origin = self.origin(default=default, others=others)
        main = self.clone(origin, self.path(name))
        for branch in others:
            git(main, "worktree", "add", "-q", self.path(name, branch), branch)
        return self.path(name)


class OpeningALayout(SessionCase):
    def test_every_path_in_the_repository_opens_the_same_session(self):
        root = self.layout()
        expected = [("Repo", root),
                    ("Main", os.path.join(root, "main")),
                    ("Feature Tmux Tui", os.path.join(root, "feature", "tmux-tui"))]
        for path in (root, os.path.join(root, ".git"),
                     os.path.join(root, "main"),
                     os.path.join(root, "feature", "tmux-tui")):
            with self.subTest(path=path):
                self.server.sessions.clear()
                self.assertEqual(tmux.open_session(path), "repo")
                self.assertEqual(self.server.sessions, {"repo": expected})

    def test_the_default_branch_comes_before_worktrees_git_lists_first(self):
        root = self.layout(others=("alpha", "beta"))
        tmux.open_session(root)
        self.assertEqual(self.server.window_list(),
                         ["Repo", "Main", "Alpha", "Beta"])

    def test_origins_default_outranks_the_bare_head(self):
        root = self.layout(default="trunk", others=("main",))
        worktrees.set_head(os.path.join(root, ".git"), "main")
        tmux.open_session(root)
        self.assertEqual(self.server.window_list(), ["Repo", "Trunk", "Main"])

    def test_without_origin_the_bare_head_names_the_default_quietly(self):
        main = self.init(self.path("repo"), branch="trunk")
        git(main, "worktree", "add", "-q", "--orphan", "-b", "alpha",
            self.path("repo", "alpha"))
        with mock.patch.object(command, "run", wraps=command.run) as run:
            tmux.open_session(main)
        self.assertEqual(self.server.window_list(), ["Repo", "Trunk", "Alpha"])
        probes = [call for call in run.call_args_list
                  if "symbolic-ref" in call.args[0]]
        self.assertEqual(len(probes), 2)
        self.assertTrue(all(call.kwargs["quiet"] for call in probes))

    def test_windows_open_detached_in_their_directory(self):
        root = self.layout()
        tmux.open_session(root)
        self.assertIn(("new-window", "-d", "-t", "=repo:", "-n", "Main",
                       "-c", os.path.join(root, "main")), self.server.calls)

    def test_a_worktree_whose_directory_is_gone_is_skipped(self):
        root = self.layout(others=("alpha", "beta"))
        shutil.rmtree(os.path.join(root, "alpha"))
        tmux.open_session(root)
        self.assertEqual(self.server.window_list(), ["Repo", "Main", "Beta"])

    def test_worktrees_whose_names_collide_each_get_a_window(self):
        root = self.layout(others=("feature/x", "feature-x", "repo"))
        tmux.open_session(root)
        self.assertEqual(sorted(self.server.window_list()),
                         ["Feature X", "Feature X 2", "Main", "Repo", "Repo 2"])


class OpeningAgain(SessionCase):
    def test_only_windows_for_new_worktrees_are_added(self):
        root = self.layout(others=("alpha",))
        tmux.open_session(root)
        git(os.path.join(root, "main"), "worktree", "add", "-q",
            os.path.join(root, "beta"), "-b", "beta")
        tmux.open_session(root)
        self.assertEqual(self.server.window_list(),
                         ["Repo", "Main", "Alpha", "Beta"])
        self.assertEqual(
            [call for call in self.server.calls if call[0] == "new-session"],
            [("new-session", "-d", "-s", "repo", "-n", "Repo", "-c", root)])

    def test_a_session_missing_its_repo_window_gets_one(self):
        root = self.layout(others=())
        self.server.sessions["repo"] = [("Main", os.path.join(root, "main"))]
        tmux.open_session(root)
        self.assertEqual(self.server.sessions["repo"],
                         [("Main", os.path.join(root, "main")), ("Repo", root)])


class OpeningACheckout(SessionCase):
    def test_the_checkout_is_repo_and_outside_worktrees_use_basenames(self):
        checkout = self.origin(others=("topic",), name="plain")
        git(checkout, "worktree", "add", "-q",
            self.path("elsewhere", "topic-wt"), "topic")
        self.assertEqual(tmux.open_session(checkout), "plain")
        self.assertEqual(self.server.sessions["plain"],
                         [("Repo", checkout),
                          ("Topic Wt", self.path("elsewhere", "topic-wt"))])

    def test_a_fallback_branch_does_not_outrank_the_checkouts_own(self):
        """A stale `master` beside a checkout on `main` stays in git's order."""
        checkout = self.origin(others=("alpha", "master"), name="plain")
        for branch in ("alpha", "master"):
            git(checkout, "worktree", "add", "-q", self.path("wt", branch), branch)
        tmux.open_session(checkout)
        self.assertEqual(self.server.window_list("plain"),
                         ["Repo", "Alpha", "Master"])

    def test_a_detached_checkout_falls_back_to_main_then_master(self):
        checkout = self.origin(default="trunk", others=("alpha", "main", "master"),
                               name="plain")
        git(checkout, "checkout", "-q", "--detach")
        for branch in ("alpha", "main", "master"):
            git(checkout, "worktree", "add", "-q", self.path("wt", branch), branch)
        tmux.open_session(checkout)
        self.assertEqual(self.server.window_list("plain"),
                         ["Repo", "Main", "Alpha", "Master"])

    def test_a_bare_clone_is_the_root_from_any_worktree(self):
        bare = self.path("r.git")
        git(self.tmp, "clone", "-q", "--bare", self.origin(others=("topic",)), bare)
        git(bare, "worktree", "add", "-q", self.path("wt"), "topic")
        expected = {"r_git": [("Repo", bare), ("Wt", self.path("wt"))]}
        for path in (bare, self.path("wt")):
            with self.subTest(path=path):
                self.server.sessions.clear()
                with mock.patch.object(command, "run", wraps=command.run) as run:
                    tmux.open_session(path)
                self.assertEqual(self.server.sessions, expected)
                # The checkout probe fails for a bare repo; git must not say so.
                probes = [call for call in run.call_args_list
                          if "--show-toplevel" in call.args[0]]
                self.assertEqual([call.kwargs["quiet"] for call in probes], [True])

    def test_a_submodule_opens_at_its_checkout_from_any_worktree(self):
        source = self.origin(others=(), name="subsource")
        outer = self.origin(others=(), name="outer")
        git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            source, "sub")
        checkout = os.path.join(outer, "sub")
        git(checkout, "worktree", "add", "-q", "-b", "side", self.path("subwt"))
        expected = {"sub": [("Repo", checkout), ("Subwt", self.path("subwt"))]}
        for path in (checkout, self.path("subwt")):
            with self.subTest(path=path):
                self.server.sessions.clear()
                tmux.open_session(path)
                self.assertEqual(self.server.sessions, expected)

    def test_a_separate_git_dir_opens_one_session_from_any_worktree(self):
        """Its git dir names no checkout, so the git dir stays the root."""
        work = self.path("work")
        os.makedirs(self.path("store"))
        git(self.tmp, "init", "-q", "--separate-git-dir",
            self.path("store", "sep.git"), work)
        git(work, "commit", "-q", "--allow-empty", "-m", "root")
        git(work, "worktree", "add", "-q", "-b", "side", self.path("side"))
        sessions = []
        for path in (work, self.path("side")):
            self.server.sessions.clear()
            sessions.append((tmux.open_session(path), self.server.sessions))
        self.assertEqual(sessions[0], sessions[1])


class SessionNames(SessionCase):
    def test_a_given_name_is_used(self):
        root = self.layout(others=())
        self.assertEqual(tmux.open_session(root, name="work"), "work")
        self.assertIn("work", self.server.sessions)

    def test_the_characters_tmux_rewrites_are_rewritten_first(self):
        """tmux stores `a.b` as `a_b`, so a target of `=a.b` would miss it."""
        self.assertEqual(tmux.session_name("/x/my.repo"), "my_repo")
        self.assertEqual(tmux.session_name("/x/r", "a.b:c"), "a_b_c")

    def test_a_hash_in_a_name_or_path_reaches_tmux_literally(self):
        """tmux expands formats in `-s` and `-c`; `#(cmd)` would run cmd."""
        root = self.layout(others=("alpha",), name="x#(true)")
        self.assertEqual(tmux.open_session(root, name="a#b"), "a#b")
        self.assertEqual(self.server.sessions["a#b"],
                         [("Repo", root), ("Main", os.path.join(root, "main")),
                          ("Alpha", os.path.join(root, "alpha"))])


class WindowNames(unittest.TestCase):
    def test_a_path_under_the_root_is_title_cased_word_by_word(self):
        for path, name in (("/r/onboarding-tui", "Onboarding Tui"),
                           ("/r/feature/tmux_SUPPORT", "Feature Tmux Support"),
                           ("/r/release/1.0", "Release 1 0")):
            with self.subTest(path=path):
                self.assertEqual(tmux.window_name("/r", path), name)

    def test_a_path_outside_the_root_uses_its_basename(self):
        self.assertEqual(tmux.window_name("/r", "/elsewhere/side-quest"),
                         "Side Quest")

    def test_a_path_with_no_words_falls_back(self):
        self.assertEqual(tmux.window_name("/r", "/r/---"), "Worktree")


class NotARepository(SessionCase):
    def test_a_directory_outside_git_is_refused_quietly(self):
        os.makedirs(self.path("plain"))
        # Stops discovery at the scratch dir, which may sit inside a checkout.
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp}), \
                mock.patch.object(command, "run", wraps=command.run) as run:
            with self.assertRaisesRegex(worktrees.GitError,
                                        "not inside a git repository"):
                tmux.open_session(self.path("plain"))
        self.assertTrue(run.call_args.kwargs["quiet"])
        self.assertEqual(self.server.calls, [])

    def test_git_that_cannot_run_is_not_reported_as_a_missing_repository(self):
        broken = worktrees.GitError("could not run git: No such file")
        with mock.patch.object(command, "run", side_effect=broken):
            with self.assertRaisesRegex(worktrees.GitError, "could not run git"):
                tmux.open_session(self.tmp)

    def test_a_missing_directory_is_refused(self):
        with self.assertRaisesRegex(worktrees.LayoutError, "not a directory"):
            tmux.open_session(self.path("missing"))


class RunningTmux(unittest.TestCase):
    def test_tmux_runs_through_the_shared_runner_as_a_tmux_error(self):
        with mock.patch.object(command, "run", return_value="out") as run:
            self.assertEqual(tmux.tmux("list-windows", capture=True), "out")
        run.assert_called_once_with(["tmux", "list-windows"], tmux.TmuxError,
                                    capture=True, quiet=False)


class Attaching(unittest.TestCase):
    def test_inside_tmux_the_client_switches(self):
        with mock.patch.object(tmux, "tmux") as run, \
                mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1/default,1,0"}):
            tmux.attach("repo")
        run.assert_called_once_with("switch-client", "-t", "=repo")

    def test_outside_tmux_this_process_becomes_tmux_attach(self):
        environment = {key: value for key, value in os.environ.items()
                       if key != "TMUX"}
        with mock.patch.object(os, "execvp") as execvp, \
                mock.patch.dict(os.environ, environment, clear=True):
            tmux.attach("repo")
        execvp.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "=repo"])

    def test_a_missing_tmux_is_an_error_not_a_traceback(self):
        with mock.patch.object(os, "execvp",
                               side_effect=FileNotFoundError(2, "No such file")), \
                mock.patch.object(tmux, "tmux") as run, \
                mock.patch.dict(os.environ, {"TMUX": ""}):
            with self.assertRaisesRegex(tmux.TmuxError, "could not run tmux"):
                tmux.attach("repo")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
