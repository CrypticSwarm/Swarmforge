---
description: Create or update a slash command prompt
agent: build
model: openai/gpt-5.2
---
Activate the general-software-engineering skill before taking any other action.

Command request:
$ARGUMENTS

Expected arguments:
- Action: `create`, `update`, or `upsert`
- Optional scope: `global` or `local` (default: `global`)
- Command name (kebab-case, without leading `/` and without `.md`)
- What the command should do (1–3 sentences)

If any of these are missing or ambiguous, ask follow-up questions and stop.

Current working tree:
!`git status --short`

Existing global commands (if present):
!`ls opencode/config/command 2>/dev/null || ls ~/.config/opencode/command 2>/dev/null || true`

Existing local commands (if present):
!`test -d .opencode/command && ls .opencode/command || true`

Locate the target command prompt file based on scope:
- Global: `<global-config>/command/<command-name>.md` where `<global-config>` is `opencode/config` if present in the workspace, otherwise `~/.config/opencode`
- Local: `.opencode/command/<command-name>.md` (create the `.opencode/command/` directory if needed)

Behavior:
- `create`: If the file already exists, ask whether to `update` it instead and stop.
- `update`: If the file does not exist, ask whether to `create` it (or confirm the correct name/scope) and stop.
- `upsert`: If the file exists, update it; otherwise, create it.

When creating a new command prompt file:
- Include YAML frontmatter with `description`, `agent`, and `model` (no extra keys).
- Prefer `agent: build` unless the command is purely advisory.
- Keep the body short and deterministic: validate `$ARGUMENTS`, then provide clear instructions.
- Inject only small, high-signal `!` context blocks (for example: `git status --short`, `git diff`, `ls`, `rg`).

When updating an existing command prompt file:
- Make minimal, focused edits consistent with the surrounding style.
- Preserve any useful context blocks unless they are clearly stale or noisy.
