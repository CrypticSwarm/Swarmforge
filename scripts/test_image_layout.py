#!/usr/bin/env python3
"""Tests for how the harness image is assembled out of the repo.

The images build from the repository root rather than from `anvil/`, so the
Dockerfile can copy the shared `swarmforge` package in beside the
container-side scripts that import it. Both halves of that arrangement are
covered here: the argv the build recipes hand docker, and whether the
translator still runs once it is laid out the way the Dockerfile lays it out.

Run: python3 scripts/test_image_layout.py
"""

import os
import posixpath
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
    """Every single-source `COPY <src> <dst>` in the Dockerfile, as pairs."""
    pairs = {}
    with open(DOCKERFILE) as handle:
        for line in handle:
            words = line.split()
            if len(words) == 3 and words[0] == "COPY":
                pairs[words[1]] = words[2]
    return pairs


class ImportRootAgreement(unittest.TestCase):
    """The Dockerfile's layout and the entrypoint's import root must agree.

    The translator can only find the shared package if the directory holding
    it is the one the entrypoint puts on the path -- and that is two strings
    in two files with nothing tying them together. A mismatch does not fail
    the build; it surfaces as agents quietly not being translated.
    """

    def setUp(self):
        self.copies = dockerfile_copies()
        with open(ENTRYPOINT) as handle:
            self.entrypoint = handle.read()

    def test_package_is_copied_beneath_the_entrypoints_import_root(self):
        package_dst = self.copies["swarmforge/"]
        import_root = posixpath.dirname(package_dst.rstrip("/"))
        self.assertIn("PYTHONPATH=%s " % import_root, self.entrypoint)

    def test_entrypoint_runs_the_translator_where_the_dockerfile_puts_it(self):
        self.assertIn(
            'translator="%s"' % self.copies["anvil/translate_agents.py"],
            self.entrypoint,
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
    """The translator imports the shared package the way the image does.

    The Dockerfile puts the container-side scripts in one directory and the
    package immediately below it, and the entrypoint names that directory as
    the import root. This stages the same shape and translates an agent
    through it, with the checkout deliberately off the path -- so it fails if
    the translator only resolves its import because a developer happened to
    run it from the repo.
    """

    def test_translator_runs_against_the_staged_package(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="swarmforge-layout-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        # Stands in for /usr/local/lib/swarmforge, the image's import root.
        libdir = os.path.join(tmp, "lib")
        os.makedirs(libdir)
        shutil.copy(
            os.path.join(REPO_ROOT, "anvil", "translate_agents.py"), libdir)
        shutil.copytree(
            os.path.join(REPO_ROOT, "swarmforge"),
            os.path.join(libdir, "swarmforge"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        src = os.path.join(tmp, "agents")
        os.makedirs(src)
        with open(os.path.join(src, "reviewer.md"), "w") as handle:
            handle.write(AGENT_MD)
        dest = os.path.join(tmp, "out")

        completed = subprocess.run(
            [sys.executable, os.path.join(libdir, "translate_agents.py"),
             "claude", dest, src],
            # Only the staged import root, and a working directory outside the
            # checkout: nothing here may reach the repo's own swarmforge/.
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": libdir},
            cwd=tmp, capture_output=True, text=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            "translator failed:\n%s" % completed.stderr,
        )
        with open(os.path.join(dest, "reviewer.md")) as handle:
            written = handle.read()
        self.assertIn("name: reviewer", written)
        self.assertIn("model: claude-sonnet-4-6", written)
        self.assertIn("You are the reviewer agent.", written)


if __name__ == "__main__":
    if shutil.which("make") is None:
        sys.stderr.write("make is required for these tests\n")
        sys.exit(1)
    unittest.main()
