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


class WholeLineCommentTests(unittest.TestCase):
    """A whole-line comment carries no structure, so it may sit anywhere."""

    def parse(self, text):
        lines = text.split("\n")
        out, index = ta.parse_map(lines, 0, 0)
        # An index short of the end would mean a comment swallowed real content.
        self.assertEqual(index, len(lines))
        return out

    def test_comment_above_first_list_item(self):
        # The comment must not hide the `- ` that makes this block a list.
        self.assertEqual(
            self.parse("mounts:\n  # why this mount\n  - docker-socket\n"),
            {"mounts": ["docker-socket"]},
        )

    def test_comment_between_list_items(self):
        self.assertEqual(
            self.parse("mounts:\n  - a\n  # about b\n  - b\n"),
            {"mounts": ["a", "b"]},
        )

    def test_comment_indent_is_ignored_inside_a_list(self):
        # Neither a shallower nor a deeper comment ends the list.
        self.assertEqual(
            self.parse("mounts:\n  - a\n# flush left\n  - b\n"),
            {"mounts": ["a", "b"]},
        )
        self.assertEqual(
            self.parse("mounts:\n  - a\n        # deep\n  - b\n"),
            {"mounts": ["a", "b"]},
        )

    def test_comment_indent_does_not_set_the_block_indent(self):
        # The block's indent comes from `kind:`, not from the deeper comment.
        self.assertEqual(
            self.parse("interface:\n      # deeply indented\n  kind: mcp\n"),
            {"interface": {"kind": "mcp"}},
        )

    def test_comment_between_a_list_and_the_next_key(self):
        self.assertEqual(
            self.parse("mounts:\n  - a\n# about the next key\nmode: subagent\n"),
            {"mounts": ["a"], "mode": "subagent"},
        )

    def test_comment_is_the_last_line(self):
        self.assertEqual(
            self.parse("mounts:\n  - a\n# nothing follows"),
            {"mounts": ["a"]},
        )

    def test_block_holding_only_a_comment_is_empty(self):
        self.assertEqual(
            self.parse("readiness:\n  # decide this later\nmode: subagent\n"),
            {"readiness": None, "mode": "subagent"},
        )

    def test_comment_closing_the_frontmatter(self):
        meta, body = ta.split_frontmatter(
            "---\ndescription: Reviews code.\n# nothing below here\n---\n\nbody\n"
        )
        self.assertEqual(meta, {"description": "Reviews code."})
        self.assertEqual(body, "body\n")


class InlineCommentTests(unittest.TestCase):
    """A `#` after whitespace ends the value, as it does in YAML."""

    def parse(self, text):
        out, _ = ta.parse_map(text.split("\n"), 0, 0)
        return out

    def test_trailing_comment_is_dropped_from_a_value(self):
        self.assertEqual(
            self.parse("description: Reviews code.  # only Python\n"),
            {"description": "Reviews code."},
        )

    def test_key_trailed_only_by_a_comment_still_opens_its_block(self):
        self.assertEqual(
            self.parse("mounts:  # opt-in words only\n  - workspace:ro\n"),
            {"mounts": ["workspace:ro"]},
        )

    def test_trailing_comment_is_dropped_from_a_list_item(self):
        self.assertEqual(
            self.parse("mounts:\n  - docker-socket  # drives the host daemon\n"),
            {"mounts": ["docker-socket"]},
        )

    def test_hash_without_leading_whitespace_stays_in_the_value(self):
        # Image digests and URL fragments carry a bare `#` mid-value.
        self.assertEqual(
            self.parse("image: ghcr.io/example/tong@sha256:ab#cd\n"),
            {"image": "ghcr.io/example/tong@sha256:ab#cd"},
        )

    def test_hash_inside_quotes_stays_in_the_value(self):
        self.assertEqual(
            self.parse('description: "sizes # and shapes"  # a note\n'),
            {"description": "sizes # and shapes"},
        )

    def test_comment_after_a_flow_list(self):
        self.assertEqual(
            self.parse('command: ["test", "-S", "/run/agent.sock"]  # probe\n'),
            {"command": ["test", "-S", "/run/agent.sock"]},
        )

    def test_typed_scalars_survive_their_comments(self):
        self.assertEqual(
            self.parse("port: 8080  # inside the container\nedit: false  # read only\n"),
            {"port": 8080, "edit": False},
        )

    def test_apostrophe_in_prose_does_not_open_a_quoted_run(self):
        self.assertEqual(
            self.parse("description: don't split it  # note\n"),
            {"description": "don't split it"},
        )

    def test_single_quoted_value_keeps_its_hash(self):
        self.assertEqual(
            self.parse("description: 'sizes # and shapes'  # a note\n"),
            {"description": "sizes # and shapes"},
        )

    def test_value_that_is_only_a_comment_is_null(self):
        self.assertEqual(self.parse("description: # decide later\n"), {"description": None})

    def test_list_item_that_is_only_a_comment_is_null(self):
        self.assertEqual(self.parse("mounts:\n  - # decide later\n"), {"mounts": [None]})


class StripInlineCommentTests(unittest.TestCase):
    """The quote/`#` scan on its own, including shapes the emitter produces."""

    def test_escaped_quote_does_not_end_the_quoted_run(self):
        # emit_scalar writes values through json.dumps, so `\\"` is the escaping
        # this parser must read back. Miscounting it truncates the value.
        text = r'"a \" b # c"'
        self.assertEqual(ta.strip_inline_comment(text), text)

    def test_doubled_quote_does_not_end_the_quoted_run(self):
        # The single-quoted counterpart of the escape above.
        self.assertEqual(
            ta.strip_inline_comment("'don''t # x'  # note"), "'don''t # x'  "
        )

    def test_bracket_or_comma_in_prose_is_not_a_flow_list(self):
        self.assertEqual(
            ta.strip_inline_comment("fixes [WIP], mostly  # note"), "fixes [WIP], mostly  "
        )

    def test_quote_only_delimits_at_the_start_of_a_value(self):
        self.assertEqual(ta.strip_inline_comment("it's here  # note"), "it's here  ")
        self.assertEqual(ta.strip_inline_comment('"it # stays"  # note'), '"it # stays"  ')

    def test_quoted_hash_inside_a_flow_item(self):
        # Each `[` and `,` opens a fresh value, so the item's quotes count.
        self.assertEqual(ta.strip_inline_comment('["a # b", c]  # note'), '["a # b", c]  ')

    def test_hash_needs_whitespace_before_it(self):
        self.assertEqual(ta.strip_inline_comment("x@sha256:ab#cd"), "x@sha256:ab#cd")
        self.assertEqual(ta.strip_inline_comment("#everything"), "")

    def test_unterminated_quote_keeps_the_value(self):
        # Cut nothing: a missed comment beats a value truncated at the `#`.
        self.assertEqual(ta.strip_inline_comment('"unclosed # x'), '"unclosed # x')


COMMENTED_AGENT = """---
# a reviewer, not an author
description: Reviews code.  # Python only
mode: subagent
model: anthropic/claude-sonnet-4-6  # pinned
tools:                      # everything not listed stays enabled
  # writing is off for review agents
  write: false
  edit: false
---

You are the reviewer agent.
"""


class CommentedAgentTests(unittest.TestCase):
    def test_frontmatter_parses_past_both_comment_styles(self):
        meta, body = ta.split_frontmatter(COMMENTED_AGENT)
        self.assertEqual(meta["description"], "Reviews code.")
        self.assertEqual(meta["mode"], "subagent")
        self.assertEqual(meta["model"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(meta["tools"], {"write": False, "edit": False})
        self.assertEqual(body, "You are the reviewer agent.\n")

    def test_rewrite_drops_comments_and_is_stable(self):
        # The in-place opencode translation rewrites a source file, so dropping
        # the comments has to leave something that holds still.
        meta, body = ta.split_frontmatter(COMMENTED_AGENT)
        rendered = ta.render(meta, body)
        self.assertNotIn("Python only", rendered)
        self.assertNotIn("a reviewer, not an author", rendered)
        again, body2 = ta.split_frontmatter(rendered)
        self.assertEqual(meta, again)
        self.assertEqual(body, body2)

    def test_a_value_holding_a_hash_survives_the_rewrite(self):
        # emit_scalar quotes what a re-parse could mistake for a comment.
        meta = {"description": "counts # of files", "model": "img@sha256:ab#cd"}
        again, _ = ta.split_frontmatter(ta.render(meta, "body\n"))
        self.assertEqual(again, meta)


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
