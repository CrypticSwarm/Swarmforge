#!/usr/bin/env python3
"""Tests for delivering generated tong MCP servers into a harness config.toml.

Run: python3 tests/test_merge_toml_mcp.py

The config dest is a persistent home, so the servers live in a managed block
rather than being appended. Most of what is asserted below is about that
block's edges: what a rerun replaces, what a session with no tongs leaves
behind, and what the user's own config keeps.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The image puts the swarmforge package on PYTHONPATH; standing in for that
# here keeps this file runnable on its own, not just under a discovery run
# that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge.config import merge_toml_mcp


FRAGMENT = {"mcp_servers": {"github": {"url": "http://github:8080/mcp"}}}


def _write_fragment(tmp, value):
    path = os.path.join(tmp, "tong-mcp.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    return path


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class MergeTomlMcpTests(unittest.TestCase):
    def _merge(self, config, fragment=None):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            merge_toml_mcp.merge(config, fragment)
        return stderr.getvalue()

    def test_creates_config_with_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            self._merge(config, _write_fragment(tmp, FRAGMENT))

            text = _read(config)
            self.assertIn(merge_toml_mcp.BLOCK_BEGIN, text)
            self.assertIn(merge_toml_mcp.BLOCK_END, text)
            self.assertEqual(
                tomllib.loads(text),
                {"mcp_servers": {"github": {"url": "http://github:8080/mcp"}}},
            )

    def test_appends_block_preserving_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            with open(config, "w", encoding="utf-8") as handle:
                handle.write('[model.grok-4]\ntemperature = 0.2\n')

            self._merge(config, _write_fragment(tmp, FRAGMENT))

            parsed = tomllib.loads(_read(config))
            self.assertEqual(parsed["model"], {"grok-4": {"temperature": 0.2}})
            self.assertEqual(
                parsed["mcp_servers"], {"github": {"url": "http://github:8080/mcp"}}
            )

    def test_rerun_replaces_stale_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            self._merge(config, _write_fragment(tmp, FRAGMENT))
            other = {"mcp_servers": {"asana": {"url": "http://asana:3000/mcp"}}}
            self._merge(config, _write_fragment(tmp, other))

            parsed = tomllib.loads(_read(config))
            self.assertEqual(
                parsed["mcp_servers"], {"asana": {"url": "http://asana:3000/mcp"}}
            )

    def test_no_fragment_strips_block_and_keeps_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            with open(config, "w", encoding="utf-8") as handle:
                handle.write('[skills]\nignore = ["scratch"]\n')
            self._merge(config, _write_fragment(tmp, FRAGMENT))

            self._merge(config)

            text = _read(config)
            self.assertNotIn(merge_toml_mcp.BLOCK_BEGIN, text)
            self.assertEqual(tomllib.loads(text), {"skills": {"ignore": ["scratch"]}})

    def test_no_fragment_and_no_config_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            self._merge(config)
            self.assertEqual(os.listdir(tmp), [])

    def test_user_defined_server_wins_over_generated_one(self):
        # A same-named table in the user's own config would be a TOML
        # duplicate-table error if the generated entry were appended; the
        # user's definition is kept and the collision reported.
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            with open(config, "w", encoding="utf-8") as handle:
                handle.write('[mcp_servers.github]\nurl = "https://example.com/mcp"\n')
            fragment = {
                "mcp_servers": {
                    "github": {"url": "http://github:8080/mcp"},
                    "asana": {"url": "http://asana:3000/mcp"},
                }
            }

            warnings = self._merge(config, _write_fragment(tmp, fragment))

            self.assertIn("github", warnings)
            parsed = tomllib.loads(_read(config))
            self.assertEqual(
                parsed["mcp_servers"],
                {
                    "github": {"url": "https://example.com/mcp"},
                    "asana": {"url": "http://asana:3000/mcp"},
                },
            )

    def test_invalid_user_config_still_gets_block(self):
        # The harness will reject the broken config on its own; the merge only
        # loses the duplicate-name check and says so.
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            with open(config, "w", encoding="utf-8") as handle:
                handle.write("not valid = = toml\n")

            warnings = self._merge(config, _write_fragment(tmp, FRAGMENT))

            self.assertIn("duplicate-name check", warnings)
            self.assertIn(merge_toml_mcp.BLOCK_BEGIN, _read(config))

    def test_unusual_server_name_is_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            fragment = {"mcp_servers": {"my.server": {"url": "http://s:1/mcp"}}}
            self._merge(config, _write_fragment(tmp, fragment))

            parsed = tomllib.loads(_read(config))
            self.assertEqual(parsed["mcp_servers"], {"my.server": {"url": "http://s:1/mcp"}})


if __name__ == "__main__":
    unittest.main()
