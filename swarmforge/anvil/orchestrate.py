"""Starting the discovered tongs, running the anvil, and tearing down after it.

The policy layer of the launcher: what to start, in what order, on which
network, what to inject into the anvil argv, and what to remove on the way out.
It drives the docker seam and the secret channel rather than talking to either
directly, so the whole sequence can be unit-tested against a fake docker and a
fake channel.

A `shared` tong is one long-lived container reused across sessions while its
config hash still matches; a `session` tong lives and dies with the anvil, on a
per-session network torn down with it. Reachability is spliced into the anvil
argv before it runs: `port` env vars, and for `mcp` tongs the generated
per-harness config. `exec_anvil` is the degenerate case -- no tongs, so the anvil
argv is exec'd verbatim.
"""

import json
import os
import shutil
import sys
import tempfile
import time

# The pure core this launcher orchestrates: layer discovery and name-based
# merge.
from swarmforge import tongs

# The git-dir guard the Makefile already runs for the anvil's workspace mount.
# The launcher reuses it for tongs that mount the workspace, so a tong sees the
# same git-dir mounts (and the same read-only guards) the anvil does.
from swarmforge import gitguard

# The per-harness registry: MCP fragment shape and delivery are read off each
# harness's spec.
from swarmforge import harness as harnesses
from swarmforge.harness.spec import provided

from .errors import OrchestrationError
from .readiness import wait_ready
from .secretchan import SecretChannel, make_secret_resolver


def _mounts_workspace(defn):
    """True if a tong's `mounts:` request the session workspace.

    The magic word may carry a target and/or a mode (e.g. `workspace:/code:ro`),
    so compare only the word before the first colon.
    """
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == tongs.WORKSPACE_MOUNT:
            return True
    return False


def _workspace_git_dir_specs(defn, workspace, warn=None):
    """Git-dir mounts a workspace-mounting tong needs beyond the workspace bind.

    The anvil's workspace bind is always paired with the mounts
    `gitguard.build_mounts` works out: read-only guards over the config and
    hooks the *host's* git obeys, and -- when the workspace is a linked worktree
    or another checkout whose git dir lives outside it -- that git dir at its
    own absolute path, which is where the checkout's `.git` pointer file says to
    look. A tong that mounts the workspace needs the same set, or git inside it
    cannot resolve a worktree checkout at all ("fatal: not a git repository").
    The guard maps workspace-internal paths below every destination the
    definition mounts the workspace at.

    When every workspace mount is read-only, the extra mounts are forced
    read-only too: build_mounts emits the git-dir binds writable (the anvil's
    workspace is writable), and left as-is they would open a write path a
    `workspace:ro` definition never asked for. Empty when the tong does not
    mount the workspace, no workspace path is known, or the workspace is not a
    git checkout. Raises `ValueError` for a malformed `mounts:` entry, like the
    argv builder.
    """
    if not workspace:
        return []
    placements = tongs.workspace_mount_placements(defn)
    if not placements:
        return []
    specs = gitguard.build_mounts(
        workspace, [destination for destination, _ in placements], warn=warn)
    if all(mode == "ro" for _, mode in placements):
        specs = [spec if spec.endswith(":ro") else spec + ":ro" for spec in specs]
    return specs


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
    module = harnesses.get(harness)
    if not mcp_names or (module is not None and provided(module.SPEC.mcp_fragment)):
        return
    supported = ", ".join(
        name for name in harnesses.names()
        if provided(harnesses.get(name).SPEC.mcp_fragment)
    )
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
    `docker inspect` or declared on the tong) that creates a FIFO on an
    in-container tmpfs, exports each `NAME=value` it reads from it into its
    environment, then execs the real process. The launcher streams the resolved
    values into the FIFO through `docker exec -i` (see `SecretChannel`), so the
    secrets are present in the environment before the real process starts, while
    the bytes live only in docker's API stream and the container kernel's pipe
    buffer -- never `-e`, argv, or disk.

    Once the argv is assembled, any existing container of the same name is removed
    so a stale or stopped one is replaced cleanly -- but a definition the argv
    builder refuses removes nothing, since it started nothing. If anything fails
    after the container starts -- a docker error, a delivery timeout, or a Ctrl-C --
    the container is removed before re-raising, so a half-configured `shared` tong
    (stamped with its config-hash label) is not reused on the next session despite
    missing its secret.
    """
    plan = tongs.plan_tong_secrets(defn.get("env"), resolver)
    plain_env = plan["env"]
    secrets = plan["secrets"]

    try:
        git_dir_specs = _workspace_git_dir_specs(
            defn, workspace,
            warn=lambda message: print("tong '%s': %s" % (name, message),
                                       file=sys.stderr))
    except ValueError as exc:
        raise OrchestrationError("tong '%s': %s" % (name, exc))

    if not secrets:
        # A definition that got past validation should never fail argv assembly,
        # but if it does it is still a config problem, not a crash.
        try:
            argv = tongs.tong_run_argv(
                name, defn,
                container_name=container, network=network, alias=alias,
                env=plain_env, label_hash=label_hash, workspace=workspace,
                extra_mount_specs=git_dir_specs,
            )
        except ValueError as exc:
            raise OrchestrationError("tong '%s': %s" % (name, exc))
        docker.rm_force(container)
        docker.run_detached(argv)
        return

    image_entrypoint, image_cmd = docker.image_exec_config(defn["image"])
    try:
        target = tongs.resolve_exec_target(defn, image_entrypoint, image_cmd)
    except ValueError as exc:
        raise OrchestrationError(str(exc))
    entrypoint, command = tongs.secret_inject_argv(target)
    payload = tongs.render_secret_exports(secrets)

    # Assembled before the teardown guard below: a refused definition starts
    # nothing, so it must remove nothing (for a `shared` tong, that would be
    # another session's running container).
    try:
        argv = tongs.tong_run_argv(
            name, defn,
            container_name=container, network=network, alias=alias,
            env=plain_env, label_hash=label_hash, workspace=workspace,
            secret_channel=True, entrypoint=entrypoint, command=command,
            extra_mount_specs=git_dir_specs,
        )
    except ValueError as exc:
        raise OrchestrationError("tong '%s': %s" % (name, exc))
    try:
        docker.rm_force(container)
        docker.run_detached(argv)
        make_channel(docker, container).deliver(payload)
    except BaseException:
        docker.rm_force(container)
        raise


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


# Where the generated MCP config is mounted in the anvil. A harness whose spec
# delivers by env var is pointed at that path through the variable below, which
# the entrypoint reads; one that delivers by flag is pointed at it on its own
# command line instead.
MCP_CONFIG_CONTAINER_PATH = "/tmp/swarmforge-tong-mcp.json"
MCP_FILE_ENV = "SWARMFORGE_TONG_MCP_FILE"


def _mcp_injection(mcp_config, harness, mcp_dir):
    """Write the generated MCP config and return its `(pre, post)` anvil args.

    `mcp_config` is the per-harness fragment from `tongs.plan_injection` (already
    shaped for the harness). It is written into `mcp_dir` on the host and mounted
    read-only into the anvil; the harness spec's `mcp_delivery` decides how the
    harness is told where it landed. A `("flag", FLAG)` harness gets `FLAG <path>`
    appended as a harness arg after the image; an `("env", VAR)` harness gets the
    mount paired with `VAR=<path>`, which the entrypoint reads to merge the
    fragment into that harness's config. An unregistered harness falls back to
    the env-var delivery. With an empty fragment nothing is written, mounted, or
    appended, so the anvil argv is unchanged.
    """
    if not mcp_config:
        return [], []
    host_path = os.path.join(mcp_dir, "tong-mcp.json")
    with open(host_path, "w", encoding="utf-8") as handle:
        json.dump(mcp_config, handle)
    mount = ["-v", "%s:%s:ro" % (host_path, MCP_CONFIG_CONTAINER_PATH)]
    delivery = ("env", MCP_FILE_ENV)
    module = harnesses.get(harness)
    if module is not None:
        delivery = module.SPEC.mcp_delivery
    kind, name = delivery
    if kind == "flag":
        return mount, [name, MCP_CONFIG_CONTAINER_PATH]
    return mount + ["-e", "%s=%s" % (name, MCP_CONFIG_CONTAINER_PATH)], []


def run_with_tongs(merged, anvil_cmd, opts, *, docker, providers=None,
                   make_channel=SecretChannel,
                   sleep=time.sleep, monotonic=time.monotonic):
    """Start the discovered tongs, run the anvil, and tear down session state.

    Only reached when at least one tong was discovered and every tong is startable
    (the empty case stays a direct exec; unsupported tongs are refused earlier).

    Each tong's secret references are resolved through the provider CLIs in
    `providers` (the user-layer table) and delivered as env over the tong's
    in-container FIFO (via `make_channel`) as the tong starts; a tong without
    secrets gets none of that machinery. `shared` tongs are ensured
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
        # every DNS name it answers to, so the anvil reaches it there without the
        # long-lived tong having to live on the session network permanently. (The
        # session-tong start loop above iterates the whole merged set, not
        # plan["session_aliases"], because a `none` session tong with no alias must
        # still be started; only the network-facing `shared` tongs in
        # plan["shared_connect"] are connected here.)
        for name, aliases in plan["shared_connect"]:
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
            docker.network_connect(plan["network"], container, aliases=aliases)
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
        # config adds a read-only mount before the image, paired with either the
        # env var the entrypoint reads or a harness arg after the image,
        # whichever the harness spec's delivery names. With no `mcp` tongs the
        # fragment is empty and nothing is written or appended.
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
