#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Roadmap Scheduler — parse ROADMAP.md XML tasks and schedule them to work queue.

P27 Implementation: Plan-Driven Daemon with Semantic Duplicate Detection.

This module provides:
1. XML task parsing from ROADMAP.md
2. Dependency wave detection and scheduling
3. State tracking for scheduled tasks
4. Integration with work_allocator for queue management

ROADMAP.md Task Format (GSD XML):
```xml
<task id="P14-1.1" wave="1" status="pending">
  <title>Create roadmap_scheduler.py</title>
  <description>Parse ROADMAP.md XML tasks...</description>
  <complexity>standard</complexity>
  <depends-on>P14-0.1</depends-on>
</task>
```

Usage:
    # Scan ROADMAP.md and schedule eligible tasks
    python roadmap_scheduler.py scan

    # Get scheduling status
    python roadmap_scheduler.py status

    # Schedule specific task (if dependencies satisfied)
    python roadmap_scheduler.py schedule --task-id "P14-1.1"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
work_allocator = None
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global work_allocator, company_resolver

    if work_allocator is not None:
        return

    try:
        from . import company_resolver as cr
        from . import work_allocator as wa

        work_allocator = wa
        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        work_allocator = wa
        company_resolver = cr


# =============================================================================
# Configuration
# =============================================================================

ROADMAP_STATE_FILE = "roadmap_state.json"
DEFAULT_SCAN_INTERVAL_MINUTES = 15
DEFAULT_MAX_TASKS_PER_SCAN = 10

# Admission-rejection backoff (2026-09-04). A roadmap task that add_task
# refuses — the admission gate rejected it, or its work is already shipped —
# never reaches tasks_scheduled, so every scan re-minted it: nothing on the
# scheduler side remembered the refusal. SCOUT-20260820-1 was minted and
# rejected every scan for 28 hours (85 rejections, three unread escalations)
# after its work had shipped in #422; the gate's target check could not find
# `author_association` because the implementation used GitHub's
# collaborators/permission API instead. The gate was right to refuse and the
# scheduler was wrong to keep asking.
#
# Each refusal is recorded against the roadmap id with a fingerprint of the
# entry's text. The wait doubles per refusal (1h, 2h, 4h ...); after
# maxAdmissionRejections the entry is PARKED and never re-minted — until the
# intake file's text for it changes, which clears the record. Editing the
# brief is the one action that can make a rejected entry admissible, so it is
# the one action that re-opens it. Both knobs live under roadmapScheduling.
DEFAULT_REJECTION_BACKOFF_HOURS = 1.0
DEFAULT_MAX_ADMISSION_REJECTIONS = 3
# add_task errors that mean "this entry will not be admitted as written".
# duplicate_task is deliberately excluded: the duplicate may still be in
# flight, and re-checking each scan is what lets the entry schedule once the
# dedup horizon passes.
_REFUSAL_ERRORS = frozenset({"admission_rejected", "already_shipped"})


def load_config() -> dict:
    """
    Load roadmap scheduling configuration from gatesmith.json.

    Returns:
        dict with configuration values:
        - enabled: bool
        - scanIntervalMinutes: int
        - autoScheduleWaves: bool
        - respectDependencies: bool
        - intakeFiles: list[str] — extra roadmap files (repo-relative) beyond
          the hardcoded discovery paths, e.g. externally PR-owned intake files
        - maxTasksPerScan: int — cap on tasks scheduled per scan (0 = unlimited)
        - rejectionBackoffHours: float — wait after the first refusal; doubles
          per further refusal of the same unchanged entry
        - maxAdmissionRejections: int — refusals after which an unchanged
          entry is parked (never re-minted until its intake text changes)
    """
    # Root gatesmith.json is canonical (#1052); the .claude/ copy is a legacy
    # fallback. The loop returns on the FIRST existing file, so with the legacy
    # copy listed first, edits to the canonical root section were silently
    # ignored whenever both files existed (bin-audit finding, 2026-07-17).
    config_paths = [
        Path.cwd() / "gatesmith.json",
        Path.cwd() / ".claude" / "gatesmith.json",
    ]

    defaults = {
        "enabled": True,
        "scanIntervalMinutes": DEFAULT_SCAN_INTERVAL_MINUTES,
        "autoScheduleWaves": True,
        "respectDependencies": True,
        "intakeFiles": [],
        "maxTasksPerScan": DEFAULT_MAX_TASKS_PER_SCAN,
        "rejectionBackoffHours": DEFAULT_REJECTION_BACKOFF_HOURS,
        "maxAdmissionRejections": DEFAULT_MAX_ADMISSION_REJECTIONS,
    }

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                if "roadmapScheduling" not in config:
                    # A config file without the section must not shadow one that
                    # has it — older installs carry roadmapScheduling only in the
                    # legacy .claude/ copy while a sectionless root file exists.
                    continue
                roadmap_config = config.get("roadmapScheduling", {})
                return {
                    "enabled": roadmap_config.get("enabled", defaults["enabled"]),
                    "scanIntervalMinutes": roadmap_config.get(
                        "scanIntervalMinutes", defaults["scanIntervalMinutes"]
                    ),
                    "autoScheduleWaves": roadmap_config.get(
                        "autoScheduleWaves", defaults["autoScheduleWaves"]
                    ),
                    "respectDependencies": roadmap_config.get(
                        "respectDependencies", defaults["respectDependencies"]
                    ),
                    "intakeFiles": roadmap_config.get(
                        "intakeFiles", defaults["intakeFiles"]
                    ),
                    "maxTasksPerScan": roadmap_config.get(
                        "maxTasksPerScan", defaults["maxTasksPerScan"]
                    ),
                    "rejectionBackoffHours": roadmap_config.get(
                        "rejectionBackoffHours", defaults["rejectionBackoffHours"]
                    ),
                    "maxAdmissionRejections": roadmap_config.get(
                        "maxAdmissionRejections",
                        defaults["maxAdmissionRejections"],
                    ),
                }
            except (json.JSONDecodeError, OSError):
                pass

    return defaults


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class RoadmapTask:
    """A task parsed from ROADMAP.md XML."""

    id: str
    title: str
    description: str = ""
    wave: int = 1
    status: str = "pending"  # pending, in_progress, completed, blocked
    complexity: str = "standard"  # trivial, standard, complex, epic
    depends_on: list[str] = field(default_factory=list)
    phase_id: str = ""
    file_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "wave": self.wave,
            "status": self.status,
            "complexity": self.complexity,
            "depends_on": self.depends_on,
            "phase_id": self.phase_id,
            "file_path": self.file_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoadmapTask":
        """Create from dictionary."""
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            wave=data.get("wave", 1),
            status=data.get("status", "pending"),
            complexity=data.get("complexity", "standard"),
            depends_on=data.get("depends_on", []),
            phase_id=data.get("phase_id", ""),
            file_path=data.get("file_path", ""),
        )


@dataclass
class RoadmapState:
    """State tracking for roadmap scheduling."""

    last_scan: str = ""
    tasks_scheduled: list[str] = field(default_factory=list)  # List of task IDs
    tasks_completed: list[str] = field(default_factory=list)
    tasks_in_progress: list[str] = field(default_factory=list)
    current_wave: int = 1
    active_phase: str = ""
    # Maps roadmap task ID -> queue task ID for already-scheduled tasks.
    # Used to translate roadmap-ID deps into queue IDs before calling add_task.
    roadmap_id_to_queue_id: dict[str, str] = field(default_factory=dict)
    # Roadmap task ID -> refusal record: {"count", "last_ts", "error",
    # "reason", "fingerprint", "title"}. See DEFAULT_REJECTION_BACKOFF_HOURS.
    tasks_rejected: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "last_scan": self.last_scan,
            "tasks_scheduled": self.tasks_scheduled,
            "tasks_completed": self.tasks_completed,
            "tasks_in_progress": self.tasks_in_progress,
            "current_wave": self.current_wave,
            "active_phase": self.active_phase,
            "roadmap_id_to_queue_id": self.roadmap_id_to_queue_id,
            "tasks_rejected": self.tasks_rejected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoadmapState":
        """Create from dictionary."""
        rejected = data.get("tasks_rejected", {})
        return cls(
            last_scan=data.get("last_scan", ""),
            tasks_scheduled=data.get("tasks_scheduled", []),
            tasks_completed=data.get("tasks_completed", []),
            tasks_in_progress=data.get("tasks_in_progress", []),
            current_wave=data.get("current_wave", 1),
            active_phase=data.get("active_phase", ""),
            roadmap_id_to_queue_id=data.get("roadmap_id_to_queue_id", {}),
            tasks_rejected=rejected if isinstance(rejected, dict) else {},
        )


# =============================================================================
# Admission-rejection backoff
# =============================================================================


def task_fingerprint(task: RoadmapTask) -> str:
    """Stable hash of the parts of an entry the admission gate reads.

    Whitespace-normalised so a reflow of the intake file does not count as
    an edit; anything that changes a word does.
    """
    text = " ".join(f"{task.title}\n{task.description}".split())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def rejection_holds(
    record: dict[str, Any] | None,
    task: RoadmapTask,
    config: dict,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Whether a refusal record still keeps *task* off the schedule.

    Returns (holds, why). why is "" when it does not hold, "edited" when the
    entry's text changed since the refusal (the record is stale and the
    caller should drop it), "parked" once the refusal count reached the
    ceiling, or "backoff" while the doubling wait is still running. Any
    malformed record fails OPEN — a corrupt state entry must never keep a
    real task off the queue.
    """
    if not isinstance(record, dict):
        return False, ""
    if record.get("fingerprint") != task_fingerprint(task):
        return False, "edited"
    try:
        count = int(record.get("count", 0))
        ceiling = int(
            config.get("maxAdmissionRejections", DEFAULT_MAX_ADMISSION_REJECTIONS)
        )
        base_hours = float(
            config.get("rejectionBackoffHours", DEFAULT_REJECTION_BACKOFF_HOURS)
        )
        last = datetime.fromisoformat(
            str(record.get("last_ts", "")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False, ""
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if count <= 0:
        return False, ""
    if ceiling > 0 and count >= ceiling:
        return True, "parked"
    if base_hours <= 0:
        return False, ""
    # Exponent capped so a never-park configuration (ceiling 0) cannot
    # overflow timedelta; 2**16 hours is seven years, which is "parked".
    wait = timedelta(hours=base_hours * (2 ** min(count - 1, 16)))
    current = now or datetime.now(timezone.utc)
    if current - last < wait:
        return True, "backoff"
    return False, ""


def record_rejection(
    state: RoadmapState,
    task: RoadmapTask,
    result: dict,
    config: dict | None = None,
) -> dict:
    """Remember that add_task refused *task*; returns the updated record.

    A refusal of an entry whose text changed since the last refusal starts
    the count over — the edit was the operator's answer to the earlier
    rejection, and it deserves a fresh run at the gate. The record carries
    a ``parked`` flag so read-only views (scout_status) can show the state
    without knowing the ceiling.
    """
    fingerprint = task_fingerprint(task)
    prior = state.tasks_rejected.get(task.id)
    count = 0
    if isinstance(prior, dict) and prior.get("fingerprint") == fingerprint:
        try:
            count = int(prior.get("count", 0))
        except (TypeError, ValueError):
            count = 0
    ceiling = DEFAULT_MAX_ADMISSION_REJECTIONS
    if isinstance(config, dict):
        try:
            ceiling = int(config.get("maxAdmissionRejections", ceiling))
        except (TypeError, ValueError):
            pass
    record = {
        "count": count + 1,
        "last_ts": datetime.now(timezone.utc).isoformat(),
        "error": str(result.get("error") or ""),
        "reason": str(result.get("reason") or result.get("message") or "")[:400],
        "fingerprint": fingerprint,
        "title": task.title[:200],
        "parked": ceiling > 0 and count + 1 >= ceiling,
    }
    state.tasks_rejected[task.id] = record
    return record


# =============================================================================
# State Management
# =============================================================================


def get_state_path() -> Path:
    """Get the path to the roadmap state file."""
    _ensure_imports()
    company_dir = company_resolver.get_company_dir()
    return company_dir / ROADMAP_STATE_FILE


def load_state() -> RoadmapState:
    """Load roadmap scheduling state from file."""
    state_path = get_state_path()

    if not state_path.exists():
        return RoadmapState()

    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        return RoadmapState.from_dict(data)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"warning: {state_path} is corrupt or unreadable ({e});"
            " starting with empty scheduling state."
            " Completed tasks may be re-scheduled.",
            file=sys.stderr,
        )
        return RoadmapState()


def save_state(state: RoadmapState) -> None:
    """Save roadmap scheduling state to file."""
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically (prevents corruption under parallel workers)
    fd, tmp_path = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.replace(tmp_path, str(state_path))
    except BaseException:
        os.unlink(tmp_path)
        raise


# =============================================================================
# XML Parsing
# =============================================================================


def parse_task_xml(xml_content: str, file_path: str = "") -> RoadmapTask | None:
    """
    Parse a single task XML block.

    Args:
        xml_content: XML content for one task (including <task> tags)
        file_path: Path to the source file (for reference)

    Returns:
        RoadmapTask if parsing succeeds, None otherwise
    """
    # Extract task attributes (flexible order)
    # First, find the opening task tag
    task_tag_match = re.search(r"<task\s+([^>]+)>", xml_content, re.IGNORECASE)

    if not task_tag_match:
        return None

    attrs = task_tag_match.group(1)

    # Extract individual attributes
    id_match = re.search(r'id=["\']([^"\']+)["\']', attrs)
    if not id_match:
        return None
    task_id = id_match.group(1)

    wave_match = re.search(r'wave=["\'](\d+)["\']', attrs)
    wave = int(wave_match.group(1)) if wave_match else 1

    status_match = re.search(r'status=["\']([^"\']+)["\']', attrs)
    status = status_match.group(1) if status_match else "pending"

    # Extract title (supports both <title> and <name> tags)
    title_match = re.search(
        r"<title>\s*(.+?)\s*</title>", xml_content, re.IGNORECASE | re.DOTALL
    )
    if not title_match:
        # Fallback to <name> tag (GSD format)
        title_match = re.search(
            r"<name>\s*(.+?)\s*</name>", xml_content, re.IGNORECASE | re.DOTALL
        )
    title = title_match.group(1).strip() if title_match else ""

    # Extract description
    desc_match = re.search(
        r"<description>\s*(.+?)\s*</description>",
        xml_content,
        re.IGNORECASE | re.DOTALL,
    )
    description = desc_match.group(1).strip() if desc_match else ""

    # Extract complexity
    complexity_match = re.search(
        r"<complexity>\s*(.+?)\s*</complexity>", xml_content, re.IGNORECASE
    )
    complexity = (
        complexity_match.group(1).strip().lower() if complexity_match else "standard"
    )

    # Extract dependencies (supports both <depends-on> tag and depends="" attribute)
    depends_on = []

    # First try <depends-on> tags
    for dep_match in re.finditer(
        r"<depends-on>\s*(.+?)\s*</depends-on>", xml_content, re.IGNORECASE
    ):
        depends_on.append(dep_match.group(1).strip())

    # Also check depends="" attribute in task tag (comma-separated)
    if not depends_on:
        depends_attr = re.search(r'depends=["\']([^"\']*)["\']', attrs)
        if depends_attr and depends_attr.group(1).strip():
            depends_on = [
                d.strip() for d in depends_attr.group(1).split(",") if d.strip()
            ]

    # Extract phase from task ID (e.g., "P14-1.1" -> "P14", or "14.1" -> "P14")
    phase_match = re.match(r"([A-Z]*\d+)", task_id)
    if phase_match:
        phase_id = phase_match.group(1)
        # Add P prefix if missing (e.g., "14" -> "P14")
        if phase_id and phase_id[0].isdigit():
            phase_id = f"P{phase_id}"
    else:
        phase_id = ""

    return RoadmapTask(
        id=task_id,
        title=title,
        description=description,
        wave=wave,
        status=status,
        complexity=complexity,
        depends_on=depends_on,
        phase_id=phase_id,
        file_path=file_path,
    )


def get_external_intake_files() -> list[Path]:
    """
    Resolve configured external intake files (roadmapScheduling.intakeFiles).

    Intake files are extra roadmap sources owned by external processes
    (e.g. the opportunity scout's PR-managed .planning/ROADMAP-SCOUT.md).
    Entries are repo-relative; anything resolving outside the repo root is
    refused, and missing files are silently skipped.

    Returns:
        List of resolved Path objects to existing intake files
    """
    config = load_config()
    root = Path.cwd().resolve()

    intake_files = []
    for entry in config.get("intakeFiles", []):
        if not isinstance(entry, str) or not entry.strip():
            continue
        candidate = (Path.cwd() / entry).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue  # path escapes the repo root
        if candidate.is_file():
            intake_files.append(candidate)

    return intake_files


def find_roadmap_files(include_external: bool = True) -> list[Path]:
    """
    Find all ROADMAP.md files in the project.

    Args:
        include_external: Also include configured external intake files.
            Pass False for operations that must never touch externally-owned
            files (e.g. status write-back).

    Returns:
        List of Path objects to ROADMAP.md files
    """
    roadmap_files = []

    # Check common locations
    planning_dir = Path.cwd() / ".planning"
    if planning_dir.exists():
        roadmap = planning_dir / "ROADMAP.md"
        if roadmap.exists():
            roadmap_files.append(roadmap)

    # Also check project root
    root_roadmap = Path.cwd() / "ROADMAP.md"
    if root_roadmap.exists():
        roadmap_files.append(root_roadmap)

    if include_external:
        known = {p.resolve() for p in roadmap_files}
        for intake in get_external_intake_files():
            if intake.resolve() not in known:
                roadmap_files.append(intake)
                known.add(intake.resolve())

    return roadmap_files


def parse_roadmap_file(file_path: Path) -> list[RoadmapTask]:
    """
    Parse all tasks from a ROADMAP.md file.

    Args:
        file_path: Path to the ROADMAP.md file

    Returns:
        List of RoadmapTask objects
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return []

    tasks = []

    # Find all task blocks
    task_pattern = re.compile(r"<task\s+[^>]*>.*?</task>", re.IGNORECASE | re.DOTALL)

    for match in task_pattern.finditer(content):
        task = parse_task_xml(match.group(0), str(file_path))
        if task:
            tasks.append(task)

    return tasks


def scan_all_roadmaps() -> list[RoadmapTask]:
    """
    Scan all ROADMAP.md files and return all tasks.

    Returns:
        List of all RoadmapTask objects from all roadmap files
    """
    all_tasks = []

    for roadmap_path in find_roadmap_files():
        tasks = parse_roadmap_file(roadmap_path)
        all_tasks.extend(tasks)

    return all_tasks


# =============================================================================
# Dependency Resolution
# =============================================================================


def check_dependencies_satisfied(
    task: RoadmapTask, state: RoadmapState, all_tasks: list[RoadmapTask]
) -> bool:
    """
    Check if a task's dependencies are satisfied.

    Dependencies are satisfied if:
    1. Task has no dependencies, OR
    2. All dependencies are in the completed list

    Args:
        task: The task to check
        state: Current roadmap state
        all_tasks: All known tasks (for dependency resolution)

    Returns:
        True if dependencies are satisfied
    """
    if not task.depends_on:
        return True

    for dep_id in task.depends_on:
        if dep_id not in state.tasks_completed:
            return False

    return True


def get_schedulable_tasks(
    tasks: list[RoadmapTask], state: RoadmapState, config: dict
) -> list[RoadmapTask]:
    """
    Get tasks that can be scheduled to the work queue.

    A task is schedulable if:
    1. Status is "pending"
    2. Not already scheduled or in progress
    3. Not inside an admission-rejection backoff, nor parked after repeated
       refusals of the same unchanged entry (see rejection_holds)
    4. Dependencies are satisfied (if respectDependencies is True)
    5. In the current or earlier wave (if autoScheduleWaves is True)

    Args:
        tasks: All roadmap tasks
        state: Current scheduling state
        config: Roadmap scheduling configuration

    Returns:
        List of tasks eligible for scheduling
    """
    schedulable = []

    already_handled = set(
        state.tasks_scheduled + state.tasks_in_progress + state.tasks_completed
    )

    for task in tasks:
        # Skip if not pending
        if task.status != "pending":
            continue

        # Skip if already scheduled/in_progress/completed
        if task.id in already_handled:
            continue

        # Skip while a refusal still holds; drop a record whose entry was
        # edited so the rewritten brief gets a fresh run at the gate.
        holds, why = rejection_holds(state.tasks_rejected.get(task.id), task, config)
        if holds:
            continue
        if why == "edited":
            state.tasks_rejected.pop(task.id, None)

        # Check dependencies
        if config["respectDependencies"]:
            if not check_dependencies_satisfied(task, state, tasks):
                continue

        # Check wave (only schedule current wave or earlier)
        if config["autoScheduleWaves"]:
            if task.wave > state.current_wave:
                continue

        schedulable.append(task)

    return schedulable


# =============================================================================
# Task Scheduling
# =============================================================================


def schedule_task(task: RoadmapTask, state: RoadmapState) -> dict:
    """
    Schedule a roadmap task to the work queue.

    WS-108: Enhanced to preserve full roadmap context in task description.

    Args:
        task: RoadmapTask to schedule
        state: Current roadmap state (will be updated)

    Returns:
        Result dict from work_allocator.add_task
    """
    _ensure_imports()

    # Map complexity to priority
    complexity_priority_map = {
        "trivial": 4,
        "standard": 3,
        "complex": 2,
        "epic": 1,
    }
    priority = complexity_priority_map.get(task.complexity, 3)

    # WS-108: Build rich description with roadmap context
    description_parts = [task.description]

    # Add dependencies if present
    if task.depends_on:
        description_parts.append("")
        description_parts.append(f"**Dependencies:** {', '.join(task.depends_on)}")

    # Add roadmap context
    description_parts.append("")
    description_parts.append("---")
    description_parts.append(f"**Roadmap Task ID:** {task.id}")
    description_parts.append(f"**Phase:** {task.phase_id}")
    description_parts.append(f"**Wave:** {task.wave}")
    description_parts.append(f"**Complexity:** {task.complexity}")
    if task.file_path:
        description_parts.append(f"**Source:** `{task.file_path}`")

    rich_description = "\n".join(description_parts)

    # Translate roadmap-ID deps to queue IDs before passing to add_task.
    # Raw roadmap IDs (e.g. "P14-0.1") can never resolve in update_dependencies
    # because that function only knows queue task IDs and source_goals.
    translated_deps: list[str] = []
    for dep in task.depends_on:
        if dep in state.tasks_completed:
            # Roadmap layer already verified this dep complete — omit entirely.
            continue
        queue_dep = state.roadmap_id_to_queue_id.get(dep)
        if queue_dep:
            translated_deps.append(queue_dep)
        else:
            # Dep not yet in the map (not yet scheduled); pass the raw roadmap
            # ID. Existing falsely-blocked tasks self-heal via Part B in
            # update_dependencies (notes JSON roadmap_task_id lookup).
            translated_deps.append(dep)

    # Add task to work queue with roadmap metadata
    result = work_allocator.add_task(
        title=task.title,
        description=rich_description,
        priority=priority,
        estimated_complexity=task.complexity,
        source="planning",  # Mark as plan-driven
        dependencies=translated_deps if translated_deps else None,
    )

    if result.get("success"):
        # Update state
        state.tasks_scheduled.append(task.id)

        # Store roadmap metadata in the task (via update)
        queue_task_id = result.get("task_id")
        if queue_task_id:
            # Record roadmap→queue mapping so later tasks can translate their deps.
            state.roadmap_id_to_queue_id[task.id] = queue_task_id
            # We'll add custom fields via the task notes
            work_allocator.update_task(
                task_id=queue_task_id,
                notes=json.dumps(
                    {
                        "roadmap_task_id": task.id,
                        "roadmap_phase_id": task.phase_id,
                        "roadmap_wave": task.wave,
                        "roadmap_file": task.file_path,
                    }
                ),
            )

    return result


def schedule_wave(wave: int, tasks: list[RoadmapTask], state: RoadmapState) -> dict:
    """
    Schedule all tasks in a specific wave.

    Args:
        wave: Wave number to schedule
        tasks: All roadmap tasks
        state: Current roadmap state

    Returns:
        Dict with scheduling results
    """
    config = load_config()

    wave_tasks = [t for t in tasks if t.wave == wave]
    schedulable = get_schedulable_tasks(wave_tasks, state, config)

    results = {
        "wave": wave,
        "total_tasks": len(wave_tasks),
        "scheduled": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }

    for task in schedulable:
        result = schedule_task(task, state)

        if result.get("success"):
            results["scheduled"] += 1
            results["details"].append(
                {
                    "task_id": task.id,
                    "status": "scheduled",
                    "queue_task_id": result.get("task_id"),
                }
            )
        elif result.get("error") == "duplicate_task":
            results["skipped"] += 1
            results["details"].append(
                {
                    "task_id": task.id,
                    "status": "skipped",
                    "reason": "duplicate",
                }
            )
        else:
            results["failed"] += 1
            results["details"].append(
                {
                    "task_id": task.id,
                    "status": "failed",
                    "reason": result.get("message", "unknown"),
                }
            )

    # Save updated state
    save_state(state)

    return results


def run_scheduling_scan() -> dict:
    """
    Run a full scheduling scan.

    1. Parse all ROADMAP.md files
    2. Find schedulable tasks
    3. Schedule them to the work queue
    4. Update state

    Returns:
        Dict with scan results
    """
    config = load_config()

    if not config["enabled"]:
        return {"success": False, "reason": "scheduling_disabled"}

    state = load_state()
    tasks = scan_all_roadmaps()

    if not tasks:
        state.last_scan = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return {
            "success": True,
            "tasks_found": 0,
            "tasks_scheduled": 0,
            "message": "No roadmap tasks found",
        }

    # Get schedulable tasks
    schedulable = get_schedulable_tasks(tasks, state, config)

    # If no schedulable tasks and auto-promotion is enabled, try to promote next phase
    promotion_result = None
    if not schedulable and config.get("autoPromotePhases", True):
        # Check if all current pending tasks are complete. External intake
        # files never get status write-back, so a completed task can stay
        # "pending" in-file forever — exclude state-completed ids too.
        pending_tasks = [
            t
            for t in tasks
            if t.status == "pending"
            and t.id not in state.tasks_scheduled
            and t.id not in state.tasks_completed
        ]
        if not pending_tasks:
            promotion_result = check_and_promote_next_phase()
            if promotion_result and promotion_result.get("success"):
                # Re-scan to get the new tasks
                tasks = scan_all_roadmaps()
                schedulable = get_schedulable_tasks(tasks, state, config)

    # Cap tasks scheduled per scan — bounds the blast radius of a bad merge
    # (e.g. a malformed intake file); the next scan picks up the remainder.
    max_per_scan = config.get("maxTasksPerScan", DEFAULT_MAX_TASKS_PER_SCAN)
    if isinstance(max_per_scan, int) and max_per_scan > 0:
        schedulable = schedulable[:max_per_scan]

    scheduled_count = 0
    skipped_count = 0
    failed_count = 0
    rejected_count = 0
    details = []

    for task in schedulable:
        result = schedule_task(task, state)

        if result.get("success"):
            scheduled_count += 1
            state.tasks_rejected.pop(task.id, None)
            details.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": "scheduled",
                }
            )
        elif result.get("error") == "duplicate_task":
            skipped_count += 1
        elif result.get("error") in _REFUSAL_ERRORS:
            # Remember the refusal so the next scan does not re-mint the
            # same entry; the record decides the wait (see rejection_holds).
            record = record_rejection(state, task, result, config)
            rejected_count += 1
            details.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": "rejected",
                    "error": record["error"],
                    "reason": record["reason"],
                    "rejections": record["count"],
                }
            )
        else:
            failed_count += 1
            details.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": "failed",
                    "reason": result.get("message"),
                }
            )

    # Update state
    state.last_scan = datetime.now(timezone.utc).isoformat()
    save_state(state)

    result = {
        "success": True,
        "tasks_found": len(tasks),
        "tasks_schedulable": len(schedulable),
        "tasks_scheduled": scheduled_count,
        "tasks_skipped": skipped_count,
        "tasks_failed": failed_count,
        "tasks_rejected": rejected_count,
        "current_wave": state.current_wave,
        "details": details,
    }

    # Include promotion info if a phase was promoted
    if promotion_result and promotion_result.get("success"):
        result["phase_promoted"] = promotion_result

    return result


def advance_wave(state: RoadmapState, tasks: list[RoadmapTask]) -> bool:
    """
    Check if the current wave is complete and advance to the next.

    A wave is complete when all tasks in that wave are in tasks_completed.

    Args:
        state: Current roadmap state
        tasks: All roadmap tasks

    Returns:
        True if wave was advanced
    """
    current_wave_tasks = [t for t in tasks if t.wave == state.current_wave]

    if not current_wave_tasks:
        return False

    # Check if all tasks in current wave are completed
    all_completed = all(t.id in state.tasks_completed for t in current_wave_tasks)

    if all_completed:
        state.current_wave += 1
        save_state(state)
        return True

    return False


def mark_task_completed(roadmap_task_id: str) -> dict:
    """
    Mark a roadmap task as completed.

    Called when a work queue task (source="planning") completes.

    Args:
        roadmap_task_id: The roadmap task ID (e.g., "P14-1.1")

    Returns:
        Dict with result
    """
    state = load_state()

    if roadmap_task_id in state.tasks_completed:
        return {"success": True, "already_completed": True}

    # Move from scheduled/in_progress to completed
    if roadmap_task_id in state.tasks_scheduled:
        state.tasks_scheduled.remove(roadmap_task_id)
    if roadmap_task_id in state.tasks_in_progress:
        state.tasks_in_progress.remove(roadmap_task_id)

    state.tasks_completed.append(roadmap_task_id)

    # Check if wave should advance
    tasks = scan_all_roadmaps()
    wave_advanced = advance_wave(state, tasks)

    save_state(state)

    return {
        "success": True,
        "task_id": roadmap_task_id,
        "wave_advanced": wave_advanced,
        "current_wave": state.current_wave,
    }


def update_roadmap_task_status(task_id: str, new_status: str) -> dict:
    """
    Update a task's status attribute in ROADMAP.md XML in-place.

    Finds the task by ID in all ROADMAP.md files and updates its
    status="..." attribute to the new status value.

    Args:
        task_id: The roadmap task ID (e.g., "P14-1.1" or "15.1")
        new_status: New status value (e.g., "complete", "in_progress")

    Returns:
        Dict with result: success, file updated, or error info
    """
    try:
        # Never rewrite external intake files — they are PR-owned and
        # read-only to the daemon; completion truth lives in state.
        roadmap_files = find_roadmap_files(include_external=False)
        if not roadmap_files:
            return {"success": False, "error": "No ROADMAP.md files found"}

        for roadmap_path in roadmap_files:
            content = roadmap_path.read_text(encoding="utf-8")

            # Match task tag with this ID and replace its status attribute
            pattern = re.compile(
                rf'(<task\s+[^>]*id=["\']){re.escape(task_id)}(["\'][^>]*)'
                rf'status=["\'][^"\']*["\']',
                re.IGNORECASE,
            )

            match = pattern.search(content)
            if match:
                # Rebuild the tag with new status
                new_content = pattern.sub(
                    rf'\g<1>{task_id}\g<2>status="{new_status}"',
                    content,
                )

                if new_content != content:
                    roadmap_path.write_text(new_content, encoding="utf-8")
                    return {
                        "success": True,
                        "task_id": task_id,
                        "new_status": new_status,
                        "file": str(roadmap_path),
                    }

        return {"success": False, "error": f"Task {task_id} not found in ROADMAP files"}
    except Exception as e:
        # Non-fatal — ROADMAP write-back is best-effort
        return {"success": False, "error": str(e)}


def mark_task_in_progress(roadmap_task_id: str) -> dict:
    """
    Mark a roadmap task as in progress.

    Called when a work queue task (source="planning") is claimed.

    Args:
        roadmap_task_id: The roadmap task ID

    Returns:
        Dict with result
    """
    state = load_state()

    if roadmap_task_id in state.tasks_in_progress:
        return {"success": True, "already_in_progress": True}

    # Move from scheduled to in_progress
    if roadmap_task_id in state.tasks_scheduled:
        state.tasks_scheduled.remove(roadmap_task_id)

    if roadmap_task_id not in state.tasks_in_progress:
        state.tasks_in_progress.append(roadmap_task_id)

    save_state(state)

    return {
        "success": True,
        "task_id": roadmap_task_id,
    }


# =============================================================================
# Phase Promotion & XML Generation
# =============================================================================


@dataclass
class FuturePhase:
    """A future phase parsed from ROADMAP.md descriptions."""

    id: str
    name: str
    owner: str
    timeline: str
    capabilities: list[str]


def parse_future_phases(content: str) -> list[FuturePhase]:
    """Parse future phases from ROADMAP.md that don't have XML tasks yet.

    Looks for the pattern:
    ### P16: Self-Organization (Apr 2026)
    **Owner:** forge-ceo

    Capabilities:
    - `/company-reorg` — Restructure departments
    - ...
    """
    phases = []

    # Find the Future Phases section
    future_match = re.search(r"## Future Phases.*?(?=\n## |$)", content, re.DOTALL)
    if not future_match:
        return []

    future_section = future_match.group(0)

    # Pattern to extract phase info
    phase_pattern = re.compile(
        r"### (P\d+): ([^\n(]+)\s*\(([^)]+)\)\s*\n"
        r"\*\*Owner:\*\* ([^\n]+)\s*\n"
        r"\s*\nCapabilities:\s*\n((?:- [^\n]+\n)+)",
        re.MULTILINE,
    )

    for match in phase_pattern.finditer(future_section):
        phase_id = match.group(1)
        name = match.group(2).strip()
        timeline = match.group(3).strip()
        owner = match.group(4).strip()
        capabilities_raw = match.group(5)

        capabilities = [
            line.strip("- \n")
            for line in capabilities_raw.split("\n")
            if line.strip().startswith("-")
        ]

        phases.append(
            FuturePhase(
                id=phase_id,
                name=name,
                owner=owner,
                timeline=timeline,
                capabilities=capabilities,
            )
        )

    return phases


def generate_xml_tasks_for_phase(phase: FuturePhase) -> str:
    """Generate XML task blocks from a phase's capabilities.

    Converts capabilities like:
    - `/company-reorg` — Restructure departments

    Into XML tasks like:
    <task id="16.1" status="pending" depends="">
      <name>Implement /company-reorg command</name>
      ...
    </task>
    """
    tasks_xml = []
    wave1_tasks = []
    wave2_tasks = []

    # Parse capabilities into tasks
    for i, cap in enumerate(phase.capabilities, 1):
        task_id = f"{phase.id[1:]}.{i}"  # P16 -> 16.1, 16.2, etc.

        # Extract command name if present (e.g., `/company-reorg`)
        cmd_match = re.search(r"`(/[^`]+)`", cap)
        if cmd_match:
            cmd_name = cmd_match.group(1)
            task_name = f"Implement {cmd_name} command"
            file_path = f".claude/commands/{cmd_name[1:]}.md"
            action = "CREATE"
        else:
            # Generic capability
            task_name = f"Implement: {cap.split('—')[0].strip() if '—' in cap else cap}"
            file_path = f".claude/hooks/company/{phase.id.lower()}_features.py"
            action = "CREATE"

        # Extract description after — if present
        if "—" in cap:
            description = cap.split("—", 1)[1].strip()
        else:
            description = cap

        # First half of capabilities are wave 1, rest are wave 2
        if i <= len(phase.capabilities) // 2 + 1:
            wave1_tasks.append((task_id, task_name, file_path, action, description, []))
        else:
            # Wave 2 tasks depend on first wave 1 task
            first_task_id = f"{phase.id[1:]}.1"
            wave2_tasks.append(
                (task_id, task_name, file_path, action, description, [first_task_id])
            )

    # Generate XML for wave 1
    if wave1_tasks:
        tasks_xml.append("### Wave 1: Foundation\n\n```xml")
        for task_id, name, file_path, action, desc, deps in wave1_tasks:
            deps_str = ",".join(deps) if deps else ""
            tasks_xml.append(
                f"""<task id="{task_id}" status="pending" depends="{deps_str}">
  <name>{name}</name>
  <file>{file_path}</file>
  <action>{action}</action>
  <description>
    {desc}
  </description>
  <acceptance>
    1. Feature implemented and functional
    2. Tests pass
    3. Documentation updated
  </acceptance>
</task>"""
            )
        tasks_xml.append("```\n")

    # Generate XML for wave 2
    if wave2_tasks:
        tasks_xml.append("### Wave 2: Integration (Depends on Wave 1)\n\n```xml")
        for task_id, name, file_path, action, desc, deps in wave2_tasks:
            deps_str = ",".join(deps) if deps else ""
            tasks_xml.append(
                f"""<task id="{task_id}" status="pending" depends="{deps_str}">
  <name>{name}</name>
  <file>{file_path}</file>
  <action>{action}</action>
  <description>
    {desc}
  </description>
  <acceptance>
    1. Feature implemented and functional
    2. Tests pass
    3. Documentation updated
  </acceptance>
</task>"""
            )
        tasks_xml.append("```\n")

    # Add tests task
    test_task_id = f"{phase.id[1:]}.{len(phase.capabilities) + 1}"
    last_wave2_id = wave2_tasks[-1][0] if wave2_tasks else wave1_tasks[-1][0]
    tasks_xml.append("### Wave 3: Testing\n\n```xml")
    tasks_xml.append(
        f"""<task id="{test_task_id}" status="pending" depends="{last_wave2_id}">
  <name>Add tests for {phase.name}</name>
  <file>tests/test_{phase.id.lower()}.py</file>
  <action>CREATE</action>
  <description>
    Comprehensive test suite for {phase.name} features.
    Coverage target: 80%+
  </description>
  <acceptance>
    1. All features have tests
    2. Coverage >= 80%
    3. All tests pass
  </acceptance>
</task>"""
    )
    tasks_xml.append("```")

    return "\n".join(tasks_xml)


def promote_phase_to_next(phase_id: str) -> dict:
    """Promote a future phase to 'Next' status with auto-generated XML tasks.

    This:
    1. Finds the phase in Future Phases section
    2. Generates XML tasks from its capabilities
    3. Creates a new 'Next: PXX' section with the tasks
    4. Removes the phase from Future Phases

    Returns dict with success status and details.
    """
    roadmap_path = Path.cwd() / ".planning" / "ROADMAP.md"
    if not roadmap_path.exists():
        return {"success": False, "error": "ROADMAP.md not found"}

    content = roadmap_path.read_text(encoding="utf-8")

    # Parse future phases
    future_phases = parse_future_phases(content)
    target_phase = None
    for phase in future_phases:
        if phase.id.upper() == phase_id.upper():
            target_phase = phase
            break

    if not target_phase:
        return {
            "success": False,
            "error": f"Phase {phase_id} not found in Future Phases",
        }

    # Generate XML tasks
    xml_tasks = generate_xml_tasks_for_phase(target_phase)

    # Create the new "Next" section
    next_section = f"""## Next: {target_phase.id} {target_phase.name}

**Goal:** {target_phase.name} - {target_phase.capabilities[0].split("—")[1].strip() if "—" in target_phase.capabilities[0] else target_phase.capabilities[0]}

**Status:** PENDING

**Owner:** {target_phase.owner}

**Timeline:** {target_phase.timeline}

---

{xml_tasks}

---

"""

    # Find where to insert (after Current section, before Future Phases)
    future_phases_match = re.search(r"## Future Phases", content)
    if future_phases_match:
        insert_pos = future_phases_match.start()
        new_content = content[:insert_pos] + next_section + content[insert_pos:]
    else:
        # Append before Previous Phases if no Future Phases
        prev_match = re.search(r"## Previous Phases", content)
        if prev_match:
            insert_pos = prev_match.start()
            new_content = content[:insert_pos] + next_section + content[insert_pos:]
        else:
            new_content = content + "\n\n" + next_section

    # Remove the phase from Future Phases section
    phase_pattern = re.compile(
        rf"### {re.escape(target_phase.id)}: [^\n]+\n"
        rf"\*\*Owner:\*\* [^\n]+\n"
        rf"\s*\nCapabilities:\s*\n(?:- [^\n]+\n)+",
        re.MULTILINE,
    )
    new_content = phase_pattern.sub("", new_content)

    # Write back
    roadmap_path.write_text(new_content, encoding="utf-8")

    return {
        "success": True,
        "phase_id": target_phase.id,
        "phase_name": target_phase.name,
        "tasks_generated": len(target_phase.capabilities) + 1,  # +1 for tests task
        "message": f"Promoted {target_phase.id} to Next with {len(target_phase.capabilities) + 1} generated tasks",
    }


def check_and_promote_next_phase() -> dict | None:
    """Check if current phase is complete and promote next phase if needed.

    Called by daemon to automatically advance the roadmap.

    Returns promotion result if a phase was promoted, None otherwise.
    """
    roadmap_path = Path.cwd() / ".planning" / "ROADMAP.md"
    if not roadmap_path.exists():
        return None

    content = roadmap_path.read_text(encoding="utf-8")

    # Check if there's already a "Next:" section with pending tasks
    next_match = re.search(r"## Next: (P\d+)", content)
    if next_match:
        # Check if it has pending tasks. External intake files never get
        # status write-back, so a completed task can stay "pending" in-file
        # forever — exclude state-completed ids or promotion stalls.
        state = load_state()
        tasks = scan_all_roadmaps()
        pending = [
            t
            for t in tasks
            if t.status == "pending" and t.id not in state.tasks_completed
        ]
        if pending:
            return None  # Still have work to do

    # Check if Current phase is COMPLETE
    current_match = re.search(
        r"## Current: (P\d+)[^\n]*\n.*?\*\*Status:\*\*\s*(COMPLETE|PENDING|IN PROGRESS)",
        content,
        re.DOTALL,
    )
    if current_match:
        current_status = current_match.group(2)
        if current_status != "COMPLETE":
            return None  # Current phase not done

    # Get next phase from Future Phases
    future_phases = parse_future_phases(content)
    if not future_phases:
        return None  # No more phases

    # Promote first future phase
    next_phase = future_phases[0]
    return promote_phase_to_next(next_phase.id)


# =============================================================================
# Validation
# =============================================================================


def validate_roadmap(roadmap_files: list[Path] | None = None) -> dict:
    """
    Parse ROADMAP.md task tags and report structural problems.

    Checks:
    - Duplicate task IDs
    - Tasks missing a title
    - Invalid status values
    - Invalid complexity values
    - Dependencies referencing non-existent task IDs

    Args:
        roadmap_files: Explicit list of files to check; uses find_roadmap_files()
                       when None (the normal CLI path).

    Returns:
        dict with keys: valid, errors, warnings, task_count, files_checked
    """
    valid_statuses = {"pending", "in_progress", "completed", "blocked"}
    valid_complexities = {"trivial", "standard", "complex", "epic"}

    if roadmap_files is None:
        roadmap_files = find_roadmap_files()

    if not roadmap_files:
        return {
            "valid": False,
            "errors": ["No ROADMAP.md files found"],
            "warnings": [],
            "task_count": 0,
            "files_checked": [],
        }

    all_tasks: list[RoadmapTask] = []
    for f in roadmap_files:
        all_tasks.extend(parse_roadmap_file(f))

    errors: list[str] = []
    warnings: list[str] = []

    # Duplicate IDs
    seen_ids: dict[str, str] = {}
    for task in all_tasks:
        if task.id in seen_ids:
            errors.append(f"Duplicate task ID '{task.id}'")
        else:
            seen_ids[task.id] = task.file_path

    all_ids = set(seen_ids.keys())

    # Missing title
    for task in all_tasks:
        if not task.title:
            warnings.append(f"Task '{task.id}' has no title")

    # Invalid status
    for task in all_tasks:
        if task.status not in valid_statuses:
            errors.append(
                f"Task '{task.id}': invalid status '{task.status}'"
                f" (valid: {', '.join(sorted(valid_statuses))})"
            )

    # Invalid complexity
    for task in all_tasks:
        if task.complexity not in valid_complexities:
            warnings.append(
                f"Task '{task.id}': invalid complexity '{task.complexity}'"
                f" (valid: {', '.join(sorted(valid_complexities))})"
            )

    # Dependencies referencing non-existent IDs
    for task in all_tasks:
        for dep_id in task.depends_on:
            if dep_id not in all_ids:
                errors.append(f"Task '{task.id}': dependency '{dep_id}' not found")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "task_count": len(all_tasks),
        "files_checked": [str(f) for f in roadmap_files],
    }


# =============================================================================
# CLI Interface
# =============================================================================


def print_help():
    """Print usage help."""
    help_text = """
Roadmap Scheduler — Parse ROADMAP.md and schedule tasks to work queue

Commands:
    validate        Parse task tags and report structural problems
    scan            Scan ROADMAP.md files and schedule eligible tasks
    status          Show current scheduling status
    schedule        Schedule a specific task by ID
    complete        Mark a task as completed
    advance-wave    Manually advance to the next wave
    promote         Promote a future phase to Next with auto-generated XML tasks
    future          List future phases available for promotion

Options:
    --task-id ID    Task ID for schedule/complete commands
    --wave N        Wave number for advance-wave command
    --phase-id ID   Phase ID for promote command (e.g., P16)

Examples:
    # Validate roadmap structure
    python roadmap_scheduler.py validate

    # Scan and auto-schedule
    python roadmap_scheduler.py scan

    # Check status
    python roadmap_scheduler.py status

    # Schedule specific task
    python roadmap_scheduler.py schedule --task-id "P14-1.1"

    # Mark task completed
    python roadmap_scheduler.py complete --task-id "P14-1.1"

    # List future phases
    python roadmap_scheduler.py future

    # Promote P16 to Next (generates XML tasks automatically)
    python roadmap_scheduler.py promote --phase-id P16
"""
    print(help_text)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    if command == "validate":
        result = validate_roadmap()
        print(json.dumps(result, indent=2))
        if not result["valid"]:
            sys.exit(1)

    elif command == "scan":
        result = run_scheduling_scan()
        print(json.dumps(result, indent=2))

    elif command == "status":
        state = load_state()
        config = load_config()
        tasks = scan_all_roadmaps()

        status = {
            "config": config,
            "state": state.to_dict(),
            "tasks_found": len(tasks),
            "tasks_by_wave": {},
            "tasks_by_status": {
                "pending": 0,
                "in_progress": 0,
                "completed": 0,
            },
        }

        for task in tasks:
            wave_key = f"wave_{task.wave}"
            if wave_key not in status["tasks_by_wave"]:
                status["tasks_by_wave"][wave_key] = 0
            status["tasks_by_wave"][wave_key] += 1

            if task.id in state.tasks_completed:
                status["tasks_by_status"]["completed"] += 1
            elif task.id in state.tasks_in_progress:
                status["tasks_by_status"]["in_progress"] += 1
            else:
                status["tasks_by_status"]["pending"] += 1

        print(json.dumps(status, indent=2))

    elif command == "schedule":
        task_id = None
        for i, arg in enumerate(sys.argv):
            if arg == "--task-id" and i + 1 < len(sys.argv):
                task_id = sys.argv[i + 1]
                break

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        tasks = scan_all_roadmaps()
        target_task = next((t for t in tasks if t.id == task_id), None)

        if not target_task:
            print(f"Error: Task {task_id} not found in roadmaps")
            sys.exit(1)

        state = load_state()
        result = schedule_task(target_task, state)
        save_state(state)
        print(json.dumps(result, indent=2))

    elif command == "complete":
        task_id = None
        for i, arg in enumerate(sys.argv):
            if arg == "--task-id" and i + 1 < len(sys.argv):
                task_id = sys.argv[i + 1]
                break

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        result = mark_task_completed(task_id)
        print(json.dumps(result, indent=2))

    elif command == "advance-wave":
        state = load_state()
        tasks = scan_all_roadmaps()

        old_wave = state.current_wave
        advanced = advance_wave(state, tasks)

        print(
            json.dumps(
                {
                    "success": True,
                    "advanced": advanced,
                    "previous_wave": old_wave,
                    "current_wave": state.current_wave,
                },
                indent=2,
            )
        )

    elif command == "future":
        roadmap_path = Path.cwd() / ".planning" / "ROADMAP.md"
        if not roadmap_path.exists():
            print("Error: ROADMAP.md not found")
            sys.exit(1)

        content = roadmap_path.read_text(encoding="utf-8")
        phases = parse_future_phases(content)

        if not phases:
            print("No future phases found in ROADMAP.md")
            sys.exit(0)

        print("Future Phases available for promotion:\n")
        for phase in phases:
            print(f"  {phase.id}: {phase.name}")
            print(f"      Owner: {phase.owner}")
            print(f"      Timeline: {phase.timeline}")
            print(f"      Capabilities: {len(phase.capabilities)}")
            print()

        print(f"Use: python roadmap_scheduler.py promote --phase-id {phases[0].id}")

    elif command == "promote":
        phase_id = None
        for i, arg in enumerate(sys.argv):
            if arg == "--phase-id" and i + 1 < len(sys.argv):
                phase_id = sys.argv[i + 1]
                break

        if not phase_id:
            print("Error: --phase-id required (e.g., --phase-id P16)")
            sys.exit(1)

        result = promote_phase_to_next(phase_id)
        print(json.dumps(result, indent=2))

        if result.get("success"):
            print(f"\n✓ {result['message']}")
            print("  Run 'python roadmap_scheduler.py scan' to schedule the new tasks")

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
