#!/usr/bin/env python3
"""Unit tests for scripts/tongs.py. Run: python3 scripts/test_tongs.py"""

import importlib.util
import json
import os
import tempfile
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tongs.py")
spec = importlib.util.spec_from_file_location("tongs", MODULE_PATH)
tongs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tongs)


GITHUB_TONG = """\
description: Holds GitHub credentials, exposes push/PR operations as MCP
lifecycle: session
image: ghcr.io/crypticswarm/github-tong@sha256:abc123
env:
  GITHUB_TOKEN: ${secret:op:op://Work/github/token}
interface:
  kind: mcp
  transport: http
  port: 8080
  name: github
mounts:
  - workspace:ro
networks:
  - some-existing-net
"""


def def_of(text):
    return tongs.load_yaml(text)


class YamlLoadTests(unittest.TestCase):
    def test_nested_maps_lists_and_secret_value(self):
        defn = def_of(GITHUB_TONG)
        self.assertEqual(defn["lifecycle"], "session")
        self.assertEqual(defn["image"], "ghcr.io/crypticswarm/github-tong@sha256:abc123")
        # The secret reference and its inner colons survive parsing intact.
        self.assertEqual(defn["env"]["GITHUB_TOKEN"], "${secret:op:op://Work/github/token}")
        self.assertEqual(defn["interface"], {"kind": "mcp", "transport": "http", "port": 8080, "name": "github"})
        self.assertEqual(defn["mounts"], ["workspace:ro"])
        self.assertEqual(defn["networks"], ["some-existing-net"])

    def test_empty_document(self):
        self.assertEqual(def_of(""), {})

    def test_flow_list_readiness_command(self):
        defn = def_of('readiness:\n  mode: healthcheck\n  command: ["test", "-S", "/run/agent.sock"]\n')
        self.assertEqual(defn["readiness"]["command"], ["test", "-S", "/run/agent.sock"])


class DiscoveryTests(unittest.TestCase):
    def test_missing_dir_is_empty(self):
        self.assertEqual(tongs.load_tong_dir("/nonexistent/path"), {})
        self.assertEqual(tongs.load_tong_dir(""), {})

    def test_reads_yaml_and_yml_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "github.yaml"), "w") as f:
                f.write(GITHUB_TONG)
            with open(os.path.join(tmp, "ollama.yml"), "w") as f:
                f.write("lifecycle: shared\nimage: ollama/ollama\n")
            with open(os.path.join(tmp, "notes.txt"), "w") as f:
                f.write("ignore me\n")
            loaded = tongs.load_tong_dir(tmp)
            self.assertEqual(sorted(loaded), ["github", "ollama"])
            self.assertEqual(loaded["ollama"]["lifecycle"], "shared")

    def test_discover_returns_layer_mappings_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.yaml"), "w") as f:
                f.write("lifecycle: session\nimage: x\n")
            layers = tongs.discover([(tongs.USER, tmp), (tongs.WORKSPACE, "/missing")])
            self.assertEqual(layers[0][0], tongs.USER)
            self.assertEqual(sorted(layers[0][1]), ["a"])
            self.assertEqual(layers[1], (tongs.WORKSPACE, {}))


class MergeTests(unittest.TestCase):
    def test_empty_discovery_is_inert(self):
        # The foundation of the passthrough invariant: nothing discovered -> {}.
        self.assertEqual(tongs.merge_tongs([]), {})
        self.assertEqual(tongs.merge_tongs([(tongs.USER, {}), (tongs.WORKSPACE, {})]), {})

    def test_higher_layer_replaces_wholesale_and_records_source(self):
        layers = [
            (tongs.USER, {"t": {"image": "old", "lifecycle": "session", "extra": 1}}),
            (tongs.ORG, {"t": {"image": "new", "lifecycle": "shared"}}),
        ]
        merged = tongs.merge_tongs(layers)
        self.assertEqual(merged["t"]["source"], tongs.ORG)
        self.assertEqual(merged["t"]["definition"], {"image": "new", "lifecycle": "shared"})
        # Wholesale replacement: the lower layer's "extra" key does not survive.
        self.assertNotIn("extra", merged["t"]["definition"])

    def test_disable_removes_inherited_tong(self):
        layers = [
            (tongs.USER, {"t": {"image": "x", "lifecycle": "session"}}),
            (tongs.WORKSPACE, {"t": {"disable": True}}),
        ]
        self.assertEqual(tongs.merge_tongs(layers), {})

    def test_workspace_cannot_redefine_trusted_tong(self):
        layers = [
            (tongs.REPO, {"gh": {"image": "trusted", "lifecycle": "session"}}),
            (tongs.WORKSPACE, {"gh": {"image": "evil", "lifecycle": "session"}}),
        ]
        merged = tongs.merge_tongs(layers)
        self.assertEqual(merged["gh"]["source"], tongs.REPO)
        self.assertEqual(merged["gh"]["definition"]["image"], "trusted")

    def test_workspace_may_disable_trusted_tong(self):
        layers = [
            (tongs.REPO, {"gh": {"image": "trusted", "lifecycle": "session"}}),
            (tongs.WORKSPACE, {"gh": {"disable": True}}),
        ]
        self.assertEqual(tongs.merge_tongs(layers), {})

    def test_workspace_only_tong_is_workspace_sourced(self):
        layers = [(tongs.WORKSPACE, {"pg": {"image": "postgres", "lifecycle": "session"}})]
        merged = tongs.merge_tongs(layers)
        self.assertTrue(tongs.is_workspace_sourced(merged["pg"]["source"]))

    def test_middle_layer_disable_then_higher_redefine(self):
        # A higher layer re-adding overrides a lower layer's disable (precedence).
        layers = [
            (tongs.USER, {"t": {"image": "a", "lifecycle": "session"}}),
            (tongs.ORG, {"t": {"disable": True}}),
            (tongs.REPO, {"t": {"image": "c", "lifecycle": "session"}}),
        ]
        merged = tongs.merge_tongs(layers)
        self.assertEqual(merged["t"]["source"], tongs.REPO)
        self.assertEqual(merged["t"]["definition"]["image"], "c")

    def test_middle_layer_disable_with_no_higher_redefine_removes(self):
        layers = [
            (tongs.USER, {"t": {"image": "a", "lifecycle": "session"}}),
            (tongs.ORG, {"t": {"disable": True}}),
        ]
        self.assertEqual(tongs.merge_tongs(layers), {})


class ValidationTests(unittest.TestCase):
    def test_valid_mcp_tong(self):
        self.assertEqual(tongs.validate_tong("github", def_of(GITHUB_TONG)), [])

    def test_missing_lifecycle_and_image(self):
        errors = tongs.validate_tong("t", {"interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        joined = " ".join(errors)
        self.assertIn("lifecycle", joined)
        self.assertIn("image", joined)

    def test_bad_lifecycle(self):
        errors = tongs.validate_tong("t", {"lifecycle": "forever", "image": "x", "interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        self.assertTrue(any("lifecycle" in e for e in errors))

    def test_mcp_requires_port_and_name(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "mcp"}})
        joined = " ".join(errors)
        self.assertIn("port", joined)
        self.assertIn("name", joined)

    def test_mcp_rejects_non_http_transport(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "mcp", "port": 80, "name": "n", "transport": "stdio"}})
        self.assertTrue(any("transport" in e for e in errors))

    def test_port_requires_port(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "port"}})
        self.assertTrue(any("port" in e for e in errors))

    def test_volume_requires_volume_mountpoint_and_readiness_mode(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "volume"}})
        joined = " ".join(errors)
        self.assertIn("volume", joined)
        self.assertIn("mountpoint", joined)
        self.assertIn("readiness", joined)

    def test_valid_volume_tong(self):
        ok = tongs.validate_tong("cache", {
            "lifecycle": "session",
            "image": "x",
            "interface": {"kind": "volume", "volume": "build-cache", "mountpoint": "/cache"},
            "readiness": {"mode": "healthcheck", "command": ["test", "-d", "/cache"]},
        })
        self.assertEqual(ok, [])

    def test_none_requires_explicit_readiness_mode(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "none"}})
        self.assertTrue(any("readiness" in e for e in errors))
        # ...and is satisfied once a mode is declared.
        ok = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "none"}, "readiness": {"mode": "none"}})
        self.assertEqual(ok, [])

    def test_bad_interface_kind(self):
        errors = tongs.validate_tong("t", {"lifecycle": "session", "image": "x", "interface": {"kind": "socket"}})
        self.assertTrue(any("interface.kind" in e for e in errors))


class SecretRefTests(unittest.TestCase):
    def test_parse_single_ref_with_inner_colons(self):
        self.assertEqual(tongs.parse_secret_ref("${secret:op:op://Work/github/token}"), ("op", "op://Work/github/token"))

    def test_parse_rejects_non_ref(self):
        self.assertIsNone(tongs.parse_secret_ref("plain"))
        self.assertIsNone(tongs.parse_secret_ref("prefix ${secret:op:x}"))

    def test_find_refs_walks_nested_and_dedups(self):
        defn = def_of(GITHUB_TONG)
        defn["env"]["SECOND"] = "${secret:pass:db/pw}"
        defn["env"]["DUP"] = "${secret:op:op://Work/github/token}"
        refs = tongs.find_secret_refs(defn)
        self.assertIn(("op", "op://Work/github/token"), refs)
        self.assertIn(("pass", "db/pw"), refs)
        self.assertEqual(len(refs), 2)  # the duplicate op ref collapses

    def test_multiple_refs_in_one_string(self):
        # Two adjacent refs in a single value: both found and both substituted.
        value = "${secret:op:a}::${secret:pass:b}"
        refs = tongs.find_secret_refs(value)
        self.assertEqual(refs, [("op", "a"), ("pass", "b")])
        out = tongs.substitute_secrets(value, lambda p, r: "<%s>" % r)
        self.assertEqual(out, "<a>::<b>")

    def test_empty_ref_does_not_match(self):
        self.assertEqual(tongs.find_secret_refs("${secret:op:}"), [])
        self.assertIsNone(tongs.parse_secret_ref("${secret:op:}"))

    def test_substitute_uses_injected_resolver(self):
        defn = {"env": {"A": "tok=${secret:op:a}", "B": "${secret:pass:b}"}, "image": "x"}
        out = tongs.substitute_secrets(defn, lambda p, r: "<%s:%s>" % (p, r))
        self.assertEqual(out["env"]["A"], "tok=<op:a>")
        self.assertEqual(out["env"]["B"], "<pass:b>")
        self.assertEqual(out["image"], "x")  # untouched
        self.assertIn("${secret", defn["env"]["A"])  # original not mutated


class EnvNamingTests(unittest.TestCase):
    def test_prefix_sanitizes_name(self):
        self.assertEqual(tongs.tong_env_prefix("github-creds"), "SWARMFORGE_TONG_GITHUB_CREDS")
        self.assertEqual(tongs.tong_env_prefix("pg.test_01"), "SWARMFORGE_TONG_PG_TEST_01")

    def test_var_appends_suffix(self):
        self.assertEqual(tongs.tong_env_var("pg", "host"), "SWARMFORGE_TONG_PG_HOST")
        self.assertEqual(tongs.tong_env_var("pg", "PORT"), "SWARMFORGE_TONG_PG_PORT")


class ConfigHashTests(unittest.TestCase):
    def test_order_independent_and_stable(self):
        a = tongs.config_hash({"image": "x", "lifecycle": "session"})
        b = tongs.config_hash({"lifecycle": "session", "image": "x"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)  # sha256 hex

    def test_changes_when_definition_changes(self):
        base = def_of(GITHUB_TONG)
        changed = def_of(GITHUB_TONG)
        changed["image"] = "ghcr.io/crypticswarm/github-tong@sha256:DIFFERENT"
        self.assertNotEqual(tongs.config_hash(base), tongs.config_hash(changed))


class PrivilegeSummaryTests(unittest.TestCase):
    def test_summary_reports_requested_privileges(self):
        defn = def_of(GITHUB_TONG)
        defn["mounts"] = ["workspace:ro", "docker-socket"]
        summary = tongs.privilege_summary(defn)
        self.assertEqual(summary["image"], defn["image"])
        self.assertEqual(summary["secrets"], [{"provider": "op", "ref": "op://Work/github/token"}])
        self.assertEqual(summary["networks"], ["some-existing-net"])
        self.assertTrue(summary["socket"])

    def test_no_socket_without_mount(self):
        self.assertFalse(tongs.privilege_summary(def_of(GITHUB_TONG))["socket"])


class ApprovalKeyingTests(unittest.TestCase):
    def setUp(self):
        self.ws = "/home/me/project"
        self.defn = def_of(GITHUB_TONG)

    def test_unapproved_then_recorded(self):
        approvals = {}
        self.assertFalse(tongs.is_approved(approvals, self.ws, "github", self.defn))
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        self.assertTrue(tongs.is_approved(approvals, self.ws, "github", self.defn))

    def test_definition_change_reprompts(self):
        approvals = {}
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        changed = def_of(GITHUB_TONG)
        changed["image"] = "ghcr.io/crypticswarm/github-tong@sha256:MOVED"
        self.assertFalse(tongs.is_approved(approvals, self.ws, "github", changed))

    def test_keyed_by_workspace_path(self):
        approvals = {}
        tongs.record_approval(approvals, self.ws, "github", self.defn)
        self.assertFalse(tongs.is_approved(approvals, "/other/ws", "github", self.defn))

    def test_load_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "approvals.json")
            approvals = tongs.record_approval({}, self.ws, "github", self.defn)
            tongs.save_approvals(path, approvals)
            self.assertTrue(tongs.is_approved(tongs.load_approvals(path), self.ws, "github", self.defn))

    def test_is_approved_tolerates_malformed_store(self):
        # A hand-edited store with a non-dict workspace value must not crash.
        self.assertFalse(tongs.is_approved({self.ws: "junk"}, self.ws, "github", self.defn))

    def test_load_missing_or_corrupt_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tongs.load_approvals(os.path.join(tmp, "nope.json")), {})
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as f:
                f.write("{not json")
            self.assertEqual(tongs.load_approvals(bad), {})


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
