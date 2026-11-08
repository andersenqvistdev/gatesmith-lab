#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Task Result Writer — Persist structured task outcomes to disk.

Writes one JSON file per completed/failed/escalated task to .company/results/,
enabling CLI and web dashboard to show what tasks actually accomplished.

Called by forge_daemon.py after each task completes. Fail-safe: if writing
fails, the daemon continues normally.

Output: .company/results/<task_id>.json

Usage (internal — called by forge_daemon):
    from task_result_writer import write_task_result
    write_task_result(result, action, duration_seconds, company_dir)
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _git_diff_stats(branch: str | None, project_root: Path | None = None) -> dict:
    """Get git diff stats (insertions/deletions/commit hash) for last commit.

    Args:
        branch: Branch name to inspect (uses HEAD if None).
        project_root: Working directory for git commands.

    Returns:
        Dict with commit_hash, insertions, deletions. Empty dict on failure.
    """
    cwd = str(project_root) if project_root else None
    stats: dict = {}

    try:
        # Get latest commit hash
        hash_result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if hash_result.returncode == 0:
            stats["commit_hash"] = hash_result.stdout.strip()[:12]

        # Get diff stats (insertions/deletions)
        stat_result = subprocess.run(
            ["git", "diff", "--shortstat", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if stat_result.returncode == 0:
            text = stat_result.stdout.strip()
            # Parse "3 files changed, 47 insertions(+), 2 deletions(-)"
            stats["insertions"] = 0
            stats["deletions"] = 0
            for part in text.split(","):
                part = part.strip()
                if "insertion" in part:
                    try:
                        stats["insertions"] = int(part.split()[0])
                    except (ValueError, IndexError):
                        pass
                elif "deletion" in part:
                    try:
                        stats["deletions"] = int(part.split()[0])
                    except (ValueError, IndexError):
                        pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return stats


def build_task_result(
    result: dict,
    action: str,
    duration_seconds: float,
) -> dict:
    """Build a structured task result dict from daemon result data.

    Args:
        result: The result dict from poll_and_execute_once.
        action: Action type (executed, failed, escalated).
        duration_seconds: How long the task took.

    Returns:
        Structured result dict ready to write to disk.
    """
    task = result.get("task", {}) or {}
    task_id = result.get("task_id", "unknown")
    emp_activation = result.get("employee_activation", {}) or {}
    git_capture = emp_activation.get("git_capture", {}) or {}
    context = emp_activation.get("context_loaded", {}) or {}

    # Map action to status
    status_map = {
        "executed": "completed",
        "failed": "failed",
        "escalated": "escalated",
    }
    status = status_map.get(action, action)

    # Build git section
    files_changed = git_capture.get("files_changed", [])
    git_section = {
        "branch": git_capture.get("branch", ""),
        "commit_hash": "",
        "files_changed": files_changed,
        "files_count": len(files_changed),
        "changes_count": git_capture.get("changes_count", 0),
        "insertions": 0,
        "deletions": 0,
        "pr_url": git_capture.get("pr_url", ""),
    }

    # Try to enrich with git diff stats
    if git_capture.get("captured") and files_changed:
        diff_stats = _git_diff_stats(git_capture.get("branch"))
        git_section["commit_hash"] = diff_stats.get("commit_hash", "")
        git_section["insertions"] = diff_stats.get("insertions", 0)
        git_section["deletions"] = diff_stats.get("deletions", 0)

    # Build quality section
    quality_section = {
        "plan_score": result.get("plan_score", 0),
        "revision_count": result.get("revision_count", 0),
        "hooks_passed": [],
        "hooks_blocked": [],
    }

    # Build employee section
    employee_section = {
        "id": emp_activation.get("employee_id", "unknown"),
        "context_chars_loaded": context.get("memory_chars", 0),
        "memory_updated": emp_activation.get("memory_updated", False),
    }

    # Build cost section
    cost_section = {
        "tokens_used": result.get("tokens_used", 0),
        "model": emp_activation.get("model", ""),
    }

    # Build the full result
    task_result = {
        "task_id": task_id,
        "title": task.get("title", task.get("description", "No description")),
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 1),
        "priority": task.get("priority", 3),
        "complexity": task.get(
            "estimated_complexity", task.get("complexity", "standard")
        ),
        "source": task.get("source", ""),
        "employee": employee_section,
        "git": git_section,
        "quality": quality_section,
        "cost": cost_section,
        "error": result.get("reason") if status != "completed" else None,
    }

    return task_result


def write_task_result(
    result: dict,
    action: str,
    duration_seconds: float,
    company_dir: Path | None = None,
) -> Path | None:
    """Write a task result to .company/results/<task_id>.json.

    Atomic write using temp file + rename to prevent partial reads.

    Args:
        result: The result dict from poll_and_execute_once.
        action: Action type (executed, failed, escalated).
        duration_seconds: How long the task took.
        company_dir: Path to .company directory. Auto-detected if None.

    Returns:
        Path to the written result file, or None on failure.
    """
    # Skip idle/claim_failed actions — only write for actual task outcomes
    if action not in ("executed", "failed", "escalated"):
        return None

    task_id = result.get("task_id")
    if not task_id:
        return None

    # Resolve company dir
    if company_dir is None:
        try:
            import sys

            _hooks_dir = Path(__file__).resolve().parent
            if str(_hooks_dir) not in sys.path:
                sys.path.insert(0, str(_hooks_dir))
            import company_resolver

            company_dir = company_resolver.get_company_dir()
        except Exception:
            company_dir = Path(".company")

    # Ensure results directory exists
    results_dir = company_dir / "results"
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug(f"Task result writer: cannot create results dir: {e}")
        return None

    # Build structured result
    task_result = build_task_result(result, action, duration_seconds)

    # Sanitize task_id for filename (remove path-unsafe chars)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(task_id))
    result_path = results_dir / f"{safe_id}.json"

    # Atomic write: temp file + rename
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(results_dir), suffix=".tmp", prefix=f"{safe_id}_"
        )
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(task_result, f, indent=2, default=str)
                f.write("\n")
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        Path(tmp_path).replace(result_path)
        logger.debug(f"Task result written: {result_path}")
        return result_path
    except Exception as e:
        logger.debug(f"Task result writer: failed to write {result_path}: {e}")
        return None
