#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.cli. Run: python3 tests/test_anvil_cli.py"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `python3 -m unittest tests.<module>` does not put this directory on the path.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# `anvil` is already these tests' word for the container the launcher wraps.
from swarmforge import anvil as launcher
from swarmforge import tongs

from anvil_fixtures import ANVIL_ARGV


# The subprocess tests below drive the shim the Makefile launches.
LAUNCHER_BIN = os.path.join(REPO_ROOT, "bin", "run-anvil")


class ParseArgsTests(unittest.TestCase):
    def test_splits_layers_and_command_at_separator(self):
        opts, cmd = launcher.parse_args(
            ["--repo-tongs", "/r", "--workspace-tongs", "/w", "--", "docker", "run", "img"]
        )
        self.assertEqual(opts.layer_dirs, [(tongs.REPO, "/r"), (tongs.WORKSPACE, "/w")])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_layers_ordered_canonically_regardless_of_flag_order(self):
        opts, _ = launcher.parse_args(
            ["--workspace-tongs", "/w", "--user-tongs", "/u", "--", "x"]
        )
        self.assertEqual(opts.layer_dirs, [(tongs.USER, "/u"), (tongs.WORKSPACE, "/w")])

    def test_no_layer_flags_is_valid(self):
        opts, cmd = launcher.parse_args(["--", "docker", "run", "img"])
        self.assertEqual(opts.layer_dirs, [])
        self.assertEqual(cmd, ["docker", "run", "img"])

    def test_approval_options_default_to_inert(self):
        opts, _ = launcher.parse_args(["--", "x"])
        self.assertIsNone(opts.workspace)
        self.assertIsNone(opts.approvals)
        self.assertIsNone(opts.providers)
        self.assertIsNone(opts.anvil_image)
        self.assertFalse(opts.no_prompt)

    def test_parses_workspace_approvals_and_no_prompt(self):
        opts, cmd = launcher.parse_args(
            ["--workspace", "/ws", "--approvals", "/a.json",
             "--providers", "/p.yaml", "--anvil-image", "anvil:img",
             "--no-prompt", "--", "x"]
        )
        self.assertEqual(opts.workspace, "/ws")
        self.assertEqual(opts.approvals, "/a.json")
        self.assertEqual(opts.providers, "/p.yaml")
        self.assertEqual(opts.anvil_image, "anvil:img")
        self.assertTrue(opts.no_prompt)
        self.assertEqual(cmd, ["x"])

    def test_parses_harness(self):
        opts, _ = launcher.parse_args(["--harness", "claude", "--", "x"])
        self.assertEqual(opts.harness, "claude")

    def test_harness_defaults_to_none(self):
        opts, _ = launcher.parse_args(["--", "x"])
        self.assertIsNone(opts.harness)

    def test_harness_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--harness"])

    def test_anvil_image_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--anvil-image"])

    def test_workspace_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--workspace"])

    def test_approvals_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--approvals"])

    def test_providers_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--providers"])

    def test_missing_separator_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--repo-tongs", "/r", "docker", "run"])

    def test_empty_command_after_separator_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--repo-tongs", "/r", "--"])

    def test_flag_without_value_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--repo-tongs"])

    def test_unknown_argument_raises(self):
        with self.assertRaises(launcher.UsageError):
            launcher.parse_args(["--bogus", "/r", "--", "x"])

    def test_command_tokens_are_preserved_even_if_they_look_like_flags(self):
        _, cmd = launcher.parse_args(["--", "docker", "run", "--user-tongs", "--"])
        self.assertEqual(cmd, ["docker", "run", "--user-tongs", "--"])


class DiscoverTongsTests(unittest.TestCase):
    def test_no_layers_is_empty(self):
        self.assertEqual(launcher.discover_tongs([]), {})

    def test_absent_layer_dirs_discover_nothing(self):
        layer_dirs = [(tongs.REPO, "/nonexistent/tongs"), (tongs.WORKSPACE, "/also/missing")]
        self.assertEqual(launcher.discover_tongs(layer_dirs), {})

    def test_discovers_a_present_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gh.yaml"), "w") as handle:
                handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
            merged = launcher.discover_tongs([(tongs.WORKSPACE, tmp)])
            self.assertEqual(sorted(merged), ["gh"])


class MainErrorTests(unittest.TestCase):
    def test_bad_args_return_two_without_exec(self):
        # Reaching exec_anvil would replace the test process.
        self.assertEqual(launcher.main(["--repo-tongs", "/r"]), 2)
        self.assertEqual(launcher.main([]), 2)

    def test_unexecutable_anvil_returns_127(self):
        # 127 is the shell's uninvocable status, and a failed exec returns here.
        self.assertEqual(launcher.exec_anvil(["/no/such/binary-xyz"]), 127)


def _run_launcher(extra_args):
    """Invoke the launcher in a child process and capture the execed argv.

    The anvil "command" is a tiny python program that prints the argv it
    receives as JSON. Because the launcher execs it, the JSON we read back is
    exactly the argv the launcher forwarded -- letting us assert the forwarded
    command byte-for-byte through a real os.execvp.
    """
    echo = [sys.executable, "-c", "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"]
    argv = [sys.executable, LAUNCHER_BIN] + extra_args + ["--"] + echo + ANVIL_ARGV
    completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout), completed.stderr


class PassthroughInvariantTests(unittest.TestCase):
    """No tongs discovered => the anvil argv is forwarded byte-identically."""

    def test_no_tongs_forwards_anvil_argv_verbatim(self):
        forwarded, stderr = _run_launcher(["--repo-tongs", "/nonexistent/tongs"])
        self.assertEqual(forwarded, ANVIL_ARGV)
        self.assertNotIn("tong", stderr)

    def test_no_layer_flags_forwards_anvil_argv_verbatim(self):
        forwarded, _ = _run_launcher([])
        self.assertEqual(forwarded, ANVIL_ARGV)

    def test_missing_workspace_tongs_dir_forwards_verbatim(self):
        # The macro always passes the workspace layer, existing dir or not.
        forwarded, stderr = _run_launcher(["--workspace-tongs", "/no/such/.swarmforge/tongs"])
        self.assertEqual(forwarded, ANVIL_ARGV)
        self.assertNotIn("tong", stderr)

    def test_launcher_flags_do_not_leak_into_anvil_argv(self):
        # The Makefile always passes --anvil-image and --providers; with no tongs the table goes unread.
        forwarded, stderr = _run_launcher([
            "--anvil-image", "opencode:local",
            "--providers", "/nonexistent/secret-providers.yaml",
            "--repo-tongs", "/nonexistent/tongs",
        ])
        self.assertEqual(forwarded, ANVIL_ARGV)
        self.assertNotIn("tong", stderr)


def _run_launcher_raw(extra_args, stdin_text=None):
    """Invoke the launcher in a child process without asserting success.

    Like `_run_launcher` but returns the raw CompletedProcess so tests can
    inspect a non-zero exit (e.g. a denied approval that must not exec the
    anvil). `stdin_text` is fed to the launcher's stdin.
    """
    echo = [sys.executable, "-c", "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"]
    argv = [sys.executable, LAUNCHER_BIN] + extra_args + ["--"] + echo + ANVIL_ARGV
    return subprocess.run(argv, input=stdin_text, capture_output=True, text=True)


class DefaultApprovalsPathTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("SWARMFORGE_USER_ASSETS_DIR")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        else:
            os.environ["SWARMFORGE_USER_ASSETS_DIR"] = self.saved

    def test_honors_user_assets_dir(self):
        os.environ["SWARMFORGE_USER_ASSETS_DIR"] = "/opt/sf"
        self.assertEqual(launcher.default_approvals_path(), "/opt/sf/approvals.json")

    def test_falls_back_to_home_swarmforge(self):
        os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        expected = os.path.join(os.path.expanduser("~"), ".swarmforge", "approvals.json")
        self.assertEqual(launcher.default_approvals_path(), expected)


class DefaultProvidersPathTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("SWARMFORGE_USER_ASSETS_DIR")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        else:
            os.environ["SWARMFORGE_USER_ASSETS_DIR"] = self.saved

    def test_honors_user_assets_dir(self):
        os.environ["SWARMFORGE_USER_ASSETS_DIR"] = "/opt/sf"
        self.assertEqual(
            launcher.default_providers_path(), "/opt/sf/secret-providers.yaml"
        )

    def test_falls_back_to_home_swarmforge(self):
        os.environ.pop("SWARMFORGE_USER_ASSETS_DIR", None)
        expected = os.path.join(
            os.path.expanduser("~"), ".swarmforge", "secret-providers.yaml"
        )
        self.assertEqual(launcher.default_providers_path(), expected)


class MainGateTests(unittest.TestCase):
    """main() stops before exec when a workspace tong is unapproved."""

    def _workspace_tongs_dir(self, tmp):
        tongs_dir = os.path.join(tmp, "tongs")
        os.makedirs(tongs_dir)
        with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
            handle.write("lifecycle: session\nimage: x\ninterface:\n  kind: none\n")
        return tongs_dir

    def test_no_prompt_unapproved_returns_one_without_exec(self):
        # The gate raises before exec_anvil, so main returns in-process.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = self._workspace_tongs_dir(tmp)
            rc = launcher.main(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", os.path.join(tmp, "approvals.json"),
                    "--no-prompt",
                    "--", "/no/such/binary-xyz",
                ]
            )
            self.assertEqual(rc, 1)

    def test_no_prompt_unapproved_does_not_forward_anvil(self):
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = self._workspace_tongs_dir(tmp)
            completed = _run_launcher_raw(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", os.path.join(tmp, "approvals.json"),
                    "--no-prompt",
                ]
            )
            self.assertNotEqual(completed.returncode, 0)
            # The echo anvil never ran, so nothing was forwarded.
            self.assertEqual(completed.stdout, "")
            self.assertIn("fails closed", completed.stderr)

    def test_approved_workspace_tong_passes_gate_then_refused_as_unsupported(self):
        # The `volume` interface, not the approval, is what refuses this launch.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "cache.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n"
                    "  kind: volume\n  volume: build-cache\n  mountpoint: /cache\n"
                    "readiness:\n  mode: none\n"
                )
            defn = tongs.load_tong_file(os.path.join(tongs_dir, "cache.yaml"))
            approvals_path = os.path.join(tmp, "approvals.json")
            tongs.save_approvals(
                approvals_path, tongs.record_approval({}, tmp, "cache", defn)
            )
            completed = _run_launcher_raw(
                [
                    "--workspace-tongs", tongs_dir,
                    "--workspace", tmp,
                    "--approvals", approvals_path,
                ]
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran
            self.assertIn("volume", completed.stderr)
            self.assertNotIn("fails closed", completed.stderr)

    def test_invalid_tong_returns_one_without_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "bad.yaml"), "w") as handle:
                handle.write("image: x\n")  # missing lifecycle + interface
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran

    def test_malformed_providers_file_returns_one_without_exec(self):
        # The provider table is loaded before any tong starts, not mid-resolution.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "shipper.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            providers = os.path.join(tmp, "secret-providers.yaml")
            with open(providers, "w") as handle:
                handle.write("providers:\n  op: not-a-list\n")
            completed = _run_launcher_raw(
                ["--repo-tongs", tongs_dir, "--providers", providers]
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")  # anvil never ran
            self.assertIn("op", completed.stderr)

    def test_keyboard_interrupt_during_run_returns_130(self):
        # 130 is the conventional 128+SIGINT status.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "shipper.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n  kind: none\n"
                    "readiness:\n  mode: none\n"
                )
            # Patching the package re-export misses main's binding and hits docker.
            with mock.patch.object(launcher.cli, "run_with_tongs",
                                   side_effect=KeyboardInterrupt):
                rc = launcher.main(["--repo-tongs", tongs_dir, "--", "/no/such/binary-xyz"])
            self.assertEqual(rc, 130)

    def test_colliding_mcp_aliases_refused_without_exec(self):
        # Two tongs sharing an interface.name would make DNS nondeterministic.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            for filename in ("gh.yaml", "gh2.yaml"):
                with open(os.path.join(tongs_dir, filename), "w") as handle:
                    handle.write(
                        "lifecycle: shared\nimage: x\ninterface:\n"
                        "  kind: mcp\n  name: github\n  port: 8080\n"
                        "readiness:\n  mode: none\n"
                    )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("github", completed.stderr)  # the colliding alias

    def test_mcp_tong_without_supported_harness_refused_without_exec(self):
        # MCP tongs need a harness-specific config emitter.
        for harness_args in ([], ["--harness", "opencdoe"]):
            with self.subTest(harness_args=harness_args):
                with tempfile.TemporaryDirectory() as tmp:
                    tongs_dir = os.path.join(tmp, "tongs")
                    os.makedirs(tongs_dir)
                    with open(os.path.join(tongs_dir, "gh.yaml"), "w") as handle:
                        handle.write(
                            "lifecycle: shared\nimage: x\ninterface:\n"
                            "  kind: mcp\n  name: github\n  port: 8080\n"
                            "readiness:\n  mode: none\n"
                        )
                    completed = _run_launcher_raw(
                        harness_args + ["--repo-tongs", tongs_dir]
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(completed.stdout, "")
                    self.assertIn("--harness", completed.stderr)

    def test_volume_tong_refused_without_exec(self):
        # A `volume` interface (a shared named volume) has no consumer yet.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "cache.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\ninterface:\n"
                    "  kind: volume\n  volume: build-cache\n  mountpoint: /cache\n"
                    "readiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("volume", completed.stderr)

    def test_shared_workspace_mount_refused_without_exec(self):
        # A `shared` tong outlives the session, so a workspace mount would leak.
        with tempfile.TemporaryDirectory() as tmp:
            tongs_dir = os.path.join(tmp, "tongs")
            os.makedirs(tongs_dir)
            with open(os.path.join(tongs_dir, "watch.yaml"), "w") as handle:
                handle.write(
                    "lifecycle: shared\nimage: x\nmounts:\n  - workspace:ro\n"
                    "interface:\n  kind: none\nreadiness:\n  mode: none\n"
                )
            completed = _run_launcher_raw(["--repo-tongs", tongs_dir])
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("workspace", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
