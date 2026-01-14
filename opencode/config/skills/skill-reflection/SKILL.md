---
name: skill-reflection
description: MUST be activated when the user asks for a reflection on the current conversation, including identifying which skills were used, suggesting general improvements to existing skills, and proposing new repeatable skills or commands.
---

# Skill Reflection

Use this skill to produce a **high-signal retrospective** on the current conversation focused on skills: what was used, what was missing, and what would make future runs faster and safer.

## Output Contract

Return a short, structured reflection. Prefer bullets over prose.

Include these sections when applicable:

1. **Conversation recap** (1–3 bullets): what the user wanted and what happened.
2. **Skills used**: list explicitly activated skills and any implicitly-triggered ones (name + evidence).
3. **Skill improvements**: actionable recommendations to update existing skills.
4. **New skills to consider**: candidates for repeatable workflows discovered in this conversation.
5. **Commands to consider** (optional): whether a slash command would reduce friction.

## How to Identify Skills Used

- **Explicit usage:** look for direct skill activation (for example, `skill: <name>` tool calls) and commands that instruct activation.
- **Implicit usage:** infer from the agent’s behavior and/or command prompts (for example, a commit message workflow likely used `commit-messages`).
- If uncertain, say so explicitly (for example, “Likely used `general-software-engineering` (pattern match), but not explicitly activated”).

## How to Recommend Skill Improvements (Keep It General)

Your goal is to improve the *skill package*, not to solve the one-off task.

When suggesting a change, include:

- **Problem pattern:** what repeated friction or failure mode appeared.
- **Proposed change:** what to add/remove/clarify in the skill (description triggers, workflow steps, safety constraints, validation commands, templates).
- **Why it helps:** how it reduces ambiguity, prevents mistakes, or saves iterations.

Guardrails:

- Avoid recommendations that are overly specific to a single repo, file path, or one-off request.
- It’s fine to cite the conversation as an example, but phrase the change as a general technique.
- Prefer *small edits* that preserve the skill’s intent and minimize token bloat.
- If the gap is better solved by a command (argument validation + small context blocks), say so.

## How to Propose New Skills

Propose a new skill only when the conversation reveals a repeatable task pattern.

For each candidate skill, provide:

- **Candidate name:** kebab-case.
- **Scope suggestion:** `global` if broadly reusable; `local` if repo-specific.
- **Activation CTA:** one sentence in the form “MUST be activated when…”.
- **Core workflow (3–7 bullets):** the minimal steps the skill should encode.
- **Non-goals:** what it explicitly should not do (to prevent overreach).

## When to Recommend a Slash Command

Recommend a new command when it would:

- Standardize a common argument shape (reduces clarification loops).
- Reliably inject minimal, high-signal context (for example, `git status`, `git diff`, recent logs).
- Enforce a safe workflow (for example, validation checks before edits).

If recommending a command, specify:

- Command name (kebab-case).
- Expected `$ARGUMENTS` shape (or explicitly “no arguments”).
- 2–4 context blocks (`!`), each small and directly relevant.

## Scope

This skill is about *reflection and recommendations*. Do not perform code edits unless the user explicitly asks you to apply the proposed changes.
