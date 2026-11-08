#!/usr/bin/env python3
"""Cancel or hold a queued task — the operator's durable stop (QUEUE-CANCEL-STATUS).

Before this module the only stops were 'blocked' (which the stranded sweep,
heal and failure recovery release and re-run) and 'completed' (which lies in
every success metric). On 2026-10-03 one webapp repair task opened three PRs
after an operator had killed its worker by hand and parked it as blocked.

``cancel_task`` moves a task to the terminal ``cancelled`` lane (see
work_allocator.CANCELLED_LANE for what that lane is excluded from), then:

* stops its worker: the process group recorded in worker_pids.json, but only
  after checking the group is that task's session — its leader's cwd is
  inside the recorded worktree, the worktree is named for the task, no daemon
  process is in it — never an unrelated group, never our own;
* closes its PR with a comment when it is open (``keep_pr`` leaves it).

The queue move comes FIRST, so a worker that dies (or finishes) afterwards
finds its task cancelled and operation_loop.release_task leaves it there.

``hold_task`` / ``release_hold`` set and clear ``operator_hold`` on a blocked
task. ``pr_close_attribution`` tells a daemon close from a human one for the
pr_open reconcile (autonomy_metrics).

Every subprocess and signal call is injectable for tests.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import work_allocator as wa
except ImportError:  # script / sys.path usage
    import work_allocator as wa  # type: ignore[no-redef]

Runner = Callable[..., Any]

# Every daemon-initiated `gh pr close` leaves a close comment starting with
# one of these (see queue_monitor._DAEMON_CLOSE_PREFIXES, which a test pins
# against forge_daemon.py). Our own cancel comment must never start with them.
DAEMON_CLOSE_PREFIXES = ("Auto-closed", "Auto-closing")
CANCEL_COMMENT_PREFIX = "Cancelled via gatesmith-queue cancel"

KILL_GRACE_SECONDS = 5.0
# Slack between a worker's launch and its worker_pids.json entry.
IDENTITY_SLACK_SECONDS = 60.0

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")

_PR_CLOSER_QUERY = (
    "query($owner:String!,$name:String!,$n:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$n){"
    "timelineItems(last:1,itemTypes:CLOSED_EVENT){nodes{"
    "... on ClosedEvent{actor{login}}}}}}}"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_actor() -> str:
    try:
        return f"operator ({getpass.getuser()})"
    except Exception:
        return "operator"


def queue_paths(company_dir: Path) -> tuple[Path, Path]:
    company_dir = Path(company_dir)
    queue_path = company_dir / "state" / "work_queue.json"
    if not queue_path.exists() and (company_dir / "work_queue.json").exists():
        queue_path = company_dir / "work_queue.json"
    return queue_path, company_dir / "runtime" / "queue.lock"


def _load(queue_path: Path) -> dict:
    with open(queue_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# PR helpers
# ---------------------------------------------------------------------------


def _gh_json(args: list[str], runner: Runner, timeout: int = 20) -> Any:
    try:
        res = runner(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(res, "returncode", 1) != 0 or not (res.stdout or "").strip():
        return None
    try:
        return json.loads(res.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def pr_state(pr_url: str, runner: Runner = subprocess.run) -> str | None:
    data = _gh_json(["gh", "pr", "view", pr_url, "--json", "state"], runner)
    if isinstance(data, dict) and isinstance(data.get("state"), str):
        return data["state"].upper()
    return None


def pr_closer_login(pr_url: str, runner: Runner = subprocess.run) -> str | None:
    """Login on the PR's last CLOSED_EVENT, or None (best-effort)."""
    m = _PR_URL_RE.search(pr_url or "")
    if not m:
        return None
    owner, name, number = m.groups()
    data = _gh_json(
        [
            "gh",
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"n={int(number)}",
            "-f",
            f"query={_PR_CLOSER_QUERY}",
        ],
        runner,
    )
    try:
        nodes = data["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
        login = (nodes[0].get("actor") or {}).get("login") if nodes else None
    except (KeyError, TypeError, IndexError, AttributeError):
        return None
    return login.strip() if isinstance(login, str) and login.strip() else None


def pr_close_attribution(
    pr_url: str, runner: Runner = subprocess.run
) -> dict[str, Any] | None:
    """Who closed a CLOSED PR: ``{"closed_by_daemon": bool, "actor": str|None}``.

    The daemon and the operator usually share one GitHub identity, so the
    timeline actor cannot tell them apart. The daemon's own closes are
    recognised by what it records on every one: a close comment starting
    with DAEMON_CLOSE_PREFIXES. No such comment means a human closed it; the
    actor login only names them. None when the PR is not CLOSED or gh cannot
    say — callers leave the task alone and look again next pass.
    """
    data = _gh_json(["gh", "pr", "view", pr_url, "--json", "state,comments"], runner)
    if not isinstance(data, dict) or str(data.get("state") or "").upper() != "CLOSED":
        return None
    by_daemon = any(
        isinstance(c, dict)
        and str(c.get("body") or "").startswith(DAEMON_CLOSE_PREFIXES)
        for c in data.get("comments") or []
    )
    actor = None if by_daemon else pr_closer_login(pr_url, runner)
    return {"closed_by_daemon": by_daemon, "actor": actor}


def close_pr(pr_url: str, comment: str, runner: Runner = subprocess.run) -> bool:
    try:
        res = runner(
            ["gh", "pr", "close", pr_url, "--comment", comment],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(res, "returncode", 1) == 0


def close_late_prs(task: dict, runner: Runner = subprocess.run) -> list[str]:
    """Close every still-OPEN PR a worker reported after its task was cancelled.

    A worker the cancel could not stop (no verified group, or one that
    finished between the queue move and the kill) can still open a PR; it
    lands only in the cancelled task's ``late_outcomes``, and CI may arm
    auto-merge on it. Nothing else would ever close it. ``cancel_keep_pr``
    (``gatesmith-queue cancel --keep-pr``) leaves them open. Best-effort; returns
    the URLs it closed.
    """
    if not isinstance(task, dict) or task.get("cancel_keep_pr") is True:
        return []
    urls: list[str] = []
    for o in task.get("late_outcomes") or []:
        url = o.get("pr_url") if isinstance(o, dict) else None
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    closed: list[str] = []
    for url in urls:
        if pr_state(url, runner) != "OPEN":
            continue
        comment = (
            f"{CANCEL_COMMENT_PREFIX} by {task.get('cancelled_by') or 'operator'}: "
            f"{task.get('cancel_reason') or ''} (PR opened by the worker after "
            "the task was cancelled)"
        )
        if close_pr(url, comment, runner):
            closed.append(url)
    return closed


# ---------------------------------------------------------------------------
# Worker process group
# ---------------------------------------------------------------------------


def _safe_id(task_id: str) -> str:
    # Same sanitising forge_daemon applies when it names a worktree.
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", task_id)


def _parse_etime(etime: str) -> float | None:
    """Seconds from ps's etime, ``[[DD-]HH:]MM:SS`` (as forge_daemon parses it)."""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = [int(x) for x in etime.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts[-3:]
        return float(((days * 24 + h) * 60 + m) * 60 + sec)
    except (ValueError, TypeError):
        return None


def _group_members(pgid: int, runner: Runner) -> dict[int, tuple[float | None, str]]:
    """``{pid: (start_epoch, command)}`` for every live process in group *pgid*."""
    try:
        out = runner(
            ["ps", "-A", "-ww", "-o", "pgid=,pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError, AttributeError):
        return {}
    now = time.time()
    members: dict[int, tuple[float | None, str]] = {}
    for line in (out or "").splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
            if int(fields[0]) == pgid:
                age = _parse_etime(fields[2])
                start = now - age if age is not None else None
                members[int(fields[1])] = (
                    start,
                    fields[3] if len(fields) > 3 else "",
                )
    return members


def _cwds(pids: list[int], runner: Runner) -> dict[int, str]:
    if not pids:
        return {}
    try:
        out = runner(
            ["lsof", "-a", "-p", ",".join(str(p) for p in pids), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError, AttributeError):
        return {}
    cwds: dict[int, str] = {}
    current: int | None = None
    for line in (out or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            cwds[current] = line[1:]
    return cwds


def _inside(path: str, root: str) -> bool:
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def worker_group_for_task(
    company_dir: Path, task_id: str, runner: Runner = subprocess.run
) -> tuple[int | None, str]:
    """The verified process group of *task_id*'s worker, or (None, why).

    The group is only returned when every check holds: a worker_pids.json
    entry for this task; a worktree named for the task; a pgid > 1 that is
    not our own group and holds no daemon process; a live group leader whose
    cwd is inside that worktree.
    """
    pid_file = Path(company_dir) / "state" / "worker_pids.json"
    try:
        records = json.loads(pid_file.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return None, "no worker registry"
    rec = records.get(task_id) if isinstance(records, dict) else None
    if not isinstance(rec, dict):
        return None, "no worker recorded for this task"
    worktree = str(rec.get("worktree") or "")
    # forge_daemon names the worktree "<safe id>-<suffix>": the dash keeps
    # task-…-abc from matching a worktree of task-…-abcd.
    if not worktree or not Path(worktree).name.startswith(_safe_id(task_id) + "-"):
        return None, f"recorded worktree {worktree!r} is not this task's"
    pgid = rec.get("pgid")
    if not isinstance(pgid, int) or isinstance(pgid, bool):
        # Entries written before E1 carry only the pid. Fall back to its group
        # only when the worker leads that group itself: a worker sharing the
        # daemon's group would otherwise make the daemon a target.
        pid = rec.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return None, "entry has neither pgid nor pid"
        try:
            pgid = os.getpgid(pid)
        except OSError:
            return None, "worker no longer running"
        if pgid != pid:
            return None, f"worker {pid} does not lead its own group — left alone"
    if pgid <= 1:
        return None, f"refusing pgid {pgid}"
    try:
        if pgid == os.getpgrp():
            return None, "refusing our own process group"
    except OSError:
        return None, "own process group unknown"
    started_at = rec.get("started_at")
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return None, "entry has no started_at — identity unknown, left alone"
    members = _group_members(pgid, runner)
    if not members:
        return None, "worker no longer running"
    if any("forge_daemon" in cmd for _start, cmd in members.values()):
        return None, f"group {pgid} contains a daemon process — left alone"
    # Identity, part 1: the session was launched just before its entry was
    # written, so a group whose every process started later is a recycled
    # pgid (the same check forge_daemon._registered_group_is_ours makes).
    starts = [st for st, _cmd in members.values() if st is not None]
    if not starts:
        return None, f"start times of group {pgid} unknown — left alone"
    if min(starts) > started_at + IDENTITY_SLACK_SECONDS:
        return None, (
            f"every process in group {pgid} started after the entry "
            f"(+{min(starts) - started_at:.0f}s) — the pgid was reused, left alone"
        )
    cwds = _cwds(list(members), runner)
    if pgid in members:
        # Identity, part 2: the leader is the session the daemon launched in
        # the worktree; a child may cd elsewhere, the leader does not.
        if not _inside(cwds.get(pgid, ""), worktree):
            return None, (
                f"group {pgid}'s leader is not inside {worktree} — not this "
                "task's worker, left alone"
            )
        return pgid, ""
    # Leader (uv) gone, members left: the orphaned claude session of
    # ENGINE-FINDINGS 2026-09-20. Kill only when EVERY member's cwd is known
    # and inside this task's worktree.
    if len(cwds) != len(members) or not all(
        _inside(cwds[pid], worktree) for pid in members
    ):
        return None, (
            f"group {pgid} has no leader and not every member is inside "
            f"{worktree} — left alone"
        )
    return pgid, ""


def kill_group(
    pgid: int,
    *,
    killpg: Callable[[int, int], None] = os.killpg,
    sleeper: Callable[[float], None] = time.sleep,
    grace: float = KILL_GRACE_SECONDS,
) -> bool:
    """SIGTERM the group, wait up to *grace*, SIGKILL survivors. True if signalled."""
    try:
        killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    waited = 0.0
    while waited < grace:
        try:
            killpg(pgid, 0)
        except OSError:
            return True
        sleeper(0.5)
        waited += 0.5
    try:
        killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _resolve(queue: dict, ident: str) -> tuple[str | None, dict | None, str]:
    matches, _how = wa.find_task_by_ident(queue, ident)
    if not matches:
        return None, None, f"no task matches {ident!r}"
    ids = {t.get("task_id") for _, t in matches}
    if len(ids) > 1:
        return None, None, f"{ident!r} is ambiguous: {', '.join(sorted(map(str, ids)))}"
    lane, task = matches[0]
    return lane, task, ""


def cancel_task(
    company_dir: Path,
    ident: str,
    reason: str,
    *,
    by: str | None = None,
    keep_pr: bool = False,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    killpg: Callable[[int, int], None] = os.killpg,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Cancel a task: terminal lane, stop its worker, close its open PR."""
    if not str(reason or "").strip():
        return {"ok": False, "error": "a --reason is required"}
    by = by or default_actor()
    queue_path, lock_path = queue_paths(company_dir)
    if not queue_path.exists():
        return {"ok": False, "error": f"no queue at {queue_path}"}

    queue = _load(queue_path)
    lane, task, err = _resolve(queue, ident)
    if err:
        return {"ok": False, "error": err}
    assert task is not None
    task_id = str(task.get("task_id"))
    if lane == wa.CANCELLED_LANE:
        return {"ok": False, "error": f"{task_id} is already cancelled"}
    if lane not in wa.CANCELLABLE_LANES:
        return {"ok": False, "error": f"{task_id} is {lane}; only active tasks cancel"}

    pgid, why_not = worker_group_for_task(company_dir, task_id, runner)
    pr_url = task.get("pr_url") or ""
    state = pr_state(pr_url, runner) if pr_url else None
    plan: dict[str, Any] = {
        "task_id": task_id,
        "title": task.get("title"),
        "from": lane,
        "reason": reason,
        "by": by,
        "kill_pgid": pgid,
        "kill_skipped": why_not if pgid is None else "",
        "pr_url": pr_url or None,
        "pr_state": state,
        "close_pr": bool(pr_url) and state == "OPEN" and not keep_pr,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, **plan}

    # 1. Queue first: a worker that dies or finishes after this point finds
    #    its task cancelled and release_task leaves it there.
    with wa.QueueLock(lock_path):
        queue = _load(queue_path)
        lane, live, err = _resolve(queue, task_id)
        if err or live is None:
            return {"ok": False, "error": err or f"{task_id} vanished"}
        if lane == wa.CANCELLED_LANE:
            return {"ok": False, "error": f"{task_id} is already cancelled"}
        if lane not in wa.CANCELLABLE_LANES:
            return {
                "ok": False,
                "error": f"{task_id} is {lane}; only active tasks cancel",
            }
        for section in wa.CANCELLABLE_LANES:
            if isinstance(queue.get(section), list):
                queue[section] = [
                    t for t in queue[section] if t.get("task_id") != task_id
                ]
        wa.mark_cancelled(live, reason, by)
        live["cancelled_from"] = lane
        live["cancel_keep_pr"] = bool(keep_pr)
        queue.setdefault(wa.CANCELLED_LANE, []).append(live)
        wa.write_queue_file(queue_path, queue)

    # 2. Worker, re-verified now (the pre-check may be stale).
    killed = False
    if pgid is not None:
        pgid, why_not = worker_group_for_task(company_dir, task_id, runner)
        if pgid is not None:
            killed = kill_group(pgid, killpg=killpg, sleeper=sleeper)

    # 3. PR.
    pr_closed = False
    if plan["close_pr"]:
        pr_closed = close_pr(
            pr_url, f"{CANCEL_COMMENT_PREFIX} by {by}: {reason}", runner
        )

    with wa.QueueLock(lock_path):
        queue = _load(queue_path)
        for t in queue.get(wa.CANCELLED_LANE, []):
            if t.get("task_id") == task_id:
                t["cancel_effects"] = {
                    "worker_killed_pgid": pgid if killed else None,
                    "worker_kill_skipped": "" if killed else why_not,
                    "pr_closed": pr_closed,
                    "pr_kept": bool(pr_url) and keep_pr,
                }
        wa.write_queue_file(queue_path, queue)

    return {
        "ok": True,
        "dry_run": False,
        **plan,
        "worker_killed": killed,
        "kill_skipped": "" if killed else why_not,
        "pr_closed": pr_closed,
    }


def hold_task(
    company_dir: Path, ident: str, reason: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Park a task in blocked with operator_hold; no sweep releases it."""
    queue_path, lock_path = queue_paths(company_dir)
    if not queue_path.exists():
        return {"ok": False, "error": f"no queue at {queue_path}"}
    with wa.QueueLock(lock_path):
        queue = _load(queue_path)
        lane, task, err = _resolve(queue, ident)
        if err or task is None:
            return {"ok": False, "error": err}
        task_id = str(task.get("task_id"))
        if lane not in ("proposed", "pending", "in_progress", "blocked", "failed"):
            return {"ok": False, "error": f"{task_id} is {lane}; cannot hold"}
        result = {
            "ok": True,
            "dry_run": dry_run,
            "task_id": task_id,
            "from": lane,
            "worker_still_running": lane == "in_progress",
        }
        if dry_run:
            return result
        queue[lane] = [t for t in queue[lane] if t.get("task_id") != task_id]
        wa.mark_operator_hold(task, reason)
        queue.setdefault("blocked", []).append(task)
        wa.write_queue_file(queue_path, queue)
    return result


def release_hold(
    company_dir: Path, ident: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Clear an operator hold and return the task to pending."""
    queue_path, lock_path = queue_paths(company_dir)
    if not queue_path.exists():
        return {"ok": False, "error": f"no queue at {queue_path}"}
    with wa.QueueLock(lock_path):
        queue = _load(queue_path)
        lane, task, err = _resolve(queue, ident)
        if err or task is None:
            return {"ok": False, "error": err}
        task_id = str(task.get("task_id"))
        if lane != "blocked" or not wa.is_operator_held(task):
            return {"ok": False, "error": f"{task_id} is not on operator hold"}
        if dry_run:
            return {"ok": True, "dry_run": True, "task_id": task_id}
        queue["blocked"] = [t for t in queue["blocked"] if t.get("task_id") != task_id]
        task.pop("operator_hold", None)
        task["previous_blocked_reason"] = task.get("blocked_reason")
        task["blocked_reason"] = None
        task["status"] = "pending"
        for key in (
            "assigned_to",
            "assigned_at",
            "claimed_by",
            "claimed_at",
            "started_at",
        ):
            task[key] = None
        queue.setdefault("pending", []).insert(0, task)
        wa.write_queue_file(queue_path, queue)
    return {"ok": True, "dry_run": False, "task_id": task_id}


def render(result: dict[str, Any], verb: str) -> str:
    if not result.get("ok"):
        return f"gatesmith-queue {verb}: {result.get('error')}"
    head = "DRY RUN — nothing changed\n" if result.get("dry_run") else ""
    lines = [f"{head}{verb}: {result.get('task_id')} (from {result.get('from', '?')})"]
    if verb == "cancel":
        if result.get("kill_pgid"):
            done = result.get("worker_killed")
            lines.append(
                f"  worker group {result['kill_pgid']}: "
                + (
                    "would be stopped"
                    if result.get("dry_run")
                    else ("stopped" if done else "NOT stopped")
                )
            )
        if result.get("kill_skipped"):
            lines.append(f"  worker: {result['kill_skipped']}")
        if result.get("pr_url"):
            if result.get("close_pr"):
                state = (
                    "would be closed"
                    if result.get("dry_run")
                    else ("closed" if result.get("pr_closed") else "close FAILED")
                )
            else:
                state = f"left {result.get('pr_state') or 'as is'}"
            lines.append(f"  PR {result['pr_url']}: {state}")
    if verb == "hold" and result.get("worker_still_running"):
        lines.append(
            "  note: its worker is still running; the hold keeps the task parked "
            "whatever the worker reports (use cancel to stop it)"
        )
    return "\n".join(lines)
