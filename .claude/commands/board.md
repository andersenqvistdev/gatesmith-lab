# /board — Steering Board Management

Manage the steering board including composition, meetings, voting, and dynamic advisor management.

**Access:** All executives can view; Chair/CEO can modify

## Input
$ARGUMENTS

Usage:
- `/board` — Show current board composition
- `/board add-advisor "expertise" --type [type]` — Add external advisor
- `/board remove-advisor [advisor-id]` — Remove an advisor
- `/board meeting --agenda "topic"` — Schedule board meeting
- `/board vote [session-id] [plan-id] --approve` — Cast board vote
- `/board expertise` — Show board expertise coverage
- `/board governance [decision-type]` — Check governance requirements
- `/board rotate-chair [new-chair-id]` — Rotate board chair (requires consensus)

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine operation:
- No args: Show composition
- `add-advisor`: Add new external advisor
- `remove-advisor`: Remove existing advisor
- `meeting`: Schedule or view meetings
- `vote`: Cast vote on pending decision
- `expertise`: Show expertise map
- `governance`: Check governance requirements
- `rotate-chair`: Rotate board chair

## Step 2: Show Board Composition (no args)

```bash
uv run .claude/hooks/company/board_governance.py status
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 STEERING BOARD                                                    [<N> members]
════════════════════════════════════════════════════════════════════════════════

### Board Chair (External)

| Field | Value |
|-------|-------|
| ID | board-chair |
| Type | External Advisor |
| Expertise | <expertise list> |
| Status | active |

### Executives (Internal)

| ID | Name | Role | Voting |
|----|------|------|--------|
| forge-ceo | Gatesmith CEO | Executive | Yes |
| forge-cto | Gatesmith CTO | Executive | Yes |

### External Advisors (Dynamic)

| ID | Name | Type | Expertise | Added |
|----|------|------|-----------|-------|
| <id> | <name> | <type> | <expertise> | <date> |

### Governance

Quorum: <quorum members>
Max Size: <max_size> | Current: <current> | Capacity: <remaining>

### Quick Actions

- `/board add-advisor "expertise" --type industry-expert` — Add advisor
- `/board expertise` — View expertise coverage
- `/board governance hiring_executive` — Check governance requirements

════════════════════════════════════════════════════════════════════════════════
```

## Step 3: Add External Advisor

Parse `add-advisor` arguments:
- First quoted string: expertise description
- `--type TYPE`: One of industry-expert, customer-advocate, investor-rep, domain-advisor
- `--name NAME`: Optional display name

```bash
uv run .claude/hooks/company/board_governance.py add-advisor \
  --expertise "<expertise>" \
  --type <type> \
  --name "<name>"
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 ADVISOR ADDED                                                         [success]
════════════════════════════════════════════════════════════════════════════════

### New Advisor

| Field | Value |
|-------|-------|
| ID | <advisor-id> |
| Name | <name> |
| Type | <type> |
| Expertise | <expertise> |
| Status | active |

### Board Status

Members: <N> / <max_size>
Capacity Remaining: <remaining>

### Next Steps

- View board: `/board`
- Add another: `/board add-advisor "expertise" --type <type>`
- Remove: `/board remove-advisor <id>`

════════════════════════════════════════════════════════════════════════════════
```

## Step 4: Remove External Advisor

```bash
uv run .claude/hooks/company/board_governance.py remove-advisor --id <advisor-id>
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 ADVISOR REMOVED                                                       [success]
════════════════════════════════════════════════════════════════════════════════

Removed: <advisor-name> (<advisor-id>)
Type: <type>
Expertise: <expertise>

Board Size: <N> / <max_size>

════════════════════════════════════════════════════════════════════════════════
```

## Step 5: Show Expertise Coverage

```bash
uv run .claude/hooks/company/board_governance.py status
uv run .claude/hooks/company/board_governance.py domain-expertise --template <domain>
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 BOARD EXPERTISE COVERAGE                                          [<domain>]
════════════════════════════════════════════════════════════════════════════════

### Domain: <domain>
Industry Focus: <industry_focus>

### Chair Expertise

| Area | Covered By |
|------|------------|
| <expertise-1> | Board Chair |
| <expertise-2> | Board Chair |
| <expertise-3> | Board Chair |

### Advisor Expertise

| Area | Covered By |
|------|------------|
| <expertise-1> | Domain Advisor |
| <expertise-2> | External Advisor |

### Coverage Gaps

[If any gaps identified:]
- <gap-1>: Consider adding <advisor-type>
- <gap-2>: Consider adding <advisor-type>

[If no gaps:]
All domain expertise areas covered.

### Executive Coverage

| Role | Focus |
|------|-------|
| forge-ceo | Vision, Strategy, Market Positioning |
| forge-cto | Technical, Architecture, Security |

════════════════════════════════════════════════════════════════════════════════
```

## Step 6: Check Governance Requirements

```bash
uv run .claude/hooks/company/board_governance.py check --decision <decision-type>
```

Decision types:
- `hiring_executive` — Hiring VP or above
- `hiring_team_lead` — Hiring team lead
- `hiring_employee` — Hiring regular employee
- `major_investment` — Major budget spend
- `budget_reallocation` — Moving budget between areas
- `expansion` — Market or product expansion
- `strategic_pivot` — Major direction change

Display:

```
════════════════════════════════════════════════════════════════════════════════
 GOVERNANCE CHECK: <decision-type>
════════════════════════════════════════════════════════════════════════════════

### Requirements

| Field | Value |
|-------|-------|
| Requires Board | <Yes/No> |
| Decision Type | <type> |
| Reason | <reason> |

### Required Participants

| Participant | Role |
|-------------|------|
| <id> | <role> |
| <id> | <role> |

### Process

[If board required:]
1. Submit proposal via `/plan-submit` or `/hire` or `/invest`
2. Board session will be scheduled automatically
3. Required participants must vote
4. Unanimous approval required for major decisions

[If CEO only:]
1. Submit for CEO review
2. CEO decision is final

════════════════════════════════════════════════════════════════════════════════
```

## Step 7: Schedule Board Meeting

```bash
uv run .claude/hooks/company/planning_authority.py board-session --plan-id <plan-id>
```

Or for ad-hoc meeting:

```
════════════════════════════════════════════════════════════════════════════════
 BOARD MEETING SCHEDULED                                               [success]
════════════════════════════════════════════════════════════════════════════════

### Meeting Details

| Field | Value |
|-------|-------|
| Session ID | <session-id> |
| Type | <initiative_approval / strategic_review / emergency> |
| Status | scheduled |
| Agenda | <agenda> |

### Required Participants

- [ ] board-chair
- [ ] forge-ceo
- [ ] forge-cto

### Next Steps

Each participant must vote:
```
/board vote <session-id> <plan-id> --approve
/board vote <session-id> <plan-id> --revise "feedback"
/board vote <session-id> <plan-id> --reject "reason"
```

════════════════════════════════════════════════════════════════════════════════
```

## Step 8: Cast Board Vote

```bash
uv run .claude/hooks/company/planning_authority.py board-vote \
  --session-id <session-id> \
  --executive-id <voter-id> \
  --plan-id <plan-id> \
  --decision <approve|revise|reject> \
  --comments "<comments>"
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 VOTE RECORDED                                                         [success]
════════════════════════════════════════════════════════════════════════════════

Session: <session-id>
Plan: <plan-id>
Voter: <executive-id>
Decision: <APPROVE / REVISE / REJECT>
Comments: <comments>

### Current Votes

| Participant | Decision | Comments |
|-------------|----------|----------|
| board-chair | APPROVE | Market timing is right |
| forge-ceo | APPROVE | Aligns with vision |
| forge-cto | Pending | - |

### Session Status

[If quorum not met:]
Status: in_progress
Pending votes: <list>

[If quorum met and consensus:]
Status: COMPLETED
Board Decision: APPROVED ✓
Plan <plan-id> is now APPROVED.

════════════════════════════════════════════════════════════════════════════════
```

## Step 9: Rotate Board Chair

```bash
uv run .claude/hooks/company/board_governance.py rotate-chair \
  --new-chair <advisor-id> \
  --consensus "<ceo:approve,cto:approve>"
```

**Note:** Chair rotation requires consensus from current quorum members.

Display:

```
════════════════════════════════════════════════════════════════════════════════
 CHAIR ROTATION                                                        [success]
════════════════════════════════════════════════════════════════════════════════

### Previous Chair

| Field | Value |
|-------|-------|
| ID | <old-chair-id> |
| Name | <old-name> |
| Status | Moved to External Advisors |

### New Chair

| Field | Value |
|-------|-------|
| ID | <new-chair-id> |
| Name | <new-name> |
| Expertise | <expertise> |

### Updated Quorum

- <new-chair-id>
- forge-ceo

════════════════════════════════════════════════════════════════════════════════
```

## Rules

1. **Board Chair is external.** The chair provides outside perspective, not internal operations.
2. **Executives are internal.** CEO and CTO are internal board members with voting rights.
3. **Dynamic advisors can join anytime.** Use `/board add-advisor` to add expertise as needed.
4. **Board size is limited.** Default max is 7 members to maintain efficiency.
5. **Quorum required for decisions.** At minimum, Chair + CEO must participate.
6. **Chair rotation requires consensus.** Cannot unilaterally change the chair.
7. **All votes are recorded.** Board decisions are tracked in planning_approvals.json.
