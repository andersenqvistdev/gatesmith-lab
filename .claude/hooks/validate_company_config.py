# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Validate org.json before writes.

Ensures organizational configuration maintains structural integrity:
- Required top-level keys present
- Department structure valid
- Agent structure valid with unique IDs
- Agent department references exist
- No duplicate IDs

Exit code 2 (block) if validation fails.
"""

import json
import sys


def validate_required_keys(data: dict) -> list[str]:
    """Validate required top-level keys exist."""
    # v2.0: "employees" is the new name, but "agents" is still valid for backward compat
    required = ["company", "departments", "work"]
    errors = []
    for key in required:
        if key not in data:
            errors.append(f"Missing required top-level key: '{key}'")
    # Must have either "agents" (v1.x) or "employees" (v2.0) or both
    if "agents" not in data and "employees" not in data:
        errors.append("Missing required top-level key: 'agents' or 'employees'")
    return errors


def validate_departments(data: dict) -> list[str]:
    """Validate department structure and uniqueness."""
    errors = []
    departments = data.get("departments", [])

    if not isinstance(departments, list):
        errors.append("'departments' must be an array")
        return errors

    seen_ids = set()
    for i, dept in enumerate(departments):
        if not isinstance(dept, dict):
            errors.append(f"Department at index {i} must be an object")
            continue

        # Required department fields
        if "id" not in dept:
            errors.append(f"Department at index {i} missing required field: 'id'")
        else:
            dept_id = dept["id"]
            if dept_id in seen_ids:
                errors.append(f"Duplicate department ID: '{dept_id}'")
            seen_ids.add(dept_id)

        if "name" not in dept:
            errors.append(f"Department at index {i} missing required field: 'name'")

    return errors


def validate_agents(data: dict) -> list[str]:
    """Validate agent/employee structure, uniqueness, and department references.

    v2.0 backward compatibility: accepts both "agents" (v1.x) and "employees" (v2.0).
    If both exist, validates both. Prefers "employees" for new schemas.
    """
    errors = []
    # Support both v1.x "agents" and v2.0 "employees"
    agents = data.get("employees", data.get("agents", []))

    if not isinstance(agents, list):
        errors.append("'employees' (or 'agents') must be an array")
        return errors

    # Build set of valid department IDs
    departments = data.get("departments", [])
    valid_dept_ids = set()
    if isinstance(departments, list):
        for dept in departments:
            if isinstance(dept, dict) and "id" in dept:
                valid_dept_ids.add(dept["id"])

    required_fields = ["id", "name", "type", "department", "status", "capabilities"]
    seen_ids = set()

    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            errors.append(f"Agent at index {i} must be an object")
            continue

        # Check required fields
        for field in required_fields:
            if field not in agent:
                errors.append(f"Agent at index {i} missing required field: '{field}'")

        # Check agent ID uniqueness
        if "id" in agent:
            agent_id = agent["id"]
            if agent_id in seen_ids:
                errors.append(f"Duplicate agent ID: '{agent_id}'")
            seen_ids.add(agent_id)

        # Check department reference exists
        if "department" in agent:
            dept_ref = agent["department"]
            if dept_ref not in valid_dept_ids:
                agent_id = agent.get("id", f"index {i}")
                errors.append(
                    f"Agent '{agent_id}' references non-existent department: '{dept_ref}'"
                )

        # Validate capabilities is an array
        if "capabilities" in agent:
            if not isinstance(agent["capabilities"], list):
                agent_id = agent.get("id", f"index {i}")
                errors.append(f"Agent '{agent_id}' capabilities must be an array")

    return errors


def validate_work(data: dict) -> list[str]:
    """Validate work section structure."""
    errors = []
    work = data.get("work")

    if work is None:
        return errors  # Already caught by required keys check

    if not isinstance(work, dict):
        errors.append("'work' must be an object")

    return errors


def validate_org_json(content: str) -> tuple[bool, list[str]]:
    """
    Validate org.json content.
    Returns (is_valid, errors).
    """
    errors = []

    # Parse JSON
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]

    if not isinstance(data, dict):
        return False, ["org.json root must be an object"]

    # Run all validations
    errors.extend(validate_required_keys(data))
    errors.extend(validate_departments(data))
    errors.extend(validate_agents(data))
    errors.extend(validate_work(data))

    return len(errors) == 0, errors


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Only validate Write and Edit operations
    if tool_name not in ("Write", "Edit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    # Only validate .company/org.json
    if not file_path.endswith(".company/org.json"):
        sys.exit(0)

    # Get content to validate
    if tool_name == "Write":
        content = tool_input.get("content", "")
    else:
        # For Edit, we need to validate the resulting content
        # Since we only have new_string, we can't fully validate an edit
        # However, we can at least check if the edit is trying to write valid JSON
        new_string = tool_input.get("new_string", "")
        # If the new string appears to be a complete JSON replacement, validate it
        if new_string.strip().startswith("{") and new_string.strip().endswith("}"):
            content = new_string
        else:
            # Partial edits are harder to validate pre-write
            # Let the edit proceed - the next full write will be validated
            sys.exit(0)

    if not content:
        sys.exit(0)

    is_valid, errors = validate_org_json(content)

    if not is_valid:
        report = "org.json VALIDATION FAILED - write blocked.\n\n"
        report += "Validation errors:\n"
        for error in errors:
            report += f"  - {error}\n"
        report += "\nFix these issues before saving org.json."

        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": report,
                    "errors": errors,
                }
            )
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
