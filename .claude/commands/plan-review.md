# /plan-review — CEO Reviews Pending Plans

CEO command to review and decide on pending plans. Lists pending plans or reviews a specific plan.

**Access:** CEO only (validated via employee context)

## Input
<args/>

Usage:
- `/plan-review` — List all pending plans
- `/plan-review <plan-id>` — View plan details
- `/plan-review <plan-id> --approve` — Approve plan
- `/plan-review <plan-id> --revise "comments"` — Request revision
- `/plan-review <plan-id> --reject "reason"` — Reject plan

## Step 1: Parse Arguments

Parse `<args/>` to determine operation:
- No args: List pending
- Plan ID only: Show details
- Plan ID + --approve: Approve
- Plan ID + --revise "comments": Request revision
- Plan ID + --reject "reason": Reject

## Step 2: List Pending (no args)

```bash
uv run .claude/hooks/company/planning_authority.py pending
```

Display:

```
════════════════════════════════════════════════════════════════════════════════
 PENDING CEO REVIEW                                              [<count> plans]
════════════════════════════════════════════════════════════════════════════════

### Awaiting CEO Decision

| # | Plan ID | Title | Type | Size | Proposed By | Age |
|---|---------|-------|------|------|-------------|-----|
| 1 | plan-xxx | P21: Feature | roadmap_phase | medium | architect | 2h |
| 2 | plan-yyy | Vision Q2 | vision_change | large | ceo | 1d |

### Awaiting Board Review

| # | Plan ID | Title | Board Session | Status |
|---|---------|-------|---------------|--------|
| 1 | plan-zzz | P20: Governance | board-xxx | scheduled |

### Quick Actions

- `/plan-review <plan-id>` — View details
- `/plan-review <plan-id> --approve` — Approve
- `/plan-review <plan-id> --revise "feedback"` — Request changes
- `/plan-review <plan-id> --reject "reason"` — Reject

════════════════════════════════════════════════════════════════════════════════
```

## Step 3: Show Plan Details (plan-id only)

```bash
uv run .claude/hooks/company/planning_authority.py get --plan-id <plan-id>
```

Display full plan details including:
- Title, description, type, size
- Proposal document (if linked)
- Strategic alignment
- Current status and history

## Step 4: CEO Decision (--approve/--revise/--reject)

```bash
uv run .claude/hooks/company/planning_authority.py review \
  --plan-id <plan-id> \
  --decision <approve|revise|reject> \
  --comments "<comments>" \
  --reviewer forge-ceo
```

### Approve Result

```
════════════════════════════════════════════════════════════════════════════════
 CEO DECISION: APPROVED                                              [success]
════════════════════════════════════════════════════════════════════════════════

Plan: <title> (<plan-id>)
Decision: APPROVED
Comments: <comments>

### Next Status

[If board review required:]
Status: board_review
Board Session: <session-id> (auto-scheduled)
Required: CEO + CTO consensus

[If no board review required:]
Status: approved
Ready for implementation

════════════════════════════════════════════════════════════════════════════════
```

### Revise Result

```
════════════════════════════════════════════════════════════════════════════════
 CEO DECISION: REVISION REQUESTED                                    [returned]
════════════════════════════════════════════════════════════════════════════════

Plan: <title> (<plan-id>)
Decision: REVISE
Feedback: <comments>

The plan has been returned to the proposer for revision.
Status: revision

════════════════════════════════════════════════════════════════════════════════
```

### Reject Result

```
════════════════════════════════════════════════════════════════════════════════
 CEO DECISION: REJECTED                                              [rejected]
════════════════════════════════════════════════════════════════════════════════

Plan: <title> (<plan-id>)
Decision: REJECTED
Reason: <reason>

The plan has been rejected and archived.
Status: rejected

════════════════════════════════════════════════════════════════════════════════
```

## Rules

1. **CEO authority only.** This command validates the caller is forge-ceo or has CEO permissions.
2. **Decisions are final.** Once approved/rejected, the decision is recorded permanently.
3. **Board review auto-triggers.** Large plans automatically schedule board sessions on approval.
4. **Track all decisions.** Every CEO decision is logged in planning_approvals.json history.
