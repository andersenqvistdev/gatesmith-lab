#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Knowledge Capture Utility — extracts learnings from completed work.

Reads agent memory files, identifies patterns (repeated successes), decisions
(architectural choices), and mistakes. Appends to .company/knowledge/patterns.md
and decisions.md with deduplication.

Features:
- Extract learnings from agent memory files
- Capture learnings from completed tasks
- Add patterns with deduplication
- Add decisions with deduplication
- Similarity-based deduplication
- Multi-project support with project attribution
- Cross-project knowledge queries

Usage:
    # Extract learnings from agent memory
    python knowledge_capture.py extract --agent-id senior-engineer

    # Capture learnings from completed task
    python knowledge_capture.py task --task-id TASK-123

    # Add a pattern
    python knowledge_capture.py pattern --category Architecture --pattern "Use dependency injection" \
        --context "When building modular services" --example "class UserService(db: Database)"

    # Add a decision
    python knowledge_capture.py decision --title "Use PostgreSQL" \
        --context "Need ACID compliance" --decision "Use PostgreSQL for main database" \
        --consequences "Positive: reliability; Negative: learning curve"

    # Query patterns across projects
    python knowledge_capture.py query --type patterns --keyword "dependency"

    # Show help
    python knowledge_capture.py help
"""

import difflib
import hashlib
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


# Configuration (legacy paths kept for backward compatibility)
COMPANY_DIR = ".company"
KNOWLEDGE_DIR = "knowledge"
AGENTS_DIR = "agents"
EMPLOYEES_DIR = "employees"
QUEUE_FILE = "state/work_queue.json"

PATTERNS_FILE = "patterns.md"
DECISIONS_FILE = "decisions.md"

# Similarity threshold for deduplication (0.0 - 1.0)
SIMILARITY_THRESHOLD = 0.7


def _get_company_base() -> Path:
    """Get the base company directory (multi-project aware)."""
    return get_company_dir(Path.cwd())


def get_knowledge_dir() -> Path:
    """Get the knowledge directory path."""
    return _get_company_base() / KNOWLEDGE_DIR


def get_patterns_path() -> Path:
    """Get patterns.md path."""
    return get_knowledge_dir() / PATTERNS_FILE


def get_decisions_path() -> Path:
    """Get decisions.md path."""
    return get_knowledge_dir() / DECISIONS_FILE


def get_employee_memory_path(employee_id: str) -> Path:
    """Get the path to an employee's memory file."""
    return _get_company_base() / EMPLOYEES_DIR / employee_id / "memory.md"


def get_employee_learnings_path(employee_id: str) -> Path:
    """Get the path to an employee's learnings file."""
    return _get_company_base() / EMPLOYEES_DIR / employee_id / "learnings.md"


# Legacy aliases for backward compatibility
def get_agent_memory_path(agent_id: str) -> Path:
    """Get the path to an agent's memory file (deprecated, use get_employee_memory_path)."""
    return get_employee_memory_path(agent_id)


def get_agent_learnings_path(agent_id: str) -> Path:
    """Get the path to an agent's learnings file (deprecated, use get_employee_learnings_path)."""
    return get_employee_learnings_path(agent_id)


def get_queue_path() -> Path:
    """Get the work queue file path."""
    return _get_company_base() / QUEUE_FILE


def get_project_context() -> dict | None:
    """
    Get current project context for knowledge attribution.

    Returns:
        Dict with project_id and project_path if in multi-project mode,
        None otherwise.
    """
    return get_current_project()


def load_queue() -> dict:
    """Load work queue from file."""
    queue_path = get_queue_path()

    if not queue_path.exists():
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "metadata": {},
        }

    try:
        with open(queue_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "metadata": {},
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


def calculate_similarity(text1: str, text2: str) -> float:
    """
    Calculate similarity ratio between two texts.
    Returns a value between 0.0 (no match) and 1.0 (exact match).
    """
    # Normalize texts for comparison
    text1_normalized = re.sub(r"\s+", " ", text1.lower().strip())
    text2_normalized = re.sub(r"\s+", " ", text2.lower().strip())

    return difflib.SequenceMatcher(None, text1_normalized, text2_normalized).ratio()


def extract_existing_patterns(content: str) -> list[dict]:
    """Extract existing patterns from patterns.md content."""
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
            {
                "name": name.strip(),
                "category": category_match.group(1).strip() if category_match else "",
                "context": context_match.group(1).strip() if context_match else "",
                "pattern": pattern_match.group(1).strip() if pattern_match else "",
                "full_body": body.strip(),
            }
        )

    return patterns


def extract_existing_decisions(content: str) -> list[dict]:
    """Extract existing decisions from decisions.md content."""
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
            {
                "number": int(number),
                "title": title.strip(),
                "context": context_match.group(1).strip() if context_match else "",
                "decision": decision_match.group(1).strip() if decision_match else "",
                "consequences": consequences_match.group(1).strip()
                if consequences_match
                else "",
                "full_body": body.strip(),
            }
        )

    return decisions


def is_duplicate_pattern(
    new_pattern: str,
    new_context: str,
    existing_patterns: list[dict],
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[bool, dict | None]:
    """
    Check if a pattern is a duplicate of an existing one.

    Returns:
        (is_duplicate, similar_pattern if found)
    """
    combined_new = f"{new_pattern} {new_context}"

    for existing in existing_patterns:
        combined_existing = f"{existing['pattern']} {existing['context']}"
        similarity = calculate_similarity(combined_new, combined_existing)

        if similarity >= threshold:
            return True, existing

    return False, None


def is_duplicate_decision(
    new_title: str,
    new_decision: str,
    existing_decisions: list[dict],
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[bool, dict | None]:
    """
    Check if a decision is a duplicate of an existing one.

    Returns:
        (is_duplicate, similar_decision if found)
    """
    combined_new = f"{new_title} {new_decision}"

    for existing in existing_decisions:
        combined_existing = f"{existing['title']} {existing['decision']}"
        similarity = calculate_similarity(combined_new, combined_existing)

        if similarity >= threshold:
            return True, existing

    return False, None


def get_next_adr_number(existing_decisions: list[dict]) -> int:
    """Get the next ADR number."""
    if not existing_decisions:
        return 1

    max_number = max(d.get("number", 0) for d in existing_decisions)
    return max_number + 1


def extract_learnings(agent_id: str) -> dict:
    """
    Extract learnings from agent/employee memory file.

    Analyzes memory content to identify:
    - Repeated successful approaches (patterns)
    - Architectural decisions made
    - Mistakes and lessons learned

    Args:
        agent_id: The agent/employee ID to extract from

    Returns:
        Dict with extracted learnings categorized, including project attribution
    """
    # Try employee path first, then fall back to agent path for backward compatibility
    memory_path = get_employee_memory_path(agent_id)
    learnings_path = get_employee_learnings_path(agent_id)

    memory_content = read_file_content(memory_path)
    learnings_content = read_file_content(learnings_path)

    if not memory_content and not learnings_content:
        return {
            "success": False,
            "reason": "no_memory_files",
            "message": f"No memory or learnings files found for employee {agent_id}",
            "agent_id": agent_id,
        }

    # Get project context for attribution
    project_context = get_project_context()
    project_id = project_context["project_id"] if project_context else get_project_id()

    extracted = {
        "patterns": [],
        "decisions": [],
        "mistakes": [],
        "domain_knowledge": [],
    }

    # Parse memory content for patterns
    if memory_content:
        # Look for preferences section which often contains successful patterns
        preferences_match = re.search(
            r"##\s+Preferences\s*(.*?)(?=\n##|\Z)", memory_content, re.DOTALL
        )
        if preferences_match:
            preferences = preferences_match.group(1)
            # Extract individual preference items
            for line in preferences.split("\n"):
                line = line.strip()
                if line.startswith("-") and len(line) > 10:
                    extracted["patterns"].append(
                        {
                            "source": "memory_preferences",
                            "content": line[1:].strip(),
                            "agent_id": agent_id,
                            "project_id": project_id,
                        }
                    )

        # Look for recent interactions that mention decisions
        interactions_match = re.search(
            r"##\s+Recent Interactions\s*(.*?)(?=\n##|\Z)", memory_content, re.DOTALL
        )
        if interactions_match:
            interactions = interactions_match.group(1)
            # Look for decision-related keywords
            decision_keywords = ["decided", "chose", "selected", "went with", "adopted"]
            for section in interactions.split("###"):
                if any(kw in section.lower() for kw in decision_keywords):
                    outcome_match = re.search(r"\*\*Outcome:\*\*\s*([^\n]+)", section)
                    if outcome_match:
                        extracted["decisions"].append(
                            {
                                "source": "memory_interactions",
                                "content": outcome_match.group(1).strip(),
                                "agent_id": agent_id,
                                "project_id": project_id,
                            }
                        )

    # Parse learnings content for more structured data
    if learnings_content:
        # Extract successful patterns
        patterns_match = re.search(
            r"##\s+Successful Patterns\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
            learnings_content,
            re.DOTALL,
        )
        if patterns_match:
            patterns_section = patterns_match.group(1)
            # Extract pattern entries
            pattern_entries = re.findall(
                r"###\s+([^\n]+)\n(.*?)(?=\n###|\n##|\Z)",
                patterns_section,
                re.DOTALL,
            )
            for name, body in pattern_entries:
                if not name.strip().startswith("["):  # Skip template
                    context_match = re.search(r"\*\*Context:\*\*\s*([^\n]+)", body)
                    approach_match = re.search(
                        r"\*\*Approach:\*\*\s*(.*?)(?=\n\*\*|\Z)", body, re.DOTALL
                    )
                    extracted["patterns"].append(
                        {
                            "source": "learnings_file",
                            "name": name.strip(),
                            "context": context_match.group(1).strip()
                            if context_match
                            else "",
                            "approach": approach_match.group(1).strip()
                            if approach_match
                            else "",
                            "agent_id": agent_id,
                            "project_id": project_id,
                        }
                    )

        # Extract mistakes and lessons
        mistakes_match = re.search(
            r"##\s+Mistakes & Lessons\s*(.*?)(?=\n##\s+[A-Z]|\Z)",
            learnings_content,
            re.DOTALL,
        )
        if mistakes_match:
            mistakes_section = mistakes_match.group(1)
            mistake_entries = re.findall(
                r"###\s+([^\n]+)\n(.*?)(?=\n###|\n##|\Z)",
                mistakes_section,
                re.DOTALL,
            )
            for title, body in mistake_entries:
                if not title.strip().startswith("["):  # Skip template
                    lesson_match = re.search(
                        r"\*\*Lesson Learned:\*\*\s*([^\n]+)", body
                    )
                    prevention_match = re.search(
                        r"\*\*Prevention:\*\*\s*([^\n]+)", body
                    )
                    extracted["mistakes"].append(
                        {
                            "source": "learnings_file",
                            "title": title.strip(),
                            "lesson": lesson_match.group(1).strip()
                            if lesson_match
                            else "",
                            "prevention": prevention_match.group(1).strip()
                            if prevention_match
                            else "",
                            "agent_id": agent_id,
                            "project_id": project_id,
                        }
                    )

        # Extract domain expertise
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
                if not name.strip().startswith("["):  # Skip template
                    overview_match = re.search(r"\*\*Overview:\*\*\s*([^\n]+)", body)
                    extracted["domain_knowledge"].append(
                        {
                            "source": "learnings_file",
                            "name": name.strip(),
                            "overview": overview_match.group(1).strip()
                            if overview_match
                            else "",
                            "agent_id": agent_id,
                            "project_id": project_id,
                        }
                    )

    return {
        "success": True,
        "agent_id": agent_id,
        "project_id": project_id,
        "multi_project_mode": is_multi_project_mode(),
        "extracted": extracted,
        "counts": {
            "patterns": len(extracted["patterns"]),
            "decisions": len(extracted["decisions"]),
            "mistakes": len(extracted["mistakes"]),
            "domain_knowledge": len(extracted["domain_knowledge"]),
        },
        "total_items": sum(len(v) for v in extracted.values()),
    }


def capture_from_completed_work(task_id: str) -> dict:
    """
    Extract learnings from a completed task.

    Looks at:
    - Task notes and progress notes
    - Associated commits
    - Review comments if any

    Args:
        task_id: The task ID to extract from

    Returns:
        Dict with extracted learnings
    """
    queue = load_queue()

    # Find the task in completed
    task = None
    for t in queue.get("completed", []):
        if t.get("task_id") == task_id:
            task = t
            break

    if not task:
        # Also check other statuses in case it's in progress
        for status in ["in_progress", "pending", "blocked"]:
            for t in queue.get(status, []):
                if t.get("task_id") == task_id:
                    return {
                        "success": False,
                        "reason": "not_completed",
                        "message": f"Task {task_id} is not completed (status: {status})",
                        "task_id": task_id,
                    }

        return {
            "success": False,
            "reason": "not_found",
            "message": f"Task {task_id} not found",
            "task_id": task_id,
        }

    # Get project context for attribution
    project_context = get_project_context()
    project_id = project_context["project_id"] if project_context else get_project_id()

    extracted = {
        "patterns": [],
        "decisions": [],
        "insights": [],
    }

    # Extract from progress notes
    progress_notes = task.get("progress_notes", [])
    for note in progress_notes:
        content = note.get("content", "")

        # Look for pattern-like content
        pattern_keywords = [
            "pattern",
            "approach",
            "strategy",
            "solution",
            "worked well",
        ]
        if any(kw in content.lower() for kw in pattern_keywords):
            extracted["patterns"].append(
                {
                    "source": "progress_note",
                    "content": content,
                    "timestamp": note.get("timestamp"),
                    "project_id": project_id,
                }
            )

        # Look for decision-like content
        decision_keywords = ["decided", "chose", "selected", "went with", "using"]
        if any(kw in content.lower() for kw in decision_keywords):
            extracted["decisions"].append(
                {
                    "source": "progress_note",
                    "content": content,
                    "timestamp": note.get("timestamp"),
                    "project_id": project_id,
                }
            )

    # Extract from task metadata
    if task.get("learnings"):
        # If task has explicit learnings field
        for learning in task["learnings"]:
            extracted["insights"].append(
                {
                    "source": "task_learnings",
                    "content": learning,
                    "project_id": project_id,
                }
            )

    # Extract from task outcome/description
    title = task.get("title", "")
    _ = task.get("description", "")  # Reserved for future pattern extraction

    # If task completed faster than estimated, might indicate a good pattern
    estimated = task.get("estimated_hours", 4)
    started_at = task.get("started_at")
    completed_at = task.get("completed_at")

    if started_at and completed_at:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            actual_hours = (end - start).total_seconds() / 3600

            if actual_hours < estimated * 0.5:
                extracted["insights"].append(
                    {
                        "source": "time_analysis",
                        "content": f"Task '{title}' completed in {actual_hours:.1f}h vs {estimated}h estimated - possible efficiency pattern",
                        "project_id": project_id,
                    }
                )
        except (ValueError, TypeError):
            pass

    return {
        "success": True,
        "task_id": task_id,
        "task_title": task.get("title"),
        "project_id": project_id,
        "multi_project_mode": is_multi_project_mode(),
        "extracted": extracted,
        "counts": {
            "patterns": len(extracted["patterns"]),
            "decisions": len(extracted["decisions"]),
            "insights": len(extracted["insights"]),
        },
        "total_items": sum(len(v) for v in extracted.values()),
    }


def add_pattern(
    category: str,
    pattern: str,
    context: str,
    example: str | None = None,
    source_project: str | None = None,
) -> dict:
    """
    Add a pattern to patterns.md with deduplication.

    Args:
        category: Pattern category (Architecture, Code, Testing, Security, DevOps)
        pattern: Description of the pattern
        context: When to use this pattern
        example: Optional code example
        source_project: Optional project ID override (defaults to current project)

    Returns:
        Dict with success status
    """
    valid_categories = {"Architecture", "Code", "Testing", "Security", "DevOps"}
    if category not in valid_categories:
        return {
            "success": False,
            "reason": "invalid_category",
            "message": f"Category must be one of: {valid_categories}",
        }

    # Get project context for attribution
    project_context = get_project_context()
    project_id = source_project or (
        project_context["project_id"] if project_context else get_project_id()
    )

    patterns_path = get_patterns_path()
    content = read_file_content(patterns_path) or ""

    # Extract existing patterns
    existing_patterns = extract_existing_patterns(content)

    # Check for duplicates
    is_dup, similar = is_duplicate_pattern(pattern, context, existing_patterns)
    if is_dup:
        return {
            "success": False,
            "reason": "duplicate",
            "message": f"Similar pattern already exists: {similar['name']}",
            "similarity_match": similar["name"],
        }

    # Generate pattern name from pattern description
    words = pattern.split()[:4]
    pattern_name = " ".join(word.capitalize() for word in words)

    # Build new pattern entry
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_entry = f"""
---

### {pattern_name}

**Category:** {category}

**Context:** {context}

**Pattern:**
{pattern}
"""

    if example:
        new_entry += f"""
**Example:**
```
{example}
```
"""

    new_entry += f"""
**Added:** {now}

**Source Project:** {project_id}
"""

    # Append to file
    if not content.strip():
        content = "# Implementation Patterns\n\n## Established Patterns\n"

    content += new_entry

    if write_file_content(patterns_path, content):
        return {
            "success": True,
            "pattern_name": pattern_name,
            "category": category,
            "project_id": project_id,
            "multi_project_mode": is_multi_project_mode(),
            "file": str(patterns_path),
            "message": f"Pattern '{pattern_name}' added successfully",
        }
    else:
        return {
            "success": False,
            "reason": "write_error",
            "message": "Failed to write to patterns.md",
        }


def add_decision(
    title: str,
    context: str,
    decision: str,
    consequences: str,
    source_project: str | None = None,
) -> dict:
    """
    Add a decision to decisions.md with deduplication.

    Args:
        title: Short title for the decision
        context: What issue motivated this decision
        decision: What was decided
        consequences: Positive and negative consequences
        source_project: Optional project ID override (defaults to current project)

    Returns:
        Dict with success status
    """
    # Get project context for attribution
    project_context = get_project_context()
    project_id = source_project or (
        project_context["project_id"] if project_context else get_project_id()
    )

    decisions_path = get_decisions_path()
    content = read_file_content(decisions_path) or ""

    # Extract existing decisions
    existing_decisions = extract_existing_decisions(content)

    # Check for duplicates
    is_dup, similar = is_duplicate_decision(title, decision, existing_decisions)
    if is_dup:
        return {
            "success": False,
            "reason": "duplicate",
            "message": f"Similar decision already exists: ADR-{similar['number']:04d}: {similar['title']}",
            "similarity_match": f"ADR-{similar['number']:04d}",
        }

    # Get next ADR number
    adr_number = get_next_adr_number(existing_decisions)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build new decision entry
    new_entry = f"""
---

## ADR-{adr_number:04d}: {title}

**Status:** Accepted

**Date:** {now}

**Source Project:** {project_id}

### Context

{context}

### Decision

{decision}

### Consequences

{consequences}
"""

    # Append to file
    if not content.strip():
        content = "# Architecture Decision Records\n"

    content += new_entry

    if write_file_content(decisions_path, content):
        return {
            "success": True,
            "adr_number": f"ADR-{adr_number:04d}",
            "title": title,
            "project_id": project_id,
            "multi_project_mode": is_multi_project_mode(),
            "file": str(decisions_path),
            "message": f"Decision 'ADR-{adr_number:04d}: {title}' added successfully",
        }
    else:
        return {
            "success": False,
            "reason": "write_error",
            "message": "Failed to write to decisions.md",
        }


def _generate_pattern_id(pattern_name: str, source_project: str) -> str:
    """Generate a unique pattern ID from name and source project."""
    combined = f"{pattern_name}:{source_project}"
    return hashlib.sha256(combined.encode()).hexdigest()[:8]


def _extract_pattern_metadata(pattern: dict) -> dict:
    """Extract metadata fields from a pattern's full_body."""
    body = pattern.get("full_body", "")
    metadata = {
        "name": pattern.get("name", ""),
        "category": pattern.get("category", ""),
        "context": pattern.get("context", ""),
        "pattern": pattern.get("pattern", ""),
    }

    # Extract source project
    source_match = re.search(r"\*\*Source Project:\*\*\s*([^\n]+)", body)
    metadata["source_project"] = (
        source_match.group(1).strip() if source_match else "unknown"
    )

    # Extract tech stack if present
    tech_match = re.search(r"\*\*Tech Stack:\*\*\s*([^\n]+)", body)
    if tech_match:
        metadata["tech_stack"] = [t.strip() for t in tech_match.group(1).split(",")]

    # Extract domain if present
    domain_match = re.search(r"\*\*Domain:\*\*\s*([^\n]+)", body)
    if domain_match:
        metadata["domain"] = domain_match.group(1).strip()

    # Extract tags if present
    tags_match = re.search(r"\*\*Tags:\*\*\s*([^\n]+)", body)
    if tags_match:
        metadata["tags"] = [t.strip() for t in tags_match.group(1).split(",")]

    # Generate pattern ID
    metadata["pattern_id"] = _generate_pattern_id(
        metadata["name"], metadata["source_project"]
    )

    return metadata


def _calculate_relevance_score(
    pattern_metadata: dict,
    context: dict,
) -> float:
    """
    Calculate relevance score for a pattern based on context matching.

    Args:
        pattern_metadata: Extracted pattern metadata
        context: Dict with optional keys: category, tech_stack, domain, keywords

    Returns:
        Float between 0.0 and 1.0 representing relevance
    """
    score = 0.0
    max_score = 0.0

    # Category match (weight: 0.3)
    if context.get("category"):
        max_score += 0.3
        if pattern_metadata.get("category", "").lower() == context["category"].lower():
            score += 0.3

    # Tech stack overlap (weight: 0.3)
    if context.get("tech_stack"):
        max_score += 0.3
        pattern_tech = set(t.lower() for t in pattern_metadata.get("tech_stack", []))
        context_tech = set(t.lower() for t in context["tech_stack"])
        if pattern_tech and context_tech:
            overlap = len(pattern_tech & context_tech) / len(context_tech)
            score += 0.3 * overlap

    # Domain match (weight: 0.2)
    if context.get("domain"):
        max_score += 0.2
        pattern_domain = pattern_metadata.get("domain", "").lower()
        if pattern_domain and context["domain"].lower() in pattern_domain:
            score += 0.2

    # Keyword matching in pattern and context fields (weight: 0.2)
    if context.get("keywords"):
        max_score += 0.2
        searchable = (
            f"{pattern_metadata.get('pattern', '')} "
            f"{pattern_metadata.get('context', '')} "
            f"{' '.join(pattern_metadata.get('tags', []))}"
        ).lower()
        keywords = context["keywords"]
        if isinstance(keywords, str):
            keywords = [keywords]
        matches = sum(1 for kw in keywords if kw.lower() in searchable)
        if keywords:
            score += 0.2 * (matches / len(keywords))

    # Normalize to 0-1 range
    return score / max_score if max_score > 0 else 0.0


def find_applicable_patterns(
    project_id: str,
    context: dict | None = None,
) -> dict:
    """
    Search patterns from OTHER projects (not the requesting project).

    Finds patterns that may be applicable to the requesting project based on
    category, tech_stack, domain, or keyword matching.

    Args:
        project_id: The requesting project's ID (patterns from this project are excluded)
        context: Optional dict with matching criteria:
            - category: Pattern category to match
            - tech_stack: List of technologies to match
            - domain: Domain to match
            - keywords: Keywords to search for in pattern content

    Returns:
        Dict with matching patterns sorted by relevance score (highest first)
    """
    if context is None:
        context = {}

    patterns_path = get_patterns_path()
    content = read_file_content(patterns_path)

    if not content:
        return {
            "success": True,
            "requesting_project": project_id,
            "patterns": [],
            "count": 0,
            "message": "No patterns found in knowledge base",
        }

    all_patterns = extract_existing_patterns(content)
    applicable_patterns = []

    for p in all_patterns:
        metadata = _extract_pattern_metadata(p)

        # Exclude patterns from the requesting project
        if metadata["source_project"] == project_id:
            continue

        # Calculate relevance score
        relevance = _calculate_relevance_score(metadata, context)

        # Include patterns with any relevance, or all if no context provided
        if relevance > 0 or not context:
            applicable_patterns.append(
                {
                    "pattern_id": metadata["pattern_id"],
                    "name": metadata["name"],
                    "category": metadata["category"],
                    "context": metadata["context"],
                    "pattern": metadata["pattern"],
                    "source_project": metadata["source_project"],
                    "tech_stack": metadata.get("tech_stack", []),
                    "domain": metadata.get("domain"),
                    "relevance_score": round(relevance, 3),
                }
            )

    # Sort by relevance score (highest first)
    applicable_patterns.sort(key=lambda x: x["relevance_score"], reverse=True)

    return {
        "success": True,
        "requesting_project": project_id,
        "context_used": context,
        "patterns": applicable_patterns,
        "count": len(applicable_patterns),
        "multi_project_mode": is_multi_project_mode(),
    }


def apply_pattern_to_project(
    pattern_id: str,
    target_project_id: str,
) -> dict:
    """
    Copy a pattern to a target project's applied patterns list.

    Creates a reference to the pattern with "applied_from" metadata preserving
    source project attribution. Prevents duplicates by checking if the pattern
    has already been applied.

    Args:
        pattern_id: The pattern ID to apply (from find_applicable_patterns)
        target_project_id: The project to apply the pattern to

    Returns:
        Dict with success status and applied pattern details
    """
    patterns_path = get_patterns_path()
    content = read_file_content(patterns_path)

    if not content:
        return {
            "success": False,
            "reason": "no_patterns_file",
            "message": "No patterns.md file found",
        }

    all_patterns = extract_existing_patterns(content)

    # Find the source pattern
    source_pattern = None
    source_metadata = None
    for p in all_patterns:
        metadata = _extract_pattern_metadata(p)
        if metadata["pattern_id"] == pattern_id:
            source_pattern = p
            source_metadata = metadata
            break

    if not source_pattern:
        return {
            "success": False,
            "reason": "pattern_not_found",
            "message": f"Pattern with ID '{pattern_id}' not found",
        }

    # Check if already applied to target project
    applied_marker = f"**Applied to:** {target_project_id}"

    # Check if this exact pattern is already marked as applied to target
    if applied_marker in content:
        # Check if it's for this specific pattern
        pattern_section_start = content.find(f"### {source_metadata['name']}")
        if pattern_section_start >= 0:
            next_pattern_start = content.find("\n### ", pattern_section_start + 1)
            if next_pattern_start < 0:
                next_pattern_start = len(content)
            pattern_section = content[pattern_section_start:next_pattern_start]
            if applied_marker in pattern_section:
                return {
                    "success": False,
                    "reason": "already_applied",
                    "message": f"Pattern '{source_metadata['name']}' already applied to project '{target_project_id}'",
                    "pattern_id": pattern_id,
                    "target_project": target_project_id,
                }

    # Add the applied reference to the source pattern section
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    applied_entry = f"\n**Applied to:** {target_project_id} (on {now})"

    # Find the pattern section and add the applied marker
    pattern_section_start = content.find(f"### {source_metadata['name']}")
    if pattern_section_start >= 0:
        # Find the end of this pattern section
        next_section = content.find("\n---", pattern_section_start)
        if next_section < 0:
            next_section = len(content)

        # Insert before the next section separator
        content = content[:next_section] + applied_entry + content[next_section:]

        if write_file_content(patterns_path, content):
            return {
                "success": True,
                "pattern_id": pattern_id,
                "pattern_name": source_metadata["name"],
                "source_project": source_metadata["source_project"],
                "target_project": target_project_id,
                "applied_on": now,
                "message": f"Pattern '{source_metadata['name']}' applied to project '{target_project_id}'",
            }
        else:
            return {
                "success": False,
                "reason": "write_error",
                "message": "Failed to write to patterns.md",
            }

    return {
        "success": False,
        "reason": "pattern_section_not_found",
        "message": f"Could not locate pattern section for '{source_metadata['name']}'",
    }


def share_decision(
    adr_number: int | str,
    target_projects: list[str],
) -> dict:
    """
    Share a decision (ADR) with target projects by adding cross-references.

    For each target project, adds a "referenced_from" entry in the decisions
    file while preserving original attribution.

    Args:
        adr_number: The ADR number to share (e.g., 1, "0001", or "ADR-0001")
        target_projects: List of project IDs to share the decision with

    Returns:
        Dict with success/failure status per project
    """
    # Normalize ADR number
    if isinstance(adr_number, str):
        adr_number = int(re.sub(r"\D", "", adr_number))

    decisions_path = get_decisions_path()
    content = read_file_content(decisions_path)

    if not content:
        return {
            "success": False,
            "reason": "no_decisions_file",
            "message": "No decisions.md file found",
        }

    existing_decisions = extract_existing_decisions(content)

    # Find the source decision
    source_decision = None
    for d in existing_decisions:
        if d.get("number") == adr_number:
            source_decision = d
            break

    if not source_decision:
        return {
            "success": False,
            "reason": "decision_not_found",
            "message": f"ADR-{adr_number:04d} not found",
        }

    # Extract source project from decision
    source_match = re.search(
        r"\*\*Source Project:\*\*\s*([^\n]+)", source_decision.get("full_body", "")
    )
    source_project = source_match.group(1).strip() if source_match else "unknown"

    results = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    adr_title = f"ADR-{adr_number:04d}"

    for target_project in target_projects:
        # Check if already shared to this project
        shared_marker = f"**Referenced by:** {target_project}"
        decision_section_start = content.find(f"## {adr_title}:")

        if decision_section_start >= 0:
            next_decision_start = content.find("\n## ADR-", decision_section_start + 1)
            if next_decision_start < 0:
                next_decision_start = len(content)
            decision_section = content[decision_section_start:next_decision_start]

            if shared_marker in decision_section:
                results[target_project] = {
                    "success": False,
                    "reason": "already_shared",
                    "message": f"Already shared to {target_project}",
                }
                continue

            # Add the reference
            reference_entry = f"\n**Referenced by:** {target_project} (on {now})"

            # Find the end of this decision section
            next_section = content.find("\n---", decision_section_start)
            if next_section < 0 or next_section > next_decision_start:
                next_section = next_decision_start

            # Insert before the next section separator
            content = content[:next_section] + reference_entry + content[next_section:]

            results[target_project] = {
                "success": True,
                "message": f"Shared to {target_project}",
                "shared_on": now,
            }
        else:
            results[target_project] = {
                "success": False,
                "reason": "decision_section_not_found",
                "message": f"Could not locate decision section for {adr_title}",
            }

    # Write updated content
    if any(r.get("success") for r in results.values()):
        if not write_file_content(decisions_path, content):
            return {
                "success": False,
                "reason": "write_error",
                "message": "Failed to write to decisions.md",
            }

    # Determine overall success
    successful = sum(1 for r in results.values() if r.get("success"))
    failed = len(results) - successful

    return {
        "success": successful > 0,
        "adr_number": adr_title,
        "adr_title": source_decision.get("title", ""),
        "source_project": source_project,
        "results": results,
        "summary": {
            "total": len(target_projects),
            "successful": successful,
            "failed": failed,
        },
    }


def get_cross_project_learnings(
    tags: list[str] | str | None = None,
) -> dict:
    """
    Aggregate learnings from ALL projects in the company.

    Collects patterns, decisions, and any captured insights across all projects,
    with optional filtering by tags.

    Args:
        tags: Optional tag(s) to filter by (single string or list of strings)

    Returns:
        Dict with aggregated learnings including project attribution
    """
    # Normalize tags
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = [tags]
    tags_lower = [t.lower() for t in tags]

    learnings = {
        "patterns": [],
        "decisions": [],
        "insights": [],
    }

    # Collect patterns
    patterns_path = get_patterns_path()
    patterns_content = read_file_content(patterns_path)
    if patterns_content:
        all_patterns = extract_existing_patterns(patterns_content)
        for p in all_patterns:
            metadata = _extract_pattern_metadata(p)

            # Apply tag filter if specified
            if tags_lower:
                pattern_tags = [t.lower() for t in metadata.get("tags", [])]
                # Also search in category and context
                searchable = (
                    f"{metadata.get('category', '')} "
                    f"{metadata.get('context', '')} "
                    f"{metadata.get('pattern', '')}"
                ).lower()

                # Check if any tag matches
                if not any(t in pattern_tags or t in searchable for t in tags_lower):
                    continue

            learnings["patterns"].append(
                {
                    "type": "pattern",
                    "pattern_id": metadata["pattern_id"],
                    "name": metadata["name"],
                    "category": metadata["category"],
                    "context": metadata["context"],
                    "pattern": metadata["pattern"],
                    "source_project": metadata["source_project"],
                    "tags": metadata.get("tags", []),
                }
            )

    # Collect decisions
    decisions_path = get_decisions_path()
    decisions_content = read_file_content(decisions_path)
    if decisions_content:
        all_decisions = extract_existing_decisions(decisions_content)
        for d in all_decisions:
            # Extract source project
            source_match = re.search(
                r"\*\*Source Project:\*\*\s*([^\n]+)", d.get("full_body", "")
            )
            source_project = (
                source_match.group(1).strip() if source_match else "unknown"
            )

            # Extract tags if present
            tags_match = re.search(r"\*\*Tags:\*\*\s*([^\n]+)", d.get("full_body", ""))
            decision_tags = []
            if tags_match:
                decision_tags = [t.strip() for t in tags_match.group(1).split(",")]

            # Apply tag filter if specified
            if tags_lower:
                decision_tags_lower = [t.lower() for t in decision_tags]
                searchable = (
                    f"{d.get('title', '')} "
                    f"{d.get('context', '')} "
                    f"{d.get('decision', '')}"
                ).lower()

                if not any(
                    t in decision_tags_lower or t in searchable for t in tags_lower
                ):
                    continue

            learnings["decisions"].append(
                {
                    "type": "decision",
                    "adr_number": f"ADR-{d.get('number', 0):04d}",
                    "title": d.get("title", ""),
                    "context": d.get("context", ""),
                    "decision": d.get("decision", ""),
                    "consequences": d.get("consequences", ""),
                    "source_project": source_project,
                    "tags": decision_tags,
                }
            )

    # Count totals
    total_patterns = len(learnings["patterns"])
    total_decisions = len(learnings["decisions"])
    total_insights = len(learnings["insights"])

    # Get unique projects
    all_projects = set()
    for item in learnings["patterns"]:
        all_projects.add(item["source_project"])
    for item in learnings["decisions"]:
        all_projects.add(item["source_project"])

    return {
        "success": True,
        "filters": {"tags": tags if tags else None},
        "learnings": learnings,
        "summary": {
            "total_patterns": total_patterns,
            "total_decisions": total_decisions,
            "total_insights": total_insights,
            "total_items": total_patterns + total_decisions + total_insights,
            "projects_represented": list(all_projects),
            "project_count": len(all_projects),
        },
        "multi_project_mode": is_multi_project_mode(),
    }


def query_knowledge(
    knowledge_type: str = "patterns",
    keyword: str | None = None,
    category: str | None = None,
    project_filter: str | None = None,
) -> dict:
    """
    Query knowledge across all projects.

    Supports cross-project knowledge queries for finding patterns and decisions
    that may be applicable to the current work.

    Args:
        knowledge_type: Type of knowledge to query ("patterns" or "decisions")
        keyword: Optional keyword to filter by
        category: Optional category filter (patterns only)
        project_filter: Optional project ID to filter by

    Returns:
        Dict with matching knowledge items
    """
    results = []

    if knowledge_type == "patterns":
        patterns_path = get_patterns_path()
        content = read_file_content(patterns_path)

        if content:
            patterns = extract_existing_patterns(content)

            for p in patterns:
                # Apply filters
                if keyword and keyword.lower() not in p.get("pattern", "").lower():
                    if keyword.lower() not in p.get("context", "").lower():
                        if keyword.lower() not in p.get("name", "").lower():
                            continue

                if category and p.get("category", "").lower() != category.lower():
                    continue

                # Extract source project from full_body if present
                source_match = re.search(
                    r"\*\*Source Project:\*\*\s*([^\n]+)", p.get("full_body", "")
                )
                source_project = (
                    source_match.group(1).strip() if source_match else "unknown"
                )

                if project_filter and source_project != project_filter:
                    continue

                results.append(
                    {
                        "type": "pattern",
                        "name": p.get("name", ""),
                        "category": p.get("category", ""),
                        "context": p.get("context", ""),
                        "pattern": p.get("pattern", ""),
                        "source_project": source_project,
                    }
                )

    elif knowledge_type == "decisions":
        decisions_path = get_decisions_path()
        content = read_file_content(decisions_path)

        if content:
            decisions = extract_existing_decisions(content)

            for d in decisions:
                # Apply filters
                if keyword and keyword.lower() not in d.get("title", "").lower():
                    if keyword.lower() not in d.get("decision", "").lower():
                        if keyword.lower() not in d.get("context", "").lower():
                            continue

                # Extract source project from full_body if present
                source_match = re.search(
                    r"\*\*Source Project:\*\*\s*([^\n]+)", d.get("full_body", "")
                )
                source_project = (
                    source_match.group(1).strip() if source_match else "unknown"
                )

                if project_filter and source_project != project_filter:
                    continue

                results.append(
                    {
                        "type": "decision",
                        "adr_number": f"ADR-{d.get('number', 0):04d}",
                        "title": d.get("title", ""),
                        "context": d.get("context", ""),
                        "decision": d.get("decision", ""),
                        "consequences": d.get("consequences", ""),
                        "source_project": source_project,
                    }
                )

    else:
        return {
            "success": False,
            "reason": "invalid_type",
            "message": f"Knowledge type must be 'patterns' or 'decisions', got '{knowledge_type}'",
        }

    return {
        "success": True,
        "knowledge_type": knowledge_type,
        "filters": {
            "keyword": keyword,
            "category": category,
            "project_filter": project_filter,
        },
        "results": results,
        "count": len(results),
        "multi_project_mode": is_multi_project_mode(),
    }


def print_help():
    """Print usage help."""
    help_text = """
Knowledge Capture Utility — Extract and store organizational learnings

Supports multi-project mode: knowledge is captured with project attribution
and can be queried across all projects in the company.

Commands:
    extract         Extract learnings from employee memory
    task            Capture learnings from completed task
    pattern         Add a pattern to patterns.md
    decision        Add a decision to decisions.md
    query           Query patterns/decisions across projects
    find-patterns   Find applicable patterns from other projects
    apply-pattern   Apply a pattern to a target project
    share-decision  Share a decision with target projects
    learnings       Get aggregated learnings across all projects

Extract options:
    --agent-id ID       Employee/Agent ID to extract from (required)

Task options:
    --task-id ID        Task ID to capture from (required)

Pattern options:
    --category CAT      Category: Architecture|Code|Testing|Security|DevOps (required)
    --pattern TEXT      Description of the pattern (required)
    --context TEXT      When to use this pattern (required)
    --example TEXT      Optional code example
    --project TEXT      Optional source project override

Decision options:
    --title TEXT        Short decision title (required)
    --context TEXT      What motivated this decision (required)
    --decision TEXT     What was decided (required)
    --consequences TEXT Positive/negative consequences (required)
    --project TEXT      Optional source project override

Query options:
    --type TYPE         Knowledge type: patterns|decisions (default: patterns)
    --keyword TEXT      Filter by keyword
    --category CAT      Filter by category (patterns only)
    --project TEXT      Filter by source project

Find-patterns options (knowledge transfer):
    --project TEXT      Requesting project ID (patterns from this project excluded)
    --category CAT      Filter by category
    --tech-stack TEXT   Comma-separated tech stack to match
    --domain TEXT       Domain to match
    --keywords TEXT     Comma-separated keywords to search

Apply-pattern options (knowledge transfer):
    --pattern-id ID     Pattern ID to apply (required)
    --target TEXT       Target project ID (required)

Share-decision options (knowledge transfer):
    --adr NUMBER        ADR number to share (required)
    --targets TEXT      Comma-separated target project IDs (required)

Learnings options (knowledge transfer):
    --tags TEXT         Comma-separated tags to filter by

Examples:
    # Extract from employee memory
    python knowledge_capture.py extract --agent-id senior-engineer

    # Capture from completed task
    python knowledge_capture.py task --task-id TASK-123

    # Add a pattern
    python knowledge_capture.py pattern --category Architecture \\
        --pattern "Use dependency injection for testability" \\
        --context "When building services that need mocking" \\
        --example "class UserService(db: Database)"

    # Add a decision
    python knowledge_capture.py decision --title "Use PostgreSQL" \\
        --context "Need ACID compliance for transactions" \\
        --decision "Use PostgreSQL as primary database" \\
        --consequences "Positive: reliability; Negative: ops complexity"

    # Query patterns across projects
    python knowledge_capture.py query --type patterns --keyword "injection"

    # Query decisions from a specific project
    python knowledge_capture.py query --type decisions --project myproject-a1b2c3

    # Find applicable patterns from other projects
    python knowledge_capture.py find-patterns --project myproject-a1b2c3 \\
        --category Architecture --tech-stack "python,fastapi"

    # Apply a pattern to a target project
    python knowledge_capture.py apply-pattern --pattern-id abc12345 \\
        --target myproject-d4e5f6

    # Share a decision with multiple projects
    python knowledge_capture.py share-decision --adr 1 \\
        --targets "project-a,project-b,project-c"

    # Get all learnings filtered by tags
    python knowledge_capture.py learnings --tags "security,authentication"

Output: JSON with operation status and details.

Multi-Project Mode:
    When .forge-company-root marker exists in a parent directory, knowledge
    is stored at the company level with project attribution. Queries can
    then find relevant knowledge from any project in the company.

Knowledge Transfer:
    Use find-patterns, apply-pattern, share-decision, and learnings commands
    to transfer knowledge between projects. This is triggered manually via
    /company-knowledge --share (no automatic propagation).

Deduplication:
    Both patterns and decisions are checked against existing entries.
    If similarity exceeds 70%, the entry is rejected as duplicate.
    apply-pattern prevents duplicate applications to the same project.
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
        if command == "extract":
            if "agent_id" not in args:
                print("Error: --agent-id required")
                sys.exit(1)

            result = extract_learnings(args["agent_id"])
            print(json.dumps(result, indent=2))

        elif command == "task":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            result = capture_from_completed_work(args["task_id"])
            print(json.dumps(result, indent=2))

        elif command == "pattern":
            required = ["category", "pattern", "context"]
            missing = [r for r in required if r not in args]
            if missing:
                print(
                    f"Error: Missing required options: {', '.join('--' + m for m in missing)}"
                )
                sys.exit(1)

            result = add_pattern(
                category=args["category"],
                pattern=args["pattern"],
                context=args["context"],
                example=args.get("example"),
                source_project=args.get("project"),
            )
            print(json.dumps(result, indent=2))

        elif command == "decision":
            required = ["title", "context", "decision", "consequences"]
            missing = [r for r in required if r not in args]
            if missing:
                print(
                    f"Error: Missing required options: {', '.join('--' + m for m in missing)}"
                )
                sys.exit(1)

            result = add_decision(
                title=args["title"],
                context=args["context"],
                decision=args["decision"],
                consequences=args["consequences"],
                source_project=args.get("project"),
            )
            print(json.dumps(result, indent=2))

        elif command == "query":
            result = query_knowledge(
                knowledge_type=args.get("type", "patterns"),
                keyword=args.get("keyword"),
                category=args.get("category"),
                project_filter=args.get("project"),
            )
            print(json.dumps(result, indent=2))

        elif command == "find-patterns":
            # Build context from args
            context = {}
            if args.get("category"):
                context["category"] = args["category"]
            if args.get("tech_stack"):
                context["tech_stack"] = [
                    t.strip() for t in args["tech_stack"].split(",")
                ]
            if args.get("domain"):
                context["domain"] = args["domain"]
            if args.get("keywords"):
                context["keywords"] = [k.strip() for k in args["keywords"].split(",")]

            # Get project ID (required or use current)
            project_id = args.get("project") or get_project_id()

            result = find_applicable_patterns(project_id, context if context else None)
            print(json.dumps(result, indent=2))

        elif command == "apply-pattern":
            if "pattern_id" not in args:
                print("Error: --pattern-id required")
                sys.exit(1)
            if "target" not in args:
                print("Error: --target required")
                sys.exit(1)

            result = apply_pattern_to_project(
                pattern_id=args["pattern_id"],
                target_project_id=args["target"],
            )
            print(json.dumps(result, indent=2))

        elif command == "share-decision":
            if "adr" not in args:
                print("Error: --adr required")
                sys.exit(1)
            if "targets" not in args:
                print("Error: --targets required")
                sys.exit(1)

            target_projects = [t.strip() for t in args["targets"].split(",")]
            result = share_decision(
                adr_number=args["adr"],
                target_projects=target_projects,
            )
            print(json.dumps(result, indent=2))

        elif command == "learnings":
            tags = None
            if args.get("tags"):
                tags = [t.strip() for t in args["tags"].split(",")]

            result = get_cross_project_learnings(tags=tags)
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
