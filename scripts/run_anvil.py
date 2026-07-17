#!/usr/bin/env python3
"""Host-side launcher that wraps an anvil (harness container) run.

The Makefile resolves the docker-run argv for an anvil (`run_opencode` /
`run_claude`) and the host paths of the four tong definition layers, then
delegates the actual launch to this script:

    run_anvil.py [--user-tongs DIR] [--org-tongs DIR] [--repo-tongs DIR]
                 [--workspace-tongs DIR] [--workspace PATH] [--approvals PATH]
                 [--anvil-image IMAGE] [--no-prompt] -- docker run -it --rm ... <image> ...

Tongs are sibling containers that must be orchestrated from the host (they are
started alongside the anvil, not from inside it), which is why this wrapper sits
between Make and `docker run`. It discovers tong definitions across the four
layers using the pure core in `tongs.py`, then runs the anvil.

Tong lifecycles
---------------
When a tong is discovered, the launcher starts it before the anvil, waits for it
to report ready, makes it reachable from the anvil, then runs the anvil in the
foreground. A `shared` tong is one long-lived container keyed by a stable name: a
running one whose config-hash label still matches is reused untouched, a
missing/stopped/stale one is (re)started, and it is left running afterwards. A
`session` tong is per-session: when any exists the launcher creates a per-session
network, starts the `session` tongs on it under their canonical aliases, connects
each network-facing `shared` tong to it, and joins the anvil to it (plus the base
`NETWORK=` network). On exit -- including SIGINT -- the `session` tongs and the
per-session network are torn down (and the connected `shared` tongs disconnected)
while the long-lived `shared` tongs keep running. A `port` tong's reachability is
injected into the anvil as environment; an `mcp` tong's as generated MCP config
(see "MCP config"); a `none` tong has no anvil-facing surface.

A tong's secret references are resolved on the host (see "Secret delivery") and
handed to the tong as environment, so the launcher starts `shared` and `session`
tongs reached over the network (`mcp`/`port`) or with no anvil-facing surface
(`none`), with or without secrets. A `volume` interface, or a `shared` tong that
mounts the workspace, is refused with a clear message rather than started
half-wired.

MCP config
----------
An `mcp` tong is an HTTP MCP server reachable at its canonical alias on the
session network. The launcher generates the per-harness MCP config (an
`opencode.json` `mcp` fragment for OpenCode, an `mcpServers` document for Claude
Code) for the discovered `mcp` tongs, writes it to a host temp file mounted
read-only into the anvil, and points the harness at it: OpenCode's entrypoint
merges the fragment via `SWARMFORGE_TONG_MCP_FILE`, while Claude Code is passed
`--mcp-config <path>`. With no `mcp` tongs nothing is written, mounted, or
appended, so the anvil argv is unchanged.

Secret delivery
---------------
A tong's `env:` values may carry `${secret:<provider>:<ref>}` references. The
launcher resolves them on the host by shelling out to the provider CLIs declared
in the user-layer table passed as `--providers` (defaulting to
`~/.swarmforge/secret-providers.yaml`), so interactive unlocks happen in the
user's terminal before the anvil starts. A resolved secret is never passed to a
tong as a docker `-e` env var (anything holding the docker socket could read it
back via `docker inspect`), never a command-line argument, and never written to
disk. Instead the launcher creates a host FIFO, bind-mounts it read-only into the
tong, and overrides the tong's entrypoint with a `/bin/sh` wrapper that reads the
FIFO, exports each `NAME=value` into its environment, then execs the image's real
entrypoint+command (looked up via `docker inspect`, or declared as
`entrypoint:`/`command:` on the tong). The launcher writes the resolved values
into the FIFO only once the wrapper has opened the read end, so the secrets reach
the real process as ordinary environment variables -- present before it starts,
since the wrapper blocks on the FIFO until delivery -- while the bytes only ever
live in the kernel pipe buffer. A tong with secret env therefore needs a `/bin/sh`
in its image; one without secrets runs its image entrypoint unchanged.

First-run approval
------------------
The user, org, and Swarmforge-repo layers are installed deliberately and are
trusted. The workspace is any repo you happened to clone, so a workspace-sourced
tong -- which may request secrets, host mounts, or the docker socket -- is gated:
before the anvil starts, the launcher prints the privilege summary and asks the
user to approve it. Approval is keyed by workspace path + tong name + a hash of
the merged definition (so any change re-prompts) and persists in the user-layer
store passed as `--approvals`. The scripted `--no-prompt` mode fails closed
(refusing the run) rather than auto-approving an unapproved tong.

Passthrough invariant
---------------------
With **no tong definitions discovered across all four layers**, the launcher
execs the anvil argv verbatim -- byte-identical to the direct `docker run` Make
would otherwise have issued. Existing repos ship no tong layers, so discovery is
empty, the approval gate sees nothing to gate, and this wrapper is a transparent
exec. `scripts/test_run_anvil.py` asserts this byte-for-byte.

The anvil argv (everything after `--`) is forwarded to `os.execvp` unchanged, so
the anvil process replaces this one and keeps the controlling tty, signal
delivery, and `--rm` cleanup it had before.
"""

import collections
import errno
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Load the pure core (layer discovery + name-based merge) by path, the same way
# tongs.py loads translate_agents.py, so the launcher needs no package install
# and no assumptions about the current working directory.
_TONGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tongs.py")
_spec = importlib.util.spec_from_file_location("tongs", _TONGS_PATH)
tongs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tongs)


USAGE = (
    "usage: run_anvil.py [--user-tongs DIR] [--org-tongs DIR] "
    "[--repo-tongs DIR] [--workspace-tongs DIR] [--workspace PATH] "
    "[--approvals PATH] [--providers PATH] [--harness NAME] "
    "[--anvil-image IMAGE] [--no-prompt] -- <anvil command>"
)

# Each flag names the host directory for one definition layer. The merge always
# orders layers canonically (LAYERS, lowest to highest precedence) regardless of
# the order the flags are passed.
LAYER_FLAGS = {
    "--user-tongs": tongs.USER,
    "--org-tongs": tongs.ORG,
    "--repo-tongs": tongs.REPO,
    "--workspace-tongs": tongs.WORKSPACE,
}

# Parsed launcher options. `workspace` is the workspace root used to key approval
# of workspace-sourced tongs and to resolve the `workspace` mount word; `approvals`
# is the approvals store path and `providers` the secret-provider table path (both
# default-resolved in main); `harness` names the anvil harness (`opencode` /
# `claude`) so the MCP config for `mcp` tongs is emitted in that harness's shape;
# `anvil_image` is the image the readiness prober runs to dial a tong's
# network-internal port; `no_prompt` makes the approval gate fail closed for
# scripted runs.
LauncherOptions = collections.namedtuple(
    "LauncherOptions",
    ["layer_dirs", "workspace", "approvals", "providers", "harness", "anvil_image",
     "no_prompt"],
)


class UsageError(ValueError):
    """Raised for malformed launcher arguments (reported, then exit 2)."""


def parse_args(argv):
    """Split launcher options from the anvil command at the first ``--``.

    Returns ``(options, anvil_cmd)`` where ``options`` is a ``LauncherOptions``
    (its ``layer_dirs`` ordered by canonical precedence, only the layers that
    were given) and ``anvil_cmd`` is the argv after ``--``. Raises ``UsageError``
    if the separator is missing, the command is empty, or an option is malformed.
    """
    paths = {}
    workspace = None
    approvals = None
    providers = None
    harness = None
    anvil_image = None
    no_prompt = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            anvil_cmd = list(argv[index + 1:])
            if not anvil_cmd:
                raise UsageError("missing anvil command after '--'")
            layer_dirs = [(layer, paths[layer]) for layer in tongs.LAYERS if layer in paths]
            return (
                LauncherOptions(
                    layer_dirs, workspace, approvals, providers, harness,
                    anvil_image, no_prompt
                ),
                anvil_cmd,
            )
        if token in LAYER_FLAGS:
            if index + 1 >= len(argv):
                raise UsageError("%s requires a directory argument" % token)
            paths[LAYER_FLAGS[token]] = argv[index + 1]
            index += 2
            continue
        if token == "--workspace":
            if index + 1 >= len(argv):
                raise UsageError("--workspace requires a path argument")
            workspace = argv[index + 1]
            index += 2
            continue
        if token == "--approvals":
            if index + 1 >= len(argv):
                raise UsageError("--approvals requires a path argument")
            approvals = argv[index + 1]
            index += 2
            continue
        if token == "--providers":
            if index + 1 >= len(argv):
                raise UsageError("--providers requires a path argument")
            providers = argv[index + 1]
            index += 2
            continue
        if token == "--harness":
            if index + 1 >= len(argv):
                raise UsageError("--harness requires a name argument")
            harness = argv[index + 1]
            index += 2
            continue
        if token == "--anvil-image":
            if index + 1 >= len(argv):
                raise UsageError("--anvil-image requires an image argument")
            anvil_image = argv[index + 1]
            index += 2
            continue
        if token == "--no-prompt":
            no_prompt = True
            index += 1
            continue
        raise UsageError("unexpected argument %r" % token)
    raise UsageError("missing '--' separating launcher options from the anvil command")


def default_approvals_path():
    """Path to the approvals store in the user layer when none is passed.

    Mirrors the Makefile's `SWARMFORGE_USER_ASSETS_DIR` default (~/.swarmforge),
    so the launcher and Make agree on where approvals live even if Make does not
    pass `--approvals` explicitly.
    """
    base = os.environ.get("SWARMFORGE_USER_ASSETS_DIR") or os.path.join(
        os.path.expanduser("~"), ".swarmforge"
    )
    return os.path.join(base, "approvals.json")


def default_providers_path():
    """Path to the secret-provider table in the user layer when none is passed.

    Mirrors the Makefile's `SWARMFORGE_USER_ASSETS_DIR` default (~/.swarmforge),
    so the launcher finds `secret-providers.yaml` even if Make does not pass
    `--providers` explicitly. A missing file means no providers configured, which
    only matters if a tong actually references a secret.
    """
    base = os.environ.get("SWARMFORGE_USER_ASSETS_DIR") or os.path.join(
        os.path.expanduser("~"), ".swarmforge"
    )
    return os.path.join(base, "secret-providers.yaml")


def discover_tongs(layer_dirs):
    """Merged tong set across the given layers ({} when none are present)."""
    return tongs.merge_tongs(tongs.discover(layer_dirs))


# --- First-run approval -------------------------------------------------------


class ApprovalDenied(Exception):
    """A workspace-sourced tong was not approved; the launch must not proceed."""


def render_privilege_summary(name, summary):
    """Human-readable block describing what a workspace tong requests.

    `summary` is the structured output of `tongs.privilege_summary`. Only the
    privileges actually requested are shown, and docker-socket access -- the
    broadest grant, since it is full control of the host's docker -- is always
    called out explicitly so it cannot be approved unseen.
    """
    lines = ["Workspace tong %r requests approval:" % name]
    lines.append("  image:    %s" % (summary.get("image") or "(none declared)"))
    secrets = summary.get("secrets") or []
    if secrets:
        rendered = ", ".join("%s:%s" % (s["provider"], s["ref"]) for s in secrets)
        lines.append("  secrets:  %s" % rendered)
    mounts = summary.get("mounts") or []
    if mounts:
        lines.append("  mounts:   %s" % ", ".join(str(m) for m in mounts))
    networks = summary.get("networks") or []
    if networks:
        lines.append("  networks: %s" % ", ".join(str(n) for n in networks))
    if summary.get("socket"):
        lines.append("  docker socket: full host docker control")
    return "\n".join(lines)


def _prompt_yes_no(question, out, inp):
    """Ask a yes/no question on `out`/`inp`, defaulting to No.

    EOF (a closed or non-interactive stdin) reads as No, so a gate that cannot
    actually ask the user never silently approves.
    """
    out.write("%s [y/N]: " % question)
    out.flush()
    answer = inp.readline()
    if not answer:
        out.write("\n")
        return False
    return answer.strip().lower() in ("y", "yes")


def gate_workspace_tongs(merged, workspace, approvals_path, prompt=True, out=None, inp=None):
    """Gate every workspace-sourced tong on first-run approval.

    The user/org/repo layers are trusted and skip the gate; only the workspace
    layer (any repo you happened to clone) is gated. For each workspace tong that
    is not already approved, the privilege summary is printed and the user is
    asked to approve it. Approval is keyed by workspace path + tong name + a hash
    of the merged definition, so any change to the definition re-prompts; newly
    granted approvals are persisted to `approvals_path` (the user layer). The
    `workspace` key is the checkout root, so a git worktree (which has its own
    root) re-approves rather than inheriting another checkout's approval.

    With no workspace-sourced tongs the gate is a no-op, which is what keeps a
    launch with zero (or only trusted) tongs byte-identical to a direct docker
    run.

    Raises `ApprovalDenied` -- the launch must not proceed -- when a workspace
    tong is unapproved and the user declines, when `prompt` is False (the
    scripted `--no-prompt` posture fails closed rather than auto-approving), or
    when there is no workspace path to key the approval by.
    """
    out = sys.stderr if out is None else out
    inp = sys.stdin if inp is None else inp

    pending = [
        (name, merged[name]["definition"])
        for name in sorted(merged)
        if tongs.is_workspace_sourced(merged[name]["source"])
    ]
    if not pending:
        return

    if not workspace:
        raise ApprovalDenied(
            "refusing to evaluate workspace tong approval without a workspace path"
        )

    approvals = tongs.load_approvals(approvals_path)
    recorded = False
    for name, defn in pending:
        if tongs.is_approved(approvals, workspace, name, defn):
            continue
        out.write(render_privilege_summary(name, tongs.privilege_summary(defn)) + "\n")
        if not prompt:
            raise ApprovalDenied(
                "workspace tong %r is unapproved and --no-prompt fails closed" % name
            )
        if not _prompt_yes_no("Approve workspace tong %r?" % name, out, inp):
            raise ApprovalDenied("workspace tong %r was not approved" % name)
        tongs.record_approval(approvals, workspace, name, defn)
        recorded = True

    if recorded:
        tongs.save_approvals(approvals_path, approvals)


# --- Secret resolution --------------------------------------------------------


class SecretResolutionError(Exception):
    """A secret reference could not be resolved; the launch must not proceed."""


def make_secret_resolver(providers):
    """Build the impure resolver closure over a configured provider table.

    Returns `resolve(provider, ref) -> str`, the side-effectful counterpart to
    the pure `tongs.substitute_secrets`/`tongs.plan_tong_secrets`: it shells out
    to the provider CLI built by `tongs.secret_provider_command` and returns the
    secret printed on stdout. Interactive unlocks (`op signin`, biometrics) work
    because the launcher runs in the user's terminal before the anvil starts. A
    single trailing newline -- which provider CLIs conventionally append -- is
    stripped; any other whitespace is preserved verbatim.

    Raises `SecretResolutionError` (naming the provider and reference, never the
    secret) for an unknown provider, a CLI that cannot be run, or a non-zero
    exit, so a misconfigured secret stops the launch rather than handing the tong
    an empty or partial value.
    """

    def resolve(provider, ref):
        try:
            command = tongs.secret_provider_command(providers, provider, ref)
        except KeyError:
            raise SecretResolutionError(
                "no secret provider %r is configured; declare it in "
                "secret-providers.yaml" % provider
            )
        except tongs.UnmappedSecretError:
            raise SecretResolutionError(
                "secret provider %r maps no command for %r; add it (or a "
                "'default') under that provider in secret-providers.yaml"
                % (provider, ref)
            )
        try:
            completed = subprocess.run(command, stdout=subprocess.PIPE, check=False)
        except OSError as exc:
            raise SecretResolutionError(
                "secret provider %r could not run: %s" % (provider, exc)
            )
        if completed.returncode != 0:
            raise SecretResolutionError(
                "secret provider %r failed for %r (exit %d)"
                % (provider, ref, completed.returncode)
            )
        value = completed.stdout.decode("utf-8")
        return value[:-1] if value.endswith("\n") else value

    return resolve


# --- Secret delivery channel --------------------------------------------------
# Resolved secrets reach a tong over a host FIFO bind-mounted into the container,
# not as `-e`/argv/disk. The bytes only ever live in the kernel pipe buffer: the
# tong's `/bin/sh` wrapper blocks reading the FIFO, and the launcher writes the
# `export NAME=value` script once the wrapper has opened the read end, so the
# values are in the process environment before the real entrypoint starts. The
# channel is created behind a factory so `run_with_tongs` can be tested with a
# fake that records the payload instead of touching the filesystem.


class SecretChannel:
    """A host FIFO handing a tong its secret env through the kernel pipe buffer."""

    def __init__(self, directory, host_path):
        self._dir = directory
        self.host_path = host_path

    def deliver(self, payload, *, timeout=30.0, poll=0.05,
                sleep=time.sleep, monotonic=time.monotonic):
        """Write `payload` once the tong has opened the FIFO's read end.

        Opens the write end non-blocking and retries while no reader is attached
        (`ENXIO`), so a tong that never starts times out rather than hanging the
        launcher forever. Once the tong's wrapper has opened the read end the open
        succeeds; the whole payload is written -- looping over partial writes and
        a full pipe buffer, since the channel is non-blocking and a payload can
        exceed the pipe capacity -- and the end closed, signalling EOF so the
        wrapper's `cat` returns and it execs the real process. Raises
        `OrchestrationError` if no reader appears, the buffer never drains within
        `timeout`, or the tong closes the read end before delivery completes (so a
        truncated secret never reaches the tong silently).
        """
        deadline = monotonic() + timeout
        while True:
            try:
                fd = os.open(self.host_path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno == errno.ENXIO and monotonic() < deadline:
                    sleep(poll)
                    continue
                if exc.errno == errno.ENXIO:
                    raise OrchestrationError(
                        "tong did not open its secret channel within %gs" % timeout
                    )
                raise OrchestrationError("secret channel error: %s" % exc)
        try:
            data = memoryview(payload.encode("utf-8"))
            while data:
                try:
                    data = data[os.write(fd, data):]
                except BlockingIOError:
                    # Pipe buffer full; the tong's `cat` is still draining. Wait,
                    # bounded by the same deadline, rather than dropping bytes.
                    if monotonic() >= deadline:
                        raise OrchestrationError(
                            "tong did not drain its secret channel within %gs" % timeout
                        )
                    sleep(poll)
                except BrokenPipeError:
                    raise OrchestrationError(
                        "tong closed its secret channel before delivery completed"
                    )
        finally:
            os.close(fd)

    def cleanup(self):
        """Remove the FIFO and its directory (best-effort)."""
        shutil.rmtree(self._dir, ignore_errors=True)


def open_secret_channel(uid=None):
    """Create a host FIFO in a private temp dir, returning a `SecretChannel`.

    The directory is mode 0700 and the FIFO 0600, so only the launcher's user can
    open the read end and intercept the secret. When the tong runs as a different
    non-root uid (from the image config) the FIFO is `chown`ed to it best-effort so
    that user can read it; a container running as root reads it regardless.
    """
    directory = tempfile.mkdtemp(prefix="swarmforge-secret-")
    os.chmod(directory, 0o700)
    host_path = os.path.join(directory, "secret-env")
    os.mkfifo(host_path, 0o600)
    if uid is not None:
        try:
            os.chown(host_path, uid, -1)
        except OSError:
            pass  # not permitted (uid differs and launcher is not root); 0600 stands
    return SecretChannel(directory, host_path)


def _uid_of(image_user):
    """Numeric uid from an image's configured user, or None if not a bare uid.

    `docker inspect`'s `.Config.User` may be empty, a numeric `uid[:gid]`, or a
    name. Only a bare numeric uid can be `chown`ed to without a passwd lookup; a
    name (or empty/root) leaves the FIFO at its default 0600 launcher ownership.
    """
    if not image_user:
        return None
    token = image_user.split(":", 1)[0]
    try:
        return int(token)
    except ValueError:
        return None


# --- Docker seam --------------------------------------------------------------
# Every docker invocation goes through DockerCLI so the orchestration logic can
# be unit-tested against a fake. The methods are thin wrappers; the launch
# sequencing and policy live in `run_with_tongs`. `_run` defaults to
# subprocess.run and is the single injection point for tests.


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

    def image_exec_config(self, image):
        """`(entrypoint, cmd, user)` from an image's config, pulling it if absent.

        Used to reconstruct the process a secret-injecting tong must `exec` once
        its `/bin/sh` wrapper has loaded the secret env: overriding `--entrypoint`
        for the wrapper drops the image's own entrypoint/command, so they are read
        back here. `entrypoint`/`cmd` are argv lists (possibly empty); `user` is
        the image's configured user (``""`` when none). A missing image is pulled
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
        fmt = ("{{json .Config.Entrypoint}}\n{{json .Config.Cmd}}\n"
               "{{json .Config.User}}")
        completed = self._run(
            ["docker", "image", "inspect", "--format", fmt, image],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return None
        lines = _decode(completed.stdout).splitlines()
        if len(lines) < 3:
            return None
        entrypoint = json.loads(lines[0]) or []
        cmd = json.loads(lines[1]) or []
        user = json.loads(lines[2]) or ""
        return entrypoint, cmd, user

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

    def network_connect(self, network, container, alias=None):
        """Attach a running container to `network`, optionally under `alias`.

        Used to connect a long-lived `shared` tong to a session network under its
        canonical alias, so the session reaches it without the tong having to live
        on the session network permanently.
        """
        argv = ["docker", "network", "connect"]
        if alias:
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


# --- Readiness ----------------------------------------------------------------


def wait_ready(docker, container, defn, alias, network, *, anvil_image,
               sleep=time.sleep, monotonic=time.monotonic, interval=0.5):
    """Block until a tong reports ready, returning True/False on timeout.

    Dispatches on the tong's resolved readiness mode (see
    `tongs.readiness_settings`): `tcp` dials the canonical alias on the network;
    `healthcheck` runs the declared exec command or polls the image HEALTHCHECK;
    `none` is treated as ready immediately. A `tcp` probe dials the tong's
    network-internal port from a throwaway container, which needs both a network
    to dial on and the anvil image to run from; without either it degrades to "is
    the container running" -- decided and warned once, not on every poll.
    """
    mode, command, timeout_s = tongs.readiness_settings(defn)
    if mode == "none":
        return True

    interface = defn.get("interface") or {}
    port = interface.get("port")

    tcp_degraded = mode == "tcp" and (not anvil_image or not network)
    if tcp_degraded:
        tongs.warn(
            "cannot run a TCP readiness probe of '%s' (no anvil image or "
            "network); falling back to a container-running check" % container
        )

    def probe():
        if mode == "tcp":
            if tcp_degraded:
                state = docker.inspect_state(container)
                return bool(state and state["running"])
            return docker.tcp_probe(network, alias, port, anvil_image)
        # healthcheck
        if command:
            return docker.exec_ok(container, command)
        return docker.health_status(container) == "healthy"

    start = monotonic()
    while True:
        if probe():
            return True
        if monotonic() - start >= timeout_s:
            return False
        sleep(interval)


# --- Orchestration ------------------------------------------------------------


class OrchestrationError(Exception):
    """A tong could not be started/made ready; the launch stops."""


def _mounts_workspace(defn):
    """True if a tong's `mounts:` request the session workspace.

    The magic word may carry a `:mode` suffix (e.g. `workspace:ro`), so compare
    only the word before the colon.
    """
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == tongs.WORKSPACE_MOUNT:
            return True
    return False


def unsupported_tong_reasons(merged):
    """Reasons each discovered tong is outside what the launcher can start.

    The launcher starts `shared` and `session` tongs reached over the network
    (`mcp`/`port`) or with no anvil-facing surface (`none`), resolving any secret
    references and delivering them as env over a FIFO. Refused here:

      * a `volume` interface -- a shared named volume has no consumer yet, so it
        is not wired into either container;
      * a `shared` tong that mounts the `workspace` -- a `shared` tong is one
        long-lived container reused across sessions, so binding one session's
        workspace into it would expose that workspace to every later session that
        reuses the container (a `session` tong is the right home for a
        per-workspace mount).

    A refused tong is reported rather than started half-wired. Returns a list of
    human-readable reason strings (empty == every discovered tong is startable).
    """
    reasons = []
    for name in sorted(merged):
        defn = merged[name]["definition"]
        kind = (defn.get("interface") or {}).get("kind")
        if kind == "volume":
            reasons.append(
                "tong '%s' has a 'volume' interface, which this launcher does not "
                "wire up" % name
            )
        if defn.get("lifecycle") == "shared" and _mounts_workspace(defn):
            reasons.append(
                "tong '%s' is a 'shared' tong that mounts the workspace; a shared "
                "container is reused across sessions, so it would leak one "
                "session's workspace into the next" % name
            )
    return reasons


def ensure_mcp_harness_supported(merged, harness):
    """Refuse MCP tongs when no emitter exists for the selected harness."""
    mcp_names = [
        name for name in sorted(merged)
        if (merged[name]["definition"].get("interface") or {}).get("kind") == "mcp"
    ]
    if not mcp_names or harness in tongs.MCP_EMITTERS:
        return
    supported = ", ".join(sorted(tongs.MCP_EMITTERS))
    got = harness if harness else "none"
    raise OrchestrationError(
        "mcp tong(s) %s require --harness to be one of: %s (got %s)"
        % (", ".join(mcp_names), supported, got)
    )


def _start_one_tong(docker, name, defn, *, container, network, alias,
                    resolver, workspace, label_hash, make_channel):
    """Start one tong container detached, delivering any secret env over a FIFO.

    Resolves the definition's secret references through `resolver` and splits the
    env into plain (`-e`) and secret. With no secret env the image's own entrypoint
    runs unchanged. With secret env, the tong's entrypoint is overridden with a
    `/bin/sh` wrapper (built from the image's real entrypoint+command, read via
    `docker inspect` or declared on the tong) that reads a bind-mounted host FIFO,
    exports each `NAME=value` into its environment, then execs the real process. The
    launcher writes the resolved values into the FIFO only once the wrapper has
    opened the read end, so the secrets are present in the environment before the
    real process starts, while the bytes live only in the kernel pipe buffer --
    never `-e`, argv, or disk.

    Any existing container of the same name is removed first so a stale or stopped
    one is replaced cleanly. If anything fails after the container starts -- a
    docker error, a delivery timeout, or a Ctrl-C -- the container is removed
    before re-raising, so a half-configured `shared` tong (stamped with its
    config-hash label) is not reused on the next session despite missing its
    secret. The FIFO is always cleaned up.
    """
    plan = tongs.plan_tong_secrets(defn.get("env"), resolver)
    plain_env = plan["env"]
    secrets = plan["secrets"]

    if not secrets:
        argv = tongs.tong_run_argv(
            name, defn,
            container_name=container, network=network, alias=alias,
            env=plain_env, label_hash=label_hash, workspace=workspace,
        )
        docker.rm_force(container)
        docker.run_detached(argv)
        return

    image_entrypoint, image_cmd, image_user = docker.image_exec_config(defn["image"])
    try:
        target = tongs.resolve_exec_target(defn, image_entrypoint, image_cmd)
    except ValueError as exc:
        raise OrchestrationError(str(exc))
    entrypoint, command = tongs.secret_inject_argv(target)
    payload = tongs.render_secret_exports(secrets)

    channel = make_channel(_uid_of(image_user))
    try:
        argv = tongs.tong_run_argv(
            name, defn,
            container_name=container, network=network, alias=alias,
            env=plain_env, label_hash=label_hash, workspace=workspace,
            fifo_host_path=channel.host_path, entrypoint=entrypoint, command=command,
        )
        docker.rm_force(container)
        docker.run_detached(argv)
        channel.deliver(payload)
    except BaseException:
        docker.rm_force(container)
        raise
    finally:
        channel.cleanup()


def _ensure_shared_tong(docker, name, defn, *, container, network, alias,
                        resolver, workspace, label_hash, make_channel):
    """Start a `shared` tong, or reuse the running one, recreating it if stale.

    A `shared` tong is one long-lived container keyed by `shared_container_name`.
    Its config-hash label answers "did the definition change since it started?":
    a missing container, a stopped one, or a hash mismatch triggers a fresh start
    (removing any old container first); a running container with a matching hash
    is reused untouched. The hash is over the merged (pre-resolution) definition,
    so the same long-lived container is reused across sessions while the
    definition is stable -- and deciding to reuse one never runs a secret-provider
    CLI, so a rotated secret behind an unchanged reference does not churn it.
    """
    state = docker.inspect_state(container)
    if state and state["running"] and state["label"] == label_hash:
        return
    _start_one_tong(
        docker, name, defn,
        container=container, network=network, alias=alias,
        resolver=resolver, workspace=workspace, label_hash=label_hash,
        make_channel=make_channel,
    )


def _injection_pre_image_args(injection):
    """`-e`/`-v` options the discovered tongs add to the anvil before the image.

    A `port` tong contributes the env vars the anvil reads to reach it. The
    named-volume mount path is a faithful consumer of `plan_injection`'s shape but
    stays empty here, since `volume` tongs are refused before this runs.
    """
    args = []
    for key in sorted(injection["env"]):
        args += ["-e", "%s=%s" % (key, injection["env"][key])]
    for mount in injection["mounts"]:
        args += ["-v", "%s:%s" % (mount["volume"], mount["mountpoint"])]
    return args


# Where the generated MCP config is mounted in the anvil, and the env var the
# OpenCode entrypoint reads to merge it into opencode.json. Claude Code is pointed
# at the same in-container path with `--mcp-config` instead.
MCP_CONFIG_CONTAINER_PATH = "/tmp/swarmforge-tong-mcp.json"
MCP_FILE_ENV = "SWARMFORGE_TONG_MCP_FILE"


def _mcp_injection(mcp_config, harness, mcp_dir):
    """Write the generated MCP config and return its `(pre, post)` anvil args.

    `mcp_config` is the per-harness fragment from `tongs.plan_injection` (already
    shaped for the harness). It is written into `mcp_dir` on the host and mounted
    read-only into the anvil. For Claude Code the mount is paired with
    `--mcp-config <path>` (a harness arg, so it appends after the image); for
    OpenCode the mount is paired with `SWARMFORGE_TONG_MCP_FILE=<path>`, which the
    entrypoint reads to merge the fragment into opencode.json. With an empty
    fragment nothing is written, mounted, or appended, so the anvil argv is
    unchanged.
    """
    if not mcp_config:
        return [], []
    host_path = os.path.join(mcp_dir, "tong-mcp.json")
    with open(host_path, "w", encoding="utf-8") as handle:
        json.dump(mcp_config, handle)
    mount = ["-v", "%s:%s:ro" % (host_path, MCP_CONFIG_CONTAINER_PATH)]
    if harness == "claude":
        return mount, ["--mcp-config", MCP_CONFIG_CONTAINER_PATH]
    return mount + ["-e", "%s=%s" % (MCP_FILE_ENV, MCP_CONFIG_CONTAINER_PATH)], []


def run_with_tongs(merged, anvil_cmd, opts, *, docker, providers=None,
                   make_channel=open_secret_channel,
                   sleep=time.sleep, monotonic=time.monotonic):
    """Start the discovered tongs, run the anvil, and tear down session state.

    Only reached when at least one tong was discovered and every tong is startable
    (the empty case stays a direct exec; unsupported tongs are refused earlier).

    Each tong's secret references are resolved through the provider CLIs in
    `providers` (the user-layer table) and delivered as env over a FIFO (created by
    `make_channel`) as the tong starts; a tong without secrets gets none of that
    machinery. `shared` tongs are ensured
    on the anvil's base network, reusing a running one whose config hash still
    matches (which never re-resolves its secrets). When any `session` tong exists a
    per-session network is created: the `session` tongs start on it under their
    canonical aliases, each network-facing `shared` tong is connected to it for
    this session, and the anvil joins it plus the base network (the `NETWORK=`
    escape hatch). With no `session` tong the anvil keeps using the base network
    exactly as before. Each tong's readiness is probed on the network the anvil
    will use, then reachability is injected into the anvil argv -- `port` env
    vars and, for `mcp` tongs, the per-harness MCP config -- and the anvil runs
    in the foreground.

    On exit -- including SIGINT -- the `session` tongs and the per-session network
    are torn down (and the connected `shared` tongs disconnected) while the
    long-lived `shared` tongs are left running.

    Returns the anvil's exit code. Raises `OrchestrationError` if a tong never
    becomes ready (the anvil does not run against a half-up environment) or a
    `session` tong is discovered with no anvil `--name` to key the session by, and
    `SecretResolutionError` if a secret reference cannot be resolved.
    """
    ensure_mcp_harness_supported(merged, opts.harness)
    resolver = make_secret_resolver(providers or {})
    base_network = tongs.anvil_option_value(anvil_cmd, "--network")
    session_id = tongs.anvil_option_value(anvil_cmd, "--name")

    has_session = any(
        merged[name]["definition"].get("lifecycle") == "session" for name in merged
    )
    if has_session and not session_id:
        # The per-session network and container names key off the anvil --name.
        # The Makefile always passes it, so its absence is a launch-shape bug --
        # stop rather than build an unnamed session network. (Checked before
        # plan_network, which needs the handle to derive the network name.)
        raise OrchestrationError(
            "session tongs require the anvil '--name' as a session handle"
        )

    # An org-layer `shared` tong is partitioned onto its own isolated network,
    # which the anvil joins by name -- so a scoped launch needs the anvil --name
    # for the same reason a session launch does. Derive the org scope token from
    # the org layer's directory (None when no org layer was passed, leaving every
    # shared tong on today's global, unscoped naming).
    org_token = tongs.org_scope_token(dict(opts.layer_dirs).get(tongs.ORG))
    has_org_shared = bool(org_token) and any(
        merged[name]["definition"].get("lifecycle") != "session"
        and merged[name]["source"] == tongs.ORG
        for name in merged
    )
    if has_org_shared and not session_id:
        raise OrchestrationError(
            "org-scoped shared tongs require the anvil '--name' as a handle to "
            "join their isolated network"
        )

    plan = tongs.plan_network(merged, base_network, session_id)
    # `volume` tongs are refused upstream, so the injection is reachability for
    # the network-facing kinds only: `port` env vars and, for `mcp` tongs, the
    # per-harness MCP config emitted for `opts.harness`.
    injection = tongs.plan_injection(merged, opts.harness)

    created_network = None
    started_sessions = []
    connected_shared = []
    joined_shared_networks = []  # isolated per-scope networks the anvil must join
    anvil_multi = False          # the anvil was created via the multi-network path
    mcp_dir = None  # host temp dir holding the generated MCP config, if any
    try:
        if plan["create"]:
            docker.ensure_network(plan["create"])
            created_network = plan["create"]

        ready_checks = []  # (name, defn, alias, container, probe_network)
        for name in sorted(merged):
            defn = merged[name]["definition"]
            alias = tongs.canonical_alias(name, defn)
            if defn.get("lifecycle") == "session":
                container = tongs.session_container_name(session_id, name)
                _start_one_tong(
                    docker, name, defn,
                    container=container, network=plan["network"], alias=alias,
                    resolver=resolver, workspace=opts.workspace,
                    label_hash=tongs.config_hash(defn), make_channel=make_channel,
                )
                started_sessions.append(container)
                probe_network = plan["network"]
            else:
                # An org-sourced shared tong is partitioned onto an isolated
                # per-org network and a scoped container name; every other shared
                # tong stays on the shared base network, unscoped, as before.
                scope = org_token if merged[name]["source"] == tongs.ORG else None
                container = tongs.shared_container_name(name, scope=scope)
                if scope:
                    tong_network = tongs.shared_network_name(scope)
                    docker.ensure_network(tong_network)
                    if tong_network not in joined_shared_networks:
                        joined_shared_networks.append(tong_network)
                    probe_network = tong_network
                else:
                    tong_network = base_network
                    probe_network = plan["network"]
                _ensure_shared_tong(
                    docker, name, defn,
                    container=container, network=tong_network, alias=alias,
                    resolver=resolver, workspace=opts.workspace,
                    label_hash=tongs.config_hash(defn), make_channel=make_channel,
                )
            ready_checks.append((name, defn, alias, container, probe_network))

        # Attach each network-facing `shared` tong to the per-session network under
        # its canonical alias, so the anvil reaches it there without the long-lived
        # tong having to live on the session network permanently. (The session-tong
        # start loop above iterates the whole merged set, not plan["session_aliases"],
        # because a `none` session tong with no alias must still be started; only the
        # network-facing `shared` tongs in plan["shared_connect"] are connected here.)
        for name, alias in plan["shared_connect"]:
            if org_token and merged[name]["source"] == tongs.ORG:
                # An org-scoped shared tong is isolated on its own network, which
                # the anvil joins directly -- it is deliberately never attached to
                # the per-session network, so the session reaches it only through
                # that org network and never via the shared base/session fabric.
                continue
            container = tongs.shared_container_name(name)
            # ensure_network may have reused a network left by a hard-killed prior
            # session whose teardown never ran, with this shared tong still attached;
            # a stale endpoint would make connect fail. Clear it first -- best-effort,
            # a no-op when the tong is not attached -- so the connect is idempotent.
            docker.network_disconnect(plan["network"], container)
            docker.network_connect(plan["network"], container, alias=alias)
            connected_shared.append((plan["network"], container))

        # Probe readiness on the network the anvil will reach each tong over: the
        # session/base network for ordinary tongs, but the isolated org network
        # for a scoped shared tong (it lives only there, never on the session
        # fabric), so each is checked at the alias the anvil actually dials.
        for name, defn, alias, container, probe_network in ready_checks:
            if not wait_ready(
                docker, container, defn, alias, probe_network,
                anvil_image=opts.anvil_image, sleep=sleep, monotonic=monotonic,
            ):
                raise OrchestrationError("tong '%s' did not become ready in time" % name)

        # `port`/`volume` reachability splices in before the image; the MCP
        # config adds a read-only mount (and, for OpenCode, the env var the
        # entrypoint reads) before the image, plus Claude's `--mcp-config` as a
        # harness arg after it. With no `mcp` tongs the fragment is empty and
        # nothing is written or appended.
        pre_image_args = _injection_pre_image_args(injection)
        post_image_args = []
        if injection["mcp"]:
            mcp_dir = tempfile.mkdtemp(prefix="swarmforge-mcp-")
            mcp_pre, mcp_post = _mcp_injection(injection["mcp"], opts.harness, mcp_dir)
            pre_image_args += mcp_pre
            post_image_args += mcp_post
        injected = tongs.inject_anvil_argv(
            anvil_cmd, network=plan["network"],
            pre_image_args=pre_image_args, post_image_args=post_image_args,
        )
        # The anvil joins the base network (the `NETWORK=` escape hatch) when a
        # per-session network is its primary, plus every isolated org network its
        # scoped shared tongs live on.
        extra_networks = list(plan["extra_networks"]) + joined_shared_networks
        if extra_networks:
            # The anvil joins more than one network, which docker run cannot do at
            # creation, so create -> connect the extras -> start it attached.
            # (session_id is guaranteed here: a session network or an org network
            # both require the anvil --name, checked above.)
            anvil_multi = True
            return docker.run_foreground_multi(injected, extra_networks, session_id)
        return docker.run_foreground(injected)
    finally:
        # Tear down per-session state, leaving the long-lived `shared` tongs
        # running. Order matters: remove the `session` tongs and the anvil, then
        # disconnect the `shared` tongs, before removing the network -- docker
        # refuses to delete a network while endpoints remain.
        for container in started_sessions:
            docker.rm_force(container)
        # A multi-network anvil is an explicitly-created container (left for us so
        # a failed connect/start is not orphaned); the plain single-network run
        # uses `--rm` and self-removes, so it is only force-removed here.
        if anvil_multi:
            docker.rm_force(session_id)
        for network, container in connected_shared:
            docker.network_disconnect(network, container)
        if created_network:
            docker.network_rm(created_network)
        # Best-effort prune of each isolated org network: docker refuses while the
        # long-lived shared tong is still attached, so the network persists with
        # its tong and is reclaimed only once nothing is on it.
        for network in joined_shared_networks:
            docker.network_rm(network)
        # The generated MCP config was bind-mounted into the anvil, which has now
        # exited; remove the host temp file holding it.
        if mcp_dir:
            shutil.rmtree(mcp_dir, ignore_errors=True)


def exec_anvil(anvil_cmd):
    """Exec the anvil argv, replacing this process.

    On success this never returns. If the command cannot be execed (e.g. the
    binary is missing from PATH), report it and return 127 -- the shell's
    convention for an uninvocable command -- rather than surfacing a traceback.
    """
    try:
        os.execvp(anvil_cmd[0], anvil_cmd)
    except OSError as exc:
        tongs.warn("cannot exec %r: %s" % (anvil_cmd[0], exc))
        return 127


def main(argv):
    try:
        opts, anvil_cmd = parse_args(argv)
    except UsageError as exc:
        tongs.warn(str(exc))
        tongs.warn(USAGE)
        return 2

    merged = discover_tongs(opts.layer_dirs)

    # Gate workspace-sourced tongs before anything else runs. With none present
    # (the common case) this is a no-op and the launch is unchanged; otherwise an
    # unapproved or declined workspace tong stops the launch before the anvil.
    try:
        gate_workspace_tongs(
            merged,
            opts.workspace,
            opts.approvals or default_approvals_path(),
            prompt=not opts.no_prompt,
        )
    except ApprovalDenied as exc:
        tongs.warn(str(exc))
        return 1

    # Passthrough invariant: with no tong definitions discovered, exec the anvil
    # argv verbatim -- byte-identical to the direct docker run, and the process
    # is replaced so the controlling tty, signals, and --rm cleanup are untouched.
    if not merged:
        return exec_anvil(anvil_cmd)

    # From here a tong actually starts, so validate before touching docker: an
    # invalid definition should stop the launch with a clear message, not fail
    # mid-orchestration with a docker error.
    errors = []
    for name in sorted(merged):
        errors.extend(tongs.validate_tong(name, merged[name]["definition"]))
    if errors:
        for error in errors:
            tongs.warn(error)
        return 1

    # Refuse anything this launcher cannot start (see unsupported_tong_reasons:
    # a volume interface, or a shared tong mounting the workspace) rather than
    # starting it half-wired.
    unsupported = unsupported_tong_reasons(merged)
    if unsupported:
        for reason in unsupported:
            tongs.warn(reason)
        return 1

    # An `mcp` tong's canonical alias is its `interface.name`, not its filename,
    # so two tongs can claim the same network alias -- which would make DNS (and
    # so readiness, env, and MCP wiring) nondeterministic. Refuse the set rather
    # than starting both. (`port`/`none` tongs alias to their unique filenames, so
    # they never collide on their own.)
    collisions = tongs.alias_collisions(merged)
    if collisions:
        for alias, names in sorted(collisions.items()):
            tongs.warn(
                "tongs %s all resolve to network alias '%s'; rename or set a "
                "distinct interface.name" % (", ".join(names), alias)
            )
        return 1

    # An `mcp` tong needs a per-harness config fragment. Starting it for an
    # unknown or omitted harness would leave the anvil unable to discover it.
    try:
        ensure_mcp_harness_supported(merged, opts.harness)
    except OrchestrationError as exc:
        tongs.warn(str(exc))
        return 1

    # Load the secret-provider table the resolver shells out to. A malformed file
    # stops the launch with a clear message rather than silently dropping a
    # provider; a missing file is fine until a tong actually references a secret.
    try:
        providers = tongs.load_secret_providers(opts.providers or default_providers_path())
    except ValueError as exc:
        tongs.warn(str(exc))
        return 1

    # run_with_tongs runs the anvil in the foreground and returns its exit code,
    # leaving the (long-lived) shared tongs running. A tong that never becomes
    # ready, or a secret reference that cannot be resolved, stops the launch
    # rather than running the anvil against a half-up environment.
    try:
        return run_with_tongs(
            merged, anvil_cmd, opts, docker=DockerCLI(), providers=providers
        )
    except (OrchestrationError, DockerError, SecretResolutionError) as exc:
        tongs.warn(str(exc))
        return 1
    except KeyboardInterrupt:
        # The anvil was interrupted (Ctrl-C); the shared tongs stay running by
        # design. Report the conventional 128+SIGINT status.
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
