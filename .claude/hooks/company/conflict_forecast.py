#!/usr/bin/env python3
"""Conflict-aware scheduling of goal builds (CONFLICT-AWARE-SCHEDULING).

Why this exists (webapp, 2026-10-10..13)
----------------------------------------
Parallel goal builds (G43..G49) kept conflicting with each other on the same
few files: append-only docs (SPEC.md, docs/VERSIONS.md, ARCHITECTURE.md), the
i18n catalog, constants.ts, the canary mock app. AUTO-REBASE-CONFLICTING-PRS
(``pr_rebase.py``) repairs those conflicts after the fact and records each one
in ``.company/state/conflict_log.jsonl``. This module prevents the predictable
ones: two builds that will both touch a known hotspot file are not run at the
same time; the second waits until the first one's PR merges.

The three parts
---------------
1. **Prediction** (:func:`predict_task_files`). The files a queued goal build
   will touch, each with the source that named it:

   * ``entry:<ID>`` — the goal's acceptance entries (ids from its ledger
     success metric): the case file, every repository file the entry names
     (its ``file``, canary flows, mock app ...);
   * ``flow:<file>`` — the product files those flow files (code, not docs)
     name: relative imports (``./flows``) and path tokens, followed two
     levels deep;
   * ``spec`` — files named in the goal's spec section (the same excerpt the
     challenger reads, split ``spec/decided/*.md`` included);
   * ``pr:#<n>`` — every file of the last ``recentPrs`` merged PRs in the
     same product area (the PR names the goal id, or touched one of the
     goal's non-hotspot predicted files);
   * ``alwaysTouched`` — files the product declares every build touches
     (e.g. a docs-currency rule: docs/VERSIONS.md in every PR).

   Path tokens only count when they resolve to a tracked file (``git
   ls-files``), exactly or by a unique path suffix (``i18n.ts`` ->
   ``src/i18n.ts``). Any other task (not a goal build) is predicted from the
   paths its title and description name.
2. **Hotspots** (:func:`hotspots`). Configured globs
   (``autonomy.conflictAware.hotspots``; a glob without ``/`` also matches a
   basename anywhere) plus files LEARNED from ``conflict_log.jsonl``: a file
   is a learned hotspot while it has at least ``learnMinConflicts`` (2)
   ``conflict`` rows in the last ``learnWindowDays`` (30) — distinct by
   (PR, base sha) — AND its decayed score (each conflict weighs
   ``0.5 ** (age_days / learnHalfLifeDays)``) is at least
   ``learnMinScore``. Old conflicts fade out; a file that keeps conflicting
   stays hot.
3. **The gate** (:func:`gate` / :func:`daemon_gate`). Before the daemon picks
   a pending task, every pending goal build is checked against the blockers:
   the builds in flight (queue ``in_progress`` plus the daemon's reserved /
   executing workers) and the open daemon PRs (``--author @me`` on
   ``daemon/*``, their real file lists). If the build's predicted files and a
   blocker's files share a HOTSPOT file, the build is deferred: it stays
   pending with ``conflict_deferred.reason`` =
   ``waits for #<pr> (shared hotspot <file>)`` (or ``waits for task <id>``
   while the blocker has no PR yet). A shared non-hotspot file never defers.
   A build is never deferred longer than ``maxDeferHours`` (6) in total; it
   then dispatches (``max_defer``). Released and expired builds are moved to
   the FRONT of the pending order, oldest first deferral first, so when a
   blocker merges the build that waited longest goes next and later builds on
   the same hotspot wait behind it (no starvation).

Every decision is appended to ``.company/state/conflict_forecast.jsonl``
(``event`` = ``clear`` | ``defer`` | ``release`` | ``max_defer``), once per
change, never once per daemon cycle. State (first-deferral times, the last
logged decision per task) lives in ``.company/state/conflict_forecast/
state.json``.

Config (``autonomy.conflictAware`` in gatesmith.json; defaults in code):
``enabled`` (false), ``hotspots`` ([]), ``maxDeferHours`` (6),
``alwaysTouched`` ([]), ``recentPrs`` (5), ``learnWindowDays`` (30),
``learnMinConflicts`` (2), ``learnHalfLifeDays`` (10), ``learnMinScore``
(0.5).

Daemon wiring: :func:`daemon_gate` reorders/filters the pending list the
daemon hands to ``_pick_next_pending_task`` (``forge_daemon.py``, protected —
applied by the operator). Until then nothing here runs in the daemon;
:func:`gate_wired` reports whether it does.

CLI::

    conflict_forecast.py status [--company-dir .company]
    conflict_forecast.py predict --task-id <id> [--company-dir .company]
    conflict_forecast.py gate [--company-dir .company]     # dry run, no writes
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
Runner = Callable[..., Any]

CONFLICT_LOG = Path("state") / "conflict_log.jsonl"
DECISION_LOG = Path("state") / "conflict_forecast.jsonl"
STATE_SUBDIR = Path("state") / "conflict_forecast"
STATE_FILE = "state.json"
STATE_LOCK = "state.lock"
QUEUE_FIELD = "conflict_deferred"
OWN_BRANCH_PREFIX = "daemon/"

GH_TIMEOUT = 30
GIT_TIMEOUT = 30
OPEN_PR_TTL = 120.0
MERGED_PR_TTL = 1800.0
INDEX_TTL = 300.0
PREDICTION_TTL = 300.0
PR_LIST_LIMIT = 100
MERGED_LIST_LIMIT = 60
FLOW_DEPTH = 2
MAX_FLOW_FILES = 60
# A flow file naming more files than this is a hub (an index re-exporting
# every mock defect): what it names says nothing about this goal.
HUB_FANOUT = 12
# A file more acceptance entries name than this is shared infrastructure
# (the canary mock app): it does not put a merged PR in a goal's area.
AREA_MAX_ENTRY_DF = 3
FILE_READ_LIMIT = 400_000
WALK_LIMIT = 20_000

_PATH_TOKEN_RE = re.compile(
    r"(?<![\w@/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])"
)
_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s+|\brequire\(\s*|\bimport\(\s*)['"](\.{1,2}/[^'"\s]+)['"]"""
)
_IMPORT_EXTS = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    "/index.ts",
    "/index.js",
)
# Only code files are followed as flows: a document an entry cites (SPEC.md)
# names every file in the product, not the ones this goal's flow drives.
_FLOW_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".sh")
_WALK_SKIP = {
    ".git",
    "node_modules",
    ".company",
    ".venv",
    "dist",
    "build",
    "__pycache__",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _import(name: str):
    """Sibling module, package-relative first (house pattern)."""
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


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run(runner: Runner | None, args: list[str], cwd: Path | None, timeout: int):
    run = runner or subprocess.run
    try:
        return run(
            args,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args, 1, "", f"{type(exc).__name__}: {exc}")


def pr_number(value: Any) -> str | None:
    if isinstance(value, int):
        return str(value)
    text = str(value or "")
    if text.isdigit():
        return text
    m = re.search(r"/pull/(\d+)", text)
    return m.group(1) if m else None


def _company_dir(company_dir: Path | str | None) -> Path:
    if company_dir is None:
        with contextlib.suppress(Exception):
            return Path(_import("company_resolver").get_company_dir())
        return Path(".company")
    with contextlib.suppress(Exception):
        return Path(_import("company_resolver").hermetic_redirect(company_dir))
    return Path(company_dir)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _strs(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


def _num(raw: dict, key: str, default: float, minimum: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class ConflictAwareConfig:
    enabled: bool = False
    hotspots: tuple[str, ...] = ()
    max_defer_hours: float = 6.0
    always_touched: tuple[str, ...] = ()
    recent_prs: int = 5
    learn_window_days: float = 30.0
    learn_min_conflicts: int = 2
    learn_half_life_days: float = 10.0
    learn_min_score: float = 0.5

    @classmethod
    def from_dict(cls, raw: Any) -> ConflictAwareConfig:
        if not isinstance(raw, dict):
            return cls()
        d = cls()
        return cls(
            enabled=raw.get("enabled") is True,
            hotspots=_strs(raw.get("hotspots")),
            max_defer_hours=float(_num(raw, "maxDeferHours", d.max_defer_hours, 0)),
            always_touched=_strs(raw.get("alwaysTouched")),
            recent_prs=int(_num(raw, "recentPrs", d.recent_prs, 0)),
            learn_window_days=float(
                _num(raw, "learnWindowDays", d.learn_window_days, 1)
            ),
            learn_min_conflicts=int(
                _num(raw, "learnMinConflicts", d.learn_min_conflicts, 1)
            ),
            learn_half_life_days=float(
                _num(raw, "learnHalfLifeDays", d.learn_half_life_days, 0.1)
            ),
            learn_min_score=float(_num(raw, "learnMinScore", d.learn_min_score, 0)),
        )


def load_config(project_root: Path) -> ConflictAwareConfig:
    try:
        data = json.loads(
            (Path(project_root) / "gatesmith.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        data = {}
    autonomy = data.get("autonomy") if isinstance(data, dict) else None
    raw = autonomy.get("conflictAware") if isinstance(autonomy, dict) else None
    return ConflictAwareConfig.from_dict(raw)


# ---------------------------------------------------------------------------
# Hotspots: configured globs + learned from conflict_log.jsonl
# ---------------------------------------------------------------------------


def read_conflict_log(company_dir: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        text = (Path(company_dir) / CONFLICT_LOG).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def learned_hotspots(
    company_dir: Path,
    config: ConflictAwareConfig,
    now: datetime | None = None,
    repo: str | None = None,
) -> dict[str, dict]:
    """``{file: {count, score, last}}`` for every learned hotspot.

    A ``conflict`` row counts once per (PR, base sha, file) inside the
    window; its weight halves every ``learn_half_life_days``. A file is hot
    while both its count and its decayed score clear the thresholds."""
    now = now or _now()
    seen: set[tuple] = set()
    stats: dict[str, dict] = {}
    for row in read_conflict_log(company_dir):
        if row.get("event") != "conflict":
            continue
        if repo and row.get("repo") and row.get("repo") != repo:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            continue
        age_days = max(0.0, (now - ts).total_seconds() / 86400)
        if age_days > config.learn_window_days:
            continue
        weight = 0.5 ** (age_days / config.learn_half_life_days)
        attempt = (row.get("pr"), row.get("base_sha") or row.get("ts"))
        for f in row.get("files") or []:
            if not isinstance(f, str) or not f:
                continue
            key = (*attempt, f)
            if key in seen:
                continue
            seen.add(key)
            st = stats.setdefault(f, {"count": 0, "score": 0.0, "last": None})
            st["count"] += 1
            st["score"] += weight
            if st["last"] is None or ts.isoformat() > st["last"]:
                st["last"] = ts.isoformat()
    return {
        f: {**st, "score": round(st["score"], 3)}
        for f, st in stats.items()
        if st["count"] >= config.learn_min_conflicts
        and st["score"] >= config.learn_min_score
    }


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch (``*`` crosses ``/``); a pattern without ``/`` also matches
    the basename anywhere (``VERSIONS.md`` == ``docs/VERSIONS.md``)."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)


@dataclass
class Hotspots:
    globs: tuple[str, ...] = ()
    learned: dict[str, dict] = field(default_factory=dict)

    def why(self, path: str) -> str | None:
        """'configured <glob>' | 'learned (n conflicts)' | None."""
        if path in self.learned:
            return f"learned ({self.learned[path]['count']} conflicts)"
        for g in self.globs:
            if glob_match(path, g):
                return f"configured {g}"
        return None

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and self.why(path) is not None


def hotspots(
    company_dir: Path,
    config: ConflictAwareConfig,
    now: datetime | None = None,
    repo: str | None = None,
) -> Hotspots:
    return Hotspots(
        globs=config.hotspots,
        learned=learned_hotspots(company_dir, config, now=now, repo=repo),
    )


# ---------------------------------------------------------------------------
# The repository index (tracked files) and path-token resolution
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, tuple[float, RepoIndex]] = {}
_PR_CACHE: dict[tuple[str, str], tuple[float, list[dict] | None]] = {}
_PREDICTION_CACHE: dict[tuple[str, str], tuple[float, Prediction]] = {}


def reset_caches() -> None:
    with _CACHE_LOCK:
        _INDEX_CACHE.clear()
        _PR_CACHE.clear()
        _PREDICTION_CACHE.clear()
        _DF_CACHE.clear()


class RepoIndex:
    def __init__(self, files: Iterable[str]):
        self.files = {f for f in files if f}
        self._by_name: dict[str, list[str]] = {}
        for f in self.files:
            self._by_name.setdefault(PurePosixPath(f).name, []).append(f)

    def resolve(self, token: str) -> str | None:
        token = token.strip()
        if token.startswith("./"):
            token = token[2:]
        if not token:
            return None
        if token in self.files:
            return token
        name = PurePosixPath(token).name
        hits = [
            f
            for f in self._by_name.get(name, [])
            if f == token or f.endswith("/" + token)
        ]
        return hits[0] if len(hits) == 1 else None

    def tokens(self, text: str) -> set[str]:
        out: set[str] = set()
        for m in _PATH_TOKEN_RE.finditer(text or ""):
            hit = self.resolve(m.group(1))
            if hit:
                out.add(hit)
        return out


def _walk(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _WALK_SKIP]
        for name in filenames:
            out.append(str(Path(dirpath, name).relative_to(root).as_posix()))
            if len(out) >= WALK_LIMIT:
                return out
    return out


def repo_index(project_root: Path, runner: Runner | None = None) -> RepoIndex:
    key = str(Path(project_root).resolve())
    with _CACHE_LOCK:
        hit = _INDEX_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    res = _run(runner, ["git", "-C", key, "ls-files"], None, GIT_TIMEOUT)
    if res.returncode == 0 and (res.stdout or "").strip():
        files = (res.stdout or "").splitlines()
    else:
        files = _walk(Path(project_root))
    index = RepoIndex(files)
    with _CACHE_LOCK:
        _INDEX_CACHE[key] = (time.monotonic() + INDEX_TTL, index)
    return index


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")[
            :FILE_READ_LIMIT
        ]
    except OSError:
        return ""


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _relative_imports(text: str, from_file: str, index: RepoIndex) -> set[str]:
    out: set[str] = set()
    base = PurePosixPath(from_file).parent
    for m in _IMPORT_RE.finditer(text):
        joined = os.path.normpath(str(base / m.group(1))).replace(os.sep, "/")
        if joined.startswith(".."):
            continue
        for ext in _IMPORT_EXTS:
            if joined + ext in index.files:
                out.add(joined + ext)
                break
    return out


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


@dataclass
class Prediction:
    task_id: str
    goal: str | None = None
    files: dict[str, list[str]] = field(default_factory=dict)  # file -> sources

    def add(self, path: str, source: str) -> None:
        if not path:
            return
        srcs = self.files.setdefault(path, [])
        if source not in srcs:
            srcs.append(source)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "files": {k: v for k, v in sorted(self.files.items())},
        }


def _entry_files(project_root: Path) -> dict[str, tuple[str, dict]]:
    out: dict[str, tuple[str, dict]] = {}
    for sub in ("cases", "relations"):
        folder = Path(project_root) / "acceptance" / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                out[data["id"]] = (f"acceptance/{sub}/{path.name}", data)
    return out


_DF_CACHE: dict[str, tuple[float, dict[str, int]]] = {}


def _entry_df(project_root: Path, index: RepoIndex) -> dict[str, int]:
    """How many acceptance entries name each file (cached)."""
    key = str(Path(project_root).resolve())
    with _CACHE_LOCK:
        hit = _DF_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    df: dict[str, int] = {}
    for _case_file, data in _entry_files(project_root).values():
        named: set[str] = set()
        for text in _strings(data):
            named |= index.tokens(text)
        for f in named:
            df[f] = df.get(f, 0) + 1
    with _CACHE_LOCK:
        _DF_CACHE[key] = (time.monotonic() + INDEX_TTL, df)
    return df


def _goal_text(goal: Any) -> str:
    return " ".join(
        str(getattr(goal, a, "") or "")
        for a in ("name", "description", "success_metric")
    )


def _spec_text(project_root: Path) -> str:
    cs = _import("challenger_stage")
    try:
        text = (Path(project_root) / "SPEC.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    return cs.with_split_spec(Path(project_root), text)


def _list_prs(
    runner: Runner | None, project_root: Path, state: str, ttl: float
) -> list[dict] | None:
    key = (str(Path(project_root).resolve()), state)
    with _CACHE_LOCK:
        hit = _PR_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    if state == "open":
        fields = "number,title,headRefName,files,createdAt"
        extra = ["--author", "@me", "--limit", str(PR_LIST_LIMIT)]
    else:
        fields = "number,title,headRefName,files,mergedAt"
        extra = ["--limit", str(MERGED_LIST_LIMIT)]
    res = _run(
        runner,
        ["gh", "pr", "list", "--state", state, *extra, "--json", fields],
        project_root,
        GH_TIMEOUT,
    )
    prs: list[dict] | None = None
    if res.returncode == 0:
        with contextlib.suppress(ValueError):
            data = json.loads(res.stdout or "[]")
            if isinstance(data, list):
                prs = [p for p in data if isinstance(p, dict) and "number" in p]
    with _CACHE_LOCK:
        # A failed call is cached briefly too, so a gh outage costs one call
        # per TTL, not one per daemon cycle.
        _PR_CACHE[key] = (time.monotonic() + (ttl if prs is not None else 60.0), prs)
    return prs


def pr_files(pr: dict) -> list[str]:
    out: list[str] = []
    for f in pr.get("files") or []:
        path = f.get("path") if isinstance(f, dict) else f
        if isinstance(path, str) and path:
            out.append(path)
    return out


def open_daemon_prs(runner: Runner | None, project_root: Path) -> list[dict] | None:
    prs = _list_prs(runner, project_root, "open", OPEN_PR_TTL)
    if prs is None:
        return None
    return [
        p for p in prs if str(p.get("headRefName") or "").startswith(OWN_BRANCH_PREFIX)
    ]


def merged_prs(runner: Runner | None, project_root: Path) -> list[dict]:
    prs = _list_prs(runner, project_root, "merged", MERGED_PR_TTL) or []
    return sorted(prs, key=lambda p: str(p.get("mergedAt") or ""), reverse=True)


def _goal_of(task: dict, company_dir: Path, goals: dict | None) -> Any | None:
    with contextlib.suppress(Exception):
        return _import("challenger_stage").product_goal(task, company_dir, goals)
    return None


def predict_task_files(
    task: dict,
    company_dir: Path,
    project_root: Path,
    *,
    config: ConflictAwareConfig,
    hot: Hotspots | None = None,
    goals: dict | None = None,
    runner: Runner | None = None,
    index: RepoIndex | None = None,
    merged: list[dict] | None = None,
) -> Prediction:
    """The files ``task`` is predicted to touch (see the module docstring)."""
    project_root = Path(project_root)
    index = index or repo_index(project_root, runner)
    hot = hot or Hotspots(globs=config.hotspots)
    pred = Prediction(task_id=str(task.get("task_id") or ""))
    # Every task: the paths its own brief names.
    brief = f"{task.get('title') or ''}\n{task.get('description') or ''}"
    for f in index.tokens(brief):
        pred.add(f, "brief")
    goal = _goal_of(task, company_dir, goals)
    if goal is None:
        return pred
    pred.goal = getattr(goal, "id", None)

    # 1. Acceptance entries and the files their flows name.
    ids: list[str] = []
    with contextlib.suppress(Exception):
        ids = list(_import("challenger_stage").goal_entry_ids(goal))
    entries = _entry_files(project_root) if ids else {}
    flows: list[str] = []
    for entry_id in ids:
        found = entries.get(entry_id)
        if not found:
            continue
        case_file, data = found
        pred.add(case_file, f"entry:{entry_id}")
        for s in _strings(data):
            for f in index.tokens(s):
                pred.add(f, f"entry:{entry_id}")
                if f != case_file and f.endswith(_FLOW_EXTS):
                    flows.append(f)
    seen: set[str] = set()
    frontier = list(dict.fromkeys(flows))
    for _depth in range(FLOW_DEPTH):
        nxt: list[str] = []
        for flow in frontier:
            if flow in seen or len(seen) >= MAX_FLOW_FILES:
                continue
            seen.add(flow)
            text = _read(project_root, flow)
            if not text:
                continue
            named = (_relative_imports(text, flow, index) | index.tokens(text)) - {flow}
            if len(named) > HUB_FANOUT:
                continue
            for f in sorted(named):
                pred.add(f, f"flow:{flow}")
                if f.endswith(_FLOW_EXTS):
                    nxt.append(f)
        frontier = nxt

    # 2. The goal's spec section.
    with contextlib.suppress(Exception):
        cs = _import("challenger_stage")
        excerpt = cs.spec_excerpt(_spec_text(project_root), _goal_text(goal), 10**7)
        if not excerpt.startswith("("):
            for f in index.tokens(excerpt):
                pred.add(f, "spec")

    # 3. Declared always-touched files.
    for pattern in config.always_touched:
        if any(ch in pattern for ch in "*?["):
            for f in sorted(index.files):
                if glob_match(f, pattern):
                    pred.add(f, "alwaysTouched")
        else:
            pred.add(index.resolve(pattern) or pattern, "alwaysTouched")

    # 4. The last N merged PRs in the same product area. The area is the
    # goal id plus the goal's own code files (named by its entries, its spec
    # section or its brief) that are not shared: never a hotspot, a document
    # or a file many entries name (VERSIONS.md, SPEC.md, the canary mock app
    # are in every PR, and would put every PR in every area).
    if config.recent_prs > 0:
        df = _entry_df(project_root, index)
        area = {
            f
            for f, srcs in pred.files.items()
            if f.endswith(_FLOW_EXTS)
            and f not in hot
            and df.get(f, 0) <= AREA_MAX_ENTRY_DF
            and any(s.split(":")[0] in ("entry", "spec", "brief") for s in srcs)
        }
        goal_re = re.compile(rf"(?<![A-Za-z0-9]){re.escape(pred.goal or '')}(?![0-9])")
        merged = merged if merged is not None else merged_prs(runner, project_root)
        picked = 0
        for pr in merged:
            if picked >= config.recent_prs:
                break
            files = pr_files(pr)
            names = f"{pr.get('title') or ''} {pr.get('headRefName') or ''}"
            if (pred.goal and goal_re.search(names)) or area.intersection(files):
                picked += 1
                for f in files:
                    pred.add(f, f"pr:#{pr.get('number')}")
    return pred


# ---------------------------------------------------------------------------
# State and the decision log
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
    if not isinstance(data.get("tasks"), dict):
        data["tasks"] = {}
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cf_", suffix=".tmp")
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


def log_decision(company_dir: Path, row: dict) -> None:
    path = Path(company_dir) / DECISION_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _iso(), **row}, sort_keys=True) + "\n")
    except OSError:
        pass


def read_decisions(company_dir: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        text = (Path(company_dir) / DECISION_LOG).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class Blocker:
    label: str  # "#212" or "task <id>"
    kind: str  # "pr" | "task"
    files: set[str]
    ref: str  # PR number or task id
    since: str = ""  # PR createdAt / task claimed_at, for stable ordering


@dataclass
class Decision:
    task_id: str
    goal: str | None
    event: str  # clear | defer | release | max_defer
    reason: str = ""
    blocker: str | None = None
    hotspot: str | None = None
    hotspot_why: str | None = None
    overlap: list[str] = field(default_factory=list)  # non-hotspot shared files
    first_deferred_at: str | None = None

    @property
    def deferred(self) -> bool:
        return self.event == "defer"


@dataclass
class GateResult:
    order: list[dict]
    decisions: dict[str, Decision] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def deferred(self) -> dict[str, Decision]:
        return {k: v for k, v in self.decisions.items() if v.deferred}


def _load_queue(company_dir: Path) -> dict:
    try:
        data = json.loads(
            (Path(company_dir) / "state" / "work_queue.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _task_label(task: dict) -> tuple[str, str, str]:
    n = pr_number(task.get("pr_url"))
    if n:
        return f"#{n}", "pr", n
    tid = str(task.get("task_id") or "?")
    return f"task {tid}", "task", tid


def _owns_pr(task: dict, pr: dict) -> bool:
    """The PR belongs to ``task`` (its own earlier attempt / fix round)."""
    tid = str(task.get("task_id") or "")
    if pr_number(task.get("pr_url")) == str(pr.get("number")):
        return True
    names = f"{pr.get('title') or ''} {pr.get('headRefName') or ''}"
    return bool(tid) and tid in names


def _shared(
    pred: Prediction, blockers: list[Blocker], hot: Hotspots
) -> tuple[tuple[Blocker, str, str] | None, list[str]]:
    """(first (blocker, hotspot file, why), every shared NON-hotspot file)."""
    overlap: set[str] = set()
    first: tuple[Blocker, str, str] | None = None
    for b in blockers:
        for f in sorted(set(pred.files) & b.files):
            why = hot.why(f)
            if why is None:
                overlap.add(f)
            elif first is None:
                first = (b, f, why)
    return first, sorted(overlap)


def gate(
    pending: list[dict],
    active_ids: Iterable[str] = (),
    company_dir: Path | str | None = None,
    *,
    runner: Runner | None = None,
    now: datetime | None = None,
    config: ConflictAwareConfig | None = None,
    goals: dict | None = None,
    write: bool = True,
) -> GateResult:
    """Decide which pending goal builds wait; return the pending list in the
    order the daemon should consider it (deferred builds removed; released
    and expired builds first, oldest deferral first). Never raises."""
    result = GateResult(order=list(pending))
    try:
        company_dir = _company_dir(company_dir)
        project_root = company_dir.resolve().parent
        config = config or load_config(project_root)
        if not config.enabled or not pending:
            return result
        now = now or _now()
        _gate(
            pending,
            set(active_ids),
            company_dir,
            project_root,
            config,
            goals,
            runner,
            now,
            write,
            result,
        )
    except Exception as exc:  # noqa: BLE001 — dispatch must never break here
        result.order = list(pending)
        result.errors.append(f"{type(exc).__name__}: {exc}")
    return result


def _predict_cached(task: dict, **kwargs: Any) -> Prediction:
    key = (str(kwargs["project_root"]), str(task.get("task_id") or id(task)))
    with _CACHE_LOCK:
        hit = _PREDICTION_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    pred = predict_task_files(task, **kwargs)
    with _CACHE_LOCK:
        _PREDICTION_CACHE[key] = (time.monotonic() + PREDICTION_TTL, pred)
    return pred


def _gate(
    pending: list[dict],
    active_ids: set[str],
    company_dir: Path,
    project_root: Path,
    config: ConflictAwareConfig,
    goals: dict | None,
    runner: Runner | None,
    now: datetime,
    write: bool,
    result: GateResult,
) -> None:
    if goals is None:
        with contextlib.suppress(Exception):
            goals = _import("challenger_stage").load_goals(company_dir)
    goals = goals or {}
    candidates = [t for t in pending if _goal_of(t, company_dir, goals) is not None]
    if not candidates:
        return
    hot = hotspots(company_dir, config, now=now, repo=project_root.name)
    index = repo_index(project_root, runner)
    merged = merged_prs(runner, project_root) if config.recent_prs > 0 else []
    pkw = dict(
        company_dir=company_dir,
        project_root=project_root,
        config=config,
        hot=hot,
        goals=goals,
        runner=runner,
        index=index,
        merged=merged,
    )

    # Blockers: builds in flight, then open daemon PRs.
    queue = _load_queue(company_dir)
    in_flight: list[dict] = [
        t for t in queue.get("in_progress") or [] if isinstance(t, dict)
    ]
    in_flight += [t for t in pending if t.get("task_id") in active_ids]
    task_blockers: list[tuple[dict, Blocker]] = []
    seen_ids: set[str] = set()
    for t in in_flight:
        tid = str(t.get("task_id") or "")
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        label, kind, ref = _task_label(t)
        pred = _predict_cached(t, **pkw)
        task_blockers.append(
            (
                t,
                Blocker(
                    label,
                    kind,
                    set(pred.files),
                    ref,
                    str(t.get("claimed_at") or t.get("created_at") or ""),
                ),
            )
        )
    prs = open_daemon_prs(runner, project_root)
    if prs is None:
        result.errors.append("gh pr list (open daemon PRs) failed")
        prs = []
    pr_blockers = [
        (
            pr,
            Blocker(
                f"#{pr['number']}",
                "pr",
                set(pr_files(pr)),
                str(pr["number"]),
                str(pr.get("createdAt") or ""),
            ),
        )
        for pr in sorted(
            prs, key=lambda p: (str(p.get("createdAt") or ""), p["number"])
        )
    ]

    max_defer = timedelta(hours=config.max_defer_hours)
    pending_ids = {str(t.get("task_id") or "") for t in pending}
    log_rows: list[dict] = []
    queue_marks: dict[str, dict | None] = {}

    state_cm = (
        _locked_state(company_dir)
        if write
        else contextlib.nullcontext(read_state(company_dir))
    )
    with state_cm as state:
        tasks_state: dict = state["tasks"]
        for t in candidates:
            tid = str(t.get("task_id") or "")
            if not tid or tid in active_ids:
                continue
            pred = _predict_cached(t, **pkw)
            blockers = [b for (bt, b) in task_blockers if bt.get("task_id") != tid]
            blockers += [b for (pr, b) in pr_blockers if not _owns_pr(t, pr)]
            hit, overlap = _shared(pred, blockers, hot)
            rec = tasks_state.get(tid) or {}
            first = _parse_ts(rec.get("first_deferred_at"))
            if hit is not None:
                b, f, why = hit
                reason = f"waits for {b.label} (shared hotspot {f})"
                if first is not None and now - first >= max_defer:
                    d = Decision(
                        tid,
                        pred.goal,
                        "max_defer",
                        f"deferred {config.max_defer_hours:g}h already; "
                        f"dispatching despite: {reason}",
                        b.label,
                        f,
                        why,
                        overlap,
                        rec.get("first_deferred_at"),
                    )
                else:
                    first_iso = rec.get("first_deferred_at") or _iso(now)
                    d = Decision(
                        tid,
                        pred.goal,
                        "defer",
                        reason,
                        b.label,
                        f,
                        why,
                        overlap,
                        first_iso,
                    )
            elif first is not None:
                d = Decision(
                    tid,
                    pred.goal,
                    "release",
                    "no shared hotspot with a build in flight or an open PR",
                    overlap=overlap,
                    first_deferred_at=rec.get("first_deferred_at"),
                )
            else:
                d = Decision(tid, pred.goal, "clear", "", overlap=overlap)
            result.decisions[tid] = d

            # One row per change of decision, never one per daemon cycle.
            key = f"{d.event}|{d.blocker or ''}|{d.hotspot or ''}"
            if rec.get("last_key") != key:
                row = {
                    "event": d.event,
                    "task_id": tid,
                    "goal": d.goal,
                    "reason": d.reason,
                    "blocker": d.blocker,
                    "hotspot": d.hotspot,
                    "hotspot_why": d.hotspot_why,
                    "non_hotspot_overlap": d.overlap[:20],
                    "predicted": len(pred.files),
                    "sources": sorted(
                        {s.split(":")[0] for v in pred.files.values() for s in v}
                    ),
                }
                if d.first_deferred_at:
                    row["first_deferred_at"] = d.first_deferred_at
                log_rows.append(row)
            new = dict(rec)
            new["last_key"] = key
            new["goal"] = d.goal
            if d.first_deferred_at:
                new["first_deferred_at"] = d.first_deferred_at
            if d.event == "defer":
                new["reason"] = d.reason
                mark = {
                    "reason": d.reason,
                    "blocker": d.blocker,
                    "hotspot": d.hotspot,
                    "since": d.first_deferred_at,
                }
            else:
                new.pop("reason", None)
                mark = None
            if t.get(QUEUE_FIELD) != mark:
                queue_marks[tid] = mark
                if mark is None:
                    t.pop(QUEUE_FIELD, None)
                else:
                    t[QUEUE_FIELD] = mark
            tasks_state[tid] = new
        # Forget tasks that are no longer pending (dispatched, cancelled...).
        for tid in list(tasks_state):
            if tid not in pending_ids:
                del tasks_state[tid]

    if write:
        for row in log_rows:
            log_decision(company_dir, row)
        if queue_marks:
            _persist_marks(company_dir, queue_marks, result)

    # The order: released / expired builds first (oldest first deferral
    # first), then everything else in its original order; deferred removed.
    def first_of(t: dict) -> str:
        d = result.decisions.get(str(t.get("task_id") or ""))
        return (d.first_deferred_at if d else "") or ""

    front = sorted(
        (
            t
            for t in pending
            if (d := result.decisions.get(str(t.get("task_id") or "")))
            and d.event in ("release", "max_defer")
        ),
        key=first_of,
    )
    front_ids = {id(t) for t in front}
    rest = [
        t
        for t in pending
        if id(t) not in front_ids
        and not (
            (d := result.decisions.get(str(t.get("task_id") or ""))) and d.deferred
        )
    ]
    result.order = front + rest


def _persist_marks(
    company_dir: Path, marks: dict[str, dict | None], result: GateResult
) -> None:
    """Write the deferral reason onto the pending task in the queue (best
    effort, under the queue lock): visible in ``gatesmith-queue`` and the
    dashboard while the build waits."""
    try:
        wa = _import("work_allocator")
        queue_path = Path(company_dir) / "state" / "work_queue.json"
        lock_path = Path(company_dir) / "runtime" / "queue.lock"
        with wa.QueueLock(lock_path):
            queue = _load_queue(company_dir)
            changed = False
            for t in queue.get("pending") or []:
                tid = str(t.get("task_id") or "") if isinstance(t, dict) else ""
                if tid not in marks:
                    continue
                mark = marks[tid]
                if mark is None:
                    changed |= t.pop(QUEUE_FIELD, None) is not None
                elif t.get(QUEUE_FIELD) != mark:
                    t[QUEUE_FIELD] = mark
                    changed = True
            if changed:
                queue.setdefault("metadata", {})["last_modified"] = _iso()
                wa.write_queue_file(queue_path, queue)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"queue mark failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# The daemon hook
# ---------------------------------------------------------------------------


def daemon_gate(
    pending: list[dict],
    active_ids: Iterable[str] = (),
    company_dir: Path | str = ".company",
    logger: Any = None,
) -> list[dict]:
    """The daemon hook: the pending list ``_pick_next_pending_task`` should
    consider. Returns ``pending`` unchanged when the feature is off or on any
    error. Never raises."""
    try:
        res = gate(pending, active_ids, company_dir)
    except Exception:  # noqa: BLE001
        return list(pending)
    if logger is not None:
        with contextlib.suppress(Exception):
            for tid, d in res.decisions.items():
                if d.event == "defer":
                    logger.debug(f"[ConflictAware] {tid} {d.reason}")
                elif d.event in ("release", "max_defer"):
                    logger.info(f"[ConflictAware] {tid} {d.event}: {d.reason}")
            for err in res.errors:
                logger.debug(f"[ConflictAware] {err}")
    return res.order


def gate_wired() -> bool:
    """forge_daemon passes the pending list through :func:`daemon_gate`."""
    try:
        text = (HOOKS_DIR / "forge_daemon.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "conflict_forecast" in text and "daemon_gate" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conflict_forecast.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "predict", "gate"):
        p = sub.add_parser(name)
        p.add_argument("--company-dir", default=None)
        if name == "predict":
            p.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    company_dir = _company_dir(args.company_dir)
    project_root = company_dir.resolve().parent
    config = load_config(project_root)
    queue = _load_queue(company_dir)
    if args.cmd == "predict":
        task = next(
            (
                t
                for lane in queue.values()
                if isinstance(lane, list)
                for t in lane
                if isinstance(t, dict) and t.get("task_id") == args.task_id
            ),
            None,
        )
        if task is None:
            print(f"task {args.task_id} not found in the queue")
            return 1
        hot = hotspots(company_dir, config, repo=project_root.name)
        pred = predict_task_files(
            task, company_dir, project_root, config=config, hot=hot
        )
        out = pred.as_dict()
        out["hotspots"] = sorted(f for f in pred.files if f in hot)
        print(json.dumps(out, indent=2))
        return 0
    if args.cmd == "gate":
        cfg = (
            config
            if config.enabled
            else ConflictAwareConfig(**{**config.__dict__, "enabled": True})
        )
        res = gate(queue.get("pending") or [], (), company_dir, config=cfg, write=False)
        for tid, d in res.decisions.items():
            print(f"{tid}: {d.event} {d.reason}".rstrip())
        for err in res.errors:
            print(f"error: {err}")
        return 0
    print(
        f"conflictAware.enabled={config.enabled} maxDeferHours={config.max_defer_hours:g} "
        f"gate_wired={gate_wired()}"
    )
    print("Configured hotspots: " + (", ".join(config.hotspots) or "(none)"))
    learned = learned_hotspots(company_dir, config, repo=project_root.name)
    print(
        "Learned hotspots: "
        + (
            ", ".join(
                f"{f} ({s['count']}, score {s['score']})"
                for f, s in sorted(learned.items(), key=lambda kv: -kv[1]["score"])
            )
            or "(none)"
        )
    )
    for tid, rec in sorted(read_state(company_dir)["tasks"].items()):
        if rec.get("reason"):
            print(f"{tid}: {rec['reason']} (since {rec.get('first_deferred_at')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
