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

2. Build the OpenCode container image:

```
make build_opencode
```

The image includes a Debian base plus the toolchain used by the harness (Node.js and Python; see `opencode/Dockerfile` for the currently configured versions).

3. Run from your project directory:

- Basic usage: `oc`
- Pass overrides either as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).

### Multiple aliases (work/personal)

You can define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys).

Example:

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

`GITCONFIG_FILE` is useful if you keep an agent-specific git config rather than using your default `~/.gitconfig`.

### Git repos and `.git` access

`PROJECT_DIR` is what gets mounted into the container. If you run from a subdirectory of a git repo, the container will not see the repo’s `.git/` directory (and git-related workflows like `/commit` will be limited).

To enable git tooling, either run `oc` from the repo root, or set `PROJECT_DIR` to the top-level directory (for example `PROJECT_DIR=$(git rev-parse --show-toplevel)`).

## Ollama

Run an LLMs locally.

## OpenCode

Test harness that has a standard set of tools exposed to LLM geared at editing code.

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

When you run OpenCode via `make run_opencode`, the entire `opencode/config/` directory is mounted into the container at `/home/opencode/.config/opencode` (see `Makefile`).
This means skills in `opencode/config/skills/` are exposed inside the container by default.

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
