#!/usr/bin/env python3
"""Fixtures shared by more than one swarmforge.harness test module."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge.harness.spec import HarnessSpec, Waiver


def write_file(path, text, mode=None):
    """Write `text` at `path`, creating the parent directories."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def read_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tree(root):
    """Every path under `root`, relative, mapped to what stands there.

    A file maps to its text, a symlink to `("link", target)` with the target
    string it was created with, and a directory to None. A missing root is an
    empty mapping, so a destination that was never created and one that was
    created empty read differently.
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            key = os.path.relpath(path, root)
            if os.path.islink(path):
                found[key] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                found[key] = None
            else:
                found[key] = read_file(path)
    return found


def fake_spec(**overrides):
    """A registrable spec that declares nothing but the hooks under test."""
    fields = dict(
        name="fake",
        config_dest=Waiver("the run's SWARMFORGE_CONFIG_DEST names the destination"),
        config_reset=False,
        layer_excludes=(),
        skills_dest=Waiver("no portable skills destination is declared"),
        commands_dest=Waiver("no portable commands destination is declared"),
        agents_dest=Waiver("unified agent definitions are not delivered"),
        mcp_fragment=lambda servers: {},
        mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
        mcp_merge=Waiver("nothing merges the fragment into a config file"),
        agent_emitter=Waiver("no emitter is defined"),
        extra_chown_paths=(),
    )
    fields.update(overrides)
    return HarnessSpec(**fields)
