#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Gatesmith Queue Monitor — Live view of work queue, recent completions, and escalations.

Usage:
    uv run .claude/hooks/company/queue_monitor.py              # One-shot display
    uv run .claude/hooks/company/queue_monitor.py --watch      # Live refresh (every 5s)
    uv run .claude/hooks/company/queue_monitor.py --watch 10   # Live refresh (every 10s)
    uv run .claude/hooks/company/queue_monitor.py --json       # JSON output
    uv run .claude/hooks/company/queue_monitor.py --heal       # Detect and fix queue issues
    uv run .claude/hooks/company/queue_monitor.py --heal --dry-run  # Show issues without fixing
    uv run .claude/hooks/company/queue_monitor.py --prs        # List all tasks with PRs
    uv run .claude/hooks/company/queue_monitor.py --completions 25  # Show last 25 completions
    uv run .claude/hooks/company/queue_monitor.py --wide       # Full titles on one line (for piping)
    uv run .claude/hooks/company/queue_monitor.py --company-dir ../other/.company  # Another project
    uv run .claude/hooks/company/queue_monitor.py cancel <id> --reason "..." [--keep-pr] [--dry-run]
    uv run .claude/hooks/company/queue_monitor.py hold <id> --reason "..." [--dry-run]
    uv run .claude/hooks/company/queue_monitor.py unhold <id> [--dry-run]
    uv run .claude/hooks/company/queue_monitor.py external add "<title>" [--branch B] [--pr N] [--owner O] [--note T]
    uv run .claude/hooks/company/queue_monitor.py external update <id> [--status S] [--pr N] [--branch B] [--note T]
    uv run .claude/hooks/company/queue_monitor.py external done <id> [--note T]
    uv run .claude/hooks/company/queue_monitor.py external list [--all]

cancel: terminal 'cancelled' lane (never pulled, healed, retried, released or
counted), stops the task's verified worker process group, closes its open PR.
hold: parks a task in blocked with operator_hold; no sweep releases it.

external: registry for work done OUTSIDE the daemon queue (session-agent
builds, manual fixes) — see external_work.py. Shown in the dashboard's
EXTERNAL section next to open, non-daemon GitHub PRs auto-discovered via
`gh`; `external list` without --all hides done/abandoned items over 24h old.

Titles wrap to the terminal width ($COLUMNS, else 120) instead of being cut;
each row shows the short task id that `gatesmith-queue follow <id>` accepts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add script directory to path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

import external_work
from autonomy_metrics import RECONCILED_STATUS, _extract_roadmap_task_id
from daemon_liveness import get_project_daemon_pid
from queue_io import save_queue_atomic
from work_allocator import is_held_for_review


def find_company_dir():
    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        for parent in [start] + list(start.parents):
            candidate = parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return Path(".company")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# save_queue_atomic imported from queue_io below


def relative_time(iso_str):
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


def trunc(s, width):
    return (s[: width - 1] + "\u2026") if len(s) > width else s


# ---------------------------------------------------------------------------
# Width-aware layout. Titles are the part an operator reads to tell tasks
# apart, so they are WRAPPED onto continuation lines under the title column,
# never cut. The title is always the last column on a row, which lets every
# other column keep a fixed width and the title take whatever is left.
# ---------------------------------------------------------------------------

DEFAULT_TERM_WIDTH = 120
MIN_TITLE_WIDTH = 20
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_QUEUE_FILL_RE = re.compile(r"^(\[QUEUE-FILL\]\s+\S+:\s*)(.*?)((?:\s\(round \d+\))?)$")
_ACTION_RE = re.compile(r"^\*\*Action Required:\*\*\s*(.+?)\s*$", re.MULTILINE)
_DAEMON_ID_RE = re.compile(r"^daemon-(\d+)-\d{6,}$")


def terminal_width() -> int:
    """Current terminal width (honours $COLUMNS); 120 when not a terminal."""
    return shutil.get_terminal_size(fallback=(DEFAULT_TERM_WIDTH, 24)).columns


def display_width(s: str) -> int:
    """Printed width of *s*: ANSI codes count 0, wide (CJK/emoji) chars 2."""
    width = 0
    for ch in _ANSI_RE.sub("", s):
        if unicodedata.combining(ch) or unicodedata.category(ch) == "Cf":
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(s: str, width: int) -> str:
    """Left-justify *s* to *width* printed columns (ANSI/unicode aware)."""
    return s + " " * max(width - display_width(s), 0)


def wrap_text(text: str, width: int) -> list[str]:
    """Word-wrap *text* to *width* printed columns without dropping anything.

    Whitespace (including newlines) collapses to single spaces. A word wider
    than the line is split across lines rather than cut.
    """
    width = max(width, 1)
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}" if cur else word
        if display_width(candidate) <= width:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while display_width(word) > width:
            piece = ""
            for ch in word:
                if display_width(piece + ch) > width:
                    break
                piece += ch
            if not piece:  # a single char wider than the line
                piece = word[0]
            # Prefer breaking an id like CONTACTS-FROM-MEETINGS after a hyphen.
            cut = piece.rfind("-")
            if 0 < cut < len(piece) - 1:
                piece = piece[: cut + 1]
            lines.append(piece)
            word = word[len(piece) :]
        cur = word
    if cur:
        lines.append(cur)
    return lines


def short_task_id(task_id) -> str:
    """Last segment of a task id (``task-…-8939d2`` -> ``8939d2``).

    ``gatesmith-queue follow`` matches ids by suffix, so this is enough to follow.
    """
    if not task_id:
        return "?"
    return str(task_id).rsplit("-", 1)[-1] or str(task_id)


def pr_number(pr_url) -> str:
    """PR number parsed from a GitHub PR URL, or ``""``."""
    m = re.search(r"/pull/(\d+)", pr_url or "")
    return m.group(1) if m else ""


def display_title(task: dict) -> str:
    """The task's title, whitespace-collapsed, with QUEUE-FILL titles restored.

    The strategic planner cuts the action in a ``[QUEUE-FILL] Gn: <action>``
    title at 60 characters, which can cut through the very entry names that
    tell two tasks apart. The full action is in the description's
    ``**Action Required:**`` line; when the stored title is a prefix of it,
    show the full action instead (display only — the stored title is the
    dedup key and stays as-is).
    """
    title = " ".join(str(task.get("title") or "").split())
    m = _QUEUE_FILL_RE.match(title)
    if not m:
        return title
    head, action, suffix = m.groups()
    am = _ACTION_RE.search(str(task.get("description") or ""))
    if not am:
        return title
    full = " ".join(am.group(1).split())
    if action and full.startswith(action) and len(full) > len(action):
        return f"{head}{full}{suffix}"
    return title


def blocked_reason(task: dict) -> str:
    """Why a blocked task is blocked, from every field that can say so."""
    parts: list[str] = []
    for key in ("blocked_reason", "last_error"):
        val = " ".join(str(task.get(key) or "").split())
        if val and val not in parts:
            parts.append(val)
    if not parts and task.get("blocked_by_tasks"):
        ids = ", ".join(short_task_id(t) for t in task["blocked_by_tasks"])
        parts.append(f"waiting on {ids}")
    return " \u2014 ".join(parts) or "unknown"


def table_rows(prefix: str, text: str, width: int | None, style: str = "") -> list[str]:
    """Render one table row: fixed-width *prefix* cells, then *text* wrapped.

    Continuation lines are indented to the start of the text column. With
    *width* None (``--wide``) the text stays on one line, whatever its length.
    """
    indent = 2 + display_width(prefix)
    close = "\033[0m" if style else ""
    if width is None:
        return [f"  {prefix}{style}{text}{close}"]
    parts = wrap_text(text, max(width - indent, MIN_TITLE_WIDTH))
    out = [f"  {prefix}{style}{parts[0]}{close}"]
    out.extend(f"{' ' * indent}{style}{p}{close}" for p in parts[1:])
    return out


def pri_label(p):
    return {1: "P1 CRIT", 2: "P2 HIGH", 3: "P3 NORM", 4: "P4 LOW", 5: "P5 MIN"}.get(
        p, f"P{p}"
    )


def emp_short(eid, task=None):
    if not eid:
        return ""
    # "daemon-<pid>-<start epoch>": the pid is what identifies the worker.
    m = _DAEMON_ID_RE.match(eid)
    name = f"d:{m.group(1)}" if m else eid.replace("daemon-", "d:")
    if task and task.get("execution_mode") == "agent-team":
        size = task.get("team_size", 3)
        name = f"{name} \033[1;35m[TEAM x{size}]\033[0m"
    return name


def get_pr_status(pr_url):
    """Check PR merge status via gh CLI. Returns (is_merged, pr_number)."""
    if not pr_url:
        return None, None
    try:
        # Extract PR number from URL
        import re

        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            return None, None
        pr_number = match.group(1)

        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            import json as _json

            data = _json.loads(result.stdout)
            # state is "MERGED", "OPEN", or "CLOSED"
            return data.get("state") == "MERGED", pr_number
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return None, None


# Every daemon-initiated `gh pr close` in forge_daemon.py leaves a close comment
# whose body starts with one of these literals: five "Auto-closed" bodies
# (path-D CI failure, path-D2 repeated CI failure, irreconcilable merge
# conflicts, stale merge conflicts, all-CI-failed) and two "Auto-closing:"
# bodies (escalation cap, nested escalation). tests/test_queue_monitor.py reads
# forge_daemon.py and fails if a close body appears that none of these match.
_DAEMON_CLOSE_PREFIXES = ("Auto-closed", "Auto-closing")

# `gh pr view --json` exposes mergedBy but no closedBy; the closer is the
# actor on the PR timeline's last CLOSED_EVENT.
_PR_CLOSER_QUERY = (
    "query($owner:String!,$name:String!,$n:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$n){"
    "timelineItems(last:1,itemTypes:CLOSED_EVENT){nodes{"
    "... on ClosedEvent{actor{login}}}}}}}"
)


def _pr_auto_closed_by_daemon(pr_number):
    """True if the daemon closed this PR, False if a human closed it, None if unknown.

    The daemon's own `gh pr close` calls (forge_daemon.py: CI-failure paths
    D/D2, merge-conflict cleanup, escalation cap, nested escalation) always
    leave a close comment whose body starts with one of
    _DAEMON_CLOSE_PREFIXES. Absence of such a comment on a CLOSED PR means a
    human closed it via GitHub.

    Returns None (unknown) when the PR isn't CLOSED, or the lookup/parse
    fails for any reason — callers must treat None as "leave the task
    alone," never as either True or False.

    A collaborator with comment access could leave a comment starting with
    "Auto-closed" on their own closed PR and get the requeue-to-pending
    branch instead of the terminal closed_unmerged one. That only reroutes
    between two non-"completed" outcomes — it can never fabricate a shipped
    delivery — and the same actor already has write access to close the PR
    outright, so this isn't a new trust boundary.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "state,comments"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data.get("state") != "CLOSED":
                return None
            for comment in data.get("comments", []):
                if isinstance(comment, dict) and str(
                    comment.get("body", "")
                ).startswith(_DAEMON_CLOSE_PREFIXES):
                    return True
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return None


def _pr_closer_login(pr_number):
    """Login of whoever closed the PR last, or None when it cannot be told.

    Best-effort: the result only decorates the reconcile reason — "closed
    unmerged (<login>)" instead of "(human)" — and never decides what heal
    does. `{owner}`/`{repo}` are gh's own placeholders for the current repo.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                "owner={owner}",
                "-F",
                "name={repo}",
                "-F",
                f"n={int(pr_number)}",
                "-f",
                f"query={_PR_CLOSER_QUERY}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            nodes = data["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
            if nodes and isinstance(nodes[0], dict):
                login = (nodes[0].get("actor") or {}).get("login")
                if isinstance(login, str) and login.strip():
                    return login.strip()
    except Exception:
        pass
    return None


def pr_status_label(pr_url, cache={}):
    """Return colored PR status label. Uses simple cache to avoid repeated gh calls."""
    if not pr_url:
        return ""
    if pr_url in cache:
        return cache[pr_url]

    is_merged, pr_num = get_pr_status(pr_url)
    if is_merged is True:
        label = f"\033[32m#PR{pr_num}\u2713\033[0m"  # Green checkmark
    elif is_merged is False:
        label = f"\033[33m#PR{pr_num}\033[0m"  # Yellow (open)
    else:
        label = "\033[2m#PR?\033[0m"  # Gray (unknown)
    cache[pr_url] = label
    return label


def get_queue_path(company_dir):
    p = company_dir / "state" / "work_queue.json"
    return p if p.exists() else company_dir / "work_queue.json"


def get_active_daemon_pids(company_dir):
    """PIDs of THIS project's live daemon (empty set when it is not running).

    Reads the project's pidfile rather than ``pgrep -f forge_daemon``, which
    also matched every other project's daemon.
    """
    pid = get_project_daemon_pid(company_dir)
    return {str(pid)} if pid is not None else set()


def _last_failure_note(health_metrics: dict) -> str:
    """Age of the newest failure, rendered next to the success rate.

    total_successes/total_failures are LIFETIME cumulative counters over
    execution ATTEMPTS (retries included) that never age out. The rate built
    from them therefore cannot recover: on 2026-08-15 the dashboard read
    69% (194/280) while every one of the 86 failures predated 2026-08-02 --
    13 days with a clean record, displayed as a two-thirds success rate.
    Read alongside autonomy_metrics (86% lifetime, 77% over 30 days, both
    over DISTINCT TASKS rather than attempts), the bare number invited the
    conclusion that something had regressed when nothing had.

    Showing when the last failure actually happened costs one field and makes
    the figure self-describing: a stale denominator is obvious at a glance.
    """
    stamp = health_metrics.get("last_failure_time")
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    if delta.days >= 1:
        age = f"{delta.days}d"
    elif delta.seconds >= 3600:
        age = f"{delta.seconds // 3600}h"
    else:
        age = f"{max(delta.seconds // 60, 0)}m"
    return f" \033[2mlifetime, last fail {age} ago\033[0m"


def render_cancelled(cancelled, title_w, shown=5) -> list[str]:
    """CANCELLED section: operator-ended tasks, never shown as completions."""
    if not cancelled:
        return []
    recent = sorted(
        [t for t in cancelled if isinstance(t, dict)],
        key=lambda t: str(t.get("cancelled_at") or ""),
        reverse=True,
    )[:shown]
    lines = [
        f"\033[1;90m  CANCELLED ({len(cancelled)} total, showing last {len(recent)})\033[0m"
    ]
    lines.append(f"  {'When':<12}   {'ID':<7} Task / reason")
    lines.append(f"  {'----':<12}   {'--':<7} -------------")
    for t in recent:
        prefix = (
            f"{relative_time(t.get('cancelled_at')):<12} \033[90m-\033[0m "
            f"{short_task_id(t.get('task_id')):<7} "
        )
        lines.extend(table_rows(prefix, display_title(t), title_w))
        why = f"by {t.get('cancelled_by') or '?'}: {t.get('cancel_reason') or '?'}"
        lines.extend(table_rows(" " * len(prefix), why, title_w, "\033[2m"))
    lines.append("")
    return lines


def external_rows(company_dir, discover_external=False):
    """Registry (running/pr-open) items + auto-discovered non-daemon open PRs,
    deduped by PR number, newest-first. Each row: ``{id, owner, age_source,
    pr, title, source}`` where *source* is ``"registry"`` or ``"gh"``.

    *discover_external* gates the `gh pr list` call — off by default so a
    bare render_dashboard() call (as most tests make it) never shells out;
    the CLI entry point (`gatesmith-queue` / `gatesmith-queue watch`) turns it on.
    """
    items = external_work.active_items(company_dir)
    rows = [
        {
            "id": short_task_id(it.get("id")),
            "owner": str(it.get("owner") or "-"),
            "age_source": it.get("started_at"),
            "pr": it.get("pr"),
            "title": str(it.get("title") or ""),
            "source": "registry",
        }
        for it in items
    ]
    if not discover_external:
        return rows
    known_prs = {int(it["pr"]) for it in items if it.get("pr")}
    for p in external_work.discover_external_prs(company_dir):
        num = p.get("number")
        if num is None or int(num) in known_prs:
            continue
        rows.append(
            {
                "id": f"PR{num}",
                "owner": "PR",
                "age_source": p.get("createdAt"),
                "pr": num,
                "title": str(p.get("title") or ""),
                "source": "gh",
            }
        )
    return rows


def render_external(company_dir, title_w, *, discover_external=False) -> list[str]:
    """EXTERNAL section: session-agent / manual work outside the daemon
    queue, shown next to it (registry items) plus open GitHub PRs that
    aren't the daemon's own, auto-discovered and deduped by PR number."""
    rows = external_rows(company_dir, discover_external=discover_external)
    lines = [f"\033[1;34m  EXTERNAL ({len(rows)})\033[0m"]
    if not rows:
        lines.append("  \033[2m(none)\033[0m")
        lines.append("")
        return lines
    aw = min(max([8] + [display_width(r["owner"]) for r in rows]), 20)
    head = f"{'ID':<10} {pad('Owner', aw)} {'Age':<12} {'PR':<8} "
    lines.append(f"  {head}Task")
    lines.append(f"  {'--':<10} {pad('-----', aw)} {'---':<12} {'--':<8} ----")
    for r in rows:
        prefix = (
            f"{r['id']:<10} "
            f"{pad(r['owner'], aw)} "
            f"{relative_time(r['age_source']):<12} "
            f"{('#' + str(r['pr'])) if r['pr'] else '-':<8} "
        )
        lines.extend(table_rows(prefix, r["title"], title_w))
    lines.append("")
    return lines


_DEP_SKIP_PREFIX = "dependency_incomplete:"
_DEP_WAIT_HORIZON = timedelta(hours=24)
_DEP_SKIP_TAIL_LINES = 2000
_QUEUE_FILL_GOAL_RE = re.compile(r"^\[QUEUE-FILL\]\s+(G\d+):")


def dependency_waits(company_dir, queue=None, now=None) -> list[dict]:
    """Goals autofill skips because a goal they depend on is not verified.

    Read from ``state/autofill_brief_skips.jsonl`` (the ``dependency_incomplete``
    rows strategic_planner writes) within the last 24 h, newest row per goal.
    A dependency the verification state now records as verified is dropped (the
    row predates the stamp). Each item: ``{"goal", "waits_on": [...], "since", "prs":
    {dep: pr_number}}`` where ``prs`` names a PR from the dependency's own
    ``[QUEUE-FILL]`` task, for the ``goal_tracker.py verify`` hint.
    webapp 2026-10-20: G58 waited 8 h on G56, whose PR had merged unverified,
    while the queue read 0 pending with no reason given.
    """
    company_dir = Path(company_dir)
    now = now or datetime.now(timezone.utc)
    path = company_dir / "state" / "autofill_brief_skips.jsonl"
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-_DEP_SKIP_TAIL_LINES:]
    except OSError:
        return []
    latest: dict[str, tuple[datetime, list[str]]] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        deps: list[str] = []
        for item in row.get("missing") or []:
            if isinstance(item, str) and item.startswith(_DEP_SKIP_PREFIX):
                deps += [
                    d.strip()
                    for d in item[len(_DEP_SKIP_PREFIX) :].split(",")
                    if d.strip()
                ]
        goal = row.get("goal_id")
        if not isinstance(goal, str):
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if now - ts > _DEP_WAIT_HORIZON:
            continue
        # The goal's NEWEST skip row decides: a later row with another reason
        # (admission refused, ...) means the dependency no longer holds it.
        if goal not in latest or ts >= latest[goal][0]:
            latest[goal] = (ts, deps)
    latest = {g: v for g, v in latest.items() if v[1]}
    if not latest:
        return []
    verification = (
        load_json(company_dir / "state" / "goal_verification_state.json") or {}
    )

    def _verified(goal_id: str) -> bool:
        rec = verification.get(goal_id) if isinstance(verification, dict) else None
        return isinstance(rec, dict) and rec.get("status") == "verified"

    prs_by_goal: dict[str, str] = {}
    for lane in ("completed", "pr_open", "blocked", "in_progress", "pending"):
        for task in (queue or {}).get(lane) or []:
            if not isinstance(task, dict):
                continue
            m = _QUEUE_FILL_GOAL_RE.match(str(task.get("title") or ""))
            num = pr_number(task.get("pr_url"))
            if m and num:
                prs_by_goal[m.group(1)] = num
    out = []
    for goal, (ts, deps) in sorted(latest.items(), key=lambda kv: kv[0]):
        if _verified(goal):
            continue
        open_deps = [d for d in deps if not _verified(d)]
        if not open_deps:
            continue
        out.append(
            {
                "goal": goal,
                "waits_on": open_deps,
                "since": ts.isoformat(),
                "prs": {d: prs_by_goal[d] for d in open_deps if d in prs_by_goal},
            }
        )
    return out


def render_dependency_waits(waits: list[dict], title_w) -> list[str]:
    """The "why is the queue empty" lines for dependency_incomplete skips."""
    if not waits:
        return []
    lines = [
        "\033[1;33m  WAITING ON GOALS ({}) \u2014 autofill mints nothing for these\033[0m".format(
            len(waits)
        )
    ]
    for w in waits:
        deps = ", ".join(w["waits_on"])
        lines.extend(
            table_rows(
                "  ",
                f"{w['goal']} waits on {deps} (not verified)",
                title_w,
            )
        )
        for dep in w["waits_on"]:
            pr = w["prs"].get(dep)
            hint = f"if {dep} shipped: goal_tracker.py verify --goal {dep}" + (
                f" --pr {pr}" if pr else " --pr <merged PR>"
            )
            lines.extend(table_rows("    ", hint, title_w, "\033[2m"))
    lines.append("")
    return lines


def render_dashboard(
    company_dir, completions_n=10, width=None, wide=False, discover_external=False
):
    """Render the one-shot / watch dashboard.

    *width* is the terminal width to lay out for (default: the current
    terminal, $COLUMNS, else 120). Long titles wrap onto continuation lines;
    *wide* keeps every title on one line regardless of width (for piping).
    """
    lines = []
    if width is None:
        width = terminal_width()
    title_w = None if wide else width
    w = 78 if wide else max(min(width, 160) - 2, 40)
    queue = load_json(get_queue_path(company_dir)) or {}
    loop_data = load_json(company_dir / "state" / "loop_monitor.json")
    if not loop_data:
        loop_data = load_json(company_dir / "loop_monitor.json") or {}
    org = load_json(company_dir / "org.json") or {}
    company_name = org.get("company", {}).get("name", "Gatesmith")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append(f"\033[1m  {company_name} \u2014 QUEUE MONITOR\033[0m")
    lines.append(f"  {now_str}")
    lines.append("\033[1m" + "=" * w + "\033[0m")

    cb = loop_data.get("circuit_breaker", {})
    hm = loop_data.get("health_metrics", {})
    cb_state = cb.get("state", "unknown").upper()
    total_ok = hm.get("total_successes", 0)
    total_fail = hm.get("total_failures", 0)
    total = total_ok + total_fail
    rate = f"{total_ok / total * 100:.0f}%" if total > 0 else "n/a"
    cb_colors = {"CLOSED": "\033[32m", "OPEN": "\033[31m"}
    cb_color = cb_colors.get(cb_state, "\033[33m")
    lines.append("")
    lines.append(
        f"  Circuit: {cb_color}{cb_state}\033[0m  |  "
        f"Success: {rate} ({total_ok}/{total}){_last_failure_note(hm)}  |  "
        f"Queue: {len(queue.get('pending', []))}P / "
        f"{len(queue.get('in_progress', []))}A / "
        f"{len(queue.get('completed', []))}C"
        + (f" / {len(queue.get('cancelled', []))}X" if queue.get("cancelled") else "")
    )
    lines.append("")

    in_progress = queue.get("in_progress", [])
    lines.append(f"\033[1;33m  IN PROGRESS ({len(in_progress)})\033[0m")
    if in_progress:
        assigned = [
            emp_short(t.get("assigned_to") or t.get("claimed_by", ""), t)
            for t in in_progress
        ]
        aw = min(max([8] + [display_width(a) for a in assigned]), 30)
        head = f"{'Pri':<8} {'ID':<7} {pad('Assigned', aw)} {'Started':<12} "
        lines.append(f"  {head}Task")
        lines.append(
            f"  {'---':<8} {'--':<7} {pad('--------', aw)} {'-------':<12} ----"
        )
        for t, who in zip(in_progress, assigned):
            prefix = (
                f"{pri_label(t.get('priority')):<8} "
                f"{short_task_id(t.get('task_id')):<7} "
                f"{pad(who, aw)} "
                f"{relative_time(t.get('claimed_at') or t.get('started_at')):<12} "
            )
            lines.extend(table_rows(prefix, display_title(t), title_w))
    else:
        lines.append("  \033[2m(none)\033[0m")
    lines.append("")

    lines.extend(
        render_external(company_dir, title_w, discover_external=discover_external)
    )

    pending = queue.get("pending", [])
    lines.append(f"\033[1;36m  PENDING ({len(pending)})\033[0m")
    if pending:
        lines.append(f"  {'Pri':<8} {'ID':<7} {'Waiting':<12} Task")
        lines.append(f"  {'---':<8} {'--':<7} {'-------':<12} ----")
        for t in pending[:15]:
            prefix = (
                f"{pri_label(t.get('priority')):<8} "
                f"{short_task_id(t.get('task_id')):<7} "
                f"{relative_time(t.get('created_at')):<12} "
            )
            lines.extend(table_rows(prefix, display_title(t), title_w))
            caps = ", ".join(t.get("required_capabilities", [])[:3])
            if caps:
                lines.extend(
                    table_rows(" " * len(prefix), f"[{caps}]", title_w, "\033[2m")
                )
        if len(pending) > 15:
            lines.append(f"  \033[2m... and {len(pending) - 15} more\033[0m")
    else:
        lines.append("  \033[2m(none)\033[0m")
    lines.append("")

    if not pending and not in_progress:
        # An empty queue with a reason: a goal waits on an unverified one.
        lines.extend(
            render_dependency_waits(dependency_waits(company_dir, queue), title_w)
        )

    blocked = queue.get("blocked", [])
    if blocked:
        lines.append(f"\033[1;31m  BLOCKED ({len(blocked)})\033[0m")
        lines.append(f"  {'ID':<7} {'PR':<7} {'Since':<12} Task / reason")
        lines.append(f"  {'--':<7} {'--':<7} {'-----':<12} -------------")
        for t in blocked[:5]:
            num = pr_number(t.get("pr_url"))
            prefix = (
                f"{short_task_id(t.get('task_id')):<7} "
                f"{('#' + num) if num else '-':<7} "
                f"{relative_time(t.get('blocked_at') or t.get('created_at')):<12} "
            )
            lines.extend(table_rows(prefix, display_title(t), title_w))
            hold = "HOLD " if t.get("operator_hold") is True else ""
            lines.extend(
                table_rows(
                    " " * len(prefix),
                    f"{hold}reason: {blocked_reason(t)}",
                    title_w,
                    "\033[2m",
                )
            )
        if len(blocked) > 5:
            lines.append(f"  \033[2m... and {len(blocked) - 5} more\033[0m")
        lines.append("")

    pr_open_tasks = queue.get("pr_open", [])
    if pr_open_tasks:
        lines.append(
            f"\033[1;35m  PR OPEN ({len(pr_open_tasks)}) — awaiting merge\033[0m"
        )
        lines.append(f"  {'ID':<7} {'PR':<10} {'Since':<12} Task")
        lines.append(f"  {'--':<7} {'--':<10} {'-----':<12} ----")
        for t in pr_open_tasks[:10]:
            num = pr_number(t.get("pr_url"))
            pr_label = (
                pr_status_label(t.get("pr_url"))
                if t.get("pr_url")
                else "\033[2m-\033[0m"
            )
            if num and "#PR?" in pr_label:
                # gh unavailable: the number is still in the URL.
                pr_label = f"\033[2m#PR{num}\033[0m"
            prefix = (
                f"{short_task_id(t.get('task_id')):<7} "
                f"{pad(pr_label, 10)} "
                f"{relative_time(t.get('pr_open_at') or t.get('completed_at') or t.get('created_at')):<12} "
            )
            lines.extend(table_rows(prefix, display_title(t), title_w))
        if len(pr_open_tasks) > 10:
            lines.append(f"  \033[2m... and {len(pr_open_tasks) - 10} more\033[0m")
        lines.append("")

    completed = queue.get("completed", [])
    stamped = sorted(
        [t for t in completed if t.get("completed_at")],
        key=lambda t: t.get("completed_at", ""),
        reverse=True,
    )
    unstamped = [t for t in completed if not t.get("completed_at")]
    recent = (stamped + unstamped)[:completions_n]
    lines.append(
        f"\033[1;32m  RECENT COMPLETIONS ({len(completed)} total, showing last {len(recent)})\033[0m"
    )
    if recent:
        lines.append(f"  {'When':<12}   {'ID':<7} {'By':<14} {'PR':<10} Task")
        lines.append(f"  {'----':<12}   {'--':<7} {'--':<14} {'--':<10} ----")
        for t in recent:
            marker = (
                "\033[32m+\033[0m"
                if t.get("result") == "completed"
                else "\033[31mx\033[0m"
            )
            completed_by = emp_short(t.get("assigned_to") or t.get("claimed_by", ""), t)
            pr_label = (
                pr_status_label(t.get("pr_url"))
                if t.get("pr_url")
                else "\033[2m-\033[0m"
            )
            prefix = (
                f"{relative_time(t.get('completed_at')):<12} {marker} "
                f"{short_task_id(t.get('task_id')):<7} "
                f"{pad(trunc(_ANSI_RE.sub('', completed_by), 14), 14)} "
                f"{pad(pr_label, 10)} "
            )
            lines.extend(table_rows(prefix, display_title(t), title_w))
    else:
        lines.append("  \033[2m(none)\033[0m")
    lines.append("")

    lines.extend(render_cancelled(queue.get("cancelled", []), title_w))

    escalations = []
    esc_dir = company_dir / "escalations"
    if esc_dir.is_dir():
        for fp in esc_dir.glob("*.json"):
            esc = load_json(fp)
            if esc and esc.get("status") != "resolved":
                escalations.append(esc)
    # Newest first BY TIME. Sorting the filenames instead ordered by prefix
    # ("task-" > "silence-" > "admission-"), which pushed whole categories to
    # the bottom regardless of age — the newest escalation sorted last and was
    # always the one the display cap dropped.
    escalations.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    approvals = load_json(company_dir / "pending_approvals.json")
    pending_approvals = []
    if isinstance(approvals, dict):
        pending_approvals = [
            a
            for a in approvals.get("pending", [])
            if a.get("status") in ("pending", "pending_review")
        ]
    esc_count = len(escalations) + len(pending_approvals)
    if esc_count > 0:
        lines.append(f"\033[1;31m  NEEDS ATTENTION ({esc_count})\033[0m")
        # Build task title lookup from queue for richer display
        _title_map = {}
        for _status in ("pending", "in_progress", "blocked", "completed"):
            for _t in queue.get(_status, [])[-50:]:
                _tid = _t.get("task_id", "")
                if _tid:
                    _title_map[_tid] = _t.get("title", "")
        _ESC_SHOWN = 5
        for e in escalations[:_ESC_SHOWN]:
            _eid = e.get("task_id", "?")
            # Try to get title from escalation fields, metadata, queue lookup.
            # metadata.title is what silence_sentinel and task_admission write;
            # without it every sentinel page rendered as "Unknown task".
            _meta = e.get("metadata") or {}
            _title = (
                e.get("title")
                or _meta.get("title")
                or _meta.get("task_title")
                or e.get("original_task", {}).get("title")
                or _title_map.get(_eid)
                or e.get("reason")
                or e.get("escalation_reason")
                or "Unknown task"
            )
            _trigger = e.get("trigger", e.get("escalation_trigger", ""))
            _age = relative_time(e.get("created_at", e.get("escalated_at")))
            _text = " ".join(str(_title).split())
            if _trigger:
                _text += f" ({_trigger})"
            lines.extend(table_rows(f"\033[31m!\033[0m {_age:<12} ", _text, title_w))
        for a in pending_approvals[:_ESC_SHOWN]:
            _age = relative_time(a.get("submitted_at", a.get("created_at")))
            _text = " ".join(str(a.get("title", a.get("type", "?"))).split())
            lines.extend(table_rows(f"\033[33m?\033[0m {_age:<12} ", _text, title_w))
        # Say what the cap dropped. A section headed "NEEDS ATTENTION (9)" that
        # lists five reads as nine listed unless you count them.
        _hidden = max(len(escalations) - _ESC_SHOWN, 0) + max(
            len(pending_approvals) - _ESC_SHOWN, 0
        )
        if _hidden:
            lines.append(
                f"  \033[2m… and {_hidden} more — see .company/escalations/\033[0m"
            )
        lines.append("")
    else:
        lines.append("\033[1;32m  ESCALATIONS: None \u2014 all clear\033[0m")
        lines.append("")

    lines.append("\033[2m" + "-" * w + "\033[0m")
    lines.append(
        "\033[2m  P=Pending A=Active C=Completed X=Cancelled | --wide | --watch | --json | --heal\033[0m"
    )
    lines.append("\033[2m  ID column: gatesmith-queue follow <id>\033[0m")
    lines.append("\033[2m" + "-" * w + "\033[0m")
    return "\n".join(lines)


def render_pr_list(company_dir, width=None, wide=False):
    """Render table of every task carrying a pr_url, sorted newest-first."""
    title_w = None if wide else (width or terminal_width())
    queue = load_json(get_queue_path(company_dir)) or {}
    lines = []
    w = 78

    all_tasks = []
    for lane in (
        "pending",
        "in_progress",
        "blocked",
        "pr_open",
        "completed",
        "failed",
        "archived",
    ):
        for t in queue.get(lane, []):
            if t.get("pr_url"):
                all_tasks.append(t)

    all_tasks.sort(
        key=lambda t: t.get("completed_at") or t.get("created_at") or "",
        reverse=True,
    )

    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append(f"\033[1m  ALL PRs ({len(all_tasks)})\033[0m")
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("")

    if all_tasks:
        pr_cache = {}
        lines.append(f"  {'ID':<7} {'PR':<10} Title")
        lines.append(f"  {'--':<7} {'--':<10} -----")
        for t in all_tasks:
            label = pr_status_label(t.get("pr_url"), cache=pr_cache)
            prefix = f"{short_task_id(t.get('task_id')):<7} {pad(label, 10)} "
            lines.extend(table_rows(prefix, display_title(t), title_w))
    else:
        lines.append("  \033[2m(no PRs found)\033[0m")

    lines.append("")
    lines.append("\033[2m" + "-" * w + "\033[0m")
    return "\n".join(lines)


STALE_HOURS = 2
ESC_STALE_HOURS = 48


def _reconcile_pr_open_lane(company_dir, dry_run):
    """Advance pr_open tasks whose PR merged (or was closed), as the daemon does.

    The daemon runs this reconcile every 5 minutes. Without a daemon (one /run-loop
    cycle at a time, as the first-run course teaches) nothing else does, so a task whose
    PR was merged by hand stayed "PR OPEN — awaiting merge" and never reached the results.
    The reconcile reads work_allocator's queue, so it runs only when that is the queue
    being healed; heal --company-dir on another project leaves it alone.
    Returns (findings, actions, reconciled_count).
    """
    try:
        import autonomy_metrics as am
        import work_allocator as wa

        if wa.get_queue_path().resolve() != get_queue_path(company_dir).resolve():
            return [], [], 0
        lane = (load_json(get_queue_path(company_dir)) or {}).get("pr_open") or []
        titles = {t.get("task_id"): t.get("title", "") for t in lane}
        result = am.reconcile_queue_closed_unmerged(
            Path(company_dir), dry_run=dry_run, pr_open_only=True
        )
    except Exception:
        return [], [], 0  # best-effort, like the daemon's own fast reconcile
    findings, actions = [], []
    for change in result.get("changes") or []:
        findings.append(
            {
                "type": "pr_open_resolved",
                "severity": "low",
                "task_id": change.get("task_id", "?"),
                "title": titles.get(change.get("task_id"), "")[:60],
                "reason": f"{change.get('transition')} ({change.get('pr_url')})",
            }
        )
        if not dry_run:
            actions.append(
                {
                    "action": "reconciled_pr_open",
                    "task_id": change.get("task_id"),
                    "transition": change.get("transition"),
                }
            )
    return findings, actions, int(result.get("reconciled", 0) or 0)


def heal_queue(company_dir, dry_run=False):
    # P4: Run fuzzy dedup FIRST (acquires its own lock, saves independently)
    # Must run before loading queue to avoid discarding in-memory modifications
    fuzzy_findings = []
    fuzzy_actions = []
    try:
        from work_allocator import deduplicate_pending_tasks

        dedup_result = deduplicate_pending_tasks(dry_run=dry_run)
        for removed in dedup_result.get("duplicates", []):
            fuzzy_findings.append(
                {
                    "type": "fuzzy_duplicate",
                    "severity": "low",
                    "task_id": removed.get("task_id", "?"),
                    "title": removed.get("title", "")[:60],
                    "reason": f"similar to {removed.get('kept_task_id', '?')}",
                }
            )
            if not dry_run:
                fuzzy_actions.append(
                    {
                        "action": "removed_fuzzy_duplicate",
                        "task_id": removed.get("task_id"),
                    }
                )
    except Exception:
        pass  # dedup is best-effort

    queue_path = get_queue_path(company_dir)
    queue = load_json(queue_path) or {}
    now = datetime.now(timezone.utc)
    active_pids = get_active_daemon_pids(company_dir)
    findings = []
    actions = []
    modified = False
    # Roadmap ids to mark completed once the queue write below is durable —
    # see the completed_merged_pr_task branch. Deferred so a crash between
    # here and save_queue_atomic can't leave roadmap_state.json claiming a
    # task done that work_queue.json still shows blocked.
    roadmap_completions = []

    # Sweep both in_progress and blocked lanes for stranded daemon tasks.
    # blocked lane was previously only checked for tasks owned by other
    # daemons (in forge_daemon._cleanup_orphan_tasks) — but a worker can
    # die while its daemon stays alive, leaving the task stranded.
    for source in ("in_progress", "blocked"):
        for t in list(queue.get(source, [])):
            # Respect explicit human-set blocks and dependency blocks
            if t.get("operator_hold") is True:
                continue
            if source == "blocked":
                if t.get("blocked_reason") or t.get("dependencies"):
                    continue
            task_id = t.get("task_id", "?")
            claimed_by = t.get("claimed_by") or t.get("assigned_to") or ""
            claimed_at = t.get("claimed_at") or t.get("started_at")
            is_daemon = claimed_by.startswith("daemon-")
            # Extract numeric PID from 'daemon-{pid}' or 'daemon-{pid}-{ts}'.
            _cb_parts = claimed_by.split("-")
            daemon_pid = _cb_parts[1] if is_daemon and len(_cb_parts) >= 2 else ""
            pid_dead = is_daemon and daemon_pid and daemon_pid not in active_pids
            age_hours = 0.0
            if claimed_at:
                try:
                    dt = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
                    age_hours = (now - dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass
            if pid_dead or age_hours > STALE_HOURS:
                parts = []
                if pid_dead:
                    parts.append(f"daemon PID {daemon_pid} dead")
                if age_hours > STALE_HOURS:
                    parts.append(f"claimed {age_hours:.1f}h ago")
                findings.append(
                    {
                        "type": "stale_task",
                        "severity": "high",
                        "task_id": task_id,
                        "title": t.get("title", "")[:60],
                        "reason": f"[{source}] " + "; ".join(parts),
                    }
                )
                if not dry_run:
                    queue[source].remove(t)
                    for key in [
                        "assigned_to",
                        "assigned_at",
                        "started_at",
                        "claimed_by",
                        "claimed_at",
                    ]:
                        t[key] = None
                    queue.setdefault("pending", []).insert(0, t)
                    modified = True
                    actions.append(
                        {
                            "action": "released_stale_task",
                            "task_id": task_id,
                            "from": source,
                        }
                    )

    # Reconcile blocked tasks whose PR has already resolved on GitHub. When the
    # deliverable gate holds a daemon PR and a human then merges it, nothing
    # moves the task out of the blocked lane: the sweep above skips anything
    # carrying a blocked_reason (treating it as a human-set block), and the
    # judge writes exactly such a reason. The task then sits blocked forever
    # while heal reports a healthy queue. get_pr_status returns None when the
    # lookup fails, True for MERGED, and False for OPEN or CLOSED-unmerged —
    # a second lookup (_pr_auto_closed_by_daemon) distinguishes OPEN
    # (skip — still unresolved) from CLOSED, and CLOSED from who closed it:
    # the daemon auto-closing a PR (stale merge-conflict cleanup) doesn't mean
    # the work is unwanted, so that case is requeued, while a human closing it
    # via GitHub is a genuine decline/supersede and is terminal.
    for t in list(queue.get("blocked", [])):
        pr_url = t.get("pr_url")
        if not pr_url:
            continue
        is_merged, pr_number = get_pr_status(pr_url)
        task_id = t.get("task_id", "?")
        if is_merged is True:
            # Same JSON-notes extraction operation_loop._update_roadmap_on_completion
            # and autonomy_metrics's reconcile pass use — returns None (never raises)
            # on absent or malformed notes.
            roadmap_task_id = _extract_roadmap_task_id(t)
            finding = {
                "type": "merged_pr_still_blocked",
                "severity": "high",
                "task_id": task_id,
                "title": t.get("title", "")[:60],
                "reason": f"PR #{pr_number} merged but task still blocked",
            }
            if roadmap_task_id:
                finding["roadmap_task_id"] = roadmap_task_id
            findings.append(finding)
            if not dry_run:
                queue["blocked"].remove(t)
                t["status"] = "completed"
                t["completed_at"] = now.isoformat()
                # Keep the judge's verdict for audit, but drop the active-sounding
                # field — a completed task reading as blocked is the exact
                # ambiguity this rule exists to remove.
                if t.get("blocked_reason"):
                    t["previous_blocked_reason"] = t["blocked_reason"]
                t["blocked_reason"] = None
                if not isinstance(t.get("notes"), list):
                    t["notes"] = []
                t["notes"].append(
                    {
                        "timestamp": now.isoformat(),
                        "content": f"queue heal: reconciled after PR #{pr_number} merged",
                    }
                )
                queue.setdefault("completed", []).append(t)
                modified = True
                action = {
                    "action": "completed_merged_pr_task",
                    "task_id": task_id,
                    "pr": pr_number,
                }
                if roadmap_task_id:
                    action["roadmap_task_id"] = roadmap_task_id
                    roadmap_completions.append(roadmap_task_id)
                actions.append(action)
        elif is_merged is False:
            closed_by_daemon = _pr_auto_closed_by_daemon(pr_number)
            if closed_by_daemon is None:
                continue  # PR not CLOSED, or lookup failed — leave alone, fail-safe
            if closed_by_daemon:
                # Daemon closed it (e.g. stale merge-conflict cleanup) — but
                # that only makes the work wanted again when the ONLY thing
                # holding the task was the deliverable gate on that very PR
                # (work_allocator.is_held_for_review: the "Deliverable gate:"
                # reason written together with result == "blocked"). Any other
                # hold outlives the PR and must keep the task blocked: the
                # build ceiling ("Human review required before another
                # attempt" — task fd44a2 / PR #445 was requeued straight past
                # its 5-build cap by the first cut of this rule), exhausted
                # retries, a human-written reason. Unmet dependencies likewise
                # aren't resolved by a PR closure (same rule as the stale-task
                # sweep earlier in this function). Those stay blocked and are
                # only reported.
                if not is_held_for_review(t):
                    hold = str(t.get("blocked_reason") or "")[:80]
                    left_why = f"not a deliverable-gate hold: {hold!r}"
                elif t.get("dependencies"):
                    left_why = f"unmet dependencies {t.get('dependencies')}"
                else:
                    left_why = None
                if left_why:
                    findings.append(
                        {
                            "type": "auto_closed_pr_left_blocked",
                            "severity": "low",
                            "task_id": task_id,
                            "title": t.get("title", "")[:60],
                            "reason": (
                                f"PR #{pr_number} auto-closed by daemon; "
                                f"task left blocked ({left_why})"
                            ),
                        }
                    )
                    continue
                # Only the gate held it: mirror pr_open_closed in
                # autonomy_metrics.py and reset to pending for a fresh attempt.
                findings.append(
                    {
                        "type": "auto_closed_pr_blocked",
                        "severity": "medium",
                        "task_id": task_id,
                        "title": t.get("title", "")[:60],
                        "reason": f"PR #{pr_number} auto-closed by daemon but task still blocked",
                    }
                )
                if not dry_run:
                    queue["blocked"].remove(t)
                    if t.get("blocked_reason"):
                        t["previous_blocked_reason"] = t["blocked_reason"]
                    t["status"] = "pending"
                    t["blocked_reason"] = None
                    t.pop("retry_history", None)
                    t.pop("pr_url", None)
                    t.pop("pr_open_at", None)
                    # Same reset the stale-task sweep applies on release: a
                    # requeued task must not carry its previous worker's
                    # ownership, nor the verdict and hold timestamps of the
                    # hold it just left — the next claim writes fresh ones,
                    # and is_held_for_review keys on result == "blocked".
                    for key in (
                        "assigned_to",
                        "assigned_at",
                        "started_at",
                        "claimed_by",
                        "claimed_at",
                    ):
                        t[key] = None
                    for key in ("result", "blocked_at", "ceiling_blocked_at"):
                        t.pop(key, None)
                    t["reconciled_at"] = now.isoformat()
                    t["reconciled_reason"] = f"daemon auto-closed PR #{pr_number}"
                    if not isinstance(t.get("notes"), list):
                        t["notes"] = []
                    t["notes"].append(
                        {
                            "timestamp": now.isoformat(),
                            "content": (
                                "queue heal: requeued to pending after daemon "
                                f"auto-closed PR #{pr_number}"
                            ),
                        }
                    )
                    queue.setdefault("pending", []).insert(0, t)
                    modified = True
                    actions.append(
                        {
                            "action": "requeued_auto_closed_task",
                            "task_id": task_id,
                            "pr": pr_number,
                        }
                    )
            else:
                # Human closed it via GitHub — declined or superseded, not a
                # bookkeeping gap. Terminal, but NOT "completed": that status is
                # exactly what the autonomy metrics gate on as "shipped," and
                # this work never shipped. Reuse RECONCILED_STATUS so this joins
                # the same "closed_unmerged" vocabulary the completed-lane
                # hygiene pass and pr_open-lane reconcile already use. Name
                # the closer when the timeline says who it was.
                closed_by = _pr_closer_login(pr_number) or "human"
                findings.append(
                    {
                        "type": "human_closed_pr_blocked",
                        "severity": "medium",
                        "task_id": task_id,
                        "title": t.get("title", "")[:60],
                        "reason": (
                            f"PR #{pr_number} closed unmerged ({closed_by}) "
                            "but task still blocked"
                        ),
                    }
                )
                if not dry_run:
                    queue["blocked"].remove(t)
                    t["status"] = RECONCILED_STATUS
                    t["completed_at"] = now.isoformat()
                    t["reconciled_at"] = now.isoformat()
                    t["reconciled_reason"] = (
                        f"PR #{pr_number} closed unmerged ({closed_by})"
                    )
                    if t.get("blocked_reason"):
                        t["previous_blocked_reason"] = t["blocked_reason"]
                    t["blocked_reason"] = None
                    if not isinstance(t.get("notes"), list):
                        t["notes"] = []
                    t["notes"].append(
                        {
                            "timestamp": now.isoformat(),
                            "content": (
                                f"queue heal: reconciled to {RECONCILED_STATUS} "
                                f"after PR #{pr_number} closed unmerged ({closed_by})"
                            ),
                        }
                    )
                    queue.setdefault("completed", []).append(t)
                    modified = True
                    actions.append(
                        {
                            "action": "reconciled_closed_unmerged_task",
                            "task_id": task_id,
                            "pr": pr_number,
                        }
                    )

    seen_ids = {}
    for q_name in ["pending", "in_progress", "blocked"]:
        for t in list(queue.get(q_name, [])):
            tid = t.get("task_id")
            if not tid:
                continue
            if tid in seen_ids:
                findings.append(
                    {
                        "type": "duplicate_task",
                        "severity": "medium",
                        "task_id": tid,
                        "title": t.get("title", "")[:60],
                        "reason": f"in both {seen_ids[tid]} and {q_name}",
                    }
                )
                if not dry_run:
                    queue[q_name].remove(t)
                    modified = True
                    actions.append(
                        {"action": "removed_duplicate", "task_id": tid, "from": q_name}
                    )
            else:
                seen_ids[tid] = q_name

    for t in list(queue.get("pending", [])):
        bo = t.get("backoff_until")
        if not bo:
            continue
        try:
            if datetime.fromisoformat(bo.replace("Z", "+00:00")) < now:
                findings.append(
                    {
                        "type": "expired_backoff",
                        "severity": "low",
                        "task_id": t.get("task_id", "?"),
                        "title": t.get("title", "")[:60],
                        "reason": f"backoff expired {relative_time(bo)}",
                    }
                )
                if not dry_run:
                    t.pop("backoff_until", None)
                    modified = True
                    actions.append(
                        {"action": "cleared_backoff", "task_id": t.get("task_id")}
                    )
        except (ValueError, TypeError):
            pass

    # P2a: Stale pending detection (report-only)
    PENDING_STALE_HOURS = 72  # 3 days

    for t in queue.get("pending", []):
        created = t.get("created_at")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600
            if age_h > PENDING_STALE_HOURS:
                findings.append(
                    {
                        "type": "stale_pending",
                        "severity": "medium",
                        "task_id": t.get("task_id", "?"),
                        "title": t.get("title", "")[:60],
                        "reason": f"pending for {age_h:.0f}h, check dependencies/routing",
                    }
                )
        except (ValueError, TypeError):
            pass

    # P2b: Archive failed tasks older than 48h
    FAILED_ARCHIVE_HOURS = 48

    for t in list(queue.get("failed", [])):
        failed_at = t.get("failed_at") or t.get("completed_at")
        if not failed_at:
            continue
        try:
            dt = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600
            if age_h > FAILED_ARCHIVE_HOURS:
                findings.append(
                    {
                        "type": "stale_failed",
                        "severity": "low",
                        "task_id": t.get("task_id", "?"),
                        "title": t.get("title", "")[:60],
                        "reason": f"failed {age_h:.0f}h ago, archiving",
                    }
                )
                if not dry_run:
                    queue["failed"].remove(t)
                    t["archived_at"] = now.isoformat()
                    t["archive_reason"] = "stale_failed"
                    queue.setdefault("archived", []).append(t)
                    # Cap archived list to prevent unbounded growth
                    MAX_ARCHIVED = 200
                    archived = queue.get("archived", [])
                    if len(archived) > MAX_ARCHIVED:
                        queue["archived"] = archived[-MAX_ARCHIVED:]
                    modified = True
                    actions.append(
                        {"action": "archived_failed", "task_id": t.get("task_id")}
                    )
        except (ValueError, TypeError):
            pass

    # P3a: Broken dependency detection (report-only)
    # A cancelled dependency never completes: report it like a failed one.
    failed_ids = {
        t.get("task_id") for qn in ("failed", "cancelled") for t in queue.get(qn, [])
    }
    all_known = set()
    for qn in ["pending", "in_progress", "blocked", "completed", "failed", "cancelled"]:
        for t in queue.get(qn, []):
            tid = t.get("task_id")
            if tid:
                all_known.add(tid)

    for t in queue.get("pending", []):
        deps = t.get("dependencies", [])
        if not deps:
            continue
        broken = [d for d in deps if d in failed_ids]
        missing = [d for d in deps if d not in all_known]
        if broken or missing:
            findings.append(
                {
                    "type": "broken_dependency",
                    "severity": "high",
                    "task_id": t.get("task_id", "?"),
                    "title": t.get("title", "")[:60],
                    "reason": f"deps broken={broken} missing={missing}",
                }
            )

    # P3b: Backoff cap (auto-fix excessive backoff)
    MAX_BACKOFF_HOURS = 24

    for t in queue.get("pending", []):
        bo = t.get("backoff_until")
        if not bo:
            continue
        try:
            bo_dt = datetime.fromisoformat(bo.replace("Z", "+00:00"))
            remaining_h = (bo_dt - now).total_seconds() / 3600
            if remaining_h > MAX_BACKOFF_HOURS:
                findings.append(
                    {
                        "type": "excessive_backoff",
                        "severity": "medium",
                        "task_id": t.get("task_id", "?"),
                        "title": t.get("title", "")[:60],
                        "reason": f"backoff {remaining_h:.0f}h in future, capping to {MAX_BACKOFF_HOURS}h",
                    }
                )
                if not dry_run:
                    t["backoff_until"] = (
                        now + timedelta(hours=MAX_BACKOFF_HOURS)
                    ).isoformat()
                    modified = True
                    actions.append(
                        {"action": "capped_backoff", "task_id": t.get("task_id")}
                    )
        except (ValueError, TypeError):
            pass

    esc_dir = company_dir / "escalations"
    if esc_dir.is_dir():
        for fp in sorted(esc_dir.glob("*.json")):
            esc = load_json(fp)
            if not esc or esc.get("status") == "resolved":
                continue
            created = esc.get("created_at") or esc.get("escalated_at")
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_h = (now - dt).total_seconds() / 3600
                    if age_h > ESC_STALE_HOURS:
                        findings.append(
                            {
                                "type": "stale_escalation",
                                "severity": "medium",
                                "task_id": esc.get("task_id", fp.stem),
                                "title": (esc.get("title") or esc.get("reason", ""))[
                                    :60
                                ],
                                "reason": f"unresolved for {age_h:.0f}h",
                                # Detect-only, deliberately. Auto-archiving an
                                # unread page is how a same-day fix became an
                                # 11-day stall, so heal reports these and
                                # leaves them for `escalation.py resolve`.
                                "auto_fixable": False,
                                "hint": (
                                    "resolve with: escalation.py resolve "
                                    f"--task-id {esc.get('task_id', fp.stem)} "
                                    "--resolution <why>"
                                ),
                            }
                        )
                except (ValueError, TypeError):
                    pass

    pending = queue.get("pending", [])
    fixed_inv = False
    for i, t in enumerate(pending):
        if t.get("priority") == 1 and not fixed_inv:
            for j in range(i):
                if pending[j].get("priority", 5) > 1:
                    findings.append(
                        {
                            "type": "priority_inversion",
                            "severity": "low",
                            "task_id": t.get("task_id", "?"),
                            "title": t.get("title", "")[:60],
                            "reason": f"P1 behind P{pending[j].get('priority')} at pos {j}",
                        }
                    )
                    if not dry_run:
                        queue["pending"].remove(t)
                        queue["pending"].insert(0, t)
                        modified = True
                        fixed_inv = True
                        actions.append(
                            {"action": "fixed_priority", "task_id": t.get("task_id")}
                        )
                    break

    if modified and not dry_run:
        save_queue_atomic(queue, queue_path)

    # Propagate roadmap completions discovered above, now that work_queue.json
    # is durable — roadmap_state.json/ROADMAP.md have their own I/O, unrelated
    # to the queue write, and mirror the non-fatal try/except both
    # operation_loop._update_roadmap_on_completion and autonomy_metrics's
    # reconcile pass use: a task without roadmap notes is unaffected, and a
    # bad roadmap_state.json write must never turn a successful heal into a
    # failed one.
    for roadmap_task_id in roadmap_completions:
        try:
            import roadmap_scheduler as rs  # local import: avoids a hard load-time coupling

            rs.mark_task_completed(roadmap_task_id)
            rs.update_roadmap_task_status(roadmap_task_id, "complete")
        except Exception:
            pass  # Non-fatal — best-effort write-back

    # The pr_open lane: the reconcile takes its own lock, so it runs after the save above.
    pr_findings, pr_actions, pr_reconciled = _reconcile_pr_open_lane(
        company_dir, dry_run
    )
    findings += pr_findings
    actions += pr_actions
    if pr_reconciled and not dry_run:
        modified = True

    # Merge P4 fuzzy dedup findings (ran at start of function)
    findings = fuzzy_findings + findings
    actions = fuzzy_actions + actions

    return {
        "timestamp": now.isoformat(),
        "dry_run": dry_run,
        "findings": findings,
        "actions": actions,
        "queue_modified": modified,
    }


def render_heal_report(result):
    lines = []
    w = 78
    findings = result.get("findings", [])
    actions = result.get("actions", [])
    # The banner reports what happened, not merely which flag was passed. It
    # used to read "HEALED" whenever dry_run was false, so a run that detected
    # findings and acted on NONE of them announced itself as a repair. That is
    # the failure shape this tool exists to catch: a control whose success
    # message is independent of whether it did anything.
    if result.get("dry_run"):
        mode = "DRY RUN"
    elif actions:
        mode = "HEALED"
    else:
        mode = "NO ACTION TAKEN"
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append(f"\033[1m  QUEUE HEALTH CHECK \u2014 {mode}\033[0m")
    lines.append(f"  {result.get('timestamp', '')[:19]}")
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("")
    if not findings:
        lines.append("  \033[32mQueue is healthy \u2014 no issues found.\033[0m")
        lines.append("")
        return "\n".join(lines)
    sev_color = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[2m"}
    sev_icon = {"high": "!!", "medium": "! ", "low": "~ "}
    by_sev = {}
    for f in findings:
        by_sev.setdefault(f.get("severity", "low"), []).append(f)
    for sev in ["high", "medium", "low"]:
        items = by_sev.get(sev, [])
        if not items:
            continue
        c = sev_color[sev]
        lines.append(f"  {c}{sev.upper()} ({len(items)})\033[0m")
        for f in items:
            lines.append(
                f"  {c}{sev_icon[sev]}\033[0m [{f.get('task_id', '?')[-8:]}] {trunc(f.get('title', ''), 45)}"
            )
            lines.append(
                f"     {c}{f['type'].replace('_', ' ')}: {f.get('reason', '')}\033[0m"
            )
        lines.append("")
    if actions:
        lines.append(f"\033[1;32m  ACTIONS TAKEN ({len(actions)})\033[0m")
        for a in actions:
            lines.append(
                f"  \033[32m+\033[0m [{a.get('task_id', '?')[-8:]}] {a.get('action', '?').replace('_', ' ')}"
            )
        lines.append("")
    if result.get("dry_run"):
        lines.append(
            f"  \033[33mDry run \u2014 {len(findings)} issues. Run --heal without --dry-run to fix.\033[0m"
        )
        lines.append("")
    else:
        # Findings heal cannot repair must be named, not omitted. Silence here
        # reads as "handled" against a HEALED banner, which is how stale
        # escalations sat unresolved for 61h while heal reported success.
        actioned = {a.get("task_id") for a in actions if a.get("task_id")}
        unfixed = [f for f in findings if f.get("task_id") not in actioned]
        if unfixed:
            lines.append(
                f"\033[1;33m  NOT AUTO-FIXED ({len(unfixed)}) \u2014 needs a human\033[0m"
            )
            for f in unfixed:
                lines.append(
                    f"  \033[33m\u2192\033[0m [{f.get('task_id', '?')[-8:]}] {f['type'].replace('_', ' ')}"
                )
            lines.append("")
    lines.append("\033[2m" + "-" * w + "\033[0m")
    return "\n".join(lines)


def render_json(company_dir, discover_external=False):
    queue = load_json(get_queue_path(company_dir)) or {}
    completed = queue.get("completed", [])
    stamped = sorted(
        [t for t in completed if t.get("completed_at")],
        key=lambda t: t.get("completed_at", ""),
        reverse=True,
    )
    unstamped_json = [t for t in completed if not t.get("completed_at")]
    recent = (stamped + unstamped_json)[:10]
    esc_dir = company_dir / "escalations"
    esc_count = sum(
        1
        for fp in (esc_dir.glob("*.json") if esc_dir.is_dir() else [])
        if (load_json(fp) or {}).get("status") != "resolved"
    )
    ext_rows = external_rows(company_dir, discover_external=discover_external)
    return json.dumps(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pending": len(queue.get("pending", [])),
            "in_progress": len(queue.get("in_progress", [])),
            "completed_total": len(completed),
            "cancelled_total": len(queue.get("cancelled", [])),
            "blocked": len(queue.get("blocked", [])),
            "in_progress_tasks": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "priority": t.get("priority"),
                    "assigned_to": t.get("assigned_to"),
                }
                for t in queue.get("in_progress", [])
            ],
            "pending_tasks": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "priority": t.get("priority"),
                }
                for t in queue.get("pending", [])
            ],
            "recent_completions": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "completed_at": t.get("completed_at"),
                    "assigned_to": t.get("assigned_to"),
                }
                for t in recent
            ],
            "escalations": esc_count,
            "dependency_waits": dependency_waits(company_dir, queue),
            "external": {
                "count": len(ext_rows),
                "items": [
                    {
                        "id": r["id"],
                        "owner": r["owner"],
                        "started_at": r["age_source"],
                        "pr": r["pr"],
                        "title": r["title"],
                        "source": r["source"],
                    }
                    for r in ext_rows
                ],
            },
        },
        indent=2,
    )


def run_task_command(args: list[str]) -> int:
    """cancel / hold / unhold <id> — see task_cancel.py."""
    import task_cancel

    verb, rest = args[0], args[1:]
    known = {"--reason", "--keep-pr", "--dry-run", "-n", "--company-dir", "--by"}
    with_value = {"--reason", "--company-dir", "--by"}
    opts: dict[str, object] = {}
    positional: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("-"):
            if a not in known:
                print(f"gatesmith-queue {verb}: unknown option {a}", file=sys.stderr)
                return 2
            if a in with_value:
                if i + 1 >= len(rest):
                    print(f"gatesmith-queue {verb}: {a} needs a value", file=sys.stderr)
                    return 2
                opts[a] = rest[i + 1]
                i += 2
                continue
            opts[a] = True
        else:
            positional.append(a)
        i += 1
    if len(positional) != 1:
        print(f"gatesmith-queue {verb}: give exactly one task id", file=sys.stderr)
        return 2
    company_dir = (
        Path(str(opts["--company-dir"])).expanduser()
        if "--company-dir" in opts
        else find_company_dir()
    )
    dry = bool(opts.get("--dry-run") or opts.get("-n"))
    reason = str(opts.get("--reason") or "")
    if verb == "cancel":
        result = task_cancel.cancel_task(
            company_dir,
            positional[0],
            reason,
            by=str(opts["--by"]) if "--by" in opts else None,
            keep_pr=bool(opts.get("--keep-pr")),
            dry_run=dry,
        )
    elif verb == "hold":
        if not reason.strip():
            print("gatesmith-queue hold: a --reason is required", file=sys.stderr)
            return 2
        result = task_cancel.hold_task(company_dir, positional[0], reason, dry_run=dry)
    else:
        result = task_cancel.release_hold(company_dir, positional[0], dry_run=dry)
    print(task_cancel.render(result, verb))
    return 0 if result.get("ok") else 1


def run_external_command(args: list[str]) -> int:
    """external add/update/done/list — session-agent and manual work outside
    the daemon queue (see external_work.py)."""
    if not args:
        print(
            "gatesmith-queue external: give a subcommand (add, update, done, list)",
            file=sys.stderr,
        )
        return 2
    verb, rest = args[0], args[1:]
    known = {
        "--branch",
        "--pr",
        "--owner",
        "--note",
        "--status",
        "--company-dir",
        "--all",
    }
    with_value = {"--branch", "--pr", "--owner", "--note", "--status", "--company-dir"}
    opts: dict[str, str] = {}
    flags: set[str] = set()
    positional: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("-"):
            if a not in known:
                print(
                    f"gatesmith-queue external {verb}: unknown option {a}", file=sys.stderr
                )
                return 2
            if a in with_value:
                if i + 1 >= len(rest):
                    print(
                        f"gatesmith-queue external {verb}: {a} needs a value",
                        file=sys.stderr,
                    )
                    return 2
                opts[a] = rest[i + 1]
                i += 2
                continue
            flags.add(a)
            i += 1
            continue
        positional.append(a)
        i += 1

    company_dir = (
        Path(opts["--company-dir"]).expanduser()
        if "--company-dir" in opts
        else find_company_dir()
    )

    if verb == "add":
        if len(positional) != 1:
            print("gatesmith-queue external add: give exactly one title", file=sys.stderr)
            return 2
        try:
            item = external_work.add_item(
                company_dir,
                positional[0],
                owner=opts.get("--owner", "claude-session"),
                branch=opts.get("--branch"),
                pr=opts.get("--pr"),
                note=opts.get("--note"),
            )
        except ValueError as e:
            print(f"gatesmith-queue external add: {e}", file=sys.stderr)
            return 2
        print(item["id"])
        return 0

    if verb == "update":
        if len(positional) != 1:
            print("gatesmith-queue external update: give exactly one id", file=sys.stderr)
            return 2
        try:
            result = external_work.update_item(
                company_dir,
                positional[0],
                status=opts.get("--status"),
                pr=opts.get("--pr"),
                branch=opts.get("--branch"),
                note=opts.get("--note"),
            )
        except ValueError as e:
            print(f"gatesmith-queue external update: {e}", file=sys.stderr)
            return 2
        if not result.get("ok"):
            print(
                f"gatesmith-queue external update: {result.get('error')}", file=sys.stderr
            )
            return 1
        print(result["item"]["id"])
        return 0

    if verb == "done":
        if len(positional) != 1:
            print("gatesmith-queue external done: give exactly one id", file=sys.stderr)
            return 2
        result = external_work.mark_done(
            company_dir, positional[0], note=opts.get("--note")
        )
        if not result.get("ok"):
            print(f"gatesmith-queue external done: {result.get('error')}", file=sys.stderr)
            return 1
        print(result["item"]["id"])
        return 0

    if verb == "list":
        items = external_work.list_items(company_dir, include_old="--all" in flags)
        if not items:
            print("(no external work registered)")
            return 0
        for it in items:
            pr = f"#{it['pr']}" if it.get("pr") else "-"
            print(
                f"{short_task_id(it.get('id')):<8} {str(it.get('status', '?')):<10} "
                f"{relative_time(it.get('started_at')):<12} {pr:<6} "
                f"{str(it.get('owner') or '-'):<16} {it.get('title', '')}"
            )
        return 0

    print(f"gatesmith-queue external: unknown subcommand {verb!r}", file=sys.stderr)
    return 2


def main():
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        sys.exit(0)

    if args and args[0] in ("cancel", "hold", "unhold"):
        sys.exit(run_task_command(args))

    if args and args[0] == "external":
        sys.exit(run_external_command(args[1:]))

    known_flags = {
        "--json",
        "--heal",
        "--dry-run",
        "--watch",
        "--prs",
        "--completions",
        "--wide",
        "--company-dir",
        "-h",
        "--help",
    }
    for a in args:
        if a.startswith("-") and a not in known_flags:
            print(f"Unknown option: {a}", file=sys.stderr)
            print(__doc__.strip(), file=sys.stderr)
            sys.exit(2)

    company_dir = find_company_dir()
    if "--company-dir" in args:
        idx = args.index("--company-dir")
        if idx + 1 >= len(args):
            print("--company-dir needs a path", file=sys.stderr)
            sys.exit(2)
        company_dir = Path(args[idx + 1]).expanduser()
    wide = "--wide" in args

    completions_n = 10
    if "--completions" in args:
        idx = args.index("--completions")
        if idx + 1 < len(args):
            try:
                completions_n = int(args[idx + 1])
            except ValueError:
                pass

    if "--prs" in args:
        print(render_pr_list(company_dir, wide=wide))
    elif "--json" in args:
        print(render_json(company_dir, discover_external=True))
    elif "--heal" in args:
        result = heal_queue(company_dir, dry_run="--dry-run" in args)
        print(render_heal_report(result))
        if not sys.stdout.isatty():
            print(json.dumps(result, indent=2))
        sys.exit(0 if not result["findings"] else 1)
    elif "--watch" in args:
        interval = 5
        for i, a in enumerate(args):
            if a == "--watch" and i + 1 < len(args):
                try:
                    interval = int(args[i + 1])
                except ValueError:
                    pass
        try:
            while True:
                os.system("clear")
                print(
                    render_dashboard(
                        company_dir,
                        completions_n=completions_n,
                        wide=wide,
                        discover_external=True,
                    )
                )
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print(
            render_dashboard(
                company_dir,
                completions_n=completions_n,
                wide=wide,
                discover_external=True,
            )
        )


if __name__ == "__main__":
    main()
