# /board-session — C-Level Board Review Session

Initiate or manage C-level board review sessions. Board sessions require CEO + CTO consensus for approval of major initiatives.

**Access:** CEO only can initiate; all executives can vote

## Input
<args/>

Usage:
- `/board-session` — List active sessions
- `/board-session start --agenda "P20 approval"` — Start new session
- `/board-session <session-id>` — View session details
- `/board-session <session-id> vote <plan-id> --approve` — Cast vote
- `/board-session <session-id> close` — Close session (if quorum met)

## Step 1: Parse Arguments

Parse `<args/>` to determine operation:
- No args: List active sessions
- `start --agenda "..."`: Start new session
- Session ID only: Show details
- Session ID + `vote`: Cast vote
- Session ID + `close`: Close session

## Step 2: List Active Sessions (no args)

```bash
uv run .claude/hooks/company/planning_authority.py status
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 BOARD SESSIONS                                               [<count> active]
════════════════════════════════════════════════════════════════════════════════

### Active Sessions

| Session ID | Type | Status | Agenda | Scheduled |
|------------|------|--------|--------|-----------|
| board-xxx | initiative_approval | scheduled | P20: Governance | 2h ago |
| board-yyy | strategic_review | in_progress | Q2 Planning | 1d ago |

### Session Details

**board-xxx: P20 Governance Approval**
- Required: forge-ceo, forge-cto
- Voted: forge-ceo (approve)
- Pending: forge-cto

### Quick Actions

- `/board-session <id>` — View details
- `/board-session <id> vote <plan-id> --approve` — Cast vote
- `/board-session start --agenda "topic"` — Start new session

════════════════════════════════════════════════════════════════════════════════
```

## Step 3: Start New Session

```bash
uv run .claude/hooks/company/planning_authority.py board-session \
  --plan-id <plan-id>
```

Or for ad-hoc session without existing plan:

```
════════════════════════════════════════════════════════════════════════════════
 BOARD SESSION INITIATED                                             [success]
════════════════════════════════════════════════════════════════════════════════

Session ID: <session-id>
Type: <initiative_approval / strategic_review / emergency>
Status: scheduled

### Agenda
- <plan-id>: <plan-title>

### Required Participants
- [ ] forge-ceo
- [ ] forge-cto

### Voting Instructions

Each executive must vote on agenda items:

```
/board-session <session-id> vote <plan-id> --approve
/board-session <session-id> vote <plan-id> --revise "feedback"
/board-session <session-id> vote <plan-id> --reject "reason"
```

Quorum: All required participants must vote
Consensus: Unanimous approval required

════════════════════════════════════════════════════════════════════════════════
```

## Step 4: Cast Vote

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
 VOTE RECORDED                                                       [success]
════════════════════════════════════════════════════════════════════════════════

Session: <session-id>
Plan: <plan-id>
Voter: <executive-id>
Decision: <APPROVE / REVISE / REJECT>
Comments: <comments>

### Current Votes

| Executive | Decision | Comments |
|-----------|----------|----------|
| forge-ceo | APPROVE | Aligns with vision |
| forge-cto | APPROVE | Technically feasible |

### Session Status

[If quorum not met:]
Status: in_progress
Pending votes: <list>

[If quorum met and consensus:]
Status: COMPLETED
Board Decision: APPROVED ✓
Plan <plan-id> is now APPROVED and ready for implementation.

[If quorum met but no consensus:]
Status: COMPLETED
Board Decision: REVISE
Plan <plan-id> requires revision before resubmission.

════════════════════════════════════════════════════════════════════════════════
```

## Step 5: View Session Details

```
════════════════════════════════════════════════════════════════════════════════
 BOARD SESSION: <session-id>
════════════════════════════════════════════════════════════════════════════════

### Session Info

| Field | Value |
|-------|-------|
| Session ID | <session-id> |
| Type | <type> |
| Status | <scheduled/in_progress/completed> |
| Scheduled | <timestamp> |
| Started | <timestamp or "Not started"> |
| Completed | <timestamp or "In progress"> |

### Participants

| Executive | Required | Voted | Decision |
|-----------|----------|-------|----------|
| forge-ceo | Yes | Yes | APPROVE |
| forge-cto | Yes | No | Pending |

### Agenda Items

| Plan ID | Title | CEO Vote | CTO Vote | Result |
|---------|-------|----------|----------|--------|
| plan-xxx | P20: Governance | APPROVE | Pending | Pending |

### Actions

[If not all votes cast:]
Waiting for: <list of pending voters>

Vote command:
```
/board-session <session-id> vote <plan-id> --approve
```

[If all votes cast:]
Session complete. Board decision: <decision>

════════════════════════════════════════════════════════════════════════════════
```

## Step 6: Close Session

Only available when quorum met:

```bash
# Session auto-closes when all votes are cast
# Manual close is not typically needed
```

## Rules

1. **CEO initiates sessions.** Only forge-ceo can start new board sessions.
2. **All executives must vote.** Quorum requires all required participants.
3. **Unanimous approval.** Any rejection or revision request blocks approval.
4. **Auto-scheduled sessions.** Plans requiring board review auto-create sessions.
5. **Session history preserved.** All sessions and votes logged in planning_approvals.json.
6. **One vote per executive per plan.** Votes cannot be changed after recording.
