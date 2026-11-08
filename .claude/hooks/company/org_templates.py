#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Organization Templates — Pre-defined structures for different business domains.

Provides template organizational structures that can be used to bootstrap
companies quickly with appropriate departments, teams, and initial roles.

Templates available:
- saas_platform: SaaS product company (engineering + product + design)
- ecommerce: E-commerce platform (full-stack + marketing)
- mobile_app: Mobile application (mobile engineering + UX)
- content_platform: Content/media platform (content + community)
- api_service: API/Developer tools (backend + docs)
- data_platform: Data/ML platform (data engineering + analytics)
- agency: Agency/consulting (flexible specialist pool)
- minimal: Minimal setup (engineering only)

Usage:
    from org_templates import detect_domain, get_template, list_templates

    # Detect best template from description
    template_name = detect_domain("Building a SaaS analytics dashboard")

    # Get full template
    template = get_template(template_name)

    # List all templates
    templates = list_templates()
"""

import json
import re
import sys
from typing import Any

# Domain keywords for detection
DOMAIN_KEYWORDS = {
    "saas_platform": [
        "saas",
        "subscription",
        "dashboard",
        "analytics",
        "b2b",
        "platform",
        "multi-tenant",
        "billing",
        "recurring",
        "enterprise",
        "crm",
        "erp",
    ],
    "ecommerce": [
        "ecommerce",
        "e-commerce",
        "shop",
        "store",
        "cart",
        "checkout",
        "payment",
        "inventory",
        "product catalog",
        "orders",
        "shipping",
    ],
    "mobile_app": [
        "mobile",
        "ios",
        "android",
        "app",
        "native",
        "react native",
        "flutter",
        "push notification",
        "offline",
        "mobile-first",
    ],
    "content_platform": [
        "content",
        "media",
        "blog",
        "cms",
        "publishing",
        "editorial",
        "articles",
        "video",
        "podcast",
        "streaming",
        "social",
    ],
    "api_service": [
        "api",
        "developer",
        "sdk",
        "integration",
        "webhook",
        "rest",
        "graphql",
        "developer portal",
        "documentation",
        "open api",
        "microservice",
    ],
    "data_platform": [
        "data",
        "ml",
        "machine learning",
        "ai",
        "analytics",
        "pipeline",
        "etl",
        "warehouse",
        "visualization",
        "reporting",
        "prediction",
    ],
    "agency": [
        "agency",
        "consulting",
        "client",
        "project-based",
        "freelance",
        "services",
        "custom development",
        "contract",
    ],
    # WS-068-008: Autonomous operation keywords
    "autonomous": [
        "autonomous",
        "autonomous company",
        "ai company",
        "full autonomy",
        "self-healing",
        "daemon",
        "agent team",
        "automated",
        "hands-off",
        "24/7",
        "background",
        "unattended",
    ],
}

# Organization templates
TEMPLATES: dict[str, dict[str, Any]] = {
    "saas_platform": {
        "name": "SaaS Platform",
        "description": "Subscription-based software platform with focus on product and engineering",
        "departments": ["engineering", "product", "design"],
        "teams": {
            "engineering": ["core", "integrations", "infrastructure"],
            "product": ["product-strategy", "analytics"],
            "design": ["ux", "visual"],
        },
        "core_roles": [
            {
                "name": "Platform Architect",
                "department": "engineering",
                "team": "core",
                "skills": ["architecture", "scalability", "api", "database"],
                "type": "persistent",
            },
            {
                "name": "Full-Stack Developer",
                "department": "engineering",
                "team": "core",
                "skills": ["frontend", "backend", "database", "api"],
                "type": "persistent",
            },
            {
                "name": "Product Manager",
                "department": "product",
                "team": "product-strategy",
                "skills": ["product", "strategy", "roadmap", "user-research"],
                "type": "persistent",
            },
            {
                "name": "UX Designer",
                "department": "design",
                "team": "ux",
                "skills": ["ux", "wireframe", "prototype", "user-research"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "DevOps Engineer",
                "department": "engineering",
                "team": "infrastructure",
                "skills": ["devops", "docker", "kubernetes", "ci/cd"],
            },
            {
                "name": "QA Engineer",
                "department": "engineering",
                "team": "core",
                "skills": ["testing", "qa", "automation"],
            },
            {
                "name": "Data Analyst",
                "department": "product",
                "team": "analytics",
                "skills": ["analytics", "sql", "visualization"],
            },
            {
                "name": "Integration Specialist",
                "department": "engineering",
                "team": "integrations",
                "skills": ["api", "integration", "webhook"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 2,
                "maxConcurrentAgents": 10,
            },
        },
    },
    "ecommerce": {
        "name": "E-Commerce Platform",
        "description": "Online store with product management, checkout, and fulfillment",
        "departments": ["engineering", "product", "design", "marketing"],
        "teams": {
            "engineering": ["frontend", "backend", "payments", "devops"],
            "product": ["catalog", "checkout"],
            "design": ["ux", "visual"],
            "marketing": ["growth", "analytics"],
        },
        "core_roles": [
            {
                "name": "E-Commerce Architect",
                "department": "engineering",
                "team": "backend",
                "skills": ["ecommerce", "architecture", "payments", "inventory"],
                "type": "persistent",
            },
            {
                "name": "Frontend Developer",
                "department": "engineering",
                "team": "frontend",
                "skills": ["frontend", "react", "css", "responsive"],
                "type": "persistent",
            },
            {
                "name": "Payments Specialist",
                "department": "engineering",
                "team": "payments",
                "skills": ["payments", "stripe", "security", "pci"],
                "type": "persistent",
            },
            {
                "name": "Product Manager",
                "department": "product",
                "team": "catalog",
                "skills": ["product", "ecommerce", "catalog", "pricing"],
                "type": "persistent",
            },
            {
                "name": "UX Designer",
                "department": "design",
                "team": "ux",
                "skills": ["ux", "ecommerce", "conversion", "checkout-flow"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "Growth Engineer",
                "department": "marketing",
                "team": "growth",
                "skills": ["seo", "analytics", "a/b-testing"],
            },
            {
                "name": "Backend Developer",
                "department": "engineering",
                "team": "backend",
                "skills": ["backend", "api", "database"],
            },
            {
                "name": "DevOps Engineer",
                "department": "engineering",
                "team": "devops",
                "skills": ["devops", "docker", "aws"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 3,
                "maxConcurrentAgents": 15,
            },
        },
    },
    "mobile_app": {
        "name": "Mobile Application",
        "description": "Native or cross-platform mobile app with mobile-first focus",
        "departments": ["engineering", "product", "design"],
        "teams": {
            "engineering": ["mobile", "backend", "qa"],
            "product": ["mobile-product"],
            "design": ["mobile-ux", "visual"],
        },
        "core_roles": [
            {
                "name": "Mobile Developer",
                "department": "engineering",
                "team": "mobile",
                "skills": ["mobile", "react-native", "ios", "android"],
                "type": "persistent",
            },
            {
                "name": "Backend Developer",
                "department": "engineering",
                "team": "backend",
                "skills": ["backend", "api", "mobile-backend", "push-notifications"],
                "type": "persistent",
            },
            {
                "name": "Mobile UX Designer",
                "department": "design",
                "team": "mobile-ux",
                "skills": [
                    "mobile-ux",
                    "ios-guidelines",
                    "android-guidelines",
                    "prototype",
                ],
                "type": "persistent",
            },
            {
                "name": "Product Manager",
                "department": "product",
                "team": "mobile-product",
                "skills": ["product", "mobile", "app-store", "user-research"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "QA Engineer",
                "department": "engineering",
                "team": "qa",
                "skills": ["testing", "mobile-testing", "automation"],
            },
            {
                "name": "Visual Designer",
                "department": "design",
                "team": "visual",
                "skills": ["visual", "icons", "illustrations"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 2,
                "maxConcurrentAgents": 8,
            },
        },
    },
    "content_platform": {
        "name": "Content Platform",
        "description": "Content publishing, media, or social platform",
        "departments": ["engineering", "product", "design", "content"],
        "teams": {
            "engineering": ["frontend", "backend", "media"],
            "product": ["content-strategy"],
            "design": ["ux", "visual"],
            "content": ["editorial", "community"],
        },
        "core_roles": [
            {
                "name": "Content Platform Engineer",
                "department": "engineering",
                "team": "backend",
                "skills": ["cms", "backend", "api", "content-modeling"],
                "type": "persistent",
            },
            {
                "name": "Frontend Developer",
                "department": "engineering",
                "team": "frontend",
                "skills": ["frontend", "react", "seo", "performance"],
                "type": "persistent",
            },
            {
                "name": "Content Strategist",
                "department": "content",
                "team": "editorial",
                "skills": ["content-strategy", "seo", "editorial", "taxonomy"],
                "type": "persistent",
            },
            {
                "name": "UX Designer",
                "department": "design",
                "team": "ux",
                "skills": ["ux", "content-ux", "reading-experience", "navigation"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "Media Engineer",
                "department": "engineering",
                "team": "media",
                "skills": ["media", "video", "streaming", "cdn"],
            },
            {
                "name": "Community Manager",
                "department": "content",
                "team": "community",
                "skills": ["community", "moderation", "engagement"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 2,
                "maxConcurrentAgents": 10,
            },
        },
    },
    "api_service": {
        "name": "API Service",
        "description": "Developer-focused API or SDK product",
        "departments": ["engineering", "product", "developer-relations"],
        "teams": {
            "engineering": ["core-api", "sdks", "infrastructure"],
            "product": ["api-product"],
            "developer-relations": ["documentation", "community"],
        },
        "core_roles": [
            {
                "name": "API Architect",
                "department": "engineering",
                "team": "core-api",
                "skills": ["api", "rest", "graphql", "architecture", "openapi"],
                "type": "persistent",
            },
            {
                "name": "Backend Developer",
                "department": "engineering",
                "team": "core-api",
                "skills": ["backend", "api", "performance", "security"],
                "type": "persistent",
            },
            {
                "name": "SDK Developer",
                "department": "engineering",
                "team": "sdks",
                "skills": ["sdk", "typescript", "python", "developer-experience"],
                "type": "persistent",
            },
            {
                "name": "Technical Writer",
                "department": "developer-relations",
                "team": "documentation",
                "skills": ["documentation", "api-docs", "tutorials", "examples"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "Developer Advocate",
                "department": "developer-relations",
                "team": "community",
                "skills": ["developer-advocacy", "community", "content"],
            },
            {
                "name": "DevOps Engineer",
                "department": "engineering",
                "team": "infrastructure",
                "skills": ["devops", "reliability", "monitoring"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 2,
                "maxConcurrentAgents": 8,
            },
        },
    },
    "data_platform": {
        "name": "Data Platform",
        "description": "Data engineering, ML, or analytics platform",
        "departments": ["engineering", "data-science", "product"],
        "teams": {
            "engineering": ["data-engineering", "infrastructure", "frontend"],
            "data-science": ["ml", "analytics"],
            "product": ["data-product"],
        },
        "core_roles": [
            {
                "name": "Data Engineer",
                "department": "engineering",
                "team": "data-engineering",
                "skills": ["data-engineering", "etl", "pipeline", "sql", "spark"],
                "type": "persistent",
            },
            {
                "name": "ML Engineer",
                "department": "data-science",
                "team": "ml",
                "skills": ["ml", "python", "tensorflow", "model-deployment"],
                "type": "persistent",
            },
            {
                "name": "Data Analyst",
                "department": "data-science",
                "team": "analytics",
                "skills": ["analytics", "sql", "visualization", "reporting"],
                "type": "persistent",
            },
            {
                "name": "Platform Engineer",
                "department": "engineering",
                "team": "infrastructure",
                "skills": ["infrastructure", "kubernetes", "airflow", "spark"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "Frontend Developer",
                "department": "engineering",
                "team": "frontend",
                "skills": ["frontend", "visualization", "dashboard"],
            },
            {
                "name": "Product Manager",
                "department": "product",
                "team": "data-product",
                "skills": ["product", "data", "analytics"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 3,
                "maxConcurrentAgents": 12,
            },
        },
    },
    "agency": {
        "name": "Agency / Consulting",
        "description": "Flexible consulting or agency with project-based work",
        "departments": ["engineering", "design", "project-management"],
        "teams": {
            "engineering": ["fullstack", "specialists"],
            "design": ["ux-ui"],
            "project-management": ["delivery"],
        },
        "core_roles": [
            {
                "name": "Tech Lead",
                "department": "engineering",
                "team": "fullstack",
                "skills": [
                    "architecture",
                    "fullstack",
                    "client-communication",
                    "estimation",
                ],
                "type": "persistent",
            },
            {
                "name": "Full-Stack Developer",
                "department": "engineering",
                "team": "fullstack",
                "skills": ["frontend", "backend", "database", "api"],
                "type": "persistent",
            },
            {
                "name": "UX/UI Designer",
                "department": "design",
                "team": "ux-ui",
                "skills": ["ux", "ui", "prototype", "figma"],
                "type": "persistent",
            },
            {
                "name": "Project Manager",
                "department": "project-management",
                "team": "delivery",
                "skills": [
                    "project-management",
                    "scrum",
                    "client-communication",
                    "estimation",
                ],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "DevOps Specialist",
                "department": "engineering",
                "team": "specialists",
                "skills": ["devops", "aws", "docker"],
            },
            {
                "name": "QA Specialist",
                "department": "engineering",
                "team": "specialists",
                "skills": ["testing", "qa", "automation"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 4,
                "maxConcurrentAgents": 20,
            },
        },
    },
    # WS-068-008: Autonomous operation template
    "autonomous": {
        "name": "Autonomous AI Company",
        "description": "Full autonomous operation with daemon, agent teams, and self-healing",
        "departments": ["engineering", "product", "operations"],
        "teams": {
            "engineering": ["core", "security", "infrastructure"],
            "product": ["product-strategy", "analytics"],
            "operations": ["support", "qa"],
        },
        "core_roles": [
            {
                "name": "Senior Architect",
                "department": "engineering",
                "team": "core",
                "skills": ["architecture", "design-decisions", "roadmap", "patterns"],
                "type": "persistent",
                "role": "lead",
            },
            {
                "name": "Senior Developer",
                "department": "engineering",
                "team": "core",
                "skills": ["python", "backend", "api", "database"],
                "type": "persistent",
            },
            {
                "name": "Security Engineer",
                "department": "engineering",
                "team": "security",
                "skills": ["security", "owasp", "secrets-scanning", "dependency-audit"],
                "type": "persistent",
            },
            {
                "name": "DevOps Engineer",
                "department": "engineering",
                "team": "infrastructure",
                "skills": ["devops", "docker", "ci-cd", "installation"],
                "type": "persistent",
            },
            {
                "name": "QA Engineer",
                "department": "operations",
                "team": "qa",
                "skills": ["testing", "qa", "automation", "pytest"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "Frontend Developer",
                "department": "engineering",
                "team": "core",
                "skills": ["frontend", "dashboard", "react", "css"],
            },
            {
                "name": "Technical Writer",
                "department": "product",
                "team": "product-strategy",
                "skills": ["documentation", "technical-writing"],
            },
            {
                "name": "Data Analyst",
                "department": "product",
                "team": "analytics",
                "skills": ["data-analysis", "analytics", "sql"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 3,
                "maxConcurrentAgents": 15,
            },
            # Daemon configuration for autonomous operation
            "daemon": {
                "enabled": True,
                "pollIntervalSeconds": 30,
                "maxIdleCycles": 50,
                "complexityThreshold": "standard",
            },
            # Agent teams for complex tasks
            "agentTeams": {
                "enabled": True,
                "experimentalAcknowledged": True,
                "triggerConditions": {
                    "epicTasks": True,
                    "complexTasks": True,
                    "stuckTasks": True,
                    "stuckRetryThreshold": 2,
                },
                "composition": {
                    "teamSize": "dynamic",
                    "maxTeammates": 4,
                    "dynamicSizing": {
                        "enabled": True,
                        "baseSizeByComplexity": {
                            "trivial": 1,
                            "standard": 1,
                            "complex": 2,
                            "epic": 4,
                        },
                    },
                },
            },
            # Self-healing configuration
            "selfHealing": {
                "enabled": True,
                "maxRetries": 3,
                "retryDelaySeconds": 60,
                "autoFixHints": True,
            },
            # Full autonomy features
            "fullAutonomy": {
                "enabled": True,
                "goalScheduler": {"enabled": True, "reorderInterval": 300},
                "budgetGovernor": {"enabled": True},
                "learningLoop": {"enabled": True},
                "adaptiveScheduler": {"enabled": True},
                "sessionContinuity": {"enabled": True},
                "approvalLearner": {"enabled": True, "minSuccessRateForAuto": 0.95},
            },
        },
    },
    "minimal": {
        "name": "Minimal Setup",
        "description": "Basic engineering-only setup for simple projects",
        "departments": ["engineering"],
        "teams": {
            "engineering": ["core"],
        },
        "core_roles": [
            {
                "name": "Developer",
                "department": "engineering",
                "team": "core",
                "skills": ["fullstack", "frontend", "backend"],
                "type": "persistent",
            },
        ],
        "optional_roles": [
            {
                "name": "DevOps",
                "department": "engineering",
                "team": "core",
                "skills": ["devops", "docker"],
            },
        ],
        "recommended_config": {
            "workAllocationMode": "pull",
            "agents": {
                "maxConcurrentTasks": 2,
                "maxConcurrentAgents": 5,
            },
        },
    },
}


def detect_domain(description: str) -> str:
    """
    Analyze a project description to suggest the best matching template.

    Uses keyword matching to identify the domain. Returns template name.

    Args:
        description: Natural language project description

    Returns:
        Template name (e.g., "saas_platform", "ecommerce")
    """
    if not description:
        return "minimal"

    description_lower = description.lower()
    scores: dict[str, int] = {}

    for template_name, keywords in DOMAIN_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            # Check for keyword match (word boundary aware for multi-word keywords)
            if " " in keyword:
                # Multi-word keyword - simple substring match
                if keyword in description_lower:
                    score += 2  # Higher weight for specific multi-word matches
            else:
                # Single word - use word boundary
                if re.search(rf"\b{re.escape(keyword)}\b", description_lower):
                    score += 1

        if score > 0:
            scores[template_name] = score

    if not scores:
        return "minimal"

    # Return template with highest score
    return max(scores, key=lambda k: scores[k])


def get_template(template_name: str) -> dict | None:
    """
    Get a full template by name.

    Args:
        template_name: Name of the template (e.g., "saas_platform")

    Returns:
        Full template dict, or None if not found
    """
    return TEMPLATES.get(template_name)


def list_templates() -> list[dict]:
    """
    List all available templates with basic info.

    Returns:
        List of dicts with name, description, departments for each template
    """
    result = []
    for key, template in TEMPLATES.items():
        result.append(
            {
                "id": key,
                "name": template["name"],
                "description": template["description"],
                "departments": template["departments"],
                "core_roles_count": len(template["core_roles"]),
                "optional_roles_count": len(template.get("optional_roles", [])),
            }
        )
    return result


def get_template_summary(template_name: str) -> str:
    """
    Get a formatted summary of a template for display.

    Args:
        template_name: Name of the template

    Returns:
        Formatted markdown string with template details
    """
    template = get_template(template_name)
    if not template:
        return f"Template '{template_name}' not found."

    lines = [
        f"## {template['name']}",
        "",
        template["description"],
        "",
        "### Departments",
        "",
    ]

    for dept in template["departments"]:
        teams = template["teams"].get(dept, [])
        lines.append(f"- **{dept}**: {', '.join(teams)}")

    lines.extend(
        [
            "",
            "### Core Roles (hired automatically)",
            "",
        ]
    )

    for role in template["core_roles"]:
        lines.append(
            f"- {role['name']} ({role['department']}/{role.get('team', 'general')})"
        )

    if template.get("optional_roles"):
        lines.extend(
            [
                "",
                "### Optional Roles (can be added later)",
                "",
            ]
        )
        for role in template["optional_roles"]:
            lines.append(
                f"- {role['name']} ({role['department']}/{role.get('team', 'general')})"
            )

    return "\n".join(lines)


def print_help():
    """Print usage help."""
    help_text = """
Organization Templates — Pre-defined structures for business domains

Commands:
    detect [description]    Detect best template from project description
    get [template-name]     Get full template as JSON
    list                    List all available templates
    summary [template-name] Get formatted summary of a template

Examples:
    python org_templates.py detect "Building a SaaS analytics dashboard"
    python org_templates.py get saas_platform
    python org_templates.py list
    python org_templates.py summary ecommerce

Available templates:
"""
    print(help_text)
    for t in list_templates():
        print(f"  {t['id']:20} - {t['description'][:50]}...")


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command == "detect":
        if len(sys.argv) < 3:
            print("Error: description required")
            sys.exit(1)
        description = " ".join(sys.argv[2:])
        result = detect_domain(description)
        print(
            json.dumps(
                {
                    "template": result,
                    "description": description,
                    "template_info": get_template(result),
                },
                indent=2,
            )
        )

    elif command == "get":
        if len(sys.argv) < 3:
            print("Error: template name required")
            sys.exit(1)
        template_name = sys.argv[2]
        template = get_template(template_name)
        if template:
            print(json.dumps(template, indent=2))
        else:
            print(json.dumps({"error": f"Template '{template_name}' not found"}))
            sys.exit(1)

    elif command == "list":
        templates = list_templates()
        print(json.dumps(templates, indent=2))

    elif command == "summary":
        if len(sys.argv) < 3:
            print("Error: template name required")
            sys.exit(1)
        template_name = sys.argv[2]
        print(get_template_summary(template_name))

    elif command in ("help", "--help", "-h"):
        print_help()

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
