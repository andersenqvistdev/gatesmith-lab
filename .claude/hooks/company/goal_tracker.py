#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest", "pytest-cov"]
# ///
"""
Goal Tracker — automated goal progress assessment for strategic planning.

P15 implementation: Reads goals from vision.md and assesses progress
using pluggable assessors.

Usage:
    # Assess all goals
    python goal_tracker.py assess

    # Assess specific goal
    python goal_tracker.py assess --goal G5

    # Show goal summary
    python goal_tracker.py summary

    # Export for strategic planner
    python goal_tracker.py export
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol

# -----------------------------------------------------------------------------
# Test Result Cache (retro-ai-014 optimization)
# -----------------------------------------------------------------------------
# Strategic planning runs G1 (coverage) and G3 (stability) assessments.
# Both previously ran full pytest, taking ~20 minutes total.
# This cache ensures pytest runs ONCE per planning cycle.


@dataclass
class TestResultCache:
    """Cache for pytest results to avoid redundant test runs.

    Used within a single planning cycle to share results between
    G1 (test coverage) and G3 (stability) assessments.
    """

    tests_passed: bool | None = None
    coverage_percent: float | None = None
    coverage_data: dict | None = None
    ran_at: float = 0.0
    # When the coverage number itself was read from a trusted source —
    # unlike ran_at, this is NOT refreshed by test-only runs (G3), so a
    # newer Python commit invalidates the cached percent (W1-P2).
    coverage_read_at: float = 0.0
    source: str = ""  # "pytest_run" or "cache"
    # G1's module-floor view of the same trusted read (see
    # _module_floor_report). Cached beside coverage_percent so a totals-only
    # coverage.json — trusted for the repository total, useless for a floor —
    # still counts as a completed read and does not send the non-lightweight
    # path back through a 10-minute pytest run every cycle. ``reason`` is why
    # there is no report, and is the string G1 reports as its distrust reason.
    module_floor: dict | None = None
    module_floor_reason: str = ""

    # Cache is valid for 5 minutes (single planning cycle)
    CACHE_TTL_SECONDS: int = 300

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        if self.ran_at == 0.0:
            return False
        age = time.time() - self.ran_at
        return age < self.CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        """Invalidate the cache."""
        self.tests_passed = None
        self.coverage_percent = None
        self.coverage_data = None
        self.ran_at = 0.0
        self.coverage_read_at = 0.0
        self.source = ""
        self.module_floor = None
        self.module_floor_reason = ""


# Global cache instance - shared across assessors within a planning cycle
_test_cache = TestResultCache()


def reset_test_cache() -> None:
    """Reset the global test cache.

    Call this between planning cycles or in tests to ensure fresh data.
    """
    global _test_cache
    _test_cache.invalidate()


def get_test_cache() -> TestResultCache:
    """Get the current test cache (for testing/debugging)."""
    return _test_cache


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


class GoalStatus(str, Enum):
    """Goal progress status."""

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    NOT_STARTED = "not_started"
    VERIFIED = "verified"


@dataclass
class GoalDefinition:
    """A goal from vision.md."""

    id: str  # "G5"
    name: str  # "Autonomy"
    description: str  # "Enable fully autonomous operation"
    success_metric: str  # "Org runs without prompts for 24h"
    owner: str  # "forge-cto"
    target_value: float = 1.0  # Normalized target (1.0 = 100%)
    category: str | None = (
        None  # WS-054-002: product, revenue, infrastructure, quality, autonomy
    )
    period: str | None = None  # e.g. "Q1 2026", "Q2 2026"
    period_status: str | None = None  # "active", "complete", "planned"
    depends_on: list[str] = field(default_factory=list)  # e.g. ["G1", "G2"]


@dataclass
class GoalAssessment:
    """Result of assessing a goal's progress."""

    goal_id: str
    goal_name: str
    description: str
    success_metric: str
    owner: str
    current_value: float  # 0.0 to 1.0 (normalized)
    target_value: float  # Usually 1.0
    progress_percent: int  # 0-100
    status: GoalStatus
    status_reason: str  # Why this status
    blockers: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    assessed_at: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # copied from GoalDefinition
    # WS-113-003: Velocity tracking (percent change per day)
    velocity_percent_per_day: float = 0.0
    velocity_trend: str = "unknown"  # "improving", "stalled", "regressing"

    def __post_init__(self):
        if not self.assessed_at:
            self.assessed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d["status"] = self.status.value
        return d


# -----------------------------------------------------------------------------
# Goal Assessor Protocol
# -----------------------------------------------------------------------------


class GoalAssessor(Protocol):
    """Protocol for goal-specific assessors."""

    def assess(self, goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
        """Assess progress toward a goal."""
        ...


# -----------------------------------------------------------------------------
# Built-in Assessors
# -----------------------------------------------------------------------------


# W1-P2 (2026-07-17): trust guards for coverage.json. A stale or subset
# measurement — or a file-count guess — must assess as unknown, never COMPLETE:
# the daemon steered on a phantom "Coverage at 100%" minted by the estimator.
G1_COVERAGE_MAX_AGE_SECONDS = 24 * 3600
G1_SCOPE_MIN_SOURCE_FILES = 10
G1_SCOPE_MIN_MEASURED_FRACTION = 0.3

# G1 (2026-09-04): the goal is a per-module FLOOR, not a repository total.
#
# The total hid the distribution the coverage lane actually works on: 91.5%
# overall while forge_daemon.py sat at 66%, and a total is exactly what the
# exclusion bias (#448) and the staleness narrowing (#383) had to be defended
# against. A floor asks one question per module -- is THIS file at or above
# the bar -- so current_value (the share of counted modules that are) moves
# only when a module crosses the line, and the next action is always the
# module furthest below it. backlog_generator.COVERAGE_MAX_PERCENT must stay
# at or above this (tests/test_backlog_generator.py pins it) or the lane
# cannot mint every module the goal counts as below floor.
#
# The bar is 0.90, not 0.80, and the two constraints that set it are the same
# one seen from both sides. Below: at 0.80 exactly ONE non-protected module in
# the measured tree was under the floor (validate_quality.py, 79.3%), so the
# goal would have reported COMPLETE after a single PR and stopped feeding the
# only origination lane -- the 2026-08-20 starvation, re-created. Above: 0.90
# is COVERAGE_MAX_PERCENT, so goal and lane now agree exactly and the
# invariant reads 90 >= 90; raising the floor past the ceiling would leave a
# band of modules that count against G1 and can never be minted. Measured
# 2026-09-05: 15 of 183 modules below 90, 5 below 85, 1 below 80.
MODULE_COVERAGE_FLOOR = 0.90

# Engine files the floor does not count (matched on basename). These are the
# maintenance-mode files from CLAUDE.md -- "don't let the engine fix the
# engine" -- and two of them carry gaps (forge_daemon.py 1,966 uncovered
# lines, employee_activator.py 409) above the coverage lane's tractable
# ceiling, so a floor that counted them could never be met by autonomous
# work. Deliberately the four-file maintenance list, not the broader
# work_allocator.ENGINE_PROTECTED_FILES auto-claim list.
PROTECTED_ENGINE_FILES = frozenset(
    {
        "forge_daemon.py",
        "employee_activator.py",
        "failure_recovery.py",
        "operation_loop.py",
    }
)

# Largest per-file gap still worth proposing as one task.
#
# Ranking coverage candidates by percentage puts the LEAST tractable file
# first: the worst-covered file in this repo is forge_daemon.py at 66% with
# 1,869 uncovered lines, which the backlog generator attempted five times
# before hitting its build ceiling ("Build ceiling: 5 builds >= 5, human
# review required"). Four consecutive QUEUE-FILL PRs were then spent on that
# same file, and four of them closed unmerged on irreconcilable conflicts.
#
# A worker closes a 30-line gap in one pass; it does not close a 1,869-line
# one, and every attempt costs a worktree, a PR and its tokens. Measured
# 2026-08-23 across 66 candidates below 90%: median gap 48 lines, and 64 of
# them sit at or under this ceiling — so it excludes the two monsters
# (448 and 1,869) and nothing else. Files above it need a planned
# decomposition, not another autonomous attempt.
#
# Fallback only. The live value is backlog_generator's, sourced at call time by
# _coverage_max_missing() so the two coverage lanes cannot drift apart on what
# counts as closeable work.
COVERAGE_TRACTABLE_MAX_MISSING = 400


def _coverage_max_missing() -> int:
    """Largest gap worth minting, sourced from backlog_generator when available."""
    try:
        from . import backlog_generator as bg
    except ImportError:
        try:
            import backlog_generator as bg  # type: ignore[no-redef]
        except ImportError:
            return COVERAGE_TRACTABLE_MAX_MISSING
    value = getattr(bg, "COVERAGE_MAX_MISSING_LINES", COVERAGE_TRACTABLE_MAX_MISSING)
    return int(value) if isinstance(value, int) else COVERAGE_TRACTABLE_MAX_MISSING


# Smallest gap worth spawning a worktree, a worker and a PR for. Mirrors
# backlog_generator.COVERAGE_MIN_MISSING_LINES (sourced at call time) so both
# mint paths agree on what counts as work: without a floor the ranking below
# happily proposes a one-line gap, which costs a full task to move coverage by
# a rounding error.
_COVERAGE_MIN_MISSING_FALLBACK = 25


def _coverage_min_missing() -> int:
    """Minimum gap worth minting, sourced from backlog_generator when available."""
    try:
        from . import backlog_generator as bg
    except ImportError:
        try:
            import backlog_generator as bg  # type: ignore[no-redef]
        except ImportError:
            return _COVERAGE_MIN_MISSING_FALLBACK
    value = getattr(bg, "COVERAGE_MIN_MISSING_LINES", _COVERAGE_MIN_MISSING_FALLBACK)
    return int(value) if isinstance(value, int) else _COVERAGE_MIN_MISSING_FALLBACK


_SOURCE_SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".worktrees"}


def _last_commit_timestamp(
    project_root: Path, pathspec: str | None = None
) -> float | None:
    """Unix timestamp of the last commit (optionally limited to a pathspec),
    or None outside a git repo."""
    cmd = ["git", "-C", str(project_root), "log", "-1", "--format=%ct"]
    if pathspec:
        cmd += ["--", pathspec]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _is_test_relpath(rel: Path) -> bool:
    return (
        rel.name.startswith("test_")
        or rel.name == "conftest.py"
        or "tests" in rel.parts
    )


def _count_source_py(root: Path) -> int:
    """Count non-test .py files under root, skipping junk directories."""
    count = 0
    try:
        for path in root.rglob("*.py"):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in _SOURCE_SKIP_DIRS for part in rel.parts):
                continue
            if _is_test_relpath(rel):
                continue
            count += 1
    except OSError:
        pass
    return count


def _expected_measured_count(project_root: Path) -> int:
    """How many .py files the project's coverage setup would measure.

    Preference order: [tool.coverage.run] source dirs from pyproject.toml,
    then git-tracked non-test .py files, then a filesystem walk. Test files
    are excluded throughout — coverage configs routinely omit them, so
    counting them would let a canonical-scope run look like a subset (this
    repo: source=[.claude/hooks] measures 176 files while a naive repo walk
    sees 1000, a third of them inside a stale gitignored .worktrees copy).
    """
    sources = None
    try:
        import tomllib

        with open(project_root / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)
        sources = cfg.get("tool", {}).get("coverage", {}).get("run", {}).get("source")
    except Exception:
        sources = None
    if isinstance(sources, list) and sources:
        count = 0
        for src in sources:
            src_root = project_root if src == "." else project_root / src
            if src_root.is_dir():
                count += _count_source_py(src_root)
        if count:
            return count

    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--", "*.py"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            tracked = [
                line
                for line in result.stdout.splitlines()
                if line.strip() and not _is_test_relpath(Path(line))
            ]
            if tracked:
                return len(tracked)
    except Exception:
        pass

    return _count_source_py(project_root)


def _coverage_staleness_helpers():
    """Per-file staleness helpers from backlog_generator, or None.

    Imported lazily so goal_tracker keeps no import-time dependency on the
    generator. When they cannot be imported the caller keeps the strict global
    refusal rather than trusting a measurement it has no way to scope.
    """
    try:
        from . import backlog_generator as bg
    except ImportError:
        try:
            import backlog_generator as bg  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return bg._python_paths_changed_since, bg._coverage_entry_is_stale
    except AttributeError:
        return None


def _coverage_excluding_stale_entries(
    cov_data: dict, measured_at: float, project_root: Path
) -> tuple[float, dict] | None:
    """Recompute coverage over the entries a later commit did not invalidate.

    Staleness is a PER-FILE property (#383): a commit touching one module says
    nothing about the coverage of the other 184. The global rule this softens
    discarded every measurement on the day's first Python merge, which left G1
    reading 0% against a real 87% for most of every day -- and a goal at 0%
    with an INFRA-ONLY action cannot originate any work.

    Returns (percent, trusted_files), or None when the surviving set cannot be
    trusted at all, in which case the caller keeps the strict refusal:
      - no per-file data to narrow with;
      - the changed-set came back empty even though the global timestamp says
        something changed, i.e. the query failed and staleness is
        indeterminate (NOT "nothing is stale");
      - an entry is missing the statement counts needed to recompute.
    """
    files = cov_data.get("files")
    if not isinstance(files, dict) or not files:
        return None

    helpers = _coverage_staleness_helpers()
    if helpers is None:
        return None
    changed_since, entry_is_stale = helpers

    changed = changed_since(measured_at, project_root)
    if not changed:
        return None

    trusted = {
        path: entry
        for path, entry in files.items()
        if isinstance(entry, dict) and not entry_is_stale(path, changed)
    }
    if not trusted:
        return None

    statements = 0
    covered = 0
    for entry in trusted.values():
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            return None
        num = summary.get("num_statements")
        hit = summary.get("covered_lines")
        if not isinstance(num, int) or not isinstance(hit, int):
            return None
        statements += num
        covered += hit

    if statements <= 0:
        return None

    recomputed = (covered / statements) * 100.0

    # Exclusion must never RAISE the reported figure.
    #
    # The excluded set is not a random sample: workers are pointed at the
    # least-covered files, so the entries a commit invalidates are
    # systematically below average, and dropping them inflates the remainder.
    # Measured 2026-08-23, hours after this narrowing shipped: 4 excluded files
    # averaged 77.2% against a trusted-set mean of 91.9%, lifting the reported
    # figure from 87.10% to 90.82% -- across the 90% target, minting a COMPLETE
    # for a goal that had not been met. Worse, it compounds: every successful
    # day of coverage work excludes more low-covered files.
    #
    # Taking the lower of the two keeps the narrowing's real benefit (a usable
    # number instead of a blanket refusal) while making exclusion strictly
    # conservative -- it can lower confidence, never manufacture it. The W1-P2
    # rule stands: never report a number you did not measure.
    reported = cov_data.get("totals", {})
    reported_percent = (
        reported.get("percent_covered") if isinstance(reported, dict) else None
    )
    if isinstance(reported_percent, (int, float)):
        recomputed = min(recomputed, float(reported_percent))

    return recomputed, trusted


def _read_trusted_coverage_files(
    coverage_file: Path, project_root: Path
) -> tuple[float | None, dict | None, str]:
    """Read coverage.json only if it is fresh and full-scope.

    Returns (percent_covered, per-file entries, "") when trustworthy, else
    (None, None, reason). The per-file map is the (possibly staleness-
    narrowed) ``files`` section the module floor is computed over; a
    totals-only file yields an empty map, which the caller treats as "no
    module measurement". Never raises: a malformed file must degrade to
    "unknown" for G1, not abort the whole goal-assessment batch.
    """
    try:
        with open(coverage_file) as f:
            cov_data = json.load(f)
        mtime = coverage_file.stat().st_mtime

        totals = cov_data.get("totals") or {}
        if not isinstance(totals, dict):
            return None, None, "coverage.json has no totals.percent_covered"
        percent = totals.get("percent_covered")
        if not isinstance(percent, (int, float)):
            return None, None, "coverage.json has no totals.percent_covered"

        # Freshness: a measurement older than the last change to Python
        # code describes code that no longer exists (docs/queue commits do
        # not invalidate it). Outside git, fall back to a wall-clock cap.
        #
        # A newer Python commit does not invalidate the files it never
        # touched, so narrow to the entries that are still trustworthy and
        # recompute over those (#383). Only when nothing survives -- or the
        # surviving set cannot be established -- does the whole measurement
        # fall, which is the strict behaviour this replaces.
        files = cov_data.get("files") or {}
        commit_ts = _last_commit_timestamp(project_root, pathspec="*.py")
        if commit_ts is not None:
            if mtime < commit_ts:
                narrowed = _coverage_excluding_stale_entries(
                    cov_data, mtime, project_root
                )
                if narrowed is None:
                    return (
                        None,
                        None,
                        "coverage.json predates the last Python change (stale)",
                    )
                percent, files = narrowed
        elif (time.time() - mtime) > G1_COVERAGE_MAX_AGE_SECONDS:
            return (
                None,
                None,
                "coverage.json older than 24h with no git history (stale)",
            )

        # Scope: a run that measured only a slice of the configured source
        # says nothing about total coverage (a single-module cov run can
        # honestly read 100%). Compare against what the coverage config
        # itself would measure, not a naive repo-wide file count. Applied to
        # the NARROWED set, so a commit that invalidates most of the scan
        # still fails the trust check rather than reporting a thin slice.
        measured = len(files) if isinstance(files, dict) else 0
        expected = _expected_measured_count(project_root)
        if expected >= G1_SCOPE_MIN_SOURCE_FILES and measured < (
            G1_SCOPE_MIN_MEASURED_FRACTION * expected
        ):
            return (
                None,
                None,
                f"coverage.json measures {measured} of ~{expected} source "
                "files expected by the coverage config (subset run)",
            )

        # Provenance: the measured files must be THIS project's files — a
        # coverage.json copied from (or generated for) another repo would
        # otherwise pass the count check and mint a phantom COMPLETE.
        if isinstance(files, dict) and files:
            sample = list(files)[:50]
            present = sum(1 for k in sample if (project_root / k).exists())
            if present < 0.5 * len(sample):
                return (
                    None,
                    None,
                    "coverage.json measures files not present in this "
                    "project (foreign measurement)",
                )

        return float(percent), (files if isinstance(files, dict) else {}), ""
    except Exception:
        return None, None, "coverage.json unreadable or malformed"


def _read_trusted_coverage(
    coverage_file: Path, project_root: Path
) -> tuple[float | None, str]:
    """Trusted repository total only: (percent_covered, "") or (None, reason)."""
    percent, _files, reason = _read_trusted_coverage_files(coverage_file, project_root)
    return percent, reason


def _module_floor_report(files: dict, project_root: Path) -> dict[str, Any]:
    """Per-module view of a trusted coverage.json ``files`` map.

    A module counts when it has statements, is not a protected engine file
    and still exists on disk (coverage.json outlives a deletion, and a task
    naming a removed file is a pointer the admission gate rejects). Returns
    how many modules were counted, how many sit below MODULE_COVERAGE_FLOOR,
    the share at or above it, and the modules below it furthest-first --
    the order the next action follows.
    """
    floor_percent = MODULE_COVERAGE_FLOOR * 100
    counted: list[tuple[float, int, str]] = []
    protected: list[str] = []
    for rel_path, entry in files.items():
        if not isinstance(rel_path, str) or not isinstance(entry, dict):
            continue
        if Path(rel_path).name in PROTECTED_ENGINE_FILES:
            protected.append(rel_path)
            continue
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        statements = summary.get("num_statements")
        percent = summary.get("percent_covered")
        if not isinstance(statements, int) or statements <= 0:
            continue
        if not isinstance(percent, (int, float)):
            continue
        missing = summary.get("missing_lines")
        if not isinstance(missing, int):
            covered = summary.get("covered_lines")
            missing = statements - (covered if isinstance(covered, int) else 0)
        if not (project_root / rel_path).exists():
            continue
        counted.append((float(percent), max(missing, 0), rel_path))

    below = sorted(c for c in counted if c[0] < floor_percent)
    total = len(counted)
    return {
        "modules_total": total,
        "modules_below_floor": len(below),
        "share_at_floor": ((total - len(below)) / total) if total else 0.0,
        "below_floor": [
            {"path": path, "percent": round(percent, 1), "missing_lines": missing}
            for percent, missing, path in below
        ],
        "protected_excluded": sorted(protected),
    }


def _read_module_floor(
    coverage_file: Path, project_root: Path
) -> tuple[dict | None, float | None, str]:
    """The module-floor report for a trusted coverage.json.

    Returns (report, repository_total_percent, "") when the file is
    trustworthy AND carries per-module data, else (None, total_or_None,
    reason). The total is handed back even when the report is not, so the
    cache and raw_data can still carry the number the old goal reported —
    but a missing report is the thing that decides G1's status, because a
    total says nothing about the distribution the floor is asking about.
    """
    percent, files, reason = _read_trusted_coverage_files(coverage_file, project_root)
    if percent is None:
        return None, None, reason
    if not files:
        return (
            None,
            float(percent),
            "coverage.json has no per-file data (totals-only measurement)",
        )
    report = _module_floor_report(files, project_root)
    if report["modules_total"] <= 0:
        return (
            None,
            float(percent),
            "coverage.json measures no countable modules "
            "(all empty, protected or absent from the tree)",
        )
    return report, float(percent), ""


def assess_test_coverage(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G1: no non-protected module below MODULE_COVERAGE_FLOOR.

    WS-013-005: Actually runs pytest with coverage instead of estimating.
    retro-ai-014: Uses cache to avoid redundant test runs with G3.
    P39: lightweight mode skips pytest entirely — uses existing coverage.json
    or file-based estimation.  Used by daemon to avoid 5-10 min blocking.
    W1-P2: only fresh, full-scope measurements count; without one the goal
    assesses as unknown (never COMPLETE from an estimate or a stale file).

    2026-09-04: the measured value is no longer the repository total. It is
    the SHARE of counted modules at or above the floor, so the goal moves
    only when a module crosses the line and the next action is always a
    named module rather than a percentage nobody owns. The trust guards are
    unchanged — an untrusted file still assesses as unknown, and a trusted
    file with no per-module section is unknown too (a total cannot answer a
    question about the distribution).
    """
    global _test_cache

    project_root = company_dir.parent
    coverage_file = project_root / "coverage.json"
    floor_report: dict | None = None
    total_percent: float | None = None
    distrust_reason = ""
    source = ""
    ran_tests = False
    tests_passed = False

    # retro-ai-014: Check cache first. The cached read is only honored
    # while no Python change postdates it — otherwise a freshly-stale
    # measurement would keep reporting COMPLETE for the TTL (and G3's
    # test-only refreshes of ran_at could revive it indefinitely).
    # Keyed on coverage_read_at, not on the report: a trusted totals-only
    # file legitimately produces no report, and re-deciding that every call
    # would drive the non-lightweight path back into pytest each cycle.
    if _test_cache.is_valid() and _test_cache.coverage_read_at > 0:
        commit_ts = _last_commit_timestamp(project_root, pathspec="*.py")
        read_at = _test_cache.coverage_read_at
        if commit_ts is not None and commit_ts > read_at:
            _test_cache.coverage_percent = None
            _test_cache.coverage_read_at = 0.0
            _test_cache.module_floor = None
            _test_cache.module_floor_reason = ""
        else:
            floor_report = _test_cache.module_floor
            total_percent = _test_cache.coverage_percent
            distrust_reason = _test_cache.module_floor_reason
            source = _test_cache.source or "cache"
            ran_tests = True
            tests_passed = _test_cache.tests_passed or False

    def _cache_read(report: dict | None, percent: float | None, reason: str) -> None:
        """Record a completed trusted read; an untrusted one caches nothing."""
        if percent is None:
            _test_cache.coverage_percent = None
            _test_cache.coverage_read_at = 0.0
            _test_cache.module_floor = None
            _test_cache.module_floor_reason = ""
            return
        _test_cache.coverage_percent = percent
        _test_cache.coverage_read_at = time.time()
        _test_cache.module_floor = report
        _test_cache.module_floor_reason = reason

    read_attempted = floor_report is not None or bool(distrust_reason)

    if not read_attempted and lightweight:
        # P39: In lightweight mode, read existing coverage.json or estimate.
        # Never run pytest — this keeps daemon cycles fast (<1s for G1).
        if coverage_file.exists():
            floor_report, total_percent, distrust_reason = _read_module_floor(
                coverage_file, project_root
            )
            if total_percent is not None:
                source = "coverage_json_file"
                ran_tests = True  # treat trusted file read as "ran"
                tests_passed = True  # assume passing if file is trusted
                _test_cache.tests_passed = True
                _test_cache.ran_at = time.time()
                _test_cache.source = "coverage_json_file"
            _cache_read(floor_report, total_percent, distrust_reason)
    elif not read_attempted:
        # WS-013-005: Actually run tests with coverage to get real data
        # WS-101: Add fcntl.flock to prevent multiple pytest processes
        # fighting over .coverage SQLite database (caused 130GB memory leak)
        lock_file_path = project_root / ".coverage.lock"
        try:
            # Mode "a", never "w": macOS APFS returns EDEADLK at open() on
            # a file with outstanding fcntl locks when opened with O_TRUNC
            # (PR #986 incident class; same rationale as QueueLock).
            with open(lock_file_path, "a") as lock_file:
                # Acquire exclusive lock - blocks if another pytest is running
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    # Use Popen for proper process control on timeout
                    proc = subprocess.Popen(
                        [
                            "uv",
                            "run",
                            "pytest",
                            "--cov=.",
                            "--cov-report=json",
                            "-q",
                            "--tb=no",
                            "--ignore=.venv",
                        ],
                        cwd=project_root,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,  # Process group for clean kill
                    )
                    try:
                        stdout, stderr = proc.communicate(timeout=600)
                        ran_tests = True
                        tests_passed = proc.returncode == 0

                        # retro-ai-014: Populate cache for G3 to reuse
                        _test_cache.tests_passed = tests_passed
                        _test_cache.ran_at = time.time()
                        _test_cache.source = "pytest_run"
                    except subprocess.TimeoutExpired:
                        # WS-101: Kill entire process group to prevent zombie
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                        proc.wait()  # Reap zombie
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            # uv/pytest not available or lock contention, fall back
            pass

        # Read coverage.json (freshly generated or pre-existing) — same
        # trust guards apply: a run that failed to produce fresh data must
        # not silently fall back to a stale file.
        if coverage_file.exists():
            floor_report, total_percent, distrust_reason = _read_module_floor(
                coverage_file, project_root
            )
            if total_percent is not None:
                source = "pytest_run" if ran_tests else "coverage_json_file"
            # retro-ai-014: Cache coverage data
            _cache_read(floor_report, total_percent, distrust_reason)

    # W1-P2: without a trustworthy measurement the estimate is context, not
    # data — it must never set status (318 test files × 0.03 saturated at
    # "100%" and reported G1 COMPLETE while the OKR said ~15%).
    estimated_percent: float | None = None
    if floor_report is None:
        test_files = list(project_root.glob("tests/**/*.py"))
        test_files = [f for f in test_files if f.name.startswith("test_")]
        estimated_percent = min(len(test_files) * 3.0, 100.0)

    # Single source of truth for G1's bar. This was a hardcoded 0.5 and drifted
    # silently: parse_goals_from_vision does not carry target_value (every
    # parsed goal defaults to 1.0), so the assessor could not read it off the
    # goal, and the literal here outlived the retarget in DEFAULT_GOALS. The
    # effect of that drift is a goal that reports COMPLETE against a bar nobody
    # believes in any more -- 86.62% "meets target 50%". Read the canonical
    # definition instead, so a retarget takes effect in one edit.
    target_value = _DEFAULT_GOALS_BY_ID["G1"].target_value
    floor_display = f"{MODULE_COVERAGE_FLOOR * 100:.0f}%"
    if floor_report is None:
        current_value = 0.0
        progress_percent = 0
        status = GoalStatus.AT_RISK
        reason = "No trustworthy per-module coverage measurement"
        if distrust_reason:
            reason += f" ({distrust_reason})"
        status_reason = (
            f"{reason}; test-file estimate ~{estimated_percent:.0f}% not used"
        )
    else:
        current_value = float(floor_report["share_at_floor"])
        modules_total = int(floor_report["modules_total"])
        below = int(floor_report["modules_below_floor"])
        progress = min(current_value / target_value, 1.0) if target_value else 0.0
        progress_percent = int(progress * 100)

        if current_value >= target_value:
            status = GoalStatus.COMPLETE
            status_reason = (
                f"All {modules_total} counted modules are at or above the "
                f"{floor_display} floor"
            )
        elif current_value >= 0.9:
            status = GoalStatus.ON_TRACK
            status_reason = (
                f"{below} of {modules_total} modules below the {floor_display} "
                f"floor ({current_value * 100:.0f}% at floor)"
            )
        else:
            status = GoalStatus.AT_RISK
            status_reason = (
                f"{below} of {modules_total} modules below the {floor_display} "
                f"floor ({current_value * 100:.0f}% at floor)"
            )

    if status == GoalStatus.COMPLETE:
        next_actions = []
    elif floor_report is None:
        # W1-P3 (2026-07-18): a full-suite coverage measurement is
        # deterministic long-running infra work (bin/gatesmith-coverage-nightly),
        # not an LLM-worker task -- a worker session cannot finish
        # `pytest --cov` over a large suite and gets correctly blocked by the
        # deliverable gate, burning a queue slot. The [INFRA-ONLY] prefix is
        # recognized by strategic_planner's queue-fill filter so this text
        # is never minted as a worker task; it still displays as-is wherever
        # next_actions is shown directly (e.g. /dashboard).
        next_actions = [
            "[INFRA-ONLY] Nightly coverage job (bin/gatesmith-coverage-nightly) "
            "produces coverage.json; no worker action needed. Once a "
            "measurement exists, add tests for the least-covered modules."
        ]
    else:
        # Name the modules, furthest below the floor first. The wording is
        # backlog_generator's coverage-signal shape ("<module> at N% coverage:
        # write tests for M uncovered lines") so the two origination lanes
        # speak about the same work in the same words -- and the pointer is
        # the REPO-RELATIVE path, because strategic_planner's brief-quality
        # gate resolves pointers against the project root and a bare basename
        # is not a file it can find.
        next_actions = [
            f"{m['path']} at {m['percent']:.0f}% coverage: write tests for "
            f"{m['missing_lines']} uncovered lines"
            for m in floor_report["below_floor"][:3]
        ]

    worst = floor_report["below_floor"][:5] if floor_report else []
    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=target_value,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            # The repository total is context now, not the metric. Kept so a
            # dashboard or a history snapshot that read it still reads it.
            "coverage_percent": total_percent,
            "module_floor_percent": MODULE_COVERAGE_FLOOR * 100,
            "modules_total": floor_report["modules_total"] if floor_report else 0,
            "modules_below_floor": (
                floor_report["modules_below_floor"] if floor_report else 0
            ),
            "modules_worst": worst,
            "protected_excluded": (
                floor_report["protected_excluded"] if floor_report else []
            ),
            "source": source
            or ("estimated" if estimated_percent is not None else "none"),
            "ran_tests": ran_tests,
            "measured": floor_report is not None,
            "distrust_reason": distrust_reason or None,
            "estimated_percent": estimated_percent,
        },
    )


def assess_tutorials(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G2: Tutorial adoption goal."""
    project_root = company_dir.parent

    # Count tutorial files
    docs_dir = project_root / "docs"
    tutorials_dir = docs_dir / "tutorials"
    getting_started = project_root / "GETTING_STARTED.md"

    tutorial_count = 0
    if tutorials_dir.exists():
        tutorial_count += len(list(tutorials_dir.glob("*.md")))
    if getting_started.exists():
        tutorial_count += 1

    # Target is 3 tutorials
    target = 3
    current_value = min(tutorial_count / target, 1.0)
    progress_percent = int(current_value * 100)

    if tutorial_count >= target:
        status = GoalStatus.COMPLETE
        status_reason = f"{tutorial_count} tutorials published"
    elif tutorial_count > 0:
        status = GoalStatus.ON_TRACK
        status_reason = f"{tutorial_count}/{target} tutorials, in progress"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No tutorials found"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Create tutorial docs"] if status != GoalStatus.COMPLETE else [],
        raw_data={"tutorial_count": tutorial_count, "target": target},
    )


def assess_stability(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G3: Stability goal (zero P0 bugs).

    retro-ai-014: Reuses G1 test results from cache instead of running pytest again.
    P39: lightweight mode skips pytest — uses cache or assumes passing.
    """
    global _test_cache

    # Check for open issues labeled P0 or critical
    # For now, check if tests pass
    project_root = company_dir.parent
    inconclusive = False

    # retro-ai-014: Reuse cached test results from G1 assessment
    if _test_cache.is_valid() and _test_cache.tests_passed is not None:
        tests_pass = _test_cache.tests_passed
    elif lightweight:
        # P39: In lightweight mode, assume tests pass if no recent failure
        # data.  The cache from G1 lightweight assessment will have been
        # populated if coverage.json existed, so reaching here means no
        # data at all — default to True to avoid blocking daemon cycles.
        tests_pass = True
    else:
        # Fallback: run tests if cache is stale (shouldn't happen in planning cycle)
        # WS-101 pattern (see assess_test_coverage): use Popen with its own
        # process group and killpg on timeout, not subprocess.run(timeout=...)
        # — the latter only kills the immediate "uv" process, leaving any
        # subprocess pytest itself spawns (e.g. tests that shell out) as an
        # orphan that keeps consuming memory after we give up on it.
        try:
            proc = subprocess.Popen(
                ["uv", "run", "pytest", "-q", "--tb=no", "-x"],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group for clean kill
            )
            try:
                proc.communicate(timeout=600)  # 10 minutes - tests can take a while
                tests_pass = proc.returncode == 0

                # Populate cache for future use
                _test_cache.tests_passed = tests_pass
                _test_cache.ran_at = time.time()
                _test_cache.source = "pytest_run_g3"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()  # reap zombie
                # A timeout means we don't know whether tests pass — it is
                # not evidence they fail. Don't poison the shared cache or
                # report a false "Tests failing" (retro: G7/G15 assessor
                # bugs were the assessor lying, not the product breaking).
                inconclusive = True
                tests_pass = True
        except Exception:
            inconclusive = True
            tests_pass = True

    # Also check for TODO/FIXME with P0/critical in source files only
    critical_issues = 0
    try:
        result = subprocess.run(
            [
                "grep",
                "-r",
                "-i",
                "-c",
                "--include=*.py",
                "--include=*.ts",
                "--include=*.js",
                "--exclude-dir=.venv",
                "--exclude-dir=.git",
                "--exclude-dir=logs",
                "--exclude-dir=node_modules",
                "--exclude=goal_tracker.py",  # Don't count ourselves
                "TODO.*P0\\|FIXME.*critical\\|BUG.*critical",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    count = line.split(":")[-1]
                    if count.isdigit():
                        critical_issues += int(count)
    except Exception:
        pass

    if tests_pass and critical_issues == 0:
        status = GoalStatus.COMPLETE
        status_reason = "Tests pass, no critical issues found"
        current_value = 1.0
    elif tests_pass:
        status = GoalStatus.AT_RISK
        status_reason = f"Tests pass but {critical_issues} critical TODOs found"
        current_value = 0.7
    else:
        status = GoalStatus.AT_RISK
        status_reason = "Tests failing"
        current_value = 0.3

    if inconclusive:
        # A timed-out/errored assessment must not assert either "tests
        # pass" or "tests fail" as fact — say plainly that we don't know.
        status_reason = (
            f"Test run inconclusive (timed out or errored) — {status_reason}"
        )

    # Generate concrete next_actions: name specific tests/TODOs instead of bare goal metric
    next_actions: list[str] = []
    if not tests_pass:
        next_actions.append("Fix failing tests to improve stability")
    elif critical_issues > 0:
        next_actions.append(f"Address {critical_issues} critical TODO(s)")

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=int(current_value * 100),
        status=status,
        status_reason=status_reason,
        blockers=["Fix failing tests"] if not tests_pass else [],
        next_actions=next_actions,
        raw_data={
            "tests_pass": tests_pass,
            "critical_issues": critical_issues,
            "inconclusive": inconclusive,
        },
    )


def assess_enterprise(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G4: Enterprise audit capabilities."""
    project_root = company_dir.parent

    # Check for audit-export command
    audit_cmd = project_root / ".claude" / "commands" / "audit-export.md"
    sbom_cmd = project_root / ".claude" / "commands" / "sbom.md"

    features = []
    if audit_cmd.exists():
        features.append("audit-export")
    if sbom_cmd.exists():
        features.append("sbom")

    current_value = len(features) / 2  # 2 enterprise features expected
    progress_percent = int(current_value * 100)

    if current_value >= 1.0:
        status = GoalStatus.COMPLETE
        status_reason = f"Enterprise features complete: {', '.join(features)}"
    elif current_value > 0:
        status = GoalStatus.ON_TRACK
        status_reason = f"Partial: {', '.join(features)}"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No enterprise features implemented"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        raw_data={"features": features},
    )


def assess_autonomy(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G5: Autonomy goal (24h operation without prompts).

    Reads live daemon heartbeat for real-time status instead of relying
    on cached snapshots. Checks actual task completion and uptime data.
    WS-014: Fixed to return COMPLETE when daemon is actively working.
    """
    project_root = company_dir.parent

    # Check for autonomy infrastructure
    components = {
        "daemon": (
            project_root / ".claude" / "hooks" / "company" / "forge_daemon.py"
        ).exists(),
        "operation_loop": (
            project_root / ".claude" / "hooks" / "company" / "operation_loop.py"
        ).exists(),
        "work_queue": (company_dir / "state/work_queue.json").exists(),
        "circuit_breaker": (
            project_root / ".claude" / "hooks" / "company" / "loop_monitor.py"
        ).exists(),
        "proactive": (
            project_root / ".claude" / "hooks" / "company" / "initiative_engine.py"
        ).exists(),
        "strategic_planner": (
            project_root / ".claude" / "hooks" / "company" / "strategic_planner.py"
        ).exists(),
    }

    # Check daemon status from live heartbeat
    daemon_running = False
    daemon_healthy = False
    tasks_completed = 0
    tasks_failed = 0
    uptime_seconds = 0
    circuit_breaker_state = "unknown"
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    if not heartbeat_file.exists():
        heartbeat_file = company_dir / "daemon.heartbeat"
    if heartbeat_file.exists():
        try:
            with open(heartbeat_file) as f:
                hb = json.load(f)
                # Check if heartbeat is recent (within 5 minutes)
                hb_time = datetime.fromisoformat(
                    hb.get("last_heartbeat", "2000-01-01T00:00:00+00:00")
                )
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                daemon_running = age < 300

                # Read operational metrics from heartbeat
                tasks_completed = hb.get("tasks_completed_this_session", 0)
                tasks_failed = hb.get("tasks_failed_this_session", 0)
                uptime_seconds = hb.get("uptime_seconds", 0)
                circuit_breaker_state = hb.get("circuit_breaker_state", "unknown")

                # Daemon is healthy if running with closed circuit breaker
                daemon_healthy = daemon_running and circuit_breaker_state == "closed"
        except Exception:
            pass

    # Also check PID file as fallback
    if not daemon_running:
        pid_file = company_dir / "runtime/daemon.pid"
        if pid_file.exists():
            try:
                with open(pid_file) as f:
                    pid_data = json.load(f)
                    pid = pid_data.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        import os

                        os.kill(pid, 0)
                        daemon_running = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
            except Exception:
                pass

    # Calculate progress
    infra_score = sum(components.values()) / len(components)

    # Determine status based on real operational data
    if daemon_healthy and infra_score >= 0.8 and tasks_completed > 0:
        # Daemon running, circuit breaker healthy, tasks completing
        status = GoalStatus.COMPLETE
        success_rate = (
            tasks_completed / (tasks_completed + tasks_failed) * 100
            if (tasks_completed + tasks_failed) > 0
            else 0
        )
        uptime_hours = uptime_seconds / 3600
        status_reason = (
            f"Daemon active: {tasks_completed} tasks completed "
            f"({success_rate:.0f}% success), "
            f"uptime {uptime_hours:.1f}h, circuit breaker {circuit_breaker_state}"
        )
        current_value = 1.0
    elif daemon_running and infra_score >= 0.8:
        # Daemon running but no tasks yet or circuit breaker tripped
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"Daemon running (circuit breaker: {circuit_breaker_state}), "
            f"{tasks_completed} tasks completed"
        )
        current_value = 0.8
    elif infra_score >= 0.8:
        status = GoalStatus.AT_RISK
        status_reason = "Infrastructure ready but daemon not running"
        current_value = 0.6
    elif infra_score >= 0.5:
        status = GoalStatus.AT_RISK
        status_reason = f"Partial infrastructure ({int(infra_score * 100)}%)"
        current_value = infra_score * 0.6
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Autonomy infrastructure not built"
        current_value = infra_score * 0.3

    progress_percent = int(current_value * 100)
    missing = [k for k, v in components.items() if not v]
    blockers = [f"Missing: {m}" for m in missing]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=blockers,
        next_actions=["Start daemon with /daemon start"] if not daemon_running else [],
        raw_data={
            "components": components,
            "daemon_running": daemon_running,
            "daemon_healthy": daemon_healthy,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "uptime_seconds": uptime_seconds,
            "circuit_breaker_state": circuit_breaker_state,
        },
    )


def _economics_recorded(company_dir: Path) -> tuple[int, int, int]:
    """Return (consumed tokens, billable tokens, task executions) from the store.

    All three come from the data the trackers actually write. Keying the goal on
    file existence instead reported COMPLETE for a token meter that had never
    recorded a single token.

    The two token figures are kept separate on purpose. ``token_usage`` holds
    full consumption; ``cost_tracking`` holds the cache-discounted billable
    equivalent of the *same* activity. Summing them produces a number that
    corresponds to nothing -- which is the exact failure this assessor exists
    to prevent, so the reported figure is consumption alone.
    """
    data_path = company_dir / "state/efficiency_data.json"
    if not data_path.exists():
        return 0, 0, 0

    try:
        with open(data_path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0, 0, 0

    def _total_tokens(section: str) -> int:
        totals = data.get(section, {})
        totals = totals.get("totals", {}) if isinstance(totals, dict) else {}
        value = totals.get("total_tokens", 0)
        return value if isinstance(value, int) else 0

    executions = data.get("task_executions", [])
    return (
        _total_tokens("token_usage"),
        _total_tokens("cost_tracking"),
        len(executions) if isinstance(executions, list) else 0,
    )


ECONOMICS_NEXT_ACTIONS = {
    "token_tracking": (
        "Capture token counts from the agent provider and feed "
        "efficiency_tracker.record_task_cost() — nothing records tokens today"
    ),
    "efficiency": "Record task executions via record_task_efficiency()",
}


def assess_economics(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G6: Economics goal (token consumption tracking).

    Scope: economics is complete when token and efficiency tracking are
    actually *recording*, not merely installed. The previous check keyed
    ``token_tracking`` on whether ``efficiency_tracker.py`` existed on disk,
    which is always true in a Gatesmith checkout, so G6 reported COMPLETE while
    its token totals sat at zero.
    """
    consumed_tokens, billable_tokens, recorded_executions = _economics_recorded(
        company_dir
    )

    features = {
        # Either store carrying data proves the meter is running; the reported
        # figure is consumption, never the two added together.
        "token_tracking": consumed_tokens > 0 or billable_tokens > 0,
        "efficiency": recorded_executions > 0,
    }
    recorded_tokens = consumed_tokens or billable_tokens

    current_value = sum(features.values()) / len(features)
    progress_percent = int(current_value * 100)

    if current_value >= 1.0:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"Token tracking recording: {recorded_tokens:,} tokens consumed over "
            f"{recorded_executions} task executions"
        )
    elif current_value >= 0.5:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"Economics {progress_percent}% complete — "
            f"{recorded_executions} task executions recorded, "
            f"{recorded_tokens:,} tokens recorded"
        )
    else:
        status = GoalStatus.AT_RISK
        status_reason = "Economics needs token tracking setup"

    missing = [k for k, v in features.items() if not v]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=[ECONOMICS_NEXT_ACTIONS.get(m, f"Set up {m}") for m in missing],
        raw_data={
            "features": features,
            "recorded_tokens": recorded_tokens,
            "consumed_tokens": consumed_tokens,
            "billable_tokens": billable_tokens,
            "recorded_executions": recorded_executions,
        },
    )


# W1-P2 (2026-07-17): G7's success rate is computed over the most recent
# window of operation instead of lifetime-cumulative (which let early
# bring-up debt cap the metric forever). Sessions are prorated by how much
# of their duration overlaps the recency window (counts are per-session, so
# a long session's tasks are attributed uniformly across its runtime), and
# a minimum recent sample is required before the rate can mint COMPLETE or
# an improve-success-rate next-action.
G7_SUCCESS_WINDOW_DAYS = 7.0
G7_MIN_WINDOW_TASKS = 5


def _parse_window_ts(value: Any) -> float | None:
    """ISO-8601 string → epoch seconds; naive timestamps are read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _shipped_task_ids(company_dir: Path) -> set[str]:
    """Task IDs whose final queue state is a completed, PR-bearing success.

    The failure log is append-only and per-ATTEMPT: an entry is written the
    moment an attempt fails and is never retracted when a later attempt (or a
    recovery run) ships the work. Every task in this set therefore has a failure
    entry that no longer describes reality.
    """
    queue_file = company_dir / "state/work_queue.json"
    if not queue_file.exists():
        return set()
    try:
        with open(queue_file) as f:
            queue = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()
    shipped: set[str] = set()
    for task in queue.get("completed", []) or []:
        if not isinstance(task, dict):
            continue
        if task.get("status") == "completed" and task.get("task_id"):
            shipped.add(task["task_id"])
    return shipped


def _recent_failure_examples(
    company_dir: Path, cutoff_ts: float | None, limit: int = 5
) -> list[dict[str, str]]:
    """Latest attempt per task_id from task_failure_log.jsonl since cutoff_ts.

    G7's "improve success rate" next_action used to be a bare percentage
    with no pointer to which tasks actually failed, forcing every dispatch
    to re-derive the failing task IDs from raw logs before it could tell a
    real bug from an already-fixed or correct-by-design rejection. cutoff_ts
    of None means "no window anchor available" — falls back to unfiltered
    (used only when daemon_metrics has no bounded sessions to anchor on).

    Tasks that ultimately shipped are excluded (see :func:`_shipped_task_ids`).
    Naming them sent every G7 dispatch chasing already-merged work — on
    2026-07-25 the live autofill task pointed at task-...7dbc86 (merged as
    PR #303) and task-...e6113c (merged as PR #300) as things to go fix.
    """
    failure_log = company_dir / "state/task_failure_log.jsonl"
    if not failure_log.exists():
        return []
    shipped = _shipped_task_ids(company_dir)
    latest_by_task: dict[str, dict[str, Any]] = {}
    try:
        with open(failure_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = entry.get("task_id")
                if not task_id or task_id in shipped:
                    continue
                ts = _parse_window_ts(entry.get("timestamp"))
                if cutoff_ts is not None and (ts is None or ts < cutoff_ts):
                    continue
                existing = latest_by_task.get(task_id)
                if existing is None or (ts or 0) >= existing["_ts"]:
                    latest_by_task[task_id] = {
                        "task_id": task_id,
                        "title": str(entry.get("title", ""))[:80],
                        "category": str(entry.get("category", "unknown")),
                        "_ts": ts or 0,
                    }
    except OSError:
        return []
    examples = sorted(latest_by_task.values(), key=lambda e: e["_ts"], reverse=True)
    for e in examples:
        e.pop("_ts", None)
    return examples[:limit]


def _find_uncovered_modules(
    project_root: Path,
    coverage_percent: float | None = None,
    limit: int = 3,
    *,
    target_fraction: float = 1.0,
) -> list[str]:
    """Coverage gaps still short of target, most closeable first.

    NOTE (2026-09-04): G1 no longer calls this — it reports the modules
    below MODULE_COVERAGE_FLOOR straight from ``_module_floor_report``, which
    counts the same denominator it grades. This stays as the tractability
    ranking (COVERAGE_TRACTABLE_MAX_MISSING and friends) for any caller that
    wants "which gap can one PR actually close", a question the floor does
    not answer.

    Each entry is a repo-relative path plus its measured numbers, e.g.
    ``.claude/hooks/company/forge_daemon.py (66% covered, 1870 lines
    uncovered)``. Both halves matter: strategic_planner's brief-quality gate
    demands a pointer to a file that exists AND a measured value, so a bare
    module stem produces a brief that is refused and never becomes work.

    Ordering is by absolute gap ascending, NOT by percentage: the worst-covered
    file is usually the least tractable one, and proposing it repeatedly buys
    nothing but conflicts (see COVERAGE_TRACTABLE_MAX_MISSING).

    Two earlier rules made this return nothing in every realistic case:
      - an early ``coverage_percent >= 0.5`` bail. The argument is a FRACTION,
        so any repo above 50% coverage got no candidates at all.
      - selecting only files at exactly 0%. Once the coverage lane has run for
        a while there are none left (measured 2026-08-22: 0 of 185 files at
        zero, while forge_daemon.py sat at 66.5% with 1,870 uncovered lines).

    Returns [] when there is no trustworthy per-file data, so the assessor
    falls back to generic next_actions rather than inventing a target.
    """
    coverage_file = project_root / "coverage.json"
    if not coverage_file.exists():
        return []

    target_percent = max(0.0, min(target_fraction, 1.0)) * 100
    min_missing = _coverage_min_missing()
    max_missing = _coverage_max_missing()

    try:
        with open(coverage_file) as f:
            cov_data = json.load(f)
        files = cov_data.get("files") or {}
        if not isinstance(files, dict):
            return []

        candidates: list[tuple[float, int, str]] = []
        for file_path, file_data in files.items():
            if not isinstance(file_data, dict):
                continue
            summary = file_data.get("summary") or {}
            percent = summary.get("percent_covered")
            missing = summary.get("missing_lines")
            statements = summary.get("num_statements")
            if not isinstance(percent, (int, float)) or not isinstance(missing, int):
                continue
            if not isinstance(statements, int) or statements <= 0:
                continue
            if percent >= target_percent:
                continue
            # A gap only becomes work if one PR can close it, and only earns a
            # worker if closing it is worth the trip.
            if missing > max_missing or missing < min_missing:
                continue
            # Only name a file the worker can actually open.
            if not (project_root / file_path).exists():
                continue
            candidates.append((float(percent), missing, file_path))

        # Most closeable first; among equals, the worse-covered file.
        candidates.sort(key=lambda c: (c[1], c[0]))
        return [
            f"{path} ({percent:.0f}% covered, {missing} "
            f"{'line' if missing == 1 else 'lines'} uncovered)"
            for percent, missing, path in candidates[:limit]
        ]
    except Exception:
        return []


def _find_failing_ci_tests(project_root: Path) -> list[str]:
    """Find specific failing test names from recent CI failures for G16.

    Returns concrete test file/function names that failed recently so tasks
    can target specific tests instead of generic "fix CI failures".
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "3",
                "--json",
                "number,statusCheckRollup",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            return []

        # Look for any failed checks
        failed_tests = set()
        for pr in prs:
            rollup = pr.get("statusCheckRollup", [])
            for check in rollup:
                if check.get("conclusion") != "SUCCESS" and check.get("name"):
                    # Extract test name if available
                    test_name = check.get("name", "")
                    if test_name and "test" in test_name.lower():
                        failed_tests.add(test_name[:80])

        return list(failed_tests)[:3]
    except Exception:
        return []


def assess_sustained_autonomy(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G7: Sustained Autonomy — 7 days cumulative, >90% success.

    Unlike G5 (Autonomy) which checks current session status, G7 requires
    CUMULATIVE multi-session data: 7 days total uptime across multiple daemon
    restarts, with a >90% task success rate sustained over the most recent
    G7_SUCCESS_WINDOW_DAYS of recorded operation.
    """
    total_uptime_seconds = 0
    total_completed = 0
    total_failed = 0
    # (start_epoch, end_epoch, tasks_completed, tasks_failed) per uptime
    # window — feeds the windowed success rate below. Either epoch may be
    # None when the record carries no parseable timestamp.
    window_records: list[tuple[float | None, float | None, int, int]] = []

    # 1. Read uptime from daemon_metrics.json (queryable heartbeat records).
    # This is the primary uptime source — it captures all sessions including
    # those killed with SIGKILL, using the last_uptime_seconds checkpoint
    # written every 30s by update_heartbeat().
    daemon_metrics_file = company_dir / "state/daemon_metrics.json"
    used_daemon_metrics = False
    if daemon_metrics_file.exists():
        try:
            with open(daemon_metrics_file) as f:
                dm = json.load(f)
            uptime_windows = dm.get("uptime_windows", [])
            for idx, window in enumerate(uptime_windows):
                dur = window.get("duration_seconds")
                if isinstance(dur, (int, float)) and dur > 0:
                    # Closed session with known duration
                    total_uptime_seconds += dur
                elif window.get("ended_at") is None:
                    # Open session (still running or killed) — use last checkpoint
                    checkpoint = window.get("last_uptime_seconds")
                    if isinstance(checkpoint, (int, float)) and checkpoint > 0:
                        total_uptime_seconds += checkpoint
                    else:
                        # No checkpoint yet — compute from started_at
                        try:
                            started = datetime.fromisoformat(
                                window["started_at"].replace("Z", "+00:00")
                            )
                            total_uptime_seconds += (
                                datetime.now(timezone.utc) - started
                            ).total_seconds()
                        except Exception:
                            pass
                # Accumulate task counts from checkpointed windows
                completed = window.get("tasks_completed", 0)
                failed = window.get("tasks_failed", 0)
                total_completed += completed
                total_failed += failed
                start_ts = _parse_window_ts(window.get("started_at"))
                end_ts = _parse_window_ts(window.get("ended_at"))
                if end_ts is None:
                    span = window.get("duration_seconds")
                    if not (isinstance(span, (int, float)) and span > 0):
                        span = window.get("last_uptime_seconds")
                    if (
                        start_ts is not None
                        and isinstance(span, (int, float))
                        and span > 0
                    ):
                        end_ts = start_ts + span
                    elif start_ts is not None:
                        # Open window with no checkpoint: only the NEWEST
                        # record can be the live session — older open
                        # records are crashed-session zombies (production
                        # carries dozens) and stamping them "now" would
                        # drag the recency anchor to wall clock forever.
                        if idx == len(uptime_windows) - 1:
                            end_ts = datetime.now(timezone.utc).timestamp()
                        else:
                            end_ts = start_ts
                if start_ts is None and end_ts is not None:
                    start_ts = end_ts
                window_records.append((start_ts, end_ts, completed, failed))
            used_daemon_metrics = True
        except Exception:
            pass

    # 2. Read current session data from daemon heartbeat.
    # Used for has_heartbeat detection and as uptime fallback when no daemon_metrics.
    # Check both locations: new runtime/ path and legacy root path
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    if not heartbeat_file.exists():
        heartbeat_file = company_dir / "daemon.heartbeat"
    if heartbeat_file.exists():
        try:
            with open(heartbeat_file) as f:
                hb = json.load(f)
            if not used_daemon_metrics:
                # Fallback: use heartbeat for uptime when daemon_metrics unavailable
                total_uptime_seconds += hb.get("uptime_seconds", 0)
                total_completed += hb.get("tasks_completed_this_session", 0)
                total_failed += hb.get("tasks_failed_this_session", 0)
        except Exception:
            pass

    # 3. Read historical session data from session_state.json for task counts.
    # Only used for uptime when both daemon_metrics and heartbeat are unavailable.
    session_state_file = company_dir / "state/session_state.json"
    if session_state_file.exists():
        try:
            with open(session_state_file) as f:
                session_data = json.load(f)
            if not used_daemon_metrics:
                total_uptime_seconds += session_data.get("total_uptime_seconds", 0)
            # Task counts from session_state are used when daemon_metrics has no tasks
            if total_completed == 0 and total_failed == 0:
                loop_metrics = session_data.get("loop_metrics", {})
                total_completed += loop_metrics.get("tasks_completed", 0)
                total_failed += loop_metrics.get("tasks_failed", 0)
                for session in session_data.get("sessions", []):
                    total_completed += session.get("tasks_completed", 0)
                    total_failed += session.get("tasks_failed", 0)
        except Exception:
            pass

    # 3. Read historical goal snapshots from strategic_state.json
    strategic_state_file = company_dir / "state/strategic_state.json"
    if strategic_state_file.exists():
        try:
            with open(strategic_state_file) as f:
                strategic_data = json.load(f)
            for snapshot in strategic_data.get("goal_snapshots", []):
                snap_goal_id = snapshot.get("goal_id", "")
                if snap_goal_id in ("G5", "G7"):
                    raw = snapshot.get("raw_data", {})
                    snap_uptime = raw.get("uptime_seconds", 0)
                    if snap_uptime > 0:
                        total_uptime_seconds += snap_uptime
        except Exception:
            pass

    # 4. Calculate cumulative uptime in days
    uptime_days = total_uptime_seconds / 86400.0

    # 5. Calculate success rate — windowed over the most recent
    # G7_SUCCESS_WINDOW_DAYS of recorded operation (anchored to the newest
    # window, not wall clock, so an idle daemon still reports its last week).
    # Lifetime accumulation let one rocky bring-up week permanently cap the
    # metric near 64% (626 clean completions needed to recover), driving an
    # endless "improve success rate" autofill treadmill — see
    # TestAssessSustainedAutonomy. Uptime stays cumulative: the 7-day uptime
    # target has no equivalent permanent-debt problem.
    total_tasks = total_completed + total_failed
    lifetime_success_rate = total_completed / total_tasks if total_tasks > 0 else 0.0

    # Each session is prorated by how much of its runtime overlaps the
    # recency window ending at the newest session END (not wall clock, so an
    # idle daemon still reports its last active week; not session START, so
    # a >7-day session's recent failures cannot vanish after a restart).
    windowed_completed = 0.0
    windowed_failed = 0.0
    bounded = [r for r in window_records if r[1] is not None]
    window_cutoff: float | None = None
    if bounded:
        anchor = max(r[1] for r in bounded)
        cutoff = anchor - G7_SUCCESS_WINDOW_DAYS * 86400.0
        window_cutoff = cutoff
        for start_ts, end_ts, completed, failed in bounded:
            if start_ts is None or start_ts >= end_ts:
                fraction = 1.0 if end_ts >= cutoff else 0.0
            else:
                overlap = min(end_ts, anchor) - max(start_ts, cutoff)
                fraction = max(0.0, min(1.0, overlap / (end_ts - start_ts)))
            windowed_completed += completed * fraction
            windowed_failed += failed * fraction
    # Windows with no parseable timestamps at all stay OUT of the windowed
    # rate (a corrupt record must never become permanent, un-ageable debt);
    # their counts still appear in the lifetime figures.

    windowed_tasks = windowed_completed + windowed_failed
    if used_daemon_metrics and windowed_tasks > 0:
        success_rate = windowed_completed / windowed_tasks
        sample_size = windowed_tasks
    elif used_daemon_metrics:
        # No recent task sample — surface the lifetime rate for context,
        # but the empty sample must not mint COMPLETE or an improve-rate
        # next-action (an idle-gap restart would otherwise resurrect the
        # autofill treadmill from historical debt).
        success_rate = lifetime_success_rate
        sample_size = 0.0
    else:
        # Fallback sources (heartbeat/session_state) carry no per-window
        # timestamps — keep the lifetime calculation there.
        success_rate = lifetime_success_rate
        sample_size = float(total_tasks)
    sample_ok = sample_size >= G7_MIN_WINDOW_TASKS
    windowed_rate = windowed_completed / windowed_tasks if windowed_tasks > 0 else None
    if not sample_ok and total_tasks > 0:
        # Too few recent tasks to speak for the trend — every displayed
        # surface (status ladder, progress, reason) uses the better-
        # supported lifetime rate; the thin windowed rate stays visible in
        # raw_data only. Otherwise one post-restart task would show 100%
        # (or 0%) progress to every dashboard consumer.
        success_rate = lifetime_success_rate

    # 6. Calculate progress (60% uptime weight, 40% success rate weight)
    uptime_progress = min(uptime_days / 7.0, 1.0)
    success_progress = min(success_rate / 0.9, 1.0)  # target is 90%
    progress = uptime_progress * 0.6 + success_progress * 0.4
    progress_percent = int(progress * 100)

    # 7. Determine status — daemon data required (heartbeat or daemon_metrics must exist)
    has_heartbeat = heartbeat_file.exists() or daemon_metrics_file.exists()
    if not has_heartbeat:
        status = GoalStatus.NOT_STARTED
        status_reason = "No daemon heartbeat data found"
    elif uptime_days >= 7 and success_rate >= 0.9 and sample_ok:
        status = GoalStatus.COMPLETE
        # The sample size belongs in the VERDICT, not just the progress line.
        # Every other branch here is self-describing -- ON_TRACK reports
        # "(N tasks)" -- but this one dropped the denominator at exactly the
        # moment it decides something. Seven days over 5 tasks and seven days
        # over 200 emitted an identical "Sustained autonomy achieved", so the
        # claim could not be read without going and counting PRs by hand.
        # Uptime accrues whether or not any work happens, so without the
        # denominator an idle week is indistinguishable from a productive one.
        # Same fix as queue_monitor._last_failure_note: one field, and the
        # figure describes its own basis.
        status_reason = (
            f"Sustained autonomy achieved: {uptime_days:.1f} days uptime, "
            f"{success_rate * 100:.0f}% recent success rate "
            f"over {sample_size:.0f} tasks"
        )
    elif uptime_days >= 3 and success_rate >= 0.8:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"Progressing: {uptime_days:.1f}/7 days, "
            f"{success_rate * 100:.0f}% success ({total_completed} tasks)"
        )
    elif uptime_days >= 1 or success_rate >= 0.5:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Early stage: {uptime_days:.1f} days uptime, "
            f"{success_rate * 100:.0f}% success rate"
        )
    else:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Minimal data: {uptime_days:.2f} days uptime, {total_tasks} tasks recorded"
        )

    next_actions = []
    if not has_heartbeat:
        next_actions.append("Start daemon with /daemon start")
    elif uptime_days < 7:
        # [INFRA-ONLY]: the 7-day window is closed by the daemon RUNNING, not
        # by any worker session. Without the marker autofill minted a task
        # from this line, the brief-quality gate refused it (no concrete
        # target), and silence_sentinel paged "Autofill quality gate held G7
        # for >24h" every day (2026-08-31 -> 09-04, 7 skip rows) for a goal
        # that was progressing exactly as designed. The marker makes autofill
        # skip G7 silently while the window runs -- including the "Improve
        # success rate" action below, which never produced a deliverable in
        # three attempts (task-20260802115715-2c5dca) and is not a task a
        # worker can do either. Same precedent as G1's nightly coverage job.
        next_actions.append(
            "[INFRA-ONLY] Continue daemon operation "
            f"({7 - uptime_days:.1f} days remaining)"
        )
    failure_examples: list[dict[str, str]] = []
    if total_tasks > 0 and success_rate < 0.9 and sample_ok:
        failure_examples = _recent_failure_examples(company_dir, window_cutoff)
        next_actions.append(
            f"Improve success rate from {success_rate * 100:.0f}% to 90%+"
        )
        if failure_examples:
            # Separate entry (not appended to the action above) so the
            # QUEUE-FILL task title — built from next_actions[0], truncated
            # to 60 chars by strategic_planner.py — stays clean; this
            # renders as a numbered "Additional Actions" line in the task
            # description instead, where a worker can actually read it.
            pointer = "; ".join(
                f"{e['task_id']} ({e['category']})" for e in failure_examples
            )
            next_actions.append(f"Recent window failures: {pointer}")

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=progress,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "uptime_days": uptime_days,
            "uptime_seconds": total_uptime_seconds,
            "tasks_completed": total_completed,
            "tasks_failed": total_failed,
            "success_rate": success_rate,
            "success_rate_lifetime": lifetime_success_rate,
            "success_rate_windowed": windowed_rate,
            "success_window_days": G7_SUCCESS_WINDOW_DAYS,
            "windowed_tasks_completed": round(windowed_completed, 2),
            "windowed_tasks_failed": round(windowed_failed, 2),
            "success_sample_size": round(sample_size, 2),
            "success_sample_ok": sample_ok,
            "uptime_progress": uptime_progress,
            "success_progress": success_progress,
            "recent_failure_examples": failure_examples,
        },
    )


# -----------------------------------------------------------------------------
# Assessor Registry
# -----------------------------------------------------------------------------


# G8 (2026-09-04). The scout is a cloud routine that appends entries to
# .planning/ROADMAP-SCOUT.md; the roadmap scan mints them as tasks and the
# scheduler stamps `{"roadmap_task_id": "SCOUT-..."}` into the task's notes.
# Counting COMPLETED tasks carrying that stamp measures the loop at its
# output — work that actually reached main — instead of at its input, where
# "10+ approved proposals" could be satisfied by the org writing about
# itself. One week, because the scout runs nightly and the goal is a rate.
SCOUT_ROADMAP_FILENAME = "ROADMAP-SCOUT.md"
SCOUT_THROUGHPUT_WINDOW_DAYS = 7
SCOUT_THROUGHPUT_TARGET = 2


def _notes_payloads(task: dict) -> list[dict]:
    """Every JSON object embedded in a task's ``notes``, whatever its shape.

    Three shapes are in the wild and all three are load-bearing:
      - ``[{"timestamp": ..., "content": "{...json...}"}]`` (update_task)
      - ``'{"roadmap_task_id": "P14-0.2"} | reset at 2026-01-02'`` (a bare
        string with the scheduler's JSON prefixed to human text)
      - a bare list of strings.
    ``raw_decode`` is what makes the second one work: it parses the leading
    JSON value and ignores the trailing prose, where ``json.loads`` raises
    and would silently drop the task from the count.
    """
    notes = task.get("notes")
    if isinstance(notes, str):
        raw = [notes]
    elif isinstance(notes, list):
        raw = [
            note.get("content", "") if isinstance(note, dict) else str(note)
            for note in notes
        ]
    else:
        return []

    decoder = json.JSONDecoder()
    payloads: list[dict] = []
    for text in raw:
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def _is_scout_task(task: dict) -> bool:
    """Did this task come in through the opportunity scout's roadmap?

    Two spellings, because the id prefix is the scout's convention and the
    file is the mechanism: a future intake that numbers entries differently
    still lands in ROADMAP-SCOUT.md, and a goal that only knew the prefix
    would report the loop dead while it was running.
    """
    for payload in _notes_payloads(task):
        roadmap_id = payload.get("roadmap_task_id")
        if isinstance(roadmap_id, str) and roadmap_id.startswith("SCOUT-"):
            return True
        roadmap_file = payload.get("roadmap_file")
        if isinstance(roadmap_file, str) and roadmap_file.rstrip("/").endswith(
            SCOUT_ROADMAP_FILENAME
        ):
            return True
    return False


def _open_scout_pr_number(project_root: Path) -> int | None:
    """The lowest open scout PR number, or None (including on any failure).

    The scout routine skips its nightly run while one of its own PRs is
    still open — backpressure, not silence (memory 2026-08-28: "silent"
    scout was a reviewed-PR queue, never a broken routine). Naming the PR
    is what turns "throughput is 0" into an action a human can take in one
    click. Any failure — gh missing, unauthenticated, offline, slow — is
    read as "no open PR", because a goal assessor must never raise into
    assess_all_goals and must never block a daemon cycle on the network.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                "head:scout/",
                "--json",
                "number",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            return None
        numbers = [
            pr["number"]
            for pr in prs
            if isinstance(pr, dict) and isinstance(pr.get("number"), int)
        ]
        return min(numbers) if numbers else None
    except Exception:
        return None


def assess_scout_throughput(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G8: scout entries delivered to main in the last 7 days."""
    project_root = company_dir.parent
    queue_file = company_dir / "state/work_queue.json"
    cutoff = time.time() - SCOUT_THROUGHPUT_WINDOW_DAYS * 86400

    delivered: list[dict] = []
    try:
        with open(queue_file) as f:
            queue = json.load(f)
        completed = queue.get("completed") if isinstance(queue, dict) else None
        for task in completed if isinstance(completed, list) else []:
            if not isinstance(task, dict) or not _is_scout_task(task):
                continue
            # completed_at is the authoritative stamp; updated_at is the
            # fallback for the older records that predate it (the live queue
            # carries completed tasks with updated_at: null and vice versa).
            ts = _parse_window_ts(task.get("completed_at")) or _parse_window_ts(
                task.get("updated_at")
            )
            if ts is None or ts < cutoff:
                continue
            delivered.append(
                {
                    "task_id": task.get("task_id"),
                    "title": str(task.get("title", ""))[:120],
                    "completed_at": task.get("completed_at") or task.get("updated_at"),
                }
            )
    except Exception:
        # A missing or malformed queue is "nothing measured", not a crash:
        # this runs inside assess_all_goals and an exception here kills the
        # whole strategic-planning batch.
        pass

    count = len(delivered)
    current_value = min(1.0, count / SCOUT_THROUGHPUT_TARGET)
    progress_percent = int(current_value * 100)

    if count >= SCOUT_THROUGHPUT_TARGET:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"{count} scout entries delivered in the last "
            f"{SCOUT_THROUGHPUT_WINDOW_DAYS} days (target: "
            f"{SCOUT_THROUGHPUT_TARGET})"
        )
    elif count == 1:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"1 scout entry delivered in the last "
            f"{SCOUT_THROUGHPUT_WINDOW_DAYS} days (target: "
            f"{SCOUT_THROUGHPUT_TARGET})"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = (
            f"No scout entries delivered in the last "
            f"{SCOUT_THROUGHPUT_WINDOW_DAYS} days"
        )

    # Both actions are [INFRA-ONLY] on purpose. The intake scan already mints
    # scout work from .planning/ROADMAP-SCOUT.md; letting strategic_planner's
    # queue-fill lane mint from this goal too would double-mint the same
    # entries under a second title, which is the treadmill the marker exists
    # to stop. G8 reports on that loop; it does not feed it.
    if status == GoalStatus.COMPLETE:
        next_actions: list[str] = []
    else:
        open_pr = _open_scout_pr_number(project_root)
        if open_pr is not None:
            next_actions = [
                f"[INFRA-ONLY] Review scout PR #{open_pr} — the scout skips "
                "its nightly run while a scout PR is open"
            ]
        else:
            next_actions = [
                "[INFRA-ONLY] The scout routine runs nightly; entries enter "
                "through .planning/ROADMAP-SCOUT.md"
            ]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "delivered_last_7d": count,
            "target_per_week": SCOUT_THROUGHPUT_TARGET,
            "window_days": SCOUT_THROUGHPUT_WINDOW_DAYS,
            "deliveries": delivered[:10],
        },
    )


# G17 (2026-09-04). GAP-7: self-tested code certifies its own bugs, so the
# acceptance ledger puts the oracle OUTSIDE the artifact — metamorphic
# relations executed by CI from origin/main. Increments 2-5 are intake
# entries in .planning/ROADMAP.md; the roadmap scheduler records the ones
# that shipped in .company/roadmap_state.json's tasks_completed.
#
# Increment 2 has two spellings. LEDGER-2 turned out too big for one worker
# run and was split into 2a/2b/2c (LEDGER-2 itself is blocked and will never
# complete), but a future intake that uses the single id must still count —
# so the increment is done when EITHER the single id OR the whole split set
# is in tasks_completed. An "any of these ids" rule would have been wrong:
# one of three parts is not the increment.
ACCEPTANCE_LEDGER_INCREMENTS: tuple[tuple[int, tuple[frozenset[str], ...]], ...] = (
    (2, (frozenset({"LEDGER-2"}), frozenset({"LEDGER-2a", "LEDGER-2b", "LEDGER-2c"}))),
    (3, (frozenset({"LEDGER-3"}),)),
    (4, (frozenset({"LEDGER-4"}),)),
    (5, (frozenset({"LEDGER-5"}),)),
)


DIVERGENCE_NOTE_MAX_IDS = 5
DIVERGENCE_NOTE_ID_MAX_CHARS = 80


def _open_required_divergence_ids(company_dir: Path) -> list[str] | None:
    """Ids of the open divergences on REQUIRED ledger entries, oldest first.

    None when the register cannot be read: "no register" is not "no
    divergences", so the caller says nothing rather than reporting zero. Never
    raises — a note about the ledger must not take the goal assessment down.
    """
    try:
        try:
            from . import acceptance_divergences as divergences
        except ImportError:
            import acceptance_divergences as divergences  # type: ignore[no-redef]

        path = company_dir / "state" / "acceptance" / "divergences.json"
        register = divergences.read_register(path)
        if register is None:
            return None
        required = divergences.ledger.TIER_REQUIRED
        return [
            "".join(c if c.isprintable() else "?" for c in str(r["id"]))[
                :DIVERGENCE_NOTE_ID_MAX_CHARS
            ]
            for r in divergences.open_records(register)
            if r.get("tier") == required
        ]
    except Exception:
        return None


def assess_acceptance_ledger(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G17: increments 2-5 of the acceptance ledger shipped.

    Also names any open divergence on a required ledger entry. That note is
    display only: it is appended to ``status_reason`` and recorded in
    ``raw_data``, and never moves ``status``, ``progress_percent``,
    ``current_value``, ``blockers`` or ``next_actions``. The repair work comes
    from backlog_generator's ``acceptance_divergence`` signal; a note that also
    minted through ``next_actions`` would queue every repair twice.
    """
    # roadmap_scheduler.get_state_path() writes company_dir/roadmap_state.json
    # — NOT company_dir/state/roadmap_state.json. company_paths lists the
    # state/ spelling, and a 148-byte stub from March still sits there; the
    # live file (updated every scan) is the one beside org.json.
    state_file = company_dir / "roadmap_state.json"
    state_exists = state_file.exists()
    completed_ids: set[str] = set()
    if state_exists:
        try:
            with open(state_file) as f:
                state = json.load(f)
            raw = state.get("tasks_completed") if isinstance(state, dict) else None
            completed_ids = {t for t in raw or [] if isinstance(t, str)}
        except Exception:
            completed_ids = set()

    per_increment = {
        f"increment_{number}": any(spelling <= completed_ids for spelling in spellings)
        for number, spellings in ACCEPTANCE_LEDGER_INCREMENTS
    }
    done = sum(1 for shipped in per_increment.values() if shipped)
    total = len(ACCEPTANCE_LEDGER_INCREMENTS)
    current_value = done / total
    progress_percent = int(current_value * 100)

    if done >= total:
        status = GoalStatus.COMPLETE
        status_reason = f"All {total} acceptance-ledger increments shipped"
    elif done >= 2:
        status = GoalStatus.ON_TRACK
        status_reason = f"{done}/{total} acceptance-ledger increments shipped"
    elif done == 0 and not state_exists:
        status = GoalStatus.NOT_STARTED
        status_reason = (
            "No roadmap_state.json — the roadmap scan has not run in this company"
        )
    else:
        status = GoalStatus.AT_RISK
        status_reason = f"{done}/{total} acceptance-ledger increments shipped"

    divergence_ids = _open_required_divergence_ids(company_dir)
    divergence_data: dict[str, Any] = {
        "divergence_register_present": divergence_ids is not None,
    }
    if divergence_ids is not None:
        divergence_data["open_required_divergences"] = len(divergence_ids)
        divergence_data["open_required_divergence_ids"] = divergence_ids[
            :DIVERGENCE_NOTE_MAX_IDS
        ]
    if divergence_ids:
        shown = divergence_ids[:DIVERGENCE_NOTE_MAX_IDS]
        more = len(divergence_ids) - len(shown)
        status_reason += (
            f"; {len(divergence_ids)} open required divergence(s): "
            + ", ".join(shown)
            + (f" (+{more} more)" if more else "")
            + " (note only)"
        )

    next_actions: list[str] = []
    for number, _spellings in ACCEPTANCE_LEDGER_INCREMENTS:
        if not per_increment[f"increment_{number}"]:
            # [INFRA-ONLY] for the same reason as G8: these increments are
            # already queued as roadmap entries and the roadmap scan mints
            # them. A queue-fill mint here would be the same work twice.
            next_actions = [
                f"[INFRA-ONLY] Increment {number} is queued in "
                ".planning/ROADMAP.md; the roadmap scan mints it"
            ]
            break

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "increments_done": done,
            "increments_total": total,
            "state_file_present": state_exists,
            **per_increment,
            **divergence_data,
        },
    )


# G18 (2026-09-04). The 2026-08-31 hook audit found 15 safety controls that
# had never fired once, and no blocking hook logged its denials. Two
# properties make a control believable, and neither is "it has unit tests":
#   (a) a test that RUNS the hook as a subprocess with a payload that must
#       block and asserts the exit code the harness actually acts on (2);
#   (b) a denial record, so a block leaves evidence a human can audit.
# A hook satisfying both is one whose firing has been observed end to end.
BLOCKING_HOOKS: tuple[str, ...] = (
    "block_dangerous",
    "git_guardian",
    "secrets_scanner",
    "lint_on_edit",
    "network_egress_guard",
    "slopsquat_check",
)

# The exit code Claude Code reads as "deny". A test asserting 0/1 is
# asserting the hook ran, not that it blocked.
_HOOK_BLOCK_ASSERTIONS = ("returncode == 2", "exit_code == 2", "returncode, 2")


def _hook_fires_tests(project_root: Path) -> dict[str, str]:
    """Hook name -> the repo-relative test file that proves it blocks.

    A file qualifies when it names the hook script, spawns a subprocess, and
    asserts the deny exit code. Reading the whole tests/ tree once and
    checking all six hooks per file keeps this one pass over the directory,
    not six.
    """
    found: dict[str, str] = {}
    tests_dir = project_root / "tests"
    if not tests_dir.is_dir():
        return found
    try:
        candidates = sorted(tests_dir.rglob("*.py"))
    except OSError:
        return found
    for path in candidates:
        if len(found) == len(BLOCKING_HOOKS):
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "subprocess" not in text:
            continue
        if not any(assertion in text for assertion in _HOOK_BLOCK_ASSERTIONS):
            continue
        for hook in BLOCKING_HOOKS:
            if hook in found:
                continue
            if f"{hook}.py" in text:
                try:
                    found[hook] = str(path.relative_to(project_root))
                except ValueError:
                    found[hook] = path.name
    return found


def _hook_records_denials(project_root: Path) -> dict[str, bool]:
    """Hook name -> whether its source calls record_decision()."""
    records: dict[str, bool] = {}
    for hook in BLOCKING_HOOKS:
        source = project_root / ".claude" / "hooks" / f"{hook}.py"
        try:
            records[hook] = "record_decision(" in source.read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            records[hook] = False
    return records


def assess_controls_that_fire(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G18: every blocking hook has a fires-test and a denial record."""
    # Every assessor in this module derives the project root the same way:
    # the company directory is .company/ inside the checkout, so its parent
    # IS the repo root. (assess_test_coverage, assess_tutorials, ... all do
    # this; a divergent rule here would measure a different tree.)
    project_root = company_dir.parent

    fires = _hook_fires_tests(project_root)
    records = _hook_records_denials(project_root)

    per_hook = {
        hook: {
            "fires_test": fires.get(hook),
            "records_denial": records.get(hook, False),
            "proven": bool(fires.get(hook)) and records.get(hook, False),
        }
        for hook in BLOCKING_HOOKS
    }
    proven = sum(1 for h in per_hook.values() if h["proven"])
    total = len(BLOCKING_HOOKS)
    current_value = proven / total
    progress_percent = int(current_value * 100)

    if proven >= total:
        status = GoalStatus.COMPLETE
        status_reason = f"All {total} blocking hooks prove they fire"
    elif proven >= 4:
        status = GoalStatus.ON_TRACK
        status_reason = f"{proven}/{total} blocking hooks prove they fire"
    else:
        status = GoalStatus.AT_RISK
        status_reason = f"{proven}/{total} blocking hooks prove they fire"

    # Deliberately NOT [INFRA-ONLY]: unlike G8 and G17 there is no other lane
    # minting this work, and the deliverable is a concrete new file a worker
    # session can write in one PR. The action names that file and the exact
    # assertions, so the brief-quality gate sees a target and the deliverable
    # judge has something to check the diff against.
    next_actions: list[str] = []
    missing_test = next((h for h in BLOCKING_HOOKS if not fires.get(h)), None)
    if missing_test:
        next_actions.append(
            f"tests/test_{missing_test}_fires.py: run "
            f".claude/hooks/{missing_test}.py as a subprocess with a payload "
            "that must block, assert exit code 2 and one record in "
            "logs/gates.jsonl"
        )
    missing_record = next((h for h in BLOCKING_HOOKS if not records.get(h)), None)
    if missing_record:
        next_actions.append(
            f"Add record_decision() on the block path of "
            f".claude/hooks/{missing_record}.py"
        )

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "hooks_proven": proven,
            "hooks_total": total,
            "hooks": per_hook,
        },
    )


# G19 (2026-11-03). Launch readiness is measured by stranger-test reports: a
# fresh reader tries the public export and files what stopped them. The goal
# is met when the newest report has no blockers and at most two confusing
# findings. The assessor only reads the reports; it never runs the test.
_STRANGER_TEST_GLOB = "stranger-test-*.md"
_STRANGER_TEST_MAX_CONFUSING = 2
# `Totals: **3 blockers, 9 confusing, 5 cosmetic.**` (bold markers optional).
_STRANGER_TOTALS_RE = re.compile(
    r"Totals:\s*\**\s*(\d+)\s+blockers?\s*,\s*(\d+)\s+confusing", re.IGNORECASE
)
_STRANGER_DATE_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def _stranger_report_date(path: Path) -> str:
    """Sortable YYYYMMDD from the filename; '' when the name carries no date."""
    matches = _STRANGER_DATE_RE.findall(path.stem)
    if not matches:
        return ""
    return "".join(matches[-1])


def _order_stranger_reports(paths: list[Path]) -> list[Path]:
    """Oldest first: by filename date, then a report that a same-date report
    names (each pass links the one it follows up) before the one naming it,
    then by name."""

    def _text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    texts = {p: _text(p) for p in paths}

    def _superseded(path: Path) -> bool:
        date = _stranger_report_date(path)
        return any(
            other != path
            and _stranger_report_date(other) == date
            and path.name in texts[other]
            for other in paths
        )

    return sorted(
        paths, key=lambda p: (_stranger_report_date(p), not _superseded(p), p.name)
    )


def _parse_stranger_totals(path: Path) -> tuple[int, int] | None:
    """(blockers, confusing) from a report's totals line, or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _STRANGER_TOTALS_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def assess_launch_readiness(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G19: the newest stranger test has 0 blockers, <= 2 confusing."""
    project_root = company_dir.parent
    docs_dir = project_root / "docs"

    def _result(
        status: GoalStatus,
        reason: str,
        current: float,
        next_actions: list[str],
        raw: dict[str, Any],
    ) -> GoalAssessment:
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=current,
            target_value=1.0,
            progress_percent=int(current * 100),
            status=status,
            status_reason=reason,
            next_actions=next_actions,
            raw_data={"assessor": "launch_readiness", **raw},
        )

    try:
        reports = _order_stranger_reports(
            [p for p in docs_dir.glob(_STRANGER_TEST_GLOB) if p.is_file()]
        )
    except OSError:
        reports = []

    if not reports:
        return _result(
            GoalStatus.NOT_STARTED,
            f"No stranger-test report found (docs/{_STRANGER_TEST_GLOB}).",
            0.0,
            [
                "Run a stranger test of the public export and write docs/stranger-test-<date>.md"
            ],
            {"report": None, "reports_found": 0},
        )

    newest = reports[-1]
    rel = f"docs/{newest.name}"
    totals = _parse_stranger_totals(newest)
    if totals is None:
        return _result(
            GoalStatus.NOT_STARTED,
            f"No parseable totals line in {rel} "
            "(expected 'Totals: **N blockers, M confusing, K cosmetic.**').",
            0.0,
            [f"Add a Totals line to {rel}"],
            {"report": rel, "reports_found": len(reports)},
        )

    blockers, confusing = totals
    # Baseline = the oldest report that parses; progress is how much of its
    # blocker+confusing count has gone since.
    baseline = totals
    for older in reports:
        parsed = _parse_stranger_totals(older)
        if parsed is not None:
            baseline = parsed
            break
    first_open = baseline[0] + baseline[1]
    open_now = blockers + confusing

    raw = {
        "report": rel,
        "reports_found": len(reports),
        "blockers": blockers,
        "confusing": confusing,
        "baseline_blockers": baseline[0],
        "baseline_confusing": baseline[1],
    }
    if blockers == 0 and confusing <= _STRANGER_TEST_MAX_CONFUSING:
        return _result(
            GoalStatus.COMPLETE,
            f"{rel}: {blockers} blockers, {confusing} confusing "
            f"(target 0 blockers, at most {_STRANGER_TEST_MAX_CONFUSING} confusing)",
            1.0,
            [],
            raw,
        )

    if first_open > 0:
        current = min(1.0, max(0.0, (first_open - open_now) / first_open))
    else:
        current = 0.0
    return _result(
        GoalStatus.ON_TRACK,
        f"{rel}: {blockers} blockers, {confusing} confusing "
        f"(target 0 blockers, at most {_STRANGER_TEST_MAX_CONFUSING} confusing)",
        current,
        [
            f"Fix the blockers and confusing findings listed in {rel}, then re-run the stranger test"
        ],
        raw,
    )


def assess_employee_initiative(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G10: Employee Initiative — 50% tasks from employees."""
    idea_count = 0
    initiative_task_count = 0
    total_completed = 0

    # Count non-rejected ideas from employee_ideas.json
    ideas_file = company_dir / "employee_ideas.json"
    if ideas_file.exists():
        try:
            with open(ideas_file) as f:
                ideas_data = json.load(f)
            ideas_list = (
                ideas_data
                if isinstance(ideas_data, list)
                else ideas_data.get("ideas", [])
            )
            for idea in ideas_list:
                if idea.get("status") != "rejected":
                    idea_count += 1
        except Exception:
            pass

    # Build set of valid employee IDs for proposed_by validation
    valid_employee_ids: set[str] = set()
    org_file = company_dir / "org.json"
    if org_file.exists():
        try:
            with open(org_file) as f:
                org_data = json.load(f)
            for emp in org_data.get("employees", []):
                eid = emp.get("id", "")
                if eid:
                    valid_employee_ids.add(eid)
        except Exception:
            pass

    # Count completed tasks from work_queue.json
    created_by_counts: dict[str, int] = {}
    queue_file = company_dir / "state/work_queue.json"
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                queue_data = json.load(f)
            completed = queue_data.get("completed", [])
            total_completed = len(completed)

            # Count initiative tasks: source="employee_initiative" (Layer 3: batch ideation)
            # OR proposed_by a valid employee (Layers 1-2: prompt injection / post-completion)
            for task in completed:
                is_initiative = task.get("source") == "employee_initiative"
                proposed_by = task.get("proposed_by", "")
                is_employee_proposal = proposed_by in valid_employee_ids
                if is_initiative or is_employee_proposal:
                    initiative_task_count += 1
                    creator = task.get("created_by") or proposed_by
                    if creator:
                        created_by_counts[creator] = (
                            created_by_counts.get(creator, 0) + 1
                        )
        except Exception:
            pass

    # Use initiative task count (from completed employee_initiative tasks) as numerator
    # This represents tasks completed from the idea_scanner pipeline
    ratio = initiative_task_count / max(total_completed, 1)
    target_ratio = 0.5
    current_value = min(ratio / target_ratio, 1.0)
    progress_percent = int(current_value * 100)

    if ratio >= target_ratio:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"{initiative_task_count} completed initiative tasks vs {total_completed} total completed "
            f"({ratio:.0%} ratio, target: {target_ratio:.0%})"
        )
    elif ratio >= 0.25:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"{ratio:.0%} initiative ratio, approaching {target_ratio:.0%} target"
        )
    elif initiative_task_count > 0:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Only {initiative_task_count} initiative tasks ({ratio:.0%} ratio)"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No employee initiative tasks completed"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Encourage employee initiative task completion"]
        if status != GoalStatus.COMPLETE
        else [],
        raw_data={
            "idea_count": idea_count,
            "initiative_task_count": initiative_task_count,
            "total_completed": total_completed,
            "ratio": ratio,
            "target_ratio": target_ratio,
            "created_by": created_by_counts,
        },
    )


def assess_queue_health(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G11: Queue Health — queue never empty > 1 hour."""
    has_pending = False
    has_recent_activity = False
    has_throughput = False

    # Check pending tasks in work_queue.json
    queue_file = company_dir / "state/work_queue.json"
    in_progress = []
    pending = []
    completed = []
    # Read in_progress tasks (read-only — no heal_queue side effects during assessment)
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                queue_data = json.load(f)
            pending = queue_data.get("pending", [])
            in_progress = queue_data.get("in_progress", [])
            completed = queue_data.get("completed", [])
            has_pending = len(pending) > 0 or len(in_progress) > 0
        except Exception:
            pass

    # Check daemon heartbeat for recent activity, throughput, and queue metrics.
    # Check runtime/ first (default config path), fall back to company_dir root.
    hb: dict = {}
    for hb_path in (
        company_dir / "runtime/daemon.heartbeat",
        company_dir / "daemon.heartbeat",
    ):
        if hb_path.exists():
            try:
                with open(hb_path) as f:
                    hb = json.load(f)
                break
            except Exception:
                pass

    uptime = 0
    throughput_grace = False
    if hb:
        hb_time = datetime.fromisoformat(
            hb.get("last_heartbeat", "2000-01-01T00:00:00+00:00")
        )
        age = (datetime.now(timezone.utc) - hb_time).total_seconds()
        has_recent_activity = age < 3600  # within 1 hour
        uptime = hb.get("uptime_seconds", 0)
        tasks_completed = hb.get("tasks_completed_this_session", 0)
        throughput_grace = tasks_completed == 0 and uptime < 600
        # G11 fix: also pass throughput if tasks are actively in_progress
        has_throughput = tasks_completed > 0 or uptime < 600

    # Fallback: if heartbeat shows no throughput but queue has recent in_progress work
    if not has_throughput and len(in_progress) > 0:
        # Only count as healthy if at least one task was claimed recently (within 2h)
        for ip_task in in_progress:
            claimed_at = ip_task.get("claimed_at") or ip_task.get("started_at", "")
            if claimed_at:
                try:
                    claimed_dt = datetime.fromisoformat(
                        claimed_at.replace("Z", "+00:00")
                    )
                    if (datetime.now(timezone.utc) - claimed_dt).total_seconds() < 7200:
                        has_throughput = True
                        break
                except (ValueError, TypeError):
                    pass

    # Fallback: check queue completed list for recent completions
    if not has_throughput and completed:
        for c_task in completed[-20:]:  # Check last 20 completed
            completed_at = c_task.get("completed_at") or c_task.get("updated_at", "")
            if completed_at:
                try:
                    c_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - c_dt).total_seconds() < 7200:
                        has_throughput = True
                        break
                except (ValueError, TypeError):
                    pass

    # Enrich queue metrics from heartbeat when available (G11 instrumentation).
    queue_depth: int = hb.get("queue_depth", -1)
    queue_blocked: int = hb.get("queue_blocked", 0)
    blocked_ratio: float = hb.get("queue_blocked_ratio", 0.0)
    throughput_per_hour: float = hb.get("throughput_per_hour", 0.0)

    # Weighted scoring: pending=0.4, recent_activity=0.3, throughput=0.3
    current_value = 0.0
    if has_pending:
        current_value += 0.4
    if has_recent_activity:
        current_value += 0.3
    if has_throughput:
        current_value += 0.3

    criteria_met = sum([has_pending, has_recent_activity, has_throughput])
    progress_percent = int(current_value * 100)

    if criteria_met == 3:
        status = GoalStatus.COMPLETE
        status_reason = "Queue healthy: pending tasks, recent activity, and throughput"
    elif criteria_met == 2:
        status = GoalStatus.ON_TRACK
        missing = []
        if not has_pending:
            missing.append("no pending tasks")
        if not has_recent_activity:
            missing.append("no recent activity")
        if not has_throughput:
            missing.append("no throughput")
        status_reason = (
            f"Queue partially healthy ({criteria_met}/3): missing {', '.join(missing)}"
        )
    elif criteria_met == 1:
        status = GoalStatus.AT_RISK
        status_reason = f"Queue unhealthy: only {criteria_met}/3 health criteria met"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No queue health signals detected"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Start daemon and populate work queue"]
        if status != GoalStatus.COMPLETE
        else [],
        raw_data={
            "has_pending": has_pending,
            "has_recent_activity": has_recent_activity,
            "has_throughput": has_throughput,
            "criteria_met": criteria_met,
            "pending_count": len(pending),
            "in_progress_count": len(in_progress),
            "uptime_seconds": uptime,
            "throughput_grace": throughput_grace,
            "queue_depth": queue_depth,
            "queue_blocked": queue_blocked,
            "blocked_ratio": blocked_ratio,
            "throughput_per_hour": throughput_per_hour,
        },
    )


# -----------------------------------------------------------------------------
# G12–G16 Assessors (Sprint 2)
# -----------------------------------------------------------------------------


def assess_parallel_throughput(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G12: Parallel Throughput — 2+ workers active simultaneously for 24h."""
    # WS-057-004: Check daemon worktree dir, not manual .worktrees/
    # Per-project base so the metric counts THIS project's workers only.
    try:
        from company_resolver import get_worktree_base

        daemon_wt_dir = get_worktree_base()
    except Exception:
        daemon_wt_dir = Path("/tmp/forge-worktrees")
    worktree_count = 0
    if (
        daemon_wt_dir.exists()
        and not daemon_wt_dir.is_symlink()
        and daemon_wt_dir.is_dir()
    ):
        worktree_count = len([d for d in daemon_wt_dir.iterdir() if d.is_dir()])
    if worktree_count == 0:
        manual_wt_dir = company_dir.parent / ".worktrees"
        if manual_wt_dir.exists():
            worktree_count = len([d for d in manual_wt_dir.iterdir() if d.is_dir()])

    # Read heartbeat for current worker status
    max_concurrent = 0
    hb: dict = {}
    for hb_path in (
        company_dir / "runtime/daemon.heartbeat",
        company_dir / "daemon.heartbeat",
    ):
        if hb_path.exists():
            try:
                with open(hb_path) as f:
                    hb = json.load(f)
                break
            except Exception:
                pass

    if hb:
        # WS-057-004: Prefer active_workers (actual threads) over queue_in_progress
        max_concurrent = max(
            1, hb.get("active_workers", hb.get("queue_in_progress", 0))
        )

    # Check session state for sustained uptime
    sustained_hours = 0.0
    session_file = company_dir / "state/session_state.json"
    if session_file.exists():
        try:
            with open(session_file) as f:
                sd = json.load(f)
            sustained_hours = sd.get("total_uptime_seconds", 0) / 3600.0
        except Exception:
            pass

    # Scoring: infra 30%, parallel 30%, duration 40%
    infra_score = 1.0 if worktree_count >= 1 else 0.0
    parallel_score = min(max_concurrent / 2.0, 1.0)
    duration_score = min(sustained_hours / 24.0, 1.0)
    current_value = infra_score * 0.3 + parallel_score * 0.3 + duration_score * 0.4
    progress_percent = int(current_value * 100)

    if max_concurrent >= 2 and sustained_hours >= 24.0 and worktree_count >= 1:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"Parallel throughput achieved: {max_concurrent} concurrent workers, "
            f"{sustained_hours:.1f}h sustained"
        )
    elif max_concurrent >= 2 and worktree_count >= 1:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"{max_concurrent} workers active, need "
            f"{max(0, 24 - sustained_hours):.1f}h more for 24h target"
        )
    elif worktree_count >= 1:
        status = GoalStatus.AT_RISK
        status_reason = (
            "Worktrees available but parallel execution not yet demonstrated"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Worktree isolation not yet set up"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[] if worktree_count >= 1 else ["Worktree isolation required"],
        next_actions=(
            ["Enable parallel workers via daemon --workers 2"]
            if max_concurrent < 2
            else []
        ),
        raw_data={
            "worktree_count": worktree_count,
            "max_concurrent_workers": max_concurrent,
            "sustained_uptime_hours": sustained_hours,
        },
    )


def assess_ci_green(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G16: CI Green — 0 failures on 3 consecutive PRs."""
    project_root = company_dir.parent

    # Query last 3 merged PRs via gh
    pr_success_count = 0
    pr_total = 0
    query_ok = False
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "3",
                "--json",
                "number,statusCheckRollup",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            prs = json.loads(result.stdout)
            if not isinstance(prs, list):
                prs = []
            pr_total = len(prs)
            for pr in prs:
                rollup = pr.get("statusCheckRollup", [])
                if rollup and all(c.get("conclusion") == "SUCCESS" for c in rollup):
                    pr_success_count += 1
            query_ok = True
    except Exception:
        pass

    # Check CI config exists
    has_ci = (project_root / ".github" / "workflows" / "ci.yml").exists()

    if not query_ok:
        # A `gh` failure (auth, network, rate limit, non-repo cwd in a worker
        # worktree) must never be scored the same as confirmed CI failures --
        # doing so previously reported "CI configured but recent PRs have
        # failures" from zero evidence, which minted an endless stream of
        # bogus [QUEUE-FILL] G16 tasks ("fix flaky tests") that no worker
        # session could act on, since there was nothing real to fix. Live
        # gh query confirmed the last 3 merged PRs were all fully green while
        # this was silently reporting 30%/at_risk. See goal_tracker.py's G1
        # [INFRA-ONLY] precedent for the same class of fix.
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=0.3 if has_ci else 0.0,
            target_value=1.0,
            progress_percent=30 if has_ci else 0,
            status=GoalStatus.AT_RISK if has_ci else GoalStatus.NOT_STARTED,
            status_reason=(
                "Unable to query recent PR CI status via gh -- cannot verify "
                "green or red; not a confirmed failure"
                if has_ci
                else "CI not yet configured"
            ),
            blockers=[],
            next_actions=(
                [
                    "[INFRA-ONLY] gh query for recent PR CI status failed "
                    "(auth/network/rate-limit) -- no worker action possible; "
                    "re-assess once gh can reach GitHub"
                ]
                if has_ci
                else []
            ),
            raw_data={
                "pr_success_count": 0,
                "pr_total": 0,
                "has_ci_config": has_ci,
                "gh_query_ok": False,
            },
        )

    # Scoring: PR success 70%, CI config 30%
    pr_score = (pr_success_count / 3.0) if pr_total else 0.0
    current_value = pr_score * 0.7 + (1.0 if has_ci else 0.0) * 0.3
    progress_percent = int(current_value * 100)

    if pr_success_count >= 3:
        status = GoalStatus.COMPLETE
        status_reason = "CI Green: 3 consecutive PRs with all checks passing"
    elif pr_success_count >= 2:
        status = GoalStatus.ON_TRACK
        status_reason = f"{pr_success_count}/3 PRs passing all checks"
    elif has_ci and pr_success_count >= 1:
        status = GoalStatus.AT_RISK
        status_reason = f"Only {pr_success_count}/{pr_total} recent PRs fully green"
    elif has_ci:
        status = GoalStatus.AT_RISK
        status_reason = "CI configured but recent PRs have failures"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "CI not yet configured"

    # Generate concrete next_actions naming specific failed tests instead of
    # bare goal metric ("0 failures"). This prevents phantom tasks where the
    # diff fixes assessor bugs but the task title claims "CI Green: 3 consecutive PRs".
    next_actions: list[str] = []
    if status != GoalStatus.COMPLETE:
        failing_tests = _find_failing_ci_tests(project_root)
        if failing_tests:
            tests_str = ", ".join(failing_tests)
            next_actions = [f"Fix failing CI checks: {tests_str}"]
        else:
            next_actions = ["Stabilize flaky tests to achieve 3 consecutive green PRs"]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[],
        next_actions=next_actions,
        raw_data={
            "pr_success_count": pr_success_count,
            "pr_total": pr_total,
            "has_ci_config": has_ci,
            "gh_query_ok": True,
        },
    )


_METRIC_ARTIFACT_RE = re.compile(r"`([^`]+)`|(/[a-zA-Z][\w-]*)")


def _verified_stamp_for_goal(goal: GoalDefinition, company_dir: Path) -> dict | None:
    """Return this goal's verification-state record iff it is a live, matching stamp.

    A stamp only counts if its recorded goal_name + success_metric still match the
    goal's CURRENT text — if vision.md redefines the goal after verification, the
    stale stamp must not silently keep reporting the (now different) goal complete.
    Returns None on any missing/corrupt/mismatched/unverified state (never raises —
    ``load_goal_verification_state`` already fails closed to {} on read errors).
    """
    record = load_goal_verification_state(company_dir).get(goal.id)
    if not isinstance(record, dict) or record.get("status") != "verified":
        return None
    if _normalize_fingerprint_text(
        record.get("goal_name", "")
    ) != _normalize_fingerprint_text(goal.name):
        return None
    if _normalize_fingerprint_text(
        record.get("success_metric", "")
    ) != _normalize_fingerprint_text(goal.success_metric):
        return None
    return record


# A metric in the CANONICAL ledger-metric sentence form below is a rare
# exception to "never auto-derive": the acceptance ledger is an oracle OUTSIDE
# the artifact (GAP-7), so a re-check of it is as trustworthy as a dogfood
# assessor, not a text-only guess.
#
# The canonical form -- the WHOLE metric must be this, nothing else:
#
#     [The acceptance entry|entries] <ids> [all] read(s) CONSISTENT in the
#     ledger run on main [, and <ids> [all] stay(s) CONSISTENT] [.]
#
# where <ids> is one or more acceptance entry ids joined by "," / ", and" /
# "and", and an id is a complete token of the acceptance id shape: uppercase
# letter first, uppercase letters/digits, at least one "-"-joined segment
# (V01-HOST-CUSTOMER-FLOW, R7-ETA-HOST-OFFLINE). Clauses may repeat, joined by
# ", and"; at least one must be a "read(s) CONSISTENT in the ledger run on
# main" clause. Every id in every clause (including "stay CONSISTENT" ones)
# must pass for the goal to be met.
#
# Anything else is NOT the shape and falls through to the existing paths:
# "X is not CONSISTENT in the ledger run on main" (negation), "X reads
# INCONSISTENT ...", a lowercase or truncated id ("V01-HOST-x"), trailing
# prose, another branch. Matching the whole sentence -- not searching for
# anchors anywhere in it -- is what keeps a negated or hedged mention from
# ever crediting a goal.
_LEDGER_ENTRY_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
_LEDGER_ID_SEPARATOR = r"(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+)"
_LEDGER_ID_LIST = rf"{_LEDGER_ENTRY_ID}(?:{_LEDGER_ID_SEPARATOR}{_LEDGER_ENTRY_ID})*"
_LEDGER_CLAUSE = (
    rf"(?P<ids>{_LEDGER_ID_LIST})[ \t]+(?:all[ \t]+)?"
    r"(?:(?P<read>reads?)[ \t]+CONSISTENT[ \t]+in[ \t]+the[ \t]+ledger[ \t]+run"
    r"[ \t]+on[ \t]+main|stays?[ \t]+CONSISTENT)"
)
_LEDGER_CLAUSE_RE = re.compile(_LEDGER_CLAUSE)
_LEDGER_METRIC_RE = re.compile(
    r"(?:(?i:the)[ \t]+acceptance[ \t]+entr(?:y|ies)[ \t]+)?"
    + _LEDGER_CLAUSE.replace("?P<ids>", "?:").replace("?P<read>", "?:")
    + r"(?:[ \t]*,?[ \t]+and[ \t]+"
    + _LEDGER_CLAUSE.replace("?P<ids>", "?:").replace("?P<read>", "?:")
    + r")*[ \t]*\.?"
)
# Only ever applied to an ids group the full-sentence match already accepted,
# so every hit is a complete id token.
_LEDGER_ENTRY_ID_RE = re.compile(_LEDGER_ENTRY_ID)
_LEDGER_PREFIX_RE = re.compile(r"(?i:the)[ \t]+acceptance[ \t]+entr(?:y|ies)[ \t]+")


def _ledger_consistent_entry_ids(success_metric: str) -> list[str] | None:
    """Entry ids of a metric in the canonical ledger-metric form, else None.

    See the comment above for the form. None means "not this shape" -- callers
    must then leave every existing path untouched. The ids are returned in
    order, de-duplicated, from every clause ("read CONSISTENT" and "stay
    CONSISTENT" alike): all of them must pass for the goal to be met.
    """
    text = " ".join((success_metric or "").split())
    if not text or not _LEDGER_METRIC_RE.fullmatch(text):
        return None
    body = _LEDGER_PREFIX_RE.sub("", text, count=1)
    ids: list[str] = []
    saw_read_clause = False
    for clause in _LEDGER_CLAUSE_RE.finditer(body):
        saw_read_clause = saw_read_clause or clause.group("read") is not None
        for entry_id in _LEDGER_ENTRY_ID_RE.findall(clause.group("ids")):
            if entry_id not in ids:
                ids.append(entry_id)
    if not saw_read_clause or not ids:
        return None
    return ids


def _ledger_named_entry_ids(success_metric: str) -> list[str]:
    """Entry ids a metric names in a ledger clause, canonical form or not.

    ``_ledger_consistent_entry_ids`` accepts only a metric that is NOTHING but
    ledger clauses (it may credit on them alone). A metric with an extra
    clause ("..., and both relays are redeployed") is not that, but the
    entries it names in "<ids> read(s) CONSISTENT in the ledger run on main"
    / "<ids> stay(s) CONSISTENT" clauses still must read CONSISTENT: this
    returns those ids (in order, de-duplicated) so a caller that is about to
    let a judge decide the rest can first check the ledger never contradicts
    it. A negated or hedged mention ("X is not CONSISTENT") does not match the
    clause shape and is not returned. Never used to credit.
    """
    text = " ".join((success_metric or "").split())
    ids: list[str] = []
    for clause in _LEDGER_CLAUSE_RE.finditer(text):
        for entry_id in _LEDGER_ENTRY_ID_RE.findall(clause.group("ids")):
            if entry_id not in ids:
                ids.append(entry_id)
    return ids


def _default_branch_commit(project_root: Path, branch: str) -> str | None:
    """Best-effort commit sha at the tip of ``branch``, local ref first.

    Deliberately not ``HEAD`` — a worker's worktree or a CI checkout can be
    detached on a task branch while ``branch`` itself sits elsewhere, and this
    must compare against that branch's own tip regardless of what happens to
    be checked out. Falls back to ``origin/<branch>`` for a checkout whose
    local branch ref is absent. None on any git failure.
    """
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project_root),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{ref}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


class LedgerCheck(NamedTuple):
    """Result of :func:`_ledger_run_confirms_metric`.

    ``state`` is one of:

    * ``"confirmed"`` -- the last run is on main's current tip and every id passed;
    * ``"failing"`` -- the last run is on main's current tip and some id did not
      pass (a real negative for that commit);
    * ``"no_run"`` / ``"not_main"`` / ``"stale"`` / ``"unavailable"`` -- no run
      has looked at main's current tip yet (none recorded, the last one was on
      another branch, main has moved since, or the record/ledger could not be
      read). These are "not yet", never a negative: a later run can confirm.
    * ``"environment"`` -- the last run is on main's current tip but the ledger
      flagged it ``environment_error`` / ``environment_suspect``
      (``acceptance_ledger.environment_flag``) and some id did not pass: the
      failure is the environment's, so it is "not yet" too, never "failing".
    """

    confirmed: bool
    reason: str
    state: str
    run_commit: str | None = None
    entry_ids: tuple[str, ...] = ()
    # _ledger_credit_check only: commits of the eligible runs (newest first)
    # that read some named entry not CONSISTENT.
    failing_runs: tuple[str, ...] = ()


# States that mean "no verdict yet": the crediting sweep re-checks them with
# back-off and never ledgers them as final. "environment" comes from
# _ledger_check: the last run was flagged environment_error /
# environment_suspect. The last three come from _ledger_credit_check:
# origin/main could not be fetched, no eligible run contains the merge yet,
# and fewer than LEDGER_CREDIT_FINAL_AFTER_RUNS eligible runs have read the
# entries failing.
LEDGER_NOT_YET_STATES = frozenset(
    {
        "no_run",
        "not_main",
        "stale",
        "unavailable",
        "environment",
        "fetch_failed",
        "not_contained",
        "failing_pending",
    }
)

# A merged goal's entries must read not CONSISTENT in eligible runs on this
# many distinct commits (see _ledger_credit_check) before crediting gives up on
# that merge for good.
LEDGER_CREDIT_FINAL_AFTER_RUNS = 3
# Only the newest lines of runs.jsonl are scanned for an eligible run.
LEDGER_CREDIT_MAX_RUNS_SCANNED = 500
# Run-record fields another component sets when a run's FAILs came from its
# environment (failed install, uniform browser timeouts), not from the
# product. Optional: a run without them is an ordinary run.
LEDGER_ENVIRONMENT_FLAGS = ("environment_error", "environment_suspect")


def _ledger_run_confirms_metric(
    goal: GoalDefinition, company_dir: Path
) -> LedgerCheck | None:
    """Does the acceptance ledger's last run on main confirm this goal's metric?

    Returns None when the success metric is not in the canonical ledger-metric
    form (see ``_ledger_consistent_entry_ids``) — every other path is left
    exactly as it was. Otherwise ``confirmed`` is True only when ALL of these
    hold against ``runs.jsonl``'s most recently recorded line:

    1. that run's branch was "main" (``acceptance_ledger.MAIN_BRANCH``);
    2. that run's commit is the CURRENT tip of ``refs/heads/main`` (not HEAD:
       a checkout on another branch must not stand in for main), so a stale
       run — main has since moved — never counts, even if it once passed;
    3. every named id is in that run's ``passing_ids``.

    This is a live check. What makes a confirmation durable is the caller
    stamping it (``_stamp_ledger_confirmation``): once stamped, the goal stays
    complete however main moves afterwards, like every other verified goal.
    Never raises.
    """
    ids = _ledger_consistent_entry_ids(goal.success_metric or "")
    if ids is None:
        return None
    entry_ids = tuple(ids)

    project_root = company_dir.parent
    try:
        try:
            from . import acceptance_ledger as al
        except ImportError:
            import acceptance_ledger as al  # type: ignore[no-redef]
    except ImportError:
        return LedgerCheck(
            False,
            "acceptance_ledger module not available",
            "unavailable",
            entry_ids=entry_ids,
        )

    runs_path = project_root / ".company" / "state" / "acceptance" / "runs.jsonl"
    try:
        last_run = al._last_run_record(runs_path)
    except Exception as exc:  # LedgerError or anything unexpected: fail closed
        return LedgerCheck(
            False,
            f"{runs_path} unreadable: {exc}",
            "unavailable",
            entry_ids=entry_ids,
        )
    if not isinstance(last_run, dict):
        return LedgerCheck(
            False,
            f"no ledger run recorded yet ({runs_path})",
            "no_run",
            entry_ids=entry_ids,
        )

    run_commit = last_run.get("commit")
    run_commit = run_commit if isinstance(run_commit, str) and run_commit else None
    branch = last_run.get("branch")
    if branch != al.MAIN_BRANCH:
        return LedgerCheck(
            False,
            f"last ledger run was on branch {branch!r}, not 'main'",
            "not_main",
            run_commit,
            entry_ids,
        )

    current_commit = _default_branch_commit(project_root, al.MAIN_BRANCH)
    if not current_commit or not run_commit or run_commit != current_commit:
        return LedgerCheck(
            False,
            f"last ledger run's commit {run_commit!r} is not the current "
            f"main head {current_commit!r} (stale run)",
            "stale",
            run_commit,
            entry_ids,
        )

    passing = last_run.get("passing_ids")
    passing_ids = set(passing) if isinstance(passing, list) else set()
    missing = [i for i in entry_ids if i not in passing_ids]
    unjudged = [i for i in missing if i in _ledger_run_unfinished(last_run)]
    if unjudged:
        # LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES: a partial run never
        # reached these entries -- "not yet", never a negative.
        return LedgerCheck(
            False,
            f"ledger run at {run_commit[:8]} was partial and did not judge "
            f"{', '.join(unjudged)}; waiting for a run that does",
            "stale",
            run_commit,
            entry_ids,
        )
    environment = al.environment_flag(last_run) if missing else None
    if environment:
        return LedgerCheck(
            False,
            f"ledger run at {run_commit[:8]} does not show {', '.join(missing)} "
            f"as CONSISTENT, but it was an environment failure ({environment}); "
            "waiting for a run that judged the product",
            "environment",
            run_commit,
            entry_ids,
        )
    if missing:
        return LedgerCheck(
            False,
            f"ledger run at {run_commit[:8]} does not show {', '.join(missing)} "
            f"as CONSISTENT (passing_ids: {sorted(passing_ids)})",
            "failing",
            run_commit,
            entry_ids,
        )

    return LedgerCheck(
        True,
        f"ledger run at {run_commit[:8]} on main recorded "
        f"{', '.join(entry_ids)} CONSISTENT",
        "confirmed",
        run_commit,
        entry_ids,
    )


def _ledger_run_unfinished(run: dict) -> set[str]:
    """The entry ids a partial run never judged (``unfinished_ids``)."""
    ids = run.get("unfinished_ids")
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def _ledger_run_environment_flagged(run: dict) -> bool:
    """True when the run record says its result came from a broken environment.

    Reads ``environment_error`` / ``environment_suspect`` as truthy fields, or
    as the value of a ``status`` / ``classification`` field. Both are optional.
    """
    for flag in LEDGER_ENVIRONMENT_FLAGS:
        if run.get(flag):
            return True
    for key in ("status", "classification"):
        if run.get(key) in LEDGER_ENVIRONMENT_FLAGS:
            return True
    return False


def _git_is_ancestor(project_root: Path, ancestor: str, descendant: str) -> bool:
    """``git merge-base --is-ancestor``; equal commits count. False on any error
    (an unknown object, no git) -- an unprovable ancestry never credits."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return False
    return result.returncode == 0


def _ledger_run_records(runs_path: Path, limit: int) -> list[dict]:
    """The newest ``limit`` JSON-object lines of runs.jsonl, newest first.

    A line that is not a JSON object (a torn write) is skipped, not fatal:
    every run is judged on its own here, unlike ``_last_run_record``.
    """
    if not runs_path.is_file():
        return []
    lines = [
        line.strip()
        for line in runs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    records: list[dict] = []
    for line in reversed(lines[-limit:]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _ledger_credit_check(
    goal: GoalDefinition,
    company_dir: Path,
    *,
    merge_commit: str | None,
    main_resolver: Callable[[], tuple[str | None, str]],
    final_after: int = LEDGER_CREDIT_FINAL_AFTER_RUNS,
) -> LedgerCheck | None:
    """Can a recorded ledger run credit the goal that ``merge_commit`` shipped?

    Used by goal_crediting's ledger fallback (GOAL-CREDIT-ANCESTOR-NOT-HEAD,
    GOAL-CREDIT-AFTER-PULL-AND-NO-FINAL-ON-ENV). Returns None when the metric is
    not in the canonical ledger-metric form. ``main_resolver`` returns
    ``(origin_main_sha, reason)`` after a fetch, or ``(None, reason)`` when the
    fetch failed; it is called only for a ledger-form metric.

    A run is ELIGIBLE when all of these hold:

    1. its branch is "main" and it carries no environment flag
       (``LEDGER_ENVIRONMENT_FLAGS``);
    2. its commit is an ancestor of (or equal to) origin/main -- a run on a
       force-pushed or abandoned commit is ignored;
    3. ``merge_commit`` is an ancestor of (or equal to) its commit -- the run
       saw the merged code. A run on a commit behind main's head still counts:
       a later commit that breaks an entry is the divergence lane's job.

    States: ``confirmed`` when some eligible run read every named id
    CONSISTENT (the newest such run is the evidence); ``failing`` (final) when
    eligible runs on at least ``final_after`` DISTINCT commits exist and none
    confirmed (re-runs of one commit count once); ``failing_pending`` when
    fewer did; ``not_contained`` when no eligible run
    contains the merge yet; ``no_run`` / ``fetch_failed`` / ``unavailable``
    otherwise. Every state but ``confirmed`` and ``failing`` is in
    ``LEDGER_NOT_YET_STATES``. Never raises.
    """
    ids = _ledger_consistent_entry_ids(goal.success_metric or "")
    if ids is None:
        return None
    entry_ids = tuple(ids)
    project_root = company_dir.parent
    try:
        try:
            from . import acceptance_ledger as al
        except ImportError:
            import acceptance_ledger as al  # type: ignore[no-redef]
        main_branch = al.MAIN_BRANCH
    except ImportError:
        return LedgerCheck(
            False,
            "acceptance_ledger module not available",
            "unavailable",
            entry_ids=entry_ids,
        )

    if not merge_commit:
        return LedgerCheck(
            False,
            "the PR's merge commit is unknown",
            "unavailable",
            entry_ids=entry_ids,
        )

    runs_path = project_root / ".company" / "state" / "acceptance" / "runs.jsonl"
    try:
        runs = _ledger_run_records(runs_path, LEDGER_CREDIT_MAX_RUNS_SCANNED)
    except Exception as exc:
        return LedgerCheck(
            False,
            f"{runs_path} unreadable: {exc}",
            "unavailable",
            entry_ids=entry_ids,
        )
    if not runs:
        return LedgerCheck(
            False,
            f"no ledger run recorded yet ({runs_path})",
            "no_run",
            entry_ids=entry_ids,
        )

    try:
        main_commit, main_reason = main_resolver()
    except Exception as exc:
        main_commit, main_reason = None, f"{type(exc).__name__}: {exc}"
    if not main_commit:
        return LedgerCheck(
            False,
            f"origin/{main_branch} unavailable: {main_reason}",
            "fetch_failed",
            entry_ids=entry_ids,
        )

    on_main: dict[str, bool] = {}
    contains_merge: dict[str, bool] = {}
    failing: list[str] = []
    skipped_env = 0
    skipped_partial = 0
    for run in runs:
        commit = run.get("commit")
        if run.get("branch") != main_branch or not isinstance(commit, str):
            continue
        if not commit:
            continue
        if _ledger_run_environment_flagged(run):
            skipped_env += 1
            continue
        if commit not in on_main:
            on_main[commit] = _git_is_ancestor(project_root, commit, main_commit)
        if not on_main[commit]:
            continue
        if commit not in contains_merge:
            contains_merge[commit] = _git_is_ancestor(
                project_root, merge_commit, commit
            )
        if not contains_merge[commit]:
            continue
        passing = run.get("passing_ids")
        passing_ids = set(passing) if isinstance(passing, list) else set()
        missing = [i for i in entry_ids if i not in passing_ids]
        if missing and set(missing) & _ledger_run_unfinished(run):
            # A partial run that never reached an entry the goal names says
            # nothing about it: not evidence either way, never "failing".
            skipped_partial += 1
            continue
        if not missing:
            return LedgerCheck(
                True,
                f"ledger run at {commit[:8]} on main (contains merge "
                f"{merge_commit[:8]}, on origin/{main_branch} "
                f"{main_commit[:8]}) recorded {', '.join(entry_ids)} CONSISTENT",
                "confirmed",
                commit,
                entry_ids,
                tuple(failing),
            )
        failing.append(commit)

    env_note = (
        f"; {skipped_env} environment-flagged run(s) skipped" if skipped_env else ""
    )
    if skipped_partial:
        env_note += (
            f"; {skipped_partial} partial run(s) that did not judge these "
            "entries skipped"
        )
    if not failing:
        return LedgerCheck(
            False,
            f"no ledger run on origin/{main_branch} ({main_commit[:8]}) contains "
            f"merge {merge_commit[:8]} yet{env_note}",
            "not_contained",
            None,
            entry_ids,
        )
    # Re-runs of one commit are one piece of evidence, not several: count
    # distinct commits, so a commit re-run three times (a retried or flaky
    # environment the guard did not flag) never makes the negative final.
    distinct = len(dict.fromkeys(failing))
    state = "failing" if distinct >= final_after else "failing_pending"
    return LedgerCheck(
        False,
        f"{len(failing)} ledger run(s) on {distinct} commit(s) containing merge "
        f"{merge_commit[:8]} did not read {', '.join(entry_ids)} all CONSISTENT "
        f"(newest {failing[0][:8]}; final after {final_after} commits){env_note}",
        state,
        failing[0],
        entry_ids,
        tuple(failing),
    )


def _stamp_ledger_confirmation(
    goal: GoalDefinition,
    company_dir: Path,
    check: LedgerCheck,
    *,
    pr_number: str | int | None = None,
    pr_url: str | None = None,
) -> dict:
    """Stamp a ledger confirmation into goal_verification_state.json.

    Same locked, atomic writer as ``verify_goal`` (``_stamp_goal_verified``),
    method "ledger", with the run commit and entry ids as evidence. Once
    stamped the goal is COMPLETE through ``_verified_stamp_for_goal`` however
    main moves afterwards — without it, the next commit on main makes the run
    stale, the goal flips back to not started, autofill re-mints it and every
    goal that depends on it waits again.
    """
    return _stamp_goal_verified(
        goal,
        company_dir,
        pr_number=pr_number,
        pr_url=pr_url,
        evidence=check.reason,
        method="ledger",
        extra={
            "ledger_run_commit": check.run_commit,
            "ledger_entry_ids": list(check.entry_ids),
        },
    )


def assess_generic_success_metric(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Product-agnostic fallback assessor, driven only by the goal's own text.

    Used for any goal that isn't the framework's own dogfood goal (see
    ``_is_framework_dogfood_goal``) — e.g. a goal ID like "G1" reused by a
    downstream product for something unrelated to Gatesmith's own test coverage.
    Unlike the framework-specific assessors, this has no per-product domain
    knowledge, so it deliberately never *auto-derives* ON_TRACK or COMPLETE from
    the goal text alone — a wrong "complete" here would be written into
    goal_verification_state.json as a silent, hard-to-detect lie. It can only
    surface a weak, honest signal and otherwise defers to human/PR verification
    via ``verify_goal``.

    Two exceptions:

    * if ``verify_goal`` has already stamped this exact goal (same id +
      matching name/metric text) as verified — via operator confirm or a
      passing PR judge — this returns COMPLETE so that stamp actually sticks
      (otherwise autofill_queue_from_goals would keep targeting a "done" goal
      forever; see ProjectK finding K1b).
    * if the metric is in the canonical ledger-metric form (see
      ``_ledger_consistent_entry_ids``) and the ledger's last run, on main's
      current tip, read every named entry CONSISTENT (see
      ``_ledger_run_confirms_metric``) — the ledger is an oracle outside the
      artifact, not a text-only guess. The confirmation is STAMPED (method
      "ledger", run commit + entry ids) so the goal stays complete once main
      moves on; the first exception then returns COMPLETE for it.
    """
    stamp = _verified_stamp_for_goal(goal, company_dir)
    if stamp is not None:
        method = stamp.get("method", "verify_goal")
        pr_suffix = f" (PR #{stamp['pr_number']})" if stamp.get("pr_number") else ""
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=1.0,
            target_value=1.0,
            progress_percent=100,
            status=GoalStatus.COMPLETE,
            status_reason=(
                f"Verified via {method} on {stamp.get('verified_at', 'unknown date')}"
                f"{pr_suffix}."
            ),
            next_actions=[],
            raw_data={
                "assessor": "generic_success_metric",
                "dogfood_match": False,
                "verified_stamp": True,
                "verified_at": stamp.get("verified_at"),
                "pr_number": stamp.get("pr_number"),
                "pr_url": stamp.get("pr_url"),
                "evidence": stamp.get("evidence"),
                "method": method,
            },
        )

    ledger_check = _ledger_run_confirms_metric(goal, company_dir)
    if ledger_check is not None and ledger_check.confirmed:
        reason = ledger_check.reason
        # Stamp it: a live-only COMPLETE flaps back to not started on the next
        # commit to main (the run goes stale), autofill re-mints the goal and
        # every dependent waits on it again. A failed write still reports
        # COMPLETE for this call and is retried on the next assessment.
        stamped = False
        try:
            _stamp_ledger_confirmation(goal, company_dir, ledger_check)
            stamped = True
        except Exception:
            pass
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=1.0,
            target_value=1.0,
            progress_percent=100,
            status=GoalStatus.COMPLETE,
            status_reason=reason,
            next_actions=[],
            raw_data={
                "assessor": "generic_success_metric",
                "dogfood_match": False,
                "ledger_confirmed": True,
                "ledger_stamped": stamped,
                "method": "ledger",
                "ledger_run_commit": ledger_check.run_commit,
                "ledger_entry_ids": list(ledger_check.entry_ids),
            },
        )
    # Not confirmed (stale/absent run, or an entry not passing): fall through
    # unchanged to the existing artifact/manual-verify paths below, exactly as
    # if the metric had never matched the shape at all.

    project_root = company_dir.parent
    success_metric = (goal.success_metric or "").strip()

    if not success_metric:
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=0.0,
            target_value=1.0,
            progress_percent=0,
            status=GoalStatus.NOT_STARTED,
            status_reason="No success metric text provided; nothing to auto-assess.",
            raw_data={"assessor": "generic_success_metric", "dogfood_match": False},
        )

    # Best-effort: if the goal author named a concrete artifact (backtick-quoted
    # path/command, or a bare /slash-command), check whether it exists. This is
    # a weak signal, not proof the metric is satisfied — capped at AT_RISK.
    artifact = None
    match = _METRIC_ARTIFACT_RE.search(success_metric)
    if match:
        artifact = (match.group(1) or match.group(2) or "").strip()

    artifact_exists = False
    if artifact and not artifact.startswith("/") and ".." not in Path(artifact).parts:
        try:
            resolved_root = project_root.resolve()
            resolved_artifact = (project_root / artifact).resolve()
            if resolved_artifact.is_relative_to(resolved_root):
                artifact_exists = resolved_artifact.exists()
        except (OSError, ValueError):
            artifact_exists = False

    if artifact_exists:
        status = GoalStatus.AT_RISK
        current_value = 0.5
        status_reason = (
            f"Referenced artifact '{artifact}' exists on disk, but generic "
            "assessment cannot confirm it satisfies the full success metric; "
            f"requires manual verification (see /verify {goal.id})."
        )
    else:
        status = GoalStatus.NOT_STARTED
        current_value = 0.0
        status_reason = (
            f'Cannot auto-verify "{success_metric}" generically; requires '
            f"manual verification (see /verify {goal.id})."
        )

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=int(current_value * 100),
        status=status,
        status_reason=status_reason,
        next_actions=[f"Manually verify and run /verify {goal.id}"],
        raw_data={
            "assessor": "generic_success_metric",
            "dogfood_match": False,
            "artifact": artifact,
            "artifact_exists": artifact_exists,
        },
    )


# -----------------------------------------------------------------------------
# Assessor Registry
# -----------------------------------------------------------------------------


GOAL_ASSESSORS: dict[str, Callable[[GoalDefinition, Path], GoalAssessment]] = {
    "G1": assess_test_coverage,
    "G2": assess_tutorials,
    "G3": assess_stability,
    "G4": assess_enterprise,
    "G5": assess_autonomy,
    "G6": assess_economics,
    "G7": assess_sustained_autonomy,
    "G8": assess_scout_throughput,
    "G9": assess_stability,
    "G10": assess_employee_initiative,
    "G11": assess_queue_health,
    "G12": assess_parallel_throughput,
    "G16": assess_ci_green,
    "G17": assess_acceptance_ledger,
    "G18": assess_controls_that_fire,
    "G19": assess_launch_readiness,
}


# Default goal definitions (from vision.md)
DEFAULT_GOALS: list[GoalDefinition] = [
    GoalDefinition(
        "G1",
        "Quality",
        # Retargeted 2026-08-20 (50% -> 90% total) and again 2026-09-04, this
        # time changing the SHAPE: a repository total hid the distribution the
        # coverage lane works on (91.5% overall while individual modules sat in
        # the 40s), and a total is the number the exclusion bias (#448) and the
        # staleness narrowing (#383) had to be defended against. The floor asks
        # one question per module, so target_value is 1.0 = "every counted
        # module at MODULE_COVERAGE_FLOOR or better", not "the average is high".
        # NOTE: name + success_metric are a FINGERPRINT. _is_forge_own_goal()
        # routes G1 to assess_test_coverage only on an exact match against
        # .company/vision.md's row, so these two strings and that row must move
        # together or G1 silently falls back to the generic assessor.
        "No non-protected module below 90 percent coverage",
        "Every module at 90%+ (protected daemon files excluded)",
        "forge-cto",
        1.0,
    ),
    GoalDefinition(
        "G2",
        "Adoption",
        "Create getting-started tutorials",
        "3 tutorials published",
        "marketing-lead",
        1.0,
    ),
    GoalDefinition(
        "G3",
        "Stability",
        "Zero critical bugs in core workflow",
        "0 P0 bugs open",
        "forge-architect",
        1.0,
    ),
    GoalDefinition(
        "G4",
        "Enterprise",
        "Expand audit capabilities",
        "Audit export command exists",
        "forge-security-engineer",
        1.0,
    ),
    GoalDefinition(
        "G5",
        "Autonomy",
        "Enable fully autonomous operation",
        "Org runs without prompts for 24h",
        "forge-cto",
        1.0,
    ),
    GoalDefinition(
        "G6",
        "Economics",
        "Track token consumption in subscription",
        "Token tracking via efficiency_tracker",
        "forge-architect",
        1.0,
    ),
    GoalDefinition(
        "G7",
        "Sustained Autonomy",
        # Description only (2026-09-04). success_metric is half the dogfood
        # fingerprint and stays byte-identical; the wording change records
        # what G7 *is* — a service-level objective read off measurement, not
        # a lane that mints work. See the [INFRA-ONLY] rationale below.
        "SLO, measured not minted: daemon uptime and worker success rate",
        "7 days autonomous, >90% success rate",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G8",
        "Self-Improvement",
        # Retargeted 2026-09-04. "10+ approved improvement proposals" counted
        # proposal records, an artifact the org writes about itself: it could
        # be satisfied without a single line reaching main. Scout throughput
        # counts entries that came in through .planning/ROADMAP-SCOUT.md and
        # SHIPPED, which is the same loop measured at its output.
        "Scout intake throughput: entries from ROADMAP-SCOUT.md delivered to main",
        "2+ scout entries delivered per week",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G9",
        "Stability",
        "All tests passing, zero regressions",
        "0 test failures",
        "forge-architect",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G10",
        "Employee Initiative",
        "Employee-initiated tasks",
        "50% tasks from employee ideas",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G11",
        "Queue Health",
        "Self-generating work queue",
        "Queue never empty >1 hour",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G12",
        "Parallel Throughput",
        "Validate parallel worktree execution under load",
        "2+ workers active simultaneously for 24h",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G16",
        "CI Green",
        # Description only (2026-09-04); success_metric is fingerprint and
        # stays byte-identical. Same shape as G7: an SLO read off the record
        # of what CI did, not a lane that mints tasks.
        "SLO, measured not minted: keep main healthy so the autonomy levers stay trustworthy",
        "0 failures on 3 consecutive PRs",
        "forge-architect",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G17",
        "Acceptance Ledger",
        "GAP-7: an oracle outside the artifact; increments 2-5 of the acceptance ledger",
        "4 increments shipped (base-ref+canary, corpus, blind probe, daemon trigger)",
        "forge-architect",
        1.0,
        period="Q3 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G18",
        "Controls That Fire",
        "Every blocking hook proves it fires: a subprocess test that triggers the block, and a denial record",
        "6/6 blocking hooks with a fires-test and a gates.jsonl record",
        "forge-security-engineer",
        1.0,
        period="Q3 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G19",
        "Launch Readiness",
        # name + success_metric are a FINGERPRINT (see G1): they must stay
        # byte-identical to .company/vision.md's G19 row or the goal routes to
        # the generic assessor and never shows progress.
        "Public launch 2027-01-19: a newcomer gets from the exported README to a first merged PR alone; README and docs match what the install ships (counts, commands, Gatesmith names); limits stated honestly (daemon macOS-only); no Gatesmith leftovers where a user looks; installer gives the user's project its own starter files",
        "Stranger test of the public export (docs/stranger-test-*.md): 0 blockers, at most 2 confusing",
        "forge-architect",
        1.0,
        period="Q3 2026",
        period_status="active",
    ),
]


_DEFAULT_GOALS_BY_ID: dict[str, GoalDefinition] = {g.id: g for g in DEFAULT_GOALS}


def _register_business_goals() -> None:
    """Gatesmith's own business goals (G13-G15) come from a private plugin.

    The public export leaves goal_tracker_business out (launch K2a): first
    revenue, the compliance pack and website conversion measure this
    company, not a Gatesmith install. The plugin extends the tables in place,
    because callers compare ``parse_goals_from_vision(...) is DEFAULT_GOALS``.
    """
    try:
        from . import optional_modules as opt
    except ImportError:
        import optional_modules as opt  # type: ignore[no-redef]
    plugin = opt.import_optional("goal_tracker_business", __package__)
    if plugin is not None:
        plugin.register(sys.modules[__name__])


_register_business_goals()


def _normalize_fingerprint_text(text: str) -> str:
    """Normalize text for dogfood fingerprint comparison (case/whitespace only)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_framework_dogfood_goal(goal: GoalDefinition) -> bool:
    """Is this goal actually Gatesmith's own canonical goal for this ID?

    GOAL_ASSESSORS dispatches by goal ID string alone ("G1".."G16"), but any
    project using Gatesmith can write its own vision.md reusing those same IDs
    for unrelated goals (e.g. a csv2md tool's "G1" meaning something totally
    different from Gatesmith's own "G1: test coverage"). Routing those through
    Gatesmith's framework-specific assessors produces nonsense results measuring
    the wrong thing entirely.

    A repo-identity check (e.g. "does .claude/commands/audit-export.md
    exist") is NOT sufficient here — any project scaffolded from Gatesmith
    ships that same .claude/ directory, so it would still misroute.
    Instead, compare the parsed goal's own name + success_metric text
    against Gatesmith's canonical DEFAULT_GOALS definition for that ID: only an
    exact (case/whitespace-normalized) match on BOTH fields counts as
    "this really is Gatesmith's own goal." description/owner/period are
    deliberately excluded — they're either synthesized differently by the
    tolerant vision.md table parser or legitimately vary (e.g. reassigned
    ownership) without changing what the goal actually measures.
    """
    default = _DEFAULT_GOALS_BY_ID.get(goal.id)
    if default is None:
        return False
    return _normalize_fingerprint_text(goal.name) == _normalize_fingerprint_text(
        default.name
    ) and _normalize_fingerprint_text(
        goal.success_metric
    ) == _normalize_fingerprint_text(default.success_metric)


def is_framework_dogfood_assessment(assessment: Any) -> bool:
    """``_is_framework_dogfood_goal`` for a GoalAssessment (or anything shaped
    like one: goal_id / goal_name / success_metric attributes).

    Consumers downstream of assessment (the goal scheduler's per-id strategic
    weights, for one) need the same "is this really Gatesmith's own goal" answer
    the assessor dispatch uses, without re-parsing vision.md. Never raises: an
    object without string fields is not Gatesmith's goal.
    """
    try:
        goal_id = getattr(assessment, "goal_id", None)
        name = getattr(assessment, "goal_name", None)
        metric = getattr(assessment, "success_metric", None)
        if not all(isinstance(v, str) for v in (goal_id, name, metric)):
            return False
        return _is_framework_dogfood_goal(GoalDefinition(goal_id, name, "", metric, ""))
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Main Functions
# -----------------------------------------------------------------------------


def parse_period_markers(content: str) -> list[dict]:
    """Parse period markers from vision.md content.

    Looks for headers like: ### Period: Q1 2026 [status: complete]
    Returns list of dicts: [{"name": "Q1 2026", "status": "complete", "start_pos": N}, ...]
    """
    pattern = r"### Period:\s*(.+?)\s*\[status:\s*(\w+)\]"
    markers = []
    for match in re.finditer(pattern, content):
        markers.append(
            {
                "name": match.group(1).strip(),
                "status": match.group(2).strip(),
                "start_pos": match.start(),
            }
        )
    return markers


def get_active_period(vision_path: Path) -> str | None:
    """Get the name of the currently active period from vision.md."""
    if not vision_path.exists():
        return None

    try:
        content = vision_path.read_text()
    except Exception:
        return None

    markers = parse_period_markers(content)
    for marker in markers:
        if marker["status"] == "active":
            return marker["name"]
    return None


def check_period_transition(company_dir: Path) -> dict | None:
    """Check if the active period is complete and advance to next.

    Reads vision.md, assesses all goals in the active period.
    If ALL goals have status COMPLETE, rewrites vision.md to:
    - Mark active period as [status: complete]
    - Mark next period as [status: active]

    Returns:
        Dict with transition details if transition occurred, None otherwise.
    """
    import tempfile

    vision_path = company_dir / "vision.md"
    if not vision_path.exists():
        return None

    active_period = get_active_period(vision_path)
    if not active_period:
        return None

    goals = parse_goals_from_vision(vision_path, period=active_period)
    if not goals:
        return None

    # Assess each goal (lightweight to avoid expensive operations)
    all_complete = True
    for goal in goals:
        assessment = assess_goal(goal, company_dir, lightweight=True)
        if (
            assessment.status != GoalStatus.COMPLETE
            and assessment.progress_percent < 100
        ):
            all_complete = False
            break

    if not all_complete:
        return None

    # All goals complete — find the next period and transition
    content = vision_path.read_text()
    markers = parse_period_markers(content)

    if len(markers) < 2:
        return None

    # Find the active period index
    active_idx = None
    for i, marker in enumerate(markers):
        if marker["name"] == active_period and marker["status"] == "active":
            active_idx = i
            break

    if active_idx is None:
        return None

    # Check there is a next period
    if active_idx + 1 >= len(markers):
        return None

    next_period = markers[active_idx + 1]
    next_name = next_period["name"]
    next_status = next_period["status"]

    # Replace the active period's status marker with "complete"
    old_active_marker = f"### Period: {active_period} [status: active]"
    new_active_marker = f"### Period: {active_period} [status: complete]"
    updated = content.replace(old_active_marker, new_active_marker, 1)

    # Replace the next period's status marker with "active"
    old_next_marker = f"### Period: {next_name} [status: {next_status}]"
    new_next_marker = f"### Period: {next_name} [status: active]"

    if old_next_marker in updated:
        updated = updated.replace(old_next_marker, new_next_marker, 1)
    else:
        # Next period might not have a status marker — add one by finding
        # the period header line (e.g. "### Period: Q3 2026") without status
        # and appending the status marker.
        bare_pattern = re.compile(
            r"(###\s+Period:\s*" + re.escape(next_name) + r")\s*$",
            re.MULTILINE,
        )
        match = bare_pattern.search(updated)
        if match:
            updated = (
                updated[: match.end()] + " [status: active]" + updated[match.end() :]
            )

    # Atomic write to vision.md
    dir_path = vision_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(updated)
        os.replace(tmp_path, str(vision_path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None

    return {
        "old_period": active_period,
        "new_period": next_name,
        "goals_completed": len(goals),
        "transitioned_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_goals_from_vision(
    vision_path: Path, *, period: str = "active"
) -> list[GoalDefinition]:
    """Parse goal definitions from vision.md.

    Args:
        vision_path: Path to vision.md file.
        period: Which period to parse goals from.
            "active" — only goals from the active period section.
            "all" — goals from all periods.
            A specific name (e.g. "Q1 2026") — goals from that period only.
    """
    if not vision_path.exists():
        return DEFAULT_GOALS

    try:
        content = vision_path.read_text()
    except Exception:
        return DEFAULT_GOALS

    # Parse period markers to determine content boundaries
    markers = parse_period_markers(content)

    # If no period markers found, fall back to existing behavior (parse all goals)
    if not markers:
        return _parse_goals_from_content(content, period_name=None, period_status=None)

    # Determine which sections to parse based on the period parameter
    sections: list[tuple[str, str | None, str | None]] = []  # (content, name, status)

    for i, marker in enumerate(markers):
        start = marker["start_pos"]
        end = markers[i + 1]["start_pos"] if i + 1 < len(markers) else len(content)
        section_content = content[start:end]
        m_name = marker["name"]
        m_status = marker["status"]

        if period == "all":
            sections.append((section_content, m_name, m_status))
        elif period == "active":
            if m_status == "active":
                sections.append((section_content, m_name, m_status))
        else:
            # Specific period name requested
            if m_name == period:
                sections.append((section_content, m_name, m_status))

    goals: list[GoalDefinition] = []
    for section_content, p_name, p_status in sections:
        goals.extend(
            _parse_goals_from_content(
                section_content, period_name=p_name, period_status=p_status
            )
        )

    return goals if goals else DEFAULT_GOALS


def _warn_dep_cycles(goals: list[GoalDefinition]) -> None:
    """Emit a stderr warning if circular dependsOn chains are detected (DFS)."""
    known = {g.id for g in goals}
    adj = {g.id: [d for d in g.depends_on if d in known] for g in goals}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        result = any(_has_cycle(dep) for dep in adj.get(node, []))
        in_stack.discard(node)
        return result

    for goal_id in list(adj):
        if goal_id not in visited and _has_cycle(goal_id):
            print(
                f"WARNING goal_tracker: circular dependsOn detected involving {goal_id}; "
                "dep-blocked goals will be suppressed indefinitely",
                file=sys.stderr,
            )
            return


# A Unicode Private Use Area character: never legitimately typed into a
# vision.md goal row, so it is safe to use as a one-way stand-in for an
# escaped pipe (`\|`) while splitting a row on raw `|` characters below.
_ESCAPED_PIPE_PLACEHOLDER = ""

# A strict-format row (`| G1: Name | Description | Metric | Owner |`) has
# exactly 4 cells. Anchors the Name cell on the left (up to its own first
# `|`) and the two RIGHTMOST cells (Success metric, Owner) on the right, so
# whatever sits in between -- however many raw, unescaped `|` characters it
# contains -- is one Description cell. `re.MULTILINE` keeps `^`/`$` per line;
# `.` without DOTALL keeps the (greedy, backtracking) middle group from ever
# crossing into a different row.
_ROW_WITH_NAME_RE = re.compile(
    r"^(\s*\|\s*\*{0,2}G\d+:[^\n|]*?\|)(.*)(\|[^|\n]*\|[^|\n]*\|\s*)$",
    re.MULTILINE,
)


def _mask_stray_row_pipes(text: str) -> str:
    """Mask unescaped `|` characters inside a goal row's Description cell.

    A literal `|` typed into free-text Description content -- not a markdown
    escape, just prose or an example string that happens to contain one (a
    key-derivation string like `"...v1|<id>|<secret>"`) -- reads as an extra
    column boundary and misaligns every cell after it, the same way an
    unescaped `\\|` does (see `_parse_goals_from_content`): webapp G38
    (2026-10-06) had its Description truncated mid-sentence, its
    `dependsOn:` annotation lost outside any parsed cell, and its Success
    metric and Owner replaced by fragments of its own example strings.
    Scoped to the strict `G\\d+:` row shape only (see `_ROW_WITH_NAME_RE`); a
    row that does not match it (the 5-column tolerated format, or one where
    Success metric/Owner themselves hold a stray `|`) is left untouched --
    not worse than before, just not fixed here.
    """

    def _mask_middle(match: "re.Match[str]") -> str:
        prefix, middle, suffix = match.group(1), match.group(2), match.group(3)
        return prefix + middle.replace("|", _ESCAPED_PIPE_PLACEHOLDER) + suffix

    return _ROW_WITH_NAME_RE.sub(_mask_middle, text)


def _parse_goals_from_content(
    content: str, *, period_name: str | None, period_status: str | None
) -> list[GoalDefinition]:
    """Parse goal table rows from a content string.

    Args:
        content: Text content containing markdown goal tables.
        period_name: Period name to set on each parsed goal (e.g. "Q1 2026").
        period_status: Period status to set on each parsed goal (e.g. "active").

    An escaped pipe (``\\|``) inside a cell -- the standard markdown escape
    for a literal ``|`` in a table -- is masked before either pattern below
    matches and restored once a cell's text is captured. Without this, a
    literal ``\\|`` in a free-text description (e.g. "invite: 'open' \\|
    'closed'") reads as an extra column boundary and shifts every group after
    it: a webapp goal row (G36, 2026-10-06) had its REAL success metric
    replaced by a fragment of its own description, and the goal read
    ``complete`` on a feature that had not been built
    (NO-DIVERGENCE-FOR-UNBUILT-ENTRIES). An UNESCAPED stray ``|`` in a
    strict-format row's Description cell gets the same treatment, via
    ``_mask_stray_row_pipes`` -- see its docstring (webapp G38).
    """
    goals: list[GoalDefinition] = []
    seen_ids: set[str] = set()
    masked_content = content.replace("\\|", _ESCAPED_PIPE_PLACEHOLDER)
    masked_content = _mask_stray_row_pipes(masked_content)

    def _add_goal(goal_id, name, description, success_metric, owner):
        goal_id = goal_id.replace(_ESCAPED_PIPE_PLACEHOLDER, "|").strip()
        if goal_id in seen_ids:
            return  # a row already matched by a stricter pattern wins
        seen_ids.add(goal_id)
        name = name.replace(_ESCAPED_PIPE_PLACEHOLDER, "|").strip()
        description = description.replace(_ESCAPED_PIPE_PLACEHOLDER, "|").strip()
        success_metric = success_metric.replace(_ESCAPED_PIPE_PLACEHOLDER, "|")
        owner = owner.replace(_ESCAPED_PIPE_PLACEHOLDER, "|")
        # Parse optional dependsOn annotation (e.g. "Enable autonomy dependsOn: G1,G2")
        depends_on: list[str] = []
        dep_match = re.search(r"\s+dependsOn:\s*", description, re.IGNORECASE)
        if dep_match:
            dep_text = description[dep_match.end() :]
            description = description[: dep_match.start()].strip()
            depends_on = re.findall(r"\bG\d+\b", dep_text)
        goals.append(
            GoalDefinition(
                goal_id,
                name,
                description,
                success_metric.strip(),
                owner.strip(),
                period=period_name,
                period_status=period_status,
                depends_on=depends_on,
            )
        )

    # Primary (canonical) format: | G1: Quality | Desc | Metric | Owner |
    # (optionally bold). Non-greedy ([^|]+?) matches multi-word names.
    strict_pattern = r"\|\s*\*{0,2}(G\d+):\s*([^|]+?)\*{0,2}\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|"
    for match in re.finditer(strict_pattern, masked_content):
        _add_goal(*match.groups())

    # Tolerated 5-column format with the ID in its OWN cell:
    # | G1 | Category | Goal | Success Criteria | Owner |  (no colon after the ID).
    # This is what the daemon's _create_default_vision historically emitted and a
    # natural shape a user might write (ProjectK K3). Anchored on `Gn` alone so it
    # never matches an arbitrary/non-goal table. The Category column is folded into
    # the description; the Goal column becomes the name.
    five_col_pattern = (
        r"\|\s*\*{0,2}(G\d+)\*{0,2}\s*\|\s*([^|]+)\|\s*([^|]+?)\s*\|"
        r"\s*([^|]+)\|\s*([^|]+)\|"
    )
    for match in re.finditer(five_col_pattern, masked_content):
        goal_id, category, goal_name, success_metric, owner = match.groups()
        category = category.replace(_ESCAPED_PIPE_PLACEHOLDER, "|").strip()
        goal_name = goal_name.replace(_ESCAPED_PIPE_PLACEHOLDER, "|").strip()
        # Skip a Markdown separator row like | G1 |---|---|---|---| defensively.
        if set(goal_name) <= {"-", ":", " "}:
            continue
        description = (
            f"{category}: {goal_name}" if category and category != "-" else goal_name
        )
        _add_goal(goal_id, goal_name, description, success_metric, owner)

    _warn_dep_cycles(goals)
    return goals


def assess_goal(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess a single goal using the appropriate assessor.

    Framework-specific assessors in GOAL_ASSESSORS only apply when this goal
    is verifiably Gatesmith's own dogfood goal for its ID (see
    ``_is_framework_dogfood_goal``) — otherwise a downstream product reusing
    goal IDs G1-G16 for unrelated goals would get measured by Gatesmith's own
    test-coverage/tutorials/autonomy checks instead of its own success
    metric. Any other goal falls through to the generic, product-agnostic
    assessor.

    Args:
        lightweight: If True, skip expensive operations (pytest).
            Used by daemon to keep cycles fast.
    """
    assessor = GOAL_ASSESSORS.get(goal.id) if _is_framework_dogfood_goal(goal) else None
    if assessor is None:
        assessor = assess_generic_success_metric

    # Pass lightweight to assessors that accept it (G1, G3, generic, ...)
    import inspect

    sig = inspect.signature(assessor)
    if "lightweight" in sig.parameters:
        return assessor(goal, company_dir, lightweight=lightweight)
    return assessor(goal, company_dir)


def calculate_goal_velocity(
    goal_id: str,
    company_dir: Path,
    lookback_days: int = 7,
) -> tuple[float, str]:
    """
    WS-113-003: Calculate goal velocity (progress change per day).

    Uses goal snapshots from strategic state to determine how fast
    a goal is progressing, stalling, or regressing.

    Args:
        goal_id: The goal ID to calculate velocity for
        company_dir: Company directory path
        lookback_days: Number of days to look back for velocity calculation

    Returns:
        Tuple of (velocity_percent_per_day, trend)
        - velocity: positive = improving, negative = regressing, ~0 = stalled
        - trend: "improving", "stalled", or "regressing"
    """
    state_path = company_dir / "state" / "strategic_state.json"
    if not state_path.exists():
        return 0.0, "unknown"

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.0, "unknown"

    snapshots = state.get("goal_snapshots", [])
    if len(snapshots) < 2:
        return 0.0, "unknown"

    # Get relevant snapshots within lookback period
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    cutoff = datetime.now(tz.utc) - timedelta(days=lookback_days)
    recent_snapshots = []

    for snap in snapshots:
        try:
            snap_time = datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))
            if snap_time >= cutoff:
                recent_snapshots.append(snap)
        except (KeyError, ValueError):
            continue

    if len(recent_snapshots) < 2:
        # Fall back to last 2 snapshots regardless of time
        recent_snapshots = snapshots[-2:]

    # Find goal progress in first and last snapshot
    first_snap = recent_snapshots[0]
    last_snap = recent_snapshots[-1]

    first_progress = None
    last_progress = None

    for assess in first_snap.get("assessments", []):
        if assess.get("goal_id") == goal_id:
            first_progress = assess.get("progress", 0)
            break

    for assess in last_snap.get("assessments", []):
        if assess.get("goal_id") == goal_id:
            last_progress = assess.get("progress", 0)
            break

    if first_progress is None or last_progress is None:
        return 0.0, "unknown"

    # Calculate time delta
    try:
        first_time = datetime.fromisoformat(
            first_snap["timestamp"].replace("Z", "+00:00")
        )
        last_time = datetime.fromisoformat(
            last_snap["timestamp"].replace("Z", "+00:00")
        )
        days_elapsed = max((last_time - first_time).total_seconds() / 86400, 0.1)
    except (KeyError, ValueError):
        days_elapsed = 1.0

    # Calculate velocity (percent change per day)
    progress_delta = last_progress - first_progress
    velocity = progress_delta / days_elapsed

    # Determine trend
    if velocity > 2.0:  # >2% per day = improving
        trend = "improving"
    elif velocity < -1.0:  # <-1% per day = regressing
        trend = "regressing"
    else:
        trend = "stalled"

    return round(velocity, 2), trend


def assess_all_goals(
    company_dir: Path, *, lightweight: bool = False, period: str = "active"
) -> list[GoalAssessment]:
    """Assess all goals and return assessments.

    Args:
        lightweight: If True, skip expensive operations (pytest).
            Daemon cycles use this to avoid 5-10 min blocking.
            Manual ``/strategy assess`` uses full mode by default.
        period: Which period to assess goals from ("active", "all", or a
            specific period name like "Q1 2026").
    """
    vision_path = company_dir.parent / ".company" / "vision.md"
    if not vision_path.exists():
        vision_path = company_dir / "vision.md"

    goals = parse_goals_from_vision(vision_path, period=period)
    assessments = []

    for goal in goals:
        assessment = assess_goal(goal, company_dir, lightweight=lightweight)

        # WS-113-003: Add velocity tracking
        velocity, trend = calculate_goal_velocity(goal.id, company_dir)
        assessment.velocity_percent_per_day = velocity
        assessment.velocity_trend = trend
        assessment.depends_on = goal.depends_on

        assessments.append(assessment)

    return assessments


_VERIFICATION_STATE_FILE = "state/goal_verification_state.json"


def load_goal_verification_state(company_dir: Path) -> dict[str, dict]:
    """Load goal verification state from disk.

    Returns dict mapping goal_id -> verification record (keys: status,
    verified_at, pr_number, pr_url, goal_name, success_metric).
    Returns {} when the file is absent or unreadable.
    """
    state_path = company_dir / _VERIFICATION_STATE_FILE
    if not state_path.exists():
        return {}
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _stamp_goal_verified(
    goal: GoalDefinition,
    company_dir: Path,
    *,
    pr_number: str | int | None,
    pr_url: str | None,
    evidence: str | None,
    method: str,
    progress_percent: int = 100,
    extra: dict | None = None,
) -> dict:
    """Write a verified record for ``goal`` and return the CLI-facing result dict.

    ``extra`` adds method-specific evidence fields to the record (e.g. the
    ledger run commit and entry ids for method "ledger"); it never overrides
    the core fields.
    """
    verified_at = datetime.now(timezone.utc).isoformat()
    record = {
        "status": "verified",
        "verified_at": verified_at,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "goal_name": goal.name,
        "success_metric": goal.success_metric,
        "progress_percent": progress_percent,
        "evidence": evidence,
        "method": method,
    }
    for key, value in (extra or {}).items():
        record.setdefault(key, value)

    # ADR-008 pattern: an exclusive flock on a sidecar lock file around the
    # read-modify-write, and an atomic temp-file + os.replace so a lock-free
    # reader never sees a truncated or half-written file. The background
    # goal-credit sweep (goal_crediting.py) stamps while the daemon loop's
    # autofill reads this file; the old in-place "w" rewrite let 144 of 34,458
    # concurrent reads come back {} (a verified G1 read as unverified), and a
    # concurrent operator `verify` could lose the other writer's goal.
    import tempfile

    state_path = company_dir / _VERIFICATION_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = load_goal_verification_state(company_dir)
            state[goal.id] = record
            fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, state_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    return {
        "goal_id": goal.id,
        "status": "verified",
        "message": f"Goal {goal.id} verified via {method}.",
        "pr_number": pr_number,
        "pr_url": pr_url,
        "verified_at": verified_at,
    }


def _judge_goal_completion_via_pr(
    goal: GoalDefinition, pr_number: str | int
) -> dict | None:
    """Ask the deliverable judge whether a merged PR satisfies this goal's metric.

    Reuses deliverable_judge's existing fail-closed CLI plumbing (same judge used
    for the pre-merge task gate) instead of re-implementing CLI-detection/subprocess
    logic here. Returns None on ANY failure — no claude/uv CLI on PATH, no `gh`,
    judge timeout/error, unparseable output, low-confidence/negative verdict, or an
    import failure — so the caller never stamps a goal on an inconclusive signal.
    Never raises.
    """
    try:
        try:
            from . import deliverable_judge as _deliverable_judge
        except ImportError:
            import deliverable_judge as _deliverable_judge
    except ImportError:
        return None

    try:
        verdict = _deliverable_judge.judge_pr_deliverable(
            task_id=f"goal-{goal.id}",
            title=goal.name,
            description=goal.description,
            pr_number=pr_number,
            goal_metric=goal.success_metric,
            use_cache=False,
        )
    except Exception:
        return None

    if (
        verdict.error is not None
        or verdict.addresses_task is not True
        or verdict.blocked
        or verdict.needs_manual_review
    ):
        return None
    return {"reason": verdict.reason, "confidence": verdict.confidence}


def verify_goal(
    goal_id: str,
    company_dir: Path,
    *,
    pr_number: str | int | None = None,
    pr_url: str | None = None,
    confirm: bool = False,
    evidence: str | None = None,
) -> dict:
    """Run goal metric check post-merge and stamp verified state.

    Loads the goal from vision.md and determines completeness one of three ways,
    in order:

    1. **Assessment-based** (default, all goals): runs ``assess_goal``; if it
       reports COMPLETE, stamps verified. This is the only path available to
       Gatesmith's own dogfood goals (see ``_is_framework_dogfood_goal``) — their
       framework-specific assessors are the sole source of truth, so ``confirm``
       has NO effect on them.
    2. **Operator confirm** (generic-assessed goals only): ``assess_generic_success_metric``
       deliberately never returns COMPLETE on its own (a wrong auto-complete would
       be a silent lie), so it needs a real escape hatch. Passing ``confirm=True``
       with non-empty ``evidence`` text stamps the goal directly, bypassing the
       assessment gate. Fails closed (not stamped) if evidence is missing/blank.
    3. **PR-judge** (generic-assessed goals only, opt-in via ``pr_number``): when
       the assessment didn't complete the goal and no confirm was given, asks the
       same LLM judge used for pre-merge deliverable gating whether the merged
       PR's diff satisfies the goal's success metric. Fails closed to not-stamped
       on any judge error, missing CLI, or a negative/low-confidence verdict.

    Args:
        goal_id: Goal identifier, e.g. "G1"
        company_dir: Path to .company directory
        pr_number: Merged PR number for evidence record / PR-judge lookup
        pr_url: Merged PR URL for evidence record
        confirm: Operator-asserted completion (generic-assessed goals only)
        evidence: Required justification text when ``confirm=True``

    Returns:
        Dict with keys: goal_id, status ("verified" | "not_complete" |
        "not_found"), message, pr_number, pr_url, and (when verified)
        verified_at. "not_complete" results also carry assessment_status.
    """
    vision_path = company_dir.parent / ".company" / "vision.md"
    if not vision_path.exists():
        vision_path = company_dir / "vision.md"

    goals = parse_goals_from_vision(vision_path)
    goal = next((g for g in goals if g.id == goal_id), None)
    if goal is None:
        return {
            "goal_id": goal_id,
            "status": "not_found",
            "message": f"Goal {goal_id} not found in vision.md",
        }

    is_dogfood = _is_framework_dogfood_goal(goal)

    # Idempotent for generic-assessed goals only: an already-live stamp wins
    # outright, so a repeat call never clobbers the original evidence/method with
    # a weaker record. Dogfood goals deliberately re-run their real assessor every
    # time (skip this shortcut) so a metric that later regresses (e.g. a new P0
    # bug after G3 was once verified) is never masked by a stale "already verified".
    existing_stamp = None if is_dogfood else _verified_stamp_for_goal(goal, company_dir)
    if existing_stamp is not None:
        return {
            "goal_id": goal_id,
            "status": "verified",
            "message": (
                f"Goal {goal_id} already verified via "
                f"{existing_stamp.get('method', 'verify_goal')} on "
                f"{existing_stamp.get('verified_at', 'unknown date')}."
            ),
            "pr_number": existing_stamp.get("pr_number"),
            "pr_url": existing_stamp.get("pr_url"),
            "verified_at": existing_stamp.get("verified_at"),
        }

    # Operator confirm: a legitimate bypass ONLY for generic-assessed goals, whose
    # own assessor can never return COMPLETE by design. Dogfood goals keep the
    # strict assessment gate unconditionally — confirm is silently ignored for them
    # rather than special-cased into an error, since callers may pass confirm=True
    # generically without knowing a goal's dogfood status.
    if confirm and not is_dogfood:
        if not evidence or not evidence.strip():
            return {
                "goal_id": goal_id,
                "status": "not_complete",
                "message": (
                    f"Cannot confirm goal {goal_id} without evidence text, e.g. "
                    f'/verify {goal_id} --confirm "evidence..."'
                ),
                "pr_number": pr_number,
                "pr_url": pr_url,
            }
        return _stamp_goal_verified(
            goal,
            company_dir,
            pr_number=pr_number,
            pr_url=pr_url,
            evidence=evidence.strip(),
            method="operator_confirm",
        )

    assessment = assess_goal(goal, company_dir)

    raw = assessment.raw_data if isinstance(assessment.raw_data, dict) else {}
    if assessment.status == GoalStatus.COMPLETE and raw.get("ledger_stamped"):
        # The assessor already stamped a ledger confirmation (method "ledger",
        # run commit + entry ids); a second "assessment" stamp would overwrite
        # that evidence with a weaker record.
        ledger_stamp = _verified_stamp_for_goal(goal, company_dir) or {}
        return {
            "goal_id": goal_id,
            "status": "verified",
            "message": f"Goal {goal_id} verified via ledger.",
            "pr_number": ledger_stamp.get("pr_number"),
            "pr_url": ledger_stamp.get("pr_url"),
            "verified_at": ledger_stamp.get("verified_at"),
        }

    if assessment.status == GoalStatus.COMPLETE:
        return _stamp_goal_verified(
            goal,
            company_dir,
            pr_number=pr_number,
            pr_url=pr_url,
            evidence=None,
            method="assessment",
            progress_percent=assessment.progress_percent,
        )

    # PR-judge fallback: only meaningful for generic-assessed goals (dogfood goals
    # already have a real assessor; a second opinion from a generic LLM judge
    # would just be a lower-quality duplicate of that check).
    if pr_number is not None and not is_dogfood:
        judged = _judge_goal_completion_via_pr(goal, pr_number)
        if judged is not None:
            return _stamp_goal_verified(
                goal,
                company_dir,
                pr_number=pr_number,
                pr_url=pr_url,
                evidence=judged["reason"],
                method="pr_judge",
            )

    return {
        "goal_id": goal_id,
        "status": "not_complete",
        "assessment_status": assessment.status.value,
        "message": f"Goal {goal_id} metric check failed: {assessment.status_reason}",
        "pr_number": pr_number,
        "pr_url": pr_url,
    }


# -----------------------------------------------------------------------------
# Task verdicts (read-only view of the deliverable gate's records)
# -----------------------------------------------------------------------------
#
# Used by strategic_planner's mint brake to spot a streak of judge rejections on
# one goal. Automated goal CREDITING lives in goal_crediting.py, which applies a
# stricter, sha-bound pre-merge rule of its own.

_GATE_LOG_FILE = "state/deliverable_gate.jsonl"
_VERDICT_CACHE_FILE = "state/deliverable_verdicts.json"
_GATE_LOG_MAX_LINES = 20000


def _is_definitive_verdict(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and not record.get("skipped_reason")
        and record.get("error") is None
        and record.get("addresses_task") in (True, False)
    )


def _verdict_is_passing(record: Any) -> bool:
    """Positive, not held, not an error."""
    return bool(
        _is_definitive_verdict(record)
        and record.get("addresses_task") is True
        and not record.get("blocked")
        and not record.get("needs_manual_review")
    )


def latest_task_verdicts(company_dir: Path) -> dict[str, dict]:
    """The latest DEFINITIVE deliverable-judge verdict per task_id.

    Source of truth is ``state/deliverable_gate.jsonl`` (append-only, one line
    per gate decision, chronological). Skip entries and judge errors (API 529,
    unparseable output) are not verdicts and never overwrite one. A task with no
    gate-log line falls back to the ``deliverable_verdicts.json`` cache, which is
    keyed by head sha and carries the task_id.

    Each returned record also carries ``passing`` (see ``_verdict_is_passing``).
    Never raises; unreadable files contribute nothing.
    """
    verdicts: dict[str, dict] = {}
    gate_path = company_dir / _GATE_LOG_FILE
    try:
        if gate_path.exists():
            with open(gate_path, encoding="utf-8") as handle:
                lines = handle.readlines()
            for line in lines[-_GATE_LOG_MAX_LINES:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = record.get("task_id") if isinstance(record, dict) else None
                if task_id and _is_definitive_verdict(record):
                    verdicts[task_id] = record
    except OSError:
        pass

    # Cache fallback. Before #652 the workflow executor judged, cached and
    # failed a rejected PR without ever writing the gate log -- and 60 of 62
    # audited sandbox full-trust merges carried a NEGATIVE verdict on the merged
    # commit. The cache has no timestamps, so when it holds several verdicts
    # for one task (one per head sha) a non-passing one wins: fail closed.
    cache_path = company_dir / _VERDICT_CACHE_FILE
    try:
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cache, dict):
                from_cache: dict[str, dict] = {}
                for record in cache.values():
                    task_id = (
                        record.get("task_id") if isinstance(record, dict) else None
                    )
                    if (
                        not task_id
                        or task_id in verdicts
                        or not _is_definitive_verdict(record)
                    ):
                        continue
                    if task_id not in from_cache or not _verdict_is_passing(record):
                        from_cache[task_id] = record
                verdicts.update(from_cache)
    except (OSError, json.JSONDecodeError):
        pass

    return {
        task_id: {**record, "passing": _verdict_is_passing(record)}
        for task_id, record in verdicts.items()
    }


def get_company_dir() -> Path:
    """Find the company directory."""
    # Try current directory
    cwd = Path.cwd()
    if (cwd / ".company").exists():
        return cwd / ".company"

    # Try to find via company_resolver
    try:
        result = subprocess.run(
            ["uv", "run", str(Path(__file__).parent / "company_resolver.py"), "dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except Exception:
        pass

    return cwd / ".company"


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Goal tracker for strategic planning")
    parser.add_argument(
        "command",
        choices=["assess", "summary", "export", "verify"],
        help="Command to run",
    )
    parser.add_argument("--goal", "-g", help="Specific goal ID (e.g., G5)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--pr", help="PR number (for verify command)")
    parser.add_argument("--pr-url", help="PR URL (for verify command)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Operator-asserted completion for generic-assessed goals (verify command)",
    )
    parser.add_argument(
        "--evidence",
        help="Required justification text when using --confirm (verify command)",
    )

    args = parser.parse_args()

    company_dir = get_company_dir()

    if args.command == "assess":
        if args.goal:
            # Assess specific goal
            goals = parse_goals_from_vision(company_dir / "vision.md")
            goal = next((g for g in goals if g.id == args.goal), None)
            if not goal:
                print(f"Goal {args.goal} not found", file=sys.stderr)
                sys.exit(1)
            assessment = assess_goal(goal, company_dir)
            assessments = [assessment]
        else:
            # Assess all goals
            assessments = assess_all_goals(company_dir)

        if args.json:
            print(json.dumps([a.to_dict() for a in assessments], indent=2))
        else:
            for a in assessments:
                status_emoji = {
                    GoalStatus.COMPLETE: "✓",
                    GoalStatus.ON_TRACK: "→",
                    GoalStatus.AT_RISK: "!",
                    GoalStatus.BLOCKED: "✗",
                    GoalStatus.NOT_STARTED: "○",
                }.get(a.status, "?")
                print(
                    f"{status_emoji} {a.goal_id}: {a.goal_name} — {a.progress_percent}% ({a.status.value})"
                )
                print(f"  {a.status_reason}")
                if a.blockers:
                    print(f"  Blockers: {', '.join(a.blockers)}")
                if a.next_actions:
                    print(f"  Next: {', '.join(a.next_actions)}")
                print()

    elif args.command == "summary":
        assessments = assess_all_goals(company_dir)
        complete = sum(1 for a in assessments if a.status == GoalStatus.COMPLETE)
        on_track = sum(1 for a in assessments if a.status == GoalStatus.ON_TRACK)
        at_risk = sum(1 for a in assessments if a.status == GoalStatus.AT_RISK)
        blocked = sum(1 for a in assessments if a.status == GoalStatus.BLOCKED)

        overall = sum(a.progress_percent for a in assessments) / len(assessments)

        if args.json:
            print(
                json.dumps(
                    {
                        "overall_progress": overall,
                        "complete": complete,
                        "on_track": on_track,
                        "at_risk": at_risk,
                        "blocked": blocked,
                        "total": len(assessments),
                    },
                    indent=2,
                )
            )
        else:
            print(f"Overall Progress: {overall:.0f}%")
            print(f"  ✓ Complete: {complete}")
            print(f"  → On Track: {on_track}")
            print(f"  ! At Risk: {at_risk}")
            print(f"  ✗ Blocked: {blocked}")

    elif args.command == "export":
        assessments = assess_all_goals(company_dir)
        print(json.dumps([a.to_dict() for a in assessments], indent=2))

    elif args.command == "verify":
        if not args.goal:
            print("--goal is required for the verify command", file=sys.stderr)
            sys.exit(1)
        result = verify_goal(
            args.goal,
            company_dir,
            pr_number=args.pr,
            pr_url=getattr(args, "pr_url", None),
            confirm=getattr(args, "confirm", False),
            evidence=getattr(args, "evidence", None),
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "✓" if result["status"] == "verified" else "✗"
            print(f"{icon} {result['goal_id']}: {result['message']}")


if __name__ == "__main__":
    main()
