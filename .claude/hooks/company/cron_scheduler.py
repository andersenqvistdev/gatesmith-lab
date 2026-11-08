#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Cron Scheduler — scheduled task execution for social media and automated tasks.

P43 Implementation: Social Presence Automation.

This module provides cron-based scheduling for automated tasks, primarily
targeting social media posting but extensible to any scheduled operations.
It parses standard cron expressions, tracks last execution times, and
integrates with the daemon or runs standalone.

Cron Expression Format (5-field standard):
    minute hour day-of-month month day-of-week

    Fields:
    - minute: 0-59
    - hour: 0-23
    - day-of-month: 1-31
    - month: 1-12
    - day-of-week: 0-6 (0 = Sunday)

    Special Characters:
    - * = any value
    - */N = every N units
    - N-M = range from N to M
    - N,M,O = list of values

Configuration:
    Schedule file: .company/social/schedule.json
    State file: .company/social/cron_state.json

    Schedule format:
    {
        "timezone": "UTC",
        "tasks": [
            {
                "id": "daily-update",
                "name": "Daily Status Update",
                "cron": "0 9 * * *",
                "platform": "twitter",
                "action": "post_status",
                "enabled": true,
                "config": {...}
            }
        ]
    }

Usage:
    # Check and execute due tasks
    python cron_scheduler.py run-once

    # List scheduled tasks
    python cron_scheduler.py list

    # Show next execution times
    python cron_scheduler.py next

    # Test cron expression matching
    python cron_scheduler.py test "*/5 * * * *"

    # Force execute a specific task
    python cron_scheduler.py execute --task-id "daily-update"
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("cron_scheduler")


# =============================================================================
# Lazy imports for sibling modules
# =============================================================================

company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver

    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


# =============================================================================
# Timezone Handling
# =============================================================================


def get_timezone(tz_name: str) -> timezone:
    """
    Get a timezone object from a timezone name.

    Supports UTC and UTC offset formats. For full timezone support,
    zoneinfo (Python 3.9+) is used if available.

    Args:
        tz_name: Timezone name (e.g., "UTC", "America/New_York", "+05:00")

    Returns:
        timezone object
    """
    if tz_name == "UTC" or tz_name == "utc":
        return timezone.utc

    # Try UTC offset format (e.g., "+05:00", "-08:00")
    offset_match = re.match(r"^([+-])(\d{1,2}):?(\d{2})?$", tz_name)
    if offset_match:
        sign = 1 if offset_match.group(1) == "+" else -1
        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3) or 0)
        offset = timedelta(hours=hours * sign, minutes=minutes * sign)
        return timezone(offset)

    # Try zoneinfo for named timezones (Python 3.9+)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)  # type: ignore[return-value]
    except ImportError:
        pass
    except KeyError:
        pass

    # Fallback to UTC with warning
    logger.warning(f"Unknown timezone '{tz_name}', falling back to UTC")
    return timezone.utc


def now_in_timezone(tz: timezone | Any) -> datetime:
    """
    Get current datetime in the specified timezone.

    Args:
        tz: timezone object

    Returns:
        Current datetime in the specified timezone
    """
    return datetime.now(tz)


# =============================================================================
# Cron Expression Parsing
# =============================================================================


class CronExpression:
    """
    Parser and matcher for standard 5-field cron expressions.

    Supports:
    - Exact values: "5" matches 5
    - Wildcards: "*" matches any
    - Steps: "*/15" matches 0, 15, 30, 45
    - Ranges: "1-5" matches 1, 2, 3, 4, 5
    - Lists: "1,3,5" matches 1, 3, 5
    - Combined: "1-5/2" matches 1, 3, 5

    Examples:
        >>> cron = CronExpression("0 9 * * *")  # 9:00 AM daily
        >>> cron = CronExpression("*/15 * * * *")  # Every 15 minutes
        >>> cron = CronExpression("0 0 1 * *")  # First day of month at midnight
    """

    # Field index constants
    MINUTE = 0
    HOUR = 1
    DAY_OF_MONTH = 2
    MONTH = 3
    DAY_OF_WEEK = 4

    # Field ranges (min, max)
    FIELD_RANGES = {
        MINUTE: (0, 59),
        HOUR: (0, 23),
        DAY_OF_MONTH: (1, 31),
        MONTH: (1, 12),
        DAY_OF_WEEK: (0, 6),  # 0 = Sunday
    }

    def __init__(self, expression: str):
        """
        Initialize a cron expression parser.

        Args:
            expression: 5-field cron expression string

        Raises:
            ValueError: If expression is invalid
        """
        self.expression = expression.strip()
        self.fields = self._parse(self.expression)

    def _parse(self, expression: str) -> list[set[int]]:
        """
        Parse a cron expression into field sets.

        Args:
            expression: Cron expression string

        Returns:
            List of 5 sets, each containing valid values for that field

        Raises:
            ValueError: If expression is invalid
        """
        parts = expression.split()

        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields, got {len(parts)}: '{expression}'"
            )

        fields: list[set[int]] = []

        for i, part in enumerate(parts):
            min_val, max_val = self.FIELD_RANGES[i]
            field_values = self._parse_field(part, min_val, max_val)
            fields.append(field_values)

        return fields

    def _parse_field(self, field: str, min_val: int, max_val: int) -> set[int]:
        """
        Parse a single cron field into a set of valid values.

        Args:
            field: Field string (e.g., "*", "*/15", "1-5", "1,3,5")
            min_val: Minimum valid value for this field
            max_val: Maximum valid value for this field

        Returns:
            Set of valid integer values

        Raises:
            ValueError: If field is invalid
        """
        values: set[int] = set()

        # Handle list (e.g., "1,3,5")
        for item in field.split(","):
            item = item.strip()

            # Handle step (e.g., "*/15" or "0-30/5")
            step = 1
            if "/" in item:
                base, step_str = item.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    raise ValueError(f"Invalid step value: '{step_str}'")
                if step < 1:
                    raise ValueError(f"Step must be positive: {step}")
                item = base

            # Handle wildcard
            if item == "*":
                values.update(range(min_val, max_val + 1, step))
                continue

            # Handle range (e.g., "1-5")
            if "-" in item:
                try:
                    start, end = item.split("-", 1)
                    start_val = int(start)
                    end_val = int(end)
                except ValueError:
                    raise ValueError(f"Invalid range: '{item}'")

                if start_val < min_val or end_val > max_val:
                    raise ValueError(
                        f"Range {start_val}-{end_val} out of bounds "
                        f"({min_val}-{max_val})"
                    )
                if start_val > end_val:
                    raise ValueError(f"Invalid range: start > end in '{item}'")

                values.update(range(start_val, end_val + 1, step))
                continue

            # Handle single value
            try:
                val = int(item)
            except ValueError:
                raise ValueError(f"Invalid value: '{item}'")

            if val < min_val or val > max_val:
                raise ValueError(f"Value {val} out of bounds ({min_val}-{max_val})")

            values.add(val)

        return values

    def matches(self, dt: datetime) -> bool:
        """
        Check if a datetime matches this cron expression.

        Args:
            dt: Datetime to check

        Returns:
            True if the datetime matches the cron expression
        """
        # Extract fields from datetime
        minute = dt.minute
        hour = dt.hour
        day_of_month = dt.day
        month = dt.month
        day_of_week = dt.weekday()  # 0 = Monday in Python

        # Convert Python weekday (0=Mon) to cron weekday (0=Sun)
        # Python: Mon=0, Tue=1, ... Sun=6
        # Cron:   Sun=0, Mon=1, ... Sat=6
        cron_dow = (day_of_week + 1) % 7

        # Check each field
        if minute not in self.fields[self.MINUTE]:
            return False
        if hour not in self.fields[self.HOUR]:
            return False
        if month not in self.fields[self.MONTH]:
            return False

        # Day matching: match if EITHER day-of-month OR day-of-week matches
        # (when both are restricted), or if one is * and the other matches
        dom_restricted = self.fields[self.DAY_OF_MONTH] != set(range(1, 32))
        dow_restricted = self.fields[self.DAY_OF_WEEK] != set(range(0, 7))

        if dom_restricted and dow_restricted:
            # Both restricted: match if either matches
            if (
                day_of_month not in self.fields[self.DAY_OF_MONTH]
                and cron_dow not in self.fields[self.DAY_OF_WEEK]
            ):
                return False
        else:
            # One or both unrestricted: both must match
            if day_of_month not in self.fields[self.DAY_OF_MONTH]:
                return False
            if cron_dow not in self.fields[self.DAY_OF_WEEK]:
                return False

        return True

    def next_occurrence(
        self,
        after: datetime,
        max_iterations: int = 366 * 24 * 60,
    ) -> datetime | None:
        """
        Find the next occurrence after a given datetime.

        Args:
            after: Start searching after this datetime
            max_iterations: Maximum minutes to search ahead (default: 1 year)

        Returns:
            Next matching datetime, or None if not found within max_iterations
        """
        # Start from the next minute
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

        for _ in range(max_iterations):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)

        return None

    def __repr__(self) -> str:
        return f"CronExpression('{self.expression}')"

    def __str__(self) -> str:
        return self.expression


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class ScheduledTask:
    """
    Represents a scheduled task with cron timing.

    Attributes:
        id: Unique identifier for the task
        name: Human-readable task name
        cron: Cron expression string
        platform: Target platform (e.g., "twitter", "linkedin")
        action: Action to perform (e.g., "post_status", "share_article")
        enabled: Whether the task is currently enabled
        config: Additional configuration for the task
        last_run: ISO timestamp of last execution
        last_status: Status of last execution ("success", "failed", "skipped")
    """

    id: str
    name: str
    cron: str
    platform: str
    action: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    last_run: str | None = None
    last_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        """Create from dictionary."""
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            cron=data.get("cron", ""),
            platform=data.get("platform", ""),
            action=data.get("action", ""),
            enabled=data.get("enabled", True),
            config=data.get("config", {}),
            last_run=data.get("last_run"),
            last_status=data.get("last_status"),
        )


@dataclass
class ExecutionResult:
    """Result of a scheduled task execution."""

    task_id: str
    success: bool
    message: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_ms: int = 0
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


# =============================================================================
# Cron Scheduler
# =============================================================================


class CronScheduler:
    """
    Scheduler for cron-based task execution.

    Manages scheduled tasks, tracks execution state, and provides
    methods for checking and executing due tasks.

    Attributes:
        company_dir: Path to .company directory
        schedule_path: Path to schedule.json
        state_path: Path to cron_state.json
        timezone: Configured timezone
        tasks: List of scheduled tasks
        executors: Registry of task executors by action name
    """

    def __init__(
        self,
        company_dir: str | Path | None = None,
        schedule_file: str = "social/schedule.json",
        state_file: str = "social/cron_state.json",
    ):
        """
        Initialize the cron scheduler.

        Args:
            company_dir: Path to .company directory. Auto-resolved if None.
            schedule_file: Relative path to schedule file within company_dir
            state_file: Relative path to state file within company_dir
        """
        _ensure_imports()

        if company_dir is None:
            self.company_dir = company_resolver.get_company_dir()
        else:
            self.company_dir = Path(company_dir)

        self.schedule_path = self.company_dir / schedule_file
        self.state_path = self.company_dir / state_file

        self.timezone: timezone | Any = timezone.utc
        self.tasks: list[ScheduledTask] = []
        self.executors: dict[str, Callable[[ScheduledTask], ExecutionResult]] = {}

        # Register default executors
        self._register_default_executors()

    def _register_default_executors(self) -> None:
        """Register default task executors."""

        # Placeholder executor for unimplemented actions
        def placeholder_executor(task: ScheduledTask) -> ExecutionResult:
            logger.warning(
                f"No executor registered for action '{task.action}' "
                f"on platform '{task.platform}'"
            )
            return ExecutionResult(
                task_id=task.id,
                success=False,
                message=f"No executor for action '{task.action}'",
            )

        # Register placeholder for common actions
        # These will be overridden by actual social media clients
        self.executors["placeholder"] = placeholder_executor

    def register_executor(
        self,
        action: str,
        executor: Callable[[ScheduledTask], ExecutionResult],
    ) -> None:
        """
        Register an executor for a task action.

        Args:
            action: Action name (e.g., "post_status")
            executor: Callable that executes the task and returns a result
        """
        self.executors[action] = executor
        logger.debug(f"Registered executor for action '{action}'")

    def load_schedules(self, config_path: str | Path | None = None) -> None:
        """
        Load scheduled tasks from configuration file.

        Args:
            config_path: Override path to schedule file
        """
        path = Path(config_path) if config_path else self.schedule_path

        if not path.exists():
            logger.info(f"Schedule file not found: {path}")
            self.tasks = []
            return

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load schedule file: {e}")
            self.tasks = []
            return

        # Load timezone
        tz_name = data.get("timezone", "UTC")
        self.timezone = get_timezone(tz_name)

        # Load tasks
        self.tasks = []
        for task_data in data.get("tasks", []):
            try:
                task = ScheduledTask.from_dict(task_data)
                # Validate cron expression
                CronExpression(task.cron)
                self.tasks.append(task)
            except (ValueError, KeyError) as e:
                logger.error(f"Invalid task in schedule: {e}")
                continue

        logger.info(f"Loaded {len(self.tasks)} scheduled tasks")

        # Merge with state
        self._merge_state()

    def _merge_state(self) -> None:
        """Merge persisted state with loaded tasks."""
        if not self.state_path.exists():
            return

        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        task_states = state.get("tasks", {})

        for task in self.tasks:
            if task.id in task_states:
                task_state = task_states[task.id]
                task.last_run = task_state.get("last_run")
                task.last_status = task_state.get("last_status")

    def save_state(self) -> None:
        """
        Save execution state to file.

        Uses atomic write to prevent corruption.
        """
        # Ensure parent directory exists
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "tasks": {
                task.id: {
                    "last_run": task.last_run,
                    "last_status": task.last_status,
                }
                for task in self.tasks
            },
        }

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json",
            prefix="cron_state_",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def should_run(self, task: ScheduledTask, now: datetime) -> bool:
        """
        Check if a task should run at the given time.

        A task should run if:
        1. It is enabled
        2. The cron expression matches the current minute
        3. It has not already run in this minute

        Args:
            task: Task to check
            now: Current datetime (timezone-aware)

        Returns:
            True if the task should run
        """
        if not task.enabled:
            return False

        # Parse cron expression
        try:
            cron = CronExpression(task.cron)
        except ValueError:
            logger.error(f"Invalid cron expression for task {task.id}: {task.cron}")
            return False

        # Check if cron matches current time
        if not cron.matches(now):
            return False

        # Check if already run this minute
        if task.last_run:
            try:
                last_run = datetime.fromisoformat(task.last_run)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)

                # Same minute = already run
                if (
                    last_run.year == now.year
                    and last_run.month == now.month
                    and last_run.day == now.day
                    and last_run.hour == now.hour
                    and last_run.minute == now.minute
                ):
                    return False
            except (ValueError, TypeError):
                pass  # Invalid last_run, allow execution

        return True

    def get_due_tasks(self, now: datetime | None = None) -> list[ScheduledTask]:
        """
        Get all tasks that are due for execution.

        Args:
            now: Current datetime. Uses current time if None.

        Returns:
            List of tasks that should run now
        """
        if now is None:
            now = now_in_timezone(self.timezone)

        due_tasks = []
        for task in self.tasks:
            if self.should_run(task, now):
                due_tasks.append(task)

        return due_tasks

    def execute_task(self, task: ScheduledTask) -> ExecutionResult:
        """
        Execute a scheduled task.

        Looks up the executor for the task's action and runs it.
        Updates the task's last_run and last_status fields.

        Args:
            task: Task to execute

        Returns:
            ExecutionResult with success status and details
        """
        start_time = datetime.now(timezone.utc)

        logger.info(
            f"Executing task '{task.id}' ({task.name}) - "
            f"platform={task.platform}, action={task.action}"
        )

        # Get executor
        executor = self.executors.get(task.action)

        if executor is None:
            # Try platform-specific executor
            platform_action = f"{task.platform}.{task.action}"
            executor = self.executors.get(platform_action)

        if executor is None:
            # Use placeholder executor
            executor = self.executors.get("placeholder")

        if executor is None:
            result = ExecutionResult(
                task_id=task.id,
                success=False,
                message=f"No executor found for action '{task.action}'",
            )
        else:
            try:
                result = executor(task)
            except Exception as e:
                logger.exception(f"Task execution failed: {e}")
                result = ExecutionResult(
                    task_id=task.id,
                    success=False,
                    message=f"Execution error: {e}",
                )

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        result.duration_ms = duration_ms

        # Update task state
        task.last_run = end_time.isoformat()
        task.last_status = "success" if result.success else "failed"

        # Log result
        status_emoji = "SUCCESS" if result.success else "FAILED"
        logger.info(
            f"Task '{task.id}' {status_emoji}: {result.message} "
            f"(duration: {duration_ms}ms)"
        )

        return result

    def run_once(self) -> list[ExecutionResult]:
        """
        Check and execute all due tasks.

        This is the main entry point for daemon integration.
        Loads schedules, checks for due tasks, executes them,
        and saves state.

        Returns:
            List of execution results
        """
        # Reload schedules to pick up any changes
        self.load_schedules()

        now = now_in_timezone(self.timezone)
        due_tasks = self.get_due_tasks(now)

        if not due_tasks:
            logger.debug("No tasks due for execution")
            return []

        results = []
        for task in due_tasks:
            result = self.execute_task(task)
            results.append(result)

        # Save state after all executions
        self.save_state()

        # Log execution to daemon log
        self._log_to_daemon(results)

        return results

    def _log_to_daemon(self, results: list[ExecutionResult]) -> None:
        """Log execution results to daemon log file."""
        log_dir = self.company_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "cron_scheduler.log"

        for result in results:
            log_entry = {
                "timestamp": result.timestamp,
                "task_id": result.task_id,
                "success": result.success,
                "message": result.message,
                "duration_ms": result.duration_ms,
            }

            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry) + "\n")
            except OSError as e:
                logger.error(f"Failed to write to log file: {e}")

    def get_next_runs(self) -> dict[str, datetime | None]:
        """
        Get next scheduled run time for all tasks.

        Returns:
            Dictionary mapping task ID to next run datetime
        """
        now = now_in_timezone(self.timezone)
        next_runs: dict[str, datetime | None] = {}

        for task in self.tasks:
            if not task.enabled:
                next_runs[task.id] = None
                continue

            try:
                cron = CronExpression(task.cron)
                next_run = cron.next_occurrence(now)
                next_runs[task.id] = next_run
            except ValueError:
                next_runs[task.id] = None

        return next_runs


# =============================================================================
# CLI Interface
# =============================================================================


def print_help() -> None:
    """Print usage help."""
    help_text = """
Cron Scheduler — Scheduled task execution for automated operations

Commands:
    run-once        Check and execute all due tasks
    list            List all scheduled tasks
    next            Show next execution times for all tasks
    test EXPR       Test a cron expression against current time
    execute         Force execute a specific task
    status          Show scheduler status

Options:
    --task-id ID    Task ID for execute command
    --json          Output as JSON
    --verbose       Verbose output

Examples:
    # Run scheduled tasks check
    python cron_scheduler.py run-once

    # List all tasks
    python cron_scheduler.py list

    # Show next run times
    python cron_scheduler.py next

    # Test cron expression
    python cron_scheduler.py test "*/5 * * * *"

    # Force execute a task
    python cron_scheduler.py execute --task-id "daily-update"
"""
    print(help_text)


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    json_output = "--json" in sys.argv
    verbose = "--verbose" in sys.argv

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    scheduler = CronScheduler()
    scheduler.load_schedules()

    if command == "run-once":
        results = scheduler.run_once()

        if json_output:
            print(json.dumps([r.to_dict() for r in results], indent=2))
        else:
            if not results:
                print("No tasks executed")
            else:
                print(f"Executed {len(results)} task(s):")
                for result in results:
                    status = "OK" if result.success else "FAILED"
                    print(f"  [{status}] {result.task_id}: {result.message}")

    elif command == "list":
        if json_output:
            print(json.dumps([t.to_dict() for t in scheduler.tasks], indent=2))
        else:
            if not scheduler.tasks:
                print("No scheduled tasks")
            else:
                print(f"Scheduled Tasks ({len(scheduler.tasks)}):\n")
                for task in scheduler.tasks:
                    status = "enabled" if task.enabled else "disabled"
                    print(f"  {task.id}: {task.name}")
                    print(f"    Cron: {task.cron}")
                    print(f"    Platform: {task.platform}")
                    print(f"    Action: {task.action}")
                    print(f"    Status: {status}")
                    if task.last_run:
                        print(f"    Last run: {task.last_run} ({task.last_status})")
                    print()

    elif command == "next":
        next_runs = scheduler.get_next_runs()

        if json_output:
            print(
                json.dumps(
                    {
                        tid: (dt.isoformat() if dt else None)
                        for tid, dt in next_runs.items()
                    },
                    indent=2,
                )
            )
        else:
            print("Next Scheduled Runs:\n")
            for task in scheduler.tasks:
                next_run = next_runs.get(task.id)
                if next_run:
                    print(f"  {task.id}: {next_run.strftime('%Y-%m-%d %H:%M')}")
                elif not task.enabled:
                    print(f"  {task.id}: (disabled)")
                else:
                    print(f"  {task.id}: (no upcoming run)")

    elif command == "test":
        if len(sys.argv) < 3:
            print("Error: cron expression required")
            print('Usage: python cron_scheduler.py test "*/5 * * * *"')
            sys.exit(1)

        expr = sys.argv[2]

        try:
            cron = CronExpression(expr)
        except ValueError as e:
            print(f"Invalid cron expression: {e}")
            sys.exit(1)

        now = now_in_timezone(scheduler.timezone)
        matches = cron.matches(now)
        next_run = cron.next_occurrence(now)

        if json_output:
            print(
                json.dumps(
                    {
                        "expression": expr,
                        "current_time": now.isoformat(),
                        "matches_now": matches,
                        "next_run": next_run.isoformat() if next_run else None,
                    },
                    indent=2,
                )
            )
        else:
            print(f"Cron Expression: {expr}")
            print(f"Current Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print(f"Matches Now: {'Yes' if matches else 'No'}")
            if next_run:
                print(f"Next Run: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")

    elif command == "execute":
        task_id = None
        for i, arg in enumerate(sys.argv):
            if arg == "--task-id" and i + 1 < len(sys.argv):
                task_id = sys.argv[i + 1]
                break

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        # Find task
        task = None
        for t in scheduler.tasks:
            if t.id == task_id:
                task = t
                break

        if not task:
            print(f"Error: Task '{task_id}' not found")
            sys.exit(1)

        result = scheduler.execute_task(task)
        scheduler.save_state()

        if json_output:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            status = "SUCCESS" if result.success else "FAILED"
            print(f"[{status}] {result.task_id}")
            print(f"  Message: {result.message}")
            print(f"  Duration: {result.duration_ms}ms")

    elif command == "status":
        now = now_in_timezone(scheduler.timezone)
        due_tasks = scheduler.get_due_tasks(now)

        status_data = {
            "timezone": str(scheduler.timezone),
            "current_time": now.isoformat(),
            "total_tasks": len(scheduler.tasks),
            "enabled_tasks": sum(1 for t in scheduler.tasks if t.enabled),
            "due_tasks": len(due_tasks),
            "schedule_file": str(scheduler.schedule_path),
            "state_file": str(scheduler.state_path),
        }

        if json_output:
            print(json.dumps(status_data, indent=2))
        else:
            print("Cron Scheduler Status\n")
            print(f"  Timezone: {status_data['timezone']}")
            print(f"  Current Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print(f"  Total Tasks: {status_data['total_tasks']}")
            print(f"  Enabled Tasks: {status_data['enabled_tasks']}")
            print(f"  Tasks Due Now: {status_data['due_tasks']}")
            print(f"  Schedule File: {status_data['schedule_file']}")
            print(f"  State File: {status_data['state_file']}")

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
