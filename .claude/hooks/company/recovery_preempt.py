# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Preemptive recovery — inject failure avoidance hints before task execution.

Scans historical failure patterns from recovery_patterns.json and task
results to identify recurring failure modes, then generates avoidance
hints that are injected into task descriptions before execution.

Public API:
    scan_failure_patterns(company_dir) -> list[PreemptHint]
    generate_avoidance_hint(pattern) -> str
    inject_hints_into_task(task, company_dir) -> dict
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

# Minimum occurrences of a failure pattern before generating a hint
_MIN_OCCURRENCES = 2

# Known failure types and their avoidance guidance templates
_AVOIDANCE_TEMPLATES: dict[str, str] = {
    "test_failure": (
        "Tests have failed {attempts} times in similar tasks. "
        "Run tests early and fix failures before committing. "
        "Check for flaky tests and ensure test isolation."
    ),
    "uncommitted_work": (
        "Previous tasks left uncommitted changes {attempts} times. "
        "Commit work incrementally — don't accumulate large uncommitted diffs."
    ),
    "wrong_approach": (
        "Tasks like this have used the wrong approach {attempts} times. "
        "Read existing code thoroughly before making changes. "
        "Verify your approach matches the codebase patterns."
    ),
    "timeout": (
        "Similar tasks have timed out {attempts} times. "
        "Break work into smaller steps. Avoid long-running operations. "
        "Set explicit timeouts on subprocess calls."
    ),
    "environment_error": (
        "Environment errors occurred {attempts} times in similar tasks. "
        "Verify imports and dependencies exist before using them. "
        "Check sys.path includes the hooks directory."
    ),
    "pr_workflow": (
        "PR workflow issues occurred {attempts} times. "
        "Always create a branch before committing. Push with -u flag. "
        "Use 'gh pr create' after pushing."
    ),
    "syntax_error": (
        "Syntax errors occurred {attempts} times. "
        "Run the linter before committing. Check for missing imports, "
        "unclosed brackets, and indentation issues."
    ),
    "success_misdetected": (
        "Task completion was incorrectly detected {attempts} times. "
        "Verify the actual outcome — check that files were created/modified, "
        "tests pass, and the PR was actually created."
    ),
    "flapping": (
        "Flapping (alternating pass/fail) detected {attempts} times. "
        "This suggests a race condition or non-deterministic test. "
        "Add explicit waits or locks where needed."
    ),
}


@dataclass
class PreemptHint:
    """A failure avoidance hint derived from historical patterns."""

    failure_type: str
    hint: str
    confidence: float  # 0.0 to 1.0
    source_pattern: str  # e.g. "test_failure:retry_with_fix_hint"
    occurrences: int  # how many times this pattern was seen


# ---------------------------------------------------------------------------
# Pattern scanning
# ---------------------------------------------------------------------------


def _safe_load_json(path: Path) -> dict:
    """Load JSON, returning empty dict on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _scan_result_failures(company_dir: Path) -> dict[str, int]:
    """Scan result files for recurring failure types.

    Returns a dict mapping failure_type to occurrence count.
    """
    results_dir = company_dir / "results"
    if not results_dir.is_dir():
        return {}

    failure_counts: dict[str, int] = {}
    for result_file in results_dir.glob("*.json"):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            status = data.get("status", "")
            if status in ("failed", "error"):
                error = data.get("error", "") or data.get("error_message", "")
                ftype = _classify_error(error)
                failure_counts[ftype] = failure_counts.get(ftype, 0) + 1
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    return failure_counts


def _classify_error(error_msg: str) -> str:
    """Classify an error message into a failure type."""
    lower = error_msg.lower()

    if any(kw in lower for kw in ("import", "modulenotfound", "no module")):
        return "environment_error"
    if any(kw in lower for kw in ("test", "assert", "pytest", "failed test")):
        return "test_failure"
    if any(kw in lower for kw in ("syntax", "indent", "unexpected token")):
        return "syntax_error"
    if any(kw in lower for kw in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(kw in lower for kw in ("uncommitted", "dirty", "unstaged")):
        return "uncommitted_work"
    if any(kw in lower for kw in ("push", "pr ", "pull request", "branch")):
        return "pr_workflow"

    return "unknown"


def scan_failure_patterns(company_dir: str | Path) -> list[PreemptHint]:
    """Analyze recovery patterns and results for recurring failure modes.

    Only generates hints for patterns with >= _MIN_OCCURRENCES occurrences.

    Args:
        company_dir: Path to the .company directory.

    Returns:
        List of PreemptHint objects, sorted by confidence (highest first).
    """
    company = Path(company_dir)

    # Source 1: recovery_patterns.json (structured failure→strategy data)
    recovery_data = _safe_load_json(company / "state" / "recovery_patterns.json")
    patterns = recovery_data.get("patterns", {})

    # Aggregate by failure type
    failure_stats: dict[str, dict] = {}
    for key, entry in patterns.items():
        parts = key.split(":")
        ftype = parts[0] if parts else "unknown"
        attempts = entry.get("attempts", 0)
        successes = entry.get("successes", 0)

        if ftype not in failure_stats:
            failure_stats[ftype] = {"attempts": 0, "successes": 0, "patterns": []}
        failure_stats[ftype]["attempts"] += attempts
        failure_stats[ftype]["successes"] += successes
        failure_stats[ftype]["patterns"].append(key)

    # Source 2: result files for additional signal
    result_failures = _scan_result_failures(company)
    for ftype, count in result_failures.items():
        if ftype not in failure_stats:
            failure_stats[ftype] = {"attempts": count, "successes": 0, "patterns": []}
        else:
            # Don't double-count, but boost the signal
            failure_stats[ftype]["attempts"] = max(
                failure_stats[ftype]["attempts"], count
            )

    # Generate hints for recurring patterns
    hints: list[PreemptHint] = []
    for ftype, stats in failure_stats.items():
        total = stats["attempts"]
        if total < _MIN_OCCURRENCES:
            continue
        if ftype == "unknown":
            continue

        # Confidence: higher failure rate → higher confidence hint is needed
        failure_rate = 1.0 - (stats["successes"] / total if total > 0 else 0)
        confidence = min(1.0, failure_rate * (total / 10))  # scale with volume

        hint_text = generate_avoidance_hint({"failure_type": ftype, "attempts": total})

        hints.append(
            PreemptHint(
                failure_type=ftype,
                hint=hint_text,
                confidence=confidence,
                source_pattern=", ".join(stats["patterns"][:3]),
                occurrences=total,
            )
        )

    # Sort by confidence descending
    hints.sort(key=lambda h: h.confidence, reverse=True)
    return hints


# ---------------------------------------------------------------------------
# Hint generation
# ---------------------------------------------------------------------------


def generate_avoidance_hint(pattern: dict) -> str:
    """Turn a failure pattern dict into a concrete avoidance hint string.

    Args:
        pattern: Dict with at least 'failure_type' and 'attempts' keys.

    Returns:
        A human-readable avoidance hint string.
    """
    ftype = pattern.get("failure_type", "unknown")
    attempts = pattern.get("attempts", 0)

    template = _AVOIDANCE_TEMPLATES.get(ftype)
    if template:
        return template.format(attempts=attempts)

    return (
        f"A recurring issue of type '{ftype}' has occurred {attempts} times. "
        f"Review previous failures and take extra care in this area."
    )


# ---------------------------------------------------------------------------
# Task injection
# ---------------------------------------------------------------------------


def inject_hints_into_task(task: dict, company_dir: str | Path) -> dict:
    """Match a task against known failure patterns and inject avoidance hints.

    Modifies the task dict in-place by adding a 'preempt_hints' field
    containing relevant avoidance hints.

    Args:
        task: Task dict from the work queue.
        company_dir: Path to the .company directory.

    Returns:
        The task dict (same reference, modified in-place).
    """
    hints = scan_failure_patterns(company_dir)
    if not hints:
        return task

    # Filter hints relevant to this task
    task_desc = (task.get("description", "") + " " + task.get("title", "")).lower()
    task_caps = [c.lower() for c in task.get("required_capabilities", [])]
    task_tags = [t.lower() for t in task.get("tags", [])]

    relevant: list[str] = []
    for hint in hints:
        # Include high-confidence hints regardless
        if hint.confidence >= 0.8:
            relevant.append(hint.hint)
            continue

        # Match by keyword overlap
        ftype_words = hint.failure_type.replace("_", " ").split()
        if any(w in task_desc for w in ftype_words):
            relevant.append(hint.hint)
            continue

        # Match by capability/tag overlap
        if any(w in " ".join(task_caps + task_tags) for w in ftype_words):
            relevant.append(hint.hint)

    if relevant:
        # Cap at 3 hints to avoid prompt bloat
        task["preempt_hints"] = relevant[:3]

    return task
