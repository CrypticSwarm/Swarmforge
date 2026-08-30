#!/usr/bin/env python3
"""Behavior tests for swarmforge.harness.init, the container's config driver.

The driver decides whose permissions, hooks, env, and MCP servers a session
runs under: it stacks the three config layers in order of trust, merges the
files each harness wants merged by key, and runs that harness's config hooks.
Every guarantee here is checked by running the driver over staged layer trees
in a temporary directory and reading what landed, so a reordered merge or a
dropped hook fails a test rather than surviving as a silent precedence change.

Nothing here may write outside the temporary directory: the harnesses that pin
a destination under /run/swarmforge have that destination, and Claude's two
settings paths, redirected for the duration of each run.

Run: python3 tests/test_harness_init.py
"""

import contextlib
import dataclasses
import io
import json
import os
import re
import shutil
import subprocess
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
from swarmforge.config import merge_json, merge_toml, merge_toml_mcp
from swarmforge.harness import claude, init
from swarmforge.harness.spec import Context, HarnessSpec, Waiver, provided

ENTRYPOINT = os.path.join(REPO_ROOT, "anvil", "entrypoint.sh")

HARNESSES = ("claude", "codex", "grok", "opencode")


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


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class DriverCase(unittest.TestCase):
    """Runs the driver over staged layer trees inside a temporary directory.

    Every path the driver can write to is rooted here. The harnesses that pin
    a config destination get that field replaced for the run, so a test never
    reaches the live /run/swarmforge the development host has.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-init-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = os.path.join(self.tmp, "dest")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.settings_file = os.path.join(self.tmp, "claude-settings.json")
        self.image_defaults = os.path.join(self.tmp, "image-defaults.json")

    def layer(self, name):
        """The directory for the named config layer, created on first use."""
        path = os.path.join(self.tmp, "layer-" + name)
        os.makedirs(path, exist_ok=True)
        return path

    def write_layer(self, name, relative, text):
        return write_file(os.path.join(self.layer(name), relative), text)

    def write_layer_json(self, name, relative, value):
        return self.write_layer(name, relative, json.dumps(value))

    def fragment(self, value):
        """A tong MCP fragment file holding `value`."""
        return write_file(
            os.path.join(self.tmp, "tong-mcp.json"), json.dumps(value))

    def env(self, **overrides):
        """The environment the entrypoint hands the driver.

        Every layer variable names a directory whether or not it was staged,
        which is how the container invokes it: an unmounted layer is a path
        that is simply not there.
        """
        environ = {
            "SWARMFORGE_CONFIG_REPO_DIR": os.path.join(self.tmp, "layer-repo"),
            "SWARMFORGE_CONFIG_USER_DIR": os.path.join(self.tmp, "layer-user"),
            "SWARMFORGE_CONFIG_ORG_DIR": os.path.join(self.tmp, "layer-org"),
            "SWARMFORGE_CONFIG_DEST": self.dest,
            "SWARMFORGE_CONFIG_RESET": "0",
        }
        environ.update(overrides)
        return {name: value for name, value in environ.items() if value is not None}

    def run_driver(self, name, environ=None):
        """Run the config phase for `name` with every pinned path redirected."""
        environ = self.env() if environ is None else environ
        module = harness.get(name)
        self.assertIsNotNone(module, "no harness registered as %s" % name)

        with contextlib.ExitStack() as stack:
            if provided(module.SPEC.config_dest):
                stack.enter_context(mock.patch.object(
                    module, "SPEC",
                    dataclasses.replace(module.SPEC, config_dest=self.dest)))
            if name == "claude":
                stack.enter_context(
                    mock.patch.object(claude, "SETTINGS_FILE", self.settings_file))
                stack.enter_context(mock.patch.object(
                    claude, "IMAGE_DEFAULT_SETTINGS", self.image_defaults))
            if name == "codex":
                # Codex publishes into a directory its image already ships.
                os.makedirs(os.path.join(self.home, ".codex"), exist_ok=True)
            return init.initialize(name, self.home, environ)


class LayerPrecedence(DriverCase):
    """The three config layers stack repo, then user, then org.

    The order is trust, not specificity: these files carry permissions, hooks,
    and env, a checkout is whatever repository was cloned, and the org layer is
    installed deliberately. A reordered stack hands a session the wrong one of
    those, and nothing about the result looks broken.
    """

    def test_the_org_layer_wins_and_lower_layers_keep_their_own_files(self):
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                for layer in ("repo", "user", "org"):
                    self.write_layer(layer, "shared.md", layer)
                self.write_layer("repo", "repo-only.md", "repo")

                self.assertEqual(self.run_driver(name), 0)
                self.assertEqual(read_file(os.path.join(self.dest, "shared.md")), "org")
                self.assertEqual(
                    read_file(os.path.join(self.dest, "repo-only.md")), "repo")

    def test_keyed_files_merge_by_key_rather_than_being_overlaid_whole(self):
        """opencode.json travels outside the tar overlay for every harness.

        Overlaid whole, the org layer's copy would drop every key the repo and
        user layers set; merged by key, only the keys the org layer names are
        replaced.
        """
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                self.write_layer_json(
                    "repo", "opencode.json", {"model": "repo/m", "repo": True})
                self.write_layer_json(
                    "user", "opencode.json", {"model": "user/m", "user": 1})
                self.write_layer_json("org", "opencode.json", {"model": "org/m"})

                self.assertEqual(self.run_driver(name), 0)
                self.assertEqual(
                    read_json(os.path.join(self.dest, "opencode.json")),
                    {"model": "org/m", "repo": True, "user": 1},
                )


class LayerExcludes(DriverCase):
    """Entries a harness keeps out of the overlay never reach the destination.

    Skills, commands, and agents have their own asset pipeline; session state
    and the files rebuilt from the layers have their own merges. An entry that
    slips through the overlay either resurrects state a reset was meant to
    clear or shadows the rebuild with a lower layer's copy.
    """

    def stage_excluded_entries(self, layer, spec):
        """Stage one file or directory in `layer` per entry the overlay skips.

        Every keyed file is staged too: those merge key-by-key, so the overlay
        carrying one whole would silently outrank the merge.
        """
        for entry in tuple(spec.keyed_files) + (".swarmforge",) + spec.layer_excludes:
            name = entry.removeprefix("./")
            path = os.path.join(self.layer(layer), name)
            if "." in name[1:]:
                write_file(path, "excluded")
            else:
                write_file(os.path.join(path, "pkg", "SKILL.md"), "excluded")

    def test_the_overlay_carries_nothing_a_harness_excludes(self):
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                spec = harness.get(name).SPEC
                self.stage_excluded_entries("repo", spec)
                self.write_layer("repo", "control.md", "carried")
                os.makedirs(self.dest)

                init.merge_config_layer(
                    self.layer("repo"), self.dest, init.layer_exclude_args(spec))
                self.assertEqual(os.listdir(self.dest), ["control.md"])

    def test_a_full_run_leaves_the_asset_trees_in_their_own_pipelines(self):
        """skills/ and .swarmforge/ reach the container through their own
        mounts and asset copies, so an overlay carrying them either litters a
        run's destination or accumulates junk in a persistent home."""
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                write_file(
                    os.path.join(self.layer("repo"), "skills", "pkg", "SKILL.md"),
                    "excluded")
                write_file(
                    os.path.join(self.layer("repo"), ".swarmforge", "junk"), "excluded")
                self.write_layer("repo", "control.md", "carried")

                self.assertEqual(self.run_driver(name), 0)
                landed = set(os.listdir(self.dest))
                self.assertEqual(landed & {"skills", ".swarmforge"}, set())
                self.assertIn("control.md", landed)

    def test_claude_exclude_arguments_match_the_recorded_flags(self):
        self.assertEqual(
            init.layer_exclude_args(harness.get("claude").SPEC),
            [
                "--exclude=./opencode.json",
                "--exclude=./.swarmforge",
                "--exclude=./skills",
                "--exclude=./commands",
                "--exclude=./agents",
                "--exclude=./settings.json",
                "--exclude=./.credentials.json",
            ],
        )

    def test_codex_exclude_arguments_match_the_recorded_flags(self):
        self.assertEqual(
            init.layer_exclude_args(harness.get("codex").SPEC),
            [
                "--exclude=./opencode.json",
                "--exclude=./.swarmforge",
                "--exclude=./skills",
                "--exclude=./packages",
                "--exclude=./sessions",
                "--exclude=./history.jsonl",
                "--exclude=./log",
                "--exclude=./config.toml",
            ],
        )

    def test_grok_exclude_arguments_match_the_recorded_flags(self):
        self.assertEqual(
            init.layer_exclude_args(harness.get("grok").SPEC),
            [
                "--exclude=./opencode.json",
                "--exclude=./.swarmforge",
                "--exclude=./skills",
                "--exclude=./commands",
                "--exclude=./bin",
                "--exclude=./downloads",
                "--exclude=./completions",
            ],
        )

    def test_opencode_exclude_arguments_match_the_recorded_flags(self):
        self.assertEqual(
            init.layer_exclude_args(harness.get("opencode").SPEC),
            [
                "--exclude=./opencode.json",
                "--exclude=./.swarmforge",
                "--exclude=./skills",
                "--exclude=./command",
            ],
        )


class SameDirectoryLayers(DriverCase):
    """A layer that resolves to the destination is skipped, not extracted.

    A home-dir layer can make the source and the destination two mounts of one
    host directory. A tar of a directory extracting over itself is at the
    mercy of how the two mounts alias each other, so it must never be spawned
    -- while the key-wise merge still has to run, since it is what the other
    layers reach that file through.
    """

    def seed_destination(self):
        os.makedirs(self.dest, exist_ok=True)
        write_file(
            os.path.join(self.dest, "opencode.json"),
            json.dumps({"model": "dest/m"}),
        )
        write_file(os.path.join(self.dest, "kept.md"), "kept")

    def assert_merged_over_the_seed(self):
        self.assertEqual(
            read_file(os.path.join(self.dest, "opencode.json")),
            json.dumps({"model": "org/m", "repo": True}, indent=2) + "\n",
        )
        self.assertEqual(read_file(os.path.join(self.dest, "kept.md")), "kept")

    def test_no_tar_is_spawned_for_an_aliased_layer(self):
        """The skip is the whole guarantee: the overlay of an aliased layer
        must never reach tar, whose self-extraction is at the mercy of how
        the two mounts alias each other."""
        self.seed_destination()
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.dest, alias)
        with mock.patch.object(
                init.subprocess, "Popen",
                side_effect=AssertionError("tar must not run")):
            init.merge_config_layer(self.dest, self.dest, [])
            init.merge_config_layer(alias, self.dest, [])
        self.assertEqual(read_file(os.path.join(self.dest, "kept.md")), "kept")

    def test_a_failed_extractor_spawn_reaps_the_creator(self):
        """The creator is on the wrong side of the pipe to notice its reader
        never arrived; the driver has to shut it down, or a stray tar outlives
        the failure that should have stopped the container."""
        self.seed_destination()
        src = self.layer("repo")
        write_file(os.path.join(src, "a.md"), "a")
        real_popen = subprocess.Popen
        creators = []

        def fake(argv, **kwargs):
            if "-cf" in argv:
                proc = real_popen(argv, **kwargs)
                creators.append(proc)
                return proc
            raise OSError("spawn failed")

        with mock.patch.object(init.subprocess, "Popen", side_effect=fake):
            with self.assertRaises(OSError):
                init.merge_config_layer(src, self.dest, [])
        self.assertEqual(len(creators), 1)
        self.assertIsNotNone(creators[0].returncode)

    def test_a_layer_that_is_the_destination_is_skipped(self):
        self.seed_destination()
        self.write_layer_json("repo", "opencode.json", {"repo": True})
        self.write_layer_json("org", "opencode.json", {"model": "org/m"})

        environ = self.env(SWARMFORGE_CONFIG_USER_DIR=self.dest)
        self.assertEqual(self.run_driver("opencode", environ), 0)
        self.assert_merged_over_the_seed()

    def test_a_symlink_to_the_destination_is_skipped(self):
        """The check is by device and inode, so an alias resolves to the same
        directory rather than reading as a separate layer."""
        self.seed_destination()
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.dest, alias)
        self.write_layer_json("repo", "opencode.json", {"repo": True})
        self.write_layer_json("org", "opencode.json", {"model": "org/m"})

        environ = self.env(SWARMFORGE_CONFIG_USER_DIR=alias)
        self.assertEqual(self.run_driver("opencode", environ), 0)
        self.assert_merged_over_the_seed()


class ResetSemantics(DriverCase):
    """Whether the destination is rebuilt from scratch is per harness, per run.

    A destination that should have been wiped carries a previous session's
    config forward; one wiped when it should not have been discards config the
    run still needs.
    """

    def seed_stale(self):
        os.makedirs(self.dest, exist_ok=True)
        write_file(os.path.join(self.dest, "stale.md"), "stale")

    def stale_survived(self):
        return os.path.exists(os.path.join(self.dest, "stale.md"))

    def test_the_run_can_ask_for_a_fresh_destination(self):
        for name in ("grok", "opencode"):
            with self.subTest(harness=name):
                self.setUp()
                self.seed_stale()
                self.assertEqual(
                    self.run_driver(name, self.env(SWARMFORGE_CONFIG_RESET="1")), 0)
                self.assertFalse(self.stale_survived())

    def test_the_destination_is_kept_when_the_run_does_not_ask(self):
        for name in ("grok", "opencode"):
            with self.subTest(harness=name):
                self.setUp()
                self.seed_stale()
                self.assertEqual(
                    self.run_driver(name, self.env(SWARMFORGE_CONFIG_RESET="0")), 0)
                self.assertTrue(self.stale_survived())

    def test_an_absent_reset_variable_keeps_the_destination(self):
        self.seed_stale()
        environ = self.env(SWARMFORGE_CONFIG_RESET=None)
        self.assertNotIn("SWARMFORGE_CONFIG_RESET", environ)
        self.assertEqual(self.run_driver("opencode", environ), 0)
        self.assertTrue(self.stale_survived())

    def test_claude_honors_the_reset_the_run_asks_for(self):
        self.seed_stale()
        self.assertEqual(
            self.run_driver("claude", self.env(SWARMFORGE_CONFIG_RESET="1")), 0)
        self.assertFalse(self.stale_survived())

    def test_codex_rebuilds_its_destination_whatever_the_run_asks(self):
        """Codex's destination holds session state the rebuild must not
        resurrect, so the harness forces the wipe rather than leaving it to
        the run."""
        self.seed_stale()
        self.assertEqual(
            self.run_driver("codex", self.env(SWARMFORGE_CONFIG_RESET="0")), 0)
        self.assertFalse(self.stale_survived())


class EmptyDestination(DriverCase):
    """With no destination, the config phase does nothing at all.

    A harness whose destination comes from the run has no fallback: writing to
    a guessed path would put a merged org layer somewhere nobody cleans up.
    """

    def snapshot(self):
        """Every path under the staging tree, so any write at all shows."""
        found = set()
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                found.add(os.path.join(dirpath, name))
        return found

    def test_an_empty_destination_variable_skips_the_phase(self):
        for name in ("grok", "opencode"):
            with self.subTest(harness=name):
                self.setUp()
                self.write_layer("repo", "shared.md", "repo")
                environ = self.env(
                    SWARMFORGE_CONFIG_DEST="",
                    SWARMFORGE_TONG_MCP_FILE=self.fragment(
                        {"mcp_servers": {"gh": {"url": "http://gh"}}}),
                )
                before = self.snapshot()
                self.assertEqual(self.run_driver(name, environ), 0)
                self.assertEqual(self.snapshot(), before)

    def test_an_absent_destination_variable_skips_the_phase(self):
        for name in ("grok", "opencode"):
            with self.subTest(harness=name):
                self.setUp()
                self.write_layer("repo", "shared.md", "repo")
                environ = self.env(
                    SWARMFORGE_CONFIG_DEST=None,
                    SWARMFORGE_TONG_MCP_FILE=self.fragment(
                        {"mcp_servers": {"gh": {"url": "http://gh"}}}),
                )
                self.assertNotIn("SWARMFORGE_CONFIG_DEST", environ)
                before = self.snapshot()
                self.assertEqual(self.run_driver(name, environ), 0)
                self.assertEqual(self.snapshot(), before)


class EmptyLayerVariables(DriverCase):
    """A layer variable that is empty names nothing anywhere.

    The driver builds layer paths by concatenation, so an empty variable
    yields an absolute path at the filesystem root -- which exists nowhere --
    rather than a path relative to the workspace the driver runs in, where a
    checkout could plant the file.
    """

    def test_the_remaining_layers_still_merge(self):
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                self.write_layer("user", "shared.md", "user")
                self.write_layer("org", "shared.md", "org")
                environ = self.env(SWARMFORGE_CONFIG_REPO_DIR="")

                self.assertEqual(self.run_driver(name, environ), 0)
                self.assertEqual(
                    read_file(os.path.join(self.dest, "shared.md")), "org")

    def test_an_empty_layer_contributes_no_codex_config_source(self):
        """An unset layer must not become a config.toml resolving from the
        root, and never one resolving from the working directory."""
        self.write_layer("user", "config.toml", 'model = "user/m"\n')
        self.write_layer("org", "config.toml", 'model = "org/m"\n')
        environ = self.env(SWARMFORGE_CONFIG_REPO_DIR="")

        with mock.patch.object(merge_toml, "build_file") as build:
            self.assertEqual(self.run_driver("codex", environ), 0)
        self.assertEqual(build.call_count, 1)
        (dst, sources), _ = build.call_args
        self.assertEqual(dst, os.path.join(self.dest, "config.toml"))
        self.assertEqual(sources, [
            os.path.join(self.tmp, "layer-user") + "/config.toml",
            os.path.join(self.tmp, "layer-org") + "/config.toml",
        ])

    def test_an_empty_layer_names_an_absolute_keyed_path(self):
        """The keyed merge for an empty layer reads "/opencode.json": absolute,
        absent, and skipped -- never a file relative to the workspace, where a
        checkout could plant one."""
        real = init.merge_config_file
        sources = []

        def spy(src, dst, replace_mcp_entries=False):
            sources.append(src)
            return real(src, dst, replace_mcp_entries=replace_mcp_entries)

        with mock.patch.object(init, "merge_config_file", side_effect=spy):
            self.assertEqual(
                self.run_driver("grok", self.env(SWARMFORGE_CONFIG_REPO_DIR="")), 0)
        self.assertIn("/opencode.json", sources)

    def test_an_empty_layer_names_an_absolute_claude_settings_path(self):
        """The path an empty layer contributes is "/settings.json": absolute,
        absent, and skipped -- never a file relative to the workspace."""
        write_file(self.image_defaults, json.dumps({"key": "image"}))
        environ = self.env(SWARMFORGE_CONFIG_REPO_DIR="")

        with mock.patch.object(merge_json, "build_file") as build:
            self.assertEqual(self.run_driver("claude", environ), 0)
        self.assertEqual(build.call_count, 1)
        (dst, sources), _ = build.call_args
        self.assertEqual(dst, self.settings_file)
        self.assertEqual(sources, [
            self.image_defaults,
            "/settings.json",
            os.path.join(self.tmp, "layer-user") + "/settings.json",
            os.path.join(self.tmp, "layer-org") + "/settings.json",
        ])


class OpencodeTongServers(DriverCase):
    """The generated tong servers merge into opencode.json last, entry by entry.

    The fragment describes containers this run started, so a layer naming the
    same server is describing something else -- and a remote server that
    inherited a local one's keys is a server OpenCode cannot reach.
    """

    LOCAL_GH = {"type": "local", "command": ["gh-mcp"], "enabled": True}

    def stage_layers(self):
        self.write_layer_json(
            "repo", "opencode.json", {"mcp": {"gh": dict(self.LOCAL_GH)}})
        self.write_layer_json(
            "org", "opencode.json",
            {"mcp": {"gh": dict(self.LOCAL_GH)}, "model": "org/m"})

    def test_the_fragment_replaces_whole_entries_and_outranks_every_layer(self):
        self.stage_layers()
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.fragment({
            "mcp": {
                "gh": {"type": "remote", "url": "http://gh"},
                "new": {"type": "remote", "url": "http://new"},
            }
        }))

        self.assertEqual(self.run_driver("opencode", environ), 0)
        merged = read_json(os.path.join(self.dest, "opencode.json"))
        self.assertEqual(merged["mcp"]["gh"], {"type": "remote", "url": "http://gh"})
        self.assertEqual(merged["mcp"]["new"], {"type": "remote", "url": "http://new"})
        self.assertEqual(merged["model"], "org/m")

    def test_without_a_fragment_the_layers_own_servers_stand(self):
        self.stage_layers()
        self.assertEqual(self.run_driver("opencode"), 0)
        merged = read_json(os.path.join(self.dest, "opencode.json"))
        self.assertEqual(merged["mcp"], {"gh": self.LOCAL_GH})


class TomlTongServers(DriverCase):
    """Grok and Codex read tong servers out of a managed block in config.toml.

    Both merge into a config that outlives the run, so the servers cannot be
    appended: the block is rewritten every run and removed when the run has no
    tongs, or a session with none would inherit an earlier session's servers.
    """

    FRAGMENT = {"mcp_servers": {"gh": {"url": "http://gh"}}}

    def assert_block_names_the_server(self, text):
        self.assertIn(merge_toml_mcp.BLOCK_BEGIN, text)
        self.assertIn(merge_toml_mcp.BLOCK_END, text)
        self.assertIn("[mcp_servers.gh]", text)
        self.assertIn('url = "http://gh"', text)

    def test_grok_gains_the_managed_block(self):
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.fragment(self.FRAGMENT))
        self.assertEqual(self.run_driver("grok", environ), 0)
        self.assert_block_names_the_server(
            read_file(os.path.join(self.dest, "config.toml")))

    def test_codex_gains_the_managed_block(self):
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.fragment(self.FRAGMENT))
        self.assertEqual(self.run_driver("codex", environ), 0)
        self.assert_block_names_the_server(
            read_file(os.path.join(self.dest, "config.toml")))

    def test_a_block_carried_in_by_a_layer_is_stripped_when_no_tongs_run(self):
        """Grok's overlay copies config.toml in whole, so a host config still
        holding a previous run's block arrives with it."""
        self.write_layer("repo", "config.toml", "\n".join([
            'model = "repo/m"',
            merge_toml_mcp.BLOCK_BEGIN,
            "[mcp_servers.old]",
            'url = "http://old"',
            merge_toml_mcp.BLOCK_END,
            "",
        ]))

        self.assertEqual(self.run_driver("grok"), 0)
        text = read_file(os.path.join(self.dest, "config.toml"))
        self.assertNotIn(merge_toml_mcp.BLOCK_BEGIN, text)
        self.assertNotIn("mcp_servers.old", text)
        self.assertIn('model = "repo/m"', text)


class ClaudeTongServers(DriverCase):
    """Claude's fragment rides its command line; no file merge carries it.

    The launcher appends `--mcp-config <path>` to claude's argv instead of
    naming the fragment in the environment, so a fragment variable that does
    reach the driver is left exactly where it is.
    """

    def test_the_fragment_is_not_merged_into_any_config_file(self):
        self.write_layer_json("repo", "opencode.json", {"model": "repo/m"})
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.fragment(
            {"mcpServers": {"gh": {"type": "http", "url": "http://gh"}}}))

        self.assertEqual(self.run_driver("claude", environ), 0)
        self.assertEqual(
            read_json(os.path.join(self.dest, "opencode.json")),
            {"model": "repo/m"},
        )
        self.assertFalse(os.path.exists(os.path.join(self.dest, "config.toml")))

    def test_the_spec_records_the_opt_out(self):
        self.assertIsInstance(harness.get("claude").SPEC.mcp_merge, Waiver)


class CodexConfigPipeline(DriverCase):
    """Codex's config.toml is rebuilt from the layers, then published.

    It is rebuilt outside the persistent home -- which also holds session
    state -- and copied back, so config.toml stays a plain file codex can
    replace with its own atomic writes.
    """

    def published(self):
        return os.path.join(self.home, ".codex", "config.toml")

    def test_layer_tables_merge_by_key_with_the_org_layer_winning(self):
        self.write_layer(
            "repo", "config.toml",
            '# a comment from the repo layer\nmodel = "repo/m"\n'
            "[tools]\nweb = true\n")
        self.write_layer("user", "config.toml", '[tools]\nsearch = true\n')
        self.write_layer("org", "config.toml", 'model = "org/m"\n')

        self.assertEqual(self.run_driver("codex"), 0)
        text = read_file(os.path.join(self.dest, "config.toml"))
        self.assertIn('model = "org/m"', text)
        self.assertIn("web = true", text)
        self.assertIn("search = true", text)

    def test_a_layer_comment_never_reaches_the_destination(self):
        """The file is rebuilt from parsed values rather than copied, so
        anything that is not a key is gone -- which is what lets three layers
        contribute to one file at all."""
        self.write_layer(
            "repo", "config.toml", '# a comment from the repo layer\nmodel = "repo/m"\n')

        self.assertEqual(self.run_driver("codex"), 0)
        self.assertNotIn(
            "a comment from the repo layer",
            read_file(os.path.join(self.dest, "config.toml")),
        )

    def test_the_built_file_and_its_managed_block_are_published_whole(self):
        self.write_layer("org", "config.toml", 'model = "org/m"\n')
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.fragment(
            {"mcp_servers": {"gh": {"url": "http://gh"}}}))

        self.assertEqual(self.run_driver("codex", environ), 0)
        built = read_file(os.path.join(self.dest, "config.toml"))
        self.assertIn("[mcp_servers.gh]", built)
        self.assertEqual(read_file(self.published()), built)

    def test_publishing_clears_the_previous_config(self):
        """No layer supplying config.toml is a session with no config, not a
        session that keeps the last one's."""
        write_file(self.published(), 'model = "stale/m"\n')

        self.assertEqual(self.run_driver("codex"), 0)
        self.assertEqual(read_file(self.published()), "")


class ClaudeSettingsBuild(DriverCase):
    """Claude's settings are built from the layers above the image defaults.

    The built file is what the exec hands claude, so it decides the session's
    permissions and hooks. The image's own defaults are the bottom layer: any
    higher and the image would overrule a key a session chose.
    """

    def test_the_layers_stack_above_the_image_defaults(self):
        write_file(
            self.image_defaults,
            json.dumps({"key": "image", "statusLine": {"command": "/sl"}}))
        self.write_layer_json("repo", "settings.json", {"key": "repo", "repoOnly": 1})
        self.write_layer_json("user", "settings.json", {"key": "user"})
        self.write_layer_json("org", "settings.json", {"key": "org"})

        self.assertEqual(self.run_driver("claude"), 0)
        self.assertEqual(
            read_json(self.settings_file),
            {
                "key": "org",
                "statusLine": {"command": "/sl"},
                "repoOnly": 1,
            },
        )

    def test_a_layer_with_no_settings_file_contributes_nothing(self):
        """Every layer is named whether or not it was mounted, so an absent
        file is the ordinary case rather than a failed build."""
        write_file(self.image_defaults, json.dumps({"key": "image"}))
        self.write_layer_json("org", "settings.json", {"key": "org"})

        self.assertEqual(self.run_driver("claude"), 0)
        self.assertEqual(read_json(self.settings_file), {"key": "org"})

    def test_a_failed_build_still_leaves_valid_json_where_the_exec_looks(self):
        """The exec hands claude the path whenever the file exists, so half a
        document there is a session that will not start."""
        write_file(self.image_defaults, json.dumps({"key": "image"}))
        captured = io.StringIO()
        with mock.patch.object(
                merge_json, "build_file", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(captured):
                self.assertEqual(self.run_driver("claude"), 0)

        self.assertIn("could not build Claude settings.json", captured.getvalue())
        self.assertEqual(read_file(self.settings_file), "{}\n")


def fake_spec(**overrides):
    """A registrable spec whose config destination comes from the run."""
    fields = dict(
        name="fake",
        binary="fake",
        config_dest=Waiver("the run's SWARMFORGE_CONFIG_DEST names the destination"),
        config_reset=False,
        layer_excludes=(),
        keyed_files=("opencode.json",),
        skills_dest=Waiver("no portable skills destination is declared"),
        commands_dest=Waiver("no portable commands destination is declared"),
        agents_dest=Waiver("unified agent definitions are not delivered"),
        mcp_fragment=lambda servers: {},
        mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
        mcp_merge="json-replace-mcp",
        agent_emitter=Waiver("no emitter is defined"),
        extra_chown_paths=(),
    )
    fields.update(overrides)
    return HarnessSpec(**fields)


class HookContract(DriverCase):
    """The config hooks run in order, around the merge each one depends on.

    build_config rebuilds what the overlay left out, so it runs before the
    generated servers merge; finalize_config reads the finished config, so it
    runs after; publish_config delivers it, so it runs last. A hook moved
    across one of those boundaries publishes a config that is missing whatever
    the step it jumped was going to add.
    """

    def setUp(self):
        super().setUp()
        self.calls = []
        self.fragment_value = {"mcp": {"gh": {"type": "remote", "url": "http://gh"}}}
        self.tong = self.fragment(self.fragment_value)

        def record(label):
            def hook(ctx):
                merged = os.path.join(ctx.config_dest, "opencode.json")
                self.calls.append((
                    label, ctx,
                    read_json(merged) if os.path.isfile(merged) else None,
                ))
            return hook

        self.spec = fake_spec(
            build_config=record("build"),
            finalize_config=record("finalize"),
            publish_config=record("publish"),
        )
        self.module = types.SimpleNamespace(SPEC=self.spec)

    def run_fake(self):
        environ = self.env(SWARMFORGE_TONG_MCP_FILE=self.tong)
        with mock.patch.dict(harness._REGISTRY, {"fake": self.module}):
            return init.initialize("fake", self.home, environ)

    def test_the_hooks_run_in_order(self):
        self.assertEqual(self.run_fake(), 0)
        self.assertEqual([label for label, _, _ in self.calls],
                         ["build", "finalize", "publish"])

    def test_the_generated_servers_land_between_build_and_finalize(self):
        self.assertEqual(self.run_fake(), 0)
        observed = {label: merged for label, _, merged in self.calls}
        self.assertIsNone(observed["build"])
        self.assertEqual(observed["finalize"], self.fragment_value)

    def test_every_hook_is_handed_the_paths_the_run_staged(self):
        self.assertEqual(self.run_fake(), 0)
        expected = Context(
            harness="fake",
            home=self.home,
            config_dest=self.dest,
            config_repo_src=os.path.join(self.tmp, "layer-repo"),
            config_user_src=os.path.join(self.tmp, "layer-user"),
            config_org_src=os.path.join(self.tmp, "layer-org"),
            tong_mcp_file=self.tong,
        )
        for label, ctx, _ in self.calls:
            with self.subTest(hook=label):
                self.assertEqual(ctx, expected)


class DriverArgv(DriverCase):
    """The entrypoint's invocation is unguarded, so its argv has to hold.

    A failure here stops the container rather than degrading, which is the
    point: a session running on unmerged config is a session running under
    somebody else's permissions.
    """

    def test_an_unregistered_harness_is_refused(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status = init.initialize("nosuch", self.home, {})
        self.assertEqual(status, 2)
        self.assertIn("unknown harness: nosuch", captured.getvalue())

    def test_too_few_arguments_are_refused(self):
        for argv in ([], ["one"]):
            with self.subTest(argv=argv):
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured):
                    self.assertEqual(init.main(argv), 2)
                self.assertIn(init.USAGE, captured.getvalue())

    def test_the_driver_runs_against_the_staged_package(self):
        """The image copies the package under an import root and invokes the
        driver out of it with `python3 -P -m`. This stages that shape, with
        the checkout off both sys.path and the working directory, so a module
        that only resolves its imports from the repo fails here."""
        libdir = os.path.join(self.tmp, "lib")
        os.makedirs(libdir)
        shutil.copytree(
            os.path.join(REPO_ROOT, "swarmforge"),
            os.path.join(libdir, "swarmforge"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.write_layer_json("repo", "opencode.json", {"model": "repo/m"})
        self.write_layer_json("org", "opencode.json", {"model": "org/m"})

        environ = self.env()
        environ["PATH"] = os.environ.get("PATH", "")
        environ["PYTHONPATH"] = libdir
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "swarmforge.harness.init", "grok", self.home],
            env=environ, cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(
            completed.returncode, 0, "driver failed:\n%s" % completed.stderr)
        self.assertEqual(
            read_json(os.path.join(self.dest, "opencode.json")), {"model": "org/m"})


class SpecEntrypointAgreement(unittest.TestCase):
    """The paths the driver writes and the entrypoint acts on are one string.

    The driver merges into a destination its harness module names, while the
    entrypoint links state into it, hands it to the anvil uid, and exports it
    to claude -- each from its own literal. A drift between the two is silent:
    the merge lands somewhere nothing reads.
    """

    def setUp(self):
        with open(ENTRYPOINT, encoding="utf-8") as handle:
            self.entrypoint = handle.read()

    def literal(self, name):
        """The value of a top-level `name="..."` assignment with no expansion."""
        match = re.search(r'^%s="([^"$]+)"$' % name, self.entrypoint, re.M)
        self.assertIsNotNone(match, "entrypoint defines no literal %s" % name)
        return match.group(1)

    def test_claude_merges_into_the_config_home_the_entrypoint_serves(self):
        self.assertEqual(
            harness.get("claude").SPEC.config_dest,
            self.literal("CLAUDE_CONFIG_HOME"),
        )

    def test_the_settings_build_writes_where_the_exec_reads(self):
        self.assertEqual(claude.SETTINGS_FILE, self.literal("CLAUDE_SETTINGS_FILE"))

    def test_pinned_destinations_stay_outside_every_host_mount(self):
        """/home/anvil is the persistent home every container for this user
        shares and /workspace is the checkout; a merged layer or a rebuilt
        config landing in either outlives the container."""
        for name in ("claude", "codex"):
            dest = harness.get(name).SPEC.config_dest
            for mounted in ("/home/", "/workspace"):
                with self.subTest(harness=name, mount=mounted):
                    self.assertFalse(
                        dest.startswith(mounted),
                        "%s merges into a host mount: %s" % (name, dest))

    def test_codex_agents_land_where_the_entrypoint_chowns(self):
        """The spec names where the translated agents are generated and the
        entrypoint hands that directory to the anvil uid by its own literal.
        Drift between the two leaves the generated agents owned by root."""
        self.assertEqual(
            harness.get("codex").SPEC.agents_dest,
            self.literal("CODEX_AGENTS_HOME"),
        )


if __name__ == "__main__":
    unittest.main()
