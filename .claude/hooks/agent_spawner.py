# /// script
# requires-python = ">=3.10"
# ///
"""
Smart Agent Spawner — contextual agent creation (from IndyDevDan's philosophy).

Analyzes a task description and determines if existing agents are sufficient
or if a specialist agent should be created. This is the "spawn the right tool"
pattern — don't force a generalist when a specialist would be better.

Usage: echo "task description" | python agent_spawner.py
Returns: JSON with recommended agents and whether new ones should be created.
"""

import json
import os
import sys

# Domain → specialist agent mapping
DOMAIN_SPECIALISTS = {
    "database": {
        "name": "db-migration",
        "trigger_words": [
            "migration",
            "schema",
            "database",
            "sql",
            "orm",
            "model",
            "table",
            "column",
            "index",
            "query optimization",
        ],
        "description": "Database migration and schema management specialist",
    },
    "api-docs": {
        "name": "api-documenter",
        "trigger_words": [
            "api doc",
            "swagger",
            "openapi",
            "endpoint doc",
            "api reference",
        ],
        "description": "API documentation and OpenAPI spec generator",
    },
    "performance": {
        "name": "perf-optimizer",
        "trigger_words": [
            "performance",
            "optimize",
            "slow",
            "latency",
            "profil",
            "benchmark",
            "cache",
            "memory leak",
        ],
        "description": "Performance analysis and optimization specialist",
    },
    "accessibility": {
        "name": "a11y-auditor",
        "trigger_words": ["accessibility", "a11y", "screen reader", "aria", "wcag"],
        "description": "Accessibility audit and remediation specialist",
    },
    "i18n": {
        "name": "i18n-specialist",
        "trigger_words": [
            "internationalization",
            "i18n",
            "localization",
            "l10n",
            "translation",
            "locale",
        ],
        "description": "Internationalization and localization specialist",
    },
    "devops": {
        "name": "devops-engineer",
        "trigger_words": [
            "docker",
            "kubernetes",
            "ci/cd",
            "pipeline",
            "deploy",
            "infrastructure",
            "terraform",
            "ansible",
        ],
        "description": "DevOps and infrastructure configuration specialist",
    },
    "data": {
        "name": "data-engineer",
        "trigger_words": [
            "etl",
            "pipeline",
            "data transform",
            "csv",
            "json transform",
            "data validation",
            "parsing",
        ],
        "description": "Data transformation and pipeline specialist",
    },
    "ui": {
        "name": "ui-specialist",
        "trigger_words": [
            "component",
            "ui",
            "ux",
            "design system",
            "responsive",
            "animation",
            "css",
            "styling",
            "theme",
        ],
        "description": "UI/UX component and design system specialist",
    },
    "company-coordination": {
        "name": "company-coordinator",
        "trigger_words": [
            "organization",
            "company",
            "delegate",
            "coordinate",
            "cross-team",
            "enterprise",
        ],
        "description": "Company-wide coordination and delegation specialist",
    },
    "department": {
        "name": "department-head",
        "trigger_words": [
            "department",
            "team",
            "manage",
            "staff",
            "personnel",
            "division",
        ],
        "description": "Department management and team coordination specialist",
    },
}

# Core agents that already exist
CORE_AGENTS = [
    "architect",
    "implementer",
    "reviewer",
    "tester",
    "security-auditor",
    "meta-agent",
    "plan-checker",
]


def discover_company_agents(base_dir: str = None) -> list[dict]:
    """
    Discover custom company agents in .claude/agents/company/.

    Returns a list of discovered agents with their metadata.
    """
    if base_dir is None:
        base_dir = os.getcwd()

    company_agents_dir = os.path.join(base_dir, ".claude", "agents", "company")
    discovered = []

    if not os.path.isdir(company_agents_dir):
        return discovered

    # Walk through company agents directory recursively
    for root, _dirs, files in os.walk(company_agents_dir):
        for filename in files:
            if filename.endswith(".md"):
                agent_path = os.path.join(root, filename)
                agent_name = filename[:-3]  # Remove .md extension

                # Determine subdomain from relative path
                rel_path = os.path.relpath(root, company_agents_dir)
                subdomain = rel_path if rel_path != "." else "general"

                # Try to extract description from file (first non-empty line after title)
                description = f"Custom company agent: {agent_name}"
                try:
                    with open(agent_path, "r") as f:
                        lines = f.readlines()
                        for line in lines:
                            line = line.strip()
                            # Skip empty lines and title lines
                            if line and not line.startswith("#"):
                                description = line[:100]  # Truncate long descriptions
                                break
                except (IOError, OSError):
                    pass

                discovered.append(
                    {
                        "name": agent_name,
                        "path": agent_path,
                        "subdomain": subdomain,
                        "description": description,
                        "is_company_agent": True,
                    }
                )

    return discovered


def analyze_task(description: str) -> dict:
    """Analyze task and recommend agents."""
    desc_lower = description.lower()

    # Check which existing agents are needed
    needed_core = ["implementer"]  # always needed

    if any(k in desc_lower for k in ["plan", "design", "architect", "structure"]):
        needed_core.append("architect")
    if any(k in desc_lower for k in ["review", "quality", "check"]):
        needed_core.append("reviewer")
    if any(k in desc_lower for k in ["test", "spec", "coverage"]):
        needed_core.append("tester")
    if any(
        k in desc_lower for k in ["security", "auth", "password", "token", "encrypt"]
    ):
        needed_core.append("security-auditor")

    # Always include architect + reviewer for non-trivial work
    if len(desc_lower.split()) > 10:
        if "architect" not in needed_core:
            needed_core.append("architect")
        if "reviewer" not in needed_core:
            needed_core.append("reviewer")

    # Check for specialist needs
    specialists_needed = []
    agents_dir = os.path.join(os.getcwd(), ".claude", "agents")

    for domain, spec in DOMAIN_SPECIALISTS.items():
        if any(word in desc_lower for word in spec["trigger_words"]):
            agent_file = os.path.join(agents_dir, f"{spec['name']}.md")
            specialists_needed.append(
                {
                    "name": spec["name"],
                    "domain": domain,
                    "description": spec["description"],
                    "exists": os.path.exists(agent_file),
                    "create": not os.path.exists(agent_file),
                }
            )

    # Discover custom company agents
    company_agents = discover_company_agents()
    company_agents_matched = []

    # Check if any company agents match the task description
    for agent in company_agents:
        agent_name_words = agent["name"].replace("-", " ").replace("_", " ").split()
        subdomain_words = agent["subdomain"].replace("-", " ").replace("_", " ").split()
        all_keywords = agent_name_words + subdomain_words

        if any(word.lower() in desc_lower for word in all_keywords if len(word) > 2):
            company_agents_matched.append(
                {
                    "name": agent["name"],
                    "path": agent["path"],
                    "subdomain": agent["subdomain"],
                    "description": agent["description"],
                    "exists": True,
                    "create": False,
                    "is_company_agent": True,
                }
            )

    return {
        "core_agents": list(set(needed_core)),
        "specialists": specialists_needed,
        "company_agents": company_agents_matched,
        "all_company_agents": company_agents,
        "create_new": [s for s in specialists_needed if s["create"]],
        "recommendation": (
            "Create specialist agents before starting"
            if any(s["create"] for s in specialists_needed)
            else "All needed agents exist"
        ),
    }


if __name__ == "__main__":
    desc = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read().strip()
    if not desc:
        print("Usage: echo 'task description' | python agent_spawner.py")
        sys.exit(1)

    result = analyze_task(desc)
    print(json.dumps(result, indent=2))
