#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.mcp. Run: python3 tests/test_tongs_mcp.py"""

import json
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


def _merged(name, text):
    """Wrap a single definition the way merge_tongs returns it."""
    return {name: {"source": tongs.WORKSPACE, "definition": def_of(text)}}


class EnvNamingTests(unittest.TestCase):
    def test_prefix_sanitizes_name(self):
        self.assertEqual(tongs.tong_env_prefix("github-creds"), "SWARMFORGE_TONG_GITHUB_CREDS")
        self.assertEqual(tongs.tong_env_prefix("pg.test_01"), "SWARMFORGE_TONG_PG_TEST_01")

    def test_var_appends_suffix(self):
        self.assertEqual(tongs.tong_env_var("pg", "host"), "SWARMFORGE_TONG_PG_HOST")
        self.assertEqual(tongs.tong_env_var("pg", "PORT"), "SWARMFORGE_TONG_PG_PORT")


class InterfaceWiringTests(unittest.TestCase):
    def test_canonical_alias_mcp_uses_interface_name(self):
        # The tong's own name (github-creds) differs from the MCP server name.
        defn = def_of(GITHUB_TONG)
        self.assertEqual(tongs.canonical_alias("github-creds", defn), "github")

    def test_canonical_alias_non_mcp_uses_tong_name(self):
        self.assertEqual(tongs.canonical_alias("pg", def_of(PORT_TONG)), "pg")
        self.assertEqual(tongs.canonical_alias("cache", def_of(VOLUME_TONG)), "cache")
        self.assertEqual(tongs.canonical_alias("watcher", def_of(NONE_TONG)), "watcher")

    def test_tong_aliases_is_canonical_only_by_default(self):
        # A tong that declares no extras keeps exactly the one DNS name it has
        # today, so the flag it produces is unchanged.
        self.assertEqual(tongs.tong_aliases("github-creds", def_of(GITHUB_TONG)), ["github"])
        self.assertEqual(tongs.tong_aliases("pg", def_of(PORT_TONG)), ["pg"])

    def test_tong_aliases_appends_extras_canonical_first(self):
        defn = def_of(PORT_TONG)
        defn["interface"]["aliases"] = ["api", "local.example.test"]
        self.assertEqual(
            tongs.tong_aliases("pg", defn), ["pg", "api", "local.example.test"]
        )

    def test_tong_aliases_dedups_extras(self):
        defn = def_of(GITHUB_TONG)
        defn["interface"]["aliases"] = ["github", "api", "api"]
        self.assertEqual(tongs.tong_aliases("github-creds", defn), ["github", "api"])

    def test_tong_aliases_empty_without_a_listener(self):
        for text, name in ((VOLUME_TONG, "cache"), (NONE_TONG, "watcher")):
            self.assertEqual(tongs.tong_aliases(name, def_of(text)), [])

    def test_mcp_url_default_and_custom_path(self):
        defn = def_of(GITHUB_TONG)
        self.assertEqual(tongs.mcp_url(defn, "github"), "http://github:8080/mcp")
        defn["interface"]["path"] = "rpc"  # leading slash is supplied
        self.assertEqual(tongs.mcp_url(defn, "github"), "http://github:8080/rpc")

    def test_port_env_injects_host_and_port(self):
        env = tongs.anvil_env("pg", def_of(PORT_TONG))
        self.assertEqual(env, {"SWARMFORGE_TONG_PG_HOST": "pg", "SWARMFORGE_TONG_PG_PORT": "5432"})

    def test_volume_env_injects_path(self):
        env = tongs.anvil_env("cache", def_of(VOLUME_TONG))
        self.assertEqual(env, {"SWARMFORGE_TONG_CACHE_PATH": "/cache"})

    def test_mcp_and_none_inject_no_env(self):
        self.assertEqual(tongs.anvil_env("github-creds", def_of(GITHUB_TONG)), {})
        self.assertEqual(tongs.anvil_env("watcher", def_of(NONE_TONG)), {})

    def test_volume_mount_only_for_volume_kind(self):
        self.assertEqual(
            tongs.anvil_mounts("cache", def_of(VOLUME_TONG)),
            [{"volume": "build-cache", "mountpoint": "/cache"}],
        )
        self.assertEqual(tongs.anvil_mounts("pg", def_of(PORT_TONG)), [])
        self.assertEqual(tongs.anvil_mounts("github-creds", def_of(GITHUB_TONG)), [])

    def test_mcp_tongs_selects_and_keys_by_alias(self):
        merged = {
            "github-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)},
            "pg": {"source": tongs.WORKSPACE, "definition": def_of(PORT_TONG)},
        }
        selected = tongs.mcp_tongs(merged)
        self.assertEqual(list(selected), ["github"])  # only the mcp tong, keyed by alias

    def test_mcp_alias_collision_keeps_first_and_drops_duplicate(self):
        first = def_of(GITHUB_TONG)
        first["image"] = "first-wins"
        second = def_of(GITHUB_TONG)
        second["image"] = "second-loses"
        merged = {
            "a-creds": {"source": tongs.REPO, "definition": first},
            "b-creds": {"source": tongs.REPO, "definition": second},
        }
        selected = tongs.mcp_tongs(merged)
        # Both resolve to alias "github"; the first by sorted tong name wins.
        self.assertEqual(list(selected), ["github"])
        self.assertEqual(selected["github"]["image"], "first-wins")

    def test_opencode_mcp_fragment_shape(self):
        fragment = tongs.mcp_config_opencode(_merged("github-creds", GITHUB_TONG))
        self.assertEqual(
            fragment,
            {"mcp": {"github": {"type": "remote", "url": "http://github:8080/mcp", "enabled": True}}},
        )

    def test_claude_mcp_config_shape(self):
        config = tongs.mcp_config_claude(_merged("github-creds", GITHUB_TONG))
        self.assertEqual(
            config,
            {"mcpServers": {"github": {"type": "http", "url": "http://github:8080/mcp"}}},
        )

    def test_mcp_config_empty_when_no_mcp_tongs(self):
        # port-only set -> no MCP fragment at all (omitted, not an empty block).
        port_only = _merged("pg", PORT_TONG)
        self.assertEqual(tongs.mcp_config_opencode(port_only), {})
        self.assertEqual(tongs.mcp_config_claude(port_only), {})
        self.assertEqual(tongs.mcp_config_opencode({}), {})
        self.assertEqual(tongs.mcp_config_claude({}), {})

    def test_plan_injection_aggregates_across_kinds(self):
        merged = {
            "github-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)},
            "pg": {"source": tongs.WORKSPACE, "definition": def_of(PORT_TONG)},
            "cache": {"source": tongs.REPO, "definition": def_of(VOLUME_TONG)},
            "watcher": {"source": tongs.REPO, "definition": def_of(NONE_TONG)},
        }
        plan = tongs.plan_injection(merged, "claude")
        self.assertEqual(
            plan["env"],
            {
                "SWARMFORGE_TONG_PG_HOST": "pg",
                "SWARMFORGE_TONG_PG_PORT": "5432",
                "SWARMFORGE_TONG_CACHE_PATH": "/cache",
            },
        )
        self.assertEqual(plan["mounts"], [{"volume": "build-cache", "mountpoint": "/cache"}])
        self.assertEqual(plan["mcp"], {"mcpServers": {"github": {"type": "http", "url": "http://github:8080/mcp"}}})

    def test_plan_injection_inert_when_empty(self):
        # The inert-when-empty invariant for this layer: nothing in, nothing out.
        for harness in ("opencode", "claude"):
            self.assertEqual(
                tongs.plan_injection({}, harness),
                {"env": {}, "mounts": [], "mcp": {}},
            )

    def test_plan_injection_unknown_harness_omits_mcp(self):
        plan = tongs.plan_injection(_merged("github-creds", GITHUB_TONG), "nonesuch")
        self.assertEqual(plan["mcp"], {})

    def test_plan_injection_never_emits_secret_references(self):
        # The GitHub tong carries an unresolved ${secret:...} in its env, but
        # interface wiring only ever surfaces host/port/path, never the tong's
        # own env, so no secret reference reaches the anvil injection plan.
        plan = tongs.plan_injection(_merged("github-creds", GITHUB_TONG), "claude")
        self.assertNotIn("${secret", json.dumps(plan))
        self.assertNotIn("GITHUB_TOKEN", json.dumps(plan))

    def test_plan_injection_warns_and_keeps_first_on_env_collision(self):
        # Two port tongs whose names sanitize to the same env prefix.
        a, b = def_of(PORT_TONG), def_of(PORT_TONG)
        b["interface"]["port"] = 6543
        merged = {
            "pg-main": {"source": tongs.REPO, "definition": a},
            "pg.main": {"source": tongs.REPO, "definition": b},
        }
        plan = tongs.plan_injection(merged, "claude")
        # First by sorted tong name ("pg-main") wins its port.
        self.assertEqual(plan["env"]["SWARMFORGE_TONG_PG_MAIN_PORT"], "5432")

    def test_mcp_url_rejects_non_http_transport(self):
        defn = def_of(GITHUB_TONG)
        defn["interface"]["transport"] = "stdio"
        with self.assertRaises(ValueError):
            tongs.mcp_url(defn, "github")


class AliasCollisionTests(unittest.TestCase):
    def _m(self, **defs):
        return {n: {"source": tongs.REPO, "definition": d} for n, d in defs.items()}

    def test_detects_shared_alias(self):
        merged = self._m(
            a={"interface": {"kind": "mcp", "name": "dup", "port": 1}},
            b={"interface": {"kind": "mcp", "name": "dup", "port": 2}},
            c={"interface": {"kind": "none"}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {"dup": ["a", "b"]})

    def test_mcp_name_can_collide_with_network_facing_tong_name(self):
        # canonical_alias is interface.name for mcp, else the tong name.
        merged = self._m(
            github={"interface": {"kind": "port", "port": 2}},
            creds={"interface": {"kind": "mcp", "name": "github", "port": 1}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {"github": ["creds", "github"]})

    def test_detects_collision_between_two_extra_aliases(self):
        # A contested name is a collision wherever it is declared, so extras are
        # folded in alongside canonical aliases.
        merged = self._m(
            a={"interface": {"kind": "port", "port": 1, "aliases": ["api"]}},
            b={"interface": {"kind": "port", "port": 2, "aliases": ["api"]}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {"api": ["a", "b"]})

    def test_detects_extra_alias_colliding_with_a_canonical_alias(self):
        merged = self._m(
            api={"interface": {"kind": "port", "port": 1}},
            b={"interface": {"kind": "port", "port": 2, "aliases": ["api"]}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {"api": ["api", "b"]})

    def test_distinct_extra_aliases_do_not_collide(self):
        merged = self._m(
            a={"interface": {"kind": "port", "port": 1, "aliases": ["api", "console"]}},
            b={"interface": {"kind": "port", "port": 2, "aliases": ["docs"]}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {})

    def test_non_network_facing_tongs_do_not_claim_aliases(self):
        merged = self._m(
            github={"interface": {"kind": "none"}},
            cache={"interface": {"kind": "volume", "volume": "cache", "mountpoint": "/cache"}},
            creds={"interface": {"kind": "mcp", "name": "github", "port": 1}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {})

    def test_empty_when_unique(self):
        merged = self._m(
            a={"interface": {"kind": "none"}},
            b={"interface": {"kind": "port", "port": 1}},
        )
        self.assertEqual(tongs.alias_collisions(merged), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
