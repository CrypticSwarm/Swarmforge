---
name: agents-md-reconnaissance
description: MUST be activated when creating, updating, or evaluating a directory-specific AGENTS.md file through codebase reconnaissance. Use when the user asks to generate AGENTS.md documentation, audit an existing AGENTS.md for completeness, or stress-test an AGENTS.md against real tasks.
---

## What This Skill Does

Runs a structured, multi-phase reconnaissance to produce (or refine) a
directory-specific `AGENTS.md` that gives future agent sessions the context
they need to work effectively in that part of the codebase.

## Workflow Overview

Four phases, executed in order:

1. **Explore** -- Build an initial AGENTS.md from codebase exploration
2. **Stress-Test** -- Spawn subagents with real tasks derived from git history to find gaps
3. **Refine** -- Incorporate subagent findings into the AGENTS.md
4. **Review** -- Run a review agent to catch inconsistencies and trim noise

## Phase 1: Explore

Goal: Produce a first-draft AGENTS.md for the target directory.

1. Read the target directory listing to understand the file landscape.
2. Identify the major subsystems by reading key entry points, config files,
   and index/barrel files.
3. Use the Task tool with explore agents to investigate:
   - Architecture and data flow (how does a request/operation flow through the code?)
   - Key abstractions and class hierarchies
   - Naming conventions and domain terminology
   - Configuration patterns and registration mechanisms
   - Common procedures (how are new features of each type typically added?)
4. Write the initial AGENTS.md at the target path. Structure it as:
   - **Architecture Overview** -- One-paragraph orientation
   - **Naming Conventions** -- Place near the top so terms are defined before use
   - **Key Files** -- Curated list with one-line descriptions (not exhaustive)
   - **Subsystem sections** -- One section per major subsystem, covering concepts
     an agent needs to make correct decisions (not implementation minutiae)
   - **Common Procedures** -- Step-by-step checklists for recurring task types

Writing guidelines:
- Optimize for agent decision-making, not human onboarding.
- Avoid line-number references (they drift on any edit).
- Define conventions once, in one place, and reference from elsewhere.
- Prefer high-level summaries over implementation details an agent would
  verify in code anyway.
- Keep under 500 lines. If longer, trim low-signal content.

## Phase 2: Stress-Test

Goal: Find gaps by giving subagents real tasks and seeing if the AGENTS.md
is sufficient.

1. Read the git log for the target directory to find 5 representative commits
   that span different task types (e.g., add a feature, modify an algorithm,
   add a new dimension/grouping, wire up a new endpoint, add a configuration).
   Prefer commits that touch multiple files -- these reveal cross-cutting
   concerns the AGENTS.md must cover.

   ```
   git log --oneline -40 -- <target-directory>/
   git show --stat <commit-hash>   # for each candidate
   ```

2. For each of the 5 commits, formulate a task description as if assigning
   it to an agent who has never seen the codebase. The task should be
   realistic and self-contained.

3. Launch all 5 as parallel Task tool calls with `subagent_type: "general"`.
   Each subagent's prompt must include:
   - The task description
   - Explicit instruction to read the AGENTS.md first
   - Explicit instruction to evaluate whether the AGENTS.md provides enough
     information to complete the task
   - Instruction to dig through the actual codebase to find what's MISSING
   - Instruction to report back:
     - What the AGENTS.md got right
     - What specific information is missing
     - Concrete text suggestions for additions
   - Explicit instruction: **DO NOT make any code changes**

4. Wait for all 5 to complete. Collect their findings.

## Phase 3: Refine

Goal: Incorporate subagent findings into the AGENTS.md without bloat.

1. Read all 5 subagent reports.
2. Identify themes -- which gaps were reported by multiple agents?
3. Deduplicate: if multiple agents suggest similar content, merge into one
   canonical version.
4. Prioritize by impact:
   - **Must add**: Information that multiple agents needed and couldn't find
   - **Should add**: Information one agent needed for a cross-cutting concern
   - **Skip**: Overly specific details that only apply to one narrow task
5. Update the AGENTS.md, maintaining the structure from Phase 1.
6. Re-check total length. If over 500 lines, trim the lowest-signal sections.

## Phase 4: Review

Goal: Catch inconsistencies, redundancies, and low-value content.

1. Launch a single Task tool call with `subagent_type: "general"`. The review
   agent's prompt must instruct it to:
   - Read the AGENTS.md thoroughly
   - Cross-reference key claims against the actual codebase (spot-check at
     least 5 specific claims for accuracy)
   - Identify: contradictions, inaccuracies, redundant passages, low-value
     content, structural problems
   - For each issue, provide a concrete recommendation (remove, consolidate,
     reword, move, or correct)
   - Provide an overall quality assessment

2. Apply the review agent's recommendations:
   - Fix all accuracy issues immediately
   - Consolidate redundant content
   - Remove line-number references if any crept in
   - Ensure naming conventions appear once, early in the document
   - Trim over-detailed sections to high-level summaries

3. Do a final read of the document to verify it's clean.

## Anti-Patterns

- **Encyclopedia mode**: The AGENTS.md is not a comprehensive codebase
  reference. If something is better learned by reading the code, omit it.
- **Stale specifics**: Avoid exact line numbers, file lengths, or counts
  that will drift on the next commit.
- **Duplicate definitions**: Define each convention or concept exactly once.
  If you need to reference it elsewhere, point to the canonical definition.
- **Missing procedures**: If the codebase has recurring task patterns (add a
  column, add a groupby, add a filter), the AGENTS.md should have a
  step-by-step checklist for each. These are the highest-value sections.
- **Buried conventions**: Naming conventions and terminology should appear
  near the top, before any section that uses them.

## When to Use Each Phase

| Scenario | Phases |
|----------|--------|
| Creating AGENTS.md from scratch | 1 -> 2 -> 3 -> 4 |
| Updating after major codebase changes | 2 -> 3 -> 4 (skip explore, stress-test the existing doc) |
| Quick audit of existing AGENTS.md | 4 only |
| User provides specific gaps to fill | 3 -> 4 (targeted refinement + review) |
