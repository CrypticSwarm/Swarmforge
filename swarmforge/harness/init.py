#!/usr/bin/env python3
"""The container's root-phase driver.

Merges the layered config into the harness's destination and runs that
harness's config hooks, translates the unified agent definitions into the
harness's native format, installs the portable skills and commands into its
native asset locations, links the state the harness keeps across runs into its
config destination, runs whatever container preparation the harness needs root
for, then hands the home, the paths that harness built outside it, and the
workspace to the anvil uid. Invoked as `HARNESS HOME UID GID`, with the source
locations arriving in the environment: `SWARMFORGE_CONFIG_{USER,ORG,REPO}_DIR`
name the three config layers, `SWARMFORGE_CONFIG_DEST` and
`SWARMFORGE_CONFIG_RESET` decide the destination and whether it is rebuilt from
scratch for a harness that leaves those to the run, `SWARMFORGE_TONG_MCP_FILE`
names the generated tong MCP fragment, `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR`
name the harness-neutral asset layers the agent definitions come from, and
`SWARMFORGE_DOTAGENTS_{USER,ORG}_DIR` plus `SWARMFORGE_SKILLS_DIR` and
`SWARMFORGE_COMMAND_DIR` name the portable skill and command layers.
"""

import dataclasses
import os
import shutil
import subprocess
import sys

from swarmforge import harness
from swarmforge.agents import translate
from swarmforge.config import merge_json, merge_toml_mcp
from swarmforge.harness.spec import AssetLayer, Context, provided

USAGE = "usage: python3 -m swarmforge.harness.init HARNESS HOME UID GID"

# The container mounts the checkout here.
WORKSPACE = "/workspace"

# The config file the layer merge keys for every harness instead of overlaying
# it whole, so a higher layer replaces only the keys it names.
KEYED_FILE = "opencode.json"

# Layer variables are concatenated onto, never joined with, their subdirectory:
# an empty layer yields an absolute "/skills" that no source check passes,
# where a join would name a path relative to the workspace this runs in.


def layer_exclude_args(spec):
    """The tar `--exclude` arguments for one harness's config layer merge."""
    return [
        # The keyed file is excluded from the overlay because it merges
        # key-by-key instead of being copied whole.
        "--exclude=./" + KEYED_FILE,
        # .swarmforge/ asset dirs are read via their own mounts, never through
        # the config merge, so transporting them here would only litter the
        # dest (or, for Claude, accumulate junk in the persistent home).
        "--exclude=./.swarmforge",
        # Everything else the harness itself keeps out of the overlay.
        *["--exclude=" + entry for entry in spec.layer_excludes],
    ]


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
        merge_config_file(src + "/" + KEYED_FILE, dest + "/" + KEYED_FILE)

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


def translate_agents(spec, ctx, environ, workspace=WORKSPACE):
    """Translate the unified agent definitions for the harness `spec` declares.

    One definition serves every harness, in the format under "Agents" in the
    README. Sources are the .swarmforge asset layers and the workspace
    overlay, lowest- to highest-precedence, later files winning by name. Only
    the destination differs, and the registered spec names the emitter.

    A native agents/ directory inside a harness config dir is never carried
    here: those belong to the harness's own discovery. A failure degrades to a
    warning, since a session can run without subagents while a stopped
    container serves nobody.
    """
    # The Waiver is the opt-out on record: unified agent definitions are not
    # delivered to this harness, so nothing is written and nothing is warned.
    if not provided(spec.agents_dest):
        return 0

    dest = resolve_dest(spec.agents_dest, ctx.home, ctx.config_dest)
    sources = [
        (environ.get("SWARMFORGE_ASSETS_USER_DIR") or "") + "/agents",
        (environ.get("SWARMFORGE_ASSETS_ORG_DIR") or "") + "/agents",
        (environ.get("SWARMFORGE_ASSETS_REPO_DIR") or "") + "/agents",
        workspace + "/.swarmforge/agents",
    ]

    try:
        status = translate.run(spec.name, dest, sources, home=ctx.home)
    except Exception:
        status = 1
    if status != 0:
        print(
            "Warning: unified agent translation failed for %s; continuing"
            % spec.name,
            file=sys.stderr,
        )
    return 0


def asset_context(spec, home, environ, cwd=""):
    """What the phases after the config merge know, for the harness's hooks.

    The config layer sources and the tong fragment are the same strings the
    config phase read. `config_dest` differs: the config phase leaves it empty
    when the run names no destination and skips itself, while for the phases
    after it "{config}" always stands for a concrete directory -- the pinned one,
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


def install_assets(spec, ctx, environ, workspace=WORKSPACE):
    """Install the portable skills and commands the harness `spec` declares.

    Skills and commands are portable across harnesses, so copying them into
    the harness's native locations is the whole translation, and the config
    merge excludes both from every layer to make this their only transport.

    The layers and their order are the ones under "Shared assets" in the
    README; only the destination is per-harness. A failed install is not
    caught: the container stops rather than starting a session whose assets
    are half-written.
    """
    skills_dest = (resolve_dest(spec.skills_dest, ctx.home, ctx.config_dest)
                   if provided(spec.skills_dest) else "")
    commands_dest = (resolve_dest(spec.commands_dest, ctx.home, ctx.config_dest)
                     if provided(spec.commands_dest) else "")

    def dotagents(variable):
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

    for layer in layers:
        spec.install_assets(ctx, layer)
    return 0


def link_state(spec, ctx):
    """Link the persistent state the harness `spec` declares into its config.

    A harness whose config destination is rebuilt for every run keeps what has
    to outlive it in the persistent home and links those entries back in; one
    whose config already lives in that home has nothing to link and declares no
    hook. Linking runs after the config merge that rebuilds the destination,
    so a link is not among what the merge wipes.

    A failed link is not caught: the state it stands for would silently die
    with the container.
    """
    spec.link_state(ctx)
    return 0


def root_setup(spec, ctx, cwd=None):
    """Prepare the container for the harness `spec` declares, as root.

    Runs on a container whose config, assets, and state links are already in
    place, just before ownership changes hands -- so whatever the hook creates
    for root's own keeping stays root's. The hook is handed the directory the
    harness process starts in, which is the one this driver was started in
    unless the caller names another.

    A failed preparation is not caught: the session would start without
    whatever the hook stands for and only root can supply.
    """
    spec.root_setup(dataclasses.replace(ctx, cwd=cwd or os.getcwd()))
    return 0


def _chown(argv):
    """Run one chown, letting it fail.

    A path that is not there, a file whose owner cannot be changed, or a
    system with no chown binary to run is skipped silently: ownership is
    handed over best-effort, and the session surfaces the error itself if
    something it needs is out of reach.
    """
    try:
        subprocess.run(argv, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass


def deliver_ownership(spec, ctx, uid, gid, workspace=WORKSPACE, chown=None):
    """Hand what root built to the anvil uid, for the harness `spec` declares.

    The last phase, run after every phase that writes as root, so nothing root
    creates afterwards is left behind owned by root once privileges drop.

    The home changes hands first, then the paths that harness builds outside
    it, then the workspace. The extras change hands with `-Rh`, which changes
    the links themselves rather than what they point at: they hold the state
    links back into the home, whose targets the home pass already covered, so
    following them would be wasted work at best.
    """
    owner = "%s:%s" % (uid, gid)
    run_chown = chown or _chown
    run_chown(["chown", "-R", owner, ctx.home])
    for path in spec.extra_chown_paths:
        run_chown(["chown", "-Rh", owner, path])
    run_chown(["chown", "-R", owner, workspace])
    return 0


def run(name, home, uid, gid, environ, workspace=WORKSPACE, cwd=None):
    """Run the container root phases for the harness registered as `name`.

    The config phase is the one that looks the name up, and it fails the run
    for a name the registry does not hold -- so the lookup below is of a name
    already known to be registered, and the phases after it are handed the
    spec and the context they all read rather than resolving either again.
    """
    status = initialize(name, home, environ)
    if status != 0:
        return status

    spec = harness.get(name).SPEC
    ctx = asset_context(spec, home, environ)

    translate_agents(spec, ctx, environ, workspace)
    install_assets(spec, ctx, environ, workspace)
    link_state(spec, ctx)
    root_setup(spec, ctx, cwd)
    deliver_ownership(spec, ctx, uid, gid, workspace)
    return 0


def main(argv):
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 2
    return run(argv[0], argv[1], argv[2], argv[3], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
