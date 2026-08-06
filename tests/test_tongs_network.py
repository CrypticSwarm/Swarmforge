#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.network. Run: python3 tests/test_tongs_network.py"""

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

from tongs_fixtures import (
    GITHUB_TONG,
    NONE_TONG,
    PORT_TONG,
    VOLUME_TONG,
    def_of,
)


# A long-lived `shared` tong reached over the network (the ollama shape): the
# anvil dials it by its canonical alias on whatever network it ends up on.
SHARED_PORT_TONG = """\
lifecycle: shared
image: ollama/ollama
interface:
  kind: port
  port: 11434
readiness:
  mode: tcp
"""


class SessionNetworkTests(unittest.TestCase):
    def test_session_network_name_sanitizes_and_prefixes(self):
        self.assertEqual(
            tongs.session_network_name("claude-myproject"),
            "swarmforge-session-claude-myproject",
        )
        # Characters docker forbids in a network name collapse to a hyphen, and
        # leading/trailing separators are trimmed.
        self.assertEqual(
            tongs.session_network_name("opencode-my proj/wt:1"),
            "swarmforge-session-opencode-my-proj-wt-1",
        )

    def test_no_session_tongs_keeps_base_network(self):
        # The gate: with no session tongs the anvil keeps today's single network
        # and no per-session network is created -- the basis of an unchanged
        # zero-tong launch.
        plan = tongs.plan_network({}, "opencode-net", "claude-myproject")
        self.assertEqual(
            plan,
            {
                "network": "opencode-net",
                "create": None,
                "extra_networks": [],
                "session_aliases": [],
                "shared_connect": [],
            },
        )

    def test_shared_only_keeps_base_network_and_does_not_connect(self):
        # A `shared` tong with no `session` tong stays reachable on the base
        # network as before; per-session connection only happens once a
        # per-session network exists.
        merged = {"ollama": {"source": tongs.REPO, "definition": def_of(SHARED_PORT_TONG)}}
        plan = tongs.plan_network(merged, "opencode-net", "claude-myproject")
        self.assertEqual(plan["network"], "opencode-net")
        self.assertIsNone(plan["create"])
        self.assertEqual(plan["shared_connect"], [])

    def test_session_tong_creates_per_session_network(self):
        merged = {"github-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)}}
        plan = tongs.plan_network(merged, "opencode-net", "claude-myproject")
        net = "swarmforge-session-claude-myproject"
        self.assertEqual(plan["network"], net)
        self.assertEqual(plan["create"], net)
        # The anvil also joins the pre-existing base network (the NETWORK= hatch).
        self.assertEqual(plan["extra_networks"], ["opencode-net"])
        # Aliased by the MCP server name, not the tong's own name.
        self.assertEqual(plan["session_aliases"], [("github-creds", ["github"])])

    def test_shared_tong_connected_per_session_when_network_exists(self):
        # A session tong forces a per-session network; the shared tong is then
        # connected to it under its canonical alias.
        merged = {
            "github-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)},
            "ollama": {"source": tongs.REPO, "definition": def_of(SHARED_PORT_TONG)},
        }
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(plan["session_aliases"], [("github-creds", ["github"])])
        self.assertEqual(plan["shared_connect"], [("ollama", ["ollama"])])

    def test_portless_session_tongs_get_no_alias(self):
        # volume/none tongs have no listener; they still trigger a per-session
        # network (they are session-scoped) but get no network alias.
        merged = {
            "watcher": {"source": tongs.REPO, "definition": def_of(NONE_TONG)},
            "cache": {"source": tongs.REPO, "definition": def_of(VOLUME_TONG)},
            "pg": {"source": tongs.REPO, "definition": def_of(PORT_TONG)},
        }
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(plan["create"], "swarmforge-session-wt")
        # Only the network-facing port tong is aliased.
        self.assertEqual(plan["session_aliases"], [("pg", ["pg"])])

    def test_empty_base_network_yields_no_extras(self):
        merged = {"pg": {"source": tongs.REPO, "definition": def_of(PORT_TONG)}}
        plan = tongs.plan_network(merged, "", "wt")
        self.assertEqual(plan["extra_networks"], [])

    def test_alias_collision_on_session_network_keeps_first(self):
        # Two tongs that resolve to the same canonical alias cannot share the one
        # session network; the first by sorted tong name wins, the other drops --
        # and the winner is deterministic across the session/shared split. Here a
        # session mcp tong (alias "github") collides with a shared tong literally
        # named "github" (alias "github").
        merged = {
            "a-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)},
            "github": {"source": tongs.REPO, "definition": def_of(SHARED_PORT_TONG)},
        }
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(plan["session_aliases"], [("a-creds", ["github"])])
        # The shared tong loses the alias and is not connected.
        self.assertEqual(plan["shared_connect"], [])

    def test_plan_carries_every_declared_alias(self):
        defn = def_of(GITHUB_TONG)
        defn["interface"]["aliases"] = ["api", "local.example.test"]
        merged = {"github-creds": {"source": tongs.REPO, "definition": defn}}
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(
            plan["session_aliases"],
            [("github-creds", ["github", "api", "local.example.test"])],
        )

    def test_contested_alias_drops_only_that_name(self):
        # Dedup is per alias: the loser keeps the names nobody else claimed
        # rather than dropping off the network entirely.
        first = def_of(GITHUB_TONG)
        first["interface"]["aliases"] = ["api"]
        second = def_of(SHARED_PORT_TONG)
        second["interface"]["aliases"] = ["api", "console"]
        merged = {
            "a-creds": {"source": tongs.REPO, "definition": first},
            "ollama": {"source": tongs.REPO, "definition": second},
        }
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(plan["session_aliases"], [("a-creds", ["github", "api"])])
        self.assertEqual(plan["shared_connect"], [("ollama", ["ollama", "console"])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
