# Codex CLI

`make run_codex` starts an [OpenAI Codex CLI](https://developers.openai.com/codex/cli) container with the same workspace, git-worktree, and repo-slug mounting as `make run_claude`.
The image installs the official CLI via `curl -fsSL https://chatgpt.com/codex/install.sh | sh`.
That release is a package rather than a lone binary -- `bin/codex` resolves ripgrep, `bwrap`, and a bundled zsh beside itself -- so it stays whole under `/opt/codex` and the installer's symlink is what lands on `PATH`.
Codex state persists by mounting `$(CODEX_HOME_DIR)` to `/home/anvil`, keeping credentials, sessions, and the project trust levels a stable mount path keeps valid.
`CODEX_HOME_DIR` defaults to `$(CODEX_DATA_DIR)/home`; use separate `CODEX_DATA_DIR` roots to isolate work/personal logins, as with `CLAUDE_DATA_DIR`.

Codex reads the repo-root `AGENTS.md` family natively from the git root down, so it picks up this repo's instructions with no extra config.
Shared skills reach `~/.agents/skills/`, Codex's native user location, through the [asset pipeline](../configuration.md#asset-layers). Portable commands reach the same location as translated skills.
Unified subagent definitions become temporary Codex role files under
`/run/swarmforge/codex-agents/` and are registered through the derived
`~/.codex/config.toml`. The checkout's native `.codex/agents/` is untouched.
MCP tongs reach Codex as `[mcp_servers.<name>]` entries in a managed block of the derived `~/.codex/config.toml`, rewritten from the current layers every run and yielding to a server the user already defines under that name.

Each launch builds `config.toml` from scratch across the three
[config layers](../configuration.md#config-layers), merging by key, and copies
it to Codex's native path. The canonical output preserves values and tables,
but not comments or formatting. The native file remains writable for Codex's
atomic settings updates, but the next launch rebuilds it; put durable settings
in a source layer. Rebuild only the Codex install layer with
`make update_codex`.
The merge skips `packages/` -- the host installer's release tree, which the container has no use for -- along with `sessions/`, `history.jsonl`, and `log/`, so one machine's transcripts do not follow the user config layer into the container's home.

Codex brings its own sandbox, which is redundant inside an anvil and may not initialize in one at all, since its Landlock and `bwrap` paths need kernel permissions a container is not guaranteed.
Relax it per run with `CODEX_ARGS='--dangerously-bypass-approvals-and-sandbox'`, or per install by setting `sandbox_mode` in a config layer.
