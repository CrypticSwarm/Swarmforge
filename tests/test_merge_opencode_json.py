#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The image puts the swarmforge package on PYTHONPATH; standing in for that
# here keeps this file runnable on its own, not just under a discovery run
# that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge.config import merge_opencode


def _write(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class MergeOpenCodeJsonTests(unittest.TestCase):
    def test_normal_layers_deep_merge_mcp_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "dst.json")
            src = os.path.join(tmp, "src.json")
            _write(dst, {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "enabled": True,
                    },
                },
                "permission": {"bash": {"*": "ask"}},
            })
            _write(src, {
                "mcp": {"github": {"enabled": False}},
                "permission": {"bash": {"git *": "allow"}},
            })

            merge_opencode.merge_files(dst, src)

            self.assertEqual(_read(dst), {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "enabled": False,
                    },
                },
                "permission": {"bash": {"*": "ask", "git *": "allow"}},
            })

    def test_tong_mcp_merge_replaces_whole_server_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "dst.json")
            src = os.path.join(tmp, "src.json")
            _write(dst, {
                "mcp": {
                    "github": {
                        "type": "local",
                        "command": ["gh", "mcp"],
                        "cwd": "/workspace",
                        "enabled": False,
                    },
                    "filesystem": {"type": "local", "command": ["fs"]},
                },
                "permission": {"bash": {"*": "ask"}},
            })
            _write(src, {
                "mcp": {
                    "github": {
                        "type": "remote",
                        "url": "http://github:8080/mcp",
                        "enabled": True,
                    },
                },
                "permission": {"bash": {"git *": "allow"}},
            })

            merge_opencode.merge_files(dst, src, replace_mcp_entries=True)

            self.assertEqual(_read(dst), {
                "mcp": {
                    "github": {
                        "type": "remote",
                        "url": "http://github:8080/mcp",
                        "enabled": True,
                    },
                    "filesystem": {"type": "local", "command": ["fs"]},
                },
                "permission": {"bash": {"*": "ask", "git *": "allow"}},
            })


if __name__ == "__main__":
    unittest.main()
