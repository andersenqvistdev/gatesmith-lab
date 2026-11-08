#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
gatesmith-doctor — First-run preflight checks for known Gatesmith gotchas.

Checks:
  1. claude on PATH
  2. Claude Code workspace trust accepted
  3. gh CLI authenticated
  4. .claude/settings.json hooks registered and compile clean
  5. .gitignore excludes __pycache__ and *.pyc
  6. gatesmith.json parses and autoMerge policy is explicit
  7. .company/vision.md has a parseable goal table (if company is founded)
  8. .company/state/ and .company/logs/ exist and are writable
  9. If allowMergeToMain=true, base branch has protection with required status checks
 10. GitHub Actions has at least one completed workflow run
 11. Deliverable-verdict log path resolves inside main tree (not a linked worktree)
 12. .company/state is not inside a cloud-sync folder (iCloud/OneDrive/Dropbox)
 13. No daemon-written paths are tracked in git (F4 dirty-tree guard)
 14. origin is pushable by the current authenticated GitHub user

Exit codes:
  0 — all checks passed (no FAILs)
  1 — one or more checks FAILED
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_JSON_BYTES = 500_000
_MAX_VISION_BYTES = 1_000_000

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]

# Canonical list of paths the Gatesmith daemon writes at runtime (F4 audit).
# Directories end with "/"; files do not.
# Keep in sync with install.sh gitignore template — the doctor check validates both.
DAEMON_WRITTEN_PATHS: list[str] = [
    # Subdirectory subtrees
    ".company/state/",
    ".company/logs/",
    ".company/runtime/",
    ".company/results/",
    ".company/agents/",
    ".company/escalations/",
    ".company/signals/",
    ".company/ipc/",
    ".company/workers/",
    ".company/plans/",
    ".company/metrics/",
    ".company/reports/",
    ".company/archive/",
    ".company/knowledge/",
    # Root-level daemon-written files
    ".company/org.json",
    ".company/approval_history.json",
    ".company/context_monitor.json",
    ".company/session_state.json",
    ".company/external_rate_limits.json",
    ".company/external_audit.log",
    ".company/webhook_config.json",
    ".company/budget_webhook_rate_limits.json",
    ".company/compliance-report.json",
    ".company/dashboard.port",
    ".company/adaptive_scheduler_state.json",
    # Planning runtime state
    ".planning/STATE.md",
    ".planning/COMPANY_STATE.md",
]

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    remedy: str = ""


@dataclass
class RunConfig:
    """All paths anchored to project_root — no cwd-relative defaults."""

    project_root: Path
    home_dir: Path
    settings_json: Path
    gitignore: Path
    forge_config: Path
    company_dir: Path


def make_config(
    project_root: Path | None = None,
    home_dir: Path | None = None,
) -> RunConfig:
    root = project_root or Path.cwd()
    home = home_dir or Path.home()
    return RunConfig(
        project_root=root,
        home_dir=home,
        settings_json=root / ".claude" / "settings.json",
        gitignore=root / ".gitignore",
        forge_config=root / "gatesmith.json",
        company_dir=root / ".company",
    )


# ── Shared helpers ────────────────────────────────────────────────────────────


def _read_allow_merge_to_main(config: RunConfig) -> bool | None:
    """Return allowMergeToMain bool from gatesmith-config, or None if not determinable."""
    cfg_path = config.forge_config
    if not cfg_path.exists():
        alt = config.project_root / ".claude" / "gatesmith.json"
        if alt.exists():
            cfg_path = alt
        else:
            return None
    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    val = cfg.get("autonomy", {}).get("autoMerge", {}).get("allowMergeToMain")
    return val if isinstance(val, bool) else None


def _gh_owner_repo(config: RunConfig) -> tuple[str, CheckResult | None]:
    """Return (owner/repo, None) or ('', error_result) via gh repo view."""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(config.project_root),
        )
    except FileNotFoundError:
        return "", CheckResult(
            "__gh__",
            "WARN",
            "gh CLI not found — cannot verify GitHub configuration",
            "Install GitHub CLI: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return "", CheckResult(
            "__gh__",
            "WARN",
            "gh timed out — skipping GitHub checks",
            "Check network connectivity or run `gh repo view` manually",
        )
    if r.returncode != 0:
        return "", CheckResult(
            "__gh__",
            "WARN",
            "no GitHub remote — skipping GitHub checks",
            "Add a GitHub remote: git remote add origin <url>",
        )
    owner_repo = r.stdout.strip()
    if not owner_repo:
        return "", CheckResult(
            "__gh__", "WARN", "could not determine repo name from gh", ""
        )
    return owner_repo, None


# ── Individual checks ─────────────────────────────────────────────────────────


def check_claude_on_path(config: RunConfig) -> CheckResult:
    """Check 1: claude CLI is on PATH."""
    try:
        r = subprocess.run(
            ["which", "claude"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult(
            "claude-on-path",
            "FAIL",
            "which: command not found",
            "Verify PATH is set correctly and Claude Code CLI is installed",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "claude-on-path",
            "FAIL",
            "timed out resolving claude binary",
            "Check your PATH configuration",
        )

    if r.returncode != 0:
        return CheckResult(
            "claude-on-path",
            "FAIL",
            "claude CLI not found on PATH",
            "Install Claude Code CLI: https://claude.ai/download",
        )
    resolved = r.stdout.strip()
    return CheckResult("claude-on-path", "PASS", f"claude found at {resolved}")


def check_workspace_trust(config: RunConfig) -> CheckResult:
    """Check 2: Claude Code workspace trust accepted for this repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(config.project_root),
        )
    except FileNotFoundError:
        return CheckResult(
            "workspace-trust",
            "WARN",
            "git not available — cannot determine trust path",
            "Install git, then run `claude` in this directory to accept workspace trust",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "workspace-trust",
            "WARN",
            "git timed out — cannot determine trust path",
            "Check git installation, then run `claude` in this directory",
        )

    if r.returncode != 0:
        return CheckResult(
            "workspace-trust",
            "WARN",
            "not a git repository — cannot determine trust path",
            "Run `claude` in this directory and accept the workspace trust prompt",
        )

    repo_root = r.stdout.strip()
    # Claude Code encodes the project path as the dir name under ~/.claude/projects/
    # by replacing every "/" with "-" (e.g. /Users/foo/bar → -Users-foo-bar)
    encoded = repo_root.replace("/", "-")
    trust_dir = config.home_dir / ".claude" / "projects" / encoded
    if trust_dir.exists():
        return CheckResult("workspace-trust", "PASS", f"trust accepted for {repo_root}")
    return CheckResult(
        "workspace-trust",
        "WARN",
        f"workspace trust not yet accepted (no entry for {encoded})",
        "Run `claude` in this directory and accept the workspace trust dialog",
    )


def check_gh_auth(config: RunConfig) -> CheckResult:
    """Check 3: gh CLI is present and authenticated."""
    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,  # never echo raw output (may contain token metadata)
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return CheckResult(
            "gh-auth",
            "FAIL",
            "gh CLI not found on PATH",
            "Install GitHub CLI: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "gh-auth",
            "WARN",
            "gh auth status timed out",
            "Check network connectivity or run `gh auth status` manually",
        )

    if r.returncode != 0:
        return CheckResult(
            "gh-auth",
            "FAIL",
            "not authenticated with GitHub",
            "Run `gh auth login`",
        )

    # Extract account name only — never forward the full output which may contain
    # token fingerprints or scope details (security: avoid credential disclosure)
    account = ""
    for line in r.stdout.splitlines():
        m = re.search(r"account\s+(\S+)", line)
        if m:
            account = m.group(1)
            break
    detail = f"authenticated as {account}" if account else "authenticated"
    return CheckResult("gh-auth", "PASS", detail)


def check_hooks(config: RunConfig) -> CheckResult:
    """Check 4: .claude/settings.json hooks are registered and compile clean."""
    # Phase 1: parse settings.json
    if not config.settings_json.exists():
        return CheckResult(
            "hooks-registered",
            "FAIL",
            ".claude/settings.json missing",
            "Re-run gatesmith-install or restore .claude/settings.json from the repo",
        )

    if config.settings_json.stat().st_size > _MAX_JSON_BYTES:
        return CheckResult(
            "hooks-registered",
            "WARN",
            ".claude/settings.json is unusually large (>500 KB)",
            "Inspect .claude/settings.json for unexpected content",
        )

    try:
        data = json.loads(config.settings_json.read_text())
    except json.JSONDecodeError as e:
        return CheckResult(
            "hooks-registered",
            "FAIL",
            f".claude/settings.json is not valid JSON: {e}",
            "Fix the JSON syntax in .claude/settings.json",
        )

    # Phase 2: confirm PreToolUse hooks are registered
    hooks_block = data.get("hooks", {})
    pre_tool = hooks_block.get("PreToolUse", [])
    if not pre_tool:
        return CheckResult(
            "hooks-registered",
            "WARN",
            "no PreToolUse hooks registered in .claude/settings.json",
            "Re-run gatesmith-install or restore .claude/settings.json from the repo",
        )

    # Phase 3: extract hook file paths and syntax-check in-process via ast.parse()
    # Using ast.parse() (never subprocess -c with content) avoids shell injection (C1)
    hook_paths: list[Path] = []
    for entry in pre_tool:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            m = re.search(r"uv run\s+(\S+\.py)", cmd)
            if m:
                hook_paths.append(config.project_root / m.group(1))

    bad_hooks: list[str] = []
    for hp in hook_paths:
        rel = (
            hp.relative_to(config.project_root)
            if hp.is_relative_to(config.project_root)
            else hp
        )
        if not hp.exists():
            bad_hooks.append(f"{rel} (not found)")
            continue
        try:
            source = hp.read_text(encoding="utf-8")
            ast.parse(source)  # syntax-only: never executes the code
        except SyntaxError as e:
            bad_hooks.append(f"{rel} (line {e.lineno}: {e.msg})")
        except (OSError, UnicodeDecodeError) as e:
            bad_hooks.append(f"{rel} (read error: {type(e).__name__})")

    if bad_hooks:
        return CheckResult(
            "hooks-registered",
            "FAIL",
            f"{len(bad_hooks)} hook(s) have issues: {', '.join(bad_hooks)}",
            "Fix syntax errors in the listed hook files",
        )

    count = len(hook_paths)
    return CheckResult(
        "hooks-registered",
        "PASS",
        f"{len(pre_tool)} PreToolUse group(s), {count} hook file(s) — all compile clean",
    )


def check_gitignore(config: RunConfig) -> CheckResult:
    """Check 5: .gitignore excludes __pycache__ and *.pyc."""
    if not config.gitignore.exists():
        return CheckResult(
            "gitignore-pycache",
            "FAIL",
            ".gitignore not found",
            "Create .gitignore with `__pycache__/` and `*.pyc` entries",
        )

    text = config.gitignore.read_text()
    has_pycache = "__pycache__" in text
    has_pyc = "*.pyc" in text

    if has_pycache and has_pyc:
        return CheckResult(
            "gitignore-pycache", "PASS", "__pycache__/ and *.pyc both excluded"
        )

    missing: list[str] = []
    if not has_pycache:
        missing.append("__pycache__/")
    if not has_pyc:
        missing.append("*.pyc")

    return CheckResult(
        "gitignore-pycache",
        "WARN",
        f"missing entries: {', '.join(missing)}",
        f"Add to .gitignore: {' '.join(missing)}",
    )


def check_forge_config(config: RunConfig) -> CheckResult:
    """Check 6: gatesmith.json parses and autoMerge policy is explicit."""
    # Two-path fallback matching gatesmith-install verify mode
    cfg_path = config.forge_config
    if not cfg_path.exists():
        alt = config.project_root / ".claude" / "gatesmith.json"
        if alt.exists():
            cfg_path = alt
        else:
            return CheckResult(
                "gatesmith-config-policy",
                "FAIL",
                "gatesmith.json not found",
                "Run gatesmith-install or restore gatesmith.json",
            )

    if cfg_path.stat().st_size > _MAX_JSON_BYTES:
        return CheckResult(
            "gatesmith-config-policy",
            "WARN",
            "gatesmith.json is unusually large (>500 KB)",
            "Inspect gatesmith.json for unexpected content",
        )

    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        return CheckResult(
            "gatesmith-config-policy",
            "FAIL",
            f"gatesmith.json is not valid JSON: {e}",
            "Fix the JSON syntax in gatesmith.json",
        )

    autonomy = cfg.get("autonomy")
    if autonomy is None:
        return CheckResult(
            "gatesmith-config-policy",
            "WARN",
            "autonomy section missing from gatesmith.json",
            "Add `autonomy.autoMerge.allowMergeToMain` (true or false) to gatesmith.json",
        )

    auto_merge = autonomy.get("autoMerge")
    if auto_merge is None:
        return CheckResult(
            "gatesmith-config-policy",
            "WARN",
            "autonomy.autoMerge section missing",
            "Add `autonomy.autoMerge.allowMergeToMain` (true or false) to gatesmith.json",
        )

    allow = auto_merge.get("allowMergeToMain")
    if allow is None:
        return CheckResult(
            "gatesmith-config-policy",
            "WARN",
            "autonomy.autoMerge.allowMergeToMain not set",
            "Add explicit `allowMergeToMain: false` (or true) under autonomy.autoMerge",
        )

    if not isinstance(allow, bool):
        return CheckResult(
            "gatesmith-config-policy",
            "WARN",
            f"allowMergeToMain must be a boolean, got {type(allow).__name__} ({allow!r})",
            "Set allowMergeToMain to true or false (not a string or number)",
        )

    return CheckResult(
        "gatesmith-config-policy",
        "PASS",
        f"allowMergeToMain={allow!r} (explicit)",
    )


def check_vision(config: RunConfig) -> CheckResult:
    """Check 7: .company/vision.md has a parseable goal table (if company is founded)."""
    if not config.company_dir.exists():
        return CheckResult(
            "vision-goal-table", "SKIP", "no .company/ directory — skipping"
        )

    vision = config.company_dir / "vision.md"
    if not vision.exists():
        # A fresh install has no goals yet; that is a choice, not a fault
        # (2026-10-29: every fresh install reported FAIL here and pointed at
        # two experimental commands).
        return CheckResult(
            "vision-goal-table",
            "WARN",
            "no goals yet: .company/vision.md not found",
            "Optional: add `### Period: Q1 2027 [status: active]` and goal rows"
            " like `| G1: Quality | Description | Metric | owner |` to"
            " .company/vision.md",
        )

    if vision.stat().st_size > _MAX_VISION_BYTES:
        return CheckResult(
            "vision-goal-table",
            "WARN",
            "vision.md is unusually large (>1 MB) — skipping parse",
            "Inspect .company/vision.md for unexpected content",
        )

    text = vision.read_text()

    has_active_period = bool(
        re.search(r"###\s+Period:.*\[status:\s*active\]", text, re.IGNORECASE)
    )
    # Match goal rows like `| G1: ...` or `| **G1: ...` (bold optional)
    has_goal_row = bool(re.search(r"\|\s*\*{0,2}G\d+:", text))

    if not has_active_period:
        return CheckResult(
            "vision-goal-table",
            "FAIL",
            "no active period header found in vision.md",
            "Add `### Period: Q1 2026 [status: active]` and a goal table to .company/vision.md",
        )

    if not has_goal_row:
        return CheckResult(
            "vision-goal-table",
            "FAIL",
            "active period found but no parseable goal table",
            "Add goals like `| G1: Quality | Description | Metric | owner |` under the period header",
        )

    goal_count = len(re.findall(r"\|\s*\*{0,2}G\d+:", text))
    return CheckResult(
        "vision-goal-table",
        "PASS",
        f"active period with {goal_count} goal(s)",
    )


def check_company_state_dirs(config: RunConfig) -> CheckResult:
    """Check 8: .company/state/ and .company/logs/ exist and are writable."""
    if not config.company_dir.exists():
        return CheckResult(
            "company-state-dirs", "SKIP", "no .company/ directory — skipping"
        )

    missing: list[str] = []
    not_writable: list[str] = []
    for subdir in ("state", "logs"):
        d = config.company_dir / subdir
        rel = f".company/{subdir}"
        if not d.exists():
            missing.append(rel)
        elif not os.access(d, os.W_OK):
            not_writable.append(rel)

    if missing:
        return CheckResult(
            "company-state-dirs",
            "FAIL",
            f"missing: {', '.join(missing)}",
            f"mkdir -p {' '.join(missing)}",
        )
    if not_writable:
        return CheckResult(
            "company-state-dirs",
            "FAIL",
            f"not writable: {', '.join(not_writable)}",
            f"chmod u+w {' '.join(not_writable)}",
        )
    return CheckResult(
        "company-state-dirs",
        "PASS",
        ".company/state/ and .company/logs/ exist and are writable",
    )


def check_branch_protection(config: RunConfig) -> CheckResult:
    """Check 9: If allowMergeToMain=true, base branch must have required status checks."""
    allow = _read_allow_merge_to_main(config)
    if allow is not True:
        return CheckResult(
            "branch-protection",
            "SKIP",
            "allowMergeToMain is not true — skipping protection check",
        )

    # Get owner/repo and default branch in one call
    try:
        r = subprocess.run(
            [
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner,defaultBranchRef",
                "-q",
                '.nameWithOwner + " " + (.defaultBranchRef.name // "main")',
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(config.project_root),
        )
    except FileNotFoundError:
        return CheckResult(
            "branch-protection",
            "WARN",
            "gh CLI not found — cannot verify branch protection",
            "Install GitHub CLI: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "branch-protection",
            "WARN",
            "gh timed out — cannot verify branch protection",
            "Check network connectivity",
        )

    if r.returncode != 0:
        return CheckResult(
            "branch-protection",
            "WARN",
            "no GitHub remote — cannot verify branch protection",
            "Add a GitHub remote: git remote add origin <url>",
        )

    parts = r.stdout.strip().split()
    if len(parts) < 2:
        return CheckResult(
            "branch-protection",
            "WARN",
            "could not parse repo/branch from gh — skipping protection check",
            "",
        )
    owner_repo, base_branch = parts[0], parts[1]

    # Check branch protection via GitHub API
    try:
        pr = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/branches/{base_branch}/protection"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return CheckResult(
            "branch-protection", "WARN", "gh CLI not found for protection API check", ""
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "branch-protection",
            "WARN",
            "gh api timed out checking branch protection",
            "",
        )

    if pr.returncode != 0:
        return CheckResult(
            "branch-protection",
            "FAIL",
            f"Level 2 without gates: allowMergeToMain=true but {base_branch} has no branch protection",
            "Add branch protection with required status checks via GitHub repo settings → Branches",
        )

    try:
        protection = json.loads(pr.stdout)
    except json.JSONDecodeError:
        return CheckResult(
            "branch-protection",
            "WARN",
            "could not parse branch protection API response",
            "",
        )

    rsc = protection.get("required_status_checks") or {}
    checks = rsc.get("checks") or rsc.get("contexts") or []
    if not checks:
        return CheckResult(
            "branch-protection",
            "FAIL",
            f"Level 2 without gates: {base_branch} is protected but has no required status checks",
            "Add at least one required status check to branch protection (GitHub repo settings → Branches → Edit)",
        )

    return CheckResult(
        "branch-protection",
        "PASS",
        f"{base_branch} protected with {len(checks)} required status check(s)",
    )


def check_actions_completed(config: RunConfig) -> CheckResult:
    """Check 10: GitHub Actions has at least one completed workflow run."""
    owner_repo, err = _gh_owner_repo(config)
    if err is not None:
        return CheckResult("actions-completed-runs", err.status, err.detail, err.remedy)

    try:
        ar = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/actions/runs", "-q", ".total_count"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return CheckResult(
            "actions-completed-runs",
            "WARN",
            "gh CLI not found for Actions API check",
            "Install GitHub CLI: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "actions-completed-runs", "WARN", "gh api timed out checking Actions", ""
        )

    if ar.returncode != 0:
        return CheckResult(
            "actions-completed-runs",
            "WARN",
            "merges will be gateless or never arm: Actions API call failed (Actions may be disabled or no workflows exist)",
            "Enable GitHub Actions and push a .github/workflows/ file, then trigger a run",
        )

    try:
        total = int(ar.stdout.strip())
    except (ValueError, AttributeError):
        return CheckResult(
            "actions-completed-runs",
            "WARN",
            "could not parse Actions run count from gh",
            "",
        )

    if total == 0:
        return CheckResult(
            "actions-completed-runs",
            "WARN",
            "merges will be gateless or never arm: no completed Actions runs found",
            "Push a .github/workflows/ file and trigger at least one run before enabling auto-merge",
        )

    return CheckResult(
        "actions-completed-runs", "PASS", f"{total} Actions workflow run(s) found"
    )


def check_verdict_log_anchoring(config: RunConfig) -> CheckResult:
    """Check 11: Deliverable-verdict log resolves inside main tree, not a linked worktree."""
    git_path = config.project_root / ".git"

    if not git_path.exists():
        return CheckResult(
            "verdict-log-anchoring",
            "SKIP",
            "no .git — cannot determine worktree status",
        )

    if git_path.is_dir():
        return CheckResult(
            "verdict-log-anchoring",
            "PASS",
            "running in main worktree — verdict paths resolve correctly",
        )

    # .git is a file → linked worktree
    try:
        content = git_path.read_text().strip()
    except OSError:
        return CheckResult(
            "verdict-log-anchoring",
            "WARN",
            "could not read .git file — worktree root unclear",
            "Run gatesmith-doctor from the main repo root, not a linked worktree",
        )

    m = re.match(r"gitdir:\s*(.+)", content)
    if not m:
        return CheckResult(
            "verdict-log-anchoring",
            "WARN",
            "unexpected .git file format in linked worktree",
            "Run gatesmith-doctor from the main repo root",
        )

    # .git file contains "gitdir: /path/to/.git/worktrees/<name>"
    # Navigate up two levels: worktrees/<name> → .git → main-repo-root
    worktree_gitdir = Path(m.group(1).strip())
    main_git = worktree_gitdir.parent.parent
    main_root = main_git.parent

    company_in_main = main_root / ".company"
    company_in_wt = config.project_root / ".company"

    if company_in_main.exists() and not company_in_wt.exists():
        return CheckResult(
            "verdict-log-anchoring",
            "WARN",
            f"cwd is a linked worktree; .company/ and verdict logs live in main tree at {main_root}",
            "Anchor verdict-log paths to the main repo root via `git worktree list --porcelain | awk 'NR==1{{print $2}}'`, not cwd",
        )

    return CheckResult(
        "verdict-log-anchoring",
        "PASS",
        "linked worktree — .company/ present in cwd, verdict paths OK",
    )


def check_sync_managed_state(config: RunConfig) -> CheckResult:
    """Check 12: .company/state is not inside a cloud-sync managed folder.

    Pure function of config.company_dir — no subprocess calls, no Path.home() probe,
    no platform-specific brctl. Detects sync roots by inspecting path components only,
    so results are deterministic regardless of the machine running the check.
    """
    # Check all components of the state path — works whether or not the path exists.
    parts = set((config.company_dir / "state").parts)

    # Ordered most-specific-first: when a path contains several markers the
    # reported one must be deterministic (a set intersection + next(iter())
    # varies with hash randomization — flaky in CI).
    _ICLOUD_MARKERS = ("com~apple~CloudDocs", "Mobile Documents")
    _ONEDRIVE_MARKERS = ("OneDrive",)
    _DROPBOX_MARKERS = ("Dropbox",)

    marker = next((m for m in _ICLOUD_MARKERS if m in parts), None)
    if marker:
        return CheckResult(
            "sync-managed-state",
            "WARN",
            f".company/state is inside an iCloud-synced folder ('{marker}' in path)",
            "Rename .company/state → state.nosync and symlink back, or move the repo outside iCloud",
        )

    if any(m in parts for m in _ONEDRIVE_MARKERS):
        return CheckResult(
            "sync-managed-state",
            "WARN",
            ".company/state is inside an OneDrive-synced folder ('OneDrive' in path)",
            "Move the repo outside your OneDrive folder",
        )

    if any(m in parts for m in _DROPBOX_MARKERS):
        return CheckResult(
            "sync-managed-state",
            "WARN",
            ".company/state is inside a Dropbox-synced folder ('Dropbox' in path)",
            "Move the repo outside your Dropbox folder",
        )

    return CheckResult(
        "sync-managed-state",
        "PASS",
        ".company/state is not inside a known cloud-sync folder",
    )


def _path_covered_by_gitignore(path: str, gitignore_text: str) -> bool:
    """Return True if `path` is covered by any entry in gitignore_text.

    Handles directory entries (ending with "/"), glob "/*" patterns, and
    exact file matches.  Not a full gitignore parser — sufficient for the
    deterministic entries written by gatesmith-install.
    """
    target = path.rstrip("/")
    for raw in gitignore_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip trailing slash for comparison
        entry = line.rstrip("/")
        if target == entry:
            return True
        # Entry is a parent directory — covers everything underneath
        if target.startswith(entry + "/"):
            return True
        # ".company/*" style glob covers ".company/foo"
        if entry.endswith("/*"):
            parent = entry[:-2]
            if target == parent or target.startswith(parent + "/"):
                return True
    return False


def _daemon_written_file_is_tracked(path: str, tracked_files: list[str]) -> bool:
    """Return True if repo-relative `path` falls under a daemon-written location."""
    for daemon_path in DAEMON_WRITTEN_PATHS:
        if daemon_path.endswith("/"):
            if path == daemon_path.rstrip("/") or path.startswith(daemon_path):
                return True
        elif path == daemon_path:
            return True
    return False


def check_gitignore_daemon_state(config: RunConfig) -> CheckResult:
    """Check 13: no daemon-written paths are tracked in git (F4 dirty-tree guard)."""
    if not config.gitignore.exists():
        return CheckResult(
            "gitignore-daemon-state",
            "SKIP",
            "no .gitignore — run gatesmith-install to create one",
        )

    # Priority 1: detect already-tracked daemon paths (the active blockade)
    try:
        ls = subprocess.run(
            [
                "git",
                "-C",
                str(config.project_root),
                "ls-files",
                "--",
                ".company/",
                ".planning/STATE.md",
                ".planning/COMPANY_STATE.md",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ls.returncode == 0:
            tracked = [
                line.strip()
                for line in ls.stdout.splitlines()
                if line.strip() and _daemon_written_file_is_tracked(line.strip(), [])
            ]
            if tracked:
                examples = tracked[:3]
                more = len(tracked) - 3
                suffix = f" (+{more} more)" if more > 0 else ""
                return CheckResult(
                    "gitignore-daemon-state",
                    "FAIL",
                    f"{len(tracked)} daemon-written file(s) tracked in git: "
                    f"{', '.join(examples)}{suffix}",
                    "Run: git rm --cached <files> and ensure .gitignore covers them. "
                    "Re-run gatesmith-install --upgrade to fix automatically.",
                )
    except (subprocess.TimeoutExpired, OSError):
        pass  # git unavailable — fall through to static check

    # Priority 2: warn if .gitignore doesn't cover known daemon paths
    gitignore_text = config.gitignore.read_text()
    not_covered = [
        p
        for p in DAEMON_WRITTEN_PATHS
        if not _path_covered_by_gitignore(p, gitignore_text)
    ]
    if not_covered:
        examples = not_covered[:3]
        more = len(not_covered) - 3
        suffix = f" (+{more} more)" if more > 0 else ""
        return CheckResult(
            "gitignore-daemon-state",
            "WARN",
            f"{len(not_covered)} daemon-written path(s) not excluded in .gitignore: "
            f"{', '.join(examples)}{suffix}",
            "Run gatesmith-install --upgrade to add missing gitignore entries (F4 fix).",
        )

    return CheckResult(
        "gitignore-daemon-state",
        "PASS",
        f"all {len(DAEMON_WRITTEN_PATHS)} daemon-written paths are gitignored",
    )


def check_origin_push_access(config: RunConfig) -> CheckResult:
    """Check 14: origin is pushable by the current authenticated GitHub user."""
    _WRITE_PERMISSIONS = {"ADMIN", "MAINTAIN", "WRITE"}
    try:
        r = subprocess.run(
            [
                "gh",
                "repo",
                "view",
                "--json",
                "viewerPermission",
                "-q",
                ".viewerPermission",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(config.project_root),
        )
    except FileNotFoundError:
        return CheckResult(
            "origin-push-access",
            "WARN",
            "gh CLI not found — cannot verify push access to origin",
            "Install GitHub CLI: https://cli.github.com/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "origin-push-access",
            "WARN",
            "gh timed out — skipping push-access check",
            "Check network connectivity or run `gh repo view --json viewerPermission` manually",
        )

    if r.returncode != 0:
        return CheckResult(
            "origin-push-access",
            "WARN",
            "could not determine push access (gh repo view failed) — origin may not be a pushable GitHub repo",
            "Ensure origin points to a GitHub repository you can push to: "
            "git remote -v  then  git remote set-url origin <your-fork-url>",
        )

    permission = r.stdout.strip().upper()
    if permission in _WRITE_PERMISSIONS:
        return CheckResult(
            "origin-push-access",
            "PASS",
            f"origin is pushable (viewerPermission={permission})",
        )

    return CheckResult(
        "origin-push-access",
        "WARN",
        f"origin is not pushable (viewerPermission={permission or 'unknown'}): "
        "the daemon's branch-push/PR step will fail silently after work is done",
        "Fork the repo, then: git remote set-url origin <your-fork-url>  —or—  "
        "ask the repo owner to grant you write access, then re-run gatesmith-doctor",
    )


# ── Runner ────────────────────────────────────────────────────────────────────


def run_all_checks(config: RunConfig) -> list[CheckResult]:
    return [
        check_claude_on_path(config),
        check_workspace_trust(config),
        check_gh_auth(config),
        check_hooks(config),
        check_gitignore(config),
        check_forge_config(config),
        check_vision(config),
        check_company_state_dirs(config),
        check_branch_protection(config),
        check_actions_completed(config),
        check_verdict_log_anchoring(config),
        check_sync_managed_state(config),
        check_gitignore_daemon_state(config),
        check_origin_push_access(config),
    ]


# ── Output ────────────────────────────────────────────────────────────────────

_ANSI: dict[str, str] = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

_STATUS_COLOR: dict[str, str] = {
    "PASS": "green",
    "WARN": "yellow",
    "FAIL": "red",
    "SKIP": "dim",
}

_NAME_COL = 26


def _color(text: str, name: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_ANSI.get(name, '')}{text}{_ANSI['reset']}"


def print_results(results: list[CheckResult], use_color: bool) -> None:
    for r in results:
        status_label = _color(
            f"{r.status:<4}", _STATUS_COLOR.get(r.status, ""), use_color
        )
        print(f"{status_label}  {r.name:<{_NAME_COL}}{r.detail}")
        if r.remedy and r.status in ("FAIL", "WARN"):
            print(f"          Remedy: {r.remedy}")

    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status == "SKIP")

    parts: list[str] = []
    parts.append(_color(f"{passed} passed", "green", use_color))
    if failed:
        parts.append(_color(f"{failed} failed", "red", use_color))
    if warned:
        parts.append(_color(f"{warned} warning(s)", "yellow", use_color))
    if skipped:
        parts.append(f"{skipped} skipped")

    print(f"\n{total} checks: {', '.join(parts)}")


def print_results_json(results: list[CheckResult]) -> None:
    out = [
        {"name": r.name, "status": r.status, "detail": r.detail, "remedy": r.remedy}
        for r in results
    ]
    print(json.dumps(out, indent=2))


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    argv = sys.argv[1:]
    json_mode = "--json" in argv

    if "--deep" in argv:
        # Deep mode delegates entirely to reality_audit — a standalone module
        # (not grown into this file) covering config/state/assessment/runtime
        # drift checks beyond this file's first-run preflight scope.
        import reality_audit

        deep_argv = [a for a in argv if a != "--deep"]
        return reality_audit.main(deep_argv)

    config = make_config()
    results = run_all_checks(config)

    if json_mode:
        print_results_json(results)
    else:
        use_color = sys.stdout.isatty()
        print_results(results, use_color)

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
