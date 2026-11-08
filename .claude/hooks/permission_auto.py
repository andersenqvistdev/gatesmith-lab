# /// script
# requires-python = ">=3.10"
# ///
"""
PermissionRequest Hook: Auto-approve safe read-only operations.
Reduces permission fatigue while maintaining security.

Gate-Aware: After /gate passes, GitHub operations are auto-approved.
"""

import json
import sys
from pathlib import Path

# Tools that are always safe to auto-approve
SAFE_TOOLS = {"Read", "Glob", "Grep", "Task", "TaskOutput", "KillShell"}

# Bash commands that are safe to auto-approve
SAFE_BASH_PREFIXES = [
    # File reading/inspection
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "stat",
    # Text processing (read-only)
    "awk",
    "grep",
    "sed -n",  # Only print mode (read-only)
    "sort",
    "uniq",
    "cut",
    "tr",
    "xargs echo",  # Safe xargs usage
    "column",
    "jq",  # JSON processing
    # System info
    "which",
    "echo",
    "pwd",
    "date",
    "whoami",
    "hostname",
    "env",
    "printenv",
    # Git read operations
    "git status",
    "git diff",
    "git log",
    "git branch",
    "git show",
    "git rev-parse",
    # Build/lint/test commands
    "npm run lint",
    "npm run test",
    "npm run build",
    "npx eslint",
    "npx prettier",
    "python -m pytest",
    "pytest",
    "ruff check",
    "ruff format",
    "eslint",
    "ruby -ryaml",  # YAML validation
    # Gatesmith/company operations
    "mkdir -p .company/",  # Company directory creation
    "cat .company/",  # Reading company files (+ piped validation)
    "uv run",
    "uv pip",
]

# GitHub operations that require gate approval first
GITHUB_BASH_PREFIXES = [
    "gh pr create",
    "gh pr merge",
    "gh issue create",
    "gh release create",
    "gh api",
]


def find_project_root() -> Path | None:
    """Find project root by looking for .claude directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent
    return None


def is_gate_passed() -> bool:
    """Check if /gate has approved the current session."""
    project_root = find_project_root()
    if not project_root:
        return False

    gate_file = project_root / ".claude" / "gate_passed"
    if not gate_file.exists():
        return False

    # Check if gate was passed in the last 4 hours (session validity)
    try:
        import time

        mtime = gate_file.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        return age_hours < 4
    except OSError:
        return False


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Auto-approve safe tools
    if tool_name in SAFE_TOOLS:
        print(json.dumps({"decision": "approve", "reason": "Safe read-only tool"}))
        sys.exit(0)

    # Auto-approve safe bash commands
    if tool_name == "Bash":
        command = tool_input.get("command", "").strip()

        # Get the first word of the command (the actual command name)
        first_word = command.split()[0] if command.split() else ""

        # Always-safe commands - check both prefix and first word
        for prefix in SAFE_BASH_PREFIXES:
            # Direct prefix match (e.g., "git status", "npm run lint")
            if command.startswith(prefix):
                print(
                    json.dumps(
                        {"decision": "approve", "reason": f"Safe command: {prefix}"}
                    )
                )
                sys.exit(0)
            # First word match (e.g., "cat /tmp/file.txt" matches "cat")
            if first_word == prefix:
                print(
                    json.dumps(
                        {"decision": "approve", "reason": f"Safe command: {first_word}"}
                    )
                )
                sys.exit(0)

        # GitHub operations: auto-approve if gate passed
        if is_gate_passed():
            for prefix in GITHUB_BASH_PREFIXES:
                if command.startswith(prefix):
                    print(
                        json.dumps(
                            {"decision": "approve", "reason": f"Gate passed: {prefix}"}
                        )
                    )
                    sys.exit(0)

            # Also allow git push to feature branches after gate
            if command.startswith("git push"):
                # Don't auto-approve push to main/master (git_guardian handles blocking)
                if "main" not in command and "master" not in command:
                    print(
                        json.dumps(
                            {
                                "decision": "approve",
                                "reason": "Gate passed: git push to feature branch",
                            }
                        )
                    )
                    sys.exit(0)

    # Everything else: don't override, let default permission flow handle it
    sys.exit(0)


if __name__ == "__main__":
    main()
