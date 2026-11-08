#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Daemon liveness — is THIS project's forge daemon running?

``pgrep -f forge_daemon`` matches the daemon of every project on the machine,
so with two projects running daemons, "a daemon is alive" was true even when
this project's own daemon was stopped: zombie detection never reset orphaned
in_progress tasks and status output claimed a running daemon.

This module answers from the project's own pidfile instead. A daemon counts as
running only when ALL of these hold:

1. the pidfile exists and names a positive pid,
2. ``os.kill(pid, 0)`` says the process exists, and
3. its command line names ``forge_daemon`` (a recycled pid belonging to some
   other process is not a daemon).

Anything else -- missing, unreadable or stale pidfile -- means no daemon.
Read-only: never writes or removes the pidfile (``forge_daemon.py`` owns it).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DAEMON_CMDLINE_MARKER = "forge_daemon"
PIDFILE_NAME = "daemon.pid"
PS_TIMEOUT_SECONDS = 5

# The shipped config's daemon.pidFile is ".company/daemon.pid"; DaemonConfig's
# own default is ".company/runtime/daemon.pid". Accept both.
PIDFILE_RELATIVE_PATHS = (Path(PIDFILE_NAME), Path("runtime") / PIDFILE_NAME)


def _parse_pidfile(path: Path) -> int | None:
    """Return the pid in ``path`` (JSON ``{"pid": N}`` or a bare integer)."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("pid")
    # bool is an int subclass; ``true`` is not a pid.
    if isinstance(data, bool) or not isinstance(data, int):
        return None
    return data if data > 0 else None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def read_command_line(pid: int) -> str:
    """Full command line of ``pid``, or "" when it cannot be determined."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()


def get_project_daemon_pid(company_dir: Path) -> int | None:
    """Pid of the live forge daemon recorded under ``company_dir``, else None."""
    for relative in PIDFILE_RELATIVE_PATHS:
        pid = _parse_pidfile(company_dir / relative)
        if pid is None or not _pid_exists(pid):
            continue
        if DAEMON_CMDLINE_MARKER in read_command_line(pid):
            return pid
    return None
