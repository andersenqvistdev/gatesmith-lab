# /// script
# requires-python = ">=3.10"
# ///
"""
Git Base Branch Detection Utility

Safely determines the base branch for git operations with proper fallbacks.
Handles cases where origin/HEAD is not set.

Usage:
    uv run .claude/hooks/git_base_branch.py              # Returns base branch ref
    uv run .claude/hooks/git_base_branch.py --setup      # Sets up origin/HEAD if missing
    uv run .claude/hooks/git_base_branch.py --diff-base  # Returns best ref for git diff

Exit codes:
    0 - Success
    1 - Error (no valid ref found)
"""

import subprocess
import sys


def run_git(args: list[str], check: bool = False) -> tuple[int, str]:
    """Run a git command and return (returncode, stdout)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip()


def get_origin_head() -> str | None:
    """Try to resolve origin/HEAD."""
    code, ref = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"])
    if code == 0 and ref:
        # Returns refs/remotes/origin/main -> extract origin/main
        return ref.replace("refs/remotes/", "")
    return None


def get_default_branch_from_remote() -> str | None:
    """Query remote for default branch (requires network)."""
    code, output = run_git(["remote", "show", "origin"])
    if code == 0:
        for line in output.splitlines():
            if "HEAD branch:" in line:
                branch = line.split(":")[-1].strip()
                return f"origin/{branch}"
    return None


def check_ref_exists(ref: str) -> bool:
    """Check if a git ref exists."""
    code, _ = run_git(["rev-parse", "--verify", ref])
    return code == 0


def setup_origin_head() -> bool:
    """Set up origin/HEAD automatically."""
    code, _ = run_git(["remote", "set-head", "origin", "--auto"])
    return code == 0


def get_base_branch() -> str | None:
    """
    Determine the base branch with fallbacks:
    1. origin/HEAD (if set)
    2. origin/main (if exists)
    3. origin/master (if exists)
    4. None (caller should handle)
    """
    # Try origin/HEAD first
    origin_head = get_origin_head()
    if origin_head and check_ref_exists(origin_head):
        return origin_head

    # Fallback to common default branches
    for branch in ["origin/main", "origin/master", "origin/develop"]:
        if check_ref_exists(branch):
            return branch

    return None


def get_diff_base() -> str:
    """
    Get the best ref for git diff operations.
    Falls back to HEAD~10 if no remote branch is found.
    """
    base = get_base_branch()
    if base:
        return base

    # Last resort: compare against recent history
    # This is useful for repos without remotes or with unusual setups
    code, _ = run_git(["rev-parse", "HEAD~10"])
    if code == 0:
        return "HEAD~10"

    return "HEAD"


def main():
    args = sys.argv[1:]

    if "--setup" in args:
        # Try to set up origin/HEAD
        if setup_origin_head():
            base = get_origin_head()
            print(f"origin/HEAD set to: {base}")
            sys.exit(0)
        else:
            # Try to determine and set manually
            base = get_base_branch()
            if base:
                branch = base.replace("origin/", "")
                run_git(["remote", "set-head", "origin", branch])
                print(f"origin/HEAD set to: {base}")
                sys.exit(0)
            print("Could not determine default branch", file=sys.stderr)
            sys.exit(1)

    elif "--diff-base" in args:
        # Return the best ref for diff operations
        print(get_diff_base())
        sys.exit(0)

    else:
        # Default: return base branch or error
        base = get_base_branch()
        if base:
            print(base)
            sys.exit(0)
        else:
            print(
                "No base branch found. Run with --setup to configure.", file=sys.stderr
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
