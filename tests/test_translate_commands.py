import os
import tempfile
import unittest

from swarmforge.commands.translate import main


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
        self.assertIn("replacing $1 with the corresponding positional", result)


if __name__ == "__main__":
    unittest.main()
