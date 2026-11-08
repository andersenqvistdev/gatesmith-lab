#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P16 Self-Organization — Autonomous organizational restructuring capabilities.

This module enables the company to self-organize based on performance metrics:
1. Employee promotion/demotion based on efficiency scores
2. Budget reallocation based on project ROI
3. Hiring triggers based on capability gaps
4. Consultant → Employee conversion
5. Department restructuring proposals

All changes are proposals that can be auto-approved or require human approval
based on risk level.

Usage:
    # Analyze workforce and generate proposals
    python p16_features.py analyze

    # Evaluate specific employee for promotion/demotion
    python p16_features.py evaluate-employee <employee-id>

    # Check for capability gaps
    python p16_features.py capability-gaps

    # Generate reorg proposals
    python p16_features.py proposals

    # Auto-execute approved proposals
    python p16_features.py execute --auto-approved
"""

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# Lazy imports for sibling modules
company_resolver = None
efficiency_tracker = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver, efficiency_tracker
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import efficiency_tracker as et

        company_resolver = cr
        efficiency_tracker = et
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import efficiency_tracker as et  # type: ignore[no-redef]

        company_resolver = cr
        efficiency_tracker = et


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ORG_FILE = "org.json"
REORG_STATE_FILE = "reorg_state.json"

# Efficiency thresholds for actions
PROMOTION_THRESHOLD = 100.0  # Efficiency score above this triggers promotion evaluation
DEMOTION_THRESHOLD = 5.0  # Efficiency score below this triggers demotion evaluation
CONVERSION_THRESHOLD = (
    50.0  # Consultant efficiency above this triggers conversion proposal
)
MIN_TASKS_FOR_EVALUATION = 5  # Minimum tasks before evaluating


class ProposalType(str, Enum):
    """Types of organizational change proposals."""

    PROMOTE = "promote"
    DEMOTE = "demote"
    HIRE = "hire"
    CONVERT_CONSULTANT = "convert_consultant"
    REALLOCATE_BUDGET = "reallocate_budget"
    CREATE_DEPARTMENT = "create_department"
    ARCHIVE_DEPARTMENT = "archive_department"
    REASSIGN_EMPLOYEE = "reassign_employee"


class ApprovalLevel(str, Enum):
    """Approval levels for proposals."""

    AUTO = "auto"  # Can be auto-approved based on config
    MANAGER = "manager"  # Requires department head approval
    EXECUTIVE = "executive"  # Requires CEO/CTO approval
    HUMAN = "human"  # Always requires human approval


@dataclass
class ReorgProposal:
    """A proposal for organizational change."""

    id: str
    type: ProposalType
    title: str
    description: str
    rationale: str
    target_id: str  # Employee ID, department ID, etc.
    target_name: str
    data: dict  # Type-specific data
    approval_level: ApprovalLevel
    estimated_impact: str  # "low", "medium", "high"
    confidence: float  # 0.0 - 1.0
    created_at: str
    status: str = "pending"  # pending, approved, rejected, executed
    approved_at: str | None = None
    approved_by: str | None = None
    executed_at: str | None = None
    rejection_reason: str | None = None


# -----------------------------------------------------------------------------
# Core Functions
# -----------------------------------------------------------------------------


def load_org(company_dir: Path | None = None) -> dict:
    """Load organization data."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    org_path = company_dir / ORG_FILE
    if not org_path.exists():
        return {"company": {}, "employees": [], "economics": {}}

    with open(org_path) as f:
        org = json.load(f)
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    # Import the real module locally rather than the module-global
    # `company_resolver` so a test that mocks the global still normalizes.
    try:
        from . import company_resolver as _cr
    except ImportError:
        import company_resolver as _cr  # type: ignore[no-redef]
    return _cr.normalize_org_employees(org, company_dir)


def save_org(org: dict, company_dir: Path | None = None) -> None:
    """Save organization data atomically."""
    import os
    import tempfile

    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    org_path = company_dir / ORG_FILE

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=company_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, org_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_reorg_state(company_dir: Path | None = None) -> dict:
    """Load reorg state (pending proposals, history)."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state_path = company_dir / REORG_STATE_FILE
    if not state_path.exists():
        return {
            "pending_proposals": [],
            "executed_proposals": [],
            "rejected_proposals": [],
            "last_analysis": None,
            "version": "1.0",
        }

    with open(state_path) as f:
        return json.load(f)


def save_reorg_state(state: dict, company_dir: Path | None = None) -> None:
    """Save reorg state atomically."""
    import os
    import tempfile

    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state_path = company_dir / REORG_STATE_FILE

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=company_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, state_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# -----------------------------------------------------------------------------
# Employee Analysis
# -----------------------------------------------------------------------------


def evaluate_employee_for_promotion(employee: dict) -> ReorgProposal | None:
    """Evaluate if an employee should be promoted based on efficiency."""
    efficiency = employee.get("efficiency", {})
    score = efficiency.get("score", 0.0)
    tasks_completed = efficiency.get("tasks_completed", 0)
    first_pass_rate = efficiency.get("first_pass_success_rate", 0.0)

    # Need minimum tasks for evaluation
    if tasks_completed < MIN_TASKS_FOR_EVALUATION:
        return None

    # Check for promotion criteria
    if score >= PROMOTION_THRESHOLD and first_pass_rate >= 0.8:
        current_role = _get_employee_role(employee)
        new_role = _get_promotion_role(current_role)

        if new_role is None:
            return None  # Already at highest role

        return ReorgProposal(
            id=f"promo-{employee['id']}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            type=ProposalType.PROMOTE,
            title=f"Promote {employee['name']} to {new_role}",
            description=f"Based on exceptional performance metrics, {employee['name']} "
            f"is recommended for promotion from {current_role} to {new_role}.",
            rationale=f"Efficiency score: {score:.1f} (threshold: {PROMOTION_THRESHOLD}), "
            f"First-pass success rate: {first_pass_rate * 100:.0f}% (required: 80%), "
            f"Tasks completed: {tasks_completed}",
            target_id=employee["id"],
            target_name=employee["name"],
            data={
                "current_role": current_role,
                "new_role": new_role,
                "efficiency_score": score,
                "tasks_completed": tasks_completed,
                "first_pass_rate": first_pass_rate,
            },
            approval_level=ApprovalLevel.MANAGER,
            estimated_impact="medium",
            confidence=min(0.9, 0.5 + (score / 200)),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    return None


def evaluate_employee_for_demotion(employee: dict) -> ReorgProposal | None:
    """Evaluate if an employee should be demoted based on poor efficiency."""
    efficiency = employee.get("efficiency", {})
    score = efficiency.get("score", 0.0)
    tasks_completed = efficiency.get("tasks_completed", 0)
    first_pass_rate = efficiency.get("first_pass_success_rate", 0.0)
    improvement_areas = efficiency.get("improvement_areas", [])

    # Need minimum tasks for evaluation
    if tasks_completed < MIN_TASKS_FOR_EVALUATION:
        return None

    # Check for demotion criteria
    if score < DEMOTION_THRESHOLD and first_pass_rate < 0.5:
        current_role = _get_employee_role(employee)
        new_role = _get_demotion_role(current_role)

        if new_role is None:
            return None  # Already at lowest role

        return ReorgProposal(
            id=f"demote-{employee['id']}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            type=ProposalType.DEMOTE,
            title=f"Demote {employee['name']} to {new_role}",
            description=f"Due to sustained underperformance, {employee['name']} "
            f"may need role adjustment from {current_role} to {new_role}.",
            rationale=f"Efficiency score: {score:.1f} (below threshold: {DEMOTION_THRESHOLD}), "
            f"First-pass success rate: {first_pass_rate * 100:.0f}% (below 50%), "
            f"Improvement areas: {', '.join(improvement_areas) or 'multiple'}",
            target_id=employee["id"],
            target_name=employee["name"],
            data={
                "current_role": current_role,
                "new_role": new_role,
                "efficiency_score": score,
                "tasks_completed": tasks_completed,
                "first_pass_rate": first_pass_rate,
                "improvement_areas": improvement_areas,
            },
            approval_level=ApprovalLevel.EXECUTIVE,  # Demotions need executive approval
            estimated_impact="high",
            confidence=0.6,  # Lower confidence - needs human judgment
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    return None


def _get_employee_role(employee: dict) -> str:
    """Get employee's current role based on department/team position."""
    dept = employee.get("department", "")
    team = employee.get("team")

    if dept == "executive":
        return "executive"
    if team is None:
        return "department-head"
    return "member"


def _get_promotion_role(current_role: str) -> str | None:
    """Get the next role up from current role."""
    promotion_path = {
        "member": "senior",
        "senior": "team-lead",
        "team-lead": "department-head",
    }
    return promotion_path.get(current_role)


def _get_demotion_role(current_role: str) -> str | None:
    """Get the next role down from current role."""
    demotion_path = {
        "department-head": "team-lead",
        "team-lead": "senior",
        "senior": "member",
    }
    return demotion_path.get(current_role)


# -----------------------------------------------------------------------------
# Capability Gap Analysis
# -----------------------------------------------------------------------------


def analyze_capability_gaps(
    org: dict, work_queue: dict | None = None
) -> list[ReorgProposal]:
    """Analyze capability gaps and generate hiring proposals."""
    proposals = []
    employees = org.get("employees", [])

    # Collect all capabilities across employees
    available_capabilities = set()
    for emp in employees:
        if emp.get("status") == "available":
            available_capabilities.update(emp.get("capabilities", []))

    # If we have a work queue, check for capability mismatches
    if work_queue:
        required_capabilities = set()
        for task in work_queue.get("tasks", []):
            if task.get("status") == "pending":
                required_capabilities.update(task.get("required_capabilities", []))

        missing = required_capabilities - available_capabilities

        for cap in missing:
            proposals.append(
                ReorgProposal(
                    id=f"hire-{cap}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    type=ProposalType.HIRE,
                    title=f"Hire specialist with '{cap}' capability",
                    description=f"Work queue contains tasks requiring '{cap}' capability "
                    f"but no available employee has this skill.",
                    rationale=f"Capability gap detected: '{cap}' is required by pending tasks "
                    f"but not present in the workforce.",
                    target_id=cap,
                    target_name=cap,
                    data={
                        "missing_capability": cap,
                        "pending_tasks_count": len(
                            [
                                t
                                for t in work_queue.get("tasks", [])
                                if cap in t.get("required_capabilities", [])
                            ]
                        ),
                    },
                    approval_level=ApprovalLevel.EXECUTIVE,
                    estimated_impact="high",
                    confidence=0.8,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    # Check for overloaded capabilities (many tasks, few employees)
    capability_coverage = {}
    for emp in employees:
        if emp.get("status") == "available":
            for cap in emp.get("capabilities", []):
                capability_coverage[cap] = capability_coverage.get(cap, 0) + 1

    for cap, count in capability_coverage.items():
        if count == 1:
            # Single point of failure
            proposals.append(
                ReorgProposal(
                    id=f"hire-backup-{cap}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    type=ProposalType.HIRE,
                    title=f"Hire backup for '{cap}' capability",
                    description=f"Only one employee has '{cap}' capability. "
                    f"Hiring a backup would prevent single-point-of-failure.",
                    rationale=f"Capability '{cap}' has only 1 employee coverage. "
                    f"Bus factor is 1 - a single employee leaving would create a gap.",
                    target_id=cap,
                    target_name=cap,
                    data={
                        "capability": cap,
                        "current_coverage": count,
                        "recommended_coverage": 2,
                    },
                    approval_level=ApprovalLevel.MANAGER,
                    estimated_impact="medium",
                    confidence=0.6,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    return proposals


# -----------------------------------------------------------------------------
# Consultant Conversion
# -----------------------------------------------------------------------------


def evaluate_consultant_conversion(employee: dict) -> ReorgProposal | None:
    """Evaluate if a consultant should be converted to full employee."""
    # Only evaluate consultants
    if employee.get("type") != "consultant":
        return None

    efficiency = employee.get("efficiency", {})
    score = efficiency.get("score", 0.0)
    tasks_completed = efficiency.get("tasks_completed", 0)
    first_pass_rate = efficiency.get("first_pass_success_rate", 0.0)

    # Check conversion criteria
    if (
        score >= CONVERSION_THRESHOLD
        and tasks_completed >= MIN_TASKS_FOR_EVALUATION
        and first_pass_rate >= 0.7
    ):
        return ReorgProposal(
            id=f"convert-{employee['id']}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            type=ProposalType.CONVERT_CONSULTANT,
            title=f"Convert {employee['name']} to full employee",
            description=f"Consultant {employee['name']} has demonstrated consistent "
            f"high performance and may be valuable as a permanent employee.",
            rationale=f"Efficiency score: {score:.1f} (threshold: {CONVERSION_THRESHOLD}), "
            f"First-pass success rate: {first_pass_rate * 100:.0f}%, "
            f"Tasks completed: {tasks_completed}",
            target_id=employee["id"],
            target_name=employee["name"],
            data={
                "current_type": "consultant",
                "new_type": "persistent",
                "efficiency_score": score,
                "tasks_completed": tasks_completed,
                "capabilities": employee.get("capabilities", []),
            },
            approval_level=ApprovalLevel.EXECUTIVE,
            estimated_impact="medium",
            confidence=0.75,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    return None


# -----------------------------------------------------------------------------
# Budget Reallocation
# -----------------------------------------------------------------------------


def analyze_budget_reallocation(org: dict) -> list[ReorgProposal]:
    """Analyze project performance and suggest budget reallocations."""
    proposals = []
    org.get("economics", {})

    # This would integrate with p18_features.py revenue/ROI data
    # For now, we'll use efficiency data as a proxy

    # Group employees by department and calculate average efficiency
    dept_efficiency = {}
    for emp in org.get("employees", []):
        dept = emp.get("department", "unknown")
        score = emp.get("efficiency", {}).get("score", 0.0)
        if dept not in dept_efficiency:
            dept_efficiency[dept] = {"total": 0.0, "count": 0}
        dept_efficiency[dept]["total"] += score
        dept_efficiency[dept]["count"] += 1

    # Calculate averages
    dept_averages = {}
    for dept, data in dept_efficiency.items():
        if data["count"] > 0:
            dept_averages[dept] = data["total"] / data["count"]

    # Find underperforming departments
    if dept_averages:
        avg_across_all = sum(dept_averages.values()) / len(dept_averages)

        for dept, avg in dept_averages.items():
            if avg < avg_across_all * 0.5:  # 50% below average
                proposals.append(
                    ReorgProposal(
                        id=f"budget-{dept}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                        type=ProposalType.REALLOCATE_BUDGET,
                        title=f"Review budget allocation for {dept}",
                        description=f"Department '{dept}' has significantly lower efficiency "
                        f"than company average. Budget review recommended.",
                        rationale=f"Department average efficiency: {avg:.1f}, "
                        f"Company average: {avg_across_all:.1f}",
                        target_id=dept,
                        target_name=dept,
                        data={
                            "department": dept,
                            "department_avg": avg,
                            "company_avg": avg_across_all,
                            "underperformance_ratio": avg / avg_across_all
                            if avg_across_all > 0
                            else 0,
                        },
                        approval_level=ApprovalLevel.EXECUTIVE,
                        estimated_impact="high",
                        confidence=0.5,  # Needs human judgment
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )

    return proposals


# -----------------------------------------------------------------------------
# Main Analysis
# -----------------------------------------------------------------------------


def run_full_analysis(company_dir: Path | None = None) -> list[ReorgProposal]:
    """Run full organizational analysis and generate all proposals."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    org = load_org(company_dir)
    proposals = []

    # Analyze each employee
    for employee in org.get("employees", []):
        # Promotion check
        promo = evaluate_employee_for_promotion(employee)
        if promo:
            proposals.append(promo)

        # Demotion check
        demote = evaluate_employee_for_demotion(employee)
        if demote:
            proposals.append(demote)

        # Consultant conversion check
        convert = evaluate_consultant_conversion(employee)
        if convert:
            proposals.append(convert)

    # Capability gap analysis
    try:
        work_queue_path = company_dir / "state/work_queue.json"
        if work_queue_path.exists():
            with open(work_queue_path) as f:
                work_queue = json.load(f)
        else:
            work_queue = None
    except Exception:
        work_queue = None

    gap_proposals = analyze_capability_gaps(org, work_queue)
    proposals.extend(gap_proposals)

    # Budget reallocation analysis
    budget_proposals = analyze_budget_reallocation(org)
    proposals.extend(budget_proposals)

    # Save state
    state = load_reorg_state(company_dir)

    # Add new proposals (avoid duplicates by ID prefix)
    existing_ids = {
        p["id"].split("-")[0] + "-" + p["id"].split("-")[1]
        for p in state.get("pending_proposals", [])
    }

    for proposal in proposals:
        prefix = "-".join(proposal.id.split("-")[:2])
        if prefix not in existing_ids:
            state["pending_proposals"].append(asdict(proposal))

    state["last_analysis"] = datetime.now(timezone.utc).isoformat()
    save_reorg_state(state, company_dir)

    return proposals


def execute_proposal(
    proposal_id: str, approved_by: str = "system", company_dir: Path | None = None
) -> dict:
    """Execute an approved proposal."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_reorg_state(company_dir)

    # Find the proposal
    proposal = None
    proposal_idx = None
    for idx, p in enumerate(state.get("pending_proposals", [])):
        if p["id"] == proposal_id:
            proposal = p
            proposal_idx = idx
            break

    if proposal is None:
        return {"success": False, "error": f"Proposal {proposal_id} not found"}

    org = load_org(company_dir)

    # Execute based on type
    result = {"success": False}
    proposal_type = proposal["type"]

    if proposal_type == ProposalType.PROMOTE.value:
        result = _execute_promotion(proposal, org)
    elif proposal_type == ProposalType.DEMOTE.value:
        result = _execute_demotion(proposal, org)
    elif proposal_type == ProposalType.CONVERT_CONSULTANT.value:
        result = _execute_conversion(proposal, org)
    elif proposal_type == ProposalType.HIRE.value:
        # Hiring creates a task, doesn't execute directly
        result = {
            "success": True,
            "action": "task_created",
            "message": f"Hiring task created for capability: {proposal['target_name']}",
        }
    elif proposal_type == ProposalType.REALLOCATE_BUDGET.value:
        # Budget reallocation requires manual review
        result = {
            "success": True,
            "action": "flagged_for_review",
            "message": f"Budget review flagged for department: {proposal['target_name']}",
        }
    else:
        result = {"success": False, "error": f"Unknown proposal type: {proposal_type}"}

    if result.get("success"):
        # Update proposal status
        proposal["status"] = "executed"
        proposal["executed_at"] = datetime.now(timezone.utc).isoformat()
        proposal["approved_by"] = approved_by

        # Move to executed
        state["executed_proposals"].append(proposal)
        state["pending_proposals"].pop(proposal_idx)

        save_reorg_state(state, company_dir)
        if result.get("org_updated"):
            save_org(org, company_dir)

    return result


def _execute_promotion(proposal: dict, org: dict) -> dict:
    """Execute a promotion proposal."""
    employee_id = proposal["target_id"]
    new_role = proposal["data"]["new_role"]

    for emp in org.get("employees", []):
        if emp["id"] == employee_id:
            # Update role in efficiency metadata
            emp.setdefault("efficiency", {})
            emp["efficiency"]["role"] = new_role
            emp["efficiency"]["last_promotion"] = datetime.now(timezone.utc).isoformat()

            # Add to memory/learnings
            emp.setdefault("promotions", [])
            emp["promotions"].append(
                {
                    "from": proposal["data"]["current_role"],
                    "to": new_role,
                    "date": datetime.now(timezone.utc).isoformat(),
                    "reason": proposal["rationale"],
                }
            )

            return {
                "success": True,
                "org_updated": True,
                "message": f"Promoted {emp['name']} to {new_role}",
            }

    return {"success": False, "error": f"Employee {employee_id} not found"}


def _execute_demotion(proposal: dict, org: dict) -> dict:
    """Execute a demotion proposal."""
    employee_id = proposal["target_id"]
    new_role = proposal["data"]["new_role"]

    for emp in org.get("employees", []):
        if emp["id"] == employee_id:
            # Update role in efficiency metadata
            emp.setdefault("efficiency", {})
            emp["efficiency"]["role"] = new_role
            emp["efficiency"]["last_demotion"] = datetime.now(timezone.utc).isoformat()

            return {
                "success": True,
                "org_updated": True,
                "message": f"Changed {emp['name']} role to {new_role}",
            }

    return {"success": False, "error": f"Employee {employee_id} not found"}


def _execute_conversion(proposal: dict, org: dict) -> dict:
    """Execute a consultant-to-employee conversion."""
    employee_id = proposal["target_id"]

    for emp in org.get("employees", []):
        if emp["id"] == employee_id:
            emp["type"] = "persistent"
            emp["convertedAt"] = datetime.now(timezone.utc).isoformat()
            emp["convertedFrom"] = "consultant"

            return {
                "success": True,
                "org_updated": True,
                "message": f"Converted {emp['name']} from consultant to employee",
            }

    return {"success": False, "error": f"Employee {employee_id} not found"}


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="P16 Self-Organization features")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # analyze command
    analyze_parser = subparsers.add_parser(
        "analyze", help="Run full organizational analysis"
    )
    analyze_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # evaluate-employee command
    eval_parser = subparsers.add_parser(
        "evaluate-employee", help="Evaluate specific employee"
    )
    eval_parser.add_argument("employee_id", help="Employee ID to evaluate")
    eval_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # capability-gaps command
    gaps_parser = subparsers.add_parser(
        "capability-gaps", help="Check for capability gaps"
    )
    gaps_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # proposals command
    proposals_parser = subparsers.add_parser("proposals", help="List pending proposals")
    proposals_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # execute command
    execute_parser = subparsers.add_parser("execute", help="Execute proposals")
    execute_parser.add_argument("--proposal-id", help="Specific proposal ID to execute")
    execute_parser.add_argument(
        "--auto-approved",
        action="store_true",
        help="Execute all auto-approved proposals",
    )
    execute_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    _ensure_imports()
    company_dir = company_resolver.get_company_dir()

    if args.command == "analyze":
        proposals = run_full_analysis(company_dir)

        if args.json:
            print(
                json.dumps(
                    [
                        asdict(p) if hasattr(p, "__dataclass_fields__") else p
                        for p in proposals
                    ],
                    indent=2,
                )
            )
        else:
            print(f"\n{'=' * 60}")
            print(" ORGANIZATIONAL ANALYSIS")
            print(f"{'=' * 60}\n")
            print(f"Generated {len(proposals)} proposals:\n")

            for p in proposals:
                if isinstance(p, dict):
                    print(f"  [{p['type'].upper()}] {p['title']}")
                    print(
                        f"    Approval: {p['approval_level']} | Impact: {p['estimated_impact']}"
                    )
                    print(f"    Confidence: {p['confidence'] * 100:.0f}%")
                else:
                    print(f"  [{p.type.value.upper()}] {p.title}")
                    print(
                        f"    Approval: {p.approval_level.value} | Impact: {p.estimated_impact}"
                    )
                    print(f"    Confidence: {p.confidence * 100:.0f}%")
                print()

    elif args.command == "evaluate-employee":
        org = load_org(company_dir)
        employee = None
        for emp in org.get("employees", []):
            if emp["id"] == args.employee_id:
                employee = emp
                break

        if employee is None:
            print(f"Employee {args.employee_id} not found")
            sys.exit(1)

        proposals = []
        promo = evaluate_employee_for_promotion(employee)
        if promo:
            proposals.append(promo)
        demote = evaluate_employee_for_demotion(employee)
        if demote:
            proposals.append(demote)
        convert = evaluate_consultant_conversion(employee)
        if convert:
            proposals.append(convert)

        if args.json:
            print(json.dumps([asdict(p) for p in proposals], indent=2))
        else:
            print(f"\nEvaluating {employee['name']}:")
            print(f"  Efficiency: {employee.get('efficiency', {}).get('score', 0):.1f}")
            print(
                f"  Tasks: {employee.get('efficiency', {}).get('tasks_completed', 0)}"
            )
            print(f"  Proposals: {len(proposals)}")
            for p in proposals:
                print(f"    - {p.title}")

    elif args.command == "capability-gaps":
        org = load_org(company_dir)
        proposals = analyze_capability_gaps(org)

        if args.json:
            print(json.dumps([asdict(p) for p in proposals], indent=2))
        else:
            print(f"\nCapability Gap Analysis: {len(proposals)} gaps found")
            for p in proposals:
                print(f"  - {p.title}: {p.rationale}")

    elif args.command == "proposals":
        state = load_reorg_state(company_dir)
        pending = state.get("pending_proposals", [])

        if args.json:
            print(json.dumps(pending, indent=2))
        else:
            print(f"\n{'=' * 60}")
            print(f" PENDING PROPOSALS ({len(pending)})")
            print(f"{'=' * 60}\n")

            for p in pending:
                print(f"  ID: {p['id']}")
                print(f"  Type: {p['type']}")
                print(f"  Title: {p['title']}")
                print(f"  Approval: {p['approval_level']}")
                print(f"  Status: {p['status']}")
                print()

    elif args.command == "execute":
        if args.proposal_id:
            result = execute_proposal(args.proposal_id, company_dir=company_dir)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                if result.get("success"):
                    print(f"Executed: {result.get('message', 'Success')}")
                else:
                    print(f"Failed: {result.get('error', 'Unknown error')}")

        elif args.auto_approved:
            state = load_reorg_state(company_dir)
            executed = []
            for p in state.get("pending_proposals", []):
                if p["approval_level"] == ApprovalLevel.AUTO.value:
                    result = execute_proposal(p["id"], company_dir=company_dir)
                    executed.append({"id": p["id"], "result": result})

            if args.json:
                print(json.dumps(executed, indent=2))
            else:
                print(f"Executed {len(executed)} auto-approved proposals")
                for e in executed:
                    print(
                        f"  - {e['id']}: {e['result'].get('message', e['result'].get('error'))}"
                    )

        else:
            print("Specify --proposal-id or --auto-approved")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
