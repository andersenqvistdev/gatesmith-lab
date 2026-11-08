# /progress — Quick Progress Overview

Show a fast, human-friendly overview of company progress and recent activity. Perfect for quick status checks without opening the web dashboard.

## Input

$ARGUMENTS

Supported arguments:
- (none) — Show summary view
- `--live` — Watch mode with auto-refresh (5s interval)
- `--tasks` — Focus on task statistics
- `--activity` — Show recent activity feed
- `--daemon` — Show daemon-specific metrics
- `--since=<duration>` — Activity since duration (e.g., "1h", "30m", "1d")

## Instructions

<command name="progress">
Display a quick progress overview combining daemon status, task metrics, and recent activity.

**Data Sources:**
1. **Daemon heartbeat** (`.company/daemon.heartbeat`) - Real-time daemon metrics
2. **Progress tracker** (`uv run .claude/hooks/company/progress_tracker.py company`)
3. **Activity log** (`logs/activity.jsonl`) - Recent tool activity
4. **Work queue** (`.company/state/work_queue.json`) - Task details

**Default View (no arguments):**

```
═══════════════════════════════════════════════════════════════
 GATESMITH PROGRESS                                    [2026-02-22]
═══════════════════════════════════════════════════════════════

 DAEMON STATUS
 ──────────────────────────────────────────────────────────────
 Status:        RUNNING (21h 45m uptime)
 Circuit:       CLOSED (healthy)
 Success Rate:  100% (126/126 tasks)

 TASK PROGRESS
 ──────────────────────────────────────────────────────────────
 Completed:     311 tasks (100%)
 In Progress:   0 tasks
 Pending:       0 tasks
 Blocked:       0 tasks

 RECENT ACTIVITY (last 1h)
 ──────────────────────────────────────────────────────────────
 08:23  [senior-python-developer] Completed task-20260222-abc123
 08:15  [technical-writer] Updated documentation
 08:02  [forge-cto] Strategic planning cycle complete
 07:45  [daemon] Poll cycle #127 - no pending work

═══════════════════════════════════════════════════════════════
```

**Implementation:**

1. **Read daemon heartbeat:**
   ```bash
   cat .company/daemon.heartbeat 2>/dev/null || echo '{"status": "stopped"}'
   ```

2. **Get progress metrics:**
   ```bash
   uv run .claude/hooks/company/progress_tracker.py company 2>/dev/null
   ```

3. **Get recent activity:**
   ```bash
   # Last 20 entries from activity.jsonl
   tail -20 logs/activity.jsonl 2>/dev/null | jq -s '.' 2>/dev/null || echo '[]'
   ```

4. **Format and display** using the template above

**--live Mode:**

Stream updates every 5 seconds:
```bash
watch -n 5 'uv run .claude/hooks/company/progress_tracker.py company'
```

Or use manual refresh loop with timestamp updates.

**--activity Mode:**

Show expanded activity feed with more detail:
```
═══════════════════════════════════════════════════════════════
 ACTIVITY FEED                                     (last 1 hour)
═══════════════════════════════════════════════════════════════

 08:23:15  senior-python-developer
           Tool: Bash (pytest tests/)
           Status: SUCCESS
           Task: Implement test coverage improvements

 08:22:45  senior-python-developer
           Tool: Edit (.claude/hooks/company/employee_activator.py)
           Status: SUCCESS

 08:22:12  daemon
           Event: Task completed (task-20260222-abc123)
           Duration: 4m 32s

 08:20:00  daemon
           Event: Task claimed by senior-python-developer
           Task: WQ-task-20260222-abc123

═══════════════════════════════════════════════════════════════
 Showing 20 of 145 events (last hour)
 Use --since=4h for more history
═══════════════════════════════════════════════════════════════
```

**--daemon Mode:**

Show detailed daemon metrics:
```
═══════════════════════════════════════════════════════════════
 DAEMON METRICS
═══════════════════════════════════════════════════════════════

 RUNTIME
 ──────────────────────────────────────────────────────────────
 Status:              RUNNING
 PID:                 5675
 Uptime:              21h 45m 32s
 Last Heartbeat:      2s ago

 PERFORMANCE
 ──────────────────────────────────────────────────────────────
 Tasks Completed:     126
 Tasks Failed:        0
 Success Rate:        100.0%
 Poll Cycles:         1,245

 CIRCUIT BREAKER
 ──────────────────────────────────────────────────────────────
 State:               CLOSED (healthy)
 Consecutive Fails:   0
 Tasks/Hour:          5.8

 STRATEGIC PLANNING
 ──────────────────────────────────────────────────────────────
 Last Run:            2h 15m ago
 Proposals Created:   3
 Cross-Project Tasks: 0

 ROADMAP SCHEDULING
 ──────────────────────────────────────────────────────────────
 Tasks Scheduled:     23
 Tasks Completed:     23
 Current Wave:        1

═══════════════════════════════════════════════════════════════
```

**Error Handling:**

- If daemon not running, show "DAEMON NOT RUNNING" with last known status
- If no activity logs exist, show "No activity recorded yet"
- If heartbeat stale (>5min), warn "Heartbeat stale - daemon may be hung"

</command>

## Related Commands

| Command | Focus | Use Case |
|---------|-------|----------|
| `/progress` | Quick overview | "What's happening now?" |
| `/daemon status` | Daemon technical details | Troubleshooting daemon |
| `/dashboard` | Full operational snapshot | Comprehensive review |
| `/company-health` | Strategic insights | Management reporting |

## Examples

```bash
# Quick status check
/progress

# Watch live updates
/progress --live

# See recent activity
/progress --activity

# Activity from last 4 hours
/progress --activity --since=4h

# Daemon-focused view
/progress --daemon
```
