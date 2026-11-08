#!/usr/bin/env python3
"""One merge decision for every Gatesmith merge lever (single-merge-gate).

Gatesmith has had more than one lever that can merge a PR, and every past
merge-path incident was a lever that missed a gate the others enforced:
#600 (review paths missed ``.claude/hooks/``), #620/#621 (a judge REJECT
never blocked the workflow path), #681 (the daemon ignored
``allowMergeToMain``/``requireApproval``). The rebase merge queue
(``pr_rebase.run_merge_queue``) was a second ``gh pr merge`` that did not
check the author, fell back to NO blockedPaths when the key was absent,
skipped the secrets scan and the diff-size cap, and ignored a recorded
negative verdict when the deliverable gate was off.

:func:`may_merge` is the decision: it reads the PR fresh, at decision time,
and returns why the lever must not merge it (``None`` = merge, pinned with
``--match-head-commit`` to the head it judged). The gates mirror
``forge_daemon._run_auto_merge`` + ``_check_security_gates``:

1. policy  — ``autonomy.autoMerge.enabled`` and ``allowMergeToMain`` true,
   ``requireApproval`` false (malformed = refuse). The daemon's own
   approved-head merge under ``requireApproval`` stays daemon-only;
2. open, not a draft, head unchanged since the caller looked;
3. base in ``allowedBaseBranches`` and never a ``daemon/*`` branch;
4. author — a ``daemon/*`` branch opened by the account Gatesmith runs as;
5. hold labels (``needs-manual-review``, ``awaiting-operator-review``,
   ``verdict-pending``, every challenger hold, ``needs-human``,
   ``do-not-merge``);
6. challenger stage for the current head;
7. deliverable verdict for the current head (a recorded negative always
   refuses; a missing one refuses while ``deliverableGate`` is on);
8. conflict-aware — ``MERGEABLE`` and ``CLEAN``;
9. CI green on the head (no checks = gateless, refused unless
   ``allowGatelessRepos``);
10. ``blockedPaths`` (daemon default when absent) and ``reviewPaths``
    (shared seed when absent);
11. secrets scan and the diff-size cap on the diff.

The daemon's loop still runs its own copy of these checks: moving it onto
this function means editing the protected ``forge_daemon.py`` (maintenance
window), which is a follow-up, not part of this change.
"""

from __future__ import annotations

import contextlib
import fnmatch
import importlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
Runner = Callable[..., Any]

GH_TIMEOUT = 30
DIFF_TIMEOUT = 60

# forge_daemon's loader default when autonomy.autoMerge.blockedPaths is absent
# (load_config, "blockedPaths" fallback) — ci.yml uses the same three files.
DEFAULT_BLOCKED_PATHS: tuple[str, ...] = (
    "gatesmith.json",
    ".claude/settings.json",
    "CLAUDE.md",
)
DEFAULT_MAX_DIFF_LINES = 500
DEFAULT_MAX_DIFF_FILES = 20
OWN_BRANCH_PREFIX = "daemon/"

# Labels no merge lever may pass. The live sets of the modules that own a
# label are added at call time (hold_labels); this literal is the fallback so
# a broken import can never fail open into "no hold".
_HOLD_FALLBACK = frozenset(
    {
        "needs-manual-review",
        "awaiting-operator-review",
        "verdict-pending",
        "challenger:pending",
        "challenger:incomplete",
        "challenger:needs-human",
        "needs-human",
        "do-not-merge",
    }
)

# forge_daemon._get_secrets_patterns — the same seven patterns.
SECRETS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("AWS Access Key", r"AKIA[0-9A-Z]{16}"),
    ("GitHub Token", r"gh[ps]_[A-Za-z0-9]{36,}"),
    ("Private Key", r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
    ("Hardcoded Password", r"""(?:password|passwd|pwd)\s*[:=]\s*['"][^'"]{8,}['"]"""),
    (
        "Generic Secret",
        r"""(?:secret|token|api_key|apikey)\s*[:=]\s*['"][^'"]{8,}['"]""",
    ),
    ("Slack Token", r"xox[bpors]-[0-9a-zA-Z]{10,}"),
    ("Stripe Key", r"(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}"),
)

PR_FIELDS = (
    "number,state,isDraft,author,headRefName,headRefOid,baseRefName,labels,"
    "mergeable,mergeStateStatus,statusCheckRollup,files,url,isCrossRepository"
)
# A view missing any of these is incomplete and refuses (a missing ``labels``
# must never read as "no hold label").
REQUIRED_FIELDS = (
    "number",
    "state",
    "isDraft",
    "author",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "labels",
    "mergeable",
    "mergeStateStatus",
    "statusCheckRollup",
    "files",
)
_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE)


def _import(name: str):
    if __package__:
        with contextlib.suppress(ImportError):
            return importlib.import_module(f"{__package__}.{name}")
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    return importlib.import_module(name)


def _gh(
    runner: Runner | None,
    args: list[str],
    cwd: str | Path | None,
    timeout: int = GH_TIMEOUT,
):
    run = runner or subprocess.run
    try:
        return run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args, 1, "", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Policy (autonomy.autoMerge + deliverableGate.enabled)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergePolicy:
    """The parts of ``autonomy.autoMerge`` a merge lever obeys — the daemon
    loader's defaults; a malformed value takes the direction that permits
    less (never a merge on a typo)."""

    enabled: bool = True
    allow_merge_to_main: bool = True
    require_approval: bool = False
    method: str = "squash"
    allowed_base_branches: tuple[str, ...] = ("main", "master")
    blocked_paths: tuple[str, ...] = DEFAULT_BLOCKED_PATHS
    review_paths: tuple[str, ...] | None = None
    # humanProtected.requireApprovalFor, resolved by auto_merge_paths.
    approval_paths: tuple[str, ...] = ()
    allow_gateless_repos: bool = False
    secrets_scan: bool = True
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES
    max_diff_files: int = DEFAULT_MAX_DIFF_FILES
    require_deliverable_verdict: bool = True

    @classmethod
    def from_dict(cls, raw: Any, deliverable_gate: Any = None) -> MergePolicy:
        if raw is None:
            raw = {}
        elif not isinstance(raw, dict):
            # `[]`, "off" and the like are a typo, and a typo never merges
            # (challenger on #778).
            return cls(enabled=False)

        def _bool(key: str, default: bool, malformed: bool) -> bool:
            value = raw.get(key, default)
            return value if isinstance(value, bool) else malformed

        def _limit(key: str, default: int) -> int:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return default
            return value

        method = raw.get("mergeMethod", "squash")
        bases = raw.get("allowedBaseBranches", ["main", "master"])
        blocked = raw.get("blockedPaths")
        review = raw.get("reviewPaths")
        if blocked is None:
            blocked_paths = DEFAULT_BLOCKED_PATHS
        elif isinstance(blocked, list) and all(isinstance(p, str) for p in blocked):
            blocked_paths = tuple(blocked)
        else:
            # Malformed: the default list, never "nothing is blocked".
            blocked_paths = DEFAULT_BLOCKED_PATHS
        gate = deliverable_gate if isinstance(deliverable_gate, dict) else {}
        return cls(
            enabled=_bool("enabled", True, False),
            allow_merge_to_main=_bool("allowMergeToMain", True, False),
            require_approval=_bool("requireApproval", False, True),
            method=method if method in ("squash", "merge", "rebase") else "squash",
            allowed_base_branches=tuple(b for b in bases if isinstance(b, str) and b)
            if isinstance(bases, list)
            else ("main", "master"),
            blocked_paths=blocked_paths,
            review_paths=tuple(review)
            if isinstance(review, list) and all(isinstance(p, str) for p in review)
            else None,
            allow_gateless_repos=_bool("allowGatelessRepos", False, False),
            secrets_scan=_bool("secretsScan", True, True),
            max_diff_lines=_limit("maxDiffLines", DEFAULT_MAX_DIFF_LINES),
            max_diff_files=_limit("maxDiffFiles", DEFAULT_MAX_DIFF_FILES),
            require_deliverable_verdict=gate.get("enabled", True) is not False,
        )

    @property
    def can_merge(self) -> bool:
        """The daemon may merge without a human (full autonomy only)."""
        return self.enabled and self.allow_merge_to_main and not self.require_approval

    def refusal(self) -> str | None:
        if not self.enabled:
            return "autonomy.autoMerge.enabled=false"
        if not self.allow_merge_to_main:
            return "autonomy.autoMerge.allowMergeToMain=false"
        if self.require_approval:
            return "autonomy.autoMerge.requireApproval=true (a human merges)"
        return None


# Written by install.sh; the only config a fresh install has.
TEMPLATE_CONFIG = ".claude/gatesmith.json"


def _read_config_file(path: Path) -> dict | None:
    """The file as a dict; {} when absent; None when unreadable or not an
    object. A dangling symlink counts as present, so it fails closed."""
    if not (path.is_symlink() or path.exists()):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_project_config(project_root: Path) -> dict | None:
    """The config the merge policy obeys; {} when there is none; None when
    the governing file is unreadable.

    The root gatesmith.json governs when it declares `autonomy.autoMerge`
    (or a malformed `autonomy`, which then refuses). Otherwise
    the installer's `.claude/gatesmith.json` does: a fresh install has
    only the template, and reading nothing turned its `autoMerge.enabled:
    false` into the permissive defaults (found 2026-10-30; the same rule as
    Layer B's humanProtected list, #772). An unreadable root still governs.
    """
    root_cfg = _read_config_file(Path(project_root) / "gatesmith.json")
    if root_cfg is None or _declares_auto_merge(root_cfg):
        return root_cfg
    template = _read_config_file(Path(project_root) / TEMPLATE_CONFIG)
    if template is None or "autonomy" in template:
        return template
    return root_cfg


def _declares_auto_merge(config: dict) -> bool:
    """Whether a root config takes the merge policy over from the template:
    it has `autonomy.autoMerge`, or an `autonomy` that is not an object (which
    load_policy refuses). `{"autonomy": {}}` leaves the policy to the template."""
    if "autonomy" not in config:
        return False
    autonomy = config["autonomy"]
    return not isinstance(autonomy, dict) or "autoMerge" in autonomy


def load_policy(project_root: Path) -> MergePolicy:
    """The project's merge policy. An unreadable config refuses every merge."""
    data = read_project_config(project_root)
    if data is None:
        return MergePolicy(enabled=False)
    autonomy = data.get("autonomy", {})
    if not isinstance(autonomy, dict):
        # null, [], false, "" — present but not an object: refuse, never the
        # permissive defaults (challenger on #778).
        return MergePolicy(enabled=False)
    if "autoMerge" in autonomy and not isinstance(autonomy["autoMerge"], dict):
        # `autoMerge: null` or a typo: refuse rather than read it as absent.
        return MergePolicy(enabled=False)
    policy = MergePolicy.from_dict(
        autonomy.get("autoMerge"), autonomy.get("deliverableGate")
    )
    return replace(policy, approval_paths=_approval_paths(project_root))


def _approval_paths(project_root: Path) -> tuple[str, ...]:
    """requireApprovalFor from the file that governs `humanProtected`; an
    untrusted file holds everything."""
    try:
        section, error = _import("governing_config").section(
            "humanProtected", project_root
        )
    except Exception:  # noqa: BLE001 - fail closed
        return ("*",)
    if error:
        return ("*",)
    return tuple(_import("auto_merge_paths").approval_paths(section or None))


# ---------------------------------------------------------------------------
# Individual gates (pure; shared with the arm paths)
# ---------------------------------------------------------------------------


def hold_labels() -> frozenset:
    """Every label that holds a PR out of every merge lever."""
    labels = set(_HOLD_FALLBACK)
    with contextlib.suppress(Exception):
        labels |= set(_import("challenger_stage").HOLD_LABELS)
    with contextlib.suppress(Exception):
        labels.add(_import("verdict_hold").PENDING_LABEL)
    with contextlib.suppress(Exception):
        labels.add(_import("auto_merge_paths").REVIEW_HOLD_LABEL)
    return frozenset(labels)


def label_names(pr: dict) -> set[str]:
    out: set[str] = set()
    for lbl in pr.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str):
            out.add(name)
    return out


def checks_green(pr: dict) -> bool:
    """Every check on the head completed and passed (an empty rollup is not
    green — see :func:`may_merge` for the gateless case)."""
    rollup = pr.get("statusCheckRollup") or []
    if not isinstance(rollup, list) or not rollup:
        return False
    for check in rollup:
        if not isinstance(check, dict):
            return False
        if "conclusion" in check or check.get("__typename") == "CheckRun":
            if str(check.get("status") or "COMPLETED").upper() != "COMPLETED":
                return False
            if str(check.get("conclusion") or "").upper() not in (
                "SUCCESS",
                "NEUTRAL",
                "SKIPPED",
            ):
                return False
        elif str(check.get("state") or "").upper() != "SUCCESS":
            return False
    return True


def blocked_path_hits(files: list[str], policy: MergePolicy) -> list[str]:
    return [
        f for f in files if any(fnmatch.fnmatch(f, p) for p in policy.blocked_paths)
    ]


def review_path_hits(files: list[str], policy: MergePolicy) -> list[str]:
    amp = _import("auto_merge_paths")
    patterns = amp.resolve_review_paths(policy.review_paths) + list(
        policy.approval_paths
    )
    return amp.match_review_paths(files, patterns)


def diff_paths(diff_text: str) -> set[str]:
    """Every path a unified diff touches, on BOTH sides of a rename or copy.

    GitHub's ``files`` list carries only the new path of a rename, so a PR
    moving ``CLAUDE.md`` or a safety hook out of its protected path showed no
    protected path at all. Quoted (C-escaped) paths are kept verbatim minus
    the quotes; they can only add matches, never remove one."""
    out: set[str] = set()
    for line in (diff_text or "").split("\n"):
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :]
            m = re.match(r'^"?a/(.*?)"? "?b/(.*?)"?$', rest)
            if m:
                out.update(m.groups())
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            out.add(line.split(" ", 2)[2].strip('"'))
        elif line.startswith(("--- a/", "+++ b/")):
            out.add(line[6:].rstrip("\t").strip('"'))
    return {p for p in out if p}


def _same_pr(ref: str, view: dict) -> bool:
    """A URL ref names the PR the view describes (owner/repo/number)."""
    want = _PR_URL.match(ref.strip())
    got = _PR_URL.match(str(view.get("url") or ""))
    if not want or not got:
        return False
    return (
        want.group(1).lower() == got.group(1).lower()
        and want.group(2).lower() == got.group(2).lower()
        and want.group(3) == got.group(3)
    )


def current_head(pr: str, runner: Runner | None, cwd: Path | None) -> str | None:
    res = _gh(runner, ["pr", "view", str(pr), "--json", "headRefOid"], cwd)
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    head = data.get("headRefOid") if isinstance(data, dict) else None
    return str(head) if head else None


def secrets_hits(diff_text: str) -> list[str]:
    return [name for name, pat in SECRETS_PATTERNS if re.search(pat, diff_text or "")]


def diff_size(diff_text: str) -> int:
    """Added+removed lines, counted exactly as ``_check_security_gates`` does."""
    return len(
        [ln for ln in (diff_text or "").split("\n") if ln.startswith(("+", "-"))]
    )


def verdict_refusal(
    company_dir: Path, head_sha: str | None, policy: MergePolicy, project_root: Path
) -> str | None:
    """A recorded negative verdict always refuses; a missing one refuses
    while the deliverable gate is on (the daemon's ``_deliverable_verdict_gate``)."""
    if not head_sha:
        return "no head commit to match a verdict to"
    rec = _import("merge_audit").lookup_verdict_record_for_sha(company_dir, head_sha)
    vh = _import("verdict_hold")
    if rec is None:
        required = policy.require_deliverable_verdict and vh.hold_enabled(
            Path(project_root) / "gatesmith.json"
        )
        return (
            "no positive deliverable verdict for the current head" if required else None
        )
    if not vh.is_positive(rec):
        return "no positive deliverable verdict for the current head"
    return None


def viewer_login(runner: Runner | None, cwd: Path | None) -> str | None:
    res = _gh(runner, ["api", "user", "--jq", ".login"], cwd)
    login = (res.stdout or "").strip() if res.returncode == 0 else ""
    return login or None


def fetch_pr(pr: str | int, runner: Runner | None, cwd: Path | None) -> dict | None:
    res = _gh(runner, ["pr", "view", str(pr), "--json", PR_FIELDS], cwd)
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def may_merge(
    pr: str | int,
    *,
    company_dir: Path,
    project_root: Path,
    runner: Runner | None = None,
    policy: MergePolicy | None = None,
    expected_head: str | None = None,
    arming: bool = False,
) -> tuple[str | None, str | None]:
    """``(refusal, head_sha)``: why no lever may merge ``pr`` now (None =
    may merge), and the head it judged — the merge MUST be pinned to it with
    ``--match-head-commit``. ``expected_head`` refuses when the head moved
    since the caller looked. ``arming``: the decision to arm GitHub's native
    auto-merge, which itself waits for checks and a clean merge state — every
    other gate applies. Never raises: an error is a refusal."""
    try:
        return _decide(
            pr,
            company_dir=Path(company_dir),
            project_root=Path(project_root),
            runner=runner,
            policy=policy or load_policy(Path(project_root)),
            expected_head=expected_head,
            arming=arming,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return f"merge gate unavailable: {type(exc).__name__}: {exc}", None


def _decide(
    pr: str | int,
    *,
    company_dir: Path,
    project_root: Path,
    runner: Runner | None,
    policy: MergePolicy,
    expected_head: str | None,
    arming: bool = False,
) -> tuple[str | None, str | None]:
    refusal = policy.refusal()
    if refusal:
        return refusal, None
    n = str(pr).rstrip("/").rsplit("/", 1)[-1]
    view = fetch_pr(n, runner, project_root)
    if view is None:
        return "PR unreadable", None
    missing = [k for k in REQUIRED_FIELDS if k not in view]
    if missing:
        return "PR view incomplete (missing " + ", ".join(missing) + ")", None
    if "/" in str(pr) and not _same_pr(str(pr), view):
        # The view is read by number in the project's repository (whose
        # config is the policy); a URL naming another repository would have
        # this gate judge a different PR than the lever merges.
        return f"{pr} is not a PR of this project's repository", None
    head = str(view.get("headRefOid") or "") or None
    if str(view.get("state") or "").upper() != "OPEN":
        return f"PR is {view.get('state')}", head
    if not head:
        return "PR head unreadable", None
    if expected_head and head != expected_head:
        return "head moved since it was checked", head
    if view.get("isDraft"):
        return "draft", head
    if view.get("isCrossRepository") is True:
        return "fork: the head lives in another repository", head

    base = str(view.get("baseRefName") or "")
    if base.startswith(OWN_BRANCH_PREFIX) or base not in policy.allowed_base_branches:
        return f"base {base!r} is not an allowed merge target", head

    branch = str(view.get("headRefName") or "")
    if not branch.startswith(OWN_BRANCH_PREFIX):
        return f"author gate: {branch!r} is not a {OWN_BRANCH_PREFIX}* branch", head
    author = (view.get("author") or {}).get("login")
    me = viewer_login(runner, project_root)
    if not me:
        return "viewer login unreadable", head
    if author != me:
        return f"author gate: opened by {author!r}, not the daemon ({me!r})", head

    held = label_names(view) & hold_labels()
    if held:
        return "held by " + ", ".join(sorted(held)), head

    block = _import("challenger_stage").merge_block_reason(
        n, company_dir=company_dir, head_sha=head, runner=runner
    )
    if block:
        return block, head

    verdict = verdict_refusal(company_dir, head, policy, project_root)
    if verdict:
        return verdict, head

    if not arming:
        # Arming leaves these to GitHub: native auto-merge fires only once
        # the merge state is clean and the required checks pass.
        if str(view.get("mergeable") or "").upper() != "MERGEABLE":
            return f"mergeable={view.get('mergeable')}", head
        if str(view.get("mergeStateStatus") or "").upper() != "CLEAN":
            return f"mergeStateStatus={view.get('mergeStateStatus')}", head
        rollup = view.get("statusCheckRollup")
        if not rollup:
            if not policy.allow_gateless_repos:
                return "no CI checks on the head (gateless repo)", head
        elif not checks_green(view):
            return "checks not green", head

    files_raw = view.get("files")
    if not isinstance(files_raw, list):
        return "changed files unreadable", head
    listed = [
        str(f.get("path"))
        for f in files_raw
        if isinstance(f, dict) and isinstance(f.get("path"), str)
    ]
    diff = _gh(runner, ["pr", "diff", n], project_root, DIFF_TIMEOUT)
    if diff.returncode != 0:
        return "diff unreadable", head
    diff_text = diff.stdout or ""
    # Both sides of every rename/copy, plus GitHub's own list.
    files = sorted(set(listed) | diff_paths(diff_text))
    blocked = blocked_path_hits(files, policy)
    if blocked:
        return "touches blockedPaths: " + ", ".join(blocked[:5]), head
    review = review_path_hits(files, policy)
    if review:
        return "touches reviewPaths: " + ", ".join(review[:5]), head

    if policy.secrets_scan:
        secrets = secrets_hits(diff_text)
        if secrets:
            return "secrets scan: " + ", ".join(secrets), head
    lines = diff_size(diff_text)
    n_files = len(listed)
    if lines > policy.max_diff_lines or n_files > policy.max_diff_files:
        return (
            f"diff too large ({lines} lines, {n_files} files; cap "
            f"{policy.max_diff_lines}/{policy.max_diff_files})",
            head,
        )
    # The diff above must describe the head the merge is pinned to: a push
    # between the view and the diff read would have the secrets/size scan
    # judge one head and --match-head-commit merge another.
    if current_head(n, runner, project_root) != head:
        return "head moved while the PR was being checked", head
    return None, head


def merge_command(pr: str | int, policy: MergePolicy, head_sha: str) -> list[str]:
    """The only shape a gated merge takes: pinned to the judged head."""
    return [
        "gh",
        "pr",
        "merge",
        str(pr),
        f"--{policy.method}",
        "--match-head-commit",
        head_sha,
    ]
