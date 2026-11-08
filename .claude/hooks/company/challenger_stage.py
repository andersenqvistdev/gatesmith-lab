#!/usr/bin/env python3
"""The challenger stage: a blind adversarial review of every product-goal PR.

Why this exists (CHALLENGER-STAGE, 2026-10-04)
----------------------------------------------
Every daemon PR for webapp goals G24 and G27-G32 needed a separate
review-and-fix agent run by the operator's assistant before merge: a mailto
header injection, future-dated sync changes winning every conflict, a11y
regressions, late erasure re-delivery, an O(n^2) render, and a PR cut off at
~30 min that delivered one file of a whole goal. This module builds that
review into Gatesmith, so the daemon hands a human a review-fixed PR or a precise
report, never a half-built one.

The flow
--------
1. **Engage.** A product-goal task (``[QUEUE-FILL] Gn:``, ``source ==
   "gap_analysis"``, a goal in vision.md that is not Gatesmith's own) lands in the
   ``pr_open`` lane. :func:`tick` (called from the daemon's fast pr_open
   reconcile) labels the PR ``challenger:pending`` (disarming native
   auto-merge) and queues ONE challenger task for it. The worker that opened
   the PR also labels it at creation (:func:`pr_create_label_args`), so no
   merge lever can act between the PR opening and the first tick.
2. **Challenge.** The challenger task is an ordinary queue task
   (``source == "challenger"``, ``challenger = {kind, parent_task_id, pr_url,
   branch, round, goal_id}``) that the daemon dispatches into its own worktree
   and worker slot. operation_loop hands it to :func:`execute_claimed`, which
   checks the PR's branch out in that worktree and runs a FRESH session whose
   prompt holds only the goal row, the SPEC sections the goal cites, the
   goal's acceptance case files and the PR diff — never the builder's
   transcript, worker log or memory ("blind"). The session runs the entries
   the ledger way (``run-entries`` below), reviews against the checklist in
   ``templates/challenger.md``, fixes what it can with tests, and writes a
   JSON report.
3. **Settle.** The stage commits the session's fixes and pushes them to the
   PR branch (fast-forward only; never ``acceptance/``, ``.claude/``,
   ``.github/`` or a human-protected path), re-runs the goal's entries itself
   on the pushed head (an oracle the session cannot argue with: a "passed"
   with a failing entry becomes "incomplete"), posts the report as a PR
   comment, and sets exactly one label: ``challenger:passed`` |
   ``challenger:incomplete`` | ``challenger:needs-human``.
4. **Continue.** "incomplete" in a round below ``maxRounds`` queues a
   CONTINUATION task on the same branch — no new PR — whose prompt carries the
   original brief and the challenger's report. When it has pushed, the next
   challenger round is queued. After ``maxRounds`` the PR is
   ``challenger:needs-human`` with the last report.

The challenger never merges. While a PR carries any ``challenger:*`` label
other than ``challenger:passed`` (:data:`HOLD_LABELS`) every merge lever
refuses it, exactly like ``verdict-pending``. The deliverable judge stays: a
push by the stage moves the head, so the stage re-applies ``verdict-pending``
(``verdict_hold.hold_for_new_head``) and, on "passed", re-runs the judge gate
on the new head, which arms auto-merge only where the project allows it.

Stage tasks never carry ``pr_url`` or ``source_goal``: a challenger's
completion must not complete its goal's dependents, a cancel of it must not
close the product PR, and the PR's own task stays the one in ``pr_open``.
Their own pushes therefore never trigger a new challenger (no loops).

Config (``autonomy.challenger`` in gatesmith.json; defaults in code):
``enabled`` (true), ``maxRounds`` (2), ``timeoutMinutes`` (60), ``model``
(null = the worker provider's model for complex tasks), ``stallHours`` (4).

Bounded wait (CHALLENGER-STALL, 2026-10-28)
-------------------------------------------
A ``challenger:pending`` PR either gets a review or a human, never silence
(webapp #324 sat pending for hours: its builder task had reached
``completed`` while the PR stayed open, and the head-moved re-review only
looked at builder tasks in ``pr_open``). The tick therefore re-checks a
passed PR's head whatever lane its builder task is in, as long as the PR is
open, and reviews every new head afresh (never a pass carried across a base
update: an identical PR diff on a changed base is not the same code); and
sweeps open ``challenger:pending`` PRs: one with no review running for
``stallHours`` is labelled ``challenger:needs-human`` with a comment that
says why.

CLI::

    challenger_stage.py run-entries --project-dir DIR --ids A,B [--state-dir D] [--history F]
    challenger_stage.py status [--company-dir .company]
    challenger_stage.py tick [--company-dir .company]
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = HOOKS_DIR / "templates"

STAGE_SOURCE = "challenger"
STAGE_ACTOR = "challenger-stage"
KIND_CHALLENGER = "challenger"
KIND_CONTINUATION = "continuation"

LABEL_PENDING = "challenger:pending"
LABEL_PASSED = "challenger:passed"
LABEL_INCOMPLETE = "challenger:incomplete"
LABEL_NEEDS_HUMAN = "challenger:needs-human"
ALL_LABELS = (LABEL_PENDING, LABEL_PASSED, LABEL_INCOMPLETE, LABEL_NEEDS_HUMAN)
# Every lever that merges (daemon loop, pr_output_manager, ci.yml) refuses a
# PR carrying one of these. A PR the stage never engaged carries none of them,
# so projects without the stage are unchanged.
HOLD_LABELS = frozenset({LABEL_PENDING, LABEL_INCOMPLETE, LABEL_NEEDS_HUMAN})
# Same literal as auto_merge_paths.REVIEW_HOLD_LABEL / forge_daemon.py's own
# copy (never imported across — each consumer owns its copy, house style): a
# human already has this PR; the stage must never engage it regardless of
# what label or lane the builder task carries (CHALLENGER-ENGAGE-ORPHAN-PRS).
REVIEW_HOLD_LABEL = "awaiting-operator-review"
_LABEL_STYLE = {
    LABEL_PENDING: ("fbca04", "Challenger review running: no merge until it passes"),
    LABEL_PASSED: ("0e8a16", "Challenger review passed"),
    LABEL_INCOMPLETE: ("d93f0b", "Challenger found the goal incomplete: no merge"),
    LABEL_NEEDS_HUMAN: ("b60205", "Challenger needs a human: no merge"),
}

VERDICT_PASSED = "passed"
VERDICT_INCOMPLETE = "incomplete"
VERDICT_NEEDS_HUMAN = "needs-human"
VERDICTS = (VERDICT_PASSED, VERDICT_INCOMPLETE, VERDICT_NEEDS_HUMAN)
_VERDICT_LABEL = {
    VERDICT_PASSED: LABEL_PASSED,
    VERDICT_INCOMPLETE: LABEL_INCOMPLETE,
    VERDICT_NEEDS_HUMAN: LABEL_NEEDS_HUMAN,
}

# Paths no stage commit may touch: the oracle, session configuration, CI.
# A builder PR that changes session configuration is never opened in a
# session at all (its own hooks would run); one that changes the oracle is
# reviewed but can only end needs-human.
FORBIDDEN_PREFIXES = ("acceptance/", ".claude/", ".github/")
FORBIDDEN_FILES = (".mcp.json", "CLAUDE.md")
SESSION_CONFIG_PREFIXES = (".claude/",)
SESSION_CONFIG_FILES = (".mcp.json", "CLAUDE.md")
ORACLE_PREFIX = "acceptance/"
# Never committed: runtime state and session notes.
_UNCOMMITTED_PREFIXES = (".company/", ".planning/")

# A transient failure (fetch, spawn refused, provider down) puts the task back
# with a back-off this many times, then the PR goes to a human.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 600
# Slack on top of the session timeout for checkout, install, the oracle run
# and the push, for the daemon's force-collect timeout.
WORKER_TIMEOUT_SLACK_SECONDS = 3600

SPEC_EXCERPT_MAX_CHARS = 40_000
CASE_FILE_MAX_CHARS = 8_000
DIFF_MAX_CHARS = 150_000
REPORT_MAX_CHARS = 60_000
COMMENT_MAX_CHARS = 60_000
GIT_TIMEOUT = 60
GH_TIMEOUT = 30

STATE_SUBDIR = Path("state") / "challenger"
STATE_FILE = "stage.json"
STATE_LOCK = "stage.lock"
REPORTS_SUBDIR = "reports"

SYSTEM_REINFORCEMENT_CHALLENGER = (
    "IMPORTANT: You are an adversarial reviewer. You MUST write the JSON report "
    "to the exact path the task names before you finish; without it the review "
    "counts as failed. Fix only what you can prove with a test."
)
SYSTEM_REINFORCEMENT_CONTINUATION = (
    "IMPORTANT: You MUST use Write or Edit tools to create or modify files. "
    "Text-only responses will be treated as failures."
)

_QUEUE_FILL_TITLE_RE = re.compile(r"^\[QUEUE-FILL\]\s+(G\d+):")
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")
_VERSION_RE = re.compile(r"\bv\d+\.\d+\b")
_RULE_RANGE_RE = re.compile(
    r"\b([A-Z]{1,3})(\d+)[a-z]?\s*[–-]\s*(?:[A-Z]{1,3})?(\d+)\b"
)
_RULE_ID_RE = re.compile(r"\b([A-Z]{1,3}\d+[a-z]?)\b")
_BLOCK_START_RE = re.compile(r"^(\s*)(?:[-*+]\s|\d+\.\s|(#{2,6})\s)")

Runner = Callable[..., Any]


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


def _completed(args: list[str], code: int, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args, code, out, err)


def _run(runner: Runner | None, args: list[str], cwd: str | Path | None, timeout: int):
    """subprocess.run resolved at call time (tests patch it), never raising."""
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
        return _completed(args, 1, "", f"{type(exc).__name__}: {exc}")


def _git(
    runner: Runner | None, cwd: str | Path, *args: str, timeout: int = GIT_TIMEOUT
):
    return _run(runner, ["git", "-C", str(cwd), *args], None, timeout)


def _gh(runner: Runner | None, args: list[str], cwd: str | Path | None = None):
    return _run(runner, ["gh", *args], cwd, GH_TIMEOUT)


def pr_number(pr_url: str | None) -> str | None:
    m = _PR_NUMBER_RE.search(str(pr_url or ""))
    return m.group(1) if m else None


def _pr_ref(pr_url: str) -> str:
    n = pr_number(pr_url)
    return f"PR #{n}" if n else pr_url


def _truncate(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... {what} cut at {limit} characters ...]\n"


# ---------------------------------------------------------------------------
# Configuration (defaults in code; gatesmith.json is human-protected)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChallengerConfig:
    enabled: bool = True
    max_rounds: int = 2
    timeout_minutes: int = 60
    model: str | None = None
    # A PR may sit ``challenger:pending`` with no review running this long
    # before the stage hands it to a human (CHALLENGER-STALL, 2026-10-28).
    stall_hours: float = 4.0

    @classmethod
    def from_dict(cls, raw: Any) -> ChallengerConfig:
        raw = raw if isinstance(raw, dict) else {}

        def _pos_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return default
            return value

        model = raw.get("model")
        stall = raw.get("stallHours", 4.0)
        if isinstance(stall, bool) or not isinstance(stall, (int, float)) or stall <= 0:
            stall = 4.0
        return cls(
            enabled=raw.get("enabled", True) is not False,
            max_rounds=_pos_int("maxRounds", 2),
            timeout_minutes=_pos_int("timeoutMinutes", 60),
            model=model if isinstance(model, str) and model.strip() else None,
            stall_hours=float(stall),
        )

    @property
    def timeout_seconds(self) -> int:
        return self.timeout_minutes * 60


def _read_project_config(project_root: Path) -> dict | None:
    """The settings this stage reads, each from its governing file (the root
    gatesmith.json when it declares them, else the install template; see
    governing_config). {} when none is declared; None when a governing file
    is unreadable, so the human-protected check fails closed."""
    try:
        return _import("governing_config").view(
            ["autonomy.challenger", "humanProtected"], Path(project_root)
        )
    except Exception:  # noqa: BLE001 — fail closed
        return None


def load_config(project_root: Path) -> ChallengerConfig:
    data = _read_project_config(project_root) or {}
    return ChallengerConfig.from_dict((data.get("autonomy") or {}).get("challenger"))


def template_path(kind: str) -> Path:
    return TEMPLATES_DIR / f"{kind}.md"


def load_template(kind: str) -> str | None:
    try:
        return template_path(kind).read_text(encoding="utf-8")
    except OSError:
        return None


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def render(template: str, values: dict[str, str]) -> str:
    """Fill ``{{KEY}}`` placeholders in ONE pass: text that came in through a
    value (the PR diff, a SPEC excerpt) is never itself expanded, so a diff
    carrying ``{{TASK_DESCRIPTION}}`` cannot pull the builder's brief into a
    blind prompt."""
    return _PLACEHOLDER_RE.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    )


# ---------------------------------------------------------------------------
# Goals: which tasks the stage serves, and the blind inputs
# ---------------------------------------------------------------------------


def is_stage_task(task: Any) -> bool:
    return (
        isinstance(task, dict)
        and task.get("source") == STAGE_SOURCE
        and isinstance(task.get("challenger"), dict)
    )


def parent_is_live(lane: str | None, task: dict | None) -> bool:
    """The builder task still owns an open PR the stage may work on: in
    ``pr_open``, or in ``blocked`` held by the deliverable gate (a judge
    reject is exactly what a continuation can fix). Never an operator hold."""
    if not isinstance(task, dict) or not task.get("pr_url"):
        return False
    if task.get("operator_hold") is True:
        return False
    if lane == "pr_open":
        return True
    if lane == "blocked":
        try:
            return bool(_import("work_allocator").is_held_for_review(task))
        except Exception:
            return False
    return False


def load_goals(company_dir: Path) -> dict[str, Any]:
    try:
        gt = _import("goal_tracker")
        return {
            g.id: g for g in gt.parse_goals_from_vision(Path(company_dir) / "vision.md")
        }
    except Exception:
        return {}


def product_goal(
    task: dict, company_dir: Path, goals: dict[str, Any] | None = None
) -> Any | None:
    """The product goal a builder task serves, or None (Gatesmith's own goals,
    roadmap/repair tasks, and the stage's own tasks are not served)."""
    if not isinstance(task, dict) or is_stage_task(task):
        return None
    if task.get("source") != "gap_analysis":
        return None
    match = _QUEUE_FILL_TITLE_RE.match(str(task.get("title") or ""))
    if not match:
        return None
    goals = goals if goals is not None else load_goals(company_dir)
    goal = goals.get(match.group(1))
    if goal is None:
        return None
    try:
        if _import("goal_tracker")._is_framework_dogfood_goal(goal):
            return None
    except Exception:
        return None
    return goal


def goal_entry_ids(goal: Any) -> list[str]:
    try:
        ids = _import("goal_tracker")._ledger_consistent_entry_ids(
            getattr(goal, "success_metric", "") or ""
        )
    except Exception:
        ids = None
    return list(ids or [])


def goal_row(goal: Any) -> str:
    deps = ", ".join(getattr(goal, "depends_on", []) or []) or "none"
    return (
        f"**{goal.id}: {goal.name}**\n\n"
        f"- Description: {goal.description}\n"
        f"- Success metric: {goal.success_metric}\n"
        f"- Depends on: {deps}"
    )


def cited_tokens(text: str) -> tuple[list[str], list[str]]:
    """Spec versions (``v0.20``) and rule ids (``F1``…``F7``) a goal cites."""
    versions = list(dict.fromkeys(_VERSION_RE.findall(text or "")))
    rules: list[str] = []
    for prefix, lo, hi in _RULE_RANGE_RE.findall(text or ""):
        lo_i, hi_i = int(lo), int(hi)
        if 0 < hi_i - lo_i <= 30:
            rules.extend(f"{prefix}{i}" for i in range(lo_i, hi_i + 1))
    rules.extend(_RULE_ID_RE.findall(text or ""))
    rules = [r for r in dict.fromkeys(rules) if not r.startswith("G")]
    return versions, rules


def with_split_spec(root: Path, spec_text: str) -> str:
    """A product may keep each version's decisions in its own file
    (``spec/decided/v0.45.md``, webapp since 2026-10-14) so parallel PRs
    don't conflict on SPEC.md. Append those files, in name order, so
    :func:`spec_excerpt` finds the blocks a goal cites either way."""
    decided = root / "spec" / "decided"
    try:
        files = sorted(p for p in decided.glob("*.md") if p.is_file())
    except OSError:
        return spec_text
    parts = [spec_text] if spec_text else []
    for f in files:
        try:
            parts.append(f.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n\n".join(parts)


def spec_excerpt(
    spec_text: str, goal_text: str, limit: int = SPEC_EXCERPT_MAX_CHARS
) -> str:
    """The SPEC blocks (list items or sub-headings, with everything nested
    under them) whose first line names a version or a rule id the goal cites."""
    versions, rules = cited_tokens(goal_text)
    if not spec_text or not (versions or rules):
        return "(the goal cites no spec version or rule id — read the spec)"
    lines = spec_text.splitlines()
    starts: list[tuple[int, int, int]] = []  # (index, indent, heading level or 0)
    for i, line in enumerate(lines):
        m = _BLOCK_START_RE.match(line)
        if m:
            starts.append((i, len(m.group(1)), len(m.group(2) or "")))
    rule_res = [re.compile(rf"(?:\*\*|^|\s){re.escape(r)}[.:)\s*]") for r in rules]
    picked: list[tuple[int, int]] = []
    for pos, (i, indent, level) in enumerate(starts):
        head = lines[i]
        if not (
            any(v in head for v in versions)
            or any(rx.search(head[:80]) for rx in rule_res)
        ):
            continue
        end = len(lines)
        for j, j_indent, j_level in starts[pos + 1 :]:
            if level:
                if j_level and j_level <= level:
                    end = j
                    break
            elif j_level or j_indent <= indent:
                end = j
                break
        if any(a <= i and end <= b for a, b in picked):
            continue
        picked = [(a, b) for a, b in picked if not (i <= a and b <= end)]
        picked.append((i, end))
    if not picked:
        return "(no spec block names the cited versions or rule ids — read the spec)"
    parts = [
        f"<!-- SPEC lines {a + 1}-{b} -->\n" + "\n".join(lines[a:b]).rstrip()
        for a, b in sorted(picked)
    ]
    return _truncate("\n\n".join(parts), limit, "spec excerpt")


def acceptance_cases(project_dir: Path, ids: list[str]) -> str:
    """The case/relation files of the goal's entries, verbatim (capped)."""
    if not ids:
        return "(the goal's metric names no acceptance entries)"
    found: dict[str, str] = {}
    for sub in ("cases", "relations"):
        folder = Path(project_dir) / "acceptance" / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                entry_id = json.loads(raw).get("id")
            except (OSError, ValueError, AttributeError):
                continue
            if entry_id in ids and entry_id not in found:
                rel = path.relative_to(project_dir)
                found[entry_id] = (
                    f"### {entry_id} — `{rel}`\n\n```json\n"
                    + _truncate(raw, CASE_FILE_MAX_CHARS, "case file")
                    + "\n```"
                )
    parts = [found[i] for i in ids if i in found]
    missing = [i for i in ids if i not in found]
    if missing:
        parts.append("Entries with no case or relation file: " + ", ".join(missing))
    return "\n\n".join(parts)


def pr_diff(worktree: Path, runner: Runner | None = None) -> str:
    stat = _git(runner, worktree, "diff", "--stat", "origin/main...HEAD")
    diff = _git(runner, worktree, "diff", "origin/main...HEAD", timeout=120)
    if diff.returncode != 0:
        return (
            "(the diff could not be computed here — run "
            "`git diff origin/main...HEAD` yourself)"
        )
    return (
        "```\n"
        + (stat.stdout or "").strip()
        + "\n```\n\n```diff\n"
        + _truncate(diff.stdout or "", DIFF_MAX_CHARS, "diff")
        + "\n```\n\nRun `git diff origin/main...HEAD` for the full diff."
    )


def run_entries_command(
    project_dir: Path, ids: list[str], state_dir: Path, history: Path | None
) -> str:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run-entries",
        "--project-dir",
        str(project_dir),
        "--ids",
        ",".join(ids) or "-",
        "--state-dir",
        str(state_dir),
    ]
    if history is not None:
        cmd += ["--history", str(history)]
    return " ".join(_shell_quote(c) for c in cmd)


def _shell_quote(arg: str) -> str:
    import shlex

    return shlex.quote(arg)


# ---------------------------------------------------------------------------
# Running a goal's entries the ledger way
# ---------------------------------------------------------------------------


def run_goal_entries(
    project_dir: Path,
    ids: list[str],
    state_dir: Path,
    history_path: Path | None = None,
) -> dict[str, Any]:
    """Run only ``ids`` with acceptance_ledger's own loader, runners, manifest
    and adaptive timeouts, against ``project_dir``'s checkout. Records nothing
    in the project's ledger (no runs.jsonl line, no divergence, no credit):
    ``state_dir`` holds only the dependency-install marker.

    Returns ``{"results": {id: {"verdict", "detail"}}, "missing": [...],
    "environment_error": str | None}``.
    """
    al = _import("acceptance_ledger")
    out: dict[str, Any] = {"results": {}, "missing": [], "environment_error": None}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        config = dataclasses.replace(
            al.LedgerConfig.for_project(Path(project_dir)), state_dir=state_dir
        )
        install = al.ensure_dependencies(config.base_dir, state_dir)
        # InstallResult carries ``action``; this read ``status`` (never set),
        # so a failed install went unnoticed and every entry ran without deps.
        if getattr(install, "action", "") == "failed":
            out["environment_error"] = (
                f"dependency install failed: {getattr(install, 'error', '')}"[:500]
            )
            return out
        manifest = al.load_manifest(config.ledger_dir)
        entries = [
            (al.run_relation_entry, e)
            for e in al.load_entries(config.ledger_dir, "relations")
        ] + [
            (al.run_case_entry, e) for e in al.load_entries(config.ledger_dir, "cases")
        ]
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        out["environment_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return out
    history = al.read_entry_history(history_path) if history_path else {}
    by_id = {al._entry_display_id(e): (fn, e) for fn, e in entries}
    with tempfile.TemporaryDirectory(prefix="challenger-ledger-") as tmp_root:
        run_config = dataclasses.replace(config, tmp_root=Path(tmp_root))
        for entry_id in ids:
            if entry_id not in by_id:
                out["missing"].append(entry_id)
                continue
            fn, entry = by_id[entry_id]
            if "_load_error" in entry:
                outcome = al._load_error_outcome(entry)
            else:
                try:
                    timeout = al.entry_timeout_seconds(
                        manifest,
                        entry,
                        history.get(entry_id),
                        config.max_entry_timeout_seconds,
                    ).seconds
                except Exception:  # noqa: BLE001
                    timeout = None
                entry_config = dataclasses.replace(
                    run_config, entry_timeout_seconds=timeout, invoke_seconds=[]
                )
                outcome, _obs = al._run_entry_isolated(
                    fn, manifest, entry, entry_config
                )
            out["results"][entry_id] = {
                "verdict": outcome.verdict,
                "detail": str(outcome.detail)[:500],
            }
    return out


def entries_all_pass(oracle: dict[str, Any] | None, ids: list[str]) -> bool:
    if not ids:
        return True
    if not oracle or oracle.get("environment_error"):
        return False
    results = oracle.get("results") or {}
    return all((results.get(i) or {}).get("verdict") == "CONSISTENT" for i in ids)


# ---------------------------------------------------------------------------
# Stage state (per PR), under a cross-process lock
# ---------------------------------------------------------------------------


def _state_dir(company_dir: Path) -> Path:
    return Path(company_dir) / STATE_SUBDIR


# One thread per process inside the flock (macOS answers EDEADLK when two
# threads of one process flock the same file; cf. work_allocator).
_STATE_THREAD_LOCK = threading.Lock()


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


def read_state(company_dir: Path) -> dict:
    path = _state_dir(company_dir) / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("prs", {})
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ch_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _update_pr_state(company_dir: Path, pr_url: str, **fields: Any) -> dict:
    with _locked_state(company_dir) as state:
        rec = state["prs"].setdefault(pr_url, {})
        history = fields.pop("history_add", None)
        rec.update(fields)
        rec["updated_at"] = _iso()
        if history:
            rec.setdefault("history", []).append(history)
        return dict(rec)


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


def _find(queue: dict, task_id: str) -> tuple[str | None, dict | None]:
    for lane, tasks in queue.items():
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if isinstance(t, dict) and t.get("task_id") == task_id:
                return lane, t
    return None, None


def _stage_tasks_for(queue: dict, pr_url: str) -> list[tuple[str, dict]]:
    out = []
    for lane, tasks in queue.items():
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if is_stage_task(t) and t["challenger"].get("pr_url") == pr_url:
                out.append((lane, t))
    return out


def build_stage_task(
    *,
    kind: str,
    parent: dict,
    pr_url: str,
    branch: str,
    round_no: int,
    goal_id: str,
    config: ChallengerConfig,
    report_file: str | None = None,
) -> dict:
    wa = _import("work_allocator")
    n = pr_number(pr_url) or "?"
    if kind == KIND_CHALLENGER:
        title = (
            f"[CHALLENGER] {goal_id} round {round_no}: adversarial review of PR #{n}"
        )
        description = (
            f"Blind adversarial review of PR #{n} ({pr_url}) for goal {goal_id}, "
            f"round {round_no} of {config.max_rounds}: run the goal's acceptance "
            "entries, review against the challenger checklist, fix with tests on "
            "the PR branch, report and label. Never merges."
        )
    else:
        title = f"[CHALLENGER-CONTINUE] {goal_id} round {round_no}: finish PR #{n}"
        description = (
            f"Continue the unfinished PR #{n} ({pr_url}) for goal {goal_id} on its "
            f"own branch, from the challenger's round {round_no} report. No new PR."
        )
    now = _iso()
    return {
        "task_id": wa.generate_task_id(),
        "title": title,
        "description": description,
        "priority": 1,
        "status": "pending",
        "source": STAGE_SOURCE,
        "created_at": now,
        "created_by": STAGE_ACTOR,
        # No LLM planning for a stage task: its prompt is built here.
        "complexity": "complex",
        "estimated_complexity": "complex",
        "max_attempts": MAX_ATTEMPTS,
        # Read by the daemon's force-collect (it must not cut the session).
        "worker_timeout_seconds": config.timeout_seconds + WORKER_TIMEOUT_SLACK_SECONDS,
        # A stage task never opens its own PR (it pushes to the parent's).
        "requires_deliverable": False,
        "challenger": {
            "kind": kind,
            "parent_task_id": parent.get("task_id"),
            "pr_url": pr_url,
            "branch": branch,
            "round": round_no,
            "goal_id": goal_id,
            "report_file": report_file,
        },
    }


def enqueue_stage_task(company_dir: Path, task: dict) -> bool:
    """Append ``task`` to pending unless a stage task of the same kind and
    round for the same PR already exists in any lane. Returns True if added."""
    wa = _import("work_allocator")
    queue_path, lock_path = queue_paths(company_dir)
    meta = task["challenger"]
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        if not queue:
            return False
        for _lane, t in _stage_tasks_for(queue, meta["pr_url"]):
            m = t["challenger"]
            if m.get("kind") == meta["kind"] and m.get("round") == meta["round"]:
                return False
        queue.setdefault("pending", []).append(task)
        queue.setdefault("metadata", {})["last_modified"] = _iso()
        wa.write_queue_file(queue_path, queue)
    return True


def _cancel_pending_stage_tasks(
    company_dir: Path, pr_url: str, reason: str
) -> list[str]:
    """Cancel the PR's not-yet-running stage tasks (pending or held)."""
    wa = _import("work_allocator")
    queue_path, lock_path = queue_paths(company_dir)
    cancelled: list[str] = []
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        for lane, t in _stage_tasks_for(queue, pr_url):
            if lane not in ("pending", "blocked"):
                continue
            queue[lane] = [x for x in queue[lane] if x.get("task_id") != t["task_id"]]
            wa.mark_cancelled(t, reason, STAGE_ACTOR)
            t["cancelled_from"] = lane
            queue.setdefault(wa.CANCELLED_LANE, []).append(t)
            cancelled.append(t["task_id"])
        if cancelled:
            wa.write_queue_file(queue_path, queue)
    return cancelled


_PARENT_HOLD_REASON = "parent task is held by an operator"


def _mirror_parent_hold(company_dir: Path, pr_url: str, parent_held: bool) -> int:
    """Hold the PR's pending stage tasks while the parent is operator-held,
    and release the holds the stage set once the parent is free again."""
    wa = _import("work_allocator")
    queue_path, lock_path = queue_paths(company_dir)
    changed = 0
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        for lane, t in _stage_tasks_for(queue, pr_url):
            if parent_held and lane == "pending":
                queue["pending"] = [
                    x for x in queue["pending"] if x.get("task_id") != t["task_id"]
                ]
                wa.mark_operator_hold(t, _PARENT_HOLD_REASON)
                t["challenger_parent_hold"] = True
                queue.setdefault("blocked", []).append(t)
                changed += 1
            elif (
                not parent_held
                and lane == "blocked"
                and t.get("challenger_parent_hold")
                and str(t.get("blocked_reason") or "").endswith(_PARENT_HOLD_REASON)
            ):
                queue["blocked"] = [
                    x for x in queue["blocked"] if x.get("task_id") != t["task_id"]
                ]
                for key in ("operator_hold", "blocked_reason", "blocked_at"):
                    t.pop(key, None)
                t.pop("challenger_parent_hold", None)
                t["status"] = "pending"
                queue.setdefault("pending", []).append(t)
                changed += 1
        if changed:
            wa.write_queue_file(queue_path, queue)
    return changed


def _requeue_with_backoff(queue_path: Path, task_id: str, error: str) -> bool:
    """Put an in-progress stage task back to pending with a back-off. A task
    an operator cancelled or held stays where the operator put it."""
    wa = _import("work_allocator")
    lock_path = Path(queue_path).parent.parent / "runtime" / "queue.lock"
    with wa.QueueLock(lock_path):
        queue = _load_queue(queue_path)
        if wa.park_operator_task(queue, {"task_id": task_id}) is not None:
            wa.write_queue_file(queue_path, queue)
            return False
        lane, task = _find(queue, task_id)
        if task is None or lane != "in_progress":
            return False
        queue["in_progress"] = [
            t for t in queue["in_progress"] if t.get("task_id") != task_id
        ]
        task["status"] = "pending"
        task["challenger_attempts"] = int(task.get("challenger_attempts", 0)) + 1
        task["backoff_until"] = _iso(_now() + timedelta(seconds=RETRY_BACKOFF_SECONDS))
        task.setdefault("notes", []).append(
            {"timestamp": _iso(), "content": error[:300]}
        )
        for key in ("claimed_by", "claimed_at", "started_at"):
            task.pop(key, None)
        queue.setdefault("pending", []).append(task)
        wa.write_queue_file(queue_path, queue)
    return True


def task_is_parked(queue_path: Path, task_id: str) -> bool:
    """Cancelled or operator-held (gatesmith-queue cancel / hold)."""
    queue = _load_queue(queue_path)
    for t in queue.get("cancelled", []) or []:
        if isinstance(t, dict) and t.get("task_id") == task_id:
            return True
    for t in queue.get("blocked", []) or []:
        if (
            isinstance(t, dict)
            and t.get("task_id") == task_id
            and t.get("operator_hold")
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Labels and comments
# ---------------------------------------------------------------------------


def ensure_labels(runner: Runner | None = None, cwd: str | Path | None = None) -> None:
    for name in ALL_LABELS:
        color, desc = _LABEL_STYLE[name]
        _gh(
            runner,
            [
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                desc,
                "--force",
            ],
            cwd,
        )


def pr_labels(
    pr: str, runner: Runner | None = None, cwd: str | Path | None = None
) -> set[str] | None:
    res = _gh(
        runner, ["pr", "view", pr, "--json", "labels", "-q", ".labels[].name"], cwd
    )
    if res.returncode != 0:
        return None
    return {ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()}


def pr_is_open(
    pr: str | None, runner: Runner | None = None, cwd: str | Path | None = None
) -> bool | None:
    """True when PR ``pr`` is OPEN on GitHub right now, False when it is
    closed or merged, None when it cannot be told (a gh blip). The two
    callers want opposite fail directions on None — an engage gate fails
    closed (never reviews on an unreadable state), an abandon gate fails
    open (never tears a review down on one) — so this returns the raw
    tri-state and leaves the direction to the caller."""
    if not pr:
        return None
    res = _gh(runner, ["pr", "view", str(pr), "--json", "state", "-q", ".state"], cwd)
    if res.returncode != 0:
        return None
    state = (res.stdout or "").strip().upper()
    return (state == "OPEN") if state else None


def set_stage_label(
    pr: str, label: str, runner: Runner | None = None, cwd: str | Path | None = None
) -> bool:
    """Exactly one challenger label on the PR: add ``label`` first (a hold is
    never lifted before its successor is on), then drop the others."""
    ensure_labels(runner, cwd)
    res = _gh(runner, ["pr", "edit", pr, "--add-label", label], cwd)
    if res.returncode != 0:
        return False
    present = pr_labels(pr, runner, cwd)
    stale = [
        lbl
        for lbl in ALL_LABELS
        if lbl != label and (present is None or lbl in present)
    ]
    if stale:
        _gh(runner, ["pr", "edit", pr, "--remove-label", ",".join(stale)], cwd)
    return True


def engage_pr(
    pr: str, runner: Runner | None = None, cwd: str | Path | None = None
) -> bool:
    """Disarm native auto-merge (it ignores labels once armed), then hold."""
    _gh(runner, ["pr", "merge", pr, "--disable-auto"], cwd)
    return set_stage_label(pr, LABEL_PENDING, runner, cwd)


def post_comment(
    pr: str, body: str, runner: Runner | None = None, cwd: str | Path | None = None
) -> bool:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="challenger-", delete=False, encoding="utf-8"
    ) as f:
        f.write(_truncate(body, COMMENT_MAX_CHARS, "report"))
        path = f.name
    try:
        res = _gh(runner, ["pr", "comment", pr, "--body-file", path], cwd)
        return res.returncode == 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def pr_create_label_args(
    task: dict,
    cwd: str | Path | None = None,
    *,
    company_dir: Path | None = None,
    runner: Runner | None = None,
) -> list[str]:
    """``gh pr create`` arguments that open a product-goal PR already held by
    the stage, so no merge lever can act before the first :func:`tick`.
    Empty when the stage does not apply. Never raises."""
    try:
        if company_dir is None:
            company_dir = Path(_import("company_resolver").get_company_dir())
        config = load_config(Path(company_dir).parent)
        if not config.enabled or load_template(KIND_CHALLENGER) is None:
            return []
        if product_goal(task, Path(company_dir)) is None:
            return []
        ensure_labels(runner, cwd)
        return ["--label", LABEL_PENDING]
    except Exception:  # noqa: BLE001 — the tick still engages the PR
        return []


def merge_block_reason(
    pr: str | int,
    *,
    company_dir: Path | None = None,
    head_sha: str | None = None,
    runner: Runner | None = None,
) -> str | None:
    """Why a merge lever must not merge PR ``pr`` now, or None.

    Decided from the queue task that owns the PR, not from labels alone: a
    product-goal PR the stage has not passed (for the current head, when
    ``head_sha`` is given) is held even if no label made it there yet — a PR
    opened by a path that does not add ``challenger:pending`` (harvest, the
    auto-PR workflow), or judged before the first tick. It is labelled
    ``challenger:pending`` on the way. Never raises; None when the stage does
    not apply or cannot tell (labels still hold an engaged PR)."""
    try:
        company_dir = Path(company_dir or _import("company_resolver").get_company_dir())
        config = load_config(company_dir.resolve().parent)
        if not config.enabled or load_template(KIND_CHALLENGER) is None:
            return None
        n = str(pr).rsplit("/", 1)[-1]
        queue = _load_queue(queue_paths(company_dir)[0])
        owner = None
        for lane, tasks in queue.items():
            if lane == "cancelled" or not isinstance(tasks, list):
                continue
            for t in tasks:
                if (
                    isinstance(t, dict)
                    and not is_stage_task(t)
                    and pr_number(t.get("pr_url")) == n
                ):
                    owner = t
                    break
            if owner:
                break
        if owner is None or product_goal(owner, company_dir) is None:
            return None
        rec = read_state(company_dir)["prs"].get(owner["pr_url"]) or {}
        # A pass that names no reviewed head cannot show it covers this one:
        # it is held like a pass for an older head (re-review), below.
        reviewed = rec.get("reviewed_sha") if rec.get("phase") == "passed" else None
        if reviewed:
            if head_sha is None:
                head_sha = _pr_head_sha(n, runner, company_dir.resolve().parent)
            if head_sha is None:
                # Fail closed, but leave the labels alone: a gh blip must not
                # put a passed PR back under review. The next call decides.
                return "challenger pass not matched to the PR head (head unreadable)"
            if head_sha == reviewed:
                return None
        if rec.get("phase") in (None, "passed"):
            # Not engaged yet, or a pass for an older head: hold it now.
            with contextlib.suppress(Exception):
                _gh(runner, ["pr", "merge", n, "--disable-auto"], None)
                set_stage_label(n, LABEL_PENDING, runner)
        return (
            f"challenger review not passed (phase {rec.get('phase') or 'not started'})"
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# The tick: engage new product-goal PRs, follow their parent's lifecycle
# ---------------------------------------------------------------------------


def _head_ref(pr: str, runner: Runner | None, cwd: Path | None) -> str | None:
    res = _gh(
        runner, ["pr", "view", pr, "--json", "headRefName", "-q", ".headRefName"], cwd
    )
    ref = (res.stdout or "").strip() if res.returncode == 0 else ""
    return ref or None


def tick(
    company_dir: Path | str = ".company",
    *,
    runner: Runner | None = None,
    config: ChallengerConfig | None = None,
    goals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One pass: engage new product-goal PRs and keep the stage's tasks in
    step with their parent (cancel, hold, merge). Never raises.

    Engagement is decided from the PR's OWN state, never the builder task's
    queue lane (CHALLENGER-ENGAGE-ORPHAN-PRS, 2026-10-12): a task can sit in
    ``blocked`` for a reason that has nothing to do with the deliverable
    gate, or reach ``completed`` while its PR is still open, and either one
    used to mean the PR's ``challenger:pending`` label never got a review —
    a silent, permanent stall (webapp #209, #167). A candidate is any
    non-stage, non-cancelled task anywhere in the queue that owns a
    product-goal PR not yet tracked, as long as: the task is not
    operator-held, no challenger task is already working its PR (the
    dedupe), the PR is confirmed OPEN, and it does not carry
    ``awaiting-operator-review`` (a human already has it).
    """
    result: dict[str, Any] = {"engaged": [], "cancelled": [], "held": 0, "errors": []}
    try:
        company_dir = Path(company_dir)
        project_root = company_dir.resolve().parent
        config = config or load_config(project_root)
        queue_path, _lock = queue_paths(company_dir)
        queue = _load_queue(queue_path)
        if not queue:
            return result
        state = read_state(company_dir)
        _follow_parents(company_dir, queue, state, result, runner)
        if not config.enabled:
            return result
        if load_template(KIND_CHALLENGER) is None:
            result["errors"].append(
                f"challenger template missing: {template_path(KIND_CHALLENGER)}"
            )
            return result
        goals = goals if goals is not None else load_goals(company_dir)
        candidates: list[tuple[str, dict]] = []
        for lane, tasks in queue.items():
            if lane == "cancelled" or not isinstance(tasks, list):
                continue
            candidates.extend((lane, t) for t in tasks if isinstance(t, dict))
        for lane, task in candidates:
            if task.get("operator_hold") is True:
                continue
            pr_url = task.get("pr_url")
            if not pr_url or pr_url in state["prs"]:
                continue
            goal = product_goal(task, company_dir, goals)
            if goal is None:
                continue
            n = pr_number(pr_url)
            if not n:
                continue
            # Dedupe before any gh call: a challenger task already owns this
            # PR (another lane the old code never saw, or a race with an
            # earlier pass in this same tick).
            if active_stage_task(queue, pr_url) is not None:
                continue
            if pr_is_open(n, runner, project_root) is not True:
                continue  # closed, merged, or unreadable: never engage on it
            labels = pr_labels(n, runner, project_root)
            if labels and REVIEW_HOLD_LABEL in labels:
                continue  # a human already has this PR
            branch = task.get("branch_name") or _head_ref(n, runner, project_root)
            if not branch:
                result["errors"].append(f"{pr_url}: head branch unknown")
                continue
            if not engage_pr(n, runner, project_root):
                result["errors"].append(f"{pr_url}: could not apply {LABEL_PENDING}")
                continue
            stage_task = build_stage_task(
                kind=KIND_CHALLENGER,
                parent=task,
                pr_url=pr_url,
                branch=branch,
                round_no=1,
                goal_id=goal.id,
                config=config,
            )
            added = enqueue_stage_task(company_dir, stage_task)
            if not added:
                # A stage task for this PR+round already exists (the same
                # race the dedupe above usually catches first): no-op.
                continue
            _update_pr_state(
                company_dir,
                pr_url,
                parent_task_id=task.get("task_id"),
                goal_id=goal.id,
                branch=branch,
                round=1,
                phase="challenging",
                active_task_id=stage_task["task_id"],
                created_at=_iso(),
                history_add={
                    "at": _iso(),
                    "event": "engaged",
                    "task_id": stage_task["task_id"],
                    "lane": lane,
                },
            )
            result["engaged"].append(pr_url)
        sweep_stalled(company_dir, config, result, runner)
    except Exception as exc:  # noqa: BLE001 — the daemon loop must never break here
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


_ACTIVE_PHASES = ("challenging", "continuing")


# A passed PR whose head moved is reviewed again at most this many times.
MAX_REREVIEWS = 2


def _pr_head_sha(pr: str, runner: Runner | None, cwd: Path | None) -> str | None:
    res = _gh(
        runner, ["pr", "view", pr, "--json", "headRefOid", "-q", ".headRefOid"], cwd
    )
    sha = (res.stdout or "").strip() if res.returncode == 0 else ""
    return sha or None


def _next_round(company_dir: Path, pr_url: str, rec: dict) -> int:
    """One above every round this PR has used — the record's and every stage
    task's in any lane — so enqueue_stage_task's (kind, round) dedupe can
    never drop the re-review as a duplicate of an old, finished task.

    A re-review's round is therefore usually >= maxRounds, so an
    "incomplete" verdict on it goes straight to needs-human with no
    continuation round (as before this helper: it only makes that certain)."""
    rounds = [int(rec.get("round", 1) or 1)]
    queue = _load_queue(queue_paths(company_dir)[0])
    for _lane, t in _stage_tasks_for(queue, pr_url):
        with contextlib.suppress(TypeError, ValueError):
            rounds.append(int(t["challenger"].get("round") or 0))
    return max(rounds) + 1


def _recheck_passed_head(
    company_dir: Path,
    pr_url: str,
    rec: dict,
    parent: dict,
    result: dict,
    runner: Runner | None = None,
) -> None:
    """``challenger:passed`` belongs to one head. A later push (CI healer, a
    rebase, a human) puts the PR back under review: label pending, queue a
    fresh challenger round; after MAX_REREVIEWS, needs-human. Every new
    head gets a fresh review — even a base update only: an identical PR diff
    says nothing about how the PR behaves on a changed base."""
    pr = pr_number(pr_url)
    reviewed = rec.get("reviewed_sha")
    if not pr:
        return
    cwd = Path(company_dir).resolve().parent
    current = _pr_head_sha(pr, runner, cwd)
    # A pass that names no reviewed head covers no head: re-review it (the
    # merge levers hold it meanwhile — merge_block_reason).
    if not current or (reviewed and current == reviewed):
        return
    # A human already has this PR: never re-engage it (the same rule as the
    # first engagement and the stall sweep). Unreadable labels: wait.
    labels = pr_labels(pr, runner, cwd)
    if labels is None or REVIEW_HOLD_LABEL in labels:
        return
    rereviews = int(rec.get("rereviews", 0)) + 1
    if rereviews > MAX_REREVIEWS:
        if set_stage_label(pr, LABEL_NEEDS_HUMAN, runner, cwd):
            post_comment(
                pr,
                f"<!-- forge-challenger rereview-limit head={current} -->\n"
                f"The head moved again after a challenger pass (now "
                f"`{current[:12]}`, reviewed `{str(reviewed or '')[:12]}`), and "
                f"this PR has already been re-reviewed {MAX_REREVIEWS} times. "
                "The PR is held for a human decision.",
                runner,
                cwd,
            )
        _update_pr_state(
            company_dir,
            pr_url,
            phase="needs_human",
            history_add={
                "at": _iso(),
                "event": "head_moved_after_pass",
                "head": current,
            },
        )
        return
    config = load_config(cwd)
    round_no = _next_round(company_dir, pr_url, rec)
    task = build_stage_task(
        kind=KIND_CHALLENGER,
        parent=parent,
        pr_url=pr_url,
        branch=str(rec.get("branch") or parent.get("branch_name") or ""),
        round_no=round_no,
        goal_id=str(rec.get("goal_id") or ""),
        config=dataclasses.replace(config, max_rounds=max(config.max_rounds, round_no)),
    )
    set_stage_label(pr, LABEL_PENDING, runner, cwd)
    if enqueue_stage_task(company_dir, task):
        result["engaged"].append(pr_url)
    _update_pr_state(
        company_dir,
        pr_url,
        phase="challenging",
        round=round_no,
        rereviews=rereviews,
        active_task_id=task["task_id"],
        history_add={"at": _iso(), "event": "head_moved_after_pass", "head": current},
    )


def _passed_pr_still_open(
    company_dir: Path,
    pr_url: str,
    rec: dict,
    lane: str | None,
    parent: dict | None,
    runner: Runner | None,
) -> bool:
    """A passed PR whose builder task is no longer live (``completed`` with
    its PR still open, or gone from the queue) is still the stage's to
    re-review on a new head — webapp #324 sat ``challenger:pending`` for
    hours because only a ``pr_open`` builder was ever re-checked. Never for
    an operator's cancel or hold. A PR confirmed closed or merged is marked
    once, so it costs no gh call on later ticks."""
    if rec.get("pr_closed"):
        return False
    if lane == "cancelled" or (
        isinstance(parent, dict) and parent.get("operator_hold") is True
    ):
        return False
    is_open = pr_is_open(pr_number(pr_url), runner, Path(company_dir).resolve().parent)
    if is_open is False:
        _update_pr_state(company_dir, pr_url, pr_closed=True)
        return False
    return is_open is True


def _stage_task_alive(lane: str, task: dict) -> bool:
    """Will this stage task still run (or is an operator deliberately
    holding it)? Anything else — exhausted retries, a daemon timeout routed
    through failure recovery to completed or pr_open, a cancel — means no
    verdict is coming for the PR."""
    if lane in ("pending", "in_progress"):
        return True
    return lane == "blocked" and bool(
        task.get("operator_hold") or task.get("challenger_parent_hold")
    )


def active_stage_task(queue: dict, pr_url: str) -> dict | None:
    """The PR's stage task that is still going (or deliberately
    operator-held), if any — the dedupe check before engaging or
    re-engaging a PR, so two ticks (or two code paths) can never queue two
    reviews racing the same PR+head."""
    for lane, t in _stage_tasks_for(queue, pr_url):
        if _stage_task_alive(lane, t):
            return t
    return None


def parent_is_abandoned(
    lane: str | None,
    task: dict | None,
    pr_url: str,
    runner: Runner | None = None,
    cwd: Path | None = None,
) -> bool:
    """True only once the parent is CONFIRMED gone: explicitly cancelled (an
    operator's own signal — no PR check needed or wanted), or the PR itself
    is confirmed closed/merged. A lane like ``completed``, or the parent no
    longer being in the queue at all, no longer says this alone
    (CHALLENGER-ENGAGE-ORPHAN-PRS, 2026-10-12): a builder task can reach
    either one while its PR is still open and needs the challenger's
    review. Fails closed on an unreadable PR state — a gh blip must never
    tear down a review in flight."""
    if parent_is_live(lane, task):
        return False
    if lane == "cancelled":
        return True
    return pr_is_open(pr_number(pr_url), runner, cwd) is False


def _settle_stranded(
    company_dir: Path, pr_url: str, result: dict, runner: Runner | None = None
) -> None:
    """A PR in an active phase whose stage tasks all ended without a verdict
    would stay ``challenger:pending`` forever: hand it to a human instead.
    Decided on a fresh queue read taken AFTER the state snapshot (the stage
    always writes its verdict before it releases its task), under no lock."""
    queue_path, _lock = queue_paths(company_dir)
    queue = _load_queue(queue_path)
    if not queue:
        return
    stage_tasks = _stage_tasks_for(queue, pr_url)
    if any(_stage_task_alive(ln, t) for ln, t in stage_tasks):
        return
    rec = read_state(company_dir)["prs"].get(pr_url) or {}
    if rec.get("phase") not in _ACTIVE_PHASES:
        return
    pr = pr_number(pr_url)
    if not pr:
        return
    ended = ", ".join(
        f"{t.get('task_id')} ({ln}: {str(t.get('blocked_reason') or t.get('status') or '')[:80]})"
        for ln, t in stage_tasks[-3:]
    )
    cwd = Path(company_dir).resolve().parent
    if not set_stage_label(pr, LABEL_NEEDS_HUMAN, runner, cwd):
        result["errors"].append(f"{pr_url}: could not apply {LABEL_NEEDS_HUMAN}")
        return
    post_comment(
        pr,
        f"<!-- forge-challenger stranded -->\n"
        f"The challenger stage ended without a verdict for this PR "
        f"(phase {rec.get('phase')}, round {rec.get('round')}): "
        f"{ended or 'no stage task left in the queue'}. "
        "The PR is held for a human decision.",
        runner,
        cwd,
    )
    _update_pr_state(
        company_dir,
        pr_url,
        phase="needs_human",
        active_task_id=None,
        history_add={"at": _iso(), "event": "stranded", "tasks": ended},
    )
    result.setdefault("stranded", []).append(pr_url)


def _follow_parents(
    company_dir: Path,
    queue: dict,
    state: dict,
    result: dict,
    runner: Runner | None = None,
) -> None:
    for pr_url, rec in list(state["prs"].items()):
        lane, parent = _find(queue, str(rec.get("parent_task_id") or ""))
        stage_tasks = _stage_tasks_for(queue, pr_url)
        if rec.get("phase") == "passed":
            if parent_is_live(lane, parent):
                _recheck_passed_head(company_dir, pr_url, rec, parent, result, runner)
            elif _passed_pr_still_open(company_dir, pr_url, rec, lane, parent, runner):
                stub = parent or {
                    "task_id": rec.get("parent_task_id"),
                    "pr_url": pr_url,
                    "branch_name": rec.get("branch"),
                }
                _recheck_passed_head(company_dir, pr_url, rec, stub, result, runner)
            continue
        if parent_is_live(lane, parent):
            # A parent that is no longer held lets its stage tasks go again.
            if any(
                ln == "blocked" and t.get("challenger_parent_hold")
                for ln, t in stage_tasks
            ):
                result["held"] += _mirror_parent_hold(company_dir, pr_url, False)
            elif rec.get("phase") in _ACTIVE_PHASES:
                _settle_stranded(company_dir, pr_url, result, runner)
            continue
        if rec.get("phase") not in _ACTIVE_PHASES:
            continue
        if parent is not None and parent.get("operator_hold") is True:
            if any(ln == "pending" for ln, _t in stage_tasks):
                result["held"] += _mirror_parent_hold(company_dir, pr_url, True)
            continue
        # The parent's lane alone no longer confirms it is gone (a task can
        # reach "completed", or leave the queue, while its PR stays open and
        # needs this review — CHALLENGER-ENGAGE-ORPHAN-PRS, 2026-10-12): an
        # explicit cancel still abandons unconditionally, everything else is
        # confirmed against the PR's real state first.
        if not parent_is_abandoned(
            lane, parent, pr_url, runner, Path(company_dir).resolve().parent
        ):
            # Still the stage's PR: a review that ended without a verdict
            # goes to a human, whatever lane the builder task is in.
            _settle_stranded(company_dir, pr_url, result, runner)
            continue
        if lane is None:
            phase, why = "abandoned", "parent task no longer in the queue"
        elif lane == "cancelled":
            phase, why = "parent_cancelled", "parent task cancelled"
        elif lane == "completed":
            phase, why = (
                "parent_completed",
                "parent task completed (PR merged or closed)",
            )
        else:
            phase, why = "abandoned", f"parent task left pr_open for {lane}"
        result["cancelled"] += _cancel_pending_stage_tasks(company_dir, pr_url, why)
        _update_pr_state(
            company_dir, pr_url, phase=phase, history_add={"at": _iso(), "event": phase}
        )


# How often the tick lists the open challenger:pending PRs (one gh call).
STALL_SWEEP_SECONDS = 600


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _alive_stage_task(queue: dict, pr_url: str) -> tuple[str, dict] | None:
    for lane, t in _stage_tasks_for(queue, pr_url):
        if _stage_task_alive(lane, t):
            return lane, t
    return None


def _stall_reason(queue: dict, pr_url: str, rec: dict, waited_h: float) -> str:
    alive = _alive_stage_task(queue, pr_url)
    if alive is not None:
        _lane, t = alive
        extra = []
        if t.get("challenger_attempts"):
            extra.append(f"{t['challenger_attempts']} failed attempt(s)")
        if t.get("backoff_until"):
            extra.append(f"back-off until {t['backoff_until']}")
        return (
            f"challenger task {t.get('task_id')} (round "
            f"{(t.get('challenger') or {}).get('round')}) has waited in the queue "
            f"{waited_h:.1f} h without running"
            + (f" ({', '.join(extra)})" if extra else "")
            + " — no worker slot took it (lane ceiling, back-off or dispatcher)"
        )
    if not rec:
        return (
            "the challenger stage never engaged this PR (no stage record): no "
            "review is queued for it"
        )
    return (
        f"no challenger task is queued for this PR (stage phase "
        f"{rec.get('phase') or 'unknown'}, round {rec.get('round')}, reviewed "
        f"head {str(rec.get('reviewed_sha') or 'none')[:12]})"
    )


def sweep_stalled(
    company_dir: Path,
    config: ChallengerConfig,
    result: dict,
    runner: Runner | None = None,
) -> None:
    """Bound the wait on ``challenger:pending`` (CHALLENGER-STALL): every open
    PR carrying it either has a review running, or has had one queued or
    running within ``stall_hours`` — else it is labelled
    ``challenger:needs-human`` with a comment naming why, its waiting stage
    tasks are cancelled (a human has it now), and the tick reports it.

    The clock per PR starts the first time a sweep sees it pending and is
    reset whenever its stage task is seen running or operator-held, so it
    measures time since the last sign of progress, whoever set the label.
    Throttled to one ``gh pr list`` per STALL_SWEEP_SECONDS. Fails open: an
    unreadable list changes nothing."""
    company_dir = Path(company_dir)
    now = _now()
    with _locked_state(company_dir) as state:
        last = _parse_iso(state.get("stall_sweep_at"))
        if last and (now - last).total_seconds() < STALL_SWEEP_SECONDS:
            return
        state["stall_sweep_at"] = _iso(now)
        watch = dict(state.get("stall_watch") or {})
    cwd = company_dir.resolve().parent
    res = _gh(
        runner,
        [
            "pr",
            "list",
            "--state",
            "open",
            "--label",
            LABEL_PENDING,
            "--json",
            "number,url,labels",
            "--limit",
            "100",
        ],
        cwd,
    )
    if res.returncode != 0:
        return
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        return
    if not isinstance(rows, list):
        return
    queue = _load_queue(queue_paths(company_dir)[0])
    prs = read_state(company_dir)["prs"]
    seen: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        url = str(row["url"])
        n = str(row.get("number") or pr_number(url) or "")
        if not n:
            continue
        names = {
            lbl.get("name") for lbl in row.get("labels") or [] if isinstance(lbl, dict)
        }
        if REVIEW_HOLD_LABEL in names:
            continue  # a human already has this PR
        alive = _alive_stage_task(queue, url)
        if alive is not None and (alive[0] == "in_progress" or alive[0] == "blocked"):
            # Running now, or an operator deliberately holds it: progress.
            seen[url] = _iso(now)
            continue
        since = _parse_iso(watch.get(url)) or now
        seen[url] = _iso(since)
        waited_h = (now - since).total_seconds() / 3600
        if waited_h < config.stall_hours:
            continue
        rec = prs.get(url) or {}
        reason = _stall_reason(queue, url, rec, waited_h)
        if not set_stage_label(n, LABEL_NEEDS_HUMAN, runner, cwd):
            result["errors"].append(f"{url}: could not apply {LABEL_NEEDS_HUMAN}")
            continue
        cancelled = _cancel_pending_stage_tasks(
            company_dir, url, "challenger review stalled; handed to a human"
        )
        post_comment(
            n,
            f"<!-- forge-challenger stalled -->\n"
            f"**Challenger review stalled.** This PR has carried "
            f"`{LABEL_PENDING}` for {waited_h:.1f} h with no review running "
            f"(limit {config.stall_hours:g} h, `autonomy.challenger.stallHours`): "
            f"{reason}."
            + (
                f" Cancelled waiting stage task(s): {', '.join(cancelled)}."
                if cancelled
                else ""
            )
            + "\n\nThe PR is held for a human: review it and set "
            f"`{LABEL_PASSED}`, or close it.",
            runner,
            cwd,
        )
        _update_pr_state(
            company_dir,
            url,
            phase="needs_human",
            active_task_id=None,
            history_add={"at": _iso(now), "event": "stalled", "reason": reason},
        )
        seen.pop(url, None)
        result.setdefault("escalated", []).append(url)
        # The daemon logs tick errors: this is how the hand-off reaches its log.
        result["errors"].append(
            f"{url}: challenger review stalled {waited_h:.1f} h — labelled "
            f"{LABEL_NEEDS_HUMAN}: {reason}"
        )
    with _locked_state(company_dir) as state:
        state["stall_watch"] = seen


# ---------------------------------------------------------------------------
# Worktree: check out the PR branch, commit and push the session's changes
# ---------------------------------------------------------------------------


def prepare_worktree(
    worktree: Path, branch: str, runner: Runner | None = None
) -> tuple[bool, str]:
    """Point the worker's worktree at the PR head, DETACHED: no local branch
    is created or moved (an operator may have the PR branch checked out
    elsewhere), every push names ``HEAD:refs/heads/<branch>``, and the
    daemon's stranded-work harvester refuses a detached worktree, so the PR's
    commits are never turned into a twin PR. Returns (ok, head sha | error)."""
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch or "") or ".." in branch:
        return False, f"unsafe branch name {branch!r}"
    fetch = _git(
        runner,
        worktree,
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    if fetch.returncode != 0:
        return False, f"fetch failed: {(fetch.stderr or '').strip()[:300]}"
    co = _git(runner, worktree, "checkout", "--detach", f"origin/{branch}")
    if co.returncode != 0:
        return False, f"checkout failed: {(co.stderr or '').strip()[:300]}"
    head = _git(runner, worktree, "rev-parse", "HEAD")
    sha = (head.stdout or "").strip()
    if head.returncode != 0 or not sha:
        return False, "HEAD unreadable after checkout"
    return True, sha


def remote_head(
    worktree: Path, branch: str, runner: Runner | None = None
) -> str | None:
    """The PR branch's sha on origin right now (not the local tracking ref)."""
    res = _git(runner, worktree, "ls-remote", "origin", f"refs/heads/{branch}")
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    return res.stdout.split()[0]


def undo_session_commits(
    worktree: Path, head: str, runner: Runner | None = None
) -> bool:
    """A session told not to commit may still have: fold its commits back
    into the index so they go through the stage's own path checks. Returns
    True when HEAD had moved."""
    now = (_git(runner, worktree, "rev-parse", "HEAD").stdout or "").strip()
    if not now or now == head:
        return False
    _git(runner, worktree, "reset", "--soft", head)
    return True


def pr_changed_files(worktree: Path, runner: Runner | None = None) -> list[str] | None:
    res = _git(runner, worktree, "diff", "--name-only", "origin/main...HEAD")
    if res.returncode != 0:
        return None
    return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]


def _matches(path: str, prefixes: tuple[str, ...], files: tuple[str, ...]) -> bool:
    return path in files or any(path.startswith(p) for p in prefixes)


def working_changes(worktree: Path, runner: Runner | None = None) -> list[str] | None:
    """Every path the session changed (incl. deletions and rename sources)."""
    res = _git(runner, worktree, "status", "--porcelain", "--untracked-files=all")
    if res.returncode != 0:
        return None
    paths: list[str] = []
    for line in (res.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            old, path = path.split(" -> ", 1)
            paths.append(old.strip().strip('"'))
        paths.append(path.strip().strip('"'))
    return [p for p in dict.fromkeys(paths) if p]


def forbidden_changes(paths: list[str], project_root: Path) -> list[str]:
    """Paths a stage commit must never carry: the oracle, session config, CI,
    and human-protected files (fails closed on an unreadable config)."""
    bad = [p for p in paths if _matches(p, FORBIDDEN_PREFIXES, FORBIDDEN_FILES)]
    config = _read_project_config(project_root)
    if config is None:
        return bad + [
            "gatesmith.json (unreadable: human-protected check failed closed)"
        ]
    try:
        bad += _import("employee_activator")._human_protected_violations(paths, config)
    except Exception:
        return bad + ["(human-protected check unavailable)"]
    return list(dict.fromkeys(bad))


def commit_and_push(
    worktree: Path,
    branch: str,
    paths: list[str],
    message: str,
    runner: Runner | None = None,
) -> tuple[bool, str]:
    """Commit ``paths`` and fast-forward the PR branch. Returns (ok, sha|error).
    On a failed push the local branch is reset to the remote head, so nothing
    is left for the stranded-work harvester to turn into a twin PR."""
    to_add = [p for p in paths if not p.startswith(_UNCOMMITTED_PREFIXES)]
    if not to_add:
        return False, "nothing to commit"
    add = _git(runner, worktree, "add", "-A", "--", *to_add)
    if add.returncode != 0:
        return False, f"git add failed: {(add.stderr or '').strip()[:300]}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="challenger-msg-", delete=False, encoding="utf-8"
    ) as f:
        f.write(message)
        msg_path = f.name
    try:
        commit = _git(runner, worktree, "commit", "-F", msg_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(msg_path)
    if commit.returncode != 0:
        return (
            False,
            f"git commit failed: {(commit.stderr or commit.stdout or '').strip()[:300]}",
        )
    push = _git(
        runner, worktree, "push", "origin", f"HEAD:refs/heads/{branch}", timeout=120
    )
    if push.returncode != 0:
        _git(runner, worktree, "checkout", "--detach", "--force", f"origin/{branch}")
        return False, f"push failed: {(push.stderr or '').strip()[:300]}"
    sha = (_git(runner, worktree, "rev-parse", "HEAD").stdout or "").strip()
    return True, sha


def discard_changes(worktree: Path, branch: str, runner: Runner | None = None) -> None:
    """Leave the worktree at the remote PR head (nothing to harvest)."""
    _git(runner, worktree, "checkout", "--detach", "--force", f"origin/{branch}")
    _git(runner, worktree, "clean", "-fd")


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    ran: bool  # a session started (False: nothing ran — a transient failure)
    ok: bool  # it exited on its own with code 0
    error: str | None = None
    report_text: str | None = None
    exit_code: int | None = None


def run_session(
    prompt_for: Callable[[Path], str],
    *,
    cwd: Path,
    task_id: str,
    timeout_seconds: int,
    model: str | None,
    employee_id: str,
    system_reinforcement: str,
    scratch_dir: Path,
    report_name: str | None = None,
) -> SessionResult:
    """One fresh worker session in ``cwd`` with the employee activator's
    launch and supervision (own process group, PID registry so a cancel can
    kill it, spawn gate, hard cap at ``timeout_seconds``). ``prompt_for``
    receives the scratch directory. The session gets no employee memory and
    no context files: its whole input is the prompt."""
    ea = _import("employee_activator")
    ap = _import("agent_providers")
    spawn_guard = _import("spawn_guard")
    try:
        act = ea.get_activation_config()
        fallback = act["modelByComplexity"].get("complex", act["model"])
        spec = ap.resolve_provider(ea.load_config(), "complex", fallback_model=fallback)
        if model:
            spec = dataclasses.replace(spec, model=model)
        health = ap.check_provider_health(spec)
        if not health.healthy:
            return SessionResult(
                False, False, f"provider preflight failed: {health.message}"
            )
        agentic = act["agenticMode"]
        cmd = ap.build_command(
            spec,
            project_root=Path(cwd),
            complexity="complex",
            allowed_tools=agentic["allowedTools"] if agentic["enabled"] else [],
            disallowed_tools=agentic["disallowedTools"] if agentic["enabled"] else [],
            system_reinforcement=system_reinforcement,
        )
        env = ap.prepare_environment(spec, employee_id=employee_id)
        env["FORGE_SCRATCH_DIR"] = str(scratch_dir)
        env["TMPDIR"] = str(scratch_dir)
        prompt = ap.prepare_prompt(
            spec,
            prompt_for(scratch_dir) + ea._scratch_prompt_section(scratch_dir),
            system_reinforcement,
        )
    except Exception as exc:  # noqa: BLE001
        return SessionResult(False, False, f"session setup failed: {exc}")

    spawn_guard.assert_spawn_allowed("challenger_stage.run_session", subprocess.Popen)
    log_dir = Path(ea.get_company_dir()) / "logs" / "workers"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_dir / f"worker-{task_id}.log", "w", encoding="utf-8") as log:
            log.write(f"=== Challenger stage session: {task_id} ({employee_id}) ===\n")
            log.flush()
            try:
                proc = ea._launch_worker_session(
                    cmd, cwd=str(cwd), env=env, task_id=task_id
                )
            except ea.WorkerSpawnRefused as exc:
                return SessionResult(False, False, str(exc))
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError) as exc:
                ea._kill_worker_group(proc)
                return SessionResult(
                    True, False, f"session exited before its prompt: {exc}"
                )
            lines: list[str] = []
            last = [time.time()]

            def _pump(pipe, prefix: str) -> None:
                try:
                    for line in iter(pipe.readline, ""):
                        lines.append(line)
                        log.write(prefix + line)
                        log.flush()
                        last[0] = time.time()
                finally:
                    pipe.close()

            pumps = [
                threading.Thread(target=_pump, args=(proc.stdout, ""), daemon=True),
                threading.Thread(
                    target=_pump, args=(proc.stderr, "[ERR] "), daemon=True
                ),
            ]
            for p in pumps:
                p.start()
            kind, detail = ea._supervise_worker(
                proc,
                str(cwd),
                log,
                last_activity=last,
                output_lines=lines,
                hard_cap_seconds=timeout_seconds,
                # A reviewer may read for a long time without writing: only
                # the hard cap ends it.
                stall_seconds=timeout_seconds + 1,
                stall_min_elapsed_seconds=timeout_seconds + 1,
            )
            for p in pumps:
                p.join(timeout=5)
            log.write(f"\nExit code: {proc.returncode} {kind or ''} {detail}\n")
    except Exception as exc:  # noqa: BLE001
        return SessionResult(True, False, f"session error: {exc}")
    report_text = None
    if report_name:
        with contextlib.suppress(OSError):
            report_text = (scratch_dir / report_name).read_text(encoding="utf-8")
    if kind:
        return SessionResult(
            True, False, f"{kind}: {detail}", report_text, proc.returncode
        )
    if ea._killed_outside_supervisor(proc.returncode):
        return SessionResult(
            True,
            False,
            f"killed outside the supervisor (exit {proc.returncode})",
            report_text,
            proc.returncode,
        )
    return SessionResult(True, proc.returncode == 0, None, report_text, proc.returncode)


# ---------------------------------------------------------------------------
# Reports and verdicts
# ---------------------------------------------------------------------------


def parse_report(text: str | None) -> tuple[dict | None, str | None]:
    """The session's JSON report, or (None, why)."""
    if not text or not text.strip():
        return None, "the challenger wrote no report"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"report is not JSON: {exc}"
    if not isinstance(data, dict):
        return None, "report is not a JSON object"
    verdict = str(data.get("verdict") or "").strip().lower().replace("_", "-")
    if verdict not in VERDICTS:
        return None, f"report verdict {data.get('verdict')!r} is not one of {VERDICTS}"
    data["verdict"] = verdict
    for key in ("findings", "fixes", "missing_surfaces", "entries"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("checks"), dict):
        data["checks"] = {}
    return data, None


def _open_blocking(report: dict) -> list[dict]:
    return [
        f
        for f in report.get("findings", [])
        if isinstance(f, dict)
        and str(f.get("severity", "")).lower() in ("critical", "high")
        and str(f.get("status", "open")).lower() != "fixed"
    ]


def decide_verdict(
    report: dict | None,
    report_error: str | None,
    *,
    entry_ids: list[str],
    oracle: dict | None,
    push_error: str | None = None,
    forbidden: list[str] | None = None,
    oracle_touched: list[str] | None = None,
) -> tuple[str, list[str]]:
    """The stage's verdict: the session's, overruled by what the stage can
    check itself. The session can only ever lower a pass, never raise one."""
    if forbidden:
        return VERDICT_NEEDS_HUMAN, [
            "the challenger changed paths a stage commit may not touch: "
            + ", ".join(forbidden[:10])
        ]
    if push_error:
        return VERDICT_NEEDS_HUMAN, [
            f"the challenger's fixes could not be pushed: {push_error}"
        ]
    if report is None:
        return VERDICT_NEEDS_HUMAN, [report_error or "no report"]
    reasons: list[str] = []
    verdict = report["verdict"]
    if oracle_touched:
        return VERDICT_NEEDS_HUMAN, [
            "the PR changes the acceptance oracle ("
            + ", ".join(oracle_touched[:10])
            + ")"
        ]
    if verdict != VERDICT_PASSED:
        return verdict, reasons
    blocking = _open_blocking(report)
    if blocking:
        reasons.append(
            f"{len(blocking)} critical/high finding(s) left open: "
            + "; ".join(str(f.get("title", "?")) for f in blocking[:5])
        )
    failed_checks = [
        k for k, v in report.get("checks", {}).items() if str(v).lower() == "fail"
    ]
    if failed_checks:
        reasons.append("checks failing: " + ", ".join(sorted(failed_checks)))
    if entry_ids:
        if not oracle or oracle.get("environment_error"):
            why = (oracle or {}).get("environment_error") or "not run"
            return VERDICT_NEEDS_HUMAN, [
                f"the goal's entries could not be run by the stage: {why}"
            ]
        results = oracle.get("results") or {}
        failing = [
            i
            for i in entry_ids
            if (results.get(i) or {}).get("verdict") != "CONSISTENT"
        ]
        if failing:
            reasons.append(
                "entries not CONSISTENT on the pushed head: " + ", ".join(failing)
            )
    return (VERDICT_INCOMPLETE if reasons else VERDICT_PASSED), reasons


def render_comment(
    *,
    task_id: str,
    round_no: int,
    max_rounds: int,
    verdict: str,
    reasons: list[str],
    report: dict | None,
    oracle: dict | None,
    entry_ids: list[str],
    pushed_sha: str | None,
    next_step: str,
) -> str:
    out = [
        f"<!-- forge-challenger task={task_id} round={round_no} verdict={verdict} -->",
        f"## Challenger review — round {round_no}/{max_rounds}: **{verdict}**",
        "",
        "_Blind adversarial review by the Gatesmith challenger stage. The challenger "
        "never merges._",
        "",
    ]
    if report and report.get("summary"):
        out += [str(report["summary"]), ""]
    if reasons:
        out += ["**Why the stage settled on this verdict:**"]
        out += [f"- {r}" for r in reasons]
        out += [""]
    findings = [f for f in (report or {}).get("findings", []) if isinstance(f, dict)]
    if findings:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings.sort(key=lambda f: order.get(str(f.get("severity", "")).lower(), 9))
        out += [
            "### Findings",
            "",
            "| Severity | Area | Finding | Status |",
            "|---|---|---|---|",
        ]
        for f in findings:
            title = str(f.get("title", "")).replace("|", "\\|")
            detail = str(f.get("detail", "")).replace("|", "\\|").replace("\n", " ")
            out.append(
                f"| {f.get('severity', '?')} | {f.get('area', '?')} | **{title}** — "
                f"{detail[:600]} | {f.get('status', 'open')} |"
            )
        out.append("")
    fixes = (report or {}).get("fixes") or []
    if fixes:
        out += ["### Fixes pushed" if pushed_sha else "### Fixes (not pushed)", ""]
        out += [f"- {x}" for x in fixes]
        out += [""]
    missing = (report or {}).get("missing_surfaces") or []
    if missing:
        out += ["### Missing spec surfaces", ""] + [f"- {x}" for x in missing] + [""]
    if entry_ids:
        out += [
            "### Acceptance entries",
            "",
            "| Entry | Challenger | Stage re-run |",
            "|---|---|---|",
        ]
        reported = {
            str(e.get("id")): str(e.get("result", "?"))
            for e in (report or {}).get("entries", [])
            if isinstance(e, dict)
        }
        results = (oracle or {}).get("results") or {}
        for i in entry_ids:
            stage = (results.get(i) or {}).get("verdict") or (
                "env error" if (oracle or {}).get("environment_error") else "not run"
            )
            out.append(f"| {i} | {reported.get(i, 'not reported')} | {stage} |")
        out.append("")
    checks = (report or {}).get("checks") or {}
    if checks:
        out += [
            "**Checks:** " + ", ".join(f"{k} {v}" for k, v in sorted(checks.items())),
            "",
        ]
    if pushed_sha:
        out += [f"Pushed `{pushed_sha[:12]}` to this branch.", ""]
    out += [f"**Next:** {next_step}"]
    return "\n".join(out)


def _save_report(company_dir: Path, task_id: str, payload: dict) -> str:
    path = _state_dir(company_dir) / REPORTS_SUBDIR / f"{task_id}.json"
    _write_json(path, payload)
    return str(path)


def _report_for_prompt(report_file: str | None) -> str:
    if not report_file:
        return "(no report recorded)"
    try:
        payload = json.loads(Path(report_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"(report unreadable: {report_file})"
    body = {
        "verdict": payload.get("verdict"),
        "reasons": payload.get("reasons"),
        "report": payload.get("report"),
        "stage_entry_results": (payload.get("oracle") or {}).get("results"),
    }
    return (
        "```json\n"
        + _truncate(json.dumps(body, indent=2), REPORT_MAX_CHARS, "report")
        + "\n```"
    )


# ---------------------------------------------------------------------------
# Executing a claimed stage task (called from operation_loop)
# ---------------------------------------------------------------------------


@dataclass
class Deps:
    """Everything with a side effect, injectable for tests."""

    runner: Runner | None = None  # git and gh
    session: Callable[..., SessionResult] = run_session
    ledger: Callable[..., dict] = run_goal_entries
    judge_gate: Callable[..., Any] | None = None
    hold_for_new_head: Callable[..., Any] | None = None
    goals: dict[str, Any] | None = None
    scratch_root: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _result(action: str, task_id: str, reason: str, task: dict, **extra: Any) -> dict:
    return {
        "action": action,
        "reason": reason,
        "task_id": task_id,
        "result": {"executed": "completed"}.get(action, action),
        "task": task,
        "escalation": None,
        "challenger": extra,
    }


def execute_claimed(
    task: dict,
    *,
    queue_path: Path,
    execution_cwd: str | None,
    release: Callable[..., dict],
    deps: Deps | None = None,
) -> dict:
    """Run a claimed challenger or continuation task end to end and release
    it. Never returns "failed": a transient problem requeues the task with a
    back-off (up to MAX_ATTEMPTS, then the PR goes to a human); everything
    else ends "completed" with the outcome on the PR and in the stage state.
    An unexpected error is treated as transient, so the task is never left
    in_progress and never reaches the builder's failure recovery."""
    task_id = str(task.get("task_id"))
    try:
        return _execute_claimed(
            task,
            queue_path=Path(queue_path),
            execution_cwd=execution_cwd,
            release=release,
            deps=deps or Deps(),
        )
    except Exception as exc:  # noqa: BLE001
        error = f"stage error: {type(exc).__name__}: {exc}"[:500]
        branch = str((task.get("challenger") or {}).get("branch") or "")
        if execution_cwd and branch:
            with contextlib.suppress(Exception):
                discard_changes(Path(execution_cwd), branch, (deps or Deps()).runner)
        if int(task.get("challenger_attempts", 0)) + 1 >= MAX_ATTEMPTS:
            pr = pr_number((task.get("challenger") or {}).get("pr_url"))
            if pr:
                with contextlib.suppress(Exception):
                    set_stage_label(pr, LABEL_NEEDS_HUMAN, (deps or Deps()).runner)
                    post_comment(
                        pr,
                        f"<!-- forge-challenger task={task_id} error -->\n"
                        f"The challenger stage failed {MAX_ATTEMPTS} times "
                        f"and gave up: {error}",
                        (deps or Deps()).runner,
                    )
            released = release(
                queue_path=Path(queue_path),
                task_id=task_id,
                result="completed",
                error=error,
            )
            return _result(
                "executed", task_id, error, (released or {}).get("task") or task
            )
        _requeue_with_backoff(Path(queue_path), task_id, error)
        return _result("requeued", task_id, error, task)


def _execute_claimed(
    task: dict,
    *,
    queue_path: Path,
    execution_cwd: str | None,
    release: Callable[..., dict],
    deps: Deps,
) -> dict:
    task_id = str(task.get("task_id"))
    meta = task.get("challenger") or {}
    queue_path = Path(queue_path)
    company_dir = queue_path.parent.parent
    project_root = company_dir.resolve().parent
    config = load_config(project_root)
    pr_url = str(meta.get("pr_url") or "")
    pr = pr_number(pr_url) or ""
    branch = str(meta.get("branch") or "")
    round_no = int(meta.get("round") or 1)
    kind = meta.get("kind")

    def _finish(reason: str, **extra: Any) -> dict:
        released = release(
            queue_path=queue_path, task_id=task_id, result="completed", error=None
        )
        return _result(
            "executed", task_id, reason, (released or {}).get("task") or task, **extra
        )

    def _transient(error: str) -> dict:
        attempts = int(task.get("challenger_attempts", 0)) + 1
        if attempts >= MAX_ATTEMPTS:
            _settle(
                company_dir,
                pr,
                pr_url,
                deps,
                config,
                task_id,
                round_no,
                VERDICT_NEEDS_HUMAN,
                [f"stage {kind} failed {attempts} times: {error}"],
                None,
                None,
                [],
                None,
                cwd=project_root,
            )
            return _finish(f"gave up after {attempts} attempts: {error}")
        _requeue_with_backoff(queue_path, task_id, error)
        return _result("requeued", task_id, error, task)

    parent_id = str(meta.get("parent_task_id") or "")

    def _still_wanted() -> bool:
        """Neither this task nor its parent was cancelled or held."""
        if task_is_parked(queue_path, task_id):
            return False
        lane_now, parent_now = _find(_load_queue(queue_path), parent_id)
        return not parent_is_abandoned(
            lane_now, parent_now, pr_url, deps.runner, project_root
        )

    def _stopped(reason: str) -> dict:
        if execution_cwd and branch:
            discard_changes(Path(execution_cwd), branch, deps.runner)
        released = release(
            queue_path=queue_path, task_id=task_id, result="completed", error=None
        )
        return _result(
            "executed", task_id, reason, (released or {}).get("task") or task
        )

    if kind not in (KIND_CHALLENGER, KIND_CONTINUATION) or not pr or not branch:
        return _finish(
            f"malformed stage task (kind={kind!r}, pr={pr_url!r}, branch={branch!r})"
        )
    if not execution_cwd:
        # Never check a PR branch out in the primary checkout.
        return _transient("stage tasks run only in a daemon worktree")
    worktree = Path(execution_cwd)

    queue = _load_queue(queue_path)
    lane, parent = _find(queue, str(meta.get("parent_task_id") or ""))
    if parent_is_abandoned(lane, parent, pr_url, deps.runner, project_root):
        _update_pr_state(
            company_dir,
            pr_url,
            phase="abandoned",
            history_add={
                "at": _iso(),
                "event": "parent_gone",
                "task_id": task_id,
                "lane": lane,
            },
        )
        return _finish(
            f"parent task no longer owns an open PR (lane={lane}); nothing to do"
        )

    goals = deps.goals if deps.goals is not None else load_goals(company_dir)
    goal = goals.get(str(meta.get("goal_id")))
    if goal is None:
        _settle(
            company_dir,
            pr,
            pr_url,
            deps,
            config,
            task_id,
            round_no,
            VERDICT_NEEDS_HUMAN,
            [f"goal {meta.get('goal_id')} is not in vision.md"],
            None,
            None,
            [],
            None,
            cwd=project_root,
        )
        return _finish("goal not found")

    template = load_template(kind)
    if template is None:
        _settle(
            company_dir,
            pr,
            pr_url,
            deps,
            config,
            task_id,
            round_no,
            VERDICT_NEEDS_HUMAN,
            [f"prompt template missing: {template_path(kind)}"],
            None,
            None,
            [],
            None,
            cwd=project_root,
        )
        return _finish("template missing")

    ok, head = prepare_worktree(worktree, branch, deps.runner)
    if not ok:
        return _transient(f"worktree: {head}")

    changed = pr_changed_files(worktree, deps.runner)
    if changed is None:
        # Fail closed: never open a session on a diff it cannot see.
        return _transient("could not list the PR's changed files")
    session_config = [
        p for p in changed if _matches(p, SESSION_CONFIG_PREFIXES, SESSION_CONFIG_FILES)
    ]
    if session_config:
        _settle(
            company_dir,
            pr,
            pr_url,
            deps,
            config,
            task_id,
            round_no,
            VERDICT_NEEDS_HUMAN,
            [
                "the PR changes session configuration ("
                + ", ".join(session_config[:10])
                + "); no automated session is run on it"
            ],
            None,
            None,
            [],
            None,
            cwd=worktree,
        )
        return _finish("PR changes session configuration")
    oracle_touched = [p for p in changed if p.startswith(ORACLE_PREFIX)]

    entry_ids = goal_entry_ids(goal)
    spec_path = project_root / "SPEC.md"
    try:
        spec_text = (worktree / "SPEC.md").read_text(encoding="utf-8")
        spec_path = worktree / "SPEC.md"
    except OSError:
        spec_text = ""
    spec_text = with_split_spec(worktree, spec_text)
    history = company_dir.resolve() / "state" / "acceptance" / "runs.jsonl"
    history_path = history if history.exists() else None
    scratch = Path(
        tempfile.mkdtemp(
            prefix="fs-ch-", dir=str(deps.scratch_root) if deps.scratch_root else None
        )
    )
    ledger_state = scratch / "ledger"
    goal_text = f"{goal.name} {goal.description}"
    values = {
        "PR_REF": f"PR #{pr} ({pr_url})",
        "ROUND": str(round_no),
        "MAX_ROUNDS": str(config.max_rounds),
        "WORKTREE": str(worktree),
        "BRANCH": branch,
        "HEAD_SHA": head,
        "GOAL_ROW": goal_row(goal),
        "SPEC_PATH": str(spec_path),
        "SPEC_EXCERPT": spec_excerpt(spec_text, goal_text),
        "ENTRY_IDS": ", ".join(entry_ids) or "(none named by the goal's metric)",
        "ACCEPTANCE_CASES": acceptance_cases(worktree, entry_ids),
        "RUN_ENTRIES_COMMAND": run_entries_command(
            worktree, entry_ids, ledger_state, history_path
        ),
        "DIFF": pr_diff(worktree, deps.runner) if kind == KIND_CHALLENGER else "",
        "TASK_DESCRIPTION": str(
            (parent or {}).get("description") or (parent or {}).get("title") or ""
        ),
        "REPORT": _report_for_prompt(meta.get("report_file")),
    }
    report_name = "challenger-report.json"

    def _prompt_for(scratch_dir: Path) -> str:
        return render(
            template, {**values, "REPORT_PATH": str(scratch_dir / report_name)}
        )

    try:
        _update_pr_state(
            company_dir,
            pr_url,
            phase="challenging" if kind == KIND_CHALLENGER else "continuing",
            active_task_id=task_id,
            round=round_no,
            history_add={
                "at": _iso(),
                "event": f"{kind}_started",
                "task_id": task_id,
                "head": head,
            },
        )
        session = deps.session(
            _prompt_for,
            cwd=worktree,
            task_id=task_id,
            timeout_seconds=config.timeout_seconds,
            model=config.model,
            employee_id=STAGE_ACTOR
            if kind == KIND_CHALLENGER
            else "challenger-continuation",
            system_reinforcement=(
                SYSTEM_REINFORCEMENT_CHALLENGER
                if kind == KIND_CHALLENGER
                else SYSTEM_REINFORCEMENT_CONTINUATION
            ),
            scratch_dir=scratch,
            report_name=report_name if kind == KIND_CHALLENGER else None,
        )
        if not _still_wanted():
            # Cancelled or held (itself or its parent) while it ran: no push,
            # no label, no next round.
            return _stopped("stopped by an operator")
        if not session.ran:
            discard_changes(worktree, branch, deps.runner)
            return _transient(session.error or "session did not start")

        # The session was told not to commit or push; make sure. Its commits
        # are folded back into the index (they then go through the stage's
        # path checks), and a branch that moved on origin means the review
        # is of a head nobody will merge.
        undo_session_commits(worktree, head, deps.runner)
        remote = remote_head(worktree, branch, deps.runner)
        if remote != head:
            discard_changes(worktree, branch, deps.runner)
            _settle(
                company_dir,
                pr,
                pr_url,
                deps,
                config,
                task_id,
                round_no,
                VERDICT_NEEDS_HUMAN,
                [
                    f"the PR branch moved on origin during the {kind} run "
                    f"(reviewed {head[:12]}, now {(remote or 'unreadable')[:12]})"
                ],
                None,
                None,
                [],
                None,
                cwd=worktree,
            )
            return _finish("PR branch moved during the run")

        paths = working_changes(worktree, deps.runner) or []
        if kind == KIND_CONTINUATION:
            return _finish_continuation(
                task,
                task_id,
                session,
                paths,
                worktree,
                branch,
                pr,
                pr_url,
                round_no,
                goal,
                config,
                company_dir,
                project_root,
                deps,
                _finish,
                _transient,
                _still_wanted,
                _stopped,
            )
        return _finish_challenger(
            task_id,
            session,
            paths,
            worktree,
            branch,
            head,
            pr,
            pr_url,
            round_no,
            goal,
            entry_ids,
            oracle_touched,
            ledger_state,
            history_path,
            config,
            company_dir,
            project_root,
            parent,
            deps,
            _finish,
            _transient,
            _still_wanted,
            _stopped,
        )
    finally:
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)


def _finish_continuation(
    task,
    task_id,
    session,
    paths,
    worktree,
    branch,
    pr,
    pr_url,
    round_no,
    goal,
    config,
    company_dir,
    project_root,
    deps,
    _finish,
    _transient,
    _still_wanted,
    _stopped,
) -> dict:
    if not paths:
        if not session.ok:
            return _transient(
                f"continuation session failed with no change: {session.error}"
            )
        _settle(
            company_dir,
            pr,
            pr_url,
            deps,
            config,
            task_id,
            round_no,
            VERDICT_NEEDS_HUMAN,
            ["the continuation round changed nothing"],
            None,
            None,
            [],
            None,
            cwd=worktree,
        )
        return _finish("continuation produced no change")
    bad = forbidden_changes(paths, project_root)
    if bad:
        discard_changes(worktree, branch, deps.runner)
        _settle(
            company_dir,
            pr,
            pr_url,
            deps,
            config,
            task_id,
            round_no,
            VERDICT_NEEDS_HUMAN,
            [
                "the continuation changed paths a stage commit may not touch: "
                + ", ".join(bad[:10])
            ],
            None,
            None,
            [],
            None,
            cwd=worktree,
        )
        return _finish("continuation touched forbidden paths")
    if not _still_wanted():
        return _stopped("stopped by an operator before the push")
    ok, sha = commit_and_push(
        worktree,
        branch,
        paths,
        f"feat(daemon): continue {goal.id} after challenger round {round_no} [{task_id}]\n\n"
        f"{'Session ended: ' + session.error if session.error else ''}\n",
        deps.runner,
    )
    if not ok:
        return _transient(f"continuation push: {sha}")
    _hold_new_head(pr, deps, worktree)
    nxt = build_stage_task(
        kind=KIND_CHALLENGER,
        parent={"task_id": task["challenger"].get("parent_task_id")},
        pr_url=pr_url,
        branch=branch,
        round_no=round_no + 1,
        goal_id=goal.id,
        config=config,
    )
    added = enqueue_stage_task(company_dir, nxt)
    set_stage_label(pr, LABEL_PENDING, deps.runner, worktree)
    post_comment(
        pr,
        f"<!-- forge-challenger task={task_id} round={round_no} continuation -->\n"
        f"Continuation after challenger round {round_no}: pushed `{sha[:12]}`"
        + (f" (session ended early: {session.error})" if session.error else "")
        + f". Challenger round {round_no + 1} of {config.max_rounds} is queued.",
        deps.runner,
        worktree,
    )
    _update_pr_state(
        company_dir,
        pr_url,
        phase="challenging",
        round=round_no + 1,
        active_task_id=nxt["task_id"] if added else None,
        history_add={
            "at": _iso(),
            "event": "continuation_pushed",
            "task_id": task_id,
            "sha": sha,
        },
    )
    return _finish(
        f"continuation pushed {sha[:12]}; challenger round {round_no + 1} queued"
    )


def _finish_challenger(
    task_id,
    session,
    paths,
    worktree,
    branch,
    head,
    pr,
    pr_url,
    round_no,
    goal,
    entry_ids,
    oracle_touched,
    ledger_state,
    history_path,
    config,
    company_dir,
    project_root,
    parent,
    deps,
    _finish,
    _transient,
    _still_wanted,
    _stopped,
) -> dict:
    report, report_error = parse_report(session.report_text)
    if report is None and not session.ok:
        report_error = f"{report_error}; session: {session.error}"
    pushed_sha: str | None = None
    push_error: str | None = None
    forbidden: list[str] = []
    if paths and report is not None:
        forbidden = forbidden_changes(paths, project_root)
        if forbidden:
            discard_changes(worktree, branch, deps.runner)
        elif not _still_wanted():
            return _stopped("stopped by an operator before the push")
        else:
            ok, out = commit_and_push(
                worktree,
                branch,
                paths,
                f"fix(challenger): {goal.id} round {round_no} review fixes [{task_id}]\n\n"
                + "\n".join(f"- {x}" for x in (report.get("fixes") or [])[:30])
                + "\n",
                deps.runner,
            )
            if ok:
                pushed_sha = out
                _hold_new_head(pr, deps, worktree)
            else:
                push_error = out
    elif paths:
        # No usable report: its edits are unexplained, so they are not pushed.
        discard_changes(worktree, branch, deps.runner)

    # The oracle check runs on the final diff, whatever the session did.
    final = pr_changed_files(worktree, deps.runner)
    if final is not None:
        oracle_touched = [f for f in final if f.startswith(ORACLE_PREFIX)]
    oracle = None
    if report is not None and entry_ids and not forbidden and not push_error:
        try:
            oracle = deps.ledger(worktree, entry_ids, ledger_state, history_path)
        except Exception as exc:  # noqa: BLE001
            oracle = {"results": {}, "missing": [], "environment_error": str(exc)[:500]}

    if not _still_wanted():
        return _stopped("stopped by an operator before the verdict")
    verdict, reasons = decide_verdict(
        report,
        report_error,
        entry_ids=entry_ids,
        oracle=oracle,
        push_error=push_error,
        forbidden=forbidden,
        oracle_touched=oracle_touched,
    )
    _settle(
        company_dir,
        pr,
        pr_url,
        deps,
        config,
        task_id,
        round_no,
        verdict,
        reasons,
        report,
        oracle,
        entry_ids,
        pushed_sha,
        cwd=worktree,
        parent=parent,
        goal=goal,
        branch=branch,
        # The head this verdict is about: the stage's own push, else the head
        # the session reviewed. Never origin's head NOW: a push that landed
        # during the oracle run was reviewed by nobody (the tick re-reviews it).
        reviewed_sha=pushed_sha or head,
    )
    return _finish(
        f"challenger round {round_no}: {verdict}", verdict=verdict, pushed=pushed_sha
    )


def _hold_new_head(pr: str, deps: Deps, cwd: Path) -> None:
    """A stage push moved the head: the judge's verdict no longer applies."""
    try:
        hold = deps.hold_for_new_head or _import("verdict_hold").hold_for_new_head
        hold(pr, deps.runner, str(cwd))
    except Exception:  # noqa: BLE001 — ci.yml's verdict-hold job also re-applies
        pass


def _settle(
    company_dir: Path,
    pr: str,
    pr_url: str,
    deps: Deps,
    config: ChallengerConfig,
    task_id: str,
    round_no: int,
    verdict: str,
    reasons: list[str],
    report: dict | None,
    oracle: dict | None,
    entry_ids: list[str],
    pushed_sha: str | None,
    *,
    cwd: Path,
    parent: dict | None = None,
    goal: Any = None,
    branch: str = "",
    reviewed_sha: str | None = None,
) -> str:
    """Label, comment, record, and queue what comes next. Returns the phase."""
    report_file = _save_report(
        company_dir,
        task_id,
        {
            "task_id": task_id,
            "pr_url": pr_url,
            "round": round_no,
            "verdict": verdict,
            "reasons": reasons,
            "report": report,
            "oracle": oracle,
            "pushed_sha": pushed_sha,
            "at": _iso(),
        },
    )
    phase = {
        VERDICT_PASSED: "passed",
        VERDICT_INCOMPLETE: "incomplete",
        VERDICT_NEEDS_HUMAN: "needs_human",
    }[verdict]
    label = _VERDICT_LABEL[verdict]
    next_step = {
        VERDICT_PASSED: "merge path open (operator, or auto-merge where enabled, after the "
        "deliverable judge passes the current head).",
        VERDICT_NEEDS_HUMAN: "a human decides; the PR is held.",
    }.get(verdict, "")
    next_task: dict | None = None
    if verdict == VERDICT_INCOMPLETE:
        if round_no < config.max_rounds and goal is not None and parent is not None:
            next_task = build_stage_task(
                kind=KIND_CONTINUATION,
                parent=parent,
                pr_url=pr_url,
                branch=branch or _branch_of(company_dir, pr_url),
                round_no=round_no,
                goal_id=goal.id,
                config=config,
                report_file=report_file,
            )
            phase = "continuing"
            next_step = (
                f"a continuation build round on this branch is queued (round {round_no} of "
                f"{config.max_rounds}); no new PR."
            )
        else:
            label, phase = LABEL_NEEDS_HUMAN, "needs_human"
            reasons = [*reasons, f"maximum of {config.max_rounds} rounds reached"]
            next_step = "a human decides; the PR is held with this report."
    set_stage_label(pr, label, deps.runner, cwd)
    post_comment(
        pr,
        render_comment(
            task_id=task_id,
            round_no=round_no,
            max_rounds=config.max_rounds,
            verdict=verdict,
            reasons=reasons,
            report=report,
            oracle=oracle,
            entry_ids=entry_ids,
            pushed_sha=pushed_sha,
            next_step=next_step,
        ),
        deps.runner,
        cwd,
    )
    added = enqueue_stage_task(company_dir, next_task) if next_task else False
    _update_pr_state(
        company_dir,
        pr_url,
        phase=phase,
        round=round_no,
        last_verdict=verdict,
        reviewed_sha=reviewed_sha,
        active_task_id=next_task["task_id"] if added and next_task else None,
        history_add={
            "at": _iso(),
            "event": "verdict",
            "task_id": task_id,
            "verdict": verdict,
            "label": label,
            "report_file": report_file,
            "pushed_sha": pushed_sha,
        },
    )
    if verdict == VERDICT_PASSED and parent is not None:
        _rerun_judge(parent, pr_url, deps)
    return phase


def _branch_of(company_dir: Path, pr_url: str) -> str:
    return str(read_state(company_dir)["prs"].get(pr_url, {}).get("branch") or "")


def _rerun_judge(parent: dict, pr_url: str, deps: Deps) -> None:
    """The deliverable judge stays: judge the current head (a challenger push
    moved it). A positive verdict releases verdict-pending and arms auto-merge
    only where the project allows it — now that challenger:passed is on."""
    try:
        gate = deps.judge_gate or _import("pr_output_manager").run_deliverable_gate
        gate(
            parent.get("task_id"),
            parent.get("title", ""),
            parent.get("description", ""),
            pr_url,
            parent.get("branch_name"),
            daemon_executed=True,
        )
    except Exception:  # noqa: BLE001 — the daemon's merge loop judges a pending head too
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_run_entries(args: argparse.Namespace) -> int:
    ids = [
        i.strip()
        for i in str(args.ids or "").split(",")
        if i.strip() and i.strip() != "-"
    ]
    if not ids:
        print("no entry ids given")
        return 0
    state_dir = (
        Path(args.state_dir)
        if args.state_dir
        else Path(tempfile.mkdtemp(prefix="ch-ledger-"))
    )
    out = run_goal_entries(
        Path(args.project_dir),
        ids,
        state_dir,
        Path(args.history) if args.history else None,
    )
    if out.get("environment_error"):
        print(f"ENVIRONMENT ERROR: {out['environment_error']}")
        return 3
    failed = 0
    for entry_id in ids:
        rec = out["results"].get(entry_id)
        if rec is None:
            print(f"MISSING {entry_id}: no case or relation with this id")
            failed += 1
            continue
        passed = rec["verdict"] == "CONSISTENT"
        failed += 0 if passed else 1
        word = "PASS" if passed else ("ERROR" if rec["verdict"] == "ERROR" else "FAIL")
        print(f"{word} {entry_id}: {rec['verdict']} — {rec['detail']}")
    return 1 if failed else 0


def _cmd_status(args: argparse.Namespace) -> int:
    state = read_state(Path(args.company_dir))
    if not state["prs"]:
        print("challenger stage: no PRs engaged")
        return 0
    for pr_url, rec in sorted(state["prs"].items()):
        print(
            f"{_pr_ref(pr_url):>10}  {rec.get('goal_id', '?'):<5} round {rec.get('round', '?')}  "
            f"{rec.get('phase', '?'):<16} last={rec.get('last_verdict', '-')}  "
            f"active={rec.get('active_task_id') or '-'}"
        )
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    print(json.dumps(tick(Path(args.company_dir)), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="challenger_stage.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "run-entries", help="run a goal's acceptance entries the ledger way"
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument("--ids", required=True, help="comma-separated entry ids")
    p.add_argument("--state-dir", help="dependency-install marker directory")
    p.add_argument("--history", help="runs.jsonl for learned timeouts (read-only)")
    p.set_defaults(func=_cmd_run_entries)
    p = sub.add_parser("status", help="the stage's per-PR state")
    p.add_argument("--company-dir", default=".company")
    p.set_defaults(func=_cmd_status)
    p = sub.add_parser("tick", help="engage new product-goal PRs now")
    p.add_argument("--company-dir", default=".company")
    p.set_defaults(func=_cmd_tick)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
