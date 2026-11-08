# /feature — Fully Autonomous Feature Development

You are running the complete autonomous feature pipeline. From a single description, you will discuss requirements (if complex), plan, build, review, test, and commit — all without stopping to ask the user unless truly ambiguous.

## Input
$ARGUMENTS

## Step 0: Detect Complexity & Load Context

```bash
echo "$ARGUMENTS" | uv run .claude/hooks/complexity_detector.py
```

Read all `.planning/` files and `CLAUDE.md` for project context.
Read the existing codebase to understand patterns, tech stack, conventions.

If `.planning/PROJECT.md` is empty or template-only, first run a quick codebase exploration:
```
Task(subagent_type="Explore", description="Map codebase for feature context")
```
Update `.planning/PROJECT.md` with findings.

## Step 0.5: Display Workflow Preview

Before starting any work, show what will happen:

```
┌─ Workflow Preview ────────────────────────────────────────────┐
│ Feature: [feature description truncated to 50 chars]          │
│ Complexity: [trivial/standard/complex/epic]                   │
├───────────────────────────────────────────────────────────────┤
│ Pipeline:                                                     │
│   1. [Plan / Skip] — Design implementation approach           │
│   2. Build — Write code with atomic commits                   │
│   3. Review — Code quality and security check                 │
│   4. Test — Write and run tests                               │
│   5. Gate — Security checkpoint                               │
│   6. Complete — Update docs, ready for push                   │
├───────────────────────────────────────────────────────────────┤
│ Human touchpoints: Gate (if security findings), Push          │
└───────────────────────────────────────────────────────────────┘
```

Adjust the pipeline display based on complexity:
- **trivial**: Skip Plan step, show "1. Build → 2. Review → 3. Complete"
- **standard**: Show full pipeline
- **complex/epic**: Add "Checker Loop" note after Plan

This preview is purely informational — do NOT add any confirmation prompts.
Proceed immediately to Step 1 after displaying.

## Step 1: Smart Agent Assessment

Before planning, decide if this feature needs a SPECIALIST agent that doesn't exist yet.

Examples:
- Feature involves database migrations → need a "migration agent"
- Feature involves API documentation → need a "docs agent"
- Feature involves internationalization → need an "i18n agent"
- Feature involves performance optimization → need a "perf agent"

If a specialist would help, spawn the Meta-Agent to create it:
```
Task(subagent_type="general-purpose", description="Create specialist agent for [domain]")
```
Pass it: "Read .claude/agents/meta-agent.md for your instructions. Create a specialist agent for [domain] that will be used in the following feature: $ARGUMENTS"

## Step 2: Autonomous Planning

### For trivial complexity:
Skip planning. Go directly to Step 3 — you are the architect AND implementer.

### For standard complexity:
Spawn Architect agent:
```
Task(subagent_type="general-purpose", description="Design plan for feature")
```
Pass the full context from .planning/ files. Accept the plan if it's reasonable — don't loop to the checker for standard work.

### For complex/epic:
Run the full planner→checker loop:
```
Task(subagent_type="general-purpose", description="Architect designs feature plan")
```
Then validate with checker:
```
Task(subagent_type="general-purpose", description="Check plan quality")
```
Loop max 2 times. If the plan is close enough, proceed — don't over-iterate.

Save the plan to `.planning/ROADMAP.md`.

## Step 3: Autonomous Implementation

### Determine execution strategy:

**If trivial (1-2 files):**
Implement directly yourself. No sub-agents needed. Read the files, make changes, run quality checks.

**If standard (3-8 files):**
Group into waves and spawn Implementer agents for parallel work:
```
Task(subagent_type="general-purpose", description="Implement [task]")
```

**If complex/epic (8+ files):**
Full wave execution with parallel Implementer agents per wave. Include any specialist agents created in Step 1.

### After EACH implementation task:
1. Atomic commit: `uv run .claude/hooks/atomic_commit.py <phase> <id> "<name>"`
2. Update state: `uv run .claude/hooks/state_tracker.py <phase> <id> "<name>" "complete" "<next>"`

## Step 4: Autonomous Review

Spawn Reviewer agent on all changes:
```
Task(subagent_type="general-purpose", description="Review feature implementation")
```

**If PASS:** continue.
**If NEEDS CHANGES:** fix them yourself (don't spawn another agent for small fixes), commit, move on.
**If BLOCK (critical security/correctness issue):** stop and report to user.

## Step 5: Autonomous Testing

Spawn Tester agent:
```
Task(subagent_type="general-purpose", description="Write and run tests for feature")
```

Atomic commit the tests. Run them. If failures, fix and re-run (max 2 attempts).

## Step 6: Final Quality

Run all quality tools:
```bash
npm run lint 2>&1 || python -m ruff check . 2>&1 || true
npm run test 2>&1 || python -m pytest 2>&1 || true
npm run build 2>&1 || true
```

Fix any issues. Commit fixes.

## Step 7: Update Planning & Report

Update `.planning/STATE.md` and `.planning/ROADMAP.md`.

Report:
```
## Feature Complete: [name]

### Pipeline
| Step | Status | Details |
|------|--------|---------|
| Complexity | [level] | [pipeline used] |
| Agents Created | [count] | [names if any] |
| Plan | DONE | [tasks count] |
| Build | DONE | [files changed], [commits] |
| Review | PASS | [round] |
| Tests | PASS | [count] passing |
| Quality | PASS | lint + build clean |

### Commits
| Hash | Message |
|------|---------|
[list of atomic commits]

### Files Changed
[table]

### What Was Built
[2-3 sentence summary of the feature]
```

## Step 7.5: Auto-Restart Daemon (if needed)

After feature completion, check if daemon-related files changed and restart automatically:

```bash
uv run .claude/hooks/daemon_auto_restart.py
```

**If action = "restarted":** Display notification in the final report.
**If action = "skip":** Continue silently.
**If action = "error":** Display warning but do NOT block.

Add a row to the Pipeline Results table:
```
| Daemon | RESTARTED / SKIPPED | [reason] |
```

## Autonomy Rules
1. **DO NOT stop to ask the user** unless there's genuine ambiguity that could lead to building the wrong thing. "Should I use X or Y pattern?" — just pick the one that fits the codebase.
2. **DO create specialist agents** when the feature domain warrants it. This is the IndyDevDan pattern — spawn the right specialist, don't force a generalist.
3. **DO fix review issues yourself** for small fixes. Only re-spawn agents for large rework.
4. **DO commit atomically** — one task, one commit. Always.
5. **DO update state** — always. This enables /continue if context is lost.
6. **STOP only for:** security concerns, destructive changes, genuine requirement ambiguity.
