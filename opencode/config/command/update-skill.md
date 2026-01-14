---
description: Update an existing skill package
agent: build
model: openai/gpt-5.2
---
Activate the skill-writer skill before taking any other action.

Update request:
$ARGUMENTS

Expected arguments:
- Skill name (or explicit path)
- Requested change(s)
- Optional scope hint: `global` or `local`

If the target skill is not clearly identifiable from the arguments, ask for the exact skill name and whether it is global or local, then stop.

Current working tree:
!`git status --short`

Available global skills (if present):
!`ls opencode/config/skills 2>/dev/null || true`

Available local skills (if present):
!`test -d .opencode/skills && ls .opencode/skills || true`

Locate the target skill directory and apply the requested changes.
- Global skills live in `opencode/config/skills/<skill-name>/SKILL.md`.
- Local skills live in `.opencode/skills/<skill-name>/SKILL.md`.

Constraints:
- Keep `SKILL.md` YAML frontmatter to `name` and `description` only.
- Make minimal, focused edits consistent with the repo style.

If the change implies a new or updated slash command, locate and update the corresponding command prompt:
- Global: `opencode/config/command/<command-name>.md`
- Local: `.opencode/command/<command-name>.md`

If no command exists but one is warranted, ask before creating it (confirm command name + expected `$ARGUMENTS`).
