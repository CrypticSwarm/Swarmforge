#!/usr/bin/env python3
"""The container's root-phase config driver.

Merges the layered config into the harness's destination and runs that
harness's config hooks. Invoked as `HARNESS HOME`, with the layer locations
arriving in the environment: `SWARMFORGE_CONFIG_{USER,ORG,REPO}_DIR` name the
three layers, `SWARMFORGE_CONFIG_DEST` and `SWARMFORGE_CONFIG_RESET` decide
the destination and whether it is rebuilt from scratch for a harness that
leaves those to the run, and `SWARMFORGE_TONG_MCP_FILE` names the generated
tong MCP fragment.
"""

import os
import shutil
import subprocess
import sys

from swarmforge import harness
from swarmforge.config import merge_json, merge_toml_mcp
from swarmforge.harness.spec import Context, provided

USAGE = "usage: python3 -m swarmforge.harness.init HARNESS HOME"


def layer_exclude_args(spec):
    """The tar `--exclude` arguments for one harness's config layer merge."""
    return (
        # Keyed files are excluded from the overlay because they merge
        # key-by-key instead of being copied whole.
        ["--exclude=./" + name for name in spec.keyed_files]
        # .swarmforge/ asset dirs are read via their own mounts, never through
        # the config merge, so transporting them here would only litter the
        # dest (or, for Claude, accumulate junk in the persistent home).
        + ["--exclude=./.swarmforge"]
        # Everything else the harness itself keeps out of the overlay.
        + ["--exclude=" + entry for entry in spec.layer_excludes]
    )


def merge_config_layer(src_dir, dst_dir, exclude_args):
    """Overlay every included entry of `src_dir` onto `dst_dir`."""
    if not src_dir or not os.path.isdir(src_dir):
        return

    # Skip when src and dst resolve to the same underlying directory (for
    # example when a home-dir layer makes both paths bind-mounts of the host's
    # own config dir). Otherwise tar would try to extract entries on top of
    # themselves and abort.
    try:
        src_stat = os.stat(src_dir)
        dst_stat = os.stat(dst_dir)
    except OSError:
        pass
    else:
        if (src_stat.st_dev, src_stat.st_ino) == (dst_stat.st_dev, dst_stat.st_ino):
            return

    # A tar stream rather than a copy, to avoid bind-mount same-file errors.
    creator = subprocess.Popen(
        ["tar", *exclude_args, "-cf", "-", "."],
        cwd=src_dir,
        stdout=subprocess.PIPE,
    )
    try:
        extractor = subprocess.Popen(
            ["tar", "-xf", "-"],
            cwd=dst_dir,
            stdin=creator.stdout,
        )
    except BaseException:
        # No reader will ever drain the pipe; closing it stops the creator,
        # which is then reaped rather than left behind.
        creator.stdout.close()
        creator.wait()
        raise
    # The extractor now holds the read end; a copy left open here would keep
    # it waiting on a pipe that never reaches end of file.
    creator.stdout.close()

    status = extractor.wait()
    # Only the extractor decides the outcome: a POSIX shell pipeline's status
    # is its last command's, and this keeps that contract.
    creator.wait()
    if status != 0:
        raise subprocess.CalledProcessError(status, ["tar", "-xf", "-"])


def merge_config_file(src_file, dst_file, replace_mcp_entries=False):
    """Merge `src_file` over `dst_file` key-by-key, or copy it in whole."""
    if not src_file or not os.path.isfile(src_file):
        return

    if not os.path.isfile(dst_file):
        shutil.copy(src_file, dst_file)
        return

    merge_json.merge_files(
        dst_file, src_file, replace_mcp_entries=replace_mcp_entries)


def initialize(name, home, environ):
    """Run the config phase for the harness registered as `name`."""
    module = harness.get(name)
    if module is None:
        print("unknown harness: %s" % name, file=sys.stderr)
        return 2
    spec = module.SPEC

    # Not the run's to choose when the harness pins a destination: a merged
    # layer landing in the shared home would outlive the container. Otherwise
    # the run's variable decides, and an empty one skips the phase.
    if provided(spec.config_dest):
        dest = spec.config_dest
    else:
        dest = environ.get("SWARMFORGE_CONFIG_DEST") or ""
    reset = spec.config_reset or (environ.get("SWARMFORGE_CONFIG_RESET") or "0") == "1"

    if not dest:
        return 0

    ctx = Context(
        harness=spec.name,
        home=home,
        config_dest=dest,
        config_repo_src=environ.get("SWARMFORGE_CONFIG_REPO_DIR") or "",
        config_user_src=environ.get("SWARMFORGE_CONFIG_USER_DIR") or "",
        config_org_src=environ.get("SWARMFORGE_CONFIG_ORG_DIR") or "",
        tong_mcp_file=environ.get("SWARMFORGE_TONG_MCP_FILE") or "",
    )

    if reset:
        if os.path.islink(dest) or os.path.isfile(dest):
            os.remove(dest)
        elif os.path.isdir(dest):
            shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    # Merge order (lowest to highest precedence): repo -> user -> org.
    #
    # Ordered by trust, not by specificity, because these files carry
    # permissions, hooks, and env: a checkout is whatever repo you cloned, and
    # the org layer is installed deliberately. That inverts the order the asset
    # pipelines use for skills, commands, and agents, where a repo's own
    # definitions are the most specific thing available and rightly win.
    excludes = layer_exclude_args(spec)
    for src in (ctx.config_repo_src, ctx.config_user_src, ctx.config_org_src):
        merge_config_layer(src, dest, excludes)
        for keyed in spec.keyed_files:
            # Concatenated rather than joined: an empty layer yields an
            # absolute "/<name>" that fails the source check, where a join
            # would name a file relative to the workspace this runs in.
            merge_config_file(src + "/" + keyed, dest + "/" + keyed)

    spec.build_config(ctx)

    # Sidecar MCP servers merge last but yield to same-named layer entries.
    if spec.mcp_merge == "toml-managed-block":
        # Servers go in a managed block the module rewrites each run rather
        # than being appended; running with no fragment is what removes a
        # stale block when no tongs are set.
        merge_toml_mcp.merge(dest + "/config.toml", ctx.tong_mcp_file or None)
    elif spec.mcp_merge == "json-replace-mcp":
        # A no-op without the variable: merge_config_file ignores an empty or
        # missing source.
        merge_config_file(
            ctx.tong_mcp_file, dest + "/opencode.json", replace_mcp_entries=True)

    spec.finalize_config(ctx)
    spec.publish_config(ctx)
    return 0


def main(argv):
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    return initialize(argv[0], argv[1], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
