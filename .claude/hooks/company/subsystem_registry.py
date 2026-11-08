"""
Subsystem Registry — unified interface for daemon subsystem management.

Implements the Subsystem Pattern for P38 (Daemon Heartbeat Unification).

Each daemon subsystem (circuit breaker, adaptive scheduler, budget governor,
strategic planner, etc.) implements the DaemonSubsystem protocol, providing
a standard interface for heartbeat serialization, status reporting, and
health checks. The SubsystemRegistry aggregates these into a single view.

This enables:
1. Heartbeat files that include ALL subsystem state (not just top-level)
2. Daemon status output that merges subsystem sections automatically
3. Health checks that surface degraded subsystems without polling each one

Usage:
    from subsystem_registry import SubsystemRegistry, SubsystemHealth

    registry = SubsystemRegistry()
    registry.register(my_subsystem)

    # Aggregate all heartbeat fields
    heartbeat = registry.aggregate_heartbeat()

    # Get health of all subsystems
    health = registry.aggregate_health()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# -----------------------------------------------------------------------------
# Health Data
# -----------------------------------------------------------------------------


@dataclass
class SubsystemHealth:
    """Health assessment for a single daemon subsystem.

    Attributes:
        status: Current health status.
        message: Human-readable description of health state.
        last_active: ISO-8601 timestamp of last activity, or None if unknown.
        metrics: Subsystem-specific KPIs (e.g., failure_count, queue_depth).
    """

    status: Literal["healthy", "degraded", "unhealthy", "unknown"]
    message: str
    last_active: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for JSON output."""
        return {
            "status": self.status,
            "message": self.message,
            "last_active": self.last_active,
            "metrics": self.metrics,
        }


# -----------------------------------------------------------------------------
# Subsystem Protocol
# -----------------------------------------------------------------------------


@runtime_checkable
class DaemonSubsystem(Protocol):
    """Protocol that each daemon subsystem must satisfy.

    This uses structural (duck) typing — any object with the right attributes
    and methods matches, no inheritance required.

    Attributes:
        name: Unique identifier for the subsystem (e.g., "circuit_breaker").

    Methods:
        heartbeat_fields: Return fields to include in the daemon heartbeat file.
        status_section: Return fields for the ``daemon_status()`` output.
        health_check: Return current health assessment.
    """

    @property
    def name(self) -> str:
        """Unique subsystem identifier."""
        ...

    def heartbeat_fields(self) -> dict[str, Any]:
        """Return key-value pairs to merge into the heartbeat payload."""
        ...

    def status_section(self) -> dict[str, Any]:
        """Return key-value pairs for the daemon status report."""
        ...

    def health_check(self) -> SubsystemHealth:
        """Assess current subsystem health."""
        ...


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------


class SubsystemRegistry:
    """Central registry for daemon subsystems.

    Provides registration, lookup, and aggregation across all registered
    subsystems. Designed to be instantiated once and shared within the
    daemon process.
    """

    def __init__(self) -> None:
        self._subsystems: dict[str, DaemonSubsystem] = {}

    # -- Lifecycle ------------------------------------------------------------

    def register(self, subsystem: DaemonSubsystem) -> None:
        """Register a subsystem. Replaces any existing subsystem with the same name."""
        self._subsystems[subsystem.name] = subsystem

    def unregister(self, name: str) -> None:
        """Remove a subsystem by name. No-op if not registered."""
        self._subsystems.pop(name, None)

    # -- Lookup ---------------------------------------------------------------

    def get_all(self) -> list[DaemonSubsystem]:
        """Return all registered subsystems in registration order."""
        return list(self._subsystems.values())

    def get(self, name: str) -> DaemonSubsystem | None:
        """Return a subsystem by name, or None if not registered."""
        return self._subsystems.get(name)

    # -- Aggregation ----------------------------------------------------------

    def aggregate_heartbeat(self) -> dict[str, Any]:
        """Merge heartbeat_fields() from all subsystems into one flat dict.

        Fields are merged flat (not nested) for backward compatibility with
        the existing heartbeat format. Each subsystem must use unique keys.
        """
        result: dict[str, Any] = {}
        for subsystem in self._subsystems.values():
            result.update(subsystem.heartbeat_fields())
        return result

    def aggregate_status(self) -> dict[str, Any]:
        """Merge status_section() from all subsystems into one dict.

        Each subsystem returns its own nested section (e.g.,
        ``{"cross_project": {...}}``), and these are merged flat into
        the result — matching the existing ``daemon_status()`` format.
        """
        result: dict[str, Any] = {}
        for subsystem in self._subsystems.values():
            result.update(subsystem.status_section())
        return result

    def aggregate_health(self) -> dict[str, SubsystemHealth]:
        """Collect health_check() from all subsystems.

        Returns:
            Mapping of subsystem name to its SubsystemHealth.
        """
        result: dict[str, SubsystemHealth] = {}
        for subsystem in self._subsystems.values():
            result[subsystem.name] = subsystem.health_check()
        return result


# -----------------------------------------------------------------------------
# Subsystem Wrappers
# -----------------------------------------------------------------------------
#
# Each wrapper implements the DaemonSubsystem protocol. They accept ``state``
# and ``config`` objects via duck typing (no direct import of DaemonState or
# DaemonConfig) to avoid circular imports with forge_daemon.py.
# Attribute access uses ``getattr`` with sensible defaults for safety.
# -----------------------------------------------------------------------------


class TaskExecutionSubsystem:
    """Core task execution and GSD planning subsystem."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "task_execution"

    def heartbeat_fields(self) -> dict[str, Any]:
        completed = getattr(self._state, "tasks_completed", 0)
        failed = getattr(self._state, "tasks_failed", 0)
        return {
            "current_task": getattr(self._state, "current_task", None),
            "tasks_completed_this_session": completed,
            "tasks_failed_this_session": failed,
            "poll_count": completed + failed,
            "tasks_planned": getattr(self._state, "tasks_planned", 0),
            "tasks_direct": getattr(self._state, "tasks_direct", 0),
            "planning_failures": getattr(self._state, "planning_failures", 0),
        }

    def status_section(self) -> dict[str, Any]:
        # Top-level fields in daemon_status, not a nested section.
        return {}

    def health_check(self) -> SubsystemHealth:
        failed = getattr(self._state, "tasks_failed", 0)
        completed = getattr(self._state, "tasks_completed", 0)
        if completed == 0 and failed == 0:
            return SubsystemHealth(status="healthy", message="No tasks processed yet")
        if failed < completed:
            return SubsystemHealth(
                status="healthy",
                message=f"{completed} completed, {failed} failed",
                metrics={"completed": completed, "failed": failed},
            )
        return SubsystemHealth(
            status="degraded",
            message=f"Failure rate high: {failed} failed >= {completed} completed",
            metrics={"completed": completed, "failed": failed},
        )


class ProactiveSubsystem:
    """Proactive initiative scanning subsystem."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "proactive"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_proactive_scan": getattr(self._state, "last_proactive_scan", ""),
            "proactive_proposals_created": getattr(
                self._state, "proactive_proposals_created", 0
            ),
        }

    def status_section(self) -> dict[str, Any]:
        # Top-level fields in daemon_status, not a nested section.
        return {}

    def health_check(self) -> SubsystemHealth:
        last_scan = getattr(self._state, "last_proactive_scan", "")
        if last_scan:
            return SubsystemHealth(
                status="healthy",
                message="Proactive scanning active",
                last_active=last_scan,
            )
        return SubsystemHealth(
            status="unknown",
            message="Proactive scanning has not run yet",
        )


class CrossProjectSubsystem:
    """Cross-project task routing subsystem (P14)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "cross_project"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "cross_project_tasks_routed": getattr(
                self._state, "cross_project_tasks_routed", 0
            ),
            "cross_project_tasks_queued": getattr(
                self._state, "cross_project_tasks_queued", 0
            ),
            "last_rebalance_check": getattr(self._state, "last_rebalance_check", ""),
            "rebalance_proposals_created": getattr(
                self._state, "rebalance_proposals_created", 0
            ),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "cross_project": {
                "enabled": getattr(self._config, "cross_project_enabled", True),
                "tasks_routed": getattr(self._state, "cross_project_tasks_routed", 0),
                "tasks_queued_for_approval": getattr(
                    self._state, "cross_project_tasks_queued", 0
                ),
                "last_rebalance_check": getattr(
                    self._state, "last_rebalance_check", None
                )
                or None,
                "rebalance_proposals": getattr(
                    self._state, "rebalance_proposals_created", 0
                ),
                "cross_project_task_count": 0,
                "employee_distribution": {},
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "cross_project_enabled", True)
        if not enabled:
            return SubsystemHealth(status="unknown", message="Cross-project disabled")
        last_check = getattr(self._state, "last_rebalance_check", "")
        if last_check:
            return SubsystemHealth(
                status="healthy",
                message="Cross-project routing active",
                last_active=last_check,
            )
        return SubsystemHealth(
            status="unknown",
            message="Cross-project routing has not run yet",
        )


class StrategicPlanningSubsystem:
    """Strategic planning subsystem (P15/P29)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "strategic_planning"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_strategic_planning": getattr(
                self._state, "last_strategic_planning", ""
            ),
            "strategic_initiatives_created": getattr(
                self._state, "strategic_initiatives_created", 0
            ),
            "strategic_tasks_queued": getattr(self._state, "strategic_tasks_queued", 0),
            "last_weekly_planning": getattr(self._state, "last_weekly_planning", ""),
            "last_daily_planning": getattr(self._state, "last_daily_planning", ""),
            "weekly_planning_runs": getattr(self._state, "weekly_planning_runs", 0),
            "daily_planning_runs": getattr(self._state, "daily_planning_runs", 0),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "strategic_planning": {
                "enabled": getattr(self._config, "strategic_planning_enabled", True),
                "last_run": getattr(self._state, "last_strategic_planning", None)
                or None,
                "initiatives_created": getattr(
                    self._state, "strategic_initiatives_created", 0
                ),
                "tasks_queued": getattr(self._state, "strategic_tasks_queued", 0),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "strategic_planning_enabled", True)
        if not enabled:
            return SubsystemHealth(
                status="unknown", message="Strategic planning disabled"
            )
        last_run = getattr(self._state, "last_strategic_planning", "")
        if last_run:
            return SubsystemHealth(
                status="healthy",
                message="Strategic planning active",
                last_active=last_run,
            )
        return SubsystemHealth(
            status="unknown",
            message="Strategic planning has not run yet",
        )


class RoadmapSchedulingSubsystem:
    """Roadmap-driven task scheduling subsystem (P27)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "roadmap_scheduling"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_roadmap_scan": getattr(self._state, "last_roadmap_scan", ""),
            "roadmap_tasks_scheduled": getattr(
                self._state, "roadmap_tasks_scheduled", 0
            ),
            "roadmap_tasks_completed": getattr(
                self._state, "roadmap_tasks_completed", 0
            ),
            "roadmap_current_wave": getattr(self._state, "roadmap_current_wave", 1),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "roadmap_scheduling": {
                "enabled": getattr(self._config, "roadmap_scheduling_enabled", True),
                "last_scan": getattr(self._state, "last_roadmap_scan", None) or None,
                "tasks_scheduled": getattr(self._state, "roadmap_tasks_scheduled", 0),
                "tasks_completed": getattr(self._state, "roadmap_tasks_completed", 0),
                "current_wave": getattr(self._state, "roadmap_current_wave", 1),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "roadmap_scheduling_enabled", True)
        if not enabled:
            return SubsystemHealth(
                status="unknown", message="Roadmap scheduling disabled"
            )
        last_scan = getattr(self._state, "last_roadmap_scan", "")
        if last_scan:
            return SubsystemHealth(
                status="healthy",
                message="Roadmap scheduling active",
                last_active=last_scan,
            )
        return SubsystemHealth(
            status="unknown",
            message="Roadmap scheduling has not run yet",
        )


class P25AutonomySubsystem:
    """P25 full autonomous operation subsystem."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "p25_autonomy"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_session_snapshot": getattr(self._state, "last_session_snapshot", ""),
            "last_goal_refresh": getattr(self._state, "last_goal_refresh", ""),
            "last_queue_reorder": getattr(self._state, "last_queue_reorder", ""),
            "p25_schedule_mode": getattr(self._state, "p25_schedule_mode", "NORMAL"),
            "p25_throttle_level": getattr(self._state, "p25_throttle_level", "NORMAL"),
            "p25_outcomes_recorded": getattr(self._state, "p25_outcomes_recorded", 0),
            "p25_auto_approvals": getattr(self._state, "p25_auto_approvals", 0),
            "p25_queue_reorders": getattr(self._state, "p25_queue_reorders", 0),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "p25_autonomy": {
                "enabled": getattr(self._config, "p25_enabled", True),
                "adaptive_scheduling": getattr(
                    self._config, "p25_adaptive_scheduling_enabled", True
                ),
                "session_continuity": getattr(
                    self._config, "p25_session_continuity_enabled", True
                ),
                "approval_learning": getattr(
                    self._config, "p25_approval_learning_enabled", True
                ),
                "goal_scheduling": getattr(
                    self._config, "p25_goal_scheduling_enabled", True
                ),
                "budget_governor": getattr(
                    self._config, "p25_budget_governor_enabled", True
                ),
                "learning_loop": getattr(
                    self._config, "p25_learning_loop_enabled", True
                ),
                "schedule_mode": getattr(self._state, "p25_schedule_mode", "NORMAL"),
                "throttle_level": getattr(self._state, "p25_throttle_level", "NORMAL"),
                "last_snapshot": getattr(self._state, "last_session_snapshot", None)
                or None,
                "last_goal_refresh": getattr(self._state, "last_goal_refresh", None)
                or None,
                "last_queue_reorder": getattr(self._state, "last_queue_reorder", None)
                or None,
                "outcomes_recorded": getattr(self._state, "p25_outcomes_recorded", 0),
                "auto_approvals": getattr(self._state, "p25_auto_approvals", 0),
                "queue_reorders": getattr(self._state, "p25_queue_reorders", 0),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "p25_enabled", True)
        if not enabled:
            return SubsystemHealth(status="unknown", message="P25 autonomy disabled")
        last_snapshot = getattr(self._state, "last_session_snapshot", "")
        if last_snapshot:
            return SubsystemHealth(
                status="healthy",
                message="P25 autonomy active",
                last_active=last_snapshot,
            )
        return SubsystemHealth(
            status="unknown",
            message="P25 autonomy has not run yet",
        )


class ExecutiveLoopSubsystem:
    """Executive loop subsystem (P17)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "executive_loop"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_executive_loop": getattr(self._state, "last_executive_loop", ""),
            "executive_sessions_run": getattr(self._state, "executive_sessions_run", 0),
            "work_submitted_by_executives": getattr(
                self._state, "work_submitted_by_executives", 0
            ),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "executive_loop": {
                "enabled": getattr(self._config, "executive_loop_enabled", True),
                "interval_hours": getattr(
                    self._config, "executive_loop_interval_hours", 12
                ),
                "last_run": getattr(self._state, "last_executive_loop", None) or None,
                "sessions_run": getattr(self._state, "executive_sessions_run", 0),
                "work_submitted": getattr(
                    self._state, "work_submitted_by_executives", 0
                ),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "executive_loop_enabled", True)
        if not enabled:
            return SubsystemHealth(status="unknown", message="Executive loop disabled")
        last_run = getattr(self._state, "last_executive_loop", "")
        if last_run:
            return SubsystemHealth(
                status="healthy",
                message="Executive loop active",
                last_active=last_run,
            )
        return SubsystemHealth(
            status="unknown",
            message="Executive loop has not run yet",
        )


class SelfImprovementSubsystem:
    """Self-improvement cycle subsystem (P30)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "improvement_cycle"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_improvement_cycle": getattr(
                self._state, "last_improvement_cycle", ""
            ),
            "improvement_cycles_run": getattr(self._state, "improvement_cycles_run", 0),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "improvement_cycle": {
                "enabled": getattr(self._config, "improvement_enabled", True),
                "interval_hours": getattr(
                    self._config, "improvement_cycle_interval_hours", 168
                ),
                "last_run": getattr(self._state, "last_improvement_cycle", None)
                or None,
                "cycles_run": getattr(self._state, "improvement_cycles_run", 0),
                "next_scheduled": None,
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "improvement_enabled", True)
        if not enabled:
            return SubsystemHealth(
                status="unknown", message="Self-improvement disabled"
            )
        last_run = getattr(self._state, "last_improvement_cycle", "")
        if last_run:
            return SubsystemHealth(
                status="healthy",
                message="Self-improvement cycle active",
                last_active=last_run,
            )
        return SubsystemHealth(
            status="unknown",
            message="Self-improvement cycle has not run yet",
        )


class EmployeeIdeationSubsystem:
    """Employee creative ideation subsystem (P34)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "employee_ideation"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_employee_ideation": getattr(
                self._state, "last_employee_ideation", ""
            ),
            "employee_ideation_cycles": getattr(
                self._state, "employee_ideation_cycles", 0
            ),
            "ideas_generated": getattr(self._state, "ideas_generated", 0),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "employee_ideation": {
                "enabled": getattr(self._config, "employee_ideation_enabled", True),
                "interval_minutes": getattr(
                    self._config, "employee_ideation_interval_minutes", 60
                ),
                "last_run": getattr(self._state, "last_employee_ideation", None)
                or None,
                "cycles_run": getattr(self._state, "employee_ideation_cycles", 0),
                "ideas_generated": getattr(self._state, "ideas_generated", 0),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "employee_ideation_enabled", True)
        if not enabled:
            return SubsystemHealth(
                status="unknown", message="Employee ideation disabled"
            )
        last_run = getattr(self._state, "last_employee_ideation", "")
        if last_run:
            return SubsystemHealth(
                status="healthy",
                message="Employee ideation active",
                last_active=last_run,
            )
        return SubsystemHealth(
            status="unknown",
            message="Employee ideation has not run yet",
        )


class AutoMergeSubsystem:
    """Auto-merge PR subsystem (P35)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "auto_merge"

    def heartbeat_fields(self) -> dict[str, Any]:
        return {
            "last_auto_merge_check": getattr(self._state, "last_auto_merge_check", ""),
            "auto_merge_checks": getattr(self._state, "auto_merge_checks", 0),
            "prs_merged": getattr(self._state, "prs_merged", 0),
        }

    def status_section(self) -> dict[str, Any]:
        return {
            "auto_merge": {
                "enabled": getattr(self._config, "auto_merge_enabled", True),
                "interval_minutes": getattr(
                    self._config, "auto_merge_interval_minutes", 5
                ),
                "last_check": getattr(self._state, "last_auto_merge_check", None)
                or None,
                "checks_run": getattr(self._state, "auto_merge_checks", 0),
                "prs_merged": getattr(self._state, "prs_merged", 0),
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "auto_merge_enabled", True)
        if not enabled:
            return SubsystemHealth(status="unknown", message="Auto-merge disabled")
        last_check = getattr(self._state, "last_auto_merge_check", "")
        if last_check:
            return SubsystemHealth(
                status="healthy",
                message="Auto-merge active",
                last_active=last_check,
            )
        return SubsystemHealth(
            status="unknown",
            message="Auto-merge has not run yet",
        )


class SchedulingEfficiencySubsystem:
    """Scheduling efficiency metrics subsystem (P36)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "scheduling_efficiency"

    def heartbeat_fields(self) -> dict[str, Any]:
        # Metrics come from adaptive_scheduler_state.json, not DaemonState.
        return {}

    def status_section(self) -> dict[str, Any]:
        return {
            "scheduling_efficiency": {
                "consecutive_idle_cycles": 0,
                "wasted_polls_count": 0,
                "current_backoff_multiplier": 1,
                "effective_poll_interval": getattr(
                    self._config, "poll_interval_seconds", 30
                ),
                "last_activity_time": None,
                "is_business_hours": True,
                "business_hours_mode_applied": False,
                "ideation_cycles_total": 0,
                "ideation_cycles_executed": 0,
                "ideation_cycles_skipped": 0,
                "ideation_skip_rate_percent": 0.0,
                "ideation_ideas_generated": 0,
                "ideation_ideas_per_cycle": 0.0,
            }
        }

    def health_check(self) -> SubsystemHealth:
        return SubsystemHealth(
            status="healthy",
            message="Scheduling efficiency tracking active",
        )


class CircuitBreakerSubsystem:
    """Circuit breaker subsystem."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "circuit_breaker"

    def heartbeat_fields(self) -> dict[str, Any]:
        # Circuit breaker state is loaded separately via loop_monitor.
        return {}

    def status_section(self) -> dict[str, Any]:
        # circuit_breaker_state is top-level in daemon_status.
        return {}

    def health_check(self) -> SubsystemHealth:
        return SubsystemHealth(
            status="unknown",
            message="Loaded from loop_monitor",
        )


class DocumentApprovalsSubsystem:
    """Document approval scanning subsystem (P23)."""

    def __init__(self, state: Any, config: Any) -> None:
        self._state = state
        self._config = config

    @property
    def name(self) -> str:
        return "document_approvals"

    def heartbeat_fields(self) -> dict[str, Any]:
        # No heartbeat fields currently.
        return {}

    def status_section(self) -> dict[str, Any]:
        return {
            "document_approvals": {
                "enabled": getattr(self._config, "document_scan_enabled", True),
                "last_scan": getattr(self._state, "last_document_scan", None) or None,
                "documents_discovered": getattr(self._state, "documents_discovered", 0),
                "documents_pending": 0,
            }
        }

    def health_check(self) -> SubsystemHealth:
        enabled = getattr(self._config, "document_scan_enabled", True)
        if not enabled:
            return SubsystemHealth(
                status="unknown", message="Document approvals disabled"
            )
        last_scan = getattr(self._state, "last_document_scan", "")
        if last_scan:
            return SubsystemHealth(
                status="healthy",
                message="Document approval scanning active",
                last_active=last_scan,
            )
        return SubsystemHealth(
            status="unknown",
            message="Document approval scanning has not run yet",
        )
