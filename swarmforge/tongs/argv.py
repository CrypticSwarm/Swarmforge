"""The docker names and command lines the launcher builds.

Turns a validated definition (plus the env, secret and network plans the rest of
the package produces) into the concrete `docker run` argv for a tong, and
rewrites the anvil's own argv so it reaches those tongs. The builders are pure --
they return argv lists and run no docker -- so the exact flags can be unit-tested;
`swarmforge.anvil` owns the side-effectful execution.
"""

import hashlib
import os
import re

from .mcp import _is_network_facing, _ordered_aliases
from .model import LABEL_CONFIG_HASH, LABEL_TONG_NAME, WORKSPACE_HOST_ENV
from .mounts import DEFAULT_DOCKER_SOCKET, _has_socket_mount, tong_mount_specs
from .secrets import SECRET_FIFO_TARGET, declared_run_override


# Shared tongs get a stable, session-independent container name so the same
# long-lived container is found (and staleness-checked) across sessions.
SHARED_CONTAINER_PREFIX = "swarmforge-shared"

# A scoped shared tong is isolated on its own docker network (this prefix +
# scope token) instead of the shared base network, so another scope's anvil has
# no interface on it and cannot reach the tong even by raw IP.
SHARED_NETWORK_PREFIX = "swarmforge-shared-net"


def _sanitize_container_token(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-_.")


def org_scope_token(org_tongs_dir):
    """Stable short token identifying one org by its tongs directory.

    A `shared` tong owned by the org layer must be partitioned per org: two orgs
    that ship the same tong (same filename, same `interface.name`) but different
    credentials would otherwise collide on one daemon-global container name --
    each launch tearing the other's container down -- and sit reachable side by
    side on the shared base network. The token scopes both the container name and
    the isolating network so neither collides across orgs.

    Derived from the absolute org-tongs directory path, so every launch pointed
    at the same org (e.g. different repos under one org) shares a token while
    different orgs differ. A readable hint from the org root (the parent of
    `.swarmforge/`) is prefixed for `docker ps`; the hash is what guarantees
    uniqueness. Returns None when no org layer path is given, leaving a launch
    with no org tongs on today's global, unscoped naming.
    """
    if not org_tongs_dir:
        return None
    canonical = os.path.normpath(os.path.abspath(org_tongs_dir))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    hint = _sanitize_container_token(
        os.path.basename(os.path.dirname(os.path.dirname(canonical)))
    )
    return "%s-%s" % (hint, digest) if hint else digest


def shared_container_name(name, scope=None):
    """Stable container name for a `shared` tong (session-independent).

    Sanitized to the characters docker permits in a container name and prefixed
    so the container is recognizable as a Swarmforge-managed shared tong. An
    optional `scope` token (see `org_scope_token`) partitions otherwise
    identically-named shared tongs owned by different scopes -- so two orgs
    shipping the same tong do not collide on one daemon-global container name.
    """
    token = _sanitize_container_token(name)
    parts = [SHARED_CONTAINER_PREFIX]
    if scope:
        parts.append(scope)
    if token:
        parts.append(token)
    return "-".join(parts)


def shared_network_name(scope):
    """Isolated docker network hosting one scope's `shared` tongs.

    A scoped `shared` tong lives alone on this network instead of the shared base
    network, and only the matching scope's anvil joins it -- so another scope's
    anvil has no interface on it and cannot reach the tong even by dialing a raw
    IP. The scope token (see `org_scope_token`) keeps two orgs' networks distinct.
    """
    return "%s-%s" % (SHARED_NETWORK_PREFIX, scope)


def session_container_name(session_id, name):
    """Per-session container name for a `session` tong.

    Carries the session handle (the anvil container name, already
    project/worktree-suffixed) so concurrent sessions never collide on a
    container name, while the tong's canonical alias -- not this name -- is what
    the anvil dials.
    """
    token = _sanitize_container_token(name)
    return "%s-tong-%s" % (session_id, token) if token else "%s-tong" % session_id


def tong_resource_flags(defn):
    """docker resource flags from a tong's `resources:` block.

    v1 understands `memory` (mapped to `--memory`). Unknown keys are ignored for
    forward compatibility. Raises `ValueError` if `resources` is present but not a
    mapping.
    """
    resources = defn.get("resources")
    if resources is None:
        return []
    if not isinstance(resources, dict):
        raise ValueError("'resources' must be a mapping")
    flags = []
    memory = resources.get("memory")
    if memory is not None:
        flags += ["--memory", str(memory)]
    return flags


def tong_run_argv(
    name,
    defn,
    container_name,
    network,
    alias,
    env=None,
    label_hash=None,
    workspace=None,
    socket_path=DEFAULT_DOCKER_SOCKET,
    fifo_host_path=None,
    entrypoint=None,
    command=None,
    extra_mount_specs=None,
):
    """Full `docker run -d` argv that starts one tong container.

    Assumes a validated definition. The container is detached (the launcher
    manages its lifecycle explicitly rather than tying it to the launcher's tty),
    named `container_name`, joined to `network` under `alias` -- plus any extra
    `interface.aliases` the definition declares -- as `--network-alias` flags (only
    for network-facing tongs; `volume`/`none` tongs need no DNS name), and stamped
    with the tong-name and config-hash labels so a later launch can detect a stale
    `shared` container. `env` (the tong's plain,
    non-secret values from `plan_tong_secrets`) is passed as `-e` in sorted order;
    resolved secret values never appear here -- they arrive over the FIFO instead.
    A socket-holding (broker) `session` tong additionally receives
    `SWARMFORGE_WORKSPACE_HOST_PATH` so it can bind-mount the session workspace into
    the workers it spawns; a tong that sets that name itself keeps its own value. A
    `shared` socket tong does not get it -- its container is reused across sessions,
    so a per-session workspace path would be stale (and a leak) for later ones.

    When the tong has secret env, the launcher passes `fifo_host_path` (bind-mounted
    read-only as the secret channel), `entrypoint` (`/bin/sh`), and `command` (the
    wrapper that reads the FIFO and execs the image's real argv) -- see
    `secret_inject_argv`. With no secrets all three are omitted; the tong's declared
    `entrypoint:`/`command:` are then applied as ordinary docker overrides (via
    `declared_run_override`) so a secret-free tong still honors them, falling back
    to the image's own entrypoint/command when it declares neither. `mounts:` magic
    words and `resources:` are appended, then the image, then the trailing argv.

    `extra_mount_specs` are fully-formed `-v` values the orchestrator computed
    outside the definition (today the git-dir mounts that ride along with a
    `workspace` mount -- see swarmforge.anvil); they are appended after the
    definition's own mounts, mirroring how the Makefile orders the anvil's.
    """
    if entrypoint is None and command is None:
        entrypoint, command = declared_run_override(defn)
    command = list(command or [])
    argv = ["docker", "run", "-d", "--name", container_name]
    if network:
        argv += ["--network", network]
    if alias and _is_network_facing(defn):
        for dns_name in _ordered_aliases(alias, defn):
            argv += ["--network-alias", dns_name]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    argv += ["--label", "%s=%s" % (LABEL_TONG_NAME, name)]
    if label_hash:
        argv += ["--label", "%s=%s" % (LABEL_CONFIG_HASH, label_hash)]
    if fifo_host_path:
        argv += ["-v", "%s:%s:ro" % (fifo_host_path, SECRET_FIFO_TARGET)]
    effective_env = dict(env or {})
    # A `shared` container is reused across sessions, so a per-session workspace
    # path baked into it would be stale for later ones; only `session` tongs get it.
    if workspace and _has_socket_mount(defn) and defn.get("lifecycle") == "session":
        effective_env.setdefault(WORKSPACE_HOST_ENV, workspace)
    for key in sorted(effective_env):
        argv += ["-e", "%s=%s" % (key, effective_env[key])]
    for spec in tong_mount_specs(defn, workspace, socket_path=socket_path):
        argv += ["-v", spec]
    for spec in extra_mount_specs or []:
        argv += ["-v", spec]
    argv += tong_resource_flags(defn)
    argv.append(defn["image"])
    argv += list(command)
    return argv


_ANVIL_DOCKER_VALUE_FLAGS = frozenset({
    "--name", "--network", "--tmpfs", "--env-file", "-e", "-v", "-w",
})


def _docker_option_end_index(argv):
    """Index of the image token, so scans ignore harness args after it.

    This only needs to understand the docker-run flags emitted by the Makefile
    wrapper; unknown options are treated as valueless rather than attempting to
    be a complete Docker CLI parser.
    """
    index = _docker_run_index(argv)
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return min(index + 1, len(argv))
        if not token.startswith("-") or token == "-":
            return index
        option, sep, _ = token.partition("=")
        index += 1
        if not sep and option in _ANVIL_DOCKER_VALUE_FLAGS:
            index += 1
    return len(argv)


def anvil_option_value(argv, flag):
    """Value of a `--flag value` or `--flag=value` option in an argv, or None.

    Used to read the anvil's `--name` (the per-session handle) and `--network`
    out of the docker-run argv the Makefile hands the launcher. Only the docker
    options before the image are scanned, so a same-named harness argument after
    the image is not mistaken for a docker option.
    """
    start = _docker_run_index(argv)
    end = _docker_option_end_index(argv)
    prefix = flag + "="
    for index in range(start, end):
        token = argv[index]
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _docker_run_index(argv):
    """Index just after the `run` (or `create`) subcommand token.

    Injected options precede the image, so this is where the launcher splices
    them in. Raises `ValueError` if the argv is not a docker run/create command.
    """
    for index, token in enumerate(argv):
        if token in ("run", "create"):
            return index + 1
    raise ValueError("anvil argv is not a 'docker run' command: %r" % (argv,))


def _replace_network(argv, network):
    out = list(argv)
    end = _docker_option_end_index(out)
    for index in range(_docker_run_index(out), end):
        token = out[index]
        if token == "--network" and index + 1 < len(out):
            out[index + 1] = network
            return out
        if token.startswith("--network="):
            out[index] = "--network=" + network
            return out
    insert_at = _docker_run_index(out)
    return out[:insert_at] + ["--network", network] + out[insert_at:]


def to_create_argv(anvil_argv):
    """Rewrite a `docker run ...` argv into the equivalent `docker create ...`.

    `docker run` attaches only one network when it creates the container, so an
    anvil that must join more than one network (its per-session network plus the
    pre-existing `NETWORK=` network) is instead created, connected to the extra
    networks, then started. Only the `run` subcommand token is swapped for
    `create`; every other token (flags, image, harness args) is preserved, so the
    created container is byte-for-byte what `docker run` would have made. Returns a
    new argv (the input is never mutated). Raises `ValueError` if the argv is not a
    docker run/create command.
    """
    out = list(anvil_argv)
    # _docker_run_index points just past the run/create subcommand, so the token
    # before it is the subcommand to rewrite (already 'create' is left as-is).
    out[_docker_run_index(out) - 1] = "create"
    return out


def inject_anvil_argv(anvil_argv, network=None, pre_image_args=(), post_image_args=()):
    """Rewrite the anvil's docker-run argv to reach the discovered tongs.

    Returns a new argv (the input is never mutated):

      * `network`         -- replaces the existing `--network` value (or inserts
                             one) so the anvil joins the network the tongs are on.
      * `pre_image_args`  -- options spliced in right after the `run` subcommand,
                             before the image: injected `-e`/`-v` for `port`/
                             `volume` tongs and the OpenCode MCP fragment mount.
      * `post_image_args` -- appended after everything, i.e. passed to the harness
                             binary: Claude's `--mcp-config <path>`.

    With all arguments empty/None the argv is returned unchanged, which keeps a
    zero-tong launch byte-identical to the direct docker run.
    """
    argv = list(anvil_argv)
    if network:
        argv = _replace_network(argv, network)
    if pre_image_args:
        insert_at = _docker_run_index(argv)
        argv = argv[:insert_at] + list(pre_image_args) + argv[insert_at:]
    if post_image_args:
        argv = argv + list(post_image_args)
    return argv
