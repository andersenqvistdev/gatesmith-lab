# /company-projects — List All Registered Projects

Display all projects registered with the company, showing project IDs, paths, employee counts, and status.

## Input
$ARGUMENTS

Usage:
- `/company-projects` — List all projects in table format
- `/company-projects --detail` — Show employee assignments per project
- `/company-projects --json` — Output in machine-readable JSON format

## Step 0: Verify Company Root Exists

Search upward from current directory for `.forge-company-root`:

```bash
# Using company_resolver.py to find company root
uv run .claude/hooks/company/company_resolver.py find
```

**If no company root found:**
```
## No Company Root Found

Cannot list projects — no multi-project company root exists.

To create a company root first, run from the parent directory:
  /company-create

Or create a single-project company in this project:
  /company-init
```

Exit without further processing.

## Step 1: Load Project Index

Read the assignments index to get the list of registered projects:

```bash
cat [company_root]/.company/assignments/_index.json
```

Parse the JSON to extract the `projects` array.

**If index file doesn't exist or is empty:**
```
## No Projects Registered

No projects have been registered with this company yet.

### Getting Started

To add an existing project:
  /company-add-project ./path/to/project

To add the current directory as a project:
  /company-add-project .

### Company Root
**Location:** [company_root]
```

Exit without further processing.

## Step 2: Load Project Details

For each project ID in the index, read its assignment file:

```bash
cat [company_root]/.company/assignments/[project_id].json
```

Collect the following data for each project:
- `project_id` — Unique project identifier
- `project_name` — Display name
- `project_path` — Relative path from company root
- `registered_at` — Registration timestamp
- `assignments` — Array of employee assignments

Also load org.json for additional project metadata:

```bash
cat [company_root]/.company/org.json
```

## Step 3: Calculate Employee Counts

For each project, count the number of unique employees assigned:

```python
# Count unique employee IDs in assignments array
employee_count = len(set(a['employee_id'] for a in project['assignments']))
```

## Step 4: Determine Project Status

Project status is determined by:
- **active** — Has assignments and recent activity
- **idle** — Has assignments but no recent activity (>7 days)
- **unassigned** — No employees assigned

## Step 5: Display Output

### Default Output (Table Format)

```
## Company Projects

=====================================================================
 PROJECTS                                                    [company]
=====================================================================
 Company: [company_name]
 Company Root: [company_root]
=====================================================================

| Project ID | Name | Path | Employees | Status | Registered |
|------------|------|------|-----------|--------|------------|
| forge-framework | Gatesmith Framework | ./projects/forge | 3 | active | 2026-02-01 |
| api-service | API Service | ./services/api | 1 | idle | 2026-01-15 |
| docs-site | Documentation | ./docs | 0 | unassigned | 2026-01-20 |

### Summary
| Metric | Value |
|--------|-------|
| Total Projects | X |
| Active Projects | X |
| Idle Projects | X |
| Unassigned Projects | X |
| Total Assignments | X |

=====================================================================

### Quick Commands
- `/company-add-project` — Register a new project
- `/company-assign` — Assign employees to projects
- `/company-status` — View full company status
- `/company-projects --detail` — Show employee assignments
```

### Detailed Output (--detail flag)

```
## Company Projects (Detailed)

=====================================================================
 PROJECTS                                                    [company]
=====================================================================

### forge-framework
| Field | Value |
|-------|-------|
| Project ID | forge-framework |
| Display Name | Gatesmith Framework |
| Path | ./projects/forge |
| Absolute Path | /home/user/company/projects/forge |
| Status | active |
| Registered | 2026-02-01T10:30:00Z |
| Has .planning/ | yes |
| Has .claude/ | yes |

**Assigned Employees (3):**
| Employee ID | Name | Role | Assigned |
|-------------|------|------|----------|
| eng-lead-001 | Alice | Lead Engineer | 2026-02-01 |
| dev-002 | Bob | Developer | 2026-02-02 |
| test-003 | Carol | Tester | 2026-02-03 |

---

### api-service
| Field | Value |
|-------|-------|
| Project ID | api-service |
| Display Name | API Service |
| Path | ./services/api |
| Absolute Path | /home/user/company/services/api |
| Status | idle |
| Registered | 2026-01-15T14:00:00Z |
| Has .planning/ | yes |
| Has .claude/ | no |

**Assigned Employees (1):**
| Employee ID | Name | Role | Assigned |
|-------------|------|------|----------|
| dev-004 | Dan | Developer | 2026-01-16 |

---

### docs-site
| Field | Value |
|-------|-------|
| Project ID | docs-site |
| Display Name | Documentation |
| Path | ./docs |
| Absolute Path | /home/user/company/docs |
| Status | unassigned |
| Registered | 2026-01-20T09:00:00Z |
| Has .planning/ | no |
| Has .claude/ | yes |

**Assigned Employees:** None

To assign employees:
  /company-assign [employee-id] docs-site

=====================================================================

### Summary
Total Projects: 3 | Active: 1 | Idle: 1 | Unassigned: 1
```

### JSON Output (--json flag)

```json
{
  "company_root": "/path/to/company",
  "company_name": "Company Name",
  "updated_at": "2026-02-04T10:00:00Z",
  "projects": [
    {
      "project_id": "forge-framework",
      "project_name": "Gatesmith Framework",
      "project_path": "./projects/forge",
      "absolute_path": "/home/user/company/projects/forge",
      "status": "active",
      "registered_at": "2026-02-01T10:30:00Z",
      "employee_count": 3,
      "assignments": [
        {
          "employee_id": "eng-lead-001",
          "employee_name": "Alice",
          "role": "Lead Engineer",
          "assigned_at": "2026-02-01T12:00:00Z"
        }
      ],
      "metadata": {
        "has_planning": true,
        "has_claude": true
      }
    }
  ],
  "summary": {
    "total_projects": 3,
    "active_projects": 1,
    "idle_projects": 1,
    "unassigned_projects": 1,
    "total_assignments": 4
  }
}
```

## Step 6: Handle Edge Cases

### Corrupted Assignment File

If a project's assignment file cannot be parsed:

```
| [project_id] | [ERROR] | - | - | error | - |
```

Include a warning at the bottom:

```
### Warnings
- Project `[project_id]`: Assignment file corrupted or unreadable. Run `/company-add-project [path] --force` to re-register.
```

### Missing Project Directory

If a project's path no longer exists:

```
| [project_id] | [project_name] | [path] (MISSING) | X | missing | [date] |
```

Include a warning at the bottom:

```
### Warnings
- Project `[project_id]`: Directory not found at `[path]`. The project may have been moved or deleted.
```

### No Assignments Directory

If `.company/assignments/` doesn't exist:

```
## No Projects Registered

The assignments directory does not exist.

**Expected Location:** [company_root]/.company/assignments/

This may indicate an incomplete company setup. To fix:
1. Run `/company-init --force` to reinitialize the company structure
2. Re-register projects with `/company-add-project`
```

## Rules

1. **Always verify company root first.** The company root must exist before listing projects.

2. **Handle missing files gracefully.** If assignment files are missing or corrupted, show warnings but continue with other projects.

3. **Sort by registration date.** Projects should be listed newest first by default.

4. **Calculate accurate employee counts.** Count unique employee IDs from the assignments array.

5. **Respect output format flags.** The `--json` flag should output valid JSON only, no markdown.

6. **Show helpful next steps.** Include relevant commands for common actions.

7. **Use relative paths for display.** Show paths relative to company root in tables.

8. **Include absolute paths in detail view.** The `--detail` view should show both relative and absolute paths.

## Error Handling

### Permission Denied

```
## Permission Denied

Cannot read company files.

**Path:** [path]
**Error:** [error details]

Check that you have read permissions to the company root directory.
```

### Invalid JSON in Index

```
## Corrupted Index File

The project index file contains invalid JSON.

**File:** .company/assignments/_index.json
**Error:** [parse error]

To recover:
1. Check if a backup exists in `.company/backups/`
2. Manually fix the JSON syntax
3. Or re-register all projects with `/company-add-project`
```
