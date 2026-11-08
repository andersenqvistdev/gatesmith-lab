# /company-upgrade — Upgrade Company Structure to v1.2

Upgrade an existing single-project company (v1.1) to the multi-project structure (v1.2). This migration enables company-wide employee management and cross-project coordination.

## Input
$ARGUMENTS

**Options:**
- `--dry-run` — Preview what changes would be made without applying them
- `--rollback` — Restore from the most recent backup
- `--backup-dir=<path>` — Specify custom backup directory (default: `.company-backup-{timestamp}`)
- `--force` — Skip confirmation prompt

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the operation mode:

| Pattern | Mode | Description |
|---------|------|-------------|
| (empty) | UPGRADE | Standard migration with confirmation |
| `--dry-run` | DRY_RUN | Preview changes without applying |
| `--rollback` | ROLLBACK | Restore from backup |
| `--force` | UPGRADE_FORCE | Migration without confirmation |

Extract optional `--backup-dir=<path>` if specified.

## Step 1: Check Current Installation State

Before proceeding, detect the current state:

```bash
# Check for v1.2 marker (already migrated)
ls -la .forge-company-root 2>/dev/null

# Check for v1.1 company directory
ls -la .company/ 2>/dev/null

# Check for org.json
ls -la .company/org.json 2>/dev/null
```

**Determine state:**
- `.forge-company-root` exists -> Already migrated to v1.2
- `.company/` exists but no marker -> v1.1 installation (can migrate)
- Neither exists -> No company installation found

## Step 2: Handle Already Migrated

**If `.forge-company-root` exists:**

```
## Already on v1.2

Your company structure has already been migrated to v1.2.

| Status | Value |
|--------|-------|
| Company Root Marker | .forge-company-root [exists] |
| Company Directory | .company/ [exists] |
| Structure Version | v1.2 (multi-project capable) |

No upgrade needed.

### Available Commands
- `/company-status` — View company status
- `/company-add-project` — Register projects with the company
- `/company-projects` — List registered projects

If you're experiencing issues, you can:
1. Run `/company-status` to verify structure
2. Manually repair by editing `.company/org.json`
3. Contact support if problems persist
```

Exit without changes.

## Step 3: Handle No Installation

**If `.company/` does not exist:**

```
## No Company Found

No company installation was found in this directory.

| Check | Result |
|-------|--------|
| .company/ | not found |
| .forge-company-root | not found |

### Getting Started

To create a new single-project company:
  /company-init

To create a new multi-project company:
  /company-create

### What's the Difference?

| Feature | Single-Project (/company-init) | Multi-Project (/company-create) |
|---------|--------------------------------|--------------------------------|
| Structure | .company/ in project | .forge-company-root + .company/ |
| Employees | Per-project agents | Company-level employees |
| Work Queue | Project-scoped | Company-level with project tags |
| Best For | Single repo/project | Multiple projects/monorepo |

The `/company-upgrade` command is only for upgrading existing v1.1 installations.
```

Exit without changes.

## Step 4: Rollback Mode

**If `--rollback` flag was specified:**

### Step 4.1: Find Backup

Search for backup directories:

```bash
# List available backups
ls -d .company-backup-* 2>/dev/null | sort -r
```

**If no backups found:**

```
## No Backups Found

No backup directories found matching `.company-backup-*`.

Backups are created automatically during migration with names like:
  .company-backup-20240115_143022

If you have a backup in a different location, restore manually:
  1. Remove current migration markers:
     rm -f .forge-company-root
     rm -rf .company/assignments/

  2. Restore from your backup:
     cp -r /path/to/backup/.company .

  3. Verify restoration:
     /company-status
```

Exit without changes.

### Step 4.2: Show Backups and Confirm

**If backups exist:**

```
## Available Backups

| # | Backup Directory | Created |
|---|------------------|---------|
| 1 | .company-backup-20240115_143022 | 2024-01-15 14:30:22 |
| 2 | .company-backup-20240114_091500 | 2024-01-14 09:15:00 |

### Rollback Preview

Restoring will:
1. Remove `.forge-company-root` marker file
2. Remove `.company/assignments/` directory (v1.2 addition)
3. Restore `.company/` from the selected backup

This will revert your company to v1.1 (single-project mode).

### Confirm Rollback

Select backup number to restore (or 'cancel'):
```

**User selects backup number:**

### Step 4.3: Execute Rollback

```bash
# Store backup path (most recent by default, or user-selected)
BACKUP_DIR=".company-backup-20240115_143022"

# Step 1: Remove v1.2 marker
rm -f .forge-company-root

# Step 2: Remove v1.2 additions
rm -rf .company/assignments/
rm -rf .company/employees/  # If was renamed from agents

# Step 3: Restore from backup
rm -rf .company/
cp -r "$BACKUP_DIR/.company" .

# Step 4: Verify restoration
ls -la .company/
```

**Display result:**

```
## Rollback Complete

Company structure restored to v1.1 from backup.

| Action | Status |
|--------|--------|
| Remove .forge-company-root | done |
| Remove .company/assignments/ | done |
| Restore .company/ from backup | done |

### Restored State
| File | Status |
|------|--------|
| .company/org.json | restored |
| .company/config.json | restored |
| .company/agents/ | restored |

### Backup Preserved

The backup directory has been preserved at:
  [backup-dir]

To remove the backup after verification:
  rm -rf [backup-dir]

### Next Steps
- Run `/company-status` to verify the restored structure
- If satisfied, remove backup directory
- To upgrade again later: `/company-upgrade`
```

Exit.

## Step 5: Dry Run Mode

**If `--dry-run` flag was specified:**

Run the migration script in dry-run mode:

```bash
uv run .claude/tools/migrate_company_v1.2.py --dry-run
```

This will:
1. Detect v1.1 installation
2. Show migration plan
3. Preview all changes without applying them

**Display output from script, then add:**

```
## Dry Run Summary

The above shows what would change during migration.

### Changes Preview
| Change | Description |
|--------|-------------|
| Create `.forge-company-root` | Multi-project marker file |
| Rename `.company/agents/` to `.company/employees/` | New naming convention |
| Create `.company/assignments/` | Project assignment tracking |
| Update `.company/org.json` | Add version, mode, employees fields |

### No Changes Made

This was a dry run. Your files are unchanged.

To perform the actual migration:
  /company-upgrade

To migrate without confirmation:
  /company-upgrade --force
```

Exit without changes.

## Step 6: Standard Upgrade

**If no special flags (or `--force`):**

### Step 6.1: Show Current State

```
## Company Upgrade: v1.1 -> v1.2

### Current Installation

| File | Status | Description |
|------|--------|-------------|
| .company/ | found | Company directory |
| .company/org.json | found | Organization structure |
| .company/config.json | found | Runtime configuration |
| .company/agents/ | found | Agent definitions |
| .forge-company-root | not found | Multi-project marker |

### Detected Version: v1.1 (Single-Project)

This upgrade will enable multi-project features.
```

### Step 6.2: Show Migration Plan

```
### Migration Plan

The following changes will be made:

| # | Action | Description |
|---|--------|-------------|
| 1 | Create | `.forge-company-root` marker file |
| 2 | Rename | `.company/agents/` -> `.company/employees/` |
| 3 | Create | `.company/assignments/` directory |
| 4 | Create | `.company/assignments/_index.json` |
| 5 | Update | `.company/org.json` (add version, mode, employees) |

### Backup

A full backup will be created before migration:
  Location: [backup-dir or .company-backup-{timestamp}]

### Rollback

If issues occur, restore with:
  /company-upgrade --rollback
```

### Step 6.3: Confirmation (unless --force)

**If not `--force`:**

```
### Confirmation Required

This will modify your company structure. A backup will be created.

Proceed with upgrade? [y/N]
```

Wait for user confirmation. If declined:

```
## Upgrade Cancelled

No changes were made.

To preview changes:
  /company-upgrade --dry-run

To proceed with upgrade:
  /company-upgrade
```

Exit without changes.

### Step 6.4: Execute Migration

Run the migration script:

```bash
# With --force to skip script's own confirmation (we already confirmed)
uv run .claude/tools/migrate_company_v1.2.py --force
```

If custom backup directory specified:
```bash
uv run .claude/tools/migrate_company_v1.2.py --force --backup-dir="[backup-path]"
```

### Step 6.5: Verify Migration

After migration, verify the results:

```bash
# Check marker file created
ls -la .forge-company-root

# Check employees directory (renamed from agents)
ls -la .company/employees/

# Check assignments directory created
ls -la .company/assignments/

# Check org.json updated
cat .company/org.json | head -20
```

### Step 6.6: Display Success

```
## Upgrade Complete

Your company has been upgraded from v1.1 to v1.2.

### Changes Made

| Action | Status | Details |
|--------|--------|---------|
| Create `.forge-company-root` | done | Multi-project marker |
| Rename `agents/` to `employees/` | done | New naming convention |
| Create `assignments/` | done | Project assignment tracking |
| Update `org.json` | done | Version 2.0, mode: single-project |

### Backup Created

A backup was created at:
  [backup-dir]

To rollback if needed:
  /company-upgrade --rollback

To remove backup after verification:
  rm -rf [backup-dir]

### New Capabilities

You can now use multi-project features:

| Command | Description |
|---------|-------------|
| `/company-add-project` | Register projects with the company |
| `/company-projects` | List all registered projects |
| `/company-assign <employee> --project=<id>` | Assign employees to projects |

### Migration Mode

Your company is currently in **single-project mode** (safe default).

To enable full multi-project mode:
1. Add at least one project: `/company-add-project .`
2. The mode will automatically become "multi-project" when >1 project exists

Or manually update `.company/config.json`:
```json
{
  "mode": "multi-project"
}
```

### Next Steps

1. **Verify the upgrade:**
   /company-status

2. **Add your first project:**
   /company-add-project .

3. **Review employee assignments:**
   /company-projects

### Need Help?

- View full company status: `/company-status`
- Rollback this upgrade: `/company-upgrade --rollback`
- Check documentation: `.company/knowledge/`
```

## Step 7: Handle Errors

### Migration Script Fails

**If the migration script exits with error:**

```
## Upgrade Failed

The migration script encountered an error.

### Error Details
[error message from script]

### What Happened?
| Exit Code | Meaning |
|-----------|---------|
| 1 | General error (check file permissions) |
| 2 | Already migrated (nothing to do) |
| 3 | Not a v1.1 installation |
| 4 | User cancelled |

### Recovery

If a backup was created before the error:
  /company-upgrade --rollback

If no backup exists, your files should be unchanged (migration aborted early).

### Manual Recovery

If partial changes were made:

1. Remove the marker file:
   rm -f .forge-company-root

2. Remove any new directories:
   rm -rf .company/assignments/

3. Restore agents if renamed:
   mv .company/employees/ .company/agents/

4. Verify structure:
   /company-status
```

### Partial Migration

**If some steps succeeded but others failed:**

The migration script handles this atomically. If `org.json` update fails, the script attempts to restore from backup automatically.

If manual recovery is needed:

```
## Partial Migration Detected

Some migration steps completed but others failed.

### Recovery Options

**Option 1: Complete Rollback**
  /company-upgrade --rollback

**Option 2: Manual Completion**

If the migration was mostly complete, you can finish manually:

1. Create marker file (if missing):
   Create `.forge-company-root` with:
   ```json
   {
     "version": "1.0",
     "company_name": "[your company name]",
     "created_at": "[timestamp]",
     "config": {
       "work_queue_mode": "company-level",
       "strict_mode": false
     }
   }
   ```

2. Create assignments directory (if missing):
   mkdir -p .company/assignments
   Create `.company/assignments/_index.json` with:
   ```json
   {
     "projects": [],
     "updated_at": "[timestamp]"
   }
   ```

3. Update org.json (if needed):
   Add `"version": "2.0"` and `"mode": "single-project"`

4. Verify:
   /company-status
```

## Rules

1. **Always detect state first.** Never assume the current version - explicitly check for markers and files.

2. **Create backup before any changes.** The migration script handles this, but verify backup exists before continuing.

3. **Require confirmation.** Unless `--force` is specified, always prompt before making changes.

4. **Support rollback.** Always maintain the ability to restore from backup.

5. **Preserve data.** Never delete data - only rename, move, or add to existing structures.

6. **Atomic operations.** The migration script handles atomicity. If it reports failure, assume no partial state.

7. **Clear communication.** Show exactly what will change before and what changed after.

8. **Exit codes matter.** Check the migration script exit code and handle each case appropriately:
   - 0: Success
   - 1: Error (general failure)
   - 2: Already migrated (not an error, just nothing to do)
   - 3: Not a v1.1 installation (wrong starting state)
   - 4: User cancelled (via script's own prompt, shouldn't happen with --force)

9. **Backup directory naming.** Use timestamp format: `.company-backup-YYYYMMDD_HHMMSS`

10. **Single operation per invocation.** Each run either upgrades OR rolls back, never both.
