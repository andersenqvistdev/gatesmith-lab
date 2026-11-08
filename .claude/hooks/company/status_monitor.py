#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Gatesmith Status Monitor — Concise, read-only operational snapshot of the Gatesmith daemon.

Usage:
    uv run .claude/hooks/company/status_monitor.py        # Human-readable snapshot
    uv run .claude/hooks/company/status_monitor.py --json # Machine-readable JSON
    uv run .claude/hooks/company/status_monitor.py --help # Show this help

Exit codes:
    0 — daemon running, circuit breaker CLOSED
    1 — daemon not running or circuit breaker OPEN/HALF_OPEN
    2 — usage error
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add script directory to path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from daemon_liveness import get_project_daemon_pid  # noqa: E402

# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def find_company_dir() -> Path:
    """Walk up from the script or cwd to find the .company directory."""
    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        for candidate_parent in [start] + list(start.parents):
            candidate = candidate_parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return Path(".company")


def load_json(path: Path) -> dict | list | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def get_queue_path(company_dir: Path) -> Path:
    primary = company_dir / "state" / "work_queue.json"
    return primary if primary.exists() else company_dir / "work_queue.json"


def get_loop_monitor(company_dir: Path) -> dict:
    data = load_json(company_dir / "state" / "loop_monitor.json")
    if not data:
        data = load_json(company_dir / "loop_monitor.json")
    return data or {}


def get_heartbeat(company_dir: Path) -> dict:
    data = load_json(company_dir / "runtime" / "daemon.heartbeat")
    if not data:
        # Fallback: older installs placed it directly under .company
        data = load_json(company_dir / "daemon.heartbeat")
    return data or {}


def get_autonomy_audit(company_dir: Path) -> dict | None:
    """Return the latest autonomy audit snapshot, or None if unavailable."""
    data = load_json(company_dir / "state" / "autonomy_audit.json")
    if not isinstance(data, dict):
        return None
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    return entries[-1]


_ACTIVE_ESCALATION_STATUSES = frozenset({"pending", "in_progress", "paused"})


def get_active_escalations_count(company_dir: Path) -> int:
    """Count escalation JSON files whose status is pending/in_progress/paused."""
    esc_dir = company_dir / "escalations"
    if not esc_dir.is_dir():
        return 0
    count = 0
    for esc_file in esc_dir.glob("*.json"):
        try:
            data = load_json(esc_file)
            if (
                isinstance(data, dict)
                and data.get("status") in _ACTIVE_ESCALATION_STATUSES
            ):
                count += 1
        except Exception:
            pass
    return count


# ---------------------------------------------------------------------------
# Process detection
# ---------------------------------------------------------------------------


def get_daemon_pids(company_dir: Path | None = None) -> list[str]:
    """Return the PID of THIS project's live daemon (empty list if stopped).

    Reads the project's pidfile rather than ``pgrep -f forge_daemon``, which
    also matched every other project's daemon.
    """
    pid = get_project_daemon_pid(company_dir or find_company_dir())
    return [str(pid)] if pid is not None else []


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


def relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 0:
            return "future"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return "?"


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

QUEUE_STATUSES = ("pending", "in_progress", "blocked", "review", "failed", "completed")


def collect_snapshot(company_dir: Path) -> dict:
    """Gather all data — strictly read-only."""
    pids = get_daemon_pids(company_dir)
    heartbeat = get_heartbeat(company_dir)
    loop_data = get_loop_monitor(company_dir)
    queue = load_json(get_queue_path(company_dir)) or {}

    cb = loop_data.get("circuit_breaker", {})
    hm = loop_data.get("health_metrics", {})

    queue_depth: dict[str, int] = {s: len(queue.get(s, [])) for s in QUEUE_STATUSES}

    autonomy_entry = get_autonomy_audit(company_dir)
    if autonomy_entry:
        gt = autonomy_entry.get("ground_truth") or {}
        autonomy: dict = {
            "calibrated": True,
            "verified_autonomy_rate_windowed": gt.get(
                "verified_autonomy_rate_windowed"
            ),
            "trust_score": gt.get("trust_score"),
            "phantom_rate": gt.get("phantom_rate"),
            "phantom_count": len(autonomy_entry.get("phantoms") or []),
            "build_sha": autonomy_entry.get("build_sha"),
        }
    else:
        autonomy = {"calibrated": False}

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "daemon": {
            "running": bool(pids),
            "pids": pids,
        },
        "heartbeat": {
            "last": heartbeat.get("last_heartbeat"),
            "status": heartbeat.get("status", "unknown"),
            "uptime_seconds": heartbeat.get("uptime_seconds"),
        },
        "circuit_breaker": {
            "state": cb.get("state", "unknown"),
            "failure_count": cb.get("failure_count", 0),
            "consecutive_failures": cb.get("consecutive_failures", 0),
            "opened_at": cb.get("opened_at"),
        },
        "health_metrics": {
            "total_successes": hm.get("total_successes", 0),
            "total_failures": hm.get("total_failures", 0),
        },
        "queue_depth": queue_depth,
        "autonomy": autonomy,
        "escalations": {"active_count": get_active_escalations_count(company_dir)},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CB_COLORS = {
    "CLOSED": "\033[32m",  # green
    "OPEN": "\033[31m",  # red
    "HALF_OPEN": "\033[33m",  # yellow
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def render_snapshot(snap: dict) -> str:
    lines: list[str] = []
    w = 60

    now_str = snap["timestamp"][:16].replace("T", " ") + " UTC"
    lines.append(_BOLD + "=" * w + _RESET)
    lines.append(f"{_BOLD}  GATESMITH STATUS  —  {now_str}{_RESET}")
    lines.append(_BOLD + "=" * w + _RESET)
    lines.append("")

    # ── Daemon ──────────────────────────────────────────────────────────────
    daemon = snap["daemon"]
    hb = snap["heartbeat"]
    if daemon["running"]:
        pid_str = ", ".join(daemon["pids"])
        lines.append(f"  {_BOLD}Daemon{_RESET}   \033[32mRUNNING\033[0m  PID {pid_str}")
    else:
        lines.append(f"  {_BOLD}Daemon{_RESET}   \033[31mSTOPPED\033[0m")

    hb_age = relative_time(hb["last"])
    uptime = hb.get("uptime_seconds")
    uptime_str = ""
    if uptime is not None:
        h, rem = divmod(int(uptime), 3600)
        m = rem // 60
        uptime_str = f"  uptime {h}h {m:02d}m" if h else f"  uptime {m}m"
    lines.append(f"  {'':9}heartbeat {hb_age}{uptime_str}")
    lines.append("")

    # ── Circuit Breaker ──────────────────────────────────────────────────────
    cb = snap["circuit_breaker"]
    hm = snap["health_metrics"]
    cb_state = (cb.get("state") or "unknown").upper()
    cb_color = _CB_COLORS.get(cb_state, "\033[33m")
    total = hm["total_successes"] + hm["total_failures"]
    rate_str = f"{hm['total_successes'] / total * 100:.0f}%" if total > 0 else "n/a"

    lines.append(
        f"  {_BOLD}Circuit{_RESET}  {cb_color}{cb_state}{_RESET}  "
        f"(success {rate_str}, {hm['total_successes']}/{total})"
    )
    if cb_state == "OPEN" and cb.get("opened_at"):
        lines.append(f"  {'':9}{_DIM}opened {relative_time(cb['opened_at'])}{_RESET}")
    lines.append("")

    # ── Autonomy ─────────────────────────────────────────────────────────────
    autonomy = snap.get("autonomy") or {}
    if autonomy.get("calibrated"):
        rate = autonomy.get("verified_autonomy_rate_windowed")
        trust = autonomy.get("trust_score")
        phantom_rate = autonomy.get("phantom_rate")
        phantom_count = autonomy.get("phantom_count", 0)
        sha = autonomy.get("build_sha") or "?"
        rate_s = f"{rate * 100:.1f}%" if rate is not None else "?"
        trust_s = f"{trust * 100:.1f}%" if trust is not None else "?"
        phantom_s = f"{phantom_rate * 100:.1f}%" if phantom_rate is not None else "?"
        lines.append(
            f"  {_BOLD}Autonomy{_RESET}  {rate_s} (30d)  "
            f"trust {trust_s}  phantom {phantom_s} ×{phantom_count}  sha {sha}"
        )
    else:
        lines.append(
            f"  {_BOLD}Autonomy{_RESET}  {_DIM}not calibrated — run /calibrate{_RESET}"
        )
    lines.append("")

    # ── Escalations ──────────────────────────────────────────────────────────
    esc = snap.get("escalations") or {}
    active_count = esc.get("active_count", 0)
    if active_count > 0:
        lines.append(
            f"  {_BOLD}Escalations{_RESET}  \033[33m{active_count} active\033[0m"
        )
    else:
        lines.append(f"  {_BOLD}Escalations{_RESET}  {_DIM}0 active{_RESET}")
    lines.append("")

    # ── Queue ────────────────────────────────────────────────────────────────
    qd = snap["queue_depth"]
    lines.append(f"  {_BOLD}Queue{_RESET}")
    for status in QUEUE_STATUSES:
        count = qd[status]
        bar = ""
        if count > 0 and status in ("pending", "in_progress", "blocked", "failed"):
            bar = " " + "█" * min(count, 20)
        color = ""
        if status == "in_progress":
            color = "\033[33m"
        elif status in ("blocked", "failed") and count > 0:
            color = "\033[31m"
        elif status == "completed":
            color = _DIM
        label = status.replace("_", " ").ljust(12)
        lines.append(f"  {'':5}{color}{label}{_RESET}  {count}{bar}")
    lines.append("")

    lines.append(_DIM + "-" * w + _RESET)
    lines.append(f"{_DIM}  gatesmith-status | --json for machine output{_RESET}")
    lines.append(_DIM + "-" * w + _RESET)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Exit code logic
# ---------------------------------------------------------------------------


def exit_code(snap: dict) -> int:
    """0 = healthy, 1 = not running or breaker non-CLOSED."""
    daemon_ok = snap["daemon"]["running"]
    cb_state = (snap["circuit_breaker"].get("state") or "unknown").upper()
    breaker_ok = cb_state == "CLOSED"
    return 0 if (daemon_ok and breaker_ok) else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        sys.exit(0)

    known = {"--json", "-h", "--help"}
    for a in args:
        if a.startswith("-") and a not in known:
            print(f"Unknown option: {a}", file=sys.stderr)
            print(__doc__.strip(), file=sys.stderr)
            sys.exit(2)

    company_dir = find_company_dir()
    snap = collect_snapshot(company_dir)

    if "--json" in args:
        print(json.dumps(snap, indent=2))
    else:
        print(render_snapshot(snap))

    sys.exit(exit_code(snap))


if __name__ == "__main__":
    main()  # pragma: no cover - exercised via main() directly in tests
