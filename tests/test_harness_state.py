#!/usr/bin/env python3
"""Behavior tests for the state the container links back into a config dir.

A harness whose config destination is rebuilt for every run keeps what has to
outlive the container in the persistent home, and the driver links those
entries back into the destination once the merge has finished with it. Claude
is the one harness that works this way, and the guarantee is narrow on purpose:
only the allowlisted entries survive, so a path claude learns to load in a
later release stays inert until somebody lists it, and everything a run writes
outside the allowlist dies with the container it was written in.

Every guarantee here is checked by running the driver's link phase over a
staged home and reading what the destination holds afterwards, so an entry that
stopped being linked, a link pointed at the wrong side, or a stale destination
entry left standing fails a test rather than surfacing as a session that lost
its history.

The root phase that follows the links is checked the same way. Claude installs
a git wrapper there when the workspace is a linked worktree recording a path
that does not exist in the container, so its /resume finds the session
directories belonging to the checkout it is actually running in; every other
shape of workspace has to leave the wrapper directory alone, or a session runs
its git through a rewrite nothing asked for.

Nothing here may write outside the temporary directory: claude pins a config
destination under /run/swarmforge and a wrapper directory under
/usr/local/libexec, and both are replaced for the duration of each run.

Run: python3 tests/test_harness_state.py
"""

import contextlib
import dataclasses
import os
import shutil
import sys
import tempfile
import types
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
from swarmforge.harness import claude, init, spec
from swarmforge.harness.spec import HarnessSpec, Waiver

# Every harness that keeps nothing across runs of its own.
UNLINKED = ("codex", "grok", "opencode")


def write_file(path, text):
    """Write `text` at `path`, creating the parent directories."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tree(root):
    """Every path under `root`, relative, mapped to what stands there.

    A file maps to its text, a symlink to `("link", target)` with the target
    string it was created with, and a directory to None. A missing root is an
    empty mapping, so a destination that was never created and one that was
    created empty read differently.
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            key = os.path.relpath(path, root)
            if os.path.islink(path):
                found[key] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                found[key] = None
            else:
                found[key] = read_file(path)
    return found


def fake_spec(**overrides):
    """A registrable spec that declares nothing but the hooks under test."""
    fields = dict(
        name="fake",
        binary="fake",
        config_dest=Waiver("the run's SWARMFORGE_CONFIG_DEST names the destination"),
        config_reset=False,
        layer_excludes=(),
        keyed_files=(),
        skills_dest=Waiver("no portable skills destination is declared"),
        commands_dest=Waiver("no portable commands destination is declared"),
        agents_dest=Waiver("unified agent definitions are not delivered"),
        mcp_fragment=lambda servers: {},
        mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
        mcp_merge=Waiver("nothing merges the fragment into a config file"),
        agent_emitter=Waiver("no emitter is defined"),
        extra_chown_paths=(),
    )
    fields.update(overrides)
    return HarnessSpec(**fields)


class StateCase(unittest.TestCase):
    """Runs the link phase over a staged home inside a temporary directory.

    Claude pins the config destination its state is linked into, so that field
    is replaced for the run and a test never reaches the live /run/swarmforge
    the development host has.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.shared = os.path.join(self.home, ".claude")
        self.dest = os.path.join(self.tmp, "dest")
        self.wrapper = os.path.join(self.tmp, "wrapper")

    def env(self, **overrides):
        """The environment the entrypoint hands the driver."""
        environ = {"SWARMFORGE_CONFIG_DEST": self.dest}
        environ.update(overrides)
        return {name: value for name, value in environ.items() if value is not None}

    @contextlib.contextmanager
    def redirected(self, name):
        """Every path `name` pins, replaced by one under the staging tree."""
        module = harness.get(name)
        self.assertIsNotNone(module, "no harness registered as %s" % name)
        with contextlib.ExitStack() as stack:
            # The wrapper directory belongs to claude's module and is where any
            # root phase that writes one puts it, so it moves for every run.
            stack.enter_context(
                mock.patch.object(claude, "WRAPPER_DIR", self.wrapper))
            if name == "claude":
                stack.enter_context(mock.patch.object(
                    module, "SPEC",
                    dataclasses.replace(module.SPEC, config_dest=self.dest)))
            yield

    def link(self, name):
        """Run the driver's link phase for `name` with every pinned path moved."""
        with self.redirected(name):
            return init.link_state(name, self.home, self.env())

    def prepare(self, name, cwd):
        """Run the driver's root phase for `name` over the workspace `cwd`.

        The working directory is always named: this checkout is itself a
        linked worktree, so a phase falling back to the test process's own
        directory would read real worktree metadata and write a real wrapper.
        """
        with self.redirected(name):
            return init.root_setup(name, self.home, self.env(), cwd=cwd)

    def snapshot(self):
        """Every path under the staging tree, so any write at all shows."""
        found = set()
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                found.add(os.path.join(dirpath, name))
        return found

    def expected_links(self):
        """The whole destination tree the link phase produces."""
        links = {".claude.json": ("link", os.path.join(self.home, ".claude.json"))}
        for entry in claude.STATE_DIRS + claude.STATE_FILES:
            links[entry] = ("link", os.path.join(self.shared, entry))
        return links


class StateAllowlist(unittest.TestCase):
    """What the allowlist may name, and which harnesses have one at all.

    The list is the whole contract: an entry on it outlives the run and
    everything else dies with the container, so a mistake in either direction
    is a session reading last week's configuration or losing today's history.
    """

    # Paths claude reads as configuration or code, which the config merge and
    # the asset install rebuild for every run.
    LOADED = ("settings.json", "CLAUDE.md", "rules", "workflows",
              "output-styles", "routines", "skills", "commands", "agents")

    def test_the_allowlist_carries_nothing_claude_loads(self):
        """The list fails safe: a path claude learns to load in a later
        release stays inert until somebody lists it."""
        listed = claude.STATE_DIRS + claude.STATE_FILES
        for name in self.LOADED:
            self.assertNotIn(name, listed)

    def test_the_credential_store_is_not_on_the_allowlist(self):
        """Credentials are written by rename, which replaces a link with a
        container-local file, so the store cannot be a link."""
        self.assertNotIn(
            ".credentials.json", claude.STATE_DIRS + claude.STATE_FILES)

    def test_claude_links_its_own_state(self):
        self.assertIs(harness.get("claude").SPEC.link_state, claude.link_state)

    def test_every_other_harness_links_nothing(self):
        """Their config already lives in the persistent home, so there is
        nothing to carry back into it."""
        for name in UNLINKED:
            with self.subTest(harness=name):
                self.assertIs(harness.get(name).SPEC.link_state, spec.link_state)


class FreshHome(StateCase):
    """A home that has never run a session gets the whole set of links.

    The destination is built from nothing on every run, so the links are what
    a session finds there -- one per allowlisted entry, and nothing else.
    """

    def test_the_destination_holds_exactly_the_allowlisted_links(self):
        self.assertEqual(self.link("claude"), 0)
        self.assertEqual(tree(self.dest), self.expected_links())
        self.assertEqual(len(self.expected_links()), 18)

    def test_the_shared_home_gains_the_state_directories_and_nothing_else(self):
        """A directory has to exist before it is linked, or claude's own
        mkdir fails on the link; a file has no such problem, and its link
        dangles until claude writes it."""
        self.assertEqual(self.link("claude"), 0)
        expected = {".claude": None}
        for entry in claude.STATE_DIRS:
            expected[os.path.join(".claude", entry)] = None
        self.assertEqual(tree(self.home), expected)


class StateFromEarlierRuns(StateCase):
    """State an earlier run wrote is served through the links.

    Reading it back through the destination is the whole point of the phase:
    the session finds its history where claude looks for it, while the file
    itself stays in the home that outlives the container.
    """

    def test_staged_state_reads_back_through_the_destination(self):
        write_file(
            os.path.join(self.shared, "projects", "p1", "sess.jsonl"), "s\n")
        write_file(os.path.join(self.shared, "history.jsonl"), "h\n")
        write_file(os.path.join(self.home, ".claude.json"), "{}\n")

        self.assertEqual(self.link("claude"), 0)
        self.assertEqual(
            read_file(os.path.join(self.dest, "projects", "p1", "sess.jsonl")),
            "s\n")
        self.assertEqual(
            read_file(os.path.join(self.dest, "history.jsonl")), "h\n")
        self.assertEqual(read_file(os.path.join(self.dest, ".claude.json")), "{}\n")


class StaleDestinationEntries(StateCase):
    """Whatever stands at a linked name is replaced by the link.

    A config layer can ship a directory under a state name and an earlier run
    can leave a link behind, so the phase has to clear the name rather than
    write beside it -- and it clears the name only, never the tree a link
    points at.
    """

    def test_every_kind_of_stale_entry_gives_way_to_the_link(self):
        outside = os.path.join(self.tmp, "outside")
        write_file(os.path.join(outside, "keep.md"), "kept\n")
        write_file(os.path.join(self.dest, "projects", "p1", "old.jsonl"), "old\n")
        write_file(os.path.join(self.dest, "history.jsonl"), "stale\n")
        os.symlink(os.path.join(self.tmp, "gone"), os.path.join(self.dest, "todos"))
        os.symlink(outside, os.path.join(self.dest, "cache"))

        self.assertEqual(self.link("claude"), 0)
        self.assertEqual(tree(self.dest), self.expected_links())
        self.assertEqual(read_file(os.path.join(outside, "keep.md")), "kept\n")


class SharedEntryInTheWay(StateCase):
    """A file standing where a state directory belongs is still linked.

    Creating the directory is a convenience for claude's own mkdir; the link
    is the guarantee, and it is made whatever the shared home holds.
    """

    def test_a_file_under_a_state_directory_name_is_linked_as_it_stands(self):
        write_file(os.path.join(self.shared, "projects"), "not a dir\n")

        self.assertEqual(self.link("claude"), 0)
        self.assertEqual(tree(self.dest), self.expected_links())
        self.assertEqual(
            read_file(os.path.join(self.dest, "projects")), "not a dir\n")


class HarnessesWithoutState(StateCase):
    """The default hook writes nothing at all.

    Their config lives in the persistent home already, so the phase has to
    leave every staged path exactly as it found it rather than creating a
    destination nothing reads.
    """

    def test_nothing_is_linked_for_the_harnesses_that_declare_no_hook(self):
        for name in UNLINKED:
            with self.subTest(harness=name):
                self.setUp()
                write_file(os.path.join(self.shared, "history.jsonl"), "h\n")
                write_file(os.path.join(self.dest, "config.toml"), "\n")

                before = self.snapshot()
                self.assertEqual(self.link(name), 0)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(tree(self.dest), {"config.toml": "\n"})


class WorktreeCase(StateCase):
    """Stages a workspace the root phase runs over.

    The workspace is a directory of its own under the staging tree rather than
    the directory the tests run in: this checkout is a linked worktree itself,
    so a phase reading the process's own working directory would find real
    metadata to rewrite.
    """

    # The path the host has the checkout at, which nothing in the container
    # resolves.
    HOST_WORKTREE = "/host/repos/proj/wt"

    def setUp(self):
        super().setUp()
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.admin = os.path.join(self.tmp, "bare", "worktrees", "wt")

    def stage_linked_worktree(self, host_dotgit):
        """A `.git` file pointing at an administrative dir that points back."""
        write_file(os.path.join(self.ws, ".git"), "gitdir: %s\n" % self.admin)
        write_file(os.path.join(self.admin, "gitdir"), host_dotgit + "\n")


class WorktreeWrapperGuards(WorktreeCase):
    """No wrapper for a workspace whose worktree paths already resolve.

    Every git command the session runs goes through whatever stands first on
    PATH, so a wrapper written for a workspace that does not need one puts a
    rewrite in front of the real git for the whole run. Each shape below
    reaches a different point of the walk from the workspace to the recorded
    host path, and all of them have to leave the wrapper directory as they
    found it.
    """

    def assert_nothing_installed(self):
        self.assertEqual(self.prepare("claude", self.ws), 0)
        self.assertEqual(tree(self.wrapper), {})

    def test_a_workspace_without_a_git_entry_gets_no_wrapper(self):
        self.assert_nothing_installed()

    def test_a_regular_checkout_gets_no_wrapper(self):
        """Its `.git` is a directory, which records no path to rewrite."""
        os.makedirs(os.path.join(self.ws, ".git"))
        self.assert_nothing_installed()

    def test_a_git_file_naming_no_gitdir_gets_no_wrapper(self):
        write_file(os.path.join(self.ws, ".git"), "ref: refs/heads/main\n")
        self.assert_nothing_installed()

    def test_an_administrative_dir_without_a_reverse_pointer_gets_no_wrapper(self):
        """Nothing records the host path, so there is none to rewrite."""
        write_file(os.path.join(self.ws, ".git"), "gitdir: %s\n" % self.admin)
        os.makedirs(self.admin)
        self.assert_nothing_installed()

    def test_a_worktree_recorded_at_the_container_path_gets_no_wrapper(self):
        """The recorded path resolves inside the container as it stands, so
        claude's own CWD-match already finds the session directories."""
        self.stage_linked_worktree(os.path.join(self.ws, ".git"))
        self.assert_nothing_installed()


class WorktreeWrapperInstall(WorktreeCase):
    """The wrapper a linked worktree from a bare repo gets, byte for byte.

    The recorded path is the host's and does not exist in the container, so
    claude's /resume matches nothing until the porcelain output names the
    directory the checkout is actually mounted at. The whole file is asserted:
    it is shell that runs for every git command of the session, and a drifted
    case pattern or an unquoted path is a broken git rather than a failed
    rewrite.
    """

    def test_the_wrapper_rewrites_the_recorded_host_path(self):
        self.stage_linked_worktree(self.HOST_WORKTREE + "/.git")
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)

        self.assertEqual(self.prepare("claude", self.ws), 0)

        expected = (
            '#!/bin/sh\n'
            '# Swarmforge git wrapper: rewrite worktree paths for container'
            ' compatibility.\n'
            'case "$*" in\n'
            '  *worktree*list*--porcelain*)\n'
            '    "%(git)s" "$@" | sed "s|^worktree %(host)s$|worktree'
            ' %(workspace)s|"\n'
            '    ;;\n'
            '  *)\n'
            '    exec "%(git)s" "$@"\n'
            '    ;;\n'
            'esac\n'
        ) % {
            "git": shutil.which("git"),
            "host": self.HOST_WORKTREE,
            "workspace": self.ws,
        }
        self.assertEqual(tree(self.wrapper), {"git": expected})

    def test_the_wrapper_is_executable(self):
        """PATH only reaches a file the shell may run."""
        self.stage_linked_worktree(self.HOST_WORKTREE + "/.git")
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)

        self.assertEqual(self.prepare("claude", self.ws), 0)
        mode = os.stat(os.path.join(self.wrapper, "git")).st_mode
        self.assertEqual(mode & 0o777, 0o755)


class RootSetupContext(StateCase):
    """The hook is handed the directory the harness process starts in.

    That directory is the run's own workdir, not a fixed mount, and it is what
    the rewrite has to name -- so the phase carries it into the context rather
    than leaving a hook to read the driver's own.
    """

    def test_the_working_directory_reaches_the_hook(self):
        seen = []
        module = types.SimpleNamespace(SPEC=fake_spec(root_setup=seen.append))
        workdir = os.path.join(self.tmp, "repos", "proj")

        with mock.patch.dict(harness._REGISTRY, {"fake": module}):
            status = init.root_setup(
                "fake", self.home, self.env(), cwd=workdir)

        self.assertEqual(status, 0)
        self.assertEqual([ctx.cwd for ctx in seen], [workdir])
        self.assertEqual([ctx.home for ctx in seen], [self.home])


class HarnessesWithoutRootSetup(WorktreeCase):
    """The default hook writes nothing at all.

    The worktree rewrite is claude's alone, so the same workspace that earns a
    wrapper there has to leave every staged path untouched for the rest.
    """

    def test_nothing_is_prepared_for_the_harnesses_that_declare_no_hook(self):
        for name in UNLINKED:
            with self.subTest(harness=name):
                self.setUp()
                self.stage_linked_worktree(self.HOST_WORKTREE + "/.git")

                before = self.snapshot()
                self.assertEqual(self.prepare(name, self.ws), 0)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(tree(self.wrapper), {})


class RootSetupDescriptor(unittest.TestCase):
    """Which harnesses prepare anything as root at all.

    The hook runs after the privilege check the container cannot repeat, so a
    harness that silently loses it loses the preparation with no other way to
    get it back.
    """

    def test_claude_installs_its_own_git_wrapper(self):
        self.assertIs(harness.get("claude").SPEC.root_setup, claude.root_setup)

    def test_every_other_harness_prepares_nothing(self):
        for name in UNLINKED:
            with self.subTest(harness=name):
                self.assertIs(harness.get(name).SPEC.root_setup, spec.root_setup)


if __name__ == "__main__":
    unittest.main()
