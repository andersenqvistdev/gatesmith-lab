#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Loop Monitor — circuit breaker and health metrics for company operation loops.

This module provides protection and observability for the operation loop:

1. CircuitBreaker: Prevents cascading failures by stopping operations when
   too many consecutive failures occur. Implements the standard circuit
   breaker pattern with three states:
   - CLOSED: Normal operation, all requests pass through
   - OPEN: Failures exceeded threshold, requests are blocked
   - HALF_OPEN: Testing recovery, limited requests allowed

2. HealthMetrics: Tracks operational health indicators for monitoring:
   - Hourly task rate
   - Consecutive failure count
   - Uptime tracking
   - Success/failure timestamps

Usage:
    # Check if circuit breaker allows execution
    python loop_monitor.py check-breaker

    # Record a successful operation
    python loop_monitor.py record-success

    # Record a failed operation
    python loop_monitor.py record-failure --error "Connection timeout"

    # Get current health metrics
    python loop_monitor.py health

    # Reset circuit breaker (for recovery)
    python loop_monitor.py reset-breaker

    # Show help
    python loop_monitor.py help

Configuration:
    Default thresholds (configurable via loop_config.json):
    - failure_threshold: 5 (failures to trip breaker)
    - recovery_time: 300 (seconds before attempting recovery)
    - half_open_max_attempts: 3 (test requests in half-open state)
    - hourly_rate_window: 3600 (seconds for rate calculation)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
# Lazy import to handle both package and direct execution
company_resolver = None
efficiency_tracker = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver, efficiency_tracker
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import efficiency_tracker as et

        company_resolver = cr
        efficiency_tracker = et
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import efficiency_tracker as et  # type: ignore[no-redef]

        company_resolver = cr
        efficiency_tracker = et


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

MONITOR_FILE = "state/loop_monitor.json"
CONFIG_FILE = "config/loop_config.json"

# Default circuit breaker configuration
DEFAULT_FAILURE_THRESHOLD = 5  # Consecutive failures to trip breaker
DEFAULT_RECOVERY_TIME = 300  # Seconds before attempting recovery (5 minutes)
DEFAULT_HALF_OPEN_MAX_ATTEMPTS = 3  # Test requests in half-open state

# Default health metrics configuration
DEFAULT_HOURLY_RATE_WINDOW = 3600  # Seconds (1 hour)

# Default efficiency thresholds (G6 Economics integration)
DEFAULT_EFFICIENCY_DEGRADATION_THRESHOLD = 0.20  # 20% drop triggers warning
DEFAULT_MIN_FIRST_PASS_RATE = 0.50  # Below 50% first-pass rate = quality collapse
DEFAULT_MAX_ESCALATION_RATE = 0.30  # Above 30% escalation rate = systemic issue
DEFAULT_EFFICIENCY_CHECK_ENABLED = True  # Enable efficiency-based breaker signals


# -----------------------------------------------------------------------------
# Circuit Breaker State Enum
# -----------------------------------------------------------------------------


class CircuitBreakerState(Enum):
    """
    Circuit breaker states following the standard pattern.

    States:
        CLOSED: Normal operation. All requests pass through.
            Transitions to OPEN when failure_count >= failure_threshold.

        OPEN: Circuit is tripped. All requests are blocked.
            Transitions to HALF_OPEN when recovery_time has elapsed.

        HALF_OPEN: Testing recovery. Limited requests are allowed.
            Transitions to CLOSED on success, back to OPEN on failure.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class CircuitBreakerConfig:
    """
    Configuration for circuit breaker behavior.

    Attributes:
        failure_threshold: Number of consecutive failures before tripping.
            Default: 5
        recovery_time: Seconds to wait before attempting recovery.
            Default: 300 (5 minutes)
        half_open_max_attempts: Number of test requests in half-open state.
            Default: 3
        efficiency_check_enabled: Whether to use efficiency signals for breaker.
            Default: True
        efficiency_degradation_threshold: Efficiency drop that triggers warning.
            Default: 0.20 (20% drop)
        min_first_pass_rate: Minimum acceptable first-pass success rate.
            Default: 0.50 (50%)
        max_escalation_rate: Maximum acceptable escalation rate.
            Default: 0.30 (30%)
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_time: int = DEFAULT_RECOVERY_TIME
    half_open_max_attempts: int = DEFAULT_HALF_OPEN_MAX_ATTEMPTS
    efficiency_check_enabled: bool = DEFAULT_EFFICIENCY_CHECK_ENABLED
    efficiency_degradation_threshold: float = DEFAULT_EFFICIENCY_DEGRADATION_THRESHOLD
    min_first_pass_rate: float = DEFAULT_MIN_FIRST_PASS_RATE
    max_escalation_rate: float = DEFAULT_MAX_ESCALATION_RATE


@dataclass
class CircuitBreaker:
    """
    Circuit breaker state tracking.

    The circuit breaker protects the operation loop from cascading failures
    by blocking requests when too many consecutive failures occur.

    Attributes:
        state: Current state (CLOSED, OPEN, or HALF_OPEN).
        failure_count: Current count of consecutive failures.
        last_failure_time: ISO timestamp of the most recent failure.
            None if no failures have occurred.
        recovery_time: Seconds to wait before attempting recovery.
            Used to determine when to transition from OPEN to HALF_OPEN.
        half_open_attempts: Number of test requests made in HALF_OPEN state.
        last_state_change: ISO timestamp of the last state transition.
        total_trips: Total number of times the breaker has tripped to OPEN.
    """

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    last_failure_time: str | None = None
    recovery_time: int = DEFAULT_RECOVERY_TIME
    half_open_attempts: int = 0
    last_state_change: str | None = None
    total_trips: int = 0


@dataclass
class HealthMetrics:
    """
    Health metrics for the operation loop.

    Tracks operational health indicators for monitoring, alerting,
    and capacity planning.

    Attributes:
        tasks_this_hour: Number of tasks processed in the current hour window.
            Resets when the hour window expires.
        consecutive_failures: Current count of consecutive task failures.
            Resets to 0 on any success.
        last_success_time: ISO timestamp of the most recent successful task.
            None if no successes have occurred.
        uptime_seconds: Total seconds the loop has been running.
            Calculated from started_at to now.
        started_at: ISO timestamp when the loop started.
        last_failure_time: ISO timestamp of the most recent failure.
            None if no failures have occurred.
        total_successes: Total number of successful operations since start.
        total_failures: Total number of failed operations since start.
        hourly_timestamps: List of task completion timestamps for rate calculation.
            Used to calculate tasks_this_hour within the rolling window.
    """

    tasks_this_hour: int = 0
    consecutive_failures: int = 0
    last_success_time: str | None = None
    uptime_seconds: float = 0.0
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_failure_time: str | None = None
    total_successes: int = 0
    total_failures: int = 0
    hourly_timestamps: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_monitor_path() -> Path:
    """
    Get the loop monitor state file path.

    Returns:
        Path to .company/loop_monitor.json
    """
    _ensure_imports()
    return company_resolver.get_company_dir() / MONITOR_FILE


def get_config_path() -> Path:
    """
    Get the loop configuration file path.

    Returns:
        Path to .company/loop_config.json
    """
    _ensure_imports()
    return company_resolver.get_company_dir() / CONFIG_FILE


def ensure_company_dir():
    """Ensure the .company directory exists."""
    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Storage Functions
# -----------------------------------------------------------------------------


def load_config() -> CircuitBreakerConfig:
    """
    Load circuit breaker configuration from file.

    Falls back to defaults if file doesn't exist or is invalid.

    Returns:
        CircuitBreakerConfig with loaded or default values.
    """
    config_path = get_config_path()

    if not config_path.exists():
        return CircuitBreakerConfig()

    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
            return CircuitBreakerConfig(
                failure_threshold=data.get(
                    "failure_threshold", DEFAULT_FAILURE_THRESHOLD
                ),
                recovery_time=data.get("recovery_time", DEFAULT_RECOVERY_TIME),
                half_open_max_attempts=data.get(
                    "half_open_max_attempts", DEFAULT_HALF_OPEN_MAX_ATTEMPTS
                ),
                efficiency_check_enabled=data.get(
                    "efficiency_check_enabled", DEFAULT_EFFICIENCY_CHECK_ENABLED
                ),
                efficiency_degradation_threshold=data.get(
                    "efficiency_degradation_threshold",
                    DEFAULT_EFFICIENCY_DEGRADATION_THRESHOLD,
                ),
                min_first_pass_rate=data.get(
                    "min_first_pass_rate", DEFAULT_MIN_FIRST_PASS_RATE
                ),
                max_escalation_rate=data.get(
                    "max_escalation_rate", DEFAULT_MAX_ESCALATION_RATE
                ),
            )
    except (json.JSONDecodeError, OSError, TypeError):
        return CircuitBreakerConfig()


def save_config(config: CircuitBreakerConfig):
    """
    Save circuit breaker configuration to file.

    WS-088-001: Uses atomic write to prevent corruption during concurrent reads.

    Args:
        config: Configuration to save.
    """
    import os
    import tempfile

    ensure_company_dir()
    config_path = get_config_path()

    data = {
        "failure_threshold": config.failure_threshold,
        "recovery_time": config.recovery_time,
        "half_open_max_attempts": config.half_open_max_attempts,
        "efficiency_check_enabled": config.efficiency_check_enabled,
        "efficiency_degradation_threshold": config.efficiency_degradation_threshold,
        "min_first_pass_rate": config.min_first_pass_rate,
        "max_escalation_rate": config.max_escalation_rate,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(config_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _serialize_breaker(breaker: CircuitBreaker) -> dict:
    """Serialize CircuitBreaker to JSON-compatible dict."""
    return {
        "state": breaker.state.value,
        "failure_count": breaker.failure_count,
        "last_failure_time": breaker.last_failure_time,
        "recovery_time": breaker.recovery_time,
        "half_open_attempts": breaker.half_open_attempts,
        "last_state_change": breaker.last_state_change,
        "total_trips": breaker.total_trips,
    }


def _deserialize_breaker(data: dict) -> CircuitBreaker:
    """Deserialize CircuitBreaker from dict."""
    state_str = data.get("state", "closed")
    try:
        state = CircuitBreakerState(state_str)
    except ValueError:
        state = CircuitBreakerState.CLOSED

    return CircuitBreaker(
        state=state,
        failure_count=data.get("failure_count", 0),
        last_failure_time=data.get("last_failure_time"),
        recovery_time=data.get("recovery_time", DEFAULT_RECOVERY_TIME),
        half_open_attempts=data.get("half_open_attempts", 0),
        last_state_change=data.get("last_state_change"),
        total_trips=data.get("total_trips", 0),
    )


def _serialize_metrics(metrics: HealthMetrics) -> dict:
    """Serialize HealthMetrics to JSON-compatible dict."""
    return {
        "tasks_this_hour": metrics.tasks_this_hour,
        "consecutive_failures": metrics.consecutive_failures,
        "last_success_time": metrics.last_success_time,
        "uptime_seconds": metrics.uptime_seconds,
        "started_at": metrics.started_at,
        "last_failure_time": metrics.last_failure_time,
        "total_successes": metrics.total_successes,
        "total_failures": metrics.total_failures,
        "hourly_timestamps": metrics.hourly_timestamps,
    }


def _deserialize_metrics(data: dict) -> HealthMetrics:
    """Deserialize HealthMetrics from dict."""
    return HealthMetrics(
        tasks_this_hour=data.get("tasks_this_hour", 0),
        consecutive_failures=data.get("consecutive_failures", 0),
        last_success_time=data.get("last_success_time"),
        uptime_seconds=data.get("uptime_seconds", 0.0),
        started_at=data.get("started_at", datetime.now(timezone.utc).isoformat()),
        last_failure_time=data.get("last_failure_time"),
        total_successes=data.get("total_successes", 0),
        total_failures=data.get("total_failures", 0),
        hourly_timestamps=data.get("hourly_timestamps", []),
    )


def load_monitor_state() -> tuple[CircuitBreaker, HealthMetrics]:
    """
    Load circuit breaker and health metrics from file.

    Returns:
        Tuple of (CircuitBreaker, HealthMetrics) with loaded or default values.
    """
    monitor_path = get_monitor_path()

    if not monitor_path.exists():
        return CircuitBreaker(), HealthMetrics()

    try:
        with open(monitor_path, encoding="utf-8") as f:
            data = json.load(f)
            breaker = _deserialize_breaker(data.get("circuit_breaker", {}))
            metrics = _deserialize_metrics(data.get("health_metrics", {}))
            return breaker, metrics
    except (json.JSONDecodeError, OSError, TypeError):
        return CircuitBreaker(), HealthMetrics()


def save_monitor_state(breaker: CircuitBreaker, metrics: HealthMetrics):
    """
    Save circuit breaker and health metrics to file.

    WS-088-001: Uses atomic write to prevent corruption during concurrent reads.

    Args:
        breaker: Circuit breaker state to save.
        metrics: Health metrics to save.
    """
    import os
    import tempfile

    ensure_company_dir()
    monitor_path = get_monitor_path()

    data = {
        "circuit_breaker": _serialize_breaker(breaker),
        "health_metrics": _serialize_metrics(metrics),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(monitor_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(monitor_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# -----------------------------------------------------------------------------
# Circuit Breaker Functions
# -----------------------------------------------------------------------------


def check_circuit_breaker(
    breaker: CircuitBreaker,
    config: CircuitBreakerConfig | None = None,
) -> bool:
    """
    Check if the circuit breaker allows execution.

    This function determines whether an operation should be allowed based
    on the current circuit breaker state:

    - CLOSED: Always returns True (normal operation)
    - OPEN: Returns False unless recovery_time has elapsed, then transitions
      to HALF_OPEN and returns True
    - HALF_OPEN: Returns True to allow test request

    Args:
        breaker: Current circuit breaker state.
        config: Optional configuration. Uses defaults if not provided.

    Returns:
        True if execution is allowed, False if blocked.

    Example:
        >>> breaker = CircuitBreaker(state=CircuitBreakerState.CLOSED)
        >>> check_circuit_breaker(breaker)
        True
        >>> breaker.state = CircuitBreakerState.OPEN
        >>> check_circuit_breaker(breaker)  # Depends on recovery_time
        False  # If recovery_time hasn't elapsed
    """
    if config is None:
        config = load_config()

    now = datetime.now(timezone.utc)

    if breaker.state == CircuitBreakerState.CLOSED:
        return True

    if breaker.state == CircuitBreakerState.HALF_OPEN:
        # Enforce half-open attempt limit
        if breaker.half_open_attempts >= config.half_open_max_attempts:
            breaker.state = CircuitBreakerState.OPEN
            breaker.last_state_change = now.isoformat()
            breaker.total_trips += 1
            return False

        # Allow test requests in half-open state
        breaker.half_open_attempts += 1
        return True

    if breaker.state == CircuitBreakerState.OPEN:
        # Check if recovery time has elapsed
        if breaker.last_failure_time is not None:
            try:
                last_failure = datetime.fromisoformat(
                    breaker.last_failure_time.replace("Z", "+00:00")
                )
                elapsed = (now - last_failure).total_seconds()

                if elapsed >= config.recovery_time:
                    # Transition to half-open for recovery testing
                    breaker.state = CircuitBreakerState.HALF_OPEN
                    breaker.half_open_attempts = 0
                    breaker.last_state_change = now.isoformat()
                    return True
            except (ValueError, TypeError):
                pass

        return False

    return False


def should_recover(breaker: CircuitBreaker) -> bool:
    """
    Check if the circuit breaker should attempt recovery.

    Recovery is attempted when the breaker is OPEN and the recovery_time
    cooldown has elapsed since the last failure.

    Args:
        breaker: Current circuit breaker state.

    Returns:
        True if cooldown has elapsed and recovery should be attempted.

    Example:
        >>> breaker = CircuitBreaker(
        ...     state=CircuitBreakerState.OPEN,
        ...     last_failure_time="2026-02-10T10:00:00+00:00",
        ...     recovery_time=300
        ... )
        >>> # If current time is 10:06:00 (6 minutes later)
        >>> should_recover(breaker)
        True
    """
    if breaker.state != CircuitBreakerState.OPEN:
        return False

    if breaker.last_failure_time is None:
        return True  # No failure recorded, can recover

    now = datetime.now(timezone.utc)

    try:
        last_failure = datetime.fromisoformat(
            breaker.last_failure_time.replace("Z", "+00:00")
        )
        elapsed = (now - last_failure).total_seconds()
        return elapsed >= breaker.recovery_time
    except (ValueError, TypeError):
        return True  # Invalid timestamp, allow recovery


def record_success(breaker: CircuitBreaker, metrics: HealthMetrics) -> None:
    """
    Record a successful operation.

    Updates both circuit breaker and health metrics:
    - Circuit breaker: Resets failure count, closes circuit if in HALF_OPEN
    - Health metrics: Updates success timestamp, increments counters

    Args:
        breaker: Circuit breaker state to update.
        metrics: Health metrics to update.

    Side Effects:
        - Modifies breaker.failure_count (reset to 0)
        - Modifies breaker.state (to CLOSED if was HALF_OPEN)
        - Modifies metrics.consecutive_failures (reset to 0)
        - Modifies metrics.last_success_time
        - Modifies metrics.total_successes
        - Appends to metrics.hourly_timestamps
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Update circuit breaker
    breaker.failure_count = 0

    if breaker.state == CircuitBreakerState.HALF_OPEN:
        # Successful recovery - close the circuit
        breaker.state = CircuitBreakerState.CLOSED
        breaker.half_open_attempts = 0
        breaker.last_state_change = now_iso

    # Update health metrics
    metrics.consecutive_failures = 0
    metrics.last_success_time = now_iso
    metrics.total_successes += 1

    # Update hourly rate tracking
    metrics.hourly_timestamps.append(now_iso)
    _cleanup_hourly_timestamps(metrics)
    metrics.tasks_this_hour = len(metrics.hourly_timestamps)

    # Update uptime
    _update_uptime(metrics)


def record_failure(
    breaker: CircuitBreaker,
    metrics: HealthMetrics,
    error: str,
    config: CircuitBreakerConfig | None = None,
) -> None:
    """
    Record a failed operation.

    Updates both circuit breaker and health metrics:
    - Circuit breaker: Increments failure count, trips to OPEN if threshold reached
    - Health metrics: Updates failure timestamp, increments counters

    Args:
        breaker: Circuit breaker state to update.
        metrics: Health metrics to update.
        error: Error message describing the failure (stored for diagnostics).
        config: Optional configuration. Uses defaults if not provided.

    Side Effects:
        - Modifies breaker.failure_count (incremented)
        - Modifies breaker.last_failure_time
        - May modify breaker.state (to OPEN if threshold reached)
        - Modifies metrics.consecutive_failures (incremented)
        - Modifies metrics.last_failure_time
        - Modifies metrics.total_failures
    """
    if config is None:
        config = load_config()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Update circuit breaker
    breaker.failure_count += 1
    breaker.last_failure_time = now_iso

    if breaker.state == CircuitBreakerState.HALF_OPEN:
        # Failed during recovery testing - reopen circuit
        breaker.state = CircuitBreakerState.OPEN
        breaker.last_state_change = now_iso
        breaker.total_trips += 1

    elif breaker.state == CircuitBreakerState.CLOSED:
        # Check if threshold reached
        if breaker.failure_count >= config.failure_threshold:
            breaker.state = CircuitBreakerState.OPEN
            breaker.last_state_change = now_iso
            breaker.total_trips += 1

    # Update health metrics
    metrics.consecutive_failures += 1
    metrics.last_failure_time = now_iso
    metrics.total_failures += 1

    # Update uptime
    _update_uptime(metrics)


def reset_circuit_breaker(breaker: CircuitBreaker) -> None:
    """
    Reset the circuit breaker to initial closed state.

    Use this for manual recovery or after resolving underlying issues.

    Args:
        breaker: Circuit breaker state to reset.

    Side Effects:
        - Sets breaker.state to CLOSED
        - Resets breaker.failure_count to 0
        - Resets breaker.half_open_attempts to 0
        - Updates breaker.last_state_change
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    breaker.state = CircuitBreakerState.CLOSED
    breaker.failure_count = 0
    breaker.half_open_attempts = 0
    breaker.last_state_change = now_iso


# -----------------------------------------------------------------------------
# Health Metrics Functions
# -----------------------------------------------------------------------------


def _cleanup_hourly_timestamps(metrics: HealthMetrics) -> None:
    """
    Remove timestamps older than 1 hour from hourly tracking.

    Args:
        metrics: Health metrics to clean up.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - DEFAULT_HOURLY_RATE_WINDOW

    valid_timestamps = []
    for ts in metrics.hourly_timestamps:
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts_dt.timestamp() >= cutoff:
                valid_timestamps.append(ts)
        except (ValueError, TypeError):
            continue

    metrics.hourly_timestamps = valid_timestamps


def _update_uptime(metrics: HealthMetrics) -> None:
    """
    Update the uptime_seconds field based on started_at.

    Args:
        metrics: Health metrics to update.
    """
    now = datetime.now(timezone.utc)

    try:
        if not metrics.started_at:
            # No started_at, initialize it
            metrics.started_at = now.isoformat()
            metrics.uptime_seconds = 0.0
            return
        started = datetime.fromisoformat(metrics.started_at.replace("Z", "+00:00"))
        metrics.uptime_seconds = (now - started).total_seconds()
    except (ValueError, TypeError, AttributeError):
        # Invalid started_at, reset
        metrics.started_at = now.isoformat()
        metrics.uptime_seconds = 0.0


# -----------------------------------------------------------------------------
# Efficiency-Based Circuit Breaker (G6 Economics Integration)
# -----------------------------------------------------------------------------


def check_efficiency_health(config: CircuitBreakerConfig | None = None) -> dict:
    """
    Check efficiency metrics for circuit breaker signals.

    Queries the efficiency tracker for current company efficiency and
    determines if efficiency signals indicate systemic issues that should
    trip or warn the circuit breaker.

    Args:
        config: Optional configuration with efficiency thresholds.

    Returns:
        Dict with efficiency health indicators:
        - healthy: True if all efficiency metrics are acceptable
        - warnings: List of warning messages
        - should_trip: True if efficiency issues warrant tripping breaker
        - company_efficiency: Current company efficiency score
        - first_pass_rate: Current first-pass success rate
        - escalation_rate: Current escalation rate
        - details: Additional diagnostic information
    """
    if config is None:
        config = load_config()

    result = {
        "healthy": True,
        "warnings": [],
        "should_trip": False,
        "company_efficiency": None,
        "first_pass_rate": None,
        "escalation_rate": None,
        "details": {},
    }

    if not config.efficiency_check_enabled:
        result["details"]["reason"] = "Efficiency check disabled in config"
        return result

    try:
        _ensure_imports()

        if efficiency_tracker is None:
            result["details"]["reason"] = "Efficiency tracker not available"
            return result

        # Get efficiency insights from the tracker
        insights = efficiency_tracker.get_efficiency_insights()

        if not insights or "company" not in insights:
            result["details"]["reason"] = "No efficiency data available"
            return result

        company_data = insights.get("company", {})

        # Extract key metrics
        company_efficiency = company_data.get("efficiency_score", 1.0)
        first_pass_rate = company_data.get("first_pass_rate", 1.0)
        escalation_rate = company_data.get("escalation_rate", 0.0)
        baseline_efficiency = company_data.get(
            "baseline_efficiency", company_efficiency
        )

        result["company_efficiency"] = company_efficiency
        result["first_pass_rate"] = first_pass_rate
        result["escalation_rate"] = escalation_rate

        # Check for efficiency degradation
        if baseline_efficiency > 0:
            degradation = (
                baseline_efficiency - company_efficiency
            ) / baseline_efficiency
            if degradation >= config.efficiency_degradation_threshold:
                result["healthy"] = False
                result["warnings"].append(
                    f"Efficiency degraded {degradation:.1%} (threshold: {config.efficiency_degradation_threshold:.1%})"
                )
                result["details"]["degradation"] = degradation

        # Check first-pass success rate (quality collapse indicator)
        if first_pass_rate < config.min_first_pass_rate:
            result["healthy"] = False
            result["warnings"].append(
                f"First-pass rate {first_pass_rate:.1%} below minimum {config.min_first_pass_rate:.1%}"
            )
            # Low first-pass rate indicates systemic quality issues
            if first_pass_rate < config.min_first_pass_rate * 0.5:
                result["should_trip"] = True
                result["warnings"].append("CRITICAL: Quality collapse detected")

        # Check escalation rate (systemic problem indicator)
        if escalation_rate > config.max_escalation_rate:
            result["healthy"] = False
            result["warnings"].append(
                f"Escalation rate {escalation_rate:.1%} exceeds maximum {config.max_escalation_rate:.1%}"
            )
            # Very high escalation indicates autonomous operation is failing
            if escalation_rate > config.max_escalation_rate * 1.5:
                result["should_trip"] = True
                result["warnings"].append("CRITICAL: Excessive escalations")

        # Aggregate: trip if multiple warning indicators
        warning_count = len(result["warnings"])
        if warning_count >= 3:
            result["should_trip"] = True
            result["warnings"].append(
                f"Multiple efficiency issues detected ({warning_count} warnings)"
            )

        result["details"]["warning_count"] = warning_count
        result["details"]["checked_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        result["details"]["error"] = str(e)
        result["details"]["reason"] = "Error checking efficiency metrics"

    return result


def check_circuit_breaker_with_efficiency(
    breaker: CircuitBreaker,
    config: CircuitBreakerConfig | None = None,
    check_efficiency: bool = True,
) -> dict:
    """
    Check circuit breaker with optional efficiency-based signals.

    Combines traditional failure-count circuit breaker logic with
    efficiency-based signals from G6 Economics.

    Args:
        breaker: Current circuit breaker state.
        config: Optional configuration.
        check_efficiency: Whether to include efficiency signals.

    Returns:
        Dict with:
        - can_execute: True if execution is allowed
        - reason: Explanation of the decision
        - efficiency_health: Efficiency check results (if enabled)
        - breaker_state: Current breaker state
    """
    if config is None:
        config = load_config()

    # First check traditional circuit breaker
    can_execute = check_circuit_breaker(breaker, config)

    result = {
        "can_execute": can_execute,
        "reason": None,
        "efficiency_health": None,
        "breaker_state": breaker.state.value,
    }

    if not can_execute:
        result["reason"] = f"Circuit breaker is {breaker.state.value}"
        return result

    # If traditional check passes, also check efficiency
    if check_efficiency and config.efficiency_check_enabled:
        efficiency_health = check_efficiency_health(config)
        result["efficiency_health"] = efficiency_health

        if efficiency_health.get("should_trip"):
            result["can_execute"] = False
            result["reason"] = "Efficiency signals indicate systemic issues"

            # Don't automatically trip the breaker, but warn
            # The operation loop can decide to trip based on this signal

        elif not efficiency_health.get("healthy"):
            # Healthy=False but not trip-worthy: add warning
            result["reason"] = "Efficiency degradation detected (warning)"

    if result["can_execute"] and result["reason"] is None:
        result["reason"] = "Execution allowed"

    return result


def get_health_status(metrics: HealthMetrics) -> dict:
    """
    Get a health status summary from metrics.

    Returns:
        Dict with health indicators:
        - tasks_per_hour: Calculated hourly rate
        - consecutive_failures: Current failure streak
        - uptime_hours: Uptime in hours
        - success_rate: Percentage of successful operations
        - status: "healthy", "degraded", or "critical"
    """
    _cleanup_hourly_timestamps(metrics)
    _update_uptime(metrics)

    tasks_per_hour = len(metrics.hourly_timestamps)
    uptime_hours = metrics.uptime_seconds / 3600

    total_ops = metrics.total_successes + metrics.total_failures
    success_rate = (
        (metrics.total_successes / total_ops * 100) if total_ops > 0 else 100.0
    )

    # Determine status
    if metrics.consecutive_failures >= 5:
        status = "critical"
    elif metrics.consecutive_failures >= 2 or success_rate < 80:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "tasks_per_hour": tasks_per_hour,
        "consecutive_failures": metrics.consecutive_failures,
        "uptime_hours": round(uptime_hours, 2),
        "success_rate": round(success_rate, 2),
        "total_successes": metrics.total_successes,
        "total_failures": metrics.total_failures,
        "last_success_time": metrics.last_success_time,
        "last_failure_time": metrics.last_failure_time,
        "status": status,
    }


def get_breaker_status(breaker: CircuitBreaker) -> dict:
    """
    Get a circuit breaker status summary.

    Returns:
        Dict with breaker state information.
    """
    return {
        "state": breaker.state.value,
        "failure_count": breaker.failure_count,
        "last_failure_time": breaker.last_failure_time,
        "recovery_time": breaker.recovery_time,
        "half_open_attempts": breaker.half_open_attempts,
        "last_state_change": breaker.last_state_change,
        "total_trips": breaker.total_trips,
        "can_execute": check_circuit_breaker(breaker),
        "should_recover": should_recover(breaker),
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Loop Monitor — circuit breaker and health metrics for operation loops

Commands:
    check-breaker        Check if circuit breaker allows execution
    check-with-efficiency Check breaker with efficiency signals (G6)
    efficiency-status    Get efficiency-based health status
    record-success       Record a successful operation
    record-failure       Record a failed operation
    health               Get current health metrics
    breaker-status       Get circuit breaker status
    reset-breaker        Reset circuit breaker to closed state
    configure            Update circuit breaker configuration

Record-failure options:
    --error TEXT      Error message (required)

Configure options:
    --failure-threshold N        Failures before tripping (default: 5)
    --recovery-time N            Seconds before recovery attempt (default: 300)
    --half-open-max N            Test requests in half-open state (default: 3)
    --efficiency-check BOOL      Enable efficiency signals (default: true)
    --efficiency-degradation N   Degradation threshold (default: 0.20)
    --min-first-pass N           Minimum first-pass rate (default: 0.50)
    --max-escalation N           Maximum escalation rate (default: 0.30)

Circuit Breaker States:
    CLOSED:    Normal operation, all requests pass through
    OPEN:      Circuit tripped, requests blocked until recovery
    HALF_OPEN: Testing recovery, limited requests allowed

Efficiency Signals (G6 Economics):
    The circuit breaker can use efficiency metrics to detect systemic issues:
    - Efficiency degradation > 20%: Warning
    - First-pass rate < 50%: Quality collapse
    - Escalation rate > 30%: Systemic failure
    Multiple warnings or critical thresholds can trip the breaker.

Health Metrics:
    tasks_per_hour:       Tasks completed in the last hour
    consecutive_failures: Current failure streak
    uptime_hours:         Time since loop started
    success_rate:         Percentage of successful operations

Examples:
    # Check if execution is allowed
    python loop_monitor.py check-breaker

    # Check with efficiency signals
    python loop_monitor.py check-with-efficiency

    # Get efficiency health status
    python loop_monitor.py efficiency-status

    # Record successful operation
    python loop_monitor.py record-success

    # Record failed operation
    python loop_monitor.py record-failure --error "Connection timeout"

    # Get health summary
    python loop_monitor.py health

    # Get breaker status
    python loop_monitor.py breaker-status

    # Reset breaker after fixing issues
    python loop_monitor.py reset-breaker

    # Configure thresholds (including efficiency)
    python loop_monitor.py configure --failure-threshold 3 --min-first-pass 0.6

Output: JSON with status/metrics data.
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
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        breaker, metrics = load_monitor_state()
        config = load_config()

        if command == "check-breaker":
            can_execute = check_circuit_breaker(breaker, config)
            save_monitor_state(breaker, metrics)  # State may have changed

            result = {
                "success": True,
                "can_execute": can_execute,
                "state": breaker.state.value,
                "failure_count": breaker.failure_count,
                "message": (
                    "Execution allowed"
                    if can_execute
                    else f"Execution blocked - circuit is {breaker.state.value}"
                ),
            }
            print(json.dumps(result, indent=2))

        elif command == "record-success":
            record_success(breaker, metrics)
            save_monitor_state(breaker, metrics)

            result = {
                "success": True,
                "state": breaker.state.value,
                "consecutive_failures": metrics.consecutive_failures,
                "total_successes": metrics.total_successes,
                "tasks_this_hour": metrics.tasks_this_hour,
                "message": "Success recorded",
            }
            print(json.dumps(result, indent=2))

        elif command == "record-failure":
            if "error" not in args:
                print("Error: --error required")
                sys.exit(1)

            error_msg = str(args["error"])
            record_failure(breaker, metrics, error_msg, config)
            save_monitor_state(breaker, metrics)

            result = {
                "success": True,
                "state": breaker.state.value,
                "failure_count": breaker.failure_count,
                "consecutive_failures": metrics.consecutive_failures,
                "total_failures": metrics.total_failures,
                "circuit_tripped": breaker.state == CircuitBreakerState.OPEN,
                "message": (
                    "Failure recorded - circuit TRIPPED"
                    if breaker.state == CircuitBreakerState.OPEN
                    else "Failure recorded"
                ),
            }
            print(json.dumps(result, indent=2))

        elif command == "health":
            health_status = get_health_status(metrics)
            save_monitor_state(breaker, metrics)  # Updated uptime

            result = {
                "success": True,
                "health": health_status,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps(result, indent=2))

        elif command == "breaker-status":
            breaker_status = get_breaker_status(breaker)

            result = {
                "success": True,
                "breaker": breaker_status,
                "config": {
                    "failure_threshold": config.failure_threshold,
                    "recovery_time": config.recovery_time,
                    "half_open_max_attempts": config.half_open_max_attempts,
                    "efficiency_check_enabled": config.efficiency_check_enabled,
                    "efficiency_degradation_threshold": config.efficiency_degradation_threshold,
                    "min_first_pass_rate": config.min_first_pass_rate,
                    "max_escalation_rate": config.max_escalation_rate,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps(result, indent=2))

        elif command == "check-with-efficiency":
            check_result = check_circuit_breaker_with_efficiency(
                breaker, config, check_efficiency=True
            )
            save_monitor_state(breaker, metrics)  # State may have changed

            result = {
                "success": True,
                "can_execute": check_result["can_execute"],
                "reason": check_result["reason"],
                "state": breaker.state.value,
                "efficiency_health": check_result.get("efficiency_health"),
                "message": (
                    "Execution allowed"
                    if check_result["can_execute"]
                    else f"Execution blocked: {check_result['reason']}"
                ),
            }
            print(json.dumps(result, indent=2))

        elif command == "efficiency-status":
            efficiency_health = check_efficiency_health(config)

            result = {
                "success": True,
                "efficiency": efficiency_health,
                "config": {
                    "efficiency_check_enabled": config.efficiency_check_enabled,
                    "efficiency_degradation_threshold": config.efficiency_degradation_threshold,
                    "min_first_pass_rate": config.min_first_pass_rate,
                    "max_escalation_rate": config.max_escalation_rate,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps(result, indent=2))

        elif command == "reset-breaker":
            reset_circuit_breaker(breaker)
            save_monitor_state(breaker, metrics)

            result = {
                "success": True,
                "state": breaker.state.value,
                "message": "Circuit breaker reset to CLOSED state",
            }
            print(json.dumps(result, indent=2))

        elif command == "configure":
            # Update configuration based on args
            if "failure_threshold" in args:
                try:
                    config.failure_threshold = int(args["failure_threshold"])
                except ValueError:
                    print("Error: --failure-threshold must be a number")
                    sys.exit(1)

            if "recovery_time" in args:
                try:
                    config.recovery_time = int(args["recovery_time"])
                except ValueError:
                    print("Error: --recovery-time must be a number")
                    sys.exit(1)

            if "half_open_max" in args:
                try:
                    config.half_open_max_attempts = int(args["half_open_max"])
                except ValueError:
                    print("Error: --half-open-max must be a number")
                    sys.exit(1)

            # Efficiency-related configuration
            if "efficiency_check" in args:
                val = str(args["efficiency_check"]).lower()
                config.efficiency_check_enabled = val in ("true", "1", "yes", "on")

            if "efficiency_degradation" in args:
                try:
                    config.efficiency_degradation_threshold = float(
                        args["efficiency_degradation"]
                    )
                except ValueError:
                    print("Error: --efficiency-degradation must be a number")
                    sys.exit(1)

            if "min_first_pass" in args:
                try:
                    config.min_first_pass_rate = float(args["min_first_pass"])
                except ValueError:
                    print("Error: --min-first-pass must be a number")
                    sys.exit(1)

            if "max_escalation" in args:
                try:
                    config.max_escalation_rate = float(args["max_escalation"])
                except ValueError:
                    print("Error: --max-escalation must be a number")
                    sys.exit(1)

            save_config(config)

            result = {
                "success": True,
                "config": {
                    "failure_threshold": config.failure_threshold,
                    "recovery_time": config.recovery_time,
                    "half_open_max_attempts": config.half_open_max_attempts,
                    "efficiency_check_enabled": config.efficiency_check_enabled,
                    "efficiency_degradation_threshold": config.efficiency_degradation_threshold,
                    "min_first_pass_rate": config.min_first_pass_rate,
                    "max_escalation_rate": config.max_escalation_rate,
                },
                "message": "Configuration updated",
            }
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
