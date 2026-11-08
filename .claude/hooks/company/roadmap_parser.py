# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Roadmap Parser - Parse .planning/ROADMAP.md to structured JSON.

This module extracts XML task blocks from ROADMAP.md and returns
structured JSON with phases, tasks, and their metadata.

Usage as module:
    from roadmap_parser import parse_roadmap, parse_roadmap_file
    result = parse_roadmap_file(".planning/ROADMAP.md")

Usage as script:
    uv run roadmap_parser.py [--roadmap PATH] [--pretty]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PhaseStatus(str, Enum):
    """Status of a phase."""

    COMPLETE = "complete"
    IN_PROGRESS = "in_progress"
    PLANNED = "planned"
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    """Status of a task."""

    COMPLETE = "complete"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class TaskAction(str, Enum):
    """Action type for a task."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    UNKNOWN = "unknown"


@dataclass
class Task:
    """Represents a task extracted from ROADMAP.md."""

    id: str
    name: str
    status: TaskStatus = TaskStatus.UNKNOWN
    depends: list[str] = field(default_factory=list)
    file: str | None = None
    action: TaskAction = TaskAction.UNKNOWN
    description: str = ""
    acceptance: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "depends": self.depends,
            "file": self.file,
            "action": self.action.value,
            "description": self.description,
            "acceptance": self.acceptance,
        }


@dataclass
class Wave:
    """Represents a wave of tasks within a phase."""

    number: int
    name: str
    tasks: list[Task] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "number": self.number,
            "name": self.name,
            "tasks": [t.to_dict() for t in self.tasks],
        }


@dataclass
class Phase:
    """Represents a phase extracted from ROADMAP.md."""

    id: str
    name: str
    status: PhaseStatus = PhaseStatus.UNKNOWN
    is_current: bool = False
    goal: str = ""
    owner: str = ""
    source: str = ""
    waves: list[Wave] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        # Flatten tasks from waves if waves exist
        all_tasks = self.tasks.copy()
        for wave in self.waves:
            all_tasks.extend(wave.tasks)

        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "is_current": self.is_current,
            "goal": self.goal,
            "owner": self.owner,
            "source": self.source,
            "waves": [w.to_dict() for w in self.waves],
            "tasks": [t.to_dict() for t in all_tasks],
        }


@dataclass
class RoadmapResult:
    """Result of parsing a ROADMAP.md file."""

    phases: list[Phase] = field(default_factory=list)
    current_phase_id: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "phases": [p.to_dict() for p in self.phases],
            "current_phase_id": self.current_phase_id,
            "errors": self.errors,
        }

    def to_json(self, pretty: bool = False) -> str:
        """Convert to JSON string."""
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict())


def _parse_task_status(status_str: str) -> TaskStatus:
    """Parse task status string to enum."""
    status_lower = status_str.lower().strip()
    mapping = {
        "complete": TaskStatus.COMPLETE,
        "completed": TaskStatus.COMPLETE,
        "done": TaskStatus.COMPLETE,
        "in_progress": TaskStatus.IN_PROGRESS,
        "in-progress": TaskStatus.IN_PROGRESS,
        "wip": TaskStatus.IN_PROGRESS,
        "pending": TaskStatus.PENDING,
        "todo": TaskStatus.PENDING,
        "blocked": TaskStatus.BLOCKED,
    }
    return mapping.get(status_lower, TaskStatus.UNKNOWN)


def _parse_task_action(action_str: str) -> TaskAction:
    """Parse task action string to enum."""
    action_lower = action_str.lower().strip()
    mapping = {
        "create": TaskAction.CREATE,
        "modify": TaskAction.MODIFY,
        "update": TaskAction.MODIFY,
        "delete": TaskAction.DELETE,
        "remove": TaskAction.DELETE,
    }
    return mapping.get(action_lower, TaskAction.UNKNOWN)


def _parse_depends(depends_str: str) -> list[str]:
    """Parse depends attribute to list of task IDs."""
    if not depends_str or depends_str.strip() == "":
        return []
    # Split by comma or space
    parts = re.split(r"[,\s]+", depends_str.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_xml_element(xml_text: str, element: str) -> str:
    """Extract content from an XML element, handling multiline content."""
    # Pattern to match element with content
    pattern = rf"<{element}>(.*?)</{element}>"
    match = re.search(pattern, xml_text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        # Clean up indentation - find minimum indent and remove it
        lines = content.split("\n")
        if len(lines) > 1:
            # Find minimum non-empty indent
            non_empty_lines = [line for line in lines if line.strip()]
            if non_empty_lines:
                min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty_lines)
                lines = [
                    ln[min_indent:] if len(ln) >= min_indent else ln for ln in lines
                ]
                content = "\n".join(lines).strip()
        return content
    return ""


def _parse_task_xml(xml_text: str, errors: list[str]) -> Task | None:
    """Parse a single task XML block to a Task object."""
    # Extract task attributes
    task_match = re.search(
        r'<task\s+id="([^"]+)"\s+status="([^"]+)"\s+depends="([^"]*)"',
        xml_text,
        re.IGNORECASE,
    )

    if not task_match:
        # Try alternative format without depends
        task_match = re.search(
            r'<task\s+id="([^"]+)"\s+status="([^"]+)"',
            xml_text,
            re.IGNORECASE,
        )
        if not task_match:
            errors.append(f"Could not parse task attributes: {xml_text[:100]}...")
            return None
        task_id = task_match.group(1)
        status_str = task_match.group(2)
        depends_str = ""
    else:
        task_id = task_match.group(1)
        status_str = task_match.group(2)
        depends_str = task_match.group(3)

    # Extract child elements
    name = _extract_xml_element(xml_text, "name")
    file_path = _extract_xml_element(xml_text, "file")
    action_str = _extract_xml_element(xml_text, "action")
    description = _extract_xml_element(xml_text, "description")
    acceptance = _extract_xml_element(xml_text, "acceptance")

    if not name:
        errors.append(f"Task {task_id} missing name element")
        name = f"Task {task_id}"

    return Task(
        id=task_id,
        name=name,
        status=_parse_task_status(status_str),
        depends=_parse_depends(depends_str),
        file=file_path if file_path else None,
        action=_parse_task_action(action_str),
        description=description,
        acceptance=acceptance,
    )


def _parse_phase_status(status_line: str) -> PhaseStatus:
    """Parse phase status from status line."""
    status_lower = status_line.lower()
    if "complete" in status_lower:
        return PhaseStatus.COMPLETE
    if "in progress" in status_lower or "in_progress" in status_lower:
        return PhaseStatus.IN_PROGRESS
    if "planned" in status_lower:
        return PhaseStatus.PLANNED
    return PhaseStatus.UNKNOWN


def _extract_phase_id(header: str) -> str | None:
    """Extract phase ID from header (e.g., 'P14' from '## Current: P14 ...')."""
    match = re.search(r"P(\d+)", header, re.IGNORECASE)
    if match:
        return f"P{match.group(1)}"
    return None


def _extract_phase_name(header: str) -> str:
    """Extract phase name from header."""
    # Remove '## Current: ' or '## Previous: ' prefix
    cleaned = re.sub(r"^##\s*(Current|Previous):\s*", "", header, flags=re.IGNORECASE)
    # Remove phase ID (P14, etc.)
    cleaned = re.sub(r"P\d+\s*", "", cleaned)
    # Remove completion markers
    cleaned = re.sub(r"[✓✔].*$", "", cleaned)
    return cleaned.strip()


def parse_roadmap(content: str) -> RoadmapResult:
    """
    Parse ROADMAP.md content to structured data.

    Args:
        content: The raw markdown content of ROADMAP.md

    Returns:
        RoadmapResult with phases, tasks, and any parsing errors
    """
    result = RoadmapResult()

    if not content or not content.strip():
        result.errors.append("Empty or missing roadmap content")
        return result

    lines = content.split("\n")
    current_phase: Phase | None = None
    current_wave: Wave | None = None
    in_code_block = False
    code_block_content: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Track code blocks for XML parsing
        if line.strip().startswith("```xml"):
            in_code_block = True
            code_block_content = []
            i += 1
            continue
        elif line.strip() == "```" and in_code_block:
            in_code_block = False
            # Parse XML tasks from code block
            xml_content = "\n".join(code_block_content)
            # Find all task blocks
            task_matches = re.findall(
                r"<task\s+[^>]*>.*?</task>",
                xml_content,
                re.DOTALL,
            )
            for task_xml in task_matches:
                task = _parse_task_xml(task_xml, result.errors)
                if task:
                    if current_wave:
                        current_wave.tasks.append(task)
                    elif current_phase:
                        current_phase.tasks.append(task)
            code_block_content = []
            i += 1
            continue
        elif in_code_block:
            code_block_content.append(line)
            i += 1
            continue

        # Parse phase headers
        # Current phase: ## Current: P14 Multi-Project Orchestration
        current_match = re.match(
            r"^##\s*Current:\s*(P\d+)\s+(.+)$",
            line,
            re.IGNORECASE,
        )
        if current_match:
            # Save previous phase if exists
            if current_phase:
                result.phases.append(current_phase)

            phase_id = current_match.group(1)
            phase_name = current_match.group(2).strip()
            current_phase = Phase(
                id=phase_id,
                name=phase_name,
                is_current=True,
            )
            result.current_phase_id = phase_id
            current_wave = None
            i += 1
            continue

        # Previous phase: ## Previous: P13 Proactive Initiative Engine ✓ COMPLETE
        previous_match = re.match(
            r"^##\s*Previous:\s*(P\d+)\s+([^✓✔]+).*$",
            line,
            re.IGNORECASE,
        )
        if previous_match:
            if current_phase:
                result.phases.append(current_phase)

            phase_id = previous_match.group(1)
            phase_name = previous_match.group(2).strip()
            current_phase = Phase(
                id=phase_id,
                name=phase_name,
                is_current=False,
                status=PhaseStatus.COMPLETE,  # Previous phases are typically complete
            )
            current_wave = None
            i += 1
            continue

        # Wave headers: ## Wave 1: Foundation (Parallel)
        wave_match = re.match(
            r"^##\s*Wave\s+(\d+)[:\s]+(.+)$",
            line,
            re.IGNORECASE,
        )
        if wave_match and current_phase:
            wave_num = int(wave_match.group(1))
            wave_name = wave_match.group(2).strip()
            # Remove trailing markers like "(Parallel)"
            wave_name = re.sub(r"\s*\([^)]+\)\s*$", "", wave_name)
            current_wave = Wave(number=wave_num, name=wave_name)
            current_phase.waves.append(current_wave)
            i += 1
            continue

        # Parse phase metadata
        if current_phase:
            # **Status:** COMPLETE
            status_match = re.match(r"^\*\*Status:\*\*\s*(.+)$", line)
            if status_match:
                current_phase.status = _parse_phase_status(status_match.group(1))
                i += 1
                continue

            # **Goal:** ...
            goal_match = re.match(r"^\*\*Goal:\*\*\s*(.+)$", line)
            if goal_match:
                current_phase.goal = goal_match.group(1).strip()
                i += 1
                continue

            # **Owner:** ...
            owner_match = re.match(r"^\*\*Owner:\*\*\s*(.+)$", line)
            if owner_match:
                current_phase.owner = owner_match.group(1).strip()
                i += 1
                continue

            # **Source:** ...
            source_match = re.match(r"^\*\*Source:\*\*\s*(.+)$", line)
            if source_match:
                current_phase.source = source_match.group(1).strip()
                i += 1
                continue

        i += 1

    # Don't forget the last phase
    if current_phase:
        result.phases.append(current_phase)

    return result


def parse_roadmap_file(path: str | Path) -> RoadmapResult:
    """
    Parse a ROADMAP.md file to structured data.

    Args:
        path: Path to the ROADMAP.md file

    Returns:
        RoadmapResult with phases, tasks, and any parsing errors
    """
    path = Path(path)

    if not path.exists():
        result = RoadmapResult()
        result.errors.append(f"File not found: {path}")
        return result

    if not path.is_file():
        result = RoadmapResult()
        result.errors.append(f"Not a file: {path}")
        return result

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        result = RoadmapResult()
        result.errors.append(f"Error reading file: {e}")
        return result

    return parse_roadmap(content)


def get_task_by_id(result: RoadmapResult, task_id: str) -> Task | None:
    """Find a task by its ID across all phases."""
    for phase in result.phases:
        for task in phase.tasks:
            if task.id == task_id:
                return task
        for wave in phase.waves:
            for task in wave.tasks:
                if task.id == task_id:
                    return task
    return None


def get_tasks_by_status(
    result: RoadmapResult,
    status: TaskStatus,
) -> list[tuple[Phase, Task]]:
    """Get all tasks with a specific status, along with their phase."""
    matches: list[tuple[Phase, Task]] = []
    for phase in result.phases:
        for task in phase.tasks:
            if task.status == status:
                matches.append((phase, task))
        for wave in phase.waves:
            for task in wave.tasks:
                if task.status == status:
                    matches.append((phase, task))
    return matches


def get_current_phase(result: RoadmapResult) -> Phase | None:
    """Get the current phase from the result."""
    for phase in result.phases:
        if phase.is_current:
            return phase
    return None


def get_phase_by_id(result: RoadmapResult, phase_id: str) -> Phase | None:
    """Find a phase by its ID."""
    for phase in result.phases:
        if phase.id == phase_id:
            return phase
    return None


def main() -> int:
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Parse ROADMAP.md to structured JSON",
    )
    parser.add_argument(
        "--roadmap",
        "-r",
        type=str,
        default=".planning/ROADMAP.md",
        help="Path to ROADMAP.md file (default: .planning/ROADMAP.md)",
    )
    parser.add_argument(
        "--pretty",
        "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--current",
        "-c",
        action="store_true",
        help="Only output current phase",
    )
    parser.add_argument(
        "--task",
        "-t",
        type=str,
        help="Output specific task by ID",
    )
    parser.add_argument(
        "--status",
        "-s",
        type=str,
        choices=["complete", "in_progress", "pending", "blocked"],
        help="Filter tasks by status",
    )

    args = parser.parse_args()

    result = parse_roadmap_file(args.roadmap)

    if result.errors:
        for error in result.errors:
            print(f"Warning: {error}", file=sys.stderr)

    # Handle specific output modes
    if args.task:
        task = get_task_by_id(result, args.task)
        if task:
            output = task.to_dict()
        else:
            print(f"Task not found: {args.task}", file=sys.stderr)
            return 1
    elif args.current:
        phase = get_current_phase(result)
        if phase:
            output = phase.to_dict()
        else:
            print("No current phase found", file=sys.stderr)
            return 1
    elif args.status:
        status = TaskStatus(args.status)
        matches = get_tasks_by_status(result, status)
        output = {
            "status": args.status,
            "count": len(matches),
            "tasks": [{"phase_id": p.id, **t.to_dict()} for p, t in matches],
        }
    else:
        output = result.to_dict()

    if args.pretty:
        print(json.dumps(output, indent=2))
    else:
        print(json.dumps(output))

    return 0


if __name__ == "__main__":
    sys.exit(main())
