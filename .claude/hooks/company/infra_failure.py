#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Infra-vs-task failure classification and cross-task outage tracking.

Provider-preflight timeouts (and other pure infra blips — CLI timeouts before
a worker subprocess ever spawns, CI runs with 0 steps executed) are not
genuine task failures: the actual work was never attempted. Treating them
like real failures wastes retry budget and produces misleading "failed 3x"
escalations (the same failure class as the earlier 0-steps-CI incident that
closed innocent PRs).

This module is the single source of truth for:
1. Classifying an error message as FailureOrigin.INFRA vs FailureOrigin.TASK
2. Tracking a cross-task infra-failure streak (distinct task_ids), firing an
   "Infrastructure Outage" notification once a threshold is crossed, and
   resolving it (with a resolution notification) once a genuine success or
   task-classified failure is observed.
3. A shared backoff formula for infra-classified retries.

Callers (operation_loop.py's release_task, failure_recovery.py's
attempt_recovery) pass ``company_dir`` explicitly — this module never
defaults a path relative to cwd (see CLAUDE.md's test-isolation convention).
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# Lazy, side-effect-free import of the escalation module (for notifications).
# Supports both package and direct-script execution, same convention as the
# sibling modules in this directory.
escalation_module = None


def _ensure_imports() -> None:
    global escalation_module
    if escalation_module is not None:
        return
    try:
        from . import escalation as em
    except ImportError:
        import escalation as em  # type: ignore[no-redef]
    escalation_module = em


class FailureOrigin(Enum):
    INFRA = "infra"
    TASK = "task"


# Provider preflight: the TIMEOUT case, plus the LOGGED-OUT case.
#
# This list originally carried only the timeout, on the reasoning that a "not
# logged in" preflight is human-actionable and "will never resolve itself", so
# it should keep consuming retry budget. The second half of that premise is
# false, and it cost real work on 2026-08-15: a transient logout failed three
# freshly-generated coverage tasks three times each, and by the time auth was
# healthy again (it recovered on its own, untouched) all three were escalated
# or ceiling-blocked behind "human review required". ~2,635 lines of measured,
# admitted, queued work destroyed by a blip that lasted hours.
#
# The deeper error was aiming the wrong instrument at it. Retry budget exists
# to decide whether a TASK is viable; an environment fault says nothing about
# the task, so spending task budget on it can only ever destroy good work.
# The right response to a provider outage — transient OR persistent — is to
# leave the tasks alone and tell a human, which is precisely what the
# cross-task streak tracker below already does (escalates + notifies once
# DEFAULT_OUTAGE_THRESHOLD distinct tasks hit the same infra fault).
#
# So the human-actionable half of the original reasoning is preserved, and
# routed through the mechanism built for it, instead of being paid for out of
# the retry budget of whichever tasks happened to be running at the time.
#
# Deliberately NOT widened to every preflight failure: "CLI not installed" and
# similar are left classified as TASK for now, because that path has not been
# observed here and the same argument for it deserves its own evidence.
_INFRA_PATTERNS = [
    r"provider preflight failed[\s\S]*?(?:timed?\s*out|timeout)",
    # Logged-out / unauthenticated preflight. Matches the JSON health-check
    # body employee_activator wraps ({"loggedIn": false, "authMethod": "none"})
    # as well as prose phrasings, so a health-check format change degrades to
    # the old behaviour rather than silently matching something unintended.
    r"provider preflight failed[\s\S]*?[\"']?loggedIn[\"']?\s*:\s*false",
    r"provider preflight failed[\s\S]*?[\"']?authMethod[\"']?\s*:\s*[\"']none[\"']",
    r"provider preflight failed[\s\S]*?not\s+logged\s+in",
    # Any provider CLI (claude/gh/codex) health-check timing out before a
    # worker subprocess ever spawned.
    r"\b(?:claude|gh|codex)\b[\s\S]{0,80}?(?:timed?\s*out|timeout)",
    # CI/workflow run that executed zero steps — infra never actually ran the
    # job (the same failure class that previously closed innocent PRs).
    r"\b0\s+steps\s+executed\b",
    r"steps_executed[\"']?\s*[:=]\s*0\b",
]

_COMPILED_INFRA_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _INFRA_PATTERNS]


def classify_failure_origin(error_msg: str | None) -> FailureOrigin:
    """Classify a failure error message as infra-blip or genuine task failure.

    None/empty error messages classify as TASK (no evidence of an infra
    blip — treat conservatively so budget-consuming behavior is unchanged
    for the common case).
    """
    if not error_msg:
        return FailureOrigin.TASK
    for pattern in _COMPILED_INFRA_PATTERNS:
        if pattern.search(error_msg):
            return FailureOrigin.INFRA
    return FailureOrigin.TASK


# -----------------------------------------------------------------------------
# Cross-task outage streak tracking
# -----------------------------------------------------------------------------

INFRA_STREAK_FILE = "state/infra_failure_streak.json"  # relative to company_dir
DEFAULT_OUTAGE_THRESHOLD = 3  # distinct task_ids


def _streak_path(company_dir: Path) -> Path:
    return company_dir / INFRA_STREAK_FILE


def _default_streak_state() -> dict:
    return {
        "task_ids": [],
        "started_at": None,
        "last_seen_at": None,
        "last_error": None,
        "escalated": False,
        "escalated_at": None,
    }


def _load_streak_state(company_dir: Path) -> dict:
    path = _streak_path(company_dir)
    if not path.exists():
        return _default_streak_state()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return _default_streak_state()
    if not isinstance(data, dict):
        return _default_streak_state()
    merged = _default_streak_state()
    merged.update(data)
    return merged


def _save_streak_state(company_dir: Path, data: dict) -> None:
    """Atomic write (tempfile + os.replace), same convention as loop_monitor.py."""
    path = _streak_path(company_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".ifs_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _notify(title: str, message: str) -> None:
    """Fire a notification via the shared escalation notification mechanism.

    Best-effort/non-fatal: notification failures must never break the
    infra-failure tracking itself. Kept as a thin, separately-named function
    so tests can monkeypatch it instead of exercising the real desktop/webhook
    notification path.
    """
    try:
        _ensure_imports()
        escalation_module.send_notification(title, message)
    except Exception:
        pass


def record_infra_failure(
    company_dir: Path,
    task_id: str,
    error_msg: str,
    threshold: int = DEFAULT_OUTAGE_THRESHOLD,
) -> dict:
    """Record an infra-classified failure for ``task_id`` and check for an outage.

    Tracks DISTINCT task_ids (not raw occurrence count) — repeated infra
    failures on the same task don't indicate a wider outage. Fires exactly
    one "Infrastructure Outage Detected" notification the first time the
    distinct-task count crosses ``threshold``.

    Returns the (persisted) state dict.
    """
    data = _load_streak_state(company_dir)
    now = datetime.now(timezone.utc).isoformat()

    if task_id not in data["task_ids"]:
        data["task_ids"].append(task_id)
        if not data.get("started_at"):
            data["started_at"] = now

    data["last_seen_at"] = now
    data["last_error"] = (error_msg or "")[:300]

    distinct_count = len(data["task_ids"])
    if distinct_count >= threshold and not data.get("escalated"):
        data["escalated"] = True
        data["escalated_at"] = now
        _notify(
            "Infrastructure Outage Detected",
            f"{distinct_count} distinct tasks hit infra-classified failures "
            f"since {data.get('started_at')}. Last error: {data['last_error']}",
        )

    _save_streak_state(company_dir, data)
    return data


def reset_infra_streak(company_dir: Path) -> dict | None:
    """Clear an open infra-failure streak after a success or task-classified failure.

    Returns the resolved {"start", "end", ...} window dict if a streak was
    open, or None if there was nothing to reset. Fires an "Infrastructure
    Outage Resolved" notification only if the streak had actually been
    escalated (crossed the threshold) — a streak that never reached the
    threshold resolves silently.
    """
    data = _load_streak_state(company_dir)
    if not data.get("task_ids"):
        return None

    now = datetime.now(timezone.utc).isoformat()
    was_escalated = bool(data.get("escalated"))
    resolved_window = {
        "start": data.get("started_at"),
        "end": now,
        "task_ids": list(data["task_ids"]),
    }

    if was_escalated:
        _notify(
            "Infrastructure Outage Resolved",
            f"Outage window {resolved_window['start']} -> {now} affected "
            f"{len(resolved_window['task_ids'])} distinct tasks.",
        )

    _save_streak_state(company_dir, _default_streak_state())
    return resolved_window


def backoff_seconds(attempt: int, base: float = 15.0, cap: float = 300.0) -> float:
    """Exponential backoff with jitter for infra-classified retries.

    Deliberately shorter/faster than the genuine-failure backoff in
    operation_loop.py — infra blips are expected to resolve quickly, so
    retries should be prompt but still back off if the outage persists.
    """
    return min(base * (2 ** max(attempt - 1, 0)), cap) + random.uniform(0, 5.0)
