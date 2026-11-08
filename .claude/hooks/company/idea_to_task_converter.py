"""
idea_to_task_converter.py — Convert approved employee ideas to work queue tasks.

Reads employee_ideas.json, finds ideas with status="approved", creates work queue
tasks from them via add_task(), and updates idea status to "queued".

Usage:
    # Preview what would be converted (dry run)
    uv run .claude/hooks/company/idea_to_task_converter.py --dry-run

    # Convert all approved ideas to tasks
    uv run .claude/hooks/company/idea_to_task_converter.py

    # Show status of all ideas
    uv run .claude/hooks/company/idea_to_task_converter.py --status
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"
IDEAS_FILE = COMPANY_DIR / "employee_ideas.json"

# Add hooks dir to path for imports
sys.path.insert(0, str(SCRIPT_DIR))
from work_allocator import add_task  # noqa: E402


def load_ideas() -> dict:
    """Load employee ideas from JSON file."""
    if not IDEAS_FILE.exists():
        return {"ideas": [], "metadata": {}}
    with open(IDEAS_FILE) as f:
        data = json.load(f)
    # Ensure metadata key exists to prevent KeyError on write
    if "metadata" not in data:
        data["metadata"] = {}
    return data


def save_ideas(data: dict) -> None:
    """Save employee ideas atomically."""
    import os
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(COMPANY_DIR), suffix=".json", prefix=".ideas_"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp_path, str(IDEAS_FILE))
    except Exception:
        os.unlink(tmp_path)
        raise


def notify_employee_idea_status(idea: dict, task_id: str) -> bool:
    """Append a notification to the submitting employee's memory file."""
    employee_id = idea.get("employee_id")
    if not employee_id:
        return False

    memory_path = COMPANY_DIR / "agents" / employee_id / "memory.md"
    if not memory_path.exists():
        return False

    idea_title = idea.get("title", "Untitled")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    notification = (
        f"\n\n## Idea Accepted: {idea_title}\n"
        f"**Date:** {now}\n"
        f"**Status:** Your idea has been approved and converted to work queue task `{task_id}`.\n"
        f"**Idea ID:** {idea.get('idea_id', 'unknown')}\n"
    )

    try:
        with open(memory_path, "a") as f:
            f.write(notification)
        return True
    except Exception:
        return False


def idea_to_task_description(idea: dict) -> str:
    """
    Build a task description from an idea.

    WS-108: Enhanced to preserve full evidence and context.
    """
    parts = [idea.get("description", "")]

    # Add rationale
    if idea.get("rationale"):
        parts.append("")
        parts.append(f"**Rationale:** {idea['rationale']}")

    # Add full evidence (not just titles)
    evidence = idea.get("evidence", [])
    if evidence:
        parts.append("")
        parts.append("**Evidence:**")
        for ev in evidence[:5]:  # First 5 evidence items
            if isinstance(ev, dict):
                ev_title = ev.get("title", ev.get("task_id", ""))
                ev_desc = ev.get("description", "")
                if ev_desc:
                    parts.append(f"- {ev_title}: {ev_desc[:100]}")
                else:
                    parts.append(f"- {ev_title}")
            else:
                parts.append(f"- {ev}")
        if len(evidence) > 5:
            parts.append(f"- ... and {len(evidence) - 5} more")

    # Add related tasks
    related_tasks = idea.get("related_tasks", [])
    if related_tasks:
        parts.append("")
        parts.append(f"**Related Tasks:** {', '.join(related_tasks[:5])}")

    # Add related employees for coordination
    related_employees = idea.get("related_employees", [])
    if related_employees:
        parts.append("")
        parts.append(f"**Coordinate With:** {', '.join(related_employees)}")

    # Add estimated value
    est_value = idea.get("estimated_value")
    if est_value is not None:
        parts.append("")
        parts.append(f"**Estimated Value:** {est_value:.2f}/1.0")

    # Add metadata footer
    parts.append("")
    parts.append("---")
    parts.append(f"**Idea ID:** {idea.get('idea_id', 'unknown')}")
    parts.append(f"**Proposed By:** {idea.get('employee_id', 'unknown')}")
    parts.append(f"**Type:** {idea.get('idea_type', 'unknown')}")

    return "\n".join(parts)


def idea_to_priority(idea: dict) -> int:
    """Map idea estimated_value to task priority."""
    value = idea.get("estimated_value", 0.5)
    if value >= 0.9:
        return 2  # High
    elif value >= 0.7:
        return 3  # Normal
    else:
        return 4  # Low


def idea_to_complexity(idea: dict) -> str:
    """Map idea estimated_effort to task complexity."""
    effort = idea.get("estimated_effort", "standard")
    mapping = {
        "trivial": "trivial",
        "small": "small",
        "standard": "standard",
        "complex": "complex",
        "epic": "epic",
    }
    return mapping.get(effort, "standard")


# ProjectK finding K5: generate_goal_decomposition_ideas / generate_product_vision_ideas
# (employee_ideation.py) tag ideas as [department, goal_id.lower(), marker] to track
# goal coverage. Neither the department name nor the goal id is an employee
# capability, so forwarding these tags verbatim as a task's required_capabilities
# either matches nobody (the common case) or matches an employee purely by
# coincidence (e.g. a department named "documentation" that happens to also be a
# listed writer capability) — never a real routing signal.
_STRUCTURAL_IDEA_TAG_MARKERS = {"goal-decomposition", "product-vision"}


def idea_tags_to_required_capabilities(tags: list[str]) -> list[str]:
    """
    Drop non-capability structural tags before using them to route a task.

    Ideas whose tags include a goal-coverage marker were stamped with
    [department, goal_id, marker] rather than real capabilities — leave
    required_capabilities empty for those so employee matching falls back to
    the fallback-employee path instead of a bogus or coincidental match.
    """
    if {t.lower() for t in tags} & _STRUCTURAL_IDEA_TAG_MARKERS:
        return []
    return tags


def has_approved_ideas() -> bool:
    """Cheap check for any idea in status 'approved', without converting.

    Used by the daemon to shrink its poll interval when approved ideas are
    waiting, so idle-backoff can't delay conversion by up to 30 minutes.
    """
    data = load_ideas()
    return any(i.get("status") == "approved" for i in data.get("ideas", []))


def convert_approved_ideas(dry_run: bool = False) -> list[dict]:
    """
    Convert all approved ideas to work queue tasks.

    Returns list of conversion results.
    """
    data = load_ideas()
    ideas = data.get("ideas", [])
    results = []

    approved = [i for i in ideas if i.get("status") == "approved"]

    if not approved:
        print("No approved ideas to convert.")
        return results

    print(f"Found {len(approved)} approved idea(s) to convert.\n")

    for idea in approved:
        idea_id = idea.get("idea_id", "unknown")
        title = idea.get("title", "Untitled idea")
        employee = idea.get("employee_id", "unknown")
        tags = idea.get("tags", [])

        task_title = f"[Employee Idea] {title}"
        description = idea_to_task_description(idea)
        priority = idea_to_priority(idea)
        complexity = idea_to_complexity(idea)

        print(f"  Idea: {idea_id}")
        print(f"  Title: {title}")
        print(f"  From: {employee}")
        print(f"  -> Task priority: P{priority}, complexity: {complexity}")

        if dry_run:
            print(f"  [DRY RUN] Would create task: {task_title}\n")
            results.append(
                {
                    "idea_id": idea_id,
                    "action": "would_create",
                    "task_title": task_title,
                }
            )
            continue

        # Create work queue task with attribution
        task_result = add_task(
            title=task_title,
            priority=priority,
            estimated_complexity=complexity,
            source="employee_initiative",
            description=description,
            required_capabilities=idea_tags_to_required_capabilities(tags),
            created_by=employee,
        )

        task_id = task_result.get("task_id", "unknown")
        success = task_result.get("success", True)
        error = task_result.get("error")

        if not success:
            if error == "admission_rejected":
                reason = task_result.get("reason", "unknown")
                print(f"  [REJECTED] Gate rejected: {reason}\n")
                idea["status"] = "rejected_by_gate"
                idea["rejected_at"] = datetime.now(timezone.utc).isoformat()
                idea["rejection_reason"] = reason
                results.append(
                    {"idea_id": idea_id, "action": "rejected", "reason": reason}
                )
            elif error in ("duplicate_task", "already_shipped"):
                existing_id = task_result.get("existing_task_id") or task_result.get(
                    "pr_number", "unknown"
                )
                print("  [DUPLICATE] Similar task already exists, skipping.\n")
                results.append(
                    {"idea_id": idea_id, "action": "duplicate", "task_id": existing_id}
                )
            else:
                print(f"  [ERROR] Task creation failed: {error}\n")
                results.append({"idea_id": idea_id, "action": "error", "error": error})
            continue

        # Update idea status to queued
        idea["status"] = "queued"
        idea["queued_at"] = datetime.now(timezone.utc).isoformat()
        idea["queued_task_id"] = task_id

        # WS-051-005: Notify submitting employee
        notified = notify_employee_idea_status(idea, task_id)
        notify_msg = " (employee notified)" if notified else ""

        print(f"  [CREATED] Task {task_id}{notify_msg}\n")
        results.append(
            {
                "idea_id": idea_id,
                "action": "created",
                "task_id": task_id,
                "task_title": task_title,
            }
        )

    if not dry_run and results:
        data.setdefault("metadata", {})["last_modified"] = datetime.now(
            timezone.utc
        ).isoformat()
        save_ideas(data)
        print(
            f"Updated {len([r for r in results if r['action'] == 'created'])} idea(s) to 'queued' status."
        )

    return results


def show_status() -> None:
    """Show current status of all ideas."""
    data = load_ideas()
    ideas = data.get("ideas", [])

    if not ideas:
        print("No ideas found.")
        return

    print(f"Total ideas: {len(ideas)}\n")
    print(f"{'Status':<20} {'Count'}")
    print("-" * 30)

    status_counts: dict[str, int] = {}
    for idea in ideas:
        s = idea.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    for status, count in sorted(status_counts.items()):
        print(f"{status:<20} {count}")

    print(f"\n{'ID':<35} {'Status':<18} {'Title'}")
    print("-" * 90)
    for idea in ideas:
        idea_id = idea.get("idea_id", "?")
        status = idea.get("status", "?")
        title = idea.get("title", "?")[:40]
        print(f"{idea_id:<35} {status:<18} {title}")


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        return

    if "--status" in args:
        show_status()
        return

    dry_run = "--dry-run" in args or "-n" in args
    results = convert_approved_ideas(dry_run=dry_run)

    # Summary
    created = len([r for r in results if r["action"] == "created"])
    dupes = len([r for r in results if r["action"] == "duplicate"])
    rejected = [r for r in results if r["action"] == "rejected"]
    if results:
        summary = f"{created} created, {dupes} duplicates"
        if rejected:
            reasons = ", ".join(r.get("reason", "unknown") for r in rejected)
            summary += f", {len(rejected)} rejected ({reasons})"
        print(f"\nSummary: {summary}")


if __name__ == "__main__":
    main()
