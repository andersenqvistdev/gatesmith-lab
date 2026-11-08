#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Nightly coverage module for the Gatesmith G1 (test coverage) goal pipeline.

Runs the project's full test suite with coverage once, deterministically,
and writes a trusted ``coverage.json`` at the project root -- the exact
file ``goal_tracker._read_trusted_coverage`` looks for. This replaces
letting an LLM worker session attempt a full-suite pytest run (which
cannot finish inside one session for a large suite and gets correctly
blocked by the deliverable gate three times, wasting a queue slot).

Design constraints:
- Scope matches the coverage config's source of truth (``.coveragerc`` /
  ``pyproject.toml [tool.coverage.run] source``), not CI's broader
  ``--cov=tests`` addition -- this keeps the measurement aligned with
  what ``goal_tracker._expected_measured_count`` treats as "the whole
  project" for its subset-run trust check.
- Takes the same ``.coverage.lock`` fcntl exclusive lock that
  ``assess_test_coverage``'s inline (non-lightweight) pytest runner
  uses, so this job can never race a daemon-triggered coverage run over
  the shared ``.coverage`` SQLite database (the WS-101 130GB-leak class).
- Injectable ``_runner`` for test isolation; never invokes ``subprocess``
  directly outside of it.
- Never raises for a coverage/test failure -- those are data, not fatal
  errors. Only I/O errors (company dir missing, lock unavailable within
  the wait) are fatal to the CLI.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Ceiling for the pytest subprocess itself. The LaunchAgent must NOT be
# relied on as an outer ceiling: launchd's ExitTimeOut is only the
# SIGTERM->SIGKILL grace at unload/shutdown, it never kills a long-running
# StartCalendarInterval job (see dev.gatesmith.coverage.plist).
DEFAULT_TIMEOUT_SECONDS = 5400  # 90 minutes

_LOCK_FILENAME = ".coverage.lock"
# pytest-cov writes the report as it exits; it must never target the final
# assessor-trusted path directly (a SIGKILL mid-write would leave a
# fresh-mtime partial file AND clobber the previous good measurement).
_PARTIAL_FILENAME = ".coverage.json.partial"

# Bounded lock wait: LOCK_NB + retry, never an unbounded flock that could
# wedge the job (and with it every future StartCalendarInterval firing —
# launchd skips firings while the label is still running).
LOCK_RETRIES = 6
LOCK_RETRY_DELAY_SECONDS = 30


def _run_pytest_with_coverage(
    project_root: Path,
    *,
    timeout: int,
    runner: Callable[..., Any],
    lock_retries: int | None = None,
    lock_retry_delay: float | None = None,
) -> dict[str, Any]:
    """Run pytest with coverage under the shared .coverage.lock.

    Returns a dict with keys: ran, tests_passed, timed_out, lock_held,
    error, duration_seconds. Never raises -- failures are reported in the
    dict so the CLI can pick a distinct exit code per class.
    """
    # Resolved at call time so tests can shorten the wait by monkeypatching
    # the module constants.
    if lock_retries is None:
        lock_retries = LOCK_RETRIES
    if lock_retry_delay is None:
        lock_retry_delay = LOCK_RETRY_DELAY_SECONDS

    result: dict[str, Any] = {
        "ran": False,
        "tests_passed": False,
        "timed_out": False,
        "lock_held": False,
        "error": None,
        "duration_seconds": 0.0,
    }

    lock_file_path = project_root / _LOCK_FILENAME
    start = time.monotonic()
    try:
        # Mode "a", never "w": macOS APFS returns EDEADLK at open() on a
        # file with outstanding fcntl locks when opened with O_TRUNC
        # (PR #986 incident class; same rationale as work_allocator's
        # QueueLock).
        with open(lock_file_path, "a") as lock_file:
            acquired = False
            for attempt in range(lock_retries):
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if attempt < lock_retries - 1:
                        time.sleep(lock_retry_delay)
            if not acquired:
                result["lock_held"] = True
                result["duration_seconds"] = time.monotonic() - start
                return result
            try:
                proc = runner(
                    [
                        "uv",
                        "run",
                        "pytest",
                        "tests/",
                        ".claude/tests/",
                        "--cov=.claude/hooks",
                        "--cov-config=.coveragerc",
                        f"--cov-report=json:{_PARTIAL_FILENAME}",
                        "-q",
                        "--tb=no",
                    ],
                    cwd=str(project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    proc.communicate(timeout=timeout)
                    result["ran"] = True
                    result["tests_passed"] = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait()
                    result["timed_out"] = True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        # Lock-file I/O problems are real failures the CLI must surface
        # (a silently swallowed lock error exited 0 and read as success),
        # but they must never raise past this function.
        result["error"] = f"lock/setup failure: {e}"

    result["duration_seconds"] = time.monotonic() - start
    return result


def _promote_partial_coverage(
    project_root: Path,
) -> tuple[float | None, int, str | None]:
    """Validate the partial report and atomically promote it to coverage.json.

    Returns (percent_covered, files_measured, error). Only a partial that
    parses as JSON with a numeric totals.percent_covered is promoted
    (os.replace within the same directory, so the swap is atomic and a
    previous good coverage.json is only ever replaced by a validated one).
    The partial is removed on every path.
    """
    partial = project_root / _PARTIAL_FILENAME
    final = project_root / "coverage.json"
    try:
        if not partial.exists():
            return None, 0, "no coverage report produced"
        try:
            data = json.loads(partial.read_text(encoding="utf-8"))
            totals = data.get("totals") or {}
            percent = totals.get("percent_covered")
            files = data.get("files") or {}
            measured = len(files) if isinstance(files, dict) else 0
            if not isinstance(percent, (int, float)):
                return None, 0, "coverage report missing totals.percent_covered"
        except (json.JSONDecodeError, OSError) as e:
            return None, 0, f"coverage report unreadable: {e}"
        os.replace(partial, final)
        return float(percent), measured, None
    finally:
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass


def run_nightly_coverage(
    company_dir: Path,
    *,
    project_root: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    _runner: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Execute one nightly coverage measurement cycle.

    Runs the full test suite with coverage and writes ``coverage.json`` at
    the project root. Returns a summary dict; never raises for test or
    coverage failures (those are reported in the summary, not exceptions).

    Parameters
    ----------
    company_dir:
        Path to the ``.company/`` directory. Never defaults to cwd.
    project_root:
        Repo root to run pytest from. Defaults to ``company_dir.parent``.
    dry_run:
        Print what would happen; do not invoke pytest or write any file.
    timeout:
        Seconds to allow the pytest subprocess before it is killed.
    _runner:
        Injectable replacement for ``subprocess.Popen`` (test isolation).
    """
    if project_root is None:
        project_root = company_dir.parent

    def _log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    coverage_file = project_root / "coverage.json"

    if dry_run:
        _log(f"[dry-run] would run pytest with coverage in {project_root}")
        return {
            "dry_run": True,
            "ran": False,
            "percent_covered": None,
            "files_measured": 0,
            "tests_passed": False,
            "timed_out": False,
            "duration_seconds": 0.0,
            "coverage_file": str(coverage_file),
        }

    _log(f"[nightly-coverage] running full suite with coverage in {project_root}")
    run_result = _run_pytest_with_coverage(
        project_root, timeout=timeout, runner=_runner
    )
    _log(
        f"[nightly-coverage] ran={run_result['ran']} "
        f"tests_passed={run_result['tests_passed']} "
        f"timed_out={run_result['timed_out']} "
        f"lock_held={run_result['lock_held']} "
        f"duration={run_result['duration_seconds']:.1f}s"
    )

    # Only a completed (non-timed-out) run may promote its report — and the
    # summary reports THIS run's measurement or nothing. Reading the final
    # coverage.json here would present a stale pre-existing file as tonight's
    # result (PR 263 review finding).
    percent: float | None = None
    measured = 0
    wrote = False
    promote_error: str | None = None
    if run_result["ran"] and not run_result["timed_out"]:
        percent, measured, promote_error = _promote_partial_coverage(project_root)
        wrote = promote_error is None
    else:
        # Remove any partial a killed/failed run left behind.
        try:
            (project_root / _PARTIAL_FILENAME).unlink(missing_ok=True)
        except OSError:
            pass

    summary: dict[str, Any] = {
        "dry_run": False,
        "ran": run_result["ran"],
        "tests_passed": run_result["tests_passed"],
        "timed_out": run_result["timed_out"],
        "lock_held": run_result["lock_held"],
        "error": run_result["error"] or promote_error,
        "duration_seconds": run_result["duration_seconds"],
        "percent_covered": percent,
        "files_measured": measured,
        "wrote_coverage": wrote,
        "coverage_file": str(coverage_file),
    }
    return summary


# ---------------------------------------------------------------------------
# CLI (called by bin/gatesmith-coverage-nightly)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dry_run": False,
        "company_dir": None,
        "project_root": None,
        "verbose": False,
        "timeout": DEFAULT_TIMEOUT_SECONDS,
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
        elif tok == "--timeout":
            i += 1
            if i >= len(argv):
                print("error: --timeout requires an argument", file=sys.stderr)
                sys.exit(2)
            try:
                result["timeout"] = int(argv[i])
            except ValueError:
                print("error: --timeout value must be an integer", file=sys.stderr)
                sys.exit(2)
        else:
            print(f"error: unknown argument {tok!r}", file=sys.stderr)
            sys.exit(2)
        i += 1
    return result


_USAGE = """\
Usage: gatesmith-coverage-nightly [OPTIONS]

Run the full test suite with coverage once and write a trusted
coverage.json at the project root, for the G1 goal assessor to read.

Options:
  --dry-run          Print what would happen; do not run pytest or write files
  --company-dir DIR  Override .company/ directory location
  --project-root DIR Project root to run pytest from (default: company_dir parent)
  --timeout SECONDS  Pytest subprocess timeout (default: 5400)
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
        summary = run_nightly_coverage(
            company_dir,
            project_root=args["project_root"],
            dry_run=args["dry_run"],
            verbose=args["verbose"] or args["dry_run"],
            timeout=args["timeout"],
        )
    except Exception as exc:
        print(f"fatal: nightly coverage run failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))

    # Distinct exit codes per failure class — launchd logs the exit status,
    # so "silently exits 0 while the pipeline is dead" (PR 263 review) must
    # be impossible. 0 strictly means "a fresh validated coverage.json was
    # written tonight".
    if summary.get("dry_run"):
        return 0
    if summary.get("lock_held"):
        print(
            "error: .coverage.lock held by another process — gave up",
            file=sys.stderr,
        )
        return 2
    if summary.get("timed_out"):
        print("error: pytest timed out and was killed", file=sys.stderr)
        return 3
    if not summary.get("wrote_coverage"):
        print(
            f"error: no trusted coverage.json written: {summary.get('error')}",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
