#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Dashboard Aggregator — aggregates data from multiple sources for the dashboard.

Combines data from metrics_tracker and progress_tracker to provide a unified
dashboard view of company health, progress, workforce, and risks.

Multi-Project Support:
- Works in both single-project and multi-project modes
- Provides per-project breakdown when in multi-project mode
- Calculates company-wide rollups

Functions:
    aggregate_health() -> dict
        Overall health score with breakdown by factors

    aggregate_progress() -> dict
        Task completion, velocity, delivery forecast

    aggregate_workforce() -> dict
        Agent status, utilization, department breakdown

    aggregate_risks() -> list[dict]
        Identified risks with severity levels

    get_dashboard_data() -> dict
        Full aggregation combining all the above

Usage:
    # Get overall health
    python dashboard_aggregator.py health

    # Get progress metrics
    python dashboard_aggregator.py progress

    # Get workforce status
    python dashboard_aggregator.py workforce

    # Get identified risks
    python dashboard_aggregator.py risks

    # Get full dashboard data
    python dashboard_aggregator.py full

    # Show help
    python dashboard_aggregator.py help
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Import from existing utilities
try:
    from metrics_tracker import (
        calculate_agent_metrics,
        calculate_escalation_metrics,
        calculate_queue_metrics,
        calculate_task_metrics,
        generate_rollup,
        get_health_summary,
        load_org,
    )

    METRICS_TRACKER_AVAILABLE = True
except ImportError:
    METRICS_TRACKER_AVAILABLE = False

try:
    from progress_tracker import (
        detect_stalled_work,
        discover_projects,
        get_company_progress,
    )

    PROGRESS_TRACKER_AVAILABLE = True
except ImportError:
    PROGRESS_TRACKER_AVAILABLE = False

# Lazy import for orchestrator_metrics (P28.2)
ORCHESTRATOR_METRICS_AVAILABLE = False


def _lazy_load_orchestrator_metrics():
    """Lazy load orchestrator_metrics module to avoid circular imports."""
    global ORCHESTRATOR_METRICS_AVAILABLE
    try:
        from orchestrator_metrics import get_metrics_summary

        ORCHESTRATOR_METRICS_AVAILABLE = True
        return get_metrics_summary
    except ImportError:
        ORCHESTRATOR_METRICS_AVAILABLE = False
        return None


# Configuration
DEFAULT_STALL_THRESHOLD_MINUTES = 60


def _ensure_dependencies() -> tuple[bool, str]:
    """Check if required dependencies are available."""
    if not METRICS_TRACKER_AVAILABLE:
        return False, "metrics_tracker module not available"
    if not PROGRESS_TRACKER_AVAILABLE:
        return False, "progress_tracker module not available"
    return True, ""


def _safe_get_health_summary(project_id: str | None = None) -> dict:
    """Safely get health summary, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "success": False,
            "health_score": 50,
            "health_status": "unknown",
            "summary": {
                "tasks": {
                    "completed_today": 0,
                    "completed_total": 0,
                    "velocity_daily": 0.0,
                },
                "escalations": {"active": 0, "total_week": 0},
                "agents": {"active": 0, "utilization": 0.0},
                "queue": {"pending": 0, "blocked": 0, "oldest_minutes": 0.0},
            },
        }
    try:
        return get_health_summary(project_id)
    except Exception:
        return {
            "success": False,
            "health_score": 50,
            "health_status": "unknown",
            "summary": {
                "tasks": {
                    "completed_today": 0,
                    "completed_total": 0,
                    "velocity_daily": 0.0,
                },
                "escalations": {"active": 0, "total_week": 0},
                "agents": {"active": 0, "utilization": 0.0},
                "queue": {"pending": 0, "blocked": 0, "oldest_minutes": 0.0},
            },
        }


def _safe_get_company_progress(include_breakdown: bool = False) -> dict:
    """Safely get company progress, returning defaults if unavailable."""
    if not PROGRESS_TRACKER_AVAILABLE:
        return {
            "success": False,
            "summary": {
                "total_tasks": 0,
                "completed": 0,
                "in_progress": 0,
                "blocked": 0,
                "pending": 0,
            },
            "percentages": {
                "completed": 0.0,
                "in_progress": 0.0,
                "blocked": 0.0,
                "pending": 0.0,
            },
            "health": {"blocked_ratio": 0.0, "is_healthy": True, "status": "unknown"},
        }
    try:
        return get_company_progress(include_breakdown)
    except Exception:
        return {
            "success": False,
            "summary": {
                "total_tasks": 0,
                "completed": 0,
                "in_progress": 0,
                "blocked": 0,
                "pending": 0,
            },
            "percentages": {
                "completed": 0.0,
                "in_progress": 0.0,
                "blocked": 0.0,
                "pending": 0.0,
            },
            "health": {"blocked_ratio": 0.0, "is_healthy": True, "status": "unknown"},
        }


def _safe_detect_stalled_work(
    threshold_minutes: int = DEFAULT_STALL_THRESHOLD_MINUTES,
) -> dict:
    """Safely detect stalled work, returning defaults if unavailable."""
    if not PROGRESS_TRACKER_AVAILABLE:
        return {
            "success": False,
            "total_stalled": 0,
            "stalled_in_progress": [],
            "stalled_in_progress_count": 0,
            "long_blocked": [],
            "long_blocked_count": 0,
        }
    try:
        return detect_stalled_work(threshold_minutes)
    except Exception:
        return {
            "success": False,
            "total_stalled": 0,
            "stalled_in_progress": [],
            "stalled_in_progress_count": 0,
            "long_blocked": [],
            "long_blocked_count": 0,
        }


def _safe_discover_projects() -> list[dict]:
    """Safely discover projects, returning empty list if unavailable."""
    if not PROGRESS_TRACKER_AVAILABLE:
        return []
    try:
        return discover_projects()
    except Exception:
        return []


def _safe_calculate_agent_metrics() -> dict:
    """Safely calculate agent metrics, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "active_count": 0,
            "idle_count": 0,
            "blocked_count": 0,
            "utilization_percent": 0.0,
            "total_agents": 0,
        }
    try:
        return calculate_agent_metrics()
    except Exception:
        return {
            "active_count": 0,
            "idle_count": 0,
            "blocked_count": 0,
            "utilization_percent": 0.0,
            "total_agents": 0,
        }


def _safe_calculate_queue_metrics() -> dict:
    """Safely calculate queue metrics, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "pending_count": 0,
            "blocked_count": 0,
            "in_progress_count": 0,
            "oldest_pending_minutes": 0.0,
        }
    try:
        return calculate_queue_metrics()
    except Exception:
        return {
            "pending_count": 0,
            "blocked_count": 0,
            "in_progress_count": 0,
            "oldest_pending_minutes": 0.0,
        }


def _safe_calculate_task_metrics(project_id: str | None = None) -> dict:
    """Safely calculate task metrics, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "completed_count": 0,
            "completed_today": 0,
            "average_duration_minutes": 0.0,
            "by_department": {},
            "velocity_daily": 0.0,
        }
    try:
        return calculate_task_metrics(project_id)
    except Exception:
        return {
            "completed_count": 0,
            "completed_today": 0,
            "average_duration_minutes": 0.0,
            "by_department": {},
            "velocity_daily": 0.0,
        }


def _safe_calculate_escalation_metrics(project_id: str | None = None) -> dict:
    """Safely calculate escalation metrics, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "total_count": 0,
            "by_tier": {"1": 0, "2": 0, "3": 0, "4": 0},
            "by_reason": {},
            "avg_resolution_minutes": 0.0,
            "active_escalations": 0,
        }
    try:
        return calculate_escalation_metrics(project_id)
    except Exception:
        return {
            "total_count": 0,
            "by_tier": {"1": 0, "2": 0, "3": 0, "4": 0},
            "by_reason": {},
            "avg_resolution_minutes": 0.0,
            "active_escalations": 0,
        }


def _safe_load_org() -> dict:
    """Safely load org data, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }
    try:
        return load_org()
    except Exception:
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }


def _safe_generate_rollup() -> dict:
    """Safely generate rollup, returning defaults if unavailable."""
    if not METRICS_TRACKER_AVAILABLE:
        return {
            "success": False,
            "totals": {
                "total_tasks_completed": 0,
                "total_tasks_today": 0,
                "total_escalations": 0,
                "total_active_agents": 0,
                "total_blocked_tasks": 0,
                "company_utilization_percent": 0.0,
                "company_velocity_daily": 0.0,
            },
            "by_project": [],
        }
    try:
        return generate_rollup()
    except Exception:
        return {
            "success": False,
            "totals": {
                "total_tasks_completed": 0,
                "total_tasks_today": 0,
                "total_escalations": 0,
                "total_active_agents": 0,
                "total_blocked_tasks": 0,
                "company_utilization_percent": 0.0,
                "company_velocity_daily": 0.0,
            },
            "by_project": [],
        }


# -----------------------------------------------------------------------------
# Aggregation Functions
# -----------------------------------------------------------------------------


def aggregate_health(project_id: str | None = None) -> dict:
    """
    Aggregate overall health score with breakdown by factors.

    Args:
        project_id: Optional project ID for project-specific health

    Returns:
        Dict with:
        - health_score: Overall score (0-100)
        - health_status: "healthy", "warning", or "critical"
        - factors: Breakdown of contributing factors
        - per_project: Project-level health (multi-project mode)
    """
    now = datetime.now(timezone.utc)

    # Get health summary from metrics_tracker
    health_summary = _safe_get_health_summary(project_id)
    company_progress = _safe_get_company_progress(include_breakdown=True)

    # Extract scores and factors
    health_score = health_summary.get("health_score", 50)
    health_status = health_summary.get("health_status", "unknown")

    summary = health_summary.get("summary", {})
    _ = summary.get("tasks", {})  # Reserved for future use
    escalations = summary.get("escalations", {})
    agents = summary.get("agents", {})
    queue = summary.get("queue", {})

    # Calculate factor scores (replicating health_summary logic for transparency)
    factors = []

    # Agent utilization factor
    # Score is context-aware:
    # - Low util with no pending work = healthy idle (75)
    # - Low util but recently active agents = serial execution, not stuck (50)
    # - Low util with pending work and no recent activity = bottleneck (25)
    util = agents.get("utilization", 0.0)
    queue_pending = queue.get("pending", 0)
    recently_active = agents.get("active", 0)
    if 60 <= util <= 80:
        util_score = 100
        util_status = "optimal"
    elif 40 <= util < 60 or 80 < util <= 90:
        util_score = 75
        util_status = "acceptable"
    elif 20 <= util < 40 or 90 < util <= 95:
        util_score = 50
        util_status = "warning"
    elif queue_pending == 0:
        # No pending tasks — agents idle because there is no work to do.
        # This is a healthy idle state, not a utilization bottleneck.
        util_score = 75
        util_status = "acceptable"
    elif recently_active > 0:
        # Agents completed work recently (rolling window) — the daemon is making
        # progress serially. Low util is expected in serial execution mode.
        # Score 75: serial execution with active progress is healthy.
        util_score = 75
        util_status = "acceptable"
    else:
        util_score = 25
        util_status = "critical"

    factors.append(
        {
            "name": "agent_utilization",
            "score": util_score,
            "status": util_status,
            "value": util,
            "target_range": "60-80%",
            "description": f"Agent utilization at {util}%",
        }
    )

    # Blocked ratio factor
    blocked_ratio = company_progress.get("health", {}).get("blocked_ratio", 0.0)
    if blocked_ratio < 10:
        blocked_score = 100
        blocked_status = "optimal"
    elif blocked_ratio < 20:
        blocked_score = 75
        blocked_status = "acceptable"
    elif blocked_ratio < 30:
        blocked_score = 50
        blocked_status = "warning"
    else:
        blocked_score = 25
        blocked_status = "critical"

    factors.append(
        {
            "name": "blocked_ratio",
            "score": blocked_score,
            "status": blocked_status,
            "value": blocked_ratio,
            "target_range": "<10%",
            "description": f"Blocked task ratio at {blocked_ratio}%",
        }
    )

    # Active escalations factor
    active_esc = escalations.get("active", 0)
    if active_esc == 0:
        esc_score = 100
        esc_status = "optimal"
    elif active_esc <= 2:
        esc_score = 75
        esc_status = "acceptable"
    elif active_esc <= 5:
        esc_score = 50
        esc_status = "warning"
    else:
        esc_score = 25
        esc_status = "critical"

    factors.append(
        {
            "name": "active_escalations",
            "score": esc_score,
            "status": esc_status,
            "value": active_esc,
            "target_range": "0-2",
            "description": f"{active_esc} active escalation(s)",
        }
    )

    # Queue age factor
    oldest = queue.get("oldest_minutes", 0.0)
    if oldest < 60:
        age_score = 100
        age_status = "optimal"
    elif oldest < 120:
        age_score = 75
        age_status = "acceptable"
    elif oldest < 240:
        age_score = 50
        age_status = "warning"
    else:
        age_score = 25
        age_status = "critical"

    factors.append(
        {
            "name": "queue_age",
            "score": age_score,
            "status": age_status,
            "value": oldest,
            "target_range": "<60 minutes",
            "description": f"Oldest pending task: {oldest:.0f} minutes",
        }
    )

    # Build per-project health if in multi-project mode
    per_project = []
    breakdown = company_progress.get("breakdown", [])
    if breakdown:
        for proj in breakdown:
            proj_id = proj.get("project_id", "unknown")
            proj_total = proj.get("total_tasks", 0)
            proj_blocked = proj.get("blocked", 0)
            proj_completed = proj.get("completed", 0)

            # Simple health score for project
            if proj_total > 0:
                proj_blocked_ratio = (proj_blocked / proj_total) * 100
                proj_completion_pct = (proj_completed / proj_total) * 100

                if proj_blocked_ratio < 10 and proj_completion_pct > 0:
                    proj_health = "healthy"
                    proj_score = 80 + min(20, proj_completion_pct / 5)
                elif proj_blocked_ratio < 20:
                    proj_health = "warning"
                    proj_score = 60 + min(20, proj_completion_pct / 5)
                else:
                    proj_health = "critical"
                    proj_score = max(20, 40 - proj_blocked_ratio)
            else:
                proj_health = "unknown"
                proj_score = 50

            per_project.append(
                {
                    "project_id": proj_id,
                    "project_name": proj.get("project_name", proj_id),
                    "health_score": round(proj_score, 1),
                    "health_status": proj_health,
                    "blocked_ratio": round(proj_blocked / proj_total * 100, 1)
                    if proj_total > 0
                    else 0.0,
                    "completion_percentage": proj.get("completion_percentage", 0.0),
                }
            )

    result = {
        "success": True,
        "health_score": health_score,
        "health_status": health_status,
        "factors": factors,
        "factor_summary": {
            "optimal_count": sum(1 for f in factors if f["status"] == "optimal"),
            "acceptable_count": sum(1 for f in factors if f["status"] == "acceptable"),
            "warning_count": sum(1 for f in factors if f["status"] == "warning"),
            "critical_count": sum(1 for f in factors if f["status"] == "critical"),
        },
        "generated_at": now.isoformat(),
    }

    if project_id:
        result["project_id"] = project_id

    if per_project:
        result["per_project"] = per_project
        result["multi_project_mode"] = True
    else:
        result["multi_project_mode"] = False

    return result


def aggregate_progress(project_id: str | None = None) -> dict:
    """
    Aggregate task completion, velocity, and delivery forecast.

    Args:
        project_id: Optional project ID for project-specific progress

    Returns:
        Dict with:
        - completion: Task counts by status
        - velocity: Daily velocity metrics
        - forecast: Estimated completion date
        - per_project: Project-level progress (multi-project mode)
    """
    now = datetime.now(timezone.utc)
    _ = now.strftime("%Y-%m-%d")  # Reserved for future use

    # Get progress data
    company_progress = _safe_get_company_progress(include_breakdown=True)
    task_metrics = _safe_calculate_task_metrics(project_id)
    queue_metrics = _safe_calculate_queue_metrics()

    # Extract completion data
    summary = company_progress.get("summary", {})
    percentages = company_progress.get("percentages", {})

    completion = {
        "total": summary.get("total_tasks", 0),
        "completed": summary.get("completed", 0),
        "in_progress": summary.get("in_progress", 0),
        "blocked": summary.get("blocked", 0),
        "pending": summary.get("pending", 0),
        "completion_percentage": percentages.get("completed", 0.0),
        "completed_today": task_metrics.get("completed_today", 0),
    }

    # Velocity data
    velocity_daily = task_metrics.get("velocity_daily", 0.0)
    velocity = {
        "daily_average": velocity_daily,
        "completed_today": task_metrics.get("completed_today", 0),
        "average_duration_minutes": task_metrics.get("average_duration_minutes", 0.0),
    }

    # Calculate delivery forecast
    remaining = (
        queue_metrics.get("pending_count", 0)
        + queue_metrics.get("in_progress_count", 0)
        + queue_metrics.get("blocked_count", 0)
    )

    if velocity_daily > 0 and remaining > 0:
        days_to_completion = remaining / velocity_daily
        estimated_date = now + timedelta(days=days_to_completion)
        forecast = {
            "remaining_tasks": remaining,
            "velocity_daily": velocity_daily,
            "estimated_days": round(days_to_completion, 1),
            "estimated_date": estimated_date.strftime("%Y-%m-%d"),
            "confidence": "high"
            if velocity_daily >= 1.0
            else "medium"
            if velocity_daily >= 0.5
            else "low",
            "can_estimate": True,
        }
    else:
        forecast = {
            "remaining_tasks": remaining,
            "velocity_daily": velocity_daily,
            "estimated_days": None,
            "estimated_date": None,
            "confidence": "none",
            "can_estimate": False,
            "reason": "No velocity data"
            if velocity_daily <= 0
            else "No remaining tasks",
        }

    # Build per-project progress if in multi-project mode
    per_project = []
    breakdown = company_progress.get("breakdown", [])
    if breakdown:
        for proj in breakdown:
            proj_id = proj.get("project_id", "unknown")
            proj_total = proj.get("total_tasks", 0)
            proj_completed = proj.get("completed", 0)
            proj_in_progress = proj.get("in_progress", 0)
            proj_blocked = proj.get("blocked", 0)
            proj_pending = proj.get("pending", 0)

            proj_remaining = proj_in_progress + proj_blocked + proj_pending

            per_project.append(
                {
                    "project_id": proj_id,
                    "project_name": proj.get("project_name", proj_id),
                    "completion": {
                        "total": proj_total,
                        "completed": proj_completed,
                        "remaining": proj_remaining,
                        "completion_percentage": proj.get("completion_percentage", 0.0),
                    },
                }
            )

    result = {
        "success": True,
        "completion": completion,
        "velocity": velocity,
        "forecast": forecast,
        "generated_at": now.isoformat(),
    }

    if project_id:
        result["project_id"] = project_id

    if per_project:
        result["per_project"] = per_project
        result["multi_project_mode"] = True
    else:
        result["multi_project_mode"] = False

    return result


def aggregate_workforce(project_id: str | None = None) -> dict:
    """
    Aggregate agent status, utilization, and department breakdown.

    Args:
        project_id: Optional project ID for project-specific workforce data

    Returns:
        Dict with:
        - agents: Agent counts by status
        - utilization: Utilization metrics
        - departments: Department-level breakdown
    """
    now = datetime.now(timezone.utc)

    # Get workforce data
    agent_metrics = _safe_calculate_agent_metrics()
    org = _safe_load_org()

    # Agent status breakdown
    agents = {
        "total": agent_metrics.get("total_agents", 0),
        "active": agent_metrics.get("active_count", 0),
        "idle": agent_metrics.get("idle_count", 0),
        "blocked": agent_metrics.get("blocked_count", 0),
    }

    # Utilization metrics
    utilization = {
        "percentage": agent_metrics.get("utilization_percent", 0.0),
        "status": (
            "optimal"
            if 60 <= agent_metrics.get("utilization_percent", 0) <= 80
            else "low"
            if agent_metrics.get("utilization_percent", 0) < 60
            else "high"
        ),
        "target_range": "60-80%",
    }

    # Department breakdown
    departments = []
    org_departments = org.get("departments", [])
    org_agents = org.get("employees", []) or org.get("agents", [])

    for dept in org_departments:
        dept_id = dept.get("id", "unknown")
        dept_name = dept.get("name", dept_id)

        # Count agents in this department
        dept_agents = [
            a
            for a in org_agents
            if a.get("department") == dept_id or dept_id in a.get("departments", [])
        ]
        total_in_dept = len(dept_agents)
        active_in_dept = len([a for a in dept_agents if a.get("status") == "active"])

        departments.append(
            {
                "department_id": dept_id,
                "department_name": dept_name,
                "total_agents": total_in_dept,
                "active_agents": active_in_dept,
                "utilization": round((active_in_dept / total_in_dept) * 100, 1)
                if total_in_dept > 0
                else 0.0,
            }
        )

    result = {
        "success": True,
        "agents": agents,
        "utilization": utilization,
        "departments": departments,
        "company_name": org.get("company", {}).get("name", "Unknown"),
        "generated_at": now.isoformat(),
    }

    if project_id:
        result["project_id"] = project_id

    return result


def aggregate_risks() -> list[dict]:
    """
    Identify risks with severity levels.

    Analyzes current state to detect:
    - High blocked ratio (>40% CRITICAL, >20% WARNING)
    - Low health score (<60 CRITICAL, <80 WARNING)
    - Stalled tasks (WARNING, count-based)
    - Velocity drops (>50% WARNING)
    - Active escalations (>5 CRITICAL, >2 WARNING)

    Returns:
        List of risk dicts with:
        - risk_id: Unique identifier
        - severity: "CRITICAL" or "WARNING"
        - category: Risk category
        - title: Short description
        - description: Detailed description
        - recommendation: Suggested action
    """
    now = datetime.now(timezone.utc)
    risks = []
    risk_counter = 0

    # Get data for risk assessment
    company_progress = _safe_get_company_progress()
    health_summary = _safe_get_health_summary()
    stalled_work = _safe_detect_stalled_work()
    escalation_metrics = _safe_calculate_escalation_metrics()
    task_metrics = _safe_calculate_task_metrics()

    # Check blocked ratio
    blocked_ratio = company_progress.get("health", {}).get("blocked_ratio", 0.0)
    if blocked_ratio > 40:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "CRITICAL",
                "category": "blocked_tasks",
                "title": "Critical blocked task ratio",
                "description": f"Blocked task ratio is {blocked_ratio:.1f}%, which is above the critical threshold of 40%.",
                "value": blocked_ratio,
                "threshold": 40,
                "recommendation": "Immediately review and unblock high-priority tasks. Consider escalating blockers.",
                "detected_at": now.isoformat(),
            }
        )
    elif blocked_ratio > 20:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "WARNING",
                "category": "blocked_tasks",
                "title": "Elevated blocked task ratio",
                "description": f"Blocked task ratio is {blocked_ratio:.1f}%, which is above the warning threshold of 20%.",
                "value": blocked_ratio,
                "threshold": 20,
                "recommendation": "Review blocked tasks and prioritize unblocking them before the situation worsens.",
                "detected_at": now.isoformat(),
            }
        )

    # Check health score
    health_score = health_summary.get("health_score", 50)
    if health_score < 60:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "CRITICAL",
                "category": "health",
                "title": "Critical health score",
                "description": f"Overall health score is {health_score}, which is below the critical threshold of 60.",
                "value": health_score,
                "threshold": 60,
                "recommendation": "Conduct immediate health review. Address the lowest-scoring factors first.",
                "detected_at": now.isoformat(),
            }
        )
    elif health_score < 80:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "WARNING",
                "category": "health",
                "title": "Suboptimal health score",
                "description": f"Overall health score is {health_score}, which is below the warning threshold of 80.",
                "value": health_score,
                "threshold": 80,
                "recommendation": "Review health factors and implement improvements to prevent degradation.",
                "detected_at": now.isoformat(),
            }
        )

    # Check stalled tasks
    stalled_count = stalled_work.get("total_stalled", 0)
    if stalled_count > 0:
        # Severity based on count
        if stalled_count >= 5:
            severity = "CRITICAL"
        elif stalled_count >= 3:
            severity = "WARNING"
        else:
            severity = "WARNING"

        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": severity,
                "category": "stalled_work",
                "title": f"{stalled_count} stalled task(s) detected",
                "description": f"Found {stalled_count} task(s) with no recent progress. This may indicate blockers or resource issues.",
                "value": stalled_count,
                "threshold": 0,
                "recommendation": "Check on assigned agents and identify root causes. Consider reassignment if needed.",
                "detected_at": now.isoformat(),
                "stalled_tasks": stalled_work.get("stalled_in_progress", [])[
                    :5
                ],  # Limit to top 5
            }
        )

    # Check active escalations
    active_escalations = escalation_metrics.get("active_escalations", 0)
    if active_escalations > 5:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "CRITICAL",
                "category": "escalations",
                "title": "High number of active escalations",
                "description": f"There are {active_escalations} active escalations, which is above the critical threshold of 5.",
                "value": active_escalations,
                "threshold": 5,
                "recommendation": "Prioritize escalation resolution. Consider assigning dedicated resources.",
                "detected_at": now.isoformat(),
            }
        )
    elif active_escalations > 2:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "WARNING",
                "category": "escalations",
                "title": "Elevated escalation count",
                "description": f"There are {active_escalations} active escalations, which is above the warning threshold of 2.",
                "value": active_escalations,
                "threshold": 2,
                "recommendation": "Review active escalations and ensure they are being addressed promptly.",
                "detected_at": now.isoformat(),
            }
        )

    # Check velocity (we don't have historical velocity, but we can flag zero velocity)
    velocity_daily = task_metrics.get("velocity_daily", 0.0)
    if velocity_daily == 0:
        risk_counter += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_counter:03d}",
                "severity": "WARNING",
                "category": "velocity",
                "title": "Zero velocity detected",
                "description": "No tasks have been completed recently, resulting in zero velocity.",
                "value": velocity_daily,
                "threshold": 0,
                "recommendation": "Investigate why no tasks are being completed. Check for systemic blockers.",
                "detected_at": now.isoformat(),
            }
        )

    # Sort by severity (CRITICAL first)
    severity_order = {"CRITICAL": 0, "WARNING": 1}
    risks.sort(key=lambda r: severity_order.get(r["severity"], 2))

    return risks


def aggregate_autonomy_metrics() -> dict:
    """
    Aggregate P25 autonomy metrics from adaptive scheduler, budget governor,
    learning loop, session continuity, and approval learner modules.

    P25 Task 25.8: Expose P25 autonomy metrics in dashboard.

    Returns:
        Dict with:
        - success: bool
        - scheduler: Schedule mode, poll interval, batch size
        - budget: Throttle level, budget remaining percentage
        - learning: Insights summary (failure patterns, best employees)
        - session: Last snapshot time, session uptime
        - approval: Auto-approve rate, always-human count
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    # Initialize result with defaults for disabled/unavailable state
    result: dict[str, Any] = {
        "success": True,
        "enabled": True,
        "scheduler": {
            "mode": "disabled",
            "mode_description": "P25 adaptive scheduler not available",
            "poll_interval_seconds": 30,
            "batch_size": 3,
            "queue_depth": 0,
        },
        "budget": {
            "throttle_level": "disabled",
            "throttle_description": "P25 budget governor not available",
            "daily_utilization_percent": 0.0,
            "remaining_daily_budget": 0,
            "monthly_utilization_percent": 0.0,
        },
        "learning": {
            "status": "disabled",
            "failure_pattern_count": 0,
            "best_employees_available": False,
            "plan_score_correlation": 0.0,
            "total_outcomes": 0,
        },
        "session": {
            "status": "disabled",
            "last_snapshot": None,
            "uptime_seconds": 0,
            "uptime_formatted": "N/A",
            "tasks_completed_this_session": 0,
        },
        "approval": {
            "status": "disabled",
            "auto_approve_rate": 0.0,
            "always_human_count": 0,
            "total_decisions": 0,
        },
        "generated_at": now.isoformat(),
    }

    # Try to import and use adaptive_scheduler
    try:
        from adaptive_scheduler import AdaptiveScheduler

        scheduler = AdaptiveScheduler()
        scheduler.compute_mode()
        status = scheduler.get_status_dict()

        result["scheduler"] = {
            "mode": status.get("current_mode", "NORMAL"),
            "mode_description": status.get("mode_description", ""),
            "poll_interval_seconds": status.get("strategy", {}).get(
                "poll_interval_seconds", 30
            ),
            "batch_size": status.get("strategy", {}).get("batch_size", 3),
            "queue_depth": status.get("context", {}).get("queue_depth", 0),
            "mode_since": status.get("mode_since", ""),
            "transitions_count": status.get("transitions_count", 0),
        }
    except ImportError:
        result["scheduler"]["status"] = "module_not_available"
    except Exception as e:
        result["scheduler"]["status"] = "error"
        result["scheduler"]["error"] = str(e)

    # Try to import and use budget_governor
    try:
        from budget_governor import BudgetGovernor

        governor = BudgetGovernor()
        status = governor.get_status_dict()

        throttle_level = status.get("throttle_level", "normal")
        throttle_descriptions = {
            "normal": "All tasks allowed",
            "cautious": "Skip low-priority, defer complex",
            "minimal": "Only critical/urgent tasks",
            "paused": "No tasks until next budget period",
        }

        result["budget"] = {
            "throttle_level": throttle_level.upper(),
            "throttle_description": throttle_descriptions.get(
                throttle_level, "Unknown"
            ),
            "daily_utilization_percent": round(
                status.get("daily_utilization_percent", 0.0), 1
            ),
            "remaining_daily_budget": status.get("remaining_daily_budget", 0),
            "monthly_utilization_percent": round(
                status.get("utilization_percent", 0.0), 1
            ),
            "remaining_budget": status.get("remaining_budget", 0),
        }
    except ImportError:
        result["budget"]["status"] = "module_not_available"
    except Exception as e:
        result["budget"]["status"] = "error"
        result["budget"]["error"] = str(e)

    # Try to import and use learning_loop
    try:
        from learning_loop import compute_insights, load_outcomes

        outcomes = load_outcomes()
        insights = compute_insights(outcomes)

        # Count best employees across all complexity levels
        best_employees_available = any(
            len(employees) > 0
            for employees in insights.best_employees_by_complexity.values()
        )

        # Get top 3 employees by overall performance
        top_performers = []
        if insights.employee_performance:
            sorted_employees = sorted(
                insights.employee_performance.items(),
                key=lambda x: x[1].get("efficiency_score", 0),
                reverse=True,
            )[:3]
            top_performers = [
                {
                    "employee_id": emp_id,
                    "success_rate": round(metrics.get("success_rate", 0) * 100, 1),
                    "total_tasks": metrics.get("total_tasks", 0),
                }
                for emp_id, metrics in sorted_employees
            ]

        result["learning"] = {
            "status": "active",
            "failure_pattern_count": len(insights.failure_patterns),
            "best_employees_available": best_employees_available,
            "plan_score_correlation": round(insights.plan_score_success_correlation, 2),
            "total_outcomes": len(outcomes),
            "top_performers": top_performers,
            "optimal_time_windows": [
                f"{s}:00-{e}:00" for s, e in insights.optimal_time_windows
            ],
        }
    except ImportError:
        result["learning"]["status"] = "module_not_available"
    except Exception as e:
        result["learning"]["status"] = "error"
        result["learning"]["error"] = str(e)

    # Try to import and use session_continuity
    try:
        from session_continuity import SessionContinuity

        continuity = SessionContinuity()
        snapshot_info = continuity.get_latest_snapshot_info()

        if snapshot_info:
            # Calculate uptime from captured_at
            captured_at_str = snapshot_info.get("captured_at", "")
            uptime_seconds = 0
            if captured_at_str:
                try:
                    captured_at = datetime.fromisoformat(captured_at_str)
                    if captured_at.tzinfo is None:
                        captured_at = captured_at.replace(tzinfo=timezone.utc)
                    uptime_seconds = int((now - captured_at).total_seconds())
                except (ValueError, TypeError):
                    pass

            result["session"] = {
                "status": "active",
                "last_snapshot": snapshot_info.get("captured_at", ""),
                "snapshot_reason": snapshot_info.get("capture_reason", ""),
                "uptime_seconds": uptime_seconds,
                "uptime_formatted": _format_uptime(uptime_seconds),
                "tasks_completed_this_session": snapshot_info.get("tasks_completed", 0),
                "in_progress_count": snapshot_info.get("in_progress_count", 0),
            }
        else:
            result["session"] = {
                "status": "no_snapshot",
                "last_snapshot": None,
                "uptime_seconds": 0,
                "uptime_formatted": "N/A",
                "tasks_completed_this_session": 0,
            }
    except ImportError:
        result["session"]["status"] = "module_not_available"
    except Exception as e:
        result["session"]["status"] = "error"
        result["session"]["error"] = str(e)

    # Try to import and use approval_learner
    try:
        from approval_learner import get_learning_summary, load_decisions

        summary = get_learning_summary()

        auto_approve_types = summary.get("auto_approve_types", [])
        always_human_types = summary.get("always_human_types", [])

        # WS-119: Count actual decisions, not type-set ratio. Previously this
        # divided len(auto_approve_types) by len(auto_approve_types + always_human_types),
        # which produced misleadingly low numbers like 10.5% even when 100% of
        # real decisions were auto-approved (because ALWAYS_HUMAN_TYPES contains
        # 17 mostly-fictional categories with zero historical decisions).
        decisions = load_decisions()
        if decisions:
            auto_approved_count = sum(
                1
                for d in decisions
                if d.task_type in auto_approve_types and d.was_approved
            )
            auto_approve_rate = round(auto_approved_count / len(decisions) * 100, 1)
        else:
            auto_approve_rate = 0.0

        result["approval"] = {
            "status": "active",
            "auto_approve_rate": auto_approve_rate,
            "auto_approve_types_count": len(auto_approve_types),
            "always_human_count": len(always_human_types),
            "total_decisions": summary.get("total_decisions", 0),
            "overall_success_rate": round(
                summary.get("overall_success_rate", 0) * 100, 1
            ),
            "graduation_candidates": len(summary.get("graduation_candidates", [])),
            "demotion_candidates": len(summary.get("demotion_candidates", [])),
        }
    except ImportError:
        result["approval"]["status"] = "module_not_available"
    except Exception as e:
        result["approval"]["status"] = "error"
        result["approval"]["error"] = str(e)

    # Determine overall P25 status
    statuses = [
        result["scheduler"].get("status", result["scheduler"].get("mode", "active")),
        result["budget"].get(
            "status", result["budget"].get("throttle_level", "active")
        ),
        result["learning"].get("status", "unknown"),
        result["session"].get("status", "unknown"),
        result["approval"].get("status", "unknown"),
    ]

    active_count = sum(
        1
        for s in statuses
        if s
        in (
            "active",
            "AGGRESSIVE",
            "NORMAL",
            "CONSERVATIVE",
            "IDLE",
            "normal",
            "cautious",
            "minimal",
            "paused",
            "NORMAL",
            "CAUTIOUS",
            "MINIMAL",
            "PAUSED",
            "no_snapshot",
        )
    )
    error_count = sum(1 for s in statuses if s == "error")

    if error_count > 2:
        result["enabled"] = False
        result["overall_status"] = "error"
    elif active_count >= 3:
        result["overall_status"] = "active"
    elif active_count >= 1:
        result["overall_status"] = "partial"
    else:
        result["enabled"] = False
        result["overall_status"] = "disabled"

    return result


def aggregate_efficiency() -> dict:
    """
    Aggregate efficiency metrics from the efficiency tracker.

    Returns:
        Dict with:
        - company_score: Overall company efficiency score
        - target_score: Target efficiency score
        - status: "on_target", "near_target", "below_target", or "critical"
        - top_performers: List of most efficient employees
        - patterns_discovered: Count of discovered patterns
        - optimizations_applied: Count of applied optimizations
        - memory_hit_rate: Context reuse effectiveness
        - recommendations: Top efficiency recommendations
    """
    now = datetime.now(timezone.utc)

    # Try to import efficiency_tracker
    try:
        from efficiency_tracker import (
            get_efficiency_report,
            get_memory_hit_rate,
        )

        EFFICIENCY_TRACKER_AVAILABLE = True
    except ImportError:
        EFFICIENCY_TRACKER_AVAILABLE = False

    if not EFFICIENCY_TRACKER_AVAILABLE:
        return {
            "success": False,
            "company_score": 0.0,
            "target_score": 0.90,
            "status": "unknown",
            "top_performers": [],
            "patterns_discovered": 0,
            "optimizations_applied": 0,
            "memory_hit_rate": 0.0,
            "recommendations": [],
            "message": "Efficiency tracker not available",
            "generated_at": now.isoformat(),
        }

    try:
        # Get efficiency report
        report = get_efficiency_report()
        memory_stats = get_memory_hit_rate()

        company_efficiency = report.get("company_efficiency", {})
        score = company_efficiency.get("score", 0.0)
        target = company_efficiency.get("target", 0.90)

        # Determine status
        if score >= target:
            status = "on_target"
        elif score >= target * 0.9:
            status = "near_target"
        elif score >= target * 0.8:
            status = "below_target"
        else:
            status = "critical"

        # Get top performers
        employee_breakdown = report.get("employee_breakdown", [])
        top_performers = [
            {
                "employee_id": emp.get("employee_id"),
                "name": emp.get("name"),
                "score": emp.get("score", 0.0),
                "tasks_completed": emp.get("tasks_completed", 0),
                "trend": emp.get("trend", "stable"),
            }
            for emp in employee_breakdown[:5]
        ]

        # Get learning stats
        learning = report.get("learning", {})

        return {
            "success": True,
            "company_score": score,
            "target_score": target,
            "status": status,
            "gap": round(target - score, 2),
            "top_performers": top_performers,
            "patterns_discovered": learning.get("patterns_discovered", 0),
            "optimizations_applied": learning.get("optimizations_applied", 0),
            "memory_hit_rate": memory_stats.get("overall_hit_rate", 0.0),
            "memory_savings": memory_stats.get("estimated_savings", "N/A"),
            "recommendations": report.get("recommendations", [])[:3],
            "generated_at": now.isoformat(),
        }

    except Exception as e:
        return {
            "success": False,
            "company_score": 0.0,
            "target_score": 0.90,
            "status": "error",
            "top_performers": [],
            "patterns_discovered": 0,
            "optimizations_applied": 0,
            "memory_hit_rate": 0.0,
            "recommendations": [],
            "error": str(e),
            "generated_at": now.isoformat(),
        }


def _load_daemon_heartbeat() -> dict:
    """Load daemon heartbeat file safely."""
    try:
        from pathlib import Path

        # Try to find company directory
        company_dir = Path(".company")

        heartbeat_path = company_dir / "runtime/daemon.heartbeat"
        if not heartbeat_path.exists():
            return {}

        with open(heartbeat_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def _load_planning_history() -> dict:
    """
    Load planning history for aggregate metrics.

    This file tracks checker results over time for calculating:
    - Average plan scores
    - Revision rates

    If the file doesn't exist, returns default structure.
    """
    try:
        from pathlib import Path

        company_dir = Path(".company")
        history_path = company_dir / "planning_history.json"

        if not history_path.exists():
            return {
                "schema_version": "1.0",
                "check_results": [],
                "totals": {
                    "total_checks": 0,
                    "total_score": 0,
                    "pass_count": 0,
                    "revise_count": 0,
                    "escalate_count": 0,
                },
            }

        with open(history_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {
            "schema_version": "1.0",
            "check_results": [],
            "totals": {
                "total_checks": 0,
                "total_score": 0,
                "pass_count": 0,
                "revise_count": 0,
                "escalate_count": 0,
            },
        }


def aggregate_daemon_metrics() -> dict:
    """
    Aggregate daemon metrics including planning statistics.

    P20 Task 20.7: Expose planning metrics in dashboard.

    Returns:
        Dict with:
        - success: bool
        - daemon_status: "running", "stopped", or "unknown"
        - uptime_seconds: Current daemon uptime
        - tasks_completed: Tasks completed this session
        - tasks_failed: Tasks failed this session
        - planning: Planning-specific metrics
            - planning_ratio: planned_tasks / total_tasks (percentage)
            - avg_plan_score: average checker score (0-25 scale)
            - revision_rate: revisions / planned_tasks (percentage)
            - tasks_planned: Count of tasks that went through planning
            - tasks_direct: Count of tasks executed directly (trivial)
            - planning_failures: Count of plan creation/validation failures
        - roadmap: Roadmap scheduling metrics
        - circuit_breaker_state: Current circuit breaker state
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    # Load daemon heartbeat
    heartbeat = _load_daemon_heartbeat()

    if not heartbeat:
        return {
            "success": False,
            "daemon_status": "unknown",
            "uptime_seconds": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "planning": {
                "planning_ratio": "N/A",
                "avg_plan_score": "N/A",
                "revision_rate": "N/A",
                "tasks_planned": 0,
                "tasks_direct": 0,
                "planning_failures": 0,
            },
            "roadmap": {
                "tasks_scheduled": 0,
                "tasks_completed": 0,
                "current_wave": 1,
            },
            "circuit_breaker_state": "unknown",
            "message": "Daemon heartbeat not available",
            "generated_at": now.isoformat(),
        }

    # Extract planning metrics from heartbeat
    tasks_planned = heartbeat.get("tasks_planned", 0)
    tasks_direct = heartbeat.get("tasks_direct", 0)
    planning_failures = heartbeat.get("planning_failures", 0)
    total_tasks = tasks_planned + tasks_direct

    # Calculate planning ratio
    if total_tasks > 0:
        planning_ratio = round((tasks_planned / total_tasks) * 100, 1)
    else:
        planning_ratio = "N/A"

    # Load planning history for avg score and revision rate
    planning_history = _load_planning_history()
    totals = planning_history.get("totals", {})

    total_checks = totals.get("total_checks", 0)
    total_score = totals.get("total_score", 0)
    revise_count = totals.get("revise_count", 0)

    # Calculate average plan score (0-25 scale)
    if total_checks > 0:
        avg_plan_score = round(total_score / total_checks, 1)
    else:
        avg_plan_score = "N/A"

    # Calculate revision rate
    if total_checks > 0:
        revision_rate = round((revise_count / total_checks) * 100, 1)
    else:
        revision_rate = "N/A"

    # Extract other daemon metrics
    uptime_seconds = heartbeat.get("uptime_seconds", 0)
    tasks_completed = heartbeat.get("tasks_completed_this_session", 0)
    tasks_failed = heartbeat.get("tasks_failed_this_session", 0)
    circuit_breaker_state = heartbeat.get("circuit_breaker_state", "unknown")
    daemon_status = heartbeat.get("status", "unknown")

    # Roadmap metrics
    roadmap_tasks_scheduled = heartbeat.get("roadmap_tasks_scheduled", 0)
    roadmap_tasks_completed = heartbeat.get("roadmap_tasks_completed", 0)
    roadmap_current_wave = heartbeat.get("roadmap_current_wave", 1)

    return {
        "success": True,
        "daemon_status": daemon_status,
        "uptime_seconds": uptime_seconds,
        "uptime_formatted": _format_uptime(uptime_seconds),
        "tasks_completed": tasks_completed,
        "tasks_failed": tasks_failed,
        "success_rate": round(
            (tasks_completed / (tasks_completed + tasks_failed)) * 100, 1
        )
        if (tasks_completed + tasks_failed) > 0
        else 100.0,
        "planning": {
            "planning_ratio": planning_ratio,
            "avg_plan_score": avg_plan_score,
            "revision_rate": revision_rate,
            "tasks_planned": tasks_planned,
            "tasks_direct": tasks_direct,
            "planning_failures": planning_failures,
            # Display-friendly format for dashboard table
            "display": {
                "Tasks Planned": f"{planning_ratio}%"
                if isinstance(planning_ratio, (int, float))
                else planning_ratio,
                "Avg Plan Score": f"{avg_plan_score}/25"
                if isinstance(avg_plan_score, (int, float))
                else avg_plan_score,
                "Revision Rate": f"{revision_rate}%"
                if isinstance(revision_rate, (int, float))
                else revision_rate,
            },
        },
        "roadmap": {
            "tasks_scheduled": roadmap_tasks_scheduled,
            "tasks_completed": roadmap_tasks_completed,
            "current_wave": roadmap_current_wave,
        },
        "circuit_breaker_state": circuit_breaker_state,
        "last_heartbeat": heartbeat.get("last_heartbeat", ""),
        "generated_at": now.isoformat(),
    }


def _format_uptime(seconds: int) -> str:
    """Format uptime seconds as human-readable string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"


def aggregate_orchestrator_metrics() -> dict:
    """
    Aggregate orchestrator routing metrics for dashboard display.

    P28.2 Task 28.2.7: Expose orchestrator metrics in dashboard.

    Returns:
        Dict with:
        - success: bool
        - tasks_routed: Total tasks routed through orchestrator
        - complexity_distribution: Breakdown by complexity level
        - execution_mode_distribution: Breakdown by execution mode
        - pipeline_stage_counts: How many tasks reached each stage
        - gate_metrics: Gate pass rate and attempts
        - performance: Routing time statistics
        - insights: Dominant patterns and daily velocity
        - health_issues: Any orchestrator-related health concerns
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    # Try to load orchestrator metrics
    get_metrics_summary = _lazy_load_orchestrator_metrics()

    if get_metrics_summary is None:
        return {
            "success": False,
            "enabled": False,
            "tasks_routed": 0,
            "complexity_distribution": {},
            "execution_mode_distribution": {},
            "pipeline_stage_counts": {},
            "gate_metrics": {
                "attempts": 0,
                "passes": 0,
                "pass_rate": 100.0,
            },
            "performance": {
                "avg_routing_time_ms": 0.0,
                "routing_samples": 0,
            },
            "insights": {},
            "health_issues": [],
            "message": "Orchestrator metrics module not available",
            "generated_at": now.isoformat(),
        }

    try:
        # Get metrics summary from orchestrator_metrics module
        metrics = get_metrics_summary()

        if not metrics.get("success", False):
            return {
                "success": False,
                "enabled": True,
                "error": metrics.get("error", "Unknown error"),
                "generated_at": now.isoformat(),
            }

        # Extract complexity distribution with percentages
        by_complexity = metrics.get("by_complexity", {})
        tasks_routed = metrics.get("tasks_routed", 0)

        complexity_distribution = {}
        for level, count in by_complexity.items():
            pct = round((count / tasks_routed) * 100, 1) if tasks_routed > 0 else 0.0
            complexity_distribution[level] = {
                "count": count,
                "percentage": pct,
            }

        # Extract execution mode distribution with percentages
        by_mode = metrics.get("by_execution_mode", {})
        execution_mode_distribution = {}
        for mode, count in by_mode.items():
            pct = round((count / tasks_routed) * 100, 1) if tasks_routed > 0 else 0.0
            execution_mode_distribution[mode] = {
                "count": count,
                "percentage": pct,
            }

        # Extract gate metrics
        gate_metrics = metrics.get("gate_metrics", {})

        # Extract performance metrics
        performance = metrics.get("performance", {})

        # Extract insights
        insights = metrics.get("insights", {})

        # Identify health issues
        health_issues = []

        # Low gate pass rate is concerning
        gate_pass_rate = gate_metrics.get("pass_rate", 100.0)
        if gate_metrics.get("attempts", 0) > 0 and gate_pass_rate < 80:
            health_issues.append(
                {
                    "issue": "low_gate_pass_rate",
                    "severity": "WARNING" if gate_pass_rate >= 50 else "CRITICAL",
                    "value": gate_pass_rate,
                    "threshold": 80,
                    "message": f"Gate pass rate is {gate_pass_rate}% (target: ≥80%)",
                }
            )

        # High routing time is concerning
        avg_routing_time = performance.get("avg_routing_time_ms", 0.0)
        if performance.get("routing_samples", 0) > 10 and avg_routing_time > 100:
            health_issues.append(
                {
                    "issue": "slow_routing",
                    "severity": "WARNING",
                    "value": avg_routing_time,
                    "threshold": 100,
                    "message": f"Average routing time is {avg_routing_time}ms (target: <100ms)",
                }
            )

        # Too many complex/epic tasks might indicate scope creep
        complex_count = by_complexity.get("complex", 0) + by_complexity.get("epic", 0)
        simple_count = by_complexity.get("trivial", 0) + by_complexity.get(
            "standard", 0
        )
        if tasks_routed >= 10 and complex_count > simple_count:
            health_issues.append(
                {
                    "issue": "high_complexity_ratio",
                    "severity": "WARNING",
                    "value": round(complex_count / tasks_routed * 100, 1),
                    "message": f"Complex/epic tasks ({complex_count}) exceed trivial/standard ({simple_count})",
                }
            )

        return {
            "success": True,
            "enabled": True,
            "tasks_routed": tasks_routed,
            "complexity_distribution": complexity_distribution,
            "execution_mode_distribution": execution_mode_distribution,
            "pipeline_stage_counts": metrics.get("pipeline_stage_counts", {}),
            "gate_metrics": {
                "attempts": gate_metrics.get("attempts", 0),
                "passes": gate_metrics.get("passes", 0),
                "pass_rate": gate_pass_rate,
            },
            "performance": {
                "avg_routing_time_ms": avg_routing_time,
                "routing_samples": performance.get("routing_samples", 0),
                "status": "optimal"
                if avg_routing_time < 50
                else "acceptable"
                if avg_routing_time < 100
                else "slow",
            },
            "insights": {
                "dominant_complexity": insights.get("dominant_complexity", "standard"),
                "dominant_execution_mode": insights.get(
                    "dominant_execution_mode", "plan_execute"
                ),
                "daily_velocity": insights.get("daily_velocity", 0.0),
            },
            "health_issues": health_issues,
            "health_issues_count": len(health_issues),
            "generated_at": now.isoformat(),
        }

    except Exception as e:
        return {
            "success": False,
            "enabled": True,
            "error": str(e),
            "generated_at": now.isoformat(),
        }


def _get_parallel_status() -> dict:
    """
    Get parallel execution status for dashboard display.

    Returns:
        Dict with parallel execution metrics
    """
    try:
        from parallel_executor import (
            get_parallel_status,
            get_pending_questions,
            is_parallel_enabled,
        )

        if not is_parallel_enabled():
            return {"enabled": False}

        status = get_parallel_status()
        questions = get_pending_questions()

        return {
            "enabled": True,
            "active_workers": status.get("active_workers", 0),
            "max_workers": status.get("max_workers", 3),
            "workers": status.get("workers", []),
            "plans": status.get("plans", {}),
            "questions_pending": len(questions),
            "questions": questions[:5],  # Limit to 5 for display
        }
    except ImportError:
        return {"enabled": False, "error": "parallel_executor not available"}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def get_health_insights() -> list[dict]:
    """
    Get health insights including orchestrator-related issues.

    P28.2 Task 28.2.7: Flag orchestrator health issues in dashboard.

    Returns:
        List of health insight dicts with severity and recommendations.
    """
    insights = []

    # Get orchestrator health issues
    orchestrator_metrics = aggregate_orchestrator_metrics()
    if orchestrator_metrics.get("success", False):
        for issue in orchestrator_metrics.get("health_issues", []):
            insights.append(
                {
                    "source": "orchestrator",
                    "issue": issue.get("issue", "unknown"),
                    "severity": issue.get("severity", "WARNING"),
                    "message": issue.get("message", ""),
                    "recommendation": _get_orchestrator_recommendation(
                        issue.get("issue", "")
                    ),
                }
            )

    return insights


def _get_orchestrator_recommendation(issue: str) -> str:
    """Get recommendation for an orchestrator issue."""
    recommendations = {
        "low_gate_pass_rate": "Review failed gates to identify common blockers. Consider adjusting gate criteria or improving plan quality.",
        "slow_routing": "Check orchestrator performance. Consider caching complexity analysis results.",
        "high_complexity_ratio": "Review task breakdown practices. Large tasks should be split into smaller, manageable pieces.",
    }
    return recommendations.get(issue, "Review orchestrator metrics for details.")


# -----------------------------------------------------------------------------
# Multi-Project Unified Dashboard Functions (Task 14.10)
# -----------------------------------------------------------------------------


def _safe_get_project_health_summary(project_id: str) -> dict:
    """Safely get project health summary from project_orchestrator."""
    try:
        from project_orchestrator import get_project_health_summary

        return get_project_health_summary(project_id)
    except ImportError:
        return {
            "success": False,
            "project_id": project_id,
            "health_score": 0.5,
            "task_counts": {
                "pending": 0,
                "in_progress": 0,
                "blocked": 0,
                "completed": 0,
            },
            "active_employees": 0,
            "errors": ["project_orchestrator module not available"],
        }
    except Exception as e:
        return {
            "success": False,
            "project_id": project_id,
            "health_score": 0.5,
            "task_counts": {
                "pending": 0,
                "in_progress": 0,
                "blocked": 0,
                "completed": 0,
            },
            "active_employees": 0,
            "errors": [str(e)],
        }


def aggregate_all_projects() -> dict:
    """
    Aggregate health, tasks, and employees across all projects.

    Performance target: <5 seconds for 10 projects.

    Returns:
        Dict with:
        - success: bool
        - project_count: Number of projects
        - projects: List of per-project summaries containing:
            - project_id: Project identifier
            - project_name: Display name
            - health_score: 0-100 score
            - health_status: "healthy", "warning", or "critical"
            - task_counts: {pending, in_progress, blocked, completed}
            - employee_count: Active employees assigned
        - totals: Company-wide totals
        - generated_at: ISO timestamp
    """
    import time

    start_time = time.time()
    now = datetime.now(timezone.utc)

    # Load org to get project list
    org = _safe_load_org()
    projects_config = org.get("projects", [])

    # Also try to discover projects if multi-project mode
    discovered_projects = _safe_discover_projects()

    # Merge project lists (prefer org.json data)
    project_ids_seen = set()
    all_projects = []

    for proj in projects_config:
        proj_id = proj.get("id", "unknown")
        if proj_id not in project_ids_seen:
            project_ids_seen.add(proj_id)
            all_projects.append(
                {
                    "id": proj_id,
                    "name": proj.get("name", proj_id),
                    "path": proj.get("path"),
                    "status": proj.get("status", "active"),
                }
            )

    for proj in discovered_projects:
        proj_id = proj.get("project_id", "unknown")
        if proj_id not in project_ids_seen:
            project_ids_seen.add(proj_id)
            all_projects.append(
                {
                    "id": proj_id,
                    "name": proj.get("project_name", proj_id),
                    "path": proj.get("project_path"),
                    "status": "active",
                }
            )

    # Aggregate per-project data
    project_summaries = []
    totals = {
        "total_tasks": 0,
        "pending": 0,
        "in_progress": 0,
        "blocked": 0,
        "completed": 0,
        "total_employees": 0,
    }

    for proj in all_projects:
        proj_id = proj["id"]

        # Get health summary from project_orchestrator
        health_summary = _safe_get_project_health_summary(proj_id)

        task_counts = health_summary.get(
            "task_counts",
            {"pending": 0, "in_progress": 0, "blocked": 0, "completed": 0},
        )
        employee_count = health_summary.get("active_employees", 0)
        raw_health_score = health_summary.get("health_score", 0.5)

        # Convert 0-1 health score to 0-100
        health_score = round(raw_health_score * 100, 1)

        # Determine health status
        if health_score >= 80:
            health_status = "healthy"
        elif health_score >= 60:
            health_status = "warning"
        else:
            health_status = "critical"

        # Calculate total tasks for this project
        proj_total_tasks = sum(
            task_counts.get(status, 0)
            for status in ["pending", "in_progress", "blocked", "completed"]
        )

        project_summaries.append(
            {
                "project_id": proj_id,
                "project_name": proj.get("name", proj_id),
                "health_score": health_score,
                "health_status": health_status,
                "task_counts": task_counts,
                "total_tasks": proj_total_tasks,
                "employee_count": employee_count,
                "status": proj.get("status", "active"),
            }
        )

        # Accumulate totals
        totals["total_tasks"] += proj_total_tasks
        totals["pending"] += task_counts.get("pending", 0)
        totals["in_progress"] += task_counts.get("in_progress", 0)
        totals["blocked"] += task_counts.get("blocked", 0)
        totals["completed"] += task_counts.get("completed", 0)
        totals["total_employees"] += employee_count

    elapsed_time = time.time() - start_time

    return {
        "success": True,
        "project_count": len(project_summaries),
        "projects": project_summaries,
        "totals": totals,
        "performance": {
            "elapsed_seconds": round(elapsed_time, 3),
            "meets_target": elapsed_time < 5.0,
            "target_seconds": 5.0,
        },
        "generated_at": now.isoformat(),
    }


def get_unified_dashboard() -> dict:
    """
    Get company-wide unified dashboard view.

    Aggregates all projects into a single company-level view with:
    - Company-wide health score (weighted by project size)
    - Aggregated progress across all projects
    - Cross-project task summary
    - Risk summary

    Returns:
        Dict with:
        - success: bool
        - company_health: Weighted company-wide health score and status
        - aggregated_progress: Combined progress across projects
        - task_summary: Cross-project task breakdown
        - risk_summary: Aggregated risks
        - project_count: Number of projects included
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    # Get all project data
    all_projects_data = aggregate_all_projects()
    projects = all_projects_data.get("projects", [])
    totals = all_projects_data.get("totals", {})

    # Calculate weighted company health score (weighted by task count)
    total_weight = 0
    weighted_health_sum = 0

    for proj in projects:
        weight = max(1, proj.get("total_tasks", 0))  # Minimum weight of 1
        total_weight += weight
        weighted_health_sum += proj.get("health_score", 50) * weight

    if total_weight > 0:
        company_health_score = round(weighted_health_sum / total_weight, 1)
    else:
        company_health_score = 50.0  # Default neutral score

    # Determine company health status
    if company_health_score >= 80:
        company_health_status = "healthy"
    elif company_health_score >= 60:
        company_health_status = "warning"
    else:
        company_health_status = "critical"

    # Count health statuses across projects
    health_distribution = {"healthy": 0, "warning": 0, "critical": 0}
    for proj in projects:
        status = proj.get("health_status", "warning")
        if status in health_distribution:
            health_distribution[status] += 1

    # Calculate aggregated progress
    total_tasks = totals.get("total_tasks", 0)
    completed = totals.get("completed", 0)
    in_progress = totals.get("in_progress", 0)
    blocked = totals.get("blocked", 0)
    pending = totals.get("pending", 0)

    if total_tasks > 0:
        completion_percentage = round((completed / total_tasks) * 100, 1)
        blocked_percentage = round((blocked / total_tasks) * 100, 1)
        in_progress_percentage = round((in_progress / total_tasks) * 100, 1)
        pending_percentage = round((pending / total_tasks) * 100, 1)
    else:
        completion_percentage = 0.0
        blocked_percentage = 0.0
        in_progress_percentage = 0.0
        pending_percentage = 0.0

    aggregated_progress = {
        "total_tasks": total_tasks,
        "completed": completed,
        "in_progress": in_progress,
        "blocked": blocked,
        "pending": pending,
        "completion_percentage": completion_percentage,
        "blocked_percentage": blocked_percentage,
        "in_progress_percentage": in_progress_percentage,
        "pending_percentage": pending_percentage,
    }

    # Task summary with cross-project view
    task_summary = {
        "total_across_projects": total_tasks,
        "active_work": in_progress + pending,
        "blocked_work": blocked,
        "completed_work": completed,
        "projects_with_blocked_tasks": sum(
            1 for p in projects if p.get("task_counts", {}).get("blocked", 0) > 0
        ),
        "projects_with_no_progress": sum(
            1
            for p in projects
            if p.get("task_counts", {}).get("completed", 0) == 0
            and p.get("task_counts", {}).get("in_progress", 0) == 0
            and p.get("total_tasks", 0) > 0
        ),
    }

    # Get risk summary from aggregate_risks
    risks = aggregate_risks()
    risk_summary = {
        "total_risks": len(risks),
        "critical_count": sum(1 for r in risks if r.get("severity") == "CRITICAL"),
        "warning_count": sum(1 for r in risks if r.get("severity") == "WARNING"),
        "categories": list(set(r.get("category", "unknown") for r in risks)),
        "top_risks": risks[:3],  # Top 3 risks
    }

    return {
        "success": True,
        "company_health": {
            "score": company_health_score,
            "status": company_health_status,
            "weighting": "by_project_size",
            "health_distribution": health_distribution,
        },
        "aggregated_progress": aggregated_progress,
        "task_summary": task_summary,
        "risk_summary": risk_summary,
        "workforce": {
            "total_employees": totals.get("total_employees", 0),
        },
        "project_count": len(projects),
        "generated_at": now.isoformat(),
    }


def get_project_comparison() -> dict:
    """
    Get side-by-side metrics comparison for all projects.

    Highlights outliers (best and worst health scores).

    Returns:
        Dict with:
        - success: bool
        - project_count: Number of projects
        - comparison: List of projects with normalized metrics
        - outliers: {best: [...], worst: [...]}
        - averages: Company-wide averages for comparison
        - generated_at: ISO timestamp
    """
    now = datetime.now(timezone.utc)

    # Get all project data
    all_projects_data = aggregate_all_projects()
    projects = all_projects_data.get("projects", [])

    if not projects:
        return {
            "success": True,
            "project_count": 0,
            "comparison": [],
            "outliers": {"best": [], "worst": []},
            "averages": {},
            "message": "No projects found",
            "generated_at": now.isoformat(),
        }

    # Build comparison data for each project
    comparison = []
    for proj in projects:
        task_counts = proj.get("task_counts", {})
        total_tasks = proj.get("total_tasks", 0)

        # Calculate blocked ratio
        blocked = task_counts.get("blocked", 0)
        blocked_ratio = (
            round((blocked / total_tasks) * 100, 1) if total_tasks > 0 else 0
        )

        # Calculate completion ratio
        completed = task_counts.get("completed", 0)
        completion_ratio = (
            round((completed / total_tasks) * 100, 1) if total_tasks > 0 else 0
        )

        comparison.append(
            {
                "project_id": proj.get("project_id"),
                "project_name": proj.get("project_name"),
                "health_score": proj.get("health_score", 50),
                "health_status": proj.get("health_status", "warning"),
                "total_tasks": total_tasks,
                "completed_tasks": completed,
                "blocked_tasks": blocked,
                "in_progress_tasks": task_counts.get("in_progress", 0),
                "pending_tasks": task_counts.get("pending", 0),
                "completion_ratio": completion_ratio,
                "blocked_ratio": blocked_ratio,
                "employee_count": proj.get("employee_count", 0),
            }
        )

    # Sort by health score to identify outliers
    sorted_by_health = sorted(
        comparison, key=lambda x: x.get("health_score", 0), reverse=True
    )

    # Identify outliers (best and worst performing)
    # Best: top projects with health >= 80 or top 3
    # Worst: projects with health < 60 or bottom 3
    best_projects = []
    worst_projects = []

    # Best performers
    for proj in sorted_by_health[:3]:
        if proj.get("health_score", 0) >= 70:  # Only include if reasonably healthy
            best_projects.append(
                {
                    "project_id": proj.get("project_id"),
                    "project_name": proj.get("project_name"),
                    "health_score": proj.get("health_score"),
                    "reason": (
                        "high_completion"
                        if proj.get("completion_ratio", 0) > 50
                        else "low_blocked"
                        if proj.get("blocked_ratio", 100) < 10
                        else "overall_health"
                    ),
                }
            )

    # Worst performers
    for proj in reversed(sorted_by_health[-3:]):
        if proj.get("health_score", 100) < 70:  # Only include if actually struggling
            worst_projects.append(
                {
                    "project_id": proj.get("project_id"),
                    "project_name": proj.get("project_name"),
                    "health_score": proj.get("health_score"),
                    "reason": (
                        "high_blocked"
                        if proj.get("blocked_ratio", 0) > 20
                        else "no_progress"
                        if proj.get("completion_ratio", 100) == 0
                        else "low_health"
                    ),
                }
            )

    # Calculate averages for comparison baseline
    if comparison:
        avg_health = round(
            sum(p.get("health_score", 0) for p in comparison) / len(comparison), 1
        )
        avg_completion = round(
            sum(p.get("completion_ratio", 0) for p in comparison) / len(comparison), 1
        )
        avg_blocked = round(
            sum(p.get("blocked_ratio", 0) for p in comparison) / len(comparison), 1
        )
        avg_tasks = round(
            sum(p.get("total_tasks", 0) for p in comparison) / len(comparison), 1
        )
        avg_employees = round(
            sum(p.get("employee_count", 0) for p in comparison) / len(comparison), 1
        )
    else:
        avg_health = 0
        avg_completion = 0
        avg_blocked = 0
        avg_tasks = 0
        avg_employees = 0

    averages = {
        "health_score": avg_health,
        "completion_ratio": avg_completion,
        "blocked_ratio": avg_blocked,
        "tasks_per_project": avg_tasks,
        "employees_per_project": avg_employees,
    }

    return {
        "success": True,
        "project_count": len(comparison),
        "comparison": comparison,
        "outliers": {
            "best": best_projects,
            "worst": worst_projects,
        },
        "averages": averages,
        "generated_at": now.isoformat(),
    }


def aggregate_autonomy_audit() -> dict:
    """Load the latest verdict-calibration snapshot for the dashboard (Phase 1).

    Produced by ``/calibrate`` (autonomy_metrics.append_autonomy_audit). Surfaces
    the deduped local autonomy proxy alongside the gh-ground-truth trust/phantom
    rates — the honest "is done actually done?" numbers. Returns a safe
    ``available=False`` shape when no calibration has been run yet.

    NOTE: this is distinct from ``aggregate_autonomy_metrics`` (P25 scheduler/
    budget) and ``aggregate_autonomy_widget_metrics`` (whose ``autonomy_percent``
    derives from session_state and reads a false 100% while ``tasks_escalated`` is
    never incremented). The audit numbers below do NOT use that formula.
    """
    from pathlib import Path

    company_dir = Path(".company")
    result: dict[str, Any] = {
        "success": True,
        "available": False,
        "autonomy_proxy_rate": None,
        "verified_autonomy_rate": None,
        # Phase 0: TREND-honest windowed rate (recent cohort); None on older snapshots.
        "verified_autonomy_rate_windowed": None,
        "window_days": None,
        "trust_score": None,
        "phantom_rate": None,
        "phantom_count": 0,
        "generated_at": None,
        "build_sha": None,
    }

    audit_path = company_dir / "state/autonomy_audit.json"
    try:
        if audit_path.exists():
            with open(audit_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") or []
            if entries:
                snap = entries[-1]
                proxy = snap.get("local_proxy", {}) or {}
                gt = snap.get("ground_truth", {}) or {}
                result.update(
                    {
                        "available": True,
                        "autonomy_proxy_rate": proxy.get("autonomy_proxy_rate"),
                        "verified_autonomy_rate": gt.get("verified_autonomy_rate"),
                        "verified_autonomy_rate_windowed": gt.get(
                            "verified_autonomy_rate_windowed"
                        ),
                        "window_days": gt.get("window_days"),
                        "trust_score": gt.get("trust_score"),
                        "phantom_rate": gt.get("phantom_rate"),
                        "phantom_count": len(snap.get("phantoms", []) or []),
                        "generated_at": snap.get("generated_at"),
                        "build_sha": snap.get("build_sha"),
                    }
                )
    except (json.JSONDecodeError, OSError, KeyError):
        pass

    return result


def aggregate_performance_trends(project_id: str | None = None) -> dict:
    """
    Aggregate performance trends from the rolling-window metrics.

    Answers the question "are we getting faster or slower?" — a gap in the
    existing point-in-time dashboards.

    Returns:
        Dict from performance_trends.get_performance_trends(), including:
        - velocity: direction, change_percent, daily_data, peak/slowest days
        - completion: day-over-day change
        - summary: human-readable headline for the dashboard
        - generated_at: ISO timestamp
    """
    try:
        from pathlib import Path

        from performance_trends import get_performance_trends

        company_dir = Path(".company")
        return get_performance_trends(company_dir, project_id)
    except ImportError:
        from datetime import datetime, timezone

        return {
            "success": False,
            "message": "performance_trends module not available",
            "velocity": {
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
            },
            "completion": {
                "today_count": 0,
                "yesterday_count": 0,
                "dod_change_percent": 0.0,
                "dod_direction": "stable",
                "weekly_total": 0,
            },
            "summary": "No trend data available",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        from datetime import datetime, timezone

        return {
            "success": False,
            "error": str(e),
            "summary": "Trend computation failed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


def get_dashboard_data(project_id: str | None = None) -> dict:
    """
    Get full dashboard data combining all aggregations.

    Args:
        project_id: Optional project ID for project-specific data

    Returns:
        Dict with:
        - health: Overall health aggregation
        - progress: Progress and forecast aggregation
        - workforce: Workforce status aggregation
        - risks: Identified risks
        - metadata: Generation metadata
    """
    now = datetime.now(timezone.utc)

    # Check dependencies
    deps_ok, deps_error = _ensure_dependencies()

    # Aggregate all data
    health = aggregate_health(project_id)
    progress = aggregate_progress(project_id)
    workforce = aggregate_workforce(project_id)
    risks = aggregate_risks()
    efficiency = aggregate_efficiency()
    daemon = aggregate_daemon_metrics()
    autonomy = aggregate_autonomy_metrics()
    orchestrator = aggregate_orchestrator_metrics()
    autonomy_audit = aggregate_autonomy_audit()
    trends = aggregate_performance_trends(project_id)

    # Calculate summary statistics
    risk_summary = {
        "total_risks": len(risks),
        "critical_count": sum(1 for r in risks if r["severity"] == "CRITICAL"),
        "warning_count": sum(1 for r in risks if r["severity"] == "WARNING"),
        "categories": list(set(r["category"] for r in risks)),
    }

    # Determine overall status
    if risk_summary["critical_count"] > 0:
        overall_status = "critical"
    elif risk_summary["warning_count"] > 0:
        overall_status = "warning"
    elif health.get("health_score", 0) >= 80:
        overall_status = "healthy"
    else:
        overall_status = "stable"

    # Get parallel execution status if enabled
    parallel = _get_parallel_status()

    # WS-117: Stability metrics from health dashboard
    stability = {}
    try:
        from pathlib import Path

        from daemon_health_dashboard import generate_health_report

        company_dir = Path(".company")
        if company_dir.is_dir():
            stability = generate_health_report(company_dir)
    except Exception:
        stability = {"error": "Health dashboard unavailable"}

    result = {
        "success": True,
        "overall_status": overall_status,
        "health": health,
        "progress": progress,
        "workforce": workforce,
        "efficiency": efficiency,
        "daemon": daemon,
        "autonomy": autonomy,
        "autonomy_audit": autonomy_audit,
        "orchestrator": orchestrator,
        "trends": trends,
        "parallel": parallel,
        "stability": stability,
        "risks": risks,
        "risk_summary": risk_summary,
        "metadata": {
            "generated_at": now.isoformat(),
            "dependencies_available": deps_ok,
            "dependencies_error": deps_error if not deps_ok else None,
            "multi_project_mode": health.get("multi_project_mode", False),
            "p25_autonomy_enabled": autonomy.get("enabled", False),
            "p28_orchestrator_enabled": orchestrator.get("enabled", False),
            "parallel_execution_enabled": parallel.get("enabled", False),
        },
    }

    if project_id:
        result["project_id"] = project_id

    return result


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Dashboard Aggregator — aggregate data from multiple sources for the dashboard

Combines data from metrics_tracker and progress_tracker to provide a unified
dashboard view of company health, progress, workforce, and risks.

Commands:
    health      Get overall health score with breakdown by factors
    progress    Get task completion, velocity, and delivery forecast
    workforce   Get agent status, utilization, and department breakdown
    efficiency  Get efficiency metrics and optimization status
    daemon      Get daemon status and planning metrics (P20)
    autonomy    Get P25 autonomy metrics (scheduler, budget, learning)
    orchestrator Get P28 orchestrator routing metrics
    trends      Get velocity and completion trend analysis (is perf improving?)
    risks       Get identified risks with severity levels
    full        Get full dashboard data combining all the above
    help        Show this help message

Daemon/Planning Commands:
    daemon          Get daemon status with planning metrics
                    - planning_ratio: % of tasks that went through planning
                    - avg_plan_score: average checker score (0-25 scale)
                    - revision_rate: % of plans requiring revision

P25 Autonomy Commands:
    autonomy        Get P25 autonomy metrics including:
                    - scheduler: Current mode (AGGRESSIVE/NORMAL/CONSERVATIVE/IDLE)
                    - budget: Throttle level and budget utilization
                    - learning: Insights from execution outcomes
                    - session: Uptime and snapshot info
                    - approval: Auto-approve rate and decision history

P28 Orchestrator Commands:
    orchestrator    Get P28 orchestrator routing metrics including:
                    - complexity_distribution: Tasks by complexity level
                    - execution_mode_distribution: Tasks by execution mode
                    - gate_metrics: Gate pass rate and attempts
                    - performance: Routing time statistics
                    - health_issues: Orchestrator-related concerns

Multi-Project Commands:
    all-projects    Aggregate health, tasks, employees for all projects
    unified         Get company-wide unified dashboard (weighted health)
    compare         Side-by-side project comparison with outlier highlighting

Options:
    --project ID    Get data for a specific project (multi-project mode)
    --json          Output as formatted JSON (default)

Examples:
    # Get overall health
    python dashboard_aggregator.py health

    # Get progress with delivery forecast
    python dashboard_aggregator.py progress

    # Get workforce utilization
    python dashboard_aggregator.py workforce

    # Get identified risks
    python dashboard_aggregator.py risks

    # Get daemon status and planning metrics
    python dashboard_aggregator.py daemon

    # Get P25 autonomy metrics
    python dashboard_aggregator.py autonomy

    # Get full dashboard data
    python dashboard_aggregator.py full

    # Get project-specific data
    python dashboard_aggregator.py full --project myproject-123

    # Multi-project: Get all projects summary
    python dashboard_aggregator.py all-projects

    # Multi-project: Get unified company dashboard
    python dashboard_aggregator.py unified

    # Multi-project: Compare all projects side-by-side
    python dashboard_aggregator.py compare

Output: JSON with aggregated dashboard data.
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


def aggregate_autonomy_widget_metrics() -> dict:
    """
    Aggregate autonomy metrics for the dashboard widget.

    Computes four key autonomy indicators:
    - autonomy_percent: % of tasks resolved without human intervention
    - time_since_last_human: seconds/formatted since last escalation
    - active_recovery_attempts: tasks currently in autonomous recovery
    - goal_progress_velocity: tasks completed per day

    Returns:
        Dict with success, autonomy_percent, tasks_completed, tasks_escalated,
        time_since_last_human_seconds, time_since_last_human_formatted,
        last_escalation_at, active_recovery_attempts, goal_progress_velocity,
        and generated_at.
    """
    from pathlib import Path

    now = datetime.now(timezone.utc)
    company_dir = Path(".company")

    result: dict[str, Any] = {
        "success": True,
        "autonomy_percent": 100.0,
        "tasks_completed": 0,
        "tasks_escalated": 0,
        "time_since_last_human_seconds": None,
        "time_since_last_human_formatted": "Never",
        "last_escalation_at": None,
        "active_recovery_attempts": 0,
        "goal_progress_velocity": 0.0,
        "generated_at": now.isoformat(),
    }

    # --- Autonomy % from session loop metrics ---
    session_state_path = company_dir / "state/session_state.json"
    try:
        if session_state_path.exists():
            with open(session_state_path, encoding="utf-8") as f:
                session_state = json.load(f)
            loop_metrics = session_state.get("loop_metrics", {})
            tasks_completed = loop_metrics.get("tasks_completed", 0)
            tasks_escalated = loop_metrics.get("tasks_escalated", 0)
            total = tasks_completed + tasks_escalated
            result["tasks_completed"] = tasks_completed
            result["tasks_escalated"] = tasks_escalated
            if total > 0:
                result["autonomy_percent"] = round(tasks_completed / total * 100, 1)
    except (json.JSONDecodeError, OSError, KeyError):
        pass

    # --- Time since last human intervention from escalation records ---
    escalations_dir = company_dir / "escalations"
    try:
        if escalations_dir.exists():
            latest_escalation_time: datetime | None = None
            for esc_file in escalations_dir.glob("*.json"):
                try:
                    with open(esc_file, encoding="utf-8") as f:
                        esc = json.load(f)
                    created_at_str = esc.get("created_at", "")
                    if created_at_str:
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if (
                            latest_escalation_time is None
                            or created_at > latest_escalation_time
                        ):
                            latest_escalation_time = created_at
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
            if latest_escalation_time is not None:
                elapsed = int((now - latest_escalation_time).total_seconds())
                result["time_since_last_human_seconds"] = elapsed
                result["time_since_last_human_formatted"] = _format_uptime(elapsed)
                result["last_escalation_at"] = latest_escalation_time.isoformat()
    except OSError:
        pass

    # --- Active recovery attempts from work queue ---
    queue_path = company_dir / "state/work_queue.json"
    try:
        if queue_path.exists():
            with open(queue_path, encoding="utf-8") as f:
                queue = json.load(f)
            active_recovery = 0
            for section in ("pending", "in_progress"):
                for task in queue.get(section, []):
                    if task.get("recovery_attempt_count", 0) > 0:
                        active_recovery += 1
            result["active_recovery_attempts"] = active_recovery
    except (json.JSONDecodeError, OSError):
        pass

    # --- Goal progress velocity (tasks/day) ---
    task_metrics = _safe_calculate_task_metrics()
    result["goal_progress_velocity"] = round(task_metrics.get("velocity_daily", 0.0), 1)

    # WS-106-007: Enhanced autonomy metrics from WS-106 features
    ws106_metrics = _aggregate_ws106_autonomy_metrics(company_dir)
    result.update(ws106_metrics)

    return result


def _aggregate_ws106_autonomy_metrics(company_dir) -> dict:
    """WS-106-007: Aggregate autonomy metrics from WS-106 features.

    Tracks:
    - Pattern library size and growth
    - Smart escalation routing effectiveness
    - Auto-merge confidence decisions
    - Gap analysis activity

    Returns:
        Dict with ws106_* prefixed metrics.
    """
    result: dict[str, Any] = {
        "ws106_patterns_total": 0,
        "ws106_patterns_high_confidence": 0,
        "ws106_escalation_routes": 0,
        "ws106_escalation_success_rate": 0.0,
        "ws106_auto_merge_decisions": 0,
        "ws106_gap_tasks_created": 0,
        "ws106_autonomy_level": "unknown",
    }

    # --- Pattern library metrics ---
    patterns_path = company_dir / "knowledge/patterns.json"
    try:
        if patterns_path.exists():
            with open(patterns_path, encoding="utf-8") as f:
                patterns_data = json.load(f)
            patterns = patterns_data.get("patterns", [])
            result["ws106_patterns_total"] = len(patterns)
            high_conf = sum(1 for p in patterns if p.get("confidence", 0) >= 0.8)
            result["ws106_patterns_high_confidence"] = high_conf
    except (json.JSONDecodeError, OSError):
        pass

    # --- Escalation routing patterns ---
    routing_patterns_path = company_dir / "state/escalation_router_patterns.json"
    try:
        if routing_patterns_path.exists():
            with open(routing_patterns_path, encoding="utf-8") as f:
                routing_data = json.load(f)
            total_routed = 0
            total_successes = 0
            for pattern_key, pattern in routing_data.items():
                total_routed += pattern.get("total_routed", 0)
                for emp_stats in pattern.get("employee_stats", {}).values():
                    total_successes += emp_stats.get("successes", 0)
            result["ws106_escalation_routes"] = total_routed
            if total_routed > 0:
                result["ws106_escalation_success_rate"] = round(
                    total_successes / total_routed * 100, 1
                )
    except (json.JSONDecodeError, OSError):
        pass

    # --- Gap analysis tasks ---
    queue_path = company_dir / "state/work_queue.json"
    try:
        if queue_path.exists():
            with open(queue_path, encoding="utf-8") as f:
                queue = json.load(f)
            gap_tasks = 0
            for section in ("pending", "in_progress", "completed"):
                for task in queue.get(section, []):
                    if task.get("source") == "gap_analysis":
                        gap_tasks += 1
            result["ws106_gap_tasks_created"] = gap_tasks
    except (json.JSONDecodeError, OSError):
        pass

    # --- Compute overall autonomy level ---
    # Based on: autonomy%, pattern count, routing success
    autonomy_pct = 0.0
    session_state_path = company_dir / "state/session_state.json"
    try:
        if session_state_path.exists():
            with open(session_state_path, encoding="utf-8") as f:
                session_state = json.load(f)
            loop_metrics = session_state.get("loop_metrics", {})
            completed = loop_metrics.get("tasks_completed", 0)
            escalated = loop_metrics.get("tasks_escalated", 0)
            total = completed + escalated
            if total > 0:
                autonomy_pct = completed / total * 100
    except (json.JSONDecodeError, OSError):
        pass

    # Level thresholds: <50% = low, 50-70% = moderate, 70-85% = high, >85% = autonomous
    if autonomy_pct >= 85:
        level = "autonomous"
    elif autonomy_pct >= 70:
        level = "high"
    elif autonomy_pct >= 50:
        level = "moderate"
    elif autonomy_pct > 0:
        level = "low"
    else:
        level = "unknown"

    result["ws106_autonomy_level"] = level

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

    project_id = args.get("project")

    try:
        if command == "health":
            result = aggregate_health(project_id)
            print(json.dumps(result, indent=2))

        elif command == "progress":
            result = aggregate_progress(project_id)
            print(json.dumps(result, indent=2))

        elif command == "workforce":
            result = aggregate_workforce(project_id)
            print(json.dumps(result, indent=2))

        elif command == "efficiency":
            result = aggregate_efficiency()
            print(json.dumps(result, indent=2))

        elif command == "daemon":
            result = aggregate_daemon_metrics()
            print(json.dumps(result, indent=2))

        elif command == "autonomy":
            result = aggregate_autonomy_metrics()
            print(json.dumps(result, indent=2))

        elif command == "autonomy-widget":
            result = aggregate_autonomy_widget_metrics()
            print(json.dumps(result, indent=2))

        elif command == "orchestrator":
            result = aggregate_orchestrator_metrics()
            print(json.dumps(result, indent=2))

        elif command == "trends":
            result = aggregate_performance_trends(project_id)
            print(json.dumps(result, indent=2))

        elif command == "risks":
            risks = aggregate_risks()
            result = {
                "success": True,
                "risk_count": len(risks),
                "critical_count": sum(1 for r in risks if r["severity"] == "CRITICAL"),
                "warning_count": sum(1 for r in risks if r["severity"] == "WARNING"),
                "risks": risks,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps(result, indent=2))

        elif command == "full":
            result = get_dashboard_data(project_id)
            print(json.dumps(result, indent=2))

        elif command == "all-projects":
            result = aggregate_all_projects()
            print(json.dumps(result, indent=2))

        elif command == "unified":
            result = get_unified_dashboard()
            print(json.dumps(result, indent=2))

        elif command == "compare":
            result = get_project_comparison()
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
