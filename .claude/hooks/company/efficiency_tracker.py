#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
G6 Economics — Efficiency Optimization & Value Maximization.

Core efficiency measurement and optimization engine for company operations.
Tracks employee efficiency, learns patterns, and optimizes task routing.

Efficiency Formula:
    Efficiency Score = (Value Delivered / Resources Used) × Quality Factor

    Where:
    - Value Delivered = task_complexity_weight × completion_rate
    - Resources Used = context_tokens + execution_tokens + retry_tokens
    - Quality Factor = first_pass_success_rate × (1 - escalation_rate)

Key Functions:
    record_task_execution(task_id, employee_id, metrics) — Log execution data
    calculate_efficiency_score(employee_id) — Compute efficiency rating
    get_efficiency_insights() — Analyze patterns, return recommendations
    suggest_optimal_employee(task) — Route task to most efficient worker
    get_memory_hit_rate() — Track memory sharing effectiveness
    optimize_agent_routing() — Learn and improve routing decisions

Storage:
    - Per-employee efficiency: .company/org.json (efficiency field per employee)
    - Learning data: .company/efficiency_data.json
    - Organization economics: .company/org.json (economics section)

Usage:
    # Record task execution
    python efficiency_tracker.py record --task-id "task-123" --employee-id "senior-python-dev" \\
        --duration 45 --complexity standard --success true

    # Get employee efficiency
    python efficiency_tracker.py employee --employee-id "senior-python-dev"

    # Get company efficiency report
    python efficiency_tracker.py report

    # Get efficiency insights
    python efficiency_tracker.py insights

    # Suggest optimal employee for task
    python efficiency_tracker.py suggest --capabilities "python,testing"

    # Run optimization analysis
    python efficiency_tracker.py optimize

    # Show help
    python efficiency_tracker.py help
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

EFFICIENCY_DATA_FILE = "state/efficiency_data.json"
ORG_FILE = "org.json"
ROLLING_WINDOW_DAYS = 30

# Complexity weights for value calculation
COMPLEXITY_WEIGHTS = {
    "trivial": 0.5,
    "standard": 1.0,
    "complex": 2.0,
    "epic": 4.0,
}

# Default efficiency thresholds
EFFICIENCY_THRESHOLDS = {
    "excellent": 1.2,  # >20% better than expected
    "good": 1.0,  # Meeting expectations
    "fair": 0.8,  # 80% of expected
    "poor": 0.6,  # 60% of expected
}

# -----------------------------------------------------------------------------
# Cost Tracking Configuration (G6 Economics)
# -----------------------------------------------------------------------------

# Token pricing per 1M tokens (USD) - Claude model family pricing
# Sonnet 5's introductory $2/$10 rate was made permanent on 2026-08-11; Anthropic
# cancelled the increase to $3/$15 that had been planned for 2026-09-01. Do not
# reintroduce a scheduled bump — see
# https://www.anthropic.com/news/claude-sonnet-5 (checked 2026-09-05).
TOKEN_PRICING = {
    # Legacy family-level keys (backward compat for existing callers)
    "claude-opus-4": {"input": 5.00, "output": 25.00},  # Opus 4.x current rate
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4": {"input": 1.00, "output": 5.00},  # Haiku 4.5 rate
    # Specific model IDs used in gatesmith.json
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    # Specific model IDs used in model_profiles.py
    "claude-opus-4-5-20251101": {"input": 15.00, "output": 75.00},  # Pre-4.8 Opus rate
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    # Current-generation models
    "claude-sonnet-5": {
        "input": 2.00,
        "output": 10.00,
    },  # Permanent rate — see TOKEN_PRICING comment above
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-fable-5": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    # Opus 5.5 (2026-09-22): ~20% below Opus 5 ($5/$25) per launch coverage.
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    # Same base rate as claude-fable-5. Its cache-hit reads are billed at
    # $0.25/MTok (0.025x input) vs. the 0.1x multiplier every other tier uses —
    # not modeled here, since neither tracker's cost calc has a cache-read/
    # cache-creation split to apply it to (flat 50/50 input/output estimate).
    # Not referenced by any gatesmith.json profile yet — see
    # docs/claude-fable-5-1-model-tier-assessment.md.
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    # The 5.5 line Gatesmith's profiles use (2026-10-31): Sonnet 5.5 keeps Sonnet
    # 5's rate; Haiku 5.5 is $0.10/$0.50 up to 100K-token prompts.
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "default": {"input": 3.00, "output": 15.00},
}

# Model to use for cost estimation (can be overridden)
DEFAULT_MODEL = "claude-sonnet-5-5"


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class TaskExecution:
    """Record of a single task execution."""

    task_id: str
    employee_id: str
    timestamp: str
    duration_minutes: float
    complexity: str
    success: bool
    first_pass: bool = True
    retry_count: int = 0
    escalated: bool = False
    context_reused: bool = False
    pattern_tags: list[str] = field(default_factory=list)


@dataclass
class EmployeeEfficiency:
    """Efficiency metrics for an employee."""

    employee_id: str
    score: float
    tasks_completed: int
    tasks_by_complexity: dict[str, int]
    avg_execution_efficiency: float
    memory_hit_rate: float
    first_pass_success_rate: float
    strengths: list[str]
    improvement_areas: list[str]
    trend: str  # "improving" | "stable" | "declining"
    last_updated: str


@dataclass
class TaskPattern:
    """Learned pattern for optimal task routing."""

    pattern: str
    optimal_employee: str
    avg_efficiency: float
    sample_size: int
    last_updated: str


@dataclass
class Optimization:
    """Applied optimization record."""

    id: str
    type: str  # "routing" | "memory" | "model"
    description: str
    measured_improvement: str
    applied_at: str
    active: bool = True


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_efficiency_data_path() -> Path:
    """Get the efficiency data file path."""
    return get_company_dir() / EFFICIENCY_DATA_FILE


def get_org_path() -> Path:
    """Get the org.json file path."""
    return get_company_dir() / ORG_FILE


def ensure_company_dir():
    """Ensure company directory and state subdirectory exist."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "state").mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Storage Functions
# -----------------------------------------------------------------------------


def get_empty_efficiency_data() -> dict:
    """Return empty efficiency data structure."""
    return {
        "task_executions": [],
        "executive_sessions": [],  # WS-009-002: Track executive session tokens
        "task_patterns": [],
        "memory_patterns": [],
        "optimizations": [],
        "token_usage": {  # WS-009-002: Aggregate token tracking
            "daily": {},  # {"2026-02-13": {"executive": 48000, "task": 12000, "total": 60000}}
            "totals": {
                "executive_tokens": 0,
                "task_tokens": 0,
                "total_tokens": 0,
            },
        },
        "cost_tracking": {  # G6 Economics: Per-task/session cost tracking
            "daily": {},  # {"2026-02-14": {"task_cost_usd": 0.05, ...}}
            "by_task": {},  # {"task-123": {"cost_usd": 0.02, ...}}
            "by_session": {},  # {"sess-001": {"cost_usd": 0.08, ...}}
            "by_employee": {},  # {"senior-dev": {"total_cost_usd": 1.50}}
            "totals": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "task_cost_usd": 0.0,
                "session_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            },
        },
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "rolling_window_days": ROLLING_WINDOW_DAYS,
            "version": "1.2",  # Bumped for cost tracking
        },
    }


def load_efficiency_data() -> dict:
    """Load efficiency data from file."""
    path = get_efficiency_data_path()

    if not path.exists():
        return get_empty_efficiency_data()

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return get_empty_efficiency_data()


def save_efficiency_data(data: dict):
    """Save efficiency data to file with rolling window cleanup."""
    ensure_company_dir()
    path = get_efficiency_data_path()

    # Apply rolling window cleanup
    cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
    cutoff_str = cutoff.isoformat()
    cutoff_date = cutoff.strftime("%Y-%m-%d")

    # Clean up old task executions
    data["task_executions"] = [
        te
        for te in data.get("task_executions", [])
        if te.get("timestamp", "") >= cutoff_str
    ]

    # Clean up old executive sessions (WS-009-002)
    data["executive_sessions"] = [
        es
        for es in data.get("executive_sessions", [])
        if es.get("timestamp", "") >= cutoff_str
    ]

    # Clean up old daily token entries (WS-009-002)
    if "token_usage" in data and "daily" in data["token_usage"]:
        data["token_usage"]["daily"] = {
            date: tokens
            for date, tokens in data["token_usage"]["daily"].items()
            if date >= cutoff_date
        }

    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_org() -> dict:
    """Load organization data from org.json.

    On read failure (concurrent write, corruption), retries once after a short
    delay rather than silently returning the default empty structure.
    """
    import time

    path = get_org_path()

    if not path.exists():
        return {
            "company": {"name": "Unknown"},
            "employees": [],
            "economics": {},
        }

    for attempt in range(2):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Sanity: if file existed but parsed to empty/no-employees,
            # it might be a mid-write artifact. Retry once.
            if attempt == 0 and not data.get("employees") and path.stat().st_size > 50:
                time.sleep(0.05)
                continue
            # Normalize bare-string employees to dict records (ProjectK fix).
            try:
                from . import company_resolver as cr
            except ImportError:
                import company_resolver as cr  # type: ignore[no-redef]
            return cr.normalize_org_employees(data, path.parent)
        except (json.JSONDecodeError, OSError):
            if attempt == 0:
                time.sleep(0.05)  # Brief pause for concurrent write to finish
                continue
            return {
                "company": {"name": "Unknown"},
                "employees": [],
                "economics": {},
            }
    # Should not reach here, but safety fallback
    return {
        "company": {"name": "Unknown"},
        "employees": [],
        "economics": {},
    }


def save_org(org: dict):
    """Save organization data to org.json.

    Safety: Refuses to save if it would wipe existing employees.
    Uses atomic writes (write to temp + os.replace) to prevent corruption
    from concurrent reads during write.
    """
    import os
    import tempfile

    ensure_company_dir()
    path = get_org_path()

    # Safety check: Don't wipe employees if file already has them
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            # Block saves that would wipe existing employees
            if len(existing_employees) > 0 and len(new_employees) == 0:
                import sys

                print(
                    f"[SAFETY] Blocked save_org: Would wipe {len(existing_employees)} employees. "
                    "This is likely a bug in the calling code.",
                    file=sys.stderr,
                )
                return  # Refuse to save
        except (json.JSONDecodeError, OSError):
            # If we can't read existing file and trying to save empty employees, block
            if len(org.get("employees", [])) == 0:
                import sys

                print(
                    "[SAFETY] Blocked save_org: Cannot read existing file and new data has no employees. "
                    "This could cause data loss.",
                    file=sys.stderr,
                )
                return  # Refuse to save

    # Atomic write: write to temp file, then os.replace (prevents truncation race)
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix="org_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on any error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# -----------------------------------------------------------------------------
# Recording Functions
# -----------------------------------------------------------------------------


def record_task_execution(
    task_id: str,
    employee_id: str,
    duration_minutes: float,
    complexity: str = "standard",
    success: bool = True,
    first_pass: bool = True,
    retry_count: int = 0,
    escalated: bool = False,
    context_reused: bool = False,
    pattern_tags: list[str] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    model: str | None = None,
    quality_score: float | None = None,  # P26: Quality score from manager review
    proposal_accepted: bool
    | None = None,  # P26: Whether employee proposal was accepted
) -> dict:
    """
    Record a task execution event.

    Args:
        task_id: The completed task ID
        employee_id: Employee who executed the task
        duration_minutes: Time to complete in minutes
        complexity: Task complexity (trivial, standard, complex, epic)
        success: Whether task completed successfully
        first_pass: Whether task succeeded on first attempt
        retry_count: Number of retry attempts
        escalated: Whether task was escalated
        context_reused: Whether shared context was reused
        pattern_tags: Tags for pattern matching
        input_tokens: Optional input tokens for cost tracking
        output_tokens: Optional output tokens for cost tracking
        model: Optional model name for pricing
        quality_score: P26 - Quality score from manager review (0.0-1.0)
        proposal_accepted: P26 - Whether employee-proposed task was accepted

    Returns:
        Dict with recording result
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc).isoformat()

    execution = {
        "task_id": task_id,
        "employee_id": employee_id,
        "timestamp": now,
        "duration_minutes": duration_minutes,
        "complexity": complexity,
        "success": success,
        "first_pass": first_pass,
        "retry_count": retry_count,
        "escalated": escalated,
        "context_reused": context_reused,
        "pattern_tags": pattern_tags or [],
        # P26: Quality and initiative metrics
        "quality_score": quality_score,
        "proposal_accepted": proposal_accepted,
    }

    data.setdefault("task_executions", []).append(execution)
    save_efficiency_data(data)

    # Update employee efficiency in org.json
    _update_employee_efficiency(employee_id)

    # Update organization economics
    _update_economics_metrics(execution)

    # Auto-record goal costs for any goal tags (G1, G2, etc.)
    goals_recorded = _auto_record_goal_costs(
        task_id=task_id,
        employee_id=employee_id,
        duration_minutes=duration_minutes,
        complexity=complexity,
        pattern_tags=pattern_tags or [],
    )

    # Record cost if token information provided
    cost_recorded = None
    if input_tokens is not None and output_tokens is not None:
        cost_result = record_task_cost(
            task_id=task_id,
            employee_id=employee_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model or DEFAULT_MODEL,
        )
        cost_recorded = cost_result.get("cost_usd")

    return {
        "success": True,
        "recorded": execution,
        "goals_attributed": goals_recorded,
        "cost_usd": cost_recorded,
        "message": f"Task execution recorded for {task_id} by {employee_id}",
    }


# -----------------------------------------------------------------------------
# Token Tracking (WS-009-002)
# -----------------------------------------------------------------------------

# Token estimation constants (approximate, based on Claude tokenizer)
TOKENS_PER_CHAR_INPUT = 0.25  # ~4 chars per token for input
TOKENS_PER_CHAR_OUTPUT = 0.3  # ~3.3 chars per token for output (more structured)


def estimate_tokens(input_chars: int, output_chars: int) -> int:
    """Estimate token count from character counts."""
    input_tokens = int(input_chars * TOKENS_PER_CHAR_INPUT)
    output_tokens = int(output_chars * TOKENS_PER_CHAR_OUTPUT)
    return input_tokens + output_tokens


def get_queue_pending_count() -> int | None:
    """
    Get the count of pending tasks in the work queue.

    P18: Used to capture queue state at executive session start for interval learning.

    Returns:
        Number of pending tasks, or None if queue cannot be read.
    """
    queue_path = get_company_dir() / WORK_QUEUE_FILE
    if not queue_path.exists():
        return None
    try:
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)
        # Count pending + in_progress (actionable work)
        pending = len(queue.get("pending", []))
        in_progress = len(queue.get("in_progress", []))
        return pending + in_progress
    except (json.JSONDecodeError, OSError):
        return None


def record_executive_session(
    executive_id: str,
    trigger: str,
    duration_seconds: float,
    prompt_chars: int,
    output_chars: int,
    decisions_count: int = 0,
    work_submitted: int = 0,
    success: bool = True,
    model: str | None = None,
    queue_pending_at_start: int | None = None,
) -> dict:
    """
    Record an executive session with token estimates and costs.

    Args:
        executive_id: Executive who was activated (e.g., "forge-ceo")
        trigger: What triggered the session ("scheduled", "empty_queue", "manual")
        duration_seconds: How long the session took
        prompt_chars: Character count of the prompt sent
        output_chars: Character count of the response
        decisions_count: Number of decisions made
        work_submitted: Number of work items submitted to queue
        success: Whether session completed successfully
        model: Optional model name for cost calculation
        queue_pending_at_start: Number of pending tasks when session started (P18)

    Returns:
        Dict with recording result including token estimate and cost
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")

    # Estimate tokens
    estimated_tokens = estimate_tokens(prompt_chars, output_chars)

    session = {
        "executive_id": executive_id,
        "timestamp": timestamp,
        "trigger": trigger,
        "duration_seconds": duration_seconds,
        "prompt_chars": prompt_chars,
        "output_chars": output_chars,
        "estimated_tokens": estimated_tokens,
        "decisions_count": decisions_count,
        "work_submitted": work_submitted,
        "success": success,
        "queue_pending_at_start": queue_pending_at_start,  # P18: for interval learning
    }

    # P18.3: Score session value for adaptive interval learning
    try:
        from . import interval_learner
    except ImportError:
        try:
            import interval_learner  # type: ignore[no-redef]
        except ImportError:
            interval_learner = None  # type: ignore[assignment]

    if interval_learner is not None:
        try:
            score_result = interval_learner.score_session(session)
            session["value_score"] = score_result.value_score
        except Exception:
            session["value_score"] = None  # Scoring failed, continue without score
    else:
        session["value_score"] = None  # Module unavailable

    # Record session
    data.setdefault("executive_sessions", []).append(session)

    # Update daily and total token usage
    token_usage = data.setdefault(
        "token_usage",
        {
            "daily": {},
            "totals": {"executive_tokens": 0, "task_tokens": 0, "total_tokens": 0},
        },
    )

    # Update daily
    daily = token_usage.setdefault("daily", {})
    day_data = daily.setdefault(
        date_str, {"executive_tokens": 0, "task_tokens": 0, "total_tokens": 0}
    )
    day_data["executive_tokens"] += estimated_tokens
    day_data["total_tokens"] += estimated_tokens

    # Update totals
    totals = token_usage.setdefault(
        "totals", {"executive_tokens": 0, "task_tokens": 0, "total_tokens": 0}
    )
    totals["executive_tokens"] += estimated_tokens
    totals["total_tokens"] += estimated_tokens

    save_efficiency_data(data)

    # Record cost using estimated tokens
    # Estimate input/output tokens from character counts
    input_tokens_est = int(prompt_chars * TOKENS_PER_CHAR_INPUT)
    output_tokens_est = int(output_chars * TOKENS_PER_CHAR_OUTPUT)

    # Generate session ID from timestamp
    session_id = f"sess-{timestamp.replace(':', '-').replace('.', '-')}"

    cost_result = record_session_cost(
        session_id=session_id,
        executive_id=executive_id,
        input_tokens=input_tokens_est,
        output_tokens=output_tokens_est,
        model=model or DEFAULT_MODEL,
    )
    cost_usd = cost_result.get("cost_usd")

    return {
        "success": True,
        "recorded": session,
        "estimated_tokens": estimated_tokens,
        "cost_usd": cost_usd,
        "message": (
            f"Executive session recorded for {executive_id}: "
            f"~{estimated_tokens:,} tokens (${cost_usd:.6f})"
        ),
    }


def get_token_report() -> dict:
    """
    Get token usage report.

    Returns:
        Dict with token usage statistics
    """
    data = load_efficiency_data()

    # Get token usage data
    token_usage = data.get(
        "token_usage",
        {
            "daily": {},
            "totals": {"executive_tokens": 0, "task_tokens": 0, "total_tokens": 0},
        },
    )
    daily = token_usage.get("daily", {})
    totals = token_usage.get("totals", {})

    # Get executive sessions for detailed breakdown
    sessions = data.get("executive_sessions", [])

    # Calculate recent stats (last 7 days)
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    recent_days = {d: v for d, v in daily.items() if d >= week_ago}
    recent_total = sum(d.get("total_tokens", 0) for d in recent_days.values())
    recent_executive = sum(d.get("executive_tokens", 0) for d in recent_days.values())

    # Sessions by executive
    exec_breakdown = {}
    for sess in sessions:
        exec_id = sess.get("executive_id", "unknown")
        tokens = sess.get("estimated_tokens", 0)
        exec_breakdown[exec_id] = exec_breakdown.get(exec_id, 0) + tokens

    # Daily average
    days_with_data = len(daily) if daily else 1
    daily_avg = totals.get("total_tokens", 0) / days_with_data

    # Recent sessions (P18: includes queue_pending_at_start for interval learning)
    recent_sessions = sorted(
        sessions, key=lambda s: s.get("timestamp", ""), reverse=True
    )[:10]

    return {
        "totals": totals,
        "daily": daily,
        "recent_7_days": {
            "total_tokens": recent_total,
            "executive_tokens": recent_executive,
            "daily_average": recent_total / 7 if recent_total else 0,
        },
        "by_executive": exec_breakdown,
        "session_count": len(sessions),
        "daily_average_all_time": daily_avg,
        "recent_sessions": recent_sessions,  # P18: shows queue_pending_at_start
        "average_value_score": _calculate_avg_value_score(sessions),  # P18.3
        "generated_at": now.isoformat(),
    }


def get_scored_sessions(limit: int = 50) -> list[dict]:
    """
    Get most recent executive sessions with value scores.

    Returns sessions sorted by timestamp (most recent first), including
    value_score for each session. Sessions without a value_score will have
    None for that field.

    Args:
        limit: Maximum number of sessions to return (default 50)

    Returns:
        List of session dicts, each including value_score (or None if not scored)
    """
    data = load_efficiency_data()
    sessions = data.get("executive_sessions", [])

    # Sort by timestamp descending (most recent first)
    sorted_sessions = sorted(
        sessions, key=lambda s: s.get("timestamp", ""), reverse=True
    )

    # Return limited list with value_score ensured
    result = []
    for session in sorted_sessions[:limit]:
        # Ensure value_score key exists (None if not present)
        session_copy = dict(session)
        if "value_score" not in session_copy:
            session_copy["value_score"] = None
        result.append(session_copy)

    return result


def _calculate_avg_value_score(sessions: list[dict]) -> float | None:
    """
    Calculate average value score from sessions.

    Args:
        sessions: List of session dicts

    Returns:
        Average value_score, or None if no scored sessions exist
    """
    scores = [
        s.get("value_score") for s in sessions if s.get("value_score") is not None
    ]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 3)


# -----------------------------------------------------------------------------
# Cost Tracking (G6 Economics)
# -----------------------------------------------------------------------------


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_MODEL,
) -> float:
    """
    Calculate cost in USD for token usage.

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        model: Model name for pricing lookup

    Returns:
        Cost in USD (rounded to 6 decimal places)
    """
    pricing = TOKEN_PRICING.get(model, TOKEN_PRICING["default"])
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


def record_task_cost(
    task_id: str,
    employee_id: str,
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_MODEL,
) -> dict:
    """
    Record cost for a task execution.

    Args:
        task_id: Unique task identifier
        employee_id: Employee who executed the task
        input_tokens: Input tokens used
        output_tokens: Output tokens generated
        model: Model used for pricing

    Returns:
        Dict with recording result including cost
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Calculate cost
    cost_usd = calculate_cost(input_tokens, output_tokens, model)
    total_tokens = input_tokens + output_tokens

    # Initialize cost_tracking if not present
    cost_tracking = data.setdefault(
        "cost_tracking",
        {
            "daily": {},
            "by_task": {},
            "by_session": {},
            "by_employee": {},
            "totals": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "task_cost_usd": 0.0,
                "session_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            },
        },
    )

    # Record by task
    cost_tracking["by_task"][task_id] = {
        "employee_id": employee_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "model": model,
        "timestamp": now.isoformat(),
    }

    # Update daily
    daily = cost_tracking.setdefault("daily", {})
    day_data = daily.setdefault(
        date_str,
        {"task_cost_usd": 0.0, "session_cost_usd": 0.0, "total_usd": 0.0},
    )
    day_data["task_cost_usd"] = round(day_data["task_cost_usd"] + cost_usd, 6)
    day_data["total_usd"] = round(day_data["total_usd"] + cost_usd, 6)

    # Update by employee
    by_employee = cost_tracking.setdefault("by_employee", {})
    emp_data = by_employee.setdefault(
        employee_id,
        {"total_cost_usd": 0.0, "task_count": 0, "input_tokens": 0, "output_tokens": 0},
    )
    emp_data["total_cost_usd"] = round(emp_data["total_cost_usd"] + cost_usd, 6)
    emp_data["task_count"] += 1
    emp_data["input_tokens"] += input_tokens
    emp_data["output_tokens"] += output_tokens

    # Update totals
    totals = cost_tracking["totals"]
    totals["input_tokens"] += input_tokens
    totals["output_tokens"] += output_tokens
    totals["total_tokens"] += total_tokens
    totals["task_cost_usd"] = round(totals["task_cost_usd"] + cost_usd, 6)
    totals["total_cost_usd"] = round(totals["total_cost_usd"] + cost_usd, 6)

    save_efficiency_data(data)

    return {
        "success": True,
        "task_id": task_id,
        "employee_id": employee_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "model": model,
        "message": f"Task cost recorded: ${cost_usd:.6f} for {total_tokens:,} tokens",
    }


# ---------------------------------------------------------------------------
# Measured task token usage
#
# The agent provider is invoked in full agent mode without --output-format, so
# no usage figures reach the worker's stdout. Claude Code does, however, write
# a session transcript per working directory to
# ``~/.claude/projects/<slugified-cwd>/<session>.jsonl`` with a per-message
# ``usage`` block. The daemon gives every task attempt its own worktree, so the
# transcript directory name carries the task_id -- which is what makes a task's
# real consumption recoverable after the worker has exited.
# ---------------------------------------------------------------------------

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Cache tokens are billed relative to the base input rate, so they are folded
# into an input-equivalent count rather than counted as plain input (which
# would price an 8M-token cache read like 8M fresh input tokens).
CACHE_WRITE_INPUT_MULTIPLIER = 1.25
CACHE_READ_INPUT_MULTIPLIER = 0.10


@dataclass
class TokenUsage:
    """Measured token counts for one task execution."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    transcripts: int = 0

    @property
    def total_tokens(self) -> int:
        """Every token the task actually consumed."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def billable_input_tokens(self) -> int:
        """Input-equivalent tokens, with cache reads/writes priced against input."""
        return int(
            self.input_tokens
            + self.cache_creation_tokens * CACHE_WRITE_INPUT_MULTIPLIER
            + self.cache_read_tokens * CACHE_READ_INPUT_MULTIPLIER
        )


def _slugify_project_path(project_root: str | Path) -> str:
    """Mirror Claude Code's transcript directory naming for a working dir."""
    resolved = str(Path(project_root).resolve())
    return "".join(c if c.isalnum() else "-" for c in resolved)


def _transcript_dirs(
    task_id: str | None,
    project_root: str | Path | None,
    projects_dir: Path | None = None,
) -> list[Path]:
    """Transcript directories for a task, preferring an exact worktree match."""
    base = projects_dir or CLAUDE_PROJECTS_DIR
    if not base.is_dir():
        return []

    if project_root:
        exact = base / _slugify_project_path(project_root)
        if exact.is_dir():
            return [exact]

    if task_id:
        return sorted(p for p in base.glob(f"*{task_id}*") if p.is_dir())

    return []


def collect_task_token_usage(
    task_id: str | None = None,
    project_root: str | Path | None = None,
    since: float | None = None,
    projects_dir: Path | None = None,
) -> TokenUsage:
    """Sum measured token usage from a task's Claude Code session transcripts.

    Args:
        task_id: Task identifier, matched against transcript directory names.
        project_root: Worktree path, used for an exact directory match.
        since: Only read transcripts modified at/after this epoch time. Retries
            get a fresh worktree, so this keeps an earlier attempt's tokens from
            being counted again when a later attempt records.
        projects_dir: Override for the transcript root (tests).

    Returns:
        TokenUsage. All-zero when nothing could be measured -- never raises.
    """
    usage = TokenUsage()

    for directory in _transcript_dirs(task_id, project_root, projects_dir):
        for transcript in sorted(directory.glob("*.jsonl")):
            try:
                if since is not None and transcript.stat().st_mtime < since:
                    continue
                with open(transcript, encoding="utf-8") as handle:
                    lines = list(handle)
            except (OSError, ValueError):
                continue

            usage.transcripts += 1
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                counts = message.get("usage")
                if not isinstance(counts, dict):
                    continue
                usage.input_tokens += counts.get("input_tokens") or 0
                usage.output_tokens += counts.get("output_tokens") or 0
                usage.cache_read_tokens += counts.get("cache_read_input_tokens") or 0
                usage.cache_creation_tokens += (
                    counts.get("cache_creation_input_tokens") or 0
                )

    return usage


def record_task_tokens(
    task_id: str,
    employee_id: str,
    usage: TokenUsage,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Record measured token consumption and cost for one task execution."""
    if usage.total_tokens <= 0:
        return {"success": False, "reason": "no_usage_measured", "total_tokens": 0}

    data = load_efficiency_data()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    empty_totals = {"executive_tokens": 0, "task_tokens": 0, "total_tokens": 0}

    token_usage = data.setdefault("token_usage", {"daily": {}, "totals": {}})
    daily = token_usage.setdefault("daily", {})
    day_data = daily.setdefault(date_str, dict(empty_totals))
    day_data["task_tokens"] = day_data.get("task_tokens", 0) + usage.total_tokens
    day_data["total_tokens"] = day_data.get("total_tokens", 0) + usage.total_tokens

    totals = token_usage.setdefault("totals", dict(empty_totals))
    totals["task_tokens"] = totals.get("task_tokens", 0) + usage.total_tokens
    totals["total_tokens"] = totals.get("total_tokens", 0) + usage.total_tokens

    save_efficiency_data(data)

    cost = record_task_cost(
        task_id=task_id,
        employee_id=employee_id,
        input_tokens=usage.billable_input_tokens,
        output_tokens=usage.output_tokens,
        model=model,
    )

    return {
        "success": True,
        "task_id": task_id,
        "total_tokens": usage.total_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cost_usd": cost.get("cost_usd"),
        "transcripts": usage.transcripts,
    }


def record_session_cost(
    session_id: str,
    executive_id: str,
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_MODEL,
) -> dict:
    """
    Record cost for an executive session.

    Args:
        session_id: Unique session identifier
        executive_id: Executive who ran the session
        input_tokens: Input tokens used
        output_tokens: Output tokens generated
        model: Model used for pricing

    Returns:
        Dict with recording result including cost
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Calculate cost
    cost_usd = calculate_cost(input_tokens, output_tokens, model)
    total_tokens = input_tokens + output_tokens

    # Initialize cost_tracking if not present
    cost_tracking = data.setdefault(
        "cost_tracking",
        {
            "daily": {},
            "by_task": {},
            "by_session": {},
            "by_employee": {},
            "totals": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "task_cost_usd": 0.0,
                "session_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            },
        },
    )

    # Record by session
    cost_tracking["by_session"][session_id] = {
        "executive_id": executive_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "model": model,
        "timestamp": now.isoformat(),
    }

    # Update daily
    daily = cost_tracking.setdefault("daily", {})
    day_data = daily.setdefault(
        date_str,
        {"task_cost_usd": 0.0, "session_cost_usd": 0.0, "total_usd": 0.0},
    )
    day_data["session_cost_usd"] = round(day_data["session_cost_usd"] + cost_usd, 6)
    day_data["total_usd"] = round(day_data["total_usd"] + cost_usd, 6)

    # Update by employee (executives are also employees)
    by_employee = cost_tracking.setdefault("by_employee", {})
    emp_data = by_employee.setdefault(
        executive_id,
        {"total_cost_usd": 0.0, "task_count": 0, "input_tokens": 0, "output_tokens": 0},
    )
    emp_data["total_cost_usd"] = round(emp_data["total_cost_usd"] + cost_usd, 6)
    emp_data["input_tokens"] += input_tokens
    emp_data["output_tokens"] += output_tokens

    # Update totals
    totals = cost_tracking["totals"]
    totals["input_tokens"] += input_tokens
    totals["output_tokens"] += output_tokens
    totals["total_tokens"] += total_tokens
    totals["session_cost_usd"] = round(totals["session_cost_usd"] + cost_usd, 6)
    totals["total_cost_usd"] = round(totals["total_cost_usd"] + cost_usd, 6)

    save_efficiency_data(data)

    return {
        "success": True,
        "session_id": session_id,
        "executive_id": executive_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "model": model,
        "message": (
            f"Session cost recorded: ${cost_usd:.6f} for {total_tokens:,} tokens"
        ),
    }


def get_cost_summary() -> dict:
    """
    Get comprehensive cost summary.

    Returns:
        Dict with cost breakdown including:
        - Total costs (task vs session)
        - Daily costs
        - Per-employee costs
        - Cost trends
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)

    # Get cost tracking data
    cost_tracking = data.get(
        "cost_tracking",
        {
            "daily": {},
            "by_task": {},
            "by_session": {},
            "by_employee": {},
            "totals": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "task_cost_usd": 0.0,
                "session_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            },
        },
    )

    daily = cost_tracking.get("daily", {})
    by_task = cost_tracking.get("by_task", {})
    by_session = cost_tracking.get("by_session", {})
    by_employee = cost_tracking.get("by_employee", {})
    totals = cost_tracking.get("totals", {})

    # Calculate recent stats (last 7 days)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    recent_days = {d: v for d, v in daily.items() if d >= week_ago}
    recent_total = sum(d.get("total_usd", 0) for d in recent_days.values())
    recent_task = sum(d.get("task_cost_usd", 0) for d in recent_days.values())
    recent_session = sum(d.get("session_cost_usd", 0) for d in recent_days.values())

    # Calculate daily average
    days_with_data = len(daily) if daily else 1
    daily_avg = totals.get("total_cost_usd", 0) / days_with_data

    # Top spenders
    top_employees = sorted(
        by_employee.items(),
        key=lambda x: x[1].get("total_cost_usd", 0),
        reverse=True,
    )[:5]

    # Recent tasks with costs
    recent_tasks = sorted(
        by_task.items(),
        key=lambda x: x[1].get("timestamp", ""),
        reverse=True,
    )[:10]

    return {
        "totals": totals,
        "daily": daily,
        "recent_7_days": {
            "total_usd": round(recent_total, 6),
            "task_cost_usd": round(recent_task, 6),
            "session_cost_usd": round(recent_session, 6),
            "daily_average": round(recent_total / 7, 6) if recent_total else 0,
        },
        "by_employee": by_employee,
        "top_employees": [
            {"id": emp_id, **emp_data} for emp_id, emp_data in top_employees
        ],
        "task_count": len(by_task),
        "session_count": len(by_session),
        "recent_tasks": [
            {"task_id": task_id, **task_data} for task_id, task_data in recent_tasks
        ],
        "daily_average_all_time": round(daily_avg, 6),
        "generated_at": now.isoformat(),
    }


def get_cost_by_employee(employee_id: str) -> dict:
    """
    Get cost breakdown for a specific employee.

    Args:
        employee_id: Employee ID to query

    Returns:
        Dict with employee's cost metrics
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)

    cost_tracking = data.get("cost_tracking", {})
    by_employee = cost_tracking.get("by_employee", {})
    by_task = cost_tracking.get("by_task", {})

    # Get employee data
    emp_data = by_employee.get(
        employee_id,
        {"total_cost_usd": 0.0, "task_count": 0, "input_tokens": 0, "output_tokens": 0},
    )

    # Get tasks for this employee
    emp_tasks = [
        {"task_id": task_id, **task_data}
        for task_id, task_data in by_task.items()
        if task_data.get("employee_id") == employee_id
    ]

    # Sort by timestamp
    emp_tasks.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return {
        "employee_id": employee_id,
        "total_cost_usd": emp_data.get("total_cost_usd", 0.0),
        "task_count": emp_data.get("task_count", 0),
        "input_tokens": emp_data.get("input_tokens", 0),
        "output_tokens": emp_data.get("output_tokens", 0),
        "recent_tasks": emp_tasks[:10],
        "avg_cost_per_task": (
            round(emp_data.get("total_cost_usd", 0) / emp_data.get("task_count", 1), 6)
            if emp_data.get("task_count", 0) > 0
            else 0.0
        ),
        "generated_at": now.isoformat(),
    }


# -----------------------------------------------------------------------------
# Department Resource Allocation
# -----------------------------------------------------------------------------


def get_resource_allocation_by_department() -> dict:
    """
    Get resource allocation breakdown by department.

    Returns:
        Dict with resource allocation per department including:
        - task count
        - token usage (estimated)
        - efficiency metrics
        - employee utilization
    """
    org = load_org()
    data = load_efficiency_data()
    employees = org.get("employees", [])
    executions = data.get("task_executions", [])

    # Build employee-to-department mapping
    emp_to_dept: dict[str, str] = {}
    for emp in employees:
        emp_to_dept[emp.get("id", "")] = emp.get("department", "unassigned")

    # Aggregate by department
    dept_stats: dict[str, dict[str, Any]] = {}

    for exec_data in executions:
        emp_id = exec_data.get("employee_id", "unknown")
        dept = emp_to_dept.get(emp_id, "unassigned")

        if dept not in dept_stats:
            dept_stats[dept] = {
                "task_count": 0,
                "successful_tasks": 0,
                "total_duration_minutes": 0.0,
                "employees": set(),
                "complexity_distribution": {
                    "trivial": 0,
                    "standard": 0,
                    "complex": 0,
                    "epic": 0,
                },
            }

        stats = dept_stats[dept]
        stats["task_count"] += 1
        if exec_data.get("success", False):
            stats["successful_tasks"] += 1
        stats["total_duration_minutes"] += exec_data.get("duration_minutes", 0)
        stats["employees"].add(emp_id)

        complexity = exec_data.get("complexity", "standard")
        if complexity in stats["complexity_distribution"]:
            stats["complexity_distribution"][complexity] += 1

    # Calculate percentages and convert sets to counts
    total_tasks = sum(s["task_count"] for s in dept_stats.values())
    result = {}

    for dept, stats in dept_stats.items():
        task_count = stats["task_count"]
        successful = stats["successful_tasks"]

        result[dept] = {
            "task_count": task_count,
            "task_percentage": round(
                (task_count / total_tasks * 100) if total_tasks > 0 else 0, 1
            ),
            "successful_tasks": successful,
            "success_rate": round(
                (successful / task_count * 100) if task_count > 0 else 0, 1
            ),
            "total_duration_minutes": round(stats["total_duration_minutes"], 1),
            "employee_count": len(stats["employees"]),
            "complexity_distribution": stats["complexity_distribution"],
        }

    # Also include departments with no executions
    for emp in employees:
        dept = emp.get("department", "unassigned")
        if dept not in result:
            result[dept] = {
                "task_count": 0,
                "task_percentage": 0.0,
                "successful_tasks": 0,
                "success_rate": 0.0,
                "total_duration_minutes": 0.0,
                "employee_count": 1,
                "complexity_distribution": {
                    "trivial": 0,
                    "standard": 0,
                    "complex": 0,
                    "epic": 0,
                },
            }
        else:
            # Count employees in this dept
            emp_count = sum(1 for e in employees if e.get("department") == dept)
            result[dept]["employee_count"] = emp_count

    return {
        "departments": result,
        "total_tasks": total_tasks,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# Cost-per-Goal Tracking
# -----------------------------------------------------------------------------


def record_goal_cost(
    goal_id: str,
    task_id: str,
    employee_id: str,
    estimated_tokens: int,
    duration_minutes: float,
) -> dict:
    """
    Record cost attribution for a strategic goal.

    Args:
        goal_id: Goal identifier (e.g., "G1", "G6")
        task_id: Task that contributed to this goal
        employee_id: Employee who performed the work
        estimated_tokens: Token estimate for this work
        duration_minutes: Time spent on this work

    Returns:
        Dict with recording result
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)

    # Initialize goal_costs if not present
    if "goal_costs" not in data:
        data["goal_costs"] = {}

    # Initialize this goal's tracking if not present
    if goal_id not in data["goal_costs"]:
        data["goal_costs"][goal_id] = {
            "total_tokens": 0,
            "total_duration_minutes": 0.0,
            "task_count": 0,
            "contributions": [],
        }

    goal_data = data["goal_costs"][goal_id]
    goal_data["total_tokens"] += estimated_tokens
    goal_data["total_duration_minutes"] += duration_minutes
    goal_data["task_count"] += 1

    # Keep last 100 contributions per goal
    goal_data["contributions"].append(
        {
            "task_id": task_id,
            "employee_id": employee_id,
            "tokens": estimated_tokens,
            "duration_minutes": duration_minutes,
            "timestamp": now.isoformat(),
        }
    )
    goal_data["contributions"] = goal_data["contributions"][-100:]

    save_efficiency_data(data)

    return {
        "success": True,
        "goal_id": goal_id,
        "total_tokens": goal_data["total_tokens"],
        "total_duration_minutes": goal_data["total_duration_minutes"],
        "task_count": goal_data["task_count"],
    }


def get_cost_per_goal() -> dict:
    """
    Get cost tracking per strategic goal.

    Returns:
        Dict with cost breakdown per goal including:
        - Total tokens spent
        - Total time spent
        - Task count
        - Efficiency metrics
    """
    data = load_efficiency_data()
    goal_costs = data.get("goal_costs", {})

    result = {}
    total_tokens = 0
    total_duration = 0.0

    for goal_id, costs in goal_costs.items():
        tokens = costs.get("total_tokens", 0)
        duration = costs.get("total_duration_minutes", 0.0)
        task_count = costs.get("task_count", 0)

        total_tokens += tokens
        total_duration += duration

        # Calculate efficiency (tokens per task, time per task)
        tokens_per_task = tokens / task_count if task_count > 0 else 0
        time_per_task = duration / task_count if task_count > 0 else 0

        result[goal_id] = {
            "total_tokens": tokens,
            "total_duration_minutes": round(duration, 1),
            "task_count": task_count,
            "tokens_per_task": round(tokens_per_task, 0),
            "minutes_per_task": round(time_per_task, 1),
            "last_contribution": costs.get("contributions", [{}])[-1].get("timestamp"),
        }

    # Calculate percentages
    for goal_id in result:
        result[goal_id]["token_percentage"] = round(
            (result[goal_id]["total_tokens"] / total_tokens * 100)
            if total_tokens > 0
            else 0,
            1,
        )
        result[goal_id]["time_percentage"] = round(
            (result[goal_id]["total_duration_minutes"] / total_duration * 100)
            if total_duration > 0
            else 0,
            1,
        )

    return {
        "goals": result,
        "totals": {
            "total_tokens": total_tokens,
            "total_duration_minutes": round(total_duration, 1),
            "goal_count": len(result),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# Budget Utilization
# -----------------------------------------------------------------------------


def get_budget_utilization() -> dict:
    """
    Get budget utilization metrics.

    Compares actual resource usage against configured budgets from org.json.

    Returns:
        Dict with budget utilization including:
        - Monthly budget vs actual
        - Department allocations
        - Goal allocations
        - Projections
    """
    org = load_org()
    data = load_efficiency_data()
    token_usage = data.get("token_usage", {})
    daily_usage = token_usage.get("daily", {})
    totals = token_usage.get("totals", {})

    # Get budget config from org.json (with defaults)
    economics = org.get("economics", {})
    budget_config = economics.get(
        "budget",
        {
            "monthly_token_budget": 500000,  # Default 500K tokens/month
            "alert_threshold_percent": 80,
            "department_allocations": {},
            "goal_allocations": {},
        },
    )

    monthly_budget = budget_config.get("monthly_token_budget", 500000)
    alert_threshold = budget_config.get("alert_threshold_percent", 80)

    # Calculate current month usage
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    days_in_month = 30  # Approximate
    day_of_month = now.day

    # Sum up current month's daily usage
    current_month_usage = sum(
        v.get("total_tokens", 0)
        for k, v in daily_usage.items()
        if k.startswith(current_month)
    )

    # Calculate usage by category (executive vs task tokens)
    executive_tokens_month = sum(
        v.get("executive_tokens", 0)
        for k, v in daily_usage.items()
        if k.startswith(current_month)
    )
    task_tokens_month = sum(
        v.get("task_tokens", 0)
        for k, v in daily_usage.items()
        if k.startswith(current_month)
    )

    # Calculate projections
    if day_of_month > 0:
        daily_rate = current_month_usage / day_of_month
        projected_month_total = daily_rate * days_in_month
    else:
        daily_rate = 0
        projected_month_total = 0

    # Calculate utilization percentage
    utilization_percent = (
        (current_month_usage / monthly_budget * 100) if monthly_budget > 0 else 0
    )

    # Determine status
    if utilization_percent >= 100:
        status = "over_budget"
    elif utilization_percent >= alert_threshold:
        status = "at_risk"
    elif utilization_percent >= 50:
        status = "on_track"
    else:
        status = "under_utilized"

    # Get department allocations
    dept_allocation = get_resource_allocation_by_department()

    # Get goal costs
    goal_costs = get_cost_per_goal()

    # Calculate days until budget exhausted
    days_until_exhausted = None
    if daily_rate > 0 and current_month_usage < monthly_budget:
        days_until_exhausted = round(
            (monthly_budget - current_month_usage) / daily_rate
        )

    return {
        "budget": {
            "monthly_limit": monthly_budget,
            "current_usage": current_month_usage,
            "utilization_percent": round(utilization_percent, 1),
            "remaining": max(0, monthly_budget - current_month_usage),
            "status": status,
            "alert_threshold": alert_threshold,
        },
        "projection": {
            "daily_rate": round(daily_rate, 0),
            "projected_month_total": round(projected_month_total, 0),
            "projected_utilization_percent": round(
                (projected_month_total / monthly_budget * 100)
                if monthly_budget > 0
                else 0,
                1,
            ),
            "days_until_budget_exhausted": days_until_exhausted,
        },
        "period": {
            "month": current_month,
            "day_of_month": day_of_month,
            "days_remaining": days_in_month - day_of_month,
        },
        "by_category": {
            "executive_sessions": {
                "tokens": executive_tokens_month,
                "percentage": round(
                    (executive_tokens_month / current_month_usage * 100)
                    if current_month_usage > 0
                    else 0.0,
                    1,
                ),
            },
            "task_executions": {
                "tokens": task_tokens_month,
                "percentage": round(
                    (task_tokens_month / current_month_usage * 100)
                    if current_month_usage > 0
                    else 0.0,
                    1,
                ),
            },
            "other": {
                "tokens": max(
                    0, current_month_usage - executive_tokens_month - task_tokens_month
                ),
                "percentage": round(
                    (
                        max(
                            0,
                            current_month_usage
                            - executive_tokens_month
                            - task_tokens_month,
                        )
                        / current_month_usage
                        * 100
                    )
                    if current_month_usage > 0
                    else 0.0,
                    1,
                ),
            },
        },
        "by_department": dept_allocation.get("departments", {}),
        "by_goal": goal_costs.get("goals", {}),
        "totals": totals,
        "generated_at": now.isoformat(),
    }


def _update_employee_efficiency(employee_id: str):
    """Update employee efficiency metrics in org.json."""
    org = load_org()
    employees = org.get("employees", [])

    # Find employee
    employee = None
    for emp in employees:
        if emp.get("id") == employee_id:
            employee = emp
            break

    if employee is None:
        return  # Employee not found, skip update

    # Calculate efficiency
    efficiency_data = calculate_efficiency_score(employee_id)

    # Update employee efficiency field
    employee["efficiency"] = {
        "score": efficiency_data.get("score", 0.0),
        "tasks_completed": efficiency_data.get("tasks_completed", 0),
        "tasks_by_complexity": efficiency_data.get("tasks_by_complexity", {}),
        "avg_execution_efficiency": efficiency_data.get(
            "avg_execution_efficiency", 1.0
        ),
        "memory_hit_rate": efficiency_data.get("memory_hit_rate", 0.0),
        "first_pass_success_rate": efficiency_data.get("first_pass_success_rate", 0.0),
        "strengths": efficiency_data.get("strengths", []),
        "improvement_areas": efficiency_data.get("improvement_areas", []),
        "trend": efficiency_data.get("trend", "stable"),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        # P26: Quality and initiative metrics
        "avg_quality_score": efficiency_data.get("avg_quality_score", 0.0),
        "proposal_acceptance_rate": efficiency_data.get(
            "proposal_acceptance_rate", 0.0
        ),
        "proposals_submitted": efficiency_data.get("proposals_submitted", 0),
    }

    save_org(org)


def _update_economics_metrics(execution: dict):
    """Update organization economics metrics."""
    org = load_org()

    # Ensure economics section exists
    if "economics" not in org:
        org["economics"] = {}

    economics = org["economics"]

    # Initialize efficiency tracking if not present
    if "efficiency" not in economics:
        economics["efficiency"] = {
            "company_score": 0.0,
            "target_score": 0.90,
            "tracking_since": datetime.now(timezone.utc).isoformat(),
        }

    # Initialize learning section if not present
    if "learning" not in economics:
        economics["learning"] = {
            "patterns_discovered": 0,
            "optimizations_applied": 0,
            "last_analysis": None,
        }

    # Update company efficiency score
    company_efficiency = _calculate_company_efficiency()
    economics["efficiency"]["company_score"] = company_efficiency

    save_org(org)


def _auto_record_goal_costs(
    task_id: str,
    employee_id: str,
    duration_minutes: float,
    complexity: str,
    pattern_tags: list[str],
) -> list[str]:
    """
    Auto-record goal costs for any goal tags in pattern_tags.

    Looks for tags matching goal patterns (G1, G2, G5, G6, etc.)
    and automatically attributes costs to those goals.

    Args:
        task_id: Task ID
        employee_id: Employee who performed the work
        duration_minutes: Time spent on task
        complexity: Task complexity level
        pattern_tags: Tags that may contain goal identifiers

    Returns:
        List of goal IDs that were attributed
    """
    import re

    # Goal pattern: G followed by one or more digits (G1, G5, G10, etc.)
    goal_pattern = re.compile(r"^G\d+$", re.IGNORECASE)

    # Extract goal tags
    goal_tags = [tag.upper() for tag in pattern_tags if goal_pattern.match(tag)]

    if not goal_tags:
        return []

    # Estimate tokens based on complexity and duration
    # Rough estimate: 1000 base tokens + complexity multiplier * duration * 100
    complexity_weight = COMPLEXITY_WEIGHTS.get(complexity, 1.0)
    estimated_tokens = int(1000 + complexity_weight * duration_minutes * 100)

    # Divide cost equally among attributed goals
    tokens_per_goal = estimated_tokens // len(goal_tags) if goal_tags else 0
    duration_per_goal = duration_minutes / len(goal_tags) if goal_tags else 0

    goals_recorded = []
    for goal_id in goal_tags:
        record_goal_cost(
            goal_id=goal_id,
            task_id=task_id,
            employee_id=employee_id,
            estimated_tokens=tokens_per_goal,
            duration_minutes=duration_per_goal,
        )
        goals_recorded.append(goal_id)

    return goals_recorded


# -----------------------------------------------------------------------------
# Calculation Functions
# -----------------------------------------------------------------------------


def calculate_efficiency_score(employee_id: str) -> dict:
    """
    Calculate efficiency score for an employee.

    Efficiency Score = (Value Delivered / Resources Used) × Quality Factor

    Where:
    - Value Delivered = sum(complexity_weight × success)
    - Resources Used = sum(duration_minutes)
    - Quality Factor = first_pass_rate × (1 - escalation_rate)

    Args:
        employee_id: Employee ID to calculate for

    Returns:
        Dict with efficiency metrics
    """
    data = load_efficiency_data()
    executions = data.get("task_executions", [])

    # Filter executions for this employee
    employee_executions = [e for e in executions if e.get("employee_id") == employee_id]

    if not employee_executions:
        return {
            "employee_id": employee_id,
            "score": 0.0,
            "tasks_completed": 0,
            "tasks_by_complexity": {},
            "avg_execution_efficiency": 1.0,
            "memory_hit_rate": 0.0,
            "first_pass_success_rate": 0.0,
            "strengths": [],
            "improvement_areas": [],
            "trend": "stable",
            "insufficient_data": True,
        }

    # Calculate base metrics
    tasks_completed = len(employee_executions)
    successful_tasks = sum(1 for e in employee_executions if e.get("success", False))
    first_pass_tasks = sum(1 for e in employee_executions if e.get("first_pass", False))
    escalated_tasks = sum(1 for e in employee_executions if e.get("escalated", False))
    context_reused = sum(
        1 for e in employee_executions if e.get("context_reused", False)
    )

    # Tasks by complexity
    tasks_by_complexity: dict[str, int] = {}
    for e in employee_executions:
        complexity = e.get("complexity", "standard")
        tasks_by_complexity[complexity] = tasks_by_complexity.get(complexity, 0) + 1

    # Calculate value delivered (complexity-weighted success)
    value_delivered = 0.0
    for e in employee_executions:
        if e.get("success", False):
            complexity = e.get("complexity", "standard")
            weight = COMPLEXITY_WEIGHTS.get(complexity, 1.0)
            value_delivered += weight

    # Calculate resources used (total duration with retry penalty)
    total_duration = sum(e.get("duration_minutes", 0) for e in employee_executions)
    retry_penalty = sum(
        e.get("retry_count", 0) * 10 for e in employee_executions
    )  # 10 min penalty per retry
    resources_used = total_duration + retry_penalty

    # Calculate quality factor
    first_pass_rate = first_pass_tasks / tasks_completed if tasks_completed > 0 else 0.0
    escalation_rate = escalated_tasks / tasks_completed if tasks_completed > 0 else 0.0
    quality_factor = first_pass_rate * (1 - escalation_rate)

    # Calculate efficiency score
    if resources_used > 0 and tasks_completed > 0:
        raw_efficiency = (
            value_delivered / (resources_used / 60)
        ) * quality_factor  # Normalize to tasks/hour
        # Normalize to 0-2 scale where 1.0 is expected performance
        expected_value_per_hour = 1.0  # 1 standard task per hour expected
        score = (
            raw_efficiency / expected_value_per_hour
            if expected_value_per_hour > 0
            else 1.0
        )
    else:
        score = 1.0  # Default to expected performance

    # Memory hit rate
    memory_hit_rate = context_reused / tasks_completed if tasks_completed > 0 else 0.0

    # Determine strengths and improvement areas based on pattern tags
    pattern_counts: dict[str, int] = {}
    for e in employee_executions:
        for tag in e.get("pattern_tags", []):
            pattern_counts[tag] = pattern_counts.get(tag, 0) + 1

    # Top 3 patterns are strengths
    sorted_patterns = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)
    strengths = [p[0] for p in sorted_patterns[:3]]

    # Areas with low success rate are improvement areas
    improvement_areas = []
    if first_pass_rate < 0.8:
        improvement_areas.append("first-pass-success")
    if escalation_rate > 0.1:
        improvement_areas.append("escalation-reduction")
    if memory_hit_rate < 0.5:
        improvement_areas.append("context-reuse")

    # Calculate trend by comparing recent vs older executions
    trend = _calculate_efficiency_trend(employee_executions)

    # P26: Calculate quality and initiative metrics
    quality_scores = [
        e.get("quality_score")
        for e in employee_executions
        if e.get("quality_score") is not None
    ]
    avg_quality_score = (
        sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
    )

    # Count proposals (tasks with proposal_accepted field)
    proposals = [
        e for e in employee_executions if e.get("proposal_accepted") is not None
    ]
    proposals_submitted = len(proposals)
    proposals_accepted = sum(1 for p in proposals if p.get("proposal_accepted", False))
    proposal_acceptance_rate = (
        proposals_accepted / proposals_submitted if proposals_submitted > 0 else 0.0
    )

    # P26: Add quality to improvement areas
    if avg_quality_score > 0 and avg_quality_score < 0.7:
        improvement_areas.append("work-quality")
    if proposals_submitted > 0 and proposal_acceptance_rate < 0.5:
        improvement_areas.append("proposal-quality")

    return {
        "employee_id": employee_id,
        "score": round(score, 2),
        "tasks_completed": tasks_completed,
        "tasks_by_complexity": tasks_by_complexity,
        "avg_execution_efficiency": round(score, 2),
        "memory_hit_rate": round(memory_hit_rate, 2),
        "first_pass_success_rate": round(first_pass_rate, 2),
        "strengths": strengths,
        "improvement_areas": improvement_areas,
        "trend": trend,
        # P26: Quality and initiative metrics
        "avg_quality_score": round(avg_quality_score, 2),
        "proposal_acceptance_rate": round(proposal_acceptance_rate, 2),
        "proposals_submitted": proposals_submitted,
        "details": {
            "value_delivered": round(value_delivered, 2),
            "resources_used_minutes": round(resources_used, 2),
            "quality_factor": round(quality_factor, 2),
            "successful_tasks": successful_tasks,
            "escalated_tasks": escalated_tasks,
        },
    }


def _calculate_efficiency_trend(executions: list[dict]) -> str:
    """Calculate efficiency trend from executions."""
    if len(executions) < 4:
        return "stable"

    # Sort by timestamp
    sorted_execs = sorted(executions, key=lambda x: x.get("timestamp", ""))

    # Split into halves
    mid = len(sorted_execs) // 2
    first_half = sorted_execs[:mid]
    second_half = sorted_execs[mid:]

    # Calculate success rate for each half
    first_success = sum(1 for e in first_half if e.get("success", False)) / len(
        first_half
    )
    second_success = sum(1 for e in second_half if e.get("success", False)) / len(
        second_half
    )

    # Determine trend
    if second_success > first_success + 0.1:
        return "improving"
    elif second_success < first_success - 0.1:
        return "declining"
    else:
        return "stable"


def _calculate_company_efficiency() -> float:
    """Calculate company-wide efficiency score."""
    org = load_org()
    employees = org.get("employees", [])

    scores = []
    for emp in employees:
        efficiency = emp.get("efficiency", {})
        score = efficiency.get("score", 0.0)
        tasks = efficiency.get("tasks_completed", 0)
        if tasks > 0:
            scores.append((score, tasks))

    if not scores:
        return 0.0

    # Weighted average by tasks completed
    total_tasks = sum(t for _, t in scores)
    if total_tasks == 0:
        return 0.0

    weighted_sum = sum(s * t for s, t in scores)
    return round(weighted_sum / total_tasks, 2)


# -----------------------------------------------------------------------------
# Insights and Optimization Functions
# -----------------------------------------------------------------------------


def get_efficiency_insights() -> dict:
    """
    Analyze patterns and return efficiency insights.

    Returns:
        Dict with:
        - patterns: Discovered task routing patterns
        - recommendations: Optimization recommendations
        - top_performers: Most efficient employees
        - improvement_opportunities: Areas for improvement
    """
    data = load_efficiency_data()
    org = load_org()

    executions = data.get("task_executions", [])
    employees = org.get("employees", [])

    if not executions:
        return {
            "success": True,
            "patterns": [],
            "recommendations": [],
            "top_performers": [],
            "improvement_opportunities": [],
            "insufficient_data": True,
            "message": "Not enough data for insights. Complete more tasks.",
        }

    # Discover patterns by analyzing employee-pattern_tag correlations
    patterns = _discover_task_patterns(executions)

    # Find top performers
    top_performers = []
    for emp in employees:
        efficiency = emp.get("efficiency", {})
        if efficiency.get("tasks_completed", 0) >= 3:
            top_performers.append(
                {
                    "employee_id": emp.get("id"),
                    "name": emp.get("name"),
                    "score": efficiency.get("score", 0.0),
                    "tasks_completed": efficiency.get("tasks_completed", 0),
                    "strengths": efficiency.get("strengths", []),
                }
            )

    top_performers.sort(key=lambda x: x["score"], reverse=True)
    top_performers = top_performers[:5]

    # Generate recommendations
    recommendations = _generate_recommendations(executions, employees, patterns)

    # Find improvement opportunities
    improvement_opportunities = _find_improvement_opportunities(executions, employees)

    # Note: _update_learning_metrics() removed here — getter functions must be
    # read-only. Learning metrics are updated by optimize_agent_routing() instead.

    return {
        "success": True,
        "patterns": patterns,
        "recommendations": recommendations,
        "top_performers": top_performers,
        "improvement_opportunities": improvement_opportunities,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _discover_task_patterns(executions: list[dict]) -> list[dict]:
    """Discover task routing patterns from execution history."""
    # Group by pattern tag and employee
    tag_employee_stats: dict[str, dict[str, list[float]]] = {}

    for e in executions:
        if not e.get("success", False):
            continue

        employee_id = e.get("employee_id", "unknown")
        duration = e.get("duration_minutes", 0)
        complexity = e.get("complexity", "standard")
        weight = COMPLEXITY_WEIGHTS.get(complexity, 1.0)

        # Calculate efficiency for this execution
        if duration > 0:
            efficiency = weight / (duration / 60)  # Value per hour
        else:
            efficiency = 1.0

        for tag in e.get("pattern_tags", []):
            if tag not in tag_employee_stats:
                tag_employee_stats[tag] = {}
            if employee_id not in tag_employee_stats[tag]:
                tag_employee_stats[tag][employee_id] = []
            tag_employee_stats[tag][employee_id].append(efficiency)

    # Find optimal employee for each pattern
    patterns = []
    for pattern, employee_stats in tag_employee_stats.items():
        best_employee = None
        best_avg = 0.0
        total_samples = 0

        for employee_id, efficiencies in employee_stats.items():
            avg_eff = sum(efficiencies) / len(efficiencies)
            total_samples += len(efficiencies)
            if avg_eff > best_avg:
                best_avg = avg_eff
                best_employee = employee_id

        if best_employee and total_samples >= 2:
            patterns.append(
                {
                    "pattern": pattern,
                    "optimal_employee": best_employee,
                    "avg_efficiency": round(best_avg, 2),
                    "sample_size": total_samples,
                }
            )

    return patterns


def _generate_recommendations(
    executions: list[dict],
    employees: list[dict],
    patterns: list[dict],
) -> list[dict]:
    """Generate optimization recommendations."""
    recommendations = []

    # Recommendation: Route tasks to most efficient employees
    for pattern in patterns[:3]:
        recommendations.append(
            {
                "type": "routing",
                "priority": "high",
                "description": f"Route {pattern['pattern']} tasks to {pattern['optimal_employee']}",
                "expected_improvement": f"+{int((pattern['avg_efficiency'] - 1) * 100)}% efficiency",
                "confidence": "high" if pattern["sample_size"] >= 5 else "medium",
            }
        )

    # Recommendation: Improve context reuse
    context_reuse_rate = (
        sum(1 for e in executions if e.get("context_reused", False)) / len(executions)
        if executions
        else 0
    )

    if context_reuse_rate < 0.5:
        recommendations.append(
            {
                "type": "memory",
                "priority": "medium",
                "description": "Increase shared context usage to reduce token overhead",
                "expected_improvement": f"Current rate: {int(context_reuse_rate * 100)}%, target: 70%",
                "confidence": "high",
            }
        )

    # Recommendation: Address high escalation rates
    escalation_rate = (
        sum(1 for e in executions if e.get("escalated", False)) / len(executions)
        if executions
        else 0
    )

    if escalation_rate > 0.1:
        recommendations.append(
            {
                "type": "quality",
                "priority": "high",
                "description": "Reduce escalation rate through better task preparation",
                "expected_improvement": f"Current rate: {int(escalation_rate * 100)}%, target: <10%",
                "confidence": "medium",
            }
        )

    return recommendations


def _find_improvement_opportunities(
    executions: list[dict],
    employees: list[dict],
) -> list[dict]:
    """Find improvement opportunities."""
    opportunities = []

    # Find employees with declining trends
    for emp in employees:
        efficiency = emp.get("efficiency", {})
        if efficiency.get("trend") == "declining":
            opportunities.append(
                {
                    "type": "employee",
                    "employee_id": emp.get("id"),
                    "description": f"{emp.get('name')} showing declining efficiency",
                    "suggestion": "Review recent tasks for blockers or training needs",
                }
            )

    # Find underutilized employees
    for emp in employees:
        efficiency = emp.get("efficiency", {})
        tasks = efficiency.get("tasks_completed", 0)
        score = efficiency.get("score", 0.0)
        if tasks < 3 and score > 1.0:
            opportunities.append(
                {
                    "type": "utilization",
                    "employee_id": emp.get("id"),
                    "description": f"{emp.get('name')} has high efficiency but low task volume",
                    "suggestion": "Consider routing more matching tasks to this employee",
                }
            )

    return opportunities


def _update_learning_metrics(patterns_discovered: int, optimizations_applied: int):
    """Update learning metrics in org.json."""
    org = load_org()

    if "economics" not in org:
        org["economics"] = {}
    if "learning" not in org["economics"]:
        org["economics"]["learning"] = {}

    learning = org["economics"]["learning"]
    learning["patterns_discovered"] = patterns_discovered
    learning["last_analysis"] = datetime.now(timezone.utc).isoformat()

    if optimizations_applied > 0:
        learning["optimizations_applied"] = (
            learning.get("optimizations_applied", 0) + optimizations_applied
        )

    save_org(org)


# -----------------------------------------------------------------------------
# Task Routing Functions
# -----------------------------------------------------------------------------


def suggest_optimal_employee(
    required_capabilities: list[str] | None = None,
    complexity: str = "standard",
    pattern_tags: list[str] | None = None,
) -> dict:
    """
    Suggest the optimal employee for a task based on efficiency data.

    Args:
        required_capabilities: Required capabilities for the task
        complexity: Task complexity level
        pattern_tags: Tags that describe the task type

    Returns:
        Dict with suggested employee and reasoning
    """
    org = load_org()
    data = load_efficiency_data()
    employees = org.get("employees", [])
    patterns = data.get("task_patterns", [])

    # Filter employees by capabilities if specified
    candidates = []
    for emp in employees:
        if emp.get("status") != "available":
            continue

        emp_capabilities = emp.get("capabilities", [])
        if required_capabilities:
            if not all(cap in emp_capabilities for cap in required_capabilities):
                continue

        candidates.append(emp)

    if not candidates:
        return {
            "success": False,
            "reason": "no_matching_employees",
            "message": "No available employees match the required capabilities",
        }

    # Score candidates based on efficiency
    scored_candidates = []
    for emp in candidates:
        efficiency = emp.get("efficiency", {})
        base_score = efficiency.get("score", 1.0)
        tasks_completed = efficiency.get("tasks_completed", 0)

        # Boost score for pattern matches
        pattern_boost = 0.0
        strengths = efficiency.get("strengths", [])
        if pattern_tags:
            matching_strengths = sum(1 for tag in pattern_tags if tag in strengths)
            pattern_boost = matching_strengths * 0.1

        # Check learned patterns
        for pattern in patterns:
            if pattern.get("optimal_employee") == emp.get("id"):
                if pattern_tags and pattern.get("pattern") in pattern_tags:
                    pattern_boost += 0.2

        # Confidence factor based on data
        confidence = min(1.0, tasks_completed / 10)

        final_score = (base_score + pattern_boost) * (0.5 + 0.5 * confidence)

        scored_candidates.append(
            {
                "employee_id": emp.get("id"),
                "name": emp.get("name"),
                "efficiency_score": efficiency.get("score", 1.0),
                "pattern_boost": round(pattern_boost, 2),
                "confidence": round(confidence, 2),
                "final_score": round(final_score, 2),
                "strengths": strengths,
            }
        )

    # Sort by final score
    scored_candidates.sort(key=lambda x: x["final_score"], reverse=True)

    best = scored_candidates[0]
    alternatives = scored_candidates[1:3] if len(scored_candidates) > 1 else []

    return {
        "success": True,
        "suggested_employee": best["employee_id"],
        "employee_name": best["name"],
        "score": best["final_score"],
        "reasoning": {
            "efficiency_score": best["efficiency_score"],
            "pattern_boost": best["pattern_boost"],
            "confidence": best["confidence"],
            "strengths": best["strengths"],
        },
        "alternatives": alternatives,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_memory_hit_rate() -> dict:
    """
    Track memory sharing effectiveness.

    Returns:
        Dict with memory hit rate metrics
    """
    data = load_efficiency_data()
    executions = data.get("task_executions", [])

    if not executions:
        return {
            "success": True,
            "overall_hit_rate": 0.0,
            "total_tasks": 0,
            "context_reused": 0,
            "estimated_savings": "N/A",
            "insufficient_data": True,
        }

    total = len(executions)
    reused = sum(1 for e in executions if e.get("context_reused", False))
    hit_rate = reused / total if total > 0 else 0.0

    # Estimate savings (rough approximation)
    avg_context_tokens = 2000  # Assume 2000 tokens per context
    estimated_savings = reused * avg_context_tokens

    return {
        "success": True,
        "overall_hit_rate": round(hit_rate, 2),
        "total_tasks": total,
        "context_reused": reused,
        "estimated_token_savings": estimated_savings,
        "estimated_savings": f"{reused} tasks worth of context",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def optimize_agent_routing() -> dict:
    """
    Learn and improve routing decisions.

    Analyzes execution history to discover optimal patterns
    and saves them for future routing decisions.

    Returns:
        Dict with optimization results
    """
    data = load_efficiency_data()
    executions = data.get("task_executions", [])

    if len(executions) < 5:
        return {
            "success": True,
            "optimizations_applied": 0,
            "message": "Need at least 5 task executions for optimization",
            "current_executions": len(executions),
        }

    # Discover patterns
    patterns = _discover_task_patterns(executions)

    # Save patterns
    data["task_patterns"] = [
        {
            "pattern": p["pattern"],
            "optimal_employee": p["optimal_employee"],
            "avg_efficiency": p["avg_efficiency"],
            "sample_size": p["sample_size"],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        for p in patterns
    ]

    # Record optimization
    optimization_count = len(patterns)
    if optimization_count > 0:
        optimization = {
            "id": f"OPT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "type": "routing",
            "description": f"Discovered {optimization_count} routing pattern(s)",
            "measured_improvement": f"{optimization_count} patterns learned",
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "active": True,
        }
        data.setdefault("optimizations", []).append(optimization)

    save_efficiency_data(data)

    # Update learning metrics
    _update_learning_metrics(optimization_count, optimization_count)

    return {
        "success": True,
        "patterns_discovered": optimization_count,
        "patterns": patterns,
        "message": f"Discovered {optimization_count} routing patterns",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# Trend Tracking Functions
# -----------------------------------------------------------------------------


def record_efficiency_snapshot() -> dict:
    """
    Record a point-in-time efficiency snapshot for trend tracking.

    Called periodically to build historical trend data.

    Returns:
        Dict with snapshot details
    """
    data = load_efficiency_data()
    org = load_org()
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Calculate current company efficiency
    company_score = _calculate_company_efficiency()

    # Get employee scores
    employee_scores: dict[str, float] = {}
    for emp in org.get("employees", []):
        emp_id = emp.get("id", "")
        efficiency = emp.get("efficiency", {})
        if efficiency.get("tasks_completed", 0) > 0:
            employee_scores[emp_id] = efficiency.get("score", 0.0)

    # Initialize snapshots if not present
    if "efficiency_snapshots" not in data:
        data["efficiency_snapshots"] = []

    # Check if we already have a snapshot for today
    existing_today = [
        s for s in data["efficiency_snapshots"] if s.get("date") == date_str
    ]

    snapshot = {
        "date": date_str,
        "timestamp": now.isoformat(),
        "company_score": company_score,
        "employee_scores": employee_scores,
        "task_count": len(data.get("task_executions", [])),
    }

    if existing_today:
        # Update today's snapshot
        idx = data["efficiency_snapshots"].index(existing_today[0])
        data["efficiency_snapshots"][idx] = snapshot
    else:
        data["efficiency_snapshots"].append(snapshot)

    # Keep only last 90 days of snapshots
    cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    data["efficiency_snapshots"] = [
        s for s in data["efficiency_snapshots"] if s.get("date", "") >= cutoff
    ]

    save_efficiency_data(data)

    return {
        "success": True,
        "snapshot": snapshot,
        "total_snapshots": len(data["efficiency_snapshots"]),
    }


def get_efficiency_trends(days: int = 7) -> dict:
    """
    Get efficiency trends over a specified period.

    Args:
        days: Number of days to analyze (default 7)

    Returns:
        Dict with trend data including:
        - company_trend: Overall company efficiency trend
        - employee_trends: Per-employee trend data
        - patterns_discovered: New patterns over time
        - daily_efficiency: Day-by-day efficiency scores
    """
    data = load_efficiency_data()
    org = load_org()
    now = datetime.now(timezone.utc)

    # Get snapshots within the period
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    snapshots = [
        s for s in data.get("efficiency_snapshots", []) if s.get("date", "") >= cutoff
    ]

    # Sort by date
    snapshots.sort(key=lambda x: x.get("date", ""))

    # Calculate company trend
    if len(snapshots) >= 2:
        first_score = snapshots[0].get("company_score", 0.0)
        last_score = snapshots[-1].get("company_score", 0.0)
        if first_score > 0:
            change_percent = ((last_score - first_score) / first_score) * 100
        else:
            change_percent = 0.0

        if change_percent > 5:
            company_trend = "improving"
        elif change_percent < -5:
            company_trend = "declining"
        else:
            company_trend = "stable"
    else:
        first_score = 0.0
        last_score = _calculate_company_efficiency()
        change_percent = 0.0
        company_trend = "insufficient_data"

    # Calculate employee trends
    employee_trends: list[dict] = []
    employees = org.get("employees", [])

    for emp in employees:
        emp_id = emp.get("id", "")
        emp_name = emp.get("name", emp_id)

        # Get first and last scores from snapshots
        first_emp_score = None
        last_emp_score = None

        for snap in snapshots:
            scores = snap.get("employee_scores", {})
            if emp_id in scores:
                if first_emp_score is None:
                    first_emp_score = scores[emp_id]
                last_emp_score = scores[emp_id]

        # Fall back to current efficiency if no snapshots
        if last_emp_score is None:
            efficiency = emp.get("efficiency", {})
            last_emp_score = efficiency.get("score", 0.0)

        if first_emp_score is not None and last_emp_score is not None:
            if first_emp_score > 0:
                emp_change = (
                    (last_emp_score - first_emp_score) / first_emp_score
                ) * 100
            else:
                emp_change = 0.0

            if emp_change > 10:
                emp_trend = "improving"
            elif emp_change < -10:
                emp_trend = "declining"
            else:
                emp_trend = "stable"

            employee_trends.append(
                {
                    "employee_id": emp_id,
                    "name": emp_name,
                    "first_score": round(first_emp_score, 2),
                    "current_score": round(last_emp_score, 2),
                    "change_percent": round(emp_change, 1),
                    "trend": emp_trend,
                }
            )

    # Sort by change (declining first as they need attention)
    employee_trends.sort(key=lambda x: x.get("change_percent", 0))

    # Build daily efficiency data
    daily_efficiency = [
        {
            "date": s.get("date"),
            "company_score": s.get("company_score", 0.0),
            "task_count": s.get("task_count", 0),
        }
        for s in snapshots
    ]

    # Get learning/pattern metrics
    learning = org.get("economics", {}).get("learning", {})

    return {
        "success": True,
        "period_days": days,
        "company_trend": {
            "first_score": round(first_score, 2),
            "current_score": round(last_score, 2),
            "change_percent": round(change_percent, 1),
            "trend": company_trend,
        },
        "employee_trends": {
            "improving": [e for e in employee_trends if e["trend"] == "improving"],
            "declining": [e for e in employee_trends if e["trend"] == "declining"],
            "stable": [e for e in employee_trends if e["trend"] == "stable"],
        },
        "daily_efficiency": daily_efficiency,
        "learning": {
            "patterns_discovered": learning.get("patterns_discovered", 0),
            "optimizations_applied": learning.get("optimizations_applied", 0),
            "last_analysis": learning.get("last_analysis"),
        },
        "snapshot_count": len(snapshots),
        "generated_at": now.isoformat(),
    }


# -----------------------------------------------------------------------------
# Capacity Metrics
# -----------------------------------------------------------------------------


def get_capacity_metrics() -> dict:
    """
    Get subscription capacity metrics with efficiency impact.

    Tracks capacity usage and calculates how efficiency affects
    effective capacity (higher efficiency = more output per unit).

    Returns:
        Dict with capacity metrics including:
        - subscription: Tier and monthly capacity
        - current_usage: Units used this period
        - efficiency_multiplier: How efficiency affects capacity
        - projections: Projected usage and recommendations
    """
    org = load_org()
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    day_of_month = now.day
    days_in_month = 30  # Approximate

    # Get resource awareness config
    economics = org.get("economics", {})
    resource_config = economics.get(
        "resource_awareness",
        {
            "subscription_tier": "pro",
            "monthly_capacity_estimate": 1000,  # Work units
        },
    )

    subscription_tier = resource_config.get("subscription_tier", "pro")
    base_capacity = resource_config.get("monthly_capacity_estimate", 1000)

    # Count task executions this month
    executions = data.get("task_executions", [])
    month_executions = [
        e for e in executions if e.get("timestamp", "").startswith(current_month)
    ]

    # Weight by complexity for "work units"
    current_usage = 0.0
    for e in month_executions:
        complexity = e.get("complexity", "standard")
        weight = COMPLEXITY_WEIGHTS.get(complexity, 1.0)
        current_usage += weight

    current_usage = round(current_usage, 1)

    # Calculate efficiency multiplier
    company_efficiency = economics.get("efficiency", {}).get("company_score", 1.0)
    # Efficiency multiplier: higher efficiency = more effective capacity
    # Score of 1.0 = 1.0x, Score of 1.5 = 1.25x, Score of 0.5 = 0.75x
    if company_efficiency > 0:
        efficiency_multiplier = 0.5 + (company_efficiency * 0.5)
        efficiency_multiplier = max(0.5, min(2.0, efficiency_multiplier))  # Clamp
    else:
        efficiency_multiplier = 1.0

    effective_capacity = round(base_capacity * efficiency_multiplier, 0)

    # Calculate projections
    if day_of_month > 0:
        daily_rate = current_usage / day_of_month
        projected_total = daily_rate * days_in_month
    else:
        daily_rate = 0.0
        projected_total = 0.0

    utilization_percent = (
        (current_usage / effective_capacity * 100) if effective_capacity > 0 else 0
    )

    # Determine status
    projected_utilization = (
        (projected_total / effective_capacity * 100) if effective_capacity > 0 else 0
    )

    if projected_utilization >= 100:
        status = "over_capacity"
        recommendation = "Prioritize high-impact tasks, defer low-priority work"
    elif projected_utilization >= 80:
        status = "at_risk"
        recommendation = "Monitor closely, consider deferring non-critical tasks"
    elif projected_utilization >= 50:
        status = "on_track"
        recommendation = "Capacity healthy, room for additional work"
    else:
        status = "under_utilized"
        recommendation = "Consider queuing deferred tasks"

    # Department breakdown
    dept_allocation = get_resource_allocation_by_department()

    return {
        "success": True,
        "subscription": {
            "tier": subscription_tier,
            "base_capacity": base_capacity,
            "effective_capacity": int(effective_capacity),
        },
        "efficiency_impact": {
            "company_score": round(company_efficiency, 2),
            "multiplier": round(efficiency_multiplier, 2),
            "capacity_gain": int(effective_capacity - base_capacity),
        },
        "current_period": {
            "month": current_month,
            "day": day_of_month,
            "days_remaining": days_in_month - day_of_month,
        },
        "usage": {
            "current_units": current_usage,
            "utilization_percent": round(utilization_percent, 1),
            "daily_rate": round(daily_rate, 2),
        },
        "projection": {
            "projected_total": round(projected_total, 0),
            "projected_utilization_percent": round(projected_utilization, 1),
            "status": status,
            "recommendation": recommendation,
        },
        "by_department": dept_allocation.get("departments", {}),
        "generated_at": now.isoformat(),
    }


# -----------------------------------------------------------------------------
# Token Efficiency Target Tracking (Q2 G8)
# -----------------------------------------------------------------------------

# Default Q2 targets (from Q2-GOALS.md Appendix C)
DEFAULT_BASELINE_TOKENS_PER_DAY = 72000  # ~72K/day Q1 baseline
DEFAULT_TARGET_TOKENS_PER_DAY = 50000  # ~50K/day Q2 target (-30%)


def get_token_efficiency_target() -> dict:
    """
    Get token efficiency progress towards Q2 targets.

    Q2 Goal G8 requires reducing token usage from ~72K/day to ~50K/day (-30%).
    This function tracks progress towards that target.

    Returns:
        Dict with:
        - baseline: Q1 baseline tokens/day
        - target: Q2 target tokens/day
        - current_rate: Current daily token rate
        - progress_percent: Progress towards target (0-100%)
        - status: on_track / at_risk / off_track / exceeding_target
        - weekly_trend: Daily rates for last 7 days
        - recommendations: Actions if off-track
    """
    org = load_org()
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)

    # Get target config from org.json (with defaults)
    economics = org.get("economics", {})
    efficiency_targets = economics.get(
        "efficiency_targets",
        {
            "baseline_tokens_per_day": DEFAULT_BASELINE_TOKENS_PER_DAY,
            "target_tokens_per_day": DEFAULT_TARGET_TOKENS_PER_DAY,
            "target_reduction_percent": 30,
        },
    )

    baseline = efficiency_targets.get(
        "baseline_tokens_per_day", DEFAULT_BASELINE_TOKENS_PER_DAY
    )
    target = efficiency_targets.get(
        "target_tokens_per_day", DEFAULT_TARGET_TOKENS_PER_DAY
    )
    target_reduction = efficiency_targets.get("target_reduction_percent", 30)

    # Calculate current daily rate from token usage data
    token_usage = data.get("token_usage", {})
    daily_usage = token_usage.get("daily", {})

    # Get last 7 days of data
    weekly_trend = []
    total_recent_tokens = 0
    days_with_data = 0

    for i in range(7):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = daily_usage.get(date, {})
        day_tokens = day_data.get("total_tokens", 0)

        weekly_trend.append(
            {
                "date": date,
                "tokens": day_tokens,
                "below_target": day_tokens <= target if day_tokens > 0 else None,
            }
        )

        if day_tokens > 0:
            total_recent_tokens += day_tokens
            days_with_data += 1

    # Reverse to show oldest first
    weekly_trend.reverse()

    # Calculate current daily rate (average of days with data)
    if days_with_data > 0:
        current_rate = total_recent_tokens / days_with_data
    else:
        current_rate = 0

    # Calculate progress towards target
    # Progress = how much of the reduction we've achieved
    # If baseline is 72K and target is 50K, reduction needed is 22K
    # If current is 60K, we've reduced by 12K, which is 12/22 = 54.5%
    reduction_needed = baseline - target
    reduction_achieved = baseline - current_rate

    if reduction_needed > 0:
        progress_percent = min(
            100, max(0, (reduction_achieved / reduction_needed) * 100)
        )
    else:
        progress_percent = 100.0 if current_rate <= target else 0.0

    # Determine status
    if current_rate == 0:
        status = "no_data"
    elif current_rate <= target:
        status = "exceeding_target"
    elif current_rate <= target * 1.1:  # Within 10% of target
        status = "on_track"
    elif current_rate <= baseline * 0.9:  # At least 10% reduction
        status = "at_risk"
    else:
        status = "off_track"

    # Generate recommendations based on status
    recommendations = []
    if status == "off_track":
        recommendations.append(
            {
                "priority": "high",
                "action": "Review executive session frequency",
                "reason": f"Current rate ({int(current_rate):,}) exceeds baseline ({baseline:,})",
            }
        )
        recommendations.append(
            {
                "priority": "high",
                "action": "Enable adaptive intervals (P18)",
                "reason": "Dynamic intervals reduce token usage during low-activity periods",
            }
        )
    elif status == "at_risk":
        recommendations.append(
            {
                "priority": "medium",
                "action": "Monitor daily token usage",
                "reason": f"Progress at {progress_percent:.1f}% — need to reach 100%",
            }
        )
        recommendations.append(
            {
                "priority": "medium",
                "action": "Route tasks to most efficient employees",
                "reason": "Efficiency-based routing reduces retry overhead",
            }
        )
    elif status == "exceeding_target":
        recommendations.append(
            {
                "priority": "low",
                "action": "Maintain current efficiency",
                "reason": "Already exceeding Q2 target — continue current practices",
            }
        )

    # Calculate days to target (extrapolation)
    if current_rate > target and days_with_data > 1:
        # Calculate trend direction from first and last day with data
        first_day_tokens = None
        last_day_tokens = None
        for day in weekly_trend:
            if day["tokens"] > 0:
                if first_day_tokens is None:
                    first_day_tokens = day["tokens"]
                last_day_tokens = day["tokens"]

        if first_day_tokens and last_day_tokens and first_day_tokens > last_day_tokens:
            daily_reduction = (first_day_tokens - last_day_tokens) / days_with_data
            if daily_reduction > 0:
                days_to_target = int((current_rate - target) / daily_reduction)
            else:
                days_to_target = None
        else:
            days_to_target = None
    else:
        days_to_target = 0 if current_rate <= target else None

    return {
        "success": True,
        "baseline": {
            "tokens_per_day": baseline,
            "source": "Q1 average",
        },
        "target": {
            "tokens_per_day": target,
            "reduction_percent": target_reduction,
        },
        "current": {
            "tokens_per_day": round(current_rate, 0),
            "days_measured": days_with_data,
        },
        "progress": {
            "percent": round(progress_percent, 1),
            "status": status,
            "reduction_achieved": round(baseline - current_rate, 0),
            "reduction_needed": round(reduction_needed, 0),
            "days_to_target": days_to_target,
        },
        "weekly_trend": weekly_trend,
        "recommendations": recommendations,
        "generated_at": now.isoformat(),
    }


# -----------------------------------------------------------------------------
# Pattern Aggregation Functions (P30 - Improvement Detection)
# -----------------------------------------------------------------------------

WORK_QUEUE_FILE = "state/work_queue.json"


def get_work_queue_path() -> Path:
    """Get work_queue.json path."""
    return get_company_dir() / WORK_QUEUE_FILE


def load_work_queue() -> dict:
    """Load work queue from work_queue.json."""
    path = get_work_queue_path()
    if not path.exists():
        return {"pending": [], "in_progress": [], "blocked": [], "completed": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"pending": [], "in_progress": [], "blocked": [], "completed": []}


def get_execution_patterns(days: int = 30) -> dict:
    """
    Aggregate task executions for pattern analysis.

    READ-ONLY aggregation function for use by improvement_detector.

    Args:
        days: Number of days to analyze (default 30)

    Returns:
        Dict with aggregations:
        - pattern_tags_distribution: Count of each pattern tag
        - complexity_distribution: Count of tasks by complexity
        - success_rate_by_complexity: Success rate for each complexity level
        - avg_duration_by_complexity: Average duration for each complexity
        - escalation_rate_by_employee: Escalation rate per employee
    """
    data = load_efficiency_data()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()

    # Filter executions within time window
    executions = [
        e for e in data.get("task_executions", []) if e.get("timestamp", "") >= cutoff
    ]

    if not executions:
        return {
            "success": True,
            "pattern_tags_distribution": {},
            "complexity_distribution": {},
            "success_rate_by_complexity": {},
            "avg_duration_by_complexity": {},
            "escalation_rate_by_employee": {},
            "total_executions": 0,
            "period_days": days,
            "insufficient_data": True,
            "generated_at": now.isoformat(),
        }

    # Pattern tags distribution
    pattern_tags_distribution: dict[str, int] = {}
    for e in executions:
        for tag in e.get("pattern_tags", []):
            pattern_tags_distribution[tag] = pattern_tags_distribution.get(tag, 0) + 1

    # Complexity distribution
    complexity_distribution: dict[str, int] = {}
    complexity_success: dict[str, list[bool]] = {}
    complexity_durations: dict[str, list[float]] = {}

    for e in executions:
        complexity = e.get("complexity", "standard")
        complexity_distribution[complexity] = (
            complexity_distribution.get(complexity, 0) + 1
        )

        # Track success/failure per complexity
        if complexity not in complexity_success:
            complexity_success[complexity] = []
        complexity_success[complexity].append(e.get("success", False))

        # Track duration per complexity
        if complexity not in complexity_durations:
            complexity_durations[complexity] = []
        duration = e.get("duration_minutes", 0.0)
        if duration > 0:
            complexity_durations[complexity].append(duration)

    # Calculate success rate by complexity
    success_rate_by_complexity: dict[str, float] = {}
    for complexity, successes in complexity_success.items():
        if successes:
            rate = sum(1 for s in successes if s) / len(successes)
            success_rate_by_complexity[complexity] = round(rate, 3)

    # Calculate average duration by complexity
    avg_duration_by_complexity: dict[str, float] = {}
    for complexity, durations in complexity_durations.items():
        if durations:
            avg_duration_by_complexity[complexity] = round(
                sum(durations) / len(durations), 2
            )

    # Escalation rate by employee
    employee_escalations: dict[str, dict[str, int]] = {}
    for e in executions:
        emp_id = e.get("employee_id", "unknown")
        if emp_id not in employee_escalations:
            employee_escalations[emp_id] = {"total": 0, "escalated": 0}
        employee_escalations[emp_id]["total"] += 1
        if e.get("escalated", False):
            employee_escalations[emp_id]["escalated"] += 1

    escalation_rate_by_employee: dict[str, float] = {}
    for emp_id, stats in employee_escalations.items():
        if stats["total"] > 0:
            rate = stats["escalated"] / stats["total"]
            escalation_rate_by_employee[emp_id] = round(rate, 3)

    return {
        "success": True,
        "pattern_tags_distribution": pattern_tags_distribution,
        "complexity_distribution": complexity_distribution,
        "success_rate_by_complexity": success_rate_by_complexity,
        "avg_duration_by_complexity": avg_duration_by_complexity,
        "escalation_rate_by_employee": escalation_rate_by_employee,
        "total_executions": len(executions),
        "period_days": days,
        "generated_at": now.isoformat(),
    }


def get_agent_performance_summary() -> list[dict]:
    """
    Get performance summary for each employee.

    READ-ONLY aggregation function for use by improvement_detector.
    Returns list sorted by success_rate ascending (worst performers first).

    Returns:
        List of dicts, each containing:
        - employee_id: Employee identifier
        - success_rate: Tasks succeeded / total tasks
        - avg_duration_minutes: Average task duration
        - first_pass_rate: Rate of first-pass successes
        - escalation_rate: Rate of escalated tasks
        - tasks_by_complexity: Breakdown by complexity level
        - total_tasks: Total tasks executed
    """
    data = load_efficiency_data()
    org = load_org()
    executions = data.get("task_executions", [])

    if not executions:
        return []

    # Aggregate per employee
    employee_stats: dict[str, dict[str, Any]] = {}

    for e in executions:
        emp_id = e.get("employee_id", "unknown")
        if emp_id not in employee_stats:
            employee_stats[emp_id] = {
                "total": 0,
                "successes": 0,
                "first_passes": 0,
                "escalations": 0,
                "durations": [],
                "complexity_counts": {},
            }

        stats = employee_stats[emp_id]
        stats["total"] += 1
        if e.get("success", False):
            stats["successes"] += 1
        if e.get("first_pass", True):
            stats["first_passes"] += 1
        if e.get("escalated", False):
            stats["escalations"] += 1

        duration = e.get("duration_minutes", 0.0)
        if duration > 0:
            stats["durations"].append(duration)

        complexity = e.get("complexity", "standard")
        stats["complexity_counts"][complexity] = (
            stats["complexity_counts"].get(complexity, 0) + 1
        )

    # Build result list
    result = []
    employees = {emp.get("id"): emp for emp in org.get("employees", [])}

    for emp_id, stats in employee_stats.items():
        total = stats["total"]
        if total == 0:
            continue

        emp_info = employees.get(emp_id, {})

        summary = {
            "employee_id": emp_id,
            "employee_name": emp_info.get("name", emp_id),
            "success_rate": round(stats["successes"] / total, 3),
            "avg_duration_minutes": (
                round(sum(stats["durations"]) / len(stats["durations"]), 2)
                if stats["durations"]
                else 0.0
            ),
            "first_pass_rate": round(stats["first_passes"] / total, 3),
            "escalation_rate": round(stats["escalations"] / total, 3),
            "tasks_by_complexity": stats["complexity_counts"],
            "total_tasks": total,
        }
        result.append(summary)

    # Sort by success_rate ascending (worst performers first for improvement focus)
    result.sort(key=lambda x: x["success_rate"])

    return result


def get_bottleneck_analysis() -> list[dict]:
    """
    Identify task types with high retry count or escalation.

    READ-ONLY aggregation function for use by improvement_detector.
    Groups by pattern_tags and identifies bottleneck patterns.

    Returns:
        List of bottleneck patterns with:
        - pattern: The pattern tag
        - failure_count: Number of failed tasks with this pattern
        - avg_retries: Average retry count for tasks with this pattern
        - escalation_count: Number of escalated tasks
        - total_tasks: Total tasks with this pattern
        - suggested_cause: Heuristic-based cause suggestion
    """
    data = load_efficiency_data()
    executions = data.get("task_executions", [])

    if not executions:
        return []

    # Aggregate by pattern tag
    pattern_stats: dict[str, dict[str, Any]] = {}

    for e in executions:
        for tag in e.get("pattern_tags", []):
            if tag not in pattern_stats:
                pattern_stats[tag] = {
                    "total": 0,
                    "failures": 0,
                    "escalations": 0,
                    "retry_counts": [],
                }

            stats = pattern_stats[tag]
            stats["total"] += 1
            if not e.get("success", True):
                stats["failures"] += 1
            if e.get("escalated", False):
                stats["escalations"] += 1
            stats["retry_counts"].append(e.get("retry_count", 0))

    # Build result list for patterns with issues
    result = []

    for pattern, stats in pattern_stats.items():
        total = stats["total"]
        if total == 0:
            continue

        failures = stats["failures"]
        escalations = stats["escalations"]
        retry_counts = stats["retry_counts"]
        avg_retries = sum(retry_counts) / len(retry_counts) if retry_counts else 0.0

        # Only include if there are actual problems
        if failures == 0 and escalations == 0 and avg_retries < 0.5:
            continue

        # Heuristic cause suggestion
        suggested_cause = _suggest_bottleneck_cause(
            failure_rate=failures / total if total > 0 else 0,
            escalation_rate=escalations / total if total > 0 else 0,
            avg_retries=avg_retries,
        )

        result.append(
            {
                "pattern": pattern,
                "failure_count": failures,
                "avg_retries": round(avg_retries, 2),
                "escalation_count": escalations,
                "total_tasks": total,
                "failure_rate": round(failures / total, 3) if total > 0 else 0.0,
                "suggested_cause": suggested_cause,
            }
        )

    # Sort by failure count descending
    result.sort(key=lambda x: (x["failure_count"], x["avg_retries"]), reverse=True)

    return result


def _suggest_bottleneck_cause(
    failure_rate: float,
    escalation_rate: float,
    avg_retries: float,
) -> str:
    """Heuristic to suggest cause of bottleneck pattern."""
    causes = []

    if failure_rate > 0.5:
        causes.append(
            "high failure rate suggests capability gap or unclear requirements"
        )
    elif failure_rate > 0.2:
        causes.append("moderate failure rate may indicate complexity underestimation")

    if escalation_rate > 0.3:
        causes.append("frequent escalations suggest need for additional training")
    elif escalation_rate > 0.1:
        causes.append("some escalations may indicate edge cases not covered")

    if avg_retries > 2.0:
        causes.append("high retry count suggests task decomposition needed")
    elif avg_retries > 1.0:
        causes.append("retries may indicate feedback loop issues")

    if not causes:
        return "pattern shows minor issues, monitor for trends"

    return "; ".join(causes)


def get_capability_utilization() -> dict:
    """
    Analyze capability utilization by comparing task requirements to employee capabilities.

    READ-ONLY aggregation function for use by improvement_detector.
    Reads required_capabilities from work_queue completed tasks and compares
    against employee capabilities from org.json.

    Returns:
        Dict with:
        - by_capability: Dict mapping capability to utilization metrics
        - over_utilized: Capabilities with high demand relative to supply
        - under_utilized: Capabilities with low demand relative to supply
        - unmet_capabilities: Required capabilities not found in any employee
        - summary: Overall utilization statistics
    """
    org = load_org()
    work_queue = load_work_queue()
    now = datetime.now(timezone.utc)

    # Get all employee capabilities
    employees = org.get("employees", [])
    capability_supply: dict[str, int] = {}  # How many employees have each capability

    for emp in employees:
        for cap in emp.get("capabilities", []):
            capability_supply[cap] = capability_supply.get(cap, 0) + 1

    # Get capability demand from completed tasks
    completed_tasks = work_queue.get("completed", [])
    capability_demand: dict[str, int] = {}  # How many tasks required each capability

    for task in completed_tasks:
        for cap in task.get("required_capabilities", []):
            capability_demand[cap] = capability_demand.get(cap, 0) + 1

    # Also check pending and in_progress for upcoming demand
    upcoming_demand: dict[str, int] = {}
    for status_key in ["pending", "in_progress", "blocked"]:
        for task in work_queue.get(status_key, []):
            for cap in task.get("required_capabilities", []):
                upcoming_demand[cap] = upcoming_demand.get(cap, 0) + 1

    # Calculate utilization metrics per capability
    all_capabilities = set(capability_supply.keys()) | set(capability_demand.keys())
    by_capability: dict[str, dict[str, Any]] = {}
    over_utilized: list[str] = []
    under_utilized: list[str] = []
    unmet_capabilities: list[str] = []

    total_tasks = len(completed_tasks)

    for cap in all_capabilities:
        supply = capability_supply.get(cap, 0)
        demand = capability_demand.get(cap, 0)
        upcoming = upcoming_demand.get(cap, 0)

        # Utilization rate: demand / supply (higher means more utilized)
        if supply > 0:
            utilization_rate = demand / (
                supply * max(1, total_tasks // len(employees) if employees else 1)
            )
            utilization_rate = min(utilization_rate, 2.0)  # Cap at 200%
        else:
            utilization_rate = float("inf") if demand > 0 else 0.0

        by_capability[cap] = {
            "supply": supply,
            "demand": demand,
            "upcoming_demand": upcoming,
            "utilization_rate": round(utilization_rate, 3)
            if utilization_rate != float("inf")
            else "inf",
        }

        # Categorize
        if supply == 0 and demand > 0:
            unmet_capabilities.append(cap)
        elif supply > 0 and demand > 0:
            if utilization_rate > 1.5:
                over_utilized.append(cap)
            elif utilization_rate < 0.3 and demand > 0:
                under_utilized.append(cap)

    return {
        "success": True,
        "by_capability": by_capability,
        "over_utilized": over_utilized,
        "under_utilized": under_utilized,
        "unmet_capabilities": unmet_capabilities,
        "summary": {
            "total_capabilities_tracked": len(all_capabilities),
            "capabilities_with_supply": len(capability_supply),
            "capabilities_with_demand": len(capability_demand),
            "total_completed_tasks": total_tasks,
        },
        "generated_at": now.isoformat(),
    }


# -----------------------------------------------------------------------------
# Report Functions
# -----------------------------------------------------------------------------


def get_efficiency_report() -> dict:
    """
    Get comprehensive efficiency report for the company.

    Returns:
        Dict with:
        - company_efficiency: Overall company score
        - employee_breakdown: Per-employee efficiency
        - patterns: Discovered patterns
        - memory_stats: Memory hit rate stats
        - recommendations: Improvement recommendations
    """
    org = load_org()
    data = load_efficiency_data()

    employees = org.get("employees", [])
    economics = org.get("economics", {})

    # Company efficiency
    company_score = economics.get("efficiency", {}).get("company_score", 0.0)
    target_score = economics.get("efficiency", {}).get("target_score", 0.90)

    # Employee breakdown
    employee_breakdown = []
    for emp in employees:
        efficiency = emp.get("efficiency", {})
        if efficiency:
            employee_breakdown.append(
                {
                    "employee_id": emp.get("id"),
                    "name": emp.get("name"),
                    "score": efficiency.get("score", 0.0),
                    "tasks_completed": efficiency.get("tasks_completed", 0),
                    "trend": efficiency.get("trend", "stable"),
                    "strengths": efficiency.get("strengths", [])[:3],
                }
            )

    # Sort by score
    employee_breakdown.sort(key=lambda x: x["score"], reverse=True)

    # Get patterns
    patterns = data.get("task_patterns", [])

    # Get memory stats
    memory_stats = get_memory_hit_rate()

    # Get insights
    insights = get_efficiency_insights()

    # Learning stats
    learning = economics.get("learning", {})

    return {
        "success": True,
        "company_efficiency": {
            "score": company_score,
            "target": target_score,
            "status": "on_target" if company_score >= target_score else "below_target",
            "gap": round(target_score - company_score, 2),
        },
        "employee_breakdown": employee_breakdown,
        "patterns": patterns,
        "memory_stats": {
            "hit_rate": memory_stats.get("overall_hit_rate", 0.0),
            "estimated_savings": memory_stats.get("estimated_savings", "N/A"),
        },
        "learning": {
            "patterns_discovered": learning.get("patterns_discovered", 0),
            "optimizations_applied": learning.get("optimizations_applied", 0),
            "last_analysis": learning.get("last_analysis"),
        },
        "recommendations": insights.get("recommendations", [])[:5],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# P18.6: Adaptive Interval Recommendation
# -----------------------------------------------------------------------------


def _get_current_interval_hours() -> float:
    """Get current executive loop interval from gatesmith.json.

    Returns:
        Current interval in hours, or 4.0 as default
    """
    _ensure_imports()

    default_interval = 4.0
    try:
        project_root = company_resolver.find_company_root()
        if not project_root:
            project_root = Path.cwd()

        config_path = project_root / "gatesmith.json"
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
                return float(
                    config.get("executiveLoop", {}).get(
                        "intervalHours", default_interval
                    )
                )
    except Exception:
        pass

    return default_interval


def get_adaptive_interval_recommendation() -> str:
    """Get adaptive interval recommendation with human-readable formatting.

    Returns:
        Human-readable formatted recommendation string
    """
    # Lazy import interval_learner
    try:
        try:
            from . import interval_learner
        except ImportError:
            import interval_learner  # type: ignore[no-redef]
    except Exception as e:
        return f"Error: Failed to import interval_learner: {e}"

    try:
        # Get current interval from config
        current_interval = _get_current_interval_hours()

        # Get recommendation and patterns
        recommendation = interval_learner.get_interval_recommendation(
            current_interval=current_interval,
            limit=100,
        )
        patterns = interval_learner.get_patterns_from_history(limit=100)

        # Format output
        lines = [
            "=== Adaptive Interval Recommendation ===",
            f"Current interval: {recommendation.current_interval_hours}h",
            f"Suggested interval: {recommendation.suggested_interval_hours}h",
            f"Confidence: {int(recommendation.confidence * 100)}%",
            f"Reasoning: {recommendation.reasoning}",
            f"Expected savings: {recommendation.expected_savings_percent:.0f}%",
            f"Auto-apply: {'Yes' if recommendation.auto_apply else 'No'}"
            + (
                ""
                if recommendation.auto_apply
                else " (confidence below threshold)"
                if recommendation.confidence < 0.8
                else " (savings below threshold)"
            ),
        ]

        # Add patterns section
        if patterns:
            lines.append("")
            lines.append("Patterns Detected:")
            for pattern in patterns:
                lines.append(
                    f"  - {pattern.pattern_type}: {pattern.description} "
                    f"(confidence: {int(pattern.confidence * 100)}%)"
                )
        else:
            lines.append("")
            lines.append("Patterns Detected: None (insufficient data)")

        # Add application hint
        lines.append("")
        lines.append("To apply: Update gatesmith.json executiveLoop.intervalHours")

        return "\n".join(lines)

    except Exception as e:
        return f"Error generating adaptive recommendation: {e}"


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
G6 Economics — Efficiency Optimization & Value Maximization

Commands:
    record            Record a task execution
    employee          Get employee efficiency metrics
    report            Get company efficiency report
    insights          Get efficiency insights and patterns
    suggest           Suggest optimal employee for task
    memory            Get memory hit rate statistics
    optimize          Run optimization analysis
    tokens            Get token usage report (WS-009-002)
    budget            Get budget utilization report
    costs             Get cost tracking summary (G6 Economics)
    cost-employee     Get cost breakdown for employee
    record-task-cost  Record task cost with token counts
    record-session-cost Record session cost with token counts
    departments       Get resource allocation by department
    goals             Get cost-per-goal tracking
    record-goal       Record cost attribution for a goal
    trends            Get efficiency trends over time
    capacity          Get subscription capacity metrics
    snapshot          Record efficiency snapshot for trends
    efficiency-target Get token efficiency target progress (Q2 G8)
    adaptive          Get adaptive interval recommendation (P18)

Record options:
    --task-id ID         Task ID (required)
    --employee-id ID     Employee ID (required)
    --duration MINUTES   Duration in minutes (required)
    --complexity STR     trivial|standard|complex|epic (default: standard)
    --success            Mark as successful (default)
    --failed             Mark as failed
    --first-pass         First pass success (default)
    --retry              Not first pass
    --retry-count N      Number of retries (default: 0)
    --escalated          Task was escalated
    --context-reused     Shared context was reused
    --tags TAG,TAG       Pattern tags (comma-separated)

Employee options:
    --employee-id ID     Employee ID (required)

Suggest options:
    --capabilities LIST  Comma-separated required capabilities
    --complexity STR     Task complexity
    --tags TAG,TAG       Pattern tags (comma-separated)

Examples:
    # Record successful task
    python efficiency_tracker.py record --task-id task-123 --employee-id senior-python-dev --duration 45

    # Record failed task with retry
    python efficiency_tracker.py record --task-id task-456 --employee-id junior-dev --duration 90 --failed --retry-count 2

    # Get employee efficiency
    python efficiency_tracker.py employee --employee-id senior-python-dev

    # Get company report
    python efficiency_tracker.py report

    # Get insights
    python efficiency_tracker.py insights

    # Suggest employee for task
    python efficiency_tracker.py suggest --capabilities python,testing --tags api-development

    # Run optimization
    python efficiency_tracker.py optimize

    # Get budget utilization
    python efficiency_tracker.py budget

    # Get department resource allocation
    python efficiency_tracker.py departments

    # Get cost-per-goal tracking
    python efficiency_tracker.py goals

    # Record cost attribution to a goal
    python efficiency_tracker.py record-goal --goal-id G1 --task-id task-123 \\
        --employee-id senior-python-dev --tokens 5000 --duration 30

    # Get cost tracking summary
    python efficiency_tracker.py costs

    # Get cost breakdown for employee
    python efficiency_tracker.py cost-employee --employee-id senior-python-dev

    # Record task cost
    python efficiency_tracker.py record-task-cost --task-id task-123 \\
        --employee-id senior-python-dev --input-tokens 1000 --output-tokens 500

    # Record session cost
    python efficiency_tracker.py record-session-cost --session-id sess-001 \\
        --executive-id forge-ceo --input-tokens 5000 --output-tokens 2000

    # Get adaptive interval recommendation
    python efficiency_tracker.py adaptive

Output: JSON with efficiency data.
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
        if command == "record":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "duration" not in args:
                print("Error: --duration required")
                sys.exit(1)

            try:
                duration = float(args["duration"])
            except ValueError:
                print("Error: --duration must be a number")
                sys.exit(1)

            pattern_tags = None
            if "tags" in args:
                pattern_tags = [t.strip() for t in args["tags"].split(",")]

            result = record_task_execution(
                task_id=args["task_id"],
                employee_id=args["employee_id"],
                duration_minutes=duration,
                complexity=args.get("complexity", "standard"),
                success=not args.get("failed", False),
                first_pass=not args.get("retry", False),
                retry_count=int(args.get("retry_count", 0)),
                escalated=args.get("escalated", False) is True,
                context_reused=args.get("context_reused", False) is True,
                pattern_tags=pattern_tags,
            )
            print(json.dumps(result, indent=2))

        elif command == "employee":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            result = calculate_efficiency_score(args["employee_id"])
            print(json.dumps(result, indent=2))

        elif command == "report":
            result = get_efficiency_report()
            print(json.dumps(result, indent=2))

        elif command == "insights":
            result = get_efficiency_insights()
            print(json.dumps(result, indent=2))

        elif command == "suggest":
            capabilities = None
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            pattern_tags = None
            if "tags" in args:
                pattern_tags = [t.strip() for t in args["tags"].split(",")]

            result = suggest_optimal_employee(
                required_capabilities=capabilities,
                complexity=args.get("complexity", "standard"),
                pattern_tags=pattern_tags,
            )
            print(json.dumps(result, indent=2))

        elif command == "memory":
            result = get_memory_hit_rate()
            print(json.dumps(result, indent=2))

        elif command == "optimize":
            result = optimize_agent_routing()
            print(json.dumps(result, indent=2))

        elif command == "tokens":
            # WS-009-002: Token usage report
            result = get_token_report()
            print(json.dumps(result, indent=2))

        elif command == "budget":
            # Budget utilization report
            result = get_budget_utilization()
            print(json.dumps(result, indent=2))

        elif command == "costs":
            # G6 Economics: Cost tracking summary
            result = get_cost_summary()
            print(json.dumps(result, indent=2))

        elif command == "cost-employee":
            # G6 Economics: Cost breakdown for specific employee
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            result = get_cost_by_employee(args["employee_id"])
            print(json.dumps(result, indent=2))

        elif command == "record-task-cost":
            # G6 Economics: Record task cost
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "input_tokens" not in args:
                print("Error: --input-tokens required")
                sys.exit(1)
            if "output_tokens" not in args:
                print("Error: --output-tokens required")
                sys.exit(1)

            try:
                input_tokens = int(args["input_tokens"])
                output_tokens = int(args["output_tokens"])
            except ValueError:
                print("Error: token counts must be integers")
                sys.exit(1)

            result = record_task_cost(
                task_id=args["task_id"],
                employee_id=args["employee_id"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=args.get("model", DEFAULT_MODEL),
            )
            print(json.dumps(result, indent=2))

        elif command == "record-session-cost":
            # G6 Economics: Record session cost
            if "session_id" not in args:
                print("Error: --session-id required")
                sys.exit(1)
            if "executive_id" not in args:
                print("Error: --executive-id required")
                sys.exit(1)
            if "input_tokens" not in args:
                print("Error: --input-tokens required")
                sys.exit(1)
            if "output_tokens" not in args:
                print("Error: --output-tokens required")
                sys.exit(1)

            try:
                input_tokens = int(args["input_tokens"])
                output_tokens = int(args["output_tokens"])
            except ValueError:
                print("Error: token counts must be integers")
                sys.exit(1)

            result = record_session_cost(
                session_id=args["session_id"],
                executive_id=args["executive_id"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=args.get("model", DEFAULT_MODEL),
            )
            print(json.dumps(result, indent=2))

        elif command == "departments":
            # Resource allocation by department
            result = get_resource_allocation_by_department()
            print(json.dumps(result, indent=2))

        elif command == "goals":
            # Cost-per-goal tracking
            result = get_cost_per_goal()
            print(json.dumps(result, indent=2))

        elif command == "record-goal":
            # Record cost attribution to a goal
            if "goal_id" not in args:
                print("Error: --goal-id required")
                sys.exit(1)
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "tokens" not in args:
                print("Error: --tokens required")
                sys.exit(1)
            if "duration" not in args:
                print("Error: --duration required")
                sys.exit(1)

            try:
                tokens = int(args["tokens"])
                duration = float(args["duration"])
            except ValueError:
                print("Error: --tokens must be integer, --duration must be number")
                sys.exit(1)

            result = record_goal_cost(
                goal_id=args["goal_id"],
                task_id=args["task_id"],
                employee_id=args["employee_id"],
                estimated_tokens=tokens,
                duration_minutes=duration,
            )
            print(json.dumps(result, indent=2))

        elif command == "trends":
            # Get efficiency trends over time
            days = int(args.get("days", 7))
            result = get_efficiency_trends(days=days)
            print(json.dumps(result, indent=2))

        elif command == "capacity":
            # Get subscription capacity metrics
            result = get_capacity_metrics()
            print(json.dumps(result, indent=2))

        elif command == "snapshot":
            # Record efficiency snapshot for trend tracking
            result = record_efficiency_snapshot()
            print(json.dumps(result, indent=2))

        elif command == "efficiency-target":
            # Get token efficiency target progress (Q2 G8)
            result = get_token_efficiency_target()
            print(json.dumps(result, indent=2))

        elif command == "adaptive":
            # P18.6: Get adaptive interval recommendation
            result = get_adaptive_interval_recommendation()
            print(result)  # Already formatted as human-readable string

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
