---
description: Generate or refine a directory-specific AGENTS.md through multi-phase codebase reconnaissance
agent: build
model: anthropic/claude-opus-4-6
---
Activate the agents-md-reconnaissance skill before taking any other action.

Request:
$ARGUMENTS

Expected arguments:
- Target directory path (e.g., `dashboard/backend`, `delivery/rtb_frontend`)
- Optional mode: `create` (default), `audit`, or `update`

If the target directory is missing or ambiguous, ask and stop.

Modes:
- `create` -- Full 4-phase workflow (explore, stress-test, refine, review). Fails if AGENTS.md already exists at the target; suggest `update` instead.
- `update` -- Phases 2-4 only (stress-test existing AGENTS.md, refine, review). Fails if no AGENTS.md exists; suggest `create` instead.
- `audit` -- Phase 4 only (review pass on existing AGENTS.md). Fails if no AGENTS.md exists.

Current working tree:
!`git status --short`

Target directory contents:
!`ls $1 2>/dev/null || echo "Directory not found"`

Existing AGENTS.md at target (if any):
!`cat $1/AGENTS.md 2>/dev/null || echo "No AGENTS.md found"`

Recent git history for target:
!`git log --oneline -20 -- $1/ 2>/dev/null || echo "No git history"`

Validate inputs, then execute the appropriate phases per the skill workflow.
Place the resulting AGENTS.md at `<target-directory>/AGENTS.md`.
