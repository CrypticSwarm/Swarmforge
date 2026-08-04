#!/usr/bin/env python3
"""Fill unset keys of Claude Code's ``settings.json`` with image defaults.

Claude has no settings layer below the user's own ``~/.claude/settings.json``,
so an image default has to be written into that file. It is written after the
config layers have merged and only for keys no layer set, which keeps the
precedence the layers already have: whatever a layer chose stays.
"""

import json
import os
import sys


def statusline(command):
    """The ``statusLine`` value that runs ``command`` for every status update."""
    return {"type": "command", "command": command}


def seed(settings, defaults):
    """Return ``settings`` with every key it does not already have defaulted."""
    seeded = dict(settings)
    for key, value in defaults.items():
        seeded.setdefault(key, value)
    return seeded


def seed_file(path, defaults):
    """Seed ``defaults`` into the settings file at ``path``. True if it changed.

    A missing file is created. A file that is not a JSON object is left exactly
    as it is: it is the user's, and a default is not worth losing it over.
    """
    settings = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                settings = json.load(handle)
        except ValueError:
            raise ValueError("%s is not valid JSON" % path)
        if not isinstance(settings, dict):
            raise ValueError("%s is not a JSON object" % path)

    seeded = seed(settings, defaults)
    if seeded == settings and os.path.exists(path):
        return False

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(seeded, handle, indent=2)
        handle.write("\n")
    return True


def main(argv):
    if len(argv) != 2:
        print(
            "usage: seed_claude_settings.py SETTINGS_JSON STATUSLINE_COMMAND",
            file=sys.stderr,
        )
        return 2
    try:
        seed_file(argv[0], {"statusLine": statusline(argv[1])})
    except (ValueError, OSError) as error:
        print("seed_claude_settings: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
