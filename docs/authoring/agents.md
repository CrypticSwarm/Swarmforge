# Agents

Subagent definitions live under `agents/` in a single unified format and are rewritten to each harness's native dialect at container startup, by `swarmforge/agents/translate.py` through the emitter the harness's spec declares.

A unified agent is a markdown file whose body is the system prompt and whose YAML frontmatter is a superset of the OpenCode agent schema. The filename is the agent's identity (`reviewer.md` -> agent `reviewer`):

```markdown
---
description: Reviews code and suggests improvements.
mode: subagent
temperature: 0.1
model: anthropic/claude-sonnet-4-6
tools:
  write: false
  edit: false
  bash: false
claude:
  maxTurns: 12
codex:
  model: gpt-5.3-codex
  model_reasoning_effort: high
  sandbox_mode: read-only
---

You are the reviewer agent...
```

Field handling per harness:

- `description` and the prompt body pass through everywhere.
- `tools` uses OpenCode's lowercase tool ids mapped to booleans. For Claude Code, disabled tools become `disallowedTools` (`write: false` -> `disallowedTools: Write`); ids with no Claude equivalent are dropped.
- `model` accepts a provider-qualified id (`anthropic/claude-sonnet-4-6`, passed through to OpenCode and stripped to the bare id for Claude — non-Anthropic providers dropped) or a Claude alias (`sonnet`, `haiku`, Claude-only and dropped for OpenCode).
- `mode`, `temperature`, and other OpenCode-only fields are dropped for Claude Code.
- For Codex, unqualified models pass through, `openai/` prefixes are stripped, and other providers are dropped. Names and `.toml` filenames are normalized to Codex's supported ASCII characters. Generic `tools` restrictions are dropped; use Codex sandbox and MCP settings instead.
- `claude:`, `codex:`, and `opencode:` blocks merge into that harness's output. Put Codex-only fields such as `model_reasoning_effort` and `sandbox_mode` in `codex:`.
- `disable: true` passes through to OpenCode and skips the agent for Claude Code and Codex.

Unified agents live in the agents dir of each of the four [asset layers](../configuration.md#asset-layers) — harness-neutral `.swarmforge/agents/`, except in the repo layer, where it is the checkout's own `agents/`.

Layers mount read-only under `/tmp/swarmforge-assets/{user,org}` and `/tmp/swarmforge-assets/repo/agents` (the in-container `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` env vars point at the layer roots). Startup translates them into `~/.config/opencode/agents/` for OpenCode, `agents/` inside Claude's container-local config dir (the one `CLAUDE_CONFIG_DIR` names), and temporary registered role files for Codex. Later layers override earlier ones by filename.
Claude-native repo-local definitions (for example `<workspace>/.claude/agents/`) are still discovered by Claude directly, outside this pipeline.

The translator is covered by the unit suite; run it with `make test`.
