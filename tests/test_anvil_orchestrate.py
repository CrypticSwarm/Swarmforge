#!/usr/bin/env python3
"""Unit tests for swarmforge.anvil.orchestrate. Run: python3 tests/test_anvil_orchestrate.py"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Standing in for the launcher's entry-point shim keeps this file runnable on its own.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# `python3 -m unittest tests.<module>` does not put this directory on the path.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# `anvil` is already these tests' word for the container the launcher wraps.
from swarmforge import anvil as launcher
from swarmforge import tongs

from anvil_fixtures import ANVIL_ARGV, _merged


class UnsupportedTongReasonsTests(unittest.TestCase):
    """The single chokepoint that refuses tongs the launcher cannot start yet."""

    def _reasons(self, defn):
        return launcher.unsupported_tong_reasons(_merged("t", defn, source=tongs.REPO))

    def test_startable_port_tong_has_no_reasons(self):
        self.assertEqual(
            self._reasons({
                "lifecycle": "shared", "image": "x",
                "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_startable_none_tong_has_no_reasons(self):
        self.assertEqual(self._reasons(SHARED_NONE), [])

    def test_startable_session_tong_has_no_reasons(self):
        self.assertEqual(
            self._reasons({
                "lifecycle": "session", "image": "x",
                "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_volume_interface_is_refused(self):
        # A shared named volume has no anvil-side consumer.
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "volume", "volume": "v", "mountpoint": "/m"},
             "readiness": {"mode": "none"}}))

    def test_mcp_tong_startable_on_either_lifecycle(self):
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])

    def test_secret_tong_startable_on_either_lifecycle(self):
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "none"}, "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"}}), [])

    def test_shared_workspace_mount_refused_but_docker_socket_allowed(self):
        # A shared workspace mount leaks the workspace across sessions.
        self.assertTrue(any(
            "workspace" in r for r in self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["workspace:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            })
        ))
        self.assertEqual(
            self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["docker-socket"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_shared_workspace_refused_with_a_custom_mount_target(self):
        self.assertTrue(any(
            "workspace" in r for r in self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["workspace:/code:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            })
        ))

    def test_workspace_refusal_is_shared_scoped(self):
        # A `session` tong is torn down with the anvil, so it cannot leak.
        self.assertEqual(
            self._reasons({
                "lifecycle": "session", "image": "x", "mounts": ["workspace:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            }),
            [],
        )


class FakeDocker:
    """In-process stand-in for DockerCLI that records calls and returns canned
    results, so orchestration is tested without a docker daemon."""

    def __init__(self, states=None, ready=True, anvil_rc=0,
                 image_config=(["app"], [])):
        self.calls = []
        self._states = states or {}
        self._ready = ready
        self._anvil_rc = anvil_rc
        self._image_config = image_config
        self.run_argvs = []
        self.inspected_images = []
        self.anvil_argv = None
        self.anvil_extra_networks = None

    def rm_force(self, container):
        self.calls.append(("rm_force", container))

    def run_detached(self, argv):
        self.run_argvs.append(argv)
        container = argv[argv.index("--name") + 1] if "--name" in argv else None
        self.calls.append(("run_detached", container))

    def image_exec_config(self, image):
        self.inspected_images.append(image)
        return self._image_config

    def ensure_network(self, name):
        self.calls.append(("ensure_network", name))

    def network_connect(self, network, container, aliases=()):
        self.calls.append(("network_connect", network, container, tuple(aliases)))

    def network_disconnect(self, network, container):
        self.calls.append(("network_disconnect", network, container))

    def network_rm(self, network):
        self.calls.append(("network_rm", network))

    def run_foreground_multi(self, argv, extra_networks, container):
        self.anvil_argv = argv
        self.anvil_extra_networks = list(extra_networks)
        self.calls.append(("run_foreground_multi", argv, tuple(extra_networks), container))
        return self._anvil_rc

    def inspect_state(self, container):
        return self._states.get(container)

    def health_status(self, container):
        return "healthy" if self._ready else "starting"

    def exec_ok(self, container, command):
        return self._ready

    def tcp_probe(self, network, host, port, image):
        self.calls.append(("tcp_probe", network, host, port, image))
        return self._ready

    def run_foreground(self, argv):
        self.anvil_argv = argv
        self.calls.append(("run_foreground", argv))
        return self._anvil_rc


class FakeChannels:
    """A `make_channel` stand-in that records secret deliveries without docker.

    Records the container each channel is opened for and every payload delivered,
    so a test can assert what reached the tong (and that the secret never went
    through `-e`/argv) without invoking docker exec. `deliver_error`, if set, is
    raised from `deliver` to exercise the failure path.
    """

    def __init__(self, deliver_error=None):
        self.containers = []
        self.payloads = []
        self._deliver_error = deliver_error

    def __call__(self, docker, container):
        self.containers.append(container)
        return self

    def deliver(self, payload):
        self.payloads.append(payload)
        if self._deliver_error is not None:
            raise self._deliver_error


def _opts(workspace=None, anvil_image="anvil:img", harness="opencode"):
    return launcher.LauncherOptions(
        layer_dirs=[], workspace=workspace, approvals=None, providers=None,
        harness=harness, anvil_image=anvil_image, no_prompt=False,
    )


# A counter clock, so readiness loops never sleep on the wall clock.
class _Clock:
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


SHARED_OLLAMA = {
    "lifecycle": "shared",
    "image": "ollama/ollama",
    "interface": {"kind": "port", "port": 11434},
    "readiness": {"mode": "tcp"},
}

SHARED_NONE = {
    "lifecycle": "shared",
    "image": "log-shipper",
    "interface": {"kind": "none"},
    "readiness": {"mode": "none"},
}

SHARED_MCP = {
    "lifecycle": "shared",
    "image": "github-tong",
    "interface": {"kind": "mcp", "name": "github", "port": 8080},
    "readiness": {"mode": "none"},
}

# Two orgs ship this same file with different credentials.
ORG_ASANA = {
    "lifecycle": "shared",
    "image": "asana-mcp:latest",
    "interface": {"kind": "mcp", "name": "asana-mcp", "port": 3000},
    "readiness": {"mode": "none"},
}

SESSION_PORT = {
    "lifecycle": "session",
    "image": "fixture-pg",
    "interface": {"kind": "port", "port": 5432},
    "readiness": {"mode": "none"},
}

# Built on the test interpreter, so no op/pass need be installed; it echoes the {ref}.
ECHO_PROVIDERS = {
    "echo": [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", "{ref}"]
}

SHARED_SECRET = {
    "lifecycle": "shared",
    "image": "github-tong",
    "env": {"GITHUB_TOKEN": "${secret:echo:s3cr3t}"},
    "interface": {"kind": "none"},
    "readiness": {"mode": "none"},
}


class McpInjectionTests(unittest.TestCase):
    """_mcp_injection writes the generated config and shapes the anvil args."""

    FRAGMENT = {"mcp": {"github": {"type": "remote", "url": "http://github:8080/mcp"}}}

    def test_empty_fragment_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = launcher.orchestrate._mcp_injection({}, "opencode", tmp)
            self.assertEqual((pre, post), ([], []))
            self.assertEqual(os.listdir(tmp), [])

    def test_opencode_mounts_and_sets_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = launcher.orchestrate._mcp_injection(self.FRAGMENT, "opencode", tmp)
            host_path = os.path.join(tmp, "tong-mcp.json")
            self.assertEqual(post, [])  # OpenCode reads it via the entrypoint
            self.assertEqual(
                pre,
                ["-v", "%s:%s:ro" % (host_path, launcher.MCP_CONFIG_CONTAINER_PATH),
                 "-e", "%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH)],
            )
            with open(host_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.FRAGMENT)

    def test_toml_harnesses_mount_and_set_env(self):
        # Delivered through the entrypoint, so the harness argv stays untouched.
        fragment = {"mcp_servers": {"github": {"url": "http://github:8080/mcp"}}}
        for harness in ("grok", "codex"):
            with tempfile.TemporaryDirectory() as tmp:
                pre, post = launcher.orchestrate._mcp_injection(fragment, harness, tmp)
                host_path = os.path.join(tmp, "tong-mcp.json")
                self.assertEqual(post, [], harness)
                self.assertEqual(
                    pre,
                    ["-v", "%s:%s:ro" % (host_path, launcher.MCP_CONFIG_CONTAINER_PATH),
                     "-e", "%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH)],
                    harness,
                )
                with open(host_path, encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle), fragment)

    def test_claude_mounts_and_appends_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre, post = launcher.orchestrate._mcp_injection(self.FRAGMENT, "claude", tmp)
            host_path = os.path.join(tmp, "tong-mcp.json")
            self.assertEqual(
                pre, ["-v", "%s:%s:ro" % (host_path, launcher.MCP_CONFIG_CONTAINER_PATH)]
            )
            self.assertEqual(post, ["--mcp-config", launcher.MCP_CONFIG_CONTAINER_PATH])
            with open(host_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.FRAGMENT)


class RunWithTongsTests(unittest.TestCase):
    def _run(self, docker, merged, anvil=None, workspace=None, harness="opencode"):
        return launcher.run_with_tongs(
            merged, anvil or ANVIL_ARGV, _opts(workspace=workspace, harness=harness),
            docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_shared_tong_starts_when_absent_and_runs_anvil(self):
        docker = FakeDocker()
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 0)
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertIn("ollama", started)  # network-alias
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_shared_tong_reused_when_running_and_hash_matches(self):
        defn = SHARED_OLLAMA
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(defn)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", defn, source=tongs.REPO))
        self.assertEqual(docker.run_argvs, [])

    def test_unusable_mount_reported_as_a_config_error_naming_the_tong(self):
        defn = dict(SHARED_OLLAMA, mounts=["docker-socket", "docker-socket"])
        docker = FakeDocker()
        with self.assertRaisesRegex(launcher.OrchestrationError, "tong 'ollama'.*overlaps"):
            self._run(docker, _merged("ollama", defn, source=tongs.REPO))
        self.assertEqual(docker.run_argvs, [])
        self.assertNotIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertIsNone(docker.anvil_argv)

    def test_shared_tong_recreated_when_hash_differs(self):
        states = {"swarmforge-shared-ollama": {"running": True, "label": "stale"}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_shared_tong_recreated_when_absent(self):
        # rm_force clears any stopped leftover before the fresh start.
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_stopped_shared_tong_is_recreated_despite_a_matching_hash(self):
        states = {"swarmforge-shared-ollama":
                  {"running": False, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_multiple_shared_tongs_started_and_injected(self):
        pg = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
        }
        redis = {
            "lifecycle": "shared", "image": "redis",
            "interface": {"kind": "port", "port": 6379}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        merged = {
            "pg": {"source": tongs.REPO, "definition": pg},
            "redis": {"source": tongs.REPO, "definition": redis},
        }
        self._run(docker, merged)
        self.assertEqual(len(docker.run_argvs), 2)
        argv = docker.anvil_argv
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", argv)
        self.assertIn("SWARMFORGE_TONG_PG_PORT=5432", argv)
        self.assertIn("SWARMFORGE_TONG_REDIS_HOST=redis", argv)
        self.assertIn("SWARMFORGE_TONG_REDIS_PORT=6379", argv)

    def test_tcp_readiness_probes_alias_with_anvil_image(self):
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("tcp_probe", "opencode-net", "ollama", 11434, "anvil:img"), docker.calls)

    def test_port_tong_injects_host_and_port_env_into_anvil(self):
        defn = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        self._run(docker, _merged("pg", defn, source=tongs.REPO))
        argv = docker.anvil_argv
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", argv)
        self.assertIn("SWARMFORGE_TONG_PG_PORT=5432", argv)

    def test_none_tong_leaves_anvil_argv_unchanged(self):
        docker = FakeDocker()
        self._run(docker, _merged("shipper", SHARED_NONE, source=tongs.REPO))
        self.assertEqual(docker.anvil_argv, ANVIL_ARGV)

    def _mcp_mount_host_path(self, argv):
        """Host path of the read-only MCP-config bind mount in an anvil argv."""
        suffix = ":%s:ro" % launcher.MCP_CONFIG_CONTAINER_PATH
        for index, token in enumerate(argv):
            if token == "-v" and argv[index + 1].endswith(suffix):
                return argv[index + 1][: -len(suffix)]
        self.fail("no MCP-config mount found in anvil argv")

    def test_opencode_mcp_tong_mounts_config_and_sets_env(self):
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="opencode")
        argv = docker.anvil_argv
        self.assertIn("github", docker.run_argvs[0])  # tong started under its alias
        self.assertIn(
            "%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH), argv
        )
        self._mcp_mount_host_path(argv)
        self.assertNotIn("--mcp-config", argv)

    def test_claude_mcp_tong_mounts_config_and_appends_flag(self):
        # The flag lands after the image so it reaches the harness binary.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="claude")
        argv = docker.anvil_argv
        self.assertEqual(argv[-2:], ["--mcp-config", launcher.MCP_CONFIG_CONTAINER_PATH])
        self.assertNotIn("%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH),
                         argv)
        self._mcp_mount_host_path(argv)

    def test_mcp_tong_with_unknown_harness_raises_before_docker(self):
        for harness in (None, "opencdoe"):
            with self.subTest(harness=harness):
                docker = FakeDocker()
                with self.assertRaisesRegex(launcher.OrchestrationError, "--harness"):
                    self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                              harness=harness)
                self.assertEqual(docker.calls, [])
                self.assertEqual(docker.run_argvs, [])
                self.assertIsNone(docker.anvil_argv)

    def test_mcp_config_tempfile_cleaned_up_after_run(self):
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="opencode")
        host_path = self._mcp_mount_host_path(docker.anvil_argv)
        self.assertFalse(os.path.exists(host_path))

    def test_unready_tong_raises_and_anvil_never_runs(self):
        docker = FakeDocker(ready=False)
        defn = {
            "lifecycle": "shared", "image": "pg",
            "interface": {"kind": "port", "port": 5432},
            "readiness": {"mode": "tcp", "timeout": "1s"},
        }
        with self.assertRaises(launcher.OrchestrationError):
            self._run(docker, _merged("pg", defn, source=tongs.REPO))
        self.assertIsNone(docker.anvil_argv)

    def test_anvil_exit_code_is_returned(self):
        docker = FakeDocker(anvil_rc=42)
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 42)

    def test_no_anvil_image_degrades_tcp_to_running_check(self):
        # Without an anvil image there is no container to dial the port from.
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        rc = launcher.run_with_tongs(
            _merged("ollama", SHARED_OLLAMA, source=tongs.REPO), ANVIL_ARGV,
            _opts(anvil_image=None), docker=docker,
            sleep=lambda _s: None, monotonic=_Clock(),
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("tcp_probe", [c[0] for c in docker.calls])

    # --- Secret resolution + channel env delivery ---------------------------

    def _run_secret(self, docker, merged, providers, channels=None):
        return launcher.run_with_tongs(
            merged, ANVIL_ARGV, _opts(), docker=docker, providers=providers,
            make_channel=channels or FakeChannels(),
            sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_secret_delivered_as_env_via_channel_never_in_argv(self):
        docker = FakeDocker(image_config=(["node"], ["server.js"]))
        channels = FakeChannels()
        rc = self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        started = docker.run_argvs[0]
        self.assertEqual(started[started.index("--entrypoint") + 1], "/bin/sh")
        self.assertEqual(started[started.index("--tmpfs") + 1],
                         "/run/swarmforge:rw,nosuid,nodev,noexec,mode=1777")
        # The wrapper execs the image's real argv, after the image token.
        self.assertEqual(started[started.index("github-tong") + 1:],
                         ["-c", started[started.index("-c") + 1],
                          "swarmforge-tong", "node", "server.js"])
        self.assertNotIn("s3cr3t", " ".join(started))
        self.assertNotIn("GITHUB_TOKEN=s3cr3t", started)
        self.assertEqual(channels.payloads, ["export GITHUB_TOKEN='s3cr3t'\n"])

    def test_secret_tong_reads_exec_target_from_image(self):
        docker = FakeDocker(image_config=(["entry"], ["arg"]))
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS
        )
        self.assertEqual(docker.inspected_images, ["github-tong"])

    def test_unresolvable_secret_stops_launch_before_anvil(self):
        docker = FakeDocker()
        with self.assertRaises(launcher.SecretResolutionError):
            self._run_secret(docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), {})
        self.assertEqual(docker.run_argvs, [])
        self.assertIsNone(docker.anvil_argv)

    def test_delivery_failure_removes_half_configured_container(self):
        # Left behind, its config-hash label marks a secret-less container reusable.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=launcher.DockerError("boom"))
        with self.assertRaises(launcher.DockerError):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        # Twice: the leftover clear before start, then the failed delivery.
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertIsNone(docker.anvil_argv)

    def test_unusable_mount_target_reported_and_starts_nothing(self):
        defn = dict(SHARED_SECRET, mounts=["workspace:/run"])
        docker = FakeDocker()
        channels = FakeChannels()
        with self.assertRaisesRegex(launcher.OrchestrationError, "tong 'gh'.*overlaps"):
            self._run_secret(
                docker, _merged("gh", defn, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        self.assertEqual(docker.run_argvs, [])
        self.assertNotIn(("rm_force", "swarmforge-shared-gh"), docker.calls)
        self.assertEqual(channels.payloads, [])
        self.assertIsNone(docker.anvil_argv)

    def test_interrupt_during_delivery_removes_half_configured_container(self):
        # Ctrl-C too: a `shared` tong is not tracked for session teardown.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertIsNone(docker.anvil_argv)

    def test_reused_shared_tong_never_resolves_or_delivers_secrets(self):
        # Invoking the provider CLI could prompt for an unlock every session.
        states = {"swarmforge-shared-gh":
                  {"running": True, "label": tongs.config_hash(SHARED_SECRET)}}
        docker = FakeDocker(states=states)
        channels = FakeChannels()
        boom = {"echo": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        rc = self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), boom,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(docker.run_argvs, [])
        self.assertEqual(channels.payloads, [])
        self.assertEqual(docker.inspected_images, [])

    def test_session_secret_tong_delivered_over_channel(self):
        defn = {
            "lifecycle": "session", "image": "creds",
            "env": {"TOKEN": "${secret:echo:abc}"},
            "interface": {"kind": "none"}, "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        channels = FakeChannels()
        rc = self._run_secret(
            docker, _merged("creds", defn, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(channels.payloads, ["export TOKEN='abc'\n"])

    def test_channel_factory_gets_the_started_container(self):
        # Delivery is a `docker exec` into that container.
        docker = FakeDocker()
        channels = FakeChannels()
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(channels.containers, ["swarmforge-shared-gh"])

    # --- Session lifecycle + per-session networks ---------------------------

    def test_shared_only_keeps_base_network_and_plain_run(self):
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        kinds = [c[0] for c in docker.calls]
        self.assertNotIn("ensure_network", kinds)
        self.assertNotIn("network_rm", kinds)
        self.assertNotIn("run_foreground_multi", kinds)
        self.assertIn("run_foreground", kinds)
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )
        self.assertIsNone(docker.anvil_extra_networks)

    def test_session_tong_creates_network_starts_on_it_and_tears_down(self):
        docker = FakeDocker()
        rc = self._run(docker, _merged("pg", SESSION_PORT, source=tongs.REPO))
        self.assertEqual(rc, 0)
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(("ensure_network", net), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("claude-myproject-tong-pg", started)
        self.assertEqual(started[started.index("--network") + 1], net)
        self.assertEqual(started[started.index("--network-alias") + 1], "pg")
        self.assertEqual(docker.anvil_argv[docker.anvil_argv.index("--network") + 1], net)
        self.assertEqual(docker.anvil_extra_networks, ["opencode-net"])
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", docker.anvil_argv)
        # docker refuses a network rm while its endpoints are still attached.
        self.assertIn(("rm_force", "claude-myproject-tong-pg"), docker.calls)
        self.assertIn(("rm_force", "claude-myproject"), docker.calls)
        self.assertIn(("network_rm", net), docker.calls)
        self.assertLess(
            docker.calls.index(("rm_force", "claude-myproject")),
            docker.calls.index(("network_rm", net)),
        )
        self.assertLess(
            docker.calls.index(("rm_force", "claude-myproject-tong-pg")),
            docker.calls.index(("network_rm", net)),
        )

    def test_declared_aliases_reach_the_session_and_shared_tongs(self):
        # A session tong registers its aliases at start, a shared tong at connect.
        docker = FakeDocker()
        session = dict(SESSION_PORT)
        session["interface"] = dict(SESSION_PORT["interface"],
                                    aliases=["db", "local.example.test"])
        shared = dict(SHARED_OLLAMA)
        shared["interface"] = dict(SHARED_OLLAMA["interface"], aliases=["models"])
        merged = {
            "pg": {"source": tongs.REPO, "definition": session},
            "ollama": {"source": tongs.REPO, "definition": shared},
        }
        self._run(docker, merged)
        net = tongs.session_network_name("claude-myproject")
        started = next(argv for argv in docker.run_argvs
                       if "claude-myproject-tong-pg" in argv)
        flagged = [started[i + 1] for i, part in enumerate(started)
                   if part == "--network-alias"]
        self.assertEqual(flagged, ["pg", "db", "local.example.test"])
        self.assertIn(
            ("network_connect", net, "swarmforge-shared-ollama", ("ollama", "models")),
            docker.calls,
        )
        # The anvil is still pointed at the canonical alias, not an extra.
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", docker.anvil_argv)

    def test_shared_tong_connected_to_session_network_and_left_running(self):
        docker = FakeDocker()
        merged = {
            "pg": {"source": tongs.REPO, "definition": SESSION_PORT},
            "ollama": {"source": tongs.REPO, "definition": SHARED_OLLAMA},
        }
        self._run(docker, merged)
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(
            ("network_connect", net, "swarmforge-shared-ollama", ("ollama",)), docker.calls
        )
        self.assertIn(
            ("network_disconnect", net, "swarmforge-shared-ollama"), docker.calls
        )
        # A best-effort disconnect precedes the connect, for a reused network.
        self.assertLess(
            docker.calls.index(("network_disconnect", net, "swarmforge-shared-ollama")),
            docker.calls.index(("network_connect", net, "swarmforge-shared-ollama", ("ollama",))),
        )
        # The one rm_force is the start-time leftover clear, not a teardown step.
        self.assertEqual(
            docker.calls.count(("rm_force", "swarmforge-shared-ollama")), 1
        )

    def test_session_tong_readiness_probes_on_session_network(self):
        docker = FakeDocker()
        defn = {
            "lifecycle": "session", "image": "pg",
            "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "tcp"},
        }
        self._run(docker, _merged("pg", defn, source=tongs.REPO))
        net = tongs.session_network_name("claude-myproject")
        self.assertIn(("tcp_probe", net, "pg", 5432, "anvil:img"), docker.calls)

    def test_session_teardown_runs_on_keyboard_interrupt(self):
        docker = FakeDocker()

        def interrupt(argv, extra_networks, container):
            docker.calls.append(("run_foreground_multi", argv, tuple(extra_networks), container))
            raise KeyboardInterrupt

        docker.run_foreground_multi = interrupt
        net = tongs.session_network_name("claude-myproject")
        with self.assertRaises(KeyboardInterrupt):
            self._run(docker, _merged("pg", SESSION_PORT, source=tongs.REPO))
        self.assertIn(("rm_force", "claude-myproject-tong-pg"), docker.calls)
        self.assertIn(("rm_force", "claude-myproject"), docker.calls)
        self.assertIn(("network_rm", net), docker.calls)

    def test_session_tong_without_anvil_name_raises_before_any_docker_call(self):
        docker = FakeDocker()
        anvil = ["docker", "run", "-it", "--rm", "--network", "opencode-net", "img"]
        with self.assertRaises(launcher.OrchestrationError):
            launcher.run_with_tongs(
                _merged("pg", SESSION_PORT, source=tongs.REPO), anvil, _opts(),
                docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
            )
        self.assertEqual(docker.calls, [])  # nothing created => nothing to tear down

    # --- Per-org isolation of `shared` tongs --------------------------------

    _ACME = "/orgs/acme/.swarmforge/tongs"
    _GLOBEX = "/orgs/globex/.swarmforge/tongs"

    def _run_org(self, docker, merged, org_dir, harness="opencode", anvil=None):
        """Drive run_with_tongs with an org layer dir wired into the options."""
        opts = launcher.LauncherOptions(
            layer_dirs=[(tongs.ORG, org_dir)], workspace=None, approvals=None,
            providers=None, harness=harness, anvil_image="anvil:img", no_prompt=False,
        )
        return launcher.run_with_tongs(
            merged, anvil or ANVIL_ARGV, opts,
            docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_org_shared_tong_isolated_on_per_org_network(self):
        docker = FakeDocker()
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        self._run_org(docker, merged, self._ACME)
        token = tongs.org_scope_token(self._ACME)
        net = tongs.shared_network_name(token)
        container = tongs.shared_container_name("asana", scope=token)
        self.assertIn(("ensure_network", net), docker.calls)
        started = docker.run_argvs[0]
        self.assertIn(container, started)
        self.assertEqual(started[started.index("--network") + 1], net)
        self.assertNotEqual(started[started.index("--network") + 1], "opencode-net")
        # opencode-net stays primary, for the model backend.
        self.assertEqual(docker.anvil_extra_networks, [net])
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_org_shared_tong_readiness_probes_on_org_network(self):
        # The org network is the only one a scoped shared tong lives on.
        docker = FakeDocker()
        defn = {
            "lifecycle": "shared", "image": "asana-mcp:latest",
            "interface": {"kind": "mcp", "name": "asana-mcp", "port": 3000},
            "readiness": {"mode": "tcp"},
        }
        merged = {"asana": {"source": tongs.ORG, "definition": defn}}
        self._run_org(docker, merged, self._ACME)
        net = tongs.shared_network_name(tongs.org_scope_token(self._ACME))
        self.assertIn(("tcp_probe", net, "asana-mcp", 3000, "anvil:img"), docker.calls)

    def test_two_orgs_partition_into_distinct_containers_and_networks(self):
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        d1 = FakeDocker()
        self._run_org(d1, merged, self._ACME)
        d2 = FakeDocker()
        self._run_org(d2, merged, self._GLOBEX)

        s1, s2 = d1.run_argvs[0], d2.run_argvs[0]
        self.assertNotEqual(
            s1[s1.index("--name") + 1], s2[s2.index("--name") + 1]
        )
        self.assertNotEqual(d1.anvil_extra_networks, d2.anvil_extra_networks)
        # Same agent-facing MCP name on each org's isolated network.
        self.assertEqual(s1[s1.index("--network-alias") + 1], "asana-mcp")
        self.assertEqual(s2[s2.index("--network-alias") + 1], "asana-mcp")

    def test_non_org_shared_tong_stays_global_even_with_org_layer(self):
        docker = FakeDocker()
        merged = {"ollama": {"source": tongs.REPO, "definition": SHARED_OLLAMA}}
        self._run_org(docker, merged, self._ACME)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertNotIn("ensure_network", [c[0] for c in docker.calls])
        self.assertIsNone(docker.anvil_extra_networks)

    def test_org_shared_network_pruned_best_effort_and_tong_left_running(self):
        # The prune is best-effort: docker refuses while the tong is attached.
        docker = FakeDocker()
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        self._run_org(docker, merged, self._ACME)
        token = tongs.org_scope_token(self._ACME)
        net = tongs.shared_network_name(token)
        container = tongs.shared_container_name("asana", scope=token)
        self.assertIn(("network_rm", net), docker.calls)
        self.assertEqual(docker.calls.count(("rm_force", container)), 1)

    def test_org_shared_tong_without_anvil_name_raises_before_any_docker_call(self):
        docker = FakeDocker()
        anvil = ["docker", "run", "-it", "--rm", "--network", "opencode-net", "img"]
        merged = {"asana": {"source": tongs.ORG, "definition": ORG_ASANA}}
        with self.assertRaises(launcher.OrchestrationError):
            self._run_org(docker, merged, self._ACME, anvil=anvil)
        self.assertEqual(docker.calls, [])


class WorkspaceGitDirSpecTests(unittest.TestCase):
    """_workspace_git_dir_specs: the git-dir mounts riding along with `workspace`."""

    def test_no_workspace_path_is_empty(self):
        defn = {"mounts": ["workspace"]}
        self.assertEqual(launcher.orchestrate._workspace_git_dir_specs(defn, None), [])

    def test_no_workspace_mount_never_calls_the_guard(self):
        defn = {"mounts": ["docker-socket"]}
        with mock.patch.object(launcher.orchestrate.gitguard, "build_mounts") as guard:
            self.assertEqual(launcher.orchestrate._workspace_git_dir_specs(defn, "/ws"), [])
        guard.assert_not_called()

    def test_guard_receives_every_workspace_destination(self):
        defn = {"mounts": ["workspace:/a", "workspace:/b:ro"]}
        with mock.patch.object(launcher.orchestrate.gitguard, "build_mounts",
                               return_value=[]) as guard:
            launcher.orchestrate._workspace_git_dir_specs(defn, "/ws")
        guard.assert_called_once_with("/ws", ["/a", "/b"], warn=None)

    def test_read_write_workspace_keeps_guard_modes(self):
        defn = {"mounts": ["workspace"]}
        specs = ["/ws/.git:/workspace/.git",
                 "/ws/.git/config:/workspace/.git/config:ro"]
        with mock.patch.object(launcher.orchestrate.gitguard, "build_mounts",
                               return_value=list(specs)):
            self.assertEqual(launcher.orchestrate._workspace_git_dir_specs(defn, "/ws"), specs)

    def test_read_only_workspace_forces_every_spec_read_only(self):
        # build_mounts emits writable binds; a `ro` workspace must get no write path.
        defn = {"mounts": ["workspace:ro"]}
        with mock.patch.object(
            launcher.orchestrate.gitguard, "build_mounts",
            return_value=["/ws/.git:/workspace/.git",
                          "/ws/.git/config:/workspace/.git/config:ro"],
        ):
            self.assertEqual(
                launcher.orchestrate._workspace_git_dir_specs(defn, "/ws"),
                ["/ws/.git:/workspace/.git:ro",
                 "/ws/.git/config:/workspace/.git/config:ro"],
            )

    def test_mixed_modes_keep_guard_modes(self):
        # One writable workspace mount means the git dir must stay writable too.
        defn = {"mounts": ["workspace:/a:ro", "workspace:/b"]}
        with mock.patch.object(launcher.orchestrate.gitguard, "build_mounts",
                               return_value=["/x/.git:/x/.git"]):
            self.assertEqual(
                launcher.orchestrate._workspace_git_dir_specs(defn, "/ws"),
                ["/x/.git:/x/.git"],
            )


# Otherwise a developer's global signing key or templateDir shapes these repos.
_GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL="/dev/null",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_AUTHOR_NAME="Test",
    GIT_AUTHOR_EMAIL="test@example.com",
    GIT_COMMITTER_NAME="Test",
    GIT_COMMITTER_EMAIL="test@example.com",
)


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd] + list(args), check=True,
                   capture_output=True, text=True, env=_GIT_ENV)


class WorktreeTongMountTests(unittest.TestCase):
    """A tong mounting a linked-worktree workspace sees the external git dir.

    The worktree's `.git` is a pointer file naming a git dir under the main
    checkout, so the workspace bind alone leaves git inside the tong with
    "fatal: not a git repository". The launcher must pair the bind with the
    same git-dir mounts the anvil gets: the external git dir at its own host
    path, plus the read-only guards.
    """

    def setUp(self):
        # realpath: git reports resolved paths, and the guard compares them.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="tong-worktree-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root")
        self.worktree = os.path.join(self.tmp, "wt")
        _git(self.repo, "worktree", "add", "-q", self.worktree)
        self.common = os.path.join(self.repo, ".git")

    def _mounted(self, mounts):
        defn = {
            "lifecycle": "session",
            "image": "git-signing",
            "mounts": mounts,
            "interface": {"kind": "none"},
            "readiness": {"mode": "none"},
        }
        docker = FakeDocker()
        rc = launcher.run_with_tongs(
            _merged("sign", defn, source=tongs.USER), ANVIL_ARGV,
            _opts(workspace=self.worktree),
            docker=docker, sleep=lambda _s: None, monotonic=_Clock(),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        return [started[i + 1] for i, part in enumerate(started) if part == "-v"]

    def test_external_git_dir_mounted_at_its_own_path(self):
        mounted = self._mounted(["workspace"])
        self.assertIn("%s:/workspace" % self.worktree, mounted)
        # The git dir the worktree's `.git` pointer names, at the path it names.
        self.assertIn("%s:%s" % (self.common, self.common), mounted)
        # The guards ride along: host-obeyed config stays read-only in the tong.
        self.assertIn("%s/config:%s/config:ro" % (self.common, self.common), mounted)
        self.assertIn("%s/.git:/workspace/.git:ro" % self.worktree, mounted)

    def test_read_only_workspace_gets_read_only_git_dir(self):
        mounted = self._mounted(["workspace:ro"])
        self.assertIn("%s:/workspace:ro" % self.worktree, mounted)
        self.assertIn("%s:%s:ro" % (self.common, self.common), mounted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
