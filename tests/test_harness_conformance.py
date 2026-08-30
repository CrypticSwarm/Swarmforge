#!/usr/bin/env python3
"""Conformance tests every registered harness has to pass.

The other harness suites name the harnesses they cover; this one asks the
registry which harnesses exist and holds all of them to the same contract, so
a harness registered without a destination, without a run target, or without
the files the image build dispatches on fails here rather than at container
start.

What is checked is the whole span a harness is driven through: the descriptor
it declares, a container root phase run over staged layers, the MCP fragment
it shapes and how that fragment reaches it, the exec that starts it, and the
`docker run` argv its make target records.

Nothing here may write outside the temporary directory: the harnesses that pin
a destination under /run/swarmforge have that destination, the paths they hand
to the anvil uid, and Claude's settings and wrapper paths redirected for the
duration of each run.

Run: python3 tests/test_harness_conformance.py
"""

import contextlib
import dataclasses
import io
import json
import os
import shutil
import signal
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

# The recorded argv is a sibling module rather than a package member, which a
# discovery run resolves through its start directory and a direct
# `unittest tests.<module>` run does not.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from swarmforge import harness
from swarmforge.harness import claude, execute, init
from swarmforge.harness.spec import (
    HarnessSpec,
    Waiver,
    pre_exec as default_pre_exec,
    provided,
)

from make_argv_fixtures import RUN_ARGV

# The layer variables every run target hands the container, whatever the
# harness: the three config layers, the three harness-neutral asset layers,
# the two portable .agents layers, the destination and reset decision, and the
# repo's own skills and commands.
LAYER_VARS = (
    "SWARMFORGE_CONFIG_USER_DIR",
    "SWARMFORGE_CONFIG_ORG_DIR",
    "SWARMFORGE_CONFIG_REPO_DIR",
    "SWARMFORGE_ASSETS_USER_DIR",
    "SWARMFORGE_ASSETS_ORG_DIR",
    "SWARMFORGE_ASSETS_REPO_DIR",
    "SWARMFORGE_DOTAGENTS_USER_DIR",
    "SWARMFORGE_DOTAGENTS_ORG_DIR",
    "SWARMFORGE_CONFIG_DEST",
    "SWARMFORGE_CONFIG_RESET",
    "SWARMFORGE_SKILLS_DIR",
    "SWARMFORGE_COMMAND_DIR",
)

# Mount targets every run target carries: the checkout, the host's own config
# dir for this harness, and the repo's portable assets.
SHARED_MOUNTS = ("/workspace",)
SHARED_READONLY_MOUNTS = (
    "/tmp/swarmforge-config/user",
    "/home/anvil/.swarmforge/skills",
    "/home/anvil/.swarmforge/command",
)

# The home the container gives the anvil user, and the import root the image
# copies the package into.
ANVIL_HOME = "/home/anvil"
PACKAGE_ROOT = "/usr/local/lib/swarmforge"

# The environment the entrypoint's launch hands the pre-exec driver. The two
# PYTHON* variables belong to that launch and must not reach the harness.
CONTAINER_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/root",
    "TERM": "xterm-256color",
    "PYTHONPATH": PACKAGE_ROOT,
    "PYTHONCOERCECLOCALE": "0",
}

SESSION_ARGS = ["--flag", "arg one"]

AGENT_MD = """---
description: Probes the translation.
---

Probe body.
"""


def skill_document(layer):
    return "---\nname: demo\ndescription: Demo skill.\n---\n\n%s skill\n" % layer


def command_document(layer):
    return "---\ndescription: Demo command.\n---\n\n%s command\n" % layer


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


def specs():
    """Every registered harness name paired with the spec it declares."""
    return [(name, harness.get(name).SPEC) for name in harness.names()]


class DescriptorContract(unittest.TestCase):
    """Every registered harness declares a complete, well-shaped descriptor.

    The spec is what the drivers dispatch on: a field holding something the
    driver cannot use is not a type error at import, it is a container that
    merges into nowhere, copies assets to a relative path, or hands the
    harness a flag it does not take. A field a harness genuinely has no answer
    for holds a Waiver naming the reason, which is a claim a reader can check
    -- so an empty reason is no declaration at all.
    """

    def test_no_declared_field_is_left_empty(self):
        for name, spec in specs():
            for field in dataclasses.fields(HarnessSpec):
                with self.subTest(harness=name, field=field.name):
                    self.assertIsNotNone(
                        getattr(spec, field.name),
                        "%s declares no %s" % (name, field.name))

    def test_every_waiver_records_why_the_field_is_unimplemented(self):
        for name, spec in specs():
            for field in dataclasses.fields(HarnessSpec):
                value = getattr(spec, field.name)
                if not isinstance(value, Waiver):
                    continue
                with self.subTest(harness=name, field=field.name):
                    self.assertTrue(
                        value.reason.strip(),
                        "%s waives %s with no reason" % (name, field.name))

    def test_the_spec_names_the_key_it_is_registered_under(self):
        """The drivers look a harness up by name and then read the spec's own
        name back out for the context they hand its hooks; a spec filed under
        another key drives one harness with another's identity."""
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertEqual(spec.name, name)

    def test_the_binary_is_a_name_the_exec_can_resolve(self):
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.binary, str)
                self.assertTrue(spec.binary, "%s names no binary" % name)

    def test_a_pinned_config_destination_is_an_absolute_path(self):
        """The destination is handed to the driver as-is, from a working
        directory that is the workspace: a relative one would merge the
        layers into the checkout."""
        for name, spec in specs():
            if not provided(spec.config_dest):
                continue
            with self.subTest(harness=name):
                self.assertIsInstance(spec.config_dest, str)
                self.assertTrue(
                    os.path.isabs(spec.config_dest),
                    "%s pins a relative destination: %s"
                    % (name, spec.config_dest))

    def test_the_reset_decision_is_a_boolean(self):
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.config_reset, bool)

    def test_layer_excludes_are_tar_patterns_anchored_to_the_layer_root(self):
        """The excludes are passed to tar, which matches an unanchored
        pattern at any depth: "skills" would drop a skills directory nested
        anywhere in the layer, where "./skills" drops the one at its root."""
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.layer_excludes, tuple)
                for entry in spec.layer_excludes:
                    self.assertIsInstance(entry, str)
                    self.assertTrue(
                        entry.startswith("./"),
                        "%s excludes an unanchored pattern: %s"
                        % (name, entry))

    def test_keyed_files_are_bare_names_beside_the_destination(self):
        """Each keyed file is merged by joining the name onto a layer and
        onto the destination; a name carrying a path would merge from and
        into somewhere else entirely."""
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.keyed_files, tuple)
                for entry in spec.keyed_files:
                    self.assertIsInstance(entry, str)
                    self.assertTrue(entry, "%s keys an empty name" % name)
                    self.assertNotIn(
                        "/", entry,
                        "%s keys a path rather than a name: %s"
                        % (name, entry))

    def test_asset_destinations_resolve_to_absolute_paths(self):
        """Every placeholder a destination uses has to be one the driver
        fills in: an unresolved "{" survives into a directory name, and a
        relative result lands the assets in the workspace."""
        for name, spec in specs():
            for field in ("skills_dest", "commands_dest", "agents_dest"):
                template = getattr(spec, field)
                if not provided(template):
                    continue
                with self.subTest(harness=name, field=field):
                    self.assertIsInstance(template, str)
                    resolved = init.resolve_dest(template, ANVIL_HOME, "/cfg")
                    self.assertNotIn(
                        "{", resolved,
                        "%s.%s leaves a placeholder unfilled: %s"
                        % (name, field, resolved))
                    self.assertTrue(
                        os.path.isabs(resolved),
                        "%s.%s resolves relative: %s"
                        % (name, field, resolved))

    def test_the_mcp_fragment_is_callable(self):
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertTrue(callable(spec.mcp_fragment))

    def test_mcp_delivery_names_a_variable_or_a_flag(self):
        """The two deliveries are the only ones the drivers implement: an env
        var the config driver reads the path out of, or a flag the pre-exec
        hook appends to the argv."""
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.mcp_delivery, tuple)
                self.assertEqual(len(spec.mcp_delivery), 2)
                kind, value = spec.mcp_delivery
                self.assertIn(kind, ("env", "flag"))
                self.assertIsInstance(value, str)
                if kind == "env":
                    self.assertTrue(value, "%s names an empty variable" % name)
                    self.assertTrue(
                        value.isupper() and value.isidentifier(),
                        "%s delivers by a name no shell can export: %s"
                        % (name, value))
                else:
                    self.assertTrue(
                        value.startswith("-"),
                        "%s delivers by a word that is not a flag: %s"
                        % (name, value))

    def test_the_mcp_merge_is_one_the_config_driver_implements(self):
        """The driver dispatches on this string; one it does not know is a
        run whose generated servers are silently never merged."""
        for name, spec in specs():
            if not provided(spec.mcp_merge):
                continue
            with self.subTest(harness=name):
                self.assertIn(
                    spec.mcp_merge, ("json-replace-mcp", "toml-managed-block"))

    def test_the_agent_emitter_is_callable_or_waived(self):
        for name, spec in specs():
            with self.subTest(harness=name):
                if provided(spec.agent_emitter):
                    self.assertTrue(callable(spec.agent_emitter))
                else:
                    self.assertIsInstance(spec.agent_emitter, Waiver)

    def test_extra_chown_paths_are_absolute(self):
        """These are run through chown -Rh as root; a relative one would
        hand the workspace's own subdirectory over instead."""
        for name, spec in specs():
            with self.subTest(harness=name):
                self.assertIsInstance(spec.extra_chown_paths, tuple)
                for path in spec.extra_chown_paths:
                    self.assertIsInstance(path, str)
                    self.assertTrue(
                        os.path.isabs(path),
                        "%s hands over a relative path: %s" % (name, path))

    def test_every_hook_is_callable(self):
        for name, spec in specs():
            for field in ("finalize_agents", "install_assets", "build_config",
                          "finalize_config", "publish_config", "link_state",
                          "root_setup", "pre_exec"):
                with self.subTest(harness=name, hook=field):
                    self.assertTrue(
                        callable(getattr(spec, field)),
                        "%s.%s is not callable" % (name, field))


class ContainerRun(unittest.TestCase):
    """The whole root phase runs for every harness over the same staged tree.

    One layout, one invocation, and the same three questions of each harness:
    the config layers stacked in order of trust, the portable assets stacked
    in order of specificity, and the unified agent definitions delivered where
    that harness reads them. A harness that only works because a suite names
    it fails here as soon as it is registered.

    Every path a run can write to is rooted in the temporary directory: the
    pinned config destination, the pinned agents destination, and the paths
    handed to the anvil uid are all replaced for the run, and so are the two
    settings paths Claude names as module constants.
    """

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="swarmforge-conformance-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.dest = os.path.join(self.tmp, "dest")
        self.agents_dest = os.path.join(self.tmp, "agents-dest")
        self.handover = os.path.join(self.tmp, "handover")
        self.workspace = os.path.join(self.tmp, "workspace")
        self.settings_file = os.path.join(self.tmp, "claude-settings.json")
        self.image_defaults = os.path.join(self.tmp, "image-defaults.json")
        os.makedirs(self.home)
        os.makedirs(self.workspace)

        for layer in ("repo", "user", "org"):
            write_file(
                os.path.join(self.tmp, "config-" + layer, "layered.txt"), layer)
        for layer in ("user", "org"):
            root = os.path.join(self.tmp, "dotagents-" + layer)
            write_file(
                os.path.join(root, "skills", "demo", "SKILL.md"),
                skill_document(layer))
            write_file(
                os.path.join(root, "commands", "democmd.md"),
                command_document(layer))
            os.makedirs(os.path.join(self.tmp, "assets-" + layer), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "assets-repo"), exist_ok=True)
        write_file(
            os.path.join(self.tmp, "shared-skills", "demo", "SKILL.md"),
            skill_document("shared"))
        write_file(
            os.path.join(self.tmp, "shared-commands", "democmd.md"),
            command_document("shared"))
        write_file(
            os.path.join(self.workspace, ".agents", "skills", "demo", "SKILL.md"),
            skill_document("workspace"))
        write_file(
            os.path.join(self.workspace, ".agents", "commands", "democmd.md"),
            command_document("workspace"))
        write_file(
            os.path.join(self.workspace, ".swarmforge", "agents", "probe.md"),
            AGENT_MD)

    def env(self):
        """The environment the entrypoint hands the driver."""
        return {
            "SWARMFORGE_CONFIG_REPO_DIR": os.path.join(self.tmp, "config-repo"),
            "SWARMFORGE_CONFIG_USER_DIR": os.path.join(self.tmp, "config-user"),
            "SWARMFORGE_CONFIG_ORG_DIR": os.path.join(self.tmp, "config-org"),
            "SWARMFORGE_CONFIG_DEST": self.dest,
            "SWARMFORGE_CONFIG_RESET": "0",
            "SWARMFORGE_ASSETS_USER_DIR": os.path.join(self.tmp, "assets-user"),
            "SWARMFORGE_ASSETS_ORG_DIR": os.path.join(self.tmp, "assets-org"),
            "SWARMFORGE_ASSETS_REPO_DIR": os.path.join(self.tmp, "assets-repo"),
            "SWARMFORGE_DOTAGENTS_USER_DIR": os.path.join(
                self.tmp, "dotagents-user"),
            "SWARMFORGE_DOTAGENTS_ORG_DIR": os.path.join(
                self.tmp, "dotagents-org"),
            "SWARMFORGE_SKILLS_DIR": os.path.join(self.tmp, "shared-skills"),
            "SWARMFORGE_COMMAND_DIR": os.path.join(self.tmp, "shared-commands"),
        }

    def inside(self, path):
        return path == self.tmp or path.startswith(self.tmp + os.sep)

    def rooted(self, path):
        """`path` relocated under the temporary directory."""
        return os.path.join(self.handover, path.lstrip("/"))

    @contextlib.contextmanager
    def redirected(self, name):
        """Every path `name` pins, replaced by one under the staging tree."""
        module = harness.get(name)
        self.assertIsNotNone(module, "no harness registered as %s" % name)
        spec = module.SPEC

        replacements = {}
        if provided(spec.config_dest):
            replacements["config_dest"] = self.dest
        # A destination that names a directory outright rather than through a
        # placeholder is the one the config redirection above cannot reach.
        if (provided(spec.agents_dest)
                and "{" not in spec.agents_dest
                and not self.inside(spec.agents_dest)):
            replacements["agents_dest"] = self.agents_dest
        extras = tuple(
            path if self.inside(path) else self.rooted(path)
            for path in spec.extra_chown_paths
        )
        if extras != spec.extra_chown_paths:
            replacements["extra_chown_paths"] = extras

        with contextlib.ExitStack() as stack:
            if replacements:
                stack.enter_context(mock.patch.object(
                    module, "SPEC", dataclasses.replace(spec, **replacements)))
            # Claude names its built settings file and the image's defaults as
            # module constants rather than spec fields, so the generic
            # replacement above cannot reach them; both point into paths a
            # host running Swarmforge itself really has.
            stack.enter_context(
                mock.patch.object(claude, "SETTINGS_FILE", self.settings_file))
            stack.enter_context(mock.patch.object(
                claude, "IMAGE_DEFAULT_SETTINGS", self.image_defaults))
            yield stack

    def run_driver(self, name):
        """Run every root phase for `name` with the pinned paths redirected.

        The uid and gid handed over are the test process's own, so the
        driver's chown calls succeed without changing anything. The harness's
        own dot-directory in the home is created first: the image's home
        volume carries it, and a harness that publishes its config there
        writes into it before anything else creates it.
        """
        os.makedirs(os.path.join(self.home, "." + name), exist_ok=True)
        with self.redirected(name):
            spec = harness.get(name).SPEC
            status = init.run(
                name, self.home, str(os.getuid()), str(os.getgid()),
                self.env(), workspace=self.workspace, cwd=self.workspace)
            self.assertEqual(status, 0, "driver failed for %s" % name)
            return spec

    def resolved(self, spec, template):
        return init.resolve_dest(
            template, self.home, init.config_root(spec, self.home, self.env()))

    def test_the_org_layer_wins_the_config_merge_for_every_harness(self):
        """The layers stack by trust -- repo, then user, then org -- and the
        order is the same whichever harness the run is for. A harness whose
        merge inverted it would run the session under the checkout's own
        permissions, hooks, and env."""
        for name in harness.names():
            with self.subTest(harness=name):
                self.setUp()
                spec = self.run_driver(name)
                merged = os.path.join(
                    init.config_root(spec, self.home, self.env()), "layered.txt")
                self.assertEqual(read_file(merged), "org")

    def test_the_workspace_layer_wins_the_portable_skills_for_every_harness(self):
        """Assets stack the other way from config, by specificity: the
        checkout's own skills are the most specific thing available and win
        over the user, org, and repo layers below them."""
        for name in harness.names():
            if not provided(harness.get(name).SPEC.skills_dest):
                continue
            with self.subTest(harness=name):
                self.setUp()
                spec = self.run_driver(name)
                installed = os.path.join(
                    self.resolved(spec, spec.skills_dest), "demo", "SKILL.md")
                self.assertEqual(
                    read_file(installed), skill_document("workspace"))

    def test_the_workspace_layer_wins_the_portable_commands(self):
        """A harness that waives the destination is passed over: its own
        install hook is what decides where portable commands go, and the
        Waiver says none of them is a commands directory."""
        for name in harness.names():
            if not provided(harness.get(name).SPEC.commands_dest):
                continue
            with self.subTest(harness=name):
                self.setUp()
                spec = self.run_driver(name)
                installed = os.path.join(
                    self.resolved(spec, spec.commands_dest), "democmd.md")
                self.assertEqual(
                    read_file(installed), command_document("workspace"))

    def test_a_unified_agent_reaches_every_harness_that_declares_a_destination(self):
        """One definition serves every harness, and the emitter that writes
        it native is reached through the spec. A destination that is declared
        and never written is a session whose subagents quietly do not
        exist."""
        for name in harness.names():
            if not provided(harness.get(name).SPEC.agents_dest):
                continue
            with self.subTest(harness=name):
                self.setUp()
                spec = self.run_driver(name)
                dest = self.resolved(spec, spec.agents_dest)
                emitted = [
                    entry for entry in os.listdir(dest)
                    if entry.startswith("probe.")
                ]
                self.assertTrue(
                    emitted, "no agent emitted for %s in %s" % (name, dest))

    def test_a_waived_agents_destination_writes_nothing_and_warns_nothing(self):
        """The Waiver is the opt-out on record, not a failure to translate:
        a warning here would train a reader to ignore the one that means a
        harness with a destination got nothing."""
        for name in harness.names():
            spec = harness.get(name).SPEC
            if provided(spec.agents_dest):
                continue
            with self.subTest(harness=name):
                self.setUp()
                captured = io.StringIO()
                with self.redirected(name), contextlib.redirect_stderr(captured):
                    status = init.translate_agents(
                        name, self.home, self.env(), workspace=self.workspace)
                self.assertEqual(status, 0)
                self.assertEqual(captured.getvalue(), "")


class McpContract(unittest.TestCase):
    """Every harness shapes the same server map into its own config fragment.

    The tongs hand each harness the same `{alias: url}` map, and the fragment
    is written to a file the run delivers by the route the spec names. A
    fragment that does not serialize, that loses the alias, or that is
    delivered by a route nothing merges is a session whose sidecars are
    simply absent, with nothing in the log to say so.
    """

    SERVERS = {"gh": "http://tong-gh:3000/mcp"}

    def test_no_servers_yields_no_fragment(self):
        """An empty fragment is what leaves the harness's config untouched;
        a document with an empty container in it rewrites that config to say
        the run has no servers, which is not the same statement."""
        for name, spec in specs():
            if not provided(spec.mcp_fragment):
                continue
            with self.subTest(harness=name):
                self.assertEqual(spec.mcp_fragment({}), {})

    def test_the_fragment_keys_the_server_by_its_alias(self):
        for name, spec in specs():
            if not provided(spec.mcp_fragment):
                continue
            with self.subTest(harness=name):
                fragment = spec.mcp_fragment(dict(self.SERVERS))
                self.assertIsInstance(fragment, dict)
                self.assertEqual(
                    len(fragment), 1,
                    "%s shapes more than one top-level key: %s"
                    % (name, sorted(fragment)))
                servers = next(iter(fragment.values()))
                self.assertIsInstance(servers, dict)
                self.assertEqual(sorted(servers), ["gh"])

    def test_the_fragment_is_written_as_json_carrying_the_url(self):
        """The fragment reaches the container as a JSON file, whatever the
        harness's own config format: a value json cannot encode is a run that
        fails while writing it, and a lost URL is a server nothing reaches."""
        for name, spec in specs():
            if not provided(spec.mcp_fragment):
                continue
            with self.subTest(harness=name):
                serialized = json.dumps(spec.mcp_fragment(dict(self.SERVERS)))
                self.assertIn(self.SERVERS["gh"], serialized)

    def test_a_file_merge_is_delivered_by_an_environment_variable(self):
        """The config driver reads the fragment out of the environment before
        it merges it into a file; a merge announced with a flag delivery names
        a path the driver never sees."""
        for name, spec in specs():
            with self.subTest(harness=name):
                kind, _ = spec.mcp_delivery
                if provided(spec.mcp_merge):
                    self.assertEqual(
                        kind, "env",
                        "%s merges %s from a path nothing reads"
                        % (name, spec.mcp_merge))
                else:
                    self.assertEqual(
                        kind, "flag",
                        "%s neither merges the fragment nor passes it on"
                        % name)


class ExecPassthrough(unittest.TestCase):
    """The pre-exec driver starts every harness the way it was invoked.

    The two PYTHON* variables exist only because the driver is a python
    module, and a harness inheriting them starts with an import root pointing
    at Swarmforge's own package. HOME is the anvil home rather than the root
    the entrypoint ran as, and the session's own arguments keep their order.
    A harness that keeps the default hook has to be exec'd byte-identically to
    a direct exec of its binary.

    Claude's hook reads a wrapper directory and a settings file on a path a
    development host running Swarmforge really has, so both are replaced for
    every run here.
    """

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="swarmforge-conformance-exec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.wrapper = os.path.join(self.tmp, "wrapper")
        self.settings = os.path.join(self.tmp, "claude-settings.json")
        self.recorded = []
        # The driver defaults the interpreter's ignored dispositions before
        # the exec, and the recording execve returns instead of replacing the
        # process -- so this process keeps running with them defaulted, and
        # the first EPIPE write would kill the suite. Put them back after
        # every test.
        for sig in execute.IGNORED_SIGNALS:
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def execve(self, path, argv, env):
        self.recorded.append((path, argv, env))

    def run_driver(self, name, environ=None):
        """Run the pre-exec driver for `name` and return the exec it made."""
        self.recorded = []
        with mock.patch.object(claude, "WRAPPER_DIR", self.wrapper), \
                mock.patch.object(claude, "SETTINGS_FILE", self.settings):
            status = execute.run(
                name, self.home, list(SESSION_ARGS),
                dict(CONTAINER_ENV) if environ is None else environ,
                execv=self.execve)
        self.assertEqual(status, 0)
        self.assertEqual(len(self.recorded), 1)
        return self.recorded[0]

    def test_a_harness_on_the_default_hook_is_exec_d_untouched(self):
        for name, spec in specs():
            if spec.pre_exec is not default_pre_exec:
                continue
            with self.subTest(harness=name):
                path, argv, env = self.run_driver(name)
                binary = execute.BIN_DIR + "/" + spec.binary
                self.assertEqual(path, binary)
                self.assertEqual(argv, [binary] + SESSION_ARGS)

                expected = dict(CONTAINER_ENV)
                for var in execute.LAUNCH_VARS:
                    expected.pop(var, None)
                expected["HOME"] = self.home
                self.assertEqual(env, expected)

    def test_every_harness_execs_the_binary_the_image_installs(self):
        for name, spec in specs():
            with self.subTest(harness=name):
                path, argv, _ = self.run_driver(name)
                binary = execute.BIN_DIR + "/" + spec.binary
                self.assertEqual(path, binary)
                self.assertEqual(argv[0], binary)

    def test_the_session_arguments_keep_their_order_behind_argv_zero(self):
        """A hook may splice its own flags in front of the session's, which
        is what keeps the session's trailing arguments the last word; it may
        not reorder or drop them."""
        for name in harness.names():
            with self.subTest(harness=name):
                _, argv, _ = self.run_driver(name)
                remaining = list(SESSION_ARGS)
                for word in argv[1:]:
                    if remaining and word == remaining[0]:
                        remaining.pop(0)
                self.assertEqual(
                    remaining, [],
                    "%s dropped or reordered the session arguments: %s"
                    % (name, argv))

    def test_no_harness_inherits_the_home_or_the_import_root_of_the_launch(self):
        for name in harness.names():
            with self.subTest(harness=name):
                _, _, env = self.run_driver(name)
                self.assertEqual(env["HOME"], self.home)
                self.assertNotIn("PYTHONPATH", env)


def launcher_argv(argv):
    """The words a run target hands the launcher, before the separator."""
    return argv[:argv.index("--")]


def docker_argv(argv):
    """The `docker run` command a run target hands the launcher."""
    return argv[argv.index("--") + 1:]


def flag_values(argv, flag):
    """Every word following `flag` in `argv`."""
    return [argv[index + 1] for index, word in enumerate(argv[:-1])
            if word == flag]


def volumes(argv):
    """Every `-v` entry as (target, options)."""
    found = []
    for entry in flag_values(argv, "-v"):
        fields = entry.split(":")
        if len(fields) < 2:
            continue
        options = fields[2].split(",") if len(fields) > 2 else []
        found.append((fields[1], options))
    return found


def tmpfs_paths(argv):
    """Every `--tmpfs` target, without its options."""
    return [entry.split(":")[0] for entry in flag_values(argv, "--tmpfs")]


def mount_targets(argv):
    """Every `--mount` target, read out of its comma-separated fields."""
    found = []
    for entry in flag_values(argv, "--mount"):
        for field in entry.split(","):
            key, _, value = field.partition("=")
            if key in ("target", "dst", "destination"):
                found.append(value)
    return found


def env_value(argv, variable):
    """The value a `-e VAR=value` entry carries, or None when it is absent."""
    prefix = variable + "="
    for entry in flag_values(argv, "-e"):
        if entry.startswith(prefix):
            return entry[len(prefix):]
    return None


class RunTargetArgv(unittest.TestCase):
    """Every registered harness has a run target, recorded word for word.

    The recorded argv in tests/make_argv_fixtures.py is the make interface as
    it stands; these read it rather than running make. What they ask is
    generic: the launcher is told which harness it is starting, the container
    is handed the layer variables and mounts the drivers read, and the
    directories the harness writes assets into are masked so a repo's assets
    do not travel to the next session in the persistent home.
    """

    def targets(self):
        return [("run_" + name, name, harness.get(name).SPEC)
                for name in harness.names()]

    def test_every_registered_harness_has_a_run_target_and_the_reverse(self):
        self.assertEqual(
            set(RUN_ARGV), {"run_" + name for name in harness.names()})

    def test_the_launcher_is_told_which_harness_it_is_starting(self):
        for target, name, _ in self.targets():
            with self.subTest(harness=name):
                argv = launcher_argv(RUN_ARGV[target])
                self.assertIn("--harness", argv)
                self.assertEqual(argv[argv.index("--harness") + 1], name)

    def test_every_container_is_handed_the_layer_variables(self):
        """The drivers read the layers out of the environment and treat an
        unset variable as a layer that was not mounted, so a missing one is
        not an error -- it is a layer silently dropped from the merge."""
        for target, name, _ in self.targets():
            argv = docker_argv(RUN_ARGV[target])
            entries = flag_values(argv, "-e")
            for variable in LAYER_VARS:
                with self.subTest(harness=name, variable=variable):
                    self.assertTrue(
                        any(entry.startswith(variable + "=")
                            for entry in entries),
                        "%s starts without %s" % (name, variable))

    def test_every_container_mounts_the_workspace_and_the_shared_assets(self):
        for target, name, _ in self.targets():
            argv = docker_argv(RUN_ARGV[target])
            mounted = dict(volumes(argv))
            for path in SHARED_MOUNTS:
                with self.subTest(harness=name, mount=path):
                    self.assertIn(path, mounted)
            for path in SHARED_READONLY_MOUNTS:
                with self.subTest(harness=name, mount=path):
                    self.assertIn(path, mounted)
                    self.assertIn(
                        "ro", mounted[path],
                        "%s mounts %s writable" % (name, path))

    def test_the_container_is_told_which_binary_to_start(self):
        """The entrypoint reads SWARMFORGE_AGENT_BIN and falls back to
        `opencode` when it is unset, which is why the opencode target records
        no such variable."""
        for target, name, spec in self.targets():
            with self.subTest(harness=name):
                recorded = env_value(docker_argv(RUN_ARGV[target]),
                                     "SWARMFORGE_AGENT_BIN")
                if recorded is None:
                    self.assertEqual(
                        spec.binary, "opencode",
                        "%s starts without naming its binary" % name)
                else:
                    self.assertEqual(recorded, spec.binary)

    def asset_dests(self, name, spec, argv):
        """Where this run's assets land inside the container.

        The pinned destination stands for "{config}" when the harness forces
        one; otherwise the destination the run records, falling back to the
        harness's own default under the home.
        """
        recorded = env_value(argv, "SWARMFORGE_CONFIG_DEST")
        if provided(spec.config_dest):
            config = spec.config_dest
        else:
            config = recorded or (ANVIL_HOME + "/.config/" + name)
        return [
            init.resolve_dest(template, ANVIL_HOME, config)
            for template in (spec.skills_dest, spec.commands_dest,
                             spec.agents_dest)
            if provided(template)
        ]

    def test_asset_destinations_inside_a_writable_mount_are_masked(self):
        """A writable bind mount is a host directory that outlives the
        container, and the home is one every session for this user shares.
        An asset destination that resolves into one needs a mask of its own,
        or this repo's skills, commands, and agents are what the next repo's
        session starts with."""
        for target, name, spec in self.targets():
            argv = docker_argv(RUN_ARGV[target])
            writable = [path for path, options in volumes(argv)
                        if "ro" not in options]
            masked = set(tmpfs_paths(argv)) | {path for path, _ in volumes(argv)}
            for dest in self.asset_dests(name, spec, argv):
                carried = [path for path in writable
                           if dest == path or dest.startswith(path + "/")]
                if not carried:
                    continue
                with self.subTest(harness=name, dest=dest):
                    self.assertIn(
                        dest, masked,
                        "%s writes assets into %s, which %s carries to the "
                        "next run" % (name, dest, carried[0]))

    def test_no_mount_stands_over_the_package_the_image_ships(self):
        """The drivers are imported out of this directory by every phase the
        container runs; a mount over it replaces them with whatever the host
        has, or with nothing."""
        for target, name, _ in self.targets():
            argv = docker_argv(RUN_ARGV[target])
            covered = ([path for path, _ in volumes(argv)]
                       + tmpfs_paths(argv) + mount_targets(argv))
            for path in covered:
                with self.subTest(harness=name, mount=path):
                    self.assertFalse(
                        path == PACKAGE_ROOT
                        or path.startswith(PACKAGE_ROOT + "/"),
                        "%s mounts over the image's package: %s"
                        % (name, path))


class RegistryLayout(unittest.TestCase):
    """The registry and the harness directories are the same list.

    The image build and the make include both dispatch on the directory name,
    while the drivers dispatch on the registry: a harness present in one and
    missing from the other is either a name nothing can be built for, or a
    directory whose install script and make include nothing ever reaches.
    """

    def setUp(self):
        self.package_dir = os.path.dirname(os.path.abspath(harness.__file__))

    def harness_dirs(self):
        return sorted(
            entry for entry in os.listdir(self.package_dir)
            if entry != "__pycache__"
            and os.path.isfile(
                os.path.join(self.package_dir, entry, "__init__.py"))
        )

    def test_the_registry_lists_every_harness_package_and_no_others(self):
        self.assertEqual(harness.names(), self.harness_dirs())

    def test_every_harness_carries_the_files_the_build_dispatches_on(self):
        """install.sh is what the image runs for the chosen harness and
        harness.mk is what the Makefile includes for it; a registered harness
        missing either is one that cannot be built or run."""
        for name in harness.names():
            for filename in ("install.sh", "harness.mk"):
                with self.subTest(harness=name, file=filename):
                    path = os.path.join(self.package_dir, name, filename)
                    self.assertTrue(
                        os.path.isfile(path), "%s has no %s" % (name, filename))


if __name__ == "__main__":
    unittest.main()
