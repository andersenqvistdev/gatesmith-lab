# /// script
# requires-python = ">=3.10"
# ///
"""Nightly autonomy calibration — delta-only Tier-1 + Tier-2 pass.

Selects only tasks whose task_id cohort date is newer than the last persisted
snapshot, runs Tier-1 (gh merged check) on them, then Tier-2 (adversarial
semantic judgment) on the survivors.  One snapshot is appended to
.company/state/autonomy_audit.json per run.  The work queue is never mutated.

CLI:
    uv run calibrate_nightly.py [--dry-run] [--company-dir DIR]
                                 [--project-root DIR] [--window N]

    --dry-run        Print what would be done; do NOT write to audit or antipatterns.
    --company-dir    Override .company path (default: .company relative to cwd).
    --project-root   Override repo root (default: two levels above this file).
    --window N       Window in days for the local proxy denominator (default: 30).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_HOOKS_DIR = Path(__file__).parent

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import autonomy_metrics as am  # noqa: E402

# ---------------------------------------------------------------------------
# Delta selection
# ---------------------------------------------------------------------------


def _snapshot_cutoff(company_dir: Path) -> str | None:
    """Return YYYYMMDD of the last snapshot's generated_at, or None."""
    snap = am.load_autonomy_audit(company_dir)
    if not snap:
        return None
    ts = snap.get("generated_at") or ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except (ValueError, AttributeError):
        return None


def select_delta(company_dir: Path, cutoff: str | None) -> list[dict[str, Any]]:
    """Return completed-with-PR tasks with task_id cohort date strictly after cutoff.

    cutoff=None (no prior snapshot) returns all completed-with-PR tasks.
    Tasks without a parseable cohort date are excluded from any windowed delta
    to avoid double-judging historical items on every run.
    """
    all_tasks = am.completed_tasks_with_pr(company_dir)
    if cutoff is None:
        return all_tasks
    return [
        t for t in all_tasks if (am.task_cohort_date(t.get("task_id")) or "") > cutoff
    ]


# ---------------------------------------------------------------------------
# Tier-2 semantic judgment (one survivor at a time, fail-closed)
# ---------------------------------------------------------------------------


def _judge_one(survivor: dict[str, Any]) -> dict[str, Any]:
    """Invoke deliverable_judge on one merged PR. Returns a judgment dict.

    On any error the judgment is marked errored=True so it is excluded from
    the phantom count (same convention as calibrate.js / summarize_ground_truth).
    """
    task_id = survivor.get("task_id", "unknown")
    try:
        try:
            from . import deliverable_judge as dj  # type: ignore[attr-defined]
        except ImportError:
            import deliverable_judge as dj  # type: ignore[no-redef]

        pr_number = survivor.get("pr_number") or ""
        verdict = dj.judge_pr_deliverable(
            task_id=task_id,
            title=survivor.get("title") or "",
            description=survivor.get("description") or "",
            pr_number=pr_number,
        )
        addresses = verdict.addresses_task
        if addresses is None:
            return {
                "task_id": task_id,
                "addresses_task": False,
                "confidence": None,
                "reason": verdict.reason or "judge could not produce verdict",
                "errored": True,
            }
        return {
            "task_id": task_id,
            "addresses_task": bool(addresses),
            "confidence": verdict.confidence,
            "reason": verdict.reason or "",
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "addresses_task": False,
            "confidence": None,
            "reason": f"judge exception: {exc}",
            "errored": True,
        }


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_sha(project_root: Path) -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
        )
        return res.stdout.strip() or None if res.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


# ---------------------------------------------------------------------------
# Antipattern helpers
# ---------------------------------------------------------------------------


def _record_phantoms(phantoms: list[dict[str, Any]], project_root: Path) -> int:
    """Record phantom-merge antipatterns. Best-effort; never raises."""
    if not phantoms:
        return 0
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la._record_batch(project_root, phantoms, source="calibrate-nightly")
    except Exception:
        return 0


def _phantoms_from_judgments(
    judgments: list[dict[str, Any]], survivors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build phantom dicts for Tier-2 failures (not errored, addresses_task=False)."""
    by_id = {s.get("task_id"): s for s in survivors}
    out = []
    for j in judgments:
        if j.get("errored") or j.get("addresses_task"):
            continue
        tid = j.get("task_id")
        s = by_id.get(tid) or {}
        out.append(
            {
                "task_id": tid,
                "title": s.get("title") or tid or "",
                "reason": j.get("reason") or "merged PR diff did not address the task",
                "pr": s.get("pr_url"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Main calibration logic
# ---------------------------------------------------------------------------


def run_nightly(
    company_dir: Path,
    project_root: Path,
    *,
    dry_run: bool = False,
    window_days: int = 30,
    judger: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one nightly calibration cycle.

    Returns a summary dict.  Appends a snapshot to autonomy_audit.json and
    records phantom antipatterns unless dry_run=True.

    ``judger`` is injectable for tests: it takes a survivor dict and returns a
    judgment dict {task_id, addresses_task, confidence, reason, errored?}.
    Defaults to _judge_one (real Claude CLI invocation).
    """
    _judge = judger if judger is not None else _judge_one
    cutoff = _snapshot_cutoff(company_dir)
    candidates = select_delta(company_dir, cutoff)
    build_sha = _git_sha(project_root)

    # --- empty-night fast path ---
    if not candidates:
        proxy = am.compute_autonomy(company_dir, window_days=window_days)
        snapshot = am.build_snapshot(
            proxy=proxy,
            ground_truth={"empty_night": True, "cutoff": cutoff},
            phantoms=[],
            build_sha=build_sha,
        )
        if not dry_run:
            am.append_autonomy_audit(company_dir, snapshot)
        return {
            "status": "empty_night",
            "cutoff": cutoff,
            "candidates": 0,
            "tier1_merged": 0,
            "tier2_judged": 0,
            "dry_run": dry_run,
        }

    # --- Tier-1: gh merged check ---
    tier1_results, survivors = am.run_tier1(candidates)

    # --- Tier-2: semantic judgment on survivors ---
    tier2_judgments: list[dict[str, Any]] = []
    for s in survivors:
        tier2_judgments.append(_judge(s))

    phantoms = _phantoms_from_judgments(tier2_judgments, survivors)
    if not dry_run:
        _record_phantoms(phantoms, project_root)

    proxy = am.compute_autonomy(company_dir, window_days=window_days)
    windowed = am.compute_autonomy(company_dir, window_days=window_days)
    gt = am.summarize_ground_truth(
        tier1_results,
        tier2_judgments,
        distinct_tasks_queued=proxy["distinct_tasks_queued"],
        window_days=window_days,
        distinct_tasks_queued_windowed=windowed.get("distinct_tasks_queued"),
    )
    snapshot = am.build_snapshot(
        proxy=proxy,
        ground_truth=gt,
        phantoms=phantoms,
        build_sha=build_sha,
    )

    if not dry_run:
        am.append_autonomy_audit(company_dir, snapshot)

    effective_tier2 = [j for j in tier2_judgments if not j.get("errored")]
    return {
        "status": "ok",
        "cutoff": cutoff,
        "candidates": len(candidates),
        "tier1_merged": len(survivors),
        "tier1_unverified": gt.get("tier1_unverified", 0),
        "tier2_judged": len(effective_tier2),
        "phantoms_found": len(phantoms),
        "dry_run": dry_run,
        "snapshot": snapshot if dry_run else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _flag_str(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _flag_int(argv: list[str], name: str) -> int | None:
    v = _flag_str(argv, name)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            return None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    dry_run = "--dry-run" in argv

    company_dir_str = _flag_str(argv, "--company-dir")
    company_dir = Path(company_dir_str) if company_dir_str else Path(".company")

    project_root_str = _flag_str(argv, "--project-root")
    project_root = (
        Path(project_root_str) if project_root_str else _HOOKS_DIR.parent.parent.parent
    )

    window = _flag_int(argv, "--window") or 30

    result = run_nightly(
        company_dir,
        project_root,
        dry_run=dry_run,
        window_days=window,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
