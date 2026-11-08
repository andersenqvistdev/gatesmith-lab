#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Data source for the SENTINEL ALERTS section of /pending.

silence_sentinel.py raises its alerts as escalation records with
``current_tier`` 2 and ``trigger`` "silence_sentinel". The /pending command's
Tier 4 query (input_channel.py pending) never returns them, so a silent daemon
was announced in .company/escalations/ and shown nowhere. This helper selects
them regardless of tier.

Selection rules:
  * sentinel alerts  — every unresolved record whose ``trigger`` is
    "silence_sentinel", at any tier, newest first.
  * escalations      — unresolved Tier 4 records raised by anything else.
    (Same meaning as ``--type escalations``; a Tier 4 sentinel record is
    listed once, under sentinel alerts.)

Usage:
    python pending_alerts.py [--json]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SENTINEL_TRIGGER = "silence_sentinel"
HUMAN_TIER = 4
CLOSED_STATUSES = frozenset({"resolved", "closed", "cancelled"})


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def format_age(seconds: float) -> str:
    """Relative age such as '45m', '2h', '3d'."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _title(record: dict) -> str:
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and metadata.get("title"):
        return str(metadata["title"])
    events = record.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if isinstance(event, dict) and event.get("action_taken"):
                return str(event["action_taken"])
    return str(record.get("task_id", "(untitled)"))


def _summarize(record: dict, now: datetime) -> dict:
    created = _parse_ts(record.get("created_at")) or _parse_ts(record.get("updated_at"))
    age_seconds = (now - created).total_seconds() if created else None
    metadata = record.get("metadata")
    condition = metadata.get("condition") if isinstance(metadata, dict) else None
    return {
        "task_id": record.get("task_id"),
        "title": _title(record),
        "condition": condition,
        "tier": record.get("current_tier"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "age_seconds": age_seconds,
        "age": format_age(age_seconds) if age_seconds is not None else "unknown",
    }


def _load_records(escalations_dir: Path) -> list[dict]:
    records: list[dict] = []
    if not escalations_dir.is_dir():
        return records
    for path in sorted(escalations_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def collect_pending_alerts(
    escalations_dir: Path, *, now: datetime | None = None
) -> dict:
    """Split unresolved escalation records into the two /pending sections."""
    now = now or datetime.now(timezone.utc)
    sentinel: list[dict] = []
    escalations: list[dict] = []
    for record in _load_records(escalations_dir):
        if record.get("status") in CLOSED_STATUSES:
            continue
        if record.get("trigger") == SENTINEL_TRIGGER:
            sentinel.append(record)
        elif record.get("current_tier") == HUMAN_TIER:
            escalations.append(record)

    def newest_first(record: dict) -> float:
        ts = _parse_ts(record.get("created_at")) or _parse_ts(record.get("updated_at"))
        return ts.timestamp() if ts else 0.0

    sentinel.sort(key=newest_first, reverse=True)
    escalations.sort(key=newest_first, reverse=True)
    return {
        "success": True,
        "sentinel_alerts": [_summarize(r, now) for r in sentinel],
        "sentinel_count": len(sentinel),
        "escalations": [_summarize(r, now) for r in escalations],
        "escalation_count": len(escalations),
        "queried_at": now.isoformat(),
    }


def _default_escalations_dir() -> Path:
    try:
        import escalation  # type: ignore[import-not-found]
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import escalation  # type: ignore[import-not-found]
    return escalation.get_escalations_dir()


def main(argv: list[str] | None = None) -> int:
    try:
        result = collect_pending_alerts(_default_escalations_dir())
    except Exception as exc:  # noqa: BLE001 — the command shows the error text
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
