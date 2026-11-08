# /// script
# requires-python = ">=3.10"
# ///
"""
SessionStart Hook: Display company organization status if .company/ exists.
Complements session_init.py with company-specific context.
"""

import json
import os
import sys


def load_org_json(company_dir: str) -> dict | None:
    """Load and parse org.json from the company directory."""
    org_path = os.path.join(company_dir, "org.json")
    if not os.path.exists(org_path):
        return None

    try:
        with open(org_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def count_agents(org: dict) -> tuple[int, int]:
    """Count persistent and consultant agents. Returns (persistent, consultant)."""
    agents = org.get("agents", [])
    persistent = sum(1 for a in agents if a.get("type") == "persistent")
    consultant = sum(1 for a in agents if a.get("type") == "consultant")
    return persistent, consultant


def count_departments(org: dict) -> int:
    """Count number of departments."""
    return len(org.get("departments", []))


def count_active_work(org: dict) -> int:
    """Count active work items (not done)."""
    work = org.get("work", {})
    active = work.get("active", [])
    return len([w for w in active if w.get("status") != "done"])


def main():
    company_dir = os.path.join(os.getcwd(), ".company")

    # Silent exit if .company/ doesn't exist
    if not os.path.isdir(company_dir):
        sys.exit(0)

    org = load_org_json(company_dir)
    if org is None:
        sys.exit(0)

    # Extract company info
    company = org.get("company", {})
    company_name = company.get("name", "Unknown Company")

    # Count stats
    dept_count = count_departments(org)
    persistent, consultant = count_agents(org)
    total_agents = persistent + consultant
    active_work = count_active_work(org)

    # Build agent string
    if total_agents > 0:
        agent_str = (
            f"{total_agents} agents ({persistent} persistent, {consultant} consultant)"
        )
    else:
        agent_str = "0 agents"

    # Build work string
    work_str = f"{active_work} active task{'s' if active_work != 1 else ''}"

    # Output summary
    print(f"Company: {company_name} | {dept_count} depts | {agent_str} | {work_str}")

    sys.exit(0)


if __name__ == "__main__":
    main()
