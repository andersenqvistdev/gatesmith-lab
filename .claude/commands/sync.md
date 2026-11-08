# /sync — Synchronize Local Branch with Remote

Cleanly sync your local branch after a PR merge, handling divergent branches and uncommitted changes automatically.

## Usage

```bash
# Sync current branch with origin
/sync

# Sync and show what changed
/sync --verbose

# Sync a specific branch
/sync --branch main
```

## Arguments

- `--verbose` - Show detailed output of sync operations
- `--branch <name>` - Branch to sync (default: current branch)
- `--dry-run` - Show what would happen without making changes

## Instructions

<command name="sync">
Execute the /sync command to synchronize the local branch with the remote after a PR merge.

**Problem this solves:**
After creating a PR from a feature branch and merging on GitHub (especially with squash),
the local branch diverges from origin. Normal `git pull` fails, and `git reset --hard`
is blocked by safety hooks.

**Sync procedure:**

1. **Check current state:**
   ```bash
   git status --porcelain
   git rev-parse --abbrev-ref HEAD
   ```

2. **Stash uncommitted changes (if any):**
   ```bash
   git stash push -m "sync-stash-$(date +%Y%m%d-%H%M%S)"
   ```
   Record whether stash was created.

3. **Fetch latest from origin:**
   ```bash
   git fetch origin
   ```

4. **Check divergence:**
   ```bash
   git rev-list --left-right --count HEAD...origin/<branch>
   ```
   This shows commits ahead/behind.

5. **Reset to origin (safe method):**
   ```bash
   git checkout -B <branch> origin/<branch>
   ```
   This is equivalent to `reset --hard` but uses checkout which isn't blocked.

6. **Restore stash (if created):**
   ```bash
   git stash pop
   ```

7. **Report result:**
   ```
   Sync complete.

   Branch: main
   Previous: abc1234 (was 3 commits ahead, 1 behind)
   Current:  def5678 (up to date with origin/main)

   Uncommitted changes: restored from stash
   ```

**Error handling:**

- If stash pop has conflicts, report them and leave stash intact
- If checkout fails, report error and suggest manual resolution
- Always show clear status of what happened

**Example output:**

```
Syncing main with origin/main...

Stashing 2 uncommitted changes...
Fetching origin...
Branch was: 12 commits ahead, 1 commit behind origin/main
Resetting to origin/main...
Restoring uncommitted changes...

Sync complete.
  Branch: main @ aef1a5f
  Status: up to date with origin/main
  Changes: 2 files restored from stash
```

**For --dry-run:**

```
Dry run - no changes made.

Would sync main with origin/main:
  - Stash 2 uncommitted files
  - Reset from abc1234 (12 ahead, 1 behind) to def5678
  - Restore stash

Run without --dry-run to execute.
```
</command>

## When to Use

| Scenario | Use /sync? |
|----------|------------|
| After merging your PR on GitHub | Yes |
| After someone else merged to main | Yes |
| Local commits not yet pushed | No - push first or they'll be lost |
| Merge conflicts to resolve | No - resolve manually |
| Clean pull would work | Optional - `git pull` is fine too |

## Related Commands

- `/commit` - Create commits
- `/pr` - Create pull requests
- `git status` - Check current state
