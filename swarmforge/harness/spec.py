"""The contract a harness module declares to the rest of Swarmforge."""

import dataclasses
import os
import shutil


@dataclasses.dataclass(frozen=True)
class Waiver:
    """An explicit opt-out of a contract field.

    A field holding a Waiver is declared unimplemented, with the reason on
    record -- distinct from a field nobody filled in, which the mandatory
    constructor arguments make impossible to express.
    """

    reason: str


def provided(value):
    """True when a contract value is a real declaration rather than an opt-out."""
    return value is not None and not isinstance(value, Waiver)


@dataclasses.dataclass(frozen=True)
class Context:
    """What one driver phase knows.

    Every field is a string, empty when the run has nothing for it, so a
    harness hook reads an absent layer the same way whichever way it went
    missing.
    """

    # The name the harness is registered and selected by.
    harness: str

    # The anvil user's home inside the container, such as /home/anvil.
    home: str

    # Directory the layered config is merged into for this run.
    config_dest: str

    # The three config layer source dirs, lowest precedence first.
    config_repo_src: str
    config_user_src: str
    config_org_src: str

    # The generated tong MCP fragment, empty when the run has no tongs.
    tong_mcp_file: str

    # The working directory the harness process will start in; empty for
    # phases that do not act on it.
    cwd: str = ""


@dataclasses.dataclass(frozen=True)
class AssetLayer:
    """One source layer of portable assets, with the resolved destinations.

    Sources may be empty or missing -- a layer contributes only what it has.
    Destinations are empty strings when the harness waives them.
    """

    skills_src: str
    commands_src: str
    skills_dest: str
    commands_dest: str


def copy_dir_entries(src_dir, dst_dir):
    """Copy every top-level entry of `src_dir` into `dst_dir`.

    Each entry replaces whatever stands at the destination under that name --
    a file, a directory, or a stale symlink, dangling or not. The replacement
    is wholesale rather than a deep merge, so a package a lower layer left
    behind cannot contribute stray files to a higher layer's copy of the same
    name, and per-entry symlinks left by earlier runs get cleaned up.

    Symlinks are recreated pointing at the same target rather than followed,
    directories are copied whole with their own symlinks intact, and files keep
    their permissions and times. Top-level entries whose name starts with a dot
    are skipped: the portable asset contract is a directory of named skill and
    command entries, and dotfiles beside them are the editor and VCS droppings
    of whoever authored the layer. Hidden files inside a copied entry travel
    with it. An empty `dst_dir` is a destination the harness waived, and
    nothing is copied or created for it.
    """
    if not src_dir or not os.path.isdir(src_dir):
        return
    if not dst_dir:
        return

    os.makedirs(dst_dir, exist_ok=True)

    for name in sorted(os.listdir(src_dir)):
        if name.startswith("."):
            continue
        entry = os.path.join(src_dir, name)
        target = os.path.join(dst_dir, name)

        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        elif os.path.lexists(target):
            os.remove(target)

        if os.path.islink(entry):
            os.symlink(os.readlink(entry), target)
        elif os.path.isdir(entry):
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)


def install_assets(ctx, layer):
    """Default install-assets hook: copy the layer's skills, then commands."""
    copy_dir_entries(layer.skills_src, layer.skills_dest)
    copy_dir_entries(layer.commands_src, layer.commands_dest)


def finalize_agents(dest_dir, emitted, home=""):
    """Default finalize-agents hook: nothing follows the emitted files."""


def build_config(ctx):
    """Default build-config hook: the layer merge is the whole build."""


def finalize_config(ctx):
    """Default finalize-config hook: nothing follows the MCP merge."""


def publish_config(ctx):
    """Default publish-config hook: the merged destination is the delivery."""


def link_state(ctx):
    """Default link-state hook: no state is linked into the config destination."""


def root_setup(ctx):
    """Default root-setup hook: nothing needs root preparation before privileges drop."""


def pre_exec(ctx, argv, env):
    """Default pre-exec hook: the harness starts exactly as invoked."""
    return argv, env


def toml_mcp_fragment(servers):
    """`mcp_servers` fragment for the given servers, TOML-shaped.

    HTTP MCP servers keyed by canonical alias, in the shape Grok Build and
    Codex CLI share: TOML `[mcp_servers.<name>]` tables, where a `url` key is
    what selects the remote transport -- there is no type key. The fragment
    stays JSON here; swarmforge.config.merge_toml_mcp renders it. Returns
    `{}` when `servers` is empty.
    """
    out = {alias: {"url": url} for alias, url in servers.items()}
    return {"mcp_servers": out} if out else {}


@dataclasses.dataclass(frozen=True)
class HarnessSpec:
    """Everything Swarmforge needs to know about one harness.

    The fields record the facts the container driver acts on per harness:
    where its config and assets live, how it learns about MCP servers, and how
    unified agent definitions reach it. A field a harness does not implement
    holds a `Waiver` naming the reason.
    """

    # The name the harness is registered and selected by.
    name: str

    # The executable under /usr/local/bin the pre-exec driver execs.
    binary: str

    # Container path the layered config is merged into when the harness forces
    # one; a Waiver when the run's SWARMFORGE_CONFIG_DEST decides instead, and
    # an unset variable skips the config phase.
    config_dest: object

    # True when the harness always rebuilds the config destination from
    # scratch; False when the run's SWARMFORGE_CONFIG_RESET decides.
    config_reset: bool

    # Additions to the shared config-layer tar excludes ("./opencode.json",
    # "./.swarmforge"), applied when a config layer is merged. Entries are
    # tar patterns and must carry the "./" prefix, or they would match at any
    # depth. Every harness lists the native skills/commands dirs it has: the
    # asset copy is their only transport, so all layers get the same
    # per-entry replacement semantics (a higher layer's skill package
    # replaces the whole package, never file-merges into it), where the tar
    # merge would union layers file-by-file.
    layer_excludes: tuple

    # Files merged key-by-key per layer rather than overlaid whole.
    keyed_files: tuple

    # Where portable skills and commands land. A string may hold the
    # placeholders "{home}" (the anvil user's home) and "{config}" (the merged
    # config destination); a Waiver opts the harness out.
    skills_dest: object
    commands_dest: object

    # Where translated native agents land, under the same placeholder rules.
    agents_dest: object

    # Callable `(servers) -> dict` shaping `{alias: url}` into the harness's
    # MCP config fragment, `{}` for no servers.
    mcp_fragment: object

    # How the anvil learns the generated MCP config path: ("flag", FLAG)
    # appends `FLAG <path>` to the harness argv, ("env", VAR) sets
    # `VAR=<path>` for the config driver to merge.
    mcp_delivery: tuple

    # How the delivered fragment merges into the harness config:
    # "json-replace-mcp" (opencode.json key merge, whole MCP entries replaced)
    # or "toml-managed-block" (a rewritten managed block in config.toml); a
    # Waiver when nothing merges it into a file.
    mcp_merge: object

    # Callable `(name, meta, body) -> (filename, text) | None` producing one
    # native agent file, or None to skip that agent; a Waiver when the harness
    # has no emitter and unified agents are not translated for it.
    agent_emitter: object

    # Container paths outside the home handed to the anvil uid before
    # privileges drop, after every root-phase write. They are changed without
    # following symlinks, so a state link changes owner itself while what it
    # points at is the home pass's business.
    extra_chown_paths: tuple

    # Hook `(dest_dir, emitted, home)` run after every agent file is written,
    # where `emitted` lists `(name, meta, path)` for the agents actually
    # emitted. `home` is the anvil user's home when the container driver runs
    # the translation, and empty for a bare CLI run.
    finalize_agents: object = finalize_agents

    # Hook `(ctx, layer)` run once per asset layer, lowest precedence first,
    # installing that layer's portable skills and commands. The default copies
    # into the declared destinations with per-entry replacement; a harness
    # overrides it when its native asset shape needs more than a copy.
    install_assets: object = install_assets

    # Hook `(ctx)` run after the config layers merge, before the tong MCP
    # servers merge.
    build_config: object = build_config

    # Hook `(ctx)` run after the tong MCP servers merge.
    finalize_config: object = finalize_config

    # Hook `(ctx)` run last, after the whole config phase.
    publish_config: object = publish_config

    # Hook `(ctx)` run after the asset phase, linking whatever state the
    # harness keeps across runs into its config destination.
    link_state: object = link_state

    # Hook `(ctx)` run after state is linked, for container preparation only
    # root can do.
    root_setup: object = root_setup

    # Hook `(ctx, argv, env) -> (argv, env)` with the last word on the argv
    # and the environment the harness binary is exec'd with. It runs as the
    # anvil user, after the root phases, in the process that becomes the
    # harness.
    pre_exec: object = pre_exec
