# Grok Build CLI

`make run_grok` starts a [Grok Build](https://x.ai/news/grok-build-cli) container with the same workspace, git-worktree, and repo-slug mounting as `make run_claude`.
The image installs the official xAI CLI via `curl -fsSL https://x.ai/cli/install.sh | bash` and relocates the binary to `/usr/local/bin/grok`.
Grok state persists by mounting `$(GROK_HOME_DIR)` to `/home/anvil`, keeping `~/.grok/` (account and session files such as `config.toml` and the credentialed user-settings JSON).
`GROK_HOME_DIR` defaults to `$(GROK_DATA_DIR)/home`; use separate `GROK_DATA_DIR` roots to isolate work/personal logins, as with `CLAUDE_DATA_DIR`.

Grok reads the repo-root `AGENTS.md` family natively from the git root down, so it picks up this repo's instructions with no extra config.
Shared skills reach `~/.grok/skills/`, Grok's native location, through the [asset pipeline](../configuration.md).
Subagent definitions are not translated for Grok; the unified-agent pipeline covers OpenCode, Claude, and Codex.
MCP tongs reach Grok as `[mcp_servers.<name>]` entries in a managed block of the merged `~/.grok/config.toml` — user-level config, so no folder-trust prompt. That file is in the persistent home, so the block is rewritten every run and stripped when a session has no MCP tongs; a server the user already defines under the same name wins over the generated entry.

Grok config layering uses the same three sources and order of trust as [Claude](claude-code.md#claude-config-layering) (lowest to highest precedence):
- `SWARMFORGE_REPO_CONFIG_DIR` (default `grok/`, if present)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.grok`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.grok` when that root is set)

These merge into `~/.grok` in the container at startup, with reset disabled so credentials survive the run. Rebuild only the Grok install layer with `make update_grok`.
