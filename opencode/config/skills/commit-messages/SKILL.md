---
name: commit-messages
description: Draft/review Git commit messages; activate when the user asks for a commit message (git commit, staged changes).
---

# Commit Messages

Use this workflow whenever you need to write or review a Git commit message.

## Agent workflow

Aim for messages that help a reviewer (often future you) understand intent.

1. Identify the user-visible intent (what changes when applied).
2. If the diff includes multiple changes, pick the **primary feature/outcome**.
3. Draft an imperative subject that leads with that primary feature.
   - Heuristic: `Verb + user-facing object + benefit`
4. Add a body to capture secondary context (enablers, constraints, risk).
5. Ensure the commits in a PR "tell a story" (each subject advances it).
6. Re-check for clarity without reading the diff.

## Format

### Subject line (first line)
- Capitalize the first word.
- Keep it short: aim for **50 characters or less**.
- Lead with the **primary user-visible feature/outcome**.
  - Prefer: `Persist session data between runs`
  - Avoid: `Prepare host dirs for Docker run`
- Use the **imperative mood** (describe what the commit does when applied):
  - Good: `Add caching to user lookup`
  - Bad: `Added caching to user lookup`
  - Bad: `Adds caching to user lookup`
- No trailing period.

### Blank line
- Always put a **blank line** after the subject.
- Omit the body entirely if it adds no value, but keep the blank line rule when a body exists.

### Body (optional)
- Wrap paragraphs to **~72 columns**.
- Focus on **why** and **impact** (rationale, constraints, tradeoffs, risk).
- Use the body for **secondary details** (enabling work, implementation notes,
  migration, risk) so the subject can stay feature-first.
- Prefer intent and constraints over a narration of the diff.
- Answer at least one of:
  - Why was this necessary?
  - What behavior changed (user-visible or system-level)?
  - What alternatives were considered and rejected?
  - What risks or follow-ups exist?
- Link to relevant context when available (issue/PR/ticket IDs).
- Separate paragraphs with blank lines.

### Bullets (optional)
- Bullets are fine (typically `- ` or `* `).
- Prefer a hanging indent for wrapped bullet lines:
  - First line: `- ` + text
  - Wrapped lines: indent two spaces so it reads cleanly.
- Keep blank lines between bullet groups if it improves readability.

## Template

```
<Primary feature/outcome in <=50 chars>

<1–2 sentences on why/impact. Wrap at ~72 cols.>
<Optional: enabling details or risks if needed>

- Optional bullets for secondary details
- Use hanging indents for wrapped lines
```

## Checklist

Before finalizing:
- Subject is imperative and <= ~50 chars.
- Subject is a single sentence, no period.
- Subject describes the **primary feature/outcome** (not setup steps).
- Subject answers: "What new capability/behavior do we get?"
- Subject describes behavior/intent (not "WIP", "misc", "updates").
- Blank line between subject and body.
- Body wrapped ~72 columns.
- Body captures secondary details and rationale (not a diff recap).
- Bullets are consistently formatted.

## Examples

Good (no body):

```
Fix nil deref in session refresh
```

Good (with body):

```
Persist opencode session data between runs

Mount the host data directory into the container so sessions survive
recreating the container. Pre-create the config/data directories to
avoid volume mount errors on fresh checkouts.
```

Another good (with body):

```
Improve retry behavior for upload requests

Uploads occasionally fail due to transient network errors. Add a bounded
retry with exponential backoff to reduce user-visible failures.

- Retries only idempotent requests
- Caps total retry time at 10 seconds
```

## Special cases

### Reverts
- Use the standard subject format: `Revert "<original subject>"`.
- In the body, explain why the revert is necessary and what symptom/regression it addresses.
- If you know it, include what commit/PR introduced the change being reverted.

Example:

```
Revert "Enable parallel uploads by default"

This change introduced intermittent timeouts in production for large files.
Revert to restore prior behavior while we investigate root cause.
```

### Breaking changes
- Keep the subject concise and imperative.
- In the body, clearly call out the breaking behavior and required migration.

Example:

```
Require explicit region for S3 clients

This changes default client construction and will break callers relying on the
implicit region. Update callers to pass `region` explicitly.
```

## Conflict resolution

When resolving merge/rebase conflicts, make the subject describe what happened:

- If it's a merge commit, keep the standard merge subject (often auto-generated).
- If it's a follow-up commit after resolving conflicts, use an imperative subject
  that names the scope (avoid generic "Fix conflicts").

Body guidance (the useful part):
- Include a short "Conflicts:" section.
- For each conflicted file/area:
  - Identify what was in conflict (two competing changes or behaviors).
  - Describe what you did to resolve it (what you kept/removed/combined) and why.

Suggested body template:

```
Conflicts:
  path/to/file.ext
    - <what conflicted> -> <what you did to resolve it>
      <optional extra detail wrapped to ~72 cols>

  path/to/other_file.ext
    - <what conflicted> -> <what you did to resolve it>
```

## Common pitfalls

- Don't mix subject and body without a blank line.
- Don't use past tense in the subject (`Fixed`, `Added`).
- Don't lead the subject with implementation details when there is a
  user-visible feature (put enabling work in the body).
- Don't use vague subjects (`Refactor`, `Cleanup`) unless that is the
  actual primary outcome.
- Don't pack multiple unrelated changes into one subject.
- Don't rely on the body to fix a vague subject; make the subject do real work.
