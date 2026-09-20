# Environment variables

Every knob, its default, and where it is read. The reasoning behind a knob is on the page that owns its subject: [Configuration and assets](../configuration.md) for the layers, the [harness pages](../README.md#harnesses) for a harness, [Make targets](make-targets.md) for which target takes what.

Three kinds, set three different ways:

- **Make variables** — `make run_claude CLAUDE_DATA_DIR=/srv/claude`, or exported before the call. Nearly all are `?=`, so either wins over the default.
- **Host environment** — read by [`install.sh`](../installation.md), `bin/swarmforge`, or the launcher; exporting is the only way to set them.
- **Container environment** — set for you, and read inside the anvil by the [lifecycle drivers](../development/architecture.md).

## Values that are not knobs

Computed with `:=`, which ignores an exported value. A command-line assignment still wins, and for the last two that breaks the container.

| Variable | Value |
| --- | --- |
| `SWARMFORGE_DIR` | the directory holding the `Makefile`, which every checkout-relative default derives from |
| `PROJECT_NAME` | the basename of `PROJECT_DIR`, which names the container |
| `UID` `GID` | `id -u` and `id -g`, so session files come out owned by you |
| `WORKSPACE_MOUNT` | `/workspace`, named by the entrypoint, the layer env, and the [git-dir guard](../git-guard.md) alike |
| `ANVIL_HOME` | `/home/anvil`, hardcoded again in `anvil/entrypoint.sh` |

## Running a session

Read by every `run_<harness>` target.

| Variable | Default | What it does |
| --- | --- | --- |
| `PROJECT_DIR` | `$(CURDIR)` | The project to run against. Computed with `:=`, so an exported value is ignored. |
| `GITCONFIG_FILE` | `$(HOME)/.gitconfig` | Mounted read-only at `/home/anvil/.gitconfig` when it is a file, and skipped when it is not, so an agent commits under your `user.name` and `user.email`. |
| `ENV_FILE` | `$(PROJECT_DIR)/.swarmforge/env` | Passed as `docker run --env-file` when the file exists. The route for anything the harness wants in its environment that no make variable names. |
| `TIMEZONE` | `Etc/UTC` | Arrives as `TZ`. A name with no file under `/usr/share/zoneinfo` keeps the image default and warns on stderr. |
| `NETWORK` | `opencode-net` | The docker network the anvil, the tongs, and Ollama share. |
| `SWARMFORGE_REPO_SLUG` | empty | The path under `/repos` the workspace mounts at; empty derives it from the remote URL, then the basename. Slug-mounting harnesses only, not `run_opencode`. |
| `SWARMFORGE_REMOTE_NAME` | `origin` | Which remote's URL the slug comes from. |

## Per-harness storage and arguments

| Harness | Image | Container | Data dir | Home dir | Extra arguments |
| --- | --- | --- | --- | --- | --- |
| [OpenCode](../harnesses/opencode.md) | `OPENCODE_IMG` = `opencode:local` | `OPENCODE_CTR` = `opencode-$(PROJECT_NAME)` | `DATA_DIR` = `$(HOME)/.local/share/opencode` | — | `OPENCODE_ARGS` |
| [Claude Code](../harnesses/claude-code.md) | `CLAUDE_IMG` = `claude-code:local` | `CLAUDE_CTR` = `claude-$(PROJECT_NAME)` | `CLAUDE_DATA_DIR` = `$(HOME)/.local/share/claude` | `CLAUDE_HOME_DIR` = `$(CLAUDE_DATA_DIR)/home` | `CLAUDE_ARGS` |
| [Grok Build CLI](../harnesses/grok.md) | `GROK_IMG` = `grok-build:local` | `GROK_CTR` = `grok-$(PROJECT_NAME)` | `GROK_DATA_DIR` = `$(HOME)/.local/share/grok` | `GROK_HOME_DIR` = `$(GROK_DATA_DIR)/home` | `GROK_ARGS` |
| [Codex CLI](../harnesses/codex.md) | `CODEX_IMG` = `codex-cli:local` | `CODEX_CTR` = `codex-$(PROJECT_NAME)` | `CODEX_DATA_DIR` = `$(HOME)/.local/share/codex` | `CODEX_HOME_DIR` = `$(CODEX_DATA_DIR)/home` | `CODEX_ARGS` |

`*_HOME_DIR` is what gets mounted at `/home/anvil`; `*_DATA_DIR` is only its parent. OpenCode has neither: `DATA_DIR` is mounted at `/home/anvil/.local/share/opencode` on its own.

| Variable | Default | What it does |
| --- | --- | --- |
| `PROFILE` | empty | Becomes `--profile <name>` on OpenCode's command line, ahead of `OPENCODE_ARGS`. |
| `*_ARGS` | empty | Appended to the harness binary's argv, word-split by the shell. |

## Asset layers

The roots the four skill, command, and agent layers resolve against; [Asset layers](../configuration.md#asset-layers) has the precedence.

| Variable | Default | What it points at |
| --- | --- | --- |
| `SWARMFORGE_ORG_CONFIG_ROOT` | empty | A checked-out org config repo. Every org default derives from it and stays empty while it is. |
| `SWARMFORGE_USER_ASSETS_DIR` | `$(HOME)/.swarmforge` | The user `.swarmforge` root: `agents/`, `tongs/`, [`approvals.json`](../tongs/approval.md), [`secret-providers.yaml`](../tongs/secrets.md). |
| `SWARMFORGE_ORG_ASSETS_DIR` | `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge` | The same for the org layer. |
| `SWARMFORGE_REPO_AGENTS_DIR` | `$(SWARMFORGE_DIR)/agents` | The repo layer's agents, pointed at directly so the rest of the checkout is never mounted. |
| `SWARMFORGE_USER_DOTAGENTS_DIR` | `$(HOME)/.agents` | The user layer's portable `skills/` and `commands/`. |
| `SWARMFORGE_ORG_DOTAGENTS_DIR` | `$(SWARMFORGE_ORG_CONFIG_ROOT)/.agents` | The same for the org layer. |
| `SHARED_SKILLS_DIR` | `$(SWARMFORGE_DIR)/skills` | The repo layer's skills. |
| `SHARED_COMMAND_DIR` | `$(SWARMFORGE_DIR)/commands` | The repo layer's commands. |
| `SWARMFORGE_REPO_TONGS_DIR` | `$(SWARMFORGE_DIR)/tongs` | The repo layer's [tong](../tongs/README.md) definitions, read on the host. |

Both org defaults are empty while `SWARMFORGE_ORG_CONFIG_ROOT` is. Every asset mount but the two `SHARED_` ones is skipped when its directory is absent, rather than created empty. The workspace layer has no variable: it is `.agents` and `.swarmforge` under the resolved workspace.

## Config layers

Where a harness's config is merged from and where it lands; [Config layers](../configuration.md#config-layers) has the precedence and the per-harness directories.

The first five are target-specific `?=` assignments on each `run_<harness>` rule, defaulted from that harness's `harness.mk`. An environment or command-line value replaces the default — so a `make` run from inside an anvil inherits `SWARMFORGE_CONFIG_DEST` and `SWARMFORGE_CONFIG_RESET` from the session around it.

| Variable | Default | What it does |
| --- | --- | --- |
| `SWARMFORGE_USER_CONFIG_DIR` | per harness | Mounted at `/tmp/swarmforge-config/user`. This mount alone is unconditional, so the run target creates the directory. |
| `SWARMFORGE_ORG_CONFIG_DIR` | per harness | Mounted at `/tmp/swarmforge-config/org` when present. |
| `SWARMFORGE_REPO_CONFIG_DIR` | per harness | Mounted at `/tmp/swarmforge-config/repo` when present. |
| `SWARMFORGE_CONFIG_DEST` | per harness, below | Where the merge lands in the container. Empty skips the config phase. |
| `SWARMFORGE_CONFIG_RESET` | per harness, below | `1` deletes the destination before merging. |
| `OPENCODE_CONFIG_DIR` | `$(SWARMFORGE_DIR)/opencode` | OpenCode's repo config layer, which `SWARMFORGE_REPO_CONFIG_DIR` defaults to for `run_opencode`. |

| Harness | Config destination | From `SWARMFORGE_CONFIG_DEST`? | Reset | From `SWARMFORGE_CONFIG_RESET`? |
| --- | --- | --- | --- | --- |
| OpenCode | `/home/anvil/.config/opencode` | yes | on | yes |
| Grok | `/home/anvil/.grok` | yes | off | yes |
| Claude Code | `/run/swarmforge/claude-config` | no, the spec pins it | off | yes |
| Codex CLI | `/run/swarmforge/codex-config` | no, the spec pins it | on | no, the spec forces it on |

Each harness also has plain-`=` `<PREFIX>_`-named defaults in its fragment: `CLAUDE_USER_CONFIG_DIR=/srv/x` overrides one harness, the `SWARMFORGE_` name whichever is running.

`scripts/skill_eval.py` sets its own `OPENCODE_CONFIG_DIR` inside the eval container: a throwaway directory assembled once per run from the checkout's `opencode/`, `skills/`, and `commands/`.

## Building images

| Variable | Default | What it does |
| --- | --- | --- |
| `DEBIAN_TAG` | `trixie-slim` | The `debian:` tag every stage builds from. |
| `SWARMFORGE_HARNESS_INSTALL_BUST` | `0` | Invalidates one image's harness install layer. `update_<harness>` sets it to `date +%s`. |
| `OPENCODE_VERSION` | empty | The OpenCode release to pin; empty installs the installer's latest. |
| `BROKER_IMG` | `swarmforge-docker-broker:latest` | The tag `build_broker` writes. |

`anvil/Dockerfile`'s own build args. The targets supply `AGENT` and map `OPENCODE_VERSION` onto the version pin; the rest are for a hand-run `docker build`.

| Build arg | Default | What it does |
| --- | --- | --- |
| `AGENT` | `opencode` | The harness to install. A name with no `swarmforge/harness/<name>/install.sh` fails the build. |
| `SWARMFORGE_HARNESS_VERSION` | empty | The version pin that harness's `install.sh` reads; `OPENCODE_VERSION` maps onto it. |
| `PYTHON_VERSION` | `3.12.7` | The CPython release built into `/opt/python`. |
| `NODE_MAJOR` | `24` | The NodeSource major version. |
| `PLAYWRIGHT_VERSION` | `1.60.0` | The Playwright release whose chromium is installed. |

## Tests and tooling

What each target runs is in [Testing](../development/testing.md).

| Variable | Default | Read by |
| --- | --- | --- |
| `PYTHON` | `python3` | `make test`, and the launcher shims the `run_*` targets call. |
| `RUFF` | `ruff` | `make lint`, as the path to the ruff binary. |
| `MODEL` | empty | `make test-skills`, which requires it. |
| `EVAL_MODEL` | `$(MODEL)` | The judge model, when judging is on. |
| `TEST_ENABLE_JUDGE` | empty | Non-empty adds `--enable-judge`. |
| `TEST_SKILL` | empty | Non-empty adds `--skill <name>`. |
| `TEST_TIMEOUT_S` | `600` | The per-scenario timeout. |
| `TEST_DATA_DIR` | `$(DATA_DIR)` | The OpenCode data dir the eval container gets. |
| `FORCE_COLOR` | unset | Host environment. `make test-skills` passes `--color always` and forwards nothing, so `1` reaches a hand-run `scripts/skill_eval.py --color auto` only. |

## Ollama

The targets are in [Make targets](make-targets.md#ollama).

| Variable | Default | What it does |
| --- | --- | --- |
| `OLLAMA_IMG` | `ollama/ollama` | The image `run_ollama` starts. |
| `OLLAMA_CTR` | `ollama` | The container name; the `run_<model>` targets name `ollama` literally and ignore this. |
| `OLLAMA_PORT` | `11434` | The host port. The container is started with `OLLAMA_HOST=0.0.0.0:11434`, so inside the network it is always `http://$(OLLAMA_CTR):11434`. |
| `OLLAMA_CTX` | `32768` | Passed through as `OLLAMA_CONTEXT_LENGTH`. |

## On the host

[`install.sh`](../installation.md) reads four variables from your shell rather than from `make`:

| Variable | What it does |
| --- | --- |
| `OC_RC_FILE` | The rc file to append the `oc` alias to, skipping the per-platform search. |
| `HOME` | Both halves of the install write under it, and the rc candidates derive from it. Unset, the script exits. |
| `SHELL` | Its basename picks which rc candidates are tried first. |
| `PATH` | Only to warn when `~/.local/bin` is not on it. |

The launcher reads `SWARMFORGE_USER_ASSETS_DIR` from its own environment too, as the fallback root for `approvals.json` and `secret-providers.yaml` when a call passes neither `--approvals` nor `--providers`.

[`swarmforge clone` and `swarmforge init`](../cli.md) drop `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, and `GIT_NAMESPACE` from the environment they hand git, since `git -C` does not override them.

## Inside the container

| Variable | Value | Read by |
| --- | --- | --- |
| `SWARMFORGE_UID` `SWARMFORGE_GID` | the host `id -u` / `id -g` | `anvil/entrypoint.sh`, which creates the `anvil` user at those ids. Both fall back to `1000`. |
| `SWARMFORGE_AGENT_BIN` | the harness name | The entrypoint, which execs `/usr/local/bin/$SWARMFORGE_AGENT_BIN`. Baked into the image as `ENV` from `AGENT`, and passed again by the Claude, Grok, and Codex run targets. Falls back to `opencode`. |
| `HOME` | `/home/anvil` | Set by the user-phase driver before the exec. `make test-skills` sets the same value with `-e HOME`, since it runs the eval script in place of the entrypoint. |
| `TZ` | `TIMEZONE` | The entrypoint's timezone step. |
| `TERM` `COLORTERM` | forwarded from your shell | The harness binary. |
| `PATH` | `/opt/python/bin` ahead of the image default | Every process in the container. Claude's `pre_exec` prepends the git-wrapper directory on top of it when the root phase installed one. |
| `PLAYWRIGHT_BROWSERS_PATH` | `/ms-playwright` | Playwright, for the chromium the image installs. |
| `SWARMFORGE_CONFIG_{USER,ORG,REPO}_DIR` | `/tmp/swarmforge-config/{user,org,repo}` | `swarmforge.harness.init`, as the three config layers. |
| `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` | `/tmp/swarmforge-assets/{user,org,repo}` | `swarmforge.harness.init`, as the agent-definition layers. |
| `SWARMFORGE_DOTAGENTS_{USER,ORG}_DIR` | `/tmp/swarmforge-dotagents/{user,org}` | `swarmforge.harness.init`, as the portable skill and command layers. |
| `SWARMFORGE_SKILLS_DIR` | `/home/anvil/.swarmforge/skills` | `swarmforge.harness.init`, as the repo skill layer. |
| `SWARMFORGE_COMMAND_DIR` | `/home/anvil/.swarmforge/command` | `swarmforge.harness.init`, as the repo command layer. |
| `SWARMFORGE_CONFIG_DEST` `SWARMFORGE_CONFIG_RESET` | the make variables, passed through | `swarmforge.harness.init`. |
| `SWARMFORGE_TONG_MCP_FILE` | `/tmp/swarmforge-tong-mcp.json` | `swarmforge.harness.init`, as the generated MCP fragment to merge. Set only for a session with MCP tongs and a harness whose spec takes the path by environment variable; one that takes a flag gets the same mount on its command line. |
| `SWARMFORGE_TONG_<NAME>_HOST` `_PORT` `_PATH` | the tong's alias and port, or its mountpoint | The agent. One set per `port` or `volume` [tong](../tongs/definitions.md#interface-kinds). |
| `SWARMFORGE_WORKSPACE_HOST_PATH` | the workspace's path on the host | A socket-holding `session` [broker](../tongs/broker.md) tong. Never a `shared` tong, and never over a value the tong sets itself. |
| `CLAUDE_CONFIG_DIR` | [the merged config dir](../harnesses/claude-code.md#the-config-directory) | `claude`, set by its `pre_exec` hook. |
| `CLAUDE_SECURESTORAGE_CONFIG_DIR` | `/home/anvil/.claude` | `claude`, set by the same hook. |
| `PYTHONPATH` | `/usr/local/lib/swarmforge` | Both lifecycle drivers, so the copied package imports. Dropped before the harness is exec'd. |
| `PYTHONCOERCECLOCALE` | `0` | The user-phase driver alone, so the interpreter does not put `LC_CTYPE` into the environment the harness inherits. Dropped before the exec. |
