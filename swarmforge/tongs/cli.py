"""The tong-definition diagnostic CLI.

Not wired into any launch path. `tongs validate <dir>...` lints definitions
layered lowest-to-highest; `tongs discover <dir>...` dumps the merged set.
"""

import json
import sys

from .discovery import discover, merge_tongs
from .model import LAYERS
from .validate import validate_tong


def _layer_dirs_from_argv(paths):
    # Map positional dirs onto LAYERS lowest-first; extra dirs keep the last name.
    pairs = []
    for index, path in enumerate(paths):
        layer = LAYERS[index] if index < len(LAYERS) else LAYERS[-1]
        pairs.append((layer, path))
    return pairs


def main(argv):
    if len(argv) < 2 or argv[0] not in ("validate", "discover"):
        print("usage: tongs {validate|discover} <layer_dir>...", file=sys.stderr)
        return 2
    command, paths = argv[0], argv[1:]
    merged = merge_tongs(discover(_layer_dirs_from_argv(paths)))

    if command == "validate":
        problems = 0
        for name in sorted(merged):
            for error in validate_tong(name, merged[name]["definition"]):
                print(error)
                problems += 1
        if not problems:
            print("ok: %d tong(s) valid" % len(merged))
        return 1 if problems else 0

    summary = {
        name: {"source": entry["source"], "definition": entry["definition"]}
        for name, entry in merged.items()
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
