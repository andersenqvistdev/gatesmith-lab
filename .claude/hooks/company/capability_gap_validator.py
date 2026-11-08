#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Smart Capability Gap Validator (P33)

Validates capability gap proposals before creating hiring recommendations.
Prevents false positives by:
1. Checking if capability already exists in an employee
2. Filtering evidence to only gaps where the specific capability was missing
3. Recommending capability expansion over hiring when appropriate

Usage:
    from capability_gap_validator import validate_capability_proposal

    result = validate_capability_proposal("shell-scripting", gap_summary)
    if not result.is_valid:
        print(f"Auto-rejected: {result.auto_reject_reason}")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class RecommendedAction(Enum):
    """Recommended action for a capability gap."""

    HIRE_NEW = "hire_new"  # No coverage, hire specialist
    HIRE_FOR_SCALE = "hire_for_scale"  # Coverage exists but employee is overloaded
    EXPAND_EXISTING = (
        "expand_existing"  # Fallback handles well, add to their capabilities
    )
    REJECT = "reject"  # False positive, don't create proposal


@dataclass
class ValidationResult:
    """Result of validating a capability gap proposal."""

    capability: str
    is_valid: bool
    coverage_exists: bool
    covering_employees: list[str] = field(default_factory=list)
    filtered_gaps: list[dict] = field(default_factory=list)
    actual_gap_count: int = 0
    recommended_action: RecommendedAction = RecommendedAction.REJECT
    auto_reject_reason: str | None = None
    expansion_candidate: str | None = None
    expansion_success_rate: float = 0.0
    # Scaling fields
    scaling_recommended: bool = False
    overloaded_employees: list[str] = field(default_factory=list)
    workload_ratio: float = 0.0  # Tasks per employee with this capability


def get_company_dir() -> Path:
    """Get the company directory path."""
    # Walk up to find .company directory
    current = Path.cwd()
    for _ in range(10):
        company_dir = current / ".company"
        if company_dir.exists():
            return company_dir
        if current.parent == current:
            break
        current = current.parent
    return Path.cwd() / ".company"


def load_org() -> dict:
    """Load organization data from org.json (employees normalized to dicts).

    A fresh /company-bootstrap can persist bare-string employees; every
    consumer here reads emp.get(...), so normalize through the canonical
    company_resolver.normalize_org_employees (ProjectK root-cause fix).
    """
    company_dir = get_company_dir()
    path = company_dir / "org.json"
    if not path.exists():
        return {"employees": [], "economics": {}}
    try:
        with open(path, encoding="utf-8") as f:
            org = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"employees": [], "economics": {}}
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, company_dir)


def load_config() -> dict:
    """Load configuration from gatesmith.json."""
    # Check project root first
    config_path = Path.cwd() / "gatesmith.json"
    if not config_path.exists():
        # Try parent directories
        current = Path.cwd()
        for _ in range(5):
            if current.parent == current:
                break
            current = current.parent
            config_path = current / "gatesmith.json"
            if config_path.exists():
                break

    if not config_path.exists():
        return {}

    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def check_capability_coverage(
    capability: str, org: dict | None = None
) -> tuple[bool, list[str]]:
    """
    Check if any employee has the given capability.

    Args:
        capability: The capability to check for
        org: Optional pre-loaded org data

    Returns:
        Tuple of (coverage_exists, list_of_employee_ids_with_capability)
    """
    if org is None:
        org = load_org()

    covering_employees = []
    for emp in org.get("employees", []):
        emp_caps = emp.get("capabilities", [])
        if capability in emp_caps:
            covering_employees.append(emp.get("id", "unknown"))

    return len(covering_employees) > 0, covering_employees


def filter_gaps_for_capability(gaps: list[dict], capability: str) -> list[dict]:
    """
    Filter gap records to only those where the specific capability was missing.

    Each gap record has:
    - required: list of capabilities task needed
    - missing: list of capabilities that were NOT matched

    Only return gaps where `capability` appears in `missing`.

    Args:
        gaps: List of gap records from efficiency_data.json
        capability: The specific capability to filter for

    Returns:
        Filtered list of gaps where this capability was actually missing
    """
    filtered = []
    for gap in gaps:
        missing = gap.get("missing", [])
        if capability in missing:
            filtered.append(gap)
    return filtered


def analyze_fallback_success(
    capability: str,
    filtered_gaps: list[dict],
    org: dict | None = None,
) -> dict[str, Any]:
    """
    Analyze how well fallback employees handled tasks requiring this capability.

    If a fallback employee consistently handles tasks needing capability X,
    we should recommend adding X to their capabilities rather than hiring.

    Args:
        capability: The capability being analyzed
        filtered_gaps: Gaps where this capability was missing
        org: Optional pre-loaded org data

    Returns:
        Analysis dict with fallback_employees, best_candidate, recommend_expansion
    """
    if org is None:
        org = load_org()

    if not filtered_gaps:
        return {
            "fallback_employees": [],
            "success_rate": 0.0,
            "recommend_expansion": False,
            "best_candidate": None,
        }

    # Count which employees handled these gaps
    fallback_counts: dict[str, int] = {}
    for gap in filtered_gaps:
        fallback = gap.get("fallback_employee")
        if fallback:
            fallback_counts[fallback] = fallback_counts.get(fallback, 0) + 1

    if not fallback_counts:
        return {
            "fallback_employees": [],
            "success_rate": 0.0,
            "recommend_expansion": False,
            "best_candidate": None,
        }

    # Find the employee who handled most of these tasks
    best_candidate = max(fallback_counts, key=lambda x: fallback_counts[x])
    best_count = fallback_counts[best_candidate]

    # Calculate how often they handled tasks with this capability
    total_gaps = len(filtered_gaps)
    concentration_rate = best_count / total_gaps if total_gaps > 0 else 0.0

    # Check the employee's efficiency for these tasks
    # For now, we use concentration rate as a proxy for success
    # (if they keep getting assigned, they're probably doing well)

    # Recommend expansion if:
    # 1. One employee handles >= 50% of tasks requiring this capability
    # 2. They have handled at least 2 such tasks
    recommend_expansion = concentration_rate >= 0.5 and best_count >= 2

    return {
        "fallback_employees": list(fallback_counts.keys()),
        "fallback_counts": fallback_counts,
        "success_rate": concentration_rate,
        "recommend_expansion": recommend_expansion,
        "best_candidate": best_candidate if recommend_expansion else None,
    }


def check_scaling_need(
    capability: str,
    covering_employees: list[str],
    filtered_gaps: list[dict],
    org: dict | None = None,
    scaling_threshold: int = 10,
) -> dict[str, Any]:
    """
    Check if hiring is needed for scaling even when capability exists.

    When a capability is covered but the employee(s) with that capability
    are handling too many tasks, hiring another specialist makes sense.

    Args:
        capability: The capability being analyzed
        covering_employees: Employees who have this capability
        filtered_gaps: Gaps where this capability was missing
        org: Optional pre-loaded org data
        scaling_threshold: Tasks per employee before scaling is recommended

    Returns:
        Analysis dict with scaling_recommended, workload_ratio, overloaded_employees
    """
    if org is None:
        org = load_org()

    if not covering_employees:
        return {
            "scaling_recommended": False,
            "workload_ratio": 0.0,
            "overloaded_employees": [],
            "reason": "No covering employees",
        }

    # Count tasks requiring this capability (from gaps + estimated current workload)
    gap_count = len(filtered_gaps)

    # Check current workload of covering employees
    overloaded = []
    total_tasks = 0

    for emp_id in covering_employees:
        emp = None
        for e in org.get("employees", []):
            if e.get("id") == emp_id:
                emp = e
                break

        if emp:
            # Check efficiency data for task count
            efficiency = emp.get("efficiency", {})
            tasks_completed = efficiency.get("tasks_completed", 0)
            total_tasks += tasks_completed

            # If employee has completed many tasks and gaps are accumulating,
            # they might be overloaded
            if tasks_completed > 50 and gap_count > scaling_threshold:
                overloaded.append(emp_id)

    # Calculate workload ratio: gaps per covering employee
    workload_ratio = gap_count / len(covering_employees) if covering_employees else 0

    # Recommend scaling if:
    # 1. Workload ratio exceeds threshold (too many gaps per employee)
    # 2. OR employees are showing overload patterns
    scaling_recommended = workload_ratio >= scaling_threshold or len(overloaded) > 0

    return {
        "scaling_recommended": scaling_recommended,
        "workload_ratio": workload_ratio,
        "overloaded_employees": overloaded,
        "reason": (
            f"Workload ratio {workload_ratio:.1f} tasks per employee"
            if scaling_recommended
            else "Workload manageable"
        ),
    }


def upskill_employee(
    employee_id: str,
    capability: str,
    org: dict | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Add a capability to an employee's skill set.

    This is called when we detect an employee is successfully handling
    tasks requiring a capability they don't officially have.

    Args:
        employee_id: The employee to upskill
        capability: The capability to add
        org: Optional pre-loaded org data
        dry_run: If True, don't actually modify org.json

    Returns:
        Result dict with success status and details
    """
    if org is None:
        org = load_org()

    # Find the employee
    employee = None
    emp_index = -1
    for i, emp in enumerate(org.get("employees", [])):
        if emp.get("id") == employee_id:
            employee = emp
            emp_index = i
            break

    if not employee:
        return {
            "success": False,
            "reason": f"Employee '{employee_id}' not found",
        }

    # Check if they already have the capability
    current_caps = employee.get("capabilities", [])
    if capability in current_caps:
        return {
            "success": False,
            "reason": f"Employee '{employee_id}' already has '{capability}'",
        }

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "employee_id": employee_id,
            "capability": capability,
            "reason": f"Would add '{capability}' to {employee_id}",
        }

    # Add the capability
    current_caps.append(capability)
    org["employees"][emp_index]["capabilities"] = current_caps

    # Save org.json atomically
    import os
    import tempfile

    org_path = get_company_dir() / "org.json"
    try:
        # Write to temp file first
        fd, temp_path = tempfile.mkstemp(
            dir=org_path.parent, prefix=".org_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(org, f, indent=2)
            # Atomic replace
            os.replace(temp_path, org_path)
        except Exception:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

        # Log the upskill
        log_upskill(employee_id, capability)

        return {
            "success": True,
            "employee_id": employee_id,
            "capability": capability,
            "reason": f"Added '{capability}' to {employee_id}",
        }

    except Exception as e:
        return {
            "success": False,
            "reason": f"Failed to save org.json: {e}",
        }


def log_upskill(employee_id: str, capability: str) -> None:
    """Log an upskilling event to the audit trail."""
    import sys
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {
        "event": "employee_upskilled",
        "employee_id": employee_id,
        "capability": capability,
        "timestamp": timestamp,
    }

    print(
        f"[P33] Upskilled '{employee_id}' with capability '{capability}'",
        file=sys.stderr,
    )

    try:
        audit_path = get_company_dir() / "audit_log.json"
        if audit_path.exists():
            with open(audit_path, encoding="utf-8") as f:
                audit = json.load(f)
        else:
            audit = {"entries": []}

        audit["entries"].append(log_entry)
        audit["entries"] = audit["entries"][-1000:]

        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def validate_capability_proposal(
    capability: str,
    gap_summary: dict,
    min_gap_count: int = 3,
    config: dict | None = None,
) -> ValidationResult:
    """
    Main entry point. Validates a capability gap before proposal creation.

    Checks:
    1. Does any employee already have this capability?
    2. Filter gaps to only those where THIS capability was missing
    3. After filtering, do we still have min_gap_count gaps?
    4. If fallback employee handles successfully, recommend expansion instead

    Args:
        capability: The capability being proposed for hiring
        gap_summary: Gap summary from employee_activator.get_capability_gap_summary()
        min_gap_count: Minimum number of filtered gaps required (default 3)
        config: Optional validator config from gatesmith.json

    Returns:
        ValidationResult with is_valid, recommended_action, and details
    """
    if config is None:
        full_config = load_config()
        config = full_config.get("capabilityGapValidator", {})

    # Load org data once
    org = load_org()

    # Get all gaps from the summary
    all_gaps = gap_summary.get("gaps", [])

    # Step 1: Check if capability already exists
    coverage_exists, covering_employees = check_capability_coverage(capability, org)

    # Step 2: Filter gaps to only those where this capability was missing
    # (We need this for scaling analysis even when coverage exists)
    filtered_gaps = filter_gaps_for_capability(all_gaps, capability)
    actual_gap_count = len(filtered_gaps)

    # Step 3: If coverage exists, check if we need to scale
    if coverage_exists:
        scaling_threshold = config.get("scalingThreshold", 10)
        scaling_analysis = check_scaling_need(
            capability, covering_employees, filtered_gaps, org, scaling_threshold
        )

        if scaling_analysis["scaling_recommended"]:
            # Hire for scale - existing employee(s) are overloaded
            return ValidationResult(
                capability=capability,
                is_valid=True,
                coverage_exists=True,
                covering_employees=covering_employees,
                filtered_gaps=filtered_gaps,
                actual_gap_count=actual_gap_count,
                recommended_action=RecommendedAction.HIRE_FOR_SCALE,
                auto_reject_reason=None,
                scaling_recommended=True,
                overloaded_employees=scaling_analysis["overloaded_employees"],
                workload_ratio=scaling_analysis["workload_ratio"],
            )

        # No scaling needed - reject (capability is covered)
        if config.get("autoRejectCoveredCapabilities", True):
            return ValidationResult(
                capability=capability,
                is_valid=False,
                coverage_exists=True,
                covering_employees=covering_employees,
                filtered_gaps=filtered_gaps,
                actual_gap_count=actual_gap_count,
                recommended_action=RecommendedAction.REJECT,
                auto_reject_reason=(
                    f"Capability '{capability}' already exists in employee(s): "
                    f"{', '.join(covering_employees)} (workload manageable)"
                ),
            )

    # Step 4: Check if we have enough actual gaps
    if actual_gap_count < min_gap_count:
        return ValidationResult(
            capability=capability,
            is_valid=False,
            coverage_exists=coverage_exists,
            covering_employees=covering_employees,
            filtered_gaps=filtered_gaps,
            actual_gap_count=actual_gap_count,
            recommended_action=RecommendedAction.REJECT,
            auto_reject_reason=(
                f"Only {actual_gap_count} gap(s) found for '{capability}' "
                f"after filtering (minimum: {min_gap_count})"
            ),
        )

    # Step 5: Analyze fallback success - recommend expansion over hiring
    fallback_analysis = analyze_fallback_success(capability, filtered_gaps, org)

    if fallback_analysis["recommend_expansion"] and config.get(
        "preferExpansionOverHiring", True
    ):
        best_candidate = fallback_analysis["best_candidate"]

        # Auto-upskill if enabled
        if config.get("autoUpskill", False):
            upskill_result = upskill_employee(best_candidate, capability, org)
            if upskill_result["success"]:
                return ValidationResult(
                    capability=capability,
                    is_valid=False,  # No proposal needed - already upskilled
                    coverage_exists=True,
                    covering_employees=[best_candidate],
                    filtered_gaps=filtered_gaps,
                    actual_gap_count=actual_gap_count,
                    recommended_action=RecommendedAction.REJECT,
                    auto_reject_reason=(
                        f"Auto-upskilled '{best_candidate}' with '{capability}'"
                    ),
                    expansion_candidate=best_candidate,
                    expansion_success_rate=fallback_analysis["success_rate"],
                )

        # Return expansion recommendation for manual approval
        return ValidationResult(
            capability=capability,
            is_valid=True,
            coverage_exists=coverage_exists,
            covering_employees=covering_employees,
            filtered_gaps=filtered_gaps,
            actual_gap_count=actual_gap_count,
            recommended_action=RecommendedAction.EXPAND_EXISTING,
            auto_reject_reason=None,
            expansion_candidate=best_candidate,
            expansion_success_rate=fallback_analysis["success_rate"],
        )

    # Step 6: Valid hiring proposal (no coverage, no expansion candidate)
    return ValidationResult(
        capability=capability,
        is_valid=True,
        coverage_exists=coverage_exists,
        covering_employees=covering_employees,
        filtered_gaps=filtered_gaps,
        actual_gap_count=actual_gap_count,
        recommended_action=RecommendedAction.HIRE_NEW,
        auto_reject_reason=None,
    )


def log_auto_rejection(capability: str, reason: str) -> None:
    """Log an auto-rejection to the audit trail."""
    import sys
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {
        "event": "capability_proposal_rejected",
        "capability": capability,
        "reason": reason,
        "timestamp": timestamp,
    }

    # Log to stderr for visibility
    print(
        f"[P33] Auto-rejected hiring proposal for '{capability}': {reason}",
        file=sys.stderr,
    )

    # Optionally log to audit file
    try:
        audit_path = get_company_dir() / "audit_log.json"
        if audit_path.exists():
            with open(audit_path, encoding="utf-8") as f:
                audit = json.load(f)
        else:
            audit = {"entries": []}

        audit["entries"].append(log_entry)
        audit["entries"] = audit["entries"][-1000:]  # Keep last 1000 entries

        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass  # Don't fail on audit logging errors


# CLI for testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: capability_gap_validator.py <capability>")
        print("Example: capability_gap_validator.py shell-scripting")
        sys.exit(1)

    capability = sys.argv[1]

    # Load gap summary
    efficiency_path = get_company_dir() / "state/efficiency_data.json"
    if efficiency_path.exists():
        with open(efficiency_path, encoding="utf-8") as f:
            data = json.load(f)
        gap_summary = data.get("capability_gaps", {"gaps": [], "summary": {}})
    else:
        gap_summary = {"gaps": [], "summary": {}}

    result = validate_capability_proposal(capability, gap_summary)

    print(f"\n=== Validation Result for '{capability}' ===")
    print(f"Valid: {result.is_valid}")
    print(f"Coverage exists: {result.coverage_exists}")
    if result.covering_employees:
        print(f"Covered by: {', '.join(result.covering_employees)}")
    print(f"Actual gap count: {result.actual_gap_count}")
    print(f"Recommended action: {result.recommended_action.value}")
    if result.auto_reject_reason:
        print(f"Rejection reason: {result.auto_reject_reason}")
    if result.expansion_candidate:
        print(f"Expansion candidate: {result.expansion_candidate}")
        print(f"Expansion success rate: {result.expansion_success_rate:.1%}")

    print(f"\nFiltered gaps ({len(result.filtered_gaps)}):")
    for gap in result.filtered_gaps[-5:]:
        print(f"  - Task: {gap.get('task_id', 'N/A')}")
        print(f"    Missing: {gap.get('missing', [])}")
        print(f"    Fallback: {gap.get('fallback_employee', 'N/A')}")
