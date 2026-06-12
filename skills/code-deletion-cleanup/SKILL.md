---
name: code-deletion-cleanup
description: MUST be activated when deleting code (features, endpoints, jobs, modules, or functions) and doing cleanups, especially when removal can trigger cascading deletions or break externally reachable entry points.
---

# Code Deletion Cleanup

This skill defines a conservative, principal-engineer workflow for deleting code while doing thorough cleanups.
It is designed for everyday engineering tasks where deleting one target can create ripple effects across call graphs, modules, configuration, data, and tests.

## Core Concepts

- **Target:** the function, class, module, file, feature flag, endpoint, job, or CLI command you intend to remove.
- **Call site:** any invocation or reference that reaches the target (imports, function calls, registrations, routing tables, reflection, string keys).
- **Entry point:** code that can be triggered externally or out of band (HTTP routes, CLI commands, scheduled jobs, message consumers, webhooks, public SDKs).
- **Deletion queue:** the ordered list of candidate items to delete next.

## Hard Rules

- Only delete code when you can prove it is unused, or when the user explicitly accepts breaking changes.
- If you are uncertain, keep the code and report what you could not prove.
- Treat entry points and public contracts as high risk.
- Never delete entry points based on “no internal references” alone.
- Prefer repo-wide searches and build graph checks over file-local guesses.

## Workflow

### 0) Preflight (Principal Engineer Step)

Before you delete anything, clarify the intent and constraints.

- Confirm the exact target(s) and the desired end state.
- Identify any entry points, public APIs, or documented behaviors involved.
- Confirm the breaking-change policy.
- Confirm data and retention implications (stored data, migrations, queued messages, file formats) if applicable.

If any of these are unclear, stop and ask questions.

### 1) Initialize a Deletion Queue

- Start with the user-provided deletion target.
- Add additional candidates only after verification.
- Use the task list tool (`todowrite`) as the canonical queue when the work spans multiple iterations.
- Model each queued item as a todo and advance statuses as you process items.
- When delegating repo-wide searches or inventories to subagents, create separate todos for each delegated chunk (for example "[explore] classify references for `<symbol>`").
- Use `pending` for not-yet-started items, `in_progress` for actively worked items, and `completed` once verified.
- It is acceptable to have multiple `in_progress` todos while subagents run in parallel, but keep ownership explicit in the todo text (for example prefix with `[main]` and `[explore]`).
- Keep the queue explicit in your narrative output.
- At the end of each iteration, print `Queue now: ...` (or equivalent from `todoread`).

### 2) Choose a Target

- Pop the next target from the deletion queue.
- Targets may be symbols (functions, classes), modules, files, routes, commands, or jobs.

### 3) Produce a Dependency Inventory (Mandatory)

Before deleting anything, enumerate dependencies used by the target and by any helper code you plan to delete with it.
Mandatory output: dependency inventory.

Include, at minimum.

- **Direct callees and referenced symbols:** functions, methods, classes, modules, constants, types, templates.
- **Indirect calls you plan to delete:** dependencies used by helpers you plan to delete alongside the target.
- **Dynamic references:** anything invoked via reflection, string keys, registries, dependency injection containers, plugin systems, or config-driven wiring.
  If a dependency name is computed, do not enqueue it unless you can prove it resolves to a fixed, unused set.
- **Imports and module dependencies:** imported symbols and modules only used by the target (and planned-to-delete helpers).
- **Non-code dependencies:** config keys, environment variables, feature flags, schema fields, message topics, filenames, and metrics names referenced by the target.

If this inventory spans many files, delegate discovery to an `explore` subagent.
Ask it to return exact symbol names and paths.

### 4) Repo-Wide Verification for Each Dependency

For each dependency that could trigger cascading deletions, perform a repo-wide search and classify matches.

Required classifications.

- **Call sites and references:** invocations, imports, registry references, type references, template usage.
- **Definitions and exports:** where the symbol or module is defined and how it is exported.
- **Entry point wiring:** routing tables, command registries, schedulers, consumer registrations, plugin manifests, dependency injection setup.
- **Config and docs:** config files, documentation, examples, and generated artifacts committed in-repo.
- **Tests and fixtures:** tests, snapshots, golden files, mocks, and test utilities.

Deletion criteria.

- Enqueue a dependency only if there are no remaining call sites or references outside code already scheduled for deletion.
- If the dependency participates in an entry point, do not auto-delete it.
  Report that it appears externally reachable and requires manual review.

### 5) Delete the Target and Immediately Re-Check Ripple Effects

After deleting the target and its last known call sites, immediately re-run the repo-wide usage checks.

- Re-check each dependency from the dependency inventory.
- If any dependency now has zero remaining references outside scheduled-for-deletion code, enqueue it.

### 6) Cleanup Build Graph and Dependency Manifests

Thorough deletion is not complete until the build graph is clean.

- Remove unused imports and unused exports.
- Remove unused modules and files.
- Remove unused third-party dependencies from the package manifest(s) when safe.
- Regenerate lockfiles if the ecosystem requires it.
- Remove dead wiring in dependency injection, registration tables, plugin manifests, and module index files.

Be conservative.
Avoid deleting shared dependencies unless you can prove they are unused across the repo.

### 7) File-Level Cleanup

After deleting symbols and imports, evaluate the entire file.

- If nothing remaining in the file is referenced (exports, imports, registrations, reachable code), you may delete the file.
- Otherwise, delete only confirmed-unused functions, helpers, and imports.

### 8) Iterate Until Empty (Hard Stopping Condition)

Repeat steps 2 through 7 until the deletion queue is empty.
Do not stop early.

### 9) Post-Delete Audit and Verification

Prefer the narrowest validation that covers the change, then add one layer of confidence.

- Build or typecheck if available.
- Run the most targeted tests.
- Run a repo-wide search for the deleted symbol(s) and any string keys from the dependency inventory.
- If entry points were removed, run a minimal smoke test that exercises the surrounding runtime startup path.

Do not fix unrelated failures.
Report them and keep the deletion scoped.

## Suggested Search Patterns

Use repo-wide searches that separate references from definitions.

- References: `rg -n "\\b<symbol>\\b"`.
- Imports: `rg -n "from ['\"][^'\"]+['\"]"` and `rg -n "require\\(['\"][^'\"]+['\"]\\)"`.
- Entry points: search for router, command, scheduler, consumer, or plugin registration patterns used in the codebase.
- Dynamic wiring: search for registry usage, string keys, reflection, or config-driven dispatch patterns.

## Subagent Pattern (Optional)

If classification is large, delegate searching to an `explore` subagent.
Ask it to return a structured result with paths and line numbers for each classification bucket.

Example prompt to the subagent.

- "Find all occurrences of `<symbol>`.
  Return call sites and imports, definitions and exports, any entry-point wiring, config and docs, and test usage.
  Include file paths and line numbers."