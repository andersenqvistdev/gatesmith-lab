#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Queue I/O utilities — Atomic read/write operations for work queue.

Provides thread-safe, atomic file operations to prevent queue corruption
during concurrent daemon operations.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def get_default_queue_path() -> Path:
    """Get the default work queue path."""
    # Search upward for .company directory
    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        for parent in [start] + list(start.parents):
            candidate = parent / ".company"
            if candidate.is_dir():
                state_path = candidate / "state" / "work_queue.json"
                if state_path.exists():
                    return state_path
                legacy_path = candidate / "work_queue.json"
                if legacy_path.exists():
                    return legacy_path
                # Return state path even if it doesn't exist yet
                return state_path
    return Path(".company/state/work_queue.json")


def get_empty_queue() -> dict:
    """Return an empty queue structure."""
    return {
        "pending": [],
        "in_progress": [],
        "blocked": [],
        "pr_open": [],
        "completed": [],
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": datetime.now(timezone.utc).isoformat(),
        },
    }


def load_queue(queue_path: Path | None = None) -> dict:
    """Load work queue from disk.

    Args:
        queue_path: Path to queue file. If None, uses default path.

    Returns:
        Queue dict, or empty queue if file doesn't exist.
    """
    if queue_path is None:
        queue_path = get_default_queue_path()

    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return get_empty_queue()


def save_queue_atomic(
    queue: dict,
    queue_path: Path | None = None,
    *,
    update_metadata: bool = True,
) -> bool:
    """Atomically save the work queue to disk.

    Uses tempfile + os.replace pattern to prevent corruption.
    The file is written completely to a temp file, then atomically
    renamed to the target path.

    Args:
        queue: The queue dict to persist.
        queue_path: Path to save to. If None, uses default path.
        update_metadata: Whether to update last_modified timestamp.

    Returns:
        True if save succeeded, False otherwise.
    """
    if queue_path is None:
        queue_path = get_default_queue_path()

    queue_path = Path(queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    if update_metadata:
        if "metadata" not in queue:
            queue["metadata"] = {}
        queue["metadata"]["last_modified"] = datetime.now(timezone.utc).isoformat()

    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(queue_path.parent),
            prefix=".wq_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(queue_path))
            return True
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except OSError:
        return False


def append_to_list(
    queue_path: Path | None,
    list_name: str,
    item: dict,
) -> bool:
    """Atomically append an item to a queue list.

    Args:
        queue_path: Path to queue file.
        list_name: One of "pending", "in_progress", "blocked", "completed".
        item: Item dict to append.

    Returns:
        True if successful.
    """
    queue = load_queue(queue_path)
    if list_name not in queue:
        queue[list_name] = []
    queue[list_name].append(item)
    return save_queue_atomic(queue, queue_path)


def move_task(
    queue_path: Path | None,
    task_id: str,
    from_list: str,
    to_list: str,
    updates: dict | None = None,
) -> bool:
    """Atomically move a task between queue lists.

    Args:
        queue_path: Path to queue file.
        task_id: ID of task to move.
        from_list: Source list name.
        to_list: Destination list name.
        updates: Optional dict of fields to update on the task.

    Returns:
        True if task was found and moved.
    """
    queue = load_queue(queue_path)

    # Find and remove from source
    task = None
    source = queue.get(from_list, [])
    for i, t in enumerate(source):
        if t.get("task_id") == task_id:
            task = source.pop(i)
            break

    if task is None:
        return False

    # Apply updates
    if updates:
        task.update(updates)

    # Add to destination
    if to_list not in queue:
        queue[to_list] = []
    queue[to_list].append(task)

    return save_queue_atomic(queue, queue_path)
