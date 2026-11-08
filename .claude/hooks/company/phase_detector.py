#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Company Phase Detection Utility — detect organizational lifecycle phase from metrics.

Detects which phase the company is in based on configurable thresholds:
- startup: employees < 5 AND completed_tasks < 20
- growth: employees >= 5 AND employees < 15 AND velocity > 0
- scale: employees >= 15 AND test_coverage >= 70% AND blocked_ratio < 10%
- mature: employees >= 15 AND velocity stable (+-10% variance 7 days) AND blocked_ratio < 5%
- decline_pivot: velocity declining (>20% drop) OR blocked_ratio > 25% OR stalled_tasks > 50%

Reads from:
- .company/org.json (employee count, agents)
- .company/work_queue.json (task states, blocked ratio, stalled tasks)
- .planning/STATE.md (completed tasks from progress section)
- .company/config.json (configurable thresholds in "phase_detection" key)
- .company/metrics.json (velocity data, test coverage)

Usage:
    uv run phase_detector.py detect    # JSON {phase, metrics, confidence, transition_suggested}
    uv run phase_detector.py metrics   # Current metrics used for detection
    uv run phase_detector.py suggest   # Suggested phase transition with reasoning

Exit codes:
    0 - Success
    1 - Error
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# -----------------------------------------------------------------------------
# Default thresholds (can be overridden in config.json)
# -----------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "startup": {
        "max_employees": 5,
        "max_completed_tasks": 20,
        "max_executives": 0,  # No C-level yet = startup
    },
    "growth": {
        "min_employees": 5,
        "max_employees": 15,
        "min_velocity": 0.1,
        "min_executives": 1,  # At least 1 C-level for growth
    },
    "scale": {
        "min_employees": 15,
        "min_test_coverage": 70,
        "max_blocked_ratio": 10,
        "min_executives": 2,  # Need CEO + CTO for scale
    },
    "mature": {
        "min_employees": 15,
        "max_velocity_variance": 10,  # percent
        "max_blocked_ratio": 5,
        "velocity_window_days": 7,
        "min_executives": 2,  # Full C-suite for mature
    },
    "decline_pivot": {
        "velocity_decline_threshold": 20,  # percent drop
        "max_blocked_ratio": 25,
        "max_stalled_ratio": 50,
    },
}

# C-level roles that count as executives
EXECUTIVE_ROLES = {"ceo", "cto", "cfo", "coo", "cmo", "coordinator"}

# Phase priority order (for when multiple phases match)
PHASE_PRIORITY = ["decline_pivot", "mature", "scale", "growth", "startup"]


# -----------------------------------------------------------------------------
# File paths
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the .company directory path."""
    return Path(os.getcwd()) / ".company"


def get_planning_dir() -> Path:
    """Get the .planning directory path."""
    return Path(os.getcwd()) / ".planning"


def get_org_path() -> Path:
    """Get org.json path."""
    return get_company_dir() / "org.json"


def get_queue_path() -> Path:
    """Get work_queue.json path."""
    return get_company_dir() / "state/work_queue.json"


def get_config_path() -> Path:
    """Get config.json path."""
    return get_company_dir() / "config/config.json"


def get_metrics_path() -> Path:
    """Get metrics.json path."""
    return get_company_dir() / "state/metrics.json"


def get_state_path() -> Path:
    """Get STATE.md path."""
    return get_planning_dir() / "STATE.md"


# -----------------------------------------------------------------------------
# Data loading functions
# -----------------------------------------------------------------------------


def load_json_file(path: Path, default: dict | list | None = None) -> dict | list:
    """Load a JSON file, returning default if not found or invalid."""
    if default is None:
        default = {}

    if not path.exists():
        return default

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def load_org() -> dict:
    """Load organization data from org.json."""
    return load_json_file(
        get_org_path(),
        {
            "employees": [],
            "agents": [],
            "work": {"completed": 0},
        },
    )


def load_queue() -> dict:
    """Load work queue from work_queue.json."""
    return load_json_file(
        get_queue_path(),
        {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
        },
    )


def load_config() -> dict:
    """Load config from config.json."""
    return load_json_file(get_config_path(), {})


def load_metrics() -> dict:
    """Load metrics from metrics.json."""
    return load_json_file(
        get_metrics_path(),
        {
            "task_completions": [],
            "velocity": {"daily": [], "weekly_average": 0.0},
        },
    )


def load_state_md() -> dict:
    """Parse STATE.md for completed tasks from progress table."""
    state_path = get_state_path()
    result = {
        "total_tasks_done": 0,
        "total_tasks": 0,
        "phases": [],
    }

    if not state_path.exists():
        return result

    try:
        with open(state_path, encoding="utf-8") as f:
            content = f.read()

        # Find the Progress table
        # Format: | Phase | Tasks Total | Tasks Done | Status |
        progress_match = re.search(
            r"## Progress\s*\n\|.*\|.*\|.*\|.*\|\s*\n\|[-\s|]+\n((?:\|.*\|\s*\n)+)",
            content,
            re.MULTILINE,
        )

        if progress_match:
            table_rows = progress_match.group(1).strip().split("\n")
            for row in table_rows:
                cells = [c.strip() for c in row.split("|") if c.strip()]
                if len(cells) >= 4:
                    try:
                        phase_name = cells[0]
                        tasks_total = int(cells[1])
                        tasks_done = int(cells[2])
                        status = cells[3]

                        result["phases"].append(
                            {
                                "name": phase_name,
                                "total": tasks_total,
                                "done": tasks_done,
                                "status": status,
                            }
                        )
                        result["total_tasks"] += tasks_total
                        result["total_tasks_done"] += tasks_done
                    except (ValueError, IndexError):
                        continue

        return result
    except OSError:
        return result


def get_thresholds() -> dict:
    """
    Get phase detection thresholds from config or defaults.

    Reads from two config locations (in order of precedence):
    1. config.lifecycle.thresholds - simplified flat threshold format
    2. config.phase_detection - detailed per-phase threshold format (legacy)

    The lifecycle.thresholds format uses flat keys like:
    - startup_to_growth_employees: 5
    - growth_to_scale_employees: 15
    - scale_to_mature_velocity_variance: 10
    - decline_velocity_drop: 20
    - decline_blocked_ratio: 25
    """
    config = load_config()

    # Start with defaults
    thresholds = {}
    for phase, defaults in DEFAULT_THRESHOLDS.items():
        thresholds[phase] = defaults.copy()

    # Apply legacy phase_detection overrides if present
    legacy_thresholds = config.get("phase_detection", {})
    for phase in thresholds:
        if phase in legacy_thresholds:
            thresholds[phase].update(legacy_thresholds[phase])

    # Apply lifecycle.thresholds overrides (takes precedence over phase_detection)
    lifecycle = config.get("lifecycle", {})
    lifecycle_thresholds = lifecycle.get("thresholds", {})

    if lifecycle_thresholds:
        # Map flat lifecycle thresholds to phase-specific thresholds
        # startup_to_growth_employees -> startup.max_employees and growth.min_employees
        if "startup_to_growth_employees" in lifecycle_thresholds:
            val = lifecycle_thresholds["startup_to_growth_employees"]
            thresholds["startup"]["max_employees"] = val
            thresholds["growth"]["min_employees"] = val

        # growth_to_scale_employees -> growth.max_employees and scale.min_employees
        if "growth_to_scale_employees" in lifecycle_thresholds:
            val = lifecycle_thresholds["growth_to_scale_employees"]
            thresholds["growth"]["max_employees"] = val
            thresholds["scale"]["min_employees"] = val
            thresholds["mature"]["min_employees"] = val

        # scale_to_mature_velocity_variance -> mature.max_velocity_variance
        if "scale_to_mature_velocity_variance" in lifecycle_thresholds:
            val = lifecycle_thresholds["scale_to_mature_velocity_variance"]
            thresholds["mature"]["max_velocity_variance"] = val

        # decline_velocity_drop -> decline_pivot.velocity_decline_threshold
        if "decline_velocity_drop" in lifecycle_thresholds:
            val = lifecycle_thresholds["decline_velocity_drop"]
            thresholds["decline_pivot"]["velocity_decline_threshold"] = val

        # decline_blocked_ratio -> decline_pivot.max_blocked_ratio
        if "decline_blocked_ratio" in lifecycle_thresholds:
            val = lifecycle_thresholds["decline_blocked_ratio"]
            thresholds["decline_pivot"]["max_blocked_ratio"] = val

    return thresholds


# -----------------------------------------------------------------------------
# Metrics calculation
# -----------------------------------------------------------------------------


def count_employees(org: dict) -> int:
    """Count total employees/agents in the organization."""
    employees = org.get("employees", [])
    agents = org.get("agents", [])

    # If employees list exists and has items, use it
    if employees:
        return len(employees)

    # Fallback to agents list for backward compatibility
    if agents:
        return len(agents)

    return 0


def count_executives(org: dict) -> int:
    """
    Count C-level executives in the organization.
    Looks for roles in EXECUTIVE_ROLES (ceo, cto, cfo, etc.)
    """
    employees = org.get("employees", [])
    if not employees:
        employees = org.get("agents", [])

    count = 0
    for emp in employees:
        role = emp.get("role", "").lower()
        emp_id = emp.get("id", "").lower()

        # Check if role or id contains executive keywords
        for exec_role in EXECUTIVE_ROLES:
            if exec_role in role or exec_role in emp_id:
                count += 1
                break

    return count


def count_completed_tasks(org: dict, state: dict) -> int:
    """Count completed tasks from org.json and STATE.md."""
    # From org.json work.completed field
    org_completed = org.get("work", {}).get("completed", 0)

    # From STATE.md progress table
    state_completed = state.get("total_tasks_done", 0)

    # Use the higher value (they track different things)
    return max(org_completed, state_completed)


def calculate_blocked_ratio(queue: dict) -> float:
    """Calculate ratio of blocked tasks to total active tasks (percent)."""
    pending = len(queue.get("pending", []))
    in_progress = len(queue.get("in_progress", []))
    blocked = len(queue.get("blocked", []))

    total_active = pending + in_progress + blocked
    if total_active == 0:
        return 0.0

    return round((blocked / total_active) * 100, 2)


def calculate_stalled_ratio(queue: dict) -> float:
    """
    Calculate ratio of stalled tasks (blocked + old pending) to total.

    A task is considered stalled if:
    - It's in the blocked list
    - It's been pending for more than 24 hours
    """
    now = datetime.now(timezone.utc)
    stall_threshold = timedelta(hours=24)

    pending = queue.get("pending", [])
    blocked = queue.get("blocked", [])
    in_progress = queue.get("in_progress", [])

    stalled_count = len(blocked)  # All blocked tasks are stalled

    # Count old pending tasks
    for task in pending:
        created_at = task.get("created_at")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if now - created > stall_threshold:
                    stalled_count += 1
            except (ValueError, TypeError):
                continue

    total = len(pending) + len(blocked) + len(in_progress)
    if total == 0:
        return 0.0

    return round((stalled_count / total) * 100, 2)


def get_velocity_data(metrics: dict) -> dict:
    """Extract velocity data from metrics."""
    velocity = metrics.get("velocity", {})
    daily = velocity.get("daily", [])
    weekly_avg = velocity.get("weekly_average", 0.0)

    return {
        "daily": daily,
        "weekly_average": weekly_avg,
        "current": weekly_avg,
    }


def calculate_velocity_variance(velocity_data: dict, window_days: int = 7) -> float:
    """
    Calculate velocity variance over the last N days.
    Returns variance as a percentage of the mean.
    """
    daily = velocity_data.get("daily", [])

    if len(daily) < 2:
        return 0.0

    # Get last N days of data
    recent = daily[-window_days:] if len(daily) >= window_days else daily
    counts = [d.get("count", 0) for d in recent]

    if not counts:
        return 0.0

    mean = sum(counts) / len(counts)
    if mean == 0:
        return 0.0

    # Calculate variance as percentage of mean
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    std_dev = variance**0.5

    # Return coefficient of variation as percentage
    return round((std_dev / mean) * 100, 2)


def calculate_velocity_decline(velocity_data: dict) -> float:
    """
    Calculate velocity decline percentage.
    Compares recent velocity (last 3 days) to previous period (days 4-7).
    Returns positive percentage if declining, negative if improving.
    """
    daily = velocity_data.get("daily", [])

    if len(daily) < 4:
        return 0.0

    # Recent period (last 3 days)
    recent = daily[-3:]
    recent_avg = sum(d.get("count", 0) for d in recent) / len(recent)

    # Previous period (days 4-7)
    previous = daily[-7:-3] if len(daily) >= 7 else daily[:-3]
    if not previous:
        return 0.0
    previous_avg = sum(d.get("count", 0) for d in previous) / len(previous)

    if previous_avg == 0:
        return 0.0

    # Calculate decline percentage
    decline = ((previous_avg - recent_avg) / previous_avg) * 100
    return round(decline, 2)


def get_test_coverage(metrics: dict) -> float:
    """
    Get test coverage percentage.
    Looks for test_coverage in metrics, defaults to 0 if not found.
    """
    return metrics.get("test_coverage", 0.0)


def collect_all_metrics() -> dict:
    """Collect all metrics needed for phase detection."""
    org = load_org()
    queue = load_queue()
    metrics = load_metrics()
    state = load_state_md()

    velocity_data = get_velocity_data(metrics)

    return {
        "employee_count": count_employees(org),
        "executive_count": count_executives(org),
        "completed_tasks": count_completed_tasks(org, state),
        "blocked_ratio": calculate_blocked_ratio(queue),
        "stalled_ratio": calculate_stalled_ratio(queue),
        "velocity": velocity_data.get("current", 0.0),
        "velocity_variance": calculate_velocity_variance(velocity_data),
        "velocity_decline": calculate_velocity_decline(velocity_data),
        "test_coverage": get_test_coverage(metrics),
        "pending_tasks": len(queue.get("pending", [])),
        "in_progress_tasks": len(queue.get("in_progress", [])),
        "blocked_tasks": len(queue.get("blocked", [])),
        "state_phases": state.get("phases", []),
    }


# -----------------------------------------------------------------------------
# Phase detection logic
# -----------------------------------------------------------------------------


def check_startup_phase(
    metrics: dict, thresholds: dict
) -> tuple[bool, float, list[str]]:
    """
    Check if company is in startup phase.
    Returns (matches, confidence, reasons).

    Startup: Few employees, few completed tasks, no C-level executives yet.
    """
    t = thresholds.get("startup", {})
    max_employees = t.get("max_employees", 5)
    max_completed = t.get("max_completed_tasks", 20)
    max_executives = t.get("max_executives", 0)

    employees = metrics["employee_count"]
    completed = metrics["completed_tasks"]
    executives = metrics.get("executive_count", 0)

    reasons = []
    confidence = 0.0

    # Check conditions
    employee_match = employees < max_employees
    task_match = completed < max_completed
    exec_match = executives <= max_executives

    if employee_match:
        reasons.append(f"employees ({employees}) < {max_employees}")
        confidence += 0.4

    if task_match:
        reasons.append(f"completed_tasks ({completed}) < {max_completed}")
        confidence += 0.4

    if exec_match:
        reasons.append(f"executives ({executives}) <= {max_executives}")
        confidence += 0.2
    else:
        # Having executives suggests growth, reduces startup confidence
        reasons.append(
            f"executives ({executives}) > {max_executives} (suggests growth)"
        )

    # Startup requires both employee and task conditions
    # Executives are a bonus check
    matches = employee_match and task_match
    return matches, confidence if matches else 0.0, reasons


def check_growth_phase(
    metrics: dict, thresholds: dict
) -> tuple[bool, float, list[str]]:
    """
    Check if company is in growth phase.
    Returns (matches, confidence, reasons).

    Growth: 5-15 employees, positive velocity, at least 1 C-level executive.
    """
    t = thresholds.get("growth", {})
    min_employees = t.get("min_employees", 5)
    max_employees = t.get("max_employees", 15)
    min_velocity = t.get("min_velocity", 0.1)
    min_executives = t.get("min_executives", 1)

    employees = metrics["employee_count"]
    velocity = metrics["velocity"]
    executives = metrics.get("executive_count", 0)

    reasons = []
    confidence = 0.0

    # Check conditions
    employee_min_match = employees >= min_employees
    employee_max_match = employees < max_employees
    velocity_match = (
        velocity > min_velocity or velocity == 0
    )  # Allow 0 velocity if just starting
    exec_match = executives >= min_executives

    if employee_min_match:
        reasons.append(f"employees ({employees}) >= {min_employees}")
        confidence += 0.25

    if employee_max_match:
        reasons.append(f"employees ({employees}) < {max_employees}")
        confidence += 0.25

    if velocity_match:
        reasons.append(f"velocity ({velocity:.2f}) meets growth criteria")
        confidence += 0.25

    if exec_match:
        reasons.append(
            f"executives ({executives}) >= {min_executives} (C-level present)"
        )
        confidence += 0.25
    else:
        reasons.append(
            f"executives ({executives}) < {min_executives} (need C-level for full growth)"
        )

    # Growth requires employee range and at least some velocity
    # Having executives is a strong signal but not strictly required
    matches = (
        employee_min_match and employee_max_match and (velocity_match or exec_match)
    )
    return matches, confidence if matches else 0.0, reasons


def check_scale_phase(metrics: dict, thresholds: dict) -> tuple[bool, float, list[str]]:
    """
    Check if company is in scale phase.
    Returns (matches, confidence, reasons).
    """
    t = thresholds.get("scale", {})
    min_employees = t.get("min_employees", 15)
    min_test_coverage = t.get("min_test_coverage", 70)
    max_blocked_ratio = t.get("max_blocked_ratio", 10)

    employees = metrics["employee_count"]
    test_coverage = metrics["test_coverage"]
    blocked_ratio = metrics["blocked_ratio"]

    reasons = []
    confidence = 0.0

    # Check conditions
    employee_match = employees >= min_employees
    coverage_match = test_coverage >= min_test_coverage
    blocked_match = blocked_ratio < max_blocked_ratio

    if employee_match:
        reasons.append(f"employees ({employees}) >= {min_employees}")
        confidence += 0.33

    if coverage_match:
        reasons.append(f"test_coverage ({test_coverage:.1f}%) >= {min_test_coverage}%")
        confidence += 0.33

    if blocked_match:
        reasons.append(f"blocked_ratio ({blocked_ratio:.1f}%) < {max_blocked_ratio}%")
        confidence += 0.34

    matches = employee_match and coverage_match and blocked_match
    return matches, confidence if matches else 0.0, reasons


def check_mature_phase(
    metrics: dict, thresholds: dict
) -> tuple[bool, float, list[str]]:
    """
    Check if company is in mature phase.
    Returns (matches, confidence, reasons).
    """
    t = thresholds.get("mature", {})
    min_employees = t.get("min_employees", 15)
    max_velocity_variance = t.get("max_velocity_variance", 10)
    max_blocked_ratio = t.get("max_blocked_ratio", 5)

    employees = metrics["employee_count"]
    velocity_variance = metrics["velocity_variance"]
    blocked_ratio = metrics["blocked_ratio"]

    reasons = []
    confidence = 0.0

    # Check conditions
    employee_match = employees >= min_employees
    variance_match = velocity_variance <= max_velocity_variance
    blocked_match = blocked_ratio < max_blocked_ratio

    if employee_match:
        reasons.append(f"employees ({employees}) >= {min_employees}")
        confidence += 0.33

    if variance_match:
        reasons.append(
            f"velocity_variance ({velocity_variance:.1f}%) <= {max_velocity_variance}%"
        )
        confidence += 0.33

    if blocked_match:
        reasons.append(f"blocked_ratio ({blocked_ratio:.1f}%) < {max_blocked_ratio}%")
        confidence += 0.34

    matches = employee_match and variance_match and blocked_match
    return matches, confidence if matches else 0.0, reasons


def check_decline_pivot_phase(
    metrics: dict, thresholds: dict
) -> tuple[bool, float, list[str]]:
    """
    Check if company is in decline/pivot phase.
    Returns (matches, confidence, reasons).

    Any single condition triggers this phase (OR logic).
    """
    t = thresholds.get("decline_pivot", {})
    velocity_decline_threshold = t.get("velocity_decline_threshold", 20)
    max_blocked_ratio = t.get("max_blocked_ratio", 25)
    max_stalled_ratio = t.get("max_stalled_ratio", 50)

    velocity_decline = metrics["velocity_decline"]
    blocked_ratio = metrics["blocked_ratio"]
    stalled_ratio = metrics["stalled_ratio"]

    reasons = []
    confidence = 0.0

    # Check conditions (OR logic - any single condition triggers)
    decline_match = velocity_decline > velocity_decline_threshold
    blocked_match = blocked_ratio > max_blocked_ratio
    stalled_match = stalled_ratio > max_stalled_ratio

    matches = decline_match or blocked_match or stalled_match

    if decline_match:
        reasons.append(
            f"velocity_decline ({velocity_decline:.1f}%) > {velocity_decline_threshold}%"
        )
        confidence += 0.4

    if blocked_match:
        reasons.append(f"blocked_ratio ({blocked_ratio:.1f}%) > {max_blocked_ratio}%")
        confidence += 0.3

    if stalled_match:
        reasons.append(f"stalled_ratio ({stalled_ratio:.1f}%) > {max_stalled_ratio}%")
        confidence += 0.3

    return matches, min(confidence, 1.0) if matches else 0.0, reasons


def detect_phase(metrics: dict | None = None) -> dict:
    """
    Detect the current company phase based on metrics.

    Returns dict with:
    - phase: detected phase name
    - metrics: current metrics used for detection
    - confidence: 0.0-1.0 confidence score
    - transition_suggested: suggested next phase if any
    - reasons: list of reasons for the detection
    - all_phases: dict of all phases and their match status
    """
    if metrics is None:
        metrics = collect_all_metrics()

    thresholds = get_thresholds()

    # Check all phases
    phase_checks = {
        "startup": check_startup_phase(metrics, thresholds),
        "growth": check_growth_phase(metrics, thresholds),
        "scale": check_scale_phase(metrics, thresholds),
        "mature": check_mature_phase(metrics, thresholds),
        "decline_pivot": check_decline_pivot_phase(metrics, thresholds),
    }

    # Find matching phases
    matching_phases = []
    for phase_name in PHASE_PRIORITY:
        matches, confidence, reasons = phase_checks[phase_name]
        if matches:
            matching_phases.append(
                {
                    "phase": phase_name,
                    "confidence": confidence,
                    "reasons": reasons,
                }
            )

    # Determine detected phase (priority order handles conflicts)
    if matching_phases:
        detected = matching_phases[0]
    else:
        # Default to startup if nothing matches
        detected = {
            "phase": "startup",
            "confidence": 0.3,
            "reasons": ["no clear phase match, defaulting to startup"],
        }

    # Build all_phases summary
    all_phases = {}
    for phase_name, (matches, confidence, reasons) in phase_checks.items():
        all_phases[phase_name] = {
            "matches": matches,
            "confidence": confidence,
            "reasons": reasons,
        }

    # Suggest transition
    transition_suggested = suggest_transition(detected["phase"], metrics, thresholds)

    return {
        "phase": detected["phase"],
        "metrics": metrics,
        "confidence": detected["confidence"],
        "transition_suggested": transition_suggested,
        "reasons": detected["reasons"],
        "all_phases": all_phases,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def suggest_transition(
    current_phase: str, metrics: dict, thresholds: dict
) -> dict | None:
    """
    Suggest a phase transition based on current metrics.

    Returns dict with suggested phase and required changes, or None.
    """
    suggestions = {
        "startup": {
            "next": "growth",
            "requirements": [
                f"Hire to reach {thresholds['growth']['min_employees']} employees",
                "Establish consistent task velocity",
            ],
        },
        "growth": {
            "next": "scale",
            "requirements": [
                f"Hire to reach {thresholds['scale']['min_employees']} employees",
                f"Achieve {thresholds['scale']['min_test_coverage']}% test coverage",
                f"Reduce blocked ratio below {thresholds['scale']['max_blocked_ratio']}%",
            ],
        },
        "scale": {
            "next": "mature",
            "requirements": [
                f"Stabilize velocity (variance < {thresholds['mature']['max_velocity_variance']}%)",
                f"Reduce blocked ratio below {thresholds['mature']['max_blocked_ratio']}%",
            ],
        },
        "mature": {
            "next": None,
            "requirements": ["Maintain current operational excellence"],
        },
        "decline_pivot": {
            "next": "growth",
            "requirements": [
                "Address velocity decline",
                f"Reduce blocked ratio below {thresholds['growth'].get('max_blocked_ratio', 15)}%",
                "Unblock stalled tasks",
                "Consider pivoting strategy",
            ],
        },
    }

    suggestion = suggestions.get(current_phase)
    if not suggestion or suggestion["next"] is None:
        return None

    # Calculate progress toward next phase
    progress = calculate_transition_progress(
        current_phase, suggestion["next"], metrics, thresholds
    )

    return {
        "current_phase": current_phase,
        "suggested_phase": suggestion["next"],
        "requirements": suggestion["requirements"],
        "progress_percent": progress,
    }


def calculate_transition_progress(
    current: str, target: str, metrics: dict, thresholds: dict
) -> float:
    """Calculate progress percentage toward the target phase."""
    if target == "growth":
        t = thresholds["growth"]
        employee_progress = min(metrics["employee_count"] / t["min_employees"], 1.0)
        velocity_progress = min(metrics["velocity"] / max(t["min_velocity"], 0.1), 1.0)
        return round((employee_progress + velocity_progress) / 2 * 100, 1)

    if target == "scale":
        t = thresholds["scale"]
        employee_progress = min(metrics["employee_count"] / t["min_employees"], 1.0)
        coverage_progress = min(metrics["test_coverage"] / t["min_test_coverage"], 1.0)
        blocked_progress = 1.0 - min(
            metrics["blocked_ratio"] / t["max_blocked_ratio"], 1.0
        )
        return round(
            (employee_progress + coverage_progress + blocked_progress) / 3 * 100, 1
        )

    if target == "mature":
        t = thresholds["mature"]
        variance_progress = 1.0 - min(
            metrics["velocity_variance"] / t["max_velocity_variance"], 1.0
        )
        blocked_progress = 1.0 - min(
            metrics["blocked_ratio"] / t["max_blocked_ratio"], 1.0
        )
        return round((variance_progress + blocked_progress) / 2 * 100, 1)

    return 0.0


# -----------------------------------------------------------------------------
# CLI commands
# -----------------------------------------------------------------------------


def cmd_detect() -> dict:
    """Run phase detection and return results."""
    return detect_phase()


def cmd_metrics() -> dict:
    """Return current metrics used for phase detection."""
    metrics = collect_all_metrics()
    return {
        "success": True,
        "metrics": metrics,
        "thresholds": get_thresholds(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def cmd_suggest() -> dict:
    """Return suggested phase transition with reasoning."""
    detection = detect_phase()

    return {
        "success": True,
        "current_phase": detection["phase"],
        "confidence": detection["confidence"],
        "transition": detection["transition_suggested"],
        "reasons": detection["reasons"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def print_help():
    """Print usage help."""
    help_text = """
Company Phase Detection Utility

Detects organizational lifecycle phase from metrics:
- startup: employees < 5 AND completed_tasks < 20
- growth: employees >= 5 AND < 15 AND velocity > 0
- scale: employees >= 15 AND test_coverage >= 70% AND blocked_ratio < 10%
- mature: employees >= 15 AND velocity stable AND blocked_ratio < 5%
- decline_pivot: velocity declining OR blocked_ratio > 25% OR stalled_tasks > 50%

Commands:
    detect    Detect current phase (JSON output)
    metrics   Show current metrics
    suggest   Suggest phase transition

Options:
    --help    Show this help message

Reads from:
    .company/org.json        Employee count, agents
    .company/work_queue.json Task states, blocked ratio
    .company/metrics.json    Velocity data, test coverage
    .company/config.json     Custom thresholds (optional)
    .planning/STATE.md       Completed task count

Custom thresholds in config.json (two formats supported):

1. Simplified lifecycle format (recommended):
{
    "lifecycle": {
        "checkInterval": "per-wave",
        "metricsRetention": 30,
        "thresholds": {
            "startup_to_growth_employees": 5,
            "growth_to_scale_employees": 15,
            "scale_to_mature_velocity_variance": 10,
            "decline_velocity_drop": 20,
            "decline_blocked_ratio": 25
        }
    }
}

2. Legacy phase_detection format (detailed):
{
    "phase_detection": {
        "startup": {"max_employees": 5, "max_completed_tasks": 20},
        "growth": {"min_employees": 5, "max_employees": 15, "min_velocity": 0.1},
        ...
    }
}

Output: JSON with phase, metrics, confidence (0.0-1.0), transition_suggested
"""
    print(help_text.strip())


def main():
    """CLI entry point."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    command = args[0].lower()

    try:
        if command == "detect":
            result = cmd_detect()
            print(json.dumps(result, indent=2, default=str))

        elif command == "metrics":
            result = cmd_metrics()
            print(json.dumps(result, indent=2, default=str))

        elif command == "suggest":
            result = cmd_suggest()
            print(json.dumps(result, indent=2, default=str))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                indent=2,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
