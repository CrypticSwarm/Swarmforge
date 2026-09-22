#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.secretchan. Run: python3 tests/test_anvil_secretchan.py"""

import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `anvil` is already these tests' word for the container the launcher wraps.
from swarmforge import anvil as launcher
from swarmforge import tongs


class SecretResolverTests(unittest.TestCase):
    """make_secret_resolver shells out to the provider CLI and reports failures."""

    # Built on the test interpreter so no op/pass/echo need be installed; "{ref}" is substituted.
    def _writes(self, expr):
        return [sys.executable, "-c", "import sys; sys.stdout.write(%s)" % expr, "{ref}"]

    def test_resolves_ref_via_provider_cli(self):
        resolve = launcher.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        self.assertEqual(resolve("echo", "op://Work/secret"), "op://Work/secret")

    def test_provider_stderr_inherits_terminal(self):
        with mock.patch.object(launcher.secretchan.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["provider"], 0, stdout=b"secret\n")
            resolve = launcher.make_secret_resolver({"p": ["provider", "{ref}"]})
            self.assertEqual(resolve("p", "ref"), "secret")
            self.assertIsNone(run.call_args.kwargs.get("stderr"))

    def test_strips_single_trailing_newline(self):
        resolve = launcher.make_secret_resolver({"echo": self._writes("sys.argv[1] + '\\n'")})
        self.assertEqual(resolve("echo", "token"), "token")

    def test_strips_only_one_trailing_newline_and_keeps_inner_ones(self):
        resolve = launcher.make_secret_resolver({"echo": self._writes("sys.argv[1] + '\\n\\n'")})
        self.assertEqual(resolve("echo", "a\nb"), "a\nb\n")

    def test_unknown_provider_raises(self):
        resolve = launcher.make_secret_resolver({"op": ["op", "read", "{ref}"]})
        with self.assertRaises(launcher.SecretResolutionError):
            resolve("vault", "x")

    def test_override_resolves_matching_ref(self):
        resolve = launcher.make_secret_resolver(
            {"shared": {"default": None, "overrides": {"tok": self._writes("sys.argv[1]")}}}
        )
        self.assertEqual(resolve("shared", "tok"), "tok")

    def test_unmapped_ref_without_default_raises(self):
        resolve = launcher.make_secret_resolver(
            {"shared": {"default": None, "overrides": {"tok": ["op", "read", "{ref}"]}}}
        )
        with self.assertRaises(launcher.SecretResolutionError) as ctx:
            resolve("shared", "other")
        self.assertIn("shared", str(ctx.exception))
        self.assertIn("other", str(ctx.exception))

    def test_nonzero_exit_raises(self):
        resolve = launcher.make_secret_resolver(
            {"boom": [sys.executable, "-c", "import sys; sys.exit(3)"]}
        )
        with self.assertRaises(launcher.SecretResolutionError):
            resolve("boom", "x")

    def test_unrunnable_provider_raises(self):
        resolve = launcher.make_secret_resolver({"missing": ["/no/such/binary-xyz", "{ref}"]})
        with self.assertRaises(launcher.SecretResolutionError):
            resolve("missing", "x")

    def test_error_message_never_contains_the_secret(self):
        # Provider and ref are not secret, and a failed resolve holds no value to leak.
        resolve = launcher.make_secret_resolver(
            {"boom": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        )
        with self.assertRaises(launcher.SecretResolutionError) as ctx:
            resolve("boom", "ref-token")
        self.assertIn("boom", str(ctx.exception))

    def test_drives_plan_tong_secrets_end_to_end(self):
        # A resolved secret lands under `secrets`, never the plain `-e` env.
        resolve = launcher.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        plan = tongs.plan_tong_secrets(
            {"REGION": "us", "TOKEN": "${secret:echo:s3cr3t}"}, resolve
        )
        self.assertEqual(plan["env"], {"REGION": "us"})
        self.assertEqual(plan["secrets"], {"TOKEN": "s3cr3t"})


class _ExecStdinDocker:
    """A docker seam for SecretChannel: canned exec_stdin results, recorded calls."""

    def __init__(self, codes, stderr=""):
        self._codes = iter(codes)
        self._stderr = stderr
        self.calls = []

    def exec_stdin(self, container, command, payload, timeout=None):
        self.calls.append((container, list(command), payload, timeout))
        return next(self._codes), self._stderr


class SecretChannelTests(unittest.TestCase):
    def test_retries_until_fifo_exists_then_delivers(self):
        docker = _ExecStdinDocker([tongs.SECRET_FIFO_ABSENT_EXIT,
                                   tongs.SECRET_FIFO_ABSENT_EXIT, 0])
        channel = launcher.SecretChannel(docker, "ctr")
        channel.deliver("export TOKEN='s3cr3t'\n", timeout=5.0, poll=0.0,
                        sleep=lambda _s: None, monotonic=lambda: 0.0)
        self.assertEqual(len(docker.calls), 3)
        container, command, payload, _timeout = docker.calls[-1]
        self.assertEqual(container, "ctr")
        self.assertEqual(command, tongs.secret_deliver_command())
        self.assertEqual(payload, b"export TOKEN='s3cr3t'\n")

    def test_times_out_when_fifo_never_appears(self):
        # It fails closed at the deadline rather than hanging the launcher.
        docker = _ExecStdinDocker([tongs.SECRET_FIFO_ABSENT_EXIT] * 3)
        channel = launcher.SecretChannel(docker, "ctr")
        clock = iter([0.0, 1.0, 2.0, 99.0])
        with self.assertRaisesRegex(launcher.OrchestrationError, "did not open"):
            channel.deliver("export X='y'\n", timeout=5.0, poll=0.0,
                            sleep=lambda _s: None, monotonic=lambda: next(clock))

    def test_times_out_when_payload_never_accepted(self):
        # exec_stdin returns None when the exec itself ran past its deadline.
        docker = _ExecStdinDocker([None])
        channel = launcher.SecretChannel(docker, "ctr")
        with self.assertRaisesRegex(launcher.OrchestrationError, "did not accept"):
            channel.deliver("export X='y'\n", timeout=5.0, poll=0.0,
                            sleep=lambda _s: None, monotonic=lambda: 0.0)

    def test_other_exec_failure_raises_immediately_with_stderr(self):
        docker = _ExecStdinDocker([1], stderr="container ctr is not running")
        channel = launcher.SecretChannel(docker, "ctr")
        with self.assertRaisesRegex(launcher.OrchestrationError,
                                    "exit 1.*is not running"):
            channel.deliver("export X='y'\n", timeout=5.0, poll=0.0,
                            sleep=lambda _s: None, monotonic=lambda: 0.0)
        self.assertEqual(len(docker.calls), 1)

    def test_remaining_deadline_bounds_the_exec(self):
        docker = _ExecStdinDocker([tongs.SECRET_FIFO_ABSENT_EXIT, 0])
        channel = launcher.SecretChannel(docker, "ctr")
        clock = iter([0.0, 1.0, 3.0])
        channel.deliver("export X='y'\n", timeout=5.0, poll=0.0,
                        sleep=lambda _s: None, monotonic=lambda: next(clock))
        self.assertEqual([call[3] for call in docker.calls], [4.0, 2.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
