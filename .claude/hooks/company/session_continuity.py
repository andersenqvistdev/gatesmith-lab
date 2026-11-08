#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P25 Session Continuity — Context Persistence Across Daemon Restarts.

Enables the daemon to survive restarts without losing context. Captures
periodic snapshots and restores queue state, goal priorities, and
in-progress tasks on startup.

Key Features:
    - SessionSnapshot dataclass with all state needed for recovery
    - Periodic snapshot capture (configurable interval)
    - Graceful shutdown snapshot
    - Safe recovery of in-progress tasks
    - Snapshot rotation (keeps last N snapshots)
    - Corrupted snapshot fallback to fresh start
    - Recovery logging for observability

Storage:
    - Snapshots: .company/daemon_snapshots/
    - Rotation: Keeps 5 most recent snapshots by default
    - Format: JSON with atomic writes

Usage:
    # Capture a snapshot (called by daemon)
    python session_continuity.py capture

    # Restore from latest snapshot (called on daemon startup)
    python session_continuity.py restore

    # List available snapshots
    python session_continuity.py list

    # Clean old snapshots beyond retention limit
    python session_continuity.py cleanup

    # Show snapshot details
    python session_continuity.py show --snapshot <filename>

    # Force fresh start (ignore snapshots)
    python session_continuity.py reset
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("session_continuity")

# =============================================================================
# Configuration
# =============================================================================

SNAPSHOT_DIR = "daemon_snapshots"
SNAPSHOT_PREFIX = "snapshot_"
SNAPSHOT_SUFFIX = ".json"
MAX_SNAPSHOTS = 5  # Keep last N snapshots
SNAPSHOT_VERSION = "1.0"

# Minimum seconds between snapshots to prevent excessive I/O
MIN_SNAPSHOT_INTERVAL_SECONDS = 60


# =============================================================================
# Path Utilities
# =============================================================================


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def _get_company_dir() -> Path:
    """Get the company directory path."""
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        return company_resolver.get_company_dir()
    except (ImportError, Exception):
        # Fallback to default
        return Path.cwd() / ".company"


def _get_snapshot_dir() -> Path:
    """Get the snapshot directory path."""
    return _get_company_dir() / SNAPSHOT_DIR


def _ensure_snapshot_dir() -> Path:
    """Ensure snapshot directory exists and return its path."""
    snapshot_dir = _get_snapshot_dir()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    return snapshot_dir


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class InProgressTask:
    """Snapshot of a task that was in progress when snapshot was taken.

    Used to recover partial work safely on restart.
    """

    task_id: str
    title: str
    employee_id: str | None
    started_at: str
    progress_state: str  # "assigned", "executing", "validating"
    partial_output: str | None = None
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "employee_id": self.employee_id,
            "started_at": self.started_at,
            "progress_state": self.progress_state,
            "partial_output": self.partial_output,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InProgressTask":
        """Create from dictionary."""
        return cls(
            task_id=data.get("task_id", ""),
            title=data.get("title", ""),
            employee_id=data.get("employee_id"),
            started_at=data.get("started_at", ""),
            progress_state=data.get("progress_state", "assigned"),
            partial_output=data.get("partial_output"),
            retry_count=data.get("retry_count", 0),
        )


@dataclass
class GoalPrioritySnapshot:
    """Snapshot of goal priorities at capture time."""

    goal_id: str
    priority_score: float
    current_progress: float
    strategic_weight: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "goal_id": self.goal_id,
            "priority_score": self.priority_score,
            "current_progress": self.current_progress,
            "strategic_weight": self.strategic_weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalPrioritySnapshot":
        """Create from dictionary."""
        return cls(
            goal_id=data.get("goal_id", ""),
            priority_score=data.get("priority_score", 0.0),
            current_progress=data.get("current_progress", 0.0),
            strategic_weight=data.get("strategic_weight", 1.0),
        )


@dataclass
class QueueState:
    """Snapshot of work queue state."""

    pending_count: int
    in_progress_count: int
    blocked_count: int
    completed_count: int
    pending_task_ids: list[str] = field(default_factory=list)
    queue_order_hash: str = ""  # Hash of pending task ID order for change detection

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pending_count": self.pending_count,
            "in_progress_count": self.in_progress_count,
            "blocked_count": self.blocked_count,
            "completed_count": self.completed_count,
            "pending_task_ids": self.pending_task_ids,
            "queue_order_hash": self.queue_order_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueState":
        """Create from dictionary."""
        return cls(
            pending_count=data.get("pending_count", 0),
            in_progress_count=data.get("in_progress_count", 0),
            blocked_count=data.get("blocked_count", 0),
            completed_count=data.get("completed_count", 0),
            pending_task_ids=data.get("pending_task_ids", []),
            queue_order_hash=data.get("queue_order_hash", ""),
        )


@dataclass
class SessionSnapshot:
    """Complete snapshot of daemon session state for recovery.

    Captures all necessary context to resume operations after a restart.
    """

    # Snapshot metadata
    version: str = SNAPSHOT_VERSION
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    capture_reason: str = "periodic"  # "periodic", "shutdown", "manual"
    daemon_pid: int = 0
    daemon_uptime_seconds: float = 0.0

    # Queue state
    queue_state: QueueState | None = None

    # In-progress work
    in_progress_tasks: list[InProgressTask] = field(default_factory=list)
    current_task_id: str | None = None

    # Goal priorities (from goal_scheduler)
    goal_priorities: list[GoalPrioritySnapshot] = field(default_factory=list)

    # Daemon metrics at snapshot time
    tasks_completed: int = 0
    tasks_failed: int = 0

    # Learning insights summary (from learning_loop)
    best_employees_by_complexity: dict[str, list[str]] = field(default_factory=dict)
    failure_pattern_count: int = 0

    # Recovery metadata
    recovery_attempted: bool = False
    recovery_success: bool = False
    recovery_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "captured_at": self.captured_at,
            "capture_reason": self.capture_reason,
            "daemon_pid": self.daemon_pid,
            "daemon_uptime_seconds": self.daemon_uptime_seconds,
            "queue_state": self.queue_state.to_dict() if self.queue_state else None,
            "in_progress_tasks": [t.to_dict() for t in self.in_progress_tasks],
            "current_task_id": self.current_task_id,
            "goal_priorities": [g.to_dict() for g in self.goal_priorities],
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "best_employees_by_complexity": self.best_employees_by_complexity,
            "failure_pattern_count": self.failure_pattern_count,
            "recovery_attempted": self.recovery_attempted,
            "recovery_success": self.recovery_success,
            "recovery_notes": self.recovery_notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionSnapshot":
        """Create from dictionary."""
        queue_state = None
        if data.get("queue_state"):
            queue_state = QueueState.from_dict(data["queue_state"])

        return cls(
            version=data.get("version", SNAPSHOT_VERSION),
            captured_at=data.get("captured_at", ""),
            capture_reason=data.get("capture_reason", "unknown"),
            daemon_pid=data.get("daemon_pid", 0),
            daemon_uptime_seconds=data.get("daemon_uptime_seconds", 0.0),
            queue_state=queue_state,
            in_progress_tasks=[
                InProgressTask.from_dict(t) for t in data.get("in_progress_tasks", [])
            ],
            current_task_id=data.get("current_task_id"),
            goal_priorities=[
                GoalPrioritySnapshot.from_dict(g)
                for g in data.get("goal_priorities", [])
            ],
            tasks_completed=data.get("tasks_completed", 0),
            tasks_failed=data.get("tasks_failed", 0),
            best_employees_by_complexity=data.get("best_employees_by_complexity", {}),
            failure_pattern_count=data.get("failure_pattern_count", 0),
            recovery_attempted=data.get("recovery_attempted", False),
            recovery_success=data.get("recovery_success", False),
            recovery_notes=data.get("recovery_notes", ""),
        )


# =============================================================================
# Session Continuity Class
# =============================================================================


class SessionContinuity:
    """Manages session continuity across daemon restarts.

    Provides snapshot capture, storage, rotation, and recovery functionality
    to ensure smooth daemon restarts without losing context.

    Usage:
        continuity = SessionContinuity()

        # On daemon shutdown or periodic checkpoint
        continuity.capture_snapshot(
            daemon_state=daemon_state,
            reason="shutdown"
        )

        # On daemon startup
        snapshot = continuity.restore_snapshot()
        if snapshot:
            continuity.handle_partial_work(snapshot)
    """

    def __init__(
        self,
        max_snapshots: int = MAX_SNAPSHOTS,
        min_interval: int = MIN_SNAPSHOT_INTERVAL_SECONDS,
    ):
        """Initialize session continuity manager.

        Args:
            max_snapshots: Maximum number of snapshots to retain
            min_interval: Minimum seconds between snapshots
        """
        self.max_snapshots = max_snapshots
        self.min_interval = min_interval
        self._last_snapshot_time: datetime | None = None

    def _generate_snapshot_filename(self) -> str:
        """Generate a unique snapshot filename based on timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"{SNAPSHOT_PREFIX}{timestamp}{SNAPSHOT_SUFFIX}"

    def _list_snapshots(self) -> list[Path]:
        """List all snapshot files sorted by modification time (newest first)."""
        snapshot_dir = _get_snapshot_dir()
        if not snapshot_dir.exists():
            return []

        snapshots = list(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}"))
        # Sort by modification time, newest first
        snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return snapshots

    def _rotate_snapshots(self) -> int:
        """Remove old snapshots beyond retention limit.

        Returns:
            Number of snapshots removed
        """
        snapshots = self._list_snapshots()
        removed = 0

        if len(snapshots) > self.max_snapshots:
            to_remove = snapshots[self.max_snapshots :]
            for snapshot_path in to_remove:
                try:
                    snapshot_path.unlink()
                    removed += 1
                    logger.debug(f"Removed old snapshot: {snapshot_path.name}")
                except OSError as e:
                    logger.warning(f"Failed to remove snapshot {snapshot_path}: {e}")

        return removed

    def _load_work_queue(self) -> dict[str, Any]:
        """Load work queue from file."""
        queue_path = _get_company_dir() / "state/work_queue.json"
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
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load work queue: {e}")
            return {
                "pending": [],
                "in_progress": [],
                "blocked": [],
                "completed": [],
            }

    def _compute_queue_hash(self, task_ids: list[str]) -> str:
        """Compute a hash of task ID order for change detection."""
        import hashlib

        content = ",".join(task_ids)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def can_capture(self) -> bool:
        """Check if enough time has passed since last snapshot.

        Returns:
            True if a new snapshot can be captured
        """
        if self._last_snapshot_time is None:
            return True

        elapsed = (
            datetime.now(timezone.utc) - self._last_snapshot_time
        ).total_seconds()
        return elapsed >= self.min_interval

    def capture_snapshot(
        self,
        daemon_state: dict[str, Any] | None = None,
        reason: str = "periodic",
        force: bool = False,
        include_slow_data: bool = False,
    ) -> SessionSnapshot | None:
        """Capture a session snapshot.

        Gathers current state from work queue, goal scheduler, and learning
        loop to create a comprehensive snapshot for recovery.

        Args:
            daemon_state: Current daemon state dict (optional)
            reason: Why snapshot is being captured ("periodic", "shutdown", "manual")
            force: If True, ignore minimum interval check
            include_slow_data: If True, include goal priorities and learning insights
                              (these can be slow as they run goal assessments)

        Returns:
            SessionSnapshot if captured, None if skipped due to interval
        """
        # Check interval unless forced
        if not force and not self.can_capture():
            logger.debug("Skipping snapshot - too soon since last capture")
            return None

        snapshot_dir = _ensure_snapshot_dir()
        now = datetime.now(timezone.utc)

        # Build snapshot
        snapshot = SessionSnapshot(
            captured_at=now.isoformat(),
            capture_reason=reason,
        )

        # Add daemon state if provided
        if daemon_state:
            snapshot.daemon_pid = daemon_state.get("pid", 0)
            started_at = daemon_state.get("started_at", "")
            if started_at:
                try:
                    start_time = datetime.fromisoformat(started_at)
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)
                    snapshot.daemon_uptime_seconds = (now - start_time).total_seconds()
                except (ValueError, TypeError):
                    pass
            snapshot.tasks_completed = daemon_state.get("tasks_completed", 0)
            snapshot.tasks_failed = daemon_state.get("tasks_failed", 0)
            snapshot.current_task_id = daemon_state.get("current_task")

        # Capture queue state
        queue = self._load_work_queue()
        pending = queue.get("pending", [])
        in_progress = queue.get("in_progress", [])

        pending_ids = [t.get("task_id", "") for t in pending if t.get("task_id")]
        snapshot.queue_state = QueueState(
            pending_count=len(pending),
            in_progress_count=len(in_progress),
            blocked_count=len(queue.get("blocked", [])),
            completed_count=len(queue.get("completed", [])),
            pending_task_ids=pending_ids[:100],  # Limit to first 100 for size
            queue_order_hash=self._compute_queue_hash(pending_ids),
        )

        # Capture in-progress tasks for recovery
        for task in in_progress:
            snapshot.in_progress_tasks.append(
                InProgressTask(
                    task_id=task.get("task_id", ""),
                    title=task.get("title", "")[:100],
                    employee_id=task.get("assigned_to"),
                    started_at=task.get("started_at", ""),
                    progress_state="executing",
                )
            )

        # Capture goal priorities if available (skip if slow data disabled)
        # Note: This can be slow as it runs goal assessments
        if include_slow_data:
            try:
                module_dir = _get_module_dir()
                if str(module_dir) not in sys.path:
                    sys.path.insert(0, str(module_dir))

                try:
                    from . import goal_scheduler
                except ImportError:
                    import goal_scheduler  # type: ignore[no-redef]

                priorities = goal_scheduler.compute_goal_priorities()
                for goal_id, priority in priorities.items():
                    snapshot.goal_priorities.append(
                        GoalPrioritySnapshot(
                            goal_id=goal_id,
                            priority_score=priority.priority_score,
                            current_progress=priority.current_progress,
                            strategic_weight=priority.strategic_weight,
                        )
                    )
            except Exception as e:
                logger.debug(f"Could not capture goal priorities: {e}")

        # Capture learning insights summary if available (always fast)
        try:
            try:
                from . import learning_loop
            except ImportError:
                import learning_loop  # type: ignore[no-redef]

            insights = learning_loop.compute_insights()
            snapshot.best_employees_by_complexity = (
                insights.best_employees_by_complexity
            )
            snapshot.failure_pattern_count = len(insights.failure_patterns)
        except Exception as e:
            logger.debug(f"Could not capture learning insights: {e}")

        # Save snapshot with atomic write
        filename = self._generate_snapshot_filename()
        snapshot_path = snapshot_dir / filename

        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix="snapshot_",
            dir=str(snapshot_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, indent=2)
            os.replace(tmp_path, snapshot_path)
            logger.info(f"Captured snapshot: {filename} (reason: {reason})")
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None

        # Update last snapshot time
        self._last_snapshot_time = now

        # Rotate old snapshots
        removed = self._rotate_snapshots()
        if removed > 0:
            logger.debug(f"Rotated {removed} old snapshots")

        return snapshot

    def restore_snapshot(self) -> SessionSnapshot | None:
        """Restore from the most recent valid snapshot.

        Attempts to load the latest snapshot. Falls back to older snapshots
        if the latest is corrupted. Returns None for fresh start if no
        valid snapshots are found.

        Returns:
            SessionSnapshot if restored, None for fresh start
        """
        snapshots = self._list_snapshots()

        if not snapshots:
            logger.info("No snapshots found - starting fresh")
            return None

        # Try each snapshot from newest to oldest
        for snapshot_path in snapshots:
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    data = json.load(f)

                snapshot = SessionSnapshot.from_dict(data)

                # Validate version compatibility
                if snapshot.version != SNAPSHOT_VERSION:
                    logger.warning(
                        f"Snapshot {snapshot_path.name} has incompatible version "
                        f"{snapshot.version}, expected {SNAPSHOT_VERSION}"
                    )
                    continue

                logger.info(
                    f"Restored snapshot: {snapshot_path.name} "
                    f"(captured: {snapshot.captured_at}, reason: {snapshot.capture_reason})"
                )
                return snapshot

            except json.JSONDecodeError as e:
                logger.warning(f"Corrupted snapshot {snapshot_path.name}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Failed to load snapshot {snapshot_path.name}: {e}")
                continue

        logger.warning("No valid snapshots found - starting fresh")
        return None

    def handle_partial_work(
        self,
        snapshot: SessionSnapshot,
    ) -> dict[str, Any]:
        """Safely recover in-progress tasks from a snapshot.

        Tasks that were in progress when the daemon stopped need careful
        handling to avoid duplicate execution or lost work.

        Strategy:
        1. Tasks in "assigned" state: Can be safely re-assigned
        2. Tasks in "executing" state: Move back to pending (may need cleanup)
        3. Tasks in "validating" state: Check for completed output

        Args:
            snapshot: The snapshot containing in-progress tasks

        Returns:
            Dictionary with recovery results:
            - reassigned: List of task IDs that were re-queued
            - skipped: List of task IDs that were skipped (already done)
            - errors: List of task IDs with recovery errors
        """
        results = {
            "reassigned": [],
            "skipped": [],
            "errors": [],
            "notes": [],
        }

        if not snapshot.in_progress_tasks:
            results["notes"].append("No in-progress tasks to recover")
            return results

        # Use QueueLock to prevent race conditions with concurrent queue operations
        try:
            from .work_allocator import QueueLock
        except ImportError:
            from work_allocator import QueueLock  # type: ignore[no-redef]

        lock_path = _get_company_dir() / "runtime/queue.lock"

        with QueueLock(lock_path):
            # Load current queue state (under lock)
            queue = self._load_work_queue()
            pending = queue.get("pending", [])
            in_progress = queue.get("in_progress", [])
            completed = queue.get("completed", [])

            # Get IDs of tasks in each state
            pending_ids = {t.get("task_id") for t in pending}
            in_progress_ids = {t.get("task_id") for t in in_progress}
            completed_ids = {t.get("task_id") for t in completed}

            for task in snapshot.in_progress_tasks:
                task_id = task.task_id

                try:
                    # Skip if already completed
                    if task_id in completed_ids:
                        results["skipped"].append(task_id)
                        results["notes"].append(
                            f"Task {task_id} already completed - skipping"
                        )
                        continue

                    # Skip if already back in pending
                    if task_id in pending_ids:
                        results["skipped"].append(task_id)
                        results["notes"].append(
                            f"Task {task_id} already in pending - skipping"
                        )
                        continue

                    # If still in progress, we need to handle it
                    if task_id in in_progress_ids:
                        # Find the task
                        task_data = None
                        task_index = None
                        for i, t in enumerate(in_progress):
                            if t.get("task_id") == task_id:
                                task_data = t
                                task_index = i
                                break

                        if task_data and task_index is not None:
                            # Move back to pending for retry
                            # Reset assignment state
                            task_data["assigned_to"] = None
                            task_data["assigned_at"] = None
                            task_data["started_at"] = None

                            # Increment retry count if present
                            retry_count = task_data.get("retry_count", 0) + 1
                            task_data["retry_count"] = retry_count
                            task_data["recovery_note"] = (
                                f"Recovered from snapshot after daemon restart "
                                f"(retry {retry_count})"
                            )

                            # Move to pending
                            in_progress.pop(task_index)
                            pending.insert(0, task_data)  # High priority

                            results["reassigned"].append(task_id)
                            results["notes"].append(
                                f"Task {task_id} moved back to pending (retry {retry_count})"
                            )

                except Exception as e:
                    results["errors"].append(task_id)
                    results["notes"].append(f"Error recovering task {task_id}: {e}")
                    logger.error(f"Error recovering task {task_id}: {e}")

            # Save updated queue if changes were made (still under lock)
            if results["reassigned"]:
                queue["pending"] = pending
                queue["in_progress"] = in_progress

                queue_path = _get_company_dir() / "state/work_queue.json"
                fd, tmp_path = tempfile.mkstemp(
                    suffix=".json",
                    prefix="work_queue_",
                    dir=str(_get_company_dir()),
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    os.replace(tmp_path, queue_path)
                    logger.info(
                        f"Recovered {len(results['reassigned'])} in-progress tasks"
                    )
                except Exception as e:
                    logger.error(f"Failed to save recovered queue: {e}")
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    results["errors"].extend(results["reassigned"])
                    results["reassigned"] = []

        return results

    def get_latest_snapshot_info(self) -> dict[str, Any] | None:
        """Get information about the most recent snapshot without full load.

        Returns:
            Dictionary with snapshot metadata or None if no snapshots exist
        """
        snapshots = self._list_snapshots()
        if not snapshots:
            return None

        latest = snapshots[0]
        try:
            stat = latest.stat()
            with open(latest, encoding="utf-8") as f:
                data = json.load(f)

            return {
                "filename": latest.name,
                "path": str(latest),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "captured_at": data.get("captured_at", ""),
                "capture_reason": data.get("capture_reason", ""),
                "daemon_pid": data.get("daemon_pid", 0),
                "tasks_completed": data.get("tasks_completed", 0),
                "in_progress_count": len(data.get("in_progress_tasks", [])),
            }
        except Exception as e:
            logger.warning(f"Failed to get snapshot info: {e}")
            return None

    def cleanup_old_snapshots(self) -> int:
        """Remove snapshots beyond retention limit.

        Returns:
            Number of snapshots removed
        """
        return self._rotate_snapshots()

    def reset(self) -> int:
        """Remove all snapshots for a fresh start.

        Returns:
            Number of snapshots removed
        """
        snapshots = self._list_snapshots()
        removed = 0

        for snapshot_path in snapshots:
            try:
                snapshot_path.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"Failed to remove snapshot {snapshot_path}: {e}")

        logger.info(f"Reset: removed {removed} snapshots")
        return removed


# =============================================================================
# Module-Level Functions
# =============================================================================


def capture_snapshot(
    daemon_state: dict[str, Any] | None = None,
    reason: str = "periodic",
    force: bool = False,
    include_slow_data: bool = False,
) -> SessionSnapshot | None:
    """Convenience function to capture a snapshot.

    Args:
        daemon_state: Current daemon state dict
        reason: Why snapshot is being captured
        force: If True, ignore minimum interval check
        include_slow_data: If True, include goal priorities (slow)

    Returns:
        SessionSnapshot if captured, None if skipped
    """
    continuity = SessionContinuity()
    return continuity.capture_snapshot(daemon_state, reason, force, include_slow_data)


def restore_snapshot() -> SessionSnapshot | None:
    """Convenience function to restore from the latest snapshot.

    Returns:
        SessionSnapshot if restored, None for fresh start
    """
    continuity = SessionContinuity()
    return continuity.restore_snapshot()


def handle_partial_work(snapshot: SessionSnapshot) -> dict[str, Any]:
    """Convenience function to handle partial work recovery.

    Args:
        snapshot: The snapshot to recover from

    Returns:
        Recovery results dictionary
    """
    continuity = SessionContinuity()
    return continuity.handle_partial_work(snapshot)


def get_recovery_summary(snapshot: SessionSnapshot) -> dict[str, Any]:
    """Generate a summary of what would be recovered from a snapshot.

    Args:
        snapshot: The snapshot to summarize

    Returns:
        Summary dictionary with counts and key info
    """
    return {
        "captured_at": snapshot.captured_at,
        "capture_reason": snapshot.capture_reason,
        "daemon_uptime_seconds": snapshot.daemon_uptime_seconds,
        "tasks_completed": snapshot.tasks_completed,
        "tasks_failed": snapshot.tasks_failed,
        "queue_state": snapshot.queue_state.to_dict() if snapshot.queue_state else None,
        "in_progress_task_count": len(snapshot.in_progress_tasks),
        "in_progress_task_ids": [t.task_id for t in snapshot.in_progress_tasks],
        "goal_priority_count": len(snapshot.goal_priorities),
        "best_employees_available": bool(snapshot.best_employees_by_complexity),
    }


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="P25 Session Continuity — Context Persistence Across Daemon Restarts"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Capture command
    capture_parser = subparsers.add_parser("capture", help="Capture a snapshot")
    capture_parser.add_argument(
        "--reason",
        default="manual",
        choices=["periodic", "shutdown", "manual"],
        help="Reason for capture",
    )
    capture_parser.add_argument(
        "--force", action="store_true", help="Ignore minimum interval"
    )
    capture_parser.add_argument(
        "--include-slow",
        action="store_true",
        help="Include slow data like goal priorities",
    )

    # Restore command
    subparsers.add_parser("restore", help="Restore from latest snapshot")

    # List command
    subparsers.add_parser("list", help="List available snapshots")

    # Show command
    show_parser = subparsers.add_parser("show", help="Show snapshot details")
    show_parser.add_argument("--snapshot", help="Snapshot filename")

    # Cleanup command
    subparsers.add_parser("cleanup", help="Remove old snapshots")

    # Reset command
    subparsers.add_parser("reset", help="Remove all snapshots")

    # Help command
    subparsers.add_parser("help", help="Show help")

    args = parser.parse_args()

    if args.command == "help" or args.command is None:
        parser.print_help()
        return

    continuity = SessionContinuity()

    if args.command == "capture":
        snapshot = continuity.capture_snapshot(
            reason=args.reason,
            force=args.force,
            include_slow_data=args.include_slow,
        )
        if snapshot:
            print(json.dumps(get_recovery_summary(snapshot), indent=2))
        else:
            print("Snapshot skipped (too soon since last capture)")

    elif args.command == "restore":
        snapshot = continuity.restore_snapshot()
        if snapshot:
            summary = get_recovery_summary(snapshot)
            print(json.dumps(summary, indent=2))

            # Handle partial work
            if snapshot.in_progress_tasks:
                print("\nRecovering in-progress tasks...")
                results = continuity.handle_partial_work(snapshot)
                print(json.dumps(results, indent=2))
        else:
            print("No valid snapshots found - fresh start")

    elif args.command == "list":
        snapshots = continuity._list_snapshots()
        if not snapshots:
            print("No snapshots found")
        else:
            print(f"Found {len(snapshots)} snapshots:\n")
            for snapshot_path in snapshots:
                try:
                    stat = snapshot_path.stat()
                    modified = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    print(f"  {snapshot_path.name}")
                    print(f"    Modified: {modified} UTC")
                    print(f"    Size: {stat.st_size:,} bytes")
                    print()
                except OSError:
                    print(f"  {snapshot_path.name} (error reading)")

    elif args.command == "show":
        if args.snapshot:
            snapshot_path = _get_snapshot_dir() / args.snapshot
        else:
            snapshots = continuity._list_snapshots()
            if not snapshots:
                print("No snapshots found")
                return
            snapshot_path = snapshots[0]

        try:
            with open(snapshot_path, encoding="utf-8") as f:
                data = json.load(f)
            print(json.dumps(data, indent=2))
        except FileNotFoundError:
            print(f"Snapshot not found: {snapshot_path}")
        except json.JSONDecodeError as e:
            print(f"Corrupted snapshot: {e}")

    elif args.command == "cleanup":
        removed = continuity.cleanup_old_snapshots()
        print(f"Removed {removed} old snapshots")

    elif args.command == "reset":
        removed = continuity.reset()
        print(f"Reset complete: removed {removed} snapshots")


if __name__ == "__main__":
    main()
