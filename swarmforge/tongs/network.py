"""Planning the per-session docker network the anvil and its tongs share."""

import re

from .mcp import tong_aliases
from .model import LIFECYCLES, warn


# Each anvil session gets its own docker network so concurrent anvils cannot
# reach each other's session-scoped tongs by container name. `session` tongs run
# only on it; a tong's canonical DNS name is a `--network-alias`, never its
# (session/worktree-suffixed) container name, so the generated config is
# identical across worktrees. A `shared` tong is one persistent container
# attached to each session network via `network connect --alias` and detached on
# teardown, so sessions can reach it without being able to reach each other.
#
# These functions only *plan* the wiring as plain data; the launcher creates the
# network, attaches tongs, and tears them down. With no `session` tongs the plan
# keeps the existing single network (and the `NETWORK=` escape hatch) untouched,
# so a zero-tong launch is byte-identical to today's direct `docker run`.

SESSION_NET_PREFIX = "swarmforge-session"


def session_network_name(session_id):
    """Per-session docker network name derived from a unique `session_id`.

    `session_id` is the launcher's per-session handle (e.g. the anvil container
    name, which already carries the project/worktree suffix). It is sanitized to
    the characters docker permits in a network name and prefixed so sessions
    never collide and the networks are recognizable as Swarmforge-managed.
    """
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-_.")
    return "%s-%s" % (SESSION_NET_PREFIX, token) if token else SESSION_NET_PREFIX


def plan_network(merged, base_network, session_id):
    """Network wiring for one anvil launch.

    Returns a plan of plain data the launcher applies:

      * `network`         -- the network the anvil joins and `session` tongs run
                             on. The per-session network when `session` tongs
                             exist, otherwise `base_network` (today's behavior).
      * `create`          -- the per-session network the launcher must create
                             (and tear down), or None to reuse `base_network`.
      * `extra_networks`  -- additional pre-existing networks the anvil also
                             joins (the `NETWORK=` escape hatch): `base_network`
                             when a per-session network is created, else none --
                             reusing `base_network` already joins it as primary.
      * `session_aliases` -- `[(tong_name, [alias, ...])]` for each network-facing
                             `session` tong, attached to the per-session network
                             under its canonical alias and any declared extras.
      * `shared_connect`  -- `[(tong_name, [alias, ...])]` for each network-facing
                             `shared` tong, connected to the per-session network
                             under those aliases and disconnected on teardown.

    A per-session network is created **only when `session` tongs exist**. With
    none, the anvil keeps using `base_network` and `shared` tongs (if any) stay
    reachable on it exactly as before -- so a zero-tong launch is unchanged. The
    per-session network is what lets a `shared` tong be connected per session,
    which is why `shared_connect` is empty unless one is created.
    """
    session_names = [
        name for name in sorted(merged)
        if merged[name]["definition"].get("lifecycle") == "session"
    ]
    if not session_names:
        return {
            "network": base_network,
            "create": None,
            "extra_networks": [],
            "session_aliases": [],
            "shared_connect": [],
        }

    net = session_network_name(session_id)
    # All network-facing tongs share the one per-session network, so two tongs
    # claiming the same alias would collide there -- DNS would resolve
    # nondeterministically. Keep the first claim by sorted tong name and drop the
    # rest with a warning, mirroring the MCP-config and env-var collision guards.
    # Dedup is per alias, not per tong, so a tong that loses one contested name
    # still registers under the rest. One pass over both lifecycles keeps the
    # winner deterministic regardless of whether the loser is `session` or `shared`.
    session_aliases = []
    shared_connect = []
    seen = {}
    for name in sorted(merged):
        defn = merged[name]["definition"]
        lifecycle = defn.get("lifecycle")
        if lifecycle not in LIFECYCLES:
            continue
        aliases = []
        for alias in tong_aliases(name, defn):
            if alias in seen:
                warn(
                    "tong '%s' reuses network alias '%s' (already used by '%s'); "
                    "ignoring the duplicate" % (name, alias, seen[alias])
                )
                continue
            seen[alias] = name
            aliases.append(alias)
        if not aliases:
            continue
        (session_aliases if lifecycle == "session" else shared_connect).append((name, aliases))
    return {
        "network": net,
        "create": net,
        "extra_networks": [base_network] if base_network else [],
        "session_aliases": session_aliases,
        "shared_connect": shared_connect,
    }
