"""The launcher's options, their defaults, and the entry point that sequences a run.

`parse_args` splits the launcher's own options from the anvil command at the
first `--`. `main` discovers tong definitions across the layers it was given,
gates the workspace-sourced ones on approval, refuses a set it cannot start, and
then either execs the anvil argv verbatim (no tongs discovered) or hands the
launch to the orchestrator. Every failure the launcher reports is mapped to a
process exit code here.

While the orchestrator runs, SIGHUP and SIGTERM unwind it through its teardown
the way Ctrl-C does, so closing the terminal or pane a session runs in does not
leave its `session` tongs running.
"""

import collections
import contextlib
import os
import signal

from swarmforge import tongs

from .approval import ApprovalDenied, gate_workspace_tongs
from .docker import DockerCLI, DockerError
from .errors import OrchestrationError, TerminationSignal
from .orchestrate import (
    ensure_mcp_harness_supported,
    exec_anvil,
    run_with_tongs,
    unsupported_tong_reasons,
)
from .secretchan import SecretResolutionError


USAGE = (
    "usage: run-anvil [--user-tongs DIR] [--org-tongs DIR] "
    "[--repo-tongs DIR] [--workspace-tongs DIR] [--workspace PATH] "
    "[--approvals PATH] [--providers PATH] [--harness NAME] "
    "[--anvil-image IMAGE] [--no-prompt] -- <anvil command>"
)

LAYER_FLAGS = {
    "--user-tongs": tongs.USER,
    "--org-tongs": tongs.ORG,
    "--repo-tongs": tongs.REPO,
    "--workspace-tongs": tongs.WORKSPACE,
}

# `anvil_image` is what the readiness prober runs; `no_prompt` fails the gate closed.
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


_TERMINATION_SIGNALS = (signal.SIGHUP, signal.SIGTERM)


class _TerminationTrap:
    """SIGHUP/SIGTERM handler that raises `TerminationSignal`, or only records it.

    Only the first signal handled is taken and kept in `received`; any after
    it is dropped. Outside `deferred` that one raises; inside it, it is only
    recorded, and `main` turns it into the exit status once the run has
    returned. SIGINT is left alone.
    """

    def __init__(self):
        self.received = None
        self._deferring = False

    def handle(self, signum, frame):
        if self.received is not None:
            return
        self.received = signum
        if not self._deferring:
            raise TerminationSignal(signum)

    @contextlib.contextmanager
    def deferred(self):
        """Teardown guard: hold SIGHUP/SIGTERM off until the block has finished.

        However the teardown was entered, both are blocked for its duration, and
        teardown's docker children inherit the block. Closing a pane hangs up the
        launcher's whole process group, so without it the in-flight `docker rm`
        would die with the launcher's signal and its tong be left behind. On the
        way out the mask is restored, and a signal held pending meanwhile is
        handled then, to be recorded rather than raised. Held signals are
        handled in signal-number order, not arrival order, so if both a TERM
        and a HUP were held, the HUP is the one taken.
        """
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        try:
            self._deferring = True
            signal.pthread_sigmask(signal.SIG_BLOCK, _TERMINATION_SIGNALS)
            yield
        finally:
            # Unblocking runs the pending handlers before it returns.
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            self._deferring = False


@contextlib.contextmanager
def _termination_signals_raise():
    """Install a `_TerminationTrap` on SIGHUP/SIGTERM for the duration; yields it.

    Python's default action for both kills the process without running any
    `finally`, which is where the orchestrator tears down. A signal already
    ignored on entry -- under `nohup`, say -- stays ignored. The previous
    handlers are put back on the way out, so the process is left as it was found.
    """
    trap = _TerminationTrap()
    previous = {}
    try:
        for signum in _TERMINATION_SIGNALS:
            handler = signal.getsignal(signum)
            if handler is signal.SIG_IGN:
                continue
            previous[signum] = handler
            signal.signal(signum, trap.handle)
        yield trap
    finally:
        for signum, handler in previous.items():
            # None means a handler installed outside Python; the default is the best match.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def main(argv):
    try:
        opts, anvil_cmd = parse_args(argv)
    except UsageError as exc:
        tongs.warn(str(exc))
        tongs.warn(USAGE)
        return 2

    merged = discover_tongs(opts.layer_dirs)

    # Gated first: an unapproved or declined workspace tong stops the launch.
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

    # Passthrough invariant: no tongs, so the anvil argv is exec'd byte-for-byte.
    if not merged:
        return exec_anvil(anvil_cmd)

    # Validate before touching docker: a bad definition must not fail mid-orchestration.
    errors = []
    for name in sorted(merged):
        errors.extend(tongs.validate_tong(name, merged[name]["definition"]))
    if errors:
        for error in errors:
            tongs.warn(error)
        return 1

    unsupported = unsupported_tong_reasons(merged)
    if unsupported:
        for reason in unsupported:
            tongs.warn(reason)
        return 1

    # A shared alias makes DNS -- and so readiness, env, and MCP wiring -- nondeterministic.
    collisions = tongs.alias_collisions(merged)
    if collisions:
        for alias, names in sorted(collisions.items()):
            tongs.warn(
                "tongs %s all resolve to network alias '%s'; rename or set a "
                "distinct interface.name" % (", ".join(names), alias)
            )
        return 1

    try:
        ensure_mcp_harness_supported(merged, opts.harness)
    except OrchestrationError as exc:
        tongs.warn(str(exc))
        return 1

    # A malformed table stops the launch; a missing one is fine until a secret is used.
    try:
        providers = tongs.load_secret_providers(opts.providers or default_providers_path())
    except ValueError as exc:
        tongs.warn(str(exc))
        return 1

    # Outermost, so a signal landing in an except clause is caught too.
    try:
        with _termination_signals_raise() as trap:
            try:
                rc = run_with_tongs(
                    merged, anvil_cmd, opts, docker=DockerCLI(), providers=providers,
                    teardown_guard=trap.deferred,
                )
            except (OrchestrationError, DockerError, SecretResolutionError) as exc:
                # Nothing written after a hangup: the terminal may be gone.
                if trap.received is not None:
                    return 128 + trap.received
                tongs.warn(str(exc))
                return 1
            except KeyboardInterrupt:
                # 128 + SIGINT, even if a HUP/TERM followed; shared tongs stay running by design.
                return 130
            if trap.received is not None:
                return 128 + trap.received
            return rc
    except TerminationSignal as exc:
        return 128 + exc.signum
