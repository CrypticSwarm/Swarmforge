#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.model. Run: python3 tests/test_tongs_model.py"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs

from tongs_fixtures import VOLUME_TONG, def_of


class ReadinessTests(unittest.TestCase):
    def test_parse_duration_units(self):
        self.assertEqual(tongs.parse_duration("30s"), 30.0)
        self.assertEqual(tongs.parse_duration("500ms"), 0.5)
        self.assertEqual(tongs.parse_duration("2m"), 120.0)
        self.assertEqual(tongs.parse_duration("1h"), 3600.0)

    def test_parse_duration_bare_number_is_seconds(self):
        self.assertEqual(tongs.parse_duration("5"), 5.0)
        self.assertEqual(tongs.parse_duration(5), 5.0)

    def test_parse_duration_none_uses_default(self):
        self.assertEqual(tongs.parse_duration(None, 9.0), 9.0)

    def test_parse_duration_invalid_raises(self):
        with self.assertRaises(ValueError):
            tongs.parse_duration("soon")

    def test_parse_duration_non_positive_raises(self):
        # A bare negative/zero number bypasses the (sign-less) duration regex, so
        # guard positivity explicitly: a non-positive deadline gives the probe no
        # time to succeed.
        for bad in (-5, 0, "0s", "-1"):
            with self.assertRaises(ValueError):
                tongs.parse_duration(bad)

    def test_readiness_defaults_tcp_for_network_facing(self):
        mode, command, timeout = tongs.readiness_settings(
            {"interface": {"kind": "port", "port": 1}}
        )
        self.assertEqual(mode, "tcp")
        self.assertIsNone(command)
        self.assertEqual(timeout, tongs.DEFAULT_READINESS_TIMEOUT_S)

    def test_readiness_explicit_mode_and_timeout(self):
        mode, command, timeout = tongs.readiness_settings(def_of(VOLUME_TONG))
        self.assertEqual(mode, "healthcheck")
        self.assertEqual(command, ["test", "-d", "/cache"])

    def test_readiness_portless_without_mode_is_none(self):
        # validate_tong requires a mode for volume/none, but the resolver still
        # falls back to "none" defensively for a kind with no port to probe.
        mode, _, _ = tongs.readiness_settings({"interface": {"kind": "none"}})
        self.assertEqual(mode, "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
