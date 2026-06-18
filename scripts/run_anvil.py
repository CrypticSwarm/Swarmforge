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

Shared tongs
------------
When a tong is discovered, the launcher starts it before the anvil, waits for it
to report ready, makes it reachable from the anvil, runs the anvil in the
foreground, and leaves the tong running afterwards. A `shared` tong is one
long-lived container keyed by a stable name: a running one whose config-hash
label still matches is reused untouched, and a missing/stopped/stale one is
(re)started. A `port` or `volume` tong's reachability is injected into the anvil
as environment (and, for `volume`, a shared mount).

The launcher starts only `shared` tongs that carry no secret references and no
`mcp` interface; a `session` lifecycle, a secret reference, or an `mcp` interface
is refused with a clear message rather than started half-wired.

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
import importlib.util
import os
import subprocess
import sys
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
    "[--approvals PATH] [--anvil-image IMAGE] [--no-prompt] -- <anvil command>"
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
# is the store path (default resolved in main); `anvil_image` is the image the
# readiness prober runs to dial a tong's network-internal port; `no_prompt` makes
# the approval gate fail closed for scripted runs.
LauncherOptions = collections.namedtuple(
    "LauncherOptions",
    ["layer_dirs", "workspace", "approvals", "anvil_image", "no_prompt"],
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
                LauncherOptions(layer_dirs, workspace, approvals, anvil_image, no_prompt),
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

    def run_foreground(self, argv):
        """Run the anvil in the foreground and return its exit code.

        Popen + wait (rather than exec) so the launcher regains control after the
        anvil exits. On Ctrl-C the SIGINT reaches both this process and the anvil
        through the controlling terminal's process group; the anvil handles it and
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
    `none` is treated as ready immediately. A `tcp` probe needs the anvil image to
    run from -- without one the launcher cannot dial the tong's network-internal
    port, so it degrades to "is the container running" and warns.
    """
    mode, command, timeout_s = tongs.readiness_settings(defn)
    if mode == "none":
        return True

    interface = defn.get("interface") or {}
    port = interface.get("port")

    def probe():
        if mode == "tcp":
            if not anvil_image:
                tongs.warn(
                    "no anvil image for a TCP readiness probe of '%s'; "
                    "falling back to a container-running check" % container
                )
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


def unsupported_tong_reasons(merged):
    """Reasons each discovered tong is outside what the launcher can start.

    The launcher starts only `shared` tongs that hold no secret and expose no MCP
    interface. A `session` lifecycle (it needs a per-session network), a secret
    reference (it needs tmpfs delivery), or an `mcp` interface (it needs generated
    MCP config) is unsupported here, so such a tong is refused rather than started
    half-wired. Returns a list of human-readable reason strings (empty == every
    discovered tong is startable).
    """
    reasons = []
    for name in sorted(merged):
        defn = merged[name]["definition"]
        if defn.get("lifecycle") == "session":
            reasons.append(
                "tong '%s' is a 'session' tong, which this launcher does not "
                "start (only 'shared' tongs are supported)" % name
            )
        if tongs.find_secret_refs(defn):
            reasons.append(
                "tong '%s' references a secret, which this launcher does not "
                "deliver" % name
            )
        if (defn.get("interface") or {}).get("kind") == "mcp":
            reasons.append(
                "tong '%s' has an 'mcp' interface, which this launcher does not "
                "wire up" % name
            )
    return reasons


def _start_shared_tong(docker, name, defn, *, container, network, alias,
                       workspace, label_hash):
    """Start one `shared` tong container detached, replacing any old one.

    The launcher only reaches here for a secret-less tong, so the definition's
    `env` is passed straight through as `-e`; any existing container of the same
    name is removed first so a stale or stopped one is replaced cleanly.
    """
    argv = tongs.tong_run_argv(
        name, defn,
        container_name=container, network=network, alias=alias,
        env=defn.get("env") or {}, label_hash=label_hash, workspace=workspace,
    )
    docker.rm_force(container)
    docker.run_detached(argv)


def _ensure_shared_tong(docker, name, defn, *, container, network, alias,
                        workspace, label_hash):
    """Start a `shared` tong, or reuse the running one, recreating it if stale.

    A `shared` tong is one long-lived container keyed by `shared_container_name`.
    Its config-hash label answers "did the definition change since it started?":
    a missing container, a stopped one, or a hash mismatch triggers a fresh start
    (removing any old container first); a running container with a matching hash
    is reused untouched. The hash is over the merged definition, so the same
    long-lived container is reused across sessions while the definition is stable.
    """
    state = docker.inspect_state(container)
    if state and state["running"] and state["label"] == label_hash:
        return
    _start_shared_tong(
        docker, name, defn,
        container=container, network=network, alias=alias,
        workspace=workspace, label_hash=label_hash,
    )


def _injection_pre_image_args(injection):
    """`-e`/`-v` options the discovered tongs add to the anvil before the image.

    Port/volume tongs contribute env vars the anvil reads to reach them, and
    volume tongs contribute the shared named-volume mount.
    """
    args = []
    for key in sorted(injection["env"]):
        args += ["-e", "%s=%s" % (key, injection["env"][key])]
    for mount in injection["mounts"]:
        args += ["-v", "%s:%s" % (mount["volume"], mount["mountpoint"])]
    return args


def run_with_tongs(merged, anvil_cmd, opts, *, docker,
                   sleep=time.sleep, monotonic=time.monotonic):
    """Start the discovered `shared` tongs, run the anvil, and leave them running.

    Only reached when at least one tong was discovered and every tong is startable
    (the empty case stays a direct exec; unsupported tongs are refused earlier).
    Sequence: ensure each `shared` tong is up on the anvil's base network
    (reusing a running one whose config hash still matches), probe each tong's
    readiness, inject `port`/`volume` reachability into the anvil argv, then run
    the anvil in the foreground. `shared` tongs are long-lived, so nothing is torn
    down when the anvil exits.

    Returns the anvil's exit code. Raises `OrchestrationError` if a tong never
    becomes ready -- the anvil does not run against a half-up environment.
    """
    base_network = tongs.anvil_option_value(anvil_cmd, "--network")
    # No `mcp` tong reaches this path (they are refused upstream), so the harness
    # emitter is unused and the injection is only `port`/`volume` env and mounts.
    injection = tongs.plan_injection(merged, None)

    ready_checks = []
    for name in sorted(merged):
        defn = merged[name]["definition"]
        alias = tongs.canonical_alias(name, defn)
        label_hash = tongs.config_hash(defn)
        container = tongs.shared_container_name(name)
        _ensure_shared_tong(
            docker, name, defn,
            container=container, network=base_network, alias=alias,
            workspace=opts.workspace, label_hash=label_hash,
        )
        ready_checks.append((name, defn, alias, base_network, container))

    for name, defn, alias, probe_net, container in ready_checks:
        if not wait_ready(
            docker, container, defn, alias, probe_net,
            anvil_image=opts.anvil_image, sleep=sleep, monotonic=monotonic,
        ):
            raise OrchestrationError("tong '%s' did not become ready in time" % name)

    injected = tongs.inject_anvil_argv(
        anvil_cmd, network=base_network,
        pre_image_args=_injection_pre_image_args(injection),
    )
    return docker.run_foreground(injected)


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

    # Refuse anything this launcher cannot start (a session lifecycle, a secret
    # reference, or an MCP interface) rather than starting it half-wired. Every
    # remaining tong is a non-MCP tong, whose canonical alias is its unique
    # filename, so no two can claim the same network alias.
    unsupported = unsupported_tong_reasons(merged)
    if unsupported:
        for reason in unsupported:
            tongs.warn(reason)
        return 1

    # run_with_tongs runs the anvil in the foreground and returns its exit code,
    # leaving the (long-lived) shared tongs running. A tong that never becomes
    # ready stops the launch rather than running the anvil against a half-up
    # environment.
    try:
        return run_with_tongs(merged, anvil_cmd, opts, docker=DockerCLI())
    except (OrchestrationError, DockerError) as exc:
        tongs.warn(str(exc))
        return 1
    except KeyboardInterrupt:
        # The anvil was interrupted (Ctrl-C); the shared tongs stay running by
        # design. Report the conventional 128+SIGINT status.
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
