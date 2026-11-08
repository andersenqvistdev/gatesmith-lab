# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Pre-validate files before write/edit.
Checks that we're not writing to protected paths or binary files.

Security Profile Aware:
- strict: Blocks on protected path access
- standard: Warns on protected path access
- minimal: Disabled
"""

import json
import os
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import get_exit_code, is_enabled
except ImportError:
    # Fallback if hook_config not available
    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True


# Import hook_utils for the symlink-escape guard (fundamental invariant,
# never gated behind is_enabled()/security profile).
try:
    from hook_utils import resolve_escapes_root, worker_write_escapes_root
except ImportError:
    # Fallback if hook_utils not available: fail closed — we cannot verify
    # path safety, so treat every path as an unconfirmed escape.
    def resolve_escapes_root(file_path: str) -> tuple[bool, str]:
        return True, "hook_utils unavailable; cannot verify path safety"

    def worker_write_escapes_root(file_path: str) -> tuple[bool, str]:
        # hook_utils unavailable: keep a worker inside its worktree by
        # refusing the write outright when the marker is set.
        if os.environ.get("FORGE_WORKER_CONTEXT") == "1":
            return True, "hook_utils unavailable; worker write refused (failing closed)"
        return False, ""


# Auditing must never be able to block a tool call: if this import fails the
# hook still works, it just leaves no record.
try:
    from hook_audit import record_decision
except ImportError:

    def record_decision(*_args, **_kwargs):
        return False


HOOK_NAME = "lint_on_edit"


def _is_daemon_context() -> bool:
    """Check if we're running in daemon context.

    No longer gates the humanProtected check (that block is unconditional —
    see main()). Kept for attribution/telemetry callers and for the test that
    asserts a real daemon worker env is still detectable as such.
    """
    # Daemon sets specific env vars or runs from specific paths
    return (
        os.environ.get("FORGE_DAEMON") == "1"
        or "forge_daemon" in os.environ.get("_", "")
        or os.environ.get("FORGE_EMPLOYEE_ID") is not None
    )


# Protected when gatesmith.json EXISTS but cannot be read or is malformed
# (2026-10-17 challenger round 3): returning [] there made every protected
# path writable by the agent, including the config itself.
DEFAULT_HUMAN_PROTECTED_PATHS = (
    "CLAUDE.md",
    "gatesmith.json",
    ".claude/settings.json",
    "pyproject.toml",
    "uv.lock",
    "org.json",
    ".company/org.json",
)


# The install template. install.sh writes only this file, so a fresh install
# has no root gatesmith.json; the template then carries the list.
TEMPLATE_CONFIG = ".claude/gatesmith.json"


def _ancestor_roots(start: str) -> list[str]:
    """Every ancestor of `start` (itself included) that holds a `.git`,
    nearest first. All of them count, not only the nearest: a `.git` the
    agent planted in a subdirectory would otherwise become the root whose
    config decides (2026-10-29 adversarial review of #765)."""
    probe = os.path.abspath(start)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return []
        probe = parent
    roots = []
    while True:
        if os.path.lexists(os.path.join(probe, ".git")):
            roots.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            return roots
        probe = parent


def _find_project_configs(start: str | None = None) -> list[str]:
    """The configs that govern `start` (default cwd).

    Inside a repository, each enclosing repository root contributes its
    `gatesmith.json`, else its `.claude/gatesmith.json`; a
    `gatesmith.json` below a root does not count. The template fallback
    exists because install.sh writes only the template, so a fresh install
    protected nothing (2026-10-29, found running the book's lab). Anchoring
    at roots closes a shadow: a nearest-file walk let an agent write
    `sub/gatesmith.json` with the list off, `cd sub`, and edit every
    protected file (2026-10-29 adversarial review of #763).

    Outside any repository the nearest `gatesmith.json` up from `start`
    is used, as before."""
    start = os.path.abspath(start or os.getcwd())
    roots = _ancestor_roots(start)
    if roots:
        found = []
        for root in roots:
            template = os.path.join(root, TEMPLATE_CONFIG)
            for name in ("gatesmith.json", TEMPLATE_CONFIG):
                candidate = os.path.join(root, name)
                if not os.path.lexists(candidate):
                    continue
                # A root config without a humanProtected section (one holding
                # only modelProfile) leaves the list to the template.
                if (
                    candidate != template
                    and os.path.lexists(template)
                    and not _declares_human_protected(candidate)
                ):
                    continue
                found.append(candidate)
                break
        return found
    probe = start
    while True:
        candidate = os.path.join(probe, "gatesmith.json")
        if os.path.lexists(candidate):
            return [candidate]
        parent = os.path.dirname(probe)
        if parent == probe:
            return []
        probe = parent


def _declares_human_protected(config_path: str) -> bool:
    """False only for a readable JSON object with no `humanProtected` key;
    anything unreadable still counts, so it fails closed (2026-10-30 review
    of #772)."""
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:  # noqa: BLE001 - unreadable/invalid: it governs, fail closed
        return True
    return not (isinstance(config, dict) and "humanProtected" not in config)


def _get_human_protected_paths(file_path: str | None = None) -> list[str]:
    """Load human-protected paths from gatesmith.json.

    The configs governing cwd, the target file's directory and the target's
    resolved directory all count, and their lists are combined: a session
    whose cwd is outside the repository (or in a planted `.git` directory)
    used to find no config and protect nothing (2026-10-29 adversarial
    reviews of #763 and #765).

    - No gatesmith.json in cwd or up to the repo root: [] — unchanged, a
      project that has no Gatesmith config has no humanProtected list.
    - `humanProtected.enabled` false or absent in a well-formed config: [] —
      an explicit choice, unchanged.
    - The file exists but cannot be read or parsed, its top level or the
      `humanProtected` section is not an object, `enabled` is not a boolean,
      or `paths` is not a list of strings: DEFAULT_HUMAN_PROTECTED_PATHS.
      Fail closed — a broken config must not unlock the files it protects.
    """
    configs = _find_project_configs()
    if isinstance(file_path, str) and file_path and "\0" not in file_path:
        target = os.path.abspath(os.path.expanduser(file_path))
        configs += _find_project_configs(os.path.dirname(target))
        configs += _find_project_configs(os.path.dirname(os.path.realpath(target)))
    paths: list[str] = []
    for config_path in dict.fromkeys(configs):
        own = _read_human_protected(config_path)
        # The file that holds the list is protected along with it: otherwise
        # an agent turns the list off by editing the template.
        is_template = config_path.endswith(
            os.sep + TEMPLATE_CONFIG.replace("/", os.sep)
        )
        if own and is_template:
            own = [*own, TEMPLATE_CONFIG]
        paths.extend(p for p in own if p not in paths)
    return paths


def _read_human_protected(config_path: str) -> list[str]:
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:  # noqa: BLE001 - unreadable/invalid: protect the defaults
        return list(DEFAULT_HUMAN_PROTECTED_PATHS)
    if not isinstance(config, dict):
        return list(DEFAULT_HUMAN_PROTECTED_PATHS)
    hp = config.get("humanProtected", {})
    if not isinstance(hp, dict):
        return list(DEFAULT_HUMAN_PROTECTED_PATHS)
    enabled = hp.get("enabled", False)
    if not isinstance(enabled, bool):
        return list(DEFAULT_HUMAN_PROTECTED_PATHS)
    if not enabled:
        return []
    paths = hp.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return list(DEFAULT_HUMAN_PROTECTED_PATHS)
    return paths


def _strip_dot_slash(path: str) -> str:
    """Drop a leading './' (repeated), and nothing else — a leading '.' that
    starts a dotfile name is part of the name."""
    while path.startswith("./"):
        path = path[2:]
    return path


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if file_path matches a glob-like repo-relative pattern.

    Real tool calls pass ABSOLUTE paths (e.g.
    /tmp/forge-worktrees/task-X/gatesmith.json) while humanProtected
    patterns are repo-relative ('gatesmith.json') — PR 265 review: with
    raw fnmatch the humanProtected block never fired on a real Write.
    Absolute paths are relativized against cwd and against the file's own
    repo root (walk up for .git — worktree sessions may run with another
    cwd). Deliberately NOT a suffix match: '.claude/gatesmith.json'
    (the install template) must not match the root 'gatesmith.json'
    pattern.

    Security review finding (SCOUT-20260814-1): the relative candidate must
    be normalized — an unnormalized '..' segment (e.g.
    '.claude/../gatesmith.json', which resolves to the exact protected
    file) previously fell through every candidate untouched and evaded the
    match.

    Hook audit 2026-08-31: the pattern and the candidates were normalized
    with str.lstrip("./"), which strips any run of the CHARACTERS '.' and
    '/' — the pattern '.claude/settings.json' became
    'claude/settings.json' and never matched the file by its absolute path
    (the relative candidates were mangled identically, which is why the
    relative-path test passed). Only a leading './' is stripped now.
    """
    import fnmatch
    import os

    pattern = _strip_dot_slash(pattern)
    candidates = [
        _strip_dot_slash(file_path),
        _strip_dot_slash(os.path.normpath(file_path)),
    ]
    if os.path.isabs(file_path):
        try:
            rel_cwd = os.path.relpath(file_path, os.getcwd())
            if not rel_cwd.startswith(".."):
                candidates.append(rel_cwd)
        except ValueError:
            pass
        probe = os.path.dirname(os.path.abspath(file_path))
        while probe and probe != os.path.dirname(probe):
            if os.path.exists(os.path.join(probe, ".git")):
                candidates.append(os.path.relpath(file_path, probe))
                break
            probe = os.path.dirname(probe)
    return any(fnmatch.fnmatch(c, pattern) for c in candidates)


_GLOB_CHARS = frozenset("*?[")


def _names_protected_file(file_path: str, pattern: str) -> bool:
    """Whether ``file_path`` reaches the file ``pattern`` protects by any
    spelling — what the write would actually modify, not how the path reads.

    _matches_pattern compares spellings, so on macOS's case-INSENSITIVE APFS a
    Write to `claude.md` or `.Claude/settings.json` overwrote the protected
    file, as did a Write through a symlink (`ln -s CLAUDE.md notes.md`, a
    symlinked directory), through a hard link, or to a relative
    `../repo/CLAUDE.md` (2026-10-18 challenger round 4 on #735; all of them
    passed on origin/main in every profile). Here the path is made absolute
    against cwd, every symlink is resolved, and:

    - the resolved path is matched against the pattern case-insensitively,
      relative to cwd and to its own repository root (casefolded on every
      platform — on a case-sensitive filesystem that over-protects a file
      whose name differs from a protected one only in case, never under-);
    - a literal pattern whose protected file exists is compared by inode
      (os.path.samefile), which also catches a hard link.

    Fails CLOSED: a path that cannot be resolved (a NUL byte, …) counts as a
    match."""
    import fnmatch

    try:
        target = os.path.abspath(os.path.expanduser(file_path))
        real = os.path.realpath(target)
        pattern_cf = _strip_dot_slash(pattern).casefold()
        roots = [os.path.realpath(os.getcwd())]
        for start in (real, target):
            roots += [os.path.realpath(r) for r in _ancestor_roots(start)]
        for root in roots:
            rel = os.path.relpath(real.casefold(), root.casefold())
            if not rel.startswith("..") and fnmatch.fnmatch(rel, pattern_cf):
                return True
        if not (_GLOB_CHARS & set(pattern)) and os.path.exists(target):
            for root in roots:
                protected = os.path.join(root, _strip_dot_slash(pattern))
                if os.path.exists(protected) and os.path.samefile(target, protected):
                    return True
    except (OSError, ValueError):
        return True
    return False


PROTECTED_PATHS = [
    ".git/",
    "node_modules/",
    ".env",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
]

# Files that get content-validated instead of blanket-blocked.
# Writes are allowed if the content passes validation; blocked otherwise.
VALIDATED_PATHS = {
    ".company/org.json": "_validate_org_json",
}

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pdf",
    ".doc",
    ".docx",
    ".pyc",
    ".pyo",
    ".class",
}


def _validate_org_json(file_path: str, tool_name: str, tool_input: dict) -> str | None:
    """Validate writes to org.json won't wipe employees.

    Returns None if write is safe, or an error message if it should be blocked.
    Allows: hiring (adds employees), edits that keep employees, new files.
    Blocks: writes that would reduce employee count to 0 when employees exist.
    """
    # For Edit tool, check if the edit touches the employees array destructively
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        # Block edits that replace the employees array with empty
        if '"employees"' in old and '"employees": []' in new:
            return "Edit would empty the employees array"
        # Allow other edits (e.g. changing a single field)
        return None

    # For Write tool, parse the full content and compare employee counts
    content = tool_input.get("content", "")
    try:
        new_data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return "Content is not valid JSON"

    new_employees = new_data.get("employees", [])

    # Read existing file to compare
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_employees = existing.get("employees", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # No existing file or can't read it — allow if new data has employees
        return None

    # Block: existing has employees but new content wipes them
    if len(existing_employees) > 0 and len(new_employees) == 0:
        return (
            f"Would wipe {len(existing_employees)} employees. "
            f"Use Python save_org() functions for safe programmatic modifications."
        )

    return None


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # This list only takes effect for tools .claude/settings.json's PreToolUse
    # matcher names explicitly (exact match, not a substring/regex search), so
    # the two must stay in sync. That matcher is "Write|Edit|MultiEdit" as of
    # 2026-08-19 — before then it read "Write|Edit", which left the MultiEdit
    # branch below correct but unreachable in production.
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    # Symlink-escape guard: a fundamental invariant — always blocks
    # regardless of security profile. Deliberately checked BEFORE
    # is_enabled(HOOK_NAME): that gate only governs profile-configurable
    # lint behavior (protected paths, binary files, etc.), and must never
    # be able to silently disable this path-safety net (a "minimal"
    # profile maps lint_on_edit to "off").
    # Worker scope guard: a daemon worker's output is the diff inside its
    # worktree; a write anywhere else is lost work or a stray edit in the
    # primary checkout (2026-09-04). Unconditional, like the symlink guard.
    stray, stray_reason = worker_write_escapes_root(file_path)
    if stray:
        print("", file=sys.stderr)
        print(
            f"BLOCKED: worker writes stay inside the worktree — {stray_reason}",
            file=sys.stderr,
        )
        print(
            "Use a path under the worktree (the current working directory) or under the temp directory.",
            file=sys.stderr,
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"BLOCKED: worker write outside worktree: {stray_reason}",
                    }
                }
            )
        )
        sys.exit(2)

    escapes, escape_reason = resolve_escapes_root(file_path)
    if escapes:
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Symlink Escape Detected                            ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        print(f"║  Path: {file_path[:55]:<55} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  This path resolves outside the project root (via a         ║",
            file=sys.stderr,
        )
        print(
            "║  symlink or an unverifiable target). Writes/edits that       ║",
            file=sys.stderr,
        )
        print(
            "║  escape the project root are always blocked.                 ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        record_decision(
            HOOK_NAME, "block", f"BLOCKED: Symlink escape: {escape_reason}", input_data
        )
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Symlink escape: {escape_reason}",
                }
            )
        )
        sys.exit(2)

    # Human-protected paths: block the agent's Write/Edit/MultiEdit tools from
    # touching files reserved for direct human editing. Unconditional — NOT
    # gated on the security profile either: this check used to sit after
    # `is_enabled(HOOK_NAME)`, and the "minimal" profile maps lint_on_edit to
    # "off", so `"profile": "minimal"` made every protected file writable
    # (2026-10-17 challenger round 3). Only an explicit
    # `humanProtected.enabled: false` turns it off. Nor is it
    # gated on _is_daemon_context(). An ordinary interactive session can be
    # steered by prompt injection (Kiro-class: AWS confirmed 2026-07-24 that
    # hidden webpage text got an agent to rewrite its own MCP config because
    # that file wasn't on the protected-path list) just as easily as a daemon
    # worker can, so "human" here means "not through this tool" for every
    # session type, not just unattended ones.
    human_protected = _get_human_protected_paths(file_path)
    for pattern in human_protected:
        if _matches_pattern(file_path, pattern) or _names_protected_file(
            file_path, pattern
        ):
            print("", file=sys.stderr)
            print(
                "╔══════════════════════════════════════════════════════════════╗",
                file=sys.stderr,
            )
            print(
                "║  BLOCKED: Human-Protected File                               ║",
                file=sys.stderr,
            )
            print(
                "╠══════════════════════════════════════════════════════════════╣",
                file=sys.stderr,
            )
            print(f"║  Path: {file_path[:55]:<55} ║", file=sys.stderr)
            print(
                "║                                                              ║",
                file=sys.stderr,
            )
            print(
                "║  This file is reserved for human editing only.              ║",
                file=sys.stderr,
            )
            print(
                "║  Configure in gatesmith.json → humanProtected.paths      ║",
                file=sys.stderr,
            )
            print(
                "╚══════════════════════════════════════════════════════════════╝",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            record_decision(
                HOOK_NAME,
                "block",
                f"BLOCKED: Human-protected path: {pattern}",
                input_data,
            )
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": f"BLOCKED: Human-protected path: {pattern}",
                    }
                )
            )
            sys.exit(2)

    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    # Protected paths and binary files always block (exit 2) regardless of profile
    # because these are fundamental invariants:
    # - Writing to .git/ would corrupt the repository
    # - Writing to node_modules/ could break dependencies
    # - Binary files cannot be edited as text

    # Content-validated paths: allow writes that pass validation, block otherwise.
    # This protects critical data files without preventing legitimate commands
    # like /company-hire from working.
    for pattern, _validator_name in VALIDATED_PATHS.items():
        if pattern in file_path:
            error = _validate_org_json(file_path, tool_name, tool_input)
            if error:
                print("", file=sys.stderr)
                print(
                    "╔══════════════════════════════════════════════════════════════╗",
                    file=sys.stderr,
                )
                print(
                    "║  BLOCKED: Employee Data Protection                           ║",
                    file=sys.stderr,
                )
                print(
                    "╠══════════════════════════════════════════════════════════════╣",
                    file=sys.stderr,
                )
                print(f"║  {error[:60]:<60} ║", file=sys.stderr)
                print(
                    "║                                                              ║",
                    file=sys.stderr,
                )
                print(
                    "║  org.json employees are protected from accidental wipes.    ║",
                    file=sys.stderr,
                )
                print(
                    "║  Hiring, promotions, and edits that keep employees work.    ║",
                    file=sys.stderr,
                )
                print(
                    "╚══════════════════════════════════════════════════════════════╝",
                    file=sys.stderr,
                )
                print("", file=sys.stderr)
                record_decision(HOOK_NAME, "block", f"BLOCKED: {error}", input_data)
                print(
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": f"BLOCKED: {error}",
                        }
                    )
                )
                sys.exit(2)
            # Validation passed — allow the write
            break

    # Check protected paths
    for protected in PROTECTED_PATHS:
        if protected in file_path:
            # Human-readable output to stderr
            print("", file=sys.stderr)
            print(
                "╔══════════════════════════════════════════════════════════════╗",
                file=sys.stderr,
            )
            print(
                "║  BLOCKED: Protected Path                                     ║",
                file=sys.stderr,
            )
            print(
                "╠══════════════════════════════════════════════════════════════╣",
                file=sys.stderr,
            )
            print(f"║  Path: {file_path[:54]:<54} ║", file=sys.stderr)
            print(f"║  Protected pattern: {protected:<42} ║", file=sys.stderr)
            print(
                "║                                                              ║",
                file=sys.stderr,
            )
            print(
                "║  This path is protected to prevent accidental modifications ║",
                file=sys.stderr,
            )
            print(
                "║  to critical files (git, dependencies, lock files, etc.)    ║",
                file=sys.stderr,
            )
            print(
                "╚══════════════════════════════════════════════════════════════╝",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            # JSON output to stdout
            record_decision(
                HOOK_NAME,
                "block",
                f"BLOCKED: Cannot write to protected path: {protected}",
                input_data,
            )
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": f"BLOCKED: Cannot write to protected path: {protected}",
                    }
                )
            )
            sys.exit(2)

    # Check binary files
    _, ext = os.path.splitext(file_path)
    if ext.lower() in BINARY_EXTENSIONS:
        # Human-readable output to stderr
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Binary File                                        ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        print(f"║  Path: {file_path[:54]:<54} ║", file=sys.stderr)
        print(f"║  Extension: {ext:<50} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Binary files cannot be edited as text. Use appropriate     ║",
            file=sys.stderr,
        )
        print(
            "║  tools for binary file manipulation.                         ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        # JSON output to stdout
        record_decision(
            HOOK_NAME,
            "block",
            f"BLOCKED: Cannot write to binary file: {ext}",
            input_data,
        )
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Cannot write to binary file: {ext}",
                }
            )
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
