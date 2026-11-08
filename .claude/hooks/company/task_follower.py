#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Task Follower — trace one task through queue → worker log → PR.

Usage:
    uv run .claude/hooks/company/task_follower.py <task-id>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_company_dir() -> Path:
    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        for parent in [start] + list(start.parents):
            candidate = parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return Path(".company")


def load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def relative_time(iso_str: str | None) -> str:
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


ALL_LANES = (
    "in_progress",
    "pending",
    "blocked",
    "pr_open",
    "completed",
    "failed",
    "archived",
)


def find_task_in_queue(
    queue: dict, task_id: str
) -> tuple[str, dict] | tuple[None, None]:
    """Return (lane_name, task_dict) or (None, None) if not found."""
    for lane in ALL_LANES:
        for t in queue.get(lane, []):
            tid = t.get("task_id", "")
            if tid == task_id or tid.endswith(task_id):
                return lane, t
    return None, None


def get_worker_log_path(company_dir: Path, task_id: str) -> Path:
    return company_dir / "logs" / "workers" / f"worker-{task_id}.log"


def read_log_tail(path: Path, n: int = 10) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip() for line in lines[-n:]]
    except (FileNotFoundError, OSError):
        return []


def get_pr_live_state(pr_url: str) -> str | None:
    """Query gh CLI for live PR state. Returns 'OPEN', 'MERGED', 'CLOSED', or None on error."""
    if not pr_url:
        return None
    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        return None
    pr_num = match.group(1)
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_num, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("state")
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        pass
    return None


_STATE_COLOR = {"MERGED": "\033[32m", "OPEN": "\033[33m", "CLOSED": "\033[31m"}


def render_follow(task_id: str, company_dir: Path) -> str:
    w = 78
    lines: list[str] = []

    queue_path = company_dir / "state" / "work_queue.json"
    if not queue_path.exists():
        queue_path = company_dir / "work_queue.json"
    queue = load_json(queue_path) or {}

    lane, task = find_task_in_queue(queue, task_id)

    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append(f"\033[1m  TASK FOLLOW: {task_id}\033[0m")
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("")

    if task is None:
        lines.append(f"  \033[31mTask not found in queue: {task_id}\033[0m")
        lines.append("")
        return "\n".join(lines)

    # --- Queue section ---
    lines.append("  \033[1mQUEUE\033[0m")
    lines.append(f"  {'─' * (w - 4)}")
    lines.append(f"  Title:       {task.get('title', '?')}")
    lines.append(f"  Lane:        \033[1m{lane.upper()}\033[0m")
    lines.append(f"  Priority:    P{task.get('priority', '?')}")
    lines.append(f"  Retry:       {task.get('retry_count') or 0}")

    assigned = task.get("assigned_to") or task.get("claimed_by")
    if assigned:
        lines.append(f"  Assigned to: {assigned}")

    created = task.get("created_at")
    if created:
        lines.append(f"  Created:     {created[:19]} ({relative_time(created)})")

    started = task.get("started_at") or task.get("claimed_at")
    if started:
        lines.append(f"  Started:     {started[:19]} ({relative_time(started)})")

    completed = task.get("completed_at")
    if completed:
        lines.append(f"  Completed:   {completed[:19]} ({relative_time(completed)})")

    last_err = task.get("last_error")
    if last_err:
        lines.append(f"  Last error:  \033[31m{str(last_err)[:70]}\033[0m")

    lines.append("")

    # --- Worker log section ---
    canonical_id = task.get("task_id", task_id)
    log_path = get_worker_log_path(company_dir, canonical_id)
    lines.append("  \033[1mWORKER LOG\033[0m")
    lines.append(f"  {'─' * (w - 4)}")
    lines.append(f"  Path: {log_path}")
    lines.append("")

    log_lines = read_log_tail(log_path, n=10)
    if log_lines:
        for log_line in log_lines:
            lines.append(f"  \033[2m{log_line}\033[0m")
    else:
        lines.append("  \033[2m(no log found)\033[0m")

    lines.append("")

    # --- PR section ---
    pr_url = task.get("pr_url")
    lines.append("  \033[1mPR\033[0m")
    lines.append(f"  {'─' * (w - 4)}")

    if pr_url:
        lines.append(f"  URL:   {pr_url}")
        state = get_pr_live_state(pr_url)
        if state is not None:
            color = _STATE_COLOR.get(state, "")
            lines.append(f"  State: {color}{state}\033[0m  (live via gh)")
        else:
            lines.append("  State: \033[2munknown (gh not available)\033[0m")
    else:
        lines.append("  \033[2m(no PR)\033[0m")

    lines.append("")
    lines.append("\033[2m" + "─" * w + "\033[0m")
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if args else 2)

    task_id = args[0]
    company_dir = find_company_dir()
    print(render_follow(task_id, company_dir))


if __name__ == "__main__":
    main()
