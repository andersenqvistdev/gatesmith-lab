# /company-assign — Assign Employee to Project

Manage employee-to-project assignments. Employees can be assigned to multiple projects, allowing them to contribute across the organization.

## Input
$ARGUMENTS

Usage:
- `/company-assign [employee-id] [project-id]` — Assign employee to a project
- `/company-assign --unassign [employee-id] [project-id]` — Remove employee from a project
- `/company-assign --list [employee-id]` — List all projects an employee is assigned to

Examples:
- `/company-assign dev-001 forge-framework`
- `/company-assign --unassign dev-001 docs-site`
- `/company-assign --list dev-001`

## Step 0: Determine Operating Mode

Use `company_resolver` to determine if operating in multi-project mode:

```bash
uv run .claude/hooks/company/company_resolver.py mode 2>/dev/null && echo "MULTI_PROJECT" || echo "SINGLE_PROJECT"
```

Also get the company directory path:

```bash
uv run .claude/hooks/company/company_resolver.py dir 2>/dev/null
```

**Store the results:**
- `$OPERATING_MODE`: "MULTI_PROJECT" or "SINGLE_PROJECT"
- `$COMPANY_DIR`: Path to `.company/` directory (resolved to company root in multi-project mode)

## Step 0.1: Validate Company Structure

Check that company is initialized:

```bash
ls $COMPANY_DIR/org.json 2>/dev/null || echo "NOT_INITIALIZED"
```

**If NOT_INITIALIZED:**
```
## Company Not Initialized

No company structure found.

Run `/company-init` or `/company-create` first to create the organizational structure.
```

Exit without changes.

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine the operation mode:

### 1.1 List Mode (--list flag)

If `--list` flag is present:
- Extract the employee ID following the flag
- Set operation to "list"

### 1.2 Unassign Mode (--unassign flag)

If `--unassign` flag is present:
- Extract employee-id and project-id following the flag
- Set operation to "unassign"

### 1.3 Assign Mode (default)

If no flag is present:
- First argument: employee-id
- Second argument: project-id
- Set operation to "assign"

**If required arguments are missing:**

For assign/unassign:
```
## Missing Arguments

Usage: /company-assign [employee-id] [project-id]
       /company-assign --unassign [employee-id] [project-id]
       /company-assign --list [employee-id]

Examples:
  /company-assign dev-001 forge-framework
  /company-assign --unassign dev-001 docs-site
  /company-assign --list dev-001

To see available employees:
  /company-status --employees

To see available projects:
  /company-projects
```

Exit without changes.

## Step 2: Load Organization Data

Read the organization file:

```bash
cat $COMPANY_DIR/org.json
```

Parse the JSON and store:
- `$EMPLOYEES`: Array of employee objects (check both `employees` and `agents` for backward compatibility)
- `$PROJECTS`: Array of project objects

## Step 3: Validate Employee Exists

Search for the employee by ID in the employees/agents array.

**If employee not found:**
```
## Employee Not Found

No employee with ID "[employee-id]" exists in the organization.

**Available Employees:**
| ID | Name | Department | Status |
|----|------|------------|--------|
| [id] | [name] | [dept] | [status] |
...

To hire a new employee:
  /company-hire [role description]
```

Exit without changes.

Store employee data:
- `$EMPLOYEE_ID`: Employee identifier
- `$EMPLOYEE_NAME`: Employee display name
- `$EMPLOYEE_ASSIGNMENTS`: Current project assignments array

## Step 4: Handle List Operation

If operation is "list", skip to Step 7 (Display List Output).

## Step 5: Validate Project Exists

### 5.1 Check Project Index

Read the assignments index:

```bash
cat $COMPANY_DIR/assignments/_index.json
```

Search for the project ID in the `projects` array.

**If project not found in index:**
```
## Project Not Found

No project with ID "[project-id]" is registered.

**Available Projects:**
| ID | Name | Path |
|----|------|------|
| [id] | [name] | [path] |
...

To register a new project:
  /company-add-project [path]

To list all projects:
  /company-projects
```

Exit without changes.

### 5.2 Load Project Assignment File

Read the project's assignment file:

```bash
cat $COMPANY_DIR/assignments/[project-id].json
```

**If file is missing or corrupted:**
```
## Project Assignment File Missing

The project "[project-id]" is in the index but its assignment file is missing.

**Expected Location:** $COMPANY_DIR/assignments/[project-id].json

To fix:
1. Re-register the project: /company-add-project [path] --force
2. Or remove from index manually
```

Exit without changes.

Store project data:
- `$PROJECT_ID`: Project identifier
- `$PROJECT_NAME`: Project display name
- `$PROJECT_ASSIGNMENTS`: Current assignments array

## Step 6: Execute Operation

### 6.1 Assign Operation

#### Check for Duplicate Assignment

Search `$PROJECT_ASSIGNMENTS` for an entry with matching `employee_id`.

**If already assigned:**
```
## Already Assigned

Employee "[employee-name]" ([employee-id]) is already assigned to project "[project-name]" ([project-id]).

**Assignment Details:**
| Field | Value |
|-------|-------|
| Assigned | [assigned_at] |
| Role | [role if specified] |

No changes made.
```

Exit without changes.

#### Add Assignment to Project File

Add new assignment entry to the project's `assignments` array:

```json
{
  "employee_id": "[employee-id]",
  "employee_name": "[employee-name]",
  "assigned_at": "[ISO 8601 timestamp]",
  "assigned_by": "company-assign"
}
```

Write updated project assignment file:

```bash
# Write to $COMPANY_DIR/assignments/[project-id].json
```

#### Update Employee in org.json

Add project-id to the employee's `projectAssignments` array (if not already present).

If this is the employee's first project assignment, also set `currentProject` to this project-id.

Write updated org.json:

```bash
# Write to $COMPANY_DIR/org.json
```

### 6.2 Unassign Operation

#### Check Assignment Exists

Search `$PROJECT_ASSIGNMENTS` for an entry with matching `employee_id`.

**If not assigned:**
```
## Not Assigned

Employee "[employee-name]" ([employee-id]) is not assigned to project "[project-name]" ([project-id]).

**Current Assignments for [employee-name]:**
| Project ID | Project Name | Assigned |
|------------|--------------|----------|
| [id] | [name] | [date] |
...

No changes made.
```

Exit without changes.

#### Remove Assignment from Project File

Remove the assignment entry from the project's `assignments` array.

Write updated project assignment file.

#### Update Employee in org.json

Remove project-id from the employee's `projectAssignments` array.

If `currentProject` was this project-id, set it to:
- The next project in `projectAssignments` (if any remain)
- `null` (if no projects remain)

Write updated org.json.

## Step 7: Display Output

### 7.1 Assign Success Output

```
## Employee Assigned to Project

=====================================================================
 ASSIGNMENT                                                  [success]
=====================================================================
 Employee: [employee-name] ([employee-id])
 Project: [project-name] ([project-id])
=====================================================================

### Assignment Details

| Field | Value |
|-------|-------|
| Employee ID | [employee-id] |
| Employee Name | [employee-name] |
| Department | [department] |
| Project ID | [project-id] |
| Project Name | [project-name] |
| Project Path | [project-path] |
| Assigned | [ISO timestamp] |

### Employee Assignments (Updated)

| # | Project ID | Project Name | Assigned |
|---|------------|--------------|----------|
| 1 | [project-id] | [project-name] | [date] |
| 2 | [other-id] | [other-name] | [date] |
...

### Files Updated

| File | Change |
|------|--------|
| $COMPANY_DIR/assignments/[project-id].json | Added employee to assignments |
| $COMPANY_DIR/org.json | Updated projectAssignments array |

=====================================================================

### Next Steps

1. **Submit work to this employee:**
   ```bash
   /company-request "[task description]" --project=[project-id] --assignee=[employee-id]
   ```

2. **View project status:**
   ```bash
   /company-projects --detail
   ```

3. **View employee status:**
   ```bash
   /company-status --employees
   ```
```

### 7.2 Unassign Success Output

```
## Employee Removed from Project

=====================================================================
 UNASSIGNMENT                                                [success]
=====================================================================
 Employee: [employee-name] ([employee-id])
 Project: [project-name] ([project-id])
=====================================================================

### Unassignment Details

| Field | Value |
|-------|-------|
| Employee ID | [employee-id] |
| Employee Name | [employee-name] |
| Project ID | [project-id] |
| Project Name | [project-name] |
| Originally Assigned | [assigned_at from removed entry] |
| Removed | [current ISO timestamp] |

### Remaining Assignments for [employee-name]

| # | Project ID | Project Name | Assigned |
|---|------------|--------------|----------|
| 1 | [other-id] | [other-name] | [date] |
...

*If no remaining assignments:*
**Employee has no remaining project assignments.**
Current Project: null (employee is now unassigned)

### Files Updated

| File | Change |
|------|--------|
| $COMPANY_DIR/assignments/[project-id].json | Removed employee from assignments |
| $COMPANY_DIR/org.json | Updated projectAssignments array |

=====================================================================

### Next Steps

1. **Assign to a different project:**
   ```bash
   /company-assign [employee-id] [new-project-id]
   ```

2. **View available projects:**
   ```bash
   /company-projects
   ```
```

### 7.3 List Output

```
## Employee Assignments: [employee-name]

=====================================================================
 ASSIGNMENTS                                                 [employee]
=====================================================================
 Employee: [employee-name] ([employee-id])
 Department: [department]
 Status: [status]
=====================================================================

### Project Assignments ([count])

| # | Project ID | Project Name | Path | Assigned | Current |
|---|------------|--------------|------|----------|---------|
| 1 | forge-framework | Gatesmith Framework | ./projects/forge | 2026-02-01 | * |
| 2 | api-service | API Service | ./services/api | 2026-02-02 | |
| 3 | docs-site | Documentation | ./docs | 2026-02-03 | |

*Current project marked with `*`*

### Employee Details

| Field | Value |
|-------|-------|
| ID | [employee-id] |
| Name | [employee-name] |
| Type | [persistent/consultant] |
| Department | [department] |
| Team | [team or "Unassigned"] |
| Status | [status] |
| Current Project | [currentProject or "None"] |
| Total Assignments | [count] |

=====================================================================

### Quick Commands

- `/company-assign [employee-id] [project-id]` — Add new assignment
- `/company-assign --unassign [employee-id] [project-id]` — Remove assignment
- `/company-projects` — List all projects
- `/company-status` — View full company status
```

**If employee has no assignments:**

```
## Employee Assignments: [employee-name]

=====================================================================
 ASSIGNMENTS                                                 [employee]
=====================================================================
 Employee: [employee-name] ([employee-id])
 Department: [department]
 Status: [status]
=====================================================================

### Project Assignments (0)

**No project assignments found.**

This employee is available but not assigned to any projects.
They cannot work until assigned to at least one project.

### Assign to a Project

```bash
/company-assign [employee-id] [project-id]
```

### Available Projects

| Project ID | Project Name | Path |
|------------|--------------|------|
| [id] | [name] | [path] |
...

To list all projects: `/company-projects`

=====================================================================
```

## Error Handling

### Permission Denied

```
## Permission Denied

Cannot write to company files.

**Path:** [path]
**Error:** [error details]

Check that you have write permissions to the company directory.
```

### Invalid JSON in org.json

```
## Corrupted Organization File

The organization file contains invalid JSON.

**File:** $COMPANY_DIR/org.json
**Error:** [parse error]

To recover:
1. Check if a backup exists
2. Manually fix the JSON syntax
3. Or run `/company-init --force` to reinitialize (warning: this will reset org structure)
```

### Invalid JSON in Assignment File

```
## Corrupted Assignment File

The project assignment file contains invalid JSON.

**File:** $COMPANY_DIR/assignments/[project-id].json
**Error:** [parse error]

To fix:
1. Re-register the project: `/company-add-project [path] --force`
2. Or manually repair the JSON file
```

### Concurrent Modification

If files changed between read and write (detected via timestamp or content hash):

```
## Concurrent Modification Detected

The organization files were modified by another process during this operation.

Please retry the command:
  /company-assign [args]

If this persists, check for other processes accessing company files.
```

## Rules

1. **Always validate employee and project exist.** Never create assignments for non-existent entities.

2. **Prevent duplicate assignments.** Check both the project assignment file and employee's projectAssignments array before adding.

3. **Update both files atomically.** If either update fails, report the partial state and suggest recovery steps.

4. **Use ISO 8601 timestamps.** All timestamps must be in ISO 8601 format (e.g., `2026-02-04T10:30:00Z`).

5. **Preserve existing data.** Only modify the specific assignment being added/removed. Never alter unrelated entries.

6. **Handle backward compatibility.** Check for both `employees` and `agents` arrays in org.json.

7. **Maintain currentProject consistency.** When unassigning, update currentProject if it was the removed project.

8. **Validate file integrity before writing.** Ensure JSON is valid before writing to prevent corruption.

9. **Single-project mode limitation.** In single-project mode, assignments are optional (employees work on the single project by default). Show a note but allow the operation.

10. **Employees can have multiple assignments.** The projectAssignments array supports multiple project IDs. This is intentional and allows cross-functional work.

11. **Always show remaining assignments after changes.** Help users understand the current state after modifications.

12. **Provide helpful next steps.** Guide users to related commands for common follow-up actions.
