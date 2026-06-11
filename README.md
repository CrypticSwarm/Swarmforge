# Swarmforge

**Swarmforge: The foundation for forging robust systems and dependable tools**

Swarmforge is a builder-focused environment for designing, refining, and reusing processes and the tools they produce.
It emphasizes robustness, constraint-driven design, and interoperability over ad-hoc interaction or one-off execution.

## Installation

1. Add the shell helper alias:

```bash
bash ./install.sh
```

This appends an `oc` alias to your shell rc file so it shells out to `make -C <repo> run_opencode PROJECT_DIR=$(pwd)`.
The installer is a Bash script (uses Bash arrays), so run it with `bash` even if your login shell is Zsh.
On macOS the installer prefers `~/.zshrc` (the default since Catalina) and falls back to `~/.bash_profile` for legacy Bash shells, so you do not need to create `~/.bashrc` manually.
Because macOS login shells source `.bash_profile` before `.bashrc`, keeping the alias in whichever file the installer chose ensures it loads during Terminal launches.
Override the target explicitly by running `OC_RC_FILE=/path/to/rc bash ./install.sh`.

2. Build one or both container images:

```
make build_opencode
make build_claude
```

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

Both images share the same Debian base and toolchain (Node.js + Python; see `opencode/Dockerfile` for configured versions).
Build targets pass `AGENT=opencode|claude` so only the requested agent install step runs (works with both legacy Docker builder and BuildKit).

3. Run from your project directory:

- OpenCode: `oc`
- Claude Code: `make run_claude PROJECT_DIR=$(pwd)`
- Pass OpenCode overrides either as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).
- Override container timezone per run (affects git commit timestamps): `oc TIMEZONE=America/New_York` or `make run_claude PROJECT_DIR=$(pwd) TIMEZONE=America/New_York`.

### Repo-local env vars

`make run_opencode` loads a repo-local env file if it exists at `.swarmforge/env`.
Override with `ENV_FILE=/path/to/env make run_opencode`.

`make run_claude` uses the same repo-local env file path.
Override with `ENV_FILE=/path/to/env make run_claude PROJECT_DIR=$(pwd)`.

Both `make run_opencode` and `make run_claude` support `TIMEZONE=<Region/City>` (default: `Etc/UTC`) and pass it as `TZ` into the container.

### Multiple aliases (work/personal)

You can define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys).

Example:

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

`GITCONFIG_FILE` is useful if you keep an agent-specific git config rather than using your default `~/.gitconfig`.
For Claude Code, use separate `CLAUDE_DATA_DIR` roots to isolate work/personal logins and session state.
`CLAUDE_HOME_DIR` defaults to `$(CLAUDE_DATA_DIR)/home`, and can be overridden directly if needed.
Config layering is controlled with shared variables: `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR`.
If your org config repo has both agent layouts, you can set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` and defaults resolve to:
- OpenCode: `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode`
- Claude: `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude`

Important: `SWARMFORGE_REPO_CONFIG_DIR` refers to the Swarmforge checkout (the harness repo), not the active working project mounted at `/workspace`.
By default this means:
- `run_opencode` uses `$(SWARMFORGE_DIR)/opencode/config`
- `run_claude` uses `$(SWARMFORGE_DIR)/opencode/config/claude` (if present)

Project-local config in the working repo is still handled by the agent tools themselves (for example `.opencode/`), independent of this Swarmforge layering.

### Git repos and worktrees

`make run_opencode` auto-detects the git root from `PROJECT_DIR` and mounts that root at `/workspace`.
If the target is a linked git worktree, it also mounts the shared git common directory so git operations keep working inside the container.

This means `oc` works from repo roots, subdirectories, and linked worktrees without extra flags.

## Ollama

Run an LLMs locally.

## OpenCode

Test harness that has a standard set of tools exposed to LLM geared at editing code.

## Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting behavior as `make run_opencode`.
Claude state is persisted by mounting `$(CLAUDE_HOME_DIR)` to `/home/opencode`, which keeps account/session files such as `~/.claude/` and `~/.claude.json`.
By default it mounts the repo to a stable path derived from the git remote slug and starts Claude there (while still mounting `/workspace` for compatibility), which improves session grouping across worktrees without relying on host-specific absolute paths.

Session compatibility notes:

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- GitHub remote slugs map to deterministic paths, for example `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`.
- Override slug detection with `CLAUDE_REPO_SLUG=crypticswarm/Swarmforge` and remote selection with `CLAUDE_REMOTE_NAME=<remote>`.

It also mounts shared Swarmforge assets so both runtimes can access common resources:

- `opencode/config/skills/` -> `/home/opencode/.swarmforge/skills`
- `opencode/config/command/` -> `/home/opencode/.swarmforge/command`

Those paths are exported in-container as `SWARMFORGE_SKILLS_DIR` and `SWARMFORGE_COMMAND_DIR`.
When launching Claude Code, the container entrypoint copies these into `~/.claude/skills/` and `~/.claude/commands/` so Claude can discover them as native skills/commands.
In-container `~/.claude/skills/`, `~/.claude/commands/`, and `~/.claude/agents/` are container-private tmpfs mounts that mask the shared persistent `CLAUDE_HOME_DIR`: each container starts them empty and the entrypoint repopulates them, lowest to highest precedence, from the user and org config layers, the harness shared assets, and the current workspace's overlays.
All Swarmforge layers carry these assets in Swarmforge formats — skills and commands are portable and copied as-is, while agents use the unified format and are translated. Claude-native repo-local definitions (for example `<workspace>/.claude/agents/`) are still discovered by Claude itself, outside the Swarmforge pipeline.
This keeps per-repo skills, commands, and agents from accumulating in the persistent home and leaking into other repos' sessions; the layered config merge skips those three directories for the Claude agent for the same reason.

You can ship project-local skills/commands in your workspace and have them overlay the harness defaults: the entrypoint reads `<workspace>/.agents/skills/` and `<workspace>/.agents/commands/`, and workspace entries override harness entries with the same name.
Harness-native repo-local dirs (such as `<workspace>/.opencode/`) belong to their own harness and are not consumed for Claude.

Claude config layering uses three sources (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.claude` for `run_claude`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude` when `SWARMFORGE_ORG_CONFIG_ROOT` is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default `opencode/config/claude` for `run_claude`, if present)

At startup these are merged into `~/.claude` inside the container, so personal defaults can be overlaid with org settings and repo-local overrides.

## Agents

Subagent definitions are stored under `opencode/config/agents/` in a single unified format and rewritten to each harness's native dialect by the container entrypoint (`opencode/translate_agents.py`), the same way `.claude` assets are populated for Claude Code.

A unified agent is a markdown file whose body is the agent's system prompt and whose YAML frontmatter is a superset of the OpenCode agent schema. The filename is the agent's identity (`reviewer.md` -> agent `reviewer`):

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
---

You are the reviewer agent...
```

Field handling per harness:

- `description` and the prompt body pass through everywhere.
- `tools` uses OpenCode's lowercase tool ids mapped to booleans. For Claude Code, disabled tools become `disallowedTools` (`write: false` -> `disallowedTools: Write`); ids without a Claude equivalent are dropped.
- `model` accepts a provider-qualified id (`anthropic/claude-sonnet-4-6`) which passes through to OpenCode and is stripped to the bare id for Claude Code (non-Anthropic providers are dropped), or a Claude alias (`sonnet`, `haiku`) which is Claude-only and dropped for OpenCode.
- `mode`, `temperature`, and other OpenCode-only fields are dropped for Claude Code.
- `claude:` / `opencode:` blocks merge verbatim into that harness's output frontmatter, for anything the unified fields don't cover.
- `disable: true` passes through to OpenCode and skips emitting the agent for Claude Code.

Every Swarmforge layer — user, org, and repo config dirs plus the workspace overlay — carries agents in this unified format. Harness-native definitions belong in the harness's own repo-local directories (`<workspace>/.claude/agents/`, `<workspace>/.opencode/agents/`), which each harness discovers directly without Swarmforge involvement.

How the definitions reach each harness:

- OpenCode: the layered config merge lands every layer's `agents/` in `~/.config/opencode/agents`, where the entrypoint translates the files in place (translation is idempotent, so re-running is harmless).
- Claude Code: user- and org-layer `agents/` dirs, then `opencode/config/agents/` (mounted read-only at `/home/opencode/.swarmforge/agents`, exported as `SWARMFORGE_AGENTS_DIR`), are translated into `~/.claude/agents/`, a container-private tmpfs scoped to the current repo.

Workspace overlays follow the skills/commands convention: `<workspace>/.agents/agents/` holds unified definitions for every harness and overrides lower layers with the same filename.

Run the translator's tests with `python3 scripts/test_translate_agents.py`.

## Commands

Slash commands are stored under `opencode/config/command/` (and optionally `.opencode/command/` for repo-local commands).
To run one, start your prompt with the command name (for example `/commit` will inject [`opencode/config/command/commit.md`](opencode/config/command/commit.md)).

Command prompt files often include `!` shell-expansion blocks, for example:

```
!`git status --short`
```

OpenCode runs these shell commands and injects their output into the prompt context, so the agent sees the live repo state without you copy/pasting it.

## Skills

Skills are stored under `opencode/config/skills/`.

When you run OpenCode via `make run_opencode`, config is layered from three sources (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.config/opencode` for `run_opencode`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode` when `SWARMFORGE_ORG_CONFIG_ROOT` is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default repo-local `opencode/config` for `run_opencode`)

At container startup these layers are merged into `/home/opencode/.config/opencode`, so Swarmforge-layer files can override org files, and org files can override personal defaults.
This keeps skills in `opencode/config/skills/` exposed by default while still allowing personal and org-specific overlays.

For `opencode.json`, layers are merged by key (not plain file overwrite), which lets org-level MCP servers in `.opencode/opencode.json` remain available even when the Swarmforge repo layer also defines `opencode.json`.

You can also define MCP servers directly in a project-local `.opencode/opencode.json` file. Example:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "org-server": {
      "type": "remote",
      "url": "https://mcp.example.com",
      "enabled": true
    }
  }
}
```

That is often the cleanest place for OpenCode MCP definitions when you want them attached to a specific repo.

OpenCode auto-discovers skills from that directory and uses only the YAML frontmatter (`name` + `description`) for discovery.
The full `SKILL.md` body is loaded on-demand when a skill is invoked, which helps keep the default context small.

When `make run_opencode` starts the container, it now mounts your host `~/.gitconfig` into `/home/opencode/.gitconfig` if the file exists so agents inherit your configured `user.name` and `user.email`.
Point to an alternative identity file with `GITCONFIG_FILE=/path/to/gitconfig make run_opencode`.

Note: `opencode/config/opencode.json` also supports an `instructions` array for global instruction files.
Those files are loaded in full, so avoid listing full `SKILL.md` files there unless you explicitly want them always in context.

## Skill Tests

This repo includes a lightweight skill test harness.
It runs scenario prompts against a chosen model and verifies expected behavior.

- Run all skill tests: `make test MODEL=<provider/model>`
- Run a single skill's tests: `make test MODEL=<provider/model> TEST_SKILL=<skill-name>`
- Optional judge mode: `make test MODEL=<student> TEST_ENABLE_JUDGE=1 EVAL_MODEL=<judge>`
- Timeout override: `make test MODEL=<provider/model> TEST_TIMEOUT_S=<seconds>`

Tests live in `opencode/config/skills/<skill-name>/tests/*.json`.
The runner is `scripts/test_skills.py`.

Assertions can be:

- Output patterns: `expect.must_match` and `expect.must_not_match` (regex against formatted output)
- Tool calls: `expect.must_tool` and `expect.must_not_tool` (extracted from `opencode run --format json` events)
