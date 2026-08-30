"""The Claude Code harness."""

import os
import sys

from swarmforge.agents.emit import OPENCODE_ONLY_FIELDS, render, warn
from swarmforge.config import merge_json
from swarmforge.harness.spec import HarnessSpec, Waiver

# Derived from the layers on every run and never read as an input. It stays
# off the persistent home -- one directory shared by every container for this
# user, where it would carry an org layer's permissions, hooks, and env into
# later runs that do not mount that layer -- and rides claude's command line
# instead, delivered by the entrypoint's exec.
SETTINGS_FILE = "/run/swarmforge/claude-settings.json"

# The image's own defaults, and the bottom settings layer: any higher and the
# image would overrule a key a session chose. This is the path claude's
# image.sh installs to.
IMAGE_DEFAULT_SETTINGS = "/usr/local/share/swarmforge/claude-settings.json"

# OpenCode tool id -> Claude Code tool name. Ids mapping to None have no
# Claude equivalent and are dropped.
CLAUDE_TOOL_NAMES = {
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "list": None,
    "patch": None,
    "skill": "Skill",
    "task": "Task",
    "todoread": None,
    "todowrite": "TodoWrite",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
}


def _override_keys():
    # Imported at call time: the registry package imports this module while
    # building the harness table, so the table does not exist yet at import.
    from swarmforge import harness

    return harness.agent_override_keys()


def to_claude(name, meta):
    if meta.get("disable") is True:
        return None
    out = {"name": meta.get("name", name)}
    if "description" in meta:
        out["description"] = meta["description"]
    else:
        warn("agent '%s' has no description" % name)

    model = meta.get("model")
    if model is not None:
        provider, sep, model_id = str(model).partition("/")
        if not sep:
            out["model"] = model
        elif provider == "anthropic":
            out["model"] = model_id

    tools = meta.get("tools")
    if isinstance(tools, dict):
        disallowed = []
        for tool, enabled in tools.items():
            if enabled is not False:
                continue
            mapped = CLAUDE_TOOL_NAMES.get(tool)
            if mapped is None:
                if tool not in CLAUDE_TOOL_NAMES:
                    warn("agent '%s': unknown tool '%s' skipped" % (name, tool))
                continue
            disallowed.append(mapped)
        if disallowed:
            out["disallowedTools"] = ", ".join(disallowed)
    elif tools is not None:
        warn("agent '%s': 'tools' must be a map of tool -> bool" % name)

    skipped = OPENCODE_ONLY_FIELDS | _override_keys() | {"name", "description", "model"}
    for key, value in meta.items():
        if key not in skipped:
            out[key] = value

    overrides = meta.get("claude")
    if isinstance(overrides, dict):
        out.update(overrides)
    return out


def agent_emitter(name, meta, body):
    """The native filename and full file text for one agent, or None to skip it."""
    out = to_claude(name, meta)
    if out is None:
        return None
    return "%s.md" % name, render(out, body)


def mcp_fragment(servers):
    """Claude Code `--mcp-config` document for the given servers.

    HTTP MCP servers keyed by canonical alias under `mcpServers`, the shape
    Claude reads from the file passed as `claude --mcp-config <path>`. Returns
    `{}` when `servers` is empty.
    """
    out = {alias: {"type": "http", "url": url} for alias, url in servers.items()}
    return {"mcpServers": out} if out else {}


def finalize_config(ctx):
    """Build the settings file claude is handed on its command line.

    Settings are built repo -> user -> org above the image defaults. A failed
    build must still leave valid JSON at the path the exec names, and an empty
    object is the safe reading of "no layer could be applied".
    """
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    # A path is passed for every layer whether or not it exists, which is the
    # normal case merge_json.build_file skips over; an empty layer contributes
    # "/settings.json", which is no more present than the rest.
    sources = [IMAGE_DEFAULT_SETTINGS] + [
        src + "/settings.json"
        for src in (ctx.config_repo_src, ctx.config_user_src, ctx.config_org_src)
    ]
    try:
        merge_json.build_file(SETTINGS_FILE, sources)
    except Exception:
        print(
            "Warning: could not build Claude settings.json; continuing",
            file=sys.stderr,
        )
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        except OSError:
            pass


SPEC = HarnessSpec(
    name="claude",
    binary="claude",
    config_dest="/run/swarmforge/claude-config",
    config_reset=False,
    # agents/ is kept out because unified agent translation is its sole
    # source; settings.json merges by key through finalize_config instead of
    # overlaying whole; .credentials.json stays out because the default user
    # layer is the host's own ~/.claude and the store is named elsewhere, so
    # a merged copy is a secret nothing reads.
    layer_excludes=(
        "./skills",
        "./commands",
        "./agents",
        "./settings.json",
        "./.credentials.json",
    ),
    keyed_files=("opencode.json",),
    skills_dest="{config}/skills",
    commands_dest="{config}/commands",
    agents_dest="{config}/agents",
    mcp_fragment=mcp_fragment,
    mcp_delivery=("flag", "--mcp-config"),
    mcp_merge=Waiver(
        "the fragment reaches claude on its command line; nothing merges it "
        "into a config file"
    ),
    agent_emitter=agent_emitter,
    extra_chown_paths=("/run/swarmforge/claude-config",),
    finalize_config=finalize_config,
)
