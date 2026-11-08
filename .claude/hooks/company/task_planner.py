#!/usr/bin/env python3
# P20: Task Planning Module for Daemon-Invoked Planning
# /// script
# requires-python = ">=3.10"
# ///
"""
Task Planner — Lightweight planning module for daemon-invoked task decomposition.

This module provides planning capabilities for autonomous task execution:
1. Analyzes task descriptions and requirements
2. Breaks complex tasks into subtasks
3. Identifies file dependencies
4. Organizes subtasks into parallel execution waves
5. Self-healing plan revision on execution failures (Task 20.6)

Part of P20: GSD/BMAD + Daemon Unification.

Self-Healing Plan Revision:
    When execution fails, revise_plan() analyzes the failure and applies
    intelligent revision strategies:
    - Test failure: adds test fix subtask
    - Lint failure: adds lint fix subtask
    - Import error: adds dependency prerequisite subtask
    - Timeout: splits subtask into smaller pieces
    - Max revisions (2): escalates to human review

Usage as module:
    from task_planner import plan_task, decompose_to_waves, revise_plan
    plan = plan_task({"task_id": "123", "description": "Build auth system"})
    waves = decompose_to_waves(plan)

    # On failure, revise the plan:
    from task_planner import ExecutionFailure
    failure = ExecutionFailure(subtask_id="...", error_type="test", error_message="...")
    result = revise_plan(plan, failure)
    if result.success:
        revised_plan = result.revised_plan

Usage as script:
    uv run task_planner.py plan --task-id "123"
    uv run task_planner.py plan --description "Build user authentication"
    uv run task_planner.py waves --task-id "123"
    uv run task_planner.py revise --plan-json '...' --error-output "..." --subtask-id "..."
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

if TYPE_CHECKING:
    pass

# Lazy imports for sibling modules
complexity_detector = None
company_resolver = None
work_allocator = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global complexity_detector, company_resolver, work_allocator
    if complexity_detector is not None:
        return

    try:
        from . import company_resolver as cr
        from . import work_allocator as wa

        company_resolver = cr
        work_allocator = wa
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        company_resolver = cr
        work_allocator = wa

    # Import complexity_detector from parent hooks directory
    try:
        hooks_dir = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(hooks_dir))
        import complexity_detector as cd

        complexity_detector = cd
    except ImportError:
        complexity_detector = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------


class SubtaskAction(str, Enum):
    """Action type for a subtask."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    TEST = "test"
    REVIEW = "review"


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class Subtask:
    """Represents a single subtask within a task plan."""

    id: str
    description: str
    files: list[str] = field(default_factory=list)
    action: SubtaskAction = SubtaskAction.MODIFY
    acceptance: str = ""
    estimated_minutes: int = 30
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "description": self.description,
            "files": self.files,
            "action": self.action.value,
            "acceptance": self.acceptance,
            "estimated_minutes": self.estimated_minutes,
            "depends_on": self.depends_on,
        }


@dataclass
class Wave:
    """Represents a wave of subtasks that can execute in parallel."""

    number: int
    subtask_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "number": self.number,
            "subtask_ids": self.subtask_ids,
        }


@dataclass
class TaskPlan:
    """Complete plan for a task, including subtasks and dependencies."""

    task_id: str
    complexity: str  # trivial, standard, complex, epic
    subtasks: list[Subtask] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    total_estimated_minutes: int = 0
    wave_assignment: dict[str, int] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "complexity": self.complexity,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "dependencies": self.dependencies,
            "total_estimated_minutes": self.total_estimated_minutes,
            "wave_assignment": self.wave_assignment,
            "created_at": self.created_at,
            "errors": self.errors,
        }

    def to_json(self, pretty: bool = False) -> str:
        """Convert to JSON string."""
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict())


@dataclass
class ExecutionFailure:
    """
    Captures execution failure context for plan revision.

    When a subtask fails during execution, this dataclass captures the
    failure details needed for intelligent plan revision.

    Attributes:
        subtask_id: ID of the subtask that failed
        error_type: Category of error (test, lint, import, timeout, permission, etc.)
        error_message: Full error message from execution
        files_modified: List of files that were modified before failure
        partial_progress: Dict tracking what was partially completed
        retry_count: Number of times this subtask has been retried
        failure_timestamp: ISO timestamp when failure occurred
    """

    subtask_id: str
    error_type: str
    error_message: str
    files_modified: list[str] = field(default_factory=list)
    partial_progress: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    failure_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "subtask_id": self.subtask_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "files_modified": self.files_modified,
            "partial_progress": self.partial_progress,
            "retry_count": self.retry_count,
            "failure_timestamp": self.failure_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionFailure":
        """Create from dictionary."""
        return cls(
            subtask_id=data.get("subtask_id", ""),
            error_type=data.get("error_type", "unknown"),
            error_message=data.get("error_message", ""),
            files_modified=data.get("files_modified", []),
            partial_progress=data.get("partial_progress", {}),
            retry_count=data.get("retry_count", 0),
            failure_timestamp=data.get(
                "failure_timestamp", datetime.now(timezone.utc).isoformat()
            ),
        )

    @classmethod
    def from_error_output(
        cls,
        subtask_id: str,
        error_output: str,
        files_modified: list[str] | None = None,
    ) -> "ExecutionFailure":
        """
        Create ExecutionFailure by analyzing error output.

        Automatically detects error type from common patterns.
        """
        error_type = _detect_error_type(error_output)
        return cls(
            subtask_id=subtask_id,
            error_type=error_type,
            error_message=error_output[:2000],  # Truncate long errors
            files_modified=files_modified or [],
        )


class RevisionStrategy(str, Enum):
    """Strategy for revising a plan after failure."""

    ADD_FIX_SUBTASK = "add_fix_subtask"  # Add a new subtask to fix the issue
    SPLIT_SUBTASK = "split_subtask"  # Split failed subtask into smaller ones
    REORDER_DEPENDENCIES = "reorder_dependencies"  # Change dependency order
    ADD_PREREQUISITE = "add_prerequisite"  # Add missing prerequisite subtask
    ESCALATE = "escalate"  # Cannot fix automatically, escalate to human


@dataclass
class RevisionResult:
    """Result of a plan revision attempt."""

    success: bool
    strategy_used: RevisionStrategy
    revised_plan: TaskPlan | None = None
    revision_count: int = 0
    escalated: bool = False
    message: str = ""
    fixes_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "strategy_used": self.strategy_used.value,
            "revised_plan": self.revised_plan.to_dict() if self.revised_plan else None,
            "revision_count": self.revision_count,
            "escalated": self.escalated,
            "message": self.message,
            "fixes_applied": self.fixes_applied,
        }


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Maximum number of plan revisions before escalation
MAX_REVISIONS = 2

# Base time estimates per complexity level (in minutes)
COMPLEXITY_BASE_MINUTES = {
    "trivial": 15,
    "standard": 60,
    "complex": 180,
    "epic": 480,
}

# File pattern keywords for dependency detection
FILE_PATTERNS = {
    "api": [r"api/", r"routes/", r"endpoints/", r"handlers/"],
    "database": [r"models/", r"schema", r"migration", r"\.sql$"],
    "frontend": [r"components/", r"pages/", r"views/", r"\.tsx$", r"\.vue$"],
    "test": [r"test_", r"_test\.py$", r"\.test\.", r"spec\."],
    "config": [r"config", r"settings", r"\.env", r"\.yaml$", r"\.json$"],
    "auth": [r"auth", r"permission", r"rbac", r"oauth", r"jwt"],
}

# Action keywords for subtask action detection
ACTION_KEYWORDS = {
    SubtaskAction.CREATE: ["create", "add", "new", "implement", "build", "generate"],
    SubtaskAction.MODIFY: ["update", "modify", "change", "fix", "refactor", "improve"],
    SubtaskAction.DELETE: ["delete", "remove", "cleanup", "deprecate"],
    SubtaskAction.TEST: ["test", "verify", "validate", "check", "assert"],
    SubtaskAction.REVIEW: ["review", "audit", "inspect", "analyze"],
}

# Subtask pattern templates
SUBTASK_TEMPLATES = {
    "trivial": [
        ("core", "Implement the core change", SubtaskAction.MODIFY),
    ],
    "standard": [
        ("setup", "Set up initial structure", SubtaskAction.CREATE),
        ("core", "Implement core functionality", SubtaskAction.MODIFY),
        ("test", "Add tests", SubtaskAction.TEST),
    ],
    "complex": [
        ("design", "Design and plan implementation approach", SubtaskAction.REVIEW),
        ("setup", "Set up scaffolding and dependencies", SubtaskAction.CREATE),
        ("core", "Implement core functionality", SubtaskAction.MODIFY),
        ("integration", "Integrate with existing systems", SubtaskAction.MODIFY),
        ("test", "Add comprehensive tests", SubtaskAction.TEST),
        ("review", "Review and validate implementation", SubtaskAction.REVIEW),
    ],
    "epic": [
        ("design", "Design system architecture", SubtaskAction.REVIEW),
        ("setup", "Set up project scaffolding", SubtaskAction.CREATE),
        ("data", "Implement data layer", SubtaskAction.CREATE),
        ("core", "Implement core business logic", SubtaskAction.MODIFY),
        ("api", "Build API endpoints", SubtaskAction.CREATE),
        ("frontend", "Implement frontend components", SubtaskAction.CREATE),
        ("integration", "System integration", SubtaskAction.MODIFY),
        ("test", "Comprehensive testing", SubtaskAction.TEST),
        ("security", "Security review and hardening", SubtaskAction.REVIEW),
        ("docs", "Documentation and cleanup", SubtaskAction.CREATE),
    ],
}

# Error type detection patterns for self-healing
ERROR_TYPE_PATTERNS = {
    "test": [
        r"AssertionError",
        r"FAILED",
        r"test.*failed",
        r"pytest",
        r"unittest",
        r"expected.*got",
        r"assertion failed",
        r"test_.*\.py",
    ],
    "lint": [
        r"flake8",
        r"pylint",
        r"ruff",
        r"eslint",
        r"SyntaxError",
        r"IndentationError",
        r"undefined name",
        r"unused import",
        r"line too long",
        r"missing whitespace",
    ],
    "import": [
        r"ImportError",
        r"ModuleNotFoundError",
        r"No module named",
        r"cannot import name",
        r"circular import",
    ],
    "timeout": [
        r"timeout",
        r"timed out",
        r"exceeded.*time",
        r"deadline exceeded",
        r"operation took too long",
    ],
    "permission": [
        r"PermissionError",
        r"permission denied",
        r"access denied",
        r"EACCES",
        r"Operation not permitted",
    ],
    "type": [
        r"TypeError",
        r"expected.*type",
        r"incompatible types",
        r"cannot assign",
        r"type mismatch",
    ],
    "file": [
        r"FileNotFoundError",
        r"No such file",
        r"ENOENT",
        r"file not found",
        r"path does not exist",
    ],
    "dependency": [
        r"dependency",
        r"requires",
        r"version conflict",
        r"incompatible",
        r"package.*not found",
    ],
}

# Revision strategy mapping by error type
ERROR_TYPE_TO_STRATEGY: dict[str, RevisionStrategy] = {
    "test": RevisionStrategy.ADD_FIX_SUBTASK,
    "lint": RevisionStrategy.ADD_FIX_SUBTASK,
    "import": RevisionStrategy.ADD_PREREQUISITE,
    "timeout": RevisionStrategy.SPLIT_SUBTASK,
    "permission": RevisionStrategy.ADD_PREREQUISITE,
    "type": RevisionStrategy.ADD_FIX_SUBTASK,
    "file": RevisionStrategy.ADD_PREREQUISITE,
    "dependency": RevisionStrategy.ADD_PREREQUISITE,
    "unknown": RevisionStrategy.ESCALATE,
}


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def _generate_subtask_id(task_id: str, subtask_name: str) -> str:
    """Generate a unique subtask ID."""
    hash_input = f"{task_id}-{subtask_name}-{datetime.now().isoformat()}"
    short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:6]
    return f"{task_id}.{subtask_name}-{short_hash}"


def _detect_error_type(error_output: str) -> str:
    """
    Detect error type from error output using pattern matching.

    Args:
        error_output: The error message/output to analyze

    Returns:
        Error type string (test, lint, import, timeout, permission, type, file,
        dependency, or unknown)
    """
    for error_type, patterns in ERROR_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, error_output, re.IGNORECASE):
                return error_type

    return "unknown"


def _detect_complexity(description: str) -> str:
    """
    Detect task complexity using the complexity_detector module.

    Combines results from complexity_detector with additional heuristics
    for daemon-specific task patterns.
    """
    _ensure_imports()

    desc_lower = description.lower()
    word_count = len(description.split())

    # Check for epic indicators first (these override complexity_detector)
    epic_keywords = [
        "full rewrite",
        "new product",
        "major refactor",
        "end-to-end",
        "from scratch",
        "entire platform",
        "new platform",
        "complete system",
        "entire system",
        "comprehensive",
        "full-stack",
        "launch",
    ]
    if any(kw in desc_lower for kw in epic_keywords):
        # Check if multiple epic indicators or combined with scale words
        epic_count = sum(1 for kw in epic_keywords if kw in desc_lower)
        scale_words = ["entire", "complete", "full", "all", "whole"]
        has_scale = any(sw in desc_lower for sw in scale_words)
        if epic_count >= 2 or (epic_count >= 1 and has_scale):
            return "epic"

    # Try complexity_detector module
    if complexity_detector is not None:
        try:
            result = complexity_detector.detect_complexity(description)
            detected = result.get("level", "standard")

            # Elevate to complex if any epic keyword present but not enough for epic
            if detected in ("trivial", "standard"):
                if any(kw in desc_lower for kw in epic_keywords):
                    return "complex"

            return detected
        except Exception:
            pass

    # Fallback heuristics when complexity_detector unavailable

    # Complex indicators
    if any(
        kw in desc_lower
        for kw in [
            "refactor",
            "migrate",
            "redesign",
            "architecture",
            "integration",
            "multiple",
        ]
    ):
        return "complex"

    # Trivial indicators
    if any(kw in desc_lower for kw in ["typo", "fix", "tweak", "rename", "update"]):
        if word_count < 15:
            return "trivial"

    # Default to standard
    return "standard"


def _detect_action(description: str) -> SubtaskAction:
    """Detect the primary action type from a description."""
    desc_lower = description.lower()

    for action, keywords in ACTION_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return action

    return SubtaskAction.MODIFY


def _extract_files(description: str) -> list[str]:
    """
    Extract likely file paths from a task description.

    Uses regex patterns to find file paths and references.
    """
    files: list[str] = []

    # Match explicit file paths (e.g., src/auth/login.py)
    path_pattern = r"[a-zA-Z0-9_\-./]+\.(py|ts|tsx|js|jsx|go|rs|java|sql|yaml|json|md)"
    matches = re.findall(path_pattern, description)
    for match in matches:
        # Find the full path that ends with this extension
        full_pattern = rf"[\w/\-\.]+\.{match}"
        full_matches = re.findall(full_pattern, description)
        files.extend(full_matches)

    # Match directory references (e.g., "in the auth/ directory")
    dir_pattern = r"(?:in|under|within|at)\s+(?:the\s+)?([a-zA-Z0-9_\-/]+/)"
    dir_matches = re.findall(dir_pattern, description, re.IGNORECASE)
    files.extend(dir_matches)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_files: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    return unique_files


def _infer_files_from_context(description: str, action: SubtaskAction) -> list[str]:
    """
    Infer likely files based on task context and keywords.

    Returns generic file patterns when explicit paths aren't found.
    """
    desc_lower = description.lower()
    inferred: list[str] = []

    for category, patterns in FILE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, desc_lower, re.IGNORECASE):
                # Add a placeholder for this category
                if category == "api":
                    inferred.append("src/api/*.py")
                elif category == "database":
                    inferred.append("src/models/*.py")
                elif category == "frontend":
                    inferred.append("src/components/*.tsx")
                elif category == "test":
                    inferred.append("tests/*.py")
                elif category == "config":
                    inferred.append("config/*")
                elif category == "auth":
                    inferred.append("src/auth/*.py")
                break  # One placeholder per category

    return inferred


def _estimate_minutes(description: str, complexity: str, action: SubtaskAction) -> int:
    """
    Estimate time in minutes for a subtask.

    Factors in complexity, action type, and description analysis.
    """
    base = COMPLEXITY_BASE_MINUTES.get(complexity, 60)

    # Adjust for action type
    multiplier = 1.0
    if action == SubtaskAction.CREATE:
        multiplier = 1.2
    elif action == SubtaskAction.DELETE:
        multiplier = 0.5
    elif action == SubtaskAction.TEST:
        multiplier = 0.8
    elif action == SubtaskAction.REVIEW:
        multiplier = 0.6

    # Adjust for description length (proxy for scope)
    word_count = len(description.split())
    if word_count > 50:
        multiplier *= 1.3
    elif word_count < 10:
        multiplier *= 0.7

    return int(base * multiplier)


def _build_dependencies(subtasks: list[Subtask]) -> dict[str, list[str]]:
    """
    Build dependency graph for subtasks.

    Uses naming conventions and action types to infer dependencies:
    - design/setup tasks have no dependencies
    - core tasks depend on setup
    - integration/test tasks depend on core
    - review tasks depend on everything else
    """
    dependencies: dict[str, list[str]] = {}

    # Create lookup by subtask name pattern
    by_pattern: dict[str, str] = {}
    for st in subtasks:
        # Extract pattern from subtask ID (e.g., "task.setup-abc123" -> "setup")
        parts = st.id.split(".")
        if len(parts) > 1:
            name_part = parts[1].split("-")[0]
            by_pattern[name_part] = st.id

    # Define dependency rules
    dep_rules = {
        "core": ["setup"],
        "api": ["setup", "data"],
        "frontend": ["api", "core"],
        "integration": ["core", "api"],
        "test": ["core", "integration"],
        "review": ["test"],
        "security": ["core", "integration"],
        "docs": ["core", "test"],
    }

    for st in subtasks:
        parts = st.id.split(".")
        if len(parts) > 1:
            name_part = parts[1].split("-")[0]
            deps = []
            for dep_name in dep_rules.get(name_part, []):
                if dep_name in by_pattern:
                    deps.append(by_pattern[dep_name])
            if deps:
                dependencies[st.id] = deps
                st.depends_on = deps

    return dependencies


# -----------------------------------------------------------------------------
# LLM-Based Planning (P92: for complex/epic tasks)
# -----------------------------------------------------------------------------

# Configuration for LLM planning
LLM_PLANNING_ENABLED = True  # Master switch
LLM_PLANNING_COMPLEXITY_THRESHOLD = ["epic"]  # P97: Only epic uses LLM
LLM_PLANNING_TIMEOUT = 45  # P97: Reduced from 120s
LLM_PLANNING_MODEL = "sonnet"  # Cost-effective for planning


def _build_llm_planning_prompt(task: dict[str, Any], complexity: str) -> str:
    """Build a prompt for LLM-based task planning."""
    task_id = task.get("task_id", "unknown")
    description = task.get("description", task.get("title", ""))
    files = task.get("files", [])
    capabilities = task.get("required_capabilities", [])

    return f"""You are a software architect planning a {complexity} development task.

TASK ID: {task_id}
DESCRIPTION: {description}
COMPLEXITY: {complexity}
RELEVANT FILES: {", ".join(files) if files else "Not specified"}
REQUIRED CAPABILITIES: {", ".join(capabilities) if capabilities else "Not specified"}

Create a detailed execution plan with specific subtasks. Each subtask should be:
- Concrete and actionable (2-10 minutes of focused work)
- Have clear acceptance criteria
- Specify which files will be affected

OUTPUT FORMAT (JSON):
{{
  "subtasks": [
    {{
      "name": "short-name",
      "description": "What to do specifically",
      "action": "create|modify|test|review|delete",
      "files": ["path/to/file.py"],
      "acceptance": "How to verify completion",
      "estimated_minutes": 5,
      "depends_on": ["previous-subtask-name"]
    }}
  ]
}}

RULES:
1. First subtask should have no dependencies (depends_on: [])
2. Order subtasks logically - dependencies before dependents
3. Include a test subtask if the task involves code changes
4. Include a review subtask for complex/epic tasks
5. Be specific about files - don't use wildcards
6. Each subtask should be independently verifiable

Output ONLY the JSON, no markdown code blocks or explanation."""


def _plan_task_with_llm(task: dict[str, Any], complexity: str) -> TaskPlan | None:
    """
    P92: Use LLM (Claude) to generate an intelligent plan for complex/epic tasks.

    This provides task-specific planning rather than generic templates.
    Falls back to template planning if LLM planning fails.

    Args:
        task: Task dictionary
        complexity: Detected complexity level

    Returns:
        TaskPlan if successful, None if LLM planning failed (caller should fallback)
    """
    import os
    import subprocess

    task_id = task.get("task_id", "unknown")
    print(
        f"[P92] LLM planning for {complexity} task: {task_id}",
        file=sys.stderr,
    )

    prompt = _build_llm_planning_prompt(task, complexity)

    # Build Claude command
    # P79: Use --setting-sources user to skip CLAUDE.md overload
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "text",
        "--model",
        LLM_PLANNING_MODEL,
        "--setting-sources",
        "user",
    ]

    # Clean environment (P54/P57/P71 fixes)
    child_env = os.environ.copy()
    problematic_prefixes = ("UV_", "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_")
    for key in list(child_env.keys()):
        if key.startswith(problematic_prefixes):
            del child_env[key]

    # Clean PATH of UV-managed environments
    original_path = child_env.get("PATH", "")
    clean_path_parts = [
        p
        for p in original_path.split(":")
        if not p.startswith(os.path.expanduser("~/.cache/uv/environments"))
        and "virtualenv" not in p.lower()
    ]
    child_env["PATH"] = (
        ":".join(clean_path_parts) if clean_path_parts else "/usr/bin:/bin"
    )

    # Set safe terminal defaults
    child_env["TERM"] = "xterm-256color"
    child_env["LANG"] = "en_US.UTF-8"
    child_env = {k: v for k, v in child_env.items() if v}

    # 2026-07-06 fork-bomb guard: LLM planning launches a real claude process.
    assert_spawn_allowed("task_planner._plan_task_with_llm", subprocess.run)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=LLM_PLANNING_TIMEOUT,
            env=child_env,
            cwd=str(Path(__file__).resolve().parent.parent.parent.parent),
        )

        if result.returncode != 0:
            return None  # Fallback to template

        output = result.stdout.strip()
        if not output:
            return None

        # Parse JSON response
        # Handle potential markdown code blocks
        if output.startswith("```"):
            lines = output.split("\n")
            output = "\n".join(lines[1:-1])

        plan_data = json.loads(output)
        subtasks_data = plan_data.get("subtasks", [])

        if not subtasks_data:
            return None

        # Build TaskPlan from LLM response
        plan = TaskPlan(task_id=task_id, complexity=complexity)

        for st_data in subtasks_data:
            name = st_data.get("name", "subtask")
            subtask_id = _generate_subtask_id(task_id, name)

            # Map action string to enum
            action_str = st_data.get("action", "modify").lower()
            action_map = {
                "create": SubtaskAction.CREATE,
                "modify": SubtaskAction.MODIFY,
                "delete": SubtaskAction.DELETE,
                "test": SubtaskAction.TEST,
                "review": SubtaskAction.REVIEW,
            }
            action = action_map.get(action_str, SubtaskAction.MODIFY)

            subtask = Subtask(
                id=subtask_id,
                description=st_data.get("description", name),
                files=st_data.get("files", []),
                action=action,
                acceptance=st_data.get("acceptance", "Verify completion"),
                estimated_minutes=st_data.get("estimated_minutes", 10),
                depends_on=[],  # Will be resolved below
            )
            plan.subtasks.append(subtask)

        # Resolve dependencies (name -> id mapping)
        name_to_id = {}
        for st_data, subtask in zip(subtasks_data, plan.subtasks):
            name = st_data.get("name", "")
            name_to_id[name] = subtask.id

        for st_data, subtask in zip(subtasks_data, plan.subtasks):
            depends_on_names = st_data.get("depends_on", [])
            subtask.depends_on = [
                name_to_id[dep] for dep in depends_on_names if dep in name_to_id
            ]

        # Build dependencies dict
        plan.dependencies = {
            st.id: st.depends_on for st in plan.subtasks if st.depends_on
        }

        # Calculate total time
        plan.total_estimated_minutes = sum(st.estimated_minutes for st in plan.subtasks)

        # Assign waves
        waves = decompose_to_waves(plan)
        for wave in waves:
            for subtask_id in wave.subtask_ids:
                plan.wave_assignment[subtask_id] = wave.number

        print(
            f"[P92] LLM planning SUCCESS: {len(plan.subtasks)} subtasks, "
            f"{len(waves)} waves, {plan.total_estimated_minutes}min",
            file=sys.stderr,
        )
        return plan

    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, Exception) as e:
        print(
            f"[P92] LLM planning FAILED ({type(e).__name__}), falling back to template",
            file=sys.stderr,
        )
        return None  # Fallback to template


# -----------------------------------------------------------------------------
# Main Planning Functions
# -----------------------------------------------------------------------------


def plan_task(task: dict[str, Any]) -> TaskPlan:
    """
    Analyze a task and create a detailed execution plan.

    P92: Uses complexity-based routing:
    - trivial/standard: Fast template-based planning
    - complex/epic: Intelligent LLM-based planning (with template fallback)

    Args:
        task: Task dictionary with at least:
            - task_id: Unique task identifier
            - description: Task description (or title)
            - Optional: complexity, required_capabilities, files

    Returns:
        TaskPlan with subtasks, dependencies, and wave assignments.
    """
    _ensure_imports()

    task_id = task.get("task_id", "unknown")
    description = task.get("description", task.get("title", ""))
    explicit_complexity = task.get("complexity", task.get("estimated_complexity"))

    # Detect complexity
    if explicit_complexity:
        complexity = explicit_complexity
    else:
        complexity = _detect_complexity(description)

    # P92: Route complex/epic tasks to LLM planning
    # WS-074: Skip LLM planning during pytest (prevents real Claude CLI spawns)
    is_pytest = os.environ.get("PYTEST_CURRENT_TEST") is not None
    if (
        LLM_PLANNING_ENABLED
        and complexity in LLM_PLANNING_COMPLEXITY_THRESHOLD
        and not is_pytest
    ):
        llm_plan = _plan_task_with_llm(task, complexity)
        if llm_plan is not None:
            return llm_plan
        # LLM planning failed — fall through to template planning

    # Template-based planning (fast, deterministic)
    plan = TaskPlan(task_id=task_id, complexity=complexity)

    # Get subtask templates for this complexity
    templates = SUBTASK_TEMPLATES.get(complexity, SUBTASK_TEMPLATES["standard"])

    # Create subtasks from templates
    for template_name, template_desc, template_action in templates:
        subtask_id = _generate_subtask_id(task_id, template_name)

        # Customize description with task context
        full_desc = f"{template_desc}: {description[:100]}"

        # Extract or infer files
        files = _extract_files(description)
        if not files:
            files = _infer_files_from_context(description, template_action)

        # Estimate time
        minutes = _estimate_minutes(description, complexity, template_action)

        # Build acceptance criteria
        acceptance = f"Verify {template_name} is complete and working correctly"
        if template_action == SubtaskAction.TEST:
            acceptance = "All tests pass, coverage is adequate"
        elif template_action == SubtaskAction.REVIEW:
            acceptance = "Code reviewed, no issues found"
        elif template_action == SubtaskAction.CREATE:
            acceptance = f"New {template_name} artifacts created and validated"

        subtask = Subtask(
            id=subtask_id,
            description=full_desc,
            files=files,
            action=template_action,
            acceptance=acceptance,
            estimated_minutes=minutes,
        )
        plan.subtasks.append(subtask)

    # Build dependencies
    plan.dependencies = _build_dependencies(plan.subtasks)

    # Calculate total time
    plan.total_estimated_minutes = sum(st.estimated_minutes for st in plan.subtasks)

    # Assign waves
    waves = decompose_to_waves(plan)
    for wave in waves:
        for subtask_id in wave.subtask_ids:
            plan.wave_assignment[subtask_id] = wave.number

    return plan


def decompose_to_waves(plan: TaskPlan) -> list[Wave]:
    """
    Organize subtasks into parallel execution waves.

    Subtasks with no dependencies go in Wave 1.
    Subtasks whose dependencies are all in earlier waves go in the next wave.

    Args:
        plan: TaskPlan with subtasks and dependencies

    Returns:
        List of Wave objects, each containing subtask IDs that can run in parallel.
    """
    if not plan.subtasks:
        return []

    waves: list[Wave] = []
    assigned: set[str] = set()
    subtask_ids = {st.id for st in plan.subtasks}

    wave_num = 1
    while len(assigned) < len(plan.subtasks):
        wave_subtasks: list[str] = []

        for st in plan.subtasks:
            if st.id in assigned:
                continue

            # Get dependencies that are in this plan
            deps = [d for d in st.depends_on if d in subtask_ids]

            # Check if all dependencies are assigned
            if all(d in assigned for d in deps):
                wave_subtasks.append(st.id)

        if not wave_subtasks:
            # Circular dependency or error - add remaining
            wave_subtasks = [st.id for st in plan.subtasks if st.id not in assigned]

        wave = Wave(number=wave_num, subtask_ids=wave_subtasks)
        waves.append(wave)
        assigned.update(wave_subtasks)
        wave_num += 1

    return waves


def get_subtask_by_id(plan: TaskPlan, subtask_id: str) -> Subtask | None:
    """Find a subtask by its ID within a plan."""
    for st in plan.subtasks:
        if st.id == subtask_id:
            return st
    return None


def get_ready_subtasks(plan: TaskPlan, completed: set[str]) -> list[Subtask]:
    """
    Get subtasks that are ready to execute.

    A subtask is ready if all its dependencies have been completed.

    Args:
        plan: The task plan
        completed: Set of subtask IDs that have been completed

    Returns:
        List of subtasks that are ready to execute.
    """
    ready: list[Subtask] = []

    for st in plan.subtasks:
        if st.id in completed:
            continue

        # Check if all dependencies are completed
        if all(d in completed for d in st.depends_on):
            ready.append(st)

    return ready


def estimate_remaining_time(plan: TaskPlan, completed: set[str]) -> int:
    """
    Estimate remaining time in minutes.

    Args:
        plan: The task plan
        completed: Set of subtask IDs that have been completed

    Returns:
        Estimated minutes remaining.
    """
    remaining = 0
    for st in plan.subtasks:
        if st.id not in completed:
            remaining += st.estimated_minutes
    return remaining


# -----------------------------------------------------------------------------
# Self-Healing Plan Revision
# -----------------------------------------------------------------------------


def _create_fix_subtask(
    plan: TaskPlan,
    failure: ExecutionFailure,
    failed_subtask: Subtask | None,
) -> Subtask:
    """
    Create a fix subtask to address the failure.

    Args:
        plan: The original plan
        failure: The execution failure details
        failed_subtask: The subtask that failed (if found)

    Returns:
        A new Subtask designed to fix the error
    """
    error_type = failure.error_type

    # Generate fix subtask based on error type
    if error_type == "test":
        description = f"Fix failing tests: {failure.error_message[:100]}"
        action = SubtaskAction.TEST
        acceptance = "All tests pass after fix"
    elif error_type == "lint":
        description = f"Fix linting errors: {failure.error_message[:100]}"
        action = SubtaskAction.MODIFY
        acceptance = "Code passes all linting checks"
    elif error_type == "type":
        description = f"Fix type errors: {failure.error_message[:100]}"
        action = SubtaskAction.MODIFY
        acceptance = "Type checking passes"
    else:
        description = (
            f"Fix error in {failure.subtask_id}: {failure.error_message[:100]}"
        )
        action = SubtaskAction.MODIFY
        acceptance = "Error resolved and task completes successfully"

    # Inherit files from failed subtask or use modified files
    files = failure.files_modified or (failed_subtask.files if failed_subtask else [])

    return Subtask(
        id=_generate_subtask_id(plan.task_id, f"fix-{error_type}"),
        description=description,
        files=files,
        action=action,
        acceptance=acceptance,
        estimated_minutes=15,  # Fix subtasks are typically quick
        depends_on=[],  # No dependencies, needs to run immediately
    )


def _create_prerequisite_subtask(
    plan: TaskPlan,
    failure: ExecutionFailure,
    failed_subtask: Subtask | None,
) -> Subtask:
    """
    Create a prerequisite subtask to address missing dependencies.

    Args:
        plan: The original plan
        failure: The execution failure details
        failed_subtask: The subtask that failed (if found)

    Returns:
        A new Subtask to add the missing prerequisite
    """
    error_type = failure.error_type

    if error_type == "import":
        description = f"Install missing dependencies: {failure.error_message[:100]}"
        action = SubtaskAction.CREATE
        acceptance = "All required imports are available"
    elif error_type == "file":
        description = f"Create missing files: {failure.error_message[:100]}"
        action = SubtaskAction.CREATE
        acceptance = "Required files exist"
    elif error_type == "permission":
        description = f"Fix file permissions: {failure.error_message[:100]}"
        action = SubtaskAction.MODIFY
        acceptance = "File permissions allow required operations"
    elif error_type == "dependency":
        description = f"Resolve dependency conflicts: {failure.error_message[:100]}"
        action = SubtaskAction.MODIFY
        acceptance = "All dependencies are compatible"
    else:
        description = (
            f"Add prerequisite for {failure.subtask_id}: {failure.error_message[:100]}"
        )
        action = SubtaskAction.CREATE
        acceptance = "Prerequisite completed"

    return Subtask(
        id=_generate_subtask_id(plan.task_id, f"prereq-{error_type}"),
        description=description,
        files=failure.files_modified or [],
        action=action,
        acceptance=acceptance,
        estimated_minutes=20,
        depends_on=[],
    )


def _split_subtask(
    plan: TaskPlan,
    failed_subtask: Subtask,
    failure: ExecutionFailure,
) -> list[Subtask]:
    """
    Split a subtask into smaller subtasks (for timeout errors).

    Args:
        plan: The original plan
        failed_subtask: The subtask that timed out
        failure: The execution failure details

    Returns:
        List of smaller subtasks to replace the original
    """
    # Split based on files if multiple files are involved
    files = failed_subtask.files or failure.files_modified

    if len(files) > 1:
        # Create one subtask per file
        subtasks = []
        for i, file_path in enumerate(files):
            subtask = Subtask(
                id=_generate_subtask_id(plan.task_id, f"split-{i + 1}"),
                description=f"Part {i + 1}: {failed_subtask.description[:80]} - {file_path}",
                files=[file_path],
                action=failed_subtask.action,
                acceptance=f"Part {i + 1} complete: {file_path}",
                estimated_minutes=max(
                    10, failed_subtask.estimated_minutes // len(files)
                ),
                depends_on=failed_subtask.depends_on if i == 0 else [],
            )
            subtasks.append(subtask)

        # Chain dependencies
        for i in range(1, len(subtasks)):
            subtasks[i].depends_on = [subtasks[i - 1].id]

        return subtasks
    else:
        # Can't split by file, create a simpler version
        return [
            Subtask(
                id=_generate_subtask_id(plan.task_id, "split-prep"),
                description=f"Prepare: {failed_subtask.description[:80]}",
                files=files,
                action=SubtaskAction.REVIEW,
                acceptance="Preparation complete",
                estimated_minutes=10,
                depends_on=failed_subtask.depends_on,
            ),
            Subtask(
                id=_generate_subtask_id(plan.task_id, "split-exec"),
                description=f"Execute: {failed_subtask.description[:80]}",
                files=files,
                action=failed_subtask.action,
                acceptance=failed_subtask.acceptance,
                estimated_minutes=max(10, failed_subtask.estimated_minutes - 10),
                depends_on=[],  # Will be set after first subtask is added
            ),
        ]


def revise_plan(
    plan: TaskPlan,
    failure: ExecutionFailure,
    revision_count: int = 0,
) -> RevisionResult:
    """
    Revise a plan based on execution failure.

    Analyzes the failure and applies the appropriate revision strategy:
    - Test failure: add test fix subtask
    - Lint failure: add lint fix subtask
    - Import error: add dependency subtask
    - Timeout: split into smaller subtasks

    Args:
        plan: The original TaskPlan that failed
        failure: ExecutionFailure with details about what went wrong
        revision_count: Number of revisions already attempted (for escalation)

    Returns:
        RevisionResult with the revised plan or escalation status
    """
    # Check if we've exceeded max revisions
    if revision_count >= MAX_REVISIONS:
        return RevisionResult(
            success=False,
            strategy_used=RevisionStrategy.ESCALATE,
            revised_plan=None,
            revision_count=revision_count,
            escalated=True,
            message=f"Max revisions ({MAX_REVISIONS}) exceeded, escalating to human review",
            fixes_applied=[],
        )

    # Find the failed subtask
    failed_subtask = get_subtask_by_id(plan, failure.subtask_id)

    # Determine revision strategy based on error type
    strategy = ERROR_TYPE_TO_STRATEGY.get(failure.error_type, RevisionStrategy.ESCALATE)

    # Apply strategy
    fixes_applied: list[str] = []
    new_subtasks: list[Subtask] = []

    if strategy == RevisionStrategy.ADD_FIX_SUBTASK:
        # Create a fix subtask
        fix_subtask = _create_fix_subtask(plan, failure, failed_subtask)
        new_subtasks.append(fix_subtask)
        fixes_applied.append(f"Added fix subtask: {fix_subtask.description[:50]}")

    elif strategy == RevisionStrategy.ADD_PREREQUISITE:
        # Create a prerequisite subtask
        prereq_subtask = _create_prerequisite_subtask(plan, failure, failed_subtask)
        new_subtasks.append(prereq_subtask)
        fixes_applied.append(f"Added prerequisite: {prereq_subtask.description[:50]}")

    elif strategy == RevisionStrategy.SPLIT_SUBTASK:
        if failed_subtask:
            # Split the failed subtask
            split_subtasks = _split_subtask(plan, failed_subtask, failure)
            new_subtasks.extend(split_subtasks)
            fixes_applied.append(
                f"Split subtask into {len(split_subtasks)} smaller tasks"
            )
        else:
            # Can't split without the original subtask
            strategy = RevisionStrategy.ESCALATE

    elif strategy == RevisionStrategy.ESCALATE:
        return RevisionResult(
            success=False,
            strategy_used=RevisionStrategy.ESCALATE,
            revised_plan=None,
            revision_count=revision_count + 1,
            escalated=True,
            message=f"Cannot auto-fix error type '{failure.error_type}', escalating",
            fixes_applied=[],
        )

    # Create revised plan
    revised_plan = TaskPlan(
        task_id=plan.task_id,
        complexity=plan.complexity,
        subtasks=list(plan.subtasks),  # Copy existing subtasks
        dependencies=dict(plan.dependencies),
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Add new subtasks
    for new_subtask in new_subtasks:
        revised_plan.subtasks.append(new_subtask)

    # If we added a fix/prereq subtask, make the failed subtask depend on it
    if strategy in (
        RevisionStrategy.ADD_FIX_SUBTASK,
        RevisionStrategy.ADD_PREREQUISITE,
    ):
        if failed_subtask and new_subtasks:
            # Find the failed subtask in the revised plan and update its dependencies
            for st in revised_plan.subtasks:
                if st.id == failure.subtask_id:
                    # Add the new subtask as a dependency
                    st.depends_on = list(st.depends_on) + [new_subtasks[0].id]
                    revised_plan.dependencies[st.id] = st.depends_on
                    break

    # If we split the subtask, remove the original and update dependencies
    if strategy == RevisionStrategy.SPLIT_SUBTASK and failed_subtask:
        # Remove the failed subtask
        revised_plan.subtasks = [
            st for st in revised_plan.subtasks if st.id != failure.subtask_id
        ]
        # Remove from dependencies
        revised_plan.dependencies.pop(failure.subtask_id, None)

        # Update any subtasks that depended on the failed one
        last_split_id = new_subtasks[-1].id if new_subtasks else None
        if last_split_id:
            for st in revised_plan.subtasks:
                if failure.subtask_id in st.depends_on:
                    st.depends_on = [
                        last_split_id if d == failure.subtask_id else d
                        for d in st.depends_on
                    ]
                    revised_plan.dependencies[st.id] = st.depends_on

    # Rebuild dependencies and wave assignments
    revised_plan.dependencies = _build_dependencies(revised_plan.subtasks)
    revised_plan.total_estimated_minutes = sum(
        st.estimated_minutes for st in revised_plan.subtasks
    )

    waves = decompose_to_waves(revised_plan)
    revised_plan.wave_assignment = {}
    for wave in waves:
        for subtask_id in wave.subtask_ids:
            revised_plan.wave_assignment[subtask_id] = wave.number

    return RevisionResult(
        success=True,
        strategy_used=strategy,
        revised_plan=revised_plan,
        revision_count=revision_count + 1,
        escalated=False,
        message=f"Plan revised using {strategy.value} strategy",
        fixes_applied=fixes_applied,
    )


def revise_plan_from_replan_context(
    plan: TaskPlan,
    replan_context: dict[str, Any],
) -> RevisionResult:
    """
    Revise a plan using a ReplanContext from plan_checker.py.

    This provides integration between the plan_checker's self-healing
    context and the task_planner's revision capabilities.

    Args:
        plan: The original TaskPlan that failed
        replan_context: Dictionary from ReplanContext.to_dict()

    Returns:
        RevisionResult with the revised plan or escalation status
    """
    # Extract failure information from ReplanContext
    failure = ExecutionFailure(
        subtask_id=replan_context.get("failed_subtask_id", ""),
        error_type=_detect_error_type(replan_context.get("failure_output", "")),
        error_message=replan_context.get("failure_reason", "Unknown failure"),
        files_modified=[],
        retry_count=replan_context.get("retry_count", 0),
    )

    # Use retry_count as revision_count
    revision_count = replan_context.get("retry_count", 0)

    return revise_plan(plan, failure, revision_count)


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Task Planner — Lightweight planning for daemon-invoked tasks.

Commands:
    plan        Create a plan for a task
    waves       Show wave decomposition for a plan
    subtask     Get details of a specific subtask
    ready       Show subtasks ready to execute
    revise      Revise a plan based on execution failure (self-healing)
    help        Show this help message

plan options:
    --task-id ID           Task ID (loads from work queue if available)
    --description TEXT     Task description (required if no task-id)
    --complexity LEVEL     Override complexity (trivial/standard/complex/epic)
    --pretty               Pretty-print JSON output

waves options:
    --task-id ID           Task ID to decompose
    --description TEXT     Task description

subtask options:
    --plan-json JSON       JSON plan to search in
    --subtask-id ID        Subtask ID to find

ready options:
    --plan-json JSON       JSON plan
    --completed IDS        Comma-separated completed subtask IDs

revise options:
    --plan-json JSON       JSON plan to revise
    --failure-json JSON    ExecutionFailure as JSON
    --error-output TEXT    Raw error output (auto-detects error type)
    --subtask-id ID        ID of the failed subtask
    --revision-count N     Number of prior revisions (default: 0)

Examples:
    # Plan a task by description
    uv run task_planner.py plan --description "Build user authentication"

    # Plan with explicit complexity
    uv run task_planner.py plan --description "Fix typo in readme" --complexity trivial

    # Show waves for a plan
    uv run task_planner.py waves --description "Refactor auth module"

    # Get ready subtasks
    uv run task_planner.py ready --plan-json '{"subtasks":[...]}' --completed "id1,id2"

    # Revise a plan after test failure (self-healing)
    uv run task_planner.py revise --plan-json '{"task_id":"T1",...}' \\
        --error-output "AssertionError: expected 5 got 3" --subtask-id "T1.test-abc123"

    # Revise with explicit failure JSON
    uv run task_planner.py revise --plan-json '...' \\
        --failure-json '{"subtask_id":"T1.core","error_type":"lint","error_message":"..."}'
"""
    print(help_text)


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


def main() -> int:
    """Main entry point for CLI usage."""
    if len(sys.argv) < 2:
        print_help()
        return 0

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        return 0

    try:
        if command == "plan":
            description = args.get("description", "")
            task_id = args.get("task_id", "cli-task")
            complexity = args.get("complexity")
            pretty = args.get("pretty", False)

            if not description and task_id != "cli-task":
                # Try to load from work queue
                _ensure_imports()
                if work_allocator is not None:
                    task_result = work_allocator.get_task(task_id)
                    if task_result.get("success"):
                        task = task_result["task"]
                        description = task.get("description", task.get("title", ""))

            if not description:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "No description provided. Use --description or valid --task-id",
                        }
                    )
                )
                return 1

            task = {"task_id": task_id, "description": description}
            if complexity:
                task["complexity"] = complexity

            plan = plan_task(task)

            if pretty:
                print(plan.to_json(pretty=True))
            else:
                print(plan.to_json())

        elif command == "waves":
            description = args.get("description", "")
            task_id = args.get("task_id", "cli-task")

            if not description:
                print(
                    json.dumps({"success": False, "error": "No description provided."})
                )
                return 1

            task = {"task_id": task_id, "description": description}
            plan = plan_task(task)
            waves = decompose_to_waves(plan)

            output = {
                "task_id": task_id,
                "complexity": plan.complexity,
                "waves": [w.to_dict() for w in waves],
                "wave_count": len(waves),
            }
            print(json.dumps(output, indent=2))

        elif command == "subtask":
            plan_json = args.get("plan_json", "")
            subtask_id = args.get("subtask_id", "")

            if not plan_json or not subtask_id:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "Both --plan-json and --subtask-id required",
                        }
                    )
                )
                return 1

            plan_data = json.loads(plan_json)

            # Reconstruct minimal plan
            subtasks = [
                Subtask(
                    id=st["id"],
                    description=st["description"],
                    files=st.get("files", []),
                    action=SubtaskAction(st.get("action", "modify")),
                    acceptance=st.get("acceptance", ""),
                    estimated_minutes=st.get("estimated_minutes", 30),
                    depends_on=st.get("depends_on", []),
                )
                for st in plan_data.get("subtasks", [])
            ]

            plan = TaskPlan(
                task_id=plan_data.get("task_id", "unknown"),
                complexity=plan_data.get("complexity", "standard"),
                subtasks=subtasks,
            )

            subtask = get_subtask_by_id(plan, subtask_id)
            if subtask:
                print(json.dumps(subtask.to_dict(), indent=2))
            else:
                print(
                    json.dumps(
                        {"success": False, "error": f"Subtask not found: {subtask_id}"}
                    )
                )
                return 1

        elif command == "ready":
            plan_json = args.get("plan_json", "")
            completed_str = args.get("completed", "")

            if not plan_json:
                print(json.dumps({"success": False, "error": "--plan-json required"}))
                return 1

            plan_data = json.loads(plan_json)
            completed = set(completed_str.split(",")) if completed_str else set()

            # Reconstruct minimal plan
            subtasks = [
                Subtask(
                    id=st["id"],
                    description=st["description"],
                    files=st.get("files", []),
                    action=SubtaskAction(st.get("action", "modify")),
                    acceptance=st.get("acceptance", ""),
                    estimated_minutes=st.get("estimated_minutes", 30),
                    depends_on=st.get("depends_on", []),
                )
                for st in plan_data.get("subtasks", [])
            ]

            plan = TaskPlan(
                task_id=plan_data.get("task_id", "unknown"),
                complexity=plan_data.get("complexity", "standard"),
                subtasks=subtasks,
            )

            ready = get_ready_subtasks(plan, completed)
            remaining = estimate_remaining_time(plan, completed)

            output = {
                "ready_subtasks": [st.to_dict() for st in ready],
                "ready_count": len(ready),
                "remaining_minutes": remaining,
                "completed_count": len(completed),
            }
            print(json.dumps(output, indent=2))

        elif command == "revise":
            plan_json = args.get("plan_json", "")
            failure_json = args.get("failure_json", "")
            error_output = args.get("error_output", "")
            subtask_id = args.get("subtask_id", "")
            revision_count = int(args.get("revision_count", "0"))

            if not plan_json:
                print(json.dumps({"success": False, "error": "--plan-json required"}))
                return 1

            plan_data = json.loads(plan_json)

            # Reconstruct the plan
            subtasks = [
                Subtask(
                    id=st["id"],
                    description=st["description"],
                    files=st.get("files", []),
                    action=SubtaskAction(st.get("action", "modify")),
                    acceptance=st.get("acceptance", ""),
                    estimated_minutes=st.get("estimated_minutes", 30),
                    depends_on=st.get("depends_on", []),
                )
                for st in plan_data.get("subtasks", [])
            ]

            plan = TaskPlan(
                task_id=plan_data.get("task_id", "unknown"),
                complexity=plan_data.get("complexity", "standard"),
                subtasks=subtasks,
                dependencies=plan_data.get("dependencies", {}),
                wave_assignment=plan_data.get("wave_assignment", {}),
            )

            # Create ExecutionFailure from args
            if failure_json:
                failure_data = json.loads(failure_json)
                failure = ExecutionFailure.from_dict(failure_data)
            elif error_output or subtask_id:
                failure = ExecutionFailure.from_error_output(
                    subtask_id=subtask_id,
                    error_output=error_output,
                )
            else:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "Provide --failure-json OR (--error-output and --subtask-id)",
                        }
                    )
                )
                return 1

            # Revise the plan
            result = revise_plan(plan, failure, revision_count)

            output = {
                "success": result.success,
                "strategy": result.strategy_used.value,
                "escalated": result.escalated,
                "revision_count": result.revision_count,
                "message": result.message,
                "fixes_applied": result.fixes_applied,
            }

            if result.revised_plan:
                output["revised_plan"] = result.revised_plan.to_dict()

            print(json.dumps(output, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            return 1

    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
        return 1
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
