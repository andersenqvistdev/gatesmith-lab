# /quick — Lightweight Ad-Hoc Task Execution (from GSD v1.22)

You are executing a quick, focused task WITHOUT the full planning ceremony. Quick mode is for ad-hoc work that doesn't warrant `/discuss` → `/plan` → `/build` — small fixes, one-off changes, quick investigations.

Quick tasks are tracked separately in `.planning/quick/` to keep the main roadmap clean.

## Input
$ARGUMENTS

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine mode:

| Flag | Effect |
|------|--------|
| (none) | Quick execute — minimal overhead |
| `--discuss` | Add lightweight pre-discussion before executing |
| `--full` | Enable plan-checking + verification after execution |
| `--dry-run` | Preview what would happen without executing |

If no task description is provided, use AskUserQuestion to ask:
"What do you need done? (One sentence is fine — this is quick mode.)"

## Step 2: Generate Task ID

Create a timestamp-based ID:
```
YYMMDD-XXX (e.g., 260314-001)
```

Check `.planning/quick/` for existing IDs today and increment the sequence number.

## Step 3: Quick Context Load

Read ONLY these files (keep context light):
- `CLAUDE.md` — project brain (always)
- `.planning/PROJECT.md` — tech stack and conventions (if exists)
- The files directly relevant to the task description

Do NOT read ROADMAP.md, REQUIREMENTS.md, or STATE.md — this is quick mode.

## Step 4: Pre-Discussion (if --discuss)

If `--discuss` flag is present, ask 2 quick questions using AskUserQuestion:

1. "Any constraints or preferences for this task?"
2. "Should this affect any specific files or areas?"

Capture responses in the quick task log.

## Step 5: Execute

### 5.1: Explore (if needed)

If the task involves existing code, use Glob/Grep to find relevant files. Keep exploration minimal — spend at most 2-3 tool calls finding context.

### 5.2: Implement

Execute the task directly. Follow existing project conventions from PROJECT.md.

Rules:
- **Read before writing.** Always read a file before modifying it.
- **Run quality checks** after every file change (linter, formatter).
- **No scope creep.** Do exactly what was asked, nothing more.
- **Small changes preferred.** If the task grows beyond ~3 files, suggest switching to `/plan` → `/build`.

### 5.3: Verify (if --full)

If `--full` flag is present:
1. Run linter on all changed files
2. Run tests related to changed code
3. Spawn a quick Plan Checker review:
   - Does the change accomplish the stated goal?
   - Any obvious issues or regressions?

## Step 6: Log & Commit

### 6.1: Create Quick Task Log

Create/append to `.planning/quick/YYMMDD.md`:

```markdown
## YYMMDD-XXX: [task title]

**Time:** HH:MM UTC
**Status:** complete | failed | partial
**Flags:** [--discuss] [--full]

### Task
[original task description]

### Changes
| File | Action | Summary |
|------|--------|---------|
| path/to/file | MODIFIED | Brief description |

### Notes
[Any relevant context for future reference]
```

### 6.2: Atomic Commit

Create one commit for the task:
```
quick(YYMMDD-XXX): [task description]
```

## Step 7: Summary

Display a compact summary:

```
═══════════════════════════════════════════════
 QUICK TASK COMPLETE                [YYMMDD-XXX]
═══════════════════════════════════════════════
 Task: [description]
 Files: [N] changed
 Commit: [short hash]
═══════════════════════════════════════════════

 Next: /quick "another task"
       /plan  (if this needs more structure)
═══════════════════════════════════════════════
```

## Scope Guard

If at any point during execution you realize the task is too large for quick mode (>5 files, complex dependencies, needs architectural decisions), STOP and tell the user:

```
This task is growing beyond quick mode scope.

Recommendation: Use the full pipeline instead:
  /discuss "[task description]"
  /plan
  /build

Continue in quick mode anyway? [y/N]
```

Use AskUserQuestion to get their decision.

## Rules

1. **Speed over ceremony.** Quick mode exists to skip planning overhead. Don't recreate it.
2. **Track everything.** Even quick tasks get logged in `.planning/quick/` for audit trail.
3. **One commit per task.** Same atomic commit discipline as `/build`.
4. **Scope guard is mandatory.** Never let a "quick" task silently become a large change.
5. **Don't touch ROADMAP.md.** Quick tasks live in their own space.
6. **Respect project conventions.** Quick doesn't mean sloppy — follow existing patterns.
