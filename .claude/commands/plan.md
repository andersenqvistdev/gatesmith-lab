# /plan — Planner→Checker Loop with Scale-Adaptive Depth (Gatesmith + GSD + BMAD)

You are orchestrating a multi-agent planning workflow with iterative quality validation.

## Input
$ARGUMENTS

## Step 1: Read Context

Read all available planning context:
- `.planning/PROJECT.md` — project intelligence
- `.planning/REQUIREMENTS.md` — captured requirements (from /discuss)
- `.planning/DISCUSS.md` — discussion notes and preferences
- `.planning/STATE.md` — where we left off (if resuming)
- `CLAUDE.md` — project brain

## Step 2: Detect Complexity

```bash
echo "$ARGUMENTS" | uv run .claude/hooks/complexity_detector.py
```

This determines pipeline depth. For **trivial** tasks, skip the checker loop. For **standard+**, use the full planner→checker loop.

## Step 3: Spawn the Architect (Planner)

```
Task(subagent_type="general-purpose", description="Architect designs plan")
```

Pass the Architect:
- The user's requirement ($ARGUMENTS)
- All context from `.planning/` files
- Instruction to read `.claude/agents/architect.md`
- Instruction to produce XML task format in the plan:

```xml
<task id="1.1" status="pending" depends="">
  <name>Task name</name>
  <file>path/to/file</file>
  <read_first>files the implementer must read before starting</read_first>
  <action>CREATE | MODIFY | DELETE</action>
  <description>What and why</description>
  <acceptance>How to verify</acceptance>
</task>
```

The `<read_first>` tag (GSD v1.22) lists files the implementer MUST read before executing. Include it when creating files that should follow existing patterns or modifying complex subsystems.

## Step 4: Checker Loop (for standard+ complexity)

Spawn the Plan Checker agent:
```
Task(subagent_type="general-purpose", description="Check plan quality")
```

Pass it the Architect's plan + instruction to read `.claude/agents/plan-checker.md`.

### Loop Rules:
- If **PASS**: proceed to Step 5
- If **REVISE**: send feedback back to Architect, regenerate plan, re-check. Max 3 iterations.
- If **REJECT**: escalate to user with the Checker's reasoning

## Step 5: Group Into Dependency Waves

Analyze tasks and group into parallel execution waves (from GSD):

```
Wave 1 (parallel): Tasks with no dependencies
Wave 2 (parallel): Tasks depending only on Wave 1
Wave 3 (sequential): Integration tasks
```

## Step 6: Update Planning Files

Write the approved plan to `.planning/ROADMAP.md` with:
- Phase breakdown
- XML task definitions
- Dependency graph
- Wave grouping

Update `.planning/STATE.md` with current phase.

## Step 7: Present to User

```
## Plan Approved ✓

### Complexity: [level] → Pipeline: [stages]

### Phases
[phase breakdown with task counts]

### Execution Waves
Wave 1: [tasks] (parallel)
Wave 2: [tasks] (parallel)
Wave 3: [tasks] (sequential)

### Checker Verdict
[PASS with scores, or iterations needed]

### Next Step
Ready for `/build` — or adjust the plan?
```

## Rules
- NEVER skip the checker for complex/epic tasks
- NEVER present an unvalidated plan
- Max 3 planner→checker iterations before escalating
- Always write the plan to `.planning/ROADMAP.md`
- Always update `.planning/STATE.md`
