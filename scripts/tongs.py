#!/usr/bin/env python3
"""Pure launcher core for Swarmforge tongs (Swarmforge-managed sidecar processes).

A *tong* is a sibling container started with (or for) an *anvil* (harness
container). Definitions are one YAML file per tong under `.swarmforge/tongs/`,
discovered across the same four layers as agents (lowest to highest precedence):

    user   -> ~/.swarmforge/tongs/        (SWARMFORGE_USER_ASSETS_DIR)
    org    -> $ORG/.swarmforge/tongs/     (SWARMFORGE_ORG_ASSETS_DIR)
    repo   -> <checkout>/tongs/           (SWARMFORGE_REPO_TONGS_DIR)
    workspace -> <workspace>/.swarmforge/tongs/

This module is the pure core of the tongs launcher: layer discovery, name-based
merge with `disable`, schema validation, secret-reference parsing, config-hash
labels, and approval keying. Every function here is side-effect free (aside from
the small JSON/YAML file readers) so it can be unit-tested exactly like
`anvil/translate_agents.py` -- see `scripts/test_tongs.py`.

It performs no orchestration: no docker, no networks, no exec-based secret
resolution, no prompting. Secret resolution is driven by a caller-injected
resolver (see `substitute_secrets`), which keeps the module pure.

YAML parsing reuses the dependency-free subset parser from
`anvil/translate_agents.py` so the launcher needs no third-party packages.
"""

import hashlib
import importlib.util
import json
import os
import re
import sys

# --- Reuse the dependency-free YAML subset parser from translate_agents -------
# Tong files are plain YAML (no frontmatter), but the nested-map / flat-list
# grammar is identical, so we borrow the existing, tested parser rather than
# duplicate it. Loaded by path like scripts/test_translate_agents.py does.

_TA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "anvil",
    "translate_agents.py",
)
_spec = importlib.util.spec_from_file_location("translate_agents", _TA_PATH)
_ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ta)


# --- Schema vocabulary --------------------------------------------------------
# The four definition layers, lowest to highest precedence. The workspace is the
# only untrusted layer (any repo you happened to clone); the rest are installed
# deliberately, which is why only workspace-sourced tongs gate on approval.
USER, ORG, REPO, WORKSPACE = "user", "org", "repo", "workspace"
LAYERS = (USER, ORG, REPO, WORKSPACE)
TRUSTED_LAYERS = frozenset({USER, ORG, REPO})

LIFECYCLES = frozenset({"session", "shared"})
INTERFACE_KINDS = frozenset({"mcp", "port", "volume", "none"})
READINESS_MODES = frozenset({"tcp", "healthcheck", "none"})
TRANSPORTS = frozenset({"http"})  # http only in v1 (stdio defeats the purpose)

# Docker labels stamped onto tong containers. The config-hash label answers
# "did the definition change since this container started?"
LABEL_TONG_NAME = "swarmforge.tong.name"
LABEL_CONFIG_HASH = "swarmforge.tong.config-hash"

# Environment injected into the anvil for `port`/`volume` interfaces. The bare
# tong name is sanitized into an env-safe token: github-creds -> GITHUB_CREDS.
ENV_PREFIX = "SWARMFORGE_TONG"

# Magic mount word that grants docker-socket access. The broker tong is the
# privileged holder; centralized here so the approval gate's privilege summary
# and the broker agree on one spelling.
SOCKET_MOUNT = "docker-socket"


def warn(message):
    print("tongs: %s" % message, file=sys.stderr)


# --- YAML loading -------------------------------------------------------------


def load_yaml(text):
    """Parse a plain-YAML tong document into a dict (empty dict if blank)."""
    lines = text.split("\n")
    data, _ = _ta.parse_map(lines, 0, 0)
    return data


def load_tong_file(path):
    """Read and parse a single tong YAML file. Returns the definition dict."""
    with open(path, "r", encoding="utf-8") as handle:
        return load_yaml(handle.read())


# --- Layer discovery ----------------------------------------------------------


def load_tong_dir(path):
    """Discover tong definitions in one layer directory.

    Returns {tong_name: definition}. The tong name is the filename without its
    `.yaml`/`.yml` extension (filename = tong identity). Missing directories
    yield {} so absent layers are simply empty -- the basis of the
    inert-when-empty invariant. Only top-level files are read.
    """
    out = {}
    if not path or not os.path.isdir(path):
        return out
    for filename in sorted(os.listdir(path)):
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        full = os.path.join(path, filename)
        if not os.path.isfile(full):
            continue
        name = filename.rsplit(".", 1)[0]
        try:
            out[name] = load_tong_file(full)
        except (ValueError, OSError) as exc:
            warn("skipping %s: %s" % (full, exc))
    return out


def discover(layer_dirs):
    """Discover every layer.

    `layer_dirs` is an ordered list of `(layer_name, path)` pairs, lowest to
    highest precedence (see LAYERS). Returns the same ordered list with each
    path replaced by its `{tong_name: definition}` mapping, ready for
    `merge_tongs`.
    """
    return [(layer, load_tong_dir(path)) for layer, path in layer_dirs]


# --- Merge --------------------------------------------------------------------


def merge_tongs(layers):
    """Merge discovered layers by name into the effective tong set.

    `layers` is an ordered list of `(layer_name, {name: definition})` pairs,
    lowest to highest precedence (the output of `discover`). Returns
    `{name: {"source": layer_name, "definition": definition}}`.

    Rules:
      * Merge by name; a higher layer replaces a lower one **wholesale** (never a
        field-merge), like skill packages.
      * `disable: true` switches off an inherited tong and is itself omitted.
      * Privilege: the (untrusted) workspace layer may **disable** a tong owned
        by a trusted layer but may not **redefine** it -- privileged tongs stay
        owned by trusted layers.

    The `source` records the winning layer, which drives approval gating: only
    workspace-sourced tongs prompt (see `is_workspace_sourced`).
    """
    merged = {}
    for layer, tongs in layers:
        for name in sorted(tongs):
            defn = tongs[name]
            disabled = isinstance(defn, dict) and defn.get("disable") is True
            existing = merged.get(name)
            owned_by_trusted = existing is not None and existing["source"] in TRUSTED_LAYERS

            if layer == WORKSPACE and owned_by_trusted:
                # Workspace may switch a trusted tong off, but not redefine it.
                if disabled:
                    merged.pop(name, None)
                else:
                    warn(
                        "workspace tong '%s' cannot redefine the %s-layer "
                        "definition; keeping the trusted one"
                        % (name, existing["source"])
                    )
                continue

            if disabled:
                merged.pop(name, None)
                continue

            merged[name] = {"source": layer, "definition": defn}
    return merged


# --- Schema validation --------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_tong(name, defn):
    """Validate one tong definition against the v1 schema.

    Returns a list of human-readable error strings (empty list == valid). This
    is intentionally permissive about unknown keys (forward compatibility) and
    strict about the fields the launcher must dispatch on.
    """
    errors = []

    def err(msg):
        errors.append("%s: %s" % (name, msg))

    if not isinstance(defn, dict):
        return ["%s: definition must be a mapping" % name]

    lifecycle = defn.get("lifecycle")
    if lifecycle is None:
        err("missing required 'lifecycle'")
    elif lifecycle not in LIFECYCLES:
        err("lifecycle %r must be one of %s" % (lifecycle, sorted(LIFECYCLES)))

    image = defn.get("image")
    if not image or not isinstance(image, str):
        err("missing required 'image' (string)")

    interface = defn.get("interface")
    kind = None
    if not isinstance(interface, dict):
        err("missing required 'interface' mapping")
    else:
        kind = interface.get("kind")
        if kind not in INTERFACE_KINDS:
            err("interface.kind %r must be one of %s" % (kind, sorted(INTERFACE_KINDS)))
        elif kind in ("mcp", "port"):
            if not _is_int(interface.get("port")):
                err("interface.kind=%s requires an integer 'port'" % kind)
            if kind == "mcp":
                if not interface.get("name"):
                    err("interface.kind=mcp requires 'name' (the MCP server name)")
                transport = interface.get("transport", "http")
                if transport not in TRANSPORTS:
                    err("interface.transport %r must be one of %s" % (transport, sorted(TRANSPORTS)))
        elif kind == "volume":
            if not interface.get("volume"):
                err("interface.kind=volume requires 'volume' (named volume)")
            if not interface.get("mountpoint"):
                err("interface.kind=volume requires 'mountpoint' (where the anvil sees it)")

    # Readiness: tcp is the implicit default for mcp/port; volume/none must
    # declare a mode (the launcher refuses to silently fire-and-forget).
    readiness = defn.get("readiness")
    if readiness is not None and not isinstance(readiness, dict):
        err("'readiness' must be a mapping")
        readiness = None
    mode = readiness.get("mode") if isinstance(readiness, dict) else None
    if mode is not None and mode not in READINESS_MODES:
        err("readiness.mode %r must be one of %s" % (mode, sorted(READINESS_MODES)))
    if kind in ("volume", "none") and mode is None:
        err("interface.kind=%s requires an explicit readiness.mode" % kind)
    if mode == "tcp" and kind not in ("mcp", "port"):
        # A TCP probe needs a port to dial; a volume/none tong has none, so this
        # would silently never become ready. Force a compatible mode instead.
        err("readiness.mode=tcp needs a port; interface.kind=%s has none "
            "(use 'healthcheck' or 'none')" % kind)
    if isinstance(readiness, dict):
        # Validate the fields orchestration consumes so a bad value is a clean
        # error here, not an uncaught ValueError/TypeError mid-launch.
        try:
            parse_duration(readiness.get("timeout"), DEFAULT_READINESS_TIMEOUT_S)
        except ValueError as exc:
            err("readiness.timeout: %s" % exc)
        command = readiness.get("command")
        if command is not None and not (
            isinstance(command, list) and command and all(isinstance(c, str) for c in command)
        ):
            err("readiness.command must be a non-empty list of strings")

    env = defn.get("env")
    if env is not None and not isinstance(env, dict):
        err("'env' must be a mapping of name -> value")
    elif isinstance(env, dict):
        plain, secret = partition_secret_env(env)
        for secret_name in sorted(secret):
            if not ENV_NAME_RE.match(secret_name):
                err("invalid secret env name %r (must be a valid identifier)" % secret_name)
                continue
            pointer_name = secret_name + SECRET_FILE_ENV_SUFFIX
            pointer_value = "%s/%s" % (SECRET_TMPFS_DIR, secret_name)
            if pointer_name in plain and plain[pointer_name] != pointer_value:
                err(
                    "env %r collides with the secret file pointer for %r"
                    % (pointer_name, secret_name)
                )

    for listish in ("mounts", "networks"):
        value = defn.get(listish)
        if value is not None and not isinstance(value, list):
            err("'%s' must be a list" % listish)

    return errors


# --- Secret references --------------------------------------------------------
# Tong defs reference secrets as ${secret:<provider>:<ref>}. The provider is a
# single token; the ref may itself contain colons (e.g. op://Work/github/token),
# so it runs greedily up to the closing brace.
SECRET_REF_RE = re.compile(r"\$\{secret:([^:}]+):([^}]+)\}")


def parse_secret_ref(text):
    """Parse a string that is exactly one secret reference.

    Returns `(provider, ref)` or None if the whole string is not a single
    reference. For substring scanning use `find_secret_refs`.
    """
    if not isinstance(text, str):
        return None
    match = SECRET_REF_RE.fullmatch(text.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def find_secret_refs(value):
    """Recursively collect every secret reference in a definition.

    Returns a de-duplicated, order-preserving list of `(provider, ref)` tuples
    found in any string anywhere in the (possibly nested) value. Used by the
    privilege summary and, later, by secret resolution.
    """
    found = []
    seen = set()

    def walk(node):
        if isinstance(node, str):
            for match in SECRET_REF_RE.finditer(node):
                pair = (match.group(1), match.group(2))
                if pair not in seen:
                    seen.add(pair)
                    found.append(pair)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def substitute_secrets(value, resolver):
    """Return a copy of `value` with every secret reference resolved.

    `resolver(provider, ref) -> str` is injected by the caller, keeping this
    function pure. Every match -- including multiple references embedded in one
    string -- is replaced.
    """
    if isinstance(value, str):
        return SECRET_REF_RE.sub(
            lambda m: resolver(m.group(1), m.group(2)), value
        )
    if isinstance(value, dict):
        return {k: substitute_secrets(v, resolver) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_secrets(item, resolver) for item in value]
    return value


# --- Secret providers ---------------------------------------------------------
# A secret reference (${secret:<provider>:<ref>}) is resolved on the host by
# shelling out to a provider CLI -- the docker-credential-helper pattern, so
# Swarmforge knows nothing about any individual secret manager. Providers are
# declared once in the user layer (~/.swarmforge/secret-providers.yaml):
#
#     providers:
#       op:   ["op", "read", "{ref}"]
#       pass: ["pass", "show", "{ref}"]
#
# Each value is an argv template; the literal token "{ref}" in any element is
# replaced with the reference. Loading the table and building the argv are pure
# and live here; the subprocess that actually runs the CLI is the caller's (see
# run_anvil.make_secret_resolver), keeping this module side-effect free.

SECRET_REF_TOKEN = "{ref}"


def load_secret_providers(path):
    """Load the user-layer secret-provider table.

    Returns `{provider: [argv template, ...]}`. A missing file (or one without a
    `providers:` block) yields `{}` -- no providers configured, so resolving any
    secret reference later fails loudly rather than silently. Raises `ValueError`
    if the file is present but malformed, so a typo surfaces at load time instead
    of dropping a provider.

    Command templates must be single-line flow lists; the dependency-free YAML
    subset parser does not join a list wrapped across lines.
    """
    if not path or not os.path.isfile(path):
        return {}
    data = load_tong_file(path)
    providers = data.get("providers") if isinstance(data, dict) else None
    if providers is None:
        return {}
    if not isinstance(providers, dict):
        raise ValueError("secret-providers: 'providers' must be a mapping")
    out = {}
    for name, template in providers.items():
        if not isinstance(template, list) or not template:
            raise ValueError(
                "secret-providers: provider %r must be a non-empty command list" % name
            )
        if not all(isinstance(part, str) for part in template):
            raise ValueError(
                "secret-providers: provider %r command must be a list of strings" % name
            )
        out[name] = list(template)
    return out


def secret_provider_command(providers, provider, ref):
    """Concrete argv that resolves `ref` through `provider`.

    Substitutes the literal `{ref}` token in every element of the provider's argv
    template. Raises `KeyError` if the provider is not declared (the caller turns
    this into a clean launch error naming the missing provider).
    """
    return [part.replace(SECRET_REF_TOKEN, ref) for part in providers[provider]]


# --- Secret delivery ----------------------------------------------------------
# A resolved secret must never reach a tong as a docker `-e` env var: anything
# holding the docker socket (the broker tong) could read it back via
# `docker inspect`. Instead each secret-bearing env var is delivered as a file on
# an in-memory tmpfs the launcher populates at startup, and the tong is pointed
# at the file with a `<NAME>_FILE` env var (the conventional docker-secret
# indirection). Plain (non-secret) env keeps flowing through `-e` unchanged.

SECRET_TMPFS_DIR = "/run/swarmforge/secrets"
SECRET_FILE_ENV_SUFFIX = "_FILE"

# A secret's file lands at SECRET_TMPFS_DIR/<env name>, so the env name becomes a
# path component. Restricting it to the POSIX env-name grammar keeps that path
# inside the tmpfs dir -- a name like "../../etc/foo" from an untrusted workspace
# tong cannot escape it -- and is exactly what docker accepts for an env var.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def partition_secret_env(env):
    """Split a tong's env into `(plain, secret)` by secret-reference presence.

    `env` is the tong definition's `env` mapping (values may be unresolved
    `${secret:...}` references). `plain` holds values with no secret reference
    (safe to pass straight through as `-e`); `secret` holds the keys whose value
    contains at least one reference (routed to tmpfs delivery so the resolved
    value never appears in `docker inspect`). Order within each is preserved.
    """
    plain, secret = {}, {}
    for key, value in (env or {}).items():
        if find_secret_refs(value):
            secret[key] = value
        else:
            plain[key] = value
    return plain, secret


def secret_delivery_plan(resolved_secrets):
    """Plan tmpfs delivery for already-resolved secret env values.

    `resolved_secrets` is `{env_name: secret_value}`. Returns plain data the
    launcher applies when it starts the tong:

      * `tmpfs` -- the in-tong tmpfs mountpoint to create (`--tmpfs`), so the
                   secret files live in memory and never touch disk, or `None`
                   when there are no secrets.
      * `files` -- `{absolute_path: secret_value}` the launcher writes into the
                   running container's tmpfs (never to the host).
      * `env`   -- `{<NAME>_FILE: absolute_path}` pointing the tong at each file;
                   these are paths, not secrets, so they are safe as `-e`.

    With no secrets the tmpfs/files/env are all empty, so a tong without secrets
    gets no tmpfs mount and no indirection. Raises `ValueError` for an env name
    that is not a valid identifier, so a name that would escape the tmpfs dir
    when used as a path component stops the launch rather than reaching disk.
    """
    files = {}
    env = {}
    for name in sorted(resolved_secrets):
        if not ENV_NAME_RE.match(name):
            raise ValueError("invalid secret env name %r (must be a valid identifier)" % name)
        path = "%s/%s" % (SECRET_TMPFS_DIR, name)
        files[path] = resolved_secrets[name]
        env[name + SECRET_FILE_ENV_SUFFIX] = path
    return {"tmpfs": SECRET_TMPFS_DIR if files else None, "files": files, "env": env}


def plan_tong_secrets(env, resolver):
    """Resolve a tong's secret env and plan how the tong receives it.

    Combines the steps the launcher performs for one tong's environment:
    partition `env` into plain and secret, resolve only the secret-bearing values
    through the injected `resolver(provider, ref) -> str` (keeping this function
    pure), then deliver those via tmpfs. Returns:

      * `env`   -- plain env vars plus the `<NAME>_FILE` pointers (never a secret
                   value).
      * `tmpfs` -- the tmpfs mountpoint to create, or `None` when no secrets.
      * `files` -- `{absolute_path: secret_value}` to write into the tmpfs.

    The resolved secret values appear only under `files`, never under `env`, so
    nothing the launcher passes as `-e` is readable through `docker inspect`.
    """
    plain, secret = partition_secret_env(env)
    resolved = {key: substitute_secrets(value, resolver) for key, value in secret.items()}
    delivery = secret_delivery_plan(resolved)
    merged_env = dict(plain)
    for key, value in delivery["env"].items():
        # A file pointer (TOKEN_FILE) can collide with a plain env var of the same
        # name. Unless it already points at the generated path, fail rather than
        # launching a tong that cannot find its secret.
        if key in merged_env:
            if merged_env[key] == value:
                continue
            raise ValueError(
                "tong env %r collides with the secret file pointer for %r"
                % (key, key[:-len(SECRET_FILE_ENV_SUFFIX)])
            )
        merged_env[key] = value
    return {"env": merged_env, "tmpfs": delivery["tmpfs"], "files": delivery["files"]}


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
# table in translate_agents.py.
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


# --- Session networks ---------------------------------------------------------
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


def _is_network_facing(defn):
    """True if the anvil reaches this tong over the network (mcp or port).

    `volume` and `none` tongs have no listener, so they need no network alias.
    """
    return (defn.get("interface") or {}).get("kind") in ("mcp", "port")


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
      * `session_aliases` -- `[(tong_name, alias)]` for each network-facing
                             `session` tong, attached to the per-session network
                             under its canonical alias.
      * `shared_connect`  -- `[(tong_name, alias)]` for each network-facing
                             `shared` tong, connected to the per-session network
                             under its canonical alias and disconnected on
                             teardown.

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
    # resolving to the same canonical alias would collide there -- DNS would
    # resolve nondeterministically. Keep the first by sorted tong name and drop
    # the rest with a warning, mirroring the MCP-config and env-var collision
    # guards. One pass over both lifecycles keeps the winner deterministic
    # regardless of whether the loser is a `session` or `shared` tong.
    session_aliases = []
    shared_connect = []
    seen = {}
    for name in sorted(merged):
        defn = merged[name]["definition"]
        lifecycle = defn.get("lifecycle")
        if lifecycle not in LIFECYCLES or not _is_network_facing(defn):
            continue
        alias = canonical_alias(name, defn)
        if alias in seen:
            warn(
                "tong '%s' reuses network alias '%s' (already used by '%s'); "
                "ignoring the duplicate" % (name, alias, seen[alias])
            )
            continue
        seen[alias] = name
        (session_aliases if lifecycle == "session" else shared_connect).append((name, alias))
    return {
        "network": net,
        "create": net,
        "extra_networks": [base_network] if base_network else [],
        "session_aliases": session_aliases,
        "shared_connect": shared_connect,
    }


# --- Config hash --------------------------------------------------------------


def config_hash(defn):
    """Stable SHA-256 hex digest of a definition.

    Canonical JSON (sorted keys) makes the hash independent of mapping order.
    The same function serves two callers: the approval hash is taken over the
    merged definition before secret resolution, and the staleness label hash
    over the resolved definition. Callers choose the input.
    """
    canonical = json.dumps(defn, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Privilege summary --------------------------------------------------------


def _has_socket_mount(defn):
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == SOCKET_MOUNT:
            return True
    return False


def privilege_summary(defn):
    """Structured summary of what a definition asks for, for the approval gate.

    Gathers the privileges a reviewer must see before approving a
    workspace-sourced tong: image, secret references, mounts, networks, and
    docker-socket access. Rendering and prompting are the caller's job; this
    just assembles the facts.
    """
    return {
        "image": defn.get("image"),
        "secrets": [{"provider": p, "ref": r} for p, r in find_secret_refs(defn)],
        "mounts": list(defn.get("mounts") or []),
        "networks": list(defn.get("networks") or []),
        "socket": _has_socket_mount(defn),
    }


# --- Approval keying ----------------------------------------------------------
# Approvals are keyed by workspace path + tong name + definition hash and stored
# in the user layer (~/.swarmforge/approvals.json). Any change to the definition
# changes its hash and re-prompts. Only workspace-sourced tongs gate.


def is_workspace_sourced(source_layer):
    """True if a tong's winning layer is the (untrusted) workspace and so gates."""
    return source_layer == WORKSPACE


def load_approvals(path):
    """Load the approvals store, returning {} when it is absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_approvals(path, approvals):
    """Persist the approvals store as pretty JSON, creating parent dirs."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(approvals, handle, indent=2, sort_keys=True)
        handle.write("\n")


def is_approved(approvals, workspace_path, name, defn):
    """True if `defn` (by its config hash) is approved for this workspace+tong.

    Fails closed (returns False) on a missing or malformed store entry rather
    than raising -- a hand-edited approvals.json must never crash the gate.
    """
    entry = approvals.get(workspace_path)
    if not isinstance(entry, dict):
        return False
    return entry.get(name) == config_hash(defn)


def record_approval(approvals, workspace_path, name, defn):
    """Return `approvals` updated to approve `defn` for this workspace+tong.

    Mutates and returns the store (same object) so callers can persist it.
    """
    approvals.setdefault(workspace_path, {})[name] = config_hash(defn)
    return approvals


# --- Readiness ----------------------------------------------------------------
# A tong's `readiness:` declaration says how the launcher decides the tong is up
# before it gates the anvil on it. `tcp` is the implicit default for the
# network-facing kinds (mcp/port); `volume`/`none` must declare a mode (validation
# enforces this). These helpers resolve the declaration to plain values; the
# probing itself (docker exec / a throwaway probe container / inspecting the
# image healthcheck) is the launcher's side-effectful job.

DEFAULT_READINESS_TIMEOUT_S = 30.0
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(value, default=None):
    """Parse a `30s`/`500ms`/`2m` (or bare-number seconds) duration to float seconds.

    A plain int/float is taken as seconds. `None` yields `default`. Raises
    `ValueError` for anything else, so a typo in `readiness.timeout` stops the
    launch rather than silently falling back.
    """
    if value is None:
        return default
    if _is_int(value) or isinstance(value, float):
        return float(value)
    if not isinstance(value, str):
        raise ValueError("duration must be a string or number, got %r" % (value,))
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError("invalid duration %r" % (value,))
    return float(match.group(1)) * _DURATION_UNITS[match.group(2)]


def readiness_settings(defn):
    """Resolve a tong's readiness declaration to `(mode, command, timeout_s)`.

    `mode` defaults to `tcp` for the network-facing kinds (mcp/port) when not
    declared; `command` is the optional exec used by `healthcheck`; `timeout_s`
    is the parsed `readiness.timeout` (default 30s). Assumes a validated
    definition, so a portless kind already carries an explicit mode.
    """
    interface = defn.get("interface") or {}
    readiness = defn.get("readiness") or {}
    mode = readiness.get("mode")
    if mode is None:
        mode = "tcp" if interface.get("kind") in ("mcp", "port") else "none"
    timeout_s = parse_duration(readiness.get("timeout"), DEFAULT_READINESS_TIMEOUT_S)
    return mode, readiness.get("command"), timeout_s


# --- Diagnostic CLI -----------------------------------------------------------
# Not wired into any launch path. `tongs.py validate <dir>...` lints definitions
# layered lowest-to-highest; `tongs.py discover <dir>...` dumps the merged set.


def _layer_dirs_from_argv(paths):
    # Map positional dirs onto LAYERS lowest-first; extra dirs keep the last name.
    pairs = []
    for index, path in enumerate(paths):
        layer = LAYERS[index] if index < len(LAYERS) else LAYERS[-1]
        pairs.append((layer, path))
    return pairs


def main(argv):
    if len(argv) < 2 or argv[0] not in ("validate", "discover"):
        print("usage: tongs.py {validate|discover} <layer_dir>...", file=sys.stderr)
        return 2
    command, paths = argv[0], argv[1:]
    merged = merge_tongs(discover(_layer_dirs_from_argv(paths)))

    if command == "validate":
        problems = 0
        for name in sorted(merged):
            for error in validate_tong(name, merged[name]["definition"]):
                print(error)
                problems += 1
        if not problems:
            print("ok: %d tong(s) valid" % len(merged))
        return 1 if problems else 0

    summary = {
        name: {"source": entry["source"], "definition": entry["definition"]}
        for name, entry in merged.items()
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
