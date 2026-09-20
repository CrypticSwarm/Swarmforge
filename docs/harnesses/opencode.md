# OpenCode

A coding-agent harness that exposes a standard set of code-editing tools to the LLM.

`make run_opencode` merges the three [config layers](../configuration.md#config-layers) into `/home/anvil/.config/opencode`. Skills and commands come separately, through the [asset pipeline](../configuration.md#asset-layers).

`opencode.json` is merged by key (not file overwrite), so org-level MCP servers survive even when the repo layer also defines `opencode.json`.
Your own `~/.config/opencode/opencode.json` overrides the toolchain defaults this checkout ships in `opencode/opencode.json`.

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
