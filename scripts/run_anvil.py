#!/usr/bin/env python3
"""Host-side launcher that wraps an anvil (harness container) run.

The Makefile resolves the docker-run argv for an anvil (`run_opencode` /
`run_claude`) and the host paths of the four tong definition layers, then
delegates the actual launch to this script:

    run_anvil.py [--user-tongs DIR] [--org-tongs DIR] [--repo-tongs DIR]
                 [--workspace-tongs DIR] -- docker run -it --rm ... <image> ...

Tongs are sibling containers that must be orchestrated from the host (they are
started alongside the anvil, not from inside it), which is why this wrapper sits
between Make and `docker run`. It discovers tong definitions across the four
layers using the pure core in `tongs.py`, then runs the anvil.

Passthrough invariant
---------------------
With **no tong definitions discovered across all four layers**, the launcher
execs the anvil argv verbatim -- byte-identical to the direct `docker run` Make
would otherwise have issued. Existing repos ship no tong layers, so discovery is
empty and this wrapper is a transparent exec. `scripts/test_run_anvil.py`
asserts this byte-for-byte.

The anvil argv (everything after `--`) is forwarded to `os.execvp` unchanged, so
the anvil process replaces this one and keeps the controlling tty, signal
delivery, and `--rm` cleanup it had before.
"""

import importlib.util
import os
import sys

# Load the pure core (layer discovery + name-based merge) by path, the same way
# tongs.py loads translate_agents.py, so the launcher needs no package install
# and no assumptions about the current working directory.
_TONGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tongs.py")
_spec = importlib.util.spec_from_file_location("tongs", _TONGS_PATH)
tongs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tongs)


USAGE = (
    "usage: run_anvil.py [--user-tongs DIR] [--org-tongs DIR] "
    "[--repo-tongs DIR] [--workspace-tongs DIR] -- <anvil command>"
)

# Each flag names the host directory for one definition layer. The merge always
# orders layers canonically (LAYERS, lowest to highest precedence) regardless of
# the order the flags are passed.
LAYER_FLAGS = {
    "--user-tongs": tongs.USER,
    "--org-tongs": tongs.ORG,
    "--repo-tongs": tongs.REPO,
    "--workspace-tongs": tongs.WORKSPACE,
}


class UsageError(ValueError):
    """Raised for malformed launcher arguments (reported, then exit 2)."""


def parse_args(argv):
    """Split launcher options from the anvil command at the first ``--``.

    Returns ``(layer_dirs, anvil_cmd)`` where ``layer_dirs`` is the ordered
    ``(layer, path)`` list (canonical precedence, only the layers that were
    given) and ``anvil_cmd`` is the argv after ``--``. Raises ``UsageError`` if
    the separator is missing, the command is empty, or an option is malformed.
    """
    paths = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            anvil_cmd = list(argv[index + 1:])
            if not anvil_cmd:
                raise UsageError("missing anvil command after '--'")
            layer_dirs = [(layer, paths[layer]) for layer in tongs.LAYERS if layer in paths]
            return layer_dirs, anvil_cmd
        if token in LAYER_FLAGS:
            if index + 1 >= len(argv):
                raise UsageError("%s requires a directory argument" % token)
            paths[LAYER_FLAGS[token]] = argv[index + 1]
            index += 2
            continue
        raise UsageError("unexpected argument %r" % token)
    raise UsageError("missing '--' separating launcher options from the anvil command")


def discover_tongs(layer_dirs):
    """Merged tong set across the given layers ({} when none are present)."""
    return tongs.merge_tongs(tongs.discover(layer_dirs))


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


def main(argv):
    try:
        layer_dirs, anvil_cmd = parse_args(argv)
    except UsageError as exc:
        tongs.warn(str(exc))
        tongs.warn(USAGE)
        return 2

    merged = discover_tongs(layer_dirs)
    if merged:
        # The launcher discovers tongs but does not start them; the anvil runs
        # without them. The passthrough invariant only governs the empty case,
        # so surface the discovered tongs rather than ignoring them silently.
        tongs.warn(
            "%d tong definition(s) discovered (%s); this launcher does not "
            "start tongs, so the anvil runs without them"
            % (len(merged), ", ".join(sorted(merged)))
        )

    # On success exec_anvil replaces this process; it only returns a status if
    # the anvil command could not be execed.
    return exec_anvil(anvil_cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
