# /// script
# requires-python = ">=3.10"
# ///
"""
Session State Tracker — enables pause/resume across sessions (from GSD).

Updates .planning/STATE.md after significant actions.
Utility script called by other hooks/commands.

Extended for company phase management (v1.6):
- get-company-phase: Returns current phase from STATE.md
- set-company-phase <phase>: Sets phase with timestamp
- confirm-transition: Confirms pending transition
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Company phase constants
VALID_PHASES = ["startup", "growth", "scale", "mature", "decline_pivot"]


def get_state_path() -> str:
    """Get the path to STATE.md."""
    return os.path.join(os.getcwd(), ".planning", "STATE.md")


def read_state_file() -> str:
    """Read STATE.md contents, return empty string if not exists."""
    state_path = get_state_path()
    if os.path.exists(state_path):
        with open(state_path) as f:
            return f.read()
    return ""


def write_state_file(content: str) -> None:
    """Write content to STATE.md."""
    state_path = get_state_path()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        f.write(content)


def get_iso_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_company_phase_section(content: str) -> dict:
    """Parse the Company Phase section from STATE.md.

    Returns dict with keys: phase, since, metrics_snapshot, last_assessment, transition_pending
    """
    result = {
        "phase": None,
        "since": None,
        "metrics_snapshot": None,
        "last_assessment": None,
        "transition_pending": None,
    }

    # Find the Company Phase section
    match = re.search(r"## Company Phase\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not match:
        return result

    section = match.group(1)

    # Parse each field
    phase_match = re.search(r"\*\*Current Phase:\*\* (\w+)", section)
    if phase_match:
        result["phase"] = phase_match.group(1)

    since_match = re.search(r"\*\*Since:\*\* ([\dT:\-Z]+)", section)
    if since_match:
        result["since"] = since_match.group(1)

    metrics_match = re.search(r"\*\*Metrics Snapshot:\*\* (\{[^}]+\})", section)
    if metrics_match:
        result["metrics_snapshot"] = metrics_match.group(1)

    assessment_match = re.search(r"\*\*Last Assessment:\*\* ([\dT:\-Z]+)", section)
    if assessment_match:
        result["last_assessment"] = assessment_match.group(1)

    transition_match = re.search(r"\*\*Transition Pending:\*\* (\w+)", section)
    if transition_match:
        result["transition_pending"] = transition_match.group(1)

    return result


def format_company_phase_section(
    phase: str,
    since: str,
    metrics_snapshot: str | None = None,
    last_assessment: str | None = None,
    transition_pending: str | None = None,
) -> str:
    """Format the Company Phase section for STATE.md."""
    lines = [
        "## Company Phase",
        f"- **Current Phase:** {phase}",
        f"- **Since:** {since}",
    ]

    if metrics_snapshot:
        lines.append(f"- **Metrics Snapshot:** {metrics_snapshot}")

    if last_assessment:
        lines.append(f"- **Last Assessment:** {last_assessment}")

    if transition_pending:
        lines.append(
            f"- **Transition Pending:** {transition_pending} (awaiting confirmation)"
        )

    return "\n".join(lines)


def update_company_phase_section(content: str, new_section: str) -> str:
    """Replace or insert the Company Phase section in STATE.md content."""
    # Check if section exists
    if "## Company Phase" in content:
        # Replace existing section
        pattern = r"## Company Phase\n.*?(?=\n## |\Z)"
        return re.sub(pattern, new_section, content, flags=re.DOTALL)
    else:
        # Insert after Context Snapshot section (or at end if not found)
        if "## Progress" in content:
            # Insert before Progress section
            return content.replace("## Progress", f"{new_section}\n\n## Progress")
        else:
            # Append to end
            return content.rstrip() + "\n\n" + new_section + "\n"


def get_company_phase() -> dict:
    """Get current company phase from STATE.md.

    Returns dict with phase info or error.
    """
    content = read_state_file()
    if not content:
        return {"error": "STATE.md not found", "phase": None}

    phase_info = parse_company_phase_section(content)
    if not phase_info["phase"]:
        return {"error": "No company phase set", "phase": None}

    return {
        "phase": phase_info["phase"],
        "since": phase_info["since"],
        "metrics_snapshot": phase_info["metrics_snapshot"],
        "last_assessment": phase_info["last_assessment"],
        "transition_pending": phase_info["transition_pending"],
    }


def set_company_phase(
    phase: str,
    metrics: dict | None = None,
    transition_to: str | None = None,
) -> dict:
    """Set company phase in STATE.md.

    Args:
        phase: The phase to set (startup, growth, scale, mature, decline_pivot)
        metrics: Optional metrics snapshot dict (employees, velocity, blocked_ratio)
        transition_to: Optional pending transition phase

    Returns dict with success status.
    """
    if phase not in VALID_PHASES:
        return {
            "error": f"Invalid phase '{phase}'. Must be one of: {', '.join(VALID_PHASES)}",
            "success": False,
        }

    if transition_to and transition_to not in VALID_PHASES:
        return {
            "error": f"Invalid transition phase '{transition_to}'. Must be one of: {', '.join(VALID_PHASES)}",
            "success": False,
        }

    content = read_state_file()
    now = get_iso_timestamp()

    # Check if we're updating an existing phase
    existing = parse_company_phase_section(content)

    # If phase is same as current, just update assessment time
    if existing["phase"] == phase:
        since = existing["since"] or now
    else:
        since = now

    # Format metrics snapshot
    metrics_str = None
    if metrics:
        parts = []
        if "employees" in metrics:
            parts.append(f"employees: {metrics['employees']}")
        if "velocity" in metrics:
            parts.append(f"velocity: {metrics['velocity']}")
        if "blocked_ratio" in metrics:
            parts.append(f"blocked_ratio: {metrics['blocked_ratio']}")
        metrics_str = "{" + ", ".join(parts) + "}"
    elif existing["metrics_snapshot"]:
        metrics_str = existing["metrics_snapshot"]

    new_section = format_company_phase_section(
        phase=phase,
        since=since,
        metrics_snapshot=metrics_str,
        last_assessment=now,
        transition_pending=transition_to,
    )

    new_content = update_company_phase_section(content, new_section)
    write_state_file(new_content)

    return {
        "success": True,
        "phase": phase,
        "since": since,
        "last_assessment": now,
        "transition_pending": transition_to,
    }


def confirm_transition() -> dict:
    """Confirm a pending phase transition.

    Returns dict with success status and new phase.
    """
    content = read_state_file()
    if not content:
        return {"error": "STATE.md not found", "success": False}

    existing = parse_company_phase_section(content)
    if not existing["transition_pending"]:
        return {"error": "No pending transition to confirm", "success": False}

    new_phase = existing["transition_pending"]
    now = get_iso_timestamp()

    # Preserve metrics snapshot from previous phase for audit trail
    new_section = format_company_phase_section(
        phase=new_phase,
        since=now,
        metrics_snapshot=existing["metrics_snapshot"],
        last_assessment=now,
        transition_pending=None,  # Clear the pending transition
    )

    new_content = update_company_phase_section(content, new_section)
    write_state_file(new_content)

    return {
        "success": True,
        "previous_phase": existing["phase"],
        "new_phase": new_phase,
        "transitioned_at": now,
    }


def get_git_info() -> dict:
    """Get current git state."""
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


def extract_company_phase_section(content: str) -> str | None:
    """Extract the Company Phase section from STATE.md content.

    Returns the full section string or None if not found.
    """
    match = re.search(r"(## Company Phase\n.*?)(?=\n## |\Z)", content, re.DOTALL)
    if match:
        return match.group(1).rstrip()
    return None


def update_state(
    phase: str,
    task_id: str = "",
    task_name: str = "",
    status: str = "in-progress",
    next_task: str = "",
):
    """Update .planning/STATE.md with current progress."""
    state_path = os.path.join(os.getcwd(), ".planning", "STATE.md")

    git = get_git_info()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Read existing state to preserve progress table and company phase
    existing = ""
    if os.path.exists(state_path):
        with open(state_path) as f:
            existing = f.read()

    # Extract Company Phase section to preserve it
    company_phase_section = extract_company_phase_section(existing)

    # Extract and update progress section if it exists
    # For now, write the session section
    session_block = f"""# Session State

<!-- Auto-updated by state_tracker.py -->

## Last Session
- **Date:** {now}
- **Phase:** {phase}
- **Last Completed Task:** {task_id} {task_name}
- **Next Task:** {next_task}
- **Branch:** {git["branch"]}
- **Commit:** {git["commit"]}

## Context Snapshot
<!-- Key context the next session needs to know -->
Status: {status}
"""

    # Insert Company Phase section if it exists (before Progress)
    if company_phase_section:
        session_block += "\n" + company_phase_section + "\n"

    # Preserve everything after "## Progress" if it exists
    if "## Progress" in existing:
        progress_onwards = existing[existing.index("## Progress") :]
        session_block += "\n" + progress_onwards
    else:
        session_block += """
## Progress
| Phase | Tasks Total | Tasks Done | Status |
|-------|------------|------------|--------|

## Recent Changes
| File | Change | Task ID |
|------|--------|---------|
"""

    with open(state_path, "w") as f:
        f.write(session_block)


def print_usage():
    """Print usage information."""
    print("""Usage: state_tracker.py <command> [args...]

Session State Commands (original):
  <phase> [task_id] [task_name] [status] [next_task]
      Update session state in STATE.md

Company Phase Commands (v1.6):
  get-company-phase
      Returns current company phase from STATE.md

  set-company-phase <phase> [--metrics <json>] [--transition <phase>]
      Sets company phase with timestamp
      Valid phases: startup, growth, scale, mature, decline_pivot
      --metrics: JSON object with employees, velocity, blocked_ratio
      --transition: Set a pending transition to another phase

  confirm-transition
      Confirms a pending phase transition
""")


def parse_metrics_arg(args: list[str]) -> dict | None:
    """Parse --metrics argument from args list."""
    for i, arg in enumerate(args):
        if arg == "--metrics" and i + 1 < len(args):
            try:
                return json.loads(args[i + 1])
            except json.JSONDecodeError:
                return None
    return None


def parse_transition_arg(args: list[str]) -> str | None:
    """Parse --transition argument from args list."""
    for i, arg in enumerate(args):
        if arg == "--transition" and i + 1 < len(args):
            return args[i + 1]
    return None


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print_usage()
        sys.exit(1)

    command = args[0]

    # Company phase commands (v1.6)
    if command == "get-company-phase":
        result = get_company_phase()
        print(json.dumps(result))
        sys.exit(0 if result.get("phase") else 1)

    elif command == "set-company-phase":
        if len(args) < 2:
            print(json.dumps({"error": "Phase argument required", "success": False}))
            sys.exit(1)
        phase = args[1]
        metrics = parse_metrics_arg(args)
        transition = parse_transition_arg(args)
        result = set_company_phase(phase, metrics=metrics, transition_to=transition)
        print(json.dumps(result))
        sys.exit(0 if result.get("success") else 1)

    elif command == "confirm-transition":
        result = confirm_transition()
        print(json.dumps(result))
        sys.exit(0 if result.get("success") else 1)

    # Original session state command (backward compatible)
    else:
        # Treat first arg as phase for session state update
        phase = args[0]
        task_id = args[1] if len(args) > 1 else ""
        task_name = args[2] if len(args) > 2 else ""
        status = args[3] if len(args) > 3 else "in-progress"
        next_task = args[4] if len(args) > 4 else ""

        update_state(phase, task_id, task_name, status, next_task)
        print(json.dumps({"updated": True, "phase": phase}))
