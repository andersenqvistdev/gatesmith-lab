#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-066-001: PR Rollback Executor

Automated rollback for daemon PRs that fail CI after merge.

Safety gates:
- Only reverts daemon/* branches
- Checks for conflicts before pushing
- Rate-limited (max N per day)
- Protected paths cannot be reverted
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Protected paths that cannot be auto-reverted
PROTECTED_PATHS = [
    "CLAUDE.md",
    "gatesmith.json",
    ".claude/settings.json",
    ".env",
]

# Max rollbacks per day to prevent runaway reverts
MAX_ROLLBACKS_PER_DAY = 5

# WS-066-002: post-merge CI failures must be CONFIRMED before a revert.
# State file tracking in-flight "rerun the failed jobs once" confirmations and
# their outcomes, keyed by merge commit SHA. This lets detect_failed_merges()
# stay non-blocking: it fires `gh run rerun --failed` once, returns nothing to
# revert yet, and checks the outcome on a LATER daemon tick instead of
# sleep-polling inline (which would stall the single-threaded daemon loop for
# as long as CI takes to re-run, worst case many minutes).
CONFIRMATION_STATE_FILENAME = "rollback_confirmations.json"

# Bound growth of the resolved-confirmation history (dedup so a still-broken
# main doesn't re-fire a rerun and re-escalate every single poll cycle).
MAX_RESOLVED_CONFIRMATIONS = 200

# A pending confirmation that never completes (stuck run, gh API outage) must
# still resolve eventually — as an escalation, NEVER as a silent drop or a
# revert — so a real break can't sit un-confirmed and un-escalated forever.
MAX_PENDING_CONFIRMATION_HOURS = 6

# WS-066-001: Only CI *correctness* gates may trigger a destructive rollback.
# A rollback reverts merged code, so the trigger set is an ALLOWLIST (fail-closed):
# a failing check whose name is not a known correctness gate does NOT trigger a
# revert — a human reviews instead. This excludes infra/deploy jobs such as
# "Deploy" (runs on product-site/** changes, fails on this repo for lack of
# deploy credentials) and "Auto-Merge", whose failures are environmental and are
# never grounds to revert otherwise-correct merged work. Matched by name prefix
# so suffixed jobs like "Install Test (macos-latest)" are covered.
DEFAULT_ROLLBACK_GATING_CHECKS = (
    "Test",  # also covers "Test Staleness Detection"
    "Lint",
    "Security",
    "Hooks",  # "Hooks Validate"
    "Install Test",  # "Install Test (macos-latest)" / "(ubuntu-latest)"
)


def select_gating_failures(
    checks: list[dict],
    gating_checks: tuple[str, ...] = DEFAULT_ROLLBACK_GATING_CHECKS,
) -> list[dict]:
    """Return only the check-run dicts that (a) concluded in failure AND (b) are
    correctness gates whose name starts with one of ``gating_checks``.

    Infra/deploy jobs (e.g. "Deploy", "Auto-Merge") are excluded even when
    failing, so they can never trigger a rollback of correct merged code.
    """
    prefixes = tuple(p.lower() for p in gating_checks)
    failures = []
    for check in checks:
        if check.get("conclusion") != "failure":
            continue
        name = (check.get("name", "") or "").lower()
        if name.startswith(prefixes):
            failures.append(check)
    return failures


def _get_changed_files(commit_sha: str) -> list[str]:
    """Return the list of files changed by ``commit_sha`` (empty on error)."""
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        pass
    return []


def validate_rollback_safety(
    pr_number: int,
    commit_sha: str,
    company_dir: Path,
) -> tuple[bool, list[str]]:
    """
    Validate that a rollback is safe to execute.

    Returns:
        (is_safe, list of warnings/reasons if not safe)
    """
    warnings = []

    # Check rate limit
    state_file = company_dir / "state" / "rollback_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            today = datetime.now(timezone.utc).date().isoformat()
            today_count = sum(
                1
                for r in state.get("rollbacks", [])
                if r.get("timestamp", "").startswith(today)
            )
            if today_count >= MAX_ROLLBACKS_PER_DAY:
                warnings.append(
                    f"Rate limit: {today_count} rollbacks today (max {MAX_ROLLBACKS_PER_DAY})"
                )
                return False, warnings
        except Exception:
            pass

    # Check what files were changed in the commit
    changed_files = _get_changed_files(commit_sha)
    if changed_files:
        for f in changed_files:
            if any(f.endswith(p) or f == p for p in PROTECTED_PATHS):
                warnings.append(f"Protected path modified: {f}")
                return False, warnings

    return True, warnings


# ---------------------------------------------------------------------------
# WS-066-002: rerun-and-confirm before revert
# ---------------------------------------------------------------------------


def _confirmation_state_path(company_dir: Path) -> Path:
    return company_dir / "state" / CONFIRMATION_STATE_FILENAME


def _load_confirmation_state(company_dir: Path) -> dict:
    path = _confirmation_state_path(company_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            state.setdefault("pending", {})
            state.setdefault("resolved", {})
            return state
        except Exception:
            pass
    return {"pending": {}, "resolved": {}}


def _save_confirmation_state(company_dir: Path, state: dict) -> None:
    # Bound the resolved history so a persistently-broken main can't grow
    # this file forever.
    resolved = state.get("resolved", {})
    if len(resolved) > MAX_RESOLVED_CONFIRMATIONS:
        ordered = sorted(resolved.items(), key=lambda kv: kv[1].get("resolved_at", ""))
        state["resolved"] = dict(ordered[-MAX_RESOLVED_CONFIRMATIONS:])

    path = _confirmation_state_path(company_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def find_workflow_run_id(commit_sha: str, branch: str = "main") -> int | None:
    """Find the most recent workflow run for ``commit_sha`` on ``branch``."""
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--commit",
                commit_sha,
                "--branch",
                branch,
                "--json",
                "databaseId",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        runs = json.loads(result.stdout or "[]")
        if runs:
            return runs[0].get("databaseId")
    except Exception as e:
        logger.warning(f"Could not find workflow run for {commit_sha}: {e}")
    return None


def trigger_rerun(run_id: int) -> bool:
    """Re-run only the failed jobs of ``run_id``. Returns True on success."""
    try:
        result = subprocess.run(
            ["gh", "run", "rerun", str(run_id), "--failed"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Could not trigger rerun for run {run_id}: {e}")
        return False


def get_run_status(run_id: int) -> dict | None:
    """Return {"status": ..., "conclusion": ...} for ``run_id``, or None on error."""
    try:
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--json", "status,conclusion"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout or "{}")
    except Exception as e:
        logger.warning(f"Could not fetch status for run {run_id}: {e}")
        return None


def get_check_runs_for_commit(commit_sha: str) -> list[dict] | None:
    """Fetch current check-run {name, conclusion} pairs for ``commit_sha``.

    Returns ``None`` on an API/transport error — distinct from an empty
    list (which means "fetched fine, zero check-runs"). Collapsing the two
    would let a transient `gh api` failure during confirmation resolution
    look identical to "all checks passed", silently writing off a commit
    that may still be genuinely broken. Callers must treat ``None`` as
    "couldn't confirm yet", never as "confirmed passing".
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{commit_sha}/check-runs",
                "--jq",
                ".check_runs[] | {name, conclusion}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        checks = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    checks.append(json.loads(line))
                except Exception:
                    pass
        return checks
    except Exception as e:
        logger.warning(f"Could not fetch check-runs for {commit_sha}: {e}")
        return None


# Pytest's final "short test summary info" line: `FAILED path/to/test.py::...`.
# Fall back to traceback frame lines (`path/to/test.py:123: in test_x`) when no
# summary line is present (e.g. a collection error truncates the log).
_PYTEST_SUMMARY_RE = re.compile(r"^FAILED\s+(\S+\.py)::", re.MULTILINE)
_PYTEST_TRACEBACK_RE = re.compile(r"^([\w./\-]+\.py):\d+: in ", re.MULTILINE)


def get_failing_test_files(run_id: int) -> set[str]:
    """Extract failing test file paths from a run's failed-job logs."""
    try:
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--log-failed"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return set()
        log = result.stdout
    except Exception as e:
        logger.warning(f"Could not fetch failed logs for run {run_id}: {e}")
        return set()

    files = set(_PYTEST_SUMMARY_RE.findall(log))
    if not files:
        files = set(_PYTEST_TRACEBACK_RE.findall(log))
    return files


def _failure_touches_pr(
    failing_files: set[str],
    changed_files: list[str],
    commit_sha: str,
) -> bool:
    """True if a failing test file was changed by the PR, or imports a module
    the PR changed."""
    changed_set = set(changed_files)
    if failing_files & changed_set:
        return True

    changed_modules = {Path(f).stem for f in changed_files if f.endswith(".py")}
    if not changed_modules:
        return False

    for failing_file in failing_files:
        try:
            result = subprocess.run(
                ["git", "show", f"{commit_sha}:{failing_file}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for module in changed_modules:
                if re.search(rf"\b{re.escape(module)}\b", stripped):
                    return True
    return False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _record_confirmation_timeout_escalation(
    company_dir: Path,
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    run_id: int,
) -> None:
    """A rerun confirmation that never completed within
    ``MAX_PENDING_CONFIRMATION_HOURS``. Never revert on an unconfirmed
    failure — escalate instead so it can't be silently dropped."""
    now = datetime.now(timezone.utc)
    record_id = f"rollback-timeout-{pr_number}-{now.strftime('%Y%m%d%H%M%S')}"
    title = (
        f"Post-merge CI rerun for PR #{pr_number} never completed confirmation "
        f"within {MAX_PENDING_CONFIRMATION_HOURS}h — NOT reverted, needs human review"
    )
    trigger_details = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "commit_sha": commit_sha,
        "run_id": run_id,
    }

    try:
        escalations_dir = company_dir / "escalations"
        escalations_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "task_id": record_id,
            "current_tier": 2,
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "original_agent": "pr_rollback_executor",
            "current_agent": None,
            "trigger": "rollback_confirmation_timeout",
            "trigger_details": trigger_details,
            "events": [
                {
                    "timestamp": now.isoformat(),
                    "tier": 2,
                    "trigger": "rollback_confirmation_timeout",
                    "action_taken": title,
                    "resolved": False,
                }
            ],
            "metadata": {
                "title": title,
                "condition": "rollback_confirmation_timeout",
            },
        }
        (escalations_dir / f"{record_id}.json").write_text(json.dumps(record, indent=2))
    except OSError:
        pass

    try:
        try:
            from . import escalation as escalation_mod  # type: ignore[attr-defined]
        except ImportError:
            import escalation as escalation_mod  # type: ignore[no-redef]

        escalation_record = escalation_mod.EscalationRecord(
            task_id=record_id,
            current_tier=2,
            status="pending",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            trigger="rollback_confirmation_timeout",
            trigger_details=trigger_details,
        )
        escalation_mod.fire_escalation_opt_in_notifications(escalation_record, title)
    except Exception:
        pass

    log_rollback_activity(
        pr_number=pr_number,
        pr_title=pr_title,
        rollback_pr_number=None,
        reason=f"CI rerun confirmation timed out after {MAX_PENDING_CONFIRMATION_HOURS}h",
        failure_type="confirmation_timeout",
        failure_details=trigger_details,
        company_dir=company_dir,
    )


def _record_unrelated_failure_escalation(
    company_dir: Path,
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    failed_checks: list[dict],
    failing_files: set[str],
) -> None:
    """Record a confirmed-but-unrelated CI failure as an escalation instead of
    reverting. Best-effort: never raises."""
    now = datetime.now(timezone.utc)
    record_id = f"rollback-unrelated-{pr_number}-{now.strftime('%Y%m%d%H%M%S')}"
    title = (
        f"Confirmed CI failure after merging PR #{pr_number} is unrelated to "
        f"its changes — NOT reverted, needs human review"
    )
    trigger_details = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "commit_sha": commit_sha,
        "failed_checks": failed_checks,
        "failing_files": sorted(failing_files),
    }

    try:
        escalations_dir = company_dir / "escalations"
        escalations_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "task_id": record_id,
            "current_tier": 2,
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "original_agent": "pr_rollback_executor",
            "current_agent": None,
            "trigger": "rollback_unrelated_failure",
            "trigger_details": trigger_details,
            "events": [
                {
                    "timestamp": now.isoformat(),
                    "tier": 2,
                    "trigger": "rollback_unrelated_failure",
                    "action_taken": title,
                    "resolved": False,
                }
            ],
            "metadata": {"title": title, "condition": "rollback_unrelated_failure"},
        }
        (escalations_dir / f"{record_id}.json").write_text(json.dumps(record, indent=2))
    except OSError:
        pass

    try:
        try:
            from . import escalation as escalation_mod  # type: ignore[attr-defined]
        except ImportError:
            import escalation as escalation_mod  # type: ignore[no-redef]

        escalation_record = escalation_mod.EscalationRecord(
            task_id=record_id,
            current_tier=2,
            status="pending",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            trigger="rollback_unrelated_failure",
            trigger_details=trigger_details,
        )
        escalation_mod.fire_escalation_opt_in_notifications(escalation_record, title)
    except Exception:
        pass

    log_rollback_activity(
        pr_number=pr_number,
        pr_title=pr_title,
        rollback_pr_number=None,
        reason="Confirmed CI failure unrelated to PR changes — not reverted",
        failure_type="unrelated_failure_confirmed",
        failure_details=trigger_details,
        company_dir=company_dir,
    )


def _notify_revert_executed(
    company_dir: Path,
    pr_number: int,
    pr_title: str,
    rollback_pr_number: int | None,
    reason: str,
) -> None:
    """Post a comment on the original PR and page the operator. Best-effort:
    a notification failure must never mask a successful revert."""
    comment = (
        "This PR was **automatically reverted**"
        + (f" as #{rollback_pr_number}" if rollback_pr_number else "")
        + f" because a post-merge CI failure was confirmed on re-run and "
        f"traced to files this PR changed.\n\nReason: {reason}"
    )
    try:
        subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", comment],
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"Could not comment on PR #{pr_number}: {e}")

    now = datetime.now(timezone.utc)
    record_id = f"rollback-executed-{pr_number}-{now.strftime('%Y%m%d%H%M%S')}"
    title = f"Auto-reverted PR #{pr_number} after confirmed post-merge CI failure"
    trigger_details = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "rollback_pr_number": rollback_pr_number,
        "reason": reason,
    }
    try:
        escalations_dir = company_dir / "escalations"
        escalations_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "task_id": record_id,
            "current_tier": 2,
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "original_agent": "pr_rollback_executor",
            "current_agent": None,
            "trigger": "rollback_executed",
            "trigger_details": trigger_details,
            "events": [
                {
                    "timestamp": now.isoformat(),
                    "tier": 2,
                    "trigger": "rollback_executed",
                    "action_taken": title,
                    "resolved": False,
                }
            ],
            "metadata": {"title": title, "condition": "rollback_executed"},
        }
        (escalations_dir / f"{record_id}.json").write_text(json.dumps(record, indent=2))
    except OSError:
        pass

    try:
        try:
            from . import escalation as escalation_mod  # type: ignore[attr-defined]
        except ImportError:
            import escalation as escalation_mod  # type: ignore[no-redef]

        escalation_record = escalation_mod.EscalationRecord(
            task_id=record_id,
            current_tier=2,
            status="pending",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            trigger="rollback_executed",
            trigger_details=trigger_details,
        )
        escalation_mod.fire_escalation_opt_in_notifications(escalation_record, title)
    except Exception:
        pass


def execute_rollback(
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    reason: str,
    failure_details: dict,
    company_dir: Path,
    arm_auto_merge: bool = True,
) -> dict:
    """
    Execute a rollback for a failed PR.

    ``arm_auto_merge=False`` opens the revert PR without arming GitHub's
    native auto-merge — the daemon passes it when the operator's config
    (autoMerge.allowMergeToMain=false or requireApproval=true) forbids
    unreviewed merges to main; the revert PR then waits for a human.

    Steps:
    1. Validate safety
    2. Create rollback branch
    3. git revert the commit
    4. Push branch
    5. Create PR
    6. Auto-merge if enabled

    Returns:
        dict with success status and details
    """
    result = {
        "success": False,
        "rollback_pr_number": None,
        "rollback_branch": None,
        "error": None,
    }

    # Step 1: Validate safety
    is_safe, warnings = validate_rollback_safety(pr_number, commit_sha, company_dir)
    if not is_safe:
        result["error"] = f"Unsafe rollback: {'; '.join(warnings)}"
        logger.warning(f"Rollback blocked for PR #{pr_number}: {warnings}")
        return result

    # Step 2: Create rollback branch
    rollback_branch = f"rollback/revert-pr{pr_number}-{commit_sha[:8]}"
    result["rollback_branch"] = rollback_branch

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            timeout=60,
        )

        # Create branch from main
        checkout_result = subprocess.run(
            ["git", "checkout", "-b", rollback_branch, "origin/main"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            result["error"] = f"Failed to create branch: {checkout_result.stderr}"
            return result

        # Step 3: Revert the commit
        revert_result = subprocess.run(
            ["git", "revert", "--no-edit", commit_sha],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if revert_result.returncode != 0:
            # Abort and cleanup
            subprocess.run(
                ["git", "revert", "--abort"], capture_output=True, timeout=10
            )
            subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)
            subprocess.run(
                ["git", "branch", "-D", rollback_branch],
                capture_output=True,
                timeout=10,
            )
            result["error"] = f"Revert conflict: {revert_result.stderr}"
            return result

        # Step 4: Push branch
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", rollback_branch],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if push_result.returncode != 0:
            result["error"] = f"Push failed: {push_result.stderr}"
            _cleanup_branch(rollback_branch)
            return result

        # Step 5: Create PR
        pr_body = _build_rollback_pr_body(
            pr_number, pr_title, commit_sha, reason, failure_details
        )
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"Revert PR #{pr_number}: {pr_title[:50]}",
                "--body",
                pr_body,
                "--base",
                "main",
                "--head",
                rollback_branch,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pr_result.returncode != 0:
            result["error"] = f"PR creation failed: {pr_result.stderr}"
            return result

        rollback_pr_number = _parse_pr_number(pr_result.stdout)
        result["rollback_pr_number"] = rollback_pr_number

        # Step 6: Enable auto-merge (only when the operator's config permits)
        if rollback_pr_number and arm_auto_merge:
            subprocess.run(
                ["gh", "pr", "merge", str(rollback_pr_number), "--auto", "--squash"],
                capture_output=True,
                timeout=30,
            )

        # Record the rollback
        _record_rollback(
            company_dir=company_dir,
            original_pr=pr_number,
            rollback_pr=rollback_pr_number,
            commit_sha=commit_sha,
            reason=reason,
        )

        # Log activity
        log_rollback_activity(
            pr_number=pr_number,
            pr_title=pr_title,
            rollback_pr_number=rollback_pr_number,
            reason=reason,
            failure_type="ci_check_failure",
            failure_details=failure_details,
            company_dir=company_dir,
        )

        result["success"] = True
        logger.info(f"Rollback PR #{rollback_pr_number} created for PR #{pr_number}")

        # WS-066-002: every auto-revert must be impossible to miss — comment
        # on the original PR and page the operator through the escalation path.
        _notify_revert_executed(
            company_dir=company_dir,
            pr_number=pr_number,
            pr_title=pr_title,
            rollback_pr_number=rollback_pr_number,
            reason=reason,
        )

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Rollback failed for PR #{pr_number}: {e}")
    finally:
        # Always return to main
        subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)

    return result


def _cleanup_branch(branch: str) -> None:
    """Clean up a failed rollback branch."""
    subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10)
    subprocess.run(["git", "branch", "-D", branch], capture_output=True, timeout=10)


def _parse_pr_number(output: str) -> int | None:
    """Parse PR number from gh pr create output."""
    if not output:
        return None
    # Output is like: https://github.com/org/repo/pull/123
    match = re.search(r"/pull/(\d+)", output)
    return int(match.group(1)) if match else None


def _build_rollback_pr_body(
    pr_number: int,
    pr_title: str,
    commit_sha: str,
    reason: str,
    failure_details: dict,
) -> str:
    """Build the PR body for a rollback PR."""
    failed_checks = failure_details.get("failed_checks", [])
    checks_list = (
        "\n".join(
            f"- {c.get('name', 'Unknown')}: {c.get('conclusion', 'failed')}"
            for c in failed_checks
        )
        or "- (no details)"
    )

    return f"""## Automated Rollback [WS-066-001]

This PR reverts #{pr_number} which failed after merge.

### Original PR
- **PR:** #{pr_number}
- **Title:** {pr_title}
- **Commit:** `{commit_sha[:12]}`

### Failure Reason
{reason}

### Failed Checks
{checks_list}

---
*This rollback was created automatically by the daemon rollback monitor.*
"""


def _record_rollback(
    company_dir: Path,
    original_pr: int,
    rollback_pr: int | None,
    commit_sha: str,
    reason: str,
) -> None:
    """Record rollback in state file for tracking."""
    state_dir = company_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "rollback_state.json"

    state = {"rollbacks": []}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass

    state["rollbacks"].append(
        {
            "original_pr": original_pr,
            "rollback_pr": rollback_pr,
            "commit_sha": commit_sha,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Keep last 100 entries
    state["rollbacks"] = state["rollbacks"][-100:]

    state_file.write_text(json.dumps(state, indent=2))


def log_rollback_activity(
    pr_number: int,
    pr_title: str,
    rollback_pr_number: int | None,
    reason: str,
    failure_type: str,
    failure_details: dict,
    company_dir: Path,
) -> None:
    """Log rollback to activity JSONL file."""
    state_dir = company_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    activity_file = state_dir / "activity.jsonl"

    entry = {
        "event_type": "rollback_executed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_pr_number": pr_number,
        "original_pr_title": pr_title,
        "rollback_pr_number": rollback_pr_number,
        "reason": reason,
        "failure_type": failure_type,
        "failure_details": failure_details,
    }

    with open(activity_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def detect_failed_merges(
    lookback_hours: int = 1,
    company_dir: Path | None = None,
    gating_checks: tuple[str, ...] = DEFAULT_ROLLBACK_GATING_CHECKS,
) -> list[dict]:
    """
    Detect recently merged daemon PRs with a CONFIRMED, PR-relevant post-merge
    CI failure.

    WS-066-002: a raw check-run failure is never enough on its own — PR #686
    was wrongly reverted over a single unrelated flake. Each newly-seen
    failure fires ``gh run rerun --failed`` once and is held in a
    pending-confirmation state (persisted under ``company_dir``, see
    :data:`CONFIRMATION_STATE_FILENAME``) until a LATER call to this function
    observes the rerun has completed:

    - rerun passes -> it was a flake; dropped, never returned.
    - rerun fails again but no failing test file touches (or imports a
      module from) a file the PR changed -> dropped, recorded as an
      "unrelated failure" escalation instead of a revert.
    - rerun fails again AND a failing test touches the PR's own changes ->
      returned here, exactly as before, so the caller's ``execute_rollback()``
      reverts it.

    This function never blocks waiting for CI to finish — resolving a pending
    confirmation costs one non-blocking ``gh run view`` per call, so repeated
    daemon polling ticks carry the wait, not one long call.

    Only failures of correctness gates in ``gating_checks`` count — infra/deploy
    job failures (e.g. "Deploy") never trigger a rollback. See
    :func:`select_gating_failures`.

    Returns list of PRs that need rollback.
    """
    if company_dir is None:
        company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"

    failed_prs = []
    confirmation_state = _load_confirmation_state(company_dir)
    pending = confirmation_state["pending"]
    resolved = confirmation_state["resolved"]
    state_dirty = False

    try:
        # Get recently merged daemon PRs
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                "head:daemon/",
                "--json",
                "number,title,mergeCommit,mergedAt",
                "--limit",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return []

        prs = json.loads(result.stdout or "[]")

        # Filter by merge time (lookback_hours) — but never drop a PR whose
        # commit already has a pending confirmation in flight. Reruns can
        # take longer than lookback_hours to complete; abandoning it here
        # would leave it stuck in `pending` forever, never resolved and
        # never escalated (the max-age valve below only fires for entries
        # this loop actually revisits).
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        filtered_prs = []
        for pr in prs:
            commit_sha = pr.get("mergeCommit", {}).get("oid")
            if commit_sha and commit_sha in pending:
                filtered_prs.append(pr)
                continue
            merged_at = pr.get("mergedAt", "")
            if merged_at:
                try:
                    merge_time = datetime.fromisoformat(
                        merged_at.replace("Z", "+00:00")
                    )
                    if merge_time >= cutoff_time:
                        filtered_prs.append(pr)
                except (ValueError, TypeError):
                    pass
        prs = filtered_prs

        for pr in prs:
            commit_sha = pr.get("mergeCommit", {}).get("oid")
            if not commit_sha:
                continue
            pr_number = pr["number"]
            pr_title = pr["title"]

            if commit_sha in resolved:
                # Already confirmed one way or another — never re-fire a
                # rerun or re-escalate for the same commit.
                continue

            if commit_sha in pending:
                entry = pending[commit_sha]

                # Safety valve: a confirmation that never completes (stuck
                # run, gh API outage) must still resolve — as an escalation,
                # never a silent drop or a revert — so it can't sit
                # unconfirmed and un-escalated forever.
                fired_at = _parse_iso(entry.get("fired_at"))
                if fired_at and (datetime.now(timezone.utc) - fired_at) > timedelta(
                    hours=MAX_PENDING_CONFIRMATION_HOURS
                ):
                    del pending[commit_sha]
                    resolved[commit_sha] = {
                        "outcome": "confirmation_timeout",
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "pr_number": pr_number,
                    }
                    state_dirty = True
                    _record_confirmation_timeout_escalation(
                        company_dir=company_dir,
                        pr_number=pr_number,
                        pr_title=pr_title,
                        commit_sha=commit_sha,
                        run_id=entry["run_id"],
                    )
                    logger.warning(
                        f"[WS-066-002] PR #{pr_number} confirmation never "
                        f"completed after {MAX_PENDING_CONFIRMATION_HOURS}h — "
                        f"escalated, no revert"
                    )
                    continue

                run_status = get_run_status(entry["run_id"])
                if not run_status or run_status.get("status") != "completed":
                    continue  # still running — check again next cycle

                checks = get_check_runs_for_commit(commit_sha)
                if checks is None:
                    # API error while resolving — never treat as "confirmed
                    # passing". Leave pending; retried next cycle (and
                    # eventually caught by the timeout valve above).
                    continue

                confirmed_checks = select_gating_failures(checks, gating_checks)
                del pending[commit_sha]
                state_dirty = True

                if not confirmed_checks:
                    resolved[commit_sha] = {
                        "outcome": "flake_resolved",
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "pr_number": pr_number,
                    }
                    logger.info(
                        f"[WS-066-002] PR #{pr_number} CI failure was a "
                        f"flake (passed on rerun) — no revert"
                    )
                    continue

                failing_files = get_failing_test_files(entry["run_id"])
                changed_files = _get_changed_files(commit_sha)
                relevant = _failure_touches_pr(failing_files, changed_files, commit_sha)

                if not relevant:
                    resolved[commit_sha] = {
                        "outcome": "unrelated_failure",
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "pr_number": pr_number,
                    }
                    _record_unrelated_failure_escalation(
                        company_dir=company_dir,
                        pr_number=pr_number,
                        pr_title=pr_title,
                        commit_sha=commit_sha,
                        failed_checks=confirmed_checks,
                        failing_files=failing_files,
                    )
                    logger.warning(
                        f"[WS-066-002] PR #{pr_number} CI failure confirmed "
                        f"but unrelated to its changes — escalated, no revert"
                    )
                    continue

                resolved[commit_sha] = {
                    "outcome": "reverted",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                    "pr_number": pr_number,
                }
                failed_prs.append(
                    {
                        "pr_number": pr_number,
                        "pr_title": pr_title,
                        "commit_sha": commit_sha,
                        "failed_checks": confirmed_checks,
                    }
                )
                continue

            # Not pending, not resolved — is this commit even failing now?
            raw_checks = get_check_runs_for_commit(commit_sha)
            if raw_checks is None:
                continue  # API error — retry next cycle, never fabricate a failure
            raw_failed = select_gating_failures(raw_checks, gating_checks)
            if not raw_failed:
                continue

            # First sighting: re-run the failed jobs once and defer the
            # revert decision to a later cycle instead of reverting now.
            run_id = find_workflow_run_id(commit_sha)
            if run_id is None or not trigger_rerun(run_id):
                logger.warning(
                    f"[WS-066-002] Could not trigger rerun for PR "
                    f"#{pr_number} ({commit_sha[:8]}) — will retry next cycle"
                )
                continue

            pending[commit_sha] = {
                "run_id": run_id,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "fired_at": datetime.now(timezone.utc).isoformat(),
            }
            state_dirty = True
            logger.info(
                f"[WS-066-002] PR #{pr_number} CI failed after merge — "
                f"re-running failed jobs once before considering a revert"
            )

    except Exception as e:
        logger.error(f"Failed to detect failed merges: {e}")

    if state_dirty:
        _save_confirmation_state(
            company_dir, {"pending": pending, "resolved": resolved}
        )

    return failed_prs


if __name__ == "__main__":
    # CLI for manual testing
    import argparse

    parser = argparse.ArgumentParser(description="PR Rollback Executor")
    parser.add_argument("--detect", action="store_true", help="Detect failed merges")
    parser.add_argument("--rollback", type=int, help="Rollback a specific PR number")
    args = parser.parse_args()

    company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"

    if args.detect:
        failed = detect_failed_merges(company_dir=company_dir)
        print(f"Found {len(failed)} failed merges:")
        for pr in failed:
            print(f"  PR #{pr['pr_number']}: {pr['pr_title']}")

    elif args.rollback:
        print("Manual rollback not implemented via CLI yet")
