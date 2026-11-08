# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Daemon watchdog — monitors heartbeat and health, restarts if needed.

External supervisor for the Gatesmith daemon. Detects stale heartbeats and
critically low health scores, then sends SIGTERM to trigger a launchd
KeepAlive respawn.

Public API:
    WatchdogConfig   — configuration dataclass
    WatchdogStatus   — health check result
    WatchdogAction   — cycle outcome enum
    check_daemon_health(config) -> WatchdogStatus
    trigger_restart(config, reason) -> bool
    run_watchdog_cycle(config) -> WatchdogAction
    main() -> NoReturn
"""

from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class WatchdogAction(Enum):
    """Outcome of a single watchdog monitoring cycle."""

    NO_ACTION = "no_action"
    RESTARTED = "restarted"
    COOLDOWN = "cooldown"


@dataclass
class WatchdogStatus:
    """Result of a daemon health check."""

    health_critical: bool = False
    health_score: int = 100


_DEFAULT_BASE_DIR = Path(".company")


@dataclass
class WatchdogConfig:
    """Configuration for the daemon watchdog.

    `base_dir` anchors every path field. Production callers leave it at
    `.company` so files resolve under the project's company directory.
    Tests pass `base_dir=<tmp_path>` to isolate from any real state on disk
    — previously each path field had its own cwd-relative default and tests
    had to override all four individually; missing one (the easy mistake)
    let real `.company/daemon_metrics.json` bleed into the fixture and made
    `_is_in_cooldown()` short-circuit, so the test passed on a clean CI
    checkout but failed locally.

    Per-file path fields still accept explicit overrides for the rare case
    where a caller wants one file under a different root.
    """

    base_dir: Path = field(default_factory=lambda: _DEFAULT_BASE_DIR)
    heartbeat_file: Path | None = None
    pid_file: Path | None = None
    health_cache_file: Path | None = None
    metrics_file: Path | None = None
    stale_threshold_seconds: int = 300
    health_threshold: int = 60
    cooldown_seconds: int = 300
    max_restarts_per_day: int = 5

    def __post_init__(self) -> None:
        if self.heartbeat_file is None:
            self.heartbeat_file = self.base_dir / "daemon.heartbeat"
        if self.pid_file is None:
            self.pid_file = self.base_dir / "daemon.pid"
        if self.health_cache_file is None:
            self.health_cache_file = self.base_dir / "health_cache.json"
        if self.metrics_file is None:
            self.metrics_file = self.base_dir / "daemon_metrics.json"


# ---------------------------------------------------------------------------
# Health-score check
# ---------------------------------------------------------------------------

# Health cache older than this is considered stale and ignored.
_HEALTH_CACHE_MAX_AGE_SECONDS = 600  # 10 minutes


def check_daemon_health(config: WatchdogConfig) -> WatchdogStatus:
    """Check whether the daemon's health score is critically low.

    Returns a WatchdogStatus. If the health cache file is missing, corrupt,
    or stale (>10 min old), returns health_critical=False (insufficient data
    to act on).
    """
    try:
        data = json.loads(config.health_cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return WatchdogStatus(health_critical=False)

    # Ignore stale health caches
    updated_at = data.get("updated_at")
    if updated_at:
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > _HEALTH_CACHE_MAX_AGE_SECONDS:
                return WatchdogStatus(health_critical=False)
        except (ValueError, TypeError):
            return WatchdogStatus(health_critical=False)

    score = data.get("health_score")
    if score is None:
        return WatchdogStatus(health_critical=False)

    score = int(score)
    critical = score < config.health_threshold
    return WatchdogStatus(health_critical=critical, health_score=score)


# ---------------------------------------------------------------------------
# Restart trigger
# ---------------------------------------------------------------------------


def _read_pid(config: WatchdogConfig) -> int | None:
    """Read PID from the pid file. Returns None on any error."""
    try:
        raw = config.pid_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        pid = data.get("pid")
        if pid is None:
            return None
        return int(pid)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None


def _load_metrics(config: WatchdogConfig) -> dict:
    """Load metrics JSON, returning a safe default if missing/corrupt."""
    try:
        return json.loads(config.metrics_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"restart_events": [], "summary": {}}


def _save_metrics(config: WatchdogConfig, data: dict) -> None:
    """Atomically write metrics JSON."""
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=config.metrics_file.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, config.metrics_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _is_in_cooldown(config: WatchdogConfig, metrics: dict) -> bool:
    """Check whether the last restart was within the cooldown window."""
    events = metrics.get("restart_events", [])
    if not events:
        return False

    try:
        last_ts = datetime.fromisoformat(events[-1]["timestamp"].replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return elapsed < config.cooldown_seconds
    except (KeyError, ValueError, TypeError):
        return False


def _exceeds_daily_cap(config: WatchdogConfig, metrics: dict) -> bool:
    """Check whether the daily restart cap has been reached."""
    events = metrics.get("restart_events", [])
    if not events:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for ev in events:
        try:
            ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
            if ts > cutoff:
                count += 1
        except (KeyError, ValueError, TypeError):
            continue

    return count >= config.max_restarts_per_day


def trigger_restart(config: WatchdogConfig, reason: str) -> bool:
    """Send SIGTERM to the daemon process and log the restart event.

    Returns True if SIGTERM was successfully sent, False otherwise.
    Respects cooldown and daily restart cap.
    """
    pid = _read_pid(config)
    if pid is None:
        return False

    metrics = _load_metrics(config)

    # Cooldown check
    if _is_in_cooldown(config, metrics):
        return False

    # Daily cap check
    if _exceeds_daily_cap(config, metrics):
        return False

    # Send SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        return False

    # Log restart event
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    metrics.setdefault("restart_events", []).append(event)
    try:
        _save_metrics(config, metrics)
    except OSError:
        pass  # best-effort logging

    return True


# ---------------------------------------------------------------------------
# Full watchdog cycle
# ---------------------------------------------------------------------------


def _is_heartbeat_stale(config: WatchdogConfig) -> bool:
    """Check heartbeat staleness using forge_daemon's is_heartbeat_stale."""
    try:
        from forge_daemon import DaemonConfig, is_heartbeat_stale

        cfg = DaemonConfig.__new__(DaemonConfig)
        cfg.heartbeat_file = config.heartbeat_file
        return is_heartbeat_stale(cfg, max_age_seconds=config.stale_threshold_seconds)
    except ImportError:
        # Fallback: read heartbeat directly
        try:
            data = json.loads(config.heartbeat_file.read_text(encoding="utf-8"))
            last_hb = data.get("last_heartbeat")
            if not last_hb:
                return True
            ts = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return age > config.stale_threshold_seconds
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            return True


def run_watchdog_cycle(config: WatchdogConfig) -> WatchdogAction:
    """Execute one complete watchdog monitoring cycle.

    Priority:
        1. Stale heartbeat → restart with reason "stale_heartbeat"
        2. Critical health  → restart with reason "health_score_low"
        3. Otherwise        → no action

    If the PID file is missing (daemon already dead), returns NO_ACTION
    even if heartbeat is stale.
    """
    stale = _is_heartbeat_stale(config)
    health = check_daemon_health(config)

    reason: str | None = None
    if stale:
        reason = "stale_heartbeat"
    elif health.health_critical:
        reason = "health_score_low"

    if reason is None:
        return WatchdogAction.NO_ACTION

    # If PID file doesn't exist, daemon is already dead — nothing to restart
    pid = _read_pid(config)
    if pid is None:
        return WatchdogAction.NO_ACTION

    # Check cooldown / daily cap before attempting restart
    metrics = _load_metrics(config)
    if _is_in_cooldown(config, metrics) or _exceeds_daily_cap(config, metrics):
        return WatchdogAction.COOLDOWN

    success = trigger_restart(config, reason=reason)
    if success:
        return WatchdogAction.RESTARTED

    return WatchdogAction.NO_ACTION


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run a single watchdog cycle and exit.

    Always exits with code 0 so it's safe for cron execution.
    """
    config = WatchdogConfig()
    try:
        run_watchdog_cycle(config)
    except Exception:
        pass  # watchdog must never crash cron
    raise SystemExit(0)


if __name__ == "__main__":
    main()
