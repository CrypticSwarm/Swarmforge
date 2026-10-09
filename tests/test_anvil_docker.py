#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.docker. Run: python3 tests/test_anvil_docker.py"""

import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `anvil` is already these tests' word for the container the launcher wraps.
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

    def test_termination_signal_is_forwarded_to_the_anvil_and_it_is_reaped(self):
        cli = launcher.DockerCLI(run=_RecordingRun())
        with mock.patch.object(launcher.docker.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.wait.side_effect = [launcher.TerminationSignal(signal.SIGHUP), 0, 0]
            proc.poll.return_value = 0
            with self.assertRaises(launcher.TerminationSignal):
                cli.run_foreground(["docker", "run", "img"])
        proc.send_signal.assert_called_once_with(signal.SIGHUP)
        proc.wait.assert_any_call(timeout=3)
        proc.kill.assert_not_called()

    def test_anvil_ignoring_a_forwarded_signal_is_killed_after_the_grace(self):
        cli = launcher.DockerCLI(run=_RecordingRun())
        with mock.patch.object(launcher.docker.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.wait.side_effect = [
                launcher.TerminationSignal(signal.SIGTERM),
                subprocess.TimeoutExpired(["docker"], 3),
                -9,
            ]
            proc.poll.return_value = None
            with self.assertRaises(launcher.TerminationSignal):
                cli.run_foreground(["docker", "run", "img"])
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once_with()
        self.assertEqual(proc.wait.call_count, 3)

    def test_run_foreground_multi_creates_connects_then_starts(self):
        rec = _RecordingRun()
        cli = launcher.DockerCLI(run=rec)
        argv = ["docker", "run", "-it", "--name", "anvil", "--network", "sess", "img"]
        with mock.patch.object(launcher.docker.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 7
            rc = cli.run_foreground_multi(argv, ["base-net"], "anvil")
        self.assertEqual(rc, 7)
        self.assertEqual(rec.argvs[0][:2], ["docker", "create"])
        self.assertEqual(rec.argvs[0][rec.argvs[0].index("--network") + 1], "sess")
        self.assertIn(["docker", "network", "connect", "base-net", "anvil"], rec.argvs)
        popen.assert_called_once_with(
            ["docker", "start", "--attach", "--interactive", "anvil"]
        )

    @staticmethod
    def _image_run(entrypoint_json, cmd_json, inspect_codes=(0,)):
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
                out = ("%s\n%s" % (entrypoint_json, cmd_json)).encode()
                return subprocess.CompletedProcess(argv, code, stdout=out)
            return subprocess.CompletedProcess(argv, 0)

        return run

    def test_image_exec_config_parses_entrypoint_and_cmd(self):
        cli = launcher.DockerCLI(run=self._image_run('["node"]', '["server.js"]'))
        self.assertEqual(cli.image_exec_config("img"), (["node"], ["server.js"]))

    def test_image_exec_config_treats_null_as_empty(self):
        cli = launcher.DockerCLI(run=self._image_run("null", "null"))
        self.assertEqual(cli.image_exec_config("img"), ([], []))

    def test_image_exec_config_pulls_when_absent_then_succeeds(self):
        rec_run = self._image_run('["app"]', "null", inspect_codes=(1, 0))
        cli = launcher.DockerCLI(run=rec_run)
        self.assertEqual(cli.image_exec_config("img"), (["app"], []))

    def test_image_exec_config_raises_when_still_missing(self):
        cli = launcher.DockerCLI(run=self._image_run("null", "null",
                                                     inspect_codes=(1, 1)))
        with self.assertRaises(launcher.DockerError):
            cli.image_exec_config("img")

    def test_exec_stdin_feeds_payload_and_returns_exit_and_stderr(self):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["input"] = kwargs.get("input")
            seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(argv, 7, stderr=b"is not running\n")

        cli = launcher.DockerCLI(run=run)
        code, stderr = cli.exec_stdin("ctr", ["/bin/sh", "-c", "cat > f"],
                                      b"payload", timeout=12.5)
        self.assertEqual(code, 7)
        self.assertEqual(stderr, "is not running")
        self.assertEqual(seen["argv"],
                         ["docker", "exec", "-i", "ctr", "/bin/sh", "-c", "cat > f"])
        self.assertEqual(seen["input"], b"payload")
        self.assertEqual(seen["timeout"], 12.5)

    def test_exec_stdin_returns_none_on_timeout(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        cli = launcher.DockerCLI(run=run)
        self.assertEqual(cli.exec_stdin("ctr", ["cmd"], b"x", timeout=0.1),
                         (None, ""))


# Unbuffered writes: a handler writing into a buffered file mid-write raises "reentrant call".
_ANVIL_STANDIN = r"""
import os, signal, sys, time
fifo, survived = sys.argv[1], [name for name in sys.argv[2].split(",") if name]
report = os.open(fifo, os.O_WRONLY)
def survive(signum, frame):
    os.write(report, b"%d\n" % signum)
for name in survived:
    signal.signal(getattr(signal, name), survive)
os.write(report, b"ready\n")
time.sleep(60)
"""


class ForegroundInterruptTests(unittest.TestCase):
    """`_wait_foreground` against a real child, interrupted by real signals."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.fifo = os.path.join(tmp, "report")
        os.mkfifo(self.fifo)
        # As under the launcher's trap, only the first of each signal raises.
        for signum, interrupt in ((signal.SIGTERM, launcher.TerminationSignal),
                                  (signal.SIGINT, lambda _signum: KeyboardInterrupt())):
            previous = signal.signal(signum, self._first_only(interrupt))
            self.addCleanup(signal.signal, signum, previous)
        self.procs = []
        self.waits = queue.Queue()
        waits = self.waits

        class ObservedPopen(subprocess.Popen):
            """Announces each wait, so a driver can tell the launcher has moved on."""

            def wait(self, timeout=None):
                waits.put(timeout)
                return super().wait(timeout)

        def recording_popen(argv):
            proc = ObservedPopen(argv)
            self.procs.append(proc)
            return proc

        patcher = mock.patch.object(launcher.docker.subprocess, "Popen", recording_popen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._kill_leftovers)

    @staticmethod
    def _first_only(interrupt):
        taken = []

        def handler(signum, frame):
            if not taken:
                taken.append(signum)
                raise interrupt(signum)

        return handler

    def _kill_leftovers(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def _run(self, survived, drive, grace):
        """Run the stand-in, letting `drive(report, deliver)` interrupt it.

        `deliver(signum)` signals the launcher and returns once it has entered
        its next wait. Returns `(exception raised, stand-in's exit code, seconds taken)`.
        """
        main_thread = threading.main_thread().ident

        def deliver(signum):
            # One landing just before a blocking waitpid waits for it to return, so re-send.
            while True:
                signal.pthread_kill(main_thread, signum)
                try:
                    self.waits.get(timeout=0.5)
                    return
                except queue.Empty:
                    pass

        def driver():
            with open(self.fifo) as report:
                if report.readline() == "ready\n":
                    self.waits.get()
                    drive(report, deliver)
                report.read()  # held open until the stand-in exits, so its reports never break

        threading.Thread(target=driver, daemon=True).start()
        argv = [sys.executable, "-c", _ANVIL_STANDIN, self.fifo, survived]
        started = time.monotonic()
        with mock.patch.object(launcher.docker, "_FOREGROUND_STOP_GRACE_S", grace):
            with self.assertRaises(BaseException) as raised:
                launcher.DockerCLI().run_foreground(argv)
        elapsed = time.monotonic() - started
        (proc,) = self.procs
        self.assertIsNotNone(proc.returncode, "the anvil client was left unreaped")
        return raised.exception, proc.returncode, elapsed

    def test_forwarded_signal_ends_the_anvil(self):
        exc, code, elapsed = self._run(
            "", lambda report, deliver: deliver(signal.SIGTERM), grace=30)
        self.assertIsInstance(exc, launcher.TerminationSignal)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertLess(elapsed, 20)

    def test_anvil_surviving_the_forwarded_signal_is_killed_after_the_grace(self):
        exc, code, _ = self._run(
            "SIGTERM", lambda report, deliver: deliver(signal.SIGTERM), grace=0.2)
        self.assertIsInstance(exc, launcher.TerminationSignal)
        self.assertEqual(code, -signal.SIGKILL)

    def test_signal_while_waiting_out_a_ctrl_c_is_forwarded(self):
        # The anvil survived the Ctrl-C (as Claude's input-clearing does), so the launcher waits.
        def drive(report, deliver):
            deliver(signal.SIGINT)
            deliver(signal.SIGTERM)

        exc, code, elapsed = self._run("", drive, grace=30)
        self.assertIsInstance(exc, launcher.TerminationSignal)
        self.assertIsInstance(exc.__context__, KeyboardInterrupt)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertLess(elapsed, 20)

    def test_ctrl_c_during_the_grace_kills_the_anvil(self):
        def drive(report, deliver):
            deliver(signal.SIGTERM)
            if report.readline() == "%d\n" % signal.SIGTERM:
                deliver(signal.SIGINT)

        exc, code, elapsed = self._run("SIGTERM", drive, grace=30)
        self.assertIsInstance(exc, KeyboardInterrupt)
        self.assertEqual(code, -signal.SIGKILL)
        self.assertLess(elapsed, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
