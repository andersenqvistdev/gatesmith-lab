#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P84: Agent Teams Role Projection — Employee-Informed Multi-Agent Execution.

Integrates Claude Code's experimental Agent Teams feature as an optional
execution mode in the daemon. Uses Role Projection to map existing employee
identities onto teammates for inter-agent collaboration on complex tasks.

Architecture (ADR-004):
    - Role Projection: existing employees projected into team roles via
      agent definition + memory + role-scoped lens
    - Ephemeral gap-fill: meta-agent generates specialists when no employee matches
    - Feature-gated: disabled by default, requires dual opt-in
    - Falls back to single-agent if team execution fails

Usage:
    from team_executor import should_use_team, compose_team, execute_with_team

    if should_use_team(task, config):
        composition = compose_team(task, lead_employee_id, config)
        result = execute_with_team(task, composition, config, project_dir)
    else:
        # Standard single-agent execution
        result = activate_employee_for_task(task)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

try:
    from . import agent_providers as _agent_providers
except ImportError:  # direct script execution
    import agent_providers as _agent_providers

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Lazy imports for sibling modules
_employee_activator = None
_company_resolver = None
_native_teams_executor = None
_worktree_integrity = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global \
        _employee_activator, \
        _company_resolver, \
        _native_teams_executor, \
        _worktree_integrity
    if _employee_activator is not None:
        return

    try:
        from . import company_resolver as cr
        from . import employee_activator as ea
        from . import native_teams_executor as nte
        from . import worktree_integrity as wi

        _employee_activator = ea
        _company_resolver = cr
        _native_teams_executor = nte
        _worktree_integrity = wi
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import employee_activator as ea  # type: ignore[no-redef]
        import worktree_integrity as wi  # type: ignore[no-redef]

        _employee_activator = ea
        _company_resolver = cr
        _worktree_integrity = wi
        # Native teams executor may not exist in older installations
        try:
            import native_teams_executor as nte  # type: ignore[no-redef]

            _native_teams_executor = nte
        except ImportError:
            _native_teams_executor = None


def _resolves_equal(a: Path | str, b: Path | str) -> bool:
    """True if two paths resolve to the same location (tolerant of errors)."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError, TypeError):
        return False


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

FORGE_CONFIG_FILE = "gatesmith.json"
EFFICIENCY_DATA_FILE = "efficiency_data.json"
TEAM_TEMPLATES_DIR = ".claude/agents/teams"

# Maximum prompt sizes to prevent P79-style failures
MAX_AGENT_DEF_CHARS = 3000  # Per teammate agent definition
MAX_MEMORY_CHARS = 2000  # Per teammate memory
MAX_TOTAL_PROMPT_CHARS = 25000  # Total team prompt

# Default team execution timeout (30 minutes — teams take longer)
DEFAULT_TEAM_TIMEOUT = 1800

# Credit weights
DEFAULT_LEAD_CREDIT_WEIGHT = 1.0
DEFAULT_TEAMMATE_CREDIT_WEIGHT = 0.3

# Hiring signal threshold
DEFAULT_HIRING_SIGNAL_THRESHOLD = 5

# WS-068-006: Team context sharing directory
TEAM_CONTEXT_DIR = ".company/team_context"

# Role-to-capability mapping for employee matching
ROLE_CAPABILITY_MAP: dict[str, list[str]] = {
    "security-review": [
        "security",
        "owasp",
        "secrets-scanning",
        "dependency-audit",
        "trust-tiers",
        "hooks",
    ],
    "architecture": [
        "architecture",
        "design-decisions",
        "patterns",
        "technical-documentation",
        "roadmap",
    ],
    "testing": [
        "testing",
        "pytest",
        "tdd",
        "test-coverage",
        "qa",
    ],
    "implementation": [
        "python",
        "feature-implementation",
        "bug-fixing",
        "code-quality",
    ],
    "documentation": [
        "technical-documentation",
        "api-docs",
        "documentation",
    ],
    "investigation": [
        "bug-fixing",
        "debugging",
        "analysis",
        "python",
    ],
}

# Team composition patterns by trigger
TEAM_PATTERNS: dict[str, dict[str, Any]] = {
    "epic-implementation": {
        "triggers": {"complexity": ["epic", "complex"]},
        "roles": ["architecture", "implementation", "security-review", "testing"],
        "template": "epic-implementation.md",
    },
    "light-review": {
        "triggers": {"complexity": ["standard"], "task_type": ["feature", "bug-fix"]},
        "roles": ["security-review"],
        "template": None,
    },
    "deep-dive": {
        "triggers": {"complexity": ["epic"], "domains_min": 4},
        "roles": [
            "architecture",
            "implementation",
            "security-review",
            "testing",
            "documentation",
        ],
        "template": None,
    },
    "debugging-investigation": {
        "triggers": {"min_retries": 3},
        "roles": ["investigation", "investigation", "investigation", "implementation"],
        "template": "debugging-investigation.md",
    },
    "multi-reviewer": {
        "triggers": {"task_type": ["review", "security-audit"]},
        "roles": ["security-review", "testing", "architecture", "implementation"],
        "template": "multi-reviewer.md",
    },
}

# Role lens definitions — narrow a general employee into a focused team contributor
ROLE_LENSES: dict[str, str] = {
    "security-review": (
        "You are the SECURITY REVIEWER on this team. Your job is to:\n"
        "- Challenge the implementation for vulnerabilities\n"
        "- Verify cryptographic correctness if applicable\n"
        "- Check for secret leaks, injection, OWASP risks\n"
        "- Message the team lead if you find blockers\n"
        "DO NOT implement. DO NOT fix. Only review and flag."
    ),
    "architecture": (
        "You are the ARCHITECTURE ADVISOR on this team. Your job is to:\n"
        "- Ensure the approach fits existing patterns\n"
        "- Identify integration points with the codebase\n"
        "- Challenge design decisions that create tech debt\n"
        "- Message the implementer with design guidance\n"
        "DO NOT write production code. Design and advise only."
    ),
    "testing": (
        "You are the TEST STRATEGIST on this team. Your job is to:\n"
        "- Define the test plan before implementation starts\n"
        "- Write tests after implementation completes\n"
        "- Verify edge cases the implementer might miss\n"
        "- Message the team if test coverage is insufficient"
    ),
    "implementation": (
        "You are the IMPLEMENTER on this team. Your job is to:\n"
        "- Write the actual code changes\n"
        "- Follow the architect's design guidance\n"
        "- Address security reviewer feedback\n"
        "- Ensure code passes linting and tests"
    ),
    "documentation": (
        "You are the DOCUMENTATION SPECIALIST on this team. Your job is to:\n"
        "- Write clear, accurate documentation for changes made\n"
        "- Update relevant docs files\n"
        "- Ensure examples are correct and runnable"
    ),
    "investigation": (
        "You are an INVESTIGATOR with a specific hypothesis.\n"
        "- Pursue your assigned hypothesis independently\n"
        "- Gather evidence for and against\n"
        "- Message other investigators if evidence contradicts their theory\n"
        "- Be willing to abandon your hypothesis if evidence disproves it"
    ),
}


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class TeamMember:
    """A member of an Agent Team."""

    role: str  # Role in the team (e.g., "security-review")
    role_lens: str  # Focused instructions for this role
    source_employee_id: str | None  # Employee projected, or None for ephemeral
    source_employee_name: str | None
    agent_definition: str  # Agent def content (capped)
    memory_content: str  # Memory content (capped)
    capabilities: list[str]
    member_type: str  # "projected" or "ephemeral"


@dataclass
class TeamComposition:
    """Complete team composition for Agent Teams execution."""

    lead_employee_id: str
    lead_name: str
    lead_agent_definition: str
    lead_memory_content: str
    lead_capabilities: list[str]
    teammates: list[TeamMember]
    pattern_name: str  # Which TEAM_PATTERN was used
    template_name: str | None  # Template file used


@dataclass
class TeamExecutionResult:
    """Result of Agent Teams execution."""

    success: bool
    output: str
    exit_code: int
    duration_seconds: float
    team_size: int  # lead + teammates
    pattern_used: str
    lead_employee_id: str
    teammate_employee_ids: list[str]  # Only projected, not ephemeral
    ephemeral_roles: list[str]  # Roles filled by meta-agent
    error: str | None = None
    # Result of the WS-103 capture a self-isolated run performs in its own
    # worktree (pr_url / branch). None when the caller supplied the worktree and
    # so owns the capture.
    git_capture: dict | None = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def load_agent_teams_config(base_dir: Path | None = None) -> dict[str, Any]:
    """
    Load agentTeams configuration from gatesmith.json.

    Args:
        base_dir: Repo root to search for gatesmith.json.  When None the
            root is resolved from ``__file__`` so the result is always anchored
            to the module location, never to cwd.  Tests pass ``tmp_path`` here
            to prevent the working-tree's real config from leaking in.

    Returns:
        Agent teams config dict with defaults applied.
    """
    defaults = {
        "enabled": False,
        "experimentalAcknowledged": False,
        "executionStrategy": "role-projection",
        "triggerConditions": {
            "epicTasks": True,
            "stuckTasks": True,
            "stuckRetryThreshold": 3,
            "reviewTeams": False,
            "capabilityGapTasks": True,
            "capabilityGapThreshold": 2,
        },
        "composition": {
            "maxTeammates": 4,
            "excludeExecutives": True,
            "ephemeralHiringSignalThreshold": DEFAULT_HIRING_SIGNAL_THRESHOLD,
            "teamSize": "fixed",
            "dynamicSizing": {
                "enabled": False,
                "baseSizeByComplexity": {
                    "trivial": 1,
                    "standard": 1,
                    "complex": 2,
                    "epic": 4,
                },
                "coverageAdjustments": {
                    "highCoverageThreshold": 0.8,
                    "highCoverageReduction": 1,
                    "lowCoverageThreshold": 0.6,
                    "lowCoverageIncrease": 1,
                },
                "budgetCaps": {
                    "AGGRESSIVE": 4,
                    "NORMAL": 4,
                    "CONSERVATIVE": 2,
                    "MINIMAL": 1,
                },
            },
        },
        "credit": {
            "leadWeight": DEFAULT_LEAD_CREDIT_WEIGHT,
            "teammateWeight": DEFAULT_TEAMMATE_CREDIT_WEIGHT,
        },
        "budget": {
            "tokenMultiplier": 3.0,
            "maxBudgetPerTeam": 45.0,
        },
        "security": {
            "requireHookVerification": True,
            "verifySecretsScanner": True,
            "verifyGitGuardian": True,
        },
    }

    if base_dir is None:
        # Anchor to the repo root via __file__, never cwd.
        # This file lives at <repo>/.claude/hooks/company/team_executor.py
        base_dir = Path(__file__).resolve().parent.parent.parent.parent

    try:
        # Check .claude/ first (canonical location), then root (legacy)
        config_path = base_dir / ".claude" / FORGE_CONFIG_FILE
        if not config_path.exists():
            config_path = base_dir / FORGE_CONFIG_FILE
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                full_config = json.load(f)
            user_config = full_config.get("agentTeams", {})
            # Deep merge user config over defaults
            _deep_merge(defaults, user_config)
    except (json.JSONDecodeError, OSError):
        pass

    return defaults


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base dict (in-place)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# -----------------------------------------------------------------------------
# Decision: Should we use Agent Teams?
# -----------------------------------------------------------------------------

# Capabilities that imply no production-code change (documentation or tests only).
_DOCS_TEST_CAPS = frozenset(
    {
        "documentation",
        "technical-writing",
        "writing",
        "docs",
        "testing",
        "qa",
        "test",
        "test-writing",
    }
)


def _is_docs_or_tests_only(task: dict) -> bool:
    """True if the task's declared work is documentation-only or test-only.

    Returns True only when there is at least one declared capability and EVERY
    capability is a doc/test capability — so a mixed task that also lists a code
    capability (python, backend, ...) is NOT excluded. Falls back to a title
    prefix check for plainly-labelled doc tasks that declare no capabilities.

    Erring toward exclusion is safe: a task kept off the team path simply runs
    on the reliable single-worker path, which handles every task type.
    """
    caps = task.get("required_capabilities") or task.get("capabilities") or []
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(",")]
    caps = [str(c).strip().lower() for c in caps if str(c).strip()]
    if caps:
        return all(c in _DOCS_TEST_CAPS for c in caps)
    # No capabilities declared: fall back to a clearly-labelled doc title.
    title = str(task.get("title", "")).strip().lower()
    return title.startswith(("document", "docs:", "doc:", "docs "))


def should_use_team(
    task: dict,
    config: dict | None = None,
    base_dir: Path | None = None,
) -> bool:
    """
    Determine whether a task should be executed using Agent Teams.

    Checks:
    1. Feature gate: both enabled AND experimentalAcknowledged must be True
    2. Trigger conditions: task matches at least one trigger pattern
    3. Budget: throttle level allows team execution

    Args:
        task: Task dictionary with complexity, retry_count, type fields.
        config: Optional pre-loaded agentTeams config. Loaded if None.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        True if task should use Agent Teams execution.
    """
    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    # Gate 1: Feature must be enabled
    if not config.get("enabled", False):
        return False

    # Gate 2: Experimental must be acknowledged
    if not config.get("experimentalAcknowledged", False):
        return False

    # Gate 3: Respect task-level team_preference override
    team_preference = task.get("team_preference", "mixed")
    if team_preference == "fresh":
        # Caller explicitly requested single-agent execution
        return False
    if team_preference == "persistent":
        # Caller explicitly requested team execution (still subject to feature gate above)
        return True

    # Gate 3b: Documentation/test-only tasks never need Agent Teams.
    # The multi-agent epic-implementation pattern adds no value for a doc edit or
    # a test-only change, and routing them through the team path exposes them to
    # its success-misdetection (a created+merged PR reported as "no deliverable
    # captured" → retried → mis-recorded as failed). The single-worker path
    # handles these reliably. Detected from declared capabilities being a subset
    # of doc/test capabilities (so a mixed code+docs task with code capabilities
    # is NOT excluded).
    if _is_docs_or_tests_only(task):
        return False

    # Gate 4: Check trigger conditions
    conditions = config.get("triggerConditions", {})

    raw_complexity = task.get(
        "complexity", task.get("estimated_complexity", "standard")
    )
    # Normalize legacy effort values (small/medium/large) to complexity scale
    _effort_map = {"small": "standard", "medium": "complex", "large": "epic"}
    complexity = _effort_map.get(raw_complexity, raw_complexity)
    retry_count = task.get("retry_count", 0)
    task_type = task.get("type", task.get("source", ""))

    triggered = False

    # Epic task trigger
    if conditions.get("epicTasks", True) and complexity == "epic":
        triggered = True

    # Complex task trigger (separate from epic for granular control)
    # WS-068-003: Default to True to enforce agent-teams for complex tasks
    if conditions.get("complexTasks", True) and complexity == "complex":
        triggered = True

    # Stuck task trigger (retry threshold exceeded)
    threshold = conditions.get("stuckRetryThreshold", 2)
    if conditions.get("stuckTasks", True) and retry_count >= threshold:
        triggered = True

    # Blocked task rescue (re-queued after max retries with team flag)
    if conditions.get("blockedTasks", True) and task.get("agent_team_attempted", False):
        triggered = True

    # Review team trigger
    if conditions.get("reviewTeams", False) and task_type in [
        "review",
        "security-audit",
    ]:
        triggered = True

    # Capability-gap trigger (escalation ladder step 2): a task spanning
    # multiple capabilities no single employee covers is better split across
    # team roles than forced onto one generalist.
    if conditions.get("capabilityGapTasks", True):
        gap_threshold = conditions.get("capabilityGapThreshold", 2)
        _ensure_imports()
        missing = _employee_activator.get_missing_capabilities(task)
        if len(missing) >= gap_threshold:
            triggered = True

    return triggered


# -----------------------------------------------------------------------------
# Team Need Analysis
# -----------------------------------------------------------------------------


def analyze_team_needs(task: dict) -> tuple[str, list[str]]:
    """
    Determine which team pattern and roles a task needs.

    WS-056-002: Supports 5 patterns scaled by complexity:
    - light-review (x2): standard tasks, just lead + reviewer
    - epic-implementation (x4): complex tasks, full team
    - deep-dive (x6): epic tasks spanning 4+ domains
    - debugging-investigation (x4): stuck tasks with 3+ retries
    - multi-reviewer (x4): review and security-audit tasks

    Args:
        task: Task dictionary.

    Returns:
        Tuple of (pattern_name, list_of_role_names).
    """
    retry_count = task.get("retry_count", 0)
    task_type = task.get("type", task.get("source", ""))
    complexity = task.get("complexity", task.get("estimated_complexity", "standard"))

    # Check debugging pattern first (stuck tasks)
    if retry_count >= 3:
        pattern = TEAM_PATTERNS["debugging-investigation"]
        return "debugging-investigation", list(pattern["roles"])

    # Check review pattern
    if task_type in ["review", "security-audit"]:
        pattern = TEAM_PATTERNS["multi-reviewer"]
        return "multi-reviewer", list(pattern["roles"])

    # WS-056-002: deep-dive for epic tasks with broad domain coverage
    if complexity == "epic":
        # Count domains mentioned in task description
        desc = (task.get("description", "") + " " + task.get("title", "")).lower()
        domain_keywords = [
            "security",
            "architecture",
            "testing",
            "documentation",
            "frontend",
            "backend",
            "database",
            "api",
            "deployment",
        ]
        domains_hit = sum(1 for kw in domain_keywords if kw in desc)
        if domains_hit >= 4:
            pattern = TEAM_PATTERNS["deep-dive"]
            return "deep-dive", list(pattern["roles"])

    # WS-056-002: light-review for standard, well-defined tasks
    if complexity in ("standard", "trivial"):
        pattern = TEAM_PATTERNS["light-review"]
        return "light-review", list(pattern["roles"])

    # Default to epic implementation (complex tasks)
    pattern = TEAM_PATTERNS["epic-implementation"]
    return "epic-implementation", list(pattern["roles"])


# -----------------------------------------------------------------------------
# Team Composition (Role Projection)
# -----------------------------------------------------------------------------


def compose_team(
    task: dict,
    lead_employee_id: str,
    config: dict | None = None,
    base_dir: Path | None = None,
) -> TeamComposition:
    """
    Compose an Agent Team from existing employees using Role Projection.

    The lead is the routed employee. Teammates are best-matching employees
    for needed complementary roles. Gaps are filled with ephemeral specialists.

    Args:
        task: Task dictionary.
        lead_employee_id: Employee ID of the team lead (routed employee).
        config: Optional pre-loaded agentTeams config.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        TeamComposition with lead and teammate details.
    """
    _ensure_imports()

    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    comp_config = config.get("composition", {})
    max_teammates = comp_config.get("maxTeammates", 3)
    exclude_executives = comp_config.get("excludeExecutives", True)

    # Load lead employee context
    lead_ctx = _employee_activator.load_employee_context(lead_employee_id)
    if not lead_ctx:
        # Fallback: minimal lead context
        lead_ctx = _employee_activator.EmployeeContext(
            employee_id=lead_employee_id,
            name=lead_employee_id,
            department="unknown",
            team=None,
            capabilities=[],
            memory_content="",
            agent_definition="",
            efficiency_score=0.0,
        )

    # Determine needed roles
    pattern_name, needed_roles = analyze_team_needs(task)
    template_name = TEAM_PATTERNS.get(pattern_name, {}).get("template")

    # Compute effective team size (dynamic or fixed)
    team_size_mode = comp_config.get("teamSize", "fixed")
    if team_size_mode == "dynamic":
        computed_size = compute_dynamic_team_size(task, lead_ctx.capabilities, config)
    else:
        computed_size = max_teammates

    # Cap teammates at computed size
    needed_roles = needed_roles[:computed_size]

    # Match teammates
    teammates: list[TeamMember] = []
    used_employee_ids = {lead_employee_id}

    team_preference = task.get("team_preference", "mixed")

    for role in needed_roles:
        match = _find_best_employee_for_role(
            role=role,
            exclude_ids=used_employee_ids,
            exclude_executives=exclude_executives,
        )

        if match:
            # Project existing employee into team role
            ctx = _employee_activator.load_employee_context(match["id"])
            if ctx:
                teammates.append(
                    TeamMember(
                        role=role,
                        role_lens=_get_role_lens(role, task),
                        source_employee_id=match["id"],
                        source_employee_name=match.get("name", match["id"]),
                        agent_definition=ctx.agent_definition[:MAX_AGENT_DEF_CHARS],
                        memory_content=ctx.memory_content[:MAX_MEMORY_CHARS],
                        capabilities=ctx.capabilities,
                        member_type="projected",
                    )
                )
                used_employee_ids.add(match["id"])
                continue

        # No match found or context load failed → ephemeral (skipped for "persistent" preference)
        if team_preference == "persistent":
            # Only use existing employees; skip roles that can't be filled
            continue
        teammates.append(_generate_ephemeral_member(role, task))

    return TeamComposition(
        lead_employee_id=lead_employee_id,
        lead_name=lead_ctx.name,
        lead_agent_definition=lead_ctx.agent_definition,
        lead_memory_content=lead_ctx.memory_content,
        lead_capabilities=lead_ctx.capabilities,
        teammates=teammates,
        pattern_name=pattern_name,
        template_name=template_name,
    )


def _find_best_employee_for_role(
    role: str,
    exclude_ids: set[str],
    exclude_executives: bool = True,
) -> dict | None:
    """
    Find the best employee for a team role based on capability overlap.

    Args:
        role: Role name (key in ROLE_CAPABILITY_MAP).
        exclude_ids: Employee IDs already in the team.
        exclude_executives: If True, skip CEO/CTO.

    Returns:
        Employee dict or None if no match.
    """
    _ensure_imports()
    org = _employee_activator.load_org()
    employees = org.get("employees", org.get("agents", []))

    needed_caps = set(ROLE_CAPABILITY_MAP.get(role, []))
    if not needed_caps:
        return None

    executive_roles = {"ceo", "cto", "chief"}
    candidates = []

    for emp in employees:
        emp_id = emp.get("id", "")
        if emp_id in exclude_ids:
            continue
        if emp.get("status") != "available":
            continue
        if exclude_executives:
            title = (emp.get("title", "") or "").lower()
            role_field = (emp.get("role", "") or "").lower()
            if any(ex in title or ex in role_field for ex in executive_roles):
                continue

        emp_caps = set(emp.get("capabilities", []))
        overlap = len(emp_caps & needed_caps)
        if overlap > 0:
            candidates.append((emp, overlap))

    if not candidates:
        return None

    # Sort by overlap count (highest first), break ties by efficiency score
    candidates.sort(
        key=lambda x: (
            x[1],
            x[0].get("efficiency", {}).get("score", 0),
        ),
        reverse=True,
    )
    return candidates[0][0]


def _get_role_lens(role: str, task: dict) -> str:
    """Get the role lens for a team role."""
    base_lens = ROLE_LENSES.get(
        role,
        f"You are the {role.upper()} specialist on this team. "
        f"Focus on your area of expertise and collaborate with teammates.",
    )
    return base_lens


def _generate_ephemeral_member(role: str, task: dict) -> TeamMember:
    """
    Generate an ephemeral team member when no employee matches.

    Records the capability gap for hiring signal tracking.

    Args:
        role: Role needed.
        task: Task context.

    Returns:
        TeamMember with ephemeral type.
    """
    # Track the capability gap
    _record_capability_gap(role, task)

    caps = ROLE_CAPABILITY_MAP.get(role, [])
    role_lens = _get_role_lens(role, task)

    return TeamMember(
        role=role,
        role_lens=role_lens,
        source_employee_id=None,
        source_employee_name=None,
        agent_definition=(
            f"You are a specialist in {role}. "
            f"Your capabilities include: {', '.join(caps)}.\n\n"
            f"Apply your expertise to help the team complete this task."
        ),
        memory_content="",  # No accumulated knowledge
        capabilities=caps,
        member_type="ephemeral",
    )


# -----------------------------------------------------------------------------
# Prompt Construction
# -----------------------------------------------------------------------------

# ~4 chars per token (rough estimate for token cap)
_CHARS_PER_TOKEN = 4
_VISION_TOKEN_CAP = 500
_VISION_CHAR_CAP = _VISION_TOKEN_CAP * _CHARS_PER_TOKEN


def _inject_vision_context() -> str:
    """
    Load condensed vision context from .company/vision.md.

    Extracts mission, active goals (top 3), and top 3 values.
    Caps output at 500 tokens (~2000 chars) so it doesn't crowd the prompt.

    Returns:
        Formatted vision context string, or empty string if vision.md not found.
    """
    _ensure_imports()
    try:
        project_root = _employee_activator.get_project_root()
        vision_path = project_root / ".company" / "vision.md"
        if not vision_path.exists():
            return ""
        text = vision_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    lines = text.splitlines()

    # --- Extract mission (first non-empty paragraph under ## Mission) ---
    mission = ""
    in_mission = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Mission"):
            in_mission = True
            continue
        if in_mission:
            if stripped.startswith("## "):
                break
            # Skip blockquote / meta lines and blank lines until we hit content
            if stripped.startswith(">") or not stripped:
                continue
            # Skip bold headings like **Why we exist:**
            if stripped.startswith("**") and stripped.endswith("**"):
                continue
            if stripped:
                mission = stripped
                break

    # --- Extract active goals (from the active period) ---
    goals: list[str] = []
    in_goals = False
    in_active_period = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Strategic Goals"):
            in_goals = True
            continue
        if not in_goals:
            continue
        if stripped.startswith("## "):
            break
        # Detect the active period heading
        if stripped.startswith("### Period:") and "[status: active]" in stripped:
            in_active_period = True
            continue
        if stripped.startswith("### "):
            in_active_period = False
            continue
        if (
            in_active_period
            and stripped.startswith("|")
            and "Goal" not in stripped
            and "---" not in stripped
        ):
            # Table row: | G7: Sustained Autonomy | ... | description | ... |
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if cells:
                # First cell is goal label, third cell (index 2) is description
                label = cells[0].lstrip("**").rstrip("**")
                desc = cells[2] if len(cells) > 2 else ""
                goals.append(f"{label}: {desc}" if desc else label)
            if len(goals) >= 3:
                break

    # --- Extract top 3 values from values table ---
    values: list[str] = []
    in_values = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Values"):
            in_values = True
            continue
        if not in_values:
            continue
        if stripped.startswith("## "):
            break
        if (
            stripped.startswith("|")
            and "Value" not in stripped
            and "---" not in stripped
        ):
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if cells:
                # First cell is value name (bold), second is description
                name = cells[0].lstrip("**").rstrip("**")
                desc = cells[1] if len(cells) > 1 else ""
                values.append(f"{name}: {desc}" if desc else name)
            if len(values) >= 3:
                break

    # --- Assemble condensed context ---
    parts: list[str] = ["## FORGE VISION CONTEXT\n"]
    if mission:
        parts.append(f"**Mission:** {mission}\n")
    if goals:
        parts.append("**Active Goals:**")
        for g in goals[:3]:
            parts.append(f"- {g}")
        parts.append("")
    if values:
        parts.append("**Core Values:**")
        for v in values[:3]:
            parts.append(f"- {v}")
        parts.append("")
    parts.append("---\n")

    context = "\n".join(parts)

    # Cap at 500 tokens
    if len(context) > _VISION_CHAR_CAP:
        context = context[:_VISION_CHAR_CAP] + "\n[Vision context truncated]\n---\n"

    return context


def build_team_prompt(
    composition: TeamComposition,
    task: dict,
) -> str:
    """
    Build the Agent Teams prompt with projected employee identities.

    The prompt instructs Claude to create a team with specific roles,
    injecting each employee's expertise and context.

    Args:
        composition: TeamComposition with lead and teammates.
        task: Task dictionary.

    Returns:
        Complete team prompt string (capped at MAX_TOTAL_PROMPT_CHARS).
    """
    # Try to load template
    template_content = ""
    if composition.template_name:
        template_path = Path(TEAM_TEMPLATES_DIR) / composition.template_name
        # Try relative to project root
        _ensure_imports()
        project_root = _employee_activator.get_project_root()
        full_path = project_root / template_path
        if full_path.exists():
            try:
                template_content = full_path.read_text(encoding="utf-8")
            except OSError:
                pass

    parts: list[str] = []

    # Part 0: Vision context (prepended so all agents — including ephemeral — see it)
    vision_ctx = _inject_vision_context()
    if vision_ctx:
        parts.append(vision_ctx)

    # Part 1: Team instruction
    parts.append(
        "Create an agent team to complete this task collaboratively.\n"
        "You are the TEAM LEAD. Spawn the teammates described below, "
        "coordinate their work, and produce the final deliverable.\n"
    )

    # Part 2: Lead identity (capped)
    lead_def = composition.lead_agent_definition[:MAX_AGENT_DEF_CHARS]
    lead_mem = composition.lead_memory_content[:MAX_MEMORY_CHARS]

    parts.append("## YOUR IDENTITY (Team Lead)\n")
    if lead_def:
        parts.append(lead_def)
        parts.append("\n---\n")
    if lead_mem:
        parts.append("### Your Current Memory\n")
        parts.append(lead_mem)
        parts.append("\n---\n")

    # Part 3: Teammate definitions
    parts.append(f"## TEAM MEMBERS TO SPAWN ({len(composition.teammates)})\n")

    for i, tm in enumerate(composition.teammates, 1):
        source_label = (
            f" (expertise from {tm.source_employee_name})"
            if tm.source_employee_id
            else " (specialist)"
        )
        parts.append(
            f"### Teammate {i}: {tm.role.replace('-', ' ').title()}{source_label}\n"
        )
        parts.append(f"**Role Focus:**\n{tm.role_lens}\n\n")

        if tm.agent_definition:
            parts.append(f"**Expertise:**\n{tm.agent_definition}\n\n")

        if tm.memory_content:
            parts.append(f"**Context:**\n{tm.memory_content}\n\n")

        parts.append("---\n")

    # Part 4: Template content (if loaded)
    if template_content:
        parts.append("## TEAM WORKFLOW\n")
        parts.append(template_content)
        parts.append("\n---\n")

    # Part 5: Task assignment
    parts.append("## TASK\n")
    parts.append(f"**Task ID:** {task.get('task_id', 'unknown')}\n")
    parts.append(f"**Title:** {task.get('title', 'Untitled')}\n")
    parts.append(f"**Description:** {task.get('description', '')}\n")
    parts.append(f"**Complexity:** {task.get('complexity', 'standard')}\n")
    if task.get("retry_count", 0) > 0:
        parts.append(f"**Previous Attempts:** {task['retry_count']} (all failed)\n")
    parts.append("\n")

    # Part 6: Coordination rules
    parts.append(
        "## COORDINATION RULES\n\n"
        "1. Team lead (you) owns the final deliverable\n"
        "2. Teammates challenge, review, and contribute — they don't override\n"
        "3. All code changes go through the team lead\n"
        "4. If teammates disagree, team lead makes the call\n"
        "5. When done, produce the final implementation\n"
        "6. Do NOT run git add, git commit, git push, deploy, or any gated "
        "operations — these are denied for this session. The daemon "
        "automatically commits your file changes and opens a PR after the "
        "session ends. Files written to disk ARE the deliverable\n"
    )

    prompt = "\n".join(parts)

    # Cap total prompt size
    if len(prompt) > MAX_TOTAL_PROMPT_CHARS:
        prompt = (
            prompt[:MAX_TOTAL_PROMPT_CHARS] + "\n\n[Prompt truncated for size limits]"
        )

    return prompt


# -----------------------------------------------------------------------------
# Execution
# -----------------------------------------------------------------------------


def _create_team_worktree(task: dict) -> tuple[Path, str] | None:
    """Create an isolated git worktree off main for un-isolated team execution.

    Mirrors the daemon's per-task worktree pattern (WS-057). Returns
    ``(worktree_path, branch_name)`` or ``None`` on any failure (the caller then
    refuses rather than running in the main tree).
    """
    try:
        import uuid as _uuid

        task_id = str(task.get("task_id", "team"))
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", task_id)
        suffix = _uuid.uuid4().hex[:6]
        # Per-project base — see company_resolver.get_worktree_base()
        wt_base = _company_resolver.get_worktree_base()
        wt_dir = wt_base / f"{safe_id}-{suffix}"
        wt_branch = f"daemon/team-wt-{safe_id[-12:]}-{suffix}"
        os.makedirs(wt_base, mode=0o700, exist_ok=True)
        # Security: base must not be a symlink (TOCTOU — WS-057-003
        # CRITICAL-2) and the resolved path must stay under the base.
        if not _company_resolver.validate_worktree_base(wt_base, wt_dir):
            return None
        # Clear any stale branch ref from a prior run, then add the worktree.
        subprocess.run(
            ["git", "branch", "-D", wt_branch], capture_output=True, timeout=15
        )
        # origin/main, not local main — see company_resolver.preferred_base_ref.
        base_ref = _company_resolver.preferred_base_ref()
        # --no-track: see employee_activator._create_worker_worktree.
        res = subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--no-track",
                "-b",
                wt_branch,
                str(wt_dir),
                base_ref,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            subprocess.run(
                ["git", "branch", "-D", wt_branch], capture_output=True, timeout=15
            )
            return None
        return wt_dir, wt_branch
    except (subprocess.TimeoutExpired, OSError):
        return None


def _remove_team_worktree(worktree_path: Path) -> None:
    """Best-effort removal of a self-created team worktree."""
    for cmd in (
        ["git", "worktree", "remove", str(worktree_path), "--force"],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _team_isolation_failure(
    composition: TeamComposition, reason: str
) -> TeamExecutionResult:
    """A fail-safe result returned when the team path cannot isolate."""
    return TeamExecutionResult(
        success=False,
        output="",
        exit_code=-1,
        duration_seconds=0.0,
        team_size=1 + len(composition.teammates),
        pattern_used=composition.pattern_name,
        lead_employee_id=composition.lead_employee_id,
        teammate_employee_ids=[],
        ephemeral_roles=[],
        error=f"team execution requires an isolated worktree; {reason}",
    )


def execute_with_team(
    task: dict,
    composition: TeamComposition,
    config: dict | None = None,
    project_dir: Path | None = None,
    base_dir: Path | None = None,
) -> TeamExecutionResult:
    """
    Execute a task using Agent Teams with projected employee identities.

    Worktree isolation: team execution spawns MULTIPLE write-capable Claude
    agents, so it must NEVER run in the main repo root. The daemon supplies an
    isolated worktree via ``project_dir`` (operation_loop WS-092). When no
    ``project_dir`` is given (manual / non-daemon callers), this SELF-ISOLATES:
    it creates a throwaway worktree off main, runs the team there, harvests any
    changes to a branch+PR (reusing the single-agent ``_capture_code_changes``),
    and removes the worktree. If worktree creation fails — or an explicit
    ``project_dir`` resolves to the main repo root — it refuses rather than
    corrupting the working tree (the caller then falls back to single-agent).

    Args:
        task: Task dictionary.
        composition: TeamComposition from compose_team().
        config: Optional pre-loaded agentTeams config.
        project_dir: Isolated worktree for subprocess cwd; self-isolated if None.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        TeamExecutionResult with success status and details.
    """
    _ensure_imports()

    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    main_root = _employee_activator.get_project_root()
    self_worktree: Path | None = None
    if project_dir is None:
        created = _create_team_worktree(task)
        if created is None:
            return _team_isolation_failure(
                composition, "could not create an isolated worktree"
            )
        project_dir, _ = created
        self_worktree = project_dir
    elif _resolves_equal(project_dir, main_root):
        return _team_isolation_failure(
            composition, "refusing to run in the main repo root"
        )

    # SCOUT-20260813-1 — ChainDrop-class defense: verify nothing has touched
    # the worktree's `.claude/settings.json`, `.mcp.json` or `.claude/hooks/`
    # since checkout, BEFORE the team session below can start. Covers both the
    # daemon-supplied project_dir and the self-created fallback above.
    _wt_integrity = _worktree_integrity.verify_worktree_integrity(project_dir)
    if not _wt_integrity.ok:
        _worktree_integrity.log_integrity_violation(
            main_root, str(task.get("task_id")), project_dir, _wt_integrity
        )
        if self_worktree is not None:
            _remove_team_worktree(self_worktree)
        return _team_isolation_failure(composition, _wt_integrity.reason)

    try:
        result = _run_team_in(task, composition, config, project_dir)
        # Harvest self-isolated work to a branch+PR so it survives cleanup.
        if self_worktree is not None and result.success:
            try:
                cap = _employee_activator._capture_code_changes(
                    task, composition.lead_employee_id, self_worktree
                )
                # The caller cannot see this worktree; without the capture the
                # PR opened here was invisible to it, WS-119 failed the task as
                # "no PR was created" and goal autofill minted a twin.
                if isinstance(cap, dict):
                    result.git_capture = cap
                if isinstance(cap, dict) and cap.get("error"):
                    print(
                        f"[WS-115] team self-isolated harvest warning: {cap['error']}",
                        file=sys.stderr,
                    )
            except Exception as _harvest_err:  # noqa: BLE001 — harvest is best-effort
                print(
                    "[WS-115] team self-isolated harvest failed (non-fatal): "
                    f"{_harvest_err}",
                    file=sys.stderr,
                )
        return result
    finally:
        if self_worktree is not None:
            _remove_team_worktree(self_worktree)


def prepare_single_agent_fallback(
    task: dict, lead_employee_id: str, execution_cwd: str | None
) -> dict[str, Any]:
    """Decide whether a failed team run may fall back to single-agent in the
    worktree it just used, and preserve that worktree's work when it may not.

    ``activate_employee_for_task`` runs the worktree integrity check before it
    starts a session. That check is a gate for a worktree nobody has worked in
    yet: it compares the guarded paths (``.claude/settings.json``,
    ``.mcp.json``, ``.claude/hooks/``) with the worktree's own HEAD. After a
    team session whose task edits code under ``.claude/hooks/`` — every
    self-maintenance task in this repository — it cannot pass, so the fallback
    was refused, recorded as a security violation, and returned before any
    capture ran; the task was re-queued and the worktree and branch deleted
    with the work uncommitted. All seven rows written to
    ``worktree_integrity_violations.jsonl`` up to 2026-09-20 were that.

    The control is not relaxed here. When the check does not pass, no session
    is started in that worktree at all: the changes are committed and pushed to
    a wip branch (no pull request) and the caller reports the team failure, so
    the retry gets a fresh worktree. When it passes, the fallback runs exactly
    as before.

    Returns ``{"fallback": bool, "reason": str, "git_capture": dict}``.
    """
    proceed = {"fallback": True, "reason": "", "git_capture": {"captured": False}}
    if not execution_cwd:
        return proceed
    _ensure_imports()
    worktree = Path(execution_cwd)
    integrity = _worktree_integrity.verify_worktree_integrity(worktree)
    if integrity.ok:
        return proceed
    try:
        capture = _employee_activator._capture_code_changes(
            task, lead_employee_id, worktree, wip=True
        )
    except Exception as exc:
        capture = {"captured": False, "wip_branch": None, "error": str(exc)}
    if not isinstance(capture, dict):
        capture = {"captured": False, "wip_branch": None, "error": "no capture result"}
    files = ", ".join(integrity.diverging_files[:5]) or "guarded paths"
    kept = (
        f"preserved on origin/{capture['wip_branch']}"
        if capture.get("wip_branch")
        else f"NOT preserved ({capture.get('error') or 'nothing captured'})"
    )
    return {
        "fallback": False,
        "reason": (
            f"the team session changed {files}; a second session is not started "
            f"in a used worktree — work {kept}"
        ),
        "git_capture": capture,
    }


SESSION_KILL_DRAIN_SECONDS = 10


def _kill_session_group(proc: subprocess.Popen) -> None:
    """End every process in *proc*'s group, then reap *proc*.

    *proc* was started with ``start_new_session=True``, so its pid is the group
    id. A group that is already gone is not an error.
    """
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.communicate(timeout=SESSION_KILL_DRAIN_SECONDS)
    except Exception:
        pass


def _run_session(
    cmd: list[str],
    *,
    prompt: str,
    env: dict[str, str],
    cwd: str,
    timeout: float,
    task_id: str | None = None,
) -> subprocess.CompletedProcess:
    """Run one CLI session to completion in its OWN process group.

    ``subprocess.run``'s timeout kills only the direct child. The session
    command is ``uv run claude ...``, so that child is ``uv`` and the claude
    session, a grandchild, outlived its own timeout: on 2026-09-20 it kept
    working for 3 m 14 s after the limit, in a worktree that had since been
    deleted, with its shell recovered to the operator's home directory,
    permissions bypassed, and every hook failing open because the project
    directory the hook commands ``cd`` into was gone.

    ``start_new_session`` makes the child a group leader; on a timeout, or on
    any other exit from the wait, the whole group is killed before the
    exception leaves this function.

    The launch goes through ``employee_activator._launch_worker_session``
    (TEAM-SPAWN-GATE): the daemon's spawn gate is checked, the session is
    started and, with ``task_id``, its pid and pgid are recorded in the worker
    PID registry, all under the activator's spawn lock. Once the daemon's
    shutdown has closed the gate, the launch raises ``WorkerSpawnRefused`` and
    nothing starts; a session launched before that is in the registry, where
    the shutdown's kill pass finds it. After a normal exit, whatever the
    leader left in its group is killed; the registry entry is removed only
    once the group has no live member.
    """
    _ensure_imports()
    ea = _employee_activator
    proc = ea._launch_worker_session(cmd, cwd=cwd, env=env, task_id=task_id)
    try:
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except BaseException:
            _kill_session_group(proc)
            raise
        # The leader exited on its own; a member it left behind (claude after
        # uv, or a background child) would otherwise run on unsupervised.
        ea._end_group_leftovers(proc, grace=ea.WORKER_KILL_GRACE_SECONDS)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        if task_id is not None and not ea._group_has_members(ea._worker_group_id(proc)):
            ea._forget_worker_pid(task_id, proc.pid)


_RUN_SESSION_REAL = _run_session


def _run_team_in(
    task: dict,
    composition: TeamComposition,
    config: dict,
    project_dir: Path,
) -> TeamExecutionResult:
    """Run the team strategy (native teams or role-projection) in ``project_dir``.

    Assumes ``project_dir`` is an isolated worktree (the caller enforces this).
    """
    _ensure_imports()

    # WS-115: Strategy selection — native teams vs role-projection
    # Native teams triggers for: epic tasks, stuck tasks (3+ retries)
    if _native_teams_executor is not None:
        native_config = config.get("nativeTeamsConfig", {})
        if native_config.get("enabled", False):
            if _native_teams_executor.should_use_native_teams(task, config):
                # Load lead context for native execution
                lead_context = _employee_activator.load_employee_context(
                    composition.lead_employee_id
                )

                print(
                    f"[WS-115] Using NATIVE TEAMS strategy for task={task.get('task_id')}",
                    file=sys.stderr,
                )

                native_result = _native_teams_executor.execute_with_native_teams(
                    task=task,
                    lead_employee_id=composition.lead_employee_id,
                    lead_context=lead_context,
                    config=config,
                    project_dir=project_dir,
                )

                # Convert NativeTeamResult to TeamExecutionResult for compatibility
                return TeamExecutionResult(
                    success=native_result.success,
                    output=native_result.output,
                    exit_code=native_result.exit_code,
                    duration_seconds=native_result.duration_seconds,
                    team_size=1 + len(native_result.teammates_defined or []),
                    pattern_used="native-teams",
                    lead_employee_id=native_result.lead_employee_id,
                    teammate_employee_ids=[],  # Native teams uses dynamic composition
                    ephemeral_roles=native_result.teammates_defined or [],
                    error=native_result.error,
                )

    # Fall through to role-projection (default strategy)
    # Security pre-flight: verify hooks exist
    security_config = config.get("security", {})
    if security_config.get("requireHookVerification", True):
        if not _verify_hooks_exist(project_dir):
            return TeamExecutionResult(
                success=False,
                output="",
                exit_code=1,
                duration_seconds=0.0,
                team_size=1 + len(composition.teammates),
                pattern_used=composition.pattern_name,
                lead_employee_id=composition.lead_employee_id,
                teammate_employee_ids=[
                    tm.source_employee_id
                    for tm in composition.teammates
                    if tm.source_employee_id
                ],
                ephemeral_roles=[
                    tm.role
                    for tm in composition.teammates
                    if tm.member_type == "ephemeral"
                ],
                error="Security hooks not found. Agent Teams requires verified hook inheritance.",
            )

    # Build the team prompt
    prompt = build_team_prompt(composition, task)

    # Load activation config for model and tool settings
    activation_config = _employee_activator.get_activation_config()
    agentic_config = activation_config["agenticMode"]
    complexity = task.get("complexity", task.get("estimated_complexity", "standard"))

    # Use the model appropriate for the complexity
    model = activation_config["modelByComplexity"].get(
        complexity, activation_config["model"]
    )

    # Build command — aligned with employee_activator (WS-121/WS-122).
    # Previously this path used --print + --setting-sources user, which made
    # claude a text generator that wouldn't actually edit files. Tasks
    # routed to AGENT TEAMS (complexity=complex/epic) silently produced
    # zero deliverables → WS-119 1.8 failed them with "no PR was created".
    #
    # Switched to full AGENT mode (no --print) + bypassPermissions, with
    # the same system reinforcement employee_activator uses to enforce
    # actual file edits. CLAUDE.md is loaded by default (no --setting-sources).
    effort_by_complexity = {
        "trivial": "high",
        "standard": "high",
        "complex": "high",
        "epic": "max",
    }
    effort_level = effort_by_complexity.get(complexity, "high")

    system_reinforcement = (
        "IMPORTANT: You MUST use Write or Edit tools to create or modify files. "
        "Text-only responses will be treated as failures. Every task must result "
        "in at least one file written or edited on disk."
    )

    cmd = [
        "uv",
        "run",
        "claude",
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--effort",
        effort_level,
        "--append-system-prompt",
        system_reinforcement,
    ]

    if agentic_config["enabled"]:
        allowed = agentic_config["allowedTools"]
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        disallowed = agentic_config["disallowedTools"]
        if disallowed:
            cmd.extend(["--disallowedTools", ",".join(disallowed)])
        # WS-099: Skip --max-budget-usd. Subscription users have unlimited
        # usage; the flag was raising "Exceeded USD budget" errors anyway.
        # team budget config is now informational only.

    # Prompt is passed via stdin (WS-099-003) — see subprocess.run below.

    # Build environment with Agent Teams enabled. SCOUT-20260726-3: this spawn
    # path was still doing dict(os.environ) + denylist strip after the
    # agent_providers.prepare_environment fix, leaking daemon-ambient secrets
    # (GitHub tokens, cloud credentials, DB URLs) into team workers running
    # inside worktrees that may process untrusted repo content. Delegate to
    # prepare_environment (not a re-inlined allowlist copy) so this path also
    # gets the auth-mode-conditional ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN
    # strip — this CLI always runs claude in subscription-style
    # bypassPermissions mode, so a stray API key in the daemon's ambient
    # shell must not override the OAuth login.
    child_env = _agent_providers.prepare_environment(
        _agent_providers.ProviderSpec(
            name="claude-subscription",
            provider_type="claude",
            auth="subscription",
            model=model,
        )
    )

    # Clean PATH of UV-managed environments
    original_path = child_env.get("PATH", "")
    cleaned_parts = [
        p
        for p in original_path.split(os.pathsep)
        if ".venv" not in p and "/uv/" not in p
    ]
    child_env["PATH"] = os.pathsep.join(cleaned_parts)

    # Enable Agent Teams
    child_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

    # Daemon-context markers (PR 265 review: this spawn path was missed by
    # the agent_providers.prepare_environment fix, so lint_on_edit's
    # humanProtected block never fired for team workers).
    child_env["FORGE_DAEMON"] = "1"
    child_env["FORGE_EMPLOYEE_ID"] = next(
        (
            tm.source_employee_id
            for tm in composition.teammates
            if tm.source_employee_id
        ),
        "agent-team",
    )

    # Set safe terminal defaults
    child_env["TERM"] = "xterm-256color"
    child_env["LANG"] = "en_US.UTF-8"
    child_env = {k: v for k, v in child_env.items() if v}

    # Execute
    start_time = time.time()
    teammate_ids = [
        tm.source_employee_id for tm in composition.teammates if tm.source_employee_id
    ]
    ephemeral_roles = [
        tm.role for tm in composition.teammates if tm.member_type == "ephemeral"
    ]

    # 2026-07-06 fork-bomb guard: this spawn launches a whole claude team; a
    # test reaching it unmocked must fail loudly instead of spawning. The guard
    # recognises a double by the primitive it is handed: a test that replaces
    # _run_session is a double; the real one launches through subprocess.Popen,
    # so that is what the guard is shown.
    runner = globals()["_run_session"]
    assert_spawn_allowed(
        "team_executor._run_team_in",
        subprocess.Popen if runner is _RUN_SESSION_REAL else runner,
    )

    try:
        # WS-099-003: Pass prompt via stdin. With --allowedTools, claude
        # CLI fails to parse positional prompts; stdin works correctly.
        result = runner(
            cmd,
            prompt=prompt,
            env=child_env,
            cwd=str(project_dir),
            timeout=DEFAULT_TEAM_TIMEOUT,
            # TEAM-SPAWN-GATE: recorded in the worker PID registry under the
            # task id, so the daemon's shutdown kill pass finds the session.
            task_id=str(task["task_id"]) if task.get("task_id") else None,
        )

        duration = time.time() - start_time
        output = result.stdout or ""
        stderr = result.stderr or ""

        # Detect success using same logic as employee_activator
        success = _detect_team_success(output, result.returncode)

        # WS-074: Build informative error when failure detected
        error_msg = None
        if not success:
            filtered_stderr = _strip_uv_banner(stderr)
            if result.returncode != 0:
                # Real subprocess failure — surface the actual stderr if
                # there is any (post-banner), otherwise just the exit code.
                error_msg = (
                    filtered_stderr
                    or f"Team execution failed with exit_code={result.returncode}"
                )
            elif filtered_stderr:
                # exit 0 but a real diagnostic on stderr beyond uv noise.
                error_msg = filtered_stderr
            elif not output or len(output.strip()) < 50:
                error_msg = (
                    f"Team produced insufficient output ({len(output.strip())} chars)"
                )
            else:
                error_msg = "Team execution failed (failure pattern detected in output)"

        return TeamExecutionResult(
            success=success,
            output=output,
            exit_code=result.returncode,
            duration_seconds=duration,
            team_size=1 + len(composition.teammates),
            pattern_used=composition.pattern_name,
            lead_employee_id=composition.lead_employee_id,
            teammate_employee_ids=teammate_ids,
            ephemeral_roles=ephemeral_roles,
            error=error_msg,
        )

    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return TeamExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            team_size=1 + len(composition.teammates),
            pattern_used=composition.pattern_name,
            lead_employee_id=composition.lead_employee_id,
            teammate_employee_ids=teammate_ids,
            ephemeral_roles=ephemeral_roles,
            error=f"Team execution timed out after {DEFAULT_TEAM_TIMEOUT}s",
        )

    except Exception as e:
        duration = time.time() - start_time
        return TeamExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            team_size=1 + len(composition.teammates),
            pattern_used=composition.pattern_name,
            lead_employee_id=composition.lead_employee_id,
            teammate_employee_ids=teammate_ids,
            ephemeral_roles=ephemeral_roles,
            error=str(e),
        )


_UV_BANNER_PREFIXES = (
    "Using CPython ",
    "Using Python ",
    "Creating virtual environment",
    "Installed ",
    "Resolved ",
    "Audited ",
    "Built ",
    "Prepared ",
    "Downloaded ",
)


def _strip_uv_banner(stderr: str) -> str:
    """Drop uv's progress/banner lines from a captured stderr.

    `uv run` emits informational lines like
    `Using CPython 3.11.13 / Creating virtual environment in .venv /
     Installed 9 packages in 16ms` on stderr whenever it lazily provisions a
    project's virtualenv. Those lines previously masked real errors when
    surfaced as the team-execution error message. Return only the lines that
    don't look like uv progress noise so a real diagnostic, if any, becomes
    visible.
    """
    if not stderr:
        return ""
    kept = [
        line
        for line in stderr.splitlines()
        if line.strip() and not any(line.startswith(p) for p in _UV_BANNER_PREFIXES)
    ]
    return "\n".join(kept).strip()


def _detect_team_success(output: str, exit_code: int) -> bool:
    """
    Detect whether team execution succeeded.

    Uses similar logic to employee_activator._detect_task_success but
    adapted for team output patterns.

    WS-118: exit_code=0 is necessary but not sufficient. Require evidence
    of work (action markers, completion markers, or substantial output).

    Issue #995: AGENT mode (no --print) renders to terminal and produces
    no stdout. Empty output with exit_code=0 must be treated as a candidate
    success — the downstream P95 deliverable guard in operation_loop will
    catch real phantoms.
    """
    if exit_code != 0:
        return False

    # Check for failure markers FIRST (even in short output)
    failure_patterns = [
        "FAILED test_",
        "FAILED ::",
        "AssertionError:",
        "Traceback (most recent call last)",
    ]
    for pattern in failure_patterns:
        if pattern in output:
            return False

    # WS-118: exit_code=0 requires evidence of work, not blind trust.
    MIN_OUTPUT_LENGTH = 50
    output_lower = output.lower() if output else ""

    action_markers = [
        "committed",
        "wrote",
        "saved",
        "pushed",
        "created file",
        "wrote file",
        "generated",
        "implemented and tested",
        "passed",
        "tests passed",
        "all tests pass",
    ]
    completion_markers = [
        "successfully",
        "implemented",
        "fixed",
        "resolved",
    ]

    has_action = any(m in output_lower for m in action_markers)
    has_completion = any(m in output_lower for m in completion_markers)

    if has_action or has_completion:
        return True
    if output and len(output.strip()) >= MIN_OUTPUT_LENGTH:
        return True
    # Empty stdout + exit_code=0 must be classified as failure so the caller
    # (operation_loop.poll_and_execute_once) takes the team-failure branch
    # and falls back to single-agent activation. Previously this returned
    # True on the assumption that AGENT-mode claude renders to terminal and
    # downstream P95 would catch real phantoms — but that interacted badly
    # with the Agent Teams subsystem, which currently exits 0 with no output
    # *and* no file writes. Result was an infinite retry loop: team reports
    # success → fallback never fires → P95 catches the phantom → retry →
    # same Agent Teams subsystem → same empty success. Validation run on
    # 2026-05-27 looped 5 complex tasks. The unlike-employee_activator part
    # is that team_executor has no filesystem-level success signal, so
    # empty stdout genuinely means "no evidence of work" — surface it as
    # a failure and let the proven single-agent fallback take over.
    return False


def _verify_hooks_exist(project_dir: Path) -> bool:
    """
    Verify that security hooks exist in the project and are configured.

    Agent Teams teammates inherit hooks because they run in the same project
    directory and load the same .claude/settings.json. This pre-flight check
    confirms:
    1. Essential hook scripts exist on disk
    2. Hooks are registered in settings.json (so teammates will load them)

    Args:
        project_dir: Project root directory.

    Returns:
        True if essential hooks exist on disk and are configured.
    """
    hooks_dir = project_dir / ".claude" / "hooks"
    essential_hooks = [
        "secrets_scanner.py",
        "git_guardian.py",
        "block_dangerous.py",
    ]

    # Check hook scripts exist on disk
    for hook in essential_hooks:
        if (
            not (hooks_dir / hook).exists()
            and not (hooks_dir / "company" / hook).exists()
        ):
            return False

    # Check hooks are configured in settings.json
    settings_path = project_dir / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            hooks_config = settings.get("hooks", {})
            pre_tool_use = hooks_config.get("PreToolUse", [])
            # Flatten all hook commands to check essential hooks are registered
            all_commands = ""
            for entry in pre_tool_use:
                for hook_def in entry.get("hooks", []):
                    all_commands += hook_def.get("command", "") + " "
            # Verify at least block_dangerous and secrets_scanner are configured
            if "block_dangerous" not in all_commands:
                return False
            if "secrets_scanner" not in all_commands:
                return False
        except (json.JSONDecodeError, OSError):
            pass  # Settings file issues are non-fatal — hook files exist

    return True


# -----------------------------------------------------------------------------
# Post-Execution: Credit and Memory
# -----------------------------------------------------------------------------


def record_team_results(
    composition: TeamComposition,
    task: dict,
    result: TeamExecutionResult,
    config: dict | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Update memory and efficiency metrics for all team participants.

    Lead gets full credit. Projected teammates get participation credit.
    Ephemeral roles are not tracked (by design).

    Args:
        composition: TeamComposition used.
        task: Original task.
        result: TeamExecutionResult.
        config: Optional agentTeams config.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        Dict summarizing credit distribution.
    """
    _ensure_imports()

    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    credit_config = config.get("credit", {})
    lead_weight = credit_config.get("leadWeight", DEFAULT_LEAD_CREDIT_WEIGHT)
    teammate_weight = credit_config.get(
        "teammateWeight", DEFAULT_TEAMMATE_CREDIT_WEIGHT
    )

    credits: dict[str, Any] = {
        "lead": {"employee_id": composition.lead_employee_id, "weight": lead_weight},
        "teammates": [],
        "ephemeral_roles": result.ephemeral_roles,
    }

    # Update lead memory
    try:
        _append_team_memory(
            composition.lead_employee_id,
            task,
            result,
            role="Team Lead",
            is_lead=True,
        )
    except Exception:
        pass  # Non-fatal

    # Update projected teammate memories
    for tm in composition.teammates:
        if tm.member_type == "projected" and tm.source_employee_id:
            credits["teammates"].append(
                {
                    "employee_id": tm.source_employee_id,
                    "role": tm.role,
                    "weight": teammate_weight,
                }
            )
            try:
                _append_team_memory(
                    tm.source_employee_id,
                    task,
                    result,
                    role=tm.role,
                    is_lead=False,
                )
            except Exception:
                pass  # Non-fatal

    return credits


def _append_team_memory(
    employee_id: str,
    task: dict,
    result: TeamExecutionResult,
    role: str,
    is_lead: bool,
) -> None:
    """Append a team participation note to an employee's memory file."""
    _ensure_imports()
    company_dir = _employee_activator.get_company_dir()
    memory_path = company_dir / "agents" / employee_id / "memory.md"

    if not memory_path.exists():
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    status = "completed" if result.success else "failed"
    role_label = "Led" if is_lead else f"Participated as {role} in"

    entry = (
        f"\n### {timestamp} - Agent Team {status.title()}\n"
        f"**Task:** {task.get('task_id', 'unknown')} — {task.get('title', '')[:80]}\n"
        f"**Role:** {role_label} team ({result.team_size} members, pattern: {result.pattern_used})\n"
        f"**Result:** {status}\n"
        f"**Duration:** {result.duration_seconds:.0f}s\n"
    )

    try:
        with open(memory_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Capability Gap Tracking (Hiring Signals)
# -----------------------------------------------------------------------------


def _record_capability_gap(role: str, task: dict) -> None:
    """
    Record an ephemeral role generation as a capability gap.

    When the same role is generated 5+ times, it becomes a hiring signal.
    """
    _ensure_imports()
    try:
        company_dir = _employee_activator.get_company_dir()
        gap_file = company_dir / EFFICIENCY_DATA_FILE
        if not gap_file.exists():
            data = {}
        else:
            with open(gap_file, encoding="utf-8") as f:
                data = json.load(f)

        gaps = data.setdefault("team_capability_gaps", {})
        role_data = gaps.setdefault(role, {"count": 0, "last_seen": "", "tasks": []})
        role_data["count"] += 1
        role_data["last_seen"] = datetime.now(timezone.utc).isoformat()

        # Keep last 10 task IDs
        task_id = task.get("task_id", "unknown")
        role_data["tasks"] = (role_data.get("tasks", []) + [task_id])[-10:]

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="efficiency_", dir=str(gap_file.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(gap_file))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception:
        pass  # Non-fatal — don't fail task execution for tracking


def get_hiring_signals(threshold: int = DEFAULT_HIRING_SIGNAL_THRESHOLD) -> list[dict]:
    """
    Get roles that have been generated ephemerally enough times to signal hiring need.

    Args:
        threshold: Minimum ephemeral generation count to trigger signal.

    Returns:
        List of dicts with role, count, and last_seen.
    """
    _ensure_imports()
    try:
        company_dir = _employee_activator.get_company_dir()
        gap_file = company_dir / EFFICIENCY_DATA_FILE
        if not gap_file.exists():
            return []

        with open(gap_file, encoding="utf-8") as f:
            data = json.load(f)

        gaps = data.get("team_capability_gaps", {})
        signals = []
        for role, info in gaps.items():
            if info.get("count", 0) >= threshold:
                signals.append(
                    {
                        "role": role,
                        "count": info["count"],
                        "last_seen": info.get("last_seen", ""),
                        "recommendation": (
                            f"Consider /company-hire for '{role}' capability — "
                            f"generated ephemerally {info['count']} times"
                        ),
                    }
                )

        return signals

    except Exception:
        return []


# -----------------------------------------------------------------------------
# Budget Integration
# -----------------------------------------------------------------------------


def estimate_team_cost(
    task: dict,
    config: dict | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Estimate token cost for team execution vs single-agent.

    Args:
        task: Task dictionary.
        config: Optional agentTeams config.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        Dict with single_agent_cost, team_cost, multiplier.
    """
    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    # Base cost estimation
    complexity = task.get("complexity", "standard")
    base_costs = {
        "trivial": 500,
        "standard": 2000,
        "complex": 5000,
        "epic": 15000,
    }
    base_cost = base_costs.get(complexity, 2000)

    budget_config = config.get("budget", {})
    multiplier = budget_config.get("tokenMultiplier", 3.0)

    # Adjust multiplier for dynamic sizing when available
    comp_config = config.get("composition", {})
    team_size_mode = comp_config.get("teamSize", "fixed")
    if team_size_mode == "dynamic":
        dynamic_size = compute_dynamic_team_size(task, [], config)
        max_teammates = comp_config.get("maxTeammates", 4)
        # Scale multiplier proportionally: 1 teammate = base, max = full
        if max_teammates > 0:
            multiplier = 1.0 + (multiplier - 1.0) * (dynamic_size / max_teammates)

    team_cost = int(base_cost * multiplier)

    return {
        "single_agent_cost": base_cost,
        "team_cost": team_cost,
        "multiplier": multiplier,
        "complexity": complexity,
    }


# -----------------------------------------------------------------------------
# Dynamic Team Sizing
# -----------------------------------------------------------------------------


def _infer_capabilities_from_text(task: dict) -> set[str]:
    """
    Infer required capabilities from task title and description keywords.

    Used as a fallback when a task has no explicit required_capabilities.

    Args:
        task: Task dictionary.

    Returns:
        Set of inferred capability strings.
    """
    text = (task.get("title", "") + " " + task.get("description", "")).lower()
    keyword_map: dict[str, list[str]] = {
        "test": ["testing"],
        "security": ["security"],
        "owasp": ["security"],
        "vulnerability": ["security"],
        "architect": ["architecture"],
        "design": ["architecture"],
        "pattern": ["architecture"],
        "implement": ["python", "feature-implementation"],
        "build": ["python", "feature-implementation"],
        "feature": ["python", "feature-implementation"],
        "fix": ["python", "feature-implementation"],
        "bug": ["python", "feature-implementation"],
        "doc": ["documentation"],
        "readme": ["documentation"],
        "debug": ["debugging", "analysis"],
        "investigate": ["debugging", "analysis"],
    }
    caps: set[str] = set()
    import re

    for keyword, mapped_caps in keyword_map.items():
        if re.search(rf"\b{keyword}\b", text):
            caps.update(mapped_caps)
    return caps


def _detect_task_type(task: dict) -> str:
    """
    WS-068-007: Detect task type from title, description, and metadata.

    Used for dynamic team sizing — different task types benefit from
    different team sizes.

    Args:
        task: Task dictionary.

    Returns:
        Task type: bug_fix, feature, documentation, refactor, security, test, unknown
    """
    title = task.get("title", "").lower()
    desc = task.get("description", "").lower()
    text = f"{title} {desc}"

    # Check explicit type field first
    if task.get("type"):
        type_map = {
            "bug": "bug_fix",
            "bugfix": "bug_fix",
            "bug_fix": "bug_fix",
            "feature": "feature",
            "enhancement": "feature",
            "docs": "documentation",
            "documentation": "documentation",
            "refactor": "refactor",
            "refactoring": "refactor",
            "security": "security",
            "audit": "security",
            "test": "test",
            "testing": "test",
        }
        return type_map.get(task["type"].lower(), "unknown")

    # Pattern matching on title/description
    bug_patterns = [
        r"\bfix\b",
        r"\bbug\b",
        r"\berror\b",
        r"\bcrash\b",
        r"\bfail",
        r"\bbroken\b",
        r"\bissue\b",
        r"\bregression\b",
    ]
    feature_patterns = [
        r"\badd\b",
        r"\bimplement\b",
        r"\bcreate\b",
        r"\bnew\b",
        r"\bfeature\b",
        r"\bbuild\b",
        r"\benable\b",
    ]
    docs_patterns = [
        r"\bdoc",
        r"\breadme\b",
        r"\bguide\b",
        r"\btutorial\b",
        r"\bexplain\b",
        r"\bwrite.*doc",
        r"\bupdate.*doc",
    ]
    refactor_patterns = [
        r"\brefactor\b",
        r"\bclean\s*up\b",
        r"\breorganize\b",
        r"\bsimplify\b",
        r"\boptimize\b",
        r"\bimprove\s*code\b",
    ]
    security_patterns = [
        r"\bsecurity\b",
        r"\baudit\b",
        r"\bvulnerab",
        r"\bowasp\b",
        r"\bauth",
        r"\bpermission\b",
        r"\bsecret\b",
    ]
    test_patterns = [
        r"\btest\b",
        r"\bcoverage\b",
        r"\bunit\s*test",
        r"\bintegration\s*test",
        r"\bpytest\b",
        r"\bspec\b",
    ]

    # Score each type
    scores = {
        "bug_fix": sum(1 for p in bug_patterns if re.search(p, text)),
        "feature": sum(1 for p in feature_patterns if re.search(p, text)),
        "documentation": sum(1 for p in docs_patterns if re.search(p, text)),
        "refactor": sum(1 for p in refactor_patterns if re.search(p, text)),
        "security": sum(1 for p in security_patterns if re.search(p, text)),
        "test": sum(1 for p in test_patterns if re.search(p, text)),
    }

    # Return highest scoring type, or unknown if no matches
    if max(scores.values()) > 0:
        return max(scores, key=lambda k: scores[k])
    return "unknown"


def _compute_domain_breadth(task: dict) -> int:
    """
    Count distinct ROLE_CAPABILITY_MAP domains that overlap with the task's
    required capabilities.

    Args:
        task: Task dictionary.

    Returns:
        Number of overlapping domains (minimum 1).
    """
    required = set(task.get("required_capabilities", []))
    if not required:
        required = _infer_capabilities_from_text(task)
    if not required:
        return 1

    count = 0
    for _domain, domain_caps in ROLE_CAPABILITY_MAP.items():
        if required & set(domain_caps):
            count += 1
    return max(count, 1)


def _compute_lead_coverage(task: dict, lead_capabilities: list[str]) -> float:
    """
    Compute fraction of the task's required capabilities covered by the lead.

    Args:
        task: Task dictionary.
        lead_capabilities: List of the lead employee's capabilities.

    Returns:
        Coverage fraction (0.0-1.0), or 0.5 if task has no required
        capabilities (neutral default).
    """
    required = set(task.get("required_capabilities", []))
    if not required:
        inferred = _infer_capabilities_from_text(task)
        if not inferred:
            return 0.5
        required = inferred

    if not required:
        return 0.5

    lead_set = set(lead_capabilities)
    covered = len(required & lead_set)
    return covered / len(required)


def compute_dynamic_team_size(
    task: dict,
    lead_capabilities: list[str],
    config: dict | None = None,
    base_dir: Path | None = None,
) -> int:
    """
    Compute optimal team size based on task characteristics and constraints.

    Considers complexity, domain breadth, lead coverage, and budget throttle
    level to determine how many teammates (1-4) the lead should have.

    Args:
        task: Task dictionary.
        lead_capabilities: List of the lead employee's capabilities.
        config: Optional pre-loaded agentTeams config.
        base_dir: Passed to load_agent_teams_config() when config is None.

    Returns:
        Number of teammates (1 to maxTeammates).
    """
    if config is None:
        config = load_agent_teams_config(base_dir=base_dir)

    comp_config = config.get("composition", {})
    # C1 fix: validate maxTeammates to prevent resource exhaustion
    raw_max = comp_config.get("maxTeammates", 4)
    max_teammates = max(
        1, min(int(raw_max) if isinstance(raw_max, (int, float)) else 4, 10)
    )
    dynamic_config = comp_config.get("dynamicSizing", {})

    # If dynamic sizing not enabled, fall back to fixed maxTeammates
    if not dynamic_config.get("enabled", False):
        return max_teammates

    # Step 1: Base size from complexity
    raw_complexity = task.get(
        "complexity", task.get("estimated_complexity", "standard")
    )
    _effort_map = {"small": "standard", "medium": "complex", "large": "epic"}
    complexity = _effort_map.get(raw_complexity, raw_complexity)

    base_sizes = dynamic_config.get(
        "baseSizeByComplexity",
        {
            "trivial": 1,
            "standard": 1,
            "complex": 2,
            "epic": 4,
        },
    )
    size = base_sizes.get(complexity, 1)

    # WS-068-007: Task-type-based sizing adjustment
    # Different task types have optimal team sizes based on parallelism potential
    task_type = _detect_task_type(task)
    type_sizes = dynamic_config.get(
        "baseSizeByTaskType",
        {
            "bug_fix": 2,  # One investigates, one fixes
            "feature": 3,  # Parallel implementation
            "documentation": 1,  # Serial work
            "refactor": 2,  # Coordination needed
            "security": 2,  # Review + implementation
            "test": 2,  # Write + review
            "unknown": size,  # Use complexity-based default
        },
    )
    type_size = type_sizes.get(task_type, size)
    # Use the higher of complexity-based or type-based size
    size = max(size, type_size)

    # Step 2: Domain breadth adjustment
    breadth = _compute_domain_breadth(task)
    if breadth > size:
        size = min(size + (breadth - size), size + 2)

    # Step 3: Lead coverage adjustment
    coverage_config = dynamic_config.get("coverageAdjustments", {})
    high_threshold = coverage_config.get("highCoverageThreshold", 0.8)
    high_reduction = coverage_config.get("highCoverageReduction", 1)
    low_threshold = coverage_config.get("lowCoverageThreshold", 0.6)
    low_increase = coverage_config.get("lowCoverageIncrease", 1)

    coverage = _compute_lead_coverage(task, lead_capabilities)
    if coverage >= high_threshold:
        size -= high_reduction
    elif coverage < low_threshold:
        size += low_increase

    # Step 4: Budget constraint from throttle level
    # C2/W10 fix: unknown throttle levels default to CONSERVATIVE (2), not uncapped
    _valid_throttle_levels = {
        "AGGRESSIVE",
        "NORMAL",
        "CONSERVATIVE",
        "MINIMAL",
        "PAUSED",
    }
    throttle_level = task.get("_throttle_level", "NORMAL")
    if throttle_level not in _valid_throttle_levels:
        throttle_level = "CONSERVATIVE"
    budget_caps = dynamic_config.get(
        "budgetCaps",
        {
            "AGGRESSIVE": 4,
            "NORMAL": 4,
            "CONSERVATIVE": 2,
            "MINIMAL": 1,
        },
    )
    cap = budget_caps.get(throttle_level, 2)
    if size > cap:
        size = cap

    # Step 5: Clamp to [1, maxTeammates]
    size = max(1, min(size, max_teammates))

    return size


# -----------------------------------------------------------------------------
# WS-068-006: Team Context Sharing
# -----------------------------------------------------------------------------


def get_team_context_path(task_id: str) -> Path:
    """Get the path to the team context file for a task."""
    _ensure_imports()
    company_dir = _company_resolver.get_company_dir()
    context_dir = company_dir / "team_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    return context_dir / f"{task_id}.json"


def save_team_context(
    task_id: str,
    findings: list[dict],
    files_investigated: list[str],
    proposed_changes: list[dict],
    phase: str = "investigation",
) -> bool:
    """
    WS-068-006: Save intermediate team context for coordination.

    Called by team members to share findings with subsequent phases.
    Enables staged execution (WS-068-004) where investigation precedes implementation.

    Args:
        task_id: Task identifier.
        findings: List of findings from investigation phase.
        files_investigated: Files that were examined.
        proposed_changes: Proposed changes for implementation phase.
        phase: Current phase (investigation, implementation, review).

    Returns:
        True if context saved successfully.
    """
    context_path = get_team_context_path(task_id)

    context = {
        "task_id": task_id,
        "phase": phase,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "findings": findings,
        "files_investigated": files_investigated,
        "proposed_changes": proposed_changes,
    }

    try:
        # Atomic write
        fd, tmp = tempfile.mkstemp(dir=str(context_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(context, f, indent=2)
        os.replace(tmp, str(context_path))
        return True
    except OSError:
        return False


def load_team_context(task_id: str) -> dict | None:
    """
    WS-068-006: Load existing team context for a task.

    Called by implementation phase to read investigation findings.

    Args:
        task_id: Task identifier.

    Returns:
        Context dict if exists, None otherwise.
    """
    context_path = get_team_context_path(task_id)

    if not context_path.exists():
        return None

    try:
        with open(context_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def append_team_context(
    task_id: str,
    phase: str,
    findings: list[dict] | None = None,
    files: list[str] | None = None,
    changes: list[dict] | None = None,
) -> bool:
    """
    WS-068-006: Append to existing team context.

    Allows multiple phases to contribute to the shared context.

    Args:
        task_id: Task identifier.
        phase: Phase adding context.
        findings: Additional findings to append.
        files: Additional files investigated.
        changes: Additional proposed changes.

    Returns:
        True if context updated successfully.
    """
    existing = load_team_context(task_id) or {
        "task_id": task_id,
        "findings": [],
        "files_investigated": [],
        "proposed_changes": [],
    }

    # Append new data
    if findings:
        existing.setdefault("findings", []).extend(findings)
    if files:
        existing_files = set(existing.get("files_investigated", []))
        existing_files.update(files)
        existing["files_investigated"] = list(existing_files)
    if changes:
        existing.setdefault("proposed_changes", []).extend(changes)

    existing["phase"] = phase
    existing["timestamp"] = datetime.now(timezone.utc).isoformat()

    return save_team_context(
        task_id,
        existing.get("findings", []),
        existing.get("files_investigated", []),
        existing.get("proposed_changes", []),
        phase,
    )


def cleanup_team_context(task_id: str) -> bool:
    """Remove team context file after task completion."""
    context_path = get_team_context_path(task_id)
    try:
        if context_path.exists():
            context_path.unlink()
        return True
    except OSError:
        return False


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main():
    """CLI entry point for team executor."""
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print("""
P84 Team Executor — Agent Teams Role Projection

Commands:
    status          Show agent teams configuration status
    analyze         Analyze a task for team needs
    signals         Show hiring signals from capability gaps
    help            Show this help

Examples:
    python team_executor.py status
    python team_executor.py signals
    python team_executor.py analyze --complexity epic
""")
        return

    command = args[0]
    result: dict[str, Any] = {}

    try:
        if command == "status":
            config = load_agent_teams_config()
            result = {
                "enabled": config.get("enabled", False),
                "experimentalAcknowledged": config.get(
                    "experimentalAcknowledged", False
                ),
                "active": (
                    config.get("enabled", False)
                    and config.get("experimentalAcknowledged", False)
                ),
                "triggerConditions": config.get("triggerConditions", {}),
                "composition": config.get("composition", {}),
                "budget": config.get("budget", {}),
            }

        elif command == "signals":
            threshold = DEFAULT_HIRING_SIGNAL_THRESHOLD
            for i, arg in enumerate(args[1:], 1):
                if arg == "--threshold" and i < len(args):
                    threshold = int(args[i + 1])

            signals = get_hiring_signals(threshold)
            result = {
                "threshold": threshold,
                "signals": signals,
                "total": len(signals),
            }

        elif command == "analyze":
            complexity = "standard"
            retry_count = 0
            for i, arg in enumerate(args[1:], 1):
                if arg == "--complexity" and i < len(args):
                    complexity = args[i + 1]
                elif arg == "--retries" and i < len(args):
                    retry_count = int(args[i + 1])

            task = {"complexity": complexity, "retry_count": retry_count}
            config = load_agent_teams_config()

            would_use = should_use_team(task, config)
            pattern_name, roles = analyze_team_needs(task)
            cost = estimate_team_cost(task, config)

            result = {
                "would_use_team": would_use,
                "pattern": pattern_name,
                "roles": roles,
                "cost_estimate": cost,
                "reason": (
                    "Feature gate disabled"
                    if not would_use
                    else f"Would use {pattern_name} pattern"
                ),
            }

        else:
            result = {"error": f"Unknown command: {command}"}

    except Exception as e:
        result = {"error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
