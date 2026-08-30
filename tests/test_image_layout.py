#!/usr/bin/env python3
"""Tests for how the harness image is assembled out of the repo.

The images build from the repository root rather than from `anvil/`, so the
Dockerfile can copy the shared `swarmforge` package in; everything the
entrypoint runs on the python side is a module inside it. Both halves of that
arrangement are covered here: the argv the build recipes hand docker, and
whether the container-side modules still run once they are laid out the way
the Dockerfile lays them out.

Run: python3 tests/test_image_layout.py
"""

import dataclasses
import json
import os
import posixpath
import re
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
from swarmforge.harness import claude, init

import make_argv_fixtures

MAKEFILE = os.path.join(REPO_ROOT, "Makefile")
DOCKERFILE = os.path.join(REPO_ROOT, "anvil", "Dockerfile")
ENTRYPOINT = os.path.join(REPO_ROOT, "anvil", "entrypoint.sh")

# Records the argv it was handed instead of building anything.
DOCKER_STUB = """#!/bin/sh
: > "$CAPTURE_FILE"
for arg in "$@"; do printf '%s\\0' "$arg" >> "$CAPTURE_FILE"; done
"""

AGENT_MD = """---
description: Reviews code for defects.
mode: subagent
model: anthropic/claude-sonnet-4-6
---

You are the reviewer agent.
"""


def dockerfile_copies():
    """Every one-source `COPY <src> <dst>` in the Dockerfile, as a src -> dst map.

    Flag words (`--from`, `--chown`) are dropped, so a copy keeps its entry if
    one is added. Multi-source copies would be indistinguishable from a flagged
    one-source copy here, and the Dockerfile uses none.
    """
    pairs = {}
    with open(DOCKERFILE) as handle:
        for line in handle:
            words = [word for word in line.split() if not word.startswith("--")]
            if len(words) == 3 and words[0] == "COPY":
                pairs[words[1]] = words[2]
    return pairs


class ImportRootAgreement(unittest.TestCase):
    """The Dockerfile's layout and the entrypoint's import root must agree.

    Where the package is copied and where the entrypoint tells python to look
    for it are strings in two files with nothing tying them together, and a
    mismatch does not fail the build -- it surfaces as agents quietly not
    being translated, or a config layer quietly not merged.
    """

    def setUp(self):
        self.copies = dockerfile_copies()
        with open(DOCKERFILE) as handle:
            self.dockerfile = handle.read()
        with open(ENTRYPOINT) as handle:
            self.entrypoint = handle.read()

    def copy_dest(self, src):
        self.assertIn(
            src, self.copies, "Dockerfile has no `COPY %s <dest>` line" % src)
        return self.copies[src]

    def package_dest(self):
        return self.copy_dest("swarmforge/").rstrip("/")

    def module_runs(self):
        """Every `python3 ... -m <module>` in the entrypoint, with its context.

        Yields (import root, flags, module). An absent PYTHONPATH gives a root
        of None, which is one of the failures this guards: the module ships in
        the image but is not importable from it. Comments are stripped first,
        so prose about an invocation is not mistaken for one.
        """
        code = "\n".join(
            re.sub(r"(?:^|\s)#.*", "", line)
            for line in self.entrypoint.splitlines()
        )
        return [
            (match.group("root"), (match.group("flags") or "").split(),
             match.group("module"))
            for match in re.finditer(
                r"(?:PYTHONPATH=(?P<root>\S+) )?python3 (?P<flags>(?:-\S+ )*)"
                r"-m (?P<module>\S+)",
                code,
            )
        ]

    def test_no_module_run_lets_the_workspace_onto_the_import_path(self):
        """`python3 -m` puts the working directory first on sys.path.

        The entrypoint's working directory is the workspace: the config
        driver runs there as root, and the pre-exec driver as the anvil user
        whose workspace it is -- so without -P a repo containing its own
        swarmforge/ would shadow the image's copy and be executed.
        """
        for _, flags, module in self.module_runs():
            self.assertIn(
                "-P", flags, "%s runs with the workspace on sys.path" % module)

    def test_the_image_python_understands_the_flags_the_entrypoint_passes(self):
        """-P needs 3.11, and the image builds its own interpreter.

        Lowering the pinned version would make every invocation exit 2 on an
        unknown option: agent translation degrades to a warning and the config
        merge takes the container down with it.
        """
        # Every pin, not just the first: a stage may redeclare the arg with
        # its own default, and the stage that compiles python is not the
        # stage the global default is written in.
        pins = re.findall(r"^ARG PYTHON_VERSION=(\S+)", self.dockerfile, re.M)
        self.assertTrue(pins, "Dockerfile pins no PYTHON_VERSION")
        for pin in pins:
            version = tuple(int(part) for part in pin.split(".")[:2])
            self.assertGreaterEqual(
                version, (3, 11), "python %s does not support -P" % pin)

    def test_entrypoint_runs_only_modules_the_package_actually_ships(self):
        runs = self.module_runs()
        self.assertTrue(runs, "entrypoint runs no python modules")
        for _, _, module in runs:
            self.assertEqual(module.split(".")[0], "swarmforge", module)
            path = os.path.join(REPO_ROOT, *module.split(".")) + ".py"
            self.assertTrue(os.path.isfile(path), "no such module: %s" % module)

    def test_every_module_run_names_the_import_root_the_package_lands_in(self):
        import_root = posixpath.dirname(self.package_dest())
        for root, _, module in self.module_runs():
            self.assertEqual(
                root, import_root,
                "%s runs without the import root on PYTHONPATH" % module,
            )


class PreExecCase(unittest.TestCase):
    """Runs a harness's pre-exec hook over paths staged in a temporary tree.

    Claude's hook reads the settings file it delivers and the wrapper
    directory it may put on PATH, and both are live directories on a
    development host that runs Swarmforge itself; staging them is what keeps a
    test's answer off whatever the last container left behind.
    """

    ARGS = ["--model", "sonnet", "run the thing"]
    PATH = "/usr/local/bin:/usr/bin:/bin"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-image-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.dest = os.path.join(self.tmp, "dest")
        self.wrapper = os.path.join(self.tmp, "wrapper")
        self.settings = os.path.join(self.tmp, "claude-settings.json")

    def staged_spec(self):
        """Claude's spec with its pinned config destination under the tree."""
        return dataclasses.replace(
            harness.get("claude").SPEC, config_dest=self.dest)

    def stage_settings(self):
        """A built settings file standing where the hook looks for one."""
        with open(self.settings, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        return self.settings

    def pre_exec(self, spec, ctx=None):
        """Run `spec`'s hook with claude's two staged paths in place."""
        if ctx is None:
            ctx = init.asset_context(spec, self.home, {})
        argv = ["/usr/local/bin/" + spec.binary] + self.ARGS
        with mock.patch.object(claude, "SETTINGS_FILE", self.settings), \
                mock.patch.object(claude, "WRAPPER_DIR", self.wrapper):
            return spec.pre_exec(ctx, argv, {"PATH": self.PATH})


class ClaudeSettingsDelivery(PreExecCase):
    """The built settings reach claude as arguments, not as a file in the home.

    The build writes the file and the exec names it on the command line, and
    one module constant is all that ties them together; `user` stays in the
    sources because that scope carries asset discovery.
    """

    def test_the_built_file_lives_outside_every_host_mount(self):
        """/home/anvil is the persistent home every container for this
        user shares, and /workspace is the checkout; a build landing in
        either is the leak the command-line delivery exists to end."""
        path = claude.SETTINGS_FILE
        for mounted in ("/home/", "/workspace"):
            self.assertFalse(
                path.startswith(mounted),
                "settings build lands in a host mount: %s" % path)

    def test_the_exec_hands_claude_the_file_the_build_writes(self):
        """The build and the exec read one constant, so running the build and
        then reading the argv follows the whole chain rather than comparing
        two strings that happen to match."""
        spec = harness.get("claude").SPEC
        org = os.path.join(self.tmp, "layer-org")
        os.makedirs(org)
        with open(os.path.join(org, "settings.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"model": "org/m"}')
        ctx = init.asset_context(
            spec, self.home, {"SWARMFORGE_CONFIG_ORG_DIR": org})

        with mock.patch.object(claude, "SETTINGS_FILE", self.settings):
            claude.finalize_config(ctx)
        self.assertTrue(os.path.isfile(self.settings))

        argv, _ = self.pre_exec(spec, ctx=ctx)

        self.assertIn("--settings", argv)
        self.assertEqual(argv[argv.index("--settings") + 1], self.settings)

    def test_the_setting_sources_name_every_scope(self):
        """`user` carries claude's skills, commands, and agents discovery;
        project and local carry the workspace's own .claude settings."""
        self.stage_settings()

        argv, _ = self.pre_exec(harness.get("claude").SPEC)

        self.assertIn("--setting-sources", argv)
        self.assertEqual(
            argv[argv.index("--setting-sources") + 1].split(","),
            ["user", "project", "local"])

    def test_only_claude_is_handed_the_flags(self):
        """The same driver execs every harness, and the flags are claude's."""
        self.stage_settings()
        for name in harness.names():
            if name == "claude":
                continue
            with self.subTest(harness=name):
                spec = harness.get(name).SPEC
                expected = ["/usr/local/bin/" + spec.binary] + self.ARGS

                argv, _ = self.pre_exec(spec)

                self.assertEqual(argv, expected)


class ClaudeConfigHome(PreExecCase):
    """Claude's config dir dies with the container; its credentials do not.

    The destination the driver merges into is the one the exec names to
    claude, and it stays out of every host mount, or the session reads its
    configuration and assets from somewhere else. The credential store is
    named in the persistent home instead, since a rename-based write replaces
    a link.
    """

    def test_the_config_home_is_outside_every_host_mount(self):
        """/home/anvil is the shared persistent home and /workspace is the
        checkout; a config dir in either outlives the container."""
        path = harness.get("claude").SPEC.config_dest
        for mounted in ("/home/", "/workspace"):
            self.assertFalse(
                path.startswith(mounted),
                "claude config dir lands in a host mount: %s" % path)

    def test_every_asset_destination_resolves_to_the_config_home(self):
        """The destinations and the config dir are one guarantee: assets in
        the shared home would be read from nowhere and kept forever.

        Skills, commands, and translated agents all resolve "{config}" to
        claude's pinned destination, which is the config dir this class
        covers.
        """
        spec = harness.get("claude").SPEC
        self.assertEqual(spec.skills_dest, "{config}/skills")
        self.assertEqual(spec.commands_dest, "{config}/commands")
        self.assertEqual(spec.agents_dest, "{config}/agents")
        self.assertEqual(init.config_root(spec, self.home, {}), spec.config_dest)

    def test_claude_is_told_where_its_config_lives(self):
        _, env = self.pre_exec(self.staged_spec())

        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.dest)

    def test_the_credential_store_is_named_not_linked(self):
        """A rename-based write replaces a link, so the store cannot be one."""
        _, env = self.pre_exec(self.staged_spec())

        store = env["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
        self.assertEqual(store, os.path.join(self.home, ".claude"))
        self.assertFalse(os.path.islink(store))
        self.assertFalse(os.path.exists(store))

    def test_the_credential_store_outlives_the_container(self):
        """The home the hook is handed is the persistent mount every
        container for this user shares, and it is also the directory the
        state links point into: the store the exec names and the state the
        links serve have to stay one directory."""
        _, env = self.pre_exec(self.staged_spec())

        self.assertEqual(
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"],
            os.path.join(self.home, ".claude"))


class StatusLineAgreement(unittest.TestCase):
    """The status line the Claude image ships must be the one it turns on.

    Three strings have to line up: where the claude harness's image.sh
    installs the script, where it installs the defaults naming it, and where
    the settings build reads those defaults from. A mismatch is silent -- a
    defaults file that is not there is just a layer contributing nothing.
    """

    IMAGE_SH = os.path.join(
        REPO_ROOT, "swarmforge", "harness", "claude", "image.sh")

    def setUp(self):
        with open(self.IMAGE_SH) as handle:
            self.image_sh = handle.read()

    def install_dest(self, src):
        """Where image.sh installs `${harness_dir}/<src>`."""
        match = re.search(
            r'install [^\n]*"\$\{harness_dir\}/%s" (\S+)' % re.escape(src),
            self.image_sh,
        )
        self.assertIsNotNone(
            match, "image.sh installs no %s" % src)
        return match.group(1)

    def test_image_defaults_name_the_status_line_image_sh_installs(self):
        with open(os.path.join(
                REPO_ROOT, "swarmforge", "harness", "claude",
                "claude-settings.json")) as handle:
            defaults = json.load(handle)
        self.assertEqual(
            defaults.get("statusLine"),
            {"type": "command", "command": self.install_dest("statusline.sh")},
        )

    def test_the_settings_build_reads_the_defaults_where_image_sh_installs_them(self):
        self.assertEqual(
            claude.IMAGE_DEFAULT_SETTINGS,
            self.install_dest("claude-settings.json"),
        )

    def test_image_sh_reads_its_assets_from_the_copy_destination(self):
        """image.sh names the package directory as a literal.

        The files it installs arrive with the package COPY, so the literal
        and that destination are one string in two files; a mismatch fails
        the build of the one image that ships a status line.
        """
        match = re.search(r'harness_dir="([^"]+)"', self.image_sh)
        self.assertIsNotNone(match, "image.sh assembles no harness_dir path")
        copies = dockerfile_copies()
        self.assertIn(
            "swarmforge/", copies,
            "Dockerfile has no `COPY swarmforge/ <dest>` line")
        package_dest = copies["swarmforge/"].rstrip("/") + "/"
        self.assertEqual(match.group(1), package_dest + "harness/claude")


class HarnessInstallLayout(unittest.TestCase):
    """The build finds each harness's scripts where the package lands.

    The image installs the agent binary, and whatever assets the harness
    ships, by running scripts out of the copied package -- so two unrelated
    strings have to agree: the COPY destination and the path each RUN
    assembles. They are in the same file but nothing ties them together, and
    a mismatch is an image with no harness binary.
    """

    HARNESS_DIR = os.path.join(REPO_ROOT, "swarmforge", "harness")

    def setUp(self):
        with open(DOCKERFILE) as handle:
            self.dockerfile = handle.read()

    def test_the_install_run_reads_from_the_copy_destination(self):
        match = re.search(r'install_sh="([^"]+)"', self.dockerfile)
        self.assertIsNotNone(match, "Dockerfile assembles no install_sh path")
        copies = dockerfile_copies()
        self.assertIn(
            "swarmforge/", copies, "Dockerfile has no `COPY swarmforge/ <dest>` line")
        package_dest = copies["swarmforge/"].rstrip("/") + "/"
        self.assertEqual(
            match.group(1), package_dest + "harness/${AGENT}/install.sh")

    def test_the_asset_run_reads_from_the_copy_destination(self):
        """A harness's optional image.sh is found the same way.

        The stage that runs it skips a harness with no such file, so a path
        that has drifted from the COPY destination is not a build failure --
        it is an image quietly missing the assets the harness ships.
        """
        match = re.search(r'image_sh="([^"]+)"', self.dockerfile)
        self.assertIsNotNone(match, "Dockerfile assembles no image_sh path")
        copies = dockerfile_copies()
        self.assertIn(
            "swarmforge/", copies, "Dockerfile has no `COPY swarmforge/ <dest>` line")
        package_dest = copies["swarmforge/"].rstrip("/") + "/"
        self.assertEqual(
            match.group(1), package_dest + "harness/${AGENT}/image.sh")

    def test_every_buildable_harness_ships_an_install_script(self):
        """A harness.mk is what generates the harness's `build_<name>` target.

        The Makefile globs the fragments, so adding one advertises a build
        that only discovers the missing script partway through the image.
        """
        fragments = sorted(
            name for name in os.listdir(self.HARNESS_DIR)
            if os.path.isfile(os.path.join(self.HARNESS_DIR, name, "harness.mk"))
        )
        self.assertTrue(fragments, "no harness declares a harness.mk")
        for name in fragments:
            self.assertTrue(
                os.path.isfile(
                    os.path.join(self.HARNESS_DIR, name, "install.sh")),
                "harness %s has a build target but no install.sh" % name,
            )

    def test_the_install_run_executes_the_script_and_fails_without_one(self):
        """Path agreement alone leaves the RUN free to do nothing.

        The dispatch is an assignment, a guard, and an execution; dropping
        the execution or the guard's exit keeps every path assertion green
        while producing an image with no harness binary.
        """
        self.assertIn('sh "${install_sh}"', self.dockerfile)
        guard = self.dockerfile[
            self.dockerfile.index('install_sh="'):
            self.dockerfile.index('sh "${install_sh}"')
        ]
        self.assertIn('[ ! -f "${install_sh}" ]', guard)
        self.assertIn("exit 1", guard)

    def test_the_asset_run_executes_the_script_when_present(self):
        """The one line where finding image.sh becomes running it.

        A guard that locates the script and does nothing leaves the claude
        image without its status line, and no build fails over it.
        """
        self.assertIn(
            'if [ -f "${image_sh}" ]; then sh "${image_sh}"; fi',
            self.dockerfile,
        )

    def test_harness_scripts_fail_their_run_on_the_first_error(self):
        """`sh <script>` starts a fresh shell, so the RUN's own errexit does
        not reach the script body. A script without its own set -e reports
        success after a failed install -- a binary-less image that only
        surfaces when a container cannot exec its harness.
        """
        scripts = sorted(
            os.path.join(self.HARNESS_DIR, name, script)
            for name in os.listdir(self.HARNESS_DIR)
            for script in ("install.sh", "image.sh")
            if os.path.isfile(os.path.join(self.HARNESS_DIR, name, script))
        )
        self.assertTrue(scripts, "no harness ships a build script")
        for path in scripts:
            with open(path) as handle:
                text = handle.read()
            self.assertRegex(
                text, r"(?m)^set -\w*e",
                "%s does not set errexit" % os.path.relpath(path, REPO_ROOT),
            )

    def test_the_build_recipes_target_a_stage_the_dockerfile_declares(self):
        """The --target word only resolves against a stage at build time.

        Every recipe test runs against a stubbed docker, so a stage renamed
        in the Dockerfile alone keeps the recorded argv green while failing
        all four builds.
        """
        stages = set(re.findall(r"(?m)^FROM \S+ AS (\S+)", self.dockerfile))
        targets = {
            argv[argv.index("--target") + 1]
            for argv in make_argv_fixtures.BUILD_ARGV.values()
        }
        self.assertTrue(targets, "no recorded build argv names a --target")
        self.assertLessEqual(
            targets, stages,
            "build recipes target stages the Dockerfile does not declare",
        )


class BuildRecipeCase(unittest.TestCase):
    """Runs a build_* target and exposes the docker argv it assembled."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-build-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as handle:
            handle.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        self.capture_path = os.path.join(self.tmp, "argv")

    def build_argv(self, target):
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "CAPTURE_FILE": self.capture_path,
        }
        completed = subprocess.run(
            ["make", "-C", REPO_ROOT, "-f", MAKEFILE, target],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            "make failed:\n%s\n%s" % (completed.stdout, completed.stderr),
        )
        with open(self.capture_path) as handle:
            return handle.read().split("\0")[:-1]


class BuildRecipeArgv(BuildRecipeCase):
    """`make build_*` pairs an explicit Dockerfile with a repo-root context.

    Building from `anvil/` again would leave the package outside the context
    and the translator unable to import it, and the failure would not surface
    until an agent went missing at runtime.
    """

    def assert_builds_from_repo_root(self, target):
        argv = self.build_argv(target)
        self.assertEqual(argv[0], "build")
        self.assertEqual(argv[-1], REPO_ROOT, "build context is not the repo root")
        self.assertIn("-f", argv, "no explicit Dockerfile for a root context")
        self.assertEqual(
            argv[argv.index("-f") + 1],
            os.path.join(REPO_ROOT, "anvil", "Dockerfile"),
        )

    def test_opencode_image_builds_from_repo_root(self):
        self.assert_builds_from_repo_root("build_opencode")

    def test_claude_image_builds_from_repo_root(self):
        self.assert_builds_from_repo_root("build_claude")

    def test_grok_image_builds_from_repo_root(self):
        self.assert_builds_from_repo_root("build_grok")

    def test_codex_image_builds_from_repo_root(self):
        self.assert_builds_from_repo_root("build_codex")


class BuildArgvBaseline(BuildRecipeCase):
    """Every word of every build_* recipe's docker argv, against a recording.

    The shape assertions above explain the Dockerfile/context pairing; this
    pins the rest -- target stage, build args and their defaults, image tag --
    so a drifted recipe fails against `make_argv_fixtures.BUILD_ARGV` instead
    of building something subtly different.
    """

    maxDiff = None

    def assert_argv_matches_recording(self, target):
        argv = self.build_argv(target)
        self.assertEqual(
            make_argv_fixtures.normalize(argv, self.tmp),
            make_argv_fixtures.BUILD_ARGV[target],
        )

    def test_build_opencode_argv_matches_recording(self):
        self.assert_argv_matches_recording("build_opencode")

    def test_build_claude_argv_matches_recording(self):
        self.assert_argv_matches_recording("build_claude")

    def test_build_grok_argv_matches_recording(self):
        self.assert_argv_matches_recording("build_grok")

    def test_build_codex_argv_matches_recording(self):
        self.assert_argv_matches_recording("build_codex")


class ContainerImportLayout(unittest.TestCase):
    """The container-side modules run the way the image runs them.

    The Dockerfile copies the package under an import root and the entrypoint
    invokes modules out of it with `python3 -m`. This stages the same shape,
    with the checkout deliberately off the path and off the working directory
    -- so it fails if a module only resolves its imports because a developer
    happened to run it from the repo.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-layout-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Stands in for /usr/local/lib/swarmforge, the image's import root.
        self.libdir = os.path.join(self.tmp, "lib")
        os.makedirs(self.libdir)
        shutil.copytree(
            os.path.join(REPO_ROOT, "swarmforge"),
            os.path.join(self.libdir, "swarmforge"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )

    def run_module(self, module, *args, env=None):
        # -P mirrors the entrypoint, which uses it to keep the workspace off
        # sys.path; here it also stops the staging dir from becoming a second
        # way for the import to resolve. The image's python always has it; the
        # host running these tests may predate it, and the staging dir holds
        # no swarmforge/ for the working directory to resolve through anyway.
        harden = ["-P"] if sys.version_info >= (3, 11) else []
        # Only the staged import root, and a working directory outside the
        # checkout: nothing here may reach the repo's own swarmforge/. A
        # module the entrypoint hands more of the container's environment gets
        # it on top of that.
        environ = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.libdir}
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, *harden, "-m", module, *args],
            env=environ, cwd=self.tmp, capture_output=True, text=True,
        )

    def test_translator_runs_against_the_staged_package(self):
        src = os.path.join(self.tmp, "agents")
        os.makedirs(src)
        with open(os.path.join(src, "reviewer.md"), "w") as handle:
            handle.write(AGENT_MD)
        dest = os.path.join(self.tmp, "out")

        completed = self.run_module(
            "swarmforge.agents.translate", "claude", dest, src)
        self.assertEqual(
            completed.returncode, 0,
            "translator failed:\n%s" % completed.stderr,
        )
        with open(os.path.join(dest, "reviewer.md")) as handle:
            written = handle.read()
        self.assertIn("name: reviewer", written)
        self.assertIn("model: claude-sonnet-4-6", written)
        self.assertIn("You are the reviewer agent.", written)

    def test_config_merge_runs_against_the_staged_package(self):
        dst = os.path.join(self.tmp, "opencode.json")
        src = os.path.join(self.tmp, "layer.json")
        with open(dst, "w") as handle:
            handle.write('{"model": "anthropic/sonnet", "share": "disabled"}')
        with open(src, "w") as handle:
            handle.write('{"share": "manual"}')

        completed = self.run_module("swarmforge.config.merge_json", dst, src)
        self.assertEqual(
            completed.returncode, 0,
            "merge failed:\n%s" % completed.stderr,
        )
        with open(dst) as handle:
            self.assertEqual(
                json.load(handle),
                {"model": "anthropic/sonnet", "share": "manual"},
            )

    def test_config_merge_accepts_the_flag_the_entrypoint_passes(self):
        """The tong MCP layer merges with --replace-mcp-entries.

        That call has no `|| continue` behind it, so an argv the module
        rejects is not a degraded merge -- it is a container that never
        starts.
        """
        dst = os.path.join(self.tmp, "opencode.json")
        src = os.path.join(self.tmp, "tong-mcp.json")
        with open(dst, "w") as handle:
            handle.write('{"mcp": {"gh": {"type": "local", "enabled": true}}}')
        with open(src, "w") as handle:
            handle.write('{"mcp": {"gh": {"type": "remote", "url": "http://gh"}}}')

        completed = self.run_module(
            "swarmforge.config.merge_json", dst, src,
            "--replace-mcp-entries")
        self.assertEqual(
            completed.returncode, 0,
            "merge failed:\n%s" % completed.stderr,
        )
        # Whole-entry replacement: the local server's keys must not survive.
        with open(dst) as handle:
            self.assertEqual(
                json.load(handle),
                {"mcp": {"gh": {"type": "remote", "url": "http://gh"}}},
            )

    def test_settings_build_runs_the_way_the_entrypoint_calls_it(self):
        """The Claude settings build, argv and all, against the staged copy.

        The entrypoint passes a path for every layer whether or not it
        exists. A run that rejected that argv would leave the container on
        the empty settings.json the host mounted in.
        """
        dst = os.path.join(self.tmp, "settings.json")
        with open(dst, "w") as handle:
            handle.write('{"stale": true}')
        defaults = os.path.join(self.tmp, "image-defaults.json")
        with open(defaults, "w") as handle:
            handle.write('{"statusLine": {"type": "command", "command": "/sl"}}')
        user = os.path.join(self.tmp, "user.json")
        with open(user, "w") as handle:
            handle.write('{"model": "sonnet"}')
        absent = os.path.join(self.tmp, "no-such-layer", "settings.json")

        completed = self.run_module(
            "swarmforge.config.merge_json", "--build", dst,
            defaults, absent, user, absent)
        self.assertEqual(
            completed.returncode, 0,
            "settings build failed:\n%s" % completed.stderr,
        )
        with open(dst) as handle:
            self.assertEqual(
                json.load(handle),
                {
                    "statusLine": {"type": "command", "command": "/sl"},
                    "model": "sonnet",
                },
            )

    def test_root_phase_driver_runs_against_the_staged_package(self):
        """The root-phase driver, run the way the entrypoint runs it.

        It is the first python the container executes and it reaches across
        the whole package -- the registry, the harness modules, the agent
        translator, and the config merges -- so it is the invocation that
        proves every import resolves from the staged import root rather than
        from a checkout that happens to be the working directory.

        The uid and gid it hands over to are this process's own, and the chown
        it resolves from PATH is a stub that only records that it ran: the
        workspace path is a fixed string, so the real binary would reach
        whatever the machine running the tests has standing there.
        """
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        dest = os.path.join(self.tmp, "dest")
        org = os.path.join(self.tmp, "layer-org")
        os.makedirs(org)
        with open(os.path.join(org, "marker.txt"), "w") as handle:
            handle.write("org")

        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "chown")
        with open(stub, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)

        completed = self.run_module(
            "swarmforge.harness.init", "grok", home,
            str(os.getuid()), str(os.getgid()),
            env={
                "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                "SWARMFORGE_CONFIG_ORG_DIR": org,
                "SWARMFORGE_CONFIG_DEST": dest,
                "SWARMFORGE_CONFIG_RESET": "0",
            },
        )
        self.assertEqual(
            completed.returncode, 0,
            "root-phase driver failed:\n%s" % completed.stderr,
        )
        with open(os.path.join(dest, "marker.txt")) as handle:
            self.assertEqual(handle.read(), "org")


if __name__ == "__main__":
    if shutil.which("make") is None:
        sys.stderr.write("make is required for these tests\n")
        sys.exit(1)
    unittest.main()
