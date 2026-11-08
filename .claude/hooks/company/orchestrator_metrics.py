#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Orchestrator Metrics — Track routing decisions and pipeline execution for daemon integration.

This module provides metrics collection for the BMAD/GSD Central Orchestrator,
tracking how tasks are routed through complexity levels, execution modes, and
pipeline stages.

Metrics tracked:
- tasks_routed: Total number of tasks routed through the orchestrator
- by_complexity: Counts by complexity level (trivial/standard/complex/epic)
- by_execution_mode: Counts by execution mode (direct/plan_execute/full_pipeline)
- pipeline_stage_counts: How many tasks reached each pipeline stage
- gate_pass_rate: Percentage of tasks that pass human gates
- avg_routing_time_ms: Average time to make routing decisions

Storage:
- Persists to .company/orchestrator_metrics.json
- Uses atomic writes (tempfile + os.replace) for safety
- 7-day rolling window for time-series data

Usage:
    # Track a routing decision
    python orchestrator_metrics.py track --plan '{"task_id": "t1", "complexity": {...}}'

    # Get metrics summary (for dashboard)
    python orchestrator_metrics.py summary

    # Get detailed report
    python orchestrator_metrics.py report [--days 7]

    # Reset metrics (for testing)
    python orchestrator_metrics.py reset

    # Show help
    python orchestrator_metrics.py help

Dashboard Integration:
    The get_metrics_summary() function returns a dict compatible with
    dashboard_aggregator.py for health monitoring and insights.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

COMPANY_DIR = ".company"
METRICS_FILE = "state/orchestrator_metrics.json"
ROLLING_WINDOW_DAYS = 7


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class OrchestratorMetrics:
    """Metrics for orchestrator routing decisions.

    Tracks how tasks flow through the orchestrator, providing visibility into:
    - Which complexity levels are most common
    - Which execution modes are used
    - Pipeline stage progression
    - Gate pass/fail rates
    - Routing performance
    """

    # Counters
    tasks_routed: int = 0

    # Distribution by complexity level
    by_complexity: dict[str, int] = field(
        default_factory=lambda: {
            "trivial": 0,
            "standard": 0,
            "complex": 0,
            "epic": 0,
        }
    )

    # Distribution by execution mode
    by_execution_mode: dict[str, int] = field(
        default_factory=lambda: {
            "direct": 0,
            "plan_execute": 0,
            "full_pipeline": 0,
        }
    )

    # Pipeline stage counts (how many tasks reached each stage)
    pipeline_stage_counts: dict[str, int] = field(
        default_factory=lambda: {
            "discuss": 0,
            "plan": 0,
            "check_plan": 0,
            "implement": 0,
            "gate": 0,
            "review": 0,
            "test": 0,
            "security_audit": 0,
        }
    )

    # Gate metrics
    gate_attempts: int = 0
    gate_passes: int = 0

    # Performance metrics
    routing_times_ms: list[float] = field(default_factory=list)

    # Time-series for trends (daily aggregates)
    daily_routed: list[dict[str, Any]] = field(default_factory=list)

    # Metadata
    created_at: str = ""
    last_updated: str = ""

    def __post_init__(self):
        """Initialize timestamps if not set."""
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_updated:
            self.last_updated = now

    @property
    def gate_pass_rate(self) -> float:
        """Calculate gate pass rate as a percentage."""
        if self.gate_attempts == 0:
            return 100.0  # No gates attempted = 100% pass
        return round((self.gate_passes / self.gate_attempts) * 100, 2)

    @property
    def avg_routing_time_ms(self) -> float:
        """Calculate average routing time in milliseconds."""
        if not self.routing_times_ms:
            return 0.0
        return round(sum(self.routing_times_ms) / len(self.routing_times_ms), 2)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "tasks_routed": self.tasks_routed,
            "by_complexity": self.by_complexity.copy(),
            "by_execution_mode": self.by_execution_mode.copy(),
            "pipeline_stage_counts": self.pipeline_stage_counts.copy(),
            "gate_attempts": self.gate_attempts,
            "gate_passes": self.gate_passes,
            "gate_pass_rate": self.gate_pass_rate,
            "routing_times_ms": self.routing_times_ms.copy(),
            "avg_routing_time_ms": self.avg_routing_time_ms,
            "daily_routed": self.daily_routed.copy(),
            "created_at": self.created_at,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrchestratorMetrics":
        """Create from dictionary (JSON deserialization)."""
        return cls(
            tasks_routed=data.get("tasks_routed", 0),
            by_complexity=data.get(
                "by_complexity",
                {"trivial": 0, "standard": 0, "complex": 0, "epic": 0},
            ),
            by_execution_mode=data.get(
                "by_execution_mode",
                {"direct": 0, "plan_execute": 0, "full_pipeline": 0},
            ),
            pipeline_stage_counts=data.get(
                "pipeline_stage_counts",
                {
                    "discuss": 0,
                    "plan": 0,
                    "check_plan": 0,
                    "implement": 0,
                    "gate": 0,
                    "review": 0,
                    "test": 0,
                    "security_audit": 0,
                },
            ),
            gate_attempts=data.get("gate_attempts", 0),
            gate_passes=data.get("gate_passes", 0),
            routing_times_ms=data.get("routing_times_ms", []),
            daily_routed=data.get("daily_routed", []),
            created_at=data.get("created_at", ""),
            last_updated=data.get("last_updated", ""),
        )


# =============================================================================
# Metrics Tracker
# =============================================================================


class OrchestratorMetricsTracker:
    """Track orchestrator routing decisions with persistence.

    Uses atomic writes to prevent corruption during concurrent access.
    Maintains a 7-day rolling window for time-series data.
    """

    def __init__(self, company_dir: str | Path | None = None):
        """Initialize tracker with optional custom company directory.

        Args:
            company_dir: Path to company directory. Defaults to .company in cwd.
        """
        if company_dir:
            self._company_dir = Path(company_dir)
        else:
            self._company_dir = Path(os.getcwd()) / COMPANY_DIR

        self._metrics_path = self._company_dir / METRICS_FILE
        self._metrics: OrchestratorMetrics | None = None

    @property
    def metrics_path(self) -> Path:
        """Get the path to the metrics file."""
        return self._metrics_path

    def _ensure_company_dir(self) -> None:
        """Ensure the company directory and state subdirectory exist."""
        self._company_dir.mkdir(parents=True, exist_ok=True)
        self._metrics_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> OrchestratorMetrics:
        """Load metrics from file.

        Returns:
            OrchestratorMetrics instance (empty if file doesn't exist)
        """
        if self._metrics is not None:
            return self._metrics

        if not self._metrics_path.exists():
            self._metrics = OrchestratorMetrics()
            return self._metrics

        try:
            with open(self._metrics_path, encoding="utf-8") as f:
                data = json.load(f)
                self._metrics = OrchestratorMetrics.from_dict(data)
        except (json.JSONDecodeError, OSError):
            # On parse failure, retry once after brief delay (race condition handling)
            time.sleep(0.05)
            try:
                with open(self._metrics_path, encoding="utf-8") as f:
                    data = json.load(f)
                    self._metrics = OrchestratorMetrics.from_dict(data)
            except (json.JSONDecodeError, OSError):
                # Still failing, return empty metrics
                self._metrics = OrchestratorMetrics()

        return self._metrics

    def save(self) -> None:
        """Save metrics to file with atomic write.

        Uses tempfile + os.replace pattern to prevent corruption.
        Also applies rolling window cleanup for time-series data.
        """
        if self._metrics is None:
            return

        self._ensure_company_dir()

        # Apply rolling window cleanup
        self._apply_rolling_window()

        # Update timestamp
        self._metrics.last_updated = datetime.now(timezone.utc).isoformat()

        # Atomic write: write to temp file, then replace
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._company_dir),
            prefix=".orchestrator_metrics_",
            suffix=".json.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._metrics.to_dict(), f, indent=2)

            # Atomic replace
            os.replace(tmp_path, self._metrics_path)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _apply_rolling_window(self) -> None:
        """Remove data older than the rolling window."""
        if self._metrics is None:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        # Filter daily_routed
        self._metrics.daily_routed = [
            entry
            for entry in self._metrics.daily_routed
            if entry.get("date", "") >= cutoff_str
        ]

        # Keep only recent routing times (last 1000)
        if len(self._metrics.routing_times_ms) > 1000:
            self._metrics.routing_times_ms = self._metrics.routing_times_ms[-1000:]

    def track_routing_decision(
        self,
        plan: dict[str, Any],
        routing_time_ms: float | None = None,
    ) -> dict[str, Any]:
        """Track a routing decision from the orchestrator.

        Args:
            plan: ExecutionPlan as dict (from orchestrator.route_task)
            routing_time_ms: Optional time taken for routing decision

        Returns:
            Dict with tracking result
        """
        metrics = self.load()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Increment total counter
        metrics.tasks_routed += 1

        # Track complexity
        complexity = plan.get("complexity", {})
        complexity_level = complexity.get("level", "standard")
        if complexity_level in metrics.by_complexity:
            metrics.by_complexity[complexity_level] += 1

        # Track execution mode
        execution_mode = plan.get("execution_mode", "plan_execute")
        if execution_mode in metrics.by_execution_mode:
            metrics.by_execution_mode[execution_mode] += 1

        # Track pipeline stages
        pipeline = plan.get("pipeline", [])
        for stage in pipeline:
            if stage in metrics.pipeline_stage_counts:
                metrics.pipeline_stage_counts[stage] += 1

        # Track routing time
        if routing_time_ms is not None:
            metrics.routing_times_ms.append(routing_time_ms)

        # Update daily aggregate
        self._update_daily_routed(today)

        # Save
        self.save()

        return {
            "success": True,
            "task_id": plan.get("task_id"),
            "complexity": complexity_level,
            "execution_mode": execution_mode,
            "tasks_routed_total": metrics.tasks_routed,
            "tracked_at": now.isoformat(),
        }

    def _update_daily_routed(self, date: str) -> None:
        """Update or create daily routed entry."""
        if self._metrics is None:
            return

        # Find existing entry for today
        for entry in self._metrics.daily_routed:
            if entry.get("date") == date:
                entry["count"] = entry.get("count", 0) + 1
                return

        # Create new entry
        self._metrics.daily_routed.append({"date": date, "count": 1})

    def track_gate_result(self, passed: bool) -> dict[str, Any]:
        """Track a gate check result.

        Args:
            passed: Whether the task passed the gate

        Returns:
            Dict with tracking result
        """
        metrics = self.load()

        metrics.gate_attempts += 1
        if passed:
            metrics.gate_passes += 1

        self.save()

        return {
            "success": True,
            "passed": passed,
            "gate_pass_rate": metrics.gate_pass_rate,
            "total_gate_attempts": metrics.gate_attempts,
        }

    def get_metrics_summary(self) -> dict[str, Any]:
        """Get metrics summary for dashboard integration.

        Returns a dict compatible with dashboard_aggregator.py.

        Returns:
            Dict with summary metrics
        """
        metrics = self.load()

        # Calculate dominant complexity
        complexity_counts = metrics.by_complexity
        dominant_complexity = max(
            complexity_counts.keys(),
            key=lambda k: complexity_counts.get(k, 0),
            default="standard",
        )

        # Calculate dominant execution mode
        mode_counts = metrics.by_execution_mode
        dominant_mode = max(
            mode_counts.keys(),
            key=lambda k: mode_counts.get(k, 0),
            default="plan_execute",
        )

        # Calculate daily velocity (tasks routed per day)
        daily_velocity = 0.0
        if metrics.daily_routed:
            total_days = len(metrics.daily_routed)
            total_tasks = sum(d.get("count", 0) for d in metrics.daily_routed)
            daily_velocity = (
                round(total_tasks / total_days, 2) if total_days > 0 else 0.0
            )

        return {
            "success": True,
            "tasks_routed": metrics.tasks_routed,
            "by_complexity": metrics.by_complexity.copy(),
            "by_execution_mode": metrics.by_execution_mode.copy(),
            "pipeline_stage_counts": metrics.pipeline_stage_counts.copy(),
            "gate_metrics": {
                "attempts": metrics.gate_attempts,
                "passes": metrics.gate_passes,
                "pass_rate": metrics.gate_pass_rate,
            },
            "performance": {
                "avg_routing_time_ms": metrics.avg_routing_time_ms,
                "routing_samples": len(metrics.routing_times_ms),
            },
            "insights": {
                "dominant_complexity": dominant_complexity,
                "dominant_execution_mode": dominant_mode,
                "daily_velocity": daily_velocity,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_detailed_report(self, days: int = 7) -> dict[str, Any]:
        """Get detailed metrics report.

        Args:
            days: Number of days to include in time-series

        Returns:
            Dict with detailed metrics and trends
        """
        metrics = self.load()
        summary = self.get_metrics_summary()

        # Filter daily data to requested window
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d"
        )
        daily_filtered = [
            entry for entry in metrics.daily_routed if entry.get("date", "") >= cutoff
        ]

        # Sort by date
        daily_filtered.sort(key=lambda x: x.get("date", ""))

        # Calculate trends
        if len(daily_filtered) >= 2:
            first_half = daily_filtered[: len(daily_filtered) // 2]
            second_half = daily_filtered[len(daily_filtered) // 2 :]

            first_avg = (
                sum(d.get("count", 0) for d in first_half) / len(first_half)
                if first_half
                else 0
            )
            second_avg = (
                sum(d.get("count", 0) for d in second_half) / len(second_half)
                if second_half
                else 0
            )

            if first_avg > 0:
                trend_pct = round(((second_avg - first_avg) / first_avg) * 100, 1)
            else:
                trend_pct = 0.0

            trend = (
                "increasing"
                if trend_pct > 5
                else "decreasing"
                if trend_pct < -5
                else "stable"
            )
        else:
            trend = "insufficient_data"
            trend_pct = 0.0

        return {
            "success": True,
            "summary": summary,
            "daily_routed": daily_filtered,
            "trends": {
                "direction": trend,
                "change_pct": trend_pct,
            },
            "period_days": days,
            "metadata": {
                "created_at": metrics.created_at,
                "last_updated": metrics.last_updated,
            },
        }

    def reset(self) -> dict[str, Any]:
        """Reset all metrics (for testing).

        Returns:
            Dict confirming reset
        """
        self._metrics = OrchestratorMetrics()
        self.save()

        return {
            "success": True,
            "message": "Orchestrator metrics reset",
            "reset_at": datetime.now(timezone.utc).isoformat(),
        }


# =============================================================================
# Module-Level Functions (for easy import by dashboard_aggregator)
# =============================================================================


_tracker: OrchestratorMetricsTracker | None = None


def _get_tracker() -> OrchestratorMetricsTracker:
    """Get or create the singleton tracker."""
    global _tracker
    if _tracker is None:
        _tracker = OrchestratorMetricsTracker()
    return _tracker


def track_routing_decision(
    plan: dict[str, Any],
    routing_time_ms: float | None = None,
) -> dict[str, Any]:
    """Track a routing decision from the orchestrator.

    Args:
        plan: ExecutionPlan as dict (from orchestrator.route_task)
        routing_time_ms: Optional time taken for routing decision

    Returns:
        Dict with tracking result
    """
    return _get_tracker().track_routing_decision(plan, routing_time_ms)


def track_gate_result(passed: bool) -> dict[str, Any]:
    """Track a gate check result.

    Args:
        passed: Whether the task passed the gate

    Returns:
        Dict with tracking result
    """
    return _get_tracker().track_gate_result(passed)


def get_metrics_summary() -> dict[str, Any]:
    """Get metrics summary for dashboard integration.

    Returns:
        Dict with summary metrics compatible with dashboard_aggregator
    """
    return _get_tracker().get_metrics_summary()


def get_detailed_report(days: int = 7) -> dict[str, Any]:
    """Get detailed metrics report.

    Args:
        days: Number of days to include in time-series

    Returns:
        Dict with detailed metrics and trends
    """
    return _get_tracker().get_detailed_report(days)


# =============================================================================
# CLI Interface
# =============================================================================


def print_help() -> None:
    """Print usage help."""
    help_text = """
Orchestrator Metrics — Track routing decisions and pipeline execution

Commands:
    track       Track a routing decision
    gate        Track a gate result
    summary     Get metrics summary (dashboard-compatible)
    report      Get detailed report with trends
    reset       Reset all metrics (for testing)
    help        Show this help

Track options:
    --plan JSON         ExecutionPlan as JSON string (required)
    --time-ms FLOAT     Routing time in milliseconds

Gate options:
    --passed            Gate was passed (default: failed)

Report options:
    --days N            Number of days to include (default: 7)

Examples:
    # Track a routing decision
    python orchestrator_metrics.py track --plan '{"task_id":"t1","complexity":{"level":"standard"},"execution_mode":"plan_execute","pipeline":["plan","implement","review","test"]}'

    # Track with routing time
    python orchestrator_metrics.py track --plan '{"task_id":"t2","complexity":{"level":"complex"},"execution_mode":"full_pipeline","pipeline":["discuss","plan","check_plan","implement","gate","review","test"]}' --time-ms 15.5

    # Track a gate pass
    python orchestrator_metrics.py gate --passed

    # Get dashboard summary
    python orchestrator_metrics.py summary

    # Get 14-day report
    python orchestrator_metrics.py report --days 14

    # Reset metrics
    python orchestrator_metrics.py reset

Output: JSON with metrics data.
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


def main() -> None:
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        tracker = OrchestratorMetricsTracker()

        if command == "track":
            if "plan" not in args:
                print(json.dumps({"success": False, "error": "--plan required"}))
                sys.exit(1)

            try:
                plan = json.loads(args["plan"])
            except json.JSONDecodeError as e:
                print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
                sys.exit(1)

            time_ms = None
            if "time_ms" in args:
                try:
                    time_ms = float(args["time_ms"])
                except ValueError:
                    print(
                        json.dumps(
                            {"success": False, "error": "--time-ms must be a number"}
                        )
                    )
                    sys.exit(1)

            result = tracker.track_routing_decision(plan, time_ms)
            print(json.dumps(result, indent=2))

        elif command == "gate":
            passed = args.get("passed", False) is True
            result = tracker.track_gate_result(passed)
            print(json.dumps(result, indent=2))

        elif command == "summary":
            result = tracker.get_metrics_summary()
            print(json.dumps(result, indent=2))

        elif command == "report":
            days = 7
            if "days" in args:
                try:
                    days = int(args["days"])
                except ValueError:
                    print(
                        json.dumps(
                            {"success": False, "error": "--days must be a number"}
                        )
                    )
                    sys.exit(1)

            result = tracker.get_detailed_report(days)
            print(json.dumps(result, indent=2))

        elif command == "reset":
            result = tracker.reset()
            print(json.dumps(result, indent=2))

        else:
            print(
                json.dumps({"success": False, "error": f"Unknown command: {command}"})
            )
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
