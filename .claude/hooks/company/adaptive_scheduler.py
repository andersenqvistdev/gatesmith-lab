#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P25 Full Autonomous Operation — Adaptive Scheduler.

Self-adjusting daemon behavior based on context. Determines HOW the daemon
operates (poll intervals, batch sizes, execution strategies) based on
queue state, budget utilization, and goal urgency.

Schedule Modes:
    AGGRESSIVE  - High activity: short polls (15s), large batches (5)
    NORMAL      - Standard operation: moderate polls (30s), standard batches (3)
    CONSERVATIVE - Resource-aware: longer polls (60s), small batches (2)
    IDLE        - Minimal activity: long polls (300s), single tasks (1)

Mode Selection Logic:
    - Queue depth > 10 pending + budget NORMAL/CAUTIOUS + urgent goals -> AGGRESSIVE
    - Queue depth 3-10 pending + budget NORMAL -> NORMAL
    - Queue depth < 3 or budget CAUTIOUS -> CONSERVATIVE
    - Queue empty or budget MINIMAL/PAUSED -> IDLE

Integration:
    The daemon main loop should call AdaptiveScheduler.compute_mode() at
    each iteration to get the current mode and adjust behavior accordingly.

Configuration (gatesmith.json):
    "adaptiveScheduler": {
        "enabled": true,
        "pollIntervals": {
            "AGGRESSIVE": 15,
            "NORMAL": 30,
            "CONSERVATIVE": 60,
            "IDLE": 300
        },
        "batchSizes": {
            "AGGRESSIVE": 5,
            "NORMAL": 3,
            "CONSERVATIVE": 2,
            "IDLE": 1
        },
        "queueThresholds": {
            "high": 10,
            "low": 3
        },
        "urgencyBoostEnabled": true,
        "logModeTransitions": true,
        "workerCounts": {
            "AGGRESSIVE": 5,
            "NORMAL": 5,
            "CONSERVATIVE": 3,
            "IDLE": 1
        },
        "maxParallelWorkers": 5
    }

Usage:
    from adaptive_scheduler import AdaptiveScheduler, ScheduleMode
    scheduler = AdaptiveScheduler()
    mode = scheduler.compute_mode()
    interval = scheduler.get_poll_interval()
    batch_size = scheduler.get_execution_strategy().batch_size
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from . import governing_config as _governing_config
except ImportError:  # direct script execution
    import governing_config as _governing_config  # type: ignore[no-redef]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("adaptive_scheduler")

# Lazy imports for sibling modules
_goal_scheduler = None
_budget_governor = None
_company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global _goal_scheduler, _budget_governor, _company_resolver

    if _goal_scheduler is not None:
        return

    try:
        from . import budget_governor as bg
        from . import company_resolver as cr
        from . import goal_scheduler as gs

        _goal_scheduler = gs
        _budget_governor = bg
        _company_resolver = cr
    except ImportError:
        import budget_governor as bg  # type: ignore[no-redef]
        import company_resolver as cr  # type: ignore[no-redef]
        import goal_scheduler as gs  # type: ignore[no-redef]

        _goal_scheduler = gs
        _budget_governor = bg
        _company_resolver = cr


# =============================================================================
# Constants and Defaults
# =============================================================================

WORK_QUEUE_FILE = "state/work_queue.json"
SCHEDULER_STATE_FILE = "adaptive_scheduler_state.json"
FORGE_CONFIG_FILE = "gatesmith.json"

# Default poll intervals in seconds per mode
DEFAULT_POLL_INTERVALS = {
    "AGGRESSIVE": 15,
    "NORMAL": 30,
    "CONSERVATIVE": 60,
    "IDLE": 300,
}

# Default batch sizes (tasks per execution cycle) per mode
DEFAULT_BATCH_SIZES = {
    "AGGRESSIVE": 5,
    "NORMAL": 3,
    "CONSERVATIVE": 2,
    "IDLE": 1,
}

# Default worker counts (parallel workers) per mode
MAX_SAFE_WORKERS = 5  # WS-057-002: Absolute cap to prevent resource exhaustion

DEFAULT_WORKER_COUNTS = {
    "AGGRESSIVE": 3,
    "NORMAL": 2,
    "CONSERVATIVE": 2,
    "IDLE": 1,
}

# Default queue thresholds for mode selection
DEFAULT_QUEUE_HIGH_THRESHOLD = 10
DEFAULT_QUEUE_LOW_THRESHOLD = 3

# Goal urgency threshold - priorities above this trigger urgency boost
DEFAULT_URGENCY_THRESHOLD = 1.5

# Ceiling for the IDLE poll backoff (seconds). Configurable via
# adaptiveScheduler.maxIdlePollSeconds; this is the default when unset.
DEFAULT_MAX_IDLE_POLL_SECONDS = 1800

# Hard upper bound on the configured IDLE poll cap, regardless of config value.
MAX_IDLE_POLL_SECONDS_CEILING = 3600

# P36: Business hours configuration (time-of-day awareness)
# During business hours: prefer NORMAL/AGGRESSIVE for faster review cycles
# Outside business hours: prefer CONSERVATIVE/IDLE to reduce wasted cycles
DEFAULT_BUSINESS_HOURS_START = 9  # 9 AM local time
DEFAULT_BUSINESS_HOURS_END = 18  # 6 PM local time
DEFAULT_BUSINESS_DAYS = [0, 1, 2, 3, 4]  # Monday=0 through Friday=4

# Hysteresis: require N consecutive downgrade readings before actually downgrading.
# Prevents mode flapping when metrics hover near thresholds.
DOWNGRADE_HYSTERESIS_CYCLES = 3


# =============================================================================
# Schedule Mode Enum
# =============================================================================


class ScheduleMode(Enum):
    """Daemon operation modes with different resource profiles."""

    AGGRESSIVE = "AGGRESSIVE"  # High activity: short polls, large batches
    NORMAL = "NORMAL"  # Standard operation
    CONSERVATIVE = "CONSERVATIVE"  # Resource-aware: longer polls, small batches
    IDLE = "IDLE"  # Minimal activity: long polls, single tasks

    def __str__(self) -> str:
        return self.value

    @property
    def description(self) -> str:
        """Human-readable description of the mode."""
        descriptions = {
            ScheduleMode.AGGRESSIVE: "High activity mode - short polls, large batches",
            ScheduleMode.NORMAL: "Standard operation - balanced throughput",
            ScheduleMode.CONSERVATIVE: "Resource-aware - longer polls, small batches",
            ScheduleMode.IDLE: "Minimal activity - long polls, single tasks",
        }
        return descriptions.get(self, "Unknown mode")


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ExecutionStrategy:
    """Execution parameters for a given mode."""

    mode: ScheduleMode
    poll_interval_seconds: int
    batch_size: int
    allow_complex_tasks: bool
    allow_parallel_execution: bool
    max_retries: int
    worker_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "mode": self.mode.value,
            "poll_interval_seconds": self.poll_interval_seconds,
            "batch_size": self.batch_size,
            "allow_complex_tasks": self.allow_complex_tasks,
            "allow_parallel_execution": self.allow_parallel_execution,
            "max_retries": self.max_retries,
            "worker_count": self.worker_count,
        }


@dataclass
class SchedulerState:
    """Current state of the adaptive scheduler."""

    current_mode: ScheduleMode
    previous_mode: ScheduleMode | None
    mode_since: str  # ISO timestamp
    transitions_count: int
    last_queue_depth: int
    last_budget_level: str
    last_goal_urgency: float
    # P36: Exponential backoff tracking for extended IDLE periods
    consecutive_idle_cycles: int = 0
    last_activity_time: str = ""  # ISO timestamp of last non-idle activity
    wasted_polls_count: int = 0  # Polls that found nothing actionable
    # P36: Business hours awareness
    is_business_hours: bool = True
    business_hours_mode_applied: bool = False
    # P36: Ideation efficiency metrics
    ideation_cycles_total: int = 0
    ideation_cycles_skipped: int = 0  # Skipped due to no completions
    ideation_ideas_generated: int = 0
    # Hysteresis: suppress downgrades until N consecutive readings confirm
    consecutive_downgrade_readings: int = 0
    pending_downgrade_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "current_mode": self.current_mode.value,
            "previous_mode": self.previous_mode.value if self.previous_mode else None,
            "mode_since": self.mode_since,
            "transitions_count": self.transitions_count,
            "last_queue_depth": self.last_queue_depth,
            "last_budget_level": self.last_budget_level,
            "last_goal_urgency": self.last_goal_urgency,
            "consecutive_idle_cycles": self.consecutive_idle_cycles,
            "last_activity_time": self.last_activity_time,
            "wasted_polls_count": self.wasted_polls_count,
            # P36: Business hours and ideation metrics
            "is_business_hours": self.is_business_hours,
            "business_hours_mode_applied": self.business_hours_mode_applied,
            "ideation_cycles_total": self.ideation_cycles_total,
            "ideation_cycles_skipped": self.ideation_cycles_skipped,
            "ideation_ideas_generated": self.ideation_ideas_generated,
            "consecutive_downgrade_readings": self.consecutive_downgrade_readings,
            "pending_downgrade_mode": self.pending_downgrade_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchedulerState":
        """Create from dictionary."""
        return cls(
            current_mode=ScheduleMode(data.get("current_mode", "NORMAL")),
            previous_mode=(
                ScheduleMode(data["previous_mode"])
                if data.get("previous_mode")
                else None
            ),
            mode_since=data.get("mode_since", datetime.now(timezone.utc).isoformat()),
            transitions_count=data.get("transitions_count", 0),
            last_queue_depth=data.get("last_queue_depth", 0),
            last_budget_level=data.get("last_budget_level", "normal"),
            last_goal_urgency=data.get("last_goal_urgency", 0.0),
            consecutive_idle_cycles=data.get("consecutive_idle_cycles", 0),
            last_activity_time=data.get("last_activity_time", ""),
            wasted_polls_count=data.get("wasted_polls_count", 0),
            # P36: Business hours and ideation metrics
            is_business_hours=data.get("is_business_hours", True),
            business_hours_mode_applied=data.get("business_hours_mode_applied", False),
            ideation_cycles_total=data.get("ideation_cycles_total", 0),
            ideation_cycles_skipped=data.get("ideation_cycles_skipped", 0),
            ideation_ideas_generated=data.get("ideation_ideas_generated", 0),
            consecutive_downgrade_readings=data.get(
                "consecutive_downgrade_readings", 0
            ),
            pending_downgrade_mode=data.get("pending_downgrade_mode"),
        )


@dataclass
class ModeTransition:
    """Record of a mode transition for logging."""

    timestamp: str
    from_mode: ScheduleMode
    to_mode: ScheduleMode
    reason: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason": self.reason,
            "context": self.context,
        }


# =============================================================================
# Configuration Loading
# =============================================================================


def load_config() -> dict[str, Any]:
    """
    Load adaptive scheduler configuration from gatesmith.json.

    Returns:
        Configuration dictionary with all scheduler settings.
    """
    _ensure_imports()

    defaults = {
        "enabled": True,
        "pollIntervals": DEFAULT_POLL_INTERVALS.copy(),
        "batchSizes": DEFAULT_BATCH_SIZES.copy(),
        "queueThresholds": {
            "high": DEFAULT_QUEUE_HIGH_THRESHOLD,
            "low": DEFAULT_QUEUE_LOW_THRESHOLD,
        },
        "urgencyBoostEnabled": True,
        "urgencyThreshold": DEFAULT_URGENCY_THRESHOLD,
        "logModeTransitions": True,
        "maxIdlePollSeconds": DEFAULT_MAX_IDLE_POLL_SECONDS,
    }

    # The adaptiveScheduler block comes from its governing file: the root
    # gatesmith.json when it declares the block, else the install template.
    # An unreadable governing file keeps the defaults.
    project_root = Path.cwd()
    try:
        project_root = _company_resolver.get_company_dir().parent
    except Exception:
        pass

    scheduler_config, error = _governing_config.section(
        "adaptiveScheduler", project_root
    )
    if error:
        return defaults

    # Merge with defaults
    result = defaults.copy()
    result["enabled"] = scheduler_config.get("enabled", defaults["enabled"])
    result["pollIntervals"] = {
        **defaults["pollIntervals"],
        **scheduler_config.get("pollIntervals", {}),
    }
    result["batchSizes"] = {
        **defaults["batchSizes"],
        **scheduler_config.get("batchSizes", {}),
    }
    result["queueThresholds"] = {
        **defaults["queueThresholds"],
        **scheduler_config.get("queueThresholds", {}),
    }
    result["urgencyBoostEnabled"] = scheduler_config.get(
        "urgencyBoostEnabled", defaults["urgencyBoostEnabled"]
    )
    result["urgencyThreshold"] = scheduler_config.get(
        "urgencyThreshold", defaults["urgencyThreshold"]
    )
    result["logModeTransitions"] = scheduler_config.get(
        "logModeTransitions", defaults["logModeTransitions"]
    )
    # WS-057: Worker counts and max parallel workers
    if "workerCounts" in scheduler_config:
        result["workerCounts"] = {
            **defaults.get("workerCounts", DEFAULT_WORKER_COUNTS),
            **scheduler_config["workerCounts"],
        }
    if "maxParallelWorkers" in scheduler_config:
        result["maxParallelWorkers"] = scheduler_config["maxParallelWorkers"]
    if "maxIdlePollSeconds" in scheduler_config:
        result["maxIdlePollSeconds"] = scheduler_config["maxIdlePollSeconds"]
    return result


# =============================================================================
# Work Queue Analysis
# =============================================================================


def load_work_queue() -> dict[str, Any]:
    """Load the work queue from file."""
    _ensure_imports()
    company_dir = _company_resolver.get_company_dir()
    queue_path = company_dir / WORK_QUEUE_FILE

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


def get_queue_depth() -> int:
    """
    Get the current queue depth (pending + in_progress tasks).

    Returns:
        Total count of actionable tasks.
    """
    queue = load_work_queue()
    pending_count = len(queue.get("pending", []))
    in_progress_count = len(queue.get("in_progress", []))
    return pending_count + in_progress_count


def get_max_goal_urgency() -> float:
    """
    Get the maximum urgency score from goal priorities.

    Uses cached value from strategic_state.json which is updated by
    the strategic planner. This avoids blocking on expensive goal
    assessment operations (which run pytest).

    For real-time urgency, the daemon should periodically refresh
    strategic_state.json via strategic_planner.py.

    Returns:
        Highest priority score among all goals (0.0 if no data or error).
    """
    _ensure_imports()

    # Read from strategic_state.json (updated by strategic planner)
    # This avoids the expensive goal_tracker.assess_all_goals() call
    try:
        company_dir = _company_resolver.get_company_dir()
        state_path = company_dir / "state/strategic_state.json"

        if not state_path.exists():
            return 0.0

        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

        # Extract max urgency from goal assessments
        goal_progress = state.get("goal_progress", {})
        if not goal_progress:
            return 0.0

        # Calculate urgency from gap and status
        max_urgency = 0.0
        for goal_id, goal_data in goal_progress.items():
            current = goal_data.get("current", 0.0)
            target = goal_data.get("target", 1.0)
            status = goal_data.get("status", "")

            # Higher gap = higher urgency
            gap = max(0.0, target - current)

            # Status-based multiplier
            if status == "blocked":
                multiplier = 2.0
            elif status == "at_risk":
                multiplier = 1.5
            else:
                multiplier = 1.0

            urgency = gap * multiplier
            max_urgency = max(max_urgency, urgency)

        return max_urgency

    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.debug(f"Could not read goal urgency from strategic state: {e}")
        return 0.0


# =============================================================================
# Adaptive Scheduler
# =============================================================================


class AdaptiveScheduler:
    """
    Self-adjusting scheduler that adapts daemon behavior based on context.

    The scheduler monitors queue depth, budget status, and goal urgency
    to determine the optimal operation mode. Mode transitions are logged
    for observability and debugging.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the adaptive scheduler.

        Args:
            config: Optional configuration dictionary. If not provided,
                   loads from gatesmith.json adaptiveScheduler section.
        """
        _ensure_imports()

        if config is None:
            config = load_config()

        self.config = config
        self.enabled = config.get("enabled", True)
        self.poll_intervals = config.get("pollIntervals", DEFAULT_POLL_INTERVALS)
        self.batch_sizes = config.get("batchSizes", DEFAULT_BATCH_SIZES)
        self.worker_counts = config.get("workerCounts", DEFAULT_WORKER_COUNTS)
        self.queue_high = config.get("queueThresholds", {}).get(
            "high", DEFAULT_QUEUE_HIGH_THRESHOLD
        )
        self.queue_low = config.get("queueThresholds", {}).get(
            "low", DEFAULT_QUEUE_LOW_THRESHOLD
        )
        # Validate: queue_low must not exceed queue_high
        if self.queue_low > self.queue_high:
            self.queue_high = DEFAULT_QUEUE_HIGH_THRESHOLD
            self.queue_low = DEFAULT_QUEUE_LOW_THRESHOLD
        self.urgency_boost_enabled = config.get("urgencyBoostEnabled", True)
        self.urgency_threshold = config.get(
            "urgencyThreshold", DEFAULT_URGENCY_THRESHOLD
        )
        self.log_transitions = config.get("logModeTransitions", True)

        raw_max_idle_poll = config.get(
            "maxIdlePollSeconds", DEFAULT_MAX_IDLE_POLL_SECONDS
        )
        if (
            isinstance(raw_max_idle_poll, bool)
            or not isinstance(raw_max_idle_poll, int)
            or raw_max_idle_poll < 0
        ):
            logger.warning(
                f"Invalid adaptiveScheduler.maxIdlePollSeconds={raw_max_idle_poll!r}; "
                f"falling back to default {DEFAULT_MAX_IDLE_POLL_SECONDS}"
            )
            raw_max_idle_poll = DEFAULT_MAX_IDLE_POLL_SECONDS
        self.max_idle_poll_seconds = raw_max_idle_poll

        # Load persisted state
        self._state = self._load_state()

    def _get_state_path(self) -> Path:
        """Get the path to the scheduler state file."""
        company_dir = _company_resolver.get_company_dir()
        return company_dir / SCHEDULER_STATE_FILE

    def _load_state(self) -> SchedulerState:
        """Load scheduler state from file."""
        try:
            state_path = self._get_state_path()
            if state_path.exists():
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)
                return SchedulerState.from_dict(data)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.debug(f"Could not load scheduler state: {e}")

        # Return default state
        return SchedulerState(
            current_mode=ScheduleMode.NORMAL,
            previous_mode=None,
            mode_since=datetime.now(timezone.utc).isoformat(),
            transitions_count=0,
            last_queue_depth=0,
            last_budget_level="normal",
            last_goal_urgency=0.0,
        )

    def _save_state(self) -> None:
        """Save scheduler state to file with atomic write."""
        import os
        import tempfile

        state_path = self._get_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="scheduler_state_", dir=str(state_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2)
            os.replace(tmp_path, str(state_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _get_budget_throttle_level(self) -> str:
        """Get current budget throttle level as string."""
        try:
            # Check if budget governor is disabled in config
            config_path = Path(__file__).resolve().parent.parent / "gatesmith.json"
            if config_path.exists():
                import json as _json

                with open(config_path, encoding="utf-8") as _f:
                    _cfg = _json.load(_f)
                fa = _cfg.get("fullAutonomy", {})
                if not fa.get("budgetGovernorEnabled", True):
                    return "normal"
                bg_cfg = fa.get("budgetGovernor", {})
                if not bg_cfg.get("enabled", True):
                    return "normal"
            governor = _budget_governor.BudgetGovernor()
            level = governor.compute_throttle_level()
            return level.value
        except Exception as e:
            logger.debug(f"Could not get budget level: {e}")
            return "normal"

    def _is_business_hours(self) -> bool:
        """
        Check if current time is within business hours.

        P36: Business hours awareness allows the scheduler to:
        - Prefer NORMAL/AGGRESSIVE during business hours (faster PR review cycles)
        - Prefer CONSERVATIVE/IDLE outside business hours (reduce wasted cycles)

        Returns:
            True if within business hours, False otherwise.
        """
        now = datetime.now()  # Local time
        current_hour = now.hour
        current_day = now.weekday()  # Monday=0, Sunday=6

        # Check if it's a business day
        if current_day not in DEFAULT_BUSINESS_DAYS:
            return False

        # Check if within business hours
        return DEFAULT_BUSINESS_HOURS_START <= current_hour < DEFAULT_BUSINESS_HOURS_END

    def compute_mode(self) -> ScheduleMode:
        """
        Compute the optimal schedule mode based on current context.

        Mode selection logic:
        1. If queue empty or budget PAUSED -> IDLE
        2. If budget MINIMAL -> IDLE (preserve resources)
        3. If queue depth > high threshold + budget OK + urgent goals -> AGGRESSIVE
        4. If queue depth > high threshold -> NORMAL (not aggressive without urgency)
        5. If queue depth > low threshold -> NORMAL
        6. Otherwise -> CONSERVATIVE

        The urgency boost (if enabled) can promote NORMAL to AGGRESSIVE when
        there are high-urgency goals requiring attention.

        Returns:
            The computed ScheduleMode.
        """
        if not self.enabled:
            return ScheduleMode.NORMAL

        # Gather context
        queue_depth = get_queue_depth()
        budget_level = self._get_budget_throttle_level()
        goal_urgency = get_max_goal_urgency() if self.urgency_boost_enabled else 0.0
        is_business_hours = self._is_business_hours()

        # Update state with latest context
        self._state.last_queue_depth = queue_depth
        self._state.last_budget_level = budget_level
        self._state.last_goal_urgency = goal_urgency
        self._state.is_business_hours = is_business_hours

        # Decision logic
        new_mode: ScheduleMode
        business_hours_adjusted = False

        # Budget constraints take precedence
        if budget_level == "paused":
            new_mode = ScheduleMode.IDLE
            reason = "Budget paused - entering IDLE mode"
        elif budget_level == "minimal":
            new_mode = ScheduleMode.IDLE
            reason = "Budget minimal - entering IDLE mode to preserve resources"
        elif queue_depth == 0:
            new_mode = ScheduleMode.IDLE
            reason = "Queue empty - entering IDLE mode"
        elif queue_depth > self.queue_high:
            # High queue depth - check for urgency boost
            if (
                self.urgency_boost_enabled
                and goal_urgency >= self.urgency_threshold
                and budget_level == "normal"
            ):
                new_mode = ScheduleMode.AGGRESSIVE
                reason = (
                    f"High queue ({queue_depth}) + urgent goals ({goal_urgency:.2f}) "
                    "- entering AGGRESSIVE mode"
                )
            else:
                new_mode = ScheduleMode.NORMAL
                reason = f"High queue ({queue_depth}) - entering NORMAL mode"
        elif queue_depth >= self.queue_low:
            # Moderate queue depth
            if budget_level == "cautious":
                new_mode = ScheduleMode.CONSERVATIVE
                reason = (
                    f"Moderate queue ({queue_depth}) + budget cautious - CONSERVATIVE"
                )
            else:
                new_mode = ScheduleMode.NORMAL
                reason = f"Moderate queue ({queue_depth}) - NORMAL mode"
        else:
            # Low queue depth
            new_mode = ScheduleMode.CONSERVATIVE
            reason = f"Low queue ({queue_depth}) - CONSERVATIVE mode"

        # P2: Hysteresis — prevent mode flapping on oscillating queue depth.
        # Downgrades require queue_depth to drop >=2 below current threshold.
        # Upgrades apply immediately (no hysteresis penalty).
        current_mode = self._state.current_mode
        _mode_rank = {
            ScheduleMode.IDLE: 0,
            ScheduleMode.CONSERVATIVE: 1,
            ScheduleMode.NORMAL: 2,
            ScheduleMode.AGGRESSIVE: 3,
        }
        is_downgrade = _mode_rank.get(new_mode, 0) < _mode_rank.get(current_mode, 0)
        if is_downgrade and self._state.previous_mode is not None:
            _hysteresis = 2
            suppress = False

            if (
                current_mode == ScheduleMode.AGGRESSIVE
                and new_mode == ScheduleMode.NORMAL
                and queue_depth > self.queue_high - _hysteresis
            ):
                suppress = True
                _threshold = self.queue_high
            elif (
                current_mode == ScheduleMode.NORMAL
                and new_mode == ScheduleMode.CONSERVATIVE
                and queue_depth > self.queue_low - _hysteresis
            ):
                suppress = True
                _threshold = self.queue_low
            # IDLE from empty queue (queue_depth == 0) is never suppressed

            if suppress:
                reason = (
                    f"Hysteresis: staying in {current_mode.value} "
                    f"(queue={queue_depth}, need <={_threshold - _hysteresis})"
                )
                new_mode = current_mode

        # P36: Business hours adjustment (DISABLED for AI companies — WS-059)
        # AI companies operate 24/7. The daemon should maintain throughput regardless
        # of clock time. The original logic downgraded NORMAL to CONSERVATIVE at night,
        # reducing parallel workers to 1 — counterproductive for autonomous operation.
        # Keeping the code path for future configurability but not applying it.
        if (
            False
            and not is_business_hours
            and new_mode
            in (
                ScheduleMode.AGGRESSIVE,
                ScheduleMode.NORMAL,
            )
        ):
            if new_mode == ScheduleMode.AGGRESSIVE:
                new_mode = ScheduleMode.NORMAL
                reason += " [downgraded: outside business hours]"
                business_hours_adjusted = True
            elif new_mode == ScheduleMode.NORMAL and queue_depth < self.queue_high:
                new_mode = ScheduleMode.CONSERVATIVE
                reason += " [downgraded: outside business hours]"
                business_hours_adjusted = True

        self._state.business_hours_mode_applied = business_hours_adjusted

        # Hysteresis: suppress soft downgrades until N consecutive readings confirm.
        # Hard constraints (budget paused/minimal, queue empty) bypass hysteresis.
        # Upgrades (moving to a more active mode) are always immediate.
        _mode_rank = {
            ScheduleMode.IDLE: 0,
            ScheduleMode.CONSERVATIVE: 1,
            ScheduleMode.NORMAL: 2,
            ScheduleMode.AGGRESSIVE: 3,
        }
        current_rank = _mode_rank.get(self._state.current_mode, 2)
        new_rank = _mode_rank.get(new_mode, 2)
        is_hard_constraint = budget_level in ("paused", "minimal") or queue_depth == 0

        if new_rank < current_rank and not is_hard_constraint:
            # Soft downgrade requested — apply hysteresis
            self._state.consecutive_downgrade_readings += 1
            self._state.pending_downgrade_mode = new_mode.value
            if self._state.consecutive_downgrade_readings < DOWNGRADE_HYSTERESIS_CYCLES:
                # Not enough consecutive readings yet — suppress downgrade
                return self._state.current_mode
        else:
            # Upgrade, same mode, or hard constraint — reset hysteresis counters
            self._state.consecutive_downgrade_readings = 0
            self._state.pending_downgrade_mode = None

        # Handle mode transition
        if new_mode != self._state.current_mode:
            # Transition confirmed — reset counters
            self._state.consecutive_downgrade_readings = 0
            self._state.pending_downgrade_mode = None
            self._handle_mode_transition(new_mode, reason)

        return new_mode

    def _handle_mode_transition(self, new_mode: ScheduleMode, reason: str) -> None:
        """
        Handle a mode transition with logging and state update.

        Args:
            new_mode: The new mode to transition to.
            reason: Human-readable reason for the transition.
        """
        old_mode = self._state.current_mode
        now = datetime.now(timezone.utc).isoformat()

        # Create transition record
        transition = ModeTransition(
            timestamp=now,
            from_mode=old_mode,
            to_mode=new_mode,
            reason=reason,
            context={
                "queue_depth": self._state.last_queue_depth,
                "budget_level": self._state.last_budget_level,
                "goal_urgency": self._state.last_goal_urgency,
            },
        )

        # Update state
        self._state.previous_mode = old_mode
        self._state.current_mode = new_mode
        self._state.mode_since = now
        self._state.transitions_count += 1

        # Save state
        self._save_state()

        # Log transition
        if self.log_transitions:
            self._log_transition(transition)

    def _log_transition(self, transition: ModeTransition) -> None:
        """
        Log a mode transition to file and logger.

        Args:
            transition: The transition record to log.
        """
        # Log to standard logger
        logger.info(
            f"Mode transition: {transition.from_mode.value} -> {transition.to_mode.value} "
            f"({transition.reason})"
        )

        # Log to file
        try:
            company_dir = _company_resolver.get_company_dir()
            log_dir = company_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            log_file = log_dir / "adaptive_scheduler.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(transition.to_dict()) + "\n")
        except OSError as e:
            logger.warning(f"Could not write transition log: {e}")

    def get_poll_interval(self) -> int:
        """
        Get the poll interval in seconds for the current mode.

        P36 Enhancement: Applies exponential backoff when in IDLE mode for
        extended periods. This reduces wasted polling when no work is available.

        Backoff schedule (IDLE mode only):
        - 0-2 consecutive idle cycles: base interval (300s)
        - 3-5 cycles: 2x base (600s = 10 min)
        - 6-10 cycles: 4x base (1200s = 20 min)
        - 11+ cycles: 6x base, capped at adaptiveScheduler.maxIdlePollSeconds
          (default 1800s = 30 min; clamped to [base interval, 3600s])

        Returns:
            Poll interval in seconds.
        """
        mode = self._state.current_mode
        base_interval = self.poll_intervals.get(
            mode.value, DEFAULT_POLL_INTERVALS["NORMAL"]
        )

        # P36: Apply exponential backoff only in IDLE mode
        if mode == ScheduleMode.IDLE:
            idle_cycles = self._state.consecutive_idle_cycles

            if idle_cycles <= 2:
                multiplier = 1
            elif idle_cycles <= 5:
                multiplier = 2
            elif idle_cycles <= 10:
                multiplier = 4
            else:
                multiplier = 6  # Cap at 6x (30 min max)

            backoff_interval = base_interval * multiplier

            # Cap at the configured ceiling, clamped to [base_interval, 3600s]
            max_interval = max(
                base_interval,
                min(self.max_idle_poll_seconds, MAX_IDLE_POLL_SECONDS_CEILING),
            )
            final_interval = min(backoff_interval, max_interval)

            if multiplier > 1:
                logger.debug(
                    f"P36: IDLE backoff applied - {idle_cycles} consecutive cycles, "
                    f"{multiplier}x multiplier, interval={final_interval}s"
                )

            return final_interval

        return base_interval

    def get_batch_size(self) -> int:
        """
        Get the batch size (tasks per cycle) for the current mode.

        Returns:
            Number of tasks to process per execution cycle.
        """
        mode = self._state.current_mode
        return self.batch_sizes.get(mode.value, DEFAULT_BATCH_SIZES["NORMAL"])

    def get_worker_count(self) -> int:
        """
        Get the optimal number of parallel workers for the current mode.

        Worker counts by mode (defaults; see adaptiveScheduler.workerCounts in gatesmith.json):
            AGGRESSIVE:  5 workers (maximum throughput)
            NORMAL:      5 workers (balanced)
            CONSERVATIVE: 3 workers (parallel-capable)
            IDLE:        1 worker (minimal)

        Respects the maxParallelWorkers cap from gatesmith.json if set.

        Returns:
            Number of parallel workers to use.
        """
        mode = self._state.current_mode
        count = self.worker_counts.get(mode.value, DEFAULT_WORKER_COUNTS["NORMAL"])
        max_workers = self.config.get("maxParallelWorkers")
        if max_workers is not None:
            count = min(count, int(max_workers))
        # Hard safety ceiling — prevent runaway worker spawning from bad config
        count = min(count, 10)
        return max(1, count)

    def get_execution_strategy(self) -> ExecutionStrategy:
        """
        Get the complete execution strategy for the current mode.

        Returns:
            ExecutionStrategy with all parameters for the current mode.
        """
        mode = self._state.current_mode

        # Determine mode-specific settings
        if mode == ScheduleMode.AGGRESSIVE:
            allow_complex = True
            allow_parallel = True
            max_retries = 3
        elif mode == ScheduleMode.NORMAL:
            allow_complex = True
            allow_parallel = True
            max_retries = 2
        elif mode == ScheduleMode.CONSERVATIVE:
            allow_complex = False  # Defer complex tasks
            allow_parallel = True  # WS-057-002: Allow parallel with 2 workers
            max_retries = 1
        else:  # IDLE
            allow_complex = False
            allow_parallel = False
            max_retries = 1

        return ExecutionStrategy(
            mode=mode,
            poll_interval_seconds=self.get_poll_interval(),
            batch_size=self.get_batch_size(),
            allow_complex_tasks=allow_complex,
            allow_parallel_execution=allow_parallel,
            max_retries=max_retries,
            worker_count=self.get_worker_count(),
        )

    def get_current_mode(self) -> ScheduleMode:
        """
        Get the current schedule mode without recomputing.

        Use compute_mode() to get a fresh computation based on current context.

        Returns:
            The current ScheduleMode.
        """
        return self._state.current_mode

    def get_state(self) -> SchedulerState:
        """
        Get the current scheduler state.

        Returns:
            SchedulerState with all current metrics.
        """
        return self._state

    def get_status_dict(self) -> dict[str, Any]:
        """
        Get complete scheduler status as a dictionary.

        Returns:
            Dictionary with mode, strategy, and state information.
        """
        strategy = self.get_execution_strategy()
        return {
            "enabled": self.enabled,
            "current_mode": self._state.current_mode.value,
            "mode_description": self._state.current_mode.description,
            "mode_since": self._state.mode_since,
            "previous_mode": (
                self._state.previous_mode.value if self._state.previous_mode else None
            ),
            "transitions_count": self._state.transitions_count,
            "strategy": strategy.to_dict(),
            "context": {
                "queue_depth": self._state.last_queue_depth,
                "budget_level": self._state.last_budget_level,
                "goal_urgency": self._state.last_goal_urgency,
            },
            "thresholds": {
                "queue_high": self.queue_high,
                "queue_low": self.queue_low,
                "urgency_threshold": self.urgency_threshold,
            },
        }

    def record_idle_cycle(self) -> None:
        """
        Record an idle cycle (no work was found/executed).

        P36: Increments the consecutive idle counter for backoff calculation.
        """
        self._state.consecutive_idle_cycles += 1
        self._state.wasted_polls_count += 1
        self._save_state()
        logger.debug(
            f"P36: Idle cycle recorded - {self._state.consecutive_idle_cycles} consecutive"
        )

    def record_activity(self) -> None:
        """
        Record that work was executed (resets idle counter).

        P36: Resets the consecutive idle counter when work is found.
        """
        if self._state.consecutive_idle_cycles > 0:
            logger.debug(
                f"P36: Activity recorded - resetting idle counter from "
                f"{self._state.consecutive_idle_cycles}"
            )
        self._state.consecutive_idle_cycles = 0
        self._state.last_activity_time = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def get_backoff_status(self) -> dict[str, Any]:
        """
        Get current backoff status for monitoring.

        Returns:
            Dict with backoff metrics.
        """
        return {
            "consecutive_idle_cycles": self._state.consecutive_idle_cycles,
            "wasted_polls_count": self._state.wasted_polls_count,
            "last_activity_time": self._state.last_activity_time,
            "current_backoff_multiplier": self._get_backoff_multiplier(),
            "effective_poll_interval": self.get_poll_interval(),
        }

    def _get_backoff_multiplier(self) -> int:
        """Get the current backoff multiplier based on idle cycles."""
        idle_cycles = self._state.consecutive_idle_cycles
        if idle_cycles <= 2:
            return 1
        elif idle_cycles <= 5:
            return 2
        elif idle_cycles <= 10:
            return 4
        else:
            return 6

    def record_ideation_cycle(
        self, ideas_generated: int, skipped: bool = False
    ) -> None:
        """
        Record an ideation cycle for efficiency tracking.

        P36: Tracks ideation efficiency to measure the value of ideation cycles.

        Args:
            ideas_generated: Number of ideas generated (0 if skipped).
            skipped: True if cycle was skipped due to no recent completions.
        """
        self._state.ideation_cycles_total += 1
        if skipped:
            self._state.ideation_cycles_skipped += 1
        else:
            self._state.ideation_ideas_generated += ideas_generated
        self._save_state()

        if skipped:
            logger.debug("P36: Ideation cycle skipped (no completions)")
        else:
            logger.debug(f"P36: Ideation cycle completed - {ideas_generated} ideas")

    def get_ideation_efficiency(self) -> dict[str, Any]:
        """
        Get ideation efficiency metrics.

        Returns:
            Dict with ideation efficiency metrics.
        """
        total = self._state.ideation_cycles_total
        skipped = self._state.ideation_cycles_skipped
        executed = total - skipped
        ideas = self._state.ideation_ideas_generated

        # Calculate efficiency metrics
        skip_rate = (skipped / total * 100) if total > 0 else 0.0
        ideas_per_cycle = (ideas / executed) if executed > 0 else 0.0

        return {
            "total_cycles": total,
            "executed_cycles": executed,
            "skipped_cycles": skipped,
            "skip_rate_percent": round(skip_rate, 1),
            "total_ideas_generated": ideas,
            "ideas_per_executed_cycle": round(ideas_per_cycle, 2),
        }

    def should_execute_task(self, task: dict) -> tuple[bool, str]:
        """
        Check if a task should be executed under current mode.

        This is an additional filter on top of budget_governor checks.
        In CONSERVATIVE/IDLE modes, complex tasks may be deferred.

        Args:
            task: Task dictionary with complexity field.

        Returns:
            Tuple of (allowed, reason).
        """
        mode = self._state.current_mode
        strategy = self.get_execution_strategy()
        complexity = task.get(
            "complexity", task.get("estimated_complexity", "standard")
        )

        # In AGGRESSIVE or NORMAL, allow everything
        if mode in (ScheduleMode.AGGRESSIVE, ScheduleMode.NORMAL):
            return True, f"Mode {mode.value} allows all tasks"

        # In CONSERVATIVE or IDLE, check complexity
        if complexity in ("complex", "epic"):
            if not strategy.allow_complex_tasks:
                return (
                    False,
                    f"Mode {mode.value} defers complex/epic tasks",
                )

        return True, f"Mode {mode.value} allows task"


# =============================================================================
# Convenience Functions
# =============================================================================


def get_adaptive_scheduler(config: dict[str, Any] | None = None) -> AdaptiveScheduler:
    """
    Get an AdaptiveScheduler instance.

    Args:
        config: Optional configuration dictionary.

    Returns:
        AdaptiveScheduler instance.
    """
    return AdaptiveScheduler(config)


def compute_schedule_mode(config: dict[str, Any] | None = None) -> ScheduleMode:
    """
    Convenience function to compute current schedule mode.

    Args:
        config: Optional configuration.

    Returns:
        Current ScheduleMode.
    """
    scheduler = AdaptiveScheduler(config)
    return scheduler.compute_mode()


def get_poll_interval(config: dict[str, Any] | None = None) -> int:
    """
    Convenience function to get current poll interval.

    Args:
        config: Optional configuration.

    Returns:
        Poll interval in seconds.
    """
    scheduler = AdaptiveScheduler(config)
    scheduler.compute_mode()  # Update mode first
    return scheduler.get_poll_interval()


def get_execution_strategy(config: dict[str, Any] | None = None) -> ExecutionStrategy:
    """
    Convenience function to get current execution strategy.

    Args:
        config: Optional configuration.

    Returns:
        ExecutionStrategy for current mode.
    """
    scheduler = AdaptiveScheduler(config)
    scheduler.compute_mode()  # Update mode first
    return scheduler.get_execution_strategy()


# =============================================================================
# CLI Interface
# =============================================================================


def print_help():
    """Print help information."""
    help_text = """
P25 Adaptive Scheduler — Self-adjusting daemon behavior

Commands:
    status            Get current scheduler status and mode
    mode              Compute and show current mode
    strategy          Get execution strategy for current mode
    interval          Get poll interval for current mode
    help              Show this help

Examples:
    # Get current scheduler status
    python adaptive_scheduler.py status

    # Compute and show current mode
    python adaptive_scheduler.py mode

    # Get execution strategy
    python adaptive_scheduler.py strategy

    # Get poll interval
    python adaptive_scheduler.py interval

Schedule Modes:
    AGGRESSIVE   - High activity: 15s polls, batch of 5
    NORMAL       - Standard: 30s polls, batch of 3
    CONSERVATIVE - Resource-aware: 60s polls, batch of 2
    IDLE         - Minimal: 300s polls, single tasks

Configuration (gatesmith.json):
    "adaptiveScheduler": {
        "enabled": true,
        "pollIntervals": {"AGGRESSIVE": 15, "NORMAL": 30, ...},
        "batchSizes": {"AGGRESSIVE": 5, "NORMAL": 3, ...},
        "queueThresholds": {"high": 10, "low": 3},
        "urgencyBoostEnabled": true,
        "logModeTransitions": true
    }
"""
    print(help_text)


def main():
    """Main CLI entry point."""
    _ensure_imports()

    args = sys.argv[1:]

    if not args or args[0] in ("help", "--help", "-h"):
        print_help()
        return

    command = args[0]
    result: dict[str, Any] = {}

    try:
        scheduler = AdaptiveScheduler()

        if command == "status":
            # Compute mode to get fresh state
            scheduler.compute_mode()
            result = scheduler.get_status_dict()

        elif command == "mode":
            mode = scheduler.compute_mode()
            result = {
                "mode": mode.value,
                "description": mode.description,
                "poll_interval": scheduler.get_poll_interval(),
                "batch_size": scheduler.get_batch_size(),
            }

        elif command == "strategy":
            scheduler.compute_mode()
            strategy = scheduler.get_execution_strategy()
            result = strategy.to_dict()

        elif command == "interval":
            scheduler.compute_mode()
            interval = scheduler.get_poll_interval()
            result = {
                "mode": scheduler.get_current_mode().value,
                "poll_interval_seconds": interval,
            }

        else:
            result = {"success": False, "error": f"Unknown command: {command}"}

    except Exception as e:
        result = {"success": False, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
