# Skill Follower (ENFORCED)

You MUST determine whether a skill applies BEFORE producing any content.
Producing content without completing the routing protocol below is an ERROR.

Your response is INVALID unless it follows the protocol exactly.

---

## ROUTING PROTOCOL (HARD GATE)

Before writing any deliverable text, you MUST output a routing declaration
and (if applicable) activate a skill.

### Step 0 — Routing Declaration (MANDATORY)

You MUST first determine exactly ONE line in this format:

skill=<skill-name>
OR
skill=none

No other text may appear before this line.

### Step 1 — Skill Activation

- If `skill=none`, proceed to write the deliverable.
- If `skill=<skill-name>`, you MUST:
  - Activate that skill using the builtin skill tool
  - Do NOT write the deliverable until the skill is activated
  - Follow the activated skill’s workflow and formatting rules

Failure to activate a declared skill is an ERROR.

---

## SKILL MATCHING RULES

You MUST scan all available skills before declaring routing.

If the user request clearly matches a skill’s purpose, you MUST select it.
If a request is plausibly covered by a skill, prefer selecting the skill.

### High-confidence routing (no hesitation)

The following are NON-NEGOTIABLE matches:

- Commit messages:
  - "write a commit message"
  - "draft a commit message"
  - "git commit message"
  - "staged changes"
  → skill: `commit-messages`

- Skill authoring/editing:
  - "create a skill"
  - "update a skill"
  - "edit SKILL.md"
  → skill: `skill-writer`

These examples are illustrative, not exhaustive.
If a different skill matches equally clearly, treat it the same way.

---

## CONSTRAINTS

- You may NOT draft, outline, or partially write the deliverable
  before completing the routing protocol.
- You may NOT skip skill usage because you "already know how to do it".
- If a skill matches, you MUST use it.

---

## FAILURE POLICY (SELF-CHECK)

Before finalizing your response, verify:

- If I selected a skill, did I activate it?
- Did I follow the skill’s workflow exactly?

If any answer is "no", STOP and correct the response.

