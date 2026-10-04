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

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time

from swarmforge import tongs
from swarmforge import gitguard
from swarmforge import harness as harnesses
from swarmforge.harness.spec import provided

from .errors import OrchestrationError
from .readiness import wait_ready
from .secretchan import SecretChannel, make_secret_resolver


def _mounts_word(defn, word):
    """True if a tong's `mounts:` request the magic word `word`.

    The magic word may carry a target and/or a mode (e.g. `workspace:/code:ro`),
    so compare only the word before the first colon.
    """
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == word:
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
    definition mounts the workspace at. A tong keeps the host's worktree
    registration, since no harness resolves a project root inside one.

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
        per-workspace mount);
      * a `shared` tong that mounts the `tmux-socket` -- the same container
        outlives the session and is reused by later ones, so it would carry one
        session's tmux server, and its pinning to that session's pane, into the
        next.

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
        if defn.get("lifecycle") == "shared" and _mounts_word(defn, tongs.WORKSPACE_MOUNT):
            reasons.append(
                "tong '%s' is a 'shared' tong that mounts the workspace; a shared "
                "container is reused across sessions, so it would leak one "
                "session's workspace into the next" % name
            )
        if defn.get("lifecycle") == "shared" and _mounts_word(defn, tongs.TMUX_MOUNT):
            reasons.append(
                "tong '%s' is a 'shared' tong that mounts the tmux socket; a shared "
                "container outlives the session and is reused by later ones, so it "
                "would carry one session's tmux server into the next" % name
            )
    return reasons


def check_tmux_socket_dir(host_dir, socket_path, uid=None):
    """Refuse a tmux socket directory that is not plainly the launcher's own.

    `host_dir` and `socket_path` come from a `$TMUX` that `tongs.resolve_tmux_socket`
    already accepted, which judged only the spelling. Here the host itself is
    checked, since the whole directory is bound into the tong: it must be reached
    with no symlink in any component (docker follows one, binding wherever it
    points), be a real directory owned by `uid` (the launcher's own by default)
    with no group or other permission bits -- tmux creates it 0700 -- and hold
    `socket_path` as a socket.

    Every directory above it, up to `/`, must pass OpenSSH's StrictModes rule:
    owned by root or `uid`, and not writable by group or others unless sticky
    (as `/tmp` is). Otherwise another local user could swap the directory for a
    symlink between this check and docker's bind. Raises `OrchestrationError`
    when any of this fails.
    """
    uid = os.getuid() if uid is None else uid
    if os.path.realpath(host_dir) != host_dir:
        raise OrchestrationError(
            "tmux socket directory %s is reached through a symlink; docker would "
            "bind whatever it points at" % host_dir)
    try:
        dir_stat = os.lstat(host_dir)
    except OSError as exc:
        raise OrchestrationError("cannot inspect the tmux socket directory: %s" % exc)
    if not stat.S_ISDIR(dir_stat.st_mode):
        raise OrchestrationError("tmux socket directory %s is not a directory" % host_dir)
    if dir_stat.st_uid != uid:
        raise OrchestrationError(
            "tmux socket directory %s is owned by uid %d, not the launcher's uid %d"
            % (host_dir, dir_stat.st_uid, uid))
    if dir_stat.st_mode & 0o077:
        raise OrchestrationError(
            "tmux socket directory %s is open to group or others (mode %04o); "
            "tmux creates it 0700" % (host_dir, stat.S_IMODE(dir_stat.st_mode)))
    ancestor = os.path.dirname(host_dir)
    while True:
        try:
            ancestor_stat = os.lstat(ancestor)
        except OSError as exc:
            raise OrchestrationError("cannot inspect %s above the tmux socket: %s"
                                     % (ancestor, exc))
        if ancestor_stat.st_uid not in (0, uid):
            raise OrchestrationError(
                "%s, above the tmux socket directory, is owned by uid %d, neither "
                "root nor the launcher's uid %d" % (ancestor, ancestor_stat.st_uid, uid))
        mode = ancestor_stat.st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH) and not mode & stat.S_ISVTX:
            raise OrchestrationError(
                "%s, above the tmux socket directory, is writable by group or others "
                "without the sticky bit (mode %04o), so another user could swap the "
                "directory before docker binds it" % (ancestor, stat.S_IMODE(mode)))
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    try:
        socket_stat = os.lstat(socket_path)
    except OSError as exc:
        raise OrchestrationError("cannot inspect the tmux socket: %s" % exc)
    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise OrchestrationError("tmux socket %s is not a socket" % socket_path)


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
                    resolver, workspace, label_hash, make_channel,
                    tmux=None, tmux_pane=None, session_handle=None):
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

    `tmux`, `tmux_pane`, and `session_handle` reach `tongs.tong_run_argv`, which
    uses them only for a tong that mounts `tmux-socket`.

    Once the argv is assembled, any existing container of the same name is removed
    so a stale or stopped one is replaced cleanly -- but a definition the argv
    builder refuses removes nothing here, since it started nothing (teardown still
    removes a `session` tong by name). If anything fails after the container
    starts -- a docker error, a delivery timeout, or a Ctrl-C -- the container is
    removed before re-raising, so a half-configured `shared` tong (stamped with its
    config-hash label) is not reused on the next session despite missing its secret.
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
        try:
            argv = tongs.tong_run_argv(
                name, defn,
                container_name=container, network=network, alias=alias,
                env=plain_env, label_hash=label_hash, workspace=workspace,
                extra_mount_specs=git_dir_specs,
                tmux=tmux, tmux_pane=tmux_pane, session_handle=session_handle,
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

    # Assembled before the rm_force below: a refused definition must remove nothing.
    try:
        argv = tongs.tong_run_argv(
            name, defn,
            container_name=container, network=network, alias=alias,
            env=plain_env, label_hash=label_hash, workspace=workspace,
            secret_channel=True, entrypoint=entrypoint, command=command,
            extra_mount_specs=git_dir_specs,
            tmux=tmux, tmux_pane=tmux_pane, session_handle=session_handle,
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


MCP_CONFIG_CONTAINER_PATH = "/tmp/swarmforge-tong-mcp.json"
MCP_FILE_ENV = "SWARMFORGE_TONG_MCP_FILE"


def _mcp_injection(mcp_config, harness, mcp_dir):
    """Write the generated MCP config and return its `(pre, post)` anvil args.

    `mcp_config` is the per-harness fragment from `tongs.plan_injection` (already
    shaped for the harness). It is written into `mcp_dir` on the host and mounted
    read-only into the anvil; the harness spec's `mcp_delivery` decides how the
    harness is told where it landed. A `("flag", FLAG)` harness gets `FLAG <path>`
    appended as a harness arg after the image; an `("env", VAR)` harness gets the
    mount paired with `VAR=<path>`, which the container's config driver
    (`swarmforge.harness.init`) reads to merge the fragment into that harness's
    config the way its spec's `mcp_merge` names. An unregistered harness falls
    back to the env-var delivery. With an empty fragment nothing is written,
    mounted, or appended, so the anvil argv is unchanged.
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
                   make_channel=SecretChannel, teardown_guard=contextlib.nullcontext,
                   check_tmux_dir=check_tmux_socket_dir,
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

    On exit -- including SIGINT, and SIGHUP/SIGTERM once `cli.main` makes them
    raise -- the `session` tongs and the per-session network are torn down (and
    the connected `shared` tongs disconnected) while the long-lived `shared`
    tongs are left running. Teardown runs inside the caller's `teardown_guard()`,
    which keeps signals from cutting it short; the default guards nothing.

    A `session` tong that mounts `tmux-socket` is handed the launcher's
    `opts.tmux`/`opts.tmux_pane` and the session handle; both values, and the
    socket directory on disk (through `check_tmux_dir`, `check_tmux_socket_dir`
    unless a test injects another), are checked before anything starts.

    Returns the anvil's exit code. Raises `OrchestrationError` if a tong never
    becomes ready (the anvil does not run against a half-up environment), a
    `session` tong is discovered with no anvil `--name` to key the session by, or
    a tong mounts `tmux-socket` while the launcher is outside tmux, its `$TMUX`
    or `$TMUX_PANE` is malformed, its socket directory fails
    `check_tmux_socket_dir`, or the definition makes one of the names the
    launcher sets a secret reference, and `SecretResolutionError` if a secret
    reference cannot be resolved.
    """
    ensure_mcp_harness_supported(merged, opts.harness)
    resolver = make_secret_resolver(providers or {})
    base_network = tongs.anvil_option_value(anvil_cmd, "--network")
    session_id = tongs.anvil_option_value(anvil_cmd, "--name")

    has_session = any(
        merged[name]["definition"].get("lifecycle") == "session" for name in merged
    )
    if has_session and not session_id:
        # Checked before plan_network, which derives the network name from the handle.
        raise OrchestrationError(
            "session tongs require the anvil '--name' as a session handle"
        )

    tmux_names = [
        name for name in sorted(merged)
        if _mounts_word(merged[name]["definition"], tongs.TMUX_MOUNT)
    ]
    for name in tmux_names:
        # Validation refuses this too; a secret here would override the launcher's values.
        owned_secret = tongs.tmux_secret_env_error(merged[name]["definition"])
        if owned_secret:
            raise OrchestrationError("tong '%s': %s" % (name, owned_secret))
    if tmux_names:
        if not opts.tmux:
            raise OrchestrationError(
                "tong(s) %s mount '%s', so the launcher must be started from inside "
                "a tmux pane ($TMUX is unset)" % (", ".join(tmux_names), tongs.TMUX_MOUNT)
            )
        try:
            tmux_dir, tmux_socket_path, _ = tongs.resolve_tmux_socket(opts.tmux)
            if opts.tmux_pane:
                tongs.check_tmux_pane(opts.tmux_pane)
        except ValueError as exc:
            raise OrchestrationError("tong(s) %s: %s" % (", ".join(tmux_names), exc))
        try:
            check_tmux_dir(tmux_dir, tmux_socket_path)
        except OrchestrationError as exc:
            raise OrchestrationError("tong(s) %s: %s" % (", ".join(tmux_names), exc))

    # No org layer means no token, and every shared tong keeps its global unscoped name.
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
    injection = tongs.plan_injection(merged, opts.harness)

    created_network = None
    started_sessions = []
    connected_shared = []
    joined_shared_networks = []
    anvil_multi = False
    mcp_dir = None
    # Recorded before creation: an interrupt can land mid-create.
    try:
        if plan["create"]:
            created_network = plan["create"]
            docker.ensure_network(plan["create"])

        ready_checks = []  # (name, defn, alias, container, probe_network)
        for name in sorted(merged):
            defn = merged[name]["definition"]
            alias = tongs.canonical_alias(name, defn)
            if defn.get("lifecycle") == "session":
                container = tongs.session_container_name(session_id, name)
                started_sessions.append(container)
                _start_one_tong(
                    docker, name, defn,
                    container=container, network=plan["network"], alias=alias,
                    resolver=resolver, workspace=opts.workspace,
                    label_hash=tongs.config_hash(defn), make_channel=make_channel,
                    tmux=opts.tmux, tmux_pane=opts.tmux_pane, session_handle=session_id,
                )
                probe_network = plan["network"]
            else:
                scope = org_token if merged[name]["source"] == tongs.ORG else None
                container = tongs.shared_container_name(name, scope=scope)
                if scope:
                    tong_network = tongs.shared_network_name(scope)
                    if tong_network not in joined_shared_networks:
                        joined_shared_networks.append(tong_network)
                    docker.ensure_network(tong_network)
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

        # Network-facing shared tongs only; a `none` tong has no alias to connect under.
        for name, aliases in plan["shared_connect"]:
            if org_token and merged[name]["source"] == tongs.ORG:
                # Reached only over its own org network, never the session fabric.
                continue
            container = tongs.shared_container_name(name)
            connected_shared.append((plan["network"], container))
            # A stale endpoint from a hard-killed session would fail the connect.
            docker.network_disconnect(plan["network"], container)
            docker.network_connect(plan["network"], container, aliases=aliases)

        # A scoped shared tong lives only on its org network, so probe where the anvil dials.
        for name, defn, alias, container, probe_network in ready_checks:
            if not wait_ready(
                docker, container, defn, alias, probe_network,
                anvil_image=opts.anvil_image, sleep=sleep, monotonic=monotonic,
            ):
                raise OrchestrationError("tong '%s' did not become ready in time" % name)

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
        # plan["extra_networks"] is the base network, when the anvil was given one.
        extra_networks = list(plan["extra_networks"]) + joined_shared_networks
        if extra_networks:
            # session_id is guaranteed: a session or org network both require --name.
            anvil_multi = True
            return docker.run_foreground_multi(injected, extra_networks, session_id)
        return docker.run_foreground(injected)
    finally:
        with teardown_guard():
            # Order matters: docker refuses to remove a network while endpoints remain.
            for container in started_sessions:
                docker.rm_force(container)
            # rm_force covers a create path that failed before its own --rm could fire.
            if anvil_multi:
                docker.rm_force(session_id)
            for network, container in connected_shared:
                docker.network_disconnect(network, container)
            if created_network:
                docker.network_rm(created_network)
            # Best-effort: docker refuses while the long-lived shared tong is still attached.
            for network in joined_shared_networks:
                docker.network_rm(network)
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
