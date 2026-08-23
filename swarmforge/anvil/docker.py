"""The single seam every docker invocation goes through.

Every docker invocation goes through `DockerCLI` so the orchestration logic can
be unit-tested against a fake. The methods are thin wrappers; the launch
sequencing and policy live in `run_with_tongs`. `_run` defaults to
subprocess.run and is the single injection point for tests.
"""

import json
import subprocess

from swarmforge import tongs


class DockerError(Exception):
    """A docker command the launch depends on failed; the launch must stop."""


# Labels read back to decide whether a running `shared` container is stale.
_INSPECT_STATE_FORMAT = (
    '{{.State.Running}}|{{index .Config.Labels "%s"}}' % tongs.LABEL_CONFIG_HASH
)
_INSPECT_HEALTH_FORMAT = "{{if .State.Health}}{{.State.Health.Status}}{{end}}"


class DockerCLI:
    def __init__(self, run=None):
        self._run = run or subprocess.run

    def _quiet(self, argv):
        """Run a command whose output we don't need; return its exit code."""
        return self._run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode

    def _checked(self, argv):
        """Run a command the launch depends on; raise DockerError on failure."""
        try:
            completed = self._run(argv, stdout=subprocess.DEVNULL)
        except OSError as exc:
            raise DockerError("could not run %r: %s" % (argv[:3], exc))
        if completed.returncode != 0:
            raise DockerError(
                "docker command failed (exit %d): %s"
                % (completed.returncode, " ".join(argv[:4]))
            )

    def rm_force(self, container):
        self._quiet(["docker", "rm", "-f", container])

    def run_detached(self, argv):
        self._checked(argv)

    def inspect_state(self, container):
        """`{"running": bool, "label": str|None}` for a container, or None if absent."""
        completed = self._run(
            ["docker", "inspect", "--format", _INSPECT_STATE_FORMAT, container],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return None
        running, _, label = _decode(completed.stdout).strip().partition("|")
        return {"running": running == "true", "label": label or None}

    def health_status(self, container):
        completed = self._run(
            ["docker", "inspect", "--format", _INSPECT_HEALTH_FORMAT, container],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return None
        return _decode(completed.stdout).strip() or None

    def exec_ok(self, container, command):
        return self._quiet(["docker", "exec", container] + list(command)) == 0

    def exec_stdin(self, container, command, payload, timeout=None):
        """`(exit code, stderr)` of `docker exec -i <container> <command>` fed `payload`.

        The secret-delivery transport: `payload` (bytes) goes to the exec's
        stdin, which docker carries over its API stream -- it appears nowhere in
        any argv or inspectable config. The exit code is None when the exec does
        not finish within `timeout` seconds (the client is killed;
        `SecretChannel.deliver` turns that into a delivery timeout). `stderr` is
        the docker CLI's own chatter ("is not running", daemon errors), kept so
        a fatal delivery failure can say which of those it was.
        """
        try:
            completed = self._run(
                ["docker", "exec", "-i", container] + list(command),
                input=payload, timeout=timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            return None, ""
        return completed.returncode, _decode(completed.stderr or b"").strip()

    def image_exec_config(self, image):
        """`(entrypoint, cmd)` from an image's config, pulling it if absent.

        Used to reconstruct the process a secret-injecting tong must `exec` once
        its `/bin/sh` wrapper has loaded the secret env: overriding `--entrypoint`
        for the wrapper drops the image's own entrypoint/command, so they are read
        back here. Both are argv lists (possibly empty). A missing image is pulled
        once before retrying; a still-missing or unreadable image is a `DockerError`.
        """
        info = self._inspect_image(image)
        if info is None:
            self._checked(["docker", "pull", image])
            info = self._inspect_image(image)
        if info is None:
            raise DockerError("cannot read image config for %r" % image)
        return info

    def _inspect_image(self, image):
        fmt = "{{json .Config.Entrypoint}}\n{{json .Config.Cmd}}"
        completed = self._run(
            ["docker", "image", "inspect", "--format", fmt, image],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return None
        lines = _decode(completed.stdout).splitlines()
        if len(lines) < 2:
            return None
        entrypoint = json.loads(lines[0]) or []
        cmd = json.loads(lines[1]) or []
        return entrypoint, cmd

    def tcp_probe(self, network, host, port, image):
        """True if `host:port` accepts a TCP connection from within `network`.

        Runs a throwaway container on the network -- the anvil image, which has
        python3 -- since a tong's own port is only reachable over the docker
        network, not from the host.
        """
        script = (
            "import socket,sys\n"
            "s=socket.socket()\n"
            "s.settimeout(2)\n"
            "try:\n"
            "    s.connect((sys.argv[1], int(sys.argv[2])))\n"
            "except OSError:\n"
            "    sys.exit(1)\n"
        )
        argv = ["docker", "run", "--rm", "--network", network,
                "--entrypoint", "python3", image, "-c", script, host, str(port)]
        return self._quiet(argv) == 0

    def ensure_network(self, name):
        """Create the per-session docker network unless it already exists.

        Mirrors the Makefile's inspect-or-create so a leftover network from a
        crashed session (whose teardown never ran) is reused rather than failing
        the launch.
        """
        if self._quiet(["docker", "network", "inspect", name]) == 0:
            return
        self._checked(["docker", "network", "create", name])

    def network_connect(self, network, container, aliases=()):
        """Attach a running container to `network` under each of `aliases`.

        Used to connect a long-lived `shared` tong to a session network under the
        DNS names it answers to, so the session reaches it without the tong having
        to live on the session network permanently.
        """
        argv = ["docker", "network", "connect"]
        for alias in aliases:
            argv += ["--alias", alias]
        self._checked(argv + [network, container])

    def network_disconnect(self, network, container):
        """Detach a container from a network (best-effort, for teardown)."""
        self._quiet(["docker", "network", "disconnect", network, container])

    def network_rm(self, network):
        """Remove a network (best-effort, for teardown)."""
        self._quiet(["docker", "network", "rm", network])

    def run_foreground(self, argv):
        """Run the anvil in the foreground and return its exit code.

        Popen + wait (rather than exec) so the launcher regains control after the
        anvil exits. On Ctrl-C the SIGINT reaches both this process and the anvil
        through the controlling terminal's process group; the anvil handles it and
        exits, we reap it, and the KeyboardInterrupt propagates to the caller.
        """
        return self._wait_foreground(argv)

    def run_foreground_multi(self, argv, extra_networks, container):
        """Create the anvil, join the extra networks, then start it attached.

        `docker run` attaches only one network at creation, so an anvil that joins
        both its per-session network and a pre-existing `NETWORK=` network is
        created on its primary (per-session) network, connected to each extra
        network, then started in the foreground. Returns the anvil's exit code.
        The container is left for the caller's teardown to remove, so a created
        container is not orphaned if `connect` or `start` fails before its `--rm`
        could fire.
        """
        self._checked(tongs.to_create_argv(argv))
        for network in extra_networks:
            self._checked(["docker", "network", "connect", network, container])
        return self._wait_foreground(
            ["docker", "start", "--attach", "--interactive", container]
        )

    def _wait_foreground(self, argv):
        """Run a foreground command, reaping it on Ctrl-C before re-raising.

        Popen + wait (rather than exec) so the launcher regains control after the
        process exits. On Ctrl-C the SIGINT reaches both this process and the child
        through the controlling terminal's process group; the child handles it and
        exits, we reap it, and the KeyboardInterrupt propagates to the caller.
        """
        try:
            proc = subprocess.Popen(argv)
        except OSError as exc:
            raise DockerError("cannot run anvil %r: %s" % (argv[:2], exc))
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.wait()
            raise


def _decode(output):
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace")
    return output or ""
