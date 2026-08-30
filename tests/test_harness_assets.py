#!/usr/bin/env python3
"""Behavior tests for the container's portable asset install.

Skills and commands are portable across harnesses, so the phase that delivers
them is a copy into whatever location the harness reads: it stacks the same
four sources for every harness, lowest to highest precedence, and replaces each
top-level entry wholesale rather than merging into it. Every guarantee here is
checked by running the phase over staged source trees in a temporary directory
and reading what landed, so a reordered layer, a destination that drifted from
the spec, or a copy that started merging fails a test rather than surfacing as
a session running somebody else's version of a skill.

Nothing here may write outside the temporary directory: the harnesses that pin
a config destination under /run/swarmforge have it redirected for the duration
of each run, and the rest resolve their destinations under the staged home.

Run: python3 tests/test_harness_assets.py
"""

import contextlib
import dataclasses
import io
import os
import shutil
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
from swarmforge.harness import codex, init
from swarmforge.harness.spec import Waiver

# The asset layers, lowest precedence first.
LAYERS = ("user", "org", "shared", "workspace")

HARNESSES = ("claude", "codex", "grok", "opencode")

# What the translator writes in place of the portable argument placeholder.
ARGUMENTS = "the arguments supplied with this skill invocation"


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


def command_document(description, body):
    """A portable slash command, the shape the translator reads."""
    if description is None:
        return "---\nmode: subagent\n---\n\n%s\n" % body
    return "---\ndescription: %s\n---\n\n%s\n" % (description, body)


def translated_command(name, description, body):
    """The skill package text the translator writes for one command."""
    return "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (
        name, description, body)


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


class AssetCase(unittest.TestCase):
    """Runs the asset phase over staged sources inside a temporary directory.

    Every path a harness can write to is rooted here. Claude pins a config
    destination its assets resolve out of, so that field is replaced for the
    run and a test never reaches the live /run/swarmforge the development host
    has.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-assets-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.workspace = os.path.join(self.tmp, "workspace")
        self.dest = os.path.join(self.tmp, "dest")
        self.dotagents_user = os.path.join(self.tmp, "dotagents-user")
        self.dotagents_org = os.path.join(self.tmp, "dotagents-org")
        self.shared_skills = os.path.join(self.tmp, "shared-skills")
        self.shared_commands = os.path.join(self.tmp, "shared-commands")

    def skills_src(self, layer):
        """One layer's portable skills directory, created on first use."""
        path = {
            "user": os.path.join(self.dotagents_user, "skills"),
            "org": os.path.join(self.dotagents_org, "skills"),
            "shared": self.shared_skills,
            "workspace": os.path.join(self.workspace, ".agents", "skills"),
        }[layer]
        os.makedirs(path, exist_ok=True)
        return path

    def commands_src(self, layer):
        """One layer's portable commands directory, created on first use."""
        path = {
            "user": os.path.join(self.dotagents_user, "commands"),
            "org": os.path.join(self.dotagents_org, "commands"),
            "shared": self.shared_commands,
            "workspace": os.path.join(self.workspace, ".agents", "commands"),
        }[layer]
        os.makedirs(path, exist_ok=True)
        return path

    def stage_skill(self, layer, package, text):
        """A skill package in `layer`, named by its directory."""
        return write_file(
            os.path.join(self.skills_src(layer), package, "SKILL.md"), text)

    def stage_command(self, layer, name, description="Does a thing.",
                      body="Do the thing."):
        """A portable slash command in `layer`, named by its filename."""
        return write_file(
            os.path.join(self.commands_src(layer), name + ".md"),
            command_document(description, body))

    def skills_dest(self, name):
        return {
            "claude": os.path.join(self.dest, "skills"),
            "codex": os.path.join(self.home, ".agents", "skills"),
            "grok": os.path.join(self.home, ".grok", "skills"),
            "opencode": os.path.join(self.dest, "skills"),
        }[name]

    def commands_dest(self, name):
        return {
            "claude": os.path.join(self.dest, "commands"),
            "grok": os.path.join(self.home, ".grok", "commands"),
            "opencode": os.path.join(self.dest, "command"),
        }[name]

    def env(self, **overrides):
        """The environment the entrypoint hands the driver.

        Every layer variable names a directory whether or not it was staged,
        which is how the container invokes it: an unmounted layer is a path
        that is simply not there.
        """
        environ = {
            "SWARMFORGE_DOTAGENTS_USER_DIR": self.dotagents_user,
            "SWARMFORGE_DOTAGENTS_ORG_DIR": self.dotagents_org,
            "SWARMFORGE_SKILLS_DIR": self.shared_skills,
            "SWARMFORGE_COMMAND_DIR": self.shared_commands,
            "SWARMFORGE_CONFIG_DEST": self.dest,
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
            yield

    def install(self, name, environ=None):
        """Run the asset phase for `name` with every pinned path moved."""
        with self.redirected(name):
            return init.install_assets(
                name, self.home,
                self.env() if environ is None else environ,
                self.workspace,
            )


class LayerPrecedence(AssetCase):
    """The four sources stack user, org, shared, then the workspace overlay.

    An asset is named by its top-level entry, so every layer can ship the same
    one; specificity decides which the session gets, and the workspace is the
    most specific thing a run has. The order is identical for every harness --
    only the destination differs.
    """

    def test_the_workspace_overlay_outranks_every_lower_layer(self):
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                for layer in LAYERS:
                    self.stage_skill(layer, "shared", "%s skill\n" % layer)
                    self.stage_skill(layer, layer + "-only", "%s only\n" % layer)

                self.assertEqual(self.install(name), 0)
                dest = self.skills_dest(name)
                self.assertEqual(
                    read_file(os.path.join(dest, "shared", "SKILL.md")),
                    "workspace skill\n")
                for layer in LAYERS:
                    self.assertEqual(
                        read_file(
                            os.path.join(dest, layer + "-only", "SKILL.md")),
                        "%s only\n" % layer)

    def test_commands_stack_the_same_way_for_the_harnesses_that_take_them(self):
        for name in ("claude", "grok", "opencode"):
            with self.subTest(harness=name):
                self.setUp()
                for layer in LAYERS:
                    self.stage_command(layer, "shared", body="%s body" % layer)
                    self.stage_command(layer, layer + "-only")

                self.assertEqual(self.install(name), 0)
                dest = self.commands_dest(name)
                self.assertIn(
                    "workspace body", read_file(os.path.join(dest, "shared.md")))
                for layer in LAYERS:
                    self.assertTrue(
                        os.path.isfile(
                            os.path.join(dest, layer + "-only.md")))


class WholesaleReplacement(AssetCase):
    """A higher layer replaces a whole entry rather than merging into it.

    A merge would let a lower layer contribute stray files to a package it no
    longer owns, and would leave per-entry symlinks from an earlier run
    standing over the real thing.
    """

    def test_an_earlier_package_contributes_nothing_to_the_one_that_replaces_it(self):
        dest = self.skills_dest("claude")
        write_file(os.path.join(dest, "pkg", "SKILL.md"), "stale\n")
        write_file(os.path.join(dest, "pkg", "EXTRA.md"), "stale extra\n")
        self.stage_skill("shared", "pkg", "fresh\n")

        self.assertEqual(self.install("claude"), 0)
        self.assertEqual(
            read_file(os.path.join(dest, "pkg", "SKILL.md")), "fresh\n")
        self.assertFalse(os.path.exists(os.path.join(dest, "pkg", "EXTRA.md")))

    def test_a_stale_symlink_is_replaced_by_the_real_entry(self):
        """A dangling link is what an earlier run's per-entry symlink becomes
        once its target is gone, and a copy onto it would follow it nowhere."""
        dest = self.skills_dest("claude")
        os.makedirs(dest)
        os.symlink(os.path.join(self.tmp, "gone"), os.path.join(dest, "pkg"))
        self.stage_skill("shared", "pkg", "fresh\n")

        self.assertEqual(self.install("claude"), 0)
        self.assertFalse(os.path.islink(os.path.join(dest, "pkg")))
        self.assertEqual(
            read_file(os.path.join(dest, "pkg", "SKILL.md")), "fresh\n")


class EntrySelection(AssetCase):
    """What counts as a top-level entry, and what a copy of one preserves.

    The portable contract is a directory of named skill and command entries;
    a dotfile beside them is the authoring tool's droppings, while a dotfile
    inside one is part of the package.
    """

    def test_top_level_dot_entries_never_land(self):
        src = self.skills_src("user")
        write_file(os.path.join(src, ".DS_Store"), "junk\n")
        write_file(os.path.join(src, ".git", "HEAD"), "junk\n")
        self.stage_skill("user", "pkg", "kept\n")

        self.assertEqual(self.install("claude"), 0)
        dest = self.skills_dest("claude")
        self.assertEqual(sorted(os.listdir(dest)), ["pkg"])

    def test_a_hidden_file_inside_a_package_travels_with_it(self):
        self.stage_skill("user", "pkg", "kept\n")
        write_file(os.path.join(self.skills_src("user"), "pkg", ".meta"), "meta\n")

        self.assertEqual(self.install("claude"), 0)
        self.assertEqual(
            read_file(os.path.join(self.skills_dest("claude"), "pkg", ".meta")),
            "meta\n")

    def test_a_loose_file_lands_beside_the_packages(self):
        write_file(os.path.join(self.skills_src("user"), "loose.md"), "loose\n")
        self.stage_skill("user", "pkg", "kept\n")

        self.assertEqual(self.install("claude"), 0)
        dest = self.skills_dest("claude")
        self.assertEqual(read_file(os.path.join(dest, "loose.md")), "loose\n")

    def test_a_symlinked_entry_lands_as_a_symlink_to_the_same_target(self):
        """Copying the tree behind it would duplicate whatever it points at,
        including a target outside the layer the run agreed to read."""
        src = self.skills_src("user")
        self.stage_skill("user", "pkg", "kept\n")
        os.symlink("pkg", os.path.join(src, "alias"))

        self.assertEqual(self.install("claude"), 0)
        alias = os.path.join(self.skills_dest("claude"), "alias")
        self.assertTrue(os.path.islink(alias))
        self.assertEqual(os.readlink(alias), "pkg")


class AbsentLayers(AssetCase):
    """A layer with nothing in it contributes nothing, and creates nothing.

    Every layer variable names a path whether or not the run mounted anything
    there, so an absent layer is the common case -- and a destination created
    for one is an empty directory the harness advertises as populated.
    """

    def snapshot(self):
        """Every path under the staging tree, so any write at all shows."""
        found = set()
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                found.add(os.path.join(dirpath, name))
        return found

    def assert_nothing_written(self, name, environ=None):
        before = self.snapshot()
        self.assertEqual(self.install(name, environ), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.skills_dest(name)))

    def test_no_destination_is_created_for_a_harness_with_no_sources(self):
        for name in HARNESSES:
            with self.subTest(harness=name):
                self.setUp()
                self.assert_nothing_written(name)

    def test_empty_and_absent_layer_variables_behave_alike(self):
        empty = self.env(
            SWARMFORGE_DOTAGENTS_USER_DIR="",
            SWARMFORGE_DOTAGENTS_ORG_DIR="",
            SWARMFORGE_SKILLS_DIR="",
            SWARMFORGE_COMMAND_DIR="",
        )
        self.assert_nothing_written("claude", empty)

        absent = self.env(
            SWARMFORGE_DOTAGENTS_USER_DIR=None,
            SWARMFORGE_DOTAGENTS_ORG_DIR=None,
            SWARMFORGE_SKILLS_DIR=None,
            SWARMFORGE_COMMAND_DIR=None,
        )
        self.assertNotIn("SWARMFORGE_SKILLS_DIR", absent)
        self.assert_nothing_written("claude", absent)


class NativeDestinations(AssetCase):
    """Each harness's assets land where its spec says they do.

    The destination is the only thing that differs between harnesses, and it
    is where that harness discovers skills and commands -- one directory off
    is a session that silently has none.
    """

    def stage_one_of_each(self):
        self.stage_skill("shared", "pkg", "shared\n")
        self.stage_command("shared", "cmd")

    def test_claude_lands_under_its_config_destination(self):
        self.stage_one_of_each()
        self.assertEqual(self.install("claude"), 0)
        self.assertEqual(
            read_file(os.path.join(self.dest, "skills", "pkg", "SKILL.md")),
            "shared\n")
        self.assertTrue(
            os.path.isfile(os.path.join(self.dest, "commands", "cmd.md")))

    def test_grok_lands_under_its_home_directory(self):
        self.stage_one_of_each()
        self.assertEqual(self.install("grok"), 0)
        self.assertEqual(
            read_file(
                os.path.join(self.home, ".grok", "skills", "pkg", "SKILL.md")),
            "shared\n")
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.home, ".grok", "commands", "cmd.md")))

    def test_opencode_lands_under_the_destination_the_run_names(self):
        """Opencode's singular `command` directory is its own spelling; the
        portable layer is `commands` on every side of the copy."""
        self.stage_one_of_each()
        self.assertEqual(self.install("opencode"), 0)
        self.assertEqual(
            read_file(os.path.join(self.dest, "skills", "pkg", "SKILL.md")),
            "shared\n")
        self.assertTrue(
            os.path.isfile(os.path.join(self.dest, "command", "cmd.md")))

    def test_an_empty_opencode_destination_variable_falls_back_to_the_home(self):
        self.stage_one_of_each()
        environ = self.env(SWARMFORGE_CONFIG_DEST="")
        self.assertEqual(self.install("opencode", environ), 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.home, ".config", "opencode", "skills", "pkg", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.home, ".config", "opencode", "command", "cmd.md")))

    def test_an_absent_opencode_destination_variable_falls_back_to_the_home(self):
        self.stage_one_of_each()
        environ = self.env(SWARMFORGE_CONFIG_DEST=None)
        self.assertNotIn("SWARMFORGE_CONFIG_DEST", environ)
        self.assertEqual(self.install("opencode", environ), 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.home, ".config", "opencode", "skills", "pkg", "SKILL.md")))

    def test_codex_records_that_it_has_no_commands_directory(self):
        self.assertIsInstance(harness.get("codex").SPEC.commands_dest, Waiver)

    def test_codex_writes_only_skills(self):
        """Skills are codex's sole extension point, so a commands directory
        anywhere under its home would be one nothing ever reads."""
        self.stage_one_of_each()
        self.assertEqual(self.install("codex"), 0)
        self.assertEqual(
            read_file(os.path.join(
                self.home, ".agents", "skills", "pkg", "SKILL.md")),
            "shared\n")
        for dirpath, dirnames, _ in os.walk(self.home):
            for name in dirnames:
                self.assertNotIn(
                    name, ("command", "commands"),
                    "codex wrote a commands directory at %s" % dirpath)


class CodexCommandTranslation(AssetCase):
    """Codex reads portable commands only once they are skill packages.

    Translation runs first within each layer, so a package the same layer
    ships under a command's name is the layer's own final word, while a later
    layer's command still replaces an earlier layer's package.
    """

    def skill_text(self, name):
        return read_file(
            os.path.join(self.home, ".agents", "skills", name, "SKILL.md"))

    def test_a_portable_command_becomes_a_skill_package(self):
        self.stage_command(
            "user", "review", description="Reviews code.",
            body="Review $ARGUMENTS closely.")

        self.assertEqual(self.install("codex"), 0)
        self.assertEqual(
            self.skill_text("review"),
            translated_command(
                "review", "Reviews code.",
                "Review %s closely." % ARGUMENTS))

    def test_a_command_without_a_description_is_skipped_with_a_warning(self):
        """The translated frontmatter is what codex matches a skill on, so a
        command with nothing to put there cannot become one."""
        self.stage_command("user", "vague", description=None)

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.assertEqual(self.install("codex"), 0)
        self.assertIn("command has no description", captured.getvalue())
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".agents", "skills", "vague")))

    def test_a_same_layer_package_wins_over_the_translated_command(self):
        self.stage_command("user", "review", description="Reviews code.")
        self.stage_skill("user", "review", "the hand-written package\n")

        self.assertEqual(self.install("codex"), 0)
        self.assertEqual(self.skill_text("review"), "the hand-written package\n")

    def test_a_later_layer_command_replaces_an_earlier_layer_package(self):
        self.stage_skill("shared", "review", "the shared package\n")
        self.stage_command(
            "workspace", "review", description="Reviews this checkout.")

        self.assertEqual(self.install("codex"), 0)
        self.assertEqual(
            self.skill_text("review"),
            translated_command(
                "review", "Reviews this checkout.", "Do the thing."))


class FailedCodexTranslation(AssetCase):
    """A translation that fails warns and lets the container come up.

    A session can run without those commands, while a container stopped over
    them serves nobody -- and the skills the same layer ships are unaffected,
    so they are still copied.
    """

    WARNING = "Warning: command translation failed for Codex; continuing"

    def test_every_layer_warns_and_the_skills_still_land(self):
        self.stage_skill("shared", "pkg", "shared\n")

        captured = io.StringIO()
        with mock.patch.object(
                codex.commands, "main", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(captured):
                status = self.install("codex")

        self.assertEqual(status, 0)
        self.assertEqual(captured.getvalue().count(self.WARNING), len(LAYERS))
        self.assertEqual(
            read_file(os.path.join(
                self.home, ".agents", "skills", "pkg", "SKILL.md")),
            "shared\n")

    def test_a_nonzero_status_becomes_the_same_warning(self):
        captured = io.StringIO()
        with mock.patch.object(codex.commands, "main", return_value=2):
            with contextlib.redirect_stderr(captured):
                status = self.install("codex")

        self.assertEqual(status, 0)
        self.assertEqual(captured.getvalue().count(self.WARNING), len(LAYERS))

    def test_a_clean_translation_says_nothing(self):
        self.stage_command("user", "review", description="Reviews code.")

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.assertEqual(self.install("codex"), 0)
        self.assertNotIn(self.WARNING, captured.getvalue())


class PhaseOrder(AssetCase):
    """The driver runs the config phase, the translation, then the install.

    Two harnesses resolve their asset destinations out of the merged config
    destination, and the merge rebuilds that directory -- so an install
    reaching it first writes into a directory about to be wiped.
    """

    def test_the_phases_run_in_order(self):
        calls = []

        def record(label, status):
            def phase(*args, **kwargs):
                calls.append(label)
                return status
            return phase

        with mock.patch.object(init, "initialize", record("config", 0)):
            with mock.patch.object(init, "translate_agents", record("agents", 0)):
                with mock.patch.object(init, "install_assets", record("assets", 0)):
                    status = init.run(
                        "claude", self.home, self.env(), self.workspace)

        self.assertEqual(status, 0)
        self.assertEqual(calls, ["config", "agents", "assets"])

    def test_a_failed_config_phase_stops_the_run(self):
        """The config phase failing takes the container down, so the phases
        after it must not run at all."""
        translated = mock.Mock()
        installed = mock.Mock()
        with mock.patch.object(init, "initialize", return_value=2):
            with mock.patch.object(init, "translate_agents", translated):
                with mock.patch.object(init, "install_assets", installed):
                    status = init.run(
                        "claude", self.home, self.env(), self.workspace)

        self.assertEqual(status, 2)
        translated.assert_not_called()
        installed.assert_not_called()


class RecordedInstalls(AssetCase):
    """The whole destination tree a representative mix produces.

    Every other test here reads one entry at a time; these two read the entire
    destination, so an extra file, a missing nested one, or a byte of drifted
    content fails rather than hiding beside what was asserted.
    """

    def test_claude_installs_the_recorded_tree(self):
        self.stage_skill("user", "shared", "user shared skill\n")
        self.stage_skill("user", "user-only", "user only skill\n")
        write_file(
            os.path.join(self.skills_src("user"), "user-only", "ref", "notes.md"),
            "user only notes\n")
        self.stage_command("user", "dup", description="The user copy.")
        self.stage_command("user", "user-cmd", description="A user command.")
        self.stage_skill("shared", "shared", "shared layer skill\n")
        self.stage_command("shared", "dup", description="The shared copy.")
        self.stage_skill("workspace", "shared", "workspace skill\n")
        self.stage_command("workspace", "ws-cmd", description="A workspace command.")

        self.assertEqual(self.install("claude"), 0)
        self.assertEqual(tree(self.dest), {
            "skills": None,
            os.path.join("skills", "shared"): None,
            os.path.join("skills", "shared", "SKILL.md"): "workspace skill\n",
            os.path.join("skills", "user-only"): None,
            os.path.join("skills", "user-only", "SKILL.md"): "user only skill\n",
            os.path.join("skills", "user-only", "ref"): None,
            os.path.join("skills", "user-only", "ref", "notes.md"):
                "user only notes\n",
            "commands": None,
            os.path.join("commands", "dup.md"):
                command_document("The shared copy.", "Do the thing."),
            os.path.join("commands", "user-cmd.md"):
                command_document("A user command.", "Do the thing."),
            os.path.join("commands", "ws-cmd.md"):
                command_document("A workspace command.", "Do the thing."),
        })

    def test_codex_installs_the_recorded_tree(self):
        self.stage_command("user", "review", description="Reviews code.")
        self.stage_skill("user", "review", "user skill review\n")
        self.stage_skill("shared", "helper", "shared helper\n")
        self.stage_command("shared", "plan", description="Plans work.")
        self.stage_command("workspace", "helper", description="Helps out.")
        self.stage_skill("workspace", "extra", "workspace extra\n")

        self.assertEqual(self.install("codex"), 0)
        self.assertEqual(tree(os.path.join(self.home, ".agents")), {
            "skills": None,
            os.path.join("skills", "review"): None,
            os.path.join("skills", "review", "SKILL.md"): "user skill review\n",
            os.path.join("skills", "plan"): None,
            os.path.join("skills", "plan", "SKILL.md"):
                translated_command("plan", "Plans work.", "Do the thing."),
            os.path.join("skills", "helper"): None,
            os.path.join("skills", "helper", "SKILL.md"):
                translated_command("helper", "Helps out.", "Do the thing."),
            os.path.join("skills", "extra"): None,
            os.path.join("skills", "extra", "SKILL.md"): "workspace extra\n",
        })


if __name__ == "__main__":
    unittest.main()
