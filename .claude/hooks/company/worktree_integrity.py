# /// script
# requires-python = ">=3.10"
# ///
"""ChainDrop-class defense: verify a freshly created worktree's Claude Code
hook configuration is unmodified before any coding session starts there.

A SessionStart hook in `.claude/settings.json` auto-executes the instant a
Claude Code session opens in a directory -- before any prompt is sent. The
2026-08-04 "ChainDrop" npm supply-chain worm used exactly this mechanism (a
planted SessionStart command) to harvest tokens from developer machines and
CI runners. `.mcp.json` is the same class of risk: it declares MCP server
launch commands that start automatically, and Gatesmith's worker sessions run
with permission prompts bypassed, so a planted MCP server command would also
auto-execute unreviewed. Gatesmith creates a fresh git worktree per task and
starts a coding session inside it; this module is the gate that must run
immediately after worktree creation and before that session starts.

The anchor is the worktree's OWN HEAD -- `git status --porcelain` scoped to
the guarded paths, run inside the worktree.

An earlier version of this module anchored on the primary checkout's working
tree instead, reasoning that `.claude/settings.json` is human-protected and
therefore a stronger trust anchor than any git ref. That inverts: human-
protected means those files carry permanent *uncommitted* local edits, which
is precisely how this repo is operated. Worktrees are created from `main` and
get the committed content, so the two can never match. Measured 2026-08-15 on
a clean worktree cut from main: `ok=False` with three diverging files, which
would have failed 100% of task executions -- and reported it as a security
violation rather than a configuration mismatch, sending whoever debugged it
hunting for an intrusion.

Anchoring on the worktree's own HEAD is also a tighter fit to the actual
threat. The worktree is created from a reviewed ref seconds earlier; the
attack is something *modifying it after checkout*. That is exactly what
`git status` reports, it cannot false-positive on the primary checkout's
state, and it costs one git call instead of hashing every file under
`.claude/hooks/` on every task.

Untracked files are included (`--untracked-files=all`): planting a NEW file
is at least as likely as editing one. Ignored files are deliberately NOT
included -- `__pycache__` and friends live under `.claude/hooks/` and would
false-positive on every run. A hook file only auto-executes if
`.claude/settings.json` or `.mcp.json` references it, and those are tracked,
so the reference itself gets caught.

`.claude/settings.local.json` is the exception that the git-status check
cannot see and that DOES execute on its own: it is gitignored by design, so
`git status` never reports it at any `-u` level, and Claude Code reads it at
session start like any other settings file. An attack-simulation run on
2026-08-16 caught exactly this -- four planted-file vectors were blocked and
this one sailed through. It is handled by presence instead of by diff: a
worktree is created from a ref seconds earlier and nothing in Gatesmith copies a
local settings file into it, so a `settings.local.json` existing there at all
is anomalous.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Import from the parent .claude/hooks dir (GitSpawn-hardened git invocation:
# a poisoned core.fsmonitor must not execute before this gate can report it).
sys.path.insert(0, str(Path(__file__).parent.parent))
from hook_utils import run_git_hardened  # noqa: E402

# Pathspecs handed to `git status`. Anything that can auto-execute when a
# session opens belongs here; nothing else does, so ordinary source edits in
# the worktree never trip the gate.
GUARDED_PATHSPECS = (
    ".claude/settings.json",
    ".mcp.json",
    ".claude/hooks",
)

# Auto-executing config that git will never report because it is ignored by
# design. Checked by EXISTENCE, not by diff -- see the module docstring.
FORBIDDEN_IN_WORKTREE = (".claude/settings.local.json",)

_VIOLATIONS_LOG_REL = Path(".company/state/worktree_integrity_violations.jsonl")

_GIT_TIMEOUT_SECONDS = 30


@dataclass
class IntegrityResult:
    ok: bool
    reason: str = ""
    diverging_files: list[str] = field(default_factory=list)


def _parse_porcelain_paths(stdout: str) -> list[str]:
    """Extract paths from `git status --porcelain` v1 output.

    Each line is `XY PATH`, or `XY ORIG -> PATH` for a rename/copy. Paths
    containing special characters are quoted by git; they are returned as
    git printed them, which is fine for an audit record.
    """
    paths: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        # Rename/copy: report the destination, which is the file that would
        # actually be read at session start.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip()
        if entry:
            paths.append(entry)
    return sorted(set(paths))


def verify_worktree_integrity(worktree_root: Path) -> IntegrityResult:
    """Check that nothing has touched the guarded paths since checkout.

    Must run before any session starts in `worktree_root` -- once an agent
    begins working there, legitimate edits (e.g. a task that changes a hook)
    would trip a later re-check.

    Never raises. Any failure to *establish* integrity -- git missing, a
    non-zero exit, a timeout -- returns ok=False so callers fail CLOSED. An
    unverifiable worktree is treated the same as a modified one.
    """
    try:
        proc = run_git_hardened(
            [
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                *GUARDED_PATHSPECS,
            ],
            cwd=str(worktree_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return IntegrityResult(
            ok=False,
            reason=f"worktree integrity check could not run: {exc}",
        )

    if proc.returncode != 0:
        return IntegrityResult(
            ok=False,
            reason=(
                "worktree integrity check failed: git status exited "
                f"{proc.returncode}: {(proc.stderr or '').strip()[:200]}"
            ),
        )

    diverging = _parse_porcelain_paths(proc.stdout)

    # Gitignored auto-executing config git status can never surface.
    for rel in FORBIDDEN_IN_WORKTREE:
        try:
            if (Path(worktree_root) / rel).exists():
                diverging.append(rel)
        except OSError:
            # Unreadable is unverifiable, which fails closed like everything
            # else in this function.
            diverging.append(rel)

    if not diverging:
        return IntegrityResult(ok=True)

    return IntegrityResult(
        ok=False,
        reason=(
            "worktree .claude/settings.json, .claude/settings.local.json, "
            ".mcp.json, or .claude/hooks/ was modified or planted after "
            "checkout -- refusing to start a session there (ChainDrop-class "
            "hook-injection defense)"
        ),
        diverging_files=sorted(set(diverging)),
    )


def log_integrity_violation(
    repo_root: Path, task_id: str, worktree_path: Path, result: IntegrityResult
) -> None:
    """Append a JSONL audit record for a detected violation. Never raises."""
    try:
        path = Path(repo_root) / _VIOLATIONS_LOG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "worktree_path": str(worktree_path),
            "reason": result.reason,
            "diverging_files": result.diverging_files,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass
