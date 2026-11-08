# /// script
# requires-python = ">=3.10"
# ///
"""
Work Decomposer — transforms natural language requests into structured work items.

Takes a natural language request and decomposes it into:
- Departments involved (based on keyword analysis)
- Estimated complexity (trivial/standard/complex/epic)
- Work items per department with dependencies
- Execution waves (parallel where possible)

Multi-Project Support (v1.2):
- Detects current project context via company_resolver
- Scopes task IDs to project (e.g., "projectA:ENG-abc123")
- Supports cross-project dependencies (e.g., "projectB:ENG-def456")
- Routes decomposed tasks to company-level work queue

Usage:
    echo "Build a user dashboard with auth" | python work_decomposer.py
    python work_decomposer.py "Add payment integration with Stripe"
    python work_decomposer.py --route-to-queue "Build feature X"

Output: JSON with departments, complexity, work_items, and waves.
"""

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime

# Import company_resolver and work_allocator from the same directory
try:
    from . import company_resolver, work_allocator
except ImportError:
    # Fallback for direct script execution
    import company_resolver  # type: ignore
    import work_allocator  # type: ignore


# -----------------------------------------------------------------------------
# Department Keyword Mappings
# -----------------------------------------------------------------------------

DEPARTMENT_KEYWORDS = {
    "engineering": {
        "keywords": [
            # Backend
            "api",
            "backend",
            "server",
            "database",
            "db",
            "sql",
            "nosql",
            "orm",
            "schema",
            "migration",
            "query",
            "endpoint",
            "rest",
            "graphql",
            "grpc",
            # Frontend
            "frontend",
            "react",
            "vue",
            "angular",
            "svelte",
            "component",
            "ui",
            "javascript",
            "typescript",
            "css",
            "html",
            "dom",
            "state management",
            # General dev
            "code",
            "implement",
            "build",
            "develop",
            "refactor",
            "optimize",
            "debug",
            "fix",
            "bug",
            "test",
            "unit test",
            "integration test",
            "performance",
            "cache",
            "async",
            "concurrency",
            "algorithm",
            # Infrastructure
            "devops",
            "ci/cd",
            "docker",
            "kubernetes",
            "k8s",
            "terraform",
            "deploy",
            "pipeline",
            "infrastructure",
            "aws",
            "gcp",
            "azure",
            "monitoring",
            "logging",
            "metrics",
            "observability",
            # Security
            "auth",
            "authentication",
            "authorization",
            "oauth",
            "jwt",
            "token",
            "encryption",
            "security",
            "rbac",
            "permission",
            "password",
            "secret",
        ],
        "capabilities": [
            "backend",
            "frontend",
            "api",
            "database",
            "devops",
            "security",
            "testing",
            "performance",
            "architecture",
        ],
    },
    "design": {
        "keywords": [
            # UX
            "ux",
            "user experience",
            "usability",
            "user flow",
            "wireframe",
            "prototype",
            "user journey",
            "accessibility",
            "a11y",
            "wcag",
            "interaction",
            "navigation",
            "information architecture",
            # Visual
            "visual",
            "ui design",
            "mockup",
            "figma",
            "sketch",
            "design system",
            "color",
            "typography",
            "icon",
            "illustration",
            "animation",
            "branding",
            "style guide",
            "responsive",
            "mobile design",
            "layout",
            "spacing",
            "grid",
        ],
        "capabilities": [
            "ux-design",
            "visual-design",
            "prototyping",
            "design-system",
            "accessibility",
            "user-research",
        ],
    },
    "product": {
        "keywords": [
            # Strategy
            "product",
            "feature",
            "roadmap",
            "requirements",
            "spec",
            "specification",
            "prd",
            "user story",
            "acceptance criteria",
            "prioritize",
            "backlog",
            "mvp",
            "scope",
            # Research
            "user research",
            "interview",
            "survey",
            "analytics",
            "metrics",
            "a/b test",
            "experiment",
            "hypothesis",
            "feedback",
            "insight",
            "persona",
            "market research",
            "competitive analysis",
        ],
        "capabilities": [
            "product-strategy",
            "requirements",
            "user-research",
            "analytics",
            "prioritization",
        ],
    },
}

# Complexity indicators
# Note: trivial keywords should be very specific single-task fixes
# standard keywords are general actions that can vary in scope
# complex/epic keywords indicate larger structural work
COMPLEXITY_INDICATORS = {
    "trivial": {
        "keywords": [
            "typo",
            "tweak",
            "small fix",
            "minor fix",
            "config change",
            "update text",
            "rename",
            "change label",
            "fix spacing",
            "quick fix",
            "hotfix",
            "bump version",
        ],
        "max_departments": 1,
        "max_items": 2,
    },
    "standard": {
        "keywords": [
            # These are action words that suggest moderate scope
            # but context matters more than presence
        ],
        "max_departments": 2,
        "max_items": 5,
    },
    "complex": {
        "keywords": [
            "refactor",
            "redesign",
            "integrate",
            "migration",
            "overhaul",
            "system",
            "architecture",
            "platform",
            "multiple",
            "several",
            "dashboard",
            "workflow",
            "pipeline",
            "multi-step",
        ],
        "max_departments": 3,
        "max_items": 10,
    },
    "epic": {
        "keywords": [
            "new product",
            "launch",
            "full rewrite",
            "major",
            "enterprise",
            "complete",
            "end-to-end",
            "entire",
            "from scratch",
            "full-stack",
            "comprehensive",
            "all features",
        ],
        "max_departments": 4,
        "max_items": 20,
    },
}

# Cross-department dependency patterns
DEPENDENCY_PATTERNS = [
    # (upstream, downstream, trigger_keywords)
    ("product", "design", ["user flow", "feature", "requirements"]),
    ("design", "engineering", ["mockup", "design", "ui", "ux"]),
    ("product", "engineering", ["spec", "requirements", "api"]),
]


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class WorkItem:
    """Represents a single work item for a department."""

    task_id: str
    title: str
    description: str
    department: str
    required_capabilities: list[str] = field(default_factory=list)
    estimated_hours: float = 4.0
    priority: str = "medium"  # low, medium, high, critical
    dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    # Multi-project support fields
    project_id: str | None = None  # Project this task belongs to
    cross_project_dependencies: list[str] = field(
        default_factory=list
    )  # Dependencies from other projects


@dataclass
class DecompositionResult:
    """Complete work decomposition result."""

    original_request: str
    departments: list[str]
    complexity: str
    estimated_total_hours: float
    work_items: list[dict]
    waves: list[list[str]]  # Each wave is a list of task_ids
    cross_department_dependencies: list[dict]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # Multi-project support fields
    project_id: str | None = None  # Project this decomposition belongs to
    multi_project_mode: bool = False  # Whether in multi-project mode
    cross_project_dependencies: list[dict] = field(
        default_factory=list
    )  # Dependencies on other projects
    routed_to_queue: bool = False  # Whether tasks were routed to company work queue


# -----------------------------------------------------------------------------
# Analysis Functions
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Project Context Functions
# -----------------------------------------------------------------------------


def get_project_context() -> dict | None:
    """
    Get the current project context using company_resolver.

    Returns:
        Dictionary with project information if in multi-project mode:
        - project_id: Unique identifier for this project
        - company_root: Path to the company root directory
        - multi_project_mode: True

        Returns None if not in multi-project mode.
    """
    return company_resolver.get_current_project()


def get_current_project_id() -> str | None:
    """Get the current project ID if in multi-project mode."""
    project_info = get_project_context()
    if project_info:
        return project_info.get("project_id")
    return None


def is_multi_project_mode() -> bool:
    """Check if operating in multi-project mode."""
    return company_resolver.is_multi_project_mode()


# -----------------------------------------------------------------------------
# Task ID Generation (with project scoping)
# -----------------------------------------------------------------------------


def generate_task_id(
    text: str, department: str, index: int, project_id: str | None = None
) -> str:
    """
    Generate a project-scoped unique task ID.

    In multi-project mode, task IDs are scoped to the project:
        "projectA:ENG-abc123"

    In legacy single-project mode:
        "ENG-abc123"

    Args:
        text: Source text for hash generation
        department: Department name (used for prefix)
        index: Task index within the decomposition
        project_id: Optional project ID (auto-detected if None)

    Returns:
        A scoped task ID string.
    """
    hash_input = f"{text}-{department}-{index}-{datetime.now().isoformat()}"
    short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:6]
    dept_prefix = department[:3].upper()
    base_id = f"{dept_prefix}-{short_hash}"

    # Auto-detect project_id if not provided
    if project_id is None:
        project_id = get_current_project_id()

    # Scope to project if in multi-project mode
    if project_id:
        return f"{project_id}:{base_id}"
    return base_id


def parse_scoped_task_id(task_id: str) -> tuple[str | None, str]:
    """
    Parse a potentially scoped task ID.

    Args:
        task_id: Task ID that may be scoped (e.g., "projectA:ENG-abc123")

    Returns:
        Tuple of (project_id, base_task_id).
        project_id is None if the task_id is not scoped.

    Examples:
        >>> parse_scoped_task_id("projectA:ENG-abc123")
        ("projectA", "ENG-abc123")

        >>> parse_scoped_task_id("ENG-abc123")
        (None, "ENG-abc123")
    """
    if ":" in task_id:
        parts = task_id.split(":", 1)
        return (parts[0], parts[1])
    return (None, task_id)


def is_cross_project_dependency(
    dependency_id: str, current_project_id: str | None
) -> bool:
    """
    Check if a dependency is a cross-project dependency.

    Args:
        dependency_id: The task ID of the dependency
        current_project_id: The current project's ID

    Returns:
        True if the dependency is from a different project.
    """
    dep_project, _ = parse_scoped_task_id(dependency_id)

    # If dependency has no project scope, it's not cross-project
    if dep_project is None:
        return False

    # If current project has no ID (legacy mode), any scoped dependency is cross-project
    if current_project_id is None:
        return True

    # Cross-project if projects differ
    return dep_project != current_project_id


def identify_departments(text: str) -> dict[str, float]:
    """
    Identify departments involved based on keyword analysis.
    Returns dict of department_id -> confidence score (0-1).
    """
    text_lower = text.lower()
    scores: dict[str, float] = {}

    for dept_id, config in DEPARTMENT_KEYWORDS.items():
        matches = 0
        total_keywords = len(config["keywords"])

        for keyword in config["keywords"]:
            if keyword in text_lower:
                matches += 1
                # Boost for exact word match vs partial
                if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
                    matches += 0.5

        if matches > 0:
            # Normalize score (cap at 1.0)
            scores[dept_id] = min(1.0, matches / (total_keywords * 0.1))

    return scores


def estimate_complexity(text: str, dept_count: int) -> str:
    """
    Estimate work complexity from text and department involvement.
    Returns: trivial, standard, complex, or epic.

    Priority: trivial keywords override standard keywords.
    Multi-department work is automatically elevated.
    """
    text_lower = text.lower()
    word_count = len(text.split())

    # Count keyword matches per complexity level
    scores: dict[str, int] = {"trivial": 0, "standard": 0, "complex": 0, "epic": 0}

    for level, config in COMPLEXITY_INDICATORS.items():
        for keyword in config["keywords"]:
            if keyword in text_lower:
                # Word boundary match gets extra weight
                if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
                    scores[level] += 2
                else:
                    scores[level] += 1

    # Trivial indicators should win if present (typo, tweak, etc.)
    # But only if standard/complex/epic indicators are absent or weak
    if scores["trivial"] > 0 and scores["standard"] == 0 and scores["complex"] == 0:
        if dept_count == 1 and word_count < 15:
            return "trivial"

    # Epic indicators are strong signals
    if scores["epic"] > 0:
        return "epic"

    # Complex indicators
    if scores["complex"] > 0:
        return "complex"

    # Multi-department work is at least "complex"
    if dept_count >= 3:
        return "epic"
    if dept_count >= 2:
        return "complex"

    # Standard indicators or default behavior
    if scores["standard"] > 0:
        # Longer text with standard keywords -> may be complex
        if word_count > 30:
            return "complex"
        return "standard"

    # Infer from text length alone (no keyword matches at this point)
    if word_count > 50:
        return "complex"
    elif word_count >= 6:
        return "standard"
    else:
        return "trivial"


def extract_capabilities(text: str, department: str) -> list[str]:
    """Extract required capabilities for a department from the text."""
    text_lower = text.lower()
    dept_config = DEPARTMENT_KEYWORDS.get(department, {})
    all_caps = dept_config.get("capabilities", [])

    matched = []
    for cap in all_caps:
        # Check if capability keywords appear in text
        cap_keywords = cap.replace("-", " ").split()
        if any(kw in text_lower for kw in cap_keywords):
            matched.append(cap)

    # Default capabilities if none matched
    if not matched and all_caps:
        matched = [all_caps[0]]

    return matched


def estimate_hours(complexity: str, item_count: int) -> float:
    """Estimate hours based on complexity and number of items."""
    base_hours = {
        "trivial": 1,
        "standard": 4,
        "complex": 8,
        "epic": 16,
    }
    return base_hours.get(complexity, 4) * max(1, item_count * 0.5)


def determine_priority(text: str, is_first_wave: bool) -> str:
    """Determine work item priority from context."""
    text_lower = text.lower()

    if any(kw in text_lower for kw in ["urgent", "asap", "critical", "blocker"]):
        return "critical"
    elif any(kw in text_lower for kw in ["important", "high priority", "soon"]):
        return "high"
    elif is_first_wave:
        return "high"
    else:
        return "medium"


def generate_acceptance_criteria(title: str, department: str) -> list[str]:
    """Generate default acceptance criteria based on title and department."""
    criteria = []

    if department == "engineering":
        criteria.extend(
            [
                f"Implementation complete for: {title}",
                "All tests passing",
                "Code reviewed and approved",
                "No security vulnerabilities introduced",
            ]
        )
    elif department == "design":
        criteria.extend(
            [
                f"Design complete for: {title}",
                "Reviewed by stakeholders",
                "Exported assets available",
                "Design specs documented",
            ]
        )
    elif department == "product":
        criteria.extend(
            [
                f"Requirements documented for: {title}",
                "Stakeholder sign-off obtained",
                "Acceptance criteria defined",
                "Success metrics identified",
            ]
        )

    return criteria


def create_work_items(
    text: str,
    departments: list[str],
    complexity: str,
    project_id: str | None = None,
    external_dependencies: list[str] | None = None,
) -> list[WorkItem]:
    """
    Create work items for each involved department.

    Args:
        text: The natural language request
        departments: List of departments involved
        complexity: Estimated complexity level
        project_id: Optional project ID (auto-detected if None)
        external_dependencies: Optional list of cross-project dependency task IDs

    Returns:
        List of WorkItem objects with project-scoped task IDs.
    """
    items: list[WorkItem] = []
    text_lower = text.lower()

    # Auto-detect project_id if not provided
    if project_id is None:
        project_id = get_current_project_id()

    # Determine work patterns based on text analysis
    work_patterns = []

    # Engineering patterns
    if "engineering" in departments:
        if any(kw in text_lower for kw in ["api", "endpoint", "backend"]):
            work_patterns.append(
                ("engineering", "API Development", "Build and test API endpoints")
            )
        if any(kw in text_lower for kw in ["frontend", "ui", "component", "react"]):
            work_patterns.append(
                ("engineering", "Frontend Implementation", "Implement UI components")
            )
        if any(kw in text_lower for kw in ["database", "schema", "migration"]):
            work_patterns.append(
                ("engineering", "Database Work", "Database schema and migrations")
            )
        if any(kw in text_lower for kw in ["auth", "security", "permission"]):
            work_patterns.append(
                (
                    "engineering",
                    "Security Implementation",
                    "Implement security features",
                )
            )
        if any(kw in text_lower for kw in ["test", "testing"]):
            work_patterns.append(
                ("engineering", "Testing", "Write comprehensive tests")
            )
        if any(kw in text_lower for kw in ["devops", "deploy", "ci/cd"]):
            work_patterns.append(
                ("engineering", "DevOps Setup", "Configure deployment pipeline")
            )

        # Default engineering task if none matched
        if not any(p[0] == "engineering" for p in work_patterns):
            work_patterns.append(
                (
                    "engineering",
                    "Implementation",
                    "Implement the requested functionality",
                )
            )

    # Design patterns
    if "design" in departments:
        if any(kw in text_lower for kw in ["ux", "user flow", "wireframe"]):
            work_patterns.append(
                ("design", "UX Design", "Design user flows and wireframes")
            )
        if any(kw in text_lower for kw in ["visual", "mockup", "ui design"]):
            work_patterns.append(("design", "Visual Design", "Create visual mockups"))
        if any(kw in text_lower for kw in ["a11y", "accessibility"]):
            work_patterns.append(
                ("design", "Accessibility Review", "Ensure accessibility compliance")
            )

        # Default design task if none matched
        if not any(p[0] == "design" for p in work_patterns):
            work_patterns.append(
                ("design", "Design Work", "Create designs for the feature")
            )

    # Product patterns
    if "product" in departments:
        if any(kw in text_lower for kw in ["requirements", "spec", "prd"]):
            work_patterns.append(
                ("product", "Requirements Definition", "Define detailed requirements")
            )
        if any(kw in text_lower for kw in ["research", "user research", "interview"]):
            work_patterns.append(("product", "User Research", "Conduct user research"))
        if any(kw in text_lower for kw in ["analytics", "metrics"]):
            work_patterns.append(
                ("product", "Analytics Setup", "Define and setup analytics")
            )

        # Default product task if none matched
        if not any(p[0] == "product" for p in work_patterns):
            work_patterns.append(
                ("product", "Product Definition", "Define product requirements")
            )

    # Create work items from patterns
    for idx, (dept, title, desc) in enumerate(work_patterns):
        task_id = generate_task_id(text, dept, idx, project_id)

        hours = estimate_hours(complexity, 1)
        if complexity == "trivial":
            hours = 1
        elif complexity == "standard":
            hours = 4
        elif complexity == "complex":
            hours = 8
        else:
            hours = 16

        # Separate cross-project dependencies from internal dependencies
        cross_project_deps = []
        if external_dependencies:
            for dep_id in external_dependencies:
                if is_cross_project_dependency(dep_id, project_id):
                    cross_project_deps.append(dep_id)

        items.append(
            WorkItem(
                task_id=task_id,
                title=title,
                description=f"{desc}: {text[:100]}{'...' if len(text) > 100 else ''}",
                department=dept,
                required_capabilities=extract_capabilities(text, dept),
                estimated_hours=hours,
                priority=determine_priority(text, idx == 0),
                dependencies=[],
                acceptance_criteria=generate_acceptance_criteria(title, dept),
                project_id=project_id,
                cross_project_dependencies=cross_project_deps,
            )
        )

    return items


def build_dependency_graph(
    items: list[WorkItem],
    departments: list[str],
    project_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Build cross-department dependencies based on patterns.

    Args:
        items: List of work items
        departments: List of departments involved
        project_id: Current project ID for cross-project detection

    Returns:
        Tuple of (internal_dependencies, cross_project_dependencies).
        Each is a list of dependency dictionaries.
    """
    internal_dependencies = []
    cross_project_deps = []

    # Create department -> task_ids mapping
    dept_tasks: dict[str, list[str]] = {}
    for item in items:
        if item.department not in dept_tasks:
            dept_tasks[item.department] = []
        dept_tasks[item.department].append(item.task_id)

    # Apply dependency patterns
    for upstream, downstream, _ in DEPENDENCY_PATTERNS:
        if upstream in dept_tasks and downstream in dept_tasks:
            # All downstream tasks depend on upstream tasks
            for up_id in dept_tasks[upstream]:
                for down_id in dept_tasks[downstream]:
                    dep_entry = {
                        "from": up_id,
                        "to": down_id,
                        "type": "blocks",
                    }
                    internal_dependencies.append(dep_entry)

                    # Update work item dependencies
                    for item in items:
                        if item.task_id == down_id:
                            if up_id not in item.dependencies:
                                item.dependencies.append(up_id)

    # Collect cross-project dependencies from work items
    for item in items:
        for cross_dep in item.cross_project_dependencies:
            cross_project_deps.append(
                {
                    "from": cross_dep,
                    "to": item.task_id,
                    "type": "blocks",
                    "cross_project": True,
                }
            )

    return internal_dependencies, cross_project_deps


# -----------------------------------------------------------------------------
# Work Queue Routing
# -----------------------------------------------------------------------------


def priority_str_to_int(priority_str: str) -> int:
    """Convert priority string to integer (1-4)."""
    mapping = {
        "critical": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }
    return mapping.get(priority_str.lower(), 3)


def route_to_work_queue(
    items: list[WorkItem],
    project_id: str | None = None,
) -> dict:
    """
    Route decomposed work items to the company-level work queue.

    Uses work_allocator.add_task to add each work item to the queue.
    Cross-project dependencies are preserved and will be checked by
    the work queue's dependency resolution.

    Args:
        items: List of WorkItem objects to route
        project_id: Project ID (auto-detected if None)

    Returns:
        Dictionary with routing results:
        - success: Overall success status
        - routed_count: Number of tasks successfully routed
        - failed_count: Number of tasks that failed to route
        - task_ids: List of task IDs that were routed
        - errors: List of errors for failed tasks
    """
    if project_id is None:
        project_id = get_current_project_id()

    results = {
        "success": True,
        "routed_count": 0,
        "failed_count": 0,
        "task_ids": [],
        "errors": [],
    }

    for item in items:
        try:
            # Combine internal and cross-project dependencies
            all_deps = item.dependencies + item.cross_project_dependencies

            # WS-108: Build rich description with acceptance criteria
            description_parts = [item.description]

            # Include acceptance criteria if present
            if hasattr(item, "acceptance_criteria") and item.acceptance_criteria:
                description_parts.append("")
                description_parts.append("**Acceptance Criteria:**")
                for criterion in item.acceptance_criteria:
                    description_parts.append(f"- [ ] {criterion}")

            # Include dependency context
            if all_deps:
                description_parts.append("")
                description_parts.append(f"**Dependencies:** {', '.join(all_deps)}")

            # Include estimated hours for planning
            if hasattr(item, "estimated_hours"):
                description_parts.append("")
                description_parts.append(
                    f"**Estimated Effort:** {item.estimated_hours:.1f} hours"
                )

            rich_description = "\n".join(description_parts)

            # Add task to the work queue
            result = work_allocator.add_task(
                title=item.title,
                priority=priority_str_to_int(item.priority),
                department=item.department,
                required_capabilities=item.required_capabilities,
                deadline=None,  # Could be enhanced to parse deadlines from text
                estimated_complexity=determine_complexity_from_hours(
                    item.estimated_hours
                ),
                dependencies=all_deps if all_deps else None,
                description=rich_description,
                project_id=project_id,
            )

            if result.get("success"):
                results["routed_count"] += 1
                results["task_ids"].append(result["task_id"])
            else:
                results["failed_count"] += 1
                results["errors"].append(
                    {
                        "task_id": item.task_id,
                        "error": result.get("error", "Unknown error"),
                    }
                )

        except Exception as e:
            results["failed_count"] += 1
            results["errors"].append(
                {
                    "task_id": item.task_id,
                    "error": str(e),
                }
            )

    if results["failed_count"] > 0:
        results["success"] = False

    return results


def determine_complexity_from_hours(hours: float) -> str:
    """Convert estimated hours to complexity level."""
    if hours <= 2:
        return "trivial"
    elif hours <= 6:
        return "standard"
    elif hours <= 12:
        return "complex"
    else:
        return "epic"


def organize_into_waves(items: list[WorkItem]) -> list[list[str]]:
    """
    Organize work items into execution waves.
    Wave 1: Items with no dependencies (can run in parallel)
    Wave N: Items whose dependencies are in earlier waves
    """
    if not items:
        return []

    # Build task lookup and remaining dependencies
    task_ids = {item.task_id for item in items}
    remaining_deps: dict[str, set[str]] = {}
    for item in items:
        # Only include dependencies that are in our item set
        remaining_deps[item.task_id] = set(
            d for d in item.dependencies if d in task_ids
        )

    waves: list[list[str]] = []
    assigned = set()

    while len(assigned) < len(items):
        # Find items with all dependencies satisfied
        wave = []
        for item in items:
            if item.task_id not in assigned:
                # Check if all dependencies are assigned
                if remaining_deps[item.task_id].issubset(assigned):
                    wave.append(item.task_id)

        if not wave:
            # Circular dependency or error - add remaining items
            wave = [item.task_id for item in items if item.task_id not in assigned]

        waves.append(wave)
        assigned.update(wave)

    return waves


def decompose_work(
    request: str,
    project_id: str | None = None,
    external_dependencies: list[str] | None = None,
    route_to_queue: bool = False,
) -> DecompositionResult:
    """
    Main entry point: decompose a natural language request into work items.

    Args:
        request: Natural language request to decompose
        project_id: Optional project ID (auto-detected if None)
        external_dependencies: Optional list of cross-project dependency task IDs
            Format: ["projectB:ENG-abc123", "projectC:DES-def456"]
        route_to_queue: If True, route tasks to the company work queue

    Returns:
        DecompositionResult with project-scoped task IDs and dependencies.
    """
    # Get project context
    if project_id is None:
        project_id = get_current_project_id()
    multi_project = is_multi_project_mode()

    # Step 1: Identify departments
    dept_scores = identify_departments(request)

    # Filter to departments with meaningful scores
    involved_depts = [
        dept
        for dept, score in sorted(dept_scores.items(), key=lambda x: x[1], reverse=True)
        if score > 0.1
    ]

    # Ensure at least one department (default to engineering)
    if not involved_depts:
        involved_depts = ["engineering"]

    # Step 2: Estimate complexity
    complexity = estimate_complexity(request, len(involved_depts))

    # Step 3: Create work items (with project scoping)
    items = create_work_items(
        request,
        involved_depts,
        complexity,
        project_id=project_id,
        external_dependencies=external_dependencies,
    )

    # Step 4: Build dependencies (including cross-project)
    internal_deps, cross_project_deps = build_dependency_graph(
        items, involved_depts, project_id
    )

    # Step 5: Organize into waves
    waves = organize_into_waves(items)

    # Step 6: Calculate total hours
    total_hours = sum(item.estimated_hours for item in items)

    # Step 7: Route to work queue if requested
    routed = False
    if route_to_queue:
        routing_result = route_to_work_queue(items, project_id)
        routed = routing_result.get("success", False)

    return DecompositionResult(
        original_request=request,
        departments=involved_depts,
        complexity=complexity,
        estimated_total_hours=total_hours,
        work_items=[asdict(item) for item in items],
        waves=waves,
        cross_department_dependencies=internal_deps,
        project_id=project_id,
        multi_project_mode=multi_project,
        cross_project_dependencies=cross_project_deps,
        routed_to_queue=routed,
    )


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------


def parse_cli_args(args: list[str]) -> tuple[str, dict]:
    """
    Parse command line arguments.

    Returns:
        Tuple of (request_text, options_dict)
    """
    options = {
        "route_to_queue": False,
        "project_id": None,
        "dependencies": None,
    }
    request_parts = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--route-to-queue":
            options["route_to_queue"] = True
            i += 1
        elif arg == "--project-id" and i + 1 < len(args):
            options["project_id"] = args[i + 1]
            i += 2
        elif arg == "--depends-on" and i + 1 < len(args):
            # Parse comma-separated dependency list
            deps = [d.strip() for d in args[i + 1].split(",")]
            options["dependencies"] = deps
            i += 2
        elif arg in ("--help", "-h"):
            options["help"] = True
            i += 1
        elif arg.startswith("--"):
            # Unknown option, skip
            i += 1
        else:
            request_parts.append(arg)
            i += 1

    return " ".join(request_parts), options


def print_help():
    """Print usage help."""
    help_text = """
Work Decomposer — Transform natural language requests into structured work items.

Usage:
    echo "Build a user dashboard with auth" | python work_decomposer.py
    python work_decomposer.py "Add payment integration with Stripe"
    python work_decomposer.py --route-to-queue "Build feature X"

Options:
    --route-to-queue     Route decomposed tasks to company work queue
    --project-id ID      Override auto-detected project ID
    --depends-on IDS     Comma-separated list of cross-project dependencies
                         Format: "projectB:ENG-abc123,projectC:DES-def456"
    --help, -h           Show this help message

Multi-Project Support:
    In multi-project mode (when .forge-company-root is found), task IDs are
    automatically scoped to the current project:
        - Task ID format: "projectA:ENG-abc123"
        - Cross-project dependencies: "projectB:ENG-def456"

    Use --depends-on to specify dependencies on tasks in other projects.

Output:
    JSON with departments, complexity, work_items, waves, and project context.

Examples:
    # Basic decomposition
    python work_decomposer.py "Add user authentication with OAuth"

    # Decompose and route to company queue
    python work_decomposer.py --route-to-queue "Build dashboard"

    # With cross-project dependencies
    python work_decomposer.py --depends-on "auth-project:ENG-abc123" "Build profile page"

    # Override project ID
    python work_decomposer.py --project-id "myproject-123" "Build feature"
"""
    print(help_text)


def main():
    """Parse input and output work decomposition."""
    # Get input from args or stdin
    if len(sys.argv) > 1:
        request, options = parse_cli_args(sys.argv[1:])
    else:
        request = sys.stdin.read().strip()
        options = {}

    if options.get("help"):
        print_help()
        sys.exit(0)

    if not request:
        print(
            json.dumps(
                {
                    "error": "No request provided",
                    "usage": "echo 'request' | python work_decomposer.py",
                    "help": "Use --help for more information",
                },
                indent=2,
            )
        )
        sys.exit(1)

    result = decompose_work(
        request,
        project_id=options.get("project_id"),
        external_dependencies=options.get("dependencies"),
        route_to_queue=options.get("route_to_queue", False),
    )
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
