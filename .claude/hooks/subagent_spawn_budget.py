# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Guard autonomous multi-wave pipelines against Claude Code's
per-session subagent-spawn cap.

Claude Code v2.1.212 added a default cap of 200 total Task-tool spawns per
session (override via CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION), resettable only
by /clear.

**v2.1.223 (2026-08-06) removed that platform cap.** The hook therefore checks
whether a cap is actually in effect before enforcing anything: an explicitly
set CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION is always honored, but with the
variable unset on v2.1.223+ there is no cap to guard and the hook counts
without ever pausing. An undetectable CLI version keeps the original guarding
behavior, so the failure mode is a needless pause rather than an uncontrolled
mid-wave cap failure. Without this check the hook pauses long autonomous runs
at ~170 spawns to protect against a limit the platform no longer enforces.

`/build` and `/feature` run fully autonomous multi-wave pipelines
that spawn several Task calls per wave (parallel Implementers, then CTO
review, code review, tests, security scan) and are explicitly instructed to
proceed through every wave without asking for user confirmation — nothing
before this hook ever counted cumulative spawns, so a long build could run
straight into the cap mid-wave: an uncontrolled failure with committed but
unreviewed code sitting on disk, and no way to recover autonomously (/clear
wipes the very context needed to notice and resume).

`.planning/STATE.md` is already updated after every completed task specifically
to support pause/resume. This hook turns the uncontrolled cap failure into a
clean, designed pause: it counts Task spawns per session and blocks (instead
of letting Claude Code hard-fail) once the count reaches a threshold kept
safely below the real cap, with a reason string telling the agent to stop
cleanly and tell the user to /clear then resume the pipeline.

State is keyed by session_id IN THE FILENAME (not a shared file with an
internal marker like context_monitor.json) so two sessions against the same
project root (parallel worktrees, daemon + interactive) never clobber each
other's count.

REGISTRATION NOTE: this hook must be wired into .claude/settings.json under
hooks.PreToolUse with matcher "Task" for Claude Code to actually invoke it —
see the docstring at the bottom of this file for the exact snippet.
settings.json is a humanProtected path (the daemon must never edit it), so
that registration has to be applied by a human/reviewer, not by this hook
being present alone.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from hook_utils import find_project_root
except ImportError:

    def find_project_root() -> Path | None:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            if (parent / ".claude").is_dir():
                return parent
        return None


CAP_ENV_VAR = "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"
DEFAULT_SESSION_CAP = 200
DEFAULT_RESERVE_BUFFER = 30
DEFAULT_WARN_AT_RATIO = 0.7
DEBOUNCE_SPAWNS = 5

# Claude Code v2.1.223 (2026-08-06) removed the 200-subagent-per-session cap
# this hook exists to guard: "Removed the 200-subagent-per-session spawn cap;
# long-running sessions no longer refuse new agents (concurrency and depth
# limits still apply)". On any release at or above this, pausing a build at
# ~170 spawns protects against nothing and costs a needless /clear-and-resume.
CAP_REMOVED_VERSION = (2, 1, 223)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_SAFE_SESSION_ID = re.compile(r"[^A-Za-z0-9_-]")


def _session_cap() -> int:
    """Read the real cap from Claude Code's own env var, defaulting to its
    documented default. Never hardcode just the default — a session that
    overrides the env var must not have this hook enforce a stale number."""
    raw = os.environ.get(CAP_ENV_VAR)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_SESSION_CAP


def _detect_cli_version() -> tuple[int, int, int] | None:
    """Installed Claude Code version, or None if it can't be determined.

    None is the fail-SAFE answer: an undetectable version keeps the hook's
    original guarding behavior rather than assuming the cap is gone.
    """
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _platform_cap_in_effect(state: dict) -> bool:
    """Whether a per-session spawn cap actually constrains this session.

    An explicitly set CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION is always honored:
    the operator asked for a cap, so enforce it whatever the CLI version. With
    the variable unset, the only cap is the platform's — and that was removed
    in v2.1.223, so on newer releases there is nothing to guard.

    The version lookup shells out (~0.6s), which is far too slow to repeat on
    every Task spawn, so the answer is cached in the per-session state file and
    resolved at most once per session.
    """
    if os.environ.get(CAP_ENV_VAR):
        return True

    if "platform_cap_in_effect" not in state:
        version = _detect_cli_version()
        state["detected_cli_version"] = (
            ".".join(str(p) for p in version) if version else None
        )
        # Unknown version -> assume the cap is still there (fail safe).
        state["platform_cap_in_effect"] = (
            True if version is None else version < CAP_REMOVED_VERSION
        )
    return bool(state["platform_cap_in_effect"])


def _load_config(project_root: Path) -> dict:
    """Optional tuning from gatesmith.json's `subagentSpawnBudget` block.
    Absent or malformed config is NOT an error — every field has a safe
    hardcoded default so this hook works before any config edit lands. Each
    field is type-checked independently: one bad value (e.g. a string where a
    number is expected) falls back to that field's default rather than
    raising later and fail-opening the WHOLE hard-stop check via the outer
    exception handler in main()."""
    defaults = {
        "enabled": True,
        "reserveBuffer": DEFAULT_RESERVE_BUFFER,
        "warnAtRatio": DEFAULT_WARN_AT_RATIO,
    }
    try:
        with open(project_root / "gatesmith.json") as f:
            full_config = json.load(f)
        budget_config = full_config.get("subagentSpawnBudget", {})
        if isinstance(budget_config.get("enabled"), bool):
            defaults["enabled"] = budget_config["enabled"]
        try:
            defaults["reserveBuffer"] = int(budget_config["reserveBuffer"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            defaults["warnAtRatio"] = float(budget_config["warnAtRatio"])
        except (KeyError, TypeError, ValueError):
            pass
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return defaults


def _clamped_reserve(reserve: object, cap: int) -> int:
    """Coerce and bound reserveBuffer to [0, cap-1] so a malformed or
    negative/oversized config value can never push hard_stop to or above the
    real cap (defeating the guard) or below 1."""
    try:
        value = int(reserve)
    except (TypeError, ValueError):
        value = DEFAULT_RESERVE_BUFFER
    return min(max(value, 0), max(cap - 1, 0))


def _clamped_warn_ratio(ratio: object) -> float:
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        value = DEFAULT_WARN_AT_RATIO
    return min(max(value, 0.0), 1.0)


def _state_path(project_root: Path, session_id: str) -> Path:
    """State file keyed by session_id so concurrent sessions never share a
    counter. A short hash suffix is appended (not just the sanitized string)
    so two distinct raw session_ids that sanitize to the same characters
    (e.g. differing only in punctuation mapped to '_') don't collide onto
    one file."""
    raw = session_id or "unknown"
    safe_id = _SAFE_SESSION_ID.sub("_", raw)
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return project_root / ".company" / f"subagent_spawn_budget_{safe_id}-{digest}.json"


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            with open(state_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"spawn_count": 0, "last_warning_at": 0}


def _save_state(state_path: Path, state: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(state_path.parent), suffix=".json", prefix=".subagent_budget_"
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


def _block_reason(count: int, hard_stop: int, reserve: int, cap: int) -> str:
    return (
        f"Subagent spawn budget exhausted for this session: {count} Task "
        f"spawns used, stopping at {hard_stop} (a {reserve}-spawn reserve kept "
        f"below Claude Code's {cap}-spawn session cap, which resets only via "
        "/clear). .planning/STATE.md already has the resume pointer from the "
        "last completed task, so this is a clean stopping point, not a "
        "failure: finish any in-flight wave bookkeeping that needs no more "
        "Task spawns, tell the user the build is pausing for the session's "
        "subagent-spawn budget, and ask them to run /clear then re-invoke "
        "/build (or /feature) to resume. Do NOT retry this Task call."
    )


def main() -> None:
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    # input_data may be valid JSON but not an object (null/number/list/string)
    # — .get() on those raises AttributeError, which must not escape this
    # early check and skip the fail-open wrapper below.
    if not isinstance(input_data, dict) or input_data.get("tool_name") != "Task":
        sys.exit(0)

    try:
        project_root = find_project_root()
        if project_root is None:
            sys.exit(0)

        company_dir = project_root / ".company"
        if not company_dir.exists():
            sys.exit(0)

        config = _load_config(project_root)
        if not config.get("enabled", True):
            sys.exit(0)

        session_id = input_data.get("session_id") or datetime.now(
            timezone.utc
        ).strftime("%Y%m%d%H")
        state_path = _state_path(project_root, session_id)
        state = _load_state(state_path)

        state["spawn_count"] = state.get("spawn_count", 0) + 1
        count = state["spawn_count"]

        # Nothing to guard against on a release where the cap was removed:
        # keep counting (so the state stays useful and an operator who sets
        # the env var mid-run is honored) but never pause or warn.
        if not _platform_cap_in_effect(state):
            _save_state(state_path, state)
            sys.exit(0)

        cap = _session_cap()
        reserve = _clamped_reserve(
            config.get("reserveBuffer", DEFAULT_RESERVE_BUFFER), cap
        )
        hard_stop = max(cap - reserve, 1)

        if count >= hard_stop:
            _save_state(state_path, state)
            reason = _block_reason(count, hard_stop, reserve, cap)
            print(json.dumps({"decision": "block", "reason": reason}))
            sys.exit(2)

        # Persist the increment now, independent of the warning logic below —
        # a bad warnAtRatio must not cost a lost spawn count via the outer
        # fail-open handler.
        _save_state(state_path, state)

        warn_ratio = _clamped_warn_ratio(
            config.get("warnAtRatio", DEFAULT_WARN_AT_RATIO)
        )
        warn_at = max(int(hard_stop * warn_ratio), 1)

        if (
            count >= warn_at
            and count - state.get("last_warning_at", 0) >= DEBOUNCE_SPAWNS
        ):
            remaining = hard_stop - count
            print(
                f"SUBAGENT SPAWN BUDGET WARNING: {count}/{hard_stop} Task "
                f"spawns used this session ({remaining} remaining before a "
                "designed pause). If mid-build, plan to wrap the current "
                "wave cleanly.",
                file=sys.stderr,
            )
            state["last_warning_at"] = count

        _save_state(state_path, state)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        # Fail OPEN on any unexpected bug: a PreToolUse hook on "Task" applies
        # session-wide to every subagent spawn, not just build/feature — a
        # crash here must never block all Task usage for the rest of the
        # session.
        sys.exit(0)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Registration snippet (apply by hand to .claude/settings.json — humanProtected,
# the daemon cannot write it). Add this entry to the hooks.PreToolUse array:
#
#   {
#     "matcher": "Task",
#     "hooks": [
#       {
#         "type": "command",
#         "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && uv run .claude/hooks/subagent_spawn_budget.py"
#       }
#     ]
#   }
#
# Optional tuning (apply by hand to gatesmith.json — also humanProtected):
#
#   "subagentSpawnBudget": {
#     "description": "Guards /build and /feature against Claude Code's per-session subagent-spawn cap.",
#     "enabled": true,
#     "reserveBuffer": 30,
#     "warnAtRatio": 0.7,
#     "maxParallelPerWave": 8
#   }
# ---------------------------------------------------------------------------
