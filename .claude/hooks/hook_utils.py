# /// script
# requires-python = ">=3.10"
# ///
"""
Utility functions for Gatesmith hooks.
Provides common functionality like project root detection.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def find_project_root() -> Path | None:
    """
    Find the project root by looking for .claude directory.
    Walks up from current directory and from the hook's own location.

    Returns the project root path or None if not found.
    """
    # Strategy 1: Walk up from current working directory
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent

    # Strategy 2: Walk up from the hook script's location
    # This works even if cwd is wrong
    if __file__:
        script_path = Path(__file__).resolve()
        for parent in script_path.parents:
            if (parent / ".claude").is_dir():
                return parent

    return None


# Must match employee_activator.WORKER_SCRATCH_PREFIX.
WORKER_SCRATCH_PREFIX = "fs-"


def _worker_temp_root() -> Path | None:
    """The temp directory a daemon worker may write scratch files into.

    A worker launched with a per-task scratch directory has it as
    ``FORGE_SCRATCH_DIR`` (and ``TMPDIR``). The worker OWNS that directory, so
    it could replace it with a symlink (``rmdir "$TMPDIR" && ln -s ~
    "$TMPDIR"``) and turn this allowance into "anywhere under home". It
    therefore counts only while it is a real directory, not a symlink, whose
    name has the scratch prefix and whose resolved path is its parent's
    resolved path plus that name. Otherwise there is no temp allowance at all
    (None) and scratch writes fail closed.

    Without ``FORGE_SCRATCH_DIR`` (older launches, interactive sessions) the
    process temp directory is used, as before.
    """
    scratch_env = os.environ.get("FORGE_SCRATCH_DIR")
    if not scratch_env:
        return Path(tempfile.gettempdir()).resolve()
    scratch = Path(scratch_env)
    try:
        if (
            not scratch.is_absolute()
            or scratch.is_symlink()
            or not scratch.is_dir()
            or not scratch.name.startswith(WORKER_SCRATCH_PREFIX)
        ):
            return None
        real = scratch.resolve()
        if real != scratch.parent.resolve() / scratch.name:
            return None
        return real
    except OSError:
        return None


def worker_write_escapes_root(file_path: str) -> tuple[bool, str]:
    """In a daemon worker, block a Write/Edit whose target is outside the worktree.

    ``resolve_escapes_root`` deliberately ALLOWS a lexically out-of-root path:
    an interactive session legitimately writes to a scratchpad, a memory
    directory or a sibling repo. A daemon worker does not. Its whole output
    is the diff inside its worktree; a write anywhere else is lost work at
    best and, at worst, lands in the primary checkout — which is what happened
    on 2026-09-04 (task-20260904174447-7bc916 edited the primary checkout's
    docs and silence_sentinel.py, its run ended "no PR or file changes", and
    the dirty tree stopped the daemon's git pull).

    Active only when FORGE_WORKER_CONTEXT is set (agent_providers sets it on
    every worker launch). Allowed: a target that resolves inside the project
    root, or inside the process temp directory (scratch files) as long as it
    is not inside ANOTHER worktree next to this one. Everything else, and any
    path that cannot be resolved, is blocked (fail closed).

    Returns (True, reason) to block, (False, "") to allow.
    """
    if os.environ.get("FORGE_WORKER_CONTEXT") != "1":
        return False, ""
    if not file_path:
        return False, ""
    if "\x00" in file_path:
        return True, f"NUL byte in path {file_path!r}"
    try:
        root = find_project_root()
    except Exception as exc:
        return True, f"Could not determine project root: {exc}"
    if root is None or not os.path.exists(str(root)):
        return True, "worker context but no reachable project root (failing closed)"
    try:
        real_root = Path(str(root)).resolve()
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        target = candidate.resolve()
        tmp_root = _worker_temp_root()
    except Exception as exc:
        return True, f"Could not resolve {file_path!r} (failing closed): {exc}"

    def _under(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    if _under(target, real_root):
        return False, ""
    # A sibling worktree (same parent directory as this one) is another task's
    # output; a worker must never write there even though it is under /tmp.
    if _under(target, real_root.parent) and not _under(target, real_root):
        return True, (
            f"{file_path!r} resolves to {str(target)!r}, inside a sibling of this "
            f"worktree {str(real_root)!r}"
        )
    if tmp_root is not None and _under(target, tmp_root):
        # Scratch is a plain file under the temp directory. A git working
        # tree there is not scratch: on 2026-09-05 a worker ran
        # `git worktree add` under its scratchpad, did its whole task there,
        # and its assigned worktree stayed empty. Walk from the target up to
        # the temp root; any .git (dir or worktree file) on the way means the
        # write lands in a repository that is not this worker's.
        probe = target if target.is_dir() else target.parent
        for _ in range(256):
            if (probe / ".git").exists():
                return True, (
                    f"{file_path!r} resolves into a git working tree under the "
                    f"temp directory ({str(probe)!r}); a worker's output belongs "
                    f"in its own worktree {str(real_root)!r}"
                )
            if probe == tmp_root or probe.parent == probe:
                break
            probe = probe.parent
        return False, ""
    return True, (
        f"{file_path!r} resolves to {str(target)!r}, outside this worker's "
        f"worktree {str(real_root)!r}"
    )


def run_git_hardened(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run(["git", *args]) with config-driven command execution
    neutralized (GitSpawn class): a poisoned repo/worktree git config setting
    core.fsmonitor (or a sibling key) executes arbitrary commands on an
    ordinary `status`, before any sandbox layer, because it's git itself
    running the command. -c overrides here always win over repo config;
    GIT_CONFIG_GLOBAL/SYSTEM env indirection is also blocked.

    Coverage today is status (core.fsmonitor/pager/hooksPath) plus a partial
    diff mitigation (diff.external). It does NOT cover diff.<driver>.textconv:
    the driver name comes from a `.gitattributes` `diff=<name>` declaration,
    so it's attacker-chosen and a fixed -c override can't neutralize it. A
    future caller passing `diff`/`log`/`show` must also pass `--no-textconv`
    explicitly -- there is no such call site yet, so that isn't implemented.
    """
    # `kwargs.pop("env", None) or {}` — a caller forwarding subprocess.run's
    # own default of env=None must be treated as "no extra env", not spread
    # ({**None} raises TypeError).
    extra_env = kwargs.pop("env", None) or {}
    env = {**os.environ, **extra_env, "GIT_CONFIG_NOSYSTEM": "1"}
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_SYSTEM", None)
    overrides = [
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    return subprocess.run(["git", *overrides, *args], env=env, **kwargs)


def get_hooks_dir() -> Path | None:
    """Get the .claude/hooks directory path."""
    root = find_project_root()
    if root:
        return root / ".claude" / "hooks"
    return None


def ensure_project_context():
    """
    Ensure we're operating in the correct project context.
    Changes to project root if needed.

    Returns True if context is valid, False otherwise.
    """
    root = find_project_root()
    if root is None:
        return False

    # If cwd is not the project root, change to it
    if Path.cwd() != root:
        os.chdir(root)

    return True


def get_input_json() -> dict:
    """
    Read JSON input from stdin (standard hook input).
    Returns empty dict if no input or invalid JSON.
    """
    import json

    try:
        input_data = sys.stdin.read()
        if input_data.strip():
            return json.loads(input_data)
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def resolve_escapes_root(file_path: str) -> tuple[bool, str]:
    """
    Detect a "GhostApproval" symlink escape: a ``file_path`` that *lexically*
    looks like it targets a location inside the project root, but — once
    symlinks are followed — actually lands OUTSIDE it.

    That deception is the threat (a Write/Edit to a local-looking path such as
    ``.claude/settings.json`` that is really a symlink to ``~/.ssh/...``).
    Callers must run this check unconditionally — BEFORE any
    security-profile-based enable/disable gate — and treat a truthy first
    return value as an unconditional block, regardless of profile.

    An *explicitly* out-of-root target — a path that is already outside the
    project lexically (a scratchpad under /tmp, a sibling repo reached via
    ``..``, ``~/.claude/...``) — is NOT a symlink deception. It is a
    deliberate write and is allowed here, so legitimate out-of-tree work
    (scratchpad, cross-repo edits, memory) is not broken. Normal
    tool-permission gating still applies to such writes elsewhere.

    Decision — block iff BOTH hold: (1) the write, following every symlink,
    lands OUTSIDE the root, AND (2) the path lexically CLAIMS to be inside the
    root (some ancestor directory of its lexical form IS the root). A path that
    lands INSIDE the root is safe even if it passed through a symlink ABOVE the
    root (e.g. /tmp -> /private/tmp).

    Containment is decided by inode identity (``os.path.samefile``), NOT string
    comparison: macOS APFS is case-INSENSITIVE, so a re-cased root prefix
    (``Forge-Framework`` vs ``forge-framework``) and a symlinked ancestor
    (``/tmp`` vs ``/private/tmp``) name the SAME directory and must be treated
    as such, or the guard is trivially bypassed by re-casing/re-spelling.

    Fails CLOSED (block) on: a NUL byte in the path; an unreachable project
    root; or a path that raises while being resolved. A plain symlink loop or
    over-long path that ``realpath`` merely swallows (returning an in-root
    path) is allowed — it stays in-root and the write fails on its own; it is
    not an escape. HARDLINKS cannot be caught by any path-based check (a
    hardlink alias inside root shares an outside file's inode but has no
    distinguishing path) — an inherent limitation, out of scope here.

    Returns (True, reason) to block, (False, "") to allow.
    """
    # A NUL byte is never legitimate (truncation/injection vector; also crashes
    # path resolution). Reject it first — wherever it points, even with no root.
    if file_path and "\x00" in file_path:
        return True, f"NUL byte in path {file_path!r}"

    try:
        root = find_project_root()
    except Exception as exc:
        return True, f"Could not determine project root: {exc}"

    if root is None or not file_path:
        # No project-root context, or no path — nothing to escape from.
        return False, ""

    # Must be able to identify the root to reason about containment.
    if not os.path.exists(str(root)):
        return True, f"Project root {root!r} not accessible (failing closed)"

    def _is_inside(path: Path) -> bool:
        """True if `path` is the root or nested within it, decided by inode
        identity — robust to case-insensitive filesystems and symlinked
        ancestors. Ancestors below the root are not special-cased: a symlink
        at/below the root that escapes still matches its in-root ancestor here,
        and the escape is caught by the resolve() landing check."""
        p = path
        for _ in range(256):  # bound the walk; real paths are far shallower
            try:
                if p.exists() and os.path.samefile(str(p), str(root)):
                    return True
            except OSError:
                pass
            parent = p.parent
            if parent == p:  # reached the filesystem root
                return False
            p = parent
        return False

    try:
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        real_candidate = candidate.resolve()
        # Lexical form (collapse . and .. textually, no symlink following) used
        # for the "claims inside" test below.
        lexical = Path(os.path.normpath(str(candidate)))
    except Exception as exc:
        return True, f"Could not resolve {file_path!r} (failing closed): {exc}"

    # (1) Where the write REALLY lands. Inside the project => safe, even if it
    # passed through a symlink ABOVE the root (e.g. /tmp -> /private/tmp).
    if _is_inside(real_candidate):
        return False, ""

    # Lands outside. (2) It's a GhostApproval escape iff the path lexically
    # CLAIMS to be inside the project (some ancestor of its lexical form IS the
    # root). An explicit out-of-root path claims no such ancestor and is a
    # deliberate, allowed write (scratchpad, sibling repo, ~/.claude).
    if _is_inside(lexical):
        return (
            True,
            f"{file_path!r} looks inside the project but resolves to "
            f"{real_candidate}, outside the project root",
        )

    return False, ""


# Export for use by other hooks
__all__ = [
    "find_project_root",
    "get_hooks_dir",
    "ensure_project_context",
    "get_input_json",
    "resolve_escapes_root",
    "run_git_hardened",
]
