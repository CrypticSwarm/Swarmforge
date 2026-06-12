---
description: Reflect on the current conversation to improve skills
agent: build
---
Activate the skill-reflection skill before taking any other action.

Arguments:
$ARGUMENTS

This command takes no arguments. If any arguments are provided, ask the user to re-run `/reflection` with no arguments and stop.

Current working tree:
!`git status --short`

Available global skills (if present):
!`ls skills 2>/dev/null || ls ~/.config/opencode/skills 2>/dev/null || true`

Available local skills (if present):
!`test -d .opencode/skills && ls .opencode/skills || true`

Available commands (if present):
!`ls commands 2>/dev/null || ls ~/.config/opencode/command 2>/dev/null || true`

Reflect on the current conversation following the skill-reflection output contract.
