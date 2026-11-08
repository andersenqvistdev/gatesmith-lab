#!/usr/bin/env python3
"""Auto-rebase conflicting PRs, and a merge queue (AUTO-REBASE-CONFLICTING-PRS).

Why this exists (webapp, 2026-10-10..13)
----------------------------------------
Every parallel goal build (#211 G43, #219 G47, #209 G45, #228 G48, #229 G49)
and several operator PRs went green and then CONFLICTING once main moved. Each
one needed a manual rebase by an operator-side agent (15-60 min) and sat
green-but-conflicting for hours. The conflicts were almost all "both sides
appended" (SPEC.md, docs/VERSIONS.md, docs/ARCHITECTURE.md, i18n catalogs,
constants.ts, the canary mock app) or two features editing one function.
This module does that rebase inside Gatesmith, the way the manual procedure that
worked did it.

The flow
--------
1. **Detect** (:func:`tick`, from the daemon's fast pr_open reconcile). The
   lane lists the daemon's own open PRs (``--author @me``, ``daemon/*``
   branches) and, when ``includeLabelled`` is on, PRs labelled
   ``gatesmith:auto-rebase``. A PR GitHub reports CONFLICTING (``DIRTY``), or
   ``BEHIND`` under a required up-to-date rule, gets ONE rebase task, deduped
   against any active rebase task for the PR and against an attempt already
   made for the same head + base SHA. A PR a human holds
   (``needs-human``, ``awaiting-operator-review``, ``challenger:needs-human``)
   or whose challenger review is running is left alone. At most
   ``maxConcurrent`` rebase tasks are active at once.
2. **Rebase** (:func:`dispatch_claimed`, from operation_loop, in the task's
   own daemon worktree). The stage checks the PR head out DETACHED and runs
   ``git rebase origin/<base>``. A clean rebase needs no session. On a
   conflict the rebase is aborted and the PR's change is re-applied as ONE
   commit on top of the base (``git merge --squash``), so the conflicts are
   resolved once, not once per commit. A fresh worker session resolves them
   keeping BOTH sides' intent (append-only docs: both, in version order;
   code: merge the semantics, never drop a side). The stage, not the session,
   does every git operation: it refuses leftover conflict markers and
   forbidden paths, commits, installs the product's dependencies from its
   lockfile (:func:`provision_dependencies`; a failure there is an
   ``environment`` outcome, never blamed on the PR), runs the product's checks
   command and the PR's acceptance entries plus the entries owning the
   conflicting files, and only
   then pushes ``HEAD:refs/heads/<branch>`` with
   ``--force-with-lease=<branch>:<pre-rebase head>`` — never a base branch.
3. **No verdict carry.** A rebased head is new code: no review verdict is
   carried to it. The deliverable judge's ``verdict-pending`` hold is
   re-applied, a PR the challenger had passed goes back to
   ``challenger:pending`` (its pass still names the old head, so
   ``merge_block_reason`` holds the new one), and the challenger stage
   reviews the new head; on a pass it re-runs the judge. Until 2026-10-30 a
   pass was carried when the PR's changed lines before and after the rebase
   compared equal as a multiset. The challenger on #777 showed that
   comparison passes a behaviourally different head: reordered lines, a
   binary content swap (no +/- lines), and a base-side rename the PR calls
   (the PR's own diff is byte-identical). Merge levers still require CI green
   on the rebased head (``merge_gate.may_merge``).
4. **One attempt.** Anything that fails after the session ran (markers left,
   checks red, an entry not CONSISTENT, a push refused for a reason other than
   the PR moving) escalates: the PR is labelled ``needs-human``, a comment
   says why, an escalation record is written, and the lane never retries that
   head. Only an infrastructure failure where nothing ran (no worktree,
   provider down) is requeued, up to three times.
5. **Merge queue.** PRs that are green, ``challenger:passed`` for their
   current head, mergeable and free of every hold label are ordered by age.
   Where ``merge_gate.may_merge`` — the one decision every merge lever
   uses (policy ``enabled``/``allowMergeToMain``/not ``requireApproval``,
   author, holds, challenger, verdict, CI, blocked/review paths, secrets,
   diff size) — lets the daemon merge, the oldest one is merged
   (``--match-head-commit``) and the remaining PRs are checked for conflicts
   at once (locally, with ``git merge-tree``), so the next one is rebased
   promptly. Otherwise — webapp: every merge is a prod deploy — the queue only
   labels ``ready-to-merge``; a human merges.
6. **Conflict log.** Every detected conflict, and every resolution or
   escalation, appends a row to ``.company/state/conflict_log.jsonl``
   (``event`` = ``conflict`` | ``resolved`` | ``escalated``; ``files``), the
   input CONFLICT-AWARE-SCHEDULING will learn hotspot files from.

Config (``autonomy.autoRebase`` in gatesmith.json; defaults in code):
``enabled`` (false), ``maxConcurrent`` (1), ``includeLabelled`` (false),
``checksCommand`` (null = detect from package.json / pyproject.toml),
``timeoutMinutes`` (45), ``checksTimeoutMinutes`` (30), ``model`` (null).

Daemon wiring: :func:`daemon_tick` and :func:`dispatch_claimed` are the two
hooks. Until operation_loop dispatches rebase tasks here
(:func:`dispatch_wired`), the lane mints nothing — a rebase task must never
reach the employee activator, which would open a new PR for it.

CLI::

    pr_rebase.py status [--company-dir .company]
    pr_rebase.py tick [--company-dir .company]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
Runner = Callable[..., Any]

SOURCE = "rebase"
ACTOR = "pr-rebase"

LABEL_OPT_IN = "gatesmith:auto-rebase"
LABEL_NEEDS_HUMAN = "needs-human"
LABEL_READY = "ready-to-merge"
# The rebase could not run because the environment is broken (dependencies do
# not install on the BASE either): not the PR's fault, but a human must look.
LABEL_ENVIRONMENT = "gatesmith:rebase-environment"
_LABEL_STYLE = {
    LABEL_NEEDS_HUMAN: ("b60205", "Gatesmith could not finish this automatically"),
    LABEL_ENVIRONMENT: (
        "d93f0b",
        "Auto-rebase blocked by the environment (dependencies do not install on the base)",
    ),
    LABEL_READY: ("0e8a16", "Green, challenger-passed, mergeable: ready to merge"),
}

# Labels under which the lane leaves a PR alone: a human has it.
HUMAN_LABELS = frozenset(
    {LABEL_NEEDS_HUMAN, "awaiting-operator-review", "challenger:needs-human"}
)
# Labels that hold a PR out of the merge queue (the daemon's hold set plus
# every challenger hold, plus our own escalation label).
_MERGE_HOLD_FALLBACK = frozenset(
    {
        "needs-manual-review",
        "awaiting-operator-review",
        "verdict-pending",
        "challenger:pending",
        "challenger:incomplete",
        "challenger:needs-human",
        LABEL_NEEDS_HUMAN,
        "do-not-merge",
    }
)
CHALLENGER_PASSED = "challenger:passed"

PROTECTED_BRANCHES = frozenset({"main", "master"})
OWN_BRANCH_PREFIX = "daemon/"

MAX_INFRA_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 600
WORKER_TIMEOUT_SLACK_SECONDS = 1800
MAX_REBASES_PER_PR = 5
PR_LIST_LIMIT = 200
GIT_TIMEOUT = 120
GH_TIMEOUT = 30
OUTPUT_TAIL_CHARS = 3000
# An install failure's detail keeps its END (where npm/pnpm/yarn say why).
INSTALL_DETAIL_CHARS = 2000
INSTALLS_PER_TASK = 3
COMMENT_MAX_CHARS = 60_000

STATE_SUBDIR = Path("state") / "pr_rebase"
STATE_FILE = "state.json"
STATE_LOCK = "state.lock"
CONFLICT_LOG = Path("state") / "conflict_log.jsonl"

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
_MARKER_RE = re.compile(r"^(<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)(?: |$)", re.MULTILINE)
_UNCOMMITTED_PREFIXES = (".company/", ".planning/")
_FORBIDDEN_PREFIXES = (".claude/", ".github/")
_FORBIDDEN_FILES = (".mcp.json", "CLAUDE.md")

SYSTEM_REINFORCEMENT = (
    "IMPORTANT: You resolve merge conflicts by editing files with the Write or "
    "Edit tools. Keep BOTH sides' intent; never drop a side. Do not run git "
    "add, commit, rebase or push: the stage does every git operation."
)


# ---------------------------------------------------------------------------
# Small helpers (house pattern: subprocess resolved at call time, never raise)
# ---------------------------------------------------------------------------


def _import(name: str):
    import importlib

    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    return importlib.import_module(name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _run(
    runner: Runner | None,
    args: list[str],
    cwd: str | Path | None,
    timeout: int,
    env: dict | None = None,
):
    run = runner or subprocess.run
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "cwd": str(cwd) if cwd else None,
        "timeout": timeout,
    }
    if env is not None:
        kwargs["env"] = env
    try:
        return run(args, **kwargs)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args, 1, "", f"{type(exc).__name__}: {exc}")


def _git_env() -> dict:
    env = dict(os.environ)
    env["GIT_EDITOR"] = "true"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    runner: Runner | None, cwd: str | Path, *args: str, timeout: int = GIT_TIMEOUT
):
    return _run(runner, ["git", "-C", str(cwd), *args], None, timeout, env=_git_env())


def _gh(runner: Runner | None, args: list[str], cwd: str | Path | None = None):
    return _run(runner, ["gh", *args], cwd, GH_TIMEOUT)


def _out(res) -> str:
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else "[...]\n" + text[-limit:]


def pr_number(pr_url: str | int | None) -> str | None:
    if isinstance(pr_url, int):
        return str(pr_url)
    text = str(pr_url or "")
    if text.isdigit():
        return text
    m = _PR_NUMBER_RE.search(text)
    return m.group(1) if m else None


def safe_branch(branch: str | None, base: str | None = None) -> bool:
    """A branch the stage may force-push: well-formed, never a base branch."""
    if not branch or not _SAFE_BRANCH_RE.fullmatch(branch) or ".." in branch:
        return False
    if branch in PROTECTED_BRANCHES or (base and branch == base):
        return False
    return not branch.startswith("-")


# ---------------------------------------------------------------------------
# Configuration (defaults in code; gatesmith.json is human-protected)
# ---------------------------------------------------------------------------


def _pos_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


@dataclass(frozen=True)
class RebaseConfig:
    enabled: bool = False
    max_concurrent: int = 1
    include_labelled: bool = False
    checks_command: str | None = None
    timeout_minutes: int = 45
    checks_timeout_minutes: int = 30
    install_timeout_minutes: int = 10
    model: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> RebaseConfig:
        raw = raw if isinstance(raw, dict) else {}
        checks = raw.get("checksCommand")
        if isinstance(checks, list) and all(isinstance(c, str) for c in checks):
            checks = " && ".join(c for c in checks if c.strip())
        model = raw.get("model")
        return cls(
            # Opt-in: only a literal true enables the lane.
            enabled=raw.get("enabled") is True,
            max_concurrent=_pos_int(raw, "maxConcurrent", 1),
            include_labelled=raw.get("includeLabelled") is True,
            checks_command=checks
            if isinstance(checks, str) and checks.strip()
            else None,
            timeout_minutes=_pos_int(raw, "timeoutMinutes", 45),
            checks_timeout_minutes=_pos_int(raw, "checksTimeoutMinutes", 30),
            install_timeout_minutes=_pos_int(raw, "installTimeoutMinutes", 10),
            model=model if isinstance(model, str) and model.strip() else None,
        )

    @property
    def timeout_seconds(self) -> int:
        return self.timeout_minutes * 60


def _merge_gate():
    return _import("merge_gate")


# The merge policy and every merge gate live in merge_gate (single-merge-gate):
# the queue is one lever among several and decides nothing on its own.
MergePolicy = _import("merge_gate").MergePolicy


def _read_project_config(project_root: Path) -> dict | None:
    """The settings this module reads, each from its governing file (the
    root gatesmith.json when it declares them, else the install template;
    see governing_config). None when a governing file is unreadable: the
    human-protected check then fails closed instead of reading "no list"."""
    try:
        return _import("governing_config").view(
            ["autonomy.autoRebase", "humanProtected"], Path(project_root)
        )
    except Exception:  # noqa: BLE001 — fail closed
        return None


def load_config(project_root: Path) -> RebaseConfig:
    autonomy = (_read_project_config(project_root) or {}).get("autonomy") or {}
    return RebaseConfig.from_dict(
        autonomy.get("autoRebase") if isinstance(autonomy, dict) else None
    )


def load_merge_policy(project_root: Path) -> MergePolicy:
    return _merge_gate().load_policy(Path(project_root))


def detect_checks_command(project_root: Path) -> str | None:
    """The product's full checks when the config names none: package.json
    scripts typecheck / lint / test (in that order, those that exist), else
    pytest for a Python project. None when nothing is recognisable."""
    root = Path(project_root)
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        steps = [f"npm run {s}" for s in ("typecheck", "lint", "test") if s in scripts]
        if not steps:
            return None
        install = "(test -d node_modules || npm ci --no-audit --no-fund)"
        return " && ".join([install, *steps])
    if (root / "pyproject.toml").exists():
        return "uv run pytest -q"
    return None


def checks_command(config: RebaseConfig, project_root: Path) -> str | None:
    return config.checks_command or detect_checks_command(project_root)


# ---------------------------------------------------------------------------
# Lane state (per PR) and the conflict log
# ---------------------------------------------------------------------------

_STATE_THREAD_LOCK = threading.Lock()


def _state_dir(company_dir: Path) -> Path:
    return Path(company_dir) / STATE_SUBDIR


def read_state(company_dir: Path) -> dict:
    try:
        data = json.loads(
            (_state_dir(company_dir) / STATE_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("prs", {})
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rb_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked_state(company_dir: Path) -> Iterator[dict]:
    import fcntl

    sdir = _state_dir(company_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    with _STATE_THREAD_LOCK, open(sdir / STATE_LOCK, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = read_state(company_dir)
            yield state
            _write_json(sdir / STATE_FILE, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update_pr(company_dir: Path, pr: str, **fields: Any) -> dict:
    with _locked_state(company_dir) as state:
        rec = state["prs"].setdefault(str(pr), {})
        history = fields.pop("history_add", None)
        attempted = fields.pop("attempted_add", None)
        rec.update(fields)
        rec["updated_at"] = _iso()
        if attempted:
            keys = rec.setdefault("attempted", [])
            if attempted not in keys:
                keys.append(attempted)
                del keys[:-50]
        if history:
            rec.setdefault("history", []).append(history)
            del rec["history"][:-50]
        return dict(rec)


def attempt_key(head_sha: str | None, base_sha: str | None) -> str:
    return f"{head_sha or '?'}..{base_sha or '?'}"


def log_conflict(company_dir: Path, row: dict) -> None:
    """Append one row to ``state/conflict_log.jsonl`` (best effort)."""
    path = Path(company_dir) / CONFLICT_LOG
    entry = {"ts": _iso(), **row}
    entry["files"] = sorted({str(f) for f in entry.get("files") or []})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def read_conflict_log(company_dir: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = (Path(company_dir) / CONFLICT_LOG).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in lines.splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def hotspot_counts(company_dir: Path) -> Counter:
    """Conflicts per file (``event == "conflict"`` rows): the hotspot list
    CONFLICT-AWARE-SCHEDULING will read."""
    counts: Counter = Counter()
    for row in read_conflict_log(company_dir):
        if row.get("event") == "conflict":
            counts.update(row.get("files") or [])
    return counts


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def queue_paths(company_dir: Path) -> tuple[Path, Path]:
    company_dir = Path(company_dir)
    return (
        company_dir / "state" / "work_queue.json",
        company_dir / "runtime" / "queue.lock",
    )


def _load_queue(queue_path: Path) -> dict:
    try:
        data = json.loads(Path(queue_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_rebase_task(task: Any) -> bool:
    return (
        isinstance(task, dict)
        and task.get("source") == SOURCE
        and isinstance(task.get("rebase"), dict)
    )


_ACTIVE_LANES = ("pending", "in_progress")


def active_rebase_tasks(queue: dict) -> list[dict]:
    return [
        t
        for lane in _ACTIVE_LANES
        for t in (queue.get(lane) or [])
        if is_rebase_task(t)
    ]


def active_rebase_task_for(queue: dict, pr: str) -> dict | None:
    for t in active_rebase_tasks(queue):
        if pr_number(t["rebase"].get("pr")) == str(pr):
            return t
    return None


def _challenger_active_for(queue: dict, pr: str) -> bool:
    for lane in _ACTIVE_LANES:
        for t in queue.get(lane) or []:
            meta = t.get("challenger") if isinstance(t, dict) else None
            if (
                isinstance(meta, dict)
                and t.get("source") == "challenger"
                and pr_number(meta.get("pr_url")) == str(pr)
            ):
                return True
    return False


def owner_task(queue: dict, pr: str) -> dict | None:
    """The builder task that owns PR ``pr`` (never a stage or rebase task)."""
    for lane, tasks in queue.items():
        if lane == "cancelled" or not isinstance(tasks, list):
            continue
        for t in tasks:
            if (
                isinstance(t, dict)
                and not is_rebase_task(t)
                and t.get("source") != "challenger"
                and pr_number(t.get("pr_url")) == str(pr)
            ):
                return t
    return None


def enqueue_rebase_task(company_dir: Path, task: dict, max_concurrent: int) -> bool:
    """Append under the queue lock unless an active rebase task already owns
    the PR, or ``max_concurrent`` are already active. True if added."""
    wa = _import("work_allocator")
    queue_path, lock_path = queue_paths(company_dir)
    pr = str(task["rebase"]["pr"])
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        if not queue:
            return False
        if active_rebase_task_for(queue, pr) is not None:
            return False
        if len(active_rebase_tasks(queue)) >= max_concurrent:
            return False
        queue.setdefault("pending", []).append(task)
        queue.setdefault("metadata", {})["last_modified"] = _iso()
        wa.write_queue_file(queue_path, queue)
    return True


def _requeue_with_backoff(queue_path: Path, task_id: str, error: str) -> bool:
    wa = _import("work_allocator")
    lock_path = Path(queue_path).parent.parent / "runtime" / "queue.lock"
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        tasks = queue.get("in_progress") or []
        task = next((t for t in tasks if t.get("task_id") == task_id), None)
        if task is None:
            return False
        queue["in_progress"] = [t for t in tasks if t.get("task_id") != task_id]
        task["status"] = "pending"
        task["rebase_infra_attempts"] = int(task.get("rebase_infra_attempts", 0)) + 1
        task["backoff_until"] = _iso(_now() + timedelta(seconds=RETRY_BACKOFF_SECONDS))
        task.setdefault("notes", []).append(
            {"timestamp": _iso(), "content": error[:300]}
        )
        for key in ("claimed_by", "claimed_at", "started_at"):
            task.pop(key, None)
        queue.setdefault("pending", []).append(task)
        wa.write_queue_file(queue_path, queue)
    return True


# ---------------------------------------------------------------------------
# GitHub: list, classify, labels, comments
# ---------------------------------------------------------------------------

_PR_FIELDS = (
    "number,title,url,headRefName,headRefOid,baseRefName,mergeable,"
    "mergeStateStatus,labels,createdAt,isDraft,isCrossRepository,statusCheckRollup"
)


def _pr_list(runner: Runner | None, cwd: Path, extra: list[str]) -> list[dict] | None:
    res = _gh(
        runner,
        [
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(PR_LIST_LIMIT),
            *extra,
            "--json",
            _PR_FIELDS,
        ],
        cwd,
    )
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else None


def label_names(pr: dict) -> set[str]:
    out: set[str] = set()
    for lbl in pr.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str):
            out.add(name)
    return out


def list_candidate_prs(
    config: RebaseConfig, runner: Runner | None, cwd: Path
) -> tuple[list[dict], list[str]]:
    """(the lane's PRs oldest first, errors). Own = ``--author @me`` on a
    ``daemon/*`` branch; labelled = ``gatesmith:auto-rebase`` (opt-in). Forks
    are never candidates (the stage cannot push to them)."""
    errors: list[str] = []
    found: dict[int, dict] = {}
    own = _pr_list(runner, cwd, ["--author", "@me"])
    if own is None:
        errors.append("gh pr list (own PRs) failed")
    for pr in own or []:
        if str(pr.get("headRefName") or "").startswith(OWN_BRANCH_PREFIX):
            found[int(pr["number"])] = {**pr, "_origin": "own"}
    if config.include_labelled:
        labelled = _pr_list(runner, cwd, ["--label", LABEL_OPT_IN])
        if labelled is None:
            errors.append(f"gh pr list (--label {LABEL_OPT_IN}) failed")
        for pr in labelled or []:
            if LABEL_OPT_IN in label_names(pr):
                found.setdefault(int(pr["number"]), {**pr, "_origin": "labelled"})
    prs = [p for p in found.values() if not p.get("isCrossRepository")]
    prs.sort(key=lambda p: (str(p.get("createdAt") or ""), int(p["number"])))
    return prs, errors


def rebase_reason(pr: dict) -> str | None:
    """'conflicting' | 'behind' | None, from GitHub's own view of the PR."""
    if pr.get("isDraft"):
        return None
    mergeable = str(pr.get("mergeable") or "").upper()
    state = str(pr.get("mergeStateStatus") or "").upper()
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return "conflicting"
    if state == "BEHIND":
        # Only reported when branch protection requires an up-to-date head.
        return "behind"
    return None


def checks_green(pr: dict) -> bool:
    return _merge_gate().checks_green(pr)


def ensure_label(name: str, runner: Runner | None, cwd: Path | None) -> None:
    color, desc = _LABEL_STYLE.get(name, ("ededed", "Gatesmith"))
    _gh(
        runner,
        ["label", "create", name, "--color", color, "--description", desc, "--force"],
        cwd,
    )


def add_label(pr: str, name: str, runner: Runner | None, cwd: Path | None) -> bool:
    ensure_label(name, runner, cwd)
    return (
        _gh(runner, ["pr", "edit", str(pr), "--add-label", name], cwd).returncode == 0
    )


def remove_label(pr: str, name: str, runner: Runner | None, cwd: Path | None) -> bool:
    return (
        _gh(runner, ["pr", "edit", str(pr), "--remove-label", name], cwd).returncode
        == 0
    )


def post_comment(pr: str, body: str, runner: Runner | None, cwd: Path | None) -> bool:
    if len(body) > COMMENT_MAX_CHARS:
        body = body[:COMMENT_MAX_CHARS] + "\n\n[... cut ...]\n"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="pr-rebase-", delete=False, encoding="utf-8"
    ) as f:
        f.write(body)
        path = f.name
    try:
        res = _gh(runner, ["pr", "comment", str(pr), "--body-file", path], cwd)
        return res.returncode == 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _pr_state(pr: str, runner: Runner | None, cwd: Path | None) -> dict | None:
    res = _gh(
        runner,
        ["pr", "view", str(pr), "--json", "state,headRefOid,labels,baseRefName"],
        cwd,
    )
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Git: base SHA, local conflict prediction, diffs
# ---------------------------------------------------------------------------


def remote_sha(cwd: Path, branch: str, runner: Runner | None = None) -> str | None:
    res = _git(runner, cwd, "ls-remote", "origin", f"refs/heads/{branch}")
    text = _out(res)
    return text.split()[0] if text else None


def fetch_refs(cwd: Path, branches: list[str], runner: Runner | None = None) -> bool:
    specs = [
        f"+refs/heads/{b}:refs/remotes/origin/{b}"
        for b in dict.fromkeys(branches)
        if b and _SAFE_BRANCH_RE.fullmatch(b) and ".." not in b
    ]
    if not specs:
        return False
    return _git(runner, cwd, "fetch", "--quiet", "origin", *specs).returncode == 0


def predict_conflicts(
    cwd: Path, base_ref: str, head_ref: str, runner: Runner | None = None
) -> list[str] | None:
    """Files a merge of ``head_ref`` into ``base_ref`` would conflict on, from
    ``git merge-tree --write-tree`` (no working tree touched). [] = clean;
    None = could not tell."""
    res = _git(
        runner,
        cwd,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        base_ref,
        head_ref,
    )
    if res.returncode == 0:
        return []
    if res.returncode != 1:
        return None
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return list(dict.fromkeys(lines[1:]))


def changed_files(
    cwd: Path, base_ref: str, head_ref: str, runner: Runner | None = None
) -> list[str]:
    res = _git(runner, cwd, "diff", "--name-only", f"{base_ref}...{head_ref}")
    return [ln.strip() for ln in _out(res).splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Acceptance entries the rebase must keep green
# ---------------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _entries(project_dir: Path) -> list[tuple[str, str, dict]]:
    """(entry id, case-file path relative to the project, entry) for every
    acceptance case and relation."""
    out: list[tuple[str, str, dict]] = []
    for sub in ("cases", "relations"):
        folder = Path(project_dir) / "acceptance" / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                out.append((data["id"], f"acceptance/{sub}/{path.name}", data))
    return out


def entries_owning(project_dir: Path, paths: list[str]) -> list[str]:
    """Entries whose case file is one of ``paths``, or which name one of
    ``paths`` (their test file, canary mock, flow file...)."""
    wanted = [p for p in paths if p]
    ids: list[str] = []
    for entry_id, case_file, data in _entries(project_dir):
        if case_file in wanted or any(
            p in s for s in _strings(data) for p in wanted if len(p) > 3
        ):
            ids.append(entry_id)
    return list(dict.fromkeys(ids))


def pr_entry_ids(
    company_dir: Path,
    project_dir: Path,
    owner: dict | None,
    pr_files: list[str],
    conflicted: list[str],
) -> list[str]:
    """The PR's own acceptance entries (its goal's, and those whose case files
    it adds or changes) plus the entries owning the conflicting files."""
    ids: list[str] = []
    if owner is not None:
        with contextlib.suppress(Exception):
            cs = _import("challenger_stage")
            goal = cs.product_goal(owner, company_dir)
            if goal is not None:
                ids += cs.goal_entry_ids(goal)
    case_files = [p for p in pr_files if p.startswith("acceptance/")]
    ids += entries_owning(project_dir, case_files)
    ids += entries_owning(project_dir, conflicted)
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


def render_brief(
    *,
    pr: str,
    pr_url: str,
    branch: str,
    base: str,
    base_sha: str | None,
    head_sha: str | None,
    reason: str,
    conflicted: list[str],
    entry_ids: list[str],
    checks: str | None,
) -> str:
    files = "\n".join(f"- `{f}`" for f in conflicted) or (
        "- (GitHub reports the PR behind its base; no conflicting file predicted)"
    )
    entries = ", ".join(entry_ids) or "(none found)"
    return f"""Rebase PR #{pr} ({pr_url}) onto `origin/{base}`: GitHub reports it {reason.upper()}.

PR head branch: `{branch}` at `{(head_sha or "?")[:12]}`; base `{base}` at `{(base_sha or "?")[:12]}`.

Conflicting files (predicted with `git merge-tree`; the stage re-computes them):
{files}

Procedure (the one that worked by hand on webapp, 2026-10-10..13):
1. A fresh worktree on the PR head (the daemon's worktree for this task, detached).
2. `git rebase origin/{base}`. A clean rebase needs no session. On a conflict the
   stage re-applies the PR as ONE commit on top of `origin/{base}`
   (`git merge --squash`), so conflicts are resolved once.
3. Resolve keeping BOTH sides' intent:
   - append-only docs (SPEC.md, docs/VERSIONS.md, docs/ARCHITECTURE.md, user guides,
     changelogs): keep both sides' additions, in version order; never drop either.
   - locale catalogs / i18n / constants: keep every key from both sides; fix only the
     punctuation the merge needs (a missing comma).
   - code (two features in one function): merge the semantics so both features work;
     never drop a side. If both cannot hold, stop and say why — that is a human call.
   - no conflict markers may remain (`<<<<<<<`, `>>>>>>>`).
4. Run the product's full checks: `{checks or "(no checks command configured or detected)"}`,
   the PR's own acceptance entries and the entries owning the conflicting files:
   {entries}.
5. Push `HEAD:refs/heads/{branch}` with `--force-with-lease={branch}:<pre-rebase head>`.
   Push target is the PR's head branch ONLY — never `{base}`, never main.

One attempt: if conflicts cannot be resolved or a check stays red, the PR is labelled
`{LABEL_NEEDS_HUMAN}` with the reason and an escalation is recorded; it is never retried.
The stage does every git operation (rebase, add, commit, push); the session only edits files.
"""


def build_task(
    *,
    pr: dict,
    reason: str,
    base_sha: str | None,
    conflicted: list[str],
    entry_ids: list[str],
    checks: str | None,
    owner: dict | None,
    config: RebaseConfig,
) -> dict:
    wa = _import("work_allocator")
    n = str(pr["number"])
    branch = str(pr.get("headRefName") or "")
    base = str(pr.get("baseRefName") or "main")
    pr_url = str(pr.get("url") or "")
    head_sha = pr.get("headRefOid")
    now = _iso()
    return {
        "task_id": wa.generate_task_id(),
        "title": f"[REBASE] PR #{n}: rebase {branch} onto {base}",
        "description": render_brief(
            pr=n,
            pr_url=pr_url,
            branch=branch,
            base=base,
            base_sha=base_sha,
            head_sha=head_sha,
            reason=reason,
            conflicted=conflicted,
            entry_ids=entry_ids,
            checks=checks,
        ),
        "priority": 1,
        "status": "pending",
        "source": SOURCE,
        "created_at": now,
        "created_by": ACTOR,
        "complexity": "complex",
        "estimated_complexity": "complex",
        # One attempt: a failure escalates, it is never retried by recovery.
        "max_attempts": 1,
        "worker_timeout_seconds": config.timeout_seconds
        + 2 * config.checks_timeout_minutes * 60
        # Up to three installs (base, head, one retry), on their own budget.
        + INSTALLS_PER_TASK * config.install_timeout_minutes * 60
        + WORKER_TIMEOUT_SLACK_SECONDS,
        "requires_deliverable": False,
        "rebase": {
            "pr": n,
            "pr_url": pr_url,
            "branch": branch,
            "base": base,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "reason": reason,
            "conflicting_files": conflicted,
            "entry_ids": entry_ids,
            "checks_command": checks,
            "owner_task_id": (owner or {}).get("task_id"),
            "origin": pr.get("_origin", "own"),
        },
    }


# ---------------------------------------------------------------------------
# The tick: detect, mint, merge queue
# ---------------------------------------------------------------------------


def dispatch_wired() -> bool:
    """operation_loop routes rebase tasks to :func:`dispatch_claimed`.

    Until it does, a rebase task would reach the employee activator (which
    opens a NEW PR for what it builds), so the lane mints nothing."""
    try:
        text = (HOOKS_DIR / "operation_loop.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "pr_rebase" in text and "dispatch_claimed" in text


def _detect(
    company_dir: Path,
    project_root: Path,
    prs: list[dict],
    config: RebaseConfig,
    result: dict,
    runner: Runner | None,
    *,
    local_check: bool = False,
) -> None:
    """Mint rebase tasks for the PRs that need one. ``local_check`` predicts
    conflicts with merge-tree instead of trusting GitHub's (lazily computed,
    stale right after a merge) mergeable state."""
    queue_path, _lock = queue_paths(company_dir)
    state = read_state(company_dir)
    base_shas: dict[str, str | None] = {}
    for pr in prs:
        n = str(pr["number"])
        branch = str(pr.get("headRefName") or "")
        base = str(pr.get("baseRefName") or "main")
        reason = rebase_reason(pr)
        if reason is None and not local_check:
            continue
        if pr.get("isDraft"):
            continue
        if label_names(pr) & HUMAN_LABELS:
            continue
        if not safe_branch(branch, base):
            continue
        queue = _load_queue(queue_path)
        if not queue:
            return
        if active_rebase_task_for(queue, n) is not None:
            continue  # dedupe: one rebase per PR at a time
        if _challenger_active_for(queue, n):
            continue  # its review pushes to the branch; rebase afterwards
        if len(active_rebase_tasks(queue)) >= config.max_concurrent:
            result["deferred"].append(n)
            continue
        rec = state["prs"].get(n) or {}
        head = pr.get("headRefOid")
        if rec.get("escalated_head") and rec.get("escalated_head") == head:
            continue  # one attempt, then a human: never loop on this head
        if int(rec.get("rebases", 0)) >= MAX_REBASES_PER_PR:
            continue
        if base not in base_shas:
            base_shas[base] = remote_sha(project_root, base, runner)
        base_sha = base_shas[base]
        if attempt_key(head, base_sha) in (rec.get("attempted") or []):
            continue  # already tried this exact head on this exact base
        fetch_refs(project_root, [base, branch], runner)
        head_ref = head or f"origin/{branch}"
        conflicted = predict_conflicts(project_root, f"origin/{base}", head_ref, runner)
        if local_check and reason is None:
            if not conflicted:
                continue
            reason = "conflicting"
        conflicted = conflicted or []
        pr_files = changed_files(project_root, f"origin/{base}", head_ref, runner)
        owner = owner_task(queue, n)
        entry_ids = pr_entry_ids(company_dir, project_root, owner, pr_files, conflicted)
        checks = checks_command(config, project_root)
        task = build_task(
            pr=pr,
            reason=reason,
            base_sha=base_sha,
            conflicted=conflicted,
            entry_ids=entry_ids,
            checks=checks,
            owner=owner,
            config=config,
        )
        if not enqueue_rebase_task(company_dir, task, config.max_concurrent):
            continue
        _update_pr(
            company_dir,
            n,
            pr_url=pr.get("url"),
            branch=branch,
            active_task_id=task["task_id"],
            attempted_add=attempt_key(head, base_sha),
            history_add={
                "at": _iso(),
                "event": "minted",
                "task_id": task["task_id"],
                "reason": reason,
                "head": head,
                "base_sha": base_sha,
            },
        )
        log_conflict(
            company_dir,
            {
                "event": "conflict",
                "repo": project_root.name,
                "pr": int(n),
                "branch": branch,
                "base": base,
                "base_sha": base_sha,
                "head_sha": head,
                "reason": reason,
                "files": conflicted,
                "task_id": task["task_id"],
            },
        )
        result["minted"].append(n)


def _merge_hold_labels() -> frozenset:
    return _merge_gate().hold_labels() | _MERGE_HOLD_FALLBACK


def queue_readiness(
    pr: dict,
    company_dir: Path,
    policy: MergePolicy,
    runner: Runner | None,
    queue: dict,
) -> str | None:
    """Why ``pr`` is not ready to merge, or None (green, challenger-passed for
    its current head, mergeable, nothing holding it)."""
    n = str(pr["number"])
    labels = label_names(pr)
    if pr.get("isDraft"):
        return "draft"
    if str(pr.get("baseRefName") or "") not in policy.allowed_base_branches:
        return f"base {pr.get('baseRefName')!r} is not an allowed merge target"
    held = labels & _merge_hold_labels()
    if held:
        return "held by " + ", ".join(sorted(held))
    if CHALLENGER_PASSED not in labels:
        return f"no {CHALLENGER_PASSED}"
    if active_rebase_task_for(queue, n) is not None:
        return "a rebase is in progress"
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        return f"mergeable={pr.get('mergeable')}"
    if str(pr.get("mergeStateStatus") or "").upper() != "CLEAN":
        return f"mergeStateStatus={pr.get('mergeStateStatus')}"
    if not checks_green(pr):
        return "checks not green"
    try:
        block = _import("challenger_stage").merge_block_reason(
            n,
            company_dir=company_dir,
            head_sha=pr.get("headRefOid"),
            runner=runner,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return f"challenger gate unavailable: {exc}"
    if block:
        return block
    return None


def merge_decision(
    pr: dict,
    company_dir: Path,
    policy: MergePolicy,
    runner: Runner | None,
    cwd: Path,
) -> tuple[str | None, str | None]:
    """(why the QUEUE may not merge this ready PR itself, the judged head).

    The one gate every merge lever uses (``merge_gate.may_merge``): policy,
    author, hold labels, challenger, verdict, conflicts, CI, blocked/review
    paths, secrets, diff size — read fresh, and refused when the head moved
    since the PR was listed. A human may still merge a refused PR."""
    return _merge_gate().may_merge(
        pr["number"],
        company_dir=Path(company_dir),
        project_root=Path(cwd),
        runner=runner,
        policy=policy,
        expected_head=pr.get("headRefOid") or None,
    )


def merge_refusal(
    pr: dict,
    company_dir: Path,
    policy: MergePolicy,
    runner: Runner | None,
    cwd: Path,
) -> str | None:
    """Why the QUEUE may not merge this ready PR itself (a human may)."""
    return merge_decision(pr, company_dir, policy, runner, cwd)[0]


def _merge(
    pr: dict, policy: MergePolicy, head: str, runner: Runner | None, cwd: Path
) -> bool:
    cmd = _merge_gate().merge_command(pr["number"], policy, head)
    return _gh(runner, cmd[1:], cwd).returncode == 0


def run_merge_queue(
    company_dir: Path,
    project_root: Path,
    prs: list[dict],
    policy: MergePolicy,
    result: dict,
    runner: Runner | None,
) -> dict | None:
    """Walk the PRs oldest first; merge at most one (where the policy lets the
    daemon merge), label the rest of the ready ones ``ready-to-merge``.
    Returns the merged PR, or None."""
    queue = _load_queue(queue_paths(company_dir)[0])
    merged: dict | None = None
    for pr in prs:
        n = str(pr["number"])
        labels = label_names(pr)
        why = queue_readiness(pr, company_dir, policy, runner, queue)
        if why is not None:
            if LABEL_READY in labels:
                remove_label(n, LABEL_READY, runner, project_root)
            result["not_ready"][n] = why
            continue
        if merged is not None:
            refusal, head = "one merge per tick", None
        else:
            refusal, head = merge_decision(
                pr, company_dir, policy, runner, project_root
            )
        if refusal is None and head and _merge(pr, policy, head, runner, project_root):
            merged = pr
            result["merged"].append(n)
            owner = owner_task(queue, n)
            with contextlib.suppress(Exception):
                ma = _import("merge_audit")
                ma.append_merge_chain(
                    company_dir,
                    task_id=(owner or {}).get("task_id"),
                    pr=int(n),
                    deliverable_verdict=ma.verdict_label(
                        ma.lookup_verdict_record_for_sha(company_dir, head)
                    ),
                    ci_state="pass",
                    merge_lever="pr_rebase_merge_queue",
                )
            continue
        if LABEL_READY not in labels:
            add_label(n, LABEL_READY, runner, project_root)
        result["ready"].append(n)
        if refusal:
            result["merge_refused"][n] = refusal
    return merged


def _empty_result() -> dict[str, Any]:
    return {
        "minted": [],
        "deferred": [],
        "merged": [],
        "ready": [],
        "not_ready": {},
        "merge_refused": {},
        "errors": [],
    }


def tick(
    company_dir: Path | str = ".company",
    *,
    runner: Runner | None = None,
    config: RebaseConfig | None = None,
    policy: MergePolicy | None = None,
) -> dict[str, Any]:
    """One pass: mint rebase tasks for conflicting/behind PRs, then run the
    merge queue, then (after a merge) check the rest for conflicts at once.
    Never raises."""
    result = _empty_result()
    try:
        company_dir = Path(company_dir)
        project_root = company_dir.resolve().parent
        config = config or load_config(project_root)
        if not config.enabled:
            return result
        policy = policy or load_merge_policy(project_root)
        prs, errors = list_candidate_prs(config, runner, project_root)
        result["errors"] += errors
        if not dispatch_wired():
            result["errors"].append(
                "operation_loop does not dispatch rebase tasks to "
                "pr_rebase.dispatch_claimed yet: minting nothing"
            )
        else:
            _detect(company_dir, project_root, prs, config, result, runner)
        merged = run_merge_queue(company_dir, project_root, prs, policy, result, runner)
        if merged is not None and dispatch_wired():
            rest = [p for p in prs if p["number"] != merged["number"]]
            _detect(
                company_dir,
                project_root,
                rest,
                config,
                result,
                runner,
                local_check=True,
            )
    except Exception as exc:  # noqa: BLE001 — the daemon loop must never break here
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def daemon_tick(company_dir: Path | str = ".company", logger: Any = None) -> dict:
    """The daemon hook: :func:`tick` with its outcome logged. Never raises."""
    out = tick(company_dir)
    if logger is not None:
        with contextlib.suppress(Exception):
            for n in out["minted"]:
                logger.info(f"[Rebase] queued a rebase task for PR #{n}")
            for n in out["merged"]:
                logger.info(f"[Rebase] merge queue merged PR #{n}")
            for n in out["ready"]:
                logger.info(f"[Rebase] PR #{n} is ready to merge (labelled)")
            for err in out["errors"]:
                logger.warning(f"[Rebase] {err}")
    return out


# ---------------------------------------------------------------------------
# Executing a claimed rebase task (called from operation_loop)
# ---------------------------------------------------------------------------


@dataclass
class Deps:
    """Everything with a side effect, injectable for tests."""

    runner: Runner | None = None  # git and gh
    session: Callable[..., Any] | None = None  # challenger_stage.run_session
    ledger: Callable[..., dict] | None = None  # challenger_stage.run_goal_entries
    checks: Callable[..., tuple[bool, str]] | None = None
    escalate: Callable[..., Any] | None = None  # escalation.escalate
    hold_for_new_head: Callable[..., Any] | None = None
    scratch_root: Path | None = None
    # (worktree, state_dir) -> (ok, detail); default provision_dependencies
    provision: Callable[..., tuple[bool, str]] | None = None


def provision_dependencies(
    worktree: Path, state_dir: Path, timeout_seconds: float | None = None
) -> tuple[bool, str]:
    """Install the product's dependencies in a rebase worktree: (ok, detail).

    A daemon worktree is a fresh checkout: no ``node_modules``. webapp#306
    (2026-10-20): the configured checks (``npx tsc --noEmit && ... && npm
    test``) ran there, vitest was not installed, and every TypeScript PR the
    lane rebased escalated to a human as "the product's checks fail" — the
    environment blamed on the PR. This reuses the acceptance ledger's
    lockfile-keyed installer (``acceptance_ledger.ensure_dependencies``: ``npm
    ci`` / ``pnpm install --frozen-lockfile`` / ``yarn install
    --frozen-lockfile``, minimal environment, recorded by lockfile hash in
    ``state_dir``), the same one the challenger stage runs before a product's
    entries. No lockfile: nothing to install, ok.
    """
    try:
        al = _import("acceptance_ledger")
        if timeout_seconds is None:
            result = al.ensure_dependencies(Path(worktree), Path(state_dir))
        else:
            result = al.ensure_dependencies(
                Path(worktree), Path(state_dir), timeout=timeout_seconds
            )
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return False, f"dependency install crashed: {type(exc).__name__}: {exc}"[:500]
    action = getattr(result, "action", "")
    if action == "failed":
        why = getattr(result, "error", None) or "unknown error"
        reason = getattr(result, "reason", "")
        lockfile = getattr(result, "lockfile", None) or "lockfile"
        return False, _tail(
            f"installing dependencies from {lockfile} failed"
            + (f" ({reason})" if reason else "")
            + f": {why}",
            INSTALL_DETAIL_CHARS,
        )
    return True, str(action or "none")


def run_checks(command: str, cwd: Path, timeout_seconds: int) -> tuple[bool, str]:
    """The product's checks in the rebased worktree: (passed, output tail)."""
    try:
        res = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout_seconds}s"
    except OSError as exc:
        return False, f"could not run: {exc}"
    return res.returncode == 0, _tail((res.stdout or "") + (res.stderr or ""))


def _result(action: str, task_id: str, reason: str, task: dict, **extra: Any) -> dict:
    return {
        "action": action,
        "reason": reason,
        "task_id": task_id,
        "result": {"executed": "completed"}.get(action, action),
        "task": task,
        "escalation": extra.pop("escalation", None),
        "rebase": extra,
    }


def dispatch_claimed(
    task: dict,
    *,
    queue_path: Path,
    execution_cwd: str | None,
    release: Callable[..., dict],
    deps: Deps | None = None,
) -> dict | None:
    """operation_loop's hook: run a claimed rebase task, or None when ``task``
    is not one (the caller carries on with its normal dispatch)."""
    if not is_rebase_task(task):
        return None
    return execute_claimed(
        task,
        queue_path=queue_path,
        execution_cwd=execution_cwd,
        release=release,
        deps=deps,
    )


def execute_claimed(
    task: dict,
    *,
    queue_path: Path,
    execution_cwd: str | None,
    release: Callable[..., dict],
    deps: Deps | None = None,
) -> dict:
    """Run a claimed rebase task end to end and release it. Never raises and
    never returns "failed" (failure recovery would retry it — this lane makes
    one attempt, then hands the PR to a human)."""
    deps = deps or Deps()
    task_id = str(task.get("task_id"))
    try:
        return _Execution(task, Path(queue_path), execution_cwd, release, deps).run()
    except Exception as exc:  # noqa: BLE001
        error = f"rebase stage error: {type(exc).__name__}: {exc}"[:500]
        with contextlib.suppress(Exception):
            released = release(
                queue_path=Path(queue_path),
                task_id=task_id,
                result="completed",
                error=error,
            )
            return _result(
                "executed", task_id, error, (released or {}).get("task") or task
            )
        return _result("executed", task_id, error, task)


class _Execution:
    def __init__(
        self,
        task: dict,
        queue_path: Path,
        execution_cwd: str | None,
        release: Callable[..., dict],
        deps: Deps,
    ) -> None:
        self.task = task
        self.task_id = str(task.get("task_id"))
        self.meta = dict(task.get("rebase") or {})
        self.queue_path = queue_path
        self.company_dir = queue_path.parent.parent
        self.project_root = self.company_dir.resolve().parent
        self.config = load_config(self.project_root)
        self.execution_cwd = execution_cwd
        self.release = release
        self.deps = deps
        self.runner = deps.runner
        self.pr = str(pr_number(self.meta.get("pr")) or "")
        self.pr_url = str(self.meta.get("pr_url") or "")
        self.branch = str(self.meta.get("branch") or "")
        self.base = str(self.meta.get("base") or "main")
        self.worktree: Path | None = None
        self.pre_head: str | None = None
        self.conflicted: list[str] = []
        self.labels: set = set()

    # -- endings -----------------------------------------------------------

    def _release(self, reason: str, *, result: str = "completed", **extra: Any) -> dict:
        released = self.release(
            queue_path=self.queue_path,
            task_id=self.task_id,
            result=result,
            error=None if result == "completed" else reason[:300],
        )
        return _result(
            "executed",
            self.task_id,
            reason,
            (released or {}).get("task") or self.task,
            **extra,
        )

    def _clean_worktree(self) -> None:
        if self.worktree is None:
            return
        _git(self.runner, self.worktree, "rebase", "--abort")
        _git(self.runner, self.worktree, "merge", "--abort")
        if self.branch:
            _git(
                self.runner,
                self.worktree,
                "checkout",
                "--detach",
                "--force",
                f"origin/{self.branch}",
            )
        _git(self.runner, self.worktree, "clean", "-fd")

    def _finish(
        self, reason: str, *, outcome: str, result: str = "completed", **extra: Any
    ) -> dict:
        self._clean_worktree()
        if self.pr:
            _update_pr(
                self.company_dir,
                self.pr,
                active_task_id=None,
                history_add={
                    "at": _iso(),
                    "event": outcome,
                    "task_id": self.task_id,
                    "reason": reason[:300],
                },
            )
        return self._release(reason, result=result, outcome=outcome, **extra)

    def _transient(self, error: str) -> dict:
        attempts = int(self.task.get("rebase_infra_attempts", 0)) + 1
        if attempts >= MAX_INFRA_ATTEMPTS:
            return self._escalate(
                f"the rebase could not start {attempts} times: {error}"
            )
        self._clean_worktree()
        _requeue_with_backoff(self.queue_path, self.task_id, error)
        return _result("requeued", self.task_id, error, self.task, outcome="requeued")

    def _environment(self, detail: str) -> dict:
        """The environment, not the PR, is broken: the BASE's dependencies do
        not install either (registry, network, disk, a broken lockfile on the
        base itself).

        Requeued with back-off like an infrastructure failure. After
        ``MAX_INFRA_ATTEMPTS`` the task is released BLOCKED (never
        "completed": it shows in gatesmith-queue's BLOCKED lane) and the PR gets
        ``LABEL_ENVIRONMENT`` plus a comment saying the PR is not at fault. It
        is not escalated as the PR's failure and does not get ``needs-human``:
        once the environment is fixed, the next base or head move rebases it.
        """
        reason = f"environment: {detail}"
        attempts = int(self.task.get("rebase_infra_attempts", 0)) + 1
        if attempts < MAX_INFRA_ATTEMPTS:
            self._clean_worktree()
            _requeue_with_backoff(self.queue_path, self.task_id, reason[-500:])
            return _result(
                "requeued", self.task_id, reason[-500:], self.task, outcome="requeued"
            )
        if self.pr and LABEL_ENVIRONMENT not in self.labels:
            # One comment per environment episode: the label marks it, and a
            # successful rebase removes the label.
            cwd = self.project_root
            add_label(self.pr, LABEL_ENVIRONMENT, self.runner, cwd)
            post_comment(
                self.pr,
                f"<!-- forge-rebase task={self.task_id} environment -->\n"
                f"**Auto-rebase onto `{self.base}` is blocked by the environment, "
                f"not by this PR** ({attempts} attempts):\n\n"
                f"```\n{_tail(detail, INSTALL_DETAIL_CHARS)}\n```\n\n"
                "Fix the environment; the lane retries when the base or this PR "
                f"moves, and removes `{LABEL_ENVIRONMENT}` once a rebase succeeds.",
                self.runner,
                cwd,
            )
        return self._finish(
            reason[-500:], outcome="environment_error", result="blocked"
        )

    def _provisioner(self) -> Callable[..., tuple[bool, str]]:
        if self.deps.provision is not None:
            return self.deps.provision
        timeout = self.config.install_timeout_minutes * 60

        def _default(worktree: Path, state_dir: Path) -> tuple[bool, str]:
            return provision_dependencies(worktree, state_dir, timeout)

        return _default

    def _install_base(self, base_ref: str, deps_state: Path) -> dict | None:
        """Install the BASE's dependencies in this worktree, then return to
        the current HEAD. An ending (environment / transient), or None.

        Run before any conflict-resolution session too, so a broken
        environment stops the task before a session is spent on it.
        """
        provision = self._provisioner()
        head = _out(_git(self.runner, self.worktree, "rev-parse", "HEAD"))
        if not head:
            return self._transient("HEAD unreadable before the dependency install")
        co = _git(self.runner, self.worktree, "checkout", "--detach", base_ref)
        if co.returncode != 0:
            return self._transient(
                f"could not check out the base to install it: {_tail(co.stderr, 300)}"
            )
        try:
            base_ok, base_detail = provision(self.worktree, deps_state)
        finally:
            back = _git(self.runner, self.worktree, "checkout", "--detach", head)
        if back.returncode != 0:
            return self._transient(
                f"could not return to the head: {_tail(back.stderr, 300)}"
            )
        if not base_ok:
            return self._environment(f"the base does not install: {base_detail}")
        return None

    def _needs_install(self) -> bool:
        """Will this task run the product's checks or acceptance entries?"""
        checks = self.meta.get("checks_command") or checks_command(
            self.config, self.worktree
        )
        return bool(checks) or (self.worktree / "acceptance").is_dir()

    def _provision(self, base_ref: str, deps_state: Path) -> dict | None:
        """Install dependencies for the rebased head, deciding whose fault a
        failure is. Returns an ending, or None when the head is installed.

        The BASE's manifests are installed first, in this same worktree (the
        install before the rebase already did that, so this is "current";
        the head's install is then "current" too unless the PR changed the
        lockfile or package.json). If the base does not install, the
        environment is at fault. If the base installs and the rebased head
        does not (twice, so a network blip is not blamed on the PR), the PR's
        own manifests are: package.json and the lockfile are PR-controlled
        and among the files a conflict resolution edits. That takes the
        escalation path like failing checks.
        """
        failure = self._install_base(base_ref, deps_state)
        if failure is not None:
            return failure
        provision = self._provisioner()
        ok, detail = provision(self.worktree, deps_state)
        if not ok:
            ok, detail = provision(self.worktree, deps_state)
        if ok:
            return None
        return self._escalate(
            "the PR's dependencies do not install after the rebase (the base's "
            "do): check package.json and the lockfile",
            _tail(detail, INSTALL_DETAIL_CHARS),
        )

    def _deps_state_dir(self) -> Path:
        """Where the install marker for this worktree lives (one per path)."""
        import hashlib

        key = hashlib.sha256(str(self.worktree).encode("utf-8")).hexdigest()[:16]
        return self.company_dir.resolve() / STATE_SUBDIR / "deps" / key

    def _escalate(self, why: str, detail: str = "") -> dict:
        cwd = self.project_root
        if self.pr:
            add_label(self.pr, LABEL_NEEDS_HUMAN, self.runner, cwd)
            post_comment(
                self.pr,
                f"<!-- forge-rebase task={self.task_id} escalated -->\n"
                f"**Auto-rebase onto `{self.base}` gave up — a human needs to "
                f"take this PR.**\n\n{why}\n\n"
                + (
                    f"<details><summary>output</summary>\n\n```\n{detail}\n```\n"
                    "</details>\n\n"
                    if detail
                    else ""
                )
                + "Conflicting files: "
                + (", ".join(f"`{f}`" for f in self.conflicted) or "none")
                + "\n\nThe lane makes one attempt per head; it will not retry "
                "until the PR's head changes.",
                self.runner,
                cwd,
            )
        escalation = None
        with contextlib.suppress(Exception):
            escalate = self.deps.escalate or _import("escalation").escalate
            escalation = escalate(
                task_id=self.task_id,
                reason="explicit_block",
                notes=f"[auto-rebase] PR #{self.pr} ({self.pr_url}): {why}"[:2000],
            )
        if self.pr:
            _update_pr(
                self.company_dir,
                self.pr,
                escalated_head=self.pre_head or self.meta.get("head_sha"),
                escalated_at=_iso(),
            )
        log_conflict(
            self.company_dir,
            {
                "event": "escalated",
                "repo": self.project_root.name,
                "pr": int(self.pr) if self.pr.isdigit() else self.pr,
                "branch": self.branch,
                "base": self.base,
                "head_sha": self.pre_head,
                "files": self.conflicted,
                "task_id": self.task_id,
                "why": why[:300],
            },
        )
        return self._finish(why, outcome="escalated", escalation=escalation)

    # -- the steps ---------------------------------------------------------

    def run(self) -> dict:
        if not self.pr or not safe_branch(self.branch, self.base):
            return self._finish(
                f"malformed rebase task (pr={self.pr!r}, branch={self.branch!r}, "
                f"base={self.base!r})",
                outcome="malformed",
            )
        if not self.execution_cwd:
            # Never rewrite a PR branch in the primary checkout.
            return self._transient("rebase tasks run only in a daemon worktree")
        self.worktree = Path(self.execution_cwd)
        view = _pr_state(self.pr, self.runner, self.project_root)
        if view is None:
            return self._transient("PR state unreadable")
        if str(view.get("state") or "").upper() != "OPEN":
            return self._finish(f"PR is {view.get('state')}", outcome="pr_closed")
        self.labels = {
            lbl.get("name") for lbl in view.get("labels") or [] if isinstance(lbl, dict)
        }
        if self.labels & HUMAN_LABELS:
            return self._finish("a human holds the PR", outcome="human_held")
        if view.get("baseRefName"):
            self.base = str(view["baseRefName"])
            if not safe_branch(self.branch, self.base):
                return self._finish("head branch is the base", outcome="malformed")

        if not fetch_refs(self.worktree, [self.base, self.branch], self.runner):
            return self._transient("fetch failed")
        co = _git(
            self.runner,
            self.worktree,
            "checkout",
            "--detach",
            "--force",
            f"origin/{self.branch}",
        )
        if co.returncode != 0:
            return self._transient(f"checkout failed: {_tail(co.stderr, 300)}")
        self.pre_head = _out(_git(self.runner, self.worktree, "rev-parse", "HEAD"))
        base_ref = f"origin/{self.base}"
        base_sha = _out(_git(self.runner, self.worktree, "rev-parse", base_ref))
        if not self.pre_head or not base_sha:
            return self._transient("HEAD or base unreadable")
        # Every later step works on the base as read here: a resolver session runs for
        # minutes, and the daemon's fetch moves origin/<base> in the shared refs meanwhile
        # (webapp#250, 2026-10-15: "does not contain its base"). A newer base is the next
        # tick's rebase.
        base_ref = base_sha
        _update_pr(
            self.company_dir,
            self.pr,
            attempted_add=attempt_key(self.pre_head, base_sha),
        )
        if (
            _git(
                self.runner,
                self.worktree,
                "merge-base",
                "--is-ancestor",
                base_ref,
                "HEAD",
            ).returncode
            == 0
        ):
            return self._finish("already up to date with its base", outcome="noop")

        # A broken environment (the base itself does not install) stops here,
        # before any resolution session is spent and before anything is pushed.
        if self._needs_install():
            failure = self._install_base(base_ref, self._deps_state_dir())
            if failure is not None:
                return failure

        pre_files = changed_files(self.worktree, base_ref, "HEAD", self.runner)
        subjects = _out(
            _git(
                self.runner, self.worktree, "log", "--format=- %s", f"{base_ref}..HEAD"
            )
        )

        rb = _git(self.runner, self.worktree, "rebase", base_ref, timeout=300)
        mode = "rebase"
        if rb.returncode != 0:
            # Conflicts: re-apply the PR once, as one commit on the base.
            mode = "squash"
            _git(self.runner, self.worktree, "rebase", "--abort")
            _git(
                self.runner, self.worktree, "checkout", "--detach", "--force", base_ref
            )
            _git(
                self.runner,
                self.worktree,
                "merge",
                "--squash",
                "--no-commit",
                self.pre_head,
            )
            self.conflicted = self._unmerged()
            if not self.conflicted:
                return self._escalate(
                    "the rebase stopped but no conflicting file was found",
                    _tail((rb.stdout or "") + (rb.stderr or "")),
                )
            outcome = self._resolve(base_sha, pre_files)
            if outcome is not None:
                return outcome
            msg = (
                f"rebase: PR #{self.pr} onto {self.base} ({base_sha[:12]})\n\n"
                f"Re-applied {self.branch} at {self.pre_head[:12]} as one commit on "
                f"{self.base}; conflicts resolved (both sides kept) in:\n"
                + "\n".join(f"- {f}" for f in self.conflicted)
                + f"\n\nOriginal commits:\n{subjects}\n"
            )
            committed = self._commit(msg)
            if committed is not None:
                return committed

        failure = self._verify(base_ref)
        if failure is not None:
            return failure

        return self._push_and_settle(base_sha, mode)

    def _unmerged(self) -> list[str]:
        res = _git(self.runner, self.worktree, "diff", "--name-only", "--diff-filter=U")
        return [ln.strip() for ln in _out(res).splitlines() if ln.strip()]

    def _snapshot(self) -> dict[str, str]:
        """Content hash of every path git reports as changed vs HEAD."""
        res = _git(
            self.runner, self.worktree, "status", "--porcelain", "--untracked-files=all"
        )
        snap: dict[str, str] = {}
        for line in (res.stdout or "").splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip().strip('"')
            full = self.worktree / path
            try:
                snap[path] = str(hash(full.read_bytes()))
            except OSError:
                snap[path] = "<missing>"
        return snap

    def _resolve(self, base_sha: str, pre_files: list[str]) -> dict | None:
        """One session resolves the conflicts. Returns an ending, or None."""
        cs = _import("challenger_stage")
        session = self.deps.session or cs.run_session
        entry_ids = list(
            dict.fromkeys(
                list(self.meta.get("entry_ids") or [])
                + entries_owning(self.worktree, self.conflicted)
            )
        )
        self.meta["entry_ids"] = entry_ids
        checks = self.meta.get("checks_command") or checks_command(
            self.config, self.worktree
        )
        brief = render_brief(
            pr=self.pr,
            pr_url=self.pr_url,
            branch=self.branch,
            base=self.base,
            base_sha=base_sha,
            head_sha=self.pre_head,
            reason=str(self.meta.get("reason") or "conflicting"),
            conflicted=self.conflicted,
            entry_ids=entry_ids,
            checks=checks,
        )

        def _prompt_for(scratch: Path) -> str:
            return (
                brief
                + "\n## Your part\n\nThe worktree is mid-merge: the PR's change is "
                f"staged on top of `{self.base}` at {base_sha[:12]} and these files hold "
                "conflict markers:\n"
                + "\n".join(f"- `{f}`" for f in self.conflicted)
                + "\n\nFor each file, read both sides (`git diff`, `git show "
                f"{base_sha[:12]}:<file>`, `git show {self.pre_head[:12]}:<file>`), "
                "then edit it so BOTH sides' intent holds and no marker is left. "
                "Then run the checks and the entries above and fix what the merge "
                "broke. Do NOT run git add, commit, rebase, merge or push. If the "
                "two sides cannot both hold, leave the markers in place and write "
                f"why to `{scratch}/rebase-report.md`.\n"
            )

        before = self._snapshot()
        scratch = Path(
            tempfile.mkdtemp(
                prefix="fs-rb-",
                dir=str(self.deps.scratch_root) if self.deps.scratch_root else None,
            )
        )
        result = session(
            _prompt_for,
            cwd=self.worktree,
            task_id=self.task_id,
            timeout_seconds=self.config.timeout_seconds,
            model=self.config.model,
            employee_id=ACTOR,
            system_reinforcement=SYSTEM_REINFORCEMENT,
            scratch_dir=scratch,
            report_name="rebase-report.md",
        )
        if not getattr(result, "ran", False):
            return self._transient(
                f"session did not start: {getattr(result, 'error', '')}"
            )
        note = getattr(result, "report_text", None) or ""
        if not getattr(result, "ok", False):
            return self._escalate(
                f"the conflict-resolution session failed: "
                f"{getattr(result, 'error', '')}",
                _tail(note),
            )
        leftover = [f for f in self.conflicted if self._has_markers(f)]
        if leftover:
            return self._escalate(
                "conflict markers left in " + ", ".join(leftover), _tail(note)
            )
        after = self._snapshot()
        touched = [p for p in after if before.get(p) != after[p]] + [
            p for p in before if p not in after
        ]
        extra = [p for p in touched if p not in self.conflicted]
        bad = self._forbidden(extra)
        if bad:
            return self._escalate(
                "the session changed paths a rebase must not touch: "
                + ", ".join(bad[:10])
            )
        return None

    def _has_markers(self, rel: str) -> bool:
        try:
            text = (self.worktree / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False  # deleted on purpose (a delete/modify resolution)
        return bool(_MARKER_RE.search(text))

    def _forbidden(self, paths: list[str]) -> list[str]:
        bad = [
            p
            for p in paths
            if p in _FORBIDDEN_FILES or p.startswith(_FORBIDDEN_PREFIXES)
        ]
        try:
            config = _read_project_config(self.project_root)
            bad += _import("employee_activator")._human_protected_violations(
                paths, config
            )
        except Exception:  # noqa: BLE001 — fail closed on extra paths only
            if paths:
                bad += ["(human-protected check unavailable)"]
        return list(dict.fromkeys(bad))

    def _commit(self, message: str) -> dict | None:
        status = _git(
            self.runner, self.worktree, "status", "--porcelain", "--untracked-files=all"
        )
        paths = []
        for line in (status.stdout or "").splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip().strip('"')
            if not path.startswith(_UNCOMMITTED_PREFIXES):
                paths.append(path)
        if paths:
            add = _git(self.runner, self.worktree, "add", "-A", "--", *paths)
            if add.returncode != 0:
                return self._escalate(f"git add failed: {_tail(add.stderr, 300)}")
        if self._unmerged():
            return self._escalate(
                "unmerged paths remain: " + ", ".join(self._unmerged())
            )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix="rebase-msg-", delete=False, encoding="utf-8"
        ) as f:
            f.write(message)
            msg_path = f.name
        try:
            commit = _git(
                self.runner, self.worktree, "commit", "--no-verify", "-F", msg_path
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(msg_path)
        if commit.returncode != 0:
            return self._escalate(
                "git commit failed (the resolution may have emptied the PR)",
                _tail((commit.stdout or "") + (commit.stderr or "")),
            )
        return None

    def _verify(self, base_ref: str) -> dict | None:
        """The product's checks and the entries, on the rebased head."""
        if (
            _git(
                self.runner,
                self.worktree,
                "merge-base",
                "--is-ancestor",
                base_ref,
                "HEAD",
            ).returncode
            != 0
        ):
            return self._escalate("the rebased head does not contain its base")
        checks = self.meta.get("checks_command") or checks_command(
            self.config, self.worktree
        )
        entry_ids = list(
            dict.fromkeys(
                list(self.meta.get("entry_ids") or [])
                + entries_owning(self.worktree, self.conflicted)
            )
        )
        has_entries = bool(entry_ids) and (self.worktree / "acceptance").is_dir()
        deps_state = self._deps_state_dir()
        if checks or has_entries:
            failure = self._provision(base_ref, deps_state)
            if failure is not None:
                return failure
        if checks:
            run = self.deps.checks or run_checks
            ok, output = run(
                checks, self.worktree, self.config.checks_timeout_minutes * 60
            )
            if not ok:
                return self._escalate(
                    f"the product's checks fail after the rebase (`{checks}`)", output
                )
        if has_entries:
            cs = _import("challenger_stage")
            ledger = self.deps.ledger or cs.run_goal_entries
            history = self.company_dir.resolve() / "state" / "acceptance" / "runs.jsonl"
            # The same state dir as the install above: run_goal_entries keeps
            # only the install marker there, so it finds the install current
            # instead of running a second clean install.
            oracle = ledger(
                self.worktree,
                entry_ids,
                deps_state,
                history if history.exists() else None,
            )
            if not cs.entries_all_pass(oracle, entry_ids):
                results = (oracle or {}).get("results") or {}
                failing = [
                    f"{i}: {(results.get(i) or {}).get('verdict', 'not run')}"
                    for i in entry_ids
                    if (results.get(i) or {}).get("verdict") != "CONSISTENT"
                ]
                env = (oracle or {}).get("environment_error")
                if (
                    env
                    and not results
                    and str(env).startswith("dependency install failed")
                ):
                    # The entries' own install failed although the base and
                    # the head installed above: nothing ran, the environment
                    # broke in between. A manifest or entry that fails to LOAD
                    # is the PR's (or its resolution's) and escalates below.
                    return self._environment(f"acceptance entries: {env}")
                return self._escalate(
                    "acceptance entries are not CONSISTENT after the rebase: "
                    + "; ".join(failing[:10])
                    + (f" (environment: {env})" if env else "")
                )
        return None

    def _push_and_settle(self, base_sha: str, mode: str) -> dict:
        new_head = _out(_git(self.runner, self.worktree, "rev-parse", "HEAD"))
        if not safe_branch(self.branch, self.base):
            return self._escalate("refusing to push to a base branch")
        push = _git(
            self.runner,
            self.worktree,
            "push",
            f"--force-with-lease=refs/heads/{self.branch}:{self.pre_head}",
            "origin",
            f"HEAD:refs/heads/{self.branch}",
            timeout=300,
        )
        if push.returncode != 0:
            now = remote_sha(self.worktree, self.branch, self.runner)
            if now and now != self.pre_head:
                # Someone pushed meanwhile: their head wins; the next tick
                # looks at it fresh.
                return self._finish(
                    "the PR branch moved during the rebase; not pushed",
                    outcome="superseded",
                )
            return self._escalate(
                "push refused", _tail((push.stdout or "") + (push.stderr or ""))
            )

        cwd = self.project_root
        # The judge's verdict belongs to the old head.
        with contextlib.suppress(Exception):
            hold = (
                self.deps.hold_for_new_head or _import("verdict_hold").hold_for_new_head
            )
            hold(self.pr, self.runner, str(cwd))
        verdict = self._request_fresh_review()
        with contextlib.suppress(Exception):
            remove_label(self.pr, LABEL_READY, self.runner, cwd)
        with contextlib.suppress(Exception):
            remove_label(self.pr, LABEL_ENVIRONMENT, self.runner, cwd)
        post_comment(
            self.pr,
            f"<!-- forge-rebase task={self.task_id} pushed={new_head} -->\n"
            f"**Rebased onto `{self.base}` ({base_sha[:12]})** — "
            f"{'clean rebase' if mode == 'rebase' else 're-applied as one commit, conflicts resolved'}"
            f"; pushed `{new_head[:12]}` (was `{(self.pre_head or '')[:12]}`).\n\n"
            "Conflicting files: "
            + (", ".join(f"`{f}`" for f in self.conflicted) or "none")
            + f"\n\nChallenger verdict: {verdict}.\n",
            self.runner,
            cwd,
        )
        with _locked_state(self.company_dir) as state:
            rec = state["prs"].setdefault(self.pr, {})
            rec["rebases"] = int(rec.get("rebases", 0)) + 1
            rec["last_rebased_head"] = new_head
            rec["attempted"] = (rec.get("attempted") or []) + [
                attempt_key(new_head, base_sha)
            ]
        log_conflict(
            self.company_dir,
            {
                "event": "resolved",
                "repo": self.project_root.name,
                "pr": int(self.pr),
                "branch": self.branch,
                "base": self.base,
                "base_sha": base_sha,
                "head_sha": self.pre_head,
                "new_head_sha": new_head,
                "mode": mode,
                "files": self.conflicted,
                # Always False: no verdict is carried across a rebase. Kept
                # so conflict-log readers see the same row shape.
                "carry": False,
                "task_id": self.task_id,
            },
        )
        return self._finish(
            f"rebased and pushed {new_head[:12]}; challenger: {verdict}",
            outcome="rebased",
            new_head=new_head,
            carry=False,
            mode=mode,
        )

    def _request_fresh_review(self) -> str:
        """Put a challenger-passed PR back under review for the new head.

        Nothing is carried: the stage record keeps naming the old head, so
        ``challenger_stage.merge_block_reason`` holds the new head and the
        challenger's own tick queues the next round (bounded by its
        re-review limit, then ``challenger:needs-human``). The judge re-runs
        when that round passes. Returns a line for the PR comment."""
        try:
            cs = _import("challenger_stage")
        except Exception:  # noqa: BLE001
            return "challenger stage unavailable (not changed)"
        with cs._locked_state(self.company_dir) as state:
            key = next(
                (url for url in state["prs"] if cs.pr_number(url) == self.pr),
                None,
            )
            rec = state["prs"].get(key) if key else None
            passed = bool(rec) and rec.get("phase") == "passed"
        if not passed:
            return "no challenger pass to re-review"
        cs.set_stage_label(self.pr, cs.LABEL_PENDING, self.runner, self.project_root)
        return (
            "back to `challenger:pending` for a fresh review (a rebased head is "
            "new code; no verdict is carried across a rebase)"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _company_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    with contextlib.suppress(Exception):
        return Path(_import("company_resolver").get_company_dir())
    return Path(".company")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pr_rebase.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "tick"):
        p = sub.add_parser(name)
        p.add_argument("--company-dir", default=None)
    args = parser.parse_args(argv)
    company_dir = _company_dir(args.company_dir)
    if args.cmd == "tick":
        print(json.dumps(tick(company_dir), indent=2))
        return 0
    state = read_state(company_dir)
    config = load_config(company_dir.resolve().parent)
    print(
        f"autoRebase.enabled={config.enabled} maxConcurrent={config.max_concurrent} "
        f"includeLabelled={config.include_labelled} dispatch_wired={dispatch_wired()}"
    )
    for pr, rec in sorted(state["prs"].items()):
        last = (rec.get("history") or [{}])[-1]
        print(
            f"PR #{pr}: rebases={rec.get('rebases', 0)} "
            f"escalated_head={(rec.get('escalated_head') or '-')[:12]} "
            f"last={last.get('event', '-')} {last.get('at', '')}"
        )
    hot = hotspot_counts(company_dir).most_common(10)
    if hot:
        print("Conflict hotspots: " + ", ".join(f"{f} ({n})" for f, n in hot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
