#!/usr/bin/env python3
"""Merge OpenCode ``opencode.json`` layers."""

import json
import sys


def merge(base, override, *, replace_mcp_entries=False, path=()):
    """Deep-merge ``override`` into ``base``.

    Normal config layers are recursively merged. Generated tong MCP fragments can
    opt into whole-entry replacement under ``mcp`` so a generated remote server
    does not inherit stale local-server keys from a lower-precedence config.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        if replace_mcp_entries and path == ("mcp",):
            out = dict(base)
            out.update(override)
            return out
        out = dict(base)
        for key, value in override.items():
            if key in out:
                out[key] = merge(
                    out[key], value,
                    replace_mcp_entries=replace_mcp_entries,
                    path=path + (key,),
                )
            else:
                out[key] = value
        return out
    return override


def merge_files(dst_path, src_path, *, replace_mcp_entries=False):
    with open(dst_path, "r", encoding="utf-8") as handle:
        dst = json.load(handle)
    with open(src_path, "r", encoding="utf-8") as handle:
        src = json.load(handle)

    with open(dst_path, "w", encoding="utf-8") as handle:
        json.dump(merge(dst, src, replace_mcp_entries=replace_mcp_entries), handle, indent=2)
        handle.write("\n")


def main(argv):
    if len(argv) not in (2, 3):
        print(
            "usage: merge_opencode_json.py DST SRC [--replace-mcp-entries]",
            file=sys.stderr,
        )
        return 2
    replace_mcp_entries = False
    if len(argv) == 3:
        if argv[2] != "--replace-mcp-entries":
            print("unknown argument %r" % argv[2], file=sys.stderr)
            return 2
        replace_mcp_entries = True
    merge_files(argv[0], argv[1], replace_mcp_entries=replace_mcp_entries)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
