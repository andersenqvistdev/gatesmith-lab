#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Operation Loop CLI - MVP (Cron Mode) + Continuous Loop Mode

Phase P2 implementation: Single-iteration execution for cron triggering.
Phase P5 implementation: Continuous polling with configurable stop conditions.

Usage:
    python run_loop.py once           # Single poll-claim-execute iteration
    python run_loop.py once --dry-run # Preview without executing
    python run_loop.py status         # Show loop metrics and circuit breaker status
    python run_loop.py status --verbose  # Include full health metrics
    python run_loop.py loop           # Continuous execution (default: until idle)
    python run_loop.py loop --max-tasks 10  # Stop after 10 tasks
    python run_loop.py loop --max-duration 3600  # Stop after 1 hour

Cron example:
    */5 * * * * cd /project && uv run .claude/hooks/company/run_loop.py once

Exit codes:
    0 = Task executed successfully OR no tasks available (idle)
    1 = Error during execution
    2 = Task escalated (requires human approval)
    3 = Loop stopped due to escalation (requires human intervention)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def _setup_imports():
    """Set up import paths for sibling modules."""
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))


# Import loop_monitor types and functions after path setup
# These are imported lazily in run_loop() to avoid circular imports
_loop_monitor_imported = False
CircuitBreaker = None
CircuitBreakerState = None
CircuitBreakerConfig = None
HealthMetrics = None
check_circuit_breaker = None
record_success = None
record_failure = None
should_recover = None
save_monitor_state = None


def _ensure_loop_monitor():
    """Lazily import loop_monitor module."""
    global _loop_monitor_imported
    global CircuitBreaker, CircuitBreakerState, CircuitBreakerConfig, HealthMetrics
    global check_circuit_breaker, record_success, record_failure, should_recover
    global save_monitor_state

    if _loop_monitor_imported:
        return

    _setup_imports()
    import loop_monitor

    CircuitBreaker = loop_monitor.CircuitBreaker
    CircuitBreakerState = loop_monitor.CircuitBreakerState
    CircuitBreakerConfig = loop_monitor.CircuitBreakerConfig
    HealthMetrics = loop_monitor.HealthMetrics
    check_circuit_breaker = loop_monitor.check_circuit_breaker
    record_success = loop_monitor.record_success
    record_failure = loop_monitor.record_failure
    should_recover = loop_monitor.should_recover
    save_monitor_state = loop_monitor.save_monitor_state

    _loop_monitor_imported = True


# =============================================================================
# Employee Selection for Efficiency Tracking
# =============================================================================


def get_available_employee(task: dict | None = None) -> str | None:
    """
    Select an available employee from org.json for task execution.

    Uses efficiency-aware routing when task info is available.
    Falls back to first available employee if no task context.

    Args:
        task: Optional task dict with requirements (capabilities, department)

    Returns:
        Employee ID if found, None if no employees available
    """
    _setup_imports()

    try:
        import company_resolver
        import work_allocator

        company_dir = company_resolver.get_company_dir()
        org_path = company_dir / "org.json"

        if not org_path.exists():
            return None

        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)

        employees = org.get("employees", [])
        if not employees:
            return None

        # Filter to available employees
        available = [e for e in employees if e.get("status") == "available"]
        if not available:
            # Fall back to any employee if none explicitly available
            available = employees

        # If we have task context, try efficiency-aware routing
        if task and hasattr(work_allocator, "suggest_optimal_agent_for_task"):
            candidate_ids = [e["id"] for e in available]
            optimal = work_allocator.suggest_optimal_agent_for_task(task, candidate_ids)
            if optimal:
                return optimal

        # Default: return first available employee
        return available[0]["id"] if available else None

    except Exception:
        return None


# =============================================================================
# Continuous Loop Enums (Task 10.2)
# =============================================================================


class StopReason(Enum):
    """Reason for stopping the continuous loop.

    Attributes:
        MAX_TASKS: Maximum task count reached.
        MAX_DURATION: Maximum duration exceeded.
        IDLE_QUEUE: Queue became idle (no more tasks available).
        ESCALATION: Task was escalated, requires human intervention.
        ERROR: An error occurred during execution.
    """

    MAX_TASKS = "max_tasks"
    MAX_DURATION = "max_duration"
    IDLE_QUEUE = "idle_queue"
    ESCALATION = "escalation"
    ERROR = "error"


class LoopAction(Enum):
    """Action to take after processing a poll result.

    Attributes:
        CONTINUE: Continue to the next iteration immediately.
        SLEEP: Sleep before the next iteration (e.g., after idle poll).
        PAUSE: Pause the loop (requires human intervention, e.g., escalation).
        STOP: Stop the loop (terminal condition reached).
    """

    CONTINUE = "continue"
    SLEEP = "sleep"
    PAUSE = "pause"
    STOP = "stop"


# =============================================================================
# Continuous Loop Dataclasses (Task 10.2)
# =============================================================================


@dataclass
class LoopConfig:
    """Configuration for continuous loop execution.

    Attributes:
        max_tasks: Maximum number of tasks to process before stopping.
                   None means no limit.
        max_duration_seconds: Maximum duration in seconds before stopping.
                              None means no limit.
        until_idle: Stop when queue becomes idle (no tasks available).
                    Default is True.
        dry_run: Preview mode without actually executing tasks.
        poll_interval: Seconds to wait between polls when idle. Default 30.
        agent_id: Agent ID for claiming tasks. Default "loop-agent-001".
                  Set to "auto" to dynamically select from org.json employees.
        use_real_employees: If True, select employees from org.json instead of
                           using the fixed agent_id. Default True.
        queue_path: Path to work queue. Auto-detected if None.
        state_path: Path to session state. Auto-detected if None.
        max_consecutive_failures: Maximum consecutive failures before circuit
                                  breaker trips. Default 3.
        failure_cooldown_seconds: Seconds to wait after circuit breaker trips
                                  before resuming. Default 300 (5 minutes).
        max_tasks_per_hour: Maximum tasks to process per hour (rate limiting).
                            Default 20.
        idle_timeout_seconds: Seconds of idle time before auto-shutdown.
                              Default 1800 (30 minutes).
    """

    max_tasks: Optional[int] = None
    max_duration_seconds: Optional[int] = None
    until_idle: bool = True
    dry_run: bool = False
    poll_interval: int = 30
    agent_id: str = "loop-agent-001"
    use_real_employees: bool = True
    queue_path: Optional[Path] = None
    state_path: Optional[Path] = None
    max_consecutive_failures: int = 3
    failure_cooldown_seconds: int = 300
    max_tasks_per_hour: int = 20
    idle_timeout_seconds: int = 1800


@dataclass
class LoopState:
    """Current state of the continuous loop.

    Attributes:
        started_at: Timestamp when the loop started.
        tasks_completed: Number of tasks successfully completed.
        tasks_failed: Number of tasks that failed.
        tasks_escalated: Number of tasks that were escalated.
        consecutive_idle_polls: Number of consecutive polls with no work.
        last_action: The action taken from the last iteration.
        last_error: Last error message if any.
        circuit_breaker: Circuit breaker state for failure protection.
        health_metrics: Health metrics for monitoring.
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_escalated: int = 0
    consecutive_idle_polls: int = 0
    last_action: Optional[str] = None
    last_error: Optional[str] = None
    circuit_breaker: Optional[object] = None  # CircuitBreaker, set lazily
    health_metrics: Optional[object] = None  # HealthMetrics, set lazily


@dataclass
class LoopResult:
    """Result of the continuous loop execution.

    Attributes:
        stop_reason: Why the loop stopped.
        duration: Total duration of the loop in seconds.
        final_state: Final state of the loop.
    """

    stop_reason: StopReason
    duration: float
    final_state: LoopState


# =============================================================================
# Continuous Loop Functions (Task 10.2)
# =============================================================================


def check_stop_conditions(state: LoopState, config: LoopConfig) -> Optional[StopReason]:
    """Evaluate stop conditions and return the first matching reason.

    Args:
        state: Current loop state.
        config: Loop configuration.

    Returns:
        StopReason if a stop condition is met, None otherwise.
    """
    # Check max tasks
    total_processed = state.tasks_completed + state.tasks_failed + state.tasks_escalated
    if config.max_tasks is not None and total_processed >= config.max_tasks:
        return StopReason.MAX_TASKS

    # Check max duration
    if config.max_duration_seconds is not None:
        elapsed = (datetime.now(timezone.utc) - state.started_at).total_seconds()
        if elapsed >= config.max_duration_seconds:
            return StopReason.MAX_DURATION

    # Check idle condition
    if config.until_idle and state.consecutive_idle_polls > 0:
        return StopReason.IDLE_QUEUE

    # Check idle timeout (only meaningful when not stopping immediately on idle)
    if (
        not config.until_idle
        and config.idle_timeout_seconds is not None
        and state.consecutive_idle_polls > 0
    ):
        idle_seconds = state.consecutive_idle_polls * config.poll_interval
        if idle_seconds >= config.idle_timeout_seconds:
            return StopReason.IDLE_QUEUE

    # Check rate limit (tasks per hour)
    if state.health_metrics and config.max_tasks_per_hour is not None:
        if state.health_metrics.tasks_this_hour >= config.max_tasks_per_hour:
            return StopReason.ERROR

    return None


def handle_execution_result(
    result: dict, state: LoopState, config: LoopConfig
) -> LoopAction:
    """Process poll result and determine next action.

    Updates state based on the result and returns the appropriate action.

    Args:
        result: Result dictionary from poll_and_execute_once.
        state: Current loop state (will be mutated).
        config: Loop configuration.

    Returns:
        LoopAction indicating what to do next.
    """
    action = result.get("action", "error")

    if action == "executed":
        state.tasks_completed += 1
        state.consecutive_idle_polls = 0
        state.last_action = "executed"
        state.last_error = None
        return LoopAction.CONTINUE

    elif action == "idle":
        state.consecutive_idle_polls += 1
        state.last_action = "idle"
        state.last_error = None
        # If until_idle is set, we stop on first idle
        if config.until_idle:
            return LoopAction.STOP
        # Otherwise sleep before next poll
        return LoopAction.SLEEP

    elif action == "escalated":
        state.tasks_escalated += 1
        state.consecutive_idle_polls = 0
        state.last_action = "escalated"
        state.last_error = result.get("reason")
        # Pause on escalation - requires human intervention
        return LoopAction.PAUSE

    elif action == "failed":
        state.tasks_failed += 1
        state.consecutive_idle_polls = 0
        state.last_action = "failed"
        state.last_error = result.get("reason")
        return LoopAction.CONTINUE

    elif action == "claim_failed":
        state.consecutive_idle_polls += 1
        state.last_action = "claim_failed"
        state.last_error = result.get("reason")
        # Sleep and retry on claim failure (could be contention)
        return LoopAction.SLEEP

    else:
        # Unknown action - treat as error
        state.last_action = action
        state.last_error = result.get("reason", f"Unknown action: {action}")
        return LoopAction.STOP


def run_loop(config: LoopConfig) -> LoopResult:
    """Main continuous loop entry point.

    Polls the queue and executes tasks until a stop condition is met.
    Integrates circuit breaker protection to prevent cascading failures.

    Args:
        config: Loop configuration.

    Returns:
        LoopResult with stop reason, duration, and final state.
    """
    _setup_imports()
    _ensure_loop_monitor()
    from operation_loop import get_claimable_tasks, poll_and_execute_once

    # Initialize state with circuit breaker and health metrics
    state = LoopState()
    state.circuit_breaker = CircuitBreaker()
    state.health_metrics = HealthMetrics()

    # Create circuit breaker config from loop config
    breaker_config = CircuitBreakerConfig(
        failure_threshold=config.max_consecutive_failures,
        recovery_time=config.failure_cooldown_seconds,
    )

    # Resolve paths
    queue_path = config.queue_path or _get_default_queue_path()
    state_path = config.state_path or _get_default_state_path()

    # Dry run mode - just show what would happen
    if config.dry_run:
        print("DRY RUN: Continuous loop preview")
        tasks = get_claimable_tasks(queue_path)
        print(f"Claimable tasks: {len(tasks)}")
        for t in tasks[:10]:
            task_id = t.get("task_id") or t.get("id", "unknown")
            title = t.get("title", "Untitled")
            priority = t.get("priority", 3)
            print(f"  - {task_id}: {title} (priority {priority})")
        if len(tasks) > 10:
            print(f"  ... and {len(tasks) - 10} more")
        print(
            f"\nConfig: max_tasks={config.max_tasks}, "
            f"max_duration={config.max_duration_seconds}s, "
            f"until_idle={config.until_idle}"
        )
        return LoopResult(
            stop_reason=StopReason.IDLE_QUEUE,
            duration=0.0,
            final_state=state,
        )

    if config.use_real_employees:
        print("Starting continuous loop (dynamic employee selection from org.json)")
    else:
        print(f"Starting continuous loop (agent={config.agent_id})")
    print(
        f"Config: max_tasks={config.max_tasks}, "
        f"max_duration={config.max_duration_seconds}s, "
        f"until_idle={config.until_idle}"
    )

    stop_reason: Optional[StopReason] = None

    while stop_reason is None:
        # Check stop conditions before polling
        stop_reason = check_stop_conditions(state, config)
        if stop_reason is not None:
            break

        # Check circuit breaker before task execution
        if not check_circuit_breaker(state.circuit_breaker, breaker_config):
            print("  CIRCUIT BREAKER OPEN: Too many consecutive failures")
            print(
                f"  Failure count: {state.circuit_breaker.failure_count}, "
                f"Recovery in: {config.failure_cooldown_seconds}s"
            )
            stop_reason = StopReason.ERROR
            break

        # Select employee for this task (dynamic or fixed)
        if config.use_real_employees:
            # Get next task to determine optimal employee
            claimable = get_claimable_tasks(queue_path)
            if claimable:
                next_task = claimable[0]
                employee_id = get_available_employee(next_task)
                if employee_id is None:
                    employee_id = config.agent_id  # Fallback to default
            else:
                employee_id = config.agent_id  # No tasks, use default
        else:
            employee_id = config.agent_id

        # Poll and execute one task
        result = poll_and_execute_once(queue_path, employee_id, state_path)

        # Handle the result
        loop_action = handle_execution_result(result, state, config)

        # Record success/failure in circuit breaker and health metrics
        action = result.get("action", "error")
        if action == "executed":
            record_success(state.circuit_breaker, state.health_metrics)
            save_monitor_state(state.circuit_breaker, state.health_metrics)
        elif action in ("failed", "claim_failed"):
            error_msg = result.get("reason", f"Task {action}")
            record_failure(
                state.circuit_breaker, state.health_metrics, error_msg, breaker_config
            )
            save_monitor_state(state.circuit_breaker, state.health_metrics)
            # Check if circuit breaker tripped after recording failure
            if state.circuit_breaker.state == CircuitBreakerState.OPEN:
                print("  CIRCUIT BREAKER TRIPPED: Pausing due to consecutive failures")
                stop_reason = StopReason.ERROR
                loop_action = LoopAction.PAUSE
                break

        # Log progress
        task_id = result.get("task_id", "none")
        action_str = result.get("action", "unknown")
        total = state.tasks_completed + state.tasks_failed + state.tasks_escalated
        breaker_state = state.circuit_breaker.state.value
        # Show which employee worked on the task (if dynamic selection)
        employee_info = f" by {employee_id}" if config.use_real_employees else ""
        print(
            f"[{total}] {action_str}: {task_id}{employee_info} (breaker: {breaker_state})"
        )

        # Take action
        if loop_action == LoopAction.CONTINUE:
            # Continue immediately to next iteration
            pass

        elif loop_action == LoopAction.SLEEP:
            # Sleep before next poll
            print(f"  Sleeping {config.poll_interval}s before next poll...")
            time.sleep(config.poll_interval)

        elif loop_action == LoopAction.PAUSE:
            # Stop on escalation
            stop_reason = StopReason.ESCALATION
            print("  PAUSED: Escalation requires human intervention")

        elif loop_action == LoopAction.STOP:
            # Terminal condition - determine reason
            if state.consecutive_idle_polls > 0:
                stop_reason = StopReason.IDLE_QUEUE
            elif state.last_error:
                stop_reason = StopReason.ERROR
            else:
                # Fallback - check conditions again
                stop_reason = check_stop_conditions(state, config)
                if stop_reason is None:
                    stop_reason = StopReason.IDLE_QUEUE

    # Calculate duration
    duration = (datetime.now(timezone.utc) - state.started_at).total_seconds()

    # Final summary
    print(f"\nLoop stopped: {stop_reason.value}")
    print(f"Duration: {duration:.1f}s")
    print(f"Completed: {state.tasks_completed}")
    print(f"Failed: {state.tasks_failed}")
    print(f"Escalated: {state.tasks_escalated}")
    if state.circuit_breaker:
        print(f"Circuit breaker state: {state.circuit_breaker.state.value}")
        print(f"Circuit breaker trips: {state.circuit_breaker.total_trips}")

    return LoopResult(
        stop_reason=stop_reason,
        duration=duration,
        final_state=state,
    )


# =============================================================================
# CLI Command Functions
# =============================================================================


def cmd_once(args: argparse.Namespace) -> None:
    """Execute single loop iteration."""
    _setup_imports()
    from operation_loop import (
        get_claimable_tasks,
        poll_and_execute_once,
    )

    queue_path = Path(args.queue) if args.queue else _get_default_queue_path()
    state_path = Path(args.state) if args.state else _get_default_state_path()

    # Get claimable tasks first (needed for both dry-run and employee selection)
    tasks = get_claimable_tasks(queue_path)

    # Determine employee to use
    if args.agent:
        # Explicit agent specified - use it
        employee_id = args.agent
        use_real = False
    else:
        # Try to get a real employee from org.json
        next_task = tasks[0] if tasks else None
        employee_id = get_available_employee(next_task)
        if employee_id:
            use_real = True
        else:
            # Fallback to synthetic agent
            employee_id = "loop-agent-001"
            use_real = False

    if args.dry_run:
        print("DRY RUN: Would poll queue and execute one task")
        if use_real:
            print(f"Employee selection: {employee_id} (from org.json)")
        else:
            print(f"Employee selection: {employee_id} (fallback)")
        # Show claimable tasks
        print(f"Claimable tasks: {len(tasks)}")
        for t in tasks[:5]:
            task_id = t.get("task_id") or t.get("id", "unknown")
            title = t.get("title", "Untitled")
            priority = t.get("priority", 3)
            print(f"  - {task_id}: {title} (priority {priority})")
        if len(tasks) > 5:
            print(f"  ... and {len(tasks) - 5} more")
        sys.exit(0)

    if use_real:
        print(f"Using employee: {employee_id}")

    result = poll_and_execute_once(queue_path, employee_id, state_path)
    print(json.dumps(result, indent=2))

    # Exit codes
    action = result.get("action", "error")
    if action == "executed":
        sys.exit(0)
    elif action == "idle":
        sys.exit(0)  # No work is not an error
    elif action == "escalated":
        sys.exit(2)
    else:
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Show current loop status and metrics."""
    _setup_imports()
    _ensure_loop_monitor()
    import loop_monitor
    from operation_loop import load_session_state

    state_path = Path(args.state) if args.state else _get_default_state_path()

    if not state_path.exists():
        print("=== Operation Loop Status ===")
        print("No session state found. Loop has not been run yet.")
        print(f"Expected path: {state_path}")
        sys.exit(0)

    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", {})

    print("=== Operation Loop Status ===")
    print(f"Tasks claimed:    {metrics.get('tasks_claimed', 0)}")
    print(f"Tasks completed:  {metrics.get('tasks_completed', 0)}")
    print(f"Tasks failed:     {metrics.get('tasks_failed', 0)}")
    print(f"Tasks escalated:  {metrics.get('tasks_escalated', 0)}")
    print(f"Idle polls:       {metrics.get('consecutive_idle_polls', 0)}")
    print(f"Last poll:        {metrics.get('last_poll_time', 'never')}")
    print(f"Current task:     {metrics.get('current_task_id', 'none')}")

    # Show recent errors if any
    errors = metrics.get("errors", [])
    if errors:
        print(f"\nRecent errors ({len(errors)}):")
        for err in errors[-3:]:
            print(
                f"  [{err.get('time', '?')}] {err.get('task_id', '?')}: "
                f"{err.get('error', '?')}"
            )

    # Load circuit breaker and health metrics from loop_monitor
    breaker, health_metrics = loop_monitor.load_monitor_state()
    breaker_config = loop_monitor.load_config()

    # Use default max_tasks_per_hour from LoopConfig
    max_tasks_per_hour = LoopConfig().max_tasks_per_hour
    max_consecutive_failures = LoopConfig().max_consecutive_failures

    # Circuit Breaker Status section
    print("\nCircuit Breaker Status")
    print("\u2500" * 22)
    print(f"State: {breaker.state.value.upper()}")
    print(f"Tasks this hour: {health_metrics.tasks_this_hour}/{max_tasks_per_hour}")
    print(f"Consecutive failures: {breaker.failure_count}/{max_consecutive_failures}")

    # Show time until recovery if circuit is OPEN
    if breaker.state == CircuitBreakerState.OPEN and breaker.last_failure_time:
        try:
            last_failure = datetime.fromisoformat(
                breaker.last_failure_time.replace("Z", "+00:00")
            )
            elapsed = (datetime.now(timezone.utc) - last_failure).total_seconds()
            remaining = max(0, breaker_config.recovery_time - elapsed)
            if remaining > 0:
                print(f"Recovery in: {int(remaining)}s")
            else:
                print("Recovery in: ready to attempt")
        except (ValueError, TypeError):
            print("Recovery in: unknown")

    # Show last success time
    if health_metrics.last_success_time:
        # Format timestamp for display (show just the time portion if today)
        try:
            success_dt = datetime.fromisoformat(
                health_metrics.last_success_time.replace("Z", "+00:00")
            )
            # Format as readable datetime
            print(f"Last success: {success_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except (ValueError, TypeError):
            print(f"Last success: {health_metrics.last_success_time}")
    else:
        print("Last success: never")

    # Verbose mode: show full health metrics
    if getattr(args, "verbose", False):
        print("\nHealth Metrics")
        print("\u2500" * 22)
        print(f"Total successes: {health_metrics.total_successes}")
        print(f"Total failures: {health_metrics.total_failures}")
        print(f"Uptime: {int(health_metrics.uptime_seconds)}s")
        print(f"Circuit trips: {breaker.total_trips}")


def cmd_loop(args: argparse.Namespace) -> None:
    """Execute continuous loop with configurable stop conditions."""
    # Build config from args
    config = LoopConfig(
        max_tasks=args.max_tasks,
        max_duration_seconds=args.max_duration,
        until_idle=not args.no_idle_stop,
        dry_run=args.dry_run,
        poll_interval=args.poll_interval,
        agent_id=args.agent if args.agent else "loop-agent-001",
        queue_path=Path(args.queue) if args.queue else None,
        state_path=Path(args.state) if args.state else None,
    )

    # Run the loop
    result = run_loop(config)

    # Exit codes based on stop reason
    if result.stop_reason == StopReason.IDLE_QUEUE:
        sys.exit(0)
    elif result.stop_reason == StopReason.MAX_TASKS:
        sys.exit(0)
    elif result.stop_reason == StopReason.MAX_DURATION:
        sys.exit(0)
    elif result.stop_reason == StopReason.ESCALATION:
        sys.exit(3)  # Requires human intervention
    elif result.stop_reason == StopReason.ERROR:
        sys.exit(1)
    else:
        sys.exit(1)


# =============================================================================
# Path Resolution
# =============================================================================


def _get_default_queue_path() -> Path:
    """Get the default work queue path."""
    _setup_imports()
    try:
        import company_resolver

        return company_resolver.get_company_dir() / "state/work_queue.json"
    except (ImportError, AttributeError):
        # Fallback to .company directory
        return Path(".company") / "state/work_queue.json"


def _get_default_state_path() -> Path:
    """Get the default session state path."""
    _setup_imports()
    try:
        import company_resolver

        return company_resolver.get_company_dir() / "state/session_state.json"
    except (ImportError, AttributeError):
        # Fallback to .company directory
        return Path(".company") / "state/session_state.json"


# =============================================================================
# Main Entry Point
# =============================================================================


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Operation Loop CLI (MVP + Continuous Mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run a single poll-claim-execute iteration
    python run_loop.py once

    # Preview what would be executed without doing it
    python run_loop.py once --dry-run

    # Show current loop metrics
    python run_loop.py status

    # Run continuous loop until queue is idle
    python run_loop.py loop

    # Run continuous loop with task limit
    python run_loop.py loop --max-tasks 10

    # Run continuous loop with time limit (1 hour)
    python run_loop.py loop --max-duration 3600

    # Run continuous loop without stopping on idle
    python run_loop.py loop --no-idle-stop --max-tasks 50

    # Use custom paths
    python run_loop.py once --queue /path/to/queue.json --state /path/to/state.json

Cron setup:
    # Add to crontab for 5-minute intervals:
    */5 * * * * cd /project && uv run .claude/hooks/company/run_loop.py once

Exit Codes:
    0 = Success (task executed, no tasks available, or loop completed normally)
    1 = Error during execution
    2 = Task escalated (requires human approval) - single iteration
    3 = Loop stopped due to escalation (requires human intervention)
""",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # once command
    once_parser = subparsers.add_parser(
        "once",
        help="Run single poll-claim-execute iteration",
    )
    once_parser.add_argument(
        "--queue",
        help="Path to work_queue.json (default: auto-detect via company_resolver)",
    )
    once_parser.add_argument(
        "--state",
        help="Path to session_state.json (default: auto-detect via company_resolver)",
    )
    once_parser.add_argument(
        "--agent",
        help="Agent ID for claiming tasks (default: loop-agent-001)",
    )
    once_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview claimable tasks without executing",
    )
    once_parser.set_defaults(func=cmd_once)

    # status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show loop metrics and current status",
    )
    status_parser.add_argument(
        "--state",
        help="Path to session_state.json (default: auto-detect via company_resolver)",
    )
    status_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show full health metrics including totals and uptime",
    )
    status_parser.set_defaults(func=cmd_status)

    # loop command (continuous execution)
    loop_parser = subparsers.add_parser(
        "loop",
        help="Run continuous poll-claim-execute loop",
    )
    loop_parser.add_argument(
        "--queue",
        help="Path to work_queue.json (default: auto-detect via company_resolver)",
    )
    loop_parser.add_argument(
        "--state",
        help="Path to session_state.json (default: auto-detect via company_resolver)",
    )
    loop_parser.add_argument(
        "--agent",
        help="Agent ID for claiming tasks (default: loop-agent-001)",
    )
    loop_parser.add_argument(
        "--max-tasks",
        type=int,
        help="Maximum number of tasks to process before stopping",
    )
    loop_parser.add_argument(
        "--max-duration",
        type=int,
        help="Maximum duration in seconds before stopping",
    )
    loop_parser.add_argument(
        "--no-idle-stop",
        action="store_true",
        help="Don't stop when queue becomes idle (continue polling)",
    )
    loop_parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds to wait between polls when idle (default: 30)",
    )
    loop_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview loop configuration without executing",
    )
    loop_parser.set_defaults(func=cmd_loop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
