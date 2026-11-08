#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Operator digest — one read-only morning page across every Gatesmith project.

    gatesmith-digest [--project DIR ...] [--save] [--notify]

Per project: NEEDS YOU (open PRs nothing will merge without a human, red main
CI, an open circuit breaker, pending escalations), SHIPPED (PRs merged in the
last 24 h) and QUALITY (main CI, daemon liveness, queue depth).

Strictly read-only toward project state: the sentinel checks are called with a
throwaway state dict, so the digest never touches silence_sentinel.json and
never writes an escalation. The only external call is ``gh`` (cwd = the
project, with a timeout); a failed gh renders as "unknown", never a crash.
``--notify`` sends one local macOS notification and nothing else.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import silence_sentinel  # noqa: E402

try:
    import daemon_liveness  # noqa: E402
except ImportError:  # pragma: no cover - sibling module always ships
    daemon_liveness = None  # type: ignore[assignment]

GH_TIMEOUT_SECONDS = 30
SHIPPED_MAX_TITLES = 5
NEEDS_MAX_LINES = 4
ESCALATION_WINDOW_DAYS = 7
OPERATOR_CONDITIONS = ("pr_needs_operator", "main_ci_red", "circuit_open")

# gh(args, cwd) -> stdout, or None on any failure.
GhRunner = Callable[[list[str], Path], "str | None"]


def default_gh(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def default_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def discover_projects(launch_agents_dir: Path) -> list[Path]:
    """WorkingDirectory of every dev.gatesmith.daemon.*.plist that exists."""
    found: list[Path] = []
    try:
        plists = sorted(launch_agents_dir.glob("dev.gatesmith.daemon.*.plist"))
    except OSError:
        return found
    for plist in plists:
        try:
            with plist.open("rb") as handle:
                workdir = plistlib.load(handle).get("WorkingDirectory")
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(workdir, str) and Path(workdir).is_dir():
            path = Path(workdir)
            if path not in found:
                found.append(path)
    return found


class _Gh:
    """Per-project gh wrapper: pins cwd, caches by args, remembers failure."""

    def __init__(self, runner: GhRunner, cwd: Path):
        self._runner = runner
        self._cwd = cwd
        self._cache: dict[tuple, str | None] = {}
        self.failed = False

    def __call__(self, args: list[str]) -> str | None:
        key = tuple(args)
        if key not in self._cache:
            try:
                out = self._runner(args, self._cwd)
            except Exception:
                out = None
            if out is None:
                self.failed = True
            self._cache[key] = out
        return self._cache[key]


def _json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text or "[]")
    except json.JSONDecodeError:
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _age(created: datetime | None, now: datetime) -> str:
    if created is None:
        return "?"
    hours = (now - created).total_seconds() / 3600
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"


def _pending_escalations(
    company_dir: Path, now: datetime, skip_conditions: set[str]
) -> list[tuple[str, str]]:
    """(title, age) of pending escalations from the last 7 days. Records that
    merely restate a live sentinel finding are skipped (no double count)."""
    # The sentinel re-raises a condition each dedup window; only the newest
    # record per condition says anything new.
    newest: dict[str, tuple[datetime, str, str]] = {}
    try:
        files = sorted((company_dir / "escalations").glob("*.json"))
    except OSError:
        return []
    cutoff = now - timedelta(days=ESCALATION_WINDOW_DAYS)
    for path in files:
        data = _read_json(path)
        if not isinstance(data, dict) or data.get("status") != "pending":
            continue
        created = silence_sentinel._parse_ts(data.get("created_at"))
        if created is None or created < cutoff:
            continue
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if meta.get("condition") in skip_conditions:
            continue
        title = meta.get("title")
        if not title:
            events = data.get("events") or []
            if events and isinstance(events[0], dict):
                title = events[0].get("action_taken")
        title = str(title or data.get("task_id") or path.stem)
        key = str(meta.get("condition") or title)
        if key not in newest or created > newest[key][0]:
            newest[key] = (created, title, _age(created, now))
    out = sorted(newest.values(), key=lambda row: row[0], reverse=True)
    return [(title, age) for _, title, age in out]


def _queue_pending(company_dir: Path) -> int | None:
    candidates = [
        company_dir / "state" / "work_queue.json",
        company_dir / "state.nosync" / "work_queue.json",
    ]
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return None
    path = max(existing, key=lambda p: p.stat().st_mtime)
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("pending"), list):
        return len(data["pending"])
    return None


def _daemon_running(company_dir: Path) -> bool | None:
    if daemon_liveness is None:
        return None
    try:
        return daemon_liveness.get_project_daemon_pid(company_dir) is not None
    except Exception:
        return None


def _main_ci(gh: _Gh) -> str:
    rows = _json(
        gh(
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
            ]
        )
    )
    if not isinstance(rows, list):
        return "unknown"
    newer_running = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "completed":
            newer_running = True
            continue
        conclusion = str(row.get("conclusion") or "unknown")
        if newer_running and conclusion != "success":
            conclusion += " (a newer run is in progress)"
        return conclusion
    return "unknown"


def _short_title(title) -> str:
    """A PR title for one digest line: no daemon task id, at most 72 chars."""
    text = re.sub(r"\s*\[task-[^\]]*\]", "", str(title or "")).strip()
    return text if len(text) <= 72 else text[:71] + "…"


def _shipped(gh: _Gh, now: datetime) -> list[str] | None:
    since = now - timedelta(hours=24)
    rows = _json(
        gh(
            [
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                f"merged:>={since.date().isoformat()}",
                "--json",
                "number,title,mergedAt",
                "--limit",
                "100",
            ]
        )
    )
    if not isinstance(rows, list):
        return None
    titles: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        merged = silence_sentinel._parse_ts(row.get("mergedAt"))
        if merged is not None and merged >= since:
            titles.append(f"#{row.get('number')} {_short_title(row.get('title'))}")
    return titles


def collect_project(project: Path, gh_runner: GhRunner, now: datetime) -> dict:
    """Everything the digest says about one project (read-only)."""
    company_dir = project / ".company"
    config = silence_sentinel.SentinelConfig(base_dir=project)
    gh = _Gh(gh_runner, project)
    throwaway: dict = {}  # never the project's sentinel state

    needs: list[str] = []
    conditions: set[str] = set()
    checks = (
        lambda: silence_sentinel.check_pr_needs_operator(config, throwaway, now, gh),
        lambda: silence_sentinel.check_main_ci_red(config, throwaway, now, gh),
        lambda: silence_sentinel.check_circuit_open(config),
    )
    for check in checks:
        try:
            finding = check()
        except Exception:
            finding = None
        if finding:
            conditions.add(finding["condition"])
            needs.append(finding["title"])
    for title, age in _pending_escalations(company_dir, now, set(OPERATOR_CONDITIONS)):
        needs.append(f"{title} ({age} old)")

    shipped = _shipped(gh, now)
    ci = _main_ci(gh)
    return {
        "name": project.name,
        "needs": needs,
        "needs_unknown": gh.failed and not needs,
        "shipped": shipped,
        "ci": ci,
        "daemon": _daemon_running(company_dir),
        "queue_pending": _queue_pending(company_dir),
    }


def render_project(info: dict) -> list[str]:
    lines = [f"== {info['name']}"]
    needs = info["needs"]
    if needs:
        lines.append("NEEDS YOU:")
        for item in needs[:NEEDS_MAX_LINES]:
            lines.append(f"  - {item}")
        if len(needs) > NEEDS_MAX_LINES:
            lines.append(f"  - +{len(needs) - NEEDS_MAX_LINES} more")
    elif info["needs_unknown"]:
        lines.append("NEEDS YOU: Nothing found (GitHub unreachable, PR/CI unknown)")
    else:
        lines.append("NEEDS YOU: Nothing")

    shipped = info["shipped"]
    if shipped is None:
        lines.append("SHIPPED (24h): unknown")
    elif not shipped:
        lines.append("SHIPPED (24h): 0")
    else:
        lines.append(f"SHIPPED (24h): {len(shipped)}")
        for title in shipped[:SHIPPED_MAX_TITLES]:
            lines.append(f"  - {title}")
        if len(shipped) > SHIPPED_MAX_TITLES:
            lines.append(f"  - +{len(shipped) - SHIPPED_MAX_TITLES} more")

    daemon = {True: "running", False: "not running", None: "unknown"}[info["daemon"]]
    queue = info["queue_pending"]
    lines.append(
        f"QUALITY: main CI {info['ci']} · daemon {daemon} · "
        f"queue {'unknown' if queue is None else f'{queue} pending'}"
    )
    return lines


def summary_line(infos: list[dict]) -> str:
    needs = sum(len(i["needs"]) for i in infos)
    shipped = sum(len(i["shipped"] or []) for i in infos)
    green = sum(1 for i in infos if i["ci"] == "success")
    unknown = sum(1 for i in infos if i["ci"] == "unknown")
    noun = "thing needs" if needs == 1 else "things need"
    pr_noun = "PR" if shipped == 1 else "PRs"
    ci = f"CI green in {green}/{len(infos)} projects"
    if unknown:
        ci += f" ({unknown} unknown)"
    return f"Gatesmith digest: {needs} {noun} you · {shipped} {pr_noun} shipped · {ci}"


def build_digest(
    projects: list[Path], gh_runner: GhRunner, now: datetime
) -> tuple[str, str]:
    """(summary line, full text)."""
    infos = [collect_project(p, gh_runner, now) for p in projects]
    summary = summary_line(infos)
    lines = [summary, ""]
    for info in infos:
        lines.extend(render_project(info))
        lines.append("")
    return summary, "\n".join(lines).rstrip() + "\n"


def notify(summary: str) -> None:
    """One local macOS notification. Best-effort, never raises."""
    try:
        safe = summary.replace("\\", "").replace('"', "'")
        script = f'display notification "{safe}" with title "Gatesmith digest"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def main(
    argv: list[str] | None = None,
    *,
    launch_agents_dir: Path | None = None,
    gh: GhRunner | None = None,
    now: datetime | None = None,
    notifier: Callable[[str], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="gatesmith-digest", description=__doc__)
    parser.add_argument("--project", action="append", default=[], metavar="DIR")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args(argv)

    now = now or datetime.now(timezone.utc)
    if args.project:
        candidates = [Path(p).expanduser() for p in args.project]
    else:
        candidates = discover_projects(launch_agents_dir or default_launch_agents_dir())
    projects = [p for p in candidates if (p / ".company").is_dir()]
    if not projects:
        print("Gatesmith digest: no projects found (no .company directory)")
        return 0

    summary, text = build_digest(projects, gh or default_gh, now)
    print(text, end="")

    if args.save:
        try:
            out_dir = projects[0] / ".company" / "reports"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"digest-{now.astimezone().strftime('%Y-%m-%d')}.md"
            path.write_text(text)
            print(f"saved: {path}")
        except OSError as exc:
            print(f"save failed: {exc}", file=sys.stderr)
    if args.notify:
        (notifier or notify)(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
