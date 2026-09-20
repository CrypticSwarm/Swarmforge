# Shared assets (skills, commands, agents)

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

Skills and commands follow the harness-neutral `.agents/{skills,commands}` convention. Skills are copied as-is; commands are copied for harnesses with native commands and translated into skills for Codex. Agents use the unified format (see [Agents](authoring/agents.md)) and are translated per harness.
Harness-native dirs (`<layer>/.opencode/skills/`, `<layer>/.claude/skills/`) are not consumed for skills/commands.
Override the `.agents` roots with `SWARMFORGE_USER_DOTAGENTS_DIR` / `SWARMFORGE_ORG_DOTAGENTS_DIR`.
