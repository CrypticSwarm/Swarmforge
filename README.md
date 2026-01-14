# Swarmforge

**Swarmforge: The foundation for forging robust systems and dependable tools**

Swarmforge is a builder-focused environment for designing, refining, and reusing processes and the tools they produce.
It emphasizes robustness, constraint-driven design, and interoperability over ad-hoc interaction or one-off execution.

## Installation

Run the installer to add common shell helpers:

```
./install.sh
```

The script appends an `oc` alias to your existing `~/.bashrc`, pointing to `make -C <repo> run_opencode PROJECT_DIR=$(pwd)` so you can launch OpenCode from any directory.
Pass any `make` overrides directly (for example `oc PROFILE=work DATA_DIR=~/.local/share/opencode-work`, or prefix them as environment variables like `PROFILE=work oc`) to map work/personal sessions to distinct profiles and storage roots.
If `~/.bashrc` is missing, create it first before rerunning the installer.

## Ollama

Run an LLMs locally.

## OpenCode

Test harness that has a standard set of tools exposed to LLM geared at editing code.

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
