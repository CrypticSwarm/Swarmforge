#!/usr/bin/env python3
"""The container's root-phase driver.

Merges the layered config into the harness's destination, runs that harness's
config hooks, then translates the unified agent definitions into the harness's
native format. Invoked as `HARNESS HOME`, with the source locations arriving in
the environment: `SWARMFORGE_CONFIG_{USER,ORG,REPO}_DIR` name the three config
layers, `SWARMFORGE_CONFIG_DEST` and `SWARMFORGE_CONFIG_RESET` decide the
destination and whether it is rebuilt from scratch for a harness that leaves
those to the run, `SWARMFORGE_TONG_MCP_FILE` names the generated tong MCP
fragment, and `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` name the harness-neutral
asset layers the agent definitions come from.
"""

import os
import shutil
import subprocess
import sys

from swarmforge import harness
from swarmforge.agents import translate
from swarmforge.config import merge_json, merge_toml_mcp
from swarmforge.harness.spec import Context, provided

USAGE = "usage: python3 -m swarmforge.harness.init HARNESS HOME"

# The container mounts the checkout here; a parameter below so a test can
# stage one of its own.
WORKSPACE = "/workspace"


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


def config_root(spec, home, environ):
    """The directory "{config}" stands for in a harness's asset destinations.

    The pinned destination when the harness forces one, otherwise the one the
    run named, falling back to the harness's own default config location under
    the home when the run names none.
    """
    if provided(spec.config_dest):
        return spec.config_dest
    return (environ.get("SWARMFORGE_CONFIG_DEST")
            or home + "/.config/" + spec.name)


def resolve_dest(template, home, config):
    """A destination template with its placeholders filled in."""
    return template.replace("{home}", home).replace("{config}", config)


def translate_agents(name, home, environ, workspace=WORKSPACE):
    """Translate the unified agent definitions for the harness named `name`.

    Unified definitions are markdown files whose YAML frontmatter is a superset
    of the OpenCode agent schema (description, mode, model, temperature, tools)
    plus optional per-harness override blocks (claude:, codex:, opencode:).
    They live under <dir>/agents in the harness-neutral .swarmforge asset
    layers, mounted read-only via SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR, plus
    <workspace>/.swarmforge/agents. One definition serves every harness; native
    agents/ directories inside harness config dirs are never transported by
    this asset pipeline. For OpenCode they still reach the harness through the
    layered config merge (the merged config dir is OpenCode's own discovery),
    while for Claude they are excluded from the merge as well -- Claude-native
    definitions belong to Claude's own discovery (for example
    <workspace>/.claude/agents).

    Sources are identical for every harness and applied lowest- to
    highest-precedence (later files win by name): user, org, repo asset layers,
    then the workspace overlay. Only the destination differs, and dispatch to
    the emitter that writes it is through the registered spec. A failure
    degrades to a warning: the session can run without subagents, while a
    stopped container serves nobody.
    """
    spec = harness.get(name).SPEC
    # The Waiver is the opt-out on record: unified agent definitions are not
    # delivered to this harness, so nothing is written and nothing is warned.
    if not provided(spec.agents_dest):
        return 0

    dest = resolve_dest(
        spec.agents_dest, home, config_root(spec, home, environ))
    sources = [
        # Concatenated rather than joined: an empty layer variable yields an
        # absolute "/agents" that no directory check passes, where a join
        # would name a path relative to the workspace this runs in.
        (environ.get("SWARMFORGE_ASSETS_USER_DIR") or "") + "/agents",
        (environ.get("SWARMFORGE_ASSETS_ORG_DIR") or "") + "/agents",
        (environ.get("SWARMFORGE_ASSETS_REPO_DIR") or "") + "/agents",
        workspace + "/.swarmforge/agents",
    ]

    try:
        status = translate.run(name, dest, sources, home=home)
    except Exception:
        status = 1
    if status != 0:
        print(
            "Warning: unified agent translation failed for %s; continuing"
            % name,
            file=sys.stderr,
        )
    return 0


def run(name, home, environ, workspace=WORKSPACE):
    """Run the container root phases for the harness registered as `name`."""
    status = initialize(name, home, environ)
    if status != 0:
        return status
    translate_agents(name, home, environ, workspace)
    return 0


def main(argv):
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    return run(argv[0], argv[1], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
