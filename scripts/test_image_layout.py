#!/usr/bin/env python3
"""Tests for how the harness image is assembled out of the repo.

The images build from the repository root rather than from `anvil/`, so the
Dockerfile can copy the shared `swarmforge` package in; everything the
entrypoint runs on the python side is a module inside it. Both halves of that
arrangement are covered here: the argv the build recipes hand docker, and
whether the container-side modules still run once they are laid out the way
the Dockerfile lays them out.

Run: python3 scripts/test_image_layout.py
"""

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
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
        the image but is not importable from it.
        """
        return [
            (match.group("root"), (match.group("flags") or "").split(),
             match.group("module"))
            for match in re.finditer(
                r"(?:PYTHONPATH=(?P<root>\S+) )?python3 (?P<flags>(?:-\S+ )*)"
                r"-m (?P<module>\S+)",
                self.entrypoint,
            )
        ]

    def test_no_module_run_lets_the_workspace_onto_the_import_path(self):
        """`python3 -m` puts the working directory first on sys.path.

        The entrypoint's working directory is the workspace and these modules
        run as root, before privileges are dropped -- so without -P a repo
        containing its own swarmforge/ would shadow the image's copy and be
        executed.
        """
        for _, flags, module in self.module_runs():
            self.assertIn(
                "-P", flags, "%s runs with the workspace on sys.path" % module)

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

    def test_the_translator_guard_points_inside_the_copied_package(self):
        """The entrypoint skips translation when the translator is missing.

        The guard is a literal path, so it silently stops matching if the
        module moves within the package -- and a guard that never fires reads
        exactly like an image with no agents to translate.
        """
        match = re.search(r'translator="([^"]+)"', self.entrypoint)
        self.assertIsNotNone(match, "entrypoint has no translator guard")
        guard = match.group(1)
        prefix = self.package_dest() + "/"
        self.assertTrue(
            guard.startswith(prefix),
            "translator guard %s is not under %s" % (guard, prefix),
        )
        self.assertTrue(
            os.path.isfile(os.path.join(REPO_ROOT, "swarmforge",
                                        guard[len(prefix):])),
            "translator guard points at no file in the package",
        )


class BuildRecipeArgv(unittest.TestCase):
    """`make build_*` pairs an explicit Dockerfile with a repo-root context.

    Building from `anvil/` again would leave the package outside the context
    and the translator unable to import it, and the failure would not surface
    until an agent went missing at runtime.
    """

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

    def run_module(self, module, *args):
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            # Only the staged import root, and a working directory outside the
            # checkout: nothing here may reach the repo's own swarmforge/.
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.libdir},
            cwd=self.tmp, capture_output=True, text=True,
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

        completed = self.run_module("swarmforge.config.merge_opencode", dst, src)
        self.assertEqual(
            completed.returncode, 0,
            "merge failed:\n%s" % completed.stderr,
        )
        with open(dst) as handle:
            self.assertEqual(
                json.load(handle),
                {"model": "anthropic/sonnet", "share": "manual"},
            )


if __name__ == "__main__":
    if shutil.which("make") is None:
        sys.stderr.write("make is required for these tests\n")
        sys.exit(1)
    unittest.main()
