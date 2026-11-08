# /// script
# requires-python = ">=3.10"
# ///
"""
Notification Hook: Desktop notifications for long-running tasks.
Uses macOS osascript, with fallback to terminal bell.

Also supports alert dispatching with severity-based formatting and rate limiting.
"""

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Alert rate limiting configuration
ALERT_RATE_FILE = os.path.join(os.getcwd(), ".company", "alert_rate.json")
RATE_LIMIT_MINUTES = 15
MAX_ALERTS_PER_HOUR = 5


def sanitize_for_applescript(text: str) -> str:
    """Escape special characters for safe AppleScript interpolation.

    Prevents command injection by escaping backslashes and double quotes
    that could break out of the string context in osascript.
    """
    if not text:
        return ""
    # Escape backslashes first, then double quotes
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str):
    """Send a desktop notification."""
    system = platform.system()

    if system == "Darwin":
        # Sanitize inputs to prevent AppleScript injection
        safe_title = sanitize_for_applescript(title)
        safe_message = sanitize_for_applescript(message)
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_message}" with title "{safe_title}"',
            ],
            capture_output=True,
            timeout=5,
        )
    elif system == "Linux":
        try:
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError:
            print("\a", end="")  # Terminal bell fallback
    else:
        print("\a", end="")  # Terminal bell fallback


def load_alert_rates() -> dict:
    """Load last sent times for alerts."""
    try:
        if os.path.exists(ALERT_RATE_FILE):
            with open(ALERT_RATE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        pass
    return {}


def save_alert_rates(rates: dict):
    """Save alert rate tracking."""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(ALERT_RATE_FILE), exist_ok=True)
        with open(ALERT_RATE_FILE, "w") as f:
            json.dump(rates, f, indent=2)
    except (IOError, OSError):
        pass  # Silently fail if we can't save


def can_send_alert(rule_id: str) -> bool:
    """Check if we can send an alert (rate limiting)."""
    rates = load_alert_rates()
    now = datetime.now(timezone.utc)

    # Check per-rule rate limit
    last_sent = rates.get(rule_id)
    if last_sent:
        try:
            last_time = datetime.fromisoformat(last_sent)
            if (now - last_time).total_seconds() < RATE_LIMIT_MINUTES * 60:
                return False
        except ValueError:
            pass  # Invalid timestamp, allow sending

    # Check hourly limit
    hour_ago = now - timedelta(hours=1)
    recent_count = 0
    for t in rates.values():
        try:
            if datetime.fromisoformat(t) > hour_ago:
                recent_count += 1
        except ValueError:
            pass  # Skip invalid timestamps

    if recent_count >= MAX_ALERTS_PER_HOUR:
        return False

    return True


def send_alert(alert: dict) -> bool:
    """Send alert notification with rate limiting.

    Args:
        alert: Dictionary containing:
            - rule_id: Unique identifier for the alert rule
            - severity: "critical" or "warning"
            - message: Alert message text

    Returns:
        True if alert was sent, False if rate limited
    """
    rule_id = alert.get("rule_id", "unknown")

    if not can_send_alert(rule_id):
        return False

    severity = alert.get("severity", "warning")
    message = alert.get("message", "Alert triggered")

    if severity == "critical":
        title = "🚨 CRITICAL Alert"
        formatted_message = f"CRITICAL: {message}"
    else:
        title = "⚠️ Warning"
        formatted_message = message

    notify(title, formatted_message)

    # Update rate tracking
    rates = load_alert_rates()
    rates[rule_id] = datetime.now(timezone.utc).isoformat()
    save_alert_rates(rates)

    return True


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    message = input_data.get("message", "Task update")
    title = "Claude Code"

    notify(title, message[:200])
    sys.exit(0)


if __name__ == "__main__":
    main()
