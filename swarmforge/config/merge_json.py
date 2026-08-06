#!/usr/bin/env python3
"""Merge layered JSON config files.

Nothing here is specific to one harness: both OpenCode's ``opencode.json`` and
Claude's ``settings.json`` are layered JSON objects, and the deep merge below
is the whole of what they share.
"""

import json
import os
import sys

USAGE = (
    "usage: python3 -m swarmforge.config.merge_json DST SRC"
    " [--replace-mcp-entries]\n"
    "       python3 -m swarmforge.config.merge_json --build DST [SRC ...]"
)


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


def read_layer(path, *, err=sys.stderr):
    """The JSON object a layer contributes, or None when it contributes none.

    A layer that ships no such file is not an error -- most layers ship none,
    and the callers pass a path for every layer whether or not it exists. One
    that ships a file it cannot contribute is reported and dropped, so a
    single malformed layer costs its own keys rather than the whole build.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as error:
        print("skipping %s: %s" % (path, error), file=err)
        return None
    if not isinstance(value, dict):
        print("skipping %s: not a JSON object" % path, file=err)
        return None
    return value


def build_file(dst_path, src_paths, *, err=sys.stderr):
    """Write ``dst_path`` as the merge of ``src_paths``, lowest layer first.

    The destination is an output only: it is never read, so a key it holds
    that no layer sets is gone after the write. That is what makes the result
    a function of the layers alone, rather than of every run that came before.

    The write truncates the destination in place. It is a bind-mounted file,
    and writing a temporary beside it to rename over would fail on the mount.
    That rules out the usual way to keep a half-written file from being read,
    so the text is serialised in full before the destination is opened: the
    window where the path holds partial JSON is one write, not one per key.
    """
    merged = {}
    for path in src_paths:
        layer = read_layer(path, err=err)
        if layer is not None:
            merged = merge(merged, layer)

    text = json.dumps(merged, indent=2) + "\n"
    with open(dst_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return merged


def main(argv, err=sys.stderr):
    if argv and argv[0] == "--build":
        if len(argv) < 2:
            print(USAGE, file=err)
            return 2
        # Every remaining argument is a layer path, and a layer path is never
        # an option. Without this a misspelled flag reads as a layer that
        # happens not to exist, and is dropped as quietly as a real one.
        for argument in argv[2:]:
            if argument.startswith("--"):
                print("unknown argument %r" % argument, file=err)
                return 2
        build_file(argv[1], argv[2:], err=err)
        return 0
    if len(argv) not in (2, 3):
        print(USAGE, file=err)
        return 2
    replace_mcp_entries = False
    if len(argv) == 3:
        if argv[2] != "--replace-mcp-entries":
            print("unknown argument %r" % argv[2], file=err)
            return 2
        replace_mcp_entries = True
    merge_files(argv[0], argv[1], replace_mcp_entries=replace_mcp_entries)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
