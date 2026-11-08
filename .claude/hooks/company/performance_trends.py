#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Performance Trends — velocity and completion trend analysis over the rolling window.

Reads from the existing metrics.json (7-day rolling window) and computes:
- Velocity trend direction: is throughput improving, declining, or stable?
- Day-over-day change in task completions
- Peak and slowest days in the window
- Department breakdown trends

This fills the gap the current dashboards miss: point-in-time snapshots show
*where you are* but not *which direction you're heading*. Trend data gives the
team early warning of throughput drops before they become crises.

Usage:
    python performance_trends.py [--project ID]
    python performance_trends.py --company-dir /path/to/.company [--project ID]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Trend direction thresholds
_TREND_INCREASING_THRESHOLD = 0.10  # +10% = increasing
_TREND_DECREASING_THRESHOLD = -0.10  # -10% = decreasing


def _load_metrics(company_dir: Path, project_id: str | None = None) -> dict:
    """Load metrics.json from the company directory."""
    if project_id:
        path = company_dir / "metrics" / f"{project_id}.json"
    else:
        path = company_dir / "metrics.json"

    try:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _velocity_by_day(metrics: dict) -> list[dict]:
    """Extract and normalise the daily velocity list.

    Returns a list of ``{"date": "YYYY-MM-DD", "count": int}`` sorted
    ascending by date.  Handles both the structured ``velocity.daily`` key
    and the older flat ``task_completions`` list as a fallback.
    """
    # Preferred path: velocity.daily already populated by metrics_tracker
    daily: list[dict] = metrics.get("velocity", {}).get("daily", [])
    if daily:
        # Normalise and sort
        seen: dict[str, int] = {}
        for entry in daily:
            date_key = str(entry.get("date", ""))
            count = int(entry.get("count", 0))
            if date_key:
                seen[date_key] = seen.get(date_key, 0) + count
        return [{"date": d, "count": c} for d, c in sorted(seen.items())]

    # Fallback: aggregate task_completions by completed_at date
    completions: list[dict] = metrics.get("task_completions", [])
    if not completions:
        return []

    day_counts: dict[str, int] = {}
    for task in completions:
        completed_at = task.get("completed_at", "")
        if not completed_at:
            continue
        try:
            dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            date_key = dt.strftime("%Y-%m-%d")
            day_counts[date_key] = day_counts.get(date_key, 0) + 1
        except (ValueError, AttributeError):
            continue

    return [{"date": d, "count": c} for d, c in sorted(day_counts.items())]


def _split_window(daily: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split daily data into early half and recent half for trend comparison.

    With 7 days: days 0-2 = early, days 4-6 = recent (day 3 = midpoint, excluded).
    With fewer days: halves may be smaller.
    """
    n = len(daily)
    if n < 2:
        return [], daily

    half = n // 2
    early = daily[:half]
    recent = daily[n - half :]
    return early, recent


def _avg_count(days: list[dict]) -> float:
    if not days:
        return 0.0
    return sum(d["count"] for d in days) / len(days)


def _trend_direction(early_avg: float, recent_avg: float) -> str:
    if early_avg == 0 and recent_avg == 0:
        return "stable"
    if early_avg == 0:
        return "increasing"

    change = (recent_avg - early_avg) / early_avg
    if change >= _TREND_INCREASING_THRESHOLD:
        return "increasing"
    if change <= _TREND_DECREASING_THRESHOLD:
        return "decreasing"
    return "stable"


def _department_breakdown(metrics: dict, cutoff_days: int = 7) -> dict[str, dict]:
    """Compute per-department task counts from task_completions.

    Returns mapping of department → {"this_period": int, "prev_period": int, "trend": str}.
    """
    completions: list[dict] = metrics.get("task_completions", [])
    if not completions:
        return {}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cutoff_days)
    prev_cutoff = cutoff - timedelta(days=cutoff_days)

    this_period: dict[str, int] = {}
    prev_period: dict[str, int] = {}

    for task in completions:
        dept = task.get("department", "unknown") or "unknown"
        completed_at = task.get("completed_at", "")
        if not completed_at:
            continue
        try:
            dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue

        if dt >= cutoff:
            this_period[dept] = this_period.get(dept, 0) + 1
        elif dt >= prev_cutoff:
            prev_period[dept] = prev_period.get(dept, 0) + 1

    # Merge all known departments
    all_depts = set(this_period) | set(prev_period)
    breakdown: dict[str, dict] = {}
    for dept in sorted(all_depts):
        this_count = this_period.get(dept, 0)
        prev_count = prev_period.get(dept, 0)

        if prev_count == 0 and this_count == 0:
            direction = "stable"
        elif prev_count == 0:
            direction = "increasing"
        else:
            change = (this_count - prev_count) / prev_count
            if change >= _TREND_INCREASING_THRESHOLD:
                direction = "increasing"
            elif change <= _TREND_DECREASING_THRESHOLD:
                direction = "decreasing"
            else:
                direction = "stable"

        breakdown[dept] = {
            "this_period": this_count,
            "prev_period": prev_count,
            "trend": direction,
        }

    return breakdown


def compute_velocity_trend(
    company_dir: Path, project_id: str | None = None
) -> dict[str, Any]:
    """Compute velocity trend from the rolling-window metrics.

    Args:
        company_dir: Path to .company directory.
        project_id: Optional project ID for multi-project mode.

    Returns:
        Dict with:
        - direction: "increasing" | "decreasing" | "stable"
        - change_percent: float, relative change early→recent (0.0 if no data)
        - current_avg: float, tasks/day in the recent half
        - previous_avg: float, tasks/day in the early half
        - daily_data: list of {date, count, is_peak, is_slowest}
        - peak_day: {date, count} or None
        - slowest_day: {date, count} or None (excludes zero-count days)
        - window_days: int, number of days with any data
        - total_completed: int, sum of completions in window
        - department_breakdown: per-department trend
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)
    metrics = _load_metrics(company_dir, project_id)
    daily = _velocity_by_day(metrics)

    if not daily:
        return {
            "success": True,
            "direction": "stable",
            "change_percent": 0.0,
            "current_avg": 0.0,
            "previous_avg": 0.0,
            "daily_data": [],
            "peak_day": None,
            "slowest_day": None,
            "window_days": 0,
            "total_completed": 0,
            "department_breakdown": {},
            "message": "No velocity data in rolling window",
            "generated_at": now.isoformat(),
        }

    early, recent = _split_window(daily)
    early_avg = _avg_count(early)
    recent_avg = _avg_count(recent)

    direction = _trend_direction(early_avg, recent_avg)

    if early_avg > 0:
        change_pct = round((recent_avg - early_avg) / early_avg * 100, 1)
    elif recent_avg > 0:
        change_pct = 100.0
    else:
        change_pct = 0.0

    total_completed = sum(d["count"] for d in daily)

    # Identify peak and slowest (non-zero) days
    peak_day = max(daily, key=lambda d: d["count"]) if daily else None
    non_zero = [d for d in daily if d["count"] > 0]
    slowest_day = min(non_zero, key=lambda d: d["count"]) if non_zero else None

    # Annotate daily data
    peak_date = peak_day["date"] if peak_day else None
    slowest_date = slowest_day["date"] if slowest_day else None
    annotated = [
        {
            "date": d["date"],
            "count": d["count"],
            "is_peak": d["date"] == peak_date,
            "is_slowest": d["date"] == slowest_date and d["date"] != peak_date,
        }
        for d in daily
    ]

    dept_breakdown = _department_breakdown(metrics)

    return {
        "success": True,
        "direction": direction,
        "change_percent": change_pct,
        "current_avg": round(recent_avg, 2),
        "previous_avg": round(early_avg, 2),
        "daily_data": annotated,
        "peak_day": peak_day,
        "slowest_day": slowest_day,
        "window_days": len(daily),
        "total_completed": total_completed,
        "department_breakdown": dept_breakdown,
        "generated_at": now.isoformat(),
    }


def compute_completion_trend(
    company_dir: Path, project_id: str | None = None
) -> dict[str, Any]:
    """Compute day-over-day completion rate trend.

    Focuses on the most recent two days so the team can spot an
    immediate drop (e.g., after a deploy or config change).

    Returns:
        Dict with today/yesterday counts, day-over-day change, and direction.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    metrics = _load_metrics(company_dir, project_id)
    daily = _velocity_by_day(metrics)

    day_map = {d["date"]: d["count"] for d in daily}
    today_count = day_map.get(today_str, 0)
    yesterday_count = day_map.get(yesterday_str, 0)

    if yesterday_count == 0 and today_count == 0:
        dod_change = 0.0
        dod_direction = "stable"
    elif yesterday_count == 0:
        dod_change = 100.0
        dod_direction = "increasing"
    else:
        dod_change = round((today_count - yesterday_count) / yesterday_count * 100, 1)
        if dod_change >= 10:
            dod_direction = "increasing"
        elif dod_change <= -10:
            dod_direction = "decreasing"
        else:
            dod_direction = "stable"

    weekly_total = sum(d["count"] for d in daily)

    return {
        "success": True,
        "today": today_str,
        "today_count": today_count,
        "yesterday_count": yesterday_count,
        "dod_change_percent": dod_change,
        "dod_direction": dod_direction,
        "weekly_total": weekly_total,
        "generated_at": now.isoformat(),
    }


def get_performance_trends(
    company_dir: Path | None = None, project_id: str | None = None
) -> dict[str, Any]:
    """Get full performance trend report.

    Combines velocity trend and day-over-day completion into a single
    response suitable for dashboard integration.

    Args:
        company_dir: Path to .company directory.  Defaults to cwd/.company.
        project_id: Optional project ID for multi-project mode.

    Returns:
        Dict with:
        - velocity: Velocity trend (direction, change, daily breakdown)
        - completion: Day-over-day completion trend
        - summary: Human-readable one-liner for dashboard headline
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    if company_dir is None:
        company_dir = Path(".company")

    velocity = compute_velocity_trend(company_dir, project_id)
    completion = compute_completion_trend(company_dir, project_id)

    # Build a concise summary line
    direction = velocity.get("direction", "stable")
    change_pct = velocity.get("change_percent", 0.0)
    total = velocity.get("total_completed", 0)
    window = velocity.get("window_days", 0)

    if direction == "increasing":
        summary = f"Velocity up {change_pct:+.0f}% — {total} tasks over {window} days, trending positive"
    elif direction == "decreasing":
        summary = f"Velocity down {change_pct:.0f}% — {total} tasks over {window} days, review blockers"
    else:
        avg = velocity.get("current_avg", 0.0)
        summary = (
            f"Velocity stable at {avg:.1f} tasks/day — {total} tasks over {window} days"
        )

    return {
        "success": True,
        "velocity": velocity,
        "completion": completion,
        "summary": summary,
        "generated_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Performance Trends — velocity and completion trend analysis"
    )
    parser.add_argument(
        "--company-dir", default=".company", help="Path to .company dir"
    )
    parser.add_argument(
        "--project", default=None, help="Project ID (multi-project mode)"
    )
    args = parser.parse_args(argv)

    company_dir = Path(args.company_dir)
    result = get_performance_trends(company_dir, args.project)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _main(sys.argv[1:])
