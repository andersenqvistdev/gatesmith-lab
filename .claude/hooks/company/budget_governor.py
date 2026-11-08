#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P25 Full Autonomous Operation — Budget Governor.

Active token throttling with budget-aware operation control.
Implements throttle levels (NORMAL/CAUTIOUS/MINIMAL/PAUSED) based on
daily budget utilization to prevent budget overruns.

Throttle Levels:
    NORMAL   - All tasks allowed (< 70% daily budget used)
    CAUTIOUS - Skip low-priority, defer complex tasks (70-85%)
    MINIMAL  - Only critical/urgent tasks (85-95%)
    PAUSED   - No tasks until next budget period (> 95%)

Cost Estimation by Complexity:
    trivial  - 500 tokens
    standard - 2000 tokens
    complex  - 5000 tokens
    epic     - 15000 tokens

Configuration (gatesmith.json):
    "budgetGovernor": {
        "monthlyTokenBudget": 500000,
        "cautiousThresholdPercent": 70,
        "minimalThresholdPercent": 85,
        "pauseThresholdPercent": 95,
        "alertOnThresholdChange": true
    }

Usage:
    # Check if task should execute
    from budget_governor import BudgetGovernor
    governor = BudgetGovernor()
    allowed, reason = governor.should_execute_task(task)

    # Get current throttle level
    level = governor.compute_throttle_level()

    # Get budget status
    status = governor.get_budget_status()
"""

from __future__ import annotations

import calendar
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
_company_resolver = None
_external_connectors = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global _company_resolver, _external_connectors
    if _company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import external_connectors as ec

        _company_resolver = cr
        _external_connectors = ec
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import external_connectors as ec  # type: ignore[no-redef]

        _company_resolver = cr
        _external_connectors = ec


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

EFFICIENCY_DATA_FILE = "state/efficiency_data.json"
FORGE_CONFIG_FILE = "gatesmith.json"
BUDGET_STATE_FILE = "budget_governor_state.json"

# Cost estimation by complexity (tokens)
COMPLEXITY_COST_ESTIMATES = {
    "trivial": 500,
    "standard": 2000,
    "complex": 5000,
    "epic": 15000,
}

# Default configuration
DEFAULT_MONTHLY_TOKEN_BUDGET = 999_999_999  # Subscription-based — no token limit
DEFAULT_CAUTIOUS_THRESHOLD = 70
DEFAULT_MINIMAL_THRESHOLD = 85
DEFAULT_PAUSE_THRESHOLD = 95

# Priority levels for task filtering
PRIORITY_CRITICAL = "critical"
PRIORITY_HIGH = "high"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"

# Worker throttling: reduce to 1 worker if remaining budget drops below this %
WORKER_REDUCTION_THRESHOLD_PERCENT = 30


# -----------------------------------------------------------------------------
# Throttle Level Enum
# -----------------------------------------------------------------------------


class ThrottleLevel(Enum):
    """Throttle levels for budget-aware operation."""

    NORMAL = "normal"  # All tasks allowed
    CAUTIOUS = "cautious"  # Skip low-priority, defer complex
    MINIMAL = "minimal"  # Only critical/urgent tasks
    PAUSED = "paused"  # No tasks until next budget period

    def __str__(self) -> str:
        return self.value


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class BudgetStatus:
    """Current budget status information."""

    monthly_budget: int
    current_spend: int
    daily_budget: int
    daily_spend: int
    days_remaining: int
    utilization_percent: float
    daily_utilization_percent: float
    throttle_level: ThrottleLevel
    remaining_budget: int
    remaining_daily_budget: int
    remaining_budget_percent: float  # % of monthly budget remaining
    max_workers: int  # Maximum parallel workers allowed given budget


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return _company_resolver.get_company_dir()


def get_project_root() -> Path:
    """
    Get the project root directory (where gatesmith.json lives).

    Searches upward from the company directory for gatesmith.json.
    Falls back to parent of .company directory if not found.
    """
    _ensure_imports()

    # Try to find gatesmith.json by searching upward
    company_dir = _company_resolver.get_company_dir()
    current = company_dir.parent  # Start from parent of .company

    # Track visited paths to avoid infinite loops
    visited: set[Path] = set()

    while current not in visited and current != current.parent:
        visited.add(current)

        config_path = current / FORGE_CONFIG_FILE
        if config_path.exists() and config_path.is_file():
            return current

        current = current.parent

    # Fallback: return parent of .company directory
    return company_dir.parent


def get_efficiency_data_path() -> Path:
    """Get the efficiency data file path."""
    return get_company_dir() / EFFICIENCY_DATA_FILE


def get_forge_config_path() -> Path:
    """Get the gatesmith.json file path."""
    return get_project_root() / FORGE_CONFIG_FILE


def get_budget_state_path() -> Path:
    """Get the budget governor state file path."""
    return get_company_dir() / BUDGET_STATE_FILE


# -----------------------------------------------------------------------------
# Budget Governor
# -----------------------------------------------------------------------------


class BudgetGovernor:
    """
    Budget-aware operation controller with active token throttling.

    Monitors token spend against budget and adjusts operation mode
    to prevent budget overruns.
    """

    def __init__(self, config: dict | None = None):
        """
        Initialize the budget governor.

        Args:
            config: Optional configuration dictionary. If not provided,
                   loads from gatesmith.json budgetGovernor section.
        """
        if config is None:
            config = self._load_config()

        self.monthly_budget = config.get(
            "monthlyTokenBudget", DEFAULT_MONTHLY_TOKEN_BUDGET
        )
        self.cautious_threshold = config.get(
            "cautiousThresholdPercent", DEFAULT_CAUTIOUS_THRESHOLD
        )
        self.minimal_threshold = config.get(
            "minimalThresholdPercent", DEFAULT_MINIMAL_THRESHOLD
        )
        self.pause_threshold = config.get(
            "pauseThresholdPercent", DEFAULT_PAUSE_THRESHOLD
        )
        self.alert_on_threshold_change = config.get("alertOnThresholdChange", True)

        # Track last known throttle level for threshold crossing detection
        self._last_throttle_level = self._load_last_throttle_level()

    def _load_config(self) -> dict:
        """Load configuration from gatesmith.json."""
        try:
            path = get_forge_config_path()
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    config = json.load(f)
                return config.get("budgetGovernor", {})
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _load_last_throttle_level(self) -> ThrottleLevel:
        """Load last known throttle level from state file."""
        try:
            path = get_budget_state_path()
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
                level_str = state.get("last_throttle_level", "normal")
                return ThrottleLevel(level_str)
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return ThrottleLevel.NORMAL

    def _save_throttle_level(self, level: ThrottleLevel) -> None:
        """Save current throttle level to state file."""
        import os
        import tempfile

        state = {
            "last_throttle_level": level.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        path = get_budget_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="budget_state_", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_current_spend(self) -> int:
        """
        Get current monthly token spend from efficiency_data.json.

        Returns:
            Total tokens spent this month.
        """
        try:
            path = get_efficiency_data_path()
            if not path.exists():
                return 0

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            cost_tracking = data.get("cost_tracking", {})
            totals = cost_tracking.get("totals", {})
            return totals.get("total_tokens", 0)
        except (json.JSONDecodeError, OSError):
            return 0

    def get_daily_spend(self) -> int:
        """
        Get today's token spend from efficiency_data.json.

        Returns:
            Tokens spent today.
        """
        try:
            path = get_efficiency_data_path()
            if not path.exists():
                return 0

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cost_tracking = data.get("cost_tracking", {})
            daily = cost_tracking.get("daily", {})

            if today in daily:
                day_data = daily[today]
                # Handle both old format (int) and new format (dict)
                if isinstance(day_data, dict):
                    return day_data.get("total_tokens", 0)
                return day_data
            return 0
        except (json.JSONDecodeError, OSError):
            return 0

    def get_days_remaining_in_month(self) -> int:
        """
        Get the number of days remaining in the current month (including today).

        Returns:
            Number of days remaining.
        """
        now = datetime.now(timezone.utc)
        _, days_in_month = calendar.monthrange(now.year, now.month)
        return days_in_month - now.day + 1

    def get_daily_budget(self) -> int:
        """
        Compute daily budget based on monthly budget and days remaining.

        Returns:
            Tokens available per day for remaining days.
        """
        days_remaining = self.get_days_remaining_in_month()
        if days_remaining <= 0:
            return 0

        current_spend = self.get_current_spend()
        remaining_budget = max(0, self.monthly_budget - current_spend)
        return remaining_budget // days_remaining

    def get_remaining_budget_percent(self) -> float:
        """
        Get the percentage of monthly budget that is still remaining.

        Returns:
            Remaining budget as a percentage (0-100). Returns 0 if budget is 0.
        """
        if self.monthly_budget <= 0:
            return 0.0
        current_spend = self.get_current_spend()
        remaining = max(0, self.monthly_budget - current_spend)
        return (remaining / self.monthly_budget) * 100.0

    def get_max_workers(self, requested_workers: int = 2) -> int:
        """
        Return the maximum number of parallel workers allowed given the current budget.

        Throttle applies to the *total* budget consumed by all workers combined.
        When budget remaining falls below WORKER_REDUCTION_THRESHOLD_PERCENT,
        worker count is capped at 1 to conserve the remaining budget.

        Args:
            requested_workers: The number of workers the scheduler would like to use.

        Returns:
            Allowed number of workers (1 or requested_workers).
        """
        remaining_pct = self.get_remaining_budget_percent()
        if remaining_pct < WORKER_REDUCTION_THRESHOLD_PERCENT:
            return 1
        return max(1, requested_workers)

    def compute_throttle_level(self) -> ThrottleLevel:
        """
        Compute the current throttle level based on daily budget utilization.

        Uses daily budget (not monthly) to provide more responsive throttling.

        Returns:
            Current ThrottleLevel.
        """
        daily_budget = self.get_daily_budget()
        if daily_budget <= 0:
            return ThrottleLevel.PAUSED

        daily_spend = self.get_daily_spend()
        utilization = (daily_spend / daily_budget) * 100

        if utilization >= self.pause_threshold:
            return ThrottleLevel.PAUSED
        elif utilization >= self.minimal_threshold:
            return ThrottleLevel.MINIMAL
        elif utilization >= self.cautious_threshold:
            return ThrottleLevel.CAUTIOUS
        else:
            return ThrottleLevel.NORMAL

    def estimate_task_cost(
        self, task: dict, team_mode: bool = False, num_workers: int = 1
    ) -> int:
        """
        Estimate token cost for a task based on complexity.

        Args:
            task: Task dictionary with complexity field.
            team_mode: If True, apply Agent Teams token multiplier (P84).
            num_workers: Number of parallel workers. Cost is multiplied by this
                         value so budget projections account for concurrent load.

        Returns:
            Estimated token cost (scaled by num_workers for concurrency).
        """
        complexity = task.get("complexity", "standard")
        base_cost = COMPLEXITY_COST_ESTIMATES.get(
            complexity, COMPLEXITY_COST_ESTIMATES["standard"]
        )

        if team_mode:
            # P84: Apply Agent Teams token multiplier from config
            try:
                config_path = get_forge_config_path()
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        config = json.load(f)
                    multiplier = (
                        config.get("agentTeams", {})
                        .get("budget", {})
                        .get("tokenMultiplier", 3.0)
                    )
                    base_cost = int(base_cost * multiplier)
            except (json.JSONDecodeError, OSError):
                base_cost = int(base_cost * 3.0)  # Default multiplier

        # Scale by number of parallel workers for concurrency-aware projection
        workers = max(1, num_workers)
        return base_cost * workers

    def _get_task_priority(self, task: dict) -> str:
        """
        Get the priority level of a task.

        Args:
            task: Task dictionary.

        Returns:
            Priority string: critical, high, normal, or low.
        """
        # Check explicit priority field
        priority = task.get("priority", "").lower()
        if priority in [
            PRIORITY_CRITICAL,
            PRIORITY_HIGH,
            PRIORITY_NORMAL,
            PRIORITY_LOW,
        ]:
            return priority

        # Infer from flags
        if task.get("critical") or task.get("urgent"):
            return PRIORITY_CRITICAL
        if task.get("blocking"):
            return PRIORITY_HIGH

        # Infer from complexity (epic tasks are usually important)
        complexity = task.get("complexity", "standard")
        if complexity == "epic":
            return PRIORITY_HIGH

        return PRIORITY_NORMAL

    def should_execute_task(self, task: dict) -> tuple[bool, str]:
        """
        Determine if a task should be executed under current budget conditions.

        Args:
            task: Task dictionary with complexity and priority fields.

        Returns:
            Tuple of (allowed, reason).
        """
        level = self.compute_throttle_level()
        priority = self._get_task_priority(task)
        complexity = task.get("complexity", "standard")

        # Check for threshold crossing and emit alerts
        self._check_threshold_crossing(level)

        if level == ThrottleLevel.NORMAL:
            return True, "Budget status normal, task allowed"

        elif level == ThrottleLevel.CAUTIOUS:
            # P84: Defer Agent Teams at CAUTIOUS (too expensive)
            if task.get("_team_mode"):
                return False, "Budget cautious: Agent Teams deferred (Nx token cost)"
            # Skip low-priority tasks
            if priority == PRIORITY_LOW:
                return False, "Budget cautious: low-priority tasks deferred"
            # Defer complex/epic tasks unless high priority
            if complexity in ["complex", "epic"] and priority not in [
                PRIORITY_CRITICAL,
                PRIORITY_HIGH,
            ]:
                return False, "Budget cautious: complex tasks deferred"
            return True, "Budget cautious: task priority sufficient"

        elif level == ThrottleLevel.MINIMAL:
            # Only critical/urgent tasks
            if priority not in [PRIORITY_CRITICAL, PRIORITY_HIGH]:
                return (
                    False,
                    "Budget minimal: only critical/high-priority tasks allowed",
                )
            return True, "Budget minimal: critical task allowed"

        elif level == ThrottleLevel.PAUSED:
            return False, "Budget paused: no tasks until next budget period"

        return True, "Unknown throttle level, allowing task"

    def _check_threshold_crossing(self, current_level: ThrottleLevel) -> None:
        """
        Check if throttle level has changed and emit alert if so.

        Args:
            current_level: The newly computed throttle level.
        """
        if current_level == self._last_throttle_level:
            return

        old_level = self._last_throttle_level
        self._last_throttle_level = current_level
        self._save_throttle_level(current_level)

        if self.alert_on_threshold_change:
            self.emit_threshold_alert(old_level, current_level)

    def emit_threshold_alert(
        self, old_level: ThrottleLevel, new_level: ThrottleLevel
    ) -> dict:
        """
        Emit an alert when throttle level changes.

        Args:
            old_level: Previous throttle level.
            new_level: New throttle level.

        Returns:
            Alert dispatch result.
        """
        _ensure_imports()

        # Determine severity based on new level
        if new_level == ThrottleLevel.PAUSED:
            severity = "critical"
        elif new_level == ThrottleLevel.MINIMAL:
            severity = "warning"
        elif new_level == ThrottleLevel.CAUTIOUS:
            severity = "warning"
        else:
            severity = "info"

        # Get budget status for context
        status = self.get_budget_status()

        alert = {
            "severity": severity,
            "message": f"Budget throttle changed: {old_level.value} -> {new_level.value}",
            "rule_id": "budget_throttle_change",
            "details": {
                "old_level": old_level.value,
                "new_level": new_level.value,
                "daily_utilization_percent": round(status.daily_utilization_percent, 1),
                "monthly_utilization_percent": round(status.utilization_percent, 1),
                "daily_budget": status.daily_budget,
                "daily_spend": status.daily_spend,
                "days_remaining": status.days_remaining,
            },
        }

        result = {"alert": alert, "dispatched": False}

        # Try to dispatch via external connectors
        try:
            if hasattr(_external_connectors, "dispatch_to_external_services"):
                # Check if any services are configured
                services = ["slack", "discord"]  # Default alert services
                dispatch_results = _external_connectors.dispatch_to_external_services(
                    alert, services
                )
                result["dispatch_results"] = [
                    {"service": r.service, "success": r.success}
                    for r in dispatch_results
                ]
                result["dispatched"] = any(r.success for r in dispatch_results)
        except Exception as e:
            result["dispatch_error"] = str(e)

        # Also log to stderr for local visibility
        print(
            f"[BUDGET] Throttle level changed: {old_level.value} -> {new_level.value} "
            f"(daily: {status.daily_utilization_percent:.1f}%, "
            f"monthly: {status.utilization_percent:.1f}%)",
            file=sys.stderr,
        )

        return result

    def get_budget_status(self) -> BudgetStatus:
        """
        Get comprehensive budget status.

        Returns:
            BudgetStatus with all current metrics.
        """
        current_spend = self.get_current_spend()
        daily_spend = self.get_daily_spend()
        daily_budget = self.get_daily_budget()
        days_remaining = self.get_days_remaining_in_month()

        utilization = (
            (current_spend / self.monthly_budget) * 100
            if self.monthly_budget > 0
            else 0
        )
        daily_utilization = (
            (daily_spend / daily_budget) * 100 if daily_budget > 0 else 0
        )

        remaining_budget = max(0, self.monthly_budget - current_spend)
        remaining_pct = (
            (remaining_budget / self.monthly_budget) * 100.0
            if self.monthly_budget > 0
            else 0.0
        )

        return BudgetStatus(
            monthly_budget=self.monthly_budget,
            current_spend=current_spend,
            daily_budget=daily_budget,
            daily_spend=daily_spend,
            days_remaining=days_remaining,
            utilization_percent=utilization,
            daily_utilization_percent=daily_utilization,
            throttle_level=self.compute_throttle_level(),
            remaining_budget=remaining_budget,
            remaining_daily_budget=max(0, daily_budget - daily_spend),
            remaining_budget_percent=remaining_pct,
            max_workers=self.get_max_workers(),
        )

    def can_afford_task(self, task: dict) -> bool:
        """
        Check if remaining daily budget can afford a task.

        Args:
            task: Task dictionary with complexity field.

        Returns:
            True if task fits within remaining daily budget.
        """
        estimated_cost = self.estimate_task_cost(task)
        daily_budget = self.get_daily_budget()
        daily_spend = self.get_daily_spend()
        remaining = daily_budget - daily_spend

        return estimated_cost <= remaining

    def get_status_dict(self) -> dict[str, Any]:
        """
        Get budget status as a dictionary (for JSON serialization).

        Returns:
            Dictionary with all budget metrics.
        """
        status = self.get_budget_status()
        return {
            "monthly_budget": status.monthly_budget,
            "current_spend": status.current_spend,
            "daily_budget": status.daily_budget,
            "daily_spend": status.daily_spend,
            "days_remaining": status.days_remaining,
            "utilization_percent": round(status.utilization_percent, 2),
            "daily_utilization_percent": round(status.daily_utilization_percent, 2),
            "throttle_level": status.throttle_level.value,
            "remaining_budget": status.remaining_budget,
            "remaining_daily_budget": status.remaining_daily_budget,
            "remaining_budget_percent": round(status.remaining_budget_percent, 2),
            "max_workers": status.max_workers,
            "thresholds": {
                "cautious": self.cautious_threshold,
                "minimal": self.minimal_threshold,
                "pause": self.pause_threshold,
                "worker_reduction": WORKER_REDUCTION_THRESHOLD_PERCENT,
            },
        }


# -----------------------------------------------------------------------------
# Convenience Functions
# -----------------------------------------------------------------------------


def get_budget_governor(config: dict | None = None) -> BudgetGovernor:
    """
    Get a BudgetGovernor instance.

    Args:
        config: Optional configuration dictionary.

    Returns:
        BudgetGovernor instance.
    """
    return BudgetGovernor(config)


def should_execute_task(task: dict, config: dict | None = None) -> tuple[bool, str]:
    """
    Convenience function to check if a task should execute.

    Args:
        task: Task dictionary.
        config: Optional configuration.

    Returns:
        Tuple of (allowed, reason).
    """
    governor = BudgetGovernor(config)
    return governor.should_execute_task(task)


def get_throttle_level(config: dict | None = None) -> ThrottleLevel:
    """
    Convenience function to get current throttle level.

    Args:
        config: Optional configuration.

    Returns:
        Current ThrottleLevel.
    """
    governor = BudgetGovernor(config)
    return governor.compute_throttle_level()


def estimate_task_cost(task: dict) -> int:
    """
    Convenience function to estimate task cost.

    Args:
        task: Task dictionary with complexity field.

    Returns:
        Estimated token cost.
    """
    complexity = task.get("complexity", "standard")
    return COMPLEXITY_COST_ESTIMATES.get(
        complexity, COMPLEXITY_COST_ESTIMATES["standard"]
    )


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print help information."""
    help_text = """
P25 Budget Governor — Active Token Throttling

Commands:
    status            Get current budget status
    level             Get current throttle level
    check             Check if a task should execute
    estimate          Estimate task cost by complexity
    help              Show this help

Examples:
    # Get current budget status
    python budget_governor.py status

    # Get current throttle level
    python budget_governor.py level

    # Check if a task should execute
    python budget_governor.py check --complexity standard --priority normal

    # Estimate task cost
    python budget_governor.py estimate --complexity complex

Configuration (gatesmith.json):
    "budgetGovernor": {
        "monthlyTokenBudget": 500000,
        "cautiousThresholdPercent": 70,
        "minimalThresholdPercent": 85,
        "pauseThresholdPercent": 95,
        "alertOnThresholdChange": true
    }
"""
    print(help_text)


def main():
    """Main CLI entry point."""
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print_help()
        return

    command = args[0]
    result: dict[str, Any] = {}

    try:
        governor = BudgetGovernor()

        if command == "status":
            result = governor.get_status_dict()

        elif command == "level":
            level = governor.compute_throttle_level()
            result = {
                "throttle_level": level.value,
                "description": {
                    "normal": "All tasks allowed",
                    "cautious": "Skip low-priority, defer complex",
                    "minimal": "Only critical/urgent tasks",
                    "paused": "No tasks until next budget period",
                }.get(level.value, "Unknown"),
            }

        elif command == "check":
            # Parse task from command line
            complexity = "standard"
            priority = "normal"

            for i, arg in enumerate(args[1:], 1):
                if arg == "--complexity" and i < len(args):
                    complexity = args[i + 1]
                elif arg == "--priority" and i < len(args):
                    priority = args[i + 1]

            task = {"complexity": complexity, "priority": priority}
            allowed, reason = governor.should_execute_task(task)

            result = {
                "allowed": allowed,
                "reason": reason,
                "task": task,
                "estimated_cost": governor.estimate_task_cost(task),
                "can_afford": governor.can_afford_task(task),
            }

        elif command == "estimate":
            complexity = "standard"

            for i, arg in enumerate(args[1:], 1):
                if arg == "--complexity" and i < len(args):
                    complexity = args[i + 1]

            task = {"complexity": complexity}
            result = {
                "complexity": complexity,
                "estimated_tokens": governor.estimate_task_cost(task),
                "cost_estimates": COMPLEXITY_COST_ESTIMATES,
            }

        else:
            result = {"success": False, "error": f"Unknown command: {command}"}

    except Exception as e:
        result = {"success": False, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
