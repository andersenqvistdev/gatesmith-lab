#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Company Operation Loop — central orchestration for company-mode task execution.

This module provides:
1. Trust tier validation for operations requiring gated approval
2. Operation lifecycle management (poll/claim/release cycle)
3. Session state integration with loop metrics (task 1.3)

Trust Tiers (from CLAUDE.md):
- Free: Read-only operations (auto-approved)
- Guarded: Local modifications (auto-approved + logged)
- Gated: External consequences (requires human confirmation)
- Forbidden: Blocked unconditionally (handled by block_dangerous.py hooks)

Operation Loop Functions:
    get_claimable_tasks(queue_path) -> list[dict]
        Returns tasks eligible for claiming (pending, deps satisfied, not in backoff)

    claim_task(queue_path, task_id, agent_id) -> dict
        Atomically claim a task for execution

    release_task(queue_path, task_id, result, error) -> dict
        Release a task after execution attempt

    poll_and_execute_once(queue_path, agent_id) -> dict
        Single iteration of the operation loop

Constants:
    LOOP_AGENT_ID: Default agent ID for the operation loop
    BASE_DELAY: Base delay for exponential backoff (seconds)
    MAX_DELAY: Maximum delay cap for backoff (seconds)

Usage:
    # Get tasks available for claiming
    python operation_loop.py claimable

    # Claim a specific task
    python operation_loop.py claim --task-id "task-123" --agent-id "agent-001"

    # Release a task after execution
    python operation_loop.py release --task-id "task-123" --result completed

    # Run one poll/execute cycle
    python operation_loop.py poll --agent-id "agent-001"
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# Infra-vs-task failure classification (provider preflight timeouts, 0-step CI
# runs, etc). Side-effect-free module, safe to import unconditionally.
try:
    from . import infra_failure
except ImportError:
    import infra_failure  # type: ignore[no-redef]

# Import patterns from block_dangerous.py (no pattern duplication)
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from block_dangerous import DANGEROUS_PATTERNS
except ImportError:
    # Fallback if block_dangerous.py not available
    DANGEROUS_PATTERNS: list[str] = []

# Import from sibling modules for operation loop
# Lazy imports to handle both package and direct execution
company_resolver = None
escalation_module = None
work_allocator = None
efficiency_tracker = None
employee_activator = None
pr_output_manager = None
roadmap_scheduler = None
employee_initiative = None  # P32: Post-completion proposals


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global company_resolver, escalation_module, work_allocator, efficiency_tracker
    global employee_activator, pr_output_manager, roadmap_scheduler, employee_initiative
    if company_resolver is not None:
        return  # Already imported

    try:
        from . import company_resolver as cr
        from . import employee_activator as ea
        from . import escalation as em
        from . import optional_modules as opt
        from . import pr_output_manager as pom
        from . import roadmap_scheduler as rs
        from . import work_allocator as wa
    except ImportError:
        # Direct script execution - import from same directory
        import company_resolver as cr  # type: ignore[no-redef]
        import employee_activator as ea  # type: ignore[no-redef]
        import escalation as em  # type: ignore[no-redef]
        import optional_modules as opt  # type: ignore[no-redef]
        import pr_output_manager as pom  # type: ignore[no-redef]
        import roadmap_scheduler as rs  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

    employee_activator = ea
    pr_output_manager = pom
    roadmap_scheduler = rs
    escalation_module = em
    work_allocator = wa
    # Not in the public export (K2a): None when absent.
    efficiency_tracker = opt.import_optional("efficiency_tracker", __package__)
    employee_initiative = opt.import_optional("employee_initiative", __package__)  # P32
    company_resolver = cr  # last: it is the already-imported sentinel


# -----------------------------------------------------------------------------
# Operation Loop Constants
# -----------------------------------------------------------------------------

LOOP_AGENT_ID = "operation-loop"
BASE_DELAY = 60  # Base delay for exponential backoff (seconds)
MAX_DELAY = 3600  # Maximum delay cap (1 hour)

# Additional gated keywords (require approval, not blocked)
# These operations have external consequences but are not dangerous
GATED_KEYWORDS = [
    r"deploy",
    r"publish",
    r"release",
    r"production",
    r"git\s+push",
    r"docker\s+push",
    r"npm\s+publish",
    r"pip\s+upload",
    r"aws\s+.*deploy",
    r"kubectl\s+apply",
    r"terraform\s+apply",
    r"ansible-playbook",
    r"make\s+deploy",
    r"make\s+release",
]

# Default maximum retry attempts before escalation
DEFAULT_MAX_ATTEMPTS = 3

# Escalation types for create_loop_escalation
ESCALATION_TYPE_GATED = "gated_operation"
ESCALATION_TYPE_MAX_RETRIES = "max_retries"
ESCALATION_TYPE_UNRECOVERABLE = "unrecoverable"


# -----------------------------------------------------------------------------
# Self-Healing Retry Logic (Task 2.1 - ADR-001)
# -----------------------------------------------------------------------------


class ErrorCategory(Enum):
    """Error category for retry strategy determination.

    WS-068-005: 4-tier error classification for self-healing.

    Categories (in order of severity):
    - TRANSIENT: Network, rate limit errors - retry with backoff
    - RECOVERABLE: Test failures, lint errors - attempt self-heal then retry
    - STRUCTURAL: Wrong branch, missing dirs - auto-fix infrastructure then retry
    - UNRECOVERABLE: Permission, config errors - escalate immediately (once only)
    """

    TRANSIENT = "transient"  # Tier 1: Network, rate limit - retry with backoff
    RECOVERABLE = "recoverable"  # Tier 2: Test fail, lint - self-heal attempt
    STRUCTURAL = "structural"  # Tier 3: Wrong branch, missing deps - auto-fix infra
    UNRECOVERABLE = "unrecoverable"  # Tier 4: Permission, config - escalate once


class FixStrategy(Enum):
    """Fix strategy derived from ErrorCategory.

    Strategies:
    - RETRY_WITH_BACKOFF: From TRANSIENT errors - wait and retry
    - RETRY_AFTER_FIX: From RECOVERABLE errors - apply fix then retry
    - AUTO_FIX_INFRA: From STRUCTURAL errors - fix infrastructure then retry
    - ESCALATE_IMMEDIATELY: From UNRECOVERABLE errors - human intervention needed
    - ESCALATE_WITH_SUGGESTION: Max retries exceeded - escalate with fix suggestions
    """

    RETRY_WITH_BACKOFF = "retry_with_backoff"  # TRANSIENT - wait and retry
    RETRY_AFTER_FIX = "retry_after_fix"  # RECOVERABLE - fix then retry
    AUTO_FIX_INFRA = "auto_fix_infra"  # STRUCTURAL - fix infra then retry
    ESCALATE_IMMEDIATELY = "escalate_immediately"  # UNRECOVERABLE - human needed
    ESCALATE_WITH_SUGGESTION = "escalate_with_suggestion"  # Max retries - with hints


# Contextual suggestion patterns for get_fix_strategy
_SUGGESTION_PATTERNS = [
    # Test failures
    (
        r"test.*fail|assertion.*error|expect.*but.*got",
        "Review test expectations and check for recent code changes that may have broken tests",
    ),
    # Import/module errors
    (
        r"import.*error|module.*not.*found|no.*module.*named",
        "Check dependencies in requirements.txt/pyproject.toml and run pip install",
    ),
    # Syntax errors
    (
        r"syntax.*error|invalid.*syntax|unexpected.*token",
        "Check for typos, missing brackets/parentheses, or indentation issues",
    ),
    # Type errors
    (
        r"type.*error|not.*callable|not.*subscriptable",
        "Verify variable types and function signatures match expected usage",
    ),
    # File not found
    (
        r"file.*not.*found|no.*such.*file|path.*does.*not.*exist",
        "Verify file paths and ensure required files exist",
    ),
    # Connection/network
    (
        r"connection|network|timeout|rate.*limit",
        "Check network connectivity and consider increasing timeout or retry delay",
    ),
    # Memory/resource
    (
        r"memory|out.*of.*memory|resource.*exhausted",
        "Consider reducing batch size or freeing up system resources",
    ),
    # Permission issues
    (
        r"permission|access.*denied|forbidden",
        "Check file/API permissions and credentials",
    ),
]


def calculate_backoff_seconds(attempt: int) -> int:
    """Calculate backoff delay for retry attempt.

    Formula (ADR-001): min(base_delay * (2 ** attempt), max_delay)
    - base_delay = 60 seconds
    - max_delay = 3600 seconds (1 hour)

    Args:
        attempt: The attempt number (0-indexed).

    Returns:
        Backoff delay in seconds.

    Examples:
        >>> calculate_backoff_seconds(0)
        60
        >>> calculate_backoff_seconds(1)
        120
        >>> calculate_backoff_seconds(2)
        240
        >>> calculate_backoff_seconds(6)
        3600
    """
    return min(BASE_DELAY * (2**attempt), MAX_DELAY)


# Patterns for transient (retryable) errors
TRANSIENT_PATTERNS = [
    r"timeout",
    r"timed?\s*out",
    r"rate\s*limit",
    r"429",
    r"503",
    r"504",
    r"connection\s*(refused|reset|closed)",
    r"temporarily\s*unavailable",
    r"service\s*unavailable",
    r"network\s*(error|unreachable)",
    r"ECONNREFUSED",
    r"ETIMEDOUT",
    r"ECONNRESET",
]

# Patterns for unrecoverable errors
UNRECOVERABLE_PATTERNS = [
    r"permission\s*denied",
    r"access\s*denied",
    r"403",
    r"401",
    r"unauthorized",
    r"blocked\s*by\s*hook",
    r"forbidden",
    r"invalid\s*(credentials?|token|api\s*key)",
    r"authentication\s*(failed|error)",
    r"not\s*authorized",
    r"EACCES",
    r"EPERM",
]

# WS-068-005: Patterns for structural errors (auto-fixable infrastructure issues)
STRUCTURAL_PATTERNS = [
    r"not\s*on\s*main\s*branch",
    r"not\s*on\s*(master|main)",
    r"branch\s*.*\s*does\s*not\s*exist",
    r"fatal:\s*not\s*a\s*git\s*repository",
    r"directory\s*.*\s*does\s*not\s*exist",
    r"no\s*such\s*file\s*or\s*directory",
    r"ENOENT",
    r"missing\s*dependency",
    r"module\s*not\s*found",
    r"package\s*.*\s*not\s*installed",
    r"could\s*not\s*find\s*module",
    r"merge\s*conflict",
    r"rebase\s*in\s*progress",
    r"working\s*tree\s*.*\s*dirty",
    r"uncommitted\s*changes",
    r"stale\s*.*\s*state",
    r"lock\s*file\s*exists",
]

# WS-068-005: Auto-fix commands for structural errors
STRUCTURAL_AUTO_FIXES: dict[str, list[str]] = {
    r"not\s*on\s*main\s*branch": ["git checkout main", "git pull origin main"],
    r"not\s*on\s*(master|main)": ["git checkout main", "git pull origin main"],
    r"directory\s*.*\s*does\s*not\s*exist": ["mkdir -p"],  # Path extracted from error
    r"module\s*not\s*found|package\s*.*\s*not\s*installed": ["uv pip install"],
    r"working\s*tree\s*.*\s*dirty|uncommitted\s*changes": ["git stash"],
    r"lock\s*file\s*exists": ["rm -f"],  # Lock path extracted from error
}


def classify_error(error: str) -> ErrorCategory:
    """Classify error to determine retry strategy.

    Transient patterns: timeout, rate limit, 429, 503, connection refused
    Unrecoverable patterns: permission denied, 403, 401, blocked by hook
    Default: RECOVERABLE

    Args:
        error: Error message string to classify.

    Returns:
        ErrorCategory indicating the type of error.

    Examples:
        >>> classify_error("Connection timeout after 30s")
        <ErrorCategory.TRANSIENT: 'transient'>
        >>> classify_error("Permission denied: /etc/passwd")
        <ErrorCategory.UNRECOVERABLE: 'unrecoverable'>
        >>> classify_error("Test failed: assertion error")
        <ErrorCategory.RECOVERABLE: 'recoverable'>
    """
    error_lower = error.lower()

    # Check transient patterns first (Tier 1: retry with backoff)
    for pattern in TRANSIENT_PATTERNS:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return ErrorCategory.TRANSIENT

    # Check unrecoverable patterns (Tier 4: escalate immediately)
    for pattern in UNRECOVERABLE_PATTERNS:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return ErrorCategory.UNRECOVERABLE

    # WS-068-005: Check structural patterns (Tier 3: auto-fix infrastructure)
    for pattern in STRUCTURAL_PATTERNS:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return ErrorCategory.STRUCTURAL

    # Default to recoverable (Tier 2: self-heal attempt)
    return ErrorCategory.RECOVERABLE


def _get_contextual_suggestion(
    error_msg: str,
    max_attempts: int,
    for_fix: bool = False,
    for_escalation: bool = False,
) -> str:
    """Generate contextual suggestion based on error message patterns.

    Args:
        error_msg: The error message to analyze.
        max_attempts: Maximum retry attempts (for context in message).
        for_fix: If True, phrase suggestion for self-fix attempt.
        for_escalation: If True, phrase suggestion for human escalation.

    Returns:
        Contextual suggestion string.
    """
    error_lower = error_msg.lower()

    # Find matching pattern
    for pattern, suggestion_base in _SUGGESTION_PATTERNS:
        if re.search(pattern, error_lower, re.IGNORECASE):
            if for_escalation:
                return f"UNRECOVERABLE: {suggestion_base}. Human intervention required."
            elif for_fix:
                return (
                    f"Suggested fix: {suggestion_base}. Will retry after applying fix."
                )
            else:
                return f"Max retries ({max_attempts}) exceeded. Suggestion: {suggestion_base}"

    # Default suggestion if no pattern matches
    if for_escalation:
        return f"UNRECOVERABLE: Error requires human intervention. Original error: {error_msg[:100]}"
    elif for_fix:
        return f"Attempting automatic recovery. Original error: {error_msg[:100]}"
    else:
        return f"Max retries ({max_attempts}) exceeded. Review error details: {error_msg[:100]}"


def _get_structural_auto_fix_suggestion(error_msg: str) -> str:
    """
    WS-068-005: Generate auto-fix commands for STRUCTURAL errors.

    STRUCTURAL errors are infrastructure issues that can be automatically fixed
    before retrying the task (e.g., wrong branch, missing directories).

    Args:
        error_msg: The error message to analyze.

    Returns:
        Suggestion string with auto-fix commands to apply.
    """
    error_lower = error_msg.lower()
    fixes: list[str] = []

    # Check each pattern and collect applicable fixes
    for pattern, commands in STRUCTURAL_AUTO_FIXES.items():
        if re.search(pattern, error_lower, re.IGNORECASE):
            fixes.extend(commands)

    if fixes:
        fix_cmds = " && ".join(fixes[:3])  # Limit to 3 commands
        return f"STRUCTURAL: Auto-fix required. Run: {fix_cmds}"

    # Fallback for unmatched structural errors
    return f"STRUCTURAL: Infrastructure issue detected. Manual fix may be required: {error_msg[:100]}"


def get_fix_strategy(
    error_category: ErrorCategory,
    error_msg: str,
    retry_count: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[FixStrategy, str]:
    """Derive fix strategy from ErrorCategory with contextual suggestions.

    The strategy is DERIVED from the ErrorCategory, not independently classified:
    - TRANSIENT -> RETRY_WITH_BACKOFF
    - RECOVERABLE -> RETRY_AFTER_FIX (or ESCALATE_WITH_SUGGESTION if max retries)
    - UNRECOVERABLE -> ESCALATE_IMMEDIATELY

    Args:
        error_category: The classified ErrorCategory of the error.
        error_msg: The original error message for contextual suggestions.
        retry_count: Current number of retry attempts.
        max_attempts: Maximum allowed retry attempts (default: 3).

    Returns:
        Tuple of (FixStrategy, suggestion_message).

    Examples:
        >>> cat = ErrorCategory.TRANSIENT
        >>> strategy, suggestion = get_fix_strategy(cat, "Connection timeout", 0, 3)
        >>> strategy
        <FixStrategy.RETRY_WITH_BACKOFF: 'retry_with_backoff'>

        >>> cat = ErrorCategory.RECOVERABLE
        >>> strategy, suggestion = get_fix_strategy(cat, "Test failed", 3, 3)
        >>> strategy
        <FixStrategy.ESCALATE_WITH_SUGGESTION: 'escalate_with_suggestion'>

        >>> cat = ErrorCategory.UNRECOVERABLE
        >>> strategy, suggestion = get_fix_strategy(cat, "Permission denied", 0, 3)
        >>> strategy
        <FixStrategy.ESCALATE_IMMEDIATELY: 'escalate_immediately'>
    """
    # Check if max retries exceeded first (applies to TRANSIENT, RECOVERABLE, STRUCTURAL)
    if retry_count >= max_attempts and error_category != ErrorCategory.UNRECOVERABLE:
        # Generate contextual suggestion based on error patterns
        suggestion = _get_contextual_suggestion(error_msg, max_attempts)
        return (FixStrategy.ESCALATE_WITH_SUGGESTION, suggestion)

    # Derive strategy from category (WS-068-005: 4-tier classification)
    if error_category == ErrorCategory.TRANSIENT:
        # Tier 1: Retry with backoff
        backoff_seconds = calculate_backoff_seconds(retry_count)
        suggestion = (
            f"Transient error detected. Will retry after {backoff_seconds}s backoff "
            f"(attempt {retry_count + 1}/{max_attempts})."
        )
        return (FixStrategy.RETRY_WITH_BACKOFF, suggestion)

    elif error_category == ErrorCategory.RECOVERABLE:
        # Tier 2: Self-heal attempt (lint fix, test fix)
        suggestion = _get_contextual_suggestion(error_msg, max_attempts, for_fix=True)
        return (FixStrategy.RETRY_AFTER_FIX, suggestion)

    elif error_category == ErrorCategory.STRUCTURAL:
        # Tier 3: Auto-fix infrastructure before retry
        suggestion = _get_structural_auto_fix_suggestion(error_msg)
        return (FixStrategy.AUTO_FIX_INFRA, suggestion)

    else:  # UNRECOVERABLE
        # Tier 4: Escalate immediately (once only)
        suggestion = _get_contextual_suggestion(
            error_msg, max_attempts, for_escalation=True
        )
        return (FixStrategy.ESCALATE_IMMEDIATELY, suggestion)


# Diagnosis patterns: (compiled_regex, category, retryable, fix_hint, backoff_seconds)
# Order matters: more specific patterns should come before general ones.
# Regex to unwrap "Task failed during execution by <employee>: <inner_error>"
_WRAPPED_ERROR_RE = re.compile(
    r"Task failed during execution by [\w-]+:\s*(.+)", re.DOTALL
)


def _unwrap_error_message(error_msg: str) -> str:
    """Extract inner error from wrapped employee activator messages.

    Many errors arrive as "Task failed during execution by <employee>: <actual error>".
    This extracts the inner error so diagnosis patterns can match it directly.

    Returns the inner error if wrapped, otherwise returns the original message.
    """
    m = _WRAPPED_ERROR_RE.search(error_msg.strip())
    return m.group(1).strip() if m else error_msg


_DIAGNOSIS_PATTERNS: list[tuple[str, str, bool, str | None, int]] = [
    # Workflow/deliverable judge explicitly rejected the implementation.
    # The worker built something real but it didn't address the task.
    # Non-retryable at the base layer — recovery system handles replan.
    (
        r"Workflow verdict.*does not deliver|diff does not deliver the task",
        "deliverable_rejected",
        False,
        "Implementation did not address the task — re-read requirements and use a different approach",
        0,
    ),
    # employee_activator's P52/WS-121 deliverable gate: exit_code=0 but no
    # file changes / PR were produced. Must outrank the exit_code=0 pattern
    # below — that catch-all is meant for genuine success-detection false
    # negatives, not confirmed zero-deliverable failures.
    (
        r"no deliverable produced|deliverable verification failed",
        "no_deliverable",
        False,
        "No files were changed and no PR was produced — retry with a concrete plan to write/modify files",
        0,
    ),
    # --- PR workflow errors (most common in production) ---
    # PR creation failed due to test failures — need code fix, not retry
    (
        r"PR creation failed.*Tests? failed",
        "pr_test_failure",
        False,
        "Fix failing tests before re-attempting PR creation",
        0,
    ),
    # PR creation failed due to git merge conflicts — need manual resolution
    (
        r"unmerged files|merge conflict|needs merge|not possible because you have unmerged",
        "git_conflict",
        False,
        "Resolve merge conflicts before retrying",
        0,
    ),
    # PR creation failed due to push rejection (branch exists, non-fast-forward)
    (
        r"PR creation failed.*Push failed|push.*rejected|failed to push|non-fast-forward",
        "git_push_error",
        True,
        "Delete stale remote branch or pull latest changes before push",
        120,
    ),
    # PR creation failed due to commit failure (other than merge conflicts)
    (
        r"PR creation failed.*Commit failed",
        "pr_commit_error",
        False,
        "Fix working tree state before committing",
        0,
    ),
    # Catch-all for PR creation failures not matched by specific sub-patterns above
    (
        r"PR creation failed",
        "pr_failure",
        False,
        "PR creation failed — review error details and fix root cause",
        0,
    ),
    # --- Exit-code wrapper messages from employee_activator ---
    # exit_code=0 but marked failed = success detection false negative
    # Task likely completed; retrying wastes resources
    (
        r"exit_code=0\b",
        "success_detection_failure",
        False,
        "Task exited successfully but output lacked completion markers. "
        "Review success detection thresholds or task output format",
        0,
    ),
    # exit_code=-1 = subprocess timeout or FileNotFoundError
    (
        r"exit_code=-1\b",
        "subprocess_error",
        True,
        "Subprocess timed out or command not found. Check timeouts and paths",
        120,
    ),
    # exit_code=143 = SIGTERM (killed externally)
    (
        r"exit_code=143\b",
        "process_killed",
        True,
        "Process was killed (SIGTERM). May be OOM or external signal",
        180,
    ),
    # exit_code=137 = SIGKILL (OOM killer)
    (
        r"exit_code=137\b",
        "oom_killed",
        True,
        "Process was OOM-killed (SIGKILL). Reduce memory usage or batch size",
        300,
    ),
    # exit_code=127 = command not found
    (
        r"exit_code=127\b",
        "command_not_found",
        False,
        "Command not found. Install missing tool or fix PATH",
        0,
    ),
    # --- Original patterns ---
    # Path/file errors — often transient (file created by prior task)
    (
        r"command not found|No such file|FileNotFoundError",
        "path_error",
        True,
        "Verify file/command paths exist before execution",
        60,
    ),
    # Import errors — need dependency fix, not a simple retry
    (
        r"ModuleNotFoundError|ImportError",
        "import_error",
        False,
        "Install missing dependency or fix import path",
        0,
    ),
    # Timeouts — retry with longer backoff
    (
        r"TimeoutExpired|timed?\s*out",
        "timeout",
        True,
        "Consider increasing timeout or reducing workload",
        120,
    ),
    # Rate limits — retry with long backoff
    (
        r"rate\s*limit|429|Too Many Requests",
        "api_limit",
        True,
        "Wait for rate limit window to reset",
        300,
    ),
    # Test failures — need code fix, not retry
    # P58 FIX: Use specific pytest failure patterns, not generic word matches.
    # Old pattern "FAILED|pytest" caused false positives when Claude mentioned
    # these words in successful output like "I fixed the FAILED test".
    (
        r"FAILED\s+test_|FAILED\s+\w+::|pytest.*\d+\s+failed|AssertionError:",
        "test_failure",
        False,
        "Fix failing test or update test expectations",
        0,
    ),
    # Permission errors — not retryable
    (
        r"Permission denied|EACCES|EPERM",
        "permission_error",
        False,
        "Check file/directory permissions",
        0,
    ),
    # Syntax errors — need code fix
    (
        r"SyntaxError|IndentationError|TabError",
        "syntax_error",
        False,
        "Fix syntax error in source code",
        0,
    ),
    # Python runtime errors — code bug, not a transient issue
    (
        r"TypeError:|AttributeError:|NameError:",
        "runtime_error",
        False,
        "Fix Python runtime error — check type usage, attribute access, and variable names",
        0,
    ),
    # Memory/resource errors — retry with backoff
    (
        r"MemoryError|ResourceExhausted|out of memory",
        "resource_error",
        True,
        "Reduce batch size or memory usage",
        300,
    ),
]


def diagnose_error(error_msg: str, exit_code: int) -> dict:
    """Categorize task error and determine retry strategy.

    Analyzes error message patterns and exit code to produce a diagnosis
    dict that guides retry decisions in release_task().

    Two-pass approach:
    1. Pattern-match against error message (primary)
    2. Fall back to exit-code-based classification if no pattern matches

    Args:
        error_msg: The error message from the failed task execution.
        exit_code: The process exit code (0=success, non-zero=failure).

    Returns:
        Dict with keys:
        - category: str — error category identifier
        - retryable: bool — whether the task should be retried
        - fix_hint: str|None — human-readable suggestion for fixing
        - backoff_seconds: int — suggested wait before retry
    """
    if not error_msg:
        # No error message — use exit code for classification
        return _diagnose_by_exit_code(exit_code)

    # Try matching the raw error first, then the unwrapped inner error.
    # Many errors arrive wrapped as "Task failed during execution by <emp>: <inner>"
    # and the inner error is what the patterns are designed to match.
    candidates = [error_msg]
    unwrapped = _unwrap_error_message(error_msg)
    if unwrapped != error_msg:
        candidates.append(unwrapped)

    for candidate in candidates:
        for pattern, category, retryable, fix_hint, backoff in _DIAGNOSIS_PATTERNS:
            if re.search(pattern, candidate, re.IGNORECASE):
                return {
                    "category": category,
                    "retryable": retryable,
                    "fix_hint": fix_hint,
                    "backoff_seconds": backoff,
                }

    # No pattern matched — fall back to exit code classification
    return _diagnose_by_exit_code(exit_code)


# Exit-code-based classification fallback.
# Maps well-known exit codes to diagnosis when no error pattern matches.
_EXIT_CODE_DIAGNOSIS: dict[int, tuple[str, bool, str, int]] = {
    # (category, retryable, fix_hint, backoff_seconds)
    0: (
        "success_detection_failure",
        False,
        "Exit code 0 but no completion markers detected",
        0,
    ),
    127: (
        "command_not_found",
        False,
        "Command not found — install tool or fix PATH",
        0,
    ),
    137: ("oom_killed", True, "Process killed by SIGKILL (likely OOM)", 300),
    143: ("process_killed", True, "Process killed by SIGTERM", 180),
    -1: ("subprocess_error", True, "Subprocess timeout or execution error", 120),
}


def _diagnose_by_exit_code(exit_code: int) -> dict:
    """Classify error based on exit code when no error pattern matches."""
    if exit_code in _EXIT_CODE_DIAGNOSIS:
        category, retryable, fix_hint, backoff = _EXIT_CODE_DIAGNOSIS[exit_code]
        return {
            "category": category,
            "retryable": retryable,
            "fix_hint": fix_hint,
            "backoff_seconds": backoff,
        }
    # Truly unknown error
    return {
        "category": "unknown",
        "retryable": True,
        "fix_hint": None,
        "backoff_seconds": 60,
    }


def should_retry(task: dict, error: str) -> tuple[bool, str]:
    """Determine if task should be retried.

    Args:
        task: Task dictionary containing at minimum:
            - retry_count: int (current retry attempts, default 0)
            - max_attempts: int (maximum allowed attempts, default 3)
        error: Error message from the failed attempt.

    Returns:
        Tuple of (should_retry: bool, reason: str)

    Logic:
    1. If error is UNRECOVERABLE: return (False, "unrecoverable_error")
    2. If attempts >= max_attempts: return (False, "max_attempts_exceeded")
    3. Else: return (True, "will_retry")

    Examples:
        >>> task = {"retry_count": 0, "max_attempts": 3}
        >>> should_retry(task, "Connection timeout")
        (True, 'will_retry')
        >>> should_retry(task, "Permission denied")
        (False, 'unrecoverable_error')
        >>> task = {"retry_count": 3, "max_attempts": 3}
        >>> should_retry(task, "Network error")
        (False, 'max_attempts_exceeded')
    """
    # Get retry metadata from task
    retry_count = task.get("retry_count", 0)
    max_attempts = task.get("max_attempts", DEFAULT_MAX_ATTEMPTS)

    # 1. Check if error is unrecoverable
    category = classify_error(error)
    if category == ErrorCategory.UNRECOVERABLE:
        return (False, "unrecoverable_error")

    # 2. Check if max attempts exceeded
    if retry_count >= max_attempts:
        return (False, "max_attempts_exceeded")

    # 3. Can retry
    return (True, "will_retry")


def attempt_auto_fix(
    task_id: str,
    error_category: ErrorCategory,
    error_msg: str,
    state_path: Path,
) -> dict:
    """
    Attempt to provide auto-fix hints for a failed task (MVP: hints only).

    This function does NOT execute fixes. It only adds hints to the task
    metadata that can guide manual or future automated resolution.

    Args:
        task_id: The failed task ID
        error_category: Classification of the error
        error_msg: The error message
        state_path: Path to the state file

    Returns:
        dict with:
        - applied: bool (always False for MVP)
        - hint: str (contextual hint)
        - suggested_action: str (what to do)
    """
    error_lower = error_msg.lower()

    # Determine hint and suggested action based on ErrorCategory
    if error_category == ErrorCategory.TRANSIENT:
        hint = "Retry after delay"
        suggested_action = "Wait and retry"
    elif error_category == ErrorCategory.RECOVERABLE:
        # Contextual hints for recoverable errors
        if "import" in error_lower:
            hint = "Check dependencies"
            suggested_action = "Review imports"
        elif "test" in error_lower:
            hint = "Review test output"
            suggested_action = "Fix failing tests"
        else:
            hint = "Check error details"
            suggested_action = "Debug and fix"
    else:  # UNRECOVERABLE
        hint = "Manual intervention required"
        suggested_action = "Escalate to human"

    # Build the fix hints structure
    fix_hints = {
        "task_id": task_id,
        "error_category": error_category.value,
        "hint": hint,
        "suggested_action": suggested_action,
        "error_snippet": error_msg[:200] if len(error_msg) > 200 else error_msg,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Update task metadata with fix_hints in the state file
    state = load_session_state(state_path)

    # Ensure task_metadata exists
    if "task_metadata" not in state:
        state["task_metadata"] = {}

    # Ensure the specific task entry exists
    if task_id not in state["task_metadata"]:
        state["task_metadata"][task_id] = {}

    # Add fix_hints to the task metadata
    state["task_metadata"][task_id]["fix_hints"] = fix_hints

    # Save the updated state
    save_session_state(state_path, state)

    return {
        "applied": False,  # MVP constraint: never execute fixes
        "hint": hint,
        "suggested_action": suggested_action,
    }


@dataclass
class TrustTierConfig:
    """Configuration for trust tier validation behavior.

    Attributes:
        mode: Validation mode - "relaxed" (trust hooks), "strict" (legacy), "audit-only"
        audit_gated_keywords: Whether to log gated keyword detections
        block_dangerous_patterns: Whether to block DANGEROUS_PATTERNS (should always be True)
    """

    mode: str = "relaxed"  # relaxed | strict | audit-only
    audit_gated_keywords: bool = True
    block_dangerous_patterns: bool = True


@dataclass
class TrustTierResult:
    """Result of trust tier validation.

    Attributes:
        requires_gate: Whether human approval is needed before execution.
        reason: Human-readable explanation of why gate is required.
        matched_pattern: The specific pattern that triggered the gate.
        audited_keywords: Keywords detected but not blocked (in relaxed/audit-only mode).
    """

    requires_gate: bool
    reason: str | None = None
    matched_pattern: str | None = None
    audited_keywords: list[str] | None = None


# Cached config to avoid repeated file reads
_trust_tier_config_cache: TrustTierConfig | None = None


def get_trust_tier_config() -> TrustTierConfig:
    """Load trust tier configuration from gatesmith.json.

    Returns cached config if available. Falls back to relaxed defaults
    if config file is missing or invalid.

    Returns:
        TrustTierConfig with mode and audit settings.
    """
    global _trust_tier_config_cache

    if _trust_tier_config_cache is not None:
        return _trust_tier_config_cache

    config_paths = [
        Path.cwd() / "gatesmith.json",
        Path.cwd().parent / "gatesmith.json",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                daemon_config = config.get("daemon", {})
                ttv = daemon_config.get("trustTierValidation", {})
                _trust_tier_config_cache = TrustTierConfig(
                    mode=ttv.get("mode", "relaxed"),
                    audit_gated_keywords=ttv.get("auditGatedKeywords", True),
                    block_dangerous_patterns=ttv.get("blockDangerousPatterns", True),
                )
                return _trust_tier_config_cache
            except (json.JSONDecodeError, OSError):
                pass

    # Default to relaxed mode
    _trust_tier_config_cache = TrustTierConfig()
    return _trust_tier_config_cache


def audit_gated_keyword_detection(
    task: dict, matched_keywords: list[str], mode: str
) -> None:
    """Audit log when gated keywords are detected but not blocked.

    Writes to .company/logs/gated_keywords.jsonl for visibility
    without blocking task execution.

    Args:
        task: Task dict with task_id, title, description
        matched_keywords: List of patterns that matched
        mode: Current validation mode (relaxed/audit-only)
    """
    try:
        company_dir = Path.cwd() / ".company"
        logs_dir = company_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        audit_file = logs_dir / "gated_keywords.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task.get("task_id") or task.get("id", "unknown"),
            "title": task.get("title", "")[:100],
            "matched_keywords": matched_keywords,
            "mode": mode,
            "action": "allowed",  # Task was allowed to proceed
        }

        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Don't fail task execution on audit errors
        pass


def validate_task_before_execution(task: dict) -> tuple[bool, str | None]:
    """Pre-execution validation to catch bad task definitions early.

    Validates task structure and content before consuming a retry attempt.
    Returns (is_valid, rejection_reason). If invalid, the task should be
    escalated immediately rather than retried.

    Side effect: May update task["last_error_diagnosis"] in-place when
    re-diagnosing stale "unknown" categories with current patterns. This
    ensures downstream code uses the corrected diagnosis.

    Args:
        task: Task dictionary from work queue (may be mutated).

    Returns:
        Tuple of (is_valid: bool, reason: str | None).
        If is_valid is False, reason explains why.
    """
    # Must have a title — tasks use title as the primary task definition
    title = task.get("title", "").strip()
    if not title:
        return False, "Task has no title — cannot execute without description"

    # Title that is just a recovered placeholder with no real content
    description = task.get("description", "").strip()
    if title.startswith("(recovered)") and not description:
        return False, (
            "Recovered task with no description — "
            "original task definition was lost during recovery"
        )

    # Re-diagnose tasks with stale "unknown" category using current patterns.
    # Diagnosis patterns improve over time; tasks diagnosed with older code may
    # carry category=unknown when current patterns would classify them correctly.
    retry_history = task.get("retry_history", [])
    last_diagnosis = task.get("last_error_diagnosis") or {}  # Handle explicit None
    if last_diagnosis.get("category") == "unknown" and retry_history:
        last_error = (retry_history[-1].get("error") or "").strip()
        if last_error:
            exit_code = task.get("exit_code", 1)
            fresh = diagnose_error(last_error, exit_code)
            if fresh["category"] != "unknown":
                # Update stale diagnosis with current classification
                task["last_error_diagnosis"] = fresh
                last_diagnosis = fresh
                if not fresh.get("retryable", True):
                    return False, (
                        f"Re-diagnosed stale 'unknown' error as "
                        f"'{fresh['category']}' (non-retryable). "
                        f"Hint: {fresh.get('fix_hint', 'N/A')}"
                    )

    # Check for excessive retry history with the same error category —
    # if the same non-transient error repeats, stop retrying
    if len(retry_history) >= 2 and last_diagnosis.get("category") not in (
        "unknown",
        "timeout",
        "api_limit",
        "subprocess_error",
        "process_killed",
        "oom_killed",
    ):
        # Same non-transient error repeated — don't waste another attempt
        return False, (
            f"Task failed {len(retry_history)} times with "
            f"category={last_diagnosis.get('category')} — "
            "not a transient issue, escalating"
        )

    # Detect repeated identical error messages regardless of diagnosis category.
    # If the last 2+ errors have the same message, the task is stuck in a loop
    # even if the category is "unknown". Re-diagnose the error to check if
    # newer patterns can classify it as non-retryable.
    if len(retry_history) >= 2:
        recent_errors = [(h.get("error") or "").strip() for h in retry_history[-2:]]
        if recent_errors[0] and recent_errors[0] == recent_errors[1]:
            # Same error repeated — re-diagnose with current patterns
            exit_code = task.get("exit_code", 1)
            fresh_diagnosis = diagnose_error(recent_errors[-1], exit_code)
            if not fresh_diagnosis.get("retryable", True):
                return False, (
                    f"Task failed {len(retry_history)} times with identical "
                    f"error (re-diagnosed as {fresh_diagnosis['category']}). "
                    "Not retryable, escalating"
                )
            # Even if retryable, 2 identical errors suggest a stuck task
            if len(retry_history) >= 3:
                return False, (
                    f"Task failed {len(retry_history)} times with identical "
                    "error message — stuck in retry loop, escalating"
                )

    # Task description too short to be actionable — only reject truly empty titles
    # (single word with no description is still a valid task definition)
    combined_text = (title + " " + description).strip()
    if len(combined_text) < 5:
        return False, (
            "Task description too short to be actionable "
            f"({len(combined_text)} chars) — escalating for clarification"
        )

    return True, None


def validate_against_trust_tiers(
    task: dict, config: TrustTierConfig | None = None
) -> TrustTierResult:
    """Check if task operations require gated approval.

    Behavior depends on config mode:
    - "relaxed" (default): Trust hooks for gated keywords, only block DANGEROUS_PATTERNS
    - "strict": Block both gated keywords and dangerous patterns (legacy behavior)
    - "audit-only": Audit everything but never block

    Args:
        task: Task dictionary containing at minimum:
            - title: str (required)
            - description: str (optional)
            - commands: list[str] (optional) - shell commands to execute
        config: Optional TrustTierConfig. Loaded from gatesmith.json if not provided.

    Returns:
        TrustTierResult indicating whether gate approval is needed.

    Example (relaxed mode - default):
        >>> task = {"title": "Update deployment docs", "description": "Fix deploy guide"}
        >>> result = validate_against_trust_tiers(task)
        >>> result.requires_gate  # False - keyword in title doesn't block
        False
        >>> result.audited_keywords  # But it's logged
        ['deploy']

    Example (strict mode):
        >>> config = TrustTierConfig(mode="strict")
        >>> task = {"title": "Deploy to production"}
        >>> result = validate_against_trust_tiers(task, config)
        >>> result.requires_gate  # True - strict mode blocks
        True
    """
    if config is None:
        config = get_trust_tier_config()

    # Extract searchable text from task
    title = task.get("title", "")
    description = task.get("description", "")
    commands = task.get("commands", [])

    # Combine all text for pattern matching
    searchable_text = f"{title} {description} {' '.join(commands)}"
    searchable_text_lower = searchable_text.lower()

    # Track audited keywords (detected but not blocked)
    audited_keywords: list[str] = []

    # DANGEROUS_PATTERNS in commands: Always block unless audit-only mode
    if config.block_dangerous_patterns and config.mode != "audit-only":
        for command in commands:
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return TrustTierResult(
                        requires_gate=True,
                        reason=f"Command matches forbidden pattern: {pattern}",
                        matched_pattern=pattern,
                    )

    # GATED_KEYWORDS: Behavior depends on mode
    for pattern in GATED_KEYWORDS:
        if re.search(pattern, searchable_text_lower, re.IGNORECASE):
            if config.mode == "strict":
                # Strict mode: Block on gated keywords (legacy behavior)
                return TrustTierResult(
                    requires_gate=True,
                    reason=f"Operation matches gated keyword: {pattern}",
                    matched_pattern=pattern,
                )
            else:
                # Relaxed or audit-only: Record but don't block
                audited_keywords.append(pattern)

    # Audit detected keywords if configured
    if audited_keywords and config.audit_gated_keywords:
        audit_gated_keyword_detection(task, audited_keywords, config.mode)

    return TrustTierResult(
        requires_gate=False,
        audited_keywords=audited_keywords if audited_keywords else None,
    )


def get_trust_tier(operation: str) -> str:
    """Determine the trust tier for a given operation.

    Args:
        operation: The operation string to classify.

    Returns:
        One of: "free", "guarded", "gated", "forbidden"
    """
    operation_lower = operation.lower()

    # Check forbidden patterns first
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, operation_lower, re.IGNORECASE):
            return "forbidden"

    # Check gated keywords
    for pattern in GATED_KEYWORDS:
        if re.search(pattern, operation_lower, re.IGNORECASE):
            return "gated"

    # Check for guarded operations (modifying local state)
    guarded_patterns = [
        r"write",
        r"edit",
        r"mkdir",
        r"git\s+add",
        r"git\s+commit",
        r"lint",
        r"test",
        r"build",
    ]
    for pattern in guarded_patterns:
        if re.search(pattern, operation_lower, re.IGNORECASE):
            return "guarded"

    # Default to free (read-only)
    return "free"


def create_loop_escalation(
    task: dict, reason: str, escalation_type: str = ESCALATION_TYPE_GATED
) -> dict:
    """Create escalation for gated operation or max retries.

    Wraps escalation.escalate() with loop-specific context.

    Args:
        task: Task dictionary containing at minimum id, title, priority, and retry info.
        reason: Human-readable reason for the escalation.
        escalation_type: Type of escalation, one of:
            - "gated_operation": Task requires human approval for gated ops
            - "max_retries": Task failed after max retry attempts
            - "unrecoverable": Task hit unrecoverable error

    Returns:
        Escalation object with id from escalation.escalate().
    """
    _ensure_imports()

    task_id = task.get("id") or task.get("task_id", "unknown")
    task_title = task.get("title", "")
    try:
        task_priority = int(task.get("priority", 3))
    except (ValueError, TypeError):
        task_priority = 3
    retry_count = task.get("retry_count", 0)

    # Map escalation_type to trigger reason for escalation module
    trigger_map = {
        ESCALATION_TYPE_GATED: escalation_module.EscalationTrigger.EXPLICIT_BLOCK,
        ESCALATION_TYPE_MAX_RETRIES: escalation_module.EscalationTrigger.REPEATED_FAILURE,
        ESCALATION_TYPE_UNRECOVERABLE: escalation_module.EscalationTrigger.EXPLICIT_BLOCK,
    }
    trigger_reason = trigger_map.get(
        escalation_type, escalation_module.EscalationTrigger.EXPLICIT_BLOCK
    )

    # Build detailed notes with loop-specific context
    notes = (
        f"[{escalation_type}] {reason}\n"
        f"Task: {task_title}\n"
        f"Priority: {task_priority}\n"
        f"Retry count: {retry_count}\n"
        f"Loop agent: {LOOP_AGENT_ID}"
    )

    # Use the escalation module's escalate function
    escalation_result = escalation_module.escalate(
        task_id=task_id,
        reason=trigger_reason,
        notes=notes,
    )

    # Add escalation_type to result for loop tracking
    escalation_result["escalation_type"] = escalation_type
    escalation_result["loop_context"] = {
        "task_title": task_title,
        "task_priority": task_priority,
        "retry_count": retry_count,
        "loop_agent": LOOP_AGENT_ID,
    }

    return escalation_result


def format_gate_request(task: dict, result: TrustTierResult) -> str:
    """Format a gate approval request for human review.

    Args:
        task: The task requiring approval.
        result: The TrustTierResult explaining why gate is needed.

    Returns:
        Formatted string for display to user.
    """
    title = task.get("title", "Unknown task")
    description = task.get("description", "")

    lines = [
        "=" * 60,
        "GATE APPROVAL REQUIRED",
        "=" * 60,
        f"Task: {title}",
    ]

    if description:
        lines.append(f"Description: {description}")

    lines.extend(
        [
            "",
            f"Reason: {result.reason}",
            f"Matched pattern: {result.matched_pattern}",
            "",
            "This operation has external consequences and requires",
            "human approval before execution.",
            "=" * 60,
        ]
    )

    return "\n".join(lines)


# =============================================================================
# Session State Integration (Task 1.3)
# =============================================================================


class LoopMetrics(TypedDict, total=False):
    """Metrics tracked by operation loop.

    All fields are optional for backward compatibility with existing
    session_state.json files that don't have loop_metrics.

    Attributes:
        tasks_claimed: Total number of tasks claimed by the loop.
        tasks_completed: Total number of tasks successfully completed.
        tasks_failed: Total number of tasks that failed (may retry).
        tasks_escalated: Total number of tasks escalated for human review.
        last_poll_time: ISO timestamp of the last poll operation.
        last_task_id: ID of the most recently processed task.
        current_task_id: ID of task currently being processed (None if idle).
        consecutive_idle_polls: Count of consecutive polls with no work.
        errors: List of recent errors [{time, task_id, error}], max 10 entries.
    """

    tasks_claimed: int
    tasks_completed: int
    tasks_failed: int
    tasks_escalated: int
    last_poll_time: str  # ISO timestamp
    last_task_id: str
    current_task_id: str | None
    consecutive_idle_polls: int
    errors: list  # [{time, task_id, error}], max 10 entries (FIFO)


class EconomicsMetrics(TypedDict, total=False):
    """Effort metrics for capacity planning (NOT cost - per REQUIREMENTS.md Out of Scope).

    This TypedDict tracks task execution effort (duration/time), not monetary costs.
    Per REQUIREMENTS.md, financial cost tracking is explicitly out of scope.
    These metrics enable capacity planning and performance analysis.

    All fields are optional for backward compatibility with existing
    session_state.json files that don't have economics_metrics.

    Attributes:
        total_tasks_attempted: Total number of tasks attempted (including retries).
        total_tasks_completed: Total number of tasks successfully completed.
        total_retry_attempts: Total number of retry attempts across all tasks.
        total_execution_time_seconds: Cumulative execution time in seconds.
        average_task_duration_seconds: Average task duration (total_time / completed).
        estimated_duration_per_task: Rolling average for capacity planning.
            Uses formula: (old * 0.7) + (new * 0.3) to smooth fluctuations.
        longest_task_id: ID of the task with the longest execution time.
        longest_task_duration_seconds: Duration of the longest task in seconds.
    """

    total_tasks_attempted: int
    total_tasks_completed: int
    total_retry_attempts: int
    total_execution_time_seconds: float
    average_task_duration_seconds: float
    estimated_duration_per_task: float  # Rolling average: (old * 0.7) + (new * 0.3)
    longest_task_id: str
    longest_task_duration_seconds: float


def _default_economics_metrics() -> dict:
    """Return default economics metrics for initialization.

    Called when economics_metrics is missing from session state.
    """
    return {
        "total_tasks_attempted": 0,
        "total_tasks_completed": 0,
        "total_retry_attempts": 0,
        "total_execution_time_seconds": 0.0,
        "average_task_duration_seconds": 0.0,
        "estimated_duration_per_task": 0.0,
        "longest_task_id": "",
        "longest_task_duration_seconds": 0.0,
    }


def update_economics_metrics(
    state_path: Path, task_id: str, duration_seconds: float, was_retry: bool = False
) -> dict:
    """Update economics metrics after task execution.

    Tracks effort (duration), NOT cost - per REQUIREMENTS.md Out of Scope.
    This function updates the economics metrics in the session state after
    a task has been executed.

    Args:
        state_path: Path to the session_state.json file.
        task_id: ID of the task that was executed.
        duration_seconds: Execution duration in seconds.
        was_retry: Whether this was a retry attempt.

    Returns:
        Updated session state dict.

    Rolling Average Formula:
        estimated_duration_per_task = (old * 0.7) + (new * 0.3)
        This smooths out fluctuations while being responsive to recent changes.

    Example:
        >>> update_economics_metrics(path, "TASK-123", 45.5, was_retry=False)
        {'economics_metrics': {'total_tasks_attempted': 1, ...}}

        >>> update_economics_metrics(path, "TASK-456", 30.0, was_retry=True)
        # Increments total_retry_attempts as well
    """
    state = load_session_state(state_path)

    # Get or initialize economics_metrics
    if "economics_metrics" not in state:
        state["economics_metrics"] = _default_economics_metrics()

    metrics = state["economics_metrics"]

    # Update attempt counts
    metrics["total_tasks_attempted"] = metrics.get("total_tasks_attempted", 0) + 1

    if was_retry:
        metrics["total_retry_attempts"] = metrics.get("total_retry_attempts", 0) + 1
    else:
        # Only count as completed if not a retry (successful first attempt)
        # Note: Retries that succeed will be tracked separately when they complete
        metrics["total_tasks_completed"] = metrics.get("total_tasks_completed", 0) + 1

    # Update execution time
    old_total_time = metrics.get("total_execution_time_seconds", 0.0)
    metrics["total_execution_time_seconds"] = old_total_time + duration_seconds

    # Update average duration
    completed = metrics.get("total_tasks_completed", 0)
    if completed > 0:
        metrics["average_task_duration_seconds"] = (
            metrics["total_execution_time_seconds"] / completed
        )

    # Update rolling average for capacity planning
    # Formula: (old * 0.7) + (new * 0.3)
    old_estimate = metrics.get("estimated_duration_per_task", 0.0)
    if old_estimate == 0.0:
        # First task - use actual duration as initial estimate
        metrics["estimated_duration_per_task"] = duration_seconds
    else:
        metrics["estimated_duration_per_task"] = (old_estimate * 0.7) + (
            duration_seconds * 0.3
        )

    # Track longest task
    longest_duration = metrics.get("longest_task_duration_seconds", 0.0)
    if duration_seconds > longest_duration:
        metrics["longest_task_id"] = task_id
        metrics["longest_task_duration_seconds"] = duration_seconds

    state["economics_metrics"] = metrics
    save_session_state(state_path, state)
    return state


def _default_loop_metrics() -> dict:
    """Return default loop metrics for initialization.

    Called when loop_metrics is missing from session state.
    """
    return {
        "tasks_claimed": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_escalated": 0,
        "consecutive_idle_polls": 0,
        "errors": [],
    }


def load_session_state(state_path: Path) -> dict:
    """Load session state with backward compatibility.

    If the file doesn't exist or is invalid, returns a new state with default
    loop_metrics. If the file exists but loop_metrics is missing, adds
    default loop_metrics to the loaded state.

    Args:
        state_path: Path to the session_state.json file.

    Returns:
        Session state dict with loop_metrics guaranteed to exist.
    """
    if not state_path.exists():
        return {"loop_metrics": _default_loop_metrics()}

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupted file - return fresh state
        return {"loop_metrics": _default_loop_metrics()}

    # Ensure loop_metrics exists (backward compatibility)
    if "loop_metrics" not in state:
        state["loop_metrics"] = _default_loop_metrics()

    return state


def save_session_state(state_path: Path, state: dict) -> None:
    """Save session state atomically.

    Adds/updates the updated_at timestamp before saving.

    Args:
        state_path: Path to the session_state.json file.
        state: Session state dict to save.
    """
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Ensure parent directory exists
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically by writing to temp file and renaming
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def update_loop_metrics(state_path: Path, **updates) -> dict:
    """Update loop metrics atomically.

    Merges updates into existing loop_metrics. For the 'errors' key,
    if the value is a dict, it's appended to the errors list (FIFO)
    with a maximum of 10 entries maintained.

    Args:
        state_path: Path to the session_state.json file.
        **updates: Key-value pairs to update in loop_metrics.
            Special handling for 'errors': dict value is appended to list.

    Returns:
        Updated session state dict.

    Example:
        # Increment completed count
        update_loop_metrics(path, tasks_completed=5)

        # Add an error (appended to list, FIFO max 10)
        update_loop_metrics(path, errors={
            "time": "2026-02-10T12:00:00Z",
            "task_id": "TASK-123",
            "error": "Connection timeout"
        })
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())

    for key, value in updates.items():
        if key == "errors" and isinstance(value, dict):
            # Append to errors list, maintain max 10 (FIFO)
            metrics.setdefault("errors", []).append(value)
            metrics["errors"] = metrics["errors"][-10:]
        else:
            metrics[key] = value

    state["loop_metrics"] = metrics
    save_session_state(state_path, state)
    return state


# =============================================================================
# Loop Metrics Recording Functions (Task 2.3)
# =============================================================================


def record_task_claimed(state_path: Path, task_id: str) -> None:
    """Record task claim in metrics.

    Updates:
    - tasks_claimed += 1
    - current_task_id = task_id
    - last_task_id = task_id
    - consecutive_idle_polls = 0

    Args:
        state_path: Path to the session_state.json file.
        task_id: ID of the task being claimed.
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())
    current_claimed = metrics.get("tasks_claimed", 0)

    update_loop_metrics(
        state_path,
        tasks_claimed=current_claimed + 1,
        current_task_id=task_id,
        last_task_id=task_id,
        consecutive_idle_polls=0,
    )


def record_task_completed(state_path: Path, task_id: str) -> None:
    """Record successful completion.

    Updates:
    - tasks_completed += 1
    - current_task_id = None
    - last_poll_time = now

    Args:
        state_path: Path to the session_state.json file.
        task_id: ID of the task that was completed.
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())
    current_completed = metrics.get("tasks_completed", 0)

    update_loop_metrics(
        state_path,
        tasks_completed=current_completed + 1,
        current_task_id=None,
        last_poll_time=datetime.now(timezone.utc).isoformat(),
    )


def record_task_failed(state_path: Path, task_id: str, error: str) -> None:
    """Record task failure.

    Updates:
    - tasks_failed += 1
    - current_task_id = None
    - errors.append({time: now, task_id, error})  # Max 10, FIFO

    Args:
        state_path: Path to the session_state.json file.
        task_id: ID of the task that failed.
        error: Error message describing the failure.
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())
    current_failed = metrics.get("tasks_failed", 0)

    update_loop_metrics(
        state_path,
        tasks_failed=current_failed + 1,
        current_task_id=None,
        errors={
            "time": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "error": error,
        },
    )


def record_task_escalated(state_path: Path, task_id: str) -> None:
    """Record escalation.

    Updates:
    - tasks_escalated += 1
    - current_task_id = None

    Args:
        state_path: Path to the session_state.json file.
        task_id: ID of the task that was escalated.
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())
    current_escalated = metrics.get("tasks_escalated", 0)

    update_loop_metrics(
        state_path,
        tasks_escalated=current_escalated + 1,
        current_task_id=None,
    )


def record_idle_poll(state_path: Path) -> None:
    """Record poll with no work.

    Updates:
    - consecutive_idle_polls += 1
    - last_poll_time = now

    Args:
        state_path: Path to the session_state.json file.
    """
    state = load_session_state(state_path)
    metrics = state.get("loop_metrics", _default_loop_metrics())
    current_idle = metrics.get("consecutive_idle_polls", 0)

    update_loop_metrics(
        state_path,
        consecutive_idle_polls=current_idle + 1,
        last_poll_time=datetime.now(timezone.utc).isoformat(),
    )


# =============================================================================
# Operation Loop Core Functions (Task 1.1)
# =============================================================================


def get_claimable_tasks(queue_path: Path) -> list[dict]:
    """
    Return tasks eligible for claiming.

    Filtering criteria:
    1. status == "pending"
    2. All dependencies satisfied (dependency task status == "completed")
    3. NOT in backoff: backoff_until is None OR datetime.fromisoformat(backoff_until) < now

    Sort: effective_priority ASC (with age-based boosting), then created ASC

    P96 Age-Based Priority Boosting:
    - Tasks gain +1 effective priority every 4 hours of waiting
    - Maximum boost is 2 levels (P4 can reach P2, P3 can reach P1)
    - Prevents low-priority task starvation

    Args:
        queue_path: Path to the work queue JSON file

    Returns:
        List of task dictionaries eligible for claiming, sorted by priority
    """
    if not queue_path.exists():
        return []

    try:
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    pending_tasks = queue.get("pending", [])
    # Collect both task_ids AND source_goals from completed tasks
    # This allows dependencies to reference either format (task_id or goal ID)
    completed_ids = set()
    for task in queue.get("completed", []):
        completed_ids.add(task.get("task_id"))
        if task.get("source_goal"):
            completed_ids.add(task["source_goal"])
    now = datetime.now(timezone.utc)

    claimable = []

    for task in pending_tasks:
        # Check 1: Must be pending (already filtered by getting from pending list)

        # Check 2: All dependencies must be satisfied (check both task_id and source_goal)
        dependencies = task.get("dependencies", [])
        if dependencies:
            if not all(dep_id in completed_ids for dep_id in dependencies):
                continue

        # Check 3: Must not be in backoff period
        # Only a string ISO timestamp is a valid backoff. A non-string value
        # (corrupted queue JSON storing an int/float/dict/list) would raise
        # AttributeError on .replace() — which `except (ValueError, TypeError)`
        # does NOT catch — crashing get_claimable_tasks and stalling ALL task
        # claiming for the cycle. Gate on isinstance so a non-string is treated
        # as "no backoff" rather than an unhandled crash.
        backoff_until = task.get("backoff_until")
        if isinstance(backoff_until, str):
            try:
                backoff_time = datetime.fromisoformat(
                    backoff_until.replace("Z", "+00:00")
                )
                if backoff_time > now:
                    continue  # Still in backoff
            except (ValueError, TypeError):
                pass  # Invalid backoff timestamp string, treat as no backoff

        claimable.append(task)

    # P96: Age-based priority boosting
    # Tasks waiting longer get higher effective priority to prevent starvation.
    # Every HOURS_PER_BOOST hours, effective priority improves by 1 level.
    # Maximum boost is capped so tasks don't all become P1 immediately.
    HOURS_PER_BOOST = 4  # Hours of waiting to gain one priority level
    MAX_BOOST = 2  # Maximum priority levels a task can gain from aging

    def sort_key(task: dict) -> tuple:
        try:
            base_priority = int(task.get("priority", 3))
        except (ValueError, TypeError):
            base_priority = 3

        created = task.get("created_at", "")

        # Calculate age in hours
        age_hours = 0.0
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = (now - created_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        # Calculate age boost (capped at MAX_BOOST)
        age_boost = min(age_hours / HOURS_PER_BOOST, MAX_BOOST)

        # Effective priority (lower = higher priority)
        # Minimum effective priority is 1 (P1)
        effective_priority = max(1.0, base_priority - age_boost)

        # Sort by effective priority, then by created_at (older first)
        return (effective_priority, created)

    claimable.sort(key=sort_key)

    return claimable


def claim_task(
    queue_path: Path, task_id: str, agent_id: str, _lock_held: bool = False
) -> dict:
    """
    Atomically claim a task for execution.

    Uses file locking to prevent race conditions.
    Sets status="in_progress", claimed_by=agent_id, claimed_at=now.
    Initializes retry metadata if not present.

    Args:
        queue_path: Path to the work queue JSON file
        task_id: ID of the task to claim
        agent_id: ID of the agent claiming the task
        _lock_held: Internal flag — if True, caller already holds the lock,
                    so skip lock acquisition to prevent deadlock.

    Returns:
        Dict with keys:
        - success: bool
        - task: dict | None (the claimed task if successful)
        - error: str | None (error message if failed)
    """
    _ensure_imports()
    company_dir = (
        queue_path.parent.parent
    )  # queue_path is .company/state/work_queue.json
    lock_path = company_dir / "runtime/queue.lock"

    # WS-074: Use contextlib to conditionally acquire lock
    # If caller already holds lock (_lock_held=True), use nullcontext to skip
    import contextlib

    lock_context = (
        contextlib.nullcontext() if _lock_held else work_allocator.QueueLock(lock_path)
    )

    try:
        with lock_context:
            if not queue_path.exists():
                return {
                    "success": False,
                    "task": None,
                    "error": "Queue file does not exist",
                }

            try:
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return {
                    "success": False,
                    "task": None,
                    "error": f"Failed to read queue: {e}",
                }

            # Find the task in pending
            pending = queue.get("pending", [])
            claimed_task = None

            for task in pending:
                if task.get("task_id") == task_id:
                    claimed_task = task
                    break

            if claimed_task is None:
                return {
                    "success": False,
                    "task": None,
                    "error": f"Task {task_id} not found in pending queue",
                }

            # Update task metadata
            now = datetime.now(timezone.utc).isoformat()
            claimed_task["status"] = "in_progress"
            claimed_task["claimed_by"] = agent_id
            claimed_task["claimed_at"] = now
            # Preserve original assigned_to if it's an employee name (not a daemon ID)
            # so employee_activator can route to the intended employee
            original_assigned = claimed_task.get("assigned_to")
            if not original_assigned or "daemon-" in str(original_assigned):
                claimed_task["assigned_to"] = agent_id
            claimed_task["assigned_at"] = now
            claimed_task["started_at"] = now

            # Initialize retry metadata if not present
            if "retry_count" not in claimed_task:
                claimed_task["retry_count"] = 0
            if "retry_history" not in claimed_task:
                claimed_task["retry_history"] = []

            # Move from pending to in_progress
            queue["pending"] = [t for t in pending if t.get("task_id") != task_id]
            if "in_progress" not in queue:
                queue["in_progress"] = []
            queue["in_progress"].append(claimed_task)

            # Update metadata
            if "metadata" not in queue:
                queue["metadata"] = {}
            queue["metadata"]["last_modified"] = now

            # Save queue (atomic write to prevent corruption)
            try:
                import os as _os
                import tempfile as _tf

                fd, tmp = _tf.mkstemp(
                    dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
                )
                try:
                    with _os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    _os.replace(tmp, str(queue_path))
                except Exception:
                    if _os.path.exists(tmp):
                        _os.unlink(tmp)
                    raise
            except OSError as e:
                return {
                    "success": False,
                    "task": None,
                    "error": f"Failed to save queue: {e}",
                }

            return {
                "success": True,
                "task": claimed_task,
                "error": None,
            }

    except TimeoutError as e:
        return {
            "success": False,
            "task": None,
            "error": f"Lock timeout: {e}",
        }


def release_task(
    queue_path: Path,
    task_id: str,
    result: str,
    error: str | None = None,
    state_path: Path | None = None,
    employee_id: str | None = None,
    pr_metadata: dict | None = None,
) -> dict:
    """
    Release a task after execution attempt.

    Args:
        queue_path: Path to the work queue JSON file
        task_id: ID of the task to release
        result: Result of execution - "completed" | "failed" | "escalated" | "blocked"
        error: Optional error message (for failed/escalated/blocked results)
        state_path: Optional path to session state for metrics updates (Task 2.2)
        employee_id: Optional employee ID who executed the task (for attribution)
        pr_metadata: Optional PR info dict with keys: pr_url, branch_name (P99)

    Note (P50 backfill): Historical completed tasks have executed_by but not
    completed_by. A one-time migration should be added in forge_daemon.py
    (near start_daemon or run_daemon_loop initialization) that iterates
    queue["completed"], copies executed_by -> completed_by for tasks missing
    completed_by, and gates the migration with a state flag
    (e.g. "completed_by_backfill_done" in session_state.json).

    Returns:
        Dict with keys:
        - success: bool
        - task: dict (the released task)
        - escalation_created: bool (True if escalation was triggered)

    Behavior by result:
        - "completed": Set status="completed", completed_at=now
        - "failed": Increment retry, check max retries, create escalation if exceeded
        - "escalated": Set status="escalated"
        - "blocked": Set status="blocked" (deliverable gate held the PR for manual
          review). No retry, no escalation record — the work is fine, a human just
          reviews/merges the held PR. Sets blocked_reason so it is not re-released.

    Escalation Integration (Task 2.2):
        - When max retries are exceeded, creates escalation with type "max_retries"
        - Updates session metrics (tasks_escalated) if state_path provided
    """
    from datetime import timedelta

    _ensure_imports()
    company_dir = (
        queue_path.parent.parent
    )  # queue_path is .company/state/work_queue.json
    lock_path = company_dir / "runtime/queue.lock"
    escalation_created = False

    try:
        with work_allocator.QueueLock(lock_path):
            if not queue_path.exists():
                return {
                    "success": False,
                    "task": None,
                    "escalation_created": False,
                    "error": "Queue file does not exist",
                }

            try:
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return {
                    "success": False,
                    "task": None,
                    "escalation_created": False,
                    "error": f"Failed to read queue: {e}",
                }

            # QUEUE-CANCEL-STATUS: a task an operator cancelled or held while
            # its worker ran stays where the operator put it — the (killed or
            # late) worker's outcome is recorded on it, nothing else happens:
            # no retry, no pr_open, no escalation, no failure record.
            _parked = work_allocator.absorb_late_outcome(
                queue,
                task_id,
                result,
                pr_url=(pr_metadata or {}).get("pr_url"),
                error=error,
            )
            if isinstance(_parked, dict):
                work_allocator.write_queue_file(queue_path, queue)
                return {
                    "success": True,
                    "task": _parked,
                    "escalation_created": False,
                    "operator_parked": True,
                }

            # Find the task in in_progress
            in_progress = queue.get("in_progress", [])
            released_task = None

            for task in in_progress:
                if task.get("task_id") == task_id:
                    released_task = task
                    break

            if released_task is None:
                # Recovery: task may have been moved by concurrent access,
                # planning decomposition, or daemon restart. Search other
                # sections and still record the outcome. (P50 fix)
                for section in ("pending", "blocked"):
                    for task in queue.get(section, []):
                        if task.get("task_id") == task_id:
                            released_task = task
                            queue[section].remove(task)
                            break
                    if released_task:
                        break

            if released_task is None:
                # Truly gone — synthesize a minimal record so the result
                # is still tracked in completed history.
                # Include a description placeholder so downstream validation
                # (validate_task_before_execution) doesn't reject it as an
                # orphan with lost metadata.
                released_task = {
                    "task_id": task_id,
                    "title": f"(recovered) {task_id}",
                    "description": (
                        f"[Auto-recovered] Original task metadata was lost. "
                        f"Task ID: {task_id}. Check escalation records or "
                        f"completed history for original context."
                    ),
                    "recovered_from": "missing_in_progress",
                }

            now = datetime.now(timezone.utc).isoformat()

            # Handle based on result
            if result == "completed":
                # A genuine success also clears any open infra-outage streak.
                infra_failure.reset_infra_streak(company_dir)

                # When a PR was created, park the task in pr_open until merge
                # is confirmed by the reconcile loop. Only move to completed
                # directly when there is no PR (e.g. pure infra / docs tasks).
                #
                # Completion-at-merge invariant (#54): also check pr_url already
                # on the task dict — MARK_COMPLETE via failure_recovery fires on a
                # "failed" worker result BEFORE the workflow judge writes its verdict,
                # so the caller (forge_daemon) may not pass pr_metadata even though
                # the task record already carries the unmerged PR's url.  If we only
                # inspect pr_metadata we silently bypass the pr_open gate and the task
                # lands in completed with an unmerged PR (the bug for tasks
                # task-20260708003852-34a07d and task-20260707232600-6c7508).
                _param_pr_url = pr_metadata.get("pr_url") if pr_metadata else None
                _task_pr_url = released_task.get("pr_url")
                _effective_pr_url = _param_pr_url or _task_pr_url
                _has_pr = bool(_effective_pr_url)

                if _has_pr:
                    released_task["status"] = "pr_open"
                    released_task["pr_open_at"] = now
                    released_task["result"] = "pr_open"
                    released_task["pr_url"] = _effective_pr_url
                    if pr_metadata and pr_metadata.get("branch_name"):
                        released_task["branch_name"] = pr_metadata["branch_name"]
                else:
                    released_task["status"] = "completed"
                    released_task["completed_at"] = now
                    released_task["result"] = "completed"
                    # P99: Store PR metadata for completions display
                    if pr_metadata:
                        if pr_metadata.get("pr_url"):
                            released_task["pr_url"] = pr_metadata["pr_url"]
                        if pr_metadata.get("branch_name"):
                            released_task["branch_name"] = pr_metadata["branch_name"]

                # Resolve any active escalation for this task
                try:
                    _ensure_imports()
                    escalation_module.resolve_escalation(
                        task_id=task_id,
                        resolution="Task completed successfully",
                        resolved_by=employee_id or "operation-loop",
                    )
                except Exception:
                    pass  # Non-fatal — escalation may not exist

                # Update assigned_to with actual employee if provided
                if employee_id:
                    released_task["assigned_to"] = employee_id
                    released_task["executed_by"] = employee_id
                    released_task["completed_by"] = employee_id

                # Record efficiency metrics (G6 Economics)
                try:
                    _ensure_imports()
                    # Calculate duration from started_at to now
                    started_at = released_task.get("started_at")
                    duration_seconds = 0.0
                    if started_at:
                        try:
                            start_time = datetime.fromisoformat(
                                started_at.replace("Z", "+00:00")
                            )
                            end_time = datetime.fromisoformat(
                                now.replace("Z", "+00:00")
                            )
                            duration_seconds = (end_time - start_time).total_seconds()
                        except (ValueError, TypeError):
                            pass

                    # Get employee ID from assignment (use parameter if provided)
                    actual_employee_id = (
                        employee_id
                        or released_task.get("assigned_to")
                        or released_task.get("claimed_by")
                    )

                    # Determine complexity from task metadata or default to standard
                    complexity = released_task.get("complexity", "standard")

                    # Check if this was a retry (first_pass = no retries)
                    retry_count = released_task.get("retry_count", 0)
                    first_pass = retry_count == 0

                    # Check for escalation
                    was_escalated = released_task.get("escalation_id") is not None

                    # Record the task execution for efficiency tracking
                    if efficiency_tracker and actual_employee_id:
                        # Convert seconds to minutes for efficiency tracker
                        duration_minutes = duration_seconds / 60.0
                        efficiency_tracker.record_task_execution(
                            task_id=task_id,
                            employee_id=actual_employee_id,
                            complexity=complexity,
                            duration_minutes=duration_minutes,
                            success=True,
                            first_pass=first_pass,
                            retry_count=retry_count,
                            escalated=was_escalated,
                            pattern_tags=released_task.get("tags", []),
                        )
                except Exception:
                    # Efficiency tracking failure should not block task release
                    pass

                # Remove from in_progress
                queue["in_progress"] = [
                    t for t in in_progress if t.get("task_id") != task_id
                ]

                if _has_pr:
                    # Add to pr_open lane (deduplicate to prevent bloat)
                    if "pr_open" not in queue:
                        queue["pr_open"] = []
                    queue["pr_open"] = [
                        t for t in queue["pr_open"] if t.get("task_id") != task_id
                    ]
                    queue["pr_open"].append(released_task)
                else:
                    # Add to completed (deduplicate to prevent bloat)
                    if "completed" not in queue:
                        queue["completed"] = []
                    queue["completed"] = [
                        t for t in queue["completed"] if t.get("task_id") != task_id
                    ]
                    queue["completed"].append(released_task)

            elif result == "failed":
                # Preserve any PR/branch a (possibly phantom-downgraded) run still
                # produced, so the returned task carries the deliverable into
                # failure_recovery — its has_deliverable check can then recognise
                # a genuine success and MARK_COMPLETE it instead of escalating
                # shipped-and-merged work (the success_misdetected → "Exhausted
                # retries" → blocked bug; #1083 shipped while #4c2502 was failed).
                if pr_metadata:
                    if pr_metadata.get("pr_url"):
                        released_task["pr_url"] = pr_metadata["pr_url"]
                    if pr_metadata.get("branch_name"):
                        released_task["branch_name"] = pr_metadata["branch_name"]

                # Classify infra-vs-task failure origin so pure infra blips
                # (provider preflight timeouts, CI runs with 0 steps executed)
                # don't consume the task's retry budget or get it escalated
                # as a genuine "failed 3x" — see infra_failure.py.
                failure_origin = infra_failure.classify_failure_origin(error)
                released_task["failure_origin"] = failure_origin.value

                if failure_origin is infra_failure.FailureOrigin.INFRA:
                    infra_failure.record_infra_failure(
                        company_dir, task_id, error or ""
                    )
                    released_task["last_error"] = error
                    released_task["result"] = "failed"
                    released_task["infra_failure_count"] = (
                        released_task.get("infra_failure_count", 0) + 1
                    )
                    released_task["backoff_until"] = (
                        datetime.now(timezone.utc)
                        + timedelta(
                            seconds=infra_failure.backoff_seconds(
                                released_task["infra_failure_count"]
                            )
                        )
                    ).isoformat()

                    # Clear assignment
                    released_task["claimed_by"] = None
                    released_task["claimed_at"] = None
                    released_task["assigned_to"] = None
                    released_task["assigned_at"] = None

                    # Move back to pending for retry — retry_count /
                    # retry_history / max_attempts are NEVER touched here,
                    # so infra blips never consume the task's retry budget.
                    queue["in_progress"] = [
                        t for t in in_progress if t.get("task_id") != task_id
                    ]
                    if "pending" not in queue:
                        queue["pending"] = []
                    queue["pending"].append(released_task)
                else:
                    infra_failure.reset_infra_streak(company_dir)

                    # Increment retry count
                    retry_count = released_task.get("retry_count", 0) + 1
                    released_task["retry_count"] = retry_count
                    max_attempts = released_task.get(
                        "max_attempts", DEFAULT_MAX_ATTEMPTS
                    )

                    # Record retry in history
                    if "retry_history" not in released_task:
                        released_task["retry_history"] = []
                    released_task["retry_history"].append(
                        {
                            "attempt": retry_count,
                            "failed_at": now,
                            "error": error,
                        }
                    )

                    # Diagnose error and determine retry strategy (Task 50.8)
                    exit_code = released_task.get("exit_code", 1)
                    diagnosis = diagnose_error(error or "", exit_code)
                    old_diag = released_task.get("last_error_diagnosis") or {}
                    old_category = old_diag.get("category", "unknown")
                    released_task["last_error_diagnosis"] = diagnosis
                    if old_category != diagnosis["category"]:
                        logger.info(
                            "Task %s diagnosis updated: %s -> %s (retryable: %s)",
                            task_id,
                            old_category,
                            diagnosis["category"],
                            diagnosis["retryable"],
                        )
                    logger.warning(
                        "Task %s failed — diagnosis: category=%s, retryable=%s, hint=%s",
                        task_id,
                        diagnosis["category"],
                        diagnosis["retryable"],
                        diagnosis["fix_hint"],
                    )

                    # Structured failure logging for metrics/reporting (P93)
                    try:
                        import json as _json

                        # Derive the log path from the QUEUE we were handed, not
                        # from __file__. The two resolve to the same real path in
                        # production (queue_path is .company/state/work_queue.json,
                        # so its parent IS .company/state), but a __file__ anchor
                        # ignored the isolated queue_path tests pass — so every
                        # test that released a task appended to the REAL failure
                        # log. That polluted G7's success-rate assessment (94% of
                        # 21k entries were fixtures like task-rescue/task-001).
                        _failure_log = queue_path.parent / "task_failure_log.jsonl"
                        _log_entry = {
                            "timestamp": now,
                            "task_id": task_id,
                            "title": released_task.get("title", ""),
                            "employee_id": (
                                released_task.get("assigned_to")
                                or released_task.get("claimed_by")
                            ),
                            "exit_code": exit_code,
                            "category": diagnosis["category"],
                            "retryable": diagnosis["retryable"],
                            "retry_count": released_task.get("retry_count", 0),
                            "complexity": released_task.get("complexity", "standard"),
                            "error_snippet": (error or "")[:200],
                        }
                        with open(_failure_log, "a") as _f:
                            _f.write(_json.dumps(_log_entry) + "\n")
                    except Exception:
                        pass

                    # If error is not retryable, skip further retries
                    if not diagnosis["retryable"]:
                        released_task["retry_count"] = max_attempts

                    # Record failed attempt for efficiency tracking (G6 Economics)
                    try:
                        _ensure_imports()
                        started_at = released_task.get("started_at")
                        duration_seconds = 0.0
                        if started_at:
                            try:
                                start_time = datetime.fromisoformat(
                                    started_at.replace("Z", "+00:00")
                                )
                                end_time = datetime.fromisoformat(
                                    now.replace("Z", "+00:00")
                                )
                                duration_seconds = (
                                    end_time - start_time
                                ).total_seconds()
                            except (ValueError, TypeError):
                                pass

                        employee_id = released_task.get(
                            "assigned_to"
                        ) or released_task.get("claimed_by")
                        complexity = released_task.get("complexity", "standard")

                        # Record the failed execution
                        if efficiency_tracker and employee_id:
                            # Convert seconds to minutes for efficiency tracker
                            duration_minutes = duration_seconds / 60.0
                            efficiency_tracker.record_task_execution(
                                task_id=task_id,
                                employee_id=employee_id,
                                complexity=complexity,
                                duration_minutes=duration_minutes,
                                success=False,
                                first_pass=False,  # Failed attempts are never first-pass success
                                retry_count=retry_count,
                                escalated=False,
                                pattern_tags=released_task.get("tags", []),
                            )
                    except Exception:
                        # Efficiency tracking failure should not block task release
                        pass

                    # Check if max retries exceeded (Task 2.2)
                    if retry_count >= max_attempts:
                        # P84-fix: Before escalating, check if Agent Teams can
                        # rescue this task with a debugging-investigation team.
                        team_already_tried = released_task.get(
                            "agent_team_attempted", False
                        )
                        team_rescue = False
                        if not team_already_tried:
                            try:
                                from . import team_executor as _te
                            except ImportError:
                                try:
                                    import team_executor as _te  # type: ignore[no-redef]
                                except ImportError:
                                    _te = None
                            if _te is not None:
                                try:
                                    _tc = _te.load_agent_teams_config()
                                    if (
                                        _tc.get("enabled")
                                        and _tc.get("experimentalAcknowledged")
                                        and _tc.get("triggerConditions", {}).get(
                                            "stuckTasks", True
                                        )
                                    ):
                                        team_rescue = True
                                except Exception:
                                    pass

                        if team_rescue:
                            # Re-queue for one team attempt before escalating
                            logger.info(
                                "[P84] AGENT TEAMS RESCUE: task %s failed %d "
                                "times, re-queuing for team execution",
                                task_id,
                                retry_count,
                            )
                            released_task["agent_team_attempted"] = True
                            released_task["max_attempts"] = retry_count + 1
                            released_task["backoff_until"] = (
                                datetime.now(timezone.utc) + timedelta(seconds=30)
                            ).isoformat()
                            released_task["last_error"] = error
                            released_task["result"] = "failed"
                            released_task["claimed_by"] = None
                            released_task["claimed_at"] = None
                            released_task["assigned_to"] = None
                            released_task["assigned_at"] = None
                            queue["in_progress"] = [
                                t for t in in_progress if t.get("task_id") != task_id
                            ]
                            if "pending" not in queue:
                                queue["pending"] = []
                            queue["pending"].append(released_task)
                        else:
                            # No team rescue - escalate as before
                            try:
                                escalation_result = create_loop_escalation(
                                    task=released_task,
                                    reason=f"Task failed after {retry_count} "
                                    f"attempts. Last error: {error or 'Unknown'}",
                                    escalation_type=ESCALATION_TYPE_MAX_RETRIES,
                                )
                                escalation_created = True
                                released_task["escalation_id"] = escalation_result.get(
                                    "task_id"
                                )
                                if state_path:
                                    current_state = load_session_state(state_path)
                                    current_escalated = current_state.get(
                                        "loop_metrics", {}
                                    ).get("tasks_escalated", 0)
                                    update_loop_metrics(
                                        state_path,
                                        tasks_escalated=current_escalated + 1,
                                        last_task_id=task_id,
                                    )
                            except Exception:
                                pass

                            released_task["status"] = "failed"
                            released_task["failed_at"] = now
                            released_task["last_error"] = error
                            released_task["result"] = "failed"
                            queue["in_progress"] = [
                                t for t in in_progress if t.get("task_id") != task_id
                            ]
                            if "blocked" not in queue:
                                queue["blocked"] = []
                            queue["blocked"].append(released_task)
                    else:
                        # Still have retries left - calculate backoff
                        # Formula: min(60 * (2 ** attempts), 3600)
                        backoff_seconds = min(BASE_DELAY * (2**retry_count), MAX_DELAY)
                        backoff_until = datetime.now(timezone.utc) + timedelta(
                            seconds=backoff_seconds
                        )
                        released_task["backoff_until"] = backoff_until.isoformat()
                        released_task["last_error"] = error
                        released_task["result"] = "failed"

                        # Clear assignment
                        released_task["claimed_by"] = None
                        released_task["claimed_at"] = None
                        released_task["assigned_to"] = None
                        released_task["assigned_at"] = None

                        # Move back to pending for retry
                        queue["in_progress"] = [
                            t for t in in_progress if t.get("task_id") != task_id
                        ]
                        if "pending" not in queue:
                            queue["pending"] = []
                        queue["pending"].append(released_task)

            elif result == "escalated":
                released_task["status"] = "escalated"
                released_task["escalated_at"] = now
                released_task["escalation_reason"] = error
                released_task["result"] = "escalated"
                # Terminal until a human resolves it (/respond). Without a
                # blocked_reason, the WS-017 stranded sweep and the startup
                # orphan cleanup treat the blocked entry as recoverable and
                # release it back to pending every ~30 min — an infinite
                # spawn→fail→escalate loop (2026-06-11: ~20 respawns/task
                # overnight).
                released_task["blocked_reason"] = (
                    f"Escalated: {error or 'awaiting human resolution'}"[:300]
                )

                # Record escalated task for efficiency tracking (G6 Economics)
                try:
                    _ensure_imports()
                    started_at = released_task.get("started_at")
                    duration_seconds = 0.0
                    if started_at:
                        try:
                            start_time = datetime.fromisoformat(
                                started_at.replace("Z", "+00:00")
                            )
                            end_time = datetime.fromisoformat(
                                now.replace("Z", "+00:00")
                            )
                            duration_seconds = (end_time - start_time).total_seconds()
                        except (ValueError, TypeError):
                            pass

                    employee_id = released_task.get("assigned_to") or released_task.get(
                        "claimed_by"
                    )
                    complexity = released_task.get("complexity", "standard")
                    retry_count = released_task.get("retry_count", 0)

                    # Record the escalated execution
                    if efficiency_tracker and employee_id:
                        # Convert seconds to minutes for efficiency tracker
                        duration_minutes = duration_seconds / 60.0
                        efficiency_tracker.record_task_execution(
                            task_id=task_id,
                            employee_id=employee_id,
                            complexity=complexity,
                            duration_minutes=duration_minutes,
                            success=False,
                            first_pass=False,
                            retry_count=retry_count,
                            escalated=True,  # Mark as escalated
                            pattern_tags=released_task.get("tags", []),
                        )
                except Exception:
                    # Efficiency tracking failure should not block task release
                    pass

                # Move to blocked (escalated tasks are blocked until resolved)
                queue["in_progress"] = [
                    t for t in in_progress if t.get("task_id") != task_id
                ]
                if "blocked" not in queue:
                    queue["blocked"] = []
                queue["blocked"].append(released_task)

                # Create escalation record
                try:
                    escalation_module.escalate(
                        task_id=task_id,
                        reason=escalation_module.EscalationTrigger.REPEATED_FAILURE,
                        notes=error,
                    )
                    escalation_created = True
                except Exception:
                    # Escalation creation failed but we still released the task
                    pass

            elif result == "blocked":
                # Phase 2 honesty: the pre-merge deliverable gate held this task's
                # PR for manual review (labelled needs-manual-review, left open).
                # The work IS done — a PR exists — but it must NOT count as an
                # autonomous completion (that inflates the metrics). It is also
                # NOT a failure: do not retry, the work is fine; a human just has
                # to review/merge the held PR. autonomy_metrics maps status
                # "blocked" -> blocked_escalated, so this is honestly excluded
                # from the completed-with-PR funnel.
                released_task["status"] = "blocked"
                released_task["blocked_at"] = now
                released_task["result"] = "blocked"
                # blocked_reason is REQUIRED: without it the WS-017 stranded sweep
                # and the startup orphan cleanup treat the entry as recoverable and
                # re-release it to pending (~30 min spawn loop). Same hazard the
                # escalated branch guards against above.
                released_task["blocked_reason"] = (
                    f"Deliverable gate: PR held for manual review — "
                    f"{error or 'phantom/non-deliverable'}"[:300]
                )

                # Preserve the held PR so a reviewer can find it (P99).
                if pr_metadata:
                    if pr_metadata.get("pr_url"):
                        released_task["pr_url"] = pr_metadata["pr_url"]
                    if pr_metadata.get("branch_name"):
                        released_task["branch_name"] = pr_metadata["branch_name"]

                if employee_id:
                    released_task["assigned_to"] = employee_id
                    released_task["executed_by"] = employee_id

                # Move to blocked (deduplicate to avoid bloat)
                queue["in_progress"] = [
                    t for t in in_progress if t.get("task_id") != task_id
                ]
                if "blocked" not in queue:
                    queue["blocked"] = []
                queue["blocked"] = [
                    t for t in queue["blocked"] if t.get("task_id") != task_id
                ]
                queue["blocked"].append(released_task)

            else:
                return {
                    "success": False,
                    "task": None,
                    "escalation_created": False,
                    "error": f"Invalid result: {result}. Must be 'completed', 'failed', 'escalated', or 'blocked'",
                }

            # Update metadata
            if "metadata" not in queue:
                queue["metadata"] = {}
            queue["metadata"]["last_modified"] = now

            # Save queue (atomic write to prevent corruption)
            try:
                import os as _os
                import tempfile as _tf

                fd, tmp = _tf.mkstemp(
                    dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
                )
                try:
                    with _os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    _os.replace(tmp, str(queue_path))
                except Exception:
                    if _os.path.exists(tmp):
                        _os.unlink(tmp)
                    raise
            except OSError as e:
                return {
                    "success": False,
                    "task": None,
                    "escalation_created": False,
                    "error": f"Failed to save queue: {e}",
                }

            return {
                "success": True,
                "task": released_task,
                "escalation_created": escalation_created,
            }

    except TimeoutError as e:
        return {
            "success": False,
            "task": None,
            "escalation_created": False,
            "error": f"Lock timeout: {e}",
        }


def _update_roadmap_on_completion(task: dict) -> None:
    """
    Update ROADMAP.md and roadmap state when a planning task completes.

    Extracts the roadmap_task_id from task notes metadata, then:
    1. Marks the task completed in roadmap_state.json
    2. Updates the task's status attribute in ROADMAP.md XML

    Non-fatal: errors are silently ignored to avoid blocking the loop.
    """
    _ensure_imports()

    # Extract roadmap metadata from task notes
    notes = task.get("notes", [])
    roadmap_task_id = None
    for note in notes if isinstance(notes, list) else []:
        content = note.get("content", "") if isinstance(note, dict) else str(note)
        try:
            parsed = json.loads(content)
            if "roadmap_task_id" in parsed:
                roadmap_task_id = parsed["roadmap_task_id"]
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if not roadmap_task_id:
        return

    try:
        roadmap_scheduler.mark_task_completed(roadmap_task_id)
        roadmap_scheduler.update_roadmap_task_status(roadmap_task_id, "complete")
    except Exception:
        pass  # Non-fatal — best-effort write-back


_OPERATOR_STOPPABLE_ACTIONS = ("executed", "failed", "escalated", "blocked")


def _report_operator_stop(queue_path: Path, result: dict) -> dict:
    """Re-label the outcome of a task an operator cancelled or held mid-run.

    QUEUE-CANCEL-STATUS: release_task leaves such a task where the operator
    put it; this makes the daemon see that too. "cancelled"/"held" match none
    of its outcome branches, so a killed worker is neither a failure (no
    WS-105 recovery, no breaker count, no failure log) nor a success. A PR the
    worker opened after a cancel is closed here — nothing else would.
    """
    try:
        if not isinstance(result, dict):
            return result
        action = result.get("action")
        task_id = result.get("task_id")
        if action not in _OPERATOR_STOPPABLE_ACTIONS or not task_id:
            return result
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)
        for t in queue.get("cancelled", []) or []:
            if isinstance(t, dict) and t.get("task_id") == task_id:
                return {
                    **result,
                    "action": "cancelled",
                    "original_action": action,
                    "reason": f"cancelled by {t.get('cancelled_by') or 'operator'}: "
                    f"{t.get('cancel_reason') or ''}",
                    "late_prs_closed": _close_late_prs(t),
                }
        for t in queue.get("blocked", []) or []:
            if (
                isinstance(t, dict)
                and t.get("task_id") == task_id
                and t.get("operator_hold") is True
            ):
                return {**result, "action": "held", "original_action": action}
    except (OSError, ValueError, TypeError):
        pass
    return result


def _close_late_prs(task: dict) -> list[str]:
    """task_cancel.close_late_prs, never raising (see _report_operator_stop)."""
    try:
        try:
            from . import task_cancel as _tc
        except ImportError:
            import task_cancel as _tc  # type: ignore[no-redef]
        return _tc.close_late_prs(task)
    except Exception:
        return []


def poll_and_execute_once(
    queue_path: Path,
    agent_id: str,
    state_path: Path | None = None,
    target_task_id: str | None = None,
    execution_cwd: str | None = None,
) -> dict:
    """Single iteration of the operation loop (see _poll_and_execute_once).

    A task cancelled or held by an operator while it ran reports action
    "cancelled" / "held" instead of its worker's outcome.
    """
    result = _poll_and_execute_once(
        queue_path=queue_path,
        agent_id=agent_id,
        state_path=state_path,
        target_task_id=target_task_id,
        execution_cwd=execution_cwd,
    )
    return _report_operator_stop(queue_path, result)


def _poll_and_execute_once(
    queue_path: Path,
    agent_id: str,
    state_path: Path | None = None,
    target_task_id: str | None = None,
    execution_cwd: str | None = None,
) -> dict:
    """
    Single iteration of the operation loop.

    MVP scope:
    1. Get claimable tasks
    2. If none: return {action: "idle", reason: "no_tasks"}
    3. Claim highest priority task (or specific target_task_id if provided)
    4. Validate against trust tiers (Task 2.2)
    5. For MVP: log intent, mark complete (real agent invocation in P5)
    6. Release task with result
    7. Return {action: "executed" | "failed" | "escalated", task_id: ..., result: ...}

    Args:
        queue_path: Path to the work queue JSON file
        agent_id: ID of the agent executing the loop
        state_path: Optional path to session state for metrics updates
        target_task_id: Optional specific task ID to claim (prevents race conditions
                       when planning was done for a specific task)

    Returns:
        Dict with keys:
        - action: "idle" | "executed" | "failed" | "claim_failed" | "escalated"
        - reason: str (explanation)
        - task_id: str | None (ID of task if claimed)
        - result: str | None (execution result if applicable)
        - task: dict | None (task details if applicable)
        - escalation: dict | None (escalation result if applicable)
    """
    # Step 1-3: Get claimable tasks and claim atomically under a single lock
    # to prevent TOCTOU race where another worker claims between read and claim.
    _ensure_imports()
    company_dir = (
        queue_path.parent.parent
    )  # queue_path is .company/state/work_queue.json
    lock_path = company_dir / "runtime/queue.lock"

    claimed_task = None
    claim_task_id = None
    claim_result = None  # WS-057: Initialize to prevent NameError in edge cases

    try:
        with work_allocator.QueueLock(lock_path):
            # Read claimable tasks under lock
            claimable = get_claimable_tasks(queue_path)

            if not claimable:
                return {
                    "action": "idle",
                    "reason": "no_tasks",
                    "task_id": None,
                    "result": None,
                    "task": None,
                    "escalation": None,
                }

            if target_task_id:
                target_task = next(
                    (t for t in claimable if t.get("task_id") == target_task_id),
                    None,
                )
                if not target_task:
                    return {
                        "action": "claim_failed",
                        "reason": f"Target task {target_task_id} not found in claimable queue",
                        "task_id": target_task_id,
                        "result": None,
                        "task": None,
                        "escalation": None,
                    }
                # WS-074: Pass _lock_held=True since we already hold the lock
                claim_result = claim_task(
                    queue_path, target_task_id, agent_id, _lock_held=True
                )
                if claim_result.get("success"):
                    claimed_task = claim_result.get("task")
                    claim_task_id = target_task_id
            else:
                # Try each claimable task in priority order
                for candidate in claimable:
                    cand_id = candidate.get("task_id")
                    # WS-074: Pass _lock_held=True since we already hold the lock
                    claim_result = claim_task(
                        queue_path, cand_id, agent_id, _lock_held=True
                    )
                    if claim_result.get("success"):
                        claimed_task = claim_result.get("task")
                        claim_task_id = cand_id
                        break
    except TimeoutError:
        return {
            "action": "claim_failed",
            "reason": "Could not acquire queue lock for atomic claim",
            "task_id": target_task_id,
            "result": None,
            "task": None,
            "escalation": None,
        }
    # Lock is released here — before task execution begins

    if claimed_task is None:
        return {
            "action": "claim_failed",
            "reason": claim_result.get(
                "error", "All claimable tasks were claimed by other workers"
            )
            if claim_result
            else "No claim attempted",
            "task_id": claim_task_id
            or (claimable[0].get("task_id") if claimable else None),
            "result": None,
            "task": None,
            "escalation": None,
        }

    task_id = claim_task_id

    # WS-057: NoneType guard — claimed_task must be a dict for downstream .get() calls
    if not isinstance(claimed_task, dict):
        logger.warning(
            "Task %s: claimed_task is %s, not dict — releasing as failed",
            task_id,
            type(claimed_task).__name__,
        )
        try:
            release_task(
                queue_path=queue_path,
                task_id=task_id,
                result="failed",
                error=f"claimed_task was {type(claimed_task).__name__}, expected dict",
            )
        except Exception:
            pass
        return {
            "action": "failed",
            "reason": f"claimed_task is {type(claimed_task).__name__}, not dict",
            "task_id": task_id,
            "result": None,
            "task": None,
            "escalation": None,
        }

    # Step 4: Validate against trust tiers (Task 2.2)
    trust_result = validate_against_trust_tiers(claimed_task)

    if trust_result.requires_gate:
        # Create escalation for gated operation
        escalation_result = create_loop_escalation(
            task=claimed_task,
            reason=trust_result.reason or "Operation requires human approval",
            escalation_type=ESCALATION_TYPE_GATED,
        )

        # Update session metrics if state_path provided
        if state_path:
            update_loop_metrics(
                state_path,
                tasks_escalated=load_session_state(state_path)
                .get("loop_metrics", {})
                .get("tasks_escalated", 0)
                + 1,
                last_task_id=task_id,
                current_task_id=None,
            )

        # Release task as escalated
        release_result = release_task(
            queue_path=queue_path,
            task_id=task_id,
            result="escalated",
            error=trust_result.reason,
        )

        return {
            "action": "escalated",
            "reason": trust_result.reason or "Gated operation requires approval",
            "task_id": task_id,
            "result": "escalated",
            "task": release_result.get("task")
            if release_result.get("success")
            else claimed_task,
            "escalation": escalation_result,
        }

    # Step 4b: Pre-execution validation — catch bad task definitions early
    # to avoid wasting retry attempts on tasks that will never succeed.
    task_valid, rejection_reason = validate_task_before_execution(claimed_task)
    if not task_valid:
        logger.warning(
            "Task %s failed pre-execution validation: %s",
            task_id,
            rejection_reason,
        )
        # Escalate immediately — don't consume a retry attempt
        escalation_result = create_loop_escalation(
            task=claimed_task,
            reason=f"Pre-execution validation failed: {rejection_reason}",
            escalation_type=ESCALATION_TYPE_UNRECOVERABLE,
        )
        release_task(
            queue_path=queue_path,
            task_id=task_id,
            result="escalated",
            error=rejection_reason,
        )
        return {
            "action": "escalated",
            "reason": rejection_reason,
            "task_id": task_id,
            "result": "escalated",
            "task": claimed_task,
            "escalation": escalation_result,
        }

    # CHALLENGER-STAGE: a challenger or continuation task runs the stage's own
    # flow (PR branch checkout, blind prompt, push to the existing PR, label).
    # A broken stage module must never stop builder tasks; a stage task it
    # cannot run fails (never reaches the employee activator, which would
    # open a new PR for it).
    try:
        try:
            from . import challenger_stage as _cs
        except ImportError:
            import challenger_stage as _cs  # type: ignore[no-redef]
    except Exception as _cs_err:  # noqa: BLE001
        _cs = None
        if claimed_task.get("source") == "challenger":
            _cs_reason = f"challenger stage unavailable: {_cs_err}"[:300]
            release_task(
                queue_path=queue_path,
                task_id=claim_task_id,
                result="failed",
                error=_cs_reason,
            )
            return {
                "action": "failed",
                "reason": _cs_reason,
                "task_id": claim_task_id,
                "result": "failed",
                "task": claimed_task,
                "escalation": None,
            }
    if _cs is not None and _cs.is_stage_task(claimed_task):
        return _cs.execute_claimed(
            claimed_task,
            queue_path=queue_path,
            execution_cwd=execution_cwd,
            release=release_task,
        )

    # AUTO-REBASE-CONFLICTING-PRS (#714): a REBASE task is run by the rebase
    # stage, never by the normal worker (which would open a second PR).
    if claimed_task.get("source") == "rebase":
        try:
            try:
                from . import pr_rebase as _rb
            except ImportError:
                import pr_rebase as _rb  # type: ignore[no-redef]
            _rb_result = _rb.dispatch_claimed(
                claimed_task,
                queue_path=queue_path,
                execution_cwd=execution_cwd,
                release=release_task,
            )
        except Exception as _rb_err:  # noqa: BLE001
            _rb_reason = f"rebase stage unavailable: {_rb_err}"[:300]
            release_task(
                queue_path=queue_path,
                task_id=claim_task_id,
                result="completed",
                error=_rb_reason,
            )
            return {
                "action": "executed",
                "reason": _rb_reason,
                "task_id": claim_task_id,
                "result": "completed",
                "task": claimed_task,
                "escalation": None,
            }
        if _rb_result is not None:
            return _rb_result

    # WS-117: Preemptive recovery — inject avoidance hints from failure patterns
    try:
        from recovery_preempt import inject_hints_into_task

        company_dir = queue_path.parent
        if not (company_dir / "state").is_dir():
            company_dir = queue_path.parent  # .company/state/work_queue.json → .company
        inject_hints_into_task(claimed_task, company_dir)
    except Exception:
        pass  # Non-fatal — hints are best-effort

    # P81: Snapshot working tree BEFORE task execution so PR only includes
    # files changed by THIS task, not accumulated dirty working tree.
    _ensure_imports()
    pre_task_snapshot = None
    if pr_output_manager is not None:
        try:
            pre_task_snapshot = pr_output_manager.snapshot_working_tree()
        except Exception:
            pass  # Non-fatal — falls back to all-changes behavior

    # Step 5: Employee Activation (P16) or Agent Team Execution (P84)
    # Route task to matched employee with context and execute
    # P84: If Agent Teams feature is enabled and task qualifies, use team execution

    activation_config = employee_activator.get_activation_config()

    if activation_config.get("enabled", True):
        # P84: Check if this task should use Agent Teams
        team_execution_used = False
        # Phase 2 (workflow execution): default-off schema-validated path.
        workflow_execution_used = False
        try:
            from . import team_executor
        except ImportError:
            try:
                import team_executor  # type: ignore[no-redef]
            except ImportError:
                team_executor = None

        if team_executor is not None:
            try:
                team_config = team_executor.load_agent_teams_config()
                if team_executor.should_use_team(claimed_task, team_config):
                    # Determine trigger reason for visibility
                    _cx = claimed_task.get(
                        "complexity",
                        claimed_task.get("estimated_complexity", "standard"),
                    )
                    _rc = claimed_task.get("retry_count", 0)
                    _rescue = claimed_task.get("agent_team_attempted", False)
                    if _rescue:
                        _trigger = "RESCUE after %d failures" % _rc
                    elif _cx in ["epic", "complex"]:
                        _trigger = "complexity=%s" % _cx
                    elif _rc >= team_config.get("triggerConditions", {}).get(
                        "stuckRetryThreshold", 2
                    ):
                        _trigger = "stuck (%d retries)" % _rc
                    else:
                        _trigger = "manual"

                    # Match employee first (needed as team lead)
                    lead_id = employee_activator.match_employee_for_task(claimed_task)
                    if lead_id:
                        logger.info(
                            "[P84] AGENT TEAMS ACTIVATED: task=%s trigger=%s lead=%s",
                            task_id,
                            _trigger,
                            lead_id,
                        )
                        composition = team_executor.compose_team(
                            claimed_task, lead_id, team_config
                        )
                        # WS-092: Pass execution_cwd to team executor for worktree isolation
                        team_result = team_executor.execute_with_team(
                            claimed_task,
                            composition,
                            team_config,
                            project_dir=Path(execution_cwd) if execution_cwd else None,
                        )

                        # Team-path WS-103 harvest: teams write files in the
                        # worktree but cannot commit them — git add/commit/push
                        # are disallowed tools by design, the harness commits
                        # for workers. The single-agent path harvests via
                        # _capture_code_changes inside activate_employee_for_task;
                        # this path never did, so team work died with the
                        # worktree and WS-119 1.8 failed the task ("no PR was
                        # created") despite a genuinely successful team session
                        # (observed on the 2026-06-12 canary: 3 runs wrote
                        # passing tests, each lost to worktree cleanup).
                        team_git_capture: dict = {"captured": False}
                        if team_result.success and execution_cwd:
                            # Harvest ONLY in worktree-isolated runs. Without
                            # execution_cwd the capture root would be the
                            # daemon's main working tree, where an unscoped
                            # git-diff sweep bundles unrelated dirty state
                            # (and any uncommitted human work) into a pushed
                            # PR while its non-empty files_changed defeats
                            # the phantom check below. Main-tree
                            # (poll-in-place) team runs keep pre-existing
                            # semantics: the P81 snapshot-scoped,
                            # secrets-gated auto-PR workflow downstream picks
                            # up their changes.
                            try:
                                team_git_capture = (
                                    employee_activator._capture_code_changes(
                                        claimed_task, lead_id, Path(execution_cwd)
                                    )
                                )
                            except Exception as _cap_err:
                                team_git_capture = {
                                    "captured": False,
                                    "error": str(_cap_err)[:200],
                                }
                            # PR-less-but-changed counts as delivered here so
                            # the WS-119 guard downstream decides (mirrors the
                            # single-agent P95 semantics); downgrading it to a
                            # team failure would rerun activation on a worktree
                            # whose changes are already committed — the exact
                            # double-activation bug PR #992 fixed.
                            _delivered = bool(
                                team_git_capture.get("pr_url")
                                or team_git_capture.get("captured")
                                or team_git_capture.get("files_changed")
                            )
                            # Only a CLEAN no-deliverable capture is a
                            # phantom. An errored capture (transient git
                            # failure, or a harvest bug like a renamed
                            # helper) means the team may well have delivered
                            # — surface it through the WS-119 fail-for-retry
                            # path instead of silently re-executing the whole
                            # task on top of the team's uncommitted work.
                            # requires_deliverable uses `is not False` so
                            # only an explicit False opts out — an explicit
                            # null in queue JSON must not bypass the gate
                            # (the PR #953 missing-field class).
                            if (
                                not _delivered
                                and not team_git_capture.get("error")
                                and claimed_task.get("requires_deliverable", True)
                                is not False
                            ):
                                # Phantom team success: session exited 0 with
                                # plausible output but no file changes were
                                # captured. Downgrade to failure so credit
                                # distribution stays honest and the proven
                                # single-agent fallback below takes over.
                                logger.warning(
                                    "[P84] AGENT TEAMS PHANTOM: task=%s team "
                                    "reported success but no deliverable was "
                                    "captured (no file changes in worktree) "
                                    "— downgrading to failure",
                                    task_id,
                                )
                                team_result.success = False
                                team_result.error = (
                                    "Phantom team success: no deliverable "
                                    "captured from worktree (no file changes)"
                                )
                        elif team_result.success and isinstance(
                            getattr(team_result, "git_capture", None), dict
                        ):
                            # No execution_cwd: the team self-isolated and
                            # harvested in ITS OWN worktree (a daemon/team-wt-*
                            # branch), possibly opening the PR there. Take that
                            # capture, or the PR is invisible here and WS-119
                            # fails the task as "no PR was created" while the
                            # PR sits in review.
                            team_git_capture = team_result.git_capture

                        # Record team results (credit distribution).
                        # Guarded: an exception here would fall through to
                        # the broad P84 handler with team_execution_used
                        # still False, re-running single-agent activation on
                        # a worktree whose changes the harvest above may
                        # already have pushed as a PR (duplicate-PR leak).
                        # Credit bookkeeping must never affect control flow.
                        try:
                            team_executor.record_team_results(
                                composition, claimed_task, team_result, team_config
                            )
                        except Exception as _rec_err:
                            logger.warning(
                                "[P84] record_team_results failed (non-fatal): %s",
                                _rec_err,
                            )

                        # Map team result to activation_result format
                        activation_result = {
                            "success": team_result.success,
                            "employee_id": lead_id,
                            "message": team_result.output[:500]
                            if team_result.output
                            else "",
                            "execution_mode": "agent-team",
                            "team_size": team_result.team_size,
                            "pattern": team_result.pattern_used,
                            "employee_activation": {"employee_id": lead_id},
                            # WS-103: downstream PR-dedup (_activation_pr),
                            # WS-119 guard, and pr_metadata all read this.
                            "git_capture": team_git_capture,
                        }
                        if not team_result.success:
                            # 2026-09-20: a second session is never started in
                            # a worktree the team session changed under the
                            # guarded paths. The activator's integrity check
                            # refuses it (rightly), and used to do so AFTER the
                            # point of no return: recorded as a security
                            # violation, nothing captured, task re-queued,
                            # worktree and branch deleted. LEDGER-2, LEDGER-3
                            # and LEDGER-4b each lost whole builds that way.
                            _fallback = team_executor.prepare_single_agent_fallback(
                                claimed_task, lead_id, execution_cwd
                            )
                            if not _fallback["fallback"]:
                                logger.warning(
                                    "[P84] AGENT TEAMS FAILED: task=%s "
                                    "pattern=%s team_size=%d error=%s "
                                    "-- NO fallback: %s",
                                    task_id,
                                    team_result.pattern_used,
                                    team_result.team_size,
                                    team_result.error,
                                    _fallback["reason"],
                                )
                                activation_result["git_capture"] = _fallback[
                                    "git_capture"
                                ]
                                activation_result["message"] = (
                                    f"{team_result.error or 'team execution failed'}"
                                    f" — {_fallback['reason']}"
                                )
                                team_execution_used = True
                        if not team_result.success and not team_execution_used:
                            logger.warning(
                                "[P84] AGENT TEAMS FAILED: task=%s "
                                "pattern=%s team_size=%d error=%s "
                                "-- falling back to single-agent",
                                task_id,
                                team_result.pattern_used,
                                team_result.team_size,
                                team_result.error,
                            )
                            activation_result = (
                                employee_activator.activate_employee_for_task(
                                    task=claimed_task,
                                    fallback_agent_id=agent_id,
                                    execution_cwd=execution_cwd,
                                )
                            )
                            # Fallback already ran the activation; without
                            # this flag the standard path below runs it a
                            # second time, and the second pass fails the
                            # P95 deliverable guard because the worktree's
                            # changes were already committed and pushed.
                            team_execution_used = True
                        elif team_result.success:
                            logger.info(
                                "[P84] AGENT TEAMS SUCCESS: task=%s "
                                "pattern=%s team_size=%d pr=%s",
                                task_id,
                                team_result.pattern_used,
                                team_result.team_size,
                                team_git_capture.get("pr_url") or "none",
                            )
                            team_execution_used = True
            except Exception as e:
                logger.debug(
                    "[P84] Agent Teams check failed: %s, using single-agent",
                    e,
                )

        # Phase 2: schema-validated workflow execution (default OFF). Mirrors the
        # team branch — gated by config, fail-closed. A {"fell_back": True} result
        # (path disabled / never ran) leaves workflow_execution_used False so the
        # standard single-agent flow below runs; any exception does the same. All
        # logic lives in workflow_executor (unprotected); this is a thin dispatch.
        if not team_execution_used:
            try:
                from . import workflow_executor
            except ImportError:
                try:
                    import workflow_executor  # type: ignore[no-redef]
                except ImportError:
                    workflow_executor = None

            if workflow_executor is not None and workflow_executor.should_use_workflow(
                claimed_task
            ):
                try:
                    _wf_result = workflow_executor.execute_task_workflow(
                        claimed_task,
                        execution_cwd=execution_cwd,
                        fallback_agent_id=agent_id,
                    )
                    if isinstance(_wf_result, dict) and not _wf_result.get("fell_back"):
                        activation_result = _wf_result
                        workflow_execution_used = True
                except Exception as e:  # noqa: BLE001 — never let workflow break the loop
                    logger.warning(
                        "[Workflow] execution failed, falling back to single-agent: %s",
                        e,
                    )

        if not team_execution_used and not workflow_execution_used:
            # Standard single-agent employee activation flow
            activation_result = employee_activator.activate_employee_for_task(
                task=claimed_task,
                fallback_agent_id=agent_id,
                execution_cwd=execution_cwd,
            )

        # WS-057: Guard against activation_result being None/non-dict
        if not isinstance(activation_result, dict):
            logger.warning(
                "Task %s: activation_result is %s, not dict",
                task_id,
                type(activation_result).__name__
                if activation_result is not None
                else "None",
            )
            activation_result = {
                "success": False,
                "reason": "activation returned non-dict result",
                "message": "activation returned non-dict result",
            }

        # P24 Fix: Update task with actual employee ID for correct efficiency tracking
        # The activation result contains the matched employee_id which should be used
        # instead of the daemon's agent_id for attribution and efficiency recording
        matched_employee = activation_result.get("employee_id")
        if matched_employee:
            claimed_task["assigned_to"] = matched_employee
            claimed_task["executed_by"] = (
                matched_employee  # Explicit execution attribution
            )

        # P85: Persist team execution metadata on the task for queue monitor
        if activation_result.get("execution_mode") == "agent-team":
            claimed_task["execution_mode"] = "agent-team"
            claimed_task["team_size"] = activation_result.get("team_size", 3)
            claimed_task["team_pattern"] = activation_result.get("pattern", "")
        elif activation_result.get("execution_mode") == "workflow":
            claimed_task["execution_mode"] = "workflow"
            _wf_verdict = activation_result.get("workflow_verdict")
            if isinstance(_wf_verdict, dict):
                claimed_task["workflow_verdict"] = _wf_verdict

        # P87: Re-save queue with updated task metadata (assigned_to, execution_mode, etc.)
        # so queue monitor can display accurate team info during execution.
        try:
            with open(queue_path, encoding="utf-8") as _qf:
                _queue = json.load(_qf)
            # Update the in_progress task with our modifications
            for _idx, _t in enumerate(_queue.get("in_progress", [])):
                if _t.get("task_id") == claimed_task.get("task_id"):
                    _queue["in_progress"][_idx] = claimed_task
                    break
            # Atomic save
            import os as _os
            import tempfile as _tf

            _fd, _tmp = _tf.mkstemp(
                dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
            )
            try:
                with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                    json.dump(_queue, _f, indent=2)
                _os.replace(_tmp, str(queue_path))
            except Exception:
                if _os.path.exists(_tmp):
                    _os.unlink(_tmp)
        except Exception as _e:
            logger.warning("P87: Failed to update queue with team metadata: %s", _e)

        # Determine success and extract result
        execution_success = activation_result.get("success", False)
        execution_error = None
        if not execution_success:
            execution_error = activation_result.get(
                "message", activation_result.get("reason", "Unknown error")
            )

        # P27: PR workflow for plan-driven tasks
        # Duplicate-PR fix: the employee activation may already have opened a PR
        # via WS-103 git-capture (activation_result.git_capture.pr_url). When it
        # has, neither the pr_workflow nor the auto_pr path below must run, or the
        # SAME task opens two PRs on two branches (observed in the B-diagnostic:
        # #1021+#1022 and #1023+#1024).
        # A PR for this task can already exist without the capture knowing
        # (gh timed out after GitHub created it, a harvest opened it). Look for
        # it BEFORE _activation_pr is read, so the deliverable gate runs on it,
        # the auto-PR workflow does not open a twin, and WS-119 below sees it.
        if (
            execution_success
            and claimed_task.get("requires_deliverable", True) is not False
        ):
            try:
                try:
                    from . import pr_reality_check as _prc
                except ImportError:
                    import pr_reality_check as _prc  # type: ignore[no-redef]
                activation_result["git_capture"] = _prc.recover_pr_into_capture(
                    activation_result.get("git_capture"),
                    claimed_task,
                    project_root=queue_path.parent.parent.parent,
                )
            except Exception as _prc_err:  # noqa: BLE001 — recovery is best-effort
                logger.debug("PR recovery skipped for %s: %s", task_id, _prc_err)

        _activation_pr = (activation_result.get("git_capture", {}) or {}).get("pr_url")

        # Phase 2/3: a WS-103 git-capture PR (the worker opened its own PR) skips
        # BOTH execute_pr_workflow and execute_auto_pr_workflow below — so without
        # this hook the pre-merge deliverable gate never runs on it (the gap that
        # let off-target worker PR #1069 through ungated). Run the gate here with
        # daemon_executed=True (this is daemon-executed work regardless of the
        # branch name the worker chose). Best-effort + fail-safe: it only applies a
        # needs-manual-review label/comment, NEVER closes the PR or fails the task.
        # FIX (calibrate audit 2026-07-06): capture the gate result so
        # deliverable_blocked is set and the task is released as "blocked", not
        # "completed", when the gate fires. Previously the result was discarded.
        _activation_gate_result: dict | None = None
        if execution_success and pr_output_manager is not None and _activation_pr:
            try:
                _gc = activation_result.get("git_capture", {}) or {}
                _activation_gate_result = pr_output_manager.run_deliverable_gate(
                    claimed_task.get("task_id", task_id),
                    claimed_task.get("title", ""),
                    claimed_task.get("description", ""),
                    _activation_pr,
                    _gc.get("branch"),
                    daemon_executed=True,
                )
            except Exception as _gate_exc:  # noqa: BLE001 — gate must never break the loop
                logger.warning(
                    "Deliverable gate on git-capture PR failed (non-fatal): %s",
                    _gate_exc,
                )
                if pr_output_manager is not None:
                    pr_output_manager.write_gate_skip(
                        claimed_task.get("task_id", task_id),
                        _activation_pr,
                        f"gate raised exception: {str(_gate_exc)[:200]}",
                    )

        pr_workflow_result = None
        if execution_success and pr_output_manager is not None and not _activation_pr:
            if pr_output_manager.should_use_pr_workflow(claimed_task):
                pr_workflow_result = pr_output_manager.execute_pr_workflow(
                    task_id=claimed_task.get("roadmap_task_id", task_id),
                    task_title=claimed_task.get("title", ""),
                    task_description=claimed_task.get("description", ""),
                )
                # P88: If PR workflow failed, mark task as FAILED so it can be retried.
                # Previously, execution_success stayed True and task was marked COMPLETED
                # even when no PR was created — losing all work silently.
                if not pr_workflow_result.success:
                    execution_success = False
                    if pr_workflow_result.tests_passed is False:
                        execution_error = (
                            pr_workflow_result.error or "Tests failed — no PR created"
                        )
                    elif pr_workflow_result.error:
                        execution_error = (
                            pr_workflow_result.error or "PR creation failed"
                        )

        # Auto-PR workflow for daemon tasks (non-planning)
        auto_pr_result = None
        if (
            execution_success
            and pr_output_manager is not None
            and not pr_workflow_result
            and not _activation_pr
        ):
            auto_pr_result = pr_output_manager.execute_auto_pr_workflow(
                task_id=claimed_task.get("task_id", task_id),
                task_title=claimed_task.get("title", ""),
                task_description=claimed_task.get("description", ""),
                employee_id=matched_employee,
                pre_task_snapshot=pre_task_snapshot,
            )
            if auto_pr_result and not auto_pr_result.success:
                if auto_pr_result.error and "Secrets detected" in auto_pr_result.error:
                    execution_success = False
                    execution_error = "Security gate blocked: secrets in changes"
                else:
                    pr_err = auto_pr_result.error or "unknown error"
                    # P88: Mark as failed so task can be retried with working PR
                    execution_success = False
                    execution_error = f"PR creation failed: {pr_err}"
                    logger.warning(
                        f"P88: PR creation failed for task {task_id}: {pr_err} — marking FAILED"
                    )

        # Phase 2 honesty fix: the pre-merge deliverable gate can hold a PR for
        # manual review (labels it needs-manual-review, leaves it open) while the
        # auto-PR workflow still returns success=True (the work + PR exist). Such
        # a task must be released as "blocked", NOT "completed" — otherwise the
        # autonomy metrics record a held-for-review PR as an autonomous success.
        # `is True`: deliverable_blocked is a strict bool flag set by the gate
        # (bool(gate.get("blocked"))). Require an explicit True so only the gate
        # actually firing downgrades the task — consistent with the
        # requires_deliverable `is not False` identity check below.
        # FIX (calibrate audit 2026-07-06): also check _activation_gate_result
        # for git-capture PRs — previously this path set the label but the task
        # was still released as "completed".
        deliverable_blocked = bool(
            execution_success
            and (
                # Path 1: auto_pr_result (non-git-capture PRs)
                (
                    auto_pr_result is not None
                    and auto_pr_result.success
                    and getattr(auto_pr_result, "deliverable_blocked", False) is True
                )
                # Path 2: _activation_gate_result (git-capture PRs via WS-103)
                or (
                    _activation_gate_result is not None
                    and _activation_gate_result.get("ran")
                    and _activation_gate_result.get("blocked") is True
                )
                # Path 3: pr_workflow_result (plan-driven PRs via execute_pr_workflow)
                or (
                    pr_workflow_result is not None
                    and pr_workflow_result.success
                    and getattr(pr_workflow_result, "deliverable_blocked", False)
                    is True
                )
            )
        )
        if deliverable_blocked and not execution_error:
            execution_error = (
                getattr(auto_pr_result, "deliverable_reason", None)
                or getattr(pr_workflow_result, "deliverable_reason", None)
                or (
                    _activation_gate_result.get("error")
                    or _activation_gate_result.get("reason")
                    if _activation_gate_result
                    else None
                )
                or "Pre-merge deliverable gate: PR held for manual review"
            )
            logger.info(
                f"Deliverable gate held PR for task {task_id} — releasing as "
                f"BLOCKED (needs manual review), not completed"
            )

        # WS-119 1.8: Tightened phantom completion guard.
        # Require a PR (via pr_workflow, auto_pr, or git_capture) unless the
        # task explicitly opts out with requires_deliverable=False.
        # Default is True: tasks created without the field (e.g. injected
        # directly into the queue, bypassing allocate_task) must still
        # produce a deliverable, otherwise 31% of completions go phantom
        # via missing-field False-fallback.
        # Branch/commit alone is NOT sufficient — worktree tasks create
        # branches/commits in ephemeral worktrees that get cleaned up,
        # so without a PR the work is lost.
        # `is not False`: only an explicit False opts out — an explicit null
        # in queue JSON must not bypass the guard (PR #953 missing-field class).
        if (
            execution_success
            and claimed_task.get("requires_deliverable", True) is not False
        ):
            _gc = activation_result.get("git_capture", {}) or {}
            _has_pr = bool(_gc.get("pr_url"))
            # Count PR workflow / auto-PR results too
            _wf_pr = bool(
                (pr_workflow_result and getattr(pr_workflow_result, "pr_url", None))
                or (auto_pr_result and getattr(auto_pr_result, "pr_url", None))
            )
            if not (_has_pr or _wf_pr):
                execution_success = False
                execution_error = (
                    "WS-119 1.8: Task produced no deliverable — no PR was created. "
                    "Branch/commit in worktree is not sufficient — work would be "
                    "lost when worktree is cleaned up. Marking failed for retry."
                )
                logger.warning(
                    f"WS-119 1.8: phantom-completion guard fired for {task_id} — "
                    f"no PR created (git_capture.pr_url={_gc.get('pr_url')}, "
                    f"wf_pr={_wf_pr})"
                )

        # Update session metrics
        if state_path:
            current_metrics = load_session_state(state_path).get("loop_metrics", {})
            if execution_success and not deliverable_blocked:
                update_loop_metrics(
                    state_path,
                    tasks_completed=current_metrics.get("tasks_completed", 0) + 1,
                    last_task_id=task_id,
                    current_task_id=None,
                )
            elif not execution_success:
                update_loop_metrics(
                    state_path,
                    tasks_failed=current_metrics.get("tasks_failed", 0) + 1,
                    last_task_id=task_id,
                    current_task_id=None,
                )
            else:
                # deliverable_blocked: PR held for manual review — counted as
                # neither completed nor failed, just clear the current task.
                update_loop_metrics(
                    state_path,
                    last_task_id=task_id,
                    current_task_id=None,
                )

        # P99: Extract PR metadata from workflows for completions display.
        # Computed REGARDLESS of execution_success: a task can be marked failed
        # (e.g. an Agent Teams phantom downgrade) while a PR was nonetheless
        # created by the worktree harvest or the single-agent fallback. Recording
        # the PR on the task record lets failure_recovery's has_deliverable check
        # recognise the genuine success and MARK_COMPLETE it, instead of
        # mis-recording shipped-and-merged work as a failure (the
        # success_misdetected → "Exhausted retries" → blocked bug, task #4c2502 /
        # PR #1083).
        pr_metadata = None
        pr_url = None
        branch_name = None
        # Priority: auto_pr_result > pr_workflow_result > git_capture
        if auto_pr_result and auto_pr_result.pr_url:
            pr_url = auto_pr_result.pr_url
            branch_name = auto_pr_result.branch_name
        elif pr_workflow_result and pr_workflow_result.pr_url:
            pr_url = pr_workflow_result.pr_url
            branch_name = pr_workflow_result.branch_name
        else:
            git_capture = activation_result.get("git_capture", {})
            pr_url = git_capture.get("pr_url")
            branch_name = git_capture.get("branch")
        if pr_url:
            pr_metadata = {"pr_url": pr_url, "branch_name": branch_name}

        # Release task with result (pass employee_id for correct attribution).
        # A deliverable-gate hold releases as "blocked" (held for manual review),
        # not "completed" — see the Phase 2 honesty fix above.
        if not execution_success:
            _release_status = "failed"
        elif deliverable_blocked:
            _release_status = "blocked"
        else:
            _release_status = "completed"
        release_result = release_task(
            queue_path=queue_path,
            task_id=task_id,
            result=_release_status,
            error=execution_error,
            employee_id=matched_employee,
            pr_metadata=pr_metadata,
        )

        if not release_result.get("success"):
            # P50: Don't fail the entire task because of a queue bookkeeping issue.
            # The employee's work still succeeded/failed independently.
            logger.warning(
                f"Queue release issue for {task_id}: "
                f"{release_result.get('error', 'unknown')} "
                f"(execution was {'successful' if execution_success else 'failed'})"
            )

        # Update ROADMAP.md for successfully completed planning tasks
        # (not for deliverable-gate holds — that work isn't merged yet).
        if (
            execution_success
            and not deliverable_blocked
            and claimed_task.get("source") == "planning"
        ):
            _update_roadmap_on_completion(claimed_task)

        # P32: Post-completion proposal hook
        # Triggers bottom-up employee proposals after successful execution.
        # Skipped for deliverable-gate holds — the work isn't validated/merged.
        post_completion_result = None
        if (
            execution_success
            and not deliverable_blocked
            and matched_employee
            and employee_initiative is not None
        ):
            import random as _random  # Inline import to avoid linter removal

            # 65% probability to avoid proposal flood
            if _random.random() < 0.65:
                try:
                    post_completion_result = (
                        employee_initiative.submit_post_completion_proposal(
                            employee_id=matched_employee,
                            completed_task=claimed_task,
                        )
                    )
                except Exception as _proposal_err:
                    # Never fail the main execution due to proposal errors
                    import logging as _logging

                    _logging.getLogger("forge_daemon").info(
                        f"P32: Post-completion proposal failed for "
                        f"{matched_employee}: {_proposal_err}"
                    )

        # Return execution result with employee activation details
        # Use appropriate message based on success/failure/blocked status
        if deliverable_blocked:
            _action = "blocked"
            _result = "blocked"
            reason_msg = (
                execution_error or "PR held for manual review (deliverable gate)"
            )
        elif execution_success:
            _action = "executed"
            _result = "completed"
            reason_msg = activation_result.get("message", "Task executed via employee")
        else:
            _action = "failed"
            _result = "failed"
            # For failures, use error message from activation or a clear failure message
            reason_msg = (
                execution_error
                or activation_result.get("message")
                or f"Task failed (exit_code={activation_result.get('execution_result', {}).get('exit_code', 'unknown')})"
            )

        return {
            "action": _action,
            "reason": reason_msg,
            "task_id": task_id,
            "result": _result,
            "task": release_result.get("task"),
            "escalation": None,
            "employee_activation": {
                "employee_id": activation_result.get("employee_id"),
                "context_loaded": activation_result.get("context_loaded"),
                "memory_updated": activation_result.get("memory_updated"),
                "efficiency_recorded": activation_result.get("efficiency_recorded"),
                "model": activation_result.get("model"),
                # WS-100: Pass git_capture through so task_result_writer can
                # populate branch, pr_url, files_changed in result files
                "git_capture": activation_result.get("git_capture", {}),
            },
            "pr_workflow": pr_workflow_result,
            "auto_pr_workflow": auto_pr_result,
            "post_completion_proposal": post_completion_result,  # P32
        }

    else:
        # Fallback: MVP mode (employee activation disabled)
        release_result = release_task(
            queue_path=queue_path,
            task_id=task_id,
            result="completed",
            error=None,
        )

        if not release_result.get("success"):
            # P50: Log but don't fail — queue bookkeeping shouldn't block results
            logger.warning(
                f"Queue release issue for {task_id} (MVP mode): "
                f"{release_result.get('error', 'unknown')}"
            )

        # Step 7: Return execution result
        return {
            "action": "executed",
            "reason": "Task completed successfully (MVP mode - activation disabled)",
            "task_id": task_id,
            "result": "completed",
            "task": release_result.get("task"),
            "escalation": None,
        }


# =============================================================================
# CLI Interface
# =============================================================================


def _get_default_queue_path() -> Path:
    """Get the default work queue path using company resolver."""
    _ensure_imports()
    return company_resolver.get_company_dir() / "state/work_queue.json"


def _parse_args(args: list[str]) -> dict[str, str | bool]:
    """Parse command line arguments."""
    result: dict[str, str | bool] = {}
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


def _json_safe(obj):  # noqa: ANN001
    """json.dumps ``default`` for CLI result dicts that embed dataclasses.

    ``poll_and_execute_once`` returns AutoPRResult objects under ``pr_workflow`` /
    ``auto_pr_workflow``. Without this, a SUCCESSFUL ``poll`` crashes while
    serializing its own result ("Object of type AutoPRResult is not JSON
    serializable") and exits 1 — a false failure on a task that actually shipped.
    The in-process daemon path is unaffected: it reads these as objects and never
    serializes them.
    """
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _print_help():
    """Print usage help."""
    help_text = """
Operation Loop — core poll/claim/release cycle

Commands:
    claimable   List tasks eligible for claiming
    claim       Claim a specific task
    release     Release a task after execution
    poll        Run one poll/execute cycle

Claimable options:
    --queue PATH    Path to work queue (default: auto-detect)

Claim options:
    --task-id ID    Task ID to claim (required)
    --agent-id ID   Agent ID claiming the task (default: operation-loop)
    --queue PATH    Path to work queue (default: auto-detect)

Release options:
    --task-id ID    Task ID to release (required)
    --result STR    Result: completed | failed | escalated (required)
    --error TEXT    Error message (for failed/escalated)
    --queue PATH    Path to work queue (default: auto-detect)

Poll options:
    --agent-id ID   Agent ID for polling (default: operation-loop)
    --queue PATH    Path to work queue (default: auto-detect)

Examples:
    # List claimable tasks
    python operation_loop.py claimable

    # Claim a task
    python operation_loop.py claim --task-id task-123 --agent-id agent-001

    # Release a completed task
    python operation_loop.py release --task-id task-123 --result completed

    # Release a failed task
    python operation_loop.py release --task-id task-123 --result failed --error "Network timeout"

    # Run one poll cycle
    python operation_loop.py poll --agent-id agent-001
"""
    print(help_text)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        _print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = _parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        _print_help()
        sys.exit(0)

    # Get queue path
    queue_arg = args.get("queue")
    if queue_arg and isinstance(queue_arg, str):
        queue_path = Path(queue_arg)
    else:
        queue_path = _get_default_queue_path()

    try:
        if command == "claimable":
            tasks = get_claimable_tasks(queue_path)
            result = {
                "success": True,
                "count": len(tasks),
                "tasks": tasks,
            }
            print(json.dumps(result, indent=2))

        elif command == "claim":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)

            agent_id_arg = args.get("agent_id")
            agent_id = agent_id_arg if isinstance(agent_id_arg, str) else LOOP_AGENT_ID
            task_id = args["task_id"]
            if not isinstance(task_id, str):
                print("Error: --task-id must be a string")
                sys.exit(1)
            result = claim_task(queue_path, task_id, agent_id)
            print(json.dumps(result, indent=2))

        elif command == "release":
            if "task_id" not in args:
                print("Error: --task-id required")
                sys.exit(1)
            if "result" not in args:
                print("Error: --result required (completed | failed | escalated)")
                sys.exit(1)

            task_id = args["task_id"]
            result_value = args["result"]
            error_value = args.get("error")
            if not isinstance(task_id, str) or not isinstance(result_value, str):
                print("Error: --task-id and --result must be strings")
                sys.exit(1)
            error_str = error_value if isinstance(error_value, str) else None

            result = release_task(
                queue_path=queue_path,
                task_id=task_id,
                result=result_value,
                error=error_str,
            )
            print(json.dumps(result, indent=2))

        elif command == "poll":
            agent_id_arg = args.get("agent_id")
            agent_id = agent_id_arg if isinstance(agent_id_arg, str) else LOOP_AGENT_ID
            result = poll_and_execute_once(queue_path, agent_id)
            print(json.dumps(result, indent=2, default=_json_safe))

        else:
            print(f"Unknown command: {command}")
            _print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
