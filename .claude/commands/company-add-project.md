# /company-add-project — Link Existing Project to Company

Register an existing Gatesmith project with a multi-project company. This creates an assignment file that links the project to the company's employee and work queue systems.

## Input
$ARGUMENTS

Usage:
- `/company-add-project ./path/to/project` — Add project at specified path
- `/company-add-project .` — Add current directory as project
- `/company-add-project` — Add current directory as project (default)

Optional arguments:
- `--name "Project Name"` — Set display name (default: directory name)
- `--force` — Re-register even if project already exists

## Step 0: Verify Company Root Exists

Search upward from current directory for `.forge-company-root`:

```bash
# Using company_resolver.py to find company root
uv run .claude/hooks/company/company_resolver.py find
```

**If no company root found:**
```
## No Company Root Found

Cannot add project — no multi-project company root exists.

To create a company root first, run from the parent directory:
  /company-create

Or create a single-project company in this project:
  /company-init
```

Exit without changes.

## Step 1: Parse Arguments and Resolve Project Path

Parse `$ARGUMENTS`:
- Extract project path (default: current directory `.`)
- Extract `--name` value if provided
- Check for `--force` flag

Resolve the project path to an absolute path:

```bash
# If relative path provided
realpath ./path/to/project
```

**If path does not exist:**
```
## Project Path Not Found

The specified path does not exist.

**Path:** [resolved path]

Please provide a valid path to an existing project directory.
```

Exit without changes.

## Step 2: Validate Project Structure

Check that the project has required Gatesmith structure (either `.planning/` or `.claude/`):

```bash
ls -la [project_path]/.planning 2>/dev/null
ls -la [project_path]/.claude 2>/dev/null
```

**If neither exists:**
```
## Invalid Project Structure

The specified directory is not a valid Gatesmith project.

**Path:** [project_path]

A valid project must have at least one of:
- `.planning/` — Gatesmith planning documents
- `.claude/` — Claude configuration

To initialize a new project:
  cd [project_path]
  /new-project
```

Exit without changes.

## Step 3: Generate Project ID

Use the `company_resolver.py` to generate a unique project ID:

```bash
uv run .claude/hooks/company/company_resolver.py id [project_path]
```

The project ID format is: `dirname-hash6` (e.g., `myproject-a1b2c3`)

## Step 4: Check for Duplicate Registration

Read the assignments index to check if project is already registered:

```bash
cat [company_root]/.company/assignments/_index.json
```

**If project already registered and no `--force` flag:**
```
## Project Already Registered

This project is already registered with the company.

**Project ID:** [project_id]
**Path:** [project_path]
**Assignment File:** [company_root]/.company/assignments/[project_id].json

To view project assignments:
  /company-status --project=[project_id]

To re-register (will reset assignments), run:
  /company-add-project [path] --force
```

Exit without changes.

## Step 5: Calculate Relative Path

Calculate the project path relative to the company root:

```bash
# Get relative path from company root to project
realpath --relative-to=[company_root] [project_path]
```

## Step 6: Create Assignment File

Create the assignment file at `.company/assignments/[project_id].json`:

```json
{
  "project_id": "[project_id]",
  "project_name": "[Project Name or directory name]",
  "project_path": "[relative path from company root]",
  "absolute_path": "[resolved absolute path]",
  "registered_at": "[ISO 8601 timestamp]",
  "registered_by": "company-add-project",
  "assignments": [],
  "metadata": {
    "has_planning": true|false,
    "has_claude": true|false
  }
}
```

## Step 7: Update Index File

Update `.company/assignments/_index.json` to include the new project:

```json
{
  "projects": ["existing-project-1", "[project_id]"],
  "updated_at": "[ISO 8601 timestamp]"
}
```

If the index file doesn't exist, create it:

```json
{
  "projects": ["[project_id]"],
  "updated_at": "[ISO 8601 timestamp]"
}
```

## Step 8: Update org.json

Add the project to the organization's projects list in `.company/org.json`:

Read the current org.json and add to the `projects` array:

```json
{
  "projects": [
    {
      "id": "[project_id]",
      "name": "[Project Name]",
      "path": "[relative path]",
      "added_at": "[ISO 8601 timestamp]"
    }
  ]
}
```

## Step 9: Display Summary

```
## Project Added to Company

=====================================================================
 PROJECT REGISTRATION                                       [success]
=====================================================================
 Project: [Project Name]
 ID: [project_id]
 Path: [relative path from company root]
=====================================================================

### Project Details
| Field | Value |
|-------|-------|
| Project ID | [project_id] |
| Display Name | [Project Name] |
| Relative Path | [relative path] |
| Absolute Path | [absolute path] |
| Has .planning/ | [yes/no] |
| Has .claude/ | [yes/no] |
| Registered | [timestamp] |

### Files Created/Updated
| File | Action | Description |
|------|--------|-------------|
| .company/assignments/[project_id].json | created | Project assignment file |
| .company/assignments/_index.json | updated | Added to project index |
| .company/org.json | updated | Added to organization projects |

=====================================================================

### Next Steps

1. **Assign employees to this project:**
   ```bash
   /company-assign [employee-id] [project_id]
   ```

2. **View project status:**
   ```bash
   /company-status --project=[project_id]
   ```

3. **Submit work to this project:**
   ```bash
   /company-request "feature description" --project=[project_id]
   ```

### Available Commands
- `/company-projects` — List all registered projects
- `/company-assign` — Assign employees to projects
- `/company-status` — View company and project status
- `/company-request` — Submit work requests
```

## Rules

1. **Always verify company root first.** The company root must exist before adding projects.

2. **Validate project structure.** Projects must have `.planning/` or `.claude/` to be valid.

3. **Prevent duplicates.** Never re-register a project without `--force` flag.

4. **Use relative paths in assignment files.** Store relative paths from company root for portability.

5. **Store absolute path as backup.** Include absolute path for debugging but use relative for lookups.

6. **Preserve existing assignments.** When using `--force`, preserve the `assignments` array if it exists.

7. **Update all relevant files atomically.** If any update fails, report the error and the partial state.

8. **Use ISO 8601 timestamps.** All timestamps must be in ISO 8601 format.

## Error Handling

### Permission Denied

```
## Permission Denied

Cannot write to company directory.

**Path:** [path]
**Error:** [error details]

Check that you have write permissions to the company root directory.
```

### Invalid JSON in Index

```
## Corrupted Index File

The project index file contains invalid JSON.

**File:** .company/assignments/_index.json
**Error:** [parse error]

To fix manually:
1. Backup the corrupted file
2. Create a new index with the project list
3. Re-run this command

Or run with --force to overwrite.
```

### Project Outside Company

```
## Project Outside Company Root

The specified project is not within the company directory tree.

**Company Root:** [company_root]
**Project Path:** [project_path]

This is allowed but the relative path will be: [relative path with ../ components]

Proceed? The project will be registered but may be harder to manage.

To confirm, run:
  /company-add-project [path] --external
```

Note: For projects outside the company root, store the absolute path as the primary reference.
