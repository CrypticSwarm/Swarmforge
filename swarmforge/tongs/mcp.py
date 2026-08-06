"""What a tong's `interface:` contributes to the anvil, and the env names it uses."""

import re

from .model import ENV_PREFIX, warn


# --- Environment-variable naming ----------------------------------------------


def tong_env_prefix(name):
    """Canonical env-var prefix for a tong: github-creds -> SWARMFORGE_TONG_GITHUB_CREDS."""
    token = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return "%s_%s" % (ENV_PREFIX, token)


def tong_env_var(name, suffix):
    """Canonical env-var name, e.g. tong_env_var('pg', 'PORT') -> SWARMFORGE_TONG_PG_PORT."""
    return "%s_%s" % (tong_env_prefix(name), suffix.upper())


# --- Interface wiring ---------------------------------------------------------
# Each tong declares an explicit `interface:` that drives what the anvil needs to
# reach it. These pure functions dispatch on `interface.kind` and return that
# contribution -- environment variables, volume mounts, and per-harness MCP
# server config -- as plain data. The launcher applies the result (env flags,
# the opencode.json merge, a Claude --mcp-config file) when it actually starts
# tongs; everything here is side-effect free so it can be unit-tested directly.

# HTTP MCP servers are reached over a streamable-HTTP endpoint. The schema pins
# the alias and port but not the path, so default to the conventional `/mcp`
# endpoint, overridable per tong via `interface.path` for servers that mount
# elsewhere.
MCP_DEFAULT_PATH = "/mcp"


def canonical_alias(name, defn):
    """The tong's stable network alias / DNS name the anvil dials.

    Container names carry per-session/worktree suffixes for uniqueness, but the
    alias is always this bare name, so the generated config is identical across
    worktrees. For an `mcp` tong the alias is `interface.name` (the canonical MCP
    server name the agent sees); for every other kind it is the tong's own name.
    """
    interface = defn.get("interface") or {}
    if interface.get("kind") == "mcp" and interface.get("name"):
        return interface["name"]
    return name


def _is_network_facing(defn):
    """True if the anvil reaches this tong over the network (mcp or port).

    `volume` and `none` tongs have no listener, so they need no network alias.
    """
    return (defn.get("interface") or {}).get("kind") in ("mcp", "port")


def _ordered_aliases(canonical, defn):
    """`canonical` followed by the tong's declared extra aliases, de-duplicated.

    Order is stable and canonical-first so the primary DNS name is always the one
    a reader (and `docker inspect`) sees first.
    """
    aliases = [canonical]
    for extra in (defn.get("interface") or {}).get("aliases") or []:
        if extra not in aliases:
            aliases.append(extra)
    return aliases


def tong_aliases(name, defn):
    """Every DNS name this tong answers to on the network, canonical alias first.

    The canonical alias is the one the anvil is told to dial (injected env, MCP
    URL); `interface.aliases` adds further names for consumers that hardcode a
    hostname of their own -- a vhost, or a certificate CN a client must match.
    Empty for a tong with no listener (`volume`/`none`), which registers no DNS
    name at all.
    """
    if not _is_network_facing(defn):
        return []
    return _ordered_aliases(canonical_alias(name, defn), defn)


def mcp_url(defn, alias):
    """HTTP MCP endpoint URL for an `mcp` tong at its canonical `alias`.

    Points at the alias and the declared `interface.port`, with `interface.path`
    (default `/mcp`) as the endpoint path. Assumes a validated `mcp` definition,
    so `port` is present and integral.
    """
    interface = defn.get("interface") or {}
    transport = interface.get("transport", "http")
    if transport != "http":
        # v1 emits HTTP MCP only, and validation rejects other transports
        # upstream; reaching here means a new transport was admitted without
        # teaching this emitter its URL scheme. Fail loudly rather than hand the
        # anvil a wrong URL.
        raise ValueError("unsupported MCP transport %r for alias %r" % (transport, alias))
    path = interface.get("path", MCP_DEFAULT_PATH)
    if not path.startswith("/"):
        path = "/" + path
    return "http://%s:%d%s" % (alias, interface["port"], path)


def anvil_env(name, defn):
    """Environment variables the anvil needs to reach this tong.

    `port` tongs inject `SWARMFORGE_TONG_<NAME>_HOST` (the canonical alias) and
    `_PORT`; the anvil composes its own connection string since Swarmforge does
    not know the scheme or auth. `volume` tongs optionally inject `_PATH` (the
    mountpoint). `mcp` tongs are reached via generated MCP config, and `none`
    tongs have no anvil-facing surface, so both inject nothing.
    """
    interface = defn.get("interface") or {}
    kind = interface.get("kind")
    env = {}
    if kind == "port":
        env[tong_env_var(name, "HOST")] = canonical_alias(name, defn)
        env[tong_env_var(name, "PORT")] = str(interface.get("port"))
    elif kind == "volume":
        mountpoint = interface.get("mountpoint")
        if mountpoint:
            env[tong_env_var(name, "PATH")] = mountpoint
    return env


def anvil_mounts(name, defn):
    """Named-volume mounts the anvil shares with this tong.

    Only a `volume` tong shares a filesystem with the anvil: its named volume is
    mounted into the anvil at the declared mountpoint. Returns a list of
    `{"volume": ..., "mountpoint": ...}` (empty for every other kind).
    """
    interface = defn.get("interface") or {}
    if interface.get("kind") == "volume":
        return [{"volume": interface.get("volume"), "mountpoint": interface.get("mountpoint")}]
    return []


def alias_collisions(merged):
    """Tong names grouped by DNS alias, for aliases claimed by >1 tong.

    Returns `{alias: [tong names]}` for the aliases more than one tong resolves
    to (empty when every alias is unique). Two tongs on one network cannot share
    a DNS alias without nondeterministic resolution, so the live launcher refuses
    such a set; the planning functions that build per-anvil config instead keep
    the first and warn. Every name a tong registers is folded in -- canonical and
    declared extras alike -- so a shared *extra* alias is caught too. Only
    network-facing tongs (mcp/port) claim a `--network-alias`, so volume/none
    tongs contribute nothing: they never register a DNS name and so cannot collide.
    """
    by_alias = {}
    for name in sorted(merged):
        defn = merged[name]["definition"]
        for alias in tong_aliases(name, defn):
            by_alias.setdefault(alias, []).append(name)
    return {alias: names for alias, names in by_alias.items() if len(names) > 1}


def mcp_tongs(merged):
    """`{alias: definition}` for every `mcp`-interface tong in the merged set.

    Keyed by canonical alias and ordered by tong name. Two tongs that resolve to
    the same alias would collide on one network; the first (by sorted tong name)
    wins and the rest are dropped with a warning.
    """
    out = {}
    for name in sorted(merged):
        defn = merged[name]["definition"]
        if (defn.get("interface") or {}).get("kind") != "mcp":
            continue
        alias = canonical_alias(name, defn)
        if alias in out:
            warn("tong '%s' reuses MCP alias '%s'; ignoring the duplicate" % (name, alias))
            continue
        out[alias] = defn
    return out


def mcp_config_opencode(merged):
    """OpenCode `mcp` fragment for the discovered `mcp` tongs.

    Remote (HTTP) MCP servers keyed by canonical alias, shaped for merging into
    `opencode.json` through the entrypoint's existing merge path. Returns `{}`
    when no `mcp` tongs exist, so the fragment is omitted entirely.
    """
    servers = {}
    for alias, defn in mcp_tongs(merged).items():
        servers[alias] = {"type": "remote", "url": mcp_url(defn, alias), "enabled": True}
    return {"mcp": servers} if servers else {}


def mcp_config_claude(merged):
    """Claude Code `--mcp-config` document for the discovered `mcp` tongs.

    HTTP MCP servers keyed by canonical alias under `mcpServers`, the shape
    Claude reads from the file passed as `claude --mcp-config <path>`. Returns
    `{}` when no `mcp` tongs exist.
    """
    servers = {}
    for alias, defn in mcp_tongs(merged).items():
        servers[alias] = {"type": "http", "url": mcp_url(defn, alias)}
    return {"mcpServers": servers} if servers else {}


# Per-harness MCP emitters, dispatched by harness name, mirroring the EMITTERS
# table in swarmforge/agents/translate.py.
MCP_EMITTERS = {
    "opencode": mcp_config_opencode,
    "claude": mcp_config_claude,
}


def plan_injection(merged, harness):
    """Everything the discovered tongs contribute to one anvil launch.

    Aggregates per-kind env vars and volume mounts across all tongs and the MCP
    config for the named harness. With no tongs (or none the anvil reaches) the
    result is empty env, empty mounts, and an empty MCP config -- the basis of
    the inert-when-empty invariant for this layer.
    """
    env = {}
    mounts = []
    for name in sorted(merged):
        defn = merged[name]["definition"]
        for key, value in anvil_env(name, defn).items():
            # Env-var names are sanitized from tong names (github-creds and
            # github_creds collapse to the same prefix), so distinct tongs can
            # clash. Keep the first by sorted name and warn, mirroring the MCP
            # alias collision guard, rather than silently clobbering.
            if key in env and env[key] != value:
                warn("tong '%s' reuses anvil env var '%s'; ignoring the duplicate" % (name, key))
                continue
            env[key] = value
        mounts.extend(anvil_mounts(name, defn))
    emit = MCP_EMITTERS.get(harness)
    mcp = emit(merged) if emit else {}
    return {"env": env, "mounts": mounts, "mcp": mcp}
