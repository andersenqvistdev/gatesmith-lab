# /run-loop — Execute Operation Loop Cycle

Run one cycle of the operation loop: poll work queue, claim a task, execute, and report result.

## Input
$ARGUMENTS

## Command Syntax

```
/run-loop                       # Run single loop iteration
/run-loop --dry-run             # Preview without executing
/run-loop --verbose             # Show detailed output
/run-loop --queue=<path>        # Custom work queue path
/run-loop --agent=<id>          # Custom agent ID

# Continuous Execution Flags (Autonomous Mode)
/run-loop --max-tasks N         # Stop after completing N tasks
/run-loop --max-duration Nm     # Stop after N minutes (e.g., 30m, 60m)
/run-loop --until-idle          # Stop when queue becomes empty
/run-loop --continuous          # Run indefinitely (combine with other flags)
```

### Continuous Mode Examples

```
/run-loop --max-tasks 5                      # Execute up to 5 tasks, then stop
/run-loop --max-duration 30m                 # Run for 30 minutes, then stop
/run-loop --until-idle                       # Process all tasks until queue empty
/run-loop --max-tasks 10 --max-duration 60m  # Stop at first limit reached
/run-loop --continuous --max-duration 8h     # Long-running autonomous session
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the operation mode:

| Pattern | Mode | Go to |
|---------|------|-------|
| (empty) | EXECUTE | Step 1 |
| `--dry-run` | PREVIEW | Step 1 (preview mode) |
| `--verbose` | VERBOSE | Step 1 (with detail) |
| Other flags | CUSTOM | Step 1 (with options) |

**Extract flags:**

```python
import re

def parse_run_loop_flags(arguments):
    flags = {
        "dry_run": "--dry-run" in arguments,
        "verbose": "--verbose" in arguments,
        "until_idle": "--until-idle" in arguments,
        "continuous": "--continuous" in arguments,
    }

    # Queue path
    queue_match = re.search(r'--queue[=\s]+(\S+)', arguments)
    if queue_match:
        flags["queue"] = queue_match.group(1)

    # Agent ID
    agent_match = re.search(r'--agent[=\s]+(\S+)', arguments)
    if agent_match:
        flags["agent"] = agent_match.group(1)

    # Max tasks (stop after N completed tasks)
    max_tasks_match = re.search(r'--max-tasks[=\s]+(\d+)', arguments)
    if max_tasks_match:
        flags["max_tasks"] = int(max_tasks_match.group(1))

    # Max duration (stop after N minutes)
    max_duration_match = re.search(r'--max-duration[=\s]+(\d+)m', arguments)
    if max_duration_match:
        flags["max_duration_minutes"] = int(max_duration_match.group(1))

    return flags
```

### Continuous Mode Detection

If any continuous flag is present, the loop enters **autonomous mode**:

```python
def is_continuous_mode(flags):
    return any([
        flags.get("max_tasks"),
        flags.get("max_duration_minutes"),
        flags.get("until_idle"),
        flags.get("continuous"),
    ])
```

---

## Step 1: Execute Loop

### 1.1: Build Command

Construct the command based on parsed flags:

**Base command:**
```bash
uv run .claude/hooks/company/operation_loop.py poll
```

**With options:**
```bash
uv run .claude/hooks/company/operation_loop.py poll \
  [--agent-id <id>] \
  [--queue <path>]
```

### 1.2: Dry Run Mode

**If `--dry-run` flag is set:**

First, check what tasks are claimable without executing:

```bash
uv run .claude/hooks/company/operation_loop.py claimable [--queue <path>]
```

Parse the JSON response and display preview:

```
================================================================================
  OPERATION LOOP — DRY RUN                                        [preview mode]
================================================================================

### Claimable Tasks

| # | Task ID | Title | Priority | Dependencies |
|---|---------|-------|----------|--------------|
| 1 | [id] | [title] | [priority] | [dep_count] |
| 2 | [id] | [title] | [priority] | [dep_count] |

### What Would Happen

If executed, the loop would:
1. Claim task [first_task_id]: "[title]"
2. Validate against trust tiers
3. Execute or escalate based on tier validation
4. Release task with result

### Run for Real

/run-loop                    # Execute loop iteration
/run-loop --verbose          # Execute with detailed output

================================================================================
```

**Exit after preview.** Do not execute.

### 1.3: Execute Loop

**If not dry-run:**

Run the poll command:

```bash
uv run .claude/hooks/company/operation_loop.py poll \
  [--agent-id <id>] \
  [--queue <path>]
```

### 1.4: Continuous Loop Execution

**If continuous mode is active** (any continuous flag set):

Instead of a single iteration, run a loop with stop condition checking:

```python
import time
from datetime import datetime, timedelta

def run_continuous_loop(flags):
    start_time = datetime.now()
    tasks_completed = 0
    consecutive_idle = 0
    max_consecutive_idle = 3  # Stop after 3 idle cycles in --until-idle mode

    while True:
        # Execute one iteration
        result = execute_single_iteration(flags)

        # Track completion
        if result["action"] == "executed":
            tasks_completed += 1
            consecutive_idle = 0
        elif result["action"] == "idle":
            consecutive_idle += 1
        elif result["action"] == "escalated":
            consecutive_idle = 0  # Reset, but don't count as completed

        # Check stop conditions
        stop_reason = check_stop_conditions(
            flags, start_time, tasks_completed, consecutive_idle, max_consecutive_idle
        )
        if stop_reason:
            return {"stopped": True, "reason": stop_reason, "tasks_completed": tasks_completed}

        # Brief pause between iterations (avoid hammering the queue)
        time.sleep(2)
```

---

## Stop Conditions

In continuous mode, the loop stops when any of these conditions are met:

| Flag | Stop Condition | Priority |
|------|----------------|----------|
| `--max-tasks N` | After completing N tasks successfully | 1 (first) |
| `--max-duration Nm` | After N minutes elapsed from start | 2 |
| `--until-idle` | After 3 consecutive idle cycles (no tasks available) | 3 |
| (none) | Never stops automatically (use Ctrl+C) | - |

### Stop Condition Logic

```python
def check_stop_conditions(flags, start_time, tasks_completed, consecutive_idle, max_idle):
    # Priority 1: Max tasks reached
    if flags.get("max_tasks") and tasks_completed >= flags["max_tasks"]:
        return f"Completed {tasks_completed} tasks (max-tasks limit)"

    # Priority 2: Duration exceeded
    if flags.get("max_duration_minutes"):
        elapsed = datetime.now() - start_time
        max_duration = timedelta(minutes=flags["max_duration_minutes"])
        if elapsed >= max_duration:
            return f"Duration limit reached ({flags['max_duration_minutes']}m)"

    # Priority 3: Queue idle (only if --until-idle set)
    if flags.get("until_idle") and consecutive_idle >= max_idle:
        return f"Queue idle ({consecutive_idle} consecutive idle cycles)"

    return None  # Continue running
```

### Stop Condition Display

When a stop condition is triggered:

```
================================================================================
  OPERATION LOOP                                               [stopped]
================================================================================

+------------------------------------------------------------------------------+
|  CONTINUOUS LOOP COMPLETED                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Stop Reason:     [reason]                                                   |
|  Tasks Completed: [N]                                                        |
|  Duration:        [elapsed time]                                             |
|  Escalations:     [N]                                                        |
|                                                                              |
+------------------------------------------------------------------------------+

### Summary

| Metric | Value |
|--------|-------|
| Total iterations | [N] |
| Tasks completed | [N] |
| Tasks escalated | [N] |
| Idle cycles | [N] |
| Errors | [N] |

### Next Steps

- `/run-loop --until-idle` — Continue processing remaining tasks
- `/pending` — Check escalations requiring attention
- `/dashboard` — View operational status

================================================================================
```

---

## Step 2: Parse Response

The operation loop returns JSON. Parse the response:

```json
{
  "action": "executed|idle|escalated|claim_failed|failed",
  "reason": "description",
  "task_id": "task-abc123",
  "result": "completed|escalated|failed",
  "task": { ... },
  "escalation": { ... }
}
```

### Action Types

| Action | Meaning | Exit Code |
|--------|---------|-----------|
| `executed` | Task was claimed and completed successfully | 0 |
| `idle` | No tasks available in queue | 0 |
| `escalated` | Task requires human approval (gated operation) | 2 |
| `claim_failed` | Failed to claim task (race condition or error) | 1 |
| `failed` | Task execution or release failed | 1 |

---

## Step 3: Display Result

### 3.1: Executed Successfully

```
================================================================================
  OPERATION LOOP                                                     [executed]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    executed                                                         |
|  Task ID:   [task_id]                                                        |
|  Title:     [task.title]                                                     |
|  Result:    completed                                                        |
|  Duration:  [if available]                                                   |
|                                                                              |
+------------------------------------------------------------------------------+

### Task Details

**Priority:** [priority] ([priority_label])
**Department:** [department or "unassigned"]
**Claimed By:** [agent_id]

### Next Steps

- `/run-loop` — Run another loop iteration
- `/pending` — Check items requiring human attention
- `/dashboard` — Quick operational snapshot

================================================================================
```

### 3.2: Idle (No Tasks)

```
================================================================================
  OPERATION LOOP                                                         [idle]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    idle                                                             |
|  Reason:    No tasks available in queue                                      |
|                                                                              |
+------------------------------------------------------------------------------+

### Queue Status

No tasks are currently claimable. This may mean:
- Work queue is empty
- All tasks have unsatisfied dependencies
- Tasks are in backoff period after failures

### Next Steps

- `/submit "task description"` — Submit new work
- `/pending` — Check blocked tasks
- `/company-status` — Full queue status

================================================================================
```

### 3.3: Escalated (Gated Operation)

```
================================================================================
  OPERATION LOOP                                                    [escalated]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    escalated                                                        |
|  Task ID:   [task_id]                                                        |
|  Title:     [task.title]                                                     |
|  Result:    escalated                                                        |
|                                                                              |
+------------------------------------------------------------------------------+

### Escalation Details

**Reason:** [escalation.reason]
**Matched Pattern:** [escalation.matched_pattern]
**Escalated To:** Tier 4 (Human)

This task contains a gated operation that requires human approval:
- Deploy, publish, or release operations
- Git push to remote
- Production infrastructure changes

### Required Action

Use `/respond [task_id] "approval or rejection"` to resolve this escalation.

### Next Steps

- `/respond [task_id] "approved"` — Approve the operation
- `/respond [task_id] "rejected: reason"` — Reject with feedback
- `/pending` — View all pending escalations

================================================================================
```

### 3.4: Error States

```
================================================================================
  OPERATION LOOP                                                        [error]
================================================================================

**Error:** [action]: [reason]

### Common Issues

| Error | Possible Cause | Solution |
|-------|---------------|----------|
| Queue file does not exist | Company not initialized | Run `/company-init` |
| Lock timeout | Another process using queue | Wait and retry |
| Task not found | Race condition | Run `/run-loop` again |
| Release failed | Queue corruption | Check `.company/state/work_queue.json` |

### Troubleshooting

1. Check company initialization: `ls .company/`
2. Verify queue file: `ls .company/state/work_queue.json`
3. Check queue contents: `uv run .claude/hooks/company/operation_loop.py claimable`
4. View company status: `/company-status`

================================================================================
```

---

## Step 4: Verbose Mode

**If `--verbose` flag is set**, include additional details:

```
================================================================================
  OPERATION LOOP                                          [executed] [verbose]
================================================================================

### Execution Timeline

| Step | Time | Action |
|------|------|--------|
| 1 | [timestamp] | Polled queue for claimable tasks |
| 2 | [timestamp] | Found [N] claimable tasks |
| 3 | [timestamp] | Claimed task [task_id] |
| 4 | [timestamp] | Validated against trust tiers |
| 5 | [timestamp] | Executed task |
| 6 | [timestamp] | Released task with result: completed |

### Task Metadata

```json
[full task object]
```

### Queue State After

- Pending: [N] tasks
- In Progress: [N] tasks
- Completed: [N] tasks
- Blocked: [N] tasks

================================================================================
```

---

## Cron Setup

To run the loop automatically, set up a cron job:

### Every 5 Minutes

```bash
# Add to crontab (crontab -e)
*/5 * * * * cd /path/to/project && uv run .claude/hooks/company/operation_loop.py poll >> /var/log/operation-loop.log 2>&1
```

### Every Minute (High-Frequency)

```bash
* * * * * cd /path/to/project && uv run .claude/hooks/company/operation_loop.py poll >> /var/log/operation-loop.log 2>&1
```

### With Agent ID

```bash
*/5 * * * * cd /path/to/project && uv run .claude/hooks/company/operation_loop.py poll --agent-id cron-worker >> /var/log/operation-loop.log 2>&1
```

### Log Rotation

```bash
# Add log rotation config to /etc/logrotate.d/operation-loop
/var/log/operation-loop.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

---

## Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Task executed successfully OR no tasks available | Normal operation |
| 1 | Error during execution (claim failed, release failed) | Check logs, retry |
| 2 | Task escalated (requires human approval) | Use `/respond` to resolve |

---

## Examples

### Example 1: Run Single Loop Iteration

```
/run-loop

================================================================================
  OPERATION LOOP                                                     [executed]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    executed                                                         |
|  Task ID:   task-1707494400-abc                                              |
|  Title:     Fix login button styling                                         |
|  Result:    completed                                                        |
|                                                                              |
+------------------------------------------------------------------------------+

### Task Details

**Priority:** 3 (Normal)
**Department:** engineering
**Claimed By:** operation-loop

### Next Steps

- `/run-loop` — Run another loop iteration
- `/pending` — Check items requiring human attention
- `/dashboard` — Quick operational snapshot

================================================================================
```

### Example 2: Preview What Would Happen

```
/run-loop --dry-run

================================================================================
  OPERATION LOOP — DRY RUN                                        [preview mode]
================================================================================

### Claimable Tasks

| # | Task ID | Title | Priority | Dependencies |
|---|---------|-------|----------|--------------|
| 1 | task-abc123 | Fix login button styling | 3 | 0 |
| 2 | task-def456 | Add dark mode toggle | 3 | 1 |
| 3 | task-ghi789 | Update footer links | 4 | 0 |

### What Would Happen

If executed, the loop would:
1. Claim task task-abc123: "Fix login button styling"
2. Validate against trust tiers
3. Execute (no gated operations detected)
4. Release task with result: completed

### Run for Real

/run-loop                    # Execute loop iteration
/run-loop --verbose          # Execute with detailed output

================================================================================
```

### Example 3: Empty Queue

```
/run-loop

================================================================================
  OPERATION LOOP                                                         [idle]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    idle                                                             |
|  Reason:    No tasks available in queue                                      |
|                                                                              |
+------------------------------------------------------------------------------+

### Queue Status

No tasks are currently claimable. This may mean:
- Work queue is empty
- All tasks have unsatisfied dependencies
- Tasks are in backoff period after failures

### Next Steps

- `/submit "task description"` — Submit new work
- `/pending` — Check blocked tasks
- `/company-status` — Full queue status

================================================================================
```

### Example 4: Gated Operation Escalation

```
/run-loop

================================================================================
  OPERATION LOOP                                                    [escalated]
================================================================================

+------------------------------------------------------------------------------+
|  LOOP RESULT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Action:    escalated                                                        |
|  Task ID:   task-xyz789                                                      |
|  Title:     Deploy to production                                             |
|  Result:    escalated                                                        |
|                                                                              |
+------------------------------------------------------------------------------+

### Escalation Details

**Reason:** Operation matches gated keyword: deploy
**Matched Pattern:** deploy
**Escalated To:** Tier 4 (Human)

This task contains a gated operation that requires human approval:
- Deploy, publish, or release operations
- Git push to remote
- Production infrastructure changes

### Required Action

Use `/respond task-xyz789 "approval or rejection"` to resolve this escalation.

### Next Steps

- `/respond task-xyz789 "approved"` — Approve the operation
- `/respond task-xyz789 "rejected: not ready for production"` — Reject with feedback
- `/pending` — View all pending escalations

================================================================================
```

### Example 5: Check Loop Status

```bash
# View claimable tasks directly
uv run .claude/hooks/company/operation_loop.py claimable

# View with custom queue
uv run .claude/hooks/company/operation_loop.py claimable --queue /custom/path/queue.json
```

### Example 6: Autonomous Mode — Process 5 Tasks

```
/run-loop --max-tasks 5

================================================================================
  OPERATION LOOP                                               [stopped]
================================================================================

+------------------------------------------------------------------------------+
|  CONTINUOUS LOOP COMPLETED                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Stop Reason:     Completed 5 tasks (max-tasks limit)                        |
|  Tasks Completed: 5                                                          |
|  Duration:        12m 34s                                                    |
|  Escalations:     1                                                          |
|                                                                              |
+------------------------------------------------------------------------------+

### Summary

| Metric | Value |
|--------|-------|
| Total iterations | 7 |
| Tasks completed | 5 |
| Tasks escalated | 1 |
| Idle cycles | 0 |
| Errors | 1 |

### Next Steps

- `/run-loop --until-idle` — Continue processing remaining tasks
- `/pending` — Check escalations requiring attention
- `/dashboard` — View operational status

================================================================================
```

### Example 7: Autonomous Mode — Time-Limited Session

```
/run-loop --max-duration 30m

================================================================================
  OPERATION LOOP                                               [stopped]
================================================================================

+------------------------------------------------------------------------------+
|  CONTINUOUS LOOP COMPLETED                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Stop Reason:     Duration limit reached (30m)                               |
|  Tasks Completed: 12                                                         |
|  Duration:        30m 02s                                                    |
|  Escalations:     0                                                          |
|                                                                              |
+------------------------------------------------------------------------------+

### Summary

| Metric | Value |
|--------|-------|
| Total iterations | 14 |
| Tasks completed | 12 |
| Tasks escalated | 0 |
| Idle cycles | 2 |
| Errors | 0 |

### Next Steps

- `/run-loop --max-duration 30m` — Run another 30-minute session
- `/pending` — Check escalations requiring attention
- `/dashboard` — View operational status

================================================================================
```

### Example 8: Autonomous Mode — Process Until Queue Empty

```
/run-loop --until-idle

================================================================================
  OPERATION LOOP                                               [stopped]
================================================================================

+------------------------------------------------------------------------------+
|  CONTINUOUS LOOP COMPLETED                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Stop Reason:     Queue idle (3 consecutive idle cycles)                     |
|  Tasks Completed: 8                                                          |
|  Duration:        5m 23s                                                     |
|  Escalations:     2                                                          |
|                                                                              |
+------------------------------------------------------------------------------+

### Summary

| Metric | Value |
|--------|-------|
| Total iterations | 11 |
| Tasks completed | 8 |
| Tasks escalated | 2 |
| Idle cycles | 3 |
| Errors | 0 |

### Note

Queue became empty or all remaining tasks are blocked/escalated.
Check `/pending` for items requiring human attention.

================================================================================
```

### Example 9: Combined Limits — First Limit Wins

```
/run-loop --max-tasks 10 --max-duration 60m --verbose

# This will stop when EITHER:
# - 10 tasks are completed, OR
# - 60 minutes have elapsed
# Whichever comes first.

================================================================================
  OPERATION LOOP                                      [stopped] [verbose]
================================================================================

+------------------------------------------------------------------------------+
|  CONTINUOUS LOOP COMPLETED                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Stop Reason:     Completed 10 tasks (max-tasks limit)                       |
|  Tasks Completed: 10                                                         |
|  Duration:        42m 17s                                                    |
|  Escalations:     0                                                          |
|                                                                              |
+------------------------------------------------------------------------------+

### Execution Timeline

| Iteration | Time | Task ID | Action | Duration |
|-----------|------|---------|--------|----------|
| 1 | 00:00 | task-001 | executed | 3m 12s |
| 2 | 03:14 | task-002 | executed | 4m 05s |
| 3 | 07:21 | task-003 | executed | 2m 48s |
| ... | ... | ... | ... | ... |
| 12 | 39:05 | task-010 | executed | 3m 12s |

### Summary

| Metric | Value |
|--------|-------|
| Total iterations | 12 |
| Tasks completed | 10 |
| Tasks escalated | 0 |
| Idle cycles | 2 |
| Errors | 0 |
| Time remaining | 17m 43s |

================================================================================
```

---

## Related Commands

| Command | Purpose |
|---------|---------|
| `/submit` | Submit work to the queue |
| `/pending` | View items requiring human attention |
| `/respond` | Respond to escalations |
| `/dashboard` | Quick operational snapshot |
| `/company-status` | Full company status |

## Architecture Reference

- ADR-001: Continuous Operation Loop Architecture
- Trust Tiers: See CLAUDE.md for tier definitions
- Escalation: See `.claude/hooks/company/escalation.py`

---

## Rules

1. **One iteration per invocation.** Each `/run-loop` call executes exactly one poll cycle (unless continuous flags are used).

2. **Trust tier validation.** All tasks are validated against trust tiers before execution. Gated operations are escalated automatically.

3. **Atomic execution.** Tasks are claimed atomically with file locking to prevent race conditions.

4. **Backoff respect.** Failed tasks enter exponential backoff (60s, 120s, 240s, up to 1 hour). The loop will not claim tasks in backoff.

5. **Escalation on max retries.** After 3 failed attempts, tasks are escalated to human tier.

6. **Dry run is safe.** The `--dry-run` flag only reads queue state, never modifies it.

7. **Exit codes are meaningful.** Use exit codes to determine next action in automation scripts.

8. **Verbose for debugging.** Use `--verbose` when troubleshooting loop behavior.

9. **Continuous mode requires limits.** When using `--continuous` without limits, the loop runs indefinitely. Always pair with `--max-tasks` or `--max-duration` for bounded execution.

10. **First limit wins.** When multiple stop conditions are set (e.g., `--max-tasks 10 --max-duration 60m`), the loop stops when the first condition is met.

11. **Graceful stop on escalation.** Escalated tasks count toward iterations but not completed tasks. The loop continues unless manually stopped.

12. **Idle detection requires patience.** The `--until-idle` flag waits for 3 consecutive idle cycles before stopping, preventing premature exit during brief queue emptiness.
