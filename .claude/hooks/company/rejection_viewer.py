#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Rejection Viewer — human-readable display of task admission rejections.

Usage:
    uv run .claude/hooks/company/rejection_viewer.py
    uv run .claude/hooks/company/rejection_viewer.py --limit 50
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


def trunc(s: str, width: int) -> str:
    return (s[: width - 1] + "…") if len(s) > width else s


def load_rejections(path: Path) -> list[dict]:
    """Load JSONL rejection records, newest first. Returns [] when file missing."""
    records: list[dict] = []
    if not path.exists():
        return records
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return records
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return records


def render_rejections(rejections: list[dict], limit: int = 20) -> str:
    w = 78
    lines: list[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("\033[1m  ADMISSION REJECTIONS\033[0m")
    lines.append(f"  {now_str}")
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("")

    if not rejections:
        lines.append("  \033[32mNo rejections on record — all tasks admitted.\033[0m")
        lines.append("")
        lines.append("\033[2m" + "─" * w + "\033[0m")
        return "\n".join(lines)

    total = len(rejections)
    shown = rejections[:limit]
    lines.append(f"  Showing {len(shown)} of {total} (newest first)")
    lines.append("")

    for rec in shown:
        ts = rec.get("ts", "")
        ts_short = ts[:19] if ts else "?"
        when = relative_time(ts)
        title = rec.get("title") or rec.get("task_id") or "?"
        source = rec.get("source") or "?"
        reason = rec.get("reason") or "?"
        shadow = rec.get("shadow", False)

        shadow_tag = "  \033[2m[shadow]\033[0m" if shadow else ""
        lines.append(
            f"  \033[31m✗\033[0m \033[1m{ts_short}\033[0m  ({when}){shadow_tag}"
        )
        lines.append(f"    Title:  {trunc(title, 65)}")
        lines.append(f"    Source: {source}")
        lines.append(f"    Reason: \033[33m{trunc(reason, 65)}\033[0m")
        lines.append("")

    if total > limit:
        lines.append(
            f"  \033[2m... {total - limit} more. Use --limit N to see more.\033[0m"
        )
        lines.append("")

    lines.append("\033[2m" + "─" * w + "\033[0m")
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    limit = 20
    i = 0
    while i < len(args):
        if args[i] in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        elif args[i] in ("-n", "--limit") and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Invalid limit: {args[i + 1]}", file=sys.stderr)
                sys.exit(2)
            i += 2
            continue
        i += 1

    company_dir = find_company_dir()
    rejections_path = company_dir / "state" / "task_admission_rejections.jsonl"
    rejections = load_rejections(rejections_path)
    print(render_rejections(rejections, limit=limit))


if __name__ == "__main__":
    main()
