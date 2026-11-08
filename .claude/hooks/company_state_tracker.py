#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Company State Tracker — extends state tracking for company organization.

Wraps the original state_tracker.py functions and stores company-specific state
in a separate .planning/COMPANY_STATE.md file, leaving the original STATE.md
untouched for /build and /continue compatibility.

Features:
- Load/save company-specific state
- Track individual agent states and tasks
- Sync critical state to core STATE.md (optional)
- Independent company state storage

Usage:
    # Load company state
    python company_state_tracker.py load

    # Save company state with agent update
    python company_state_tracker.py save --agent-id eng-001 --status active --task TASK-123

    # Get specific agent state
    python company_state_tracker.py agent --agent-id eng-001

    # Update agent task assignment
    python company_state_tracker.py update-task --agent-id eng-001 --task-id TASK-123 --task-status in_progress

    # Sync company state to core STATE.md (optional metadata only)
    python company_state_tracker.py sync

    # Show help
    python company_state_tracker.py help
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

PLANNING_DIR = ".planning"
COMPANY_STATE_FILE = "COMPANY_STATE.md"
CORE_STATE_FILE = "STATE.md"
COMPANY_DIR = ".company"
ORG_FILE = "org.json"

# State tracker script location
STATE_TRACKER_PATH = ".claude/hooks/state_tracker.py"


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def get_planning_dir() -> Path:
    """Get the .planning directory path."""
    return Path(os.getcwd()) / PLANNING_DIR


def get_company_state_path() -> Path:
    """Get COMPANY_STATE.md path."""
    return get_planning_dir() / COMPANY_STATE_FILE


def get_core_state_path() -> Path:
    """Get core STATE.md path."""
    return get_planning_dir() / CORE_STATE_FILE


def get_org_path() -> Path:
    """Get org.json path."""
    return Path(os.getcwd()) / COMPANY_DIR / ORG_FILE


def get_state_tracker_path() -> Path:
    """Get the original state_tracker.py path."""
    return Path(os.getcwd()) / STATE_TRACKER_PATH


def get_git_info() -> dict:
    """Get current git state (mirrors state_tracker.py)."""
    info = {"branch": "unknown", "commit": "unknown"}
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()

        result = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            info["commit"] = result.stdout.strip()
    except Exception:
        pass
    return info


def load_org() -> dict:
    """Load organization structure from org.json."""
    org_path = get_org_path()
    if not org_path.exists():
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }

    try:
        with open(org_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "company": {"name": "Unknown"},
            "departments": [],
            "agents": [],
        }


# -----------------------------------------------------------------------------
# Company State Functions
# -----------------------------------------------------------------------------


def load_company_state() -> dict:
    """
    Load company state from COMPANY_STATE.md.

    Returns a dictionary with:
    - last_updated: timestamp
    - agents: dict mapping agent_id -> agent state
    - departments: dict mapping dept_id -> department state
    - active_tasks: list of currently active task assignments
    - session_context: any session-level context
    """
    state_path = get_company_state_path()

    default_state = {
        "last_updated": None,
        "agents": {},
        "departments": {},
        "active_tasks": [],
        "session_context": {},
        "git": get_git_info(),
    }

    if not state_path.exists():
        return default_state

    try:
        with open(state_path, encoding="utf-8") as f:
            content = f.read()

        # Parse the markdown file to extract JSON state block
        state = default_state.copy()

        # Look for Raw State JSON block (the complete state)
        if "## Raw State" in content:
            raw_section = content[content.index("## Raw State") :]
            if "```json" in raw_section:
                start = raw_section.index("```json") + 7
                end = raw_section.index("```", start)
                json_block = raw_section[start:end].strip()
                state = json.loads(json_block)
                state.setdefault("git", get_git_info())
        elif "```json" in content:
            # Fallback to first JSON block
            start = content.index("```json") + 7
            end = content.index("```", start)
            json_block = content[start:end].strip()
            state = json.loads(json_block)
            state.setdefault("git", get_git_info())
        else:
            # Parse markdown format
            state = _parse_markdown_state(content)

        return state

    except (json.JSONDecodeError, OSError, ValueError) as e:
        return {**default_state, "parse_error": str(e)}


def _parse_markdown_state(content: str) -> dict:
    """Parse markdown-formatted state file."""
    state = {
        "last_updated": None,
        "agents": {},
        "departments": {},
        "active_tasks": [],
        "session_context": {},
        "git": get_git_info(),
    }

    lines = content.split("\n")
    current_section = None
    current_agent = None

    for line in lines:
        line = line.strip()

        # Detect sections
        if line.startswith("## "):
            current_section = line[3:].lower()
            current_agent = None
            continue

        # Parse last updated
        if "**Last Updated:**" in line:
            state["last_updated"] = line.split("**Last Updated:**")[1].strip()

        # Parse agent entries
        if current_section == "agent states" and line.startswith("### "):
            current_agent = line[4:].strip()
            state["agents"][current_agent] = {
                "status": "unknown",
                "current_task": None,
                "last_active": None,
            }

        if current_agent and line.startswith("- **Status:**"):
            state["agents"][current_agent]["status"] = line.split(":**")[1].strip()
        if current_agent and line.startswith("- **Current Task:**"):
            task = line.split(":**")[1].strip()
            state["agents"][current_agent]["current_task"] = (
                task if task != "None" else None
            )
        if current_agent and line.startswith("- **Last Active:**"):
            state["agents"][current_agent]["last_active"] = line.split(":**")[1].strip()

    return state


def save_company_state(
    agents: dict | None = None,
    departments: dict | None = None,
    active_tasks: list | None = None,
    session_context: dict | None = None,
) -> dict:
    """
    Save company state to COMPANY_STATE.md.

    Args:
        agents: Dict of agent states to update (merged with existing)
        departments: Dict of department states to update (merged with existing)
        active_tasks: List of active task assignments (replaces existing)
        session_context: Session context dict (merged with existing)

    Returns:
        Dict with success status and saved state
    """
    state_path = get_company_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing state
    current_state = load_company_state()
    now = datetime.now(timezone.utc).isoformat()

    # Merge updates
    if agents:
        current_state["agents"].update(agents)
    if departments:
        current_state["departments"].update(departments)
    if active_tasks is not None:
        current_state["active_tasks"] = active_tasks
    if session_context:
        current_state["session_context"].update(session_context)

    current_state["last_updated"] = now
    current_state["git"] = get_git_info()

    # Generate markdown content
    content = _generate_state_markdown(current_state)

    try:
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "success": True,
            "path": str(state_path),
            "last_updated": now,
            "agent_count": len(current_state["agents"]),
            "active_task_count": len(current_state["active_tasks"]),
        }
    except OSError as e:
        return {
            "success": False,
            "error": str(e),
        }


def _generate_state_markdown(state: dict) -> str:
    """Generate markdown content for company state."""
    git = state.get("git", {})
    lines = [
        "# Company State",
        "",
        "<!-- Auto-updated by company_state_tracker.py -->",
        "<!-- Do NOT modify this file manually -->",
        "",
        "## Session Info",
        f"- **Last Updated:** {state.get('last_updated', 'Never')}",
        f"- **Branch:** {git.get('branch', 'unknown')}",
        f"- **Commit:** {git.get('commit', 'unknown')}",
        "",
    ]

    # Agent states section
    lines.extend(
        [
            "## Agent States",
            "",
        ]
    )

    agents = state.get("agents", {})
    if agents:
        for agent_id, agent_state in sorted(agents.items()):
            lines.append(f"### {agent_id}")
            lines.append(f"- **Status:** {agent_state.get('status', 'unknown')}")
            lines.append(
                f"- **Current Task:** {agent_state.get('current_task') or 'None'}"
            )
            lines.append(
                f"- **Last Active:** {agent_state.get('last_active', 'Never')}"
            )
            if agent_state.get("department"):
                lines.append(f"- **Department:** {agent_state['department']}")
            lines.append("")
    else:
        lines.append("_No agents registered_")
        lines.append("")

    # Active tasks section
    lines.extend(
        [
            "## Active Task Assignments",
            "",
            "| Agent | Task ID | Task Status | Assigned At |",
            "|-------|---------|-------------|-------------|",
        ]
    )

    active_tasks = state.get("active_tasks", [])
    if active_tasks:
        for task in active_tasks:
            lines.append(
                f"| {task.get('agent_id', '?')} "
                f"| {task.get('task_id', '?')} "
                f"| {task.get('status', '?')} "
                f"| {task.get('assigned_at', '?')} |"
            )
    else:
        lines.append("| - | - | - | - |")

    lines.append("")

    # Department summary section
    lines.extend(
        [
            "## Department Summary",
            "",
            "| Department | Active Agents | Tasks In Progress |",
            "|------------|---------------|-------------------|",
        ]
    )

    departments = state.get("departments", {})
    if departments:
        for dept_id, dept_state in sorted(departments.items()):
            lines.append(
                f"| {dept_id} "
                f"| {dept_state.get('active_agents', 0)} "
                f"| {dept_state.get('tasks_in_progress', 0)} |"
            )
    else:
        lines.append("| - | - | - |")

    lines.append("")

    # Session context section (JSON for flexibility)
    lines.extend(
        [
            "## Session Context",
            "",
            "```json",
            json.dumps(state.get("session_context", {}), indent=2),
            "```",
            "",
        ]
    )

    # Raw state (for programmatic access)
    lines.extend(
        [
            "## Raw State",
            "",
            "<!-- Used for programmatic parsing -->",
            "```json",
            json.dumps(state, indent=2),
            "```",
        ]
    )

    return "\n".join(lines)


def get_agent_state(agent_id: str) -> dict:
    """
    Get state for a specific agent.

    Args:
        agent_id: The agent identifier

    Returns:
        Dict with agent state or error
    """
    state = load_company_state()
    agents = state.get("agents", {})

    if agent_id not in agents:
        # Try to get from org.json
        org = load_org()
        org_agents = org.get("agents", [])
        org_agent = next((a for a in org_agents if a.get("id") == agent_id), None)

        if org_agent:
            return {
                "success": True,
                "agent_id": agent_id,
                "source": "org.json",
                "status": org_agent.get("status", "unknown"),
                "current_task": None,
                "last_active": None,
                "department": org_agent.get("department"),
                "capabilities": org_agent.get("capabilities", []),
            }

        return {
            "success": False,
            "error": f"Agent {agent_id} not found",
        }

    agent_state = agents[agent_id]
    return {
        "success": True,
        "agent_id": agent_id,
        "source": "company_state",
        **agent_state,
    }


def update_agent_task(
    agent_id: str,
    task_id: str | None,
    task_status: str = "in_progress",
    notes: str = "",
) -> dict:
    """
    Update an agent's current task assignment.

    Args:
        agent_id: The agent identifier
        task_id: Task ID to assign (None to clear)
        task_status: Task status (pending, in_progress, completed, blocked)
        notes: Optional notes about the assignment

    Returns:
        Dict with success status
    """
    state = load_company_state()
    now = datetime.now(timezone.utc).isoformat()

    # Update agent state
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = {
            "status": "active",
            "current_task": None,
            "last_active": None,
        }

    agent = state["agents"][agent_id]
    previous_task = agent.get("current_task")

    agent["current_task"] = task_id
    agent["last_active"] = now
    agent["status"] = "active" if task_id else "idle"

    # Update active tasks list
    active_tasks = state.get("active_tasks", [])

    # Remove any existing task for this agent
    active_tasks = [t for t in active_tasks if t.get("agent_id") != agent_id]

    # Add new task if assigned
    if task_id:
        active_tasks.append(
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "status": task_status,
                "assigned_at": now,
                "notes": notes,
            }
        )

    state["active_tasks"] = active_tasks

    # Save updated state
    result = save_company_state(
        agents=state["agents"],
        active_tasks=state["active_tasks"],
    )

    return {
        "success": result.get("success", False),
        "agent_id": agent_id,
        "previous_task": previous_task,
        "new_task": task_id,
        "task_status": task_status,
        "updated_at": now,
    }


def sync_to_core_state() -> dict:
    """
    Sync company state summary to core STATE.md context section.

    This adds a non-intrusive comment block to STATE.md with company summary,
    without modifying the core state tracking used by /build and /continue.

    Returns:
        Dict with sync status
    """
    company_state = load_company_state()
    core_state_path = get_core_state_path()

    if not core_state_path.exists():
        return {
            "success": False,
            "error": "Core STATE.md does not exist",
        }

    try:
        with open(core_state_path, encoding="utf-8") as f:
            content = f.read()

        # Build company summary block
        agents = company_state.get("agents", {})
        active_agents = [a for a, s in agents.items() if s.get("status") == "active"]
        active_tasks = company_state.get("active_tasks", [])

        summary_block = f"""
<!-- Company State Summary (from COMPANY_STATE.md) -->
<!-- Active Agents: {len(active_agents)} | Active Tasks: {len(active_tasks)} -->
<!-- Last Sync: {datetime.now(timezone.utc).isoformat()} -->
"""

        # Check if company block already exists
        marker_start = "<!-- Company State Summary"
        marker_end = "<!-- Last Sync:"

        if marker_start in content:
            # Find and replace existing block
            start_idx = content.index(marker_start)
            # Find end of the block (next line after Last Sync comment)
            end_marker_idx = content.index(marker_end, start_idx)
            end_idx = content.index("-->", end_marker_idx) + 3

            content = content[:start_idx] + summary_block.strip() + content[end_idx:]
        else:
            # Add after the "## Context Snapshot" section if it exists
            if "## Context Snapshot" in content:
                insert_point = content.index("## Context Snapshot")
                # Find end of context snapshot section
                next_section = content.find("## ", insert_point + 1)
                if next_section == -1:
                    next_section = len(content)
                content = (
                    content[:next_section] + summary_block + content[next_section:]
                )
            else:
                # Append at end
                content = content.rstrip() + "\n" + summary_block

        with open(core_state_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "success": True,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "active_agents": len(active_agents),
            "active_tasks": len(active_tasks),
        }

    except (OSError, ValueError) as e:
        return {
            "success": False,
            "error": str(e),
        }


def invoke_core_state_tracker(
    phase: str,
    task_id: str = "",
    task_name: str = "",
    status: str = "in-progress",
    next_task: str = "",
) -> dict:
    """
    Invoke the original state_tracker.py for core state updates.

    This wraps the original state tracker for cases where core state
    updates are needed alongside company state.

    Args:
        phase: Current phase
        task_id: Current task ID
        task_name: Current task name
        status: Status string
        next_task: Next task identifier

    Returns:
        Dict with invocation result
    """
    tracker_path = get_state_tracker_path()

    if not tracker_path.exists():
        return {
            "success": False,
            "error": f"Core state tracker not found at {tracker_path}",
        }

    try:
        cmd = [
            sys.executable,
            str(tracker_path),
            phase,
        ]
        if task_id:
            cmd.append(task_id)
        if task_name:
            cmd.append(task_name)
        if status:
            cmd.append(status)
        if next_task:
            cmd.append(next_task)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.getcwd(),
        )

        if result.returncode == 0:
            try:
                return {
                    "success": True,
                    "result": json.loads(result.stdout),
                }
            except json.JSONDecodeError:
                return {
                    "success": True,
                    "result": result.stdout,
                }
        else:
            return {
                "success": False,
                "error": result.stderr or "Unknown error",
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Core state tracker timed out",
        }
    except OSError as e:
        return {
            "success": False,
            "error": str(e),
        }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Company State Tracker - Extended state tracking for company organization

Commands:
    load            Load and display company state
    save            Save/update company state
    agent           Get specific agent state
    update-task     Update agent task assignment
    sync            Sync company summary to core STATE.md

Save options:
    --agent-id ID       Agent ID to update
    --status STATUS     Agent status (active, idle, busy, offline)
    --task TASK-ID      Current task assignment
    --dept-id ID        Department ID to update
    --context KEY=VAL   Add session context (can repeat)

Agent options:
    --agent-id ID       Agent ID (required)

Update-task options:
    --agent-id ID       Agent ID (required)
    --task-id ID        Task ID to assign (omit to clear)
    --task-status ST    Task status (pending, in_progress, completed, blocked)
    --notes TEXT        Assignment notes

Examples:
    # Load current company state
    python company_state_tracker.py load

    # Update agent state
    python company_state_tracker.py save --agent-id eng-001 --status active --task TASK-123

    # Get agent state
    python company_state_tracker.py agent --agent-id eng-001

    # Assign task to agent
    python company_state_tracker.py update-task --agent-id eng-001 --task-id TASK-123 --task-status in_progress

    # Clear agent task
    python company_state_tracker.py update-task --agent-id eng-001

    # Sync to core STATE.md
    python company_state_tracker.py sync

Output: JSON with state information.

Note: This tracker stores state in .planning/COMPANY_STATE.md, separate from
the core .planning/STATE.md used by /build and /continue commands.
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
        if command == "load":
            state = load_company_state()
            print(json.dumps(state, indent=2))

        elif command == "save":
            agents = None
            if "agent_id" in args:
                agent_state = {
                    "status": args.get("status", "active"),
                    "current_task": args.get("task"),
                    "last_active": datetime.now(timezone.utc).isoformat(),
                }
                if "dept_id" in args:
                    agent_state["department"] = args["dept_id"]
                agents = {args["agent_id"]: agent_state}

            departments = None
            if "dept_id" in args and "agent_id" not in args:
                departments = {
                    args["dept_id"]: {
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }
                }

            session_context = None
            if "context" in args:
                # Parse key=value format
                ctx = args["context"]
                if "=" in ctx:
                    key, val = ctx.split("=", 1)
                    session_context = {key: val}

            result = save_company_state(
                agents=agents,
                departments=departments,
                session_context=session_context,
            )
            print(json.dumps(result, indent=2))

        elif command == "agent":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)
            result = get_agent_state(args["agent_id"])
            print(json.dumps(result, indent=2))

        elif command == "update-task":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)
            result = update_agent_task(
                agent_id=args["agent_id"],
                task_id=args.get("task_id"),
                task_status=args.get("task_status", "in_progress"),
                notes=args.get("notes", ""),
            )
            print(json.dumps(result, indent=2))

        elif command == "sync":
            result = sync_to_core_state()
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
