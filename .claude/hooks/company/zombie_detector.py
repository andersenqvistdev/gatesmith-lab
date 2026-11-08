#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Zombie Task Detector — Scans work_queue.json for stale in_progress tasks.

Detects tasks that are stuck in "in_progress" state with no active daemon
process or exceeding the staleness threshold. Resets them to "pending"
using atomic writes and logs each recovery.

Usage:
    uv run .claude/hooks/company/zombie_detector.py              # Scan and reset
    uv run .claude/hooks/company/zombie_detector.py --dry-run    # Report only
    uv run .claude/hooks/company/zombie_detector.py --target daemon-87312  # Target specific daemon
    uv run .claude/hooks/company/zombie_detector.py --json       # JSON output
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add script directory to path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from daemon_liveness import get_project_daemon_pid
from queue_io import save_queue_atomic

STALE_THRESHOLD_HOURS = 2.0


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
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_queue_path(company_dir: Path) -> Path:
    p = company_dir / "state" / "work_queue.json"
    return p if p.exists() else company_dir / "work_queue.json"


# save_queue_atomic imported from queue_io below


def get_active_daemon_pids(company_dir: Path | None = None) -> set[str]:
    """PIDs of THIS project's live daemon (empty set when it is not running).

    Reads the project's pidfile rather than ``pgrep -f forge_daemon``, which
    also matched every other project's daemon.
    """
    pid = get_project_daemon_pid(company_dir or find_company_dir())
    return {str(pid)} if pid is not None else set()


def log_zombie_reset(
    company_dir: Path,
    task_id: str,
    title: str,
    reason: str,
    metadata: dict | None = None,
) -> None:
    """Append zombie reset event to .company/logs/zombie_recovery.jsonl."""
    log_dir = company_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "zombie_recovery.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "zombie_reset",
        "task_id": task_id,
        "title": title[:80] if title else "",
        "reason": reason,
    }
    if metadata:
        entry["metadata"] = metadata
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def detect_zombies(
    company_dir: Path,
    *,
    dry_run: bool = False,
    target_daemon_ids: list[str] | None = None,
    target_task_ids: list[str] | None = None,
    stale_hours: float = STALE_THRESHOLD_HOURS,
) -> dict:
    """Scan in_progress tasks for zombies and optionally reset them.

    A task is a zombie if:
    - Its claiming daemon PID is no longer running, OR
    - It has been in_progress for longer than stale_hours

    Targeted detection:
    - target_daemon_ids: Only consider tasks claimed by these daemons
    - target_task_ids: Only consider tasks with these IDs

    Returns dict with findings, actions taken, and summary.
    """
    queue_path = get_queue_path(company_dir)
    queue = load_json(queue_path) or {}
    now = datetime.now(timezone.utc)
    active_pids = get_active_daemon_pids(company_dir)

    zombies: list[dict] = []
    actions: list[dict] = []
    modified = False

    in_progress = list(queue.get("in_progress", []))

    for task in in_progress:
        task_id = task.get("task_id", "?")
        title = task.get("title", "")
        claimed_by = task.get("claimed_by") or task.get("assigned_to") or ""
        claimed_at = task.get("claimed_at") or task.get("started_at")

        # Apply target filters
        if target_daemon_ids and claimed_by not in target_daemon_ids:
            continue
        if target_task_ids and task_id not in target_task_ids:
            continue

        # Check if daemon PID is dead
        is_daemon = claimed_by.startswith("daemon-")
        daemon_pid = claimed_by.replace("daemon-", "") if is_daemon else ""
        pid_dead = is_daemon and daemon_pid and daemon_pid not in active_pids

        # Check age
        age_hours = 0.0
        if claimed_at:
            try:
                dt = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
                age_hours = (now - dt).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        # Check for "(recovered)" title pattern
        is_recovered = title.startswith("(recovered)")

        # Determine if zombie
        reasons: list[str] = []
        if pid_dead:
            reasons.append(f"daemon PID {daemon_pid} is dead")
        if age_hours > stale_hours:
            reasons.append(f"stale for {age_hours:.1f}h (threshold: {stale_hours}h)")
        if is_recovered:
            reasons.append("recovered task still in_progress")

        if not reasons:
            continue

        reason_str = "; ".join(reasons)
        zombie_info = {
            "task_id": task_id,
            "title": title[:80],
            "claimed_by": claimed_by,
            "claimed_at": claimed_at,
            "age_hours": round(age_hours, 1),
            "pid_dead": pid_dead,
            "is_recovered": is_recovered,
            "reason": reason_str,
        }
        zombies.append(zombie_info)

        if not dry_run:
            queue["in_progress"].remove(task)
            for key in [
                "assigned_to",
                "assigned_at",
                "started_at",
                "claimed_by",
                "claimed_at",
            ]:
                task[key] = None
            task["zombie_reset_at"] = now.isoformat()
            task["zombie_reset_reason"] = reason_str
            queue.setdefault("pending", []).insert(0, task)
            modified = True
            actions.append({"action": "reset_to_pending", "task_id": task_id})
            log_zombie_reset(
                company_dir,
                task_id,
                title,
                reason_str,
                metadata={
                    "claimed_by": claimed_by,
                    "age_hours": round(age_hours, 1),
                    "pid_dead": pid_dead,
                },
            )

    if modified:
        queue.setdefault("metadata", {})["last_modified"] = now.isoformat()
        save_queue_atomic(queue, queue_path)

    return {
        "timestamp": now.isoformat(),
        "dry_run": dry_run,
        "active_daemon_pids": sorted(active_pids),
        "zombies_found": len(zombies),
        "zombies": zombies,
        "actions": actions,
        "queue_modified": modified,
    }


def render_report(result: dict) -> str:
    lines: list[str] = []
    mode = "DRY RUN" if result.get("dry_run") else "ZOMBIE RESET"
    lines.append("=" * 60)
    lines.append(f"  ZOMBIE DETECTOR — {mode}")
    lines.append(f"  {result['timestamp'][:19]}")
    lines.append(f"  Active daemon PIDs: {result['active_daemon_pids']}")
    lines.append("=" * 60)
    lines.append("")

    zombies = result.get("zombies", [])
    if not zombies:
        lines.append("  No zombies found. Queue is healthy.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"  ZOMBIES DETECTED: {len(zombies)}")
    lines.append("")
    for z in zombies:
        lines.append(f"  [{z['task_id'][-8:]}] {z['title'][:55]}")
        lines.append(f"    Claimed by: {z['claimed_by']}")
        lines.append(f"    Age: {z['age_hours']}h | PID dead: {z['pid_dead']}")
        lines.append(f"    Reason: {z['reason']}")
        lines.append("")

    actions = result.get("actions", [])
    if actions:
        lines.append(f"  ACTIONS TAKEN: {len(actions)}")
        for a in actions:
            lines.append(f"    + [{a['task_id'][-8:]}] {a['action']}")
        lines.append("")
    elif result.get("dry_run"):
        lines.append(
            f"  Dry run — {len(zombies)} zombies found. Run without --dry-run to reset."
        )
        lines.append("")

    lines.append("-" * 60)
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    company_dir = find_company_dir()
    dry_run = "--dry-run" in args
    json_output = "--json" in args

    target_daemon_ids: list[str] | None = None
    target_task_ids: list[str] | None = None

    for i, arg in enumerate(args):
        if arg == "--target" and i + 1 < len(args):
            target = args[i + 1]
            if target.startswith("daemon-"):
                target_daemon_ids = target_daemon_ids or []
                target_daemon_ids.append(target)
            else:
                target_task_ids = target_task_ids or []
                target_task_ids.append(target)

    result = detect_zombies(
        company_dir,
        dry_run=dry_run,
        target_daemon_ids=target_daemon_ids,
        target_task_ids=target_task_ids,
    )

    if json_output:
        print(json.dumps(result, indent=2))
    else:
        print(render_report(result))

    sys.exit(0 if result["zombies_found"] == 0 else 1)


if __name__ == "__main__":
    main()
