# /// script
# requires-python = ">=3.10"
# ///
"""
Security Profile Management Utility

View and manage Gatesmith security profiles. This tool provides:
- status: Display current security profile and hook statuses
- set: Change the active security profile
- explain: Show detailed explanation of each profile

Usage:
    uv run .claude/tools/security_profile.py status
    uv run .claude/tools/security_profile.py set minimal
    uv run .claude/tools/security_profile.py set strict
    uv run .claude/tools/security_profile.py explain
    uv run .claude/tools/security_profile.py explain minimal
"""

import json
import sys
from pathlib import Path
from typing import Literal

ProfileName = Literal["strict", "standard", "minimal"]

# Hook descriptions for display
HOOK_DESCRIPTIONS = {
    "block_dangerous": "Prevents rm -rf, sudo rm, etc.",
    "secrets_scanner": "Prevents hardcoded secrets",
    "git_guardian": "Prevents secrets in commits",
    "lint_on_edit": "Protects .git/, lock files",
    "prompt_guard": "Warns on destructive prompts",
    "validate_quality": "Runs linter after edits",
    "dependency_check": "Checks for vulnerabilities",
    "log_activity": "Audit trail (never blocks)",
    "session_init": "Session initialization",
    "notify": "Desktop notifications",
}

# Profile descriptions
PROFILE_INFO = {
    "strict": {
        "name": "Strict",
        "tagline": "Defense in Depth",
        "description": "All hooks active as blocking checks. Maximum protection for production environments.",
        "use_cases": [
            "Production deployments",
            "Security-sensitive projects",
            "Teams with strict compliance requirements",
            "When learning Gatesmith (safest option)",
        ],
    },
    "standard": {
        "name": "Standard",
        "tagline": "Balanced Protection",
        "description": "Critical protections block, others warn. Recommended for most development.",
        "use_cases": [
            "Normal development workflow",
            "Team projects",
            "When you want protection without friction",
            "Default for new installations",
        ],
    },
    "minimal": {
        "name": "Minimal",
        "tagline": "Trust + Checkpoints",
        "description": "Only catastrophic operations blocked. Relies on /gate for security checkpoints.",
        "use_cases": [
            "Experienced developers who know the risks",
            "Fast iteration during prototyping",
            "When hooks are causing too much friction",
            "Personal projects with low risk tolerance",
        ],
    },
}

# Hook behaviors per profile
HOOK_BEHAVIORS = {
    "block_dangerous": {"strict": "BLOCK", "standard": "BLOCK", "minimal": "BLOCK*"},
    "secrets_scanner": {"strict": "BLOCK", "standard": "BLOCK", "minimal": "WARN"},
    "git_guardian": {"strict": "BLOCK", "standard": "BLOCK", "minimal": "WARN"},
    "lint_on_edit": {"strict": "BLOCK", "standard": "WARN", "minimal": "OFF"},
    "prompt_guard": {"strict": "BLOCK", "standard": "WARN", "minimal": "OFF"},
    "validate_quality": {"strict": "BLOCK", "standard": "WARN", "minimal": "OFF"},
    "dependency_check": {"strict": "WARN", "standard": "WARN", "minimal": "OFF"},
    "log_activity": {"strict": "ON", "standard": "ON", "minimal": "ON"},
    "session_init": {"strict": "ON", "standard": "ON", "minimal": "ON"},
    "notify": {"strict": "ON", "standard": "ON", "minimal": "ON"},
}


def find_forge_config() -> Path | None:
    """Find gatesmith.json by searching up from cwd."""
    cwd = Path.cwd()

    paths_to_check = [
        cwd / ".claude" / "gatesmith.json",
        cwd / "gatesmith.json",
    ]

    current = cwd
    while current != current.parent:
        paths_to_check.append(current / ".claude" / "gatesmith.json")
        current = current.parent

    for path in paths_to_check:
        if path.exists():
            return path

    return None


def load_config() -> tuple[dict, Path | None]:
    """Load gatesmith.json, returning config and path."""
    config_path = find_forge_config()
    if not config_path:
        return {}, None

    try:
        with open(config_path) as f:
            return json.load(f), config_path
    except (json.JSONDecodeError, OSError):
        return {}, config_path


def get_current_profile() -> ProfileName:
    """Get the currently active security profile."""
    config, _ = load_config()
    security = config.get("security", {})
    profile = security.get("profile", "standard")

    if profile not in ("strict", "standard", "minimal"):
        return "standard"

    return profile  # type: ignore


def cmd_status():
    """Display current security profile status."""
    profile = get_current_profile()
    _, config_path = load_config()

    profile_info = PROFILE_INFO[profile]

    border = "═" * 67
    print(f"\n╔{border}╗")
    print(f"║ {'FORGE SECURITY PROFILE':<40} [{profile:>10}] ║")
    print(f"╠{border}╣")
    print(f"║ Profile: {profile_info['name']:<56} ║")
    print(f"║ {profile_info['description']:<65} ║")
    print(f"╠{border}╣")
    print(f"║ {'Hook Status:':<65} ║")

    # Display hook statuses
    hooks = list(HOOK_BEHAVIORS.keys())
    for i, hook_name in enumerate(hooks):
        status = HOOK_BEHAVIORS[hook_name][profile]
        description = HOOK_DESCRIPTIONS.get(hook_name, "")
        is_last = i == len(hooks) - 1
        prefix = "└─" if is_last else "├─"
        display_name = f"{hook_name}.py"

        # Color-code status (using ANSI where supported)
        status_display = status.ljust(8)

        line = f" {prefix} {display_name:<25} {status_display} {description}"
        # Truncate if too long
        if len(line) > 65:
            line = line[:62] + "..."
        print(f"║{line:<65} ║")

    print(f"╠{border}╣")
    if config_path:
        config_rel = (
            str(config_path.relative_to(Path.cwd()))
            if config_path.is_relative_to(Path.cwd())
            else str(config_path)
        )
        print(f"║ Config: {config_rel:<57} ║")
    else:
        print(f"║ {'Config: Not found (using defaults)':<65} ║")

    print(f"╠{border}╣")
    print(
        f"║ {'To change: uv run .claude/tools/security_profile.py set <profile>':<65} ║"
    )
    print(f"║ {'Profiles:  strict | standard | minimal':<65} ║")
    print(f"╚{border}╝\n")

    # Note about BLOCK*
    if profile == "minimal":
        print("* BLOCK* means reduced patterns (only catastrophic operations)")
        print("  Full protection uses /gate checkpoints\n")


def cmd_set(profile: str):
    """Set the security profile."""
    if profile not in ("strict", "standard", "minimal"):
        print(f"Error: Invalid profile '{profile}'", file=sys.stderr)
        print("Valid profiles: strict, standard, minimal", file=sys.stderr)
        sys.exit(1)

    config, config_path = load_config()

    if not config_path:
        print("Error: gatesmith.json not found", file=sys.stderr)
        print("Run this from a Gatesmith project directory", file=sys.stderr)
        sys.exit(1)

    # Update the profile
    if "security" not in config:
        config["security"] = {}

    old_profile = config["security"].get("profile", "standard")
    config["security"]["profile"] = profile

    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

        print(f"Security profile changed: {old_profile} → {profile}")
        print()

        # Show what changed
        profile_info = PROFILE_INFO[profile]
        print(f"  {profile_info['name']}: {profile_info['tagline']}")
        print(f"  {profile_info['description']}")
        print()

        # Show key hook changes
        print("  Key changes:")
        for hook_name in ["secrets_scanner", "git_guardian", "lint_on_edit"]:
            old_status = HOOK_BEHAVIORS[hook_name][old_profile]
            new_status = HOOK_BEHAVIORS[hook_name][profile]
            if old_status != new_status:
                print(f"    {hook_name}: {old_status} → {new_status}")

        print()
        print(
            "  Run 'uv run .claude/tools/security_profile.py status' for full details"
        )

    except OSError as e:
        print(f"Error writing config: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_explain(profile: str | None = None):
    """Explain security profiles in detail."""
    profiles_to_show = [profile] if profile else ["strict", "standard", "minimal"]

    for p in profiles_to_show:
        if p not in PROFILE_INFO:
            print(f"Error: Unknown profile '{p}'", file=sys.stderr)
            print("Valid profiles: strict, standard, minimal", file=sys.stderr)
            sys.exit(1)

        info = PROFILE_INFO[p]
        current = get_current_profile()
        marker = " ← CURRENT" if p == current else ""

        border = "─" * 60
        print(f"\n┌{border}┐")
        print(f"│ {info['name'].upper()}: {info['tagline']}{marker:<30} │")
        print(f"├{border}┤")

        # Description (word wrap)
        desc = info["description"]
        while len(desc) > 58:
            # Find last space before 58
            wrap_at = desc[:58].rfind(" ")
            if wrap_at == -1:
                wrap_at = 58
            print(f"│ {desc[:wrap_at]:<58} │")
            desc = desc[wrap_at:].strip()
        print(f"│ {desc:<58} │")

        print(f"├{border}┤")
        print(f"│ {'Use Cases:':<58} │")
        for use_case in info["use_cases"]:
            print(f"│   • {use_case:<55} │")

        print(f"├{border}┤")
        print(f"│ {'Hook Behavior:':<58} │")

        # Show hooks in categories
        blocking_hooks = [
            h for h in HOOK_BEHAVIORS if HOOK_BEHAVIORS[h][p] in ("BLOCK", "BLOCK*")
        ]
        warning_hooks = [h for h in HOOK_BEHAVIORS if HOOK_BEHAVIORS[h][p] == "WARN"]
        off_hooks = [h for h in HOOK_BEHAVIORS if HOOK_BEHAVIORS[h][p] == "OFF"]
        on_hooks = [h for h in HOOK_BEHAVIORS if HOOK_BEHAVIORS[h][p] == "ON"]

        if blocking_hooks:
            print(f"│   BLOCK: {', '.join(blocking_hooks):<49} │")
        if warning_hooks:
            print(f"│   WARN:  {', '.join(warning_hooks):<49} │")
        if off_hooks:
            print(f"│   OFF:   {', '.join(off_hooks):<49} │")
        if on_hooks:
            print(f"│   ON:    {', '.join(on_hooks):<49} │")

        print(f"└{border}┘")

    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: security_profile.py <command> [args]")
        print()
        print("Commands:")
        print("  status              Show current security profile and hook status")
        print("  set <profile>       Change security profile (strict|standard|minimal)")
        print("  explain [profile]   Explain profiles in detail")
        print()
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "status":
        cmd_status()
    elif command == "set":
        if len(sys.argv) < 3:
            print("Error: 'set' requires a profile name", file=sys.stderr)
            print(
                "Usage: security_profile.py set <strict|standard|minimal>",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd_set(sys.argv[2])
    elif command == "explain":
        profile = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_explain(profile)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Commands: status, set, explain", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
