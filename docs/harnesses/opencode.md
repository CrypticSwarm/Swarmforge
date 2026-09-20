# OpenCode

A coding-agent harness that exposes a standard set of code-editing tools to the LLM.

`make run_opencode` merges the three [config layers](../configuration.md#config-layers) into `/home/anvil/.config/opencode`. Skills and commands come separately, through the [asset pipeline](../configuration.md#asset-layers).

`opencode.json` is merged by key (not file overwrite), so org-level MCP servers survive even when the repo layer also defines `opencode.json`.

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

`opencode/opencode.json` also supports an `instructions` array for global instruction files, which load in full — a `SKILL.md` listed there is in context for every session instead of loading on demand.
