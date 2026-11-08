# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
P21: Board Governance Module

Provides board governance functions including:
- Board creation with domain expertise
- Dynamic advisor management
- Governance checks for hiring and investments
- Board session integration
- Budget-conscious expansion planning
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Configuration ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"
ORG_PATH = COMPANY_DIR / "org.json"
CONFIG_PATH = PROJECT_ROOT / ".claude" / "gatesmith.json"


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class BoardChair:
    """Board Chair - external advisor with market expertise."""

    id: str
    name: str
    type: str  # always "external_advisor"
    expertise: list[str]
    status: str = "active"


@dataclass
class BoardMember:
    """Internal board member (executive)."""

    id: str
    role: str  # "executive" or "advisor"
    voting: bool = True


@dataclass
class ExternalAdvisor:
    """Dynamically added external advisor."""

    id: str
    name: str
    advisor_type: (
        str  # "industry-expert", "customer-advocate", "investor-rep", "domain-advisor"
    )
    expertise: str
    added_at: str
    status: str = "active"


@dataclass
class Board:
    """Full board structure."""

    domain: str
    created_at: str
    chair: BoardChair | None
    members: list[BoardMember]
    external_advisors: list[ExternalAdvisor]
    quorum: list[str]
    max_size: int = 7


@dataclass
class GovernanceCheck:
    """Result of a governance check."""

    requires_board: bool
    required_participants: list[str]
    decision_type: str
    reason: str


# ── Domain Expertise Mapping ──────────────────────────────────────────────────

DOMAIN_EXPERTISE = {
    "saas_platform": {
        "chair_expertise": ["saas-gtm", "subscription-economics", "b2b-growth"],
        "advisor_expertise": [
            "product-led-growth",
            "enterprise-sales",
            "churn-reduction",
        ],
        "industry_focus": "Software-as-a-Service",
    },
    "ecommerce": {
        "chair_expertise": [
            "retail-dynamics",
            "marketplace-economics",
            "customer-acquisition",
        ],
        "advisor_expertise": ["payments", "logistics", "conversion-optimization"],
        "industry_focus": "E-commerce/Retail",
    },
    "mobile_app": {
        "chair_expertise": ["mobile-ux", "app-store-optimization", "mobile-growth"],
        "advisor_expertise": ["user-retention", "monetization", "engagement"],
        "industry_focus": "Mobile Applications",
    },
    "api_service": {
        "chair_expertise": ["developer-relations", "api-ecosystems", "developer-tools"],
        "advisor_expertise": ["developer-experience", "sdk-design", "documentation"],
        "industry_focus": "Developer Tools/APIs",
    },
    "data_platform": {
        "chair_expertise": ["data-economics", "ml-operations", "analytics-strategy"],
        "advisor_expertise": ["data-privacy", "data-governance", "compliance"],
        "industry_focus": "Data/Analytics",
    },
    "content_platform": {
        "chair_expertise": ["content-strategy", "media-economics", "platform-growth"],
        "advisor_expertise": [
            "creator-economy",
            "engagement-optimization",
            "moderation",
        ],
        "industry_focus": "Content/Media",
    },
    "agency": {
        "chair_expertise": ["client-services", "project-economics", "service-scaling"],
        "advisor_expertise": [
            "utilization-optimization",
            "margin-management",
            "talent",
        ],
        "industry_focus": "Professional Services",
    },
    "minimal": {
        "chair_expertise": ["general-business", "startup-operations"],
        "advisor_expertise": [],
        "industry_focus": "General",
    },
}


# ── Helper Functions ──────────────────────────────────────────────────────────


def _load_org() -> dict[str, Any]:
    """Load org.json (employees normalized to dict records).

    A fresh /company-bootstrap can persist bare-string employees; normalize
    through the canonical company_resolver.normalize_org_employees so board
    governance never crashes on emp.get(...) (ProjectK root-cause fix).
    """
    if not ORG_PATH.exists():
        return {}
    org = json.loads(ORG_PATH.read_text(encoding="utf-8"))
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, ORG_PATH.parent)


def _save_org(org: dict[str, Any]) -> None:
    """Save org.json using atomic write pattern.

    Uses ORG_PATH (which can be patched in tests) rather than company_resolver.save_org
    so tests can properly isolate writes using mock paths.

    Safety: Refuses to save if it would wipe existing employees.
    """
    import os as _os
    import sys
    import tempfile

    ORG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Safety check: Don't wipe employees if file already has them
    if ORG_PATH.exists():
        try:
            existing = json.loads(ORG_PATH.read_text(encoding="utf-8"))
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            # Block saves that would wipe existing employees
            if len(existing_employees) > 0 and len(new_employees) == 0:
                print(
                    f"[SAFETY] Blocked _save_org: Would wipe {len(existing_employees)} employees. "
                    "This is likely a bug in the calling code.",
                    file=sys.stderr,
                )
                return  # Refuse to save
        except (json.JSONDecodeError, OSError):
            # If we can't read existing file and trying to save empty employees, block
            if len(org.get("employees", [])) == 0:
                print(
                    "[SAFETY] Blocked _save_org: Cannot read existing file and new data has no employees. "
                    "This could cause data loss.",
                    file=sys.stderr,
                )
                return  # Refuse to save

    # Atomic write: temp file + replace prevents truncation races
    fd, tmp_path = tempfile.mkstemp(
        dir=str(ORG_PATH.parent), prefix=".org_", suffix=".tmp"
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        _os.replace(tmp_path, str(ORG_PATH))
    except Exception:
        if _os.path.exists(tmp_path):
            _os.unlink(tmp_path)
        raise


def _load_config() -> dict[str, Any]:
    """Load gatesmith.json."""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _generate_advisor_id(advisor_type: str, expertise: str) -> str:
    """Generate unique advisor ID."""
    base = f"{advisor_type}-{expertise[:20]}".lower().replace(" ", "-").replace(",", "")
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"{base}-{timestamp}"


# ── Board Creation ────────────────────────────────────────────────────────────


def get_domain_expertise_for_template(template: str) -> dict[str, Any]:
    """Get domain expertise mapping for a template."""
    return DOMAIN_EXPERTISE.get(template, DOMAIN_EXPERTISE["minimal"])


def create_board(
    domain: str, chair_expertise: list[str] | None = None
) -> dict[str, Any]:
    """
    Create a new board structure for the organization.

    Args:
        domain: Domain template (saas_platform, ecommerce, etc.)
        chair_expertise: Optional override for chair expertise

    Returns:
        Created board structure
    """
    org = _load_org()

    if "board" in org and org["board"].get("chair"):
        return {
            "success": False,
            "error": "Board already exists",
            "board": org["board"],
        }

    # Get domain expertise
    expertise = get_domain_expertise_for_template(domain)
    final_chair_expertise = chair_expertise or expertise["chair_expertise"]

    # Create board structure
    board = {
        "domain": domain,
        "created_at": _now_iso(),
        "chair": {
            "id": "board-chair",
            "name": "Board Chair",
            "type": "external_advisor",
            "expertise": final_chair_expertise,
            "status": "active",
        },
        "members": [
            {"id": "forge-ceo", "role": "executive", "voting": True},
            {"id": "forge-cto", "role": "executive", "voting": True},
        ],
        "external_advisors": [],
        "quorum": ["board-chair", "forge-ceo"],
        "max_size": 7,
    }

    # Optionally create domain advisor if template has advisor expertise
    if expertise["advisor_expertise"]:
        domain_advisor = {
            "id": f"domain-advisor-{domain}",
            "name": f"Domain Advisor ({expertise['industry_focus']})",
            "type": "domain-advisor",
            "expertise": ", ".join(expertise["advisor_expertise"]),
            "added_at": _now_iso(),
            "status": "active",
        }
        board["external_advisors"].append(domain_advisor)

    # Save to org.json
    org["board"] = board
    org["version"] = "2.4"
    _save_org(org)

    return {
        "success": True,
        "board": board,
        "domain": domain,
        "industry_focus": expertise["industry_focus"],
    }


def get_board_composition() -> dict[str, Any]:
    """Get current board composition."""
    org = _load_org()

    if "board" not in org:
        return {"exists": False, "board": None}

    board = org["board"]
    total_members = 1  # Chair
    total_members += len(board.get("members", []))
    total_members += len(board.get("external_advisors", []))

    return {
        "exists": True,
        "board": board,
        "total_members": total_members,
        "max_size": board.get("max_size", 7),
        "has_capacity": total_members < board.get("max_size", 7),
    }


# ── Dynamic Membership ────────────────────────────────────────────────────────


def add_external_advisor(
    expertise: str,
    advisor_type: str,
    name: str | None = None,
) -> dict[str, Any]:
    """
    Add an external advisor to the board dynamically.

    Args:
        expertise: Description of advisor's expertise
        advisor_type: One of: industry-expert, customer-advocate, investor-rep, domain-advisor
        name: Optional display name for the advisor

    Returns:
        Result with created advisor
    """
    valid_types = [
        "industry-expert",
        "customer-advocate",
        "investor-rep",
        "domain-advisor",
    ]
    if advisor_type not in valid_types:
        return {
            "success": False,
            "error": f"Invalid advisor type. Must be one of: {valid_types}",
        }

    org = _load_org()

    if "board" not in org:
        return {"success": False, "error": "Board does not exist. Create board first."}

    board = org["board"]

    # Check capacity
    current_size = (
        1 + len(board.get("members", [])) + len(board.get("external_advisors", []))
    )
    max_size = board.get("max_size", 7)

    if current_size >= max_size:
        return {
            "success": False,
            "error": f"Board at maximum capacity ({max_size} members)",
            "current_size": current_size,
        }

    # Create advisor
    advisor_id = _generate_advisor_id(advisor_type, expertise)
    display_name = (
        name or f"{advisor_type.replace('-', ' ').title()} ({expertise[:30]})"
    )

    advisor = {
        "id": advisor_id,
        "name": display_name,
        "type": advisor_type,
        "expertise": expertise,
        "added_at": _now_iso(),
        "status": "active",
    }

    # Add to board
    if "external_advisors" not in board:
        board["external_advisors"] = []

    board["external_advisors"].append(advisor)
    _save_org(org)

    return {
        "success": True,
        "advisor": advisor,
        "board_size": current_size + 1,
        "max_size": max_size,
    }


def remove_external_advisor(advisor_id: str) -> dict[str, Any]:
    """Remove an external advisor from the board."""
    org = _load_org()

    if "board" not in org:
        return {"success": False, "error": "Board does not exist"}

    board = org["board"]
    advisors = board.get("external_advisors", [])

    # Find and remove advisor
    for i, advisor in enumerate(advisors):
        if advisor["id"] == advisor_id:
            removed = advisors.pop(i)
            _save_org(org)
            return {"success": True, "removed": removed}

    return {"success": False, "error": f"Advisor '{advisor_id}' not found"}


def get_external_advisors() -> list[dict[str, Any]]:
    """Get all external advisors."""
    org = _load_org()

    if "board" not in org:
        return []

    return org["board"].get("external_advisors", [])


def rotate_board_chair(new_chair_id: str, consensus: dict[str, str]) -> dict[str, Any]:
    """
    Rotate the board chair (requires consensus).

    Args:
        new_chair_id: ID of the new chair (must be existing member/advisor)
        consensus: Dict of {member_id: "approve"/"reject"} from existing board members

    Returns:
        Result of rotation
    """
    org = _load_org()

    if "board" not in org:
        return {"success": False, "error": "Board does not exist"}

    board = org["board"]

    # Check consensus - need all current voting members to approve
    required = board.get("quorum", ["board-chair", "forge-ceo"])
    approvals = sum(1 for v in consensus.values() if v == "approve")
    rejections = sum(1 for v in consensus.values() if v == "reject")

    if rejections > 0:
        return {"success": False, "error": "Chair rotation rejected by board member(s)"}

    if approvals < len(required):
        return {
            "success": False,
            "error": f"Insufficient approvals. Need {len(required)}, got {approvals}",
        }

    # Find new chair in advisors
    new_chair_found = False
    new_chair_expertise = []

    for advisor in board.get("external_advisors", []):
        if advisor["id"] == new_chair_id:
            new_chair_found = True
            new_chair_expertise = advisor.get("expertise", "").split(", ")
            break

    if not new_chair_found:
        return {
            "success": False,
            "error": f"New chair '{new_chair_id}' not found in advisors",
        }

    # Rotate
    old_chair = board["chair"]

    # Move old chair to advisors
    board["external_advisors"].append(
        {
            "id": old_chair["id"],
            "name": old_chair["name"],
            "type": "domain-advisor",
            "expertise": ", ".join(old_chair.get("expertise", [])),
            "added_at": _now_iso(),
            "status": "active",
        }
    )

    # Remove new chair from advisors
    board["external_advisors"] = [
        a for a in board["external_advisors"] if a["id"] != new_chair_id
    ]

    # Set new chair
    board["chair"] = {
        "id": new_chair_id,
        "name": f"Board Chair (rotated from {new_chair_id})",
        "type": "external_advisor",
        "expertise": new_chair_expertise,
        "status": "active",
    }

    # Update quorum
    board["quorum"] = [new_chair_id, "forge-ceo"]

    _save_org(org)

    return {
        "success": True,
        "old_chair": old_chair,
        "new_chair": board["chair"],
    }


def validate_board_size(max_size: int = 7) -> dict[str, Any]:
    """Check if board is within size limits."""
    org = _load_org()

    if "board" not in org:
        return {"valid": True, "reason": "No board exists"}

    board = org["board"]
    current_size = (
        1 + len(board.get("members", [])) + len(board.get("external_advisors", []))
    )

    return {
        "valid": current_size <= max_size,
        "current_size": current_size,
        "max_size": max_size,
        "capacity_remaining": max_size - current_size,
    }


# ── Governance Checks ─────────────────────────────────────────────────────────


def requires_board_approval(
    decision_type: str, context: dict[str, Any] | None = None
) -> GovernanceCheck:
    """
    Check if a decision requires board approval.

    Args:
        decision_type: Type of decision (hiring_executive, investment, expansion, etc.)
        context: Additional context (budget_amount, etc.)

    Returns:
        GovernanceCheck with requirements
    """
    config = _load_config()
    board_config = config.get("boardGovernance", {})

    if not board_config.get("enabled", False):
        return GovernanceCheck(
            requires_board=False,
            required_participants=[],
            decision_type=decision_type,
            reason="Board governance disabled",
        )

    org = _load_org()
    if "board" not in org:
        return GovernanceCheck(
            requires_board=False,
            required_participants=["forge-ceo"],
            decision_type=decision_type,
            reason="No board exists - CEO approval only",
        )

    context = context or {}
    board = org["board"]
    quorum = board.get("quorum", ["board-chair", "forge-ceo"])

    # Hiring decisions
    hiring_config = board_config.get("requireBoardForHiring", {})
    if decision_type == "hiring_executive" and hiring_config.get("executive", True):
        return GovernanceCheck(
            requires_board=True,
            required_participants=["board-chair", "forge-ceo", "forge-cto"],
            decision_type=decision_type,
            reason="Executive hiring requires full board approval",
        )

    if decision_type == "hiring_team_lead" and hiring_config.get("teamLead", True):
        return GovernanceCheck(
            requires_board=True,
            required_participants=quorum,
            decision_type=decision_type,
            reason="Team lead hiring requires board quorum",
        )

    if decision_type == "hiring_employee" and hiring_config.get("employee", False):
        return GovernanceCheck(
            requires_board=True,
            required_participants=["forge-ceo"],
            decision_type=decision_type,
            reason="Employee hiring requires CEO approval",
        )

    # Investment decisions
    investment_config = board_config.get("requireBoardForInvestment", {})
    if decision_type == "major_investment":
        budget_threshold = investment_config.get("budgetThresholdUsd", 1000)
        amount = context.get("amount", 0)

        if amount >= budget_threshold:
            return GovernanceCheck(
                requires_board=True,
                required_participants=["board-chair", "forge-ceo", "forge-cto"],
                decision_type=decision_type,
                reason=f"Investment >= ${budget_threshold} requires full board",
            )

    if decision_type == "budget_reallocation":
        reallocation_threshold = investment_config.get(
            "reallocationPercentThreshold", 20
        )
        percent = context.get("percent", 0)

        if percent >= reallocation_threshold:
            return GovernanceCheck(
                requires_board=True,
                required_participants=quorum,
                decision_type=decision_type,
                reason=f"Reallocation >= {reallocation_threshold}% requires board quorum",
            )

    # Expansion decisions
    expansion_config = board_config.get("expansionPlanning", {})
    if decision_type in ["expansion", "new_market", "new_product", "strategic_pivot"]:
        if expansion_config.get("requireBoardApproval", True):
            return GovernanceCheck(
                requires_board=True,
                required_participants=["board-chair", "forge-ceo", "forge-cto"],
                decision_type=decision_type,
                reason="Expansion/strategic decisions require full board",
            )

    # Default: no board required
    return GovernanceCheck(
        requires_board=False,
        required_participants=["forge-ceo"],
        decision_type=decision_type,
        reason="Standard decision - CEO approval sufficient",
    )


def get_required_consensus(decision_type: str) -> list[str]:
    """Get list of required participants for a decision type."""
    check = requires_board_approval(decision_type)
    return check.required_participants


def validate_quorum(participants: list[str]) -> dict[str, Any]:
    """Validate if participants meet quorum requirements."""
    org = _load_org()

    if "board" not in org:
        # No board - any CEO participation is valid
        return {
            "valid": "forge-ceo" in participants,
            "reason": "No board - CEO required",
        }

    board = org["board"]
    quorum = board.get("quorum", ["board-chair", "forge-ceo"])

    missing = [p for p in quorum if p not in participants]

    return {
        "valid": len(missing) == 0,
        "quorum": quorum,
        "participants": participants,
        "missing": missing,
    }


# ── Expansion Planning ────────────────────────────────────────────────────────


def propose_expansion(
    expansion_type: str,
    scope: dict[str, Any],
    budget_estimate: float | None = None,
) -> dict[str, Any]:
    """
    Create an expansion proposal for board review.

    Args:
        expansion_type: One of: new_market, new_product, team_growth, capability
        scope: Details of the expansion
        budget_estimate: Estimated budget impact

    Returns:
        Proposal ready for board review
    """
    valid_types = ["new_market", "new_product", "team_growth", "capability"]
    if expansion_type not in valid_types:
        return {
            "success": False,
            "error": f"Invalid expansion type. Must be one of: {valid_types}",
        }

    # Check governance requirements
    governance = requires_board_approval("expansion", {"type": expansion_type})

    proposal = {
        "proposal_id": f"expansion-{_now_iso().replace(':', '-')}",
        "type": expansion_type,
        "scope": scope,
        "budget_estimate": budget_estimate,
        "proposed_at": _now_iso(),
        "requires_board": governance.requires_board,
        "required_participants": governance.required_participants,
        "status": "pending",
    }

    return {"success": True, "proposal": proposal, "governance": asdict(governance)}


def analyze_budget_impact(proposal: dict[str, Any]) -> dict[str, Any]:
    """
    Analyze budget impact of a proposal.

    Args:
        proposal: Expansion or investment proposal

    Returns:
        Budget analysis
    """
    budget_estimate = proposal.get("budget_estimate", 0)

    # Get current budget from org.json economics
    org = _load_org()
    economics = org.get("economics", {})
    budget = economics.get("budget", {})

    monthly_limit = budget.get("monthly_limit_usd", 0)
    current_spent = budget.get("current_month_spent_usd", 0)
    remaining = monthly_limit - current_spent if monthly_limit > 0 else float("inf")

    analysis = {
        "proposal_cost": budget_estimate,
        "monthly_limit": monthly_limit,
        "current_spent": current_spent,
        "remaining_budget": remaining,
        "within_budget": budget_estimate <= remaining
        if remaining != float("inf")
        else True,
        "budget_impact_percent": (budget_estimate / monthly_limit * 100)
        if monthly_limit > 0
        else 0,
    }

    # Budget consciousness check
    config = _load_config()
    board_config = config.get("boardGovernance", {})
    expansion_config = board_config.get("expansionPlanning", {})

    if expansion_config.get("budgetConscious", True):
        if not analysis["within_budget"]:
            analysis["warning"] = "Proposal exceeds remaining monthly budget"
            analysis["recommendation"] = "Consider phasing or reducing scope"
        elif analysis["budget_impact_percent"] > 50:
            analysis["warning"] = "Proposal uses >50% of monthly budget"
            analysis["recommendation"] = "Review against other priorities"

    return analysis


def get_expansion_readiness() -> dict[str, Any]:
    """Check organizational readiness for expansion."""
    org = _load_org()

    # Check board exists
    has_board = "board" in org

    # Check employee count
    employees = org.get("employees", org.get("agents", []))
    employee_count = len(employees)

    # Check economics
    economics = org.get("economics", {})
    efficiency = economics.get("efficiency", {})
    company_score = efficiency.get("company_score", 0)

    readiness = {
        "has_board": has_board,
        "employee_count": employee_count,
        "efficiency_score": company_score,
        "ready": has_board and employee_count >= 3 and company_score > 0.5,
        "blockers": [],
    }

    if not has_board:
        readiness["blockers"].append("No board established - create board first")
    if employee_count < 3:
        readiness["blockers"].append("Insufficient team size - hire more employees")
    if company_score < 0.5:
        readiness["blockers"].append(
            "Efficiency score too low - optimize operations first"
        )

    return readiness


# ── CLI Interface ─────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: board_governance.py <command> [args]"}))
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "create":
            # board_governance.py create --domain saas_platform
            domain = "minimal"
            for i, arg in enumerate(sys.argv):
                if arg == "--domain" and i + 1 < len(sys.argv):
                    domain = sys.argv[i + 1]
            result = create_board(domain)

        elif command == "status":
            result = get_board_composition()

        elif command == "add-advisor":
            # board_governance.py add-advisor --expertise "..." --type industry-expert
            expertise = ""
            advisor_type = "industry-expert"
            name = None
            for i, arg in enumerate(sys.argv):
                if arg == "--expertise" and i + 1 < len(sys.argv):
                    expertise = sys.argv[i + 1]
                if arg == "--type" and i + 1 < len(sys.argv):
                    advisor_type = sys.argv[i + 1]
                if arg == "--name" and i + 1 < len(sys.argv):
                    name = sys.argv[i + 1]
            result = add_external_advisor(expertise, advisor_type, name)

        elif command == "remove-advisor":
            # board_governance.py remove-advisor --id advisor-id
            advisor_id = ""
            for i, arg in enumerate(sys.argv):
                if arg == "--id" and i + 1 < len(sys.argv):
                    advisor_id = sys.argv[i + 1]
            result = remove_external_advisor(advisor_id)

        elif command == "advisors":
            result = {"advisors": get_external_advisors()}

        elif command == "check":
            # board_governance.py check --decision hiring_executive
            decision_type = "general"
            for i, arg in enumerate(sys.argv):
                if arg == "--decision" and i + 1 < len(sys.argv):
                    decision_type = sys.argv[i + 1]
            check = requires_board_approval(decision_type)
            result = asdict(check)

        elif command == "quorum":
            # board_governance.py quorum --participants forge-ceo,forge-cto
            participants = []
            for i, arg in enumerate(sys.argv):
                if arg == "--participants" and i + 1 < len(sys.argv):
                    participants = sys.argv[i + 1].split(",")
            result = validate_quorum(participants)

        elif command == "readiness":
            result = get_expansion_readiness()

        elif command == "domain-expertise":
            # board_governance.py domain-expertise --template saas_platform
            template = "minimal"
            for i, arg in enumerate(sys.argv):
                if arg == "--template" and i + 1 < len(sys.argv):
                    template = sys.argv[i + 1]
            result = get_domain_expertise_for_template(template)

        else:
            result = {"error": f"Unknown command: {command}"}

        print(json.dumps(result, indent=2, default=str))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
