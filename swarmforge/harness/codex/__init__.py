"""The Codex CLI harness."""

import os
import re
import shutil
import sys

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


def finalize_agents(dest_dir, emitted, home=""):
    """Register every emitted agent so codex discovers it.

    The registrations are written to a config.toml beside the agent files and
    merged into the published `<home>/.codex/config.toml`, which is where
    codex looks for them. A failed merge degrades to a warning: the session
    still runs, only without subagents.
    """
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

    if not home:
        return
    config_file = home + "/.codex/config.toml"
    # Imported at call time: the launcher imports this module on whatever
    # python3 the host has, while merge_toml needs the container's tomllib
    # (3.11+) and this merge only ever runs there.
    from swarmforge.config import merge_toml

    try:
        # Sources lowest precedence first: the published config's own keys
        # outrank the generated registrations.
        merge_toml.build_file(config_file, [config_path, config_file])
    except Exception:
        print(
            "Warning: Codex agent registration failed; continuing",
            file=sys.stderr,
        )


def build_config(ctx):
    """Rebuild config.toml from the layers, key-wise, repo -> user -> org.

    The whole-file tar overlay excludes config.toml, so this is where the
    layers reach it. An empty layer contributes no source at all. Errors
    propagate: a config the serializer cannot round-trip must fail the run
    rather than hand codex something it will not read.
    """
    # Imported at call time: the launcher imports this module on whatever
    # python3 the host has, while merge_toml needs the container's tomllib
    # (3.11+) and this hook only ever runs there.
    from swarmforge.config import merge_toml

    sources = [
        src + "/config.toml"
        for src in (ctx.config_repo_src, ctx.config_user_src, ctx.config_org_src)
        if src
    ]
    merge_toml.build_file(ctx.config_dest + "/config.toml", sources)


def publish_config(ctx):
    """Copy the built config.toml into codex's persistent home.

    The config is rebuilt outside that home, which also holds state, and
    copied back so config.toml stays writable for codex's own atomic updates.
    """
    config_file = ctx.home + "/.codex/config.toml"
    # Truncation clears the prior run even when no layer supplies config.toml.
    with open(config_file, "w", encoding="utf-8"):
        pass
    built = ctx.config_dest + "/config.toml"
    if os.path.isfile(built):
        # Content only, onto the file just created.
        shutil.copyfile(built, config_file)


SPEC = HarnessSpec(
    name="codex",
    binary="codex",
    config_dest="/run/swarmforge/codex-config",
    config_reset=True,
    # packages, sessions, history.jsonl, and log are session state the dest
    # rebuild must not resurrect; config.toml merges by key through
    # build_config instead of overlaying whole.
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
    build_config=build_config,
    publish_config=publish_config,
)
