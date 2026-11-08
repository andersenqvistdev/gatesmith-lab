#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Employee Pool Manager — cross-project employee sharing via org.json.

This module wraps org.json for employee pool management. It does NOT create
separate state — all reads/writes go through org.json.

Functions:
    get_available_employees(project_id, capabilities) -> list
        Query employees with project access and optional capability filter.

    assign_employee_to_project(employee_id, project_id) -> dict
        Update org.json employees[].projectAssignments array.

    get_employee_project_load() -> dict
        Return employee_id -> count of assigned projects from org.json.

    suggest_employee_for_task(task) -> dict
        Match task required_capabilities to employee capabilities.

    balance_employee_load() -> list[dict]
        Identify overloaded/underutilized employees and suggest rebalancing.

Usage:
    # Get available employees for a project
    python employee_pool.py available --project-id proj-123 --capabilities "python,testing"

    # Assign an employee to a project
    python employee_pool.py assign --employee-id dev-001 --project-id proj-123

    # Get employee load across projects
    python employee_pool.py load

    # Suggest employee for a task
    python employee_pool.py suggest --capabilities "python,testing" --complexity standard

    # Get load balancing suggestions
    python employee_pool.py balance
"""

import fcntl
import json
import sys
import time
from pathlib import Path
from typing import Any

# Lazy imports to handle both package and direct execution
company_resolver = None

# Configuration
ORG_FILE = "org.json"
FORGE_CONFIG_FILE = "gatesmith.json"
LOCK_FILE = "org.lock"
LOCK_TIMEOUT = 10


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        # Direct script execution - import from same directory
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


def get_company_dir() -> Path:
    """Get the company directory path using company_resolver."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_claude_dir() -> Path:
    """Get the .claude directory path."""
    _ensure_imports()
    company_root = company_resolver.find_company_root()
    if company_root:
        return company_root / ".claude"
    return Path.cwd() / ".claude"


def get_org_path() -> Path:
    """Get the org.json file path."""
    return get_company_dir() / ORG_FILE


def get_forge_config_path() -> Path:
    """Get the gatesmith.json file path."""
    return get_claude_dir() / FORGE_CONFIG_FILE


def get_lock_path() -> Path:
    """Get the lock file path."""
    return get_company_dir() / LOCK_FILE


class OrgLock:
    """
    Context manager for file-based org.json locking.

    Uses fcntl.flock for atomic file locking on Unix systems.
    Provides safe concurrent access to org.json.
    """

    def __init__(self, lock_path: Path, timeout: int = LOCK_TIMEOUT):
        self.lock_path = lock_path
        self.timeout = timeout
        self.lock_file = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.lock_path, "w")

        start_time = time.time()
        while True:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() - start_time > self.timeout:
                    raise TimeoutError(
                        f"Could not acquire org lock within {self.timeout}s"
                    )
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
        return False


def load_org() -> dict:
    """Load org.json from file."""
    org_path = get_org_path()

    if not org_path.exists():
        return {"employees": [], "projects": []}

    try:
        with open(org_path, "r", encoding="utf-8") as f:
            org = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"employees": [], "projects": []}
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, org_path.parent)


def save_org(org: dict):
    """Save org.json to file.

    Safety: Refuses to save if it would wipe existing employees.
    """
    import os
    import tempfile

    org_path = get_org_path()
    org_path.parent.mkdir(parents=True, exist_ok=True)

    # Safety check: Don't wipe employees if file already has them
    if org_path.exists():
        try:
            with open(org_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            # Block saves that would wipe existing employees
            if len(existing_employees) > 0 and len(new_employees) == 0:
                import sys

                print(
                    f"[SAFETY] Blocked save_org: Would wipe {len(existing_employees)} employees. "
                    "This is likely a bug in the calling code.",
                    file=sys.stderr,
                )
                return  # Refuse to save
        except (json.JSONDecodeError, OSError):
            # If we can't read existing file and trying to save empty employees, block
            if len(org.get("employees", [])) == 0:
                import sys

                print(
                    "[SAFETY] Blocked save_org: Cannot read existing file and new data has no employees. "
                    "This could cause data loss.",
                    file=sys.stderr,
                )
                return  # Refuse to save

    # Atomic write: write to temp file, then os.replace (prevents truncation race)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix="org_", dir=str(org_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, str(org_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_forge_config() -> dict:
    """Load gatesmith.json for employee sharing settings."""
    config_path = get_forge_config_path()

    if not config_path.exists():
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_max_projects_per_employee() -> int:
    """Get maxProjectsPerEmployee from gatesmith.json."""
    config = load_forge_config()
    cross_project = config.get("crossProject", {})
    employee_sharing = cross_project.get("employeeSharing", {})
    return employee_sharing.get("maxProjectsPerEmployee", 3)


def get_employee_sharing_enabled() -> bool:
    """Check if employee sharing is enabled in gatesmith.json."""
    config = load_forge_config()
    cross_project = config.get("crossProject", {})
    employee_sharing = cross_project.get("employeeSharing", {})
    return employee_sharing.get("enabled", True)


def get_prefer_project_specialists() -> bool:
    """Check if project specialists should be preferred."""
    config = load_forge_config()
    cross_project = config.get("crossProject", {})
    employee_sharing = cross_project.get("employeeSharing", {})
    return employee_sharing.get("preferProjectSpecialists", True)


def capabilities_match(required: list[str], available: list[str]) -> bool:
    """Check if all required capabilities are available."""
    if not required:
        return True
    return all(cap in available for cap in required)


def get_available_employees(
    project_id: str | None = None,
    capabilities: list[str] | None = None,
) -> list[dict]:
    """
    Query org.json for employees available to work on a project.

    An employee is available for a project if:
    1. Their projectAssignments is empty (can work on all projects), OR
    2. The project_id is in their projectAssignments, OR
    3. They have capacity to take on a new project (below maxProjectsPerEmployee)

    Args:
        project_id: Optional project ID to filter by access
        capabilities: Optional list of required capabilities to filter by

    Returns:
        List of employee dicts with additional 'availability_score' field
    """
    org = load_org()
    employees = org.get("employees", [])
    max_projects = get_max_projects_per_employee()

    available = []

    for emp in employees:
        # Skip offline employees
        if emp.get("status") == "offline":
            continue

        emp_capabilities = emp.get("capabilities", [])
        emp_assignments = emp.get("projectAssignments", [])

        # Check capability match if filter provided
        if capabilities and not capabilities_match(capabilities, emp_capabilities):
            continue

        # Determine project access
        has_access = False
        is_specialist = False

        if not emp_assignments:
            # No specific assignments = available for all projects
            has_access = True
        elif project_id and project_id in emp_assignments:
            # Explicitly assigned to this project
            has_access = True
            is_specialist = True
        elif project_id is None:
            # No project filter, any employee matches
            has_access = True

        # Also consider employees who can take on a new project
        current_load = len(emp_assignments)
        has_capacity = current_load < max_projects

        if has_access or (has_capacity and project_id):
            # Calculate availability score (higher = better)
            # Factors: specialist status, current load, status
            score = 0.0

            # Specialists get highest priority
            if is_specialist:
                score += 100.0

            # Lower load = higher score
            score += (max_projects - current_load) * 10.0

            # Available status gets bonus
            if emp.get("status") == "available":
                score += 20.0

            available.append(
                {
                    **emp,
                    "availability_score": score,
                    "is_specialist": is_specialist,
                    "current_project_count": current_load,
                    "has_capacity": has_capacity,
                }
            )

    # Sort by availability score descending
    available.sort(key=lambda x: x.get("availability_score", 0), reverse=True)

    return available


def assign_employee_to_project(employee_id: str, project_id: str) -> dict:
    """
    Assign an employee to a project by updating org.json.

    Updates the employee's projectAssignments array. Validates:
    - Employee exists
    - Employee is not already at maxProjectsPerEmployee limit
    - Employee is not already assigned to this project

    Args:
        employee_id: The employee ID to assign
        project_id: The project ID to assign to

    Returns:
        Dict with success status and details
    """
    if not get_employee_sharing_enabled():
        return {
            "success": False,
            "reason": "disabled",
            "message": "Employee sharing is disabled in gatesmith.json",
        }

    max_projects = get_max_projects_per_employee()

    with OrgLock(get_lock_path()):
        org = load_org()
        employees = org.get("employees", [])

        # Find the employee
        emp_index = None
        for i, emp in enumerate(employees):
            if emp.get("id") == employee_id:
                emp_index = i
                break

        if emp_index is None:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Employee {employee_id} not found",
            }

        emp = employees[emp_index]
        assignments = emp.get("projectAssignments", [])

        # Check if already assigned
        if project_id in assignments:
            return {
                "success": False,
                "reason": "already_assigned",
                "message": f"Employee {employee_id} is already assigned to {project_id}",
            }

        # Check capacity
        if len(assignments) >= max_projects:
            return {
                "success": False,
                "reason": "max_projects",
                "message": (
                    f"Employee {employee_id} is at max capacity "
                    f"({len(assignments)}/{max_projects} projects)"
                ),
                "current_assignments": assignments,
                "max_projects": max_projects,
            }

        # Add assignment
        if "projectAssignments" not in emp:
            emp["projectAssignments"] = []
        emp["projectAssignments"].append(project_id)

        save_org(org)

    return {
        "success": True,
        "employee_id": employee_id,
        "project_id": project_id,
        "message": f"Assigned {employee_id} to {project_id}",
        "current_assignments": emp["projectAssignments"],
        "remaining_capacity": max_projects - len(emp["projectAssignments"]),
    }


def unassign_employee_from_project(employee_id: str, project_id: str) -> dict:
    """
    Remove an employee from a project assignment.

    Args:
        employee_id: The employee ID to unassign
        project_id: The project ID to unassign from

    Returns:
        Dict with success status and details
    """
    with OrgLock(get_lock_path()):
        org = load_org()
        employees = org.get("employees", [])

        # Find the employee
        emp_index = None
        for i, emp in enumerate(employees):
            if emp.get("id") == employee_id:
                emp_index = i
                break

        if emp_index is None:
            return {
                "success": False,
                "reason": "not_found",
                "message": f"Employee {employee_id} not found",
            }

        emp = employees[emp_index]
        assignments = emp.get("projectAssignments", [])

        # Check if assigned
        if project_id not in assignments:
            return {
                "success": False,
                "reason": "not_assigned",
                "message": f"Employee {employee_id} is not assigned to {project_id}",
            }

        # Remove assignment
        emp["projectAssignments"] = [p for p in assignments if p != project_id]

        # Clear currentProject if it was this project
        if emp.get("currentProject") == project_id:
            emp["currentProject"] = None

        save_org(org)

    return {
        "success": True,
        "employee_id": employee_id,
        "project_id": project_id,
        "message": f"Unassigned {employee_id} from {project_id}",
        "current_assignments": emp["projectAssignments"],
    }


def get_employee_project_load() -> dict:
    """
    Get the number of projects each employee is assigned to.

    Returns:
        Dict with:
        - employee_load: Dict of employee_id -> project count
        - total_employees: Total employee count
        - average_load: Average projects per employee
        - max_projects: Config max projects per employee
    """
    org = load_org()
    employees = org.get("employees", [])
    max_projects = get_max_projects_per_employee()

    employee_load = {}
    total_load = 0

    for emp in employees:
        emp_id = emp.get("id", "")
        assignments = emp.get("projectAssignments", [])
        load = len(assignments)
        employee_load[emp_id] = {
            "project_count": load,
            "projects": assignments,
            "status": emp.get("status", "available"),
            "at_capacity": load >= max_projects,
            "remaining_capacity": max(0, max_projects - load),
        }
        total_load += load

    total_employees = len(employees)
    average_load = total_load / total_employees if total_employees > 0 else 0.0

    return {
        "success": True,
        "employee_load": employee_load,
        "total_employees": total_employees,
        "total_assignments": total_load,
        "average_load": round(average_load, 2),
        "max_projects_per_employee": max_projects,
    }


def suggest_employee_for_task(
    required_capabilities: list[str] | None = None,
    complexity: str = "standard",
    project_id: str | None = None,
    prefer_specialists: bool | None = None,
) -> dict:
    """
    Suggest the best employee for a task based on capabilities and workload.

    Ranking factors:
    1. Capability match (required)
    2. Project specialist status (bonus)
    3. Current workload (prefer less loaded)
    4. Employee status (prefer available)

    Args:
        required_capabilities: List of required capabilities for the task
        complexity: Task complexity (trivial, standard, complex, epic)
        project_id: Optional project ID for specialist matching
        prefer_specialists: Whether to prefer project specialists (default from config)

    Returns:
        Dict with ranked suggestions and scores
    """
    if prefer_specialists is None:
        prefer_specialists = get_prefer_project_specialists()

    # Get available employees filtered by capabilities
    available = get_available_employees(
        project_id=project_id,
        capabilities=required_capabilities,
    )

    if not available:
        return {
            "success": False,
            "reason": "no_match",
            "message": "No employees match the required capabilities",
            "required_capabilities": required_capabilities,
        }

    # Score and rank candidates
    suggestions = []
    max_projects = get_max_projects_per_employee()

    for emp in available:
        score = 0.0
        reasons = []

        emp_capabilities = emp.get("capabilities", [])
        emp_assignments = emp.get("projectAssignments", [])
        current_load = len(emp_assignments)

        # Base score from capability match quality
        if required_capabilities:
            # Bonus for having MORE capabilities than required (versatility)
            matching = sum(
                1 for cap in required_capabilities if cap in emp_capabilities
            )
            match_ratio = (
                matching / len(required_capabilities) if required_capabilities else 1.0
            )
            score += match_ratio * 50
            reasons.append(f"capability_match={match_ratio:.0%}")

            # Small bonus for extra relevant capabilities
            extra_caps = len(emp_capabilities) - len(required_capabilities)
            if extra_caps > 0:
                score += min(extra_caps * 2, 10)
                reasons.append(f"extra_capabilities={extra_caps}")

        # Specialist bonus
        if prefer_specialists and emp.get("is_specialist"):
            score += 30
            reasons.append("project_specialist")

        # Workload factor (lower load = higher score)
        load_factor = 1.0 - (current_load / max_projects) if max_projects > 0 else 1.0
        score += load_factor * 20
        reasons.append(f"workload={current_load}/{max_projects}")

        # Status bonus
        if emp.get("status") == "available":
            score += 15
            reasons.append("status=available")
        elif emp.get("status") == "busy":
            score -= 5
            reasons.append("status=busy")
        elif emp.get("status") == "blocked":
            score -= 10
            reasons.append("status=blocked")

        # Complexity consideration
        # For complex/epic tasks, prefer employees with more capabilities
        if complexity in ("complex", "epic"):
            cap_count = len(emp_capabilities)
            score += min(cap_count * 2, 15)
            reasons.append(f"complexity_fit={complexity}")

        suggestions.append(
            {
                "employee_id": emp.get("id"),
                "name": emp.get("name"),
                "score": round(score, 1),
                "reasons": reasons,
                "capabilities": emp_capabilities,
                "current_project_count": current_load,
                "status": emp.get("status"),
            }
        )

    # Sort by score descending
    suggestions.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {
        "success": True,
        "suggestions": suggestions,
        "top_suggestion": suggestions[0] if suggestions else None,
        "total_candidates": len(suggestions),
        "required_capabilities": required_capabilities,
        "complexity": complexity,
        "project_id": project_id,
    }


def balance_employee_load() -> list[dict]:
    """
    Identify overloaded and underutilized employees and suggest rebalancing.

    Returns a list of rebalancing suggestions. Does NOT auto-execute changes.

    Criteria:
    - Overloaded: Above maxProjectsPerEmployee
    - Underutilized: Below average load
    - Ideal: At or near average load

    Returns:
        List of suggestion dicts with:
        - action: "transfer" | "unassign"
        - employee_id: Employee to adjust
        - project_id: Project involved
        - reason: Why this suggestion
        - priority: Suggestion priority (1=highest)
    """
    load_info = get_employee_project_load()
    employee_load = load_info.get("employee_load", {})
    average_load = load_info.get("average_load", 0.0)
    max_projects = load_info.get("max_projects_per_employee", 3)

    suggestions = []

    # Identify overloaded and underutilized employees
    overloaded = []
    underutilized = []

    for emp_id, info in employee_load.items():
        project_count = info.get("project_count", 0)
        status = info.get("status", "available")

        # Skip offline employees
        if status == "offline":
            continue

        if project_count > max_projects:
            overloaded.append(
                {
                    "employee_id": emp_id,
                    "project_count": project_count,
                    "projects": info.get("projects", []),
                    "excess": project_count - max_projects,
                }
            )
        elif project_count < average_load - 0.5 and status == "available":
            underutilized.append(
                {
                    "employee_id": emp_id,
                    "project_count": project_count,
                    "projects": info.get("projects", []),
                    "capacity": max_projects - project_count,
                }
            )

    # Generate suggestions for overloaded employees
    priority = 1
    for over in overloaded:
        emp_id = over["employee_id"]
        projects = over["projects"]
        excess = over["excess"]

        # Suggest unassigning from projects with least recent activity
        # (We don't have activity data here, so suggest removing from end of list)
        for i in range(excess):
            if i < len(projects):
                project_to_remove = projects[-(i + 1)]

                # Check if any underutilized employee can take this project
                transfer_target = None
                for under in underutilized:
                    if under["capacity"] > 0:
                        transfer_target = under["employee_id"]
                        under["capacity"] -= 1
                        break

                if transfer_target:
                    suggestions.append(
                        {
                            "action": "transfer",
                            "from_employee_id": emp_id,
                            "to_employee_id": transfer_target,
                            "project_id": project_to_remove,
                            "reason": (
                                f"{emp_id} is overloaded ({over['project_count']} projects), "
                                f"transfer to {transfer_target} who has capacity"
                            ),
                            "priority": priority,
                        }
                    )
                else:
                    suggestions.append(
                        {
                            "action": "unassign",
                            "employee_id": emp_id,
                            "project_id": project_to_remove,
                            "reason": (
                                f"{emp_id} is overloaded ({over['project_count']} projects), "
                                f"no available transfer target"
                            ),
                            "priority": priority,
                        }
                    )
                priority += 1

    return {
        "success": True,
        "suggestions": suggestions,
        "summary": {
            "overloaded_count": len(overloaded),
            "underutilized_count": len(underutilized),
            "suggestion_count": len(suggestions),
            "average_load": average_load,
            "max_projects_per_employee": max_projects,
        },
        "overloaded_employees": [o["employee_id"] for o in overloaded],
        "underutilized_employees": [u["employee_id"] for u in underutilized],
    }


def print_help():
    """Print usage help."""
    help_text = """
Employee Pool Manager

Commands:
    available   Get available employees for a project
    assign      Assign an employee to a project
    unassign    Remove an employee from a project
    load        Get employee project load
    suggest     Suggest employee for a task
    balance     Get load balancing suggestions

Available options:
    --project-id ID       Project ID to filter by or assign to
    --capabilities LIST   Comma-separated capability filter
    --employee-id ID      Employee ID for assignment

Assign options:
    --employee-id ID      Employee ID (required)
    --project-id ID       Project ID (required)

Suggest options:
    --capabilities LIST   Comma-separated required capabilities
    --complexity STR      trivial|standard|complex|epic (default=standard)
    --project-id ID       Optional project ID for specialist matching
    --no-prefer-specialists  Disable specialist preference

Examples:
    # Get available employees for a project with Python skills
    python employee_pool.py available --project-id proj-123 --capabilities "python,testing"

    # Assign an employee to a project
    python employee_pool.py assign --employee-id dev-001 --project-id proj-123

    # Get employee load across projects
    python employee_pool.py load

    # Suggest employee for a complex task requiring specific skills
    python employee_pool.py suggest --capabilities "python,testing" --complexity complex

    # Get load balancing suggestions
    python employee_pool.py balance
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
        if command == "available":
            capabilities = None
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            result = get_available_employees(
                project_id=args.get("project_id"),
                capabilities=capabilities,
            )
            print(json.dumps(result, indent=2, default=str))

        elif command == "assign":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "project_id" not in args:
                print("Error: --project-id required")
                sys.exit(1)

            result = assign_employee_to_project(
                employee_id=args["employee_id"],
                project_id=args["project_id"],
            )
            print(json.dumps(result, indent=2))

        elif command == "unassign":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)
            if "project_id" not in args:
                print("Error: --project-id required")
                sys.exit(1)

            result = unassign_employee_from_project(
                employee_id=args["employee_id"],
                project_id=args["project_id"],
            )
            print(json.dumps(result, indent=2))

        elif command == "load":
            result = get_employee_project_load()
            print(json.dumps(result, indent=2))

        elif command == "suggest":
            capabilities = None
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]

            prefer_specialists = args.get("no_prefer_specialists", False) is not True

            result = suggest_employee_for_task(
                required_capabilities=capabilities,
                complexity=args.get("complexity", "standard"),
                project_id=args.get("project_id"),
                prefer_specialists=prefer_specialists,
            )
            print(json.dumps(result, indent=2))

        elif command == "balance":
            result = balance_employee_load()
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except TimeoutError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
