# /complete — Mark Milestone Complete (from GSD)

Finalize a phase or milestone. Creates a summary commit and updates all planning docs.

## Input
$ARGUMENTS

## Step 1: Verify Completeness

Run `/verify` mentally — check that all tasks in the current phase are marked complete in `.planning/ROADMAP.md`.

If any tasks are incomplete, list them and ask the user whether to:
1. Complete them now
2. Defer to next phase
3. Remove from scope

## Step 2: Update Planning Docs

- `.planning/ROADMAP.md`: Mark current phase as **complete**, advance to next phase
- `.planning/STATE.md`: Update progress table, set next phase
- `.planning/PROJECT.md`: Add any new conventions or decisions discovered during this phase

## Step 3: Summary Commit

```bash
uv run .claude/hooks/atomic_commit.py <phase> "milestone" "complete phase <N>"
```

## Step 4: Report

```
## Phase [N] Complete

### Summary
- Tasks completed: X/Y
- Commits: Z
- Files changed: N

### Key Changes
[bullet list of what was accomplished]

### Deferred
[anything moved to later phases]

### Next Phase
[what's coming next, or "Project complete" if all phases done]

### Recommended Next Step
- `/discuss` if next phase has open questions
- `/plan` if requirements are clear
- `/gate` if ready to push
```
