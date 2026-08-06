#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.docker. Run: python3 tests/test_anvil_docker.py"""

import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Aliased because `anvil` is already these tests' word for the container
# the launcher wraps.
from swarmforge import anvil as launcher


class _RecordingRun:
    """A subprocess.run stand-in that records argvs and returns canned codes.

    `codes` maps the first three argv tokens to a return code (default 0), so a
    test can make one docker subcommand "fail" while the rest succeed.
    """

    def __init__(self, codes=None):
        self.argvs = []
        self._codes = codes or {}

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, self._codes.get(tuple(argv[:3]), 0))


class DockerCLITests(unittest.TestCase):
    """The network seam used by the session-network launch path."""

    def test_ensure_network_creates_when_absent(self):
        rec = _RecordingRun({("docker", "network", "inspect"): 1})
        launcher.DockerCLI(run=rec).ensure_network("sess-net")
        self.assertEqual(rec.argvs[0][:4], ["docker", "network", "inspect", "sess-net"])
        self.assertIn(["docker", "network", "create", "sess-net"], rec.argvs)

    def test_ensure_network_reuses_existing(self):
        rec = _RecordingRun()  # inspect returns 0 => already present
        launcher.DockerCLI(run=rec).ensure_network("sess-net")
        self.assertNotIn(["docker", "network", "create", "sess-net"], rec.argvs)

    def test_ensure_network_raises_when_create_fails(self):
        rec = _RecordingRun(
            {("docker", "network", "inspect"): 1, ("docker", "network", "create"): 1}
        )
        with self.assertRaises(launcher.DockerError):
            launcher.DockerCLI(run=rec).ensure_network("sess-net")

    def test_network_connect_passes_alias(self):
        rec = _RecordingRun()
        launcher.DockerCLI(run=rec).network_connect("net", "ctr", aliases=["gh"])
        self.assertEqual(
            rec.argvs[-1], ["docker", "network", "connect", "--alias", "gh", "net", "ctr"]
        )

    def test_network_connect_passes_every_alias(self):
        rec = _RecordingRun()
        launcher.DockerCLI(run=rec).network_connect("net", "ctr", aliases=["gh", "api"])
        self.assertEqual(
            rec.argvs[-1],
            ["docker", "network", "connect", "--alias", "gh", "--alias", "api",
             "net", "ctr"],
        )

    def test_network_connect_without_alias(self):
        rec = _RecordingRun()
        launcher.DockerCLI(run=rec).network_connect("net", "ctr")
        self.assertEqual(rec.argvs[-1], ["docker", "network", "connect", "net", "ctr"])

    def test_network_connect_raises_on_failure(self):
        rec = _RecordingRun({("docker", "network", "connect"): 1})
        with self.assertRaises(launcher.DockerError):
            launcher.DockerCLI(run=rec).network_connect("net", "ctr")

    def test_network_disconnect_and_rm_are_best_effort(self):
        # Teardown must not raise even when the network or endpoint is already gone.
        rec = _RecordingRun(
            {("docker", "network", "disconnect"): 1, ("docker", "network", "rm"): 1}
        )
        cli = launcher.DockerCLI(run=rec)
        cli.network_disconnect("net", "ctr")
        cli.network_rm("net")
        self.assertIn(["docker", "network", "disconnect", "net", "ctr"], rec.argvs)
        self.assertIn(["docker", "network", "rm", "net"], rec.argvs)

    def test_run_foreground_multi_creates_connects_then_starts(self):
        rec = _RecordingRun()
        cli = launcher.DockerCLI(run=rec)
        argv = ["docker", "run", "-it", "--name", "anvil", "--network", "sess", "img"]
        with mock.patch.object(launcher.docker.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 7
            rc = cli.run_foreground_multi(argv, ["base-net"], "anvil")
        self.assertEqual(rc, 7)
        # Created on its primary (session) network...
        self.assertEqual(rec.argvs[0][:2], ["docker", "create"])
        self.assertEqual(rec.argvs[0][rec.argvs[0].index("--network") + 1], "sess")
        # ...connected to the extra network, then started attached.
        self.assertIn(["docker", "network", "connect", "base-net", "anvil"], rec.argvs)
        popen.assert_called_once_with(
            ["docker", "start", "--attach", "--interactive", "anvil"]
        )

    @staticmethod
    def _image_run(entrypoint_json, cmd_json, user_json, inspect_codes=(0,)):
        """A run() that answers `docker image inspect` with canned JSON.

        `inspect_codes` is the return code for each successive inspect call (so a
        test can fail the first and succeed after a pull); other commands return 0.
        """
        state = {"calls": 0}

        def run(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                idx = min(state["calls"], len(inspect_codes) - 1)
                code = inspect_codes[idx]
                state["calls"] += 1
                out = ("%s\n%s\n%s" % (entrypoint_json, cmd_json, user_json)).encode()
                return subprocess.CompletedProcess(argv, code, stdout=out)
            return subprocess.CompletedProcess(argv, 0)

        return run

    def test_image_exec_config_parses_entrypoint_cmd_user(self):
        cli = launcher.DockerCLI(run=self._image_run('["node"]', '["server.js"]', '"1000"'))
        self.assertEqual(cli.image_exec_config("img"), (["node"], ["server.js"], "1000"))

    def test_image_exec_config_treats_null_as_empty(self):
        cli = launcher.DockerCLI(run=self._image_run("null", "null", "null"))
        self.assertEqual(cli.image_exec_config("img"), ([], [], ""))

    def test_image_exec_config_pulls_when_absent_then_succeeds(self):
        rec_run = self._image_run('["app"]', "null", '""', inspect_codes=(1, 0))
        cli = launcher.DockerCLI(run=rec_run)
        self.assertEqual(cli.image_exec_config("img"), (["app"], [], ""))

    def test_image_exec_config_raises_when_still_missing(self):
        cli = launcher.DockerCLI(run=self._image_run("null", "null", "null",
                                                      inspect_codes=(1, 1)))
        with self.assertRaises(launcher.DockerError):
            cli.image_exec_config("img")


if __name__ == "__main__":
    unittest.main(verbosity=2)
