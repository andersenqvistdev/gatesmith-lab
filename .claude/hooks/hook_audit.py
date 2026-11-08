# /// script
# requires-python = ">=3.10"
# ///
"""Record hook block/warn decisions to the gate audit trail.

Until now no blocking hook wrote its denials anywhere. block_dangerous,
git_guardian, secrets_scanner, lint_on_edit, network_egress_guard and
slopsquat_check all printed a reason to stdout/stderr and exited 2, and that was
the entire record. Once the session scrolled, a denial left no trace at all —
so "what has been blocked, and was it right?" was not a question anyone could
answer, and a block caused by an infrastructure failure was indistinguishable
from a real security decision after the fact.

Records go to ``logs/gates.jsonl``, the file audit_exporter.py already reads and
which had never existed. Human approvals from /gate and automated hook decisions
share that file and are told apart by ``source``. The exporter's approval count
only counts approvals, so adding denials here cannot distort it.

What a record holds — and deliberately does not
-----------------------------------------------
A Bash command is never stored. The record carries the program it ran
(``argv[0]``) and the first 16 hex chars of the command's SHA-256 for
correlation. The review of 2026-09-04 put twelve credential shapes through a
real egress block — Bearer and Basic headers, ``curl -u``, ``PGPASSWORD=``,
``AWS_SECRET_ACCESS_KEY=``, ``mysql -p``, a JSON body, ``?key=``, a private-key
body — and every one landed verbatim in this file and then in the compliance
pack. Redaction by pattern cannot be made complete; not storing the text can.
``_redact`` remains as defence in depth for the reason field, which is prose a
hook composed and may quote what it saw.

Write and Edit records keep the file path: a path is what the decision was
about, and it carries no content.

Where a record goes
-------------------
The project root is resolved from CLAUDE_PROJECT_DIR, then this file's own
location, then cwd. A daemon worker runs inside a git worktree under
/tmp/forge-worktrees with CLAUDE_PROJECT_DIR pointing there, and a record
written inside it dies with ``git worktree remove`` — the worker's denials are
the ones this trail exists for. So a resolved root that is a worktree routes
to the primary checkout (the parent of the repository's common git dir) and the
record names the worktree it came from.

Nothing in this module may raise or block. A failure to write an audit record is
never a reason to fail a tool call — it is logged to stderr and dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Long reasons, rules and paths are truncated: this is an index of what was
# decided, not a copy of the payload.
MAX_FIELD = 500
# A program name is short; a very long first word is not a program.
MAX_ARGV0 = 120
# A hook must not hang on git: a worktree lookup that takes longer than this
# is treated as "not a worktree".
GIT_TIMEOUT_SECONDS = 3


def _project_root() -> Path:
    """Resolve the project root without trusting cwd.

    Same precedence as log_activity: CLAUDE_PROJECT_DIR, then this file's own
    location, then cwd. Preferring cwd is what scattered this project's audit
    trail into other repositories.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and (Path(env_root) / ".claude").is_dir():
        return Path(env_root)
    for parent in Path(__file__).resolve().parents:
        if (parent / ".claude").is_dir():
            return parent
    return Path.cwd()


def _write_root(root: Path) -> tuple[Path, str | None]:
    """Where the record is written, and the worktree it came from, if any.

    Inside a git worktree the repository's common git dir is the primary
    checkout's ``.git``; its parent is where the trail must live so it outlives
    the worktree. Anything unexpected — not a repository, an old git without
    ``--path-format``, a timeout, a bare or submodule layout — keeps ``root``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return root, None
        common_dir = Path(result.stdout.strip())
        if common_dir.name != ".git":
            return root, None
        primary = common_dir.parent
        if primary.resolve() == root.resolve():
            return root, None
        return primary, str(root)
    except Exception:
        return root, None


# Credential shapes, redacted from the prose fields before anything is written.
# A reason is composed by a hook and may quote what it saw: secrets_scanner's
# full report quotes the offending line (its call site passes a summary
# instead). This is the second layer for that field, not a substitute for the
# rule above that a command is never stored.
_SECRET_SHAPES = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{12,16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    # A credential-shaped assignment: keep the name, drop the value.
    re.compile(
        r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|access[_-]?key)"
        r"\s*[=:]\s*)['\"]?[A-Za-z0-9/+=_-]{12,}['\"]?"
    ),
    re.compile(r"(?i)\b(://[^:@/\s]+:)[^@/\s]{4,}(?=@)"),  # URL userinfo password
]

# NAME=value before the program: for ``PGPASSWORD=... psql`` the program is
# psql and the first word is the secret.
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _redact(text: str) -> str:
    """Remove credential-shaped substrings before they reach the log."""
    for shape in _SECRET_SHAPES:
        if shape.groups:
            text = shape.sub(r"\1[REDACTED]", text)
        else:
            text = shape.sub("[REDACTED]", text)
    return text


def _truncate(value: object) -> str:
    text = "" if value is None else str(value)
    text = _redact(text)
    if len(text) > MAX_FIELD:
        return text[:MAX_FIELD] + f"... [{len(text) - MAX_FIELD} more chars]"
    return text


def _argv0(command: str) -> str:
    """The program a Bash command runs, and nothing else."""
    try:
        words = shlex.split(command)
    except ValueError:  # unbalanced quotes: the shell would reject it too
        words = command.split()
    for word in words:
        if _ENV_ASSIGNMENT.match(word):
            continue
        return word[:MAX_ARGV0]
    return ""


def _target_of(tool_name: str, tool_input: object) -> str:
    """The thing the decision was about, for the record's ``command`` field.

    For Bash that is the program only — see the module docstring for why the
    command text is never stored. For Write/Edit it is the file path.
    """
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return _truncate(_argv0(str(tool_input.get("command", "") or "")))
    return _truncate(tool_input.get("file_path") or tool_input.get("path") or "")


def _digest_of(tool_name: str, tool_input: object) -> str | None:
    """A short SHA-256 of the Bash command, so records can be correlated with
    a transcript without the command itself being in the file."""
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", "")
    if not command:
        return None
    return hashlib.sha256(str(command).encode("utf-8", "replace")).hexdigest()[:16]


def record_decision(
    hook: str,
    decision: str,
    reason: str,
    input_data: dict | None = None,
    rule: str | None = None,
) -> bool:
    """Append one hook decision to logs/gates.jsonl.

    Args:
        hook: the hook making the decision, e.g. "block_dangerous".
        decision: "block", "warn" or "ask".
        reason: why, in the words the operator was shown.
        input_data: the raw hook payload, for tool/session/mode context.
        rule: the specific pattern or rule id that matched, when there is one.

    Returns True when a record was written. Never raises.
    """
    try:
        payload = input_data if isinstance(input_data, dict) else {}
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "hook",
            "hook": hook,
            "decision": decision,
            "reason": _truncate(reason),
            "tool": tool_name,
            "command": _target_of(tool_name, tool_input),
            "session_id": payload.get("session_id", ""),
            "permission_mode": payload.get("permission_mode", ""),
        }
        digest = _digest_of(tool_name, tool_input)
        if digest:
            record["command_sha256"] = digest
        if rule:
            record["rule"] = _truncate(rule)

        write_root, worktree = _write_root(_project_root())
        if worktree:
            record["worktree"] = worktree

        log_dir = write_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record) + "\n"

        # Workers run in parallel and all append to one file. Lock where the
        # platform allows it; a record is small enough that a plain append is
        # atomic in practice, so a missing fcntl is not a reason to skip the
        # write.
        with open(log_dir / "gates.jsonl", "a", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            handle.write(line)
        return True
    except Exception as exc:  # never let auditing break a tool call
        print(f"hook_audit: could not record decision ({exc})", file=sys.stderr)
        return False
