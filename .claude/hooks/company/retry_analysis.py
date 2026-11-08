#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Task retry pattern analysis — identify friction and suggest improvements.

Analyzes work_queue.json to find:
1. Tasks with high retry counts (bottleneck indicator)
2. Common error patterns across retry chains
3. Tasks that recovered vs. those that were terminal
4. Environmental factors (network, timeouts, resource issues)

Usage:
    python retry_analysis.py [--json] [--threshold 3]
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / ".company"
QUEUE_FILE = COMPANY_DIR / "state" / "work_queue.json"


def _ts(val: str | None) -> datetime | None:
    """Parse ISO timestamp, return None on failure."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _ts_key(task: dict) -> datetime:
    """Return a sortable timestamp for a task; epoch if no timestamp present."""
    return _ts(
        task.get("updated_at") or task.get("created_at")
    ) or datetime.min.replace(tzinfo=timezone.utc)


def _extract_error_category(error_text: str) -> str:
    """Categorize an error from failure_output or last_error."""
    if not error_text:
        return "unknown"
    error_lower = error_text.lower()
    if "connection" in error_lower or "network" in error_lower:
        return "network"
    if "timeout" in error_lower or "timed out" in error_lower:
        return "timeout"
    if (
        "rate limit" in error_lower
        or "api limit" in error_lower
        or "quota exceeded" in error_lower
        or "too many requests" in error_lower
    ):
        return "api_limit"
    if "permission" in error_lower or "denied" in error_lower:
        return "permission"
    if "import" in error_lower or "module" in error_lower:
        return "import"
    if "syntax" in error_lower or "parse" in error_lower:
        return "syntax"
    if "file not found" in error_lower or "no such file" in error_lower:
        return "missing_file"
    if (
        "merge conflict" in error_lower
        or "git push" in error_lower
        or "git conflict" in error_lower
        or "push failed" in error_lower
    ):
        return "git_error"
    if "memory" in error_lower or "out of" in error_lower:
        return "resource"
    if "assertion" in error_lower or "assert" in error_lower:
        return "assertion"
    if "attribute" in error_lower or "typeerror" in error_lower:
        return "type_error"
    if "test" in error_lower or "fail" in error_lower:
        return "test_failure"
    return "other"


def load_work_queue() -> dict[str, Any]:
    """Load work_queue.json; return empty dict if missing."""
    if not QUEUE_FILE.exists():
        return {}
    try:
        with open(QUEUE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def analyze_retries(threshold: int = 3) -> dict[str, Any]:
    """
    Analyze retry patterns across all tasks in the queue.

    Returns a dict with:
    - high_retry_tasks: tasks that exceeded retry threshold
    - error_pattern_analysis: common error categories
    - recovery_analysis: which errors were recovered vs terminal
    - recommendations: actionable suggestions
    """
    queue = load_work_queue()
    all_tasks = []

    for status in ("pending", "in_progress", "completed", "failed", "blocked"):
        for task in queue.get(status, []) or []:
            if isinstance(task, dict) and task.get("task_id"):
                all_tasks.append(task)

    # Group by task_id, keeping the most-recently-updated copy of each task.
    # Use _ts_key() so tasks without timestamps compare as epoch (never crash on None > None).
    tasks_by_id: dict[str, dict] = {}
    for task in all_tasks:
        tid = task.get("task_id")
        if tid not in tasks_by_id or _ts_key(task) > _ts_key(tasks_by_id[tid]):
            tasks_by_id[tid] = task

    high_retry_tasks = [
        t for t in tasks_by_id.values() if t.get("retry_count", 0) >= threshold
    ]
    high_retry_tasks.sort(key=lambda t: t.get("retry_count", 0), reverse=True)

    # Analyze error patterns
    error_patterns: Counter = Counter()
    error_by_category: dict[str, list[str]] = defaultdict(list)

    for task in high_retry_tasks:
        error = task.get("last_error", "")
        if error:
            category = _extract_error_category(error)
            error_patterns[category] += 1
            error_by_category[category].append(
                f"{task.get('task_id', 'unknown')}({task.get('retry_count', 0)} retries)"
            )

    # Recovery analysis: tasks that succeeded despite retries vs those still failing
    recovered = [t for t in high_retry_tasks if t.get("status") == "completed"]
    terminal = [t for t in high_retry_tasks if t.get("status") in ("failed", "blocked")]

    # Analyze retry history if available
    retry_reasons: Counter = Counter()
    for task in high_retry_tasks:
        history = task.get("retry_history", [])
        for entry in history:
            if isinstance(entry, dict):
                reason = entry.get("reason", "unknown")
                retry_reasons[reason] += 1

    # Extract constraints/patterns from failure contexts
    constraint_patterns: Counter = Counter()
    for task in high_retry_tasks:
        # Check if task has failure context with constraints
        for entry in task.get("retry_history", []):
            if isinstance(entry, dict):
                # Some entries might have a replan_context
                ctx = entry.get("replan_context", {})
                if isinstance(ctx, dict):
                    constraints = ctx.get("constraints", [])
                    for c in constraints:
                        constraint_patterns[c[:50]] += 1  # Truncate for readability

    # Build recommendations
    recommendations = []

    # Recommend on top error patterns
    top_errors = error_patterns.most_common(3)
    if top_errors:
        for error_cat, count in top_errors:
            if error_cat == "timeout":
                recommendations.append(
                    {
                        "priority": "high",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "Consider breaking tasks into smaller steps or increasing timeout thresholds. Timeouts indicate tasks may be too complex for single execution window.",
                        "action": "Review affected tasks and implement step-by-step decomposition",
                    }
                )
            elif error_cat == "network":
                recommendations.append(
                    {
                        "priority": "high",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "Network errors suggest dependency fetching or API call failures. Add exponential backoff and circuit breakers.",
                        "action": "Implement retry logic with backoff in daemon worker",
                    }
                )
            elif error_cat == "permission":
                recommendations.append(
                    {
                        "priority": "medium",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "Permission errors indicate file/resource access issues. Ensure worker processes have correct permissions.",
                        "action": "Audit file permissions and worker environment setup",
                    }
                )
            elif error_cat == "test_failure":
                recommendations.append(
                    {
                        "priority": "high",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "Test failures that retry indicate flaky tests or race conditions. Add test isolation and fix root causes.",
                        "action": "Analyze failing test patterns and add determinism guards",
                    }
                )
            elif error_cat == "api_limit":
                recommendations.append(
                    {
                        "priority": "high",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "API rate limit errors indicate too many concurrent requests or insufficient backoff. Reduce concurrency or add exponential backoff with jitter.",
                        "action": "Add rate-limit aware retry with exponential backoff in daemon worker",
                    }
                )
            elif error_cat == "git_error":
                recommendations.append(
                    {
                        "priority": "medium",
                        "category": error_cat,
                        "count": count,
                        "suggestion": "Git errors (push failures, merge conflicts) indicate branch divergence or concurrent writes. Ensure tasks rebase before push and avoid parallel writes to the same branch.",
                        "action": "Add pre-push rebase step and serialize concurrent PR creation",
                    }
                )

    # Add recovery-based recommendations
    if terminal:
        terminal_pct = round(len(terminal) / len(high_retry_tasks) * 100)
        recommendations.append(
            {
                "priority": "critical",
                "category": "terminal_failures",
                "count": len(terminal),
                "percentage": terminal_pct,
                "suggestion": f"{terminal_pct}% of high-retry tasks are terminal (never recovered). These need escalation or fundamental rework.",
                "action": "Review terminal-failure tasks for fundamental issues vs retryable errors",
            }
        )

    if recovered:
        recovery_pct = round(len(recovered) / len(high_retry_tasks) * 100)
        recommendations.append(
            {
                "priority": "info",
                "category": "recovered_tasks",
                "count": len(recovered),
                "percentage": recovery_pct,
                "suggestion": f"{recovery_pct}% of high-retry tasks eventually succeeded. This indicates environmental transience.",
                "action": "Investigate what changed between failure and recovery (timing, resources, external state)",
            }
        )

    return {
        "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "summary": {
            "total_tasks_analyzed": len(tasks_by_id),
            "high_retry_tasks": len(high_retry_tasks),
            "max_retry_count": max(
                (t.get("retry_count", 0) for t in high_retry_tasks), default=0
            ),
            "avg_retry_count": (
                round(
                    sum(t.get("retry_count", 0) for t in high_retry_tasks)
                    / len(high_retry_tasks),
                    1,
                )
                if high_retry_tasks
                else 0
            ),
            "recovered_count": len(recovered),
            "terminal_count": len(terminal),
        },
        "top_error_patterns": dict(error_patterns.most_common(10)),
        "error_details": {
            category: tasks[:5]  # Top 5 tasks per category
            for category, tasks in error_by_category.items()
        },
        "retry_reasons": dict(retry_reasons.most_common(10)),
        "constraint_patterns": dict(constraint_patterns.most_common(10)),
        "high_retry_tasks": [
            {
                "task_id": t.get("task_id"),
                "title": t.get("title", "")[:80],
                "retry_count": t.get("retry_count", 0),
                "status": t.get("status"),
                "last_error": (t.get("last_error") or "")[:150],
                "error_category": _extract_error_category(t.get("last_error", "")),
            }
            for t in high_retry_tasks[:20]
        ],
        "recommendations": recommendations,
    }


def print_human_report(report: dict) -> None:
    """Pretty-print the retry analysis report."""
    s = report["summary"]
    print(f"\n{'=' * 70}")
    print("  TASK RETRY ANALYSIS REPORT")
    print(f"  Threshold: {report['threshold']}+ retries")
    print(f"{'=' * 70}\n")

    print("SUMMARY:")
    print(f"  Total tasks analyzed      : {s['total_tasks_analyzed']}")
    print(f"  High-retry tasks          : {s['high_retry_tasks']}")
    print(f"  Max retry count           : {s['max_retry_count']}")
    print(f"  Average retry count       : {s['avg_retry_count']}")
    print(
        f"  Recovered (eventually OK) : {s['recovered_count']} ({round(s['recovered_count'] / s['high_retry_tasks'] * 100) if s['high_retry_tasks'] else 0}%)"
    )
    print(
        f"  Terminal (never recovered): {s['terminal_count']} ({round(s['terminal_count'] / s['high_retry_tasks'] * 100) if s['high_retry_tasks'] else 0}%)"
    )

    print(f"\n{'=' * 70}")
    print("TOP ERROR PATTERNS:")
    for error_type, count in report["top_error_patterns"].items():
        print(f"  {error_type:25s} : {count:3d} tasks")

    print(f"\n{'=' * 70}")
    print("AFFECTED TASKS (showing top 20):")
    for task in report["high_retry_tasks"]:
        print(f"\n  [{task['task_id']}]")
        print(f"    Status       : {task['status']}")
        print(f"    Retries      : {task['retry_count']}")
        print(f"    Error type   : {task['error_category']}")
        print(f"    Title        : {task['title']}")
        if task["last_error"]:
            print(f"    Last error   : {task['last_error']}")

    print(f"\n{'=' * 70}")
    print(f"RECOMMENDATIONS ({len(report['recommendations'])} total):")
    for i, rec in enumerate(report["recommendations"], 1):
        priority = rec.get("priority", "").upper()
        category = rec.get("category", "")
        count = rec.get("count", 0)
        pct = rec.get("percentage")
        pct_str = f" ({pct}%)" if pct else ""
        print(f"\n  {i}. [{priority}] {category} ({count} tasks{pct_str})")
        print(f"     → {rec.get('suggestion', '')}")
        print(f"     ACTION: {rec.get('action', '')}")

    print(f"\n{'=' * 70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze task retry patterns and suggest improvements"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of human-readable report",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Minimum retry count to analyze (default 3)",
    )
    args = parser.parse_args()

    report = analyze_retries(threshold=args.threshold)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human_report(report)


if __name__ == "__main__":
    main()
