#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Batch Hire Utility — Hire multiple employees in a single operation.

Used by /company-bootstrap to create initial organizational structure
efficiently without calling /company-hire multiple times.

Features:
- Atomic org.json updates (all or nothing)
- Pre-validation of roles against org structure
- Parallel employee file creation
- Rollback on failure

Usage:
    from batch_hire import batch_hire, validate_roles

    roles = [
        {"name": "Developer", "department": "engineering", "skills": ["python"]},
        {"name": "Designer", "department": "design", "skills": ["ux"]},
    ]

    result = batch_hire(roles, company_dir=Path(".company"))
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import company_resolver for multi-project support
try:
    from company_resolver import (
        get_company_dir,
        get_current_project,
        get_project_id,
        is_multi_project_mode,
    )
except ImportError:

    def get_company_dir(start_path=None):
        return Path(start_path or os.getcwd()) / ".company"

    def get_current_project():
        return None

    def get_project_id(project_path=None):
        path = Path(project_path or os.getcwd())
        return path.name.lower()

    def is_multi_project_mode(start_path=None):
        return False


# Constants
ORG_FILE = "org.json"
EMPLOYEES_DIR = "employees"


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


def _generate_employee_id(name: str, existing_ids: set[str]) -> str:
    """Generate unique employee ID from name."""
    # Convert name to lowercase, hyphenated format
    base_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not base_id:
        base_id = "employee"

    # Ensure unique
    candidate = base_id
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base_id}-{suffix}"
        suffix += 1

    return candidate


def validate_roles(roles: list[dict], org: dict) -> list[str]:
    """
    Validate roles against org structure.

    Args:
        roles: List of role dicts to validate
        org: Organization structure from org.json

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    # Get existing departments
    departments = {}
    for dept in org.get("departments", []):
        dept_id = dept.get("id")
        if dept_id:
            departments[dept_id] = {
                "teams": [t.get("id") for t in dept.get("teams", [])],
            }

    # Get existing employee IDs
    existing_ids = set()
    employees = org.get("employees", org.get("agents", []))
    for emp in employees:
        if emp.get("id"):
            existing_ids.add(emp["id"])

    for i, role in enumerate(roles):
        prefix = f"Role {i + 1}"

        # Check required fields
        if not role.get("name"):
            errors.append(f"{prefix}: Missing 'name' field")
            continue

        if not role.get("department"):
            errors.append(f"{prefix} ({role['name']}): Missing 'department' field")
            continue

        # Check department exists
        dept = role["department"]
        if dept not in departments:
            errors.append(
                f"{prefix} ({role['name']}): Department '{dept}' not found. "
                f"Available: {', '.join(departments.keys())}"
            )
            continue

        # Check team exists (if specified)
        team = role.get("team")
        if team and team not in departments[dept]["teams"]:
            errors.append(
                f"{prefix} ({role['name']}): Team '{team}' not found in {dept}. "
                f"Available: {', '.join(departments[dept]['teams'])}"
            )

        # Check skills is a list
        skills = role.get("skills", [])
        if not isinstance(skills, list):
            errors.append(f"{prefix} ({role['name']}): 'skills' must be a list")

    return errors


def generate_employee_from_role(
    role: dict,
    employee_id: str,
    project_id: str | None = None,
) -> dict:
    """
    Generate full employee entry from role specification.

    Args:
        role: Role dict with name, department, team, skills, type
        employee_id: Unique employee ID
        project_id: Current project ID for assignment

    Returns:
        Full employee dict for org.json
    """
    now = _now_iso()

    return {
        "id": employee_id,
        "name": role["name"],
        "type": role.get("type", "persistent"),
        "department": role["department"],
        "team": role.get("team"),
        "status": "available",
        "capabilities": role.get("skills", []),
        "memoryPath": f".company/employees/{employee_id}/memory.md",
        "projectAssignments": [project_id] if project_id else [],
        "currentProject": project_id,
        "hireDate": now,
        "autoCreated": False,  # These are deliberately hired, not auto-created
        "lastActive": now,
        "activationCount": 0,
    }


def generate_memory_content(employee: dict) -> str:
    """Generate initial memory file content for employee."""
    now = _now_iso()
    skills = ", ".join(employee.get("capabilities", []))

    return f"""# {employee["name"]} Memory

## Context

**Created:** {now}
**Type:** {employee.get("type", "persistent")}
**Department:** {employee.get("department", "engineering")}
**Team:** {employee.get("team") or "unassigned"}
**Skills:** {skills}

## Preferences

<!-- Preferences will be captured as the employee works -->

## Recent Interactions

<!-- Recent interaction history will be recorded here -->

## Assignment History

<!-- Project assignments will be tracked here -->
"""


def batch_hire(
    roles: list[dict],
    company_dir: Path | str | None = None,
    skip_validation: bool = False,
) -> dict[str, Any]:
    """
    Hire multiple employees in a single atomic operation.

    Args:
        roles: List of role dicts, each with:
            - name: Display name (required)
            - department: Department ID (required)
            - team: Team ID (optional)
            - skills: List of skill strings (optional)
            - type: "persistent" or "consultant" (default: "persistent")
        company_dir: Company directory path (default: auto-detect)
        skip_validation: Skip role validation (for testing)

    Returns:
        Dict with:
            - success: bool
            - hired: list of hired employee IDs
            - errors: list of error messages
            - org_updated: bool
    """
    if company_dir is None:
        company_dir = get_company_dir()
    else:
        company_dir = Path(company_dir)

    org_path = company_dir / ORG_FILE

    # Load org.json
    org = _load_json(org_path)
    if org is None:
        return {
            "success": False,
            "hired": [],
            "errors": ["org.json not found or invalid"],
            "org_updated": False,
        }
    # Normalize any pre-existing bare-string employees to dict records before we
    # extend + persist, so this hire path can never re-persist them (ProjectK
    # root-cause fix; new hires below are already constructed as dict records).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    org = cr.normalize_org_employees(org, org_path.parent)

    # Validate roles
    if not skip_validation:
        validation_errors = validate_roles(roles, org)
        if validation_errors:
            return {
                "success": False,
                "hired": [],
                "errors": validation_errors,
                "org_updated": False,
            }

    # Get existing employee IDs
    employees = org.get("employees", org.get("agents", []))
    existing_ids = {emp.get("id") for emp in employees if emp.get("id")}

    # Get project context
    project_context = get_current_project()
    project_id = project_context["project_id"] if project_context else get_project_id()

    # Generate employees
    hired = []
    created_files: list[Path] = []
    new_employees = []

    try:
        for role in roles:
            # Generate unique ID
            employee_id = _generate_employee_id(role["name"], existing_ids)
            existing_ids.add(employee_id)

            # Generate employee entry
            employee = generate_employee_from_role(role, employee_id, project_id)
            new_employees.append(employee)

            # Create employee directory and memory file
            emp_dir = company_dir / EMPLOYEES_DIR / employee_id
            memory_path = emp_dir / "memory.md"

            memory_content = generate_memory_content(employee)
            if not _write_file(memory_path, memory_content):
                raise OSError(f"Failed to write memory file: {memory_path}")

            created_files.append(memory_path)
            hired.append(
                {
                    "id": employee_id,
                    "name": role["name"],
                    "department": role["department"],
                    "team": role.get("team"),
                    "memory_path": str(memory_path),
                }
            )

        # Update org.json atomically
        employees.extend(new_employees)
        org["employees"] = employees
        # Also update agents for backward compatibility
        org["agents"] = employees

        if not _save_json(org_path, org):
            raise OSError("Failed to save org.json")

        return {
            "success": True,
            "hired": hired,
            "errors": [],
            "org_updated": True,
            "total_hired": len(hired),
        }

    except Exception as e:
        # Rollback: delete created files
        for file_path in created_files:
            try:
                file_path.unlink()
                # Try to remove empty directory
                if file_path.parent.exists() and not any(file_path.parent.iterdir()):
                    file_path.parent.rmdir()
            except OSError:
                pass

        return {
            "success": False,
            "hired": [],
            "errors": [str(e)],
            "org_updated": False,
        }


def print_help():
    """Print usage help."""
    print("""
Batch Hire Utility — Create multiple employees at once

Commands:
    hire [json-file]     Hire employees from JSON file
    validate [json-file] Validate roles without hiring
    help                 Show this help

JSON format:
[
  {
    "name": "Developer Name",
    "department": "engineering",
    "team": "core",
    "skills": ["python", "api"],
    "type": "persistent"
  }
]

Examples:
    python batch_hire.py hire roles.json
    python batch_hire.py validate roles.json

    # Or pipe JSON directly:
    echo '[{"name": "Dev", "department": "engineering"}]' | python batch_hire.py hire -
""")


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command == "help":
        print_help()
        sys.exit(0)

    if command in ("hire", "validate"):
        # Get JSON input
        if len(sys.argv) < 3:
            print("Error: JSON file or '-' for stdin required")
            sys.exit(1)

        json_source = sys.argv[2]

        if json_source == "-":
            roles_json = sys.stdin.read()
        else:
            try:
                with open(json_source, encoding="utf-8") as f:
                    roles_json = f.read()
            except OSError as e:
                print(f"Error reading file: {e}")
                sys.exit(1)

        try:
            roles = json.loads(roles_json)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            sys.exit(1)

        if not isinstance(roles, list):
            print("Error: JSON must be a list of roles")
            sys.exit(1)

        if command == "validate":
            company_dir = get_company_dir()
            org = _load_json(company_dir / ORG_FILE)
            if org is None:
                print(json.dumps({"valid": False, "errors": ["org.json not found"]}))
                sys.exit(1)

            errors = validate_roles(roles, org)
            print(
                json.dumps(
                    {
                        "valid": len(errors) == 0,
                        "errors": errors,
                        "role_count": len(roles),
                    },
                    indent=2,
                )
            )

        else:  # hire
            result = batch_hire(roles)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result["success"] else 1)

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
