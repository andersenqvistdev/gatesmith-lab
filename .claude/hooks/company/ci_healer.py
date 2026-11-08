#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-066-004: Self-Healing CI

Automatically diagnoses and fixes common CI failures:
- Missing dependencies (ModuleNotFoundError)
- Lint/format errors (ruff, black)
- Missing files (FileNotFoundError for test fixtures)
- Import errors

Safety:
- Rate limited (max 3 heal attempts per PR)
- Only fixes known safe patterns
- Creates fix commits on the same branch (not new PRs)
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class CIFailure:
    """Represents a CI failure that may be auto-healable."""

    pr_number: int
    branch: str
    check_name: str
    error_type: str
    error_message: str
    file_path: str | None = None
    line_number: int | None = None
    suggested_fix: str | None = None


@dataclass
class HealResult:
    """Result of a heal attempt."""

    success: bool
    failure: CIFailure
    fix_applied: str | None = None
    commit_sha: str | None = None
    error: str | None = None


@dataclass
class HealerConfig:
    """Configuration for CI healer."""

    enabled: bool = True
    max_heal_attempts_per_pr: int = 3
    max_heals_per_day: int = 10
    auto_push: bool = True
    healable_patterns: list = field(default_factory=list)


# Pattern matchers: (regex_pattern, error_type, fix_function_name)
FAILURE_PATTERNS: list[tuple[str, str, str]] = [
    # Missing Python module
    (
        r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]",
        "missing_module",
        "_fix_missing_module",
    ),
    # Missing file (test fixtures)
    (
        r"FileNotFoundError: \[Errno 2\] No such file or directory: ['\"]([^'\"]+)['\"]",
        "missing_file",
        "_fix_missing_file",
    ),
    # Ruff lint errors
    (
        r"(\S+\.py):(\d+):\d+: ([A-Z]\d+) (.+)",
        "lint_error",
        "_fix_lint_error",
    ),
    # Black format errors
    (
        r"would reformat (\S+\.py)",
        "format_error",
        "_fix_format_error",
    ),
    # Import error (undefined name)
    (
        r"(\S+\.py):\d+:\d+: F821 undefined name ['\"](\w+)['\"]",
        "undefined_name",
        "_fix_undefined_name",
    ),
    # Pytest collection error
    (
        r"ERROR collecting (\S+\.py)",
        "collection_error",
        "_fix_collection_error",
    ),
]

# Known safe modules to add as dependencies
SAFE_MODULES: dict[str, str] = {
    "cryptography": "cryptography>=42.0.0",
    "pytest": "pytest>=8.0.0",
    "requests": "requests>=2.31.0",
    "pydantic": "pydantic>=2.0.0",
    "httpx": "httpx>=0.27.0",
    "stripe": "stripe>=8.0.0",
    "flask": "flask>=3.0.0",
    "fastapi": "fastapi>=0.110.0",
}


def parse_ci_failures(log_output: str, pr_number: int, branch: str) -> list[CIFailure]:
    """
    Parse CI log output and extract healable failures.

    Args:
        log_output: Raw CI log output
        pr_number: PR number
        branch: Branch name

    Returns:
        List of CIFailure objects
    """
    failures = []

    for pattern, error_type, fix_func in FAILURE_PATTERNS:
        for match in re.finditer(pattern, log_output, re.MULTILINE):
            failure = CIFailure(
                pr_number=pr_number,
                branch=branch,
                check_name="Test",  # Default, can be overridden
                error_type=error_type,
                error_message=match.group(0),
            )

            # Extract additional info based on pattern type
            if error_type == "missing_module":
                failure.suggested_fix = f"Add {match.group(1)} to dependencies"
            elif error_type == "missing_file":
                failure.file_path = match.group(1)
                failure.suggested_fix = f"Create missing file: {match.group(1)}"
            elif error_type == "lint_error":
                failure.file_path = match.group(1)
                failure.line_number = int(match.group(2))
                failure.suggested_fix = f"Fix {match.group(3)}: {match.group(4)}"
            elif error_type == "format_error":
                failure.file_path = match.group(1)
                failure.suggested_fix = f"Run black on {match.group(1)}"
            elif error_type == "undefined_name":
                failure.file_path = match.group(1)
                failure.suggested_fix = f"Import or define {match.group(2)}"

            failures.append(failure)

    return failures


def _fix_missing_module(failure: CIFailure, project_root: Path) -> HealResult:
    """
    Fix missing module by adding to pyproject.toml dependencies.

    Only adds known safe modules from SAFE_MODULES.
    """
    # Extract module name from error message
    match = re.search(r"No module named ['\"]([^'\"]+)['\"]", failure.error_message)
    if not match:
        return HealResult(
            success=False,
            failure=failure,
            error="Could not extract module name",
        )

    module_name = match.group(1).split(".")[0]  # Get base module

    if module_name not in SAFE_MODULES:
        return HealResult(
            success=False,
            failure=failure,
            error=f"Module {module_name} not in safe list",
        )

    # Check pyproject.toml
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return HealResult(
            success=False,
            failure=failure,
            error="pyproject.toml not found",
        )

    try:
        content = pyproject_path.read_text()

        # Check if already in dependencies
        if module_name in content:
            return HealResult(
                success=False,
                failure=failure,
                error=f"Module {module_name} already in pyproject.toml",
            )

        # Find dependencies section and add
        dep_spec = SAFE_MODULES[module_name]

        # Try to find [project.optional-dependencies.dev] first
        if "[project.optional-dependencies]" in content:
            # Add to dev dependencies
            content = re.sub(
                r"(\[project\.optional-dependencies\]\s*\n\s*dev\s*=\s*\[)",
                f'\\1\n    "{dep_spec}",',
                content,
            )
        elif "[project.dependencies]" in content:
            # Add to main dependencies
            content = re.sub(
                r"(\[project\.dependencies\]\s*\n)",
                f'\\1    "{dep_spec}",\n',
                content,
            )
        else:
            return HealResult(
                success=False,
                failure=failure,
                error="Could not find dependencies section",
            )

        pyproject_path.write_text(content)

        return HealResult(
            success=True,
            failure=failure,
            fix_applied=f"Added {dep_spec} to pyproject.toml",
        )

    except Exception as e:
        return HealResult(
            success=False,
            failure=failure,
            error=str(e),
        )


def _fix_missing_file(failure: CIFailure, project_root: Path) -> HealResult:
    """
    Fix missing file by creating it with minimal content.

    Only creates files in safe directories (tests/, .company/).
    """
    if not failure.file_path:
        return HealResult(
            success=False,
            failure=failure,
            error="No file path specified",
        )

    file_path = Path(failure.file_path)

    # Security: only create files in safe directories
    safe_prefixes = [".company/", "tests/", ".claude/"]
    if not any(str(file_path).startswith(p) for p in safe_prefixes):
        return HealResult(
            success=False,
            failure=failure,
            error=f"Cannot create file outside safe directories: {file_path}",
        )

    try:
        full_path = project_root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Determine content based on file type
        if file_path.suffix == ".json":
            content = "{}"
        elif file_path.suffix == ".py":
            content = '"""Auto-generated placeholder."""\n'
        elif file_path.suffix == ".md":
            content = "# Placeholder\n"
        else:
            content = ""

        full_path.write_text(content)

        return HealResult(
            success=True,
            failure=failure,
            fix_applied=f"Created {file_path} with placeholder content",
        )

    except Exception as e:
        return HealResult(
            success=False,
            failure=failure,
            error=str(e),
        )


def _fix_lint_error(failure: CIFailure, project_root: Path) -> HealResult:
    """Fix lint errors by running ruff --fix."""
    if not failure.file_path:
        return HealResult(
            success=False,
            failure=failure,
            error="No file path specified",
        )

    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "check", "--fix", failure.file_path],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=30,
        )

        if result.returncode == 0:
            return HealResult(
                success=True,
                failure=failure,
                fix_applied=f"Applied ruff --fix to {failure.file_path}",
            )
        else:
            return HealResult(
                success=False,
                failure=failure,
                error=f"ruff --fix failed: {result.stderr[:200]}",
            )

    except Exception as e:
        return HealResult(
            success=False,
            failure=failure,
            error=str(e),
        )


def _fix_format_error(failure: CIFailure, project_root: Path) -> HealResult:
    """Fix format errors by running ruff format."""
    if not failure.file_path:
        return HealResult(
            success=False,
            failure=failure,
            error="No file path specified",
        )

    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "format", failure.file_path],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=30,
        )

        if result.returncode == 0:
            return HealResult(
                success=True,
                failure=failure,
                fix_applied=f"Applied ruff format to {failure.file_path}",
            )
        else:
            return HealResult(
                success=False,
                failure=failure,
                error=f"ruff format failed: {result.stderr[:200]}",
            )

    except Exception as e:
        return HealResult(
            success=False,
            failure=failure,
            error=str(e),
        )


def _fix_undefined_name(failure: CIFailure, project_root: Path) -> HealResult:
    """
    Fix undefined name errors by adding imports.

    Only handles well-known names.
    """
    # Extract the undefined name
    match = re.search(r"undefined name ['\"](\w+)['\"]", failure.error_message)
    if not match:
        return HealResult(
            success=False,
            failure=failure,
            error="Could not extract undefined name",
        )

    undefined_name = match.group(1)

    # Known imports
    known_imports: dict[str, str] = {
        "Path": "from pathlib import Path",
        "datetime": "from datetime import datetime",
        "timezone": "from datetime import timezone",
        "timedelta": "from datetime import timedelta",
        "json": "import json",
        "re": "import re",
        "os": "import os",
        "sys": "import sys",
        "logging": "import logging",
        "subprocess": "import subprocess",
        "Dict": "from typing import Dict",
        "List": "from typing import List",
        "Optional": "from typing import Optional",
        "Any": "from typing import Any",
        "Union": "from typing import Union",
        "Callable": "from typing import Callable",
        "dataclass": "from dataclasses import dataclass",
        "field": "from dataclasses import field",
        "pytest": "import pytest",
        "MagicMock": "from unittest.mock import MagicMock",
        "patch": "from unittest.mock import patch",
    }

    if undefined_name not in known_imports:
        return HealResult(
            success=False,
            failure=failure,
            error=f"Unknown name {undefined_name}, cannot auto-import",
        )

    if not failure.file_path:
        return HealResult(
            success=False,
            failure=failure,
            error="No file path specified",
        )

    try:
        file_path = project_root / failure.file_path
        content = file_path.read_text()

        import_line = known_imports[undefined_name]

        # Check if already imported
        if import_line in content or f"import {undefined_name}" in content:
            return HealResult(
                success=False,
                failure=failure,
                error=f"{undefined_name} appears to already be imported",
            )

        # Add import after existing imports or at top
        lines = content.split("\n")
        insert_idx = 0

        for i, line in enumerate(lines):
            if line.startswith("import ") or line.startswith("from "):
                insert_idx = i + 1
            elif line.strip() and not line.startswith("#") and insert_idx > 0:
                break

        lines.insert(insert_idx, import_line)
        file_path.write_text("\n".join(lines))

        return HealResult(
            success=True,
            failure=failure,
            fix_applied=f"Added '{import_line}' to {failure.file_path}",
        )

    except Exception as e:
        return HealResult(
            success=False,
            failure=failure,
            error=str(e),
        )


def _fix_collection_error(failure: CIFailure, project_root: Path) -> HealResult:
    """Fix pytest collection errors (usually syntax errors)."""
    # Collection errors usually require manual intervention
    return HealResult(
        success=False,
        failure=failure,
        error="Collection errors require manual fix",
    )


# Map error types to fix functions
FIX_FUNCTIONS: dict[str, Callable[[CIFailure, Path], HealResult]] = {
    "missing_module": _fix_missing_module,
    "missing_file": _fix_missing_file,
    "lint_error": _fix_lint_error,
    "format_error": _fix_format_error,
    "undefined_name": _fix_undefined_name,
    "collection_error": _fix_collection_error,
}


def attempt_heal(
    pr_number: int,
    branch: str,
    project_root: Path,
    company_dir: Path,
) -> dict:
    """
    Attempt to heal CI failures for a PR.

    Args:
        pr_number: PR number
        branch: Branch name
        project_root: Path to project root
        company_dir: Path to .company directory

    Returns:
        dict with heal results
    """
    result = {
        "pr_number": pr_number,
        "branch": branch,
        "failures_found": 0,
        "fixes_attempted": 0,
        "fixes_successful": 0,
        "commit_sha": None,
        "errors": [],
    }

    # Check heal attempt count
    state_file = company_dir / "state" / "ci_heal_state.json"
    state = _load_heal_state(state_file)

    pr_key = str(pr_number)
    if state.get("attempts", {}).get(pr_key, 0) >= 3:
        result["errors"].append(f"Max heal attempts (3) reached for PR #{pr_number}")
        return result

    # Fetch CI logs
    try:
        log_result = subprocess.run(
            ["gh", "run", "view", "--log-failed"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if log_result.returncode != 0:
            result["errors"].append("Could not fetch CI logs")
            return result

        log_output = log_result.stdout
    except Exception as e:
        result["errors"].append(f"Failed to fetch logs: {e}")
        return result

    # Parse failures
    failures = parse_ci_failures(log_output, pr_number, branch)
    result["failures_found"] = len(failures)

    if not failures:
        return result

    # Checkout the branch
    try:
        subprocess.run(
            ["git", "fetch", "origin", branch],
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "checkout", branch],
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        result["errors"].append(f"Failed to checkout branch: {e}")
        return result

    # Attempt fixes
    successful_fixes = []
    for failure in failures:
        fix_func = FIX_FUNCTIONS.get(failure.error_type)
        if not fix_func:
            continue

        result["fixes_attempted"] += 1
        heal_result = fix_func(failure, project_root)

        if heal_result.success:
            result["fixes_successful"] += 1
            successful_fixes.append(heal_result.fix_applied)
        else:
            result["errors"].append(
                f"{failure.error_type}: {heal_result.error or 'Unknown error'}"
            )

    # Commit and push if we made fixes
    if successful_fixes:
        try:
            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True,
                timeout=30,
            )

            # Commit
            commit_msg = "fix(ci): Auto-heal CI failures [WS-066-004]\n\n"
            commit_msg += "Fixes applied:\n"
            for fix in successful_fixes:
                commit_msg += f"- {fix}\n"
            commit_msg += "\nCo-Authored-By: CI Healer <noreply@forge.dev>"

            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if commit_result.returncode == 0:
                # Get commit SHA
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                result["commit_sha"] = sha_result.stdout.strip()

                # Push
                push_result = subprocess.run(
                    ["git", "push", "origin", branch],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                if push_result.returncode != 0:
                    result["errors"].append(f"Push failed: {push_result.stderr[:200]}")
                else:
                    # The head moved: the verdict for the old head no longer
                    # answers for it, and a native auto-merge armed on the old
                    # head would merge this unjudged one (#581).
                    result["verdict_hold"] = _hold_for_new_head(pr_number)

        except Exception as e:
            result["errors"].append(f"Commit/push failed: {e}")

    # Update heal state
    _update_heal_state(state_file, pr_number)

    # Return to main
    subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=30)

    return result


def _hold_for_new_head(pr_number: int) -> bool:
    """Disarm native auto-merge and re-apply verdict-pending (best-effort)."""
    try:
        try:
            from . import verdict_hold as vh
        except ImportError:
            import verdict_hold as vh  # type: ignore[no-redef]
        return vh.hold_for_new_head(pr_number)
    except Exception as exc:  # noqa: BLE001 — ci.yml's hold job also catches the push
        logger.warning(
            "verdict hold after heal push failed for #%s: %s", pr_number, exc
        )
        return False


def _load_heal_state(state_file: Path) -> dict:
    """Load heal state from file."""
    if not state_file.exists():
        return {"attempts": {}, "heals_today": 0, "last_date": ""}

    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {"attempts": {}, "heals_today": 0, "last_date": ""}


def _update_heal_state(state_file: Path, pr_number: int) -> None:
    """Update heal state after an attempt."""
    state_file.parent.mkdir(parents=True, exist_ok=True)

    state = _load_heal_state(state_file)

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_date") != today:
        state["heals_today"] = 0
        state["last_date"] = today

    pr_key = str(pr_number)
    state.setdefault("attempts", {})
    state["attempts"][pr_key] = state["attempts"].get(pr_key, 0) + 1
    state["heals_today"] = state.get("heals_today", 0) + 1

    state_file.write_text(json.dumps(state, indent=2))


def check_heal_rate_limit(company_dir: Path, max_per_day: int = 10) -> bool:
    """Check if we've hit the daily heal rate limit."""
    state_file = company_dir / "state" / "ci_heal_state.json"
    state = _load_heal_state(state_file)

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_date") != today:
        return True  # New day, reset

    return state.get("heals_today", 0) < max_per_day


def log_heal_activity(
    pr_number: int,
    result: dict,
    company_dir: Path,
) -> None:
    """Log heal activity to activity.jsonl."""
    activity_file = company_dir / "state" / "activity.jsonl"
    activity_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "event_type": "ci_heal_attempt",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pr_number": pr_number,
        "failures_found": result.get("failures_found", 0),
        "fixes_attempted": result.get("fixes_attempted", 0),
        "fixes_successful": result.get("fixes_successful", 0),
        "commit_sha": result.get("commit_sha"),
        "errors": result.get("errors", []),
    }

    with open(activity_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CI Healer")
    parser.add_argument("--pr", type=int, help="PR number to heal")
    parser.add_argument("--branch", type=str, help="Branch name")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no fixes")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    company_dir = project_root / ".company"

    if args.pr and args.branch:
        if args.dry_run:
            # Just show what would be fixed
            log_result = subprocess.run(
                ["gh", "run", "view", "--log-failed"],
                capture_output=True,
                text=True,
            )
            if log_result.returncode == 0:
                failures = parse_ci_failures(log_result.stdout, args.pr, args.branch)
                print(f"Found {len(failures)} healable failures:")
                for f in failures:
                    print(f"  - {f.error_type}: {f.suggested_fix}")
        else:
            result = attempt_heal(args.pr, args.branch, project_root, company_dir)
            print(f"Heal result: {result}")
    else:
        print("Usage: ci_healer.py --pr <number> --branch <name> [--dry-run]")
