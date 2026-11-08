# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Git Guardian — validates git operations for security.
Blocks commits with secrets, prevents pushes to protected branches,
validates commit messages, and catches large binary files.

Security Profile Aware:
- strict/standard: Blocks on security issues
- minimal: Warns only (relies on /gate checkpoint)
"""

import json
import os
import re
import shlex
import subprocess
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


# Auditing must never be able to block a tool call: if this import fails the
# hook still works, it just leaves no record.
try:
    from hook_audit import record_decision
except ImportError:

    def record_decision(*_args, **_kwargs):
        return False


HOOK_NAME = "git_guardian"

PROTECTED_BRANCHES = ["main", "master", "production", "release"]

# Max file size to allow in commits (5MB)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

SENSITIVE_PATHS = [
    r"\.env($|\.)",
    r"\.env\.local",
    r"\.env\.production",
    r"secrets/",
    r"credentials",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"id_rsa",
    r"id_ed25519",
]

# Patterns for executable syntax in commit messages (dangerous injection vectors)
# These are ACTUAL executable constructs, not descriptive text.
# NOTE: bare backticks are intentionally NOT blocked here. In a commit message a
# commit message is inert text that nothing executes, and backticks are standard
# markdown inline-code referencing `flags`, `files`, or `functions`. Blocking them
# is a false positive that forces awkward prose. The genuinely injection-shaped
# constructs below ($(), eval, here-doc piped to a shell) remain blocked.
EXECUTABLE_MESSAGE_PATTERNS = [
    (r"\$\([^)]+\)", "subshell command substitution"),  # $(command)
    (r"eval\s+[\"'\$]", "eval construct"),  # eval "...", eval $var
    (r"<<\s*\w+.*\|\s*(ba)?sh", "here-doc piped to shell"),  # << EOF | bash
]

# Patterns for secret content in staged files
SECRET_CONTENT_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"-----BEGIN\s+(?:RSA\s+|EC\s+)?PRIVATE\s+KEY-----", "Private Key"),
    (r"sk_live_[A-Za-z0-9]{24,}", "Stripe Secret Key"),
    (r"gh[pousr]_[A-Za-z0-9_]{36,}", "GitHub Token"),
    (r"sk-ant-[A-Za-z0-9_-]{40,}", "Anthropic API Key"),
    (r"sk-[A-Za-z0-9]{48,}", "OpenAI API Key"),
]


def extract_commit_message(command: str) -> str | None:
    """Extract the commit message from a git commit command.

    Returns the message text if -m or --message flag is present, None otherwise.
    Handles quoted messages correctly using shlex.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    # Find -m or --message flag
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-m" or token == "--message":
            # Next token is the message
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        elif token.startswith("-m"):
            # -m"message" or -mmessage format
            return token[2:]
        elif token.startswith("--message="):
            # --message="message" format
            return token[10:]
        i += 1

    return None


def check_message_for_executable_syntax(message: str) -> tuple[str, str] | None:
    """Check if a commit message contains executable syntax.

    Returns (pattern_name, matched_pattern) if found, None if safe.
    Only blocks actual executable constructs, NOT descriptive text.
    """
    for pattern, name in EXECUTABLE_MESSAGE_PATTERNS:
        if re.search(pattern, message):
            return (name, pattern)
    return None


def get_staged_files() -> list[str]:
    """Get list of files staged for commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_current_branch(cwd: str | None = None, timeout: float = 5) -> str:
    """Get the current git branch of the repository at ``cwd``.

    Hooks run from $CLAUDE_PROJECT_DIR, which is not necessarily the repository
    a Bash command targets (a worktree has its own HEAD), so callers that know
    the directory a command runs in must pass it. Returns "" when the branch
    cannot be read (no such directory, not a repository, timeout); callers
    guarding a push must treat "" as unknown, never as "not protected".
    """
    cmd = ["git"] + (["-C", cwd] if cwd else []) + ["rev-parse", "--abbrev-ref", "HEAD"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_staged_content(file_path: str) -> str:
    """Get the staged content of a file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def check_commit(command: str) -> dict | None:
    """Validate a git commit operation."""
    issues = []

    # Check staged files for sensitive paths
    staged_files = get_staged_files()
    for file_path in staged_files:
        for pattern in SENSITIVE_PATHS:
            if re.search(pattern, file_path, re.IGNORECASE):
                issues.append(f"Sensitive file staged: {file_path}")

    # Scan staged content for secrets
    for file_path in staged_files:
        content = get_staged_content(file_path)
        # Only scan added lines (lines starting with +)
        added_lines = [
            ln[1:]
            for ln in content.split("\n")
            if ln.startswith("+") and not ln.startswith("+++")
        ]

        for line in added_lines:
            for pattern, name in SECRET_CONTENT_PATTERNS:
                if re.search(pattern, line):
                    issues.append(f"Secret in staged content ({name}): {file_path}")
                    break  # One finding per file is enough

    # Check for large binary files
    for file_path in staged_files:
        try:
            import os

            if os.path.exists(file_path):
                size = os.path.getsize(file_path)
                if size > MAX_FILE_SIZE_BYTES:
                    size_mb = size / (1024 * 1024)
                    issues.append(
                        f"Large file ({size_mb:.1f}MB): {file_path} — consider Git LFS"
                    )
        except OSError:
            pass

    # Check commit message for executable syntax
    message = extract_commit_message(command)
    if message:
        exec_check = check_message_for_executable_syntax(message)
        if exec_check:
            name, pattern = exec_check
            # Human-readable output to stderr
            print("", file=sys.stderr)
            print(
                "╔══════════════════════════════════════════════════════════════╗",
                file=sys.stderr,
            )
            print(
                "║  BLOCKED: Executable Syntax in Commit Message               ║",
                file=sys.stderr,
            )
            print(
                "╠══════════════════════════════════════════════════════════════╣",
                file=sys.stderr,
            )
            truncated_msg = message[:50] + "..." if len(message) > 50 else message
            print(f"║  Message: {truncated_msg:<52} ║", file=sys.stderr)
            print(f"║  Found: {name:<54} ║", file=sys.stderr)
            print(
                "║                                                              ║",
                file=sys.stderr,
            )
            print(
                "║  Commit messages should be descriptive text only.           ║",
                file=sys.stderr,
            )
            print(
                "║  Remove executable syntax: backticks, $(), eval             ║",
                file=sys.stderr,
            )
            print(
                "╚══════════════════════════════════════════════════════════════╝",
                file=sys.stderr,
            )
            print("", file=sys.stderr)

            return {
                "decision": "block",
                "reason": f"GIT GUARDIAN — executable syntax in commit message:\n"
                f"  Found: {name}\n"
                f"  Message: {truncated_msg}\n\n"
                f"Commit messages should be descriptive text only.\n"
                f"Tip: Describe what you're testing, not the actual pattern.\n"
                f'  Bad:  "Fix `whoami` injection"\n'
                f'  Good: "Fix command substitution injection"',
            }

    if issues:
        # Human-readable output to stderr
        print("", file=sys.stderr)
        print(
            "╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  BLOCKED: Secret Detected in Commit                         ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        for issue in issues:
            # Truncate long issues to fit in box
            display_issue = issue[:60] if len(issue) > 60 else issue
            print(f"║  - {display_issue:<58} ║", file=sys.stderr)
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Suggestions:                                                ║",
            file=sys.stderr,
        )
        print(
            "║  1. Use environment variables for secrets                    ║",
            file=sys.stderr,
        )
        print(
            "║  2. Add sensitive files to .gitignore                        ║",
            file=sys.stderr,
        )
        print(
            "║  3. Use a secrets manager (Vault, AWS Secrets Manager)       ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print("", file=sys.stderr)

        # JSON output to stdout (unchanged)
        report = "GIT GUARDIAN — commit blocked:\n\n"
        for issue in issues:
            report += f"  - {issue}\n"
        report += "\nFix these issues before committing."
        return {"decision": "block", "reason": report}

    return None


# ---------------------------------------------------------------------------
# git push checks
#
# Two rules, deliberately not a shell model:
#
# 1. Explicit destinations. Every `git push` found anywhere in the command —
#    every segment, heredoc bodies included — is checked for a protected
#    destination, `--all`/`--mirror`/`--branches`, the `:` refspec and
#    wildcard destinations. No cwd tracking is involved.
#
# 2. Branch-dependent pushes (no refspec, or a HEAD/@ destination) are allowed
#    only in a SIMPLE command: exactly one push in the whole command,
#    preceded only by literal `cd <dir> &&` segments and plain non-branch-
#    changing git commands joined by `&&`, the push itself `git [-C <dir>]...
#    push ...`, and no variables, command substitution, subshells, eval/source
#    or heredocs anywhere. The branch is read once, in the resolved literal
#    directory; it must be readable and not protected. Everything else blocks.
# ---------------------------------------------------------------------------

# Git global options that consume the following token as their value.
GIT_GLOBAL_OPTS_WITH_VALUE = (
    "-c",
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--attr-source",
)

# ``git push`` options that consume the following token as their value.
PUSH_OPTS_WITH_VALUE = ("-o", "--push-option", "--repo", "--receive-pack", "--exec")

# ``git push`` options that push every local branch, protected ones included.
PUSH_ALL_BRANCH_OPTS = ("--all", "--mirror", "--branches")

# Git subcommands allowed before a branch-dependent push in a simple command:
# none of them can move the shell's directory or switch the checked-out branch.
SIMPLE_PREFIX_GIT_SUBCOMMANDS = frozenset(
    {"add", "commit", "status", "diff", "log", "show", "fetch", "rm", "mv"}
)

# Segment-leading words that run arbitrary code in the current shell.
_SHELL_EVALUATORS = frozenset({"eval", "source", ".", "exec"})

_SHELL_OPERATOR_CHARS = ";&|()"
_HEREDOC_DELIM = re.compile(
    r"""<<(-?)[ \t]*((?:'[^'\n]*'|"[^"\n]*"|\\?[^\s;&|()<>'"])+)"""
)
_WORD_BREAKS = " \t\n;&|()"
_NOT_LITERAL = re.compile(r"[$`*?\[]")
_MAX_HEREDOC_DEPTH = 8
_MAX_SUBST_NESTING = 64

# Characters that make a push argument impossible to judge statically:
# parameter expansion / ANSI-C quoting ($), command substitution (`), globs
# (? [) and brace expansion or reflog/upstream syntax such as @{u} ({).
_UNJUDGEABLE = re.compile(r"[$`?\[{]")

# Git's own commands. An alias can never shadow one of these, so any other
# subcommand name may be an alias and is resolved before judging.
KNOWN_GIT_SUBCOMMANDS = frozenset(
    """add am annotate apply archive bisect blame branch bugreport bundle
    cat-file check-attr check-ignore check-mailmap check-ref-format checkout
    checkout-index cherry cherry-pick citool clean clone column commit
    commit-graph commit-tree config count-objects credential daemon describe
    diagnose diff diff-files diff-index diff-tree difftool fast-export
    fast-import fetch fetch-pack filter-branch fmt-merge-msg for-each-ref
    for-each-repo format-patch fsck gc get-tar-commit-id grep gui hash-object
    help hook http-backend index-pack init instaweb interpret-trailers log
    ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base
    merge-file merge-index merge-tree mergetool mktag mktree multi-pack-index
    mv name-rev notes pack-objects pack-refs patch-id prune prune-packed pull
    push range-diff read-tree rebase receive-pack reflog remote repack replace
    request-pull rerere reset restore rev-list rev-parse revert rm scalar
    send-email send-pack shortlog show show-branch show-index show-ref
    sparse-checkout stage stash status stripspace submodule switch
    symbolic-ref tag unpack-file unpack-objects update-index update-ref
    update-server-info upload-archive upload-pack var verify-commit
    verify-pack verify-tag version whatchanged worktree write-tree lfs""".split()
)
MAX_ALIAS_LOOKUPS = 6

NOT_SIMPLE_MESSAGE = (
    "a push that depends on the current branch must be a simple command, "
    "or name its destination explicitly (git push origin <branch>)"
)


def _parse_git_globals(tokens: list[str], git_idx: int) -> tuple[int | None, dict]:
    """Walk git's global options; return (subcommand index, what they are)."""
    opts: dict = {"c_dirs": [], "other": False, "config": {}}
    i = git_idx + 1
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-"):
            return i, opts
        name, eq, attached = token.partition("=")
        if token in GIT_GLOBAL_OPTS_WITH_VALUE:
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        elif eq and name in GIT_GLOBAL_OPTS_WITH_VALUE:
            value = attached
        else:
            opts["other"] = True
            i += 1
            continue
        if name == "-C":
            opts["c_dirs"].append(value)
        else:
            opts["other"] = True
            if name == "-c" and "=" in value:
                key, _, val = value.partition("=")
                opts["config"][key.lower()] = val
        i += 1
    return None, opts


def _find_git_subcommand(
    tokens: list[str], git_idx: int
) -> tuple[int | None, list[str]]:
    """Return (index of the git subcommand, -C directories in order)."""
    sub_idx, opts = _parse_git_globals(tokens, git_idx)
    return sub_idx, opts["c_dirs"]


def extract_git_push_args(command: str) -> list[str] | None:
    """
    Extract arguments from a git push command.
    Returns None if this is not a direct git push command.
    Only matches actual 'git push' commands, not text inside messages.
    """
    try:
        # Use shlex to properly parse the command, respecting quotes
        tokens = shlex.split(command)
    except ValueError:
        # Malformed command, let it through
        return None

    # Find 'git' followed by 'push' at the start of a command
    git_idx = next((i for i, t in enumerate(tokens) if t == "git"), None)
    if git_idx is None:
        return None

    sub_idx, _ = _find_git_subcommand(tokens, git_idx)
    if sub_idx is None or tokens[sub_idx] != "push":
        return None

    # Return everything after 'push'
    return tokens[sub_idx + 1 :]


def _join_dir(base: str, target: str) -> str:
    """Resolve ``target`` (possibly relative, possibly ~) against ``base``."""
    return os.path.normpath(os.path.join(base, os.path.expanduser(target)))


class _ShellScanner:
    """Split shell text into lexable code plus embedded command texts.

    Understands quoting, ``\\`` escapes, line continuations, comments (a ``#``
    at the start of a word in code, never inside quotes), heredocs, and
    command substitution ``$( … )`` / backticks — also inside double quotes,
    where they still run. Heredoc bodies and substitution bodies are returned
    as separate texts for the caller to scan as commands; in the code they are
    replaced by the word ``$SUBST`` so the command is visibly not simple.
    """

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.bodies: list[str] = []
        self.pending: list[tuple[str, bool]] = []
        self.nesting = 0

    def code(self, closer: bool = False) -> str:
        out: list[str] = []
        depth = 0
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == "\\" and self.i + 1 < n:
                if s[self.i + 1] != "\n":
                    out.append(s[self.i : self.i + 2])
                self.i += 2
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                j = n - 1 if j < 0 else j
                out.append(s[self.i : j + 1])
                self.i = j + 1
                continue
            if c == '"':
                out.append(self._double_quoted())
                continue
            if c == "`":
                self._backticks()
                out.append("$SUBST")
                continue
            if (
                c == "$"
                and s.startswith("$(", self.i)
                and not s.startswith("$((", self.i)
            ):
                self._substitution()
                out.append("$SUBST")
                continue
            if c == "#" and (not out or out[-1][-1] in _WORD_BREAKS):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j
                continue
            if (
                c == "<"
                and s.startswith("<<", self.i)
                and not s.startswith("<<<", self.i)
            ):
                m = _HEREDOC_DELIM.match(s, self.i)
                if m:
                    delim = re.sub(r"""['"\\]""", "", m.group(2))
                    self.pending.append((delim, m.group(1) == "-"))
                    out.append(" ")
                    self.i = m.end()
                    continue
            if c == "\n":
                out.append("\n")
                self.i += 1
                self._heredoc_bodies()
                continue
            if closer:
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        self.i += 1
                        return "".join(out)
                    depth -= 1
            out.append(c)
            self.i += 1
        return "".join(out)

    def _substitution(self) -> None:
        self.i += 2
        self.nesting += 1
        if self.nesting > _MAX_SUBST_NESTING:
            raise RecursionError("command substitution nested too deeply")
        self.bodies.append(self.code(closer=True))
        self.nesting -= 1

    def _backticks(self) -> None:
        self.i += 1
        buf: list[str] = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < self.n:
                nxt = self.s[self.i + 1]
                buf.append(nxt if nxt in "`\\$" else self.s[self.i : self.i + 2])
                self.i += 2
                continue
            self.i += 1
            if c == "`":
                break
            buf.append(c)
        self.bodies.append("".join(buf))

    def _double_quoted(self) -> str:
        out = ['"']
        self.i += 1
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == "\\" and self.i + 1 < n:
                if s[self.i + 1] != "\n":
                    out.append(s[self.i : self.i + 2])
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                out.append('"')
                return "".join(out)
            if c == "`":
                self._backticks()
                out.append("$SUBST")
                continue
            if (
                c == "$"
                and s.startswith("$(", self.i)
                and not s.startswith("$((", self.i)
            ):
                self._substitution()
                out.append("$SUBST")
                continue
            out.append(c)
            self.i += 1
        return "".join(out)

    def _heredoc_bodies(self) -> None:
        s, n = self.s, self.n
        for delim, strip_tabs in self.pending:
            body: list[str] = []
            while self.i < n:
                j = s.find("\n", self.i)
                end = n if j < 0 else j
                line = s[self.i : end]
                self.i = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
                body.append(line)
            self.bodies.append("\n".join(body))
        self.pending = []


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """(lexable code, embedded heredoc/substitution bodies) — see _ShellScanner."""
    scanner = _ShellScanner(command)
    code = scanner.code()
    return code, scanner.bodies


def _lex_line_fallback(line: str) -> list[str]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=_SHELL_OPERATOR_CHARS)
        lexer.commenters = ""
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        words = re.findall(r"&&|\|\||[;&|()]|[^\s;&|()]+", line)
        return [w.strip("'\"") for w in words]


def _tokenize(text: str) -> tuple[list[tuple[str, str]], bool]:
    """Lex shell text into ("word", w) / ("op", op) items; never raises.

    Returns (items, parsed_cleanly). Unbalanced quoting falls back to a
    per-line lex so a push after the bad line is still seen.
    """
    clean = True
    try:
        lexer = shlex.shlex(
            text.replace("\n", " ; "),
            posix=True,
            punctuation_chars=_SHELL_OPERATOR_CHARS,
        )
        lexer.commenters = ""  # comments were stripped, quote-aware, above
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        clean = False
        tokens = []
        for line in text.split("\n"):
            tokens.extend(_lex_line_fallback(line))
            tokens.append(";")
    items: list[tuple[str, str]] = []
    for token in tokens:
        if not all(ch in _SHELL_OPERATOR_CHARS for ch in token):
            items.append(("word", token))
            continue
        j = 0
        while j < len(token):
            pair = token[j : j + 2]
            if pair in ("&&", "||"):
                items.append(("op", pair))
                j += 2
                continue
            items.append(("op", token[j]))
            j += 1
    # Fold redirections: `2>&1` lexes as "2>" "&" "1", `&>f` as "&" ">f".
    folded: list[tuple[str, str]] = []
    k = 0
    while k < len(items):
        kind, val = items[k]
        if kind == "op" and val == "&":
            prev = folded[-1] if folded else None
            nxt = items[k + 1] if k + 1 < len(items) else None
            if prev and prev[0] == "word" and prev[1].endswith((">", "<")):
                folded[-1] = ("word", prev[1] + "&")
                k += 1
                continue
            if nxt and nxt[0] == "word" and nxt[1].startswith((">", "<")):
                folded.append(("word", "&" + nxt[1]))
                k += 2
                continue
        folded.append((kind, val))
        k += 1
    return folded, clean


def _strip_redirections(words: list[str]) -> list[str]:
    """Drop redirections (``2>/dev/null``, ``> f``, ``main>/dev/null`` -> ``main``)."""
    out: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        m = re.search(r"[<>]", word)
        if not m:
            out.append(word)
            continue
        prefix = word[: m.start()]
        rest = word[m.start() :].lstrip("<>&")
        if not rest:  # operator only: the target is the next word
            skip_next = True
        if prefix and not prefix.isdigit() and prefix != "&":
            out.append(prefix)
    return out


class _Segment:
    __slots__ = ("words", "prev_op", "next_op")

    def __init__(self, words: list[str], prev_op: str | None, next_op: str | None):
        self.words = words
        self.prev_op = prev_op
        self.next_op = next_op


class _Text:
    """One piece of shell text (the command, or a heredoc body) as segments."""

    def __init__(self, text: str):
        items, self.clean = _tokenize(text)
        self.has_subshell = any(k == "op" and v in "()" for k, v in items)
        self.segments: list[_Segment] = []
        words: list[str] = []
        prev_op: str | None = None
        for kind, val in items + [("op", None)]:
            if kind == "word":
                words.append(val)
                continue
            if words:
                self.segments.append(_Segment(_strip_redirections(words), prev_op, val))
                words = []
            prev_op = val


class PushSite:
    """One ``git push`` found in a command."""

    __slots__ = ("text", "seg_idx", "git_idx", "sub_idx", "opts", "args", "alias")

    def __init__(
        self, text: _Text, seg_idx: int, git_idx: int, sub_idx: int, opts: dict
    ):
        self.text = text
        self.seg_idx = seg_idx
        self.git_idx = git_idx
        self.sub_idx = sub_idx
        self.opts = opts
        self.args = text.segments[seg_idx].words[sub_idx + 1 :]
        self.alias: str | None = None  # set when reached through a git alias


def _parse(command: str) -> tuple[list[_Text], bool]:
    """All shell texts in a command: the command itself plus heredoc bodies.

    Returns (texts, has_heredoc). Bodies are split recursively (bounded).
    """
    texts: list[_Text] = []
    queue = [(command, 0)]
    has_heredoc = False
    while queue:
        text, depth = queue.pop()
        if depth >= _MAX_HEREDOC_DEPTH:
            texts.append(_Text(text))
            continue
        shell, bodies = _split_heredocs(text)
        has_heredoc = has_heredoc or bool(bodies)
        texts.append(_Text(shell))
        queue.extend((b, depth + 1) for b in bodies)
    return texts, has_heredoc


def _aliases_in(d: str, cache: dict) -> dict | None:
    """All git aliases visible in directory ``d`` (one lookup per directory).

    None when the lookup budget is spent or git could not be asked; callers
    treat that as unjudgeable.
    """
    if d in cache:
        return cache[d]
    if len(cache) >= MAX_ALIAS_LOOKUPS:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", d, "config", "--get-regexp", r"^alias\."],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        cache[d] = None
        return None
    aliases: dict = {}
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        aliases[key[len("alias.") :].lower()] = value
    cache[d] = aliases
    return aliases


def _alias_value(name: str, opts: dict, dirs: list[str], cache: dict) -> str | None:
    """The definition of git alias ``name`` (inline ``-c`` first, then config)."""
    inline = opts["config"].get(f"alias.{name.lower()}")
    if inline is not None:
        return inline
    for d in dirs:
        aliases = _aliases_in(d, cache)
        if aliases is None:
            return "!unresolved"  # could not look: treat as unjudgeable
        if name.lower() in aliases:
            return aliases[name.lower()]
    return None


def _candidate_dirs(text: _Text, opts: dict, base: str) -> list[str]:
    """Where a git alias might be defined: the payload dir, literal cd targets, -C."""
    dirs = [base]
    for seg in text.segments:
        w = seg.words
        if len(w) == 2 and w[0] == "cd" and not _NOT_LITERAL.search(w[1]):
            dirs.append(_join_dir(base, w[1]))
    dirs += [_join_dir(base, c) for c in opts["c_dirs"] if not _NOT_LITERAL.search(c)]
    return list(dict.fromkeys(dirs))


def _find_sites(texts: list[_Text], base: str | None = None) -> list[PushSite]:
    base = base or os.getcwd()
    alias_cache: dict = {}
    sites: list[PushSite] = []
    for text in texts:
        for s, seg in enumerate(text.segments):
            words = seg.words
            git_idx = next(
                (i for i, w in enumerate(words) if w == "git" or w.endswith("/git")),
                None,
            )
            if git_idx is None:
                continue
            sub_idx, opts = _parse_git_globals(words, git_idx)
            if sub_idx is None:
                continue
            sub = words[sub_idx]
            if sub == "push":
                sites.append(PushSite(text, s, git_idx, sub_idx, opts))
                continue
            if sub in KNOWN_GIT_SUBCOMMANDS or _NOT_LITERAL.search(sub):
                continue
            value = _alias_value(
                sub, opts, _candidate_dirs(text, opts, base), alias_cache
            )
            if value is None:
                continue
            site = PushSite(text, s, git_idx, sub_idx, opts)
            site.alias = value
            try:
                expansion = shlex.split(value)
            except ValueError:
                expansion = []
            if value.startswith("!") or not expansion:
                site.args = ["$ALIAS"]  # a shell alias cannot be judged
            elif expansion[0] == "push":
                site.args = expansion[1:] + words[sub_idx + 1 :]
            elif expansion[0] in KNOWN_GIT_SUBCOMMANDS:
                continue  # an alias for another git command
            else:
                site.args = ["$ALIAS"]  # alias of an alias: not judged
            sites.append(site)
    return sites


def _simple_push_dir(
    texts: list[_Text], has_heredoc: bool, sites: list[PushSite], base: str
) -> tuple[str | None, str]:
    """(directory, "") when the command is simple, else (None, why not)."""
    if has_heredoc:
        return None, "the command contains a heredoc or command substitution"
    if len(sites) != 1:
        return None, "the command contains more than one git push"
    (site,) = sites
    text = site.text
    if not text.clean:
        return None, "the command could not be parsed"
    if text.has_subshell:
        return None, "the command uses a subshell or command substitution"
    for seg in text.segments:
        if any("$" in w or "`" in w for w in seg.words):
            return None, "the command uses a variable or command substitution"
        if seg.words and seg.words[0] in _SHELL_EVALUATORS:
            return None, f"the command runs `{seg.words[0]}`"
    directory = base
    for seg in text.segments[: site.seg_idx]:
        if seg.next_op != "&&":
            return None, "commands before the push must be joined with &&"
        words = seg.words
        if len(words) == 2 and words[0] == "cd" and not _NOT_LITERAL.search(words[1]):
            if words[1] == "-":
                return None, "`cd -` has no literal directory"
            if os.environ.get("CDPATH") and not words[1].startswith(("/", ".", "~")):
                return None, "CDPATH is set, so a relative `cd` is ambiguous"
            directory = _join_dir(directory, words[1])
        elif (
            len(words) >= 2
            and words[0] == "git"
            and words[1] in SIMPLE_PREFIX_GIT_SUBCOMMANDS
        ):
            continue
        else:
            return None, f"`{' '.join(words[:3])}` runs before the push"
    words = text.segments[site.seg_idx].words
    if site.alias is not None:
        return None, "the push runs through a git alias"
    if site.git_idx != 0 or words[0] != "git":
        return None, "the push is wrapped in another command"
    if site.opts["other"]:
        return None, "the push uses git options other than -C <dir>"
    for c in site.opts["c_dirs"]:
        if _NOT_LITERAL.search(c):
            return None, "-C has no literal directory"
        directory = _join_dir(directory, c)
    return directory, ""


def find_push_invocations(
    command: str, base_dir: str
) -> list[tuple[str | None, list[str]]]:
    """Locate every ``git push`` in a command (heredoc bodies included).

    Returns ``(directory, push_args)`` pairs. The directory is resolved only
    for a simple command (see module comment); otherwise it is ``None``.
    """
    texts, has_heredoc = _parse(command)
    sites = _find_sites(texts, base_dir)
    directory, _ = _simple_push_dir(texts, has_heredoc, sites, base_dir)
    return [(directory, s.args) for s in sites]


def _refspecs(push_args: list[str]) -> list[str]:
    """Return the refspecs of ``git push [options] [<repository> [<refspec>...]]``."""
    positional: list[str] = []
    i = 0
    while i < len(push_args):
        arg = push_args[i]
        if arg == "--":
            positional.extend(push_args[i + 1 :])
            break
        if arg.startswith("-"):
            # Some flags take arguments (NOT -u/--set-upstream, they're standalone)
            if arg in PUSH_OPTS_WITH_VALUE:
                i += 1
        else:
            positional.append(arg)
        i += 1
    if any(a == "--repo" or a.startswith("--repo=") for a in push_args):
        # With --repo the first positional may be a refspec or override the
        # repository; judge every positional as a destination (conservative).
        return positional
    return positional[1:]  # first positional is the remote


def _destination(refspec: str, current_branch: str) -> str:
    """The remote ref a refspec updates: ``+src:dst`` -> dst, ``HEAD``/``@`` -> branch."""
    spec = refspec.lstrip("+")
    dst = spec.split(":", 1)[1] if ":" in spec else spec
    if dst in ("HEAD", "@"):
        dst = current_branch
    return dst.removeprefix("refs/heads/")


def _needs_branch(refspec: str) -> bool:
    spec = refspec.lstrip("+")
    dst = spec.split(":", 1)[1] if ":" in spec else spec
    return dst in ("HEAD", "@")


def _protected_match(name: str) -> str | None:
    for protected in PROTECTED_BRANCHES:
        if name == protected or name.endswith(f"/{protected}"):
            return protected
    return None


def _print_block_banner(label: str, name: str) -> None:
    """Human-readable block notice to stderr."""
    width = 61 - len(f"{label}: ")
    bar = "═" * 62
    blank = f"║{' ' * 62}║"
    lines = [
        "",
        f"╔{bar}╗",
        "║  BLOCKED: Push to Protected Branch                          ║",
        f"╠{bar}╣",
        f"║  {label}: {name[:width]:<{width}} ║",
        blank,
        "║  Direct pushes to protected branches are not allowed.       ║",
        "║  Please use a feature branch and create a pull request.     ║",
        blank,
        "║  Try this instead:                                           ║",
        "║    git checkout -b feature/your-feature                      ║",
        "║    git push -u origin feature/your-feature                   ║",
        "║    gh pr create                                              ║",
        f"╚{bar}╝",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


_FIX_HINT = (
    "Create a feature branch and open a pull request instead.\n"
    "  git checkout -b feature/your-feature\n"
    "  git push origin feature/your-feature"
)


def _block_reason(label: str, name: str, reason: str) -> dict:
    _print_block_banner(label, name)
    return {"decision": "block", "reason": reason}


def _block_protected(label: str, protected: str, what: str) -> dict:
    return _block_reason(
        label,
        protected,
        f"GIT GUARDIAN — push {what} protected branch '{protected}' blocked.\n"
        + _FIX_HINT,
    )


def _block_not_simple(why: str) -> dict:
    return _block_reason(
        "Not simple",
        why,
        f"GIT GUARDIAN — push blocked: {NOT_SIMPLE_MESSAGE}.\nWhy: {why}.",
    )


def _explicit_violation(push_args: list[str]) -> dict | None:
    """Rule 1: destinations that are protected whatever the current branch."""
    for arg in push_args:
        if _UNJUDGEABLE.search(arg):
            return _block_reason(
                "Unjudgeable",
                arg,
                f"GIT GUARDIAN — push blocked: argument '{arg}' cannot be judged "
                "statically (variable, substitution, glob, brace expansion, "
                "@{...} or a git alias). Name the destination literally: "
                "git push origin <branch>",
            )
    for arg in push_args:
        if arg in PUSH_ALL_BRANCH_OPTS:
            return _block_reason(
                "Option",
                arg,
                f"GIT GUARDIAN — `git push {arg}` blocked: it pushes every local "
                "branch, protected ones included.\n" + _FIX_HINT,
            )
    for refspec in _refspecs(push_args):
        if _needs_branch(refspec):
            continue
        spec = refspec.lstrip("+")
        if spec == ":":
            return _block_reason(
                "Refspec",
                ":",
                "GIT GUARDIAN — push of refspec ':' blocked: it pushes every "
                "matching branch, protected ones included.\n" + _FIX_HINT,
            )
        dst = _destination(refspec, "")
        if "*" in dst:
            return _block_reason(
                "Refspec",
                refspec,
                f"GIT GUARDIAN — push of wildcard refspec '{refspec}' blocked: "
                "it can update a protected branch.\n" + _FIX_HINT,
            )
        protected = _protected_match(dst)
        if protected:
            return _block_protected("Branch", protected, "to")
    return None


def _branch_dependent(push_args: list[str]) -> bool:
    refspecs = _refspecs(push_args)
    return not refspecs or any(_needs_branch(r) for r in refspecs)


def check_push(command: str, cwd: str | None = None) -> dict | None:
    """Validate every git push in a command (see the rules above)."""
    if "push" not in command and "git" not in command:
        return None
    try:
        return _check_push(command, cwd)
    except (RecursionError, ValueError, IndexError) as exc:
        # A safety hook that crashes fails open; this one fails closed.
        return _block_reason(
            "Unanalysable",
            type(exc).__name__,
            "GIT GUARDIAN — push check could not analyse this command "
            f"({type(exc).__name__}); blocked. Simplify the command.",
        )


def _check_push(command: str, cwd: str | None) -> dict | None:
    base = cwd if cwd and os.path.isabs(cwd) else os.getcwd()
    texts, has_heredoc = _parse(command)
    sites = _find_sites(texts, base)
    for site in sites:
        result = _explicit_violation(site.args)
        if result:
            return result
    dependent = [s for s in sites if _branch_dependent(s.args)]
    if not dependent:
        return None

    directory, why = _simple_push_dir(texts, has_heredoc, sites, base)
    if directory is None:
        return _block_not_simple(why)
    branch = get_current_branch(directory)
    if not branch:
        return _block_not_simple(f"no branch could be read in {directory}")
    protected = _protected_match(branch)
    if protected:
        return _block_protected("Current branch", branch, "from")
    return None


def is_git_subcommand(command: str, subcommand: str) -> bool:
    """
    Check if the command is a git <subcommand> invocation.
    Properly parses the command to avoid matching text inside quotes.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    git_idx = next((i for i, t in enumerate(tokens) if t == "git"), None)
    if git_idx is None:
        return False

    sub_idx, _ = _find_git_subcommand(tokens, git_idx)
    return sub_idx is not None and tokens[sub_idx] == subcommand


def main():
    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")

    # Get exit code based on security profile
    exit_code = get_exit_code(HOOK_NAME, issue_found=True)

    # Check every git push segment (`git status; git push origin main`,
    # `gh ... && git push origin main`), before the gh skip below.
    result = check_push(command, cwd=input_data.get("cwd"))
    if result:
        # Adjust decision based on profile
        if exit_code == 1:
            result["decision"] = "warn"
        record_decision(
            HOOK_NAME,
            result["decision"],
            result.get("reason", ""),
            input_data,
            rule="git push",
        )
        print(json.dumps(result))
        sys.exit(exit_code)

    # Skip gh (GitHub CLI) commands entirely - they have their own workflows
    # and we don't want to block PR descriptions that mention git/branches
    try:
        tokens = shlex.split(command)
        if tokens and tokens[0] == "gh":
            sys.exit(0)
    except ValueError:
        pass

    # Check git commit - uses proper parsing to avoid matching text in messages
    if is_git_subcommand(command, "commit"):
        result = check_commit(command)
        if result:
            # Adjust decision based on profile
            if exit_code == 1:
                result["decision"] = "warn"
            record_decision(
                HOOK_NAME,
                result["decision"],
                result.get("reason", ""),
                input_data,
                rule="git commit",
            )
            print(json.dumps(result))
            sys.exit(exit_code)

    sys.exit(0)


if __name__ == "__main__":
    main()
