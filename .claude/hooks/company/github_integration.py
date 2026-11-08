#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
WS-067-002: Bidirectional GitHub Integration

Outbound: Create GitHub issues for escalations
Inbound: Process comments on Gatesmith-created issues to update tasks

Usage:
    # Create issue for escalation
    uv run github_integration.py create --task-id <id>

    # Process incoming comment (called by webhook_receiver)
    uv run github_integration.py process-comment --issue <num> --comment <body>

    # Sync all open escalation issues
    uv run github_integration.py sync
"""

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("github_integration")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"
QUEUE_PATH = COMPANY_DIR / "state" / "work_queue.json"
GITHUB_ISSUES_PATH = COMPANY_DIR / "github_issues.json"

# Labels for Gatesmith-created issues
FORGE_LABEL = "forge-escalation"
PRIORITY_LABELS = {
    "priority:critical": 100,
    "priority:high": 75,
    "priority:medium": 50,
    "priority:low": 25,
}


def load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON file safely."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_json(path: Path, data: dict[str, Any]) -> bool:
    """Save JSON file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return False


def load_issue_mapping() -> dict[str, Any]:
    """Load task_id -> issue_number mapping."""
    data = load_json(GITHUB_ISSUES_PATH)
    return data if data else {"issues": {}, "updated_at": None}


def save_issue_mapping(mapping: dict[str, Any]) -> bool:
    """Save task_id -> issue_number mapping."""
    mapping["updated_at"] = datetime.now(timezone.utc).isoformat()
    return save_json(GITHUB_ISSUES_PATH, mapping)


def run_gh(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run gh CLI command."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=capture,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except FileNotFoundError:
        return 1, "", "gh CLI not found"


def ensure_label_exists() -> bool:
    """Ensure the forge-escalation label exists."""
    code, _, _ = run_gh(["label", "list", "--search", FORGE_LABEL])
    if code != 0:
        # Create the label
        run_gh(
            [
                "label",
                "create",
                FORGE_LABEL,
                "--description",
                "Issue created by Gatesmith daemon for escalation",
                "--color",
                "d93f0b",
            ]
        )
    return True


def create_issue_for_task(task: dict[str, Any]) -> int | None:
    """
    Create a GitHub issue for an escalated task.

    Returns the issue number if successful, None otherwise.
    """
    task_id = task.get("task_id", "unknown")
    title = task.get("title", "Untitled task")
    description = task.get("description", "")
    priority = task.get("priority", 50)
    source = task.get("source", "unknown")
    created_at = task.get("created_at", "unknown")

    # Check if issue already exists
    mapping = load_issue_mapping()
    if task_id in mapping.get("issues", {}):
        existing = mapping["issues"][task_id]
        logger.info(f"Issue already exists for {task_id}: #{existing['issue_number']}")
        return existing["issue_number"]

    ensure_label_exists()

    # Build issue body
    body = f"""## Gatesmith Escalation

**Task ID:** `{task_id}`
**Priority:** {priority}
**Source:** {source}
**Created:** {created_at}

---

### Description

{description}

---

### Commands

Reply to this issue with commands:

| Command | Action |
|---------|--------|
| `/close` | Close escalation, mark resolved |
| `/assign @employee-id` | Assign to specific employee |
| `/priority high` | Set priority (critical/high/medium/low) |
| `/blocked reason` | Mark as blocked |
| `/context info` | Add context to task |

---

*This issue was automatically created by [Gatesmith](https://github.com/gatesmithdev/gatesmith)*
"""

    # Determine priority label
    priority_label = "priority:medium"
    if priority >= 90:
        priority_label = "priority:critical"
    elif priority >= 70:
        priority_label = "priority:high"
    elif priority <= 30:
        priority_label = "priority:low"

    # Create the issue
    code, stdout, stderr = run_gh(
        [
            "issue",
            "create",
            "--title",
            f"[Gatesmith] {title}",
            "--body",
            body,
            "--label",
            FORGE_LABEL,
            "--label",
            priority_label,
        ]
    )

    if code != 0:
        logger.error(f"Failed to create issue: {stderr}")
        return None

    # Parse issue number from output (e.g., "https://github.com/user/repo/issues/123")
    match = re.search(r"/issues/(\d+)", stdout)
    if not match:
        logger.error(f"Could not parse issue number from: {stdout}")
        return None

    issue_number = int(match.group(1))

    # Save mapping
    mapping.setdefault("issues", {})[task_id] = {
        "issue_number": issue_number,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    save_issue_mapping(mapping)

    logger.info(f"Created issue #{issue_number} for task {task_id}")
    return issue_number


# Permission strings GitHub reports for a repo collaborator. Only these carry
# write access; "read", "none", and anything unrecognised are refused.
_AUTHORIZED_PERMISSIONS = frozenset({"admin", "maintain", "write"})


def _commenter_permission(commenter: str) -> str | None:
    """The commenter's repo permission per GitHub, or None when undeterminable.

    None means "could not establish", never "has no permission", so the caller
    can fail closed on an unanswerable question instead of guessing at one.
    """
    if not commenter or commenter == "unknown":
        return None
    code, stdout, _ = run_gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/collaborators/{commenter}/permission",
            "--jq",
            ".permission",
        ]
    )
    if code != 0:
        return None
    return stdout.strip() or None


def _is_authorized_commenter(commenter: str) -> bool:
    """True only when GitHub affirms the commenter has write access.

    Fails CLOSED. A missing gh CLI, a network error, an empty or unparseable
    response, and an unrecognised permission string all deny: a gate on
    privileged commands that fails open is not a gate.
    """
    return _commenter_permission(commenter) in _AUTHORIZED_PERMISSIONS


def process_comment(
    issue_number: int, comment_body: str, commenter: str
) -> dict[str, Any]:
    """
    Process a comment on a Gatesmith-created issue.

    Returns action taken.
    """
    result = {
        "issue_number": issue_number,
        "command": None,
        "action": None,
        "success": False,
    }

    # Authorization precedes everything, including the issue lookup. /close,
    # /assign, /priority and /blocked all mutate the work queue, and a GitHub
    # issue comment is attacker-supplied text from anyone who can reach the
    # repo. Before this gate `commenter` was read only to compose the audit
    # line ("Closed by @{commenter} via /close command"), so the login was
    # recorded but never checked — any commenter could drive the queue.
    if not _is_authorized_commenter(commenter):
        result["action"] = "unauthorized"
        return result

    # Find the task for this issue
    mapping = load_issue_mapping()
    task_id = None
    for tid, info in mapping.get("issues", {}).items():
        if info.get("issue_number") == issue_number:
            task_id = tid
            break

    if not task_id:
        result["action"] = "unknown_issue"
        return result

    # Parse command from comment
    comment_lower = comment_body.lower().strip()

    # /close command
    if comment_lower.startswith("/close"):
        result["command"] = "close"
        result["action"] = "resolve_escalation"

        # Close the GitHub issue
        run_gh(
            [
                "issue",
                "close",
                str(issue_number),
                "-c",
                f"Closed by @{commenter} via /close command",
            ]
        )

        # Update mapping
        if task_id in mapping.get("issues", {}):
            mapping["issues"][task_id]["status"] = "closed"
            mapping["issues"][task_id]["closed_by"] = commenter
            mapping["issues"][task_id]["closed_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            save_issue_mapping(mapping)

        # Remove from escalations
        _resolve_escalation(task_id)
        result["success"] = True

    # /assign command
    elif comment_lower.startswith("/assign"):
        match = re.search(r"/assign\s+@?(\S+)", comment_body, re.IGNORECASE)
        if match:
            employee_id = match.group(1)
            result["command"] = "assign"
            result["action"] = f"assign_to_{employee_id}"
            result["success"] = _assign_task(task_id, employee_id)

            if result["success"]:
                run_gh(
                    [
                        "issue",
                        "comment",
                        str(issue_number),
                        "-b",
                        f"Task assigned to `{employee_id}` by @{commenter}",
                    ]
                )

    # /priority command
    elif comment_lower.startswith("/priority"):
        match = re.search(
            r"/priority\s+(critical|high|medium|low)", comment_body, re.IGNORECASE
        )
        if match:
            level = match.group(1).lower()
            priority_value = {"critical": 95, "high": 75, "medium": 50, "low": 25}[
                level
            ]
            result["command"] = "priority"
            result["action"] = f"set_priority_{level}"
            result["success"] = _update_task_priority(task_id, priority_value)

            if result["success"]:
                # Update label
                for label in PRIORITY_LABELS:
                    run_gh(
                        ["issue", "edit", str(issue_number), "--remove-label", label]
                    )
                run_gh(
                    [
                        "issue",
                        "edit",
                        str(issue_number),
                        "--add-label",
                        f"priority:{level}",
                    ]
                )
                run_gh(
                    [
                        "issue",
                        "comment",
                        str(issue_number),
                        "-b",
                        f"Priority set to `{level}` by @{commenter}",
                    ]
                )

    # /blocked command
    elif comment_lower.startswith("/blocked"):
        reason = (
            comment_body[8:].strip() if len(comment_body) > 8 else "No reason specified"
        )
        result["command"] = "blocked"
        result["action"] = "mark_blocked"
        result["success"] = _mark_task_blocked(task_id, reason)

        if result["success"]:
            run_gh(["issue", "edit", str(issue_number), "--add-label", "blocked"])
            run_gh(
                [
                    "issue",
                    "comment",
                    str(issue_number),
                    "-b",
                    f"Task marked as blocked: {reason}\n\n— @{commenter}",
                ]
            )

    # /context command
    elif comment_lower.startswith("/context"):
        context = comment_body[8:].strip()
        result["command"] = "context"
        result["action"] = "add_context"
        result["success"] = _add_task_context(task_id, context, commenter)

    # No command - just a regular comment, add as context
    elif not comment_lower.startswith("/"):
        result["command"] = "comment"
        result["action"] = "add_context"
        result["success"] = _add_task_context(task_id, comment_body, commenter)

    return result


def _resolve_escalation(task_id: str) -> bool:
    """Remove task from escalations."""
    esc_dir = COMPANY_DIR / "escalations"
    for fp in esc_dir.glob("*.json"):
        try:
            data = load_json(fp)
            if data and data.get("task_id") == task_id:
                data["status"] = "resolved"
                data["resolved_at"] = datetime.now(timezone.utc).isoformat()
                data["resolved_via"] = "github_comment"
                save_json(fp, data)

                # Move to archive
                archive_dir = COMPANY_DIR / "archive" / "escalations"
                archive_dir.mkdir(parents=True, exist_ok=True)
                fp.rename(archive_dir / fp.name)
                return True
        except Exception:
            continue
    return False


def _assign_task(task_id: str, employee_id: str) -> bool:
    """Assign task to employee in queue."""
    queue = load_json(QUEUE_PATH)
    if not queue:
        return False

    for status in ["pending", "in_progress", "blocked"]:
        for task in queue.get(status, []):
            if task.get("task_id") == task_id:
                task["assigned_to"] = employee_id
                task["assigned_at"] = datetime.now(timezone.utc).isoformat()
                task["assigned_via"] = "github_comment"
                return save_json(QUEUE_PATH, queue)
    return False


def _update_task_priority(task_id: str, priority: int) -> bool:
    """Update task priority in queue."""
    queue = load_json(QUEUE_PATH)
    if not queue:
        return False

    for status in ["pending", "in_progress", "blocked"]:
        for task in queue.get(status, []):
            if task.get("task_id") == task_id:
                task["priority"] = priority
                task["priority_updated_at"] = datetime.now(timezone.utc).isoformat()
                task["priority_updated_via"] = "github_comment"
                return save_json(QUEUE_PATH, queue)
    return False


def _mark_task_blocked(task_id: str, reason: str) -> bool:
    """Mark task as blocked."""
    queue = load_json(QUEUE_PATH)
    if not queue:
        return False

    # Find and move task to blocked
    for status in ["pending", "in_progress"]:
        for i, task in enumerate(queue.get(status, [])):
            if task.get("task_id") == task_id:
                task["blocked_reason"] = reason
                task["blocked_at"] = datetime.now(timezone.utc).isoformat()
                task["blocked_via"] = "github_comment"
                queue.setdefault("blocked", []).append(task)
                queue[status].pop(i)
                return save_json(QUEUE_PATH, queue)
    return False


def _add_task_context(task_id: str, context: str, commenter: str) -> bool:
    """Add context to task."""
    queue = load_json(QUEUE_PATH)
    if not queue:
        return False

    for status in ["pending", "in_progress", "blocked"]:
        for task in queue.get(status, []):
            if task.get("task_id") == task_id:
                task.setdefault("context", []).append(
                    {
                        "text": context,
                        "from": commenter,
                        "source": "github_comment",
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                return save_json(QUEUE_PATH, queue)
    return False


def sync_issues() -> dict[str, Any]:
    """Sync status of all Gatesmith-created issues."""
    mapping = load_issue_mapping()
    results = {"checked": 0, "closed": 0, "errors": 0}

    for task_id, info in mapping.get("issues", {}).items():
        if info.get("status") == "closed":
            continue

        issue_num = info.get("issue_number")
        if not issue_num:
            continue

        results["checked"] += 1

        # Check issue state
        code, stdout, _ = run_gh(
            ["issue", "view", str(issue_num), "--json", "state,comments"]
        )

        if code != 0:
            results["errors"] += 1
            continue

        try:
            data = json.loads(stdout)
            if data.get("state") == "CLOSED":
                info["status"] = "closed"
                results["closed"] += 1
        except json.JSONDecodeError:
            results["errors"] += 1

    save_issue_mapping(mapping)
    return results


def create_issues_for_escalations() -> list[int]:
    """Create GitHub issues for all active escalations."""
    created = []
    esc_dir = COMPANY_DIR / "escalations"

    if not esc_dir.is_dir():
        return created

    for fp in esc_dir.glob("*.json"):
        try:
            data = load_json(fp)
            if not data or data.get("status") == "resolved":
                continue

            # Build task-like dict from escalation
            task = {
                "task_id": data.get("task_id", fp.stem),
                "title": data.get("title", data.get("task_id", "Unknown")),
                "description": data.get("description", data.get("reason", "")),
                "priority": data.get("priority", 85),
                "source": "escalation",
                "created_at": data.get("created_at", "unknown"),
            }

            issue_num = create_issue_for_task(task)
            if issue_num:
                created.append(issue_num)

        except Exception as e:
            logger.error(f"Error processing {fp}: {e}")

    return created


def main():
    """CLI entry point."""
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h", "help"):
        print(__doc__)
        sys.exit(0)

    command = args[0]

    if command == "create":
        # Create issue for a specific task
        task_id = None
        for i, arg in enumerate(args):
            if arg == "--task-id" and i + 1 < len(args):
                task_id = args[i + 1]

        if not task_id:
            print("Usage: github_integration.py create --task-id <id>")
            sys.exit(1)

        # Load task from queue
        queue = load_json(QUEUE_PATH)
        task = None
        if queue:
            for status in ["pending", "in_progress", "blocked"]:
                for t in queue.get(status, []):
                    if t.get("task_id") == task_id:
                        task = t
                        break

        if not task:
            print(f"Task {task_id} not found in queue")
            sys.exit(1)

        issue_num = create_issue_for_task(task)
        if issue_num:
            print(f"Created issue #{issue_num}")
        else:
            print("Failed to create issue")
            sys.exit(1)

    elif command == "process-comment":
        # Process incoming comment
        issue_num = None
        comment_body = None
        commenter = "unknown"

        for i, arg in enumerate(args):
            if arg == "--issue" and i + 1 < len(args):
                issue_num = int(args[i + 1])
            elif arg == "--comment" and i + 1 < len(args):
                comment_body = args[i + 1]
            elif arg == "--commenter" and i + 1 < len(args):
                commenter = args[i + 1]

        if not issue_num or not comment_body:
            print(
                "Usage: github_integration.py process-comment --issue <num> --comment <body>"
            )
            sys.exit(1)

        result = process_comment(issue_num, comment_body, commenter)
        print(json.dumps(result, indent=2))

    elif command == "sync":
        # Sync all issues
        results = sync_issues()
        print(
            f"Synced: {results['checked']} issues, {results['closed']} closed, {results['errors']} errors"
        )

    elif command == "create-all":
        # Create issues for all escalations
        created = create_issues_for_escalations()
        print(f"Created {len(created)} issues: {created}")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
