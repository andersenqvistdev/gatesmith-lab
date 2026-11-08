#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
External work registry — tracks work done OUTSIDE the daemon queue.

Claude Code session agents building PRs, manual fixes, anything an operator
starts by hand, are invisible to `gatesmith-queue watch` because they never touch
`work_queue.json`. This module is the registry those sessions (or the
operator, on their behalf) write to, plus best-effort auto-discovery of open
GitHub PRs that aren't the daemon's own, so both show up in the same place.

Storage: ``<company_dir>/state/external_work.json`` — the same directory as
``work_queue.json`` (``get_queue_path`` in queue_monitor.py). Falls back to
``<company_dir>/external_work.json`` when the state-dir copy doesn't exist
yet, mirroring that function's legacy fallback exactly.

Writes follow the ADR-008 pattern used by goal_tracker.py's verification
state: an exclusive flock on a sidecar ``.lock`` file around the whole
read-modify-write, and an atomic temp-file + os.replace so a lock-free reader
never observes a truncated or half-written file.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("running", "pr-open", "done", "abandoned")
ACTIVE_STATUSES = ("running", "pr-open")
TERMINAL_STATUSES = ("done", "abandoned")
DONE_HIDE_AFTER = timedelta(hours=24)

GH_PR_CACHE_TTL = 60.0
# Module-level so repeated calls within one `gatesmith-queue watch` process (the
# same interpreter, looping with time.sleep) share the cache; a fresh `gh`
# call per refresh would hammer the API for no benefit to the operator.
_GH_PR_CACHE: dict[str, tuple[float, list[dict]]] = {}


def get_external_work_path(company_dir: Path) -> Path:
    """Registry path for *company_dir*, matching get_queue_path's fallback."""
    p = Path(company_dir) / "state" / "external_work.json"
    return p if p.exists() else Path(company_dir) / "external_work.json"


def _empty_registry() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"items": [], "metadata": {"created_at": now, "last_modified": now}}


def load_external_work(company_dir: Path) -> dict:
    """Load the registry, tolerating a missing or corrupt file."""
    path = get_external_work_path(company_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty_registry()
    data["items"] = [it for it in data["items"] if isinstance(it, dict)]
    return data


def generate_external_id() -> str:
    """Same shape as work_allocator.generate_task_id, prefixed `ext-` so the
    two id spaces never collide and stay visually distinct in the dashboard.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"ext-{timestamp}-{short_uuid}"


def _save_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("metadata", {})["last_modified"] = datetime.now(
        timezone.utc
    ).isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ew_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _with_lock(company_dir: Path, fn):
    """Run ``fn(registry) -> (registry_to_save_or_None, result)`` under an
    exclusive flock on a sidecar lock file (ADR-008 pattern: see
    goal_tracker.py's verification-state writer for the precedent)."""
    path = get_external_work_path(company_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_external_work(company_dir)
            new_registry, result = fn(registry)
            if new_registry is not None:
                _save_atomic(path, new_registry)
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def add_item(
    company_dir: Path,
    title: str,
    *,
    owner: str = "claude-session",
    branch: str | None = None,
    pr: int | str | None = None,
    note: str | None = None,
    status: str | None = None,
) -> dict:
    """Register a new piece of external work. Returns the new item (with its
    generated ``id``)."""
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("title is required")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    pr_int = int(pr) if pr not in (None, "") else None

    def _do(registry):
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": generate_external_id(),
            "title": title,
            "owner": owner or "claude-session",
            "started_at": now,
            "updated_at": now,
            "status": status or ("pr-open" if pr_int else "running"),
            "pr": pr_int,
            "branch": branch or None,
            "note": note or None,
        }
        registry["items"].append(item)
        return registry, item

    return _with_lock(company_dir, _do)


def _match(items: list[dict], ident: str) -> tuple[dict | None, str | None]:
    """Exact id match wins; else a suffix match (>=4 chars) if unambiguous.
    Mirrors work_allocator.find_task_by_ident's exact-then-suffix rule."""
    ident = str(ident or "").strip()
    if not ident:
        return None, "an id is required"
    exact = [it for it in items if str(it.get("id")) == ident]
    if exact:
        return exact[0], None
    if len(ident) >= 4:
        suffix = [it for it in items if str(it.get("id", "")).endswith(ident)]
        if len(suffix) == 1:
            return suffix[0], None
        if len(suffix) > 1:
            ids = ", ".join(sorted(str(it.get("id")) for it in suffix))
            return None, f"{ident!r} is ambiguous: {ids}"
    return None, f"no external item matches {ident!r}"


def update_item(
    company_dir: Path,
    ident: str,
    *,
    status: str | None = None,
    pr: int | str | None = None,
    branch: str | None = None,
    note: str | None = None,
) -> dict:
    """Update fields on a registered item, matched by full or suffix id."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    pr_int = int(pr) if pr not in (None, "") else None

    def _do(registry):
        item, err = _match(registry["items"], ident)
        if item is None:
            return None, {"ok": False, "error": err}
        if status is not None:
            item["status"] = status
        if pr_int is not None:
            item["pr"] = pr_int
        if branch is not None:
            item["branch"] = branch
        if note is not None:
            item["note"] = note
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        return registry, {"ok": True, "item": item}

    return _with_lock(company_dir, _do)


def mark_done(company_dir: Path, ident: str, *, note: str | None = None) -> dict:
    """Shorthand for ``update_item(..., status="done")``."""
    return update_item(company_dir, ident, status="done", note=note)


def _is_stale_terminal(item: dict, now: datetime) -> bool:
    if item.get("status") not in TERMINAL_STATUSES:
        return False
    stamp = item.get("updated_at") or item.get("started_at")
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when) > DONE_HIDE_AFTER


def list_items(company_dir: Path, *, include_old: bool = False) -> list[dict]:
    """Registry items, newest-started first. Terminal items (done/abandoned)
    older than DONE_HIDE_AFTER are dropped unless *include_old* is set
    (``gatesmith-queue external list --all``)."""
    items = load_external_work(company_dir).get("items", [])
    now = datetime.now(timezone.utc)
    if not include_old:
        items = [it for it in items if not _is_stale_terminal(it, now)]
    return sorted(items, key=lambda it: str(it.get("started_at") or ""), reverse=True)


def active_items(company_dir: Path) -> list[dict]:
    """Items a daemon-style dashboard should show: running or pr-open only,
    regardless of age — done/abandoned never belong in the live view."""
    items = load_external_work(company_dir).get("items", [])
    return [it for it in items if it.get("status") in ACTIVE_STATUSES]


def discover_external_prs(
    company_dir: Path,
    *,
    runner=subprocess.run,
    now: float | None = None,
    cache: dict | None = None,
) -> list[dict]:
    """Open GitHub PRs for the project at *company_dir* that are NOT from a
    daemon branch (``headRefName`` starting with ``daemon/``), cached for
    GH_PR_CACHE_TTL seconds so a `watch` loop doesn't call `gh` every tick.

    Returns an empty list — never raises, never prints — when `gh` is
    missing, times out, or returns something unparseable: the registry lane
    must still render.
    """
    if cache is None:
        cache = _GH_PR_CACHE
    key = str(Path(company_dir).resolve())
    moment = now if now is not None else time.monotonic()
    cached = cache.get(key)
    if cached is not None and moment - cached[0] < GH_PR_CACHE_TTL:
        return cached[1]

    prs: list[dict] = []
    try:
        result = runner(
            ["gh", "pr", "list", "--json", "number,title,headRefName,createdAt,labels"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(company_dir).parent),
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, list):
                prs = [
                    p
                    for p in data
                    if isinstance(p, dict)
                    and not str(p.get("headRefName") or "").startswith("daemon/")
                ]
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        prs = []

    cache[key] = (moment, prs)
    return prs
