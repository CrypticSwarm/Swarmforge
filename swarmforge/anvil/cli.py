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

    try:
        return run_with_tongs(
            merged, anvil_cmd, opts, docker=DockerCLI(), providers=providers
        )
    except (OrchestrationError, DockerError, SecretResolutionError) as exc:
        tongs.warn(str(exc))
        return 1
    except KeyboardInterrupt:
        # 128 + SIGINT; the shared tongs stay running by design.
        return 130
