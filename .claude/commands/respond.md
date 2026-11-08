# /respond — Respond to Escalations

Respond to escalations that require human input, or list all pending escalations awaiting human attention.

## Input
$ARGUMENTS

## Command Syntax

```
/respond task-123 "Your response message"    # Respond to specific escalation
/respond                                      # List pending escalations
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the operation mode:

| Pattern | Mode | Go to |
|---------|------|-------|
| (empty) | LIST | Step 1 |
| `task-id "response"` | RESPOND | Step 2 |
| `task-id response text` | RESPOND | Step 2 |

**If empty arguments:** Go to Step 1 (List Pending)
**If arguments provided:** Go to Step 2 (Respond Mode)

---

## Step 1: List Pending Escalations

Query the input channel for escalations awaiting human attention:

```bash
uv run .claude/hooks/company/input_channel.py pending
```

### 1.1: Parse Response

The input channel returns JSON:

**On success:**
```json
{
  "success": true,
  "escalations": [...],
  "count": 3,
  "tier_info": {
    "tier": 4,
    "name": "Human",
    "description": "Escalations requiring human intervention"
  },
  "queried_at": "2025-02-09T..."
}
```

### 1.2: Display Results

**If escalations exist:**

```
================================================================================
  PENDING ESCALATIONS                                    [awaiting your response]
================================================================================

You have [count] escalation(s) requiring human attention.

+------------------------------------------------------------------------------+
|  ESCALATION QUEUE                                                            |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Tier | Status | Escalated At |
|---|---------|-------|------|--------|--------------|
| 1 | [id] | [title] | 4 (Human) | paused | [timestamp] |
| 2 | [id] | [title] | 4 (Human) | paused | [timestamp] |

### Escalation Details

#### 1. [task_id]: [title]

**Reason:** [escalation reason]
**Escalated By:** [escalated_by]
**Escalated At:** [timestamp]
**Original Assignment:** [original_assignee]
**Attempts:** [attempt_count]

**Context:**
[escalation context/history if available]

---

#### 2. [task_id]: [title]
...

================================================================================

### To Respond

Use `/respond` with the task ID and your response:

/respond [task-id] "Your resolution or guidance here"

### Examples

/respond task-123 "Reassign to senior developer with OAuth experience"
/respond task-456 "Approved - proceed with the proposed approach"
/respond task-789 "Blocked - waiting for client clarification on requirements"

================================================================================
```

**If no escalations:**

```
================================================================================
  NO PENDING ESCALATIONS                                          [all clear]
================================================================================

There are no escalations currently awaiting human attention.

### What This Means

All work is either:
- Being handled by automated agents
- Completed and resolved
- In progress within normal escalation tiers

### Check Company Status

/company-status     - See overall company health
/employee-status    - Check what employees are working on
/dashboard          - Quick operational snapshot

================================================================================
```

---

## Step 2: Respond to Escalation

### 2.1: Extract Task ID and Response

Parse arguments to extract the task ID and response message:

```python
import re

args = arguments.strip()

# Try to match: task-id "quoted response"
quoted_match = re.match(r'^(\S+)\s+"([^"]+)"', args)
if quoted_match:
    task_id = quoted_match.group(1)
    response = quoted_match.group(2)
else:
    # Match: task-id unquoted response text
    parts = args.split(None, 1)  # Split on first whitespace
    if len(parts) >= 2:
        task_id = parts[0]
        response = parts[1].strip('"')  # Remove surrounding quotes if present
    elif len(parts) == 1:
        task_id = parts[0]
        response = None  # Missing response
    else:
        task_id = None
        response = None
```

### 2.2: Validate Inputs

**If task_id is missing:**

```
## Missing Task ID

A task ID is required to respond to an escalation.

**Usage:**
  /respond task-id "Your response message"

**To see pending escalations:**
  /respond

**Examples:**
  /respond task-123 "Reassign to senior developer"
  /respond task-456 "Approved - proceed with implementation"
```
Exit without submitting.

**If response is missing:**

```
## Missing Response

A response message is required.

**Usage:**
  /respond [task-id] "Your response message"

**Task ID provided:** [task_id]

**Examples:**
  /respond [task_id] "Reassign to senior developer with more experience"
  /respond [task_id] "Approved - proceed with the proposed approach"
  /respond [task_id] "Blocked - need more information from client"
```
Exit without submitting.

### 2.3: Submit Response

Call the input channel to submit the escalation response:

```bash
uv run .claude/hooks/company/input_channel.py respond \
  --task-id "[task_id]" \
  --response "[response]"
```

---

## Step 3: Parse Response

The input channel returns JSON:

**On success:**
```json
{
  "success": true,
  "task_id": "task-123",
  "resolution": "Reassign to senior developer",
  "resolved_at": "2025-02-09T...",
  "resolved_by": "human",
  "escalation": { ... }
}
```

**On error:**
```json
{
  "success": false,
  "error": "Error message here"
}
```

---

## Step 4: Display Result

### 4.1: Success Output

```
================================================================================
  ESCALATION RESOLVED                                              [success]
================================================================================

+------------------------------------------------------------------------------+
|  RESOLUTION DETAILS                                                          |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:      [task_id]                                                     |
|  Resolution:   [resolution]                                                  |
|  Resolved By:  [resolved_by]                                                 |
|  Resolved At:  [resolved_at]                                                 |
|                                                                              |
+------------------------------------------------------------------------------+

### What Happens Next

The escalation has been resolved and the work system will:
1. Update the task status based on your response
2. Notify relevant agents of your decision
3. Resume work if applicable

### Next Steps

/respond            - Check for other pending escalations
/company-status     - See overall company status
/employee-status    - Check employee workloads
/dashboard          - Quick operational snapshot

================================================================================
```

### 4.2: Error Output

```
================================================================================
  RESOLUTION FAILED                                                  [error]
================================================================================

**Error:** [error message]

### Common Issues

| Error | Solution |
|-------|----------|
| "task_id not found" | Verify the task ID exists. Run `/respond` to see pending escalations. |
| "escalation not found" | The task may not have an active escalation. Check with `/respond`. |
| "response required" | Provide a response message explaining your decision. |
| "already resolved" | This escalation has already been handled. |

### Try Again

1. List pending escalations:
   /respond

2. Respond with task ID and message:
   /respond task-123 "Your resolution here"

================================================================================
```

---

## Examples

### Example 1: List Pending Escalations

```
/respond

================================================================================
  PENDING ESCALATIONS                                    [awaiting your response]
================================================================================

You have 2 escalation(s) requiring human attention.

+------------------------------------------------------------------------------+
|  ESCALATION QUEUE                                                            |
+------------------------------------------------------------------------------+

| # | Task ID | Title | Tier | Status | Escalated At |
|---|---------|-------|------|--------|--------------|
| 1 | task-abc123 | Implement OAuth integration | 4 (Human) | paused | 2025-02-09T14:30:00Z |
| 2 | task-def456 | Database migration blocked | 4 (Human) | paused | 2025-02-09T15:00:00Z |

### Escalation Details

#### 1. task-abc123: Implement OAuth integration

**Reason:** Requires architectural decision on OAuth provider selection
**Escalated By:** eng-001
**Escalated At:** 2025-02-09T14:30:00Z
**Original Assignment:** eng-001
**Attempts:** 3

**Context:**
Need human decision on whether to use OAuth 2.0 with Google, GitHub, or both.
Budget and timeline implications require management approval.

---

#### 2. task-def456: Database migration blocked

**Reason:** Production database access requires DBA approval
**Escalated By:** eng-002
**Escalated At:** 2025-02-09T15:00:00Z
**Original Assignment:** eng-002
**Attempts:** 2

**Context:**
Migration script is ready but requires elevated permissions.

================================================================================

### To Respond

/respond task-abc123 "Use Google OAuth only for MVP, add GitHub later"
/respond task-def456 "Approved - contact DBA team for access"

================================================================================
```

### Example 2: Respond to Escalation (Quoted Response)

```
/respond task-abc123 "Use Google OAuth only for MVP. Add GitHub integration in Phase 2."

================================================================================
  ESCALATION RESOLVED                                              [success]
================================================================================

+------------------------------------------------------------------------------+
|  RESOLUTION DETAILS                                                          |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:      task-abc123                                                   |
|  Resolution:   Use Google OAuth only for MVP. Add GitHub integration in...   |
|  Resolved By:  human                                                         |
|  Resolved At:  2025-02-09T15:30:00Z                                          |
|                                                                              |
+------------------------------------------------------------------------------+

### What Happens Next

The escalation has been resolved and the work system will:
1. Update the task status based on your response
2. Notify relevant agents of your decision
3. Resume work if applicable

### Next Steps

/respond            - Check for other pending escalations (1 remaining)
/company-status     - See overall company status

================================================================================
```

### Example 3: Respond to Escalation (Unquoted Response)

```
/respond task-def456 Approved - proceed with migration during maintenance window

================================================================================
  ESCALATION RESOLVED                                              [success]
================================================================================

+------------------------------------------------------------------------------+
|  RESOLUTION DETAILS                                                          |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:      task-def456                                                   |
|  Resolution:   Approved - proceed with migration during maintenance window   |
|  Resolved By:  human                                                         |
|  Resolved At:  2025-02-09T15:35:00Z                                          |
|                                                                              |
+------------------------------------------------------------------------------+

...
================================================================================
```

### Example 4: Task Not Found

```
/respond task-invalid "Some response"

================================================================================
  RESOLUTION FAILED                                                  [error]
================================================================================

**Error:** Escalation not found for task: task-invalid

### Common Issues

| Error | Solution |
|-------|----------|
| "task_id not found" | Verify the task ID exists. Run `/respond` to see pending escalations. |

### Try Again

1. List pending escalations:
   /respond

2. Respond with a valid task ID:
   /respond task-123 "Your resolution here"

================================================================================
```

---

## Rules

1. **Task ID is required for responses.** Never submit without a valid task ID.

2. **Response message is required.** Provide clear guidance or resolution.

3. **Show escalation details.** When listing, include context to help the user make informed decisions.

4. **Validate task exists.** Handle "not found" errors gracefully with helpful guidance.

5. **Support both quoted and unquoted responses.** Parse flexibly for good UX.

6. **Show remaining count.** After resolving, indicate if other escalations are pending.

7. **Suggest next steps.** Always show relevant follow-up commands.

8. **Handle errors gracefully.** Parse error responses and show actionable guidance.

---

## Error Handling

### Missing Task ID

```
## Missing Task ID

A task ID is required to respond to an escalation.

**Usage:**
  /respond task-id "Your response message"

**To see pending escalations:**
  /respond
```

### Missing Response

```
## Missing Response

A response message is required when responding to an escalation.

**Task ID:** [task_id]

**Usage:**
  /respond [task_id] "Your response or guidance"

**Examples:**
  /respond [task_id] "Approved - proceed with implementation"
  /respond [task_id] "Reassign to eng-002 who has OAuth experience"
  /respond [task_id] "Blocked - need client clarification first"
```

### Task Not Found

```
## Task Not Found

No active escalation found for task: [task_id]

**Possible reasons:**
1. The task ID is incorrect
2. The escalation has already been resolved
3. The task exists but is not at Tier 4 (Human)

**To see pending escalations:**
  /respond

**To check task status:**
  /company-status
```

### Input Channel Not Found

```
## Input Channel Not Found

The input channel script was not found at:
  .claude/hooks/company/input_channel.py

This may indicate:
1. Gatesmith is not properly installed
2. The hooks directory is missing

**To fix:**
1. Verify Gatesmith installation
2. Check that .claude/hooks/company/ exists
3. Run `/prime` to reload project context
```
