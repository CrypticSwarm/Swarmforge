"""Vocabulary of a tong definition, and the resolution of its declared values.

The leaf of the package: it imports nothing else from `swarmforge.tongs`, so
every other module can depend on it. It holds the constant sets the launcher
dispatches on (layers, lifecycles, interface kinds, readiness modes), the docker
labels it stamps, and the pure resolution of a definition's `readiness:` block.
`warn` lives here too, so the one `tongs: ` stderr prefix has a single home.
"""

import re
import sys


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


# `True` is an `int` in Python, so a bare isinstance check would accept `port:
# true`. Every integer test in the package goes through here instead.
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


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
