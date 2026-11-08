# /// script
# requires-python = ">=3.10"
# ///
"""
Stop Hook: Validate that the agent actually completed its task.
Can force continuation if completion criteria aren't met.

Returns JSON with "continue": true to keep the agent working.

Also displays a session summary when the session ends, including:
- Duration
- Tool call counts
- Tasks completed
- Commits created
- Health score and change
- Alert summary
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def find_project_root() -> Path | None:
    """Find project root by looking for .claude directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / ".claude").is_dir():
            return parent
    return None


# Find project root for all path operations
PROJECT_ROOT = find_project_root() or Path.cwd()


# -----------------------------------------------------------------------------
# Session Summary Functions
# -----------------------------------------------------------------------------


def load_session_start_time() -> datetime | None:
    """
    Load the session start time from logs/sessions.jsonl.
    Returns the most recent session start time.
    """
    log_file = PROJECT_ROOT / "logs" / "sessions.jsonl"
    if not os.path.exists(log_file):
        return None

    try:
        last_session = None
        with open(log_file) as f:
            for line in f:
                if line.strip():
                    last_session = json.loads(line)

        if last_session and "session_start" in last_session:
            start_str = last_session["session_start"]
            # Handle timezone format variations
            return datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    return None


def load_session_state() -> dict:
    """
    Load session state from .company/session_state.json.
    Falls back to sessions.jsonl for start_time if needed.
    """
    state_file = PROJECT_ROOT / ".company" / "session_state.json"
    state = {}

    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # If no start_time in state, try to get from sessions.jsonl
    if "start_time" not in state:
        start = load_session_start_time()
        if start:
            state["start_time"] = start.isoformat()

    return state


def count_tool_calls(session_id: str | None = None) -> dict:
    """
    Count tool calls from the activity log.
    If session_id is provided, filter by that session.
    Otherwise, count all tools from the current session (based on recent timestamps).
    """
    log_file = PROJECT_ROOT / "logs" / "activity.jsonl"
    if not os.path.exists(log_file):
        return {}

    tool_counts: Counter = Counter()
    session_start = load_session_start_time()

    try:
        with open(log_file) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)

                    # Filter by session_id if provided
                    if session_id and entry.get("session_id") != session_id:
                        continue

                    # Otherwise filter by timestamp (after session start)
                    if not session_id and session_start:
                        entry_time_str = entry.get("timestamp", "")
                        if entry_time_str:
                            entry_time = datetime.fromisoformat(
                                entry_time_str.replace("Z", "+00:00")
                            )
                            if entry_time < session_start:
                                continue

                    tool_name = entry.get("tool", "unknown")
                    tool_counts[tool_name] += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return dict(tool_counts)


def count_commits_in_session() -> int:
    """
    Count git commits created during the current session.
    Uses git log --since with the session start time.
    """
    session_start = load_session_start_time()
    if not session_start:
        return 0

    # Format for git log --since
    since_str = session_start.strftime("%Y-%m-%d %H:%M:%S")

    import subprocess

    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_str}", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.getcwd(),
        )
        if result.returncode == 0:
            lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
            return len(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return 0


def count_tasks_completed_in_session() -> int:
    """
    Count tasks marked as completed during the session.
    Looks in activity log for Task tool calls or git commits with task IDs.
    """
    # For now, we approximate by counting commits (1 task = 1 commit pattern)
    return count_commits_in_session()


def get_current_health() -> int:
    """
    Get current health score from dashboard_aggregator.
    Returns a default of 75 if unavailable.
    """
    try:
        # Add hooks/company to path for import
        hooks_company_dir = str(PROJECT_ROOT / ".claude" / "hooks" / "company")
        if hooks_company_dir not in sys.path:
            sys.path.insert(0, hooks_company_dir)

        from dashboard_aggregator import aggregate_health

        health_data = aggregate_health()
        return health_data.get("health_score", 75)
    except (ImportError, Exception):
        return 75


def get_alert_summary() -> dict:
    """
    Get alert summary for the session.
    Returns counts of resolved and new alerts.
    """
    # For now, return placeholder - could be extended to read from alerts log
    return {"resolved": 0, "new": 0}


def format_duration(delta: timedelta) -> str:
    """Format timedelta as 'Xh Ym'."""
    total_seconds = int(delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_session_summary(session_id: str | None = None) -> dict:
    """
    Generate session summary statistics.

    Args:
        session_id: Optional session ID to filter activity

    Returns:
        Dict with session statistics
    """
    # Load session state
    session_state = load_session_state()
    start_time_str = session_state.get("start_time")
    start_health = session_state.get("start_health", 75)

    # Calculate duration
    if start_time_str:
        try:
            start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            duration = datetime.now(timezone.utc) - start
            duration_str = format_duration(duration)
        except ValueError:
            duration_str = "Unknown"
    else:
        duration_str = "Unknown"

    # Count tool calls from activity log
    tool_counts = count_tool_calls(session_id)
    total_calls = sum(tool_counts.values())

    # Format tool breakdown (top tools)
    top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    tool_breakdown_str = ", ".join(f"{name}: {count}" for name, count in top_tools)

    # Count tasks and commits
    commits = count_commits_in_session()
    tasks = count_tasks_completed_in_session()

    # Get current health
    current_health = get_current_health()
    health_change = current_health - start_health

    # Get alert summary
    alerts = get_alert_summary()

    return {
        "duration": duration_str,
        "tool_calls": total_calls,
        "tool_breakdown": tool_breakdown_str,
        "tool_counts": tool_counts,
        "tasks_completed": tasks,
        "commits_created": commits,
        "patterns_captured": 0,  # Placeholder for future pattern detection
        "health_current": current_health,
        "health_start": start_health,
        "health_change": health_change,
        "alerts_resolved": alerts["resolved"],
        "alerts_new": alerts["new"],
    }


def render_session_summary(summary: dict) -> str:
    """
    Render the session summary as formatted text.

    Args:
        summary: Session summary dict from get_session_summary()

    Returns:
        Formatted string for display
    """
    # Health change indicator
    health_change = summary["health_change"]
    if health_change > 0:
        health_indicator = f"(+{health_change} from session start)"
    elif health_change < 0:
        health_indicator = f"({health_change} from session start)"
    else:
        health_indicator = "(unchanged)"

    # Tool calls line
    tool_line = f"Tool calls: {summary['tool_calls']}"
    if summary["tool_breakdown"]:
        tool_line += f" ({summary['tool_breakdown']})"

    lines = [
        "",
        "-" * 61,
        "SESSION SUMMARY",
        "-" * 61,
        f"Duration: {summary['duration']}",
        tool_line,
        f"Tasks completed: {summary['tasks_completed']}",
        f"Commits created: {summary['commits_created']}",
        "",
        f"Health: {summary['health_current']}/100 {health_indicator}",
        f"Alerts: {summary['alerts_resolved']} resolved, {summary['alerts_new']} new",
        "-" * 61,
        "",
    ]

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Incomplete-work detection
# -----------------------------------------------------------------------------

# These patterns must match the agent ANNOUNCING that it is stopping with its own
# work unfinished — nothing else. The previous list was bare substrings ("TODO",
# "FIXME", "placeholder"), which match a message that merely mentions them: a
# report saying "I removed the TODO comment" would have forced the agent to keep
# working. Forced continuation is expensive and confusing when it is wrong, so
# these are deliberately narrow and first-person. Prefer missing a real case to
# inventing one; a missed nudge costs nothing, a false one derails the session.
INCOMPLETE_PATTERNS = [
    (
        r"\bI(?:['’]ll| will) (?:do|finish|implement|complete|add|handle|write|fix)\b"
        r"[^.!?\n]{0,60}\blater\b",
        "says it will finish the work later",
    ),
    (
        r"\bskipping\b[^.!?\n]{0,60}\bfor now\b",
        "says it is skipping work for now",
    ),
    (
        r"\b(?:left|leaving) (?:this |that |it )?as a placeholder\b",
        "says it left a placeholder",
    ),
    (
        r"\b(?:will be|to be) (?:done|finished|implemented|completed|added)\b"
        r"[^.!?\n]{0,40}\bin a follow[- ]?up\b",
        "defers the work to a follow-up",
    ),
    (
        r"\bfor now,? I(?:['’]ve| have)? ?(?:just )?(?:stubbed|left|skipped)\b",
        "says it stubbed or skipped part of the work",
    ),
]

_COMPILED_INCOMPLETE = [
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in INCOMPLETE_PATTERNS
]


# Text inside quotes is somebody else's words, not the agent announcing its own
# state: "I removed the line that said 'skipping for now'" reports finished
# work. Straight double, straight single (only when it opens a span — the
# apostrophe in "I'll" is preceded by a word character and is left alone),
# curly double, curly single, and backtick code spans.
_QUOTED_SPAN = re.compile(
    r'"[^"\n]*"'
    r"|\u201c[^\u201d\n]*\u201d"
    r"|(?<!\w)'[^'\n]*'(?!\w)"
    r"|\u2018[^\u2019\n]*\u2019"
    r"|`[^`\n]*`"
)

# A negation earlier in the same clause flips the meaning: "Nothing was left as
# a placeholder" and "I am not skipping anything for now" report finished work.
# The clause starts at the last sentence/clause punctuation, so a negation in a
# previous clause ("It is not done, and I left this as a placeholder") does not
# mask a real signal.
_NEGATION = re.compile(
    r"\b(?:no|not|nothing|never|none|nobody|without|\w+n['\u2019]t)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(r"[.!?;:,\n]")
_NEGATION_WINDOW = 200  # chars of look-back; keeps the scan linear on huge messages


def _is_negated(text: str, start: int) -> bool:
    """Whether the clause leading up to ``start`` carries a negation."""
    preceding = text[max(0, start - _NEGATION_WINDOW) : start]
    clause_start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(preceding):
        clause_start = boundary.end()
    return bool(_NEGATION.search(preceding[clause_start:]))


def find_incomplete_signal(message: str) -> str | None:
    """Description of the unfinished-work signal in ``message``, if any."""
    if not message or not isinstance(message, str):
        return None
    text = _QUOTED_SPAN.sub(" ", message)
    for pattern, description in _COMPILED_INCOMPLETE:
        for match in pattern.finditer(text):
            if not _is_negated(text, match.start()):
                return description
    return None


# -----------------------------------------------------------------------------
# Main Hook Logic
# -----------------------------------------------------------------------------


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    if not isinstance(input_data, dict):
        # The schema always sends an object. Anything else (null, a list, a
        # bare string) must not become a traceback on the operator's screen.
        sys.exit(0)

    session_id = input_data.get("session_id")
    # The Stop payload carries the final assistant text directly. The old code
    # read `transcript`, a key Claude Code has never sent, so this check has
    # never fired once in the project's history.
    last_assistant_msg = input_data.get("last_assistant_message")
    if not isinstance(last_assistant_msg, str):
        # String-or-absent per the schema; anything else is treated as absent
        # rather than crashing the hook with a traceback on every stop.
        last_assistant_msg = ""
    # Force a continuation ONLY when the payload explicitly says no nudge has
    # happened yet. A missing or null value means we cannot tell, and the
    # safe reading is "assume we already nudged": a missed nudge costs
    # nothing, a stop hook that will not let a session end costs a lot.
    already_nudged = input_data.get("stop_hook_active") is not False

    # `stop_hook_active` is true when a previous run of this hook already forced
    # a continuation. Ignoring it is how a stop hook becomes a session that
    # cannot end: it would re-fire on every subsequent stop forever. At most one
    # nudge, then the agent is allowed to finish.
    if not already_nudged:
        signal = find_incomplete_signal(last_assistant_msg)
        if signal:
            # Claude Code's rule (from the CLI's own hook dispatcher, verified
            # on 2.1.260): a Stop hook blocks the stop with `decision: "block"`
            # or `continue: false`. `continue: true` is the default and the
            # output normaliser DROPS the key, so the previous
            # `{"continue": true, "reason": ...}` was a no-op — the session
            # ended anyway. Proven live: this shape yields a second Stop event
            # with stop_hook_active true; the old shape yielded none.
            result = {
                "decision": "block",
                "reason": (
                    f"Possible unfinished work: the closing message {signal}. "
                    "Finish it, or say explicitly why it is being left."
                ),
            }
            print(json.dumps(result))
            sys.exit(0)

    # Generate and print session summary
    try:
        summary = get_session_summary(session_id)
        print(render_session_summary(summary), file=sys.stderr)
    except Exception:
        # Don't fail the hook if summary generation fails
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
