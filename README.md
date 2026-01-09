# LLM experiments

Aim is to build a repetoire of tools leveraging LLMs.
This includes tools for running LLMs locally in a safe way without giving access to the full filesystem.

## Installation

Run the installer to add common shell helpers:

```
./install.sh
```

The script appends an `oc` alias to your existing `~/.bashrc`, pointing to `make -C <repo> run_opencode PROJECT_DIR=$(pwd)` so you can launch OpenCode from any directory. If `~/.bashrc` is missing, create it first before rerunning the installer.

## Ollama

Run an LLMs locally.

## OpenCode

Test harness that has a standard set of tools exposed to LLM geared at editing code.

## Skills

Skills are stored under `opencode/config/skills/`.

When you run OpenCode via `make run_opencode`, the entire `opencode/config/` directory is mounted into the container at `/home/opencode/.config/opencode` (see `Makefile`). This means skills in `opencode/config/skills/` are exposed inside the container by default.

OpenCode auto-discovers skills from that directory and uses only the YAML frontmatter (`name` + `description`) for discovery. The full `SKILL.md` body is loaded on-demand when a skill is invoked, which helps keep the default context small.

Note: `opencode/config/opencode.json` also supports an `instructions` array for global instruction files. Those files are loaded in full, so avoid listing full `SKILL.md` files there unless you explicitly want them always in context.
