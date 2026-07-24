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

# A broker tong holds the docker socket and spawns its own worker containers. A
# container cannot re-share the bind mounts it received, so a broker that wants
# to mount the session workspace into a worker needs the workspace's *host* path
# (the path the daemon understands), not the in-container mount point. The
# launcher injects it here for socket-holding tongs; non-broker tongs never see
# it, so the passthrough behavior for ordinary tongs is unchanged.
WORKSPACE_HOST_ENV = "SWARMFORGE_WORKSPACE_HOST_PATH"


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
        _, secret = partition_secret_env(env)
        for secret_name in sorted(secret):
            if not ENV_NAME_RE.match(secret_name):
                err("invalid secret env name %r (must be a valid identifier)" % secret_name)

    # `entrypoint`/`command` override the image's entrypoint/command (and what the
    # secret-injection wrapper execs), so they must be argv lists of strings.
    for argvish in ("entrypoint", "command"):
        value = defn.get(argvish)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(part, str) for part in value)
        ):
            err("'%s' must be a list of strings" % argvish)

    for listish in ("mounts", "networks"):
        value = defn.get(listish)
        if value is not None and not isinstance(value, list):
            err("'%s' must be a list" % listish)

    # Entry-level checks for the values orchestration turns into docker flags, so
    # a malformed entry fails validation rather than raising during the launch.
    mounts = defn.get("mounts")
    if isinstance(mounts, list):
        for mount in mounts:
            if not isinstance(mount, str):
                err("mount entries must be strings, got %r" % (mount,))
                continue
            word, sep, mode = mount.partition(":")
            if word not in (WORKSPACE_MOUNT, SOCKET_MOUNT):
                err("unknown mount %r (expected '%s' or '%s')"
                    % (mount, WORKSPACE_MOUNT, SOCKET_MOUNT))
                continue
            # `tong_mount_specs` forwards everything after the first colon verbatim
            # as the docker mount mode; the `workspace:/target` form is broker-config
            # only, not valid in a tong definition.
            if sep and mode not in ("ro", "rw"):
                err("mount %r has an invalid mode %r (expected 'ro' or 'rw'; the "
                    "'workspace:/target' form is broker-config only, not a tong mount)"
                    % (mount, mode))

    networks = defn.get("networks")
    if isinstance(networks, list):
        for network in networks:
            if not isinstance(network, str):
                err("network entries must be strings, got %r" % (network,))

    resources = defn.get("resources")
    if resources is not None and not isinstance(resources, dict):
        err("'resources' must be a mapping")
    elif isinstance(resources, dict):
        memory = resources.get("memory")
        if memory is not None and not (
            isinstance(memory, str) or _is_int(memory) or isinstance(memory, float)
        ):
            err("resources.memory must be a string or number")

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
# replaced with the reference.
#
# A provider value may instead be a *structured* entry, so a shared (org-layer)
# tong can reference `${secret:<provider>:<ref>}` while each developer's personal
# table decides how each individual secret is fetched -- one dev's `pass`, another
# dev's `1Password`, all under the same reference:
#
#     providers:
#       shared:
#         default: ["pass", "show", "{ref}"]        # fallback for unlisted refs
#         overrides:
#           ci-token: ["doppler", "secrets", "get", "CI_TOKEN", "--plain"]
#
# Resolving `${secret:shared:<ref>}` uses the argv in `overrides` for that ref,
# falling back to `default`; a ref with neither raises `UnmappedSecretError`.
# `default` and `overrides` live in separate namespaces on purpose: a secret
# literally named "default" is just `overrides.default`, distinct from the
# fallback, and any other key at the provider level is a typo caught at load.
#
# Loading the table and building the argv are pure and live here; the subprocess
# that actually runs the CLI is the caller's (see run_anvil.make_secret_resolver),
# keeping this module side-effect free.

SECRET_REF_TOKEN = "{ref}"

# The two keys a structured provider entry may hold. `default` is the argv used
# for any ref `overrides` does not name, so a table can override a couple of
# secrets without re-declaring the command for all the rest. Kept as a frozenset
# so an unrecognized key (a typo) fails loudly at load rather than being ignored.
PROVIDER_DEFAULT_KEY = "default"
PROVIDER_OVERRIDES_KEY = "overrides"
PROVIDER_ENTRY_KEYS = frozenset({PROVIDER_DEFAULT_KEY, PROVIDER_OVERRIDES_KEY})


class UnmappedSecretError(Exception):
    """A structured provider entry declares no command for this ref.

    Distinct from the `KeyError` raised for an unknown provider so the caller can
    report the two misconfigurations differently. Carries `provider`/`ref` (never
    a resolved value) for a clean, leak-free launch error.
    """

    def __init__(self, provider, ref):
        self.provider = provider
        self.ref = ref
        super().__init__(
            "no command mapped for secret %r under provider %r" % (ref, provider)
        )


def _coerce_provider_command(label, template):
    """Validate one argv template, returning a fresh copy.

    `label` names the offending entry in error messages (e.g. `provider 'op'` or
    `provider 'shared' override 'ci-token'`). Raises `ValueError` for anything
    that is not a non-empty list of strings, so a typo surfaces at load time.
    """
    if not isinstance(template, list) or not template:
        raise ValueError(
            "secret-providers: %s must be a non-empty command list" % label
        )
    if not all(isinstance(part, str) for part in template):
        raise ValueError(
            "secret-providers: %s command must be a list of strings" % label
        )
    return list(template)


def _load_provider_entry(name, entry):
    """Validate one structured provider entry into `{default, overrides}`.

    `entry` is the mapping under a provider name. Recognizes only `default` (an
    argv template) and `overrides` (a `{ref: argv}` map); any other key, a
    non-mapping `overrides`, or an entry declaring neither raises `ValueError` so
    misconfiguration surfaces at load. Returns a normalized dict with a `default`
    of `None` when absent and an `overrides` map (possibly empty).
    """
    unknown = set(entry) - PROVIDER_ENTRY_KEYS
    if unknown:
        raise ValueError(
            "secret-providers: provider %r has unknown key(s) %s; only %s are "
            "allowed" % (
                name,
                ", ".join(repr(k) for k in sorted(unknown)),
                " and ".join(repr(k) for k in sorted(PROVIDER_ENTRY_KEYS)),
            )
        )
    default = entry.get(PROVIDER_DEFAULT_KEY)
    if default is not None:
        default = _coerce_provider_command("provider %r default" % name, default)
    raw_overrides = entry.get(PROVIDER_OVERRIDES_KEY)
    if raw_overrides is not None and not isinstance(raw_overrides, dict):
        raise ValueError(
            "secret-providers: provider %r 'overrides' must be a mapping" % name
        )
    overrides = {
        ref: _coerce_provider_command("provider %r override %r" % (name, ref), template)
        for ref, template in (raw_overrides or {}).items()
    }
    if default is None and not overrides:
        raise ValueError(
            "secret-providers: provider %r must declare 'default' and/or "
            "'overrides'" % name
        )
    return {PROVIDER_DEFAULT_KEY: default, PROVIDER_OVERRIDES_KEY: overrides}


def load_secret_providers(path):
    """Load the user-layer secret-provider table.

    Returns `{provider: entry}` where each `entry` is either a single argv
    template (`[str, ...]`, one command for every ref) or a structured mapping
    (`{"default": argv_or_None, "overrides": {ref: argv}}`) that resolves each ref
    through its own command. A missing file (or one without a `providers:` block)
    yields `{}` -- no providers configured, so resolving any secret reference
    later fails loudly rather than silently. Raises `ValueError` if the file is
    present but malformed, so a typo surfaces at load time instead of dropping a
    provider.

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
    for name, entry in providers.items():
        if isinstance(entry, dict):
            out[name] = _load_provider_entry(name, entry)
        else:
            out[name] = _coerce_provider_command("provider %r" % name, entry)
    return out


def secret_provider_command(providers, provider, ref):
    """Concrete argv that resolves `ref` through `provider`.

    Substitutes the literal `{ref}` token in every element of the provider's argv
    template. A structured provider resolves `ref` through its `overrides` map,
    falling back to `default`. Raises `KeyError` if the provider is not declared,
    and `UnmappedSecretError` if a structured provider covers neither `ref` nor
    `default` (the caller turns each into a clean launch error).
    """
    entry = providers[provider]
    if isinstance(entry, dict):
        template = entry[PROVIDER_OVERRIDES_KEY].get(ref)
        if template is None:
            template = entry[PROVIDER_DEFAULT_KEY]
        if template is None:
            raise UnmappedSecretError(provider, ref)
    else:
        template = entry
    return [part.replace(SECRET_REF_TOKEN, ref) for part in template]


# --- Secret delivery ----------------------------------------------------------
# A resolved secret must never reach a tong as a docker `-e` env var, a command
# argument, or a file on disk: anything holding the docker socket (the broker
# tong) could read an `-e` value back via `docker inspect`. Instead the launcher
# hands the secret-bearing env to the tong over a host FIFO bind-mounted into the
# container, and wraps the tong's entrypoint with a `/bin/sh` prologue that reads
# the FIFO, exports each value into its own environment, then execs the image's
# real entrypoint+command. The bytes travel through the kernel pipe buffer -- never
# a file, an argv, or the container's `Config.Env` -- and arrive as ordinary
# environment variables, so an unmodified server that reads them from its
# environment at startup works unchanged. Plain (non-secret) env keeps flowing
# through `-e`, which is safe because those values are not secret.

# Where the FIFO is bind-mounted inside the tong, and the shell the wrapper runs.
SECRET_FIFO_TARGET = "/run/swarmforge/secret-env"
SECRET_INJECT_SHELL = "/bin/sh"

# A secret env name becomes a shell assignment target (`export NAME=...`), so it
# must be a valid identifier -- which is exactly what docker accepts for an env
# var, and what keeps a hostile name from being anything but a variable name.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def partition_secret_env(env):
    """Split a tong's env into `(plain, secret)` by secret-reference presence.

    `env` is the tong definition's `env` mapping (values may be unresolved
    `${secret:...}` references). `plain` holds values with no secret reference
    (safe to pass straight through as `-e`); `secret` holds the keys whose value
    contains at least one reference (delivered over the FIFO so the resolved value
    never appears in `docker inspect`). Order within each is preserved.
    """
    plain, secret = {}, {}
    for key, value in (env or {}).items():
        if find_secret_refs(value):
            secret[key] = value
        else:
            plain[key] = value
    return plain, secret


def plan_tong_secrets(env, resolver):
    """Resolve a tong's secret env and split it from the plain env.

    Partitions `env` into plain and secret-bearing values, resolves only the
    secret-bearing ones through the injected `resolver(provider, ref) -> str`
    (keeping this function pure), and returns:

      * `env`     -- the plain env vars, safe to pass straight through as `-e`.
      * `secrets` -- `{name: resolved value}` to hand the tong over the FIFO.

    Resolved values appear only under `secrets`; nothing here is passed as `-e`,
    so no secret is readable through `docker inspect`.
    """
    plain, secret = partition_secret_env(env)
    resolved = {key: substitute_secrets(value, resolver) for key, value in secret.items()}
    return {"env": dict(plain), "secrets": resolved}


def render_secret_exports(resolved_secrets):
    """POSIX-sh that exports already-resolved secret env, for writing to the FIFO.

    `resolved_secrets` is `{env_name: value}`. Returns one `export NAME='value'`
    line per entry (sorted), with the value single-quoted and embedded single
    quotes escaped as `'\\''`, so an arbitrary value -- including newlines or shell
    metacharacters -- cannot break out of its assignment. The tong's entrypoint
    wrapper `eval`s this text, so the launcher (never the secret content) controls
    the quoting. Raises `ValueError` for a name that is not a valid identifier.
    """
    lines = []
    for name in sorted(resolved_secrets):
        if not ENV_NAME_RE.match(name):
            raise ValueError("invalid secret env name %r (must be a valid identifier)" % name)
        quoted = "'" + resolved_secrets[name].replace("'", "'\\''") + "'"
        lines.append("export %s=%s\n" % (name, quoted))
    return "".join(lines)


def secret_inject_argv(target_argv):
    """`(entrypoint, command)` that loads FIFO secrets then execs the real argv.

    `target_argv` is the tong image's real entrypoint+command (the process the
    tong would have run without secret injection). Returns the `--entrypoint`
    (`/bin/sh`) and the command tokens for `tong_run_argv`: a `-c` prologue that
    reads the bind-mounted FIFO, exports each `NAME=value` into the environment,
    then `exec`s `target_argv`. The blocking read of the FIFO is also the
    synchronization point -- the wrapper waits there until the launcher delivers --
    so the real process never starts before its secret env is set.
    """
    script = (
        'secret_env=$(cat %s) || exit 1; '
        'eval "$secret_env" || exit 1; '
        'exec "$@"'
    ) % SECRET_FIFO_TARGET
    return SECRET_INJECT_SHELL, ["-c", script, "swarmforge-tong"] + list(target_argv)


def resolve_exec_target(defn, image_entrypoint, image_cmd):
    """The argv the tong should ultimately exec, given the image's defaults.

    Overriding `--entrypoint` to inject the secret wrapper drops the image's own
    entrypoint/command, so the launcher must restore them. A tong definition may
    set them explicitly via `entrypoint:`/`command:` (lists); otherwise the
    image's own values (`image_entrypoint`, `image_cmd`, read from
    `docker inspect`) are used. The result is `entrypoint + command`. Raises
    `ValueError` if that is empty -- there would be no process to exec after the
    wrapper, so the definition must declare a `command`.
    """
    entrypoint = defn.get("entrypoint")
    if entrypoint is None:
        entrypoint = image_entrypoint or []
    command = defn.get("command")
    if command is None:
        command = image_cmd or []
    target = list(entrypoint) + list(command)
    if not target:
        raise ValueError(
            "cannot inject secrets: image %r declares no entrypoint or command to "
            "exec; set 'command' in the tong definition" % defn.get("image")
        )
    return target


def declared_run_override(defn):
    """`(--entrypoint token, trailing args)` for a tong's declared overrides.

    Applied on the non-secret launch path, where there is no `/bin/sh` wrapper to
    restore the image defaults, so a tong's `entrypoint:`/`command:` must be turned
    into ordinary `docker run` overrides. A declared `command:` overrides the image
    `CMD` (it trails the image). A declared `entrypoint:` overrides the image
    `ENTRYPOINT`; docker's `--entrypoint` takes a single token, so any extra
    entrypoint tokens lead the trailing args. Returns `(None, [])` when the tong
    declares neither, leaving the image's own entrypoint and command untouched.
    """
    entrypoint = defn.get("entrypoint") or []
    command = defn.get("command") or []
    if entrypoint:
        return entrypoint[0], list(entrypoint[1:]) + list(command)
    return None, list(command)


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


def alias_collisions(merged):
    """Tong names grouped by canonical alias, for aliases claimed by >1 tong.

    Returns `{alias: [tong names]}` for the aliases more than one tong resolves
    to (empty when every alias is unique). Two tongs on one network cannot share
    a DNS alias without nondeterministic resolution, so the live launcher refuses
    such a set; the planning functions that build per-anvil config instead keep
    the first and warn. Only network-facing tongs (mcp/port) claim a
    `--network-alias`, so volume/none tongs are skipped -- they never register a
    DNS name and so cannot collide.
    """
    by_alias = {}
    for name in sorted(merged):
        defn = merged[name]["definition"]
        if not _is_network_facing(defn):
            continue
        alias = canonical_alias(name, defn)
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
        seconds = float(value)
    elif not isinstance(value, str):
        raise ValueError("duration must be a string or number, got %r" % (value,))
    else:
        match = _DURATION_RE.match(value.strip())
        if not match:
            raise ValueError("invalid duration %r" % (value,))
        seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2)]
    if seconds <= 0:
        # A non-positive readiness deadline is never useful -- it gives the
        # probe no time to succeed -- so reject it here rather than letting the
        # launch fail mysteriously when nothing ever reports ready.
        raise ValueError("duration must be positive, got %r" % (value,))
    return seconds


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


# --- Docker argv assembly -----------------------------------------------------
# The launcher turns a validated definition (plus the env/secret/network plan the
# functions above produce) into the concrete `docker run` argv for a tong, and
# rewrites the anvil's own argv to reach the tongs. These builders are pure --
# they return argv lists, run no docker -- so the exact flags can be unit-tested;
# `run_anvil.py` owns the side-effectful execution.

# Mount magic words (decision: opt-in words, never raw host paths from a
# definition). `workspace` mounts the session's workspace; `docker-socket` grants
# docker control (the broker's privilege, surfaced by the approval gate). An
# optional `:mode` suffix (e.g. `workspace:ro`) is forwarded to docker verbatim.
WORKSPACE_MOUNT = "workspace"
WORKSPACE_MOUNT_TARGET = "/workspace"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"

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


def tong_mount_specs(defn, workspace, socket_path=DEFAULT_DOCKER_SOCKET):
    """Concrete docker `-v` specs for a tong's `mounts:` magic words.

    Returns the list of `-v` *values* (the orchestrator pairs each with a `-v`
    flag). `workspace[:mode]` mounts the session workspace at /workspace;
    `docker-socket[:mode]` bind-mounts the host docker socket. Raises
    `ValueError` for an unknown magic word, a non-string entry, or a `workspace`
    mount when no workspace path is known -- a definition never names a raw host
    path, so anything else is a mistake that should stop the launch.
    """
    specs = []
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            raise ValueError("mount entries must be strings, got %r" % (mount,))
        word, sep, mode = mount.partition(":")
        if word == WORKSPACE_MOUNT:
            if not workspace:
                raise ValueError("mount 'workspace' requested but no workspace path is known")
            spec = "%s:%s" % (workspace, WORKSPACE_MOUNT_TARGET)
        elif word == SOCKET_MOUNT:
            spec = "%s:%s" % (socket_path, socket_path)
        else:
            raise ValueError(
                "unknown mount %r (expected '%s' or '%s')"
                % (mount, WORKSPACE_MOUNT, SOCKET_MOUNT)
            )
        if sep and mode:
            spec += ":" + mode
        specs.append(spec)
    return specs


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
):
    """Full `docker run -d` argv that starts one tong container.

    Assumes a validated definition. The container is detached (the launcher
    manages its lifecycle explicitly rather than tying it to the launcher's tty),
    named `container_name`, joined to `network` under `alias` as a
    `--network-alias` (only for network-facing tongs -- `volume`/`none` tongs need
    no DNS name), and stamped with the tong-name and config-hash labels so a later
    launch can detect a stale `shared` container. `env` (the tong's plain,
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
    """
    if entrypoint is None and command is None:
        entrypoint, command = declared_run_override(defn)
    command = list(command or [])
    argv = ["docker", "run", "-d", "--name", container_name]
    if network:
        argv += ["--network", network]
    if alias and _is_network_facing(defn):
        argv += ["--network-alias", alias]
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
