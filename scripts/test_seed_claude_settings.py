#!/usr/bin/env python3
"""Tests for seeding image defaults into Claude Code's settings.json.

Run: python3 scripts/test_seed_claude_settings.py

The seeder writes into a file the user owns and that persists across runs, so
what it must not do carries as much weight as what it must: no key a config
layer already set may move, and nothing it cannot parse may be overwritten.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest


SEED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "anvil",
    "seed_claude_settings.py",
)
_spec = importlib.util.spec_from_file_location("seed_claude_settings", SEED_PATH)
seed_claude_settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_claude_settings)

STATUSLINE = "/usr/local/bin/swarmforge-statusline"


def _defaults(command=STATUSLINE):
    return {"statusLine": seed_claude_settings.statusline(command)}


class SeedFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="swarmforge-settings-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = os.path.join(tmp, "nested", "settings.json")

    def write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def seed(self, command=STATUSLINE):
        return seed_claude_settings.seed_file(self.path, _defaults(command))

    def test_missing_settings_file_is_created_with_the_default(self):
        self.assertTrue(self.seed())
        self.assertEqual(
            json.loads(self.read()),
            {"statusLine": {"type": "command", "command": STATUSLINE}},
        )

    def test_other_settings_survive_the_seed(self):
        self.write(json.dumps({"model": "sonnet", "env": {"FOO": "bar"}}))
        self.assertTrue(self.seed())
        settings = json.loads(self.read())
        self.assertEqual(settings["model"], "sonnet")
        self.assertEqual(settings["env"], {"FOO": "bar"})
        self.assertEqual(settings["statusLine"]["command"], STATUSLINE)

    def test_a_status_line_from_a_config_layer_wins(self):
        chosen = {"statusLine": {"type": "command", "command": "~/mine.sh"}}
        self.write(json.dumps(chosen))
        self.assertFalse(self.seed())
        self.assertEqual(json.loads(self.read()), chosen)

    def test_reseeding_an_already_seeded_file_changes_nothing(self):
        self.seed()
        before = self.read()
        self.assertFalse(self.seed())
        self.assertEqual(self.read(), before)

    def test_unparseable_settings_are_left_alone(self):
        self.write("{ not json")
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.read(), "{ not json")

    def test_settings_that_are_not_an_object_are_left_alone(self):
        self.write("[1, 2]")
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.read(), "[1, 2]")


class MainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="swarmforge-settings-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = os.path.join(tmp, "settings.json")

    def test_command_line_seeds_the_status_line_it_is_given(self):
        self.assertEqual(
            seed_claude_settings.main([self.path, STATUSLINE]), 0)
        with open(self.path, encoding="utf-8") as handle:
            settings = json.load(handle)
        self.assertEqual(
            settings["statusLine"],
            {"type": "command", "command": STATUSLINE},
        )

    def test_wrong_argument_count_is_a_usage_error(self):
        self.assertEqual(seed_claude_settings.main([self.path]), 2)
        self.assertFalse(os.path.exists(self.path))

    def test_settings_it_cannot_parse_are_reported_not_raised(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(
            seed_claude_settings.main([self.path, STATUSLINE]), 1)


if __name__ == "__main__":
    unittest.main()
