#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Manager Review Module — P26 Task 26.7

Provides manager/tech lead review capabilities for:
- Reviewing completed tasks with quality feedback
- Approving/rejecting employee proposals
- Generating constructive feedback for learning

This module creates the middle management layer that connects
executive strategy with employee execution.

Usage:
    # Review a completed task
    python manager_review.py review --task-id "task-123" --reviewer-id "forge-architect"

    # List tasks awaiting review
    python manager_review.py list-reviews

    # List pending proposals
    python manager_review.py list-proposals

    # Approve a proposal
    python manager_review.py approve --task-id "task-123" --reviewer-id "forge-architect"

    # Reject a proposal
    python manager_review.py reject --task-id "task-123" --reviewer-id "forge-architect" --reason "..."
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
work_allocator = None
company_resolver = None
approval_learner = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global work_allocator, company_resolver, approval_learner
    if work_allocator is not None:
        return

    try:
        from . import approval_learner as al
        from . import company_resolver as cr
        from . import work_allocator as wa

        work_allocator = wa
        company_resolver = cr
        approval_learner = al
    except ImportError:
        import approval_learner as al  # type: ignore[no-redef]
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        work_allocator = wa
        company_resolver = cr
        approval_learner = al


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


# Quality score thresholds
QUALITY_EXCELLENT = 0.9
QUALITY_GOOD = 0.7
QUALITY_ACCEPTABLE = 0.5

# Recurring task patterns that are always safe to auto-review.
# Each entry maps a compiled regex to a human-readable reason.
# These patterns are checked AFTER the hard safety gates (not-escalation,
# not-critical-priority, not-NEVER_AUTO_REVIEW_TYPES) but BYPASS the
# complexity and high-risk-keyword checks since the title itself proves
# the task is a known-safe repetitive pattern.
RECURRING_AUTO_REVIEW_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"^Documentation Gaps \(\d+ files?\)$"),
        "recurring doc-gap audit task",
    ),
    (
        re.compile(r"^(\[AUTO\] )?Health Recovery: fix \w+"),
        "recurring auto-generated health recovery task",
    ),
    (
        re.compile(r"^(Improve test coverage for: )+"),
        "recurring test-coverage improvement task",
    ),
    (
        re.compile(r"^Diagnose G\d+\b"),
        "recurring goal diagnosis task",
    ),
    (
        re.compile(r"^G\d+ .*(metric|uptime|stability|queue health)", re.IGNORECASE),
        "recurring goal metric assessment task",
    ),
    (
        re.compile(r"^Bootstrap G\d+\b"),
        "recurring goal bootstrap task",
    ),
    (
        re.compile(r"^(Fix linter|Fix ruff|Fix type)\b", re.IGNORECASE),
        "recurring linter/type fix task",
    ),
    # G10 Employee Initiative recurring review tasks
    (
        re.compile(
            r"^(Review|Coordinate)\b.*\b(G10|employee (initiative|idea)|codebase and (company state|generate))",
            re.IGNORECASE,
        ),
        "recurring G10 employee initiative review task",
    ),
    # Recurring documentation/docstring audit tasks
    (
        re.compile(r"^Audit and fill docstring gaps\b", re.IGNORECASE),
        "recurring docstring audit task",
    ),
    # Recurring CI/pipeline audit tasks (non-security)
    (
        re.compile(r"^Audit CI pipeline\b", re.IGNORECASE),
        "recurring CI pipeline audit task",
    ),
]


def get_managers() -> list[str]:
    """
    Get list of manager/tech lead IDs who can perform reviews.

    Managers are identified by role containing 'architect', 'lead', 'manager',
    or 'senior'.
    """
    org_path = get_company_dir() / "org.json"

    if not org_path.exists():
        return []

    try:
        with open(org_path, "r") as f:
            org = json.load(f)
        # Normalize bare-string employees to dict records (ProjectK root-cause fix).
        try:
            from . import company_resolver as cr
        except ImportError:
            import company_resolver as cr  # type: ignore[no-redef]
        org = cr.normalize_org_employees(org, org_path.parent)

        managers = []
        manager_keywords = [
            "architect",
            "lead",
            "manager",
            "senior",
            "cto",
            "ceo",
            "executive",
        ]

        for emp in org.get("employees", []):
            emp_id = emp.get("id", "").lower()
            role = emp.get("role", "").lower()

            # Check both role AND employee ID for manager keywords
            if emp_id and (
                any(kw in role for kw in manager_keywords)
                or any(kw in emp_id for kw in manager_keywords)
            ):
                managers.append(emp.get("id"))

        return managers
    except (json.JSONDecodeError, OSError):
        return []


def is_manager(employee_id: str) -> bool:
    """Check if an employee is a manager/reviewer."""
    return employee_id in get_managers()


def generate_quality_feedback(
    task: dict,
    quality_score: float,
) -> str:
    """
    Generate constructive feedback based on task and quality score.

    Args:
        task: The completed task
        quality_score: 0.0-1.0 quality assessment

    Returns:
        Feedback string
    """
    title = task.get("title", "Task")
    complexity = task.get("estimated_complexity", "standard")

    if quality_score >= QUALITY_EXCELLENT:
        return (
            f"Excellent work on '{title}'! "
            f"The implementation exceeds expectations for {complexity} complexity. "
            "Consider sharing your approach with the team."
        )
    elif quality_score >= QUALITY_GOOD:
        return (
            f"Good work on '{title}'. "
            f"The implementation meets quality standards for {complexity} complexity. "
            "Keep up the consistent delivery."
        )
    elif quality_score >= QUALITY_ACCEPTABLE:
        return (
            f"Acceptable completion of '{title}'. "
            "Consider additional testing or documentation in future similar tasks. "
            "Review the coding standards guide for improvements."
        )
    else:
        return (
            f"'{title}' needs improvement. "
            "Key areas to focus on: code quality, testing coverage, and documentation. "
            "Consider pairing with a senior developer on the next similar task."
        )


def review_completed_task(
    task_id: str,
    reviewer_id: str,
    quality_score: float | None = None,
    feedback: str | None = None,
    auto_generate_feedback: bool = True,
) -> dict:
    """
    Review a completed task and provide feedback.

    Args:
        task_id: The task ID in review queue
        reviewer_id: ID of the reviewing manager
        quality_score: Optional quality score (0.0-1.0)
        feedback: Optional custom feedback
        auto_generate_feedback: If True and no feedback, generate automatically

    Returns:
        Dict with review result
    """
    _ensure_imports()

    if not is_manager(reviewer_id):
        return {
            "success": False,
            "error": "not_authorized",
            "message": f"{reviewer_id} is not authorized to review tasks",
        }

    # Get the task
    task_result = work_allocator.get_task(task_id)
    if not task_result.get("success"):
        return task_result

    task = task_result.get("task", {})
    current_status = task_result.get("status")

    if current_status != "review":
        return {
            "success": False,
            "error": "wrong_status",
            "message": f"Task is in '{current_status}' status, not 'review'",
        }

    # Default quality score if not provided
    if quality_score is None:
        quality_score = 0.75  # Default to "good"

    # Generate feedback if not provided and auto-generate is enabled
    if feedback is None and auto_generate_feedback:
        feedback = generate_quality_feedback(task, quality_score)

    # Complete the review
    result = work_allocator.complete_review(
        task_id=task_id,
        reviewer_id=reviewer_id,
        feedback=feedback,
        quality_score=quality_score,
    )

    if result.get("success"):
        # Store feedback for employee learning (append to their memory)
        _store_employee_feedback(
            employee_id=task.get("assigned_to"),
            task_id=task_id,
            task_title=task.get("title"),
            quality_score=quality_score,
            feedback=feedback,
            reviewer_id=reviewer_id,
        )

    return result


def _store_employee_feedback(
    employee_id: str | None,
    task_id: str,
    task_title: str,
    quality_score: float,
    feedback: str | None,
    reviewer_id: str,
):
    """Store feedback in employee's memory for learning."""
    if not employee_id:
        return

    company_dir = get_company_dir()
    memory_path = company_dir / "agents" / employee_id / "feedback.json"

    # Load existing feedback
    feedbacks = []
    if memory_path.exists():
        try:
            with open(memory_path, "r") as f:
                feedbacks = json.load(f)
        except (json.JSONDecodeError, OSError):
            feedbacks = []

    # Add new feedback
    feedbacks.append(
        {
            "task_id": task_id,
            "task_title": task_title,
            "quality_score": quality_score,
            "feedback": feedback,
            "reviewer_id": reviewer_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Keep only last 50 feedbacks
    feedbacks = feedbacks[-50:]

    # Save atomically (prevents corruption under parallel workers)
    try:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(memory_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(feedbacks, f, indent=2)
            os.replace(tmp_path, str(memory_path))
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError:
        pass  # Best effort


def approve_proposal(
    task_id: str,
    reviewer_id: str,
    priority: int | None = None,
    notes: str | None = None,
) -> dict:
    """
    Approve an employee proposal.

    Args:
        task_id: The proposal task ID
        reviewer_id: ID of the approving manager
        priority: Optional adjusted priority
        notes: Optional approval notes

    Returns:
        Dict with approval result
    """
    _ensure_imports()

    if not is_manager(reviewer_id):
        return {
            "success": False,
            "error": "not_authorized",
            "message": f"{reviewer_id} is not authorized to approve proposals",
        }

    return work_allocator.approve_proposal(
        task_id=task_id,
        reviewer_id=reviewer_id,
        priority=priority,
        notes=notes,
    )


def reject_proposal(
    task_id: str,
    reviewer_id: str,
    reason: str,
) -> dict:
    """
    Reject an employee proposal with feedback.

    Args:
        task_id: The proposal task ID
        reviewer_id: ID of the rejecting manager
        reason: Reason for rejection (constructive feedback)

    Returns:
        Dict with rejection result
    """
    _ensure_imports()

    if not is_manager(reviewer_id):
        return {
            "success": False,
            "error": "not_authorized",
            "message": f"{reviewer_id} is not authorized to reject proposals",
        }

    result = work_allocator.reject_proposal(
        task_id=task_id,
        reviewer_id=reviewer_id,
        reason=reason,
    )

    if result.get("success"):
        # Get proposal info for feedback
        # Note: proposal is now in completed queue with rejection info
        _store_proposal_feedback(
            task_id=task_id,
            reason=reason,
            reviewer_id=reviewer_id,
        )

    return result


def _store_proposal_feedback(
    task_id: str,
    reason: str,
    reviewer_id: str,
):
    """Store proposal rejection feedback for learning."""
    _ensure_imports()

    # Get the rejected proposal from completed queue
    queue = work_allocator.load_queue()
    for task in queue.get("completed", []):
        if task.get("task_id") == task_id and task.get("status") == "rejected":
            proposer_id = task.get("proposed_by")
            if proposer_id:
                _store_employee_feedback(
                    employee_id=proposer_id,
                    task_id=task_id,
                    task_title=task.get("title", ""),
                    quality_score=0.3,  # Low score for rejected proposals
                    feedback=f"Proposal rejected: {reason}",
                    reviewer_id=reviewer_id,
                )
            break


def get_pending_reviews(
    reviewer_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """
    Get tasks awaiting review.

    Args:
        reviewer_id: Optional filter by suggested reviewer
        project_id: Optional filter by project

    Returns:
        Dict with review queue
    """
    _ensure_imports()
    return work_allocator.list_reviews(project_id=project_id)


def get_pending_proposals(
    reviewer_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """
    Get proposals awaiting approval.

    Args:
        reviewer_id: Optional filter (not yet implemented)
        project_id: Optional filter by project

    Returns:
        Dict with proposal queue
    """
    _ensure_imports()
    return work_allocator.list_proposals(project_id=project_id)


def auto_review_completed_tasks(
    reviewer_id: str,
    max_reviews: int = 20,
) -> dict:
    """
    Auto-review completed tasks that meet clear success criteria.

    Tasks are auto-reviewed when ALL of these hold:
    - Task complexity is trivial or standard
    - Task type/title contains no high-risk keywords
    - Task is not from an escalation
    - Task type is not in the NEVER_AUTO_REVIEW blocklist

    Args:
        reviewer_id: ID of the auto-reviewing manager
        max_reviews: Maximum tasks to auto-review per call

    Returns:
        Dict with auto-review results
    """
    _ensure_imports()

    if not is_manager(reviewer_id):
        return {
            "success": False,
            "error": "not_authorized",
            "message": f"{reviewer_id} is not authorized to review tasks",
        }

    # Types that must ALWAYS require human review (security boundary)
    NEVER_AUTO_REVIEW_TYPES = {
        "hook_adjustment",
        "workflow_change",
        "capability_addition",
        "security",
        "migration",
    }

    HIGH_RISK_KEYWORDS = {
        "security",
        "auth",
        "password",
        "token",
        "secret",
        "api_key",
        "database",
        "migration",
        "hook",
        "permission",
    }

    AUTO_REVIEW_COMPLEXITY = {"trivial", "standard", ""}

    reviews_result = get_pending_reviews()
    reviews = reviews_result.get("reviews", [])

    reviewed = 0
    skipped = 0
    results = []

    for task in reviews:
        if reviewed >= max_reviews:
            break

        task_id = task.get("task_id")
        raw_title = task.get("title") or ""
        title = raw_title.lower()
        description = (task.get("description") or "").lower()
        complexity = (task.get("estimated_complexity") or "").lower()
        proposal_type = (task.get("proposal_type") or "").lower()
        source = (task.get("source") or "").lower()
        priority = task.get("priority", 3)

        # --- Hard safety gates (apply to ALL tasks including recurring) ---

        # Never auto-review escalated tasks
        if source == "escalation":
            skipped += 1
            continue

        # Never auto-review critical priority
        if priority == 1:
            skipped += 1
            continue

        # Never auto-review self-modification types
        if proposal_type in NEVER_AUTO_REVIEW_TYPES:
            skipped += 1
            continue

        # High-risk keyword check applies to ALL tasks (including recurring)
        text = f"{title} {description}"
        if any(kw in text for kw in HIGH_RISK_KEYWORDS):
            skipped += 1
            continue

        # --- Recurring pattern fast-path ---
        # Known-safe repetitive tasks bypass the complexity check only
        recurring_match = None
        for pattern, reason in RECURRING_AUTO_REVIEW_PATTERNS:
            if pattern.search(raw_title):
                recurring_match = reason
                break

        if not recurring_match:
            # Standard path: also check complexity
            if complexity not in AUTO_REVIEW_COMPLEXITY:
                skipped += 1
                continue

        # Task passes all checks — auto-review with default good score
        feedback_msg = (
            f"Auto-reviewed: recurring pattern ({recurring_match})"
            if recurring_match
            else "Auto-reviewed: task met success criteria (low-risk, standard complexity)"
        )
        result = review_completed_task(
            task_id=task_id,
            reviewer_id=reviewer_id,
            quality_score=0.75,
            feedback=feedback_msg,
        )

        if result.get("success"):
            reviewed += 1
            results.append(
                {
                    "task_id": task_id,
                    "title": task.get("title"),
                    "status": "auto_reviewed",
                }
            )
            # Record decision in approval learner for adaptive tier learning
            _record_auto_review_decision(task, reviewer_id)
        else:
            results.append(
                {
                    "task_id": task_id,
                    "title": task.get("title"),
                    "status": "error",
                    "error": result.get("message"),
                }
            )

    return {
        "success": True,
        "reviewed": reviewed,
        "skipped": skipped,
        "results": results,
    }


def _record_auto_review_decision(task: dict, reviewer_id: str) -> None:
    """Record an auto-review decision in the approval learner for adaptive learning.

    Best-effort: failures are silently ignored to avoid blocking the review flow.
    """
    _ensure_imports()
    if approval_learner is None:
        return
    try:
        decision = approval_learner.ApprovalDecision(
            decision_id="",
            task_type=task.get("proposal_type") or "task",
            complexity=task.get("estimated_complexity") or "standard",
            was_approved=True,
            approver="auto",
            outcome="success",
            task_id=task.get("task_id", ""),
        )
        approval_learner.record_decision(decision)
    except Exception:
        pass  # Best effort — don't break review flow


def auto_approve_low_risk_proposals(
    reviewer_id: str,
    max_approvals: int = 10,
) -> dict:
    """
    Auto-approve low-risk proposals.

    Low-risk proposals are:
    - TODOs and documentation tasks
    - Complexity: trivial or standard
    - No security implications

    Args:
        reviewer_id: ID of the auto-approving manager
        max_approvals: Maximum proposals to auto-approve

    Returns:
        Dict with auto-approval results
    """
    _ensure_imports()

    if not is_manager(reviewer_id):
        return {
            "success": False,
            "error": "not_authorized",
            "message": f"{reviewer_id} is not authorized to approve proposals",
        }

    LOW_RISK_TYPES = {
        "todo",
        "improvement",
        "documentation",
        "docs",
        "documentation_update",  # initiative_engine / capability_proposer variant
        "employee_idea",
        "task_investigation",
        "employee_reassignment",
        "process_improvement",
        "bottleneck_observation",
        "capability_expansion",
        "collaboration_proposal",
        "self_improvement",
        "follow_up",  # employee_initiative.py follow-up proposals
        "test_coverage_sprint",  # initiative_engine: coverage improvement proposals
        "dependency_update_minor",  # initiative_engine: minor version bumps (safe)
        "code_quality",  # initiative_engine / capability_proposer: safe refactoring
        "performance_optimization",  # initiative_engine: optimisation proposals
        # NOTE: workflow_change, hook_adjustment, capability_addition are
        # intentionally EXCLUDED — they are in NEVER_AUTO_REVIEW_TYPES.
        # NOTE: dependency_update_major, hiring_recommendation, capability_enhancement,
        # security_fix, roadmap_phase are intentionally EXCLUDED — require human review.
    }
    LOW_RISK_COMPLEXITY = {"trivial", "standard", ""}
    HIGH_RISK_KEYWORDS = {
        "security",
        "auth",
        "password",
        "token",
        "secret",
        "database",
        "migration",
    }
    # "api" removed - too many false positives in docs/tests

    # Action words that make keyword mentions actually risky
    ACTION_WORDS = {
        "modify",
        "change",
        "delete",
        "remove",
        "update",
        "fix",
        "implement",
        "add",
        "create",
        "refactor",
        "rewrite",
    }

    # Safe prefixes - documentation and testing are low-risk even with keywords
    SAFE_PREFIXES = {
        "document:",
        "improve test coverage",
        "verify and test:",
        "add documentation",
        "write tests",
        "add tests",
    }

    proposals_result = get_pending_proposals()
    proposals = proposals_result.get("proposals", [])

    approved = 0
    skipped = 0
    results = []

    for proposal in proposals:
        if approved >= max_approvals:
            break

        # Check if low-risk
        proposal_type = (proposal.get("proposal_type") or "").lower()
        complexity = (proposal.get("estimated_complexity") or "").lower()
        title = (proposal.get("title") or "").lower()
        description = (proposal.get("description") or "").lower()

        # Check for safe prefixes first - docs/tests are safe
        text = f"{title} {description}"
        is_safe_prefix = any(title.startswith(prefix) for prefix in SAFE_PREFIXES)

        # Only check high-risk keywords for non-safe proposals
        is_high_risk = False
        if not is_safe_prefix:
            # Check if high-risk keywords appear with action words
            has_keyword = any(kw in text for kw in HIGH_RISK_KEYWORDS)
            has_action = any(act in text for act in ACTION_WORDS)
            is_high_risk = has_keyword and has_action

        if is_high_risk:
            skipped += 1
            continue

        if proposal_type in LOW_RISK_TYPES and complexity in LOW_RISK_COMPLEXITY:
            result = approve_proposal(
                task_id=proposal.get("task_id"),
                reviewer_id=reviewer_id,
                notes="Auto-approved: low-risk proposal",
            )
            if result.get("success"):
                approved += 1
                results.append(
                    {
                        "task_id": proposal.get("task_id"),
                        "title": proposal.get("title"),
                        "status": "approved",
                    }
                )
            else:
                results.append(
                    {
                        "task_id": proposal.get("task_id"),
                        "title": proposal.get("title"),
                        "status": "error",
                        "error": result.get("message"),
                    }
                )
        else:
            skipped += 1

    # Phase 2: also process pending_approvals.json (proposals written by initiative_engine)
    pending_approved = _auto_approve_from_pending_file(
        LOW_RISK_TYPES,
        LOW_RISK_COMPLEXITY,
        HIGH_RISK_KEYWORDS,
        max_approvals - approved,
    )
    approved += pending_approved

    return {
        "success": True,
        "approved": approved,
        "skipped": skipped,
        "results": results,
    }


def _auto_approve_from_pending_file(
    low_risk_types: set,
    low_risk_complexity: set,
    high_risk_keywords: set,
    remaining_budget: int,
) -> int:
    """Auto-approve eligible proposals directly from pending_approvals.json.

    Bridges the gap between proposals written by initiative_engine (which go to
    pending_approvals.json) and the auto-approver (which previously only read
    work_queue.json["proposed"]).  Returns the count of newly approved proposals.
    """
    try:
        from . import company_paths
    except ImportError:
        import company_paths  # type: ignore[no-redef]

    pending_path = company_paths.PENDING_APPROVALS
    if not pending_path.exists():
        return 0

    try:
        with open(pending_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    proposals = data.get("proposals", [])
    approved_list = data.get("approved", [])
    approved_count = 0
    remaining_proposals = []

    for proposal in proposals:
        if approved_count >= remaining_budget:
            remaining_proposals.append(proposal)
            continue

        # Skip proposals that require human approval
        approval_required = proposal.get("approval_required") or []
        if isinstance(approval_required, list) and "human" in approval_required:
            remaining_proposals.append(proposal)
            continue
        if isinstance(approval_required, str) and "human" in approval_required:
            remaining_proposals.append(proposal)
            continue

        proposal_type = (proposal.get("proposal_type") or "").lower()
        complexity = (proposal.get("estimated_complexity") or "").lower()
        title = (proposal.get("title") or "").lower()
        description = (proposal.get("description") or "").lower()

        if proposal_type not in low_risk_types:
            remaining_proposals.append(proposal)
            continue

        if complexity not in low_risk_complexity:
            remaining_proposals.append(proposal)
            continue

        text = f"{title} {description}"
        if any(kw in text for kw in high_risk_keywords):
            remaining_proposals.append(proposal)
            continue

        # Eligible — move to approved list
        approved_entry = dict(proposal)
        approved_entry["approved_at"] = datetime.now(timezone.utc).isoformat()
        approved_entry["approved_by"] = "auto-approver"
        approved_list.append(approved_entry)
        approved_count += 1

    if approved_count == 0:
        return 0

    # Save updated data atomically
    data["proposals"] = remaining_proposals
    data["approved"] = approved_list

    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(pending_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(pending_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        return 0  # Best effort

    return approved_count


def get_review_stats(
    days: int = 7,
) -> dict:
    """
    Get review statistics.

    Args:
        days: Number of days to analyze

    Returns:
        Dict with review metrics
    """
    _ensure_imports()

    queue = work_allocator.load_queue()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Count reviews and proposals
    completed = queue.get("completed", [])
    pending_reviews = len(queue.get("review", []))
    pending_proposals = len(queue.get("proposed", []))

    reviewed_count = 0
    approved_count = 0
    rejected_count = 0
    total_quality = 0.0

    for task in completed:
        # Check if within time window
        completed_at_str = task.get("completed_at") or task.get("reviewed_at")
        if completed_at_str:
            try:
                completed_at = datetime.fromisoformat(
                    completed_at_str.replace("Z", "+00:00")
                )
                if completed_at < cutoff:
                    continue
            except (ValueError, TypeError):
                continue

        if task.get("reviewed_by"):
            reviewed_count += 1
            quality = task.get("quality_score", 0.7)
            total_quality += quality

        if task.get("approved_by"):
            approved_count += 1

        if task.get("status") == "rejected":
            rejected_count += 1

    avg_quality = total_quality / reviewed_count if reviewed_count > 0 else 0.0

    return {
        "period_days": days,
        "pending_reviews": pending_reviews,
        "pending_proposals": pending_proposals,
        "tasks_reviewed": reviewed_count,
        "proposals_approved": approved_count,
        "proposals_rejected": rejected_count,
        "average_quality_score": round(avg_quality, 2),
        "approval_rate": (
            approved_count / (approved_count + rejected_count)
            if (approved_count + rejected_count) > 0
            else 0.0
        ),
    }


def print_help():
    """Print usage help."""
    help_text = """
Manager Review Module (P26)

Commands:
    review          Review a completed task
    approve         Approve a proposal
    reject          Reject a proposal
    list-reviews    List tasks awaiting review
    list-proposals  List pending proposals
    auto-approve    Auto-approve low-risk proposals
    stats           Get review statistics

Options:
    --task-id ID        Task or proposal ID
    --reviewer-id ID    Manager/reviewer ID (required)
    --quality FLOAT     Quality score (0.0-1.0)
    --feedback TEXT     Custom feedback message
    --reason TEXT       Rejection reason (for reject)
    --priority 1-4      Adjusted priority (for approve)
    --notes TEXT        Approval notes

Examples:
    python manager_review.py review --task-id task-123 --reviewer-id forge-architect
    python manager_review.py approve --task-id task-123 --reviewer-id forge-architect
    python manager_review.py reject --task-id task-123 --reviewer-id forge-architect --reason "Needs more detail"
    python manager_review.py auto-approve --reviewer-id forge-architect
    python manager_review.py stats
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
        if command == "review":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "reviewer_id" not in args:
                print("Error: --reviewer-id required")
                sys.exit(1)

            quality = None
            if "quality" in args:
                quality = float(args["quality"])

            result = review_completed_task(
                task_id=args["task_id"],
                reviewer_id=args["reviewer_id"],
                quality_score=quality,
                feedback=args.get("feedback"),
            )
            print(json.dumps(result, indent=2))

        elif command == "approve":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "reviewer_id" not in args:
                print("Error: --reviewer-id required")
                sys.exit(1)

            priority = None
            if "priority" in args:
                priority = int(args["priority"])

            result = approve_proposal(
                task_id=args["task_id"],
                reviewer_id=args["reviewer_id"],
                priority=priority,
                notes=args.get("notes"),
            )
            print(json.dumps(result, indent=2))

        elif command == "reject":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "reviewer_id" not in args:
                print("Error: --reviewer-id required")
                sys.exit(1)
            if "reason" not in args:
                print("Error: --reason required")
                sys.exit(1)

            result = reject_proposal(
                task_id=args["task_id"],
                reviewer_id=args["reviewer_id"],
                reason=args["reason"],
            )
            print(json.dumps(result, indent=2))

        elif command == "list-reviews":
            result = get_pending_reviews()
            print(json.dumps(result, indent=2))

        elif command == "list-proposals":
            result = get_pending_proposals()
            print(json.dumps(result, indent=2))

        elif command == "auto-approve":
            if "reviewer_id" not in args:
                print("Error: --reviewer-id required")
                sys.exit(1)

            max_approvals = int(args.get("max", 5))
            result = auto_approve_low_risk_proposals(
                reviewer_id=args["reviewer_id"],
                max_approvals=max_approvals,
            )
            print(json.dumps(result, indent=2))

        elif command == "stats":
            days = int(args.get("days", 7))
            result = get_review_stats(days=days)
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
