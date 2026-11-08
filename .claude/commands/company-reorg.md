# /company-reorg — Reorganize Company Structure

Reorganize the company structure with atomic updates and full audit logging. Supports adding/archiving departments, reassigning agents between teams, moving employees between projects, and promoting agents to new roles. Works at company level in multi-project mode.

## Input
$ARGUMENTS

**Operations:**
- `add-dept <name>` — Create a new department
- `remove-dept <name>` — Archive a department (not delete)
- `reassign <agent> <new-team>` — Move agent to a different team
- `reassign <agent> --project=<project-id>` — Move agent to a different project (multi-project mode)
- `promote <agent> <new-role>` — Change an agent's role
- `transfer <agent> <from-project> <to-project>` — Transfer agent between projects (multi-project mode)

## Step 0: Resolve Company Root and Mode

Use the company_resolver to find the company root and determine operating mode:

```bash
# Find company root (searches upward for .forge-company-root)
uv run .claude/hooks/company/company_resolver.py find

# Check if in multi-project mode
uv run .claude/hooks/company/company_resolver.py mode

# Get company directory path
uv run .claude/hooks/company/company_resolver.py dir
```

Store the results:
- `company_root` — Path to company root (multi-project) or current directory (legacy)
- `company_dir` — Path to `.company/` directory
- `is_multi_project` — Boolean indicating multi-project mode

## Step 0.1: Validate Company Exists

Check if the resolved `.company/` directory exists:

```bash
ls -la [company_dir]/org.json 2>/dev/null
```

**If not initialized (multi-project mode):**
```
## Company Not Initialized

No company directory found at [company_root]/.company/.

This appears to be a multi-project company root (found .forge-company-root).

To initialize the company structure, run:
  /company-init

This will create:
- Organization structure (departments, teams)
- Knowledge base (decisions, patterns)
- Agent memory templates
- Project assignments directory
```

**If not initialized (legacy mode):**
```
## Company Not Initialized

The company structure has not been initialized yet.

Run `/company-init` first to create the organizational structure.
```

Exit without changes.

## Step 1: Parse Operation

Parse `$ARGUMENTS` to determine which operation to perform:

| Pattern | Operation | Extracted Values |
|---------|-----------|------------------|
| `add-dept <name>` | ADD_DEPT | name |
| `remove-dept <name>` | REMOVE_DEPT | name |
| `reassign <agent> <team>` | REASSIGN_TEAM | agent_id, team_id |
| `reassign <agent> --project=<id>` | REASSIGN_PROJECT | agent_id, project_id |
| `transfer <agent> <from> <to>` | TRANSFER_PROJECT | agent_id, from_project, to_project |
| `promote <agent> <role>` | PROMOTE | agent_id, new_role |

**Multi-project operations (REASSIGN_PROJECT, TRANSFER_PROJECT) are only valid in multi-project mode.**

If no valid operation detected:
```
## Invalid Operation

Usage:
  /company-reorg add-dept <department-name>
  /company-reorg remove-dept <department-name>
  /company-reorg reassign <agent-id> <team-id>
  /company-reorg promote <agent-id> <new-role>

### Multi-Project Operations (if in multi-project mode)
  /company-reorg reassign <agent-id> --project=<project-id>
  /company-reorg transfer <agent-id> <from-project> <to-project>

Examples:
  /company-reorg add-dept security
  /company-reorg remove-dept design
  /company-reorg reassign alice-dev core
  /company-reorg reassign alice-dev --project=api-service
  /company-reorg transfer bob-eng forge-core api-service
  /company-reorg promote bob-eng team-lead
```

Exit without changes.

## Step 2: Load Current State

Read the current organization state:

1. Read `[company_dir]/org.json` to get current structure
2. Read `[company_dir]/config.json` for enabled departments

**In multi-project mode, also load:**
3. Read `[company_dir]/assignments/_index.json` to get project list
4. Read `[company_dir]/assignments/[project_id].json` for each registered project's assignments

## Step 3: Execute Operation

### ADD_DEPT — Create New Department

**Validation:**
- Department name must be lowercase alphanumeric with hyphens
- Department ID must not already exist
- Name cannot be empty

**Execution:**

1. Generate department ID from name (lowercase, spaces to hyphens)
2. Create department entry:
```json
{
  "id": "<generated-id>",
  "name": "<display-name>",
  "head": null,
  "status": "active",
  "created": "<ISO-8601-timestamp>",
  "teams": [
    {
      "id": "general",
      "name": "General",
      "lead": null,
      "members": []
    }
  ]
}
```
3. Add to `departments` array in org.json
4. Update `enabledDepartments` in config.json
5. Create agent memory directory: `[company_dir]/agents/<dept-id>/`
6. Copy TEMPLATE files to new directory

**Log entry:**
```
ORG-CHANGE: Added department '<name>' with ID '<id>'
Timestamp: <ISO-8601>
Authorized by: user
```

---

### REMOVE_DEPT — Archive Department

**Validation:**
- Department must exist
- Department must not have active agents (or confirm force archive)
- Cannot archive if it's the last active department

**Pre-check:**
```
## Confirm Archive

Department: <name>
Current Status: active
Teams: <count>
Agents: <count>

⚠️  Archiving will:
- Mark department as archived (not delete)
- Set all agents to 'archived' status
- Preserve all history and memory
- Remove from active org view

The department can be restored later with:
  /company-reorg restore-dept <name>

Proceed with archive? [y/N]
```

**Execution:**

1. Set department `status` to "archived"
2. Set department `archivedAt` to current timestamp
3. Update all agent statuses to "archived"
4. Remove from `enabledDepartments` in config.json (but keep in org.json)
5. Keep agent memory files intact (for history)

**Log entry:**
```
ORG-CHANGE: Archived department '<name>' (ID: <id>)
Agents archived: <count>
Timestamp: <ISO-8601>
Authorized by: user
Reason: Organizational restructure
```

---

### REASSIGN_TEAM — Move Agent Between Teams

**Validation:**
- Agent must exist in `[company_dir]/org.json`
- Target team must exist
- Agent cannot be reassigned while busy (status check)
- Agent cannot be reassigned to their current team

**Pre-check:**
```
## Confirm Team Reassignment

Agent: <name> (<id>)
Current Assignment:
  Department: <current-dept>
  Team: <current-team>
  Role: <current-role>

New Assignment:
  Department: <new-dept>
  Team: <new-team>
  Role: <same-role>

⚠️  Agent's current work items will be transferred.

Proceed? [y/N]
```

**Execution:**

1. Remove agent from current team's `members` array
2. If agent was `lead` of old team, set old team's `lead` to null
3. Add agent to new team's `members` array
4. Update agent's `department` and `team` fields
5. Update agent's memory file with reassignment note

**Log entry:**
```
ORG-CHANGE: Reassigned agent '<name>' (<id>)
From: <old-dept>/<old-team>
To: <new-dept>/<new-team>
Timestamp: <ISO-8601>
Authorized by: user
```

---

### REASSIGN_PROJECT — Assign Agent to Different Project (Multi-Project Mode)

**Only available in multi-project mode.** Adds an agent to a project's assignment list.

**Validation:**
- Must be in multi-project mode (checked via `company_resolver.py mode`)
- Agent must exist in `[company_dir]/org.json`
- Target project must exist in `[company_dir]/assignments/_index.json`
- Agent can be assigned to multiple projects (not exclusive)

**Pre-check:**
```
## Confirm Project Assignment

Agent: <name> (<id>)
Department: <dept> / Team: <team>
Current Project Assignments: <list of project IDs or "none">

Target Project:
  Project ID: <project-id>
  Project Name: <project-name>
  Path: <relative-path>
  Current Assignees: <count>

Action: ADD agent to project <project-id>

Note: This does not remove the agent from other projects.
Use `/company-reorg transfer` to move between projects.

Proceed? [y/N]
```

**Execution:**

1. Read `[company_dir]/assignments/<project-id>.json`
2. Add assignment entry to the `assignments` array:
   ```json
   {
     "employee_id": "<agent-id>",
     "role": "contributor",
     "start_date": "<ISO-8601>",
     "end_date": null,
     "active": true
   }
   ```
3. Update `updated_at` timestamp in the assignment file
4. Update agent's `projectAssignments` array in org.json to include `<project-id>`
5. Update agent's memory file with project assignment note

**Log entry:**
```
ORG-CHANGE: Assigned agent '<name>' (<id>) to project '<project-id>'
Project: <project-name>
Path: <project-path>
Role: contributor
Timestamp: <ISO-8601>
Authorized by: user
```

---

### TRANSFER_PROJECT — Transfer Agent Between Projects (Multi-Project Mode)

**Only available in multi-project mode.** Moves an agent from one project to another, removing from the source and adding to the target.

**Validation:**
- Must be in multi-project mode
- Agent must exist in `[company_dir]/org.json`
- Source project must exist and agent must be assigned to it
- Target project must exist
- Source and target must be different projects

**Pre-check:**
```
## Confirm Project Transfer

Agent: <name> (<id>)
Department: <dept> / Team: <team>

Transfer Details:
  FROM Project: <from-project-name> (<from-project-id>)
    Path: <from-path>
    Assignment Start: <date>
    Role: <current-role>

  TO Project: <to-project-name> (<to-project-id>)
    Path: <to-path>
    Current Assignees: <count>

⚠️  This will:
- End agent's assignment to <from-project-id>
- Create new assignment to <to-project-id>
- Transfer any active work items (if applicable)

Proceed? [y/N]
```

**Execution:**

1. Read `[company_dir]/assignments/<from-project-id>.json`
2. Find agent's assignment entry
3. Set `active` to `false` and `end_date` to current timestamp (preserve history)
4. Update `updated_at` timestamp

5. Read `[company_dir]/assignments/<to-project-id>.json`
6. Add new assignment entry:
   ```json
   {
     "employee_id": "<agent-id>",
     "role": "contributor",
     "start_date": "<ISO-8601>",
     "end_date": null,
     "active": true,
     "transferred_from": "<from-project-id>"
   }
   ```
7. Update `updated_at` timestamp

8. Update agent's `projectAssignments` array in org.json:
   - Remove `<from-project-id>`
   - Add `<to-project-id>`
9. Update agent's `currentProject` to `<to-project-id>` (if was set to from-project)
10. Update agent's memory file with transfer note

**Log entry:**
```
ORG-CHANGE: Transferred agent '<name>' (<id>) between projects
From: <from-project-name> (<from-project-id>)
To: <to-project-name> (<to-project-id>)
Timestamp: <ISO-8601>
Authorized by: user
Reason: Organizational reassignment
```

---

### PROMOTE — Change Agent Role

**Validation:**
- Agent must exist
- New role must be valid (see roles below)
- If promoting to lead/head, check no existing lead/head

**Valid roles:**
- `member` — Standard team member
- `senior` — Senior team member
- `team-lead` — Team lead (one per team)
- `dept-head` — Department head (one per department)

**Pre-check:**
```
## Confirm Promotion

Agent: <name> (<id>)
Current Role: <current-role>
New Role: <new-role>

<if team-lead>
⚠️  This will make <agent> the lead of team '<team>'.
    Current lead: <current-lead or 'none'>
</if>

<if dept-head>
⚠️  This will make <agent> the head of department '<dept>'.
    Current head: <current-head or 'none'>
</if>

Proceed? [y/N]
```

**Execution:**

1. Update agent's `role` field
2. If promoting to `team-lead`:
   - Set team's `lead` to agent ID
   - Demote previous lead to `senior` (if exists)
3. If promoting to `dept-head`:
   - Set department's `head` to agent ID
   - Demote previous head to `team-lead` (if exists)
4. Update agent's memory file with promotion note

**Log entry:**
```
ORG-CHANGE: Promoted agent '<name>' (<id>)
Previous role: <old-role>
New role: <new-role>
Department: <dept>
Team: <team>
Timestamp: <ISO-8601>
Authorized by: user
```

## Step 4: Atomic Update

All changes must be atomic. Write changes in this order:

1. Create backup: `cp [company_dir]/org.json [company_dir]/org.json.backup`
2. Write updated org.json
3. Write updated config.json (if changed)
4. **In multi-project mode, also update:**
   - Project assignment files: `[company_dir]/assignments/<project-id>.json`
   - Assignment index: `[company_dir]/assignments/_index.json` (if projects added/removed)
5. Verify all JSON is valid: `python -c "import json; json.load(open('[company_dir]/org.json'))"`
6. If verification fails, restore backup and report error
7. Remove backup on success

## Step 5: Log to Knowledge Base

Append to `[company_dir]/knowledge/decisions.md`:

```markdown
## ORG-<NNNN>: <Operation Title>

**Status:** Executed

**Date:** <YYYY-MM-DD>

### Context

<Auto-generated description of why this change was made>

### Change

<Operation details>

### Impact

- <List of affected agents/teams/departments>
```

Increment the ORG number based on existing entries.

## Step 6: Display Summary

### Single-Project Mode (Legacy)

```
═══════════════════════════════════════════════════════════════
 ORGANIZATION UPDATED                               [<operation>]
═══════════════════════════════════════════════════════════════

### Change Applied
<operation-specific summary>

### Updated Structure
| Department | Status | Teams | Agents |
|------------|--------|-------|--------|
| Engineering | active | 3 | 5 |
| Product | active | 2 | 3 |
| Design | archived | 2 | 0 |

### Logged
- Change logged to .company/knowledge/decisions.md as ORG-<NNNN>
- org.json updated atomically

### Next Steps
- Use `/company-status` to view full organization
- Use `/company-hire` to add agents to new departments
- Use `/company-assign` to delegate work

═══════════════════════════════════════════════════════════════
```

### Multi-Project Mode

```
═══════════════════════════════════════════════════════════════
 ORGANIZATION UPDATED                               [<operation>]
═══════════════════════════════════════════════════════════════
 Mode: Multi-Project
 Company Root: [company_root]
═══════════════════════════════════════════════════════════════

### Change Applied
<operation-specific summary>

### Updated Structure
| Department | Status | Teams | Agents |
|------------|--------|-------|--------|
| Engineering | active | 3 | 5 |
| Product | active | 2 | 3 |
| Design | archived | 2 | 0 |

### Project Assignments (for REASSIGN_PROJECT/TRANSFER_PROJECT operations)
| Project | Path | Assigned Agents | Status |
|---------|------|-----------------|--------|
| forge-framework | ./projects/forge | 3 | active |
| api-service | ./services/api | 1 | active |
| docs-site | ./docs | 0 | unassigned |

### Logged
- Change logged to [company_dir]/knowledge/decisions.md as ORG-<NNNN>
- org.json updated atomically
- Assignment files updated (if project operations)

### Next Steps
- Use `/company-status` to view full organization
- Use `/company-projects` to view project assignments
- Use `/company-hire` to add agents to departments
- Use `/company-assign` to delegate work
- Use `/company-reorg transfer <agent> <from> <to>` to move agents between projects

═══════════════════════════════════════════════════════════════
```

## Rules

- **Never delete, always archive.** Departments and agents are archived, not deleted. History is preserved.
- **Atomic updates only.** All changes to org.json and assignment files must be atomic with backup/verify/restore.
- **Log all changes.** Every organization change is logged to decisions.md for audit trail.
- **Validate before execute.** All operations validate inputs and show confirmation before making changes.
- **Preserve agent memory.** Reassignments and promotions preserve the agent's memory and learnings.
- **One operation per command.** Each invocation performs exactly one reorganization operation.
- **Use company_resolver for paths.** Always use the company_resolver.py utility to find the company root and resolve paths. Never hard-code `.company/` — use `[company_dir]` resolved at runtime.
- **Multi-project operations require multi-project mode.** REASSIGN_PROJECT and TRANSFER_PROJECT operations are only valid when `is_multi_project` is true.
- **Keep assignment history.** When transferring between projects, set `active: false` and `end_date` on the old assignment rather than deleting it.
- **Update both org.json and assignment files.** Project operations must update both the employee's `projectAssignments` array in org.json AND the per-project assignment file in `[company_dir]/assignments/`.
- **Works from any directory.** The command should function correctly from any subdirectory within the company hierarchy.
