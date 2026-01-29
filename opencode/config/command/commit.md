---
description: Draft commit message for staged changes
agent: build
model: openai/gpt-5.2-codex
---
Activate the commit-messages skill before taking any other action. If no files are staged, explain the gap and stop.

Additional context (optional):
$ARGUMENTS

Currently staged files:
!`git status --short`

Staged diff:
!`git diff --staged`

Use the staged diff above as the source of truth. If `Additional context` is provided, use it to clarify intent/rationale or constraints, but do not contradict the diff. Produce the commit message following the commit-messages skill.
