#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.cli. Run: python3 tests/test_tongs_cli.py"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs

from tongs_fixtures import GITHUB_TONG


class CliTests(unittest.TestCase):
    def test_validate_command_returns_zero_for_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "github.yaml"), "w") as f:
                f.write(GITHUB_TONG)
            self.assertEqual(tongs.main(["validate", tmp]), 0)

    def test_validate_command_flags_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "broken.yaml"), "w") as f:
                f.write("image: x\n")  # missing lifecycle + interface
            self.assertEqual(tongs.main(["validate", tmp]), 1)

    def test_usage_on_bad_args(self):
        self.assertEqual(tongs.main([]), 2)
        self.assertEqual(tongs.main(["bogus", "/tmp"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
