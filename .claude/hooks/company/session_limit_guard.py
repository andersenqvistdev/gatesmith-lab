"""Session-limit guard — detect Claude subscription exhaustion and pause activation.

During the 2026-07-06 incident the daemon burned 622 worker sessions into an
already-exhausted Claude subscription because nothing checked for the CLI
message:

    You have hit your session limit - resets HH:MM

Since 2026-09-14 Anthropic also enforces a separate, permanent WEEKLY quota
that resets on its own multi-day schedule, reported as:

    You've hit your weekly limit · resets Mon 12:00am

This module:
1. Detects both messages (and variants) in worker stdout/stderr.
2. Parses the reset time — bare "HH:MM" for the session limit, or
   "<weekday> <h>[:mm](am|pm)" for the weekly limit — into an absolute
   UTC datetime.
3. Persists a pause record to .company/state/session_limit.json so
   ALL activation pathways (queue workers, employee initiative, teams,
   judges) can skip activation while the quota is exhausted.
4. Adds a small buffer beyond the stated reset time before clearing.
5. Provides heartbeat-safe query functions so the daemon stays alive.

State file schema:
    {
        "paused_until": "<ISO 8601 UTC>",   # absolute resume timestamp
        "detected_at":  "<ISO 8601 UTC>",   # when the limit was first hit
        "raw_message":  "<first matching line>",
        "reset_hhmm":   "HH:MM" | null,     # parsed session-limit reset, or null
        "reset_label":  "<weekday h:mmam>" | null,  # parsed weekly-limit reset, or null
        "limit_kind":   "session" | "weekly"
    }

File is removed (not zeroed) once the pause window expires, so a missing
file always means "not paused".
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------

SESSION_LIMIT_FILE = "session_limit.json"
STATE_SUBDIR = "state"

# Buffer added beyond the stated reset time (minutes).  The CLI resets on
# a server-side clock that may differ slightly from ours.
RESET_BUFFER_MINUTES = 5

# Fallback pause duration when no reset time can be parsed for a
# SESSION-limit message (minutes). Session-limit windows reset same-day.
FALLBACK_PAUSE_MINUTES = 30

# Fallback pause duration when no reset time can be parsed for a
# WEEKLY-limit message (minutes) = 7 days. Weekly windows can take up to
# a week to reset, so a same-day fallback (FALLBACK_PAUSE_MINUTES) would
# just cause repeated retry-and-repause churn against an exhausted quota.
WEEKLY_FALLBACK_MINUTES = 7 * 24 * 60

# Session-limit patterns — match the official Claude CLI session-limit
# message. "You have hit your session limit" with optional reset time.
_SESSION_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"you have hit your session limit",
        re.IGNORECASE,
    ),
    re.compile(
        r"you['‘’]ve hit your (claude\s+)?session limit",
        re.IGNORECASE,
    ),
    re.compile(
        r"session limit (reached|exceeded|exhausted)",
        re.IGNORECASE,
    ),
    re.compile(
        r"usage limit (reached|exceeded|hit)",
        re.IGNORECASE,
    ),
    re.compile(
        r"subscription (limit|quota) (reached|exceeded|hit|exhausted)",
        re.IGNORECASE,
    ),
]

# Weekly-limit patterns — match the CLI's separate weekly-quota message
# introduced 2026-09-14, e.g. "You've hit your weekly limit · resets Mon
# 12:00am". Distinct wording from the session-limit patterns above.
_WEEKLY_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"you have hit your weekly limit",
        re.IGNORECASE,
    ),
    re.compile(
        r"you['‘’]ve hit your (claude\s+)?weekly limit",
        re.IGNORECASE,
    ),
    re.compile(
        r"weekly limit (reached|exceeded|hit)",
        re.IGNORECASE,
    ),
]

_LIMIT_PATTERNS: list[re.Pattern[str]] = (
    _SESSION_LIMIT_PATTERNS + _WEEKLY_LIMIT_PATTERNS
)

# Matches "resets HH:MM" or "resets at HH:MM" anywhere in a line
# (session-limit reset format — 24h clock, no day name).
_RESET_TIME_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)

# Matches "resets <weekday> <h>[:mm](am|pm)" (weekly-limit reset format,
# e.g. "resets Mon 12:00am"). Accepts abbreviated or full weekday names
# and an optional space before am/pm to match the CLI's "12:00am" form.
_WEEKLY_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?"
    r"(?P<label>(?P<weekday>mon|tue|wed|thu|fri|sat|sun)[a-z]*"
    r"\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm))",
    re.IGNORECASE,
)

_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


# -------------------------------------------------------------------------
# Detection
# -------------------------------------------------------------------------


def detect_session_limit(text: str) -> tuple[bool, str | None]:
    """Scan text for session-limit messages.

    Args:
        text: Combined stdout + stderr from a worker run.

    Returns:
        (found, raw_line) where raw_line is the first matching line,
        or (False, None) if no limit message is present.
    """
    if not text:
        return False, None

    for line in text.splitlines():
        for pattern in _LIMIT_PATTERNS:
            if pattern.search(line):
                return True, line.strip()

    return False, None


def parse_reset_time(raw_line: str, now: datetime | None = None) -> datetime | None:
    """Parse a reset time from a limit message line.

    Tries the session-limit "HH:MM" form first (next occurrence today or
    tomorrow); falls through to the weekly-limit "<weekday> <h>[:mm](am|pm)"
    form (next occurrence within the coming week). Returns None when
    neither is parseable.

    Args:
        raw_line: The matching line from detect_session_limit.
        now: Override current UTC time (for testing).
    """
    if not raw_line:
        return None

    utc_now = now if now is not None else datetime.now(timezone.utc)

    m = _RESET_TIME_RE.search(raw_line)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour <= 23 and minute <= 59:
            candidate = utc_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )

            # If the reset time is in the past (or very close to now), push to tomorrow.
            if candidate <= utc_now + timedelta(minutes=1):
                candidate += timedelta(days=1)

            return candidate
        # Invalid HH:MM digits — fall through and try weekly parsing rather
        # than assuming no reset time exists at all in the line.

    return _parse_weekly_reset_time(raw_line, utc_now)


def _parse_weekly_reset_time(raw_line: str, utc_now: datetime) -> datetime | None:
    """Parse a "<weekday> <h>[:mm](am|pm)" weekly-limit reset time.

    The result is the next occurrence of that weekday+time in UTC, pushed a
    full 7 days ahead (not 1 day) if today already matches the weekday but
    the time has already passed — this is a weekly recurrence, not daily.
    Returns None when no weekday+time is parseable.
    """
    m = _WEEKLY_RESET_RE.search(raw_line)
    if not m:
        return None

    target_weekday = _WEEKDAY_INDEX[m.group("weekday").lower()[:3]]
    hour = int(m.group("hour"))
    minute = int(m.group("minute")) if m.group("minute") else 0
    if hour < 1 or hour > 12 or minute > 59:
        return None

    # 12-hour clock: 12am is midnight (hour 0), 12pm is noon (hour 12).
    if hour == 12:
        hour = 0
    if m.group("meridiem").lower() == "pm":
        hour += 12

    days_ahead = (target_weekday - utc_now.weekday()) % 7
    candidate = (utc_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )

    # If the computed slot is in the past (or very close to now) — including
    # "today" resolving to days_ahead == 0 with an already-passed time —
    # push a full week ahead, not one day.
    if candidate <= utc_now + timedelta(minutes=1):
        candidate += timedelta(days=7)

    return candidate


def _classify_limit_kind(raw_line: str | None) -> str:
    """Return "weekly" or "session" based on which pattern family matched.

    Defaults to "session" when raw_line is missing or matches neither
    weekly-specific pattern (e.g. the generic "usage limit ..." pattern
    catching an ambiguously-worded message) — session is the narrower,
    same-day-fallback assumption and was this guard's original scope.
    """
    if not raw_line:
        return "session"
    for pattern in _WEEKLY_LIMIT_PATTERNS:
        if pattern.search(raw_line):
            return "weekly"
    return "session"


def compute_resume_at(
    raw_line: str | None,
    now: datetime | None = None,
) -> datetime:
    """Compute the absolute UTC datetime after which activation may resume.

    Uses parse_reset_time when possible. Falls back to WEEKLY_FALLBACK_MINUTES
    for an unparseable weekly-limit message, or FALLBACK_PAUSE_MINUTES for an
    unparseable session-limit message (or no raw_line at all) — a wrong
    same-day guess for a multi-day weekly outage would just cause repeated
    retry-and-repause churn against an exhausted quota.

    Args:
        raw_line: The matching line (may be None).
        now: Override current UTC time (for testing).

    Returns:
        UTC datetime after which the pause expires.
    """
    utc_now = now if now is not None else datetime.now(timezone.utc)

    if raw_line:
        reset_dt = parse_reset_time(raw_line, now=utc_now)
        if reset_dt:
            return reset_dt + timedelta(minutes=RESET_BUFFER_MINUTES)
        if _classify_limit_kind(raw_line) == "weekly":
            return utc_now + timedelta(minutes=WEEKLY_FALLBACK_MINUTES)

    return utc_now + timedelta(minutes=FALLBACK_PAUSE_MINUTES)


# -------------------------------------------------------------------------
# State persistence
# -------------------------------------------------------------------------


def _state_path(company_dir: Path) -> Path:
    return company_dir / STATE_SUBDIR / SESSION_LIMIT_FILE


def set_session_limit_pause(
    raw_line: str | None,
    company_dir: Path,
    now: datetime | None = None,
) -> datetime:
    """Persist a session-limit pause record.

    Idempotent: if a valid pause record already exists, it is NOT overwritten
    (the earliest hit wins — we don't want a later detection to push the
    resume time forward if the earlier one was already correct).

    Args:
        raw_line: The matching line from detect_session_limit (may be None).
        company_dir: .company directory path.
        now: Override current UTC time (for testing).

    Returns:
        The paused_until datetime that is now in effect.
    """
    utc_now = now if now is not None else datetime.now(timezone.utc)
    state_file = _state_path(company_dir)

    # Check for an existing, still-valid record.
    existing = _read_state(state_file)
    if existing:
        paused_until_str = existing.get("paused_until", "")
        if paused_until_str:
            try:
                existing_until = datetime.fromisoformat(
                    paused_until_str.replace("Z", "+00:00")
                )
                existing_kind = existing.get("limit_kind", "session")
                new_kind = _classify_limit_kind(raw_line)
                if existing_until > utc_now:
                    # Same kind as the active pause: earliest hit wins — a
                    # later detection of the SAME limit shouldn't push the
                    # resume time around (the first report is trusted).
                    #
                    # Different kind (e.g. a weekly-limit hit arriving while
                    # a shorter session-limit pause is active, or vice
                    # versa): the two quotas are independent, so silently
                    # keeping the narrower pause could resume activation
                    # straight into a still-exhausted quota. Upgrade to
                    # whichever protects longer instead of dropping the new
                    # detection outright.
                    if existing_kind == new_kind:
                        logger.debug(
                            "session_limit_guard: existing pause still valid "
                            f"until {existing_until.isoformat()} — not overwriting"
                        )
                        return existing_until

                    candidate_resume_at = compute_resume_at(raw_line, now=utc_now)
                    if existing_until >= candidate_resume_at:
                        logger.debug(
                            "session_limit_guard: existing "
                            f"{existing_kind} pause until {existing_until.isoformat()} "
                            f"already covers the new {new_kind} detection — not overwriting"
                        )
                        return existing_until
                    logger.warning(
                        f"session_limit_guard: {new_kind} limit detected during an "
                        f"active {existing_kind} pause — upgrading pause from "
                        f"{existing_until.isoformat()} to {candidate_resume_at.isoformat()}"
                    )
            except ValueError:
                pass  # Malformed timestamp — overwrite

    resume_at = compute_resume_at(raw_line, now=utc_now)
    limit_kind = _classify_limit_kind(raw_line)

    reset_hhmm: str | None = None
    reset_label: str | None = None
    if raw_line:
        m = _RESET_TIME_RE.search(raw_line)
        if m:
            reset_hhmm = f"{m.group(1).zfill(2)}:{m.group(2)}"
        else:
            wm = _WEEKLY_RESET_RE.search(raw_line)
            if wm:
                reset_label = wm.group("label")[:80]

    record = {
        "paused_until": resume_at.isoformat(),
        "detected_at": utc_now.isoformat(),
        "raw_message": (raw_line or "")[:300],
        "reset_hhmm": reset_hhmm,
        "reset_label": reset_label,
        "limit_kind": limit_kind,
    }

    _write_state_atomic(state_file, record)

    logger.warning(
        f"session_limit_guard: Claude {limit_kind} limit detected — "
        f"ALL activation paused until {resume_at.strftime('%Y-%m-%d %H:%M UTC')} "
        f"(+{RESET_BUFFER_MINUTES}min buffer). "
        f"Raw: {(raw_line or '')[:120]!r}"
    )
    return resume_at


def get_session_limit_state(company_dir: Path) -> dict | None:
    """Return the active pause record, or None if not paused.

    Also removes the state file when the pause window has expired.

    Returns:
        The raw JSON dict if the pause is still active, otherwise None.
    """
    state_file = _state_path(company_dir)
    record = _read_state(state_file)
    if not record:
        return None

    paused_until_str = record.get("paused_until", "")
    if not paused_until_str:
        return None

    try:
        paused_until = datetime.fromisoformat(paused_until_str.replace("Z", "+00:00"))
    except ValueError:
        _remove_state(state_file)
        return None

    if datetime.now(timezone.utc) >= paused_until:
        logger.info("session_limit_guard: pause window expired — resuming activation")
        _remove_state(state_file)
        return None

    return record


def is_activation_paused(company_dir: Path) -> tuple[bool, str]:
    """High-level check for use in the daemon cycle.

    Returns:
        (paused: bool, reason: str) — reason is empty when not paused.
    """
    record = get_session_limit_state(company_dir)
    if record is None:
        return False, ""

    paused_until = record.get("paused_until", "unknown")
    reset_hhmm = record.get("reset_hhmm")
    reset_label = record.get("reset_label")
    limit_kind = record.get("limit_kind", "session")
    detected_at = record.get("detected_at", "")

    if reset_hhmm:
        reason = (
            f"Claude session limit active (resets {reset_hhmm} + "
            f"{RESET_BUFFER_MINUTES}min buffer); "
            f"activation paused until {paused_until}; "
            f"first detected {detected_at}"
        )
    elif reset_label:
        reason = (
            f"Claude weekly limit active (resets {reset_label} + "
            f"{RESET_BUFFER_MINUTES}min buffer); "
            f"activation paused until {paused_until}; "
            f"first detected {detected_at}"
        )
    elif limit_kind == "weekly":
        reason = (
            f"Claude weekly limit active (no reset time parsed — "
            f"{WEEKLY_FALLBACK_MINUTES // 60}h fallback); "
            f"activation paused until {paused_until}; "
            f"first detected {detected_at}"
        )
    else:
        reason = (
            f"Claude session limit active (no reset time parsed — "
            f"{FALLBACK_PAUSE_MINUTES}min fallback); "
            f"activation paused until {paused_until}; "
            f"first detected {detected_at}"
        )

    return True, reason


# -------------------------------------------------------------------------
# File helpers
# -------------------------------------------------------------------------


def _read_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _write_state_atomic(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_state(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
