# Swarmforge documentation

## Running a session

- [Installation](installation.md) — the install script, pinning an OpenCode release, the repo-local env file, several aliases against one checkout.
- [swarmforge CLI](cli.md) — `swarmforge clone` and `swarmforge init`, and the worktree layout they create.
- [Git repos and worktrees](git-guard.md) — the read-only git mounts every session gets, what they stop, and what they do not.
- [Ollama](ollama.md) — running models locally on the shared network.

## Harnesses

- [OpenCode](harnesses/opencode.md)
- [Claude Code](harnesses/claude-code.md)
- [Grok Build CLI](harnesses/grok.md)
- [Codex CLI](harnesses/codex.md)

## Configuration and assets

- [Configuration and assets](configuration.md) — the four layers skills, commands, and agents are collected from, the three a harness's config is merged from, and why this checkout ranks differently in each.
- [Agents](authoring/agents.md) — the unified subagent format and what each harness makes of it.
- [Skills](authoring/skills.md)
- [Commands](authoring/commands.md)

## Tongs

- [Tongs](tongs/README.md) — what a sidecar tong is, running one, and where definitions live.
- [Definition format](tongs/definitions.md)
- [Secret providers](tongs/secrets.md)
- [First-run approval](tongs/approval.md)
- [Broker tongs](tongs/broker.md)

## Working on Swarmforge

- [Harness lifecycle](development/architecture.md) — what a harness directory holds and the phases every run walks.
- [Testing](development/testing.md) — the unit suite, the linter, and the skill evals.

## Reading paths

- **First session in a container** — [Installation](installation.md), then your harness's page, then [Git repos and worktrees](git-guard.md) for what the read-only mounts change about git.
- **Giving an agent a credential without handing it over** — [Tongs](tongs/README.md), [Definition format](tongs/definitions.md), [Secret providers](tongs/secrets.md).
- **Sharing skills, commands, and agents across repos** — [Configuration and assets](configuration.md), then [Agents](authoring/agents.md).
- **Adding a harness or changing startup** — [Harness lifecycle](development/architecture.md), then [Testing](development/testing.md).
