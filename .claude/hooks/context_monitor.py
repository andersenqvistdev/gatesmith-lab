# /// script
# requires-python = ">=3.10"
# ///
"""
PostToolUse Hook: Context usage monitor (from GSD v1.22).

Tracks tool call count as a proxy for context consumption. Warns when
approaching limits to prevent silent context overflow during long sessions.

Uses a lightweight state file (.company/context_monitor.json) to persist
call counts across hook invocations within a session. The file is keyed
by session start time to auto-reset between sessions.

Output:
  - stderr: Warning messages shown to the user at WARNING (35%) and CRITICAL (25%)
  - stdout: JSON for machine consumption (exit 0 always — advisory only)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_project_root() -> Path | None:
    """Find project root by looking for .claude directory."""
    cwd = Path.cwd()
    for parent in [cwd, *list(cwd.parents)]:
        if (parent / ".claude").is_dir():
            return parent
    return None


# ── Configuration ──────────────────────────────────────────────────────────────
# Estimated tool calls before context is exhausted (conservative estimate).
# Claude Code sessions typically support ~200k tokens. Average tool call
# consumes ~800 tokens (input + output). So ~250 calls fills context.
# We warn early because compaction loses nuance.
ESTIMATED_MAX_CALLS = 250
WARNING_THRESHOLD = 0.65  # Warn at 65% usage (35% remaining)
CRITICAL_THRESHOLD = 0.75  # Critical at 75% usage (25% remaining)
DEBOUNCE_CALLS = 8  # Don't warn more often than every N calls


def load_state(state_path: Path, session_key: str) -> dict:
    """Load or initialize monitor state for this session."""
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            # Reset if different session
            if state.get("session_key") != session_key:
                return new_state(session_key)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return new_state(session_key)


def new_state(session_key: str) -> dict:
    """Create fresh state for a new session."""
    return {
        "session_key": session_key,
        "call_count": 0,
        "last_warning_at": 0,
        "warning_level": "none",  # none, warning, critical
    }


def save_state(state_path: Path, state: dict) -> None:
    """Save state atomically."""
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(state_path.parent), suffix=".json", prefix=".ctx_"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, str(state_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    project_root = find_project_root()
    if not project_root:
        sys.exit(0)

    # Determine session key from session_id or fallback to date
    session_id = hook_input.get("session_id", "")
    session_key = (
        session_id if session_id else datetime.now(timezone.utc).strftime("%Y%m%d%H")
    )

    # State file
    company_dir = project_root / ".company"
    if not company_dir.exists():
        # No company dir = likely not a full Gatesmith project, skip
        sys.exit(0)

    state_path = company_dir / "context_monitor.json"
    state = load_state(state_path, session_key)

    # Increment call count
    state["call_count"] += 1
    call_count = state["call_count"]
    usage_ratio = call_count / ESTIMATED_MAX_CALLS

    # Check thresholds with debounce
    calls_since_last_warning = call_count - state["last_warning_at"]

    if usage_ratio >= CRITICAL_THRESHOLD and calls_since_last_warning >= DEBOUNCE_CALLS:
        remaining_pct = int((1 - usage_ratio) * 100)
        print(
            f"CONTEXT CRITICAL: ~{remaining_pct}% context remaining "
            f"({call_count}/{ESTIMATED_MAX_CALLS} tool calls). "
            "Consider wrapping up current work or using /complete to checkpoint.",
            file=sys.stderr,
        )
        state["last_warning_at"] = call_count
        state["warning_level"] = "critical"

    elif (
        usage_ratio >= WARNING_THRESHOLD and calls_since_last_warning >= DEBOUNCE_CALLS
    ):
        remaining_pct = int((1 - usage_ratio) * 100)
        if state["warning_level"] != "critical":  # Don't downgrade
            print(
                f"CONTEXT WARNING: ~{remaining_pct}% context remaining "
                f"({call_count}/{ESTIMATED_MAX_CALLS} tool calls). "
                "Plan to checkpoint soon with /complete or /quick.",
                file=sys.stderr,
            )
            state["last_warning_at"] = call_count
            state["warning_level"] = "warning"

    save_state(state_path, state)

    # Always exit 0 — this is advisory only, never blocks
    sys.exit(0)


if __name__ == "__main__":
    main()
