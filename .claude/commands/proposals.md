# /proposals — View and Manage Employee Proposals

View and approve/reject employee-submitted work proposals.

## Usage

```bash
# List pending proposals
/proposals

# Filter by employee
/proposals --by senior-python-developer

# Approve a proposal
/proposals approve <task-id>

# Reject a proposal with feedback
/proposals reject <task-id> --reason "Needs more detail"

# Auto-approve low-risk proposals
/proposals auto-approve
```

## Arguments

- `approve <task-id>` - Approve a proposal and move to pending queue
- `reject <task-id>` - Reject with required `--reason`
- `auto-approve` - Auto-approve low-risk proposals (TODOs, docs)
- `--by <employee-id>` - Filter proposals by employee
- `--reason <text>` - Rejection reason (required for reject)
- `--priority <1-4>` - Override priority when approving

## Instructions

<command name="proposals">
Execute the /proposals command to view and manage employee proposals.

**For listing proposals:**
1. Load the work queue and extract proposals from the "proposed" queue
2. Display proposals in a formatted table showing:
   - Task ID
   - Title (truncated if long)
   - Proposer (employee ID)
   - Type (todo, improvement, follow_up)
   - Priority
   - Proposed date

**For approve:**
1. Verify you have manager permissions
2. Call work_allocator.approve_proposal() with task_id and your ID
3. Optionally adjust priority
4. Report success/failure

**For reject:**
1. Verify you have manager permissions
2. Require --reason flag
3. Call work_allocator.reject_proposal() with task_id, your ID, and reason
4. Report success/failure

**For auto-approve:**
1. Call manager_review.auto_approve_low_risk_proposals()
2. Report how many were approved/skipped

**Example output for /proposals:**
```
Pending Proposals (3)
─────────────────────────────────────────────────────────
ID                       Title                          Proposer              Type         Priority
task-20260214-abc123     [TODO] Add input validation    senior-python-dev     improvement  3
task-20260214-def456     Document API endpoints         technical-writer      follow_up    4
task-20260214-ghi789     [FIXME] Handle edge case       senior-python-dev     bug          2

Commands:
  /proposals approve <id>           - Approve proposal
  /proposals reject <id> --reason   - Reject with feedback
  /proposals auto-approve           - Auto-approve low-risk
```

**Example output for /proposals approve task-20260214-abc123:**
```
Proposal Approved

Task: [TODO] Add input validation
ID: task-20260214-abc123
Approved by: forge-architect
Status: pending (ready for assignment)
```
</command>

## Related Commands

- `/reviews` - View tasks awaiting quality review
- `/pending` - View all items needing attention
- `/company-request` - Submit new work request
