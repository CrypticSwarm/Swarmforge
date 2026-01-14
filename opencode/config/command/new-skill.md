---
description: Create a new skill package (global or local)
agent: build
model: openai/gpt-5.2
---
Activate the skill-writer skill before taking any other action.

New skill request:
$ARGUMENTS

Expected arguments:
- Scope: `global` or `local`
- Skill name (kebab-case directory name)
- What the skill should do (1–3 sentences)

If any of these are missing or ambiguous, ask follow-up questions and stop.

Current working tree:
!`git status --short`

Existing global skills (if present):
!`ls opencode/config/skills 2>/dev/null || true`

Existing local skills (if present):
!`test -d .opencode/skills && ls .opencode/skills || true`

Create the skill in the appropriate location:
- If scope is `global`, write to `opencode/config/skills/<skill-name>/SKILL.md`.
- If scope is `local`, write to `.opencode/skills/<skill-name>/SKILL.md`.

Ensure `SKILL.md` frontmatter contains only `name` and `description`.
Use `description` as an activation CTA ("MUST be activated when…").

After the skill content is drafted, determine whether a slash command should also be created for it.
- If scope is `global`, commands live in `opencode/config/command/`.
- If scope is `local`, commands live in `.opencode/command/`.

If creating a command, confirm the desired command name and argument shape, then create `<command-name>.md` that:
1) activates the new skill first,
2) validates `$ARGUMENTS` (ask and stop if missing), and
3) injects only small, high-signal `!` context blocks.
