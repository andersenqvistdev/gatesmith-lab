#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-113-002: Cross-Employee Pattern Propagation System

Automatically propagates successful patterns to employees with matching capabilities.
When a pattern is learned from recovery or task success, this module identifies
relevant employees and injects the pattern into their memory files.

This creates a knowledge spreading network where successful approaches
automatically become available to all employees who might benefit.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pattern categories mapped to employee capabilities
PATTERN_CAPABILITY_MAP: dict[str, list[str]] = {
    "test_failure": ["testing", "pytest", "quality-assurance", "test-automation"],
    "syntax_error": ["python", "testing", "code-review"],
    "environment_error": ["shell-scripting", "installation", "distribution"],
    "pr_workflow": ["architecture", "code-review", "testing"],
    "timeout": ["python", "backend", "performance"],
    "wrong_approach": ["architecture", "design-decisions", "roadmap"],
    "success_misdetected": ["testing", "quality-assurance"],
    "uncommitted_work": ["python", "testing", "code-review"],
}

# Strategies that indicate successful approaches worth sharing
SHAREABLE_STRATEGIES = [
    "retry_with_fix_hint",
    "retry_simplified",
    "retry_fresh_worktree",
    "fix_environment",
    "retry_with_replan",
]


def get_company_dir(start_path: Path | None = None) -> Path:
    """Get company directory (import-free for standalone use)."""
    if start_path is None:
        start_path = Path.cwd()

    # Try to import company_resolver
    try:
        from . import company_resolver

        return company_resolver.get_company_dir(start_path)
    except ImportError:
        try:
            import company_resolver

            return company_resolver.get_company_dir(start_path)
        except ImportError:
            pass

    # Fallback: search upward
    current = start_path.resolve()
    while current != current.parent:
        if (current / ".company").exists():
            return current / ".company"
        current = current.parent
    return start_path / ".company"


def load_org_employees(company_dir: Path) -> list[dict]:
    """Load employee list from org.json."""
    org_path = company_dir / "org.json"
    if not org_path.exists():
        return []

    try:
        with open(org_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("employees", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load org.json: {e}")
        return []


def load_recovery_patterns(company_dir: Path) -> dict[str, Any]:
    """Load recovery patterns from state file."""
    patterns_path = company_dir / "state" / "recovery_patterns.json"
    if not patterns_path.exists():
        return {"patterns": {}}

    try:
        with open(patterns_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load recovery patterns: {e}")
        return {"patterns": {}}


def find_employees_for_pattern(
    failure_type: str,
    employees: list[dict],
) -> list[dict]:
    """Find employees whose capabilities match the pattern type."""
    relevant_caps = PATTERN_CAPABILITY_MAP.get(failure_type, [])
    if not relevant_caps:
        return []

    matched = []
    for emp in employees:
        emp_caps = set(emp.get("capabilities", []))
        if emp_caps & set(relevant_caps):
            matched.append(emp)
    return matched


def format_pattern_entry(
    failure_type: str,
    strategy: str,
    success_rate: float,
    attempts: int,
) -> str:
    """Format a pattern as markdown for injection into memory."""
    strategy_display = strategy.replace("_", " ").title()
    failure_display = failure_type.replace("_", " ").title()

    return f"""
### Pattern: {failure_display} → {strategy_display}
- **Success Rate:** {success_rate:.0%} ({attempts} attempts)
- **When to Use:** When encountering {failure_display.lower()} errors
- **Approach:** {_get_strategy_description(strategy)}
- **Source:** Auto-propagated from failure recovery (WS-113)
"""


def _get_strategy_description(strategy: str) -> str:
    """Get human-readable description of strategy."""
    descriptions = {
        "retry_with_fix_hint": "Re-attempt with specific guidance about what failed",
        "retry_simplified": "Reduce complexity, focus on core deliverable only",
        "retry_fresh_worktree": "Start fresh in isolated environment to avoid state pollution",
        "fix_environment": "Address environment/setup issues before retrying task",
        "retry_with_replan": "Re-plan the approach before attempting again",
    }
    return descriptions.get(strategy, "Apply learned recovery approach")


def append_pattern_to_memory(
    employee_id: str,
    pattern_entry: str,
    company_dir: Path,
) -> bool:
    """Append a pattern to employee's memory file."""
    memory_path = company_dir / "agents" / employee_id / "memory.md"

    if not memory_path.exists():
        logger.debug(f"No memory file for {employee_id}, skipping")
        return False

    try:
        content = memory_path.read_text(encoding="utf-8")

        # Check if pattern section exists
        pattern_header = "## Propagated Patterns"
        if pattern_header not in content:
            # Add section before the first --- or at end
            insert_point = content.find("\n---\n")
            if insert_point == -1:
                content += f"\n\n{pattern_header}\n\n> Auto-injected successful patterns from across the organization.\n"
            else:
                # Insert after first metadata block
                content = (
                    content[: insert_point + 5]
                    + f"\n{pattern_header}\n\n> Auto-injected successful patterns from across the organization.\n\n---\n"
                    + content[insert_point + 5 :]
                )

        # Check if this specific pattern already exists (avoid duplicates)
        pattern_id = pattern_entry.split("\n")[
            1
        ].strip()  # Get the "### Pattern: ..." line
        if pattern_id in content:
            logger.debug(f"Pattern already in {employee_id}'s memory, skipping")
            return False

        # Append pattern after the Propagated Patterns header
        pattern_section_idx = content.find(pattern_header)
        if pattern_section_idx != -1:
            # Find the end of the section header line
            header_end = content.find("\n", pattern_section_idx)
            if header_end != -1:
                # Find the next section or end of file
                next_section = content.find("\n## ", header_end)
                if next_section == -1:
                    # Append at end
                    content = content.rstrip() + pattern_entry + "\n"
                else:
                    # Insert before next section
                    content = (
                        content[:next_section] + pattern_entry + content[next_section:]
                    )

        # Atomic write
        fd, tmp = tempfile.mkstemp(
            dir=str(memory_path.parent), prefix=".mem_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(memory_path))
            return True
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    except Exception as e:
        logger.warning(f"Failed to update memory for {employee_id}: {e}")
        return False


def propagate_patterns(
    company_dir: Path | None = None,
    min_success_rate: float = 0.7,
    min_attempts: int = 3,
) -> dict[str, Any]:
    """
    Main entry point: propagate successful patterns to relevant employees.

    Args:
        company_dir: Company directory path
        min_success_rate: Minimum success rate to consider pattern worth sharing
        min_attempts: Minimum attempts before pattern is considered validated

    Returns:
        Dict with propagation results
    """
    if company_dir is None:
        company_dir = get_company_dir()

    results = {
        "patterns_found": 0,
        "patterns_propagated": 0,
        "employees_updated": set(),
        "details": [],
    }

    # Load patterns and employees
    patterns_data = load_recovery_patterns(company_dir)
    employees = load_org_employees(company_dir)

    if not employees:
        logger.info("No employees found, skipping propagation")
        return results

    patterns = patterns_data.get("patterns", {})
    results["patterns_found"] = len(patterns)

    for pattern_key, stats in patterns.items():
        # Parse pattern key (format: "failure_type:strategy")
        if ":" not in pattern_key:
            continue
        failure_type, strategy = pattern_key.split(":", 1)

        # Skip non-shareable strategies
        if strategy not in SHAREABLE_STRATEGIES:
            continue

        attempts = stats.get("attempts", 0)
        success_rate = stats.get("success_rate", 0.0)

        # Skip if not enough evidence
        if attempts < min_attempts or success_rate < min_success_rate:
            continue

        # Find relevant employees
        relevant_employees = find_employees_for_pattern(failure_type, employees)

        if not relevant_employees:
            continue

        # Format the pattern entry
        pattern_entry = format_pattern_entry(
            failure_type, strategy, success_rate, attempts
        )

        # Propagate to each relevant employee
        for emp in relevant_employees:
            emp_id = emp.get("id", "")
            if not emp_id:
                continue

            if append_pattern_to_memory(emp_id, pattern_entry, company_dir):
                results["patterns_propagated"] += 1
                results["employees_updated"].add(emp_id)
                results["details"].append(
                    {
                        "pattern": pattern_key,
                        "employee": emp_id,
                        "success_rate": success_rate,
                    }
                )

    # Convert set to list for JSON serialization
    results["employees_updated"] = list(results["employees_updated"])

    logger.info(
        f"Pattern propagation complete: {results['patterns_propagated']} patterns "
        f"propagated to {len(results['employees_updated'])} employees"
    )

    return results


def propagate_single_pattern(
    failure_type: str,
    strategy: str,
    success_rate: float,
    attempts: int,
    company_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Propagate a single pattern immediately (called after successful recovery).

    This is the real-time propagation hook that can be called from
    failure_recovery.py when a recovery succeeds.
    """
    if company_dir is None:
        company_dir = get_company_dir()

    results = {
        "propagated": False,
        "employees_updated": [],
    }

    if strategy not in SHAREABLE_STRATEGIES:
        return results

    if success_rate < 0.5 or attempts < 2:
        return results

    employees = load_org_employees(company_dir)
    relevant = find_employees_for_pattern(failure_type, employees)

    pattern_entry = format_pattern_entry(failure_type, strategy, success_rate, attempts)

    for emp in relevant:
        emp_id = emp.get("id", "")
        if emp_id and append_pattern_to_memory(emp_id, pattern_entry, company_dir):
            results["employees_updated"].append(emp_id)

    results["propagated"] = len(results["employees_updated"]) > 0
    return results


# CLI for testing/manual execution
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1 and sys.argv[1] == "propagate":
        company_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
        results = propagate_patterns(company_dir)
        print(json.dumps(results, indent=2))
    else:
        print("Usage: python pattern_propagator.py propagate [company_dir]")
        print("\nPropagates successful recovery patterns to relevant employees.")
