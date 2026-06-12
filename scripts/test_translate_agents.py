#!/usr/bin/env python3
"""Unit tests for anvil/translate_agents.py. Run: python3 scripts/test_translate_agents.py"""

import importlib.util
import os
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "anvil",
    "translate_agents.py",
)
spec = importlib.util.spec_from_file_location("translate_agents", MODULE_PATH)
ta = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ta)

UNIFIED = """---
description: Reviews code for defects.
mode: subagent
temperature: 0.1
model: anthropic/claude-sonnet-4-6
tools:
  write: false
  edit: false
  bash: false
claude:
  maxTurns: 12
opencode:
  steps: 8
---

You are the reviewer agent.
"""


class FrontmatterTests(unittest.TestCase):
    def test_split_and_parse(self):
        meta, body = ta.split_frontmatter(UNIFIED)
        self.assertEqual(meta["description"], "Reviews code for defects.")
        self.assertEqual(meta["temperature"], 0.1)
        self.assertEqual(meta["tools"], {"write": False, "edit": False, "bash": False})
        self.assertEqual(meta["claude"], {"maxTurns": 12})
        self.assertEqual(body, "You are the reviewer agent.\n")

    def test_no_frontmatter(self):
        meta, body = ta.split_frontmatter("just a prompt\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just a prompt\n")

    def test_scalars(self):
        self.assertEqual(ta.parse_scalar("true"), True)
        self.assertEqual(ta.parse_scalar('"quoted: text"'), "quoted: text")
        self.assertEqual(ta.parse_scalar("[a, b]"), ["a", "b"])

    def test_render_roundtrip(self):
        meta, body = ta.split_frontmatter(UNIFIED)
        again, body2 = ta.split_frontmatter(ta.render(meta, body))
        self.assertEqual(meta, again)
        self.assertEqual(body, body2)

    def test_render_quotes_ambiguous_strings(self):
        rendered = ta.render({"description": "Use when: reviewing"}, "x")
        meta, _ = ta.split_frontmatter(rendered)
        self.assertEqual(meta["description"], "Use when: reviewing")


class ClaudeEmitterTests(unittest.TestCase):
    def setUp(self):
        self.meta, _ = ta.split_frontmatter(UNIFIED)

    def test_basic_translation(self):
        out = ta.to_claude("reviewer", self.meta)
        self.assertEqual(out["name"], "reviewer")
        self.assertEqual(out["description"], "Reviews code for defects.")
        self.assertEqual(out["disallowedTools"], "Write, Edit, Bash")
        self.assertEqual(out["model"], "claude-sonnet-4-6")
        self.assertEqual(out["maxTurns"], 12)
        for dropped in ("mode", "temperature", "tools", "claude", "opencode", "steps"):
            self.assertNotIn(dropped, out)

    def test_model_alias_passthrough(self):
        out = ta.to_claude("a", {"description": "d", "model": "haiku"})
        self.assertEqual(out["model"], "haiku")

    def test_non_anthropic_model_dropped(self):
        out = ta.to_claude("a", {"description": "d", "model": "ollama/llama3.1"})
        self.assertNotIn("model", out)

    def test_disable_skips_agent(self):
        self.assertIsNone(ta.to_claude("a", {"description": "d", "disable": True}))

    def test_enabled_tools_do_not_restrict(self):
        out = ta.to_claude("a", {"description": "d", "tools": {"bash": True}})
        self.assertNotIn("disallowedTools", out)


class OpencodeEmitterTests(unittest.TestCase):
    def setUp(self):
        self.meta, _ = ta.split_frontmatter(UNIFIED)

    def test_basic_translation(self):
        out = ta.to_opencode("reviewer", self.meta)
        self.assertEqual(out["mode"], "subagent")
        self.assertEqual(out["temperature"], 0.1)
        self.assertEqual(out["model"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(out["tools"], {"write": False, "edit": False, "bash": False})
        self.assertEqual(out["steps"], 8)
        self.assertNotIn("claude", out)

    def test_alias_model_dropped(self):
        out = ta.to_opencode("a", {"description": "d", "model": "sonnet"})
        self.assertNotIn("model", out)

    def test_idempotent(self):
        once = ta.to_opencode("reviewer", self.meta)
        twice = ta.to_opencode("reviewer", once)
        self.assertEqual(once, twice)


class MainTests(unittest.TestCase):
    def test_overlay_precedence_and_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, "shared")
            overlay = os.path.join(tmp, "overlay")
            dest = os.path.join(tmp, "dest")
            os.makedirs(shared)
            os.makedirs(overlay)
            with open(os.path.join(shared, "a.md"), "w") as f:
                f.write("---\ndescription: shared\n---\n\nbody\n")
            with open(os.path.join(overlay, "a.md"), "w") as f:
                f.write(UNIFIED)

            rc = ta.main(["claude", dest, shared, overlay, os.path.join(tmp, "missing"), ""])
            self.assertEqual(rc, 0)
            with open(os.path.join(dest, "a.md")) as f:
                meta, body = ta.split_frontmatter(f.read())
            self.assertEqual(meta["description"], "Reviews code for defects.")
            self.assertEqual(body, "You are the reviewer agent.\n")

            # OpenCode-style in-place translation (src == dest) is stable.
            with open(os.path.join(dest, "a.md"), "w") as f:
                f.write(UNIFIED)
            for _ in range(2):
                rc = ta.main(["opencode", dest, dest])
                self.assertEqual(rc, 0)
            with open(os.path.join(dest, "a.md")) as f:
                meta, _ = ta.split_frontmatter(f.read())
            self.assertNotIn("claude", meta)
            self.assertEqual(meta["steps"], 8)
            self.assertEqual(meta["tools"], {"write": False, "edit": False, "bash": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
