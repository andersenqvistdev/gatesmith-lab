# /// script
# requires-python = ">=3.10"
# ///
"""
Autonomy helper functions for hooks and commands.
Provides consistent autonomy mode queries across the framework.
"""

import json
import os
from pathlib import Path

__all__ = [
    "load_forge_config",
    "is_autonomous_mode",
    "get_autonomy_level",
    "can_proceed_without_prompt",
    "is_flow_mode_enabled",
]


def load_forge_config() -> dict:
    """Load .claude/gatesmith.json, return empty dict if missing."""
    config_path = Path(os.getcwd()) / ".claude" / "gatesmith.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def is_autonomous_mode() -> bool:
    """Return True if autonomy.level is 'full' in gatesmith.json."""
    config = load_forge_config()
    return config.get("autonomy", {}).get("level") == "full"


def get_autonomy_level() -> str:
    """Return autonomy level: 'full', 'standard', or 'supervised'. Default: 'standard'."""
    config = load_forge_config()
    return config.get("autonomy", {}).get("level", "standard")


def can_proceed_without_prompt(operation: str) -> bool:
    """
    Determine if operation can proceed autonomously.

    Args:
        operation: One of 'build', 'verify', 'gate', 'commit', 'push'

    Returns:
        True if operation can proceed without human prompt based on:
        - autonomy.level setting
        - autonomy.pausePoints configuration
        - operation type (push always requires prompt unless flowMode.enabled)
    """
    config = load_forge_config()
    autonomy = config.get("autonomy", {})
    level = autonomy.get("level", "standard")
    pause_points = autonomy.get("pausePoints", {})
    flow_mode = autonomy.get("flowMode", {})

    # Supervised mode always requires prompts
    if level == "supervised":
        return False

    # Push requires flowMode unless in full autonomy
    if operation == "push":
        return flow_mode.get("enabled", False) or level == "full"

    # Check specific pause points
    pause_map = {
        "build": "afterWaves",
        "verify": "afterVerify",
        "gate": "afterGate",
    }

    if operation in pause_map:
        pause_key = pause_map[operation]
        if pause_points.get(pause_key, False):
            return False

    # Full autonomy proceeds without prompts (except for security/failure)
    if level == "full":
        return True

    # Standard mode: build and verify proceed, gate pauses
    if level == "standard":
        return operation in ("build", "verify", "commit")

    return False


def is_flow_mode_enabled() -> bool:
    """Return True if autonomy.flowMode.enabled is True."""
    config = load_forge_config()
    return config.get("autonomy", {}).get("flowMode", {}).get("enabled", False)


if __name__ == "__main__":
    # Quick test when run directly
    print(f"Autonomy level: {get_autonomy_level()}")
    print(f"Is autonomous: {is_autonomous_mode()}")
    print(f"Flow mode: {is_flow_mode_enabled()}")
    print(f"Can build: {can_proceed_without_prompt('build')}")
    print(f"Can push: {can_proceed_without_prompt('push')}")
