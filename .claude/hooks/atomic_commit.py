# /// script
# requires-python = ">=3.10"
# ///
"""
Atomic Commit Strategy — from GSD.

1 task = 1 commit. Each commit is revertable via git bisect.
Format: feat(phase-N): task-name

Usage: python atomic_commit.py <phase> <task_id> <task_name> [--dry-run]
"""

import subprocess
import sys


def get_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split("\n") if f]
    return []


def get_unstaged_changes() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only"], capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split("\n") if f]
    return []


def get_untracked() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split("\n") if f]
    return []


def atomic_commit(
    phase: str, task_id: str, task_name: str, dry_run: bool = False
) -> dict:
    """Stage all changes and create an atomic commit for this task."""

    # Determine commit type from task name
    name_lower = task_name.lower()
    if any(k in name_lower for k in ["fix", "bug", "patch"]):
        commit_type = "fix"
    elif any(k in name_lower for k in ["test", "spec"]):
        commit_type = "test"
    elif any(k in name_lower for k in ["doc", "readme"]):
        commit_type = "docs"
    elif any(k in name_lower for k in ["refactor", "clean"]):
        commit_type = "refactor"
    else:
        commit_type = "feat"

    commit_msg = f"{commit_type}(phase-{phase}): {task_name} [{task_id}]"

    unstaged = get_unstaged_changes()
    untracked = get_untracked()
    all_files = unstaged + untracked

    if not all_files and not get_staged_files():
        return {"committed": False, "reason": "No changes to commit"}

    if dry_run:
        return {
            "committed": False,
            "dry_run": True,
            "message": commit_msg,
            "files": all_files,
        }

    # Stage all changes
    if all_files:
        subprocess.run(["git", "add"] + all_files, timeout=10)

    # Commit
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
        timeout=30,
    )

    return {
        "committed": result.returncode == 0,
        "message": commit_msg,
        "files": all_files,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: atomic_commit.py <phase> <task_id> <task_name> [--dry-run]")
        sys.exit(1)

    phase = sys.argv[1]
    task_id = sys.argv[2]
    task_name = sys.argv[3]
    dry_run = "--dry-run" in sys.argv

    import json

    result = atomic_commit(phase, task_id, task_name, dry_run)
    print(json.dumps(result, indent=2))
