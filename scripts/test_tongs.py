#!/usr/bin/env python3
"""Unit tests for scripts/tongs.py. Run: python3 scripts/test_tongs.py"""

import importlib.util
import json
import os
import subprocess
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

    def test_tcp_readiness_rejected_for_portless_kind(self):
        # A volume/none tong has no port, so a tcp probe could never succeed;
        # validation must reject the combination rather than time out at runtime.
        errors = tongs.validate_tong("t", {
            "lifecycle": "session", "image": "x",
            "interface": {"kind": "none"}, "readiness": {"mode": "tcp"},
        })
        self.assertTrue(any("tcp" in e for e in errors))

    def test_tcp_readiness_allowed_for_port_kind(self):
        self.assertEqual(
            tongs.validate_tong("t", def_of(PORT_TONG)), []
        )

    def _base(self, **extra):
        defn = {"lifecycle": "session", "image": "x",
                "interface": {"kind": "none"}, "readiness": {"mode": "none"}}
        defn.update(extra)
        return defn

    def test_bad_readiness_timeout_rejected(self):
        defn = self._base(interface={"kind": "port", "port": 5432},
                          readiness={"mode": "tcp", "timeout": "soon"})
        errors = tongs.validate_tong("t", defn)
        self.assertTrue(any("timeout" in e for e in errors))

    def test_bad_readiness_command_rejected(self):
        defn = self._base(readiness={"mode": "healthcheck", "command": "test -d /x"})
        errors = tongs.validate_tong("t", defn)
        self.assertTrue(any("command" in e for e in errors))

    def test_unknown_mount_word_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=["/etc/passwd:/etc/passwd"]))
        self.assertTrue(any("mount" in e for e in errors))

    def test_non_string_mount_rejected(self):
        errors = tongs.validate_tong("t", self._base(mounts=[123]))
        self.assertTrue(any("mount" in e for e in errors))

    def test_known_mounts_accepted(self):
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:ro", "docker-socket"])), [])

    def test_rw_mount_mode_accepted(self):
        self.assertEqual(tongs.validate_tong("t", self._base(mounts=["workspace:rw"])), [])

    def test_target_path_mount_rejected(self):
        # `workspace:/target` is broker-config only; as a tong mount the launcher
        # would forward it as a bogus docker mode.
        errors = tongs.validate_tong("t", self._base(mounts=["workspace:/work:ro"]))
        self.assertTrue(any("invalid mode" in e for e in errors))

    def test_non_string_network_rejected(self):
        errors = tongs.validate_tong("t", self._base(networks=[{"name": "x"}]))
        self.assertTrue(any("network" in e for e in errors))

    def test_resources_must_be_mapping(self):
        errors = tongs.validate_tong("t", self._base(resources="512m"))
        self.assertTrue(any("resources" in e for e in errors))

    def test_resources_memory_type_checked(self):
        errors = tongs.validate_tong("t", self._base(resources={"memory": ["512m"]}))
        self.assertTrue(any("memory" in e for e in errors))
        self.assertEqual(tongs.validate_tong("t", self._base(resources={"memory": "512m"})), [])

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

    def test_rejects_invalid_secret_env_name(self):
        errors = tongs.validate_tong("t", {
            "lifecycle": "session",
            "image": "x",
            "interface": {"kind": "none"},
            "readiness": {"mode": "none"},
            "env": {"a/b": "${secret:op:t}"},
        })
        self.assertTrue(any("a/b" in e for e in errors))

    def test_rejects_non_list_entrypoint_or_command(self):
        for field in ("entrypoint", "command"):
            errors = tongs.validate_tong("t", {
                "lifecycle": "session", "image": "x",
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
                field: "node server.js",  # must be a list of strings
            })
            self.assertTrue(any(field in e for e in errors), field)

    def test_accepts_list_entrypoint_and_command(self):
        errors = tongs.validate_tong("t", {
            "lifecycle": "session", "image": "x",
            "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            "entrypoint": ["node"], "command": ["server.js"],
        })
        self.assertEqual(errors, [])


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


PROVIDERS_YAML = """\
providers:
  op: ["op", "read", "{ref}"]
  pass: ["pass", "show", "{ref}"]
"""


class SecretProviderTests(unittest.TestCase):
    def test_loads_provider_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secret-providers.yaml")
            with open(path, "w") as f:
                f.write(PROVIDERS_YAML)
            providers = tongs.load_secret_providers(path)
            self.assertEqual(
                providers,
                {"op": ["op", "read", "{ref}"], "pass": ["pass", "show", "{ref}"]},
            )

    def test_missing_file_yields_empty(self):
        self.assertEqual(tongs.load_secret_providers("/no/such/file.yaml"), {})
        self.assertEqual(tongs.load_secret_providers(""), {})

    def test_file_without_providers_block_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write("unrelated: true\n")
            self.assertEqual(tongs.load_secret_providers(path), {})

    def test_non_mapping_providers_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write("providers: nope\n")
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_non_list_command_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write('providers:\n  op: "op read {ref}"\n')
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_command_substitutes_ref_in_every_element(self):
        providers = {"op": ["op", "read", "{ref}", "--prefix={ref}"]}
        self.assertEqual(
            tongs.secret_provider_command(providers, "op", "op://Work/x"),
            ["op", "read", "op://Work/x", "--prefix=op://Work/x"],
        )

    def test_command_unknown_provider_raises_keyerror(self):
        with self.assertRaises(KeyError):
            tongs.secret_provider_command({"op": ["op"]}, "vault", "x")

    def test_loads_structured_provider_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secret-providers.yaml")
            with open(path, "w") as f:
                f.write(
                    "providers:\n"
                    "  op: [\"op\", \"read\", \"{ref}\"]\n"
                    "  shared:\n"
                    "    default: [\"pass\", \"show\", \"{ref}\"]\n"
                    "    overrides:\n"
                    "      ci-token: [\"doppler\", \"secrets\", \"get\", \"CI\", \"--plain\"]\n"
                )
            self.assertEqual(
                tongs.load_secret_providers(path),
                {
                    "op": ["op", "read", "{ref}"],
                    "shared": {
                        "default": ["pass", "show", "{ref}"],
                        "overrides": {
                            "ci-token": ["doppler", "secrets", "get", "CI", "--plain"],
                        },
                    },
                },
            )

    def test_loads_overrides_only_entry(self):
        # `default` is optional: overrides alone is valid, with a `None` default.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write(
                    "providers:\n"
                    "  shared:\n"
                    "    overrides:\n"
                    "      tok: [\"op\", \"read\", \"{ref}\"]\n"
                )
            self.assertEqual(
                tongs.load_secret_providers(path),
                {"shared": {"default": None, "overrides": {"tok": ["op", "read", "{ref}"]}}},
            )

    def test_unknown_provider_key_raises(self):
        # A typo at the provider level (not `default`/`overrides`) fails loudly.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write('providers:\n  shared:\n    ci-token: ["op", "read", "{ref}"]\n')
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_entry_without_default_or_overrides_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write("providers:\n  shared: {}\n")
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_non_mapping_overrides_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write("providers:\n  shared:\n    overrides: nope\n")
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_non_list_override_command_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.yaml")
            with open(path, "w") as f:
                f.write('providers:\n  shared:\n    overrides:\n      ci: "doppler get CI"\n')
            with self.assertRaises(ValueError):
                tongs.load_secret_providers(path)

    def test_command_resolves_override_ref(self):
        providers = {
            "shared": {
                "default": ["pass", "show", "{ref}"],
                "overrides": {"ci-token": ["doppler", "secrets", "get", "CI", "--plain"]},
            }
        }
        self.assertEqual(
            tongs.secret_provider_command(providers, "shared", "ci-token"),
            ["doppler", "secrets", "get", "CI", "--plain"],
        )

    def test_command_falls_back_to_default(self):
        providers = {"shared": {"default": ["pass", "show", "{ref}"], "overrides": {}}}
        self.assertEqual(
            tongs.secret_provider_command(providers, "shared", "github/token"),
            ["pass", "show", "github/token"],
        )

    def test_secret_named_default_is_distinct_from_fallback(self):
        # A secret literally named "default" lives under overrides and is served
        # by its own command, never conflated with the sibling `default` fallback.
        providers = {
            "shared": {
                "default": ["pass", "show", "{ref}"],
                "overrides": {"default": ["op", "read", "{ref}"]},
            }
        }
        self.assertEqual(
            tongs.secret_provider_command(providers, "shared", "default"),
            ["op", "read", "default"],
        )

    def test_command_unmapped_ref_without_default_raises(self):
        providers = {"shared": {"default": None, "overrides": {"ci-token": ["doppler", "get", "CI"]}}}
        with self.assertRaises(tongs.UnmappedSecretError) as caught:
            tongs.secret_provider_command(providers, "shared", "github/token")
        self.assertEqual(caught.exception.provider, "shared")
        self.assertEqual(caught.exception.ref, "github/token")


class SecretDeliveryTests(unittest.TestCase):
    def test_partition_splits_plain_from_secret_bearing_env(self):
        env = {
            "PLAIN": "value",
            "TOKEN": "${secret:op:op://Work/github/token}",
            "MIXED": "Bearer ${secret:pass:db/pw}",
        }
        plain, secret = tongs.partition_secret_env(env)
        self.assertEqual(plain, {"PLAIN": "value"})
        self.assertEqual(
            secret,
            {"TOKEN": "${secret:op:op://Work/github/token}", "MIXED": "Bearer ${secret:pass:db/pw}"},
        )

    def test_partition_empty_env(self):
        self.assertEqual(tongs.partition_secret_env(None), ({}, {}))
        self.assertEqual(tongs.partition_secret_env({}), ({}, {}))

    def test_plan_tong_secrets_keeps_secret_values_out_of_plain_env(self):
        env = {"REGION": "us", "TOKEN": "${secret:op:op://Work/github/token}"}
        plan = tongs.plan_tong_secrets(env, lambda p, r: "RESOLVED-%s" % r)
        # Plain env passes through; the resolved secret lands only under `secrets`.
        self.assertEqual(plan["env"], {"REGION": "us"})
        self.assertEqual(plan["secrets"], {"TOKEN": "RESOLVED-op://Work/github/token"})
        self.assertNotIn("RESOLVED-op://Work/github/token", json.dumps(plan["env"]))

    def test_plan_tong_secrets_inert_without_secrets(self):
        plan = tongs.plan_tong_secrets({"REGION": "us"}, lambda p, r: "x")
        self.assertEqual(plan, {"env": {"REGION": "us"}, "secrets": {}})

    def test_plan_tong_secrets_resolves_each_provider_with_its_ref(self):
        env = {"A": "${secret:op:a}", "B": "${secret:pass:b}"}
        seen = []
        tongs.plan_tong_secrets(env, lambda p, r: seen.append((p, r)) or "v")
        self.assertEqual(sorted(seen), [("op", "a"), ("pass", "b")])

    def test_render_secret_exports_quotes_values_safely(self):
        # Each value is single-quoted with embedded quotes escaped, so an arbitrary
        # value -- here one with a quote, a space, and a newline -- cannot break out
        # of its assignment when the wrapper evals the script.
        script = tongs.render_secret_exports({"B": "two\nlines", "A": "it's a $X"})
        # Sorted by name; A first.
        self.assertEqual(
            script,
            "export A='it'\\''s a $X'\n" "export B='two\nlines'\n",
        )

    def test_render_secret_exports_eval_round_trips_the_value(self):
        # Sanity-check that evaling the rendered script in a real shell reproduces
        # the exact bytes, proving the quoting survives metacharacters.
        value = "a'b\"c $d `e` \\f\n g"
        script = tongs.render_secret_exports({"V": value})
        out = subprocess.run(
            ["/bin/sh", "-c", 'eval "$1"; printf %s "$V"', "sh", script],
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        self.assertEqual(out, value)

    def test_render_secret_exports_rejects_invalid_name(self):
        with self.assertRaises(ValueError):
            tongs.render_secret_exports({"a/b": "v"})

    def test_secret_inject_argv_reads_fifo_then_execs_target(self):
        entrypoint, command = tongs.secret_inject_argv(["node", "server.js"])
        self.assertEqual(entrypoint, "/bin/sh")
        self.assertEqual(command[0], "-c")
        self.assertIn("/run/swarmforge/secret-env", command[1])
        self.assertIn("|| exit 1", command[1])
        self.assertIn('exec "$@"', command[1])
        # The target argv is passed after the `$0` placeholder so `"$@"` is it.
        self.assertEqual(command[2:], ["swarmforge-tong", "node", "server.js"])

    def test_secret_inject_argv_does_not_exec_target_when_fifo_read_fails(self):
        old_target = tongs.SECRET_FIFO_TARGET
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongs.SECRET_FIFO_TARGET = os.path.join(tmp, "missing")
                entrypoint, command = tongs.secret_inject_argv(
                    ["/bin/sh", "-c", "printf target-ran"]
                )
                completed = subprocess.run(
                    [entrypoint] + command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        finally:
            tongs.SECRET_FIFO_TARGET = old_target
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")

    def test_resolve_exec_target_uses_image_defaults(self):
        self.assertEqual(
            tongs.resolve_exec_target({"image": "x"}, ["node"], ["server.js"]),
            ["node", "server.js"],
        )

    def test_resolve_exec_target_definition_overrides_image(self):
        defn = {"image": "x", "entrypoint": ["tini", "--"], "command": ["app"]}
        self.assertEqual(
            tongs.resolve_exec_target(defn, ["node"], ["server.js"]),
            ["tini", "--", "app"],
        )

    def test_resolve_exec_target_empty_raises(self):
        with self.assertRaises(ValueError):
            tongs.resolve_exec_target({"image": "x"}, [], [])


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


def _merged(name, text):
    """Wrap a single definition the way merge_tongs returns it."""
    return {name: {"source": tongs.WORKSPACE, "definition": def_of(text)}}


PORT_TONG = """\
lifecycle: session
image: postgres:16
interface:
  kind: port
  port: 5432
  protocol: postgres
readiness:
  mode: tcp
"""

VOLUME_TONG = """\
lifecycle: session
image: cache-builder
interface:
  kind: volume
  volume: build-cache
  mountpoint: /cache
readiness:
  mode: healthcheck
  command: ["test", "-d", "/cache"]
"""

NONE_TONG = """\
lifecycle: session
image: log-shipper
interface:
  kind: none
readiness:
  mode: none
"""

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


class InterfaceWiringTests(unittest.TestCase):
    def test_canonical_alias_mcp_uses_interface_name(self):
        # The tong's own name (github-creds) differs from the MCP server name.
        defn = def_of(GITHUB_TONG)
        self.assertEqual(tongs.canonical_alias("github-creds", defn), "github")

    def test_canonical_alias_non_mcp_uses_tong_name(self):
        self.assertEqual(tongs.canonical_alias("pg", def_of(PORT_TONG)), "pg")
        self.assertEqual(tongs.canonical_alias("cache", def_of(VOLUME_TONG)), "cache")
        self.assertEqual(tongs.canonical_alias("watcher", def_of(NONE_TONG)), "watcher")

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
        self.assertEqual(plan["session_aliases"], [("github-creds", "github")])

    def test_shared_tong_connected_per_session_when_network_exists(self):
        # A session tong forces a per-session network; the shared tong is then
        # connected to it under its canonical alias.
        merged = {
            "github-creds": {"source": tongs.REPO, "definition": def_of(GITHUB_TONG)},
            "ollama": {"source": tongs.REPO, "definition": def_of(SHARED_PORT_TONG)},
        }
        plan = tongs.plan_network(merged, "opencode-net", "wt")
        self.assertEqual(plan["session_aliases"], [("github-creds", "github")])
        self.assertEqual(plan["shared_connect"], [("ollama", "ollama")])

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
        self.assertEqual(plan["session_aliases"], [("pg", "pg")])

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
        self.assertEqual(plan["session_aliases"], [("a-creds", "github")])
        # The shared tong loses the alias and is not connected.
        self.assertEqual(plan["shared_connect"], [])


ANVIL_ARGV = [
    "docker", "run", "-it", "--rm", "--name", "claude-proj",
    "--network", "opencode-net",
    "-e", "TZ=Etc/UTC",
    "-v", "/home/me/proj:/workspace",
    "claude-code:local",
    "--harness-arg",
]


class DockerArgvTests(unittest.TestCase):
    def test_shared_container_name_sanitizes_and_prefixes(self):
        self.assertEqual(tongs.shared_container_name("ollama"), "swarmforge-shared-ollama")
        self.assertEqual(tongs.shared_container_name("my tong/x"), "swarmforge-shared-my-tong-x")

    def test_shared_container_name_scope_partitions_identical_names(self):
        # Two orgs shipping the same tong name get distinct container names so
        # they never collide on one daemon-global name (the teardown bug).
        a = tongs.shared_container_name("asana", scope="acme-1a2b3c4d")
        b = tongs.shared_container_name("asana", scope="globex-9f8e7d6c")
        self.assertEqual(a, "swarmforge-shared-acme-1a2b3c4d-asana")
        self.assertEqual(b, "swarmforge-shared-globex-9f8e7d6c-asana")
        self.assertNotEqual(a, b)
        # No scope is byte-identical to the unscoped name (today's behavior).
        self.assertEqual(
            tongs.shared_container_name("asana"), "swarmforge-shared-asana"
        )

    def test_shared_network_name_is_scope_prefixed(self):
        self.assertEqual(
            tongs.shared_network_name("acme-1a2b3c4d"),
            "swarmforge-shared-net-acme-1a2b3c4d",
        )

    def test_org_scope_token_none_without_org_dir(self):
        self.assertIsNone(tongs.org_scope_token(None))
        self.assertIsNone(tongs.org_scope_token(""))

    def test_org_scope_token_stable_per_path_and_distinct_per_org(self):
        # Same org path (e.g. two repos under one org) => same token; different
        # orgs => different tokens. Path is normalized so trailing slashes and
        # `.`/`..` segments do not change identity.
        acme = tongs.org_scope_token("/home/me/orgs/acme/.swarmforge/tongs")
        acme_again = tongs.org_scope_token("/home/me/orgs/acme/.swarmforge/tongs/")
        acme_dotted = tongs.org_scope_token("/home/me/orgs/acme/./.swarmforge/tongs")
        globex = tongs.org_scope_token("/home/me/orgs/globex/.swarmforge/tongs")
        self.assertEqual(acme, acme_again)
        self.assertEqual(acme, acme_dotted)
        self.assertNotEqual(acme, globex)

    def test_org_scope_token_carries_readable_org_root_hint(self):
        # The org root (parent of `.swarmforge/`) is prefixed for `docker ps`.
        token = tongs.org_scope_token("/home/me/orgs/acme/.swarmforge/tongs")
        self.assertTrue(token.startswith("acme-"), token)

    def test_session_container_name_carries_session_and_sanitizes(self):
        self.assertEqual(tongs.session_container_name("claude-proj", "github"), "claude-proj-tong-github")
        self.assertEqual(tongs.session_container_name("claude-proj", "my tong/x"), "claude-proj-tong-my-tong-x")

    def test_session_container_name_empty_token_has_no_trailing_dash(self):
        # A name that sanitizes to empty must not yield a "<sess>-tong-" name that
        # would collide with another such tong; fall back like shared names do.
        self.assertEqual(tongs.session_container_name("sess", "@@@"), "sess-tong")

    def test_mount_specs_workspace_and_socket(self):
        defn = {"mounts": ["workspace:ro", "docker-socket"]}
        specs = tongs.tong_mount_specs(defn, "/ws")
        self.assertEqual(specs, ["/ws:/workspace:ro", "/var/run/docker.sock:/var/run/docker.sock"])

    def test_mount_specs_workspace_without_mode(self):
        self.assertEqual(tongs.tong_mount_specs({"mounts": ["workspace"]}, "/ws"), ["/ws:/workspace"])

    def test_mount_specs_socket_honors_custom_path(self):
        specs = tongs.tong_mount_specs({"mounts": ["docker-socket:ro"]}, "/ws", socket_path="/run/d.sock")
        self.assertEqual(specs, ["/run/d.sock:/run/d.sock:ro"])

    def test_mount_specs_no_mounts_is_empty(self):
        self.assertEqual(tongs.tong_mount_specs({}, "/ws"), [])

    def test_mount_specs_workspace_without_workspace_path_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": ["workspace"]}, "")

    def test_mount_specs_unknown_word_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": ["/etc/passwd:/etc/passwd"]}, "/ws")

    def test_mount_specs_non_string_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_mount_specs({"mounts": [123]}, "/ws")

    def test_resource_flags_memory(self):
        self.assertEqual(tongs.tong_resource_flags({"resources": {"memory": "512m"}}), ["--memory", "512m"])

    def test_resource_flags_absent_is_empty(self):
        self.assertEqual(tongs.tong_resource_flags({}), [])

    def test_resource_flags_ignores_unknown_keys(self):
        self.assertEqual(tongs.tong_resource_flags({"resources": {"cpus": 2}}), [])

    def test_resource_flags_non_mapping_raises(self):
        with self.assertRaises(ValueError):
            tongs.tong_resource_flags({"resources": "512m"})

    def test_run_argv_port_tong_full_shape(self):
        argv = tongs.tong_run_argv(
            "pg", def_of(PORT_TONG),
            container_name="ctr-pg", network="net", alias="pg",
            env={"PGDATA": "/data"}, label_hash="h0",
        )
        self.assertEqual(argv[:5], ["docker", "run", "-d", "--name", "ctr-pg"])
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "net")
        # port is network-facing => it gets an alias
        self.assertIn("--network-alias", argv)
        self.assertEqual(argv[argv.index("--network-alias") + 1], "pg")
        self.assertIn("swarmforge.tong.name=pg", argv)
        self.assertIn("swarmforge.tong.config-hash=h0", argv)
        self.assertIn("PGDATA=/data", argv)
        # image is last
        self.assertEqual(argv[-1], "postgres:16")

    def test_run_argv_non_network_facing_omits_alias(self):
        argv = tongs.tong_run_argv(
            "watcher", def_of(NONE_TONG),
            container_name="ctr", network="net", alias="watcher",
        )
        self.assertNotIn("--network-alias", argv)

    def test_run_argv_mounts_and_resources(self):
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["workspace:ro"]
        defn["resources"] = {"memory": "256m"}
        argv = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w", workspace="/ws",
        )
        self.assertIn("/ws:/workspace:ro", argv)
        self.assertIn("--memory", argv)
        self.assertEqual(argv[argv.index("--memory") + 1], "256m")

    def test_run_argv_secret_injection_mounts_fifo_wraps_entrypoint(self):
        # A secret-bearing tong gets the FIFO bind (read-only), the /bin/sh
        # entrypoint override, and the wrapper command appended after the image.
        entrypoint, command = tongs.secret_inject_argv(["node", "server.js"])
        argv = tongs.tong_run_argv(
            "g", def_of(NONE_TONG), container_name="c", network="n", alias="g",
            fifo_host_path="/tmp/sf/secret-env", entrypoint=entrypoint, command=command,
        )
        self.assertEqual(
            argv[argv.index("--entrypoint") + 1], "/bin/sh"
        )
        self.assertIn("/tmp/sf/secret-env:/run/swarmforge/secret-env:ro", argv)
        # The wrapper command trails the image (which is NONE_TONG's image).
        image = def_of(NONE_TONG)["image"]
        self.assertEqual(argv[argv.index(image) + 1 :], command)

    def test_run_argv_without_secrets_omits_entrypoint_and_fifo(self):
        argv = tongs.tong_run_argv(
            "g", def_of(NONE_TONG), container_name="c", network="n", alias="g",
        )
        self.assertNotIn("--entrypoint", argv)
        self.assertNotIn("secret-env", " ".join(argv))
        self.assertEqual(argv[-1], def_of(NONE_TONG)["image"])  # nothing after image

    def test_run_argv_without_secrets_applies_declared_command(self):
        # A secret-free tong's command: still overrides the image CMD (regression:
        # it used to be honored only on the secret-injection path).
        defn = def_of(PORT_TONG)
        defn["command"] = ["redis-server", "--port", "5002"]
        argv = tongs.tong_run_argv(
            "r", defn, container_name="c", network="n", alias="r",
        )
        image = defn["image"]
        self.assertEqual(argv[argv.index(image) + 1 :], ["redis-server", "--port", "5002"])
        self.assertNotIn("--entrypoint", argv)  # command: alone keeps the image entrypoint

    def test_run_argv_without_secrets_applies_declared_entrypoint(self):
        defn = def_of(NONE_TONG)
        defn["entrypoint"] = ["/bin/tini", "--"]
        defn["command"] = ["serve"]
        argv = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w",
        )
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "/bin/tini")
        image = defn["image"]
        self.assertEqual(argv[argv.index(image) + 1 :], ["--", "serve"])

    def test_declared_run_override_command_only(self):
        self.assertEqual(
            tongs.declared_run_override({"command": ["redis-server", "--port", "5002"]}),
            (None, ["redis-server", "--port", "5002"]),
        )

    def test_declared_run_override_entrypoint_leads_trailing_args(self):
        self.assertEqual(
            tongs.declared_run_override({"entrypoint": ["/bin/tini", "--"], "command": ["serve"]}),
            ("/bin/tini", ["--", "serve"]),
        )

    def test_declared_run_override_none_leaves_image_defaults(self):
        self.assertEqual(tongs.declared_run_override({}), (None, []))

    def test_run_argv_does_not_emit_empty_hash_label(self):
        argv = tongs.tong_run_argv("g", def_of(NONE_TONG), container_name="c", network="n", alias="g")
        self.assertNotIn("swarmforge.tong.config-hash=", " ".join(argv))

    def test_run_argv_injects_workspace_host_path_for_socket_tong(self):
        # A broker (socket-holding) tong is handed the workspace's host path so it
        # can bind-mount the workspace into the workers it spawns.
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["docker-socket"]
        argv = tongs.tong_run_argv(
            "broker", defn, container_name="c", network="n", alias="broker",
            workspace="/host/ws",
        )
        self.assertIn("SWARMFORGE_WORKSPACE_HOST_PATH=/host/ws", argv)

    def test_run_argv_omits_workspace_host_path_for_non_socket_tong(self):
        # Ordinary tongs never see the host path, so the env they get is unchanged.
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["workspace:ro"]
        argv = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w", workspace="/host/ws",
        )
        self.assertNotIn("SWARMFORGE_WORKSPACE_HOST_PATH", " ".join(argv))

    def test_run_argv_omits_workspace_host_path_when_workspace_unknown(self):
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["docker-socket"]
        argv = tongs.tong_run_argv("broker", defn, container_name="c", network="n", alias="broker")
        self.assertNotIn("SWARMFORGE_WORKSPACE_HOST_PATH", " ".join(argv))

    def test_run_argv_explicit_workspace_host_path_wins(self):
        # A tong that sets the name itself keeps its own value (setdefault).
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["docker-socket"]
        argv = tongs.tong_run_argv(
            "broker", defn, container_name="c", network="n", alias="broker",
            env={"SWARMFORGE_WORKSPACE_HOST_PATH": "/explicit"}, workspace="/host/ws",
        )
        self.assertIn("SWARMFORGE_WORKSPACE_HOST_PATH=/explicit", argv)
        self.assertNotIn("SWARMFORGE_WORKSPACE_HOST_PATH=/host/ws", argv)

    def test_run_argv_omits_workspace_host_path_for_shared_socket_tong(self):
        # A `shared` broker is reused across sessions, so it must not receive a
        # per-session workspace path.
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["docker-socket"]
        defn["lifecycle"] = "shared"
        argv = tongs.tong_run_argv(
            "broker", defn, container_name="c", network="n", alias="broker",
            workspace="/host/ws",
        )
        self.assertNotIn("SWARMFORGE_WORKSPACE_HOST_PATH", " ".join(argv))

    def test_anvil_option_value_reads_name_and_network(self):
        self.assertEqual(tongs.anvil_option_value(ANVIL_ARGV, "--name"), "claude-proj")
        self.assertEqual(tongs.anvil_option_value(ANVIL_ARGV, "--network"), "opencode-net")

    def test_anvil_option_value_equals_form(self):
        self.assertEqual(tongs.anvil_option_value(["docker", "run", "--network=foo", "img"], "--network"), "foo")

    def test_anvil_option_value_absent_is_none(self):
        self.assertIsNone(tongs.anvil_option_value(ANVIL_ARGV, "--gpus"))

    def test_anvil_option_value_ignores_harness_args_after_image(self):
        argv = ["docker", "run", "--rm", "img", "--network", "harness-net"]
        self.assertIsNone(tongs.anvil_option_value(argv, "--network"))

    def test_inject_noop_returns_argv_unchanged(self):
        # The passthrough basis: no network/args injected => byte-identical argv.
        self.assertEqual(tongs.inject_anvil_argv(ANVIL_ARGV), ANVIL_ARGV)

    def test_inject_does_not_mutate_input(self):
        original = list(ANVIL_ARGV)
        tongs.inject_anvil_argv(ANVIL_ARGV, network="x", pre_image_args=["-e", "A=1"])
        self.assertEqual(ANVIL_ARGV, original)

    def test_inject_replaces_existing_network(self):
        out = tongs.inject_anvil_argv(ANVIL_ARGV, network="swarmforge-session-x")
        self.assertEqual(out[out.index("--network") + 1], "swarmforge-session-x")
        self.assertEqual(out.count("--network"), 1)  # replaced, not appended

    def test_inject_pre_image_args_go_before_image(self):
        out = tongs.inject_anvil_argv(ANVIL_ARGV, pre_image_args=["-e", "SWARMFORGE_TONG_PG_HOST=pg"])
        self.assertLess(out.index("SWARMFORGE_TONG_PG_HOST=pg"), out.index("claude-code:local"))
        # inserted right after the run subcommand
        self.assertEqual(out[2], "-e")

    def test_inject_post_image_args_go_to_harness(self):
        out = tongs.inject_anvil_argv(ANVIL_ARGV, post_image_args=["--mcp-config", "/p.json"])
        self.assertEqual(out[-2:], ["--mcp-config", "/p.json"])
        self.assertGreater(out.index("--mcp-config"), out.index("claude-code:local"))

    def test_inject_inserts_network_when_absent(self):
        argv = ["docker", "run", "--rm", "img"]
        out = tongs.inject_anvil_argv(argv, network="net")
        self.assertIn("--network", out)
        self.assertEqual(out[out.index("--network") + 1], "net")

    def test_inject_does_not_rewrite_harness_network_arg(self):
        argv = ["docker", "run", "--rm", "img", "--network", "harness-net"]
        out = tongs.inject_anvil_argv(argv, network="net")
        self.assertEqual(out[:4], ["docker", "run", "--network", "net"])
        self.assertEqual(out[-2:], ["--network", "harness-net"])

    def test_inject_non_docker_run_raises_when_splicing(self):
        with self.assertRaises(ValueError):
            tongs.inject_anvil_argv(["podman", "ps"], pre_image_args=["-e", "A=1"])

    def test_to_create_argv_swaps_run_for_create(self):
        out = tongs.to_create_argv(ANVIL_ARGV)
        self.assertEqual(out[:2], ["docker", "create"])
        # Everything else is preserved byte-for-byte.
        self.assertEqual(out[2:], ANVIL_ARGV[2:])

    def test_to_create_argv_does_not_mutate_input(self):
        original = list(ANVIL_ARGV)
        tongs.to_create_argv(ANVIL_ARGV)
        self.assertEqual(ANVIL_ARGV, original)

    def test_to_create_argv_leaves_a_create_argv_unchanged(self):
        argv = ["docker", "create", "--rm", "img"]
        self.assertEqual(tongs.to_create_argv(argv), argv)

    def test_to_create_argv_does_not_rewrite_a_harness_run_arg(self):
        # Only the subcommand is swapped; a later 'run' token (e.g. a harness arg)
        # is left alone.
        argv = ["docker", "run", "img", "run"]
        self.assertEqual(tongs.to_create_argv(argv), ["docker", "create", "img", "run"])

    def test_to_create_argv_non_docker_run_raises(self):
        with self.assertRaises(ValueError):
            tongs.to_create_argv(["podman", "ps"])


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
