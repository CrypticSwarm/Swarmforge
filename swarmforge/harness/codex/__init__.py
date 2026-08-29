"""The Codex CLI harness."""

import os
import re

from swarmforge.agents.emit import (
    emit_toml_key,
    emit_toml_multiline,
    emit_toml_value,
    warn,
)
from swarmforge.harness.spec import HarnessSpec, Waiver, toml_mcp_fragment

CODEX_AGENT_TABLE_FIELDS = {
    "default_subagent_model",
    "enabled",
    "max_depth",
}


def render_codex(meta):
    lines = []
    for key, value in meta.items():
        rendered = (
            emit_toml_multiline(value)
            if key == "developer_instructions"
            else emit_toml_value(value)
        )
        lines.append("%s = %s" % (emit_toml_key(key), rendered))
    return "\n".join(lines) + "\n"


def normalize_codex_name(name):
    normalized = re.sub(r"[^A-Za-z0-9 _-]+", "-", str(name))
    normalized = normalized.strip(" _-") or "agent"
    if normalized in CODEX_AGENT_TABLE_FIELDS:
        normalized = "agent-" + normalized
    return normalized


def registered_name(name, meta):
    """The name an emitted agent registers under in config.toml.

    The declared `name` (falling back to the source filename) after codex
    normalization, with a `codex:` override block's own `name` winning.
    """
    requested = meta.get("name", name)
    overrides = meta.get("codex")
    if isinstance(overrides, dict) and "name" in overrides:
        requested = overrides["name"]
    return normalize_codex_name(requested)


def to_codex(name, meta, body):
    if meta.get("disable") is True:
        return None
    requested_name = meta.get("name", name)
    codex_name = normalize_codex_name(requested_name)
    if codex_name != requested_name:
        warn("agent '%s': Codex name normalized to '%s'" % (name, codex_name))
    out = {"name": codex_name, "developer_instructions": body}
    if "description" in meta:
        out["description"] = meta["description"]
    else:
        warn("agent '%s' has no description" % name)

    model = meta.get("model")
    if model is not None:
        provider, sep, model_id = str(model).partition("/")
        if not sep:
            out["model"] = model
        elif provider == "openai":
            out["model"] = model_id

    if "tools" in meta:
        warn(
            "agent '%s': tool restrictions are not translated for Codex; "
            "use codex sandbox/MCP settings" % name
        )

    overrides = meta.get("codex")
    if isinstance(overrides, dict):
        out.update(overrides)
    out["name"] = registered_name(name, meta)
    return out


def agent_emitter(name, meta, body):
    """The native filename and full file text for one agent, or None to skip it."""
    out = to_codex(name, meta, body)
    if out is None:
        return None
    return "%s.toml" % normalize_codex_name(name), render_codex(out)


def finalize_agents(dest_dir, emitted):
    """Register every emitted agent in a config.toml beside the agent files."""
    registrations = {}
    for name, meta, path in emitted:
        registrations[registered_name(name, meta)] = {
            "config_file": os.path.abspath(path)
        }
    if not registrations:
        return
    config_path = os.path.join(dest_dir, "config.toml")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write(render_codex({"agents": registrations}))


SPEC = HarnessSpec(
    name="codex",
    binary="codex",
    config_dest="/run/swarmforge/codex-config",
    config_reset=True,
    layer_excludes=(
        "./skills",
        "./packages",
        "./sessions",
        "./history.jsonl",
        "./log",
        "./config.toml",
    ),
    keyed_files=("opencode.json",),
    skills_dest="{home}/.agents/skills",
    commands_dest=Waiver(
        "portable commands become skill packages under the skills destination "
        "instead of a commands directory"
    ),
    agents_dest="/run/swarmforge/codex-agents",
    mcp_fragment=toml_mcp_fragment,
    mcp_delivery=("env", "SWARMFORGE_TONG_MCP_FILE"),
    mcp_merge="toml-managed-block",
    agent_emitter=agent_emitter,
    finalize_agents=finalize_agents,
    extra_chown_paths=("/run/swarmforge/codex-agents",),
)
