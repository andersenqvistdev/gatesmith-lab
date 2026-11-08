"""Test flakiness tracker for CI stability (Initiative 4 / G9).

Parses pytest output from daemon task logs, accumulates per-test pass/fail
history in .company/analytics/test-history.json, classifies tests as
stable / flaky / broken, and generates a weekly flakiness report.

Stdlib only — no external dependencies.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Classification thresholds (from initiative spec)
# ---------------------------------------------------------------------------
STABLE_MAX_FAILURE_RATE = 0.05  # 0–5 %  → stable
FLAKY_MAX_FAILURE_RATE = 0.90  # 6–90 % → flaky
# > 90 % → broken
MIN_RUNS_FOR_CLASSIFICATION = 2  # need at least 2 runs to classify

# ---------------------------------------------------------------------------
# Patterns for log parsing
# ---------------------------------------------------------------------------
# Matches individual pytest lines:  "tests/test_foo.py::test_bar PASSED"
_PYTEST_ITEM_RE = re.compile(
    r"(tests/[\w/]+\.py::[\w\[\]\-]+)\s+(PASSED|FAILED|ERROR)",
)
# Matches pytest summary line: "5 passed, 2 failed"
_PYTEST_SUMMARY_RE = re.compile(
    r"(\d+)\s+passed(?:,\s*(\d+)\s+failed)?(?:,\s*(\d+)\s+error)?",
)
# Matches daemon task outcome lines (task-level, not test-level)
_TASK_OUTCOME_RE = re.compile(
    r'"message":\s*"\s*[✓✗]\s*(COMPLETED|FAILED):\s*([^|"]+?)(?:\s*\|[^"]*)?"\s*',
)
# Timestamp in daemon log entries
_TIMESTAMP_RE = re.compile(r'"timestamp":\s*"([^"]+)"')


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def parse_log_file(log_path: Path) -> list[dict[str, Any]]:
    """Parse a daemon log file and return a list of test result events.

    Each event has the shape::

        {
            "timestamp": "2026-03-09T...",
            "test_name": "tests/test_foo.py::test_bar",
            "outcome": "PASSED" | "FAILED" | "ERROR",
            "source": "pytest_item" | "task",
        }
    """
    events: list[dict[str, Any]] = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return events

    for line in lines:
        # --- Try to decode as JSON first (structured daemon log) ----------
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            entry = None

        if entry:
            ts = entry.get("timestamp", _utcnow())
            msg = entry.get("message", "")

            # Individual pytest items embedded inside a log message
            for m in _PYTEST_ITEM_RE.finditer(msg):
                events.append(
                    {
                        "timestamp": ts,
                        "test_name": m.group(1),
                        "outcome": m.group(2),
                        "source": "pytest_item",
                    }
                )

            # Task-level outcomes: map to a synthetic "test name" based on
            # truncated task description so we can track recurring tasks.
            for m in _TASK_OUTCOME_RE.finditer(line):
                outcome_word, description = m.group(1), m.group(2).strip()
                if not description:
                    continue
                # Normalise: lowercase, collapse whitespace, cap at 80 chars
                norm = re.sub(r"\s+", " ", description).strip()[:80]
                outcome = "PASSED" if outcome_word == "COMPLETED" else "FAILED"
                events.append(
                    {
                        "timestamp": ts,
                        "test_name": f"task::{norm}",
                        "outcome": outcome,
                        "source": "task",
                    }
                )
        else:
            # Plain-text line (e.g. captured stdout from employee subprocess)
            ts_match = _TIMESTAMP_RE.search(line)
            ts = ts_match.group(1) if ts_match else _utcnow()
            for m in _PYTEST_ITEM_RE.finditer(line):
                events.append(
                    {
                        "timestamp": ts,
                        "test_name": m.group(1),
                        "outcome": m.group(2),
                        "source": "pytest_item",
                    }
                )

    return events


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------


def load_history(history_path: Path) -> dict[str, Any]:
    """Load existing test history or return empty structure."""
    try:
        return json.loads(history_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "updated": _utcnow(), "tests": {}}


def save_history(history: dict[str, Any], history_path: Path) -> None:
    """Atomically save history JSON."""
    history["updated"] = _utcnow()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = history_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(history_path)


def accumulate_events(
    history: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    max_runs_per_test: int = 200,
) -> dict[str, Any]:
    """Merge new events into the history dict and return it.

    Each test entry stores the last ``max_runs_per_test`` run outcomes to
    bound memory usage.
    """
    tests: dict[str, Any] = history.setdefault("tests", {})
    for event in events:
        name = event["test_name"]
        if name not in tests:
            tests[name] = {
                "runs": [],
                "first_seen": event["timestamp"],
                "source": event["source"],
            }
        entry = tests[name]
        entry.setdefault("source", event["source"])
        runs: list[dict] = entry.setdefault("runs", [])
        runs.append({"ts": event["timestamp"], "outcome": event["outcome"]})
        if len(runs) > max_runs_per_test:
            entry["runs"] = runs[-max_runs_per_test:]
    return history


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_test(runs: list[dict[str, str]]) -> dict[str, Any]:
    """Return classification info for a single test's run history."""
    total = len(runs)
    if total == 0:
        return {"category": "unknown", "failure_rate": None, "total_runs": 0}

    failures = sum(1 for r in runs if r["outcome"] in ("FAILED", "ERROR"))
    failure_rate = failures / total
    last_run = runs[-1]["outcome"] if runs else None

    if total < MIN_RUNS_FOR_CLASSIFICATION:
        category = "insufficient_data"
    elif failure_rate <= STABLE_MAX_FAILURE_RATE:
        category = "stable"
    elif failure_rate <= FLAKY_MAX_FAILURE_RATE:
        category = "flaky"
    else:
        category = "broken"

    return {
        "category": category,
        "failure_rate": round(failure_rate, 4),
        "total_runs": total,
        "failures": failures,
        "passes": total - failures,
        "last_outcome": last_run,
    }


def build_classification_table(history: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify all tests in history and return sorted rows."""
    rows = []
    for name, data in history.get("tests", {}).items():
        info = classify_test(data.get("runs", []))
        rows.append(
            {
                "test_name": name,
                "first_seen": data.get("first_seen"),
                "source": data.get("source", "unknown"),
                **info,
            }
        )
    # Sort: broken first, then flaky (by failure_rate desc), then stable
    _order = {
        "broken": 0,
        "flaky": 1,
        "stable": 2,
        "insufficient_data": 3,
        "unknown": 4,
    }
    rows.sort(
        key=lambda r: (
            _order.get(r["category"], 5),
            -(r["failure_rate"] or 0),
        )
    )
    return rows


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_REPORT_TEMPLATE = """\
# Test Flakiness Report

**Generated:** {generated}
**Period:** All accumulated history
**Source:** `.company/analytics/test-history.json`
**Goal Alignment:** G9 (Stability)

---

## Summary

| Category | Count | Description |
|----------|-------|-------------|
| Broken   | {broken_count:>5} | Failure rate > 90 % — priority fix |
| Flaky    | {flaky_count:>5} | Failure rate 6–90 % — isolate & fix |
| Stable   | {stable_count:>5} | Failure rate 0–5 % — monitor |
| Insufficient data | {insufficient_count:>5} | < {min_runs} runs — too early to classify |
| **Total tracked** | **{total_count:>5}** | |

---

## Broken Tests (>90 % failure rate)

{broken_section}

---

## Flaky Tests (6–90 % failure rate)

{flaky_section}

---

## Recommendations

{recommendations}

---

*Auto-generated by `test_flakiness_tracker.py`. Update by running the tracker.*
"""

_TABLE_HEADER = (
    "| Test | Failure Rate | Failures | Total Runs | Last Outcome |\n"
    "|------|-------------|----------|------------|--------------|"
)


def _format_table(rows: list[dict[str, Any]], max_rows: int = 50) -> str:
    if not rows:
        return "_None found._"
    lines = [_TABLE_HEADER]
    for row in rows[:max_rows]:
        name = row["test_name"]
        rate = (
            f"{row['failure_rate'] * 100:.1f}%"
            if row["failure_rate"] is not None
            else "—"
        )
        lines.append(
            f"| `{name}` | {rate} | {row['failures']} | {row['total_runs']} | {row['last_outcome'] or '—'} |"
        )
    if len(rows) > max_rows:
        lines.append(f"| *(+{len(rows) - max_rows} more)* | | | | |")
    return "\n".join(lines)


def _build_recommendations(broken: list[dict], flaky: list[dict]) -> str:
    items = []
    if not broken and not flaky:
        items.append("No action required — all tests are stable.")
    if broken:
        items.append(
            f"**{len(broken)} broken test(s)** detected (>90% failure rate). "
            "Assign priority fixes; these are blocking CI confidence."
        )
    if flaky:
        items.append(
            f"**{len(flaky)} flaky test(s)** detected. "
            "Investigate non-determinism: timing, external state, random seeds. "
            "Consider quarantining while fixes are developed."
        )
    top_flaky = [r for r in flaky if (r.get("failure_rate") or 0) >= 0.5]
    if top_flaky:
        names = ", ".join(f"`{r['test_name']}`" for r in top_flaky[:3])
        items.append(f"Highest-priority flaky tests (≥50% failure rate): {names}.")
    return "\n".join(f"- {item}" for item in items)


def generate_report(history: dict[str, Any], report_path: Path) -> str:
    """Classify tests and write the flakiness report. Returns rendered text."""
    rows = build_classification_table(history)

    broken = [r for r in rows if r["category"] == "broken"]
    flaky = [r for r in rows if r["category"] == "flaky"]
    stable = [r for r in rows if r["category"] == "stable"]
    insufficient = [r for r in rows if r["category"] == "insufficient_data"]

    report = _REPORT_TEMPLATE.format(
        generated=_utcnow(),
        broken_count=len(broken),
        flaky_count=len(flaky),
        stable_count=len(stable),
        insufficient_count=len(insufficient),
        min_runs=MIN_RUNS_FOR_CLASSIFICATION,
        total_count=len(rows),
        broken_section=_format_table(broken),
        flaky_section=_format_table(flaky),
        recommendations=_build_recommendations(broken, flaky),
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(
    company_dir: Path | None = None,
    *,
    all_log_files: bool = False,
) -> dict[str, Any]:
    """Run the full aggregation + report pipeline.

    Args:
        company_dir: Path to ``.company/`` directory. Defaults to cwd-relative.
        all_log_files: If True, parse *all* ``*.log`` files in the logs dir
            rather than just ``daemon.log``.

    Returns:
        Summary dict with counts by category.
    """
    if company_dir is None:
        company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"

    logs_dir = company_dir / "logs"
    analytics_dir = company_dir / "analytics"
    history_path = analytics_dir / "test-history.json"
    report_path = analytics_dir / "test-flakiness-report.md"

    # Collect log files to parse
    if all_log_files:
        log_files = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
    else:
        log_files = [logs_dir / "daemon.log"]

    # Parse logs
    events: list[dict[str, Any]] = []
    for lf in log_files:
        events.extend(parse_log_file(lf))

    # Load + update history
    history = load_history(history_path)
    history = accumulate_events(history, events)
    save_history(history, history_path)

    # Generate report
    generate_report(history, report_path)

    # Build summary
    rows = build_classification_table(history)
    summary = {
        "total": len(rows),
        "broken": sum(1 for r in rows if r["category"] == "broken"),
        "flaky": sum(1 for r in rows if r["category"] == "flaky"),
        "stable": sum(1 for r in rows if r["category"] == "stable"),
        "insufficient_data": sum(
            1 for r in rows if r["category"] == "insufficient_data"
        ),
        "history_path": str(history_path),
        "report_path": str(report_path),
        "events_parsed": len(events),
    }
    return summary


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
