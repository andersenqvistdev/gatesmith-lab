#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Merge Audit Viewer — print the audit chain for a specific PR or task.

Usage:
    uv run .claude/hooks/company/merge_audit_viewer.py <PR#>
    uv run .claude/hooks/company/merge_audit_viewer.py <task-id>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_company_dir(start: Path | None = None) -> Path:
    # start lets callers (and tests) anchor the search explicitly; the default
    # keeps the __file__-then-cwd walk for CLI use from anywhere in the repo.
    starts = (
        [start] if start is not None else [Path(__file__).resolve().parent, Path.cwd()]
    )
    for s in starts:
        for parent in [s] + list(s.parents):
            candidate = parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return Path(".company")


def _load_merge_audit(company_dir: Path) -> list[dict]:
    path = company_dir / "state" / "merge_audit.jsonl"
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    results.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return results


def relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 0:
            return "future"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return "?"


_VERDICT_COLOR = {
    "PASS": "\033[32m",
    "BLOCK": "\033[31m",
    "SKIP": "\033[33m",
    "ERROR": "\033[35m",
}
_CI_COLOR = {
    "pass": "\033[32m",
    "fail": "\033[31m",
    "pending": "\033[33m",
    "unknown": "\033[2m",
}
_LEVER_COLOR = {
    "full_trust": "\033[36m",
    "full_trust_admin": "\033[35m",
    "github_auto": "\033[34m",
}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"


def _color(value: str | None, table: dict) -> str:
    if value is None:
        return f"{DIM}NULL{RESET}"
    return f"{table.get(value, '')}{value}{RESET}"


def render_audit(entries: list[dict], query: str) -> str:
    w = 78
    lines: list[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append(BOLD + "=" * w + RESET)
    lines.append(f"{BOLD}  MERGE AUDIT CHAIN: {query}{RESET}")
    lines.append(f"  {now_str}")
    lines.append(BOLD + "=" * w + RESET)
    lines.append("")

    if not entries:
        lines.append(f"  {DIM}No audit records found for {query!r}.{RESET}")
        lines.append("")
        lines.append(DIM + "─" * w + RESET)
        return "\n".join(lines)

    lines.append(f"  {len(entries)} record(s) found")
    lines.append("")

    for i, rec in enumerate(entries, 1):
        ts = rec.get("ts", "")
        ts_short = ts[:19] if ts else "?"
        when = relative_time(ts)

        task_id = rec.get("task_id")
        pr = rec.get("pr")
        dv = rec.get("deliverable_verdict")
        ci = rec.get("ci_state")
        lever = rec.get("merge_lever")

        lines.append(f"  {BOLD}#{i}  {ts_short}{RESET}  ({when})")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append(
            f"  Task:      {task_id if task_id is not None else f'{DIM}NULL{RESET}'}"
        )
        lines.append(
            f"  PR:        {('#' + str(pr)) if pr is not None else f'{DIM}NULL{RESET}'}"
        )
        lines.append(f"  Deliverable verdict: {_color(dv, _VERDICT_COLOR)}")
        lines.append(f"  CI state:            {_color(ci, _CI_COLOR)}")
        lines.append(f"  Merge lever:         {_color(lever, _LEVER_COLOR)}")
        lines.append("")

    lines.append(DIM + "─" * w + RESET)
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if args else 2)

    query = args[0]
    company_dir = find_company_dir()
    all_records = _load_merge_audit(company_dir)

    # Match by PR number (numeric) or task-id (starts with "task-")
    if query.isdigit():
        pr_num = int(query)
        entries = [r for r in all_records if r.get("pr") == pr_num]
    else:
        entries = [r for r in all_records if r.get("task_id") == query]

    # Sort oldest-first for chain display
    entries.sort(key=lambda r: r.get("ts", ""))

    print(render_audit(entries, query))


if __name__ == "__main__":
    main()
