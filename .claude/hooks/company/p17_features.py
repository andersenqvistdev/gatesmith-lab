#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P17 New Project Genesis — Autonomous project creation and venture management.

This module enables the company to detect market opportunities, create new ventures,
manage project lifecycle, and track project health:
1. Market opportunity detection
2. New venture creation (project scaffolding)
3. Resource allocation for new ventures
4. Kill criteria for failing projects
5. Revenue attribution per project

All decisions are proposals that flow through the governance system.

Usage:
    # Scan for market opportunities
    python p17_features.py scan-opportunities

    # Create a new venture
    python p17_features.py create-venture --name "API Service" --template python-api

    # Evaluate project health
    python p17_features.py evaluate-project <project-id>

    # Check kill criteria
    python p17_features.py check-kill-criteria

    # Attribute revenue to project
    python p17_features.py attribute-revenue --project <id> --amount 1000
"""

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# Lazy imports for sibling modules
company_resolver = None
p18_features = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver, p18_features
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import p18_features as p18

        company_resolver = cr
        p18_features = p18
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr
        try:
            import p18_features as p18  # type: ignore[no-redef]

            p18_features = p18
        except ImportError:
            p18_features = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

VENTURE_STATE_FILE = "venture_state.json"
PROJECT_MANIFEST = "forge.company.json"
PROJECTS_DIR = "projects"

# Kill criteria thresholds
MIN_PROJECT_AGE_DAYS = 30  # Minimum age before evaluating for kill
MIN_TASKS_FOR_EVALUATION = 10  # Minimum tasks before kill evaluation
FAILURE_RATE_THRESHOLD = 0.7  # 70%+ failure rate triggers kill consideration
STAGNATION_DAYS = 14  # No activity for 14 days triggers review
ROI_THRESHOLD = -0.5  # ROI below -50% triggers kill consideration


class OpportunityType(str, Enum):
    """Types of market opportunities."""

    CAPABILITY_GAP = "capability_gap"  # Internal gap that could be productized
    MARKET_TREND = "market_trend"  # External market trend
    CUSTOMER_REQUEST = "customer_request"  # Direct customer feedback
    COMPETITIVE_GAP = "competitive_gap"  # Competitor weakness
    SYNERGY = "synergy"  # Cross-project synergy opportunity


class ProjectStatus(str, Enum):
    """Project lifecycle status."""

    PROPOSED = "proposed"  # Under evaluation
    INCUBATING = "incubating"  # Early development
    ACTIVE = "active"  # Full development
    MAINTENANCE = "maintenance"  # Stable, minimal changes
    SUNSET = "sunset"  # Winding down
    KILLED = "killed"  # Terminated


class KillReason(str, Enum):
    """Reasons for killing a project."""

    HIGH_FAILURE_RATE = "high_failure_rate"
    NEGATIVE_ROI = "negative_roi"
    STAGNATION = "stagnation"
    STRATEGIC_PIVOT = "strategic_pivot"
    RESOURCE_CONSOLIDATION = "resource_consolidation"
    MARKET_CHANGE = "market_change"


@dataclass
class MarketOpportunity:
    """A detected market opportunity."""

    id: str
    type: OpportunityType
    title: str
    description: str
    evidence: list[str]
    estimated_effort: str  # small, medium, large, epic
    estimated_value: str  # low, medium, high
    confidence: float
    detected_at: str
    status: str = "open"  # open, pursued, rejected


@dataclass
class Venture:
    """A new venture/project."""

    id: str
    name: str
    description: str
    status: ProjectStatus
    template: str
    path: str
    created_at: str
    created_by: str
    opportunity_id: str | None
    assigned_employees: list[str]
    budget_allocation: float
    metrics: dict
    kill_criteria_status: dict


@dataclass
class KillEvaluation:
    """Evaluation of whether a project should be killed."""

    project_id: str
    project_name: str
    should_kill: bool
    reasons: list[KillReason]
    evidence: dict
    confidence: float
    evaluated_at: str
    recommendation: str


# -----------------------------------------------------------------------------
# Core State Management
# -----------------------------------------------------------------------------


def load_venture_state(company_dir: Path | None = None) -> dict:
    """Load venture state."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state_path = company_dir / VENTURE_STATE_FILE
    if not state_path.exists():
        return {
            "opportunities": [],
            "ventures": [],
            "kill_evaluations": [],
            "last_scan": None,
            "version": "1.0",
        }

    with open(state_path) as f:
        return json.load(f)


def save_venture_state(state: dict, company_dir: Path | None = None) -> None:
    """Save venture state atomically."""
    import os
    import tempfile

    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state_path = company_dir / VENTURE_STATE_FILE

    fd, tmp_path = tempfile.mkstemp(dir=company_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, state_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_projects(company_dir: Path | None = None) -> list[dict]:
    """Load all registered projects."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    manifest_path = company_dir.parent / PROJECT_MANIFEST
    if not manifest_path.exists():
        # Try in company_dir itself
        manifest_path = company_dir / PROJECT_MANIFEST

    if not manifest_path.exists():
        return []

    with open(manifest_path) as f:
        manifest = json.load(f)

    return manifest.get("projects", [])


# -----------------------------------------------------------------------------
# Opportunity Detection
# -----------------------------------------------------------------------------


def scan_for_opportunities(company_dir: Path | None = None) -> list[MarketOpportunity]:
    """Scan for market opportunities based on various signals."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    opportunities = []

    # Load organization data
    org_path = company_dir / "org.json"
    if org_path.exists():
        with open(org_path) as f:
            org = json.load(f)
    else:
        org = {"employees": []}

    # 1. Detect capability gaps that could be productized
    capabilities = {}
    for emp in org.get("employees", []):
        for cap in emp.get("capabilities", []):
            capabilities[cap] = capabilities.get(cap, 0) + 1

    # Find capabilities with high concentration (potential product areas)
    for cap, count in capabilities.items():
        if count >= 3:  # 3+ employees with same capability = expertise
            opportunities.append(
                MarketOpportunity(
                    id=f"opp-cap-{cap}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    type=OpportunityType.CAPABILITY_GAP,
                    title=f"Productize '{cap}' expertise",
                    description=f"Strong internal capability in '{cap}' ({count} employees). "
                    f"Consider creating a specialized service or product.",
                    evidence=[
                        f"{count} employees have {cap} capability",
                        "Internal expertise indicates market demand",
                    ],
                    estimated_effort="medium",
                    estimated_value="medium",
                    confidence=0.5 + (count * 0.1),
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    # 2. Detect synergy opportunities between projects
    projects = load_projects(company_dir)
    if len(projects) >= 2:
        # Check for shared dependencies/patterns
        opportunities.append(
            MarketOpportunity(
                id=f"opp-synergy-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                type=OpportunityType.SYNERGY,
                title="Cross-project integration platform",
                description=f"Managing {len(projects)} projects suggests opportunity for "
                f"shared infrastructure, common libraries, or integration layer.",
                evidence=[
                    f"{len(projects)} active projects",
                    "Potential for code/knowledge sharing",
                ],
                estimated_effort="large",
                estimated_value="high",
                confidence=0.6,
                detected_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    # 3. Check for stagnant capabilities (potential pivot opportunities)
    for emp in org.get("employees", []):
        efficiency = emp.get("efficiency", {})
        if efficiency.get("tasks_completed", 0) == 0:
            continue
        if efficiency.get("score", 100) < 10:  # Very low efficiency
            for cap in emp.get("capabilities", []):
                opportunities.append(
                    MarketOpportunity(
                        id=f"opp-pivot-{cap}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                        type=OpportunityType.MARKET_TREND,
                        title=f"Pivot from {cap} - low demand signal",
                        description=f"Low efficiency in '{cap}' may indicate market shift. "
                        f"Consider pivoting to adjacent capability.",
                        evidence=[
                            f"Employee with {cap} has low efficiency",
                            "Possible market demand decrease",
                        ],
                        estimated_effort="medium",
                        estimated_value="medium",
                        confidence=0.4,
                        detected_at=datetime.now(timezone.utc).isoformat(),
                    )
                )

    # Save opportunities to state
    state = load_venture_state(company_dir)
    existing_ids = {
        o["id"].split("-")[1] + o["id"].split("-")[2]
        for o in state.get("opportunities", [])
    }

    for opp in opportunities:
        opp_key = opp.id.split("-")[1] + opp.id.split("-")[2]
        if opp_key not in existing_ids:
            state["opportunities"].append(asdict(opp))

    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_venture_state(state, company_dir)

    return opportunities


# -----------------------------------------------------------------------------
# Venture Creation
# -----------------------------------------------------------------------------


def create_venture(
    name: str,
    description: str,
    template: str = "minimal",
    opportunity_id: str | None = None,
    budget: float = 0.0,
    company_dir: Path | None = None,
) -> dict:
    """Create a new venture/project."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    # Generate venture ID
    venture_id = name.lower().replace(" ", "-").replace("_", "-")
    venture_id = "".join(c for c in venture_id if c.isalnum() or c == "-")

    # Determine project path
    project_path = company_dir.parent / PROJECTS_DIR / venture_id
    if project_path.exists():
        return {
            "success": False,
            "error": f"Project path already exists: {project_path}",
        }

    # Create venture record
    venture = Venture(
        id=venture_id,
        name=name,
        description=description,
        status=ProjectStatus.PROPOSED,
        template=template,
        path=str(project_path.relative_to(company_dir.parent)),
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by="system",
        opportunity_id=opportunity_id,
        assigned_employees=[],
        budget_allocation=budget,
        metrics={
            "tasks_completed": 0,
            "tasks_failed": 0,
            "commits": 0,
            "revenue": 0.0,
            "costs": 0.0,
        },
        kill_criteria_status={
            "failure_rate": 0.0,
            "days_stagnant": 0,
            "roi": 0.0,
            "last_activity": None,
        },
    )

    # Create project directory structure
    try:
        project_path.mkdir(parents=True, exist_ok=True)
        (project_path / ".planning").mkdir(exist_ok=True)
        (project_path / ".claude").mkdir(exist_ok=True)
        (project_path / "CLAUDE.md").write_text(f"# {name}\n\n{description}\n")
        (project_path / ".planning" / "PROJECT.md").write_text(
            f"# {name}\n\n## Description\n{description}\n\n## Template\n{template}\n"
        )
    except OSError as e:
        return {"success": False, "error": f"Failed to create project directory: {e}"}

    # Save venture to state
    state = load_venture_state(company_dir)
    state["ventures"].append(asdict(venture))
    save_venture_state(state, company_dir)

    # Update project manifest if it exists
    manifest_path = company_dir.parent / PROJECT_MANIFEST
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)

        manifest.setdefault("projects", []).append(
            {
                "name": venture_id,
                "path": venture.path,
                "added": datetime.now(timezone.utc).isoformat(),
                "status": "proposed",
            }
        )

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    return {
        "success": True,
        "venture_id": venture_id,
        "path": str(project_path),
        "message": f"Created venture '{name}' at {project_path}",
    }


# -----------------------------------------------------------------------------
# Resource Allocation
# -----------------------------------------------------------------------------


def allocate_resources(
    venture_id: str,
    employee_ids: list[str],
    budget: float = 0.0,
    company_dir: Path | None = None,
) -> dict:
    """Allocate employees and budget to a venture."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_venture_state(company_dir)

    # Find the venture
    venture = None
    venture_idx = None
    for idx, v in enumerate(state.get("ventures", [])):
        if v["id"] == venture_id:
            venture = v
            venture_idx = idx
            break

    if venture is None:
        return {"success": False, "error": f"Venture {venture_id} not found"}

    # Update assignments
    venture["assigned_employees"] = employee_ids
    venture["budget_allocation"] = budget

    # Update org.json with project assignments
    org_path = company_dir / "org.json"
    if org_path.exists():
        with open(org_path) as f:
            org = json.load(f)

        for emp in org.get("employees", []):
            if emp["id"] in employee_ids:
                emp.setdefault("projectAssignments", [])
                if venture_id not in emp["projectAssignments"]:
                    emp["projectAssignments"].append(venture_id)

        # Atomic write
        import os
        import tempfile

        fd, tmp_path = tempfile.mkstemp(dir=company_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(org, f, indent=2)
            os.replace(tmp_path, org_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    state["ventures"][venture_idx] = venture
    save_venture_state(state, company_dir)

    return {
        "success": True,
        "venture_id": venture_id,
        "employees_assigned": len(employee_ids),
        "budget_allocated": budget,
    }


# -----------------------------------------------------------------------------
# Kill Criteria Evaluation
# -----------------------------------------------------------------------------


def evaluate_kill_criteria(
    venture_id: str, company_dir: Path | None = None
) -> KillEvaluation:
    """Evaluate whether a venture should be killed."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_venture_state(company_dir)

    # Find the venture
    venture = None
    for v in state.get("ventures", []):
        if v["id"] == venture_id:
            venture = v
            break

    if venture is None:
        return KillEvaluation(
            project_id=venture_id,
            project_name="Unknown",
            should_kill=False,
            reasons=[],
            evidence={"error": "Venture not found"},
            confidence=0.0,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            recommendation="Venture not found - cannot evaluate",
        )

    # Calculate metrics
    created_at = datetime.fromisoformat(venture["created_at"].replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - created_at).days
    metrics = venture.get("metrics", {})

    tasks_completed = metrics.get("tasks_completed", 0)
    tasks_failed = metrics.get("tasks_failed", 0)
    total_tasks = tasks_completed + tasks_failed

    failure_rate = tasks_failed / total_tasks if total_tasks > 0 else 0.0
    revenue = metrics.get("revenue", 0.0)
    costs = metrics.get("costs", 0.0)
    roi = (revenue - costs) / costs if costs > 0 else 0.0

    # Check stagnation
    last_activity = venture.get("kill_criteria_status", {}).get("last_activity")
    if last_activity:
        last_activity_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
        days_stagnant = (datetime.now(timezone.utc) - last_activity_dt).days
    else:
        days_stagnant = age_days

    # Evaluate kill criteria
    reasons = []
    evidence = {
        "age_days": age_days,
        "failure_rate": failure_rate,
        "roi": roi,
        "days_stagnant": days_stagnant,
        "total_tasks": total_tasks,
        "revenue": revenue,
        "costs": costs,
    }

    should_kill = False
    confidence = 0.0

    # Only evaluate if project is old enough and has activity
    if age_days >= MIN_PROJECT_AGE_DAYS and total_tasks >= MIN_TASKS_FOR_EVALUATION:
        if failure_rate >= FAILURE_RATE_THRESHOLD:
            reasons.append(KillReason.HIGH_FAILURE_RATE)
            confidence += 0.3

        if costs > 0 and roi < ROI_THRESHOLD:
            reasons.append(KillReason.NEGATIVE_ROI)
            confidence += 0.3

        if days_stagnant >= STAGNATION_DAYS:
            reasons.append(KillReason.STAGNATION)
            confidence += 0.2

        should_kill = len(reasons) >= 2 or (len(reasons) == 1 and confidence >= 0.3)
        confidence = min(1.0, confidence)

    # Generate recommendation
    if should_kill:
        recommendation = (
            f"Recommend killing project due to: {', '.join(r.value for r in reasons)}"
        )
    elif reasons:
        recommendation = f"Project showing warning signs: {', '.join(r.value for r in reasons)}. Monitor closely."
    else:
        recommendation = "Project healthy - no kill criteria triggered."

    evaluation = KillEvaluation(
        project_id=venture_id,
        project_name=venture["name"],
        should_kill=should_kill,
        reasons=reasons,
        evidence=evidence,
        confidence=confidence,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        recommendation=recommendation,
    )

    # Save evaluation
    state["kill_evaluations"].append(asdict(evaluation))
    save_venture_state(state, company_dir)

    return evaluation


def kill_venture(
    venture_id: str, reason: KillReason, company_dir: Path | None = None
) -> dict:
    """Kill/sunset a venture."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_venture_state(company_dir)

    # Find and update the venture
    for v in state["ventures"]:
        if v["id"] == venture_id:
            v["status"] = ProjectStatus.KILLED.value
            v["killed_at"] = datetime.now(timezone.utc).isoformat()
            v["kill_reason"] = reason.value

            # Clear employee assignments
            employee_ids = v.get("assigned_employees", [])
            v["assigned_employees"] = []

            save_venture_state(state, company_dir)

            # Update org.json to remove assignments
            org_path = company_dir / "org.json"
            if org_path.exists():
                with open(org_path) as f:
                    org = json.load(f)

                for emp in org.get("employees", []):
                    if emp["id"] in employee_ids:
                        assignments = emp.get("projectAssignments", [])
                        if venture_id in assignments:
                            assignments.remove(venture_id)
                            emp["projectAssignments"] = assignments

                # Atomic write
                import os
                import tempfile

                fd, tmp_path = tempfile.mkstemp(dir=company_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(org, f, indent=2)
                    os.replace(tmp_path, org_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

            return {
                "success": True,
                "venture_id": venture_id,
                "message": f"Venture '{v['name']}' killed due to {reason.value}",
                "employees_freed": employee_ids,
            }

    return {"success": False, "error": f"Venture {venture_id} not found"}


# -----------------------------------------------------------------------------
# Revenue Attribution
# -----------------------------------------------------------------------------


def attribute_revenue(
    venture_id: str,
    amount: float,
    source: str = "other",
    company_dir: Path | None = None,
) -> dict:
    """Attribute revenue to a specific venture."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_venture_state(company_dir)

    for v in state["ventures"]:
        if v["id"] == venture_id:
            v.setdefault("metrics", {})
            v["metrics"]["revenue"] = v["metrics"].get("revenue", 0.0) + amount
            v["kill_criteria_status"]["last_activity"] = datetime.now(
                timezone.utc
            ).isoformat()

            save_venture_state(state, company_dir)

            # Also record in p18 economics if available
            if p18_features:
                try:
                    p18_features.record_revenue(amount, source, venture_id)
                except Exception:
                    pass  # P18 integration is optional

            return {
                "success": True,
                "venture_id": venture_id,
                "amount": amount,
                "total_revenue": v["metrics"]["revenue"],
            }

    return {"success": False, "error": f"Venture {venture_id} not found"}


def attribute_cost(
    venture_id: str,
    amount: float,
    category: str = "development",
    company_dir: Path | None = None,
) -> dict:
    """Attribute cost to a specific venture."""
    _ensure_imports()
    if company_dir is None:
        company_dir = company_resolver.get_company_dir()

    state = load_venture_state(company_dir)

    for v in state["ventures"]:
        if v["id"] == venture_id:
            v.setdefault("metrics", {})
            v["metrics"]["costs"] = v["metrics"].get("costs", 0.0) + amount

            save_venture_state(state, company_dir)
            return {
                "success": True,
                "venture_id": venture_id,
                "amount": amount,
                "total_costs": v["metrics"]["costs"],
            }

    return {"success": False, "error": f"Venture {venture_id} not found"}


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="P17 New Project Genesis")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan-opportunities
    scan_parser = subparsers.add_parser(
        "scan-opportunities", help="Scan for market opportunities"
    )
    scan_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # create-venture
    create_parser = subparsers.add_parser("create-venture", help="Create a new venture")
    create_parser.add_argument("--name", required=True, help="Venture name")
    create_parser.add_argument("--description", default="", help="Venture description")
    create_parser.add_argument("--template", default="minimal", help="Project template")
    create_parser.add_argument(
        "--budget", type=float, default=0.0, help="Initial budget"
    )
    create_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # evaluate-project
    eval_parser = subparsers.add_parser(
        "evaluate-project", help="Evaluate project health"
    )
    eval_parser.add_argument("project_id", help="Project ID to evaluate")
    eval_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # check-kill-criteria
    kill_parser = subparsers.add_parser(
        "check-kill-criteria", help="Check all projects for kill criteria"
    )
    kill_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # attribute-revenue
    rev_parser = subparsers.add_parser(
        "attribute-revenue", help="Attribute revenue to project"
    )
    rev_parser.add_argument("--project", required=True, help="Project ID")
    rev_parser.add_argument(
        "--amount", type=float, required=True, help="Revenue amount"
    )
    rev_parser.add_argument("--source", default="other", help="Revenue source")
    rev_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # list-ventures
    list_parser = subparsers.add_parser("list-ventures", help="List all ventures")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    _ensure_imports()
    company_dir = company_resolver.get_company_dir()

    if args.command == "scan-opportunities":
        opportunities = scan_for_opportunities(company_dir)

        if args.json:
            print(json.dumps([asdict(o) for o in opportunities], indent=2))
        else:
            print(f"\n{'=' * 60}")
            print(" MARKET OPPORTUNITIES")
            print(f"{'=' * 60}\n")
            print(f"Found {len(opportunities)} opportunities:\n")

            for opp in opportunities:
                print(f"  [{opp.type.value.upper()}] {opp.title}")
                print(
                    f"    Effort: {opp.estimated_effort} | Value: {opp.estimated_value}"
                )
                print(f"    Confidence: {opp.confidence * 100:.0f}%")
                print()

    elif args.command == "create-venture":
        result = create_venture(
            name=args.name,
            description=args.description,
            template=args.template,
            budget=args.budget,
            company_dir=company_dir,
        )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("success"):
                print(f"Created venture: {result['venture_id']}")
                print(f"Path: {result['path']}")
            else:
                print(f"Failed: {result.get('error')}")

    elif args.command == "evaluate-project":
        evaluation = evaluate_kill_criteria(args.project_id, company_dir)

        if args.json:
            print(json.dumps(asdict(evaluation), indent=2))
        else:
            print(f"\n{'=' * 60}")
            print(f" PROJECT EVALUATION: {evaluation.project_name}")
            print(f"{'=' * 60}\n")
            print(f"Should Kill: {'YES' if evaluation.should_kill else 'NO'}")
            print(f"Confidence: {evaluation.confidence * 100:.0f}%")
            print(
                f"Reasons: {', '.join(r.value for r in evaluation.reasons) or 'None'}"
            )
            print(f"\nRecommendation: {evaluation.recommendation}")
            print("\nEvidence:")
            for key, value in evaluation.evidence.items():
                print(f"  {key}: {value}")

    elif args.command == "check-kill-criteria":
        state = load_venture_state(company_dir)
        evaluations = []

        for venture in state.get("ventures", []):
            if venture["status"] not in [
                ProjectStatus.KILLED.value,
                ProjectStatus.SUNSET.value,
            ]:
                evaluation = evaluate_kill_criteria(venture["id"], company_dir)
                evaluations.append(evaluation)

        if args.json:
            print(json.dumps([asdict(e) for e in evaluations], indent=2))
        else:
            print(f"\n{'=' * 60}")
            print(" KILL CRITERIA CHECK")
            print(f"{'=' * 60}\n")

            kill_candidates = [e for e in evaluations if e.should_kill]
            warning_candidates = [
                e for e in evaluations if e.reasons and not e.should_kill
            ]

            if kill_candidates:
                print("KILL CANDIDATES:")
                for e in kill_candidates:
                    print(f"  - {e.project_name}: {e.recommendation}")
                print()

            if warning_candidates:
                print("WARNING (monitor closely):")
                for e in warning_candidates:
                    print(f"  - {e.project_name}: {e.recommendation}")
                print()

            if not kill_candidates and not warning_candidates:
                print("All projects healthy - no kill criteria triggered.")

    elif args.command == "attribute-revenue":
        result = attribute_revenue(
            venture_id=args.project,
            amount=args.amount,
            source=args.source,
            company_dir=company_dir,
        )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("success"):
                print(f"Attributed ${args.amount} to {args.project}")
                print(f"Total revenue: ${result['total_revenue']}")
            else:
                print(f"Failed: {result.get('error')}")

    elif args.command == "list-ventures":
        state = load_venture_state(company_dir)
        ventures = state.get("ventures", [])

        if args.json:
            print(json.dumps(ventures, indent=2))
        else:
            print(f"\n{'=' * 60}")
            print(f" VENTURES ({len(ventures)})")
            print(f"{'=' * 60}\n")

            for v in ventures:
                print(f"  {v['id']}: {v['name']}")
                print(f"    Status: {v['status']}")
                print(f"    Path: {v['path']}")
                print(f"    Employees: {len(v.get('assigned_employees', []))}")
                print()

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
