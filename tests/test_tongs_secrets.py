#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.secrets. Run: python3 tests/test_tongs_secrets.py"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs

from tongs_fixtures import GITHUB_TONG, def_of


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
        # Redirected on the module `secret_inject_argv` reads its global from:
        # the package re-export is a second binding the function never consults,
        # so pointing that one at the temp path would leave the real FIFO path
        # baked into the script and the test asserting nothing.
        old_target = tongs.secrets.SECRET_FIFO_TARGET
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongs.secrets.SECRET_FIFO_TARGET = os.path.join(tmp, "missing")
                entrypoint, command = tongs.secret_inject_argv(
                    ["/bin/sh", "-c", "printf target-ran"]
                )
                # The redirect has to reach the script. Without this the test
                # also passes on the real FIFO path merely being absent, which
                # is the failure mode redirecting the re-export produces.
                self.assertIn(tmp, command[1])
                completed = subprocess.run(
                    [entrypoint] + command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        finally:
            tongs.secrets.SECRET_FIFO_TARGET = old_target
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
