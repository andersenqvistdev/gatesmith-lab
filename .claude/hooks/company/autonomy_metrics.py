# /// script
# requires-python = ">=3.10"
# ///
"""Verdict-calibration & composite autonomy metrics (Phase 1).

Two distinct signals, deliberately kept separate (the project's hard-won lesson —
see ws116_autonomy_assessment / recovery_mark_complete_escalation_gate memories):

1. ``compute_autonomy()`` — a LOCAL proxy read only from work_queue.json, deduped
   by ``task_id``: distinct tasks that reached a PR / distinct tasks ever queued.
   It is an UPPER BOUND because a ``pr_url`` proves a PR was *opened*, not merged.

2. ``verify_task_merged()`` / ``run_tier1()`` — GROUND TRUTH via the ``gh`` CLI:
   a task is genuinely shipped only if its PR is ``MERGED`` with a real diff. This
   is the "is done actually done?" check that turns the proxy into a trustworthy
   number and exposes the phantom-completion leak.

Design rules:
- Every reader tolerates missing/corrupt files (mirrors PatternLearner._load).
- ``company_dir`` is always an explicit argument — never cwd-defaulted — so tests
  pass ``tmp_path`` and real ``.company/`` state cannot leak in (CLAUDE.md: "Test
  isolation for path-defaulting configs").
- ``gh`` verification FAILS CLOSED: any error / empty output / unmerged / empty
  diff counts as NOT shipped, never as success (the empty-stdout-as-success bug
  class, #1017).
- Failing closed is not the same as *claiming a phantom*. A transport failure
  (gh timeout / gh missing / empty stdout / non-JSON) means "no verdict was
  reached", and is reported as ``unverified`` — excluded from the trust maths
  rather than counted against the run. See :func:`verify_task_merged`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

AUTONOMY_AUDIT_FILE = "state/autonomy_audit.json"

# Regex that matches goal IDs (G1–G99) embedded in task titles/descriptions.
# Used for per-goal proxy breakdown without a formal goal_id field in the queue schema.
_GOAL_RE = re.compile(r"\b(G\d{1,2})\b")

# Lanes of work_queue.json that represent a task that was ever queued. Deduped by
# task_id, this is the honest denominator (every other counter in .company/state
# is per-attempt and double-counts retries). The "cancelled" lane is left out on
# purpose: an operator-ended task is neither a success nor a failure of the
# engine, so it is in no numerator and no denominator (QUEUE-CANCEL-STATUS).
_QUEUE_LANES = (
    "proposed",
    "pending",
    "in_progress",
    "blocked",
    "review",
    "pr_open",
    "completed",
    "failed",
    "active",
)


def _is_stage_task(task: Any) -> bool:
    """A CHALLENGER-STAGE task (challenger review or continuation round).

    It reviews or finishes a builder task's PR and never opens one of its
    own, so it is neither a unit of delivered work nor a failure to deliver:
    it is left out of every numerator and denominator here (otherwise each
    review would count as a queued task that "completed without a PR").

    A pr_rebase task (AUTO-REBASE-CONFLICTING-PRS) is left out the same way:
    it rebases a builder's PR and never opens one of its own."""
    if not isinstance(task, dict):
        return False
    return (
        task.get("source") == "challenger" and isinstance(task.get("challenger"), dict)
    ) or (task.get("source") == "rebase" and isinstance(task.get("rebase"), dict))


def _load(path: Path, default: Any) -> Any:
    """Load JSON, returning ``default`` on missing/corrupt/unreadable file."""
    try:
        if not path.exists():
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _rate(num: int, denom: int) -> float:
    return round(num / denom, 3) if denom else 0.0


def _resolve_queue_path(company_dir: Path) -> Path:
    """work_queue.json lives at state/ in production; fall back to the legacy
    root location so the function works in both layouts."""
    state_path = company_dir / "state" / "work_queue.json"
    root_path = company_dir / "work_queue.json"
    # Prefer the canonical state/ path; only fall back to the legacy root
    # location when it actually exists. When neither exists, default to state/.
    if not state_path.exists() and root_path.exists():
        return root_path
    return state_path


# ---------------------------------------------------------------------------
# Cohort windowing — make the metric honest about TREND (Phase 0).
#
# The lifetime denominator freezes historically-failed tasks (e.g. the April
# CI-halt graveyard) into every future score, which hides all current progress
# (recent cohort ~75% vs lifetime ~30%). A windowed rate over recent task
# cohorts shows current capability. Task ids embed the creation date:
# ``task-YYYYMMDDhhmmss-xxxxxx`` — so a lexical compare of the YYYYMMDD prefix
# is a valid date compare.
# ---------------------------------------------------------------------------

_COHORT_RE = re.compile(r"(20\d{6})")


def task_cohort_date(task_id: str | None) -> str | None:
    """Extract the ``YYYYMMDD`` cohort date embedded in a task id, or None."""
    m = _COHORT_RE.search(task_id or "")
    return m.group(1) if m else None


def _window_cutoff(window_days: int, now: datetime | None = None) -> str:
    """Inclusive ``YYYYMMDD`` lower bound for a trailing ``window_days`` window."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=window_days)).strftime("%Y%m%d")


def _in_window(task_id: str | None, cutoff: str | None) -> bool:
    """True if the task's cohort date is >= ``cutoff`` (YYYYMMDD).

    ``cutoff`` None means no window (lifetime — everything is in). A task with no
    parseable date is treated as HISTORICAL (excluded from any window), so the
    recent number is never inflated by undatable rows.
    """
    if cutoff is None:
        return True
    d = task_cohort_date(task_id)
    return d is not None and d >= cutoff


def _normalize_since(since: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` or ``YYYYMMDD`` (or None); return ``YYYYMMDD``/None."""
    if not since:
        return None
    return since.replace("-", "")


# ---------------------------------------------------------------------------
# 1. Local autonomy proxy (no network)
# ---------------------------------------------------------------------------


def compute_autonomy(
    company_dir: Path,
    *,
    window_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Composite LOCAL autonomy proxy, deduped by task_id.

    A task in the ``completed`` lane is NOT necessarily a success — that lane also
    holds stale/cancelled/blocked/escalated entries, and ``result`` can even be
    "failed". The success label is the per-task ``status`` field == "completed".
    Requiring a ``pr_url`` enforces the deliverable invariant (~71% of
    status-complete tasks have one), which is why this is an upper bound.

    When ``window_days`` is set, only tasks whose embedded cohort date is within
    the trailing window are counted (the TREND-honest number); ``None`` (default)
    is the lifetime proxy and is unchanged for back-compat.
    """
    wq = _load(_resolve_queue_path(company_dir), default={})
    cutoff = _window_cutoff(window_days, now) if window_days else None

    queued = {
        t["task_id"]
        for lane in _QUEUE_LANES
        for t in wq.get(lane, []) or []
        if isinstance(t, dict)
        and t.get("task_id")
        and not _is_stage_task(t)
        and _in_window(t.get("task_id"), cutoff)
    }
    completed = [
        t
        for t in wq.get("completed", []) or []
        if isinstance(t, dict)
        and not _is_stage_task(t)
        and t.get("status") == "completed"
        and _in_window(t.get("task_id"), cutoff)
    ]
    with_pr = {t["task_id"] for t in completed if t.get("task_id") and t.get("pr_url")}

    result = {
        "distinct_tasks_queued": len(queued),
        "distinct_completed_status": len(completed),
        "distinct_completed_with_pr": len(with_pr),
        "autonomy_proxy_rate": _rate(len(with_pr), len(queued)),
    }
    if window_days:
        result["window_days"] = window_days
        result["cohort_start"] = cutoff
    return result


def completed_tasks_with_pr(
    company_dir: Path,
    *,
    window_days: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the status-complete, PR-bearing tasks the harness should verify.

    Each item is trimmed to the fields the verifier needs (task_id, pr_url, title,
    description) so the caller / workflow gets a small, JSON-safe payload.
    ``window_days`` restricts to recent cohorts (see :func:`compute_autonomy`).
    """
    wq = _load(_resolve_queue_path(company_dir), default={})
    cutoff = _window_cutoff(window_days, now) if window_days else None
    out: list[dict[str, Any]] = []
    for t in wq.get("completed", []) or []:
        if not isinstance(t, dict) or _is_stage_task(t):
            continue
        if (
            t.get("status") == "completed"
            and t.get("task_id")
            and t.get("pr_url")
            and _in_window(t.get("task_id"), cutoff)
        ):
            out.append(
                {
                    "task_id": t["task_id"],
                    "pr_url": t["pr_url"],
                    "title": t.get("title"),
                    "description": t.get("description"),
                }
            )
    return out


# Local funnel buckets that are knowable WITHOUT the network. The merged vs
# closed vs phantom split of ``completed_with_pr`` needs gh (``tier1`` / the
# /calibrate Tier-2 pass); everything else is decided from work_queue.json alone.
def _local_bucket(task: dict[str, Any]) -> str:
    status = task.get("status")
    has_pr = bool(task.get("pr_url"))
    if status == "completed" and has_pr:
        return "completed_with_pr"
    if status == "completed":
        return "completed_no_pr"
    if status in ("blocked", "escalated"):
        return "blocked_escalated"
    if status in ("pending", "in_progress", "active"):
        return "in_flight"
    if status == "failed":
        return "failed"
    return "other"


def local_cohorts(company_dir: Path, *, since: str | None = None) -> dict[str, Any]:
    """LOCAL (no-network) funnel decomposition, deduped by task_id and split into
    historical vs recent by ``since`` (``YYYY-MM-DD`` or ``YYYYMMDD``).

    Reproduces the funnel framing in .planning/autonomy/00-MASTER.md from local
    state. The merged/closed/phantom breakdown of ``completed_with_pr`` requires
    gh — run ``tier1`` or ``/calibrate`` for that.
    """
    wq = _load(_resolve_queue_path(company_dir), default={})
    cutoff = _normalize_since(since)
    tasks: dict[str, dict[str, Any]] = {}
    for lane in _QUEUE_LANES:
        for t in wq.get(lane, []) or []:
            if isinstance(t, dict) and t.get("task_id") and not _is_stage_task(t):
                tasks[t["task_id"]] = t

    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"historical": 0, "recent": 0, "total": 0}
    )
    for tid, t in tasks.items():
        b = _local_bucket(t)
        recent = _in_window(tid, cutoff)
        buckets[b]["recent" if recent else "historical"] += 1
        buckets[b]["total"] += 1

    return {
        "since": cutoff,
        "total_tasks": len(tasks),
        "buckets": {k: dict(v) for k, v in buckets.items()},
    }


# ---------------------------------------------------------------------------
# 2. Ground-truth verification via gh (fails closed)
# ---------------------------------------------------------------------------

# A transport failure is retried this many times before the task is declared
# unverifiable. GitHub flakiness is bursty and short-lived: on 2026-07-25 a
# single bad window produced 15 `gh` TimeoutExpired results, every one of which
# was recorded as a phantom, dropping the published autonomy rate from 72.3% to
# 63.2% while nothing about the underlying work had changed.
VERIFY_ATTEMPTS = 3
VERIFY_RETRY_SLEEP_SECONDS = 2.0


def _pr_number_from_url(pr_url: str) -> str | None:
    m = re.search(r"/pull/(\d+)", pr_url or "")
    return m.group(1) if m else None


def verify_task_merged(
    pr_url: str,
    *,
    task_id: str | None = None,
    timeout: int = 30,
    runner: Callable[..., Any] = subprocess.run,
    attempts: int = VERIFY_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Authoritative check that a task's PR is actually MERGED with a real diff.

    Returns a dict with ``merged`` (bool) and the supporting fields. Fails closed:
    gh error / nonzero / empty stdout / non-JSON / not-merged / empty-diff all
    yield ``merged=False``. ``runner`` is injectable for tests.

    Two distinct kinds of ``merged=False`` are reported, and callers must keep
    them apart:

    * ``unverified=False`` — a real verdict was reached. The PR exists and is
      demonstrably not a merged-with-diff success (OPEN, CLOSED, empty diff), or
      the url carries no PR number at all. This is evidence of a phantom.
    * ``unverified=True`` — no verdict could be reached because ``gh`` itself
      failed (timeout, missing binary, nonzero exit, empty stdout, non-JSON)
      after ``attempts`` tries. This is evidence of nothing; the trust maths
      excludes it (see :func:`summarize_ground_truth`) instead of charging the
      run for the tooling's bad night.

    ``sleeper`` is injectable so tests exercise the retry path without waiting.
    """
    out: dict[str, Any] = {
        "task_id": task_id,
        "pr_url": pr_url,
        "pr_number": None,
        "merged": False,
        "unverified": False,
        "state": None,
        "merged_at": None,
        "additions": 0,
        "deletions": 0,
        "reason": "",
        "attempts": 0,
    }

    num = _pr_number_from_url(pr_url)
    if not num:
        # A malformed url is a real verdict, not a transport failure: retrying
        # cannot change it and no PR was ever demonstrably shipped.
        out["reason"] = "no PR number in url"
        return out
    out["pr_number"] = num

    data: dict[str, Any] | None = None
    for attempt in range(1, max(1, attempts) + 1):
        out["attempts"] = attempt
        try:
            res = runner(
                [
                    "gh",
                    "pr",
                    "view",
                    num,
                    "--json",
                    "state,mergedAt,additions,deletions",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as e:  # gh missing / hung
            out["reason"] = f"gh error: {type(e).__name__}"
        else:
            if getattr(res, "returncode", 1) != 0 or not (res.stdout or "").strip():
                out["reason"] = "gh nonzero/empty (fail closed)"
            else:
                try:
                    data = json.loads(res.stdout)
                except json.JSONDecodeError:
                    out["reason"] = "gh non-json output"
                else:
                    break  # verdict reached

        if attempt < max(1, attempts):
            sleeper(VERIFY_RETRY_SLEEP_SECONDS)

    if data is None:
        # Every attempt hit the transport layer — we know nothing about this PR.
        out["unverified"] = True
        return out

    out["state"] = data.get("state")
    out["merged_at"] = data.get("mergedAt")
    out["additions"] = data.get("additions") or 0
    out["deletions"] = data.get("deletions") or 0
    diff = out["additions"] + out["deletions"]

    if data.get("state") == "MERGED" and data.get("mergedAt") and diff > 0:
        out["merged"] = True
        out["reason"] = "merged with diff"
    else:
        out["reason"] = f"not shipped (state={out['state']}, diff={diff})"
    return out


def run_tier1(
    tasks: list[dict[str, Any]],
    *,
    timeout: int = 30,
    runner: Callable[..., Any] = subprocess.run,
    attempts: int = VERIFY_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic Tier-1 pass over completed-with-PR tasks.

    Returns ``(results, survivors)`` where ``results`` is one verify dict per task
    and ``survivors`` are the merged-with-diff tasks that proceed to Tier-2
    semantic judgment (carrying title/description for the judge agents).

    Results carrying ``unverified=True`` reached no verdict (see
    :func:`verify_task_merged`); they are neither survivors nor phantoms.
    """
    results: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    for t in tasks:
        v = verify_task_merged(
            t.get("pr_url", ""),
            task_id=t.get("task_id"),
            timeout=timeout,
            runner=runner,
            attempts=attempts,
            sleeper=sleeper,
        )
        results.append(v)
        if v["merged"]:
            survivors.append(
                {
                    "task_id": t.get("task_id"),
                    "pr_url": t.get("pr_url"),
                    "pr_number": v["pr_number"],
                    "title": t.get("title"),
                    "description": t.get("description"),
                }
            )
    return results, survivors


# ---------------------------------------------------------------------------
# 2b. Reconcile stale completions (measurement hygiene)
# ---------------------------------------------------------------------------

# Terminal, non-success status for a task whose PR ended CLOSED without merging.
# Deliberately NOT "failed" — that would re-arm failure_recovery; this is a final
# state. The proxy / Tier-1 candidate set both gate on status == "completed", so a
# reconciled task simply stops counting as a completed-with-PR success.
RECONCILED_STATUS = "closed_unmerged"


def _extract_roadmap_task_id(task: dict[str, Any]) -> str | None:
    """Pull ``roadmap_task_id`` out of a task's JSON-encoded notes, if present.

    Mirrors ``operation_loop._update_roadmap_on_completion``'s extraction so a
    task reconciled to "completed" here clears the same ``roadmap_state.json``
    bookkeeping that the synchronous same-worker completion path clears —
    otherwise the id never leaves ``tasks_scheduled`` and the daemon re-mints
    the already-shipped roadmap item indefinitely.
    """
    notes = task.get("notes", [])
    for note in notes if isinstance(notes, list) else []:
        content = note.get("content", "") if isinstance(note, dict) else str(note)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and "roadmap_task_id" in parsed:
            return parsed["roadmap_task_id"]
    return None


# Terminal non-success statuses that a *merged* PR should overturn. A task in one
# of these states whose PR demonstrably merged did ship — the bookkeeping lost,
# not the work. "failed" is deliberately absent: failed tasks are owned by
# failure_recovery, and promoting one out from under it would race the retry
# machinery. "blocked" IS included: it is set only after retries are exhausted
# or a deliverable gate holds a PR for manual review (operation_loop.py) — a
# final, human-attention state that failure_recovery no longer touches, so
# there is nothing left to race. Observed cause: duplicate dispatch of one
# roadmap item, where the cancelled loser is the task whose PR merged (tasks
# 8c4d8a/#269, 43a3fc/#290); a stranded-in-"blocked" case (task 6b77c9/#354)
# needed a manual operator note because reconcile never swept it, which kept
# it as a permanent "recent window failure" pointer to already-shipped work.
SHIPPED_RECOVERABLE_STATUSES = (RECONCILED_STATUS, "cancelled", "blocked")


def reconcile_closed_unmerged(
    queue: dict[str, Any],
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 30,
    now: datetime | None = None,
    window_days: int | None = None,
    pr_open_only: bool = False,
    attempts: int = VERIFY_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
    close_attribution: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reconcile stale completions and pr_open tasks against actual PR state.

    **Completed-lane tasks** (existing behaviour): a task with
    ``status == "completed"`` whose PR ended CLOSED-unmerged is flipped to
    :data:`RECONCILED_STATUS` so it stops counting as a shipped success.

    **pr_open-lane tasks** (new): a task parked in the ``pr_open`` lane
    (PR created but merge not yet confirmed) is advanced or reset:

    * PR **MERGED** → move to ``completed`` with ``status="completed"``
    * PR **CLOSED** unmerged **by the daemon** (its close comment starts with
      "Auto-closed"/"Auto-closing") → return to ``pending`` with
      ``retry_history`` popped (the task is rebuilt from scratch)
    * PR **CLOSED** unmerged by anyone else — a human declined the work →
      move to the terminal ``cancelled`` lane (``cancel_reason`` "PR closed
      by <actor>"); never requeued (QUEUE-CANCEL-STATUS: a human-closed
      webapp PR was rebuilt as a second PR within the hour)
    * PR **OPEN**, gh error, or a close nobody can attribute → leave as
      ``pr_open`` (fail-safe)

    ``close_attribution(pr_url)`` is injectable; the default asks gh via
    task_cancel.pr_close_attribution with ``runner``.

    **Shipped-but-not-completed tasks** (the mirror case): a completed-lane task
    carrying a terminal non-success status (:data:`RECONCILED_STATUS` or
    ``cancelled``) whose PR is nonetheless **MERGED with a real diff** is flipped
    back to ``status="completed"``. Duplicate dispatch is the usual cause — two
    tasks race on one piece of work, the loser is cancelled, and its PR is the
    one that merges. Left uncorrected the shipped work is invisible to every
    consumer that gates on ``status == "completed"`` (the Tier-1 candidate set,
    the local proxy, G7's success rate), so real deliveries silently depress the
    autonomy number and keep the "improve success rate" autofill armed.

    Fail SAFE in both directions — only a *definitively* CLOSED-unmerged PR
    demotes a task, and only a *definitively* MERGED-with-diff PR promotes one.
    gh error / timeout / unknown state never move anything.

    ``window_days`` bounds the work to recent cohorts; ``None`` checks every
    pr_open/completed-with-PR task.

    Mutates ``queue`` in place and returns ``(queue, changes)`` where each
    change dict carries a ``"transition"`` key: ``"closed_unmerged"`` |
    ``"pr_open_merged"`` | ``"pr_open_closed"`` | ``"pr_open_cancelled"`` |
    ``"shipped_not_completed"``.
    Pure: performs NO file I/O — the caller owns load/lock/save.
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    cutoff = _window_cutoff(window_days, now) if window_days else None
    changes: list[dict[str, Any]] = []

    # --- Phase 1: existing completed-lane hygiene ---
    # pr_open_only skips this pass entirely: it is the network-heavy part
    # (one gh call per completed-with-PR task in the window), while the
    # pr_open lane is small and time-sensitive (P1-R2 fast cadence).
    for t in [] if pr_open_only else (queue.get("completed", []) or []):
        if not isinstance(t, dict):
            continue
        if t.get("status") != "completed" or not t.get("pr_url"):
            continue
        if not _in_window(t.get("task_id"), cutoff):
            continue
        v = verify_task_merged(
            t["pr_url"],
            task_id=t.get("task_id"),
            timeout=timeout,
            runner=runner,
            attempts=attempts,
            sleeper=sleeper,
        )
        if v.get("state") == "CLOSED" and not v.get("merged"):
            t["status"] = RECONCILED_STATUS
            t["reconciled_at"] = ts
            t["reconciled_reason"] = v.get("reason", "closed unmerged")
            changes.append(
                {
                    "task_id": t.get("task_id"),
                    "pr_url": t["pr_url"],
                    "pr_number": v.get("pr_number"),
                    "reason": v.get("reason"),
                    "transition": "closed_unmerged",
                }
            )

    # --- Phase 1b: the mirror — terminal non-success whose PR actually merged ---
    # Runs over the same lane but the disjoint status set, so a task touched by
    # Phase 1 is never revisited here (Phase 1 requires CLOSED-unmerged, this
    # requires MERGED — the two can never both hold, so there is no oscillation).
    demoted_ids = {c["task_id"] for c in changes}
    for t in [] if pr_open_only else (queue.get("completed", []) or []):
        if not isinstance(t, dict):
            continue
        if t.get("status") not in SHIPPED_RECOVERABLE_STATUSES or not t.get("pr_url"):
            continue
        if t.get("task_id") in demoted_ids:
            continue
        if not _in_window(t.get("task_id"), cutoff):
            continue
        v = verify_task_merged(
            t["pr_url"],
            task_id=t.get("task_id"),
            timeout=timeout,
            runner=runner,
            attempts=attempts,
            sleeper=sleeper,
        )
        if v.get("merged"):
            previous = t.get("status")
            t["status"] = "completed"
            t.setdefault("completed_at", ts)
            t["reconciled_at"] = ts
            t["reconciled_reason"] = f"PR merged despite status={previous}"
            changes.append(
                {
                    "task_id": t.get("task_id"),
                    "pr_url": t["pr_url"],
                    "pr_number": v.get("pr_number"),
                    "reason": v.get("reason"),
                    "previous_status": previous,
                    "transition": "shipped_not_completed",
                }
            )

    # --- Phase 2: pr_open lane — advance to completed or reset to pending ---
    if close_attribution is None:

        def close_attribution(url: str) -> dict[str, Any] | None:
            return _pr_close_attribution(url, runner)

    to_cancel: list[dict[str, Any]] = []
    pr_open_tasks = list(queue.get("pr_open", []) or [])
    remaining_pr_open: list[dict[str, Any]] = []
    to_complete: list[dict[str, Any]] = []
    to_pending: list[dict[str, Any]] = []

    for t in pr_open_tasks:
        if not isinstance(t, dict):
            remaining_pr_open.append(t)
            continue
        if not t.get("pr_url"):
            remaining_pr_open.append(t)
            continue
        if not _in_window(t.get("task_id"), cutoff):
            remaining_pr_open.append(t)
            continue
        v = verify_task_merged(
            t["pr_url"],
            task_id=t.get("task_id"),
            timeout=timeout,
            runner=runner,
            attempts=attempts,
            sleeper=sleeper,
        )
        if v.get("merged"):
            # PR merged — advance to completed
            t["status"] = "completed"
            t["completed_at"] = ts
            t["reconciled_at"] = ts
            t["reconciled_reason"] = "PR merged (confirmed by reconcile)"
            to_complete.append(t)
            changes.append(
                {
                    "task_id": t.get("task_id"),
                    "pr_url": t["pr_url"],
                    "pr_number": v.get("pr_number"),
                    "reason": v.get("reason"),
                    "transition": "pr_open_merged",
                }
            )
        elif v.get("state") == "CLOSED" and not v.get("merged"):
            who = close_attribution(t["pr_url"])
            if who is None:
                # Closed, but by whom cannot be told now — look again next pass.
                remaining_pr_open.append(t)
                continue
            if not who.get("closed_by_daemon"):
                # A human closed it: the work was declined. End the task.
                actor = who.get("actor") or "a human"
                _mark_cancelled(t, f"PR closed by {actor}", actor, ts)
                to_cancel.append(t)
                changes.append(
                    {
                        "task_id": t.get("task_id"),
                        "pr_url": t["pr_url"],
                        "pr_number": v.get("pr_number"),
                        "reason": f"PR closed by {actor}",
                        "actor": actor,
                        "transition": "pr_open_cancelled",
                    }
                )
                continue
            # The daemon closed it — return to pending for rebuild
            t["status"] = "pending"
            t.pop("retry_history", None)  # pop per reset convention
            t["reconciled_at"] = ts
            t["reconciled_reason"] = v.get("reason", "closed unmerged")
            # Clear PR-open bookkeeping so a fresh attempt starts clean
            t.pop("pr_open_at", None)
            to_pending.append(t)
            changes.append(
                {
                    "task_id": t.get("task_id"),
                    "pr_url": t["pr_url"],
                    "pr_number": v.get("pr_number"),
                    "reason": v.get("reason"),
                    "transition": "pr_open_closed",
                }
            )
        else:
            # OPEN or unknown — leave as pr_open (fail-safe)
            remaining_pr_open.append(t)

    # Apply pr_open transitions in-place
    queue["pr_open"] = remaining_pr_open
    if to_complete:
        if "completed" not in queue:
            queue["completed"] = []
        queue["completed"].extend(to_complete)
    if to_pending:
        # Insert at front of pending so rebuilt tasks are picked up promptly
        queue.setdefault("pending", [])
        queue["pending"] = to_pending + queue["pending"]
    if to_cancel:
        queue.setdefault("cancelled", []).extend(to_cancel)

    return queue, changes


def _pr_close_attribution(
    pr_url: str, runner: Callable[..., Any] = subprocess.run
) -> dict[str, Any] | None:
    """Daemon close vs human close (task_cancel.pr_close_attribution)."""
    try:
        from . import task_cancel as _tc
    except ImportError:
        import task_cancel as _tc  # type: ignore[no-redef]
    return _tc.pr_close_attribution(pr_url, runner)


def _mark_cancelled(task: dict[str, Any], reason: str, by: str, ts: str) -> None:
    try:
        from . import work_allocator as _wa
    except ImportError:
        import work_allocator as _wa  # type: ignore[no-redef]
    _wa.mark_cancelled(task, reason, by, ts)


def reconcile_queue_closed_unmerged(
    company_dir: Path,
    *,
    dry_run: bool = False,
    timeout: int = 30,
    window_days: int | None = None,
    pr_open_only: bool = False,
) -> dict[str, Any]:
    """Locked load → reconcile → save against the real work queue.

    Two-phase to avoid holding the queue lock across slow ``gh`` calls:

    1. WITHOUT the lock, snapshot the queue and run the gh checks to compute
       all state changes (completed-lane flips + pr_open transitions).
    2. WITH the lock held briefly, reload the queue fresh and apply only those
       changes — re-checking each task is still in the expected state so a
       concurrent daemon write is never clobbered — then save.

    ``dry_run`` performs phase 1 only and reports what *would* change.
    """
    import work_allocator as wa  # local import: avoids a hard load-time coupling

    snapshot = wa.load_queue()
    if pr_open_only and not (snapshot.get("pr_open") or []):
        # Fast-cadence path with nothing to do: zero gh calls, zero locking.
        # Goal crediting still starts: the daemon's own merge loop moves tasks
        # straight to "completed" without ever passing through pr_open here.
        if not dry_run:
            _credit_goals(company_dir, snapshot)
        return {"reconciled": 0, "dry_run": dry_run, "changes": []}
    _, changes = reconcile_closed_unmerged(
        snapshot,
        timeout=timeout,
        window_days=window_days,
        pr_open_only=pr_open_only,
    )
    if dry_run:
        return {"reconciled": len(changes), "dry_run": dry_run, "changes": changes}
    if not changes:
        _credit_goals(company_dir, snapshot)
        return {"reconciled": 0, "dry_run": dry_run, "changes": changes}

    by_id = {c["task_id"]: c for c in changes}
    ts = datetime.now(timezone.utc).isoformat()
    applied: list[dict[str, Any]] = []
    newly_completed: list[dict[str, Any]] = []

    with wa.QueueLock(wa.get_lock_path()):
        queue = wa.load_queue()

        # Apply completed-lane flips (closed_unmerged)
        for t in queue.get("completed", []) or []:
            if not isinstance(t, dict):
                continue
            c = by_id.get(t.get("task_id"))
            if (
                c
                and c.get("transition") == "closed_unmerged"
                and t.get("status") == "completed"
                and t.get("pr_url") == c["pr_url"]
            ):
                t["status"] = RECONCILED_STATUS
                t["reconciled_at"] = ts
                t["reconciled_reason"] = c.get("reason", "closed unmerged")
                applied.append(c)

        # Apply completed-lane promotions (shipped_not_completed)
        for t in queue.get("completed", []) or []:
            if not isinstance(t, dict):
                continue
            c = by_id.get(t.get("task_id"))
            if (
                c
                and c.get("transition") == "shipped_not_completed"
                and t.get("status") in SHIPPED_RECOVERABLE_STATUSES
                and t.get("pr_url") == c["pr_url"]
            ):
                previous = t.get("status")
                t["status"] = "completed"
                t.setdefault("completed_at", ts)
                t["reconciled_at"] = ts
                t["reconciled_reason"] = f"PR merged despite status={previous}"
                applied.append(c)
                newly_completed.append(t)

        # Apply pr_open transitions (pr_open_merged / pr_open_closed)
        pr_open_before = list(queue.get("pr_open", []) or [])
        remaining_pr_open: list[dict[str, Any]] = []
        for t in pr_open_before:
            if not isinstance(t, dict):
                remaining_pr_open.append(t)
                continue
            c = by_id.get(t.get("task_id"))
            if not c or t.get("pr_url") != c.get("pr_url"):
                remaining_pr_open.append(t)
                continue
            if c["transition"] == "pr_open_merged":
                t["status"] = "completed"
                t["completed_at"] = ts
                t["reconciled_at"] = ts
                t["reconciled_reason"] = c.get("reason", "PR merged")
                queue.setdefault("completed", []).append(t)
                applied.append(c)
                newly_completed.append(t)
            elif c["transition"] == "pr_open_cancelled":
                _mark_cancelled(
                    t, c.get("reason") or "PR closed", c.get("actor") or "a human", ts
                )
                queue.setdefault("cancelled", []).append(t)
                applied.append(c)
            elif c["transition"] == "pr_open_closed":
                t["status"] = "pending"
                t.pop("retry_history", None)
                t["reconciled_at"] = ts
                t["reconciled_reason"] = c.get("reason", "closed unmerged")
                t.pop("pr_open_at", None)
                queue.setdefault("pending", [])
                queue["pending"].insert(0, t)
                applied.append(c)
            else:
                remaining_pr_open.append(t)
        queue["pr_open"] = remaining_pr_open

        if applied:
            wa.save_queue(queue)

    # Outside the queue lock (roadmap_state.json/ROADMAP.md have their own
    # I/O, unrelated to wa.QueueLock): clear tasks_scheduled for any
    # roadmap-sourced task that just reconciled to "completed" here, the same
    # way operation_loop._update_roadmap_on_completion does for the
    # synchronous same-worker path. Without this, a roadmap task whose
    # completion is only ever discovered via reconcile (pr_open lane, or a
    # stranded-worker recovery) never leaves tasks_scheduled and the daemon
    # re-mints the already-shipped roadmap item indefinitely.
    for t in newly_completed:
        roadmap_task_id = _extract_roadmap_task_id(t)
        if not roadmap_task_id:
            continue
        try:
            import roadmap_scheduler as rs  # local import: avoids a hard load-time coupling

            rs.mark_task_completed(roadmap_task_id)
            rs.update_roadmap_task_status(roadmap_task_id, "complete")
        except Exception:
            pass  # Non-fatal — best-effort write-back, mirrors _update_roadmap_on_completion

    _credit_goals(company_dir, wa.load_queue())
    return {"reconciled": len(applied), "dry_run": dry_run, "changes": applied}


def _credit_goals(company_dir: Path, queue: dict[str, Any]) -> None:
    """Start a background goal-credit sweep (goal_crediting.py); never waits.

    Reconcile is where "this PR merged" becomes ground truth outside the
    daemon's protected merge loop, and it runs on every fast pr_open cadence
    (5 min) and every 6-hour sweep. The sweep reads the whole completed lane,
    so it also covers tasks the daemon's merge loop completed directly. It runs
    in its own thread because one goal-judge call can take minutes: reconcile
    is called inline from the daemon loop and must not wait on it. Best-effort,
    never raises.
    """
    try:
        try:
            from . import goal_crediting as gc  # type: ignore[attr-defined]
        except ImportError:
            import goal_crediting as gc  # type: ignore[no-redef]
        gc.start_credit_sweep(Path(company_dir), queue)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 3. Aggregate into a trust snapshot
# ---------------------------------------------------------------------------


def summarize_ground_truth(
    tier1: list[dict[str, Any]],
    tier2: list[dict[str, Any]],
    *,
    distinct_tasks_queued: int,
    window_days: int | None = None,
    distinct_tasks_queued_windowed: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine Tier-1 (gh merged-check) and Tier-2 (semantic judgment) into the
    trust signals.

    A sampled task counts as genuinely shipped ("real") iff Tier-1 says merged AND
    (it was not judged in Tier-2 OR Tier-2 says the diff addresses the task).
    Tier-2 entries are ``{task_id, addresses_task: bool, ...}``.

    When ``window_days`` and ``distinct_tasks_queued_windowed`` are both supplied,
    a parallel set of TREND-honest windowed fields is added (recent cohort only):
    ``verified_autonomy_rate_windowed`` / ``trust_score_windowed`` /
    ``confirmed_real_windowed`` / ``sampled_windowed``. Lifetime fields are
    unchanged for back-compat.

    Phase 3 (measurement hygiene): a Tier-2 judgment flagged ``errored: true`` (the
    judge could not produce a verdict — transient API/rate-limit failure, see
    ``calibrate.js``) is EXCLUDED, not counted as a phantom. An errored judgment is
    treated as "not judged", so the deterministic Tier-1 merged verdict stands for
    that task. This stops a flaky judge fan-out from depressing the autonomy number.

    The same exclusion applies at Tier-1: a result flagged ``unverified: true``
    (``gh`` never answered — see :func:`verify_task_merged`) is dropped from
    ``sampled`` AND from the ``verified_autonomy_rate`` denominator, so a flaky
    ``gh`` night shrinks the sample instead of manufacturing phantoms. The count
    is preserved as ``tier1_unverified`` and, when any exist, ``degraded`` is set
    so consumers can label the number as measured on a reduced sample.
    """
    unverified = [v for v in tier1 if v.get("unverified")]
    tier1 = [v for v in tier1 if not v.get("unverified")]
    sampled = len(tier1)
    merged = [v for v in tier1 if v.get("merged")]
    # Errored judgments are excluded entirely — they are neither evidence the task
    # was addressed nor evidence it was a phantom.
    effective_tier2 = [j for j in tier2 if not j.get("errored")]
    errored_count = len(tier2) - len(effective_tier2)
    addresses = {j["task_id"] for j in effective_tier2 if j.get("addresses_task")}
    judged_ids = {j["task_id"] for j in effective_tier2 if j.get("task_id")}

    def _is_real(v: dict[str, Any]) -> bool:
        tid = v.get("task_id")
        return tid not in judged_ids or tid in addresses

    confirmed_real = sum(1 for v in merged if _is_real(v))

    # Unverified tasks leave the denominator too. Keeping them in would let a
    # broken `gh` depress the rate exactly as counting them as phantoms did —
    # the sample must shrink at both ends, not just the numerator.
    unverified_count = len(unverified)
    queued_denom = max(0, distinct_tasks_queued - unverified_count)

    result = {
        "sampled": sampled,
        "tier1_merged": len(merged),
        "tier1_phantom": sampled - len(merged),
        "tier1_unverified": unverified_count,
        "tier2_judged": len(judged_ids),
        "tier2_addresses_task": len(addresses),
        "tier2_errored": errored_count,
        "confirmed_real": confirmed_real,
        "trust_score": _rate(confirmed_real, sampled),
        "phantom_rate": _rate(sampled - confirmed_real, sampled),
        "verified_autonomy_rate": _rate(confirmed_real, queued_denom),
        "degraded": unverified_count > 0,
    }

    if window_days and distinct_tasks_queued_windowed is not None:
        cutoff = _window_cutoff(window_days, now)
        sampled_w = sum(1 for v in tier1 if _in_window(v.get("task_id"), cutoff))
        confirmed_real_w = sum(
            1 for v in merged if _is_real(v) and _in_window(v.get("task_id"), cutoff)
        )
        unverified_w = sum(
            1 for v in unverified if _in_window(v.get("task_id"), cutoff)
        )
        result.update(
            {
                "window_days": window_days,
                "cohort_start": cutoff,
                "sampled_windowed": sampled_w,
                "confirmed_real_windowed": confirmed_real_w,
                "tier1_unverified_windowed": unverified_w,
                "trust_score_windowed": _rate(confirmed_real_w, sampled_w),
                "verified_autonomy_rate_windowed": _rate(
                    confirmed_real_w,
                    max(0, distinct_tasks_queued_windowed - unverified_w),
                ),
            }
        )

    return result


def build_snapshot(
    proxy: dict[str, Any],
    ground_truth: dict[str, Any],
    phantoms: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
    build_sha: str | None = None,
    unverified: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one autonomy_audit snapshot entry.

    ``unverified`` lists tasks whose ``gh`` check never returned a verdict. They
    are recorded separately from ``phantoms`` so a flaky night is legible after
    the fact instead of looking like a wave of phantom completions.
    """
    return {
        "version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "build_sha": build_sha,
        "local_proxy": proxy,
        "ground_truth": ground_truth,
        "phantoms": phantoms,
        "unverified": unverified or [],
    }


# ---------------------------------------------------------------------------
# 4. Persist / read the audit (ring buffer, atomic write)
# ---------------------------------------------------------------------------


def append_autonomy_audit(
    company_dir: Path, snapshot: dict[str, Any], *, max_entries: int = 200
) -> Path:
    """Append a snapshot to autonomy_audit.json (ring buffer, atomic replace)."""
    path = company_dir / AUTONOMY_AUDIT_FILE
    data = _load(path, default={"version": 1, "entries": []})
    if not isinstance(data, dict) or "entries" not in data:
        data = {"version": 1, "entries": []}
    entries = data.setdefault("entries", [])
    entries.append(snapshot)
    if len(entries) > max_entries:
        data["entries"] = entries[-max_entries:]

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".aa_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def load_autonomy_audit(company_dir: Path) -> dict[str, Any] | None:
    """Return the latest audit snapshot, or None if no calibration has run."""
    data = _load(company_dir / AUTONOMY_AUDIT_FILE, default=None)
    if not isinstance(data, dict):
        return None
    entries = data.get("entries") or []
    return entries[-1] if entries else None


def autonomy_trend(
    company_dir: Path, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return the windowed-autonomy trend series from the audit ring buffer (Phase 3c).

    One compact row per persisted snapshot (oldest→newest) so ``/dashboard`` can draw
    a trend line proving (or disproving) that Phases 1–2 moved the number. The
    ``verified_windowed`` field is the trend-honest recent-cohort rate; it falls back
    to the lifetime rate for snapshots written before Phase 0 added the windowed math.
    """
    data = _load(company_dir / AUTONOMY_AUDIT_FILE, default=None)
    if not isinstance(data, dict):
        return []
    entries = data.get("entries") or []
    if limit is not None and limit > 0:
        entries = entries[-limit:]
    rows: list[dict[str, Any]] = []
    for snap in entries:
        gt = snap.get("ground_truth", {}) if isinstance(snap, dict) else {}
        rows.append(
            {
                "generated_at": snap.get("generated_at"),
                "build_sha": snap.get("build_sha"),
                "verified_windowed": gt.get(
                    "verified_autonomy_rate_windowed",
                    gt.get("verified_autonomy_rate"),
                ),
                "verified_lifetime": gt.get("verified_autonomy_rate"),
                "trust": gt.get("trust_score"),
                "phantom_rate": gt.get("phantom_rate"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 4b. Historical export — full phantom-rate + autonomy trend as CSV
# ---------------------------------------------------------------------------

_EXPORT_CSV_FIELDNAMES = [
    "week_start",
    "phantom_rate",
    "verified_autonomy_windowed",
    "verified_autonomy_lifetime",
    "trust_score",
    "tasks_queued",
    "tasks_completed_with_pr",
    "tasks_failed",
    "tasks_closed_unmerged",
    "reconcile_flips",
    "success_rate",
]


def _cohort_to_week(yyyymmdd: str) -> str | None:
    """Convert YYYYMMDD to the ISO Monday of that week (YYYY-MM-DD)."""
    try:
        dt = datetime(
            int(yyyymmdd[:4]),
            int(yyyymmdd[4:6]),
            int(yyyymmdd[6:8]),
            tzinfo=timezone.utc,
        )
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return None


def _iso_dt_to_week(iso_str: str) -> str | None:
    """Convert an ISO datetime string to the ISO Monday of that week (YYYY-MM-DD)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def export_history(
    company_dir: Path,
    *,
    output_path: Path,
) -> dict[str, Any]:
    """Export the full autonomy history to a CSV file (one row per ISO week).

    Reads:
    - ``state/autonomy_audit.json`` for phantom rate, autonomy rates, trust score
      (one entry per /calibrate run; the last snapshot per week is used).
    - ``state/work_queue.json`` for task counts grouped by cohort week embedded
      in each task_id.

    Rate columns (``phantom_rate``, ``verified_autonomy_*``, ``trust_score``,
    ``success_rate``) are empty strings for weeks with no calibration run /
    no resolved tasks — they are never fabricated.

    ``reconcile_flips`` counts closed-unmerged status flips keyed by the week
    the reconcile happened (``reconciled_at``), not the task-creation week.

    See ``docs/data/README.md`` for column definitions and incident annotations.
    """
    import csv  # stdlib — imported locally so ruff doesn't strip it before this fn exists

    # --- audit snapshots: index by ISO week, keep last per week ---
    audit_data = _load(company_dir / AUTONOMY_AUDIT_FILE, default=None)
    audit_entries: list[dict[str, Any]] = []
    if isinstance(audit_data, dict):
        audit_entries = audit_data.get("entries") or []

    audit_by_week: dict[str, dict[str, Any]] = {}
    for snap in audit_entries:
        if not isinstance(snap, dict):
            continue
        week = _iso_dt_to_week(snap.get("generated_at") or "")
        if week:
            audit_by_week[week] = snap  # last snapshot per week wins

    # --- work queue: deduplicate by task_id across all lanes ---
    wq = _load(_resolve_queue_path(company_dir), default={})
    seen: dict[str, dict[str, Any]] = {}
    for lane in _QUEUE_LANES:
        for t in wq.get(lane, []) or []:
            if isinstance(t, dict) and t.get("task_id"):
                seen[t["task_id"]] = t
    # completed lane holds closed_unmerged tasks too; iterate it explicitly
    # so closed_unmerged tasks (which are IN _QUEUE_LANES via "completed")
    # always have the most-recent version.
    for t in wq.get("completed", []) or []:
        if isinstance(t, dict) and t.get("task_id"):
            seen[t["task_id"]] = t

    # --- aggregate task counts per ISO week ---
    queued_w: dict[str, set[str]] = defaultdict(set)
    completed_pr_w: dict[str, set[str]] = defaultdict(set)
    failed_w: dict[str, int] = defaultdict(int)
    closed_unmerged_w: dict[str, int] = defaultdict(int)
    reconcile_flips_w: dict[str, int] = defaultdict(int)

    for tid, t in seen.items():
        if _is_stage_task(t):
            continue
        cohort = task_cohort_date(tid)
        if not cohort:
            continue
        week = _cohort_to_week(cohort)
        if not week:
            continue
        queued_w[week].add(tid)
        status = t.get("status")
        if status == "completed" and t.get("pr_url"):
            completed_pr_w[week].add(tid)
        elif status == "failed":
            failed_w[week] += 1
        elif status == RECONCILED_STATUS:
            closed_unmerged_w[week] += 1
            flip_week = _iso_dt_to_week(t.get("reconciled_at") or "")
            if flip_week:
                reconcile_flips_w[flip_week] += 1

    # --- union of all weeks, sorted ascending ---
    all_weeks = sorted(
        set(audit_by_week)
        | set(queued_w)
        | set(closed_unmerged_w)
        | set(reconcile_flips_w)
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_EXPORT_CSV_FIELDNAMES)
        writer.writeheader()
        for week in all_weeks:
            snap = audit_by_week.get(week)
            gt: dict[str, Any] = snap.get("ground_truth", {}) if snap else {}

            comp_pr = len(completed_pr_w.get(week, set()))
            fail_n = failed_w.get(week, 0)
            closed_n = closed_unmerged_w.get(week, 0)
            denom = comp_pr + fail_n + closed_n
            success_rate: float | str = round(comp_pr / denom, 3) if denom else ""

            writer.writerow(
                {
                    "week_start": week,
                    "phantom_rate": gt.get("phantom_rate", ""),
                    "verified_autonomy_windowed": gt.get(
                        "verified_autonomy_rate_windowed",
                        gt.get("verified_autonomy_rate", ""),
                    ),
                    "verified_autonomy_lifetime": gt.get("verified_autonomy_rate", ""),
                    "trust_score": gt.get("trust_score", ""),
                    "tasks_queued": len(queued_w.get(week, set())),
                    "tasks_completed_with_pr": comp_pr,
                    "tasks_failed": fail_n,
                    "tasks_closed_unmerged": closed_n,
                    "reconcile_flips": reconcile_flips_w.get(week, 0),
                    "success_rate": success_rate,
                }
            )

    return {"rows": len(all_weeks), "output": str(output_path)}


# ---------------------------------------------------------------------------
# 5. Per-goal proxy breakdown (no network, local queue only)
# ---------------------------------------------------------------------------


def _infer_goal_ids(task: dict[str, Any]) -> list[str]:
    """Extract goal IDs (G1, G3, …) from a task's title and description.

    Returns a sorted, deduplicated list of uppercase IDs, or ``["uncategorized"]``
    when none are found. Both title and description are scanned so that tasks
    created by the strategic planner (``[QUEUE-FILL] G1: …`` titles or
    ``**Goal:** G1 -- Quality`` descriptions) are correctly attributed.
    """
    # Scan full title but only the first 500 chars of description — longer bodies
    # tend to reference goal IDs in historical/contextual prose and produce false positives.
    desc = (task.get("description") or "")[:500]
    text = " ".join(filter(None, [task.get("title"), desc]))
    found = {m.upper() for m in _GOAL_RE.findall(text)}
    return sorted(found) if found else ["uncategorized"]


def per_goal_proxy(
    company_dir: Path,
    *,
    window_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-goal breakdown of the local autonomy proxy (no network required).

    Scans every task in the work queue and infers goal associations from title
    and description text (regex ``G\\d{1,2}``). Returns claimed/no_pr/queued
    counts and a local proxy rate for each inferred goal.

    ``claimed`` — tasks with ``status=="completed"`` **and** a ``pr_url``
        (the deliverable-gated proxy; same numerator as ``compute_autonomy``).
    ``no_pr``   — tasks with ``status=="completed"`` but **no** ``pr_url``
        (local phantom candidates: they completed without opening a PR).
    ``queued``  — distinct tasks ever seen in any queue lane (denominator).

    ``verified`` and ``phantom`` (true ground-truth) require running the
    ``tier1`` command against GitHub and are intentionally absent here so
    callers are not misled by a zero that conflates "unknown" with "none".

    Tasks that match multiple goal IDs (e.g. "G1 and G3" in description) are
    counted in each matched goal independently. Tasks with no goal signal
    are bucketed under ``"uncategorized"``.
    """
    wq = _load(_resolve_queue_path(company_dir), default={})
    cutoff = _window_cutoff(window_days, now) if window_days else None

    # Deduplicate by task_id; later lanes (e.g. completed) override earlier
    # (e.g. pending) so the final status is the most recent one.
    seen: dict[str, dict[str, Any]] = {}
    for lane in _QUEUE_LANES:
        for t in wq.get(lane, []) or []:
            if (
                isinstance(t, dict)
                and t.get("task_id")
                and not _is_stage_task(t)
                and _in_window(t.get("task_id"), cutoff)
            ):
                seen[t["task_id"]] = t

    by_goal: dict[str, dict[str, int]] = defaultdict(
        lambda: {"queued": 0, "claimed": 0, "no_pr": 0}
    )
    for task in seen.values():
        goal_ids = _infer_goal_ids(task)
        status = task.get("status")
        has_pr = bool(task.get("pr_url"))
        for gid in goal_ids:
            by_goal[gid]["queued"] += 1
            if status == "completed":
                if has_pr:
                    by_goal[gid]["claimed"] += 1
                else:
                    by_goal[gid]["no_pr"] += 1

    goals: dict[str, Any] = {}
    for gid in sorted(by_goal):
        counts = by_goal[gid]
        goals[gid] = {
            "queued": counts["queued"],
            "claimed": counts["claimed"],
            "no_pr": counts["no_pr"],
            "claimed_rate": _rate(counts["claimed"], counts["queued"]),
        }

    result: dict[str, Any] = {"goals": goals}
    if window_days:
        result["window_days"] = window_days
        result["cohort_start"] = cutoff
    return result


# ---------------------------------------------------------------------------
# CLI — used by /calibrate and for quick manual checks
# ---------------------------------------------------------------------------


def _git_head_sha() -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            return res.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _flag_int(argv: list[str], name: str) -> int | None:
    """Parse ``--name N`` from argv (returns None if absent/unparseable)."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
    return None


def _flag_str(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Parse and strip --company-dir before command detection so it does not
    # land in argv[0] and get mistaken for a subcommand.
    company_dir_str = _flag_str(argv, "--company-dir")
    company_dir = Path(company_dir_str) if company_dir_str else Path(".company")
    if company_dir_str and "--company-dir" in argv:
        i = argv.index("--company-dir")
        argv = argv[:i] + argv[i + 2 :]

    cmd = argv[0] if argv else "proxy"
    window = _flag_int(argv, "--window")

    if cmd == "proxy":
        print(json.dumps(compute_autonomy(company_dir, window_days=window), indent=2))
    elif cmd == "candidates":
        print(
            json.dumps(
                completed_tasks_with_pr(company_dir, window_days=window), indent=2
            )
        )
    elif cmd == "cohorts":
        # Local (no-network) funnel split historical/recent by --since YYYY-MM-DD.
        print(
            json.dumps(
                local_cohorts(company_dir, since=_flag_str(argv, "--since")), indent=2
            )
        )
    elif cmd == "verify" and len(argv) >= 2:
        print(json.dumps(verify_task_merged(argv[1]), indent=2))
    elif cmd == "tier1":
        # Deterministic ground-truth pass over all completed-with-PR tasks.
        tasks = completed_tasks_with_pr(company_dir, window_days=window)
        results, survivors = run_tier1(tasks)
        print(
            json.dumps(
                {
                    "results": results,
                    "survivors": survivors,
                    "build_sha": _git_head_sha(),
                },
                indent=2,
            )
        )
    elif cmd == "reconcile":
        # Flip stale completions whose PR ended CLOSED-unmerged (measurement
        # hygiene). --dry-run reports without writing. Mutates the work queue.
        summary = reconcile_queue_closed_unmerged(
            company_dir,
            dry_run="--dry-run" in argv,
            timeout=_flag_int(argv, "--timeout") or 30,
            window_days=window,
        )
        if "--dry-run" not in argv:
            # A CLI process exits right after this; let the sweep finish.
            try:
                import goal_crediting as _gc

                _gc.join_credit_sweep()
            except Exception:
                pass
        print(json.dumps(summary, indent=2))
    elif cmd == "audit":
        print(json.dumps(load_autonomy_audit(company_dir), indent=2))
    elif cmd == "trend":
        # Windowed-autonomy time series from the audit ring buffer (Phase 3c).
        print(
            json.dumps(
                autonomy_trend(company_dir, limit=_flag_int(argv, "--limit")), indent=2
            )
        )
    elif cmd == "goals":
        # Per-goal local proxy breakdown (no network). Use --company-dir to
        # target a product sandbox and --window to restrict to recent cohorts.
        print(json.dumps(per_goal_proxy(company_dir, window_days=window), indent=2))
    elif cmd == "export-history":
        # Export full phantom-rate timeline + autonomy trend as CSV.
        # --format csv (only csv supported currently)
        # --output PATH  override output file (default: docs/data/autonomy-history.csv)
        # --repo-root PATH  base for the default output path (default: cwd)
        out_str = _flag_str(argv, "--output")
        repo_root = _flag_str(argv, "--repo-root")
        if out_str:
            out_path = Path(out_str)
        else:
            base = Path(repo_root) if repo_root else Path(".")
            out_path = base / "docs" / "data" / "autonomy-history.csv"
        result = export_history(company_dir, output_path=out_path)
        print(json.dumps(result, indent=2))
    else:
        print(
            "usage: autonomy_metrics.py [--company-dir DIR] "
            "[proxy [--window N]|candidates [--window N]|cohorts [--since YYYY-MM-DD]|"
            "verify <pr_url>|tier1 [--window N]|"
            "reconcile [--dry-run] [--timeout N] [--window N]|"
            "audit|trend [--limit N]|goals [--window N]|"
            "export-history [--format csv] [--output PATH] [--repo-root PATH]]",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
