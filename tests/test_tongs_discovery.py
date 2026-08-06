#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.discovery. Run: python3 tests/test_tongs_discovery.py"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Discovery and `python3 tests/<file>.py` both put this directory on the path,
# but `python3 -m unittest tests.<module>` does not; the sibling fixture module
# has to import under all three.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from swarmforge import tongs

from tongs_fixtures import GITHUB_TONG, def_of


# A comment in every position a definition puts one: whole lines above a key and
# above/between list items, and trailing a key and a value.
COMMENTED_TONG = """\
description: Exposes the project's build steps as MCP tools
# `session`, not `shared`: each tool needs this session's workspace host path.
lifecycle: session
image: ghcr.io/example/build-tong@sha256:ab#cd  # the `#` here is part of the digest
mounts:                       # opt-in magic words only
  # each tool drives the host docker daemon
  - docker-socket
  # the checkout the tools build from
  - workspace:ro              # read-only: the tools build, they do not edit
interface:
  kind: mcp
  transport: http
  port: 8080                  # the port inside the container
  name: build-tools
"""


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

    def test_comments_do_not_change_the_definition(self):
        defn = def_of(COMMENTED_TONG)
        self.assertEqual(defn["lifecycle"], "session")
        self.assertEqual(defn["mounts"], ["docker-socket", "workspace:ro"])
        self.assertEqual(
            defn["interface"],
            {"kind": "mcp", "transport": "http", "port": 8080, "name": "build-tools"},
        )
        # The digest keeps its `#`; the comment beside it goes.
        self.assertEqual(defn["image"], "ghcr.io/example/build-tong@sha256:ab#cd")
        # Every commented value still lands on the type the schema expects.
        self.assertEqual(tongs.validate_tong("build-tools", defn), [])

    def test_commented_secret_ref_still_resolves(self):
        # Unquoted and dense with punctuation -- the value most easily mis-cut.
        defn = def_of("env:\n  TOKEN: ${secret:op:op://Work/gh/token}  # rotated monthly\n")
        self.assertEqual(defn["env"]["TOKEN"], "${secret:op:op://Work/gh/token}")
        plain, secret = tongs.partition_secret_env(defn["env"])
        self.assertEqual(plain, {})
        self.assertEqual(sorted(secret), ["TOKEN"])

    def test_comments_leave_no_residue_in_values_handed_to_docker(self):
        # The schema constrains none of these by shape, so a comment left in one
        # reaches the container instead of being caught by validation.
        defn = def_of(
            "entrypoint:\n"
            "  - /bin/sh                # the secret wrapper needs a shell\n"
            "  - -c\n"
            "command:\n"
            "  - exec build-tong --port 8080  # don't background it\n"
            "env:\n"
            "  LOG_LEVEL: info          # plain value, passes through as -e\n"
            "  GREETING: don't panic    # an apostrophe is not a quote\n"
        )
        self.assertEqual(defn["entrypoint"], ["/bin/sh", "-c"])
        self.assertEqual(defn["command"], ["exec build-tong --port 8080"])
        self.assertEqual(defn["env"], {"LOG_LEVEL": "info", "GREETING": "don't panic"})


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

    def test_commented_definition_is_discovered_not_skipped(self):
        # A parse error here is only warned about and the tong vanishes, so the
        # failure this guards against is silent.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "build-tools.yaml"), "w") as f:
                f.write(COMMENTED_TONG)
            loaded = tongs.load_tong_dir(tmp)
            self.assertEqual(sorted(loaded), ["build-tools"])
            self.assertEqual(loaded["build-tools"]["mounts"], ["docker-socket", "workspace:ro"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
