#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Gatesmith Workflow Bridge — connects company work items to core Gatesmith commands.

Maps company work types to Gatesmith commands:
- implementation -> /build
- planning -> /plan
- review -> /review
- testing -> /verify
- documentation -> /docs

Passes context from work items (file paths, requirements, acceptance criteria)
to the appropriate Gatesmith command and captures results for the progress tracker.

Usage:
    # Invoke Gatesmith command for a work item
    python forge_bridge.py invoke --task-id TASK-123

    # Invoke with explicit work type override
    python forge_bridge.py invoke --task-id TASK-123 --type implementation

    # Get mapping info for a work item
    python forge_bridge.py map --task-id TASK-123

    # List all available mappings
    python forge_bridge.py mappings

    # Show help
    python forge_bridge.py help
"""

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
from enum import Enum
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
from company_resolver import (
    get_company_dir as resolver_get_company_dir,
)
from company_resolver import (
    get_current_project,
    get_project_id,
)


class InvocationMode(Enum):
    """Execution mode for Gatesmith commands."""

    PREVIEW = "preview"  # Dry-run, no changes
    EXECUTE = "execute"  # Full execution
    VALIDATE = "validate"  # Check preconditions only


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

QUEUE_FILE = "state/work_queue.json"
PLANNING_DIR = ".planning"

# Work type to Gatesmith command mapping
WORK_TYPE_MAPPINGS = {
    "implementation": {
        "command": "/build",
        "description": "Execute implementation tasks with wave-based execution",
        "keywords": [
            "implement",
            "build",
            "develop",
            "create",
            "code",
            "write",
            "backend",
            "frontend",
            "api",
            "feature",
            "component",
        ],
    },
    "planning": {
        "command": "/plan",
        "description": "Create structured plans with planner->checker loop",
        "keywords": [
            "plan",
            "design",
            "architect",
            "structure",
            "spec",
            "requirements",
            "breakdown",
            "decompose",
        ],
    },
    "review": {
        "command": "/review",
        "description": "Code review with reviewer agent",
        "keywords": [
            "review",
            "check",
            "validate",
            "inspect",
            "audit",
            "quality",
            "feedback",
            "approve",
        ],
    },
    "testing": {
        "command": "/verify",
        "description": "Verify all planned work is complete and tests pass",
        "keywords": [
            "test",
            "verify",
            "validate",
            "qa",
            "quality",
            "coverage",
            "integration",
            "unit test",
            "e2e",
        ],
    },
    "documentation": {
        "command": "/docs",
        "description": "Generate documentation from source code",
        "keywords": [
            "document",
            "docs",
            "readme",
            "api docs",
            "jsdoc",
            "docstring",
            "comment",
            "explain",
            "guide",
        ],
    },
}


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class ForgeInvocation:
    """Represents a Gatesmith command invocation."""

    task_id: str
    work_type: str
    forge_command: str
    context: dict = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    success: bool = False
    result: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class InvocationResult:
    """Result of a Gatesmith command invocation."""

    success: bool
    task_id: str
    work_type: str
    forge_command: str
    message: str
    duration_seconds: float = 0.0
    output: str = ""
    error: str = ""
    progress_updated: bool = False


@dataclass
class ExecutionResult:
    """Result from the execution layer (separate from InvocationResult for workflow)."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: str
    mode: InvocationMode
    artifacts: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "command": self.command,
            "mode": self.mode.value,
            "artifacts": self.artifacts,
            "success": self.success,
        }


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """
    Get the company directory path using the resolver.

    In multi-project mode, returns the .company directory at the company root.
    In legacy mode, returns .company in the current directory.
    """
    return resolver_get_company_dir(Path(os.getcwd()))


def get_queue_path() -> Path:
    """Get work_queue.json path."""
    return get_company_dir() / QUEUE_FILE


def get_planning_dir() -> Path:
    """
    Get the .planning directory path.

    In multi-project mode, each project has its own .planning directory.
    This returns the local project's planning directory.
    """
    return Path(os.getcwd()) / PLANNING_DIR


def get_project_context() -> dict:
    """
    Get context about the current project for Gatesmith invocations.

    Returns information needed to provide project context to Gatesmith commands,
    including whether we're in multi-project mode and relevant paths.

    Returns:
        Dict with project context information:
        - multi_project: bool indicating if in multi-project mode
        - project_id: unique identifier for this project (if multi-project)
        - project_path: path to the current project
        - company_root: path to company root (if multi-project)
        - company_dir: path to .company directory
        - planning_dir: path to .planning directory
    """
    cwd = Path(os.getcwd())
    project_info = get_current_project()

    if project_info:
        # Multi-project mode
        return {
            "multi_project": True,
            "project_id": project_info["project_id"],
            "project_path": str(project_info["project_path"]),
            "company_root": str(project_info["company_root"]),
            "company_dir": str(project_info["company_dir"]),
            "planning_dir": str(cwd / PLANNING_DIR),
            "company_config": project_info.get("company_config", {}),
        }
    else:
        # Legacy single-project mode
        return {
            "multi_project": False,
            "project_id": get_project_id(cwd),
            "project_path": str(cwd),
            "company_root": None,
            "company_dir": str(get_company_dir()),
            "planning_dir": str(cwd / PLANNING_DIR),
            "company_config": {},
        }


def load_queue() -> dict:
    """Load work queue from file."""
    queue_path = get_queue_path()

    if not queue_path.exists():
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "metadata": {},
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


def find_task(task_id: str) -> tuple[dict | None, str | None]:
    """
    Find a task by ID in the work queue.

    Returns:
        Tuple of (task dict, status string) or (None, None) if not found
    """
    queue = load_queue()

    for status in ["pending", "in_progress", "blocked", "completed"]:
        for task in queue.get(status, []):
            if task.get("task_id") == task_id:
                return task, status

    return None, None


def detect_work_type(task: dict) -> str:
    """
    Detect work type from task metadata.

    Checks in order:
    1. Explicit work_type field
    2. Required capabilities
    3. Title/description keyword matching
    4. Default to "implementation"
    """
    # 1. Explicit work_type
    if task.get("work_type"):
        work_type = task["work_type"].lower()
        if work_type in WORK_TYPE_MAPPINGS:
            return work_type

    # 2. Check capabilities
    capabilities = task.get("required_capabilities", [])
    caps_lower = [c.lower() for c in capabilities]

    if any("test" in c for c in caps_lower):
        return "testing"
    if any("review" in c for c in caps_lower):
        return "review"
    if any("doc" in c for c in caps_lower):
        return "documentation"
    if any("plan" in c or "architect" in c for c in caps_lower):
        return "planning"

    # 3. Keyword matching from title and description
    text = f"{task.get('title', '')} {task.get('description', '')}".lower()

    best_match = None
    best_score = 0

    for work_type, config in WORK_TYPE_MAPPINGS.items():
        score = sum(1 for kw in config["keywords"] if kw in text)
        if score > best_score:
            best_score = score
            best_match = work_type

    if best_match and best_score > 0:
        return best_match

    # 4. Default
    return "implementation"


def build_context(task: dict) -> dict:
    """
    Build context dictionary from work item for Gatesmith command.

    Extracts:
    - File paths mentioned in task
    - Requirements from acceptance criteria
    - Dependencies
    - Estimated complexity
    - Project context (multi-project support)
    """
    # Get project context for multi-project support
    project_ctx = get_project_context()

    context = {
        "task_id": task.get("task_id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "department": task.get("department", ""),
        "priority": task.get("priority", 3),
        "complexity": task.get("estimated_complexity", "standard"),
        # Project context for Gatesmith
        "project": project_ctx,
    }

    # Extract file paths from description
    file_paths = []
    description = task.get("description", "")
    # Look for common file path patterns
    import re

    path_patterns = [
        r"`([^`]+\.[a-z]+)`",  # backtick-wrapped paths
        r"(\S+\.[a-z]{2,4})\b",  # file.ext patterns
        r"(/[^\s]+)",  # absolute paths
        r"(\./[^\s]+)",  # relative paths
    ]
    for pattern in path_patterns:
        matches = re.findall(pattern, description)
        file_paths.extend(matches)

    if file_paths:
        # Deduplicate and filter valid-looking paths
        context["file_paths"] = list(
            set(p for p in file_paths if "." in p and not p.startswith("http"))
        )

    # Requirements from acceptance criteria
    acceptance_criteria = task.get("acceptance_criteria", [])
    if acceptance_criteria:
        context["requirements"] = acceptance_criteria

    # Dependencies
    dependencies = task.get("dependencies", [])
    if dependencies:
        context["dependencies"] = dependencies

    # Progress notes for context
    progress_notes = task.get("progress_notes", [])
    if progress_notes:
        context["previous_progress"] = [
            note.get("content", "")
            for note in progress_notes[-3:]  # Last 3 notes
        ]

    return context


def update_progress_tracker(
    task_id: str,
    status: str,
    notes: str,
) -> bool:
    """
    Update the progress tracker with invocation results.

    Returns True if update was successful.
    """
    tracker_script = get_company_dir() / "progress_tracker.py"

    # Fall back to hooks directory if not in company dir
    if not tracker_script.exists():
        tracker_script = (
            Path(os.getcwd()) / ".claude" / "hooks" / "company" / "progress_tracker.py"
        )

    if not tracker_script.exists():
        return False

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(tracker_script),
                "update",
                "--task-id",
                task_id,
                "--status",
                status,
                "--notes",
                notes,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.getcwd(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def format_forge_prompt(work_type: str, context: dict) -> str:
    """
    Format a prompt for the Gatesmith command based on work type and context.

    Includes project context for multi-project mode.
    """
    _ = WORK_TYPE_MAPPINGS.get(
        work_type, WORK_TYPE_MAPPINGS["implementation"]
    )  # Reserved

    lines = [
        f"Task: {context.get('title', 'Untitled')}",
        f"ID: {context.get('task_id', 'unknown')}",
    ]

    # Add project context if in multi-project mode
    project = context.get("project", {})
    if project.get("multi_project"):
        lines.append(f"Project: {project.get('project_id', 'unknown')}")
        lines.append(f"Project Path: {project.get('project_path', '')}")
        if project.get("company_root"):
            lines.append(f"Company Root: {project.get('company_root')}")

    if context.get("description"):
        lines.append(f"Description: {context['description']}")

    if context.get("file_paths"):
        lines.append(f"Files: {', '.join(context['file_paths'])}")

    if context.get("requirements"):
        lines.append("Requirements:")
        for req in context["requirements"]:
            lines.append(f"  - {req}")

    if context.get("complexity"):
        lines.append(f"Complexity: {context['complexity']}")

    if context.get("previous_progress"):
        lines.append("Previous progress:")
        for note in context["previous_progress"]:
            lines.append(f"  - {note}")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Core Functions
# -----------------------------------------------------------------------------


def invoke_forge(
    task_id: str,
    work_type_override: str | None = None,
    dry_run: bool = False,
) -> InvocationResult:
    """
    Invoke the appropriate Gatesmith command for a work item.

    Args:
        task_id: The task ID to process
        work_type_override: Optional work type to use instead of auto-detection
        dry_run: If True, show what would be done without executing

    Returns:
        InvocationResult with success status and details
    """
    start_time = datetime.now(timezone.utc)

    # Find the task
    task, current_status = find_task(task_id)
    if not task:
        return InvocationResult(
            success=False,
            task_id=task_id,
            work_type="unknown",
            forge_command="",
            message=f"Task {task_id} not found in work queue",
            error="task_not_found",
        )

    # Determine work type
    work_type = work_type_override or detect_work_type(task)

    if work_type not in WORK_TYPE_MAPPINGS:
        return InvocationResult(
            success=False,
            task_id=task_id,
            work_type=work_type,
            forge_command="",
            message=f"Unknown work type: {work_type}",
            error="invalid_work_type",
        )

    mapping = WORK_TYPE_MAPPINGS[work_type]
    forge_command = mapping["command"]

    # Build context
    context = build_context(task)
    prompt = format_forge_prompt(work_type, context)

    if dry_run:
        return InvocationResult(
            success=True,
            task_id=task_id,
            work_type=work_type,
            forge_command=forge_command,
            message="Dry run - no command executed",
            output=f"Would invoke: {forge_command}\n\nWith context:\n{prompt}",
        )

    # Update progress tracker - mark as in progress
    update_progress_tracker(
        task_id=task_id,
        status="in_progress",
        notes=f"Starting Gatesmith {forge_command} command",
    )

    # Execute the Gatesmith command
    # Note: In a real implementation, this would integrate with Claude Code's
    # command system. For now, we output the invocation details for the
    # orchestrating agent to execute.
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    # Update progress tracker with result
    progress_updated = update_progress_tracker(
        task_id=task_id,
        status="in_progress",
        notes=f"Gatesmith {forge_command} command prepared for execution",
    )

    return InvocationResult(
        success=True,
        task_id=task_id,
        work_type=work_type,
        forge_command=forge_command,
        message=f"Prepared Gatesmith command {forge_command} for task {task_id}",
        duration_seconds=duration,
        output=json.dumps(
            {
                "command": forge_command,
                "context": context,
                "prompt": prompt,
            },
            indent=2,
        ),
        progress_updated=progress_updated,
    )


def get_mapping(task_id: str) -> dict:
    """
    Get the Gatesmith command mapping for a work item without executing.

    Args:
        task_id: The task ID to analyze

    Returns:
        Dict with mapping information including project context
    """
    task, current_status = find_task(task_id)
    if not task:
        return {
            "success": False,
            "error": f"Task {task_id} not found",
        }

    work_type = detect_work_type(task)
    mapping = WORK_TYPE_MAPPINGS.get(work_type, WORK_TYPE_MAPPINGS["implementation"])
    project_ctx = get_project_context()

    return {
        "success": True,
        "task_id": task_id,
        "current_status": current_status,
        "detected_work_type": work_type,
        "forge_command": mapping["command"],
        "command_description": mapping["description"],
        "context": build_context(task),
        "project": project_ctx,
    }


def list_mappings() -> dict:
    """
    List all available work type to Gatesmith command mappings.

    Returns:
        Dict with all mappings and their descriptions
    """
    mappings = []
    for work_type, config in WORK_TYPE_MAPPINGS.items():
        mappings.append(
            {
                "work_type": work_type,
                "forge_command": config["command"],
                "description": config["description"],
                "keywords": config["keywords"],
            }
        )

    return {
        "success": True,
        "mappings": mappings,
        "count": len(mappings),
    }


def complete_forge_invocation(
    task_id: str,
    success: bool,
    output: str = "",
    error: str = "",
) -> dict:
    """
    Mark a Gatesmith invocation as complete and update progress tracker.

    Called after the Gatesmith command has been executed externally.

    Args:
        task_id: The task ID that was processed
        success: Whether the Gatesmith command succeeded
        output: Command output/results
        error: Error message if failed

    Returns:
        Dict with completion status
    """
    task, current_status = find_task(task_id)
    if not task:
        return {
            "success": False,
            "error": f"Task {task_id} not found",
        }

    now = datetime.now(timezone.utc).isoformat()

    if success:
        notes = f"Gatesmith command completed successfully at {now}"
        if output:
            notes += f"\nOutput: {output[:500]}"  # Truncate long output
        new_status = "completed" if "complete" in output.lower() else "in_progress"
    else:
        notes = f"Gatesmith command failed at {now}: {error}"
        new_status = "in_progress"  # Keep in progress for retry

    progress_updated = update_progress_tracker(
        task_id=task_id,
        status=new_status,
        notes=notes,
    )

    return {
        "success": True,
        "task_id": task_id,
        "forge_success": success,
        "new_status": new_status,
        "progress_updated": progress_updated,
        "completed_at": now,
    }


def execute_task(
    task_id: str,
    task_data: dict,
    mode: InvocationMode = InvocationMode.PREVIEW,
) -> ExecutionResult:
    """
    Execute a task using the specified invocation mode.

    Args:
        task_id: The task ID to execute
        task_data: Task data dictionary
        mode: Execution mode (PREVIEW, VALIDATE, EXECUTE)

    Returns:
        ExecutionResult with exit code, output, and timing
    """
    start_time = datetime.now(timezone.utc)

    # PREVIEW mode: Return immediately with mock success result
    if mode == InvocationMode.PREVIEW:
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        return ExecutionResult(
            exit_code=0,
            stdout=f"Preview mode: Task {task_id} would be executed",
            stderr="",
            duration_seconds=duration,
            command=f"execute_task({task_id})",
            mode=mode,
            artifacts={
                "task_id": task_id,
                "preview": True,
                "task_title": task_data.get("title", ""),
            },
        )

    # VALIDATE mode: Check task exists in work queue
    if mode == InvocationMode.VALIDATE:
        task, status = find_task(task_id)
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()

        if task is None:
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Validation failed: Task {task_id} not found in work queue",
                duration_seconds=duration,
                command=f"execute_task({task_id})",
                mode=mode,
                artifacts={
                    "task_id": task_id,
                    "validated": False,
                    "reason": "task_not_found",
                },
            )

        return ExecutionResult(
            exit_code=0,
            stdout=f"Validation passed: Task {task_id} exists with status '{status}'",
            stderr="",
            duration_seconds=duration,
            command=f"execute_task({task_id})",
            mode=mode,
            artifacts={
                "task_id": task_id,
                "validated": True,
                "status": status,
                "task_title": task.get("title", ""),
            },
        )

    # EXECUTE mode: Log warning and return placeholder
    if mode == InvocationMode.EXECUTE:
        import logging

        logging.warning(f"Full execution not yet implemented for task {task_id}")
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        return ExecutionResult(
            exit_code=0,
            stdout=f"Execute mode: Full execution not yet implemented for task {task_id}",
            stderr="",
            duration_seconds=duration,
            command=f"execute_task({task_id})",
            mode=mode,
            artifacts={
                "task_id": task_id,
                "executed": False,
                "reason": "not_implemented",
                "task_title": task_data.get("title", ""),
            },
        )

    # Fallback for unknown modes (should not happen with enum)
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()
    return ExecutionResult(
        exit_code=1,
        stdout="",
        stderr=f"Unknown mode: {mode}",
        duration_seconds=duration,
        command=f"execute_task({task_id})",
        mode=mode,
        artifacts={"task_id": task_id, "error": "unknown_mode"},
    )


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Gatesmith Workflow Bridge - Connect company work items to Gatesmith commands

Commands:
    invoke      Invoke Gatesmith command for a work item
    map         Get mapping info for a work item
    mappings    List all available mappings
    complete    Mark a Gatesmith invocation as complete

Invoke options:
    --task-id ID        Task ID (required)
    --type TYPE         Work type override (optional)
    --dry-run           Show what would be done without executing

Map options:
    --task-id ID        Task ID (required)

Complete options:
    --task-id ID        Task ID (required)
    --success           Mark as successful (default)
    --failure           Mark as failed
    --output TEXT       Command output
    --error TEXT        Error message (for failures)

Work Types and Gatesmith Commands:
    implementation  -> /build   (wave-based execution)
    planning        -> /plan    (planner->checker loop)
    review          -> /review  (code review)
    testing         -> /verify  (verify completion)
    documentation   -> /docs    (generate docs)

Examples:
    # Invoke Gatesmith for a task
    python forge_bridge.py invoke --task-id TASK-123

    # Override work type
    python forge_bridge.py invoke --task-id TASK-123 --type review

    # Dry run to see what would happen
    python forge_bridge.py invoke --task-id TASK-123 --dry-run

    # Get mapping info
    python forge_bridge.py map --task-id TASK-123

    # Mark invocation complete
    python forge_bridge.py complete --task-id TASK-123 --success --output "All tests pass"

Output: JSON with invocation details and results.
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
        if command == "invoke":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = invoke_forge(
                task_id=args["task_id"],
                work_type_override=args.get("type"),
                dry_run=args.get("dry_run", False),
            )
            print(json.dumps(asdict(result), indent=2))

        elif command == "map":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = get_mapping(args["task_id"])
            print(json.dumps(result, indent=2))

        elif command == "mappings":
            result = list_mappings()
            print(json.dumps(result, indent=2))

        elif command == "complete":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            success = not args.get("failure", False)
            result = complete_forge_invocation(
                task_id=args["task_id"],
                success=success,
                output=args.get("output", ""),
                error=args.get("error", ""),
            )
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
