#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# P20: CEO Planning Authority
"""
Planning Authority — CEO sign-off mechanism for planning documents.

P20 implementation: Establishes formal CEO planning authority with:
- Plan submission and review workflow
- C-level board approval process
- Organizational assignment system (RACI model)
- Automatic board session scheduling

Plan Lifecycle:
    PROPOSAL -> CEO_REVIEW -> BOARD_REVIEW -> APPROVED -> ACTIVE -> COMPLETE
        |           |              |              |
        v           v              v              v
    REJECTED   REVISION       REVISION      SUSPENDED

Usage:
    # Submit a plan for CEO review
    python planning_authority.py submit --title "P21: Feature X" --type roadmap_phase

    # CEO reviews pending plans
    python planning_authority.py review --plan-id plan-xxx

    # CEO approves a plan
    python planning_authority.py approve --plan-id plan-xxx

    # Initiate board session
    python planning_authority.py board-session --plan-id plan-xxx

    # Assign goal ownership
    python planning_authority.py assign-goal --goal-id G7 --employee-id forge-architect

    # List pending reviews
    python planning_authority.py pending

    # Show planning status
    python planning_authority.py status
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# Lazy imports
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------


class PlanStatus(str, Enum):
    """Plan lifecycle status."""

    PROPOSAL = "proposal"
    CEO_REVIEW = "ceo_review"
    BOARD_REVIEW = "board_review"
    APPROVED = "approved"
    ACTIVE = "active"
    COMPLETE = "complete"
    REJECTED = "rejected"
    REVISION = "revision"
    SUSPENDED = "suspended"


class PlanType(str, Enum):
    """Types of planning documents."""

    ROADMAP_PHASE = "roadmap_phase"
    GOAL = "goal"
    INITIATIVE = "initiative"
    VISION_CHANGE = "vision_change"
    REORG = "reorg"
    BUG_FIX = "bug_fix"
    DOCUMENTATION = "documentation"


class PlanSize(str, Enum):
    """Plan size for approval routing."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    EPIC = "epic"


class DecisionType(str, Enum):
    """Types of decisions."""

    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    ABSTAIN = "abstain"


class AssignmentType(str, Enum):
    """Types of organizational assignments."""

    GOAL_OWNER = "goal_owner"
    PHASE_OWNER = "phase_owner"
    TASK_OWNER = "task_owner"
    INITIATIVE_OWNER = "initiative_owner"


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


@dataclass
class PlanSubmission:
    """A plan submitted for CEO review."""

    plan_id: str
    title: str
    plan_type: str  # PlanType value
    status: str  # PlanStatus value
    size: str  # PlanSize value
    document_path: str | None = None
    description: str = ""

    # Lifecycle tracking
    proposed_at: str = ""
    proposed_by: str = ""
    ceo_reviewed_at: str | None = None
    ceo_decision: str | None = None
    ceo_comments: str | None = None
    board_reviewed_at: str | None = None
    board_decision: str | None = None
    board_participants: list[str] = field(default_factory=list)
    approved_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None

    # Assignment
    assigned_owner: str | None = None
    accountable_executive: str = "forge-ceo"
    estimated_effort: str | None = None
    strategic_alignment: list[str] = field(default_factory=list)


@dataclass
class BoardSession:
    """A C-level board review session."""

    session_id: str
    session_type: str  # "initiative_approval", "strategic_review", "emergency"
    status: str  # "scheduled", "in_progress", "completed", "cancelled"
    scheduled_at: str
    started_at: str | None = None
    completed_at: str | None = None

    # Participants
    required_participants: list[str] = field(
        default_factory=lambda: ["forge-ceo", "forge-cto"]
    )
    actual_participants: list[str] = field(default_factory=list)

    # Agenda
    agenda_items: list[str] = field(default_factory=list)  # Plan IDs
    decisions: dict[str, dict] = field(
        default_factory=dict
    )  # plan_id -> {ceo: decision, cto: decision}

    # Output
    board_decision: str | None = None  # Final consensus
    notes: str = ""


@dataclass
class AssignmentRecord:
    """Organizational assignment record."""

    assignment_id: str
    assignment_type: str  # AssignmentType value
    target_id: str  # Goal ID, Phase ID, etc.
    target_name: str
    employee_id: str
    employee_name: str

    assigned_at: str
    assigned_by: str
    accountability: str = "delivery"  # "delivery", "oversight", "consultation"
    review_frequency: str = "weekly"  # "daily", "weekly", "monthly"

    # Status
    acknowledged: bool = False
    acknowledged_at: str | None = None


@dataclass
class PlanningState:
    """Full planning authority state."""

    schema_version: str = "1.0"
    plans: list[dict] = field(default_factory=list)
    pending_ceo_review: list[str] = field(default_factory=list)  # Plan IDs
    pending_board_review: list[str] = field(default_factory=list)  # Plan IDs
    board_sessions: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    last_updated: str = ""


# -----------------------------------------------------------------------------
# State Management
# -----------------------------------------------------------------------------


def _get_planning_state_path() -> Path:
    """Get path to planning approvals state file."""
    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    return company_dir / "planning_approvals.json"


def load_planning_state() -> PlanningState:
    """Load the planning authority state."""
    path = _get_planning_state_path()
    if not path.exists():
        return PlanningState(last_updated=datetime.now(timezone.utc).isoformat())

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PlanningState(
            schema_version=data.get("schema_version", "1.0"),
            plans=data.get("plans", []),
            pending_ceo_review=data.get("pending_ceo_review", []),
            pending_board_review=data.get("pending_board_review", []),
            board_sessions=data.get("board_sessions", []),
            assignments=data.get("assignments", []),
            history=data.get("history", []),
            last_updated=data.get("last_updated", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return PlanningState(last_updated=datetime.now(timezone.utc).isoformat())


def save_planning_state(state: PlanningState) -> None:
    """Save the planning authority state."""
    path = _get_planning_state_path()
    state.last_updated = datetime.now(timezone.utc).isoformat()

    data = {
        "schema_version": state.schema_version,
        "plans": state.plans,
        "pending_ceo_review": state.pending_ceo_review,
        "pending_board_review": state.pending_board_review,
        "board_sessions": state.board_sessions,
        "assignments": state.assignments,
        "history": state.history,
        "last_updated": state.last_updated,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _generate_plan_id() -> str:
    """Generate a unique plan ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    import random

    suffix = f"{random.randint(0, 0xFFFF):04x}"
    return f"plan-{ts}-{suffix}"


def _generate_session_id() -> str:
    """Generate a unique board session ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    import random

    suffix = f"{random.randint(0, 0xFFFF):04x}"
    return f"board-{ts}-{suffix}"


def _generate_assignment_id() -> str:
    """Generate a unique assignment ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    import random

    suffix = f"{random.randint(0, 0xFFFF):04x}"
    return f"assign-{ts}-{suffix}"


# -----------------------------------------------------------------------------
# Plan Submission
# -----------------------------------------------------------------------------


def submit_plan(
    title: str,
    plan_type: str,
    proposed_by: str,
    size: str = "medium",
    description: str = "",
    document_path: str | None = None,
    strategic_alignment: list[str] | None = None,
    estimated_effort: str | None = None,
) -> dict:
    """
    Submit a plan for CEO review.

    Returns the created PlanSubmission as a dict.
    """
    state = load_planning_state()

    plan_id = _generate_plan_id()
    now = datetime.now(timezone.utc).isoformat()

    plan = PlanSubmission(
        plan_id=plan_id,
        title=title,
        plan_type=plan_type,
        status=PlanStatus.CEO_REVIEW.value,
        size=size,
        document_path=document_path,
        description=description,
        proposed_at=now,
        proposed_by=proposed_by,
        strategic_alignment=strategic_alignment or [],
        estimated_effort=estimated_effort,
    )

    plan_dict = asdict(plan)
    state.plans.append(plan_dict)
    state.pending_ceo_review.append(plan_id)

    # Add to history
    state.history.append(
        {
            "timestamp": now,
            "action": "plan_submitted",
            "plan_id": plan_id,
            "actor": proposed_by,
            "details": f"Plan '{title}' submitted for CEO review",
        }
    )

    save_planning_state(state)

    return plan_dict


def get_pending_ceo_review() -> list[dict]:
    """Get all plans pending CEO review."""
    state = load_planning_state()
    pending_ids = set(state.pending_ceo_review)
    return [p for p in state.plans if p.get("plan_id") in pending_ids]


def get_pending_board_review() -> list[dict]:
    """Get all plans pending board review."""
    state = load_planning_state()
    pending_ids = set(state.pending_board_review)
    return [p for p in state.plans if p.get("plan_id") in pending_ids]


def get_plan(plan_id: str) -> dict | None:
    """Get a specific plan by ID."""
    state = load_planning_state()
    for plan in state.plans:
        if plan.get("plan_id") == plan_id:
            return plan
    return None


# -----------------------------------------------------------------------------
# CEO Review
# -----------------------------------------------------------------------------


def ceo_review(
    plan_id: str,
    decision: str,
    comments: str = "",
    reviewer: str = "forge-ceo",
) -> dict:
    """
    CEO reviews a plan.

    Args:
        plan_id: The plan to review
        decision: "approve", "revise", or "reject"
        comments: Optional review comments
        reviewer: Reviewer ID (default: forge-ceo)

    Returns the updated plan dict.
    """
    state = load_planning_state()
    now = datetime.now(timezone.utc).isoformat()

    # Find the plan
    plan = None
    plan_index = -1
    for i, p in enumerate(state.plans):
        if p.get("plan_id") == plan_id:
            plan = p
            plan_index = i
            break

    if plan is None:
        return {"error": f"Plan not found: {plan_id}"}

    # Update plan
    plan["ceo_reviewed_at"] = now
    plan["ceo_decision"] = decision
    plan["ceo_comments"] = comments

    # Remove from pending CEO review
    if plan_id in state.pending_ceo_review:
        state.pending_ceo_review.remove(plan_id)

    # Determine next status
    if decision == DecisionType.APPROVE.value:
        # Check if board review required
        if _requires_board_review(plan):
            plan["status"] = PlanStatus.BOARD_REVIEW.value
            state.pending_board_review.append(plan_id)
            # Auto-schedule board session
            _schedule_board_session(state, plan_id, plan["title"])
        else:
            plan["status"] = PlanStatus.APPROVED.value
            plan["approved_at"] = now
    elif decision == DecisionType.REVISE.value:
        plan["status"] = PlanStatus.REVISION.value
    elif decision == DecisionType.REJECT.value:
        plan["status"] = PlanStatus.REJECTED.value
        plan["rejected_at"] = now
        plan["rejection_reason"] = comments

    state.plans[plan_index] = plan

    # Add to history
    state.history.append(
        {
            "timestamp": now,
            "action": f"ceo_{decision}",
            "plan_id": plan_id,
            "actor": reviewer,
            "details": f"CEO decision: {decision}"
            + (f" - {comments}" if comments else ""),
        }
    )

    save_planning_state(state)

    return plan


def _requires_board_review(plan: dict) -> bool:
    """Check if a plan requires board review based on type and size."""
    # Load config to check thresholds
    _ensure_imports()
    try:
        config_path = company_resolver.get_project_root() / "gatesmith.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            pa_config = config.get("planningAuthority", {})
            thresholds = pa_config.get("boardReviewThreshold", {})

            size_thresholds = thresholds.get("size", ["large", "epic"])
            type_thresholds = thresholds.get(
                "type", ["roadmap_phase", "vision_change", "reorg"]
            )

            plan_size = plan.get("size", "medium")
            plan_type = plan.get("plan_type", "")

            return plan_size in size_thresholds or plan_type in type_thresholds
    except Exception:
        pass

    # Default: require board review for large/epic or vision_change/reorg
    return plan.get("size") in ["large", "epic"] or plan.get("plan_type") in [
        "vision_change",
        "reorg",
    ]


def _schedule_board_session(
    state: PlanningState, plan_id: str, plan_title: str
) -> dict:
    """Auto-schedule a board session for plan review."""
    session_id = _generate_session_id()
    now = datetime.now(timezone.utc).isoformat()

    session = BoardSession(
        session_id=session_id,
        session_type="initiative_approval",
        status="scheduled",
        scheduled_at=now,
        agenda_items=[plan_id],
    )

    session_dict = asdict(session)
    state.board_sessions.append(session_dict)

    state.history.append(
        {
            "timestamp": now,
            "action": "board_session_scheduled",
            "session_id": session_id,
            "actor": "system",
            "details": f"Board session scheduled for plan: {plan_title}",
        }
    )

    return session_dict


# -----------------------------------------------------------------------------
# Board Review
# -----------------------------------------------------------------------------


def initiate_board_review(plan_id: str) -> dict:
    """Explicitly initiate a board review session for a plan."""
    state = load_planning_state()

    plan = get_plan(plan_id)
    if plan is None:
        return {"error": f"Plan not found: {plan_id}"}

    session_dict = _schedule_board_session(state, plan_id, plan.get("title", ""))
    save_planning_state(state)

    return session_dict


def record_board_decision(
    session_id: str,
    executive_id: str,
    plan_id: str,
    decision: str,
    comments: str = "",
) -> dict:
    """Record an executive's decision in a board session."""
    state = load_planning_state()
    now = datetime.now(timezone.utc).isoformat()

    # Find session
    session = None
    session_index = -1
    for i, s in enumerate(state.board_sessions):
        if s.get("session_id") == session_id:
            session = s
            session_index = i
            break

    if session is None:
        return {"error": f"Session not found: {session_id}"}

    # Record individual decision
    if "decisions" not in session:
        session["decisions"] = {}
    if plan_id not in session["decisions"]:
        session["decisions"][plan_id] = {}

    session["decisions"][plan_id][executive_id] = {
        "decision": decision,
        "comments": comments,
        "recorded_at": now,
    }

    # Track participant
    if executive_id not in session.get("actual_participants", []):
        if "actual_participants" not in session:
            session["actual_participants"] = []
        session["actual_participants"].append(executive_id)

    # Check for consensus (all required participants voted)
    required = set(session.get("required_participants", ["forge-ceo", "forge-cto"]))
    voted = set(session.get("actual_participants", []))

    if required <= voted:
        # All required participants have voted - determine consensus
        plan_decisions = session["decisions"].get(plan_id, {})
        all_approve = all(
            d.get("decision") == DecisionType.APPROVE.value
            for d in plan_decisions.values()
        )
        any_reject = any(
            d.get("decision") == DecisionType.REJECT.value
            for d in plan_decisions.values()
        )

        if any_reject:
            session["board_decision"] = DecisionType.REJECT.value
        elif all_approve:
            session["board_decision"] = DecisionType.APPROVE.value
        else:
            session["board_decision"] = DecisionType.REVISE.value

        session["status"] = "completed"
        session["completed_at"] = now

        # Update the plan
        _apply_board_decision(state, plan_id, session["board_decision"])

    state.board_sessions[session_index] = session

    state.history.append(
        {
            "timestamp": now,
            "action": "board_vote",
            "session_id": session_id,
            "actor": executive_id,
            "details": f"{executive_id} voted {decision} on {plan_id}",
        }
    )

    save_planning_state(state)

    return session


def _apply_board_decision(state: PlanningState, plan_id: str, decision: str) -> None:
    """Apply board decision to the plan."""
    now = datetime.now(timezone.utc).isoformat()

    for i, plan in enumerate(state.plans):
        if plan.get("plan_id") == plan_id:
            plan["board_reviewed_at"] = now
            plan["board_decision"] = decision

            if decision == DecisionType.APPROVE.value:
                plan["status"] = PlanStatus.APPROVED.value
                plan["approved_at"] = now
            elif decision == DecisionType.REJECT.value:
                plan["status"] = PlanStatus.REJECTED.value
                plan["rejected_at"] = now
            else:
                plan["status"] = PlanStatus.REVISION.value

            # Remove from pending board review
            if plan_id in state.pending_board_review:
                state.pending_board_review.remove(plan_id)

            state.plans[i] = plan
            break


def get_active_board_sessions() -> list[dict]:
    """Get all active (scheduled or in_progress) board sessions."""
    state = load_planning_state()
    return [
        s
        for s in state.board_sessions
        if s.get("status") in ["scheduled", "in_progress"]
    ]


# -----------------------------------------------------------------------------
# Approval / Rejection Shortcuts
# -----------------------------------------------------------------------------


def approve_plan(plan_id: str, approver: str = "forge-ceo") -> dict:
    """Shortcut to approve a plan (CEO direct approval for non-board items)."""
    plan = get_plan(plan_id)
    if plan is None:
        return {"error": f"Plan not found: {plan_id}"}

    if plan.get("status") == PlanStatus.CEO_REVIEW.value:
        return ceo_review(plan_id, DecisionType.APPROVE.value, "", approver)

    return {"error": f"Plan is not pending CEO review: status={plan.get('status')}"}


def reject_plan(plan_id: str, reason: str, rejector: str = "forge-ceo") -> dict:
    """Shortcut to reject a plan."""
    plan = get_plan(plan_id)
    if plan is None:
        return {"error": f"Plan not found: {plan_id}"}

    if plan.get("status") in [
        PlanStatus.CEO_REVIEW.value,
        PlanStatus.BOARD_REVIEW.value,
    ]:
        return ceo_review(plan_id, DecisionType.REJECT.value, reason, rejector)

    return {"error": f"Plan cannot be rejected from status: {plan.get('status')}"}


# -----------------------------------------------------------------------------
# Assignment System
# -----------------------------------------------------------------------------


def assign_goal_owner(
    goal_id: str,
    goal_name: str,
    employee_id: str,
    employee_name: str,
    assigned_by: str,
    accountability: str = "delivery",
    review_frequency: str = "weekly",
) -> dict:
    """Assign an employee as goal owner."""
    return _create_assignment(
        assignment_type=AssignmentType.GOAL_OWNER.value,
        target_id=goal_id,
        target_name=goal_name,
        employee_id=employee_id,
        employee_name=employee_name,
        assigned_by=assigned_by,
        accountability=accountability,
        review_frequency=review_frequency,
    )


def assign_phase_owner(
    phase_id: str,
    phase_name: str,
    employee_id: str,
    employee_name: str,
    assigned_by: str,
    accountability: str = "implementation",
    review_frequency: str = "daily",
) -> dict:
    """Assign an employee as phase owner."""
    return _create_assignment(
        assignment_type=AssignmentType.PHASE_OWNER.value,
        target_id=phase_id,
        target_name=phase_name,
        employee_id=employee_id,
        employee_name=employee_name,
        assigned_by=assigned_by,
        accountability=accountability,
        review_frequency=review_frequency,
    )


def _create_assignment(
    assignment_type: str,
    target_id: str,
    target_name: str,
    employee_id: str,
    employee_name: str,
    assigned_by: str,
    accountability: str,
    review_frequency: str,
) -> dict:
    """Create an assignment record."""
    state = load_planning_state()
    now = datetime.now(timezone.utc).isoformat()

    # Check for existing assignment to same target
    for a in state.assignments:
        if (
            a.get("target_id") == target_id
            and a.get("assignment_type") == assignment_type
        ):
            # Update existing assignment
            a["employee_id"] = employee_id
            a["employee_name"] = employee_name
            a["assigned_at"] = now
            a["assigned_by"] = assigned_by
            a["accountability"] = accountability
            a["review_frequency"] = review_frequency
            a["acknowledged"] = False
            a["acknowledged_at"] = None

            state.history.append(
                {
                    "timestamp": now,
                    "action": "assignment_updated",
                    "assignment_id": a.get("assignment_id"),
                    "actor": assigned_by,
                    "details": f"Reassigned {target_name} to {employee_name}",
                }
            )

            save_planning_state(state)
            return a

    # Create new assignment
    assignment_id = _generate_assignment_id()

    assignment = AssignmentRecord(
        assignment_id=assignment_id,
        assignment_type=assignment_type,
        target_id=target_id,
        target_name=target_name,
        employee_id=employee_id,
        employee_name=employee_name,
        assigned_at=now,
        assigned_by=assigned_by,
        accountability=accountability,
        review_frequency=review_frequency,
    )

    assignment_dict = asdict(assignment)
    state.assignments.append(assignment_dict)

    state.history.append(
        {
            "timestamp": now,
            "action": "assignment_created",
            "assignment_id": assignment_id,
            "actor": assigned_by,
            "details": f"Assigned {target_name} ({assignment_type}) to {employee_name}",
        }
    )

    save_planning_state(state)

    return assignment_dict


def get_assignments(employee_id: str | None = None) -> list[dict]:
    """Get all assignments, optionally filtered by employee."""
    state = load_planning_state()
    if employee_id is None:
        return state.assignments
    return [a for a in state.assignments if a.get("employee_id") == employee_id]


def get_goal_owner(goal_id: str) -> str | None:
    """Get the employee ID assigned as owner of a goal."""
    state = load_planning_state()
    for a in state.assignments:
        if (
            a.get("target_id") == goal_id
            and a.get("assignment_type") == AssignmentType.GOAL_OWNER.value
        ):
            return a.get("employee_id")
    return None


def get_phase_owner(phase_id: str) -> str | None:
    """Get the employee ID assigned as owner of a phase."""
    state = load_planning_state()
    for a in state.assignments:
        if (
            a.get("target_id") == phase_id
            and a.get("assignment_type") == AssignmentType.PHASE_OWNER.value
        ):
            return a.get("employee_id")
    return None


# -----------------------------------------------------------------------------
# Status / Reporting
# -----------------------------------------------------------------------------


def get_planning_status() -> dict:
    """Get comprehensive planning status."""
    state = load_planning_state()

    # Count plans by status
    status_counts = {}
    for plan in state.plans:
        status = plan.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    # Active board sessions
    active_sessions = [
        s
        for s in state.board_sessions
        if s.get("status") in ["scheduled", "in_progress"]
    ]

    return {
        "total_plans": len(state.plans),
        "pending_ceo_review": len(state.pending_ceo_review),
        "pending_board_review": len(state.pending_board_review),
        "status_breakdown": status_counts,
        "active_board_sessions": len(active_sessions),
        "total_assignments": len(state.assignments),
        "last_updated": state.last_updated,
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: planning_authority.py <command> [options]")
        print("\nCommands:")
        print("  submit          Submit a plan for CEO review")
        print("  review          CEO reviews a plan")
        print("  approve         Approve a plan")
        print("  reject          Reject a plan")
        print("  board-session   Initiate board session")
        print("  board-vote      Record board vote")
        print("  assign-goal     Assign goal ownership")
        print("  assign-phase    Assign phase ownership")
        print("  pending         List pending reviews")
        print("  status          Show planning status")
        print("  get             Get plan details")
        sys.exit(1)

    command = sys.argv[1]

    if command == "submit":
        # Parse args
        title = ""
        plan_type = "roadmap_phase"
        proposed_by = "unknown"
        size = "medium"
        description = ""

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--title" and i + 1 < len(sys.argv):
                title = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--type" and i + 1 < len(sys.argv):
                plan_type = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--proposed-by" and i + 1 < len(sys.argv):
                proposed_by = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--size" and i + 1 < len(sys.argv):
                size = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--description" and i + 1 < len(sys.argv):
                description = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not title:
            print("Error: --title is required", file=sys.stderr)
            sys.exit(1)

        result = submit_plan(title, plan_type, proposed_by, size, description)
        print(json.dumps(result, indent=2))

    elif command == "review":
        plan_id = ""
        decision = ""
        comments = ""
        reviewer = "forge-ceo"

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--decision" and i + 1 < len(sys.argv):
                decision = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--comments" and i + 1 < len(sys.argv):
                comments = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--reviewer" and i + 1 < len(sys.argv):
                reviewer = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not plan_id or not decision:
            print("Error: --plan-id and --decision are required", file=sys.stderr)
            sys.exit(1)

        result = ceo_review(plan_id, decision, comments, reviewer)
        print(json.dumps(result, indent=2))

    elif command == "approve":
        plan_id = ""
        approver = "forge-ceo"

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--approver" and i + 1 < len(sys.argv):
                approver = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not plan_id:
            print("Error: --plan-id is required", file=sys.stderr)
            sys.exit(1)

        result = approve_plan(plan_id, approver)
        print(json.dumps(result, indent=2))

    elif command == "reject":
        plan_id = ""
        reason = ""
        rejector = "forge-ceo"

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--reason" and i + 1 < len(sys.argv):
                reason = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--rejector" and i + 1 < len(sys.argv):
                rejector = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not plan_id or not reason:
            print("Error: --plan-id and --reason are required", file=sys.stderr)
            sys.exit(1)

        result = reject_plan(plan_id, reason, rejector)
        print(json.dumps(result, indent=2))

    elif command == "board-session":
        plan_id = ""

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not plan_id:
            print("Error: --plan-id is required", file=sys.stderr)
            sys.exit(1)

        result = initiate_board_review(plan_id)
        print(json.dumps(result, indent=2))

    elif command == "board-vote":
        session_id = ""
        executive_id = ""
        plan_id = ""
        decision = ""
        comments = ""

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--session-id" and i + 1 < len(sys.argv):
                session_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--executive-id" and i + 1 < len(sys.argv):
                executive_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--decision" and i + 1 < len(sys.argv):
                decision = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--comments" and i + 1 < len(sys.argv):
                comments = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not session_id or not executive_id or not plan_id or not decision:
            print(
                "Error: --session-id, --executive-id, --plan-id, --decision are required",
                file=sys.stderr,
            )
            sys.exit(1)

        result = record_board_decision(
            session_id, executive_id, plan_id, decision, comments
        )
        print(json.dumps(result, indent=2))

    elif command == "assign-goal":
        goal_id = ""
        goal_name = ""
        employee_id = ""
        employee_name = ""
        assigned_by = "forge-ceo"

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--goal-id" and i + 1 < len(sys.argv):
                goal_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--goal-name" and i + 1 < len(sys.argv):
                goal_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--employee-id" and i + 1 < len(sys.argv):
                employee_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--employee-name" and i + 1 < len(sys.argv):
                employee_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--assigned-by" and i + 1 < len(sys.argv):
                assigned_by = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not goal_id or not employee_id:
            print("Error: --goal-id and --employee-id are required", file=sys.stderr)
            sys.exit(1)

        result = assign_goal_owner(
            goal_id,
            goal_name or goal_id,
            employee_id,
            employee_name or employee_id,
            assigned_by,
        )
        print(json.dumps(result, indent=2))

    elif command == "assign-phase":
        phase_id = ""
        phase_name = ""
        employee_id = ""
        employee_name = ""
        assigned_by = "forge-ceo"

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--phase-id" and i + 1 < len(sys.argv):
                phase_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--phase-name" and i + 1 < len(sys.argv):
                phase_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--employee-id" and i + 1 < len(sys.argv):
                employee_id = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--employee-name" and i + 1 < len(sys.argv):
                employee_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--assigned-by" and i + 1 < len(sys.argv):
                assigned_by = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not phase_id or not employee_id:
            print("Error: --phase-id and --employee-id are required", file=sys.stderr)
            sys.exit(1)

        result = assign_phase_owner(
            phase_id,
            phase_name or phase_id,
            employee_id,
            employee_name or employee_id,
            assigned_by,
        )
        print(json.dumps(result, indent=2))

    elif command == "pending":
        ceo_pending = get_pending_ceo_review()
        board_pending = get_pending_board_review()
        print(
            json.dumps(
                {
                    "pending_ceo_review": ceo_pending,
                    "pending_board_review": board_pending,
                },
                indent=2,
            )
        )

    elif command == "status":
        result = get_planning_status()
        print(json.dumps(result, indent=2))

    elif command == "get":
        plan_id = ""

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--plan-id" and i + 1 < len(sys.argv):
                plan_id = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not plan_id:
            print("Error: --plan-id is required", file=sys.stderr)
            sys.exit(1)

        result = get_plan(plan_id)
        if result:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"error": f"Plan not found: {plan_id}"}))

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
