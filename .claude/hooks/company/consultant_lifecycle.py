#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Consultant Lifecycle Utility — manages auto-consultant operations.

Provides functions for the complete lifecycle of auto-consultants:
- Registration: Create new auto-consultants in org.json
- Matching: Find existing consultants that match a request
- Archival: Archive inactive consultants with knowledge capture
- Reactivation: Restore archived consultants for new work
- Status tracking: Update consultant status and context

Features:
- Multi-project support via company_resolver
- Knowledge capture integration for preserving learnings
- Skill-based consultant matching
- Idle timeout detection
- Full archive with memory snapshots

Usage:
    # Register a new consultant
    python consultant_lifecycle.py register --id my-specialist --name "My Specialist" \
        --request "Help with API design" --skills "api,rest,design" --department engineering

    # Find matching consultant
    python consultant_lifecycle.py find --request "Help with REST API" --skills "api,rest"

    # Archive a consultant
    python consultant_lifecycle.py archive --id my-specialist --reason "Project complete"

    # Reactivate an archived consultant
    python consultant_lifecycle.py reactivate --id my-specialist --request "New API work needed"

    # Update consultant status
    python consultant_lifecycle.py status --id my-specialist --status busy --context "Working on task"

    # Get consultant context
    python consultant_lifecycle.py context --id my-specialist

    # Check for idle consultants
    python consultant_lifecycle.py check-idle --timeout 24

    # Show help
    python consultant_lifecycle.py help
"""

import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import company_resolver for multi-project support
try:
    from company_resolver import (
        get_company_dir,
        get_current_project,
        get_project_id,
        is_multi_project_mode,
    )
except ImportError:
    # Fallback for when running standalone or in legacy mode
    def get_company_dir(start_path=None):
        return Path(start_path or os.getcwd()) / ".company"

    def get_current_project():
        return None

    def get_project_id(project_path=None):
        path = Path(project_path or os.getcwd())
        return path.name.lower()

    def is_multi_project_mode(start_path=None):
        return False


# Import knowledge_capture for extract_learnings reuse
try:
    from knowledge_capture import extract_learnings
except ImportError:
    # Fallback stub - returns minimal result if knowledge_capture not available
    def extract_learnings(agent_id: str) -> dict:
        return {
            "success": False,
            "reason": "import_error",
            "message": "knowledge_capture module not available",
            "agent_id": agent_id,
        }


# Constants
ORG_FILE = "org.json"
CONFIG_FILE = "config.json"
ARCHIVE_DIR = "archive/consultants"
EMPLOYEES_DIR = "employees"
DEFAULT_IDLE_TIMEOUT_HOURS = 24
DEFAULT_SKILL_MATCH_THRESHOLD = 0.6  # Default threshold for skill matching

# Skill synonym map for related skills matching
SKILL_SYNONYMS = {
    "database": ["sql", "orm", "postgres", "mysql", "mongodb"],
    "frontend": ["react", "vue", "angular", "ui", "css"],
    "backend": ["api", "server", "rest", "graphql"],
    "devops": ["docker", "kubernetes", "ci/cd", "infrastructure"],
    "testing": ["test", "spec", "coverage", "qa"],
    "security": ["auth", "encryption", "owasp", "vulnerability"],
}

# Domain specialists mapping (imported from agent_spawner.py concepts)
# Maps domain keywords to skill sets for extraction from natural language
DOMAIN_SKILL_MAPPING = {
    "database": [
        "database",
        "sql",
        "orm",
        "postgres",
        "mysql",
        "mongodb",
        "migration",
        "schema",
        "query",
    ],
    "api": ["api", "rest", "graphql", "endpoint", "swagger", "openapi"],
    "frontend": [
        "frontend",
        "react",
        "vue",
        "angular",
        "ui",
        "css",
        "component",
        "responsive",
    ],
    "backend": ["backend", "server", "api", "service", "microservice"],
    "devops": [
        "devops",
        "docker",
        "kubernetes",
        "ci/cd",
        "pipeline",
        "deploy",
        "infrastructure",
        "terraform",
    ],
    "testing": [
        "testing",
        "test",
        "spec",
        "coverage",
        "qa",
        "unit",
        "integration",
        "e2e",
    ],
    "security": [
        "security",
        "auth",
        "authentication",
        "encryption",
        "owasp",
        "vulnerability",
        "token",
    ],
    "performance": [
        "performance",
        "optimize",
        "latency",
        "profiling",
        "benchmark",
        "cache",
    ],
    "data": [
        "data",
        "etl",
        "pipeline",
        "transform",
        "csv",
        "json",
        "parsing",
        "validation",
    ],
    "accessibility": ["accessibility", "a11y", "aria", "wcag", "screen reader"],
    "i18n": [
        "i18n",
        "internationalization",
        "l10n",
        "localization",
        "translation",
        "locale",
    ],
}

# Common technical terms for skill extraction
COMMON_TECH_TERMS = [
    # Languages
    "python",
    "javascript",
    "typescript",
    "java",
    "go",
    "rust",
    "ruby",
    "php",
    "swift",
    "kotlin",
    # Frameworks
    "react",
    "vue",
    "angular",
    "django",
    "flask",
    "fastapi",
    "express",
    "spring",
    "rails",
    # Databases
    "postgres",
    "mysql",
    "mongodb",
    "redis",
    "elasticsearch",
    "sqlite",
    # DevOps
    "docker",
    "kubernetes",
    "aws",
    "gcp",
    "azure",
    "terraform",
    "ansible",
    # General
    "api",
    "rest",
    "graphql",
    "testing",
    "security",
    "performance",
    "documentation",
]


def _get_company_base(start_path: Path | str | None = None) -> Path:
    """Get the base company directory (multi-project aware)."""
    return get_company_dir(start_path or Path.cwd())


def _get_org_path(company_dir: Path | str | None = None) -> Path:
    """Get the org.json file path."""
    base = Path(company_dir) if company_dir else _get_company_base()
    return base / ORG_FILE


def _get_config_path(company_dir: Path | str | None = None) -> Path:
    """Get the config.json file path."""
    base = Path(company_dir) if company_dir else _get_company_base()
    return base / CONFIG_FILE


def _get_archive_dir(company_dir: Path | str | None = None) -> Path:
    """Get the consultants archive directory path."""
    base = Path(company_dir) if company_dir else _get_company_base()
    return base / ARCHIVE_DIR


def _get_employee_dir(employee_id: str, company_dir: Path | str | None = None) -> Path:
    """Get the employee directory path."""
    base = Path(company_dir) if company_dir else _get_company_base()
    return base / EMPLOYEES_DIR / employee_id


def _load_json(path: Path) -> dict | None:
    """Load JSON file safely."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: dict) -> bool:
    """Save JSON file safely."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def _read_file(path: Path) -> str | None:
    """Read file content safely."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_file(path: Path, content: str) -> bool:
    """Write file content safely."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except OSError:
        return False


def _load_org(company_dir: Path | None = None) -> dict:
    """Load org.json with fallback defaults."""
    path = _get_org_path(company_dir)
    data = _load_json(path)
    if data is None:
        return {
            "version": "2.1",
            "mode": "single-project",
            "employees": [],
            "agents": [],  # Legacy fallback
        }
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(data, path.parent)


def _save_org(data: dict, company_dir: Path | None = None) -> bool:
    """Save org.json.

    Safety: Refuses to save if it would wipe existing employees.
    """
    path = _get_org_path(company_dir)

    # Safety check: Don't wipe employees if file already has them
    if path.exists():
        existing = _load_json(path)
        if existing:
            existing_employees = existing.get("employees", [])
            new_employees = data.get("employees", [])

            if len(existing_employees) > 0 and len(new_employees) == 0:
                import sys

                print(
                    f"[SAFETY] Blocked _save_org: Would wipe {len(existing_employees)} employees.",
                    file=sys.stderr,
                )
                return False

    return _save_json(path, data)


def _load_config(company_dir: Path | None = None) -> dict:
    """Load config.json with defaults."""
    path = _get_config_path(company_dir)
    data = _load_json(path)
    if data is None:
        return {
            "agents": {
                "consultantIdleTimeout": DEFAULT_IDLE_TIMEOUT_HOURS,
                "autoArchiveConsultants": True,
            }
        }
    return data


def _get_employees(org: dict) -> list:
    """Get employees list from org, handling legacy 'agents' field."""
    if "employees" in org and org["employees"]:
        return org["employees"]
    # Legacy fallback
    return org.get("agents", [])


def _set_employees(org: dict, employees: list) -> None:
    """Set employees list in org, maintaining both fields for compatibility."""
    org["employees"] = employees
    # Also update agents for backward compatibility
    org["agents"] = employees


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def extract_skills_from_request(request: str) -> list[str]:
    """
    Extract skill keywords from natural language request.

    Uses DOMAIN_SKILL_MAPPING plus common technical terms to identify
    relevant skills from a free-form request description.

    Args:
        request: Natural language request string

    Returns:
        List of extracted skill keywords (lowercase, deduplicated)
    """
    if not request:
        return []

    request_lower = request.lower()
    extracted_skills = set()

    # Extract skills from domain mapping
    for domain, skills in DOMAIN_SKILL_MAPPING.items():
        for skill in skills:
            # Check if skill term appears in request (word boundary aware)
            if re.search(rf"\b{re.escape(skill)}\b", request_lower):
                extracted_skills.add(skill)
                # Also add the domain as a skill if any of its terms match
                extracted_skills.add(domain)

    # Extract common tech terms
    for term in COMMON_TECH_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", request_lower):
            extracted_skills.add(term)

    return sorted(extracted_skills)


def _get_skill_synonyms(skill: str) -> set[str]:
    """
    Get all synonyms for a skill from SKILL_SYNONYMS mapping.

    Args:
        skill: The skill to find synonyms for

    Returns:
        Set of related skills (includes the skill itself)
    """
    skill_lower = skill.lower().strip()
    related = {skill_lower}

    # Check if skill is a key in synonyms
    if skill_lower in SKILL_SYNONYMS:
        related.update(SKILL_SYNONYMS[skill_lower])

    # Check if skill appears in any synonym list
    for key, synonyms in SKILL_SYNONYMS.items():
        if skill_lower in synonyms or skill_lower == key:
            related.add(key)
            related.update(synonyms)

    return related


def calculate_skill_match_score(
    consultant_skills: list[str],
    required_skills: list[str],
) -> float:
    """
    Calculate match score between consultant and required skills.

    Scoring:
    - Exact matches: 1.0 per skill
    - Partial matches (substring): 0.5 per skill
    - Related skills (synonym map): 0.3 per skill

    The score is normalized by max(len(consultant_skills), len(required_skills))
    to account for both specificity and breadth.

    Args:
        consultant_skills: List of skills the consultant has
        required_skills: List of skills required for the task

    Returns:
        Match score between 0.0 and 1.0
    """
    if not required_skills:
        return 1.0  # No requirements means any consultant matches
    if not consultant_skills:
        return 0.0

    # Normalize skills for comparison
    required_normalized = [s.lower().strip() for s in required_skills]
    consultant_normalized = [s.lower().strip() for s in consultant_skills]
    consultant_set = set(consultant_normalized)

    total_score = 0.0
    matched_consultant_skills = set()  # Track which consultant skills have been matched

    for req_skill in required_normalized:
        best_match_score = 0.0
        best_match_skill = None

        # Check for exact match (1.0 points)
        if req_skill in consultant_set:
            best_match_score = 1.0
            best_match_skill = req_skill
        else:
            # Check for partial matches (substring) (0.5 points)
            for cons_skill in consultant_normalized:
                if cons_skill in matched_consultant_skills:
                    continue
                if req_skill in cons_skill or cons_skill in req_skill:
                    if 0.5 > best_match_score:
                        best_match_score = 0.5
                        best_match_skill = cons_skill

            # Check for synonym matches (0.3 points)
            if best_match_score < 0.3:
                req_synonyms = _get_skill_synonyms(req_skill)
                for cons_skill in consultant_normalized:
                    if cons_skill in matched_consultant_skills:
                        continue
                    cons_synonyms = _get_skill_synonyms(cons_skill)
                    if req_synonyms & cons_synonyms:  # If there's overlap
                        if 0.3 > best_match_score:
                            best_match_score = 0.3
                            best_match_skill = cons_skill
                            break

        total_score += best_match_score
        if best_match_skill:
            matched_consultant_skills.add(best_match_skill)

    # Normalize by max of both lists to account for both specificity and breadth
    normalizer = max(len(consultant_normalized), len(required_normalized))
    return total_score / normalizer if normalizer > 0 else 0.0


def _calculate_skill_match(
    required_skills: list[str], consultant_skills: list[str]
) -> float:
    """
    Calculate how well consultant skills match required skills.

    This is a legacy wrapper for calculate_skill_match_score with swapped argument order.

    Returns a score between 0.0 and 1.0.
    """
    return calculate_skill_match_score(consultant_skills, required_skills)


def _calculate_request_similarity(request1: str, request2: str) -> float:
    """
    Calculate similarity between two requests using fuzzy matching.

    Returns a score between 0.0 and 1.0.
    """
    # Normalize for comparison
    r1 = re.sub(r"\s+", " ", request1.lower().strip())
    r2 = re.sub(r"\s+", " ", request2.lower().strip())

    return difflib.SequenceMatcher(None, r1, r2).ratio()


def register_consultant(
    consultant_id: str,
    name: str,
    request: str,
    skills: list[str],
    department: str,
    team: str | None = None,
    company_dir: Path | None = None,
) -> dict:
    """
    Register a new auto-consultant in org.json with type="auto-consultant".

    Args:
        consultant_id: Unique identifier for the consultant
        name: Display name for the consultant
        request: The original user request that triggered creation
        skills: List of skill strings the consultant possesses
        department: Department ID the consultant belongs to
        team: Optional team ID within the department
        company_dir: Optional company directory override for testing

    Returns:
        Dict with registration result and consultant details
    """
    # Validate consultant_id format
    if not re.match(r"^[a-z][a-z0-9-]*$", consultant_id):
        return {
            "success": False,
            "reason": "invalid_id",
            "message": f"Consultant ID must match pattern ^[a-z][a-z0-9-]*$, got: {consultant_id}",
        }

    # Validate department format (for data hygiene, though not used in paths)
    if not re.match(r"^[a-z][a-z0-9-]*$", department):
        return {
            "success": False,
            "reason": "invalid_department",
            "message": f"Department must match pattern ^[a-z][a-z0-9-]*$, got: {department}",
        }

    # Validate team format if provided
    if team and not re.match(r"^[a-z][a-z0-9-]*$", team):
        return {
            "success": False,
            "reason": "invalid_team",
            "message": f"Team must match pattern ^[a-z][a-z0-9-]*$, got: {team}",
        }

    org = _load_org(company_dir)
    employees = _get_employees(org)

    # Check if consultant already exists
    for emp in employees:
        if emp.get("id") == consultant_id:
            return {
                "success": False,
                "reason": "already_exists",
                "message": f"Consultant with ID '{consultant_id}' already exists",
                "existing_consultant": emp,
            }

    # Get project context
    project_context = get_current_project()
    project_id = project_context["project_id"] if project_context else get_project_id()

    # Create employee directory
    emp_dir = _get_employee_dir(consultant_id, company_dir)
    emp_dir.mkdir(parents=True, exist_ok=True)

    # Create initial memory file
    memory_path = emp_dir / "memory.md"
    now = _now_iso()
    memory_content = f"""# {name} Memory

## Context

**Created:** {now}
**Type:** auto-consultant
**Original Request:** {request}
**Skills:** {", ".join(skills)}

## Preferences

<!-- Preferences will be captured as the consultant works -->

## Recent Interactions

<!-- Recent interaction history will be recorded here -->
"""
    _write_file(memory_path, memory_content)

    # Create the consultant employee entry
    consultant = {
        "id": consultant_id,
        "name": name,
        "type": "auto-consultant",
        "department": department,
        "team": team,
        "status": "available",
        "capabilities": skills,
        "memoryPath": f".company/employees/{consultant_id}/memory.md",
        "projectAssignments": [project_id] if project_id else [],
        "currentProject": project_id,
        "hireDate": now,
        "autoCreated": True,
        "sourceRequest": request,
        "lastActive": now,
        "activationCount": 1,
    }

    # Add to employees list
    employees.append(consultant)
    _set_employees(org, employees)

    # Save org.json
    if not _save_org(org, company_dir):
        return {
            "success": False,
            "reason": "save_error",
            "message": "Failed to save org.json",
        }

    return {
        "success": True,
        "consultant_id": consultant_id,
        "name": name,
        "type": "auto-consultant",
        "department": department,
        "team": team,
        "skills": skills,
        "request": request,
        "project_id": project_id,
        "memory_path": str(memory_path),
        "message": f"Auto-consultant '{name}' registered successfully",
    }


def find_matching_consultant(
    request: str,
    required_skills: list[str] | None = None,
    company_dir: Path | None = None,
    include_archived: bool = True,
    threshold: float | None = None,
) -> dict | None:
    """
    Search org.json and archives for matching consultant using skill matching.

    The algorithm:
    1. Extract skills from request if not provided
    2. Search active auto-consultants (status="available")
    3. Search archived consultants
    4. Score each by skill match using calculate_skill_match_score()
    5. Return best match above threshold, or None

    Args:
        request: The user request to match against
        required_skills: List of required skill strings (extracted from request if None)
        company_dir: Optional company directory override for testing
        include_archived: Whether to search archived consultants (default: True)
        threshold: Minimum skill match ratio (default: from config.json or 0.6)

    Returns:
        Dict with matching consultant info, or None if no match found
    """
    # Load threshold from config if not provided
    if threshold is None:
        config = _load_config(company_dir)
        threshold = config.get("agents", {}).get(
            "skillMatchThreshold", DEFAULT_SKILL_MATCH_THRESHOLD
        )

    # Extract skills from request if not provided
    if required_skills is None or len(required_skills) == 0:
        required_skills = extract_skills_from_request(request)

    # If still no skills, we can't match
    if not required_skills:
        return None

    org = _load_org(company_dir)
    employees = _get_employees(org)

    best_match = None
    best_score = 0.0

    # Search active auto-consultants (prefer available status)
    for emp in employees:
        if emp.get("type") != "auto-consultant":
            continue

        # Prioritize available consultants but don't exclude others
        status = emp.get("status", "available")
        status_bonus = 0.1 if status == "available" else 0.0

        skills = emp.get("capabilities", [])
        skill_score = calculate_skill_match_score(skills, required_skills)

        # Also consider request similarity if sourceRequest exists
        source_request = emp.get("sourceRequest", "")
        request_score = (
            _calculate_request_similarity(request, source_request)
            if source_request
            else 0.0
        )

        # Combined score: 70% skill match, 20% request similarity, 10% availability bonus
        combined_score = (skill_score * 0.7) + (request_score * 0.2) + status_bonus

        if combined_score > best_score and skill_score >= threshold:
            best_score = combined_score
            best_match = {
                "source": "active",
                "consultant": emp,
                "skill_score": skill_score,
                "request_score": request_score,
                "combined_score": combined_score,
                "extracted_skills": required_skills,
                "threshold_used": threshold,
            }

    # Search archives if enabled (with same algorithm)
    if include_archived:
        archive_dir = _get_archive_dir(company_dir)
        if archive_dir.exists():
            for archive_file in archive_dir.glob("*.md"):
                if archive_file.name == "TEMPLATE.md":
                    continue
                # Skip reactivated archives
                if ".reactivated." in archive_file.name:
                    continue

                content = _read_file(archive_file)
                if not content:
                    continue

                # Parse YAML frontmatter
                frontmatter_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                if not frontmatter_match:
                    continue

                frontmatter = frontmatter_match.group(1)

                # Extract fields
                archived_id_match = re.search(
                    r"^id:\s*(.+)$", frontmatter, re.MULTILINE
                )
                archived_name_match = re.search(
                    r"^name:\s*(.+)$", frontmatter, re.MULTILINE
                )
                archived_skills_match = re.search(
                    r"^skills_matched:\s*\[(.+)\]$", frontmatter, re.MULTILINE
                )
                # Match original_request with double quotes (escaped quotes handled)
                archived_request_match = re.search(
                    r'^original_request:\s*"((?:[^"\\]|\\.)*)"$',
                    frontmatter,
                    re.MULTILINE,
                )
                archived_dept_match = re.search(
                    r"^department:\s*(.+)$", frontmatter, re.MULTILINE
                )
                archived_team_match = re.search(
                    r"^team:\s*(.+)$", frontmatter, re.MULTILINE
                )
                archived_count_match = re.search(
                    r"^activation_count:\s*(\d+)$", frontmatter, re.MULTILINE
                )

                if not archived_id_match:
                    continue

                archived_id = archived_id_match.group(1).strip()
                archived_name = (
                    archived_name_match.group(1).strip()
                    if archived_name_match
                    else archived_id
                )
                archived_skills = []
                if archived_skills_match:
                    skills_str = archived_skills_match.group(1)
                    archived_skills = [
                        s.strip().strip("\"'") for s in skills_str.split(",")
                    ]

                archived_request_raw = (
                    archived_request_match.group(1) if archived_request_match else ""
                )
                # Unescape quotes that were escaped when writing
                archived_request = archived_request_raw.replace('\\"', '"')
                archived_dept = (
                    archived_dept_match.group(1).strip()
                    if archived_dept_match
                    else "engineering"
                )
                archived_team_raw = (
                    archived_team_match.group(1).strip()
                    if archived_team_match
                    else "null"
                )
                archived_team = (
                    None if archived_team_raw == "null" else archived_team_raw
                )
                archived_count = (
                    int(archived_count_match.group(1)) if archived_count_match else 0
                )

                skill_score = calculate_skill_match_score(
                    archived_skills, required_skills
                )
                request_score = (
                    _calculate_request_similarity(request, archived_request)
                    if archived_request
                    else 0.0
                )
                # Archived consultants get no availability bonus
                combined_score = (skill_score * 0.7) + (request_score * 0.2)

                if combined_score > best_score and skill_score >= threshold:
                    best_score = combined_score
                    best_match = {
                        "source": "archived",
                        "consultant": {
                            "id": archived_id,
                            "name": archived_name,
                            "type": "auto-consultant",
                            "capabilities": archived_skills,
                            "sourceRequest": archived_request,
                            "department": archived_dept,
                            "team": archived_team,
                            "activationCount": archived_count,
                        },
                        "archive_file": str(archive_file),
                        "skill_score": skill_score,
                        "request_score": request_score,
                        "combined_score": combined_score,
                        "extracted_skills": required_skills,
                        "threshold_used": threshold,
                    }

    return best_match


def archive_consultant(
    consultant_id: str,
    reason: str,
    archived_by: str = "system",
    company_dir: Path | None = None,
) -> dict:
    """
    Archive an auto-consultant: extract learnings, create archive file, remove from org.json.

    Args:
        consultant_id: ID of the consultant to archive
        reason: Reason for archiving (e.g., "Project complete", "Idle timeout")
        archived_by: Who initiated the archive (default: "system")
        company_dir: Optional company directory override for testing

    Returns:
        Dict with archive result and file path
    """
    org = _load_org(company_dir)
    employees = _get_employees(org)

    # Find the consultant
    consultant = None
    consultant_index = None
    for i, emp in enumerate(employees):
        if emp.get("id") == consultant_id:
            consultant = emp
            consultant_index = i
            break

    if consultant is None:
        return {
            "success": False,
            "reason": "not_found",
            "message": f"Consultant '{consultant_id}' not found in org.json",
        }

    if consultant.get("type") != "auto-consultant":
        return {
            "success": False,
            "reason": "not_auto_consultant",
            "message": f"Employee '{consultant_id}' is not an auto-consultant (type: {consultant.get('type')})",
        }

    # Extract learnings before archiving
    learnings = extract_learnings(consultant_id)

    # Read memory file for snapshot
    emp_dir = _get_employee_dir(consultant_id, company_dir)
    memory_path = emp_dir / "memory.md"
    memory_content = _read_file(memory_path) or "<!-- No memory content -->"

    # Prepare archive content
    now = _now_iso()
    # Escape quotes in source request for YAML frontmatter
    source_request = consultant.get("sourceRequest", "").replace('"', '\\"')
    archive_content = f"""---
id: {consultant_id}
name: {consultant.get("name", consultant_id)}
type: auto-consultant
archived: {now}
created: {consultant.get("hireDate", "unknown")}
last_active: {consultant.get("lastActive", "unknown")}
activation_count: {consultant.get("activationCount", 0)}
original_request: "{source_request}"
skills_matched: {json.dumps(consultant.get("capabilities", []))}
department: {consultant.get("department", "engineering")}
team: {consultant.get("team") or "null"}
---

# Archived Consultant: {consultant.get("name", consultant_id)}

## Archive Metadata

### Archive Information

- **Archived Date:** {now}
- **Archived By:** {archived_by}
- **Archive Reason:** {reason}

### Reactivation Notes

- **Recommended For:** Tasks matching skills: {", ".join(consultant.get("capabilities", []))}
- **Considerations:** Review memory snapshot for domain context
- **Dependencies:** See memory snapshot for project-specific knowledge

---

## Agent Definition

### Role

{consultant.get("name", consultant_id)} - Auto-created specialist consultant.

### Capabilities

"""

    for skill in consultant.get("capabilities", []):
        archive_content += f"- {skill}\n"

    archive_content += f"""
### Operating Parameters

- **Type:** auto-consultant
- **Department:** {consultant.get("department", "engineering")}
- **Team:** {consultant.get("team") or "None"}

---

## Memory Snapshot

> State of consultant's memory at time of archival.

{memory_content}

---

## Extracted Learnings

"""

    if learnings.get("success") and learnings.get("extracted"):
        extracted = learnings["extracted"]
        if extracted.get("patterns"):
            archive_content += "### Patterns Identified\n\n"
            for p in extracted["patterns"]:
                archive_content += (
                    f"- {p.get('content', p.get('name', 'Unknown pattern'))}\n"
                )
            archive_content += "\n"

        if extracted.get("decisions"):
            archive_content += "### Decisions Made\n\n"
            for d in extracted["decisions"]:
                archive_content += f"- {d.get('content', 'Unknown decision')}\n"
            archive_content += "\n"

        if extracted.get("mistakes"):
            archive_content += "### Lessons Learned\n\n"
            for m in extracted["mistakes"]:
                archive_content += (
                    f"- {m.get('title', 'Unknown')}: {m.get('lesson', '')}\n"
                )
            archive_content += "\n"
    else:
        archive_content += "_No learnings extracted._\n"

    archive_content += f"""
---

## Activation History

### Summary

- **Total Activations:** {consultant.get("activationCount", 0)}
- **First Activated:** {consultant.get("hireDate", "unknown")}
- **Last Activated:** {consultant.get("lastActive", "unknown")}
- **Original Request:** {consultant.get("sourceRequest", "Unknown")}
"""

    # Write archive file
    archive_dir = _get_archive_dir(company_dir)
    archive_file = archive_dir / f"{consultant_id}.md"
    if not _write_file(archive_file, archive_content):
        return {
            "success": False,
            "reason": "write_error",
            "message": f"Failed to write archive file: {archive_file}",
        }

    # Remove from org.json
    employees.pop(consultant_index)
    _set_employees(org, employees)

    if not _save_org(org, company_dir):
        return {
            "success": False,
            "reason": "save_error",
            "message": "Failed to save org.json after removing consultant",
        }

    return {
        "success": True,
        "consultant_id": consultant_id,
        "archive_file": str(archive_file),
        "reason": reason,
        "archived_by": archived_by,
        "learnings_extracted": learnings.get("success", False),
        "learnings_count": learnings.get("total_items", 0),
        "message": f"Consultant '{consultant_id}' archived successfully",
    }


def reactivate_consultant(
    consultant_id: str,
    new_request: str,
    company_dir: Path | None = None,
) -> dict:
    """
    Re-activate archived consultant: restore to org.json, update activation count.

    Args:
        consultant_id: ID of the archived consultant to reactivate
        new_request: The new request that triggered reactivation
        company_dir: Optional company directory override for testing

    Returns:
        Dict with reactivation result and updated consultant details
    """
    # Check if already active
    org = _load_org(company_dir)
    employees = _get_employees(org)

    for emp in employees:
        if emp.get("id") == consultant_id:
            return {
                "success": False,
                "reason": "already_active",
                "message": f"Consultant '{consultant_id}' is already active",
                "consultant": emp,
            }

    # Find archive file
    archive_dir = _get_archive_dir(company_dir)
    archive_file = archive_dir / f"{consultant_id}.md"

    if not archive_file.exists():
        return {
            "success": False,
            "reason": "archive_not_found",
            "message": f"No archive found for consultant '{consultant_id}'",
        }

    # Parse archive file
    content = _read_file(archive_file)
    if not content:
        return {
            "success": False,
            "reason": "read_error",
            "message": f"Failed to read archive file: {archive_file}",
        }

    # Extract frontmatter
    frontmatter_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not frontmatter_match:
        return {
            "success": False,
            "reason": "parse_error",
            "message": "Failed to parse archive frontmatter",
        }

    frontmatter = frontmatter_match.group(1)

    # Extract fields
    name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
    created_match = re.search(r"^created:\s*(.+)$", frontmatter, re.MULTILINE)
    count_match = re.search(r"^activation_count:\s*(\d+)$", frontmatter, re.MULTILINE)
    request_match = re.search(
        r'^original_request:\s*"((?:[^"\\]|\\.)*)"$', frontmatter, re.MULTILINE
    )
    skills_match = re.search(r"^skills_matched:\s*\[(.+)\]$", frontmatter, re.MULTILINE)
    dept_match = re.search(r"^department:\s*(.+)$", frontmatter, re.MULTILINE)
    team_match = re.search(r"^team:\s*(.+)$", frontmatter, re.MULTILINE)

    name = name_match.group(1).strip() if name_match else consultant_id
    created = created_match.group(1).strip() if created_match else _now_iso()
    activation_count = int(count_match.group(1)) if count_match else 0
    original_request_raw = request_match.group(1) if request_match else ""
    original_request = original_request_raw.replace('\\"', '"')  # Unescape quotes
    skills = []
    if skills_match:
        skills_str = skills_match.group(1)
        skills = [s.strip().strip("\"'") for s in skills_str.split(",")]
    department = dept_match.group(1).strip() if dept_match else "engineering"
    team_raw = team_match.group(1).strip() if team_match else "null"
    team = None if team_raw == "null" else team_raw

    # Get project context
    project_context = get_current_project()
    project_id = project_context["project_id"] if project_context else get_project_id()

    now = _now_iso()

    # Create reactivated consultant entry
    consultant = {
        "id": consultant_id,
        "name": name,
        "type": "auto-consultant",
        "department": department,
        "team": team,
        "status": "available",
        "capabilities": skills,
        "memoryPath": f".company/employees/{consultant_id}/memory.md",
        "projectAssignments": [project_id] if project_id else [],
        "currentProject": project_id,
        "hireDate": created,
        "autoCreated": True,
        "sourceRequest": original_request,
        "lastActive": now,
        "activationCount": activation_count + 1,
    }

    # Add to employees list
    employees.append(consultant)
    _set_employees(org, employees)

    if not _save_org(org, company_dir):
        return {
            "success": False,
            "reason": "save_error",
            "message": "Failed to save org.json",
        }

    # Update memory file with reactivation note
    emp_dir = _get_employee_dir(consultant_id, company_dir)
    memory_path = emp_dir / "memory.md"
    memory_content = _read_file(memory_path) or ""

    reactivation_note = f"""

---

## Reactivation #{activation_count + 1}: {now}

**New Request:** {new_request}

**Reactivated From Archive**

"""

    memory_content += reactivation_note
    _write_file(memory_path, memory_content)

    # Optionally remove or rename archive file
    # For now, keep it as historical record but mark as reactivated
    archive_backup = archive_dir / f"{consultant_id}.reactivated.md"
    try:
        archive_file.rename(archive_backup)
    except OSError:
        pass  # Not critical if this fails

    return {
        "success": True,
        "consultant_id": consultant_id,
        "name": name,
        "activation_count": activation_count + 1,
        "new_request": new_request,
        "project_id": project_id,
        "skills": skills,
        "message": f"Consultant '{name}' reactivated (activation #{activation_count + 1})",
    }


def update_consultant_status(
    consultant_id: str,
    status: str,
    context: str | None = None,
    company_dir: Path | None = None,
) -> dict:
    """
    Update consultant status (called by SubagentStop hook).

    Args:
        consultant_id: ID of the consultant to update
        status: New status (available, busy, blocked, offline)
        context: Optional context about the status change
        company_dir: Optional company directory override for testing

    Returns:
        Dict with update result
    """
    valid_statuses = {"available", "busy", "blocked", "offline"}
    if status not in valid_statuses:
        return {
            "success": False,
            "reason": "invalid_status",
            "message": f"Status must be one of {valid_statuses}, got: {status}",
        }

    org = _load_org(company_dir)
    employees = _get_employees(org)

    # Find the consultant
    consultant = None
    for emp in employees:
        if emp.get("id") == consultant_id:
            consultant = emp
            break

    if consultant is None:
        return {
            "success": False,
            "reason": "not_found",
            "message": f"Consultant '{consultant_id}' not found",
        }

    old_status = consultant.get("status", "unknown")
    now = _now_iso()

    # Update fields
    consultant["status"] = status
    consultant["lastActive"] = now

    # Save org.json
    _set_employees(org, employees)
    if not _save_org(org, company_dir):
        return {
            "success": False,
            "reason": "save_error",
            "message": "Failed to save org.json",
        }

    # Optionally update memory file with context
    if context:
        emp_dir = _get_employee_dir(consultant_id, company_dir)
        memory_path = emp_dir / "memory.md"
        memory_content = _read_file(memory_path) or ""

        status_note = f"\n\n### Status Update: {now}\n\n**Status:** {old_status} -> {status}\n\n**Context:** {context}\n"
        memory_content += status_note
        _write_file(memory_path, memory_content)

    return {
        "success": True,
        "consultant_id": consultant_id,
        "old_status": old_status,
        "new_status": status,
        "context": context,
        "last_active": now,
        "message": f"Consultant '{consultant_id}' status updated: {old_status} -> {status}",
    }


def get_consultant_context(
    consultant_id: str,
    company_dir: Path | None = None,
) -> dict:
    """
    Get full context for subagent initialization.

    Args:
        consultant_id: ID of the consultant
        company_dir: Optional company directory override for testing

    Returns:
        Dict with full consultant context including memory, skills, and history
    """
    org = _load_org(company_dir)
    employees = _get_employees(org)

    # Find the consultant
    consultant = None
    for emp in employees:
        if emp.get("id") == consultant_id:
            consultant = emp
            break

    if consultant is None:
        return {
            "success": False,
            "reason": "not_found",
            "message": f"Consultant '{consultant_id}' not found",
        }

    # Read memory file
    emp_dir = _get_employee_dir(consultant_id, company_dir)
    memory_path = emp_dir / "memory.md"
    memory_content = _read_file(memory_path)

    # Read learnings file if exists
    learnings_path = emp_dir / "learnings.md"
    learnings_content = _read_file(learnings_path)

    # Get project context
    project_context = get_current_project()

    return {
        "success": True,
        "consultant_id": consultant_id,
        "consultant": consultant,
        "name": consultant.get("name", consultant_id),
        "type": consultant.get("type", "auto-consultant"),
        "status": consultant.get("status", "unknown"),
        "department": consultant.get("department", "engineering"),
        "team": consultant.get("team"),
        "capabilities": consultant.get("capabilities", []),
        "source_request": consultant.get("sourceRequest"),
        "activation_count": consultant.get("activationCount", 0),
        "last_active": consultant.get("lastActive"),
        "hire_date": consultant.get("hireDate"),
        "current_project": consultant.get("currentProject"),
        "project_assignments": consultant.get("projectAssignments", []),
        "memory": {
            "path": str(memory_path),
            "content": memory_content,
            "exists": memory_content is not None,
        },
        "learnings": {
            "path": str(learnings_path),
            "content": learnings_content,
            "exists": learnings_content is not None,
        },
        "project_context": project_context,
        "multi_project_mode": is_multi_project_mode(),
    }


def check_idle_consultants(
    timeout_hours: float | None = None,
    company_dir: Path | None = None,
) -> list[str]:
    """
    Find auto-consultants idle past threshold.

    Args:
        timeout_hours: Idle timeout in hours (default from config.json)
        company_dir: Optional company directory override for testing

    Returns:
        List of consultant IDs that are idle past threshold
    """
    # Get timeout from config if not provided
    if timeout_hours is None:
        config = _load_config(company_dir)
        timeout_hours = config.get("agents", {}).get(
            "consultantIdleTimeout", DEFAULT_IDLE_TIMEOUT_HOURS
        )

    org = _load_org(company_dir)
    employees = _get_employees(org)

    now = datetime.now(timezone.utc)
    idle_consultants = []

    for emp in employees:
        if emp.get("type") != "auto-consultant":
            continue

        last_active_str = emp.get("lastActive")
        if not last_active_str:
            # If no lastActive, use hireDate
            last_active_str = emp.get("hireDate")

        if not last_active_str:
            # No timestamp at all - consider idle
            idle_consultants.append(emp.get("id"))
            continue

        try:
            # Parse ISO timestamp
            last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            idle_hours = (now - last_active).total_seconds() / 3600

            if idle_hours > timeout_hours:
                idle_consultants.append(emp.get("id"))
        except (ValueError, TypeError):
            # Can't parse timestamp - consider idle
            idle_consultants.append(emp.get("id"))

    return idle_consultants


def print_help():
    """Print usage help."""
    help_text = """
Consultant Lifecycle Utility — Manage auto-consultant operations

Commands:
    register     Register a new auto-consultant
    find         Find matching consultant for a request
    archive      Archive an auto-consultant
    reactivate   Reactivate an archived consultant
    status       Update consultant status
    context      Get consultant context for initialization
    check-idle   Find consultants idle past threshold

Register options:
    --id ID              Unique consultant ID (required)
    --name NAME          Display name (required)
    --request TEXT       Original user request (required)
    --skills SKILLS      Comma-separated skills (required)
    --department DEPT    Department ID (required)
    --team TEAM          Team ID (optional)

Find options:
    --request TEXT       User request to match (required)
    --skills SKILLS      Comma-separated required skills (required)
    --no-archived        Don't search archived consultants

Archive options:
    --id ID              Consultant ID to archive (required)
    --reason TEXT        Reason for archiving (required)
    --by WHO             Who is archiving (default: system)

Reactivate options:
    --id ID              Archived consultant ID (required)
    --request TEXT       New request triggering reactivation (required)

Status options:
    --id ID              Consultant ID (required)
    --status STATUS      New status: available|busy|blocked|offline (required)
    --context TEXT       Optional context for the change

Context options:
    --id ID              Consultant ID (required)

Check-idle options:
    --timeout HOURS      Idle threshold in hours (default: from config)

Examples:
    # Register new consultant
    python consultant_lifecycle.py register --id graphql-expert --name "GraphQL Expert" \\
        --request "Help with GraphQL schema" --skills "graphql,api,typescript" \\
        --department engineering

    # Find matching consultant
    python consultant_lifecycle.py find --request "Need GraphQL help" --skills "graphql,api"

    # Archive inactive consultant
    python consultant_lifecycle.py archive --id graphql-expert --reason "Project complete"

    # Reactivate archived consultant
    python consultant_lifecycle.py reactivate --id graphql-expert --request "New schema work"

    # Update status
    python consultant_lifecycle.py status --id graphql-expert --status busy \\
        --context "Working on schema migration"

    # Get full context
    python consultant_lifecycle.py context --id graphql-expert

    # Check for idle consultants
    python consultant_lifecycle.py check-idle --timeout 24

Output: JSON with operation status and details.
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

    command = sys.argv[1].lower().replace("-", "_")
    args = parse_args(sys.argv[2:])

    if command in ("help", "__help", "_h"):
        print_help()
        sys.exit(0)

    try:
        if command == "register":
            required = ["id", "name", "request", "skills", "department"]
            missing = [r for r in required if r not in args]
            if missing:
                print(
                    f"Error: Missing required options: {', '.join('--' + m for m in missing)}"
                )
                sys.exit(1)

            skills = [s.strip() for s in args["skills"].split(",")]
            result = register_consultant(
                consultant_id=args["id"],
                name=args["name"],
                request=args["request"],
                skills=skills,
                department=args["department"],
                team=args.get("team"),
            )
            print(json.dumps(result, indent=2))

        elif command == "find":
            if "request" not in args or "skills" not in args:
                print("Error: --request and --skills are required")
                sys.exit(1)

            skills = [s.strip() for s in args["skills"].split(",")]
            include_archived = not args.get("no_archived", False)
            result = find_matching_consultant(
                request=args["request"],
                required_skills=skills,
                include_archived=include_archived,
            )

            if result:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "reason": "no_match",
                            "message": "No matching consultant found",
                        }
                    )
                )

        elif command == "archive":
            if "id" not in args or "reason" not in args:
                print("Error: --id and --reason are required")
                sys.exit(1)

            result = archive_consultant(
                consultant_id=args["id"],
                reason=args["reason"],
                archived_by=args.get("by", "system"),
            )
            print(json.dumps(result, indent=2))

        elif command == "reactivate":
            if "id" not in args or "request" not in args:
                print("Error: --id and --request are required")
                sys.exit(1)

            result = reactivate_consultant(
                consultant_id=args["id"],
                new_request=args["request"],
            )
            print(json.dumps(result, indent=2))

        elif command == "status":
            if "id" not in args or "status" not in args:
                print("Error: --id and --status are required")
                sys.exit(1)

            result = update_consultant_status(
                consultant_id=args["id"],
                status=args["status"],
                context=args.get("context"),
            )
            print(json.dumps(result, indent=2))

        elif command == "context":
            if "id" not in args:
                print("Error: --id is required")
                sys.exit(1)

            result = get_consultant_context(consultant_id=args["id"])
            print(json.dumps(result, indent=2, default=str))

        elif command == "check_idle":
            timeout = float(args["timeout"]) if "timeout" in args else None
            result = check_idle_consultants(timeout_hours=timeout)
            print(
                json.dumps(
                    {
                        "success": True,
                        "idle_consultants": result,
                        "count": len(result),
                        "timeout_hours": timeout or DEFAULT_IDLE_TIMEOUT_HOURS,
                    },
                    indent=2,
                )
            )

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
