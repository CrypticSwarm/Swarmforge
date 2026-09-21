#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.secrets. Run: python3 tests/test_tongs_secrets.py"""

import json
import os
import signal
import subprocess
import sys
import tempfile
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
        self.assertEqual(len(refs), 2)

    def test_multiple_refs_in_one_string_are_all_found_and_substituted(self):
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
        self.assertEqual(out["image"], "x")
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

    def test_loads_overrides_only_entry_with_a_none_default(self):
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
        # Single-quoting with the quotes escaped is what keeps a value from breaking out.
        script = tongs.render_secret_exports({"B": "two\nlines", "A": "it's a $X"})
        # Sorted by name; A first.
        self.assertEqual(
            script,
            "export A='it'\\''s a $X'\n" "export B='two\nlines'\n",
        )

    def test_render_secret_exports_eval_round_trips_the_value(self):
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

    def test_secret_inject_argv_makes_and_reads_fifo_then_execs_target(self):
        entrypoint, command = tongs.secret_inject_argv(["node", "server.js"])
        self.assertEqual(entrypoint, "/bin/sh")
        self.assertEqual(command[0], "-c")
        self.assertIn("mkfifo /run/swarmforge/secret-env", command[1])
        self.assertIn("cat /run/swarmforge/secret-env", command[1])
        self.assertIn("rm -f /run/swarmforge/secret-env", command[1])
        self.assertIn("|| exit 1", command[1])
        self.assertIn('exec "$@"', command[1])
        # The target argv is passed after the `$0` placeholder so `"$@"` is it.
        self.assertEqual(command[2:], ["swarmforge-tong", "node", "server.js"])

    def test_secret_inject_argv_does_not_exec_target_when_fifo_fails(self):
        # Redirect the module's own global; the package re-export is a second binding.
        old_target = tongs.secrets.SECRET_FIFO_TARGET
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongs.secrets.SECRET_FIFO_TARGET = os.path.join(
                    tmp, "no-such-dir", "fifo"
                )
                entrypoint, command = tongs.secret_inject_argv(
                    ["/bin/sh", "-c", "printf target-ran"]
                )
                # Without this the test also passes on the real FIFO merely being absent.
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

    def _spawn_wrapper(self, target_argv):
        """Popen the secret wrapper around `target_argv`, reaped on any failure.

        A wrapper blocked on its FIFO outlives a failed test (the orphaned FIFO
        inode never gets a writer), so it is killed-then-waited via cleanups.
        The kill targets the whole process group: killing just the `/bin/sh`
        would orphan its `$(cat fifo)` child, still blocked in the FIFO open.
        """
        entrypoint, command = tongs.secret_inject_argv(target_argv)
        wrapper = subprocess.Popen(
            [entrypoint] + command,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(wrapper.wait)

        def kill_group():
            try:
                os.killpg(wrapper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(kill_group)
        return wrapper

    def _deliver_when_fifo_appears(self, payload):
        """Run the deliver command, retrying the wrapper's `mkfifo` window.

        Returns the first result that is not `SECRET_FIFO_ABSENT_EXIT` -- the
        launcher's retry loop, condensed for tests.
        """
        deliver = tongs.secret_deliver_command()
        attempts = 50
        while True:
            writer = subprocess.run(
                deliver, input=payload, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if writer.returncode != tongs.SECRET_FIFO_ABSENT_EXIT:
                return writer
            attempts -= 1
            self.assertGreater(attempts, 0, "wrapper never created the FIFO")

    def test_secret_wrapper_and_deliver_command_round_trip(self):
        old_target = tongs.secrets.SECRET_FIFO_TARGET
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongs.secrets.SECRET_FIFO_TARGET = os.path.join(tmp, "secret-env")
                # With no FIFO yet, deliver must refuse retryably and create no file.
                early = subprocess.run(
                    tongs.secret_deliver_command(), input=b"export TOKEN='never'\n",
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                self.assertEqual(early.returncode, tongs.SECRET_FIFO_ABSENT_EXIT)
                self.assertEqual(os.listdir(tmp), [])
                wrapper = self._spawn_wrapper(
                    ["/bin/sh", "-c", 'printf "%%s|%%s" "$TOKEN" "$(ls %s)"' % tmp]
                )
                payload = tongs.render_secret_exports({"TOKEN": "s3cr3t"}).encode()
                writer = self._deliver_when_fifo_appears(payload)
                self.assertEqual(writer.returncode, 0)
                stdout, _ = wrapper.communicate(timeout=10)
        finally:
            tongs.secrets.SECRET_FIFO_TARGET = old_target
        self.assertEqual(wrapper.returncode, 0)
        self.assertEqual(stdout, b"s3cr3t|")  # env delivered, FIFO removed

    def test_secret_round_trip_delivers_payload_larger_than_pipe_buffer(self):
        # 100 KB: over the ~64 KiB pipe buffer, under the kernel's 128 KiB env-string cap.
        old_target = tongs.secrets.SECRET_FIFO_TARGET
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongs.secrets.SECRET_FIFO_TARGET = os.path.join(tmp, "secret-env")
                wrapper = self._spawn_wrapper(
                    ["/bin/sh", "-c", 'printf %s "${#TOKEN}"']
                )
                payload = tongs.render_secret_exports(
                    {"TOKEN": "x" * 100000}
                ).encode()
                writer = self._deliver_when_fifo_appears(payload)
                self.assertEqual(writer.returncode, 0)
                stdout, _ = wrapper.communicate(timeout=10)
        finally:
            tongs.secrets.SECRET_FIFO_TARGET = old_target
        self.assertEqual(wrapper.returncode, 0)
        self.assertEqual(stdout, b"100000")

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
