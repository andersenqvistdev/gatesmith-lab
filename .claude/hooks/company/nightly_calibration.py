#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Nightly calibration module for the Gatesmith autonomy pipeline.

Runs Tier-1 (gh merged-check) against all completed-with-PR tasks in the
configured window, then Tier-2 semantic judgment only on survivors that have
not been judged in a previous nightly run.  Appends a snapshot to
``.company/state/autonomy_audit.json`` and records semantic phantoms in
``.company/knowledge/anti_patterns.json``.

Delta selection: survivors are compared against a sidecar file
``.company/state/nightly_calibration_judgments.json`` that accumulates all
previously-made Tier-2 judgments.  A survivor is "new" if its task_id is not
yet in the sidecar, or if its prior judgment was errored (retried next night).

The snapshot built on an empty night (no new survivors) is still appended so
the trend ring-buffer stays fresh.

Design constraints:
- Never mutates the work queue.
- Fails closed on gh errors (tier1 already handles this; tier2 judges that
  err are marked errored=True → excluded, not counted as phantoms).
- The judgment logic reuses deliverable_judge.judge_pr_deliverable() with an
  explicit ``enabled=True`` config so a disabled deliverable-gate setting does
  not suppress nightly calibration.
- Subprocess env follows deliverable_judge._run_judge_cli()'s
  agent_providers.prepare_environment() allowlist (same module; no extra
  handling needed here).
- Injectable ``_tier1_runner`` and ``_judge_fn`` for test isolation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import autonomy_metrics as am
    from . import deliverable_judge as dj
    from . import learned_antipatterns as la
except ImportError:
    _HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HERE))
    import autonomy_metrics as am
    import deliverable_judge as dj
    import learned_antipatterns as la

_JUDGMENTS_FILE = "state/nightly_calibration_judgments.json"
DEFAULT_WINDOW = 30

# Config passed to judge_pr_deliverable: bypass the gate-enabled flag so a
# disabled deliverable gate doesn't suppress nightly calibration.
_NIGHTLY_JUDGE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "onJudgeError": "block",
    "confidenceThreshold": 0.7,
    "timeoutSeconds": 300,
}


# ---------------------------------------------------------------------------
# Sidecar — accumulated tier2 judgments
# ---------------------------------------------------------------------------


def load_sidecar(company_dir: Path) -> dict[str, dict]:
    """Return accumulated tier2 judgments keyed by task_id.

    Gracefully returns an empty dict on missing / corrupt file.
    """
    path = company_dir / _JUDGMENTS_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("judgments"), dict):
            return raw["judgments"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_sidecar(company_dir: Path, judgments: dict[str, dict]) -> None:
    """Atomically overwrite the sidecar with updated judgments."""
    path = company_dir / _JUDGMENTS_FILE
    data = {"version": 1, "judgments": judgments}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ncj_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Tier2 judgment helpers
# ---------------------------------------------------------------------------


def _verdict_to_judgment(verdict: dj.DeliverableVerdict) -> dict[str, Any]:
    """Normalize a DeliverableVerdict to the dict shape expected by
    ``summarize_ground_truth``:
    ``{task_id, addresses_task, confidence, reason, judged_at, errored?}``

    ``addresses_task is None`` (judge error / gate-disabled skip) maps to
    ``errored=True`` so ``summarize_ground_truth`` excludes it rather than
    counting it as a phantom.
    """
    j: dict[str, Any] = {
        "task_id": verdict.task_id,
        "addresses_task": verdict.addresses_task,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    if verdict.addresses_task is None:
        j["errored"] = True
    return j


def _default_judge_fn(
    task_id: str,
    title: str,
    description: str,
    pr_number: str | int,
    **kwargs: Any,
) -> dj.DeliverableVerdict:
    """Call the real deliverable_judge with nightly-specific config."""
    return dj.judge_pr_deliverable(
        task_id,
        title,
        description,
        pr_number,
        config=_NIGHTLY_JUDGE_CONFIG,
        use_cache=True,
    )


def judge_survivors(
    new_survivors: list[dict],
    *,
    judge_fn: Callable[..., dj.DeliverableVerdict] | None = None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Run tier2 semantic judgment on a list of new survivors.

    Returns a list of judgment dicts (same shape as the sidecar entries).
    Never raises; individual judgment failures produce an ``errored=True``
    entry so they are retried next night rather than counted as phantoms.
    """
    fn = judge_fn or _default_judge_fn
    judgments: list[dict[str, Any]] = []
    for i, s in enumerate(new_survivors, 1):
        task_id = s.get("task_id", "?")
        pr_number = s.get("pr_number", "")
        if verbose:
            print(
                f"  [{i}/{len(new_survivors)}] judging {task_id} PR#{pr_number}",
                file=sys.stderr,
            )
        try:
            verdict = fn(
                task_id,
                s.get("title") or "",
                s.get("description") or "",
                pr_number,
            )
            j = _verdict_to_judgment(verdict)
        except Exception as exc:
            if verbose:
                print(f"  [warn] judge exception for {task_id}: {exc}", file=sys.stderr)
            j = {
                "task_id": task_id,
                "addresses_task": None,
                "confidence": None,
                "reason": f"nightly judge exception: {exc}",
                "judged_at": datetime.now(timezone.utc).isoformat(),
                "errored": True,
            }
        judgments.append(j)
    return judgments


# ---------------------------------------------------------------------------
# Delta selection
# ---------------------------------------------------------------------------


def select_new_survivors(
    all_survivors: list[dict],
    sidecar: dict[str, dict],
) -> list[dict]:
    """Return survivors not yet in the sidecar, or whose prior judgment errored.

    Errored entries are retried so a transient rate-limit failure one night does
    not permanently exclude a task from calibration.
    """
    return [
        s
        for s in all_survivors
        if s.get("task_id") not in sidecar or sidecar[s["task_id"]].get("errored")
    ]


# ---------------------------------------------------------------------------
# Phantom list assembly
# ---------------------------------------------------------------------------


def build_phantom_list(
    tier1_results: list[dict],
    all_judgments: list[dict],
    survivor_pr_map: dict[str, str],
) -> list[dict]:
    """Assemble the combined phantom list from tier1 + tier2.

    Tier-1 phantoms: tasks whose PR is not MERGED with a real diff.
    Tier-2 phantoms: tasks where the judge confirmed the diff does NOT address
    the task (``addresses_task=False``, not errored).

    A Tier-1 result flagged ``unverified`` is NOT a phantom — ``gh`` never
    answered, so nothing is known about that PR. Those are reported separately
    by :func:`build_unverified_list`.

    ``survivor_pr_map``: task_id → pr_url for tier-2 phantom URL lookup.
    """
    tier1_phantoms = [
        {
            "task_id": v["task_id"],
            "pr_url": v.get("pr_url", ""),
            "reason": v["reason"],
        }
        for v in tier1_results
        if not v.get("merged") and not v.get("unverified") and v.get("task_id")
    ]
    tier2_phantoms = [
        {
            "task_id": j["task_id"],
            "pr_url": survivor_pr_map.get(j["task_id"], ""),
            "reason": j["reason"],
        }
        for j in all_judgments
        if j.get("addresses_task") is False and not j.get("errored")
    ]
    return tier1_phantoms + tier2_phantoms


def build_unverified_list(tier1_results: list[dict]) -> list[dict]:
    """Tasks whose Tier-1 ``gh`` check never reached a verdict.

    Kept out of the phantom list and out of the trust maths — they record that
    the measurement failed, not that the work did.
    """
    return [
        {
            "task_id": v["task_id"],
            "pr_url": v.get("pr_url", ""),
            "reason": v.get("reason", ""),
            "attempts": v.get("attempts", 0),
        }
        for v in tier1_results
        if v.get("unverified") and v.get("task_id")
    ]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_nightly(
    company_dir: Path,
    *,
    project_root: Path | None = None,
    window: int = DEFAULT_WINDOW,
    dry_run: bool = False,
    verbose: bool = False,
    _tier1_runner: Callable[..., Any] = subprocess.run,
    _tier1_sleeper: Callable[[float], None] = time.sleep,
    _judge_fn: Callable[..., dj.DeliverableVerdict] | None = None,
) -> dict[str, Any]:
    """Execute one nightly calibration cycle.

    Returns a summary dict. Raises on fatal I/O errors (company_dir not
    writable, gh not authenticated, etc.) so launchd can capture the failure.

    Parameters
    ----------
    company_dir:
        Path to the ``.company/`` directory.  Never defaults to cwd.
    project_root:
        Repo root for antipattern recording. Defaults to the parent of the
        ``.claude/hooks/company/`` directory (i.e. the project root at import
        time).
    window:
        Look-back window in days for ``completed_tasks_with_pr``.
    dry_run:
        Print what would happen without writing any state (audit file,
        sidecar, antipatterns).
    _tier1_runner / _tier1_sleeper / _judge_fn:
        Injectable callables for test isolation.  ``_tier1_runner`` replaces
        ``subprocess.run`` inside ``am.run_tier1``; ``_tier1_sleeper`` replaces
        the retry back-off so tests exercising the transport-failure path do not
        actually wait; ``_judge_fn`` replaces
        ``deliverable_judge.judge_pr_deliverable``.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent

    def _log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    # 1. Load accumulated judgments from sidecar.
    sidecar = load_sidecar(company_dir)
    _log(f"[nightly] sidecar: {len(sidecar)} accumulated judgments")

    # 2. Tier-1: deterministic gh merged-check over all window-day tasks.
    tasks = am.completed_tasks_with_pr(company_dir, window_days=window)
    _log(f"[nightly] tier1 candidates: {len(tasks)}")
    tier1_results, all_survivors = am.run_tier1(
        tasks, runner=_tier1_runner, sleeper=_tier1_sleeper
    )
    _log(
        f"[nightly] tier1: {len(tier1_results)} checked, "
        f"{len(all_survivors)} merged survivors"
    )

    # Build pr_url lookup for phantom list assembly.
    survivor_pr_map: dict[str, str] = {
        s["task_id"]: s.get("pr_url", "") for s in all_survivors
    }

    # 3. Delta: survivors not yet (successfully) judged.
    new_survivors = select_new_survivors(all_survivors, sidecar)
    already_judged = len(all_survivors) - len(new_survivors)
    _log(
        f"[nightly] new survivors for tier2: {len(new_survivors)} "
        f"({already_judged} already judged)"
    )

    empty_night = not new_survivors
    if empty_night:
        _log("[nightly] empty night — no new survivors for tier2")

    # 4. Tier-2: semantic judgment on new survivors only.
    new_judgments: list[dict[str, Any]] = []
    if new_survivors and not dry_run:
        new_judgments = judge_survivors(
            new_survivors,
            judge_fn=_judge_fn,
            verbose=verbose,
        )
    elif new_survivors and dry_run:
        _log(f"  [dry-run] would judge {len(new_survivors)} survivors")

    # 5. Merge new non-errored judgments into sidecar.
    for j in new_judgments:
        if not j.get("errored"):
            sidecar[j["task_id"]] = j

    # 6. Build the full judgment pool: sidecar entries that are relevant to the
    #    current survivor set + new judgments (errored or not).
    survivor_ids = {s["task_id"] for s in all_survivors}
    historical_judgments = [j for tid, j in sidecar.items() if tid in survivor_ids]
    # Include all new judgments (summarize_ground_truth handles errored ones).
    all_judgments = historical_judgments + [
        j for j in new_judgments if j["task_id"] not in sidecar
    ]

    # 7. Compute local proxy metrics (no network).
    proxy = am.compute_autonomy(company_dir, window_days=window)

    # 8. Build ground_truth from combined tier1 + accumulated judgments.
    ground_truth = am.summarize_ground_truth(
        tier1_results,
        all_judgments,
        distinct_tasks_queued=proxy["distinct_tasks_queued"],
        window_days=window,
        distinct_tasks_queued_windowed=proxy.get("distinct_tasks_queued"),
    )

    # 9. Assemble phantom list (unverified results are excluded — see below).
    all_phantoms = build_phantom_list(tier1_results, all_judgments, survivor_pr_map)
    unverified = build_unverified_list(tier1_results)
    if unverified:
        _log(
            f"[nightly] {len(unverified)} task(s) unverifiable (gh never answered) "
            f"— excluded from the trust maths, not counted as phantoms"
        )

    # 10. Build snapshot.
    build_sha = _git_head_sha()
    snapshot = am.build_snapshot(
        proxy, ground_truth, all_phantoms, build_sha=build_sha, unverified=unverified
    )

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "empty_night": empty_night,
        "tasks_checked": len(tasks),
        "tier1_merged": ground_truth["tier1_merged"],
        "tier1_phantom": ground_truth["tier1_phantom"],
        "tier1_unverified": ground_truth.get("tier1_unverified", 0),
        "degraded": ground_truth.get("degraded", False),
        "new_survivors": len(new_survivors),
        "new_judgments": len(new_judgments),
        "errored_judgments": sum(1 for j in new_judgments if j.get("errored")),
        "tier2_addresses_task": ground_truth.get("tier2_addresses_task", 0),
        "trust_score": ground_truth["trust_score"],
        "phantom_rate": ground_truth["phantom_rate"],
        "build_sha": build_sha,
        "snapshot_generated_at": snapshot["generated_at"],
    }

    if dry_run:
        return summary

    # 11. Persist sidecar (updated with new non-errored judgments).
    save_sidecar(company_dir, sidecar)

    # 12. Append snapshot to autonomy_audit.json.
    am.append_autonomy_audit(company_dir, snapshot)

    # 13. Record new tier-2 semantic phantoms in the antipattern learner.
    new_tier2_phantoms = [
        {
            "task_id": j["task_id"],
            "reason": j["reason"],
            "pr": survivor_pr_map.get(j["task_id"], ""),
        }
        for j in new_judgments
        if j.get("addresses_task") is False and not j.get("errored")
    ]
    if new_tier2_phantoms:
        la._record_batch(project_root, new_tier2_phantoms, "nightly-calibrate")

    return summary


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


def _git_head_sha() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# CLI (called by bin/gatesmith-calibrate-nightly)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dry_run": False,
        "window": DEFAULT_WINDOW,
        "company_dir": None,
        "project_root": None,
        "verbose": False,
        "help": False,
    }
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            result["help"] = True
        elif tok == "--dry-run":
            result["dry_run"] = True
        elif tok == "--verbose":
            result["verbose"] = True
        elif tok == "--window":
            i += 1
            if i >= len(argv):
                print("error: --window requires an argument", file=sys.stderr)
                sys.exit(2)
            try:
                result["window"] = int(argv[i])
            except ValueError:
                print("error: --window value must be an integer", file=sys.stderr)
                sys.exit(2)
        elif tok == "--company-dir":
            i += 1
            if i >= len(argv):
                print("error: --company-dir requires an argument", file=sys.stderr)
                sys.exit(2)
            result["company_dir"] = Path(argv[i])
        elif tok == "--project-root":
            i += 1
            if i >= len(argv):
                print("error: --project-root requires an argument", file=sys.stderr)
                sys.exit(2)
            result["project_root"] = Path(argv[i])
        else:
            print(f"error: unknown argument {tok!r}", file=sys.stderr)
            sys.exit(2)
        i += 1
    return result


_USAGE = """\
Usage: gatesmith-calibrate-nightly [OPTIONS]

Run nightly calibration: Tier-1 gh merged-check + Tier-2 semantic judgment
on new merged PRs since the last snapshot.  Appends to autonomy_audit.json.

Options:
  --dry-run          Print what would happen; do not write any state
  --window N         Look-back window in days (default: 30)
  --company-dir DIR  Override .company/ directory location
  --project-root DIR Project root for antipattern recording
  --verbose          Print progress to stderr
  -h, --help         Show this message and exit
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(argv)

    if args["help"]:
        print(_USAGE)
        return 0

    company_dir = args["company_dir"]
    if company_dir is None:
        company_dir = Path(".company")

    if not company_dir.exists():
        print(
            f"error: company directory not found: {company_dir}\n"
            "Use --company-dir to specify the path.",
            file=sys.stderr,
        )
        return 1

    try:
        summary = run_nightly(
            company_dir,
            project_root=args["project_root"],
            window=args["window"],
            dry_run=args["dry_run"],
            verbose=args["verbose"] or args["dry_run"],
        )
    except Exception as exc:
        print(f"fatal: nightly calibration failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
