# /discuss — Capture Requirements Before Planning (from GSD)

You are facilitating a structured discussion to capture requirements, preferences, and gray areas BEFORE any planning or coding happens. This prevents rework and ensures alignment.

## Input
$ARGUMENTS

## Step 1: Read Existing Context

Read these files if they exist:
- `.planning/PROJECT.md` — existing project intelligence
- `.planning/REQUIREMENTS.md` — existing requirements
- `.planning/DISCUSS.md` — previous discussions
- `CLAUDE.md` — project brain

## Step 2: Explore the Codebase (if existing project)

If this is an existing project (not greenfield), spawn an Explorer agent:
```
Task(subagent_type="Explore", description="Map codebase for discussion context")
```
Ask it to identify: tech stack, architecture patterns, existing conventions, test patterns, and any relevant existing code related to the user's topic.

## Step 3: Structured Discussion

Have a conversation with the user covering these areas. Ask 2-3 questions at a time using AskUserQuestion, not all at once:

### Round 1: Goals
- What problem are you solving?
- What does success look like?
- Are there hard constraints (timeline, budget, compatibility)?

### Round 2: Preferences
- Do you prefer a specific approach/pattern? (e.g., REST vs GraphQL, ORM vs raw SQL)
- Any libraries/tools you want to use or avoid?
- How important is backward compatibility?

### Round 3: Gray Areas
- What aspects are you unsure about?
- Are there tradeoffs you want me to decide on, or do you want to weigh in?
- Any scope boundaries? (what is explicitly NOT part of this work)

## Step 4: Capture & Confirm

Update `.planning/REQUIREMENTS.md` with everything captured.
Update `.planning/DISCUSS.md` with the discussion log.

Present a summary to the user:
```
## Discussion Summary

### Goals
[bulleted list]

### Requirements
[functional + non-functional]

### Preferences
[table: topic | preference | reason]

### Decisions Made
[what was decided]

### Deferred
[what to decide later]

### Recommended Next Step
Ready for `/plan` — or need another `/discuss` round?
```

## Rules
- NEVER skip this step for complex features. Planning without discussion leads to rework.
- Ask questions in rounds, not all at once.
- Capture EVERYTHING — preferences matter as much as requirements.
- If the user says "you decide", still document what you decided and why.
