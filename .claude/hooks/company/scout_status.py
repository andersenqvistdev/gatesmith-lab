#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Scout Status — show the opportunity scout pipeline status.

Usage:
    uv run .claude/hooks/company/scout_status.py
    uv run .claude/hooks/company/scout_status.py /path/to/.company
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_TASK_BLOCK_RE = re.compile(r"<task\s([^>]*)>(.*?)</task>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_CHILD_TEXT_RE = re.compile(r"<(\w+)>(.*?)</\1>", re.DOTALL)
_SCOUT_PREFIX = "SCOUT-"


def find_company_dir(start: Path | None = None) -> Path | None:
    """Walk upward from start searching for a .company dir with org.json."""
    starts: list[Path] = (
        [start] if start is not None else [Path(__file__).resolve().parent, Path.cwd()]
    )
    for s in starts:
        for parent in [s, *s.parents]:
            candidate = parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return None


def parse_scout_intake(intake_file: Path) -> list[dict]:
    """Parse SCOUT-* <task> elements from the intake Markdown file."""
    if not intake_file.exists():
        return []
    try:
        content = intake_file.read_text(encoding="utf-8")
    except OSError:
        return []
    tasks: list[dict] = []
    for m in _TASK_BLOCK_RE.finditer(content):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        task_id = attrs.get("id", "")
        if not task_id.startswith(_SCOUT_PREFIX):
            continue
        children = {k: v.strip() for k, v in _CHILD_TEXT_RE.findall(m.group(2))}
        tasks.append(
            {
                "id": task_id,
                "title": children.get("title", ""),
                "complexity": children.get("complexity", ""),
            }
        )
    return tasks


def load_roadmap_state(company_dir: Path) -> dict:
    """Load roadmap_state.json; return safe defaults when the file is absent."""
    defaults: dict = {
        "last_scan": None,
        "tasks_scheduled": [],
        "tasks_completed": [],
        "tasks_in_progress": [],
        "current_wave": 1,
        "active_phase": None,
        "roadmap_id_to_queue_id": {},
        "tasks_rejected": {},
    }
    path = company_dir / "roadmap_state.json"
    if not path.exists():
        return defaults
    try:
        with open(path, encoding="utf-8") as f:
            data: dict = json.load(f)
    except (OSError, json.JSONDecodeError):
        return defaults
    for key, val in defaults.items():
        if key not in data:
            data[key] = val
    return data


def _title_key(title: object) -> str:
    """Case- and whitespace-insensitive title identity (as task_admission)."""
    return " ".join(str(title or "").lower().split())


def load_rejections(company_dir: Path, intake: list[dict]) -> list[dict]:
    """Admission rejections of scout intake entries; newest first.

    A rejection row carries the QUEUE task id the scheduler minted
    (``task-2026…``), never the roadmap id — so the only link back to a
    SCOUT-* entry is the title. Each returned row gains ``scout_id``. (Until
    2026-09-04 this filtered on a ``SCOUT-`` task_id prefix that no row has
    ever carried, so the view showed zero rejections while one entry was
    being rejected every scan.)
    """
    path = company_dir / "state" / "task_admission_rejections.jsonl"
    if not path.exists():
        return []
    by_title = {_title_key(t.get("title")): t.get("id", "") for t in intake}
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                scout_id = by_title.get(_title_key(rec.get("title")))
                if scout_id:
                    records.append({**rec, "scout_id": scout_id})
    except OSError:
        return records
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return records


def _classify_task(
    tid: str,
    completed: set,
    in_progress: set,
    scheduled: set,
    id_to_queue: dict,
    rejected_ids: set,
    parked_ids: set | None = None,
) -> tuple[str, str | None]:
    """Return (status, queue_id) for a single scout task ID.

    ``parked`` means the scheduler has stopped re-minting the entry after
    repeated admission rejections; editing its intake text re-opens it.
    """
    queue_id: str | None = id_to_queue.get(tid)
    if tid in completed:
        return "completed", queue_id
    if tid in in_progress:
        return "in_progress", queue_id
    if tid in scheduled and queue_id:
        return f"queued ({queue_id})", queue_id
    if parked_ids and tid in parked_ids:
        return "parked", queue_id
    if tid in rejected_ids:
        return "rejected", queue_id
    return "pending", queue_id


def build_status_report(
    intake: list[dict],
    state: dict,
    rejections: list[dict],
) -> dict:
    """Combine intake, roadmap state, and rejections into one report dict."""
    completed = set(state.get("tasks_completed") or [])
    in_progress = set(state.get("tasks_in_progress") or [])
    scheduled = set(state.get("tasks_scheduled") or [])
    id_to_queue: dict = state.get("roadmap_id_to_queue_id") or {}
    refusals: dict = state.get("tasks_rejected") or {}
    if not isinstance(refusals, dict):
        refusals = {}
    rejected_ids = {r.get("scout_id") for r in rejections} | set(refusals)
    parked_ids = {
        tid
        for tid, rec in refusals.items()
        if isinstance(rec, dict) and rec.get("parked")
    }
    scout_tasks: list[dict] = []
    stats: dict = {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "in_progress": 0,
        "completed": 0,
        "rejected": 0,
        "parked": 0,
    }
    for task in intake:
        tid = task["id"]
        status, queue_id = _classify_task(
            tid,
            completed,
            in_progress,
            scheduled,
            id_to_queue,
            rejected_ids,
            parked_ids,
        )
        entry = {
            "id": tid,
            "title": task["title"],
            "complexity": task["complexity"],
            "status": status,
            "queue_id": queue_id,
        }
        refusal = refusals.get(tid)
        if isinstance(refusal, dict):
            entry["rejections"] = refusal.get("count")
            entry["last_rejection"] = refusal.get("reason")
        scout_tasks.append(entry)
        stats["total"] += 1
        stat_key = "queued" if status.startswith("queued") else status
        if stat_key in stats:
            stats[stat_key] += 1
    recent: list[dict] = [
        {
            "ts": r.get("ts", ""),
            "task_id": r.get("task_id", ""),
            "scout_id": r.get("scout_id", ""),
            "title": r.get("title", ""),
            "reason": r.get("reason", ""),
        }
        for r in rejections[:10]
    ]
    return {
        "scout_tasks": scout_tasks,
        "recent_rejections": recent,
        "stats": stats,
        "last_scan": state.get("last_scan"),
        "current_wave": state.get("current_wave", 1),
    }


def main() -> None:
    """Entry point: gather data and print a JSON status report to stdout."""
    args = sys.argv[1:]
    company_dir: Path | None = None
    for arg in args:
        if not arg.startswith("-"):
            p = Path(arg).resolve()
            # Only accept paths named ".company" to prevent path traversal
            if p.is_dir() and p.name == ".company":
                company_dir = p
            break

    if company_dir is None:
        company_dir = find_company_dir()

    intake_file: Path = (
        company_dir.parent / ".planning" / "ROADMAP-SCOUT.md"
        if company_dir is not None
        else Path.cwd() / ".planning" / "ROADMAP-SCOUT.md"
    )

    _default_state: dict = {
        "last_scan": None,
        "tasks_scheduled": [],
        "tasks_completed": [],
        "tasks_in_progress": [],
        "current_wave": 1,
        "active_phase": None,
        "roadmap_id_to_queue_id": {},
        "tasks_rejected": {},
    }
    intake = parse_scout_intake(intake_file)
    state = load_roadmap_state(company_dir) if company_dir else _default_state
    rejections = load_rejections(company_dir, intake) if company_dir else []
    report = build_status_report(intake, state, rejections)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
