#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Work Queue Validator — Schema validation and health checks for work_queue.json.

This script validates the work queue structure, identifies malformed entries,
and provides guards to prevent future corruption.

Usage:
    python work_queue_validator.py validate          # Full validation report
    python work_queue_validator.py check             # Quick health check (exit code 0/1)
    python work_queue_validator.py fix               # Auto-fix recoverable issues
    python work_queue_validator.py schema            # Print JSON schema
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Schema Definition ────────────────────────────────────────────────────────

TASK_SCHEMA = {
    "required": [
        "task_id",
        "title",
        "description",
        "priority",
        "required_capabilities",
        "source",
    ],
    "optional": [
        "department",
        "project_id",
        "created_at",
        "deadline",
        "estimated_complexity",
        "dependencies",
        "dependencies_satisfied",
        "assigned_to",
        "assigned_at",
        "started_at",
        "completed_at",
        "source_project_id",
        "target_project_id",
        "routing_reason",
        "routing_history",
        "cross_project",
        "allowed_projects",
        "claimed_by",
        "claimed_at",
        "retry_count",
        "retry_history",
        "status",
        "result",
        "backoff_until",
        "last_error",
        "submitted_at",
    ],
    # Extended fields added by specific processes (not errors)
    "extended": [
        "proposed_at",
        "proposed_by",
        "proposal_type",
        "review_notes",
        "approved_by",
        "approved_at",
        "rejected_by",
        "rejected_at",
        "rejection_reason",
        "failed_at",
    ],
}

VALID_PRIORITIES = {1, 2, 3, 4, 5}
VALID_COMPLEXITY = {"trivial", "standard", "complex", "epic", None}
VALID_QUEUES = {"pending", "in_progress", "blocked", "completed"}
# Additional valid top-level keys (not task queues)
VALID_TOP_LEVEL_KEYS = VALID_QUEUES | {"metadata", "proposed"}
MAX_CAPABILITY_LENGTH = 40
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


# ── Utility Functions ────────────────────────────────────────────────────────


def get_company_dir() -> Path:
    """Get the .company directory path."""
    return Path(__file__).parent.parent.parent.parent / ".company"


def load_work_queue() -> dict:
    """Load the work queue JSON file."""
    queue_path = get_company_dir() / "state/work_queue.json"
    if queue_path.exists():
        with open(queue_path, encoding="utf-8") as f:
            return json.load(f)
    return {"pending": [], "in_progress": [], "blocked": [], "completed": []}


def save_work_queue(data: dict) -> None:
    """Save the work queue JSON file (atomic + locked)."""
    import os
    import tempfile

    # P80: Prevent pytest from overwriting production queue.
    _real_company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    _is_pytest = "PYTEST_CURRENT_TEST" in os.environ or any(
        "pytest" in str(v) for v in [os.environ.get("_", "")]
    )
    if _is_pytest:
        try:
            _candidate = get_company_dir() / "state/work_queue.json"
            if _candidate.resolve().is_relative_to(_real_company.resolve()):
                return  # Silently skip — never overwrite production queue from tests
        except (ValueError, OSError):
            pass

    company_dir = get_company_dir()
    queue_path = company_dir / "state/work_queue.json"
    lock_path = company_dir / "runtime/queue.lock"

    # Import QueueLock for safe concurrent access
    _QueueLock = None
    try:
        from work_allocator import QueueLock as _QueueLock
    except ImportError:
        try:
            hooks_dir = str(Path(__file__).resolve().parent)
            if hooks_dir not in sys.path:
                sys.path.insert(0, hooks_dir)
            from work_allocator import QueueLock as _QueueLock
        except ImportError:
            pass

    if _QueueLock is not None:
        lock_ctx = _QueueLock(lock_path)
    else:
        from contextlib import nullcontext

        lock_ctx = nullcontext()

    with lock_ctx:
        fd, tmp = tempfile.mkstemp(
            dir=str(queue_path.parent), prefix=".wqv_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, str(queue_path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ── Validation Functions ─────────────────────────────────────────────────────


def validate_capability(cap: Any) -> tuple[bool, str]:
    """Validate a single capability string."""
    if not isinstance(cap, str):
        return False, f"capability must be string, got {type(cap).__name__}"

    if "\n" in cap or "\t" in cap or "\r" in cap:
        return False, "capability contains control characters"

    if ". " in cap or " is " in cap or " the " in cap.lower():
        return False, "capability appears to be prose"

    if len(cap) > MAX_CAPABILITY_LENGTH:
        return False, f"capability too long ({len(cap)} > {MAX_CAPABILITY_LENGTH})"

    cleaned = cap.strip().lower()
    if not cleaned:
        return False, "capability is empty"

    if not CAPABILITY_PATTERN.match(cleaned):
        return False, f"capability '{cleaned}' contains invalid characters"

    return True, cleaned


def validate_iso_date(value: Any) -> bool:
    """Check if value is a valid ISO date string."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_task(task: dict, queue_name: str, index: int) -> tuple[str, list, list]:
    """
    Validate a single task against the schema.

    Returns:
        Tuple of (task_id, errors, warnings)
    """
    errors = []
    warnings = []
    task_id = task.get("task_id", f"UNKNOWN-{index}")

    # Check required fields
    for field in TASK_SCHEMA["required"]:
        if field not in task:
            errors.append(f"Missing required field: {field}")

    # Validate priority
    priority = task.get("priority")
    if priority is not None and priority not in VALID_PRIORITIES:
        errors.append(f"Invalid priority: {priority} (must be 1-5)")

    # Validate complexity
    complexity = task.get("estimated_complexity")
    if complexity not in VALID_COMPLEXITY:
        warnings.append(f"Unusual complexity: {complexity}")

    # Validate capabilities
    caps = task.get("required_capabilities", [])
    if isinstance(caps, str):
        errors.append("required_capabilities is string, not list")
    elif isinstance(caps, list):
        for i, cap in enumerate(caps):
            valid, msg = validate_capability(cap)
            if not valid:
                errors.append(f"capability[{i}]: {msg}")
    else:
        errors.append(f"required_capabilities is {type(caps).__name__}, not list")

    # Validate date fields
    date_fields = [
        "created_at",
        "assigned_at",
        "started_at",
        "completed_at",
        "claimed_at",
        "backoff_until",
    ]
    for field in date_fields:
        if field in task and not validate_iso_date(task.get(field)):
            errors.append(f"Invalid date format for {field}")

    # Validate dependencies
    deps = task.get("dependencies")
    if deps is not None and not isinstance(deps, list):
        errors.append(f"dependencies must be list, got {type(deps).__name__}")

    # State consistency checks
    if queue_name == "completed" and not task.get("completed_at"):
        warnings.append("Completed task missing completed_at")
    if queue_name == "in_progress" and not task.get("assigned_to"):
        warnings.append("In-progress task missing assigned_to")

    return task_id, errors, warnings


def validate_queue_structure(data: dict) -> list[str]:
    """Validate the top-level queue structure."""
    errors = []

    for queue in VALID_QUEUES:
        if queue not in data:
            errors.append(f"Missing queue: {queue}")
        elif not isinstance(data[queue], list):
            errors.append(f"Queue {queue} is not a list")

    # Check for unexpected top-level keys
    for key in data:
        if key not in VALID_TOP_LEVEL_KEYS:
            errors.append(f"Unexpected top-level key: {key}")

    return errors


def check_duplicate_ids(data: dict) -> list[str]:
    """Check for duplicate task IDs across all queues."""
    all_ids = []
    errors = []

    for queue_name in VALID_QUEUES:
        for task in data.get(queue_name, []):
            task_id = task.get("task_id")
            if task_id in all_ids:
                errors.append(f"Duplicate task_id: {task_id} in {queue_name}")
            all_ids.append(task_id)

    return errors


# ── Fix Functions ────────────────────────────────────────────────────────────


def fix_malformed_capabilities(task: dict) -> int:
    """Fix malformed capabilities in a task. Returns count of fixes."""
    fixes = 0
    caps = task.get("required_capabilities", [])

    if not isinstance(caps, list):
        return 0

    cleaned = []
    for cap in caps:
        valid, result = validate_capability(cap)
        if valid:
            cleaned.append(result)
        else:
            fixes += 1

    if fixes > 0:
        task["required_capabilities"] = cleaned

    return fixes


def fix_queue(data: dict) -> dict[str, int]:
    """Apply auto-fixes to the work queue. Returns fix counts."""
    fixes = {
        "capabilities_fixed": 0,
        "tasks_removed": 0,
    }

    for queue_name in VALID_QUEUES:
        queue = data.get(queue_name, [])
        for task in queue:
            cap_fixes = fix_malformed_capabilities(task)
            fixes["capabilities_fixed"] += cap_fixes

    return fixes


# ── Command Handlers ─────────────────────────────────────────────────────────


def cmd_validate() -> int:
    """Full validation report. Returns exit code."""
    data = load_work_queue()
    all_errors = []
    all_warnings = []

    print("=" * 60)
    print("WORK QUEUE VALIDATION REPORT")
    print("=" * 60)

    # Structure validation
    struct_errors = validate_queue_structure(data)
    if struct_errors:
        all_errors.extend(struct_errors)
        print("\n✗ STRUCTURE ERRORS:")
        for err in struct_errors:
            print(f"  - {err}")
    else:
        print("\n✓ Structure valid")

    # Duplicate check
    dup_errors = check_duplicate_ids(data)
    if dup_errors:
        all_errors.extend(dup_errors)
        print("\n✗ DUPLICATE IDS:")
        for err in dup_errors:
            print(f"  - {err}")
    else:
        print("✓ No duplicate task IDs")

    # Task validation
    for queue_name in VALID_QUEUES:
        queue = data.get(queue_name, [])
        queue_errors = 0
        queue_warnings = 0

        for i, task in enumerate(queue):
            task_id, errors, warnings = validate_task(task, queue_name, i)
            if errors:
                queue_errors += len(errors)
                all_errors.append((queue_name, task_id, errors))
            if warnings:
                queue_warnings += len(warnings)
                all_warnings.append((queue_name, task_id, warnings))

        status = "✓" if queue_errors == 0 else "✗"
        print(
            f"{status} {queue_name.upper()}: {len(queue)} tasks "
            f"({queue_errors} errors, {queue_warnings} warnings)"
        )

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    total_tasks = sum(len(data.get(q, [])) for q in VALID_QUEUES)
    print(f"Total tasks: {total_tasks}")
    print(f"Errors: {len(all_errors)}")
    print(f"Warnings: {len(all_warnings)}")

    if all_errors:
        print("\n--- ERRORS ---")
        for item in all_errors[:10]:
            if isinstance(item, tuple):
                queue, task_id, errs = item
                print(f"[{queue}] {task_id}:")
                for err in errs:
                    print(f"  - {err}")
            else:
                print(f"  - {item}")
        if len(all_errors) > 10:
            print(f"  ... and {len(all_errors) - 10} more errors")

    status = "HEALTHY" if len(all_errors) == 0 else "NEEDS ATTENTION"
    print(f"\n{'=' * 60}")
    print(f"STATUS: {status}")
    print(f"{'=' * 60}")

    return 0 if len(all_errors) == 0 else 1


def cmd_check() -> int:
    """Quick health check. Returns 0 if healthy, 1 if errors."""
    data = load_work_queue()

    errors = []
    errors.extend(validate_queue_structure(data))
    errors.extend(check_duplicate_ids(data))

    for queue_name in VALID_QUEUES:
        for i, task in enumerate(data.get(queue_name, [])):
            _, task_errors, _ = validate_task(task, queue_name, i)
            errors.extend(task_errors)

    if errors:
        result = {"success": False, "errors": len(errors), "sample": errors[:3]}
    else:
        total = sum(len(data.get(q, [])) for q in VALID_QUEUES)
        result = {"success": True, "total_tasks": total}

    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def cmd_fix() -> int:
    """Auto-fix recoverable issues."""
    data = load_work_queue()
    fixes = fix_queue(data)

    if fixes["capabilities_fixed"] > 0 or fixes["tasks_removed"] > 0:
        save_work_queue(data)
        print(json.dumps({"success": True, "fixes": fixes}, indent=2))
    else:
        print(json.dumps({"success": True, "message": "No fixes needed"}, indent=2))

    return 0


def cmd_schema() -> int:
    """Print the task schema."""
    schema = {
        "task": {
            "required_fields": TASK_SCHEMA["required"],
            "optional_fields": TASK_SCHEMA["optional"],
            "extended_fields": TASK_SCHEMA["extended"],
        },
        "constraints": {
            "valid_priorities": list(VALID_PRIORITIES),
            "valid_complexity": [c for c in VALID_COMPLEXITY if c],
            "max_capability_length": MAX_CAPABILITY_LENGTH,
            "capability_pattern": CAPABILITY_PATTERN.pattern,
        },
        "queues": list(VALID_QUEUES),
    }
    print(json.dumps(schema, indent=2))
    return 0


def print_help():
    """Print usage help."""
    print(__doc__)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)
    elif command == "validate":
        sys.exit(cmd_validate())
    elif command == "check":
        sys.exit(cmd_check())
    elif command == "fix":
        sys.exit(cmd_fix())
    elif command == "schema":
        sys.exit(cmd_schema())
    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
