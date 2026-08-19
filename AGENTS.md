# AGENTS.md

## Scope
- These instructions apply to the entire repository until a nested `AGENTS.md` overrides them.
- Follow this file whenever you add or modify source, scripts, Docker assets, or skills anywhere in the repo.

## Repo Overview
- `Makefile` orchestrates the local containers (`build_opencode`, `run_opencode`, `run_ollama`, etc.) and is the preferred entry point for automation.
- `install.sh` appends an `oc` helper alias that shells out to `make -C <repo> run_opencode`; keep it POSIX-compliant because it is sourced in user shells.
- `anvil/` holds the container-side assets shared by every harness image (OpenCode, Claude Code): the Dockerfile, the container entrypoint, and the Claude-only status line (`statusline.sh`) with the image config layer that turns it on (`claude-settings.json`). The images build from the repo root (`-f anvil/Dockerfile`) so `swarmforge/` can be copied in; `.dockerignore` keeps everything else out of the context.
- `swarmforge/` is the Python for both sides of the container boundary: the host launcher (`anvil`, `tongs`, `gitguard`), and the agent translation and config merging the entrypoint runs with `python3 -m`. It is stdlib-only: the image installs no third-party Python and the launcher runs on the host's `python3`.
- `bin/` holds the launcher entry points (`run-anvil`, `tongs`, `git-guard`). Each is a shim that puts the checkout on `sys.path` and calls its module's `main()` — the only files that resolve a path, so nothing under `swarmforge/` needs to know where it sits on disk.
- `opencode/` holds the OpenCode-native repo config layer (`opencode.json`, plus untracked plugin state); harness-neutral assets live at the top level in `skills/`, `commands/`, and `agents/`.
- `ollama/` stores persistent Ollama state. Do not add large model blobs to git—only configuration or lightweight defaults belong here.

## Coding Conventions
- Default to Bash or POSIX shell for scripts and include `set -euo pipefail` (or equivalent) when modifying shell entrypoints.
- Prefer `make` variables and targets over ad-hoc scripts so contributors can compose workflows via the existing Makefile.
- Keep Dockerfiles Debian-based (see `DEBIAN_TAG`) and avoid pinning GPU driver versions inside the image; rely on host NVIDIA tooling instead.
- When editing skills under `skills/`, ensure YAML frontmatter only contains `name` and `description`, and keep the detailed guidance in the corresponding `SKILL.md` body.
- `swarmforge/` is layered rather than a bag of modules: `yamlite` is a leaf both sides of the container boundary import, the `tongs` modules build on each other in one direction, and the `anvil` modules sit on top of `tongs`. The unit suite fails on an import cycle, and on any file outside `bin/` that loads python from a file path instead of importing it by name.
- Subagent definitions under `agents/` use the unified agent format documented in `README.md` (`## Agents`); the entrypoint rewrites them per harness via `swarmforge/agents/translate.py`, so never hand-write harness-specific dialects there.

## Build, Test, and Run
- Build the OpenCode image with `make build_opencode` after changing anything under `anvil/` or `swarmforge/`.
- Launch a development session via `make run_opencode PROFILE=<name> DATA_DIR=<path?>` (defaults are fine for local work). The target automatically mounts project files and skills.
- Run the Python unit tests with `make test`. They are stdlib `unittest` collected by discovery over `tests/test_*.py`, so add a test file and it runs — never wire one up by name. A test module is named for the source module it covers: `tests/test_tongs_<module>.py`, `tests/test_anvil_<module>.py`. Fixtures more than one of them needs live in `tests/tongs_fixtures.py` / `tests/anvil_fixtures.py`, which the glob deliberately skips.
- Lint the Python with `make lint` (`ruff check`, configured in `pyproject.toml`). It is a linter only — never run `ruff format`, which would reflow files the change did not touch. Ruff is the one tool outside the stdlib this repo asks for, and it is a contributor tool: no image installs it and nothing under `swarmforge/` imports it.
- Run the skill eval harness via `make test-skills MODEL=<provider/model>`. It drives a real model in the OpenCode image, so it is a separate target from the unit suite.
- Filter skill evals with `TEST_SKILL=<skill-name>` and adjust timeouts with `TEST_TIMEOUT_S=<seconds>`.
- Start the local Ollama service with `make run_ollama` when testing models; pair it with `make stop_ollama` and `make clean` to tear everything down.
- Use `gpu_stat` (wraps `nvidia-smi`) to confirm GPU availability before running high-memory models.

## Additional Notes
- Keep secrets, API keys, and downloaded models out of version control; anything mounted into containers should be reproducible from repo contents.
- If you add new tooling, document the invocation in `README.md` so contributors understand how it integrates with `make`.
- Prefer small, surgical edits—do not reformat or restructure unrelated files when touching scripts or skills.
- Inside a container the workspace's `.git/config` and `.git/hooks` are mounted read-only, because both run commands on the host later. Committing, branching, fetching, stashing, and `git worktree add` work. Anything that writes config reports `error: could not write config file <path>: Device or resource busy`:
  - `git config --local`, `git remote add`, `git submodule update --init`, and `git sparse-checkout` fail outright.
  - `git push -u` and `git switch <remote-branch>` **exit 0 and still print "set up to track"**, but no tracking is recorded — check `git config --get branch.<name>.remote` rather than trusting the message. Use `git push origin HEAD:<branch>` and `git switch -c <name> --no-track origin/<branch>`.
  - Hook installers (`pre-commit install`, husky, lefthook) fail: `.git/hooks` is read-only.
  - Configure the repo on the host when you need any of this to stick.
