# /dashboard-web — Web Dashboard Management

Start, stop, or check status of the Gatesmith web dashboard.

## Usage

```
/dashboard-web              # Show status
/dashboard-web start        # Start dashboard server
/dashboard-web stop         # Stop dashboard server
/dashboard-web open         # Start and open in browser
/dashboard-web --port 3000  # Custom port (default: 8080)
```

## Behavior

When invoked, run the appropriate dashboard_server.py command:

### Start Dashboard
```bash
# Check if already running
if [ -f .company/dashboard.pid ]; then
  PID=$(cat .company/dashboard.pid)
  if ps -p $PID > /dev/null 2>&1; then
    echo "Dashboard already running (pid=$PID)"
    echo "URL: http://localhost:8080"
    exit 0
  fi
fi

# Start server in background
uv run .claude/hooks/company/dashboard_server.py --port ${PORT:-8080} &
PID=$!
echo $PID > .company/dashboard.pid
echo "Dashboard started (pid=$PID)"
echo "URL: http://localhost:${PORT:-8080}"
```

### Stop Dashboard
```bash
if [ -f .company/dashboard.pid ]; then
  PID=$(cat .company/dashboard.pid)
  kill $PID 2>/dev/null && echo "Dashboard stopped" || echo "Dashboard not running"
  rm -f .company/dashboard.pid
else
  echo "Dashboard not running"
fi
```

### Open in Browser
```bash
# Start if not running, then open browser
/dashboard-web start

# Open in default browser (cross-platform)
URL="http://localhost:${PORT:-8080}"
case "$(uname)" in
  Darwin) open "$URL" ;;
  Linux) xdg-open "$URL" 2>/dev/null || echo "Visit: $URL" ;;
  *) echo "Visit: $URL" ;;
esac
```

### Status
```bash
if [ -f .company/dashboard.pid ]; then
  PID=$(cat .company/dashboard.pid)
  if ps -p $PID > /dev/null 2>&1; then
    echo "Dashboard: running (pid=$PID)"
    echo "URL: http://localhost:8080"
    echo "Uptime: $(ps -o etime= -p $PID | tr -d ' ')"
  else
    echo "Dashboard: not running (stale pid file)"
    rm -f .company/dashboard.pid
  fi
else
  echo "Dashboard: not running"
fi
```

## Output Format

```
┌─ Dashboard Status ─────────────────────────────┐
│ Status: Running                                │
│ PID: 12345                                     │
│ URL: http://localhost:8080                     │
│ Uptime: 01:23:45                               │
│ Clients: 2 connected                           │
└────────────────────────────────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `.company/dashboard.pid` | Process ID tracking |
| `.company/logs/dashboard.log` | Server logs |
| `.claude/hooks/company/dashboard_server.py` | Main server |
| `.claude/hooks/company/dashboard/` | Static files |

## Requirements

- Python 3.10+
- UV (for running single-file scripts)
- Optional: `watchdog` for real-time updates (installed via UV deps)

## Notes

- Dashboard is read-only — it cannot modify company state
- Binds to localhost only by default (safe for local dev)
- Real-time updates via Server-Sent Events (SSE)
- Works without watchdog (polling fallback)
