#!/usr/bin/env python3
"""Tests for building a TOML config from ordered layers."""

import datetime
import io
import math
import os
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge.config import merge_toml


class MergeTests(unittest.TestCase):
    def test_nested_tables_merge_recursively(self):
        self.assertEqual(
            merge_toml.merge(
                {"sandbox": {"mode": "workspace", "network": False}},
                {"sandbox": {"network": True}},
            ),
            {"sandbox": {"mode": "workspace", "network": True}},
        )

    def test_later_mcp_server_replaces_the_whole_lower_entry(self):
        self.assertEqual(
            merge_toml.merge(
                {"mcp_servers": {"api": {"command": "repo-tool", "env": {"A": "1"}}}},
                {"mcp_servers": {"api": {"url": "https://org.example/mcp"}}},
            ),
            {"mcp_servers": {"api": {"url": "https://org.example/mcp"}}},
        )

    def test_later_value_replaces_scalar_array_or_table(self):
        self.assertEqual(merge_toml.merge({"value": 1}, {"value": [2]}), {"value": [2]})
        self.assertEqual(merge_toml.merge({"value": [1]}, {"value": {"x": 2}}), {"value": {"x": 2}})
        self.assertEqual(merge_toml.merge({"value": {"x": 1}}, {"value": "new"}), {"value": "new"})


class BuildFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swarmforge-toml-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dst = os.path.join(self.tmp, "config.toml")
        self.err = io.StringIO()

    def layer(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def build(self, *paths):
        return merge_toml.build_file(self.dst, paths, err=self.err)

    def parsed(self):
        with open(self.dst, "rb") as handle:
            return tomllib.load(handle)


class BuildFileTests(BuildFileCase):
    def test_later_layers_win_key_by_key_and_nested_tables_compose(self):
        low = self.layer(
            "low.toml",
            'model = "gpt-5"\n[sandbox]\nmode = "workspace"\nnetwork = false\n',
        )
        high = self.layer(
            "high.toml",
            'reasoning_effort = "high"\n[sandbox]\nnetwork = true\n',
        )

        result = self.build(low, high)

        expected = {
            "model": "gpt-5",
            "reasoning_effort": "high",
            "sandbox": {"mode": "workspace", "network": True},
        }
        self.assertEqual(result, expected)
        self.assertEqual(self.parsed(), expected)

    def test_arrays_replace_instead_of_merging(self):
        low = self.layer("low.toml", 'features = ["a", "b"]\n')
        high = self.layer("high.toml", 'features = ["c"]\n')

        self.build(low, high)

        self.assertEqual(self.parsed(), {"features": ["c"]})

    def test_arrays_of_tables_round_trip_and_replace_as_one_value(self):
        low = self.layer(
            "low.toml",
            '[[hooks]]\nname = "first"\ncommand = ["old"]\n'
            '[[hooks]]\nname = "second"\ncommand = ["also-old"]\n',
        )
        high = self.layer(
            "high.toml",
            '[[hooks]]\nname = "replacement"\ncommand = ["new", "--flag"]\n'
            '[hooks.env]\nMODE = "safe"\n',
        )

        self.build(low, high)

        self.assertEqual(
            self.parsed(),
            {
                "hooks": [
                    {
                        "name": "replacement",
                        "command": ["new", "--flag"],
                        "env": {"MODE": "safe"},
                    }
                ]
            },
        )

    def test_destination_is_output_only(self):
        self.layer("config.toml", 'stale = true\nmodel = "old"\n')
        current = self.layer("current.toml", 'model = "new"\n')

        self.build(current)

        self.assertEqual(self.parsed(), {"model": "new"})

    def test_absent_layer_is_skipped_quietly(self):
        good = self.layer("good.toml", 'model = "gpt-5"\n')

        self.build(os.path.join(self.tmp, "absent.toml"), good)

        self.assertEqual(self.parsed(), {"model": "gpt-5"})
        self.assertEqual(self.err.getvalue(), "")

    def test_malformed_layer_is_reported_and_skipped(self):
        broken = self.layer("broken.toml", "not = = valid\n")
        good = self.layer("good.toml", 'model = "gpt-5"\n')

        self.build(broken, good)

        self.assertEqual(self.parsed(), {"model": "gpt-5"})
        self.assertIn("skipping %s:" % broken, self.err.getvalue())

    def test_non_utf8_layer_is_reported_and_skipped(self):
        broken = os.path.join(self.tmp, "broken.toml")
        with open(broken, "wb") as handle:
            handle.write(b"model = \"" + bytes([0xff]) + b"\"\n")
        good = self.layer("good.toml", "model = \"gpt-5\"\n")

        self.build(broken, good)

        self.assertEqual(self.parsed(), {"model": "gpt-5"})
        self.assertIn("skipping %s:" % broken, self.err.getvalue())

    def test_unreadable_layer_is_reported_and_skipped(self):
        unreadable = self.layer("unreadable.toml", 'model = "old"\n')
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, stat.S_IRUSR | stat.S_IWUSR)
        if os.access(unreadable, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        good = self.layer("good.toml", 'model = "gpt-5"\n')

        self.build(unreadable, good)

        self.assertEqual(self.parsed(), {"model": "gpt-5"})
        self.assertIn(unreadable, self.err.getvalue())

    def test_no_layers_replaces_stale_destination_with_empty_toml(self):
        self.layer("config.toml", "stale = true\n")

        self.build()

        self.assertEqual(self.parsed(), {})

    def test_serializer_round_trips_all_tomllib_value_shapes(self):
        value = {
            "quoted.key": "snowman ☃\nline",
            "emoji 😀 key": "delete \x7f escaped",
            "enabled": True,
            "count": 3,
            "ratio": 1.25,
            "nan": float("nan"),
            "infinity": float("inf"),
            "when": datetime.datetime(2026, 8, 24, 12, 30, tzinfo=datetime.timezone.utc),
            "day": datetime.date(2026, 8, 24),
            "clock": datetime.time(12, 30, 1),
            "items": ["x", 2, {"nested.key": False}],
            "empty": {},
            "section": {"answer": 42, "child": {"ok": True}},
        }

        parsed = tomllib.loads(merge_toml.dumps(value))

        self.assertEqual(parsed.keys(), value.keys())
        self.assertTrue(math.isnan(parsed.pop("nan")))
        expected = dict(value)
        expected.pop("nan")
        self.assertEqual(parsed, expected)


class MainTests(BuildFileCase):
    def test_build_cli(self):
        layer = self.layer("layer.toml", 'model = "gpt-5"\n')
        self.assertEqual(merge_toml.main(["--build", self.dst, layer], self.err), 0)
        self.assertEqual(self.parsed(), {"model": "gpt-5"})

    def test_bad_invocation_and_unknown_option(self):
        self.assertEqual(merge_toml.main([], self.err), 2)
        self.assertIn("usage:", self.err.getvalue())
        self.err.seek(0)
        self.err.truncate(0)
        self.assertEqual(merge_toml.main(["--build", self.dst, "--wat"], self.err), 2)
        self.assertIn("unknown argument", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
