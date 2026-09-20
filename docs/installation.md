# Installation

The three install steps are in the [repo README](../README.md); this page carries the detail behind them.

## The install script

It warns if `~/.local/bin` is not on your `PATH`.
Run it with `bash` (it uses Bash arrays) even if your login shell is Zsh.
On macOS it prefers `~/.zshrc` and falls back to `~/.bash_profile`, so you don't need to create `~/.bashrc` manually.
Override the target file with `OC_RC_FILE=/path/to/rc bash ./install.sh`.

## Building the images

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

The four harness images share the same Debian base and toolchain (Node.js + Python; see `anvil/Dockerfile`).
All four build the same `harness-runtime` stage, selected by `--build-arg AGENT=opencode|claude|grok|codex`,
which runs that harness's install step alone. A hand-run `docker build` names that target and a matching `AGENT`.

## Repo-local env vars

The `run_*` harness targets load a repo-local env file from `.swarmforge/env` if it exists; override with `ENV_FILE=/path/to/env`.
They also accept `TIMEZONE=<Region/City>` (default `Etc/UTC`), passed into the container as `TZ`.

## Multiple aliases (work/personal)

Define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys):

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

- `GITCONFIG_FILE` points at an agent-specific git config instead of `~/.gitconfig`.
- For Claude Code, use separate `CLAUDE_DATA_DIR` roots to isolate work/personal logins and session state. `CLAUDE_HOME_DIR` defaults to `$(CLAUDE_DATA_DIR)/home`.
- Config layering uses `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR` (their defaults differ per harness — see [OpenCode layering](harnesses/opencode.md) and [Claude config layering](harnesses/claude-code.md#claude-config-layering)). Set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` to resolve org defaults to each harness's own directory under that root (`.opencode`, `.claude`, `.grok`, `.codex`).

`SWARMFORGE_REPO_CONFIG_DIR` refers to the Swarmforge checkout (the harness repo), not the working project mounted at `/workspace`.
By default each `run_*` target points it at that harness's directory in the checkout: `$(SWARMFORGE_DIR)/opencode`, and `$(SWARMFORGE_DIR)/claude`, `/grok`, `/codex` if present.
Project-local config in the working repo (for example `.opencode/`) is still handled by the agent tools themselves.
