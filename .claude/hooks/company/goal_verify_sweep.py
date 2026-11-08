"""Verify goals whose PR merged outside the daemon (GOAL-VERIFY-SWEEP).

Why this exists (webapp, 2026-10-19..20)
----------------------------------------
G56's build task (``[QUEUE-FILL] G56: ...``) produced PR #294. The operator
committed fix rounds on top of the daemon's head and merged it by hand. The
goal-credit sweep (goal_crediting.py) found no pre-merge gate verdict for the
merged sha, and G56's metric carries a clause beyond the canonical ledger form
("... and both relays are redeployed ..."), so the ledger fallback did not
apply either: the task was ledgered ``no_passing_verdict``, final. G56 stayed
not_started, and G58 (``dependsOn: G56``) waited eight hours with an empty
queue until ``goal_tracker.py verify --goal G56 --pr 294`` was run by hand.

What it does
------------
:func:`verify_goals_from_merged_prs` runs that same manual step for the daemon.
For every queue task (any lane) titled ``[QUEUE-FILL] Gn: ...`` whose goal is
in vision.md, is a product goal (not Gatesmith's own) and is not verified:

1. **The task's own PR, by ownership.** The PR the queue task records
   (``pr_url``) when it merged. Otherwise (no PR recorded, or it was closed
   unmerged) a merged PR whose TITLE or HEAD BRANCH carries the task id; a
   body mention ("follow-up to task-...") does not make a PR the task's, and
   a revert PR is never a candidate. More than one candidate: a human decides
   (``ambiguous_prs``), the sweep never picks the newest.
2. **Refusals.** The PR merged into a branch other than main/master; the gate
   REJECTed its merged head for this task (the rule goal_crediting applies);
   its merge commit was reverted ("This reverts commit ..."); or a merged
   GitHub revert PR names it (``Revert "<title>"`` / ``Reverts owner/repo#N``,
   read from origin/main's log and from the search results).
3. **Deferral to the oracle.** goal_crediting's record for the task wins when
   it is still in flight (``retry``), was decided by a verdict (``not_complete``,
   ``reverted``) or carries a failing ledger read (``ledger_failing_runs``).
   And whatever that record says (or when there is none, e.g. a blocked-lane
   task): every acceptance entry the goal's metric names
   (``goal_tracker._ledger_named_entry_ids``, also for a metric with extra
   clauses) must read CONSISTENT in the newest eligible main ledger run that
   contains the merge (:func:`check_named_entries`). Failing or no such run
   yet: re-checked later, no judge call, nothing spent from the budget. The
   PR judge never credits a goal the acceptance ledger reads as failing.
4. ``goal_tracker.verify_goal(goal, pr_number=..., pr_url=...)``, the
   operator's verify path: the goal's own assessment first, then the PR judge.

Bounded: per sweep at most :data:`MAX_SEARCHES_PER_SWEEP` gh lookups and
:data:`MAX_VERIFY_CALLS_PER_SWEEP` verify calls; per goal at most
:data:`MAX_JUDGE_CALLS_PER_GOAL` verify calls ever (counted persistently, only
when the verifier runs). Goals another goal ``dependsOn`` go first.
``state/goal_verify_sweep.json`` holds ``tasks`` (outcome per task id, "not
yet" outcomes re-checked with back-off, counters carried across retries) and
``goals`` (the judge budget). It runs in goal_crediting's background thread,
right after the credit sweep; the verify judge runs in deliverable_judge's
own process group, which deliverable_judge kills at interpreter exit.

CLI::

    goal_verify_sweep.py run [--company-dir .company]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LEDGER_FILE = "state/goal_verify_sweep.json"

MAX_SEARCHES_PER_SWEEP = 3
MAX_VERIFY_CALLS_PER_SWEEP = 1
# Verify calls (each may be a judge call of minutes) per goal, ever.
MAX_JUDGE_CALLS_PER_GOAL = 3
# No merged PR yet / gh failed: re-check with back-off.
SEARCH_RECHECK_SECONDS = 3600
SEARCH_RECHECK_MAX_SECONDS = 86400
# verify_goal said not complete: a later ledger run or re-judge may differ.
NOT_COMPLETE_RECHECK_SECONDS = 86400
# Merge commit not (yet) on main per the local refs: re-check, then final.
MAX_GIT_WAITS = 6
GH_TIMEOUT = 30
SEARCH_LIMIT = 20
MAIN_BRANCHES = frozenset({"main", "master"})

_QUEUE_FILL_TITLE_RE = re.compile(r"^\[QUEUE-FILL\]\s+(G\d+):")
_TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9_-]+$")
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")
# goal_crediting outcomes this sweep leaves alone: still in flight there, or a
# verdict (goal judge said no, merge reverted) a second judge must not override.
_CREDIT_DEFERS = frozenset({"retry", "not_complete", "reverted"})
_LANES = ("pending", "in_progress", "pr_open", "blocked", "completed", "failed")
_COUNTERS = ("search_waits", "git_waits", "not_complete", "ledger_waits")
# A named entry not read CONSISTENT yet (no run contains the merge, or the run
# read it failing): re-check with back-off, never a judge call.
LEDGER_RECHECK_SECONDS = 1800
LEDGER_RECHECK_MAX_SECONDS = 14400
_PR_FIELDS = (
    "number,url,title,body,state,mergedAt,headRefOid,headRefName,"
    "baseRefName,mergeCommit"
)

PrSearch = Callable[[str], "list[dict] | None"]
PrView = Callable[[str], "dict | None"]
Verifier = Callable[[str, str, str], dict]


def _import(name: str):
    import importlib

    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    return importlib.import_module(name)


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def load_ledger(company_dir: Path) -> dict[str, dict]:
    """``{"tasks": {task_id: record}, "goals": {goal_id: {"judge_calls"}}}``."""
    try:
        data = json.loads((Path(company_dir) / LEDGER_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    tasks = data.get("tasks")
    goals = data.get("goals")
    return {
        "tasks": tasks if isinstance(tasks, dict) else {},
        "goals": goals if isinstance(goals, dict) else {},
    }


def _save_ledger(company_dir: Path, ledger: dict) -> None:
    path = Path(company_dir) / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# PR ownership and reverts
# ---------------------------------------------------------------------------


def is_revert_pr(pr: dict) -> bool:
    title = str(pr.get("title") or "")
    head = str(pr.get("headRefName") or "")
    return title.startswith(("Revert ", 'Revert "')) or head.startswith("revert-")


def pr_owned_by_task(pr: dict, task_id: str) -> bool:
    """The task id is in the PR's title or head branch, and it is no revert.

    A body mention does not count: a follow-up PR saying "follow-up to
    task-..." is not the task's deliverable.
    """
    if is_revert_pr(pr):
        return False
    return task_id in str(pr.get("title") or "") or task_id in str(
        pr.get("headRefName") or ""
    )


def reverts_pr(candidate: dict, pr: dict) -> bool:
    """Is ``candidate`` a merged GitHub revert of ``pr``?"""
    if not is_revert_pr(candidate) or candidate is pr:
        return False
    if candidate.get("number") == pr.get("number"):
        return False
    number = str(pr.get("number") or "")
    title = str(pr.get("title") or "")
    body = str(candidate.get("body") or "")
    if number and re.search(rf"Reverts\s+\S*#{re.escape(number)}\b", body):
        return True
    return bool(title) and f'Revert "{title}"' in str(candidate.get("title") or "")


def _pr_number(pr_url: Any) -> str | None:
    match = _PR_NUMBER_RE.search(str(pr_url or ""))
    return match.group(1) if match else None


def _gh_json(args: list[str], cwd: Path | None, timeout: int) -> Any:
    try:
        res = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
        )
        return json.loads(res.stdout) if res.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def search_merged_prs_for_task(
    task_id: str, *, cwd: Path | None = None, timeout: int = GH_TIMEOUT
) -> list[dict] | None:
    """Merged PRs gh finds for ``task_id`` (None when gh failed).

    A pre-filter only: ownership (:func:`pr_owned_by_task`) and reverts
    (:func:`reverts_pr`) are decided by the caller on these results.
    """
    if not _TASK_ID_RE.match(task_id or ""):
        return []
    data = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "merged",
            "--search",
            task_id,
            "--limit",
            str(SEARCH_LIMIT),
            "--json",
            _PR_FIELDS,
        ],
        cwd,
        timeout,
    )
    if not isinstance(data, list):
        return None
    return [pr for pr in data if isinstance(pr, dict)]


def view_pr(number: str, *, cwd: Path | None = None, timeout: int = GH_TIMEOUT):
    """``gh pr view`` for the PR the task recorded; None when gh failed."""
    if not str(number).isdigit():
        return None
    data = _gh_json(["pr", "view", str(number), "--json", _PR_FIELDS], cwd, timeout)
    return data if isinstance(data, dict) else None


def check_pr_reverted(pr: dict, project_root: Path) -> str:
    """ "reverted" | "ok" | "unknown": does origin/main carry a GitHub revert
    of ``pr`` (``Reverts owner/repo#N`` or ``Revert "<title>"``)?

    ``goal_crediting.check_merge_commit`` sees only ``git revert``'s "This
    reverts commit <sha>"; a revert merged through GitHub's Revert button
    says neither.
    """
    number = str(pr.get("number") or "")
    title = str(pr.get("title") or "")
    ref = None
    for candidate in ("origin/main", "origin/master", "origin/HEAD"):
        try:
            res = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project_root),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    candidate,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"
        if res.returncode == 0 and res.stdout.strip():
            ref = candidate
            break
    if ref is None:
        return "unknown"
    patterns = []
    if number.isdigit():
        patterns.append(f"Reverts [^ ]*#{number}([^0-9]|$)")
    if title:
        patterns.append('Revert "' + re.escape(title) + '"')
    if not patterns:
        return "unknown"
    args = ["git", "-C", str(project_root), "log", ref, "--format=%H", "-n", "1", "-E"]
    args += [f"--grep={p}" for p in patterns]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return "unknown"
    if res.returncode != 0:
        return "unknown"
    return "reverted" if res.stdout.strip() else "ok"


def check_named_entries(
    company_dir: Path,
    entry_ids: list[str],
    merge_commit: str | None,
    *,
    main_resolver: Callable[[], tuple[str | None, str]],
    is_ancestor: Callable[[Path, str, str], bool] | None = None,
) -> tuple[str, str]:
    """What the acceptance ledger says about ``entry_ids`` after the merge.

    Returns ``("ok" | "failing" | "not_yet", reason)``. The NEWEST eligible
    run decides: a run on main, not flagged environment_error /
    environment_suspect, whose commit is on origin/main (fetched by
    ``main_resolver``) and contains ``merge_commit``, and that judged every
    named entry (a partial run that never reached one is skipped). "ok" when
    it read them all CONSISTENT, "failing" when it did not; "not_yet" when no
    such run exists or anything is unreadable. Never raises.
    """
    gt = _import("goal_tracker")
    is_ancestor = is_ancestor or gt._git_is_ancestor
    if not merge_commit:
        return "not_yet", "the PR's merge commit is unknown"
    project_root = Path(company_dir).parent
    runs_path = Path(company_dir) / "state" / "acceptance" / "runs.jsonl"
    try:
        runs = gt._ledger_run_records(runs_path, gt.LEDGER_CREDIT_MAX_RUNS_SCANNED)
    except Exception as exc:  # noqa: BLE001
        return "not_yet", f"{runs_path} unreadable: {exc}"
    if not runs:
        return "not_yet", "no ledger run recorded yet"
    try:
        main_commit, why = main_resolver()
    except Exception as exc:  # noqa: BLE001
        main_commit, why = None, f"{type(exc).__name__}: {exc}"
    if not main_commit:
        return "not_yet", f"origin/main unavailable: {why}"
    for run in runs:
        commit = run.get("commit")
        if run.get("branch") != "main" or not isinstance(commit, str) or not commit:
            continue
        if gt._ledger_run_environment_flagged(run):
            continue
        if set(entry_ids) & gt._ledger_run_unfinished(run):
            continue
        if not is_ancestor(project_root, commit, main_commit):
            continue
        if not is_ancestor(project_root, merge_commit, commit):
            continue
        passing = run.get("passing_ids")
        passing_ids = set(passing) if isinstance(passing, list) else set()
        missing = [i for i in entry_ids if i not in passing_ids]
        if missing:
            return "failing", (
                f"ledger run at {commit[:8]} on main reads {', '.join(missing)} "
                "not CONSISTENT"
            )
        return (
            "ok",
            f"ledger run at {commit[:8]} reads {', '.join(entry_ids)} CONSISTENT",
        )
    return "not_yet", f"no eligible ledger run on main contains {merge_commit[:8]} yet"


def candidates_from_queue(queue: dict) -> list[tuple[str, dict]]:
    """(goal_id, task) for every ``[QUEUE-FILL] Gn:`` task in any lane."""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for lane in _LANES:
        for task in (queue or {}).get(lane) or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or "")
            match = _QUEUE_FILL_TITLE_RE.match(str(task.get("title") or ""))
            if match and _TASK_ID_RE.match(task_id) and task_id not in seen:
                seen.add(task_id)
                out.append((match.group(1), task))
    return out


def _credit_defers(record: Any) -> bool:
    """goal_crediting's record for the task settles it or is still in flight."""
    if not isinstance(record, dict):
        return False
    if record.get("outcome") in _CREDIT_DEFERS:
        return True
    # The acceptance ledger read the goal's entries failing: the oracle's
    # negative read is never overridden by a PR judge.
    return bool(record.get("ledger_failing_runs"))


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def verify_goals_from_merged_prs(
    company_dir: Path,
    queue: dict,
    *,
    pr_search: PrSearch | None = None,
    pr_view: PrView | None = None,
    verifier: Verifier | None = None,
    git_checker: Callable[[str | None], str] | None = None,
    revert_checker: Callable[[dict], str] | None = None,
    ledger_reader: Callable[[list[str], str | None], tuple[str, str]] | None = None,
    now: datetime | None = None,
    max_searches: int = MAX_SEARCHES_PER_SWEEP,
    max_verify_calls: int = MAX_VERIFY_CALLS_PER_SWEEP,
    window_days: int | None = None,
) -> dict:
    """One sweep (see the module docstring). Never raises.

    Returns ``{"verified": [goal_ids], "outcomes": {task_id: outcome}}``;
    ``skipped`` is set when another process holds the sweep lock.
    """
    result: dict[str, Any] = {"verified": [], "outcomes": {}}
    company_dir = Path(company_dir)
    try:
        gc = _import("goal_crediting")
        gt = _import("goal_tracker")
        goals = {g.id: g for g in gt.parse_goals_from_vision(company_dir / "vision.md")}
        candidates = candidates_from_queue(queue)
        if not goals or not candidates:
            return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    fd = gc._try_lock(company_dir)
    if fd is None:
        result["skipped"] = "locked"
        return result
    try:
        root = company_dir.parent
        _sweep(
            company_dir,
            gc,
            gt,
            goals,
            candidates,
            result,
            pr_search=pr_search
            or (lambda task_id: search_merged_prs_for_task(task_id, cwd=root)),
            pr_view=pr_view or (lambda number: view_pr(number, cwd=root)),
            verifier=verifier
            or (
                lambda goal_id, pr_number, pr_url: gt.verify_goal(
                    goal_id, company_dir, pr_number=pr_number, pr_url=pr_url
                )
            ),
            git_checker=git_checker or (lambda sha: gc.check_merge_commit(sha, root)),
            revert_checker=revert_checker or (lambda pr: check_pr_reverted(pr, root)),
            ledger_reader=ledger_reader
            or (
                lambda ids, merge_commit: check_named_entries(
                    company_dir,
                    ids,
                    merge_commit,
                    main_resolver=lambda: gc.resolve_origin_main(root),
                )
            ),
            now=now or datetime.now(timezone.utc),
            max_searches=max_searches,
            max_verify_calls=max_verify_calls,
            window_days=gc.CREDIT_WINDOW_DAYS if window_days is None else window_days,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        gc._unlock(fd)
    return result


def _sweep(
    company_dir: Path,
    gc: Any,
    gt: Any,
    goals: dict,
    candidates: list[tuple[str, dict]],
    result: dict,
    *,
    pr_search: PrSearch,
    pr_view: PrView,
    verifier: Verifier,
    git_checker: Callable[[str | None], str],
    revert_checker: Callable[[dict], str],
    ledger_reader: Callable[[list[str], str | None], tuple[str, str]],
    now: datetime,
    max_searches: int,
    max_verify_calls: int,
    window_days: int,
) -> None:
    ledger = load_ledger(company_dir)
    tasks = ledger["tasks"]
    budget = ledger["goals"]
    credit = gc._load_ledger(company_dir)
    stamp = now.isoformat()
    window = timedelta(days=window_days)
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _record(task_id: str, goal_id: str, outcome: str, **extra: Any) -> None:
        prev = tasks.get(task_id) or {}
        carried = {k: prev[k] for k in _COUNTERS if k in prev}
        tasks[task_id] = {
            **carried,
            "goal_id": goal_id,
            "outcome": outcome,
            "at": stamp,
            **extra,
        }
        result["outcomes"][task_id] = outcome
        _save_ledger(company_dir, ledger)

    def _later(task_id: str, goal_id: str, why: str, delay: float, **extra) -> None:
        _record(
            task_id,
            goal_id,
            "retry",
            last_error=why[:300],
            next_attempt_at=(now + timedelta(seconds=delay)).isoformat(),
            **extra,
        )

    def _waits(task_id: str, key: str) -> int:
        return int((tasks.get(task_id) or {}).get(key, 0))

    def _merged_in_window(pr: dict) -> bool:
        merged = _parse_ts(pr.get("mergedAt"))
        return merged is not None and merged >= now - window and merged >= epoch

    # Goals another goal waits on first: their verification unblocks work.
    depended_on = {d for g in goals.values() for d in (g.depends_on or [])}
    candidates = sorted(
        candidates,
        key=lambda c: (
            c[0] in depended_on,
            str(c[1].get("completed_at") or c[1].get("created_at") or ""),
        ),
        reverse=True,
    )
    verified_now: set[str] = set()
    lookups = verify_calls = 0
    for goal_id, task in candidates:
        task_id = str(task["task_id"])
        entry = tasks.get(task_id) or {}
        if entry and entry.get("outcome") != "retry":
            continue
        if (_parse_ts(entry.get("next_attempt_at")) or now) > now:
            continue
        goal = goals.get(goal_id)
        if goal is None or goal_id in verified_now:
            continue
        if gt._is_framework_dogfood_goal(goal):
            continue
        if gt._verified_stamp_for_goal(goal, company_dir) is not None:
            continue
        if _credit_defers(credit.get(task_id)):
            continue
        if int((budget.get(goal_id) or {}).get("judge_calls", 0)) >= (
            MAX_JUDGE_CALLS_PER_GOAL
        ):
            _record(task_id, goal_id, "judge_budget_spent")
            continue
        if lookups >= max_searches:
            break
        lookups += 1

        # 1. The task's own PR.
        chosen: dict | None = None
        searched: list[dict] = []
        recorded = _pr_number(task.get("pr_url"))
        if recorded:
            view = pr_view(recorded)
            if view is None:
                _later(task_id, goal_id, "gh pr view failed", SEARCH_RECHECK_SECONDS)
                continue
            state = str(view.get("state") or "").upper()
            if state == "MERGED":
                chosen = view
            elif state == "OPEN":
                _later(task_id, goal_id, "task PR still open", SEARCH_RECHECK_SECONDS)
                continue
        if chosen is None:
            prs = pr_search(task_id)
            if prs is None:
                _later(task_id, goal_id, "gh search failed", SEARCH_RECHECK_SECONDS)
                continue
            searched = prs
            owned = [
                pr
                for pr in prs
                if pr_owned_by_task(pr, task_id) and _merged_in_window(pr)
            ]
            if len(owned) > 1:
                _record(
                    task_id,
                    goal_id,
                    "ambiguous_prs",
                    prs=sorted(str(p.get("number")) for p in owned),
                )
                continue
            if not owned:
                created = _parse_ts(task.get("created_at"))
                if created is not None and now - created > window:
                    _record(task_id, goal_id, "no_merged_pr")
                    continue
                waits = _waits(task_id, "search_waits")
                _later(
                    task_id,
                    goal_id,
                    "no merged PR owned by the task",
                    min(SEARCH_RECHECK_SECONDS * 2**waits, SEARCH_RECHECK_MAX_SECONDS),
                    search_waits=waits + 1,
                )
                continue
            chosen = owned[0]
        if not _merged_in_window(chosen):
            _record(
                task_id, goal_id, "outside_window", merged_at=chosen.get("mergedAt")
            )
            continue

        pr_number = str(chosen.get("number") or "")
        pr_url = str(chosen.get("url") or task.get("pr_url") or "")
        head_sha = chosen.get("headRefOid")
        merge_commit = (chosen.get("mergeCommit") or {}).get("oid")
        base = str(chosen.get("baseRefName") or "")
        if base and base not in MAIN_BRANCHES:
            _record(task_id, goal_id, "not_main_base", pr_number=pr_number, base=base)
            continue

        # 2. Refusals.
        if gc._has_explicit_reject_for_sha(company_dir, task_id, head_sha):
            _record(
                task_id,
                goal_id,
                "gate_rejected",
                pr_number=pr_number,
                head_sha=head_sha,
            )
            continue
        if any(reverts_pr(p, chosen) for p in searched) or (
            revert_checker(chosen) == "reverted"
        ):
            _record(task_id, goal_id, "reverted", pr_number=pr_number, how="revert PR")
            continue
        git_state = git_checker(merge_commit)
        if git_state == "reverted":
            _record(
                task_id,
                goal_id,
                "reverted",
                pr_number=pr_number,
                merge_commit=merge_commit,
            )
            continue
        if git_state != "ok":
            waits = _waits(task_id, "git_waits") + 1
            if waits >= MAX_GIT_WAITS:
                _record(
                    task_id,
                    goal_id,
                    "merge_not_on_main",
                    pr_number=pr_number,
                    git_state=git_state,
                    git_waits=waits,
                )
                continue
            _later(
                task_id,
                goal_id,
                f"git: {git_state}",
                SEARCH_RECHECK_SECONDS,
                git_waits=waits,
            )
            continue

        # 3. The acceptance ledger first: every entry the metric names (in a
        # canonical metric or one with extra clauses, e.g. G56's "and both
        # relays are redeployed") must read CONSISTENT in the newest eligible
        # main run containing the merge. Failing or not yet: wait, no judge
        # call, nothing spent from the budget.
        named = gt._ledger_named_entry_ids(goal.success_metric or "")
        if named:
            ledger_state, ledger_why = ledger_reader(named, merge_commit)
            if ledger_state != "ok":
                waits = _waits(task_id, "ledger_waits")
                _later(
                    task_id,
                    goal_id,
                    f"ledger {ledger_state}: {ledger_why}",
                    min(LEDGER_RECHECK_SECONDS * 2**waits, LEDGER_RECHECK_MAX_SECONDS),
                    pr_number=pr_number,
                    ledger_state=ledger_state,
                    ledger_waits=waits + 1,
                )
                continue

        # 4. The operator's verify path, within the goal's judge budget.
        if verify_calls >= max_verify_calls:
            break
        verify_calls += 1
        spent = int((budget.get(goal_id) or {}).get("judge_calls", 0)) + 1
        budget[goal_id] = {"judge_calls": spent, "last_at": stamp}
        _save_ledger(
            company_dir, ledger
        )  # counted before the call: a crash still counts
        try:
            verdict = verifier(goal_id, pr_number, pr_url) or {}
        except Exception as exc:  # noqa: BLE001
            verdict = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        status = verdict.get("status")
        if status == "verified":
            verified_now.add(goal_id)
            result["verified"].append(goal_id)
            _record(task_id, goal_id, "verified", pr_number=pr_number, pr_url=pr_url)
            continue
        if status == "not_found":
            _record(task_id, goal_id, "goal_not_in_vision")
            continue
        message = str(verdict.get("message") or verdict.get("assessment_status") or "")
        if spent >= MAX_JUDGE_CALLS_PER_GOAL:
            _record(
                task_id,
                goal_id,
                "not_complete",
                pr_number=pr_number,
                not_complete=_waits(task_id, "not_complete") + 1,
                last_error=message[:300],
            )
            continue
        _later(
            task_id,
            goal_id,
            f"verify: {status}: {message}",
            NOT_COMPLETE_RECHECK_SECONDS,
            pr_number=pr_number,
            not_complete=_waits(task_id, "not_complete") + 1,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one sweep now (waits for it)")
    run.add_argument("--company-dir", help=".company directory (default: resolved)")
    args = parser.parse_args(argv)
    if args.company_dir:
        company_dir = Path(args.company_dir).expanduser()
    else:
        company_dir = Path(_import("company_resolver").get_company_dir())
    queue: dict = {}
    for name in ("state", "state.nosync"):
        try:
            data = json.loads(
                (company_dir / name / "work_queue.json").read_text("utf-8")
            )
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            queue = data
            break
    if args.command == "run":
        out = verify_goals_from_merged_prs(company_dir, queue)
        print(json.dumps(out, indent=2))
        return 1 if out.get("error") else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
