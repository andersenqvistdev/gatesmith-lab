# /new-project — Initialize a New Project with Full Context (from GSD + BMAD)

You are initializing a new project with the Gatesmith framework. This captures everything needed before any code is written.

## Input
$ARGUMENTS

## Step 1: Scale Detection

Run the complexity detector to determine project scale:
```bash
echo "$ARGUMENTS" | uv run .claude/hooks/complexity_detector.py
```

Report the detected complexity level and recommended pipeline to the user.

## Step 2: Project Intelligence

If this is an existing codebase, spawn an Explorer to map it:
```
Task(subagent_type="Explore", description="Map existing codebase structure")
```

Create/update `.planning/PROJECT.md` with:
- Project name and type
- Tech stack (detected or specified)
- Architecture overview
- Conventions discovered
- Key constraints

## Step 3: Initialize Planning Files

Ensure all `.planning/` files exist:
- `PROJECT.md` — filled with project intelligence
- `REQUIREMENTS.md` — initialized with any known requirements from $ARGUMENTS
- `ROADMAP.md` — empty template ready for /plan
- `STATE.md` — initialized with session start
- `DISCUSS.md` — empty template ready for /discuss

## Step 4: Git Setup (if not initialized)

If no `.git` exists:
```bash
git init
git add .
git commit -m "feat(phase-0): initialize project with Gatesmith framework"
```

## Step 5: Recommend Next Step

Based on complexity:
- **trivial/standard**: "Ready to go. Describe what you want to build, or use `/plan` to start planning."
- **complex/epic**: "This looks like a complex project. I recommend starting with `/discuss` to capture requirements and preferences before planning."

Present the project summary:
```
## Project Initialized

| Property | Value |
|----------|-------|
| Name | ... |
| Type | ... |
| Complexity | ... |
| Pipeline | ... |
| Tech Stack | ... |

### Recommended Workflow
[based on complexity level]
```
