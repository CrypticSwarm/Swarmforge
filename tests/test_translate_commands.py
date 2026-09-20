import contextlib
import io
import os
import tempfile
import unittest

from swarmforge.harness.codex.commands import main


class CodexCommandTranslation(unittest.TestCase):
    def translate(self, text):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "commands")
            dest = os.path.join(root, "skills")
            os.makedirs(source)
            with open(os.path.join(source, "review.md"), "w") as handle:
                handle.write(text)
            self.assertEqual(main([dest, source]), 0)
            with open(os.path.join(dest, "review", "SKILL.md")) as handle:
                result = handle.read()
        return result

    def test_translates_portable_command_to_codex_skill(self):
        result = self.translate(
            "---\ndescription: Inspect a path\nagent: build\n---\n"
            "Request: $ARGUMENTS\nContents: !`ls $1`\n"
        )
        self.assertTrue(result.startswith("---\nname: review\ndescription: Inspect a path\n---"))
        self.assertNotIn("agent:", result)
        self.assertNotIn("!`", result)
        self.assertIn("the arguments supplied with this skill invocation", result)
        self.assertIn(
            "Run `ls $1`, replacing $1 with the corresponding "
            "positional invocation argument and use its output.",
            result,
        )

    def test_two_positionals_are_listed_in_order_and_described_in_the_plural(self):
        result = self.translate(
            "---\ndescription: Compare two paths\n---\n"
            "Difference: !`diff $2 $1`\n"
        )
        self.assertIn(
            "Run `diff $2 $1`, replacing $1, $2 with the corresponding "
            "positional invocation arguments and use its output.",
            result,
        )


class CodexCommandWarnings(unittest.TestCase):
    """What a skipped command writes to stderr.

    The warning is the only report a command gets that it produced no skill,
    so it has to say which module dropped it -- these assert the whole line,
    prefix included.
    """

    def skip_warning(self, text):
        """Run the translator over a command it must skip; return its path and stderr."""
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "commands")
            dest = os.path.join(root, "skills")
            os.makedirs(source)
            path = os.path.join(source, "review.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            with contextlib.redirect_stderr(stderr):
                status = main([dest, source])
            self.assertEqual(status, 0)
            self.assertFalse(os.path.exists(os.path.join(dest, "review")))
        return path, stderr.getvalue()

    def test_a_command_without_a_description_is_skipped_under_this_module_name(self):
        path, stderr = self.skip_warning(
            "---\nagent: build\n---\nRequest: $ARGUMENTS\n"
        )
        self.assertEqual(
            stderr,
            "swarmforge.harness.codex.commands: "
            "skipping %s: command has no description\n" % path,
        )

    def test_unterminated_frontmatter_is_skipped_under_this_module_name(self):
        path, stderr = self.skip_warning(
            "---\ndescription: Inspect a path\nRequest: $ARGUMENTS\n"
        )
        self.assertEqual(
            stderr,
            "swarmforge.harness.codex.commands: "
            "skipping %s: unterminated frontmatter\n" % path,
        )


if __name__ == "__main__":
    unittest.main()
