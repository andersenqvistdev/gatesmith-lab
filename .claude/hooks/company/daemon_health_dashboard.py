# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Daemon health dashboard — operational metrics and trend analysis.

Computes MTBF, MTTR, success rates, recovery effectiveness, and an
overall autonomy score from existing daemon telemetry files.

Public API:
    compute_mtbf(metrics_data) -> float
    compute_mttr(metrics_data) -> float
    compute_success_rate(heartbeat_data, window_hours=24) -> float
    compute_recovery_effectiveness(recovery_patterns) -> dict
    compute_autonomy_score(metrics) -> float
    generate_health_report(company_dir) -> dict
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Individual metric computations
# ---------------------------------------------------------------------------


def compute_mtbf(metrics_data: dict) -> float:
    """Mean Time Between Failures — average uptime between crashes/restarts.

    Computed from uptime_windows in daemon_metrics.json. Returns seconds.
    Returns 0.0 if insufficient data (need at least 2 windows to measure
    the gap between failures).
    """
    windows = metrics_data.get("uptime_windows", [])
    if len(windows) < 2:
        # With 0 or 1 windows, we can return total uptime or 0
        if len(windows) == 1:
            return float(windows[0].get("duration_seconds", 0))
        return 0.0

    total_uptime = sum(w.get("duration_seconds", 0) for w in windows)
    # Number of failures = number of windows - 1 (each window end is a failure
    # except potentially the last one which may still be running)
    failure_count = len(windows) - 1
    if failure_count == 0:
        return float(total_uptime)

    return total_uptime / failure_count


def compute_mttr(metrics_data: dict) -> float:
    """Mean Time To Recovery — average time between failure and next start.

    Computed by looking at gaps between consecutive uptime windows.
    Returns seconds. Returns 0.0 if insufficient data.
    """
    windows = metrics_data.get("uptime_windows", [])
    if len(windows) < 2:
        return 0.0

    recovery_times: list[float] = []
    for i in range(1, len(windows)):
        prev_end = windows[i - 1].get("ended_at")
        curr_start = windows[i].get("started_at")
        if not prev_end or not curr_start:
            continue
        try:
            end_ts = datetime.fromisoformat(prev_end.replace("Z", "+00:00"))
            start_ts = datetime.fromisoformat(curr_start.replace("Z", "+00:00"))
            gap = (start_ts - end_ts).total_seconds()
            if gap >= 0:
                recovery_times.append(gap)
        except (ValueError, TypeError):
            continue

    if not recovery_times:
        return 0.0

    return sum(recovery_times) / len(recovery_times)


def compute_success_rate(heartbeat_data: dict, window_hours: int = 24) -> float:
    """Task success rate from heartbeat or loop_monitor data.

    Returns a float between 0.0 and 1.0. Returns 1.0 if no tasks recorded.
    """
    # Try heartbeat format first
    completed = heartbeat_data.get("tasks_completed_this_session", 0)
    failed = heartbeat_data.get("tasks_failed_this_session", 0)

    # Also try loop_monitor health_metrics format
    if completed == 0 and failed == 0:
        hm = heartbeat_data.get("health_metrics", {})
        completed = hm.get("total_successes", 0)
        failed = hm.get("total_failures", 0)

    total = completed + failed
    if total == 0:
        return 1.0

    return completed / total


def compute_recovery_effectiveness(recovery_patterns: dict) -> dict:
    """Per-strategy success rate from recovery_patterns.json.

    Returns a dict mapping strategy names to their success rates,
    plus an 'overall' key with the aggregate rate.
    """
    patterns = recovery_patterns.get("patterns", {})
    if not patterns:
        return {"overall": 0.0}

    result: dict[str, float] = {}
    total_attempts = 0
    total_successes = 0

    for key, entry in patterns.items():
        attempts = entry.get("attempts", 0)
        successes = entry.get("successes", 0)
        rate = entry.get("success_rate", 0.0)

        # Extract strategy name (format: "failure_type:strategy")
        parts = key.split(":")
        strategy = parts[1] if len(parts) > 1 else key

        # Aggregate by strategy
        if strategy not in result:
            result[strategy] = 0.0

        total_attempts += attempts
        total_successes += successes

        if attempts > 0:
            # Weighted update: keep the per-key rate in the result
            result[key] = rate

    overall = total_successes / total_attempts if total_attempts > 0 else 0.0
    result["overall"] = overall
    result["total_attempts"] = float(total_attempts)
    result["total_successes"] = float(total_successes)

    return result


def compute_autonomy_score(metrics: dict) -> float:
    """Percentage of tasks resolved without human intervention.

    Considers:
    - Auto-merged PRs (from heartbeat)
    - Auto-recovered tasks (from recovery patterns)
    - Tasks completed without escalation

    Returns a float between 0.0 and 100.0.
    """
    heartbeat = metrics.get("heartbeat", {})
    loop_monitor = metrics.get("loop_monitor", {})
    recovery = metrics.get("recovery_patterns", {})

    # Total tasks from loop_monitor or heartbeat
    hm = loop_monitor.get("health_metrics", {})
    total_successes = hm.get("total_successes", 0)
    total_failures = hm.get("total_failures", 0)
    total_tasks = total_successes + total_failures

    if total_tasks == 0:
        # Fallback to heartbeat
        completed = heartbeat.get("tasks_completed_this_session", 0)
        failed = heartbeat.get("tasks_failed_this_session", 0)
        total_tasks = completed + failed

    if total_tasks == 0:
        return 0.0

    # Escalations = tasks that required human intervention
    # Count escalation patterns (strategy == "escalate")
    patterns = recovery.get("patterns", {})
    escalation_count = 0
    for key, entry in patterns.items():
        if key.endswith(":escalate"):
            escalation_count += entry.get("attempts", 0)

    # Autonomous = total - escalations
    autonomous = max(0, total_tasks - escalation_count)
    score = (autonomous / total_tasks) * 100.0

    return min(100.0, score)


# ---------------------------------------------------------------------------
# Subsystem health grading
# ---------------------------------------------------------------------------

_SUBSYSTEM_FIELDS = {
    "proactive_scan": "last_proactive_scan",
    "strategic_planning": "last_strategic_planning",
    "weekly_planning": "last_weekly_planning",
    "daily_planning": "last_daily_planning",
    "roadmap_scan": "last_roadmap_scan",
    "executive_loop": "last_executive_loop",
    "improvement_cycle": "last_improvement_cycle",
    "employee_ideation": "last_employee_ideation",
    "auto_merge": "last_auto_merge_check",
    "rebalance": "last_rebalance_check",
}


def _grade_subsystem_freshness(last_ts_str: str, max_age_minutes: int = 30) -> str:
    """Grade a subsystem based on how recently it ran."""
    if not last_ts_str:
        return "INACTIVE"
    try:
        ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age_min < max_age_minutes:
            return "A"
        elif age_min < max_age_minutes * 2:
            return "B"
        elif age_min < max_age_minutes * 4:
            return "C"
        else:
            return "F"
    except (ValueError, TypeError):
        return "UNKNOWN"


def _compute_subsystem_grades(heartbeat: dict) -> dict[str, str]:
    """Grade each daemon subsystem based on heartbeat timestamps."""
    grades = {}
    for name, field in _SUBSYSTEM_FIELDS.items():
        ts = heartbeat.get(field, "")
        grades[name] = _grade_subsystem_freshness(ts)
    return grades


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


def _safe_load_json(path: Path) -> dict:
    """Load JSON file, returning empty dict on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def generate_health_report(company_dir: str | Path) -> dict:
    """Generate a comprehensive health report from all daemon telemetry.

    Args:
        company_dir: Path to the .company directory.

    Returns:
        Dict with sections: stability, performance, recovery, autonomy, subsystems.
    """
    company = Path(company_dir)

    # Load telemetry files
    metrics_data = _safe_load_json(company / "state" / "daemon_metrics.json")
    if not metrics_data:
        metrics_data = _safe_load_json(company / "daemon_metrics.json")

    heartbeat = _safe_load_json(company / "runtime" / "daemon.heartbeat")
    if not heartbeat:
        heartbeat = _safe_load_json(company / "daemon.heartbeat")

    loop_monitor = _safe_load_json(company / "state" / "loop_monitor.json")
    if not loop_monitor:
        loop_monitor = _safe_load_json(company / "loop_monitor.json")

    recovery_patterns = _safe_load_json(company / "state" / "recovery_patterns.json")

    # Compute metrics
    mtbf = compute_mtbf(metrics_data)
    mttr = compute_mttr(metrics_data)
    success_rate = compute_success_rate(heartbeat if heartbeat else loop_monitor)
    recovery_eff = compute_recovery_effectiveness(recovery_patterns)
    autonomy = compute_autonomy_score(
        {
            "heartbeat": heartbeat,
            "loop_monitor": loop_monitor,
            "recovery_patterns": recovery_patterns,
        }
    )
    subsystem_grades = _compute_subsystem_grades(heartbeat)

    # Circuit breaker state
    cb = loop_monitor.get("circuit_breaker", {})
    cb_state = cb.get("state", "unknown")
    cb_trips = cb.get("total_trips", 0)

    # Uptime
    uptime_seconds = heartbeat.get("uptime_seconds", 0)

    # Summary
    summary = metrics_data.get("summary", {})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stability": {
            "mtbf_seconds": round(mtbf, 1),
            "mtbf_human": _format_duration(mtbf),
            "mttr_seconds": round(mttr, 1),
            "mttr_human": _format_duration(mttr),
            "total_starts": summary.get("total_starts", 0),
            "total_restarts": summary.get("total_restarts", 0),
            "total_crashes": summary.get("total_crashes", 0),
            "current_uptime_seconds": uptime_seconds,
            "current_uptime_human": _format_duration(uptime_seconds),
        },
        "performance": {
            "success_rate": round(success_rate, 4),
            "success_rate_pct": f"{success_rate * 100:.1f}%",
            "tasks_completed": heartbeat.get("tasks_completed_this_session", 0),
            "tasks_failed": heartbeat.get("tasks_failed_this_session", 0),
            "throughput_per_hour": heartbeat.get("throughput_per_hour", 0),
            "active_workers": heartbeat.get("active_workers", 0),
        },
        "recovery": recovery_eff,
        "autonomy": {
            "score": round(autonomy, 1),
            "score_human": f"{autonomy:.1f}%",
            "target": 85.0,
            "on_target": autonomy >= 85.0,
        },
        "circuit_breaker": {
            "state": cb_state,
            "total_trips": cb_trips,
        },
        "subsystems": subsystem_grades,
    }


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if s:
            return f"{h}h {m}m {s}s"
        elif m:
            return f"{h}h {m}m"
        else:
            return f"{h}h"
