---
name: skill-writer
description: Guidance for creating, editing, or maintaining agent skills; invoke this whenever you create, revise, or otherwise modify a skill package.
---

# Skill Writer

Use this guide whenever you need to create, revise, or maintain a skill (a modular package that expands the agent with domain knowledge, workflows, or tooling). Skills act like onboarding kits: they teach the agent how to approach a recurring problem reliably.

## Skills at a Glance

- **Specialized workflows** – codified, repeatable procedures for fragile or multi-step tasks.
- **Tool integrations** – instructions for working with specific APIs, formats, or scripts.
- **Domain expertise** – company, product, or dataset knowledge that the base model lacks.
- **Bundled resources** – scripts, references, and assets reused across sessions.

Keep every skill lean. Assume the agent already knows general programming, writing, and reasoning; only include content that is unique to the domain or workflow.

## Core Principles

1. **Protect the context window.** Every token in a skill competes with live conversation history. Prefer short, actionable guidance over essays. Use tables, bullets, or concise examples instead of prose.
2. **Match specificity to task fragility.**
   - High freedom → text instructions and heuristics (open problem spaces).
   - Medium freedom → pseudocode or parameterized scripts (preferred patterns, but variations allowed).
   - Low freedom → exact scripts/commands (fragile operations, compliance, or prod changes).
3. **Progressive disclosure.** Organize content so only the necessary layer loads:
   1. YAML frontmatter (`name`, `description`) – always visible, describes when to trigger.
   2. `SKILL.md` body – core workflows (<500 lines).
   3. Optional `scripts/`, `references/`, `assets/` – loaded only when invoked.

## Anatomy of a Skill

```
skill-name/
├── SKILL.md            # required frontmatter + guidance
├── scripts/            # optional executable helpers
├── references/         # optional docs/schemas/examples
└── assets/             # optional templates or files used in output
```

### SKILL.md
- YAML frontmatter **must** contain `name` and `description` only.
- Description doubles as the trigger text. Include what the skill covers **and** the cues that should activate it.
- Body should focus on workflows, decision trees, and how to use bundled resources. Avoid "When to Use This Skill" sections (that belongs in `description`).

### Optional Bundled Resources

| Directory    | Include when…                                          | Notes |
|--------------|--------------------------------------------------------|-------|
| `scripts/`   | Code must be reused verbatim or requires determinism   | Keep lightweight; test representative scripts |
| `references/`| Documentation/schema/policies exceed a few paragraphs  | Link from `SKILL.md` with guidance on when to read |
| `assets/`    | Files must be copied into deliverables or edited       | Templates, icons, fonts, boilerplate projects |

Keep references one hop away from `SKILL.md` (no deep nesting). For reference files >100 lines, add a mini table of contents so the agent can skim quickly.

## What Not to Include

Do **not** add auxiliary docs like `README.md`, changelogs, or installation guides. Skills should contain only what another agent instance needs to execute the work - no meta commentary.

## Skill Writing Workflow

Follow these steps in order unless you have explicit reason to skip one:

1. **Collect concrete examples.** Ask users for realistic requests the skill must solve. Identify triggers and boundaries.
2. **Plan reusable resources.** For each example, note which scripts, references, or assets would save time when repeated.
3. **Initialize the skeleton.** Run tooling (e.g., `scripts/init_skill.py <skill-name> --path <dir>`) to scaffold `SKILL.md`, `scripts/`, `references/`, and `assets/`.
4. **Implement resources first.** Create/update scripts, references, and assets identified in step 2. Delete template files you do not need.
5. **Write `SKILL.md`.** Use imperative voice, emphasize decision points, reference supporting files explicitly ("For tracked changes see `references/redlining.md`"). Keep it <500 lines; move details to references when longer.
6. **Package & validate.** Run `scripts/package_skill.py <path/to/skill>` (optionally with an output dir). Fix any validation errors (frontmatter, structure, description quality) before distributing.

## Design Patterns

- **High-level guide with references:** Keep `SKILL.md` to workflow overview; link to `references/` files (schemas, API docs, examples) to load on demand.
- **Domain partitioning:** For multi-domain skills, split references by domain (`references/finance.md`, `references/sales.md`) and explain in `SKILL.md` which one to open.
- **Conditional depth:** Provide quick instructions inline, then link to deeper guides (e.g., "For redlining details, see `references/redlining.md`").

## Quality Checklist

- [ ] Frontmatter contains only `name` and `description` with clear triggers.
- [ ] `SKILL.md` body stays under 500 lines, uses concise sections, and references resources.
- [ ] Scripts tested (at least representative samples).
- [ ] Large references have ToCs; assets required by workflows live under `assets/`.
- [ ] Packaging script passes without errors.

Use this skill whenever you need to spin up a new domain-specific skill, extend an existing one, or perform maintenance passes. The result should be a lean, discoverable package that equips the agent with repeatable expertise without wasting context tokens.