#!/usr/bin/env python3
"""Fixtures shared by more than one swarmforge.anvil test module."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs


# A docker invocation shaped like the one run_agent_container builds: the
# interactive/remove flags, name, network, injected env/mounts, image, and the
# harness args. The launcher must forward this verbatim when no tongs exist.
ANVIL_ARGV = [
    "docker", "run", "-it", "--rm", "--name", "claude-myproject",
    "--network", "opencode-net",
    "-e", "SWARMFORGE_UID=1000",
    "-e", "TZ=Etc/UTC",
    "-v", "/home/me/proj:/workspace",
    # A path with a space exercises that a single argv word is forwarded whole,
    # never re-split, through the real execvp.
    "-v", "/home/me/my proj:/repos/me/my proj",
    "claude-code:local",
    "--some-harness-arg",
]


def _merged(name, defn, source=tongs.WORKSPACE):
    """A one-entry merged set as merge_tongs would return it."""
    return {name: {"source": source, "definition": defn}}
