"""The contract a harness module declares to the rest of Swarmforge."""

import dataclasses


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


def finalize_agents(dest_dir, emitted):
    """Default finalize-agents hook: nothing follows the emitted files."""


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

    The fields record the facts the container entrypoint acts on per harness:
    where its config and assets live, how it learns about MCP servers, and how
    unified agent definitions reach it. A field a harness does not implement
    holds a `Waiver` naming the reason.
    """

    # The name the harness is registered and selected by.
    name: str

    # The executable under /usr/local/bin the entrypoint execs.
    binary: str

    # Container path the layered config is merged into when the harness forces
    # one; a Waiver when the run's SWARMFORGE_CONFIG_DEST decides instead, and
    # an unset variable skips the config phase.
    config_dest: object

    # True when the harness always rebuilds the config destination from
    # scratch; False when the run's SWARMFORGE_CONFIG_RESET decides.
    config_reset: bool

    # Additions to the shared config-layer tar excludes ("./opencode.json",
    # "./.swarmforge"), applied when a config layer is merged.
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
    # `VAR=<path>` for the entrypoint to merge.
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
    # privileges drop.
    extra_chown_paths: tuple

    # Hook `(dest_dir, emitted)` run after every agent file is written, where
    # `emitted` lists `(name, meta, path)` for the agents actually emitted.
    finalize_agents: object = finalize_agents
