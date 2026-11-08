# /company-dismiss — Dismiss an Employee

Dismiss an employee from the company, removing them from all project assignments, extracting learnings, and archiving their work. Core (persistent) employees cannot be dismissed without explicit confirmation.

## Input
$ARGUMENTS

Expected format: `[employee-id] [--force]`

Examples:
- `performance-optimization-specialist`
- `database-migration-expert --force`

## Step 0: Validate Company Structure

Check that `.company/` is initialized:

```bash
ls .company/org.json 2>/dev/null || echo "NOT_INITIALIZED"
```

**If NOT_INITIALIZED:**
```
## Company Not Initialized

The company directory structure does not exist.

Run `/company-init` first to create the organizational structure.
```

Exit without changes.

## Step 1: Parse and Validate Arguments

Parse `$ARGUMENTS` to extract the employee ID and any flags.

**If no employee ID provided:**
```
## Missing Employee ID

Usage: /company-dismiss [employee-id] [--force]

Examples:
  /company-dismiss performance-optimization-specialist
  /company-dismiss database-migration-expert --force

To see available employees, run `/company-status --employees`
```

Exit without changes.

## Step 2: Find Employee

### 2.1 Check org.json

Read `.company/org.json` and find the employee by ID in the `employees` array.

**If not found in employees array, also check `agents` array for backward compatibility.**

### 2.2 Locate Employee File

Search for the employee definition file in `.company/employees/[department]/[employee-id].md`.

**If employee not found in org.json AND no employee file exists:**
```
## Employee Not Found

No employee with ID "[employee-id]" exists in the organization.

Available employees:
| ID | Name | Type | Department |
|----|------|------|------------|
| [id] | [name] | [type] | [dept] |
...

To see all employees, run `/company-status --employees`
```

Exit without changes.

## Step 3: Verify Employee Type

Check the employee's `type` field.

**If type is "persistent" and --force not provided:**
```
## Cannot Dismiss Persistent Employee

The employee "[employee-id]" is a persistent (core) company employee.

Persistent employees are permanent members of the organization and require explicit confirmation to dismiss.

**Employee Details:**
| Field | Value |
|-------|-------|
| ID | [employee-id] |
| Name | [name] |
| Type | persistent |
| Department | [department] |
| Team | [team] |

**To confirm dismissal of a persistent employee:**
  /company-dismiss [employee-id] --force

This action will:
1. Remove the employee from all project assignments
2. Archive all employee data
3. Transfer learnings to the knowledge base
4. Update the organization structure
```

Exit without changes unless --force was provided.

**If type is "consultant" OR --force provided:** Proceed to Step 4.

## Step 4: Check Active Work

Verify the employee is not actively working:

Read the employee's current status from org.json.

**If status is "busy" or has active work items:**
```
## Employee Currently Working

The employee "[employee-id]" is currently assigned to active work.

**Current Status:** [status]
**Active Work Items:**
| Work ID | Project | Title | Status |
|---------|---------|-------|--------|
| [id] | [project] | [title] | [status] |

**Options:**
1. Wait for current work to complete, then dismiss
2. Reassign work to another employee first
3. Use `/company-dismiss [employee-id] --force` to dismiss anyway
   (Warning: work will be marked as abandoned)
```

Wait for user to confirm `--force` or exit.

## Step 5: Remove From All Project Assignments

### 5.1 Find All Project Assignments

Read `.company/assignments/_index.json` to get list of all projects.

For each project in the index:
1. Read `.company/assignments/[project-id].json`
2. Check if employee has any assignments (active or historical)

### 5.2 Deactivate All Assignments

For each project where the employee is assigned:

```json
{
  "employee_id": "[employee-id]",
  "role": "[role]",
  "start_date": "[original]",
  "end_date": "[current ISO timestamp]",
  "active": false,
  "termination_reason": "employee_dismissed"
}
```

Update the `updated_at` field in each modified project file.

### 5.3 Track Removed Assignments

Create a list of all project assignments that were deactivated:

```
Removed from projects:
| Project | Role | Duration |
|---------|------|----------|
| [project-id] | [role] | [start] to [now] |
...
```

## Step 6: Extract Learnings

### 6.1 Read Employee Memory

Read the employee's file at `.company/employees/[department]/[employee-id].md`:

From the **Memory** section, extract:
- **Learnings** entries (valuable for preventing repeated errors and preserving successful patterns)
- **Preferences** that proved effective
- **Current Context** notes

From the **Assignment History** section, extract:
- **Contributions** per project
- **Lessons Learned** per project

### 6.2 Identify Cross-Project Learnings

Look for patterns that appeared across multiple project assignments:
- Repeated lessons learned
- Consistent preferences
- Accumulated domain expertise

These cross-project learnings are especially valuable as they represent validated knowledge.

### 6.3 Prepare Learnings Summary

```markdown
## Learnings Extracted from [employee-id]

**Extraction Date:** [ISO timestamp]
**Employee:** [name] ([employee-id])
**Active Period:** [created/hireDate] to [dismissed date]
**Department:** [department]
**Projects Worked:** [count]

### Cross-Project Insights
<!-- Learnings that appeared across multiple projects -->

[List cross-project patterns and lessons]

### Project-Specific Learnings

#### [Project 1]
- [Contribution summary]
- [Key lesson]

#### [Project 2]
- [Contribution summary]
- [Key lesson]

### Domain Knowledge

[List domain expertise worth preserving]

### Recommendations

[Any recommendations for future similar employees]
```

## Step 7: Merge Learnings into Knowledge Base

### 7.1 Patterns

For patterns worth preserving, append to `.company/knowledge/patterns.md`:

```markdown
### [Pattern Name] (from [employee-id])

**Category:** [category]

**Context:** [context]

**Pattern:**
[pattern description]

**Projects Applied:** [list of projects where this pattern was used]

**Source:** Employee [employee-id], dismissed [date]
```

### 7.2 Lessons/Decisions

For significant lessons that affect future decisions, consider adding to `.company/knowledge/decisions.md` as an ADR if appropriate.

### 7.3 Update Knowledge Base

After merging, track what was added:

```
### Knowledge Base Updates

Added to patterns.md:
- [Pattern 1]: [brief description]
- [Pattern 2]: [brief description]

Added to decisions.md:
- (none / or list any ADRs added)

Cross-project learnings preserved:
- [Learning 1]
- [Learning 2]
```

## Step 8: Archive Employee Files

### 8.1 Create Archive Directory

```bash
mkdir -p .company/archive/employees
```

### 8.2 Archive Employee Definition

Move the employee's file to the archive with timestamp:

```bash
mv .company/employees/[department]/[employee-id].md \
   .company/archive/employees/[employee-id]-[YYYYMMDD].md
```

### 8.3 Add Dismissal Metadata

Prepend to the archived file:

```markdown
---
archived: [ISO timestamp]
reason: dismissed
final_status: [status at dismissal]
projects_worked: [list of project IDs]
total_duration: [calculated duration]
---

[Original file content]
```

### 8.4 Handle Legacy Agent Memory

If `.company/agents/[employee-id]/` directory exists (legacy structure):

```bash
mv .company/agents/[employee-id] .company/archive/employees/[employee-id]-[YYYYMMDD]-memory/
```

## Step 9: Update org.json

### 9.1 Remove from Employees Array

Read `.company/org.json`, remove the employee entry from the `employees` array.

**Also check and remove from `agents` array if it exists (backward compatibility).**

### 9.2 Remove from Team Members

If the employee was assigned to a team, remove their ID from that team's `members` array in the department structure.

### 9.3 Update Department Head/Team Lead

If the employee was a department `head` or team `lead`:
- Set the respective field to `null`
- Add a warning to the output about needing a replacement

### 9.4 Clean Up Active Work

If the employee had any work items in `work.active`:
- Mark those items as status: "unassigned"
- Clear the `assignee` field
- Add `abandoned_by: [employee-id]` and `abandoned_at: [timestamp]`

### 9.5 Write Updated org.json

Write the updated org.json back to file with proper formatting.

## Step 10: Display Dismissal Summary

```
## Employee Dismissed: [Employee Name]

===============================================================================
 EMPLOYEE DISMISSED                                                  [complete]
===============================================================================

### Employee Details

| Field | Value |
|-------|-------|
| ID | [employee-id] |
| Name | [Employee Name] |
| Type | [persistent/consultant] |
| Department | [Department] |
| Team | [Team or N/A] |
| Hired | [hireDate or created] |
| Dismissed | [now] |
| Active Duration | [calculated duration] |

### Project Assignments Removed

| Project | Role | Period | Status |
|---------|------|--------|--------|
| [project-1] | [role] | [start] - [now] | Deactivated |
| [project-2] | [role] | [start] - [now] | Deactivated |

**Total projects:** [count]

### Work Summary

| Metric | Value |
|--------|-------|
| Projects Worked | [count] |
| Last Active | [lastActive] |
| Final Status | [status] |

### Knowledge Extracted

**Cross-project learnings preserved:** [count]
- [Learning 1]
- [Learning 2]

**Patterns added to knowledge base:** [count]
- [Pattern 1]
- [Pattern 2]

**Domain knowledge preserved:** [count]
- [Domain area 1]

### Files Archived

| Original Location | Archive Location |
|-------------------|------------------|
| .company/employees/[dept]/[id].md | .company/archive/employees/[id]-[YYYYMMDD].md |
| .company/agents/[id]/ (if exists) | .company/archive/employees/[id]-[YYYYMMDD]-memory/ |

### Organization Updates

- Removed from org.json employees array
- Removed from [N] project assignment(s)
- [Removed from team members list / N/A]
- [Was department head - needs replacement / N/A]
- [Was team lead - needs replacement / N/A]
- [Work items marked unassigned / No active work]

===============================================================================

### Archive Contents

The following files have been preserved in `.company/archive/employees/`:

| File | Contents |
|------|----------|
| [id]-[YYYYMMDD].md | Complete employee record with dismissal metadata |
| [id]-[YYYYMMDD]-memory/ | Working memory (if legacy structure existed) |

To review archived data:
```bash
cat .company/archive/employees/[employee-id]-[YYYYMMDD].md
```

### Leadership Vacancies (if any)

[If employee was department head or team lead, display:]
⚠️  **[Department/Team] now has no [head/lead]**

Consider running `/company-hire` or `/company-reorg` to fill this role.

### Next Steps

1. **Review merged knowledge:**
   Check `.company/knowledge/patterns.md` for newly added patterns

2. **Check orphaned work:**
   Run `/company-status --work` to check for unassigned tasks

3. **Verify project coverage:**
   Run `/company-projects` to ensure all projects have adequate staffing

4. **Hire replacement if needed:**
   Run `/company-hire [role description]` to bring in a new employee
```

## Error Handling

### Employee Has Active Work (without --force)

```
## Cannot Dismiss: Active Work

Employee "[employee-id]" has active work assignments.

**Active Work Items:**
| Work ID | Project | Title | Status |
|---------|---------|-------|--------|
| [id] | [project] | [title] | [status] |

Either:
1. Wait for work to complete
2. Reassign work to another employee
3. Use `/company-dismiss [employee-id] --force` to dismiss anyway
   (work will be marked as unassigned)
```

### Employee File Not Found (but exists in org.json)

```
## Warning: No Employee File

Employee "[employee-id]" exists in org.json but has no file at `.company/employees/[dept]/[employee-id].md`.

This employee may have been partially created or their file was already archived.

Proceeding with dismissal (removing from org.json and assignments only).
```

### Archive Already Exists

If `.company/archive/employees/[employee-id]-[YYYYMMDD].md` already exists:

Use timestamp with time: `[employee-id]-[YYYYMMDD]-[HHMMSS].md`

### No Learnings to Extract

```
## Note: No Learnings Found

Employee "[employee-id]" has no documented learnings or assignment history.

This may indicate:
- The employee was newly hired and not yet active
- Learnings were not documented during work

Proceeding with dismissal without knowledge base updates.
```

### Assignment Files Not Found

```
## Note: No Assignment Records

No project assignment records found in `.company/assignments/`.

This may indicate:
- The employee was never assigned to projects
- Assignment tracking was not enabled

Proceeding with dismissal using org.json data only.
```

### Persistent Employee Dismissal Warning

When --force is used on a persistent employee:

```
⚠️  **Dismissing Persistent Employee**

You are dismissing a persistent (core) employee. This action:
- Removes accumulated context and institutional knowledge
- May impact ongoing projects that depend on this employee
- Cannot be easily undone (though archive is preserved)

Proceeding with dismissal as --force was specified.
```

## Rules

1. **Protect persistent employees.** Persistent employees require `--force` confirmation. This prevents accidental dismissal of core team members who carry institutional knowledge.

2. **Remove from ALL projects first.** Before archiving, deactivate all project assignments. This ensures no orphaned references exist in the assignment system.

3. **Extract cross-project learnings.** Knowledge that appears across multiple projects is especially valuable and should be prioritized for preservation in the knowledge base.

4. **Archive to employees subdirectory.** Employee data goes to `.company/archive/employees/`, maintaining separation from other archived content.

5. **Update org.json atomically.** Read, modify, and write as a single operation. Validate JSON before writing.

6. **Respect active work.** Employees with active assignments cannot be dismissed without --force confirmation. This prevents orphaned work.

7. **Handle leadership roles.** If dismissing a department head or team lead, clearly communicate the vacancy that needs to be filled.

8. **Clean up completely.** Remove the employee from all locations: org.json, team members arrays, project assignments, and employee files.

9. **Document the dismissal.** The summary provides a complete audit trail of what was archived, what was merged, and what was removed.

10. **Preserve department structure.** Even if dismissing the last employee in a department directory, keep the directory structure intact.

11. **Support backward compatibility.** Check both `employees` and `agents` arrays in org.json, and handle legacy `.company/agents/` memory directories if they exist.
