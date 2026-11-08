# /daemon — Manage Gatesmith Daemon

Control the Gatesmith autonomous operation daemon from within Claude Code.

The daemon enables continuous background execution of tasks from the work queue, with circuit breaker protection, health monitoring, and graceful lifecycle management.

## Input

$ARGUMENTS

Supported commands:
- `start` — Start the daemon (daemonized in background)
- `start --foreground` — Start in foreground mode (for debugging/containers)
- `stop` — Gracefully stop the daemon
- `restart` — Stop and start the daemon
- `status` — Show daemon status and health metrics

## Step 1: Parse Command

Parse `$ARGUMENTS` to extract the daemon action:

```bash
# Extract command and flags
action=$(echo "$ARGUMENTS" | awk '{print $1}')
flags=$(echo "$ARGUMENTS" | grep -oE '\-\-[a-z]+' || true)
```

Store:
- `action` — One of: `start`, `stop`, `restart`, `status`
- `foreground_mode` — Boolean (true if `--foreground` flag present)

## Step 2: Validate Company Root

Ensure we're in or can find a company root:

```bash
# Find company root (searches upward for .company directory)
company_root=$(uv run .claude/hooks/company/company_resolver.py find 2>/dev/null || echo ".")
company_dir="${company_root}/.company"
```

If neither exists, show helpful error:

```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                         [ERROR]
═══════════════════════════════════════════════════════════════

No company structure found.

To initialize a company:
  /company-init

Or ensure you're in a Gatesmith project with .company/ directory.
═══════════════════════════════════════════════════════════════
```

Exit without further processing.

## Step 3: Execute Daemon Command

Run the forge_daemon.py utility with the appropriate action:

```bash
# Build command with optional foreground flag
if [ "$foreground_mode" = "true" ]; then
  cmd="uv run .claude/hooks/company/forge_daemon.py $action --foreground"
else
  cmd="uv run .claude/hooks/company/forge_daemon.py $action"
fi

# Execute and capture output
exit_code=$?
```

Capture both stdout and stderr:
- stdout: JSON status data
- stderr: Human-readable messages

Exit codes:
- `0` = Success
- `1` = Error
- `2` = State mismatch (daemon already running, not running, etc.)

## Step 4: Parse Status and Display

### For `start` command:

Success output:
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                       [STARTING]
═══════════════════════════════════════════════════════════════

Daemon starting...
```

If already running (exit code 2):
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                      [ALREADY RUNNING]
═══════════════════════════════════════════════════════════════

Daemon is already running (pid=12345)

Current Status:
  Uptime: 2h 34m
  Tasks Completed: 47
  Tasks Failed: 2
  Current Task: WQ-123
  Circuit Breaker: CLOSED

Options:
  • Continue running — no action needed
  • Restart — /daemon restart
  • Check status — /daemon status
  • Stop — /daemon stop
═══════════════════════════════════════════════════════════════
```

On error (exit code 1):
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                         [ERROR]
═══════════════════════════════════════════════════════════════

Failed to start daemon: [error message from stderr]

Common issues:
  • Insufficient permissions to create .company/daemon.pid
  • Another process is using the same PID file
  • Missing dependencies (check logs)

Debug:
  • Check logs: tail -f .company/logs/daemon.log
  • Try foreground: /daemon start --foreground
═══════════════════════════════════════════════════════════════
```

### For `stop` command:

Success output:
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                       [STOPPING]
═══════════════════════════════════════════════════════════════

Gracefully stopping daemon...
Final metrics before shutdown:
  Tasks Completed: 47
  Tasks Failed: 2
  Uptime: 2h 34m

Status: STOPPED ✓
═══════════════════════════════════════════════════════════════
```

If not running (exit code 2):
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                    [NOT RUNNING]
═══════════════════════════════════════════════════════════════

Daemon is not running.

To start:
  /daemon start
═══════════════════════════════════════════════════════════════
```

### For `restart` command:

Combined output:
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                     [RESTARTING]
═══════════════════════════════════════════════════════════════

Stopping existing daemon...
Status: STOPPED ✓

Starting new daemon...
Status: STARTED ✓

New PID: 54321
═══════════════════════════════════════════════════════════════
```

### For `status` command:

Read the status JSON and format as:

```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                       [RUNNING]
═══════════════════════════════════════════════════════════════

 PID:                  12345
 Uptime:               2h 34m 15s

 Tasks Completed:      47
 Tasks Failed:         2
 Current Task:         WQ-123 (in progress)

 Circuit Breaker:      CLOSED (healthy)
 Last Heartbeat:       5s ago

 ──────────────────────────────────────────────────────────────
 ROADMAP SCHEDULING (P27)
 ──────────────────────────────────────────────────────────────
 Enabled:              Yes
 Last Scan:            5m ago
 Tasks Scheduled:      12
 Tasks Completed:      8
 Current Wave:         2

 ──────────────────────────────────────────────────────────────
 PR WORKFLOW (P27)
 ──────────────────────────────────────────────────────────────
 Flow Mode:            Enabled
 Auto-Push Branches:   Yes
 Auto-Create PR:       Yes (draft)
 Require Tests:        Yes
 On Test Failure:      create_fix_task
═══════════════════════════════════════════════════════════════
```

If daemon not running:
```
═══════════════════════════════════════════════════════════════
 GATESMITH DAEMON                                    [NOT RUNNING]
═══════════════════════════════════════════════════════════════

No daemon process active.

To start the daemon:
  /daemon start

To start in foreground (debugging):
  /daemon start --foreground

To check if task queue exists:
  ls -la .company/state/work_queue.json
═══════════════════════════════════════════════════════════════
```

### Format Uptime Nicely

Convert seconds to human-readable format:

```
0-60s = "Xs"
60-3600s = "Xm Ys"
3600+ = "Xh Ym Zs"
```

Examples:
- 30 = "30s"
- 125 = "2m 5s"
- 9215 = "2h 33m 35s"

### Parse Heartbeat Time

Convert ISO timestamp to relative time:

```
now - heartbeat < 60s = "Xs ago"
now - heartbeat < 3600s = "Xm ago"
now - heartbeat < 86400s = "Xh ago"
else = timestamp itself
```

### Display Circuit Breaker State

Show the state from heartbeat JSON:

```
"closed" or "CLOSED" = "CLOSED (healthy)"
"open" or "OPEN" = "OPEN (recovering)" [yellow]
"half_open" or "HALF_OPEN" = "HALF_OPEN (checking)" [yellow]
"unknown" = "UNKNOWN"
```

## Step 5: Display Next Steps

For status command, show helpful next actions:

```
### Next Steps

• View daemon logs:
  tail -f .company/logs/daemon.log

• Monitor daemon:
  watch -n 5 'uv run .claude/hooks/company/forge_daemon.py status'

• Manually execute work queue:
  /run-loop

• Stop daemon:
  /daemon stop
```

## Rules

- **Use `uv run` to execute** — Always invoke forge_daemon.py via `uv run .claude/hooks/company/forge_daemon.py`
- **Show clear success/failure messages** — Use the formatted box output consistently
- **Warn on state mismatches** — If trying to start when already running or stop when not running, show the current state
- **Handle permissions gracefully** — If PID file can't be written, suggest permission checks
- **Parse JSON status output** — The daemon returns JSON on stdout for scripting; format it for human display
- **Graceful shutdown** — The daemon respects SIGTERM and will shut down within 30 seconds
- **Show heartbeat freshness** — If heartbeat is stale (> 2 minutes old), warn that daemon may be hung
- **Foreground mode for debugging** — When `--foreground` is used, output goes to console in real-time
- **Respect configuration** — Load daemon config from gatesmith.json or defaults in DaemonConfig
- **Never force kill** — Let the daemon shut down gracefully; only after 30s timeout use SIGKILL
