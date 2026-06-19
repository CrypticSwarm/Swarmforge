#!/usr/bin/env python3
import importlib.util
import json
import os
import tempfile
import unittest


MERGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "anvil",
    "merge_opencode_json.py",
)
_spec = importlib.util.spec_from_file_location("merge_opencode_json", MERGE_PATH)
merge_opencode_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_opencode_json)


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

            merge_opencode_json.merge_files(dst, src)

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

            merge_opencode_json.merge_files(dst, src, replace_mcp_entries=True)

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
