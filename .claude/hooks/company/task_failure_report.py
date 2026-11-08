#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Task failure rate reporting — P93 instrumentation.

Reads the last 50 task attempts from work_queue.json and task_failure_log.jsonl,
categorises them by failure mode, and outputs a structured summary report.

Usage:
    python task_failure_report.py [--json] [--n 50]
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / ".company"
QUEUE_FILE = COMPANY_DIR / "state" / "work_queue.json"
FAILURE_LOG = COMPANY_DIR / "state" / "task_failure_log.jsonl"

# Map raw diagnosis categories → display buckets
CATEGORY_BUCKETS: dict[str, str] = {
    "timeout": "timeout",
    "subprocess_error": "timeout",  # exit_code=-1 often means timeout
    "process_killed": "timeout",
    "oom_killed": "timeout",
    "success_detection_failure": "p51_short_output",
    "pr_test_failure": "test_failure",
    "test_failure": "test_failure",
    "pr_failure": "pr_failure",
    "pr_commit_error": "pr_failure",
    "git_push_error": "pr_failure",
    "git_conflict": "pr_failure",
    "import_error": "code_error",
    "syntax_error": "code_error",
    "path_error": "code_error",
    "command_not_found": "code_error",
    "permission_error": "code_error",
    "api_limit": "api_limit",
    "network_error": "network_error",
    "resource_error": "resource_error",
    "unknown": "unknown",
}


def _ts(val: str | None) -> datetime | None:
    """Parse ISO timestamp, return None on failure."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _bucket(category: str | None, exit_code: int | None) -> str:
    """Map a raw diagnosis category + exit_code to a display bucket."""
    if category in CATEGORY_BUCKETS:
        return CATEGORY_BUCKETS[category]
    # Fallback: categorise by exit code
    if exit_code is not None:
        if exit_code == 0:
            return "p51_short_output"  # success detection failure
        if exit_code == -1:
            return "timeout"
        if exit_code == 137:
            return "timeout"  # OOM killed
        if exit_code == 143:
            return "timeout"  # SIGTERM
        if exit_code == 127:
            return "code_error"  # command not found
        if exit_code != 0:
            return "exit_code_nonzero"
    return "unknown"


def load_terminal_tasks(n: int) -> list[dict]:
    """
    Load the last n terminal-state task records from work_queue.json.
    Terminal states: completed, failed, blocked, archived.
    """
    if not QUEUE_FILE.exists():
        return []

    with open(QUEUE_FILE) as f:
        queue = json.load(f)

    records: list[dict] = []
    for status in ("completed", "failed", "blocked", "archived"):
        for task in queue.get(status, []):
            # Determine the terminal timestamp
            ts = (
                _ts(task.get("completed_at"))
                or _ts(task.get("failed_at"))
                or _ts(task.get("escalated_at"))
                or _ts(task.get("updated_at"))
                or _ts(task.get("created_at"))
            )
            records.append(
                {
                    "task_id": task.get("task_id", ""),
                    "title": task.get("title", ""),
                    "status": status,
                    "exit_code": task.get("exit_code"),
                    "category": (task.get("last_error_diagnosis") or {}).get(
                        "category"
                    ),
                    "retryable": (task.get("last_error_diagnosis") or {}).get(
                        "retryable"
                    ),
                    "retry_count": task.get("retry_count", 0),
                    "complexity": task.get("complexity", "standard"),
                    "employee_id": task.get("assigned_to") or task.get("claimed_by"),
                    "error_snippet": task.get("last_error", "")[:200]
                    if task.get("last_error")
                    else "",
                    "ts": ts,
                }
            )

    records.sort(
        key=lambda r: r["ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
    return records[:n]


def load_failure_log(n: int) -> list[dict]:
    """Load the last n entries from task_failure_log.jsonl."""
    if not FAILURE_LOG.exists():
        return []
    lines = FAILURE_LOG.read_text().splitlines()
    entries = []
    for line in reversed(lines[-n * 2 :]):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= n:
            break
    return entries


def build_report(n: int) -> dict:
    tasks = load_terminal_tasks(n)
    failure_log = load_failure_log(n)

    # Build a lookup from failure log (more detailed category info)
    log_by_task: dict[str, dict] = {}
    for entry in failure_log:
        tid = entry.get("task_id", "")
        if tid:
            log_by_task[tid] = entry

    total = len(tasks)
    successful = sum(1 for t in tasks if t["status"] == "completed")
    failed_count = total - successful

    bucket_counts: Counter = Counter()
    category_counts: Counter = Counter()
    by_employee: dict[str, dict] = defaultdict(lambda: {"success": 0, "failure": 0})
    failure_details: list[dict] = []

    for task in tasks:
        emp = task.get("employee_id") or "unassigned"
        if task["status"] == "completed":
            by_employee[emp]["success"] += 1
            continue

        # Use failure log entry if available for richer data
        log_entry = log_by_task.get(task["task_id"], {})
        category = log_entry.get("category") or task.get("category")
        exit_code = (
            log_entry.get("exit_code")
            if "exit_code" in log_entry
            else task.get("exit_code")
        )

        bucket = _bucket(category, exit_code)
        bucket_counts[bucket] += 1
        if category:
            category_counts[category] += 1
        by_employee[emp]["failure"] += 1

        failure_details.append(
            {
                "task_id": task["task_id"],
                "title": task["title"][:60] + ("…" if len(task["title"]) > 60 else ""),
                "status": task["status"],
                "bucket": bucket,
                "category": category or "unknown",
                "exit_code": exit_code,
                "retry_count": task.get("retry_count", 0),
                "employee_id": emp,
                "error_snippet": (
                    log_entry.get("error_snippet") or task.get("error_snippet") or ""
                )[:120],
            }
        )

    # Top 3 failure modes
    top3 = bucket_counts.most_common(3)

    return {
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "window": f"last {total} tasks",
        "summary": {
            "total_tasks": total,
            "successful": successful,
            "failed": failed_count,
            "success_rate_pct": round(successful / total * 100, 1) if total else 0,
        },
        "failure_buckets": dict(bucket_counts.most_common()),
        "failure_categories_raw": dict(category_counts.most_common()),
        "top_3_failure_modes": [
            {
                "bucket": b,
                "count": c,
                "pct": round(c / failed_count * 100, 1) if failed_count else 0,
            }
            for b, c in top3
        ],
        "by_employee": {
            emp: {**counts, "total": counts["success"] + counts["failure"]}
            for emp, counts in sorted(by_employee.items())
        },
        "failure_log_available": FAILURE_LOG.exists(),
        "failure_details": failure_details,
    }


def print_human_report(report: dict) -> None:
    s = report["summary"]
    print(f"\n{'=' * 60}")
    print(f"  DAEMON TASK SUCCESS/FAILURE REPORT  ({report['window']})")
    print(f"{'=' * 60}")
    print(f"  Total tasks   : {s['total_tasks']}")
    print(f"  Successful    : {s['successful']}")
    print(f"  Failed        : {s['failed']}")
    print(f"  Success rate  : {s['success_rate_pct']}%")
    print(f"{'=' * 60}\n")

    print("TOP 3 FAILURE MODES:")
    for i, mode in enumerate(report["top_3_failure_modes"], 1):
        print(f"  {i}. {mode['bucket']:30s}  {mode['count']} failures ({mode['pct']}%)")

    print("\nFAILURE BUCKETS (all):")
    for bucket, count in report["failure_buckets"].items():
        print(f"  {bucket:30s}  {count}")

    print("\nFAILURE CATEGORIES (raw diagnosis):")
    for cat, count in report["failure_categories_raw"].items():
        print(f"  {cat:35s}  {count}")

    print("\nBY EMPLOYEE:")
    for emp, counts in report["by_employee"].items():
        rate = (
            round(counts["success"] / counts["total"] * 100, 1)
            if counts["total"]
            else 0
        )
        print(
            f"  {emp:35s}  total={counts['total']:3d}  ok={counts['success']:3d}  fail={counts['failure']:3d}  ({rate}%)"
        )

    if report["failure_details"]:
        print("\nRECENT FAILURES (last {n}):".format(n=len(report["failure_details"])))
        for d in report["failure_details"][:20]:
            print(
                f"  [{d['bucket']:25s}] {d['task_id']}  retry={d['retry_count']}  emp={d['employee_id'] or '-'}"
            )
            print(f"    title: {d['title']}")
            if d["error_snippet"]:
                print(f"    error: {d['error_snippet'][:100]}")

    note = (
        ""
        if report["failure_log_available"]
        else " (failure_log not yet populated — run daemon to generate)"
    )
    print(f"\nStructured failure log: {FAILURE_LOG}{note}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Daemon task failure rate report")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--n", type=int, default=50, help="Number of tasks to analyse (default 50)"
    )
    args = parser.parse_args()

    report = build_report(args.n)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human_report(report)


if __name__ == "__main__":
    main()
