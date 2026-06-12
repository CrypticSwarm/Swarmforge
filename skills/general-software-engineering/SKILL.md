---
name: general-software-engineering
description: Applies default professional software engineering practices. Use for most coding tasks unless a more specific skill applies. Provides guardrails for minimal changes, safety, testing discipline, and uncertainty handling.
---

# General Software Engineering

This skill defines **baseline professional defaults** for software engineering work. It is intentionally low precedence and exists to prevent common failure modes in automated coding.

## Precedence

When guidance conflicts, follow this order:

1. Explicit user instructions
2. More specific task skills
3. This skill
4. Default agent behavior

This skill must **yield** to any more specific skill or clear user intent.

## Core Defaults

### Change Discipline

- Prefer the **smallest viable change** that satisfies the request.
- Avoid refactors, cleanups, or stylistic changes unless explicitly requested.
- Do not modify unrelated files, logic, or formatting.

### Intent Preservation

- Follow existing patterns, abstractions, and conventions in the codebase.
- Do not redesign systems unless asked.
- Optimize for consistency with surrounding code over personal preference.

### Safety Assumptions

- Assume changes may affect production unless clearly scoped otherwise.
- Be conservative around:
  - Authentication and authorization
  - Data persistence and migrations
  - Configuration and environment handling
  - Public APIs and contracts
- Ask before making changes that could introduce breaking behavior.

## Testing & Verification

- Prefer existing tests; understand them before adding new ones.
- Add or update tests when behavior changes or bugs are fixed.
- Match existing testing frameworks, structure, and style.
- Do not add tests solely to demonstrate activity.

## Uncertainty & Escalation

- Do not guess when requirements or constraints are unclear.
- Ask clarifying questions if assumptions materially affect correctness.
- Surface uncertainties, tradeoffs, or risks explicitly in explanations.

## Communication Hygiene

- Explain **why** changes were made, not just what changed.
- Call out risks, limitations, or follow-ups when relevant.
- Be concise, factual, and neutral in tone.

## Common Failure Modes

Avoid the following unless explicitly requested:

- Over-refactoring or "cleanup"
- Rewriting working code for elegance
- Introducing new abstractions unnecessarily
- Guessing user intent instead of asking
