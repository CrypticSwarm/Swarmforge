#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.model. Run: python3 tests/test_tongs_model.py"""

import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `python3 -m unittest tests.<module>` does not put this directory on the path.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from swarmforge import tongs

from tongs_fixtures import VOLUME_TONG, def_of


class PrintableTests(unittest.TestCase):
    def test_controls_and_invisible_characters_become_escapes(self):
        self.assertEqual(
            tongs.printable("\x1b[2K\r\n\t\b\x7f\x9b\u202e\u2066\u200b\u00a0"),
            "\\x1b[2K\\r\\n\\t\\x08\\x7f\\x9b\\u202e\\u2066\\u200b\\xa0",
        )

    def test_printable_text_is_unchanged(self):
        for text in ("", "registry/img:1.0 @sha256:abc", "naïve 日本 Ωmega", "back\\slash"):
            self.assertEqual(tongs.printable(text), text)

    def test_output_is_printable_for_every_code_point(self):
        every = "".join(chr(cp) for cp in range(0x110000))
        self.assertTrue(tongs.printable(every).isprintable())

    def test_warn_escapes_its_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tongs.warn("tong 'x\x1b[2K\napproved' is refused")
        self.assertEqual(err.getvalue(), "tongs: tong 'x\\x1b[2K\\napproved' is refused\n")


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
        # A bare negative or zero slips past the sign-less duration regex.
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
        # validate_tong already requires a mode here; the fallback is defensive.
        mode, _, _ = tongs.readiness_settings({"interface": {"kind": "none"}})
        self.assertEqual(mode, "none")


class GpuRequestTests(unittest.TestCase):
    def test_parse_gpus_none_is_not_requested(self):
        self.assertIsNone(tongs.parse_gpus(None))

    def test_parse_gpus_accepts_all_and_positive_counts(self):
        self.assertEqual(tongs.parse_gpus("all"), "all")
        self.assertEqual(tongs.parse_gpus(2), "2")
        self.assertEqual(tongs.parse_gpus("2"), "2")

    def test_parse_gpus_accepts_device_selectors_verbatim(self):
        for selector in ("device=0", "device=GPU-3a8f-11ee", '"device=0"',
                         '"device=0,1"', '"device=GPU-3a8f,MIG-7c2e"', "device=GPU-1:0"):
            with self.subTest(selector=selector):
                self.assertEqual(tongs.parse_gpus(selector), selector)

    def test_parse_gpus_rejects_non_positive_and_non_count_types(self):
        for bad in (True, False, 0, -1, 1.5, ["all"], {"count": 1}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tongs.parse_gpus(bad)

    def test_parse_gpus_rejects_counts_docker_would_not_read_as_positive(self):
        # int() accepts several of these; docker's Atoi does not.
        for bad in ("0", "00", "-1", "+1", "1_0", "\u0663", " 2", ""):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tongs.parse_gpus(bad)

    def test_parse_gpus_rejects_docker_keys_beyond_device(self):
        for bad in ("count=2", "driver=cdi,device=vendor.com/class=x",
                    "capabilities=compute", "options=x", '"device=0",driver=nvidia',
                    "ALL", " all", "device=", '"device=0,"', "device=-0"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tongs.parse_gpus(bad)

    def test_parse_gpus_rejects_control_characters(self):
        for bad in ("\x1b[1A\x1b[2K", "all\x1b[2K", "device=0\r"):
            with self.subTest(value=bad), self.assertRaises(ValueError) as caught:
                tongs.parse_gpus(bad)
            self.assertNotIn("\x1b", str(caught.exception))

    def test_parse_gpus_unquoted_device_list_hints_at_the_quoting(self):
        with self.assertRaisesRegex(ValueError, "double quotes") as caught:
            tongs.parse_gpus("device=0,1")
        self.assertIn("""gpus: '"device=0,1"'""", str(caught.exception))

    def test_parse_gpus_caps_the_count(self):
        limit = tongs.GPU_COUNT_MAX
        self.assertEqual(tongs.parse_gpus(limit), str(limit))
        self.assertEqual(tongs.parse_gpus(str(limit)), str(limit))
        for bad in (limit + 1, str(limit + 1), 10**30, "99999999999999999999"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tongs.parse_gpus(bad)

    def test_parse_gpus_matches_the_whole_value(self):
        for bad in ("all\n", "2\n", "device=0\n", '"device=0,1"\n', "device=0,1\n"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tongs.parse_gpus(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
