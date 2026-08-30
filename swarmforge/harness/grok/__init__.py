"""The Grok Build harness."""

from swarmforge.harness.spec import HarnessSpec, Waiver, toml_mcp_fragment

SPEC = HarnessSpec(
    name="grok",
    binary="grok",
    config_dest=Waiver(
        "the run's SWARMFORGE_CONFIG_DEST names the destination, and an unset "
        "variable skips the config phase"
    ),
    config_reset=False,
    # bin/downloads/completions are the host installer's own artifacts and
    # the dest is a persistent home: the container has its own
    # /usr/local/bin/grok, so copying them in would only leave them there.
    layer_excludes=("./skills", "./commands", "./bin", "./downloads", "./completions"),
    keyed_files=("opencode.json",),
    skills_dest="{home}/.grok/skills",
    commands_dest="{home}/.grok/commands",
    agents_dest=Waiver(
        "no destination is declared and unified agent definitions are not "
        "delivered to grok"
    ),
    mcp_fragment=toml_mcp_fragment,
    mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
    mcp_merge="toml-managed-block",
    agent_emitter=Waiver(
        "no emitter is defined, so the translator rejects grok as a target"
    ),
    extra_chown_paths=(),
)
