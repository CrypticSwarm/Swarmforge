"""Rendering and frontmatter helpers for the unified agent format.

The translator CLI and the harness modules share these: splitting a unified
definition into frontmatter and body, rendering frontmatter back as YAML, and
rendering a mapping as TOML for the harnesses whose native agents are TOML.
"""

import json
import re
import sys

from swarmforge.yamlite import parse_map, parse_scalar

# Unified-schema fields only OpenCode consumes; other harnesses drop them.
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


# The prefix names the CLI entry point a user invokes, not this module.
def warn(message):
    print("swarmforge.agents.translate: %s" % message, file=sys.stderr)


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


TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def emit_toml_key(value):
    text = str(value)
    return text if TOML_BARE_KEY_RE.fullmatch(text) else emit_toml_string(text)


def emit_toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def emit_toml_multiline(value):
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return chr(34) * 3 + "\n" + text + chr(34) * 3


def emit_toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[%s]" % ", ".join(emit_toml_value(item) for item in value)
    if isinstance(value, dict):
        pairs = (
            "%s = %s" % (emit_toml_key(key), emit_toml_value(item))
            for key, item in value.items()
        )
        return "{ %s }" % ", ".join(pairs)
    return emit_toml_string(value)
