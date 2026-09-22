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

    harness: str

    # The anvil user's home inside the container.
    home: str

    # Directory the layered config is merged into for this run.
    config_dest: str

    # The three config layer source dirs, lowest precedence first.
    config_repo_src: str
    config_user_src: str
    config_org_src: str

    tong_mcp_file: str

    # The working directory the harness process starts in.
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

    A top-level entry whose name starts with a dot is skipped: the portable
    asset contract is a directory of named skill and command entries, and
    dotfiles beside them belong to whoever authored the layer. Hidden files
    inside a copied entry travel with it. An empty `dst_dir` is a destination
    the harness waived, and nothing is copied or created for it.
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

    name: str

    # Container path the layered config is merged into, when the harness pins one.
    config_dest: object

    # True when the harness always rebuilds its config destination; else the run.
    config_reset: bool

    # Additions to the shared config-layer tar excludes. Entries are tar
    # patterns and must carry the "./" prefix, or they match at any depth.
    # Every harness excludes the native skills/commands dirs it has, so the
    # asset copy is their only transport: that copy replaces a package whole,
    # where the tar merge would union the layers file-by-file.
    layer_excludes: tuple

    # Where portable skills and commands land; "{home}"/"{config}" expand.
    skills_dest: object
    commands_dest: object

    # Where translated native agents land, under the same placeholders.
    agents_dest: object

    # Callable `(servers) -> dict` shaping `{alias: url}` into an MCP fragment.
    mcp_fragment: object

    # How the anvil learns the MCP config path: ("flag", FLAG) or ("env", VAR).
    mcp_delivery: tuple

    # Which merge carries the fragment in: json-replace-mcp or toml-managed-block.
    mcp_merge: object

    # Callable `(name, meta, body) -> (filename, text) | None` per agent.
    agent_emitter: object

    # Container paths outside the home to hand to the anvil uid.
    extra_chown_paths: tuple

    # Hook `(dest_dir, emitted, home)` run after every agent file is written.
    finalize_agents: object = finalize_agents

    # Hook `(ctx, layer)` run once per asset layer, lowest precedence first.
    install_assets: object = install_assets

    # Hook `(ctx)` run after the config layers merge, before the MCP merge.
    build_config: object = build_config

    # Hook `(ctx)` run after the tong MCP servers merge.
    finalize_config: object = finalize_config

    # Hook `(ctx)` run last, after the whole config phase.
    publish_config: object = publish_config

    # Hook `(ctx)` run after the asset phase, linking persistent state in.
    link_state: object = link_state

    # Hook `(ctx)` run after state is linked, for root-only preparation.
    root_setup: object = root_setup

    # Hook `(ctx, argv, env) -> (argv, env)` run last, as the anvil user.
    pre_exec: object = pre_exec
