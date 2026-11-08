#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Morning Report Generator — Overnight Activity Analysis

Generates a comprehensive analysis of daemon activity, merged PRs,
code changes, and issues discovered overnight.

Usage:
    uv run .claude/hooks/company/morning_report.py [--since=TIME] [--output=FORMAT] [--save]

Options:
    --since=TIME    Analysis start (default: 18:00 previous day)
    --output=FORMAT Output: terminal, html, markdown (default: terminal)
    --save          Save report to .company/reports/
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run_cmd(cmd: str) -> str:
    """Run command and return output (secure: no shell=True)."""
    try:
        # Parse command string into list for safe execution
        cmd_list = shlex.split(cmd)
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=30)
        return result.stdout.strip()
    except Exception:
        return ""


def get_merged_prs(since: str) -> list[dict]:
    """Get PRs merged since given time."""
    cmd = f'gh pr list --state merged --search "merged:>{since}" --json number,title,mergedAt,additions,deletions,files'
    output = run_cmd(cmd)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def get_open_prs() -> list[dict]:
    """Get currently open PRs with status."""
    cmd = "gh pr list --state open --json number,title,mergeable,statusCheckRollup,createdAt"
    output = run_cmd(cmd)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def get_commits_since(since: str) -> list[str]:
    """Get commits on main since given time."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={since}", "main"],
            capture_output=True,
            text=True,
            timeout=30,
            stderr=subprocess.DEVNULL,  # Replaces 2>/dev/null
        )
        output = result.stdout.strip()
        return output.split("\n") if output else []
    except Exception:
        return []


def categorize_pr(pr: dict) -> str:
    """Categorize PR by file types changed."""
    files = pr.get("files", [])
    if not files:
        return "UNKNOWN"

    file_paths = [f.get("path", "") if isinstance(f, dict) else str(f) for f in files]

    # Check categories
    has_code = any(
        (
            p.endswith((".py", ".ts", ".js", ".tsx", ".jsx"))
            and not p.startswith("tests/")
            and "_test" not in p
        )
        for p in file_paths
    )
    has_tests = any(
        p.startswith("tests/") or "_test.py" in p or ".spec." in p for p in file_paths
    )
    has_docs = any(p.endswith(".md") or p.startswith("docs/") for p in file_paths)
    has_status = any("status.html" in p or "product-site" in p for p in file_paths)
    has_config = any(p.endswith((".json", ".yml", ".yaml")) for p in file_paths)

    if has_status and not has_code:
        return "STATUS PAGE"
    if has_code:
        return "REAL CODE" + (" + TESTS" if has_tests else "")
    if has_tests:
        return "TESTS ONLY"
    if has_docs:
        return "DOCS"
    if has_config:
        return "CONFIG"
    return "OTHER"


def analyze_stuck_prs(prs: list[dict]) -> list[dict]:
    """Find PRs that are stuck."""
    stuck = []
    now = datetime.now(timezone.utc)

    for pr in prs:
        issues = []

        # Check age
        created = pr.get("createdAt", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = (now - created_dt).total_seconds() / 3600
                if age_hours > 12:
                    issues.append(f"Open {int(age_hours)}h")
            except ValueError:
                pass

        # Check mergeable
        if pr.get("mergeable") == "CONFLICTING":
            issues.append("Merge conflict")

        # Check CI
        checks = pr.get("statusCheckRollup", [])
        if not checks:
            issues.append("No CI running")
        else:
            failed = [c for c in checks if c.get("conclusion") == "FAILURE"]
            if failed:
                issues.append(
                    f"CI failing: {', '.join(c.get('name', '?') for c in failed)}"
                )

        if issues:
            stuck.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title", "")[:60],
                    "issues": issues,
                }
            )

    return stuck


def get_daemon_activity(since: datetime) -> dict:
    """Analyze daemon logs for activity."""
    log_path = Path(".company/logs/daemon.log")
    activity = {
        "tasks_completed": 0,
        "tasks_failed": 0,
        "circuit_breaker_trips": 0,
        "escalations": 0,
        "errors": [],
    }

    if not log_path.exists():
        return activity

    try:
        lines = log_path.read_text().split("\n")
        for line in lines[-500:]:  # Last 500 lines
            try:
                if not line.strip():
                    continue
                entry = json.loads(line)

                ts_str = entry.get("timestamp", "")
                if ts_str:
                    # Parse timestamp
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts < since:
                            continue
                    except ValueError:
                        continue

                msg = entry.get("message", "")

                if "COMPLETED" in msg:
                    activity["tasks_completed"] += 1
                elif "FAILED" in msg or "failed" in msg.lower():
                    activity["tasks_failed"] += 1
                elif "circuit" in msg.lower() and "open" in msg.lower():
                    activity["circuit_breaker_trips"] += 1
                elif "escalat" in msg.lower():
                    activity["escalations"] += 1

            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    return activity


def get_autonomy_metrics(company_dir: Path | None = None) -> dict:
    """Read latest autonomy metrics from autonomy_audit.json. Returns available=False when absent."""
    base = company_dir if company_dir is not None else Path(".company")
    audit_path = base / "state" / "autonomy_audit.json"
    if not audit_path.exists():
        return {"available": False}

    try:
        data = json.loads(audit_path.read_text())
        entries = data.get("entries", [])
        if not entries:
            return {"available": False}

        latest = entries[-1]
        ground_truth = latest.get("ground_truth", {})
        local_proxy = latest.get("local_proxy", {}) or {}
        return {
            "available": True,
            "autonomy_proxy_rate": local_proxy.get("autonomy_proxy_rate"),
            "verified_autonomy_rate": ground_truth.get("verified_autonomy_rate"),
            "phantom_rate": ground_truth.get("phantom_rate"),
            # Phase 0: TREND-honest windowed rate (recent cohort) when present.
            "verified_autonomy_rate_windowed": ground_truth.get(
                "verified_autonomy_rate_windowed"
            ),
            "window_days": ground_truth.get("window_days"),
            "generated_at": latest.get("generated_at", ""),
        }
    except Exception:
        return {"available": False}


def get_goal_status(company_dir: Path | None = None) -> list[dict]:
    """Read per-goal progress from the latest strategic_state.json snapshot.

    Returns a list of assessment dicts ``{"goal_id": "G1", "progress": 72,
    "status": "on_track"}`` — or an empty list when no planning cycle has run.
    """
    base = company_dir if company_dir is not None else Path(".company")
    state_path = base / "state" / "strategic_state.json"
    if not state_path.exists():
        return []
    try:
        data = json.loads(state_path.read_text())
        snapshots = data.get("goal_snapshots") or []
        if not snapshots:
            return []
        latest = snapshots[-1]
        return latest.get("assessments") or []
    except Exception:
        return []


def get_queue_status() -> dict:
    """Get current queue status."""
    queue_path = Path(".company/state/work_queue.json")
    status = {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "p0": 0,
        "p1": 0,
        "p2_plus": 0,
        "next_tasks": [],
    }

    if not queue_path.exists():
        return status

    try:
        queue = json.loads(queue_path.read_text())
        pending = queue.get("pending", [])

        status["pending"] = len(pending)
        status["in_progress"] = len(queue.get("in_progress", []))
        status["completed"] = len(queue.get("completed", []))

        for t in pending:
            p = t.get("priority", 99)
            if p == 0:
                status["p0"] += 1
            elif p == 1:
                status["p1"] += 1
            else:
                status["p2_plus"] += 1

        # Next 3 tasks
        status["next_tasks"] = [t.get("title", "?")[:55] for t in pending[:3]]

    except Exception:
        pass

    return status


def generate_terminal_report(
    merged_prs: list[dict],
    stuck_prs: list[dict],
    daemon: dict,
    queue: dict,
    since: str,
    now: str,
    autonomy: dict | None = None,
    goal_status: list[dict] | None = None,
) -> str:
    """Generate terminal-formatted report."""

    lines = []
    lines.append("=" * 79)
    lines.append(
        f" OVERNIGHT ACTIVITY ANALYSIS                              [{since} - {now}]"
    )
    lines.append("=" * 79)
    lines.append("")

    # Merged PRs
    total_additions = sum(pr.get("additions", 0) for pr in merged_prs)
    total_deletions = sum(pr.get("deletions", 0) for pr in merged_prs)
    lines.append(
        f" MERGED PRs ({len(merged_prs)})                                    +{total_additions} / -{total_deletions}"
    )
    lines.append("-" * 79)

    categories = {"REAL CODE": 0, "TESTS": 0, "DOCS": 0, "STATUS PAGE": 0, "OTHER": 0}

    for pr in merged_prs:
        cat = categorize_pr(pr)
        title = pr.get("title", "")[:55]
        lines.append(f" * PR #{pr.get('number')}: {title}...")
        lines.append(f"   Verdict: {cat}")

        # Track categories
        if "REAL CODE" in cat:
            categories["REAL CODE"] += 1
        elif "TEST" in cat:
            categories["TESTS"] += 1
        elif cat == "DOCS":
            categories["DOCS"] += 1
        elif cat == "STATUS PAGE":
            categories["STATUS PAGE"] += 1
        else:
            categories["OTHER"] += 1

    if not merged_prs:
        lines.append(" (no PRs merged overnight)")

    lines.append("")

    # Stuck PRs
    if stuck_prs:
        lines.append(f" STUCK PRs ({len(stuck_prs)}) - ACTION REQUIRED")
        lines.append("-" * 79)
        for pr in stuck_prs:
            lines.append(f" ! PR #{pr['number']}: {pr['title']}")
            lines.append(f"   Issues: {', '.join(pr['issues'])}")
        lines.append("")

    # Daemon Activity
    lines.append(" DAEMON ACTIVITY")
    lines.append("-" * 79)
    lines.append(f" Tasks Completed:     {daemon['tasks_completed']}")
    lines.append(f" Tasks Failed:        {daemon['tasks_failed']}")
    lines.append(
        f" Circuit Breaker:     {'TRIPPED' if daemon['circuit_breaker_trips'] else 'OK'}"
    )
    lines.append(f" Escalations:         {daemon['escalations']}")
    lines.append("")

    # Queue Status
    lines.append(" QUEUE STATUS")
    lines.append("-" * 79)
    lines.append(
        f" Pending:       {queue['pending']} (P0: {queue['p0']}, P1: {queue['p1']}, P2+: {queue['p2_plus']})"
    )
    lines.append(f" In Progress:   {queue['in_progress']}")
    lines.append(f" Completed:     {queue['completed']}")
    lines.append("")
    lines.append(" Next up:")
    for i, task in enumerate(queue["next_tasks"], 1):
        lines.append(f"   {i}. {task}")
    lines.append("")

    # Autonomy
    if autonomy and autonomy.get("available"):
        verified = autonomy.get("verified_autonomy_rate")
        phantom = autonomy.get("phantom_rate")
        proxy_rate = autonomy.get("autonomy_proxy_rate")
        windowed = autonomy.get("verified_autonomy_rate_windowed")
        window_days = autonomy.get("window_days")
        as_of = (autonomy.get("generated_at", "") or "")[:10]
        verified_str = f"{verified:.1%}" if verified is not None else "n/a"
        phantom_str = f"{phantom:.1%}" if phantom is not None else "n/a"
        claimed_str = f"{proxy_rate:.1%}" if proxy_rate is not None else "n/a"
        date_tag = f"  [{as_of}]" if as_of else ""
        lines.append(" AUTONOMY")
        lines.append("-" * 79)
        # Lead with the TREND-honest windowed number when available (Phase 0).
        if windowed is not None:
            lines.append(
                f" Verified ({window_days or 30}d): {windowed:.1%}"
                f"  |  All-time: {verified_str}  |  Claimed: {claimed_str}  |  Phantom: {phantom_str}{date_tag}"
            )
        else:
            lines.append(
                f" Verified: {verified_str}  |  Claimed: {claimed_str}  |  Phantom: {phantom_str}{date_tag}"
            )
        lines.append("")

    # Per-goal status vs vision
    if goal_status:
        lines.append(" GOAL STATUS (vs vision)")
        lines.append("-" * 79)
        for g in goal_status:
            gid = g.get("goal_id", "?")
            progress = g.get("progress", 0)
            status = g.get("status", "unknown")
            bar = "#" * (progress // 10) + "." * (10 - progress // 10)
            lines.append(f" {gid:<4} [{bar}] {progress:3}%  {status}")
        lines.append("")

    # Verdict
    lines.append("-" * 79)
    lines.append(" VERDICT")
    lines.append("-" * 79)
    lines.append(f" Real Code:     {categories['REAL CODE']} PRs")
    lines.append(f" Tests:         {categories['TESTS']} PRs")
    lines.append(f" Docs/Config:   {categories['DOCS'] + categories['OTHER']} PRs")
    lines.append(f" Status Pages:  {categories['STATUS PAGE']} PRs")
    lines.append("")

    # Assessment
    if categories["REAL CODE"] >= 2 and daemon["tasks_failed"] == 0:
        assessment = "PRODUCTIVE"
    elif categories["REAL CODE"] >= 1 or daemon["tasks_completed"] >= 5:
        assessment = "MIXED"
    else:
        assessment = "QUIET" if not stuck_prs else "CONCERNING"

    lines.append(f" Assessment:    {assessment}")
    lines.append("=" * 79)

    return "\n".join(lines)


def generate_html_report(
    merged_prs: list[dict],
    stuck_prs: list[dict],
    daemon: dict,
    queue: dict,
    date_str: str,
    autonomy: dict | None = None,
    goal_status: list[dict] | None = None,
) -> str:
    """Generate HTML report matching daily report style."""

    # Count categories
    real_code = sum(1 for pr in merged_prs if "REAL CODE" in categorize_pr(pr))
    tests = sum(1 for pr in merged_prs if "TEST" in categorize_pr(pr))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gatesmith Labs &mdash; Morning Report {date_str}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:'SF Mono',Monaco,Consolas,monospace;
font-size:14px;line-height:1.6;padding:20px;max-width:900px;margin:0 auto}}
h1{{color:#58a6ff;font-size:1.6em;border-bottom:1px solid #21262d;padding-bottom:12px;margin-bottom:24px}}
h2{{color:#58a6ff;font-size:1.2em;margin-bottom:12px}}
.section{{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:20px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
th{{text-align:left;color:#8b949e;font-weight:600;padding:6px 12px;border-bottom:1px solid #21262d}}
td{{padding:6px 12px;border-bottom:1px solid #21262d}}
.verdict-good{{color:#238636}}
.verdict-mixed{{color:#d29922}}
.verdict-bad{{color:#f85149}}
.footer{{text-align:center;color:#484f58;font-size:0.85em;padding:20px 0;border-top:1px solid #21262d;margin-top:8px}}
</style>
</head>
<body>
<h1>Morning Report &mdash; {date_str}</h1>

<div class="section">
<h2>Overnight Summary</h2>
<table>
<tr><td>PRs Merged</td><td>{len(merged_prs)}</td></tr>
<tr><td>Real Code Changes</td><td>{real_code}</td></tr>
<tr><td>Test Additions</td><td>{tests}</td></tr>
<tr><td>Tasks Completed</td><td>{daemon["tasks_completed"]}</td></tr>
<tr><td>Tasks Failed</td><td>{daemon["tasks_failed"]}</td></tr>
<tr><td>Stuck PRs</td><td>{len(stuck_prs)}</td></tr>
</table>
</div>

<div class="section">
<h2>Merged PRs</h2>
<table>
<tr><th>PR</th><th>Title</th><th>Category</th></tr>
"""

    for pr in merged_prs:
        cat = categorize_pr(pr)
        html += f"<tr><td>#{pr.get('number')}</td><td>{pr.get('title', '')[:50]}</td><td>{cat}</td></tr>\n"

    if not merged_prs:
        html += '<tr><td colspan="3">(no PRs merged overnight)</td></tr>\n'

    html += """</table>
</div>

<div class="section">
<h2>Queue Status</h2>
<table>
"""
    html += f"<tr><td>Pending</td><td>{queue['pending']} (P0: {queue['p0']}, P1: {queue['p1']})</td></tr>\n"
    html += f"<tr><td>In Progress</td><td>{queue['in_progress']}</td></tr>\n"
    html += f"<tr><td>Total Completed</td><td>{queue['completed']}</td></tr>\n"
    html += """</table>
</div>
"""

    if autonomy and autonomy.get("available"):
        verified = autonomy.get("verified_autonomy_rate")
        phantom = autonomy.get("phantom_rate")
        proxy_rate = autonomy.get("autonomy_proxy_rate")
        windowed = autonomy.get("verified_autonomy_rate_windowed")
        window_days = autonomy.get("window_days") or 30
        as_of = (autonomy.get("generated_at", "") or "")[:10]
        verified_str = f"{verified:.1%}" if verified is not None else "n/a"
        phantom_str = f"{phantom:.1%}" if phantom is not None else "n/a"
        claimed_str = f"{proxy_rate:.1%}" if proxy_rate is not None else "n/a"
        date_tag = f" (as of {as_of})" if as_of else ""
        windowed_row = (
            f"<tr><td>Verified Autonomy (last {window_days}d)</td>"
            f"<td>{windowed:.1%}</td></tr>\n"
            if windowed is not None
            else ""
        )
        html += f"""<div class="section">
<h2>Autonomy{date_tag}</h2>
<table>
{windowed_row}<tr><td>Verified Autonomy Rate (all-time)</td><td>{verified_str}</td></tr>
<tr><td>Claimed Rate (proxy)</td><td>{claimed_str}</td></tr>
<tr><td>Phantom Rate</td><td>{phantom_str}</td></tr>
</table>
</div>
"""

    if goal_status:
        html += """<div class="section">
<h2>Goal Status (vs vision)</h2>
<table>
<tr><th>Goal</th><th>Progress</th><th>Status</th></tr>
"""
        for g in goal_status:
            gid = g.get("goal_id", "?")
            progress = g.get("progress", 0)
            status = g.get("status", "unknown")
            html += f"<tr><td>{gid}</td><td>{progress}%</td><td>{status}</td></tr>\n"
        html += "</table>\n</div>\n"

    if stuck_prs:
        html += """<div class="section">
<h2>Action Required</h2>
<table>
<tr><th>PR</th><th>Issue</th></tr>
"""
        for pr in stuck_prs:
            html += (
                f"<tr><td>#{pr['number']}</td><td>{', '.join(pr['issues'])}</td></tr>\n"
            )
        html += "</table>\n</div>\n"

    html += f"""<div class="footer">Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; Gatesmith Morning Report</div>
</body>
</html>"""

    return html


def main():
    args = sys.argv[1:]

    # Parse arguments
    since_arg = "18:00 yesterday"
    output_format = "terminal"
    save_report = False
    company_dir: Path | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--since="):
            since_arg = arg.split("=", 1)[1]
        elif arg.startswith("--output="):
            output_format = arg.split("=", 1)[1]
        elif arg == "--save":
            save_report = True
        elif arg == "--company-dir" and i + 1 < len(args):
            company_dir = Path(args[i + 1])
            i += 1
        elif arg.startswith("--company-dir="):
            company_dir = Path(arg.split("=", 1)[1])
        i += 1

    # Calculate since datetime
    now = datetime.now(timezone.utc)
    if "yesterday" in since_arg:
        since_dt = now - timedelta(days=1)
        since_dt = since_dt.replace(hour=18, minute=0, second=0, microsecond=0)
    else:
        since_dt = now - timedelta(hours=12)

    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Gather data
    merged_prs = get_merged_prs(since_iso[:10])
    open_prs = get_open_prs()
    stuck_prs = analyze_stuck_prs(open_prs)
    daemon = get_daemon_activity(since_dt)
    queue = get_queue_status()
    autonomy = get_autonomy_metrics(company_dir)
    goals = get_goal_status(company_dir)

    # Generate report
    date_str = now.strftime("%Y-%m-%d")
    since_str = since_dt.strftime("%Y-%m-%d %H:%M")
    now_str = now.strftime("%Y-%m-%d %H:%M")

    if output_format == "html":
        report = generate_html_report(
            merged_prs, stuck_prs, daemon, queue, date_str, autonomy, goals
        )
    else:
        report = generate_terminal_report(
            merged_prs, stuck_prs, daemon, queue, since_str, now_str, autonomy, goals
        )

    print(report)

    # Save if requested
    if save_report:
        report_dir = (company_dir or Path(".company")) / "reports"
        report_dir.mkdir(exist_ok=True)

        html_report = generate_html_report(
            merged_prs, stuck_prs, daemon, queue, date_str, autonomy, goals
        )
        report_path = report_dir / f"morning-{date_str}.html"
        report_path.write_text(html_report)
        print(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    main()
