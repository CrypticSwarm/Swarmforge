#!/usr/bin/env python3
"""Fixtures shared by more than one swarmforge.harness test module."""

import contextlib
import dataclasses
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here means an importer that reached this module without it still
# resolves the package these fixtures read.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import harness
from swarmforge.harness import claude
from swarmforge.harness.spec import HarnessSpec, Waiver, provided


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


# Where `redirected` puts each path a harness pins, named relative to the
# staging tree: the merged config destination, the directory the anvil uid is
# handed its trees under, and the three paths claude names as module constants.
STAGED = {
    "config_dest": "dest",
    "handover": "handover",
    "settings_file": "claude-settings.json",
    "image_defaults": "image-defaults.json",
    "wrapper_dir": "wrapper",
}


def staged(tmp, name):
    """The path under the staging tree `tmp` that `redirected` moves `name` to.

    A name with no entry in STAGED -- a spec field naming a destination
    outright -- is staged under the field's own name.
    """
    return os.path.join(tmp, STAGED.get(name, name))


@contextlib.contextmanager
def redirected(name, tmp):
    """Every path `name` pins, replaced by one under the staging tree `tmp`.

    The replacements are read off the spec rather than listed per harness, so
    a newly registered harness is staged by the same rules as the four that
    exist: the pinned config destination and every destination named outright
    move to the slot `staged` gives them, and a path handed to the anvil uid
    keeps its shape under the handover slot.
    """
    module = harness.get(name)
    if module is None:
        raise AssertionError("no harness registered as %s" % name)
    spec = module.SPEC
    handover = staged(tmp, "handover")

    def inside(path):
        return path == tmp or path.startswith(tmp + os.sep)

    replacements = {}
    if provided(spec.config_dest):
        replacements["config_dest"] = staged(tmp, "config_dest")
    # A destination that names a directory outright rather than through a
    # placeholder is one the config redirection above cannot reach.
    for field in ("skills_dest", "commands_dest", "agents_dest"):
        template = getattr(spec, field)
        if (provided(template)
                and "{" not in template
                and not inside(template)):
            replacements[field] = staged(tmp, field)
    extras = tuple(
        path if inside(path) else os.path.join(handover, path.lstrip("/"))
        for path in spec.extra_chown_paths
    )
    if extras != spec.extra_chown_paths:
        replacements["extra_chown_paths"] = extras

    with contextlib.ExitStack() as stack:
        if replacements:
            stack.enter_context(mock.patch.object(
                module, "SPEC", dataclasses.replace(spec, **replacements)))
        # Claude names its built settings file, the image's defaults, and the
        # git wrapper directory as module constants rather than spec fields,
        # so the generic replacement above cannot reach them; all three point
        # into paths a host running Swarmforge itself really has.
        stack.enter_context(mock.patch.object(
            claude, "SETTINGS_FILE", staged(tmp, "settings_file")))
        stack.enter_context(mock.patch.object(
            claude, "IMAGE_DEFAULT_SETTINGS", staged(tmp, "image_defaults")))
        stack.enter_context(mock.patch.object(
            claude, "WRAPPER_DIR", staged(tmp, "wrapper_dir")))
        yield
