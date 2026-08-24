#!/usr/bin/env python3
"""Render generated tong MCP servers into a harness's TOML config.

Grok Build and Codex CLI both read MCP servers from TOML
``[mcp_servers.<name>]`` tables, where a ``url`` key is what selects the
remote transport -- there is no type key. The launcher emits the discovered
tongs as JSON; this module renders them into the file the entrypoint names.

Both harnesses merge into a persistent home, so the servers cannot simply be
appended: they go in a sentinel-delimited managed block, rewritten on every
run and removed when no fragment is given, so a session with no tongs leaves
no trace of an earlier one's servers.

A name already defined outside the block is skipped with a warning.
Appending it would be a TOML duplicate-table error, and the user's own
definition outranking a generated one matches both harnesses' config
precedence.
"""

import json
import re
import sys
import tomllib

USAGE = "usage: python3 -m swarmforge.config.merge_toml_mcp CONFIG_TOML [FRAGMENT_JSON]"

BLOCK_BEGIN = "# >>> swarmforge tong mcp servers (generated; do not edit) >>>"
BLOCK_END = "# <<< swarmforge tong mcp servers <<<"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def strip_block(text):
    """``text`` without the managed block (sentinel lines included).

    An unterminated begin sentinel swallows to end of file: the block is
    machine-owned, so a missing end marker means a corrupted block, and
    dropping it beats duplicating it.
    """
    out = []
    in_block = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if not in_block and stripped == BLOCK_BEGIN:
            in_block = True
            continue
        if in_block:
            if stripped == BLOCK_END:
                in_block = False
            continue
        out.append(line)
    return "".join(out)


def _toml_key(name):
    return name if _BARE_KEY.match(name) else json.dumps(name)


def _toml_value(value):
    # JSON scalar/array syntax is valid TOML for strings, booleans, numbers,
    # and arrays of those; the launcher's fragment never contains nested
    # tables as values.
    return json.dumps(value)


def render_block(servers):
    lines = [BLOCK_BEGIN]
    for name in sorted(servers):
        lines.append("[mcp_servers.%s]" % _toml_key(name))
        for key in sorted(servers[name]):
            lines.append("%s = %s" % (_toml_key(key), _toml_value(servers[name][key])))
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def _existing_server_names(text, path):
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # The harness itself will refuse the invalid config with its own
        # error; the duplicate check is all that degrades here.
        print(
            "Warning: could not parse %s (%s); skipping duplicate-name check"
            % (path, exc),
            file=sys.stderr,
        )
        return set()
    return set(parsed.get("mcp_servers") or {})


def merge(config_path, fragment_path=None):
    servers = {}
    if fragment_path:
        try:
            with open(fragment_path, "r", encoding="utf-8") as handle:
                servers = (json.load(handle) or {}).get("mcp_servers") or {}
        except FileNotFoundError:
            pass

    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            original = handle.read()
    except FileNotFoundError:
        original = None

    base = strip_block(original) if original else ""

    kept = {}
    if servers:
        taken = _existing_server_names(base, config_path)
        for name in sorted(servers):
            if name in taken:
                print(
                    "Warning: mcp server '%s' already defined in %s; keeping the existing entry"
                    % (name, config_path),
                    file=sys.stderr,
                )
                continue
            kept[name] = servers[name]

    trimmed = base.rstrip("\n")
    if kept:
        block = render_block(kept)
        new_text = trimmed + "\n\n" + block if trimmed else block
    else:
        new_text = trimmed + "\n" if trimmed else ""

    if original is None and not new_text:
        return
    if new_text != original:
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(new_text)


def main(argv):
    if len(argv) not in (1, 2):
        print(USAGE, file=sys.stderr)
        return 2
    merge(argv[0], argv[1] if len(argv) == 2 else None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
