#!/usr/bin/env python3
"""Unit tests for scripts/run_anvil.py. Run: python3 scripts/test_run_anvil.py"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "run_anvil.py")
spec = importlib.util.spec_from_file_location("run_anvil", MODULE_PATH)
run_anvil = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_anvil)
tongs = run_anvil.tongs


# A docker invocation shaped like the one run_agent_container builds: the
# interactive/remove flags, name, network, injected env/mounts, image, and the
# harness args. The launcher must forward this verbatim when no tongs exist.
ANVIL_ARGV = [
    "docker", "run", "-it", "--rm", "--name", "claude-myproject",
    "--network", "opencode-net",
    "-e", "OPENCODE_UID=1000",
    "-e", "TZ=Etc/UTC",
    "-v", "/home/me/proj:/workspace",
    "-v", "/home/me/proj:/repos/me/proj",
    "claude-code:local",
    "--some-harness-arg",
]


class ParseArgsTests(unittest.TestCase):
    def test_splits_layers_and_command_at_separator(self):
        layer_dirs, cmd = run_anvil.parse_args(
            ["--repo-tongs", "/r", "--workspace-tongs", "/w", "--", "docker", "run", "img"]
        )
        self.assertEqual(layer_dirs, [(tongs.REPO, "/r"), (tongs.WORKSPACE, "/w")])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_layers_ordered_canonically_regardless_of_flag_order(self):
        layer_dirs, _ = run_anvil.parse_args(
            ["--workspace-tongs", "/w", "--user-tongs", "/u", "--", "x"]
        )
        # USER precedes WORKSPACE in canonical precedence even though the
        # workspace flag came first.
        self.assertEqual(layer_dirs, [(tongs.USER, "/u"), (tongs.WORKSPACE, "/w")])

    def test_no_layer_flags_is_valid(self):
        layer_dirs, cmd = run_anvil.parse_args(["--", "docker", "run", "img"])
        self.assertEqual(layer_dirs, [])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_missing_separator_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs", "/r", "docker", "run"])

    def test_empty_command_after_separator_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs", "/r", "--"])

    def test_flag_without_value_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--repo-tongs"])

    def test_unknown_argument_raises(self):
        with self.assertRaises(run_anvil.UsageError):
            run_anvil.parse_args(["--bogus", "/r", "--", "x"])

    def test_command_tokens_are_preserved_even_if_they_look_like_flags(self):
        # Everything after '--' is the command; a later '--' or a tong-looking
        # flag inside it is data, not parsed.
        _, cmd = run_anvil.parse_args(["--", "docker", "run", "--user-tongs", "--"])
        self.assertEqual(cmd, ["docker", "run", "--user-tongs", "--"])


class DiscoverTongsTests(unittest.TestCase):
    def test_no_layers_is_empty(self):
        self.assertEqual(run_anvil.discover_tongs([]), {})

    def test_missing_dirs_are_empty(self):
        # The inert-when-empty basis: absent layer dirs discover nothing.
        layer_dirs = [(tongs.REPO, "/nonexistent/tongs"), (tongs.WORKSPACE, "/also/missing")]
        self.assertEqual(run_anvil.discover_tongs(layer_dirs), {})

    def test_discovers_a_present_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gh.yaml"), "w") as handle:
                handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
            merged = run_anvil.discover_tongs([(tongs.WORKSPACE, tmp)])
            self.assertEqual(sorted(merged), ["gh"])


class MainErrorTests(unittest.TestCase):
    def test_bad_args_return_two_without_exec(self):
        # main() reports usage and returns 2 for malformed argv; it must not
        # reach exec_anvil (which would replace the test process).
        self.assertEqual(run_anvil.main(["--repo-tongs", "/r"]), 2)
        self.assertEqual(run_anvil.main([]), 2)


def _run_launcher(extra_args):
    """Invoke run_anvil.py in a child process and capture the execed argv.

    The anvil "command" is a tiny python program that prints the argv it
    receives as JSON. Because the launcher execs it, the JSON we read back is
    exactly the argv the launcher forwarded -- letting us assert the forwarded
    command byte-for-byte through a real os.execvp.
    """
    echo = [sys.executable, "-c", "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"]
    argv = [sys.executable, MODULE_PATH] + extra_args + ["--"] + echo + ANVIL_ARGV
    completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout), completed.stderr


class PassthroughInvariantTests(unittest.TestCase):
    """No tongs discovered => the anvil argv is forwarded byte-identically."""

    def test_no_tongs_forwards_anvil_argv_verbatim(self):
        forwarded, stderr = _run_launcher(["--repo-tongs", "/nonexistent/tongs"])
        self.assertEqual(forwarded, ANVIL_ARGV)
        # Nothing about tongs is reported when none are discovered.
        self.assertNotIn("tong", stderr)

    def test_no_layer_flags_forwards_anvil_argv_verbatim(self):
        forwarded, _ = _run_launcher([])
        self.assertEqual(forwarded, ANVIL_ARGV)

    def test_present_tong_still_forwards_anvil_argv_unchanged(self):
        # Even with a definition discovered, the launcher does not rewrite the
        # anvil command; it warns and runs the anvil as given.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gh.yaml"), "w") as handle:
                handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
            forwarded, stderr = _run_launcher(["--workspace-tongs", tmp])
            self.assertEqual(forwarded, ANVIL_ARGV)
            self.assertIn("gh", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
