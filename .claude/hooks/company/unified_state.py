#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
P28 Unified State Management — Single Source of Truth for Session State.

This module unifies the two state sources in Gatesmith:
1. `.planning/STATE.md` — GSD session state (markdown format, human-readable)
2. `.company/session_state.json` — Daemon metrics (JSON format, machine-readable)

By providing a unified interface, we ensure:
- Both sources stay in sync
- Atomic load/save operations prevent data loss
- Migration from legacy formats is handled gracefully
- Consistent state access for all components

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                    UNIFIED STATE MANAGER                     │
    ├─────────────────────────────────────────────────────────────┤
    │                                                              │
    │   UnifiedState ←──→ UnifiedStateManager                     │
    │        │                    │                                │
    │        ▼                    ▼                                │
    │   ┌──────────┐        ┌──────────────┐                      │
    │   │ STATE.md │        │ session_     │                      │
    │   │ (GSD)    │        │ state.json   │                      │
    │   └──────────┘        └──────────────┘                      │
    │                                                              │
    └─────────────────────────────────────────────────────────────┘

Usage:
    # Load current state
    python unified_state.py load

    # Save state with updates
    python unified_state.py save --status "P28 in progress"

    # Migrate legacy state
    python unified_state.py migrate

    # Sync both sources
    python unified_state.py sync

    # Show current state
    python unified_state.py show
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("unified_state")


# =============================================================================
# Constants
# =============================================================================

STATE_VERSION = "1.0"
STATE_MD_FILE = "STATE.md"
SESSION_STATE_FILE = "state/session_state.json"


# =============================================================================
# Path Utilities
# =============================================================================


def _get_module_dir() -> Path:
    """Get the directory containing this module."""
    return Path(__file__).parent


def get_company_dir() -> Path:
    """Get the company directory path."""
    module_dir = _get_module_dir()
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        import company_resolver

        return company_resolver.get_company_dir()
    except (ImportError, Exception):
        # Fallback to default
        return Path.cwd() / ".company"


def get_planning_dir() -> Path:
    """Get the planning directory path."""
    company_dir = get_company_dir()
    # Planning dir is at same level as .company
    return company_dir.parent / ".planning"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class LoopMetrics:
    """Daemon loop execution metrics."""

    tasks_claimed: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_escalated: int = 0
    consecutive_idle_polls: int = 0
    errors: list[str] = field(default_factory=list)
    last_task_id: str | None = None
    current_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "tasks_claimed": self.tasks_claimed,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_escalated": self.tasks_escalated,
            "consecutive_idle_polls": self.consecutive_idle_polls,
            "errors": self.errors,
            "last_task_id": self.last_task_id,
            "current_task_id": self.current_task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopMetrics":
        """Create from dictionary."""
        return cls(
            tasks_claimed=data.get("tasks_claimed", 0),
            tasks_completed=data.get("tasks_completed", 0),
            tasks_failed=data.get("tasks_failed", 0),
            tasks_escalated=data.get("tasks_escalated", 0),
            consecutive_idle_polls=data.get("consecutive_idle_polls", 0),
            errors=data.get("errors", []),
            last_task_id=data.get("last_task_id"),
            current_task_id=data.get("current_task_id"),
        )


@dataclass
class PhaseProgress:
    """Progress tracking for a phase."""

    name: str
    tasks_total: int
    tasks_done: int
    status: str  # "pending", "in_progress", "complete"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "tasks_total": self.tasks_total,
            "tasks_done": self.tasks_done,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PhaseProgress":
        """Create from dictionary."""
        return cls(
            name=data.get("name", ""),
            tasks_total=data.get("tasks_total", 0),
            tasks_done=data.get("tasks_done", 0),
            status=data.get("status", "pending"),
        )


@dataclass
class CompanyPhase:
    """Company growth phase information."""

    current_phase: str  # "bootstrap", "growth", "scale", "mature"
    since: str  # ISO timestamp
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    last_assessment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "current_phase": self.current_phase,
            "since": self.since,
            "metrics_snapshot": self.metrics_snapshot,
            "last_assessment": self.last_assessment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyPhase":
        """Create from dictionary."""
        return cls(
            current_phase=data.get("current_phase", "growth"),
            since=data.get("since", ""),
            metrics_snapshot=data.get("metrics_snapshot", {}),
            last_assessment=data.get("last_assessment"),
        )


@dataclass
class KeyDecision:
    """A key decision made during planning."""

    decision: str
    choice: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "decision": self.decision,
            "choice": self.choice,
        }


@dataclass
class RecentChange:
    """A recent change made to the codebase."""

    file: str
    change: str
    task_id: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "file": self.file,
            "change": self.change,
            "task_id": self.task_id,
        }


@dataclass
class UnifiedState:
    """Unified state combining GSD session state and daemon metrics.

    This dataclass represents the single source of truth for all session state
    in Gatesmith. It combines data from both STATE.md (human-readable) and
    session_state.json (machine-readable).
    """

    # Version for migration support
    version: str = STATE_VERSION

    # Session info (from STATE.md)
    session_date: str = ""
    current_phase: str = ""
    branch: str = "main"
    status: str = ""

    # Task tracking (from STATE.md)
    last_completed_task: str = ""
    next_task: str = ""

    # Company phase info (from STATE.md)
    company_phase: CompanyPhase | None = None

    # Progress metrics (from STATE.md)
    progress: list[PhaseProgress] = field(default_factory=list)

    # Blockers (from STATE.md)
    blockers: list[str] = field(default_factory=list)

    # Context snapshot (from STATE.md)
    context_snapshot: str = ""

    # Key decisions (from STATE.md)
    key_decisions: list[KeyDecision] = field(default_factory=list)

    # Recent changes (from STATE.md)
    recent_changes: list[RecentChange] = field(default_factory=list)

    # Daemon metrics (from session_state.json)
    start_time: str = ""
    start_health: int = 0
    updated_at: str = ""
    loop_metrics: LoopMetrics = field(default_factory=LoopMetrics)
    # Cumulative uptime across all daemon sessions (seconds)
    total_uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "session_date": self.session_date,
            "current_phase": self.current_phase,
            "branch": self.branch,
            "status": self.status,
            "last_completed_task": self.last_completed_task,
            "next_task": self.next_task,
            "company_phase": self.company_phase.to_dict()
            if self.company_phase
            else None,
            "progress": [p.to_dict() for p in self.progress],
            "blockers": self.blockers,
            "context_snapshot": self.context_snapshot,
            "key_decisions": [d.to_dict() for d in self.key_decisions],
            "recent_changes": [c.to_dict() for c in self.recent_changes],
            "start_time": self.start_time,
            "start_health": self.start_health,
            "updated_at": self.updated_at,
            "loop_metrics": self.loop_metrics.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnifiedState":
        """Create from dictionary."""
        company_phase = None
        if data.get("company_phase"):
            company_phase = CompanyPhase.from_dict(data["company_phase"])

        return cls(
            version=data.get("version", STATE_VERSION),
            session_date=data.get("session_date", ""),
            current_phase=data.get("current_phase", ""),
            branch=data.get("branch", "main"),
            status=data.get("status", ""),
            last_completed_task=data.get("last_completed_task", ""),
            next_task=data.get("next_task", ""),
            company_phase=company_phase,
            progress=[PhaseProgress.from_dict(p) for p in data.get("progress", [])],
            blockers=data.get("blockers", []),
            context_snapshot=data.get("context_snapshot", ""),
            key_decisions=[KeyDecision(**d) for d in data.get("key_decisions", [])],
            recent_changes=[RecentChange(**c) for c in data.get("recent_changes", [])],
            start_time=data.get("start_time", ""),
            start_health=data.get("start_health", 0),
            updated_at=data.get("updated_at", ""),
            loop_metrics=LoopMetrics.from_dict(data.get("loop_metrics", {})),
        )

    @classmethod
    def empty(cls) -> "UnifiedState":
        """Create an empty state with defaults."""
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            session_date=now[:10],
            updated_at=now,
        )


# =============================================================================
# Parsing Functions
# =============================================================================


def parse_state_md(content: str) -> dict[str, Any]:
    """Parse STATE.md markdown content into a dictionary.

    This parser extracts structured data from the markdown format used by
    GSD state tracking.

    Args:
        content: The markdown content of STATE.md

    Returns:
        Dictionary with parsed state fields
    """
    result: dict[str, Any] = {
        "session_date": "",
        "current_phase": "",
        "branch": "main",
        "status": "",
        "last_completed_task": "",
        "next_task": "",
        "company_phase": None,
        "progress": [],
        "blockers": [],
        "context_snapshot": "",
        "key_decisions": [],
        "recent_changes": [],
    }

    # Parse Last Session section
    session_match = re.search(r"## Last Session\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if session_match:
        section = session_match.group(1)

        # Extract fields
        date_match = re.search(r"\*\*Date:\*\* (\S+)", section)
        if date_match:
            result["session_date"] = date_match.group(1)

        phase_match = re.search(r"\*\*Phase:\*\* (\S+)", section)
        if phase_match:
            result["current_phase"] = phase_match.group(1)

        last_match = re.search(r"\*\*Last Completed Task:\*\* (.+)", section)
        if last_match:
            result["last_completed_task"] = last_match.group(1).strip()

        next_match = re.search(r"\*\*Next Task:\*\* (.+)", section)
        if next_match:
            result["next_task"] = next_match.group(1).strip()

        branch_match = re.search(r"\*\*Branch:\*\* (\S+)", section)
        if branch_match:
            result["branch"] = branch_match.group(1)

        status_match = re.search(r"\*\*Status:\*\* (.+)", section)
        if status_match:
            result["status"] = status_match.group(1).strip()

    # Parse Context Snapshot
    context_match = re.search(
        r"## Context Snapshot\n<!-- [^>]* -->\n(.*?)(?=\n## |\Z)", content, re.DOTALL
    )
    if context_match:
        result["context_snapshot"] = context_match.group(1).strip()

    # Parse Company Phase section
    company_match = re.search(
        r"## Company Phase\n(.*?)(?=\n## |\Z)", content, re.DOTALL
    )
    if company_match:
        section = company_match.group(1)

        phase_match = re.search(r"\*\*Current Phase:\*\* (\w+)", section)
        since_match = re.search(r"\*\*Since:\*\* (.+)", section)
        metrics_match = re.search(r"\*\*Metrics Snapshot:\*\* \{(.+)\}", section)
        last_assess_match = re.search(r"\*\*Last Assessment:\*\* (.+)", section)

        if phase_match:
            company_phase: dict[str, Any] = {
                "current_phase": phase_match.group(1),
                "since": since_match.group(1).strip() if since_match else "",
                "metrics_snapshot": {},
                "last_assessment": (
                    last_assess_match.group(1).strip() if last_assess_match else None
                ),
            }

            # Parse metrics snapshot
            if metrics_match:
                metrics_str = metrics_match.group(1)
                for item in metrics_str.split(","):
                    if ":" in item:
                        key, value = item.split(":", 1)
                        key = key.strip()
                        value = value.strip()
                        # Try to convert to number
                        if value.endswith("%"):
                            try:
                                company_phase["metrics_snapshot"][key] = float(
                                    value[:-1]
                                )
                            except ValueError:
                                company_phase["metrics_snapshot"][key] = value
                        else:
                            try:
                                company_phase["metrics_snapshot"][key] = float(value)
                            except ValueError:
                                company_phase["metrics_snapshot"][key] = value

            result["company_phase"] = company_phase

    # Parse Progress table
    progress_match = re.search(
        r"## Progress\n\|.*?\|.*?\|.*?\|.*?\|\n\|[-\s|]+\|\n(.*?)(?=\n## |\n### |\Z)",
        content,
        re.DOTALL,
    )
    if progress_match:
        for line in progress_match.group(1).strip().split("\n"):
            if line.startswith("|"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 4:
                    try:
                        result["progress"].append(
                            {
                                "name": parts[0],
                                "tasks_total": int(parts[1])
                                if parts[1].isdigit()
                                else 0,
                                "tasks_done": int(parts[2])
                                if parts[2].isdigit()
                                else 0,
                                "status": parts[3],
                            }
                        )
                    except (ValueError, IndexError):
                        pass

    # Parse Blockers
    blockers_match = re.search(
        r"## Blockers\n<!-- [^>]* -->\n(.*?)(?=\n## |\Z)", content, re.DOTALL
    )
    if blockers_match:
        blockers_text = blockers_match.group(1).strip()
        if blockers_text.lower() not in (
            "none",
            "n/a",
            "-",
            "none - p10 plan approved, ready for /build",
        ):
            result["blockers"] = [
                b.strip().lstrip("- ")
                for b in blockers_text.split("\n")
                if b.strip() and not b.startswith("<!--")
            ]

    # Parse Key Decisions
    # Look for any "Key Decisions" section
    decisions_match = re.search(
        r"## Key Decisions.*?\n\|.*?\|.*?\|\n\|[-\s|]+\|\n(.*?)(?=\n## |\Z)",
        content,
        re.DOTALL,
    )
    if decisions_match:
        for line in decisions_match.group(1).strip().split("\n"):
            if line.startswith("|"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 2:
                    result["key_decisions"].append(
                        {
                            "decision": parts[0],
                            "choice": parts[1],
                        }
                    )

    # Parse Recent Changes
    changes_match = re.search(
        r"## Recent Changes\n<!-- [^>]* -->\n\|.*?\|.*?\|.*?\|\n\|[-\s|]+\|\n(.*?)(?=\n## |\Z)",
        content,
        re.DOTALL,
    )
    if changes_match:
        for line in changes_match.group(1).strip().split("\n"):
            if line.startswith("|"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 3:
                    result["recent_changes"].append(
                        {
                            "file": parts[0],
                            "change": parts[1],
                            "task_id": parts[2],
                        }
                    )

    return result


def render_state_md(state: UnifiedState) -> str:
    """Render UnifiedState back to STATE.md markdown format.

    This generates the human-readable markdown representation of the state,
    preserving the format expected by GSD tools.

    Args:
        state: The unified state to render

    Returns:
        Markdown content for STATE.md
    """
    lines = [
        "# Session State",
        "",
        "<!-- Auto-updated by state_tracker.py -->",
        "",
        "## Last Session",
        f"- **Date:** {state.session_date}",
        f"- **Phase:** {state.current_phase}",
        f"- **Last Completed Task:** {state.last_completed_task}",
        f"- **Next Task:** {state.next_task}",
        f"- **Branch:** {state.branch}",
        f"- **Status:** {state.status}",
        "",
        "## Context Snapshot",
        "<!-- Key context the next session needs to know -->",
        state.context_snapshot,
        "",
    ]

    # Company Phase
    if state.company_phase:
        cp = state.company_phase
        metrics_parts = []
        for k, v in cp.metrics_snapshot.items():
            # Ratio values are stored as percentages (e.g., 8 means 8%)
            if isinstance(v, float) and ("ratio" in k or "rate" in k):
                metrics_parts.append(f"{k}: {v:.0f}%")
            elif isinstance(v, float) and v == int(v):
                # Render whole numbers without decimal
                metrics_parts.append(f"{k}: {int(v)}")
            else:
                metrics_parts.append(f"{k}: {v}")
        metrics_str = ", ".join(metrics_parts) if metrics_parts else ""

        lines.extend(
            [
                "## Company Phase",
                f"- **Current Phase:** {cp.current_phase}",
                f"- **Since:** {cp.since}",
                f"- **Metrics Snapshot:** {{{metrics_str}}}",
                f"- **Last Assessment:** {cp.last_assessment or 'N/A'}",
                "",
            ]
        )

    # Progress
    if state.progress:
        lines.extend(
            [
                "## Progress",
                "| Phase | Tasks Total | Tasks Done | Status |",
                "|-------|------------|------------|--------|",
            ]
        )
        for p in state.progress:
            lines.append(
                f"| {p.name} | {p.tasks_total} | {p.tasks_done} | {p.status} |"
            )
        lines.append("")

    # Blockers
    lines.extend(
        [
            "## Blockers",
            "<!-- Anything preventing progress -->",
        ]
    )
    if state.blockers:
        for b in state.blockers:
            lines.append(f"- {b}")
    else:
        lines.append("None")
    lines.append("")

    # Key Decisions
    if state.key_decisions:
        lines.extend(
            [
                "## Key Decisions",
                "| Decision | Choice |",
                "|----------|--------|",
            ]
        )
        for d in state.key_decisions:
            lines.append(f"| {d.decision} | {d.choice} |")
        lines.append("")

    # Recent Changes
    if state.recent_changes:
        lines.extend(
            [
                "## Recent Changes",
                "<!-- Last 5 significant changes -->",
                "| File | Change | Task ID |",
                "|------|--------|---------|",
            ]
        )
        for c in state.recent_changes[-5:]:  # Last 5 only
            lines.append(f"| {c.file} | {c.change} | {c.task_id} |")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Atomic File Operations
# =============================================================================


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON data atomically using tempfile + os.replace.

    This prevents data loss during writes by using the rename pattern
    which is atomic on most filesystems.

    Args:
        path: Target file path
        data: Data to write as JSON
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=path.stem + "_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text content atomically using tempfile + os.replace.

    Args:
        path: Target file path
        content: Text content to write
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=path.stem + "_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# Unified State Manager
# =============================================================================


class UnifiedStateManager:
    """Manages unified state across both data sources.

    This class provides a single interface for loading, saving, and updating
    state, ensuring both STATE.md and session_state.json stay in sync.

    Usage:
        manager = UnifiedStateManager()

        # Load current state
        state = manager.load()

        # Update specific fields
        manager.update(status="Working on P28")

        # Save changes
        manager.save(state)

        # Sync sources
        manager.sync()
    """

    def __init__(
        self,
        planning_dir: Path | None = None,
        company_dir: Path | None = None,
    ):
        """Initialize the state manager.

        Args:
            planning_dir: Override for .planning directory path
            company_dir: Override for .company directory path
        """
        self.planning_dir = planning_dir or get_planning_dir()
        self.company_dir = company_dir or get_company_dir()

    @property
    def state_md_path(self) -> Path:
        """Path to STATE.md."""
        return self.planning_dir / STATE_MD_FILE

    @property
    def session_state_path(self) -> Path:
        """Path to session_state.json."""
        return self.company_dir / SESSION_STATE_FILE

    def load(self) -> UnifiedState:
        """Load state from both sources and merge.

        Loads data from both STATE.md and session_state.json, then merges
        them into a single UnifiedState object. If either source is missing,
        only the available data is used.

        Returns:
            Merged UnifiedState object
        """
        state = UnifiedState.empty()

        # Load from STATE.md (GSD session state)
        if self.state_md_path.exists():
            try:
                content = self.state_md_path.read_text(encoding="utf-8")
                parsed = parse_state_md(content)

                state.session_date = parsed.get("session_date", "")
                state.current_phase = parsed.get("current_phase", "")
                state.branch = parsed.get("branch", "main")
                state.status = parsed.get("status", "")
                state.last_completed_task = parsed.get("last_completed_task", "")
                state.next_task = parsed.get("next_task", "")
                state.context_snapshot = parsed.get("context_snapshot", "")
                state.blockers = parsed.get("blockers", [])

                # Parse company phase
                if parsed.get("company_phase"):
                    state.company_phase = CompanyPhase.from_dict(
                        parsed["company_phase"]
                    )

                # Parse progress
                state.progress = [
                    PhaseProgress.from_dict(p) for p in parsed.get("progress", [])
                ]

                # Parse key decisions
                state.key_decisions = [
                    KeyDecision(**d) for d in parsed.get("key_decisions", [])
                ]

                # Parse recent changes
                state.recent_changes = [
                    RecentChange(**c) for c in parsed.get("recent_changes", [])
                ]

                logger.debug(f"Loaded STATE.md: phase={state.current_phase}")
            except Exception as e:
                logger.warning(f"Failed to parse STATE.md: {e}")

        # Load from session_state.json (daemon metrics)
        if self.session_state_path.exists():
            try:
                with open(self.session_state_path, encoding="utf-8") as f:
                    data = json.load(f)

                state.start_time = data.get("start_time", "")
                state.start_health = data.get("start_health", 0)
                state.updated_at = data.get("updated_at", "")
                state.total_uptime_seconds = float(
                    data.get("total_uptime_seconds", 0.0)
                )

                if data.get("loop_metrics"):
                    state.loop_metrics = LoopMetrics.from_dict(data["loop_metrics"])

                logger.debug(
                    f"Loaded session_state.json: "
                    f"completed={state.loop_metrics.tasks_completed}"
                )
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON in session_state.json: {e}")
                # Retry once after brief delay (handles transient read-during-write)
                import time

                time.sleep(0.05)
                try:
                    with open(self.session_state_path, encoding="utf-8") as f:
                        data = json.load(f)
                    state.start_time = data.get("start_time", "")
                    state.start_health = data.get("start_health", 0)
                    state.updated_at = data.get("updated_at", "")
                    state.total_uptime_seconds = float(
                        data.get("total_uptime_seconds", 0.0)
                    )
                    if data.get("loop_metrics"):
                        state.loop_metrics = LoopMetrics.from_dict(data["loop_metrics"])
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Failed to load session_state.json: {e}")

        return state

    def save(self, state: UnifiedState) -> None:
        """Save state to both sources atomically.

        Writes to both STATE.md and session_state.json using atomic operations
        to prevent data loss.

        Args:
            state: The state to save
        """
        # Update timestamp
        state.updated_at = datetime.now(timezone.utc).isoformat()

        # Save to STATE.md
        try:
            content = render_state_md(state)
            _atomic_write_text(self.state_md_path, content)
            logger.debug("Saved STATE.md")
        except Exception as e:
            logger.error(f"Failed to save STATE.md: {e}")
            raise

        # Save to session_state.json
        try:
            data = {
                "start_time": state.start_time,
                "start_health": state.start_health,
                "updated_at": state.updated_at,
                "loop_metrics": state.loop_metrics.to_dict(),
                "total_uptime_seconds": state.total_uptime_seconds,
            }
            _atomic_write_json(self.session_state_path, data)
            logger.debug("Saved session_state.json")
        except Exception as e:
            logger.error(f"Failed to save session_state.json: {e}")
            raise

    def update(self, **kwargs: Any) -> UnifiedState:
        """Update specific fields and save.

        Convenience method to load, update specific fields, and save in one call.

        Args:
            **kwargs: Fields to update (must be valid UnifiedState fields)

        Returns:
            Updated UnifiedState
        """
        state = self.load()

        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
            else:
                logger.warning(f"Unknown state field: {key}")

        self.save(state)
        return state

    def sync(self) -> UnifiedState:
        """Ensure both sources are consistent.

        Loads from both sources, merges them, and writes back to both.
        This ensures both files contain the same data.

        Returns:
            The synchronized state
        """
        state = self.load()
        self.save(state)
        logger.info("Synchronized state sources")
        return state

    def migrate(self) -> UnifiedState:
        """Migrate from legacy state format.

        Handles migration from older state formats to the current unified format.
        This is idempotent and safe to run multiple times.

        Returns:
            The migrated state
        """
        state = self.load()

        # Set version if missing
        if not state.version:
            state.version = STATE_VERSION

        # Ensure session_date is set
        if not state.session_date:
            state.session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Ensure updated_at is set
        if not state.updated_at:
            state.updated_at = datetime.now(timezone.utc).isoformat()

        # Create session_state.json if it doesn't exist
        if not self.session_state_path.exists():
            state.start_time = datetime.now(timezone.utc).isoformat()
            state.start_health = 100

        self.save(state)
        logger.info("Migration complete")
        return state

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the current state.

        Returns:
            Dictionary with key state information
        """
        state = self.load()
        return {
            "session_date": state.session_date,
            "current_phase": state.current_phase,
            "status": state.status,
            "last_completed_task": state.last_completed_task,
            "next_task": state.next_task,
            "blockers_count": len(state.blockers),
            "progress_phases": len(state.progress),
            "tasks_completed": state.loop_metrics.tasks_completed,
            "tasks_failed": state.loop_metrics.tasks_failed,
        }


# =============================================================================
# Module-Level Convenience Functions
# =============================================================================


def load_unified_state() -> UnifiedState:
    """Load the unified state.

    Convenience function for quick access without instantiating the manager.

    Returns:
        Current unified state
    """
    manager = UnifiedStateManager()
    return manager.load()


def save_unified_state(state: UnifiedState) -> None:
    """Save the unified state.

    Convenience function for quick access without instantiating the manager.

    Args:
        state: The state to save
    """
    manager = UnifiedStateManager()
    manager.save(state)


def update_unified_state(**kwargs: Any) -> UnifiedState:
    """Update specific fields in the unified state.

    Convenience function for quick updates.

    Args:
        **kwargs: Fields to update

    Returns:
        Updated state
    """
    manager = UnifiedStateManager()
    return manager.update(**kwargs)


def sync_state_sources() -> UnifiedState:
    """Synchronize both state sources.

    Convenience function for ensuring consistency.

    Returns:
        Synchronized state
    """
    manager = UnifiedStateManager()
    return manager.sync()


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="P28 Unified State Management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Load and display current state
  python unified_state.py load

  # Show summary
  python unified_state.py show

  # Update specific fields
  python unified_state.py save --status "Working on P28"

  # Sync both sources
  python unified_state.py sync

  # Migrate legacy format
  python unified_state.py migrate
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # load command
    subparsers.add_parser("load", help="Load and display current state")

    # show command
    subparsers.add_parser("show", help="Show state summary")

    # save command
    save_parser = subparsers.add_parser("save", help="Update and save state")
    save_parser.add_argument("--status", help="Update status field")
    save_parser.add_argument("--phase", help="Update current_phase field")
    save_parser.add_argument("--next-task", help="Update next_task field")
    save_parser.add_argument("--last-task", help="Update last_completed_task field")

    # sync command
    subparsers.add_parser("sync", help="Synchronize both state sources")

    # migrate command
    subparsers.add_parser("migrate", help="Migrate from legacy format")

    args = parser.parse_args()
    manager = UnifiedStateManager()

    if args.command == "load":
        state = manager.load()
        print(json.dumps(state.to_dict(), indent=2))

    elif args.command == "show":
        summary = manager.get_summary()
        print(json.dumps(summary, indent=2))

    elif args.command == "save":
        updates = {}
        if args.status:
            updates["status"] = args.status
        if args.phase:
            updates["current_phase"] = args.phase
        if args.next_task:
            updates["next_task"] = args.next_task
        if args.last_task:
            updates["last_completed_task"] = args.last_task

        if updates:
            state = manager.update(**updates)
            print(json.dumps(manager.get_summary(), indent=2))
        else:
            print(
                "No updates specified. Use --status, --phase, --next-task, or --last-task"
            )

    elif args.command == "sync":
        state = manager.sync()
        print(json.dumps(manager.get_summary(), indent=2))

    elif args.command == "migrate":
        state = manager.migrate()
        print(json.dumps(manager.get_summary(), indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
