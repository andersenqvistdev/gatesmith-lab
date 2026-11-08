#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Semantic Search Utility — Claude-powered knowledge search.

Provides utilities for loading, formatting, and parsing knowledge entries
for semantic search using Claude as the ranking engine.

No external embedding services required — uses Claude's native understanding.

Usage:
    from semantic_search import load_knowledge_entries, format_for_claude

    entries = load_knowledge_entries()
    prompt = format_for_claude(entries, "authentication patterns")
    # Send prompt to Claude subagent, get ranked results
"""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Import company_resolver for multi-project support
try:
    from . import company_resolver
except ImportError:
    try:
        import company_resolver  # type: ignore
    except ImportError:
        company_resolver = None


@dataclass
class KnowledgeEntry:
    """A single knowledge entry from the knowledge base."""

    id: str
    type: str  # pattern, decision, learning, workshop
    title: str
    content: str
    file: str
    line: int
    project: str | None = None
    tags: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "file": self.file,
            "line": self.line,
            "project": self.project,
            "tags": self.tags,
        }


def get_knowledge_dir() -> Path:
    """Get the knowledge directory path."""
    if company_resolver:
        company_dir = company_resolver.get_company_dir()
    else:
        company_dir = Path.cwd() / ".company"
    return company_dir / "knowledge"


def get_current_project() -> str | None:
    """Get current project ID if in multi-project mode."""
    if company_resolver:
        project_info = company_resolver.get_current_project()
        if project_info:
            return project_info.get("project_id")
    return None


def parse_patterns_md(content: str, filename: str) -> list[KnowledgeEntry]:
    """Parse patterns.md into knowledge entries."""
    entries = []
    lines = content.split("\n")

    current_entry: dict[str, Any] | None = None
    current_content_lines: list[str] = []
    entry_start_line = 0
    entry_count = 0

    for i, line in enumerate(lines, 1):
        # New pattern starts with ## (excluding template)
        if line.startswith("## ") and "Template" not in line and "[Pattern" not in line:
            # Save previous entry
            if current_entry:
                current_entry["content"] = "\n".join(current_content_lines).strip()
                entries.append(
                    KnowledgeEntry(
                        id=current_entry["id"],
                        type="pattern",
                        title=current_entry["title"],
                        content=current_entry["content"],
                        file=filename,
                        line=entry_start_line,
                        project=current_entry.get("project"),
                        tags=current_entry.get("tags"),
                    )
                )

            # Start new entry
            entry_count += 1
            title = line[3:].strip()
            current_entry = {
                "id": f"pattern-{entry_count:03d}",
                "title": title,
                "project": None,
                "tags": [],
            }
            current_content_lines = []
            entry_start_line = i

        elif current_entry:
            # Check for project attribution
            if "**Project:**" in line or "Project:" in line:
                # Handle markdown bold: **Project:** value or Project: value
                match = re.search(r"Project:\*?\*?\s*(\S+)", line)
                if match:
                    current_entry["project"] = match.group(1)
            # Check for tags
            elif "**Tags:**" in line or "Tags:" in line:
                # Handle markdown bold: **Tags:** value or Tags: value
                match = re.search(r"Tags:\*?\*?\s*(.+)", line)
                if match:
                    tags = [
                        t.strip().lstrip("*").strip() for t in match.group(1).split(",")
                    ]
                    current_entry["tags"] = tags
            else:
                current_content_lines.append(line)

    # Don't forget the last entry
    if current_entry:
        current_entry["content"] = "\n".join(current_content_lines).strip()
        entries.append(
            KnowledgeEntry(
                id=current_entry["id"],
                type="pattern",
                title=current_entry["title"],
                content=current_entry["content"],
                file=filename,
                line=entry_start_line,
                project=current_entry.get("project"),
                tags=current_entry.get("tags"),
            )
        )

    return entries


def parse_decisions_md(content: str, filename: str) -> list[KnowledgeEntry]:
    """Parse decisions.md into knowledge entries."""
    entries = []
    lines = content.split("\n")

    current_entry: dict[str, Any] | None = None
    current_content_lines: list[str] = []
    entry_start_line = 0
    entry_count = 0

    for i, line in enumerate(lines, 1):
        # ADR format: ## ADR-NNN: Title or ## Decision: Title
        if line.startswith("## ") and ("ADR" in line or "Decision" in line):
            # Save previous entry
            if current_entry:
                current_entry["content"] = "\n".join(current_content_lines).strip()
                entries.append(
                    KnowledgeEntry(
                        id=current_entry["id"],
                        type="decision",
                        title=current_entry["title"],
                        content=current_entry["content"],
                        file=filename,
                        line=entry_start_line,
                        project=current_entry.get("project"),
                    )
                )

            # Start new entry
            entry_count += 1
            title = line[3:].strip()
            # Extract ADR number if present
            adr_match = re.search(r"ADR-(\d+)", title)
            entry_id = (
                f"adr-{adr_match.group(1)}"
                if adr_match
                else f"decision-{entry_count:03d}"
            )

            current_entry = {
                "id": entry_id,
                "title": title,
                "project": None,
            }
            current_content_lines = []
            entry_start_line = i

        elif current_entry:
            # Check for project attribution
            if "**Project:**" in line or "Project:" in line:
                # Handle markdown bold: **Project:** value or Project: value
                match = re.search(r"Project:\*?\*?\s*(\S+)", line)
                if match:
                    current_entry["project"] = match.group(1)
            else:
                current_content_lines.append(line)

    # Don't forget the last entry
    if current_entry:
        current_entry["content"] = "\n".join(current_content_lines).strip()
        entries.append(
            KnowledgeEntry(
                id=current_entry["id"],
                type="decision",
                title=current_entry["title"],
                content=current_entry["content"],
                file=filename,
                line=entry_start_line,
                project=current_entry.get("project"),
            )
        )

    return entries


def parse_workshops_md(content: str, filename: str) -> list[KnowledgeEntry]:
    """Parse workshops.md into knowledge entries."""
    entries = []
    lines = content.split("\n")

    current_entry: dict[str, Any] | None = None
    current_content_lines: list[str] = []
    entry_start_line = 0

    for i, line in enumerate(lines, 1):
        # Workshop format: ## WS-NNN: Title
        if line.startswith("## WS-") and ":" in line:
            # Save previous entry
            if current_entry:
                current_entry["content"] = "\n".join(current_content_lines).strip()
                entries.append(
                    KnowledgeEntry(
                        id=current_entry["id"],
                        type="workshop",
                        title=current_entry["title"],
                        content=current_entry["content"],
                        file=filename,
                        line=entry_start_line,
                    )
                )

            # Start new entry
            title = line[3:].strip()
            ws_match = re.search(r"WS-(\d+)", title)
            entry_id = f"ws-{ws_match.group(1)}" if ws_match else f"workshop-{i}"

            current_entry = {
                "id": entry_id,
                "title": title,
            }
            current_content_lines = []
            entry_start_line = i

        elif current_entry:
            current_content_lines.append(line)

    # Don't forget the last entry
    if current_entry:
        current_entry["content"] = "\n".join(current_content_lines).strip()
        entries.append(
            KnowledgeEntry(
                id=current_entry["id"],
                type="workshop",
                title=current_entry["title"],
                content=current_entry["content"],
                file=filename,
                line=entry_start_line,
            )
        )

    return entries


def parse_generic_md(
    content: str, filename: str, entry_type: str
) -> list[KnowledgeEntry]:
    """Parse a generic markdown file into knowledge entries."""
    entries = []
    lines = content.split("\n")

    current_entry: dict[str, Any] | None = None
    current_content_lines: list[str] = []
    entry_start_line = 0
    entry_count = 0

    for i, line in enumerate(lines, 1):
        # Any ## heading starts a new entry
        if line.startswith("## ") and "Template" not in line:
            # Save previous entry
            if current_entry:
                current_entry["content"] = "\n".join(current_content_lines).strip()
                if current_entry["content"]:  # Only add if has content
                    entries.append(
                        KnowledgeEntry(
                            id=current_entry["id"],
                            type=entry_type,
                            title=current_entry["title"],
                            content=current_entry["content"],
                            file=filename,
                            line=entry_start_line,
                        )
                    )

            # Start new entry
            entry_count += 1
            title = line[3:].strip()
            current_entry = {
                "id": f"{entry_type}-{entry_count:03d}",
                "title": title,
            }
            current_content_lines = []
            entry_start_line = i

        elif current_entry:
            current_content_lines.append(line)

    # Don't forget the last entry
    if current_entry:
        current_entry["content"] = "\n".join(current_content_lines).strip()
        if current_entry["content"]:
            entries.append(
                KnowledgeEntry(
                    id=current_entry["id"],
                    type=entry_type,
                    title=current_entry["title"],
                    content=current_entry["content"],
                    file=filename,
                    line=entry_start_line,
                )
            )

    return entries


def load_knowledge_entries(
    type_filter: str | None = None,
    project_filter: str | None = None,
) -> list[KnowledgeEntry]:
    """
    Load all knowledge entries from the knowledge base.

    Args:
        type_filter: Only load entries of this type (pattern, decision, etc.)
        project_filter: Only load entries from this project

    Returns:
        List of KnowledgeEntry objects
    """
    knowledge_dir = get_knowledge_dir()
    entries: list[KnowledgeEntry] = []

    if not knowledge_dir.exists():
        return entries

    # Map filenames to parsers
    parsers = {
        "patterns.md": ("pattern", parse_patterns_md),
        "decisions.md": ("decision", parse_decisions_md),
        "workshops.md": ("workshop", parse_workshops_md),
        "alignment.md": ("alignment", lambda c, f: parse_generic_md(c, f, "alignment")),
    }

    for filename, (entry_type, parser) in parsers.items():
        # Skip if type filter doesn't match
        if type_filter and entry_type != type_filter:
            continue

        filepath = knowledge_dir / filename
        if filepath.exists():
            try:
                content = filepath.read_text()
                file_entries = parser(content, filename)
                entries.extend(file_entries)
            except Exception:
                pass  # Skip files that can't be parsed

    # Apply project filter
    if project_filter:
        entries = [
            e for e in entries if e.project == project_filter or e.project is None
        ]

    return entries


def format_for_claude(
    entries: list[KnowledgeEntry],
    query: str,
    max_entries: int = 20,
) -> str:
    """
    Format knowledge entries for Claude to evaluate relevance.

    Args:
        entries: List of knowledge entries to evaluate
        query: The search query
        max_entries: Maximum entries to include (to limit tokens)

    Returns:
        Formatted prompt for Claude
    """
    if not entries:
        return f"No knowledge entries found for query: {query}"

    # Limit entries to avoid token overflow
    limited_entries = entries[:max_entries]

    prompt_parts = [
        "# Semantic Knowledge Search",
        "",
        f"**Query:** {query}",
        "",
        f"**Task:** Rank the following {len(limited_entries)} knowledge entries by semantic relevance to the query.",
        "Return the top 5 most relevant entries with relevance scores (0-100).",
        "",
        "**Output Format:**",
        "```json",
        '[{"id": "entry-id", "score": 85, "reason": "Brief reason for relevance"}]',
        "```",
        "",
        "---",
        "",
        "## Knowledge Entries",
        "",
    ]

    for entry in limited_entries:
        # Truncate content to avoid token overflow
        content_preview = (
            entry.content[:500] + "..." if len(entry.content) > 500 else entry.content
        )
        prompt_parts.extend(
            [
                f"### [{entry.id}] {entry.title}",
                f"**Type:** {entry.type} | **File:** {entry.file}:{entry.line}",
                "",
                content_preview,
                "",
                "---",
                "",
            ]
        )

    return "\n".join(prompt_parts)


@dataclass
class SearchResult:
    """A ranked search result."""

    entry: KnowledgeEntry
    score: int
    reason: str


def parse_relevance_response(
    response: str,
    entries: list[KnowledgeEntry],
) -> list[SearchResult]:
    """
    Parse Claude's relevance ranking response.

    Args:
        response: Claude's response containing JSON rankings
        entries: Original entries for lookup

    Returns:
        List of SearchResult objects sorted by score
    """
    results: list[SearchResult] = []

    # Create lookup dict
    entry_map = {e.id: e for e in entries}

    # Try to extract JSON from response
    json_match = re.search(r"\[[\s\S]*?\]", response)
    if not json_match:
        return results

    try:
        rankings = json.loads(json_match.group())
        for item in rankings:
            entry_id = item.get("id", "")
            score = item.get("score", 0)
            reason = item.get("reason", "")

            if entry_id in entry_map:
                results.append(
                    SearchResult(
                        entry=entry_map[entry_id],
                        score=score,
                        reason=reason,
                    )
                )
    except json.JSONDecodeError:
        pass

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def main():
    """CLI interface for testing."""
    if len(sys.argv) < 2:
        print("Usage: semantic_search.py <command> [args]")
        print("Commands:")
        print("  list              - List all knowledge entries")
        print("  format <query>    - Format entries for Claude")
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        entries = load_knowledge_entries()
        for entry in entries:
            print(
                f"[{entry.type}] {entry.id}: {entry.title} ({entry.file}:{entry.line})"
            )

    elif command == "format":
        if len(sys.argv) < 3:
            print("Usage: semantic_search.py format <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        entries = load_knowledge_entries()
        prompt = format_for_claude(entries, query)
        print(prompt)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
