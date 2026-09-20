# Swarmforge

**Swarmforge: The foundation for forging robust systems and dependable tools**

Swarmforge is a builder-focused environment for designing, refining, and reusing processes and the tools they produce.
It emphasizes robustness, constraint-driven design, and interoperability over ad-hoc interaction or one-off execution.

## Installation

1. Add the shell helper alias and link the `swarmforge` command:

```bash
bash ./install.sh
```

This appends an `oc` alias to your shell rc file that runs `make run_opencode PROJECT_DIR=$(pwd)` against the repo's Makefile.
It also links `bin/swarmforge` into `~/.local/bin` so you can run [`swarmforge`](docs/cli.md) by name, replacing a symlink already there but never a regular file.

2. Build the container images you want:

```
make build_opencode
make build_claude
make build_grok
make build_codex
```

Or build them all at once with `make build_harnesses`. Each harness adds its own
build to that target, so a harness added later joins it with no list to update.
The images share every stage below the harness install, so `make -j -O build_harnesses`
is worth it once one build has populated that cache (`-O` keeps the parallel logs apart).

3. Run from your project directory:

- OpenCode: `oc`
- Claude Code: `make run_claude PROJECT_DIR=$(pwd)`
- Grok Build: `make run_grok PROJECT_DIR=$(pwd)`
- Codex CLI: `make run_codex PROJECT_DIR=$(pwd)`
- Pass OpenCode overrides as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).
- Override the container timezone per run (affects git commit timestamps): `oc TIMEZONE=America/New_York`.

[Installation](docs/installation.md) carries the rest: which rc file `install.sh` writes, pinning OpenCode to a release, the repo-local env file, and pointing several aliases at one checkout.

## Documentation

[docs/](docs/README.md) is the index. The pages behind it:

- [Installation](docs/installation.md), [swarmforge CLI](docs/cli.md), and [Git repos and worktrees](docs/git-guard.md) — getting a session running, and what the read-only git mounts change about it.
- [OpenCode](docs/harnesses/opencode.md), [Claude Code](docs/harnesses/claude-code.md), [Grok Build CLI](docs/harnesses/grok.md), [Codex CLI](docs/harnesses/codex.md) — one page per harness, and [Ollama](docs/ollama.md) for models served locally.
- [Configuration and assets](docs/configuration.md), [Agents](docs/authoring/agents.md), [Skills](docs/authoring/skills.md), [Commands](docs/authoring/commands.md) — what every harness picks up, and how to write it once.
- [Tongs](docs/tongs/README.md) — sidecar containers that hold what the agent must not, such as credentials.
- [Harness lifecycle](docs/development/architecture.md) and [Testing](docs/development/testing.md) — working on Swarmforge itself.
