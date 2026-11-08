# /hire — Governed Hiring with Board Approval

Submit a hiring request with appropriate governance based on role level. Executive hires require full board approval; team leads require quorum; employees require CEO approval.

**Access:** CEO, CTO, Department Heads

## Input
$ARGUMENTS

Usage:
- `/hire "Senior Engineer" --level employee` — Hire regular employee
- `/hire "VP Engineering" --level executive` — Hire executive (requires board)
- `/hire "Core Team Lead" --level team-lead --team core` — Hire team lead
- `/hire --check executive` — Check governance requirements for level

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to extract:
- Role description (quoted string or positional)
- `--level`: employee (default), team-lead, executive
- `--department`: Target department
- `--team`: Target team (for team-lead level)
- `--budget`: Estimated cost impact
- `--check`: Just check governance, don't submit

**If no role description provided:**
```
## Usage

/hire "[role description]" [options]

Options:
  --level LEVEL       Hiring level (default: employee)
                      Levels: employee, team-lead, executive
  --department DEPT   Target department
  --team TEAM         Target team (for team-lead)
  --budget AMOUNT     Estimated cost impact
  --check LEVEL       Check governance requirements only

Examples:
  /hire "Senior Python Developer" --level employee --department engineering
  /hire "VP of Product" --level executive --budget 150000
  /hire "DevOps Team Lead" --level team-lead --team devops
  /hire --check executive
```
Exit.

## Step 2: Check Governance Requirements

```bash
uv run .claude/hooks/company/board_governance.py check --decision hiring_<level>
```

Map level to decision type:
- `employee` → `hiring_employee`
- `team-lead` → `hiring_team_lead`
- `executive` → `hiring_executive`

**If `--check` flag only:**

```
════════════════════════════════════════════════════════════════════════════════
 HIRING GOVERNANCE: <level>
════════════════════════════════════════════════════════════════════════════════

### Requirements

| Field | Value |
|-------|-------|
| Level | <level> |
| Requires Board | <Yes/No> |
| Reason | <reason> |

### Required Approvers

| Approver | Role |
|----------|------|
| <id> | <role> |

### Process

[For executive:]
1. Submit hiring proposal via this command
2. CEO reviews and approves
3. Board session auto-scheduled
4. Chair + CEO + CTO must all approve
5. If approved, proceed with /company-hire

[For team-lead:]
1. Submit hiring proposal
2. CEO reviews
3. Board quorum reviews (Chair + CEO)
4. If approved, proceed with /company-hire

[For employee:]
1. Submit hiring proposal
2. CEO approves
3. Proceed with /company-hire

════════════════════════════════════════════════════════════════════════════════
```
Exit.

## Step 3: Analyze Budget Impact

```bash
# Get current economics
cat .company/org.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('economics', {})))"
```

Calculate:
- Current employee count
- Budget impact of new hire
- Department distribution

## Step 4: Submit Hiring Proposal

For roles requiring board approval, submit as a plan:

```bash
uv run .claude/hooks/company/planning_authority.py submit \
  --title "Hire: <role>" \
  --type initiative \
  --proposed-by <current-user> \
  --size <small|medium|large> \
  --description "Hiring proposal for <role> at <level> level. Department: <dept>. Budget impact: <budget>."
```

Size mapping:
- `employee` → small
- `team-lead` → medium
- `executive` → large

## Step 5: Display Submission Result

```
════════════════════════════════════════════════════════════════════════════════
 HIRING PROPOSAL SUBMITTED                                            [<status>]
════════════════════════════════════════════════════════════════════════════════

### Position Details

| Field | Value |
|-------|-------|
| Role | <role> |
| Level | <level> |
| Department | <department> |
| Team | <team or "Unassigned"> |
| Budget Impact | <budget or "Not specified"> |

### Governance

| Field | Value |
|-------|-------|
| Requires Board | <Yes/No> |
| Required Approvers | <list> |
| Plan ID | <plan-id> |
| Status | <ceo_review / pending> |

### Budget Analysis

| Metric | Value |
|--------|-------|
| Current Employees | <count> |
| After Hire | <count + 1> |
| Monthly Budget Impact | <estimate> |

### Approval Flow

[For executive (requires full board):]
```
CEO Review ──▶ Board Session ──▶ Chair + CEO + CTO Vote ──▶ Approved
     │              │                    │
     │              │                    └── If any reject: BLOCKED
     │              └── Auto-scheduled on CEO approval
     └── /plan-review <plan-id> --approve
```

[For team-lead (requires quorum):]
```
CEO Review ──▶ Board Quorum ──▶ Chair + CEO Vote ──▶ Approved
```

[For employee (CEO only):]
```
CEO Review ──▶ Approved
     │
     └── /plan-review <plan-id> --approve
```

### Next Steps

[If CEO review pending:]
CEO should review: `/plan-review <plan-id>`

[If board review pending:]
Board should vote: `/board-session <session-id>`

[If approved:]
Execute hire: `/company-hire "<role>" --department <dept>`

════════════════════════════════════════════════════════════════════════════════
```

## Step 6: Direct Hire (Employee Level, CEO Already Approved)

If level is `employee` and governance allows direct CEO approval:

```
════════════════════════════════════════════════════════════════════════════════
 HIRING: CEO APPROVAL REQUIRED                                         [pending]
════════════════════════════════════════════════════════════════════════════════

### Position

| Field | Value |
|-------|-------|
| Role | <role> |
| Level | employee |
| Department | <department> |

### CEO Decision Required

This hire requires CEO approval only (no board needed).

**To approve and hire:**
```bash
/company-hire "<role>" --department <department>
```

**To review first:**
```bash
/plan-review <plan-id>
```

════════════════════════════════════════════════════════════════════════════════
```

## Rules

1. **Level determines governance.** Executive = full board, team-lead = quorum, employee = CEO.
2. **Budget consciousness.** Always show budget impact when specified.
3. **Use planning authority.** All hires go through the P20 planning approval system.
4. **Board sessions auto-schedule.** For executive hires, board session is automatic on CEO approval.
5. **Unanimous for executives.** Executive hires require Chair + CEO + CTO all approving.
6. **Track in history.** All hiring decisions recorded in planning_approvals.json.
7. **After approval, use /company-hire.** This command submits; /company-hire executes.
