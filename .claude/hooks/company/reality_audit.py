#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
reality_audit — Deep nightly audit for silent-wrongness (W1-P1, 2026-07-17).

The platform rarely crashes; it confidently does the wrong thing quietly
until a human audits it by hand. This module mechanizes that audit so it can
run headless, nightly, alongside the calibration LaunchAgent.

Checks (each reports PASS/WARN/FAIL/SKIP with one-line evidence):
  a. CONFIG-VS-LOADER      — which config file governs autonomy.autoMerge and
                             whether it can be trusted (forge_canary's
                             config-governing check, governing_config.py).
  b. STATE-VS-GITHUB       — blocked/pending queue tasks that are ghosts of
                             already-merged PRs; pr_open tasks whose PR closed.
  c. ASSESSMENT-VS-REALITY — coverage.json freshness vs HEAD commit time;
                             vision.md DEFAULT_GOALS fallback detection.
  d. RUNTIME               — daemon pid file vs live process; LaunchAgent
                             loaded state.
  e. CWD-SKELETON SWEEP    — stray .company directories outside project root.

This module is audit-only: it never mutates the work queue, vision.md, or
any other production file (that is autonomy_metrics.reconcile's job).

Usage:
  uv run reality_audit.py [--json] [--project-root PATH]

Exit codes:
  0 — all checks passed (no FAILs)
  1 — one or more checks FAILED
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]
Runner = Callable[..., Any]

_GHOST_LANES = ("blocked", "pending")

# Directories that legitimately contain their own nested trees (venvs, git
# internals, node modules) — never descend into these when sweeping for
# stray .company skeletons.
_SKELETON_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "site-packages",
}


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class AuditResult:
    name: str
    status: Status
    detail: str
    remedy: str = ""


@dataclass
class AuditConfig:
    """All paths anchored to base_dir — no cwd-relative defaults.

    Per CLAUDE.md's test-isolation rule: a single base_dir override (e.g.
    tmp_path in tests) re-anchors every path at once, so a fixture can never
    accidentally leak real production state.
    """

    base_dir: Path
    project_root: Path
    company_dir: Path
    forge_config: Path
    legacy_config: Path
    vision: Path
    coverage_json: Path
    work_queue: Path
    pid_file: Path
    launchagent_dir: Path
    report_path: Path


def make_config(
    base_dir: Path | None = None,
    *,
    home_dir: Path | None = None,
) -> AuditConfig:
    root = base_dir or Path.cwd()
    home = home_dir or Path.home()
    company_dir = root / ".company"
    forge_config = root / "gatesmith.json"

    # The daemon honors config overrides for its pid file (daemon.pidFile,
    # or autonomy.continuousOperation.daemon.pidFile) — resolve it the way
    # the daemon does, or the runtime check watches a path the production
    # daemon never writes (production sets daemon.pidFile=.company/daemon.pid
    # while the DaemonConfig default is .company/runtime/daemon.pid).
    pid_file = company_dir / "runtime" / "daemon.pid"
    try:
        cfg_data = json.loads(forge_config.read_text(encoding="utf-8"))
        override = cfg_data.get("daemon", {}).get("pidFile") or (
            cfg_data.get("autonomy", {})
            .get("continuousOperation", {})
            .get("daemon", {})
            .get("pidFile")
        )
        if override:
            override_path = Path(override)
            pid_file = (
                override_path if override_path.is_absolute() else root / override_path
            )
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    return AuditConfig(
        base_dir=root,
        project_root=root,
        company_dir=company_dir,
        forge_config=forge_config,
        legacy_config=root / ".claude" / "gatesmith.json",
        vision=company_dir / "vision.md",
        coverage_json=root / "coverage.json",
        work_queue=company_dir / "state" / "work_queue.json",
        pid_file=pid_file,
        launchagent_dir=home / "Library" / "LaunchAgents",
        report_path=company_dir / "state" / "reality_audit_report.json",
    )


# ── (a) CONFIG-VS-LOADER ──────────────────────────────────────────────────────


def check_config_vs_loader(cfg: AuditConfig) -> list[AuditResult]:
    """Report the file that governs ``autonomy.autoMerge`` (forge_canary's
    config-governing check). Since governing_config.py, a root value that
    differs from the template is the root governing, not drift; what still
    fails is a governing answer that cannot be trusted."""
    try:
        import forge_canary
    except ImportError as exc:
        return [
            AuditResult("config-vs-loader", "SKIP", f"forge_canary unavailable: {exc}")
        ]
    stage = forge_canary.check_config_governing(cfg.project_root)
    return [AuditResult("config-vs-loader:governing", stage.status, stage.detail)]


# ── (b) STATE-VS-GITHUB ───────────────────────────────────────────────────────


def _load_queue(cfg: AuditConfig) -> dict[str, Any]:
    if not cfg.work_queue.exists():
        return {}
    try:
        data = json.loads(cfg.work_queue.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _search_merged_pr_for_task(
    task_id: str, *, runner: Runner, timeout: int = 30
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (pr, error) for the first merged PR whose title/body mentions task_id.

    ``error`` is None only when gh was actually, successfully queried — a
    non-None error means the check is INCONCLUSIVE (gh down/timeout/malformed
    output), which the caller must surface as WARN rather than silently
    treating a broken gh call as "no ghost found" (the exact silent-pass
    failure mode this module exists to catch — see PR #248).
    """
    try:
        r = runner(
            [
                "gh",
                "pr",
                "list",
                "--search",
                task_id,
                "--state",
                "merged",
                "--json",
                "number,title,url,mergedAt",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"gh error: {type(exc).__name__}"
    if getattr(r, "returncode", 1) != 0:
        return None, "gh nonzero exit"
    stdout = (r.stdout or "").strip()
    try:
        prs = json.loads(stdout) if stdout else []
    except json.JSONDecodeError:
        return None, "gh non-json output"
    return (prs[0] if prs else None), None


def _pin_runner_cwd(runner: Runner, project_root: Path) -> Runner:
    """gh resolves the repository from the process cwd — pin every gh call
    to project_root so auditing repo A from a shell parked in repo B cannot
    silently search the wrong repo and report a false 'no ghosts' PASS."""

    def pinned(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cwd", str(project_root))
        return runner(*args, **kwargs)

    return pinned


def check_state_vs_github(
    cfg: AuditConfig, *, runner: Runner = subprocess.run, timeout: int = 30
) -> list[AuditResult]:
    queue = _load_queue(cfg)
    if not queue:
        return [AuditResult("state-vs-github", "SKIP", "no work_queue.json found")]
    runner = _pin_runner_cwd(runner, cfg.project_root)

    results: list[AuditResult] = []
    checked = 0
    unverified = 0

    for lane in _GHOST_LANES:
        for task in queue.get(lane, []) or []:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            if not task_id:
                continue
            checked += 1
            pr, error = _search_merged_pr_for_task(
                task_id, runner=runner, timeout=timeout
            )
            if pr:
                results.append(
                    AuditResult(
                        f"state-vs-github:ghost:{task_id}",
                        "FAIL",
                        f"{lane} task {task_id} is a ghost of already-merged "
                        f"PR #{pr.get('number')} ({pr.get('url')})",
                        f"Remove {task_id} from the {lane} lane or run "
                        "autonomy_metrics reconcile",
                    )
                )
            elif error:
                unverified += 1
                results.append(
                    AuditResult(
                        f"state-vs-github:unverified:{task_id}",
                        "WARN",
                        f"could not verify {lane} task {task_id} against GitHub — {error}",
                    )
                )

    try:
        import autonomy_metrics as am
    except ImportError as exc:
        results.append(
            AuditResult(
                "state-vs-github:pr-open",
                "SKIP",
                f"autonomy_metrics unavailable: {exc}",
            )
        )
        am = None

    if am is not None:
        for task in queue.get("pr_open", []) or []:
            if not isinstance(task, dict):
                continue
            pr_url = task.get("pr_url")
            task_id = task.get("task_id")
            if not pr_url:
                continue
            v = am.verify_task_merged(
                pr_url, task_id=task_id, timeout=timeout, runner=runner
            )
            state = v.get("state")
            # gh reports OPEN / CLOSED / MERGED — anything not OPEN means
            # the pr_open lane is stale (a merged-but-never-advanced task
            # was invisible under a CLOSED-only comparison).
            if state and state != "OPEN":
                reason = (
                    "merged but never advanced"
                    if v.get("merged") or state == "MERGED"
                    else "closed unmerged"
                )
                results.append(
                    AuditResult(
                        f"state-vs-github:stale:{task_id}",
                        "FAIL",
                        f"pr_open task {task_id} PR is {state} ({reason}) — {pr_url}",
                        "Run autonomy_metrics reconcile_queue_closed_unmerged "
                        "to advance/reset it",
                    )
                )
            elif state is None:
                results.append(
                    AuditResult(
                        f"state-vs-github:stale:{task_id}",
                        "WARN",
                        f"could not verify pr_open task {task_id} — {v.get('reason')}",
                    )
                )

    if not any(r.status == "FAIL" for r in results):
        if unverified:
            results.append(
                AuditResult(
                    "state-vs-github",
                    "WARN",
                    f"{checked} blocked/pending task(s) checked, {unverified} "
                    "could not be verified against GitHub (gh unavailable) — "
                    "no ghosts confirmed among the rest",
                )
            )
        else:
            results.append(
                AuditResult(
                    "state-vs-github",
                    "PASS",
                    f"{checked} blocked/pending task(s) checked, no ghosts found; "
                    "pr_open lane consistent",
                )
            )
    return results


# ── (c) ASSESSMENT-VS-REALITY ─────────────────────────────────────────────────


def _git_head_commit_time(
    project_root: Path, *, runner: Runner, timeout: int = 10
) -> int | None:
    try:
        r = runner(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        return int((r.stdout or "").strip())
    except ValueError:
        return None


def _check_coverage_freshness(cfg: AuditConfig, *, runner: Runner) -> AuditResult:
    if not cfg.coverage_json.exists():
        return AuditResult(
            "assessment-vs-reality:coverage-freshness", "SKIP", "no coverage.json found"
        )

    head_ct = _git_head_commit_time(cfg.project_root, runner=runner)
    mtime = cfg.coverage_json.stat().st_mtime
    if head_ct is None:
        return AuditResult(
            "assessment-vs-reality:coverage-freshness",
            "WARN",
            "could not determine HEAD commit time — cannot verify freshness",
        )
    if mtime < head_ct:
        age = int(head_ct - mtime)
        return AuditResult(
            "assessment-vs-reality:coverage-freshness",
            "FAIL",
            f"coverage.json is {age}s older than HEAD commit — stale data "
            "must never be trusted as current",
            "Re-run `uv run pytest --cov=. --cov-report=json` to regenerate coverage.json",
        )
    return AuditResult(
        "assessment-vs-reality:coverage-freshness",
        "PASS",
        "coverage.json is newer than HEAD commit",
    )


def _check_vision_fallback(cfg: AuditConfig) -> AuditResult:
    try:
        import goal_tracker
    except ImportError as exc:
        return AuditResult(
            "assessment-vs-reality:vision-fallback",
            "SKIP",
            f"goal_tracker unavailable: {exc}",
        )

    if not cfg.vision.exists():
        return AuditResult(
            "assessment-vs-reality:vision-fallback",
            "FAIL",
            "vision.md missing — goals silently fall back to DEFAULT_GOALS",
            "Run /company-bootstrap or /company-init to found the company",
        )

    text = cfg.vision.read_text(encoding="utf-8")
    has_active_period = bool(
        re.search(r"###\s+Period:.*\[status:\s*active\]", text, re.IGNORECASE)
    )

    parsed = goal_tracker.parse_goals_from_vision(cfg.vision)

    # Every silent-wrongness exit in parse_goals_from_vision returns the
    # module-level DEFAULT_GOALS list itself, so identity is the exact
    # detector — it also catches the case where an active Period header
    # EXISTS but the goal table under it is unparseable (an ID-set or
    # header-regex comparison false-PASSed exactly that shape). An empty
    # parse is the other zero-goals exit.
    fell_back = parsed is goal_tracker.DEFAULT_GOALS
    if fell_back or not parsed:
        reason = (
            "goals silently fell back to DEFAULT_GOALS"
            if fell_back
            else "zero goals parsed (no recognizable goal table)"
        )
        if has_active_period:
            detail = (
                "vision.md has an active Period header but its goal table "
                f"did not parse — {reason}"
            )
            remedy = (
                "Fix the goal table under the active Period header in "
                "vision.md (machine-readable markdown table required)"
            )
        else:
            detail = f"vision.md lacks an active Period header — {reason}"
            remedy = (
                "Add `### Period: <name> [status: active]` above the goal "
                "table in vision.md"
            )
        return AuditResult(
            "assessment-vs-reality:vision-fallback", "FAIL", detail, remedy
        )
    return AuditResult(
        "assessment-vs-reality:vision-fallback",
        "PASS",
        f"{len(parsed)} goal(s) parsed from vision.md active period",
    )


def check_assessment_vs_reality(
    cfg: AuditConfig, *, runner: Runner = subprocess.run
) -> list[AuditResult]:
    return [
        _check_coverage_freshness(cfg, runner=runner),
        _check_vision_fallback(cfg),
    ]


# ── (d) RUNTIME ────────────────────────────────────────────────────────────────


def _check_pid(cfg: AuditConfig) -> AuditResult:
    if not cfg.pid_file.exists():
        return AuditResult(
            "runtime:pid",
            "WARN",
            "no daemon.pid file found — daemon may not be running",
        )
    try:
        data = json.loads(cfg.pid_file.read_text(encoding="utf-8"))
        pid = data.get("pid") if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        pid = None
    if not isinstance(pid, int):
        return AuditResult("runtime:pid", "WARN", "daemon.pid unreadable or malformed")

    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True  # process exists, just owned by someone else

    if alive:
        return AuditResult("runtime:pid", "PASS", f"daemon.pid={pid} is alive")
    return AuditResult(
        "runtime:pid",
        "FAIL",
        f"daemon.pid={pid} is stale — process not running",
        "Remove the stale pid file or restart the daemon",
    )


def _check_launchagent(cfg: AuditConfig, *, runner: Runner) -> AuditResult:
    try:
        import company_resolver
    except ImportError as exc:
        return AuditResult(
            "runtime:launchagent", "SKIP", f"company_resolver unavailable: {exc}"
        )

    project_id = company_resolver.get_project_id(cfg.project_root)
    plist_path = cfg.launchagent_dir / f"dev.gatesmith.daemon.{project_id}.plist"
    if not plist_path.exists():
        return AuditResult(
            "runtime:launchagent", "WARN", f"no LaunchAgent plist found at {plist_path}"
        )

    try:
        r = runner(["launchctl", "list"], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return AuditResult(
            "runtime:launchagent", "WARN", f"launchctl unavailable: {exc}"
        )

    loaded = f"dev.gatesmith.daemon.{project_id}" in (r.stdout or "")
    if loaded:
        return AuditResult(
            "runtime:launchagent",
            "PASS",
            f"LaunchAgent dev.gatesmith.daemon.{project_id} is loaded",
        )
    return AuditResult(
        "runtime:launchagent",
        "FAIL",
        f"LaunchAgent plist exists at {plist_path} but is not loaded",
        f"launchctl load {plist_path}",
    )


def check_runtime(
    cfg: AuditConfig, *, runner: Runner = subprocess.run
) -> list[AuditResult]:
    return [_check_pid(cfg), _check_launchagent(cfg, runner=runner)]


# ── (e) CWD-SKELETON SWEEP ─────────────────────────────────────────────────────


def _find_stray_company_dirs(project_root: Path) -> list[Path]:
    """Find `.company` directories anywhere under project_root other than the
    canonical one at the root itself.

    Found 2026-07-17: bin/.company containing an empty runtime queue.lock —
    some tool resolved `.company` relative to cwd (bare `Path(".company")`
    fallback, e.g. rejection_viewer.py, approval_viewer.py,
    strategic_planner.py) instead of company_resolver.get_company_dir(), and
    silently created a skeleton wherever it happened to be invoked from.
    """
    canonical = (project_root / ".company").resolve()
    strays: list[Path] = []
    # os.walk (not rglob) so excluded dirs (.git, node_modules, venvs) are
    # pruned from the walk itself rather than filtered from results after —
    # rglob would still fully descend into e.g. a large .git or site-packages
    # tree before discarding the match, making the sweep pathologically slow
    # on a big repo. followlinks=False (the default) so a symlink loop cannot
    # turn this into an unbounded walk either.
    for dirpath, dirnames, _filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in _SKELETON_EXCLUDE_DIRS]
        if ".company" not in dirnames:
            continue
        candidate = Path(dirpath) / ".company"
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == canonical:
            continue
        strays.append(candidate)
    return strays


def check_cwd_skeleton_sweep(cfg: AuditConfig) -> list[AuditResult]:
    strays = _find_stray_company_dirs(cfg.project_root)
    if not strays:
        return [
            AuditResult(
                "cwd-skeleton-sweep",
                "PASS",
                "no stray .company skeletons found outside project root",
            )
        ]

    listing = ", ".join(str(p.relative_to(cfg.project_root)) for p in sorted(strays))
    return [
        AuditResult(
            "cwd-skeleton-sweep",
            "FAIL",
            f"{len(strays)} stray .company skeleton(s) found: {listing}",
            "Remove the stray directory; route the creating tool through "
            "company_resolver.get_company_dir() instead of a bare "
            'Path(".company") cwd-relative fallback',
        )
    ]


# ── Runner ────────────────────────────────────────────────────────────────────


def run_all_checks(
    cfg: AuditConfig, *, runner: Runner = subprocess.run
) -> list[AuditResult]:
    results: list[AuditResult] = []
    results.extend(check_config_vs_loader(cfg))
    results.extend(check_state_vs_github(cfg, runner=runner))
    results.extend(check_assessment_vs_reality(cfg, runner=runner))
    results.extend(check_runtime(cfg, runner=runner))
    results.extend(check_cwd_skeleton_sweep(cfg))
    return results


# ── Output ────────────────────────────────────────────────────────────────────

_ANSI: dict[str, str] = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}
_STATUS_COLOR: dict[str, str] = {
    "PASS": "green",
    "WARN": "yellow",
    "FAIL": "red",
    "SKIP": "dim",
}
_NAME_COL = 36


def _color(text: str, name: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_ANSI.get(name, '')}{text}{_ANSI['reset']}"


def print_results(results: list[AuditResult], use_color: bool) -> None:
    for r in results:
        status_label = _color(
            f"{r.status:<4}", _STATUS_COLOR.get(r.status, ""), use_color
        )
        # Check names here are longer than forge_doctor's (namespaced, e.g.
        # "assessment-vs-reality:coverage-freshness") — pad to _NAME_COL but
        # never let a long name run straight into detail with no separator.
        name_field = f"{r.name:<{_NAME_COL}}"
        if len(r.name) >= _NAME_COL:
            name_field = f"{r.name} "
        print(f"{status_label}  {name_field}{r.detail}")
        if r.remedy and r.status in ("FAIL", "WARN"):
            print(f"          Remedy: {r.remedy}")

    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status == "SKIP")

    parts: list[str] = [_color(f"{passed} passed", "green", use_color)]
    if failed:
        parts.append(_color(f"{failed} failed", "red", use_color))
    if warned:
        parts.append(_color(f"{warned} warning(s)", "yellow", use_color))
    if skipped:
        parts.append(f"{skipped} skipped")

    print(f"\n{total} checks: {', '.join(parts)}")


def print_results_json(results: list[AuditResult]) -> None:
    out = [
        {"name": r.name, "status": r.status, "detail": r.detail, "remedy": r.remedy}
        for r in results
    ]
    print(json.dumps(out, indent=2))


def _results_summary(results: list[AuditResult]) -> dict[str, int]:
    return {
        "pass": sum(1 for r in results if r.status == "PASS"),
        "warn": sum(1 for r in results if r.status == "WARN"),
        "fail": sum(1 for r in results if r.status == "FAIL"),
        "skip": sum(1 for r in results if r.status == "SKIP"),
    }


def write_report(cfg: AuditConfig, results: list[AuditResult]) -> None:
    """Atomically write the JSON report under .company/state/."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [
            {"name": r.name, "status": r.status, "detail": r.detail, "remedy": r.remedy}
            for r in results
        ],
        "summary": _results_summary(results),
    }

    cfg.report_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(cfg.report_path.parent), prefix=".reality_audit_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(cfg.report_path))
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in args

    project_root = Path.cwd()
    if "--project-root" in args:
        idx = args.index("--project-root")
        if idx + 1 < len(args):
            project_root = Path(args[idx + 1])

    cfg = make_config(project_root)
    results = run_all_checks(cfg)
    write_report(cfg, results)

    if json_mode:
        print_results_json(results)
    else:
        print_results(results, use_color=sys.stdout.isatty())

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
