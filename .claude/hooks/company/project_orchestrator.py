#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Project Orchestrator — multi-project creation, routing, and discovery.

Provides core functionality for multi-project orchestration:

1. Project creation functions:
   - create_project(name, path, template, tech_stack) -> dict
   - scaffold_project_structure(project_path, template) -> dict
   - register_project_with_company(project_path) -> dict

2. Cross-project routing functions:
   - route_task_to_project(task_id, target_project_id, reason) -> dict
   - get_routing_suggestions(task) -> list[dict] with project matches
   - is_cross_project_task(task) -> bool
   - validate_cross_project_access(employee_id, target_project_id) -> bool

3. Project discovery:
   - get_project_capabilities(project_id) -> dict (tech stack, domains)
   - find_projects_by_capability(capability) -> list[str]
   - get_project_health_summary(project_id) -> dict

Security: All routing validates employee has target project assignment.

Usage:
    # Create a new project
    python project_orchestrator.py create --name "my-project" --path "./projects/my-project"

    # Route a task to another project
    python project_orchestrator.py route --task-id TASK-123 --target-project other-project

    # Get routing suggestions for a task
    python project_orchestrator.py suggest --task-id TASK-123

    # Find projects by capability
    python project_orchestrator.py find-by-cap --capability python
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
company_resolver = None
work_allocator = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver, work_allocator
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import work_allocator as wa

        company_resolver = cr
        work_allocator = wa
    except ImportError:
        # Direct script execution - import from same directory
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        company_resolver = cr
        work_allocator = wa


# Constants
ORG_FILE = "org.json"
PROJECTS_DIR = "projects"
TEMPLATES_DIR = ".company/templates/projects"
FORGE_COMPANY_ROOT_MARKER = ".forge-company-root"
CLAUDE_DIR = ".claude"
PLANNING_DIR = ".planning"


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict | None:
    """Load JSON file safely."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: dict) -> bool:
    """Save JSON file safely."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def _write_file(path: Path, content: str) -> bool:
    """Write file content safely."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except OSError:
        return False


def _get_company_dir() -> Path:
    """Get the company directory path using company_resolver."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def _get_company_root() -> Path | None:
    """Get the company root path if in multi-project mode."""
    _ensure_imports()
    return company_resolver.find_company_root()


def _load_org() -> dict | None:
    """Load org.json from company directory."""
    company_dir = _get_company_dir()
    org = _load_json(company_dir / ORG_FILE)
    if org is None:
        return None
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, company_dir)


def _save_org(org: dict) -> bool:
    """Save org.json to company directory.

    Safety: Refuses to save if it would wipe existing employees.
    """
    company_dir = _get_company_dir()
    org_path = company_dir / ORG_FILE

    # Safety check: Don't wipe employees if file already has them
    if org_path.exists():
        existing = _load_json(org_path)
        if existing:
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            if len(existing_employees) > 0 and len(new_employees) == 0:
                import sys

                print(
                    f"[SAFETY] Blocked _save_org: Would wipe {len(existing_employees)} employees.",
                    file=sys.stderr,
                )
                return False

    return _save_json(org_path, org)


def _generate_project_id(name: str) -> str:
    """Generate a valid project ID from name."""
    # Convert to lowercase, replace spaces and special chars with hyphens
    project_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not project_id:
        project_id = "project"
    return project_id


# -----------------------------------------------------------------------------
# Project Creation Functions
# -----------------------------------------------------------------------------


def create_project(
    name: str,
    path: str | Path,
    template: str | None = None,
    tech_stack: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a new project with optional template and tech stack.

    Args:
        name: Display name for the project
        path: Path where the project will be created
        template: Template name to use (e.g., "python-api", "web-app")
        tech_stack: List of technologies (e.g., ["python", "fastapi", "postgres"])

    Returns:
        Dict with:
            - success: bool
            - project_id: Generated project ID
            - project_path: Absolute path to project
            - scaffolded: Whether template was applied
            - registered: Whether registered with company
            - errors: List of error messages
    """
    project_path = Path(path).resolve()
    project_id = _generate_project_id(name)
    errors = []

    # Check if path already exists and has content
    if project_path.exists() and any(project_path.iterdir()):
        return {
            "success": False,
            "project_id": project_id,
            "project_path": str(project_path),
            "scaffolded": False,
            "registered": False,
            "errors": [f"Path already exists and is not empty: {project_path}"],
        }

    # Create project directory
    try:
        project_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {
            "success": False,
            "project_id": project_id,
            "project_path": str(project_path),
            "scaffolded": False,
            "registered": False,
            "errors": [f"Failed to create project directory: {e}"],
        }

    # Scaffold project structure
    scaffold_result = scaffold_project_structure(
        project_path=project_path,
        template=template,
        tech_stack=tech_stack,
        project_name=name,
    )

    if not scaffold_result["success"]:
        errors.extend(scaffold_result.get("errors", []))

    # Register with company
    register_result = register_project_with_company(
        project_path=project_path,
        project_name=name,
        project_id=project_id,
        tech_stack=tech_stack,
    )

    if not register_result["success"]:
        errors.extend(register_result.get("errors", []))

    return {
        "success": len(errors) == 0,
        "project_id": project_id,
        "project_path": str(project_path),
        "scaffolded": scaffold_result["success"],
        "registered": register_result["success"],
        "errors": errors,
        "template_used": template,
        "tech_stack": tech_stack or [],
    }


def scaffold_project_structure(
    project_path: str | Path,
    template: str | None = None,
    tech_stack: list[str] | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    """
    Scaffold project directory structure based on template.

    Creates:
        - .claude/ directory for agent configs
        - .planning/ directory for Gatesmith planning docs
        - Basic CLAUDE.md for project instructions

    Args:
        project_path: Path to the project directory
        template: Template name to use (loads from .company/templates/projects/)
        tech_stack: List of technologies for context
        project_name: Display name for the project

    Returns:
        Dict with:
            - success: bool
            - created_dirs: List of created directories
            - created_files: List of created files
            - template_applied: Template name if applied
            - errors: List of error messages
    """
    project_path = Path(project_path).resolve()
    created_dirs: list[str] = []
    created_files: list[str] = []
    errors: list[str] = []

    # Core directories to create
    core_dirs = [
        project_path / CLAUDE_DIR / "agents",
        project_path / PLANNING_DIR,
    ]

    for dir_path in core_dirs:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(dir_path))
        except OSError as e:
            errors.append(f"Failed to create {dir_path}: {e}")

    # Create basic CLAUDE.md
    claude_md_content = _generate_claude_md(
        project_name=project_name or project_path.name,
        tech_stack=tech_stack,
    )
    claude_md_path = project_path / "CLAUDE.md"
    if _write_file(claude_md_path, claude_md_content):
        created_files.append(str(claude_md_path))
    else:
        errors.append("Failed to create CLAUDE.md")

    # Create basic .planning files
    planning_files = {
        "PROJECT.md": _generate_project_md(
            project_name=project_name or project_path.name,
            tech_stack=tech_stack,
        ),
        "STATE.md": _generate_state_md(),
    }

    for filename, content in planning_files.items():
        file_path = project_path / PLANNING_DIR / filename
        if _write_file(file_path, content):
            created_files.append(str(file_path))
        else:
            errors.append(f"Failed to create {filename}")

    # Apply template if specified
    template_applied = None
    if template:
        template_result = _apply_template(project_path, template)
        if template_result["success"]:
            template_applied = template
            created_dirs.extend(template_result.get("created_dirs", []))
            created_files.extend(template_result.get("created_files", []))
        else:
            errors.extend(template_result.get("errors", []))

    return {
        "success": len(errors) == 0,
        "created_dirs": created_dirs,
        "created_files": created_files,
        "template_applied": template_applied,
        "errors": errors,
    }


def _generate_claude_md(
    project_name: str,
    tech_stack: list[str] | None = None,
) -> str:
    """Generate basic CLAUDE.md content for a project."""
    tech_stack_str = ", ".join(tech_stack) if tech_stack else "Not specified"

    return f"""# {project_name}

## Project Overview

This project was created as part of a multi-project company structure.

## Tech Stack

{tech_stack_str}

## Conventions

- Follow existing code patterns
- Run tests before committing
- Use atomic commits

## Development

See `.planning/` for roadmap and state.

"""


def _generate_project_md(
    project_name: str,
    tech_stack: list[str] | None = None,
) -> str:
    """Generate basic PROJECT.md for .planning/."""
    tech_list = "\n".join(f"- {t}" for t in (tech_stack or [])) or "- Not specified"
    now = _now_iso()

    return f"""# {project_name} — Project Context

## Created

{now}

## Tech Stack

{tech_list}

## Architecture

<!-- Document architecture decisions here -->

## Conventions

<!-- Document coding conventions here -->

"""


def _generate_state_md() -> str:
    """Generate basic STATE.md for .planning/."""
    now = _now_iso()

    return f"""# Session State

## Last Updated

{now}

## Current Phase

Initial Setup

## Active Work

- Project scaffolding

## Blockers

None

"""


def _apply_template(project_path: Path, template: str) -> dict[str, Any]:
    """
    Apply a project template from .company/templates/projects/.

    Args:
        project_path: Path to the project
        template: Template name

    Returns:
        Dict with success status and created files/dirs
    """
    company_root = _get_company_root()
    if not company_root:
        return {
            "success": False,
            "errors": ["Not in multi-project mode, no templates available"],
        }

    template_path = company_root / TEMPLATES_DIR / template
    if not template_path.exists():
        return {
            "success": False,
            "errors": [f"Template '{template}' not found at {template_path}"],
        }

    created_dirs: list[str] = []
    created_files: list[str] = []
    errors: list[str] = []

    # Copy template files
    for item in template_path.rglob("*"):
        relative = item.relative_to(template_path)
        target = project_path / relative

        try:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(target))
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                content = item.read_text(encoding="utf-8")
                target.write_text(content, encoding="utf-8")
                created_files.append(str(target))
        except OSError as e:
            errors.append(f"Failed to copy {item}: {e}")

    return {
        "success": len(errors) == 0,
        "created_dirs": created_dirs,
        "created_files": created_files,
        "errors": errors,
    }


def register_project_with_company(
    project_path: str | Path,
    project_name: str | None = None,
    project_id: str | None = None,
    tech_stack: list[str] | None = None,
) -> dict[str, Any]:
    """
    Register a project with the company in org.json.

    Args:
        project_path: Path to the project directory
        project_name: Display name (defaults to directory name)
        project_id: Unique ID (defaults to generated from name)
        tech_stack: List of technologies

    Returns:
        Dict with:
            - success: bool
            - project_id: Registered project ID
            - errors: List of error messages
    """
    project_path = Path(project_path).resolve()
    project_name = project_name or project_path.name
    project_id = project_id or _generate_project_id(project_name)

    org = _load_org()
    if org is None:
        return {
            "success": False,
            "project_id": project_id,
            "errors": ["org.json not found or invalid"],
        }

    # Check if project already exists
    projects = org.get("projects", [])
    for existing in projects:
        if existing.get("id") == project_id:
            return {
                "success": False,
                "project_id": project_id,
                "errors": [f"Project '{project_id}' already registered"],
            }

    # Add project entry
    now = _now_iso()
    project_entry = {
        "id": project_id,
        "path": str(project_path),
        "name": project_name,
        "description": "",
        "registered": now,
        "lastAccessed": now,
        "techStack": tech_stack or [],
        "status": "active",
    }

    projects.append(project_entry)
    org["projects"] = projects

    # Update mode to multi-project if needed
    if org.get("mode") == "single-project":
        org["mode"] = "multi-project"

    if not _save_org(org):
        return {
            "success": False,
            "project_id": project_id,
            "errors": ["Failed to save org.json"],
        }

    return {
        "success": True,
        "project_id": project_id,
        "project_entry": project_entry,
        "errors": [],
    }


# -----------------------------------------------------------------------------
# Cross-Project Routing Functions
# -----------------------------------------------------------------------------


def validate_cross_project_access(
    employee_id: str,
    target_project_id: str,
) -> bool:
    """
    Validate that an employee has access to a target project.

    Checks org.json projectAssignments for the employee.

    Args:
        employee_id: The employee ID to check
        target_project_id: The project ID to validate access to

    Returns:
        True if employee has access, False otherwise
    """
    org = _load_org()
    if org is None:
        return False

    employees = org.get("employees", org.get("agents", []))
    for emp in employees:
        if emp.get("id") == employee_id:
            assignments = emp.get("projectAssignments", [])
            # Empty assignments means access to all projects (legacy behavior)
            if not assignments:
                return True
            return target_project_id in assignments

    return False


def route_task_to_project(
    task_id: str,
    target_project_id: str,
    reason: str,
    employee_id: str | None = None,
) -> dict[str, Any]:
    """
    Route a task to a different project.

    Security: Validates employee has access to target project before routing.

    Args:
        task_id: The task ID to route
        target_project_id: Target project ID
        reason: Reason for routing
        employee_id: Employee requesting the route (for access validation)

    Returns:
        Dict with:
            - success: bool
            - task_id: The routed task ID
            - from_project: Original project ID
            - to_project: Target project ID
            - reason: Routing reason
            - errors: List of error messages
    """
    _ensure_imports()

    # Validate employee access if employee_id provided
    if employee_id and not validate_cross_project_access(
        employee_id, target_project_id
    ):
        return {
            "success": False,
            "task_id": task_id,
            "from_project": None,
            "to_project": target_project_id,
            "reason": reason,
            "errors": [
                f"Employee '{employee_id}' does not have access to project '{target_project_id}'"
            ],
        }

    # Validate target project exists
    org = _load_org()
    if org is None:
        return {
            "success": False,
            "task_id": task_id,
            "from_project": None,
            "to_project": target_project_id,
            "reason": reason,
            "errors": ["org.json not found"],
        }

    project_ids = {p.get("id") for p in org.get("projects", [])}
    if target_project_id not in project_ids:
        return {
            "success": False,
            "task_id": task_id,
            "from_project": None,
            "to_project": target_project_id,
            "reason": reason,
            "errors": [f"Target project '{target_project_id}' not found"],
        }

    # Get the task from work queue
    task_result = work_allocator.get_task(task_id)
    if not task_result.get("success"):
        return {
            "success": False,
            "task_id": task_id,
            "from_project": None,
            "to_project": target_project_id,
            "reason": reason,
            "errors": [f"Task '{task_id}' not found in work queue"],
        }

    task = task_result.get("task", {})
    from_project = task.get("project_id")

    # Update task with new project_id
    now = _now_iso()
    routing_note = (
        f"Routed from {from_project or 'unassigned'} to {target_project_id}: {reason}"
    )

    update_result = work_allocator.update_task(
        task_id=task_id,
        notes=routing_note,
    )

    if not update_result.get("success"):
        return {
            "success": False,
            "task_id": task_id,
            "from_project": from_project,
            "to_project": target_project_id,
            "reason": reason,
            "errors": [
                f"Failed to update task: {update_result.get('message', 'unknown error')}"
            ],
        }

    # Manually update project_id in the queue (work_allocator.update_task doesn't support this)
    # We need to directly modify the queue
    company_dir = _get_company_dir()
    queue_path = company_dir / "state/work_queue.json"
    queue = _load_json(queue_path)

    if queue:
        for status in ["pending", "in_progress", "blocked", "completed"]:
            for task_entry in queue.get(status, []):
                if task_entry.get("task_id") == task_id:
                    task_entry["project_id"] = target_project_id
                    if "routing_history" not in task_entry:
                        task_entry["routing_history"] = []
                    task_entry["routing_history"].append(
                        {
                            "from_project": from_project,
                            "to_project": target_project_id,
                            "reason": reason,
                            "routed_at": now,
                            "routed_by": employee_id,
                        }
                    )
                    break

        _save_json(queue_path, queue)

    return {
        "success": True,
        "task_id": task_id,
        "from_project": from_project,
        "to_project": target_project_id,
        "reason": reason,
        "routed_at": now,
        "errors": [],
    }


def is_cross_project_task(task: dict) -> bool:
    """
    Determine if a task involves cross-project work.

    Checks:
        - Task description mentions other project names
        - Task has dependencies in other projects
        - Task has cross-project routing history

    Args:
        task: Task dictionary from work queue

    Returns:
        True if task involves cross-project work
    """
    # Check routing history
    if task.get("routing_history"):
        return True

    # Check dependencies - would need to look up each dep's project_id
    # For now, just check if task mentions "cross-project" or similar
    description = (task.get("description") or "").lower()
    title = (task.get("title") or "").lower()
    text = f"{title} {description}"

    cross_project_keywords = [
        "cross-project",
        "cross project",
        "other project",
        "shared",
        "multi-project",
    ]

    for keyword in cross_project_keywords:
        if keyword in text:
            return True

    return False


def get_routing_suggestions(task: dict) -> list[dict[str, Any]]:
    """
    Get suggestions for which project a task might belong to.

    Analyzes task content and matches against project capabilities.

    Args:
        task: Task dictionary from work queue

    Returns:
        List of suggestion dicts with:
            - project_id: Suggested project
            - confidence: 0.0-1.0 confidence score
            - reason: Why this project was suggested
    """
    suggestions = []

    org = _load_org()
    if org is None:
        return suggestions

    projects = org.get("projects", [])
    if not projects:
        return suggestions

    # Extract keywords from task
    title = (task.get("title") or "").lower()
    description = (task.get("description") or "").lower()
    capabilities = [c.lower() for c in task.get("required_capabilities", [])]
    text = f"{title} {description} {' '.join(capabilities)}"

    for project in projects:
        project_id = project.get("id")
        tech_stack = [t.lower() for t in project.get("techStack", [])]
        project_name = (project.get("name") or "").lower()

        # Calculate match score
        score = 0.0
        reasons = []

        # Check tech stack matches
        for tech in tech_stack:
            if tech in text:
                score += 0.3
                reasons.append(f"Matches tech: {tech}")

        # Check capabilities match tech stack
        for cap in capabilities:
            if cap in tech_stack:
                score += 0.2
                reasons.append(f"Capability matches tech: {cap}")

        # Check project name mentioned
        if project_name and project_name in text:
            score += 0.5
            reasons.append(f"Project name mentioned: {project_name}")

        # Cap score at 1.0
        score = min(1.0, score)

        if score > 0.1:
            suggestions.append(
                {
                    "project_id": project_id,
                    "project_name": project.get("name"),
                    "confidence": round(score, 2),
                    "reasons": reasons,
                }
            )

    # Sort by confidence descending
    suggestions.sort(key=lambda x: x["confidence"], reverse=True)

    return suggestions


# -----------------------------------------------------------------------------
# Project Discovery Functions
# -----------------------------------------------------------------------------


def get_project_capabilities(project_id: str) -> dict[str, Any]:
    """
    Get capabilities and tech stack for a project.

    Args:
        project_id: The project ID

    Returns:
        Dict with:
            - success: bool
            - project_id: Project ID
            - tech_stack: List of technologies
            - domains: List of domain areas (inferred)
            - employee_count: Number of assigned employees
            - errors: List of error messages
    """
    org = _load_org()
    if org is None:
        return {
            "success": False,
            "project_id": project_id,
            "tech_stack": [],
            "domains": [],
            "employee_count": 0,
            "errors": ["org.json not found"],
        }

    # Find project
    project = None
    for p in org.get("projects", []):
        if p.get("id") == project_id:
            project = p
            break

    if not project:
        return {
            "success": False,
            "project_id": project_id,
            "tech_stack": [],
            "domains": [],
            "employee_count": 0,
            "errors": [f"Project '{project_id}' not found"],
        }

    tech_stack = project.get("techStack", [])

    # Infer domains from tech stack
    domains = _infer_domains(tech_stack)

    # Count assigned employees
    employees = org.get("employees", org.get("agents", []))
    assigned_count = sum(
        1 for emp in employees if project_id in emp.get("projectAssignments", [])
    )

    return {
        "success": True,
        "project_id": project_id,
        "project_name": project.get("name"),
        "tech_stack": tech_stack,
        "domains": domains,
        "employee_count": assigned_count,
        "status": project.get("status", "active"),
        "errors": [],
    }


def _infer_domains(tech_stack: list[str]) -> list[str]:
    """Infer domain areas from tech stack."""
    domains = set()

    tech_to_domain = {
        # Backend
        "python": "backend",
        "node": "backend",
        "go": "backend",
        "rust": "backend",
        "java": "backend",
        "fastapi": "backend",
        "django": "backend",
        "express": "backend",
        # Frontend
        "react": "frontend",
        "vue": "frontend",
        "angular": "frontend",
        "svelte": "frontend",
        "typescript": "frontend",
        "javascript": "frontend",
        # Data
        "postgres": "data",
        "mysql": "data",
        "mongodb": "data",
        "redis": "data",
        "elasticsearch": "data",
        # Infrastructure
        "docker": "infrastructure",
        "kubernetes": "infrastructure",
        "aws": "infrastructure",
        "gcp": "infrastructure",
        "terraform": "infrastructure",
        # ML/AI
        "pytorch": "ml",
        "tensorflow": "ml",
        "scikit-learn": "ml",
        "pandas": "ml",
        # Mobile
        "swift": "mobile",
        "kotlin": "mobile",
        "flutter": "mobile",
        "react-native": "mobile",
    }

    for tech in tech_stack:
        tech_lower = tech.lower()
        if tech_lower in tech_to_domain:
            domains.add(tech_to_domain[tech_lower])

    return sorted(domains)


def find_projects_by_capability(capability: str) -> list[str]:
    """
    Find projects that have a specific capability in their tech stack.

    Args:
        capability: The capability/technology to search for

    Returns:
        List of project IDs that have the capability
    """
    org = _load_org()
    if org is None:
        return []

    matching = []
    capability_lower = capability.lower()

    for project in org.get("projects", []):
        tech_stack = [t.lower() for t in project.get("techStack", [])]
        if capability_lower in tech_stack:
            matching.append(project.get("id"))

    return matching


def get_project_health_summary(project_id: str) -> dict[str, Any]:
    """
    Get health summary for a project.

    Includes:
        - Task counts by status
        - Active employee count
        - Last activity timestamp

    Args:
        project_id: The project ID

    Returns:
        Dict with health metrics
    """
    _ensure_imports()

    org = _load_org()
    if org is None:
        return {
            "success": False,
            "project_id": project_id,
            "errors": ["org.json not found"],
        }

    # Find project
    project = None
    for p in org.get("projects", []):
        if p.get("id") == project_id:
            project = p
            break

    if not project:
        return {
            "success": False,
            "project_id": project_id,
            "errors": [f"Project '{project_id}' not found"],
        }

    # Get task counts for this project
    task_list = work_allocator.list_tasks(project_id=project_id, all_projects=False)
    counts = task_list.get("counts", {})

    # Count active employees
    employees = org.get("employees", org.get("agents", []))
    active_employees = [
        emp
        for emp in employees
        if project_id in emp.get("projectAssignments", [])
        and emp.get("status") in ("available", "busy")
    ]

    # Calculate health score (simple heuristic)
    pending = counts.get("pending", 0)
    in_progress = counts.get("in_progress", 0)
    blocked = counts.get("blocked", 0)
    completed = counts.get("completed", 0)

    total_tasks = pending + in_progress + blocked + completed
    health_score = 1.0

    if total_tasks > 0:
        # Penalize for blocked tasks
        blocked_ratio = blocked / total_tasks
        health_score -= blocked_ratio * 0.5

        # Penalize if no progress (all pending)
        if completed == 0 and in_progress == 0 and pending > 0:
            health_score -= 0.2

    health_score = max(0.0, min(1.0, health_score))

    return {
        "success": True,
        "project_id": project_id,
        "project_name": project.get("name"),
        "status": project.get("status", "active"),
        "task_counts": counts,
        "active_employees": len(active_employees),
        "employee_ids": [emp.get("id") for emp in active_employees],
        "health_score": round(health_score, 2),
        "last_accessed": project.get("lastAccessed"),
        "errors": [],
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    print("""
Project Orchestrator — Multi-project creation, routing, and discovery

Commands:
    create          Create a new project
    scaffold        Scaffold project structure
    register        Register existing project with company
    route           Route task to different project
    suggest         Get routing suggestions for a task
    validate-access Check employee access to project
    capabilities    Get project capabilities
    find-by-cap     Find projects by capability
    health          Get project health summary

Create options:
    --name NAME         Project name (required)
    --path PATH         Project path (required)
    --template TEMPLATE Template to use
    --tech-stack LIST   Comma-separated tech stack

Route options:
    --task-id ID           Task ID to route (required)
    --target-project ID    Target project ID (required)
    --reason TEXT          Reason for routing
    --employee-id ID       Employee requesting route

Suggest options:
    --task-id ID           Task ID to analyze (required)

Validate-access options:
    --employee-id ID       Employee ID (required)
    --target-project ID    Project ID (required)

Capabilities options:
    --project-id ID        Project ID (required)

Find-by-cap options:
    --capability CAP       Capability to search for (required)

Health options:
    --project-id ID        Project ID (required)

Examples:
    python project_orchestrator.py create --name "API Service" --path ./projects/api --tech-stack python,fastapi

    python project_orchestrator.py route --task-id TASK-123 --target-project api-service --reason "Task requires API work"

    python project_orchestrator.py suggest --task-id TASK-123

    python project_orchestrator.py find-by-cap --capability python

    python project_orchestrator.py health --project-id api-service
""")


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
        if command == "create":
            if "name" not in args or "path" not in args:
                print("Error: --name and --path required")
                sys.exit(1)

            tech_stack = None
            if "tech_stack" in args:
                tech_stack = [t.strip() for t in args["tech_stack"].split(",")]

            result = create_project(
                name=args["name"],
                path=args["path"],
                template=args.get("template"),
                tech_stack=tech_stack,
            )
            print(json.dumps(result, indent=2))

        elif command == "scaffold":
            if "path" not in args:
                print("Error: --path required")
                sys.exit(1)

            tech_stack = None
            if "tech_stack" in args:
                tech_stack = [t.strip() for t in args["tech_stack"].split(",")]

            result = scaffold_project_structure(
                project_path=args["path"],
                template=args.get("template"),
                tech_stack=tech_stack,
                project_name=args.get("name"),
            )
            print(json.dumps(result, indent=2))

        elif command == "register":
            if "path" not in args:
                print("Error: --path required")
                sys.exit(1)

            tech_stack = None
            if "tech_stack" in args:
                tech_stack = [t.strip() for t in args["tech_stack"].split(",")]

            result = register_project_with_company(
                project_path=args["path"],
                project_name=args.get("name"),
                project_id=args.get("project_id"),
                tech_stack=tech_stack,
            )
            print(json.dumps(result, indent=2))

        elif command == "route":
            if "task_id" not in args or "target_project" not in args:
                print("Error: --task-id and --target-project required")
                sys.exit(1)

            result = route_task_to_project(
                task_id=args["task_id"],
                target_project_id=args["target_project"],
                reason=args.get("reason", "Manual routing"),
                employee_id=args.get("employee_id"),
            )
            print(json.dumps(result, indent=2))

        elif command == "suggest":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            _ensure_imports()
            task_result = work_allocator.get_task(args["task_id"])
            if not task_result.get("success"):
                print(json.dumps({"success": False, "error": "Task not found"}))
                sys.exit(1)

            suggestions = get_routing_suggestions(task_result["task"])
            print(
                json.dumps(
                    {
                        "success": True,
                        "task_id": args["task_id"],
                        "suggestions": suggestions,
                    },
                    indent=2,
                )
            )

        elif command == "validate-access":
            if "employee_id" not in args or "target_project" not in args:
                print("Error: --employee-id and --target-project required")
                sys.exit(1)

            has_access = validate_cross_project_access(
                employee_id=args["employee_id"],
                target_project_id=args["target_project"],
            )
            print(
                json.dumps(
                    {
                        "success": True,
                        "employee_id": args["employee_id"],
                        "target_project": args["target_project"],
                        "has_access": has_access,
                    },
                    indent=2,
                )
            )

        elif command == "capabilities":
            if "project_id" not in args:
                print("Error: --project-id required")
                sys.exit(1)

            result = get_project_capabilities(args["project_id"])
            print(json.dumps(result, indent=2))

        elif command == "find-by-cap":
            if "capability" not in args:
                print("Error: --capability required")
                sys.exit(1)

            projects = find_projects_by_capability(args["capability"])
            print(
                json.dumps(
                    {
                        "success": True,
                        "capability": args["capability"],
                        "matching_projects": projects,
                        "count": len(projects),
                    },
                    indent=2,
                )
            )

        elif command == "health":
            if "project_id" not in args:
                print("Error: --project-id required")
                sys.exit(1)

            result = get_project_health_summary(args["project_id"])
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
