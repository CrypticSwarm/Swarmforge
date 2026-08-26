#!/usr/bin/env python3
"""Build a TOML config from ordered, key-aware layers."""

import datetime
import json
import math
import os
import re
import sys
import tempfile
import tomllib


USAGE = "usage: python3 -m swarmforge.config.merge_toml --build DST [SRC ...]"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def merge(base, override, *, path=()):
    """Deep-merge two parsed TOML values, with ``override`` taking precedence."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        if path in {("agents",), ("mcp_servers",)}:
            out.update(override)
            return out
        for key, value in override.items():
            out[key] = (
                merge(out[key], value, path=path + (key,))
                if key in out else value
            )
        return out
    return override


def read_layer(path, *, err=sys.stderr):
    """Return a parsed layer, or None when it is absent or invalid."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            value = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        print("skipping %s: %s" % (path, error), file=err)
        return None
    return value


def _key(value):
    return value if _BARE_KEY.fullmatch(value) else _string(value)


def _string(value):
    # JSON basic strings are also TOML basic strings. Keeping Unicode literal
    # avoids surrogate escapes, while DEL still needs an explicit TOML escape.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")


def _value(value):
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = ("%s = %s" % (_key(key), _value(item)) for key, item in value.items())
        return "{ " + ", ".join(fields) + " }"
    raise TypeError("unsupported TOML value: %r" % (value,))


def dumps(value):
    """Serialize a dictionary of values produced by ``tomllib``."""
    lines = []

    def emit_table(table, path, *, heading):
        if heading:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[" + ".".join(_key(part) for part in path) + "]")

        child_tables = []
        for key, item in table.items():
            if isinstance(item, dict):
                child_tables.append((key, item))
            else:
                lines.append("%s = %s" % (_key(key), _value(item)))

        for key, child in child_tables:
            emit_table(child, path + (key,), heading=True)

    emit_table(value, (), heading=False)
    return "\n".join(lines) + ("\n" if lines else "")


def build_file(dst_path, src_paths, *, err=sys.stderr):
    """Build ``dst_path`` solely from ``src_paths``, lowest precedence first."""
    merged = {}
    for path in src_paths:
        layer = read_layer(path, err=err)
        if layer is not None:
            merged = merge(merged, layer)

    text = dumps(merged)
    # Refuse to replace a valid destination with serializer output that our
    # own parser cannot read back.
    tomllib.loads(text)

    directory = os.path.dirname(os.path.abspath(dst_path))
    fd, temporary = tempfile.mkstemp(prefix=".swarmforge-toml-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, dst_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return merged


def main(argv, err=sys.stderr):
    if len(argv) < 2 or argv[0] != "--build":
        print(USAGE, file=err)
        return 2
    for argument in argv[2:]:
        if argument.startswith("--"):
            print("unknown argument %r" % argument, file=err)
            return 2
    build_file(argv[1], argv[2:], err=err)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
