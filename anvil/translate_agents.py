#!/usr/bin/env python3
"""Translate unified Swarmforge agent definitions into harness-native formats.

Unified agent definitions are markdown files whose YAML frontmatter is a
superset of the OpenCode agent schema. The markdown body is the agent's
system prompt and passes through unchanged. Recognized frontmatter:

  description: <when to delegate to this agent>      (required)
  mode: subagent | primary | all                     (OpenCode only)
  model: <provider/model-id or harness alias>
  temperature: <float>                               (OpenCode only)
  tools:                                             (map of tool -> bool)
    write: false
  claude:                                            (per-harness overrides,
    model: haiku                                      merged into the output
  opencode:                                           frontmatter verbatim)
    permission: ...

The agent's identity is its filename (foo.md -> agent "foo"); a `name` field
is emitted only for harnesses that require one. Tool names use OpenCode's
lowercase ids; for harnesses without an equivalent the entry is dropped.
`disable: true` skips the agent on harnesses without native disable support.

Per-target rules:
  opencode  Frontmatter passes through minus other harnesses' override
            blocks; `model` is dropped unless provider-qualified (contains
            "/"). Translation is idempotent, so a directory can be
            translated in place.
  claude    Emits name/description, maps `tools: {x: false}` entries to
            `disallowedTools`, rewrites `model` (anthropic/<id> -> <id>,
            other providers dropped, aliases pass through), and drops
            OpenCode-only fields.

Usage: translate_agents.py <target> <dest_dir> <src_dir>...

Later source directories override earlier ones by filename. Missing or
empty source paths are skipped. Only top-level *.md files are read.
"""

import json
import os
import re
import sys

HARNESS_OVERRIDE_KEYS = {"claude", "opencode"}

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

OPENCODE_ONLY_FIELDS = {
    "mode",
    "temperature",
    "top_p",
    "steps",
    "permission",
    "hidden",
    "disable",
    "tools",
}


def warn(message):
    print("translate_agents: %s" % message, file=sys.stderr)


# --- Minimal YAML subset ----------------------------------------------------
# Frontmatter is restricted to nested maps of scalars and flat lists, which
# keeps the harness image free of third-party Python packages.


def parse_scalar(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part) for part in inner.split(",")]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("", "null", "~"):
        return None
    for converter in (int, float):
        try:
            return converter(text)
        except ValueError:
            pass
    return text


def parse_map(lines, index, indent):
    out = {}
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        current = len(line) - len(line.lstrip(" "))
        if current < indent:
            break
        if current > indent or stripped.startswith("- "):
            raise ValueError("unexpected layout at line: %r" % line)
        key, sep, rest = stripped.partition(":")
        if not sep:
            raise ValueError("expected 'key: value' at line: %r" % line)
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            out[key] = parse_scalar(rest)
            continue
        peek = index
        while peek < len(lines) and not lines[peek].strip():
            peek += 1
        if peek < len(lines):
            next_indent = len(lines[peek]) - len(lines[peek].lstrip(" "))
            if next_indent > indent:
                if lines[peek].strip().startswith("- "):
                    out[key], index = parse_list(lines, peek, next_indent)
                else:
                    out[key], index = parse_map(lines, peek, next_indent)
                continue
        out[key] = None
    return out, index


def parse_list(lines, index, indent):
    out = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        current = len(line) - len(line.lstrip(" "))
        if current != indent or not line.strip().startswith("- "):
            break
        out.append(parse_scalar(line.strip()[2:]))
        index += 1
    return out, index


PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,()/+-]*$")


def emit_scalar(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if PLAIN_SCALAR_RE.match(text) and not text.endswith(" "):
        if parse_scalar(text) == text:
            return text
    return json.dumps(text)


def emit_map(mapping, indent=0):
    lines = []
    pad = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append("%s%s:" % (pad, key))
            lines.extend(emit_map(value, indent + 2))
        elif isinstance(value, list):
            lines.append("%s%s:" % (pad, key))
            for item in value:
                lines.append("%s  - %s" % (pad, emit_scalar(item)))
        else:
            lines.append("%s%s: %s" % (pad, key, emit_scalar(value)))
    return lines


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            meta, _ = parse_map(lines[1:end], 0, 0)
            body = "\n".join(lines[end + 1 :]).lstrip("\n")
            return meta, body
    raise ValueError("unterminated frontmatter")


def render(meta, body):
    return "---\n%s\n---\n\n%s" % ("\n".join(emit_map(meta)), body)


# --- Per-harness emitters ---------------------------------------------------


def to_opencode(name, meta):
    out = {k: v for k, v in meta.items() if k not in HARNESS_OVERRIDE_KEYS and k != "name"}
    model = out.get("model")
    if model is not None and "/" not in str(model):
        del out["model"]
    overrides = meta.get("opencode")
    if isinstance(overrides, dict):
        out.update(overrides)
    return out


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

    skipped = OPENCODE_ONLY_FIELDS | HARNESS_OVERRIDE_KEYS | {"name", "description", "model"}
    for key, value in meta.items():
        if key not in skipped:
            out[key] = value

    overrides = meta.get("claude")
    if isinstance(overrides, dict):
        out.update(overrides)
    return out


EMITTERS = {
    "opencode": to_opencode,
    "claude": to_claude,
}


def load_agents(src_dirs):
    agents = {}
    for src_dir in src_dirs:
        if not src_dir or not os.path.isdir(src_dir):
            continue
        for filename in sorted(os.listdir(src_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(src_dir, filename)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            try:
                agents[filename] = split_frontmatter(text)
            except ValueError as exc:
                warn("skipping %s: %s" % (path, exc))
    return agents


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target, dest_dir = argv[0], argv[1]
    emitter = EMITTERS.get(target)
    if emitter is None:
        warn("unknown target '%s' (expected: %s)" % (target, ", ".join(sorted(EMITTERS))))
        return 2

    agents = load_agents(argv[2:])
    if not agents:
        return 0

    os.makedirs(dest_dir, exist_ok=True)
    for filename, (meta, body) in agents.items():
        name = filename[: -len(".md")]
        out_meta = emitter(name, meta)
        if out_meta is None:
            continue
        with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(render(out_meta, body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
