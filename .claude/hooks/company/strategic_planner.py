#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Strategic Planner — generates initiatives from vision gaps.

P15 implementation: Reads goals, identifies gaps, generates initiatives,
and queues work for autonomous execution.

Usage:
    # Identify gaps between vision and current state
    python strategic_planner.py gaps

    # Generate initiatives to close gaps
    python strategic_planner.py propose

    # Run full planning cycle
    python strategic_planner.py plan

    # Show active initiatives
    python strategic_planner.py active
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Goal autofill lives in goal_autofill (core); re-exported for existing callers.
try:
    from .goal_autofill import (  # noqa: F401
        _incomplete_dependencies,
        _should_defer_fill_to_ideation,
        autofill_queue_from_goals,
    )
except ImportError:
    from goal_autofill import (  # type: ignore[no-redef]  # noqa: F401
        _incomplete_dependencies,
        _should_defer_fill_to_ideation,
        autofill_queue_from_goals,
    )

# Lazy imports for sibling modules
goal_tracker = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global goal_tracker
    if goal_tracker is not None:
        return

    try:
        from . import goal_tracker as gt

        goal_tracker = gt
    except ImportError:
        import goal_tracker as gt  # type: ignore[no-redef]

        goal_tracker = gt


# -----------------------------------------------------------------------------
# WS-108: Context Preservation Helpers
# -----------------------------------------------------------------------------


def _get_company_dir_for_docs() -> Path:
    """Get company directory for creating reference docs."""
    try:
        try:
            from . import company_resolver
        except ImportError:
            import company_resolver  # type: ignore[no-redef]
        return company_resolver.get_company_dir()
    except Exception:
        return Path(".company")


def _create_gap_reference_doc(gap: dict, company_dir: Path | None = None) -> str | None:
    """
    Create a reference document for a gap analysis with full context.

    Returns the path to the created document, or None if creation fails.
    """
    import os
    import tempfile

    if company_dir is None:
        company_dir = _get_company_dir_for_docs()

    gap_id = gap.get("goal_id", "unknown")
    gaps_dir = company_dir / "gaps"
    gaps_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    doc_path = gaps_dir / f"gap-{gap_id}-{timestamp}.md"

    # Build suggested actions section
    actions = gap.get("suggested_actions", [])
    actions_section = "\n".join(f"- {a}" for a in actions) if actions else "None"

    # Build evidence section (progress data, metrics)
    progress = gap.get("progress", 0)
    target = gap.get("target", 100)
    gap_size = target - progress

    doc_content = f"""# Gap Analysis: {gap.get("goal_name", "Unknown Goal")}

**Goal ID:** {gap_id}
**Analyzed:** {datetime.now(timezone.utc).isoformat()}
**Status:** {gap.get("status", "unknown")}

## Gap Summary

**Current Progress:** {progress}%
**Target:** {target}%
**Gap Size:** {gap_size}%
**Urgency:** {gap.get("urgency", "N/A")}/5

## Description

{gap.get("description", "No description provided.")}

## Impact Assessment

{gap.get("impact", "No impact assessment provided.")}

## Suggested Actions

{actions_section}

## Context

This gap was identified during strategic planning cycle. The goal is currently
at {progress}% progress toward the {target}% target, leaving a {gap_size}%
gap to close.

**Urgency Level:** {gap.get("urgency", 0)} — {"CRITICAL" if gap.get("urgency", 0) >= 4 else "IMPORTANT" if gap.get("urgency", 0) >= 3 else "MONITOR"}

---
*Auto-generated gap analysis document. Reference for task implementation context.*
"""

    try:
        fd, tmp_path = tempfile.mkstemp(dir=gaps_dir, suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(doc_content)
        os.replace(tmp_path, doc_path)
        return str(doc_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


def _create_initiative_reference_doc(
    initiative: "Initiative", company_dir: Path | None = None
) -> str | None:
    """
    Create a reference document for an initiative with full context.

    Returns the path to the created document, or None if creation fails.
    """
    import os
    import tempfile

    if company_dir is None:
        company_dir = _get_company_dir_for_docs()

    initiatives_dir = company_dir / "initiatives"
    initiatives_dir.mkdir(exist_ok=True)

    doc_path = initiatives_dir / f"{initiative.id}.md"

    # Build goal alignment section
    goals = initiative.goal_alignment or []
    goals_section = "\n".join(f"- {g}" for g in goals) if goals else "None specified"

    # Build tasks section with full details
    tasks_lines = []
    for i, task in enumerate(initiative.tasks, 1):
        task_desc = f"""### Task {i}: {task.name}

**ID:** {task.id}
**Action:** {task.action}
**Effort:** {task.estimated_effort}
**Status:** {task.status}
**File:** {task.file or "N/A"}

**Description:**
{task.description}

**Acceptance Criteria:**
{task.acceptance or "Not specified"}
"""
        tasks_lines.append(task_desc)

    tasks_section = "\n".join(tasks_lines) if tasks_lines else "No tasks defined"

    doc_content = f"""# Initiative: {initiative.title}

**ID:** {initiative.id}
**Status:** {initiative.status.value if hasattr(initiative.status, "value") else initiative.status}
**Owner:** {initiative.owner}
**Priority:** {initiative.priority}/100
**Size:** {initiative.size.value if hasattr(initiative.size, "value") else initiative.size}
**Approval:** {initiative.approval_required.value if hasattr(initiative.approval_required, "value") else initiative.approval_required}

**Created:** {initiative.created_at}
**Approved:** {initiative.approved_at or "Not yet approved"}

## Description

{initiative.description}

## Goal Alignment

This initiative supports the following strategic goals:

{goals_section}

## Tasks

{tasks_section}

---
*Auto-generated initiative document. Reference for task implementation context.*
"""

    try:
        fd, tmp_path = tempfile.mkstemp(dir=initiatives_dir, suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(doc_content)
        os.replace(tmp_path, doc_path)
        return str(doc_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


class GoalLevel(str, Enum):
    """Goal hierarchy levels."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    WEEKLY = "weekly"
    DAILY = "daily"


class InitiativeSize(str, Enum):
    """Initiative size/effort classification."""

    SMALL = "small"  # 1-3 tasks, <1 day
    MEDIUM = "medium"  # 4-10 tasks, 1-3 days
    LARGE = "large"  # 10+ tasks, >3 days
    EPIC = "epic"  # Strategic initiative, multi-week


class ApprovalType(str, Enum):
    """Approval requirements for initiatives."""

    AUTO = "auto"  # Auto-approve and queue
    HUMAN = "human"  # Requires human review via /pending
    EXECUTIVE = "executive"  # Requires CEO/CTO approval


class InitiativeStatus(str, Enum):
    """Initiative lifecycle status."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass
class StrategicGoal:
    """A goal within the goal hierarchy."""

    id: str
    name: str
    parent_id: str | None  # None for top-level goals
    level: GoalLevel
    target_metric: float  # Target value to achieve
    current_metric: float = 0.0  # Current progress toward target
    status: str = "active"  # "active" or "paused"
    paused_reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StrategicGoal":
        d = d.copy()
        d["level"] = GoalLevel(d["level"])
        # Strip unknown fields that aren't in the dataclass
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)


@dataclass
class StrategicGap:
    """A gap between current state and goal target."""

    goal_id: str
    goal_name: str
    description: str
    current_progress: int  # 0-100
    target_progress: int  # Usually 100
    gap_size: int  # target - current
    urgency: int  # 1-5 (5 = critical)
    impact: str  # Description of impact if not addressed
    suggested_actions: list[str] = field(default_factory=list)
    # WS-113-003: Velocity tracking for priority boosting
    velocity: float = 0.0  # percent per day
    velocity_trend: str = "unknown"  # "improving", "stalled", "regressing"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InitiativeTask:
    """A task within an initiative."""

    id: str
    name: str
    description: str
    file: str | None = None
    action: str = "implement"  # implement, create, modify, test
    acceptance: str = ""
    estimated_effort: str = "small"  # small, medium, large
    status: str = "pending"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Initiative:
    """A strategic initiative to close a gap."""

    id: str
    title: str
    description: str
    goal_alignment: list[str]  # Goal IDs this supports
    size: InitiativeSize
    approval_required: ApprovalType
    status: InitiativeStatus
    owner: str
    tasks: list[InitiativeTask] = field(default_factory=list)
    priority: int = 50  # 0-100, higher = more important
    created_at: str = ""
    approved_at: str | None = None
    completed_at: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size"] = self.size.value
        d["approval_required"] = self.approval_required.value
        d["status"] = self.status.value
        d["tasks"] = [t.to_dict() if hasattr(t, "to_dict") else t for t in self.tasks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Initiative":
        d["size"] = InitiativeSize(d["size"])
        d["approval_required"] = ApprovalType(d["approval_required"])
        d["status"] = InitiativeStatus(d["status"])
        d["tasks"] = [
            InitiativeTask(**t) if isinstance(t, dict) else t
            for t in d.get("tasks", [])
        ]
        return cls(**d)


class GoalHierarchy:
    """Manages hierarchical goal decomposition."""

    def __init__(self, goals: list[StrategicGoal] | None = None):
        self.goals: list[StrategicGoal] = goals or []

    def decompose_goal(
        self,
        parent_goal: StrategicGoal,
        child_count: int = 4,
        child_level: GoalLevel | None = None,
    ) -> list[StrategicGoal]:
        """
        Decompose a parent goal into child goals at the next level.

        Args:
            parent_goal: The goal to decompose
            child_count: Number of child goals to create
            child_level: Override the child level (defaults to next level down)

        Returns:
            List of created child goals
        """
        # Determine child level if not specified
        if child_level is None:
            level_order = [
                GoalLevel.ANNUAL,
                GoalLevel.QUARTERLY,
                GoalLevel.WEEKLY,
                GoalLevel.DAILY,
            ]
            try:
                parent_idx = level_order.index(parent_goal.level)
                if parent_idx >= len(level_order) - 1:
                    # Cannot decompose daily goals further
                    return []
                child_level = level_order[parent_idx + 1]
            except ValueError:
                return []

        # Create child goals that sum to parent target
        per_child_target = parent_goal.target_metric / child_count
        children = []

        for i in range(child_count):
            child = StrategicGoal(
                id=f"{parent_goal.id}-{child_level.value[0]}{i + 1}",
                name=f"{parent_goal.name} ({child_level.value} {i + 1})",
                parent_id=parent_goal.id,
                level=child_level,
                target_metric=per_child_target,
                current_metric=0.0,
            )
            children.append(child)
            self.goals.append(child)

        return children

    def get_active_goals_for_level(self, level: GoalLevel) -> list[StrategicGoal]:
        """
        Get all active (incomplete) goals at a specific level.

        Args:
            level: The goal level to filter by

        Returns:
            List of goals at that level that are not yet complete
        """
        return [
            goal
            for goal in self.goals
            if goal.level == level
            and goal.current_metric < goal.target_metric
            and getattr(goal, "status", "active") != "paused"
        ]


# -----------------------------------------------------------------------------
# Planning Cycles
# -----------------------------------------------------------------------------


class WeeklyPlanningCycle:
    """Runs weekly strategic planning to create initiatives from gaps."""

    def __init__(self, company_dir: Path):
        self.company_dir = company_dir
        self.state: StrategicState | None = None

    def run(self, *, lightweight: bool = False) -> dict[str, Any]:
        """
        Run the weekly planning cycle.

        Identifies gaps between current state and goals, then generates
        3-5 initiatives targeting the highest-priority gaps.

        Args:
            lightweight: If True, skip expensive assessments (pytest).
                Used by daemon to keep cycles under 1 second.

        Returns:
            Dict with cycle results including initiatives created
        """
        _ensure_imports()

        now = datetime.now(timezone.utc).isoformat()
        self.state = load_state(self.company_dir)

        # Assess goals and identify gaps
        assessments = goal_tracker.assess_all_goals(
            self.company_dir, lightweight=lightweight
        )

        # P40: Check for period transition (all goals complete → advance period)
        transition = goal_tracker.check_period_transition(self.company_dir)
        if transition:
            # Period advanced — re-assess with new active period goals
            assessments = goal_tracker.assess_all_goals(
                self.company_dir, lightweight=lightweight
            )

        # WS-119: Augment with synthetic assessments from goal_hierarchy
        assessments = augment_assessments_from_hierarchy(
            assessments, self.state.goal_hierarchy
        )

        gaps = identify_gaps(assessments)

        # Save goal snapshot
        self.state.goal_snapshots.append(
            {"timestamp": now, "assessments": [a.to_dict() for a in assessments]}
        )
        self.state.goal_snapshots = self.state.goal_snapshots[-10:]

        # Generate initiatives for week (3-5 targeting highest priority gaps)
        initiatives = self._generate_initiatives_for_week(gaps)

        # Add to active initiatives
        for initiative in initiatives:
            self.state.active_initiatives.append(initiative)

        # Update timestamps
        self.state.last_assessment = now
        self.state.last_planning_run = now

        # Calculate next weekly run

        next_run = datetime.now(timezone.utc) + timedelta(days=7)
        self.state.next_planning_run = next_run.isoformat()

        # Auto-approve small initiatives
        auto_approved = []
        for initiative in self.state.active_initiatives:
            if (
                initiative.status == InitiativeStatus.PROPOSED
                and initiative.approval_required == ApprovalType.AUTO
            ):
                initiative.status = InitiativeStatus.APPROVED
                initiative.approved_at = now
                auto_approved.append(initiative)

        # Queue auto-approved tasks
        queued_count = 0
        if auto_approved:
            queued_count = queue_initiative_tasks(self.company_dir, auto_approved)

        # Save state
        save_state(self.company_dir, self.state)

        return {
            "cycle_type": "weekly",
            "timestamp": now,
            "goals_assessed": len(assessments),
            "gaps_identified": len(gaps),
            "initiatives_created": len(initiatives),
            "initiatives_auto_approved": len(auto_approved),
            "tasks_queued": queued_count,
            "active_initiatives": len(self.state.active_initiatives),
            "next_weekly_run": self.state.next_planning_run,
            "period_transition": transition,
        }

    def _generate_initiatives_for_week(
        self, gaps: list[StrategicGap]
    ) -> list[Initiative]:
        """Generate 3-5 initiatives targeting highest-priority gaps."""
        # Get existing IDs to avoid conflicts
        existing_ids = {i.id for i in self.state.active_initiatives}
        existing_ids.update(i.id for i in self.state.completed_initiatives)

        # Track goals that already have ANY initiative (active or recently
        # completed) to prevent duplicate creation.  Previously only non-
        # completed initiatives were counted, so the planner kept creating
        # new initiatives for goals whose initiative was marked "completed"
        # while the underlying metric remained unchanged.
        covered_goals: set[str] = set()
        for initiative in self.state.active_initiatives:
            if initiative.status != InitiativeStatus.REJECTED:
                for goal_id in initiative.goal_alignment:
                    covered_goals.add(goal_id)

        # Filter out gaps that already have initiatives
        uncovered_gaps = [gap for gap in gaps if gap.goal_id not in covered_goals]

        # Calculate how many new initiatives we can create (3-5 active at a time)
        # Count all non-rejected initiatives to prevent unbounded growth.
        active_count = len(
            [
                i
                for i in self.state.active_initiatives
                if i.status != InitiativeStatus.REJECTED
            ]
        )
        min_new = max(0, 3 - active_count)
        max_new = max(0, 5 - active_count)

        # Generate initiatives for top gaps
        initiatives = []
        for gap in uncovered_gaps:
            if len(initiatives) >= max_new:
                break
            initiative = generate_initiative_for_gap(gap, existing_ids)
            if initiative and impact_score(initiative) >= 0.3:
                initiatives.append(initiative)
                existing_ids.add(initiative.id)

        # Ensure we have at least min_new if gaps exist
        return initiatives[: max(min_new, len(initiatives))]


class DailyPlanningCycle:
    """Runs daily planning to break initiatives into daily tasks."""

    def __init__(
        self,
        company_dir: Path,
        *,
        last_ideation: str = "",
        defer_to_ideation: bool = False,
    ):
        self.company_dir = company_dir
        self.state: StrategicState | None = None
        self._last_ideation = last_ideation
        self._defer_to_ideation = defer_to_ideation

    def run(self) -> dict[str, Any]:
        """
        Run the daily planning cycle.

        WS-106-004: Enhanced with daily gap analysis:
        1. Updates initiative progress based on completed tasks
        2. Runs goal assessment and identifies gaps
        3. Auto-generates small tasks for critical gaps
        4. Detects goal regressions from previous snapshots

        Returns:
            Dict with cycle results including tasks created and gaps found
        """
        _ensure_imports()

        now = datetime.now(timezone.utc).isoformat()
        self.state = load_state(self.company_dir)

        # Update initiative progress based on completed tasks
        progress_updates = self._update_initiative_progress()

        # Break approved initiatives into daily tasks
        daily_tasks_created = self._create_daily_tasks()

        # WS-106-004: Run daily gap analysis
        gap_analysis = self._run_gap_analysis()

        # WS-106-004: Detect goal regressions
        regressions = self._detect_regressions()

        # WS-106-004: Auto-generate tasks for critical gaps (urgency >= 4)
        auto_tasks_created = self._auto_generate_gap_tasks(gap_analysis.get("gaps", []))

        # WS-113-006: Auto-generate initiatives for stalled/regressing goals
        auto_initiatives = self._auto_generate_stalled_initiatives(
            gap_analysis.get("gaps", [])
        )

        # Save state (includes updated goal snapshots)
        save_state(self.company_dir, self.state)

        return {
            "cycle_type": "daily",
            "timestamp": now,
            "initiatives_updated": len(progress_updates),
            "daily_tasks_created": daily_tasks_created,
            "active_initiatives": len(self.state.active_initiatives),
            "progress_updates": progress_updates,
            # WS-106-004: Gap analysis results
            "gap_analysis": gap_analysis,
            "regressions": regressions,
            "auto_tasks_created": auto_tasks_created,
            # WS-113-006: Auto-generated initiatives for stalled goals
            "auto_initiatives_created": auto_initiatives.get("created", 0),
            "auto_initiatives_details": auto_initiatives.get("details", []),
        }

    def _update_initiative_progress(self) -> list[dict]:
        """Update initiative progress based on task completion status."""
        updates = []

        # Load work queue to check task completion
        queue_file = self.company_dir / "state/work_queue.json"
        if not queue_file.exists():
            return updates

        try:
            with open(queue_file) as f:
                queue = json.load(f)
        except Exception:
            return updates

        completed_tasks = queue.get("completed", [])

        for initiative in self.state.active_initiatives:
            if initiative.status not in (
                InitiativeStatus.APPROVED,
                InitiativeStatus.IN_PROGRESS,
            ):
                continue

            # Count completed tasks for this initiative
            total_tasks = len(initiative.tasks)
            if total_tasks == 0:
                continue

            completed_count = sum(
                1 for t in initiative.tasks if t.status in ("completed", "done")
            )

            # Check work queue for completed tasks
            for task in initiative.tasks:
                if task.status == "queued":
                    # Check if task completed in queue
                    for ct in completed_tasks:
                        if ct.get(
                            "initiative_id"
                        ) == initiative.id and task.name in ct.get("title", ""):
                            task.status = "completed"
                            completed_count += 1
                            break

            # Update initiative status based on progress
            old_status = initiative.status
            if completed_count > 0 and initiative.status == InitiativeStatus.APPROVED:
                initiative.status = InitiativeStatus.IN_PROGRESS

            if completed_count == total_tasks:
                initiative.status = InitiativeStatus.COMPLETED
                initiative.completed_at = datetime.now(timezone.utc).isoformat()

            if old_status != initiative.status:
                updates.append(
                    {
                        "initiative_id": initiative.id,
                        "old_status": old_status.value,
                        "new_status": initiative.status.value,
                        "completed_tasks": completed_count,
                        "total_tasks": total_tasks,
                    }
                )

        # Move completed initiatives to completed_initiatives to free capacity
        newly_completed = [
            i
            for i in self.state.active_initiatives
            if i.status == InitiativeStatus.COMPLETED
        ]
        if newly_completed:
            self.state.completed_initiatives.extend(newly_completed)
            self.state.active_initiatives = [
                i
                for i in self.state.active_initiatives
                if i.status != InitiativeStatus.COMPLETED
            ]

        return updates

    def _create_daily_tasks(self) -> int:
        """Create daily tasks from approved initiatives."""
        # Get in-progress initiatives with pending tasks
        initiatives_to_queue = []

        for initiative in self.state.active_initiatives:
            if initiative.status not in (
                InitiativeStatus.APPROVED,
                InitiativeStatus.IN_PROGRESS,
            ):
                continue

            # Find pending tasks that haven't been queued
            pending_tasks = [t for t in initiative.tasks if t.status == "pending"]
            if pending_tasks:
                # Limit to 2-3 tasks per day per initiative
                initiative_copy = Initiative(
                    id=initiative.id,
                    title=initiative.title,
                    description=initiative.description,
                    goal_alignment=initiative.goal_alignment,
                    size=initiative.size,
                    approval_required=initiative.approval_required,
                    status=initiative.status,
                    owner=initiative.owner,
                    tasks=pending_tasks[:3],
                    priority=initiative.priority,
                    created_at=initiative.created_at,
                    approved_at=initiative.approved_at,
                )
                initiatives_to_queue.append(initiative_copy)

        if not initiatives_to_queue:
            return 0

        return queue_initiative_tasks(self.company_dir, initiatives_to_queue)

    def _run_gap_analysis(self) -> dict[str, Any]:
        """WS-106-004: Run daily gap analysis to identify goal shortfalls.

        Returns:
            Dict with gaps found and their urgency levels.
        """
        _ensure_imports()

        try:
            # Run goal assessment
            assessments = goal_tracker.assess_all_goals()
            if not assessments:
                return {"gaps": [], "total_gap_size": 0, "critical_count": 0}

            # Identify gaps from assessments
            gaps = identify_gaps(assessments)

            # Store current snapshot for regression detection
            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "assessments": [
                    {
                        "goal_id": a.goal_id,
                        "progress": a.progress_percent,
                        "status": a.status.value,
                    }
                    for a in assessments
                ],
            }
            self.state.goal_snapshots.append(snapshot)

            # Keep only last 30 snapshots (30 days of history)
            if len(self.state.goal_snapshots) > 30:
                self.state.goal_snapshots = self.state.goal_snapshots[-30:]

            total_gap = sum(g.gap_size for g in gaps)
            critical = sum(1 for g in gaps if g.urgency >= 4)

            return {
                "gaps": [g.to_dict() for g in gaps],
                "total_gap_size": total_gap,
                "critical_count": critical,
                "goals_assessed": len(assessments),
            }
        except Exception as e:
            return {
                "error": str(e),
                "gaps": [],
                "total_gap_size": 0,
                "critical_count": 0,
            }

    def _detect_regressions(self) -> list[dict]:
        """WS-106-004: Detect goal regressions from previous snapshots.

        Compares current goal progress against previous snapshot to identify
        goals that have regressed (progress decreased).

        Returns:
            List of regression alerts with goal_id, previous/current progress.
        """
        if len(self.state.goal_snapshots) < 2:
            return []

        current = self.state.goal_snapshots[-1]
        previous = self.state.goal_snapshots[-2]

        regressions = []

        current_by_id = {a["goal_id"]: a for a in current.get("assessments", [])}
        previous_by_id = {a["goal_id"]: a for a in previous.get("assessments", [])}

        for goal_id, curr in current_by_id.items():
            prev = previous_by_id.get(goal_id)
            if prev and curr["progress"] < prev["progress"]:
                delta = prev["progress"] - curr["progress"]
                regressions.append(
                    {
                        "goal_id": goal_id,
                        "previous_progress": prev["progress"],
                        "current_progress": curr["progress"],
                        "regression_delta": delta,
                        "severity": "critical" if delta >= 20 else "warning",
                    }
                )

        return regressions

    def _auto_generate_gap_tasks(self, gaps: list[dict]) -> int:
        """WS-106-004: Auto-generate tasks for critical gaps without human approval.

        For urgency >= 4 (critical) gaps with suggested actions, creates work queue
        tasks directly. These are small, tactical fixes that don't need initiative
        overhead.

        Args:
            gaps: List of gap dicts from _run_gap_analysis

        Returns:
            Number of tasks created.
        """
        _ensure_imports()

        if not gaps:
            return 0

        # E7-ext: Ideation-first ordering — defer until direction pipeline has had a turn.
        if _should_defer_fill_to_ideation(self._last_ideation, self._defer_to_ideation):
            return 0

        # Only process critical gaps (urgency >= 4)
        critical_gaps = [g for g in gaps if g.get("urgency", 0) >= 4]
        if not critical_gaps:
            return 0

        # Load work allocator for task creation
        try:
            try:
                from . import work_allocator
            except ImportError:
                import work_allocator  # type: ignore[no-redef]
        except ImportError:
            return 0

        tasks_created = 0

        for gap in critical_gaps:
            actions = gap.get("suggested_actions", [])
            if not actions:
                continue

            # Create a task for the first suggested action
            action = actions[0]

            # WS-108: Create gap reference document with full context
            ref_doc_path = _create_gap_reference_doc(gap, self.company_dir)

            # WS-108: Build rich description with evidence and context
            progress = gap.get("progress", 0)
            target = gap.get("target", 100)
            gap_size = target - progress
            urgency = gap.get("urgency", 0)

            description_parts = [
                f"Auto-generated task to close gap for goal {gap['goal_id']}.",
                "",
                f"**Gap Summary:** {gap.get('description', 'No description')}",
                "",
                f"**Progress:** {progress}% / {target}% (gap: {gap_size}%)",
                f"**Urgency:** {urgency}/5 — {'CRITICAL' if urgency >= 4 else 'IMPORTANT'}",
                "",
                f"**Action Required:** {action}",
                "",
                f"**Impact:** {gap.get('impact', 'Unknown')}",
            ]

            # Include all suggested actions
            if len(actions) > 1:
                description_parts.append("")
                description_parts.append("**All Suggested Actions:**")
                for i, act in enumerate(actions, 1):
                    description_parts.append(f"{i}. {act}")

            # Reference the full gap document
            if ref_doc_path:
                description_parts.append("")
                description_parts.append(f"**Full Context:** See `{ref_doc_path}`")

            # WS-108: Use proper keyword arguments (not dict)
            # Context is in description and reference doc

            # WS-113-003: Velocity-based priority boost
            # Regressing or stalled goals get highest priority (P1)
            # Improving goals get lower priority (P2)
            velocity = gap.get("velocity", 0)
            velocity_trend = gap.get("velocity_trend", "unknown")

            if velocity_trend == "regressing" or velocity < -1.0:
                task_priority = 1  # CRITICAL - goal is getting worse
                priority_reason = f"regressing ({velocity:+.1f}%/day)"
            elif velocity_trend == "stalled" or abs(velocity) < 2.0:
                task_priority = 1  # CRITICAL - no progress
                priority_reason = f"stalled ({velocity:+.1f}%/day)"
            else:
                task_priority = 2  # HIGH but not critical
                priority_reason = f"improving ({velocity:+.1f}%/day)"

            # Add velocity info to description
            description_parts.append("")
            description_parts.append(
                f"**Velocity:** {velocity:+.1f}%/day ({velocity_trend}) — Priority {priority_reason}"
            )

            try:
                result = work_allocator.add_task(
                    title=f"[GAP-FIX] {gap['goal_name']}: {action[:60]}",
                    description="\n".join(description_parts),
                    priority=task_priority,
                    source="gap_analysis",
                    estimated_complexity="standard",
                )
                if result.get("success"):
                    tasks_created += 1
            except Exception:
                pass

        return tasks_created

    def _auto_generate_stalled_initiatives(self, gaps: list[dict]) -> dict[str, Any]:
        """WS-113-006: Auto-generate initiatives for stalled/regressing goals.

        For goals that have been stalled (zero velocity) or regressing (negative
        velocity) for multiple days, automatically generate an initiative to
        address the situation.

        Unlike _auto_generate_gap_tasks which creates small tactical fixes,
        this creates coordinated initiatives with multiple tasks.

        Args:
            gaps: List of gap dicts from _run_gap_analysis

        Returns:
            Dict with created count and details
        """
        result = {
            "created": 0,
            "details": [],
        }

        if not gaps or not self.state:
            return result

        # Check which goals already have active initiatives
        active_goal_ids = {
            i.goal_alignment[0] if i.goal_alignment else None
            for i in self.state.active_initiatives
            if i.status not in (InitiativeStatus.COMPLETED, InitiativeStatus.REJECTED)
        }

        existing_ids = {i.id for i in self.state.active_initiatives}

        for gap in gaps:
            goal_id = gap.get("goal_id", "")
            velocity = gap.get("velocity", 0)
            velocity_trend = gap.get("velocity_trend", "unknown")
            urgency = gap.get("urgency", 0)

            # Skip if goal already has an active initiative
            if goal_id in active_goal_ids:
                continue

            # Only generate for regressing or stalled goals with urgency >= 3
            is_stalled = velocity_trend == "stalled" or abs(velocity) < 1.0
            is_regressing = velocity_trend == "regressing" or velocity < -1.0

            if not (is_stalled or is_regressing) or urgency < 3:
                continue

            # Create a StrategicGap object for generate_initiative_for_gap
            strategic_gap = StrategicGap(
                goal_id=goal_id,
                goal_name=gap.get("goal_name", ""),
                description=gap.get("description", ""),
                current_progress=100 - gap.get("gap_size", 0),
                target_progress=100,
                gap_size=gap.get("gap_size", 0),
                urgency=urgency,
                impact=gap.get("impact", ""),
                suggested_actions=gap.get("suggested_actions", []),
                velocity=velocity,
                velocity_trend=velocity_trend,
            )

            # Generate the initiative
            initiative = generate_initiative_for_gap(strategic_gap, existing_ids)
            if not initiative:
                continue

            # Add velocity context to description
            velocity_reason = "regressing" if is_regressing else "stalled"
            initiative.description = (
                f"{initiative.description}\n\n"
                f"**Auto-generated by WS-113-006:** Goal velocity is {velocity_reason} "
                f"({velocity:+.1f}%/day). This initiative was created automatically to "
                f"address the lack of progress."
            )

            # Auto-approve initiatives for urgent situations (urgency >= 4)
            if urgency >= 4:
                initiative.status = InitiativeStatus.APPROVED
                initiative.approved_at = datetime.now(timezone.utc).isoformat()

            self.state.active_initiatives.append(initiative)
            existing_ids.add(initiative.id)
            active_goal_ids.add(goal_id)

            result["created"] += 1
            result["details"].append(
                {
                    "initiative_id": initiative.id,
                    "goal_id": goal_id,
                    "goal_name": gap.get("goal_name", ""),
                    "velocity": velocity,
                    "velocity_trend": velocity_trend,
                    "auto_approved": urgency >= 4,
                }
            )

        return result


@dataclass
class StrategicState:
    """Persistent state for strategic planning."""

    last_assessment: str = ""
    last_planning_run: str = ""
    next_planning_run: str = ""
    active_initiatives: list[Initiative] = field(default_factory=list)
    completed_initiatives: list[Initiative] = field(default_factory=list)
    goal_snapshots: list[dict] = field(default_factory=list)
    goal_hierarchy: list[StrategicGoal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "last_assessment": self.last_assessment,
            "last_planning_run": self.last_planning_run,
            "next_planning_run": self.next_planning_run,
            "active_initiatives": [i.to_dict() for i in self.active_initiatives],
            "completed_initiatives": [i.to_dict() for i in self.completed_initiatives],
            "goal_snapshots": self.goal_snapshots,
            "goal_hierarchy": [g.to_dict() for g in self.goal_hierarchy],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategicState":
        return cls(
            last_assessment=d.get("last_assessment", ""),
            last_planning_run=d.get("last_planning_run", ""),
            next_planning_run=d.get("next_planning_run", ""),
            active_initiatives=[
                Initiative.from_dict(i) for i in d.get("active_initiatives", [])
            ],
            completed_initiatives=[
                Initiative.from_dict(i) for i in d.get("completed_initiatives", [])
            ],
            goal_snapshots=d.get("goal_snapshots", []),
            goal_hierarchy=[
                StrategicGoal.from_dict(g) for g in d.get("goal_hierarchy", [])
            ],
        )


# -----------------------------------------------------------------------------
# Sprint Management
# -----------------------------------------------------------------------------

SPRINT_DURATION_DAYS = 7  # One-week sprints


@dataclass
class SprintData:
    """Represents a sprint lifecycle."""

    id: str
    number: int
    goal: str
    status: str  # "active" | "closed"
    starts_at: str
    ends_at: str
    tasks_planned: int = 0
    tasks_completed: int = 0
    velocity: int = 0
    completion_rate: float = 0.0
    closed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "goal": self.goal,
            "status": self.status,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "tasks_planned": self.tasks_planned,
            "tasks_completed": self.tasks_completed,
            "velocity": self.velocity,
            "completion_rate": self.completion_rate,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SprintData":
        return cls(
            id=d.get("id", ""),
            number=d.get("number", 1),
            goal=d.get("goal", ""),
            status=d.get("status", "active"),
            starts_at=d.get("starts_at", ""),
            ends_at=d.get("ends_at", ""),
            tasks_planned=d.get("tasks_planned", 0),
            tasks_completed=d.get("tasks_completed", 0),
            velocity=d.get("velocity", 0),
            completion_rate=d.get("completion_rate", 0.0),
            closed_at=d.get("closed_at"),
        )


def _sprint_file(company_dir: Path) -> Path:
    return company_dir / "state/sprint.json"


def _sprint_history_file(company_dir: Path) -> Path:
    return company_dir / "state/sprint_history.json"


def load_current_sprint(company_dir: Path):
    """Load the current active sprint, or None if no sprint exists."""
    sprint_file = _sprint_file(company_dir)
    if not sprint_file.exists():
        return None
    try:
        with open(sprint_file) as f:
            return SprintData.from_dict(json.load(f))
    except Exception:
        return None


def _save_sprint(company_dir: Path, sprint: SprintData) -> None:
    """Atomically save sprint state to file."""
    import os
    import tempfile

    sprint_file = _sprint_file(company_dir)
    sprint_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(sprint_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sprint.to_dict(), f, indent=2)
        os.replace(tmp_path, str(sprint_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _count_sprint_tasks(company_dir: Path, since: str) -> tuple:
    """Return (tasks_completed, tasks_planned) created since the given ISO timestamp."""
    queue_file = company_dir / "state/work_queue.json"
    if not queue_file.exists():
        return 0, 0
    try:
        with open(queue_file) as f:
            queue = json.load(f)
    except Exception:
        return 0, 0

    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    completed = 0
    planned = 0

    for task in queue.get("completed", []):
        created = task.get("created_at", "")
        if created:
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if ct >= since_dt:
                    completed += 1
                    planned += 1
            except Exception:
                pass

    for task in queue.get("queue", []):
        created = task.get("created_at", "")
        if created:
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if ct >= since_dt:
                    planned += 1
            except Exception:
                pass

    return completed, planned


def open_new_sprint(company_dir: Path) -> SprintData:
    """Open a new sprint with a one-week duration.

    Derives sprint number from history length so numbers are always sequential.
    """

    history_file = _sprint_history_file(company_dir)
    history = []
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
        except Exception:
            history = []

    number = len(history) + 1
    now = datetime.now(timezone.utc)
    sprint = SprintData(
        id=f"sprint-{number:03d}",
        number=number,
        goal=f"Sprint {number}: Advance Q1 2026 goals",
        status="active",
        starts_at=now.isoformat(),
        ends_at=(now + timedelta(days=SPRINT_DURATION_DAYS)).isoformat(),
    )
    _save_sprint(company_dir, sprint)
    return sprint


def close_sprint(company_dir: Path) -> dict:
    """Close the current active sprint and record metrics in sprint history.

    Returns a summary dict with sprint_number, velocity, and completion_rate.
    Returns an empty dict if there is no active sprint to close.
    """
    import os
    import tempfile

    sprint = load_current_sprint(company_dir)
    if sprint is None or sprint.status != "active":
        return {}

    completed, planned = _count_sprint_tasks(company_dir, sprint.starts_at)
    completion_rate = completed / planned if planned > 0 else 0.0

    sprint.status = "closed"
    sprint.tasks_completed = completed
    sprint.tasks_planned = planned
    sprint.velocity = completed
    sprint.completion_rate = round(completion_rate, 3)
    sprint.closed_at = datetime.now(timezone.utc).isoformat()

    _save_sprint(company_dir, sprint)

    # Append to sprint history (atomic write)
    history_file = _sprint_history_file(company_dir)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append(sprint.to_dict())
    fd, tmp_path = tempfile.mkstemp(dir=str(history_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp_path, str(history_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {
        "sprint_number": sprint.number,
        "velocity": sprint.velocity,
        "completion_rate": sprint.completion_rate,
        "tasks_completed": sprint.tasks_completed,
        "tasks_planned": sprint.tasks_planned,
    }


# -----------------------------------------------------------------------------
# Gap Identification
# -----------------------------------------------------------------------------


def augment_assessments_from_hierarchy(assessments: list, goal_hierarchy: list) -> list:
    """WS-119: Synthesize at-risk assessments from strategic goal_hierarchy.

    The live goal_tracker only assesses goals defined in DEFAULT_GOALS via
    Python assessor functions. Goals populated directly into the strategic
    state's goal_hierarchy (e.g. market-launch goals G13-G15 with no live
    assessor) would otherwise be invisible to identify_gaps. This helper
    appends a synthetic at-risk assessment for each hierarchy goal whose
    current_metric is below 70% of target and that wasn't already assessed.
    """
    if not goal_hierarchy:
        return assessments
    try:
        assessed_ids = {a.goal_id for a in assessments}
        for sgoal in goal_hierarchy:
            sgoal_id = (
                getattr(sgoal, "id", None) or sgoal.get("id")
                if isinstance(sgoal, dict)
                else getattr(sgoal, "id", None)
            )
            if not sgoal_id or sgoal_id in assessed_ids:
                continue
            # Skip paused goals — they shouldn't generate new work
            sgoal_status = (
                getattr(sgoal, "status", "active")
                if not isinstance(sgoal, dict)
                else sgoal.get("status", "active")
            )
            if sgoal_status == "paused":
                continue
            target = (
                getattr(sgoal, "target_metric", None)
                if not isinstance(sgoal, dict)
                else sgoal.get("target_metric", 0)
            )
            current = (
                getattr(sgoal, "current_metric", None)
                if not isinstance(sgoal, dict)
                else sgoal.get("current_metric", 0)
            )
            name = (
                getattr(sgoal, "name", "")
                if not isinstance(sgoal, dict)
                else sgoal.get("name", "")
            )
            if not target or target <= 0:
                continue
            progress = (current or 0) / target
            if progress >= 0.7:
                continue
            synth = goal_tracker.GoalAssessment(
                goal_id=sgoal_id,
                goal_name=name,
                description=f"Strategic goal {sgoal_id}: {name}",
                success_metric=str(target),
                owner="forge-cto",
                current_value=current or 0,
                target_value=target,
                progress_percent=int(progress * 100),
                status=goal_tracker.GoalStatus.AT_RISK,
                status_reason=(
                    f"Strategic goal at {int(progress * 100)}% — below 70% target"
                ),
            )
            assessments.append(synth)
    except Exception:
        # Best-effort augmentation; silently skip if hierarchy is malformed
        pass
    return assessments


def identify_gaps(assessments: list) -> list[StrategicGap]:
    """Identify strategic gaps from goal assessments."""
    gaps = []

    # Build a status map for dependency blocking checks
    status_by_goal = {a.goal_id: a.status.value for a in assessments}

    for assessment in assessments:
        # Skip completed goals
        if assessment.status.value == "complete":
            continue

        # Skip goals whose dependencies are not yet complete
        if _incomplete_dependencies(assessment, status_by_goal):
            continue

        gap_size = 100 - assessment.progress_percent

        # Determine urgency based on status and gap size
        if assessment.status.value == "blocked":
            urgency = 5
        elif assessment.status.value == "at_risk" and gap_size > 30:
            urgency = 4
        elif assessment.status.value == "at_risk":
            urgency = 3
        elif gap_size > 50:
            urgency = 3
        elif gap_size > 20:
            urgency = 2
        else:
            urgency = 1

        # Generate impact description
        if urgency >= 4:
            impact = f"Critical: {assessment.goal_name} goal at risk of failure"
        elif urgency >= 3:
            impact = (
                f"Important: {assessment.goal_name} needs attention to meet targets"
            )
        else:
            impact = f"Monitor: {assessment.goal_name} making progress but has room for improvement"

        # WS-113-003: Include velocity in gap for priority boosting
        # Use isinstance check to handle MagicMock in tests
        velocity = getattr(assessment, "velocity_percent_per_day", 0.0)
        if not isinstance(velocity, (int, float)):
            velocity = 0.0
        velocity_trend = getattr(assessment, "velocity_trend", "unknown")
        if not isinstance(velocity_trend, str):
            velocity_trend = "unknown"

        # Boost urgency for stalled/regressing goals
        if velocity_trend == "regressing" or velocity < -1.0:
            urgency = min(urgency + 1, 5)  # Regressing goals are more urgent
        elif velocity_trend == "stalled" and gap_size > 20:
            urgency = min(urgency + 1, 5)  # Stalled goals with big gaps are urgent

        gap = StrategicGap(
            goal_id=assessment.goal_id,
            goal_name=assessment.goal_name,
            description=f"{assessment.goal_name} is at {assessment.progress_percent}%, target is 100%",
            current_progress=assessment.progress_percent,
            target_progress=100,
            gap_size=gap_size,
            urgency=urgency,
            impact=impact,
            suggested_actions=assessment.next_actions,
        )
        # WS-113-003: Add velocity to gap for task priority calculation
        gap.velocity = velocity
        gap.velocity_trend = velocity_trend
        gaps.append(gap)

    # Sort by urgency (highest first)
    gaps.sort(key=lambda g: g.urgency, reverse=True)
    return gaps


# -----------------------------------------------------------------------------
# Initiative Generation
# -----------------------------------------------------------------------------


def generate_initiative_for_gap(
    gap: StrategicGap, existing_ids: set[str]
) -> Initiative | None:
    """Generate an initiative to address a strategic gap."""
    # Generate unique ID
    base_id = f"P15-{gap.goal_id}"
    initiative_id = base_id
    counter = 1
    while initiative_id in existing_ids:
        initiative_id = f"{base_id}-{counter}"
        counter += 1

    # Determine size based on gap
    if gap.gap_size <= 10:
        size = InitiativeSize.SMALL
        approval = ApprovalType.AUTO
    elif gap.gap_size <= 30:
        size = InitiativeSize.MEDIUM
        approval = ApprovalType.HUMAN
    elif gap.gap_size <= 50:
        size = InitiativeSize.LARGE
        approval = ApprovalType.HUMAN
    else:
        size = InitiativeSize.EPIC
        approval = ApprovalType.EXECUTIVE

    # Override: Auto-approve critical goals (at_risk with high urgency)
    # This ensures failing tests, blocked goals get immediate attention
    if gap.urgency >= 4 and "at risk" in gap.impact.lower():
        approval = ApprovalType.AUTO

    # Generate tasks based on goal type
    tasks = generate_tasks_for_gap(gap, initiative_id)

    if not tasks:
        return None

    # Calculate priority (urgency * 20, capped at 100)
    priority = min(gap.urgency * 20, 100)

    return Initiative(
        id=initiative_id,
        title=f"Close {gap.goal_name} Gap ({gap.gap_size}%)",
        description=f"Initiative to improve {gap.goal_name} from {gap.current_progress}% to {gap.target_progress}%. {gap.impact}",
        goal_alignment=[gap.goal_id],
        size=size,
        approval_required=approval,
        status=InitiativeStatus.PROPOSED,
        owner=get_owner_for_goal(gap.goal_id),
        tasks=tasks,
        priority=priority,
    )


def generate_tasks_for_gap(
    gap: StrategicGap, initiative_id: str
) -> list[InitiativeTask]:
    """Generate specific tasks to close a gap."""
    tasks = []

    if gap.goal_id == "G1":  # Test coverage
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Run coverage analysis",
                description="Identify files with lowest coverage",
                action="analyze",
                estimated_effort="small",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Add tests for critical paths",
                description="Write tests for uncovered critical code paths",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-3",
                name="Verify coverage target met",
                description="Run pytest --cov and verify 50% threshold",
                action="verify",
                estimated_effort="small",
            ),
        ]

    elif gap.goal_id == "G2":  # Tutorials
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Create tutorial outline",
                description="Define structure for getting-started tutorial",
                file="docs/tutorials/01-getting-started.md",
                action="create",
                estimated_effort="small",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Write tutorial content",
                description="Complete tutorial with examples",
                action="implement",
                estimated_effort="medium",
            ),
        ]

    elif gap.goal_id == "G3":  # Stability
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Run test suite",
                description="Identify and fix failing tests",
                action="fix",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Address critical TODOs",
                description="Review and resolve P0/critical TODOs",
                action="fix",
                estimated_effort="medium",
            ),
        ]

    elif gap.goal_id == "G4":  # Enterprise
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Verify audit capabilities",
                description="Test audit-export and sbom commands",
                action="verify",
                estimated_effort="small",
            )
        ]

    elif gap.goal_id == "G5":  # Autonomy
        if "strategic_planner" in str(gap.suggested_actions):
            tasks = [
                InitiativeTask(
                    id=f"{initiative_id}-1",
                    name="Complete strategic planner",
                    description="Finish P15 implementation",
                    action="implement",
                    estimated_effort="large",
                )
            ]
        else:
            tasks = [
                InitiativeTask(
                    id=f"{initiative_id}-1",
                    name="Start daemon",
                    description="Ensure daemon is running with /daemon start",
                    action="configure",
                    estimated_effort="small",
                ),
                InitiativeTask(
                    id=f"{initiative_id}-2",
                    name="Submit test work",
                    description="Submit work via /submit to test autonomous execution",
                    action="test",
                    estimated_effort="small",
                ),
            ]

    elif gap.goal_id == "G6":  # Economics
        missing = gap.suggested_actions
        for i, action in enumerate(missing):
            tasks.append(
                InitiativeTask(
                    id=f"{initiative_id}-{i + 1}",
                    name=action,
                    description=f"Implement: {action}",
                    action="implement",
                    estimated_effort="medium",
                )
            )

    elif gap.goal_id == "G7":  # Sustained Autonomy
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Add daemon heartbeat monitoring",
                description="Implement heartbeat check in forge_daemon.py that writes daemon.heartbeat with timestamp every 60s",
                file=".claude/hooks/company/forge_daemon.py",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Implement health-based daemon recovery",
                description="Add automated restart mechanism when daemon.heartbeat is stale (>5min) or health score drops below threshold",
                file=".claude/hooks/company/forge_daemon.py",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-3",
                name="Track daemon uptime metrics",
                description="Record uptime windows and restart events in .company/state/daemon_metrics.json for trend analysis",
                file=".company/state/daemon_metrics.json",
                action="create",
                estimated_effort="small",
            ),
        ]

    elif gap.goal_id == "G8":  # Self-Improvement
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Run P30 improvement scan",
                description="Execute self_improvement.py scan to detect capability gaps and underperforming agents",
                file=".claude/hooks/company/self_improvement.py",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Analyze proposal history and implement top proposals",
                description="Review .company/improvement_proposals.json, identify highest-voted proposals, implement the top-scored enhancement",
                file=".company/improvement_proposals.json",
                action="implement",
                estimated_effort="medium",
            ),
        ]

    elif gap.goal_id == "G9":  # Stability
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Run pytest and fix failing tests",
                description="Execute pytest on tests/ directory, identify failures, fix root causes in source modules",
                file="tests/",
                action="fix",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Add regression tests for recent fixes",
                description="Write regression tests covering bugs fixed in the last 5 commits to prevent recurrence",
                file="tests/",
                action="create",
                estimated_effort="medium",
            ),
        ]

    elif gap.goal_id == "G10":  # Employee Initiative
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Review and score employee ideas",
                description="Parse employee_ideas.json, apply scoring rubric (impact, feasibility, alignment), rank ideas",
                file=".company/employee_ideas.json",
                action="implement",
                estimated_effort="small",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Promote top-scored ideas to work queue",
                description="Take top 3 scored ideas from employee_ideas.json and create concrete work queue tasks via employee_ideation.py",
                file=".claude/hooks/company/employee_ideation.py",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-3",
                name="Create idea scoring rubric",
                description="Implement standardized scoring function in employee_ideation.py with impact/feasibility/alignment dimensions",
                file=".claude/hooks/company/employee_ideation.py",
                action="create",
                estimated_effort="small",
            ),
        ]

    elif gap.goal_id == "G11":  # Queue Health
        tasks = [
            InitiativeTask(
                id=f"{initiative_id}-1",
                name="Analyze queue empty periods from daemon logs",
                description="Parse daemon log entries to identify periods when work_queue.json had zero pending tasks, calculate idle duration stats",
                file=".company/work_queue.json",
                action="implement",
                estimated_effort="medium",
            ),
            InitiativeTask(
                id=f"{initiative_id}-2",
                name="Add proactive task generation on low queue",
                description="Implement trigger in daemon loop that generates new tasks when pending queue drops below 2 items, using strategic goals as input",
                file=".claude/hooks/company/forge_daemon.py",
                action="implement",
                estimated_effort="medium",
            ),
        ]

    # Fallback: create generic tasks from suggested actions
    if not tasks and gap.suggested_actions:
        for i, action in enumerate(gap.suggested_actions[:3]):  # Max 3 tasks
            tasks.append(
                InitiativeTask(
                    id=f"{initiative_id}-{i + 1}",
                    name=action,
                    description=f"Action: {action}",
                    action="implement",
                    estimated_effort="medium",
                )
            )

    return tasks


def impact_score(initiative: Initiative) -> float:
    """Score an initiative on a 0.0-1.0 scale based on substantiveness.

    Scoring criteria:
    - Has specific file targets (+0.2)
    - Action is implement/fix/create, not analyze/review/document (+0.3)
    - Estimated effort is medium or higher (+0.2)
    - Description references code/tests/modules, not docs/formatting (+0.3)
    """
    score = 0.0

    # Check if any task has specific file targets
    has_file_targets = any(t.file for t in initiative.tasks)
    if has_file_targets:
        score += 0.2

    # Check if primary actions are substantive (implement/fix/create)
    substantive_actions = {"implement", "fix", "create"}
    non_substantive_actions = {"analyze", "review", "document"}
    task_actions = [t.action.lower() for t in initiative.tasks]
    substantive_count = sum(1 for a in task_actions if a in substantive_actions)
    non_substantive_count = sum(1 for a in task_actions if a in non_substantive_actions)
    if substantive_count > non_substantive_count:
        score += 0.3

    # Check estimated effort (medium or higher)
    high_effort = {"medium", "large", "epic"}
    has_high_effort = any(
        t.estimated_effort.lower() in high_effort for t in initiative.tasks
    )
    if has_high_effort:
        score += 0.2

    # Check description references code/tests/modules (not docs/formatting)
    desc_lower = initiative.description.lower()
    code_keywords = {
        "code",
        "test",
        "module",
        "function",
        "class",
        "implement",
        "fix",
        "bug",
        "pytest",
        "import",
        ".py",
        "daemon",
        "hook",
    }
    doc_keywords = {
        "docs",
        "formatting",
        "documentation",
        "readme",
        "tutorial",
        "guide",
        "outline",
    }
    has_code_ref = any(kw in desc_lower for kw in code_keywords)
    has_doc_ref = any(kw in desc_lower for kw in doc_keywords) and not has_code_ref
    if has_code_ref and not has_doc_ref:
        score += 0.3

    return round(score, 1)


def get_owner_for_goal(goal_id: str) -> str:
    """Get the owner for a goal."""
    owners = {
        "G1": "forge-cto",
        "G2": "marketing-lead",
        "G3": "forge-architect",
        "G4": "forge-security-engineer",
        "G5": "forge-cto",
        "G6": "forge-architect",
        "G7": "forge-cto",
        "G8": "forge-cto",
        "G9": "forge-architect",
        "G10": "forge-cto",
        "G11": "forge-cto",
    }
    return owners.get(goal_id, "forge-architect")


# -----------------------------------------------------------------------------
# Planning Functions
# -----------------------------------------------------------------------------


def load_state(company_dir: Path) -> StrategicState:
    """Load strategic state from file."""
    state_file = company_dir / "state/strategic_state.json"
    if state_file.exists():
        try:
            with open(state_file) as f:
                return StrategicState.from_dict(json.load(f))
        except Exception:
            pass
    return StrategicState()


def save_state(company_dir: Path, state: StrategicState) -> None:
    """Save strategic state to file.

    WS-089-002: Uses atomic write (tempfile + os.replace) to prevent
    corruption when concurrent readers access the file during write.
    """
    import os
    import tempfile

    state_file = company_dir / "state/strategic_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(state_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.replace(tmp_path, str(state_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def propose_initiatives(
    company_dir: Path, *, lightweight: bool = False
) -> list[Initiative]:
    """Generate initiative proposals based on current gaps."""
    _ensure_imports()

    # Get current goal assessments
    assessments = goal_tracker.assess_all_goals(company_dir, lightweight=lightweight)

    # Identify gaps
    gaps = identify_gaps(assessments)

    if not gaps:
        return []

    # Load existing state to get existing initiative IDs
    state = load_state(company_dir)
    existing_ids = {i.id for i in state.active_initiatives}
    existing_ids.update(i.id for i in state.completed_initiatives)

    # Track goals that already have ANY initiative (active or completed)
    # to prevent duplicate creation for the same gap.
    covered_goals: set[str] = set()
    for initiative in state.active_initiatives:
        if initiative.status != InitiativeStatus.REJECTED:
            for goal_id in initiative.goal_alignment:
                covered_goals.add(goal_id)

    # Filter out gaps that already have initiatives
    uncovered_gaps = [gap for gap in gaps if gap.goal_id not in covered_goals]

    # Generate initiatives for top gaps (limit to 3 non-rejected at a time)
    max_new = 3 - len(
        [i for i in state.active_initiatives if i.status != InitiativeStatus.REJECTED]
    )
    max_new = max(0, max_new)

    initiatives = []
    for gap in uncovered_gaps[:max_new]:
        initiative = generate_initiative_for_gap(gap, existing_ids)
        if initiative and impact_score(initiative) >= 0.3:
            initiatives.append(initiative)
            existing_ids.add(initiative.id)

    return initiatives


def run_planning_cycle(
    company_dir: Path, *, lightweight: bool = False
) -> dict[str, Any]:
    """
    Run a full strategic planning cycle.

    This is the original entry point, maintained for backward compatibility.
    Now delegates to run_weekly_cycle() and adapts the return format.

    Args:
        company_dir: Path to the .company directory
        lightweight: If True, skip expensive assessments (pytest).

    Returns:
        Dict with cycle results (backward-compatible format)
    """
    # Delegate to weekly cycle
    result = run_weekly_cycle(company_dir, lightweight=lightweight)

    # Adapt return format for backward compatibility
    return {
        "timestamp": result["timestamp"],
        "goals_assessed": result["goals_assessed"],
        "gaps_identified": result["gaps_identified"],
        "initiatives_proposed": result["initiatives_created"],
        "initiatives_auto_approved": result["initiatives_auto_approved"],
        "tasks_queued": result["tasks_queued"],
        "active_initiatives": result["active_initiatives"],
        "next_planning_run": result["next_weekly_run"],
    }


def _department_for_initiative(initiative: Initiative) -> str:
    """Resolve department from initiative's goal alignment (WS-054-002).

    Uses GOAL_CATEGORIES and CATEGORY_DEPARTMENTS from goal_scheduler
    to route tasks to the right department instead of hardcoding engineering.
    """
    try:
        from . import goal_scheduler
    except ImportError:
        try:
            import goal_scheduler  # type: ignore[no-redef]
        except ImportError:
            return "engineering"

    categories = getattr(goal_scheduler, "GOAL_CATEGORIES", {})
    dept_map = getattr(goal_scheduler, "CATEGORY_DEPARTMENTS", {})

    for goal_id in initiative.goal_alignment:
        category = categories.get(goal_id)
        if category:
            dept = dept_map.get(category)
            if dept:
                return dept

    return "engineering"


def queue_initiative_tasks(company_dir: Path, initiatives: list[Initiative]) -> int:
    """Queue tasks from approved initiatives to work queue via add_task().

    Routes through work_allocator.add_task() for proper dedup checking
    instead of manually appending to the queue.

    WS-108: Enhanced to preserve full initiative context in tasks.
    """
    try:
        from . import work_allocator
    except ImportError:
        import work_allocator  # type: ignore[no-redef]

    queued = 0

    for initiative in initiatives:
        # WS-108: Create initiative reference document (once per initiative)
        ref_doc_path = _create_initiative_reference_doc(initiative, company_dir)

        # Build goal alignment description
        goals_desc = (
            ", ".join(initiative.goal_alignment)
            if initiative.goal_alignment
            else "None"
        )

        for task in initiative.tasks:
            if task.status == "pending":
                title = f"[{initiative.id}] {task.name}"
                priority = 1 if initiative.priority >= 70 else 2

                # WS-054-002: Route department by goal category
                department = _department_for_initiative(initiative)

                # Map effort (small/medium/large) to complexity
                # (trivial/standard/complex/epic) for team_executor
                _effort_to_complexity = {
                    "small": "standard",
                    "medium": "complex",
                    "large": "epic",
                }
                complexity = _effort_to_complexity.get(
                    task.estimated_effort, "standard"
                )

                # WS-108: Build rich description with full context
                description_parts = [task.description]

                # Add acceptance criteria if present
                if task.acceptance:
                    description_parts.append("")
                    description_parts.append("**Acceptance Criteria:**")
                    description_parts.append(task.acceptance)

                # Add file context if specified
                if task.file:
                    description_parts.append("")
                    description_parts.append(f"**Target File:** `{task.file}`")
                    description_parts.append(f"**Action:** {task.action}")

                # Add initiative context
                description_parts.append("")
                description_parts.append("---")
                description_parts.append(f"**Initiative:** {initiative.title}")
                description_parts.append(f"**Initiative ID:** {initiative.id}")
                description_parts.append(f"**Owner:** {initiative.owner}")
                description_parts.append(f"**Priority:** {initiative.priority}/100")
                description_parts.append(f"**Goal Alignment:** {goals_desc}")

                # Reference the full initiative document
                if ref_doc_path:
                    description_parts.append("")
                    description_parts.append(f"**Full Context:** See `{ref_doc_path}`")

                rich_description = "\n".join(description_parts)

                # WS-108: All context is now in rich_description and reference doc
                result = work_allocator.add_task(
                    title=title,
                    priority=priority,
                    department=department,
                    required_capabilities=[],
                    deadline=None,
                    estimated_complexity=complexity,
                    dependencies=[],
                    description=rich_description,
                    project_id=None,
                    source="planning",
                )

                if result.get("success"):
                    task.status = "queued"
                    queued += 1
                elif result.get("error") == "duplicate_task":
                    # Duplicate detected — skip without counting
                    task.status = "queued"

    return queued


def approve_initiative(company_dir: Path, initiative_id: str) -> bool:
    """Approve a proposed initiative."""
    state = load_state(company_dir)

    for initiative in state.active_initiatives:
        if (
            initiative.id == initiative_id
            and initiative.status == InitiativeStatus.PROPOSED
        ):
            initiative.status = InitiativeStatus.APPROVED
            initiative.approved_at = datetime.now(timezone.utc).isoformat()
            save_state(company_dir, state)

            # Queue tasks
            queue_initiative_tasks(company_dir, [initiative])
            return True

    return False


def get_company_dir() -> Path:
    """Find the company directory."""
    cwd = Path.cwd()
    if (cwd / ".company").exists():
        return cwd / ".company"
    return cwd / ".company"


def run_weekly_cycle(company_dir: Path, *, lightweight: bool = False) -> dict[str, Any]:
    """
    Run a weekly planning cycle.

    Identifies gaps between current state and goals, then generates
    3-5 initiatives targeting the highest-priority gaps.

    Args:
        company_dir: Path to the .company directory
        lightweight: If True, skip expensive assessments (pytest).

    Returns:
        Dict with cycle results including initiatives created
    """
    cycle = WeeklyPlanningCycle(company_dir)
    return cycle.run(lightweight=lightweight)


def run_daily_cycle(
    company_dir: Path,
    *,
    last_ideation: str = "",
    defer_to_ideation: bool = False,
) -> dict[str, Any]:
    """
    Run a daily planning cycle.

    Breaks weekly initiatives into daily tasks and updates
    initiative progress based on task completion.

    Args:
        company_dir: Path to the .company directory
        last_ideation: ISO timestamp of last ideation cycle ("" = never ran).
        defer_to_ideation: When True, gap-fill defers until ideation has run once.

    Returns:
        Dict with cycle results including tasks created
    """
    cycle = DailyPlanningCycle(
        company_dir,
        last_ideation=last_ideation,
        defer_to_ideation=defer_to_ideation,
    )
    return cycle.run()


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Strategic planner for autonomous operation"
    )
    parser.add_argument(
        "command",
        choices=[
            "gaps",
            "propose",
            "plan",
            "active",
            "approve",
            "weekly",
            "daily",
            "hierarchy",
            "velocity",
        ],
        help="Command to run",
    )
    parser.add_argument("--initiative", "-i", help="Initiative ID (for approve)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    _ensure_imports()
    company_dir = get_company_dir()

    if args.command == "gaps":
        assessments = goal_tracker.assess_all_goals(company_dir)
        # WS-119: Augment with synthetic assessments from strategic state hierarchy
        try:
            _state = load_state(company_dir)
            assessments = augment_assessments_from_hierarchy(
                assessments, _state.goal_hierarchy
            )
        except Exception:
            pass
        gaps = identify_gaps(assessments)

        if args.json:
            print(json.dumps([g.to_dict() for g in gaps], indent=2))
        else:
            if not gaps:
                print("No strategic gaps identified. All goals on track!")
            else:
                print(f"Strategic Gaps Identified: {len(gaps)}\n")
                for gap in gaps:
                    urgency_bar = "!" * gap.urgency
                    print(f"[{urgency_bar:5}] {gap.goal_id}: {gap.goal_name}")
                    print(
                        f"        Progress: {gap.current_progress}% → Target: {gap.target_progress}%"
                    )
                    print(f"        Gap: {gap.gap_size}%")
                    print(f"        Impact: {gap.impact}")
                    if gap.suggested_actions:
                        print(f"        Actions: {', '.join(gap.suggested_actions)}")
                    print()

    elif args.command == "propose":
        initiatives = propose_initiatives(company_dir)

        if args.json:
            print(json.dumps([i.to_dict() for i in initiatives], indent=2))
        else:
            if not initiatives:
                print(
                    "No new initiatives proposed. Either no gaps or max active initiatives reached."
                )
            else:
                print(f"Initiatives Proposed: {len(initiatives)}\n")
                for init in initiatives:
                    print(f"[{init.id}] {init.title}")
                    print(
                        f"  Size: {init.size.value} | Approval: {init.approval_required.value}"
                    )
                    print(f"  Owner: {init.owner} | Priority: {init.priority}")
                    print(f"  Goals: {', '.join(init.goal_alignment)}")
                    print(f"  Tasks: {len(init.tasks)}")
                    for task in init.tasks:
                        print(f"    - {task.name}")
                    print()

    elif args.command == "plan":
        result = run_planning_cycle(company_dir)

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Strategic Planning Cycle Complete\n")
            print(f"  Goals Assessed: {result['goals_assessed']}")
            print(f"  Gaps Identified: {result['gaps_identified']}")
            print(f"  Initiatives Proposed: {result['initiatives_proposed']}")
            print(f"  Auto-Approved: {result['initiatives_auto_approved']}")
            print(f"  Tasks Queued: {result['tasks_queued']}")
            print(f"  Active Initiatives: {result['active_initiatives']}")
            print(f"  Next Run: {result['next_planning_run']}")

    elif args.command == "active":
        state = load_state(company_dir)

        if args.json:
            print(json.dumps([i.to_dict() for i in state.active_initiatives], indent=2))
        else:
            if not state.active_initiatives:
                print("No active initiatives.")
            else:
                print(f"Active Initiatives: {len(state.active_initiatives)}\n")
                for init in state.active_initiatives:
                    status_emoji = {
                        InitiativeStatus.PROPOSED: "○",
                        InitiativeStatus.APPROVED: "→",
                        InitiativeStatus.IN_PROGRESS: "⏳",
                        InitiativeStatus.COMPLETED: "✓",
                        InitiativeStatus.REJECTED: "✗",
                        InitiativeStatus.BLOCKED: "!",
                    }.get(init.status, "?")
                    print(f"{status_emoji} [{init.id}] {init.title}")
                    print(f"  Status: {init.status.value} | Size: {init.size.value}")
                    print(f"  Tasks: {len(init.tasks)} | Owner: {init.owner}")
                    print()

    elif args.command == "approve":
        if not args.initiative:
            print("Error: --initiative ID required", file=sys.stderr)
            sys.exit(1)

        if approve_initiative(company_dir, args.initiative):
            print(f"Initiative {args.initiative} approved and tasks queued.")
        else:
            print(
                f"Could not approve {args.initiative}. Not found or not in proposed state.",
                file=sys.stderr,
            )
            sys.exit(1)

    elif args.command == "weekly":
        result = run_weekly_cycle(company_dir)

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Weekly Planning Cycle Complete\n")
            print(f"  Cycle Type: {result['cycle_type']}")
            print(f"  Goals Assessed: {result['goals_assessed']}")
            print(f"  Gaps Identified: {result['gaps_identified']}")
            print(f"  Initiatives Created: {result['initiatives_created']}")
            print(f"  Auto-Approved: {result['initiatives_auto_approved']}")
            print(f"  Tasks Queued: {result['tasks_queued']}")
            print(f"  Active Initiatives: {result['active_initiatives']}")
            print(f"  Next Weekly Run: {result['next_weekly_run']}")

    elif args.command == "daily":
        result = run_daily_cycle(company_dir)

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Daily Planning Cycle Complete\n")
            print(f"  Cycle Type: {result['cycle_type']}")
            print(f"  Initiatives Updated: {result['initiatives_updated']}")
            print(f"  Daily Tasks Created: {result['daily_tasks_created']}")
            print(f"  Active Initiatives: {result['active_initiatives']}")
            if result.get("progress_updates"):
                print("\n  Progress Updates:")
                for update in result["progress_updates"]:
                    print(
                        f"    • {update['initiative_id']}: "
                        f"{update['old_status']} → {update['new_status']} "
                        f"({update['completed_tasks']}/{update['total_tasks']} tasks)"
                    )

    elif args.command == "hierarchy":
        state = load_state(company_dir)

        if args.json:
            print(
                json.dumps(
                    {
                        "goal_hierarchy": [g.to_dict() for g in state.goal_hierarchy],
                        "active_initiatives": [
                            i.to_dict() for i in state.active_initiatives
                        ],
                    },
                    indent=2,
                )
            )
        else:
            # Display hierarchy tree
            print("Goal Hierarchy\n")

            if state.goal_hierarchy:
                # Group goals by level
                by_level: dict[GoalLevel, list[StrategicGoal]] = {}
                for goal in state.goal_hierarchy:
                    if goal.level not in by_level:
                        by_level[goal.level] = []
                    by_level[goal.level].append(goal)

                # Display by level
                for level in [
                    GoalLevel.ANNUAL,
                    GoalLevel.QUARTERLY,
                    GoalLevel.WEEKLY,
                    GoalLevel.DAILY,
                ]:
                    if level in by_level:
                        print(f"  {level.value.upper()} GOALS:")
                        for goal in by_level[level]:
                            progress = (
                                int(goal.current_metric / goal.target_metric * 100)
                                if goal.target_metric > 0
                                else 0
                            )
                            parent_str = (
                                f" (parent: {goal.parent_id})" if goal.parent_id else ""
                            )
                            print(
                                f"    {goal.id}: {goal.name} ({progress}%){parent_str}"
                            )
                        print()
            else:
                print("  No goal hierarchy defined yet.\n")
                print("  Goal hierarchy is built automatically as:")
                print(
                    "    1. Weekly cycles decompose annual → quarterly → weekly goals"
                )
                print("    2. Daily cycles create daily tasks from weekly goals")
                print("    3. Initiatives track progress toward goals\n")

            # Also show current initiatives as a proxy for active goals
            if state.active_initiatives:
                print("  ACTIVE INITIATIVES (mapped to goals):")
                for init in state.active_initiatives:
                    if init.status not in (
                        InitiativeStatus.COMPLETED,
                        InitiativeStatus.REJECTED,
                    ):
                        status_char = {
                            InitiativeStatus.PROPOSED: "○",
                            InitiativeStatus.APPROVED: "→",
                            InitiativeStatus.IN_PROGRESS: "⏳",
                            InitiativeStatus.BLOCKED: "!",
                        }.get(init.status, "?")
                        goals_str = ", ".join(init.goal_alignment)
                        print(
                            f"    {status_char} [{init.id}] → {goals_str}: {init.title}"
                        )
                print()

    elif args.command == "velocity":
        state = load_state(company_dir)

        if args.json:
            # Calculate velocity data
            velocity_data = _calculate_velocity(state.goal_snapshots)
            print(json.dumps(velocity_data, indent=2))
        else:
            print("Goal Velocity Report\n")

            if len(state.goal_snapshots) < 2:
                print("  No velocity data available yet.\n")
                print("  Velocity tracking requires multiple goal snapshots.")
                print("  Run /strategy plan or /strategy weekly to create snapshots.\n")
                print(f"  Current snapshot count: {len(state.goal_snapshots)}")
                print("  Required for velocity: 2+")
            else:
                velocity_data = _calculate_velocity(state.goal_snapshots)

                print("  COMPLETION VELOCITY (recent snapshots)\n")
                for goal_id, data in velocity_data.get("goals", {}).items():
                    trend_char = (
                        "▲"
                        if data["velocity"] > 0
                        else ("▼" if data["velocity"] < 0 else "—")
                    )
                    status = ""
                    if data["velocity"] < -5:
                        status = " (NEEDS ATTENTION)"
                    elif data["current"] >= 100:
                        status = " (complete)"

                    print(f"  {goal_id}: {data.get('name', 'Unknown')}")
                    print(
                        f"      Progress: {data['first']}% → {data['current']}% "
                        f"({data['change']:+}%)"
                    )
                    print(f"      Velocity: {data['velocity']:+.1f}%/snapshot")
                    print(f"      Trend: {trend_char}{status}")
                    print()

                # Summary
                improving = sum(
                    1
                    for d in velocity_data.get("goals", {}).values()
                    if d["velocity"] > 0
                )
                stable = sum(
                    1
                    for d in velocity_data.get("goals", {}).values()
                    if d["velocity"] == 0
                )
                regressing = sum(
                    1
                    for d in velocity_data.get("goals", {}).values()
                    if d["velocity"] < 0
                )

                print(
                    f"  SUMMARY: {improving} improving | {stable} stable | {regressing} regressing"
                )


def _calculate_velocity(snapshots: list[dict]) -> dict:
    """Calculate velocity metrics from goal snapshots."""
    if len(snapshots) < 2:
        return {"goals": {}, "error": "Not enough snapshots"}

    # Get first and last snapshots
    first_snapshot = snapshots[0]
    last_snapshot = snapshots[-1]

    # Build goal data
    goals = {}

    # Index assessments by goal_id
    first_by_id = {a["goal_id"]: a for a in first_snapshot.get("assessments", [])}
    last_by_id = {a["goal_id"]: a for a in last_snapshot.get("assessments", [])}

    for goal_id in set(first_by_id.keys()) | set(last_by_id.keys()):
        first_assessment = first_by_id.get(goal_id, {})
        last_assessment = last_by_id.get(goal_id, {})

        first_progress = first_assessment.get("progress_percent", 0)
        current_progress = last_assessment.get("progress_percent", 0)
        change = current_progress - first_progress

        # Calculate velocity per snapshot
        num_snapshots = len(snapshots)
        velocity = change / max(num_snapshots - 1, 1)

        goals[goal_id] = {
            "name": last_assessment.get(
                "goal_name", first_assessment.get("goal_name", "Unknown")
            ),
            "first": first_progress,
            "current": current_progress,
            "change": change,
            "velocity": round(velocity, 2),
            "snapshots": num_snapshots,
        }

    return {
        "goals": goals,
        "first_timestamp": first_snapshot.get("timestamp"),
        "last_timestamp": last_snapshot.get("timestamp"),
        "snapshot_count": len(snapshots),
    }


if __name__ == "__main__":
    main()
