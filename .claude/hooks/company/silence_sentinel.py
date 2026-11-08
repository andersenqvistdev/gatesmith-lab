#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Silence sentinel — makes the daemon's silence observable (plan item 4, 2026-07-26).

The failure mode this closes: the scout was dead for two nights, the queue sat
empty for hours, and the WS-119 watchdog self-killed nine times in a week —
and none of it surfaced until a human went looking. Autonomy that fails
silently is indistinguishable from autonomy that is working; this module makes
the difference visible.

Ten conditions, each raised as an escalation record (surfaced by
/pending and /respond, and by the opt-in notifications.escalation channels in
gatesmith.json — enable osascript there to get an actual desktop page):

1. scout_silent      — newest scout/* PR is older than scout_silent_hours.
                       The message carries the backpressure caveat: a quiet
                       night can mean the intake is saturated, not broken
                       (scout rule 8) — the alert says "look", not "panic".
2. queue_empty       — pending lane continuously empty longer than
                       queue_empty_hours while the daemon is running.
3. daemon_restarts   — restarts_per_day_threshold+ daemon restarts within
                       24h, detected via daemon.pid started_at changes. The
                       WS-119 watchdog SIGKILLs the process, which by design
                       cannot record its own death — watching the pid
                       snapshot catches every restart cause generically.
4. brief_skips       — a goal held back by the autofill brief-quality gate
                       for longer than brief_skip_persist_hours (its assessor
                       needs the PR #301 evidence treatment).
5. git_update_blocked — git_update_blocked_min_failures+ consecutive
                       fetch/pull failures against origin/main persisting
                       longer than git_update_blocked_hours, read from
                       .company/state/git_update_failures.json (written by
                       forge_daemon.py's _check_for_git_updates()/
                       _perform_git_pull()).
6. challenger_stall   — an open PR has carried challenger:pending longer
                       than challenger_stall_hours with no active
                       challenger task working it: the review never
                       engaged, or its task ended with nothing re-queued
                       (CHALLENGER-ENGAGE-ORPHAN-PRS, 2026-10-12; webapp
                       #209, #167). Age falls back to the PR's own
                       createdAt when challenger_stage.py never wrote a
                       state record for it — exactly the orphaned case.
7. ledger_stalled     — the acceptance ledger (acceptance.enabled) has
                       timed out ledger_stalled_timeouts+ times in a row
                       (partial runs and killed ones, from
                       .company/state/acceptance/ledger_attempts.json), or
                       no complete ledger run has been recorded for longer
                       than ledger_stalled_intervals x acceptance.intervalHours
                       (LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES, 2026-10-13:
                       webapp's ledger timed out for 16 h, no goal credit,
                       nothing paged).

8. pr_needs_operator — an open, non-draft PR older than pr_needs_operator_hours
                       that nothing will merge without a human: auto-merge is
                       not armed, or it carries awaiting-operator-review —
                       or auto-merge is armed but the PR is still open after
                       pr_stuck_hours (merge conflict / not merging).
                       scout/* heads are skipped (own cadence, own check).
                       Dedup key includes the PR numbers, so a NEW held PR
                       pages again while the same set does not.
9. main_ci_red       — the newest completed ci.yml run on main concluded
                       failure (dedup key includes the run id).
10. circuit_open     — .company/state/circuit_breaker.json reports a state
                       other than "closed".

Alerts dedup per condition key per escalation_dedup_hours. Every check fails
open: a gh timeout or unreadable state file must never fabricate an alert
(2026-07-25: fifteen gh timeouts made the metrics lie; alerting must not
repeat that) — and must never break the daemon loop.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# A child of the daemon's own logger: lines logged here reach daemon.log
# through its handlers (the daemon prints only condition keys itself).
_log = logging.getLogger("forge_daemon.silence_sentinel")

# Bounded tail scan of autofill_brief_skips.jsonl (append-only, chronological).
_SKIP_LOG_MAX_LINES = 500

# Bounded restart-event history kept in sentinel state.
_MAX_RESTART_EVENTS = 50


@dataclass
class SentinelConfig:
    """All paths anchor on base_dir (the project root) so test isolation is a
    single base_dir=tmp_path override — never cwd-relative defaults."""

    base_dir: Path
    scout_silent_hours: float = 48.0
    queue_empty_hours: float = 4.0
    restarts_per_day_threshold: int = 3
    brief_skip_persist_hours: float = 24.0
    scout_check_min_interval_hours: float = 6.0
    escalation_dedup_hours: float = 24.0
    scout_enabled: bool = True
    git_update_blocked_hours: float = 1.0
    git_update_blocked_min_failures: int = 3
    challenger_stall_hours: float = 6.0
    ledger_stalled_timeouts: int = 2
    ledger_stalled_intervals: float = 2.0
    # How long past its budget an attempt still marked in flight counts as
    # killed (the daemon SIGKILLs 10 s after its SIGINT; this is generous).
    ledger_killed_grace_seconds: float = 900.0
    pr_needs_operator_hours: float = 1.0
    # An auto-merge-armed PR still open after this long is stuck (conflict or
    # blocked), however healthy "armed" looks.
    pr_stuck_hours: float = 24.0
    # Minimum gap between gh calls for pr_needs_operator / main_ci_red.
    operator_check_min_interval_hours: float = 0.25

    @property
    def company_dir(self) -> Path:
        return self.base_dir / ".company"

    @property
    def state_path(self) -> Path:
        return self.company_dir / "state" / "silence_sentinel.json"

    @property
    def queue_path(self) -> Path:
        return self.company_dir / "state" / "work_queue.json"

    @property
    def challenger_state_path(self) -> Path:
        """Same path challenger_stage.py's STATE_SUBDIR/STATE_FILE write —
        duplicated here (not imported) in this module's own house style."""
        return self.company_dir / "state" / "challenger" / "stage.json"

    @property
    def acceptance_state_dir(self) -> Path:
        """Same directory acceptance_ledger.py writes (LedgerConfig.state_dir)."""
        return self.company_dir / "state" / "acceptance"

    @property
    def forge_config_path(self) -> Path:
        return self.base_dir / "gatesmith.json"

    @property
    def pid_path(self) -> Path:
        return self.company_dir / "daemon.pid"

    @property
    def skips_path(self) -> Path:
        return self.company_dir / "state" / "autofill_brief_skips.jsonl"

    @property
    def git_update_failures_path(self) -> Path:
        return self.company_dir / "state" / "git_update_failures.json"

    @property
    def circuit_breaker_path(self) -> Path:
        return self.company_dir / "state" / "circuit_breaker.json"

    @property
    def escalations_dir(self) -> Path:
        return self.company_dir / "escalations"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _load_state(config: SentinelConfig) -> dict:
    try:
        data = json.loads(config.state_path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        pass
    return {}


def _save_state(config: SentinelConfig, state: dict) -> None:
    """Atomic write (mkstemp + replace) — a torn state file must never make
    the next cycle double-alert or lose the empty-since anchor."""
    path = config.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Checks — each returns a finding dict or None, and only mutates its own
# state keys. All fail open.
# ---------------------------------------------------------------------------


def _default_gh_runner(args: list[str]) -> str | None:
    """Run gh, return stdout or None on any failure (fail-open)."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def check_scout_silence(
    config: SentinelConfig,
    state: dict,
    now: datetime,
    run_gh: Callable[[list[str]], str | None],
) -> dict | None:
    """Newest scout/* PR older than the threshold -> finding.

    Rate-limited to one gh call per scout_check_min_interval_hours. A gh
    failure records nothing and alerts nothing — a flaky gh must never
    fabricate a dead scout (the 07-25 instrument-lie lesson).
    """
    if not config.scout_enabled:
        return None
    last_check = _parse_ts(state.get("last_scout_check"))
    if (
        last_check
        and (now - last_check).total_seconds()
        < config.scout_check_min_interval_hours * 3600
    ):
        newest = _parse_ts(state.get("last_scout_pr_at"))
    else:
        output = run_gh(
            [
                "pr",
                "list",
                "--search",
                "head:scout/",
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "createdAt",
            ]
        )
        if output is None:
            return None
        try:
            rows = json.loads(output or "[]")
        except json.JSONDecodeError:
            return None
        state["last_scout_check"] = now.isoformat()
        newest = _parse_ts(rows[0].get("createdAt")) if rows else None
        if newest:
            state["last_scout_pr_at"] = newest.isoformat()

    if newest is None:
        # No scout PR has ever been observed — that is its own kind of
        # silence, but only alert once a check has actually succeeded.
        if not state.get("last_scout_check"):
            return None
        age_hours = float("inf")
    else:
        age_hours = (now - newest).total_seconds() / 3600

    if age_hours <= config.scout_silent_hours:
        return None
    age_text = "never" if age_hours == float("inf") else f"{age_hours:.0f}h ago"
    return {
        "condition": "scout_silent",
        "title": f"Scout silent: newest scout/* PR is {age_text}",
        "details": {
            "last_scout_pr_at": state.get("last_scout_pr_at"),
            "threshold_hours": config.scout_silent_hours,
            "note": (
                "A quiet night can mean intake is saturated, not broken "
                "(scout backpressure rule 8). Check "
                "state/task_admission_rejections.jsonl and the scout "
                "LaunchAgent before assuming failure."
            ),
        },
    }


def check_queue_empty(
    config: SentinelConfig, state: dict, now: datetime
) -> dict | None:
    """Pending lane continuously empty longer than the threshold -> finding."""
    try:
        queue = json.loads(config.queue_path.read_text())
        pending = len(queue.get("pending", []))
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None

    if pending > 0:
        state.pop("queue_empty_since", None)
        return None

    since = _parse_ts(state.get("queue_empty_since"))
    if since is None:
        state["queue_empty_since"] = now.isoformat()
        return None

    empty_hours = (now - since).total_seconds() / 3600
    if empty_hours <= config.queue_empty_hours:
        return None
    return {
        "condition": "queue_empty",
        "title": f"Work queue empty for {empty_hours:.1f}h",
        "details": {
            "empty_since": state.get("queue_empty_since"),
            "threshold_hours": config.queue_empty_hours,
            "note": (
                "Autofill may be quality-skipping every goal (see "
                "state/autofill_brief_skips.jsonl) or all sources may be "
                "idle. An empty queue is cheaper than vague tasks, but "
                "hours of it deserves a look."
            ),
        },
    }


def check_daemon_restarts(
    config: SentinelConfig, state: dict, now: datetime
) -> dict | None:
    """Threshold+ restarts within 24h -> finding.

    Detects restarts by watching daemon.pid started_at change between
    sentinel cycles — catches WS-119 self-SIGKILLs, crashes, and launchd
    respawns alike, none of which can reliably record their own death.
    """
    try:
        pid_data = json.loads(config.pid_path.read_text())
        started_at = str(pid_data.get("started_at", ""))
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None
    if not started_at:
        return None

    events = [e for e in state.get("restart_events", []) if isinstance(e, str)]
    previous = state.get("last_daemon_started_at")
    if previous and previous != started_at:
        events.append(now.isoformat())
        events = events[-_MAX_RESTART_EVENTS:]
        state["restart_events"] = events
    state["last_daemon_started_at"] = started_at

    recent = [
        ts
        for e in events
        if (ts := _parse_ts(e)) and (now - ts).total_seconds() < 24 * 3600
    ]
    if len(recent) < config.restarts_per_day_threshold:
        return None
    return {
        "condition": "daemon_restarts",
        "title": f"Daemon restarted {len(recent)}x in 24h",
        "details": {
            "restarts_24h": len(recent),
            "threshold": config.restarts_per_day_threshold,
            "note": (
                "Repeated restarts usually mean the WS-119 heartbeat "
                "watchdog is self-killing a hung loop — check "
                ".company/logs for 'watchdog: heartbeat stale' lines."
            ),
        },
    }


def check_brief_skips(
    config: SentinelConfig, state: dict, now: datetime
) -> dict | None:
    """A goal quality-skipped for longer than the persistence window -> finding.

    One skip record is the gate doing its job; the same goal skipped across
    brief_skip_persist_hours means its assessor never emits evidence and
    needs the PR #301 treatment — that is a human/interactive task.
    """
    try:
        lines = config.skips_path.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return None

    per_goal: dict[str, list[datetime]] = {}
    for line in lines[-_SKIP_LOG_MAX_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(record.get("ts"))
        goal_id = record.get("goal_id")
        if ts and goal_id:
            per_goal.setdefault(str(goal_id), []).append(ts)

    persistent: list[str] = []
    recent_window = 24 * 3600
    for goal_id, stamps in sorted(per_goal.items()):
        newest = max(stamps)
        oldest = min(stamps)
        if (now - newest).total_seconds() > recent_window:
            continue  # not currently skipping
        if (newest - oldest).total_seconds() / 3600 >= config.brief_skip_persist_hours:
            persistent.append(goal_id)

    if not persistent:
        return None
    return {
        "condition": "brief_skips",
        "title": (
            f"Autofill quality gate held {', '.join(persistent)} for "
            f">{config.brief_skip_persist_hours:.0f}h"
        ),
        "details": {
            "goals": persistent,
            "note": (
                "These goals' assessors emit actions without verified "
                "pointers or measured evidence. Upgrade them with the "
                "PR #301 pattern (name the failing artifacts) or mark "
                "actions [INFRA-ONLY]/[OWNER-ONLY]."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


def check_git_update_blocked(
    config: SentinelConfig, state: dict, now: datetime
) -> dict | None:
    """The daemon has been failing to fetch/pull origin/main for hours -> finding.

    The daemon records every consecutive failure in
    state/git_update_failures.json (forge_daemon._record_git_update_failure)
    and deletes the file on the next successful pull. 314 consecutive
    failures — a stale .git/index.lock, 2026-08-20 → 08-28 — produced only
    WARNING lines while the checkout froze and every worktree shipped
    against a stale base.
    """
    try:
        data = json.loads(config.git_update_failures_path.read_text())
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        consecutive = int(data.get("consecutive", 0) or 0)
    except (TypeError, ValueError):
        return None
    first = _parse_ts(data.get("first_failed_at"))
    if first is None or consecutive < config.git_update_blocked_min_failures:
        return None
    blocked_hours = (now - first).total_seconds() / 3600
    if blocked_hours <= config.git_update_blocked_hours:
        return None
    stage = data.get("stage") or "pull"
    return {
        "condition": "git_update_blocked",
        "title": (
            f"Git updates failing for {blocked_hours:.1f}h "
            f"({consecutive} consecutive {stage} failures)"
        ),
        "details": {
            "first_failed_at": data.get("first_failed_at"),
            "last_failed_at": data.get("last_failed_at"),
            "consecutive": consecutive,
            "stage": stage,
            "last_error": data.get("last_error"),
            "note": (
                "The daemon is running stale code and, wherever a worktree "
                "falls back to local main, shipping against a stale base. "
                "Usual causes: a .git/index.lock the policy will not remove "
                "(non-empty and under an hour old), a permanent local "
                "override that origin/main also changed, or an unreachable "
                "remote. Fix the cause; the file clears on the next "
                "successful pull."
            ),
        },
    }


def _queue_has_active_challenger(queue: Any, pr_url: str) -> bool:
    """A challenger (or continuation) task is still pending/in_progress for
    this PR, or deliberately operator-held — mirrors challenger_stage.py's
    own ``active_stage_task`` dedupe (duplicated, not imported: this module
    never reaches into a sibling's internals, per its own house style)."""
    if not isinstance(queue, dict):
        return False
    for lane, tasks in queue.items():
        if lane == "cancelled" or not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict) or t.get("source") != "challenger":
                continue
            meta = t.get("challenger")
            if not isinstance(meta, dict) or meta.get("pr_url") != pr_url:
                continue
            if lane in ("pending", "in_progress"):
                return True
            if lane == "blocked" and (
                t.get("operator_hold") or t.get("challenger_parent_hold")
            ):
                return True
    return False


def _pr_number(url: str) -> str | None:
    tail = str(url).rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def check_challenger_stall(
    config: SentinelConfig,
    state: dict,
    now: datetime,
    run_gh: Callable[[list[str]], str | None],
) -> dict | None:
    """An open PR has carried ``challenger:pending`` longer than
    challenger_stall_hours with no active challenger task working it ->
    finding. The review either never engaged (a builder-task lane the stage
    didn't recognize — CHALLENGER-ENGAGE-ORPHAN-PRS, 2026-10-12; webapp #209
    sat from 2026-10-11 ~12:00, #167 three days) or its task ended with
    nothing re-queued. challenger_stage.py's own tick() is expected to fix
    the first cause going forward; this check is the trip-wire for whatever
    it still misses, so the stall is never silent again.

    Age is read from challenger_stage's own per-PR state (``created_at``)
    when there is one, else falls back to the PR's own ``createdAt`` — the
    exact case where the stage never engaged it at all leaves no state
    record, and that is precisely the stall this must still catch.
    """
    output = run_gh(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--label",
            "challenger:pending",
            "--json",
            "number,url,createdAt",
            "--limit",
            "100",
        ]
    )
    if output is None:
        return None
    try:
        rows = json.loads(output or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    try:
        queue = json.loads(config.queue_path.read_text())
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        queue = {}
    stage_prs: dict = {}
    try:
        stage_data = json.loads(config.challenger_state_path.read_text())
        if isinstance(stage_data, dict) and isinstance(stage_data.get("prs"), dict):
            stage_prs = stage_data["prs"]
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        stage_prs = {}

    stalled: list[tuple[str, float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not url:
            continue
        rec = stage_prs.get(url)
        rec = rec if isinstance(rec, dict) else {}
        since = _parse_ts(rec.get("created_at")) or _parse_ts(row.get("createdAt"))
        if since is None:
            continue
        pending_hours = (now - since).total_seconds() / 3600
        if pending_hours <= config.challenger_stall_hours:
            continue
        if _queue_has_active_challenger(queue, url):
            continue
        if rec:
            why = (
                f"stage phase {rec.get('phase') or 'unknown'}, no challenger "
                "task queued"
            )
        else:
            why = "never engaged by the stage"
        stalled.append((str(url), pending_hours, why))

    if not stalled:
        return None
    stalled.sort(key=lambda item: -item[1])
    named = ", ".join(
        f"#{_pr_number(u) or u} ({h:.0f}h, {why})" for u, h, why in stalled
    )
    # Name the PRs where the operator looks first: the daemon log.
    _log.warning(f"[Sentinel] challenger_stall: {named}")
    return {
        "condition": "challenger_stall",
        "title": f"{len(stalled)} PR(s) stuck challenger:pending: {named}",
        "details": {
            "stalled_prs": [u for u, _h, _w in stalled],
            "prs": [
                {
                    "url": u,
                    "number": _pr_number(u),
                    "pending_hours": round(h, 1),
                    "reason": why,
                }
                for u, h, why in stalled
            ],
            "threshold_hours": config.challenger_stall_hours,
            "note": (
                "challenger:pending with no active challenger task: the "
                "stage never engaged it, or its task ended without a "
                "verdict and nothing was re-queued. The PR cannot merge "
                "until relabeled — check challenger_stage.py's tick() "
                "output in the daemon log for this PR."
            ),
        },
    }


# Bounded tail scan of the ledger's runs.jsonl for the last complete run.
_LEDGER_RUNS_MAX_LINES = 500


def _acceptance_schedule(config: SentinelConfig) -> tuple[bool, float]:
    """(acceptance.enabled is literally true, acceptance.intervalHours or 24)
    -- read as the daemon reads them; anything unreadable is "off"."""
    try:
        block = json.loads(config.forge_config_path.read_text()).get("acceptance")
    except (OSError, ValueError, AttributeError):
        return False, 24.0
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return False, 24.0
    interval = block.get("intervalHours")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return True, 24.0
    return True, float(interval) if interval > 0 else 24.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _last_complete_ledger_run(config: SentinelConfig) -> float | None:
    """Epoch of the newest runs.jsonl record that judged every entry (not
    partial) and judged the product at all (no environment_error)."""
    try:
        lines = (
            (config.acceptance_state_dir / "runs.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()[-_LEDGER_RUNS_MAX_LINES:]
        )
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("partial") is True or record.get("environment_error"):
            continue
        stamp = _number(record.get("timestamp"))
        if stamp is not None:
            return stamp
    return None


def check_ledger_stalled(
    config: SentinelConfig, state: dict, now: datetime
) -> dict | None:
    """The acceptance ledger keeps timing out, or has not completed a run in
    ledger_stalled_intervals x intervalHours -> finding.

    webapp, 2026-10-12/13: every scheduled ledger run was killed at its fixed
    step timeout for 16 h (rc=-2); no run recorded anything, so no goal was
    credited, G46 waited, the queue sat empty and nothing paged. The ledger
    now records partial runs and counts timed-out attempts
    (acceptance_ledger.begin_attempt/end_attempt -> ledger_attempts.json);
    an attempt still marked in flight past its budget + grace was killed
    without recording and counts too. With no complete run ever recorded the
    age is measured from the first cycle that saw the ledger enabled, so a
    fresh install is not paged at once. Only when acceptance.enabled is true.
    """
    enabled, interval_hours = _acceptance_schedule(config)
    if not enabled:
        state.pop("ledger_watch_since", None)
        return None
    try:
        attempts = json.loads(
            (config.acceptance_state_dir / "ledger_attempts.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        attempts = {}
    if not isinstance(attempts, dict):
        attempts = {}
    now_epoch = now.timestamp()
    timeouts = attempts.get("consecutive_timeouts")
    timeouts = (
        timeouts if isinstance(timeouts, int) and not isinstance(timeouts, bool) else 0
    )
    in_flight = attempts.get("in_flight")
    killed_now = False
    if isinstance(in_flight, dict):
        started = _number(in_flight.get("started_at"))
        budget = _number(in_flight.get("budget_seconds")) or 0.0
        if (
            started is not None
            and budget > 0
            and now_epoch - started > budget + config.ledger_killed_grace_seconds
        ):
            timeouts += 1
            killed_now = True

    complete = [
        stamp
        for stamp in (
            _number(attempts.get("last_complete_at")),
            _last_complete_ledger_run(config),
        )
        if stamp is not None
    ]
    since = state.setdefault("ledger_watch_since", now.isoformat())
    anchor = max(complete) if complete else None
    if anchor is None:
        watched = _parse_ts(since)
        anchor = watched.timestamp() if watched else now_epoch
    hours = (now_epoch - anchor) / 3600
    limit_hours = config.ledger_stalled_intervals * interval_hours

    reasons: list[str] = []
    if timeouts >= config.ledger_stalled_timeouts:
        reasons.append(f"{timeouts} consecutive ledger runs timed out")
    if hours > limit_hours:
        reasons.append(
            f"no complete ledger run for {hours:.0f}h "
            f"(limit {limit_hours:.0f}h = {config.ledger_stalled_intervals:g} x "
            "intervalHours)"
        )
    if not reasons:
        return None
    return {
        "condition": "ledger_stalled",
        "title": "Acceptance ledger stalled: " + "; ".join(reasons),
        "details": {
            "consecutive_timeouts": timeouts,
            "attempt_in_flight_killed": killed_now,
            "hours_since_complete_run": round(hours, 1),
            "threshold_timeouts": config.ledger_stalled_timeouts,
            "threshold_hours": limit_hours,
            "last_outcome": attempts.get("last_outcome"),
            "note": (
                "No complete ledger run means no goal credit for entries the "
                "partial runs never reached. Check the daemon log for "
                "'[Acceptance] ledger:' and last_run.json's unfinished_ids; "
                "raise acceptance.stepTimeoutSeconds, or move the ledger to "
                'CI shards (acceptance.runner: "ci", '
                "docs/acceptance-ledger.md § Sharded CI runs)."
            ),
        },
    }


def _gh_throttled(
    config: SentinelConfig,
    state: dict,
    now: datetime,
    key: str,
    run_gh: Callable[[list[str]], str | None],
    args: list[str],
) -> Any:
    """Run a gh JSON query at most once per operator_check_min_interval_hours.

    Returns the parsed JSON, or None when throttled or on any failure. The
    throttle stamp is only written after a successful, parseable call, so a
    flaky gh is retried next cycle rather than going quiet.
    """
    last = _parse_ts(state.get(key))
    if (
        last
        and (now - last).total_seconds()
        < config.operator_check_min_interval_hours * 3600
    ):
        return None
    output = run_gh(args)
    if output is None:
        return None
    try:
        data = json.loads(output or "[]")
    except json.JSONDecodeError:
        return None
    state[key] = now.isoformat()
    return data


def check_pr_needs_operator(
    config: SentinelConfig,
    state: dict,
    now: datetime,
    run_gh: Callable[[list[str]], str | None],
) -> dict | None:
    """Open non-draft PRs that nothing will merge without a human -> finding.

    A PR qualifies when it is older than pr_needs_operator_hours and either
    auto-merge is not armed (a merge gate silently refused: a secrets-scan
    false positive once left a daemon PR 3 h with no log line) or it carries
    awaiting-operator-review. A PR whose auto-merge IS armed also qualifies once
    it is older than pr_stuck_hours: "merge conflict" when GitHub reports it
    CONFLICTING, otherwise "auto-merge armed, not merging" (blocked checks or
    reviews; armed PRs sat open for two weeks unnoticed). scout/* heads are
    skipped.
    """
    rows = _gh_throttled(
        config,
        state,
        now,
        "last_pr_needs_operator_check",
        run_gh,
        [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,isDraft,autoMergeRequest,labels,createdAt,headRefName,"
            "mergeable,mergeStateStatus",
            "--limit",
            "100",
        ],
    )
    if not isinstance(rows, list):
        return None
    prs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("isDraft"):
            continue
        number = row.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        if str(row.get("headRefName") or "").startswith("scout/"):
            continue
        created = _parse_ts(row.get("createdAt"))
        if (
            created is None
            or (now - created).total_seconds() < config.pr_needs_operator_hours * 3600
        ):
            continue
        labels = {
            lab.get("name") for lab in row.get("labels") or [] if isinstance(lab, dict)
        }
        if "awaiting-operator-review" in labels:
            reason = "held for review"
        elif not row.get("autoMergeRequest"):
            reason = "auto-merge not armed"
        elif (now - created).total_seconds() < config.pr_stuck_hours * 3600:
            continue
        elif str(row.get("mergeable") or "").upper() == "CONFLICTING":
            reason = "merge conflict"
        else:
            reason = "auto-merge armed, not merging"
        prs.append(
            {
                "number": number,
                "title": str(row.get("title") or ""),
                "reason": reason,
                "age_hours": round((now - created).total_seconds() / 3600, 1),
            }
        )
    if not prs:
        return None
    prs.sort(key=lambda p: p["number"])
    noun = "PR needs" if len(prs) == 1 else "PRs need"
    listing = ", ".join(f"#{p['number']} ({p['reason']})" for p in prs)
    return {
        "condition": "pr_needs_operator",
        "dedup_key": "pr_needs_operator:" + ",".join(str(p["number"]) for p in prs),
        "title": f"{len(prs)} {noun} you: {listing}",
        "details": {
            "prs": prs,
            "threshold_hours": config.pr_needs_operator_hours,
            "note": (
                "Auto-merge not armed usually means a merge gate refused the "
                "PR; check its comments and checks. Held for review means a "
                "human decision is required."
            ),
        },
    }


def check_main_ci_red(
    config: SentinelConfig,
    state: dict,
    now: datetime,
    run_gh: Callable[[list[str]], str | None],
) -> dict | None:
    """Newest completed ci.yml run on main concluded failure -> finding."""
    rows = _gh_throttled(
        config,
        state,
        now,
        "last_main_ci_check",
        run_gh,
        [
            "run",
            "list",
            "--branch",
            "main",
            "--workflow",
            "ci.yml",
            "--limit",
            "5",
            "--json",
            "conclusion,status,createdAt,databaseId,displayTitle",
        ],
    )
    if not isinstance(rows, list):
        return None
    latest = next(
        (r for r in rows if isinstance(r, dict) and r.get("status") == "completed"),
        None,
    )
    if latest is None or latest.get("conclusion") != "failure":
        return None
    run_id = latest.get("databaseId")
    return {
        "condition": "main_ci_red",
        "dedup_key": f"main_ci_red:{run_id}",
        "title": (
            "CI on main is red: " + str(latest.get("displayTitle") or "latest run")
        ),
        "details": {
            "run_id": run_id,
            "created_at": latest.get("createdAt"),
            "note": (
                "The daemon's pre-start check refuses to launch while main "
                "CI is red; fix main first."
            ),
        },
    }


def check_circuit_open(config: SentinelConfig) -> dict | None:
    """circuit_breaker.json state other than closed -> finding."""
    try:
        data = json.loads(config.circuit_breaker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    circuit = data.get("state")
    if not isinstance(circuit, str) or circuit.lower() == "closed":
        return None
    return {
        "condition": "circuit_open",
        "title": f"Daemon circuit breaker is {circuit.lower()}: work is paused",
        "details": {
            "state": circuit,
            "failure_count": data.get("failure_count"),
            "last_failure_time": data.get("last_failure_time"),
            "note": "Check .company/logs/ for the failures that tripped it.",
        },
    }


def _raise_alert(
    config: SentinelConfig, state: dict, finding: dict, now: datetime
) -> bool:
    """Write an escalation record and fire opt-in notifications.

    Deduped per condition per escalation_dedup_hours. Returns True when an
    alert was actually raised. Notification failures never affect the record.
    """
    condition = finding["condition"]
    dedup_key = finding.get("dedup_key") or condition
    last_alerts = state.setdefault("last_alerts", {})
    previous = _parse_ts(last_alerts.get(dedup_key))
    if (
        previous
        and (now - previous).total_seconds() < config.escalation_dedup_hours * 3600
    ):
        return False

    record_id = f"silence-{condition}-{now.strftime('%Y%m%d%H%M')}"
    record = {
        "task_id": record_id,
        "current_tier": 2,
        "status": "pending",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "original_agent": "silence_sentinel",
        "current_agent": None,
        "trigger": "silence_sentinel",
        "trigger_details": finding.get("details", {}),
        "events": [
            {
                "timestamp": now.isoformat(),
                "tier": 2,
                "trigger": condition,
                "action_taken": finding["title"],
                "resolved": False,
            }
        ],
        "metadata": {"title": finding["title"], "condition": condition},
    }
    try:
        config.escalations_dir.mkdir(parents=True, exist_ok=True)
        (config.escalations_dir / f"{record_id}.json").write_text(
            json.dumps(record, indent=2)
        )
    except OSError:
        return False

    last_alerts[dedup_key] = now.isoformat()

    # Opt-in page (osascript / webhook) — best-effort, never fatal.
    try:
        try:
            from . import escalation as escalation_mod  # type: ignore[attr-defined]
        except ImportError:
            import escalation as escalation_mod  # type: ignore[no-redef]

        escalation_record = escalation_mod.EscalationRecord(
            task_id=record_id,
            current_tier=2,
            status="pending",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            trigger="silence_sentinel",
            trigger_details=finding.get("details", {}),
        )
        escalation_mod.fire_escalation_opt_in_notifications(
            escalation_record, finding["title"]
        )
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


def run_sentinel_cycle(
    config: SentinelConfig,
    *,
    now: datetime | None = None,
    run_gh: Callable[[list[str]], str | None] | None = None,
) -> dict:
    """Run all silence checks; raise deduped alerts; persist state.

    Returns {"findings": [...], "alerts_raised": [...]} — findings lists every
    currently-true condition, alerts_raised only those that were newly paged
    this cycle (the rest were within the dedup window).
    """
    now = now or datetime.now(timezone.utc)
    run_gh = run_gh or _default_gh_runner
    state = _load_state(config)

    findings: list[dict] = []
    for check in (
        lambda: check_scout_silence(config, state, now, run_gh),
        lambda: check_queue_empty(config, state, now),
        lambda: check_daemon_restarts(config, state, now),
        lambda: check_brief_skips(config, state, now),
        lambda: check_git_update_blocked(config, state, now),
        lambda: check_challenger_stall(config, state, now, run_gh),
        lambda: check_ledger_stalled(config, state, now),
        lambda: check_pr_needs_operator(config, state, now, run_gh),
        lambda: check_main_ci_red(config, state, now, run_gh),
        lambda: check_circuit_open(config),
    ):
        try:
            finding = check()
        except Exception:
            finding = None  # a broken check must never break the cycle
        if finding:
            findings.append(finding)

    alerts_raised = [
        f["condition"] for f in findings if _raise_alert(config, state, f, now)
    ]

    try:
        _save_state(config, state)
    except Exception:
        pass

    return {"findings": findings, "alerts_raised": alerts_raised}
