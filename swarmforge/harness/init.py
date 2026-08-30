#!/usr/bin/env python3
"""The container's root-phase driver.

Merges the layered config into the harness's destination and runs that
harness's config hooks, translates the unified agent definitions into the
harness's native format, installs the portable skills and commands into its
native asset locations, links the state the harness keeps across runs into its
config destination, then runs whatever container preparation the harness needs
root for. Invoked as `HARNESS HOME`, with the source
locations arriving in the environment: `SWARMFORGE_CONFIG_{USER,ORG,REPO}_DIR`
name the three config layers, `SWARMFORGE_CONFIG_DEST` and
`SWARMFORGE_CONFIG_RESET` decide the destination and whether it is rebuilt from
scratch for a harness that leaves those to the run, `SWARMFORGE_TONG_MCP_FILE`
names the generated tong MCP fragment, `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR`
name the harness-neutral asset layers the agent definitions come from, and
`SWARMFORGE_DOTAGENTS_{USER,ORG}_DIR` plus `SWARMFORGE_SKILLS_DIR` and
`SWARMFORGE_COMMAND_DIR` name the portable skill and command layers.
"""

import os
import shutil
import subprocess
import sys

from swarmforge import harness
from swarmforge.agents import translate
from swarmforge.config import merge_json, merge_toml_mcp
from swarmforge.harness.spec import AssetLayer, Context, provided

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


def asset_context(spec, home, environ, cwd=""):
    """What one asset-phase run knows, for the harness's install hooks.

    The config layer sources and the tong fragment are the same strings the
    config phase read. `config_dest` differs: the config phase leaves it empty
    when the run names no destination and skips itself, while for the asset
    phase "{config}" always stands for a concrete directory -- the pinned one,
    the one the run named, or the harness's default under the home -- because
    that is where the harness reads its assets from either way. `cwd` is filled
    in only for the phases that act on the directory the harness process starts
    in, and empty for the rest.
    """
    return Context(
        harness=spec.name,
        home=home,
        config_dest=config_root(spec, home, environ),
        config_repo_src=environ.get("SWARMFORGE_CONFIG_REPO_DIR") or "",
        config_user_src=environ.get("SWARMFORGE_CONFIG_USER_DIR") or "",
        config_org_src=environ.get("SWARMFORGE_CONFIG_ORG_DIR") or "",
        tong_mcp_file=environ.get("SWARMFORGE_TONG_MCP_FILE") or "",
        cwd=cwd,
    )


def install_assets(name, home, environ, workspace=WORKSPACE):
    """Install the portable skills and commands for the harness named `name`.

    Skills and commands are portable across harnesses, so copying them into
    the harness's native locations is the whole translation. The config merge
    excludes both from every layer, which makes this their only transport.

    Sources are identical for every harness and applied lowest- to
    highest-precedence:

      1. The portable .agents layers, user then org, mounted via
         SWARMFORGE_DOTAGENTS_USER_DIR and SWARMFORGE_DOTAGENTS_ORG_DIR. They
         follow the harness-neutral .agents/{skills,commands} convention, so
         the source directory names are the same for every harness.
      2. The shared Swarmforge assets, the repo's own skills/ and commands/,
         mounted via SWARMFORGE_SKILLS_DIR and SWARMFORGE_COMMAND_DIR.
      3. The workspace overlay, <workspace>/.agents/{skills,commands}.

    Harness-native config dirs inside a layer (such as <layer>/.claude) are
    never consumed for skills or commands; those formats are portable and live
    under the .agents convention instead. Claude's destinations resolve into
    the container-local config dir, so each run starts empty and a repo's
    assets never leak into the next repo's session.

    A failed install is not caught: the container stops rather than starting a
    session whose assets are half-written.
    """
    spec = harness.get(name).SPEC
    config = config_root(spec, home, environ)
    skills_dest = (resolve_dest(spec.skills_dest, home, config)
                   if provided(spec.skills_dest) else "")
    commands_dest = (resolve_dest(spec.commands_dest, home, config)
                     if provided(spec.commands_dest) else "")

    def dotagents(variable):
        # Concatenated rather than joined: an empty layer variable yields an
        # absolute "/skills" that no directory check passes, where a join
        # would name a path relative to the workspace this runs in.
        root = environ.get(variable) or ""
        return AssetLayer(
            skills_src=root + "/skills",
            commands_src=root + "/commands",
            skills_dest=skills_dest,
            commands_dest=commands_dest,
        )

    layers = [
        dotagents("SWARMFORGE_DOTAGENTS_USER_DIR"),
        dotagents("SWARMFORGE_DOTAGENTS_ORG_DIR"),
        AssetLayer(
            skills_src=environ.get("SWARMFORGE_SKILLS_DIR") or "",
            commands_src=environ.get("SWARMFORGE_COMMAND_DIR") or "",
            skills_dest=skills_dest,
            commands_dest=commands_dest,
        ),
        AssetLayer(
            skills_src=workspace + "/.agents/skills",
            commands_src=workspace + "/.agents/commands",
            skills_dest=skills_dest,
            commands_dest=commands_dest,
        ),
    ]

    ctx = asset_context(spec, home, environ)
    for layer in layers:
        spec.install_assets(ctx, layer)
    return 0


def link_state(name, home, environ):
    """Link the persistent state of the harness named `name` into its config.

    A harness whose config destination is rebuilt for every run keeps what has
    to outlive it in the persistent home and links those entries back in; one
    whose config already lives in that home has nothing to link and declares no
    hook. Linking runs after the config merge that rebuilds the destination,
    so a link is not among what the merge wipes.

    A failed link is not caught: the state it stands for would silently die
    with the container.
    """
    spec = harness.get(name).SPEC
    spec.link_state(asset_context(spec, home, environ))
    return 0


def root_setup(name, home, environ, cwd=None):
    """Prepare the container for the harness named `name`, as root.

    The last phase, so a hook acts on a container whose config, assets, and
    state links are already in place, and the last moment root can act at all:
    what follows it is the privilege drop and the exec. The hook is handed the
    directory the harness process starts in, which is the one this driver was
    started in unless the caller names another.

    A failed preparation is not caught: the session would start without
    whatever the hook stands for and only root can supply.
    """
    spec = harness.get(name).SPEC
    ctx = asset_context(spec, home, environ, cwd=cwd or os.getcwd())
    spec.root_setup(ctx)
    return 0


def run(name, home, environ, workspace=WORKSPACE, cwd=None):
    """Run the container root phases for the harness registered as `name`."""
    status = initialize(name, home, environ)
    if status != 0:
        return status
    translate_agents(name, home, environ, workspace)
    install_assets(name, home, environ, workspace)
    link_state(name, home, environ)
    root_setup(name, home, environ, cwd)
    return 0


def main(argv):
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    return run(argv[0], argv[1], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
