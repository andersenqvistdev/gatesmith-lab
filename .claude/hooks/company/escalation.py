#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Escalation Manager — 4-tier escalation system for company task management.

Implements a structured escalation system with 4 tiers:
    Tier 1: Peer Agent (15m timeout) - reassign to capable peer
    Tier 2: Department Head (30m) - escalate to department head
    Tier 3: Coordinator (60m) - escalate to coordinator
    Tier 4: Human (120m) - notify human and pause

6 Triggers:
    - timeout: Task exceeds 1.5x expected duration
    - explicit_block: Agent explicitly reports being blocked
    - repeated_failure: Same task failed 3+ times
    - capability_mismatch: Agent lacks required capabilities
    - resource_contention: Multiple agents need same resource
    - quality_rejection: Work rejected by reviewer 2+ times

Escalation logs stored in .company/escalations/{task_id}.json

Usage:
    # Check if escalation is needed for a task
    python escalation.py check --task-id "task-123"

    # Trigger escalation for a task
    python escalation.py escalate --task-id "task-123" --reason timeout

    # List active escalations
    python escalation.py list [--tier 1-4] [--status pending|resolved]

    # Resolve an escalation
    python escalation.py resolve --task-id "task-123" --resolution "Reassigned to agent-002"

    # Get escalation history for a task
    python escalation.py history --task-id "task-123"
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
from company_resolver import (
    find_company_root,
    get_company_dir,
    get_current_project,
    get_project_id,
    is_multi_project_mode,
)

try:
    from . import governing_config
except ImportError:  # direct script execution
    import governing_config  # type: ignore[no-redef]

# Lazy import for external_connectors to avoid circular imports
_external_connectors = None


def _ensure_external_connectors():
    """Lazily import external_connectors module."""
    global _external_connectors
    if _external_connectors is not None:
        return _external_connectors

    try:
        from . import external_connectors as ec

        _external_connectors = ec
    except ImportError:
        try:
            import external_connectors as ec  # type: ignore[no-redef]

            _external_connectors = ec
        except ImportError:
            _external_connectors = None

    return _external_connectors


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ESCALATIONS_DIR = "escalations"
ARCHIVE_DIR = "archive/escalations"
QUEUE_FILE = "state/work_queue.json"
FORGE_CONFIG_FILE = "gatesmith.json"

# Notify hook path (relative to project root)
NOTIFY_HOOK = ".claude/hooks/notify.py"

# Default external notification routing by tier
# Tier 1-2: Internal only (no external dispatch)
# Tier 3: Slack notification
# Tier 4: Slack + Discord + all configured webhooks
DEFAULT_TIER_NOTIFICATION_ROUTING = {
    1: [],  # Internal only
    2: [],  # Internal only
    3: ["slack"],  # Slack notification
    4: ["slack", "discord", "generic_webhook"],  # All channels
}

# Rate limiting for escalation notifications
# (seconds between notifications of same tier)
ESCALATION_NOTIFICATION_COOLDOWN_SECONDS = 300  # 5 minutes


# -----------------------------------------------------------------------------
# Escalation Tiers
# -----------------------------------------------------------------------------


class EscalationTier(IntEnum):
    """Escalation tiers with timeout values in minutes."""

    PEER = 1  # 15 minutes - reassign to capable peer
    DEPARTMENT = 2  # 30 minutes - escalate to department head
    COORDINATOR = 3  # 60 minutes - escalate to coordinator
    HUMAN = 4  # 120 minutes - notify human and pause


TIER_CONFIG = {
    EscalationTier.PEER: {
        "name": "Peer Agent",
        "timeout_minutes": 15,
        "action": "reassign_to_peer",
        "description": "Reassign to a capable peer agent within the same team",
    },
    EscalationTier.DEPARTMENT: {
        "name": "Department Head",
        "timeout_minutes": 30,
        "action": "escalate_to_department_head",
        "description": "Escalate to department head for resource reallocation",
    },
    EscalationTier.COORDINATOR: {
        "name": "Coordinator",
        "timeout_minutes": 60,
        "action": "escalate_to_coordinator",
        "description": "Escalate to coordinator for cross-department resolution",
    },
    EscalationTier.HUMAN: {
        "name": "Human",
        "timeout_minutes": 120,
        "action": "notify_human_and_pause",
        "description": "Notify human operator and pause task execution",
    },
}


# -----------------------------------------------------------------------------
# Escalation Triggers
# -----------------------------------------------------------------------------


class EscalationTrigger:
    """Enumeration of escalation trigger types."""

    TIMEOUT = "timeout"
    EXPLICIT_BLOCK = "explicit_block"
    REPEATED_FAILURE = "repeated_failure"
    CAPABILITY_MISMATCH = "capability_mismatch"
    RESOURCE_CONTENTION = "resource_contention"
    QUALITY_REJECTION = "quality_rejection"


TRIGGER_CONFIG = {
    EscalationTrigger.TIMEOUT: {
        "description": "Task exceeds 1.5x expected duration",
        "threshold": 1.5,  # multiplier of expected duration
        "initial_tier": EscalationTier.PEER,
    },
    EscalationTrigger.EXPLICIT_BLOCK: {
        "description": "Agent explicitly reports being blocked",
        "initial_tier": EscalationTier.DEPARTMENT,
    },
    EscalationTrigger.REPEATED_FAILURE: {
        "description": "Same task failed 3+ times",
        "threshold": 3,  # failure count
        "initial_tier": EscalationTier.DEPARTMENT,
    },
    EscalationTrigger.CAPABILITY_MISMATCH: {
        "description": "Agent lacks required capabilities",
        "initial_tier": EscalationTier.PEER,
    },
    EscalationTrigger.RESOURCE_CONTENTION: {
        "description": "Multiple agents need same resource",
        "initial_tier": EscalationTier.COORDINATOR,
    },
    EscalationTrigger.QUALITY_REJECTION: {
        "description": "Work rejected by reviewer 2+ times",
        "threshold": 2,  # rejection count
        "initial_tier": EscalationTier.DEPARTMENT,
    },
}


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class EscalationEvent:
    """Represents a single escalation event in the history."""

    timestamp: str
    tier: int
    trigger: str
    action_taken: str
    resolved: bool = False
    resolution: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    notes: str | None = None


@dataclass
class EscalationRecord:
    """Complete escalation record for a task."""

    task_id: str
    current_tier: int
    status: str  # pending, in_progress, resolved, paused
    created_at: str
    updated_at: str
    original_agent: str | None = None
    current_agent: str | None = None
    trigger: str = ""
    trigger_details: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # Multi-project fields (v1.2)
    project_id: str | None = None
    project_path: str | None = None
    multi_project_mode: bool = False


# -----------------------------------------------------------------------------
# Path Utilities (using company_resolver for multi-project support)
# -----------------------------------------------------------------------------


def get_escalations_dir() -> Path:
    """
    Get the escalations directory path.

    Uses company_resolver to locate the correct .company directory,
    supporting both multi-project and legacy modes.
    """
    return get_company_dir() / ESCALATIONS_DIR


def get_escalation_path(task_id: str) -> Path:
    """Get the escalation file path for a task."""
    return get_escalations_dir() / f"{task_id}.json"


def get_queue_path() -> Path:
    """Get the work queue file path."""
    return get_company_dir() / QUEUE_FILE


def ensure_escalations_dir():
    """Ensure escalations directory exists."""
    escalations_dir = get_escalations_dir()
    escalations_dir.mkdir(parents=True, exist_ok=True)


def get_project_context() -> dict:
    """
    Get the current project context for cross-project escalation tracking.

    Returns:
        Dict with project_id, project_path, multi_project_mode, and company_dir.
        Falls back to sensible defaults in legacy mode.
    """
    project_info = get_current_project()

    if project_info:
        return {
            "project_id": project_info["project_id"],
            "project_path": str(project_info["project_path"]),
            "multi_project_mode": True,
            "company_dir": str(project_info["company_dir"]),
            "company_root": str(project_info["company_root"]),
        }
    else:
        # Legacy mode fallback
        cwd = Path.cwd()
        return {
            "project_id": get_project_id(cwd),
            "project_path": str(cwd),
            "multi_project_mode": False,
            "company_dir": str(get_company_dir()),
            "company_root": None,
        }


# -----------------------------------------------------------------------------
# Notification
# -----------------------------------------------------------------------------


def send_notification(title: str, message: str):
    """Send desktop notification via notify.py hook."""
    # Use company root for multi-project mode, otherwise cwd
    company_root = find_company_root()
    base_path = company_root if company_root else Path.cwd()
    notify_path = base_path / NOTIFY_HOOK

    if notify_path.exists():
        try:
            payload = json.dumps({"message": f"{title}: {message}"})
            subprocess.run(
                ["python3", str(notify_path)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            # Fallback to terminal bell
            print("\a", end="")
    else:
        # Terminal bell fallback
        print("\a", end="")


# -----------------------------------------------------------------------------
# External Notification Configuration
# -----------------------------------------------------------------------------


def load_forge_config() -> dict:
    """
    Load gatesmith.json to get external services configuration.

    Returns:
        Configuration dict, or empty dict if not found/invalid.
    """
    # Try project root first, then company root
    search_paths = []

    # Current working directory
    cwd = Path.cwd()
    search_paths.append(cwd / FORGE_CONFIG_FILE)
    search_paths.append(cwd / ".claude" / FORGE_CONFIG_FILE)

    # Company root if in multi-project mode
    company_root = find_company_root()
    if company_root:
        search_paths.append(company_root / FORGE_CONFIG_FILE)
        search_paths.append(company_root / ".claude" / FORGE_CONFIG_FILE)

    for config_path in search_paths:
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

    return {}


def get_tier_notification_services(tier: int) -> list[str]:
    """
    Get the list of external services to notify for a given escalation tier.

    Reads from gatesmith.json if available, otherwise uses defaults.

    Routing:
        Tier 1-2: Internal only (no external dispatch)
        Tier 3: Slack notification
        Tier 4: Slack + Discord + all configured webhooks

    Args:
        tier: Escalation tier (1-4)

    Returns:
        List of service names to dispatch to (e.g., ["slack", "discord"])
    """
    config = load_forge_config()

    # Check for custom configuration
    external_services = config.get("externalServices", {})
    escalation_routing = external_services.get("escalationRouting", {})

    # Try tier-specific configuration
    tier_key = f"tier{tier}"
    if tier_key in escalation_routing:
        return escalation_routing[tier_key]

    # Try numeric tier key
    if str(tier) in escalation_routing:
        return escalation_routing[str(tier)]

    # Fall back to defaults
    return DEFAULT_TIER_NOTIFICATION_ROUTING.get(tier, [])


# -----------------------------------------------------------------------------
# Opt-in notifications (osascript + webhook) — configured under notifications.escalation
# in gatesmith.json; default off; errors never affect escalation handling.
# -----------------------------------------------------------------------------


def _get_opt_in_notifications_config() -> dict:
    """Return the notifications.escalation block from the governing config, or {}.

    The root gatesmith.json governs when it declares the block, else the
    install template (see governing_config). An unreadable governing file
    leaves both channels off.
    """
    company_root = find_company_root()
    project_root = company_root if company_root else Path.cwd()
    value, error = governing_config.section("notifications.escalation", project_root)
    if error:
        return {}
    return value


def _send_osascript_notification(
    task_id: str, tier: int, reason: str, task_title: str
) -> None:
    """Fire a macOS desktop notification via osascript. Fail-open on any error."""
    try:
        title = "Gatesmith Escalation"
        # Sanitise quotes so the osascript -e string stays well-formed
        safe_title = task_title.replace('"', "'")
        safe_reason = reason.replace('"', "'")
        body = f"Task {task_id} ({safe_title}) → Tier {tier}: {safe_reason}"
        script = f'display notification "{body}" with title "{title}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass  # Never affect escalation handling


WEBHOOK_TIMEOUT_SECONDS = 10
WEBHOOK_ALLOWED_SCHEMES = ("http", "https")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 3xx surfaces as HTTPError instead of being followed.

    Following a redirect would bypass the scheme allowlist and forward the
    configured headers (for example Authorization) to a host the user never set.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _webhook_host(url: object) -> str:
    """Return only the host part of a webhook URL (never path, query, or payload)."""
    try:
        return urllib.parse.urlsplit(str(url)).hostname or "<unknown>"
    except Exception:
        return "<unparseable>"


def _webhook_error_label(exc: BaseException) -> str:
    """Describe a failure by class (and HTTP status) only.

    str(exc) is never used: it can echo the URL path or query, or a header value.
    """
    label = type(exc).__name__
    if isinstance(exc, urllib.error.HTTPError):
        return f"{label} {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{label} ({type(exc.reason).__name__})"
    return label


def _validated_header_items(headers: dict) -> list[tuple[str, str]]:
    """Return header pairs, rejecting anything that could break the request line."""
    items = []
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("header names and values must be strings")
        if any(c in name + value for c in "\r\n\0"):
            raise ValueError("header contains a control character")
        items.append((name, value))
    return items


def _send_webhook_notification(url: str, payload: dict, headers: dict) -> None:
    """POST a JSON escalation payload to the configured webhook URL. Fail-open.

    Uses only the standard library so it works on a plain install. Redirects are
    refused. On any failure, writes one warning line to stderr naming the URL host
    and the error class. Neither the payload nor the URL path/query is logged.
    """
    try:
        if not isinstance(url, str):
            raise ValueError("webhook url must be a string")
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in WEBHOOK_ALLOWED_SCHEMES:
            raise ValueError(f"unsupported scheme {scheme!r}")
        body = json.dumps(payload).encode("utf-8")
        merged = {
            "Content-Type": "application/json",
            **dict(_validated_header_items(headers)),
        }
        request = urllib.request.Request(url, data=body, headers=merged, method="POST")
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=WEBHOOK_TIMEOUT_SECONDS):
            pass
    except Exception as exc:  # Never affect escalation handling
        print(
            f"[escalation] webhook POST to {_webhook_host(url)} failed: "
            f"{_webhook_error_label(exc)}",
            file=sys.stderr,
        )


def fire_escalation_opt_in_notifications(
    record: "EscalationRecord", task_title: str
) -> None:
    """Send opt-in notifications when an escalation is created or advanced.

    Reads notifications.escalation from gatesmith.json.  Both channels are
    off by default.  Any error is swallowed — this must never affect the
    escalation record or its persistence.
    """
    notif_cfg = _get_opt_in_notifications_config()

    osascript_cfg = notif_cfg.get("osascript", {})
    if osascript_cfg.get("enabled", False):
        _send_osascript_notification(
            record.task_id,
            record.current_tier,
            record.trigger,
            task_title,
        )

    webhook_cfg = notif_cfg.get("webhook", {})
    if webhook_cfg.get("enabled", False) and webhook_cfg.get("url"):
        payload: dict = {
            "task_id": record.task_id,
            "task_title": task_title,
            "tier": record.current_tier,
            "reason": record.trigger,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        _send_webhook_notification(
            webhook_cfg["url"],
            payload,
            webhook_cfg.get("headers", {}),
        )


def _get_notification_cooldown_key(task_id: str, tier: int) -> str:
    """Generate a unique key for rate limiting notifications."""
    return f"escalation:{task_id}:{tier}"


def _check_notification_cooldown(task_id: str, tier: int) -> bool:
    """
    Check if a notification is allowed (not rate limited).

    Prevents notification spam by enforcing a cooldown period between
    notifications for the same task and tier.

    Args:
        task_id: The task ID being escalated
        tier: The escalation tier

    Returns:
        True if notification is allowed, False if rate limited
    """
    ec = _ensure_external_connectors()
    if ec is None:
        # If external_connectors not available, allow notification
        return True

    rate_limiter = ec.get_rate_limiter()

    # Use a synthetic service name for escalation rate limiting
    service_key = f"escalation_tier_{tier}"

    # Check if we can proceed (but don't consume token yet)
    return rate_limiter.can_proceed(service_key)


def _acquire_notification_token(task_id: str, tier: int) -> bool:
    """
    Acquire a rate limit token for escalation notification.

    Args:
        task_id: The task ID being escalated
        tier: The escalation tier

    Returns:
        True if token acquired, False if rate limited
    """
    ec = _ensure_external_connectors()
    if ec is None:
        return True

    rate_limiter = ec.get_rate_limiter()
    service_key = f"escalation_tier_{tier}"

    # Configure rate limit if not set (1 notification per 5 minutes per tier)
    # This prevents notification spam while allowing urgent escalations
    rate_per_minute = 60 / (ESCALATION_NOTIFICATION_COOLDOWN_SECONDS / 60)
    rate_limiter.configure(service_key, int(rate_per_minute))

    return rate_limiter.acquire(service_key)


# -----------------------------------------------------------------------------
# External Notification Dispatch
# -----------------------------------------------------------------------------


def dispatch_escalation_notification(escalation: dict) -> list[dict]:
    """
    Dispatch escalation notifications to external services based on tier.

    Routes escalations to appropriate notification channels:
        - Tier 1-2: Internal only (no external dispatch)
        - Tier 3: Slack notification
        - Tier 4: Slack + Discord + all configured webhooks

    The notification includes full escalation context:
        - Task ID, title, and assignee
        - Escalation reason and current tier
        - Time elapsed since initial assignment
        - Suggested actions based on tier

    Args:
        escalation: Escalation record dict containing:
            - task_id: str
            - current_tier: int
            - trigger: str
            - original_agent: str | None
            - current_agent: str | None
            - created_at: str (ISO timestamp)
            - metadata: dict (with task_title)
            - events: list[dict]

    Returns:
        List of dispatch result dicts, each containing:
            - service: str (service name)
            - success: bool
            - message: str
            - error: str | None
            - rate_limited: bool

    Example:
        >>> from escalation import dispatch_escalation_notification
        >>> results = dispatch_escalation_notification({
        ...     "task_id": "task-123",
        ...     "current_tier": 3,
        ...     "trigger": "timeout",
        ...     "original_agent": "senior-python-developer",
        ...     "metadata": {"task_title": "Implement feature X"},
        ...     "created_at": "2025-01-15T10:00:00Z",
        ... })
        >>> for r in results:
        ...     print(f"{r['service']}: {'OK' if r['success'] else 'FAILED'}")
    """
    results: list[dict] = []

    # Extract escalation info
    task_id = escalation.get("task_id", "unknown")
    current_tier = escalation.get("current_tier", 1)

    # Get services to notify based on tier
    services = get_tier_notification_services(current_tier)

    # Tier 1-2: Internal only
    if not services:
        return [
            {
                "service": "internal",
                "success": True,
                "message": f"Tier {current_tier} escalation - internal handling only",
                "error": None,
                "rate_limited": False,
            }
        ]

    # Check rate limiting for this tier
    if not _acquire_notification_token(task_id, current_tier):
        return [
            {
                "service": "rate_limiter",
                "success": False,
                "message": (
                    f"Rate limited - tier {current_tier} notifications throttled"
                ),
                "error": "Notification cooldown in effect",
                "rate_limited": True,
            }
        ]

    # Build alert payload with full escalation context
    alert = _build_escalation_alert(escalation)

    # Try to import external_connectors
    ec = _ensure_external_connectors()
    if ec is None:
        return [
            {
                "service": "external_connectors",
                "success": False,
                "message": "External connectors module not available",
                "error": "Could not import external_connectors",
                "rate_limited": False,
            }
        ]

    # Dispatch to external services
    try:
        connector_results = ec.dispatch_to_external_services(alert, services)

        # Convert ConnectorResult objects to dicts
        for cr in connector_results:
            results.append(
                {
                    "service": cr.service,
                    "success": cr.success,
                    "message": cr.message,
                    "error": cr.error,
                    "rate_limited": cr.rate_limited,
                }
            )

    except Exception as e:
        results.append(
            {
                "service": "dispatcher",
                "success": False,
                "message": "Dispatch failed with exception",
                "error": str(e),
                "rate_limited": False,
            }
        )

    return results


def _build_escalation_alert(escalation: dict) -> dict:
    """
    Build an alert payload from escalation record with full context.

    Creates a structured alert suitable for dispatch_to_external_services().

    Args:
        escalation: Escalation record dict

    Returns:
        Alert dict with:
            - severity: "critical" | "warning" | "info"
            - message: Formatted escalation message
            - rule_id: "escalation"
            - task_id: The task ID
            - task_name: Task title from metadata
            - details: Additional escalation context
    """
    task_id = escalation.get("task_id", "unknown")
    current_tier = escalation.get("current_tier", 1)
    trigger = escalation.get("trigger", "unknown")
    original_agent = escalation.get("original_agent", "unassigned")
    current_agent = escalation.get("current_agent", original_agent)
    created_at = escalation.get("created_at", "")
    metadata = escalation.get("metadata", {})
    task_title = metadata.get("task_title", "Untitled task")
    status = escalation.get("status", "unknown")

    # Calculate time elapsed since escalation started
    elapsed_str = "unknown"
    if created_at:
        try:
            created_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - created_time
            elapsed_minutes = int(elapsed.total_seconds() / 60)
            if elapsed_minutes < 60:
                elapsed_str = f"{elapsed_minutes}m"
            else:
                elapsed_hours = elapsed_minutes // 60
                remaining_mins = elapsed_minutes % 60
                elapsed_str = f"{elapsed_hours}h {remaining_mins}m"
        except (ValueError, TypeError):
            pass

    # Map tier to severity
    if current_tier >= 4:
        severity = "critical"
    elif current_tier == 3:
        severity = "warning"
    else:
        severity = "info"

    # Map trigger to human-readable description
    trigger_descriptions = {
        "timeout": "Task exceeded expected duration",
        "explicit_block": "Agent reported being blocked",
        "repeated_failure": "Task failed multiple times",
        "capability_mismatch": "Agent lacks required capabilities",
        "resource_contention": "Resource conflict detected",
        "quality_rejection": "Work rejected by reviewer",
    }
    trigger_desc = trigger_descriptions.get(trigger, trigger)

    # Get tier info for suggested actions
    tier_info = TIER_CONFIG.get(current_tier, {})
    tier_name = tier_info.get("name", f"Tier {current_tier}")
    tier_action = tier_info.get("description", "")

    # Build message
    message = (
        f"Escalation Tier {current_tier} ({tier_name}): {task_title}\n"
        f"Reason: {trigger_desc}"
    )

    # Build alert
    return {
        "severity": severity,
        "message": message,
        "rule_id": "escalation",
        "task_id": task_id,
        "task_name": task_title,
        "details": {
            "tier": current_tier,
            "tier_name": tier_name,
            "trigger": trigger,
            "trigger_description": trigger_desc,
            "original_assignee": original_agent,
            "current_assignee": current_agent,
            "time_elapsed": elapsed_str,
            "status": status,
            "suggested_action": tier_action,
        },
    }


# -----------------------------------------------------------------------------
# Queue Integration
# -----------------------------------------------------------------------------


def load_queue() -> dict:
    """Load work queue from file."""
    queue_path = get_queue_path()

    if not queue_path.exists():
        return {"pending": [], "in_progress": [], "blocked": [], "completed": []}

    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"pending": [], "in_progress": [], "blocked": [], "completed": []}


def get_task_from_queue(task_id: str) -> dict | None:
    """Get a task from the work queue."""
    queue = load_queue()

    for status in ["pending", "in_progress", "blocked", "completed"]:
        for task in queue.get(status, []):
            if task.get("task_id") == task_id:
                return task

    return None


def _close_task_in_queue(
    task_id: str, resolution: str, resolved_by: str | None
) -> bool:
    """Move the task to the terminal "cancelled" lane with the resolution.

    P69: a task whose escalation is resolved must not be picked up again.
    It used to be deleted from the queue, without the queue lock, leaving no
    record of what happened to it (2026-10-31). The work allocator's own
    cancel keeps the task, its reason and who closed it, under the lock.

    Returns:
        True if a pending, in-progress or blocked task was closed.
    """
    try:
        import work_allocator
    except ImportError:
        return False
    try:
        result = work_allocator.update_task(
            task_id,
            status=work_allocator.CANCELLED_LANE,
            reason=f"escalation resolved: {resolution}",
            actor=resolved_by or "operator",
            only_from=("pending", "in_progress", "blocked"),
        )
    except Exception:
        return False
    return bool(result.get("success"))


def load_escalation(task_id: str) -> EscalationRecord | None:
    """Load escalation record for a task."""
    esc_path = get_escalation_path(task_id)

    if not esc_path.exists():
        return None

    try:
        with open(esc_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return EscalationRecord(**data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def save_escalation(record: EscalationRecord):
    """Save escalation record to file."""
    ensure_escalations_dir()
    esc_path = get_escalation_path(record.task_id)

    record.updated_at = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to temp file then rename to avoid truncation races
    fd, tmp_path = tempfile.mkstemp(dir=str(esc_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(record), f, indent=2)
        os.replace(tmp_path, str(esc_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_escalations(
    tier: int | None = None,
    status: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """
    List all escalation records, optionally filtered.

    Args:
        tier: Filter by escalation tier (1-4)
        status: Filter by status (pending, in_progress, resolved, paused)
        project_id: Filter by project ID (for cross-project visibility)

    Returns:
        List of escalation records matching the filters.
    """
    escalations_dir = get_escalations_dir()

    if not escalations_dir.exists():
        return []

    results = []

    for esc_file in escalations_dir.glob("*.json"):
        try:
            with open(esc_file, "r", encoding="utf-8") as f:
                record = json.load(f)

                # Apply filters
                if tier is not None and record.get("current_tier") != tier:
                    continue
                if status is not None and record.get("status") != status:
                    continue
                if project_id is not None and record.get("project_id") != project_id:
                    continue

                results.append(record)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by updated_at descending
    results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

    return results


def list_cross_project_escalations(
    tier: int | None = None,
    status: str | None = None,
) -> dict:
    """
    List all escalations across all projects in the company.

    This function provides cross-project visibility for escalations in
    multi-project mode. It groups escalations by project for easy review.

    Args:
        tier: Filter by escalation tier (1-4)
        status: Filter by status (pending, in_progress, resolved, paused)

    Returns:
        Dict with:
        - multi_project_mode: bool indicating if multi-project mode is active
        - company_root: str path to company root (if multi-project)
        - total_count: total number of escalations
        - by_project: dict mapping project_id to list of escalations
        - escalations: flat list of all escalations (for compatibility)
    """
    if not is_multi_project_mode():
        # Legacy mode - just return regular list
        escalations = list_escalations(tier=tier, status=status)
        return {
            "multi_project_mode": False,
            "company_root": None,
            "total_count": len(escalations),
            "by_project": {},
            "escalations": escalations,
        }

    escalations = list_escalations(tier=tier, status=status)

    # Group by project
    by_project: dict[str, list[dict]] = {}
    for esc in escalations:
        proj_id = esc.get("project_id") or "unknown"
        if proj_id not in by_project:
            by_project[proj_id] = []
        by_project[proj_id].append(esc)

    company_root = find_company_root()

    return {
        "multi_project_mode": True,
        "company_root": str(company_root) if company_root else None,
        "total_count": len(escalations),
        "by_project": by_project,
        "escalations": escalations,
    }


# -----------------------------------------------------------------------------
# Trigger Detection Functions
# -----------------------------------------------------------------------------


def check_timeout_trigger(
    task: dict, escalation: EscalationRecord | None
) -> dict | None:
    """
    Check if task has exceeded 1.5x expected duration.

    Returns trigger details if triggered, None otherwise.
    """
    started_at = task.get("started_at")
    if not started_at:
        return None

    # Parse complexity to estimate duration
    complexity = task.get("estimated_complexity", "standard")
    estimated_hours = {
        "trivial": 1,
        "standard": 4,
        "complex": 8,
        "epic": 16,
    }.get(complexity, 4)

    estimated_minutes = estimated_hours * 60
    threshold = (
        estimated_minutes * TRIGGER_CONFIG[EscalationTrigger.TIMEOUT]["threshold"]
    )

    try:
        start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() / 60

        if elapsed > threshold:
            return {
                "trigger": EscalationTrigger.TIMEOUT,
                "elapsed_minutes": round(elapsed, 1),
                "threshold_minutes": round(threshold, 1),
                "exceeded_by_minutes": round(elapsed - threshold, 1),
            }
    except (ValueError, TypeError):
        pass

    return None


def check_explicit_block_trigger(task: dict) -> dict | None:
    """
    Check if agent has explicitly reported being blocked.

    Looks for block indicators in task notes or status.
    """
    notes = task.get("notes", [])

    block_keywords = ["blocked", "waiting on", "cannot proceed", "stuck", "need help"]

    for note in notes:
        content = note.get("content", "").lower()
        if any(keyword in content for keyword in block_keywords):
            return {
                "trigger": EscalationTrigger.EXPLICIT_BLOCK,
                "blocking_note": note.get("content", "")[:200],
                "reported_at": note.get("timestamp"),
            }

    return None


def check_repeated_failure_trigger(task: dict) -> dict | None:
    """
    Check if task has failed 3+ times.

    Looks at release history and failure notes.
    """
    release_history = task.get("release_history", [])
    threshold = TRIGGER_CONFIG[EscalationTrigger.REPEATED_FAILURE]["threshold"]

    if len(release_history) >= threshold:
        return {
            "trigger": EscalationTrigger.REPEATED_FAILURE,
            "failure_count": len(release_history),
            "threshold": threshold,
            "history": release_history[-3:],  # Last 3 releases
        }

    return None


def check_capability_mismatch_trigger(
    task: dict, agent_id: str | None = None
) -> dict | None:
    """
    Check if assigned agent lacks required capabilities.

    Note: Full implementation requires agent registry with capabilities.
    This is a placeholder that checks for explicit mismatch notes.
    """
    notes = task.get("notes", [])

    mismatch_keywords = [
        "missing capability",
        "cannot",
        "don't know how",
        "need specialist",
        "outside expertise",
        "not qualified",
    ]

    for note in notes:
        content = note.get("content", "").lower()
        if any(keyword in content for keyword in mismatch_keywords):
            return {
                "trigger": EscalationTrigger.CAPABILITY_MISMATCH,
                "mismatch_note": note.get("content", "")[:200],
                "required_capabilities": task.get("required_capabilities", []),
                "agent_id": agent_id or task.get("assigned_to"),
            }

    return None


def check_resource_contention_trigger(task: dict) -> dict | None:
    """
    Check if multiple agents need the same resource.

    Looks for contention indicators in notes.
    """
    notes = task.get("notes", [])

    contention_keywords = [
        "resource conflict",
        "locked by",
        "in use by",
        "contention",
        "waiting for lock",
        "concurrent access",
    ]

    for note in notes:
        content = note.get("content", "").lower()
        if any(keyword in content for keyword in contention_keywords):
            return {
                "trigger": EscalationTrigger.RESOURCE_CONTENTION,
                "contention_note": note.get("content", "")[:200],
                "reported_at": note.get("timestamp"),
            }

    return None


def check_quality_rejection_trigger(task: dict) -> dict | None:
    """
    Check if work has been rejected by reviewer 2+ times.
    """
    notes = task.get("notes", [])
    threshold = TRIGGER_CONFIG[EscalationTrigger.QUALITY_REJECTION]["threshold"]

    rejection_keywords = ["rejected", "needs changes", "failed review", "quality issue"]
    rejection_count = 0
    rejection_notes = []

    for note in notes:
        content = note.get("content", "").lower()
        if any(keyword in content for keyword in rejection_keywords):
            rejection_count += 1
            rejection_notes.append(note.get("content", "")[:100])

    if rejection_count >= threshold:
        return {
            "trigger": EscalationTrigger.QUALITY_REJECTION,
            "rejection_count": rejection_count,
            "threshold": threshold,
            "rejection_notes": rejection_notes[-2:],  # Last 2 rejections
        }

    return None


# -----------------------------------------------------------------------------
# Main Functions
# -----------------------------------------------------------------------------


def check_escalation_needed(task_id: str) -> dict:
    """
    Check if escalation is needed for a task.

    Runs all trigger detection functions and returns the highest priority trigger.

    Args:
        task_id: The task ID to check

    Returns:
        Dict with:
        - needs_escalation: bool
        - trigger: str (if triggered)
        - trigger_details: dict (if triggered)
        - recommended_tier: int (if triggered)
        - existing_escalation: dict (if exists)
    """
    task = get_task_from_queue(task_id)

    if not task:
        return {
            "needs_escalation": False,
            "error": f"Task {task_id} not found in queue",
        }

    existing = load_escalation(task_id)

    # Check all triggers in priority order
    triggers_to_check = [
        (
            EscalationTrigger.RESOURCE_CONTENTION,
            check_resource_contention_trigger,
            (task,),
        ),
        (EscalationTrigger.REPEATED_FAILURE, check_repeated_failure_trigger, (task,)),
        (EscalationTrigger.EXPLICIT_BLOCK, check_explicit_block_trigger, (task,)),
        (EscalationTrigger.QUALITY_REJECTION, check_quality_rejection_trigger, (task,)),
        (
            EscalationTrigger.CAPABILITY_MISMATCH,
            check_capability_mismatch_trigger,
            (task,),
        ),
        (EscalationTrigger.TIMEOUT, check_timeout_trigger, (task, existing)),
    ]

    for trigger_name, check_func, args in triggers_to_check:
        result = check_func(*args)
        if result:
            initial_tier = TRIGGER_CONFIG[trigger_name]["initial_tier"]

            # If already escalated, recommend next tier
            recommended_tier = initial_tier
            if existing and existing.status != "resolved":
                recommended_tier = min(existing.current_tier + 1, EscalationTier.HUMAN)

            return {
                "needs_escalation": True,
                "trigger": trigger_name,
                "trigger_details": result,
                "recommended_tier": recommended_tier,
                "tier_name": TIER_CONFIG[recommended_tier]["name"],
                "tier_action": TIER_CONFIG[recommended_tier]["action"],
                "existing_escalation": asdict(existing) if existing else None,
            }

    return {
        "needs_escalation": False,
        "task_id": task_id,
        "existing_escalation": asdict(existing) if existing else None,
    }


def escalate(task_id: str, reason: str, notes: str | None = None) -> dict:
    """
    Escalate a task to the appropriate tier.

    Args:
        task_id: The task ID to escalate
        reason: The trigger reason (timeout, explicit_block, etc.)
        notes: Optional notes about the escalation

    Returns:
        Dict with escalation result
    """
    task = get_task_from_queue(task_id)

    if not task:
        return {
            "success": False,
            "error": f"Task {task_id} not found in queue",
        }

    # Circular escalation prevention (WS-097)
    # Blocks escalations about fixing failing PRs/CI to prevent infinite loops
    task_title_lower = (task.get("title") or "").lower()
    task_desc_lower = (task.get("description") or "").lower()
    circular_keywords = [
        "fix failing",
        "fix test",
        "fix lint",
        "fix ci",
        "failing pr",
        "stuck pr",
        "escalation",
        "pre-merge",
    ]
    is_meta_fix_task = any(
        kw in task_title_lower or kw in task_desc_lower for kw in circular_keywords
    )
    if is_meta_fix_task and reason in ("max_retries", "test_failure", "timeout"):
        return {
            "success": False,
            "error": "Circular escalation blocked: task is fixing a prior escalation",
            "task_id": task_id,
            "reason": reason,
        }

    # Validate reason
    if reason not in TRIGGER_CONFIG:
        return {
            "success": False,
            "error": f"Invalid escalation reason: {reason}",
            "valid_reasons": list(TRIGGER_CONFIG.keys()),
        }

    now = datetime.now(timezone.utc).isoformat()
    existing = load_escalation(task_id)

    if existing and existing.status != "resolved":
        # Escalate to next tier
        new_tier = min(existing.current_tier + 1, EscalationTier.HUMAN)

        event = EscalationEvent(
            timestamp=now,
            tier=new_tier,
            trigger=reason,
            action_taken=TIER_CONFIG[new_tier]["action"],
            notes=notes,
        )

        existing.current_tier = new_tier
        existing.status = (
            "paused" if new_tier == EscalationTier.HUMAN else "in_progress"
        )
        existing.trigger = reason
        existing.events.append(asdict(event))

        record = existing
    else:
        # Create new escalation
        initial_tier = TRIGGER_CONFIG[reason]["initial_tier"]

        event = EscalationEvent(
            timestamp=now,
            tier=initial_tier,
            trigger=reason,
            action_taken=TIER_CONFIG[initial_tier]["action"],
            notes=notes,
        )

        # Get project context for cross-project visibility
        project_ctx = get_project_context()

        record = EscalationRecord(
            task_id=task_id,
            current_tier=initial_tier,
            status="paused" if initial_tier == EscalationTier.HUMAN else "in_progress",
            created_at=now,
            updated_at=now,
            original_agent=task.get("assigned_to"),
            current_agent=task.get("assigned_to"),
            trigger=reason,
            trigger_details={
                "reason": reason,
                "description": TRIGGER_CONFIG[reason]["description"],
            },
            events=[asdict(event)],
            metadata={"task_title": task.get("title", "")},
            # Multi-project context (v1.2)
            project_id=project_ctx["project_id"],
            project_path=project_ctx["project_path"],
            multi_project_mode=project_ctx["multi_project_mode"],
        )

    save_escalation(record)

    # Opt-in notifications (osascript + webhook) — all tiers, default off.
    # Wrapped in try/except so any unexpected bug in the notification layer
    # can never surface to the caller or corrupt the escalation result.
    try:
        fire_escalation_opt_in_notifications(record, task.get("title", ""))
    except Exception:
        pass

    # Send desktop notification for Tier 4 (Human)
    if record.current_tier == EscalationTier.HUMAN:
        send_notification(
            "ESCALATION: Human Required",
            f"Task {task_id} requires human intervention. Reason: {reason}",
        )

    # Dispatch to external services for Tier 3-4 escalations
    external_dispatch_results: list[dict] = []
    if record.current_tier >= EscalationTier.COORDINATOR:
        external_dispatch_results = dispatch_escalation_notification(asdict(record))

    tier_info = TIER_CONFIG[record.current_tier]

    return {
        "success": True,
        "task_id": task_id,
        "escalation": asdict(record),
        "current_tier": record.current_tier,
        "tier_name": tier_info["name"],
        "action": tier_info["action"],
        "description": tier_info["description"],
        "timeout_minutes": tier_info["timeout_minutes"],
        "human_notification_sent": record.current_tier == EscalationTier.HUMAN,
        "external_notifications": external_dispatch_results,
    }


def resolve_escalation(
    task_id: str, resolution: str, resolved_by: str | None = None
) -> dict:
    """
    Resolve an escalation.

    Args:
        task_id: The task ID to resolve
        resolution: Description of how the escalation was resolved
        resolved_by: Who resolved the escalation

    Returns:
        Dict with resolution result
    """
    record = load_escalation(task_id)

    if not record:
        return {
            "success": False,
            "error": f"No escalation found for task {task_id}",
        }

    if record.status == "resolved":
        return {
            "success": False,
            "error": f"Escalation for task {task_id} is already resolved",
        }

    now = datetime.now(timezone.utc).isoformat()

    # Update the last event as resolved
    if record.events:
        record.events[-1]["resolved"] = True
        record.events[-1]["resolution"] = resolution
        record.events[-1]["resolved_at"] = now
        record.events[-1]["resolved_by"] = resolved_by

    record.status = "resolved"
    record.updated_at = now

    save_escalation(record)

    # P69 FIX: a resolved escalation closes its task, or it is picked up
    # again and loops. Closed as cancelled, so the record stays.
    queue_closed = _close_task_in_queue(task_id, resolution, resolved_by)

    return {
        "success": True,
        "task_id": task_id,
        "resolution": resolution,
        "resolved_at": now,
        "resolved_by": resolved_by,
        "queue_task_closed": queue_closed,
        "escalation": asdict(record),
    }


def get_escalation_history(task_id: str) -> dict:
    """
    Get the full escalation history for a task.

    Args:
        task_id: The task ID

    Returns:
        Dict with escalation history
    """
    record = load_escalation(task_id)

    if not record:
        return {
            "success": False,
            "error": f"No escalation found for task {task_id}",
        }

    return {
        "success": True,
        "task_id": task_id,
        "escalation": asdict(record),
        "event_count": len(record.events),
        "current_tier": record.current_tier,
        "tier_name": TIER_CONFIG[record.current_tier]["name"],
        "status": record.status,
    }


# -----------------------------------------------------------------------------
# Archive Functions
# -----------------------------------------------------------------------------


def get_archive_dir() -> Path:
    """Get the archive directory for escalations."""
    company_dir = get_company_dir()
    return company_dir / ARCHIVE_DIR


def ensure_archive_dir():
    """Ensure the archive directory exists."""
    archive_dir = get_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)


def is_stale(record: dict, stale_hours: int = 48) -> bool:
    """
    Check if an escalation record is stale.

    Args:
        record: The escalation record dict
        stale_hours: Hours after which a record is considered stale

    Returns:
        True if the record is stale
    """
    timestamp_str = record.get("updated_at") or record.get("created_at")
    if not timestamp_str:
        return False

    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_hours = (now - timestamp).total_seconds() / 3600
        return age_hours > stale_hours
    except (ValueError, TypeError):
        return False


def archive_escalation(task_id: str, reason: str = "manual") -> dict:
    """
    Archive a single escalation.

    Args:
        task_id: The task ID to archive
        reason: Reason for archiving (stale, manual, etc.)

    Returns:
        Dict with result
    """
    esc_path = get_escalation_path(task_id)

    if not esc_path.exists():
        return {
            "success": False,
            "error": f"Escalation file not found for {task_id}",
        }

    ensure_archive_dir()
    archive_dir = get_archive_dir()
    archive_path = archive_dir / f"{task_id}.json"

    # Read the escalation data
    with open(esc_path) as f:
        data = json.load(f)

    # Add archive metadata
    data["archived_at"] = datetime.now(timezone.utc).isoformat()
    data["archive_reason"] = reason

    # Write to archive
    with open(archive_path, "w") as f:
        json.dump(data, f, indent=2)

    # Remove original
    esc_path.unlink()

    return {
        "success": True,
        "task_id": task_id,
        "reason": reason,
        "archived_to": str(archive_path),
    }


def archive_stale_escalations(stale_hours: int = 48, dry_run: bool = False) -> dict:
    """
    Archive all stale escalations.

    Args:
        stale_hours: Hours after which a record is considered stale
        dry_run: If True, report what would be archived without actually archiving

    Returns:
        Dict with result
    """
    esc_dir = get_escalations_dir()
    if not esc_dir.exists():
        return {
            "success": True,
            "stale_count": 0,
            "archived_count": 0,
            "archived": [],
            "stale_escalations": [],
            "errors": [],
            "dry_run": dry_run,
        }

    stale_escalations = []
    archived = []
    errors = []

    for esc_file in esc_dir.glob("*.json"):
        try:
            with open(esc_file) as f:
                data = json.load(f)
            task_id = data.get("task_id", esc_file.stem)
            if is_stale(data, stale_hours):
                stale_escalations.append({"task_id": task_id})
                if not dry_run:
                    result = archive_escalation(task_id, reason="stale")
                    if result["success"]:
                        archived.append(task_id)
                    else:
                        errors.append({"task_id": task_id, "error": result["error"]})
        except Exception as e:
            errors.append({"file": str(esc_file), "error": str(e)})

    return {
        "success": True,
        "stale_count": len(stale_escalations),
        "archived_count": len(archived),
        "archived": archived,
        "stale_escalations": stale_escalations,
        "errors": errors,
        "dry_run": dry_run,
    }


def list_archived_escalations(status: str | None = None) -> list[dict]:
    """
    List archived escalations.

    Args:
        status: Optional status filter

    Returns:
        List of archived escalation records
    """
    archive_dir = get_archive_dir()
    if not archive_dir.exists():
        return []

    result = []
    for archive_file in archive_dir.glob("*.json"):
        try:
            with open(archive_file) as f:
                data = json.load(f)
            if status is None or data.get("status") == status:
                result.append(data)
        except Exception:
            continue

    return result


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Escalation Manager — 4-tier escalation system (v1.2 multi-project)

Commands:
    check       Check if escalation is needed for a task
    escalate    Trigger escalation for a task
    list        List active escalations
    cross       List escalations across all projects (multi-project mode)
    resolve     Resolve an escalation
    history     Get escalation history for a task
    context     Show current project context
    archive     Archive stale escalations
    archived    List archived escalations
    notify      Test external notification dispatch for an escalation
    routing     Show notification routing configuration for each tier

Escalation Tiers:
    Tier 1: Peer Agent (15m) - reassign to capable peer (internal only)
    Tier 2: Department Head (30m) - escalate to department head (internal only)
    Tier 3: Coordinator (60m) - escalate to coordinator (+ Slack notification)
    Tier 4: Human (120m) - notify human and pause (+ Slack, Discord, webhooks)

Escalation Triggers:
    timeout            - Task exceeds 1.5x expected duration
    explicit_block     - Agent explicitly reports being blocked
    repeated_failure   - Same task failed 3+ times
    capability_mismatch - Agent lacks required capabilities
    resource_contention - Multiple agents need same resource
    quality_rejection   - Work rejected by reviewer 2+ times

Check options:
    --task-id ID       Task ID to check (required)

Escalate options:
    --task-id ID       Task ID to escalate (required)
    --reason REASON    Trigger reason (required)
    --notes TEXT       Optional notes

List options:
    --tier 1-4         Filter by tier
    --status STATUS    Filter by status (pending, in_progress, resolved, paused)
    --project-id ID    Filter by project ID (multi-project mode)

Cross options (multi-project):
    --tier 1-4         Filter by tier
    --status STATUS    Filter by status

Resolve options:
    --task-id ID       Task ID to resolve (required)
    --resolution TEXT  Resolution description (required)
    --resolved-by ID   Who resolved it

History options:
    --task-id ID       Task ID (required)

Archive options:
    --dry-run          Show what would be archived without archiving
    --task-id ID       Archive a specific escalation
    --stale-hours N    Hours after which an escalation is stale (default: 48)

Archived options:
    --status STATUS    Filter by status

Notify options (external notification dispatch):
    --task-id ID       Task ID to notify about (required)
    --dry-run          Show what would be sent without sending

Routing options:
    (no options)       Show current tier-to-service routing configuration

External Notification Routing:
    Tier 1-2: Internal only (no external dispatch)
    Tier 3:   Slack notification
    Tier 4:   Slack + Discord + all configured webhooks

    Configure custom routing in gatesmith.json:
    {
      "externalServices": {
        "escalationRouting": {
          "tier3": ["slack"],
          "tier4": ["slack", "discord", "generic_webhook"]
        }
      }
    }

Examples:
    # Check if task needs escalation
    python escalation.py check --task-id task-123

    # Escalate due to timeout
    python escalation.py escalate --task-id task-123 --reason timeout

    # List all pending escalations at Tier 2+
    python escalation.py list --tier 2 --status pending

    # List escalations for a specific project
    python escalation.py list --project-id myproject-a1b2c3

    # List escalations across all projects
    python escalation.py cross --status pending

    # Show current project context
    python escalation.py context

    # Resolve an escalation
    python escalation.py resolve --task-id task-123 --resolution "Reassigned to senior-eng"

    # Test external notification for an escalation (dry-run)
    python escalation.py notify --task-id task-123 --dry-run

    # Show notification routing configuration
    python escalation.py routing
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
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "check":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = check_escalation_needed(args["task_id"])
            print(json.dumps(result, indent=2))

        elif command == "escalate":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "reason" not in args:
                print("Error: --reason required")
                print(f"Valid reasons: {list(TRIGGER_CONFIG.keys())}")
                sys.exit(1)

            result = escalate(
                task_id=args["task_id"],
                reason=args["reason"],
                notes=args.get("notes"),
            )
            print(json.dumps(result, indent=2))

        elif command == "list":
            tier = int(args["tier"]) if "tier" in args else None
            status = args.get("status")
            project_id = args.get("project_id")

            escalations = list_escalations(
                tier=tier, status=status, project_id=project_id
            )

            result = {
                "success": True,
                "count": len(escalations),
                "filters": {"tier": tier, "status": status, "project_id": project_id},
                "multi_project_mode": is_multi_project_mode(),
                "escalations": escalations,
            }
            print(json.dumps(result, indent=2))

        elif command == "cross":
            tier = int(args["tier"]) if "tier" in args else None
            status = args.get("status")

            result = list_cross_project_escalations(tier=tier, status=status)
            result["success"] = True
            result["filters"] = {"tier": tier, "status": status}
            print(json.dumps(result, indent=2))

        elif command == "context":
            result = get_project_context()
            result["success"] = True
            print(json.dumps(result, indent=2))

        elif command == "resolve":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "resolution" not in args:
                print("Error: --resolution required")
                sys.exit(1)

            result = resolve_escalation(
                task_id=args["task_id"],
                resolution=args["resolution"],
                resolved_by=args.get("resolved_by"),
            )
            print(json.dumps(result, indent=2))

        elif command == "history":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = get_escalation_history(args["task_id"])
            print(json.dumps(result, indent=2))

        elif command == "archive":
            if "task_id" in args:
                # Archive a specific escalation
                result = archive_escalation(args["task_id"], reason="manual")
            else:
                # Archive all stale escalations
                stale_hours = int(args.get("stale_hours", 48))
                dry_run = args.get("dry_run", False)
                result = archive_stale_escalations(
                    stale_hours=stale_hours, dry_run=dry_run
                )
            print(json.dumps(result, indent=2))

        elif command == "archived":
            status = args.get("status")
            archived = list_archived_escalations(status=status)
            result = {
                "success": True,
                "count": len(archived),
                "status_filter": status,
                "archived": archived,
            }
            print(json.dumps(result, indent=2))

        elif command == "notify":
            # Test external notification dispatch for an escalation
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            task_id = args["task_id"]
            dry_run = args.get("dry_run", False)

            # Load the escalation record
            record = load_escalation(task_id)
            if not record:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": f"No escalation found for task {task_id}",
                        }
                    )
                )
                sys.exit(1)

            record_dict = asdict(record)

            if dry_run:
                # Show what would be sent without actually sending
                services = get_tier_notification_services(record.current_tier)
                alert = _build_escalation_alert(record_dict)
                result = {
                    "success": True,
                    "dry_run": True,
                    "task_id": task_id,
                    "current_tier": record.current_tier,
                    "services_to_notify": services,
                    "alert_payload": alert,
                }
                print(json.dumps(result, indent=2))
            else:
                # Actually dispatch notifications
                dispatch_results = dispatch_escalation_notification(record_dict)
                result = {
                    "success": True,
                    "task_id": task_id,
                    "current_tier": record.current_tier,
                    "dispatch_results": dispatch_results,
                }
                print(json.dumps(result, indent=2))

        elif command == "routing":
            # Show notification routing configuration
            config = load_forge_config()
            external_services = config.get("externalServices", {})
            escalation_routing = external_services.get("escalationRouting", {})

            routing_info = {
                "success": True,
                "source": "gatesmith.json" if escalation_routing else "defaults",
                "routing": {},
            }

            for tier in [1, 2, 3, 4]:
                services = get_tier_notification_services(tier)
                tier_config = TIER_CONFIG.get(tier, {})
                routing_info["routing"][f"tier_{tier}"] = {
                    "name": tier_config.get("name", f"Tier {tier}"),
                    "services": services,
                    "description": "Internal only"
                    if not services
                    else f"External: {', '.join(services)}",
                }

            print(json.dumps(routing_info, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
