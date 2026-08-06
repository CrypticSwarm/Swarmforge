"""The launcher's options, their defaults, and the entry point that sequences a run.

`parse_args` splits the launcher's own options from the anvil command at the
first `--`. `main` discovers tong definitions across the layers it was given,
gates the workspace-sourced ones on approval, refuses a set it cannot start, and
then either execs the anvil argv verbatim (no tongs discovered) or hands the
launch to the orchestrator. Every failure the launcher reports is mapped to a
process exit code here.
"""

import collections
import os

from swarmforge import tongs

from .approval import ApprovalDenied, gate_workspace_tongs
from .docker import DockerCLI, DockerError
from .errors import OrchestrationError
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
