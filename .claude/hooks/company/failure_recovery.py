#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-105: Autonomous Failure Recovery System

Automatically diagnoses and recovers from failed tasks without human intervention.
Targets 80% autonomous resolution rate (from current 0%).

Components:
  1. FailureAnalyzer   — categorize failures beyond basic error categories
  2. StrategyEngine    — map failure types to recovery approaches
  3. CompletionVerifier — verify goal achieved, not just task executed
  4. RecoveryOrchestrator — coordinate the recovery pipeline
  5. PatternLearner    — learn which strategies succeed over time
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # default pr_runner for the W2-P2 PR-state reality consult
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from . import infra_failure
except ImportError:
    import infra_failure  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_PATTERNS_FILE = "state/recovery_patterns.json"


class FailureType(Enum):
    TEST_FAILURE = "test_failure"
    UNCOMMITTED_WORK = "uncommitted_work"
    WRONG_APPROACH = "wrong_approach"
    TIMEOUT = "timeout"
    ENVIRONMENT_ERROR = "environment_error"
    FLAPPING = "flapping"
    PR_WORKFLOW = "pr_workflow"
    SYNTAX_ERROR = "syntax_error"
    SUCCESS_MISDETECTED = "success_misdetected"
    DELIVERABLE_REJECTED = "deliverable_rejected"
    NO_DELIVERABLE = "no_deliverable"
    PROVIDER_PREFLIGHT = "provider_preflight"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    RETRY_WITH_FIX_HINT = "retry_with_fix_hint"
    RETRY_SIMPLIFIED = "retry_simplified"
    RETRY_FRESH_WORKTREE = "retry_fresh_worktree"
    MARK_COMPLETE = "mark_complete"
    FIX_ENVIRONMENT = "fix_environment"
    RETRY_WITH_REPLAN = "retry_with_replan"
    ESCALATE = "escalate"
    NO_RETRY = "no_retry"
    # W2-P2: a reality-consult (see pr_reality_check) found an OPEN PR for the
    # task — route it to the pr_open lane instead of blocking it. (A MERGED PR
    # reuses MARK_COMPLETE, which the daemon already routes to completed.)
    ROUTE_TO_PR_OPEN = "route_to_pr_open"


@dataclass
class FailureAnalysis:
    task_id: str
    failure_type: FailureType
    confidence: float
    evidence: list[str] = field(default_factory=list)
    suggested_strategy: RecoveryStrategy = RecoveryStrategy.ESCALATE
    fix_hint: str = ""
    is_recoverable: bool = True
    last_error_msg: str = ""


@dataclass
class RecoveryResult:
    task_id: str
    attempted: bool
    strategy_used: RecoveryStrategy
    success: bool
    reason: str
    recovery_attempt_num: int = 1
    new_task_id: str | None = None
    # W2-P2: populated when a reality-consult routed the task by real PR state
    # (a merged PR -> MARK_COMPLETE, an open PR -> ROUTE_TO_PR_OPEN).
    pr_url: str | None = None
    pr_number: int | None = None


@dataclass
class VerificationResult:
    task_id: str
    goal_achieved: bool
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


class FailureAnalyzer:
    """Classify failed tasks into fine-grained FailureType values."""

    _PATTERNS: list[tuple[str, FailureType, float, str]] = [
        (
            # Deliverable judge explicitly rejected the implementation: the worker
            # built something real but it didn't address what the task asked for.
            # Highest-confidence pattern — when the judge fires, this is definitive.
            # Routes to RETRY_WITH_REPLAN so the worker gets a fresh plan rather
            # than blindly retrying the same wrong approach.
            r"Workflow verdict.*does not deliver|diff does not deliver the task",
            FailureType.DELIVERABLE_REJECTED,
            0.95,
            "Implementation did not address the task requirements — re-read the task and use a completely different approach",
        ),
        (
            # employee_activator's P52/WS-121 deliverable gate: the worker
            # exited 0 but produced no file changes / PR (or the PR didn't
            # actually address the task). This text is now threaded through
            # into the error message (previously it was printed to stderr and
            # discarded, so the message fell back to a generic exit_code=0
            # placeholder). Must outrank the weak SUCCESS_MISDETECTED pattern
            # below — otherwise a confirmed zero-deliverable failure gets
            # routed to MARK_COMPLETE, phantom-completing a task where no
            # work happened at all.
            r"no deliverable produced|deliverable verification failed",
            FailureType.NO_DELIVERABLE,
            0.92,
            "No files were changed and no PR was produced — retry with a "
            "concrete plan to write/modify files, not just describe the work",
        ),
        (
            # The worker CLI's own health check (e.g. `claude auth status`)
            # timed out before any task work started. This is a transient
            # environment/CLI-availability blip, not something the task
            # description or implementation approach can fix — must outrank
            # the generic TIMEOUT pattern below, which would otherwise catch
            # the "timed out" substring in this same message and suggest
            # "break into smaller steps", which is nonsensical here.
            #
            # Scoped to the timeout case specifically (requires "timed out"
            # alongside the preflight-failed prefix): employee_activator.py
            # wraps EVERY check_provider_health() failure — including
            # persistent ones like "not logged in" or "CLI not installed" —
            # in the same "Provider preflight failed: ..." prefix. Those are
            # real, human-actionable failures and must NOT get the "retry
            # as-is, nothing is wrong" guidance this pattern grants.
            r"provider preflight failed[\s\S]*timed\s*out",
            FailureType.PROVIDER_PREFLIGHT,
            0.93,
            "Transient provider CLI timeout — retry as-is, no code or task changes needed",
        ),
        (
            # Lowest priority among patterns: a clean exit is the WEAKEST
            # signal and must lose to any concrete failure pattern that also
            # matches (e.g. an error containing both "exit_code=0" and "FAILED
            # test_" is a TEST_FAILURE, not a success). It only wins — and
            # routes to MARK_COMPLETE — when no real-failure pattern matched.
            r"exit_code=0\b|success_detection_failure",
            FailureType.SUCCESS_MISDETECTED,
            0.50,
            "Task exited successfully; verify output meets completion criteria",
        ),
        (
            r"nothing to commit|working tree clean|no changes|already up.to.date"
            r"|uncommitted changes|untracked files",
            FailureType.UNCOMMITTED_WORK,
            0.80,
            "Commit pending changes before retrying",
        ),
        (
            r"FAILED\s+test_|FAILED\s+\w+::|pytest.*\d+\s+failed|AssertionError:",
            FailureType.TEST_FAILURE,
            0.90,
            "Fix failing tests before retrying",
        ),
        (
            r"SyntaxError|IndentationError|TabError|ruff.*error|lint.*error"
            r"|TypeError:|AttributeError:|NameError:",
            FailureType.SYNTAX_ERROR,
            0.85,
            "Fix code errors: syntax, type, or attribute issues",
        ),
        (
            r"ModuleNotFoundError|ImportError|command not found|exit_code=127"
            r"|No such file or directory|FileNotFoundError",
            FailureType.ENVIRONMENT_ERROR,
            0.85,
            "Install missing dependencies or fix paths",
        ),
        (
            r"TimeoutExpired|timed?\s*out|exit_code=-1\b|exit_code=137\b"
            r"|exit_code=143\b|SIGKILL|SIGTERM|OOM",
            FailureType.TIMEOUT,
            0.90,
            "Break task into smaller steps or increase timeout",
        ),
        (
            r"PR creation failed|push.*rejected|non-fast-forward|merge conflict"
            r"|git_push_error|pr_failure|pr_commit_error",
            FailureType.PR_WORKFLOW,
            0.85,
            "Resolve git conflicts or stale branches before retrying PR",
        ),
        (
            r"wrong approach|incorrect implementation|not what was asked"
            r"|misunderstood|needs complete rewrite|fundamentally wrong",
            FailureType.WRONG_APPROACH,
            0.70,
            "Re-read requirements and use a different implementation approach",
        ),
        (
            r"flak|intermittent|non.deterministic|race condition|sometimes fails",
            FailureType.FLAPPING,
            0.65,
            "Add retry logic or fix race condition in implementation",
        ),
    ]

    def analyze(
        self,
        task: dict,
        error_msg: str,
        exit_code: int | None = None,
        *,
        strategy_retry_count: int | None = None,
    ) -> FailureAnalysis:
        """Analyze a failed task and return a FailureAnalysis.

        Args:
            strategy_retry_count: Override the retry count used for strategy
                budget determination only.  Wrong-approach detection still uses
                the task's actual retry_count.  Callers in a recovery-loop
                context must pass the real recovery_attempt_count so each
                failure type's per-type _MAX_AUTONOMOUS budget can escalate
                to a human once exhausted -- passing a constant here (e.g. a
                hardcoded 0) silently defeats that escalation for every
                failure type whose budget is >=1.
        """
        task_id = task.get("task_id", "unknown")
        error_lower = (error_msg or "").lower()

        best_type = FailureType.UNKNOWN
        best_confidence = 0.0
        best_hint = ""
        evidence: list[str] = []

        for pattern, ftype, conf, hint in self._PATTERNS:
            if re.search(pattern, error_lower, re.IGNORECASE):
                evidence.append(f"Pattern matched: {ftype.value}")
                if conf > best_confidence:
                    best_type = ftype
                    best_confidence = conf
                    best_hint = hint

        retry_count = task.get("retry_count", 0)
        retry_history = task.get("retry_history", [])

        if retry_count >= 2 and best_type == FailureType.UNKNOWN:
            if len({r.get("error", "")[:50] for r in retry_history}) >= 2:
                best_type = FailureType.WRONG_APPROACH
                best_confidence = 0.60
                best_hint = "Multiple distinct errors suggest wrong approach"
                evidence.append("Multiple distinct retry errors detected")

        if best_type == FailureType.UNKNOWN and exit_code is not None:
            if exit_code == 0:
                best_type = FailureType.SUCCESS_MISDETECTED
                best_confidence = 0.90
                best_hint = "Exit code 0 indicates likely success"
                evidence.append(f"exit_code={exit_code}")
            elif exit_code in (-1, 137, 143):
                best_type = FailureType.TIMEOUT
                best_confidence = 0.80
                best_hint = "Exit code indicates timeout/kill signal"
                evidence.append(f"exit_code={exit_code}")

        budget = (
            strategy_retry_count if strategy_retry_count is not None else retry_count
        )
        strategy = StrategyEngine.get_strategy(best_type, budget)
        is_recoverable = strategy not in (
            RecoveryStrategy.ESCALATE,
            RecoveryStrategy.NO_RETRY,
        )

        return FailureAnalysis(
            task_id=task_id,
            failure_type=best_type,
            confidence=best_confidence,
            evidence=evidence,
            suggested_strategy=strategy,
            fix_hint=best_hint,
            is_recoverable=is_recoverable,
            last_error_msg=(error_msg or "")[:400],
        )


class StrategyEngine:
    """Map FailureType to RecoveryStrategy with attempt-count-aware escalation."""

    _STRATEGY_MAP: dict[FailureType, RecoveryStrategy] = {
        FailureType.SUCCESS_MISDETECTED: RecoveryStrategy.MARK_COMPLETE,
        FailureType.TEST_FAILURE: RecoveryStrategy.RETRY_WITH_FIX_HINT,
        FailureType.SYNTAX_ERROR: RecoveryStrategy.RETRY_WITH_FIX_HINT,
        FailureType.ENVIRONMENT_ERROR: RecoveryStrategy.FIX_ENVIRONMENT,
        FailureType.TIMEOUT: RecoveryStrategy.RETRY_SIMPLIFIED,
        FailureType.PR_WORKFLOW: RecoveryStrategy.RETRY_FRESH_WORKTREE,
        FailureType.UNCOMMITTED_WORK: RecoveryStrategy.RETRY_FRESH_WORKTREE,
        FailureType.WRONG_APPROACH: RecoveryStrategy.RETRY_WITH_REPLAN,
        FailureType.FLAPPING: RecoveryStrategy.RETRY_WITH_FIX_HINT,
        FailureType.DELIVERABLE_REJECTED: RecoveryStrategy.RETRY_WITH_REPLAN,
        FailureType.NO_DELIVERABLE: RecoveryStrategy.RETRY_WITH_REPLAN,
        FailureType.PROVIDER_PREFLIGHT: RecoveryStrategy.RETRY_WITH_FIX_HINT,
        FailureType.UNKNOWN: RecoveryStrategy.RETRY_WITH_FIX_HINT,
    }

    _MAX_AUTONOMOUS: dict[FailureType, int] = {
        FailureType.SUCCESS_MISDETECTED: 1,
        FailureType.TEST_FAILURE: 2,
        FailureType.SYNTAX_ERROR: 2,
        FailureType.ENVIRONMENT_ERROR: 1,
        FailureType.TIMEOUT: 1,
        FailureType.PR_WORKFLOW: 2,
        FailureType.UNCOMMITTED_WORK: 2,
        FailureType.WRONG_APPROACH: 1,
        FailureType.FLAPPING: 2,
        FailureType.DELIVERABLE_REJECTED: 1,
        FailureType.NO_DELIVERABLE: 1,
        FailureType.PROVIDER_PREFLIGHT: 2,
        FailureType.UNKNOWN: 1,
    }

    # Strategies that represent an already-decided, terminal outcome rather
    # than a retry. The attempt-count escalation gate must NOT override these:
    # escalating an already-achieved goal on its second pass strands a
    # succeeded task in a human queue instead of recording it complete.
    _TERMINAL_STRATEGIES: frozenset[RecoveryStrategy] = frozenset(
        {RecoveryStrategy.MARK_COMPLETE}
    )

    @classmethod
    def get_strategy(
        cls, failure_type: FailureType, recovery_attempt: int = 0
    ) -> RecoveryStrategy:
        """Return recovery strategy, escalating after max autonomous attempts.

        Terminal verdicts (see ``_TERMINAL_STRATEGIES``) are returned
        regardless of ``recovery_attempt``: the outcome is already decided, so
        the attempt-count gate does not apply. Only genuine retry strategies
        escalate once their per-type attempt budget is exhausted.
        """
        strategy = cls._STRATEGY_MAP.get(
            failure_type, RecoveryStrategy.RETRY_WITH_FIX_HINT
        )
        if strategy in cls._TERMINAL_STRATEGIES:
            return strategy
        max_attempts = cls._MAX_AUTONOMOUS.get(failure_type, 1)
        if recovery_attempt >= max_attempts:
            return RecoveryStrategy.ESCALATE
        return strategy

    @classmethod
    def build_recovery_description(cls, task: dict, analysis: FailureAnalysis) -> str:
        """Build an enhanced task description with recovery guidance."""
        original_desc = task.get("description") or task.get("title", "")
        hint = analysis.fix_hint
        failure_name = analysis.failure_type.value.replace("_", " ").title()
        strategy_name = analysis.suggested_strategy.value.replace("_", " ").title()

        # Use hint when available; fall back to error excerpt so the line is never empty.
        failure_description = hint or (
            analysis.last_error_msg[:200]
            if analysis.last_error_msg
            else "(no details available)"
        )

        lines = [
            f"[RECOVERY ATTEMPT — {failure_name}]",
            f"Previous failure: {failure_description}",
            f"Recovery strategy: {strategy_name}",
        ]

        # Include the raw error excerpt so workers have concrete details to act on.
        # Skip when error text and hint are identical (would duplicate the line above).
        if analysis.last_error_msg and analysis.last_error_msg != hint:
            excerpt = analysis.last_error_msg[:300]
            if len(analysis.last_error_msg) > 300:
                excerpt += "..."
            lines += ["", f"Error excerpt: {excerpt}"]

        # Summarise prior attempts so workers don't repeat the same approach.
        retry_history = task.get("retry_history", [])
        if retry_history:
            lines.append("")
            lines.append("Prior attempts:")
            for attempt in retry_history[-2:]:
                attempt_num = attempt.get("attempt", "?")
                prior_err = (attempt.get("error") or "")[:150]
                if prior_err:
                    lines.append(f"  Attempt {attempt_num}: {prior_err}")

        lines += [
            "",
            "Original task:",
            original_desc,
        ]

        if analysis.failure_type == FailureType.TEST_FAILURE:
            lines += [
                "",
                "IMPORTANT: Run tests first to see what is failing. Fix all test",
                "failures before creating a PR. Do not ignore test output.",
            ]
        elif analysis.failure_type == FailureType.SYNTAX_ERROR:
            lines += [
                "",
                "IMPORTANT: Fix all syntax, lint, and code errors before committing.",
                "Run `ruff check .` and `ruff format --check .` to identify issues.",
            ]
        elif analysis.failure_type == FailureType.UNCOMMITTED_WORK:
            lines += [
                "",
                "IMPORTANT: Commit all pending changes before proceeding.",
                "Run `git status` to see what is uncommitted, then stage and commit.",
            ]
        elif analysis.failure_type == FailureType.TIMEOUT:
            lines += [
                "",
                "IMPORTANT: This task timed out previously. Break implementation",
                "into smaller focused steps. Avoid bulk operations.",
            ]
        elif analysis.failure_type == FailureType.WRONG_APPROACH:
            lines += [
                "",
                "IMPORTANT: Previous approach(es) did not work. Re-read the task",
                "requirements carefully and use a completely different strategy.",
            ]
        elif analysis.failure_type == FailureType.PR_WORKFLOW:
            lines += [
                "",
                "IMPORTANT: Start on a fresh branch. Resolve any merge conflicts.",
                "Ensure all tests pass before pushing.",
            ]
        elif analysis.failure_type == FailureType.ENVIRONMENT_ERROR:
            lines += [
                "",
                "IMPORTANT: Check that all required packages are installed.",
                "Run: uv sync or pip install -r requirements.txt if needed.",
            ]
        elif analysis.failure_type == FailureType.FLAPPING:
            lines += [
                "",
                "IMPORTANT: This task shows intermittent failures (flapping).",
                "Investigate race conditions or non-deterministic behavior.",
                "Add explicit waits, isolation, or retries where needed.",
            ]
        elif analysis.failure_type == FailureType.DELIVERABLE_REJECTED:
            lines += [
                "",
                "IMPORTANT: The deliverable judge reviewed your implementation and",
                "determined it did NOT address what the task actually asked for.",
                "This is NOT a test failure or syntax error — the code may run fine",
                "but it solves the wrong problem.",
                "Re-read the original task requirements from scratch. Identify",
                "exactly what deliverable is requested (new feature, bug fix,",
                "specific file, specific behavior). Implement THAT, not something",
                "adjacent to it.",
            ]
        elif analysis.failure_type == FailureType.PROVIDER_PREFLIGHT:
            lines += [
                "",
                "IMPORTANT: The previous attempt failed before any work started —",
                "the worker CLI's own auth/health check timed out. This is a",
                "transient environment issue, not a problem with the task or a",
                "prior approach. Simply proceed with the original task as normal.",
            ]
        elif analysis.failure_type == FailureType.UNKNOWN:
            lines += [
                "",
                "IMPORTANT: The failure cause is unclear. Read the error excerpt",
                "above carefully and address the root cause directly.",
            ]

        return "\n".join(lines)


class CompletionVerifier:
    """Verify that a task's goal was actually achieved, not just executed."""

    # Patterns indicating PR is required
    _PR_REQUIRED_PATTERNS = [
        r"must\s+create\s+pr",
        r"create\s+(?:a\s+)?pr\b",
        r"submit\s+(?:a\s+)?pr\b",
        r"open\s+(?:a\s+)?pr\b",
        r"pr\s+required",
        r"task\s+fails?\s+if\s+no\s+pr",
        r"must-create-pr",  # tag
    ]

    # Patterns indicating file creation is required
    _FILE_REQUIRED_PATTERNS = [
        r"create\s+[\w/._-]+\.py\b",
        r"implement\s+[\w/._-]+\.py\b",
        r"add\s+[\w/._-]+\.py\b",
        r"write\s+[\w/._-]+\.py\b",
    ]

    def _check_required_deliverables(self, task: dict) -> tuple[list[str], list[str]]:
        """Check if task description requires specific deliverables.

        Returns:
            Tuple of (missing_deliverables, found_deliverables)
        """
        description = (task.get("description") or "") + " " + (task.get("title") or "")
        tags = task.get("tags", [])
        description_lower = description.lower()

        missing: list[str] = []
        found: list[str] = []

        # Check if PR is required
        pr_required = (
            any(re.search(p, description_lower) for p in self._PR_REQUIRED_PATTERNS)
            or "must-create-pr" in tags
        )

        if pr_required:
            if task.get("pr_url"):
                found.append(f"PR created: {task['pr_url']}")
            else:
                missing.append("PR required but not created")

        # Check for required file creations
        for pattern in self._FILE_REQUIRED_PATTERNS:
            match = re.search(pattern, description_lower)
            if match:
                # Extract the filename from the match
                file_match = re.search(r"([\w/._-]+\.py)\b", match.group(0))
                if file_match:
                    filename = file_match.group(1)
                    # We can't easily verify file creation here, but we note it
                    # as a required deliverable that should be checked
                    if not task.get("pr_url") and not task.get("files_changed"):
                        missing.append(
                            f"File '{filename}' creation required but no changes detected"
                        )

        return missing, found

    def verify(self, task: dict, error_msg: str | None = None) -> VerificationResult:
        """Check if a task's goal was actually achieved.

        Returns:
            VerificationResult with goal_achieved=True when evidence suggests
            the task completed successfully despite being marked failed.
        """
        task_id = task.get("task_id", "unknown")
        evidence: list[str] = []
        confidence_signals: list[float] = []

        # First check required deliverables
        missing_deliverables, found_deliverables = self._check_required_deliverables(
            task
        )

        # If deliverables are missing, this is NOT a false failure - it's a real failure
        if missing_deliverables:
            evidence.extend([f"MISSING: {d}" for d in missing_deliverables])
            return VerificationResult(
                task_id=task_id,
                goal_achieved=False,
                evidence=evidence,
                confidence=0.0,
            )

        # Add found deliverables as positive evidence
        evidence.extend(found_deliverables)
        if found_deliverables:
            confidence_signals.append(0.9)

        # Guard A: if the workflow verdict already explicitly rejected this PR's
        # diff, the pr_url is the REJECTED deliverable, not evidence of success.
        # Return immediately before the pr_url / exit_code / error_pattern signals
        # are evaluated — a rejected verdict must not be overridden by signal
        # accumulation.
        wf_verdict = task.get("workflow_verdict") or {}
        if wf_verdict.get("addresses_task") is False and not wf_verdict.get("errored"):
            evidence.append(
                "workflow_verdict explicitly rejected this PR's diff "
                f"(addresses_task=False, confidence={wf_verdict.get('confidence', '?')})"
            )
            return VerificationResult(
                task_id=task_id,
                goal_achieved=False,
                evidence=evidence,
                confidence=0.0,
            )

        exit_code = task.get("exit_code")
        if exit_code == 0:
            # WS-118: exit_code=0 is necessary but NOT sufficient evidence.
            # Previously 0.8 — crossed threshold alone with any weak signal.
            evidence.append("exit_code=0 (process exited successfully)")
            confidence_signals.append(0.3)

        if task.get("pr_url"):
            evidence.append(f"PR created: {task['pr_url']}")
            confidence_signals.append(0.9)

        # WS-118: Removed "No substantial error message" signal (0.5).
        # Absence of error is not evidence of success.

        if error_msg and re.search(
            r"exit_code=0\b|success_detection_failure|output too short",
            error_msg,
            re.IGNORECASE,
        ):
            evidence.append("Error pattern matches success misdetection")
            confidence_signals.append(0.85)

        # WS-118: Removed "First attempt with exit_code=0" signal (0.7).
        # Doing nothing on first try is not success.

        if confidence_signals:
            overall = sum(confidence_signals) / len(confidence_signals)
        else:
            overall = 0.0

        # WS-118: Require at least one deliverable (PR, commits or file
        # changes) before declaring goal achieved. Output text is never one: a
        # worker that explains at length and changes nothing is a phantom
        # (2026-10-31, found writing the book's Chapter 6).
        has_deliverable = bool(
            task.get("pr_url")
            or task.get("files_changed")
            or task.get("commits")
            or found_deliverables
        )
        goal_achieved = (
            overall >= 0.7 and len(confidence_signals) >= 1 and has_deliverable
        )

        return VerificationResult(
            task_id=task_id,
            goal_achieved=goal_achieved,
            evidence=evidence,
            confidence=round(overall, 3),
        )


class PatternLearner:
    """Record and learn from recovery outcomes over time."""

    # Terminal "give up" verdicts: recorded with succeeded=False by
    # construction, so they are excluded from strategy_success_rate and counted
    # toward escalation_rate instead (see summary()).
    _GIVE_UP_STRATEGIES: frozenset[str] = frozenset(
        {RecoveryStrategy.ESCALATE.value, RecoveryStrategy.NO_RETRY.value}
    )

    def __init__(self, company_dir: Path) -> None:
        self._path = company_dir / RECOVERY_PATTERNS_FILE
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self._path.exists():
            return {"patterns": {}, "updated_at": None}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"patterns": {}, "updated_at": None}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".rp_", suffix=".tmp"
        )
        try:
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self._path))
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._data = None

    def record_attempt(
        self,
        failure_type: FailureType,
        strategy: RecoveryStrategy,
        succeeded: bool,
    ) -> None:
        """Record the outcome of a recovery attempt."""
        data = self._load()
        key = f"{failure_type.value}:{strategy.value}"
        patterns = data.setdefault("patterns", {})
        entry = patterns.setdefault(
            key, {"attempts": 0, "successes": 0, "success_rate": 0.0}
        )
        entry["attempts"] += 1
        if succeeded:
            entry["successes"] += 1
        total = entry["attempts"]
        entry["success_rate"] = round(entry["successes"] / total, 3) if total else 0.0
        try:
            self._save(data)
        except Exception as e:
            logger.warning("PatternLearner: failed to save patterns: %s", e)

    def get_success_rate(
        self, failure_type: FailureType, strategy: RecoveryStrategy
    ) -> float:
        """Return historical success rate. Returns 0.5 (neutral prior) when no data."""
        data = self._load()
        key = f"{failure_type.value}:{strategy.value}"
        entry = data.get("patterns", {}).get(key)
        if entry and entry.get("attempts", 0) >= 3:
            return entry["success_rate"]
        return 0.5

    def get_best_strategy(
        self, failure_type: FailureType, candidates: list[RecoveryStrategy]
    ) -> RecoveryStrategy:
        """Pick the highest-success-rate strategy from candidates."""
        if not candidates:
            return RecoveryStrategy.ESCALATE
        return max(candidates, key=lambda s: self.get_success_rate(failure_type, s))

    def summary(self) -> dict[str, Any]:
        """Return a summary of learned patterns.

        The recovery rate is decomposed into two distinct signals because
        conflating them is misleading:

        - ``strategy_success_rate`` — of recovery strategies that actually RAN
          (retries, fix-environment, mark-complete), how often they resolved
          the task. This is the true quality of recovery.
        - ``escalation_rate`` — of all recovery decisions, the fraction that
          gave up (escalate / no_retry). Give-ups are recorded
          ``succeeded=False`` by construction, so folding them into a single
          ``overall_recovery_rate`` denominator drags it down regardless of how
          well the strategies that ran performed.

        ``overall_recovery_rate`` is retained for backward compatibility but
        prefer the decomposed metrics.
        """
        data = self._load()
        patterns = data.get("patterns", {})

        total_attempts = 0
        total_successes = 0
        escalation_attempts = 0
        strategy_attempts = 0
        strategy_successes = 0
        for key, p in patterns.items():
            attempts = p.get("attempts", 0)
            successes = p.get("successes", 0)
            total_attempts += attempts
            total_successes += successes
            # Key format is "<failure_type>:<strategy>"; neither value contains
            # a colon, so the last segment is the strategy.
            strategy = key.rsplit(":", 1)[-1]
            if strategy in self._GIVE_UP_STRATEGIES:
                escalation_attempts += attempts
            else:
                strategy_attempts += attempts
                strategy_successes += successes

        def _rate(num: int, denom: int) -> float:
            return round(num / denom, 3) if denom else 0.0

        return {
            "total_attempts": total_attempts,
            "total_successes": total_successes,
            "overall_recovery_rate": _rate(total_successes, total_attempts),
            "strategy_attempts": strategy_attempts,
            "strategy_successes": strategy_successes,
            "strategy_success_rate": _rate(strategy_successes, strategy_attempts),
            "escalation_attempts": escalation_attempts,
            "escalation_rate": _rate(escalation_attempts, total_attempts),
            "patterns": patterns,
            "updated_at": data.get("updated_at"),
        }


class RecoveryOrchestrator:
    """Coordinate autonomous failure recovery for failed daemon tasks.

    Usage:
        orchestrator = RecoveryOrchestrator(company_dir)
        result = orchestrator.attempt_recovery(task, error_msg, exit_code)
        if result.success:
            # task was re-queued with recovery context
        else:
            # proceed with normal escalation
    """

    def __init__(self, company_dir: Path, *, pr_runner: Any = subprocess.run) -> None:
        self._company_dir = company_dir
        self._analyzer = FailureAnalyzer()
        self._verifier = CompletionVerifier()
        self._learner = PatternLearner(company_dir)
        # Injectable subprocess runner for the W2-P2 PR-state reality consult
        # (tests pass a fake to avoid real git/gh calls). project_root for the
        # consult is company_dir.parent — the repo checkout that owns .company.
        self._pr_runner = pr_runner

    def attempt_recovery(
        self,
        task: dict,
        error_msg: str,
        exit_code: int | None = None,
        queue_path: Path | None = None,
    ) -> RecoveryResult:
        """Attempt autonomous recovery for a failed task.

        Args:
            task: Task dict from work queue.
            error_msg: Error message from the failed attempt.
            exit_code: Process exit code, if available.
            queue_path: Path to work_queue.json. If None, result is returned
                        but the task is not re-queued.

        Returns:
            RecoveryResult indicating strategy and whether recovery was initiated.
        """
        task_id = task.get("task_id", "unknown")

        # Infra-classified failures (provider preflight timeouts, CI runs with
        # 0 steps executed, etc.) never consumed retry budget in release_task
        # — it already reclassified and requeued the task without touching
        # retry_count. Recovery must not undo that by spending its own
        # recovery_attempt_count budget or escalating on a blip that isn't a
        # genuine task failure. See infra_failure.py.
        if (
            infra_failure.classify_failure_origin(error_msg)
            is infra_failure.FailureOrigin.INFRA
        ):
            logger.info(
                "[Recovery] Task %s: infra-classified failure — not consuming "
                "recovery/retry budget: %s",
                task_id,
                (error_msg or "")[:120],
            )
            return RecoveryResult(
                task_id=task_id,
                attempted=False,
                strategy_used=RecoveryStrategy.NO_RETRY,
                success=True,
                reason=(
                    "Infra-classified failure — release_task already "
                    "requeued without consuming budget"
                ),
                recovery_attempt_num=task.get("recovery_attempt_count", 0),
            )

        # W2-P2 (2026-07-18): Consult REAL PR state before any terminal
        # transition. Recovery decides blocked/escalate/retry from queue-file
        # attempt bookkeeping alone, but the ground truth (branch pushed, PR
        # open, PR merged) lives in git/GitHub. A task can exhaust its in-queue
        # retries while a PR it produced is open under review or already merged;
        # terminal-marking it then strands shipped/under-review work (the
        # 2026-07-17 ghost class; the 2026-07-18 respawn of two open-PR tasks).
        # A merged PR -> completed; an open PR -> pr_open; neither -> today's
        # logic proceeds. Fail-open: any gh/git error -> None -> today's path.
        # Per-failure consult is MERGED-only (cheap, TTL-cached): shipped
        # work must never be respawned regardless of attempt count. The
        # open-PR consult is reserved for TERMINAL transitions below —
        # routing to pr_open on a FIRST failure would park a task whose PR
        # is open-but-doomed instead of letting it retry (PR 266 review).
        reality_route = self._consult_pr_state(task, queue_path, include_open=False)
        if reality_route is not None:
            return reality_route

        recovery_count = task.get("recovery_attempt_count", 0)

        # Verify if the goal was actually already achieved. This MUST run
        # before the recovery_count exhaustion check below: operation_loop's
        # diagnose_error() force-sets retry_count to max_attempts on the very
        # FIRST failure for non-retryable categories (e.g. exit_code=0 with no
        # completion markers, category="success_detection_failure") — see
        # _EXIT_CODE_DIAGNOSIS in operation_loop.py. That means recovery_count
        # can already be >= MAX_RECOVERY_ATTEMPTS on the first call here, and
        # the old ordering escalated such tasks immediately, never reaching
        # this verifier — permanently dropping tasks that had, in fact,
        # already produced a real deliverable (2026-07-22 class: task
        # succeeded, produced a mergeable PR, but was logged terminal-failed).
        task_with_exit = dict(task)
        if exit_code is not None:
            task_with_exit["exit_code"] = exit_code
        verification = self._verifier.verify(task_with_exit, error_msg)
        if verification.goal_achieved:
            logger.info(
                "[Recovery] Task %s likely succeeded (confidence=%.2f): %s",
                task_id,
                verification.confidence,
                "; ".join(verification.evidence),
            )
            self._learner.record_attempt(
                FailureType.SUCCESS_MISDETECTED,
                RecoveryStrategy.MARK_COMPLETE,
                succeeded=True,
            )
            return RecoveryResult(
                task_id=task_id,
                attempted=True,
                strategy_used=RecoveryStrategy.MARK_COMPLETE,
                success=True,
                reason=(
                    f"Goal already achieved (confidence={verification.confidence:.2f}): "
                    + "; ".join(verification.evidence)
                ),
                recovery_attempt_num=recovery_count + 1,
            )

        # Feed the analyzer the REAL persisted attempt count (not a hardcoded
        # fresh-budget 0) so StrategyEngine._MAX_AUTONOMOUS's per-failure-type
        # escalation actually fires. Before this fix every call passed 0, and
        # since every _MAX_AUTONOMOUS budget is >=1, `0 >= max_attempts` was
        # always False -- the differentiated 1-2 attempt budgets (e.g.
        # DELIVERABLE_REJECTED/WRONG_APPROACH/TIMEOUT at 1) were silently dead
        # code, and only the flat MAX_RECOVERY_ATTEMPTS=3 gate above (recovery_count
        # exhaustion) ever escalated, regardless of failure type. SUCCESS_MISDETECTED
        # now genuinely bypasses that gate: the verifier check above runs first,
        # so a goal actually achieved is caught before this exhaustion gate can fire.
        analysis = self._analyzer.analyze(
            task_with_exit, error_msg, exit_code, strategy_retry_count=recovery_count
        )
        logger.info(
            "[Recovery] Task %s: failure_type=%s confidence=%.2f strategy=%s",
            task_id,
            analysis.failure_type.value,
            analysis.confidence,
            analysis.suggested_strategy.value,
        )

        # Terminal verdict: the analyzer concluded the task already succeeded
        # (e.g. exit_code 0 with no error signal). This is independent of the
        # attempt count — re-queuing or escalating an already-achieved goal is
        # wrong, which is exactly why this verdict must bypass _apply_strategy
        # (whose only tail action is to re-queue) and the escalation gate.
        if analysis.suggested_strategy == RecoveryStrategy.MARK_COMPLETE:
            # Guard B: backstop — if the workflow verdict explicitly rejected this
            # PR's diff, do NOT auto-complete regardless of what the analyzer decided.
            # This covers the path where error_msg pattern matching (not verify())
            # triggers MARK_COMPLETE, e.g. if "workflow_phantom" were added to the
            # SUCCESS_MISDETECTED regex in the future.
            _wf_verdict = task.get("workflow_verdict") or {}
            if _wf_verdict.get("addresses_task") is False and not _wf_verdict.get(
                "errored"
            ):
                self._learner.record_attempt(
                    FailureType.PR_WORKFLOW,
                    RecoveryStrategy.ESCALATE,
                    succeeded=False,
                )
                return RecoveryResult(
                    task_id=task_id,
                    attempted=False,
                    strategy_used=RecoveryStrategy.ESCALATE,
                    success=False,
                    reason=(
                        "workflow_verdict explicitly rejected this PR's diff "
                        "(addresses_task=False) — not auto-completing a "
                        "workflow-phantom task; surfacing as failure"
                    ),
                    recovery_attempt_num=recovery_count,
                )

            # Auto-completing a task is irreversible, so it requires POSITIVE
            # evidence the work was done — not merely a clean exit code. The
            # strong CompletionVerifier already ran above and declined; only
            # override it when a concrete deliverable exists. We mirror the
            # verifier's own has_deliverable gate (see verify(): pr_url /
            # files_changed / commits; output text never counts) rather than the
            # absence of a narrow "MISSING:" marker, which would fail open for
            # the many tasks the verifier does not model (e.g. "append tests to
            # X", "fix the bug", "update docs"). With no deliverable the task
            # may have exited 0 without doing the work (the exit-0-but-failed
            # class, cf. PR #1017), so we surface it as a failure instead.
            has_deliverable = bool(
                task.get("pr_url") or task.get("files_changed") or task.get("commits")
            )
            if has_deliverable:
                self._learner.record_attempt(
                    FailureType.SUCCESS_MISDETECTED,
                    RecoveryStrategy.MARK_COMPLETE,
                    succeeded=True,
                )
                return RecoveryResult(
                    task_id=task_id,
                    attempted=True,
                    strategy_used=RecoveryStrategy.MARK_COMPLETE,
                    success=True,
                    reason=(
                        "Goal already achieved per failure analysis "
                        f"(confidence={analysis.confidence:.2f}): "
                        f"{analysis.fix_hint}"
                    ),
                    recovery_attempt_num=recovery_count + 1,
                )
            # No deliverable to confirm success. Do NOT auto-complete; surface
            # as a failure so the caller's normal failure handling retries or
            # escalates it rather than silently closing an empty task.
            self._learner.record_attempt(
                FailureType.SUCCESS_MISDETECTED,
                RecoveryStrategy.ESCALATE,
                succeeded=False,
            )
            return RecoveryResult(
                task_id=task_id,
                attempted=False,
                strategy_used=RecoveryStrategy.ESCALATE,
                success=False,
                reason=(
                    "exit_code 0 but no deliverable found — not auto-completing "
                    "an unverifiable success; surfacing as failure"
                    + (
                        f" ({'; '.join(verification.evidence)})"
                        if verification.evidence
                        else ""
                    )
                ),
                recovery_attempt_num=recovery_count,
            )

        if recovery_count >= MAX_RECOVERY_ATTEMPTS:
            # Terminal transition — now also consult OPEN PR state: a task
            # exhausting retries while its PR sits open under review must
            # park in pr_open, not blocked (the 2026-07-18 live class).
            reality_route = self._consult_pr_state(task, queue_path, include_open=True)
            if reality_route is not None:
                return reality_route
            logger.info(
                "[Recovery] Task %s exceeded max recovery attempts (%d), escalating",
                task_id,
                MAX_RECOVERY_ATTEMPTS,
            )
            return RecoveryResult(
                task_id=task_id,
                attempted=False,
                strategy_used=RecoveryStrategy.ESCALATE,
                success=False,
                reason=f"Max recovery attempts ({MAX_RECOVERY_ATTEMPTS}) exceeded",
                recovery_attempt_num=recovery_count,
            )

        if not analysis.is_recoverable:
            self._learner.record_attempt(
                analysis.failure_type, analysis.suggested_strategy, succeeded=False
            )
            return RecoveryResult(
                task_id=task_id,
                attempted=False,
                strategy_used=analysis.suggested_strategy,
                success=False,
                reason=(
                    f"Failure type {analysis.failure_type.value} is not autonomously "
                    f"recoverable: {analysis.fix_hint}"
                ),
                recovery_attempt_num=recovery_count,
            )

        result = self._apply_strategy(task, analysis, recovery_count, queue_path)
        self._learner.record_attempt(
            analysis.failure_type, analysis.suggested_strategy, succeeded=result.success
        )

        # WS-113-002: Propagate successful patterns to relevant employees
        if result.success:
            try:
                from . import pattern_propagator
            except ImportError:
                try:
                    import pattern_propagator  # type: ignore[no-redef]
                except ImportError:
                    pattern_propagator = None

            if pattern_propagator is not None:
                try:
                    # Get updated success rate after recording
                    success_rate = self._learner.get_success_rate(
                        analysis.failure_type, analysis.suggested_strategy
                    )
                    attempts = (
                        self._learner._load()
                        .get("patterns", {})
                        .get(
                            f"{analysis.failure_type.value}:{analysis.suggested_strategy.value}",
                            {},
                        )
                        .get("attempts", 0)
                    )

                    pattern_propagator.propagate_single_pattern(
                        failure_type=analysis.failure_type.value,
                        strategy=analysis.suggested_strategy.value,
                        success_rate=success_rate,
                        attempts=attempts,
                        company_dir=self._company_dir,
                    )
                except Exception as e:
                    logger.debug(f"Pattern propagation skipped: {e}")

        return result

    def _consult_pr_state(
        self, task: dict, queue_path: Path | None, *, include_open: bool = True
    ) -> RecoveryResult | None:
        """Route a task by REAL PR state instead of queue bookkeeping.

        Returns a terminal-avoiding RecoveryResult when a merged PR (-> route to
        completed via MARK_COMPLETE) or open PR (-> route to pr_open) references
        the task, else None so the caller proceeds with its normal
        recovery/terminal logic. Never raises — a failed or inconclusive consult
        returns None (fail-open); recovery must not hang on GitHub availability.
        """
        task_id = task.get("task_id", "unknown")

        # Respect an explicit workflow-verdict rejection: the judge already
        # consulted the diff and rejected it, so that is a reality check, not
        # bookkeeping blindness. Auto-completing or holding such a task just
        # because a PR object exists would override a deliberate decision —
        # leave it to the normal workflow-phantom handling below.
        _wf = task.get("workflow_verdict") or {}
        if _wf.get("addresses_task") is False and not _wf.get("errored"):
            return None

        try:
            try:
                from . import pr_reality_check
            except ImportError:
                import pr_reality_check  # type: ignore[no-redef]
        except ImportError:
            # Helper unavailable — fail open to today's behavior.
            return None

        try:
            verdict = pr_reality_check.consult_pr_state(
                task_id,
                runner=self._pr_runner,
                project_root=self._company_dir.parent,
                include_open=include_open,
            )
        except Exception as exc:  # pragma: no cover - defensive fail-open
            logger.debug(
                "[Recovery] PR-state consult failed for %s (fail-open): %s",
                task_id,
                exc,
            )
            return None

        recovery_count = task.get("recovery_attempt_count", 0)

        if verdict.routes_to_completed:
            logger.info(
                "[Recovery] Task %s already shipped (merged PR %s, signal=%s) — "
                "routing to completed instead of terminal-marking",
                task_id,
                verdict.pr_number if verdict.pr_number is not None else "?",
                verdict.signal,
            )
            pr_ref = f"#{verdict.pr_number}" if verdict.pr_number is not None else ""
            return RecoveryResult(
                task_id=task_id,
                attempted=True,
                strategy_used=RecoveryStrategy.MARK_COMPLETE,
                success=True,
                reason=(
                    f"Merged PR {pr_ref} references this task "
                    f"(signal={verdict.signal}) — work already shipped; routing "
                    "to completed rather than terminal-marking from queue "
                    "bookkeeping"
                ),
                recovery_attempt_num=recovery_count,
                pr_url=verdict.pr_url,
                pr_number=verdict.pr_number,
            )

        if verdict.routes_to_pr_open:
            # Move the task to the pr_open lane ourselves when we own the queue
            # file. The daemon caller treats a successful non-MARK_COMPLETE
            # recovery as "already re-queued" and takes no further action, so the
            # move must happen here. With no queue_path (dry-run / unit test) we
            # just return the verdict for the caller/test to act on.
            routed = False
            if queue_path is not None:
                routed = pr_reality_check.route_task_to_pr_open(
                    queue_path,
                    task,
                    pr_url=verdict.pr_url,
                    pr_number=verdict.pr_number,
                )
                if not routed:
                    # The lane move failed (lock timeout, queue I/O) — do NOT
                    # report success, or the daemon treats the task as handled
                    # and it strands in in_progress with no failure recorded
                    # (PR 266 review). Fall through to normal recovery.
                    logger.warning(
                        "[Recovery] Task %s: pr_open lane move FAILED — "
                        "falling through to normal recovery",
                        task_id,
                    )
                    return None
            logger.info(
                "[Recovery] Task %s has an OPEN PR %s under review — routing to "
                "pr_open instead of blocked (moved=%s)",
                task_id,
                verdict.pr_number if verdict.pr_number is not None else "?",
                routed,
            )
            pr_ref = f"#{verdict.pr_number}" if verdict.pr_number is not None else ""
            return RecoveryResult(
                task_id=task_id,
                attempted=True,
                strategy_used=RecoveryStrategy.ROUTE_TO_PR_OPEN,
                success=True,
                reason=(
                    f"Open PR {pr_ref} references this task — work is under "
                    "review; routing to pr_open rather than blocked"
                ),
                recovery_attempt_num=recovery_count,
                pr_url=verdict.pr_url,
                pr_number=verdict.pr_number,
            )

        # No PR evidence — record any branch found for the audit trail, then let
        # the caller proceed with today's terminal/recovery logic.
        if verdict.branch:
            logger.debug(
                "[Recovery] Task %s: branch %s exists but no open/merged PR — "
                "proceeding with normal recovery/terminal logic",
                task_id,
                verdict.branch,
            )
        return None

    def _apply_strategy(
        self,
        task: dict,
        analysis: FailureAnalysis,
        recovery_count: int,
        queue_path: Path | None,
    ) -> RecoveryResult:
        """Apply the chosen recovery strategy."""
        # 2026-07-06: force-collected timeouts hand recovery a {"task_id": ...}
        # stub (forge_daemon builds the result without the task dict). Requeuing
        # that stub loses title/description and the retry dies with "Task has no
        # title — cannot execute". Hydrate from the queue before building the
        # retry task.
        if queue_path is not None and not (
            task.get("title") and task.get("description")
        ):
            task = self._hydrate_task_from_queue(task, queue_path)

        task_id = task.get("task_id", "unknown")
        strategy = analysis.suggested_strategy
        enhanced_desc = StrategyEngine.build_recovery_description(task, analysis)

        recovery_task = dict(task)
        recovery_task["description"] = enhanced_desc
        recovery_task["recovery_attempt_count"] = recovery_count + 1
        recovery_task["recovery_failure_type"] = analysis.failure_type.value
        recovery_task["recovery_strategy"] = strategy.value
        recovery_task["recovery_fix_hint"] = analysis.fix_hint
        recovery_task["recovery_triggered_at"] = datetime.now(timezone.utc).isoformat()
        recovery_task["status"] = "pending"
        recovery_task["claimed_by"] = None
        recovery_task["claimed_at"] = None
        recovery_task["assigned_to"] = None
        recovery_task["assigned_at"] = None
        recovery_task["started_at"] = None
        recovery_task["result"] = None
        # Reset the per-cycle retry budget so recovery strategies get a fair
        # attempt count rather than being immediately escalated because the
        # original task already exhausted most of its retries. The outer
        # MAX_RECOVERY_ATTEMPTS cap prevents unbounded loops.
        recovery_task["retry_count"] = 0
        recovery_task["retry_history"] = []

        if strategy == RecoveryStrategy.RETRY_SIMPLIFIED:
            recovery_task["complexity"] = "simple"
            recovery_task["max_attempts"] = 2
        elif strategy == RecoveryStrategy.RETRY_FRESH_WORKTREE:
            recovery_task["prefer_fresh_worktree"] = True
        elif strategy == RecoveryStrategy.RETRY_WITH_REPLAN:
            recovery_task["force_replan"] = True
            recovery_task["pipeline"] = "plan_execute"
        elif strategy == RecoveryStrategy.FIX_ENVIRONMENT:
            env_prefix = (
                "SETUP FIRST: Run `uv sync` or install missing dependencies. "
                "Verify all imports work before implementing.\n\n"
            )
            recovery_task["description"] = env_prefix + enhanced_desc

        if queue_path is not None:
            try:
                re_queued = self._requeue_task(recovery_task, queue_path)
                if re_queued:
                    logger.info(
                        "[Recovery] Task %s re-queued with strategy=%s (attempt %d)",
                        task_id,
                        strategy.value,
                        recovery_count + 1,
                    )
                    return RecoveryResult(
                        task_id=task_id,
                        attempted=True,
                        strategy_used=strategy,
                        success=True,
                        reason=f"Re-queued with {strategy.value}: {analysis.fix_hint}",
                        recovery_attempt_num=recovery_count + 1,
                        new_task_id=recovery_task.get("task_id"),
                    )
                return RecoveryResult(
                    task_id=task_id,
                    attempted=True,
                    strategy_used=strategy,
                    success=False,
                    reason="Failed to re-queue task in work queue",
                    recovery_attempt_num=recovery_count + 1,
                )
            except Exception as e:
                logger.error("[Recovery] Re-queue failed for task %s: %s", task_id, e)
                return RecoveryResult(
                    task_id=task_id,
                    attempted=True,
                    strategy_used=strategy,
                    success=False,
                    reason=f"Re-queue exception: {e}",
                    recovery_attempt_num=recovery_count + 1,
                )

        # No queue_path — return prepared result for caller to act on
        return RecoveryResult(
            task_id=task_id,
            attempted=True,
            strategy_used=strategy,
            success=True,
            reason=f"Recovery plan prepared: {strategy.value} — {analysis.fix_hint}",
            recovery_attempt_num=recovery_count + 1,
            new_task_id=recovery_task.get("task_id"),
        )

    def _hydrate_task_from_queue(self, stub: dict, queue_path: Path) -> dict:
        """Merge a task stub with its full queue entry (stub fields win).

        Searches every queue section for the stub's task_id. Returns the stub
        unchanged when the queue is unreadable or the task is not found —
        recovery must never fail because hydration did.
        """
        task_id = stub.get("task_id")
        if not task_id or not queue_path.exists():
            return stub
        try:
            with open(queue_path, encoding="utf-8") as f:
                queue = json.load(f)
        except (json.JSONDecodeError, OSError):
            return stub
        for section in ("in_progress", "pending", "blocked", "failed", "completed"):
            for entry in queue.get(section, []):
                if entry.get("task_id") == task_id:
                    merged = dict(entry)
                    merged.update({k: v for k, v in stub.items() if v is not None})
                    return merged
        return stub

    def _requeue_task(self, task: dict, queue_path: Path) -> bool:
        """Add task back to pending queue atomically, deduplicating by task_id."""
        if not queue_path.exists():
            logger.warning("[Recovery] Queue file not found: %s", queue_path)
            return False

        task_id = task.get("task_id", "unknown")

        try:
            try:
                from . import work_allocator as _wa
            except ImportError:
                import work_allocator as _wa  # type: ignore[no-redef]

            lock_path = queue_path.parent.parent / "runtime" / "queue.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with _wa.QueueLock(lock_path):
                try:
                    with open(queue_path, encoding="utf-8") as f:
                        queue = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.error("[Recovery] Failed to read queue: %s", e)
                    return False

                inserted = _wa.guarded_requeue_to_pending(queue, task)
                if not inserted:
                    logger.info(
                        "[Recovery] Task %s already in pending — skipped duplicate requeue",
                        task_id,
                    )

                fd, tmp = tempfile.mkstemp(
                    dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    os.replace(tmp, str(queue_path))
                except Exception as e:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    logger.error("[Recovery] Atomic write failed: %s", e)
                    return False

        except Exception as e:
            logger.error("[Recovery] _requeue_task failed for %s: %s", task_id, e)
            return False

        return True

    def get_learner_summary(self) -> dict[str, Any]:
        """Return pattern learner summary for reporting."""
        return self._learner.summary()


def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Autonomous failure recovery for daemon tasks"
    )
    parser.add_argument("--task-id", required=True, help="Task ID to recover")
    parser.add_argument("--error", default="", help="Error message")
    parser.add_argument("--exit-code", type=int, default=None, help="Process exit code")
    parser.add_argument(
        "--dry-run", action="store_true", help="Analyze only, no re-queue"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Show pattern learner summary"
    )
    args = parser.parse_args()

    company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    orchestrator = RecoveryOrchestrator(company_dir)

    if args.summary:
        print(json.dumps(orchestrator.get_learner_summary(), indent=2))
        return

    queue_path = company_dir / "state/work_queue.json"
    task: dict = {"task_id": args.task_id}
    if queue_path.exists():
        try:
            with open(queue_path) as f:
                queue = json.load(f)
            for section in ("in_progress", "failed", "blocked", "pending", "completed"):
                for t in queue.get(section, []):
                    if t.get("task_id") == args.task_id:
                        task = t
                        break
        except Exception as e:
            print(f"Warning: could not load task: {e}")

    target_queue = None if args.dry_run else queue_path
    result = orchestrator.attempt_recovery(
        task=task,
        error_msg=args.error,
        exit_code=args.exit_code,
        queue_path=target_queue,
    )

    print(f"\nRecovery Result for {result.task_id}:")
    print(f"  Attempted:   {result.attempted}")
    print(f"  Strategy:    {result.strategy_used.value}")
    print(f"  Success:     {result.success}")
    print(f"  Reason:      {result.reason}")
    print(f"  Attempt #:   {result.recovery_attempt_num}")
    if result.new_task_id:
        print(f"  New task ID: {result.new_task_id}")


if __name__ == "__main__":
    _cli_main()
