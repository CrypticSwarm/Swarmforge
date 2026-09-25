# Installation

The three install steps are in the [repo README](../README.md); this page carries the detail behind them.
Every variable below has a row in [Environment variables](reference/environment.md), and every target one in [Make targets](reference/make-targets.md).

## Prerequisites

The host needs Docker with BuildKit, git, GNU make, bash, and Python 3.9 or newer; the launcher uses only the standard library.
On Windows these live inside WSL: see [Windows (WSL)](windows.md).

## The install script

Run it with `bash` (it uses Bash arrays) even if your login shell is Zsh.
On macOS it prefers `~/.zshrc` and falls back to `~/.bash_profile`, so you don't need to create `~/.bashrc` manually.

## Building the images

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

The four harness images share the same Debian base and toolchain (Node.js + Python; see `anvil/Dockerfile`).

## Multiple aliases (work/personal)

Define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys):

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

- `GITCONFIG_FILE` points at an agent-specific git config instead of `~/.gitconfig`.
- Separate `CLAUDE_DATA_DIR`, `GROK_DATA_DIR`, and `CODEX_DATA_DIR` roots isolate work and personal logins and session state; `DATA_DIR` does the same for OpenCode.
- Config layering uses `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR` (their defaults differ per harness — see [Config layers](configuration.md#config-layers)). Set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` to resolve org defaults to each harness's own directory under that root.
