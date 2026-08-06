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

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Aliased because `anvil` is already these tests' word for the container
# the launcher wraps.
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
        # A `session` tong reached over the network (or with no surface) is now
        # startable -- it runs on a per-session network.
        self.assertEqual(
            self._reasons({
                "lifecycle": "session", "image": "x",
                "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"},
            }),
            [],
        )

    def test_volume_refused(self):
        # A `volume` interface (a shared named volume) has no anvil-side consumer
        # yet, so it remains refused.
        self.assertTrue(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "volume", "volume": "v", "mountpoint": "/m"},
             "readiness": {"mode": "none"}}))

    def test_mcp_tong_is_now_startable(self):
        # An `mcp` tong is reached via generated MCP config, so it is no longer
        # refused -- on either lifecycle.
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x",
             "interface": {"kind": "mcp", "name": "g", "port": 8080},
             "readiness": {"mode": "none"}}), [])

    def test_secret_tong_is_now_startable(self):
        # Secrets are resolved and delivered as env over a FIFO, so a tong that references
        # one (and is otherwise reachable over the network or has no surface) is no
        # longer refused -- on either lifecycle.
        self.assertEqual(self._reasons(
            {"lifecycle": "shared", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "none"}, "readiness": {"mode": "none"}}), [])
        self.assertEqual(self._reasons(
            {"lifecycle": "session", "image": "x", "env": {"T": "${secret:op:r}"},
             "interface": {"kind": "port", "port": 5432}, "readiness": {"mode": "none"}}), [])

    def test_shared_workspace_mount_refused_but_docker_socket_allowed(self):
        # A shared tong that mounts the workspace leaks it across sessions, so it
        # is refused; the docker-socket mount (the broker pattern) is not.
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

    def test_shared_workspace_refusal_sees_a_custom_target(self):
        # The leak is the same wherever the workspace lands inside the container,
        # so the refusal keys on the magic word, not on the whole mount string.
        self.assertTrue(any(
            "workspace" in r for r in self._reasons({
                "lifecycle": "shared", "image": "x", "mounts": ["workspace:/code:ro"],
                "interface": {"kind": "none"}, "readiness": {"mode": "none"},
            })
        ))

    def test_workspace_refusal_is_shared_scoped(self):
        # The workspace-mount leak is a `shared`-reuse hazard, so a `session` tong
        # that mounts the workspace is legitimate (it is torn down with the anvil)
        # and must NOT be refused -- only a `shared` one is.
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
                 image_config=(["app"], [], "")):
        self.calls = []
        self._states = states or {}      # container -> inspect_state dict
        self._ready = ready
        self._anvil_rc = anvil_rc
        self._image_config = image_config
        self.run_argvs = []              # detached `docker run` argvs
        self.inspected_images = []       # images whose exec config was read
        self.anvil_argv = None           # set when the anvil runs
        self.anvil_extra_networks = None  # extra networks the anvil joined

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
    """A `make_channel` stand-in that records secret deliveries without a FIFO.

    Records the uid each channel is opened for, every payload delivered, and how
    many channels were cleaned up, so a test can assert what reached the tong (and
    that the secret never went through `-e`/argv) without touching the filesystem.
    `deliver_error`, if set, is raised from `deliver` to exercise the failure path.
    """

    def __init__(self, deliver_error=None):
        self.uids = []
        self.payloads = []
        self.cleanups = 0
        self._deliver_error = deliver_error

    def __call__(self, uid=None):
        self.uids.append(uid)
        return FakeChannels._Channel(self)

    class _Channel:
        host_path = "/fake/swarmforge-secret/secret-env"

        def __init__(self, owner):
            self._owner = owner

        def deliver(self, payload, **kwargs):
            self._owner.payloads.append(payload)
            if self._owner._deliver_error is not None:
                raise self._owner._deliver_error

        def cleanup(self):
            self._owner.cleanups += 1


# Tiny launcher options for driving run_with_tongs directly.
def _opts(workspace=None, anvil_image="anvil:img", harness="opencode"):
    return launcher.LauncherOptions(
        layer_dirs=[], workspace=workspace, approvals=None, providers=None,
        harness=harness, anvil_image=anvil_image, no_prompt=False,
    )


# A counter clock so readiness loops never sleep on the wall clock in tests.
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

# A background side-effect tong with no anvil-facing surface and no probe.
SHARED_NONE = {
    "lifecycle": "shared",
    "image": "log-shipper",
    "interface": {"kind": "none"},
    "readiness": {"mode": "none"},
}

# A credential-holding MCP tong: an HTTP MCP server the anvil reaches at its
# canonical alias (interface.name) on the session/base network.
SHARED_MCP = {
    "lifecycle": "shared",
    "image": "github-tong",
    "interface": {"kind": "mcp", "name": "github", "port": 8080},
    "readiness": {"mode": "none"},
}

# An org-owned credential-holding MCP tong: the user's reported case. Two orgs
# ship this same file with different credentials; each must run partitioned.
ORG_ASANA = {
    "lifecycle": "shared",
    "image": "asana-mcp:latest",
    "interface": {"kind": "mcp", "name": "asana-mcp", "port": 3000},
    "readiness": {"mode": "none"},
}

# A per-session network service (a throwaway fixture DB) reached by host+port.
SESSION_PORT = {
    "lifecycle": "session",
    "image": "fixture-pg",
    "interface": {"kind": "port", "port": 5432},
    "readiness": {"mode": "none"},
}

# A secret provider built on the test interpreter (so the suite needs no op/pass
# installed): it echoes the {ref} it is handed, so ${secret:echo:VALUE} resolves
# to "VALUE".
ECHO_PROVIDERS = {
    "echo": [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", "{ref}"]
}

# A credential-holding shared tong: its token is a secret reference, delivered to
# the running container as env over a FIFO rather than passed as a docker env var.
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
            self.assertEqual(os.listdir(tmp), [])  # no file written

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
        # ollama-shape shared tong on the anvil's base network: it is started
        # there under its canonical alias, then the anvil runs on that network.
        docker = FakeDocker()
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 0)
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertIn("ollama", started)  # network-alias
        # The anvil ran on the unchanged base network.
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_shared_tong_reused_when_running_and_hash_matches(self):
        defn = SHARED_OLLAMA
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(defn)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", defn, source=tongs.REPO))
        self.assertEqual(docker.run_argvs, [])  # reused, not restarted

    def test_unusable_mount_reported_as_a_config_error_naming_the_tong(self):
        # A mount the argv builder refuses ends the launch as a named config error
        # rather than a traceback, and nothing is started or removed.
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
        # No running container of that name => start fresh (rm_force clears any
        # stopped leftover first).
        docker = FakeDocker()
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_stopped_shared_tong_is_recreated(self):
        # A container exists by name but is not running (a stale leftover) =>
        # recreate even though its label happens to match.
        states = {"swarmforge-shared-ollama":
                  {"running": False, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertIn(("rm_force", "swarmforge-shared-ollama"), docker.calls)
        self.assertEqual(len(docker.run_argvs), 1)

    def test_multiple_shared_tongs_started_and_injected(self):
        # Two shared tongs in one launch: both are started and both contribute
        # their reachability to the anvil.
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
        # A `none` shared tong has no anvil-facing surface, so nothing is injected
        # and the anvil command is exactly what the macro built.
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
        # An OpenCode session reaches an `mcp` tong via the entrypoint merge: the
        # generated config is bind-mounted read-only and pointed at by the env var.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="opencode")
        argv = docker.anvil_argv
        self.assertIn("github", docker.run_argvs[0])  # tong started under its alias
        self.assertIn(
            "%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH), argv
        )
        self._mcp_mount_host_path(argv)  # the read-only mount is present
        self.assertNotIn("--mcp-config", argv)  # OpenCode does not use the flag

    def test_claude_mcp_tong_mounts_config_and_appends_flag(self):
        # A Claude session reads the generated config directly via --mcp-config,
        # appended after the image so it reaches the harness binary.
        docker = FakeDocker()
        self._run(docker, _merged("github-creds", SHARED_MCP, source=tongs.REPO),
                  harness="claude")
        argv = docker.anvil_argv
        self.assertEqual(argv[-2:], ["--mcp-config", launcher.MCP_CONFIG_CONTAINER_PATH])
        self.assertNotIn("%s=%s" % (launcher.MCP_FILE_ENV, launcher.MCP_CONFIG_CONTAINER_PATH),
                         argv)
        self._mcp_mount_host_path(argv)  # the read-only mount is present

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
        # The generated config lives in a host temp dir bind-mounted into the
        # anvil; once the anvil exits the temp dir is removed.
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
        self.assertIsNone(docker.anvil_argv)  # anvil never ran

    def test_anvil_exit_code_is_returned(self):
        docker = FakeDocker(anvil_rc=42)
        rc = self._run(docker, _merged("ollama", SHARED_OLLAMA, source=tongs.REPO))
        self.assertEqual(rc, 42)

    def test_no_anvil_image_degrades_tcp_to_running_check(self):
        # Without an anvil image a TCP probe cannot dial the tong's port, so it
        # falls back to "is the container running" using inspect_state.
        states = {"swarmforge-shared-ollama": {"running": True, "label": tongs.config_hash(SHARED_OLLAMA)}}
        docker = FakeDocker(states=states)
        rc = launcher.run_with_tongs(
            _merged("ollama", SHARED_OLLAMA, source=tongs.REPO), ANVIL_ARGV,
            _opts(anvil_image=None), docker=docker,
            sleep=lambda _s: None, monotonic=_Clock(),
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("tcp_probe", [c[0] for c in docker.calls])

    # --- Secret resolution + FIFO env delivery ------------------------------

    def _run_secret(self, docker, merged, providers, channels=None):
        return launcher.run_with_tongs(
            merged, ANVIL_ARGV, _opts(), docker=docker, providers=providers,
            make_channel=channels or FakeChannels(),
            sleep=lambda _s: None, monotonic=_Clock(),
        )

    def test_secret_delivered_as_env_via_fifo_never_in_argv(self):
        # A resolved secret is handed to the tong over the FIFO (an `export`
        # script), never as a docker `-e` value; the run argv carries only the
        # entrypoint wrapper and the read-only FIFO bind, not the secret.
        docker = FakeDocker(image_config=(["node"], ["server.js"], ""))
        channels = FakeChannels()
        rc = self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(rc, 0)
        started = docker.run_argvs[0]
        # Entrypoint is wrapped and the FIFO is bind-mounted read-only.
        self.assertEqual(started[started.index("--entrypoint") + 1], "/bin/sh")
        self.assertIn(
            "/fake/swarmforge-secret/secret-env:/run/swarmforge/secret-env:ro", started
        )
        # The image's real argv is what the wrapper execs (after the image token).
        self.assertEqual(started[started.index("github-tong") + 1:],
                         ["-c", started[started.index("-c") + 1],
                          "swarmforge-tong", "node", "server.js"])
        # The secret is nowhere in the argv -- it only went through the channel.
        self.assertNotIn("s3cr3t", " ".join(started))
        self.assertNotIn("GITHUB_TOKEN=s3cr3t", started)
        self.assertEqual(channels.payloads, ["export GITHUB_TOKEN='s3cr3t'\n"])
        self.assertEqual(channels.cleanups, 1)  # FIFO cleaned up after delivery

    def test_secret_tong_reads_exec_target_from_image(self):
        docker = FakeDocker(image_config=(["entry"], ["arg"], ""))
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS
        )
        self.assertEqual(docker.inspected_images, ["github-tong"])

    def test_unresolvable_secret_stops_launch_before_anvil(self):
        # No provider for the referenced scheme => resolution fails before the tong
        # even starts, and the anvil never runs.
        docker = FakeDocker()
        with self.assertRaises(launcher.SecretResolutionError):
            self._run_secret(docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), {})
        self.assertEqual(docker.run_argvs, [])  # never reached the start
        self.assertIsNone(docker.anvil_argv)

    def test_delivery_failure_removes_half_configured_container(self):
        # If delivery over the FIFO fails after the container started, the
        # container is removed before raising, so a `shared` tong is not left
        # stamped with its config-hash label (and reused) while missing its secret.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=launcher.DockerError("boom"))
        with self.assertRaises(launcher.DockerError):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        # rm_force fires twice: clearing any leftover before start, then removing
        # the half-configured container after the failed delivery.
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertEqual(channels.cleanups, 1)  # FIFO still cleaned up
        self.assertIsNone(docker.anvil_argv)

    def test_unusable_mount_target_reported_and_starts_nothing(self):
        # The secret-bearing path: a refused mount is reported as the config error
        # it is, and having started nothing it removes nothing.
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
        self.assertEqual(channels.cleanups, 1)  # FIFO still cleaned up
        self.assertIsNone(docker.anvil_argv)

    def test_interrupt_during_delivery_removes_half_configured_container(self):
        # Ctrl-C while delivering must still remove the container, or a `shared`
        # tong (stamped with its config-hash label and not tracked for session
        # teardown) would be reused next session with a missing secret.
        docker = FakeDocker()
        channels = FakeChannels(deliver_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self._run_secret(
                docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
                channels=channels,
            )
        self.assertEqual(docker.calls.count(("rm_force", "swarmforge-shared-gh")), 2)
        self.assertEqual(channels.cleanups, 1)
        self.assertIsNone(docker.anvil_argv)

    def test_reused_shared_tong_never_resolves_or_delivers_secrets(self):
        # A running shared tong whose hash matches is reused untouched -- deciding
        # to reuse must never invoke a secret-provider CLI (which could prompt for
        # an unlock every session) or open a channel.
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
        self.assertEqual(docker.run_argvs, [])   # reused, not restarted
        self.assertEqual(channels.payloads, [])  # no resolution, no delivery
        self.assertEqual(docker.inspected_images, [])  # no image inspect either

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

    def test_secret_uid_passed_to_channel_factory(self):
        # The image's numeric user is passed to the channel factory so the FIFO can
        # be chowned to the uid that will read it.
        docker = FakeDocker(image_config=(["app"], [], "1000"))
        channels = FakeChannels()
        self._run_secret(
            docker, _merged("gh", SHARED_SECRET, source=tongs.REPO), ECHO_PROVIDERS,
            channels=channels,
        )
        self.assertEqual(channels.uids, [1000])

    # --- Session lifecycle + per-session networks ---------------------------

    def test_shared_only_keeps_base_network_and_plain_run(self):
        # No `session` tong => no per-session network is created and the anvil runs
        # on the base network through the plain (single-network) foreground path.
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
        # The session tong is started on the per-session network under its alias.
        self.assertEqual(len(docker.run_argvs), 1)
        started = docker.run_argvs[0]
        self.assertIn("claude-myproject-tong-pg", started)
        self.assertEqual(started[started.index("--network") + 1], net)
        self.assertEqual(started[started.index("--network-alias") + 1], "pg")
        # The anvil joined the session network (primary) and the base network
        # (extra) via the create -> connect -> start path, and got the port env.
        self.assertEqual(docker.anvil_argv[docker.anvil_argv.index("--network") + 1], net)
        self.assertEqual(docker.anvil_extra_networks, ["opencode-net"])
        self.assertIn("SWARMFORGE_TONG_PG_HOST=pg", docker.anvil_argv)
        # Teardown removes the session tong and the anvil, then the network -- the
        # network rm must come after its endpoints are gone or docker refuses it.
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
        # Each tong answers on the per-session network under every DNS name it
        # declares: a session tong registers them at start, a shared tong when it
        # is connected to the session network.
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
        # A `shared` tong alongside a `session` tong is ensured on the base network,
        # then connected to the per-session network for the anvil to reach; on
        # teardown it is disconnected but never removed.
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
        # The connect is idempotent against a reused network: a best-effort
        # disconnect precedes it (a no-op when the tong is not already attached).
        self.assertLess(
            docker.calls.index(("network_disconnect", net, "swarmforge-shared-ollama")),
            docker.calls.index(("network_connect", net, "swarmforge-shared-ollama", ("ollama",))),
        )
        # The shared tong is rm_force'd only once -- when (re)started to clear a
        # leftover -- never as part of teardown, so it is left running.
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
        # Ctrl-C mid-session must still tear down the session tong and network so an
        # interrupted run leaks neither.
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
        # An org-owned shared tong starts on its own per-org network (never the
        # shared base network), and the anvil joins that network as an extra.
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
        # The anvil keeps opencode-net as its primary (for the model backend) and
        # joins the org network as an extra via the multi-network path.
        self.assertEqual(docker.anvil_extra_networks, [net])
        self.assertEqual(
            docker.anvil_argv[docker.anvil_argv.index("--network") + 1], "opencode-net"
        )

    def test_org_shared_tong_readiness_probes_on_org_network(self):
        # A scoped shared tong with a tcp probe is checked on its org network --
        # the only network it lives on -- not on the anvil's base network.
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
        # The crux: the same tong file in two orgs yields distinct containers and
        # distinct networks (so neither tears the other down, and neither is
        # reachable from the other), while the agent-facing MCP server name
        # (interface.name) stays identical in both.
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
        # A repo-sourced shared tong keeps the base network and unscoped name even
        # when the launch also carries an org layer dir -- only org-owned shared
        # tongs are partitioned.
        docker = FakeDocker()
        merged = {"ollama": {"source": tongs.REPO, "definition": SHARED_OLLAMA}}
        self._run_org(docker, merged, self._ACME)
        started = docker.run_argvs[0]
        self.assertIn("swarmforge-shared-ollama", started)
        self.assertEqual(started[started.index("--network") + 1], "opencode-net")
        self.assertNotIn("ensure_network", [c[0] for c in docker.calls])
        self.assertIsNone(docker.anvil_extra_networks)

    def test_org_shared_network_pruned_best_effort_and_tong_left_running(self):
        # On teardown the org network is pruned best-effort (docker refuses while
        # the long-lived tong is attached, so it persists), and the shared tong is
        # force-removed only once -- at start, to clear a leftover -- never as a
        # teardown step.
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
        # build_mounts emits the git-dir binds writable (the anvil's workspace is
        # writable); under a workspace:ro definition they must not open a write path.
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


# git runs with the developer's global config otherwise, where a signing key or
# a templateDir would change what these repos come out looking like.
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
