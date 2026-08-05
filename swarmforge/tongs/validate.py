"""Schema validation for one tong definition.

Permissive about unknown keys (forward compatibility) and strict about the
fields the launcher dispatches on, so a malformed definition becomes a list of
errors here rather than a docker failure mid-launch. Every check that would
otherwise restate launcher logic -- the mount grammar and its targets, secret
env names, readiness durations -- calls the same function the launcher calls, so
validation and assembly can never disagree.
"""

import re

from .model import (
    DEFAULT_READINESS_TIMEOUT_S,
    INTERFACE_KINDS,
    LIFECYCLES,
    READINESS_MODES,
    TRANSPORTS,
    _is_int,
    parse_duration,
)
from .mounts import (
    mount_destination,
    mount_target_error,
    overlapping_mount_error,
    parse_mount,
    reserved_mount_targets,
)
from .secrets import ENV_NAME_RE, partition_secret_env


# Dot-separated labels of letters, digits and inner hyphens -- what docker's
# embedded DNS resolves a `--network-alias` as. Anchored per label so a leading
# or trailing hyphen (or an empty label from a doubled dot) is rejected here
# rather than by `docker run` mid-launch.
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
DNS_NAME_MAX_LEN = 253


def _is_dns_name(value):
    """True if `value` is a hostname docker will accept as a network alias."""
    if not isinstance(value, str) or not value or len(value) > DNS_NAME_MAX_LEN:
        return False
    return all(_DNS_LABEL_RE.match(label) for label in value.split("."))


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

        # Extra DNS names the tong answers to, beyond its canonical alias, for
        # consumers that hardcode a hostname (vhosts, certificate CNs). They
        # become `--network-alias` flags, so only a tong with a listener can
        # carry them and each must be a name docker's embedded DNS accepts.
        aliases = interface.get("aliases")
        if aliases is not None:
            if kind in ("volume", "none"):
                err("interface.aliases needs a network-facing tong; "
                    "interface.kind=%s has no listener" % kind)
            if not isinstance(aliases, list):
                err("interface.aliases must be a list of DNS names")
            else:
                for alias in aliases:
                    if not _is_dns_name(alias):
                        err("invalid interface.aliases entry %r (must be a DNS name: "
                            "letters, digits, hyphens and dots)" % (alias,))

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
        reserved = reserved_mount_targets(defn)
        placed = []                      # (mount, destination) already accounted for
        for mount in mounts:
            if not isinstance(mount, str):
                err("mount entries must be strings, got %r" % (mount,))
                continue
            try:
                word, target, _ = parse_mount(mount)
                destination = mount_destination(word, target)
            except ValueError as exc:
                err(str(exc))
                continue
            reason = mount_target_error(mount, word, target, destination, reserved)
            if reason is None:
                reason = overlapping_mount_error(mount, destination, placed)
            if reason:
                err(reason)
                continue
            placed.append((mount, destination))

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
