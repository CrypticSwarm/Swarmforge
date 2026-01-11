# AGENTS.md

## Scope
- These instructions apply to the entire repository until a nested `AGENTS.md` overrides them.
- Follow this file whenever you add or modify source, scripts, Docker assets, or skills anywhere in the repo.

## Repo Overview
- `Makefile` orchestrates the local containers (`build_opencode`, `run_opencode`, `run_ollama`, etc.) and is the preferred entry point for automation.
- `install.sh` appends an `oc` helper alias that shells out to `make -C <repo> run_opencode`; keep it POSIX-compliant because it is sourced in user shells.
- `opencode/` is the Docker build context for the OpenCode harness, which provides a constrained execution environment for building and refining systems and tools.
- `ollama/` stores persistent Ollama state. Do not add large model blobs to git—only configuration or lightweight defaults belong here.

## Coding Conventions
- Default to Bash or POSIX shell for scripts and include `set -euo pipefail` (or equivalent) when modifying shell entrypoints.
- Prefer `make` variables and targets over ad-hoc scripts so contributors can compose workflows via the existing Makefile.
- Keep Dockerfiles Debian-based (see `DEBIAN_TAG`) and avoid pinning GPU driver versions inside the image; rely on host NVIDIA tooling instead.
- When editing skills under `opencode/config/skills/`, ensure YAML frontmatter only contains `name` and `description`, and keep the detailed guidance in the corresponding `SKILL.md` body.

## Build, Test, and Run
- Build the OpenCode image with `make build_opencode` after changing anything under `opencode/`.
- Launch a development session via `make run_opencode PROFILE=<name> DATA_DIR=<path?>` (defaults are fine for local work). The target automatically mounts project files and skills.
- Start the local Ollama service with `make run_ollama` when testing models; pair it with `make stop_ollama` and `make clean` to tear everything down.
- Use `gpu_stat` (wraps `nvidia-smi`) to confirm GPU availability before running high-memory models.

## Additional Notes
- Keep secrets, API keys, and downloaded models out of version control; anything mounted into containers should be reproducible from repo contents.
- If you add new tooling, document the invocation in `README.md` so contributors understand how it integrates with `make`.
- Prefer small, surgical edits—do not reformat or restructure unrelated files when touching scripts or skills.
