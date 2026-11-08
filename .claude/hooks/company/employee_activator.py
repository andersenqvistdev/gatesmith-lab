#!/usr/bin/env python3
# P16: Employee Activation Layer
# /// script
# requires-python = ">=3.10"
# ///
"""
Employee Activation Layer — Wire employees to execute tasks with context.

This module bridges the gap between task assignment and actual execution by:
1. Matching tasks to employees based on capabilities
2. Loading employee memory and agent definitions
3. Executing Claude with employee-specific context
4. Updating employee memory after task completion

Part of P16: Employee Activation Layer implementation.
P16 Bug Fixes Applied: Scored ANY matching, default fallback, capability gap tracking (cf3cf82).

Usage:
    # Match employee for a task
    python employee_activator.py match --task-id "task-123"

    # Load employee context
    python employee_activator.py context --employee-id "senior-python-developer"

    # Execute task with employee context
    python employee_activator.py execute --task-id "task-123" --employee-id "senior-python-developer"

    # Show help
    python employee_activator.py help
"""

import codecs
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

try:
    from . import agent_providers
except ImportError:  # direct script execution
    import agent_providers  # type: ignore[no-redef]

try:
    from . import governing_config
except ImportError:  # direct script execution
    import governing_config  # type: ignore[no-redef]

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Lazy imports for sibling modules
company_resolver = None
work_allocator = None
memory_sync = None
efficiency_tracker = None
complexity_detector = None
task_planner = None
consultant_lifecycle = None
worktree_integrity = None

# WS-068-001: Semantic capability matching - keyword expansion map
# Maps task keywords/phrases to employee capabilities for fuzzy matching
CAPABILITY_SYNONYMS: dict[str, list[str]] = {
    # Web/Frontend
    "web-design": [
        "website",
        "html",
        "css",
        "frontend",
        "ui",
        "landing-page",
        "webpage",
    ],
    "frontend": [
        "react",
        "vue",
        "angular",
        "javascript",
        "typescript",
        "web-design",
        "ui",
    ],
    "dashboard": ["frontend", "visualization", "charts", "graphs", "monitoring"],
    # Backend/Python
    "python": [
        "backend",
        "scripting",
        "automation",
        "api",
        "flask",
        "django",
        "fastapi",
    ],
    "backend": ["api", "server", "database", "python", "node", "go"],
    "api": ["rest", "graphql", "endpoint", "backend", "integration"],
    # DevOps
    "devops": [
        "deployment",
        "ci-cd",
        "docker",
        "kubernetes",
        "infrastructure",
        "installation",
    ],
    "installation": ["setup", "deploy", "configure", "devops", "distribution"],
    # Security
    "security": [
        "audit",
        "vulnerability",
        "owasp",
        "secrets",
        "authentication",
        "authorization",
    ],
    "owasp": ["security", "vulnerability", "injection", "xss"],
    # Documentation
    "documentation": ["docs", "readme", "technical-writing", "guide", "tutorial"],
    "technical-writing": ["documentation", "docs", "copywriting"],
    # Marketing/Business
    "saas-marketing": ["marketing", "landing-page", "conversion", "growth", "sales"],
    "marketing": ["saas-marketing", "content", "seo", "campaign"],
    "sales": ["revenue", "pricing", "conversion", "deals"],
    # Data/Analytics
    "data-analysis": ["analytics", "metrics", "reporting", "sql", "statistics"],
    "analytics": ["data-analysis", "tracking", "metrics", "dashboard"],
    # Architecture
    "architecture": ["design", "planning", "system-design", "patterns", "roadmap"],
    "design-decisions": ["architecture", "adr", "patterns", "trade-offs"],
    # Testing/QA
    "testing": ["qa", "test", "pytest", "unittest", "integration-test"],
    "qa": ["testing", "quality", "bug", "regression"],
}


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver, work_allocator, memory_sync, efficiency_tracker
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr
        from . import memory_sync as ms
        from . import optional_modules as opt
        from . import work_allocator as wa
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import memory_sync as ms  # type: ignore[no-redef]
        import optional_modules as opt  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

    work_allocator = wa
    memory_sync = ms
    # Not in the public export (K2a): None when absent.
    efficiency_tracker = opt.import_optional("efficiency_tracker", __package__)
    company_resolver = cr  # last: it is the already-imported sentinel


def _ensure_worktree_integrity():
    """Lazily import worktree_integrity from the sibling company module."""
    global worktree_integrity
    if worktree_integrity is not None:
        return

    try:
        from . import worktree_integrity as wi
    except ImportError:
        import worktree_integrity as wi  # type: ignore[no-redef]

    worktree_integrity = wi


def _ensure_consultant_lifecycle():
    """Lazily import consultant_lifecycle from the sibling company module."""
    global consultant_lifecycle
    if consultant_lifecycle is not None:
        return

    try:
        from . import optional_modules as opt
    except ImportError:
        import optional_modules as opt  # type: ignore[no-redef]

    # Not in the public export (K2a): None when absent.
    consultant_lifecycle = opt.import_optional("consultant_lifecycle", __package__)


def _ensure_complexity_detector():
    """Lazily import complexity_detector from hooks directory."""
    global complexity_detector
    if complexity_detector is not None:
        return

    try:
        # complexity_detector is in .claude/hooks/, not .claude/hooks/company/
        hooks_dir = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(hooks_dir))
        import complexity_detector as cd  # type: ignore[import]

        complexity_detector = cd
    except ImportError:
        # Fallback: provide a minimal implementation
        complexity_detector = None


def _ensure_task_planner():
    """Lazily import task_planner for GSD/BMAD planning."""
    global task_planner
    if task_planner is not None:
        return

    try:
        from . import task_planner as tp

        task_planner = tp
    except ImportError:
        try:
            import task_planner as tp  # type: ignore[no-redef]

            task_planner = tp
        except ImportError:
            # task_planner not implemented yet - that's OK, we'll skip planning
            task_planner = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ORG_FILE = "org.json"
CONFIG_FILE = "gatesmith.json"
# Written by install.sh; the only config a fresh install has.
TEMPLATE_CONFIG_FILE = ".claude/gatesmith.json"
DEFAULT_MAX_PROMPT_TOKENS = 8000
DEFAULT_EXECUTION_TIMEOUT = 600  # 10 minutes base (WS-105: raised from 300)

# Complexity-based timeout multipliers to prevent premature timeout on complex tasks
COMPLEXITY_TIMEOUT_MULTIPLIERS = {
    "trivial": 0.5,  # 5 minutes
    "standard": 1.0,  # 10 minutes
    "complex": 2.0,  # 20 minutes
    "epic": 3.0,  # 30 minutes
}

# Agentic mode defaults — employees get tool access via --allowedTools
DEFAULT_AGENTIC_MODE = True

AGENTIC_TIMEOUT_MULTIPLIERS = {
    "trivial": 1.0,  # 10 min
    "standard": 2.0,  # 20 min
    "complex": 3.0,  # 30 min
    "epic": 4.5,  # 45 min
}

COMPLEXITY_MAX_BUDGET = {
    "trivial": 5.00,
    "standard": 15.00,
    "complex": 30.00,
    "epic": 50.00,
}

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(pytest *)",
    "Bash(ruff *)",
    "Bash(python *)",
    "Bash(git diff *)",
    "Bash(git status *)",
    "Bash(git status)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(ls *)",
    "Bash(ls)",
    "Bash(cat *)",
    "Bash(wc *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(find *)",
    "Bash(tree *)",
    "Bash(grep *)",
    "Bash(sort *)",
    "Bash(mkdir *)",
    "Bash(mkdir -p *)",
    "Bash(uv run *)",
    "Bash(pip *)",
]

DEFAULT_DISALLOWED_TOOLS = [
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git add *)",
    "Bash(rm *)",
    "Bash(rm -rf *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(docker *)",
    "Bash(sudo *)",
    "Bash(chmod 777 *)",
    # Defense-in-depth: block daemon lifecycle commands at the LLM tool-call layer.
    # The authoritative guard is the FORGE_WORKER_CONTEXT env check in forge_daemon main().
    "Bash(python *forge_daemon.py stop*)",
    "Bash(python *forge_daemon.py start*)",
    "Bash(python *forge_daemon.py restart*)",
    "Bash(python3 *forge_daemon.py stop*)",
    "Bash(python3 *forge_daemon.py start*)",
    "Bash(python3 *forge_daemon.py restart*)",
]


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class EmployeeContext:
    """Context loaded for an employee before task execution."""

    employee_id: str
    name: str
    department: str
    team: str | None
    capabilities: list[str]
    memory_content: str  # Contents of memory.md
    agent_definition: str  # Contents of agent definition .md
    efficiency_score: float
    memory_path: str | None = None
    agent_definition_path: str | None = None


@dataclass
class ExecutionResult:
    """Result of executing a task with employee context."""

    success: bool
    output: str
    exit_code: int
    duration_seconds: float
    error: str | None = None
    employee_id: str | None = None
    task_id: str | None = None


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_project_root() -> Path:
    """Get the project root directory."""
    _ensure_imports()
    # In company mode, project root is current directory or company root
    company_root = company_resolver.find_company_root()
    return company_root if company_root else Path.cwd()


def get_org_path() -> Path:
    """Get the org.json file path."""
    return get_company_dir() / ORG_FILE


def get_config_path() -> Path:
    """Get the gatesmith.json file path."""
    return get_project_root() / CONFIG_FILE


def _normalize_employee_entry(entry):
    """Normalize an org.json employees[] entry to a dict record.

    Thin module-local alias for the canonical
    company_resolver.normalize_employee_entry. A fresh /company-bootstrap can
    write "employees" as bare ID strings instead of full records (ProjectK
    K2); every routing consumer here reads emp.get("id"/"status"/
    "capabilities"), so a bare string crashes get_default_fallback_employee /
    get_employees_by_capability / match_employee_for_task with
    "'str' object has no attribute 'get'". The single source of truth for that
    coercion now lives in company_resolver so all ~34 org consumers share one
    implementation (ProjectK root-cause fix).
    """
    _ensure_imports()  # guarantees company_resolver even if get_company_dir patched
    return company_resolver.normalize_employee_entry(entry, get_company_dir())


def _normalize_org_employees(data: dict) -> dict:
    """Coerce every org['employees'] entry to a dict (drops blank/None).

    Delegates to the canonical company_resolver.normalize_org_employees.
    """
    _ensure_imports()  # guarantees company_resolver even if get_company_dir patched
    return company_resolver.normalize_org_employees(data, get_company_dir())


def load_org() -> dict:
    """Load organization data from org.json with retry on transient failures.

    Uses retry logic to handle race conditions where another process
    may be writing to org.json (atomic writes via tempfile + os.replace
    should prevent truncation, but we add belt-and-suspenders retry).

    Employee entries written as bare ID strings by a fresh bootstrap are
    normalized to dict records (ProjectK K2) so routing never crashes.
    """
    path = get_org_path()

    if not path.exists():
        return {"employees": [], "economics": {}}

    # First attempt
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            # Validate that employees list is not unexpectedly empty
            # If the file exists but employees is empty, it might be a transient state
            if data.get("employees"):
                return _normalize_org_employees(data)
            # Fall through to retry if employees list is empty
    except (json.JSONDecodeError, OSError):
        pass  # Fall through to retry

    # Retry once after 50ms (handles transient read-during-write)
    time.sleep(0.05)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return _normalize_org_employees(data)
    except (json.JSONDecodeError, OSError):
        return {"employees": [], "economics": {}}


def load_config() -> dict:
    """Load configuration from gatesmith.json."""
    path = get_config_path()

    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _automerge_to_main_allowed() -> bool:
    """Whether daemon PRs may enable GitHub auto-merge (``gh pr merge --auto``).

    Respects ``gatesmith.json`` ``autonomy.autoMerge.{enabled,
    allowMergeToMain}``. Fail-safe: a missing/unreadable config or key returns
    False, so a broken or absent config never silently auto-merges to main.

    WS-069-001 originally enabled ``--auto`` unconditionally, which made the
    ``allowMergeToMain`` gate a no-op — daemon PRs merged to main regardless of
    config (caught in a supervised A-run). This guard makes the gate real.
    """
    auto_merge = (load_config().get("autonomy", {}) or {}).get("autoMerge", {}) or {}
    return bool(auto_merge.get("enabled", False)) and bool(
        auto_merge.get("allowMergeToMain", False)
    )


def _resolve_model_profile(config: dict) -> dict:
    """Resolve model-by-complexity from modelProfile in gatesmith.json.

    WS-040: Subscription-aware model profiles. Resolution order:
    1. employeeActivation.modelByComplexity (explicit override)
    2. modelProfiles.profiles[modelProfile] (profile-based)
    3. Hardcoded defaults (Haiku trivial/standard, Sonnet complex/epic)
    """
    profile_name = config.get("modelProfile")
    if not profile_name:
        return {}

    profiles = config.get("modelProfiles", {}).get("profiles", {})
    profile = profiles.get(profile_name, {})
    if not profile:
        return {}

    # Map profile keys to modelByComplexity format (exclude non-model keys)
    _skip = {"executive", "description", "subscription"}
    return {k: v for k, v in profile.items() if k not in _skip}


# The settings model selection reads. Each comes from its governing file, so
# a fresh install whose root holds only ``modelProfile`` still finds the
# template's profiles.
_MODEL_SETTINGS = ["modelProfile", "modelProfiles", "employeeActivation"]


def _model_selection_config() -> dict:
    """``modelProfile``, ``modelProfiles`` and ``employeeActivation`` from
    their governing files. Model choice is not a safety control: an
    unreadable file falls back to the root alone, as before."""
    view = governing_config.view(_MODEL_SETTINGS, get_project_root())
    return view if view is not None else load_config()


def get_activation_config() -> dict:
    """Get employee activation configuration with defaults."""
    config = _model_selection_config()
    activation = config.get("employeeActivation", {})

    # WS-040: Resolve model defaults from profile, then allow explicit overrides
    hardcoded_defaults = {
        "trivial": "claude-haiku-5-5",
        "standard": "claude-haiku-5-5",
        "complex": "claude-sonnet-5-5",
        "epic": "claude-sonnet-5-5",
    }
    profile_models = _resolve_model_profile(config)
    # Profile overrides hardcoded; explicit config overrides profile
    resolved_models = {**hardcoded_defaults, **profile_models}
    explicit_models = activation.get("modelByComplexity", {})
    resolved_models.update(explicit_models)

    return {
        "enabled": activation.get("enabled", True),
        "fallbackToGeneric": activation.get("fallbackToGeneric", True),
        "loadMemory": activation.get("loadMemory", True),
        "loadAgentDefinition": activation.get("loadAgentDefinition", True),
        "updateMemoryAfterTask": activation.get("updateMemoryAfterTask", True),
        "trackEfficiency": activation.get("trackEfficiency", True),
        "maxPromptTokens": activation.get("maxPromptTokens", DEFAULT_MAX_PROMPT_TOKENS),
        "model": activation.get(
            "model", resolved_models.get("standard", "claude-haiku-5-5")
        ),
        "modelByComplexity": resolved_models,
        "agenticMode": _build_agentic_config(activation.get("agenticMode", {})),
    }


def _build_agentic_config(agentic: dict) -> dict:
    """Build agentic mode configuration with defaults."""
    return {
        "enabled": agentic.get("enabled", DEFAULT_AGENTIC_MODE),
        "allowedTools": agentic.get("allowedTools", DEFAULT_ALLOWED_TOOLS),
        "disallowedTools": agentic.get("disallowedTools", DEFAULT_DISALLOWED_TOOLS),
        "maxBudgetByComplexity": agentic.get(
            "maxBudgetByComplexity", COMPLEXITY_MAX_BUDGET
        ),
        "timeoutMultipliers": agentic.get(
            "timeoutMultipliers", AGENTIC_TIMEOUT_MULTIPLIERS
        ),
    }


# Complexity levels that require planning (used with useGsdBmadPlanning)
COMPLEXITY_LEVELS = ["trivial", "standard", "complex", "epic"]


def get_daemon_config() -> dict:
    """Get daemon configuration with GSD/BMAD planning defaults.

    Returns:
        Dict with daemon config including:
        - useGsdBmadPlanning: bool (default False for backward compatibility)
        - complexityThreshold: str (plan for this complexity and above)
    """
    config = load_config()
    daemon = config.get("daemon", {})

    return {
        "enabled": daemon.get("enabled", True),
        "pollIntervalSeconds": daemon.get("pollIntervalSeconds", 60),
        "useGsdBmadPlanning": daemon.get("useGsdBmadPlanning", False),
        "complexityThreshold": daemon.get("complexityThreshold", "standard"),
    }


def _complexity_requires_planning(complexity: str, threshold: str) -> bool:
    """Check if a complexity level requires planning based on threshold.

    Args:
        complexity: Detected complexity level (trivial/standard/complex/epic)
        threshold: Minimum complexity that requires planning

    Returns:
        True if complexity >= threshold (planning required)
    """
    if complexity not in COMPLEXITY_LEVELS or threshold not in COMPLEXITY_LEVELS:
        return False

    complexity_index = COMPLEXITY_LEVELS.index(complexity)
    threshold_index = COMPLEXITY_LEVELS.index(threshold)

    return complexity_index >= threshold_index


# -----------------------------------------------------------------------------
# Employee Matching
# -----------------------------------------------------------------------------


def get_employee_by_id(employee_id: str) -> dict | None:
    """Get employee record from org.json by ID."""
    org = load_org()
    for emp in org.get("employees", []):
        if emp.get("id") == employee_id:
            return emp
    return None


def _expand_capability(cap: str) -> set[str]:
    """
    WS-068-001: Expand a capability keyword to include synonyms.

    Returns the original capability plus any semantically related terms
    that might match employee capabilities.
    """
    expanded = {cap.lower()}
    cap_lower = cap.lower()

    # Direct synonym lookup
    if cap_lower in CAPABILITY_SYNONYMS:
        expanded.update(CAPABILITY_SYNONYMS[cap_lower])

    # Reverse lookup: if cap matches any synonym, include the key
    for key, synonyms in CAPABILITY_SYNONYMS.items():
        if cap_lower in synonyms or cap_lower == key:
            expanded.add(key)
            expanded.update(synonyms)

    # Substring matching for compound terms (e.g., "web-design" matches "web")
    for key in CAPABILITY_SYNONYMS:
        if cap_lower in key or key in cap_lower:
            expanded.add(key)

    return expanded


def get_employees_by_capability(capabilities: list[str]) -> list[dict]:
    """
    Find employees that have ANY of the required capabilities.

    Returns employees sorted by how many capabilities they match (best first).
    Changed from strict ALL matching to scored ANY matching to improve
    task routing when no single employee has all required capabilities.

    WS-068-001: Uses semantic capability matching with keyword expansion.
    A task requiring "website" will match employees with "web-design" capability.
    """
    org = load_org()
    scored = []

    # Expand required capabilities to include synonyms
    expanded_caps: set[str] = set()
    for cap in capabilities:
        expanded_caps.update(_expand_capability(cap))

    for emp in org.get("employees", []):
        emp_caps = {c.lower() for c in emp.get("capabilities", [])}
        # Also expand employee capabilities for bidirectional matching
        emp_expanded: set[str] = set()
        for ec in emp.get("capabilities", []):
            emp_expanded.update(_expand_capability(ec))

        # Count matches: direct + expanded (weighted)
        # Direct match = capability string exactly matches = 2 points
        direct_matches = sum(1 for cap in capabilities if cap.lower() in emp_caps)
        # Expanded match = semantic synonym matched = 1 point
        expanded_matches = len(expanded_caps & emp_expanded) - direct_matches

        # Weight: direct match = 2 points, expanded match = 1 point
        match_score = (direct_matches * 2) + max(0, expanded_matches)

        if match_score > 0:
            scored.append((emp, match_score))

    # Sort by match score descending (best matches first)
    scored.sort(key=lambda x: x[1], reverse=True)
    return [emp for emp, _ in scored]


def get_missing_capabilities(task: dict) -> list[str]:
    """
    Required capabilities the best-matching employee doesn't cover.

    Used by team_executor to decide whether a task spans enough distinct
    capability gaps to warrant Agent Teams instead of a single generalist
    (escalation ladder step 2). Read-only — does not record a gap.
    """
    required_caps = task.get("required_capabilities", []) if task else []
    if not required_caps:
        return []

    matching_employees = get_employees_by_capability(required_caps)
    if not matching_employees:
        return list(required_caps)

    best_caps = {c.lower() for c in matching_employees[0].get("capabilities", [])}
    return [cap for cap in required_caps if cap.lower() not in best_caps]


def get_default_fallback_employee() -> str | None:
    """
    Get a fallback employee using round-robin load balancing.

    Uses a rotating index to distribute unmatched tasks across all available
    employees instead of always defaulting to forge-architect. Executives
    (CEO, CTO) are excluded from fallback routing to preserve their capacity
    for strategic work, but only while a non-executive alternative exists.

    Resolution order:
    1. Config-specified default employee (if set)
    2. Round-robin across available non-executive CODE-CAPABLE employees
    3. Round-robin across available non-executive employees of ANY capability
       (a capability miss doesn't mean the roster is empty — e.g. a
       goal-decomposition task for a non-engineering department)
    4. Round-robin across literally any active/available employee, including
       executives, so a staffed company is never reported as having none

    Returns:
        Employee ID if found, None otherwise
    """
    config = load_config()
    activation = config.get("employeeActivation", {})

    # 1. Check for config-specified default
    default_employee = activation.get("defaultEmployee")
    if default_employee:
        emp = get_employee_by_id(default_employee)
        if emp and emp.get("status") == "available":
            return default_employee

    # 2. Round-robin across available CODE-CAPABLE employees only.
    # Non-code employees (sales, success, analyst, researcher, marketing, CFO)
    # cannot write code and should never receive fallback tasks.
    org = load_org()
    _NON_CODE_IDS = {
        "forge-ceo",
        "forge-cto",
        "cfo",
        "customer-success-lead",
        "revenue-sales-lead",
        "data-analyst",
        "ux-researcher",
        "marketing-lead",
        "external-webmaster",
    }
    # Also detect non-code employees by capability — if they have no code-related
    # capability, exclude them from fallback routing.
    _CODE_CAPABILITIES = {
        "python",
        "testing",
        "security",
        "backend",
        "frontend",
        "code-review",
        "devops",
        "installation",
        "shell-scripting",
        "test-automation",
        "bug-fixing",
        "feature-implementation",
        "hooks",
        "bash",
        "fastapi",
    }
    available = []
    for emp in org.get("employees", []):
        emp_id = emp.get("id")
        if not emp_id or emp.get("status") != "available":
            continue
        if emp_id in _NON_CODE_IDS:
            continue
        emp_caps = {c.lower() for c in emp.get("capabilities", [])}
        if emp_caps & _CODE_CAPABILITIES:
            available.append(emp_id)

    if not available:
        # No code-capable candidate matched. This does not mean the roster
        # is empty — it means the capability filter is too narrow for this
        # task (e.g. a goal-decomposition task for a non-engineering
        # department). Broaden to any active non-executive employee before
        # giving up (ProjectK finding K5: this used to return None here and
        # get misreported upstream as "no employees available").
        available = [
            emp.get("id")
            for emp in org.get("employees", [])
            if emp.get("id")
            and emp.get("status") in ("available", "active")
            and emp.get("id") not in _NON_CODE_IDS
        ]

    if not available:
        # Only executives (or truly nobody) remain — better to route to an
        # executive than to falsely report an empty roster.
        available = [
            emp.get("id")
            for emp in org.get("employees", [])
            if emp.get("id") and emp.get("status") in ("available", "active")
        ]

    if not available:
        return None

    # Load and increment round-robin index from a state file
    state_path = get_company_dir() / "state" / "routing_state.json"
    rr_index = 0
    try:
        if state_path.exists():
            state = json.loads(state_path.read_text())
            rr_index = state.get("fallback_rr_index", 0)
    except (json.JSONDecodeError, OSError):
        rr_index = 0

    selected = available[rr_index % len(available)]

    # Persist next index (atomic write)
    try:
        import os
        import tempfile

        new_state = {"fallback_rr_index": (rr_index + 1) % len(available)}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(new_state, f)
        os.replace(tmp, str(state_path))
    except OSError:
        pass  # Non-critical — worst case we re-use the same index

    return selected


def record_capability_gap(
    required_capabilities: list[str],
    matched_capabilities: list[str],
    fallback_employee_id: str | None,
    task_id: str | None = None,
) -> None:
    """
    Record a capability gap when no employee fully matches task requirements.

    This data is stored in efficiency_data.json and can be used by the
    initiative engine to suggest hiring employees with missing capabilities.

    Args:
        required_capabilities: Capabilities the task required
        matched_capabilities: Capabilities that were actually matched
        fallback_employee_id: Employee ID used as fallback
        task_id: Optional task ID for tracking
    """
    _ensure_imports()

    missing_caps = set(required_capabilities) - set(matched_capabilities)
    if not missing_caps:
        return

    # Load efficiency data
    company_dir = get_company_dir()
    data_path = company_dir / "state/efficiency_data.json"

    try:
        if data_path.exists():
            with open(data_path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        # Initialize capability_gaps structure if needed
        if "capability_gaps" not in data:
            data["capability_gaps"] = {
                "gaps": [],
                "summary": {},
            }

        # Record the gap
        from datetime import datetime, timezone

        gap_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "required": list(required_capabilities),
            "missing": list(missing_caps),
            "fallback_employee": fallback_employee_id,
            "task_id": task_id,
        }
        data["capability_gaps"]["gaps"].append(gap_record)

        # Keep only last 100 gaps
        data["capability_gaps"]["gaps"] = data["capability_gaps"]["gaps"][-100:]

        # Update summary counts
        summary = data["capability_gaps"]["summary"]
        for cap in missing_caps:
            summary[cap] = summary.get(cap, 0) + 1

        # Write back
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    except (OSError, json.JSONDecodeError):
        pass  # Don't fail task execution due to tracking errors


def get_capability_gap_summary() -> dict:
    """
    Get summary of capability gaps for hiring recommendations.

    Returns dict with:
    - gaps: List of recent gap records
    - summary: Dict of capability -> count (most needed capabilities)
    - recommendations: List of capabilities to hire for (count >= 3)
    """
    company_dir = get_company_dir()
    data_path = company_dir / "state/efficiency_data.json"

    try:
        if not data_path.exists():
            return {"gaps": [], "summary": {}, "recommendations": []}

        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)

        gaps_data = data.get("capability_gaps", {"gaps": [], "summary": {}})
        summary = gaps_data.get("summary", {})

        # Recommend hiring for capabilities with 3+ gaps
        recommendations = [
            {"capability": cap, "gap_count": count}
            for cap, count in sorted(summary.items(), key=lambda x: x[1], reverse=True)
            if count >= 3
        ]

        return {
            "gaps": gaps_data.get("gaps", [])[-10:],  # Last 10 gaps
            "summary": summary,
            "recommendations": recommendations,
        }

    except (OSError, json.JSONDecodeError):
        return {"gaps": [], "summary": {}, "recommendations": []}


def match_employee_for_task(task: dict) -> str | None:
    """
    Match the best employee for a task based on capabilities and efficiency.

    Resolution order:
    0. If assigned_to is set and is a valid employee, use that directly
    1. Find employees with ANY matching capabilities (scored by match count)
    2. Try efficiency-based routing among candidates
    3. Fall back to first available matching employee
    4. Fall back to default employee if no capability match

    Args:
        task: Task dictionary with required_capabilities, tags, etc.

    Returns:
        Employee ID if matched, None otherwise
    """
    _ensure_imports()

    # Priority 0: Respect explicit assigned_to if it's a valid employee
    assigned_to = task.get("assigned_to")
    if assigned_to and isinstance(assigned_to, str):
        emp = get_employee_by_id(assigned_to)
        if emp:
            return assigned_to

    required_caps = task.get("required_capabilities", [])

    # Find employees with matching capabilities (scored, best matches first)
    matching_employees = (
        get_employees_by_capability(required_caps) if required_caps else []
    )

    if matching_employees:
        candidate_ids = [emp.get("id") for emp in matching_employees if emp.get("id")]
        selected_id = None

        # Try efficiency-based routing first
        if candidate_ids:
            optimal_id = work_allocator.suggest_optimal_agent_for_task(
                task, candidate_ids
            )
            if optimal_id:
                selected_id = optimal_id

        # Fall back to first available employee with matching capabilities
        if not selected_id:
            for emp in matching_employees:
                if emp.get("status") == "available":
                    selected_id = emp.get("id")
                    break

        # Return best match even if not "available"
        if not selected_id:
            selected_id = matching_employees[0].get("id")

        # Track partial capability gaps (employee matched some but not all)
        if selected_id and required_caps:
            selected_emp = get_employee_by_id(selected_id)
            if selected_emp:
                emp_caps = set(selected_emp.get("capabilities", []))
                matched = [cap for cap in required_caps if cap in emp_caps]
                if len(matched) < len(required_caps):
                    # Partial match - record the gap for hiring recommendations
                    record_capability_gap(
                        required_capabilities=required_caps,
                        matched_capabilities=matched,
                        fallback_employee_id=selected_id,
                        task_id=task.get("task_id"),
                    )

        return selected_id

    # No capability match at all. Before falling back to a generalist,
    # check for a short-lived consultant already registered for this kind
    # of specialized need (escalation ladder step 4) — cheaper than a
    # permanent hire and a better fit than a generalist for niche skills.
    # Archive search is skipped here (include_archived=False): reactivating
    # an archived consultant is a deliberate action, not an inline hot-path
    # side effect of routing an unrelated task.
    fallback_id = None
    if required_caps:
        _ensure_consultant_lifecycle()
        try:
            consultant_match = consultant_lifecycle.find_matching_consultant(
                request=f"{task.get('title', '')} {task.get('description', '')}".strip(),
                required_skills=required_caps,
                include_archived=False,
            )
        except Exception:
            consultant_match = None
        if consultant_match:
            candidate_id = consultant_match.get("consultant", {}).get("id")
            # Defense in depth: re-validate against the same format
            # register_consultant() enforces at write time before trusting
            # an org.json-sourced id as an employee_id (used downstream to
            # build agent-definition file paths).
            if candidate_id and re.match(r"^[a-z][a-z0-9-]*$", candidate_id):
                fallback_id = candidate_id

    # No matching consultant either - use default fallback employee
    # This ensures tasks are never assigned to non-employee IDs like "daemon-6036"
    if not fallback_id:
        fallback_id = get_default_fallback_employee()

    # Record capability gap for hiring recommendations
    if required_caps:
        record_capability_gap(
            required_capabilities=required_caps,
            matched_capabilities=[],
            fallback_employee_id=fallback_id,
            task_id=task.get("task_id"),
        )

    return fallback_id


# -----------------------------------------------------------------------------
# Agent Definition Resolution
# -----------------------------------------------------------------------------


def get_agent_definition_path(
    employee_id: str, department: str | None = None
) -> Path | None:
    """
    Resolve the agent definition file path for an employee.

    Resolution order:
    1. Exact match by ID: .claude/agents/company/{department}/{employee-id}.md
    2. Role-based match: .claude/agents/company/{department}/senior-engineer.md
    3. Department head: .claude/agents/company/{department}/head.md
    4. Generic fallback: .claude/agents/company/coordinator.md

    Args:
        employee_id: The employee ID
        department: Optional department hint

    Returns:
        Path to agent definition if found, None otherwise
    """
    project_root = get_project_root()
    agents_dir = project_root / ".claude" / "agents" / "company"

    if not agents_dir.exists():
        return None

    # 1. Try exact match with department
    if department:
        exact_path = agents_dir / department / f"{employee_id}.md"
        if exact_path.exists():
            return exact_path

    # 2. Try exact match without department (check all departments)
    for dept_dir in agents_dir.iterdir():
        if dept_dir.is_dir():
            exact_path = dept_dir / f"{employee_id}.md"
            if exact_path.exists():
                return exact_path

    # 3. Try top-level match (e.g., forge-ceo.md)
    top_level = agents_dir / f"{employee_id}.md"
    if top_level.exists():
        return top_level

    # 4. Role-based fallback (senior-python-developer -> senior-engineer)
    role_mappings = {
        "senior-python-developer": "senior-engineer",
        "senior-typescript-developer": "senior-engineer",
        "junior-developer": "senior-engineer",
        "lead-developer": "tech-lead",
    }

    mapped_role = role_mappings.get(employee_id)
    if mapped_role and department:
        role_path = agents_dir / department / f"{mapped_role}.md"
        if role_path.exists():
            return role_path

    # 5. Department head fallback
    if department:
        head_path = agents_dir / department / "head.md"
        if head_path.exists():
            return head_path

    # 6. Generic coordinator fallback
    coordinator_path = agents_dir / "coordinator.md"
    if coordinator_path.exists():
        return coordinator_path

    return None


# -----------------------------------------------------------------------------
# Context Loading
# -----------------------------------------------------------------------------


def read_file_content(path: Path, max_chars: int | None = None) -> str:
    """Read file content safely with optional truncation."""
    if not path.exists():
        return ""

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
            if max_chars and len(content) > max_chars:
                return content[:max_chars] + "\n\n[... truncated ...]"
            return content
    except (OSError, IOError):
        return ""


def load_employee_context(employee_id: str) -> EmployeeContext | None:
    """
    Load full context for an employee before task execution.

    Loads:
    - Employee record from org.json
    - Memory file content
    - Agent definition content

    Args:
        employee_id: The employee ID

    Returns:
        EmployeeContext if employee found, None otherwise
    """
    _ensure_imports()
    config = get_activation_config()

    # Get employee record
    employee = get_employee_by_id(employee_id)
    if not employee:
        return None

    # Get paths
    memory_path = get_company_dir() / "agents" / employee_id / "memory.md"
    if not memory_path.exists():
        # Try alternate path from memoryPath field
        mem_path_str = employee.get("memoryPath", "")
        if mem_path_str:
            memory_path = get_project_root() / mem_path_str

    agent_def_path = get_agent_definition_path(employee_id, employee.get("department"))

    # Load content
    memory_content = ""
    if config["loadMemory"]:
        # Estimate max chars based on token limit (rough estimate: 4 chars per token)
        max_memory_chars = (config["maxPromptTokens"] // 2) * 4
        memory_content = read_file_content(memory_path, max_memory_chars)

    agent_definition = ""
    if config["loadAgentDefinition"] and agent_def_path:
        max_def_chars = (config["maxPromptTokens"] // 2) * 4
        agent_definition = read_file_content(agent_def_path, max_def_chars)

    # Get efficiency score
    efficiency_score = 0.8  # Default
    emp_efficiency = employee.get("efficiency", {})
    if emp_efficiency:
        efficiency_score = emp_efficiency.get("score", 0.8)

    return EmployeeContext(
        employee_id=employee_id,
        name=employee.get("name", employee_id),
        department=employee.get("department", "unknown"),
        team=employee.get("team"),
        capabilities=employee.get("capabilities", []),
        memory_content=memory_content,
        agent_definition=agent_definition,
        efficiency_score=efficiency_score,
        memory_path=str(memory_path) if memory_path.exists() else None,
        agent_definition_path=str(agent_def_path) if agent_def_path else None,
    )


# -----------------------------------------------------------------------------
# Initiative Injection
# -----------------------------------------------------------------------------

_FALLBACK_SUGGESTIONS = [
    "Are there tests missing for the code you touched?",
    "Could any repeated patterns be extracted into a shared utility?",
    "Did you encounter unclear documentation that could be improved?",
    "Is there a performance concern in the code paths you reviewed?",
    "Could error handling be improved in the area you worked in?",
]

COMPLEXITY_ORDER = ["trivial", "standard", "complex", "epic"]

# Security constants for initiative suggestion sanitization
MAX_SUGGESTION_LEN = 200
MAX_SUGGESTIONS = 20
MAX_POOL_FILE_SIZE = 65536  # 64KB
MAX_PROMPT_CHARS = 40960  # 40KB — P79 safe limit
SUSPICIOUS_PATTERNS = re.compile(
    r"ignore.*(previous|above|all)|you are now|<system|```(bash|sh|zsh)|IGNORE|override|forget.*(instructions|rules)",
    re.IGNORECASE,
)


def _load_initiative_config() -> dict:
    """Load initiative_prompts config from gatesmith.json. Returns defaults if missing."""
    defaults = {
        "enabled": True,
        "frequency": 0.3,
        "min_complexity": "standard",
        "excluded_sources": ["escalation", "cron"],
        "suggestions_pool_path": ".company/initiative_suggestions.json",
    }
    try:
        config_path = (
            Path(__file__).resolve().parent.parent.parent.parent / "gatesmith.json"
        )
        if config_path.exists():
            with open(config_path) as f:
                data = json.load(f)
            initiative = data.get("initiative_prompts", {})
            if initiative:
                merged = dict(defaults)
                merged.update(initiative)
                return merged
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


def _should_inject_initiative(task: dict, config: dict) -> bool:
    """Determine whether to inject an initiative section for this task."""
    if not config.get("enabled", True):
        return False

    # Check excluded sources
    source = task.get("source", "")
    excluded = config.get("excluded_sources", [])
    if source in excluded:
        return False

    # Check min complexity
    task_complexity = task.get("complexity", "standard")
    min_complexity = config.get("min_complexity", "standard")
    task_idx = (
        COMPLEXITY_ORDER.index(task_complexity)
        if task_complexity in COMPLEXITY_ORDER
        else 1
    )
    min_idx = (
        COMPLEXITY_ORDER.index(min_complexity)
        if min_complexity in COMPLEXITY_ORDER
        else 1
    )
    if task_idx < min_idx:
        return False

    # Frequency gating: deterministic based on task_id + date
    frequency = max(0.0, min(1.0, float(config.get("frequency", 0.3))))
    task_id = task.get("task_id", "unknown")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    gate_value = (
        int(hashlib.sha256((task_id + date_str).encode()).hexdigest(), 16) % 100
    )
    return gate_value < frequency * 100


def _select_initiative_suggestion(task_id: str, config: dict) -> str:
    """Select a suggestion from the pool, deterministically based on task_id."""
    suggestions = None
    pool_path_str = config.get("suggestions_pool_path", "")
    if pool_path_str:
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        pool_path = (project_root / pool_path_str).resolve()
        # Critical Fix 1: Reject path traversal outside project root
        if not pool_path.is_relative_to(project_root):
            pool_path = None  # Reject path traversal
        if pool_path is not None:
            try:
                if pool_path.exists():
                    # Critical Fix 2: File size check
                    if pool_path.stat().st_size > MAX_POOL_FILE_SIZE:
                        pool_path = None  # Too large
                    else:
                        with open(pool_path) as f:
                            loaded = json.load(f)
                        if isinstance(loaded, list) and loaded:
                            suggestions = [
                                s[:MAX_SUGGESTION_LEN]
                                for s in loaded[:MAX_SUGGESTIONS]
                                if isinstance(s, str)
                                and not SUSPICIOUS_PATTERNS.search(s)
                            ]
                            if not suggestions:
                                suggestions = None
            except (json.JSONDecodeError, OSError):
                pass

    if not suggestions:
        suggestions = _FALLBACK_SUGGESTIONS

    idx = int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % len(suggestions)
    return suggestions[idx]


def _build_initiative_section(task: dict, config: dict) -> str:
    """Build the initiative injection section, or return empty string if not applicable."""
    if not _should_inject_initiative(task, config):
        return ""

    task_id = task.get("task_id", "unknown")
    suggestion = _select_initiative_suggestion(task_id, config)

    return f"""
## Initiative Opportunity (Optional)

After completing your primary task, you may submit ONE improvement proposal via submit_proposal() if you notice something worth improving.

Suggestion: {suggestion}
"""


# -----------------------------------------------------------------------------
# Prompt Construction
# -----------------------------------------------------------------------------


def _get_relevant_patterns(task: dict, max_patterns: int = 3) -> str:
    """WS-106-006: Load patterns relevant to this task from patterns.json.

    Filters patterns by:
    - Category matching task type (testing, bug-fix, infrastructure, etc.)
    - File types matching task's likely file extensions
    - Tags matching task keywords

    Args:
        task: Task dictionary with description, title, tags, etc.
        max_patterns: Maximum patterns to include (to limit prompt size)

    Returns:
        Formatted guidance section, or empty string if no relevant patterns.
    """
    try:
        try:
            from . import pattern_extractor
        except ImportError:
            import pattern_extractor  # type: ignore[no-redef]
    except ImportError:
        return ""

    try:
        patterns = pattern_extractor.list_patterns()
        if not patterns:
            return ""

        # Build relevance score for each pattern
        task_text = f"{task.get('title', '')} {task.get('description', '')}".lower()
        task_tags = set(t.lower() for t in task.get("tags", []))

        scored_patterns = []
        for p in patterns:
            score = 0
            category = p.get("category", "").lower()
            p_tags = set(t.lower() for t in p.get("tags", []))

            # Category match (highest weight)
            if category and category in task_text:
                score += 5
            if "test" in category and "test" in task_text:
                score += 3
            if "bug" in category and ("fix" in task_text or "bug" in task_text):
                score += 3

            # Tag overlap
            common_tags = task_tags & p_tags
            score += len(common_tags) * 2

            # Title/description keyword match
            p_title = p.get("title", "").lower()
            for word in p_title.split():
                if len(word) > 3 and word in task_text:
                    score += 1

            # Confidence boost
            confidence = p.get("confidence", 0)
            if confidence >= 0.8:
                score += 2

            if score > 0:
                scored_patterns.append((score, p))

        if not scored_patterns:
            return ""

        # Sort by score, take top N
        scored_patterns.sort(key=lambda x: x[0], reverse=True)
        top_patterns = [p for _, p in scored_patterns[:max_patterns]]

        # Format guidance section
        lines = ["## Relevant Patterns (WS-106-006)\n"]
        lines.append("The following patterns from past successes may help:\n")

        for p in top_patterns:
            lines.append(
                f"**{p.get('title', 'Unnamed')}** ({p.get('category', 'general')})"
            )
            if p.get("approach"):
                lines.append(f"  Approach: {p['approach'][:200]}")
            if p.get("tools_used"):
                lines.append(f"  Tools: {', '.join(p['tools_used'][:5])}")
            lines.append("")

        return "\n".join(lines) + "\n---\n"

    except Exception:
        return ""


def _extract_target_file_paths(description: str) -> list[str]:
    """Extract file paths mentioned in a task description.

    Handles backtick-wrapped paths, slash-separated paths, and bare filenames.
    Returns up to 10 unique paths.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = p.strip().strip("/")
        if p and p not in seen:
            seen.add(p)
            found.append(p)

    # Backtick-wrapped paths: `path/to/file.py`
    for p in re.findall(r"`([^`\s]+\.[a-zA-Z0-9]{1,6})`", description):
        if "/" in p or p.endswith(".py") or p.endswith(".json"):
            _add(p)

    # Slash-separated paths: path/to/file.py
    for p in re.findall(
        r"(?<![:/\w])((?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[a-zA-Z0-9]{1,6})(?!\w)",
        description,
    ):
        _add(p)

    # Bare filenames with extensions: filename.py, config.json
    for p in re.findall(r"\b(\w[\w-]*[._]\w+\.(?:py|json|md))\b", description):
        _add(p)

    return found[:10]


def _get_git_log_for_files(paths: list[str], project_root: Path) -> str:
    """Return formatted recent git log lines for each target file."""
    if not paths:
        return ""
    lines: list[str] = []
    for path in paths[:5]:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-5", "--", path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(project_root),
            )
            log_output = result.stdout.strip()
            if log_output:
                lines.append(f"`{path}`:")
                for entry in log_output.splitlines():
                    lines.append(f"  {entry}")
        except (subprocess.TimeoutExpired, OSError):
            continue
    if not lines:
        return ""
    return "**Recent git log:**\n" + "\n".join(lines)


def _find_related_test_files(paths: list[str], project_root: Path) -> list[str]:
    """Return test file paths that correspond to the given source files."""
    found: list[str] = []
    seen: set[str] = set()
    for path in paths[:5]:
        stem = Path(path).stem
        candidates = [
            f"tests/test_{stem}.py",
            f"tests/{stem}_test.py",
            f"test_{stem}.py",
        ]
        for candidate in candidates:
            if candidate not in seen and (project_root / candidate).exists():
                seen.add(candidate)
                found.append(candidate)
                break
    return found


def _load_patterns_from_knowledge(
    task: dict, project_root: Path, max_patterns: int = 3
) -> str:
    """Load relevant patterns from .company/knowledge/patterns.json."""
    patterns_path = project_root / ".company" / "knowledge" / "patterns.json"
    if not patterns_path.exists():
        return ""
    try:
        with open(patterns_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""
    patterns = data.get("patterns", [])
    if not patterns:
        return ""

    task_text = f"{task.get('title', '')} {task.get('description', '')}".lower()
    task_tags = {t.lower() for t in task.get("tags", [])}

    scored: list[tuple[int, dict]] = []
    for p in patterns:
        score = 0
        category = p.get("category", "").lower()
        p_tags = {t.lower() for t in p.get("tags", [])}

        if category and category in task_text:
            score += 5
        if "test" in category and "test" in task_text:
            score += 3
        if "bug" in category and ("fix" in task_text or "bug" in task_text):
            score += 3
        score += len(task_tags & p_tags) * 2
        for word in p.get("title", "").lower().split():
            if len(word) > 3 and word in task_text:
                score += 1
        if p.get("confidence", 0) >= 0.8:
            score += 2

        if score > 0:
            scored.append((score, p))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [p for _, p in scored[:max_patterns]]

    lines = ["**Relevant patterns:**"]
    for p in top:
        title = p.get("title", "Unnamed")[:80]
        category = p.get("category", "general")
        approach = p.get("approach", "")[:200]
        lines.append(f"- **{title}** ({category}): {approach}")
    return "\n".join(lines)


def _build_worker_context_section(task: dict) -> str:
    """Build the worker context section injected before task execution.

    P85-5: Provides file-level context so workers start informed rather than
    discovering everything cold.
    """
    try:
        project_root = get_project_root()
        description = task.get("description", task.get("title", ""))
        parts: list[str] = []

        target_paths = _extract_target_file_paths(description)
        if target_paths:
            parts.append(
                "**Target files:** " + ", ".join(f"`{p}`" for p in target_paths)
            )

        if target_paths:
            git_log = _get_git_log_for_files(target_paths, project_root)
            if git_log:
                parts.append(git_log)

        if target_paths:
            test_files = _find_related_test_files(target_paths, project_root)
            if test_files:
                parts.append(
                    "**Related tests:** " + ", ".join(f"`{p}`" for p in test_files)
                )

        patterns_text = _load_patterns_from_knowledge(task, project_root)
        if patterns_text:
            parts.append(patterns_text)

        if not parts:
            return ""

        return "\n\n## Worker Context\n\n" + "\n\n".join(parts) + "\n"
    except Exception:
        return ""  # Never let context injection break task execution


def build_execution_prompt(
    task: dict, context: EmployeeContext, timeout_seconds: int | None = None
) -> str:
    """
    Build the execution prompt for an employee task.

    WS-121: Lean prompt for AGENT mode. Instead of stuffing 15-25KB into
    the prompt (agent definition + memory + patterns), give a short task
    description and tell Claude where to read context files. Claude in
    AGENT mode can Read files itself — progressive disclosure.

    P85-5: Inject worker context (target files, git log, test paths, patterns)
    so workers start with file-level context rather than discovering it cold.

    G7: Workers have no awareness they run one-shot, non-resumable batch
    execution with a hard wall-clock timeout — a slow task can time out
    with a final message promising to "check back later," a promise that
    can never be kept and guarantees a failed, deliverable-less task. Rule
    6 below makes that constraint explicit and tells the worker to stop and
    report honest partial progress instead — deliberately NOT phrased as
    "hurry up," since a stated time budget can otherwise incentivize
    rushed or fabricated completions.

    Args:
        task: Task dictionary with task_id, description, priority, etc.
        context: EmployeeContext with memory and agent definition
        timeout_seconds: Real, already complexity-adjusted execution timeout
            for this task, if known. When provided, the prompt states the
            approximate time budget in minutes; when None, the one-shot/
            no-continuation instruction is still included, just without a
            specific figure.

    Returns:
        Complete prompt string
    """
    parts = []

    # WS-121: Reference context files instead of inlining them
    context_refs = []
    if context.agent_definition_path:
        context_refs.append(
            f"- Your role definition: `{context.agent_definition_path}` (Read this first)"
        )
    if context.memory_path:
        context_refs.append(f"- Your memory/learnings: `{context.memory_path}`")

    if context_refs:
        parts.append("## Context Files\n")
        parts.append("\n".join(context_refs))
        parts.append("\n\n")
        parts.append("\n---\n")

    # P85-5: Inject worker context (target files, git log, test paths, patterns)
    worker_ctx = _build_worker_context_section(task)
    if worker_ctx:
        parts.append(worker_ctx)
        parts.append("\n---\n")

    # WS-121: Lean task prompt for AGENT mode.
    # Claude runs as a full agent — it can Read files, Grep for context,
    # explore the codebase. No need to stuff everything into the prompt.
    task_id = task.get("task_id", "unknown")
    description = task.get("description", task.get("title", "No description"))
    priority = task.get("priority", 3)

    # Include failure hints if this is a retry
    hints = ""
    preempt_hints = task.get("preempt_hints", [])
    if preempt_hints:
        hints = "\n**Prior failures:** " + "; ".join(preempt_hints[:3])

    if timeout_seconds:
        minutes = max(1, round(timeout_seconds / 60))
        time_budget_note = f" You have approximately {minutes} minutes for this task."
    else:
        time_budget_note = ""

    parts.append(f"""## Task: {description}

**ID:** {task_id} | **Priority:** P{priority}
{hints}

## Rules

1. You MUST create or modify files. Text-only output = FAILURE.
2. Read existing code before making changes.
3. Run pytest on your changes. Run ruff check on Python files.
4. Do NOT run git add, commit, or push — the system handles persistence.
5. Do NOT delete files unless the task explicitly requires it.
6. This is a one-shot, non-interactive execution: there is no future turn,
   no "checking back later," and no continuation after this response
   ends.{time_budget_note} If a slow operation (e.g. a long test run)
   risks running out of time, STOP it now and report your best honest
   partial progress instead of letting it run — do not write output that
   promises to check back, follow up, or continue later, since that
   promise cannot be fulfilled and the task will be marked failed with no
   deliverable. This is not a signal to rush, hurry, or cut corners to
   beat the clock — it is only a signal to stop and report honestly
   rather than leaving a broken promise as your final output.

Begin now.""")
    # No else branch — WS-121 uses AGENT mode for all tasks

    return "\n".join(parts)


# -----------------------------------------------------------------------------
# Worker scratch directory
# -----------------------------------------------------------------------------


# Short on purpose. macOS caps an AF_UNIX socket path at 104 bytes; under
# launchd the temp dir is /var/folders/xx/<28 chars>/T/ (49 bytes), and a
# worker's tools put sockets in $TMPDIR (multiprocessing.Manager, pytest-xdist,
# ssh control sockets). A name carrying the task id reached 106 bytes. The same
# prefix is checked by hook_utils.worker_write_escapes_root — keep them equal.
WORKER_SCRATCH_PREFIX = "fs-"
# Room left in the scratch path for a socket name inside it.
WORKER_SCRATCH_SOCKET_ROOM = 24
AF_UNIX_PATH_MAX = 104


def _worker_scratch_base() -> Path:
    """Where per-task scratch directories are created: the daemon's temp dir,
    as given (not resolved: /var -> /private/var adds 8 bytes), or /tmp when
    that is too long to leave room for a socket name."""
    base = Path(tempfile.gettempdir())
    longest = len(str(base)) + 1 + len(WORKER_SCRATCH_PREFIX) + 8
    if longest + 1 + WORKER_SCRATCH_SOCKET_ROOM >= AF_UNIX_PATH_MAX:
        return Path("/tmp")
    return base


def _create_worker_scratch_dir(task_id: str, worktree: Path | str) -> Path | None:
    """Create a per-task scratch directory OUTSIDE the worker's worktree.

    Workers wrote probe logs, commit-message drafts, ``_dbg_test.py`` and
    ``.scratch_probe/`` into their worktree, and those reached pull requests
    (#616, #629, #631, #635, #636; .gitignore rules in #630/#639 only catch
    names already seen). The session gets this directory as ``TMPDIR`` and
    ``FORGE_SCRATCH_DIR`` and is told to keep every scratch file there.

    The path is short (``<tmp>/fs-XXXXXXXX``) and not resolved, so a socket
    created in it stays under the 104-byte AF_UNIX limit. ``task_id`` is only
    used in log lines, never in the path.

    The directory is refused (None) when it would land inside the worktree or
    beside it — worktrees live side by side under one parent, and
    ``hook_utils.worker_write_escapes_root`` blocks writes into a sibling of
    the worktree even under the temp directory. Best-effort: None on any
    error, and the session then runs exactly as before.
    """
    try:
        base = _worker_scratch_base()
        scratch = Path(tempfile.mkdtemp(prefix=WORKER_SCRATCH_PREFIX, dir=str(base)))
        real = scratch.resolve()
        wt = Path(worktree).resolve()
        for forbidden in (wt, wt.parent):
            try:
                real.relative_to(forbidden)
            except ValueError:
                continue
            _remove_worker_scratch_dir(scratch)
            return None
        return scratch
    except Exception:
        return None


def _remove_worker_scratch_dir(scratch: Path | None) -> None:
    """Delete a scratch directory made by ``_create_worker_scratch_dir``.

    Only a direct child of the scratch base with the scratch prefix is ever
    deleted. A symlink put in its place is unlinked, never followed. What
    ``rmtree`` could not remove is logged, not ignored.
    """
    if not scratch:
        return
    try:
        scratch = Path(scratch)
        if not scratch.name.startswith(WORKER_SCRATCH_PREFIX):
            return  # never delete a directory this module did not create
        if scratch.parent != _worker_scratch_base():
            return
        if scratch.is_symlink():
            scratch.unlink()
            return
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)
        if os.path.lexists(scratch):
            print(
                f"[worker scratch] could not remove {scratch} — left behind",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[worker scratch] cleanup of {scratch} failed: {exc}", file=sys.stderr)


def _scratch_prompt_section(scratch: Path) -> str:
    return (
        "\n\n## Scratch files\n\n"
        f"Put EVERY scratch file in `{scratch}` (also exported as "
        "$FORGE_SCRATCH_DIR and $TMPDIR): probe scripts and their logs, debug "
        "or throwaway tests, commit-message drafts, notes, copies of output. "
        "Never create them in the worktree — anything left there can end up in "
        "the pull request. The worktree must contain only the deliverable. "
        "The scratch directory is deleted when this task ends."
    )


# -----------------------------------------------------------------------------
# Execution
# -----------------------------------------------------------------------------


def _detect_task_success(output: str, exit_code: int) -> bool:
    """
    Detect task success from output content and exit code.

    Claude may return non-zero exit codes even when tasks complete successfully.
    This function looks at output markers to determine actual success.

    IMPORTANT: Exit code 0 does NOT guarantee success. Claude can exit 0 with
    minimal/no output when it doesn't actually complete work (P51 fix).

    Args:
        output: stdout from Claude execution
        exit_code: Process exit code

    Returns:
        True if task appears to have completed successfully
    """
    # Robustness: a negative exit code means the process was killed by a
    # signal — e.g. -9 (SIGKILL) from the activity-stall/2h-cap killer below,
    # -15 (SIGTERM), or -11 (SIGSEGV). The marker scan further down would
    # falsely report success if the partial output happens to contain an
    # action word ("wrote", "passed", ...), producing a phantom completion
    # for a worker that was forcibly terminated mid-task. Forced termination
    # is never success.
    if exit_code < 0:
        return False

    # WS-121: AGENT mode produces no stdout (renders to terminal).
    # exit_code=0 means Claude completed. Trust it. The deliverable
    # check and phantom guard handle whether files were produced.
    if exit_code == 0:
        return True
        # This prevents P51 from blocking P68's advisory handling

    # Check for completion markers in output
    output_lower = output.lower()

    # For non-zero exit codes, require strong action verbs that indicate
    # actual work was done (not just generic "completed" / "done")
    action_markers = [
        "committed",
        "wrote",
        "saved",
        "pushed",
        "created file",
        "wrote file",
        "generated",
        "implemented and tested",
        "passed",  # Test results: "N passed"
        "tests passed",
        "all tests pass",
    ]

    # Generic completion words are too loose for non-zero exit codes
    completion_markers = [
        "successfully",
        "implemented",
        "fixed",
        "resolved",
    ]

    # P86 FIX: Separate hard and soft failure markers.
    # Hard markers indicate definite failures (exceptions, tracebacks).
    # Soft markers can appear in success messages ("I fixed the not found error").
    hard_failure_markers = [
        "traceback",
        "exception:",
        "fatal error",
        "panic:",
        "segmentation fault",
    ]

    # Soft markers only checked for non-zero exit codes
    soft_failure_markers = [
        "error:",
        "failed:",
        "could not",
        "unable to",
        "permission denied",
        "not found",
    ]

    has_action = any(marker in output_lower for marker in action_markers)
    has_completion = any(marker in output_lower for marker in completion_markers)
    has_hard_failure = any(marker in output_lower for marker in hard_failure_markers)
    has_soft_failure = any(marker in output_lower for marker in soft_failure_markers)

    # P86 FIX + WS-118: exit_code=0 is necessary but not sufficient.
    # Require evidence of work (action/completion markers or substantial output).
    if exit_code == 0 and not has_hard_failure:
        if has_action or has_completion:
            return True
        if output and len(output.strip()) >= 200:
            return True
        return False  # exit_code=0 but no evidence of work

    # For non-zero exit codes: check both hard and soft failure markers
    has_any_failure = has_hard_failure or has_soft_failure

    # If output has strong action verbs and no failure markers, consider success
    if has_action and not has_any_failure:
        return True

    # If output has completion markers and no failure markers, consider success
    if has_completion and not has_any_failure:
        return True

    # Removed: "output > 200 chars = success" heuristic (false positive source)

    return False


def _verify_task_artifacts(task: dict, project_root: Path) -> bool:
    """
    Verify that expected task artifacts actually exist on disk.

    For planning-source tasks, checks ROADMAP.md XML to find expected
    <file> and <action> elements, then verifies:
    - CREATE: file exists
    - MODIFY: file mtime > task start time

    Args:
        task: Task dictionary with notes containing roadmap metadata
        project_root: Project root directory

    Returns:
        True if artifacts verified or verification not applicable.
        False if expected artifacts are missing.
    """
    import re as _re

    # Extract roadmap metadata from task notes
    notes = task.get("notes", [])
    roadmap_meta = None
    for note in notes if isinstance(notes, list) else []:
        content = note.get("content", "") if isinstance(note, dict) else str(note)
        try:
            parsed = json.loads(content)
            if "roadmap_task_id" in parsed:
                roadmap_meta = parsed
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if not roadmap_meta:
        return True  # Can't verify non-roadmap tasks — graceful degradation

    roadmap_file = roadmap_meta.get("roadmap_file", "")
    roadmap_task_id = roadmap_meta.get("roadmap_task_id", "")

    if not roadmap_file or not roadmap_task_id:
        return True

    roadmap_path = Path(roadmap_file)
    if not roadmap_path.exists():
        return True  # Can't verify if ROADMAP.md is missing

    try:
        content = roadmap_path.read_text(encoding="utf-8")
    except OSError:
        return True

    # Find the task XML block for this task ID
    task_pattern = _re.compile(
        rf'<task[^>]*id=["\']{_re.escape(roadmap_task_id)}["\'][^>]*>.*?</task>',
        _re.IGNORECASE | _re.DOTALL,
    )
    task_match = task_pattern.search(content)
    if not task_match:
        return True  # Task not found in ROADMAP — can't verify

    task_xml = task_match.group(0)

    # Extract <file> element
    file_match = _re.search(
        r"<file>\s*(.+?)\s*</file>", task_xml, _re.IGNORECASE | _re.DOTALL
    )
    if not file_match:
        return True  # No file expectation — can't verify

    expected_file = file_match.group(1).strip()

    # Extract <action> element
    action_match = _re.search(
        r"<action>\s*(.+?)\s*</action>", task_xml, _re.IGNORECASE | _re.DOTALL
    )
    action = action_match.group(1).strip().upper() if action_match else "CREATE"

    # Resolve file path relative to project root
    expected_path = project_root / expected_file

    # Security: Prevent path traversal attacks
    # Reject paths containing .. or absolute paths that escape project root
    if ".." in expected_file or expected_file.startswith("/"):
        return True  # Suspicious path — skip verification, don't trust it
    try:
        resolved_path = expected_path.resolve()
        if not resolved_path.is_relative_to(project_root.resolve()):
            return True  # Path escapes project root — skip verification
    except (ValueError, OSError):
        return True  # Resolution failed — skip verification

    if action == "CREATE":
        return expected_path.exists()
    elif action == "MODIFY":
        if not expected_path.exists():
            return False
        # Check if file was modified after task started
        started_at = task.get("started_at", "")
        if started_at:
            try:
                start_ts = datetime.fromisoformat(started_at).timestamp()
                file_mtime = expected_path.stat().st_mtime
                return file_mtime > start_ts
            except (ValueError, OSError):
                return True  # Can't verify timing — assume OK
        return True
    else:
        return True  # Unknown action — can't verify


# -----------------------------------------------------------------------------
# P52: Deliverable Verification
# -----------------------------------------------------------------------------

# Task types that require file creation
DOCUMENT_TASK_KEYWORDS = [
    "draft",
    "create",
    "write",
    "generate",
    "document",
    "proposal",
    "plan",
    "spec",
    "playbook",
    "template",
    "brief",
    "report",
]

# Task types that require code changes
CODE_TASK_KEYWORDS = [
    "implement",
    "fix",
    "add",
    "update",
    "refactor",
    "modify",
    "change",
    "build",
    "develop",
]


def _detect_task_type(description: str) -> str:
    """
    Detect task type from description to determine verification strategy.

    Returns:
        'document' - requires file creation
        'code' - requires git changes
        'other' - no specific verification
    """
    desc_lower = description.lower()

    # Check for document keywords
    for keyword in DOCUMENT_TASK_KEYWORDS:
        if keyword in desc_lower:
            return "document"

    # Check for code keywords
    for keyword in CODE_TASK_KEYWORDS:
        if keyword in desc_lower:
            return "code"

    return "other"


def _extract_file_paths_from_output(output: str) -> list[str]:
    """
    Extract file paths mentioned in Claude's output.

    Looks for patterns like:
    - "wrote to .company/business/file.md"
    - "created file: path/to/file.py"
    - "saved to ./docs/readme.md"
    - file paths in backticks: `path/to/file.md`
    """
    import re as _re

    paths = []

    # Pattern: "wrote/created/saved to <path>"
    action_patterns = [
        r"(?:wrote|created|saved|generated|wrote to|saved to|created file[:]?)\s+[`'\"]?([^\s`'\"]+\.[a-z]{1,5})",
        r"(?:file|document|output)[:]?\s+[`'\"]?([^\s`'\"]+\.[a-z]{1,5})",
    ]

    for pattern in action_patterns:
        matches = _re.findall(pattern, output, _re.IGNORECASE)
        paths.extend(matches)

    # Pattern: file paths in backticks that look like real paths
    backtick_matches = _re.findall(r"`([^`]+\.[a-z]{1,5})`", output, _re.IGNORECASE)
    for match in backtick_matches:
        # Filter out likely code snippets vs file paths
        if "/" in match or match.startswith("."):
            paths.append(match)

    # Deduplicate while preserving order
    seen = set()
    unique_paths = []
    for p in paths:
        # Clean up path
        p = p.strip("'\"`,;:)(")
        if p and p not in seen:
            seen.add(p)
            unique_paths.append(p)

    return unique_paths


def _check_git_diff(project_root: Path) -> bool:
    """Check if there are any uncommitted changes (staged, unstaged, or untracked).

    WS-122 FIX: Use git status --porcelain to also detect UNTRACKED new files.
    The previous git diff approach missed new files like README.md that weren't
    yet staged, causing document tasks to fail deliverable verification even
    when the file was created successfully.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
        )
        # Any output means there are changes (modified, staged, or untracked)
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True  # Can't verify — assume OK


def _verify_deliverable(
    task: dict, output: str, project_root: Path
) -> tuple[bool, str]:
    """
    P52: Verify task actually produced deliverables.

    For document tasks: verify mentioned files exist on disk.
    For code tasks: verify git diff shows changes.

    Args:
        task: Task dictionary with description
        output: stdout from Claude execution
        project_root: Project root directory

    Returns:
        Tuple of (success: bool, reason: str)
    """
    description = task.get("description", task.get("title", ""))
    task_type = _detect_task_type(description)

    if task_type == "document":
        # WS-122 FIX: Extract file paths from BOTH output AND task description.
        # AGENT mode produces minimal output, so paths mentioned in output may be empty.
        # But the task description often names the target file explicitly.
        mentioned_paths = _extract_file_paths_from_output(output)

        # Also extract from task description (catches "Create README.md", etc.)
        description_paths = _extract_file_paths_from_output(description)
        all_paths = list(set(mentioned_paths + description_paths))

        if not all_paths:
            # No files mentioned anywhere — check if output describes intent without action
            output_lower = output.lower()
            intent_phrases = [
                "i would create",
                "i will create",
                "should create",
                "need to create",
                "would write",
                "plan to",
            ]
            if any(phrase in output_lower for phrase in intent_phrases):
                return False, "Output describes intent but no file was created"

            # WS-122: As fallback, check git status for ANY changes
            # This catches cases where the file was created but not mentioned
            if _check_git_diff(project_root):
                return (
                    True,
                    "Git status shows changes (file paths not parsed from output)",
                )

            # No paths found and no intent phrases — might be other type of task
            return True, "No file paths detected (may not be document task)"

        # Verify at least one mentioned file exists
        for rel_path in all_paths:
            # Handle relative paths
            if rel_path.startswith("./"):
                rel_path = rel_path[2:]
            elif rel_path.startswith("/"):
                continue  # Skip absolute paths for safety

            full_path = project_root / rel_path
            if full_path.exists():
                return True, f"Verified file exists: {rel_path}"

        # WS-122: Before failing, check git status as final fallback
        # The file might exist under a different name or path
        if _check_git_diff(project_root):
            return (
                True,
                "Git status shows changes (expected files not found but other changes exist)",
            )

        return False, f"No mentioned files exist on disk: {all_paths}"

    elif task_type == "code":
        # For code tasks, verify git shows changes
        if _check_git_diff(project_root):
            return True, "Git diff shows changes"

        # No git changes — check if output claims success without changes
        output_lower = output.lower()
        if "no changes needed" in output_lower or "already" in output_lower:
            return True, "Task determined no changes needed"

        return False, "Code task but no git changes detected"

    else:
        # Other task types — no specific verification
        return True, "No specific deliverable verification for this task type"


def _detect_session_limit_in_output(text: str) -> tuple[bool, str | None]:
    """Scan combined stdout+stderr for Claude session/usage-limit messages.

    Delegates to session_limit_guard.detect_session_limit.  Wrapped here so
    callers in this module never have to import the guard directly.

    Returns:
        (found, raw_line) — raw_line is the first matching line, or None.
    """
    try:
        try:
            from . import session_limit_guard as _slg
        except ImportError:
            import session_limit_guard as _slg  # type: ignore[no-redef]
        return _slg.detect_session_limit(text)
    except Exception:
        return False, None


def _pause_session_limit(
    raw_line: str | None,
    project_root: Path,
    task_id: str | None = None,
) -> None:
    """Persist a session-limit pause record via session_limit_guard.

    Best-effort: never raises (a logging failure must not block result handling).

    Args:
        raw_line: First matching line from _detect_session_limit_in_output.
        project_root: Repo root; company_dir is inferred as project_root/.company.
        task_id: Optional task ID for richer log context.
    """
    try:
        try:
            from . import session_limit_guard as _slg
        except ImportError:
            import session_limit_guard as _slg  # type: ignore[no-redef]

        company_dir = project_root / ".company"
        _slg.set_session_limit_pause(raw_line=raw_line, company_dir=company_dir)
        print(
            f"[session_limit_guard] Activation pause set for task={task_id} "
            f"raw={raw_line!r:.80}",
            file=sys.stderr,
        )
    except Exception as _exc:
        # Never let guard errors surface as task errors
        print(
            f"[session_limit_guard] WARNING: failed to persist pause: {_exc}",
            file=sys.stderr,
        )


def _worktree_still_present(path: str) -> bool:
    """True if ``path`` still looks like a live git worktree.

    2026-08-30: the daemon's worktree GC removed a worker's active worktree
    mid-session; the worker had no way to notice and kept running for 30
    more minutes in an empty directory, unable to commit, branch, or open a
    PR. A missing directory or missing ``.git`` is exactly what that removal
    leaves behind, and both are cheap to check every poll (no subprocess).
    """
    try:
        return os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def _record_worker_pid(
    task_id: str, pid: int, worktree: str, pgid: int | None = None
) -> None:
    """Record a spawned worker's claude PID for cross-generation cleanup.

    A daemon restart kills its worker THREADS but not the claude subprocesses
    they spawned. The new daemon reads this registry
    (.company/state/worker_pids.json) and kills surviving workers before
    re-executing their tasks — otherwise two generations of workers race on
    the same task and destroy each other's uncommitted work (2026-07-06).

    ``pgid`` is the session's process group (the session is launched as its
    own group leader, so it equals ``pid``). ``pid`` is ``uv``; the claude
    session is its child, so a killer that signals only ``pid`` leaves claude
    running (ENGINE-FINDINGS 2026-09-20, finding 2). Readers kill the group
    when ``pgid`` is present and fall back to the pid for older entries.

    Best-effort: never raises; a lost update just means a skipped kill.
    Entries older than 24h are dropped because PIDs recycle.
    """
    try:
        state_dir = get_company_dir() / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        pid_file = state_dir / "worker_pids.json"
        records = {}
        if pid_file.exists():
            try:
                with open(pid_file, encoding="utf-8") as f:
                    records = json.load(f) or {}
            except Exception:
                records = {}
        record: dict[str, Any] = {
            "pid": int(pid),
            "worktree": worktree,
            "started_at": time.time(),
        }
        if isinstance(pgid, int) and pgid > 1:
            record["pgid"] = pgid
        records[str(task_id)] = record
        cutoff = time.time() - 86400
        records = {
            k: v
            for k, v in records.items()
            if isinstance(v, dict) and v.get("started_at", 0) > cutoff
        }
        fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".wpids_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            os.replace(tmp, pid_file)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception:
        pass


# Seconds a worker session's group gets to exit on SIGTERM before SIGKILL.
WORKER_KILL_GRACE_SECONDS = 5

# Supervision limits for a streaming worker session (see _supervise_worker).
WORKER_HARD_CAP_SECONDS = 7200
WORKER_STALL_SECONDS = 900
WORKER_STALL_MIN_ELAPSED_SECONDS = 1200
WORKER_POLL_SECONDS = 5


# Spawn gate (E1, challenger round 3). The daemon closes it BEFORE its
# shutdown wait, so a worker thread still before its launch (LLM planning can
# take up to 45 s) cannot start a session after the shutdown's registry kill
# pass: in its own process group that session would outlive the daemon,
# unsupervised, in a worktree the shutdown then removes. The lock makes
# "check gate, Popen, record in the registry" one step: once
# close_worker_spawns() returns, every session is either in the registry
# (and killed by the pass) or will never be started.
_SPAWN_LOCK = threading.Lock()
_SPAWNS_CLOSED = threading.Event()


class WorkerSpawnRefused(RuntimeError):
    """The daemon is shutting down; no new worker session may start."""


def close_worker_spawns() -> None:
    """Refuse every later worker launch (daemon shutdown)."""
    with _SPAWN_LOCK:
        _SPAWNS_CLOSED.set()


def reopen_worker_spawns() -> None:
    """Allow worker launches again (daemon start; tests)."""
    with _SPAWN_LOCK:
        _SPAWNS_CLOSED.clear()


def worker_spawns_closed() -> bool:
    return _SPAWNS_CLOSED.is_set()


def _launch_worker_session(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    task_id: str | None = None,
) -> subprocess.Popen:
    """Start one worker CLI session as the leader of its OWN process group.

    The command is ``uv run claude ...``: the direct child is ``uv`` and the
    claude session is a grandchild. Without a group of its own, every kill
    path reached ``uv`` only and claude survived as an orphan — permissions
    bypassed and, once its worktree was gone, every hook failing open
    (ENGINE-FINDINGS 2026-09-20, finding 2; the team path was fixed in #634).
    ``start_new_session`` makes ``proc.pid`` the group id, which
    ``_kill_worker_group`` and the PID registry rely on.

    Raises ``WorkerSpawnRefused`` once the daemon has closed the spawn gate.
    With ``task_id`` the session is recorded in the PID registry (pid and
    pgid) inside the same locked step as the launch.
    """
    with _SPAWN_LOCK:
        if _SPAWNS_CLOSED.is_set():
            raise WorkerSpawnRefused(
                "daemon shutting down: worker session not launched (spawn gate closed)"
            )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        if task_id is not None:
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None
            # 2026-07-06: register the PID so a restarted daemon can kill
            # this worker if its task gets orphan-released.
            _record_worker_pid(task_id, proc.pid, str(cwd), pgid=pgid)
    return proc


def _forget_worker_pid(task_id: str, pid: int) -> bool:
    """Remove ``task_id``'s PID-registry entry if it still records ``pid``.

    For a caller whose session has ended and whose group is empty (the
    agent-team path). An entry that records another pid — a later launch for
    the same task — is left in place. Runs under the spawn lock, so it cannot
    interleave with a launch's registry write. Best-effort: never raises;
    returns True when the entry was removed.
    """
    with _SPAWN_LOCK:
        try:
            state_dir = get_company_dir() / "state"
            pid_file = state_dir / "worker_pids.json"
            if not pid_file.exists():
                return False
            with open(pid_file, encoding="utf-8") as f:
                records = json.load(f) or {}
            rec = records.get(str(task_id))
            if not isinstance(rec, dict) or rec.get("pid") != int(pid):
                return False
            del records[str(task_id)]
            fd, tmp = tempfile.mkstemp(
                dir=str(state_dir), prefix=".wpids_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(records, f, indent=2)
                os.replace(tmp, pid_file)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return True
        except Exception:
            return False


def _worker_group_id(proc: subprocess.Popen) -> int | None:
    """The group to signal for *proc*, or None when it must not be signalled.

    ``_launch_worker_session`` makes the session a group leader, so the group
    id is ``proc.pid``. It is never taken from ``getpgid``: for a session that
    is somehow not a leader that would be the daemon's own group.
    """
    pgid = proc.pid
    try:
        own_group = os.getpgrp()
    except OSError:
        return None
    if not isinstance(pgid, int) or pgid <= 1 or pgid == own_group:
        return None
    return pgid


def _group_has_members(pgid: int | None) -> bool:
    """Whether group ``pgid`` still has a live member.

    macOS answers EPERM for a group whose only members are zombies; that group
    has nothing left to kill, so any error counts as empty.
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _kill_worker_group(
    proc: subprocess.Popen, grace: float = WORKER_KILL_GRACE_SECONDS
) -> None:
    """End every process in a worker session's group, then reap the leader.

    SIGTERM to the group, then up to ``grace`` seconds for the WHOLE group to
    exit (not just the leader), then SIGKILL to the group for whatever is
    left — a member that ignored SIGTERM, or outlived a leader that did exit,
    is still in the group. A group or process already gone is not an error.

    SIGTERM first is deliberate and must stay first. Claude Code's Bash tool
    runs each shell in a session of its OWN (its pgid is not the group's), so
    a killpg never reaches those shells or what they started; only claude can
    end them, and it does so in its SIGTERM handler. A session that does not
    finish that within ``grace`` leaves those shells behind. This is a known
    limitation, pinned by
    ``test_a_setsid_descendant_survives_when_its_parent_ignores_sigterm``.
    """
    import signal as _signal

    pgid = _worker_group_id(proc)

    def _signal_group(sig: int) -> None:
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass

    _signal_group(_signal.SIGTERM)
    if pgid is None:
        # Only when the group cannot be signalled: the group SIGTERM already
        # reached the leader, and a second SIGTERM can interrupt its handler.
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        leader_done = proc.poll() is not None
        if leader_done and not _group_has_members(pgid):
            break
        time.sleep(0.05)
    _signal_group(_signal.SIGKILL)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass


def _end_group_leftovers(proc: subprocess.Popen, grace: float) -> bool:
    """After the leader exited on its own, end what it left in its group.

    ``uv`` can exit before its claude child (or claude before a background
    child it started); the group then still has live members that nothing
    supervises. Returns True when there were leftovers.
    """
    pgid = _worker_group_id(proc)
    if not _group_has_members(pgid):
        return False
    _kill_worker_group(proc, grace=grace)
    return True


# Exit codes that mean "ended by SIGTERM/SIGKILL": negative when the leader
# itself died of the signal, 128+N when uv/claude caught it and exited.
_SIGNAL_EXIT_CODES = frozenset({-15, -9, 143, 137})


def _killed_outside_supervisor(returncode: int | None) -> bool:
    """A session that exited by SIGTERM/SIGKILL without the supervisor killing
    it was killed from outside — the daemon's force-collect timeout or its
    orphan cleanup (both via the PID registry)."""
    return returncode in _SIGNAL_EXIT_CODES


def _supervise_worker(
    proc: subprocess.Popen,
    wt_path: str,
    log_file,
    *,
    last_activity: list[float],
    output_lines: list[str],
    check_activity=None,
    hard_cap_seconds: float = WORKER_HARD_CAP_SECONDS,
    stall_seconds: float = WORKER_STALL_SECONDS,
    stall_min_elapsed_seconds: float = WORKER_STALL_MIN_ELAPSED_SECONDS,
    poll_seconds: float = WORKER_POLL_SECONDS,
    kill_grace_seconds: float = WORKER_KILL_GRACE_SECONDS,
) -> tuple[str | None, str]:
    """Wait for a streaming worker session, killing its group when it must end.

    Returns ``(kind, detail)``: ``kind`` is None when the session exited on its
    own, else ``"worktree_lost"``, ``"stalled"`` or ``"max_timeout"``. Every
    kill path ends the whole process group (``_kill_worker_group``); so does
    any exception out of the wait, before it propagates.
    """
    start = time.time()
    try:
        while proc.poll() is None:
            elapsed = time.time() - start

            # 2026-08-30: the daemon's worktree GC once removed a worker's
            # active worktree mid-session — the worker kept running for
            # another 30 minutes in an empty directory with no .git, unable
            # to commit/branch/PR and with no way to know it had happened.
            # Check every poll (cheap: no subprocess) so the loss becomes an
            # immediate, clearly-reasoned failure instead of silent wasted
            # work.
            if not _worktree_still_present(wt_path):
                _kill_worker_group(proc, grace=kill_grace_seconds)
                detail = (
                    f"worktree removed out from under the worker at "
                    f"{elapsed:.0f}s elapsed (.git missing at "
                    f"{wt_path}) -- daemon worktree cleanup likely "
                    "reaped it while the worker was still active"
                )
                log_file.write(f"\n[WORKTREE LOST] {detail}\n")
                log_file.flush()
                return "worktree_lost", detail

            # Check worktree activity every 60s
            if check_activity is not None and int(elapsed) % 60 == 0 and elapsed > 60:
                check_activity()

            activity_stall = time.time() - last_activity[0]
            # Kill if no activity for 15 min AND running > 20 min
            if activity_stall > stall_seconds and elapsed > stall_min_elapsed_seconds:
                _kill_worker_group(proc, grace=kill_grace_seconds)
                detail = (
                    f"No worktree activity for {activity_stall:.0f}s — "
                    f"killed after {elapsed:.0f}s"
                )
                log_file.write(f"\n[STALLED] {detail}\n")
                return "stalled", detail

            # Hard safety cap: 2 hours
            if elapsed > hard_cap_seconds:
                _kill_worker_group(proc, grace=kill_grace_seconds)
                detail = f"{hard_cap_seconds:.0f}s hard cap — killed"
                log_file.write(f"\n[MAX TIMEOUT] {detail}\n")
                return "max_timeout", detail

            # Log progress every 2 minutes
            if int(elapsed) % 120 == 0 and int(elapsed) > 0:
                log_file.write(
                    f"[heartbeat] {elapsed:.0f}s elapsed, "
                    f"{len(output_lines)} output lines, "
                    f"activity {activity_stall:.0f}s ago\n"
                )
                log_file.flush()
            time.sleep(poll_seconds)
    except BaseException:
        _kill_worker_group(proc, grace=kill_grace_seconds)
        raise
    # The leader exited on its own. Its group may not have: uv can exit while
    # its claude child keeps running, unsupervised and with bypassed
    # permissions.
    if _end_group_leftovers(proc, grace=kill_grace_seconds):
        log_file.write(
            "\n[GROUP LEFTOVERS] the session leader exited but its group still "
            "had live members — killed\n"
        )
        log_file.flush()
    return None, ""


def execute_with_employee_context(
    task: dict,
    employee_id: str,
    context: EmployeeContext,
    timeout: int = DEFAULT_EXECUTION_TIMEOUT,
    execution_cwd: str | None = None,
) -> ExecutionResult:
    """
    Execute a task using the configured coding-agent provider.

    Uses subprocess to invoke the selected provider with the constructed prompt.

    Args:
        task: Task dictionary
        employee_id: Employee ID executing the task
        context: EmployeeContext with memory and agent definition
        timeout: Execution timeout in seconds

    Returns:
        ExecutionResult with success status, output, and timing
    """
    # Load config and resolve agentic mode
    activation_config = get_activation_config()
    agentic_config = activation_config["agenticMode"]
    complexity = task.get("complexity", task.get("estimated_complexity", "standard"))

    # Inject agentic mode flag for prompt builder
    task["_agentic_mode"] = agentic_config["enabled"]
    prompt = build_execution_prompt(task, context, timeout_seconds=timeout)
    # WS-057-002: Allow override for parallel worktree execution
    project_root = Path(execution_cwd) if execution_cwd else get_project_root()

    # Resolve model: prefer complexity-specific override, fall back to default
    model = activation_config["modelByComplexity"].get(
        complexity, activation_config["model"]
    )

    try:
        provider_spec = agent_providers.resolve_provider(
            load_config(), complexity, fallback_model=model
        )
    except ValueError as exc:
        return ExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=0,
            error=str(exc),
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )

    # Build command — agentic mode adds --allowedTools, --disallowedTools,
    # and --max-budget-usd so the employee can use Read/Write/Edit/Bash
    # NOTE: -p means --print (output only), prompt comes via stdin
    #
    # P71 FIX: --print mode alone CANNOT execute tools! Claude will describe
    # work without doing it. Adding --dangerously-skip-permissions enables
    # tool execution in --print mode. This is safe because:
    # 1. Daemon runs in a controlled environment
    # 2. --allowedTools restricts available tools
    # 3. Hook system provides additional safety layer
    #
    # WS-114: Claude Code 2.1.92 optimizations:
    # - --no-session-persistence: Workers don't need resumable sessions
    # - --effort: Map complexity to effort level for faster simple tasks
    # WS-121: Full AGENT mode for all tasks. No more --print.
    # --print made Claude a text generator that had to be forced to use tools.
    # Without --print, Claude runs as a full agent — naturally reads files,
    # writes code, runs tests, iterates. Like an interactive session.
    # Lesson from wshobson/agents: don't fight Claude's nature.
    # System-level instruction to write files
    system_reinforcement = (
        "IMPORTANT: You MUST use Write or Edit tools to create or modify files. "
        "Text-only responses will be treated as failures. Every task must result "
        "in at least one file written or edited on disk."
    )

    allowed = agentic_config["allowedTools"] if agentic_config["enabled"] else []
    disallowed = agentic_config["disallowedTools"] if agentic_config["enabled"] else []
    cmd = agent_providers.build_command(
        provider_spec,
        project_root=project_root,
        complexity=complexity,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        system_reinforcement=system_reinforcement,
    )

    # WS-099-003: Pass prompt via stdin instead of positional argument
    # When --allowedTools is used, Claude CLI fails to parse positional prompts.
    # Stdin works correctly regardless of other flags.
    if not prompt or not prompt.strip():
        # CRITICAL: Empty prompt will cause Claude CLI to fail
        import sys as _sys

        print(
            f"[WS-099 CRITICAL] Empty prompt! task={task.get('task_id')}",
            file=_sys.stderr,
        )
        return {
            "success": False,
            "exit_code": -1,
            "output": "",
            "error": "Empty prompt generated - cannot execute task",
            "employee_id": employee_id,
        }

    # NOTE: Prompt is now passed via stdin, not as positional arg
    # This fixes the bug where --allowedTools breaks positional prompt parsing

    # Codex has no --append-system-prompt — the reinforcement rides in the
    # prompt instead (no-op for claude providers).
    prompt = agent_providers.prepare_prompt(provider_spec, prompt, system_reinforcement)

    # WS-114 DEBUG: Log command for troubleshooting
    import sys as _sys

    retry_count = task.get("retry_count", 0)
    print(
        f"[WS-121] mode=AGENT, provider={provider_spec.name}, "
        f"complexity={complexity}, retries={retry_count}, "
        f"prompt={len(prompt)}chars",
        file=_sys.stderr,
    )
    print(f"[WS-114 DEBUG] cmd: {' '.join(cmd[:10])}...", file=_sys.stderr)

    start_time = time.time()
    scratch_dir: Path | None = None
    try:
        provider_health = agent_providers.check_provider_health(provider_spec)
        if not provider_health.healthy:
            return ExecutionResult(
                success=False,
                output="",
                exit_code=-1,
                duration_seconds=time.time() - start_time,
                error=f"Provider preflight failed: {provider_health.message}",
                employee_id=employee_id,
                task_id=task.get("task_id"),
            )

        child_env = agent_providers.prepare_environment(
            provider_spec, employee_id=employee_id
        )

        # Per-task scratch directory outside the worktree. Set after the
        # allowlist filter in prepare_environment, so it always reaches the
        # session; TMPDIR also moves the hook's "temp directory" allowance
        # for worker writes to this directory.
        scratch_dir = _create_worker_scratch_dir(
            task.get("task_id", "unknown"), project_root
        )
        if scratch_dir is not None:
            child_env["FORGE_SCRATCH_DIR"] = str(scratch_dir)
            child_env["TMPDIR"] = str(scratch_dir)
            prompt = prompt + _scratch_prompt_section(scratch_dir)

        # WS-095: Stream worker output to log file for real-time visibility
        # Users can watch with: tail -f .company/logs/workers/worker-{task_id}.log
        # Skip streaming in pytest to allow subprocess.run mocking in tests.
        _is_pytest = "pytest" in sys.modules
        task_id = task.get("task_id", "unknown")

        # 2026-07-06 fork-bomb guard: never launch a real claude worker from a
        # test context. Passing the exact primitive lets properly-mocked tests
        # through; unmocked paths raise instead of spawning.
        assert_spawn_allowed(
            "employee_activator.execute_with_employee_context",
            subprocess.run if _is_pytest else subprocess.Popen,
        )

        # Set by the streaming path when the supervisor ended the session.
        _worktree_lost_reason: list[str | None] = [None]
        _forced_kill_reason: list[str | None] = [None]

        if _is_pytest:
            # Test mode: use subprocess.run for mock compatibility
            # WS-099-003: Pass prompt via stdin
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=child_env,
                timeout=timeout,
            )
        else:
            # Production: stream output to log file for real-time visibility
            worker_log_dir = get_company_dir() / "logs" / "workers"
            worker_log_dir.mkdir(parents=True, exist_ok=True)
            worker_log_path = worker_log_dir / f"worker-{task_id}.log"

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            with open(worker_log_path, "w") as log_file:
                log_file.write(f"=== Worker Log: {task_id} ===\n")
                log_file.write(f"Employee: {employee_id}\n")
                log_file.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                log_file.write(
                    f"Task: {task.get('title', task.get('description', ''))[:100]}\n"
                )
                log_file.write(f"Cmd args: {len(cmd)}, Prompt chars: {len(prompt)}\n")
                log_file.write(f"Cmd: {cmd}\n")
                log_file.write("=" * 60 + "\n\n")
                log_file.flush()

                # WS-099-003: Pass prompt via stdin (fixes --allowedTools parsing bug)
                # ENGINE-FINDINGS 2026-09-20 E1: the session is its own
                # process group, and every kill below ends the whole group.
                # Launch + PID-registry record (pid and pgid) in one gated
                # step; raises WorkerSpawnRefused during daemon shutdown.
                proc = _launch_worker_session(
                    cmd, cwd=str(project_root), env=child_env, task_id=task_id
                )
                # Write prompt to stdin immediately.
                # Robustness: if the worker exits before reading its prompt,
                # write/close raises BrokenPipeError/OSError. Kill the orphan
                # so it can't leak, then surface a clear failure rather than
                # letting a half-launched process hang the cycle.
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError) as e:
                    _kill_worker_group(proc)
                    raise RuntimeError(
                        f"worker exited before reading its prompt: {e}"
                    ) from e

                import threading

                # WS-121: Activity-based timeout for AGENT mode.
                # AGENT mode produces no stdout (Claude renders to terminal).
                # Monitor worktree file changes instead. Kill only if truly stuck.
                _last_activity_time = [time.time()]
                _wt_path = str(project_root)

                def stream_output(pipe, chunks, prefix, log_f):
                    """Stream output line by line to log file and collect."""
                    try:
                        for line in iter(pipe.readline, ""):
                            if line:
                                chunks.append(line)
                                log_f.write(f"{prefix}{line}")
                                log_f.flush()
                                _last_activity_time[0] = time.time()
                    except Exception:
                        pass
                    finally:
                        pipe.close()

                stdout_thread = threading.Thread(
                    target=stream_output,
                    args=(proc.stdout, stdout_chunks, "", log_file),
                )
                stderr_thread = threading.Thread(
                    target=stream_output,
                    args=(proc.stderr, stderr_chunks, "[ERR] ", log_file),
                )
                stdout_thread.start()
                stderr_thread.start()

                def _check_worktree_activity():
                    """Check if files were modified in worktree."""
                    try:
                        import subprocess as _sp

                        for _cmd in [
                            ["git", "diff", "--stat"],
                            ["git", "ls-files", "--others", "--exclude-standard"],
                        ]:
                            r = _sp.run(
                                _cmd,
                                capture_output=True,
                                text=True,
                                cwd=_wt_path,
                                timeout=5,
                            )
                            if r.stdout.strip():
                                _last_activity_time[0] = time.time()
                                return True
                    except Exception:
                        pass
                    return False

                start_time = time.time()
                _kill_kind, _kill_detail = _supervise_worker(
                    proc,
                    _wt_path,
                    log_file,
                    last_activity=_last_activity_time,
                    output_lines=stdout_chunks,
                    check_activity=_check_worktree_activity,
                )
                if _kill_kind == "worktree_lost":
                    _worktree_lost_reason[0] = _kill_detail
                elif _kill_kind:
                    _forced_kill_reason[0] = f"{_kill_kind}: {_kill_detail}"
                elif _killed_outside_supervisor(proc.returncode):
                    # The daemon's force-collect / orphan cleanup killed the
                    # group via the PID registry. uv then exits 143, not -9,
                    # and the marker scan must not read that as success.
                    _forced_kill_reason[0] = (
                        f"killed outside the supervisor (exit {proc.returncode})"
                    )

                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)

                log_file.write(f"\n{'=' * 60}\n")
                log_file.write(f"Completed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                log_file.write(f"Exit code: {proc.returncode}\n")

            # Create result object compatible with subprocess.run return
            class ProcessResult:
                def __init__(self, returncode, stdout, stderr):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            result = ProcessResult(
                returncode=proc.returncode,
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
            )

            if _worktree_lost_reason[0]:
                # Fail immediately with the real cause instead of falling
                # through to success/deliverable detection against a
                # directory that no longer exists.
                return ExecutionResult(
                    success=False,
                    output=result.stdout,
                    exit_code=result.returncode,
                    duration_seconds=time.time() - start_time,
                    error=_worktree_lost_reason[0],
                    employee_id=employee_id,
                    task_id=task.get("task_id"),
                )

        duration = time.time() - start_time

        # 2026-07-06 incident: detect Claude session/usage limit in worker output.
        # Scan BOTH stdout and stderr because the CLI may emit the message on either.
        # If detected, persist a pause record so all activation pathways skip new
        # work until the stated reset time (+ buffer) has passed.
        if provider_spec.provider_type == "claude":
            _combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
            _limit_found, _limit_line = _detect_session_limit_in_output(
                _combined_output
            )
            if _limit_found:
                _pause_session_limit(_limit_line, project_root, task.get("task_id"))

        normalized_output = agent_providers.normalize_output(
            provider_spec, result.stdout or ""
        )

        # Use smart success detection based on output content, not just exit code
        success = _detect_task_success(normalized_output, result.returncode)
        # A session the supervisor killed (stall / hard cap) is never a
        # success. Before E1 that followed from the SIGKILL exit code (-9);
        # the group now gets SIGTERM first, and uv may turn that into an
        # ordinary non-zero exit that the marker scan could read as success.
        if _forced_kill_reason[0]:
            success = False

        # For planning-source tasks, verify expected artifacts exist on disk.
        # Artifact check is advisory when success detection already passed —
        # employees may implement in different files than ROADMAP specifies.
        if success and task.get("source") == "planning":
            if not _verify_task_artifacts(task, project_root):
                if result.returncode != 0:
                    success = False
                else:
                    print(
                        f"  WARNING: Artifact verification failed for task "
                        f"{task.get('task_id', 'unknown')} but exit_code=0 "
                        f"and output indicates success — treating as success "
                        f"(employee may have used a different file path)",
                        file=sys.stderr,
                    )

        # P52: Verify deliverables for ALL tasks (not just planning-source).
        # This catches cases where Claude outputs "I would create X" but
        # never actually creates the file.
        #
        # P68 FIX: Make P52 advisory for exit_code=0 tasks. With --print mode
        # (P67), Claude describes work in output but may not write files directly.
        # Only fail if exit_code != 0 AND deliverable check fails.
        #
        # Track the specific reason here (not just success=False) — it used to
        # be printed to stderr and then discarded, so every deliverable failure
        # fell back to the generic "no diagnostic output captured" message below.
        # That generic message trivially matches the `exit_code=0` diagnosis
        # pattern in operation_loop/failure_recovery, which routes to
        # SUCCESS_MISDETECTED -> MARK_COMPLETE — silently phantom-completing
        # tasks that produced zero deliverables.
        _deliverable_fail_reason: str | None = None
        if success:
            deliverable_ok, deliverable_reason = _verify_deliverable(
                task, normalized_output, project_root
            )
            if not deliverable_ok:
                if result.returncode != 0:
                    # Non-zero exit + failed deliverable check = real failure
                    success = False
                    _deliverable_fail_reason = (
                        f"Deliverable verification failed: {deliverable_reason}"
                    )
                    print(
                        f"  P52 FAIL: Deliverable verification failed for task "
                        f"{task.get('task_id', 'unknown')}: {deliverable_reason}",
                        file=sys.stderr,
                    )
                else:
                    # WS-121: No deliverable = failure. Period.
                    # The P68 escape hatch (500+ chars = success) was the #1
                    # cause of phantom completions. In AGENT mode, Claude
                    # should always write files — text-only output is a bug.
                    success = False
                    _deliverable_fail_reason = (
                        f"No deliverable produced: {deliverable_reason}"
                    )
                    print(
                        f"  WS-121 FAIL: No deliverable produced: {deliverable_reason}",
                        file=sys.stderr,
                    )

        # Robustness: when the task failed, surface a concrete diagnostic.
        # A signal-killed worker (negative returncode) and an AGENT-mode
        # worker both routinely have empty stderr, so a bare exit code is
        # impossible to triage in the daemon log. Synthesize a reason.
        if success:
            error_detail = None
        elif _forced_kill_reason[0]:
            error_detail = f"worker session killed ({_forced_kill_reason[0]})"
        elif result.returncode < 0:
            error_detail = (
                result.stderr.strip()
                or f"worker terminated by signal {-result.returncode} "
                "(killed — likely activity-stall or 2h hard-cap)"
            )
        else:
            error_detail = (
                _deliverable_fail_reason
                or result.stderr.strip()
                or "no diagnostic output captured"
            )

        return ExecutionResult(
            success=success,
            output=normalized_output,
            exit_code=result.returncode,
            duration_seconds=duration,
            error=error_detail,
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )

    except WorkerSpawnRefused as e:
        # Daemon shutdown: nothing ran, nothing to recover. The next daemon's
        # orphan sweep returns the task to pending.
        return ExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=time.time() - start_time,
            error=str(e),
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )

    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return ExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            error=f"Execution timed out after {timeout} seconds",
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )

    except FileNotFoundError:
        duration = time.time() - start_time
        return ExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            error=(
                f"{provider_spec.provider_type} CLI not found for provider "
                f"{provider_spec.name!r}"
            ),
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )

    except Exception as e:
        duration = time.time() - start_time
        return ExecutionResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            error=str(e),
            employee_id=employee_id,
            task_id=task.get("task_id"),
        )
    finally:
        _remove_worker_scratch_dir(scratch_dir)


# -----------------------------------------------------------------------------
# Memory Updates
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# WS-069-005: Memory Improvement Helpers
# -----------------------------------------------------------------------------

# Patterns that look like secrets — filter these from learnings
_SECRET_PATTERNS = re.compile(
    r"(?:sk_live_|sk_test_|ghp_|gho_|glpat-|xox[bpsa]-|AKIA[0-9A-Z]"
    r"|AIza[0-9A-Za-z_-]|eyJ[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+)"
)


def _sanitize_markdown_injection(text: str) -> str:
    """Escape markdown headers in interpolated text to prevent section injection."""
    return re.sub(r"(?m)^(#{1,6}\s)", r"\\\1", text)


def _line_may_contain_secret(line: str) -> bool:
    """Check if a line likely contains a secret/credential."""
    return bool(_SECRET_PATTERNS.search(line))


def _extract_learnings(output: str) -> dict:
    """
    Extract structured learnings from Claude task output.

    Returns dict with keys: files_touched, fixes, decisions
    Each value is a list of strings (max 5 per category).
    """
    if not output:
        return {"files_touched": [], "fixes": [], "decisions": []}

    # Extract file paths mentioned (common project patterns)
    file_pattern = re.compile(
        r"(?:^|\s)([\w./-]+\.(?:py|js|ts|tsx|json|md|html|css|yaml|yml|toml|sh))(?:\s|$|:|,)",
        re.MULTILINE,
    )
    files_raw = file_pattern.findall(output)
    # Deduplicate and limit
    files_touched = list(
        dict.fromkeys(f for f in files_raw if "/" in f or f.startswith("."))
    )[:5]

    # Extract fix/solution lines
    fix_pattern = re.compile(
        r"^.*(?:fixed|resolved|solution|root cause|bug was|issue was).*$",
        re.MULTILINE | re.IGNORECASE,
    )
    fixes = []
    for m in fix_pattern.finditer(output):
        line = m.group(0).strip()[:120]
        if not _line_may_contain_secret(line):
            fixes.append(line)
        if len(fixes) >= 5:
            break

    # Extract decision/note lines
    decision_pattern = re.compile(
        r"^.*(?:warning|note:|decided|chose to|approach:|strategy:).*$",
        re.MULTILINE | re.IGNORECASE,
    )
    decisions = []
    for m in decision_pattern.finditer(output):
        line = m.group(0).strip()[:120]
        if not _line_may_contain_secret(line):
            decisions.append(line)
        if len(decisions) >= 5:
            break

    return {
        "files_touched": files_touched,
        "fixes": fixes,
        "decisions": decisions,
    }


def _trim_recent_interactions(content: str, max_entries: int = 10) -> str:
    """Trim Recent Interactions section to max_entries most recent."""
    marker = "## Recent Interactions"
    if marker not in content:
        return content

    parts = content.split(marker, 1)
    before = parts[0]
    after = parts[1]

    # Split entries by ### headers
    entry_pattern = re.compile(r"(?=^### )", re.MULTILINE)
    chunks = entry_pattern.split(after)

    # First chunk is the text between ## header and first ### (usually empty/newline)
    header_text = chunks[0] if chunks else ""
    entries = [c for c in chunks[1:] if c.strip()]

    # Keep only the most recent entries
    kept = entries[:max_entries]

    return before + marker + header_text + "".join("### " + e for e in kept)


def _filter_memory_for_task(
    memory_content: str, task: dict, max_chars: int = 12000
) -> str:
    """
    Filter memory content by relevance to the current task.

    Priority order:
    1. Current Context (always include)
    2. Active Assignments (always include)
    3. Learnings (scored by keyword overlap with task)
    4. Recent Interactions (fill remaining space)
    """
    if not memory_content or len(memory_content) <= max_chars:
        return memory_content

    # Parse into sections
    sections: dict[str, str] = {}
    current_section = "__header__"
    current_lines: list[str] = []

    for line in memory_content.split("\n"):
        if line.startswith("## "):
            sections[current_section] = "\n".join(current_lines)
            current_section = line
            current_lines = [line]
        else:
            current_lines.append(line)
    sections[current_section] = "\n".join(current_lines)

    # Build task keywords for relevance scoring
    task_desc = task.get("description", "") + " " + task.get("title", "")
    task_keywords = set(w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", task_desc))

    # Priority sections (always include)
    priority_keys = ["__header__"]
    scored_keys: list[tuple[str, float]] = []

    for key in sections:
        if key == "__header__":
            continue
        key_lower = key.lower()
        if "current context" in key_lower or "active assignment" in key_lower:
            priority_keys.append(key)
        elif "learning" in key_lower:
            # Score learnings by keyword overlap
            section_words = set(
                w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", sections[key])
            )
            overlap = len(task_keywords & section_words)
            scored_keys.append((key, overlap))
        else:
            scored_keys.append((key, 0))

    # Sort scored sections: learnings with high overlap first, then others
    scored_keys.sort(key=lambda x: -x[1])

    # Build filtered content within budget
    result_parts: list[str] = []
    used = 0

    # Add priority sections first
    for key in priority_keys:
        text = sections.get(key, "")
        if used + len(text) <= max_chars:
            result_parts.append(text)
            used += len(text)

    # Add scored sections by priority
    for key, _score in scored_keys:
        text = sections.get(key, "")
        if used + len(text) <= max_chars:
            result_parts.append(text)
            used += len(text)

    return "\n".join(result_parts)


# A markdown H2 only counts when it starts a line. A bare ``"## "`` substring
# search also matches the tail of a ``"### "`` sub-heading, which silently
# truncated every Learnings section to the empty string and pinned the memory
# hit rate at 0% across every recorded task.
_H2_LEARNINGS_RE = re.compile(r"^## Learnings[^\n]*(?:\n|$)", re.MULTILINE)
_H2_HEADING_RE = re.compile(r"^## ", re.MULTILINE)


def _did_reuse_context(memory_content: str, task: dict) -> bool:
    """
    Determine if employee memory contained relevant context for this task.

    Returns True only if memory has structured learnings with keyword overlap.
    """
    if not memory_content:
        return False

    # Locate the Learnings section by line-anchored H2 heading.
    heading = _H2_LEARNINGS_RE.search(memory_content)
    if not heading:
        return False

    # The section runs from the end of the heading line to the next H2 (or EOF).
    body = memory_content[heading.end() :]
    next_h2 = _H2_HEADING_RE.search(body)
    learnings_text = body[: next_h2.start()] if next_h2 else body

    # Check keyword overlap with task
    task_desc = task.get("description", "") + " " + task.get("title", "")
    task_keywords = set(w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", task_desc))
    learning_words = set(
        w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", learnings_text)
    )

    overlap = len(task_keywords & learning_words)
    return overlap >= 2


def update_employee_memory(
    employee_id: str,
    task: dict,
    result: ExecutionResult,
) -> bool:
    """
    Update employee memory after task execution.

    Appends task execution record to the employee's memory.md file
    under the "Recent Interactions" section.

    Args:
        employee_id: Employee ID
        task: Task dictionary
        result: ExecutionResult from execution

    Returns:
        True if update successful, False otherwise
    """
    config = get_activation_config()
    if not config["updateMemoryAfterTask"]:
        return True

    employee = get_employee_by_id(employee_id)
    if not employee:
        return False

    # Get memory path
    memory_path_str = employee.get("memoryPath", "")
    if memory_path_str:
        memory_path = get_project_root() / memory_path_str
    else:
        memory_path = get_company_dir() / "agents" / employee_id / "memory.md"

    if not memory_path.exists():
        return False

    # Format the new entry
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    task_id = task.get("task_id", "unknown")
    description = task.get("description", task.get("title", "Unknown task"))
    status = "completed" if result.success else "failed"
    duration = round(result.duration_seconds, 1)

    # WS-069-005: Sanitize interpolated fields against markdown injection
    safe_description = _sanitize_markdown_injection(description)

    # WS-069-005: Extract structured learnings from output
    learnings = _extract_learnings(result.output or "")

    new_entry = f"""
### {now} - Task Execution
**Task ID:** {task_id}
**Description:** {safe_description}
**Result:** {status}
**Duration:** {duration}s
"""

    try:
        # Read existing content
        with open(memory_path, encoding="utf-8") as f:
            mem_content = f.read()

        # WS-069-005: Upsert Learnings section with extracted knowledge
        has_learnings = any(learnings.values())
        if has_learnings:
            learning_lines = []
            if learnings["files_touched"]:
                learning_lines.append(
                    f"- **Files:** {', '.join(learnings['files_touched'])}"
                )
            for fix in learnings["fixes"]:
                learning_lines.append(f"- **Fix:** {fix}")
            for dec in learnings["decisions"]:
                learning_lines.append(f"- **Note:** {dec}")
            learning_block = (
                f"\n### {now} \u2014 {safe_description[:60]}\n"
                + "\n".join(learning_lines)
                + "\n"
            )

            learnings_marker = "## Learnings"
            if learnings_marker in mem_content:
                lparts = mem_content.split(learnings_marker, 1)
                lrest = lparts[1]
                llines = lrest.split("\n", 1)
                if len(llines) > 1:
                    mem_content = (
                        lparts[0]
                        + learnings_marker
                        + llines[0]
                        + "\n"
                        + learning_block
                        + llines[1]
                    )
                else:
                    mem_content = (
                        lparts[0] + learnings_marker + "\n" + learning_block + lrest
                    )
            else:
                ri_marker = "## Recent Interactions"
                if ri_marker in mem_content:
                    mem_content = mem_content.replace(
                        ri_marker,
                        learnings_marker + "\n" + learning_block + "\n" + ri_marker,
                    )
                else:
                    mem_content += f"\n\n{learnings_marker}\n{learning_block}"

        # Find the "Recent Interactions" section and append
        marker = "## Recent Interactions"
        if marker in mem_content:
            parts = mem_content.split(marker, 1)
            rest = parts[1]
            lines = rest.split("\n", 1)
            if len(lines) > 1:
                new_content = parts[0] + marker + lines[0] + "\n" + new_entry + lines[1]
            else:
                new_content = parts[0] + marker + "\n" + new_entry + rest
        else:
            new_content = mem_content + f"\n\n{marker}\n{new_entry}"

        # WS-069-005: Trim recent interactions to 10 entries
        new_content = _trim_recent_interactions(new_content, max_entries=10)

        # WS-069-005: Atomic write (tempfile + os.replace)
        dir_path = memory_path.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(new_content)
            os.replace(tmp_path, str(memory_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return True

    except (OSError, IOError):
        return False


def record_task_efficiency(
    employee_id: str,
    task: dict,
    result: ExecutionResult,
) -> bool:
    """
    Record task execution for efficiency tracking.

    Args:
        employee_id: Employee ID
        task: Task dictionary
        result: ExecutionResult from execution

    Returns:
        True if recording successful, False otherwise
    """
    config = get_activation_config()
    if not config["trackEfficiency"]:
        return True

    _ensure_imports()
    if efficiency_tracker is None:
        # Not in the public export: same as tracking switched off.
        return True

    try:
        efficiency_tracker.record_task_execution(
            task_id=task.get("task_id", "unknown"),
            employee_id=employee_id,
            duration_minutes=result.duration_seconds / 60.0,
            complexity=task.get("estimated_complexity", "standard"),
            success=result.success,
            first_pass=True,  # Assumed for now
            retry_count=0,
            escalated=False,
            context_reused=_did_reuse_context(
                getattr(result, "_memory_content", ""), task
            ),  # WS-069-005: Honest hit rate
            pattern_tags=task.get("tags", []),
        )
    except Exception:
        return False

    _record_measured_tokens(employee_id, task, result)
    return True


def _record_measured_tokens(
    employee_id: str,
    task: dict,
    result: ExecutionResult,
) -> None:
    """Record the task's measured token consumption, best effort.

    The provider runs in full agent mode with no ``--output-format``, so usage
    never reaches stdout; it is recovered from the worker's session transcript
    instead. Matching is by task_id, which only resolves for worktree runs --
    the primary checkout's transcript directory also holds interactive
    sessions, and attributing those to a task would be worse than not counting.
    A task must never fail because its tokens could not be counted.
    """
    task_id = task.get("task_id")
    if not task_id:
        return

    try:
        # Retries get a fresh worktree; only read transcripts touched during
        # this attempt so an earlier attempt's tokens are not counted twice.
        window_start = time.time() - max(result.duration_seconds or 0.0, 0.0) - 60.0
        usage = efficiency_tracker.collect_task_token_usage(
            task_id=task_id,
            since=window_start,
        )
        if usage.total_tokens > 0:
            efficiency_tracker.record_task_tokens(
                task_id=task_id,
                employee_id=employee_id,
                usage=usage,
            )
    except Exception:
        return


# -----------------------------------------------------------------------------
# Post-Execution Git Capture (WS-049)
# -----------------------------------------------------------------------------


def _escalate_failed_pr(
    task_id: str,
    employee_id: str,
    branch_name: str,
    error_msg: str,
    project_root: Path,
) -> None:
    """Escalate a failed PR creation to pending_approvals for human rescue."""
    import os
    import tempfile
    import uuid

    company_dir = project_root / ".company"
    pending_path = company_dir / "state/pending_approvals.json"

    try:
        if pending_path.exists():
            with open(pending_path, encoding="utf-8") as f:
                pending = json.load(f)
        else:
            pending = {"proposals": []}
    except (json.JSONDecodeError, OSError):
        pending = {"proposals": []}

    if "proposals" not in pending:
        pending["proposals"] = []

    proposal = {
        "proposal_id": f"pr-rescue-{uuid.uuid4().hex[:12]}",
        "proposal_type": "pr_creation_failure",
        "title": f"Rescue stranded code on branch {branch_name}",
        "description": (
            f"PR creation failed for task {task_id} (employee: {employee_id}). "
            f"Code is committed on branch `{branch_name}` but no PR was created.\n\n"
            f"Error: {error_msg}\n\n"
            f"To rescue: `git push -u origin {branch_name} && "
            f"gh pr create --head {branch_name} --base main`"
        ),
        "rationale": "Daemon code changes must reach main via PR to avoid ghost completions",
        "estimated_effort_minutes": 5,
        "estimated_value": 0.8,
        "roi_score": 2.0,
        "approval_tier": "auto",
        "source_data": {
            "task_id": task_id,
            "employee_id": employee_id,
            "branch_name": branch_name,
            "error": error_msg,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "priority": "high",
    }

    pending["proposals"].append(proposal)

    fd, tmp_path = tempfile.mkstemp(dir=pending_path.parent, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2)
        os.replace(tmp_path, pending_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _pre_push_checks_section(
    py_files: list[str],
    lint_passed: bool,
    lint_issues: list[str],
    test_outcome: str,
    test_count: int = 0,
) -> str:
    """The "Checks before push" table for a git-capture PR body.

    The pre-push lint and test results never block the PR (WS-119 1.8), so the
    PR must show them: without this a failing or absent test run looked
    exactly like a passing one to the person reviewing the PR.
    ``test_outcome`` is one of passed, failed, timeout, none, not_run.
    """
    if not py_files:
        lint_row = "| Lint | Not run (no Python files changed) |"
    elif lint_passed:
        lint_row = "| Lint | Passed |"
    else:
        lint_row = f"| Lint | **Issues** ({len(lint_issues)}) |"
    files = f"{test_count} test file{'s' if test_count != 1 else ''}"
    tests_row = {
        "passed": f"| Tests | Passed ({files}) |",
        "failed": f"| Tests | **FAILED** ({files}) |",
        "timeout": f"| Tests | **Timed out** ({files}) |",
        "none": "| Tests | **None ran**: no test file matches the changed files |",
        "not_run": "| Tests | Not run (lint failed first) |",
    }.get(test_outcome, "| Tests | Not run |")
    return (
        "### Checks before push\n\n"
        "| Check | Result |\n|---|---|\n"
        f"{lint_row}\n{tests_row}\n\n"
        "These checks do not stop the PR. Read a failed or missing result "
        "before you merge."
    )


def _run_pre_push_lint_check(
    py_files: list[str], worktree_dir: str
) -> tuple[bool, list[str]]:
    """Run ruff lint check scoped to the task's changed Python files only.

    Scoping to py_files (not '.') prevents pre-existing violations in unrelated
    files from triggering a false 'Lint errors not auto-fixable' report.
    A re-verify pass after --fix confirms whether remaining issues are genuinely
    unfixable or were all resolved.

    Returns (passed, issues). issues is empty when passed=True.
    """
    if not py_files:
        return True, []

    lint_result = subprocess.run(
        ["uv", "tool", "run", "ruff", "check", "--"] + py_files,
        capture_output=True,
        text=True,
        cwd=worktree_dir,
        timeout=60,
    )
    if lint_result.returncode == 0:
        return True, []

    subprocess.run(
        ["uv", "tool", "run", "ruff", "check", "--fix", "--"] + py_files,
        capture_output=True,
        cwd=worktree_dir,
        timeout=60,
    )

    # Re-verify: confirms all violations in changed files are now clean
    reverify = subprocess.run(
        ["uv", "tool", "run", "ruff", "check", "--"] + py_files,
        capture_output=True,
        text=True,
        cwd=worktree_dir,
        timeout=60,
    )
    if reverify.returncode != 0:
        return False, ["Lint errors not auto-fixable"]

    subprocess.run(
        ["git", "add", "--"] + py_files, capture_output=True, cwd=worktree_dir
    )
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit"],
        capture_output=True,
        cwd=worktree_dir,
        timeout=15,
    )
    return True, []


def _human_protected_violations(files: list[str], config: dict) -> list[str]:
    """Files that match a humanProtected.paths glob (fnmatch, posix paths).

    Layer B of the humanProtected fix (PR 260 review finding): the
    PreToolUse hook (lint_on_edit.py) only fires for Write/Edit tool calls —
    a Bash-tool write (``cat >``, ``sed -i``, ...) never goes through it. This
    is the second, independent check at PR-creation time, so a daemon PR can
    never ship a human-protected change even if the hook was bypassed.
    """
    import fnmatch

    hp = config.get("humanProtected") or {}
    if not hp.get("enabled", False):
        return []

    def _normalize(path: str) -> str:
        return path[2:] if path.startswith("./") else path

    patterns = [_normalize(p) for p in hp.get("paths", [])]
    return [
        f for f in files if any(fnmatch.fnmatch(_normalize(f), p) for p in patterns)
    ]


def _declares_human_protected(path: Path) -> bool:
    """False only for a readable JSON object with no `humanProtected` key.

    Such a root config (`/company-init` writes one holding only
    `modelProfile`) leaves the list to the template; anything unreadable
    still counts, so it fails closed (2026-10-30 review of #772)."""
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return True
    return not (isinstance(config, dict) and "humanProtected" not in config)


def _protected_config_path() -> Path:
    """The config Layer B reads: the root gatesmith.json when it declares
    `humanProtected`, else the installer's `.claude/gatesmith.json`
    template beside it.

    A fresh install has only the template, so Layer B read no list and
    refused nothing, while lint_on_edit already obeyed the template (#763;
    found 2026-10-30)."""
    path = get_config_path()
    template = path.parent / TEMPLATE_CONFIG_FILE
    if _present(path) and (_declares_human_protected(path) or not _present(template)):
        return path
    if _present(template):
        return template
    return path


def _present(path: Path) -> bool:
    """Exists, or is a symlink (even a dangling one): a dangling config must
    fail closed, not read as absent (lint_on_edit uses lexists too)."""
    return path.is_symlink() or path.exists()


def _load_protected_config() -> dict:
    """Layer B's config. The root file goes through load_config(); the
    template, while it governs, also protects itself, as in lint_on_edit:
    otherwise a worker switches the list off by editing it."""
    path = _protected_config_path()
    if path == get_config_path():
        return load_config()
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    hp = config.get("humanProtected") if isinstance(config, dict) else None
    if isinstance(hp, dict) and isinstance(hp.get("paths"), list) and hp["paths"]:
        hp["paths"] = [*hp["paths"], TEMPLATE_CONFIG_FILE]
    return config if isinstance(config, dict) else {}


def _human_protected_shape_ok(config: object) -> bool:
    """The shape lint_on_edit accepts; anything else must fail closed here
    too instead of reading as "nothing protected" (review of #772)."""
    if not isinstance(config, dict):
        return False
    hp = config.get("humanProtected", {})
    if not isinstance(hp, dict):
        return False
    if "enabled" in hp and not isinstance(hp["enabled"], bool):
        return False
    paths = hp.get("paths", [])
    return isinstance(paths, list) and all(isinstance(p, str) for p in paths)


def _config_read_ok() -> bool:
    """Whether Layer B's config (if present) parses cleanly and has the
    expected humanProtected shape.

    A missing file is fine — nothing to protect. A present-but-unreadable
    or malformed file is a failure: load_config() silently swallows that
    into ``{}``, which would make Layer B behave as if humanProtected were
    unconfigured and let a violating PR ship. This check exists so the
    caller can fail closed instead (PR 260 review finding #5).
    """
    path = _protected_config_path()
    if not _present(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            return _human_protected_shape_ok(json.load(f))
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _roadmap_task_id(task: dict) -> str | None:
    """The roadmap/intake id (e.g. ``SCOUT-20260723-1``) this queue task came from.

    ``roadmap_scheduler`` stamps ``{"roadmap_task_id": ...}`` as a JSON note when
    it schedules a task out of ROADMAP.md or an external intake file, but nothing
    downstream ever surfaced it, so the executing PR referenced only the opaque
    queue id. That broke the opportunity scout's backpressure check, which asks
    ``gh pr list --search "<intake id>"`` to decide whether an already-merged
    proposal was ever executed: with no PR carrying the id, every executed intake
    item read as unexecuted, the backlog looked permanently saturated, and the
    scout throttled itself into silence (2026-07-24 and 07-25 produced no run,
    with 8 of 12 recent ids mis-read as unexecuted).

    Returns ``None`` for ordinary tasks, which have no roadmap origin.
    """
    for note in task.get("notes") or []:
        content = note.get("content") if isinstance(note, dict) else None
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("roadmap_task_id"):
            return str(data["roadmap_task_id"])
    return None


def _is_daemon_worktree(project_root: Path) -> bool:
    """True when *project_root* is one of the daemon's per-task worktrees."""
    project_root_str = str(project_root.resolve())
    return (
        "/tmp/forge-worktrees/" in project_root_str
        or "/private/tmp/forge-worktrees/" in project_root_str
    )


def _unquote_git_path(path: str) -> str:
    """Decode a path as `git status --porcelain` prints it.

    Git wraps a path in double quotes and C-escapes it when it holds a quote, a
    backslash, a control character or (with the default core.quotePath) any
    non-ASCII byte: "docs/caf\\303\\251.md". Stripping the quotes alone left the
    escapes in, `git add` failed with "pathspec did not match", and the worker's
    PR was never made.
    """
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        raw = codecs.escape_decode(path[1:-1].encode("utf-8", "surrogateescape"))[0]
        return raw.decode("utf-8", "surrogateescape")
    return path


def _capture_code_changes(
    task: dict, employee_id: str, project_root: Path, wip: bool = False
) -> dict[str, Any]:
    """
    After employee execution, detect code file changes and persist them
    via git branch + commit + draft PR.

    Returns dict with capture result:
      - captured: bool (whether changes were committed)
      - branch: str | None
      - pr_url: str | None
      - files_changed: list[str]
      - error: str | None
      - wip_branch: str | None (wip mode only: the pushed branch)

    WS-049-001: Fixes ghost completion pattern where daemon employee
    work was lost when sessions ended.

    ``wip=True`` is for a session that FAILED (a timeout, most often) with
    changes in its worktree. The retry gets a fresh worktree and the old one,
    with its branch, is deleted — so without this the work is unrecoverable and
    every retry restarts from zero against the same clock. In wip mode the same
    file filter and human-protected check apply, the changes are committed and
    the branch is pushed, and that is all: no rebase, no pre-push run, and NO
    pull request. A failed session's tree can be mid-edit (one was caught with
    its own mutation-check mutant still applied), so it is preserved for the
    operator or a later attempt and handed to neither the judge nor the
    auto-merge loop. ``captured`` stays False; the branch is ``wip_branch``.
    """

    task_id = task.get("task_id", "unknown")
    # The title names the change; a description starts with evidence or a
    # brief and made PR titles such as "feat(daemon): Evidence: three ...".
    task_desc = (task.get("title") or task.get("description") or "daemon task")[:80]
    commit_prefix = "wip(daemon): unfinished session —" if wip else "feat(daemon):"

    result: dict[str, Any] = {
        "captured": False,
        "branch": None,
        "pr_url": None,
        "files_changed": [],
        "error": None,
        "wip_branch": None,
    }

    try:
        # 1. Detect changed files (exclude .company/ state files — they persist via Python)
        # Use git status --porcelain for comprehensive change detection: captures
        # staged modifications, unstaged modifications, and untracked new files in a
        # single pass. The previous two-command approach (git diff --name-only +
        # git ls-files --others) missed staged modifications to pre-existing tracked
        # files, causing tasks that modified an existing file to produce no PR.
        # --untracked-files=all: without it git reports a new folder as "?? scripts/"
        # instead of the files inside it, so lint and tests (which pick ".py" paths
        # from this list) were skipped and the PR listed folders, not files.
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
        if status_result.returncode != 0:
            result["error"] = f"git status failed: {status_result.stderr[:200]}"
            return result

        all_changed = set()
        rename_sources = set()
        deleted_paths = set()
        for line in status_result.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]  # Two-char XY status (index col, worktree col)
            path = line[3:].strip()
            # Renames: "old -> new" — track the source separately (a rename
            # off a human-protected path must not dodge Layer B by hiding
            # behind its destination name) while using the destination as
            # the path that actually gets staged/committed.
            if " -> " in path:
                old_path, path = path.split(" -> ", 1)
                rename_sources.add(_unquote_git_path(old_path.strip()))
            path = _unquote_git_path(path.strip())
            # Deleted files: excluded from the stage list (they no longer
            # exist on disk) but MUST still reach the humanProtected check —
            # the commit below is index-wide, so a staged deletion of a
            # protected file would ship even though it never appears in
            # code_changes (PR 265 review finding).
            if xy[0] == "D" or xy[1] == "D":
                if path:
                    deleted_paths.add(path)
                continue
            if path:
                all_changed.add(path)

        # P73 UPDATE: After .company/ reorganization, state/config/runtime are in subdirs.
        # Exclude infrastructure dirs, include deliverable dirs.
        COMPANY_EXCLUDE_DIRS = {
            ".company/state/",  # Runtime state (JSON files)
            ".company/config/",  # Configuration
            ".company/runtime/",  # Daemon pid/heartbeat/lock
            ".company/daemon_snapshots/",
            ".company/escalations/",
            ".company/analytics/",
            ".company/agents/",  # Agent definitions
            ".company/employees/",  # Employee records
            ".company/assignments/",
            ".company/logs/",
            ".company/schemas/",
            ".company/social/",
            ".company/templates/",
            ".company/archive/",
        }
        COMPANY_EXCLUDE_FILES = {
            ".company/org.json",  # Core org definition
            ".company/vision.md",  # Company vision
        }
        # Deliverable directories that SHOULD be included in PRs
        COMPANY_DELIVERABLE_DIRS = {
            ".company/business/",
            ".company/sales/",
            ".company/research/",
            ".company/knowledge/",
            ".company/reports/",
            ".company/employee-ideas/",
            ".company/compliance/",  # Future compliance subdirectory expansion
        }
        # Explicit compliance deliverable files (legal-compliance-officer output)
        COMPANY_COMPLIANCE_DELIVERABLES = {
            ".company/compliance-report.json",
        }

        def should_include_file(f: str) -> bool:
            # Always exclude .planning/ (session state)
            if f.startswith(".planning/"):
                return False
            # For .company/ files, check if it's a deliverable
            if f.startswith(".company/"):
                # Explicit exclude files
                if f in COMPANY_EXCLUDE_FILES:
                    return False
                # Compliance deliverables — include before exclude-unknown fallback.
                # legal-compliance-officer writes .company/compliance-report.json
                # directly (e.g. when execution_cwd is the venture directory).
                if f in COMPANY_COMPLIANCE_DELIVERABLES:
                    return True
                # Exclude directories (state, config, runtime, infrastructure)
                for exclude_dir in COMPANY_EXCLUDE_DIRS:
                    if f.startswith(exclude_dir):
                        return False
                # Deliverable directories - include
                for deliv_dir in COMPANY_DELIVERABLE_DIRS:
                    if f.startswith(deliv_dir):
                        return True
                # Unknown .company/ file - exclude by default (safer)
                return False
            # All other files - include
            return True

        code_changes = [f for f in all_changed if should_include_file(f)]

        # WS-123 FIX: Filter out gitignored files before git add.
        # git status --porcelain shows ALL changes including untracked files in
        # gitignored directories (e.g. __pycache__/). If we try to git add these,
        # git fails with "paths are ignored by .gitignore". Filter them out.
        if code_changes:
            check_ignore = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                input="\n".join(code_changes),
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=10,
            )
            ignored_paths = set(check_ignore.stdout.strip().splitlines())
            if ignored_paths:
                code_changes = [f for f in code_changes if f not in ignored_paths]

        if not code_changes:
            # No code files changed — this is a state-only task (legitimate)
            return result

        result["files_changed"] = sorted(code_changes)
        result["changes_count"] = len(code_changes)

        # Layer B humanProtected enforcement (PR 260 review finding): refuse
        # to stage/commit/push a PR that touches a human-protected path.
        # load_config() resolves against this process's cwd (the daemon's
        # main checkout), not `project_root` — intentional: a worker cannot
        # defeat this check by editing humanProtected.paths in its own
        # worktree copy of gatesmith.json. rename_sources catches a
        # protected file renamed away under a non-protected name — the
        # source path never appears in code_changes otherwise.
        if not _config_read_ok():
            result["error"] = (
                "Refusing to create PR: gatesmith.json could not be read/"
                "parsed, so human-protected paths cannot be verified — "
                "failing closed"
            )
            return result

        # Checked set = stage list + rename sources + deletions + the branch
        # diff vs origin/main (a worker's own `git commit` inside its
        # worktree would otherwise ship unchecked — PR 265 review finding).
        branch_files: list[str] = []
        try:
            branch_diff = subprocess.run(
                ["git", "diff", "origin/main...HEAD", "--name-only", "--no-renames"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=15,
            )
            if branch_diff.returncode == 0:
                branch_files = [
                    ln.strip() for ln in branch_diff.stdout.splitlines() if ln.strip()
                ]
        except (subprocess.TimeoutExpired, OSError):
            pass

        protected_hits = _human_protected_violations(
            sorted(
                set(code_changes) | rename_sources | deleted_paths | set(branch_files)
            ),
            _load_protected_config(),
        )
        if protected_hits:
            # NOTE: `sys` is shadowed by a local import further down this
            # function — import locally here or the print raises
            # UnboundLocalError before the refusal is returned.
            import sys as _sys

            print(
                f"[humanProtected] REFUSED PR capture for {task_id}: touches "
                + ", ".join(sorted(protected_hits)),
                file=_sys.stderr,
            )
            result["error"] = (
                "Refusing to create PR: touches human-protected path(s): "
                + ", ".join(sorted(protected_hits))
            )
            return result

        # PR-SCRATCH-GUARD (#674): leave scratch-shaped new files out of the
        # stage list, or refuse when an unbriefed root file is added. Not in
        # wip mode — a failed session's branch is preserved as-is for triage.
        if not wip:
            try:
                from . import pr_scratch_guard as _sg
            except ImportError:
                import pr_scratch_guard as _sg  # type: ignore[no-redef]
            if _sg_err := _sg.enforce_on_stage_list(
                project_root,
                task.get("title", ""),
                task.get("description", ""),
                code_changes,
            ):
                result["error"] = _sg_err
                return result
            if not code_changes:
                return result
            result["files_changed"] = sorted(code_changes)
            result["changes_count"] = len(code_changes)

        # WS-103: Detect if already running in a daemon worktree.
        # The daemon creates worktrees at /tmp/forge-worktrees/task-XXXX on branch
        # daemon/wt-XXXX. If we're already there, use it directly instead of
        # creating a nested worktree (which fails or causes issues).
        import shutil
        import sys

        is_daemon_worktree = _is_daemon_worktree(project_root)

        if wip and not is_daemon_worktree:
            # The nested-worktree path below copies files out of the caller's
            # checkout and cleans up after the PR step, which wip mode never
            # reaches. Preserving a failed session is only defined for the
            # daemon's own per-task worktree.
            result["error"] = "wip capture runs only in a daemon worktree"
            return result

        if is_daemon_worktree:
            # WS-103: Already in daemon worktree — use current branch directly
            current_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=5,
            ).stdout.strip()

            if not current_branch or current_branch == "HEAD":
                # Detached HEAD state — create a branch
                current_branch = f"daemon/{task_id}"
                current_branch = re.sub(r"[^a-zA-Z0-9/_-]", "-", current_branch)
                subprocess.run(
                    ["git", "checkout", "-b", current_branch],
                    capture_output=True,
                    cwd=str(project_root),
                    timeout=5,
                )

            result["branch"] = current_branch
            worktree_dir = project_root
            worktree_dir_str = str(project_root)

            # Run ruff --fix on Python files before staging (eliminates I001 CI failures)
            py_files = [f for f in code_changes if f.endswith(".py")]
            if py_files:
                subprocess.run(
                    ["uv", "tool", "run", "ruff", "check", "--fix"] + py_files,
                    capture_output=True,
                    cwd=worktree_dir_str,
                    timeout=30,
                )

            # Stage changed files directly in the existing worktree
            add_result = subprocess.run(
                ["git", "add"] + code_changes,
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=10,
            )
            if add_result.returncode != 0:
                result["error"] = f"git add failed: {add_result.stderr[:200]}"
                return result

            # Commit directly
            safe_desc = re.sub(r"[\n\r\x00-\x1f]", " ", task_desc)[:200]
            commit_msg = f"{commit_prefix} {safe_desc} [{task_id}]"
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=15,
            )
            if commit_result.returncode != 0:
                combined = (commit_result.stdout or "") + (commit_result.stderr or "")
                if "nothing to commit" in combined:
                    return result
                result["error"] = f"git commit failed: {commit_result.stderr[:200]}"
                return result

            # Skip to push/PR (bypass nested worktree creation)
            # Jump to pre-push validation and push
        else:
            # Original path: running in main directory, need worktree isolation
            branch_name = f"daemon/{task_id}"
            # Sanitize branch name (remove invalid chars)
            branch_name = re.sub(r"[^a-zA-Z0-9/_-]", "-", branch_name)

            # Create the branch without switching (stays on user's branch)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=5,
            ).stdout.strip()

            # Create branch pointing at current HEAD (no checkout)
            create_branch = subprocess.run(
                ["git", "branch", branch_name, head_sha],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=5,
            )
            if create_branch.returncode != 0:
                # Branch may already exist — reset it to HEAD
                subprocess.run(
                    ["git", "branch", "-f", branch_name, head_sha],
                    capture_output=True,
                    text=True,
                    cwd=str(project_root),
                    timeout=5,
                )

            result["branch"] = branch_name

            # Create a temporary worktree for this branch
            worktree_dir = project_root / ".worktrees" / f"daemon-{task_id[:20]}"
            worktree_dir_str = str(worktree_dir)

            # Clean up stale worktree if it exists
            if worktree_dir.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_dir_str],
                    capture_output=True,
                    cwd=str(project_root),
                    timeout=10,
                )

            wt_result = subprocess.run(
                ["git", "worktree", "add", worktree_dir_str, branch_name],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=15,
            )
            if wt_result.returncode != 0:
                result["error"] = (
                    f"Failed to create worktree for {branch_name}: {wt_result.stderr[:200]}"
                )
                return result

            # 3. Copy changed files into the worktree
            for f in code_changes:
                src = project_root / f
                dst = worktree_dir / f
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))

            # 4. Stage and commit in the worktree (user's dir untouched)
            # Run ruff --fix on Python files before staging (eliminates I001 CI failures)
            py_files = [f for f in code_changes if f.endswith(".py")]
            if py_files:
                subprocess.run(
                    ["uv", "tool", "run", "ruff", "check", "--fix"] + py_files,
                    capture_output=True,
                    cwd=worktree_dir_str,
                    timeout=30,
                )

            add_result = subprocess.run(
                ["git", "add"] + code_changes,
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=10,
            )
            if add_result.returncode != 0:
                result["error"] = f"git add failed: {add_result.stderr[:200]}"
                return result

            # Sanitize task description for safe use in commit message
            safe_desc = re.sub(r"[\n\r\x00-\x1f]", " ", task_desc)[:200]
            commit_msg = f"feat(daemon): {safe_desc} [{task_id}]"
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=15,
            )
            if commit_result.returncode != 0:
                combined = (commit_result.stdout or "") + (commit_result.stderr or "")
                if "nothing to commit" in combined:
                    return result
                result["error"] = f"git commit failed: {commit_result.stderr[:200]}"
                return result

        # WS-103: Both paths (daemon worktree and nested worktree) continue here
        # with pre-push validation, push, and PR creation
        # Get branch name for push (set earlier in both paths)
        branch_name = result.get("branch", f"daemon/{task_id}")
        safe_desc = re.sub(r"[\n\r\x00-\x1f]", " ", task_desc)[:200]

        if wip:
            # Preserve, publish nothing: push the branch and stop. The retry's
            # cleanup deletes the local branch with the worktree; the pushed one
            # is what survives. One attempt, short timeout — this runs on the
            # failure path and must not hold the worker slot.
            try:
                _wip_push = subprocess.run(
                    ["git", "push", "-u", "origin", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=worktree_dir_str,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                result["error"] = "wip push timed out"
                return result
            if _wip_push.returncode != 0:
                result["error"] = f"wip push failed: {_wip_push.stderr[:200]}"
                return result
            result["wip_branch"] = branch_name
            print(
                f"  [WS-049] Session failed with {len(code_changes)} changed file(s) "
                f"— preserved on origin/{branch_name} (no PR)",
                file=sys.stderr,
            )
            return result

        # Phase2-T2.2: Rebase onto origin/main before PR creation.
        # Worktrees branch from main at spawn time; any merge landing mid-build
        # guarantees a stale PR base. One attempt: if it fails, abort and continue
        # with the un-rebased branch so the PR surfaces DIRTY to the auto-merge loop's
        # terminal paths (PATH B close + T0.6 task-level escalation counter).
        _fetch_result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=worktree_dir_str,
            timeout=60,
        )
        if _fetch_result.returncode == 0:
            _rebase_result = subprocess.run(
                ["git", "rebase", "origin/main"],
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=60,
            )
            if _rebase_result.returncode != 0:
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    capture_output=True,
                    cwd=worktree_dir_str,
                    timeout=10,
                )
                result["conflict"] = True
                print(
                    f"  [Phase2-T2.2] Rebase conflict on {branch_name} — "
                    "PR will be DIRTY; auto-merge terminal paths will handle it",
                    file=sys.stderr,
                )
        else:
            print(
                f"  [Phase2-T2.2] git fetch failed (non-blocking): "
                f"{_fetch_result.stderr[:100]}",
                file=sys.stderr,
            )

        # Pre-push validation (WS-097): Run CI checks locally to prevent stuck PRs
        pre_push_passed = True
        pre_push_issues: list[str] = []

        # Format check with auto-fix (scoped to changed .py files only)
        if py_files:
            fmt_result = subprocess.run(
                ["uv", "tool", "run", "ruff", "format", "--check"] + py_files,
                capture_output=True,
                text=True,
                cwd=worktree_dir_str,
                timeout=60,
            )
            if fmt_result.returncode != 0:
                subprocess.run(
                    ["uv", "tool", "run", "ruff", "format"] + py_files,
                    capture_output=True,
                    cwd=worktree_dir_str,
                    timeout=60,
                )
                subprocess.run(
                    ["git", "add"] + py_files,
                    capture_output=True,
                    cwd=worktree_dir_str,
                )
                subprocess.run(
                    ["git", "commit", "--amend", "--no-edit"],
                    capture_output=True,
                    cwd=worktree_dir_str,
                    timeout=15,
                )

        # Lint check with auto-fix (scoped to changed .py files only)
        lint_passed, lint_issues = _run_pre_push_lint_check(py_files, worktree_dir_str)
        if not lint_passed:
            pre_push_passed = False
            pre_push_issues.extend(lint_issues)

        # Quick test check (skip if lint failed)
        # WS-105: Only run tests on files related to the current task, not the entire suite.
        # Running all tests causes unrelated pre-existing failures to block new work.
        test_outcome = "not_run"
        test_files_to_run: list[str] = []
        if pre_push_passed and code_changes:
            test_outcome = "none"
            # Find test files that correspond to changed files
            test_files_to_run = []
            for f in code_changes:
                # If it's already a test file, add it
                if f.startswith("tests/") and f.endswith(".py"):
                    test_files_to_run.append(f)
                # Otherwise, look for corresponding test file
                elif f.endswith(".py"):
                    basename = Path(f).stem
                    potential_test = f"tests/test_{basename}.py"
                    test_path = Path(worktree_dir_str) / potential_test
                    if test_path.exists():
                        test_files_to_run.append(potential_test)

            # Only run tests if we found relevant test files
            if test_files_to_run:
                # A TimeoutExpired here must be caught locally, not left to
                # bubble to the outer except (which turns it into a hard
                # "Git capture timed out" failure and discards otherwise-
                # complete work — see ca8cca, where 5 changed test files
                # aggregated past the 300s budget). Same non-blocking
                # treatment as a test *failure* below: report and move on
                # to push/PR creation; GitHub CI still catches real
                # regressions.
                try:
                    test_result = subprocess.run(
                        ["uv", "run", "pytest"]
                        + test_files_to_run
                        + ["-x", "--tb=no", "-q"],
                        capture_output=True,
                        text=True,
                        cwd=worktree_dir_str,
                        timeout=300,
                    )
                    if test_result.returncode != 0:
                        pre_push_passed = False
                        pre_push_issues.append("Tests failed")
                        test_outcome = "failed"
                    else:
                        test_outcome = "passed"
                except subprocess.TimeoutExpired:
                    pre_push_passed = False
                    pre_push_issues.append("Tests timed out")
                    test_outcome = "timeout"

        # WS-119 1.8: Don't abort PR creation on pre-push failures.
        # Test/lint failures in worktrees are often pre-existing issues unrelated
        # to the task's changes. GitHub CI will catch real problems. Blocking here
        # causes phantom completions (work done but never persisted as PR).
        if not pre_push_passed:
            result["pre_push_issues"] = pre_push_issues
            print(
                f"  [WS-119 1.8] Pre-push issues (non-blocking): {'; '.join(pre_push_issues)}",
                file=sys.stderr,
            )

        # 5. Push branch from worktree (retry once; 120s timeout per attempt —
        # 30s was too short when auth prompts or slow network caused timeouts)
        _push_max = 2
        _push_error = ""
        for _push_attempt in range(1, _push_max + 1):
            try:
                push_result = subprocess.run(
                    ["git", "push", "-u", "origin", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=worktree_dir_str,
                    timeout=120,
                )
                if push_result.returncode == 0:
                    _push_error = ""
                    break
                _push_error = f"git push failed: {push_result.stderr[:200]}"
            except subprocess.TimeoutExpired:
                _push_error = f"git push timed out (attempt {_push_attempt})"
            if _push_attempt < _push_max:
                import time as _time_mod

                _time_mod.sleep(5)
        if _push_error:
            result["error"] = _push_error
            return result

        # 6. Create draft PR (with retry + escalation)
        pr_title = f"feat(daemon): {safe_desc} [{task_id}]"
        # Roadmap id is emitted WITHOUT backticks: it exists to be found by
        # `gh pr list --search "<id>"`, and plain text is what search reliably
        # tokenises (see _roadmap_task_id).
        roadmap_id = _roadmap_task_id(task)
        roadmap_line = f"**Roadmap task:** {roadmap_id}\n" if roadmap_id else ""
        pr_body = (
            f"## Daemon Auto-PR\n\n"
            f"**Task:** `{task_id}`\n"
            f"{roadmap_line}"
            f"**Employee:** `{employee_id}`\n"
            f"**Files changed:** {len(code_changes)}\n\n"
            f"### Changed Files\n"
            + "\n".join(f"- `{f}`" for f in code_changes)
            + "\n\n"
            + _pre_push_checks_section(
                py_files,
                lint_passed,
                lint_issues,
                test_outcome,
                len(test_files_to_run),
            )
            + "\n\n---\n*Auto-generated by daemon post-execution capture (WS-049)*"
        )

        # The PR opens held (verdict-pending) until the deliverable judge
        # passes its head — atomic with the create, so no merge lever (ci.yml's
        # Auto-Merge job reads only labels) can act in the judge-vs-CI window.
        try:
            try:
                from . import verdict_hold as _vh
            except ImportError:
                import verdict_hold as _vh  # type: ignore[no-redef]
            _hold_args = _vh.create_args(cwd=worktree_dir_str)
        except Exception as _vh_err:  # noqa: BLE001 — ci.yml's hold job still labels
            print(f"  [verdict-hold] label setup failed: {_vh_err}", file=sys.stderr)
            _hold_args = []
        # CHALLENGER-STAGE: a product-goal PR also opens held by the challenger
        # (challenger:pending), so no merge lever acts before its review.
        try:
            try:
                from . import challenger_stage as _cs
            except ImportError:
                import challenger_stage as _cs  # type: ignore[no-redef]
            _hold_args = _hold_args + _cs.pr_create_label_args(task, worktree_dir_str)
        except Exception:  # noqa: BLE001 — the stage's tick labels the PR too
            pass

        max_pr_retries = 3
        pr_created = False
        last_pr_error = ""
        for attempt in range(1, max_pr_retries + 1):
            try:
                pr_result = subprocess.run(
                    ["gh", "pr", "create", "--title", pr_title, "--body", pr_body]
                    + _hold_args,
                    capture_output=True,
                    text=True,
                    cwd=worktree_dir_str,
                    timeout=30,
                )
                if pr_result.returncode == 0:
                    result["pr_url"] = pr_result.stdout.strip()
                    result["captured"] = True
                    pr_created = True
                    # NOTE: auto-merge is deliberately NOT enabled here anymore.
                    # Enabling GitHub native `gh pr merge --auto` at PR creation —
                    # BEFORE the pre-merge deliverable gate runs — bypassed the gate
                    # entirely (native auto-merge merges on green CI and ignores the
                    # needs-manual-review label). Auto-merge is now armed only AFTER
                    # a passing deliverable verdict, centralised in
                    # pr_output_manager.run_deliverable_gate (which the operation
                    # loop invokes on this git-capture PR). That makes the gate
                    # load-bearing for the allowMergeToMain flip.
                    break
                last_pr_error = (
                    pr_result.stderr.strip()
                    if pr_result.stderr
                    else f"gh pr create failed (exit {pr_result.returncode})"
                )
            except subprocess.TimeoutExpired:
                last_pr_error = f"gh pr create timed out (attempt {attempt})"
            if attempt < max_pr_retries:
                import time

                time.sleep(2 * attempt)

        if not pr_created:
            result["captured"] = False
            result["pr_error"] = last_pr_error
            print(
                f"WARNING: PR creation failed after {max_pr_retries} attempts "
                f"for branch {branch_name}: {last_pr_error}",
                file=sys.stderr,
            )
            _escalate_failed_pr(
                task_id, employee_id, branch_name, last_pr_error, project_root
            )

        print(
            f"  [WS-103] Captured {len(code_changes)} code changes "
            f"→ branch {branch_name}"
            + (f" → PR {result['pr_url']}" if result["pr_url"] else ""),
            file=sys.stderr,
        )

        # WS-103: Only clean up worktree if we created a nested one (not if using daemon worktree)
        if not is_daemon_worktree:
            # Clean up the nested worktree — user's working dir is untouched
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_dir_str],
                capture_output=True,
                cwd=str(project_root),
                timeout=10,
            )
            # WS-057-005: Clean up local branch ref after worktree removal.
            # The branch exists on origin after successful push, or is orphaned
            # after push failure. Either way, the local ref is no longer needed.
            if branch_name:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=str(project_root),
                    timeout=5,
                )

    except subprocess.TimeoutExpired as _te:
        # git push has its own try/except with retry; if we land here it's a
        # different subprocess (git add, commit, rebase, worktree setup, etc.)
        result["error"] = f"Git capture timed out (non-push op): {_te}"
    except Exception as e:
        result["error"] = f"Git capture error: {str(e)[:200]}"

    return result


# -----------------------------------------------------------------------------
# Worker Self-Isolation Helpers (WS-115)
# -----------------------------------------------------------------------------


def _create_worker_worktree(task: dict) -> tuple[Path, str] | None:
    """Create a throwaway git worktree for isolated worker execution.

    When no worktree is supplied by the daemon (direct-execution fallback or
    recovery re-execution), self-isolate by creating a temporary worktree off
    main so the worker never runs in the main repo root.  Mirrors
    team_executor._create_team_worktree (WS-115 pattern).

    Returns (worktree_path, branch_name) or None on failure; the caller then
    refuses rather than running in the main tree (WS-092).
    """
    try:
        import uuid as _uuid

        task_id = str(task.get("task_id", "worker"))
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", task_id)
        suffix = _uuid.uuid4().hex[:6]
        # Per-project base — see company_resolver.get_worktree_base()
        wt_base = company_resolver.get_worktree_base()
        wt_dir = wt_base / f"{safe_id}-{suffix}"
        wt_branch = f"daemon/wt-{safe_id[-16:]}-{suffix}"
        os.makedirs(wt_base, mode=0o700, exist_ok=True)
        # Security: base must not be a symlink (TOCTOU — WS-057-003
        # CRITICAL-2) and the resolved path must stay under the base.
        if not company_resolver.validate_worktree_base(wt_base, wt_dir):
            return None
        project_root_str = str(get_project_root())
        # Clear any stale branch ref from a prior run, then add the worktree.
        subprocess.run(
            ["git", "branch", "-D", wt_branch],
            capture_output=True,
            timeout=15,
            cwd=project_root_str,
        )
        # origin/main, not local main — see company_resolver.preferred_base_ref.
        base_ref = company_resolver.preferred_base_ref(project_root_str)
        # --no-track: a remote-tracking start point would otherwise set upstream
        # and write the shared .git/config (config.lock contention with a
        # concurrent worker's push -u).
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
            cwd=project_root_str,
        )
        if res.returncode != 0:
            subprocess.run(
                ["git", "branch", "-D", wt_branch],
                capture_output=True,
                timeout=15,
                cwd=project_root_str,
            )
            return None
        return wt_dir, wt_branch
    except (subprocess.TimeoutExpired, OSError):
        return None


def _remove_worker_worktree(worktree_path: Path) -> None:
    """Best-effort removal of a self-created worker worktree (WS-115)."""
    for cmd in (
        ["git", "worktree", "remove", str(worktree_path), "--force"],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            pass


# -----------------------------------------------------------------------------
# High-Level Activation Flow
# -----------------------------------------------------------------------------


def activate_employee_for_task(
    task: dict,
    fallback_agent_id: str | None = None,
    execution_cwd: str | None = None,
) -> dict:
    """
    Full employee activation flow for a task.

    This is the main entry point for the activation layer:
    1. Detect task complexity (P20: GSD/BMAD integration)
    2. Route to planning if complexity >= threshold (when enabled)
    3. Match best employee for task
    4. Load employee context
    5. Execute with context
    6. Update memory
    7. Record efficiency

    Args:
        task: Task dictionary
        fallback_agent_id: Deprecated - no longer used. Kept for API compatibility.
            Employee matching now always falls back to a default employee rather
            than using a daemon process ID.

    Returns:
        Dict with activation result including:
        - success: bool
        - reason: str
        - message: str
        - complexity: str (detected complexity level)
        - used_planning: bool (whether GSD/BMAD planning was used)
    """
    config = get_activation_config()
    daemon_config = get_daemon_config()

    if not config["enabled"]:
        return {
            "success": False,
            "reason": "employee_activation_disabled",
            "message": "Employee activation is disabled in config",
        }

    # Step 0: Detect task complexity (P20: GSD/BMAD Unification)
    # This determines whether to use direct execution or planning-based routing
    #
    # P28: Prefer orchestrator's execution_plan if available (set by forge_daemon)
    # This avoids duplicate complexity detection and uses richer analysis.
    detected_complexity = task.get("estimated_complexity", None)
    complexity_info = None
    used_planning = False
    complexity_source = "preset"  # Track where complexity came from

    # P28: Check for execution_plan from central orchestrator
    execution_plan = task.get("execution_plan")
    if execution_plan and isinstance(execution_plan, dict):
        plan_complexity = execution_plan.get("complexity", {})
        if isinstance(plan_complexity, dict) and plan_complexity.get("level"):
            detected_complexity = plan_complexity["level"]
            complexity_info = plan_complexity
            complexity_source = "orchestrator"
            print(
                f"  [P28] Using orchestrator complexity: {detected_complexity}",
                file=sys.stderr,
            )

    if not detected_complexity:
        # Fallback: Detect complexity from task description
        _ensure_complexity_detector()
        if complexity_detector is not None:
            try:
                task_desc = task.get("description", "") or task.get("title", "")
                complexity_info = complexity_detector.detect_complexity(task_desc)
                detected_complexity = complexity_info.get("level", "standard")
                complexity_source = "detector"
                print(
                    f"  [P28] Using complexity_detector: {detected_complexity}",
                    file=sys.stderr,
                )
            except Exception:
                # Fallback if detection fails
                detected_complexity = "standard"
                complexity_source = "default"
        else:
            detected_complexity = "standard"
            complexity_source = "default"

    # Store complexity in task metadata for downstream use
    task["estimated_complexity"] = detected_complexity
    task["_complexity_source"] = complexity_source
    if complexity_info:
        task["_complexity_info"] = complexity_info

    # Step 0.5: Route to planning if GSD/BMAD planning is enabled and complexity
    # meets threshold. This is for P20 integration.
    if daemon_config.get("useGsdBmadPlanning", False):
        threshold = daemon_config.get("complexityThreshold", "standard")
        if _complexity_requires_planning(detected_complexity, threshold):
            # Check if task_planner is available
            _ensure_task_planner()
            if task_planner is not None:
                try:
                    # Invoke planning layer before execution
                    plan_result = task_planner.plan_task(task)
                    if plan_result.get("success"):
                        used_planning = True
                        # Planning may have enriched the task with subtasks or details
                        if "enriched_task" in plan_result:
                            task.update(plan_result["enriched_task"])
                except Exception as e:
                    # Planning failed - fall back to direct execution
                    # Log but don't fail the task
                    print(
                        f"  WARNING: GSD/BMAD planning failed for task "
                        f"{task.get('task_id', 'unknown')}: {e}. "
                        f"Falling back to direct execution.",
                        file=sys.stderr,
                    )

    # Step 1: Match employee
    # match_employee_for_task now includes fallback to default employee,
    # so it should only return None if there are no employees at all
    employee_id = match_employee_for_task(task)

    if not employee_id:
        # No employees available - cannot proceed
        return {
            "success": False,
            "reason": "no_employees_available",
            "message": "No employees available in the organization. Hire employees with /company-hire.",
            "required_capabilities": task.get("required_capabilities", []),
            "complexity": detected_complexity,
            "used_planning": used_planning,
        }

    # Step 2: Load context
    context = load_employee_context(employee_id)
    if not context:
        return {
            "success": False,
            "reason": "context_load_failed",
            "message": f"Failed to load context for employee {employee_id}",
            "employee_id": employee_id,
            "complexity": detected_complexity,
            "used_planning": used_planning,
        }

    # Step 2.5: Check for self-destructive task patterns
    # Tasks that modify work_queue.json, org.json, or similar system files
    # can accidentally remove themselves from the queue causing "not found" errors.
    #
    # IMPORTANT: Match on intent to MODIFY, not mere mention.
    # e.g. "documenting the org.json race fix" should NOT block — only
    # "edit org.json" / "update work_queue.json" / "delete from org.json" should.
    task_desc = (task.get("description", "") + " " + task.get("title", "")).lower()

    # File-specific patterns: only trigger when an action verb precedes the filename
    _action_verbs = r"(?:edit|update|modify|write to|delete from|clean(?:\s+up)?|clear|truncate|overwrite|patch)\s+"
    _file_patterns = {
        re.compile(_action_verbs + r"work_queue\.json"): "state/work_queue.json",
        re.compile(_action_verbs + r"org\.json"): "org.json",
    }
    _file_patterns_regex = list(_file_patterns.keys())
    # Action-phrase patterns that are inherently about queue manipulation (no verb needed)
    _phrase_patterns = [
        "clean queue",
        "deduplicate queue",
        "dedup queue",
        "consolidate queue",
        "remove stale task",
        "archive completed",  # Archiving tasks can remove self
        "validate work_queue",  # Validation that modifies
        "clean work_queue",
        "stale tasks",  # Generic stale pattern
    ]

    matched_pattern = None
    for regex in _file_patterns_regex:
        if regex.search(task_desc):
            matched_pattern = _file_patterns.get(regex, regex.pattern)
            break
    if not matched_pattern:
        for phrase in _phrase_patterns:
            if phrase in task_desc:
                matched_pattern = phrase
                break

    if matched_pattern:
        return {
            "success": False,
            "reason": "self_destructive_task",
            "message": f"Task appears to modify system files ({matched_pattern}). "
            "These tasks require human execution to prevent self-removal.",
            "employee_id": employee_id,
            "complexity": detected_complexity,
            "used_planning": used_planning,
        }

    # WS-115: Self-isolation guard.  When no worktree was supplied by the daemon
    # (direct-execution fallback, recovery re-execution) create a throwaway one
    # off main so the worker never writes to the main repo root.  A dirty main
    # tree blocks engine self-updates ("Dirty working tree, skipping pull").
    # WS-092: if an explicit execution_cwd resolves to the main root, refuse it.
    _self_wt: Path | None = None
    if execution_cwd is None:
        _wt_result = _create_worker_worktree(task)
        if _wt_result is not None:
            execution_cwd = str(_wt_result[0])
            _self_wt = _wt_result[0]
        else:
            return {
                "success": False,
                "reason": "worktree_isolation_failed",
                "message": (
                    "Worker self-isolation failed: could not create a throwaway "
                    "worktree. Refusing to run in main repo root (WS-092)."
                ),
                "employee_id": employee_id,
                "complexity": detected_complexity,
                "used_planning": used_planning,
            }
    elif Path(execution_cwd).resolve() == get_project_root().resolve():
        return {
            "success": False,
            "reason": "main_root_execution_refused",
            "message": (
                "WS-092: Refusing to execute worker in main repo root. "
                "Provide an isolated worktree path."
            ),
            "employee_id": employee_id,
            "complexity": detected_complexity,
            "used_planning": used_planning,
        }

    # SCOUT-20260813-1 — ChainDrop-class defense: verify nothing has touched
    # the worktree's `.claude/settings.json`, `.mcp.json` or `.claude/hooks/`
    # since checkout, BEFORE the session below can start. Covers both the
    # daemon-supplied execution_cwd and the self-created fallback above — by
    # this point execution_cwd is resolved and guaranteed off the main root.
    _ensure_worktree_integrity()
    _wt_integrity = worktree_integrity.verify_worktree_integrity(Path(execution_cwd))
    if not _wt_integrity.ok:
        print(
            f"[SECURITY] worktree integrity check failed for "
            f"{task.get('task_id')}: {_wt_integrity.reason} "
            f"(diverging: {_wt_integrity.diverging_files})",
            file=sys.stderr,
        )
        worktree_integrity.log_integrity_violation(
            get_project_root(),
            str(task.get("task_id")),
            Path(execution_cwd),
            _wt_integrity,
        )
        if _self_wt is not None:
            _remove_worker_worktree(_self_wt)
        return {
            "success": False,
            "reason": "worktree_integrity_check_failed",
            "message": _wt_integrity.reason,
            "employee_id": employee_id,
            "complexity": detected_complexity,
            "used_planning": used_planning,
        }

    # Step 3: Execute with complexity-based timeout
    # Agentic mode uses longer timeouts since tool calls take more time
    complexity = task.get("estimated_complexity", "standard")
    agentic_cfg = config.get("agenticMode", _build_agentic_config({}))
    if agentic_cfg.get("enabled", DEFAULT_AGENTIC_MODE):
        timeout_multiplier = agentic_cfg.get(
            "timeoutMultipliers", AGENTIC_TIMEOUT_MULTIPLIERS
        ).get(complexity, 2.0)
    else:
        timeout_multiplier = COMPLEXITY_TIMEOUT_MULTIPLIERS.get(complexity, 1.0)
    timeout = int(DEFAULT_EXECUTION_TIMEOUT * timeout_multiplier)
    result = execute_with_employee_context(
        task, employee_id, context, timeout=timeout, execution_cwd=execution_cwd
    )

    # Step 3.5: Post-execution git capture (WS-049)
    # Detect code file changes and persist via branch + commit + draft PR.
    # Only runs if the task succeeded and produced code changes.
    # WS-093 FIX: Use execution_cwd (worktree) if provided, not main project_root.
    # Workers run in isolated worktrees, so changes are there, not in main dir.
    git_capture = {"captured": False}
    if result.success:
        try:
            capture_root = Path(execution_cwd) if execution_cwd else get_project_root()
            git_capture = _capture_code_changes(task, employee_id, capture_root)
            if git_capture.get("error"):
                print(
                    f"  [WS-049] Git capture warning: {git_capture['error']}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"  [WS-049] Git capture failed (non-fatal): {e}",
                file=sys.stderr,
            )
    elif execution_cwd and _is_daemon_worktree(Path(execution_cwd)):
        # A failed session (a timeout, most often) leaves its work uncommitted,
        # and the retry deletes this worktree and its branch. Until 2026-09-20
        # nothing ran here, so LEDGER-2 and LEDGER-4b each lost two full
        # 30-minute builds and every retry restarted from zero. Preserve the
        # tree on a pushed branch; open no PR (see _capture_code_changes, wip).
        try:
            git_capture = _capture_code_changes(
                task, employee_id, Path(execution_cwd), wip=True
            )
            if git_capture.get("error"):
                print(
                    f"  [WS-049] WIP capture warning: {git_capture['error']}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"  [WS-049] WIP capture failed (non-fatal): {e}",
                file=sys.stderr,
            )

    # WS-115: Clean up self-created throwaway worktree. A successful session's
    # changes are already committed and pushed to a PR by _capture_code_changes
    # above; a failed one's were pushed to a wip branch, or reported as lost.
    if _self_wt is not None:
        _remove_worker_worktree(_self_wt)
        _self_wt = None

    # Step 3.6: Deliverable enforcement (P95)
    # Default: ALL tasks require deliverables (file changes or PR).
    # This prevents phantom completions where Claude outputs text but
    # never writes files. Tasks can opt out with requires_deliverable=False.
    requires_deliverable = task.get("requires_deliverable", True)

    # Only exempt documentation-only or analysis tasks that explicitly opt out
    if not requires_deliverable:
        # Still enforce if task has code capabilities or code keywords
        task_caps = task.get("required_capabilities", [])
        code_capabilities = {
            "python",
            "javascript",
            "typescript",
            "frontend",
            "backend",
            "web-design",
            "css",
            "html",
            "api",
            "database",
            "testing",
            "refactoring",
            "bug-fix",
            "feature",
            "implementation",
            "security",
            "installation",
            "shell-scripting",
            "code-review",
            "devops",
        }
        if any(cap.lower() in code_capabilities for cap in task_caps):
            requires_deliverable = True

    # Enforce deliverable requirement
    if result.success and requires_deliverable:
        pr_created = git_capture.get("captured", False) or git_capture.get("pr_url")
        # Fix: git_capture returns files_changed (list), not changes_count (int)
        files_changed = git_capture.get("files_changed", [])
        has_changes = len(files_changed) > 0

        if has_changes and not pr_created:
            # P95-CAPTURE: Changes detected but not shipped to a PR.
            # Catches the phantom where a worker modified files (visible to git
            # status) but branch/commit/push/PR creation failed — the task would
            # otherwise be marked SUCCESS with uncommitted work in the worktree.
            capture_err = git_capture.get("error") or "capture/PR creation failed"
            print(
                f"  [P95-CAPTURE] FAIL: {len(files_changed)} file(s) changed but not "
                f"shipped to PR. Error: {capture_err}",
                file=sys.stderr,
            )
            result = ExecutionResult(
                success=False,
                output=result.output,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                error=(
                    f"Work done but not shipped — {len(files_changed)} file(s) modified "
                    f"but capture/PR failed: {capture_err}"
                ),
                employee_id=result.employee_id,
                task_id=result.task_id,
            )
        elif not pr_created and not has_changes:
            # P95: Task claims success but produced no deliverables at all
            print(
                f"  [P95] FAIL: Task requires deliverable but none produced. "
                f"PR created: {pr_created}, Changes: {has_changes}",
                file=sys.stderr,
            )
            # Override success - this task didn't actually complete
            result = ExecutionResult(
                success=False,
                output=result.output,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                error="Task completed without producing required deliverables (no PR or file changes)",
                employee_id=result.employee_id,
                task_id=result.task_id,
            )

    # Step 4: Update memory
    memory_updated = update_employee_memory(employee_id, task, result)

    # Step 5: Record efficiency
    # WS-069-005: Stash memory content on result for honest hit rate
    result._memory_content = context.memory_content if context else ""
    efficiency_recorded = record_task_efficiency(employee_id, task, result)

    return {
        "success": result.success,
        "reason": "executed" if result.success else "execution_failed",
        "message": (
            f"Task completed by {employee_id}"
            if result.success
            else (
                f"Task failed during execution by {employee_id}: "
                f"exit_code={result.exit_code}"
                + (f" — {result.error[:300]}" if result.error else "")
            )
        ),
        "employee_id": employee_id,
        "complexity": detected_complexity,
        "complexity_source": complexity_source,  # P28: Track where complexity came from
        "used_planning": used_planning,
        "execution_result": asdict(result),
        "memory_updated": memory_updated,
        "efficiency_recorded": efficiency_recorded,
        "git_capture": git_capture,  # WS-049: persistence result
        "context_loaded": {
            "memory_chars": len(context.memory_content),
            "definition_chars": len(context.agent_definition),
            "memory_path": context.memory_path,
            "definition_path": context.agent_definition_path,
        },
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Employee Activation Layer — Wire employees to execute tasks with context

Commands:
    match           Match best employee for a task
    context         Load context for an employee
    execute         Execute a task with employee context
    activate        Full activation flow (match + context + execute + update)
    config          Show activation configuration

match options:
    --task-id ID            Task ID to match (loads from work queue)
    --capabilities LIST     Comma-separated capabilities to match

context options:
    --employee-id ID        Employee ID to load context for

execute options:
    --task-id ID            Task ID to execute
    --employee-id ID        Employee ID to execute as
    --timeout SECONDS       Execution timeout (default: 300)

activate options:
    --task-id ID            Task ID to activate
    --fallback-agent ID     Fallback agent if no employee matches

Examples:
    # Match employee for task
    python employee_activator.py match --capabilities "python,testing"

    # Load employee context
    python employee_activator.py context --employee-id senior-python-developer

    # Execute task with employee
    python employee_activator.py execute --task-id task-123 --employee-id senior-python-developer

    # Full activation flow
    python employee_activator.py activate --task-id task-123

    # Show configuration
    python employee_activator.py config
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result: dict[str, Any] = {}
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
        if command == "match":
            if "capabilities" in args:
                capabilities = [c.strip() for c in args["capabilities"].split(",")]
            else:
                capabilities = []

            # Create a minimal task for matching
            task = {"required_capabilities": capabilities}
            employee_id = match_employee_for_task(task)

            print(
                json.dumps(
                    {
                        "success": employee_id is not None,
                        "employee_id": employee_id,
                        "capabilities_requested": capabilities,
                    },
                    indent=2,
                )
            )

        elif command == "context":
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            context = load_employee_context(args["employee_id"])
            if context:
                print(
                    json.dumps(
                        {
                            "success": True,
                            "context": {
                                "employee_id": context.employee_id,
                                "name": context.name,
                                "department": context.department,
                                "team": context.team,
                                "capabilities": context.capabilities,
                                "memory_chars": len(context.memory_content),
                                "definition_chars": len(context.agent_definition),
                                "efficiency_score": context.efficiency_score,
                                "memory_path": context.memory_path,
                                "definition_path": context.agent_definition_path,
                            },
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": f"Employee {args['employee_id']} not found",
                        },
                        indent=2,
                    )
                )

        elif command == "execute":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "employee_id" not in args:
                print("Error: --employee-id required")
                sys.exit(1)

            # Load task from work queue
            _ensure_imports()
            task_result = work_allocator.get_task(args["task_id"])

            if not task_result.get("success"):
                print(json.dumps(task_result, indent=2))
                sys.exit(1)

            task = task_result["task"]
            employee_id = args["employee_id"]

            # Load context
            context = load_employee_context(employee_id)
            if not context:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": f"Failed to load context for {employee_id}",
                        },
                        indent=2,
                    )
                )
                sys.exit(1)

            # Execute
            timeout = int(args.get("timeout", DEFAULT_EXECUTION_TIMEOUT))
            result = execute_with_employee_context(task, employee_id, context, timeout)

            print(json.dumps(asdict(result), indent=2))

        elif command == "activate":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            # Load task from work queue
            _ensure_imports()
            task_result = work_allocator.get_task(args["task_id"])

            if not task_result.get("success"):
                print(json.dumps(task_result, indent=2))
                sys.exit(1)

            task = task_result["task"]
            fallback = args.get("fallback_agent")

            result = activate_employee_for_task(task, fallback)
            print(json.dumps(result, indent=2))

        elif command == "config":
            config = get_activation_config()
            print(json.dumps(config, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
