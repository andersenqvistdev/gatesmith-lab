#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Company Health Metrics Tracker — capture and monitor organizational metrics.

Supports both single-project and multi-project modes with per-project isolation
and company-level rollups.

Collects and tracks metrics for health monitoring across the company:
- Tasks: completed_count, completed_today, average_duration_minutes, by_department
- Escalations: total_count, by_tier, by_reason, avg_resolution_minutes
- Agents: active_count, idle_count, blocked_count, utilization_percent
- Queue: pending_count, blocked_count, oldest_pending_minutes
- Velocity: tasks completed per day (per project and company-wide)

Storage:
- Single-project mode: .company/metrics.json (7-day rolling window)
- Multi-project mode:
  - Per-project: .company/metrics/{project_id}.json
  - Company rollup: .company/metrics/_rollup.json

Usage:
    # Record a task completion (optional --project for multi-project mode)
    python metrics_tracker.py record-task --task-id "task-123" --department "engineering" --duration 45
    python metrics_tracker.py record-task --task-id "task-123" --project "forge-cli" --duration 45

    # Record an escalation
    python metrics_tracker.py record-escalation --tier 2 --reason timeout --resolution-minutes 30

    # Get health summary (quick overview)
    python metrics_tracker.py summary
    python metrics_tracker.py summary --project "forge-cli"

    # Get detailed health report
    python metrics_tracker.py report [--days 7]
    python metrics_tracker.py report --project "forge-cli" --days 7

    # Generate company-wide rollup from all projects
    python metrics_tracker.py rollup

    # List all projects with metrics
    python metrics_tracker.py list-projects

    # Show help
    python metrics_tracker.py help
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Configuration
COMPANY_DIR = ".company"
METRICS_DIR = "metrics"
METRICS_FILE = "metrics.json"
ROLLUP_FILE = "_rollup.json"
QUEUE_FILE = "state/work_queue.json"
ORG_FILE = "org.json"
ESCALATIONS_DIR = "escalations"
ROLLING_WINDOW_DAYS = 7
DEFAULT_PROJECT_ID = "_default"


def get_company_dir() -> Path:
    """Get the company directory path."""
    return Path(os.getcwd()) / COMPANY_DIR


def get_metrics_dir() -> Path:
    """Get the metrics directory path for multi-project mode."""
    return get_company_dir() / METRICS_DIR


def get_metrics_path(project_id: str | None = None) -> Path:
    """
    Get metrics file path.

    In multi-project mode (project_id provided):
        Returns .company/metrics/{project_id}.json

    In single-project mode (no project_id):
        Returns .company/metrics.json for backward compatibility
    """
    if project_id:
        return get_metrics_dir() / f"{project_id}.json"
    return get_company_dir() / METRICS_FILE


def get_rollup_path() -> Path:
    """Get the rollup metrics file path."""
    return get_metrics_dir() / ROLLUP_FILE


def get_queue_path() -> Path:
    """Get work_queue.json path."""
    return get_company_dir() / QUEUE_FILE


def get_org_path() -> Path:
    """Get org.json path."""
    return get_company_dir() / ORG_FILE


def get_escalations_dir() -> Path:
    """Get escalations directory path."""
    return get_company_dir() / ESCALATIONS_DIR


def ensure_company_dir():
    """Ensure .company directory exists."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


def ensure_metrics_dir():
    """Ensure .company/metrics directory exists."""
    metrics_dir = get_metrics_dir()
    metrics_dir.mkdir(parents=True, exist_ok=True)


def is_multi_project_mode() -> bool:
    """Check if we're in multi-project mode based on org.json."""
    org = load_org()
    return org.get("mode") == "multi-project"


def list_registered_projects() -> list[dict]:
    """Get list of registered projects from org.json."""
    org = load_org()
    return org.get("projects", [])


def get_empty_metrics(project_id: str | None = None) -> dict:
    """Return empty metrics structure."""
    return {
        "project_id": project_id or DEFAULT_PROJECT_ID,
        "task_completions": [],
        "escalation_records": [],
        "daily_snapshots": [],
        "velocity": {
            "daily": [],  # List of {date, count} for tasks completed each day
            "weekly_average": 0.0,
        },
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "rolling_window_days": ROLLING_WINDOW_DAYS,
        },
    }


def get_empty_rollup() -> dict:
    """Return empty rollup structure for company-wide aggregation."""
    return {
        "type": "rollup",
        "projects": {},  # project_id -> summary metrics
        "totals": {
            "total_tasks_completed": 0,
            "total_tasks_today": 0,
            "total_escalations": 0,
            "total_active_agents": 0,
            "total_blocked_tasks": 0,
            "company_utilization_percent": 0.0,
            "company_velocity_daily": 0.0,
        },
        "by_project": [],  # List of per-project summaries
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "rolling_window_days": ROLLING_WINDOW_DAYS,
        },
    }


def load_metrics(project_id: str | None = None) -> dict:
    """
    Load metrics from file.

    Args:
        project_id: Optional project ID for multi-project mode.
                   If provided, loads from .company/metrics/{project_id}.json
                   Otherwise, loads from .company/metrics.json
    """
    metrics_path = get_metrics_path(project_id)

    if not metrics_path.exists():
        return get_empty_metrics(project_id)

    try:
        with open(metrics_path, encoding="utf-8") as f:
            data = json.load(f)
            # Ensure project_id is set for backward compatibility
            if project_id and "project_id" not in data:
                data["project_id"] = project_id
            return data
    except (json.JSONDecodeError, OSError):
        return get_empty_metrics(project_id)


def load_rollup() -> dict:
    """Load company-wide rollup metrics."""
    rollup_path = get_rollup_path()

    if not rollup_path.exists():
        return get_empty_rollup()

    try:
        with open(rollup_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return get_empty_rollup()


def save_metrics(metrics: dict, project_id: str | None = None):
    """
    Save metrics to file with rolling window cleanup.

    Args:
        metrics: Metrics data to save
        project_id: Optional project ID for multi-project mode
    """
    if project_id:
        ensure_metrics_dir()
    else:
        ensure_company_dir()

    metrics_path = get_metrics_path(project_id)

    # Apply rolling window cleanup
    cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
    cutoff_str = cutoff.isoformat()

    # Clean up old task completions
    metrics["task_completions"] = [
        tc
        for tc in metrics.get("task_completions", [])
        if tc.get("completed_at", "") >= cutoff_str
    ]

    # Clean up old escalation records
    metrics["escalation_records"] = [
        er
        for er in metrics.get("escalation_records", [])
        if er.get("recorded_at", "") >= cutoff_str
    ]

    # Clean up old daily snapshots
    metrics["daily_snapshots"] = [
        ds
        for ds in metrics.get("daily_snapshots", [])
        if ds.get("date", "") >= cutoff.strftime("%Y-%m-%d")
    ]

    # Clean up old velocity daily entries
    if "velocity" in metrics and "daily" in metrics["velocity"]:
        metrics["velocity"]["daily"] = [
            v
            for v in metrics["velocity"]["daily"]
            if v.get("date", "") >= cutoff.strftime("%Y-%m-%d")
        ]

    metrics["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


def save_rollup(rollup: dict):
    """Save company-wide rollup metrics."""
    ensure_metrics_dir()
    rollup_path = get_rollup_path()
    rollup["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(rollup_path, "w", encoding="utf-8") as f:
        json.dump(rollup, f, indent=2)


def list_project_metrics_files() -> list[Path]:
    """List all per-project metrics files in .company/metrics/."""
    metrics_dir = get_metrics_dir()
    if not metrics_dir.exists():
        return []

    files = []
    for f in metrics_dir.glob("*.json"):
        # Exclude the rollup file
        if f.name != ROLLUP_FILE:
            files.append(f)
    return files


def get_project_id_from_path(metrics_file: Path) -> str:
    """Extract project ID from metrics file path."""
    return metrics_file.stem  # filename without .json extension


def load_queue() -> dict:
    """Load work queue from file."""
    queue_path = get_queue_path()

    if not queue_path.exists():
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
        }

    try:
        with open(queue_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
        }


def load_org() -> dict:
    """Load organization structure from org.json."""
    org_path = get_org_path()

    if not org_path.exists():
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }

    try:
        with open(org_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }


def load_escalations() -> list[dict]:
    """Load all escalation records."""
    escalations_dir = get_escalations_dir()

    if not escalations_dir.exists():
        return []

    escalations = []
    for esc_file in escalations_dir.glob("*.json"):
        try:
            with open(esc_file, encoding="utf-8") as f:
                escalations.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue

    return escalations


# -----------------------------------------------------------------------------
# Recording Functions
# -----------------------------------------------------------------------------


def update_velocity(metrics: dict) -> None:
    """
    Update velocity metrics based on task completions.

    Calculates daily task counts and weekly average velocity.
    """
    completions = metrics.get("task_completions", [])

    # Group completions by date
    by_date: dict[str, int] = {}
    for c in completions:
        date = c.get("date", "")
        if date:
            by_date[date] = by_date.get(date, 0) + 1

    # Build daily velocity list
    daily = [{"date": d, "count": c} for d, c in sorted(by_date.items())]

    # Calculate 7-day average
    last_7_days = []
    for i in range(7):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        last_7_days.append(by_date.get(date, 0))

    weekly_avg = sum(last_7_days) / 7 if last_7_days else 0.0

    # Update metrics
    if "velocity" not in metrics:
        metrics["velocity"] = {}
    metrics["velocity"]["daily"] = daily
    metrics["velocity"]["weekly_average"] = round(weekly_avg, 2)


def record_task_completion(
    task_id: str,
    department: str | None = None,
    duration_minutes: float | None = None,
    agent_id: str | None = None,
    complexity: str = "standard",
    project_id: str | None = None,
) -> dict:
    """
    Record a task completion event.

    Args:
        task_id: The completed task ID
        department: Department that owned the task
        duration_minutes: Time to complete in minutes
        agent_id: Agent that completed the task
        complexity: Task complexity (trivial, standard, complex, epic)
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with recording result
    """
    metrics = load_metrics(project_id)
    now = datetime.now(timezone.utc)

    completion_record = {
        "task_id": task_id,
        "completed_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "department": department,
        "duration_minutes": duration_minutes,
        "agent_id": agent_id,
        "complexity": complexity,
    }

    if project_id:
        completion_record["project_id"] = project_id

    metrics.setdefault("task_completions", []).append(completion_record)

    # Update velocity metrics
    update_velocity(metrics)

    save_metrics(metrics, project_id)

    return {
        "success": True,
        "recorded": completion_record,
        "project_id": project_id or DEFAULT_PROJECT_ID,
        "message": f"Task completion recorded for {task_id}",
    }


def record_escalation(
    tier: int,
    reason: str,
    task_id: str | None = None,
    resolution_minutes: float | None = None,
    resolved: bool = False,
    project_id: str | None = None,
) -> dict:
    """
    Record an escalation event.

    Args:
        tier: Escalation tier (1-4)
        reason: Escalation trigger reason
        task_id: Associated task ID
        resolution_minutes: Time to resolve in minutes
        resolved: Whether the escalation was resolved
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with recording result
    """
    metrics = load_metrics(project_id)
    now = datetime.now(timezone.utc)

    escalation_record = {
        "tier": tier,
        "reason": reason,
        "task_id": task_id,
        "recorded_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "resolution_minutes": resolution_minutes,
        "resolved": resolved,
    }

    if project_id:
        escalation_record["project_id"] = project_id

    metrics.setdefault("escalation_records", []).append(escalation_record)
    save_metrics(metrics, project_id)

    return {
        "success": True,
        "recorded": escalation_record,
        "project_id": project_id or DEFAULT_PROJECT_ID,
        "message": f"Escalation recorded: tier {tier}, reason: {reason}",
    }


# -----------------------------------------------------------------------------
# Metrics Calculation Functions
# -----------------------------------------------------------------------------


def calculate_task_metrics(project_id: str | None = None) -> dict:
    """
    Calculate task-related metrics.

    Args:
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with task metrics:
        - completed_count: Total completed in window
        - completed_today: Completed today
        - average_duration_minutes: Average completion time
        - by_department: Breakdown by department
        - velocity_daily: Average tasks per day
    """
    metrics = load_metrics(project_id)
    queue = load_queue()

    completions = metrics.get("task_completions", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Count completions
    completed_count = len(completions)
    completed_today = sum(1 for c in completions if c.get("date") == today)

    # Calculate average duration
    durations = [
        c.get("duration_minutes")
        for c in completions
        if c.get("duration_minutes") is not None
    ]
    average_duration = round(sum(durations) / len(durations), 1) if durations else 0

    # Group by department
    by_department: dict[str, int] = {}
    for completion in completions:
        dept = completion.get("department") or "unassigned"
        by_department[dept] = by_department.get(dept, 0) + 1

    # Also add from queue's completed list
    queue_completed = len(queue.get("completed", []))

    # Get velocity metrics
    velocity = metrics.get("velocity", {})
    velocity_daily = velocity.get("weekly_average", 0.0)

    return {
        "completed_count": completed_count,
        "completed_today": completed_today,
        "average_duration_minutes": average_duration,
        "by_department": by_department,
        "queue_completed_total": queue_completed,
        "velocity_daily": velocity_daily,
    }


def calculate_escalation_metrics(project_id: str | None = None) -> dict:
    """
    Calculate escalation-related metrics.

    Args:
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with escalation metrics:
        - total_count: Total escalations in window
        - by_tier: Count by tier (1-4)
        - by_reason: Count by trigger reason
        - avg_resolution_minutes: Average resolution time
    """
    metrics = load_metrics(project_id)
    escalations_data = load_escalations()

    # From recorded metrics
    recorded = metrics.get("escalation_records", [])
    total_count = len(recorded)

    # Count by tier
    by_tier: dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0}
    for esc in recorded:
        tier = str(esc.get("tier", 1))
        by_tier[tier] = by_tier.get(tier, 0) + 1

    # Count by reason
    by_reason: dict[str, int] = {}
    for esc in recorded:
        reason = esc.get("reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1

    # Calculate average resolution time
    resolution_times = [
        esc.get("resolution_minutes")
        for esc in recorded
        if esc.get("resolution_minutes") is not None and esc.get("resolved")
    ]
    avg_resolution = (
        round(sum(resolution_times) / len(resolution_times), 1)
        if resolution_times
        else 0
    )

    # Add live escalations from files
    active_escalations = [
        e
        for e in escalations_data
        if e.get("status") in ("pending", "in_progress", "paused")
    ]

    return {
        "total_count": total_count,
        "by_tier": by_tier,
        "by_reason": by_reason,
        "avg_resolution_minutes": avg_resolution,
        "active_escalations": len(active_escalations),
    }


def calculate_agent_metrics() -> dict:
    """
    Calculate agent-related metrics using a rolling window.

    Uses both point-in-time (in_progress tasks) and rolling window
    (completed tasks in the last hour) to measure utilization. This
    prevents utilization from showing 0% between daemon cycles when
    agents are actively completing work.

    Returns:
        Dict with agent metrics:
        - active_count: Agents currently working or recently active
        - idle_count: Agents not assigned to tasks
        - blocked_count: Agents working on blocked tasks
        - utilization_percent: Percentage of agents actively working
    """
    org = load_org()
    queue = load_queue()

    agents = org.get("employees", []) or org.get("agents", [])
    total_agents = len(agents)

    if total_agents == 0:
        return {
            "active_count": 0,
            "idle_count": 0,
            "blocked_count": 0,
            "utilization_percent": 0,
            "total_agents": 0,
        }

    # Find agents working on in_progress tasks (point-in-time)
    in_progress_agents = set()
    for task in queue.get("in_progress", []):
        agent_id = task.get("assigned_to")
        if agent_id:
            in_progress_agents.add(agent_id)

    # Find agents working on blocked tasks
    blocked_agents = set()
    for task in queue.get("blocked", []):
        agent_id = task.get("assigned_to")
        if agent_id:
            blocked_agents.add(agent_id)

    # Rolling window: agents that completed tasks in the last hour
    # This captures daemon activity between cycles
    recently_active_agents = set()
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    for task in queue.get("completed", []):
        completed_at_str = task.get("completed_at")
        if not completed_at_str:
            continue
        try:
            completed_at = datetime.fromisoformat(
                completed_at_str.replace("Z", "+00:00")
            )
            if completed_at >= one_hour_ago:
                # Use completed_by if set, fall back to assigned_to
                # (completed_by may be null when employee_id wasn't passed to release_task)
                completed_by = task.get("completed_by") or task.get("assigned_to")
                if completed_by:
                    recently_active_agents.add(completed_by)
        except (ValueError, TypeError):
            pass

    # Combine point-in-time and rolling window
    all_active_agents = in_progress_agents | recently_active_agents
    active_count = len(all_active_agents)
    blocked_count = len(blocked_agents - in_progress_agents)

    # Get registered agent IDs
    registered_agents = {a.get("id") for a in agents if a.get("id")}

    # Idle = registered but not in any active work (current or recent)
    working_agents = all_active_agents | blocked_agents
    idle_count = len(registered_agents - working_agents)

    # Utilization = actively working / total
    utilization = (
        round((active_count / total_agents) * 100, 1) if total_agents > 0 else 0
    )

    return {
        "active_count": active_count,
        "idle_count": idle_count,
        "blocked_count": blocked_count,
        "utilization_percent": utilization,
        "total_agents": total_agents,
    }


def calculate_queue_metrics() -> dict:
    """
    Calculate queue-related metrics.

    Returns:
        Dict with queue metrics:
        - pending_count: Tasks waiting to be picked up
        - blocked_count: Tasks blocked on dependencies
        - oldest_pending_minutes: Age of oldest pending task
    """
    queue = load_queue()
    now = datetime.now(timezone.utc)

    pending = queue.get("pending", [])
    blocked = queue.get("blocked", [])
    in_progress = queue.get("in_progress", [])

    pending_count = len(pending)
    blocked_count = len(blocked)
    in_progress_count = len(in_progress)

    # Find oldest pending task
    oldest_pending_minutes = 0
    for task in pending:
        created_at = task.get("created_at")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (now - created).total_seconds() / 60
                oldest_pending_minutes = max(oldest_pending_minutes, age)
            except (ValueError, TypeError):
                continue

    return {
        "pending_count": pending_count,
        "blocked_count": blocked_count,
        "in_progress_count": in_progress_count,
        "oldest_pending_minutes": round(oldest_pending_minutes, 1),
    }


# -----------------------------------------------------------------------------
# Summary and Report Functions
# -----------------------------------------------------------------------------


def get_health_summary(project_id: str | None = None) -> dict:
    """
    Get a quick health summary for the organization or a specific project.

    Args:
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with summary metrics for dashboard display
    """
    task_metrics = calculate_task_metrics(project_id)
    escalation_metrics = calculate_escalation_metrics(project_id)
    agent_metrics = calculate_agent_metrics()
    queue_metrics = calculate_queue_metrics()

    # Calculate overall health score (0-100)
    health_factors = []

    # Agent utilization (target: 60-80%)
    # Score is context-aware:
    # - Low util with no pending work = healthy idle (75)
    # - Low util but recently active agents = serial execution, healthy (75)
    # - Low util with pending work and no recent activity = bottleneck (25)
    # NOTE: The daemon executes tasks serially (1 at a time). With 16 agents,
    # max point-in-time utilization is ~6%. The rolling window helps but serial
    # mode will never reach 60%. Score serial-with-progress as healthy (75).
    util = agent_metrics["utilization_percent"]
    active_workload = (
        queue_metrics["pending_count"] + queue_metrics["in_progress_count"]
    )
    recently_active = agent_metrics.get("active_count", 0)
    if 60 <= util <= 80:
        health_factors.append(100)
    elif 40 <= util < 60 or 80 < util <= 90:
        health_factors.append(75)
    elif 20 <= util < 40 or 90 < util <= 95:
        health_factors.append(50)
    elif active_workload == 0:
        # No pending or in-progress tasks — agents idle because there is no work.
        # This is a healthy idle state, not a utilization bottleneck.
        health_factors.append(75)
    elif recently_active > 0:
        # Agents completed work recently (rolling window) — the daemon is making
        # progress serially. Low util is expected in serial execution mode.
        # Score 75 (not 50) because the system IS working — serial execution
        # with active progress is healthy, not a warning condition.
        health_factors.append(75)
    else:
        health_factors.append(25)

    # Blocked ratio (target: < 10%)
    total_active = (
        queue_metrics["pending_count"]
        + queue_metrics["in_progress_count"]
        + queue_metrics["blocked_count"]
    )
    blocked_ratio = (
        (queue_metrics["blocked_count"] / total_active * 100) if total_active > 0 else 0
    )
    if blocked_ratio < 10:
        health_factors.append(100)
    elif blocked_ratio < 20:
        health_factors.append(75)
    elif blocked_ratio < 30:
        health_factors.append(50)
    else:
        health_factors.append(25)

    # Active escalations (target: < 3)
    active_esc = escalation_metrics["active_escalations"]
    if active_esc == 0:
        health_factors.append(100)
    elif active_esc <= 2:
        health_factors.append(75)
    elif active_esc <= 5:
        health_factors.append(50)
    else:
        health_factors.append(25)

    # Oldest pending age (target: < 60 min)
    oldest = queue_metrics["oldest_pending_minutes"]
    if oldest < 60:
        health_factors.append(100)
    elif oldest < 120:
        health_factors.append(75)
    elif oldest < 240:
        health_factors.append(50)
    else:
        health_factors.append(25)

    health_score = (
        round(sum(health_factors) / len(health_factors)) if health_factors else 50
    )

    # Determine status
    if health_score >= 80:
        health_status = "healthy"
    elif health_score >= 60:
        health_status = "warning"
    else:
        health_status = "critical"

    result = {
        "success": True,
        "health_score": health_score,
        "health_status": health_status,
        "summary": {
            "tasks": {
                "completed_today": task_metrics["completed_today"],
                "completed_total": task_metrics["completed_count"],
                "avg_duration_minutes": task_metrics["average_duration_minutes"],
                "velocity_daily": task_metrics["velocity_daily"],
            },
            "escalations": {
                "active": escalation_metrics["active_escalations"],
                "total_week": escalation_metrics["total_count"],
            },
            "agents": {
                "active": agent_metrics["active_count"],
                "utilization": agent_metrics["utilization_percent"],
            },
            "queue": {
                "pending": queue_metrics["pending_count"],
                "blocked": queue_metrics["blocked_count"],
                "oldest_minutes": queue_metrics["oldest_pending_minutes"],
            },
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if project_id:
        result["project_id"] = project_id

    return result


def get_health_report(days: int = 7, project_id: str | None = None) -> dict:
    """
    Get a detailed health report for the organization or a specific project.

    Args:
        days: Number of days to include in report
        project_id: Optional project ID for multi-project mode

    Returns:
        Dict with comprehensive health metrics
    """
    metrics = load_metrics(project_id)
    org = load_org()

    task_metrics = calculate_task_metrics(project_id)
    escalation_metrics = calculate_escalation_metrics(project_id)
    agent_metrics = calculate_agent_metrics()
    queue_metrics = calculate_queue_metrics()

    # Calculate daily trends
    completions = metrics.get("task_completions", [])
    escalations = metrics.get("escalation_records", [])

    # Group completions by date
    completions_by_date: dict[str, int] = {}
    for c in completions:
        date = c.get("date", "unknown")
        completions_by_date[date] = completions_by_date.get(date, 0) + 1

    # Group escalations by date
    escalations_by_date: dict[str, int] = {}
    for e in escalations:
        date = e.get("date", "unknown")
        escalations_by_date[date] = escalations_by_date.get(date, 0) + 1

    # Build daily trends for last N days
    daily_trends = []
    for i in range(days):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_trends.append(
            {
                "date": date,
                "completions": completions_by_date.get(date, 0),
                "escalations": escalations_by_date.get(date, 0),
            }
        )

    daily_trends.reverse()  # Oldest first

    # Calculate department breakdown
    departments = org.get("departments", [])
    dept_breakdown = []
    for dept in departments:
        dept_id = dept.get("id", "unknown")
        dept_name = dept.get("name", dept_id)
        completions_count = task_metrics["by_department"].get(dept_id, 0)
        dept_breakdown.append(
            {
                "id": dept_id,
                "name": dept_name,
                "completions": completions_count,
            }
        )

    # Get summary metrics
    summary = get_health_summary(project_id)

    result = {
        "success": True,
        "report": {
            "health_score": summary["health_score"],
            "health_status": summary["health_status"],
            "period_days": days,
            "tasks": task_metrics,
            "escalations": escalation_metrics,
            "agents": agent_metrics,
            "queue": queue_metrics,
            "daily_trends": daily_trends,
            "department_breakdown": dept_breakdown,
        },
        "company_name": org.get("company", {}).get("name", "Unknown"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if project_id:
        result["project_id"] = project_id

    return result


# -----------------------------------------------------------------------------
# Multi-Project Aggregation Functions
# -----------------------------------------------------------------------------


def generate_rollup() -> dict:
    """
    Generate company-level rollup from all per-project metrics.

    Aggregates metrics from all projects in .company/metrics/{project_id}.json
    and saves to .company/metrics/_rollup.json.

    Returns:
        Dict with rollup generation result and aggregated metrics
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Get all project metrics files
    project_files = list_project_metrics_files()

    # Also check for single-project mode metrics.json
    single_project_path = get_company_dir() / METRICS_FILE
    has_single_project = single_project_path.exists()

    if not project_files and not has_single_project:
        return {
            "success": False,
            "error": "No project metrics found",
            "message": "Record some metrics first using record-task command",
        }

    # Initialize rollup
    rollup = get_empty_rollup()

    # Aggregated totals
    total_tasks_completed = 0
    total_tasks_today = 0
    total_escalations = 0
    total_blocked = 0
    total_velocity_sum = 0.0
    project_summaries = []

    # Process each project
    projects_processed = []

    # Include single-project mode metrics if present
    if has_single_project:
        project_id = DEFAULT_PROJECT_ID
        metrics = load_metrics(None)  # Load from metrics.json
        summary = _aggregate_project_metrics(metrics, project_id, today)
        projects_processed.append(project_id)
        project_summaries.append(summary)

        total_tasks_completed += summary["tasks_completed"]
        total_tasks_today += summary["tasks_today"]
        total_escalations += summary["escalations"]
        total_blocked += summary["blocked_tasks"]
        total_velocity_sum += summary["velocity_daily"]

        rollup["projects"][project_id] = summary

    # Process multi-project metrics
    for metrics_file in project_files:
        project_id = get_project_id_from_path(metrics_file)
        metrics = load_metrics(project_id)

        summary = _aggregate_project_metrics(metrics, project_id, today)
        projects_processed.append(project_id)
        project_summaries.append(summary)

        total_tasks_completed += summary["tasks_completed"]
        total_tasks_today += summary["tasks_today"]
        total_escalations += summary["escalations"]
        total_blocked += summary["blocked_tasks"]
        total_velocity_sum += summary["velocity_daily"]

        rollup["projects"][project_id] = summary

    # Calculate company-level utilization
    agent_metrics = calculate_agent_metrics()
    company_utilization = agent_metrics["utilization_percent"]

    # Calculate average velocity across projects
    project_count = len(projects_processed)
    company_velocity = (
        round(total_velocity_sum / project_count, 2) if project_count > 0 else 0.0
    )

    # Update totals
    rollup["totals"] = {
        "total_tasks_completed": total_tasks_completed,
        "total_tasks_today": total_tasks_today,
        "total_escalations": total_escalations,
        "total_active_agents": agent_metrics["active_count"],
        "total_blocked_tasks": total_blocked,
        "company_utilization_percent": company_utilization,
        "company_velocity_daily": company_velocity,
    }

    # Build by_project list (sorted by project_id)
    rollup["by_project"] = sorted(project_summaries, key=lambda x: x["project_id"])

    # Save rollup
    save_rollup(rollup)

    return {
        "success": True,
        "rollup_path": str(get_rollup_path()),
        "projects_processed": projects_processed,
        "totals": rollup["totals"],
        "message": f"Rollup generated for {project_count} project(s)",
        "generated_at": now.isoformat(),
    }


def _aggregate_project_metrics(metrics: dict, project_id: str, today: str) -> dict:
    """
    Aggregate metrics for a single project.

    Args:
        metrics: Project metrics data
        project_id: Project identifier
        today: Today's date string (YYYY-MM-DD)

    Returns:
        Dict with aggregated project summary
    """
    completions = metrics.get("task_completions", [])
    escalations = metrics.get("escalation_records", [])
    velocity = metrics.get("velocity", {})

    tasks_completed = len(completions)
    tasks_today = sum(1 for c in completions if c.get("date") == today)

    # Calculate average duration
    durations = [
        c.get("duration_minutes")
        for c in completions
        if c.get("duration_minutes") is not None
    ]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0

    # Count blocked tasks (from escalations with reason 'blocked')
    blocked_count = sum(
        1 for e in escalations if e.get("reason") == "blocked" and not e.get("resolved")
    )

    return {
        "project_id": project_id,
        "tasks_completed": tasks_completed,
        "tasks_today": tasks_today,
        "avg_duration_minutes": avg_duration,
        "escalations": len(escalations),
        "blocked_tasks": blocked_count,
        "velocity_daily": velocity.get("weekly_average", 0.0),
        "last_updated": metrics.get("metadata", {}).get("last_updated", ""),
    }


def get_projects_with_metrics() -> dict:
    """
    List all projects that have metrics data.

    Returns:
        Dict with list of projects and their basic metrics
    """
    projects = []

    # Check single-project mode
    single_project_path = get_company_dir() / METRICS_FILE
    if single_project_path.exists():
        metrics = load_metrics(None)
        task_count = len(metrics.get("task_completions", []))
        last_updated = metrics.get("metadata", {}).get("last_updated", "")
        projects.append(
            {
                "project_id": DEFAULT_PROJECT_ID,
                "metrics_path": str(single_project_path),
                "task_completions": task_count,
                "last_updated": last_updated,
                "mode": "single-project",
            }
        )

    # Check multi-project metrics
    for metrics_file in list_project_metrics_files():
        project_id = get_project_id_from_path(metrics_file)
        metrics = load_metrics(project_id)
        task_count = len(metrics.get("task_completions", []))
        last_updated = metrics.get("metadata", {}).get("last_updated", "")
        projects.append(
            {
                "project_id": project_id,
                "metrics_path": str(metrics_file),
                "task_completions": task_count,
                "last_updated": last_updated,
                "mode": "multi-project",
            }
        )

    # Also check registered projects from org.json
    registered = list_registered_projects()
    _ = {p.get("id") for p in registered}  # Reserved for future filtering
    project_ids_with_metrics = {p["project_id"] for p in projects}

    # Add registered projects without metrics yet
    for reg_project in registered:
        pid = reg_project.get("id")
        if pid and pid not in project_ids_with_metrics:
            projects.append(
                {
                    "project_id": pid,
                    "metrics_path": str(get_metrics_path(pid)),
                    "task_completions": 0,
                    "last_updated": "",
                    "mode": "multi-project",
                    "status": "no-metrics",
                    "name": reg_project.get("name", pid),
                }
            )

    return {
        "success": True,
        "projects": projects,
        "count": len(projects),
        "multi_project_mode": is_multi_project_mode(),
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Company Health Metrics Tracker — capture and monitor organizational metrics

Supports both single-project and multi-project modes with per-project isolation
and company-level rollups.

Commands:
    record-task        Record a task completion event
    record-escalation  Record an escalation event
    summary            Get quick health summary
    report             Get detailed health report
    rollup             Generate company-wide rollup from all projects
    list-projects      List all projects with metrics

Record-task options:
    --task-id ID       Task ID (required)
    --project ID       Project ID (for multi-project mode)
    --department ID    Department that owned the task
    --duration MIN     Completion time in minutes
    --agent-id ID      Agent that completed the task
    --complexity TYPE  Task complexity (trivial, standard, complex, epic)

Record-escalation options:
    --tier 1-4         Escalation tier (required)
    --reason TEXT      Trigger reason (required)
    --project ID       Project ID (for multi-project mode)
    --task-id ID       Associated task ID
    --resolution-minutes MIN  Time to resolve
    --resolved         Mark as resolved

Summary/Report options:
    --project ID       Project ID (for multi-project mode)
    --days N           Number of days to include (default: 7, report only)

Metrics tracked:
    Tasks:       completed_count, completed_today, average_duration_minutes, by_department, velocity
    Escalations: total_count, by_tier, by_reason, avg_resolution_minutes
    Agents:      active_count, idle_count, blocked_count, utilization_percent
    Queue:       pending_count, blocked_count, oldest_pending_minutes
    Velocity:    daily task completion rate (per project and company-wide)

Storage:
    Single-project: .company/metrics.json (7-day rolling window)
    Multi-project:
        Per-project: .company/metrics/{project_id}.json
        Rollup:      .company/metrics/_rollup.json

Examples:
    # Record task completion (single-project mode)
    python metrics_tracker.py record-task --task-id task-123 --department engineering --duration 45

    # Record task completion (multi-project mode)
    python metrics_tracker.py record-task --task-id task-123 --project forge-cli --duration 45

    # Record an escalation
    python metrics_tracker.py record-escalation --tier 2 --reason timeout --resolution-minutes 30 --resolved

    # Get quick health summary
    python metrics_tracker.py summary
    python metrics_tracker.py summary --project forge-cli

    # Get detailed report for last 7 days
    python metrics_tracker.py report --days 7
    python metrics_tracker.py report --project forge-cli --days 7

    # Generate company-wide rollup
    python metrics_tracker.py rollup

    # List all projects with metrics
    python metrics_tracker.py list-projects

Output: JSON with metrics data.
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "record-task":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            duration = None
            if "duration" in args:
                try:
                    duration = float(args["duration"])
                except ValueError:
                    print("Error: --duration must be a number")
                    sys.exit(1)

            # Get project_id for multi-project mode
            project_id = args.get("project")

            result = record_task_completion(
                task_id=args["task_id"],
                department=args.get("department"),
                duration_minutes=duration,
                agent_id=args.get("agent_id"),
                complexity=args.get("complexity", "standard"),
                project_id=project_id,
            )
            print(json.dumps(result, indent=2))

        elif command == "record-escalation":
            if "tier" not in args:
                print("Error: --tier required (1-4)")
                sys.exit(1)
            if "reason" not in args:
                print("Error: --reason required")
                sys.exit(1)

            try:
                tier = int(args["tier"])
                if tier < 1 or tier > 4:
                    raise ValueError("Tier must be 1-4")
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)

            resolution_min = None
            if "resolution_minutes" in args:
                try:
                    resolution_min = float(args["resolution_minutes"])
                except ValueError:
                    print("Error: --resolution-minutes must be a number")
                    sys.exit(1)

            # Get project_id for multi-project mode
            project_id = args.get("project")

            result = record_escalation(
                tier=tier,
                reason=args["reason"],
                task_id=args.get("task_id"),
                resolution_minutes=resolution_min,
                resolved=args.get("resolved", False) is True,
                project_id=project_id,
            )
            print(json.dumps(result, indent=2))

        elif command == "summary":
            # Get project_id for multi-project mode
            project_id = args.get("project")
            result = get_health_summary(project_id=project_id)
            print(json.dumps(result, indent=2))

        elif command == "report":
            days = 7
            if "days" in args:
                try:
                    days = int(args["days"])
                except ValueError:
                    print("Error: --days must be a number")
                    sys.exit(1)

            # Get project_id for multi-project mode
            project_id = args.get("project")
            result = get_health_report(days=days, project_id=project_id)
            print(json.dumps(result, indent=2))

        elif command == "rollup":
            result = generate_rollup()
            print(json.dumps(result, indent=2))

        elif command == "list-projects":
            result = get_projects_with_metrics()
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
