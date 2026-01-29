---
description: Delete code with thorough cleanup
agent: build
model: openai/gpt-5.2-codex
---
Activate the code-deletion-cleanup skill before taking any other action.

Deletion target:
$ARGUMENTS

If no deletion target is provided, ask for the exact symbol, file path, feature, endpoint, job, or command to remove and stop.

Current working tree:
!`git status --short`

Recent commits:
!`git log --oneline -10`

Follow the code-deletion-cleanup skill exactly.
Use `todowrite` as the canonical deletion queue.
It is acceptable to have multiple `in_progress` todos while subagents run in parallel.
At the end of each iteration, report what remains in the queue.
