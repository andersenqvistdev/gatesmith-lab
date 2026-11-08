#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
BMAD/GSD Central Orchestrator — The Brain of Gatesmith Autonomous Operations.

This module implements the unified orchestration layer that brings together:
- BMAD v6 Scale-Adaptive Intelligence (complexity-aware routing)
- GSD State Management (planning docs, session continuity, dependency waves)

Philosophy (from BMAD v6 - https://github.com/bmad-code-org/BMAD-METHOD):
    "Expert collaborators who guide you through a structured process to bring
    out your best thinking in partnership with the AI."

Instead of having the daemon make routing decisions, ALL work flows through
this orchestrator which determines:
1. What complexity level is this work?
2. What pipeline should be used?
3. What's the current state and context?
4. Who should execute this?
5. What quality gates apply?

Usage:
    # Route a task (daemon calls this)
    python orchestrator.py route --task-id <id> --title "Task title"

    # Get current execution state
    python orchestrator.py state

    # Get recommended pipeline for a description
    python orchestrator.py analyze "implement user authentication"

    # Check if we can proceed with next step
    python orchestrator.py gate --phase implement --task-id <id>

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                    BMAD/GSD ORCHESTRATOR                     │
    ├─────────────────────────────────────────────────────────────┤
    │                                                              │
    │   Input ──→ Complexity Detection ──→ Pipeline Selection     │
    │                     │                        │               │
    │                     ▼                        ▼               │
    │              ┌──────────┐            ┌──────────────┐        │
    │              │ BMAD     │            │ GSD State    │        │
    │              │ Routing  │            │ Management   │        │
    │              └──────────┘            └──────────────┘        │
    │                     │                        │               │
    │                     └────────┬───────────────┘               │
    │                              ▼                               │
    │                     ┌──────────────┐                        │
    │                     │ Execution    │                        │
    │                     │ Plan         │                        │
    │                     └──────────────┘                        │
    │                                                              │
    └─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")


# =============================================================================
# Constants & Enums (BMAD v6 Aligned)
# =============================================================================


class Complexity(Enum):
    """BMAD Scale-Adaptive Complexity Levels.

    From BMAD v6: The framework automatically adjusts planning depth based
    on project complexity—from minor bug fixes to enterprise-scale systems.
    """

    TRIVIAL = "trivial"  # Typo fix, config change, small bug
    STANDARD = "standard"  # Single feature, moderate change
    COMPLEX = "complex"  # Multi-file feature, architectural change
    EPIC = "epic"  # New product, major refactor


class PipelineStage(Enum):
    """GSD Pipeline Stages.

    Each complexity level maps to a subset of these stages.
    """

    DISCUSS = "discuss"  # Capture requirements (complex+)
    PLAN = "plan"  # Create plan with tasks (standard+)
    CHECK_PLAN = "check_plan"  # Adversarial plan review (complex+)
    IMPLEMENT = "implement"  # Execute the work (all)
    GATE = "gate"  # Human security checkpoint (complex+)
    REVIEW = "review"  # Code review (all)
    TEST = "test"  # Run tests (standard+)
    SECURITY_AUDIT = "security_audit"  # Full OWASP audit (epic)


class ExecutionMode(Enum):
    """How work should be executed."""

    DIRECT = "direct"  # Execute immediately, no planning
    PLAN_THEN_EXECUTE = "plan_execute"  # Create plan, then execute
    FULL_PIPELINE = "full_pipeline"  # Full GSD pipeline with gates


# Pipeline definitions by complexity
PIPELINES: dict[Complexity, list[PipelineStage]] = {
    Complexity.TRIVIAL: [
        PipelineStage.IMPLEMENT,
        PipelineStage.REVIEW,
    ],
    Complexity.STANDARD: [
        PipelineStage.PLAN,
        PipelineStage.IMPLEMENT,
        PipelineStage.REVIEW,
        PipelineStage.TEST,
    ],
    Complexity.COMPLEX: [
        PipelineStage.DISCUSS,
        PipelineStage.PLAN,
        PipelineStage.CHECK_PLAN,
        PipelineStage.IMPLEMENT,
        PipelineStage.GATE,
        PipelineStage.REVIEW,
        PipelineStage.TEST,
    ],
    Complexity.EPIC: [
        PipelineStage.DISCUSS,
        PipelineStage.PLAN,
        PipelineStage.CHECK_PLAN,
        PipelineStage.IMPLEMENT,
        PipelineStage.GATE,
        PipelineStage.REVIEW,
        PipelineStage.TEST,
        PipelineStage.SECURITY_AUDIT,
    ],
}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ComplexityAnalysis:
    """Result of BMAD complexity detection."""

    level: Complexity
    estimated_files: int
    is_architectural: bool
    is_security_sensitive: bool
    reasoning: str
    pipeline: list[PipelineStage]
    execution_mode: ExecutionMode

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "estimated_files": self.estimated_files,
            "is_architectural": self.is_architectural,
            "is_security_sensitive": self.is_security_sensitive,
            "reasoning": self.reasoning,
            "pipeline": [s.value for s in self.pipeline],
            "execution_mode": self.execution_mode.value,
        }


@dataclass
class GSDState:
    """GSD Session State from .planning/STATE.md."""

    current_phase: str | None
    last_task: str | None
    next_task: str | None
    status: str | None
    company_phase: str | None
    blockers: list[str]
    context_snapshot: str | None
    progress: dict[str, dict]  # phase -> {total, done, status}

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_phase": self.current_phase,
            "last_task": self.last_task,
            "next_task": self.next_task,
            "status": self.status,
            "company_phase": self.company_phase,
            "blockers": self.blockers,
            "context_snapshot": self.context_snapshot,
            "progress": self.progress,
        }

    @classmethod
    def empty(cls) -> "GSDState":
        return cls(
            current_phase=None,
            last_task=None,
            next_task=None,
            status=None,
            company_phase=None,
            blockers=[],
            context_snapshot=None,
            progress={},
        )


@dataclass
class ExecutionPlan:
    """Plan for how to execute a piece of work."""

    task_id: str
    title: str
    complexity: ComplexityAnalysis
    pipeline: list[PipelineStage]
    current_stage: PipelineStage | None
    execution_mode: ExecutionMode
    gsd_state: GSDState
    employee_hint: str | None  # Suggested employee based on capabilities
    wave: int  # For dependency-based execution
    dependencies: list[str]  # Task IDs this depends on

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "complexity": self.complexity.to_dict(),
            "pipeline": [s.value for s in self.pipeline],
            "current_stage": self.current_stage.value if self.current_stage else None,
            "execution_mode": self.execution_mode.value,
            "gsd_state": self.gsd_state.to_dict(),
            "employee_hint": self.employee_hint,
            "wave": self.wave,
            "dependencies": self.dependencies,
        }


# =============================================================================
# BMAD Complexity Detection
# =============================================================================


class BMADAnalyzer:
    """BMAD v6 Scale-Adaptive Intelligence.

    Analyzes work descriptions to determine complexity and appropriate pipeline.
    """

    # Keywords that suggest scope
    # Broad-scope signals → ~20 estimated files → complex/epic. These must be
    # STRUCTURAL scope words, not bare quantifiers: "all"/"every" matched natural
    # phrasing that implies no real file scope ("update every mention", "all tests
    # pass", "fix all callers in one file") and inflated trivial edits — notably
    # doc fixes — to "complex", over-routing them to the Agent Teams pipeline.
    # Genuinely broad work still trips "refactor"/"migrate"/"across"/"entire"/etc.
    BROAD_KEYWORDS = [
        "refactor",
        "migrate",
        "redesign",
        "rewrite",
        "across",
        "entire",
        "complete overhaul",
        "from scratch",
        "codebase-wide",
    ]
    MEDIUM_KEYWORDS = [
        "feature",
        "add",
        "implement",
        "create",
        "integrate",
        "build",
        "new",
        "extend",
        "enhance",
    ]
    NARROW_KEYWORDS = [
        "fix",
        "bug",
        "typo",
        "update",
        "change",
        "tweak",
        "config",
        "small",
        "minor",
        "quick",
        "simple",
    ]

    # Architectural indicators
    ARCHITECTURAL_KEYWORDS = [
        "architect",
        "database",
        "schema",
        "migration",
        "api design",
        "authentication",
        "authorization",
        "infrastructure",
        "deploy",
        "microservice",
        "monolith",
        "redesign",
        "new project",
        "platform",
        "framework",
        "core",
        "foundation",
    ]

    # Security-sensitive indicators
    SECURITY_KEYWORDS = [
        "auth",
        "password",
        "token",
        "encrypt",
        "secret",
        "permission",
        "rbac",
        "oauth",
        "jwt",
        "payment",
        "credit card",
        "pii",
        "security",
        "vulnerability",
        "credential",
        "api key",
    ]

    @staticmethod
    def _matches_keyword(keyword: str, text: str) -> bool:
        # Start-of-word match — full substring matching mis-fires on common
        # embeddings ("all" inside "fallback", "fix" inside "prefix", "small"
        # inside "smaller") and used to inflate task complexity for any
        # description that happened to contain those substrings. A strict
        # `\b...\b` match would also lose intended morphology like
        # "auth" → "oauth2" or "create" → "creates", so we anchor only the
        # left side: keyword must start a word, but is allowed to be a prefix
        # of a longer word.
        import re

        return re.search(r"\b" + re.escape(keyword), text) is not None

    def analyze(self, description: str) -> ComplexityAnalysis:
        """Analyze work description and return complexity assessment."""
        desc_lower = description.lower()

        # Estimate file scope
        estimated_files = self._estimate_files(desc_lower)

        # Check for architectural/security concerns
        is_architectural = any(
            self._matches_keyword(k, desc_lower) for k in self.ARCHITECTURAL_KEYWORDS
        )
        is_security = any(
            self._matches_keyword(k, desc_lower) for k in self.SECURITY_KEYWORDS
        )

        # Determine complexity level
        level = self._determine_level(estimated_files, is_architectural, is_security)

        # Build reasoning
        reasoning = self._build_reasoning(
            estimated_files, is_architectural, is_security, level
        )

        # Get pipeline for this complexity
        pipeline = PIPELINES[level]

        # Determine execution mode
        if level == Complexity.TRIVIAL:
            mode = ExecutionMode.DIRECT
        elif level == Complexity.STANDARD:
            mode = ExecutionMode.PLAN_THEN_EXECUTE
        else:
            mode = ExecutionMode.FULL_PIPELINE

        return ComplexityAnalysis(
            level=level,
            estimated_files=estimated_files,
            is_architectural=is_architectural,
            is_security_sensitive=is_security,
            reasoning=reasoning,
            pipeline=pipeline,
            execution_mode=mode,
        )

    def _estimate_files(self, desc_lower: str) -> int:
        """Estimate number of files that will be touched."""
        if any(self._matches_keyword(k, desc_lower) for k in self.BROAD_KEYWORDS):
            return 20
        if any(self._matches_keyword(k, desc_lower) for k in self.MEDIUM_KEYWORDS):
            return 8
        if any(self._matches_keyword(k, desc_lower) for k in self.NARROW_KEYWORDS):
            return 2
        return 5  # default

    def _determine_level(
        self, estimated_files: int, is_architectural: bool, is_security: bool
    ) -> Complexity:
        """Determine complexity level based on analysis."""
        # Start with file-based estimate
        if estimated_files <= 2 and not is_architectural and not is_security:
            level = Complexity.TRIVIAL
        elif estimated_files <= 10 and not is_architectural:
            level = Complexity.STANDARD
        elif estimated_files <= 20 or is_architectural:
            level = Complexity.COMPLEX
        else:
            level = Complexity.EPIC

        # Elevate if security-sensitive
        if is_security and level in (Complexity.TRIVIAL, Complexity.STANDARD):
            level = Complexity.COMPLEX

        return level

    def _build_reasoning(
        self,
        estimated_files: int,
        is_architectural: bool,
        is_security: bool,
        level: Complexity,
    ) -> str:
        """Build human-readable reasoning for the complexity assessment."""
        reasons = []

        reasons.append(f"Estimated ~{estimated_files} files affected")

        if is_architectural:
            reasons.append("Contains architectural changes")
        if is_security:
            reasons.append("Security-sensitive work")

        reasons.append(f"Recommended: {level.value} pipeline")

        return "; ".join(reasons)


# =============================================================================
# GSD State Management
# =============================================================================


def _lazy_load_unified_state():
    """Lazy load UnifiedStateManager to avoid circular imports.

    Returns:
        UnifiedStateManager class if available, None otherwise.
    """
    try:
        from unified_state import UnifiedStateManager

        return UnifiedStateManager
    except ImportError:
        logger.debug("unified_state module not available, using fallback")
        return None


class GSDStateManager:
    """Manages GSD session state from .planning/STATE.md.

    Supports unified state management via UnifiedStateManager when available.
    Falls back to direct STATE.md parsing if unified state is unavailable or fails.
    """

    def __init__(
        self,
        planning_dir: Path | None = None,
        use_unified_state: bool = True,
    ):
        """Initialize GSD state manager.

        Args:
            planning_dir: Override for .planning directory path
            use_unified_state: Whether to use UnifiedStateManager (default: True)
        """
        self.planning_dir = planning_dir or Path.cwd() / ".planning"
        self.state_file = self.planning_dir / "STATE.md"
        self._use_unified_state = use_unified_state
        self._unified_manager = None  # Lazy-loaded

    def _get_unified_manager(self):
        """Get or create the unified state manager instance.

        Returns:
            UnifiedStateManager instance or None if unavailable.
        """
        if not self._use_unified_state:
            return None

        if self._unified_manager is None:
            UnifiedStateManagerClass = _lazy_load_unified_state()
            if UnifiedStateManagerClass is not None:
                try:
                    self._unified_manager = UnifiedStateManagerClass(
                        planning_dir=self.planning_dir
                    )
                except Exception as e:
                    logger.warning(f"Failed to initialize UnifiedStateManager: {e}")
                    self._unified_manager = None

        return self._unified_manager

    def load(self) -> GSDState:
        """Load current GSD state.

        Attempts to use UnifiedStateManager first (if enabled), falling back
        to direct STATE.md parsing if unified state is unavailable or fails.

        Returns:
            GSDState with current session state.
        """
        # Try unified state first
        unified_manager = self._get_unified_manager()
        if unified_manager is not None:
            try:
                unified_state = unified_manager.load()
                return self._convert_from_unified(unified_state)
            except Exception as e:
                logger.warning(f"Unified state load failed, falling back: {e}")

        # Fallback to direct STATE.md parsing
        return self._load_from_state_md()

    def _load_from_state_md(self) -> GSDState:
        """Load state directly from STATE.md (fallback method)."""
        if not self.state_file.exists():
            return GSDState.empty()

        try:
            content = self.state_file.read_text()
            return self._parse_state(content)
        except Exception as e:
            logger.warning(f"Failed to parse STATE.md: {e}")
            return GSDState.empty()

    def _convert_from_unified(self, unified_state) -> GSDState:
        """Convert UnifiedState to GSDState.

        Args:
            unified_state: UnifiedState instance from unified_state module

        Returns:
            GSDState with equivalent data
        """
        # Build progress dict in the expected format
        progress = {}
        for p in unified_state.progress:
            progress[p.name] = {
                "total": str(p.tasks_total),
                "done": str(p.tasks_done),
                "status": p.status,
            }

        return GSDState(
            current_phase=unified_state.current_phase or None,
            last_task=unified_state.last_completed_task or None,
            next_task=unified_state.next_task or None,
            status=unified_state.status or None,
            company_phase=(
                unified_state.company_phase.current_phase
                if unified_state.company_phase
                else None
            ),
            blockers=unified_state.blockers,
            context_snapshot=unified_state.context_snapshot or None,
            progress=progress,
        )

    def _parse_state(self, content: str) -> GSDState:
        """Parse STATE.md content into GSDState."""
        state = GSDState.empty()

        # Parse Last Session section
        session_match = re.search(
            r"## Last Session\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if session_match:
            section = session_match.group(1)

            phase_match = re.search(r"\*\*Phase:\*\* (\S+)", section)
            if phase_match:
                state.current_phase = phase_match.group(1)

            last_match = re.search(r"\*\*Last Completed Task:\*\* (.+)", section)
            if last_match:
                state.last_task = last_match.group(1)

            next_match = re.search(r"\*\*Next Task:\*\* (.+)", section)
            if next_match:
                state.next_task = next_match.group(1)

            status_match = re.search(r"\*\*Status:\*\* (.+)", section)
            if status_match:
                state.status = status_match.group(1)

        # Parse Company Phase section
        company_match = re.search(
            r"## Company Phase\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if company_match:
            section = company_match.group(1)
            phase_match = re.search(r"\*\*Current Phase:\*\* (\w+)", section)
            if phase_match:
                state.company_phase = phase_match.group(1)

        # Parse Context Snapshot
        context_match = re.search(
            r"## Context Snapshot\n.*?\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if context_match:
            state.context_snapshot = context_match.group(1).strip()

        # Parse Blockers
        blockers_match = re.search(
            r"## Blockers\n.*?\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if blockers_match:
            blockers_text = blockers_match.group(1).strip()
            if blockers_text.lower() not in ("none", "n/a", "-"):
                state.blockers = [
                    b.strip()
                    for b in blockers_text.split("\n")
                    if b.strip() and not b.startswith("<!--")
                ]

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
                        phase_name = parts[0]
                        state.progress[phase_name] = {
                            "total": parts[1],
                            "done": parts[2],
                            "status": parts[3],
                        }

        return state

    def update_context(self, context: str) -> None:
        """Update the context snapshot.

        Attempts to use UnifiedStateManager first (if enabled), falling back
        to direct STATE.md manipulation if unified state is unavailable or fails.

        Args:
            context: The new context snapshot text
        """
        # Try unified state first
        unified_manager = self._get_unified_manager()
        if unified_manager is not None:
            try:
                unified_manager.update(context_snapshot=context)
                return
            except Exception as e:
                logger.warning(f"Unified state update failed, falling back: {e}")

        # Fallback to direct STATE.md manipulation
        self._update_context_in_state_md(context)

    def _update_context_in_state_md(self, context: str) -> None:
        """Update context directly in STATE.md (fallback method)."""
        if not self.state_file.exists():
            return

        content = self.state_file.read_text()

        # Find and replace context snapshot section
        new_content = re.sub(
            r"(## Context Snapshot\n<!-- [^>]+ -->\n).*?(?=\n## |\Z)",
            f"\\1{context}\n",
            content,
            flags=re.DOTALL,
        )

        self.state_file.write_text(new_content)


# =============================================================================
# Central Orchestrator
# =============================================================================


class Orchestrator:
    """BMAD/GSD Central Orchestrator.

    The brain of Gatesmith autonomous operations. All work routing decisions
    flow through this orchestrator.
    """

    def __init__(self):
        self.bmad = BMADAnalyzer()
        self.gsd = GSDStateManager()
        self._employee_capabilities: dict[str, list[str]] | None = None

    def route_task(
        self,
        task_id: str,
        title: str,
        description: str | None = None,
        dependencies: list[str] | None = None,
        wave: int = 1,
    ) -> ExecutionPlan:
        """Route a task through the orchestrator.

        This is the main entry point for the daemon. Instead of the daemon
        deciding how to handle work, it asks the orchestrator.

        Args:
            task_id: Unique task identifier
            title: Task title
            description: Optional detailed description (uses title if not provided)
            dependencies: List of task IDs this depends on
            wave: Execution wave for dependency ordering

        Returns:
            ExecutionPlan with full routing decision
        """
        # Analyze complexity using BMAD
        analysis = self.bmad.analyze(description or title)

        # Load current GSD state
        gsd_state = self.gsd.load()

        # Determine suggested employee based on work type
        employee_hint = self._suggest_employee(analysis, title)

        # Build execution plan
        plan = ExecutionPlan(
            task_id=task_id,
            title=title,
            complexity=analysis,
            pipeline=analysis.pipeline,
            current_stage=analysis.pipeline[0] if analysis.pipeline else None,
            execution_mode=analysis.execution_mode,
            gsd_state=gsd_state,
            employee_hint=employee_hint,
            wave=wave,
            dependencies=dependencies or [],
        )

        logger.info(
            f"Routed task {task_id}: complexity={analysis.level.value}, "
            f"mode={analysis.execution_mode.value}, stages={len(analysis.pipeline)}"
        )

        return plan

    def get_state(self) -> GSDState:
        """Get current GSD state."""
        return self.gsd.load()

    def analyze_description(self, description: str) -> ComplexityAnalysis:
        """Analyze a description without creating a full plan.

        Useful for previewing what pipeline would be used.
        """
        return self.bmad.analyze(description)

    def can_proceed(
        self,
        task_id: str,
        current_stage: PipelineStage,
        stage_result: str = "pass",
    ) -> dict[str, Any]:
        """Check if we can proceed to the next pipeline stage.

        Implements quality gates for the GSD pipeline.

        Args:
            task_id: Task identifier
            current_stage: Stage that just completed
            stage_result: "pass", "fail", or "needs_review"

        Returns:
            Dict with proceed (bool), next_stage, reason
        """
        result = {
            "proceed": False,
            "next_stage": None,
            "reason": "",
            "requires_human": False,
        }

        # Gate logic
        if stage_result == "fail":
            result["reason"] = f"Stage {current_stage.value} failed"
            result["requires_human"] = current_stage in (
                PipelineStage.GATE,
                PipelineStage.SECURITY_AUDIT,
            )
            return result

        if stage_result == "needs_review":
            result["reason"] = f"Stage {current_stage.value} needs human review"
            result["requires_human"] = True
            return result

        # Determine next stage
        # This would need the full pipeline context in practice
        result["proceed"] = True
        result["reason"] = f"Stage {current_stage.value} passed"

        return result

    def _suggest_employee(self, analysis: ComplexityAnalysis, title: str) -> str | None:
        """Suggest an employee based on work characteristics."""
        title_lower = title.lower()

        # Simple capability matching
        if analysis.is_security_sensitive:
            return "forge-security-engineer"
        if analysis.is_architectural:
            return "forge-architect"
        if any(k in title_lower for k in ["test", "coverage", "spec"]):
            return "senior-python-developer"
        if any(k in title_lower for k in ["doc", "readme", "tutorial"]):
            return "technical-writer"
        if any(k in title_lower for k in ["website", "html", "css", "frontend"]):
            return "external-webmaster"
        if any(k in title_lower for k in ["marketing", "launch", "announce"]):
            return "marketing-lead"

        # Default to architect for complex, developer for standard
        if analysis.level in (Complexity.COMPLEX, Complexity.EPIC):
            return "forge-architect"
        return "senior-python-developer"

    def _load_employee_capabilities(self) -> dict[str, list[str]]:
        """Load employee capabilities from org.json."""
        if self._employee_capabilities is not None:
            return self._employee_capabilities

        org_path = Path.cwd() / ".company" / "org.json"
        if not org_path.exists():
            return {}

        try:
            with open(org_path) as f:
                org = json.load(f)

            self._employee_capabilities = {}
            for emp in org.get("employees", []):
                emp_id = emp.get("id")
                caps = emp.get("capabilities", [])
                if emp_id:
                    self._employee_capabilities[emp_id] = caps

            return self._employee_capabilities
        except Exception:
            return {}


# =============================================================================
# CLI Interface
# =============================================================================


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="BMAD/GSD Central Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze a task description
  python orchestrator.py analyze "implement user authentication"

  # Route a task from the daemon
  python orchestrator.py route --task-id task-123 --title "Add OAuth support"

  # Get current GSD state
  python orchestrator.py state

  # Check gate for proceeding
  python orchestrator.py gate --stage implement --result pass
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # analyze command
    analyze_parser = subparsers.add_parser(
        "analyze", help="Analyze complexity of a description"
    )
    analyze_parser.add_argument(
        "description", nargs="?", help="Work description to analyze"
    )

    # route command
    route_parser = subparsers.add_parser(
        "route", help="Route a task through the orchestrator"
    )
    route_parser.add_argument("--task-id", required=True, help="Task ID")
    route_parser.add_argument("--title", required=True, help="Task title")
    route_parser.add_argument("--description", help="Detailed description")
    route_parser.add_argument("--wave", type=int, default=1, help="Execution wave")
    route_parser.add_argument(
        "--dependencies", help="Comma-separated dependency task IDs"
    )

    # state command
    subparsers.add_parser("state", help="Get current GSD state")

    # gate command
    gate_parser = subparsers.add_parser(
        "gate", help="Check if we can proceed past a gate"
    )
    gate_parser.add_argument("--task-id", required=True, help="Task ID")
    gate_parser.add_argument(
        "--stage",
        required=True,
        choices=[s.value for s in PipelineStage],
        help="Current stage",
    )
    gate_parser.add_argument(
        "--result",
        default="pass",
        choices=["pass", "fail", "needs_review"],
        help="Stage result",
    )

    args = parser.parse_args()
    orchestrator = Orchestrator()

    if args.command == "analyze":
        desc = args.description
        if not desc:
            desc = sys.stdin.read().strip()
        if not desc:
            print("Error: No description provided", file=sys.stderr)
            sys.exit(1)

        analysis = orchestrator.analyze_description(desc)
        print(json.dumps(analysis.to_dict(), indent=2))

    elif args.command == "route":
        deps = args.dependencies.split(",") if args.dependencies else []
        plan = orchestrator.route_task(
            task_id=args.task_id,
            title=args.title,
            description=args.description,
            wave=args.wave,
            dependencies=deps,
        )
        print(json.dumps(plan.to_dict(), indent=2))

    elif args.command == "state":
        state = orchestrator.get_state()
        print(json.dumps(state.to_dict(), indent=2))

    elif args.command == "gate":
        result = orchestrator.can_proceed(
            task_id=args.task_id,
            current_stage=PipelineStage(args.stage),
            stage_result=args.result,
        )
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
