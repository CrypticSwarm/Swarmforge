"""Secret references, the provider table that resolves them, and their delivery.

A definition names a secret as `${secret:<provider>:<ref>}`, the user-layer
provider table says which CLI fetches it, and delivery hands the resolved value
to the tong over a FIFO instead of a docker `-e` flag. Everything here is pure:
running a provider CLI and writing the FIFO are the caller's job (see
`swarmforge.anvil`), so the parsing, the table, and the shell the tong runs can
all be unit-tested without a subprocess.
"""

import os
import re

from .discovery import load_tong_file


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
# that actually runs the CLI is the caller's (see
# swarmforge.anvil.make_secret_resolver), keeping this module side-effect free.

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
# tong) could read an `-e` value back via `docker inspect`. Instead the tong's
# entrypoint is wrapped with a `/bin/sh` prologue that creates a FIFO *inside*
# the container (on a tmpfs the launcher mounts at `SECRET_FIFO_DIR`), blocks
# reading it, exports each delivered value into its own environment, then execs
# the image's real entrypoint+command. The launcher delivers the `export` script
# through `docker exec -i ... cat > <fifo>`, fed on stdin. The bytes travel
# docker's API stream into the container kernel's pipe buffer -- never a file, an
# argv, or the container's `Config.Env` -- and arrive as ordinary environment
# variables, so an unmodified server that reads them from its environment at
# startup works unchanged. Plain (non-secret) env keeps flowing through `-e`,
# which is safe because those values are not secret.
#
# The FIFO lives in the container rather than on the host because a host FIFO
# only works when host and container share a kernel: under Docker Desktop
# (macOS/Windows) containers run in a VM and bind mounts cross the boundary via
# a file-sharing layer (virtiofs) that shares file data, not pipe semantics.
# Everything used here -- `docker exec -i`, a tmpfs, `mkfifo` in the container --
# is served by the VM's own kernel, so it behaves identically on every platform.
# FIFO open/EOF semantics double as the synchronization; `secret_inject_argv`
# and `secret_deliver_command` each describe their half of the handshake.

# The tmpfs holding the FIFO inside the tong, the FIFO itself, and the shell the
# wrapper runs. The tmpfs keeps even the FIFO inode out of the container's
# writable layer and guarantees the wrapper a writable path on any image. Its
# options are pinned rather than left to engine defaults: `mode=1777` so a
# non-root image user can `mkfifo` even where the engine would inherit a
# stricter mode from a directory the image ships at this path.
SECRET_FIFO_DIR = "/run/swarmforge"
SECRET_FIFO_TMPFS = SECRET_FIFO_DIR + ":rw,nosuid,nodev,noexec,mode=1777"
SECRET_FIFO_TARGET = SECRET_FIFO_DIR + "/secret-env"
SECRET_INJECT_SHELL = "/bin/sh"

# Exit code the delivery script reserves for "the wrapper has not created the
# FIFO yet"; the launcher retries on it and treats any other failure as fatal.
SECRET_FIFO_ABSENT_EXIT = 42

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
    creates the FIFO on the container's tmpfs, reads it, exports each
    `NAME=value` into the environment, removes the FIFO, then `exec`s
    `target_argv`. The blocking read of the FIFO is also the synchronization
    point -- the wrapper waits there until the launcher delivers -- so the real
    process never starts before its secret env is set. The image must carry
    `/bin/sh`, `mkfifo`, `cat`, and `rm`.
    """
    script = (
        'mkfifo %(fifo)s || exit 1; '
        'secret_env=$(cat %(fifo)s) || exit 1; '
        'rm -f %(fifo)s; '
        'eval "$secret_env" || exit 1; '
        'exec "$@"'
    ) % {"fifo": SECRET_FIFO_TARGET}
    return SECRET_INJECT_SHELL, ["-c", script, "swarmforge-tong"] + list(target_argv)


def secret_deliver_command():
    """The in-container argv `docker exec -i` runs to deliver the secret payload.

    The launcher feeds the rendered `export` script on the exec's stdin; this
    command copies it into the wrapper's FIFO. The `-p` guard exits
    `SECRET_FIFO_ABSENT_EXIT` while the wrapper has not run `mkfifo` yet -- the
    launcher retries on that -- and matters beyond ordering: without it a too-early
    `cat > path` would create a *regular file*, landing the secret on the
    container filesystem and breaking the wrapper's `mkfifo`. Opening the FIFO
    for write blocks until the wrapper's read side is open, and closing it (exec
    stdin EOF propagates through `cat`) tells the wrapper the payload is
    complete.
    """
    script = (
        '[ -p %(fifo)s ] || exit %(absent)d; '
        'exec cat > %(fifo)s'
    ) % {"fifo": SECRET_FIFO_TARGET, "absent": SECRET_FIFO_ABSENT_EXIT}
    return [SECRET_INJECT_SHELL, "-c", script]


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
