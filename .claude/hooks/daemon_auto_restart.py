# /// script
# requires-python = ">=3.11"
# ///
"""Auto-restart daemon when daemon-related files change during a build.

Usage:
    uv run .claude/hooks/daemon_auto_restart.py [--commits N] [--dry-run]

Checks if any files changed in the last N commits (default: all uncommitted +
last commit) are daemon-critical.  If the daemon is running, stops it so that
launchd KeepAlive restarts it with the new code.

Exit codes:
    0 — success (restarted, not needed, or dry-run)
    1 — error during restart
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Paths relative to project root that, when changed, require a daemon restart.
DAEMON_CRITICAL_PATHS = [
    ".claude/hooks/company/forge_daemon.py",
    ".claude/hooks/company/employee_activator.py",
    ".claude/hooks/company/executive_loop.py",
    ".claude/hooks/company/dashboard_server.py",
    ".claude/hooks/company/dashboard_aggregator.py",
    ".claude/hooks/company/daily_report.py",
    ".claude/hooks/company/social_content_generator.py",
    ".claude/hooks/company/operation_loop.py",
    ".claude/hooks/company/strategic_planner.py",
    ".claude/hooks/company/goal_autofill.py",
    ".claude/hooks/company/escalation.py",
    ".claude/hooks/company/feedback_monitor.py",
    ".claude/hooks/company/adaptive_scheduler.py",
    ".claude/hooks/company/budget_governor.py",
    ".claude/hooks/company/goal_scheduler.py",
    ".claude/hooks/company/learning_loop.py",
    ".claude/hooks/company/session_continuity.py",
    ".claude/hooks/company/approval_learner.py",
    ".claude/hooks/company/cron_scheduler.py",
    ".claude/hooks/company/subsystem_registry.py",
    ".claude/hooks/company/work_allocator.py",
    ".claude/hooks/company/orchestrator.py",
    ".claude/hooks/company/loop_monitor.py",
    ".claude/hooks/company/self_improvement_loop.py",
    ".claude/hooks/company/launchd/forge-daemon.sh",
]

# Also match any new files added under these directories
DAEMON_CRITICAL_DIRS = [
    ".claude/hooks/company/",
]

# Multi-instance safe: PID file is per-project (relative to project root),
# so auto-restart only affects the daemon for the current project directory.
PID_FILE = Path(".company/daemon.pid")


def get_changed_files(commits: int | None = None) -> list[str]:
    """Get files changed in recent commits."""
    files: set[str] = set()

    if commits is not None:
        # Files changed in last N commits
        result = subprocess.run(
            ["git", "diff", "--name-only", f"HEAD~{commits}..HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            files.update(result.stdout.strip().splitlines())
    else:
        # Staged + unstaged + last commit
        for cmd in [
            ["git", "diff", "--name-only"],
            ["git", "diff", "--name-only", "--cached"],
            ["git", "diff", "--name-only", "HEAD~1..HEAD"],
        ]:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                files.update(result.stdout.strip().splitlines())

    return [f for f in files if f]


def is_daemon_critical(changed_files: list[str]) -> list[str]:
    """Return which changed files are daemon-critical."""
    critical = []
    for f in changed_files:
        if f in DAEMON_CRITICAL_PATHS:
            critical.append(f)
            continue
        for d in DAEMON_CRITICAL_DIRS:
            if f.startswith(d) and f.endswith(".py"):
                critical.append(f)
                break
    # Deduplicate while preserving order
    seen: set[str] = set()
    result = []
    for f in critical:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


def read_daemon_pid() -> int | None:
    """Read daemon PID from pid file."""
    if not PID_FILE.exists():
        return None
    try:
        data = json.loads(PID_FILE.read_text())
        if isinstance(data, dict):
            return data.get("pid")
        if isinstance(data, int):
            return data
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    # Try plain text PID
    try:
        return int(PID_FILE.read_text().strip())
    except ValueError:
        return None


def is_process_alive(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_daemon(pid: int) -> bool:
    """Stop daemon via SIGTERM. launchd KeepAlive will restart it."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        print(f"Permission denied to stop daemon (PID {pid})", file=sys.stderr)
        return False

    # Wait up to 15 seconds for graceful shutdown
    for _ in range(15):
        time.sleep(1)
        if not is_process_alive(pid):
            return True

    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
    except ProcessLookupError:
        pass
    return True


def main() -> int:
    commits: int | None = None
    dry_run = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--commits" and i + 1 < len(args):
            commits = int(args[i + 1])
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    changed = get_changed_files(commits)
    critical = is_daemon_critical(changed)

    result = {
        "changed_files": len(changed),
        "daemon_critical_files": critical,
        "daemon_running": False,
        "action": "none",
    }

    if not critical:
        result["action"] = "skip"
        result["reason"] = "no daemon-critical files changed"
        print(json.dumps(result, indent=2))
        return 0

    pid = read_daemon_pid()
    if pid is None or not is_process_alive(pid):
        result["action"] = "skip"
        result["reason"] = "daemon not running"
        print(json.dumps(result, indent=2))
        return 0

    result["daemon_running"] = True
    result["daemon_pid"] = pid

    if dry_run:
        result["action"] = "would_restart"
        result["reason"] = f"{len(critical)} daemon-critical files changed"
        print(json.dumps(result, indent=2))
        return 0

    # Stop daemon — launchd KeepAlive will auto-restart with new code
    print(
        f"Daemon-critical files changed ({len(critical)}). "
        f"Stopping daemon (PID {pid}) for launchd auto-restart...",
        file=sys.stderr,
    )
    if stop_daemon(pid):
        result["action"] = "restarted"
        result["reason"] = (
            f"stopped PID {pid}, launchd KeepAlive will restart with new code"
        )
        print(json.dumps(result, indent=2))
        return 0
    else:
        result["action"] = "error"
        result["reason"] = f"failed to stop daemon PID {pid}"
        print(json.dumps(result, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
