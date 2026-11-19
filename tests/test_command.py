#!/usr/bin/env python3
"""Unit tests for swarmforge.command. Run: python3 tests/test_command.py"""

import contextlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the CLI's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import command


class Failed(command.CommandError):
    pass


def python(source):
    return [sys.executable, "-c", source]


@contextlib.contextmanager
def fds_to_files():
    """Collect what reaches fds 1 and 2, which the child inherits, as text."""
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        collected = {}
        saved = [os.dup(1), os.dup(2)]
        os.dup2(out.fileno(), 1)
        os.dup2(err.fileno(), 2)
        try:
            yield collected
        finally:
            for fd, original in enumerate(saved, start=1):
                os.dup2(original, fd)
                os.close(original)
            for name, sink in (("out", out), ("err", err)):
                sink.seek(0)
                collected[name] = sink.read()


class Run(unittest.TestCase):
    def test_capture_returns_stdout(self):
        self.assertEqual(command.run(python("print('hi')"), capture=True), "hi\n")

    def test_uncaptured_output_goes_to_stderr_not_stdout(self):
        """`$(swarmforge tmux open -d)` must hold only what the CLI prints."""
        with fds_to_files() as fds:
            self.assertIsNone(command.run(python("print('noise')")))
        self.assertEqual((fds["out"], fds["err"]), ("", "noise\n"))

    def test_quiet_discards_stderr(self):
        failing = python("import sys; sys.stderr.write('fatal'); sys.exit(1)")
        with fds_to_files() as fds:
            with self.assertRaises(Failed):
                command.run(failing, Failed, quiet=True)
        self.assertEqual(fds["err"], "")

    def test_a_nonzero_exit_raises_the_given_error_with_its_status(self):
        with self.assertRaises(Failed) as caught:
            command.run(python("import sys; sys.exit(3)"), Failed)
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn("exited 3", str(caught.exception))

    def test_a_command_that_cannot_start_has_no_status(self):
        with self.assertRaises(Failed) as caught:
            command.run(["/no/such/binary-xyz"], Failed)
        self.assertIsNone(caught.exception.returncode)
        self.assertIn("could not run /no/such/binary-xyz", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
