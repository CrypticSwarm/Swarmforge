# OpenCode

A coding-agent harness that exposes a standard set of code-editing tools to the LLM.

`make run_opencode` merges config into `/home/anvil/.config/opencode` from three sources (lowest to highest precedence — see the note on trust ordering under [Claude config layering](claude-code.md#claude-config-layering)):
- `SWARMFORGE_REPO_CONFIG_DIR` (default repo-local `opencode/`)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.config/opencode`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode` when that root is set)

`opencode.json` is merged by key (not file overwrite), so org-level MCP servers survive even when the repo layer also defines `opencode.json`.
Your own `~/.config/opencode/opencode.json` overrides the toolchain defaults this checkout ships in `opencode/opencode.json`.
Skills and commands are excluded from this merge and travel through the asset pipeline described under [Shared assets](../configuration.md).

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
