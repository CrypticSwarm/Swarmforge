#!/usr/bin/env python3
"""Behavior tests for the container's agent translation phase.

One unified agent definition serves every harness, so the phase is what turns
it into subagents a session can actually delegate to: it reads the same four
asset sources for every harness, lowest to highest precedence, and writes the
native files to the destination that harness's spec declares. Every guarantee
here is checked by running the phase over staged source trees in a temporary
directory and reading what landed, so a reordered source list or a destination
that drifted from the spec fails a test rather than surfacing as a session
whose subagents are quietly missing.

Nothing here may write outside the temporary directory: the harnesses that pin
a config or agents destination under /run/swarmforge have those destinations,
and Claude's two settings paths, redirected for the duration of each run.

Run: python3 tests/test_harness_agents.py
"""

import contextlib
import dataclasses
import io
import os
import shutil
import sys
import tempfile
import tomllib
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
from swarmforge.agents import translate
from swarmforge.config import merge_toml
from swarmforge.harness import claude, init
from swarmforge.harness.spec import Waiver

AGENT_MD = """---
description: Reviews code for defects.
mode: subagent
model: anthropic/claude-sonnet-4-6
---

You are the reviewer agent.
"""

# The asset layers, lowest precedence first. The workspace overlay ranks above
# all three and arrives as a path rather than a mounted layer.
LAYERS = ("user", "org", "repo")


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


def agent_document(body):
    """A unified agent definition whose body says which source it came from."""
    return AGENT_MD.replace("You are the reviewer agent.", body)


class TranslationCase(unittest.TestCase):
    """Runs the translation phase over staged sources inside a temporary dir.

    Every path a harness can write to is rooted here. The harnesses that pin a
    destination get those fields replaced for the run, so a test never reaches
    the live /run/swarmforge the development host has.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-agents-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        # Codex publishes into a directory its image already ships.
        os.makedirs(os.path.join(self.home, ".codex"))
        self.workspace = os.path.join(self.tmp, "workspace")
        self.dest = os.path.join(self.tmp, "dest")
        self.agents_dest = os.path.join(self.tmp, "codex-agents")
        self.settings_file = os.path.join(self.tmp, "claude-settings.json")
        self.image_defaults = os.path.join(self.tmp, "image-defaults.json")

    def source(self, layer):
        """The agents directory of one asset source, created on first use."""
        root = (self.workspace + "/.swarmforge" if layer == "workspace"
                else os.path.join(self.tmp, "assets-" + layer))
        path = os.path.join(root, "agents")
        os.makedirs(path, exist_ok=True)
        return path

    def stage(self, layer, filename, text):
        return write_file(os.path.join(self.source(layer), filename), text)

    def env(self, **overrides):
        """The environment the entrypoint hands the driver.

        Every asset variable names a directory whether or not it was staged,
        which is how the container invokes it: an unmounted layer is a path
        that is simply not there.
        """
        environ = {
            "SWARMFORGE_ASSETS_USER_DIR": os.path.join(self.tmp, "assets-user"),
            "SWARMFORGE_ASSETS_ORG_DIR": os.path.join(self.tmp, "assets-org"),
            "SWARMFORGE_ASSETS_REPO_DIR": os.path.join(self.tmp, "assets-repo"),
        }
        environ.update(overrides)
        return {name: value for name, value in environ.items() if value is not None}

    @contextlib.contextmanager
    def redirected(self, name):
        """Every path `name` pins, replaced by one under the staging tree."""
        module = harness.get(name)
        self.assertIsNotNone(module, "no harness registered as %s" % name)
        with contextlib.ExitStack() as stack:
            if name == "claude":
                stack.enter_context(mock.patch.object(
                    module, "SPEC",
                    dataclasses.replace(module.SPEC, config_dest=self.dest)))
                stack.enter_context(
                    mock.patch.object(claude, "SETTINGS_FILE", self.settings_file))
                stack.enter_context(mock.patch.object(
                    claude, "IMAGE_DEFAULT_SETTINGS", self.image_defaults))
            if name == "codex":
                stack.enter_context(mock.patch.object(
                    module, "SPEC",
                    dataclasses.replace(
                        module.SPEC,
                        config_dest=self.dest,
                        agents_dest=self.agents_dest)))
            yield

    def translate_agents(self, name, environ=None):
        """Run the translation phase for `name` with every pinned path moved."""
        with self.redirected(name):
            return init.translate_agents(
                name, self.home,
                self.env() if environ is None else environ,
                self.workspace,
            )


class NativeDestinations(TranslationCase):
    """Each harness's translated agents land where its spec says they do.

    The destination is the only thing that differs between harnesses, and it
    is where that harness discovers subagents -- a file one directory off is
    a session that silently has none.
    """

    def test_claude_agents_land_under_its_config_destination(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        self.assertEqual(self.translate_agents("claude"), 0)
        written = read_file(os.path.join(self.dest, "agents", "reviewer.md"))
        self.assertIn("name: reviewer", written)
        self.assertIn("model: claude-sonnet-4-6", written)
        self.assertIn("You are the reviewer agent.", written)

    def test_opencode_agents_land_under_the_destination_the_run_names(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        environ = self.env(SWARMFORGE_CONFIG_DEST=self.dest)
        self.assertEqual(self.translate_agents("opencode", environ), 0)
        written = read_file(os.path.join(self.dest, "agents", "reviewer.md"))
        self.assertIn("mode: subagent", written)
        self.assertIn("model: anthropic/claude-sonnet-4-6", written)
        self.assertIn("You are the reviewer agent.", written)

    def test_codex_agents_land_in_the_generated_agents_directory(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        self.assertEqual(self.translate_agents("codex"), 0)
        written = read_file(os.path.join(self.agents_dest, "reviewer.toml"))
        self.assertIn('name = "reviewer"', written)
        self.assertIn("You are the reviewer agent.", written)


class SourcePrecedence(TranslationCase):
    """The four sources stack user, org, repo, then the workspace overlay.

    A definition is named by its filename, so every source can hold the same
    agent; specificity decides which one the session gets, and the workspace
    is the most specific thing a run has.
    """

    def test_the_workspace_overlay_outranks_every_asset_layer(self):
        for layer in LAYERS + ("workspace",):
            self.stage(layer, "reviewer.md",
                       agent_document("I came from the %s source." % layer))

        self.assertEqual(self.translate_agents("claude"), 0)
        written = read_file(os.path.join(self.dest, "agents", "reviewer.md"))
        self.assertIn("I came from the workspace source.", written)
        for layer in LAYERS:
            self.assertNotIn("I came from the %s source." % layer, written)

    def test_a_higher_asset_layer_wins_without_the_workspace(self):
        for layer in LAYERS:
            self.stage(layer, "reviewer.md",
                       agent_document("I came from the %s source." % layer))

        self.assertEqual(self.translate_agents("claude"), 0)
        written = read_file(os.path.join(self.dest, "agents", "reviewer.md"))
        self.assertIn("I came from the repo source.", written)


class OpencodeDestinationFallback(TranslationCase):
    """With no destination named, opencode's agents go to its home config dir.

    Opencode discovers agents under whatever config directory it is running
    on, and a run that names none is running on the home one -- so unlike the
    config phase, which has nowhere safe to guess, the translation has exactly
    one place the files can go.
    """

    def expected(self):
        return os.path.join(self.home, ".config", "opencode", "agents", "reviewer.md")

    def test_an_empty_destination_variable_falls_back_to_the_home(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        environ = self.env(SWARMFORGE_CONFIG_DEST="")
        self.assertEqual(self.translate_agents("opencode", environ), 0)
        self.assertIn("You are the reviewer agent.", read_file(self.expected()))

    def test_an_absent_destination_variable_falls_back_to_the_home(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        environ = self.env(SWARMFORGE_CONFIG_DEST=None)
        self.assertNotIn("SWARMFORGE_CONFIG_DEST", environ)
        self.assertEqual(self.translate_agents("opencode", environ), 0)
        self.assertIn("You are the reviewer agent.", read_file(self.expected()))


class WaivedTranslation(TranslationCase):
    """A harness that declares no destination is skipped in silence.

    The waiver is the opt-out on record, not a missing feature, so staged
    definitions are left alone and the phase says nothing -- a warning here
    would show up in every grok session that has agents on disk.
    """

    def snapshot(self):
        """Every path under the staging tree, so any write at all shows."""
        found = set()
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                found.add(os.path.join(dirpath, name))
        return found

    def test_grok_records_the_opt_out(self):
        self.assertIsInstance(harness.get("grok").SPEC.agents_dest, Waiver)

    def test_nothing_is_written_and_nothing_is_said(self):
        for layer in LAYERS + ("workspace",):
            self.stage(layer, "reviewer.md", AGENT_MD)

        before = self.snapshot()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status = self.translate_agents("grok")
        self.assertEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(captured.getvalue(), "")


class FailedTranslation(TranslationCase):
    """A translation that fails warns and lets the container come up.

    A session can run without subagents, while a container stopped over them
    serves nobody -- so the phase reports the failure and returns success.
    """

    WARNING = "Warning: unified agent translation failed for claude; continuing"

    def run_with(self, **patch):
        captured = io.StringIO()
        with mock.patch.object(init.translate, "run", **patch):
            with contextlib.redirect_stderr(captured):
                status = self.translate_agents("claude")
        return status, captured.getvalue()

    def test_a_raised_error_becomes_a_warning(self):
        status, stderr = self.run_with(side_effect=RuntimeError("boom"))
        self.assertEqual(status, 0)
        self.assertIn(self.WARNING, stderr)

    def test_a_nonzero_status_becomes_the_same_warning(self):
        status, stderr = self.run_with(return_value=2)
        self.assertEqual(status, 0)
        self.assertIn(self.WARNING, stderr)

    def test_a_clean_translation_says_nothing(self):
        self.stage("user", "reviewer.md", AGENT_MD)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.assertEqual(self.translate_agents("claude"), 0)
        self.assertNotIn(self.WARNING, captured.getvalue())


class CodexRegistration(TranslationCase):
    """Codex discovers a generated agent only once its config.toml names it.

    The agent files are written outside codex's home, so the registrations
    have to reach the config it actually reads. They merge under the published
    config's own keys: the layers describe the session, the registrations only
    say where the generated files are.
    """

    def published(self):
        return os.path.join(self.home, ".codex", "config.toml")

    def config_layer(self, text='model = "org/m"\n'):
        """An org config layer, which the config phase publishes into the home."""
        return write_file(os.path.join(self.tmp, "layer-org", "config.toml"), text)

    def read_published(self):
        with open(self.published(), "rb") as handle:
            return tomllib.load(handle)

    def test_registrations_merge_into_the_published_config(self):
        self.config_layer()
        self.stage("user", "reviewer.md", AGENT_MD)
        environ = self.env(
            SWARMFORGE_CONFIG_ORG_DIR=os.path.join(self.tmp, "layer-org"),
            SWARMFORGE_CONFIG_DEST=self.dest,
        )

        with self.redirected("codex"):
            status = init.run("codex", self.home, environ, self.workspace)
        self.assertEqual(status, 0)

        published = self.read_published()
        self.assertEqual(published["model"], "org/m")
        self.assertEqual(
            published["agents"]["reviewer"]["config_file"],
            os.path.join(self.agents_dest, "reviewer.toml"),
        )

    def test_a_failed_registration_leaves_the_generated_agents_in_place(self):
        """The merge is the last step, so its failure costs the session its
        subagents and nothing else; the files it could not register stay
        where they were written."""
        self.stage("user", "reviewer.md", AGENT_MD)

        captured = io.StringIO()
        with mock.patch.object(
                merge_toml, "build_file", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(captured):
                status = self.translate_agents("codex")

        self.assertEqual(status, 0)
        self.assertIn(
            "Warning: Codex agent registration failed; continuing",
            captured.getvalue(),
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.agents_dest, "reviewer.toml")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.agents_dest, "config.toml")))

    def test_a_published_registration_outranks_the_generated_one(self):
        """The published config is the higher-precedence merge source, so a
        registration it already carries survives the generated one."""
        write_file(
            self.published(),
            '[agents.reviewer]\nconfig_file = "/pinned/reviewer.toml"\n')
        self.stage("user", "reviewer.md", AGENT_MD)

        self.assertEqual(self.translate_agents("codex"), 0)
        self.assertEqual(
            self.read_published()["agents"]["reviewer"]["config_file"],
            "/pinned/reviewer.toml",
        )

    def test_no_emitted_agents_leaves_the_published_config_untouched(self):
        """An empty registration is not an empty `agents` table: a session
        with no definitions must read the config it would have read anyway."""
        seed = 'model = "org/m"\n'
        write_file(self.published(), seed)

        self.assertEqual(self.translate_agents("codex"), 0)
        self.assertFalse(
            os.path.exists(os.path.join(self.agents_dest, "config.toml")))
        self.assertEqual(read_file(self.published()), seed)

    def test_the_command_line_translation_publishes_nothing(self):
        """The CLI runs against a directory pair, with no container home to
        publish into -- so it writes the registrations beside the agents and
        stops there."""
        seed = 'model = "org/m"\n'
        write_file(self.published(), seed)
        self.stage("user", "reviewer.md", AGENT_MD)

        status = translate.main(
            ["codex", self.agents_dest, self.source("user")])
        self.assertEqual(status, 0)
        self.assertTrue(
            os.path.isfile(os.path.join(self.agents_dest, "config.toml")))
        self.assertEqual(read_file(self.published()), seed)


class PhaseOrder(TranslationCase):
    """The config phase runs first, and the translation only if it succeeded.

    Two harnesses resolve their agents destination out of the merged config
    destination, and codex registers into the config the merge publishes -- so
    a translation reaching either before the merge writes into a directory
    about to be rebuilt, or into a config about to be replaced.
    """

    def test_the_config_phase_runs_before_the_translation(self):
        calls = []

        def record(label, status):
            def phase(*args, **kwargs):
                calls.append(label)
                return status
            return phase

        with mock.patch.object(init, "initialize", record("config", 0)):
            with mock.patch.object(init, "translate_agents", record("agents", 0)):
                with mock.patch.object(init, "install_assets", record("assets", 0)):
                    with self.redirected("claude"):
                        status = init.run(
                            "claude", self.home, self.env(), self.workspace)

        self.assertEqual(status, 0)
        self.assertEqual(calls, ["config", "agents", "assets"])

    def test_a_failed_config_phase_stops_the_run(self):
        """The config phase failing takes the container down, so the phases
        after it must not run at all: a session on unmerged config is a
        session running under somebody else's permissions."""
        translated = mock.Mock()
        installed = mock.Mock()
        with mock.patch.object(init, "initialize", return_value=2):
            with mock.patch.object(init, "translate_agents", translated):
                with mock.patch.object(init, "install_assets", installed):
                    with self.redirected("claude"):
                        status = init.run(
                            "claude", self.home, self.env(), self.workspace)

        self.assertEqual(status, 2)
        translated.assert_not_called()
        installed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
