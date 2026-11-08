#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Daemon Metrics Tracker — record uptime windows and restart events for trend analysis.

Writes to .company/daemon_metrics.json using atomic writes to prevent corruption.

Schema:
    {
      "version": "1.0",
      "uptime_windows": [
        {
          "session_id": "<pid>-<started_at>",
          "pid": 12345,
          "started_at": "2026-03-10T16:00:00+00:00",
          "ended_at": "2026-03-10T17:00:00+00:00",  // null while running
          "duration_seconds": 3600,                  // null while running
          "end_reason": "shutdown" | "crash" | "restart" | null
        }
      ],
      "restart_events": [
        {
          "timestamp": "2026-03-10T17:00:01+00:00",
          "restart_count": 1,
          "reason": "crash" | "exception",
          "exit_code": 1,
          "delay_seconds": 60
        }
      ],
      "summary": {
        "total_starts": 5,
        "total_restarts": 2,
        "total_crashes": 1,
        "total_uptime_seconds": 18000,
        "last_started_at": "...",
        "last_restart_at": "..."
      }
    }

Usage:
    from daemon_metrics import DaemonMetricsTracker

    tracker = DaemonMetricsTracker()
    session_id = tracker.record_start(pid=os.getpid())
    # ... daemon runs ...
    tracker.record_restart(session_id, exit_code=1, delay_seconds=60)
    # or
    tracker.record_stop(session_id, reason="shutdown")
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRICS_FILE = ".company/state/daemon_metrics.json"
MAX_UPTIME_WINDOWS = 100  # Rolling window — keep last N sessions
MAX_RESTART_EVENTS = 200  # Rolling window — keep last N restarts


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_metrics(path: Path) -> dict[str, Any]:
    """Load metrics JSON, returning a fresh structure on missing/corrupt file."""
    if not path.exists():
        return _empty_metrics()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Validate top-level keys
        if not isinstance(data, dict) or "uptime_windows" not in data:
            return _empty_metrics()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_metrics()


def _empty_metrics() -> dict[str, Any]:
    return {
        "version": "1.0",
        "uptime_windows": [],
        "restart_events": [],
        "summary": {
            "total_starts": 0,
            "total_restarts": 0,
            "total_crashes": 0,
            "total_uptime_seconds": 0,
            "last_started_at": None,
            "last_restart_at": None,
        },
    }


def _save_metrics(data: dict[str, Any], path: Path) -> None:
    """Atomically write metrics to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".daemon_metrics.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _recompute_summary(data: dict[str, Any]) -> None:
    """Recompute summary totals from uptime_windows and restart_events."""
    windows = data.get("uptime_windows", [])
    restarts = data.get("restart_events", [])

    total_uptime = 0
    last_started: str | None = None

    for w in windows:
        duration = w.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            total_uptime += duration
        started = w.get("started_at")
        if started and (last_started is None or started > last_started):
            last_started = started

    total_crashes = sum(
        1 for r in restarts if r.get("reason") in ("crash", "exception")
    )

    last_restart_at: str | None = None
    if restarts:
        last_restart_at = restarts[-1].get("timestamp")

    data["summary"] = {
        "total_starts": len(windows),
        "total_restarts": len(restarts),
        "total_crashes": total_crashes,
        "total_uptime_seconds": int(total_uptime),
        "last_started_at": last_started,
        "last_restart_at": last_restart_at,
    }


class DaemonMetricsTracker:
    """Records daemon uptime windows and restart events to daemon_metrics.json."""

    def __init__(self, metrics_file: str | Path = METRICS_FILE) -> None:
        path = Path(metrics_file)
        # A cwd-relative path under a hermetic test run (tests/conftest.py sets
        # FORGE_HERMETIC_COMPANY_DIR) must never reach the real .company/state.
        hermetic = os.environ.get("FORGE_HERMETIC_COMPANY_DIR")
        if hermetic and not path.is_absolute():
            path = Path(hermetic) / "state" / path.name
        self.path = path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_start(self, pid: int) -> str:
        """Record a new uptime window start.

        Args:
            pid: Daemon process ID.

        Returns:
            session_id string that must be passed to record_stop/record_restart.
        """
        started_at = _now_iso()
        session_id = f"{pid}-{started_at}"

        data = _load_metrics(self.path)

        window: dict[str, Any] = {
            "session_id": session_id,
            "pid": pid,
            "started_at": started_at,
            "ended_at": None,
            "duration_seconds": None,
            "end_reason": None,
        }
        data["uptime_windows"].append(window)

        # Trim rolling window
        if len(data["uptime_windows"]) > MAX_UPTIME_WINDOWS:
            data["uptime_windows"] = data["uptime_windows"][-MAX_UPTIME_WINDOWS:]

        _recompute_summary(data)
        _save_metrics(data, self.path)
        return session_id

    def record_stop(self, session_id: str, reason: str = "shutdown") -> None:
        """Record that an uptime window has ended normally.

        Args:
            session_id: ID returned by record_start.
            reason: Why the daemon stopped ("shutdown", "crash", "restart").
        """
        data = _load_metrics(self.path)
        ended_at = _now_iso()

        for window in data["uptime_windows"]:
            if window.get("session_id") == session_id:
                window["ended_at"] = ended_at
                window["end_reason"] = reason
                # Calculate duration
                try:
                    start = datetime.fromisoformat(
                        window["started_at"].replace("Z", "+00:00")
                    )
                    end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    window["duration_seconds"] = int((end - start).total_seconds())
                except (ValueError, TypeError):
                    window["duration_seconds"] = None
                break

        _recompute_summary(data)
        _save_metrics(data, self.path)

    def record_restart(
        self,
        session_id: str,
        exit_code: int,
        delay_seconds: int,
        reason: str = "crash",
    ) -> None:
        """Record a restart event and close the current uptime window.

        Args:
            session_id: ID returned by record_start.
            exit_code: Exit code of the crashed loop.
            delay_seconds: Delay before restart.
            reason: "crash" or "exception".
        """
        # Close current uptime window as restarted
        self.record_stop(session_id, reason="restart")

        data = _load_metrics(self.path)

        restart_count = len(data.get("restart_events", [])) + 1
        event: dict[str, Any] = {
            "timestamp": _now_iso(),
            "restart_count": restart_count,
            "reason": reason,
            "exit_code": exit_code,
            "delay_seconds": delay_seconds,
        }
        data["restart_events"].append(event)

        # Trim rolling window
        if len(data["restart_events"]) > MAX_RESTART_EVENTS:
            data["restart_events"] = data["restart_events"][-MAX_RESTART_EVENTS:]

        _recompute_summary(data)
        _save_metrics(data, self.path)

    def record_heartbeat(
        self,
        session_id: str,
        uptime_seconds: int,
        tasks_completed: int = 0,
        tasks_failed: int = 0,
    ) -> None:
        """Checkpoint live uptime into the open session window.

        Called every ~30s by the heartbeat thread so uptime is preserved
        even if the daemon is killed with SIGKILL (no clean shutdown).

        Args:
            session_id: ID returned by record_start.
            uptime_seconds: Seconds since this session started.
            tasks_completed: Tasks completed this session.
            tasks_failed: Tasks failed this session.
        """
        data = _load_metrics(self.path)
        for window in data["uptime_windows"]:
            if (
                window.get("session_id") == session_id
                and window.get("ended_at") is None
            ):
                window["last_heartbeat_at"] = _now_iso()
                window["last_uptime_seconds"] = uptime_seconds
                window["tasks_completed"] = tasks_completed
                window["tasks_failed"] = tasks_failed
                break
        _save_metrics(data, self.path)

    def get_summary(self) -> dict[str, Any]:
        """Return the summary section from metrics."""
        data = _load_metrics(self.path)
        return data.get("summary", _empty_metrics()["summary"])

    def get_metrics(self) -> dict[str, Any]:
        """Return full metrics data."""
        return _load_metrics(self.path)
