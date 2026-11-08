"""Daily status report generator for Gatesmith daemon.

Generates a self-contained HTML daily status report from .company/ state files.
Stdlib only, no external dependencies.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path


def _load_json(path: Path) -> dict | list | None:
    """Load a JSON file, returning None if missing or invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _parse_daemon_log(company_dir: Path) -> dict:
    """Extract daemon health metrics from daemon.log (last 200 lines)."""
    log_path = company_dir / "logs" / "daemon.log"
    info: dict = {
        "last_cycle": None,
        "cycle_number": None,
        "lifetime_completed": 0,
        "lifetime_failed": 0,
        "planned": 0,
        "direct": 0,
        "last_timestamp": None,
    }
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()[-200:]
    except (FileNotFoundError, OSError):
        return info

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = entry.get("message", "")
        ts = entry.get("timestamp")

        if info["last_timestamp"] is None and ts:
            info["last_timestamp"] = ts

        # --- Cycle N | queue: ...
        if "--- Cycle " in msg and "done" not in msg:
            try:
                cycle_num = int(msg.split("Cycle ")[1].split(" ")[0])
                if info["cycle_number"] is None:
                    info["cycle_number"] = cycle_num
            except (IndexError, ValueError):
                pass

        # --- Cycle N done (...) | lifetime: X completed, Y failed ...
        if "done" in msg and "lifetime:" in msg:
            try:
                lifetime_part = msg.split("lifetime:")[1]
                completed = int(lifetime_part.split("completed")[0].strip())
                failed = int(
                    lifetime_part.split("completed,")[1].split("failed")[0].strip()
                )
                if info["lifetime_completed"] == 0:
                    info["lifetime_completed"] = completed
                    info["lifetime_failed"] = failed
            except (IndexError, ValueError):
                pass
            try:
                planning_part = msg.split("planning:")[1]
                planned = int(planning_part.split("planned")[0].strip())
                direct = int(
                    planning_part.split("planned,")[1].split("direct")[0].strip()
                )
                if info["planned"] == 0:
                    info["planned"] = planned
                    info["direct"] = direct
            except (IndexError, ValueError):
                pass

    return info


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _esc(text: str) -> str:
    return html.escape(str(text))


def _section(title: str, body: str) -> str:
    return f"""<div class="section">
<h2>{_esc(title)}</h2>
{body}
</div>"""


def _kv_row(key: str, value: str) -> str:
    return f"<tr><td class='label'>{_esc(key)}</td><td>{_esc(value)}</td></tr>"


def _status_badge(status: str) -> str:
    colors = {
        "completed": "#4caf50",
        "on_track": "#4caf50",
        "approved": "#2196f3",
        "at_risk": "#ff9800",
        "behind": "#ff5722",
        "failed": "#f44336",
        "blocked": "#9e9e9e",
        "queued": "#607d8b",
    }
    color = colors.get(status, "#607d8b")
    return (
        f"<span style='background:{color};color:#fff;padding:2px 8px;"
        f"border-radius:3px;font-size:0.85em;'>{_esc(status)}</span>"
    )


def _build_daemon_section(company_dir: Path) -> str:
    daemon = _parse_daemon_log(company_dir)
    total = daemon["lifetime_completed"] + daemon["lifetime_failed"]
    success_rate = (
        f"{daemon['lifetime_completed'] / total * 100:.1f}%" if total > 0 else "N/A"
    )

    rows = [
        _kv_row("Last heartbeat", daemon["last_timestamp"] or "N/A"),
        _kv_row("Current cycle", str(daemon["cycle_number"] or "N/A")),
        _kv_row("Lifetime completed", str(daemon["lifetime_completed"])),
        _kv_row("Lifetime failed", str(daemon["lifetime_failed"])),
        _kv_row("Success rate", success_rate),
        _kv_row("Planned executions", str(daemon["planned"])),
        _kv_row("Direct executions", str(daemon["direct"])),
    ]
    return _section("Daemon Health", f"<table>{''.join(rows)}</table>")


def _build_tasks_section(company_dir: Path) -> str:
    wq = _load_json(company_dir / "state/work_queue.json")
    if not isinstance(wq, dict):
        return _section("Tasks", "<p>No work queue data available.</p>")

    today = _today_str()
    completed = wq.get("completed", [])
    completed_today = [
        t
        for t in completed
        if isinstance(t, dict)
        and isinstance(t.get("completed_at"), str)
        and t["completed_at"][:10] == today
    ]
    failed_today = [
        t
        for t in completed
        if isinstance(t, dict)
        and t.get("result") == "failed"
        and isinstance(t.get("completed_at"), str)
        and t["completed_at"][:10] == today
    ]
    pending = wq.get("pending", [])
    in_progress = wq.get("in_progress", [])
    blocked = wq.get("blocked", [])

    rows = [
        _kv_row("Completed today", str(len(completed_today))),
        _kv_row("Failed today", str(len(failed_today))),
        _kv_row("Pending", str(len(pending))),
        _kv_row("In progress", str(len(in_progress))),
        _kv_row("Blocked", str(len(blocked))),
        _kv_row("Total completed (all time)", str(len(completed))),
    ]

    task_table = f"<table>{''.join(rows)}</table>"

    # List today's completed tasks
    if completed_today:
        items = "".join(
            f"<li>{_esc(t.get('title', t.get('task_id', '?')))}</li>"
            for t in completed_today[:20]
        )
        task_table += f"<h3>Completed Today</h3><ul>{items}</ul>"

    if failed_today:
        items = "".join(
            f"<li>{_esc(t.get('title', t.get('task_id', '?')))}</li>"
            for t in failed_today[:10]
        )
        task_table += f"<h3>Failed Today</h3><ul class='failed'>{items}</ul>"

    return _section("Tasks", task_table)


def _build_goals_section(company_dir: Path) -> str:
    ss = _load_json(company_dir / "state/strategic_state.json")
    if not isinstance(ss, dict):
        return _section("Goals", "<p>No strategic state data available.</p>")

    # Get latest goal assessments from snapshots
    snapshots = ss.get("goal_snapshots", [])
    assessments = []
    if snapshots and isinstance(snapshots[-1], dict):
        assessments = snapshots[-1].get("assessments", [])

    # Get active initiatives
    initiatives = ss.get("active_initiatives", [])

    body = ""

    if assessments:
        header = "<tr><th>Goal</th><th>Progress</th><th>Status</th><th>Owner</th></tr>"
        rows = []
        for a in assessments:
            if not isinstance(a, dict):
                continue
            progress = a.get("progress_percent", 0)
            if not isinstance(progress, (int, float)):
                progress = 0
            status = a.get("status", "unknown")
            bar = (
                f"<div class='progress-bar'>"
                f"<div class='progress-fill' style='width:{progress}%'></div>"
                f"<span class='progress-text'>{progress}%</span>"
                f"</div>"
            )
            # Trend arrow: up for on_track/complete, down for at_risk, flat otherwise
            if status in ("on_track", "completed", "complete"):
                trend = "<span class='trend-arrow trend-up'>&uarr;</span>"
            elif status == "at_risk":
                trend = "<span class='trend-arrow trend-down'>&darr;</span>"
            else:
                trend = "<span class='trend-arrow trend-flat'>&rarr;</span>"
            # At-risk row class when status is at_risk or progress < 50
            row_class = (
                " class='at-risk'" if (status == "at_risk" or progress < 50) else ""
            )
            goal_name = _esc(a.get("goal_name", a.get("goal_id", "?")))
            rows.append(
                f"<tr{row_class}>"
                f"<td>{goal_name} {trend}</td>"
                f"<td>{bar}</td>"
                f"<td>{_status_badge(status)}</td>"
                f"<td>{_esc(a.get('owner', 'N/A'))}</td>"
                f"</tr>"
            )
        body += f"<table class='goals'>{header}{''.join(rows)}</table>"
    else:
        body += "<p>No goal assessments available.</p>"

    if initiatives:
        active = [i for i in initiatives if i.get("status") != "completed"]
        if active:
            header = "<tr><th>Initiative</th><th>Status</th><th>Owner</th></tr>"
            rows = []
            for init in active[:10]:
                rows.append(
                    f"<tr>"
                    f"<td>{_esc(init.get('title', init.get('id', '?')))}</td>"
                    f"<td>{_status_badge(init.get('status', 'unknown'))}</td>"
                    f"<td>{_esc(init.get('owner', 'N/A'))}</td>"
                    f"</tr>"
                )
            body += f"<h3>Active Initiatives</h3><table>{header}{''.join(rows)}</table>"

    return _section("Goals", body)


def _build_queue_section(company_dir: Path) -> str:
    wq = _load_json(company_dir / "state/work_queue.json")
    if not isinstance(wq, dict):
        return _section("Queue State", "<p>No work queue data available.</p>")

    pending = wq.get("pending", [])
    if not pending:
        return _section("Queue State", "<p>Queue is empty. All tasks processed.</p>")

    header = "<tr><th>Task</th><th>Priority</th><th>Complexity</th><th>Source</th></tr>"
    rows = []
    for t in pending[:15]:
        rows.append(
            f"<tr>"
            f"<td>{_esc(t.get('title', t.get('task_id', '?')))}</td>"
            f"<td>{_esc(str(t.get('priority', 'N/A')))}</td>"
            f"<td>{_esc(str(t.get('estimated_complexity', 'N/A')))}</td>"
            f"<td>{_esc(str(t.get('source', 'N/A')))}</td>"
            f"</tr>"
        )
    remaining = len(pending) - 15
    footer = f"<p class='muted'>... and {remaining} more</p>" if remaining > 0 else ""
    return _section(
        "Queue State",
        f"<p>{len(pending)} pending tasks</p>"
        f"<table>{header}{''.join(rows)}</table>{footer}",
    )


def _build_html(company_dir: Path, company_name: str) -> str:
    today = _today_str()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = [
        _build_daemon_section(company_dir),
        _build_tasks_section(company_dir),
        _build_goals_section(company_dir),
        _build_queue_section(company_dir),
    ]

    footer = f'<div class="footer">Generated by Gatesmith daemon &middot; {_esc(now)}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(company_name)} &mdash; Daily Status {today}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:'SF Mono',Monaco,Consolas,monospace;
font-size:14px;line-height:1.6;padding:20px;max-width:900px;margin:0 auto}}
h1{{color:#58a6ff;font-size:1.6em;border-bottom:1px solid #21262d;padding-bottom:12px;
margin-bottom:24px}}
h2{{color:#58a6ff;font-size:1.2em;margin-bottom:12px}}
h3{{color:#8b949e;font-size:1em;margin:16px 0 8px}}
.section{{background:#161b22;border:1px solid #21262d;border-radius:6px;
padding:20px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
th{{text-align:left;color:#8b949e;font-weight:600;padding:6px 12px;
border-bottom:1px solid #21262d;font-size:0.85em;text-transform:uppercase}}
td{{padding:6px 12px;border-bottom:1px solid #21262d}}
td.label{{color:#8b949e;width:200px;font-weight:500}}
tr:last-child td{{border-bottom:none}}
ul{{list-style:none;padding-left:0}}
ul li{{padding:4px 0;color:#c9d1d9}}
ul li::before{{content:"\\2022";color:#58a6ff;font-weight:bold;display:inline-block;
width:1em;margin-left:0}}
ul.failed li::before{{color:#f44336}}
.progress-bar{{background:#21262d;border-radius:4px;height:18px;position:relative;
min-width:120px}}
.progress-fill{{background:#238636;height:100%;border-radius:4px;
transition:width 0.3s}}
.progress-text{{position:absolute;right:6px;top:0;font-size:0.8em;
line-height:18px;color:#c9d1d9}}
tr.at-risk td{{background:rgba(255,152,0,0.08);border-left:3px solid #ff9800}}
.trend-arrow{{font-size:1.1em;margin-left:4px}}
.trend-up{{color:#4caf50}}
.trend-down{{color:#ff9800}}
.trend-flat{{color:#8b949e}}
.footer{{text-align:center;color:#484f58;font-size:0.85em;padding:20px 0;
border-top:1px solid #21262d;margin-top:8px}}
.muted{{color:#484f58;font-size:0.9em;margin-top:8px}}
table.goals td:first-child{{font-weight:500}}
</style>
</head>
<body>
<h1>{_esc(company_name)} &mdash; Daily Status Report</h1>
<p class="muted">Report date: {today}</p>
{"".join(sections)}
{footer}
</body>
</html>"""


def generate_daily_report(company_dir: Path) -> Path:
    """Generate a daily HTML status report from company state files.

    Args:
        company_dir: Path to the .company/ directory.

    Returns:
        Path to the generated report file.
    """
    company_dir = Path(company_dir)

    # Determine company name
    org = _load_json(company_dir / "org.json")
    company_name = "Gatesmith"
    if isinstance(org, dict):
        company_block = org.get("company", {})
        if isinstance(company_block, dict):
            company_name = company_block.get("name", "Gatesmith")

    html_content = _build_html(company_dir, company_name)

    # Write to reports directory
    reports_dir = company_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today = _today_str()
    report_path = reports_dir / f"daily-{today}.html"
    report_path.write_text(html_content, encoding="utf-8")

    # Also keep a "latest" copy for dashboards. Runtime output must stay
    # inside .company/ — never write into tracked/deployed directories.
    status_path = reports_dir / "status.html"
    status_path.write_text(html_content, encoding="utf-8")

    return report_path


def _parse_args(argv: list[str]) -> Path:
    """Resolve the company dir from CLI args, ignoring flag-style arguments.

    Flags like --save (accepted by morning_report.py) must never be treated
    as a path: doing so once created a literal './--save/' directory.
    """
    import sys

    company: Path | None = None
    for arg in argv:
        if arg.startswith("-"):
            print(f"daily_report: ignoring flag argument {arg!r}", file=sys.stderr)
            continue
        if company is None:
            company = Path(arg)
    if company is None:
        company = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    return company


if __name__ == "__main__":
    import sys

    out = generate_daily_report(_parse_args(sys.argv[1:]))
    print(f"Report generated: {out}")
