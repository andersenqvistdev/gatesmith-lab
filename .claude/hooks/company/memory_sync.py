#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Agent Memory Sync — bidirectional sync between individual and shared knowledge.

Enables organizational learning by:
1. Individual→Shared: Push agent learnings to shared knowledge base
2. Shared→Individual: Pull relevant org knowledge into agent context
3. Relevance scoring to prevent knowledge bloat

Features:
- Sync agent learnings to shared patterns/decisions
- Inject relevant shared knowledge into agent context
- Score relevance based on context similarity
- Full bidirectional sync
- Deduplication with similarity threshold

Usage:
    # Push agent learnings to shared knowledge
    python memory_sync.py to-shared --agent-id senior-engineer

    # Pull relevant shared knowledge for agent context
    python memory_sync.py from-shared --agent-id senior-engineer --context "Building REST API"

    # Full bidirectional sync
    python memory_sync.py sync --agent-id senior-engineer --context "Database migration task"

    # Score relevance of a knowledge item
    python memory_sync.py score --text "Use connection pooling" --context "Building database layer"

    # Show help
    python memory_sync.py help
"""

import difflib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
try:
    from company_resolver import (
        find_company_root,
        get_company_dir,
        get_current_project,
        get_project_id,
        is_multi_project_mode,
    )
except ImportError:
    # Fallback if running standalone - import from same directory
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "company_resolver", Path(__file__).parent / "company_resolver.py"
    )
    company_resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(company_resolver)
    find_company_root = company_resolver.find_company_root
    get_company_dir = company_resolver.get_company_dir
    get_project_id = company_resolver.get_project_id
    get_current_project = company_resolver.get_current_project
    is_multi_project_mode = company_resolver.is_multi_project_mode


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Legacy paths (for backward compatibility)
LEGACY_COMPANY_DIR = ".company"
LEGACY_KNOWLEDGE_DIR = os.path.join(LEGACY_COMPANY_DIR, "knowledge")
LEGACY_AGENTS_DIR = os.path.join(LEGACY_COMPANY_DIR, "agents")

# New multi-project paths
EMPLOYEES_DIR = "employees"
KNOWLEDGE_DIR_NAME = "knowledge"

PATTERNS_FILE = "patterns.md"
DECISIONS_FILE = "decisions.md"

# Relevance thresholds
MIN_RELEVANCE_SCORE = 0.4  # Minimum score to include in context
HIGH_RELEVANCE_SCORE = 0.6  # High relevance threshold
SIMILARITY_THRESHOLD = 0.7  # For deduplication

# Maximum items to inject into context
MAX_CONTEXT_ITEMS = 5

# Token estimation
TOKENS_PER_ITEM = 200  # Estimated tokens per injected knowledge item


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class KnowledgeItem:
    """Represents a piece of knowledge (pattern or decision)."""

    item_type: str  # "pattern" or "decision"
    title: str
    content: str
    category: str = ""
    context: str = ""
    source: str = ""
    relevance_score: float = 0.0
    project_id: str = ""  # Project context for multi-project tracking
    metadata: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    direction: str  # "to_shared", "from_shared", or "bidirectional"
    agent_id: str
    items_pushed: int = 0
    items_pulled: int = 0
    items_skipped: int = 0
    duplicates_found: int = 0
    message: str = ""
    project_id: str = ""  # Current project context
    multi_project_mode: bool = False  # Whether in multi-project mode
    details: list = field(default_factory=list)


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def get_knowledge_dir() -> Path:
    """
    Get the knowledge directory path.

    In multi-project mode, uses company-level knowledge directory.
    In legacy mode, uses local .company/knowledge.
    """
    company_dir = get_company_dir()
    return company_dir / KNOWLEDGE_DIR_NAME


def get_patterns_path() -> Path:
    """Get patterns.md path."""
    return get_knowledge_dir() / PATTERNS_FILE


def get_decisions_path() -> Path:
    """Get decisions.md path."""
    return get_knowledge_dir() / DECISIONS_FILE


def get_employee_memory_path(employee_id: str, department: str | None = None) -> Path:
    """
    Get the path to an employee's memory file (new structure).

    In multi-project mode, employees are at company level in .company/employees/.
    Employees can be in department subdirectories or at root level.

    Args:
        employee_id: The employee ID
        department: Optional department (engineering, product, design)

    Returns:
        Path to the employee's memory.md file
    """
    company_dir = get_company_dir()
    employees_dir = company_dir / EMPLOYEES_DIR

    if department:
        # Check department subdirectory first
        dept_path = employees_dir / department / employee_id / "memory.md"
        if dept_path.parent.exists():
            return dept_path

    # Check direct employee path
    direct_path = employees_dir / employee_id / "memory.md"
    if direct_path.parent.exists():
        return direct_path

    # Check all departments for the employee
    for dept in ["engineering", "product", "design"]:
        dept_path = employees_dir / dept / employee_id / "memory.md"
        if dept_path.parent.exists():
            return dept_path

    # Default to direct path (for new employees)
    return employees_dir / employee_id / "memory.md"


def get_employee_file(employee_id: str, department: str | None = None) -> Path:
    """
    Get the path to an employee's single-file definition (TEMPLATE.md style).

    Some employees use single-file definitions at .company/employees/{id}.md
    rather than directory structure.

    Args:
        employee_id: The employee ID
        department: Optional department

    Returns:
        Path to the employee's .md file
    """
    company_dir = get_company_dir()
    employees_dir = company_dir / EMPLOYEES_DIR

    if department:
        dept_path = employees_dir / department / f"{employee_id}.md"
        if dept_path.exists():
            return dept_path

    direct_path = employees_dir / f"{employee_id}.md"
    if direct_path.exists():
        return direct_path

    for dept in ["engineering", "product", "design"]:
        dept_path = employees_dir / dept / f"{employee_id}.md"
        if dept_path.exists():
            return dept_path

    return employees_dir / f"{employee_id}.md"


def get_agent_memory_path(agent_id: str) -> Path:
    """
    Get the path to an agent's memory file (legacy structure).

    Maintains backward compatibility with v1.1 .company/agents/ structure.
    First checks for employee in new structure, then falls back to legacy.

    Args:
        agent_id: The agent ID

    Returns:
        Path to the agent's memory.md file
    """
    # First try new employee structure
    employee_path = get_employee_memory_path(agent_id)
    if employee_path.parent.exists():
        return employee_path

    # Fall back to legacy agents structure
    company_dir = get_company_dir()
    return company_dir / "agents" / agent_id / "memory.md"


def get_agent_learnings_path(agent_id: str) -> Path:
    """
    Get the path to an agent's learnings file (legacy structure).

    Maintains backward compatibility with v1.1 .company/agents/ structure.

    Args:
        agent_id: The agent ID

    Returns:
        Path to the agent's learnings.md file
    """
    company_dir = get_company_dir()
    return company_dir / "agents" / agent_id / "learnings.md"


def get_current_project_context() -> dict:
    """
    Get the current project context for memory updates.

    Returns:
        Dictionary with project_id, company_root, and multi_project_mode
    """
    project_info = get_current_project()
    if project_info:
        return {
            "project_id": project_info["project_id"],
            "company_root": str(project_info["company_root"]),
            "multi_project_mode": True,
        }

    return {
        "project_id": get_project_id(),
        "company_root": str(Path.cwd()),
        "multi_project_mode": False,
    }


def read_file_content(path: Path) -> str | None:
    """Read file content safely."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, IOError):
        return None


def write_file_content(path: Path, content: str) -> bool:
    """Write file content safely."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except (OSError, IOError):
        return False


def read_employee_memory(employee_id: str, department: str | None = None) -> str:
    """
    Read the full content of an employee's memory file.

    Args:
        employee_id: The employee ID
        department: Optional department (engineering, product, design)

    Returns:
        Memory file content as string, empty string if not found
    """
    path = get_employee_memory_path(employee_id, department)
    if path.exists():
        content = read_file_content(path)
        return content if content else ""
    return ""


def append_to_employee_memory(
    employee_id: str,
    section: str,
    content: str,
    department: str | None = None,
) -> bool:
    """
    Append content to a specific section of an employee's memory file.

    Supported sections:
    - "Recent Interactions" — Task execution records
    - "Active Assignments" — Current work items
    - "Scratchpad" — Temporary notes

    Args:
        employee_id: The employee ID
        section: Section header to append under (without ## prefix)
        content: Content to append
        department: Optional department

    Returns:
        True if successful, False otherwise
    """
    path = get_employee_memory_path(employee_id, department)

    if not path.exists():
        return False

    try:
        existing = read_file_content(path) or ""

        # Find the section header (## Section Name)
        section_marker = f"## {section}"

        if section_marker in existing:
            # Insert after the section header
            parts = existing.split(section_marker, 1)
            if len(parts) == 2:
                rest = parts[1]
                # Find the next section or end
                lines = rest.split("\n", 1)
                if len(lines) > 1:
                    new_content = (
                        parts[0] + section_marker + lines[0] + "\n" + content + lines[1]
                    )
                else:
                    new_content = parts[0] + section_marker + "\n" + content + rest
            else:
                new_content = existing + f"\n\n{section_marker}\n{content}"
        else:
            # Add section at the end
            new_content = existing + f"\n\n{section_marker}\n{content}"

        return write_file_content(path, new_content)

    except (OSError, IOError):
        return False


def normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"\s+", " ", text.lower().strip())


def calculate_similarity(text1: str, text2: str) -> float:
    """
    Calculate similarity ratio between two texts.
    Returns a value between 0.0 (no match) and 1.0 (exact match).
    """
    text1_normalized = normalize_text(text1)
    text2_normalized = normalize_text(text2)
    return difflib.SequenceMatcher(None, text1_normalized, text2_normalized).ratio()


def extract_keywords(text: str) -> set[str]:
    """Extract significant keywords from text."""
    # Remove common words and extract meaningful terms
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "when",
        "where",
        "why",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "up",
        "about",
        "into",
        "over",
        "after",
        "use",
        "using",
    }

    words = re.findall(r"\b[a-z]+\b", text.lower())
    return {w for w in words if len(w) > 2 and w not in stop_words}


# -----------------------------------------------------------------------------
# Relevance Scoring
# -----------------------------------------------------------------------------


def score_relevance(knowledge_item: KnowledgeItem | dict, context: str) -> float:
    """
    Score the relevance of a knowledge item to a given context.

    Uses multiple signals:
    1. Direct text similarity
    2. Keyword overlap
    3. Category/domain matching

    Args:
        knowledge_item: KnowledgeItem or dict with knowledge data
        context: The context string to match against

    Returns:
        Relevance score between 0.0 and 1.0
    """
    if isinstance(knowledge_item, dict):
        item_title = knowledge_item.get("title", "")
        item_content = knowledge_item.get("content", "")
        item_context = knowledge_item.get("context", "")
        item_category = knowledge_item.get("category", "")
    else:
        item_title = knowledge_item.title
        item_content = knowledge_item.content
        item_context = knowledge_item.context
        item_category = knowledge_item.category

    # Combine item text for comparison
    item_text = f"{item_title} {item_content} {item_context} {item_category}"

    # Signal 1: Direct text similarity (weight: 0.4)
    text_similarity = calculate_similarity(item_text, context)

    # Signal 2: Keyword overlap (weight: 0.4)
    item_keywords = extract_keywords(item_text)
    context_keywords = extract_keywords(context)

    if item_keywords and context_keywords:
        overlap = len(item_keywords & context_keywords)
        max_possible = min(len(item_keywords), len(context_keywords))
        keyword_score = overlap / max_possible if max_possible > 0 else 0
    else:
        keyword_score = 0

    # Signal 3: Category/domain bonus (weight: 0.2)
    category_bonus = 0
    if item_category:
        category_lower = item_category.lower()
        context_lower = context.lower()

        # Check for domain keywords in context
        domain_mappings = {
            "architecture": ["design", "structure", "system", "module", "component"],
            "code": ["implement", "function", "class", "method", "variable"],
            "testing": ["test", "spec", "coverage", "mock", "assert"],
            "security": ["auth", "encrypt", "password", "token", "permission"],
            "devops": ["deploy", "ci", "cd", "pipeline", "docker", "kubernetes"],
            "database": ["sql", "query", "migration", "schema", "postgres", "mysql"],
            "api": ["rest", "endpoint", "request", "response", "http"],
        }

        for domain, keywords in domain_mappings.items():
            if domain in category_lower or any(kw in context_lower for kw in keywords):
                if domain in category_lower:
                    category_bonus = 1.0
                    break

    # Weighted combination
    final_score = text_similarity * 0.4 + keyword_score * 0.4 + category_bonus * 0.2

    return min(1.0, max(0.0, final_score))


# -----------------------------------------------------------------------------
# Knowledge Extraction
# -----------------------------------------------------------------------------


def extract_patterns_from_shared() -> list[KnowledgeItem]:
    """Extract all patterns from shared knowledge base."""
    patterns_path = get_patterns_path()
    content = read_file_content(patterns_path)

    if not content:
        return []

    patterns = []

    # Match pattern sections: ### Pattern Name followed by content until next ###
    pattern_regex = r"###\s+([^\n]+)\n(.*?)(?=\n###|\n---|\Z)"
    matches = re.findall(pattern_regex, content, re.DOTALL)

    for name, body in matches:
        # Skip template sections
        if "[Pattern Name]" in name or name.strip().startswith("["):
            continue

        # Extract fields
        category_match = re.search(r"\*\*Category:\*\*\s*([^\n]+)", body)
        context_match = re.search(r"\*\*Context:\*\*\s*([^\n]+)", body)
        pattern_match = re.search(
            r"\*\*Pattern:\*\*\s*(.*?)(?=\n\*\*|\Z)", body, re.DOTALL
        )

        patterns.append(
            KnowledgeItem(
                item_type="pattern",
                title=name.strip(),
                content=pattern_match.group(1).strip() if pattern_match else "",
                category=category_match.group(1).strip() if category_match else "",
                context=context_match.group(1).strip() if context_match else "",
                source="shared_patterns",
            )
        )

    return patterns


def extract_decisions_from_shared() -> list[KnowledgeItem]:
    """Extract all decisions from shared knowledge base."""
    decisions_path = get_decisions_path()
    content = read_file_content(decisions_path)

    if not content:
        return []

    decisions = []

    # Match ADR sections: ## ADR-NNNN: Title followed by content until next ## ADR
    decision_regex = r"##\s+ADR-(\d+):\s+([^\n]+)\n(.*?)(?=\n##\s+ADR-|\Z)"
    matches = re.findall(decision_regex, content, re.DOTALL)

    for number, title, body in matches:
        # Skip template sections
        if "[Short Title]" in title or title.strip().startswith("["):
            continue

        # Extract fields
        context_match = re.search(r"###\s+Context\s*(.*?)(?=\n###|\Z)", body, re.DOTALL)
        decision_match = re.search(
            r"###\s+Decision\s*(.*?)(?=\n###|\Z)", body, re.DOTALL
        )
        consequences_match = re.search(
            r"###\s+Consequences\s*(.*?)(?=\n###|\Z)", body, re.DOTALL
        )

        decisions.append(
            KnowledgeItem(
                item_type="decision",
                title=title.strip(),
                content=decision_match.group(1).strip() if decision_match else "",
                context=context_match.group(1).strip() if context_match else "",
                source="shared_decisions",
                metadata={
                    "adr_number": int(number),
                    "consequences": consequences_match.group(1).strip()
                    if consequences_match
                    else "",
                },
            )
        )

    return decisions


def extract_learnings_from_employee(
    employee_id: str, department: str | None = None
) -> list[KnowledgeItem]:
    """
    Extract learnings from an employee's memory file (new structure).

    Supports the v1.2 employee structure with single-file definitions
    and the memory.md format with project experience and cross-project insights.

    Args:
        employee_id: The employee ID
        department: Optional department

    Returns:
        List of KnowledgeItem objects extracted from the employee
    """
    learnings = []
    project_ctx = get_current_project_context()

    # Try single-file employee definition first (TEMPLATE.md style)
    employee_file = get_employee_file(employee_id, department)
    employee_content = read_file_content(employee_file)

    # Also try directory-based memory.md
    memory_path = get_employee_memory_path(employee_id, department)
    memory_content = read_file_content(memory_path)

    content_to_parse = employee_content or memory_content

    if not content_to_parse:
        return learnings

    # Extract from Learnings section (single-file style)
    learnings_match = re.search(
        r"###?\s+Learnings\s*(.*?)(?=\n###?|\n---|\Z)", content_to_parse, re.DOTALL
    )
    if learnings_match:
        learnings_section = learnings_match.group(1)
        for line in learnings_section.split("\n"):
            line = line.strip()
            if line.startswith("-") and len(line) > 10 and not line.startswith("*No"):
                learnings.append(
                    KnowledgeItem(
                        item_type="pattern",
                        title=line[1:].strip()[:50] + "..."
                        if len(line) > 53
                        else line[1:].strip(),
                        content=line[1:].strip(),
                        source=f"employee:{employee_id}:learnings",
                        project_id=project_ctx["project_id"],
                        metadata={
                            "employee_id": employee_id,
                            "multi_project": project_ctx["multi_project_mode"],
                        },
                    )
                )

    # Extract from Cross-Project Insights section (new v1.2 structure)
    cross_project_match = re.search(
        r"##\s+Cross-Project Insights\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
        content_to_parse,
        re.DOTALL,
    )
    if cross_project_match:
        insights_section = cross_project_match.group(1)

        # Extract Technical Patterns
        tech_match = re.search(
            r"###\s+Technical Patterns\s*(.*?)(?=\n###|\Z)", insights_section, re.DOTALL
        )
        if tech_match:
            for line in tech_match.group(1).split("\n"):
                line = line.strip()
                if line.startswith("-") and len(line) > 10:
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=line[1:].strip()[:50] + "..."
                            if len(line) > 53
                            else line[1:].strip(),
                            content=line[1:].strip(),
                            category="Technical",
                            source=f"employee:{employee_id}:cross-project",
                            project_id=project_ctx["project_id"],
                            metadata={
                                "employee_id": employee_id,
                                "cross_project": True,
                            },
                        )
                    )

        # Extract Process Insights
        process_match = re.search(
            r"###\s+Process Insights\s*(.*?)(?=\n###|\Z)", insights_section, re.DOTALL
        )
        if process_match:
            for line in process_match.group(1).split("\n"):
                line = line.strip()
                if line.startswith("-") and len(line) > 10:
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=line[1:].strip()[:50] + "..."
                            if len(line) > 53
                            else line[1:].strip(),
                            content=line[1:].strip(),
                            category="Process",
                            source=f"employee:{employee_id}:cross-project",
                            project_id=project_ctx["project_id"],
                            metadata={
                                "employee_id": employee_id,
                                "cross_project": True,
                            },
                        )
                    )

        # Extract Domain Knowledge
        domain_match = re.search(
            r"###\s+Domain Knowledge\s*(.*?)(?=\n###|\Z)", insights_section, re.DOTALL
        )
        if domain_match:
            for line in domain_match.group(1).split("\n"):
                line = line.strip()
                if line.startswith("-") and len(line) > 10:
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=line[1:].strip()[:50] + "..."
                            if len(line) > 53
                            else line[1:].strip(),
                            content=line[1:].strip(),
                            category="Domain",
                            source=f"employee:{employee_id}:cross-project",
                            project_id=project_ctx["project_id"],
                            metadata={
                                "employee_id": employee_id,
                                "cross_project": True,
                                "is_domain": True,
                            },
                        )
                    )

    # Extract from Project Experience section (per-project learnings)
    project_exp_match = re.search(
        r"##\s+Project Experience\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
        content_to_parse,
        re.DOTALL,
    )
    if project_exp_match:
        exp_section = project_exp_match.group(1)
        # Parse each project entry
        project_entries = re.findall(
            r"###\s+([^\n]+)\n(.*?)(?=\n###\s+[a-z]|\n##|\Z)",
            exp_section,
            re.DOTALL | re.IGNORECASE,
        )
        for project_name, project_body in project_entries:
            if project_name.strip().startswith("["):
                continue

            # Extract learnings from this project
            proj_learnings_match = re.search(
                r"\*\*Learnings:\*\*\s*(.*?)(?=\n\*\*|\Z)", project_body, re.DOTALL
            )
            if proj_learnings_match:
                for line in proj_learnings_match.group(1).split("\n"):
                    line = line.strip()
                    if line.startswith("-") and len(line) > 10:
                        learnings.append(
                            KnowledgeItem(
                                item_type="pattern",
                                title=line[1:].strip()[:50] + "..."
                                if len(line) > 53
                                else line[1:].strip(),
                                content=line[1:].strip(),
                                source=f"employee:{employee_id}:project:{project_name.strip()}",
                                project_id=project_name.strip(),
                                metadata={
                                    "employee_id": employee_id,
                                    "from_project": project_name.strip(),
                                },
                            )
                        )

    # Extract from Preferences section
    preferences_match = re.search(
        r"###?\s+Preferences\s*(.*?)(?=\n###?|\n---|\Z)", content_to_parse, re.DOTALL
    )
    if preferences_match:
        preferences = preferences_match.group(1)
        for line in preferences.split("\n"):
            line = line.strip()
            if line.startswith("-") and len(line) > 10 and not line.startswith("*No"):
                learnings.append(
                    KnowledgeItem(
                        item_type="pattern",
                        title=line[1:].strip()[:50] + "..."
                        if len(line) > 53
                        else line[1:].strip(),
                        content=line[1:].strip(),
                        source=f"employee:{employee_id}:preferences",
                        project_id=project_ctx["project_id"],
                        metadata={"employee_id": employee_id},
                    )
                )

    return learnings


def extract_learnings_from_agent(agent_id: str) -> list[KnowledgeItem]:
    """
    Extract learnings from an agent/employee's memory and learnings files.

    Supports both legacy v1.1 agent structure (.company/agents/) and
    new v1.2 employee structure (.company/employees/).

    Args:
        agent_id: The agent/employee ID

    Returns:
        List of KnowledgeItem objects extracted
    """
    # First try new employee structure
    employee_learnings = extract_learnings_from_employee(agent_id)
    if employee_learnings:
        return employee_learnings

    # Fall back to legacy agent structure
    learnings = []
    project_ctx = get_current_project_context()

    memory_path = get_agent_memory_path(agent_id)
    learnings_path = get_agent_learnings_path(agent_id)

    memory_content = read_file_content(memory_path)
    learnings_content = read_file_content(learnings_path)

    # Parse memory content for patterns
    if memory_content:
        # Look for preferences section
        preferences_match = re.search(
            r"##\s+Preferences\s*(.*?)(?=\n##|\Z)", memory_content, re.DOTALL
        )
        if preferences_match:
            preferences = preferences_match.group(1)
            for line in preferences.split("\n"):
                line = line.strip()
                if line.startswith("-") and len(line) > 10:
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=line[1:].strip()[:50] + "..."
                            if len(line) > 53
                            else line[1:].strip(),
                            content=line[1:].strip(),
                            source=f"agent:{agent_id}:memory",
                            project_id=project_ctx["project_id"],
                            metadata={"agent_id": agent_id},
                        )
                    )

    # Parse learnings content
    if learnings_content:
        # Extract successful patterns
        patterns_match = re.search(
            r"##\s+Successful Patterns\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
            learnings_content,
            re.DOTALL,
        )
        if patterns_match:
            patterns_section = patterns_match.group(1)
            pattern_entries = re.findall(
                r"###\s+([^\n]+)\n(.*?)(?=\n###|\n##|\Z)",
                patterns_section,
                re.DOTALL,
            )
            for name, body in pattern_entries:
                if not name.strip().startswith("["):
                    context_match = re.search(r"\*\*Context:\*\*\s*([^\n]+)", body)
                    approach_match = re.search(
                        r"\*\*Approach:\*\*\s*(.*?)(?=\n\*\*|\Z)", body, re.DOTALL
                    )
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=name.strip(),
                            content=approach_match.group(1).strip()
                            if approach_match
                            else "",
                            context=context_match.group(1).strip()
                            if context_match
                            else "",
                            source=f"agent:{agent_id}:learnings",
                            project_id=project_ctx["project_id"],
                            metadata={"agent_id": agent_id},
                        )
                    )

        # Extract domain expertise as potential patterns
        domain_match = re.search(
            r"##\s+Domain Expertise\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
            learnings_content,
            re.DOTALL,
        )
        if domain_match:
            domain_section = domain_match.group(1)
            domain_entries = re.findall(
                r"###\s+([^\n]+)\n(.*?)(?=\n###|\n##|\Z)",
                domain_section,
                re.DOTALL,
            )
            for name, body in domain_entries:
                if not name.strip().startswith("["):
                    overview_match = re.search(r"\*\*Overview:\*\*\s*([^\n]+)", body)
                    learnings.append(
                        KnowledgeItem(
                            item_type="pattern",
                            title=f"Domain: {name.strip()}",
                            content=overview_match.group(1).strip()
                            if overview_match
                            else "",
                            category="Domain",
                            source=f"agent:{agent_id}:learnings",
                            project_id=project_ctx["project_id"],
                            metadata={"agent_id": agent_id, "is_domain": True},
                        )
                    )

    return learnings


# -----------------------------------------------------------------------------
# Sync Functions
# -----------------------------------------------------------------------------


def sync_to_shared(agent_id: str) -> SyncResult:
    """
    Push agent/employee learnings to shared knowledge base.

    Extracts learnings from agent/employee memory/learnings files and adds them
    to the shared patterns.md (with deduplication). In multi-project mode,
    syncs to company-level knowledge directory.

    Args:
        agent_id: The agent/employee ID to sync from

    Returns:
        SyncResult with sync details including project context
    """
    # Get project context for tracking
    project_ctx = get_current_project_context()

    # Extract agent/employee learnings
    agent_learnings = extract_learnings_from_agent(agent_id)

    if not agent_learnings:
        return SyncResult(
            success=True,
            direction="to_shared",
            agent_id=agent_id,
            message=f"No learnings found for agent/employee {agent_id}",
            project_id=project_ctx["project_id"],
            multi_project_mode=project_ctx["multi_project_mode"],
        )

    # Get existing shared patterns for deduplication
    shared_patterns = extract_patterns_from_shared()
    shared_texts = [f"{p.title} {p.content}" for p in shared_patterns]

    # Load patterns file for appending
    patterns_path = get_patterns_path()
    content = (
        read_file_content(patterns_path)
        or "# Implementation Patterns\n\n## Established Patterns\n"
    )

    items_pushed = 0
    duplicates_found = 0
    details = []

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for learning in agent_learnings:
        # Skip domain knowledge items (they're context, not patterns)
        if learning.metadata.get("is_domain"):
            continue

        # Check for duplicates
        learning_text = f"{learning.title} {learning.content}"
        is_duplicate = False

        for shared_text in shared_texts:
            if calculate_similarity(learning_text, shared_text) >= SIMILARITY_THRESHOLD:
                is_duplicate = True
                duplicates_found += 1
                details.append(
                    {
                        "action": "skipped_duplicate",
                        "title": learning.title,
                        "project_id": learning.project_id or project_ctx["project_id"],
                    }
                )
                break

        if is_duplicate:
            continue

        # Determine source label (employee vs agent)
        source_label = (
            f"Employee {agent_id}"
            if learning.source.startswith("employee:")
            else f"Agent {agent_id}"
        )

        # Include project context in the entry
        project_info = ""
        if project_ctx["multi_project_mode"]:
            source_project = learning.project_id or project_ctx["project_id"]
            project_info = f"\n**Project:** {source_project}"

        # Add to shared patterns
        new_entry = f"""
---

### {learning.title}

**Category:** {learning.category or "Code"}

**Context:** {learning.context or "General usage"}

**Pattern:**
{learning.content}

**Source:** {source_label}{project_info}

**Added:** {now}
"""
        content += new_entry
        shared_texts.append(learning_text)  # Add to dedup list
        items_pushed += 1
        details.append(
            {
                "action": "pushed",
                "title": learning.title,
                "project_id": learning.project_id or project_ctx["project_id"],
                "cross_project": learning.metadata.get("cross_project", False),
            }
        )

    # Write updated patterns
    if items_pushed > 0:
        if not write_file_content(patterns_path, content):
            return SyncResult(
                success=False,
                direction="to_shared",
                agent_id=agent_id,
                message="Failed to write to patterns.md",
                project_id=project_ctx["project_id"],
                multi_project_mode=project_ctx["multi_project_mode"],
            )

    return SyncResult(
        success=True,
        direction="to_shared",
        agent_id=agent_id,
        items_pushed=items_pushed,
        duplicates_found=duplicates_found,
        message=f"Pushed {items_pushed} learnings to shared knowledge ({duplicates_found} duplicates skipped)",
        project_id=project_ctx["project_id"],
        multi_project_mode=project_ctx["multi_project_mode"],
        details=details,
    )


def sync_from_shared(agent_id: str, context: str) -> SyncResult:
    """
    Pull relevant shared knowledge for agent/employee context.

    Finds relevant patterns and decisions from shared knowledge base
    and returns them for injection into agent context. In multi-project
    mode, pulls from company-level knowledge directory.

    Args:
        agent_id: The agent/employee ID requesting knowledge
        context: The context/task description to match against

    Returns:
        SyncResult with relevant knowledge items and project context
    """
    # Get project context for tracking
    project_ctx = get_current_project_context()

    # Early-exit for empty/short context
    if not context or len(context.strip()) < 10:
        return SyncResult(
            success=True,
            direction="from_shared",
            agent_id=agent_id,
            message="Context too short for knowledge matching (skipped)",
            project_id=project_ctx["project_id"],
            multi_project_mode=project_ctx["multi_project_mode"],
        )

    # Get all shared knowledge
    shared_patterns = extract_patterns_from_shared()
    shared_decisions = extract_decisions_from_shared()

    # Early-exit if no knowledge base exists
    if not shared_patterns and not shared_decisions:
        return SyncResult(
            success=True,
            direction="from_shared",
            agent_id=agent_id,
            message="No shared knowledge available (skipped)",
            project_id=project_ctx["project_id"],
            multi_project_mode=project_ctx["multi_project_mode"],
        )

    all_knowledge = shared_patterns + shared_decisions

    # Score and filter by relevance
    scored_items = []
    for item in all_knowledge:
        score = score_relevance(item, context)
        if score >= MIN_RELEVANCE_SCORE:
            item.relevance_score = score
            scored_items.append(item)

    # Sort by relevance and limit
    scored_items.sort(key=lambda x: x.relevance_score, reverse=True)
    selected_items = scored_items[:MAX_CONTEXT_ITEMS]

    # Build details for response
    details = []
    for item in selected_items:
        details.append(
            {
                "type": item.item_type,
                "title": item.title,
                "content": item.content,
                "context": item.context,
                "category": item.category,
                "project_id": item.project_id,  # Include source project
                "relevance_score": round(item.relevance_score, 3),
                "is_high_relevance": item.relevance_score >= HIGH_RELEVANCE_SCORE,
            }
        )

    return SyncResult(
        success=True,
        direction="from_shared",
        agent_id=agent_id,
        items_pulled=len(selected_items),
        items_skipped=len(all_knowledge) - len(scored_items),
        message=f"Found {len(selected_items)} relevant items (~{len(selected_items) * TOKENS_PER_ITEM} tokens)",
        project_id=project_ctx["project_id"],
        multi_project_mode=project_ctx["multi_project_mode"],
        details=details,
    )


def full_sync(agent_id: str, context: str) -> SyncResult:
    """
    Perform full bidirectional sync.

    1. Push agent/employee learnings to shared knowledge
    2. Pull relevant shared knowledge for agent/employee

    In multi-project mode, syncs to/from company-level knowledge
    directory and includes project context in all updates.

    Args:
        agent_id: The agent/employee ID to sync
        context: The context for pulling relevant knowledge

    Returns:
        SyncResult with combined sync details and project context
    """
    # Get project context
    project_ctx = get_current_project_context()

    # Push to shared
    push_result = sync_to_shared(agent_id)

    # Pull from shared
    pull_result = sync_from_shared(agent_id, context)

    # Combine results
    combined_details = [
        {"phase": "push", "details": push_result.details},
        {"phase": "pull", "details": pull_result.details},
    ]

    return SyncResult(
        success=push_result.success and pull_result.success,
        direction="bidirectional",
        agent_id=agent_id,
        items_pushed=push_result.items_pushed,
        items_pulled=pull_result.items_pulled,
        items_skipped=push_result.items_skipped + pull_result.items_skipped,
        duplicates_found=push_result.duplicates_found,
        message=f"Synced: pushed {push_result.items_pushed}, pulled {pull_result.items_pulled} items",
        project_id=project_ctx["project_id"],
        multi_project_mode=project_ctx["multi_project_mode"],
        details=combined_details,
    )


# -----------------------------------------------------------------------------
# Context Injection
# -----------------------------------------------------------------------------


def format_knowledge_for_context(sync_result: SyncResult) -> str:
    """
    Format pulled knowledge items into a context string for agent injection.

    In multi-project mode, includes project source information to enable
    cross-project knowledge sharing.

    Args:
        sync_result: Result from sync_from_shared or full_sync

    Returns:
        Formatted markdown string for context injection
    """
    if not sync_result.details:
        return ""

    # Handle bidirectional sync (has nested structure)
    if sync_result.direction == "bidirectional":
        items = []
        for phase in sync_result.details:
            if phase.get("phase") == "pull":
                items = phase.get("details", [])
                break
    else:
        items = sync_result.details

    if not items:
        return ""

    # Include multi-project context in header if applicable
    if sync_result.multi_project_mode:
        lines = [
            "## Relevant Organizational Knowledge\n",
            f"_Current project: {sync_result.project_id} | Multi-project mode: enabled_\n",
        ]
    else:
        lines = ["## Relevant Organizational Knowledge\n"]

    # Group by high/normal relevance
    high_relevance = [i for i in items if i.get("is_high_relevance")]
    normal_relevance = [i for i in items if not i.get("is_high_relevance")]

    if high_relevance:
        lines.append("### Highly Relevant\n")
        for item in high_relevance:
            # Include project source if available
            project_info = ""
            if item.get("project_id"):
                project_info = f" [from: {item['project_id']}]"
            lines.append(f"**{item['title']}** ({item['type']}){project_info}")
            if item.get("content"):
                lines.append(f"> {item['content'][:200]}...")
            if item.get("context"):
                lines.append(f"_Context: {item['context']}_")
            lines.append("")

    if normal_relevance:
        lines.append("### Also Relevant\n")
        for item in normal_relevance:
            project_info = ""
            if item.get("project_id"):
                project_info = f" [{item['project_id']}]"
            lines.append(
                f"- **{item['title']}**: {item.get('content', '')[:100]}...{project_info}"
            )

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Agent Memory Sync — Bidirectional sync between individual and shared knowledge

Supports both legacy agent structure (.company/agents/) and new employee
structure (.company/employees/). In multi-project mode, syncs to company-level
knowledge directory and includes project context in all updates.

Commands:
    to-shared       Push agent/employee learnings to shared knowledge
    from-shared     Pull relevant shared knowledge for agent/employee
    sync            Full bidirectional sync
    score           Score relevance of knowledge to context
    format          Format pulled knowledge for context injection
    project         Show current project context

to-shared options:
    --agent-id ID       Agent/employee ID to sync from (required)
    --department DEPT   Department (engineering, product, design) for employees

from-shared options:
    --agent-id ID       Agent/employee ID requesting knowledge (required)
    --context TEXT      Context/task to match against (required)

sync options:
    --agent-id ID       Agent/employee ID to sync (required)
    --context TEXT      Context for pulling relevant knowledge (required)

score options:
    --text TEXT         Knowledge text to score (required)
    --context TEXT      Context to score against (required)

Examples:
    # Push agent/employee learnings to shared knowledge
    python memory_sync.py to-shared --agent-id senior-engineer

    # Push employee learnings from engineering department
    python memory_sync.py to-shared --agent-id alice --department engineering

    # Pull relevant shared knowledge for a task
    python memory_sync.py from-shared --agent-id senior-engineer \\
        --context "Building REST API with authentication"

    # Full bidirectional sync
    python memory_sync.py sync --agent-id senior-engineer \\
        --context "Database migration task"

    # Score relevance
    python memory_sync.py score \\
        --text "Use connection pooling for database efficiency" \\
        --context "Building database layer with PostgreSQL"

    # Show current project context
    python memory_sync.py project

Output: JSON with sync details, project context, and relevant knowledge items.

Multi-Project Mode:
    - Automatically detected via .forge-company-root marker file
    - Syncs to/from company-level .company/knowledge/ directory
    - Includes project_id in all sync results
    - Supports cross-project knowledge sharing

Relevance Scoring:
    - Scores range from 0.0 to 1.0
    - Minimum threshold for inclusion: 0.3
    - High relevance threshold: 0.6
    - Uses text similarity, keyword overlap, and domain matching
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
        if command == "to_shared":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)

            result = sync_to_shared(args["agent_id"])
            print(json.dumps(asdict(result), indent=2))

        elif command == "from_shared":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)
            if "context" not in args:
                print("Error: --context required")
                sys.exit(1)

            result = sync_from_shared(args["agent_id"], args["context"])
            print(json.dumps(asdict(result), indent=2))

        elif command == "sync":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)
            if "context" not in args:
                print("Error: --context required")
                sys.exit(1)

            result = full_sync(args["agent_id"], args["context"])
            print(json.dumps(asdict(result), indent=2))

        elif command == "score":
            if "text" not in args:
                print("Error: --text required")
                sys.exit(1)
            if "context" not in args:
                print("Error: --context required")
                sys.exit(1)

            # Create a simple knowledge item for scoring
            item = KnowledgeItem(
                item_type="test",
                title="Test Item",
                content=args["text"],
            )
            relevance = score_relevance(item, args["context"])

            print(
                json.dumps(
                    {
                        "success": True,
                        "text": args["text"],
                        "context": args["context"],
                        "relevance_score": round(relevance, 3),
                        "meets_minimum": relevance >= MIN_RELEVANCE_SCORE,
                        "is_high_relevance": relevance >= HIGH_RELEVANCE_SCORE,
                    },
                    indent=2,
                )
            )

        elif command == "format":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)
            if "context" not in args:
                print("Error: --context required")
                sys.exit(1)

            # Get knowledge and format it
            result = sync_from_shared(args["agent_id"], args["context"])
            formatted = format_knowledge_for_context(result)

            print(
                json.dumps(
                    {
                        "success": True,
                        "agent_id": args["agent_id"],
                        "items_found": result.items_pulled,
                        "project_id": result.project_id,
                        "multi_project_mode": result.multi_project_mode,
                        "formatted_context": formatted,
                    },
                    indent=2,
                )
            )

        elif command == "project":
            # Show current project context
            project_ctx = get_current_project_context()
            company_dir = get_company_dir()

            print(
                json.dumps(
                    {
                        "success": True,
                        "project_id": project_ctx["project_id"],
                        "company_root": project_ctx["company_root"],
                        "multi_project_mode": project_ctx["multi_project_mode"],
                        "company_dir": str(company_dir),
                        "knowledge_dir": str(get_knowledge_dir()),
                        "employees_dir": str(company_dir / EMPLOYEES_DIR),
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
