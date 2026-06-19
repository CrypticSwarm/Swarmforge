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

Both images share the same Debian base and toolchain (Node.js + Python; see `anvil/Dockerfile` for configured versions).
Build targets pass `AGENT=opencode|claude` so only the requested agent install step runs (works with both legacy Docker builder and BuildKit).

3. Run from your project directory:

- OpenCode: `oc`
- Claude Code: `make run_claude PROJECT_DIR=$(pwd)`
- Pass OpenCode overrides either as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).
- Override container timezone per run (affects git commit timestamps): `oc TIMEZONE=America/New_York` or `make run_claude PROJECT_DIR=$(pwd) TIMEZONE=America/New_York`.

### Repo-local env vars

`make run_opencode` loads a repo-local env file if it exists at `.swarmforge/env`.
Override with `ENV_FILE=/path/to/env make run_opencode`.

`make run_claude` uses the same repo-local env file path.
Override with `ENV_FILE=/path/to/env make run_claude PROJECT_DIR=$(pwd)`.

Both `make run_opencode` and `make run_claude` support `TIMEZONE=<Region/City>` (default: `Etc/UTC`) and pass it as `TZ` into the container.

### Multiple aliases (work/personal)

You can define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys).

Example:

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

`GITCONFIG_FILE` is useful if you keep an agent-specific git config rather than using your default `~/.gitconfig`.
For Claude Code, use separate `CLAUDE_DATA_DIR` roots to isolate work/personal logins and session state.
`CLAUDE_HOME_DIR` defaults to `$(CLAUDE_DATA_DIR)/home`, and can be overridden directly if needed.
Config layering is controlled with shared variables: `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR`.
If your org config repo has both agent layouts, you can set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` and defaults resolve to:
- OpenCode: `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode`
- Claude: `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude`

Important: `SWARMFORGE_REPO_CONFIG_DIR` refers to the Swarmforge checkout (the harness repo), not the active working project mounted at `/workspace`.
By default this means:
- `run_opencode` uses `$(SWARMFORGE_DIR)/opencode`
- `run_claude` uses `$(SWARMFORGE_DIR)/claude` (if present)

Project-local config in the working repo is still handled by the agent tools themselves (for example `.opencode/`), independent of this Swarmforge layering.

### Git repos and worktrees

`make run_opencode` auto-detects the git root from `PROJECT_DIR` and mounts that root at `/workspace`.
If the target is a linked git worktree, it also mounts the shared git common directory so git operations keep working inside the container.

This means `oc` works from repo roots, subdirectories, and linked worktrees without extra flags.

## Ollama

Run an LLMs locally.

## OpenCode

Test harness that has a standard set of tools exposed to LLM geared at editing code.

## Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting behavior as `make run_opencode`.
Claude state is persisted by mounting `$(CLAUDE_HOME_DIR)` to `/home/opencode`, which keeps account/session files such as `~/.claude/` and `~/.claude.json`.
By default it mounts the repo to a stable path derived from the git remote slug and starts Claude there (while still mounting `/workspace` for compatibility), which improves session grouping across worktrees without relying on host-specific absolute paths.

Session compatibility notes:

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- GitHub remote slugs map to deterministic paths, for example `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`.
- Override slug detection with `CLAUDE_REPO_SLUG=crypticswarm/Swarmforge` and remote selection with `CLAUDE_REMOTE_NAME=<remote>`.

Both `run_opencode` and `run_claude` mount the shared Swarmforge assets so every harness can access common resources:

- `skills/` -> `/home/opencode/.swarmforge/skills`
- `commands/` -> `/home/opencode/.swarmforge/command`

Those paths are exported in-container as `SWARMFORGE_SKILLS_DIR` and `SWARMFORGE_COMMAND_DIR`.
When launching Claude Code, the container entrypoint copies these into `~/.claude/skills/` and `~/.claude/commands/` so Claude can discover them as native skills/commands.
When launching OpenCode, the same entrypoint step copies them into the merged config destination (`~/.config/opencode/skills/` and `~/.config/opencode/command/`).
In-container `~/.claude/skills/`, `~/.claude/commands/`, and `~/.claude/agents/` are container-private tmpfs mounts that mask the shared persistent `CLAUDE_HOME_DIR`: each container starts them empty and the entrypoint repopulates them, lowest to highest precedence — skills and commands from the config layers (user, org, repo), the harness shared assets, and the current workspace's `.agents` overlay; agents from the `.swarmforge` asset layers described under `## Agents`.
All Swarmforge layers carry these assets in Swarmforge formats — skills and commands are portable and copied as-is, while agents use the unified format and are translated from the harness-neutral `.swarmforge/agents/` layers described under `## Agents`. Claude-native repo-local definitions (for example `<workspace>/.claude/agents/`) are still discovered by Claude itself, outside the Swarmforge pipeline.
This keeps per-repo skills, commands, and agents from accumulating in the persistent home and leaking into other repos' sessions.
The layered config merge skips skills and commands for every harness — they travel only through this asset pipeline, so each skill package is replaced wholesale by the highest-precedence layer that provides it instead of being file-merged across layers — and additionally skips `agents/` for Claude.

You can ship project-local skills/commands in your workspace and have them overlay the harness defaults: the entrypoint reads `<workspace>/.agents/skills/` and `<workspace>/.agents/commands/`, and workspace entries override harness entries with the same name.
Harness-native repo-local dirs (such as `<workspace>/.opencode/`) belong to their own harness and are not consumed for Claude.

Claude config layering uses three sources (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.claude` for `run_claude`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude` when `SWARMFORGE_ORG_CONFIG_ROOT` is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default `claude/` for `run_claude`, if present)

At startup these are merged into `~/.claude` inside the container, so personal defaults can be overlaid with org settings and repo-local overrides.

## Agents

Subagent definitions are stored under `agents/` in a single unified format and rewritten to each harness's native dialect by the container entrypoint (`anvil/translate_agents.py`), the same way `.claude` assets are populated for Claude Code.

A unified agent is a markdown file whose body is the agent's system prompt and whose YAML frontmatter is a superset of the OpenCode agent schema. The filename is the agent's identity (`reviewer.md` -> agent `reviewer`):

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
- `tools` uses OpenCode's lowercase tool ids mapped to booleans. For Claude Code, disabled tools become `disallowedTools` (`write: false` -> `disallowedTools: Write`); ids without a Claude equivalent are dropped.
- `model` accepts a provider-qualified id (`anthropic/claude-sonnet-4-6`) which passes through to OpenCode and is stripped to the bare id for Claude Code (non-Anthropic providers are dropped), or a Claude alias (`sonnet`, `haiku`) which is Claude-only and dropped for OpenCode.
- `mode`, `temperature`, and other OpenCode-only fields are dropped for Claude Code.
- `claude:` / `opencode:` blocks merge verbatim into that harness's output frontmatter, for anything the unified fields don't cover.
- `disable: true` passes through to OpenCode and skips emitting the agent for Claude Code.

Unified agents live in harness-neutral `.swarmforge/agents/` directories — one definition serves every harness, so the layers are deliberately not harness-specific. Lowest to highest precedence:

- user: `~/.swarmforge/agents/` (override with `SWARMFORGE_USER_ASSETS_DIR`, which points at the `.swarmforge` root; `agents/` is appended)
- org: `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/agents/` (override with `SWARMFORGE_ORG_ASSETS_DIR`, same root convention)
- repo: `agents/` in the Swarmforge checkout (override with `SWARMFORGE_REPO_AGENTS_DIR`, which unlike the other two points directly at an agents directory so the rest of the checkout is never mounted)
- workspace: `<workspace>/.swarmforge/agents/`

The asset layers are mounted read-only at `/tmp/swarmforge-assets/{user,org}` and `/tmp/swarmforge-assets/repo/agents` (the env vars `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` point at the layer roots), and the entrypoint translates the stacked sources — identical for every harness — into the harness's native location: `~/.config/opencode/agents/` for OpenCode, `~/.claude/agents/` (a container-private tmpfs scoped to the current repo) for Claude Code. Later layers override earlier ones by filename.

Native `agents/` directories are never carried by this asset pipeline. For OpenCode they still pass through the layered config merge (the merged config dir is OpenCode's own discovery); for Claude they are excluded from the merge entirely, and Claude-native definitions belong in Claude's own discovery (`<workspace>/.claude/agents/`, or `<workspace>/.opencode/agents/` for OpenCode), which each harness reads directly. (Skills and commands keep their `.agents/{skills,commands}` workspace convention — those formats are portable across harnesses, while agents are Swarmforge-specific and translated.)

Run the translator's tests with `python3 scripts/test_translate_agents.py`.

## Commands

Slash commands are stored under `commands/` (and optionally `.opencode/command/` for repo-local commands).
To run one, start your prompt with the command name (for example `/commit` will inject [`commands/commit.md`](commands/commit.md)).

Command prompt files often include `!` shell-expansion blocks, for example:

```
!`git status --short`
```

OpenCode runs these shell commands and injects their output into the prompt context, so the agent sees the live repo state without you copy/pasting it.

## Skills

Skills are stored under `skills/` (harness-neutral, shared by every harness).

When you run OpenCode via `make run_opencode`, config is layered from three sources (lowest to highest precedence):
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.config/opencode` for `run_opencode`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode` when `SWARMFORGE_ORG_CONFIG_ROOT` is set)
- `SWARMFORGE_REPO_CONFIG_DIR` (default repo-local `opencode/` for `run_opencode`)

At container startup these layers are merged into `/home/opencode/.config/opencode`, so Swarmforge-layer files can override org files, and org files can override personal defaults.
Skills and commands are excluded from that merge and travel through the same asset pipeline Claude Code uses: the user, org, and repo config layers, then the shared `skills/` and `commands/`, then any `<workspace>/.agents/{skills,commands}` overlay.
Each skill package is replaced wholesale by the highest-precedence layer that provides it (never file-merged across layers), which keeps the shared skills exposed by default while still allowing personal and org-specific overlays.

For `opencode.json`, layers are merged by key (not plain file overwrite), which lets org-level MCP servers in `.opencode/opencode.json` remain available even when the Swarmforge repo layer also defines `opencode.json`.

You can also define MCP servers directly in a project-local `.opencode/opencode.json` file. Example:

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

That is often the cleanest place for OpenCode MCP definitions when you want them attached to a specific repo.

OpenCode auto-discovers skills from that directory and uses only the YAML frontmatter (`name` + `description`) for discovery.
The full `SKILL.md` body is loaded on-demand when a skill is invoked, which helps keep the default context small.

When `make run_opencode` starts the container, it now mounts your host `~/.gitconfig` into `/home/opencode/.gitconfig` if the file exists so agents inherit your configured `user.name` and `user.email`.
Point to an alternative identity file with `GITCONFIG_FILE=/path/to/gitconfig make run_opencode`.

Note: `opencode/opencode.json` also supports an `instructions` array for global instruction files.
Those files are loaded in full, so avoid listing full `SKILL.md` files there unless you explicitly want them always in context.

## Tongs (sidecar processes)

A **tong** is a Swarmforge-managed sidecar container started alongside the anvil (the harness container you work in).
The name captures the primary use case: a tool that holds something hot — usually credentials — so the agent never touches it directly.
A credential-holding tong runs as a sibling container exposing an **MCP server** the agent calls over the session network; the secret material lives only in the tong's process space.
Tongs can also be plain network services (a throwaway Postgres, a fixture server), volume providers (a build-cache populator), or background side-effect processes.

Tongs are defined as YAML files and discovered across the **same four layers** as agents.
The host-side launcher (`scripts/run_anvil.py`) discovers, approves, starts, and tears them down; `make run_opencode` / `make run_claude` already delegate to it, so no new commands are needed.

### Quick start: run a tong

1. (Only if the tong needs secrets) create the secret-provider table (see [Secret providers](#secret-providers) below).
2. Drop a tong definition into one of the layer directories, e.g. a personal one at `~/.swarmforge/tongs/<name>.yaml`, or a project one at `<workspace>/.swarmforge/tongs/<name>.yaml`.
3. Run the anvil as usual (`oc`, or `make run_claude PROJECT_DIR=$(pwd)`).
   - A **workspace**-sourced tong prints a privilege summary and asks you to approve it on first run (see [First-run approval](#first-run-approval)).
   - The launcher resolves any secrets (which may prompt your provider CLI to unlock), starts the tong, waits for it to become ready, injects reachability into the anvil, then runs the anvil in the foreground.
4. On exit (including Ctrl-C), `session` tongs and the per-session network are torn down; `shared` tongs are left running.

### Where definitions live

One YAML file per tong under `.swarmforge/tongs/`, merged **by name** (filename = tong identity) lowest to highest precedence:

- **user** — `~/.swarmforge/tongs/` (override the root with `SWARMFORGE_USER_ASSETS_DIR`; `tongs/` is appended)
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/tongs/` (override the root with `SWARMFORGE_ORG_ASSETS_DIR`)
- **repo** — `tongs/` in the Swarmforge checkout (override with `SWARMFORGE_REPO_TONGS_DIR`, which points directly at the directory so the rest of the checkout is never read)
- **workspace** — `<workspace>/.swarmforge/tongs/`

A higher layer replaces a same-named tong wholesale (never a file-merge). `disable: true` switches off an inherited tong.
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
# entrypoint: [...]           # optional argv override (needed only for secret-env tongs
# command: [...]              #   whose image entrypoint/command can't be read via inspect)
```

Required fields: `lifecycle`, `image`, and `interface` (with a valid `kind`). Unknown keys are tolerated for forward compatibility.

#### Interface kinds

The `interface:` block drives what gets injected into the anvil, how readiness is checked, and what plumbing is wired up:

- **`mcp`** — an HTTP MCP server (the common case). Requires `port` and `name`; `transport` defaults to `http`. Injection: per-harness MCP config pointing at `http://<name>:<port>/mcp` on the session network (`interface.path` overrides the `/mcp` suffix). Readiness: TCP probe by default.
- **`port`** — a non-MCP network service. Requires `port`; optional `protocol` is informational. Injection: `SWARMFORGE_TONG_<NAME>_HOST` (the canonical alias) and `SWARMFORGE_TONG_<NAME>_PORT` into the anvil's environment — the anvil composes its own connection string. Readiness: TCP probe by default.
- **`volume`** — a shared named volume, no network. Requires `volume` and `mountpoint`; readiness must be declared (no port to probe). The schema accepts it, but **the launcher does not wire it up yet** and refuses to start such a tong with a clear message — there is no consumer for the shared volume yet.
- **`none`** — a background side-effect with no anvil-facing surface. Injects nothing. Readiness must be declared.

`<NAME>` in the env vars is the tong's filename uppercased with hyphens turned into underscores (`github-creds` → `SWARMFORGE_TONG_GITHUB_CREDS_*`).
The MCP server name and the `port` alias are stable across worktrees (they are docker network aliases, not container names), so generated config is identical regardless of where the workspace is mounted.

#### Readiness

```yaml
readiness:
  mode: healthcheck           # tcp | healthcheck | none
  command: ["test", "-S", "/run/agent.sock"]   # for mode: healthcheck (docker exec)
  timeout: 30s                # 30s / 500ms / 2m, or a bare number of seconds (default 30s)
```

`tcp` is the implicit default for `mcp` and `port`. `volume` and `none` have no port to probe, so `mode` is **required** for them — the launcher refuses to silently fire-and-forget a portless tong. Use `mode: none` to deliberately skip the gate.

#### Mounts

Mounts are opt-in **magic words**, never raw host paths. Only two are recognized, each with an optional `:mode` suffix forwarded to docker verbatim:

- `workspace[:mode]` — bind-mounts the session workspace at `/workspace` (e.g. `workspace:ro`).
- `docker-socket[:mode]` — bind-mounts the host docker socket. This is full host docker control and is always called out explicitly in the workspace approval prompt; it is the grant a broker tong needs.

### Lifecycle

- **`session`** — started with the anvil, torn down when the anvil exits. Per-session isolation; the default for credential tongs.
- **`shared`** — long-lived, survives across anvil sessions (ollama-style). Started on first use and connected to each session's network via a network alias; left running on teardown (no refcounting). A running `shared` container whose config-hash docker label still matches the current definition is reused untouched; a missing, stopped, or stale one is recreated automatically. A rotated secret behind an unchanged reference does **not** churn it — to force a restart (e.g. to pick up a rotated secret) remove the container yourself with `docker rm -f <container>`. A `shared` tong may not mount the `workspace` (it would leak one session's workspace into the next); use a `session` tong for per-workspace mounts.

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

**Delivery is leak-resistant by design.** A resolved secret is never passed as a docker `-e` value, a command-line argument, or a file on disk (anything holding the docker socket could read those back). Instead the launcher streams the secret env to the tong over a host FIFO and wraps the tong's entrypoint with a `/bin/sh` prologue that reads the FIFO, exports the values, then execs the image's real entrypoint — so an unmodified off-the-shelf server that reads its credentials from `process.env` works as-is. A tong with secret env therefore needs a `/bin/sh` in its image; a tong without secrets runs its image entrypoint unchanged. Plain (non-secret) `env:` values still flow through `-e`.

### First-run approval

The user, org, and repo layers are installed deliberately and are trusted; they skip the gate.
A **workspace**-sourced tong (from a repo you cloned) could otherwise request your secrets, host mounts, or the docker socket simply by being present, so the launcher gates it:

- Before starting, it prints exactly what the tong requests — image, secret references, mounts, networks, and docker-socket access — and asks you to approve.
- Approval is keyed by workspace path + tong name + a hash of the merged definition, stored in `~/.swarmforge/approvals.json`. Any change to the definition re-prompts.
- The gate defaults to **No** and a non-interactive stdin reads as No. A scripted `--no-prompt` run **fails closed** rather than auto-approving.
- Approving `image: foo:latest` approves a moving target; **pinned digests are the recommended convention** for workspace tongs.

## Skill Tests

This repo includes a lightweight skill test harness.
It runs scenario prompts against a chosen model and verifies expected behavior.

- Run all skill tests: `make test MODEL=<provider/model>`
- Run a single skill's tests: `make test MODEL=<provider/model> TEST_SKILL=<skill-name>`
- Optional judge mode: `make test MODEL=<student> TEST_ENABLE_JUDGE=1 EVAL_MODEL=<judge>`
- Timeout override: `make test MODEL=<provider/model> TEST_TIMEOUT_S=<seconds>`

Tests live in `skills/<skill-name>/tests/*.json`.
The runner is `scripts/test_skills.py`.

Assertions can be:

- Output patterns: `expect.must_match` and `expect.must_not_match` (regex against formatted output)
- Tool calls: `expect.must_tool` and `expect.must_not_tool` (extracted from `opencode run --format json` events)
