"""The OpenCode harness."""

from swarmforge.agents.emit import render
from swarmforge.harness.spec import HarnessSpec, Waiver


def _override_keys():
    # Imported at call time: the registry package imports this module while
    # building the harness table, so the table does not exist yet at import.
    from swarmforge import harness

    return harness.agent_override_keys()


def to_opencode(name, meta):
    override_keys = _override_keys()
    out = {k: v for k, v in meta.items() if k not in override_keys and k != "name"}
    model = out.get("model")
    if model is not None and "/" not in str(model):
        del out["model"]
    overrides = meta.get("opencode")
    if isinstance(overrides, dict):
        out.update(overrides)
    return out


def agent_emitter(name, meta, body):
    """The native filename and full file text for one agent, or None to skip it."""
    return "%s.md" % name, render(to_opencode(name, meta), body)


def mcp_fragment(servers):
    """OpenCode `mcp` fragment for the given servers.

    Remote (HTTP) MCP servers keyed by canonical alias, shaped for merging
    into `opencode.json`. Returns `{}` when `servers` is empty, so the
    fragment is omitted entirely.
    """
    out = {
        alias: {"type": "remote", "url": url, "enabled": True}
        for alias, url in servers.items()
    }
    return {"mcp": out} if out else {}


SPEC = HarnessSpec(
    name="opencode",
    binary="opencode",
    config_dest=Waiver(
        "the run's SWARMFORGE_CONFIG_DEST names the destination, and an unset "
        "variable skips the config phase"
    ),
    config_reset=False,
    layer_excludes=("./skills", "./command"),
    keyed_files=("opencode.json",),
    skills_dest="{config}/skills",
    commands_dest="{config}/command",
    agents_dest="{config}/agents",
    mcp_fragment=mcp_fragment,
    mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
    mcp_merge="json-replace-mcp",
    agent_emitter=agent_emitter,
    extra_chown_paths=(),
)
