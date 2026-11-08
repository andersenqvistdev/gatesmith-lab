# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block slopsquatted/hallucinated packages before install runs.

Closes the gap in dependency_check.py (PostToolUse, warn-only, small typosquat
regex): that hook only fires AFTER npm/pip install has already completed, and
its list can't catch "slopsquatting" — attackers pre-registering package names
that LLMs are known to hallucinate. An agent (the daemon's normal mode is
unattended) that resolves an unfamiliar dependency without checking whether it
actually exists, or how recently it was registered, can install an
attacker-controlled payload before any post-install check runs.

This hook runs BEFORE the install command executes and, for each package being
installed:
  1. Checks a small offline typosquat regex list (fast path, no network) —
     the only BLOCK signal that never depends on the network.
  2. Looks the package up on the real registry (PyPI / npm) to confirm it
     exists — a nonexistent name on the DEFAULT public registry is either a
     harmless typo (which pip/npm would reject anyway) or a hallucinated name
     an attacker hasn't squatted yet; blocking forces the agent to re-plan
     with a clear message instead of proceeding blind.
  3. If the package exists, checks how long ago it was first published. A
     very new package matching a trending hallucination is a warning signal
     even though it's real.

Install forms recognized (each anchored at a command-segment start so a literal
"pip install ..." inside `echo`/heredoc DATA is not mistaken for an invocation):
pip / pip3 / pipx / pipenv install, uv pip install, uv add, poetry add, and the
`python[3] -m pip install` / `python -m uv ...` wrappers; npm install / npm i,
yarn add, pnpm add / pnpm install. Segments are split on `;`, `&`, `|`, and
unquoted newlines. Scoped npm packages (`@scope/name`) are extracted and
checked.

Registration note: `.claude/settings.json` is human-protected, so this module
has no PreToolUse entry of its own. `block_dangerous.py` (already registered
for the Bash matcher) imports and runs `check_command` after its own dangerous-
pattern and network-egress checks — the same piggyback pattern
network_egress_guard.py uses. The module stays fully unit-testable in isolation
(see tests/test_slopsquat_check.py) and its standalone `main()` below remains
usable for direct invocation.

Custom-registry safety: when an install carries a custom-index/registry flag
(`--index-url`/`-i`/`--extra-index-url` for pip, `--registry` for npm) the
package lives on a PRIVATE registry, not public PyPI/npm. Querying the public
registry for it would 404 and wrongly block a legitimate internal install — the
exact workflow enterprise users rely on. So the network lookups (existence +
age) are SKIPPED for such segments; only the offline typosquat regex still
applies (it's registry-independent).

Fail-open on network errors by design: if the registry can't be reached
(offline dev, registry outage, timeout, malformed response), the package is
skipped rather than blocked — inability to check is not a positive block
signal, and a PreToolUse hook that hangs or wrongly blocks on network hiccups
would break normal dev workflow. Existence/age findings are the signal;
network errors are not.

Security Profile Aware (see hook_config.py):
- strict/standard: BLOCK on typosquat matches and on nonexistent packages
  (default registry only); WARN on very-recently-registered real packages.
- minimal: disabled.
"""

import json
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from hook_config import get_exit_code, is_enabled
except ImportError:  # pragma: no cover - fallback if hook_config unavailable

    def is_enabled(hook_name: str) -> bool:
        return True

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0


# Auditing must never be able to block a tool call: if this import fails the
# hook still works, it just leaves no record.
try:
    from hook_audit import record_decision
except ImportError:

    def record_decision(*_args, **_kwargs):
        return False


HOOK_NAME = "slopsquat_check"

REGISTRY_TIMEOUT_SECONDS = 3
NEW_PACKAGE_AGE_DAYS = 30
MAX_RESPONSE_BYTES = 2_000_000

# Known typosquat patterns (common misspellings of popular packages).
# Cheap offline first pass — the only block signal that never touches the
# network, so it applies even to custom-registry installs.
TYPOSQUAT_INDICATORS = [
    (r"^(?:expresss|exprss|expres|ecpress)$", "express"),
    (r"^(?:lodassh|lodahs|lod4sh)$", "lodash"),
    (r"^(?:reacct|raect|reactt)$", "react"),
    (r"^(?:requets|reqeusts|requestes)$", "requests"),
    (r"^(?:djnago|dajngo|djangoo)$", "django"),
    (r"^(?:flaskk|flaask|flsk)$", "flask"),
]

# Install invocations, anchored at a segment start (find_install_segments uses
# re.match). Longest-prefix forms are listed so the anchored match consumes the
# whole verb (e.g. `uv pip install` before a bare `pip install`).
NPM_INSTALL_PATTERNS = [
    r"npm\s+install\s+",
    r"npm\s+i\s+",
    r"yarn\s+add\s+",
    r"pnpm\s+add\s+",
    r"pnpm\s+install\s+",
]

PIP_INSTALL_PATTERNS = [
    r"uv\s+pip\s+install\s+",
    r"uv\s+add\s+",
    r"pip\s+install\s+",
    r"pip3\s+install\s+",
    r"pipx\s+install\s+",
    r"pipenv\s+install\s+",
    r"poetry\s+add\s+",
]

# Flags that take a following value (a filename, URL, host, etc.) rather than
# being a package name themselves. Without this, `pip install -r
# requirements.txt` would treat "requirements.txt" as a package name, look it
# up on PyPI, get a 404, and block one of the most common install commands.
FLAGS_WITH_VALUES = {
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "-e",
    "--editable",
    "-i",
    "--index-url",
    "--extra-index-url",
    "--trusted-host",
    "-t",
    "--target",
    "--prefix",
    "--root",
    "--cache-dir",
    "--log",
    "--build",
    "--src",
    "--platform",
    "--python-version",
    "--implementation",
    "--abi",
    "--proxy",
    "--retries",
    "--timeout",
    "--registry",
    "--tag",
    "--scope",
}

# Flags that redirect the install to a PRIVATE/custom registry. Their presence
# means the package is NOT expected on public PyPI/npm, so the public-registry
# lookups (existence + age) must be skipped to avoid false-blocking a
# legitimate internal install. The `=`-attached forms (`--index-url=...`) are
# handled by prefix match in _segment_has_custom_registry.
CUSTOM_REGISTRY_FLAGS = {
    "-i",
    "--index-url",
    "--extra-index-url",
    "--registry",
}

# A bare version fragment left over from a spaced PEP 508 specifier — e.g. the
# "2.0" in `pip install "requests >= 2.0"` after shlex strips the quotes. Pure
# digits/dots (optional trailing comma) is a version, never a distribution name
# (real digit-leading names like `2to3` contain letters and don't match).
_VERSION_ONLY_RE = re.compile(r"^\d+(?:\.\d+)*,?$")


def _base_name(part: str) -> str:
    """Reduce a package token to its base distribution name — stripping version
    specifiers (`@1.0`, `>=2`, `==1`, `~=`, `!=`), PEP 508 extras (`[extra]`),
    while PRESERVING an npm scope (`@scope/name`)."""
    if part.startswith("@"):
        # npm scoped: @scope/name[@version] — the version separator is the
        # SECOND '@' (the leading one is the scope marker, not a delimiter).
        at_idx = part.find("@", 1)
        name = part[:at_idx] if at_idx != -1 else part
        return re.split(r"[>=<~^!\[]", name)[0]
    return re.split(r"[@>=<~^!\[]", part)[0]


def extract_package_names(command: str) -> list[str]:
    """Extract package names from a single install segment.

    Expects the segment with any leading `sudo`/env-assignment/`python -m`
    prefix already removed (find_install_segments does this) so those tokens
    don't leak in as bogus package names.
    """
    # Strip the leading install verb ANCHORED at the start (the segment is
    # already prefix-stripped, so the verb is at index 0). Anchoring prevents a
    # pattern matching an inner substring — e.g. `pip install` inside
    # `uv pip install` (which would leak 'uv') or `npm install` inside
    # `pnpm install` (which would corrupt 'express' -> 'pexpress').
    for pattern in NPM_INSTALL_PATTERNS + PIP_INSTALL_PATTERNS:
        m = re.match(pattern, command)
        if m:
            command = command[m.end() :]
            break

    packages = []
    skip_next = False
    for part in command.split():
        if skip_next:
            skip_next = False
            continue
        if part.startswith("-"):
            flag = part.split("=", 1)[0]
            if flag in FLAGS_WITH_VALUES and "=" not in part:
                skip_next = True
            continue
        if _VERSION_ONLY_RE.match(part):
            # bare version fragment from a spaced specifier — not a package
            continue
        name = _base_name(part)
        if name and not name.startswith("-") and not _VERSION_ONLY_RE.match(name):
            packages.append(name)

    return packages


def _split_unquoted_newlines(command: str) -> list[str]:
    """Split on newlines that are NOT inside quotes (a newline is a real bash
    command separator). Quoted newlines (inside '...' or "...") stay as data,
    and a backslash-newline line-continuation is joined. This lets an install
    on its own line (`cd /tmp\\npip install evil`) be seen as a distinct
    command while a quoted multi-line string is not mis-split."""
    lines: list[str] = []
    buf: list[str] = []
    quote = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < n and command[i + 1] == "\n":
            i += 2  # line continuation: join
            continue
        elif c == "\n":
            lines.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    lines.append("".join(buf))
    return lines


def _split_line_segments(line: str) -> list[str]:
    """Split a single (newline-free) command line into segments on the control
    operators `;` `&` `|`, respecting quotes so a quoted DATA argument (e.g.
    text passed to `echo`) isn't mistaken for a chained command. Falls back to
    the whole line as one segment if it can't be tokenized."""
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [line]

    segments = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= {";", "&", "|"}:
            if current:
                segments.append(" ".join(current))
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(" ".join(current))
    return segments


# A heredoc operator: `<<` (not the `<<<` here-string, which has no body)
# followed by an optional `-`, optional whitespace/quote, then a delimiter word
# that STARTS with a letter or underscore. Requiring an identifier delimiter
# excludes shell arithmetic left-shift on a numeric literal (`$((1 << 8))`),
# which is the common non-heredoc use of `<<`.
_HAS_HEREDOC_RE = re.compile(r"""(?<!<)<<(?!<)-?\s*["']?[A-Za-z_]""")


def split_command_segments(command: str) -> list[str]:
    """Split a shell command into segments on `;`, `&`, `|`, and unquoted
    newlines, so each install invocation can be anchor-matched independently.

    Heredoc handling: a heredoc puts unquoted newlines inside DATA (body lines
    are not commands, and a body apostrophe would desync quote tracking), so a
    heredoc makes newline boundaries genuinely ambiguous to parse with regex.
    Rather than risk mis-reading heredoc content as an install (false-positive)
    or flattening the whole command into one bogus mega-segment, we SKIP install
    detection entirely when a real heredoc operator is present — fail open. A
    heredoc-wrapped install goes unchecked (dependency_check.py remains the
    post-install backstop); the accidental-hallucination threat this hook
    targets does not wrap its installs in heredocs, and never false-blocking a
    legitimate `install ...` + heredoc-config script matters more. Here-strings
    (`<<<`, no body) and arithmetic `<<` do not trigger this."""
    if _HAS_HEREDOC_RE.search(command):
        return []
    segments: list[str] = []
    for line in _split_unquoted_newlines(command):
        segments.extend(_split_line_segments(line))
    return segments


# `sudo npm install x` / `FOO=bar pip install x` / `python -m pip install x`
# invoke the same install — strip these leading wrappers before anchor-matching
# so they aren't missed, AND so extract_package_names never sees them as
# package names.
_LEADING_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
_LEADING_SUDO_RE = re.compile(r"^sudo\s+(?:-\S+\s+)*")
# `python`/`python3`/`python3.11`/`py` `-m` before a pip/uv install verb. The
# lookahead keeps `python -m pytest` (and anything else) untouched.
_LEADING_PYTHON_M_RE = re.compile(r"^(?:python[0-9.]*|py)\s+-m\s+(?=(?:pip|uv)\b)")


def _strip_leading_prefixes(segment: str) -> str:
    stripped = segment.lstrip()
    while True:
        new = _LEADING_SUDO_RE.sub("", stripped)
        new = _LEADING_ENV_ASSIGNMENT_RE.sub("", new)
        new = _LEADING_PYTHON_M_RE.sub("", new)
        if new == stripped:
            break
        stripped = new
    return stripped


def find_install_segments(command: str) -> list[tuple[str, str]]:
    """Return (stripped_segment, ecosystem) for each command segment that
    actually INVOKES an npm/pip install — i.e. the install pattern anchors at
    the start of the segment (after stripping `sudo`/env-var/`python -m`
    prefixes), not merely appears somewhere inside it. This is what stops
    `echo "run: npm install evil-pkg"` (data, not a command) from being treated
    the same as an actual `npm install evil-pkg` invocation.

    The returned segment is the PREFIX-STRIPPED form so downstream package
    extraction never sees `sudo`/env-assignment/`python -m` tokens (which would
    otherwise be looked up as bogus package names and false-block the install).
    """
    invocations = []
    for segment in split_command_segments(command):
        stripped = _strip_leading_prefixes(segment)
        if any(re.match(p, stripped) for p in NPM_INSTALL_PATTERNS):
            invocations.append((stripped, "npm"))
        elif any(re.match(p, stripped) for p in PIP_INSTALL_PATTERNS):
            invocations.append((stripped, "pypi"))
    return invocations


def _segment_has_custom_registry(segment: str) -> bool:
    """True if the install segment redirects to a custom/private registry
    (`--index-url`/`-i`/`--extra-index-url`/`--registry`), in which case the
    package is not expected on the public registry and network lookups must be
    skipped. Matches both space-separated (`--index-url URL`) and attached
    (`--index-url=URL`) forms."""
    for tok in segment.split():
        flag = tok.split("=", 1)[0]
        if flag in CUSTOM_REGISTRY_FLAGS:
            return True
    return False


def is_local_or_url_reference(package: str) -> bool:
    """Skip local paths, VCS refs, URLs, and build artifacts — not registry
    lookups."""
    if not package:
        return True
    if package in (".", ".."):
        return True
    if package.startswith((".", "/", "~")):
        return True
    if "://" in package or package.startswith("git+"):
        return True
    # A path (dist/foo.whl, ./x, org/repo github shorthand) — registry
    # distribution names never contain '/'. npm scoped names (@scope/name) do,
    # but they start with '@' and must NOT be treated as local.
    if "/" in package and not package.startswith("@"):
        return True
    if package.endswith((".whl", ".tar.gz", ".tar.bz2", ".tgz", ".zip")):
        return True
    return False


def check_typosquat(package_name: str) -> str | None:
    """Check if a package name looks like a typosquat."""
    for pattern, real_package in TYPOSQUAT_INDICATORS:
        if re.match(pattern, package_name, re.IGNORECASE):
            return (
                f"Package '{package_name}' looks like a typosquat of '{real_package}'"
            )
    return None


def _read_json_response(resp) -> dict | None:
    """Read a response body up to MAX_RESPONSE_BYTES and parse as JSON.

    Returns None (a non-finding, fail-open) if the body exceeds the cap or
    isn't valid JSON — a compromised/misbehaving registry response should never
    be trusted enough to block or crash the hook.
    """
    body = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    return json.loads(body.decode("utf-8"))


def fetch_pypi_metadata(package_name: str) -> dict | None | str:
    """Fetch PyPI package metadata.

    Returns the parsed JSON dict, "not_found" if the registry returned 404, or
    None if the lookup failed for any other reason (network error, timeout,
    malformed/oversized response) — a non-finding, not a block signal.
    """
    url = f"https://pypi.org/pypi/{urllib.parse.quote(package_name)}/json"
    try:
        with urllib.request.urlopen(url, timeout=REGISTRY_TIMEOUT_SECONDS) as resp:
            return _read_json_response(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "not_found"
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def fetch_npm_metadata(package_name: str) -> dict | None | str:
    """Fetch npm package metadata.

    Returns the parsed JSON dict, "not_found" if the registry returned 404, or
    None if the lookup failed for any other reason.
    """
    encoded = urllib.parse.quote(package_name, safe="@")
    url = f"https://registry.npmjs.org/{encoded}"
    try:
        with urllib.request.urlopen(url, timeout=REGISTRY_TIMEOUT_SECONDS) as resp:
            return _read_json_response(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "not_found"
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def pypi_first_release_age_days(data: dict) -> int | None:
    """Days since the earliest release upload on PyPI, or None if unknown."""
    releases = data.get("releases", {})
    upload_times = []
    for files in releases.values():
        for f in files:
            ts = f.get("upload_time_iso_8601")
            if ts:
                upload_times.append(ts)

    if not upload_times:
        return None

    try:
        earliest = min(
            datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in upload_times
        )
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    return (now - earliest).days


def npm_first_release_age_days(data: dict) -> int | None:
    """Days since npm package creation, or None if unknown."""
    created = data.get("time", {}).get("created")
    if not created:
        return None

    try:
        earliest = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    return (now - earliest).days


def evaluate_package(
    package: str, ecosystem: str, skip_registry_lookup: bool = False
) -> dict | None:
    """Check a single package. Returns a finding dict or None if clean.

    Finding shape: {"package": str, "severity": "block"|"warn", "reason": str}

    The offline typosquat check always runs. The public-registry lookups
    (existence + age) are skipped when skip_registry_lookup is True (custom/
    private registry) — querying public PyPI/npm for a private package would
    404 and false-block a legitimate internal install.
    """
    typo_reason = check_typosquat(package)
    if typo_reason:
        return {"package": package, "severity": "block", "reason": typo_reason}

    if is_local_or_url_reference(package):
        return None

    if skip_registry_lookup:
        return None

    fetch = fetch_pypi_metadata if ecosystem == "pypi" else fetch_npm_metadata
    result = fetch(package)

    if result is None:
        # Registry unreachable — fail open, not a finding.
        return None

    if result == "not_found":
        return {
            "package": package,
            "severity": "block",
            "reason": (
                f"Package '{package}' does not exist on {ecosystem}. "
                "This may be a hallucinated dependency name — verify it before "
                "installing (see slopsquatting: attackers pre-register names "
                "LLMs are known to hallucinate)."
            ),
        }

    age_days = (
        pypi_first_release_age_days(result)
        if ecosystem == "pypi"
        else npm_first_release_age_days(result)
    )
    if age_days is not None and age_days < NEW_PACKAGE_AGE_DAYS:
        return {
            "package": package,
            "severity": "warn",
            "reason": (
                f"Package '{package}' was first published {age_days} day(s) ago "
                f"(< {NEW_PACKAGE_AGE_DAYS}). Newly-registered packages are a "
                "common slopsquatting pattern — verify this is the package you "
                "intended before trusting it."
            ),
        }

    return None


def evaluate_command(command: str) -> list[dict]:
    """Evaluate every package in every install segment of a command and return
    the list of findings (may be empty). Custom-registry segments skip the
    network lookups."""
    findings = []
    for segment, ecosystem in find_install_segments(command):
        skip_lookup = _segment_has_custom_registry(segment)
        for pkg in extract_package_names(segment):
            finding = evaluate_package(pkg, ecosystem, skip_registry_lookup=skip_lookup)
            if finding:
                findings.append(finding)
    return findings


def format_report(findings: list[dict]) -> str:
    lines = ["SLOPSQUAT CHECK:"]
    for finding in findings:
        tag = "BLOCK" if finding["severity"] == "block" else "WARN"
        lines.append(f"  [{tag}] {finding['reason']}")
    return "\n".join(lines)


def print_report(command: str, findings: list[dict]) -> None:
    """Print the findings report to stderr, mirroring the other piggybacked
    hooks. Called by block_dangerous.py."""
    print(format_report(findings), file=sys.stderr)


def check_command(command: str):
    """Piggyback entry point invoked by block_dangerous.py.

    Returns None when the command is clean or isn't an install; otherwise
    (decision, result_dict, findings) where decision is:
      - "block": at least one block-severity finding (typosquat, or a
        nonexistent package on the default registry),
      - "warn": only warn-severity findings (recently-registered real package).
    The caller (block_dangerous.main) decides the exit code from `decision`.
    """
    findings = evaluate_command(command)
    if not findings:
        return None

    block_findings = [f for f in findings if f["severity"] == "block"]
    if block_findings:
        reason = "; ".join(f["reason"] for f in block_findings)
        return ("block", {"decision": "block", "reason": reason}, findings)

    return ("warn", {"decision": "warn"}, findings)


def main():
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    command = input_data.get("tool_input", {}).get("command", "")

    result = check_command(command)
    if result is None:
        sys.exit(0)

    decision, result_dict, findings = result
    print_report(command, findings)
    record_decision(
        HOOK_NAME,
        decision,
        result_dict.get("reason", ""),
        input_data,
        rule=", ".join(str(f.get("package", "")) for f in (findings or [])),
    )

    if decision == "block":
        print(json.dumps(result_dict))
        sys.exit(get_exit_code(HOOK_NAME, issue_found=True))

    # Only warn-severity findings: never escalate past exit 1, regardless of
    # profile — age is a signal, not proof, of a squatted package.
    sys.exit(min(get_exit_code(HOOK_NAME, issue_found=True), 1))


if __name__ == "__main__":
    main()
