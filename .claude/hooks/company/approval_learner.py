#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P25 Approval Learner — Adaptive Human-in-Loop Decision System.

Learns from approval history to dynamically adjust approval tiers.
Some decisions always need human approval (high risk), while others
can be auto-approved after consistent success patterns.

Key Capabilities:
    - Record approval decisions with outcomes
    - Compute policy from patterns (what gets rejected, what succeeds)
    - Dynamic tier adjustment with weekly refresh
    - Safety constraints for always-human categories

Storage:
    - Approval history: .company/approval_history.json
    - Policy refresh: Weekly (configurable)

Usage:
    # Record an approval decision
    python approval_learner.py record --task-type "deployment" \\
        --complexity standard --was-approved true --outcome success

    # Compute current policy
    python approval_learner.py policy

    # Check if task should auto-approve
    python approval_learner.py check --task-type "minor-fix" --complexity trivial

    # Force policy refresh
    python approval_learner.py refresh

    # Show help
    python approval_learner.py help
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

APPROVAL_HISTORY_FILE = "approval_history.json"
POLICY_REFRESH_DAYS = 7
ROLLING_WINDOW_DAYS = 30
RETENTION_DAYS = 90

# Minimum sample size for reliable policy decisions
MIN_SAMPLE_SIZE = 5

# WS-119: Fast-track sample size for known-safe categories (see SAFE_CATEGORIES).
# These categories have low blast radius and the cost of a wrong call is small,
# so they can graduate after fewer samples to lift the auto-approve rate sooner.
FAST_TRACK_SAMPLE_SIZE = 3

# Success rate threshold for auto-approval graduation
AUTO_APPROVE_SUCCESS_THRESHOLD = 0.95

# Failure threshold that triggers reverting to human approval
HUMAN_APPROVAL_FAILURE_THRESHOLD = 0.2

# WS-119: Categories that are explicitly safe to fast-track. These are
# documentation, style, and similar low-risk task types where a wrong
# approval costs little and is easy to revert.
SAFE_CATEGORIES = frozenset(
    [
        "docs-update",
        "documentation",
        "docstring",
        "formatting",
        "lint-fix",
        "style",
        "dependency-minor",
        "dependency-patch",
        "comment-update",
        "typo-fix",
    ]
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("approval_learner")


# =============================================================================
# Enums
# =============================================================================


class ApprovalTier(Enum):
    """Approval tiers for decisions.

    Determines whether human approval is required.
    """

    AUTO_APPROVE = "auto_approve"  # Low risk, execute automatically
    CONFIG_APPROVE = "config_approve"  # Check config flag
    HUMAN_APPROVE = "human_approve"  # Requires human sign-off


class Outcome(Enum):
    """Outcome of an approved/executed decision."""

    SUCCESS = "success"  # Task completed successfully
    FAILURE = "failure"  # Task failed
    PARTIAL = "partial"  # Partial success
    REVERTED = "reverted"  # Had to be reverted
    UNKNOWN = "unknown"  # Outcome not yet recorded


# =============================================================================
# Safety Constraints
# =============================================================================

# Task types that ALWAYS require human approval regardless of history
ALWAYS_HUMAN_TYPES = frozenset(
    [
        "security",
        "security-fix",
        "production-deploy",
        "database-migration",
        "budget-change",
        "hiring",
        "firing",
        "policy-change",
        "major-refactor",
        "breaking-change",
        "roadmap-phase",
        "strategic-decision",
        "customer-facing",
        "api-breaking",
        "data-deletion",
        "access-control",
        "encryption-change",
    ]
)

# Complexity levels that ALWAYS require human approval
ALWAYS_HUMAN_COMPLEXITY = frozenset(["epic"])

# Budget thresholds (in arbitrary units) above which human approval is required
BUDGET_THRESHOLD = 1000


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ApprovalDecision:
    """Record of a single approval decision and its outcome."""

    decision_id: str
    task_type: str
    complexity: str  # trivial/standard/complex/epic
    was_approved: bool
    approver: str  # "human", "auto", "config"
    outcome: str  # success/failure/partial/reverted/unknown
    task_id: str = ""
    budget_impact: float = 0.0
    tags: list[str] = field(default_factory=list)
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "decision_id": self.decision_id,
            "task_type": self.task_type,
            "complexity": self.complexity,
            "was_approved": self.was_approved,
            "approver": self.approver,
            "outcome": self.outcome,
            "task_id": self.task_id,
            "budget_impact": self.budget_impact,
            "tags": self.tags,
            "recorded_at": self.recorded_at.isoformat(),
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalDecision:
        """Create from dictionary."""
        recorded_at = data.get("recorded_at")
        if isinstance(recorded_at, str):
            recorded_at = datetime.fromisoformat(recorded_at)
        elif recorded_at is None:
            recorded_at = datetime.now(timezone.utc)

        return cls(
            decision_id=data.get("decision_id", ""),
            task_type=data.get("task_type", "unknown"),
            complexity=data.get("complexity", "standard"),
            was_approved=data.get("was_approved", False),
            approver=data.get("approver", "unknown"),
            outcome=data.get("outcome", "unknown"),
            task_id=data.get("task_id", ""),
            budget_impact=data.get("budget_impact", 0.0),
            tags=data.get("tags", []),
            recorded_at=recorded_at,
            context=data.get("context", {}),
        )


@dataclass
class TypePolicy:
    """Policy for a specific task type."""

    task_type: str
    current_tier: ApprovalTier
    sample_size: int
    success_rate: float
    rejection_rate: float
    can_graduate: bool  # Can move to more permissive tier
    should_demote: bool  # Should move to more restrictive tier
    locked: bool  # Cannot be auto-adjusted (safety constraint)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_type": self.task_type,
            "current_tier": self.current_tier.value,
            "sample_size": self.sample_size,
            "success_rate": round(self.success_rate, 3),
            "rejection_rate": round(self.rejection_rate, 3),
            "can_graduate": self.can_graduate,
            "should_demote": self.should_demote,
            "locked": self.locked,
            "reason": self.reason,
        }


@dataclass
class ApprovalPolicy:
    """Computed approval policy from historical data."""

    tier_adjustments: dict[str, str]  # task_type -> tier
    auto_approve_types: list[str]  # Types safe for auto-approval
    always_human_types: list[str]  # Types requiring human approval
    type_policies: dict[str, TypePolicy]  # Detailed policy per type
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    next_refresh: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=7)
    )
    total_decisions: int = 0
    overall_success_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "tier_adjustments": self.tier_adjustments,
            "auto_approve_types": self.auto_approve_types,
            "always_human_types": self.always_human_types,
            "type_policies": {k: v.to_dict() for k, v in self.type_policies.items()},
            "computed_at": self.computed_at.isoformat(),
            "next_refresh": self.next_refresh.isoformat(),
            "total_decisions": self.total_decisions,
            "overall_success_rate": round(self.overall_success_rate, 3),
        }


# =============================================================================
# Path Utilities
# =============================================================================


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def _get_company_dir() -> Path:
    """Get the company directory path."""
    # Try to use company_resolver if available
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        return company_resolver.get_company_dir()
    except (ImportError, Exception):
        # Fallback to default
        return Path.cwd() / ".company"


def _get_history_path() -> Path:
    """Get the approval history file path."""
    return _get_company_dir() / APPROVAL_HISTORY_FILE


def _ensure_company_dir():
    """Ensure company directory exists."""
    company_dir = _get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Storage Functions
# =============================================================================


def _get_empty_history_data() -> dict[str, Any]:
    """Return empty history data structure."""
    return {
        "decisions": [],
        "cached_policy": None,
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "rolling_window_days": ROLLING_WINDOW_DAYS,
            "retention_days": RETENTION_DAYS,
            "policy_refresh_days": POLICY_REFRESH_DAYS,
            "version": "1.0",
        },
    }


def _load_history_data() -> dict[str, Any]:
    """Load approval history data from file."""
    path = _get_history_path()

    if not path.exists():
        return _get_empty_history_data()

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load history data: {e}")
        return _get_empty_history_data()


def _save_history_data(data: dict[str, Any]):
    """Save history data to file with atomic write."""
    _ensure_company_dir()
    path = _get_history_path()

    # Apply retention cleanup
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.isoformat()

    data["decisions"] = [
        decision
        for decision in data.get("decisions", [])
        if decision.get("recorded_at", "") >= cutoff_str
    ]

    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Atomic write: write to temp file, then os.replace
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix="approval_", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
        logger.debug(f"Saved history data to {path}")
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# ID Generation
# =============================================================================


def _generate_decision_id(task_type: str) -> str:
    """Generate a unique decision ID."""
    import hashlib

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    hash_suffix = hashlib.md5(f"{timestamp}{task_type}".encode()).hexdigest()[:8]
    return f"appr-{timestamp}-{hash_suffix}"


# =============================================================================
# Core Functions
# =============================================================================


def record_decision(decision: ApprovalDecision) -> dict[str, Any]:
    """
    Record an approval decision.

    Appends the decision to the approval history for analysis.

    Args:
        decision: The approval decision to store

    Returns:
        Dict with recording result including decision_id
    """
    data = _load_history_data()

    # Generate ID if not set
    if not decision.decision_id:
        decision.decision_id = _generate_decision_id(decision.task_type)

    decision_dict = decision.to_dict()
    data.setdefault("decisions", []).append(decision_dict)

    # Invalidate cached policy when new data arrives
    data["cached_policy"] = None

    _save_history_data(data)

    logger.info(
        f"Recorded decision {decision.decision_id}: "
        f"type={decision.task_type}, approved={decision.was_approved}, "
        f"outcome={decision.outcome}"
    )

    return {
        "success": True,
        "decision_id": decision.decision_id,
        "recorded_at": decision.recorded_at.isoformat(),
        "total_decisions": len(data["decisions"]),
    }


def load_decisions(days: int = ROLLING_WINDOW_DAYS) -> list[ApprovalDecision]:
    """
    Load decisions within the rolling window.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of ApprovalDecision objects
    """
    data = _load_history_data()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    decisions = []
    for decision_dict in data.get("decisions", []):
        recorded_at = decision_dict.get("recorded_at", "")
        if recorded_at >= cutoff_str:
            try:
                decisions.append(ApprovalDecision.from_dict(decision_dict))
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to parse decision: {e}")
                continue

    return decisions


def compute_policy(decisions: list[ApprovalDecision] | None = None) -> ApprovalPolicy:
    """
    Compute approval policy from historical data.

    Analyzes decisions to determine:
    - Which task types can safely auto-approve
    - Which types should require human approval
    - Success/failure patterns by type

    Args:
        decisions: List of decisions to analyze (loads from file if None)

    Returns:
        ApprovalPolicy object with computed recommendations
    """
    if decisions is None:
        decisions = load_decisions()

    now = datetime.now(timezone.utc)
    next_refresh = now + timedelta(days=POLICY_REFRESH_DAYS)

    if not decisions:
        return ApprovalPolicy(
            tier_adjustments={},
            auto_approve_types=[],
            always_human_types=list(ALWAYS_HUMAN_TYPES),
            type_policies={},
            computed_at=now,
            next_refresh=next_refresh,
            total_decisions=0,
            overall_success_rate=0.0,
        )

    # -------------------------------------------------------------------------
    # Group decisions by task type
    # -------------------------------------------------------------------------
    type_decisions: dict[str, list[ApprovalDecision]] = {}
    for decision in decisions:
        task_type = decision.task_type
        if task_type not in type_decisions:
            type_decisions[task_type] = []
        type_decisions[task_type].append(decision)

    # -------------------------------------------------------------------------
    # Calculate overall success rate
    # -------------------------------------------------------------------------
    approved_decisions = [d for d in decisions if d.was_approved]
    successful = sum(
        1 for d in approved_decisions if d.outcome == Outcome.SUCCESS.value
    )
    overall_success_rate = (
        successful / len(approved_decisions) if approved_decisions else 0.0
    )

    # -------------------------------------------------------------------------
    # Compute policy for each type
    # -------------------------------------------------------------------------
    tier_adjustments: dict[str, str] = {}
    auto_approve_types: list[str] = []
    always_human_types: list[str] = list(ALWAYS_HUMAN_TYPES)
    type_policies: dict[str, TypePolicy] = {}

    for task_type, type_decs in type_decisions.items():
        sample_size = len(type_decs)

        # Count approved vs rejected
        approved = [d for d in type_decs if d.was_approved]
        rejected = [d for d in type_decs if not d.was_approved]

        # Calculate success rate (only from approved decisions)
        successful = sum(1 for d in approved if d.outcome == Outcome.SUCCESS.value)
        failed = sum(
            1
            for d in approved
            if d.outcome in (Outcome.FAILURE.value, Outcome.REVERTED.value)
        )

        success_rate = successful / len(approved) if approved else 0.0
        failure_rate = failed / len(approved) if approved else 0.0
        rejection_rate = len(rejected) / sample_size if sample_size > 0 else 0.0

        # Check safety constraints
        is_locked = (
            task_type in ALWAYS_HUMAN_TYPES
            or any(d.complexity in ALWAYS_HUMAN_COMPLEXITY for d in type_decs)
            or any(d.budget_impact >= BUDGET_THRESHOLD for d in type_decs)
        )

        # WS-119: Apply fast-track sample size for known-safe categories.
        # These have low blast radius so we accept fewer samples before
        # graduating them to auto-approve.
        effective_min_sample = (
            FAST_TRACK_SAMPLE_SIZE if task_type in SAFE_CATEGORIES else MIN_SAMPLE_SIZE
        )

        # Determine current tier and adjustments
        if is_locked:
            current_tier = ApprovalTier.HUMAN_APPROVE
            can_graduate = False
            should_demote = False
            reason = "Safety constraint: always requires human approval"
        elif sample_size < effective_min_sample:
            current_tier = ApprovalTier.HUMAN_APPROVE
            can_graduate = False
            should_demote = False
            reason = (
                f"Insufficient data: {sample_size} < {effective_min_sample} samples"
            )
        elif success_rate >= AUTO_APPROVE_SUCCESS_THRESHOLD:
            current_tier = ApprovalTier.AUTO_APPROVE
            can_graduate = True
            should_demote = False
            reason = f"High success rate: {success_rate:.1%} >= {AUTO_APPROVE_SUCCESS_THRESHOLD:.0%}"
            auto_approve_types.append(task_type)
        elif failure_rate >= HUMAN_APPROVAL_FAILURE_THRESHOLD:
            current_tier = ApprovalTier.HUMAN_APPROVE
            can_graduate = False
            should_demote = True
            reason = f"High failure rate: {failure_rate:.1%} >= {HUMAN_APPROVAL_FAILURE_THRESHOLD:.0%}"
            if task_type not in always_human_types:
                always_human_types.append(task_type)
        else:
            current_tier = ApprovalTier.CONFIG_APPROVE
            can_graduate = success_rate > 0.8
            should_demote = failure_rate > 0.1
            reason = f"Moderate performance: {success_rate:.1%} success"

        tier_adjustments[task_type] = current_tier.value

        type_policies[task_type] = TypePolicy(
            task_type=task_type,
            current_tier=current_tier,
            sample_size=sample_size,
            success_rate=success_rate,
            rejection_rate=rejection_rate,
            can_graduate=can_graduate,
            should_demote=should_demote,
            locked=is_locked,
            reason=reason,
        )

    return ApprovalPolicy(
        tier_adjustments=tier_adjustments,
        auto_approve_types=auto_approve_types,
        always_human_types=always_human_types,
        type_policies=type_policies,
        computed_at=now,
        next_refresh=next_refresh,
        total_decisions=len(decisions),
        overall_success_rate=overall_success_rate,
    )


def should_auto_approve(
    task_type: str,
    complexity: str = "standard",
    budget_impact: float = 0.0,
    policy: ApprovalPolicy | None = None,
) -> tuple[bool, str]:
    """
    Determine if a task should be auto-approved based on learned policy.

    Conservative by default: starts with human approval, graduates to
    auto-approval only after consistent success patterns.

    Args:
        task_type: The type of task
        complexity: Task complexity (trivial/standard/complex/epic)
        budget_impact: Financial impact of the task
        policy: Pre-computed policy (computes if None)

    Returns:
        Tuple of (should_auto_approve, reason)
    """
    # Safety constraints first - these are non-negotiable
    if task_type in ALWAYS_HUMAN_TYPES:
        return (False, f"Task type '{task_type}' always requires human approval")

    if complexity in ALWAYS_HUMAN_COMPLEXITY:
        return (False, f"Complexity '{complexity}' always requires human approval")

    if budget_impact >= BUDGET_THRESHOLD:
        return (
            False,
            f"Budget impact {budget_impact} >= {BUDGET_THRESHOLD} threshold",
        )

    # Load policy if not provided
    if policy is None:
        policy = compute_policy()

    # Check if we have learned policy for this type
    type_policy = policy.type_policies.get(task_type)

    if type_policy is None:
        return (
            False,
            f"No history for task type '{task_type}' - defaulting to human approval",
        )

    if type_policy.locked:
        return (False, type_policy.reason)

    if type_policy.current_tier == ApprovalTier.AUTO_APPROVE:
        return (
            True,
            f"Auto-approved: {type_policy.success_rate:.1%} success rate "
            f"over {type_policy.sample_size} decisions",
        )

    if type_policy.should_demote:
        return (
            False,
            f"High failure rate detected: {type_policy.reason}",
        )

    # Default: require human approval for unproven types
    return (False, f"Not yet graduated to auto-approve: {type_policy.reason}")


def get_approval_tier(
    task_type: str,
    complexity: str = "standard",
    budget_impact: float = 0.0,
    policy: ApprovalPolicy | None = None,
) -> ApprovalTier:
    """
    Get the appropriate approval tier for a task.

    Args:
        task_type: The type of task
        complexity: Task complexity
        budget_impact: Financial impact
        policy: Pre-computed policy (computes if None)

    Returns:
        ApprovalTier enum value
    """
    can_auto, _ = should_auto_approve(task_type, complexity, budget_impact, policy)

    if can_auto:
        return ApprovalTier.AUTO_APPROVE

    # Check for config-level approval
    if policy is None:
        policy = compute_policy()

    type_policy = policy.type_policies.get(task_type)
    if type_policy and type_policy.current_tier == ApprovalTier.CONFIG_APPROVE:
        return ApprovalTier.CONFIG_APPROVE

    return ApprovalTier.HUMAN_APPROVE


def refresh_policy(force: bool = False) -> ApprovalPolicy:
    """
    Refresh the cached policy.

    Recomputes policy from all decisions if:
    - Forced refresh requested
    - Policy has expired (past next_refresh date)
    - No cached policy exists

    Args:
        force: Force refresh even if cache is valid

    Returns:
        Updated ApprovalPolicy
    """
    data = _load_history_data()
    cached = data.get("cached_policy")
    now = datetime.now(timezone.utc)

    should_refresh = force
    if cached is None:
        should_refresh = True
    elif not force:
        # Check if cached policy has expired
        next_refresh = cached.get("next_refresh")
        if next_refresh:
            try:
                refresh_time = datetime.fromisoformat(next_refresh)
                if now >= refresh_time:
                    should_refresh = True
            except ValueError:
                should_refresh = True

    if should_refresh:
        logger.info("Refreshing approval policy...")
        policy = compute_policy()

        # Cache the policy
        data["cached_policy"] = policy.to_dict()
        _save_history_data(data)

        logger.info(
            f"Policy refreshed: {len(policy.auto_approve_types)} auto-approve types, "
            f"{len(policy.always_human_types)} always-human types"
        )
        return policy
    else:
        # Return cached policy
        logger.debug("Using cached policy")
        return ApprovalPolicy(
            tier_adjustments=cached.get("tier_adjustments", {}),
            auto_approve_types=cached.get("auto_approve_types", []),
            always_human_types=cached.get("always_human_types", []),
            type_policies={
                k: TypePolicy(
                    task_type=k,
                    current_tier=ApprovalTier(v.get("current_tier", "human_approve")),
                    sample_size=v.get("sample_size", 0),
                    success_rate=v.get("success_rate", 0.0),
                    rejection_rate=v.get("rejection_rate", 0.0),
                    can_graduate=v.get("can_graduate", False),
                    should_demote=v.get("should_demote", False),
                    locked=v.get("locked", True),
                    reason=v.get("reason", ""),
                )
                for k, v in cached.get("type_policies", {}).items()
            },
            computed_at=datetime.fromisoformat(
                cached.get("computed_at", now.isoformat())
            ),
            next_refresh=datetime.fromisoformat(
                cached.get("next_refresh", (now + timedelta(days=7)).isoformat())
            ),
            total_decisions=cached.get("total_decisions", 0),
            overall_success_rate=cached.get("overall_success_rate", 0.0),
        )


def update_outcome(
    decision_id: str, outcome: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Update the outcome of a previously recorded decision.

    Useful when outcome is not known at approval time.

    Args:
        decision_id: The decision ID to update
        outcome: The outcome (success/failure/partial/reverted)
        context: Additional context about the outcome

    Returns:
        Dict with update result
    """
    data = _load_history_data()

    for decision in data.get("decisions", []):
        if decision.get("decision_id") == decision_id:
            decision["outcome"] = outcome
            if context:
                decision.setdefault("context", {}).update(context)

            # Invalidate cached policy
            data["cached_policy"] = None

            _save_history_data(data)

            logger.info(f"Updated outcome for {decision_id}: {outcome}")
            return {
                "success": True,
                "decision_id": decision_id,
                "new_outcome": outcome,
            }

    return {
        "success": False,
        "error": f"Decision {decision_id} not found",
    }


def get_learning_summary() -> dict[str, Any]:
    """
    Get a summary of the learning state.

    Returns:
        Dict with learning statistics
    """
    policy = refresh_policy()
    decisions = load_decisions()

    # Group by approver type
    by_approver: dict[str, int] = {}
    for d in decisions:
        by_approver[d.approver] = by_approver.get(d.approver, 0) + 1

    # Group by outcome
    by_outcome: dict[str, int] = {}
    for d in decisions:
        by_outcome[d.outcome] = by_outcome.get(d.outcome, 0) + 1

    # Calculate graduation candidates
    graduation_candidates = [
        tp.task_type
        for tp in policy.type_policies.values()
        if tp.can_graduate and tp.current_tier != ApprovalTier.AUTO_APPROVE
    ]

    # Calculate demotion candidates
    demotion_candidates = [
        tp.task_type
        for tp in policy.type_policies.values()
        if tp.should_demote and tp.current_tier != ApprovalTier.HUMAN_APPROVE
    ]

    return {
        "total_decisions": policy.total_decisions,
        "overall_success_rate": policy.overall_success_rate,
        "decisions_by_approver": by_approver,
        "decisions_by_outcome": by_outcome,
        "auto_approve_types": policy.auto_approve_types,
        "always_human_types": policy.always_human_types,
        "graduation_candidates": graduation_candidates,
        "demotion_candidates": demotion_candidates,
        "policy_computed_at": policy.computed_at.isoformat(),
        "policy_next_refresh": policy.next_refresh.isoformat(),
        "type_count": len(policy.type_policies),
    }


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="P25 Approval Learner — Adaptive Human-in-Loop Decision System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Record command
    record_parser = subparsers.add_parser("record", help="Record an approval decision")
    record_parser.add_argument("--task-type", required=True, help="Task type")
    record_parser.add_argument(
        "--complexity",
        default="standard",
        choices=["trivial", "standard", "complex", "epic"],
        help="Task complexity",
    )
    record_parser.add_argument(
        "--was-approved", type=str, default="true", help="Was approved (true/false)"
    )
    record_parser.add_argument(
        "--outcome",
        default="unknown",
        choices=["success", "failure", "partial", "reverted", "unknown"],
        help="Outcome",
    )
    record_parser.add_argument("--approver", default="human", help="Who approved")
    record_parser.add_argument("--task-id", default="", help="Task ID")
    record_parser.add_argument(
        "--budget-impact", type=float, default=0.0, help="Budget impact"
    )
    record_parser.add_argument("--tags", default="", help="Comma-separated tags")

    # Policy command
    subparsers.add_parser("policy", help="Compute and display current policy")

    # Check command
    check_parser = subparsers.add_parser(
        "check", help="Check if task should auto-approve"
    )
    check_parser.add_argument("--task-type", required=True, help="Task type")
    check_parser.add_argument(
        "--complexity",
        default="standard",
        choices=["trivial", "standard", "complex", "epic"],
        help="Complexity",
    )
    check_parser.add_argument(
        "--budget-impact", type=float, default=0.0, help="Budget impact"
    )

    # Refresh command
    refresh_parser = subparsers.add_parser("refresh", help="Force policy refresh")
    refresh_parser.add_argument(
        "--force", action="store_true", help="Force refresh even if cache is valid"
    )

    # Summary command
    subparsers.add_parser("summary", help="Show learning summary")

    # Update outcome command
    update_parser = subparsers.add_parser(
        "update-outcome", help="Update outcome of a decision"
    )
    update_parser.add_argument("--decision-id", required=True, help="Decision ID")
    update_parser.add_argument(
        "--outcome",
        required=True,
        choices=["success", "failure", "partial", "reverted"],
        help="New outcome",
    )

    # Help command
    subparsers.add_parser("help", help="Show help")

    args = parser.parse_args()

    if args.command == "help" or args.command is None:
        parser.print_help()
        return

    if args.command == "record":
        decision = ApprovalDecision(
            decision_id="",  # Will be generated
            task_type=args.task_type,
            complexity=args.complexity,
            was_approved=args.was_approved.lower() == "true",
            approver=args.approver,
            outcome=args.outcome,
            task_id=args.task_id,
            budget_impact=args.budget_impact,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        )
        result = record_decision(decision)
        print(json.dumps(result, indent=2))

    elif args.command == "policy":
        policy = compute_policy()
        print(json.dumps(policy.to_dict(), indent=2))

    elif args.command == "check":
        should_auto, reason = should_auto_approve(
            task_type=args.task_type,
            complexity=args.complexity,
            budget_impact=args.budget_impact,
        )
        tier = get_approval_tier(
            task_type=args.task_type,
            complexity=args.complexity,
            budget_impact=args.budget_impact,
        )
        print(
            json.dumps(
                {
                    "task_type": args.task_type,
                    "complexity": args.complexity,
                    "should_auto_approve": should_auto,
                    "tier": tier.value,
                    "reason": reason,
                },
                indent=2,
            )
        )

    elif args.command == "refresh":
        policy = refresh_policy(force=args.force if hasattr(args, "force") else True)
        print(
            json.dumps(
                {
                    "refreshed": True,
                    "computed_at": policy.computed_at.isoformat(),
                    "next_refresh": policy.next_refresh.isoformat(),
                    "auto_approve_count": len(policy.auto_approve_types),
                    "always_human_count": len(policy.always_human_types),
                },
                indent=2,
            )
        )

    elif args.command == "summary":
        summary = get_learning_summary()
        print(json.dumps(summary, indent=2))

    elif args.command == "update-outcome":
        result = update_outcome(args.decision_id, args.outcome)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
