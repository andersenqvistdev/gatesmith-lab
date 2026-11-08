#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Goal Scheduler — priority-based task ordering driven by strategic goals.

P25 Implementation: Full Autonomous Operation.

This module provides goal-driven scheduling that dynamically reorders the work
queue based on strategic priorities. It computes priority scores from goal
progress, deadlines, and strategic weights, then reorders tasks while
respecting dependencies.

Key Features:
1. Compute goal priorities from goal_tracker progress and deadlines
2. Reorder queue tasks by combined priority (task + goal bonus)
3. Preserve dependency ordering (blocked tasks stay after blockers)
4. Integration with daemon work loop for continuous optimization

Usage:
    # Compute goal priorities
    python goal_scheduler.py priorities

    # Reorder work queue by goals
    python goal_scheduler.py reorder

    # Get priority for specific task
    python goal_scheduler.py task-priority --task-id "task-123"

    # Show reorder preview without applying
    python goal_scheduler.py preview
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("goal_scheduler")

# Lazy imports for sibling modules
goal_tracker = None
company_resolver = None
work_allocator = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global goal_tracker, company_resolver, work_allocator

    # All three, not just goal_tracker: a test that patches company_resolver
    # while it is still None restores None on exit, leaving goal_tracker set
    # and company_resolver unset -- the next caller then crashed on None.
    if (
        goal_tracker is not None
        and company_resolver is not None
        and work_allocator is not None
    ):
        return

    try:
        from . import company_resolver as cr
        from . import goal_tracker as gt
        from . import work_allocator as wa

        goal_tracker = gt
        company_resolver = cr
        work_allocator = wa
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import goal_tracker as gt  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        goal_tracker = gt
        company_resolver = cr
        work_allocator = wa


# =============================================================================
# Configuration
# =============================================================================

# Default strategic weights for goals (can be overridden in gatesmith.json)
# WS-054-002: Rebalanced for 50%+ product/revenue focus
DEFAULT_STRATEGIC_WEIGHTS: dict[str, float] = {
    "G1": 0.8,  # Quality - foundation (reduced from 1.0)
    "G2": 1.3,  # Adoption - product growth driver (boosted from 0.8)
    "G3": 1.0,  # Stability - important but not dominant (reduced from 1.2)
    "G4": 1.3,  # Enterprise/Product - revenue driver (boosted from 0.7)
    "G5": 0.9,  # Autonomy - supporting (reduced from 1.5)
    "G6": 1.0,  # Economics - steady
    "G7": 0.8,  # Sustained Autonomy - infrastructure
    "G8": 0.7,  # Self-Improvement - infrastructure
    "G9": 0.8,  # Stability - maintenance
    "G10": 1.4,  # Employee Initiative - product innovation (highest)
    "G11": 0.9,  # Queue Health - operational
    # Q3-B 2026 goal set. Both outrank the maintenance lanes because they
    # build the machinery every other goal's numbers are read through: G17 is
    # the oracle outside the artifact (GAP-7), G18 is the evidence that the
    # blocking controls actually block.
    "G17": 1.2,  # Acceptance Ledger - the trust substrate
    "G18": 1.1,  # Controls That Fire - proof the safety layer works
}

# Goal category mapping for department routing (WS-054-002)
GOAL_CATEGORIES: dict[str, str] = {
    "G1": "quality",
    "G2": "product",
    "G3": "infrastructure",
    "G4": "revenue",
    "G5": "autonomy",
    "G6": "revenue",
    "G7": "autonomy",
    "G8": "infrastructure",
    "G9": "quality",
    "G10": "product",
    "G11": "infrastructure",
    # "quality" for both: CATEGORY_DEPARTMENTS has no "security" key, and an
    # unmapped category routes to no department at all — worse for G18 than
    # the engineering department it would land in anyway.
    "G17": "quality",
    "G18": "quality",
}

# Map goal categories to departments for task routing
CATEGORY_DEPARTMENTS: dict[str, str] = {
    "product": "product",
    "revenue": "sales",
    "quality": "engineering",
    "infrastructure": "engineering",
    "autonomy": "engineering",
}

# Priority bonus scaling factor (goal priority -> task priority boost)
PRIORITY_BONUS_SCALE = 0.5

# Maximum priority bonus to prevent runaway priority inflation
MAX_PRIORITY_BONUS = 3.0

# Deadline urgency thresholds (days)
DEADLINE_CRITICAL_DAYS = 3
DEADLINE_URGENT_DAYS = 7
DEADLINE_NEAR_DAYS = 14


def load_config() -> dict[str, Any]:
    """
    Load goal scheduling configuration from gatesmith.json.

    Returns:
        dict with configuration values:
        - enabled: bool
        - strategicWeights: dict[str, float] (Gatesmith defaults merged with config)
        - configuredWeights: dict[str, float] (only what the config sets)
        - priorityBonusScale: float
        - maxPriorityBonus: float
        - deadlineThresholds: dict
    """
    config_paths = [
        Path.cwd() / ".claude" / "gatesmith.json",
        Path.cwd() / "gatesmith.json",
    ]

    defaults = {
        "enabled": True,
        "strategicWeights": DEFAULT_STRATEGIC_WEIGHTS.copy(),
        # Only the weights the repo's own config sets. DEFAULT_STRATEGIC_WEIGHTS
        # describe FORGE's goal set; see _strategic_weight_for.
        "configuredWeights": {},
        "priorityBonusScale": PRIORITY_BONUS_SCALE,
        "maxPriorityBonus": MAX_PRIORITY_BONUS,
        "deadlineThresholds": {
            "criticalDays": DEADLINE_CRITICAL_DAYS,
            "urgentDays": DEADLINE_URGENT_DAYS,
            "nearDays": DEADLINE_NEAR_DAYS,
        },
        "logReordering": True,
    }

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                goal_config = config.get("goalScheduling", {})
                return {
                    "enabled": goal_config.get("enabled", defaults["enabled"]),
                    "strategicWeights": {
                        **defaults["strategicWeights"],
                        **goal_config.get("strategicWeights", {}),
                    },
                    "configuredWeights": dict(
                        goal_config.get("strategicWeights", {}) or {}
                    ),
                    "priorityBonusScale": goal_config.get(
                        "priorityBonusScale", defaults["priorityBonusScale"]
                    ),
                    "maxPriorityBonus": goal_config.get(
                        "maxPriorityBonus", defaults["maxPriorityBonus"]
                    ),
                    "deadlineThresholds": {
                        **defaults["deadlineThresholds"],
                        **goal_config.get("deadlineThresholds", {}),
                    },
                    "logReordering": goal_config.get(
                        "logReordering", defaults["logReordering"]
                    ),
                }
            except (json.JSONDecodeError, OSError):
                pass

    return defaults


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class GoalPriority:
    """Priority information for a strategic goal."""

    goal_id: str  # e.g., "G5"
    goal_name: str  # e.g., "Autonomy"
    current_progress: float  # 0.0-1.0
    target_progress: float  # Usually 1.0
    deadline: datetime | None  # Optional deadline
    strategic_weight: float  # From config
    priority_score: float  # Computed final score
    gap_size: float  # target - current (0.0-1.0)
    urgency_multiplier: float  # Based on deadline proximity
    status: str  # "on_track", "at_risk", "blocked", "complete"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "goal_id": self.goal_id,
            "goal_name": self.goal_name,
            "current_progress": self.current_progress,
            "target_progress": self.target_progress,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "strategic_weight": self.strategic_weight,
            "priority_score": self.priority_score,
            "gap_size": self.gap_size,
            "urgency_multiplier": self.urgency_multiplier,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalPriority":
        """Create from dictionary."""
        deadline = None
        if data.get("deadline"):
            try:
                deadline = datetime.fromisoformat(data["deadline"])
            except (ValueError, TypeError):
                pass

        return cls(
            goal_id=data.get("goal_id", ""),
            goal_name=data.get("goal_name", ""),
            current_progress=data.get("current_progress", 0.0),
            target_progress=data.get("target_progress", 1.0),
            deadline=deadline,
            strategic_weight=data.get("strategic_weight", 1.0),
            priority_score=data.get("priority_score", 0.0),
            gap_size=data.get("gap_size", 0.0),
            urgency_multiplier=data.get("urgency_multiplier", 1.0),
            status=data.get("status", "not_started"),
        )


@dataclass
class ReorderResult:
    """Result of a queue reordering operation."""

    success: bool
    tasks_reordered: int
    reorder_reason: str
    original_order: list[str]  # Task IDs in original order
    new_order: list[str]  # Task IDs in new order
    goal_priorities: dict[str, float]  # goal_id -> priority_score
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class GoalVelocityReport:
    """Report of goal completion velocity metrics."""

    goal_id: str
    goal_name: str
    tasks_completed: int  # Total tasks completed for this goal
    tasks_per_day: float  # Average tasks completed per day
    period_days: int  # Analysis period in days
    trend: str  # "improving", "stable", "declining"
    last_completion: str | None  # ISO timestamp of last completion
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


# =============================================================================
# Goal Tracking Persistence
# =============================================================================


def _get_tracking_path() -> Path:
    """Get path to goal_tracking.json file."""
    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    return company_dir / "goal_tracking.json"


def _load_tracking_data() -> dict[str, Any]:
    """
    Load goal tracking data from persistence file.

    Returns:
        Dictionary with tracking data:
        - goal_completions: list of completion events
        - initiative_outcomes: list of initiative outcome records
        - last_updated: ISO timestamp
    """
    tracking_path = _get_tracking_path()

    if not tracking_path.exists():
        return {
            "goal_completions": [],
            "initiative_outcomes": [],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    try:
        with open(tracking_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "goal_completions": [],
            "initiative_outcomes": [],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }


def _save_tracking_data(data: dict[str, Any]) -> None:
    """
    Save goal tracking data to persistence file with atomic write.

    Args:
        data: Tracking data dictionary to persist
    """
    import os
    import tempfile

    tracking_path = _get_tracking_path()

    # Ensure parent directory exists
    tracking_path.parent.mkdir(parents=True, exist_ok=True)

    # Update timestamp
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Atomic write to prevent corruption
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="goal_tracking_",
        dir=str(tracking_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, tracking_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# =============================================================================
# Goal Tracking Methods
# =============================================================================


def track_goal_completion(goal_id: str, completed: bool) -> None:
    """
    Track a goal completion event.

    Records whether a goal (or goal-related task) was completed or failed.
    Used for computing velocity metrics and trend analysis.

    Args:
        goal_id: The goal identifier (e.g., "G5")
        completed: True if completed successfully, False if failed
    """
    data = _load_tracking_data()

    completion_event = {
        "goal_id": goal_id,
        "completed": completed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    data["goal_completions"].append(completion_event)
    _save_tracking_data(data)

    logger.info(f"Tracked goal completion: {goal_id} completed={completed}")


def record_initiative_outcome(
    initiative_id: str, success: bool, tasks_completed: int
) -> None:
    """
    Record the outcome of an initiative execution.

    Initiatives are strategic units of work that may span multiple tasks.
    This records the overall success/failure and task completion count.

    Args:
        initiative_id: The initiative identifier
        success: True if initiative succeeded, False if failed
        tasks_completed: Number of tasks completed within this initiative
    """
    data = _load_tracking_data()

    outcome_record = {
        "initiative_id": initiative_id,
        "success": success,
        "tasks_completed": tasks_completed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    data["initiative_outcomes"].append(outcome_record)
    _save_tracking_data(data)

    logger.info(
        f"Recorded initiative outcome: {initiative_id} "
        f"success={success} tasks={tasks_completed}"
    )


def compute_goal_velocity(goal_id: str, days: int = 7) -> float:
    """
    Compute task completion velocity for a goal.

    Calculates the average number of tasks completed per day for the
    specified goal over the given time period.

    Args:
        goal_id: The goal identifier (e.g., "G5")
        days: Number of days to look back (default 7)

    Returns:
        Tasks completed per day (float). Returns 0.0 if no data.
    """
    if days <= 0:
        return 0.0

    data = _load_tracking_data()
    completions = data.get("goal_completions", [])

    # Calculate cutoff timestamp
    now = datetime.now(timezone.utc)
    cutoff = now - __import__("datetime").timedelta(days=days)

    # Count successful completions within the period
    completed_count = 0
    for event in completions:
        if event.get("goal_id") != goal_id:
            continue
        if not event.get("completed", False):
            continue

        try:
            event_time = datetime.fromisoformat(event["timestamp"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if event_time >= cutoff:
                completed_count += 1
        except (ValueError, TypeError, KeyError):
            continue

    return completed_count / days


def get_velocity_report(
    goal_id: str | None = None, days: int = 7
) -> list[GoalVelocityReport]:
    """
    Generate velocity reports for goals.

    Computes completion velocity metrics and trend analysis for the
    specified goal or all goals if none specified.

    Args:
        goal_id: Optional specific goal ID. If None, reports for all goals.
        days: Number of days for velocity calculation (default 7)

    Returns:
        List of GoalVelocityReport objects
    """
    data = _load_tracking_data()
    completions = data.get("goal_completions", [])

    # Gather all goal IDs if none specified
    if goal_id:
        goal_ids = [goal_id]
    else:
        goal_ids = list({e.get("goal_id") for e in completions if e.get("goal_id")})
        if not goal_ids:
            # No tracking data yet - return empty list
            return []

    # Get goal names from priorities (if available)
    try:
        priorities = compute_goal_priorities()
    except Exception:
        priorities = {}

    reports: list[GoalVelocityReport] = []
    now = datetime.now(timezone.utc)
    cutoff = now - __import__("datetime").timedelta(days=days)
    half_cutoff = now - __import__("datetime").timedelta(days=days // 2)

    for gid in sorted(goal_ids):
        # Filter completions for this goal
        goal_completions = [e for e in completions if e.get("goal_id") == gid]

        # Get successful completions in the period
        recent_completions = []
        first_half_count = 0
        second_half_count = 0
        last_completion_time: str | None = None

        for event in goal_completions:
            if not event.get("completed", False):
                continue

            try:
                event_time = datetime.fromisoformat(event["timestamp"])
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)

                # Track last completion
                if (
                    last_completion_time is None
                    or event["timestamp"] > last_completion_time
                ):
                    last_completion_time = event["timestamp"]

                # Within analysis period
                if event_time >= cutoff:
                    recent_completions.append(event)
                    if event_time >= half_cutoff:
                        second_half_count += 1
                    else:
                        first_half_count += 1
            except (ValueError, TypeError, KeyError):
                continue

        # Compute velocity
        tasks_completed = len(recent_completions)
        tasks_per_day = tasks_completed / days if days > 0 else 0.0

        # Determine trend
        if tasks_completed == 0:
            trend = "stable"
        elif second_half_count > first_half_count:
            trend = "improving"
        elif second_half_count < first_half_count:
            trend = "declining"
        else:
            trend = "stable"

        # Get goal name
        goal_name = gid
        if gid in priorities:
            goal_name = priorities[gid].goal_name

        reports.append(
            GoalVelocityReport(
                goal_id=gid,
                goal_name=goal_name,
                tasks_completed=tasks_completed,
                tasks_per_day=tasks_per_day,
                period_days=days,
                trend=trend,
                last_completion=last_completion_time,
            )
        )

    return reports


# =============================================================================
# Goal Priority Computation
# =============================================================================


def compute_urgency_multiplier(
    deadline: datetime | None, config: dict[str, Any]
) -> float:
    """
    Compute urgency multiplier based on deadline proximity.

    Args:
        deadline: Optional deadline datetime
        config: Configuration with deadline thresholds

    Returns:
        Urgency multiplier (1.0 = normal, higher = more urgent)
    """
    if deadline is None:
        return 1.0

    now = datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        # Assume UTC for naive datetimes
        deadline = deadline.replace(tzinfo=timezone.utc)

    days_until = (deadline - now).days

    thresholds = config.get("deadlineThresholds", {})
    critical_days = thresholds.get("criticalDays", DEADLINE_CRITICAL_DAYS)
    urgent_days = thresholds.get("urgentDays", DEADLINE_URGENT_DAYS)
    near_days = thresholds.get("nearDays", DEADLINE_NEAR_DAYS)

    if days_until < 0:
        # Past deadline - maximum urgency
        return 3.0
    elif days_until <= critical_days:
        return 2.5
    elif days_until <= urgent_days:
        return 2.0
    elif days_until <= near_days:
        return 1.5
    else:
        return 1.0


def _strategic_weight_for(assessment: Any, config: dict[str, Any]) -> float:
    """The strategic weight for one goal, without leaking Gatesmith's into a product.

    DEFAULT_STRATEGIC_WEIGHTS is keyed by goal ID and describes FORGE's own goal
    set ("G1" = Gatesmith's test coverage, 0.8). A product repo reuses the same IDs
    for unrelated goals -- the mdlint sandbox's "G1: Core engine" was scheduled
    at Gatesmith's 0.8 quality weight, and every "Gn" at whatever Gatesmith happens to
    weight its own Gn. So:

    1. A weight the repo's own config sets for this ID always wins.
    2. Otherwise Gatesmith's default applies only when the goal is verifiably Gatesmith's
       own goal for that ID (the same name + metric fingerprint the assessor
       dispatch uses, goal_tracker._is_framework_dogfood_goal).
    3. Anything else gets the neutral 1.0.

    A caller-built config without ``configuredWeights`` (tests, older callers)
    has its ``strategicWeights`` treated as explicitly configured, which is
    exactly what such a dict means.
    """
    goal_id = getattr(assessment, "goal_id", "")
    explicit = config.get("configuredWeights")
    if explicit is None:
        explicit = config.get("strategicWeights") or {}
    if goal_id in explicit:
        return explicit[goal_id]
    if goal_tracker is not None and goal_tracker.is_framework_dogfood_assessment(
        assessment
    ):
        return DEFAULT_STRATEGIC_WEIGHTS.get(goal_id, 1.0)
    return 1.0


def compute_goal_priorities(
    assessments: list | None = None,
) -> dict[str, GoalPriority]:
    """
    Compute goal priorities from goal assessments.

    Uses goal_tracker to get current progress and computes priority scores
    based on gap size, deadline proximity, and strategic weight.

    Formula: priority_score = (gap_size * urgency_multiplier) * strategic_weight

    Args:
        assessments: Optional list of GoalAssessment objects. If None,
                    fetches from goal_tracker.

    Returns:
        Dictionary mapping goal_id to GoalPriority object
    """
    _ensure_imports()
    config = load_config()

    if assessments is None:
        # Get company directory
        company_dir = company_resolver.get_company_dir()
        # lightweight=True: G1 reads the nightly's trusted coverage.json instead
        # of running pytest --cov here. Without it this hourly refresh ran the
        # FULL suite in the primary checkout every hour, was SIGKILLed at 600 s
        # every time, and was the dominant writer of fixture records into the
        # real .company/state (found 2026-08-29). The daemon's other planning
        # paths already pass lightweight=True.
        assessments = goal_tracker.assess_all_goals(company_dir, lightweight=True)

    priorities: dict[str, GoalPriority] = {}

    # Build a status map for dependency blocking checks
    status_by_goal = {a.goal_id: a.status.value for a in assessments}

    for assessment in assessments:
        goal_id = assessment.goal_id

        # Get progress values (normalized to 0.0-1.0)
        current_progress = assessment.current_value
        target_progress = assessment.target_value

        # Compute gap
        gap_size = max(0.0, target_progress - current_progress)

        strategic_weight = _strategic_weight_for(assessment, config)

        # Get deadline from raw_data if available
        deadline = None
        raw_data = getattr(assessment, "raw_data", {}) or {}
        if raw_data.get("deadline"):
            try:
                deadline = datetime.fromisoformat(raw_data["deadline"])
            except (ValueError, TypeError):
                pass

        # Compute urgency multiplier
        urgency_multiplier = compute_urgency_multiplier(deadline, config)

        # Compute priority score
        # Higher gap + higher urgency + higher weight = higher priority
        priority_score = (gap_size * urgency_multiplier) * strategic_weight

        # Zero out priority for goals with unmet dependencies
        dep_blocked = any(
            status_by_goal.get(dep) != "complete"
            for dep in getattr(assessment, "depends_on", [])
        )
        if dep_blocked:
            priority_score = 0.0

        priorities[goal_id] = GoalPriority(
            goal_id=goal_id,
            goal_name=assessment.goal_name,
            current_progress=current_progress,
            target_progress=target_progress,
            deadline=deadline,
            strategic_weight=strategic_weight,
            priority_score=priority_score,
            gap_size=gap_size,
            urgency_multiplier=urgency_multiplier,
            status="dep_blocked" if dep_blocked else assessment.status.value,
        )

    return priorities


def get_priority_scores(
    assessments: list | None = None,
) -> dict[str, float]:
    """
    Get simplified priority scores for goals.

    Convenience function that returns just the priority scores.

    Args:
        assessments: Optional list of GoalAssessment objects

    Returns:
        Dictionary mapping goal_id to priority score
    """
    priorities = compute_goal_priorities(assessments)
    return {goal_id: p.priority_score for goal_id, p in priorities.items()}


# =============================================================================
# Task Priority Mapping
# =============================================================================


def get_goal_priority_for_task(
    task: dict[str, Any], priorities: dict[str, float]
) -> float:
    """
    Get the goal priority bonus for a task.

    Looks at the task's goal_alignment field and returns the highest
    priority among aligned goals, scaled by the bonus factor.

    Args:
        task: Task dictionary with optional goal_alignment field
        priorities: Dictionary mapping goal_id to priority score

    Returns:
        Priority bonus for this task (0.0 if no goal alignment)
    """
    config = load_config()
    priority_bonus_scale = config.get("priorityBonusScale", PRIORITY_BONUS_SCALE)
    max_bonus = config.get("maxPriorityBonus", MAX_PRIORITY_BONUS)

    # Check various places where goal alignment might be stored
    goal_alignment = task.get("goal_alignment", [])

    # Also check in notes (roadmap tasks store metadata there)
    notes = task.get("notes", "")
    if notes and isinstance(notes, str):
        try:
            notes_data = json.loads(notes)
            if "goal_alignment" in notes_data:
                goal_alignment = notes_data["goal_alignment"]
        except (json.JSONDecodeError, TypeError):
            pass

    # Also check description for goal references like "[G5]"
    description = task.get("description", "")
    if not goal_alignment and description:
        import re

        goal_refs = re.findall(r"\[?(G\d+)\]?", description)
        if goal_refs:
            goal_alignment = goal_refs

    if not goal_alignment:
        return 0.0

    # Get maximum priority among aligned goals
    max_priority = 0.0
    for goal_id in goal_alignment:
        if goal_id in priorities:
            max_priority = max(max_priority, priorities[goal_id])

    # Scale and cap the bonus
    bonus = max_priority * priority_bonus_scale
    return min(bonus, max_bonus)


def compute_effective_priority(
    task: dict[str, Any],
    goal_priorities: dict[str, float],
    signal_boosts: dict[str, float] | None = None,
) -> float:
    """
    Compute the effective priority for a task including goal and signal bonuses.

    Combines the task's base priority with goal alignment bonus and external
    signal boosts. Lower effective priority = higher importance.

    Args:
        task: Task dictionary
        goal_priorities: Dictionary mapping goal_id to priority score
        signal_boosts: Optional dict mapping task_id to signal priority boost
                       (WS-067-001: External signal integration)

    Returns:
        Effective priority (lower = more important)
    """
    # Base priority (1=Critical, 4=Low in work queue)
    base_priority = task.get("priority", 3)

    # Get goal bonus (higher = more important)
    goal_bonus = get_goal_priority_for_task(task, goal_priorities)

    # WS-067-001: Get signal boost if available (additive, capped)
    signal_bonus = 0.0
    if signal_boosts:
        task_id = task.get("task_id", "")
        if task_id and task_id in signal_boosts:
            # Signal boosts are in 0-100 scale, normalize to 0-2 priority scale
            raw_boost = signal_boosts.get(task_id, 0)
            signal_bonus = min(raw_boost / 50.0, 2.0)  # Cap at 2.0 priority boost

    # Subtract bonuses from priority (lower = more important)
    # A task with priority 3 and goal_bonus 1.5 + signal_bonus 0.5 becomes priority 1.0
    effective = base_priority - goal_bonus - signal_bonus

    return effective


# =============================================================================
# Queue Reordering
# =============================================================================


def build_dependency_graph(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    """
    Build a dependency graph from tasks.

    Args:
        tasks: List of task dictionaries

    Returns:
        Dictionary mapping task_id to set of dependency task_ids
    """
    graph: dict[str, set[str]] = {}

    for task in tasks:
        task_id = task.get("task_id", "")
        dependencies = task.get("dependencies", [])

        if task_id:
            graph[task_id] = set(dependencies) if dependencies else set()

    return graph


def topological_sort_with_priority(
    tasks: list[dict[str, Any]],
    effective_priorities: dict[str, float],
) -> list[dict[str, Any]]:
    """
    Sort tasks respecting dependencies and priorities.

    Uses a modified topological sort that considers priority within each
    dependency level. Tasks with no dependencies are sorted by priority first.

    Args:
        tasks: List of task dictionaries
        effective_priorities: Dictionary mapping task_id to effective priority

    Returns:
        Sorted list of tasks
    """
    if not tasks:
        return []

    # Build dependency graph
    dep_graph = build_dependency_graph(tasks)
    task_map = {t.get("task_id", ""): t for t in tasks if t.get("task_id")}

    # Track which tasks are in the result
    result: list[dict[str, Any]] = []
    processed: set[str] = set()

    def get_ready_tasks() -> list[str]:
        """Get tasks with all dependencies satisfied."""
        ready = []
        for task_id, deps in dep_graph.items():
            if task_id not in processed:
                # Check if all dependencies are processed
                unmet = deps - processed
                if not unmet:
                    ready.append(task_id)
        return ready

    # Process in waves by dependency level
    while len(processed) < len(dep_graph):
        ready = get_ready_tasks()

        if not ready:
            # No ready tasks but not done - circular dependency or missing tasks
            # Add remaining tasks by priority
            remaining = [tid for tid in dep_graph.keys() if tid not in processed]
            ready = remaining

        # Sort ready tasks by effective priority (lower = more important)
        ready.sort(key=lambda tid: effective_priorities.get(tid, 999))

        for task_id in ready:
            if task_id in task_map and task_id not in processed:
                result.append(task_map[task_id])
                processed.add(task_id)

    # Add any tasks that weren't in the dependency graph
    for task in tasks:
        task_id = task.get("task_id", "")
        if task_id not in processed:
            result.append(task)

    return result


def reorder_queue_by_goals(
    queue: list[dict[str, Any]],
    priorities: dict[str, float],
    signal_boosts: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Reorder work queue tasks by goal priorities and external signals.

    Maps tasks to goals via goal_alignment field, computes combined priority
    (task.priority + goal_priority_bonus + signal_boost), and sorts while
    preserving dependency ordering.

    Args:
        queue: List of task dictionaries from work queue
        priorities: Dictionary mapping goal_id to priority score
        signal_boosts: Optional dict mapping task_id to signal priority boost
                       (WS-067-001: External signal integration)

    Returns:
        Reordered list of tasks
    """
    if not queue:
        return []

    # Compute effective priorities for all tasks
    effective_priorities: dict[str, float] = {}
    for task in queue:
        task_id = task.get("task_id", "")
        if task_id:
            effective_priorities[task_id] = compute_effective_priority(
                task, priorities, signal_boosts
            )

    # Sort respecting dependencies
    return topological_sort_with_priority(queue, effective_priorities)


# =============================================================================
# Logging and Observability
# =============================================================================


def log_reordering(
    original: list[dict[str, Any]],
    reordered: list[dict[str, Any]],
    goal_priorities: dict[str, float],
) -> ReorderResult:
    """
    Log the reordering operation for observability.

    Creates a ReorderResult with before/after task order and logs to file.

    Args:
        original: Original task list
        reordered: Reordered task list
        goal_priorities: Goal priorities used for reordering

    Returns:
        ReorderResult with operation details
    """
    _ensure_imports()
    config = load_config()

    original_ids = [t.get("task_id", "") for t in original]
    new_ids = [t.get("task_id", "") for t in reordered]

    # Count how many tasks changed position
    tasks_moved = sum(
        1 for i, (orig, new) in enumerate(zip(original_ids, new_ids)) if orig != new
    )

    # Determine reorder reason
    if tasks_moved == 0:
        reason = "No reordering needed - order unchanged"
    elif tasks_moved == len(original):
        reason = "Full reorder - all tasks repositioned"
    else:
        reason = f"Partial reorder - {tasks_moved}/{len(original)} tasks moved"

    result = ReorderResult(
        success=True,
        tasks_reordered=tasks_moved,
        reorder_reason=reason,
        original_order=original_ids,
        new_order=new_ids,
        goal_priorities=goal_priorities,
    )

    # Log to file if enabled
    if config.get("logReordering", True):
        company_dir = company_resolver.get_company_dir()
        log_dir = company_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "goal_scheduler.log"
        with open(log_file, "a", encoding="utf-8") as f:
            log_entry = {
                "timestamp": result.timestamp,
                "tasks_reordered": tasks_moved,
                "reason": reason,
                "goal_priorities": goal_priorities,
            }
            f.write(json.dumps(log_entry) + "\n")

    logger.info(f"Queue reorder: {reason}")
    return result


# =============================================================================
# Work Queue Integration
# =============================================================================


def load_work_queue() -> dict[str, Any]:
    """Load the work queue from file."""
    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    queue_path = company_dir / "state/work_queue.json"

    if not queue_path.exists():
        return {
            "proposed": [],
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
            "proposed": [],
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
        }


def save_work_queue(queue: dict[str, Any]) -> None:
    """Save the work queue to file with atomic write."""
    import os
    import tempfile

    # P80: Prevent pytest from overwriting production queue.
    _real_company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    _is_pytest = "PYTEST_CURRENT_TEST" in os.environ or any(
        "pytest" in str(v) for v in [os.environ.get("_", "")]
    )
    if _is_pytest:
        _ensure_imports()
        try:
            _candidate = company_resolver.get_company_dir() / "state/work_queue.json"
            if _candidate.resolve().is_relative_to(_real_company.resolve()):
                return  # Silently skip — never overwrite production queue from tests
        except (ValueError, OSError):
            pass

    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    queue_path = company_dir / "state/work_queue.json"

    # Atomic write to prevent corruption
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="work_queue_",
        dir=str(company_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp_path, queue_path)
    except Exception:
        # Clean up temp file on error
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def reorder_pending_queue(
    dry_run: bool = False,
    signal_boosts: dict[str, float] | None = None,
) -> ReorderResult:
    """
    Reorder the pending queue by goal priorities and external signals.

    Main entry point for daemon integration. Loads queue, computes
    goal priorities, applies signal boosts from external integrations
    (WS-067), reorders pending tasks, and saves back.

    Uses QueueLock only around the actual queue read-modify-write; goal
    priority computation runs unlocked (see note below).

    Args:
        dry_run: If True, don't save changes (preview mode)
        signal_boosts: Optional dict mapping task_id to priority boost
            from external signals (WS-067-001: Stripe, Sentry, GitHub webhooks)

    Returns:
        ReorderResult with operation details
    """
    config = load_config()

    if not config.get("enabled", True):
        return ReorderResult(
            success=False,
            tasks_reordered=0,
            reorder_reason="Goal scheduling disabled in config",
            original_order=[],
            new_order=[],
            goal_priorities={},
        )

    # Import QueueLock from work_allocator
    _ensure_imports()
    lock_path = company_resolver.get_company_dir() / "runtime/queue.lock"

    # Skip the (possibly expensive) priority computation entirely if there's
    # nothing to reorder. Short-lived lock: read-only peek at "pending".
    with work_allocator.QueueLock(lock_path):
        if not load_work_queue().get("pending", []):
            return ReorderResult(
                success=True,
                tasks_reordered=0,
                reorder_reason="No pending tasks to reorder",
                original_order=[],
                new_order=[],
                goal_priorities={},
            )

    # Compute goal priorities OUTSIDE the lock. On a cache miss this runs
    # the full pytest suite for G1 test-coverage assessment (up to 600s —
    # see goal_tracker.assess_test_coverage). QueueLock is a single
    # cross-process fcntl flock shared by every queue writer (add_task,
    # claim_task, release_task, ...); holding it for up to 10 minutes here
    # starved external tooling and the daemon's own parallel workers.
    priorities = get_priority_scores()

    # Re-acquire the lock for the actual read-modify-write. The queue may
    # have changed while priorities were being computed above, so re-read
    # it fresh rather than reusing the peek from the first lock.
    with work_allocator.QueueLock(lock_path):
        queue = load_work_queue()
        pending = queue.get("pending", [])

        if not pending:
            return ReorderResult(
                success=True,
                tasks_reordered=0,
                reorder_reason="No pending tasks to reorder",
                original_order=[],
                new_order=[],
                goal_priorities={},
            )

        # Reorder (WS-067-001: include signal boosts from external integrations)
        reordered = reorder_queue_by_goals(pending, priorities, signal_boosts)

        # Log the operation
        result = log_reordering(pending, reordered, priorities)

        # Save if not dry run
        if not dry_run and result.tasks_reordered > 0:
            queue["pending"] = reordered
            save_work_queue(queue)
            logger.info(f"Saved reordered queue with {result.tasks_reordered} changes")

        return result


# =============================================================================
# CLI Interface
# =============================================================================


def print_help():
    """Print usage help."""
    help_text = """
Goal Scheduler — Priority-based task ordering driven by strategic goals

Commands:
    priorities      Show computed goal priorities
    reorder         Reorder pending queue by goal priorities
    preview         Preview reorder without applying changes
    task-priority   Get priority info for a specific task

Options:
    --task-id ID    Task ID for task-priority command
    --json          Output as JSON

Examples:
    # Show goal priorities
    python goal_scheduler.py priorities

    # Reorder queue
    python goal_scheduler.py reorder

    # Preview reorder
    python goal_scheduler.py preview

    # Get priority for specific task
    python goal_scheduler.py task-priority --task-id "task-123"
"""
    print(help_text)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    json_output = "--json" in sys.argv

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    if command == "priorities":
        priorities = compute_goal_priorities()
        if json_output:
            print(
                json.dumps(
                    {gid: p.to_dict() for gid, p in priorities.items()}, indent=2
                )
            )
        else:
            print("Goal Priorities:\n")
            # Sort by priority score descending
            sorted_priorities = sorted(
                priorities.items(),
                key=lambda x: x[1].priority_score,
                reverse=True,
            )
            for goal_id, p in sorted_priorities:
                print(f"  {goal_id}: {p.goal_name}")
                print(
                    f"    Progress: {p.current_progress * 100:.0f}% / {p.target_progress * 100:.0f}%"
                )
                print(f"    Gap: {p.gap_size * 100:.0f}%")
                print(f"    Weight: {p.strategic_weight:.1f}")
                print(f"    Urgency: {p.urgency_multiplier:.1f}x")
                print(f"    Priority Score: {p.priority_score:.2f}")
                print(f"    Status: {p.status}")
                print()

    elif command == "reorder":
        result = reorder_pending_queue(dry_run=False)
        if json_output:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"Reorder Result: {result.reorder_reason}")
            print(f"Tasks reordered: {result.tasks_reordered}")
            if result.tasks_reordered > 0:
                print("\nNew order:")
                for i, task_id in enumerate(result.new_order[:10], 1):
                    print(f"  {i}. {task_id}")
                if len(result.new_order) > 10:
                    print(f"  ... and {len(result.new_order) - 10} more")

    elif command == "preview":
        result = reorder_pending_queue(dry_run=True)
        if json_output:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("Preview (no changes applied):")
            print(f"  {result.reorder_reason}")
            print(f"  Would reorder {result.tasks_reordered} tasks")
            if result.tasks_reordered > 0:
                print("\nCurrent -> New order:")
                for i, (orig, new) in enumerate(
                    zip(result.original_order[:10], result.new_order[:10]), 1
                ):
                    marker = " *" if orig != new else ""
                    print(f"  {i}. {orig} -> {new}{marker}")

    elif command == "task-priority":
        task_id = None
        for i, arg in enumerate(sys.argv):
            if arg == "--task-id" and i + 1 < len(sys.argv):
                task_id = sys.argv[i + 1]
                break

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        # Load queue and find task
        queue = load_work_queue()
        task = None
        for status in ["pending", "in_progress", "proposed", "blocked"]:
            for t in queue.get(status, []):
                if t.get("task_id") == task_id:
                    task = t
                    break
            if task:
                break

        if not task:
            print(f"Error: Task {task_id} not found in queue")
            sys.exit(1)

        priorities = get_priority_scores()
        bonus = get_goal_priority_for_task(task, priorities)
        effective = compute_effective_priority(task, priorities)

        if json_output:
            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "base_priority": task.get("priority", 3),
                        "goal_bonus": bonus,
                        "effective_priority": effective,
                        "goal_alignment": task.get("goal_alignment", []),
                    },
                    indent=2,
                )
            )
        else:
            print(f"Task: {task_id}")
            print(f"  Title: {task.get('title', 'N/A')}")
            print(f"  Base Priority: {task.get('priority', 3)}")
            print(f"  Goal Alignment: {task.get('goal_alignment', [])}")
            print(f"  Goal Bonus: {bonus:.2f}")
            print(f"  Effective Priority: {effective:.2f}")

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
