# /// script
# requires-python = ">=3.10"
# ///
"""
Hook Configuration Module — Central security profile management.

This module reads the security profile from gatesmith.json and provides
functions for hooks to determine their behavior based on the active profile.

Security Profiles:
- strict: All hooks block on issues (defense in depth)
- standard: Critical hooks block, others warn (balanced)
- minimal: Only catastrophic operations blocked (relies on /gate)

Usage in hooks:
    from hook_config import get_hook_behavior, should_exit_with_block

    behavior = get_hook_behavior("secrets_scanner")
    if behavior == "off":
        sys.exit(0)  # Hook disabled

    # ... hook logic ...

    if found_issue:
        exit_code = 2 if behavior == "block" else 1
        sys.exit(exit_code)
"""

import json
import sys
from pathlib import Path
from typing import Literal

# Type definitions
HookBehavior = Literal["block", "warn", "off", "on", "block_reduced"]
ProfileName = Literal["strict", "standard", "minimal"]

# Default profile if not configured
DEFAULT_PROFILE: ProfileName = "standard"

# Hook behavior definitions per profile
# This serves as fallback if gatesmith.json doesn't have the hook defined
DEFAULT_HOOK_BEHAVIORS: dict[str, dict[ProfileName, HookBehavior]] = {
    "block_dangerous": {
        "strict": "block",
        "standard": "block",
        "minimal": "block_reduced",
    },
    "network_egress_guard": {
        "strict": "block",
        "standard": "block",
        "minimal": "block",
    },
    "secrets_scanner": {"strict": "block", "standard": "block", "minimal": "warn"},
    "git_guardian": {"strict": "block", "standard": "block", "minimal": "warn"},
    "lint_on_edit": {"strict": "block", "standard": "warn", "minimal": "off"},
    "prompt_guard": {"strict": "block", "standard": "warn", "minimal": "off"},
    "validate_quality": {"strict": "block", "standard": "warn", "minimal": "off"},
    "dependency_check": {"strict": "warn", "standard": "warn", "minimal": "off"},
    "slopsquat_check": {"strict": "block", "standard": "block", "minimal": "off"},
    "log_activity": {"strict": "on", "standard": "on", "minimal": "on"},
    "session_init": {"strict": "on", "standard": "on", "minimal": "on"},
    "notify": {"strict": "on", "standard": "on", "minimal": "on"},
}

# Hard cap on the command text block_dangerous/network_egress_guard will run
# their canonicalization analysis over, in bytes. Their analysis cost grows
# with input size (2026-09-05: 1KB=0.13s, 32KB=34.8s measured pre-fix), and a
# hook Claude Code kills at its timeout is non-blocking — so an unbounded
# input lets a padded command (a large comment, a long heredoc body, a big
# generated string) time the check out and pass every rule unexamined. A
# command longer than this is blocked on size alone, before any analysis.
DEFAULT_MAX_COMMAND_LENGTH = 32 * 1024  # 32 KB

# Reduced dangerous patterns for minimal profile
# Only blocks truly catastrophic operations
MINIMAL_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+[/~]",  # rm -rf on root or home
    r"rm\s+-rf\s+/",  # rm -rf root
    r"sudo\s+rm\s+-rf",  # sudo rm -rf
    r"mkfs\.",  # format filesystem
    r"dd\s+if=.*of=/dev/sd",  # raw disk write
    r":\(\)\{.*\|.*&\s*\};:",  # fork bomb
]


def _has_security_section(path: Path) -> bool:
    """Whether a gatesmith.json carries the section this module reads."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and "security" in data


def find_forge_config() -> Path | None:
    """Find gatesmith.json by searching up from cwd.

    Two files carry the name: the project root's ``gatesmith.json`` — the
    one CLAUDE.md calls canonical — and ``.claude/gatesmith.json``, the
    install template. This module reads only the ``security`` section, so at
    each directory from cwd upwards the root file wins when it has one; the
    template is used otherwise, and a root file without ``security`` is the
    last fallback. Before this the template always won, so a ``security``
    block added to the canonical file was silently ignored.
    """
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        root_file = directory / "gatesmith.json"
        template = directory / ".claude" / "gatesmith.json"
        if root_file.is_file() and _has_security_section(root_file):
            return root_file
        if template.is_file():
            return template
        if root_file.is_file():
            return root_file
    return None


def load_config() -> dict:
    """Load gatesmith.json, returning empty dict if not found."""
    config_path = find_forge_config()
    if not config_path:
        return {}

    try:
        with open(config_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _security_section(config) -> dict:
    """The ``security`` object of a loaded config, or {} when the config or the
    section is not a JSON object. Every reader below used to call ``.get`` on
    whatever was there, so ``"security": []`` raised AttributeError in every
    hook that imports this module; the hook exited 1, which Claude Code treats
    as a NON-blocking failure, and the command ran unexamined (2026-10-17
    challenger on #735). Used only for values that fall back to a SAFE default
    (the command-length cap, profile descriptions); the profile and hook
    behaviours go through _security_state, which fails to STRICT.
    network_egress_guard.get_allowlist validates the section itself and BLOCKS
    on a malformed allowlist instead of using the defaults."""
    if not isinstance(config, dict):
        return {}
    security = config.get("security", {})
    return security if isinstance(security, dict) else {}


_PROFILES = ("strict", "standard", "minimal")
_BEHAVIORS = frozenset({"block", "warn", "off", "on", "block_reduced"})


def _read_config() -> tuple[dict, bool]:
    """(config, malformed). No gatesmith.json at all is ({}, False) — the
    documented defaults. A file that exists but cannot be read or parsed, or
    whose top level is not a JSON object, is ({}, True): malformed.
    load_config() returns {} for both, which is why a broken file silently
    meant "standard profile, defaults" (2026-10-17 challenger round 3)."""
    path = find_forge_config()
    if not path:
        return {}, False
    try:
        with open(path) as f:
            data = json.load(f)
    except (ValueError, OSError):  # JSONDecodeError and UnicodeDecodeError too
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def _security_state() -> tuple[dict, bool]:
    """(security section, malformed). Malformed when the config file is, when
    `security` is present but not an object, when `security.profile` is
    present but not one of the three profile names (a typo such as "Minimal"
    used to fall back to "standard"), or when `security.hooks` or one of its
    entries is not an object."""
    config, malformed = _read_config()
    if malformed:
        return {}, True
    if "security" not in config:
        return {}, False
    security = config["security"]
    if not isinstance(security, dict):
        return {}, True
    if "profile" in security and security["profile"] not in _PROFILES:
        return {}, True
    hooks = security.get("hooks", {})
    if not isinstance(hooks, dict) or not all(
        isinstance(entry, dict) for entry in hooks.values()
    ):
        return {}, True
    return security, False


def get_current_profile() -> ProfileName:
    """Get the currently active security profile.

    Fails CLOSED: a malformed config file or `security` section selects the
    STRICT profile (see _security_state), never the default "standard" — a
    broken config must not quietly relax a single hook."""
    security, malformed = _security_state()
    if malformed:
        return "strict"
    return security.get("profile", DEFAULT_PROFILE)  # type: ignore[return-value]


def get_hook_behavior(hook_name: str) -> HookBehavior:
    """
    Get the behavior for a specific hook based on current profile.

    Args:
        hook_name: Name of the hook (e.g., "secrets_scanner", "block_dangerous")

    Returns:
        HookBehavior: "block", "warn", "off", "on", or "block_reduced"

    Fails CLOSED: a malformed `security` section means the strict profile
    with the BUILT-IN behaviours (its own `hooks` entries are ignored), and a
    configured value that is not one of the five behaviours (`"Block"`,
    `true`, `null`, …) means "block". "Block" used to be returned as-is: truthy
    for is_enabled, but neither blocking nor warning in get_exit_code, so the
    hook ran and passed everything.
    """
    profile = get_current_profile()  # "strict" when the section is malformed
    security, _malformed = _security_state()  # {} when malformed: built-ins
    hooks = security.get("hooks", {})
    hook_config = hooks.get(hook_name, {})

    if profile in hook_config:
        value = hook_config[profile]
        return value if isinstance(value, str) and value in _BEHAVIORS else "block"

    # Fall back to defaults
    if hook_name in DEFAULT_HOOK_BEHAVIORS:
        return DEFAULT_HOOK_BEHAVIORS[hook_name].get(profile, "off")

    # Unknown hook, default to "on" for strict/standard, "off" for minimal
    return "off" if profile == "minimal" else "on"


def is_enabled(hook_name: str) -> bool:
    """
    Check if a hook is enabled (not "off").

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should run, False if disabled
    """
    behavior = get_hook_behavior(hook_name)
    return behavior != "off"


def should_block(hook_name: str) -> bool:
    """
    Check if a hook should block (exit code 2) on issues.

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should use exit code 2, False for warning (exit code 1)
    """
    behavior = get_hook_behavior(hook_name)
    return behavior in ("block", "block_reduced")


def should_warn(hook_name: str) -> bool:
    """
    Check if a hook should warn (exit code 1) on issues.

    Args:
        hook_name: Name of the hook

    Returns:
        True if the hook should use exit code 1 (warning)
    """
    behavior = get_hook_behavior(hook_name)
    return behavior == "warn"


def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
    """
    Get the appropriate exit code for a hook based on profile.

    Args:
        hook_name: Name of the hook
        issue_found: Whether an issue was found (default True)

    Returns:
        Exit code: 0 (success), 1 (warning), or 2 (block)
    """
    if not issue_found:
        return 0

    behavior = get_hook_behavior(hook_name)

    if behavior in ("block", "block_reduced"):
        return 2
    elif behavior == "warn":
        return 1
    else:
        return 0


def get_reduced_patterns(hook_name: str) -> list[str] | None:
    """
    Get reduced pattern list for minimal profile hooks.

    Args:
        hook_name: Name of the hook

    Returns:
        List of reduced patterns, or None if not applicable
    """
    behavior = get_hook_behavior(hook_name)
    if behavior != "block_reduced":
        return None

    if hook_name == "block_dangerous":
        return MINIMAL_DANGEROUS_PATTERNS

    return None


def get_max_command_length() -> int:
    """Hard cap on analyzable command text, in bytes.

    Read from ``security.max_command_length`` in gatesmith.json when
    present (same section/precedence as ``get_hook_behavior``); falls back to
    ``DEFAULT_MAX_COMMAND_LENGTH`` when absent, non-numeric, or non-positive —
    a misconfigured cap must fail toward the safe (bounded) default, never
    toward "no limit."
    """
    config = load_config()
    security = _security_section(config)
    value = security.get("max_command_length", DEFAULT_MAX_COMMAND_LENGTH)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return DEFAULT_MAX_COMMAND_LENGTH
    return value


def get_profile_info() -> dict:
    """
    Get detailed information about the current security profile.

    Returns:
        Dict with profile name, description, and hook statuses
    """
    profile = get_current_profile()
    config = load_config()
    security = _security_section(config)
    profiles = security.get("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}

    profile_info = profiles.get(profile, {})

    hook_statuses = {}
    for hook_name in DEFAULT_HOOK_BEHAVIORS:
        hook_statuses[hook_name] = get_hook_behavior(hook_name)

    return {
        "profile": profile,
        "description": profile_info.get("description", ""),
        "philosophy": profile_info.get("philosophy", ""),
        "hooks": hook_statuses,
    }


def print_profile_status(file=sys.stderr) -> None:
    """Print formatted profile status to stderr."""
    info = get_profile_info()

    border = "═" * 67
    print(f"\n╔{border}╗", file=file)
    print(f"║ {'FORGE SECURITY PROFILE':<40} [{info['profile']:>10}] ║", file=file)
    print(f"╠{border}╣", file=file)
    print(f"║ {info['description']:<65} ║", file=file)
    print(f"╠{border}╣", file=file)
    print(f"║ {'Hook Status:':<65} ║", file=file)

    # Format hook statuses
    hooks = info["hooks"]
    hook_names = list(hooks.keys())

    for i, hook_name in enumerate(hook_names):
        status = hooks[hook_name].upper()
        is_last = i == len(hook_names) - 1
        prefix = "└─" if is_last else "├─"
        display_name = f"{hook_name}.py"
        line = f" {prefix} {display_name:<25} {status:<8}"
        print(f"║{line:<65} ║", file=file)

    print(f"╚{border}╝\n", file=file)


# For direct execution (testing)
if __name__ == "__main__":
    print_profile_status(file=sys.stdout)
