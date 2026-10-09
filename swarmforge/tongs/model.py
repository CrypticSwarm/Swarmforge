"""Vocabulary of a tong definition, and the resolution of its declared values.

The leaf of the package: it imports nothing else from `swarmforge.tongs`, so
every other module can depend on it. It holds the constant sets the launcher
dispatches on (layers, lifecycles, interface kinds, readiness modes), the docker
labels it stamps, and the pure resolution of a definition's `readiness:` block
and `resources.gpus` request.
`warn` lives here too, so the one `tongs: ` stderr prefix has a single home.
"""

import re
import sys


# Lowest to highest precedence; the workspace (the checkout you launched in) is untrusted.
USER, ORG, REPO, WORKSPACE = "user", "org", "repo", "workspace"
LAYERS = (USER, ORG, REPO, WORKSPACE)
TRUSTED_LAYERS = frozenset({USER, ORG, REPO})

LIFECYCLES = frozenset({"session", "shared"})
INTERFACE_KINDS = frozenset({"mcp", "port", "volume", "none"})
READINESS_MODES = frozenset({"tcp", "healthcheck", "none"})
TRANSPORTS = frozenset({"http"})  # http only in v1 (stdio defeats the purpose)

LABEL_TONG_NAME = "swarmforge.tong.name"
LABEL_CONFIG_HASH = "swarmforge.tong.config-hash"

ENV_PREFIX = "SWARMFORGE_TONG"

# The mount word granting docker control; here so every module spells it one way.
SOCKET_MOUNT = "docker-socket"

# A container cannot re-share a bind mount, so a broker needs the workspace's host path.
WORKSPACE_HOST_ENV = "SWARMFORGE_WORKSPACE_HOST_PATH"


def warn(message):
    print("tongs: %s" % message, file=sys.stderr)


# `True` is an `int`, so a bare isinstance check would accept `port: true`.
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


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
        raise ValueError("duration must be positive, got %r" % (value,))
    return seconds


# Of docker's --gpus keys only `device`: driver/capabilities/options reach beyond GPUs.
_GPU_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
_GPU_COUNT_RE = re.compile(r"[0-9]+")
# Docker reads a count with Go's Atoi; a 32-bit int fits on every platform it builds for.
GPU_COUNT_MAX = 2**31 - 1
_GPU_DEVICE_RE = re.compile(r'device=%s|"device=%s(?:,%s)*"' % (_GPU_ID, _GPU_ID, _GPU_ID))
_GPU_UNQUOTED_LIST_RE = re.compile(r"device=%s(?:,%s)+" % (_GPU_ID, _GPU_ID))
_GPU_FORMS = (
    "'all', a positive GPU count, 'device=<id>', or '\"device=<id>,<id>,...\"'"
)


def parse_gpus(value):
    """Resolve a `resources.gpus` declaration to its `docker run --gpus` value.

    `None` (no GPUs requested) yields None. Accepted, and returned verbatim as
    a string: `all`; a count from 1 to `GPU_COUNT_MAX`, as an int or a string
    of ASCII digits; or a device selector, `device=<id>` or the docker-quoted
    list `"device=<id>,<id>,..."` (literal double quotes, since docker reads
    the value as CSV), each id starting with a letter or digit followed by
    letters, digits, and `._:-`. Raises `ValueError`
    for anything else -- including docker's other `--gpus` keys, which can
    reach devices beyond GPUs -- so validation and argv assembly share one
    definition of a usable request.
    """
    if value is None:
        return None
    if _is_int(value) and 0 < value <= GPU_COUNT_MAX:
        return str(value)
    if isinstance(value, str):
        if value == "all" or _GPU_DEVICE_RE.fullmatch(value):
            return value
        if _GPU_COUNT_RE.fullmatch(value) and 0 < int(value) <= GPU_COUNT_MAX:
            return value
        if _GPU_UNQUOTED_LIST_RE.fullmatch(value):
            raise ValueError(
                "a device list needs literal double quotes because docker reads "
                "--gpus as CSV; write gpus: '\"%s\"', got %r" % (value, value)
            )
    raise ValueError("must be %s, got %r" % (_GPU_FORMS, value))


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
