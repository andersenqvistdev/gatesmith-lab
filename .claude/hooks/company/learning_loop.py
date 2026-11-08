#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P25 Learning Loop — Outcome-Based Improvement System.

Feedback loop that learns from execution outcomes to optimize future scheduling.
Records task outcomes, computes insights, and applies learnings to scheduling decisions.

Key Capabilities:
    - Track execution outcomes with employee/complexity/success/tokens
    - Compute insights (best employees per complexity, failure patterns)
    - Correlate plan scores with success rates
    - Apply insights to scheduling decisions
    - Generate improvement proposals

Storage:
    - Learning outcomes: .company/learning_outcomes.json
    - Rolling window: 30 days (configurable)

Usage:
    # Record an outcome
    python learning_loop.py record --task-id "task-123" --employee-id "senior-dev" \\
        --complexity standard --success true --first-pass true

    # Compute insights
    python learning_loop.py insights

    # Get best employee for task
    python learning_loop.py suggest --complexity complex

    # Check if task should be deferred
    python learning_loop.py defer --task-id "task-123" --complexity epic

    # Generate improvement proposals
    python learning_loop.py proposals

    # Show help
    python learning_loop.py help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

LEARNING_OUTCOMES_FILE = "learning_outcomes.json"
ROLLING_WINDOW_DAYS = 30
RETENTION_DAYS = 90  # Keep older data for trend analysis

# Minimum sample size for reliable insights
MIN_SAMPLE_SIZE = 5

# Success rate threshold for identifying failure patterns
FAILURE_PATTERN_THRESHOLD = 0.7

# Complexity weights for scoring
COMPLEXITY_WEIGHTS = {
    "trivial": 0.5,
    "standard": 1.0,
    "complex": 2.0,
    "epic": 4.0,
}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class OutcomeRecord:
    """Record of a single task execution outcome."""

    task_id: str
    employee_id: str
    complexity: str  # trivial/standard/complex/epic
    success: bool
    first_pass: bool
    revision_count: int
    plan_score: int  # From checker (0-25)
    execution_minutes: float
    tokens_used: int
    recorded_at: datetime
    task_type: str = ""  # Optional: type/category of task
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "employee_id": self.employee_id,
            "complexity": self.complexity,
            "success": self.success,
            "first_pass": self.first_pass,
            "revision_count": self.revision_count,
            "plan_score": self.plan_score,
            "execution_minutes": self.execution_minutes,
            "tokens_used": self.tokens_used,
            "recorded_at": self.recorded_at.isoformat(),
            "task_type": self.task_type,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutcomeRecord:
        """Create from dictionary."""
        recorded_at = data.get("recorded_at")
        if isinstance(recorded_at, str):
            recorded_at = datetime.fromisoformat(recorded_at)
        elif recorded_at is None:
            recorded_at = datetime.now(timezone.utc)

        return cls(
            task_id=data.get("task_id", ""),
            employee_id=data.get("employee_id", ""),
            complexity=data.get("complexity", "standard"),
            success=data.get("success", False),
            first_pass=data.get("first_pass", False),
            revision_count=data.get("revision_count", 0),
            plan_score=data.get("plan_score", 0),
            execution_minutes=data.get("execution_minutes", 0.0),
            tokens_used=data.get("tokens_used", 0),
            recorded_at=recorded_at,
            task_type=data.get("task_type", ""),
            tags=data.get("tags", []),
        )


@dataclass
class EmployeePerformance:
    """Aggregated performance metrics for an employee."""

    employee_id: str
    total_tasks: int
    success_rate: float
    first_pass_rate: float
    avg_execution_minutes: float
    avg_tokens_used: int
    efficiency_score: float  # success_rate * (1 / avg_time_normalized)
    by_complexity: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class FailurePattern:
    """Pattern of task failures."""

    pattern_id: str
    description: str
    task_type: str
    complexity: str
    failure_rate: float
    sample_size: int
    common_tags: list[str]
    recent_examples: list[str]  # Task IDs


@dataclass
class LearningInsights:
    """Computed insights from learning data."""

    best_employees_by_complexity: dict[str, list[str]]  # complexity -> [employee_ids]
    failure_patterns: list[dict[str, Any]]  # task types with high failure rates
    optimal_time_windows: list[tuple[int, int]]  # (hour_start, hour_end)
    plan_score_success_correlation: float
    employee_performance: dict[str, dict[str, Any]]  # employee_id -> metrics
    computed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "best_employees_by_complexity": self.best_employees_by_complexity,
            "failure_patterns": self.failure_patterns,
            "optimal_time_windows": self.optimal_time_windows,
            "plan_score_success_correlation": self.plan_score_success_correlation,
            "employee_performance": self.employee_performance,
            "computed_at": self.computed_at.isoformat(),
        }


# =============================================================================
# Path Utilities
# =============================================================================


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def _get_company_dir() -> Path:
    """Get the company directory path."""
    # Try to use company_resolver if available
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        return company_resolver.get_company_dir()
    except (ImportError, Exception):
        # Fallback to default
        return Path.cwd() / ".company"


def _get_outcomes_path() -> Path:
    """Get the learning outcomes file path."""
    return _get_company_dir() / LEARNING_OUTCOMES_FILE


def _ensure_company_dir():
    """Ensure company directory exists."""
    company_dir = _get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Storage Functions
# =============================================================================


def _get_empty_outcomes_data() -> dict[str, Any]:
    """Return empty outcomes data structure."""
    return {
        "outcomes": [],
        "cached_insights": None,
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "rolling_window_days": ROLLING_WINDOW_DAYS,
            "retention_days": RETENTION_DAYS,
            "version": "1.0",
        },
    }


def _load_outcomes_data() -> dict[str, Any]:
    """Load outcomes data from file."""
    path = _get_outcomes_path()

    if not path.exists():
        return _get_empty_outcomes_data()

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _get_empty_outcomes_data()


def _save_outcomes_data(data: dict[str, Any]):
    """Save outcomes data to file with atomic write."""
    _ensure_company_dir()
    path = _get_outcomes_path()

    # Apply retention cleanup
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.isoformat()

    data["outcomes"] = [
        outcome
        for outcome in data.get("outcomes", [])
        if outcome.get("recorded_at", "") >= cutoff_str
    ]

    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to temp file, then os.replace
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix="learning_", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# Core Functions
# =============================================================================


def record_outcome(outcome: OutcomeRecord) -> dict[str, Any]:
    """
    Record a task execution outcome.

    Appends the outcome to the learning outcomes file for analysis.

    Args:
        outcome: The outcome record to store

    Returns:
        Dict with recording result including outcome_id
    """
    data = _load_outcomes_data()

    outcome_dict = outcome.to_dict()
    data.setdefault("outcomes", []).append(outcome_dict)

    # Invalidate cached insights
    data["cached_insights"] = None

    _save_outcomes_data(data)

    return {
        "success": True,
        "task_id": outcome.task_id,
        "recorded_at": outcome.recorded_at.isoformat(),
        "total_outcomes": len(data["outcomes"]),
    }


def load_outcomes(days: int = ROLLING_WINDOW_DAYS) -> list[OutcomeRecord]:
    """
    Load outcomes within the rolling window.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of OutcomeRecord objects
    """
    data = _load_outcomes_data()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    outcomes = []
    for outcome_dict in data.get("outcomes", []):
        recorded_at = outcome_dict.get("recorded_at", "")
        if recorded_at >= cutoff_str:
            try:
                outcomes.append(OutcomeRecord.from_dict(outcome_dict))
            except (KeyError, ValueError):
                continue

    return outcomes


def _calculate_pearson_correlation(x: list[float], y: list[float]) -> float:
    """
    Calculate Pearson correlation coefficient between two lists.

    Returns value between -1 and 1, or 0.0 if calculation is not possible.
    """
    n = len(x)
    if n != len(y) or n < 2:
        return 0.0

    # Calculate means
    mean_x = sum(x) / n
    mean_y = sum(y) / n

    # Calculate deviations
    dev_x = [xi - mean_x for xi in x]
    dev_y = [yi - mean_y for yi in y]

    # Calculate sums of squares
    sum_dev_xy = sum(dx * dy for dx, dy in zip(dev_x, dev_y))
    sum_dev_x2 = sum(dx * dx for dx in dev_x)
    sum_dev_y2 = sum(dy * dy for dy in dev_y)

    # Avoid division by zero
    denominator = (sum_dev_x2 * sum_dev_y2) ** 0.5
    if denominator == 0:
        return 0.0

    return sum_dev_xy / denominator


def compute_insights(outcomes: list[OutcomeRecord] | None = None) -> LearningInsights:
    """
    Compute learning insights from outcome data.

    Analyzes outcomes to determine:
    - Best employees for each complexity level
    - Failure patterns (task types with high failure rates)
    - Optimal time windows for task execution
    - Correlation between plan scores and success

    Args:
        outcomes: List of outcomes to analyze (loads from file if None)

    Returns:
        LearningInsights object with computed insights
    """
    if outcomes is None:
        outcomes = load_outcomes()

    now = datetime.now(timezone.utc)

    if not outcomes:
        return LearningInsights(
            best_employees_by_complexity={
                "trivial": [],
                "standard": [],
                "complex": [],
                "epic": [],
            },
            failure_patterns=[],
            optimal_time_windows=[],
            plan_score_success_correlation=0.0,
            employee_performance={},
            computed_at=now,
        )

    # -------------------------------------------------------------------------
    # Group outcomes by employee and complexity
    # -------------------------------------------------------------------------
    employee_outcomes: dict[str, dict[str, list[OutcomeRecord]]] = {}
    for outcome in outcomes:
        if outcome.employee_id not in employee_outcomes:
            employee_outcomes[outcome.employee_id] = {}
        complexity = outcome.complexity
        if complexity not in employee_outcomes[outcome.employee_id]:
            employee_outcomes[outcome.employee_id][complexity] = []
        employee_outcomes[outcome.employee_id][complexity].append(outcome)

    # -------------------------------------------------------------------------
    # Calculate employee performance metrics
    # -------------------------------------------------------------------------
    employee_performance: dict[str, dict[str, Any]] = {}
    complexity_rankings: dict[str, list[tuple[str, float]]] = {
        "trivial": [],
        "standard": [],
        "complex": [],
        "epic": [],
    }

    for employee_id, complexity_outcomes in employee_outcomes.items():
        all_outcomes = [o for outs in complexity_outcomes.values() for o in outs]
        if (
            not all_outcomes
        ):  # pragma: no cover - grouping loop above always appends >=1 outcome
            continue

        total_tasks = len(all_outcomes)
        successes = sum(1 for o in all_outcomes if o.success)
        first_passes = sum(1 for o in all_outcomes if o.first_pass)
        total_time = sum(o.execution_minutes for o in all_outcomes)
        total_tokens = sum(o.tokens_used for o in all_outcomes)

        success_rate = successes / total_tasks if total_tasks > 0 else 0.0
        first_pass_rate = first_passes / total_tasks if total_tasks > 0 else 0.0
        avg_time = total_time / total_tasks if total_tasks > 0 else 0.0
        avg_tokens = int(total_tokens / total_tasks) if total_tasks > 0 else 0

        # Efficiency score: success_rate * (1 / avg_time_normalized)
        # Normalize time to 0-1 range (assuming 120 min as max reasonable time)
        time_factor = 1.0 / (1.0 + avg_time / 60.0)  # Diminishing returns formula
        efficiency_score = success_rate * time_factor

        by_complexity: dict[str, dict[str, Any]] = {}
        for complexity, comp_outcomes in complexity_outcomes.items():
            comp_total = len(comp_outcomes)
            comp_successes = sum(1 for o in comp_outcomes if o.success)
            comp_time = sum(o.execution_minutes for o in comp_outcomes)
            comp_rate = comp_successes / comp_total if comp_total > 0 else 0.0
            comp_avg_time = comp_time / comp_total if comp_total > 0 else 0.0

            by_complexity[complexity] = {
                "total_tasks": comp_total,
                "success_rate": round(comp_rate, 3),
                "avg_execution_minutes": round(comp_avg_time, 2),
            }

            # Add to complexity rankings if sufficient sample size
            if comp_total >= MIN_SAMPLE_SIZE:
                # Ranking score: success_rate * (1 / avg_time)
                rank_score = comp_rate * (1.0 / (1.0 + comp_avg_time / 60.0))
                complexity_rankings[complexity].append((employee_id, rank_score))

        employee_performance[employee_id] = {
            "total_tasks": total_tasks,
            "success_rate": round(success_rate, 3),
            "first_pass_rate": round(first_pass_rate, 3),
            "avg_execution_minutes": round(avg_time, 2),
            "avg_tokens_used": avg_tokens,
            "efficiency_score": round(efficiency_score, 3),
            "by_complexity": by_complexity,
        }

    # -------------------------------------------------------------------------
    # Rank employees by complexity
    # -------------------------------------------------------------------------
    best_employees_by_complexity: dict[str, list[str]] = {}
    for complexity, rankings in complexity_rankings.items():
        # Sort by score descending
        rankings.sort(key=lambda x: x[1], reverse=True)
        best_employees_by_complexity[complexity] = [emp_id for emp_id, _ in rankings]

    # -------------------------------------------------------------------------
    # Detect failure patterns
    # -------------------------------------------------------------------------
    failure_patterns: list[dict[str, Any]] = []

    # Group by task_type and complexity
    task_type_outcomes: dict[tuple[str, str], list[OutcomeRecord]] = {}
    for outcome in outcomes:
        key = (outcome.task_type or "unknown", outcome.complexity)
        if key not in task_type_outcomes:
            task_type_outcomes[key] = []
        task_type_outcomes[key].append(outcome)

    for (task_type, complexity), type_outcomes in task_type_outcomes.items():
        if len(type_outcomes) < MIN_SAMPLE_SIZE:
            continue

        failures = [o for o in type_outcomes if not o.success]
        failure_rate = len(failures) / len(type_outcomes)

        if failure_rate >= (1 - FAILURE_PATTERN_THRESHOLD):
            # Collect common tags from failures
            tag_counts: dict[str, int] = {}
            for o in failures:
                for tag in o.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            common_tags = sorted(
                tag_counts.keys(), key=lambda t: tag_counts[t], reverse=True
            )[:5]

            failure_patterns.append(
                {
                    "pattern_id": f"failure-{task_type}-{complexity}",
                    "description": f"High failure rate for {task_type} tasks at {complexity} complexity",
                    "task_type": task_type,
                    "complexity": complexity,
                    "failure_rate": round(failure_rate, 3),
                    "sample_size": len(type_outcomes),
                    "common_tags": common_tags,
                    "recent_examples": [o.task_id for o in failures[:5]],
                }
            )

    # -------------------------------------------------------------------------
    # Find optimal time windows
    # -------------------------------------------------------------------------
    hourly_success: dict[int, tuple[int, int]] = {}  # hour -> (successes, total)
    for outcome in outcomes:
        hour = outcome.recorded_at.hour
        if hour not in hourly_success:
            hourly_success[hour] = (0, 0)
        successes, total = hourly_success[hour]
        hourly_success[hour] = (
            successes + (1 if outcome.success else 0),
            total + 1,
        )

    # Find hours with above-average success rate
    if hourly_success:
        overall_success_rate = sum(s for s, _ in hourly_success.values()) / sum(
            t for _, t in hourly_success.values()
        )

        good_hours = []
        for hour, (successes, total) in hourly_success.items():
            if total >= 3:  # Minimum sample
                rate = successes / total
                if rate >= overall_success_rate:
                    good_hours.append(hour)

        # Group consecutive hours into windows
        optimal_time_windows: list[tuple[int, int]] = []
        if good_hours:
            good_hours.sort()
            start = good_hours[0]
            end = good_hours[0]
            for hour in good_hours[1:]:
                if hour == end + 1:
                    end = hour
                else:
                    optimal_time_windows.append((start, end + 1))
                    start = hour
                    end = hour
            optimal_time_windows.append((start, end + 1))
    else:  # pragma: no cover - unreachable, outcomes is non-empty past the early return above
        optimal_time_windows = []

    # -------------------------------------------------------------------------
    # Calculate plan score to success correlation
    # -------------------------------------------------------------------------
    outcomes_with_scores = [o for o in outcomes if o.plan_score > 0]
    if len(outcomes_with_scores) >= MIN_SAMPLE_SIZE:
        plan_scores = [float(o.plan_score) for o in outcomes_with_scores]
        success_values = [1.0 if o.success else 0.0 for o in outcomes_with_scores]
        correlation = _calculate_pearson_correlation(plan_scores, success_values)
    else:
        correlation = 0.0

    return LearningInsights(
        best_employees_by_complexity=best_employees_by_complexity,
        failure_patterns=failure_patterns,
        optimal_time_windows=optimal_time_windows,
        plan_score_success_correlation=round(correlation, 3),
        employee_performance=employee_performance,
        computed_at=now,
    )


def get_best_employee_for_task(
    task: dict[str, Any],
    insights: LearningInsights | None = None,
) -> str | None:
    """
    Get the best employee for a given task based on learning insights.

    Args:
        task: Task dictionary with at least 'complexity' field
        insights: Pre-computed insights (computes if None)

    Returns:
        Employee ID of the best match, or None if no recommendation
    """
    if insights is None:
        insights = compute_insights()

    complexity = task.get("complexity", "standard")
    best_employees = insights.best_employees_by_complexity.get(complexity, [])

    if not best_employees:
        # Fallback: try adjacent complexity levels
        fallback_order = {
            "trivial": ["standard", "complex"],
            "standard": ["complex", "trivial"],
            "complex": ["standard", "epic"],
            "epic": ["complex", "standard"],
        }
        for fallback_complexity in fallback_order.get(complexity, []):
            fallback_employees = insights.best_employees_by_complexity.get(
                fallback_complexity, []
            )
            if fallback_employees:
                return fallback_employees[0]
        return None

    return best_employees[0]


def should_defer_task(
    task: dict[str, Any],
    insights: LearningInsights | None = None,
) -> tuple[bool, str]:
    """
    Determine if a task should be deferred based on learning insights.

    Reasons to defer:
    - Task matches a known failure pattern
    - Current time is outside optimal windows
    - Plan score is below threshold

    Args:
        task: Task dictionary
        insights: Pre-computed insights (computes if None)

    Returns:
        Tuple of (should_defer, reason)
    """
    if insights is None:
        insights = compute_insights()

    task_type = task.get("task_type", task.get("type", "unknown"))
    complexity = task.get("complexity", "standard")
    plan_score = task.get("plan_score", 0)

    # Check for failure pattern match
    for pattern in insights.failure_patterns:
        if pattern["task_type"] == task_type and pattern["complexity"] == complexity:
            if pattern["failure_rate"] >= 0.5:  # More than 50% failure rate
                return (
                    True,
                    f"Matches failure pattern: {pattern['description']} "
                    f"({pattern['failure_rate']:.0%} failure rate)",
                )

    # Check time window (only if we have enough data)
    if insights.optimal_time_windows:
        current_hour = datetime.now(timezone.utc).hour
        in_optimal_window = any(
            start <= current_hour < end for start, end in insights.optimal_time_windows
        )
        if not in_optimal_window and complexity in ("complex", "epic"):
            # Only defer complex/epic tasks for time windows
            windows_str = ", ".join(
                f"{s}:00-{e}:00" for s, e in insights.optimal_time_windows
            )
            return (
                True,
                f"Complex task outside optimal time windows ({windows_str})",
            )

    # Check plan score correlation
    if insights.plan_score_success_correlation > 0.5:
        # Strong positive correlation - low plan scores predict failure
        if plan_score > 0 and plan_score < 15:  # Below 60% (15/25)
            return (
                True,
                f"Low plan score ({plan_score}/25) with strong correlation to failure",
            )

    return (False, "")


def generate_improvement_proposals(
    insights: LearningInsights | None = None,
) -> list[dict[str, Any]]:
    """
    Generate improvement proposals based on learning insights.

    Analyzes insights to suggest:
    - Training recommendations for struggling employees
    - Process improvements for failure patterns
    - Scheduling optimizations

    Args:
        insights: Pre-computed insights (computes if None)

    Returns:
        List of proposal dictionaries
    """
    if insights is None:
        insights = compute_insights()

    proposals: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Proposal 1: Address failure patterns
    # -------------------------------------------------------------------------
    for pattern in insights.failure_patterns:
        if pattern["failure_rate"] >= 0.5:
            proposals.append(
                {
                    "proposal_id": f"improve-{pattern['pattern_id']}",
                    "type": "failure_pattern_mitigation",
                    "title": f"Address {pattern['task_type']} failure pattern",
                    "description": (
                        f"Tasks of type '{pattern['task_type']}' at {pattern['complexity']} "
                        f"complexity have a {pattern['failure_rate']:.0%} failure rate "
                        f"(sample size: {pattern['sample_size']}). "
                        f"Common tags: {', '.join(pattern['common_tags'][:3])}."
                    ),
                    "recommendations": [
                        "Review recent failures for common root causes",
                        "Consider breaking down into smaller tasks",
                        "Add pre-execution validation checks",
                        "Assign to employees with proven success in this area",
                    ],
                    "priority": 2 if pattern["failure_rate"] >= 0.7 else 3,
                    "estimated_impact": "high"
                    if pattern["failure_rate"] >= 0.7
                    else "medium",
                }
            )

    # -------------------------------------------------------------------------
    # Proposal 2: Employee skill development
    # -------------------------------------------------------------------------
    for employee_id, metrics in insights.employee_performance.items():
        if metrics["success_rate"] < 0.7 and metrics["total_tasks"] >= MIN_SAMPLE_SIZE:
            weak_complexities = [
                comp
                for comp, comp_metrics in metrics.get("by_complexity", {}).items()
                if comp_metrics.get("success_rate", 1.0) < 0.6
                and comp_metrics.get("total_tasks", 0) >= 3
            ]

            if weak_complexities:
                proposals.append(
                    {
                        "proposal_id": f"training-{employee_id}",
                        "type": "skill_development",
                        "title": f"Skill development for {employee_id}",
                        "description": (
                            f"Employee {employee_id} has a {metrics['success_rate']:.0%} "
                            f"overall success rate. Struggling with: {', '.join(weak_complexities)} tasks."
                        ),
                        "recommendations": [
                            f"Pair with high-performing employee on {weak_complexities[0]} tasks",
                            "Review and provide feedback on recent failures",
                            "Consider task routing away from weak areas temporarily",
                        ],
                        "priority": 3,
                        "estimated_impact": "medium",
                    }
                )

    # -------------------------------------------------------------------------
    # Proposal 3: Leverage high performers
    # -------------------------------------------------------------------------
    for complexity in ("complex", "epic"):
        best = insights.best_employees_by_complexity.get(complexity, [])
        if best:
            top_performer = best[0]
            top_metrics = insights.employee_performance.get(top_performer, {})
            if top_metrics.get("success_rate", 0) >= 0.9:
                proposals.append(
                    {
                        "proposal_id": f"leverage-{top_performer}-{complexity}",
                        "type": "resource_optimization",
                        "title": f"Prioritize {top_performer} for {complexity} tasks",
                        "description": (
                            f"{top_performer} has exceptional performance on {complexity} tasks "
                            f"({top_metrics.get('success_rate', 0):.0%} success rate). "
                            "Consider routing more critical work to this employee."
                        ),
                        "recommendations": [
                            f"Route high-priority {complexity} tasks to {top_performer}",
                            "Have other employees shadow for knowledge transfer",
                            "Capture patterns/approaches as reusable knowledge",
                        ],
                        "priority": 4,
                        "estimated_impact": "medium",
                    }
                )

    # -------------------------------------------------------------------------
    # Proposal 4: Scheduling optimization
    # -------------------------------------------------------------------------
    if insights.optimal_time_windows and len(insights.optimal_time_windows) < 12:
        # We have identified specific good windows (not just "all times are good")
        windows_str = ", ".join(
            f"{s}:00-{e}:00 UTC" for s, e in insights.optimal_time_windows
        )
        proposals.append(
            {
                "proposal_id": "scheduling-optimization",
                "type": "process_improvement",
                "title": "Optimize task scheduling for peak performance",
                "description": (
                    f"Analysis shows higher success rates during: {windows_str}. "
                    "Consider scheduling complex tasks during these windows."
                ),
                "recommendations": [
                    "Schedule complex/epic tasks during optimal windows",
                    "Use non-optimal hours for routine/trivial tasks",
                    "Monitor if pattern holds after adjustment",
                ],
                "priority": 4,
                "estimated_impact": "low",
            }
        )

    # -------------------------------------------------------------------------
    # Proposal 5: Plan quality improvement
    # -------------------------------------------------------------------------
    if insights.plan_score_success_correlation > 0.6:
        proposals.append(
            {
                "proposal_id": "plan-quality-focus",
                "type": "process_improvement",
                "title": "Invest more in planning phase",
                "description": (
                    f"Strong correlation ({insights.plan_score_success_correlation:.2f}) "
                    "between plan scores and task success. Higher quality plans lead "
                    "to better outcomes."
                ),
                "recommendations": [
                    "Allocate more time/tokens to planning phase",
                    "Require minimum plan score before execution",
                    "Add planning review step for complex tasks",
                ],
                "priority": 3,
                "estimated_impact": "high",
            }
        )

    # Sort by priority
    proposals.sort(key=lambda p: p.get("priority", 5))

    return proposals


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="P25 Learning Loop — Outcome-Based Improvement System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Record command
    record_parser = subparsers.add_parser("record", help="Record a task outcome")
    record_parser.add_argument("--task-id", required=True, help="Task ID")
    record_parser.add_argument("--employee-id", required=True, help="Employee ID")
    record_parser.add_argument(
        "--complexity",
        default="standard",
        choices=["trivial", "standard", "complex", "epic"],
        help="Task complexity",
    )
    record_parser.add_argument(
        "--success", type=str, default="true", help="Success (true/false)"
    )
    record_parser.add_argument(
        "--first-pass", type=str, default="true", help="First pass success"
    )
    record_parser.add_argument(
        "--revision-count", type=int, default=0, help="Number of revisions"
    )
    record_parser.add_argument(
        "--plan-score", type=int, default=0, help="Plan score (0-25)"
    )
    record_parser.add_argument(
        "--execution-minutes", type=float, default=0.0, help="Execution time"
    )
    record_parser.add_argument("--tokens-used", type=int, default=0, help="Tokens used")
    record_parser.add_argument("--task-type", default="", help="Task type/category")
    record_parser.add_argument("--tags", default="", help="Comma-separated tags")

    # Insights command
    subparsers.add_parser("insights", help="Compute and display insights")

    # Suggest command
    suggest_parser = subparsers.add_parser("suggest", help="Get best employee for task")
    suggest_parser.add_argument(
        "--complexity",
        default="standard",
        choices=["trivial", "standard", "complex", "epic"],
        help="Task complexity",
    )

    # Defer command
    defer_parser = subparsers.add_parser(
        "defer", help="Check if task should be deferred"
    )
    defer_parser.add_argument("--task-id", required=True, help="Task ID")
    defer_parser.add_argument(
        "--complexity",
        default="standard",
        choices=["trivial", "standard", "complex", "epic"],
        help="Task complexity",
    )
    defer_parser.add_argument("--plan-score", type=int, default=0, help="Plan score")
    defer_parser.add_argument("--task-type", default="", help="Task type")

    # Proposals command
    subparsers.add_parser("proposals", help="Generate improvement proposals")

    # Help command
    subparsers.add_parser("help", help="Show help")

    args = parser.parse_args()

    if args.command == "help" or args.command is None:
        parser.print_help()
        return

    if args.command == "record":
        outcome = OutcomeRecord(
            task_id=args.task_id,
            employee_id=args.employee_id,
            complexity=args.complexity,
            success=args.success.lower() == "true",
            first_pass=args.first_pass.lower() == "true",
            revision_count=args.revision_count,
            plan_score=args.plan_score,
            execution_minutes=args.execution_minutes,
            tokens_used=args.tokens_used,
            recorded_at=datetime.now(timezone.utc),
            task_type=args.task_type,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        )
        result = record_outcome(outcome)
        print(json.dumps(result, indent=2))

    elif args.command == "insights":
        insights = compute_insights()
        print(json.dumps(insights.to_dict(), indent=2))

    elif args.command == "suggest":
        task = {"complexity": args.complexity}
        insights = compute_insights()
        best = get_best_employee_for_task(task, insights)
        print(
            json.dumps({"best_employee": best, "complexity": args.complexity}, indent=2)
        )

    elif args.command == "defer":
        task = {
            "task_id": args.task_id,
            "complexity": args.complexity,
            "plan_score": args.plan_score,
            "task_type": args.task_type,
        }
        should_defer, reason = should_defer_task(task)
        print(
            json.dumps(
                {
                    "should_defer": should_defer,
                    "reason": reason,
                    "task_id": args.task_id,
                },
                indent=2,
            )
        )

    elif (
        args.command == "proposals"
    ):  # pragma: no branch - argparse restricts to defined subcommands
        proposals = generate_improvement_proposals()
        print(json.dumps({"proposals": proposals, "count": len(proposals)}, indent=2))


if __name__ == "__main__":
    main()
