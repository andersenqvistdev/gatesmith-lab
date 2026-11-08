#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Progress Tracker — tracks work progress across organization.

Monitors task status, calculates completion percentages, detects stalled work,
and triggers completion notifications. Uses org.json for company structure
and work_queue.json for task data.

Multi-Project Support (v1.2):
- In multi-project mode, aggregates progress across all projects under company root
- Supports company-wide views and per-project breakdowns
- Uses company_resolver for path resolution

Features:
- Update task progress with notes
- Calculate company-wide progress percentages (aggregated across projects)
- Calculate department-level progress
- Calculate per-project progress breakdowns
- Detect stalled work (no progress for configurable time)
- Trigger notifications on completion

Usage:
    # Update task progress
    python progress_tracker.py update --task-id TASK-123 --status in_progress --notes "Started implementation"

    # Get company-wide progress (aggregated)
    python progress_tracker.py company

    # Get company-wide progress with per-project breakdown
    python progress_tracker.py company --breakdown

    # Get specific project progress
    python progress_tracker.py project --project-id myproject-a1b2c3

    # List all projects
    python progress_tracker.py projects

    # Get department progress
    python progress_tracker.py department --dept-id engineering

    # Detect stalled work (no updates in 60 minutes)
    python progress_tracker.py stalled --threshold 60

    # Show help
    python progress_tracker.py help
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Import company_resolver for multi-project support
try:
    from company_resolver import (
        find_company_root,
        get_project_id,
        is_multi_project_mode,
    )
    from company_resolver import (
        get_company_dir as resolver_get_company_dir,
    )

    COMPANY_RESOLVER_AVAILABLE = True
except ImportError:
    COMPANY_RESOLVER_AVAILABLE = False


# Configuration
LEGACY_COMPANY_DIR = ".company"
ORG_FILE = "org.json"
QUEUE_FILE = "state/work_queue.json"
DEFAULT_STALL_THRESHOLD_MINUTES = 60


def get_company_dir(start_path: Path | None = None) -> Path:
    """
    Get the company directory path.

    In multi-project mode, returns the shared company directory at company root.
    In legacy mode, returns .company in the current/specified directory.
    """
    if COMPANY_RESOLVER_AVAILABLE:
        return resolver_get_company_dir(start_path)

    # Legacy fallback
    base = start_path if start_path else Path(os.getcwd())
    return base / LEGACY_COMPANY_DIR


def get_org_path(start_path: Path | None = None) -> Path:
    """Get org.json path."""
    return get_company_dir(start_path) / ORG_FILE


def get_queue_path(start_path: Path | None = None) -> Path:
    """Get work_queue.json path."""
    return get_company_dir(start_path) / QUEUE_FILE


def discover_projects() -> list[dict]:
    """
    Discover all projects under the company root.

    In multi-project mode, scans the company root for directories that
    appear to be projects (have .planning, .claude, or other markers).

    Returns:
        List of project info dicts with keys:
        - project_id: Unique identifier
        - project_path: Path to project directory
        - project_name: Human-readable name
        - has_queue: Whether project has its own work queue
    """
    if not COMPANY_RESOLVER_AVAILABLE:
        # Legacy mode - single project
        cwd = Path.cwd()
        return [
            {
                "project_id": cwd.name,
                "project_path": cwd,
                "project_name": cwd.name,
                "has_queue": (cwd / LEGACY_COMPANY_DIR / QUEUE_FILE).exists(),
            }
        ]

    company_root = find_company_root()
    if company_root is None:
        # Not in multi-project mode
        cwd = Path.cwd()
        return [
            {
                "project_id": get_project_id(cwd),
                "project_path": cwd,
                "project_name": cwd.name,
                "has_queue": get_queue_path().exists(),
            }
        ]

    projects = []

    # Scan company root for project directories
    try:
        for item in company_root.iterdir():
            if not item.is_dir():
                continue

            # Skip hidden directories and common non-project dirs
            if item.name.startswith("."):
                continue
            if item.name in ("node_modules", "__pycache__", "venv", ".venv"):
                continue

            # Check for project indicators
            has_planning = (item / ".planning").exists()
            has_claude = (item / ".claude").exists()
            has_git = (item / ".git").exists()
            has_package = (item / "package.json").exists() or (
                item / "pyproject.toml"
            ).exists()

            # Consider it a project if it has any project indicator
            if has_planning or has_claude or has_git or has_package:
                proj_id = get_project_id(item)

                # Check for project-level queue (legacy per-project mode)
                project_queue_path = item / LEGACY_COMPANY_DIR / QUEUE_FILE

                projects.append(
                    {
                        "project_id": proj_id,
                        "project_path": item,
                        "project_name": item.name,
                        "has_queue": project_queue_path.exists(),
                    }
                )
    except (OSError, PermissionError):
        pass

    return projects


def get_all_project_queues() -> dict[str, dict]:
    """
    Get work queues from all discovered projects.

    Returns:
        Dict mapping project_id to their work queue data.
    """
    queues = {}

    # Get the main company-level queue
    main_queue = load_queue()
    main_queue_path = get_queue_path()

    if main_queue_path.exists():
        queues["__company__"] = main_queue

    # In multi-project mode, also check project-level queues
    if COMPANY_RESOLVER_AVAILABLE and is_multi_project_mode():
        for project in discover_projects():
            project_queue_path = (
                project["project_path"] / LEGACY_COMPANY_DIR / QUEUE_FILE
            )
            if project_queue_path.exists():
                try:
                    with open(project_queue_path, encoding="utf-8") as f:
                        queues[project["project_id"]] = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

    return queues


def load_org(start_path: Path | None = None) -> dict:
    """Load organization structure from org.json."""
    org_path = get_org_path(start_path)
    if not org_path.exists():
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
            "work": {"active": [], "completed": []},
        }

    try:
        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
            "work": {"active": [], "completed": []},
        }
    # Normalize bare-string employees/agents to dict records (ProjectK fix).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, org_path.parent)


def load_queue(start_path: Path | None = None) -> dict:
    """Load work queue from file."""
    queue_path = get_queue_path(start_path)

    if not queue_path.exists():
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "metadata": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_modified": datetime.now(timezone.utc).isoformat(),
            },
        }

    try:
        with open(queue_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "metadata": {},
        }


def save_queue(queue: dict, start_path: Path | None = None) -> None:
    """Save work queue to file (with QueueLock to prevent concurrent overwrites)."""
    import os as _os

    # P80: Prevent pytest from overwriting production queue.
    _real_company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    _is_pytest = "PYTEST_CURRENT_TEST" in _os.environ or any(
        "pytest" in str(v) for v in [_os.environ.get("_", "")]
    )
    if _is_pytest:
        try:
            _candidate = get_queue_path(start_path)
            if _candidate.resolve().is_relative_to(_real_company.resolve()):
                return  # Silently skip
        except (ValueError, OSError):
            pass

    queue_path = get_queue_path(start_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    company_dir = (
        queue_path.parent.parent
    )  # queue_path is .company/state/work_queue.json
    lock_path = company_dir / "runtime/queue.lock"

    queue.setdefault("metadata", {})
    queue["metadata"]["last_modified"] = datetime.now(timezone.utc).isoformat()

    # Import QueueLock for safe concurrent access
    _QueueLock = None
    try:
        from work_allocator import QueueLock as _QueueLock
    except ImportError:
        try:
            import sys as _sys

            hooks_dir = str(Path(__file__).resolve().parent)
            if hooks_dir not in _sys.path:
                _sys.path.insert(0, hooks_dir)
            from work_allocator import QueueLock as _QueueLock
        except ImportError:
            pass

    if _QueueLock is not None:
        lock_ctx = _QueueLock(lock_path)
    else:
        from contextlib import nullcontext

        lock_ctx = nullcontext()

    # Atomic write with QueueLock to prevent data loss from concurrent overwrites
    import tempfile

    with lock_ctx:
        fd, tmp = tempfile.mkstemp(
            dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
            os.replace(tmp, str(queue_path))
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def save_org(org: dict, start_path: Path | None = None) -> None:
    """Save organization data to org.json.

    Safety: Refuses to save if it would wipe existing employees.
    """
    import tempfile

    org_path = get_org_path(start_path)
    org_path.parent.mkdir(parents=True, exist_ok=True)

    # Safety check: Don't wipe employees if file already has them
    if org_path.exists():
        try:
            with open(org_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            # Block saves that would wipe existing employees
            if len(existing_employees) > 0 and len(new_employees) == 0:
                import sys

                print(
                    f"[SAFETY] Blocked save_org: Would wipe {len(existing_employees)} employees. "
                    "This is likely a bug in the calling code.",
                    file=sys.stderr,
                )
                return  # Refuse to save
        except (json.JSONDecodeError, OSError):
            # If we can't read existing file and trying to save empty employees, block
            if len(org.get("employees", [])) == 0:
                import sys

                print(
                    "[SAFETY] Blocked save_org: Cannot read existing file and new data has no employees. "
                    "This could cause data loss.",
                    file=sys.stderr,
                )
                return  # Refuse to save

    # Atomic write: write to temp file, then os.replace (prevents truncation race)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix="org_", dir=str(org_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, str(org_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def sanitize_for_applescript(text: str) -> str:
    """Escape special characters for safe AppleScript interpolation.

    Prevents command injection by escaping backslashes and double quotes
    that could break out of the string context in osascript.
    """
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str) -> None:
    """Send a desktop notification."""
    system = platform.system()

    try:
        if system == "Darwin":
            # Sanitize inputs to prevent AppleScript injection
            safe_title = sanitize_for_applescript(title)
            safe_message = sanitize_for_applescript(message)
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_message}" with title "{safe_title}"',
                ],
                capture_output=True,
                timeout=5,
            )
        elif system == "Linux":
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True,
                timeout=5,
            )
        else:
            print("\a", end="")  # Terminal bell fallback
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("\a", end="")  # Terminal bell fallback


def update_progress(
    task_id: str,
    status: str | None = None,
    notes: str | None = None,
) -> dict:
    """
    Update task progress and add notes.

    Args:
        task_id: The task to update
        status: New status (pending, in_progress, blocked, completed)
        notes: Progress notes to add

    Returns:
        Dict with success status and updated task
    """
    valid_statuses = {"pending", "in_progress", "blocked", "completed"}

    if status and status not in valid_statuses:
        return {
            "success": False,
            "reason": "invalid_status",
            "message": f"Status must be one of: {valid_statuses}",
        }

    queue = load_queue()
    now = datetime.now(timezone.utc).isoformat()

    # Find the task in any status list
    found_task = None
    found_status = None

    for current_status in ["pending", "in_progress", "blocked", "completed"]:
        for task in queue.get(current_status, []):
            if task.get("task_id") == task_id:
                found_task = task
                found_status = current_status
                break
        if found_task:
            break

    if not found_task:
        return {
            "success": False,
            "reason": "not_found",
            "message": f"Task {task_id} not found",
        }

    # Track if completing
    completing = status == "completed" and found_status != "completed"

    # Update progress timestamp
    found_task["last_progress_at"] = now

    # Add notes if provided
    if notes:
        if "progress_notes" not in found_task:
            found_task["progress_notes"] = []
        found_task["progress_notes"].append(
            {
                "timestamp": now,
                "content": notes,
            }
        )

    # Handle status change
    if status and status != found_status:
        # Remove from current status list
        queue[found_status] = [
            t for t in queue[found_status] if t.get("task_id") != task_id
        ]

        # Update timestamps based on new status
        if status == "completed":
            found_task["completed_at"] = now
        elif status == "in_progress" and not found_task.get("started_at"):
            found_task["started_at"] = now

        # Add to new status list
        queue[status].append(found_task)

    save_queue(queue)

    # Trigger completion notification
    if completing:
        title = found_task.get("title", task_id)
        send_notification(
            "Task Completed",
            f"Completed: {title[:50]}{'...' if len(title) > 50 else ''}",
        )

    return {
        "success": True,
        "task_id": task_id,
        "previous_status": found_status,
        "new_status": status or found_status,
        "task": found_task,
        "notification_sent": completing,
    }


def aggregate_queue_stats(queues: dict[str, dict]) -> dict:
    """
    Aggregate statistics from multiple work queues.

    Args:
        queues: Dict mapping source_id to queue data

    Returns:
        Dict with aggregated counts and task lists
    """
    aggregated = {
        "pending": [],
        "in_progress": [],
        "blocked": [],
        "completed": [],
    }

    for source_id, queue in queues.items():
        for status in ["pending", "in_progress", "blocked", "completed"]:
            for task in queue.get(status, []):
                # Add source tracking to task
                task_copy = task.copy()
                task_copy["_source"] = source_id
                aggregated[status].append(task_copy)

    return aggregated


def get_company_progress(include_breakdown: bool = False) -> dict:
    """
    Get overall company progress summary.

    In multi-project mode, aggregates progress across all projects.

    Args:
        include_breakdown: If True, include per-project breakdown

    Returns:
        Dict with company-wide progress metrics
    """
    org = load_org()

    # Determine if we're in multi-project mode
    multi_project = COMPANY_RESOLVER_AVAILABLE and is_multi_project_mode()

    if multi_project:
        # Aggregate across all project queues
        all_queues = get_all_project_queues()
        aggregated = aggregate_queue_stats(all_queues)
        queue = aggregated
    else:
        # Single project mode
        queue = load_queue()
        all_queues = {"current": queue}

    # Count tasks by status
    pending_count = len(queue.get("pending", []))
    in_progress_count = len(queue.get("in_progress", []))
    blocked_count = len(queue.get("blocked", []))
    completed_count = len(queue.get("completed", []))

    total_count = pending_count + in_progress_count + blocked_count + completed_count

    # Calculate percentages
    if total_count > 0:
        completion_percentage = round((completed_count / total_count) * 100, 1)
        in_progress_percentage = round((in_progress_count / total_count) * 100, 1)
        blocked_percentage = round((blocked_count / total_count) * 100, 1)
        pending_percentage = round((pending_count / total_count) * 100, 1)
    else:
        completion_percentage = 0
        in_progress_percentage = 0
        blocked_percentage = 0
        pending_percentage = 0

    # Calculate estimated hours
    all_tasks = (
        queue.get("pending", [])
        + queue.get("in_progress", [])
        + queue.get("blocked", [])
        + queue.get("completed", [])
    )
    total_estimated_hours = sum(task.get("estimated_hours", 4) for task in all_tasks)
    completed_hours = sum(
        task.get("estimated_hours", 4) for task in queue.get("completed", [])
    )
    remaining_hours = total_estimated_hours - completed_hours

    # Get company name
    company_name = org.get("company", {}).get("name", "Unknown Company")

    # Count agents and departments
    agents = org.get("agents", [])
    active_agents = [a for a in agents if a.get("status") == "active"]
    departments = org.get("departments", [])

    result = {
        "success": True,
        "company_name": company_name,
        "multi_project_mode": multi_project,
        "summary": {
            "total_tasks": total_count,
            "completed": completed_count,
            "in_progress": in_progress_count,
            "blocked": blocked_count,
            "pending": pending_count,
        },
        "percentages": {
            "completed": completion_percentage,
            "in_progress": in_progress_percentage,
            "blocked": blocked_percentage,
            "pending": pending_percentage,
        },
        "hours": {
            "total_estimated": round(total_estimated_hours, 1),
            "completed": round(completed_hours, 1),
            "remaining": round(remaining_hours, 1),
        },
        "resources": {
            "total_agents": len(agents),
            "active_agents": len(active_agents),
            "departments": len(departments),
        },
        "health": {
            "blocked_ratio": blocked_percentage,
            "is_healthy": blocked_percentage < 20,
            "status": (
                "healthy"
                if blocked_percentage < 10
                else "warning"
                if blocked_percentage < 20
                else "critical"
            ),
        },
    }

    # Include per-project breakdown if requested
    if include_breakdown and multi_project:
        projects = discover_projects()
        breakdown = []

        for project in projects:
            proj_queue = all_queues.get(project["project_id"], {})
            proj_pending = len(proj_queue.get("pending", []))
            proj_in_progress = len(proj_queue.get("in_progress", []))
            proj_blocked = len(proj_queue.get("blocked", []))
            proj_completed = len(proj_queue.get("completed", []))
            proj_total = proj_pending + proj_in_progress + proj_blocked + proj_completed

            proj_completion = (
                round((proj_completed / proj_total) * 100, 1) if proj_total > 0 else 0
            )

            breakdown.append(
                {
                    "project_id": project["project_id"],
                    "project_name": project["project_name"],
                    "total_tasks": proj_total,
                    "completed": proj_completed,
                    "in_progress": proj_in_progress,
                    "blocked": proj_blocked,
                    "pending": proj_pending,
                    "completion_percentage": proj_completion,
                }
            )

        # Also include company-level queue if present
        if "__company__" in all_queues:
            company_queue = all_queues["__company__"]
            c_pending = len(company_queue.get("pending", []))
            c_in_progress = len(company_queue.get("in_progress", []))
            c_blocked = len(company_queue.get("blocked", []))
            c_completed = len(company_queue.get("completed", []))
            c_total = c_pending + c_in_progress + c_blocked + c_completed

            c_completion = round((c_completed / c_total) * 100, 1) if c_total > 0 else 0

            breakdown.insert(
                0,
                {
                    "project_id": "__company__",
                    "project_name": "Company-Level Tasks",
                    "total_tasks": c_total,
                    "completed": c_completed,
                    "in_progress": c_in_progress,
                    "blocked": c_blocked,
                    "pending": c_pending,
                    "completion_percentage": c_completion,
                },
            )

        result["breakdown"] = breakdown
        result["project_count"] = len(projects)

    return result


def get_project_progress(project_id: str) -> dict:
    """
    Get progress for a specific project.

    Args:
        project_id: The project identifier

    Returns:
        Dict with project-specific progress metrics
    """
    projects = discover_projects()
    project = next((p for p in projects if p["project_id"] == project_id), None)

    if not project:
        return {
            "success": False,
            "reason": "not_found",
            "message": f"Project '{project_id}' not found",
            "available_projects": [p["project_id"] for p in projects],
        }

    # Try to load project-specific queue first
    project_queue_path = project["project_path"] / LEGACY_COMPANY_DIR / QUEUE_FILE

    if project_queue_path.exists():
        try:
            with open(project_queue_path, encoding="utf-8") as f:
                queue = json.load(f)
        except (json.JSONDecodeError, OSError):
            queue = {"pending": [], "in_progress": [], "blocked": [], "completed": []}
    else:
        queue = {"pending": [], "in_progress": [], "blocked": [], "completed": []}

    # Also check company-level queue for tasks tagged with this project
    company_queue = load_queue()
    for status in ["pending", "in_progress", "blocked", "completed"]:
        for task in company_queue.get(status, []):
            if (
                task.get("project_id") == project_id
                or task.get("project") == project_id
            ):
                queue[status].append(task)

    # Calculate statistics
    pending_count = len(queue.get("pending", []))
    in_progress_count = len(queue.get("in_progress", []))
    blocked_count = len(queue.get("blocked", []))
    completed_count = len(queue.get("completed", []))
    total_count = pending_count + in_progress_count + blocked_count + completed_count

    if total_count > 0:
        completion_percentage = round((completed_count / total_count) * 100, 1)
        in_progress_percentage = round((in_progress_count / total_count) * 100, 1)
        blocked_percentage = round((blocked_count / total_count) * 100, 1)
    else:
        completion_percentage = 0
        in_progress_percentage = 0
        blocked_percentage = 0

    # Calculate hours
    all_tasks = (
        queue.get("pending", [])
        + queue.get("in_progress", [])
        + queue.get("blocked", [])
        + queue.get("completed", [])
    )
    total_hours = sum(t.get("estimated_hours", 4) for t in all_tasks)
    completed_hours = sum(
        t.get("estimated_hours", 4) for t in queue.get("completed", [])
    )

    return {
        "success": True,
        "project_id": project_id,
        "project_name": project["project_name"],
        "project_path": str(project["project_path"]),
        "summary": {
            "total_tasks": total_count,
            "completed": completed_count,
            "in_progress": in_progress_count,
            "blocked": blocked_count,
            "pending": pending_count,
        },
        "percentages": {
            "completed": completion_percentage,
            "in_progress": in_progress_percentage,
            "blocked": blocked_percentage,
        },
        "hours": {
            "total_estimated": round(total_hours, 1),
            "completed": round(completed_hours, 1),
            "remaining": round(total_hours - completed_hours, 1),
        },
        "tasks": {
            "in_progress": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "assigned_to": t.get("assigned_to"),
                }
                for t in queue.get("in_progress", [])
            ],
            "blocked": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "blocked_by": t.get("dependencies", []),
                }
                for t in queue.get("blocked", [])
            ],
        },
    }


def list_projects() -> dict:
    """
    List all discovered projects with summary statistics.

    Returns:
        Dict with list of projects and their basic stats
    """
    projects = discover_projects()

    multi_project = COMPANY_RESOLVER_AVAILABLE and is_multi_project_mode()

    project_list = []
    for project in projects:
        proj_queue_path = project["project_path"] / LEGACY_COMPANY_DIR / QUEUE_FILE

        total_tasks = 0
        completed_tasks = 0

        if proj_queue_path.exists():
            try:
                with open(proj_queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
                    total_tasks = sum(
                        len(queue.get(s, []))
                        for s in ["pending", "in_progress", "blocked", "completed"]
                    )
                    completed_tasks = len(queue.get("completed", []))
            except (json.JSONDecodeError, OSError):
                pass

        project_list.append(
            {
                "project_id": project["project_id"],
                "project_name": project["project_name"],
                "project_path": str(project["project_path"]),
                "has_queue": project["has_queue"],
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "completion_percentage": (
                    round((completed_tasks / total_tasks) * 100, 1)
                    if total_tasks > 0
                    else 0
                ),
            }
        )

    return {
        "success": True,
        "multi_project_mode": multi_project,
        "project_count": len(project_list),
        "projects": project_list,
    }


def get_department_progress(dept_id: str) -> dict:
    """
    Get progress for a specific department.

    Args:
        dept_id: Department identifier

    Returns:
        Dict with department progress metrics
    """
    queue = load_queue()
    org = load_org()

    # Filter tasks by department
    def is_dept_task(task: dict) -> bool:
        """Return True if task belongs to the target department."""
        return task.get("department") == dept_id

    pending = [t for t in queue.get("pending", []) if is_dept_task(t)]
    in_progress = [t for t in queue.get("in_progress", []) if is_dept_task(t)]
    blocked = [t for t in queue.get("blocked", []) if is_dept_task(t)]
    completed = [t for t in queue.get("completed", []) if is_dept_task(t)]

    total_count = len(pending) + len(in_progress) + len(blocked) + len(completed)

    # Calculate percentages
    if total_count > 0:
        completion_percentage = round((len(completed) / total_count) * 100, 1)
        in_progress_percentage = round((len(in_progress) / total_count) * 100, 1)
        blocked_percentage = round((len(blocked) / total_count) * 100, 1)
    else:
        completion_percentage = 0
        in_progress_percentage = 0
        blocked_percentage = 0

    # Calculate hours
    all_dept_tasks = pending + in_progress + blocked + completed
    total_hours = sum(t.get("estimated_hours", 4) for t in all_dept_tasks)
    completed_hours = sum(t.get("estimated_hours", 4) for t in completed)

    # Get department info from org
    departments = org.get("departments", [])
    dept_info = next((d for d in departments if d.get("id") == dept_id), None)
    dept_name = dept_info.get("name", dept_id) if dept_info else dept_id

    # Get agents assigned to this department
    agents = org.get("agents", [])
    dept_agents = [
        a
        for a in agents
        if a.get("department") == dept_id or dept_id in a.get("departments", [])
    ]

    # Get assigned agents from in-progress tasks
    assigned_agents = set()
    for task in in_progress:
        if task.get("assigned_to"):
            assigned_agents.add(task["assigned_to"])

    return {
        "success": True,
        "department_id": dept_id,
        "department_name": dept_name,
        "summary": {
            "total_tasks": total_count,
            "completed": len(completed),
            "in_progress": len(in_progress),
            "blocked": len(blocked),
            "pending": len(pending),
        },
        "percentages": {
            "completed": completion_percentage,
            "in_progress": in_progress_percentage,
            "blocked": blocked_percentage,
        },
        "hours": {
            "total_estimated": round(total_hours, 1),
            "completed": round(completed_hours, 1),
            "remaining": round(total_hours - completed_hours, 1),
        },
        "agents": {
            "total": len(dept_agents),
            "actively_working": len(assigned_agents),
            "assigned_agent_ids": list(assigned_agents),
        },
        "tasks": {
            "in_progress": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "assigned_to": t.get("assigned_to"),
                }
                for t in in_progress
            ],
            "blocked": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "blocked_by": t.get("dependencies", []),
                }
                for t in blocked
            ],
        },
    }


def detect_stalled_work(
    threshold_minutes: int = DEFAULT_STALL_THRESHOLD_MINUTES,
) -> dict:
    """
    Detect tasks that have stalled (no progress for threshold time).

    Args:
        threshold_minutes: Minutes without progress to consider stalled

    Returns:
        Dict with stalled tasks and recommendations
    """
    queue = load_queue()
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=threshold_minutes)

    stalled_tasks = []

    # Check in_progress tasks for staleness
    for task in queue.get("in_progress", []):
        task_id = task.get("task_id", "unknown")
        title = task.get("title", "Untitled")

        # Get last activity timestamp
        last_progress = task.get("last_progress_at")
        started_at = task.get("started_at")
        assigned_at = task.get("assigned_at")

        # Use most recent timestamp
        last_activity_str = last_progress or started_at or assigned_at

        if not last_activity_str:
            # No timestamp - consider stalled
            stalled_tasks.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "assigned_to": task.get("assigned_to"),
                    "department": task.get("department"),
                    "stalled_reason": "no_timestamp",
                    "stalled_duration_minutes": None,
                    "last_activity": None,
                }
            )
            continue

        try:
            # Parse timestamp
            last_activity = datetime.fromisoformat(
                last_activity_str.replace("Z", "+00:00")
            )

            # Make timezone-aware if naive
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)

            duration = now - last_activity

            if duration > threshold:
                stalled_tasks.append(
                    {
                        "task_id": task_id,
                        "title": title,
                        "assigned_to": task.get("assigned_to"),
                        "department": task.get("department"),
                        "stalled_reason": "no_recent_progress",
                        "stalled_duration_minutes": int(duration.total_seconds() / 60),
                        "last_activity": last_activity_str,
                    }
                )
        except (ValueError, TypeError):
            # Invalid timestamp
            stalled_tasks.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "assigned_to": task.get("assigned_to"),
                    "department": task.get("department"),
                    "stalled_reason": "invalid_timestamp",
                    "stalled_duration_minutes": None,
                    "last_activity": last_activity_str,
                }
            )

    # Also check blocked tasks that have been blocked too long
    stalled_blocked = []
    for task in queue.get("blocked", []):
        task_id = task.get("task_id", "unknown")
        created_at = task.get("created_at")

        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)

                blocked_duration = now - created
                # Blocked tasks stall at 3x the threshold
                if blocked_duration > threshold * 3:
                    stalled_blocked.append(
                        {
                            "task_id": task_id,
                            "title": task.get("title", "Untitled"),
                            "blocked_by": task.get("dependencies", []),
                            "blocked_duration_minutes": int(
                                blocked_duration.total_seconds() / 60
                            ),
                        }
                    )
            except (ValueError, TypeError):
                pass

    # Generate recommendations
    recommendations = []
    if stalled_tasks:
        recommendations.append(
            f"Review {len(stalled_tasks)} stalled in-progress task(s)"
        )
        agent_ids = set(
            t.get("assigned_to") for t in stalled_tasks if t.get("assigned_to")
        )
        if agent_ids:
            recommendations.append(f"Check on agents: {', '.join(agent_ids)}")
    if stalled_blocked:
        recommendations.append(f"Unblock {len(stalled_blocked)} long-blocked task(s)")

    # Note: notifications removed from this data-gathering function.
    # Callers (dashboard_aggregator, CLI) handle alerting with rate limiting.

    return {
        "success": True,
        "threshold_minutes": threshold_minutes,
        "stalled_in_progress": stalled_tasks,
        "stalled_in_progress_count": len(stalled_tasks),
        "long_blocked": stalled_blocked,
        "long_blocked_count": len(stalled_blocked),
        "total_stalled": len(stalled_tasks) + len(stalled_blocked),
        "recommendations": recommendations,
        "checked_at": now.isoformat(),
    }


def print_help() -> None:
    """Print usage help."""
    help_text = """
Progress Tracker — Track work progress across organization

Multi-project mode: When a .forge-company-root marker is found, the tracker
aggregates progress across all projects under the company root.

Commands:
    update      Update task progress and add notes
    company     Get company-wide progress summary (aggregated in multi-project mode)
    project     Get progress for a specific project
    projects    List all discovered projects
    department  Get department-specific progress
    stalled     Detect stalled work

Update options:
    --task-id ID        Task ID (required)
    --status STATUS     New status: pending|in_progress|blocked|completed
    --notes TEXT        Progress notes to add

Company options:
    --breakdown         Include per-project breakdown (multi-project mode only)

Project options:
    --project-id ID     Project ID (required, use 'projects' command to list)

Department options:
    --dept-id ID        Department ID (required)

Stalled options:
    --threshold MINS    Minutes without progress to consider stalled (default: 60)

Examples:
    # Update task progress
    python progress_tracker.py update --task-id TASK-123 --status in_progress --notes "Started work"

    # Get company progress (aggregated)
    python progress_tracker.py company

    # Get company progress with per-project breakdown
    python progress_tracker.py company --breakdown

    # List all projects
    python progress_tracker.py projects

    # Get specific project progress
    python progress_tracker.py project --project-id myproject-a1b2c3

    # Get engineering department progress
    python progress_tracker.py department --dept-id engineering

    # Find tasks stalled for 30+ minutes
    python progress_tracker.py stalled --threshold 30

Output: JSON with progress metrics and status.
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def main() -> None:
    """Entry point for the progress tracker CLI.

    Parses command-line arguments and dispatches to the appropriate
    progress-tracking function, printing JSON results to stdout.
    Exits with code 1 on errors, 0 on success.
    """
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "update":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = update_progress(
                task_id=args["task_id"],
                status=args.get("status"),
                notes=args.get("notes"),
            )
            print(json.dumps(result, indent=2))

        elif command == "company":
            include_breakdown = args.get("breakdown", False)
            result = get_company_progress(include_breakdown=bool(include_breakdown))
            print(json.dumps(result, indent=2))

        elif command == "project":
            if "project_id" not in args:
                print("Error: --project-id required")
                print("Use 'projects' command to list available projects")
                sys.exit(1)

            result = get_project_progress(args["project_id"])
            print(json.dumps(result, indent=2))

        elif command == "projects":
            result = list_projects()
            print(json.dumps(result, indent=2))

        elif command == "department":
            if "dept_id" not in args:
                print("Error: --dept-id required")
                sys.exit(1)

            result = get_department_progress(args["dept_id"])
            print(json.dumps(result, indent=2))

        elif command == "stalled":
            threshold = int(args.get("threshold", DEFAULT_STALL_THRESHOLD_MINUTES))
            result = detect_stalled_work(threshold)
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
