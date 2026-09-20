# Skills

Skills live under `skills/` in this checkout, and in the skills dir of every other [asset layer](../configuration.md#asset-layers).
OpenCode auto-discovers them using only the YAML frontmatter (`name` + `description`); the full `SKILL.md` body loads on demand when a skill is invoked, keeping the default context small.
