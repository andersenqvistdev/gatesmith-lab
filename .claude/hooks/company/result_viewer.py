#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Result Viewer — CLI display of task outcomes from .company/results/.

Reads structured JSON result files written by task_result_writer.py and
renders them as formatted terminal output.

Usage:
    uv run .claude/hooks/company/result_viewer.py                    # Last 10 results
    uv run .claude/hooks/company/result_viewer.py --all              # All results
    uv run .claude/hooks/company/result_viewer.py --id WQ-042        # Detail view
    uv run .claude/hooks/company/result_viewer.py --watch            # Live tail
    uv run .claude/hooks/company/result_viewer.py --failed           # Only failures
    uv run .claude/hooks/company/result_viewer.py --today            # Today only
    uv run .claude/hooks/company/result_viewer.py --json             # JSON output
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def find_results_dir() -> Path:
    """Find the .company/results/ directory."""
    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        for parent in [start] + list(start.parents):
            candidate = parent / ".company" / "results"
            if candidate.is_dir():
                return candidate
    return Path(".company") / "results"


def load_results(results_dir: Path) -> list[dict]:
    """Load all result JSON files, sorted by timestamp descending."""
    results = []
    if not results_dir.is_dir():
        return results
    for fp in results_dir.glob("*.json"):
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
                data["_file"] = str(fp)
                results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return results


def relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to relative time string."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 0:
            return "future"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return "?"


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if not seconds or seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    total_secs = int(seconds)
    hours = total_secs // 3600
    remaining_secs = total_secs % 3600
    minutes = remaining_secs // 60
    secs = remaining_secs % 60
    if hours > 0:
        return f"{hours}h {minutes}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def status_icon(status: str) -> str:
    """Return colored status icon."""
    icons = {
        "completed": "\033[32m✓\033[0m",
        "failed": "\033[31m✗\033[0m",
        "escalated": "\033[33m⚠\033[0m",
    }
    return icons.get(status, "?")


def trunc(s: str, width: int) -> str:
    """Truncate string with ellipsis."""
    return (s[: width - 1] + "…") if len(s) > width else s


def pr_label(pr_url: str) -> str:
    """Format PR URL as short label."""
    if not pr_url:
        return "\033[2m—\033[0m"
    import re

    match = re.search(r"/pull/(\d+)", pr_url)
    if match:
        return f"\033[36m#PR{match.group(1)}\033[0m"
    return "\033[36mPR\033[0m"


def render_summary(results: list[dict], limit: int = 10) -> str:
    """Render summary view of task results."""
    w = 78
    lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("\033[1m" + "═" * w + "\033[0m")
    lines.append(f"\033[1m  TASK RESULTS — Last {min(limit, len(results))}\033[0m")
    lines.append(f"  {now_str}")
    lines.append("\033[1m" + "═" * w + "\033[0m")
    lines.append("")

    if not results:
        lines.append("  \033[2m(no results found)\033[0m")
        lines.append("")
        lines.append("\033[1m" + "═" * w + "\033[0m")
        return "\n".join(lines)

    shown = results[:limit]
    for r in shown:
        status = r.get("status", "unknown")
        icon = status_icon(status)
        task_id = r.get("task_id", "?")
        title = trunc(r.get("title", ""), 38)
        duration = format_duration(r.get("duration_seconds", 0))
        git = r.get("git", {})
        files_count = git.get("files_count", len(git.get("files_changed", [])))
        files_str = (
            f"{files_count} file{'s' if files_count != 1 else ''}"
            if files_count
            else "0 files"
        )
        pr = pr_label(git.get("pr_url", ""))

        # Line 1: status, id, title, duration, files, PR
        lines.append(
            f"  {icon} \033[1m{task_id:<10}\033[0m {title:<38} "
            f"{duration:>7}  {files_str:>8}  {pr}"
        )

        # Line 2: employee, quality details
        emp = r.get("employee", {})
        emp_id = emp.get("id", "")
        quality = r.get("quality", {})
        plan_score = quality.get("plan_score", 0)
        revision_count = quality.get("revision_count", 0)

        details = []
        if emp_id:
            details.append(emp_id)
        if plan_score:
            details.append(f"plan:{plan_score}")
        if revision_count:
            details.append(f"rev:{revision_count}")

        error = r.get("error")
        if error and status != "completed":
            error_short = trunc(str(error), 50)
            details.append(f"\033[31mError: {error_short}\033[0m")

        if details:
            lines.append(f"            \033[2m{' | '.join(details)}\033[0m")

        lines.append("")

    # Footer summary
    completed = sum(1 for r in shown if r.get("status") == "completed")
    failed = sum(1 for r in shown if r.get("status") == "failed")
    escalated = sum(1 for r in shown if r.get("status") == "escalated")

    lines.append("\033[1m" + "═" * w + "\033[0m")
    parts = [f"{len(shown)} results"]
    if completed:
        parts.append(f"\033[32m{completed} completed\033[0m")
    if failed:
        parts.append(f"\033[31m{failed} failed\033[0m")
    if escalated:
        parts.append(f"\033[33m{escalated} escalated\033[0m")
    lines.append(f"  {', '.join(parts)}")
    lines.append("\033[1m" + "═" * w + "\033[0m")

    return "\n".join(lines)


def render_detail(r: dict) -> str:
    """Render detailed view of a single task result."""
    w = 78
    lines = []

    task_id = r.get("task_id", "?")
    status = r.get("status", "unknown")
    icon = status_icon(status)

    lines.append("\033[1m" + "═" * w + "\033[0m")
    lines.append(f"\033[1m  TASK RESULT: {task_id}\033[0m")
    lines.append("\033[1m" + "═" * w + "\033[0m")
    lines.append("")

    lines.append(f"  Title:      {r.get('title', '?')}")
    lines.append(f"  Status:     {icon} {status.upper()}")
    lines.append(f"  Duration:   {format_duration(r.get('duration_seconds', 0))}")

    emp = r.get("employee", {})
    lines.append(f"  Employee:   {emp.get('id', '?')}")
    lines.append(f"  Timestamp:  {r.get('timestamp', '?')}")

    if r.get("priority"):
        lines.append(f"  Priority:   P{r['priority']}")
    if r.get("complexity"):
        lines.append(f"  Complexity: {r['complexity']}")

    error = r.get("error")
    if error:
        lines.append(f"  \033[31mError:      {error}\033[0m")

    # Git section
    git = r.get("git", {})
    if git.get("branch") or git.get("files_changed"):
        lines.append("")
        lines.append("  \033[1mGIT\033[0m")
        lines.append(f"  {'─' * (w - 4)}")

        if git.get("branch"):
            lines.append(f"  Branch:     {git['branch']}")
        if git.get("commit_hash"):
            lines.append(f"  Commit:     {git['commit_hash']}")
        if git.get("pr_url"):
            lines.append(f"  PR:         {git['pr_url']}")

        ins = git.get("insertions", 0)
        dels = git.get("deletions", 0)
        fc = git.get("files_count", len(git.get("files_changed", [])))
        if ins or dels or fc:
            lines.append(
                f"  Changes:    \033[32m+{ins}\033[0m \033[31m-{dels}\033[0m across {fc} file{'s' if fc != 1 else ''}"
            )

        for f in git.get("files_changed", []):
            lines.append(f"    {f}")

    # Quality section
    quality = r.get("quality", {})
    if any(quality.values()):
        lines.append("")
        lines.append("  \033[1mQUALITY\033[0m")
        lines.append(f"  {'─' * (w - 4)}")

        ps = quality.get("plan_score", 0)
        if ps:
            lines.append(f"  Plan Score: {ps}")
        rev = quality.get("revision_count", 0)
        lines.append(f"  Revisions:  {rev}")

        passed = quality.get("hooks_passed", [])
        blocked = quality.get("hooks_blocked", [])
        if passed:
            hooks_str = " ".join(f"\033[32m{h}✓\033[0m" for h in passed)
            lines.append(f"  Hooks:      {hooks_str}")
        if blocked:
            hooks_str = " ".join(f"\033[31m{h}✗\033[0m" for h in blocked)
            lines.append(f"  Blocked:    {hooks_str}")

    # Cost section
    cost = r.get("cost", {})
    if cost.get("tokens_used") or cost.get("model"):
        lines.append("")
        lines.append("  \033[1mCOST\033[0m")
        lines.append(f"  {'─' * (w - 4)}")

        tokens = cost.get("tokens_used", 0)
        if tokens:
            lines.append(f"  Tokens:     {tokens:,}")
        model = cost.get("model", "")
        if model:
            lines.append(f"  Model:      {model}")

    # Employee details
    if emp.get("context_chars_loaded") or emp.get("memory_updated"):
        lines.append("")
        lines.append("  \033[1mEMPLOYEE\033[0m")
        lines.append(f"  {'─' * (w - 4)}")

        ctx = emp.get("context_chars_loaded", 0)
        if ctx:
            lines.append(f"  Context:    {ctx:,} chars loaded")
        if emp.get("memory_updated"):
            lines.append("  Memory:     \033[32mupdated\033[0m")

    lines.append("")
    lines.append("\033[1m" + "═" * w + "\033[0m")

    return "\n".join(lines)


def watch_results(results_dir: Path, interval: int = 5) -> None:
    """Live tail of results as they appear."""
    seen_files: set[str] = set()

    # Initialize with existing files
    if results_dir.is_dir():
        for fp in results_dir.glob("*.json"):
            seen_files.add(str(fp))

    print(f"\033[2mWatching {results_dir} for new results (Ctrl+C to stop)...\033[0m")
    print()

    try:
        while True:
            if results_dir.is_dir():
                for fp in sorted(results_dir.glob("*.json")):
                    fp_str = str(fp)
                    if fp_str not in seen_files:
                        seen_files.add(fp_str)
                        try:
                            with open(fp, encoding="utf-8") as f:
                                data = json.load(f)
                            # Print compact one-liner for new results
                            status = data.get("status", "?")
                            icon = status_icon(status)
                            task_id = data.get("task_id", "?")
                            title = trunc(data.get("title", ""), 40)
                            duration = format_duration(data.get("duration_seconds", 0))
                            git = data.get("git", {})
                            fc = git.get(
                                "files_count", len(git.get("files_changed", []))
                            )
                            ts = data.get("timestamp", "")[:19]
                            print(
                                f"  {ts} {icon} \033[1m{task_id}\033[0m "
                                f"{title} ({duration}, {fc} files)"
                            )
                            if data.get("error"):
                                print(
                                    f"             \033[31m→ {trunc(str(data['error']), 60)}\033[0m"
                                )
                        except (json.JSONDecodeError, OSError):
                            continue
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\033[2mStopped.\033[0m")


def main():
    parser = argparse.ArgumentParser(description="Gatesmith Task Results Viewer")
    parser.add_argument("--all", action="store_true", help="Show all results")
    parser.add_argument("--id", type=str, help="Show detail for specific task ID")
    parser.add_argument(
        "--watch", nargs="?", const=5, type=int, help="Live tail (interval in seconds)"
    )
    parser.add_argument("--failed", action="store_true", help="Only failed tasks")
    parser.add_argument("--completed", action="store_true", help="Only completed tasks")
    parser.add_argument("--escalated", action="store_true", help="Only escalated tasks")
    parser.add_argument("--today", action="store_true", help="Only today's results")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of results to show"
    )
    args = parser.parse_args()

    results_dir = find_results_dir()

    # Watch mode
    if args.watch is not None:
        watch_results(results_dir, args.watch)
        return

    # Load all results
    results = load_results(results_dir)

    # Filter by status
    if args.failed:
        results = [r for r in results if r.get("status") == "failed"]
    elif args.completed:
        results = [r for r in results if r.get("status") == "completed"]
    elif args.escalated:
        results = [r for r in results if r.get("status") == "escalated"]

    # Filter by today
    if args.today:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results = [r for r in results if r.get("timestamp", "").startswith(today)]

    # Detail view
    if args.id:
        target_id = args.id
        match = [r for r in results if r.get("task_id") == target_id]
        if not match:
            # Try partial match
            match = [
                r for r in results if target_id.lower() in r.get("task_id", "").lower()
            ]
        if match:
            if args.json:
                print(json.dumps(match[0], indent=2, default=str))
            else:
                print(render_detail(match[0]))
        else:
            print(f"No result found for task ID: {target_id}", file=sys.stderr)
            sys.exit(1)
        return

    # JSON output
    if args.json:
        limit = len(results) if args.all else args.limit
        # Remove internal _file key
        output = [{k: v for k, v in r.items() if k != "_file"} for r in results[:limit]]
        print(json.dumps(output, indent=2, default=str))
        return

    # Summary view
    limit = len(results) if args.all else args.limit
    print(render_summary(results, limit))


if __name__ == "__main__":
    main()
