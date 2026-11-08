#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Plan Checker — lightweight plan validation for daemon autonomous execution.

Validates TaskPlan objects produced by task_planner.py before execution.
Implements the BMAD checker pattern with a 0-25 scoring scale:

Scoring Dimensions (5 points each):
    1. Completeness — Do subtasks cover the task requirements?
    2. Correctness — Are dependencies properly ordered?
    3. Feasibility — Can subtasks be executed with available resources?
    4. Specificity — Are subtasks concrete and actionable?
    5. Safety — No dangerous operations or missing validations?

Thresholds:
    - score >= 18: PASS — proceed with execution
    - score 12-17: REVISE — attempt one auto-revision cycle
    - score < 12: ESCALATE — requires human review

Integration with self-healing:
    On execution failure, pass failure context back to planner for
    re-planning with learned constraints.

Usage:
    # Validate a plan
    python plan_checker.py validate --plan-file plan.json

    # Check a plan with verbose output
    python plan_checker.py validate --plan-file plan.json --verbose

    # Validate from stdin
    echo '{"task_id": "...", ...}' | python plan_checker.py validate --stdin

    # Get scoring thresholds
    python plan_checker.py thresholds
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

# Lazy imports for sibling modules
_company_resolver = None
_escalation = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global _company_resolver, _escalation
    if _company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import escalation as esc

        _company_resolver = cr
        _escalation = esc
    except ImportError:
        try:
            import company_resolver as cr  # type: ignore[no-redef]
            import escalation as esc  # type: ignore[no-redef]

            _company_resolver = cr
            _escalation = esc
        except ImportError:
            # Minimal fallback for standalone testing
            _company_resolver = None
            _escalation = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Scoring thresholds (matching BMAD checker pattern)
PASS_THRESHOLD = 18  # score >= 18: PASS
REVISE_THRESHOLD = 12  # score 12-17: REVISE, score < 12: ESCALATE
MAX_SCORE = 25  # 5 dimensions x 5 points each

# Dangerous operations that trigger safety deductions
DANGEROUS_PATTERNS = [
    r"rm\s+-rf",
    r"rm\s+.*\*",
    r"sudo\s+",
    r"chmod\s+777",
    r">\s*/dev/",
    r"curl.*\|\s*bash",
    r"wget.*\|\s*sh",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM\s+\*",
    r"TRUNCATE\s+TABLE",
]

# File action types that are valid
VALID_FILE_ACTIONS = {"create", "modify", "delete", "rename", "test", "verify"}

# Notification state file for deduplication
NOTIFICATION_STATE_FILE = "plan_checker_notifications.json"


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------


class CheckerVerdict(str, Enum):
    """Verdict from plan validation."""

    PASS = "pass"  # Score >= 18, proceed with execution
    REVISE = "revise"  # Score 12-17, attempt one auto-revision
    ESCALATE = "escalate"  # Score < 12, requires human review


class ScoreDimension(str, Enum):
    """Scoring dimensions for plan validation."""

    COMPLETENESS = "completeness"  # Do subtasks cover requirements?
    CORRECTNESS = "correctness"  # Are dependencies properly ordered?
    FEASIBILITY = "feasibility"  # Can subtasks be executed?
    SPECIFICITY = "specificity"  # Are subtasks concrete?
    SAFETY = "safety"  # No dangerous operations?


# -----------------------------------------------------------------------------
# Protocol for TaskPlan (interface, not tight coupling)
# -----------------------------------------------------------------------------


class SubtaskProtocol(Protocol):
    """Protocol defining expected subtask structure."""

    id: str
    name: str
    description: str
    file: str | None
    action: str
    depends_on: list[str]
    acceptance: str


class TaskPlanProtocol(Protocol):
    """Protocol defining expected TaskPlan structure."""

    task_id: str
    original_task: dict
    subtasks: list[Any]  # List of SubtaskProtocol
    created_at: str
    planner_version: str


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class DimensionScore:
    """Score for a single dimension."""

    dimension: ScoreDimension
    score: int  # 0-5
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "dimension": self.dimension.value,
            "score": self.score,
            "issues": self.issues,
            "suggestions": self.suggestions,
        }


@dataclass
class CheckerResult:
    """Result of plan validation.

    Attributes:
        passed: Whether the plan passed validation (score >= PASS_THRESHOLD)
        score: Total score (0-25)
        verdict: PASS, REVISE, or ESCALATE
        dimension_scores: Individual scores per dimension
        issues: Critical issues that must be fixed
        suggestions: Recommendations for improvement
        checked_at: ISO timestamp when check was performed
        plan_id: ID of the plan that was checked
        failure_context: If this check follows an execution failure,
            contains the failure details for re-planning
    """

    passed: bool
    score: int  # 0-25
    verdict: CheckerVerdict
    dimension_scores: list[DimensionScore] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    checked_at: str = ""
    plan_id: str = ""
    failure_context: dict | None = None

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "passed": self.passed,
            "score": self.score,
            "max_score": MAX_SCORE,
            "verdict": self.verdict.value,
            "dimension_scores": [d.to_dict() for d in self.dimension_scores],
            "issues": self.issues,
            "suggestions": self.suggestions,
            "checked_at": self.checked_at,
            "plan_id": self.plan_id,
            "thresholds": {
                "pass": PASS_THRESHOLD,
                "revise": REVISE_THRESHOLD,
            },
            "failure_context": self.failure_context,
        }


@dataclass
class ReplanContext:
    """Context for re-planning after execution failure.

    Passed to task_planner when self-healing triggers re-planning.

    Attributes:
        original_plan_id: ID of the failed plan
        failed_subtask_id: ID of the subtask that failed
        failure_reason: Description of what went wrong
        failure_output: stdout/stderr from failed execution
        attempted_fixes: List of fixes already attempted
        constraints: Learned constraints to apply in new plan
        retry_count: Number of retries so far
    """

    original_plan_id: str
    failed_subtask_id: str | None
    failure_reason: str
    failure_output: str = ""
    attempted_fixes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    retry_count: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReplanContext":
        """Create from dictionary."""
        return cls(**data)


# -----------------------------------------------------------------------------
# Scoring Functions
# -----------------------------------------------------------------------------


def score_completeness(plan: dict) -> DimensionScore:
    """
    Score completeness: Do subtasks cover the task requirements?

    Checks:
        - Original task has clear requirements
        - Every requirement has a corresponding subtask
        - No orphan subtasks unrelated to the goal
        - Testing/verification included where appropriate

    Args:
        plan: Plan dictionary with 'original_task' and 'subtasks'

    Returns:
        DimensionScore with 0-5 score and issues/suggestions
    """
    issues = []
    suggestions = []
    score = 5  # Start at max, deduct for problems

    subtasks = plan.get("subtasks", [])
    original_task = plan.get("original_task", {})
    task_title = original_task.get("title", "")
    task_description = original_task.get("description", "")

    # Check: Plan has subtasks
    if not subtasks:
        issues.append("Plan has no subtasks")
        return DimensionScore(
            dimension=ScoreDimension.COMPLETENESS,
            score=0,
            issues=issues,
            suggestions=["Add subtasks to implement the task"],
        )

    # Check: Original task has content
    if not task_title and not task_description:
        issues.append("Original task lacks title and description")
        score -= 2

    # Check: Subtasks have descriptions
    undescribed = [s for s in subtasks if not s.get("description")]
    if undescribed:
        issues.append(
            f"{len(undescribed)} subtask(s) lack descriptions: "
            f"{[s.get('id', 'unknown') for s in undescribed[:3]]}"
        )
        score -= 1

    # Check: Testing/verification subtask exists for non-trivial plans
    if len(subtasks) > 2:
        has_verification = any(
            s.get("action") in ("test", "verify")
            or "test" in s.get("name", "").lower()
            or "verify" in s.get("name", "").lower()
            for s in subtasks
        )
        if not has_verification:
            suggestions.append(
                "Consider adding a verification/testing subtask for non-trivial plans"
            )
            score -= 1

    # Check: Acceptance criteria exist
    without_acceptance = [
        s for s in subtasks if not s.get("acceptance") and s.get("action") != "verify"
    ]
    if without_acceptance:
        if len(without_acceptance) == len(subtasks):
            # All subtasks lack acceptance criteria - major issue
            issues.append(
                f"All {len(without_acceptance)} subtask(s) lack acceptance criteria"
            )
            score -= 2
        elif len(without_acceptance) > len(subtasks) // 2:
            issues.append(
                f"{len(without_acceptance)} subtask(s) lack acceptance criteria"
            )
            score -= 1

    return DimensionScore(
        dimension=ScoreDimension.COMPLETENESS,
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
    )


def score_correctness(plan: dict) -> DimensionScore:
    """
    Score correctness: Are dependencies properly ordered?

    Checks:
        - No circular dependencies
        - Dependencies reference existing subtask IDs
        - File actions make sense (can't modify before create)
        - Dependency order is logical

    Args:
        plan: Plan dictionary with 'subtasks'

    Returns:
        DimensionScore with 0-5 score and issues/suggestions
    """
    issues = []
    suggestions = []
    score = 5

    subtasks = plan.get("subtasks", [])
    if not subtasks:
        return DimensionScore(
            dimension=ScoreDimension.CORRECTNESS,
            score=0,
            issues=["No subtasks to validate"],
            suggestions=[],
        )

    # Build ID set and position map
    subtask_ids = {s.get("id") for s in subtasks if s.get("id")}
    id_to_position = {s.get("id"): i for i, s in enumerate(subtasks) if s.get("id")}

    # Check: All dependencies reference valid IDs
    for subtask in subtasks:
        depends_on = subtask.get("depends_on", [])
        subtask_id = subtask.get("id", "unknown")

        for dep_id in depends_on:
            if dep_id not in subtask_ids:
                issues.append(
                    f"Subtask '{subtask_id}' depends on non-existent '{dep_id}'"
                )
                score -= 1

            # Check: Dependency comes before dependent
            elif id_to_position.get(dep_id, 0) >= id_to_position.get(subtask_id, 0):
                issues.append(
                    f"Subtask '{subtask_id}' depends on '{dep_id}' which comes later"
                )
                score -= 1

    # Check for circular dependencies (simple cycle detection)
    def has_cycle(start_id: str, visited: set, path: set) -> bool:
        if start_id in path:
            return True
        if start_id in visited:
            return False

        visited.add(start_id)
        path.add(start_id)

        subtask = next((s for s in subtasks if s.get("id") == start_id), None)
        if subtask:
            for dep_id in subtask.get("depends_on", []):
                if has_cycle(dep_id, visited, path):
                    return True

        path.remove(start_id)
        return False

    visited: set = set()
    for subtask in subtasks:
        if has_cycle(subtask.get("id", ""), visited, set()):
            issues.append("Circular dependency detected in subtask dependencies")
            score -= 2
            break

    # Check: File action consistency
    file_actions: dict[str, list[str]] = {}  # file -> list of actions
    for subtask in subtasks:
        file_path = subtask.get("file")
        action = subtask.get("action", "")
        if file_path and action:
            if file_path not in file_actions:
                file_actions[file_path] = []
            file_actions[file_path].append(action)

    for file_path, actions in file_actions.items():
        # Can't modify/delete before create
        if "create" in actions:
            create_idx = actions.index("create")
            for i, action in enumerate(actions):
                if action in ("modify", "delete") and i < create_idx:
                    issues.append(f"File '{file_path}' has '{action}' before 'create'")
                    score -= 1

    return DimensionScore(
        dimension=ScoreDimension.CORRECTNESS,
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
    )


def score_feasibility(plan: dict) -> DimensionScore:
    """
    Score feasibility: Can subtasks be executed with available resources?

    Checks:
        - File paths are reasonable (not absolute system paths)
        - Actions are recognized types
        - Estimated effort is reasonable
        - No impossible requirements

    Args:
        plan: Plan dictionary with 'subtasks'

    Returns:
        DimensionScore with 0-5 score and issues/suggestions
    """
    issues = []
    suggestions = []
    score = 5

    subtasks = plan.get("subtasks", [])
    if not subtasks:
        return DimensionScore(
            dimension=ScoreDimension.FEASIBILITY,
            score=0,
            issues=["No subtasks to validate"],
            suggestions=[],
        )

    for subtask in subtasks:
        subtask_id = subtask.get("id", "unknown")
        action = subtask.get("action", "")
        file_path = subtask.get("file")

        # Check: Action is valid
        if action and action.lower() not in VALID_FILE_ACTIONS:
            suggestions.append(
                f"Subtask '{subtask_id}' has unusual action '{action}'. "
                f"Expected one of: {sorted(VALID_FILE_ACTIONS)}"
            )

        # Check: File path is reasonable
        if file_path:
            # Flag suspicious paths
            if file_path.startswith("/etc/"):
                issues.append(
                    f"Subtask '{subtask_id}' targets system path: {file_path}"
                )
                score -= 2
            elif file_path.startswith("/usr/"):
                issues.append(
                    f"Subtask '{subtask_id}' targets system path: {file_path}"
                )
                score -= 2
            elif file_path.startswith("/var/"):
                suggestions.append(
                    f"Subtask '{subtask_id}' targets var path: {file_path}"
                )
                score -= 1

    # Check: Reasonable number of subtasks
    if len(subtasks) > 20:
        suggestions.append(
            f"Plan has {len(subtasks)} subtasks. Consider breaking into phases."
        )
        score -= 1

    return DimensionScore(
        dimension=ScoreDimension.FEASIBILITY,
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
    )


def score_specificity(plan: dict) -> DimensionScore:
    """
    Score specificity: Are subtasks concrete and actionable?

    Checks:
        - Subtasks have specific file targets (not "update files")
        - Actions are concrete (not "do something")
        - Acceptance criteria are measurable
        - Names are descriptive

    Args:
        plan: Plan dictionary with 'subtasks'

    Returns:
        DimensionScore with 0-5 score and issues/suggestions
    """
    issues = []
    suggestions = []
    score = 5

    subtasks = plan.get("subtasks", [])
    if not subtasks:
        return DimensionScore(
            dimension=ScoreDimension.SPECIFICITY,
            score=0,
            issues=["No subtasks to validate"],
            suggestions=[],
        )

    vague_patterns = [
        r"^do\s+",
        r"^handle\s+",
        r"^deal\s+with",
        r"^fix\s+stuff",
        r"^update\s+things",
        r"^make\s+changes",
        r"^implement\s+it$",
        r"^add\s+functionality$",
    ]

    for subtask in subtasks:
        subtask_id = subtask.get("id", "unknown")
        name = subtask.get("name", "")
        description = subtask.get("description", "")
        file_path = subtask.get("file")
        acceptance = subtask.get("acceptance", "")

        # Check: Name is specific
        name_lower = name.lower()
        for pattern in vague_patterns:
            if re.search(pattern, name_lower):
                issues.append(f"Subtask '{subtask_id}' has vague name: '{name}'")
                score -= 1
                break

        # Check: Has file target for modification actions
        action = subtask.get("action", "")
        if action in ("create", "modify", "delete") and not file_path:
            issues.append(
                f"Subtask '{subtask_id}' is '{action}' action but has no file target"
            )
            score -= 1

        # Check: Acceptance criteria are specific
        if acceptance:
            if len(acceptance) < 10:
                suggestions.append(
                    f"Subtask '{subtask_id}' has brief acceptance criteria: "
                    f"'{acceptance}'"
                )
            if acceptance.lower() in ("done", "complete", "finished", "works"):
                issues.append(
                    f"Subtask '{subtask_id}' has non-measurable acceptance: "
                    f"'{acceptance}'"
                )
                score -= 1

        # Check: Description provides context
        if not description or len(description) < 10:
            suggestions.append(
                f"Subtask '{subtask_id}' could use a more detailed description"
            )

    return DimensionScore(
        dimension=ScoreDimension.SPECIFICITY,
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
    )


def score_safety(plan: dict) -> DimensionScore:
    """
    Score safety: No dangerous operations or missing validations?

    Checks:
        - No dangerous shell patterns (rm -rf, sudo, etc.)
        - No destructive database operations
        - File operations are within project scope
        - No credential/secret exposure

    Args:
        plan: Plan dictionary with 'subtasks'

    Returns:
        DimensionScore with 0-5 score and issues/suggestions
    """
    issues = []
    suggestions = []
    score = 5

    subtasks = plan.get("subtasks", [])
    if not subtasks:
        return DimensionScore(
            dimension=ScoreDimension.SAFETY,
            score=5,  # Empty plan is safe
            issues=[],
            suggestions=["Add subtasks to implement the task"],
        )

    for subtask in subtasks:
        subtask_id = subtask.get("id", "unknown")
        name = subtask.get("name", "")
        description = subtask.get("description", "")

        # Check all text fields for dangerous patterns
        text_to_check = f"{name} {description}"

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                issues.append(
                    f"Subtask '{subtask_id}' contains dangerous pattern: '{pattern}'"
                )
                score -= 2

        # Check: No credential-related operations without validation
        cred_patterns = [
            r"password",
            r"api[_-]?key",
            r"secret",
            r"token",
            r"credential",
            r"\.env",
        ]
        for pattern in cred_patterns:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                suggestions.append(
                    f"Subtask '{subtask_id}' involves credentials. "
                    "Ensure proper handling."
                )
                # Don't deduct score, just flag for review

        # Check: Delete operations have safeguards
        if subtask.get("action") == "delete":
            file_path = subtask.get("file", "")
            if "*" in file_path or not file_path:
                issues.append(
                    f"Subtask '{subtask_id}' is delete action with "
                    "wildcard or no target"
                )
                score -= 2

    return DimensionScore(
        dimension=ScoreDimension.SAFETY,
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
    )


# -----------------------------------------------------------------------------
# Main Validation Function
# -----------------------------------------------------------------------------


def validate_plan(
    plan: dict,
    failure_context: ReplanContext | None = None,
) -> CheckerResult:
    """
    Validate a task plan and return a CheckerResult.

    Scores the plan on 5 dimensions (completeness, correctness, feasibility,
    specificity, safety) and determines the verdict based on total score.

    Args:
        plan: Plan dictionary with structure:
            {
                "task_id": str,
                "original_task": {...},
                "subtasks": [...],
                "created_at": str,
                "planner_version": str
            }
        failure_context: If re-planning after failure, contains context
            about what went wrong

    Returns:
        CheckerResult with:
            - passed: True if score >= PASS_THRESHOLD
            - score: Total score (0-25)
            - verdict: PASS, REVISE, or ESCALATE
            - dimension_scores: Individual dimension scores
            - issues: Critical issues to fix
            - suggestions: Improvement recommendations
    """
    # Score each dimension
    dimension_scores = [
        score_completeness(plan),
        score_correctness(plan),
        score_feasibility(plan),
        score_specificity(plan),
        score_safety(plan),
    ]

    # Calculate total score
    total_score = sum(d.score for d in dimension_scores)

    # Aggregate issues and suggestions
    all_issues = []
    all_suggestions = []
    for ds in dimension_scores:
        all_issues.extend(ds.issues)
        all_suggestions.extend(ds.suggestions)

    # Determine verdict
    if total_score >= PASS_THRESHOLD:
        verdict = CheckerVerdict.PASS
        passed = True
    elif total_score >= REVISE_THRESHOLD:
        verdict = CheckerVerdict.REVISE
        passed = False
    else:
        verdict = CheckerVerdict.ESCALATE
        passed = False

    # Add failure context hints if present
    if failure_context:
        all_suggestions.insert(
            0,
            f"Previous execution failed: {failure_context.failure_reason}. "
            "Plan should address this failure mode.",
        )
        for constraint in failure_context.constraints:
            all_suggestions.append(f"Constraint from failure: {constraint}")

    return CheckerResult(
        passed=passed,
        score=total_score,
        verdict=verdict,
        dimension_scores=dimension_scores,
        issues=all_issues,
        suggestions=all_suggestions,
        plan_id=plan.get("task_id", ""),
        failure_context=failure_context.to_dict() if failure_context else None,
    )


# -----------------------------------------------------------------------------
# Escalation Integration
# -----------------------------------------------------------------------------


def escalate_plan(
    plan: dict,
    checker_result: CheckerResult,
    task_id: str | None = None,
) -> dict:
    """
    Escalate a failed plan to human review.

    Called when checker verdict is ESCALATE (score < 12).
    Creates an escalation record for the task.

    Args:
        plan: The plan that failed validation
        checker_result: The CheckerResult from validate_plan
        task_id: Optional task ID (uses plan's task_id if not provided)

    Returns:
        Escalation result dict
    """
    _ensure_imports()

    if _escalation is None:
        return {
            "success": False,
            "error": "Escalation module not available",
            "checker_result": checker_result.to_dict(),
        }

    effective_task_id = task_id or plan.get("task_id", "unknown")

    try:
        result = _escalation.escalate(
            task_id=effective_task_id,
            reason="quality_rejection",
            notes=f"Plan validation failed with score {checker_result.score}/{MAX_SCORE}. "
            f"Issues: {'; '.join(checker_result.issues[:3])}",
        )
        result["checker_result"] = checker_result.to_dict()
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "checker_result": checker_result.to_dict(),
        }


def notify_escalation(
    checker_result: CheckerResult,
    plan: dict,
) -> dict:
    """
    Send notification for plan escalation.

    Uses the notify.py hook for desktop notifications and optionally
    external services for Tier 4 escalations.

    Args:
        checker_result: The CheckerResult that triggered escalation
        plan: The plan that failed

    Returns:
        Notification result dict
    """
    _ensure_imports()

    original_task = plan.get("original_task", {})
    task_title = original_task.get("title", "Unknown task")

    message = (
        f"Plan validation ESCALATE: {task_title}\n"
        f"Score: {checker_result.score}/{MAX_SCORE}\n"
        f"Issues: {len(checker_result.issues)}"
    )

    # Use escalation's send_notification if available
    if _escalation is not None:
        try:
            _escalation.send_notification(
                title="Plan Validation Failed",
                message=message,
            )
            return {"success": True, "message": message}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Fallback: print to stderr
    print(f"[ESCALATE] {message}", file=sys.stderr)
    return {"success": True, "message": message, "method": "stderr"}


# -----------------------------------------------------------------------------
# Self-Healing Integration
# -----------------------------------------------------------------------------


def create_replan_context(
    original_plan: dict,
    failed_subtask_id: str | None,
    failure_reason: str,
    failure_output: str = "",
    previous_context: ReplanContext | None = None,
) -> ReplanContext:
    """
    Create context for re-planning after execution failure.

    Called by the execution layer when a subtask fails. The context
    is passed to task_planner to generate a revised plan.

    Args:
        original_plan: The plan that failed during execution
        failed_subtask_id: ID of the subtask that failed (None if plan-level)
        failure_reason: Description of the failure
        failure_output: stdout/stderr from failed execution
        previous_context: If this is a retry, the previous ReplanContext

    Returns:
        ReplanContext for the task_planner
    """
    # Extract learned constraints from failure
    constraints = []

    # Parse common failure patterns
    if "permission denied" in failure_output.lower():
        constraints.append("Ensure file permissions are correct before write")
    if "file not found" in failure_output.lower():
        constraints.append("Verify file exists before modification")
    if "syntax error" in failure_output.lower():
        constraints.append("Validate syntax before execution")
    if "import error" in failure_output.lower():
        constraints.append("Check dependencies are installed")
    if "timeout" in failure_output.lower():
        constraints.append("Consider breaking into smaller steps")

    # Inherit previous constraints and attempted fixes
    attempted_fixes = []
    retry_count = 0
    if previous_context:
        constraints.extend(previous_context.constraints)
        attempted_fixes = previous_context.attempted_fixes.copy()
        attempted_fixes.append(failure_reason)
        retry_count = previous_context.retry_count + 1

    # Deduplicate constraints
    constraints = list(dict.fromkeys(constraints))

    return ReplanContext(
        original_plan_id=original_plan.get("task_id", "unknown"),
        failed_subtask_id=failed_subtask_id,
        failure_reason=failure_reason,
        failure_output=failure_output[:2000],  # Truncate long output
        attempted_fixes=attempted_fixes,
        constraints=constraints,
        retry_count=retry_count,
    )


def should_retry(context: ReplanContext, max_retries: int = 3) -> bool:
    """
    Determine if re-planning should be attempted.

    Args:
        context: The ReplanContext from a failed execution
        max_retries: Maximum number of retry attempts

    Returns:
        True if retry should be attempted, False if escalation needed
    """
    return context.retry_count < max_retries


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    print(__doc__)


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


def cmd_validate(args: dict) -> None:
    """Validate a plan from file or stdin."""
    plan_data = None

    if args.get("stdin"):
        try:
            plan_data = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
            sys.exit(1)
    elif args.get("plan_file"):
        plan_path = Path(args["plan_file"])
        if not plan_path.exists():
            print(
                json.dumps({"success": False, "error": f"File not found: {plan_path}"})
            )
            sys.exit(1)
        try:
            with open(plan_path, encoding="utf-8") as f:
                plan_data = json.load(f)
        except json.JSONDecodeError as e:
            print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
            sys.exit(1)
    else:
        print(json.dumps({"success": False, "error": "Provide --plan-file or --stdin"}))
        sys.exit(1)

    # Load failure context if provided
    failure_context = None
    if args.get("failure_context"):
        try:
            ctx_data = json.loads(args["failure_context"])
            failure_context = ReplanContext.from_dict(ctx_data)
        except (json.JSONDecodeError, TypeError) as e:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": f"Invalid failure context: {e}",
                    }
                )
            )
            sys.exit(1)

    result = validate_plan(plan_data, failure_context)

    if args.get("verbose"):
        output = {
            "success": True,
            "result": result.to_dict(),
            "summary": {
                "verdict": result.verdict.value,
                "score": f"{result.score}/{MAX_SCORE}",
                "passed": result.passed,
                "issue_count": len(result.issues),
                "suggestion_count": len(result.suggestions),
            },
        }
    else:
        output = {
            "success": True,
            "passed": result.passed,
            "score": result.score,
            "verdict": result.verdict.value,
            "issues": result.issues,
        }

    print(json.dumps(output, indent=2))

    # Handle escalation if needed
    if result.verdict == CheckerVerdict.ESCALATE:
        escalate_plan(plan_data, result)
        notify_escalation(result, plan_data)


def cmd_thresholds() -> None:
    """Show scoring thresholds."""
    result = {
        "success": True,
        "thresholds": {
            "pass": {
                "min_score": PASS_THRESHOLD,
                "description": "Plan passes validation, proceed with execution",
            },
            "revise": {
                "score_range": f"{REVISE_THRESHOLD}-{PASS_THRESHOLD - 1}",
                "description": "Attempt one auto-revision cycle",
            },
            "escalate": {
                "max_score": REVISE_THRESHOLD - 1,
                "description": "Requires human review",
            },
        },
        "max_score": MAX_SCORE,
        "dimensions": [d.value for d in ScoreDimension],
        "points_per_dimension": 5,
    }
    print(json.dumps(result, indent=2))


def cmd_create_context(args: dict) -> None:
    """Create a replan context from failure info."""
    failure_reason = args.get("reason", "Unknown failure")
    failure_output = args.get("output", "")
    failed_subtask = args.get("subtask_id")

    # Load original plan if provided
    plan_data = {}
    if args.get("plan_file"):
        plan_path = Path(args["plan_file"])
        if plan_path.exists():
            with open(plan_path, encoding="utf-8") as f:
                plan_data = json.load(f)

    # Load previous context if retry
    previous_context = None
    if args.get("previous_context"):
        try:
            ctx_data = json.loads(args["previous_context"])
            previous_context = ReplanContext.from_dict(ctx_data)
        except (json.JSONDecodeError, TypeError):
            pass

    context = create_replan_context(
        original_plan=plan_data,
        failed_subtask_id=failed_subtask,
        failure_reason=failure_reason,
        failure_output=failure_output,
        previous_context=previous_context,
    )

    result = {
        "success": True,
        "context": context.to_dict(),
        "should_retry": should_retry(context),
    }
    print(json.dumps(result, indent=2))


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    try:
        if command == "validate":
            cmd_validate(args)
        elif command == "thresholds":
            cmd_thresholds()
        elif command == "create-context":
            cmd_create_context(args)
        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
