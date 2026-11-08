#!/usr/bin/env python3
"""Acceptance ledger from CI — the daemon side of ``acceptance.runner: "ci"``.

LEDGER-AS-SHARDED-RUNNER-JOB (operator decision 2026-10-13). webapp's full
ledger took ~45 min serially inside the daemon on the operator's laptop, and a
timed-out daemon run blocked goal credit for 16 h. With ``acceptance.runner``
set to ``"ci"`` in gatesmith.json the scheduled ledger runs instead as the
product's own workflow (``templates/ci/acceptance-ledger.yml``): N shards on
the product's self-hosted runners, each uploading a shard result artifact
(``acceptance_ledger.py run --shard i/N``).

The daemon's ledger sub-step still runs ``acceptance_ledger.py run``; that
command calls ``scheduled_ingest`` here before it would run anything:

* the newest SUCCESSFUL run of the workflow on ``main``, started by
  ``schedule`` or ``workflow_dispatch``, that this project has not ingested
  yet, is downloaded (``gh run download``) and its shard results are merged
  into this project's ``last_run.json`` / ``runs.jsonl`` by
  ``acceptance_ledger.apply_merged_run`` -- exactly what a serial run would
  have written, judged against this project's own history;
* when that run is already ingested and is less than
  ``FALLBACK_INTERVALS`` x ``acceptance.intervalHours`` old, nothing runs;
* when CI has had no fresh result for longer than that (no workflow yet, the
  runners are down, gh is unavailable), the ledger runs locally as before.

GAP-7 invariant -- the run always uses origin/main's checks: a run is
ingested only when GitHub reports it on ``main`` from one of the two events
above, every shard reports that run's head commit, and that commit is an
ancestor of (or equal to) ``origin/main`` here. Anything else is refused and
never folded. The workflow itself runs only on the default branch.

Stdlib only; every path is derived from the project directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_ledger as ledger  # noqa: E402

DEFAULT_WORKFLOW = "acceptance-ledger.yml"
SHARD_ARTIFACT_PATTERN = "acceptance-shard-*"
INGEST_FILENAME = "ci_ingest.json"
ALLOWED_EVENTS = ("schedule", "workflow_dispatch")
DEFAULT_INTERVAL_HOURS = 24.0
# CI has "no fresh result" once its newest result is older than this many
# intervals; the daemon then runs the ledger locally.
FALLBACK_INTERVALS = 2
GH_TIMEOUT_SECONDS = 300
GIT_TIMEOUT_SECONDS = 60

RunGh = Callable[[list[str], Path], "str | None"]
RunGit = Callable[[list[str], Path], int]


@dataclass(frozen=True)
class CiSettings:
    workflow: str = DEFAULT_WORKFLOW
    interval_hours: float = DEFAULT_INTERVAL_HOURS

    @property
    def fresh_seconds(self) -> float:
        return FALLBACK_INTERVALS * self.interval_hours * 3600.0


@dataclass(frozen=True)
class IngestDecision:
    """``action``: "ingested", "up_to_date", "refused" or "fallback_local"."""

    action: str
    reason: str
    exit_code: int = 0
    run_id: int | None = None


def load_settings(base_dir: Path) -> CiSettings:
    """``acceptance.ci.workflow`` and ``acceptance.intervalHours``; defaults
    when absent or malformed."""
    try:
        block = json.loads((base_dir / "gatesmith.json").read_text()).get(
            "acceptance", {}
        )
    except (OSError, ValueError, AttributeError):
        return CiSettings()
    if not isinstance(block, dict):
        return CiSettings()
    ci = block.get("ci") if isinstance(block.get("ci"), dict) else {}
    workflow = ci.get("workflow")
    interval = ledger._positive_seconds(block.get("intervalHours"))
    return CiSettings(
        workflow=workflow
        if isinstance(workflow, str) and workflow.strip()
        else DEFAULT_WORKFLOW,
        interval_hours=interval or DEFAULT_INTERVAL_HOURS,
    )


def _default_gh(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _default_git(args: list[str], cwd: Path) -> int:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_ci_run(
    base_dir: Path, settings: CiSettings, run_gh: RunGh
) -> dict[str, Any] | None:
    """The newest successful ``schedule``/``workflow_dispatch`` run of the
    workflow on ``main``, or None (none, or gh unavailable)."""
    output = run_gh(
        [
            "run",
            "list",
            "--workflow",
            settings.workflow,
            "--branch",
            ledger.MAIN_BRANCH,
            "--status",
            "success",
            "--limit",
            "20",
            "--json",
            "databaseId,headSha,headBranch,event,conclusion,createdAt",
        ],
        base_dir,
    )
    if output is None:
        return None
    try:
        rows = json.loads(output or "[]")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    candidates = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("headBranch") == ledger.MAIN_BRANCH
        and row.get("event") in ALLOWED_EVENTS
        and row.get("conclusion") == "success"
        and isinstance(row.get("databaseId"), int)
        and isinstance(row.get("headSha"), str)
        and _epoch(row.get("createdAt")) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: _epoch(row["createdAt"]) or 0.0)


def commit_on_origin_main(base_dir: Path, sha: str, run_git: RunGit) -> bool:
    """True when ``sha`` is ``origin/main`` or an ancestor of it (after a
    best-effort fetch). False on any doubt: an unprovable commit is refused."""
    run_git(["fetch", "--quiet", "origin", ledger.MAIN_BRANCH], base_dir)
    return (
        run_git(
            ["merge-base", "--is-ancestor", sha, f"origin/{ledger.MAIN_BRANCH}"],
            base_dir,
        )
        == 0
    )


def download_shard_results(
    base_dir: Path, run_id: int, dest: Path, run_gh: RunGh
) -> list[Path]:
    """Every shard result file of ``run_id``'s ``acceptance-shard-*`` artifacts."""
    output = run_gh(
        [
            "run",
            "download",
            str(run_id),
            "--pattern",
            SHARD_ARTIFACT_PATTERN,
            "--dir",
            str(dest),
        ],
        base_dir,
    )
    if output is None:
        return []
    return sorted(path for path in dest.rglob("*.json") if path.is_file())


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def scheduled_ingest(
    base_dir: Path,
    config: ledger.LedgerConfig,
    *,
    now: float | None = None,
    run_gh: RunGh | None = None,
    run_git: RunGit | None = None,
) -> IngestDecision:
    """Ingest CI's newest ledger result, report it current, or ask for a local run.

    Never raises: anything unexpected is a reason to fall back to a local run
    once CI's result is stale, never a reason to fold an unverified one.
    """
    now = time.time() if now is None else now
    run_gh = run_gh or _default_gh
    run_git = run_git or _default_git
    settings = load_settings(base_dir)
    state_path = config.state_dir / INGEST_FILENAME
    state = _read_state(state_path)
    window = settings.fresh_seconds
    latest = latest_ci_run(base_dir, settings, run_gh)

    def stale(newest: float | None, why: str) -> IngestDecision:
        if newest is None or now - newest > window:
            age = "never" if newest is None else f"{(now - newest) / 3600:.1f}h ago"
            return IngestDecision(
                "fallback_local",
                f"{why}; CI's newest ledger result is from {age}, over "
                f"{FALLBACK_INTERVALS} x intervalHours ({window / 3600:.0f}h) "
                "— running the ledger locally",
            )
        return IngestDecision(
            "up_to_date",
            f"{why}; CI's newest ledger result is {(now - newest) / 3600:.1f}h old",
        )

    known = ledger._positive_seconds(state.get("run_created_at"))
    if latest is None:
        return stale(known, f"no successful {settings.workflow} run on main found")
    run_id = latest["databaseId"]
    created = _epoch(latest["createdAt"])
    if run_id == state.get("run_id"):
        return stale(created, f"CI run {run_id} is already ingested")
    if run_id == state.get("refused_run_id"):
        return stale(known, f"CI run {run_id} was refused before")
    if created is not None and now - created > window:
        return stale(created, f"CI run {run_id} is new here but stale")

    def refuse(why: str) -> IngestDecision:
        state["refused_run_id"] = run_id
        state["refused_reason"] = why
        try:
            ledger._write_json_atomic(state_path, state)
        except OSError:
            pass
        fallback = stale(known, f"CI run {run_id} refused: {why}")
        if fallback.action == "fallback_local":
            return fallback
        return IngestDecision("refused", why, exit_code=1, run_id=run_id)

    head_sha = latest["headSha"]
    if not commit_on_origin_main(base_dir, head_sha, run_git):
        return refuse(f"its commit {head_sha[:12]} is not on origin/main here")
    with tempfile.TemporaryDirectory(prefix="acceptance-ci-") as tmp:
        files = download_shard_results(base_dir, run_id, Path(tmp), run_gh)
        if not files:
            return refuse("it has no acceptance-shard-* artifacts to download")
        try:
            payloads = ledger.load_shard_results(files)
            commits = {p.get("commit") for p in payloads}
            if commits != {head_sha}:
                return refuse(
                    f"its shards ran on {sorted(map(str, commits))}, not the "
                    f"run's head {head_sha[:12]}"
                )
            code = ledger.apply_merged_run(
                config,
                payloads,
                source=f"ci:{run_id}",
                branch_override=ledger.MAIN_BRANCH,
            )
        except ledger.LedgerError as exc:
            return refuse(str(exc))
    state.update(
        {
            "run_id": run_id,
            "head_sha": head_sha,
            "run_created_at": created,
            "ingested_at": now,
            "shard_files": len(files),
        }
    )
    try:
        ledger._write_json_atomic(state_path, state)
    except OSError:
        pass
    return IngestDecision(
        "ingested",
        f"merged {len(files)} shard result(s) of CI run {run_id} ({head_sha[:12]})",
        exit_code=code,
        run_id=run_id,
    )
