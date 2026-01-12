---
description: Draft commit message for staged changes
agent: build
model: openai/gpt-5.1-codex
---
Activate the commit-messages skill before taking any other action. If no files are staged, explain the gap and stop.

Currently staged files:
!`git status --short`

Staged diff:
!`git diff --staged`

Use only the staged diff above to produce the commit message following the commit-messages skill.
