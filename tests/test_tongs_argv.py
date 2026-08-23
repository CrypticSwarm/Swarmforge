#!/usr/bin/env python3
"""Unit tests for swarmforge.tongs.argv. Run: python3 tests/test_tongs_argv.py"""

import os
import sys
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

from tongs_fixtures import NONE_TONG, PORT_TONG, def_of


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

    def test_run_argv_emits_one_flag_per_declared_alias(self):
        defn = def_of(PORT_TONG)
        defn["interface"]["aliases"] = ["api", "local.example.test"]
        argv = tongs.tong_run_argv(
            "pg", defn, container_name="ctr-pg", network="net", alias="pg",
        )
        flagged = [argv[i + 1] for i, part in enumerate(argv) if part == "--network-alias"]
        # Canonical first, then the declared extras, in declaration order.
        self.assertEqual(flagged, ["pg", "api", "local.example.test"])

    def test_run_argv_alias_flags_unchanged_without_extras(self):
        # The inert case: a definition that declares no extras produces exactly
        # the single alias flag it produced before extras existed.
        argv = tongs.tong_run_argv(
            "pg", def_of(PORT_TONG), container_name="ctr-pg", network="net", alias="pg",
        )
        self.assertEqual(argv.count("--network-alias"), 1)

    def test_run_argv_does_not_repeat_an_extra_matching_the_canonical(self):
        defn = def_of(PORT_TONG)
        defn["interface"]["aliases"] = ["pg", "api"]
        argv = tongs.tong_run_argv(
            "pg", defn, container_name="ctr-pg", network="net", alias="pg",
        )
        flagged = [argv[i + 1] for i, part in enumerate(argv) if part == "--network-alias"]
        self.assertEqual(flagged, ["pg", "api"])

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

    def test_run_argv_mount_target_is_workspace_unless_declared(self):
        # The inert case beside its opt-in: an undeclared target still lands on
        # /workspace, and declaring one changes nothing but the `-v` value.
        def argv_for(mounts):
            defn = def_of(NONE_TONG)
            defn["mounts"] = mounts
            return tongs.tong_run_argv(
                "w", defn, container_name="c", network="n", alias="w", workspace="/ws",
            )

        default = argv_for(["workspace"])
        self.assertEqual(default[default.index("-v") + 1], "/ws:/workspace")

        declared = argv_for(["workspace:/work:rw"])
        self.assertEqual(declared[declared.index("-v") + 1], "/ws:/work:rw")
        self.assertEqual(
            [part for part in declared if part != "/ws:/work:rw"],
            [part for part in default if part != "/ws:/workspace"],
        )

    def test_run_argv_extra_mount_specs_follow_definition_mounts(self):
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["workspace"]
        argv = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w", workspace="/ws",
            extra_mount_specs=["/ws/.git/config:/workspace/.git/config:ro"],
        )
        mounted = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
        self.assertEqual(
            mounted,
            ["/ws:/workspace", "/ws/.git/config:/workspace/.git/config:ro"],
        )

    def test_run_argv_without_extra_mount_specs_is_unchanged(self):
        defn = def_of(NONE_TONG)
        defn["mounts"] = ["workspace"]
        with_none = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w", workspace="/ws",
            extra_mount_specs=None,
        )
        omitted = tongs.tong_run_argv(
            "w", defn, container_name="c", network="n", alias="w", workspace="/ws",
        )
        self.assertEqual(with_none, omitted)

    def test_run_argv_secret_injection_mounts_tmpfs_wraps_entrypoint(self):
        # A secret-bearing tong gets a tmpfs for its in-container FIFO, the
        # /bin/sh entrypoint override, and the wrapper command appended after the
        # image.
        entrypoint, command = tongs.secret_inject_argv(["node", "server.js"])
        argv = tongs.tong_run_argv(
            "g", def_of(NONE_TONG), container_name="c", network="n", alias="g",
            secret_channel=True, entrypoint=entrypoint, command=command,
        )
        self.assertEqual(
            argv[argv.index("--entrypoint") + 1], "/bin/sh"
        )
        self.assertEqual(argv[argv.index("--tmpfs") + 1],
                         "/run/swarmforge:rw,nosuid,nodev,noexec,mode=1777")
        self.assertNotIn("-v", argv)  # no host bind backs the secret channel
        # The wrapper command trails the image (which is NONE_TONG's image).
        image = def_of(NONE_TONG)["image"]
        self.assertEqual(argv[argv.index(image) + 1 :], command)

    def test_run_argv_without_secrets_omits_entrypoint_and_tmpfs(self):
        argv = tongs.tong_run_argv(
            "g", def_of(NONE_TONG), container_name="c", network="n", alias="g",
        )
        self.assertNotIn("--entrypoint", argv)
        self.assertNotIn("--tmpfs", argv)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
