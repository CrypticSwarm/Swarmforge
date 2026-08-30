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

2. Build the container images you want:

```
make build_opencode
make build_claude
make build_grok
make build_codex
```

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

The images share the same Debian base and toolchain (Node.js + Python; see `anvil/Dockerfile`).
Build targets pass `AGENT=opencode|claude|grok|codex` so only the requested agent install step runs.
All four build the same `harness-runtime` stage, selected by `--build-arg AGENT=...`; the Dockerfile
has no per-harness stages, so a hand-run `docker build` names `--target harness-runtime` with the
matching `AGENT` build arg rather than `--target claude-runtime` or any other `<name>-runtime`.

3. Run from your project directory:

- OpenCode: `oc`
- Claude Code: `make run_claude PROJECT_DIR=$(pwd)`
- Grok Build: `make run_grok PROJECT_DIR=$(pwd)`
- Codex CLI: `make run_codex PROJECT_DIR=$(pwd)`
- Pass OpenCode overrides as arguments (`oc PROFILE=work DATA_DIR=...`) or env vars (`PROFILE=work oc`).
- Override the container timezone per run (affects git commit timestamps): `oc TIMEZONE=America/New_York`.

### Repo-local env vars

The `run_*` harness targets load a repo-local env file from `.swarmforge/env` if it exists; override with `ENV_FILE=/path/to/env`.
They also accept `TIMEZONE=<Region/City>` (default `Etc/UTC`), passed into the container as `TZ`.

### Multiple aliases (work/personal)

Define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys):

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

- `GITCONFIG_FILE` points at an agent-specific git config instead of `~/.gitconfig`.
- For Claude Code, use separate `CLAUDE_DATA_DIR` roots to isolate work/personal logins and session state. `CLAUDE_HOME_DIR` defaults to `$(CLAUDE_DATA_DIR)/home`.
- Config layering uses `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR` (their defaults differ per harness — see OpenCode layering under [Skills](#skills) and [Claude config layering](#claude-config-layering)). Set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` to resolve org defaults to each harness's own directory under that root (`.opencode`, `.claude`, `.grok`, `.codex`).

`SWARMFORGE_REPO_CONFIG_DIR` refers to the Swarmforge checkout (the harness repo), not the working project mounted at `/workspace`.
By default each `run_*` target points it at that harness's directory in the checkout: `$(SWARMFORGE_DIR)/opencode`, and `$(SWARMFORGE_DIR)/claude`, `/grok`, `/codex` if present.
Project-local config in the working repo (for example `.opencode/`) is still handled by the agent tools themselves.

### Git repos and worktrees

The `run_*` harness targets auto-detect the git root from `PROJECT_DIR` and mount it at `/workspace`.
For a linked git worktree they also mount the shared git common directory so git operations keep working inside the container.
This means `oc` works from repo roots, subdirectories, and linked worktrees without extra flags.

`.git/config` and `.git/hooks` are mounted read-only wherever the git dir is visible in the container.
Both execute on the *host* — hooks run on your next commit or checkout, and config carries `core.hooksPath`, `core.pager`, `core.sshCommand` and aliases — so the agent gets no write access to them.
`swarmforge/gitguard.py` builds those mounts, covering every git dir reachable from the workspace: the repo's own, a linked worktree's shared common dir, and the git dirs of initialized submodules — each with their own submodules and worktrees, including a submodule initialized inside a worktree, whose git dir git keeps under that worktree rather than the repository.
`remotes/` and `branches/`, the pre-config way to define a remote, are read-only for the same reason as `config`.
It also guards the pointers that say where config and hooks live (`commondir`, a `.git` that is a `gitdir:` file, and `config.worktree` where `extensions.worktreeConfig` is on), and binds every directory on the way down onto itself, since a plain directory containing a read-only mount can still be renamed aside and recreated writable.
A guarded path that is absent is created on the host first so there is no gap to slip through — a repo with no `config` works fine, which makes its absence room to write one rather than a sign there is nothing to guard.
The placeholders are inert, though a repo that gains a `commondir` starts answering `git rev-parse --git-common-dir` with an absolute path instead of `.git`.
Only git dirs that exist when the session starts are covered — a repo the agent clones or `git init`s inside the workspace, or an unrelated checkout vendored there, is not.
A `.git` written into an existing subdirectory is worth knowing about specifically: it shadows the guarded repo for anything run from inside that directory, `git status` at the root neither reports it nor executes it, and a git-aware shell prompt or editor entering the directory is enough to run what its config says. `safe.directory`, git's gate for this, keys on ownership, and the container runs as your own uid.

The rest of the git dir stays writable, so committing, branching, fetching, and `git worktree add` work as usual.
Commands that write config do not, by design: `git config --local`, `git remote add`, `git submodule update --init`, and `git sparse-checkout` fail with `could not write config file ...: Device or resource busy`, and hook installers like `pre-commit install` or husky fail on the read-only `.git/hooks`.
Branch tracking is the sharp edge: `git push -u` and `git switch <remote-branch>` exit 0 and still report "set up to track", but the tracking config is silently not recorded — git treats that write failing as non-fatal. Use `git push origin HEAD:<branch>` and `git switch -c <name> --no-track origin/<branch>`, and set a repo up on the host when it needs to stick.
This narrows the git-specific surface; it does not make the workspace a trust boundary. Hooks that config already points *outside* the git dir (`core.hooksPath = .githooks`, husky) and attribute-driven filter commands live in the workspace, as do `package.json` scripts and `Makefile`s — anything you run on the host from a directory an agent could write is still yours to trust.

## Ollama

Run LLMs locally. `make run_ollama` starts an Ollama container on the shared network (`make stop_ollama` / `make clean` to tear down).
The `run_*` model targets (for example `make run_gpt-oss-20b`) exec into it to pull and run a model; `make gpu_stat` wraps `nvidia-smi`.

## OpenCode

A coding-agent harness that exposes a standard set of code-editing tools to the LLM.

## Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting as `make run_opencode`.
Claude state persists by mounting `$(CLAUDE_HOME_DIR)` to `/home/anvil`, keeping account/session files like `~/.claude/` and `~/.claude.json`.
The repo is mounted at a stable path derived from the git remote slug (with `/workspace` still mounted for compatibility), which groups sessions consistently across worktrees without host-specific absolute paths.

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- Remote slugs map deterministically, e.g. `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`. Override with `SWARMFORGE_REPO_SLUG=crypticswarm/Swarmforge` and `SWARMFORGE_REMOTE_NAME=<remote>`.

### Shared assets (skills, commands, agents)

Every harness mounts this repo's `skills/` and `commands/` into the container, exported as `SWARMFORGE_SKILLS_DIR` and `SWARMFORGE_COMMAND_DIR`.
At container startup they are copied into each harness's native location: the container-local config dir for Claude (see [The config directory](#the-config-directory)), the merged config dir for OpenCode (`~/.config/opencode/skills/`) and Grok (`~/.grok/skills/`), and `~/.agents/skills/` for Codex, whose native user location is the `.agents` convention itself.
For Claude, Grok, and Codex those dirs are container-private and rebuilt each run, so per-repo assets never accumulate in the persistent home or leak into other repos' sessions.
Codex has no user-defined slash commands, so portable commands become
same-named skills. Translation removes command-only metadata and adapts
arguments and shell interpolation. A native skill wins over a translated
command in the same layer; normal layer precedence still applies.

Skills, commands, and agents come from four layers, lowest to highest precedence — later layers override same-named entries wholesale (never file-merged):

- **user** — `~/.agents/{skills,commands}` and `~/.swarmforge/agents/`
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.agents/{skills,commands}` and `.../.swarmforge/agents/`
- **repo** — this checkout's `skills/`, `commands/`, and `agents/`
- **workspace** — `<workspace>/.agents/{skills,commands}` and `<workspace>/.swarmforge/agents/`

Skills and commands follow the harness-neutral `.agents/{skills,commands}` convention. Skills are copied as-is; commands are copied for harnesses with native commands and translated into skills for Codex. Agents use the unified format (see [Agents](#agents)) and are translated per harness.
Harness-native dirs (`<layer>/.opencode/skills/`, `<layer>/.claude/skills/`) are not consumed for skills/commands.
Override the `.agents` roots with `SWARMFORGE_USER_DOTAGENTS_DIR` / `SWARMFORGE_ORG_DOTAGENTS_DIR`.

### Claude config layering

Three sources merge into Claude's config dir at startup (lowest to highest precedence):
- `SWARMFORGE_REPO_CONFIG_DIR` (default `claude/`, if present)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.claude`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude` when that root is set)

Skills, commands, and `agents/` are excluded from this merge — they travel through the asset pipeline above.

Config layers stack in the opposite order to the assets above: assets order by specificity, so a repo's own skill wins, while config orders by **trust**, because these files carry permissions, hooks, and env. A checkout is whatever repo you cloned and sits at the bottom; the org layer is installed deliberately and sits on top.

#### The config directory

Claude runs with `CLAUDE_CONFIG_DIR` pointed at a container-local path, rebuilt from the config layers and the asset pipeline on every run. Everything Claude reads as configuration or code lives in that directory, so a shared one would hand a session's writes to the next container and to any running alongside it.

State that must outlive the run (`projects/`, `history.jsonl`, …) is symlinked back in from the shared home; the allowlist is `STATE_DIRS`/`STATE_FILES` in `swarmforge/harness/claude/`. It fails safe — a directory Claude learns to load in a later release stays inert until listed — at the cost that an unlisted new state directory dies with the container. A link holds only what Claude writes in place: an entry it rewrites by rename replaces the link with a container-local file.

Credentials are that second kind, so `CLAUDE_SECURESTORAGE_CONFIG_DIR` names their store instead: `~/.claude` in the shared home. The rename lands on the persistent mount, and Claude's token-refresh lock sits in the same directory, so concurrent containers rotate the shared token one at a time.

`plugins/` is linked but mounted read-only: marketplace clones are worth keeping, but a session must not rewrite what the next container executes, so plugin installs happen host-side.

#### settings.json

`settings.json` is the exception to the file-replacement rule above: like `opencode.json`, it is merged **by key**, and it is rebuilt from scratch on every run rather than merged into whatever the last run left behind. Below the three layers sits a fourth the image ships (`swarmforge/harness/claude/claude-settings.json`), which is where the status line default comes from.

The result never touches the host or the shared home: it is built at a container-local path during the config phase, and `--settings <path> --setting-sources user,project,local` is spliced onto claude's command line ahead of the session's own arguments. `user` stays in the sources because that scope carries skills, commands, and agents discovery. Three consequences:

- The built file sits at command-line precedence, above the workspace's own `.claude/settings.json` and `settings.local.json` (both still load natively) — a key an org layer sets cannot be overridden from a checkout.
- A key edited from inside a session (`/config`, the statusline-setup skill) lands in the container-local `settings.json` and dies with the container. Put it in a config layer instead.
- Under `CLAUDE_HOME_DIR=$HOME`, your real `~/.claude/settings.json` is read as the user config layer and never written.

A layer whose `settings.json` is not valid JSON, or not a JSON object, is skipped with a message on stderr; the rest of the layers still apply.

This covers `settings.json` only. Every other layer file — a `CLAUDE.md`, a hooks script, `settings.local.json` — is replaced wholesale in the container-local config dir and dies with it.

### Status line

`make build_claude` bakes `swarmforge/harness/claude/statusline.sh` into the image at `/usr/local/bin/swarmforge-statusline`, and the image defaults layer points `statusLine` at it — so a container shows the model, directory, turn count, context percentage, and session token/cost totals with no host setup. It reads the session JSON on stdin and the transcript.

Being the lowest layer, its `statusLine` is overridden key by key — a layer that sets both `type` and `command`, as any real one does, replaces it entirely:

```json
{
  "statusLine": { "type": "command", "command": "~/.claude/my-statusline.sh" }
}
```

## Grok Build CLI

`make run_grok` starts a [Grok Build](https://x.ai/news/grok-build-cli) container with the same workspace, git-worktree, and repo-slug mounting as `make run_claude`.
The image installs the official xAI CLI via `curl -fsSL https://x.ai/cli/install.sh | bash` and relocates the binary to `/usr/local/bin/grok`.
Grok state persists by mounting `$(GROK_HOME_DIR)` to `/home/anvil`, keeping `~/.grok/` (account and session files such as `config.toml` and the credentialed user-settings JSON).
`GROK_HOME_DIR` defaults to `$(GROK_DATA_DIR)/home`; use separate `GROK_DATA_DIR` roots to isolate work/personal logins, as with `CLAUDE_DATA_DIR`.

Grok reads the repo-root `AGENTS.md` family natively from the git root down, so it picks up this repo's instructions with no extra config.
Shared skills reach `~/.grok/skills/`, Grok's native location, through the [asset pipeline](#shared-assets-skills-commands-agents) above.
Subagent definitions are not translated for Grok; the unified-agent pipeline covers OpenCode, Claude, and Codex.
MCP tongs reach Grok as `[mcp_servers.<name>]` entries in a managed block of the merged `~/.grok/config.toml` — user-level config, so no folder-trust prompt. That file is in the persistent home, so the block is rewritten every run and stripped when a session has no MCP tongs; a server the user already defines under the same name wins over the generated entry.

Grok config layering uses the same three sources and order of trust as Claude (lowest to highest precedence):
- `SWARMFORGE_REPO_CONFIG_DIR` (default `grok/`, if present)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.grok`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.grok` when that root is set)

These merge into `~/.grok` in the container at startup, with reset disabled so credentials survive the run. Rebuild only the Grok install layer with `make update_grok`.

## Codex CLI

`make run_codex` starts an [OpenAI Codex CLI](https://developers.openai.com/codex/cli) container with the same workspace, git-worktree, and repo-slug mounting as `make run_claude`.
The image installs the official CLI via `curl -fsSL https://chatgpt.com/codex/install.sh | sh`.
That release is a package rather than a lone binary -- `bin/codex` resolves ripgrep, `bwrap`, and a bundled zsh beside itself -- so it stays whole under `/opt/codex` and the installer's symlink is what lands on `PATH`.
Codex state persists by mounting `$(CODEX_HOME_DIR)` to `/home/anvil`, keeping credentials, sessions, and the project trust levels a stable mount path keeps valid.
`CODEX_HOME_DIR` defaults to `$(CODEX_DATA_DIR)/home`; use separate `CODEX_DATA_DIR` roots to isolate work/personal logins, as with `CLAUDE_DATA_DIR`.

Codex reads the repo-root `AGENTS.md` family natively from the git root down, so it picks up this repo's instructions with no extra config.
Shared skills reach `~/.agents/skills/`, Codex's native user location, through the [asset pipeline](#shared-assets-skills-commands-agents) above. Portable commands reach the same location as translated skills.
Unified subagent definitions become temporary Codex role files under
`/run/swarmforge/codex-agents/` and are registered through the derived
`~/.codex/config.toml`. The checkout's native `.codex/agents/` is untouched.
MCP tongs reach Codex as `[mcp_servers.<name>]` entries in a managed block of the derived `~/.codex/config.toml`, rewritten from the current layers every run and yielding to a server the user already defines under that name.

Codex config layering uses the same three sources and order of trust as Claude (lowest to highest precedence):
- `SWARMFORGE_REPO_CONFIG_DIR` (default `codex/`, if present)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.codex`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.codex` when that root is set)

Each launch builds `config.toml` from scratch in repo → user → org order,
merging by key, and copies it to Codex's native path. The canonical output
preserves values and tables, but not comments or formatting. The native file
remains writable for Codex's atomic settings updates, but the next launch
rebuilds it; put durable settings in a source layer. Rebuild only the Codex
install layer with `make update_codex`.
The merge skips `packages/` -- the host installer's release tree, which the container has no use for -- along with `sessions/`, `history.jsonl`, and `log/`, so one machine's transcripts do not follow the user config layer into the container's home.

Codex brings its own sandbox, which is redundant inside an anvil and may not initialize in one at all, since its Landlock and `bwrap` paths need kernel permissions a container is not guaranteed.
Relax it per run with `CODEX_ARGS='--dangerously-bypass-approvals-and-sandbox'`, or per install by setting `sandbox_mode` in a config layer.

## Harness lifecycle

A harness is one directory, `swarmforge/harness/<name>/`, holding everything Swarmforge knows about it: the spec module (`__init__.py`, a `HarnessSpec` plus the hook functions it points at), the `harness.mk` fragment the Makefile includes to generate `build_<name>`, `update_<name>`, `run_<name>`, and `stop_<name>`, the `install.sh` the image build runs, and any build-time assets with the `image.sh` that installs them (Claude ships `statusline.sh` and `claude-settings.json` this way). `swarmforge/harness/__init__.py` maps each name to its module in a static dict, so the set of harnesses is greppable and closed at image build time.

The image build runs the harness's `install.sh`, which leaves its binary under `/usr/local/bin/`, then its `image.sh` when it declares one.

Every run walks the same phases against that spec:

- **initialize** — merge the three config layers into the harness's config destination, run its config hooks, and merge the tong MCP servers the way its `mcp_merge` names.
- **translate-agents** — write the unified agent definitions into the harness's native format and destination.
- **install-assets** — install the portable skills and commands into its native asset locations, layer by layer.
- **link-state** — link the state that has to outlive the container back into the config destination.
- **root-setup** — whatever container preparation the harness needs root for (Claude's git worktree wrapper).
- **handover** — chown the home, the paths the harness builds outside it, and the workspace to the anvil uid.
- **pre-exec** — after privileges drop, the harness's `pre_exec` hook has the last word on the argv and the environment the binary is exec'd with.

`anvil/entrypoint.sh` carries no harness-specific logic: it configures the timezone, creates the user, and invokes the two drivers — `swarmforge.harness.init` as root, then `swarmforge.harness.execute` as the anvil user.

Adding a harness is one new directory and one line in the registry dict. `tests/test_harness_conformance.py` runs over the registry, so the new spec's completeness, its behavior in each phase, and its recorded run argv are checked with no test to write.

## Agents

Subagent definitions live under `agents/` in a single unified format and are rewritten to each harness's native dialect at container startup, by `swarmforge/agents/translate.py` through the emitter the harness's spec declares.

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
codex:
  model: gpt-5.3-codex
  model_reasoning_effort: high
  sandbox_mode: read-only
---

You are the reviewer agent...
```

Field handling per harness:

- `description` and the prompt body pass through everywhere.
- `tools` uses OpenCode's lowercase tool ids mapped to booleans. For Claude Code, disabled tools become `disallowedTools` (`write: false` -> `disallowedTools: Write`); ids with no Claude equivalent are dropped.
- `model` accepts a provider-qualified id (`anthropic/claude-sonnet-4-6`, passed through to OpenCode and stripped to the bare id for Claude — non-Anthropic providers dropped) or a Claude alias (`sonnet`, `haiku`, Claude-only and dropped for OpenCode).
- `mode`, `temperature`, and other OpenCode-only fields are dropped for Claude Code.
- For Codex, unqualified models pass through, `openai/` prefixes are stripped, and other providers are dropped. Names and `.toml` filenames are normalized to Codex's supported ASCII characters. Generic `tools` restrictions are dropped; use Codex sandbox and MCP settings instead.
- `claude:`, `codex:`, and `opencode:` blocks merge into that harness's output. Put Codex-only fields such as `model_reasoning_effort` and `sandbox_mode` in `codex:`.
- `disable: true` passes through to OpenCode and skips the agent for Claude Code and Codex.

Unified agents live in harness-neutral `.swarmforge/agents/` directories across the same four layers as shared assets (lowest to highest precedence):

- **user** — `~/.swarmforge/agents/` (override the `.swarmforge` root with `SWARMFORGE_USER_ASSETS_DIR`)
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/agents/` (override with `SWARMFORGE_ORG_ASSETS_DIR`)
- **repo** — `agents/` in the checkout (override with `SWARMFORGE_REPO_AGENTS_DIR`, which points directly at an agents dir so the rest of the checkout is never mounted)
- **workspace** — `<workspace>/.swarmforge/agents/`

Layers mount read-only under `/tmp/swarmforge-assets/{user,org}` and `/tmp/swarmforge-assets/repo/agents` (the in-container `SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR` env vars point at the layer roots). Startup translates them into `~/.config/opencode/agents/` for OpenCode, `agents/` inside Claude's container-local config dir (the one `CLAUDE_CONFIG_DIR` names), and temporary registered role files for Codex. Later layers override earlier ones by filename.
Claude-native repo-local definitions (for example `<workspace>/.claude/agents/`) are still discovered by Claude directly, outside this pipeline.

The translator is covered by the unit suite; run it with `make test`.

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

`make run_opencode` merges config into `/home/anvil/.config/opencode` from three sources (lowest to highest precedence — see the note on trust ordering under [Claude config layering](#claude-config-layering)):
- `SWARMFORGE_REPO_CONFIG_DIR` (default repo-local `opencode/`)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.config/opencode`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode` when that root is set)

`opencode.json` is merged by key (not file overwrite), so org-level MCP servers survive even when the repo layer also defines `opencode.json`.
Your own `~/.config/opencode/opencode.json` overrides the toolchain defaults this checkout ships in `opencode/opencode.json`.
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
The host-side launcher (`swarmforge/anvil/`, run through `bin/run-anvil`) discovers, approves, starts, and tears them down; `make run_opencode` / `make run_claude` already delegate to it.

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
# aliases: [gh, git.example]  # optional extra DNS names the tong also answers to
mounts:                       # opt-in magic words only, never raw host paths
  - workspace:ro              # or workspace:/code:ro to bind it somewhere else
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

#### Extra aliases

A network-facing tong (`mcp` or `port`) may declare additional DNS names it answers to on the session network:

```yaml
interface:
  kind: port
  port: 3000
  aliases: [api, console, local.example.test]
```

Use this when something dialing the tong hardcodes a hostname of its own — a vhost another container expects, or the CN on a TLS certificate a client must match. Each entry must be a valid DNS name (letters, digits, hyphens and dots) and is registered as a further `--network-alias`; the canonical alias is unaffected and stays the name injected into the anvil (`SWARMFORGE_TONG_<NAME>_HOST`, the MCP URL). Extra aliases participate in the same collision check as canonical ones — two tongs on the session network may not claim the same name, whether canonical or extra. `volume` and `none` tongs have no listener and reject the field.

#### Readiness

```yaml
readiness:
  mode: healthcheck           # tcp | healthcheck | none
  command: ["test", "-S", "/run/agent.sock"]   # for mode: healthcheck (docker exec)
  timeout: 30s                # 30s / 500ms / 2m, or a bare number of seconds (default 30s)
```

`tcp` is the implicit default for `mcp` and `port`. `volume` and `none` have no port to probe, so `mode` is **required** for them; use `mode: none` to deliberately skip the gate.

#### Mounts

Mounts are opt-in **magic words**, never raw host paths. Only two are recognized, spelled `<word>[:/target][:mode]` with the access mode (`ro`/`rw`) last:

- `workspace[:/target][:mode]` — bind-mounts the session workspace, at `/workspace` unless an absolute `target` says otherwise (e.g. `workspace:ro`, `workspace:/code`, `workspace:/code:ro`). A custom target lets an image that expects its sources elsewhere be used unmodified (it does not set the working directory — the process still starts in the image's own `WORKDIR`). A target is refused unless it is an absolute path free of whitespace, and refused if it resolves to `/`, overlaps another of the tong's mounts, or overlaps a path the tong's own wiring occupies — the secret-delivery tmpfs at `/run/swarmforge` and the `/bin/sh` its wrapper execs (for a tong with secret references), or the docker socket (for a tong that mounts it). The workspace bind is paired with the same git-dir mounts the anvil gets from `swarmforge/gitguard.py`: read-only guards over the config and hooks the host's git obeys, and — when the workspace is a linked worktree or another checkout whose git dir lives outside it — that git dir at its own absolute path, which is where the checkout's `.git` pointer file says to look (without it, git inside the tong fails with "not a git repository"). When every `workspace` mount is `ro`, the ride-along git-dir mounts are forced read-only too.
- `docker-socket[:mode]` — bind-mounts the host docker socket onto the same path inside the container (so it takes no target). This is full host docker control and is always called out explicitly in the workspace approval prompt; it is the grant a broker tong needs.

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

**Delivery is leak-resistant by design.** A resolved secret is never passed as a docker `-e` value, a command-line argument, or a file on disk (anything holding the docker socket could read those back). Instead the launcher wraps the tong's entrypoint with a `/bin/sh` prologue that creates a FIFO on a tmpfs inside the container, reads it, exports the values, then execs the image's real entrypoint — so an unmodified off-the-shelf server that reads its credentials from `process.env` works as-is. The launcher streams the secret env into that FIFO over `docker exec` stdin, which docker carries on its API stream; because nothing crosses the host filesystem, delivery behaves the same on native Linux and under Docker Desktop's VM (macOS/Windows). A tong with secret env therefore needs `/bin/sh`, `mkfifo`, `cat`, and `rm` in its image; a tong without secrets runs its image entrypoint unchanged. Plain (non-secret) `env:` values still flow through `-e`.

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

## Unit Tests

The launcher, the tongs layer, and the container-side translators are covered by stdlib `unittest` tests in `tests/test_*.py`. A test module is named for the source module it covers — `tests/test_tongs_<module>.py` for `swarmforge/tongs/<module>.py`, `tests/test_anvil_<module>.py` for `swarmforge/anvil/<module>.py` — so the file that covers a change is the one named after it. Two modules have no namesake file because they have nothing to assert on their own: `swarmforge/anvil/readiness.py` is exercised through `run_with_tongs`, and `swarmforge/anvil/errors.py` holds one exception class. Fixtures that more than one test module needs live in `tests/tongs_fixtures.py` and `tests/anvil_fixtures.py`, which the discovery glob skips.

Two files assert on the shape of the repo rather than on any one module. `tests/test_image_layout.py` holds the Dockerfile and the entrypoint to the same import root, and `tests/test_package_layering.py` keeps the package's imports acyclic and keeps loading a module from a file path out of everything but the `bin/` shims. Both fail the way a build should — before anything reaches a container.

- Run them: `make test`

The target is `python3 -m unittest discover -s tests -p 'test_*.py'` with the repo root on `PYTHONPATH`, and CI runs the same discovery. Nothing names test modules by hand, so a new `tests/test_*.py` file runs the moment it lands. It needs only a host python — no Docker, no network, no model.

## Lint

- Run it: `make lint`

`ruff check` over every Python file in the repo, configured in `pyproject.toml` — including the extensionless commands in `bin/`, which ruff would otherwise skip. The rule set is ruff's default — the pycodestyle checks that catch mistakes plus all of pyflakes — and stops there on purpose: line length, import order, and whitespace are left to the author, so turning the linter on does not reflow files a change never touched. Only `ruff check` is ever run; `ruff format` is not part of this repo. Install ruff with `pipx install ruff` (CI pins the version), or point the target at another copy with `make lint RUFF=<path>`.

Ruff is a contributor tool, not a dependency: the harness image installs no third-party Python, and every module under `swarmforge/` stays stdlib-only.

## Skill Tests

A lightweight skill test harness runs scenario prompts against a chosen model and verifies expected behavior. It drives a real model inside the OpenCode image, which is why it is a separate target from the unit suite.

- Run all skill tests: `make test-skills MODEL=<provider/model>`
- Run a single skill's tests: `make test-skills MODEL=<provider/model> TEST_SKILL=<skill-name>`
- Optional judge mode: `make test-skills MODEL=<student> TEST_ENABLE_JUDGE=1 EVAL_MODEL=<judge>`
- Timeout override: `make test-skills MODEL=<provider/model> TEST_TIMEOUT_S=<seconds>`

Tests live in `skills/<skill-name>/tests/*.json`; the runner is `scripts/skill_eval.py`.
Assertions can be:

- Output patterns: `expect.must_match` and `expect.must_not_match` (regex against formatted output)
- Tool calls: `expect.must_tool` and `expect.must_not_tool` (extracted from `opencode run --format json` events)
