#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
PR Output Manager — branch/PR creation for plan-driven tasks.

P27 Implementation: Plan-Driven Daemon with PR-Based Output.

This module provides:
1. Feature branch creation for plan-driven tasks
2. Draft PR creation with task metadata
3. Test failure handling (create fix tasks)
4. Integration with work_allocator for queue management

Flow:
    Plan-driven task claimed
        → Create feature branch: feat/p14-task-14.3
        → Execute task
        → Run tests
        → If tests pass: Create draft PR
        → If tests fail: Create fix task in queue

Usage:
    # Create branch for task
    python pr_output_manager.py create-branch --task-id "P14-1.1"

    # Create PR after task completion
    python pr_output_manager.py create-pr --task-id "P14-1.1"

    # Handle test failure
    python pr_output_manager.py handle-test-failure --task-id "P14-1.1" --error "Test failed"
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Task 83.7: Git lock file patterns
_GIT_LOCK_ERRORS = ("index.lock", "config.lock", "shallow.lock", "Unable to create")
_GIT_LOCK_MAX_RETRIES = 3
_GIT_LOCK_RETRY_DELAY = 2  # seconds

# D10: Coverage/build artifact deny-list — excluded from commits regardless of
# .gitignore state so pre-existing installs without updated gitignore are covered.
_ARTIFACT_DENY_DIRS = frozenset(["htmlcov", ".pytest_cache", ".ruff_cache", "dist"])
_ARTIFACT_DENY_GLOBS = (".coverage", ".coverage.*", "*.egg-info", "*.egg-info/*")


def _run_git_with_lock_retry(
    cmd: list[str],
    *,
    timeout: int = 120,
    max_retries: int = _GIT_LOCK_MAX_RETRIES,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command with automatic retry on lock file errors.

    Task 83.7: Git operations fail intermittently when concurrent processes
    hold .git/index.lock or .git/config.lock. This retries with a short
    delay, and removes stale lock files (older than 5 minutes) before retry.
    """
    for attempt in range(max_retries + 1):
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result

        stderr = result.stderr or ""
        is_lock_error = any(pat in stderr for pat in _GIT_LOCK_ERRORS)

        if not is_lock_error or attempt == max_retries:
            return result

        # Remove stale lock files before retrying — one policy for the whole
        # daemon (company_resolver.stale_git_lock_age: a 0-byte lock after
        # five minutes, any lock after an hour; a live interactive commit
        # holds a non-empty index.lock for its whole editor session).
        _ensure_imports()
        for lock_name in ("index.lock", "config.lock", "shallow.lock"):
            lock_path = Path(cwd or ".") / ".git" / lock_name
            if lock_path.exists():
                try:
                    company_resolver.clear_stale_git_lock(lock_path)
                except OSError:
                    pass

        time.sleep(_GIT_LOCK_RETRY_DELAY * (attempt + 1))

    return result  # unreachable but satisfies type checkers


# Lazy imports for sibling modules
work_allocator = None
company_resolver = None
deliverable_judge = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global work_allocator, company_resolver

    if work_allocator is not None:
        return

    try:
        from . import company_resolver as cr
        from . import work_allocator as wa

        work_allocator = wa
        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

        work_allocator = wa
        company_resolver = cr


def _ensure_deliverable_judge():
    """Lazily import the Phase-2 deliverable judge (package or script mode)."""
    global deliverable_judge
    if deliverable_judge is not None:
        return deliverable_judge
    try:
        from . import deliverable_judge as dj
    except ImportError:
        import deliverable_judge as dj  # type: ignore[no-redef]
    deliverable_judge = dj
    return dj


def _verdict_hold():
    """Lazily import the pending-verdict hold (package or script mode)."""
    try:
        from . import verdict_hold as vh
    except ImportError:
        import verdict_hold as vh  # type: ignore[no-redef]
    return vh


def _scratch_guard():
    """Lazily import the scratch-file guard (package or script mode)."""
    try:
        from . import pr_scratch_guard as sg
    except ImportError:
        import pr_scratch_guard as sg  # type: ignore[no-redef]
    return sg


def write_gate_skip(task_id: str, pr_url: str | None, reason: str) -> None:
    """Write an explicit skip entry to deliverable_gate.jsonl (best-effort).

    Call this whenever a code path that would normally run run_deliverable_gate
    cannot do so — so the audit log accounts for every daemon PR without a gap.
    """
    try:
        dj = _ensure_deliverable_judge()
        dj._record_skip(task_id, pr_url, reason)
    except Exception as exc:  # noqa: BLE001 — skip-recording must never break callers
        print(
            f"[pr_output_manager] write_gate_skip failed for {task_id}: {exc}",
            file=sys.stderr,
        )


def _pr_number_from_url(pr_url: str | None) -> str | None:
    """Extract the numeric PR id from a gh PR URL (or a bare number)."""
    if not pr_url:
        return None
    match = re.search(r"/pull/(\d+)", pr_url)
    if match:
        return match.group(1)
    tail = pr_url.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else None


def _pr_label_names(pr_info: dict) -> list[str]:
    """Normalize gh ``labels`` (list of dicts or strings) to a list of names."""
    names = []
    for label in pr_info.get("labels", []) or []:
        if isinstance(label, dict):
            name = label.get("name")
            if name:
                names.append(name)
        elif isinstance(label, str):
            names.append(label)
    return names


def _gate_label() -> str:
    """Manual-review label name used by the Phase-2 deliverable gate."""
    try:
        return (
            _ensure_deliverable_judge()
            .load_gate_config()
            .get("label", "needs-manual-review")
        )
    except Exception:  # noqa: BLE001 — never let config loading break the merge path
        return "needs-manual-review"


def _challenger_hold_labels() -> frozenset:
    """CHALLENGER-STAGE hold labels (literal fallback: a broken import must
    never fail open into "no hold"). challenger:passed is not among them."""
    try:
        try:
            from . import challenger_stage
        except ImportError:
            import challenger_stage  # type: ignore[no-redef]
        return frozenset(challenger_stage.HOLD_LABELS)
    except Exception:  # noqa: BLE001
        return frozenset(
            {"challenger:pending", "challenger:incomplete", "challenger:needs-human"}
        )


def _challenger_merge_block(pr: str) -> str | None:
    """CHALLENGER-STAGE: a product-goal PR the challenger has not passed."""
    try:
        try:
            from . import challenger_stage
        except ImportError:
            import challenger_stage  # type: ignore[no-redef]
        return challenger_stage.merge_block_reason(pr)
    except Exception:  # noqa: BLE001
        return None


def _merge_gate():
    """The one merge decision every lever shares (single-merge-gate)."""
    try:
        from . import merge_gate
    except ImportError:
        import merge_gate  # type: ignore[no-redef]
    return merge_gate


def _governing_config():
    """Which config file governs a setting (root if declared, else template)."""
    try:
        from . import governing_config
    except ImportError:
        import governing_config  # type: ignore[no-redef]
    return governing_config


def _governed_auto_merge() -> tuple[dict, str | None]:
    """``autonomy.autoMerge`` from the governing config, as ``(dict, error)``.
    A fresh install's template counts; an untrusted file is an error."""
    try:
        return _governing_config().section("autonomy.autoMerge", Path.cwd())
    except Exception as exc:  # noqa: BLE001 — callers fail closed on error
        return {}, f"config resolver unavailable: {exc}"


def _merge_hold_labels() -> frozenset:
    """Every hold label any merge lever honours (literal fallback: a broken
    import must never fail open into "no hold")."""
    try:
        return _merge_gate().hold_labels()
    except Exception:  # noqa: BLE001
        return frozenset(
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


def _shared_merge_gate(pr_url: str) -> tuple[str | None, str | None]:
    """``merge_gate.may_merge`` for the project in the cwd: (refusal, head)."""
    try:
        root = Path.cwd()
        return _merge_gate().may_merge(
            pr_url, company_dir=root / ".company", project_root=root
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return f"merge gate unavailable: {exc}", None


def _review_hold_label() -> str:
    """Operator/reviewPaths hold label (shared seed; literal fallback).

    A broken import must never fail open into "no hold" on this surface.
    """
    try:
        from auto_merge_paths import REVIEW_HOLD_LABEL

        return REVIEW_HOLD_LABEL
    except ImportError:
        return "awaiting-operator-review"


# =============================================================================
# Configuration
# =============================================================================


def load_config() -> dict:
    """
    Load PR output configuration from gatesmith.json.

    Returns:
        dict with configuration values from autonomy.flowMode section.
    """
    config_paths = [
        Path.cwd() / ".claude" / "gatesmith.json",
        Path.cwd() / "gatesmith.json",
    ]

    defaults = {
        "enabled": True,
        "autoPushFeatureBranches": True,
        "autoCreateDraftPR": True,
        "autoMergeAfterApproval": False,
        "requireTestsPass": True,
        "onTestFailure": "create_fix_task",  # create_fix_task | block | skip
        "logAllOperations": True,
    }

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                flow_mode = config.get("autonomy", {}).get("flowMode", {})
                return {
                    "enabled": flow_mode.get("enabled", defaults["enabled"]),
                    "autoPushFeatureBranches": flow_mode.get(
                        "autoPushFeatureBranches", defaults["autoPushFeatureBranches"]
                    ),
                    "autoCreateDraftPR": flow_mode.get(
                        "autoCreateDraftPR", defaults["autoCreateDraftPR"]
                    ),
                    "autoMergeAfterApproval": flow_mode.get(
                        "autoMergeAfterApproval", defaults["autoMergeAfterApproval"]
                    ),
                    "requireTestsPass": flow_mode.get(
                        "requireTestsPass", defaults["requireTestsPass"]
                    ),
                    "onTestFailure": flow_mode.get(
                        "onTestFailure", defaults["onTestFailure"]
                    ),
                    "logAllOperations": flow_mode.get(
                        "logAllOperations", defaults["logAllOperations"]
                    ),
                }
            except (json.JSONDecodeError, OSError):
                pass

    return defaults


# =============================================================================
# Git Operations
# =============================================================================


def get_current_branch() -> str | None:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_main_branch() -> str:
    """Get the main branch name (main or master)."""
    try:
        # Check for main first
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "main"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return "main"

        # Fall back to master
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "master"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return "master"
    except (subprocess.TimeoutExpired, OSError):
        pass

    return "main"  # Default to main


def create_feature_branch(task_id: str, task_title: str) -> dict:
    """
    Create a feature branch for a plan-driven task.

    Branch naming: feat/{phase}-{task-id-slug}
    Example: feat/p14-create-roadmap-scheduler

    Args:
        task_id: The roadmap task ID (e.g., "P14-1.1")
        task_title: The task title for branch name generation

    Returns:
        Dict with success status and branch name
    """
    # Generate branch name
    # Extract phase from task_id (e.g., "P14" from "P14-1.1")
    phase_match = re.match(r"([A-Z]+\d+)", task_id)
    phase = phase_match.group(1).lower() if phase_match else "task"

    # Slugify task title
    slug = re.sub(r"[^a-z0-9]+", "-", task_title.lower())
    slug = slug.strip("-")[:40]  # Limit length

    branch_name = f"feat/{phase}-{slug}"

    try:
        # Task 83.7: Use lock-aware retry for branch operations
        main_branch = get_main_branch()
        _run_git_with_lock_retry(
            ["git", "checkout", main_branch],
            timeout=30,
        )

        # Pull latest
        _run_git_with_lock_retry(
            ["git", "pull", "--ff-only"],
            timeout=60,
        )

        # Create and checkout new branch
        result = _run_git_with_lock_retry(
            ["git", "checkout", "-b", branch_name],
            timeout=30,
        )

        if result.returncode == 0:
            return {
                "success": True,
                "branch_name": branch_name,
                "task_id": task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            return {
                "success": False,
                "error": result.stderr.strip(),
                "branch_name": branch_name,
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git operation timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def push_branch(branch_name: str) -> dict:
    """
    Push a branch to the remote repository.

    P91: Verifies branch exists locally before pushing to prevent
    "cannot be resolved to branch" errors on stale retries.

    Args:
        branch_name: Name of the branch to push

    Returns:
        Dict with success status
    """
    try:
        # P91: Verify branch exists locally before attempting push
        if (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                capture_output=True,
                timeout=10,
            ).returncode
            != 0
        ):
            return {
                "success": False,
                "error": f"Branch '{branch_name}' does not exist locally (stale retry?)",
                "branch_name": branch_name,
            }

        # WS-057-005: Prune stale remote tracking refs before push to prevent
        # "cannot be resolved to branch" errors from orphaned remote refs
        subprocess.run(
            ["git", "remote", "prune", "origin"],
            capture_output=True,
            timeout=15,
        )

        # Task 83.7: Use lock-aware retry for push
        result = _run_git_with_lock_retry(
            ["git", "push", "-u", "origin", branch_name],
            timeout=120,
        )

        if result.returncode == 0:
            return {"success": True, "branch_name": branch_name}
        else:
            return {
                "success": False,
                "error": result.stderr.strip(),
                "branch_name": branch_name,
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git push timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def _is_artifact(path: str) -> bool:
    """Return True if *path* matches the hardcoded coverage/build artifact deny-list.

    Checks the top-level directory component (catches all files inside denied
    dirs) and then tests the full path and basename against glob patterns.
    """
    import fnmatch

    top = path.split("/")[0]
    if top in _ARTIFACT_DENY_DIRS:
        return True
    name = path.split("/")[-1]
    return any(
        fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g)
        for g in _ARTIFACT_DENY_GLOBS
    )


def filter_gitignored_paths(files: list[str]) -> list[str]:
    """Return only the paths from ``files`` that git will accept for staging.

    Applies two filters in order:

    1. Hardcoded artifact deny-list (D10): coverage/build artifacts are dropped
       regardless of ``.gitignore`` state so pre-existing installs without
       updated gitignore rules are protected.  Filtered paths are logged to
       stderr.
    2. ``git check-ignore --stdin``: drops any remaining paths covered by
       ``.gitignore`` rules (WS-123), preventing capture failures when
       gitignored sibling directories appear in the changed-files list.

    Fails open: if the check-ignore call errors or times out, the list
    surviving step 1 is returned unchanged.
    """
    if not files:
        return files

    # Step 1: drop known artifact paths and log each one
    after_deny: list[str] = []
    for f in files:
        if _is_artifact(f):
            print(
                f"[artifact-filter] Excluded coverage/build artifact from commit: {f}",
                file=sys.stderr,
            )
        else:
            after_deny.append(f)

    if not after_deny:
        return after_deny

    # Step 2: drop gitignored paths
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            input="\n".join(after_deny) + "\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        # exit 0 → some paths ignored; exit 1 → no paths ignored; >1 → error
        if result.returncode > 1:
            return after_deny
        ignored = set(result.stdout.splitlines())
        return [f for f in after_deny if f not in ignored]
    except (subprocess.TimeoutExpired, OSError):
        return after_deny


def commit_changes(message: str, files: list[str] | None = None) -> dict:
    """
    Commit changes with the given message.

    Args:
        message: Commit message
        files: List of files to stage (None = all changed files)

    Returns:
        Dict with success status
    """
    try:
        # Task 83.7: Use lock-aware retry for git add and commit
        if files:
            # WS-123: Drop gitignored paths (e.g. __pycache__) before staging
            stageable = filter_gitignored_paths(files)
            for file in stageable:
                _run_git_with_lock_retry(
                    ["git", "add", file],
                    timeout=10,
                    max_retries=2,
                )
        else:
            _run_git_with_lock_retry(
                ["git", "add", "-A"],
                timeout=30,
                max_retries=2,
            )

        # Commit
        result = _run_git_with_lock_retry(
            ["git", "commit", "-m", message],
            timeout=30,
        )

        if result.returncode == 0:
            return {"success": True, "message": message}
        elif (
            "nothing to commit" in result.stdout or "nothing to commit" in result.stderr
        ):
            return {"success": True, "message": message, "note": "nothing to commit"}
        else:
            return {"success": False, "error": result.stderr.strip()}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git commit timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# PR Operations
# =============================================================================


def create_draft_pr(
    task_id: str,
    task_title: str,
    task_description: str,
    branch_name: str,
) -> dict:
    """
    Create a draft pull request for a completed task.

    Args:
        task_id: The roadmap task ID
        task_title: PR title (from task title)
        task_description: PR body content
        branch_name: Source branch for the PR

    Returns:
        Dict with success status and PR URL
    """
    main_branch = get_main_branch()

    # Format PR body
    body = f"""## Summary

{task_description}

## Task Reference

- **Task ID**: `{task_id}`
- **Branch**: `{branch_name}`
- **Source**: Plan-driven (from ROADMAP.md)

## Test Plan

- [ ] Unit tests pass
- [ ] Integration tests pass (if applicable)
- [ ] Manual verification completed

---
Generated by Gatesmith Daemon (P27 Plan-Driven Output)
"""

    try:
        # Check if gh CLI is available
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": "GitHub CLI (gh) not installed or not configured",
            }

        # Create draft PR
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                f"[{task_id}] {task_title}",
                "--body",
                body,
                "--base",
                main_branch,
                # Opens held until its head is judged (verdict_hold).
                *_verdict_hold().create_args(),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            pr_url = result.stdout.strip()
            return {
                "success": True,
                "pr_url": pr_url,
                "task_id": task_id,
                "branch_name": branch_name,
                "draft": True,
            }
        else:
            return {
                "success": False,
                "error": result.stderr.strip(),
                "task_id": task_id,
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PR creation timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Test Handling
# =============================================================================


def run_tests() -> dict:
    """
    Run project tests scoped to changed files.

    P89: Instead of running the full test suite (4000+ tests), detect which
    files were modified and only run tests related to those files. This prevents
    pre-existing test failures from blocking daemon PRs for unrelated work.

    Falls back to full suite if no changed files detected or no matching tests found.

    Returns:
        Dict with success status and test output
    """
    # P89: Detect changed files and scope tests accordingly
    changed_files = []
    try:
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_result.returncode == 0:
            changed_files = [
                f for f in diff_result.stdout.strip().split("\n") if f.strip()
            ]
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Map changed source files to their test files
    test_files = set()
    for f in changed_files:
        # Direct test files
        if f.startswith("tests/") or f.startswith(".claude/tests/"):
            test_files.add(f)
            continue
        # Source file → find matching test
        basename = Path(f).stem
        for test_dir in ["tests", ".claude/tests"]:
            candidate = Path(test_dir) / f"test_{basename}.py"
            if candidate.exists():
                test_files.add(str(candidate))

    # If we found scoped tests, run only those
    if test_files:
        cmd = ["pytest", "--tb=short", "-q"] + sorted(test_files)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "success": result.returncode == 0,
                "command": " ".join(cmd),
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None,
                "scoped": True,
                "test_files": sorted(test_files),
            }
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: no changed files detected or no matching tests — skip tests
    # (rather than running 4000+ tests and blocking on pre-existing failures).
    # Not a failure, but not a pass either: ``ran: False`` makes the PR say
    # that no test ran instead of "Passed".
    if changed_files and not test_files:
        return {
            "success": True,
            "ran": False,
            "command": "skipped (no matching test files for changed sources)",
            "output": f"Changed {len(changed_files)} files, no matching tests found. Skipping.",
            "error": None,
            "scoped": True,
            "test_files": [],
        }

    # Final fallback: run full suite (only if we couldn't detect changes at all)
    test_commands = [
        ["pytest", "--tb=short", "-q"],
        ["python", "-m", "pytest", "--tb=short", "-q"],
    ]
    for cmd in test_commands:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "success": result.returncode == 0,
                "command": " ".join(cmd),
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None,
            }
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Tests timed out ({' '.join(cmd)})"}
        except OSError:
            continue

    return {"success": False, "error": "No test runner found"}


def handle_test_failure(
    task_id: str,
    task_title: str,
    error_output: str,
    config: dict | None = None,
) -> dict:
    """
    Handle test failure based on configuration.

    Strategies:
    - create_fix_task: Create a new task in the queue to fix the issue
    - block: Leave the PR blocked until manually fixed
    - skip: Skip PR creation but mark task complete

    Args:
        task_id: The failing task ID
        task_title: Original task title
        error_output: Test error output
        config: PR output configuration (loaded if not provided)

    Returns:
        Dict with action taken
    """
    if config is None:
        config = load_config()

    strategy = config.get("onTestFailure", "create_fix_task")

    if strategy == "create_fix_task":
        _ensure_imports()

        # WS-069-002: Zombie prevention — don't create meta-tasks for meta-tasks
        zombie_patterns = [
            "Fix test failures for:",
            "(recovered)",
            "auto-health-recovery",
            "Fix failures for:",
        ]
        is_already_meta_task = any(
            pattern.lower() in task_title.lower() for pattern in zombie_patterns
        )

        if is_already_meta_task:
            # Escalate instead of creating another fix task
            return {
                "action": "escalated",
                "success": True,
                "reason": "zombie_prevention",
                "message": f"Task '{task_title[:50]}...' is already a meta-task. "
                "Escalating instead of creating recursive fix task.",
                "original_task_id": task_id,
            }

        # Create a fix task
        fix_title = f"Fix test failures for: {task_title}"
        fix_description = f"""Test failures encountered for task {task_id}.

## Original Task
{task_title}

## Error Output
```
{error_output[:2000]}
```

## Required Actions
1. Review test failures
2. Fix the underlying issues
3. Ensure all tests pass
4. Update PR or merge fix

---
Auto-generated by Gatesmith Daemon (P27 Test Failure Handler)
"""

        result = work_allocator.add_task(
            title=fix_title,
            description=fix_description,
            priority=2,  # High priority
            estimated_complexity="standard",
            source="planning",
        )

        return {
            "action": "created_fix_task",
            "success": result.get("success", False),
            "fix_task_id": result.get("task_id"),
            "original_task_id": task_id,
        }

    elif strategy == "block":
        return {
            "action": "blocked",
            "success": True,
            "message": "PR blocked due to test failures",
            "task_id": task_id,
        }

    elif strategy == "skip":
        return {
            "action": "skipped",
            "success": True,
            "message": "PR creation skipped, task marked complete",
            "task_id": task_id,
        }

    else:
        return {
            "action": "unknown",
            "success": False,
            "error": f"Unknown test failure strategy: {strategy}",
        }


# =============================================================================
# Workflow Integration
# =============================================================================


@dataclass
class PRWorkflowResult:
    """Result of a complete PR workflow execution."""

    success: bool
    task_id: str
    branch_name: str | None = None
    pr_url: str | None = None
    tests_passed: bool | None = None
    fix_task_id: str | None = None
    error: str | None = None
    steps_completed: list[str] | None = None
    deliverable_blocked: bool = False
    deliverable_reason: str | None = None


def execute_pr_workflow(
    task_id: str,
    task_title: str,
    task_description: str,
) -> PRWorkflowResult:
    """
    Execute the complete PR workflow for a plan-driven task.

    Steps:
    1. Create feature branch
    2. (Assumes task execution happens externally)
    3. Run tests
    4. If tests pass: Push branch and create draft PR
    5. If tests fail: Handle according to config

    Args:
        task_id: The roadmap task ID
        task_title: Task title
        task_description: Task description

    Returns:
        PRWorkflowResult with workflow outcome
    """
    config = load_config()
    steps_completed = []

    if not config["enabled"]:
        return PRWorkflowResult(
            success=False,
            task_id=task_id,
            error="PR workflow disabled in configuration",
        )

    # 1. Create feature branch
    branch_result = create_feature_branch(task_id, task_title)
    if not branch_result["success"]:
        return PRWorkflowResult(
            success=False,
            task_id=task_id,
            error=f"Failed to create branch: {branch_result.get('error')}",
            steps_completed=steps_completed,
        )

    branch_name = branch_result["branch_name"]
    steps_completed.append("create_branch")

    # Layer B humanProtected enforcement (PR 265 review: this third
    # PR-creation path had no check). Same fail-closed contract as
    # execute_auto_pr_workflow.
    _pr_checked = sorted(
        set(get_deliverable_changed_files([])) | set(_branch_changed_files())
    )
    _pr_hits = _human_protected_violations(_pr_checked)
    if _pr_hits is None or _pr_hits:
        reason = (
            "committed gatesmith.json unreadable — failing closed"
            if _pr_hits is None
            else "touches human-protected path(s): " + ", ".join(sorted(_pr_hits))
        )
        print(
            f"[humanProtected] REFUSED pr-workflow for {task_id}: {reason}",
            file=sys.stderr,
        )
        return PRWorkflowResult(
            success=False,
            task_id=task_id,
            branch_name=branch_name,
            error=f"Refusing to create PR: {reason}",
            steps_completed=steps_completed,
        )

    # PR-SCRATCH-GUARD: this path pushes the branch as committed (it stages
    # nothing itself), so a flagged file cannot be left out — refuse and name
    # it. Same failure-log route as the humanProtected refusal above.
    _sg = _scratch_guard().check(None, task_title, task_description)
    if _sg.refused:
        print(
            f"[scratch-guard] REFUSED pr-workflow for {task_id}: {_sg.reason}",
            file=sys.stderr,
        )
        return PRWorkflowResult(
            success=False,
            task_id=task_id,
            branch_name=branch_name,
            error=_sg.reason,
            steps_completed=steps_completed,
        )

    # 2. Run tests (if required)
    tests_passed = True
    if config["requireTestsPass"]:
        test_result = run_tests()
        tests_passed = test_result["success"]
        steps_completed.append("run_tests")

        if not tests_passed:
            # Handle test failure
            failure_result = handle_test_failure(
                task_id=task_id,
                task_title=task_title,
                error_output=test_result.get("error", test_result.get("output", "")),
                config=config,
            )

            return PRWorkflowResult(
                success=False,
                task_id=task_id,
                branch_name=branch_name,
                tests_passed=False,
                fix_task_id=failure_result.get("fix_task_id"),
                error="Tests failed",
                steps_completed=steps_completed,
            )

    # 3. Push branch (if auto-push enabled)
    if config["autoPushFeatureBranches"]:
        push_result = push_branch(branch_name)
        if not push_result["success"]:
            return PRWorkflowResult(
                success=False,
                task_id=task_id,
                branch_name=branch_name,
                tests_passed=tests_passed,
                error=f"Failed to push branch: {push_result.get('error')}",
                steps_completed=steps_completed,
            )
        steps_completed.append("push_branch")

    # 4. Create draft PR (if enabled)
    pr_url = None
    _gate_blocked = False
    _gate_reason: str | None = None
    if config["autoCreateDraftPR"]:
        pr_result = create_draft_pr(
            task_id=task_id,
            task_title=task_title,
            task_description=task_description,
            branch_name=branch_name,
        )

        if pr_result["success"]:
            pr_url = pr_result["pr_url"]
            steps_completed.append("create_pr")
            # Phase 2/3: gate this plan-driven daemon PR too (it was previously
            # ungated — only execute_auto_pr_workflow ran the judge). Fail-safe:
            # only labels, never fails the workflow.
            try:
                _gate = run_deliverable_gate(
                    task_id,
                    task_title,
                    task_description,
                    pr_url,
                    branch_name,
                    daemon_executed=True,
                )
                if _gate.get("ran"):
                    steps_completed.append(
                        "deliverable_gate_blocked"
                        if _gate.get("blocked")
                        else "deliverable_gate_passed"
                    )
                    _gate_blocked = bool(_gate.get("blocked"))
                    _gate_reason = _gate.get("error") or _gate.get("reason")
            except Exception as exc:  # noqa: BLE001 — gate must never break PR creation
                write_gate_skip(
                    task_id, pr_url, f"gate raised exception: {str(exc)[:200]}"
                )
        else:
            # PR creation failed but branch was pushed - partial success
            return PRWorkflowResult(
                success=True,  # Partial success
                task_id=task_id,
                branch_name=branch_name,
                tests_passed=tests_passed,
                error=f"Branch pushed but PR creation failed: {pr_result.get('error')}",
                steps_completed=steps_completed,
            )

    return PRWorkflowResult(
        success=True,
        task_id=task_id,
        branch_name=branch_name,
        pr_url=pr_url,
        tests_passed=tests_passed,
        steps_completed=steps_completed,
        deliverable_blocked=_gate_blocked,
        deliverable_reason=_gate_reason,
    )


def should_use_pr_workflow(task: dict) -> bool:
    """
    Determine if a task should use the PR workflow.

    PR workflow is used when:
    1. flowMode.enabled is True
    2. Task source is "planning" (from ROADMAP.md)

    Args:
        task: Task dictionary

    Returns:
        True if PR workflow should be used
    """
    config = load_config()

    if not config["enabled"]:
        return False

    # Only plan-driven tasks get PRs
    source = task.get("source", "")
    return source == "planning"


# =============================================================================
# Auto-PR Workflow (Daemon tasks)
# =============================================================================


def load_auto_pr_config() -> dict:
    """Load auto-PR configuration from gatesmith.json autonomy.autoPR."""
    defaults = {
        "enabled": False,
        "branchPrefix": "daemon",
        "createDraftPR": True,
        "requireTestsPass": True,
        "requireSecretsClean": True,
        "requireLintPass": True,
        "checkDependencies": True,
        "autoFixLint": True,
        "onTestFailure": "create_fix_task",
        "onSecretFound": "block",
        "onDependencyIssue": "warn_in_pr",
        "excludePathPatterns": [
            ".company/*.json",
            ".company/*.md",
            ".company/*.tmp",
            ".company/state/*",
            ".company/config/*",
            ".company/runtime/*",
            ".company/logs/*",
            ".company/daemon.*",
            ".company/queue.lock",
            ".company/escalations/*",
            ".company/agents/*/memory.md",
            ".company/employee-ideas/*",
            ".company/reports/*",
            ".company/daemon_snapshots/*",
            ".company/analytics/*",
            ".company/research/*",
            ".company/sales/*",
            ".company/business/*",
            ".company/social/*",
            ".company/templates/*",
            ".company/knowledge/*",
            ".company/org.json",
            ".planning/DISCUSS-*",
            ".planning/PLAN-*",
            ".planning/ADR-*",
            ".planning/P*-*",
            ".planning/qa/*",
            ".planning/adr/*",
            ".worktrees/*",
            "coverage.json",
            ".coverage*",
            "tests/test_secret*",
            ".claude/hooks/secret_scanner.py",
            "* 2.*",
            "* 2",
            "*.tmp",
        ],
    }

    config_paths = [
        Path.cwd() / "gatesmith.json",
        Path.cwd() / ".claude" / "gatesmith.json",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                auto_pr = config.get("autonomy", {}).get("autoPR", {})
                return {k: auto_pr.get(k, v) for k, v in defaults.items()}
            except (json.JSONDecodeError, OSError):
                pass

    return defaults


def get_deliverable_changed_files(exclude_patterns: list[str]) -> list[str]:
    """Get changed files excluding state/runtime files.

    Uses git status --porcelain to detect modified and untracked files,
    then filters out files matching exclude_patterns via fnmatch.

    Args:
        exclude_patterns: Glob patterns for files to exclude (e.g., ".company/*.json")

    Returns:
        List of deliverable file paths that changed.
    """
    import fnmatch

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        changed = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            # Porcelain format: XY filename (or XY -> renamed)
            file_path = line[3:].strip().strip('"')
            # Handle renames: "old -> new"
            if " -> " in file_path:
                file_path = file_path.split(" -> ", 1)[1]

            # Check against exclude patterns
            excluded = False
            for pattern in exclude_patterns:
                if fnmatch.fnmatch(file_path, pattern):
                    excluded = True
                    break
            if not excluded:
                changed.append(file_path)

        return changed

    except (subprocess.TimeoutExpired, OSError):
        return []


def _human_protected_config() -> dict | None:
    """The humanProtected block from the COMMITTED config, never the
    working tree.

    PR 265 review blocker: reading ``Path.cwd()/gatesmith.json`` read the
    same mutable tree the check protects — on the auto-PR (poll-in-place)
    path a worker that gutted humanProtected (or deleted the file outright,
    which the old ``if not exists: return []`` treated as "no violations")
    made the check read its own sabotage and then SHIP it. ``git show
    HEAD:gatesmith.json`` is the pre-worker state; falls back to the
    committed ``.claude/gatesmith.json`` template (fresh installs ship
    only that copy — config-resolution asymmetry finding).

    Returns the humanProtected dict, {} when neither committed copy exists
    (genuinely unconfigured repo), or None when a committed copy exists but
    cannot be read/parsed — callers MUST fail closed on None.
    """
    for committed_path in ("gatesmith.json", ".claude/gatesmith.json"):
        try:
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{committed_path}"],
                capture_output=True,
                timeout=10,
            )
            if probe.returncode != 0:
                continue
            shown = subprocess.run(
                ["git", "show", f"HEAD:{committed_path}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if shown.returncode != 0:
                return None
            config = json.loads(shown.stdout)
            if not isinstance(config, dict):
                return None
            if committed_path == "gatesmith.json" and "humanProtected" not in config:
                # Only modelProfile and the like: the template's list
                # governs (2026-10-30 review of #772).
                continue
            hp = config.get("humanProtected", {})
            if not _human_protected_shape_ok(hp):
                return None
            if committed_path == "gatesmith.json":
                return hp
            paths = hp.get("paths")
            if isinstance(paths, list) and paths:
                # The governing template protects itself, as in lint_on_edit.
                hp = {**hp, "paths": [*paths, committed_path]}
            return hp
        except Exception:
            # Broad on purpose: ANY failure reading a committed copy that
            # the cat-file probe said exists must fail CLOSED (None), never
            # silently read as "unconfigured" — includes TypeError from
            # test-context blanket subprocess mocks handing back Mocks.
            return None
    return {}


def _human_protected_shape_ok(hp: object) -> bool:
    """The humanProtected shape lint_on_edit and employee_activator accept;
    anything else fails closed (2026-10-30 review of #772)."""
    if not isinstance(hp, dict):
        return False
    if "enabled" in hp and not isinstance(hp["enabled"], bool):
        return False
    paths = hp.get("paths", [])
    return isinstance(paths, list) and all(isinstance(p, str) for p in paths)


def _human_protected_violations(files: list[str]) -> list[str] | None:
    """Files among *files* that match a humanProtected.paths glob.

    Layer B of the humanProtected fix (PR 260 review finding): the daemon's
    other PR-creation path (employee_activator._capture_code_changes) gained
    this check first, but execute_auto_pr_workflow below stages/commits/pushes
    independently and was not covered by it — a worker whose changes went
    through THIS path could still ship a human-protected edit. Mirrors
    lint_on_edit.py's _matches_pattern (fnmatch, posix paths). Patterns come
    from the COMMITTED config (see _human_protected_config), so a worker
    cannot defeat the check by editing or deleting the working-tree copy.

    Returns [] when there are no violations, None when the committed config
    could not be read — the caller MUST treat None as fail-closed (refuse).
    """
    import fnmatch

    hp = _human_protected_config()
    if hp is None:
        return None
    if not hp.get("enabled", False):
        return []

    def _normalize(path: str) -> str:
        return path[2:] if path.startswith("./") else path

    patterns = [_normalize(p) for p in hp.get("paths", [])]
    return [
        f for f in files if any(fnmatch.fnmatch(_normalize(f), p) for p in patterns)
    ]


def _branch_changed_files() -> list[str]:
    """Names changed on the branch vs origin/main, plus both rename sides.

    PR 265 review major: the humanProtected check inspected only
    ``git status --porcelain`` (uncommitted work) — a worker that COMMITTED
    its protected-file edit (git commit is Guarded-tier, auto-approved)
    shipped it unchecked. ``--no-renames`` makes both sides of a rename
    appear as plain A/D entries. Fails open to [] — the status-based set
    still applies; the branch diff is additive evidence.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "origin/main...HEAD", "--name-only", "--no-renames"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except Exception:
        # Fail-open by contract (the status-based set still applies) — and
        # broad on purpose: callers/tests that mock subprocess.run
        # module-wide hand back bare Mocks whose attributes raise TypeError,
        # which must degrade the same way a git error does.
        return []


@dataclass
class ValidationResult:
    """Result of the security validation gate."""

    passed: bool
    tests_passed: bool | None = None
    secrets_clean: bool | None = None
    lint_passed: bool | None = None
    dependency_issues: list[str] | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None
    report_markdown: str = ""


def _scan_files_for_secrets(files: list[str]) -> tuple[bool, list[str]]:
    """Scan changed files for hardcoded secrets using secrets_scanner.

    Returns:
        Tuple of (clean, findings) where clean is True if no secrets found.
    """
    findings = []
    hooks_dir = Path(__file__).resolve().parent.parent

    try:
        # Import scan_content from secrets_scanner
        sys.path.insert(0, str(hooks_dir))
        from secrets_scanner import scan_content
    except ImportError:
        # If import fails, skip secrets check (don't block)
        return True, ["secrets_scanner not available — skipped"]

    for file_path in files:
        try:
            full_path = Path.cwd() / file_path
            if not full_path.exists() or full_path.is_dir():
                continue
            # Skip binary files
            try:
                content = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            file_findings = scan_content(content, file_path)
            content_lines = content.splitlines()
            for finding in file_findings:
                # Respect # nosec inline suppression
                line_num = finding.get("line", 0)
                if line_num and 0 < line_num <= len(content_lines):
                    if "nosec" in content_lines[line_num - 1]:
                        continue
                findings.append(
                    f"{file_path}: {finding.get('type', 'secret')} "
                    f"(line {finding.get('line', '?')})"
                )
        except OSError:
            continue

    return len(findings) == 0, findings


def _run_lint_checks(files: list[str], auto_fix: bool = True) -> tuple[bool, list[str]]:
    """Run ruff lint and format on changed Python files.

    Args:
        files: List of changed file paths.
        auto_fix: Whether to auto-fix issues.

    Returns:
        Tuple of (passed, issues).
    """
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return True, []

    issues = []

    # Run ruff check
    try:
        cmd = ["ruff", "check"]
        if auto_fix:
            cmd.append("--fix")
        cmd.extend(py_files)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 and result.stdout:
            issues.append(result.stdout.strip()[:500])
        # Stage any auto-fixed files
        if auto_fix:
            subprocess.run(["git", "add", *py_files], capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Run ruff format
    try:
        cmd = ["ruff", "format"]
        cmd.extend(py_files)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # Stage formatted files so they're included in the commit
        if result.returncode == 0:
            subprocess.run(["git", "add", *py_files], capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Re-check after auto-fix
    if auto_fix and issues:
        try:
            result = subprocess.run(
                ["ruff", "check", *py_files],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return True, ["auto-fixed"]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    return len(issues) == 0, issues


def _check_dependencies(files: list[str]) -> tuple[bool, list[str]]:
    """Check for dependency vulnerabilities if requirements files changed.

    Returns:
        Tuple of (clean, warnings).
    """
    req_patterns = [
        "requirements.txt",
        "requirements-*.txt",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
    ]
    import fnmatch

    has_req_change = any(
        any(fnmatch.fnmatch(f, p) for p in req_patterns) for f in files
    )
    if not has_req_change:
        return True, []

    warnings = []
    hooks_dir = Path(__file__).resolve().parent.parent

    try:
        sys.path.insert(0, str(hooks_dir))
        from dependency_check import run_pip_audit

        findings, has_critical = run_pip_audit()
        if findings:
            warnings.extend(findings[:5])  # Limit to 5 findings
    except (ImportError, Exception):
        pass

    return len(warnings) == 0, warnings


def run_security_validation_gate(
    changed_files: list[str],
    config: dict,
) -> ValidationResult:
    """Run the security validation gate on changed files.

    Chains validators:
    1. Tests (pytest) — hard gate
    2. Secrets scan — hard gate
    3. Lint/quality — soft gate (auto-fix)
    4. Dependency check — advisory

    Args:
        changed_files: List of deliverable file paths.
        config: Auto-PR configuration dict.

    Returns:
        ValidationResult with all check outcomes.
    """
    errors = []
    warnings = []
    tests_passed = None
    secrets_clean = None
    lint_passed = None
    dep_issues = None
    report_lines = []

    # 1. Tests
    if config.get("requireTestsPass", True):
        test_result = run_tests()
        tests_passed = test_result["success"]
        if tests_passed and test_result.get("ran") is False:
            report_lines.append("| Tests | **None ran** |")
            warnings.append(
                "No test ran: no test file matches the changed files. "
                "Review this change by running it."
            )
        elif tests_passed:
            report_lines.append("| Tests | Passed |")
        else:
            report_lines.append("| Tests | **FAILED** |")
            errors.append(
                test_result.get("error", test_result.get("output", "Tests failed"))[
                    :500
                ]
            )
    else:
        report_lines.append("| Tests | Skipped |")

    # 2. Secrets scan
    if config.get("requireSecretsClean", True):
        secrets_clean, secret_findings = _scan_files_for_secrets(changed_files)
        if secrets_clean:
            report_lines.append("| Secrets scan | Clean |")
        else:
            report_lines.append("| Secrets scan | **BLOCKED** |")
            errors.extend(secret_findings)
    else:
        report_lines.append("| Secrets scan | Skipped |")

    # 3. Lint
    if config.get("requireLintPass", True):
        lint_passed, lint_issues = _run_lint_checks(
            changed_files, auto_fix=config.get("autoFixLint", True)
        )
        if lint_passed:
            if lint_issues and "auto-fixed" in lint_issues:
                report_lines.append("| Lint/format | Auto-fixed |")
            else:
                report_lines.append("| Lint/format | Passed |")
        else:
            report_lines.append("| Lint/format | **Issues** |")
            warnings.extend(lint_issues)
    else:
        report_lines.append("| Lint/format | Skipped |")

    # 4. Dependency check
    if config.get("checkDependencies", True):
        dep_clean, dep_warnings = _check_dependencies(changed_files)
        dep_issues = dep_warnings if dep_warnings else None
        if dep_clean:
            report_lines.append("| Dependencies | Clean |")
        else:
            report_lines.append("| Dependencies | Warnings |")
            warnings.extend(dep_warnings)
    else:
        report_lines.append("| Dependencies | Skipped |")

    # Determine overall pass/fail
    passed = True
    if tests_passed is False:
        passed = False
    if secrets_clean is False:
        passed = False
    # Lint and dependency issues are soft — don't block

    # Build report markdown
    report_md = "| Check | Result |\n|-------|--------|\n"
    report_md += "\n".join(report_lines)

    if warnings:
        report_md += "\n\n### Warnings\n"
        for w in warnings[:10]:
            report_md += f"- {w}\n"

    return ValidationResult(
        passed=passed,
        tests_passed=tests_passed,
        secrets_clean=secrets_clean,
        lint_passed=lint_passed,
        dependency_issues=dep_issues,
        errors=errors if errors else None,
        warnings=warnings if warnings else None,
        report_markdown=report_md,
    )


def create_daemon_feature_branch(
    task_id: str, task_title: str, prefix: str = "daemon"
) -> dict:
    """Create feature branch for a daemon-executed task.

    Branch naming: {prefix}/{task-id}-{slug}
    Does NOT checkout main or pull — daemon is already on main.

    Args:
        task_id: Task identifier.
        task_title: Task title for slug generation.
        prefix: Branch prefix (default "daemon").

    Returns:
        Dict with success, branch_name, error.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task_title.lower()).strip("-")[:30]
    branch_name = f"{prefix}/{task_id}-{slug}"

    try:
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"success": True, "branch_name": branch_name}
        # Branch might already exist — try with timestamp suffix
        import time

        branch_name = f"{prefix}/{task_id}-{slug}-{int(time.time()) % 10000}"
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"success": True, "branch_name": branch_name}
        return {
            "success": False,
            "error": result.stderr.strip(),
            "branch_name": branch_name,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git operation timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def return_to_main_branch() -> dict:
    """Return to main branch using stash + force-checkout + unstash pattern.

    Stashes any uncommitted changes before checking out so the checkout
    succeeds even when the working directory is dirty.  The stash is
    popped afterward to restore those changes on the main branch.

    Returns:
        Dict with success and error.
    """
    main_branch = get_main_branch()
    stashed = False
    try:
        # Step 1: stash any uncommitted changes so checkout won't be blocked
        stash_result = subprocess.run(
            ["git", "stash", "--include-untracked"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stashed = (
            stash_result.returncode == 0
            and "No local changes to save" not in stash_result.stdout
        )

        # Step 2: force-checkout the main branch
        checkout_result = subprocess.run(
            ["git", "checkout", "--force", main_branch],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            # Restore stash before returning failure so caller's state is intact
            if stashed:
                subprocess.run(
                    ["git", "stash", "pop"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            return {"success": False, "error": checkout_result.stderr.strip()}

        # Step 3: restore stashed changes (best-effort)
        if stashed:
            pop_result = subprocess.run(
                ["git", "stash", "pop"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if pop_result.returncode != 0:
                return {
                    "success": True,
                    "error": f"Checkout succeeded but stash pop failed: {pop_result.stderr.strip()}",
                }

        return {"success": True, "error": ""}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git operation timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def create_daemon_draft_pr(
    task_id: str,
    task_title: str,
    task_description: str,
    branch_name: str,
    validation: ValidationResult,
    employee_id: str | None = None,
    is_draft: bool = True,
) -> dict:
    """Create PR with security validation report in body.

    When ``is_draft`` is True the PR is created with ``--draft``; otherwise a
    ready-for-review PR is opened. Either way a PR is created — gating PR
    creation on the draft flag previously caused work to be pushed without a
    PR, producing phantom completions (WS-119 1.8).

    Args:
        task_id: Task identifier.
        task_title: PR title.
        task_description: Task description for PR body.
        branch_name: Source branch.
        validation: Validation gate results.
        employee_id: Employee who executed the task.
        is_draft: When True, open the PR as a draft. Defaults to True.

    Returns:
        Dict with success, pr_url, error.
    """
    main_branch = get_main_branch()

    body = f"""## Summary

{task_description}

## Task Info

| Field | Value |
|-------|-------|
| Task ID | `{task_id}` |
| Employee | `{employee_id or "daemon"}` |
| Source | Daemon auto-PR |

## Security Validation Report

{validation.report_markdown}

## Test Plan

- [ ] Review code changes
- [ ] Verify tests pass in CI
- [ ] Manual verification (if needed)

---
Generated by Gatesmith Daemon (Auto-PR Workflow)
"""

    try:
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"success": False, "error": "gh CLI not available"}

        gh_args = ["gh", "pr", "create"]
        if is_draft:
            gh_args.append("--draft")
        gh_args.extend(
            [
                "--title",
                f"[daemon] {task_title}",
                "--body",
                body,
                "--base",
                main_branch,
                # Opens held until its head is judged (verdict_hold).
                *_verdict_hold().create_args(),
            ]
        )
        result = subprocess.run(
            gh_args,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            return {
                "success": True,
                "pr_url": result.stdout.strip(),
                "branch_name": branch_name,
            }
        return {"success": False, "error": result.stderr.strip()}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PR creation timed out"}
    except OSError as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Pre-merge Deliverable Gate (Phase 2)
# =============================================================================


def automerge_to_main_allowed() -> bool:
    """Whether daemon PRs may enable GitHub auto-merge to main.

    Reads ``autonomy.autoMerge.{enabled, allowMergeToMain, requireApproval}``
    from the governing config (governing_config: the root when it declares
    the section, else the install template). Fail-safe: a missing/unreadable
    config or key returns False, so a broken or absent config never silently
    auto-merges to main.
    ``requireApproval: true`` also refuses: native ``--auto`` merges as soon as
    checks pass unless branch protection demands a review, so arming it would
    skip the approval the operator asked for. The daemon's own merge loop
    merges such a PR once it carries an approving review. The older copy,
    ``employee_activator._automerge_to_main_allowed``, has no caller left and
    reads only the root config; this is the gate.
    """
    auto_merge, error = _governed_auto_merge()
    if error is not None:
        return False
    require_approval = auto_merge.get("requireApproval", False)
    if require_approval is not False:
        # true, or a malformed non-bool value -> fail closed.
        return False
    return auto_merge.get("enabled", False) is True and (
        auto_merge.get("allowMergeToMain", False) is True
    )


def _pr_files_and_labels(pr_number: str) -> tuple[list[str], list[str]] | None:
    """Fetch changed file paths and label names for a PR via gh.

    Returns (changed_files, label_names) or None on any gh error (used to fail
    closed: a None result means do NOT enable auto-merge).
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "files,labels"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        files = [f.get("path", "") for f in (data.get("files") or [])]
        labels = _pr_label_names(data)
        return files, labels
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


# Per-process caches for branch-protection and CI-run checks.
# Keyed by (owner, repo, base_branch) and (owner, repo) respectively.
# These values are stable within a daemon run — protection rules and run
# history only grow; caching eliminates per-PR API spam.
_protection_cache: dict[tuple[str, str, str], bool] = {}
_ci_runs_cache: dict[tuple[str, str], bool] = {}


def _get_pr_base_info(pr_number: str) -> tuple[str, str, str] | None:
    """Return (owner, repo_name, base_branch) for a PR. None on any error (fail closed)."""
    try:
        pr_result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "baseRefName"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pr_result.returncode != 0:
            return None
        base_branch = json.loads(pr_result.stdout).get("baseRefName", "")
        if not base_branch:
            return None
        repo_result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if repo_result.returncode != 0:
            return None
        name_with_owner = json.loads(repo_result.stdout).get("nameWithOwner", "")
        if "/" not in name_with_owner:
            return None
        owner, repo = name_with_owner.split("/", 1)
        return (owner, repo, base_branch)
    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


def _repo_has_branch_protection(owner: str, repo: str, base: str) -> bool:
    """Return True if the base branch has required status checks configured.

    Fail closed: any gh error returns False (treat as no protection).
    Result is cached per (owner, repo, base) for the process lifetime.
    """
    key = (owner, repo, base)
    if key in _protection_cache:
        return _protection_cache[key]
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/branches/{base}/protection"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            _protection_cache[key] = False
            return False
        data = json.loads(result.stdout)
        rsc = data.get("required_status_checks") or {}
        has_checks = bool(rsc.get("contexts") or rsc.get("checks"))
        _protection_cache[key] = has_checks
        return has_checks
    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ):
        _protection_cache[key] = False
        return False


def _repo_has_ci_runs(owner: str, repo: str) -> bool:
    """Return True if the repo has at least one Actions run in its history.

    Fail closed: any gh error returns False.
    Result is cached per (owner, repo) for the process lifetime.
    """
    key = (owner, repo)
    if key in _ci_runs_cache:
        return _ci_runs_cache[key]
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/actions/runs?per_page=1"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            _ci_runs_cache[key] = False
            return False
        data = json.loads(result.stdout)
        has_runs = int(data.get("total_count", 0)) >= 1
        _ci_runs_cache[key] = has_runs
        return has_runs
    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ):
        _ci_runs_cache[key] = False
        return False


def _repo_has_required_gates(pr_number: str) -> tuple[bool, str]:
    """Check whether the repo has the GitHub gates required for safe auto-merge.

    Both checks must pass:
      (a) the base branch has required status checks (branch protection rule), AND
      (b) the repo has at least one Actions run in its history.

    If either is absent, GitHub's ``--auto`` flag will merge immediately without
    waiting for any checks — indistinguishable from a direct merge.

    Returns ``(True, "")`` when gates are present.
    Returns ``(False, reason)`` when gateless (or on any gh error — fail closed).
    """
    repo_info = _get_pr_base_info(pr_number)
    if repo_info is None:
        return False, "could not fetch repo info from gh"
    owner, repo, base = repo_info
    has_protection = _repo_has_branch_protection(owner, repo, base)
    has_runs = _repo_has_ci_runs(owner, repo)
    if not has_protection and not has_runs:
        return (
            False,
            f"base branch '{base}' has no required status checks and the repo has no CI run history",
        )
    if not has_protection:
        return False, f"base branch '{base}' has no required status checks"
    if not has_runs:
        return False, "repo has no CI run history"
    return True, ""


def _post_gateless_comment(pr_number: str, reason: str) -> None:
    """Post an explanatory comment when auto-merge is withheld due to a gateless repo."""
    body = (
        f"⚠️ **Auto-merge not armed**: {reason}.\n\n"
        "Without required status checks GitHub's `--auto` flag merges immediately "
        "with no checks executed. To enable safe auto-merge, add branch protection "
        "rules with required status checks. See `bin/gatesmith-protect-main` for a "
        "setup script, or set `autonomy.autoMerge.allowGatelessRepos: true` in "
        "`gatesmith.json` to override (not recommended for production repos)."
    )
    try:
        subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _load_auto_merge_gate_config() -> tuple[dict, str | None]:
    """The governing ``autonomy.autoMerge`` for the enable_github_auto_merge
    arming gate, wrapped as ``{"autonomy": {"autoMerge": ...}}``.

    Returns ``({}, None)`` when no config declares the section — the guards
    below then use their built-in defaults (the default blockedPaths and
    reviewPaths seeds), never "no restrictions".

    Returns ``({}, reason)`` when the governing file cannot be trusted: a
    JSON parse error, a non-object root or section, or
    ``autonomy.autoMerge.reviewPaths`` present but not a list of strings.
    These are read/shape failures, not "unconfigured" — the caller must
    refuse to arm rather than silently treat a corrupt config as "no
    restrictions" (PR 257, 2026-07-17: the whole point of reviewPaths is to
    hold gate/audit/steering changes for a human; a config the gate can't
    parse must not silently waive that hold). A fresh install's template
    governs when the root does not declare the section (2026-10-31).
    """
    auto_merge, error = _governed_auto_merge()
    if error is not None:
        return {}, error
    if not auto_merge:
        return {}, None
    data = {"autonomy": {"autoMerge": auto_merge}}
    review_paths = auto_merge.get("reviewPaths", [])
    if not isinstance(review_paths, list) or not all(
        isinstance(p, str) for p in review_paths
    ):
        return (
            {},
            "autonomy.autoMerge.reviewPaths is malformed (expected a list of strings)",
        )
    return data, None


_REVIEW_PATHS_REASON_PREFIX = "review_paths match: "


def enable_github_auto_merge(pr_url_or_number: str, *, method: str = "squash") -> bool:
    """Enable GitHub native auto-merge on a PR (``gh pr merge --auto``).

    Guards (fail closed — returns False on any refusal or gh error):
    1. Refuses if the PR carries the needs-manual-review label. GitHub's native
       auto-merge ignores labels, so a label alone cannot veto an --auto
       enablement made by another actor; this must be checked here.
    2. Refuses if any changed file exactly matches autonomy.autoMerge.blockedPaths
       (same exact-match semantics as the ci.yml "Check for protected files" step).
    3. Refuses if any changed file matches an autonomy.autoMerge.reviewPaths glob
       (gate/audit/steering subsystem hold — broader than blockedPaths' exact-match
       list, so a whole subsystem can be marked "always needs human review" without
       enumerating every file. PR 257, 2026-07-17: a daemon PR touching the trust
       instrument that judges the platform's own health armed and merged before the
       operator's review hold could run — blockedPaths' 3-file exact list didn't
       cover it).
    4. Refuses if gatesmith.json exists but cannot be read/parsed, or if
       reviewPaths is present but not a list of strings — a config the gate
       can't verify must never be treated as "no restrictions" (PR 257 follow-up,
       2026-07-18: guards 2+3 previously fell back to an empty dict on any read
       error via load_config_root(), which fails OPEN, not closed).
    5. Refuses if the base branch has no required status checks OR the repo has no
       CI run history (gateless repo), unless autonomy.autoMerge.allowGatelessRepos
       is True. On a gateless repo GitHub's --auto merges immediately; this guard
       prevents silent zero-check merges (finding D5).
    6. Returns False on any gh fetch or merge error (fail closed).

    This is the 6th independent-lever fix; parity between daemon-side and CI
    levers is the invariant (each lever must enforce the same gates).

    Thin wrapper over ``_enable_github_auto_merge_impl`` — see that function
    for the refusal-reason variant used by ``run_deliverable_gate`` to give
    reviewPaths holds a specific operator-facing comment without a second gh
    fetch.
    """
    armed, _reason = _enable_github_auto_merge_impl(pr_url_or_number, method=method)
    return armed


def _enable_github_auto_merge_impl(
    pr_url_or_number: str, *, method: str = "squash", head_sha: str | None = None
) -> tuple[bool, str | None]:
    """Implementation behind ``enable_github_auto_merge`` — also returns why.

    Returns ``(True, None)`` on success. Returns ``(False, reason)`` on any
    refusal or error. ``reason`` is prefixed with ``_REVIEW_PATHS_REASON_PREFIX``
    specifically for a reviewPaths match, so callers that want to distinguish
    "held for gate/audit/steering review" from every other refusal reason
    (label present, blockedPaths, gateless repo, gh error) can do so with a
    single ``str.startswith`` check rather than re-deriving the match.

    ``head_sha`` (the commit the deliverable verdict judged) pins the arm with
    ``--match-head-commit``: GitHub refuses to arm when the head already moved
    past the judged commit (single-merge-gate review).
    """
    pr_number = str(pr_url_or_number)

    # Guards 1+2: fetch files + labels; None means gh error → fail closed
    pr_data = _pr_files_and_labels(pr_number)
    if pr_data is None:
        return False, "gh fetch error (files/labels)"

    changed_files, label_names = pr_data

    # Guard 1: refuse if the deliverable-gate label is present
    if _gate_label() in label_names:
        return False, "needs-manual-review label present"
    # Guard 1b: refuse while the head still awaits a verdict. run_deliverable_gate
    # releases this label only for a positive verdict on the CURRENT head, so a
    # label still present here means the head moved or the release failed.
    pending = _verdict_hold().PENDING_LABEL
    if pending in label_names:
        return False, f"{pending} label present"
    # Guard 1c (CHALLENGER-STAGE): no arming while the challenger holds the PR,
    # labelled or not yet (decided from the PR's queue task).
    challenger_held = _challenger_hold_labels() & set(label_names)
    if challenger_held:
        return False, f"{', '.join(sorted(challenger_held))} label present"
    challenger_block = _challenger_merge_block(pr_number)
    if challenger_block:
        return False, challenger_block
    # Guard 1d (single-merge-gate): every other hold label any merge lever
    # honours — awaiting-operator-review (the operator's manual brake),
    # needs-human, do-not-merge. Native --auto ignores labels once armed.
    other_held = _merge_hold_labels() & set(label_names)
    if other_held:
        return False, f"{', '.join(sorted(other_held))} label present"

    # Guard 4 (config read): fail closed on an unreadable/malformed config so
    # guards 2+3 below never silently run against an empty fallback dict.
    cfg, cfg_err = _load_auto_merge_gate_config()
    if cfg_err is not None:
        print(
            f"[pr_output_manager] auto-merge arming refused for PR {pr_number}: "
            f"{cfg_err}",
            file=sys.stderr,
        )
        return False, f"config error: {cfg_err}"

    # Guard 2: refuse if any changed file matches a blockedPath. An ABSENT key
    # is the daemon's default list (merge_gate), never "nothing is blocked".
    mg = _merge_gate()
    policy = mg.MergePolicy.from_dict(
        (cfg.get("autonomy", {}) or {}).get("autoMerge", {}) or {}
    )
    blocked_hits = mg.blocked_path_hits(changed_files, policy)
    if blocked_hits:
        return False, f"blocked_paths match: {blocked_hits[0]}"

    # Guard 3: refuse if any changed file matches a reviewPaths glob (ci.yml
    # parity via the shared auto_merge_paths module — one seed, one matcher).
    # An ABSENT reviewPaths key resolves to the built-in default seed, never
    # to "no holds" (PR 260 review blocker: the seed lived only in a config
    # hunk the operator was required to strip, leaving the feature inert).
    from auto_merge_paths import (
        approval_paths,
        match_review_paths,
        resolve_review_paths,
    )

    review_paths = resolve_review_paths(
        cfg.get("autonomy", {}).get("autoMerge", {}).get("reviewPaths")
    )
    # humanProtected.requireApprovalFor holds too; an untrusted file holds all.
    try:
        human_protected, hp_err = _governing_config().section(
            "humanProtected", Path.cwd()
        )
    except Exception:  # noqa: BLE001 - fail closed
        human_protected, hp_err = None, "unreadable"
    review_paths += ["*"] if hp_err else approval_paths(human_protected or None)
    review_matched = match_review_paths(changed_files, review_paths)
    if review_matched:
        return False, _REVIEW_PATHS_REASON_PREFIX + ", ".join(review_matched)

    # Guard 5: refuse on gateless repos (no required checks + no CI history).
    # allowGatelessRepos=true is an escape hatch for sandbox/test environments.
    allow_gateless = bool(
        (cfg.get("autonomy", {}) or {})
        .get("autoMerge", {})
        .get("allowGatelessRepos", False)
    )
    if not allow_gateless:
        gates_ok, reason = _repo_has_required_gates(pr_number)
        if not gates_ok:
            _post_gateless_comment(pr_number, reason)
            return False, f"gateless repo: {reason}"

    # Guard 6 (single-merge-gate): the shared decision, in arming mode (every
    # gate but checks/merge state, which native auto-merge waits for itself):
    # secrets, diff caps, author, base, rename-aware paths, verdict. The arm
    # is pinned to the head it judged.
    try:
        root = Path.cwd()
        gate_refusal, judged = _merge_gate().may_merge(
            pr_number,
            company_dir=root / ".company",
            project_root=root,
            expected_head=head_sha,
            arming=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        gate_refusal, judged = f"merge gate unavailable: {exc}", None
    if gate_refusal:
        return False, f"merge gate: {gate_refusal}"
    if not judged:
        return False, "merge gate judged no head"
    head_sha = judged

    try:
        result = subprocess.run(
            ["gh", "pr", "merge", str(pr_url_or_number), "--auto", f"--{method}"]
            + ["--match-head-commit", head_sha],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, None
        return False, f"gh merge command failed: {result.stderr.strip()[:200]}"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"gh merge error: {e}"


def apply_manual_review_label(
    pr_number: str | int,
    comment_body: str | None = None,
    label: str = "needs-manual-review",
) -> bool:
    """Apply the manual-review label to a PR and (optionally) post a comment.

    Idempotent and best-effort: ensures the label exists, adds it, and posts the
    reason as a comment. A labeling/commenting hiccup must NEVER be fatal — the
    safety-critical action (the daemon NOT auto-merging) is enforced by the gate
    that calls this, mirroring ci.yml's non-fatal label step.

    Also disarms any already-armed GitHub native auto-merge (``--disable-auto``):
    the label alone does not stop a merge that was armed *before* this hold was
    applied — GitHub's native auto-merge ignores labels once armed (PR #248,
    2026-07-17: an armed merge fired the instant CI went green, with the
    needs-manual-review label already present the whole time).

    Returns True if the label was added without error, else False.
    """
    ok = True
    # Disarm first — before the label even exists — to shrink the window where a
    # PR reads as "held" but is still armed. Disarming an unarmed (or
    # already-merged/closed) PR errors, which is expected and must never affect
    # `ok`, which reflects only the label-apply outcome below.
    try:
        subprocess.run(
            ["gh", "pr", "merge", str(pr_number), "--disable-auto"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Ensure the label exists (idempotent — ignore "already exists").
    try:
        subprocess.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                "fbca04",
                "--description",
                "Daemon PR left open by a pre-merge gate; needs a human merge",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        result = subprocess.run(
            ["gh", "pr", "edit", str(pr_number), "--add-label", label],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            ok = False
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    if comment_body:
        try:
            subprocess.run(
                ["gh", "pr", "comment", str(pr_number), "--body", comment_body],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    return ok


def _deliverable_hold_comment(task_id: str, verdict) -> str:
    """The PR comment posted when the deliverable judge holds a PR."""
    if verdict.error:
        return (
            "🛑 **Pre-merge deliverable gate: needs manual review**\n\n"
            f"The deliverable judge could not confirm this PR addresses task "
            f"`{task_id}` (fail-closed): {verdict.error}\n\n"
            "Auto-merge is blocked until a human reviews and merges."
        )
    conf = f"{verdict.confidence:.2f}" if verdict.confidence is not None else "n/a"
    return (
        "🛑 **Pre-merge deliverable gate: needs manual review**\n\n"
        f"The deliverable judge does not consider this PR to substantively "
        f"address task `{task_id}` (confidence {conf}).\n\n"
        f"> {verdict.reason}\n\n"
        "Auto-merge is blocked until a human reviews and merges."
    )


def hold_pr_for_verdict(task_id: str, pr_url: str | None, verdict) -> dict:
    """Hold a PR whose deliverable verdict the caller already has, and record it.

    For a caller that judged the PR itself (the workflow executor) and then
    FAILED the task on that verdict. ``run_deliverable_gate`` is the wrong tool
    there: it runs only on a successful task in ``operation_loop``, and on a
    cache miss (the head moved between judging and gating) it re-judges and, on
    a pass, ARMS auto-merge — on a task that was just marked failed. This
    function never judges and never arms: it applies the gate's hold label
    (which also disarms any armed native auto-merge, and is what ci.yml's
    Auto-Merge job reads) and appends the verdict to ``deliverable_gate.jsonl``
    so the audit trail and the daemon's merge gate can find it.

    Best-effort per step; returns {label_applied, recorded}.
    """
    dj = _ensure_deliverable_judge()
    out = {"label_applied": False, "recorded": False}
    try:
        verdict.pr_url = verdict.pr_url or pr_url
        dj._record_decision(verdict)
        out["recorded"] = True
    except Exception as exc:  # noqa: BLE001 — recording must never break callers
        print(f"[pr_output_manager] verdict record failed: {exc}", file=sys.stderr)
    pr_number = _pr_number_from_url(pr_url)
    if pr_number:
        out["label_applied"] = apply_manual_review_label(
            pr_number, _deliverable_hold_comment(task_id, verdict), _gate_label()
        )
    return out


def _deliverable_pass_comment(task_id: str, verdict, auto_merge_enabled: bool) -> str:
    """The PR comment posted when the deliverable judge passes a PR.

    Without it a passing verdict was invisible: only a hold commented, so a
    reviewer could not tell a checked PR from an unchecked one.
    """
    conf = f"{verdict.confidence:.2f}" if verdict.confidence is not None else "n/a"
    after = (
        "Auto-merge is armed: it merges when the required checks pass."
        if auto_merge_enabled
        else "Nothing merges by itself: a human reviews and merges."
    )
    return (
        "✅ **Pre-merge deliverable gate: passed**\n\n"
        f"The deliverable judge considers this PR to address task "
        f"`{task_id}` (confidence {conf}).\n\n"
        f"> {verdict.reason}\n\n"
        f"{after} The judge reads the diff; it does not run the code."
    )


def _post_pr_comment(pr_number: str, body: str) -> None:
    """Best-effort ``gh pr comment``; a failure never affects the gate."""
    try:
        subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_deliverable_gate(
    task_id: str,
    task_title: str,
    task_description: str,
    pr_url: str | None,
    branch_name: str | None,
    daemon_executed: bool = False,
) -> dict:
    """Run the pre-merge deliverable judge on a freshly-created daemon PR.

    On a block verdict (the diff does not substantively address the task, the
    judge could not verify, or the judge errored under the fail-closed default),
    apply the ``needs-manual-review`` label + post the reason as a comment. This
    NEVER closes the PR and never marks the task failed — the work is preserved,
    just held back from auto-merge for a human.

    Eligibility: the gate runs when ``daemon_executed`` is True (the caller knows
    this PR came out of the daemon's autonomous worker pipeline) OR the branch
    carries a daemon prefix. Keying on EXECUTION CONTEXT — not just the branch
    name — closes the bypass where a worker self-names its branch (e.g. a WS-103
    git-capture PR on ``docs/…`` / ``feat/…`` instead of ``daemon/…``), which used
    to skip the gate entirely. Human-authored PRs (neither flag) are never gated.

    Returns a dict: {ran, blocked, reason, addresses_task, confidence, label_applied}.
    """
    dj = _ensure_deliverable_judge()
    config = dj.load_gate_config()
    label = config.get("label", "needs-manual-review")

    if not config.get("enabled", True):
        dj._record_skip(task_id, pr_url, "deliverable gate disabled")
        return {"ran": False, "blocked": False, "reason": "deliverable gate disabled"}
    if not (
        daemon_executed
        or dj.is_daemon_branch(branch_name, config.get("branchPrefixes"))
    ):
        return {"ran": False, "blocked": False, "reason": "not a daemon-executed PR"}
    pr_number = _pr_number_from_url(pr_url)
    if not pr_number:
        dj._record_skip(task_id, pr_url, "no PR number extractable from URL")
        return {"ran": False, "blocked": False, "reason": "no PR number to judge"}

    verdict = dj.judge_pr_deliverable(
        task_id=task_id,
        title=task_title,
        description=task_description,
        pr_number=pr_number,
        branch=branch_name,
        config=config,
    )

    label_applied = False
    if verdict.blocked:
        comment = _deliverable_hold_comment(task_id, verdict)
        label_applied = apply_manual_review_label(pr_number, comment, label)

    # Gate-then-merge: enable GitHub auto-merge ONLY on a passing verdict, and only
    # when allowMergeToMain permits. This is what makes the deliverable gate
    # LOAD-BEARING — previously the worker enabled native `--auto` at PR creation
    # (before this gate ran), and native auto-merge ignores the needs-manual-review
    # label, so a blocked PR could still merge. Now the daemon only ever arms
    # auto-merge AFTER the gate clears the PR.
    # A positive verdict releases the pending-verdict hold — only if it judged
    # the PR's CURRENT head (a push after judging keeps the hold on).
    pending_released = False
    if not verdict.blocked:
        try:
            vh = _verdict_hold()
            pending_released = vh.release_if_positive_for_head(
                pr_number,
                verdict.sha,
                vh.is_positive(
                    {"addresses_task": verdict.addresses_task, "blocked": False}
                ),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; the arm then refuses
            print(f"[pr_output_manager] pending release failed: {exc}", file=sys.stderr)

    auto_merge_enabled = False
    review_paths_hold = False
    if not verdict.blocked and automerge_to_main_allowed():
        # PR 257 (2026-07-17): use the reason-returning impl (single gh fetch,
        # same guards as enable_github_auto_merge) so a reviewPaths refusal is
        # immediately visible via a specific label + comment instead of a
        # silent no-arm — the exact failure mode from the incident.
        auto_merge_enabled, arm_reason = _enable_github_auto_merge_impl(
            pr_number, head_sha=getattr(verdict, "sha", None) or None
        )
        if (
            not auto_merge_enabled
            and arm_reason
            and arm_reason.startswith(_REVIEW_PATHS_REASON_PREFIX)
        ):
            review_paths_hold = True
            matched_files = arm_reason[len(_REVIEW_PATHS_REASON_PREFIX) :]
            review_comment = (
                "🔒 **Operator review required**\n\n"
                f"This PR touches a file matching "
                f"`autonomy.autoMerge.reviewPaths` or "
                f"`humanProtected.requireApprovalFor` ({matched_files}) — "
                "gate/audit/steering subsystems always need a human review "
                "before merge. Auto-merge was not armed.\n\n"
                "An operator must review and merge this manually."
            )
            # Distinct label (brief requirement): a review hold means "sound
            # work on a sensitive surface — review, then merge", which the
            # operator must be able to tell apart from needs-manual-review's
            # "deliverable gate suspects phantom work" at a glance.
            from auto_merge_paths import REVIEW_HOLD_LABEL

            label_applied = (
                apply_manual_review_label(pr_number, review_comment, REVIEW_HOLD_LABEL)
                or label_applied
            )

    # A pass is shown on the PR too, once per judged head (a cached verdict
    # was already posted when it was first reached).
    if not verdict.blocked and not verdict.error and not verdict.cached:
        _post_pr_comment(
            pr_number,
            _deliverable_pass_comment(task_id, verdict, auto_merge_enabled),
        )

    return {
        "ran": True,
        "blocked": verdict.blocked,
        "reason": verdict.reason,
        "addresses_task": verdict.addresses_task,
        "confidence": verdict.confidence,
        "error": verdict.error,
        "label_applied": label_applied,
        "auto_merge_enabled": auto_merge_enabled,
        "review_paths_hold": review_paths_hold,
        "pending_released": pending_released,
    }


@dataclass
class AutoPRResult:
    """Result of the auto-PR workflow for daemon tasks."""

    success: bool
    task_id: str
    branch_name: str | None = None
    pr_url: str | None = None
    validation: ValidationResult | None = None
    error: str | None = None
    skipped_reason: str | None = None
    steps_completed: list[str] | None = None
    deliverable_blocked: bool = False
    deliverable_reason: str | None = None
    # Scratch-shaped new files left out of the commit (PR-SCRATCH-GUARD);
    # they stay untracked on disk.
    scratch_stripped: list[str] | None = None


def snapshot_working_tree(exclude_patterns: list[str] | None = None) -> dict[str, str]:
    """Capture a snapshot of file modification times for later diffing.

    Call this BEFORE task execution. After execution, pass the result to
    execute_auto_pr_workflow(pre_task_snapshot=...) so only files that
    changed during the task are included in the PR.

    Returns:
        Dict mapping file path to its mtime (ISO format) or 'new' for untracked.
    """
    import fnmatch

    if exclude_patterns is None:
        exclude_patterns = load_auto_pr_config()["excludePathPatterns"]

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}

        snapshot = {}
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            file_path = line[3:].strip().strip('"')
            if " -> " in file_path:
                file_path = file_path.split(" -> ", 1)[1]
            if any(fnmatch.fnmatch(file_path, p) for p in exclude_patterns):
                continue
            # Record current state
            try:
                mtime = Path(file_path).stat().st_mtime
                snapshot[file_path] = str(mtime)
            except OSError:
                snapshot[file_path] = "missing"
        return snapshot

    except (subprocess.TimeoutExpired, OSError):
        return {}


def execute_auto_pr_workflow(
    task_id: str,
    task_title: str,
    task_description: str,
    employee_id: str | None = None,
    pre_task_snapshot: dict[str, str] | None = None,
) -> AutoPRResult:
    """Execute the auto-PR workflow for a daemon-completed task.

    Full flow:
    1. Load config (skip if disabled)
    2. Detect deliverable changed files (skip if none)
    3. Run security validation gate
    4. Handle hard failures (secrets, tests)
    5. Create feature branch
    6. Stage and commit deliverable files only
    7. Push feature branch
    8. Create draft PR with validation report
    9. Return to main branch

    Args:
        task_id: Task identifier.
        task_title: Task title for branch/PR naming.
        task_description: Description for PR body.
        employee_id: Employee who executed the task.

    Returns:
        AutoPRResult with workflow outcome.
    """
    config = load_auto_pr_config()
    steps: list[str] = []

    if not config["enabled"]:
        return AutoPRResult(
            success=True, task_id=task_id, skipped_reason="auto-PR disabled"
        )

    # Safety: ensure we're on main
    current = get_current_branch()
    main_branch = get_main_branch()
    if current != main_branch:
        return_to_main_branch()
        current = get_current_branch()
        if current != main_branch:
            return AutoPRResult(
                success=False,
                task_id=task_id,
                error=f"Not on main branch: {current}",
            )

    # 1. Detect deliverable changes
    changed_files = get_deliverable_changed_files(config["excludePathPatterns"])

    # P81: If pre-task snapshot provided, only include files that changed
    # DURING this task (new files or files with different mtime).
    if pre_task_snapshot is not None and changed_files:
        task_files = []
        for f in changed_files:
            if f not in pre_task_snapshot:
                # New file (didn't exist before task)
                task_files.append(f)
            else:
                # Existing file — check if mtime changed
                try:
                    current_mtime = str(Path(f).stat().st_mtime)
                    if current_mtime != pre_task_snapshot[f]:
                        task_files.append(f)
                except OSError:
                    pass  # File disappeared
        changed_files = task_files
    if not changed_files:
        return AutoPRResult(
            success=True, task_id=task_id, skipped_reason="no deliverable changes"
        )
    steps.append("detect_changes")

    # Layer B humanProtected enforcement (PR 260 review finding): refuse to
    # stage/commit/push a PR that touches a human-protected path, before any
    # branch/commit/push work happens. The checked set is the union of
    # uncommitted work (git status) and the branch diff vs origin/main
    # (PR 265 review: a worker's own `git commit` otherwise shipped
    # unchecked), and the commit below stages index-wide, so every path the
    # commit could ship is in this set.
    checked_files = sorted(set(changed_files) | set(_branch_changed_files()))
    protected_hits = _human_protected_violations(checked_files)
    if protected_hits is None:
        print(
            f"[humanProtected] REFUSED auto-PR for {task_id}: committed "
            "gatesmith.json unreadable — failing closed",
            file=sys.stderr,
        )
        return AutoPRResult(
            success=False,
            task_id=task_id,
            error=(
                "committed gatesmith.json could not be read/parsed, so "
                "human-protected paths cannot be verified — failing closed"
            ),
            steps_completed=steps,
        )
    if protected_hits:
        print(
            f"[humanProtected] REFUSED auto-PR for {task_id}: diff touches "
            + ", ".join(sorted(protected_hits)),
            file=sys.stderr,
        )
        return AutoPRResult(
            success=False,
            task_id=task_id,
            error=(
                "Refusing to create PR: touches human-protected path(s): "
                + ", ".join(sorted(protected_hits))
            ),
            steps_completed=steps,
        )

    # PR-SCRATCH-GUARD: a scratch-shaped NEW file that is not yet committed is
    # left out of the commit (it stays on disk); a flagged file already
    # committed, or a new root-level file the task text does not name, refuses
    # the PR. See pr_scratch_guard's module doc for the rules and why.
    _sg = _scratch_guard().check(
        None, task_title, task_description, stage_list=changed_files
    )
    if _sg.refused:
        print(
            f"[scratch-guard] REFUSED auto-PR for {task_id}: {_sg.reason}",
            file=sys.stderr,
        )
        return AutoPRResult(
            success=False,
            task_id=task_id,
            error=_sg.reason,
            steps_completed=steps,
        )
    scratch_stripped: list[str] = []
    if _sg.stripped and _sg.stage is not None:
        scratch_stripped = list(_sg.stripped)
        changed_files = _sg.stage
        steps.append("scratch_stripped")
        print(
            f"[scratch-guard] auto-PR for {task_id}: left out of the commit "
            "(untracked on disk, not deleted): " + ", ".join(scratch_stripped),
            file=sys.stderr,
        )

    # 2. Security validation gate
    validation = run_security_validation_gate(changed_files, config)
    steps.append("security_validation")
    if scratch_stripped:
        validation.report_markdown = (validation.report_markdown or "") + (
            "\n\n**Scratch guard:** left out of this PR (scratch-shaped, "
            "untracked on disk): " + ", ".join(f"`{p}`" for p in scratch_stripped)
        )

    # 3. Handle hard failures
    if not validation.passed:
        if validation.secrets_clean is False:
            return AutoPRResult(
                success=False,
                task_id=task_id,
                validation=validation,
                error="Secrets detected in changed files",
                steps_completed=steps,
            )
        if validation.tests_passed is False:
            if config["onTestFailure"] == "create_fix_task":
                _ensure_imports()
                handle_test_failure(
                    task_id=task_id,
                    task_title=task_title,
                    error_output=validation.errors[0]
                    if validation.errors
                    else "Tests failed",
                )
            return AutoPRResult(
                success=False,
                task_id=task_id,
                validation=validation,
                error="Tests failed",
                steps_completed=steps,
            )

    # 4. Create feature branch
    branch_result = create_daemon_feature_branch(
        task_id, task_title, config["branchPrefix"]
    )
    if not branch_result["success"]:
        return_to_main_branch()
        return AutoPRResult(
            success=False,
            task_id=task_id,
            error=f"Branch creation failed: {branch_result.get('error')}",
            steps_completed=steps,
        )
    branch_name = branch_result["branch_name"]
    steps.append("create_branch")

    # 5. Stage and commit deliverable files only
    commit_msg = f"feat(daemon): {task_title} [{task_id}]"
    commit_result = commit_changes(commit_msg, files=changed_files)
    if not commit_result["success"]:
        return_to_main_branch()
        # WS-057-005: Clean up orphaned branch — commit failed so nothing pushed
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return AutoPRResult(
            success=False,
            task_id=task_id,
            branch_name=branch_name,
            error=f"Commit failed: {commit_result.get('error')}",
            steps_completed=steps,
        )
    steps.append("commit")

    # 6. Push feature branch
    push_result = push_branch(branch_name)
    if not push_result["success"]:
        return_to_main_branch()
        # WS-057-005: Clean up orphaned branch — push failed so no upstream exists
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return AutoPRResult(
            success=False,
            task_id=task_id,
            branch_name=branch_name,
            error=f"Push failed: {push_result.get('error')}",
            steps_completed=steps,
        )
    steps.append("push")

    # 7. Create PR (draft when createDraftPR is set, otherwise ready-for-review).
    # PR creation is unconditional: branch+commit+push without a PR produces
    # the WS-119 1.8 phantom-completion failure, since the work is orphaned
    # once the worktree is cleaned up.
    pr_url = None
    pr_result = create_daemon_draft_pr(
        task_id,
        task_title,
        task_description,
        branch_name,
        validation,
        employee_id,
        is_draft=config["createDraftPR"],
    )
    if pr_result["success"]:
        pr_url = pr_result.get("pr_url")
        steps.append("create_pr")
    else:
        steps.append("create_pr_FAILED")

    # 7b. Pre-merge deliverable gate (Phase 2). Runs the adversarial judge on the
    # PR diff; on a block verdict it applies the needs-manual-review label + a
    # reason comment so the auto-merge levers (ci.yml + execute_auto_merge) leave
    # the PR open for a human. Best-effort: a gate failure must not lose the work
    # (the PR already exists), so we never flip success to False here.
    deliverable_blocked = False
    deliverable_reason = None
    if pr_url:
        try:
            gate = run_deliverable_gate(
                task_id,
                task_title,
                task_description,
                pr_url,
                branch_name,
                daemon_executed=True,
            )
            if gate.get("ran"):
                steps.append(
                    "deliverable_gate_blocked"
                    if gate.get("blocked")
                    else "deliverable_gate_passed"
                )
                deliverable_blocked = bool(gate.get("blocked"))
                deliverable_reason = gate.get("error") or gate.get("reason")
        except Exception as exc:  # noqa: BLE001 — gate must never break PR creation
            steps.append("deliverable_gate_ERROR")
            deliverable_reason = f"deliverable gate raised: {exc}"
            write_gate_skip(task_id, pr_url, f"gate raised exception: {str(exc)[:200]}")
    elif pr_result["success"]:
        # PR creation succeeded but returned no URL — gate cannot run.
        write_gate_skip(task_id, None, "pr_url absent after successful pr creation")
    else:
        # PR creation failed entirely — no PR exists to gate.
        write_gate_skip(task_id, None, "pr creation failed — gate cannot run")

    # 8. Return to main branch
    return_result = return_to_main_branch()
    if not return_result["success"]:
        # Log but don't fail — the PR was created
        steps.append("return_to_main_FAILED")
    else:
        steps.append("return_to_main")
    # WS-057-005: Clean up local branch ref — remote has it after successful push
    if branch_name:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True,
            text=True,
            timeout=5,
        )

    return AutoPRResult(
        success=True,
        task_id=task_id,
        branch_name=branch_name,
        pr_url=pr_url,
        validation=validation,
        steps_completed=steps,
        deliverable_blocked=deliverable_blocked,
        deliverable_reason=deliverable_reason,
        scratch_stripped=scratch_stripped or None,
    )


# =============================================================================
# Auto-Merge Policy (P19 External Service Integration)
# =============================================================================


@dataclass
class AutoMergePolicy:
    """
    Policy configuration for auto-merge functionality.

    CRITICAL: require_human_approval defaults to True and MUST be checked FIRST.
    This ensures no automated merge bypasses human oversight unless explicitly
    configured to allow it.
    """

    require_human_approval: bool = True  # CRITICAL: default True, checked FIRST
    require_all_checks_pass: bool = True
    require_review_count: int = 1
    allowed_authors: list[str] | None = None  # None = allow all, empty list = block all
    blocked_paths: list[str] | None = None  # Paths that block auto-merge if touched
    max_files_changed: int = 10
    max_lines_changed: int = 500


def _default_allowed_authors() -> list[str]:
    """Default allowed authors for auto-merge."""
    return ["daemon"]


def _default_blocked_paths() -> list[str]:
    """Default paths that block auto-merge if touched."""
    return [".claude/", ".company/"]


def load_auto_merge_policy() -> AutoMergePolicy:
    """
    Load auto-merge policy from gatesmith.json.

    Loads from: autonomy.flowMode.autoMergePolicy

    Returns:
        AutoMergePolicy with configured or default values.
    """
    config_paths = [
        Path.cwd() / "gatesmith.json",
        Path.cwd() / ".claude" / "gatesmith.json",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)

                policy_config = (
                    config.get("autonomy", {})
                    .get("flowMode", {})
                    .get("autoMergePolicy", {})
                )

                if not policy_config:
                    # No policy configured, use defaults
                    return AutoMergePolicy(
                        allowed_authors=_default_allowed_authors(),
                        blocked_paths=_default_blocked_paths(),
                    )

                return AutoMergePolicy(
                    require_human_approval=policy_config.get(
                        "requireHumanApproval", True
                    ),
                    require_all_checks_pass=policy_config.get(
                        "requireAllChecksPass", True
                    ),
                    require_review_count=policy_config.get("requireReviewCount", 1),
                    allowed_authors=policy_config.get(
                        "allowedAuthors", _default_allowed_authors()
                    ),
                    blocked_paths=policy_config.get(
                        "blockedPaths", _default_blocked_paths()
                    ),
                    max_files_changed=policy_config.get("maxFilesChanged", 10),
                    max_lines_changed=policy_config.get("maxLinesChanged", 500),
                )
            except (json.JSONDecodeError, OSError):
                pass

    # No config found, use defaults
    return AutoMergePolicy(
        allowed_authors=_default_allowed_authors(),
        blocked_paths=_default_blocked_paths(),
    )


def _is_human_reviewer(reviewer: dict) -> bool:
    """
    Determine if a reviewer is a human (not a bot).

    Args:
        reviewer: Reviewer dict with 'login' and optionally 'type' fields.

    Returns:
        True if reviewer appears to be a human, False if bot.
    """
    # Check explicit type field if available
    reviewer_type = reviewer.get("type", "").lower()
    if reviewer_type == "bot":
        return False

    # Check for common bot indicators in login
    login = reviewer.get("login", "").lower()
    bot_indicators = [
        "[bot]",
        "-bot",
        "_bot",
        "dependabot",
        "renovate",
        "github-actions",
        "codecov",
        "mergify",
        "semantic-release",
    ]

    for indicator in bot_indicators:
        if indicator in login:
            return False

    return True


def _check_human_approval(pr_info: dict) -> tuple[bool, str]:
    """
    Check if a human has approved the PR.

    CRITICAL: This is the most important check and cannot be bypassed.

    Args:
        pr_info: PR information dict with 'reviews' field containing list of reviews.

    Returns:
        Tuple of (has_human_approval, reason_message).
    """
    reviews = pr_info.get("reviews", [])

    if not reviews:
        return False, "No reviews found — human approval required"

    # Look for human approvals
    human_approvals = []
    for review in reviews:
        state = review.get("state", "").upper()
        if state != "APPROVED":
            continue

        author = review.get("user", review.get("author", {}))
        if _is_human_reviewer(author):
            human_approvals.append(author.get("login", "unknown"))

    if human_approvals:
        return True, f"Human approval(s) from: {', '.join(human_approvals)}"

    return False, "No human approvals found — only bot approvals detected"


def evaluate_auto_merge_policy(
    pr_info: dict, policy: AutoMergePolicy
) -> tuple[bool, list[str]]:
    """
    Evaluate whether a PR meets auto-merge policy requirements.

    CRITICAL SAFETY: Human approval check is FIRST and cannot be bypassed.
    When require_human_approval=True, this function MUST verify a human
    (not a bot) has approved before allowing merge.

    Args:
        pr_info: PR information dictionary containing:
            - author: dict with 'login' field
            - reviews: list of review dicts with 'state' and 'user'/'author' fields
            - checks: dict with 'conclusion' field (or list of check runs)
            - files_changed: int or list of file paths
            - lines_changed: int (additions + deletions)
            - changed_files: list of file paths (alternative to files_changed)
        policy: AutoMergePolicy configuration

    Returns:
        Tuple of (allowed, reasons) where:
            - allowed: True if all policy gates pass
            - reasons: List of human-readable reasons (pass or block messages)

    Examples:
        >>> policy = AutoMergePolicy(require_human_approval=True)
        >>> pr_info = {"reviews": [], "author": {"login": "daemon"}}
        >>> allowed, reasons = evaluate_auto_merge_policy(pr_info, policy)
        >>> allowed
        False
        >>> "BLOCKED: No reviews found" in reasons[0]
        True
    """
    reasons: list[str] = []

    # ==========================================================================
    # CRITICAL SAFETY CHECK — MUST BE FIRST AND CANNOT BE BYPASSED
    # ==========================================================================
    if policy.require_human_approval:
        has_human_approval, approval_msg = _check_human_approval(pr_info)
        if not has_human_approval:
            reasons.append(f"BLOCKED: {approval_msg}")
            # Return immediately — this is a hard gate
            return False, reasons
        reasons.append(f"PASSED: {approval_msg}")

    # ==========================================================================
    # Review count check
    # ==========================================================================
    if policy.require_review_count > 0:
        reviews = pr_info.get("reviews", [])
        # Count approved reviews (both human and bot for this check)
        approved_count = sum(
            1 for r in reviews if r.get("state", "").upper() == "APPROVED"
        )

        if approved_count < policy.require_review_count:
            reasons.append(
                f"BLOCKED: Insufficient reviews — need {policy.require_review_count}, "
                f"have {approved_count}"
            )
            return False, reasons
        reasons.append(
            f"PASSED: Review count met ({approved_count}/{policy.require_review_count})"
        )

    # ==========================================================================
    # CI checks status
    # ==========================================================================
    if policy.require_all_checks_pass:
        checks = pr_info.get("checks", {})

        # Handle both dict format {"conclusion": "success"} and list format
        if isinstance(checks, dict):
            conclusion = checks.get("conclusion", "").lower()
            checks_passed = conclusion in ("success", "skipped", "neutral")
        elif isinstance(checks, list):
            # All checks must pass
            checks_passed = all(
                c.get("conclusion", "").lower() in ("success", "skipped", "neutral")
                for c in checks
            )
        else:
            # No checks info available — fail safe
            checks_passed = False

        if not checks_passed:
            reasons.append("BLOCKED: CI checks have not all passed")
            return False, reasons
        reasons.append("PASSED: All CI checks passed")

    # ==========================================================================
    # Author allowlist check
    # ==========================================================================
    if policy.allowed_authors is not None and len(policy.allowed_authors) > 0:
        author = pr_info.get("author", {})
        author_login = author.get("login", "")

        if author_login not in policy.allowed_authors:
            reasons.append(
                f"BLOCKED: Author '{author_login}' not in allowed list: "
                f"{policy.allowed_authors}"
            )
            return False, reasons
        reasons.append(f"PASSED: Author '{author_login}' is in allowed list")

    # ==========================================================================
    # Blocked paths check
    # ==========================================================================
    if policy.blocked_paths:
        # Get list of changed file paths
        changed_files = pr_info.get("changed_files", [])
        if not changed_files and isinstance(pr_info.get("files_changed"), list):
            changed_files = pr_info["files_changed"]

        blocked_files = []
        for file_path in changed_files:
            for blocked_path in policy.blocked_paths:
                # Match if file starts with blocked path (directory) or equals it
                if file_path.startswith(blocked_path) or file_path == blocked_path:
                    blocked_files.append(file_path)
                    break

        if blocked_files:
            reasons.append(
                f"BLOCKED: Changes to protected paths: {blocked_files[:5]}"
                + (
                    f" (+{len(blocked_files) - 5} more)"
                    if len(blocked_files) > 5
                    else ""
                )
            )
            return False, reasons
        reasons.append("PASSED: No changes to protected paths")

    # ==========================================================================
    # Max files changed check
    # ==========================================================================
    files_changed_count = pr_info.get("files_changed")
    if isinstance(files_changed_count, list):
        files_changed_count = len(files_changed_count)
    elif files_changed_count is None:
        files_changed_count = len(pr_info.get("changed_files", []))

    if files_changed_count > policy.max_files_changed:
        reasons.append(
            f"BLOCKED: Too many files changed — "
            f"{files_changed_count} > {policy.max_files_changed}"
        )
        return False, reasons
    reasons.append(
        f"PASSED: Files changed within limit "
        f"({files_changed_count}/{policy.max_files_changed})"
    )

    # ==========================================================================
    # Max lines changed check
    # ==========================================================================
    lines_changed = pr_info.get("lines_changed", 0)
    # Handle case where additions/deletions are separate
    if lines_changed == 0:
        lines_changed = pr_info.get("additions", 0) + pr_info.get("deletions", 0)

    if lines_changed > policy.max_lines_changed:
        reasons.append(
            f"BLOCKED: Too many lines changed — "
            f"{lines_changed} > {policy.max_lines_changed}"
        )
        return False, reasons
    reasons.append(
        f"PASSED: Lines changed within limit "
        f"({lines_changed}/{policy.max_lines_changed})"
    )

    # ==========================================================================
    # All checks passed
    # ==========================================================================
    return True, reasons


# =============================================================================
# Auto-Merge Execution (Task 19.8 - P19 External Service Integration)
# =============================================================================


def _find_log_dir() -> Path:
    """Find the logs directory, creating if needed."""
    # Try project root first
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            log_dir = parent / "logs"
            log_dir.mkdir(exist_ok=True)
            return log_dir

    # Fallback to cwd/logs
    log_dir = cwd / "logs"
    log_dir.mkdir(exist_ok=True)
    return log_dir


def _log_merge_attempt(
    pr_url: str,
    action: str,
    success: bool,
    policy_result: tuple[bool, list[str]] | None = None,
    error: str | None = None,
    pr_info: dict | None = None,
) -> None:
    """
    Log a merge attempt to activity.jsonl.

    All merge attempts are logged regardless of success/failure.
    This provides a complete audit trail for compliance.

    Args:
        pr_url: GitHub PR URL
        action: Action taken (evaluate, merge, skip, blocked)
        success: Whether the action succeeded
        policy_result: Tuple of (allowed, reasons) from policy evaluation
        error: Error message if failed
        pr_info: PR information dict
    """
    log_dir = _find_log_dir()
    log_file = log_dir / "activity.jsonl"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "auto_merge_attempt",
        "pr_url": pr_url,
        "action": action,
        "success": success,
    }

    if policy_result:
        entry["policy_allowed"] = policy_result[0]
        entry["policy_reasons"] = policy_result[1]

    if error:
        entry["error"] = error

    if pr_info:
        entry["pr_number"] = pr_info.get("number")
        entry["pr_title"] = pr_info.get("title")
        entry["base_branch"] = pr_info.get("baseRefName")
        entry["head_branch"] = pr_info.get("headRefName")
        entry["author"] = pr_info.get("author", {}).get("login")
        entry["additions"] = pr_info.get("additions")
        entry["deletions"] = pr_info.get("deletions")

    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Logging failure should not block merge
        pass


def _fetch_pr_info_for_merge(pr_url: str) -> dict | None:
    """
    Fetch comprehensive PR information for merge evaluation.

    Args:
        pr_url: GitHub PR URL

    Returns:
        Dict with PR info including reviews and checks, or None on failure.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "number,title,state,mergeable,baseRefName,headRefName,"
                "reviews,labels,statusCheckRollup,isDraft,autoMergeRequest,"
                "author,additions,deletions,changedFiles,files",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            pr_info = json.loads(result.stdout)

            # Transform to format expected by evaluate_auto_merge_policy
            status_checks = pr_info.get("statusCheckRollup", [])

            # Calculate lines changed
            additions = pr_info.get("additions", 0)
            deletions = pr_info.get("deletions", 0)
            pr_info["lines_changed"] = additions + deletions

            # Get file paths from files array
            files = pr_info.get("files", [])
            pr_info["changed_files"] = [f.get("path", "") for f in files]
            pr_info["files_changed"] = len(files)

            # Transform checks to format expected by evaluate_auto_merge_policy
            if status_checks:
                pr_info["checks"] = status_checks
            else:
                # No checks means we should fail safe
                pr_info["checks"] = {"conclusion": "unknown"}

            return pr_info
        return None

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _load_flow_mode_config() -> dict:
    """Load flowMode config to check if autoMergeAfterApproval is enabled."""
    config_paths = [
        Path.cwd() / "gatesmith.json",
        Path.cwd() / ".claude" / "gatesmith.json",
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                flow_mode = config.get("autonomy", {}).get("flowMode", {})
                return {
                    "autoMergeAfterApproval": flow_mode.get(
                        "autoMergeAfterApproval", False
                    ),
                    "mergeMethod": flow_mode.get("mergeMethod", "squash"),
                    "deleteBranchAfterMerge": flow_mode.get(
                        "deleteBranchAfterMerge", True
                    ),
                    "allowMergeToMain": flow_mode.get("allowMergeToMain", False),
                }
            except (json.JSONDecodeError, OSError):
                pass

    return {
        "autoMergeAfterApproval": False,
        "mergeMethod": "squash",
        "deleteBranchAfterMerge": True,
        "allowMergeToMain": False,
    }


def execute_auto_merge(pr_url: str) -> dict:
    """
    Execute auto-merge for a PR with strict policy enforcement.

    SAFETY GATES (HARD requirements - cannot be bypassed):
    1. NEVER merge without human approval if policy requires it
    2. NEVER merge if any required check failed
    3. NEVER merge to main/master without explicit config (allowMergeToMain: true)

    Flow:
    1. Load policy from gatesmith.json
    2. Fetch PR info via gh CLI
    3. Call evaluate_auto_merge_policy()
    4. If policy passes AND autoMergeAfterApproval enabled: merge
    5. Log ALL attempts to activity.jsonl

    Args:
        pr_url: GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)

    Returns:
        Dict with:
            - success: bool - Whether merge was executed successfully
            - action: str - What action was taken (merged, blocked, skipped)
            - policy_passed: bool - Whether policy evaluation passed
            - reasons: list[str] - Policy evaluation reasons
            - error: str | None - Error message if failed
            - pr_info: dict | None - Basic PR info (number, title, branches)
    """
    # Load configurations
    flow_config = _load_flow_mode_config()
    policy = load_auto_merge_policy()

    # Fetch PR info
    pr_info = _fetch_pr_info_for_merge(pr_url)
    if pr_info is None:
        error = "Failed to fetch PR information from GitHub"
        _log_merge_attempt(pr_url, "fetch_failed", False, error=error)
        return {
            "success": False,
            "action": "fetch_failed",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": None,
        }

    # Check if PR is in a mergeable state
    state = pr_info.get("state", "")
    if state != "OPEN":
        error = f"PR is not open (state: {state})"
        _log_merge_attempt(pr_url, "skipped", False, error=error, pr_info=pr_info)
        return {
            "success": False,
            "action": "skipped",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": pr_info,
        }

    if pr_info.get("isDraft", False):
        error = "PR is a draft"
        _log_merge_attempt(pr_url, "skipped", False, error=error, pr_info=pr_info)
        return {
            "success": False,
            "action": "skipped",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": pr_info,
        }

    # SAFETY GATE (hold labels): honour needs-manual-review (applied by the
    # pre-merge deliverable judge, fail-closed on unverifiable diffs) AND
    # awaiting-operator-review (the reviewPaths hold / operator's manual
    # brake). This Python auto-merge lever must refuse to merge past either,
    # in parity with the ci.yml auto-merge gate. This is ADDITIVE to — never
    # a bypass of — the allowMergeToMain gate below.
    held = {_gate_label(), _review_hold_label(), *_challenger_hold_labels()} & set(
        _pr_label_names(pr_info)
    )
    if not held:
        _ch_block = _challenger_merge_block(pr_url)
        if _ch_block:
            held = {_ch_block}
    if held:
        # Best-effort disarm: a refusal here is theater if GitHub's native
        # auto-merge is still armed on the PR (it ignores labels once armed).
        if pr_info.get("autoMergeRequest"):
            try:
                subprocess.run(
                    ["gh", "pr", "merge", pr_url, "--disable-auto"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
        error = (
            f"BLOCKED: hold label(s) {', '.join(sorted(held))} present — "
            "PR needs human review before merge"
        )
        _log_merge_attempt(pr_url, "blocked", False, error=error, pr_info=pr_info)
        return {
            "success": False,
            "action": "blocked",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": pr_info,
        }

    # SAFETY GATE 3: Check if merging to main/master without explicit config
    base_branch = pr_info.get("baseRefName", "")
    if base_branch in ("main", "master") and not flow_config.get(
        "allowMergeToMain", False
    ):
        error = (
            f"BLOCKED: Auto-merge to '{base_branch}' requires explicit config "
            "(autonomy.flowMode.allowMergeToMain: true)"
        )
        _log_merge_attempt(pr_url, "blocked", False, error=error, pr_info=pr_info)
        return {
            "success": False,
            "action": "blocked",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": pr_info,
        }

    # Evaluate policy
    policy_passed, reasons = evaluate_auto_merge_policy(pr_info, policy)
    _log_merge_attempt(
        pr_url,
        "evaluate" if not policy_passed else "policy_passed",
        policy_passed,
        policy_result=(policy_passed, reasons),
        pr_info=pr_info,
    )

    if not policy_passed:
        return {
            "success": False,
            "action": "blocked",
            "policy_passed": False,
            "reasons": reasons,
            "error": "Policy evaluation failed",
            "pr_info": pr_info,
        }

    # Check if auto-merge is enabled
    if not flow_config.get("autoMergeAfterApproval", False):
        _log_merge_attempt(
            pr_url,
            "skipped_disabled",
            True,
            policy_result=(policy_passed, reasons),
            pr_info=pr_info,
        )
        return {
            "success": True,
            "action": "skipped",
            "policy_passed": True,
            "reasons": reasons
            + ["Auto-merge disabled (autoMergeAfterApproval: false)"],
            "error": None,
            "pr_info": pr_info,
        }

    # The one merge decision every lever shares (single-merge-gate): author,
    # every hold label, challenger, verdict for the head, conflicts, CI on the
    # head, blocked/review paths, secrets, diff size, autoMerge policy. The
    # merge is pinned to the head it judged.
    gate_refusal, gate_head = _shared_merge_gate(pr_url)
    if gate_refusal is not None or not gate_head:
        error = f"BLOCKED by merge gate: {gate_refusal or 'no head to pin'}"
        _log_merge_attempt(pr_url, "blocked", False, error=error, pr_info=pr_info)
        return {
            "success": False,
            "action": "blocked",
            "policy_passed": False,
            "reasons": [error],
            "error": error,
            "pr_info": pr_info,
        }

    # Execute merge
    merge_method = flow_config.get("mergeMethod", "squash")
    delete_branch = flow_config.get("deleteBranchAfterMerge", True)

    try:
        merge_cmd = ["gh", "pr", "merge", pr_url, "--match-head-commit", gate_head]

        # Add merge method flag
        if merge_method == "squash":
            merge_cmd.append("--squash")
        elif merge_method == "rebase":
            merge_cmd.append("--rebase")
        else:  # merge
            merge_cmd.append("--merge")

        # Add delete branch flag
        if delete_branch:
            merge_cmd.append("--delete-branch")

        result = subprocess.run(
            merge_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            _log_merge_attempt(
                pr_url,
                "merged",
                True,
                policy_result=(policy_passed, reasons),
                pr_info=pr_info,
            )
            return {
                "success": True,
                "action": "merged",
                "policy_passed": True,
                "reasons": reasons,
                "error": None,
                "pr_info": pr_info,
                "merge_method": merge_method,
                "branch_deleted": delete_branch,
            }
        else:
            error = result.stderr.strip() or "Merge command failed"
            _log_merge_attempt(
                pr_url,
                "merge_failed",
                False,
                policy_result=(policy_passed, reasons),
                error=error,
                pr_info=pr_info,
            )
            return {
                "success": False,
                "action": "merge_failed",
                "policy_passed": True,
                "reasons": reasons,
                "error": error,
                "pr_info": pr_info,
            }

    except subprocess.TimeoutExpired:
        error = "Merge command timed out"
        _log_merge_attempt(
            pr_url,
            "merge_timeout",
            False,
            policy_result=(policy_passed, reasons),
            error=error,
            pr_info=pr_info,
        )
        return {
            "success": False,
            "action": "merge_timeout",
            "policy_passed": True,
            "reasons": reasons,
            "error": error,
            "pr_info": pr_info,
        }
    except OSError as e:
        error = str(e)
        _log_merge_attempt(
            pr_url,
            "merge_error",
            False,
            policy_result=(policy_passed, reasons),
            error=error,
            pr_info=pr_info,
        )
        return {
            "success": False,
            "action": "merge_error",
            "policy_passed": True,
            "reasons": reasons,
            "error": error,
            "pr_info": pr_info,
        }


def check_mergeable_prs(
    label: str = "ready-for-auto-merge",
    limit: int = 10,
) -> list[dict]:
    """
    List PRs with the specified label that pass policy evaluation.

    This function is designed for use in scheduled/daemon contexts to find
    PRs that are ready for auto-merge.

    Args:
        label: Label to filter by (default: "ready-for-auto-merge")
        limit: Maximum number of PRs to check (default: 10)

    Returns:
        List of dicts, each containing:
            - pr_url: str - GitHub PR URL
            - pr_number: int - PR number
            - title: str - PR title
            - policy_passed: bool - Whether policy evaluation passed
            - reasons: list[str] - Policy evaluation reasons
            - can_merge: bool - Whether PR can be auto-merged (policy passed + config enabled)
    """
    mergeable_prs = []
    flow_config = _load_flow_mode_config()
    policy = load_auto_merge_policy()

    try:
        # List PRs with the label
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,url,title,baseRefName,headRefName",
                "--limit",
                str(limit),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return []

        prs = json.loads(result.stdout)

        for pr in prs:
            pr_url = pr.get("url", "")
            pr_number = pr.get("number")
            title = pr.get("title", "")
            base_branch = pr.get("baseRefName", "")

            # Fetch full PR info for policy evaluation
            pr_info = _fetch_pr_info_for_merge(pr_url)
            if pr_info is None:
                mergeable_prs.append(
                    {
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "title": title,
                        "policy_passed": False,
                        "reasons": ["Failed to fetch PR info"],
                        "can_merge": False,
                    }
                )
                continue

            # Check main/master merge restriction
            if base_branch in ("main", "master") and not flow_config.get(
                "allowMergeToMain", False
            ):
                mergeable_prs.append(
                    {
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "title": title,
                        "policy_passed": False,
                        "reasons": [
                            f"Auto-merge to '{base_branch}' requires allowMergeToMain: true"
                        ],
                        "can_merge": False,
                    }
                )
                continue

            # Evaluate policy
            policy_passed, reasons = evaluate_auto_merge_policy(pr_info, policy)

            # Check if auto-merge is enabled
            auto_merge_enabled = flow_config.get("autoMergeAfterApproval", False)
            can_merge = policy_passed and auto_merge_enabled

            mergeable_prs.append(
                {
                    "pr_url": pr_url,
                    "pr_number": pr_number,
                    "title": title,
                    "policy_passed": policy_passed,
                    "reasons": reasons,
                    "can_merge": can_merge,
                    "base_branch": base_branch,
                    "head_branch": pr.get("headRefName", ""),
                }
            )

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    return mergeable_prs


# =============================================================================
# CLI Interface
# =============================================================================


def print_help():
    """Print usage help."""
    help_text = """
PR Output Manager — branch/PR creation for plan-driven tasks

Commands:
    create-branch       Create feature branch for a task
    push-branch         Push branch to remote
    create-pr           Create draft PR for completed task
    run-tests           Run project tests
    handle-test-failure Handle test failure (create fix task)
    workflow            Execute complete PR workflow
    auto-merge          Execute auto-merge for a PR with policy enforcement
    check-mergeable     List PRs ready for auto-merge

Options:
    --task-id ID        Task ID (required for most commands)
    --title TEXT        Task title
    --description TEXT  Task description
    --branch NAME       Branch name (for push/PR commands)
    --error TEXT        Error output (for test failure handling)
    --pr-url URL        PR URL (for auto-merge command)
    --label LABEL       Label filter (for check-mergeable, default: ready-for-auto-merge)
    --limit N           Max PRs to check (for check-mergeable, default: 10)

Examples:
    # Create branch for task
    python pr_output_manager.py create-branch --task-id "P14-1.1" --title "Create scheduler"

    # Run complete workflow
    python pr_output_manager.py workflow --task-id "P14-1.1" --title "Create scheduler" --description "..."

    # Auto-merge a PR (with policy enforcement)
    python pr_output_manager.py auto-merge --pr-url "https://github.com/org/repo/pull/123"

    # List PRs ready for auto-merge
    python pr_output_manager.py check-mergeable --label "ready-for-auto-merge"
"""
    print(help_text)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    # Parse arguments
    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:].replace("-", "_")
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                args[key] = sys.argv[i + 1]
                i += 2
            else:
                args[key] = True
                i += 1
        else:
            i += 1

    if command in ("help", "--help", "-h"):
        print_help()
        sys.exit(0)

    if command == "create-branch":
        task_id = args.get("task_id")
        title = args.get("title", "task")

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        result = create_feature_branch(task_id, title)
        print(json.dumps(result, indent=2))

    elif command == "push-branch":
        branch = args.get("branch")

        if not branch:
            branch = get_current_branch()
            if not branch:
                print("Error: --branch required or must be on a branch")
                sys.exit(1)

        result = push_branch(branch)
        print(json.dumps(result, indent=2))

    elif command == "create-pr":
        task_id = args.get("task_id", "unknown")
        title = args.get("title", "Task")
        description = args.get("description", "")
        branch = args.get("branch")

        if not branch:
            branch = get_current_branch()
            if not branch:
                print("Error: --branch required or must be on a branch")
                sys.exit(1)

        result = create_draft_pr(task_id, title, description, branch)
        print(json.dumps(result, indent=2))

    elif command == "run-tests":
        result = run_tests()
        print(json.dumps(result, indent=2))

    elif command == "handle-test-failure":
        task_id = args.get("task_id", "unknown")
        title = args.get("title", "Task")
        error = args.get("error", "Test failed")

        result = handle_test_failure(task_id, title, error)
        print(json.dumps(result, indent=2))

    elif command == "workflow":
        task_id = args.get("task_id")
        title = args.get("title", "Task")
        description = args.get("description", "")

        if not task_id:
            print("Error: --task-id required")
            sys.exit(1)

        result = execute_pr_workflow(task_id, title, description)
        print(
            json.dumps(
                {
                    "success": result.success,
                    "task_id": result.task_id,
                    "branch_name": result.branch_name,
                    "pr_url": result.pr_url,
                    "tests_passed": result.tests_passed,
                    "fix_task_id": result.fix_task_id,
                    "error": result.error,
                    "steps_completed": result.steps_completed,
                },
                indent=2,
            )
        )

    elif command == "auto-merge":
        pr_url = args.get("pr_url")

        if not pr_url:
            print("Error: --pr-url required")
            sys.exit(1)

        result = execute_auto_merge(pr_url)
        print(json.dumps(result, indent=2, default=str))

    elif command == "check-mergeable":
        label = args.get("label", "ready-for-auto-merge")
        limit = int(args.get("limit", 10))

        result = check_mergeable_prs(label=label, limit=limit)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
