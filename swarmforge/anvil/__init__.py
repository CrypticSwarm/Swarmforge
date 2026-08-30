"""Host-side launcher that wraps an anvil (harness container) run.

The Makefile resolves the docker-run argv for an anvil (`run_opencode` /
`run_claude`) and the host paths of the four tong definition layers, then
delegates the actual launch to this package:

    run-anvil [--user-tongs DIR] [--org-tongs DIR] [--repo-tongs DIR]
              [--workspace-tongs DIR] [--workspace PATH] [--approvals PATH]
              [--anvil-image IMAGE] [--no-prompt] -- docker run -it --rm ... <image> ...

Tongs are sibling containers that must be orchestrated from the host (they are
started alongside the anvil, not from inside it), which is why this wrapper sits
between Make and `docker run`. It discovers tong definitions across the four
layers using the pure core in `swarmforge.tongs`, then runs the anvil.

Package layout
--------------
One module per concern, in the order a launch passes through them:

    cli         options, their defaults, and the entry point that sequences a run
    approval    the first-run gate for workspace-sourced tongs
    secretchan  resolving secret references, and the channel that delivers them
    docker      the one seam every docker invocation goes through
    readiness   waiting for a started tong to report ready
    orchestrate starting the tongs, running the anvil, tearing down after it
    errors      the one failure raised from more than one of the above

Every public name is re-exported below, so a caller imports `swarmforge.anvil`
and never has to know which module a function sits in; a module's own private
helpers stay on it. Each re-export is a fresh binding rather than an alias, so
anything that redirects a name -- a test replacing a function with a fake --
has to redirect it on the module that owns it, not on this one.

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
read-only into the anvil, and points the harness at it: the container's config
driver merges OpenCode's fragment via `SWARMFORGE_TONG_MCP_FILE`, while Claude
Code is passed `--mcp-config <path>`. With no `mcp` tongs nothing is written, mounted, or
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
disk. Instead the launcher overrides the tong's entrypoint with a `/bin/sh`
wrapper that creates a FIFO on a tmpfs inside the container, reads it, exports
each `NAME=value` into its environment, then execs the image's real
entrypoint+command (looked up via `docker inspect`, or declared as
`entrypoint:`/`command:` on the tong). The launcher streams the resolved values
into that FIFO over `docker exec -i` stdin, so the secrets reach the real
process as ordinary environment variables -- present before it starts, since the
wrapper blocks on the FIFO until delivery -- while the bytes only ever live in
docker's API stream and the container kernel's pipe buffer. Because nothing in
the path crosses the host filesystem boundary, delivery works the same under
Docker Desktop's VM (macOS/Windows) as on native Linux. A tong with secret env
therefore needs `/bin/sh`, `mkfifo`, `cat`, and `rm` in its image; one without
secrets runs its image entrypoint unchanged.

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
exec. `tests/test_anvil_cli.py` asserts this byte-for-byte.

The anvil argv (everything after `--`) is forwarded to `os.execvp` unchanged, so
the anvil process replaces this one and keeps the controlling tty, signal
delivery, and `--rm` cleanup it had before.
"""

from .approval import (
    ApprovalDenied,
    gate_workspace_tongs,
    render_privilege_summary,
)
from .cli import (
    LAYER_FLAGS,
    USAGE,
    LauncherOptions,
    UsageError,
    default_approvals_path,
    default_providers_path,
    discover_tongs,
    main,
    parse_args,
)
from .docker import DockerCLI, DockerError
from .errors import OrchestrationError
from .orchestrate import (
    MCP_CONFIG_CONTAINER_PATH,
    MCP_FILE_ENV,
    ensure_mcp_harness_supported,
    exec_anvil,
    run_with_tongs,
    unsupported_tong_reasons,
)
from .readiness import wait_ready
from .secretchan import (
    SecretChannel,
    SecretResolutionError,
    make_secret_resolver,
)

__all__ = [
    # approval
    "ApprovalDenied",
    "gate_workspace_tongs",
    "render_privilege_summary",
    # cli
    "LAYER_FLAGS",
    "USAGE",
    "LauncherOptions",
    "UsageError",
    "default_approvals_path",
    "default_providers_path",
    "discover_tongs",
    "main",
    "parse_args",
    # docker
    "DockerCLI",
    "DockerError",
    # errors
    "OrchestrationError",
    # orchestrate
    "MCP_CONFIG_CONTAINER_PATH",
    "MCP_FILE_ENV",
    "ensure_mcp_harness_supported",
    "exec_anvil",
    "run_with_tongs",
    "unsupported_tong_reasons",
    # readiness
    "wait_ready",
    # secretchan
    "SecretChannel",
    "SecretResolutionError",
    "make_secret_resolver",
]
