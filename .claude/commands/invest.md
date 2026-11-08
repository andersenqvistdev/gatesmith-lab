# /invest — Investment Decisions with Board Governance

Submit investment proposals with appropriate governance based on amount and type. Major investments require full board approval; budget reallocations require quorum; small operational expenses require CEO approval.

**Access:** CEO, CTO, Department Heads

## Input
$ARGUMENTS

Usage:
- `/invest "Cloud infrastructure upgrade" --amount 5000` — Major investment (requires board)
- `/invest "New dev tools subscription" --amount 200` — Small expense (CEO only)
- `/invest --reallocate engineering product --amount 500` — Budget reallocation
- `/invest --check 5000` — Check governance requirements for amount

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to extract:
- Description (quoted string or positional)
- `--amount`: Investment amount in USD (required for new investments)
- `--reallocate`: Source and target departments for reallocation
- `--type`: investment (default), reallocation, operational
- `--category`: infrastructure, tooling, hiring, expansion, marketing, other
- `--recurring`: monthly, yearly, one-time (default: one-time)
- `--justification`: Business justification
- `--check`: Just check governance for amount, don't submit

**If no description provided (except for --check):**
```
## Usage

/invest "[description]" [options]

Options:
  --amount AMOUNT       Investment amount in USD (required)
  --type TYPE           Type: investment, reallocation, operational
  --category CAT        Category: infrastructure, tooling, hiring, expansion, marketing, other
  --recurring PERIOD    Recurring: monthly, yearly, one-time (default)
  --justification TEXT  Business justification
  --reallocate SRC DST  Reallocate budget between departments
  --check AMOUNT        Check governance requirements only

Examples:
  /invest "Cloud infrastructure upgrade" --amount 5000 --category infrastructure
  /invest "New design tools" --amount 200 --category tooling --recurring monthly
  /invest --reallocate engineering product --amount 500
  /invest --check 10000
```
Exit.

## Step 2: Check Governance Requirements

```bash
uv run .claude/hooks/company/board_governance.py check --decision <decision_type>
```

Map investment to decision type:
- Amount >= `budgetThresholdUsd` (default: $1000) → `major_investment`
- Reallocation >= `reallocationPercentThreshold` (default: 20%) → `budget_reallocation`
- Otherwise → `operational_expense` (CEO only)

**If `--check` flag only:**

```
════════════════════════════════════════════════════════════════════════════════
 INVESTMENT GOVERNANCE: $<amount>
════════════════════════════════════════════════════════════════════════════════

### Requirements

| Field | Value |
|-------|-------|
| Amount | $<amount> |
| Threshold | $<threshold> |
| Requires Board | <Yes/No> |
| Decision Type | <type> |
| Reason | <reason> |

### Required Approvers

| Approver | Role |
|----------|------|
| <id> | <role> |

### Process

[For major investment (>= threshold):]
1. Submit investment proposal via this command
2. CEO reviews and approves
3. Board session auto-scheduled
4. Chair + CEO + CTO must all approve
5. If approved, funds allocated

[For budget reallocation:]
1. Submit reallocation proposal
2. CEO reviews
3. Board quorum reviews (Chair + CEO)
4. If approved, budget transferred

[For operational expense (< threshold):]
1. Submit expense
2. CEO approves
3. Funds allocated immediately

════════════════════════════════════════════════════════════════════════════════
```
Exit.

## Step 3: Analyze Budget Impact

```bash
# Get current economics
cat .company/org.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('economics', {})))"

# Get budget impact analysis
uv run .claude/hooks/company/board_governance.py budget-impact \
  --amount <amount> \
  --type <type> \
  --recurring <period>
```

Calculate:
- Current budget allocation by department
- Impact of investment on available funds
- Monthly/yearly cost projection (for recurring)
- ROI estimation if justification provided

## Step 4: Submit Investment Proposal

For investments requiring board approval, submit as a plan:

```bash
uv run .claude/hooks/company/planning_authority.py submit \
  --title "Investment: <description>" \
  --type initiative \
  --proposed-by <current-user> \
  --size <small|medium|large> \
  --description "Investment proposal: <description>. Amount: $<amount>. Category: <category>. Justification: <justification>."
```

Size mapping based on amount:
- < $500 → small
- $500 - $5000 → medium
- > $5000 → large

## Step 5: Display Submission Result

```
════════════════════════════════════════════════════════════════════════════════
 INVESTMENT PROPOSAL SUBMITTED                                        [<status>]
════════════════════════════════════════════════════════════════════════════════

### Investment Details

| Field | Value |
|-------|-------|
| Description | <description> |
| Amount | $<amount> |
| Type | <type> |
| Category | <category> |
| Recurring | <recurring> |

### Budget Impact

| Metric | Value |
|--------|-------|
| Current Available | $<available> |
| After Investment | $<after> |
| Monthly Impact | $<monthly or "One-time"> |
| Yearly Impact | $<yearly or "One-time"> |

### Governance

| Field | Value |
|-------|-------|
| Requires Board | <Yes/No> |
| Required Approvers | <list> |
| Plan ID | <plan-id> |
| Status | <ceo_review / pending> |

### Approval Flow

[For major investment (requires full board):]
```
CEO Review ──▶ Board Session ──▶ Chair + CEO + CTO Vote ──▶ Approved
     │              │                    │
     │              │                    └── If any reject: BLOCKED
     │              └── Auto-scheduled on CEO approval
     └── /plan-review <plan-id> --approve
```

[For budget reallocation (requires quorum):]
```
CEO Review ──▶ Board Quorum ──▶ Chair + CEO Vote ──▶ Approved
```

[For operational expense (CEO only):]
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
Funds allocated. Update budget tracking.

════════════════════════════════════════════════════════════════════════════════
```

## Step 6: Budget Reallocation (--reallocate flag)

For budget reallocation between departments:

```bash
# Get current department budgets
cat .company/org.json | python3 -c "
import sys,json
d=json.load(sys.stdin)
depts = d.get('departments', [])
for dept in depts:
    print(f\"{dept['id']}: ${dept.get('budget', 0)}\")
"
```

Validate:
- Source department exists and has sufficient funds
- Target department exists
- Amount doesn't exceed source's available budget

```
════════════════════════════════════════════════════════════════════════════════
 BUDGET REALLOCATION PROPOSAL                                         [<status>]
════════════════════════════════════════════════════════════════════════════════

### Reallocation Details

| Field | Value |
|-------|-------|
| From | <source-department> |
| To | <target-department> |
| Amount | $<amount> |

### Current Budgets

| Department | Current | After Reallocation |
|------------|---------|-------------------|
| <source> | $<current> | $<after> |
| <target> | $<current> | $<after> |

### Governance

| Field | Value |
|-------|-------|
| Reallocation % | <percent>% of source budget |
| Threshold | <threshold>% |
| Requires Board | <Yes/No> |
| Required Approvers | <list> |

### Approval Flow

[For significant reallocation (>= threshold%):]
```
CEO Review ──▶ Board Quorum ──▶ Chair + CEO Vote ──▶ Approved
```

[For minor reallocation (< threshold%):]
```
CEO Review ──▶ Approved
```

### Next Steps

CEO should review: `/plan-review <plan-id>`

════════════════════════════════════════════════════════════════════════════════
```

## Step 7: Direct Approval (Small Operational Expenses)

If amount is below threshold and doesn't require board:

```
════════════════════════════════════════════════════════════════════════════════
 OPERATIONAL EXPENSE: CEO APPROVAL REQUIRED                           [pending]
════════════════════════════════════════════════════════════════════════════════

### Expense Details

| Field | Value |
|-------|-------|
| Description | <description> |
| Amount | $<amount> |
| Category | <category> |
| Type | operational_expense |

### Note

This expense is below the board threshold ($<threshold>).
CEO approval is sufficient.

**To approve:**
```bash
/plan-review <plan-id> --approve
```

**To review first:**
```bash
/plan-review <plan-id>
```

════════════════════════════════════════════════════════════════════════════════
```

## Step 8: ROI Analysis (if justification provided)

If `--justification` includes metrics or expected returns:

```
### ROI Analysis

| Metric | Value |
|--------|-------|
| Investment | $<amount> |
| Expected Return | <parsed from justification or "Not specified"> |
| Payback Period | <calculated or "Not calculable"> |
| Confidence | <High/Medium/Low based on justification detail> |

### Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| <risk-1> | <impact> | <mitigation> |

### Board Recommendation

Based on analysis: <RECOMMEND / NEEDS_REVIEW / CAUTION>
Reasoning: <brief reasoning>
```

## Rules

1. **Amount determines governance.** >= $1000 requires full board, < $1000 is CEO only.
2. **Reallocations use percentage threshold.** Default 20% of source budget requires quorum.
3. **Recurring costs are annualized.** Monthly costs show full year impact.
4. **Budget consciousness.** Always show available funds and impact.
5. **Use planning authority.** All investments go through P20 approval system.
6. **Board sessions auto-schedule.** Major investments trigger board session on CEO approval.
7. **Track all decisions.** Investment decisions recorded in planning_approvals.json.
8. **ROI when possible.** Encourage justification with expected returns.

