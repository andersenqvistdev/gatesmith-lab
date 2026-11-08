# /// script
# requires-python = ">=3.10"
# ///
"""Gatesmith branch protection bootstrap — apply/preview GitHub branch protection rules.

Reads branchProtection from gatesmith.json, builds the GitHub API payload,
and optionally applies it via gh api PUT.

Usage:
    uv run protect_main.py [--dry-run] [--json] [--branch BRANCH] [--config PATH]
    uv run protect_main.py --ensure-ci   # CI liveness gate + apply protection
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BRANCH = "main"
DEFAULT_EXPECTED_CHECKS = ["Lint", "Security", "Hooks Validate"]

# Minimal CI workflow written when no .github/workflows/ files are found.
MINIMAL_CI_YAML = """\
# Minimal Gatesmith CI — written by gatesmith-protect-main --ensure-ci
# Add your project's lint and test steps as you grow.
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  check:
    name: Check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
"""

_CI_WORKFLOW_DIR = ".github/workflows"
_CI_POLL_INTERVAL = 15  # seconds between run-status polls
_CI_WAIT_TIMEOUT = 600  # 10 minutes total wait

# Config discovery: project root relative to this script, then cwd
_SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = _SCRIPT_DIR.parents[
    2
]  # .claude/hooks/company/ → .claude/hooks/ → .claude/ → project root
_DEFAULT_PROJECT_ROOTS = [_PROJECT_ROOT, Path(".")]


def _governing_config():
    """Which config file governs a setting (root if declared, else template)."""
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    import governing_config

    return governing_config


# Valid branch name: alphanumeric, dots, hyphens, underscores, slashes.
# Rejects ".." path traversal and leading/trailing slashes.
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")

# Valid GitHub owner/repo format: owner/repo, no nested slashes.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validate_branch(branch: str) -> None:
    """Raise ValueError if branch is not a safe git branch name."""
    if not _BRANCH_RE.match(branch) or ".." in branch:
        raise ValueError(
            f"Invalid branch name {branch!r}. "
            "Branch names must be alphanumeric and may contain ., -, _, /."
        )


def _validate_repo(repo: str) -> None:
    """Raise ValueError if repo is not in owner/repo format."""
    if not _REPO_RE.match(repo):
        raise ValueError(
            f"Unexpected repo format {repo!r} from gh CLI. Expected 'owner/repo'."
        )


def _defaults() -> dict:
    return {
        "branch": DEFAULT_BRANCH,
        "expectedChecks": DEFAULT_EXPECTED_CHECKS,
        "requireNoForcePush": True,
        "requireNoDeletion": True,
        "enforceAdmins": False,
        "strictStatusChecks": False,
        "requirePullRequestReviews": None,
    }


def load_config(config_path: Path | None = None) -> dict:
    """Load branchProtection config from gatesmith.json.

    When config_path is explicitly provided, parse errors are fatal (exit 1).
    When using auto-discovery paths, parse errors fall back to defaults.

    Returns a dict with all fields resolved to their defaults when missing.
    """
    if config_path is not None:
        # Explicit path: fail loudly if it exists but is unparseable.
        if not config_path.exists():
            return _defaults()
        try:
            with open(config_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"[FAIL] Cannot parse config {config_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise SystemExit(f"[FAIL] Cannot read config {config_path}: {exc}") from exc
        bp = data.get("branchProtection", {})
        return {
            **_defaults(),
            **{
                k: v
                for k, v in {
                    "branch": bp.get("branch"),
                    "expectedChecks": bp.get("expectedChecks"),
                    "requireNoForcePush": bp.get("requireNoForcePush"),
                    "requireNoDeletion": bp.get("requireNoDeletion"),
                    "enforceAdmins": bp.get("enforceAdmins"),
                    "strictStatusChecks": bp.get("strictStatusChecks"),
                    "requirePullRequestReviews": bp.get("requirePullRequestReviews"),
                }.items()
                if v is not None
            },
        }

    # Auto-discovery: the governing config (root if it declares
    # branchProtection, else the install template) of the first project that
    # has one (this script's, then the cwd's); defaults when it declares none
    # (the defaults are the protective rules).
    try:
        gc = _governing_config()
    except Exception:  # noqa: BLE001 — the defaults are the protective rules
        return _defaults()
    for root in _DEFAULT_PROJECT_ROOTS:
        root = Path(root)
        if not any((root / f).exists() for f in (gc.ROOT_CONFIG, gc.TEMPLATE_CONFIG)):
            continue
        bp, error = gc.section("branchProtection", root)
        if error is not None:
            continue  # unreadable: as before, try the next project
        # The first project with a config decides, even when it declares no
        # branchProtection: then the defaults hold, never another
        # project's rules (challenger on #787).
        d = _defaults()
        if bp.get("branch") is not None:
            d["branch"] = bp["branch"]
        if bp.get("expectedChecks") is not None:
            d["expectedChecks"] = bp["expectedChecks"]
        if bp.get("requireNoForcePush") is not None:
            d["requireNoForcePush"] = bp["requireNoForcePush"]
        if bp.get("requireNoDeletion") is not None:
            d["requireNoDeletion"] = bp["requireNoDeletion"]
        if bp.get("enforceAdmins") is not None:
            d["enforceAdmins"] = bp["enforceAdmins"]
        if bp.get("strictStatusChecks") is not None:
            d["strictStatusChecks"] = bp["strictStatusChecks"]
        if "requirePullRequestReviews" in bp:
            d["requirePullRequestReviews"] = bp["requirePullRequestReviews"]
        return d
    return _defaults()


def build_payload(config: dict) -> dict:
    """Build the GitHub branch protection API payload from resolved config."""
    return {
        "required_status_checks": {
            "strict": config.get("strictStatusChecks", False),
            "contexts": list(config.get("expectedChecks", DEFAULT_EXPECTED_CHECKS)),
        },
        "enforce_admins": config.get("enforceAdmins", False),
        "required_pull_request_reviews": config.get("requirePullRequestReviews", None),
        "restrictions": None,
        "allow_force_pushes": not config.get("requireNoForcePush", True),
        "allow_deletions": not config.get("requireNoDeletion", True),
    }


def check_gh_available() -> tuple[bool, str]:
    """Check that gh CLI is installed and authenticated.

    Returns:
        (True, "") on success, (False, error_message) on failure.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "gh CLI not authenticated. Run: gh auth login"
        return True, ""
    except FileNotFoundError:
        return False, (
            "gh CLI not found.\n"
            "  Install: brew install gh  (macOS)\n"
            "           https://cli.github.com/"
        )
    except subprocess.TimeoutExpired:
        return False, "gh CLI timed out during auth check"


def get_repo_name() -> str | None:
    """Get owner/repo from gh CLI, validated to owner/repo format."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            name = result.stdout.strip()
            try:
                _validate_repo(name)
                return name
            except ValueError:
                return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def apply_protection(
    repo: str,
    branch: str,
    payload: dict,
    *,
    dry_run: bool = False,
) -> dict:
    """Apply branch protection via gh api PUT, or preview with dry_run.

    Args:
        repo: GitHub repo in owner/repo format.
        branch: Branch name to protect.
        payload: GitHub API payload dict.
        dry_run: If True, return plan without calling gh.

    Returns:
        Result dict with success, endpoint, payload, and optional error/command.

    Raises:
        ValueError: if repo or branch fail format validation.
    """
    _validate_repo(repo)
    _validate_branch(branch)
    endpoint = f"repos/{repo}/branches/{branch}/protection"
    gh_cmd = ["gh", "api", "-X", "PUT", endpoint, "--input", "-"]

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "endpoint": endpoint,
            "payload": payload,
            "command": " ".join(gh_cmd),
        }

    try:
        result = subprocess.run(
            gh_cmd,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {
                "success": True,
                "dry_run": False,
                "endpoint": endpoint,
                "payload": payload,
            }
        return {
            "success": False,
            "dry_run": False,
            "error": (result.stderr or result.stdout)[:500],
            "endpoint": endpoint,
            "payload": payload,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "dry_run": False,
            "error": "gh api timed out",
            "endpoint": endpoint,
            "payload": payload,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "dry_run": False,
            "error": "gh not found",
            "endpoint": endpoint,
            "payload": payload,
        }


def find_workflow_files(repo_root: Path | None = None) -> list[Path]:
    """Return .yml/.yaml files under .github/workflows/ relative to repo_root."""
    root = repo_root or Path.cwd()
    wf_dir = root / _CI_WORKFLOW_DIR
    if not wf_dir.exists():
        return []
    return sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")))


def write_minimal_ci(repo_root: Path | None = None) -> Path:
    """Write a minimal ci.yml to .github/workflows/ and return its path."""
    root = repo_root or Path.cwd()
    wf_dir = root / _CI_WORKFLOW_DIR
    wf_dir.mkdir(parents=True, exist_ok=True)
    ci_path = wf_dir / "ci.yml"
    ci_path.write_text(MINIMAL_CI_YAML)
    return ci_path


def get_recent_workflow_runs(repo: str, *, limit: int = 5) -> list[dict]:
    """Return recent workflow runs from the GitHub API."""
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--limit",
                str(limit),
                "--json",
                "status,conclusion,databaseId",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def trigger_workflow_run(repo: str, workflow_file: str, branch: str = "main") -> bool:
    """Trigger a workflow_dispatch run. Returns True on success."""
    try:
        result = subprocess.run(
            ["gh", "workflow", "run", workflow_file, "--repo", repo, "--ref", branch],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def wait_for_ci_run(
    repo: str,
    *,
    timeout: int = _CI_WAIT_TIMEOUT,
    poll_interval: int = _CI_POLL_INTERVAL,
) -> tuple[bool, str]:
    """Poll until the most recent CI run reaches a terminal state.

    Returns:
        (success: bool, message: str)
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = get_recent_workflow_runs(repo, limit=1)
        if runs:
            run = runs[0]
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")
            if status == "completed":
                if conclusion == "success":
                    return (
                        True,
                        f"CI run completed successfully (id={run.get('databaseId')})",
                    )
                return (
                    False,
                    f"CI run completed with conclusion={conclusion!r} — "
                    "fix CI failures before enabling allowMergeToMain",
                )
        time.sleep(poll_interval)
    return False, f"Timed out after {timeout}s waiting for CI run to complete"


def ensure_ci_and_protection(
    repo: str,
    branch: str,
    payload: dict,
    *,
    repo_root: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Ensure CI is live and branch protection is applied.

    Steps:
    1. Check for workflow files; write minimal ci.yml if none found.
    2. If no recent successful run exists, trigger workflow_dispatch and wait.
    3. Apply branch protection with the CI checks required.

    Returns a result dict with:
      success, steps, error (on failure),
      action_required="commit_and_push_ci" when ci.yml was written and needs pushing,
      ci_path when ci.yml was written,
      plus apply_protection fields (endpoint, payload, dry_run) on success.
    """
    steps: list[str] = []

    # Step 1: Check for workflow files.
    workflow_files = find_workflow_files(repo_root)
    if not workflow_files:
        ci_path = (repo_root or Path.cwd()) / _CI_WORKFLOW_DIR / "ci.yml"
        if dry_run:
            steps.append(f"[DRY RUN] No workflows found; would write: {ci_path}")
            steps.append("[DRY RUN] Commit + push required before CI can run")
            return {
                "success": False,
                "dry_run": True,
                "steps": steps,
                "error": "No workflow files; minimal ci.yml would be written (commit+push needed)",
                "action_required": "commit_and_push_ci",
                "ci_path": str(ci_path),
            }
        written = write_minimal_ci(repo_root)
        steps.append(f"Wrote minimal CI workflow: {written}")
        return {
            "success": False,
            "steps": steps,
            "error": "Minimal ci.yml written — commit and push it, then re-run",
            "action_required": "commit_and_push_ci",
            "ci_path": str(written),
        }

    steps.append(
        f"Found {len(workflow_files)} workflow file(s): "
        f"{', '.join(f.name for f in workflow_files)}"
    )

    # Step 2: Verify recent successful run exists; trigger + wait if not.
    if dry_run:
        steps.append("[DRY RUN] Would check for recent successful CI runs")
        steps.append("[DRY RUN] Would trigger workflow_dispatch if none found")
    else:
        runs = get_recent_workflow_runs(repo, limit=5)
        has_recent_success = any(
            r.get("status") == "completed" and r.get("conclusion") == "success"
            for r in runs
        )

        if has_recent_success:
            steps.append("Recent successful CI run found — skipping trigger")
        else:
            steps.append("No recent successful run — triggering workflow_dispatch")
            wf_name = workflow_files[0].name
            triggered = trigger_workflow_run(repo, wf_name, branch)
            if not triggered:
                return {
                    "success": False,
                    "steps": steps,
                    "error": (
                        f"Could not trigger workflow_dispatch on {wf_name!r}. "
                        "Ensure the workflow has 'workflow_dispatch:' in its 'on:' block "
                        "and that GitHub Actions is enabled for this repo."
                    ),
                }
            steps.append(f"Triggered workflow_dispatch on {wf_name!r}")
            steps.append(f"Waiting up to {_CI_WAIT_TIMEOUT}s for CI to complete...")
            ok, msg = wait_for_ci_run(repo)
            steps.append(msg)
            if not ok:
                return {"success": False, "steps": steps, "error": msg}

    # Step 3: Apply branch protection.
    result = apply_protection(repo, branch, payload, dry_run=dry_run)
    result["steps"] = steps
    return result


def format_output(result: dict, *, json_mode: bool = False) -> str:
    """Format apply_protection result for human or machine consumption."""
    if json_mode:
        return json.dumps(result, indent=2)

    lines: list[str] = []
    payload = result.get("payload", {})

    if result.get("dry_run"):
        lines.append("[DRY RUN] Branch protection payload that would be applied:")
        lines.append("")
        lines.append(f"  Endpoint: PUT {result['endpoint']}")
        lines.append("")
        lines.append("  Payload:")
        for line in json.dumps(payload, indent=2).splitlines():
            lines.append(f"    {line}")
        lines.append("")
        lines.append(f"  Command: {result['command']}")
        lines.append("")
        lines.append("[DRY RUN] No changes applied.")
    elif result.get("success"):
        lines.append(f"[OK] Branch protection applied to {result['endpoint']}")
    else:
        error = result.get("error", "unknown error")
        lines.append(f"[FAIL] Failed to apply branch protection: {error}")

    return "\n".join(lines)


def _print_ensure_ci_steps(steps: list[str]) -> None:
    for step in steps:
        print(f"  {step}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gatesmith-protect-main",
        description="Apply GitHub branch protection rules from gatesmith.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  gatesmith-protect-main --dry-run          Preview payload, no changes applied
  gatesmith-protect-main                    Apply protection to main branch
  gatesmith-protect-main --ensure-ci        Ensure CI is live, then apply protection
  gatesmith-protect-main --branch develop   Apply to a specific branch
  gatesmith-protect-main --json             Machine-readable JSON output
""",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show payload without applying changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Branch to protect (default: from config or 'main')",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to gatesmith.json",
    )
    parser.add_argument(
        "--ensure-ci",
        action="store_true",
        dest="ensure_ci",
        help=(
            "CI liveness gate: write minimal ci.yml if absent, trigger a run, "
            "wait for it to pass, then apply branch protection. "
            "Used by /autonomy on before enabling allowMergeToMain."
        ),
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    branch = args.branch or config["branch"]
    payload = build_payload(config)

    # --ensure-ci: CI liveness gate then apply protection.
    if args.ensure_ci:
        if args.dry_run:
            repo = get_repo_name() or "OWNER/REPO"
            result = ensure_ci_and_protection(repo, branch, payload, dry_run=True)
            _print_ensure_ci_steps(result.get("steps", []))
            if args.json_mode:
                print(json.dumps(result, indent=2, default=str))
            elif result.get("action_required") == "commit_and_push_ci":
                ci_path = result.get("ci_path", f"{_CI_WORKFLOW_DIR}/ci.yml")
                print(f"\n[DRY RUN] Would write minimal CI workflow: {ci_path}")
                print("  Then: commit, push, and re-run gatesmith-protect-main --ensure-ci")
            else:
                print(format_output(result, json_mode=False))
            return 0

        gh_ok, gh_error = check_gh_available()
        if not gh_ok:
            if args.json_mode:
                print(json.dumps({"success": False, "error": gh_error}))
            else:
                print(f"[FAIL] {gh_error}", file=sys.stderr)
            return 1

        repo = get_repo_name()
        if not repo:
            error = (
                "Could not determine repo name. Run from within a GitHub repository."
            )
            if args.json_mode:
                print(json.dumps({"success": False, "error": error}))
            else:
                print(f"[FAIL] {error}", file=sys.stderr)
            return 1

        result = ensure_ci_and_protection(repo, branch, payload)
        _print_ensure_ci_steps(result.get("steps", []))

        action = result.get("action_required")
        if action == "commit_and_push_ci":
            ci_path = result.get("ci_path", f"{_CI_WORKFLOW_DIR}/ci.yml")
            if args.json_mode:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(f"\n[ACTION REQUIRED] Minimal CI workflow written to {ci_path}")
                print("  1. git add " + ci_path)
                print('  2. git commit -m "ci: add minimal GitHub Actions workflow"')
                print("  3. git push")
                print("  4. Re-run: bin/gatesmith-protect-main --ensure-ci")
            return 1

        if not result.get("success"):
            error = result.get("error", "unknown error")
            if args.json_mode:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": error,
                            "steps": result.get("steps", []),
                        }
                    )
                )
            else:
                print(f"[FAIL] {error}", file=sys.stderr)
            return 1

        output = format_output(result, json_mode=args.json_mode)
        print(output)
        return 0

    if args.dry_run:
        # Dry-run: show payload without touching gh auth or network
        repo = get_repo_name() or "OWNER/REPO"
        result = apply_protection(repo, branch, payload, dry_run=True)
        print(format_output(result, json_mode=args.json_mode))
        return 0

    # Live apply: require gh to be available and authenticated
    gh_ok, gh_error = check_gh_available()
    if not gh_ok:
        if args.json_mode:
            print(json.dumps({"success": False, "error": gh_error}))
        else:
            print(f"[FAIL] {gh_error}", file=sys.stderr)
        return 1

    repo = get_repo_name()
    if not repo:
        error = "Could not determine repo name. Run from within a GitHub repository."
        if args.json_mode:
            print(json.dumps({"success": False, "error": error}))
        else:
            print(f"[FAIL] {error}", file=sys.stderr)
        return 1

    result = apply_protection(repo, branch, payload)
    output = format_output(result, json_mode=args.json_mode)
    if result.get("success"):
        print(output)
        return 0
    else:
        print(output, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
