#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
G6 Economics — Budget Tracking & Resource Allocation.

Manages budget configuration and spending tracking for company operations.
Integrates with efficiency_tracker.py for cost data and org.json for configuration.

Budget Schema in org.json:
    economics.budget = {
        "monthly_token_budget": 500000,     # Monthly limit in tokens
        "alert_threshold_percent": 80,       # Warning threshold %
        "currency": "USD",                   # Display currency
        "pricing_model": "claude-sonnet-4",  # Model for cost calculation
        "department_allocations": {          # Budget % per department
            "engineering": 60,
            "product": 20,
            "marketing": 15,
            "executive": 5
        },
        "goal_allocations": {                # Budget % per strategic goal
            "G1": 30,
            "G3": 25,
            "G5": 25,
            "G6": 20
        },
        "spending": {                        # Actual spending tracking
            "current_period": "2026-02",
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "by_department": {},
            "by_goal": {},
            "history": []
        }
    }

Usage:
    # Initialize budget configuration
    python budget_tracker.py init --monthly-budget 500000 --alert-threshold 80

    # Set department allocation
    python budget_tracker.py set-dept --department engineering --percent 60

    # Set goal allocation
    python budget_tracker.py set-goal --goal G1 --percent 30

    # Record spending
    python budget_tracker.py spend --tokens 1000 --department engineering --goal G1

    # Get current status
    python budget_tracker.py status

    # Get department breakdown
    python budget_tracker.py departments

    # Get goal breakdown
    python budget_tracker.py goals

    # Reset monthly spending (new period)
    python budget_tracker.py reset-period

    # Show help
    python budget_tracker.py help
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ORG_FILE = "org.json"

# Token pricing per 1M tokens (USD) - Claude model family pricing
# Sonnet 5's introductory $2/$10 rate was made permanent on 2026-08-11; Anthropic
# cancelled the increase to $3/$15 that had been planned for 2026-09-01. Do not
# reintroduce a scheduled bump — see
# https://www.anthropic.com/news/claude-sonnet-5 (checked 2026-09-05).
TOKEN_PRICING = {
    # Legacy family-level keys (backward compat for existing callers)
    "claude-opus-4": {"input": 5.00, "output": 25.00},  # Opus 4.x current rate
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4": {"input": 1.00, "output": 5.00},  # Haiku 4.5 rate
    # Specific model IDs used in gatesmith.json
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    # Specific model IDs used in model_profiles.py
    "claude-opus-4-5-20251101": {"input": 15.00, "output": 75.00},  # Pre-4.8 Opus rate
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    # Current-generation models
    "claude-sonnet-5": {
        "input": 2.00,
        "output": 10.00,
    },  # Permanent rate — see TOKEN_PRICING comment above
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-fable-5": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    # Opus 5.5 (2026-09-22): ~20% below Opus 5 ($5/$25) per launch coverage.
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    # Same base rate as claude-fable-5. Its cache-hit reads are billed at
    # $0.25/MTok (0.025x input) vs. the 0.1x multiplier every other tier uses —
    # not modeled here, since neither tracker's cost calc has a cache-read/
    # cache-creation split to apply it to (flat 50/50 input/output estimate).
    # Not referenced by any gatesmith.json profile yet — see
    # docs/claude-fable-5-1-model-tier-assessment.md.
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    # The 5.5 line Gatesmith's profiles use (2026-10-31): Sonnet 5.5 keeps Sonnet
    # 5's rate; Haiku 5.5 is $0.10/$0.50 up to 100K-token prompts.
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "default": {"input": 3.00, "output": 15.00},
}

# Default budget configuration
DEFAULT_BUDGET_CONFIG = {
    "monthly_token_budget": 500000,
    "alert_threshold_percent": 80,
    "currency": "USD",
    "pricing_model": "claude-sonnet-5-5",
    "department_allocations": {},
    "goal_allocations": {},
    "spending": {
        "current_period": None,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "by_department": {},
        "by_goal": {},
        "history": [],
    },
}

# -----------------------------------------------------------------------------
# Webhook Configuration
# -----------------------------------------------------------------------------

# Path to webhook configuration file
WEBHOOK_CONFIG_FILE = "webhook_config.json"

# Webhook rate limiting file
WEBHOOK_RATE_LIMIT_FILE = "budget_webhook_rate_limits.json"

# Rate limit: max 1 webhook per threshold per hour (in seconds)
WEBHOOK_RATE_LIMIT_SECONDS = 3600

# Default webhook events
WEBHOOK_EVENT_TYPES = ["threshold_warning", "budget_exceeded", "period_reset"]


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_org_path() -> Path:
    """Get the org.json file path."""
    return get_company_dir() / ORG_FILE


def ensure_company_dir() -> Path:
    """Ensure company directory exists."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)
    return company_dir


def get_webhook_config_path() -> Path:
    """Get the webhook configuration file path."""
    return get_company_dir() / WEBHOOK_CONFIG_FILE


def get_webhook_rate_limit_path() -> Path:
    """Get the webhook rate limit file path."""
    return get_company_dir() / WEBHOOK_RATE_LIMIT_FILE


# -----------------------------------------------------------------------------
# Webhook Functions
# -----------------------------------------------------------------------------


def load_webhook_config() -> dict:
    """
    Load webhook configuration from webhook_config.json.

    Expected format:
    {
        "budget_webhooks": [
            {
                "url": "https://example.com/webhook",
                "secret": "your-hmac-secret",
                "enabled": true,
                "events": ["threshold_warning", "budget_exceeded", "period_reset"]
            }
        ]
    }
    """
    path = get_webhook_config_path()

    if not path.exists():
        return {"budget_webhooks": []}

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"budget_webhooks": []}


def load_webhook_rate_limits() -> dict:
    """Load webhook rate limit state."""
    path = get_webhook_rate_limit_path()

    if not path.exists():
        return {"last_sent": {}}

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_sent": {}}


def save_webhook_rate_limits(rate_limits: dict):
    """Save webhook rate limit state."""
    import os
    import tempfile

    ensure_company_dir()
    path = get_webhook_rate_limit_path()

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix="webhook_rate_", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rate_limits, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _can_send_webhook(event_type: str, threshold_key: str) -> bool:
    """
    Check if a webhook can be sent based on rate limiting.

    Rate limit: max 1 webhook per threshold per hour.

    Args:
        event_type: The webhook event type
        threshold_key: A unique key for the threshold (e.g., "overall_80" or "dept_engineering_100")

    Returns:
        True if the webhook can be sent, False if rate limited.
    """
    rate_limits = load_webhook_rate_limits()
    last_sent = rate_limits.get("last_sent", {})

    key = f"{event_type}:{threshold_key}"
    last_time_str = last_sent.get(key)

    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            elapsed = (now - last_time).total_seconds()

            if elapsed < WEBHOOK_RATE_LIMIT_SECONDS:
                return False
        except (ValueError, TypeError):
            pass  # Invalid timestamp, allow sending

    return True


def _record_webhook_sent(event_type: str, threshold_key: str):
    """Record that a webhook was sent for rate limiting."""
    rate_limits = load_webhook_rate_limits()

    if "last_sent" not in rate_limits:
        rate_limits["last_sent"] = {}

    key = f"{event_type}:{threshold_key}"
    rate_limits["last_sent"][key] = datetime.now(timezone.utc).isoformat()

    # Clean up old entries (older than 24 hours)
    now = datetime.now(timezone.utc)
    cleaned = {}
    for k, v in rate_limits["last_sent"].items():
        try:
            last_time = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if (now - last_time).total_seconds() < 86400:  # 24 hours
                cleaned[k] = v
        except (ValueError, TypeError):
            pass  # Skip invalid entries

    rate_limits["last_sent"] = cleaned
    save_webhook_rate_limits(rate_limits)


def _sign_payload(payload: dict, secret: str) -> str:
    """
    Sign a webhook payload using HMAC-SHA256.

    Args:
        payload: The JSON payload to sign
        secret: The HMAC secret key

    Returns:
        The signature in "sha256=<hex>" format
    """
    import hashlib
    import hmac as hmac_module

    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = hmac_module.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()

    return f"sha256={signature}"


def _send_webhook_request(url: str, payload: dict, signature: str) -> dict:
    """
    Send a webhook HTTP POST request.

    Args:
        url: The webhook URL
        payload: The JSON payload
        signature: The HMAC signature

    Returns:
        Dict with success status and details
    """
    import urllib.error
    import urllib.request

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Forge-Signature": signature,
                "User-Agent": "forge-budget-tracker/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            return {
                "success": True,
                "status_code": response.status,
                "url": url,
            }

    except urllib.error.HTTPError as e:
        return {
            "success": False,
            "status_code": e.code,
            "url": url,
            "error": str(e),
        }
    except urllib.error.URLError as e:
        return {
            "success": False,
            "status_code": None,
            "url": url,
            "error": str(e.reason),
        }
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "url": url,
            "error": str(e),
        }


def dispatch_budget_event(event_type: str, data: dict) -> list[dict]:
    """
    Dispatch a budget event to configured webhook endpoints.

    Event types:
    - "threshold_warning": Sent when utilization crosses alert_threshold
    - "budget_exceeded": Sent when utilization >= 100%
    - "period_reset": Sent when budget period is reset

    Args:
        event_type: One of WEBHOOK_EVENT_TYPES
        data: Event-specific data (budget pool, utilization, etc.)

    Returns:
        List of dispatch results for each webhook endpoint

    Example payload:
        {
            "event": "threshold_warning",
            "timestamp": "2026-02-22T10:30:00+00:00",
            "forge_signature": "sha256=...",
            "budget": {
                "pool": "overall",
                "utilization": 0.85,
                "remaining": 150.00,
                "threshold": 0.80
            }
        }
    """
    if event_type not in WEBHOOK_EVENT_TYPES:
        return [{"success": False, "error": f"Invalid event type: {event_type}"}]

    # Load webhook configuration
    config = load_webhook_config()
    webhooks = config.get("budget_webhooks", [])

    if not webhooks:
        return []

    # Build the base payload
    timestamp = datetime.now(timezone.utc).isoformat()

    results: list[dict] = []

    for webhook in webhooks:
        # Skip disabled webhooks
        if not webhook.get("enabled", True):
            continue

        # Check if this webhook subscribes to this event
        webhook_events = webhook.get("events", WEBHOOK_EVENT_TYPES)
        if event_type not in webhook_events:
            continue

        url = webhook.get("url")
        secret = webhook.get("secret", "")

        if not url:
            results.append({"success": False, "error": "Missing webhook URL"})
            continue

        # Build threshold key for rate limiting
        pool = data.get("pool", "unknown")
        threshold = data.get("threshold", 0)
        threshold_key = f"{pool}_{int(threshold * 100)}"

        # Check rate limit
        if not _can_send_webhook(event_type, threshold_key):
            results.append(
                {
                    "success": False,
                    "url": url,
                    "error": "Rate limited",
                    "rate_limited": True,
                }
            )
            continue

        # Build payload
        payload = {
            "event": event_type,
            "timestamp": timestamp,
            "budget": data,
        }

        # Sign payload
        signature = _sign_payload(payload, secret) if secret else "sha256=unsigned"
        payload["forge_signature"] = signature

        # Send webhook
        result = _send_webhook_request(url, payload, signature)

        # Record successful send for rate limiting
        if result.get("success"):
            _record_webhook_sent(event_type, threshold_key)

        results.append(result)

    return results


def _check_and_dispatch_threshold_events(
    utilization: float,
    alert_threshold: float,
    monthly_budget: int,
    total_tokens: int,
    pool: str = "overall",
    previous_utilization: float | None = None,
) -> list[dict]:
    """
    Check if threshold events should be dispatched and dispatch them.

    Only dispatches if:
    1. Utilization has crossed a threshold (not just any high utilization)
    2. Rate limits allow

    Args:
        utilization: Current utilization percentage (0-100)
        alert_threshold: The alert threshold percentage
        monthly_budget: Monthly token budget
        total_tokens: Total tokens spent
        pool: Budget pool name (e.g., "overall", "engineering")
        previous_utilization: Previous utilization percentage (optional)

    Returns:
        List of dispatch results
    """
    results: list[dict] = []

    # Calculate remaining
    remaining = max(0, monthly_budget - total_tokens)

    # Check for budget exceeded (100%)
    if utilization >= 100:
        results.extend(
            dispatch_budget_event(
                "budget_exceeded",
                {
                    "pool": pool,
                    "utilization": round(utilization / 100, 4),  # Convert to 0-1 scale
                    "remaining": 0.0,
                    "threshold": 1.0,
                    "total_tokens": total_tokens,
                    "monthly_budget": monthly_budget,
                },
            )
        )
    # Check for threshold warning
    elif utilization >= alert_threshold:
        # Only dispatch if we crossed the threshold (if we know previous state)
        should_dispatch = True
        if previous_utilization is not None:
            should_dispatch = previous_utilization < alert_threshold

        if should_dispatch:
            results.extend(
                dispatch_budget_event(
                    "threshold_warning",
                    {
                        "pool": pool,
                        "utilization": round(
                            utilization / 100, 4
                        ),  # Convert to 0-1 scale
                        "remaining": round(remaining * 0.000009, 2),  # Convert to USD
                        "threshold": round(alert_threshold / 100, 2),
                        "total_tokens": total_tokens,
                        "monthly_budget": monthly_budget,
                    },
                )
            )

    return results


# -----------------------------------------------------------------------------
# Data Access
# -----------------------------------------------------------------------------


def load_org() -> dict:
    """Load organization data from org.json."""
    path = get_org_path()

    if not path.exists():
        return {
            "company": {"name": "Unknown"},
            "employees": [],
            "economics": {},
        }

    try:
        with open(path, encoding="utf-8") as f:
            org = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "company": {"name": "Unknown"},
            "employees": [],
            "economics": {},
        }
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    # Import the real module locally rather than the module-global
    # `company_resolver` so a test that mocks the global still normalizes.
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, path.parent)


def save_org(org: dict):
    """Save organization data to org.json.

    Safety: Refuses to save if it would wipe existing employees.
    """
    import os
    import tempfile

    ensure_company_dir()
    path = get_org_path()

    # Safety check: Don't wipe employees if file already has them
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            if len(existing_employees) > 0 and len(new_employees) == 0:
                print(
                    f"[SAFETY] Blocked save_org: Would wipe {len(existing_employees)} employees.",
                    file=sys.stderr,
                )
                return
        except (json.JSONDecodeError, OSError):
            if len(org.get("employees", [])) == 0:
                print(
                    "[SAFETY] Blocked save_org: Cannot read existing file and new data has no employees.",
                    file=sys.stderr,
                )
                return

    # Atomic write: write to temp file, then os.replace (prevents truncation race)
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix="org_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_budget_config() -> dict:
    """Get budget configuration from org.json, with defaults."""
    org = load_org()
    economics = org.get("economics", {})
    budget = economics.get("budget", {})

    # Merge with defaults
    config = DEFAULT_BUDGET_CONFIG.copy()
    for key, value in budget.items():
        if key in config:
            if isinstance(config[key], dict) and isinstance(value, dict):
                config[key] = {**config[key], **value}
            else:
                config[key] = value
        else:
            config[key] = value

    return config


def save_budget_config(budget_config: dict):
    """Save budget configuration to org.json."""
    org = load_org()

    if "economics" not in org:
        org["economics"] = {}

    org["economics"]["budget"] = budget_config
    save_org(org)


# -----------------------------------------------------------------------------
# Budget Management Functions
# -----------------------------------------------------------------------------


def initialize_budget(
    monthly_budget: int = 500000,
    alert_threshold: int = 80,
    pricing_model: str = "claude-sonnet-5-5",
) -> dict:
    """
    Initialize budget configuration in org.json.

    Args:
        monthly_budget: Monthly token budget limit
        alert_threshold: Percentage threshold for alerts (0-100)
        pricing_model: Claude model for cost calculation

    Returns:
        Dict with initialization result
    """
    now = datetime.now(timezone.utc)
    current_period = now.strftime("%Y-%m")

    budget_config = {
        "monthly_token_budget": monthly_budget,
        "alert_threshold_percent": alert_threshold,
        "currency": "USD",
        "pricing_model": pricing_model,
        "department_allocations": {},
        "goal_allocations": {},
        "spending": {
            "current_period": current_period,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "by_department": {},
            "by_goal": {},
            "history": [],
        },
        "created_at": now.isoformat(),
        "last_updated": now.isoformat(),
    }

    save_budget_config(budget_config)

    return {
        "success": True,
        "action": "initialize",
        "monthly_budget": monthly_budget,
        "alert_threshold": alert_threshold,
        "pricing_model": pricing_model,
        "period": current_period,
    }


def set_department_allocation(department: str, percent: float) -> dict:
    """
    Set budget allocation percentage for a department.

    Args:
        department: Department name (e.g., "engineering")
        percent: Allocation percentage (0-100)

    Returns:
        Dict with allocation result
    """
    if percent < 0 or percent > 100:
        return {"success": False, "error": "Percent must be between 0 and 100"}

    config = get_budget_config()
    config["department_allocations"][department] = percent
    config["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Validate total doesn't exceed 100%
    total = sum(config["department_allocations"].values())
    if total > 100:
        return {
            "success": False,
            "error": f"Total department allocation would be {total}%, exceeds 100%",
            "current_allocations": config["department_allocations"],
        }

    save_budget_config(config)

    return {
        "success": True,
        "action": "set_department_allocation",
        "department": department,
        "percent": percent,
        "total_allocated": total,
        "remaining": 100 - total,
    }


def set_goal_allocation(goal_id: str, percent: float) -> dict:
    """
    Set budget allocation percentage for a strategic goal.

    Args:
        goal_id: Goal identifier (e.g., "G1")
        percent: Allocation percentage (0-100)

    Returns:
        Dict with allocation result
    """
    if percent < 0 or percent > 100:
        return {"success": False, "error": "Percent must be between 0 and 100"}

    config = get_budget_config()
    config["goal_allocations"][goal_id] = percent
    config["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Validate total doesn't exceed 100%
    total = sum(config["goal_allocations"].values())
    if total > 100:
        return {
            "success": False,
            "error": f"Total goal allocation would be {total}%, exceeds 100%",
            "current_allocations": config["goal_allocations"],
        }

    save_budget_config(config)

    return {
        "success": True,
        "action": "set_goal_allocation",
        "goal": goal_id,
        "percent": percent,
        "total_allocated": total,
        "remaining": 100 - total,
    }


def record_spending(
    tokens: int,
    department: str | None = None,
    goal_id: str | None = None,
    employee_id: str | None = None,
    task_id: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Record token spending against budget.

    Dispatches webhook events when utilization crosses thresholds:
    - threshold_warning: When utilization crosses alert_threshold_percent
    - budget_exceeded: When utilization reaches 100%

    Args:
        tokens: Number of tokens spent
        department: Department to charge (optional)
        goal_id: Strategic goal to charge (optional)
        employee_id: Employee who spent (optional)
        task_id: Associated task (optional)
        model: Model used for pricing (optional)

    Returns:
        Dict with spending record result including webhook_dispatched field
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    monthly_budget = config.get("monthly_token_budget", 500000)
    alert_threshold = config.get("alert_threshold_percent", 80)

    # Calculate previous utilization for threshold crossing detection
    previous_tokens = spending.get("total_tokens", 0)
    previous_utilization = (
        (previous_tokens / monthly_budget * 100) if monthly_budget > 0 else 0
    )

    # Check if we need to roll over to new period
    now = datetime.now(timezone.utc)
    current_period = now.strftime("%Y-%m")

    if spending.get("current_period") != current_period:
        # Archive old period and start new
        if spending.get("current_period"):
            history = spending.get("history", [])
            history.append(
                {
                    "period": spending.get("current_period"),
                    "total_tokens": spending.get("total_tokens", 0),
                    "total_cost_usd": spending.get("total_cost_usd", 0.0),
                    "by_department": spending.get("by_department", {}),
                    "by_goal": spending.get("by_goal", {}),
                }
            )
            spending["history"] = history[-12:]  # Keep 12 months

        # Reset for new period
        spending["current_period"] = current_period
        spending["total_tokens"] = 0
        spending["total_cost_usd"] = 0.0
        spending["by_department"] = {}
        spending["by_goal"] = {}
        # Reset previous utilization since period reset
        previous_utilization = 0

    # Calculate cost
    pricing_model = model or config.get("pricing_model", "default")
    if pricing_model not in TOKEN_PRICING:
        print(
            f"[BUDGET] Unpriced model '{pricing_model}' — falling back to default "
            "rate ($3.00/$15.00 per 1M tokens). Spend for this model is likely "
            "mispriced; add it to TOKEN_PRICING in budget_tracker.py.",
            file=sys.stderr,
        )
    pricing = TOKEN_PRICING.get(pricing_model, TOKEN_PRICING["default"])
    # Assume 50/50 split input/output for simplicity
    cost_per_token = (pricing["input"] + pricing["output"]) / 2 / 1_000_000
    cost_usd = tokens * cost_per_token

    # Update totals
    spending["total_tokens"] = spending.get("total_tokens", 0) + tokens
    spending["total_cost_usd"] = spending.get("total_cost_usd", 0.0) + cost_usd

    # Update department spending
    if department:
        by_dept = spending.get("by_department", {})
        if department not in by_dept:
            by_dept[department] = {"tokens": 0, "cost_usd": 0.0}
        by_dept[department]["tokens"] += tokens
        by_dept[department]["cost_usd"] += cost_usd
        spending["by_department"] = by_dept

    # Update goal spending
    if goal_id:
        by_goal = spending.get("by_goal", {})
        if goal_id not in by_goal:
            by_goal[goal_id] = {"tokens": 0, "cost_usd": 0.0}
        by_goal[goal_id]["tokens"] += tokens
        by_goal[goal_id]["cost_usd"] += cost_usd
        spending["by_goal"] = by_goal

    config["spending"] = spending
    config["last_updated"] = now.isoformat()
    save_budget_config(config)

    # Calculate new utilization
    utilization = (
        (spending["total_tokens"] / monthly_budget * 100) if monthly_budget > 0 else 0
    )

    # Dispatch webhook events if threshold crossed
    webhook_results = _check_and_dispatch_threshold_events(
        utilization=utilization,
        alert_threshold=alert_threshold,
        monthly_budget=monthly_budget,
        total_tokens=spending["total_tokens"],
        pool="overall",
        previous_utilization=previous_utilization,
    )

    result = {
        "success": True,
        "action": "record_spending",
        "tokens": tokens,
        "cost_usd": round(cost_usd, 4),
        "department": department,
        "goal": goal_id,
        "total_tokens": spending["total_tokens"],
        "total_cost_usd": round(spending["total_cost_usd"], 4),
        "utilization_percent": round(utilization, 1),
        "budget_remaining": max(0, monthly_budget - spending["total_tokens"]),
    }

    # Include webhook dispatch info if any were sent
    if webhook_results:
        result["webhook_dispatched"] = len(
            [r for r in webhook_results if r.get("success")]
        )
        result["webhook_results"] = webhook_results

    return result


def get_budget_status() -> dict:
    """
    Get comprehensive budget status.

    Returns:
        Dict with current budget status including:
        - Budget limits and thresholds
        - Current spending totals
        - Utilization percentages
        - Department and goal breakdowns
        - Alerts and projections
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    now = datetime.now(timezone.utc)

    monthly_budget = config.get("monthly_token_budget", 500000)
    alert_threshold = config.get("alert_threshold_percent", 80)
    total_tokens = spending.get("total_tokens", 0)
    total_cost = spending.get("total_cost_usd", 0.0)

    # Calculate utilization
    utilization = (total_tokens / monthly_budget * 100) if monthly_budget > 0 else 0

    # Determine status
    if utilization >= 100:
        status = "over_budget"
    elif utilization >= alert_threshold:
        status = "at_risk"
    elif utilization >= 50:
        status = "on_track"
    else:
        status = "under_utilized"

    # Calculate projections
    current_period = spending.get("current_period", now.strftime("%Y-%m"))
    day_of_month = now.day
    days_in_month = 30  # Approximate

    if day_of_month > 0:
        daily_rate = total_tokens / day_of_month
        projected_total = daily_rate * days_in_month
    else:
        daily_rate = 0
        projected_total = 0

    # Days until exhausted
    days_until_exhausted = None
    if daily_rate > 0 and total_tokens < monthly_budget:
        days_until_exhausted = round((monthly_budget - total_tokens) / daily_rate)

    # Check department utilization vs allocation
    dept_allocations = config.get("department_allocations", {})
    dept_spending = spending.get("by_department", {})
    dept_status = {}

    for dept, allocation in dept_allocations.items():
        allocated_tokens = monthly_budget * allocation / 100
        spent = dept_spending.get(dept, {}).get("tokens", 0)
        dept_utilization = (
            (spent / allocated_tokens * 100) if allocated_tokens > 0 else 0
        )
        dept_status[dept] = {
            "allocated_percent": allocation,
            "allocated_tokens": allocated_tokens,
            "spent_tokens": spent,
            "utilization_percent": round(dept_utilization, 1),
            "remaining": max(0, allocated_tokens - spent),
        }

    # Check goal utilization vs allocation
    goal_allocations = config.get("goal_allocations", {})
    goal_spending = spending.get("by_goal", {})
    goal_status = {}

    for goal, allocation in goal_allocations.items():
        allocated_tokens = monthly_budget * allocation / 100
        spent = goal_spending.get(goal, {}).get("tokens", 0)
        goal_utilization = (
            (spent / allocated_tokens * 100) if allocated_tokens > 0 else 0
        )
        goal_status[goal] = {
            "allocated_percent": allocation,
            "allocated_tokens": allocated_tokens,
            "spent_tokens": spent,
            "utilization_percent": round(goal_utilization, 1),
            "remaining": max(0, allocated_tokens - spent),
        }

    # Generate alerts
    alerts = []
    if status == "over_budget":
        alerts.append(
            {
                "level": "critical",
                "message": f"Budget exceeded: {round(utilization, 1)}% utilized",
            }
        )
    elif status == "at_risk":
        alerts.append(
            {
                "level": "warning",
                "message": f"Budget at risk: {round(utilization, 1)}% utilized (threshold: {alert_threshold}%)",
            }
        )

    # Check department overages
    for dept, info in dept_status.items():
        if info["utilization_percent"] >= 100:
            alerts.append(
                {
                    "level": "warning",
                    "message": f"Department '{dept}' over allocation: {info['utilization_percent']}%",
                }
            )

    # Get webhook status
    webhook_rate_limits = load_webhook_rate_limits()
    last_sent = webhook_rate_limits.get("last_sent", {})
    webhook_last_sent = None

    # Find the most recent webhook send time
    for key, timestamp in last_sent.items():
        if webhook_last_sent is None:
            webhook_last_sent = timestamp
        else:
            try:
                if timestamp > webhook_last_sent:
                    webhook_last_sent = timestamp
            except TypeError:
                pass

    return {
        "success": True,
        "period": current_period,
        "budget": {
            "monthly_limit": monthly_budget,
            "alert_threshold": alert_threshold,
            "currency": config.get("currency", "USD"),
            "pricing_model": config.get("pricing_model", "claude-sonnet-4"),
        },
        "spending": {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "utilization_percent": round(utilization, 1),
            "remaining_tokens": max(0, monthly_budget - total_tokens),
            "status": status,
        },
        "projection": {
            "daily_rate": round(daily_rate, 0),
            "projected_month_total": round(projected_total, 0),
            "projected_utilization_percent": round(
                (projected_total / monthly_budget * 100) if monthly_budget > 0 else 0, 1
            ),
            "days_until_exhausted": days_until_exhausted,
        },
        "by_department": dept_status,
        "by_goal": goal_status,
        "alerts": alerts,
        "last_updated": config.get("last_updated"),
        "webhook_last_sent": webhook_last_sent,
    }


def get_department_breakdown() -> dict:
    """
    Get detailed department budget breakdown.

    Returns:
        Dict with department allocations and spending details
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    monthly_budget = config.get("monthly_token_budget", 500000)

    dept_allocations = config.get("department_allocations", {})
    dept_spending = spending.get("by_department", {})

    departments = {}

    # Add allocated departments
    for dept, allocation in dept_allocations.items():
        allocated = monthly_budget * allocation / 100
        spent = dept_spending.get(dept, {}).get("tokens", 0)
        cost = dept_spending.get(dept, {}).get("cost_usd", 0.0)
        utilization = (spent / allocated * 100) if allocated > 0 else 0

        departments[dept] = {
            "allocated_percent": allocation,
            "allocated_tokens": round(allocated),
            "spent_tokens": spent,
            "spent_cost_usd": round(cost, 4),
            "utilization_percent": round(utilization, 1),
            "remaining_tokens": max(0, round(allocated - spent)),
            "status": "over"
            if utilization >= 100
            else ("at_risk" if utilization >= 80 else "ok"),
        }

    # Add unallocated spending departments
    for dept, data in dept_spending.items():
        if dept not in departments:
            departments[dept] = {
                "allocated_percent": 0,
                "allocated_tokens": 0,
                "spent_tokens": data.get("tokens", 0),
                "spent_cost_usd": round(data.get("cost_usd", 0.0), 4),
                "utilization_percent": 0,  # No allocation
                "remaining_tokens": 0,
                "status": "unallocated",
            }

    return {
        "success": True,
        "monthly_budget": monthly_budget,
        "departments": departments,
        "total_allocated_percent": sum(dept_allocations.values()),
        "unallocated_percent": 100 - sum(dept_allocations.values()),
    }


def get_goal_breakdown() -> dict:
    """
    Get detailed strategic goal budget breakdown.

    Returns:
        Dict with goal allocations and spending details
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    monthly_budget = config.get("monthly_token_budget", 500000)

    goal_allocations = config.get("goal_allocations", {})
    goal_spending = spending.get("by_goal", {})

    goals = {}

    # Add allocated goals
    for goal, allocation in goal_allocations.items():
        allocated = monthly_budget * allocation / 100
        spent = goal_spending.get(goal, {}).get("tokens", 0)
        cost = goal_spending.get(goal, {}).get("cost_usd", 0.0)
        utilization = (spent / allocated * 100) if allocated > 0 else 0

        goals[goal] = {
            "allocated_percent": allocation,
            "allocated_tokens": round(allocated),
            "spent_tokens": spent,
            "spent_cost_usd": round(cost, 4),
            "utilization_percent": round(utilization, 1),
            "remaining_tokens": max(0, round(allocated - spent)),
            "status": "over"
            if utilization >= 100
            else ("at_risk" if utilization >= 80 else "ok"),
        }

    # Add unallocated spending goals
    for goal, data in goal_spending.items():
        if goal not in goals:
            goals[goal] = {
                "allocated_percent": 0,
                "allocated_tokens": 0,
                "spent_tokens": data.get("tokens", 0),
                "spent_cost_usd": round(data.get("cost_usd", 0.0), 4),
                "utilization_percent": 0,
                "remaining_tokens": 0,
                "status": "unallocated",
            }

    return {
        "success": True,
        "monthly_budget": monthly_budget,
        "goals": goals,
        "total_allocated_percent": sum(goal_allocations.values()),
        "unallocated_percent": 100 - sum(goal_allocations.values()),
    }


def reset_period(archive: bool = True) -> dict:
    """
    Reset spending for new budget period.

    Dispatches a period_reset webhook event.

    Args:
        archive: Whether to archive current period to history

    Returns:
        Dict with reset result including webhook_dispatched field
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    monthly_budget = config.get("monthly_token_budget", 500000)
    now = datetime.now(timezone.utc)
    new_period = now.strftime("%Y-%m")

    # Capture old period data for webhook
    old_period = spending.get("current_period")
    old_tokens = spending.get("total_tokens", 0)
    old_cost = spending.get("total_cost_usd", 0.0)

    # Archive if requested and there's data
    if archive and spending.get("current_period"):
        history = spending.get("history", [])
        history.append(
            {
                "period": spending.get("current_period"),
                "total_tokens": old_tokens,
                "total_cost_usd": old_cost,
                "by_department": spending.get("by_department", {}),
                "by_goal": spending.get("by_goal", {}),
            }
        )
        spending["history"] = history[-12:]  # Keep 12 months

    # Reset spending
    spending["current_period"] = new_period
    spending["total_tokens"] = 0
    spending["total_cost_usd"] = 0.0
    spending["by_department"] = {}
    spending["by_goal"] = {}

    config["spending"] = spending
    config["last_updated"] = now.isoformat()
    save_budget_config(config)

    # Dispatch period_reset webhook event
    webhook_results = dispatch_budget_event(
        "period_reset",
        {
            "pool": "overall",
            "old_period": old_period,
            "new_period": new_period,
            "archived": archive,
            "previous_total_tokens": old_tokens,
            "previous_total_cost_usd": round(old_cost, 4),
            "monthly_budget": monthly_budget,
            "utilization": 0.0,
            "remaining": monthly_budget,
            "threshold": 0.0,
        },
    )

    result = {
        "success": True,
        "action": "reset_period",
        "old_period": old_period,
        "new_period": new_period,
        "archived": archive,
    }

    # Include webhook dispatch info if any were sent
    if webhook_results:
        result["webhook_dispatched"] = len(
            [r for r in webhook_results if r.get("success")]
        )
        result["webhook_results"] = webhook_results

    return result


def get_spending_history() -> dict:
    """
    Get historical spending data.

    Returns:
        Dict with spending history by period
    """
    config = get_budget_config()
    spending = config.get("spending", {})
    monthly_budget = config.get("monthly_token_budget", 500000)

    history = spending.get("history", [])

    # Add current period
    current = {
        "period": spending.get("current_period"),
        "total_tokens": spending.get("total_tokens", 0),
        "total_cost_usd": spending.get("total_cost_usd", 0.0),
        "by_department": spending.get("by_department", {}),
        "by_goal": spending.get("by_goal", {}),
        "is_current": True,
    }

    # Calculate utilization for each period
    periods = []
    for entry in history:
        utilization = (
            (entry["total_tokens"] / monthly_budget * 100) if monthly_budget > 0 else 0
        )
        periods.append(
            {
                **entry,
                "utilization_percent": round(utilization, 1),
                "is_current": False,
            }
        )

    if current["period"]:
        current["utilization_percent"] = round(
            (current["total_tokens"] / monthly_budget * 100)
            if monthly_budget > 0
            else 0,
            1,
        )
        periods.append(current)

    return {
        "success": True,
        "monthly_budget": monthly_budget,
        "periods": periods,
        "total_periods": len(periods),
    }


# -----------------------------------------------------------------------------
# Efficiency Integration (G6 Economics)
# -----------------------------------------------------------------------------

EFFICIENCY_DATA_FILE = "state/efficiency_data.json"


def get_efficiency_data_path() -> Path:
    """Get the efficiency data file path."""
    return get_company_dir() / EFFICIENCY_DATA_FILE


def load_efficiency_data() -> dict:
    """Load efficiency data from efficiency_data.json."""
    path = get_efficiency_data_path()

    if not path.exists():
        return {
            "cost_tracking": {
                "daily": {},
                "by_task": {},
                "by_session": {},
                "by_employee": {},
                "totals": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "task_cost_usd": 0.0,
                    "session_cost_usd": 0.0,
                    "total_cost_usd": 0.0,
                },
            },
            "token_usage": {
                "daily": {},
                "totals": {
                    "executive_tokens": 0,
                    "task_tokens": 0,
                    "total_tokens": 0,
                },
            },
        }

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "cost_tracking": {
                "daily": {},
                "by_task": {},
                "by_session": {},
                "by_employee": {},
                "totals": {},
            },
            "token_usage": {"daily": {}, "totals": {}},
        }


def get_employee_department(employee_id: str) -> str | None:
    """Get the department for an employee from org.json."""
    org = load_org()
    for emp in org.get("employees", []):
        if emp.get("id") == employee_id:
            return emp.get("department")
    return None


def sync_efficiency_to_budget(
    period: str | None = None,
    reset_before_sync: bool = False,
) -> dict:
    """
    Sync efficiency cost tracking data to budget spending.

    This function reads token usage from efficiency_data.json and
    updates the budget spending in org.json, aggregating by department.

    Args:
        period: Budget period to sync to (default: current period)
        reset_before_sync: Clear existing spending before sync

    Returns:
        Dict with sync results including tokens synced and departments updated
    """
    now = datetime.now(timezone.utc)
    target_period = period or now.strftime("%Y-%m")

    # Load efficiency data
    efficiency_data = load_efficiency_data()
    cost_tracking = efficiency_data.get("cost_tracking", {})
    by_employee = cost_tracking.get("by_employee", {})

    # Load current budget config
    config = get_budget_config()
    spending = config.get("spending", {})

    # Reset if requested
    if reset_before_sync:
        spending["total_tokens"] = 0
        spending["total_cost_usd"] = 0.0
        spending["by_department"] = {}
        spending["by_goal"] = {}

    # Ensure period is set
    if spending.get("current_period") != target_period:
        # Archive old period if exists
        if spending.get("current_period"):
            history = spending.get("history", [])
            history.append(
                {
                    "period": spending.get("current_period"),
                    "total_tokens": spending.get("total_tokens", 0),
                    "total_cost_usd": spending.get("total_cost_usd", 0.0),
                    "by_department": spending.get("by_department", {}),
                    "by_goal": spending.get("by_goal", {}),
                }
            )
            spending["history"] = history[-12:]

        spending["current_period"] = target_period
        spending["total_tokens"] = 0
        spending["total_cost_usd"] = 0.0
        spending["by_department"] = {}
        spending["by_goal"] = {}

    # Aggregate by department from employee data
    department_tokens: dict[str, int] = {}
    department_cost: dict[str, float] = {}
    synced_employees: list[str] = []

    for employee_id, emp_data in by_employee.items():
        dept = get_employee_department(employee_id)
        if not dept:
            dept = "unassigned"

        emp_tokens = emp_data.get("total_tokens", 0)
        emp_cost = emp_data.get("total_cost_usd", 0.0)

        if emp_tokens > 0:
            department_tokens[dept] = department_tokens.get(dept, 0) + emp_tokens
            department_cost[dept] = department_cost.get(dept, 0.0) + emp_cost
            synced_employees.append(employee_id)

    # Update spending
    by_dept = spending.setdefault("by_department", {})
    total_synced_tokens = 0
    total_synced_cost = 0.0

    for dept, tokens in department_tokens.items():
        cost = department_cost.get(dept, 0.0)

        if dept not in by_dept:
            by_dept[dept] = {"tokens": 0, "cost_usd": 0.0}

        # Add to existing (don't overwrite)
        if not reset_before_sync:
            # Calculate delta (new tokens not yet recorded)
            current_tokens = by_dept[dept].get("tokens", 0)
            if tokens > current_tokens:
                delta_tokens = tokens - current_tokens
                delta_cost = cost - by_dept[dept].get("cost_usd", 0.0)
                by_dept[dept]["tokens"] = tokens
                by_dept[dept]["cost_usd"] = cost
                total_synced_tokens += delta_tokens
                total_synced_cost += delta_cost
        else:
            by_dept[dept]["tokens"] = tokens
            by_dept[dept]["cost_usd"] = cost
            total_synced_tokens += tokens
            total_synced_cost += cost

    # Update totals
    spending["total_tokens"] = sum(d.get("tokens", 0) for d in by_dept.values())
    spending["total_cost_usd"] = sum(d.get("cost_usd", 0.0) for d in by_dept.values())
    spending["by_department"] = by_dept

    # Save config
    config["spending"] = spending
    config["last_updated"] = now.isoformat()
    save_budget_config(config)

    # Calculate utilization
    monthly_budget = config.get("monthly_token_budget", 500000)
    utilization = (
        (spending["total_tokens"] / monthly_budget * 100) if monthly_budget > 0 else 0
    )

    return {
        "success": True,
        "action": "sync_efficiency_to_budget",
        "period": target_period,
        "synced_tokens": total_synced_tokens,
        "synced_cost_usd": round(total_synced_cost, 4),
        "total_tokens": spending["total_tokens"],
        "total_cost_usd": round(spending["total_cost_usd"], 4),
        "utilization_percent": round(utilization, 1),
        "departments_updated": list(department_tokens.keys()),
        "employees_synced": synced_employees,
        "reset_before_sync": reset_before_sync,
    }


def get_efficiency_cost_summary() -> dict:
    """
    Get a summary of costs from efficiency tracking.

    Returns:
        Dict with efficiency cost data for comparison with budget
    """
    efficiency_data = load_efficiency_data()
    cost_tracking = efficiency_data.get("cost_tracking", {})
    token_usage = efficiency_data.get("token_usage", {})

    totals = cost_tracking.get("totals", {})
    by_employee = cost_tracking.get("by_employee", {})
    by_task = cost_tracking.get("by_task", {})
    daily = cost_tracking.get("daily", {})

    # Aggregate by department
    dept_summary: dict[str, dict] = {}
    for employee_id, emp_data in by_employee.items():
        dept = get_employee_department(employee_id) or "unassigned"
        if dept not in dept_summary:
            dept_summary[dept] = {"tokens": 0, "cost_usd": 0.0, "employees": []}
        dept_summary[dept]["tokens"] += emp_data.get("total_tokens", 0)
        dept_summary[dept]["cost_usd"] += emp_data.get("total_cost_usd", 0.0)
        dept_summary[dept]["employees"].append(employee_id)

    return {
        "success": True,
        "totals": {
            "total_tokens": totals.get("total_tokens", 0),
            "total_cost_usd": round(totals.get("total_cost_usd", 0.0), 4),
            "input_tokens": totals.get("input_tokens", 0),
            "output_tokens": totals.get("output_tokens", 0),
        },
        "by_department": dept_summary,
        "task_count": len(by_task),
        "employee_count": len(by_employee),
        "daily_periods": len(daily),
        "token_usage": {
            "executive_tokens": token_usage.get("totals", {}).get(
                "executive_tokens", 0
            ),
            "task_tokens": token_usage.get("totals", {}).get("task_tokens", 0),
        },
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print help information."""
    help_text = """
G6 Economics — Budget Tracking

Commands:
    init              Initialize budget configuration
    set-dept          Set department allocation percentage
    set-goal          Set goal allocation percentage
    spend             Record token spending
    status            Get comprehensive budget status
    departments       Get department breakdown
    goals             Get goal breakdown
    history           Get spending history
    reset-period      Reset for new budget period
    sync              Sync efficiency tracking data to budget
    efficiency        Get efficiency cost summary
    help              Show this help

Examples:
    # Initialize with 500K monthly budget
    python budget_tracker.py init --monthly-budget 500000

    # Set engineering department to 60% of budget
    python budget_tracker.py set-dept --department engineering --percent 60

    # Set G1 goal to 30% of budget
    python budget_tracker.py set-goal --goal G1 --percent 30

    # Record 1000 tokens spent by engineering on G1
    python budget_tracker.py spend --tokens 1000 --department engineering --goal G1

    # Get current status
    python budget_tracker.py status

    # Sync efficiency data to budget
    python budget_tracker.py sync

    # Sync with reset (replaces existing spending)
    python budget_tracker.py sync --reset

    # Get efficiency cost summary
    python budget_tracker.py efficiency
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
        if command == "init":
            monthly_budget = 500000
            alert_threshold = 80
            pricing_model = "claude-sonnet-5-5"

            for i, arg in enumerate(args[1:], 1):
                if arg == "--monthly-budget" and i < len(args):
                    monthly_budget = int(args[i + 1])
                elif arg == "--alert-threshold" and i < len(args):
                    alert_threshold = int(args[i + 1])
                elif arg == "--pricing-model" and i < len(args):
                    pricing_model = args[i + 1]

            result = initialize_budget(monthly_budget, alert_threshold, pricing_model)

        elif command == "set-dept":
            department = None
            percent = 0.0

            for i, arg in enumerate(args[1:], 1):
                if arg == "--department" and i < len(args):
                    department = args[i + 1]
                elif arg == "--percent" and i < len(args):
                    percent = float(args[i + 1])

            if not department:
                result = {"success": False, "error": "Missing --department argument"}
            else:
                result = set_department_allocation(department, percent)

        elif command == "set-goal":
            goal = None
            percent = 0.0

            for i, arg in enumerate(args[1:], 1):
                if arg == "--goal" and i < len(args):
                    goal = args[i + 1]
                elif arg == "--percent" and i < len(args):
                    percent = float(args[i + 1])

            if not goal:
                result = {"success": False, "error": "Missing --goal argument"}
            else:
                result = set_goal_allocation(goal, percent)

        elif command == "spend":
            tokens = 0
            department = None
            goal = None
            employee = None
            task = None

            for i, arg in enumerate(args[1:], 1):
                if arg == "--tokens" and i < len(args):
                    tokens = int(args[i + 1])
                elif arg == "--department" and i < len(args):
                    department = args[i + 1]
                elif arg == "--goal" and i < len(args):
                    goal = args[i + 1]
                elif arg == "--employee" and i < len(args):
                    employee = args[i + 1]
                elif arg == "--task" and i < len(args):
                    task = args[i + 1]

            if tokens <= 0:
                result = {
                    "success": False,
                    "error": "Missing or invalid --tokens argument",
                }
            else:
                result = record_spending(tokens, department, goal, employee, task)

        elif command == "status":
            result = get_budget_status()

        elif command == "departments":
            result = get_department_breakdown()

        elif command == "goals":
            result = get_goal_breakdown()

        elif command == "history":
            result = get_spending_history()

        elif command == "reset-period":
            archive = "--no-archive" not in args
            result = reset_period(archive)

        elif command == "sync":
            reset_flag = "--reset" in args
            period = None

            for i, arg in enumerate(args[1:], 1):
                if arg == "--period" and i < len(args):
                    period = args[i + 1]

            result = sync_efficiency_to_budget(period, reset_flag)

        elif command == "efficiency":
            result = get_efficiency_cost_summary()

        else:
            result = {"success": False, "error": f"Unknown command: {command}"}

    except Exception as e:
        result = {"success": False, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
