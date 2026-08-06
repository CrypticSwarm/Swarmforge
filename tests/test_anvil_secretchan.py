#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.secretchan. Run: python3 tests/test_anvil_secretchan.py"""

import os
import subprocess
import sys
import threading
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
from swarmforge import tongs


class SecretResolverTests(unittest.TestCase):
    """make_secret_resolver shells out to the provider CLI and reports failures."""

    # Portable provider commands built on the test interpreter so the suite does
    # not depend on op/pass/echo being installed. "{ref}" is substituted by
    # tongs.secret_provider_command before exec.
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

    def test_preserves_inner_and_other_whitespace(self):
        # Only one trailing newline is stripped; interior/extra newlines survive.
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
        # A structured provider that names neither the ref nor `default` stops the
        # launch with a clear message rather than shelling out to a wrong command.
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
        # A failing CLI must not surface the resolved value; here it prints the
        # ref to stderr and fails, and the error names provider/ref (which are
        # not secret) -- the resolver never reaches a secret value on failure.
        resolve = launcher.make_secret_resolver(
            {"boom": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        )
        with self.assertRaises(launcher.SecretResolutionError) as ctx:
            resolve("boom", "ref-token")
        self.assertIn("boom", str(ctx.exception))

    def test_drives_plan_tong_secrets_end_to_end(self):
        # The resolver is the impure half of tongs.plan_tong_secrets: a secret env
        # var resolves to a value under `secrets`, never the plain `-e` env.
        resolve = launcher.make_secret_resolver({"echo": self._writes("sys.argv[1]")})
        plan = tongs.plan_tong_secrets(
            {"REGION": "us", "TOKEN": "${secret:echo:s3cr3t}"}, resolve
        )
        self.assertEqual(plan["env"], {"REGION": "us"})
        self.assertEqual(plan["secrets"], {"TOKEN": "s3cr3t"})


class UidOfTests(unittest.TestCase):
    def test_bare_uid_parses(self):
        self.assertEqual(launcher.secretchan.uid_of("1000"), 1000)
        self.assertEqual(launcher.secretchan.uid_of("1000:1000"), 1000)

    def test_name_or_empty_is_none(self):
        self.assertIsNone(launcher.secretchan.uid_of("appuser"))
        self.assertIsNone(launcher.secretchan.uid_of(""))
        self.assertIsNone(launcher.secretchan.uid_of(None))


class SecretChannelTests(unittest.TestCase):
    def test_times_out_when_no_reader_opens(self):
        # No reader ever opens the FIFO, so the non-blocking write open keeps
        # getting ENXIO; once the (fake) clock passes the deadline it fails closed
        # rather than hanging the launcher.
        channel = launcher.open_secret_channel()
        try:
            clock = iter([0.0, 1.0, 2.0, 99.0])
            with self.assertRaises(launcher.OrchestrationError):
                channel.deliver(
                    "export X='y'\n", timeout=5.0, poll=0.0,
                    sleep=lambda _s: None, monotonic=lambda: next(clock),
                )
        finally:
            channel.cleanup()

    def test_delivers_payload_to_a_reader(self):
        # With a reader attached, the payload is written and the reader sees it
        # followed by EOF -- the real FIFO round-trip, no docker involved.
        channel = launcher.open_secret_channel()
        received = []

        def reader():
            with open(channel.host_path, "r") as handle:
                received.append(handle.read())

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            channel.deliver("export TOKEN='s3cr3t'\n")
        finally:
            thread.join(timeout=5)
            channel.cleanup()
        self.assertEqual(received, ["export TOKEN='s3cr3t'\n"])

    def test_delivers_payload_larger_than_pipe_buffer(self):
        # A payload bigger than the pipe capacity (~64 KiB) forces several writes
        # and a full buffer; every byte must still arrive (no silent truncation).
        channel = launcher.open_secret_channel()
        payload = "export BIG='" + ("x" * 200000) + "'\n"
        received = []

        def reader():
            with open(channel.host_path, "r") as handle:
                received.append(handle.read())

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            channel.deliver(payload)
        finally:
            thread.join(timeout=10)
            channel.cleanup()
        self.assertEqual(received, [payload])


if __name__ == "__main__":
    unittest.main(verbosity=2)
