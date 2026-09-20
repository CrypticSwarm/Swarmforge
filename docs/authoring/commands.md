# Commands

Slash commands live under `commands/` in this checkout, and in the commands dir of every other [asset layer](../configuration.md#asset-layers).
Start your prompt with the command name to inject it (for example `/commit` injects [`commands/commit.md`](../../commands/commit.md)).

Command files can include `!` shell-expansion blocks, for example:

```
!`git status --short`
```

The harness runs these and injects their output into the prompt context, so the agent sees live repo state without copy/pasting.
