#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Escalation Router — Context-aware escalation assignment with pattern learning.

Analyzes escalation context, matches to senior employee expertise, and auto-assigns.
Learns from resolutions: if a senior resolves, the pattern is stored for future routing;
if a senior escalates further (reaches Tier 4), the human is notified.

Components:
    EscalationContext   — Parsed context derived from escalation record + task
    RoutingDecision     — Result of routing: assigned_to, action, reason
    PatternStore        — Persist and retrieve resolution patterns per trigger/capability
    EscalationRouter    — Orchestrate routing decisions using expertise matching

Storage:
    .company/state/escalation_router_patterns.json

Usage:
    # Route an open escalation to the best senior employee
    python escalation_router.py route --task-id "task-123"

    # Record a resolution outcome (triggers learning)
    python escalation_router.py record-resolution \\
        --task-id "task-123" \\
        --resolved-by "senior-python-developer" \\
        --resolution-type resolved

    # Inspect learned patterns
    python escalation_router.py patterns
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("escalation_router")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PATTERNS_FILE = "state/escalation_router_patterns.json"

# Roles considered "senior" for escalation routing (ordered by preference)
SENIOR_ROLES: list[str] = ["lead", "senior", "executive"]

# Minimum successful resolutions before a pattern is considered reliable
MIN_RELIABLE_RESOLUTIONS = 3

# Score boost for employees with a known success pattern for this context
PATTERN_SCORE_BOOST = 10

# Tier at which a human notification is mandatory
HUMAN_TIER = 4

# ---------------------------------------------------------------------------
# Lazy module references (avoid circular imports)
# ---------------------------------------------------------------------------

_company_resolver = None
_escalation = None
_employee_activator = None


def _ensure_company_resolver() -> Any:
    global _company_resolver
    if _company_resolver is not None:
        return _company_resolver
    try:
        from . import company_resolver as cr

        _company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        _company_resolver = cr
    return _company_resolver


def _ensure_escalation() -> Any:
    global _escalation
    if _escalation is not None:
        return _escalation
    try:
        from . import escalation as esc

        _escalation = esc
    except ImportError:
        import escalation as esc  # type: ignore[no-redef]

        _escalation = esc
    return _escalation


def _ensure_employee_activator() -> Any:
    global _employee_activator
    if _employee_activator is not None:
        return _employee_activator
    try:
        from . import employee_activator as ea

        _employee_activator = ea
    except ImportError:
        import employee_activator as ea  # type: ignore[no-redef]

        _employee_activator = ea
    return _employee_activator


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


def _get_company_dir() -> Path:
    cr = _ensure_company_resolver()
    return cr.get_company_dir()


def _get_patterns_path() -> Path:
    return _get_company_dir() / PATTERNS_FILE


def _get_queue_path() -> Path:
    return _get_company_dir() / "state/work_queue.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EscalationContext:
    """Parsed context distilled from an escalation record and its originating task."""

    task_id: str
    trigger: str
    current_tier: int
    status: str
    original_agent: str | None
    current_agent: str | None
    # Task-level fields
    task_title: str
    required_capabilities: list[str]
    task_complexity: str
    # Derived fields
    capability_area: str  # coarsened label for pattern matching
    failure_count: int
    created_at: str
    # Raw payloads for downstream consumers
    escalation_record: dict = field(default_factory=dict)
    task_record: dict = field(default_factory=dict)


@dataclass
class RoutingDecision:
    """Result of a routing attempt."""

    task_id: str
    action: str  # "assign" | "notify_human" | "no_action"
    assigned_to: str | None
    reason: str
    # Ordered shortlist of candidate employee IDs (best first)
    candidates: list[str] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class PatternRecord:
    """Learned resolution pattern for a (trigger, capability_area) pair."""

    trigger: str
    capability_area: str
    # employee_id -> {resolutions: int, successes: int}
    employee_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    total_routed: int = 0
    last_updated: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Pattern Store
# ---------------------------------------------------------------------------


class PatternStore:
    """Persist and retrieve employee resolution patterns."""

    def __init__(self, patterns_path: Path | None = None) -> None:
        self._path = patterns_path or _get_patterns_path()
        self._data: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load patterns file: %s", exc)
                self._data = {}
        self._loaded = True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, str(self._path))
        except OSError as exc:
            logger.error("Could not save patterns file: %s", exc)

    @staticmethod
    def _key(trigger: str, capability_area: str) -> str:
        return f"{trigger}:{capability_area}"

    def get_pattern(self, trigger: str, capability_area: str) -> PatternRecord:
        """Return the pattern record for a (trigger, capability_area) pair."""
        self._load()
        key = self._key(trigger, capability_area)
        raw = self._data.get(key)
        if raw is None:
            return PatternRecord(trigger=trigger, capability_area=capability_area)
        return PatternRecord(
            trigger=raw.get("trigger", trigger),
            capability_area=raw.get("capability_area", capability_area),
            employee_stats=raw.get("employee_stats", {}),
            total_routed=raw.get("total_routed", 0),
            last_updated=raw.get("last_updated", ""),
        )

    def record_routing(
        self, trigger: str, capability_area: str, employee_id: str
    ) -> None:
        """Record that an employee was assigned to handle this escalation type."""
        self._load()
        key = self._key(trigger, capability_area)
        if key not in self._data:
            self._data[key] = {
                "trigger": trigger,
                "capability_area": capability_area,
                "employee_stats": {},
                "total_routed": 0,
                "last_updated": "",
            }
        entry = self._data[key]
        entry["total_routed"] = entry.get("total_routed", 0) + 1
        entry.setdefault("employee_stats", {})
        entry["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def record_outcome(
        self,
        trigger: str,
        capability_area: str,
        employee_id: str,
        success: bool,
    ) -> None:
        """Record an outcome (success or failure) for an employee on this pattern."""
        self._load()
        key = self._key(trigger, capability_area)
        if key not in self._data:
            self._data[key] = {
                "trigger": trigger,
                "capability_area": capability_area,
                "employee_stats": {},
                "total_routed": 0,
                "last_updated": "",
            }
        entry = self._data[key]
        stats = entry.setdefault("employee_stats", {})
        if employee_id not in stats:
            stats[employee_id] = {"resolutions": 0, "successes": 0}
        stats[employee_id]["resolutions"] += 1
        if success:
            stats[employee_id]["successes"] += 1
        entry["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def success_rate(
        self, trigger: str, capability_area: str, employee_id: str
    ) -> float:
        """Return the empirical success rate for an employee on this pattern (0.0-1.0)."""
        pattern = self.get_pattern(trigger, capability_area)
        stats = pattern.employee_stats.get(employee_id, {})
        resolutions = stats.get("resolutions", 0)
        successes = stats.get("successes", 0)
        if resolutions < MIN_RELIABLE_RESOLUTIONS:
            return 0.5  # neutral prior until enough data
        return successes / resolutions

    def best_employee_for_pattern(
        self, trigger: str, capability_area: str
    ) -> str | None:
        """Return the employee ID with the highest success rate for this pattern."""
        pattern = self.get_pattern(trigger, capability_area)
        if not pattern.employee_stats:
            return None
        scored = [
            (emp_id, self.success_rate(trigger, capability_area, emp_id))
            for emp_id in pattern.employee_stats
            if pattern.employee_stats[emp_id].get("resolutions", 0)
            >= MIN_RELIABLE_RESOLUTIONS
        ]
        if not scored:
            return None
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def all_patterns(self) -> dict[str, dict]:
        """Return all raw pattern data."""
        self._load()
        return dict(self._data)


# ---------------------------------------------------------------------------
# Context analysis
# ---------------------------------------------------------------------------


def _derive_capability_area(capabilities: list[str]) -> str:
    """Coarsen a list of capabilities to a single area label for pattern matching."""
    if not capabilities:
        return "general"
    # Priority order: first recognisable domain wins
    priority = [
        "security",
        "devops",
        "testing",
        "architecture",
        "frontend",
        "backend",
        "python",
        "data-analysis",
        "documentation",
        "marketing",
    ]
    caps_lower = {c.lower() for c in capabilities}
    for domain in priority:
        if domain in caps_lower:
            return domain
    # Fallback: use the first capability
    return capabilities[0].lower()


def _load_task_from_queue(task_id: str) -> dict:
    """Load a task record from the work queue."""
    queue_path = _get_queue_path()
    if not queue_path.exists():
        return {}
    try:
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    tasks = queue if isinstance(queue, list) else queue.get("tasks", [])
    for task in tasks:
        if task.get("task_id") == task_id or task.get("id") == task_id:
            return task
    return {}


def analyze_escalation_context(escalation: dict) -> EscalationContext:
    """Parse an escalation record and its originating task into an EscalationContext.

    Args:
        escalation: Raw escalation record dict (as stored in .company/escalations/).

    Returns:
        EscalationContext with all fields populated.
    """
    task_id = escalation.get("task_id", "unknown")
    trigger = escalation.get("trigger", "")
    current_tier = int(escalation.get("current_tier", 1))
    status = escalation.get("status", "pending")
    original_agent = escalation.get("original_agent")
    current_agent = escalation.get("current_agent")
    metadata = escalation.get("metadata", {})

    # Try to load the full task record from the work queue for richer context
    task_record = _load_task_from_queue(task_id)
    if not task_record:
        # Fall back to metadata embedded in the escalation
        task_record = metadata

    task_title = (
        task_record.get("title")
        or task_record.get("description", "")[:80]
        or metadata.get("task_title", task_id)
    )
    required_capabilities: list[str] = task_record.get("required_capabilities", [])
    task_complexity: str = task_record.get("complexity", "standard")

    # Count failure events in escalation history
    events: list[dict] = escalation.get("events", [])
    failure_count = sum(
        1 for ev in events if ev.get("trigger") in {"repeated_failure", "timeout"}
    )

    capability_area = _derive_capability_area(required_capabilities)

    return EscalationContext(
        task_id=task_id,
        trigger=trigger,
        current_tier=current_tier,
        status=status,
        original_agent=original_agent,
        current_agent=current_agent,
        task_title=task_title,
        required_capabilities=required_capabilities,
        task_complexity=task_complexity,
        capability_area=capability_area,
        failure_count=failure_count,
        created_at=escalation.get("created_at", ""),
        escalation_record=escalation,
        task_record=task_record,
    )


# ---------------------------------------------------------------------------
# Senior employee matching
# ---------------------------------------------------------------------------


def _get_senior_employees_by_capability(capabilities: list[str]) -> list[dict]:
    """Return senior/lead employees sorted by capability match score.

    Only employees with role in SENIOR_ROLES are included.
    """
    ea = _ensure_employee_activator()
    # get_employees_by_capability returns best-match-first already
    matched = ea.get_employees_by_capability(capabilities) if capabilities else []

    if not matched:
        # No capability filter — return all senior employees
        org = ea.load_org()
        matched = org.get("employees", [])

    senior = [emp for emp in matched if emp.get("role") in SENIOR_ROLES]
    # Sort: lead > senior > executive (preserve existing order within each tier)
    role_priority = {role: i for i, role in enumerate(SENIOR_ROLES)}
    senior.sort(key=lambda e: role_priority.get(e.get("role", ""), 99))
    return senior


def find_senior_for_escalation(context: EscalationContext) -> str | None:
    """Return the best senior employee ID for this escalation context.

    Selection order:
    1. Employee preferred by learned patterns for this (trigger, capability_area)
    2. Capability-matched senior employees (sorted by match score + role)
    3. Any senior employee as a last resort

    Excludes the original_agent and current_agent to ensure fresh perspective.

    Args:
        context: Parsed escalation context.

    Returns:
        Employee ID, or None if no suitable senior is found.
    """
    store = PatternStore()
    exclude: set[str] = {
        a for a in (context.original_agent, context.current_agent) if a
    }

    # 1. Check learned pattern preference
    preferred = store.best_employee_for_pattern(
        context.trigger, context.capability_area
    )
    if preferred and preferred not in exclude:
        ea = _ensure_employee_activator()
        emp = ea.get_employee_by_id(preferred)
        if emp and emp.get("role") in SENIOR_ROLES:
            return preferred

    # 2. Capability-matched senior employees
    seniors = _get_senior_employees_by_capability(context.required_capabilities)
    for emp in seniors:
        emp_id = emp.get("id")
        if emp_id and emp_id not in exclude:
            return emp_id

    # 3. Any senior employee (ignore capability filter)
    if context.required_capabilities:
        seniors_any = _get_senior_employees_by_capability([])
        for emp in seniors_any:
            emp_id = emp.get("id")
            if emp_id and emp_id not in exclude:
                return emp_id

    return None


# ---------------------------------------------------------------------------
# Human notification
# ---------------------------------------------------------------------------


def notify_human(escalation: dict, reason: str) -> None:
    """Notify the human operator that an escalation requires their attention.

    WS-122: Notifications suppressed. Task failures are handled by retry +
    circuit breaker. Human escalation popups were disruptive noise from
    tasks that simply need better prompts, not human intervention.
    Use /respond to check escalations when you want to.
    """
    task_id = escalation.get("task_id", "unknown")
    logger.debug("Human notification suppressed for %s: %s", task_id, reason)
    return

    # Original code below (disabled)
    esc_mod = _ensure_escalation()
    title = f"[HUMAN REQUIRED] Escalation: {task_id}"
    logger.warning("Notifying human for task %s: %s", task_id, reason)
    esc_mod.send_notification(title, reason)

    # External services (Slack, Discord, webhooks) configured for Tier 4
    # Force tier=4 to use full notification routing even if record is lower
    escalation_for_dispatch = dict(escalation)
    escalation_for_dispatch["current_tier"] = HUMAN_TIER
    try:
        esc_mod.dispatch_escalation_notification(escalation_for_dispatch)
    except Exception as exc:
        logger.error("External notification dispatch failed: %s", exc)


# ---------------------------------------------------------------------------
# Core routing logic
# ---------------------------------------------------------------------------


def route_escalation(task_id: str) -> RoutingDecision:
    """Analyze escalation context and auto-assign to the best senior employee.

    If the escalation is already at Tier 4 (HUMAN), or no senior employee is
    available, a human notification is sent instead.

    Args:
        task_id: The task ID with an active escalation record.

    Returns:
        RoutingDecision describing the action taken.
    """
    esc_mod = _ensure_escalation()

    # Load escalation record
    escalation = esc_mod.load_escalation(task_id)
    if escalation is None:
        return RoutingDecision(
            task_id=task_id,
            action="no_action",
            assigned_to=None,
            reason=f"No escalation record found for task {task_id}",
        )

    context = analyze_escalation_context(escalation)

    # Already resolved — nothing to do
    if context.status == "resolved":
        return RoutingDecision(
            task_id=task_id,
            action="no_action",
            assigned_to=None,
            reason="Escalation already resolved",
        )

    # Tier 4 or above -> must notify human
    if context.current_tier >= HUMAN_TIER:
        reason = (
            f"Escalation reached Tier {context.current_tier} (HUMAN) "
            f"for task '{context.task_title}' "
            f"(trigger: {context.trigger}, failures: {context.failure_count})"
        )
        notify_human(escalation, reason)
        return RoutingDecision(
            task_id=task_id,
            action="notify_human",
            assigned_to=None,
            reason=reason,
        )

    # Find best senior employee
    senior_id = find_senior_for_escalation(context)
    if senior_id is None:
        # No senior available — escalate to human
        reason = (
            f"No senior employee found for capabilities {context.required_capabilities} "
            f"(trigger: {context.trigger}). Notifying human."
        )
        notify_human(escalation, reason)
        return RoutingDecision(
            task_id=task_id,
            action="notify_human",
            assigned_to=None,
            reason=reason,
        )

    # Record routing in pattern store
    store = PatternStore()
    store.record_routing(context.trigger, context.capability_area, senior_id)

    # Update escalation record with the new assignee
    _update_escalation_agent(task_id, senior_id, escalation, context)

    logger.info(
        "Routed escalation for task %s -> %s (trigger=%s, area=%s)",
        task_id,
        senior_id,
        context.trigger,
        context.capability_area,
    )

    return RoutingDecision(
        task_id=task_id,
        action="assign",
        assigned_to=senior_id,
        reason=(
            f"Assigned to senior employee '{senior_id}' based on "
            f"capability match for area '{context.capability_area}' "
            f"(trigger: {context.trigger})"
        ),
        candidates=[senior_id],
    )


def _update_escalation_agent(
    task_id: str,
    employee_id: str,
    escalation: dict,
    context: EscalationContext,
) -> None:
    """Update the escalation record to reflect the new agent assignment."""
    esc_mod = _ensure_escalation()
    now = datetime.now(timezone.utc).isoformat()

    updated = dict(escalation)
    updated["current_agent"] = employee_id
    updated["updated_at"] = now
    updated.setdefault("events", []).append(
        {
            "timestamp": now,
            "tier": context.current_tier,
            "trigger": context.trigger,
            "action_taken": "auto_routed_to_senior",
            "notes": (
                f"escalation_router: assigned to {employee_id} "
                f"for capability area '{context.capability_area}'"
            ),
        }
    )
    try:
        esc_mod.save_escalation(updated)
    except Exception as exc:
        logger.error("Failed to save updated escalation record: %s", exc)


# ---------------------------------------------------------------------------
# Resolution recording (learning)
# ---------------------------------------------------------------------------


def record_resolution(
    task_id: str,
    resolved_by: str,
    resolution_type: str,
) -> None:
    """Record the outcome of a senior employee's escalation handling.

    - If resolution_type == "resolved": marks a success, updates patterns.
    - If resolution_type == "escalated" or "failed": marks a failure,
      notifies human.

    Args:
        task_id: The task that was escalated.
        resolved_by: Employee ID who handled the escalation.
        resolution_type: One of "resolved" | "escalated" | "failed".
    """
    esc_mod = _ensure_escalation()

    escalation = esc_mod.load_escalation(task_id)
    if escalation is None:
        logger.warning("record_resolution: no escalation record for %s", task_id)
        return

    context = analyze_escalation_context(escalation)
    success = resolution_type == "resolved"

    store = PatternStore()
    store.record_outcome(
        context.trigger,
        context.capability_area,
        resolved_by,
        success=success,
    )

    if success:
        logger.info(
            "Senior %s resolved escalation for task %s (trigger=%s, area=%s). "
            "Pattern learned.",
            resolved_by,
            task_id,
            context.trigger,
            context.capability_area,
        )
        # Mark escalation as resolved
        try:
            esc_mod.resolve_escalation(
                task_id,
                resolution=f"Resolved by senior employee {resolved_by}",
                resolved_by=resolved_by,
            )
        except Exception as exc:
            logger.error("Failed to mark escalation resolved: %s", exc)
    else:
        # Senior could not resolve — notify human
        reason = (
            f"Senior employee '{resolved_by}' could not resolve escalation "
            f"(resolution_type={resolution_type}) for task '{context.task_title}'. "
            f"Trigger: {context.trigger}, area: {context.capability_area}."
        )
        logger.warning(reason)
        notify_human(escalation, reason)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_route(args: argparse.Namespace) -> None:
    decision = route_escalation(args.task_id)
    print(json.dumps(asdict(decision), indent=2))


def _cli_record_resolution(args: argparse.Namespace) -> None:
    record_resolution(
        task_id=args.task_id,
        resolved_by=args.resolved_by,
        resolution_type=args.resolution_type,
    )
    print(f"Recorded resolution for task {args.task_id}: {args.resolution_type}")


def _cli_patterns(args: argparse.Namespace) -> None:
    store = PatternStore()
    patterns = store.all_patterns()
    if not patterns:
        print("No patterns learned yet.")
        return
    print(json.dumps(patterns, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="escalation_router",
        description="Route escalations to senior employees based on context and learned patterns.",
    )
    sub = parser.add_subparsers(dest="command")

    # route
    p_route = sub.add_parser("route", help="Route an open escalation")
    p_route.add_argument("--task-id", required=True, help="Task ID with escalation")
    p_route.set_defaults(func=_cli_route)

    # record-resolution
    p_rec = sub.add_parser(
        "record-resolution", help="Record senior employee resolution outcome"
    )
    p_rec.add_argument("--task-id", required=True, help="Task ID")
    p_rec.add_argument(
        "--resolved-by", required=True, help="Employee ID who handled escalation"
    )
    p_rec.add_argument(
        "--resolution-type",
        required=True,
        choices=["resolved", "escalated", "failed"],
        help="Outcome: 'resolved' = success, 'escalated'/'failed' = notify human",
    )
    p_rec.set_defaults(func=_cli_record_resolution)

    # patterns
    p_pat = sub.add_parser("patterns", help="Display all learned routing patterns")
    p_pat.set_defaults(func=_cli_patterns)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
