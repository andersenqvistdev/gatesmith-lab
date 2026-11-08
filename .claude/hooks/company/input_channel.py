#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Input Channel — human-to-company interface for work submission and escalation responses.

This module provides a clean facade over work_allocator.py and escalation.py,
serving as the single entry point for human-directed work requests and
escalation responses.

The input channel enables:
- Submitting new work requests with source="human"
- Responding to escalations that require human input (Tier 4)
- Listing all items pending human attention

Functions:
    submit_work_request() -> dict
        Submit human-directed work request to the queue

    respond_escalation() -> dict
        Respond to an escalation requiring human input

    list_pending_human_actions() -> dict
        List all items requiring human attention

Usage:
    # Submit a new work request
    python input_channel.py submit --title "Fix login bug" --priority 2

    # Respond to an escalation
    python input_channel.py respond --task-id "task-123" --response "Reassign to senior dev"

    # List items needing human attention
    python input_channel.py pending

    # Show help
    python input_channel.py help
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any

# Import work_allocator and escalation from the same directory
try:
    from . import escalation, work_allocator
except ImportError:
    # Fallback for direct script execution
    import escalation  # type: ignore
    import work_allocator  # type: ignore


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

VALID_COMPLEXITIES = {"trivial", "standard", "complex", "epic"}
VALID_PRIORITIES = {1, 2, 3, 4}


# -----------------------------------------------------------------------------
# Core Functions
# -----------------------------------------------------------------------------


def submit_work_request(
    title: str,
    description: str = "",
    priority: int = 3,
    department: str | None = None,
    required_capabilities: list[str] | None = None,
    deadline: str | None = None,
    estimated_complexity: str = "standard",
    project_id: str | None = None,
    requested_by: str = "human",
) -> dict:
    """
    Submit human-directed work request to the queue.

    This function wraps work_allocator.add_task() with source="human",
    ensuring all human-submitted work is properly tagged for tracking
    and priority handling.

    Args:
        title: Task title (required, non-empty)
        description: Detailed task description
        priority: 1-4 (1=Critical, 2=High, 3=Normal, 4=Low)
        department: Target department ID
        required_capabilities: List of required agent capabilities
        deadline: ISO timestamp deadline
        estimated_complexity: trivial|standard|complex|epic
        project_id: Project identifier (auto-detected if None)
        requested_by: Who submitted the request (default: "human")

    Returns:
        Dict with:
        - success: True if request submitted successfully
        - task_id: The assigned task ID
        - task: Full task object
        Or on error:
        - success: False
        - error: Error message
    """
    # Input validation
    if not title or not title.strip():
        return {
            "success": False,
            "error": "Title is required and cannot be empty",
        }

    title = title.strip()
    # The worker's prompt is built from the description; an empty one sent a
    # worker off with nothing but a title (found 2026-10-30, /submit).
    description = (description or "").strip() or title

    # Validate priority
    if priority not in VALID_PRIORITIES:
        return {
            "success": False,
            "error": f"Priority must be 1-4, got: {priority}",
            "valid_priorities": sorted(VALID_PRIORITIES),
        }

    # Validate complexity
    if estimated_complexity not in VALID_COMPLEXITIES:
        return {
            "success": False,
            "error": f"Invalid complexity: {estimated_complexity}",
            "valid_complexities": sorted(VALID_COMPLEXITIES),
        }

    # Validate deadline format if provided
    if deadline:
        try:
            # Attempt to parse ISO format
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return {
                "success": False,
                "error": f"Invalid deadline format: {deadline}. Use ISO 8601 format (e.g., 2025-01-15T10:00:00Z)",
            }

    # Validate required_capabilities is a list if provided
    if required_capabilities is not None:
        if not isinstance(required_capabilities, list):
            return {
                "success": False,
                "error": "required_capabilities must be a list of strings",
            }
        # Ensure all items are strings
        for cap in required_capabilities:
            if not isinstance(cap, str):
                return {
                    "success": False,
                    "error": f"Each capability must be a string, got: {type(cap).__name__}",
                }

    try:
        # Submit to work allocator with source="human"
        result = work_allocator.add_task(
            title=title,
            description=description,
            priority=priority,
            department=department,
            required_capabilities=required_capabilities,
            deadline=deadline,
            estimated_complexity=estimated_complexity,
            project_id=project_id,
            source="human",
        )

        # Augment result with request metadata
        if result.get("success"):
            result["requested_by"] = requested_by
            result["submitted_at"] = datetime.now(timezone.utc).isoformat()

        return result

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to submit work request: {str(e)}",
        }


def respond_escalation(
    task_id: str,
    response: str,
    responded_by: str = "human",
) -> dict:
    """
    Respond to an escalation requiring human input.

    This function wraps escalation.resolve_escalation() to provide
    human responses to Tier 4 escalations that require manual intervention.

    Args:
        task_id: The task ID of the escalation to respond to (required)
        response: The human response/resolution text (required)
        responded_by: Who provided the response (default: "human")

    Returns:
        Dict with escalation resolution result:
        - success: True if escalation resolved successfully
        - task_id: The task ID
        - resolution: The provided response
        - resolved_at: Timestamp of resolution
        - resolved_by: Who resolved it
        - escalation: Full escalation record
        Or on error:
        - success: False
        - error: Error message
    """
    # Input validation
    if not task_id or not task_id.strip():
        return {
            "success": False,
            "error": "task_id is required and cannot be empty",
        }

    if not response or not response.strip():
        return {
            "success": False,
            "error": "response is required and cannot be empty",
        }

    task_id = task_id.strip()
    response = response.strip()

    try:
        # Resolve the escalation
        result = escalation.resolve_escalation(
            task_id=task_id,
            resolution=response,
            resolved_by=responded_by,
        )

        return result

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to respond to escalation: {str(e)}",
        }


def list_pending_human_actions() -> dict:
    """
    List all items requiring human attention.

    This function queries the escalation system for Tier 4 (paused) items
    that require human input to proceed. These are escalations that have
    reached the highest tier and are waiting for manual intervention.

    Returns:
        Dict with:
        - success: True
        - escalations: List of escalations requiring human attention
        - count: Number of pending human actions
        - tier_info: Description of Tier 4 escalations
        Or on error:
        - success: False
        - error: Error message
    """
    try:
        # Query escalations at Tier 4 (HUMAN) with status "paused"
        # Tier 4 = Human intervention required
        escalations_list = escalation.list_escalations(
            tier=4,  # EscalationTier.HUMAN
            status="paused",
        )

        return {
            "success": True,
            "escalations": escalations_list,
            "count": len(escalations_list),
            "tier_info": {
                "tier": 4,
                "name": "Human",
                "description": "Escalations requiring human intervention",
                "expected_action": "Review and provide resolution or reassignment",
            },
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to list pending human actions: {str(e)}",
        }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Input Channel — human-to-company interface for work submission and escalation responses

Commands:
    submit      Submit a new work request
    respond     Respond to an escalation requiring human input
    pending     List all items requiring human attention
    help        Show this help message

Submit options:
    --title TEXT            Task title (required)
    --description TEXT      Detailed task description
    --priority 1-4          Priority (1=Critical, 4=Low, default=3)
    --department ID         Target department ID
    --capabilities LIST     Comma-separated required capabilities
    --deadline ISO          Deadline in ISO 8601 format
    --complexity STR        trivial|standard|complex|epic (default=standard)
    --project-id ID         Project ID (auto-detected if not specified)
    --requested-by ID       Who submitted the request (default=human)

Respond options:
    --task-id ID            Task ID of the escalation (required)
    --response TEXT         Human response/resolution (required)
    --responded-by ID       Who provided the response (default=human)

Examples:
    # Submit a high-priority work request
    python input_channel.py submit --title "Fix login bug" --priority 2

    # Submit with full details
    python input_channel.py submit --title "Add OAuth support" \\
        --description "Implement Google OAuth for user login" \\
        --priority 3 --department eng --complexity complex

    # Respond to an escalation
    python input_channel.py respond --task-id task-123 \\
        --response "Reassign to senior developer with OAuth experience"

    # List items needing human attention
    python input_channel.py pending
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result: dict[str, Any] = {}
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


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "submit":
            if "title" not in args:
                print("Error: --title required")
                sys.exit(1)

            capabilities = None
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            result = submit_work_request(
                title=args["title"],
                description=args.get("description", ""),
                priority=int(args.get("priority", 3)),
                department=args.get("department"),
                required_capabilities=capabilities,
                deadline=args.get("deadline"),
                estimated_complexity=args.get("complexity", "standard"),
                project_id=args.get("project_id"),
                requested_by=args.get("requested_by", "human"),
            )
            print(json.dumps(result, indent=2))

        elif command == "respond":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "response" not in args:
                print("Error: --response required")
                sys.exit(1)

            result = respond_escalation(
                task_id=args["task_id"],
                response=args["response"],
                responded_by=args.get("responded_by", "human"),
            )
            print(json.dumps(result, indent=2))

        elif command == "pending":
            result = list_pending_human_actions()
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
