# Swarmforge

**Swarmforge: The foundation for forging robust systems and dependable tools**

Swarmforge is a builder-focused environment for designing, refining, and reusing processes and the tools they produce.
It emphasizes robustness, constraint-driven design, and interoperability over ad-hoc interaction or one-off execution.

## Installation

1. Add the shell helper alias:

```bash
bash ./install.sh
```

This appends an `oc` alias to your shell rc file that runs `make run_opencode PROJECT_DIR=$(pwd)` against the repo's Makefile.
Run it with `bash` (it uses Bash arrays) even if your login shell is Zsh.
On macOS it prefers `~/.zshrc` and falls back to `~/.bash_profile`, so you don't need to create `~/.bashrc` manually.
Override the target file with `OC_RC_FILE=/path/to/rc bash ./install.sh`.

2. Build one or both container images:

```
make build_opencode
make build_claude
```

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

Both images share the same Debian base and toolchain (Node.js + Python; see `anvil/Dockerfile`).
Build targets pass `AGENT=opencode|claude` so only the requested agent install step runs.

3. Run from your project directory:

- OpenCode: `oc`
- Claude Code: `make run_claude PROJECT_DIR=$(pwd)`
- Pass OpenCode overrides as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).
- Override the container timezone per run (affects git commit timestamps): `oc TIMEZONE=America/New_York`.

### Repo-local env vars

`make run_opencode` and `make run_claude` load a repo-local env file from `.swarmforge/env` if it exists; override with `ENV_FILE=/path/to/env`.
Both also accept `TIMEZONE=<Region/City>` (default `Etc/UTC`), passed into the container as `TZ`.

### Multiple aliases (work/personal)

Define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys):

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

- `GITCONFIG_FILE` points at an agent-specific git config instead of `~/.gitconfig`.
- For Claude Code, use separate `CLAUDE_DATA_DIR` roots to isolate work/personal logins and session state. `CLAUDE_HOME_DIR` defaults to `$(CLAUDE_DATA_DIR)/home`.
- Config layering uses `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR` (their defaults differ per harness — see OpenCode layering under [Skills](#skills) and [Claude config layering](#claude-config-layering)). Set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` to resolve org defaults to `.opencode` (OpenCode) and `.claude` (Claude) under that root.

`SWARMFORGE_REPO_CONFIG_DIR` refers to the Swarmforge checkout (the harness repo), not the working project mounted at `/workspace`.
By default it is `$(SWARMFORGE_DIR)/opencode` for `run_opencode` and `$(SWARMFORGE_DIR)/claude` (if present) for `run_claude`.
Project-local config in the working repo (for example `.opencode/`) is still handled by the agent tools themselves.

### Git repos and worktrees

`make run_opencode` and `make run_claude` auto-detect the git root from `PROJECT_DIR` and mount it at `/workspace`.
For a linked git worktree they also mount the shared git common directory so git operations keep working inside the container.
This means `oc` works from repo roots, subdirectories, and linked worktrees without extra flags.

## Ollama

Run LLMs locally. `make run_ollama` starts an Ollama container on the shared network (`make stop_ollama` / `make clean` to tear down).
The `run_*` model targets (for example `make run_gpt-oss-20b`) exec into it to pull and run a model; `make gpu_stat` wraps `nvidia-smi`.

## OpenCode

A coding-agent harness that exposes a standard set of code-editing tools to the LLM.

## Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting as `make run_opencode`.
Claude state persists by mounting `$(CLAUDE_HOME_DIR)` to `/home/opencode`, keeping account/session files like `~/.claude/` and `~/.claude.json`.
The repo is mounted at a stable path derived from the git remote slug (with `/workspace` still mounted for compatibility), which groups sessions consistently across worktrees without host-specific absolute paths.

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- Remote slugs map deterministically, e.g. `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`. Override with `CLAUDE_REPO_SLUG=crypticswarm/Swarmforge` and `CLAUDE_REMOTE_NAME=<remote>`.

### Shared assets (skills, commands, agents)

Both harnesses mount this repo's `skills/` and `commands/` into the container, exported as `SWARMFORGE_SKILLS_DIR` and `SWARMFORGE_COMMAND_DIR`.
The entrypoint copies them into each harness's native location — `~/.claude/skills/` and `~/.claude/commands/` for Claude, the merged config dir (`~/.config/opencode/skills/`, `~/.config/opencode/command/`) for OpenCode.
For Claude these dirs (plus `~/.claude/agents/`) are container-private tmpfs mounts that mask the persistent home and are repopulated each run, so per-repo assets never accumulate in `CLAUDE_HOME_DIR` or leak into other repos' sessions.

Skills, commands, and agents come from four layers, lowest to highest precedence — later layers override same-named entries wholesale (never file-merged):

- **user** — `~/.agents/{skills,commands}` and `~/.swarmforge/agents/`
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.agents/{skills,commands}` and `.../.swarmforge/agents/`
- **repo** — this checkout's `skills/`, `commands/`, and `agents/`
- **workspace** — `<workspace>/.agents/{skills,commands}` and `<workspace>/.swarmforge/agents/`

Skills and commands follow the harness-neutral `.agents/{skills,commands}` convention and are copied as-is; agents use the unified format (see [Agents](#agents)) and are translated per harness.
Harness-native dirs (`<layer>/.opencode/skills/`, `<layer>/.claude/skills/`) are not consumed for skills/commands.
Override the `.agents` roots with `SWARMFORGE_USER_DOTAGENTS_DIR` / `SWARMFORGE_ORG_DOTAGENTS_DIR`.

### Claude config layering

Three sources merge into `~/.claude` at startup (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.claude`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude` when that root is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default `claude/`, if present)

Skills, commands, and `agents/` are excluded from this merge — they travel through the asset pipeline above.

## Agents

Subagent definitions live under `agents/` in a single unified format and are rewritten to each harness's native dialect by the container entrypoint (`anvil/translate_agents.py`).

A unified agent is a markdown file whose body is the system prompt and whose YAML frontmatter is a superset of the OpenCode agent schema. The filename is the agent's identity (`reviewer.md` -> agent `reviewer`):

```markdown
---
description: Reviews code and suggests improvements.
mode: subagent
temperature: 0.1
model: anthropic/claude-sonnet-4-6
tools:
  write: false
  edit: false
  bash: false
claude:
  maxTurns: 12
---

You are the reviewer agent...
```

Field handling per harness:

- `description` and the prompt body pass through everywhere.
- `tools` uses OpenCode's lowercase tool ids mapped to booleans. For Claude Code, disabled tools become `disallowedTools` (`write: false` -> `disallowedTools: Write`); ids with no Claude equivalent are dropped.
- `model` accepts a provider-qualified id (`anthropic/claude-sonnet-4-6`, passed through to OpenCode and stripped to the bare id for Claude — non-Anthropic providers dropped) or a Claude alias (`sonnet`, `haiku`, Claude-only and dropped for OpenCode).
- `mode`, `temperature`, and other OpenCode-only fields are dropped for Claude Code.
- `claude:` / `opencode:` blocks merge verbatim into that harness's output frontmatter.
- `disable: true` passes through to OpenCode and skips the agent for Claude Code.

Unified agents live in harness-neutral `.swarmforge/agents/` directories across the same four layers as shared assets (lowest to highest precedence):

- **user** — `~/.swarmforge/agents/` (override the `.swarmforge` root with `SWARMFORGE_USER_ASSETS_DIR`)
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/agents/` (override with `SWARMFORGE_ORG_ASSETS_DIR`)
- **repo** — `agents/` in the checkout (override with `SWARMFORGE_REPO_AGENTS_DIR`, which points directly at an agents dir so the rest of the checkout is never mounted)
- **workspace** — `<workspace>/.swarmforge/agents/`

Layers mount read-only under `/tmp/swarmforge-assets/{user,org}` and `/tmp/swarmforge-assets/repo/agents` (the in-container `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` env vars point at the layer roots); the entrypoint translates the stacked sources into each harness's native location (`~/.config/opencode/agents/` for OpenCode, the container-private `~/.claude/agents/` for Claude). Later layers override earlier ones by filename.
Claude-native repo-local definitions (for example `<workspace>/.claude/agents/`) are still discovered by Claude directly, outside this pipeline.

Run the translator's tests with `python3 scripts/test_translate_agents.py`.

## Commands

Slash commands live under `commands/` (and optionally `.opencode/command/` for repo-local commands).
Start your prompt with the command name to inject it (for example `/commit` injects [`commands/commit.md`](commands/commit.md)).

Command files can include `!` shell-expansion blocks, for example:

```
!`git status --short`
```

The harness runs these and injects their output into the prompt context, so the agent sees live repo state without copy/pasting.

## Skills

Skills live under `skills/` (harness-neutral, shared by every harness).
OpenCode auto-discovers them using only the YAML frontmatter (`name` + `description`); the full `SKILL.md` body loads on demand when a skill is invoked, keeping the default context small.

`make run_opencode` merges config into `/home/opencode/.config/opencode` from three sources (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.config/opencode`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode` when that root is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default repo-local `opencode/`)

`opencode.json` is merged by key (not file overwrite), so org-level MCP servers survive even when the repo layer also defines `opencode.json`.
Skills and commands are excluded from this merge and travel through the asset pipeline described under [Claude Code](#claude-code).

You can also define MCP servers in a project-local `.opencode/opencode.json` — often the cleanest place to attach them to a specific repo:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "org-server": {
      "type": "remote",
      "url": "https://mcp.example.com",
      "enabled": true
    }
  }
}
```

`make run_opencode` mounts your host `~/.gitconfig` into the container if it exists, so agents inherit your `user.name` and `user.email`.
Point at an alternative with `GITCONFIG_FILE=/path/to/gitconfig`.

Note: `opencode/opencode.json` also supports an `instructions` array for global instruction files, which load in full — avoid listing full `SKILL.md` files there unless you want them always in context.

## Tongs (sidecar processes)

A **tong** is a Swarmforge-managed sidecar container started alongside the anvil (the harness container you work in).
The name captures the primary use case: holding something hot — usually credentials — so the agent never touches it directly.
A credential-holding tong runs as a sibling container exposing an **MCP server** the agent calls over the session network; the secret material lives only in the tong's process space.
Tongs can also be plain network services (a throwaway Postgres, a fixture server), volume providers, or background side-effect processes.

Tongs are YAML files discovered across the **same four layers** as agents.
The host-side launcher (`scripts/run_anvil.py`) discovers, approves, starts, and tears them down; `make run_opencode` / `make run_claude` already delegate to it.

### Quick start: run a tong

1. (Only if the tong needs secrets) configure the secret-provider table (see [Secret providers](#secret-providers)).
2. Drop a tong definition into a layer directory, e.g. `~/.swarmforge/tongs/<name>.yaml` (personal) or `<workspace>/.swarmforge/tongs/<name>.yaml` (project).
3. Run the anvil as usual (`oc`, or `make run_claude PROJECT_DIR=$(pwd)`). A **workspace**-sourced tong prints a privilege summary and asks for approval on first run (see [First-run approval](#first-run-approval)). The launcher resolves secrets (which may prompt your provider CLI to unlock), starts the tong, waits for readiness, injects reachability into the anvil, then runs the anvil in the foreground.
4. On exit (including Ctrl-C), `session` tongs and the per-session network are torn down; `shared` tongs are left running.

### Where definitions live

One YAML file per tong under `.swarmforge/tongs/`, merged **by name** (filename = identity) lowest to highest precedence:

- **user** — `~/.swarmforge/tongs/` (override the root with `SWARMFORGE_USER_ASSETS_DIR`)
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/tongs/` (override with `SWARMFORGE_ORG_ASSETS_DIR`)
- **repo** — `tongs/` in the checkout (override with `SWARMFORGE_REPO_TONGS_DIR`, which points directly at the directory)
- **workspace** — `<workspace>/.swarmforge/tongs/`

A higher layer replaces a same-named tong wholesale; `disable: true` switches off an inherited tong.
The user/org/repo layers are **trusted**; the workspace layer (any repo you happened to clone) is gated by first-run approval.

### Definition format

```yaml
# ~/.swarmforge/tongs/github-creds.yaml
description: Holds GitHub credentials, exposes push/PR operations as MCP
lifecycle: session            # session | shared (required)
image: ghcr.io/example/github-tong@sha256:...   # required; pinned digest recommended
env:
  GITHUB_TOKEN: ${secret:op:op://Work/github/token}  # resolved on the host launcher
  LOG_LEVEL: info             # plain values pass through as ordinary -e env
interface:                    # required; how (or whether) the anvil reaches the tong
  kind: mcp                   # mcp | port | volume | none
  transport: http             # http only in v1
  port: 8080                  # the port the server listens on inside the container
  name: github                # canonical MCP server name the agent sees
mounts:                       # opt-in magic words only, never raw host paths
  - workspace:ro
resources:
  memory: 512m                # string or number
networks:                     # optional extra pre-existing networks to also join
  - some-existing-net
# entrypoint: [...]           # optional argv override of the image ENTRYPOINT
# command: [...]              # optional argv override of the image CMD
```

Required fields: `lifecycle`, `image`, and `interface` (with a valid `kind`). Unknown keys are tolerated for forward compatibility.

#### Interface kinds

The `interface:` block drives what gets injected into the anvil, how readiness is checked, and what plumbing is wired up:

- **`mcp`** — an HTTP MCP server (the common case). Requires `port` and `name`; `transport` defaults to `http`. Injects per-harness MCP config pointing at `http://<name>:<port>/mcp` on the session network (`interface.path` overrides the `/mcp` suffix). TCP readiness probe by default.
- **`port`** — a non-MCP network service. Requires `port`; optional `protocol` is informational. Injects `SWARMFORGE_TONG_<NAME>_HOST` (the canonical alias) and `SWARMFORGE_TONG_<NAME>_PORT` so the anvil composes its own connection string. TCP readiness probe by default.
- **`volume`** — a shared named volume, no network. Requires `volume` and `mountpoint`; readiness must be declared. The schema accepts it, but **the launcher does not wire it up yet** and refuses to start such a tong with a clear message.
- **`none`** — a background side-effect with no anvil-facing surface. Injects nothing. Readiness must be declared.

`<NAME>` is the filename uppercased with hyphens turned into underscores (`github-creds` → `SWARMFORGE_TONG_GITHUB_CREDS_*`).
The MCP server name and the `port` alias are docker network aliases (not container names), so generated config is identical regardless of where the workspace is mounted.

#### Readiness

```yaml
readiness:
  mode: healthcheck           # tcp | healthcheck | none
  command: ["test", "-S", "/run/agent.sock"]   # for mode: healthcheck (docker exec)
  timeout: 30s                # 30s / 500ms / 2m, or a bare number of seconds (default 30s)
```

`tcp` is the implicit default for `mcp` and `port`. `volume` and `none` have no port to probe, so `mode` is **required** for them; use `mode: none` to deliberately skip the gate.

#### Mounts

Mounts are opt-in **magic words**, never raw host paths. Only two are recognized, each with an optional `:mode` suffix forwarded to docker verbatim:

- `workspace[:mode]` — bind-mounts the session workspace at `/workspace` (e.g. `workspace:ro`).
- `docker-socket[:mode]` — bind-mounts the host docker socket. This is full host docker control and is always called out explicitly in the workspace approval prompt; it is the grant a broker tong needs.

### Lifecycle

- **`session`** — started with the anvil, torn down when it exits. Per-session isolation; the default for credential tongs.
- **`shared`** — long-lived, survives across anvil sessions (ollama-style). Started on first use, connected to each session's network via a network alias, and left running on teardown (no refcounting). A running `shared` container whose config-hash docker label still matches the current definition is reused untouched; a missing, stopped, or stale one is recreated automatically. A rotated secret behind an unchanged reference does **not** churn it — force a restart with `docker rm -f <container>`. A `shared` tong may not mount the `workspace` (it would leak one session's workspace into the next); use a `session` tong for per-workspace mounts.

### Secret providers

Secret references are resolved **on the host** by shelling out to a provider CLI — Swarmforge knows nothing about any individual secret manager.
Declare your providers once in the user layer at `~/.swarmforge/secret-providers.yaml` (override the root with `SWARMFORGE_USER_ASSETS_DIR`):

```yaml
# ~/.swarmforge/secret-providers.yaml
providers:
  op:      ["op", "read", "{ref}"]
  pass:    ["pass", "show", "{ref}"]
  doppler: ["doppler", "secrets", "get", "{ref}", "--plain"]
  aws:     ["aws", "secretsmanager", "get-secret-value", "--secret-id", "{ref}",
            "--query", "SecretString", "--output", "text"]
```

Each value is an argv template; the literal token `{ref}` in any element is replaced with the reference. Command templates must be single-line flow lists.
A missing file means no providers are configured, so any secret reference fails loudly rather than resolving to an empty value.

Reference a secret from a tong's `env:` as `${secret:<provider>:<ref>}`, for example `${secret:op:op://Work/github/token}`.
Because the launcher runs in your terminal before the anvil starts, interactive unlocks (`op signin`, biometric prompts) work for free.

**Per-secret overrides.** A provider value may instead be a structured entry with a `default` command and per-secret `overrides`, so a *shared* tong (say in the org layer) can reference `${secret:<provider>:<ref>}` while each developer's personal table decides how each individual secret is fetched. One developer resolves a ref through `pass`, another through `1Password`, without touching the shared tong:

```yaml
# ~/.swarmforge/secret-providers.yaml
providers:
  shared:
    default: ["pass", "show", "{ref}"]        # used for any ref not overridden
    overrides:
      ci-token: ["doppler", "secrets", "get", "CI_TOKEN", "--plain"]
```

Resolving `${secret:shared:<ref>}` uses the argv in `overrides` for that ref, falling back to `default`. A ref with neither stops the launch with a clear message. `default` is optional (use `overrides` alone to require every ref be listed), and `{ref}` substitution still applies to whichever command is chosen. Because `default` and `overrides` are separate keys, a secret literally named `default` is just an entry under `overrides` — distinct from the fallback — and any other provider-level key is flagged as a typo at load.

**Delivery is leak-resistant by design.** A resolved secret is never passed as a docker `-e` value, a command-line argument, or a file on disk (anything holding the docker socket could read those back). Instead the launcher streams the secret env to the tong over a host FIFO and wraps the tong's entrypoint with a `/bin/sh` prologue that reads the FIFO, exports the values, then execs the image's real entrypoint — so an unmodified off-the-shelf server that reads its credentials from `process.env` works as-is. A tong with secret env therefore needs `/bin/sh` in its image; a tong without secrets runs its image entrypoint unchanged. Plain (non-secret) `env:` values still flow through `-e`.

### First-run approval

The user, org, and repo layers are installed deliberately and are trusted; they skip the gate.
A **workspace**-sourced tong (from a repo you cloned) could otherwise request your secrets, host mounts, or the docker socket simply by being present, so the launcher gates it:

- Before starting, it prints exactly what the tong requests — image, secret references, mounts, networks, and docker-socket access — and asks you to approve.
- Approval is keyed by workspace path + tong name + a hash of the merged definition, stored in `~/.swarmforge/approvals.json`. Any change to the definition re-prompts.
- The gate defaults to **No**, and a non-interactive stdin reads as No. A scripted `--no-prompt` run **fails closed** rather than auto-approving.
- Approving `image: foo:latest` approves a moving target; **pinned digests are the recommended convention** for workspace tongs.

### Broker tongs

A **broker** is a tong that holds the docker socket and spawns its own short-lived worker containers on demand, so the anvil can compile, run tests, or do other sandboxed work without ever getting socket access itself.

`tongs/docker-broker/` ships a reference broker: an HTTP MCP server whose verbs are defined by a **declarative config**, not hand-written per project. Each command in `broker.config.yaml` describes the worker container to spawn — reusing the tong definition shape (`image`, `mounts`, `command`, `env`, `resources`, `networks`) — with an MCP surface (`name`, `description`, typed `params`) on top:

```yaml
allowed_images:
  - node:24-alpine            # the entire image allowlist; nothing else can run
commands:
  - name: test                # the MCP tool the agent calls
    description: Run the project's test suite.
    image: node:24-alpine
    mounts: [workspace:/work:ro]
    workdir: /work
    command: [npm, test, --]
    params:
      - name: suite            # exposed as a constrained MCP input
        type: enum             # boolean | enum
        values: [unit, integration, e2e]
        append_value: true     # the chosen value is appended as one command token
```

The config **is** the broker's allowlist. There is no verb that runs an arbitrary image or mounts an arbitrary host path: a worker may only mount the session `workspace`, and a parameter can only toggle a fixed effect (`boolean`) or pick a value from a fixed set (`enum`) — values are passed as whole argv words to a worker spawned without a shell, so nothing a caller sends can become a flag, path, or shell metacharacter. The launcher hands the broker the workspace's host path as `SWARMFORGE_WORKSPACE_HOST_PATH` so it can mount the workspace into the workers it spawns.

To enable it:

1. `make build_broker` — builds the `swarmforge-docker-broker` image.
2. Copy the example definition into a layer: `cp tongs/docker-broker/docker-broker.tong.yaml ~/.swarmforge/tongs/docker-broker.yaml`.

The example definition is **not** auto-discovered from the checkout (it lives a directory below the layer root, and discovery reads only top-level `*.yaml`), so the broker stays off until you opt in. Because it requests the docker socket, a workspace-sourced copy is always called out in the approval prompt.

## Skill Tests

A lightweight skill test harness runs scenario prompts against a chosen model and verifies expected behavior.

- Run all skill tests: `make test MODEL=<provider/model>`
- Run a single skill's tests: `make test MODEL=<provider/model> TEST_SKILL=<skill-name>`
- Optional judge mode: `make test MODEL=<student> TEST_ENABLE_JUDGE=1 EVAL_MODEL=<judge>`
- Timeout override: `make test MODEL=<provider/model> TEST_TIMEOUT_S=<seconds>`

Tests live in `skills/<skill-name>/tests/*.json`; the runner is `scripts/test_skills.py`.
Assertions can be:

- Output patterns: `expect.must_match` and `expect.must_not_match` (regex against formatted output)
- Tool calls: `expect.must_tool` and `expect.must_not_tool` (extracted from `opencode run --format json` events)
