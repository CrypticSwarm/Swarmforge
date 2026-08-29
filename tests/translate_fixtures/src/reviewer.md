---
description: Reviews code for defects.
mode: subagent
temperature: 0.1
model: anthropic/claude-sonnet-4-6
tools:
  write: false
  edit: false
  bash: false
  patch: false
  webfetch: true
metadata:
  team: search
  tags:
    - review
    - python
claude:
  maxTurns: 12
opencode:
  steps: 8
codex:
  model_reasoning_effort: high
  sandbox_mode: read-only
---

You are the reviewer agent.
