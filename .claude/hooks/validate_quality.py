# /// script
# requires-python = ">=3.10"
# ///
"""
PostToolUse Hook: Validate code quality after file writes/edits.
Runs linter and formatter checks. Exit code 2 blocks if quality fails.

Security Profile Aware:
- strict: Blocks on quality failures
- standard: Warns on quality failures
- minimal: Disabled
"""

import json
import os
import subprocess
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import get_exit_code, is_enabled
except ImportError:
    # Fallback if hook_config not available
    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "validate_quality"


def _project_root() -> str:
    """The project this hook belongs to, without trusting cwd.

    CLAUDE_PROJECT_DIR is authoritative; the hook's own location is the fallback
    because it cannot drift the way a working directory can.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and os.path.isdir(os.path.join(env_root, ".claude")):
        return os.path.realpath(env_root)
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(here, ".claude")):
            return os.path.realpath(here)
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.realpath(os.getcwd())
        here = parent


def _inside_project(file_path: str) -> bool:
    """Whether ``file_path`` lies within the project root.

    realpath on both sides, so a symlink pointing out of the tree cannot smuggle
    an outside file past the check.
    """
    root = _project_root()
    target = os.path.realpath(file_path)
    return target == root or target.startswith(root + os.sep)


# Map file extensions to their quality check commands
QUALITY_CHECKS = {
    ".py": [
        {"name": "ruff-check", "cmd": ["ruff", "check", "--fix"], "fix": True},
        {"name": "ruff-format", "cmd": ["ruff", "format"], "fix": True},
    ],
    ".ts": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".tsx": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".js": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
    ".jsx": [
        {"name": "eslint", "cmd": ["npx", "eslint", "--fix"], "fix": True},
    ],
}


def main():
    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name not in ("Write", "Edit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        sys.exit(0)

    # Only ever rewrite files inside THIS project. Every check below runs with
    # --fix, so this hook edits the file in place; without a bound it did that
    # to any .py path the payload named, anywhere on disk. A file written into
    # another repository or a scratch directory would be reformatted by this
    # project's linter, under whatever configuration happened to apply there.
    if not _inside_project(file_path):
        sys.exit(0)

    _, ext = os.path.splitext(file_path)
    checks = QUALITY_CHECKS.get(ext, [])

    if not checks:
        sys.exit(0)

    errors = []
    for check in checks:
        try:
            result = subprocess.run(
                check["cmd"] + [file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # A fix-mode tool exits non-zero when it found something it could
            # NOT fix. The old condition skipped those entirely, and since every
            # configured check is fix-mode, `errors` was always empty and this
            # hook could never report anything at all. Unfixable findings are
            # exactly the ones worth surfacing.
            if result.returncode != 0:
                detail = (result.stdout or result.stderr or "").strip()
                if detail:
                    errors.append(f"{check['name']}: {detail[:500]}")
        except FileNotFoundError:
            # Tool not installed, skip
            continue
        except subprocess.TimeoutExpired:
            continue

    if errors:
        error_msg = "\n".join(errors)
        exit_code = get_exit_code(HOOK_NAME, issue_found=True)
        decision = "block" if exit_code == 2 else "warn"
        print(
            json.dumps(
                {
                    "decision": decision,
                    "reason": f"Quality check {'failed' if decision == 'block' else 'warning'}:\n{error_msg}",
                }
            )
        )
        sys.exit(exit_code)

    sys.exit(0)


if __name__ == "__main__":
    main()
