# Configuration and assets

Two pipelines fill a container at startup: one for assets — skills,
commands, and agents — and one for the config a harness reads as its
settings. They collect their layers from different places and order them on
different grounds.

## Asset layers

Every harness mounts this repo's `skills/` and `commands/` into the container, exported as `SWARMFORGE_SKILLS_DIR` and `SWARMFORGE_COMMAND_DIR`.
At container startup they are copied into each harness's native location: the container-local config dir for Claude (see [The config directory](harnesses/claude-code.md#the-config-directory)), the merged config dir for OpenCode (`~/.config/opencode/skills/`) and Grok (`~/.grok/skills/`), and `~/.agents/skills/` for Codex, whose native user location is the `.agents` convention itself.
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

Skills and commands follow the harness-neutral `.agents/{skills,commands}` convention, and skills are copied as-is. Agents use the unified format (see [Agents](authoring/agents.md)) and are translated per harness.
Harness-native dirs (`<layer>/.opencode/skills/`, `<layer>/.claude/skills/`) are not consumed for skills/commands.
Override the `.agents` roots with `SWARMFORGE_USER_DOTAGENTS_DIR` / `SWARMFORGE_ORG_DOTAGENTS_DIR`, and the `.swarmforge` roots with `SWARMFORGE_USER_ASSETS_DIR` / `SWARMFORGE_ORG_ASSETS_DIR`. `SWARMFORGE_REPO_AGENTS_DIR` overrides the repo layer's agents dir.

## Config layers

Every harness merges its own config from three sources, lowest to highest precedence:

- **repo** — `SWARMFORGE_REPO_CONFIG_DIR`, this checkout's directory for the harness, applied if present
- **user** — `SWARMFORGE_USER_CONFIG_DIR`, your own host config for the harness
- **org** — `SWARMFORGE_ORG_CONFIG_DIR`, optional; defaults to the harness's own directory under `$(SWARMFORGE_ORG_CONFIG_ROOT)` when that root is set

| Harness | repo | user | org, under `SWARMFORGE_ORG_CONFIG_ROOT` |
| --- | --- | --- | --- |
| [OpenCode](harnesses/opencode.md) | `opencode/` | `~/.config/opencode` | `.opencode` |
| [Claude Code](harnesses/claude-code.md) | `claude/` | `~/.claude` | `.claude` |
| [Grok Build CLI](harnesses/grok.md) | `grok/` | `~/.grok` | `.grok` |
| [Codex CLI](harnesses/codex.md) | `codex/` | `~/.codex` | `.codex` |

The two orders differ over this checkout: assets order by specificity, so the checkout outranks org and user while the workspace outranks the checkout, whereas config orders by **trust**, because these files carry permissions, hooks, and env. A checkout is whatever repo you cloned and sits at the bottom; the org layer is installed deliberately and sits on top.

A higher layer's file replaces the same-named file below it rather than merging into it. Where a merge lands, which files are the exception to that file-replacement rule and merge by key instead, and whether a harness stacks a layer of its own beneath these three for one of those files, are per harness.

`.swarmforge/` is held out of this merge for every harness, as are the skills and commands dirs the asset pipeline fills, so those assets arrive by one route.
Project-local config in the working repo (for example `.opencode/`) is no part of the merge either; the harness reads it natively.
