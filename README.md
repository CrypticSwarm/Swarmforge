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
