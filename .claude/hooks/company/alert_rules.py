#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Configurable Alert Rules Engine — rule-based alerting for company health metrics.

Evaluates configurable rules against metrics to generate alerts with severity levels.
Supports custom thresholds, alert persistence, deduplication, and auto-expiry.

Default Rules:
    - health_critical: Health score < 60 (critical)
    - health_warning: Health score < 80 (warning)
    - blocked_critical: Blocked ratio > 40% (critical)
    - blocked_warning: Blocked ratio > 20% (warning)
    - stalled_warning: Stalled count > 0 (warning)
    - escalation_critical: Active escalations > 5 (critical)
    - escalation_warning: Active escalations > 2 (warning)
    - velocity_warning: Velocity drop > 50% (warning)

Storage:
    - Alerts: .company/alerts.json
    - Custom thresholds: .company/alert_config.json

Usage:
    # Evaluate rules against metrics
    python alert_rules.py evaluate --metrics '{"health_score": 65, "blocked_ratio": 25}'

    # List active alerts
    python alert_rules.py list

    # Configure a threshold
    python alert_rules.py configure --rule health_warning --threshold 75

    # Clear an alert
    python alert_rules.py clear --alert-id abc123

    # Show help
    python alert_rules.py help
"""

import json
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
from company_resolver import get_company_dir

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ALERTS_FILE = "alerts.json"
CONFIG_FILE = "alert_config.json"

# Rate limiting: same alert type max once per 15 minutes
RATE_LIMIT_MINUTES = 15

# Auto-expire alerts after 24 hours
EXPIRY_HOURS = 24

# Default rules configuration
RULES = {
    "health_critical": {
        "metric": "health_score",
        "operator": "<",
        "threshold": 60,
        "severity": "critical",
    },
    "health_warning": {
        "metric": "health_score",
        "operator": "<",
        "threshold": 80,
        "severity": "warning",
    },
    "blocked_critical": {
        "metric": "blocked_ratio",
        "operator": ">",
        "threshold": 40,
        "severity": "critical",
    },
    "blocked_warning": {
        "metric": "blocked_ratio",
        "operator": ">",
        "threshold": 20,
        "severity": "warning",
    },
    "stalled_warning": {
        "metric": "stalled_count",
        "operator": ">",
        "threshold": 0,
        "severity": "warning",
    },
    "escalation_critical": {
        "metric": "escalations_active",
        "operator": ">",
        "threshold": 5,
        "severity": "critical",
    },
    "escalation_warning": {
        "metric": "escalations_active",
        "operator": ">",
        "threshold": 2,
        "severity": "warning",
    },
    "velocity_warning": {
        "metric": "velocity_drop_percent",
        "operator": ">",
        "threshold": 50,
        "severity": "warning",
    },
    # G6 Economics: Efficiency alerts
    "efficiency_critical": {
        "metric": "efficiency_score",
        "operator": "<",
        "threshold": 0.6,
        "severity": "critical",
    },
    "efficiency_warning": {
        "metric": "efficiency_score",
        "operator": "<",
        "threshold": 0.8,
        "severity": "warning",
    },
    "efficiency_degradation_warning": {
        "metric": "efficiency_decline_percent",
        "operator": ">",
        "threshold": 15,
        "severity": "warning",
    },
    "memory_hit_rate_warning": {
        "metric": "memory_hit_rate",
        "operator": "<",
        "threshold": 0.3,
        "severity": "warning",
    },
}

# Message templates for each rule
MESSAGES = {
    "health_critical": "CRITICAL: Health score at {value}/100 (below {threshold})",
    "health_warning": "Health score dropped to {value}/100 (threshold: {threshold})",
    "blocked_critical": "CRITICAL: {value:.1f}% of tasks blocked (>{threshold}%)",
    "blocked_warning": "Blocked ratio at {value:.1f}% (threshold: {threshold}%)",
    "stalled_warning": "{value} task(s) have stalled",
    "escalation_critical": "CRITICAL: {value} active escalations",
    "escalation_warning": "{value} escalations pending resolution",
    "velocity_warning": "Velocity dropped {value:.1f}% from average",
    # G6 Economics: Efficiency messages
    "efficiency_critical": "CRITICAL: Efficiency score at {value:.2f} (below {threshold})",
    "efficiency_warning": "Efficiency score dropped to {value:.2f} (threshold: {threshold})",
    "efficiency_degradation_warning": "Efficiency declined {value:.1f}% from baseline",
    "memory_hit_rate_warning": "Memory hit rate low at {value:.1%} (threshold: {threshold:.0%})",
}


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class Alert:
    """Represents a triggered alert."""

    id: str
    rule_id: str
    severity: str
    message: str
    triggered_at: str
    metric_name: str
    metric_value: float
    threshold: float
    resolved: bool = False
    resolved_at: str | None = None


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_alerts_path(company_dir: Path | None = None) -> Path:
    """Get the alerts file path."""
    if company_dir is None:
        company_dir = get_company_dir()
    return company_dir / ALERTS_FILE


def get_config_path(company_dir: Path | None = None) -> Path:
    """Get the alert config file path."""
    if company_dir is None:
        company_dir = get_company_dir()
    return company_dir / CONFIG_FILE


def ensure_company_dir(company_dir: Path | None = None):
    """Ensure company directory exists."""
    if company_dir is None:
        company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Alert Storage Functions
# -----------------------------------------------------------------------------


def load_alerts(company_dir: Path | None = None) -> list[dict]:
    """Load alerts from file."""
    alerts_path = get_alerts_path(company_dir)

    if not alerts_path.exists():
        return []

    try:
        with open(alerts_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("alerts", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_alerts(alerts: list[dict], company_dir: Path | None = None):
    """Save alerts to file."""
    ensure_company_dir(company_dir)
    alerts_path = get_alerts_path(company_dir)

    data = {
        "alerts": alerts,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    with open(alerts_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config(company_dir: Path | None = None) -> dict:
    """Load custom alert configuration."""
    config_path = get_config_path(company_dir)

    if not config_path.exists():
        return {"thresholds": {}}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"thresholds": {}}


def save_config(config: dict, company_dir: Path | None = None):
    """Save custom alert configuration."""
    ensure_company_dir(company_dir)
    config_path = get_config_path(company_dir)

    config["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# -----------------------------------------------------------------------------
# Rule Functions
# -----------------------------------------------------------------------------


def get_rule_config(company_dir: Path | None = None) -> dict:
    """
    Get current rule configuration.

    Merges default rules with custom threshold overrides from alert_config.json.

    Args:
        company_dir: Optional company directory path.

    Returns:
        Dict of rule_id -> rule configuration with custom thresholds applied.
    """
    config = load_config(company_dir)
    custom_thresholds = config.get("thresholds", {})

    # Start with default rules
    merged = {}
    for rule_id, rule in RULES.items():
        merged[rule_id] = rule.copy()
        # Apply custom threshold if set
        if rule_id in custom_thresholds:
            merged[rule_id]["threshold"] = custom_thresholds[rule_id]
            merged[rule_id]["custom"] = True

    return merged


def configure_threshold(
    rule_id: str,
    value: float,
    company_dir: Path | None = None,
) -> dict:
    """
    Update threshold for a rule.

    Saves custom thresholds to .company/alert_config.json.

    Args:
        rule_id: The rule ID to update.
        value: The new threshold value.
        company_dir: Optional company directory path.

    Returns:
        Dict with configuration result.
    """
    if rule_id not in RULES:
        return {
            "success": False,
            "error": f"Unknown rule: {rule_id}",
            "valid_rules": list(RULES.keys()),
        }

    config = load_config(company_dir)

    if "thresholds" not in config:
        config["thresholds"] = {}

    old_value = config["thresholds"].get(rule_id, RULES[rule_id]["threshold"])
    config["thresholds"][rule_id] = value

    save_config(config, company_dir)

    return {
        "success": True,
        "rule_id": rule_id,
        "old_threshold": old_value,
        "new_threshold": value,
        "message": f"Updated {rule_id} threshold from {old_value} to {value}",
    }


# -----------------------------------------------------------------------------
# Alert Evaluation Functions
# -----------------------------------------------------------------------------


def _check_condition(value: float, operator: str, threshold: float) -> bool:
    """Check if a metric value satisfies the rule condition."""
    if operator == "<":
        return value < threshold
    elif operator == ">":
        return value > threshold
    elif operator == "<=":
        return value <= threshold
    elif operator == ">=":
        return value >= threshold
    elif operator == "==":
        return value == threshold
    elif operator == "!=":
        return value != threshold
    return False


def _format_message(rule_id: str, value: float, threshold: float) -> str:
    """Generate human-readable alert message."""
    template = MESSAGES.get(
        rule_id, "Alert: {metric_name} = {value} (threshold: {threshold})"
    )

    # Handle formatting based on whether value is integer-like
    try:
        if value == int(value):
            formatted_value = int(value)
        else:
            formatted_value = value

        return template.format(value=formatted_value, threshold=threshold)
    except (ValueError, KeyError):
        return f"Alert triggered for {rule_id}: value={value}, threshold={threshold}"


def _is_rate_limited(
    rule_id: str,
    alerts: list[dict],
    rate_limit_minutes: int = RATE_LIMIT_MINUTES,
) -> bool:
    """Check if an alert for this rule was triggered within the rate limit window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=rate_limit_minutes)
    cutoff_str = cutoff.isoformat()

    for alert in alerts:
        if alert.get("rule_id") == rule_id:
            triggered_at = alert.get("triggered_at", "")
            if triggered_at >= cutoff_str:
                return True

    return False


def evaluate_rules(
    metrics: dict,
    company_dir: Path | None = None,
) -> list[Alert]:
    """
    Evaluate all rules against provided metrics.

    Args:
        metrics: Dict of metric_name -> value to evaluate.
        company_dir: Optional company directory path.

    Returns:
        List of triggered Alert objects.
    """
    rules = get_rule_config(company_dir)
    existing_alerts = load_alerts(company_dir)
    triggered: list[Alert] = []
    now = datetime.now(timezone.utc).isoformat()

    for rule_id, rule in rules.items():
        metric_name = rule["metric"]

        # Skip if metric not provided
        if metric_name not in metrics:
            continue

        value = metrics[metric_name]
        threshold = rule["threshold"]
        operator = rule["operator"]

        # Check if condition is triggered
        if not _check_condition(value, operator, threshold):
            continue

        # Check rate limiting
        if _is_rate_limited(rule_id, existing_alerts):
            continue

        # Create alert
        alert = Alert(
            id=f"alert-{uuid.uuid4().hex[:12]}",
            rule_id=rule_id,
            severity=rule["severity"],
            message=_format_message(rule_id, value, threshold),
            triggered_at=now,
            metric_name=metric_name,
            metric_value=value,
            threshold=threshold,
        )

        triggered.append(alert)

    return triggered


# -----------------------------------------------------------------------------
# Alert Management Functions
# -----------------------------------------------------------------------------


def get_active_alerts(company_dir: Path | None = None) -> list[Alert]:
    """
    Get currently active (non-resolved, non-expired) alerts.

    Loads from .company/alerts.json and filters out:
    - Resolved alerts
    - Alerts older than 24 hours

    Args:
        company_dir: Optional company directory path.

    Returns:
        List of active Alert objects.
    """
    alerts = load_alerts(company_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)
    cutoff_str = cutoff.isoformat()

    active: list[Alert] = []

    for alert_data in alerts:
        # Skip resolved alerts
        if alert_data.get("resolved", False):
            continue

        # Skip expired alerts
        triggered_at = alert_data.get("triggered_at", "")
        if triggered_at < cutoff_str:
            continue

        # Convert to Alert object
        try:
            alert = Alert(
                id=alert_data.get("id", ""),
                rule_id=alert_data.get("rule_id", ""),
                severity=alert_data.get("severity", "warning"),
                message=alert_data.get("message", ""),
                triggered_at=triggered_at,
                metric_name=alert_data.get("metric_name", ""),
                metric_value=alert_data.get("metric_value", 0),
                threshold=alert_data.get("threshold", 0),
                resolved=False,
                resolved_at=None,
            )
            active.append(alert)
        except (TypeError, KeyError):
            continue

    return active


def save_alert(alert: dict, company_dir: Path | None = None):
    """
    Persist an alert.

    Saves to .company/alerts.json with deduplication.
    Same rule_id within 15 minutes is considered a duplicate.

    Args:
        alert: Alert dict to save.
        company_dir: Optional company directory path.
    """
    alerts = load_alerts(company_dir)

    # Check for duplicate (same rule_id within rate limit window)
    rule_id = alert.get("rule_id", "")
    if _is_rate_limited(rule_id, alerts):
        return  # Skip duplicate

    alerts.append(alert)
    save_alerts(alerts, company_dir)


def clear_alert(alert_id: str, company_dir: Path | None = None) -> dict:
    """
    Mark an alert as resolved.

    Args:
        alert_id: The alert ID to resolve.
        company_dir: Optional company directory path.

    Returns:
        Dict with clear result.
    """
    alerts = load_alerts(company_dir)
    now = datetime.now(timezone.utc).isoformat()

    found = False
    for alert in alerts:
        if alert.get("id") == alert_id:
            alert["resolved"] = True
            alert["resolved_at"] = now
            found = True
            break

    if not found:
        return {
            "success": False,
            "error": f"Alert not found: {alert_id}",
        }

    save_alerts(alerts, company_dir)

    return {
        "success": True,
        "alert_id": alert_id,
        "resolved_at": now,
        "message": f"Alert {alert_id} marked as resolved",
    }


# -----------------------------------------------------------------------------
# Main API Functions
# -----------------------------------------------------------------------------


def evaluate_and_save(
    metrics: dict,
    company_dir: Path | None = None,
) -> dict:
    """
    Evaluate rules against metrics and save triggered alerts.

    Args:
        metrics: Dict of metric_name -> value to evaluate.
        company_dir: Optional company directory path.

    Returns:
        Dict with evaluation results including triggered alerts.
    """
    triggered = evaluate_rules(metrics, company_dir)

    # Save each triggered alert
    for alert in triggered:
        save_alert(asdict(alert), company_dir)

    # Get all active alerts for summary
    active = get_active_alerts(company_dir)

    return {
        "success": True,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "metrics_provided": list(metrics.keys()),
        "triggered_count": len(triggered),
        "triggered_alerts": [asdict(a) for a in triggered],
        "active_alerts_count": len(active),
        "critical_count": sum(1 for a in active if a.severity == "critical"),
        "warning_count": sum(1 for a in active if a.severity == "warning"),
    }


def list_active_alerts(company_dir: Path | None = None) -> dict:
    """
    List all active alerts.

    Args:
        company_dir: Optional company directory path.

    Returns:
        Dict with active alerts summary.
    """
    active = get_active_alerts(company_dir)

    # Group by severity
    critical = [a for a in active if a.severity == "critical"]
    warning = [a for a in active if a.severity == "warning"]

    return {
        "success": True,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "total_active": len(active),
        "critical_count": len(critical),
        "warning_count": len(warning),
        "alerts": [asdict(a) for a in active],
        "by_severity": {
            "critical": [asdict(a) for a in critical],
            "warning": [asdict(a) for a in warning],
        },
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Configurable Alert Rules Engine — rule-based alerting for company health metrics

Commands:
    evaluate    Evaluate rules against provided metrics
    list        List active alerts
    configure   Update threshold for a rule
    clear       Mark an alert as resolved
    rules       Show current rule configuration

Evaluate options:
    --metrics JSON     JSON object with metrics (required)
                       Example: '{"health_score": 65, "blocked_ratio": 25}'

Configure options:
    --rule RULE_ID     Rule ID to configure (required)
    --threshold VALUE  New threshold value (required)

Clear options:
    --alert-id ID      Alert ID to clear (required)

Available metrics:
    health_score        Overall health score (0-100)
    blocked_ratio       Percentage of tasks blocked
    stalled_count       Number of stalled tasks
    escalations_active  Number of active escalations
    velocity_drop_percent  Velocity drop from average (%)
    efficiency_score    G6 efficiency score (0-2, 1.0 = expected)
    efficiency_decline_percent  Efficiency decline from baseline (%)
    memory_hit_rate     Context reuse rate (0-1)

Default rules:
    health_critical      Health score < 60 (critical)
    health_warning       Health score < 80 (warning)
    blocked_critical     Blocked ratio > 40% (critical)
    blocked_warning      Blocked ratio > 20% (warning)
    stalled_warning      Stalled count > 0 (warning)
    escalation_critical  Active escalations > 5 (critical)
    escalation_warning   Active escalations > 2 (warning)
    velocity_warning     Velocity drop > 50% (warning)
    efficiency_critical  Efficiency score < 0.6 (critical)
    efficiency_warning   Efficiency score < 0.8 (warning)
    efficiency_degradation_warning  Efficiency decline > 15% (warning)
    memory_hit_rate_warning  Memory hit rate < 30% (warning)

Examples:
    # Evaluate metrics
    python alert_rules.py evaluate --metrics '{"health_score": 65, "blocked_ratio": 25}'

    # List active alerts
    python alert_rules.py list

    # Configure threshold
    python alert_rules.py configure --rule health_warning --threshold 75

    # Clear an alert
    python alert_rules.py clear --alert-id alert-abc123

    # Show current rules
    python alert_rules.py rules

Output: JSON with alert data.
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
        if command == "evaluate":
            if "metrics" not in args:
                print("Error: --metrics required")
                print("Example: --metrics '{\"health_score\": 65}'")
                sys.exit(1)

            try:
                metrics = json.loads(args["metrics"])
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in --metrics: {e}")
                sys.exit(1)

            result = evaluate_and_save(metrics)
            print(json.dumps(result, indent=2))

        elif command == "list":
            result = list_active_alerts()
            print(json.dumps(result, indent=2))

        elif command == "configure":
            if "rule" not in args:
                print("Error: --rule required")
                print(f"Valid rules: {list(RULES.keys())}")
                sys.exit(1)
            if "threshold" not in args:
                print("Error: --threshold required")
                sys.exit(1)

            try:
                threshold = float(args["threshold"])
            except ValueError:
                print("Error: --threshold must be a number")
                sys.exit(1)

            result = configure_threshold(args["rule"], threshold)
            print(json.dumps(result, indent=2))

        elif command == "clear":
            if "alert_id" not in args:
                print("Error: --alert-id required")
                sys.exit(1)

            result = clear_alert(args["alert_id"])
            print(json.dumps(result, indent=2))

        elif command == "rules":
            rules = get_rule_config()
            result = {
                "success": True,
                "rules": rules,
                "rule_count": len(rules),
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
