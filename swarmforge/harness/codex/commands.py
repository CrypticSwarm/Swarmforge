#!/usr/bin/env python3
"""Translate portable slash commands into Codex skill packages.

Usage: python3 -m swarmforge.commands.translate <dest_dir> <src_dir>
"""

import os
import re
import shutil
import sys

from swarmforge.agents.emit import split_frontmatter


SHELL_INTERPOLATION_RE = re.compile(r"!`([^`\n]+)`")
POSITIONAL_RE = re.compile(r"\$(\d+)")


def warn(message):
    print("swarmforge.commands.translate: %s" % message, file=sys.stderr)


def describe_positionals(command):
    positions = sorted({int(value) for value in POSITIONAL_RE.findall(command)})
    if not positions:
        return ""
    labels = ", ".join("$%d" % position for position in positions)
    return ", replacing %s with the corresponding positional invocation argument%s" % (
        labels,
        "" if len(positions) == 1 else "s",
    )


def translate_body(body):
    body = body.replace(
        "$ARGUMENTS", "the arguments supplied with this skill invocation"
    )

    def shell_instruction(match):
        command = match.group(1)
        return "Run `%s`%s and use its output." % (
            command,
            describe_positionals(command),
        )

    return SHELL_INTERPOLATION_RE.sub(shell_instruction, body)


def translate_file(path, dest_dir):
    filename = os.path.basename(path)
    name = filename[:-3]
    with open(path, "r", encoding="utf-8") as handle:
        meta, body = split_frontmatter(handle.read())
    description = meta.get("description")
    if not description:
        warn("skipping %s: command has no description" % path)
        return
    skill_dir = os.path.join(dest_dir, name)
    if os.path.lexists(skill_dir):
        if os.path.isdir(skill_dir) and not os.path.islink(skill_dir):
            shutil.rmtree(skill_dir)
        else:
            os.unlink(skill_dir)
    os.makedirs(skill_dir)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write(
            "---\nname: %s\ndescription: %s\n---\n\n%s"
            % (name, description, translate_body(body))
        )


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    dest_dir, src_dir = argv
    if not src_dir or not os.path.isdir(src_dir):
        return 0
    os.makedirs(dest_dir, exist_ok=True)
    for filename in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, filename)
        if filename.endswith(".md") and os.path.isfile(path):
            try:
                translate_file(path, dest_dir)
            except ValueError as exc:
                warn("skipping %s: %s" % (path, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
