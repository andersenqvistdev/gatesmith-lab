# /assign-goal — Assign Goal Ownership

CEO/CTO command to assign goal or phase ownership to an employee. Creates formal accountability for delivery.

**Access:** CEO or CTO only

## Input
<args/>

Usage:
- `/assign-goal G1 forge-architect` — Assign G1 to architect
- `/assign-goal G5 --to forge-cto --accountability oversight`
- `/assign-goal --phase P14 forge-architect`
- `/assign-goal --list` — Show all assignments

## Step 1: Parse Arguments

Parse `<args/>` to extract:
- Goal/Phase ID (G1, P14, etc.)
- Employee ID (--to or positional)
- `--accountability`: delivery (default), oversight, consultation
- `--review`: daily, weekly (default), monthly
- `--phase`: Assign phase ownership instead of goal
- `--list`: List all current assignments

## Step 2: List Assignments (--list)

```bash
uv run .claude/hooks/company/planning_authority.py status
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 ORGANIZATIONAL ASSIGNMENTS                                     [<count> active]
════════════════════════════════════════════════════════════════════════════════

### Goal Owners

| Goal | Owner | Accountability | Review | Assigned By |
|------|-------|----------------|--------|-------------|
| G1 | forge-architect | delivery | weekly | forge-ceo |
| G5 | forge-cto | oversight | weekly | forge-ceo |
| G6 | senior-python-dev | delivery | daily | forge-cto |

### Phase Owners

| Phase | Owner | Accountability | Review | Assigned By |
|-------|-------|----------------|--------|-------------|
| P20 | forge-architect | implementation | daily | forge-ceo |

### Unassigned Goals

- G2: No owner assigned
- G3: No owner assigned

════════════════════════════════════════════════════════════════════════════════
```

## Step 3: Validate Employee Exists

```bash
cat .company/org.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps([e['id'] for e in d.get('employees', d.get('agents', []))]))"
```

**If employee not found:**
```
## Employee Not Found

No employee with ID "<employee-id>" exists.

Available employees:
- forge-ceo
- forge-cto
- forge-architect
- senior-python-developer
...
```

## Step 4: Create Assignment

For goal assignment:
```bash
uv run .claude/hooks/company/planning_authority.py assign-goal \
  --goal-id <goal-id> \
  --goal-name "<goal name from REQUIREMENTS.md>" \
  --employee-id <employee-id> \
  --employee-name "<employee name>" \
  --assigned-by <current-executive>
```

For phase assignment:
```bash
uv run .claude/hooks/company/planning_authority.py assign-phase \
  --phase-id <phase-id> \
  --phase-name "<phase name from ROADMAP.md>" \
  --employee-id <employee-id> \
  --employee-name "<employee name>" \
  --assigned-by <current-executive>
```

## Step 5: Display Result

```
════════════════════════════════════════════════════════════════════════════════
 ASSIGNMENT CREATED                                                  [success]
════════════════════════════════════════════════════════════════════════════════

### Assignment Details

| Field | Value |
|-------|-------|
| Assignment ID | <assignment-id> |
| Type | goal_owner / phase_owner |
| Target | <goal/phase-id>: <name> |
| Owner | <employee-name> (<employee-id>) |
| Accountability | <delivery/oversight/consultation> |
| Review Frequency | <daily/weekly/monthly> |
| Assigned By | <assigner> |
| Assigned At | <timestamp> |

### Owner Responsibilities

[If accountability = delivery:]
- Accountable for goal/phase completion
- Reports progress at <review-frequency> intervals
- Escalates blockers to <accountable-executive>

[If accountability = oversight:]
- Monitors progress and provides guidance
- Reviews deliverables before completion
- Ensures alignment with strategic goals

### Next Steps

1. Owner will be notified of assignment
2. Progress tracked via `/dashboard` and `/employee-status`
3. Review cadence: <review-frequency>

════════════════════════════════════════════════════════════════════════════════
```

## Rules

1. **Executive authority required.** Only forge-ceo or forge-cto can assign ownership.
2. **One owner per target.** Reassignment replaces the previous owner.
3. **Track accountability chain.** All assignments logged in planning_approvals.json.
4. **Notify assigned employee.** Assignment appears in employee's memory context.
5. **Link to goals/phases.** Validate that goal/phase IDs exist in REQUIREMENTS.md or ROADMAP.md.
