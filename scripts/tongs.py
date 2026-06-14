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

    env = defn.get("env")
    if env is not None and not isinstance(env, dict):
        err("'env' must be a mapping of name -> value")

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


# --- Environment-variable naming ----------------------------------------------


def tong_env_prefix(name):
    """Canonical env-var prefix for a tong: github-creds -> SWARMFORGE_TONG_GITHUB_CREDS."""
    token = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return "%s_%s" % (ENV_PREFIX, token)


def tong_env_var(name, suffix):
    """Canonical env-var name, e.g. tong_env_var('pg', 'PORT') -> SWARMFORGE_TONG_PG_PORT."""
    return "%s_%s" % (tong_env_prefix(name), suffix.upper())


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
