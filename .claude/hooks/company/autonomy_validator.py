#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Autonomy Validator — 24-hour autonomous operation validation for G5 goal.

This script provides validation and monitoring for the G5 Autonomy goal:
"Org runs without prompts for 24h"

Features:
1. Track daemon uptime (24h target)
2. Track tasks processed (completed and failed)
3. Calculate success rate
4. Validate G5 goal status
5. Generate validation reports
6. Persist validation history

Usage:
    # Validate current autonomy status
    python autonomy_validator.py validate

    # Show current metrics status
    python autonomy_validator.py status

    # Show validation history
    python autonomy_validator.py history

    # Run continuous monitoring (for daemon integration)
    python autonomy_validator.py monitor

Exit codes:
    0 = Validation passed or command successful
    1 = Validation failed or error
    2 = Insufficient data for validation
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

TARGET_UPTIME_HOURS = 24.0
MIN_SUCCESS_RATE = 0.7  # 70% success rate warning threshold
MIN_TASKS_FOR_VALIDATION = 1  # At least 1 task must be processed
STALE_HEARTBEAT_MINUTES = 5  # Heartbeat older than this is considered stale


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path.

    Looks for .company directory in current directory or parent directories.

    Returns:
        Path to company directory, or current directory if not found.
    """
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        company_dir = parent / ".company"
        if company_dir.exists():
            return parent
    return current


def parse_iso_datetime(iso_str: str) -> datetime | None:
    """Parse an ISO format datetime string.

    Args:
        iso_str: ISO format datetime string.

    Returns:
        Parsed datetime or None if parsing fails.
    """
    if not iso_str:
        return None
    try:
        # Handle both with and without timezone
        if "+" in iso_str or iso_str.endswith("Z"):
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class SessionData:
    """Data for a single daemon session.

    Attributes:
        session_id: Unique identifier for this session.
        started_at: When the session started.
        ended_at: When the session ended (None if still running).
        uptime_seconds: Total uptime in seconds.
        tasks_completed: Number of tasks completed.
        tasks_failed: Number of tasks failed.
        circuit_breaker_trips: Number of circuit breaker trips.
        is_running: Whether the session is currently active.
    """

    session_id: str
    started_at: datetime
    ended_at: datetime | None
    uptime_seconds: float
    tasks_completed: int
    tasks_failed: int
    circuit_breaker_trips: int
    is_running: bool

    @property
    def duration_hours(self) -> float:
        """Calculate session duration in hours."""
        if self.ended_at:
            delta = self.ended_at - self.started_at
            return delta.total_seconds() / 3600
        # For running sessions, calculate from uptime_seconds
        return self.uptime_seconds / 3600


@dataclass
class AutonomyMetrics:
    """Aggregated autonomy metrics across sessions.

    Attributes:
        total_uptime_hours: Total uptime across all sessions.
        total_tasks_completed: Total tasks completed.
        total_tasks_failed: Total tasks failed.
        sessions: List of session data.
        current_session_start: Start time of current session.
        daemon_running: Whether daemon is currently running.
    """

    total_uptime_hours: float = 0.0
    total_tasks_completed: int = 0
    total_tasks_failed: int = 0
    sessions: list[SessionData] = field(default_factory=list)
    current_session_start: datetime | None = None
    daemon_running: bool = False

    @property
    def success_rate(self) -> float:
        """Calculate overall success rate."""
        total = self.total_tasks_completed + self.total_tasks_failed
        if total == 0:
            return 0.0
        return self.total_tasks_completed / total

    @property
    def meets_24h_target(self) -> bool:
        """Check if uptime meets the 24h target."""
        return self.total_uptime_hours >= TARGET_UPTIME_HOURS


@dataclass
class ValidationResult:
    """Result of autonomy validation.

    Attributes:
        validated_at: When the validation was performed.
        g5_status: Status for G5 goal (complete, on_track, at_risk).
        uptime_hours: Total uptime hours.
        tasks_processed: Total tasks processed.
        success_rate: Task success rate.
        is_valid: Whether validation passed.
        failure_reason: Reason for failure (if any).
        warnings: List of warnings (non-blocking issues).
    """

    validated_at: datetime
    g5_status: str
    uptime_hours: float
    tasks_processed: int
    success_rate: float
    is_valid: bool
    failure_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "validated_at": self.validated_at.isoformat(),
            "g5_status": self.g5_status,
            "uptime_hours": self.uptime_hours,
            "tasks_processed": self.tasks_processed,
            "success_rate": self.success_rate,
            "is_valid": self.is_valid,
            "failure_reason": self.failure_reason,
            "warnings": self.warnings,
        }


# -----------------------------------------------------------------------------
# Metric Collection Functions
# -----------------------------------------------------------------------------


def collect_daemon_metrics() -> AutonomyMetrics:
    """Collect metrics from daemon state files.

    Reads daemon.pid, daemon.heartbeat, and loop_monitor.json to build
    a complete picture of daemon operation.

    Returns:
        AutonomyMetrics with collected data.
    """
    metrics = AutonomyMetrics()
    company_root = get_company_dir()
    company_dir = company_root / ".company"

    if not company_dir.exists():
        return metrics

    # Read daemon.pid for session start and task counts
    pid_file = company_dir / "runtime/daemon.pid"
    pid_data = _read_json_file(pid_file)

    if not pid_data:
        return metrics

    # Parse start time
    started_at = parse_iso_datetime(pid_data.get("started_at", ""))
    if started_at:
        metrics.current_session_start = started_at

    # Read heartbeat for current status
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    heartbeat_data = _read_json_file(heartbeat_file)

    if heartbeat_data:
        metrics.daemon_running = heartbeat_data.get("status") == "running"
        uptime_seconds = heartbeat_data.get("uptime_seconds", 0)
        metrics.total_uptime_hours = uptime_seconds / 3600
        metrics.total_tasks_completed = heartbeat_data.get(
            "tasks_completed_this_session", 0
        )
        metrics.total_tasks_failed = heartbeat_data.get("tasks_failed_this_session", 0)

        # Check if heartbeat is stale
        last_heartbeat = parse_iso_datetime(heartbeat_data.get("last_heartbeat", ""))
        if last_heartbeat:
            staleness = datetime.now(timezone.utc) - last_heartbeat
            if staleness > timedelta(minutes=STALE_HEARTBEAT_MINUTES):
                metrics.daemon_running = False

    # If no heartbeat, fall back to pid data
    elif pid_data:
        metrics.total_tasks_completed = pid_data.get("tasks_completed", 0)
        metrics.total_tasks_failed = pid_data.get("tasks_failed", 0)
        if started_at:
            delta = datetime.now(timezone.utc) - started_at
            metrics.total_uptime_hours = delta.total_seconds() / 3600

    # Read loop_monitor.json for additional metrics
    monitor_file = company_dir / "loop_monitor.json"
    monitor_data = _read_json_file(monitor_file)

    if monitor_data:
        health = monitor_data.get("health_metrics", {})
        # Use health metrics if more up-to-date
        if health.get("total_successes", 0) > 0:
            metrics.total_tasks_completed = max(
                metrics.total_tasks_completed, health.get("total_successes", 0)
            )
        if health.get("total_failures", 0) > 0:
            metrics.total_tasks_failed = max(
                metrics.total_tasks_failed, health.get("total_failures", 0)
            )

    # Create session data for current session
    if metrics.current_session_start:
        session = SessionData(
            session_id=f"session-{metrics.current_session_start.strftime('%Y%m%d%H%M%S')}",
            started_at=metrics.current_session_start,
            ended_at=None,
            uptime_seconds=metrics.total_uptime_hours * 3600,
            tasks_completed=metrics.total_tasks_completed,
            tasks_failed=metrics.total_tasks_failed,
            circuit_breaker_trips=0,
            is_running=metrics.daemon_running,
        )
        metrics.sessions.append(session)

    return metrics


def collect_session_data() -> SessionData | None:
    """Collect data for the current daemon session.

    Returns:
        SessionData for current session, or None if no session.
    """
    company_root = get_company_dir()
    company_dir = company_root / ".company"

    if not company_dir.exists():
        return None

    pid_file = company_dir / "runtime/daemon.pid"
    pid_data = _read_json_file(pid_file)

    if not pid_data:
        return None

    started_at = parse_iso_datetime(pid_data.get("started_at", ""))
    if not started_at:
        return None

    # Read heartbeat for current status
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    heartbeat_data = _read_json_file(heartbeat_file)

    uptime_seconds = 0
    tasks_completed = 0
    tasks_failed = 0
    is_running = False

    if heartbeat_data:
        uptime_seconds = heartbeat_data.get("uptime_seconds", 0)
        tasks_completed = heartbeat_data.get("tasks_completed_this_session", 0)
        tasks_failed = heartbeat_data.get("tasks_failed_this_session", 0)
        is_running = heartbeat_data.get("status") == "running"

        # Check staleness
        last_heartbeat = parse_iso_datetime(heartbeat_data.get("last_heartbeat", ""))
        if last_heartbeat:
            staleness = datetime.now(timezone.utc) - last_heartbeat
            if staleness > timedelta(minutes=STALE_HEARTBEAT_MINUTES):
                is_running = False
    else:
        # Fall back to pid data
        tasks_completed = pid_data.get("tasks_completed", 0)
        tasks_failed = pid_data.get("tasks_failed", 0)
        delta = datetime.now(timezone.utc) - started_at
        uptime_seconds = delta.total_seconds()

    return SessionData(
        session_id=f"session-{started_at.strftime('%Y%m%d%H%M%S')}",
        started_at=started_at,
        ended_at=None,
        uptime_seconds=uptime_seconds,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        circuit_breaker_trips=0,
        is_running=is_running,
    )


def _read_json_file(file_path: Path) -> dict[str, Any]:
    """Read and parse a JSON file.

    Args:
        file_path: Path to JSON file.

    Returns:
        Parsed JSON data, or empty dict if file doesn't exist or is invalid.
    """
    if not file_path.exists():
        return {}
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# -----------------------------------------------------------------------------
# Calculation Functions
# -----------------------------------------------------------------------------


def calculate_uptime_hours(uptime_seconds: float) -> float:
    """Convert uptime from seconds to hours.

    Args:
        uptime_seconds: Uptime in seconds.

    Returns:
        Uptime in hours.
    """
    return uptime_seconds / 3600


def calculate_success_rate(completed: int, failed: int) -> float:
    """Calculate task success rate.

    Args:
        completed: Number of completed tasks.
        failed: Number of failed tasks.

    Returns:
        Success rate as a float between 0 and 1.
    """
    total = completed + failed
    if total == 0:
        return 0.0
    return completed / total


# -----------------------------------------------------------------------------
# Validation Functions
# -----------------------------------------------------------------------------


def validate_autonomy_goal(metrics: AutonomyMetrics) -> ValidationResult:
    """Validate the G5 autonomy goal against collected metrics.

    Args:
        metrics: Collected autonomy metrics.

    Returns:
        ValidationResult with validation outcome.
    """
    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    # Check for minimum data
    total_tasks = metrics.total_tasks_completed + metrics.total_tasks_failed
    if total_tasks < MIN_TASKS_FOR_VALIDATION and metrics.total_uptime_hours < 1:
        return ValidationResult(
            validated_at=now,
            g5_status="at_risk",
            uptime_hours=metrics.total_uptime_hours,
            tasks_processed=total_tasks,
            success_rate=metrics.success_rate,
            is_valid=False,
            failure_reason="Insufficient data: No tasks processed and less than 1 hour uptime",
            warnings=warnings,
        )

    # Check 24h uptime target
    if not metrics.meets_24h_target:
        return ValidationResult(
            validated_at=now,
            g5_status="at_risk",
            uptime_hours=metrics.total_uptime_hours,
            tasks_processed=total_tasks,
            success_rate=metrics.success_rate,
            is_valid=False,
            failure_reason=f"Uptime below 24h target: {metrics.total_uptime_hours:.1f}h / 24h",
            warnings=warnings,
        )

    # Check success rate (warning, not failure)
    if (
        metrics.success_rate < MIN_SUCCESS_RATE
        and total_tasks >= MIN_TASKS_FOR_VALIDATION
    ):
        warnings.append(
            f"Success rate below {MIN_SUCCESS_RATE * 100:.0f}%: {metrics.success_rate * 100:.1f}%"
        )

    # Passed validation
    return ValidationResult(
        validated_at=now,
        g5_status="complete",
        uptime_hours=metrics.total_uptime_hours,
        tasks_processed=total_tasks,
        success_rate=metrics.success_rate,
        is_valid=True,
        warnings=warnings,
    )


# -----------------------------------------------------------------------------
# Report Generation
# -----------------------------------------------------------------------------


def generate_validation_report(
    metrics: AutonomyMetrics, result: ValidationResult
) -> str:
    """Generate a human-readable validation report.

    Args:
        metrics: Collected autonomy metrics.
        result: Validation result.

    Returns:
        Formatted report string.
    """
    status_icon = "✓" if result.is_valid else "✗"
    status_text = "PASS" if result.is_valid else "FAIL"

    lines = [
        "=" * 60,
        "G5 Autonomy Validation Report",
        "=" * 60,
        "",
        f"Status: {status_icon} {status_text}",
        f"G5 Goal Status: {result.g5_status}",
        f"Validated At: {result.validated_at.isoformat()}",
        "",
        "--- Metrics ---",
        "",
        f"Uptime: {result.uptime_hours:.1f}h / 24.0h target",
        f"Tasks Processed: {result.tasks_processed}",
        f"  - Completed: {metrics.total_tasks_completed}",
        f"  - Failed: {metrics.total_tasks_failed}",
        f"Success Rate: {result.success_rate * 100:.1f}%",
        f"Daemon Running: {'Yes' if metrics.daemon_running else 'No'}",
        "",
    ]

    # Add sessions info
    if metrics.sessions:
        lines.append("--- Sessions ---")
        lines.append("")
        for session in metrics.sessions:
            status = "running" if session.is_running else "ended"
            lines.append(f"Session: {session.session_id} ({status})")
            lines.append(f"  Started: {session.started_at.isoformat()}")
            lines.append(f"  Duration: {session.duration_hours:.1f}h")
            lines.append(
                f"  Tasks: {session.tasks_completed} completed, {session.tasks_failed} failed"
            )
            lines.append("")

    # Add failure reason if applicable
    if result.failure_reason:
        lines.append("--- Failure Reason ---")
        lines.append("")
        lines.append(result.failure_reason)
        lines.append("")

    # Add warnings if any
    if result.warnings:
        lines.append("--- Warnings ---")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"⚠ {warning}")
        lines.append("")

    lines.append("=" * 60)

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Persistence Functions
# -----------------------------------------------------------------------------


def save_validation_result(result: ValidationResult) -> None:
    """Save validation result to history file.

    Args:
        result: Validation result to save.
    """
    company_root = get_company_dir()
    company_dir = company_root / ".company"
    company_dir.mkdir(exist_ok=True)

    history_file = company_dir / "autonomy_validation.json"

    # Load existing history
    history_data = _read_json_file(history_file)
    if not history_data:
        history_data = {"validations": []}

    # Append new result
    history_data["validations"].append(result.to_dict())

    # Keep only last 100 validations
    history_data["validations"] = history_data["validations"][-100:]

    # Save
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2)


def load_validation_history() -> list[dict[str, Any]]:
    """Load validation history.

    Returns:
        List of validation result dictionaries.
    """
    company_root = get_company_dir()
    company_dir = company_root / ".company"
    history_file = company_dir / "autonomy_validation.json"

    history_data = _read_json_file(history_file)
    return history_data.get("validations", [])


# -----------------------------------------------------------------------------
# CLI Commands
# -----------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """Run validation and print report.

    Args:
        args: Parsed command line arguments.

    Returns:
        Exit code (0 for pass, 1 for fail, 2 for insufficient data).
    """
    metrics = collect_daemon_metrics()
    result = validate_autonomy_goal(metrics)
    report = generate_validation_report(metrics, result)

    print(report)

    # Save result to history
    if not args.dry_run:
        save_validation_result(result)

    if result.is_valid:
        return 0
    elif "Insufficient data" in result.failure_reason:
        return 2
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show current metrics status.

    Args:
        args: Parsed command line arguments.

    Returns:
        Exit code (always 0).
    """
    metrics = collect_daemon_metrics()

    total_tasks = metrics.total_tasks_completed + metrics.total_tasks_failed
    progress = min(100, (metrics.total_uptime_hours / TARGET_UPTIME_HOURS) * 100)

    print("G5 Autonomy Status")
    print("-" * 40)
    print(f"Uptime: {metrics.total_uptime_hours:.1f}h / 24.0h ({progress:.0f}%)")
    print(
        f"Tasks: {total_tasks} ({metrics.total_tasks_completed} completed, {metrics.total_tasks_failed} failed)"
    )
    print(f"Success Rate: {metrics.success_rate * 100:.1f}%")
    print(f"Daemon: {'Running' if metrics.daemon_running else 'Stopped'}")

    if metrics.meets_24h_target:
        print("\n✓ 24h target MET")
    else:
        remaining = TARGET_UPTIME_HOURS - metrics.total_uptime_hours
        print(f"\n⏱ {remaining:.1f}h remaining to 24h target")

    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show validation history.

    Args:
        args: Parsed command line arguments.

    Returns:
        Exit code (always 0).
    """
    history = load_validation_history()

    if not history:
        print("No validation history found.")
        return 0

    print("Validation History")
    print("-" * 60)

    # Show last N entries
    limit = args.limit if hasattr(args, "limit") else 10
    for entry in reversed(history[-limit:]):
        validated_at = entry.get("validated_at", "unknown")
        status = "PASS" if entry.get("is_valid") else "FAIL"
        uptime = entry.get("uptime_hours", 0)
        tasks = entry.get("tasks_processed", 0)
        rate = entry.get("success_rate", 0) * 100

        print(
            f"{validated_at} | {status} | {uptime:.1f}h | {tasks} tasks | {rate:.0f}% success"
        )

    print("-" * 60)
    print(f"Showing last {min(limit, len(history))} of {len(history)} validations")

    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Continuous monitoring mode (for daemon integration).

    Args:
        args: Parsed command line arguments.

    Returns:
        Exit code (0 for success).
    """
    import time

    interval = args.interval if hasattr(args, "interval") else 300  # 5 minutes

    print(f"Starting continuous monitoring (interval: {interval}s)")
    print("Press Ctrl+C to stop")
    print()

    try:
        while True:
            metrics = collect_daemon_metrics()
            result = validate_autonomy_goal(metrics)

            status_icon = "✓" if result.is_valid else "✗"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            print(
                f"[{now}] {status_icon} "
                f"Uptime: {result.uptime_hours:.1f}h | "
                f"Tasks: {result.tasks_processed} | "
                f"Success: {result.success_rate * 100:.0f}%"
            )

            # Save periodic validation
            save_validation_result(result)

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
        return 0

    return 0


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------


def main() -> int:
    """Main entry point for the autonomy validator.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(
        description="G5 Autonomy Validation - 24h autonomous operation tracking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python autonomy_validator.py validate    # Run validation
    python autonomy_validator.py status      # Show current metrics
    python autonomy_validator.py history     # Show validation history
    python autonomy_validator.py monitor     # Continuous monitoring
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # validate command
    validate_parser = subparsers.add_parser(
        "validate", help="Run G5 autonomy validation"
    )
    validate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't save result to history",
    )

    # status command
    subparsers.add_parser("status", help="Show current autonomy metrics")

    # history command
    history_parser = subparsers.add_parser("history", help="Show validation history")
    history_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of entries to show (default: 10)",
    )

    # monitor command
    monitor_parser = subparsers.add_parser("monitor", help="Continuous monitoring mode")
    monitor_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Check interval in seconds (default: 300)",
    )

    args = parser.parse_args()

    # Default to status if no command given
    if not args.command:
        args.command = "status"

    # Dispatch to command handler
    if args.command == "validate":
        return cmd_validate(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "history":
        return cmd_history(args)
    elif args.command == "monitor":
        return cmd_monitor(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
