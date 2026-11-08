# /company-route — Cross-Project Task Routing

Route tasks between projects in a multi-project company. Validates employee access before routing and maintains routing history for audit purposes.

## Input
$ARGUMENTS

Usage:
- `/company-route [task-id] [target-project] "reason"` — Route a task to another project
- `/company-route --suggest [task-id]` — Show routing recommendations for a task
- `/company-route --list` — Show routing history for cross-project tasks
- `/company-route --dry-run [task-id] [target-project]` — Preview what would happen without executing

Examples:
- `/company-route task-20260212-abc123 api-service "Requires backend expertise"`
- `/company-route --suggest task-20260212-abc123`
- `/company-route --list`
- `/company-route --dry-run task-20260212-abc123 frontend-app`

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
- `$COMPANY_DIR`: Path to `.company/` directory

## Step 0.1: Validate Multi-Project Mode

**If SINGLE_PROJECT mode:**
```
## Single-Project Mode

Cross-project task routing is only available in multi-project mode.

To set up a multi-project company:
  /company-create   (in parent directory)
  /company-add-project .   (in each project)

Or to upgrade this project to multi-project mode:
  /company-upgrade
```

Exit without changes.

## Step 0.2: Validate Company Structure

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

### 1.1 Suggest Mode (--suggest flag)

If `--suggest` flag is present:
- Extract the task ID following the flag
- Set operation to "suggest"

### 1.2 List Mode (--list flag)

If `--list` flag is present:
- Set operation to "list"

### 1.3 Dry-Run Mode (--dry-run flag)

If `--dry-run` flag is present:
- Extract task-id and target-project following the flag
- Set operation to "dry-run"

### 1.4 Route Mode (default)

If no flag is present:
- First argument: task-id
- Second argument: target-project
- Remaining text (in quotes): reason
- Set operation to "route"

**If required arguments are missing for route/dry-run:**

```
## Missing Arguments

Usage: /company-route [task-id] [target-project] "reason"
       /company-route --suggest [task-id]
       /company-route --list
       /company-route --dry-run [task-id] [target-project]

Examples:
  /company-route task-20260212-abc123 api-service "Requires backend expertise"
  /company-route --suggest task-20260212-abc123
  /company-route --dry-run task-20260212-abc123 frontend-app

To see available tasks:
  uv run .claude/hooks/company/work_allocator.py list --all-projects

To see available projects:
  /company-projects
```

Exit without changes.

**If task-id missing for suggest mode:**

```
## Missing Task ID

Usage: /company-route --suggest [task-id]

Example:
  /company-route --suggest task-20260212-abc123

To see available tasks:
  uv run .claude/hooks/company/work_allocator.py list --all-projects
```

Exit without changes.

## Step 2: Load Context

### 2.1 Load Organization Data

Read the organization file:

```bash
cat $COMPANY_DIR/org.json
```

Parse the JSON and store:
- `$EMPLOYEES`: Array of employee objects
- `$PROJECTS`: Array of project objects

### 2.2 Get Current Employee Context

Determine the employee requesting the route. Use the current session context or default to the first available employee with multi-project access:

```bash
uv run .claude/hooks/company/company_resolver.py project 2>/dev/null
```

Store:
- `$CURRENT_PROJECT_ID`: Current project context
- `$EMPLOYEE_ID`: Employee ID for access validation (use session context or default)

## Step 3: Handle List Operation

If operation is "list", execute:

```bash
uv run .claude/hooks/company/work_allocator.py cross-project
```

Parse the response and display:

```
## Cross-Project Task Routing History

=====================================================================
 ROUTING HISTORY                                              [list]
=====================================================================

### Active Cross-Project Tasks

| Task ID | Title | Source Project | Current Project | Status |
|---------|-------|----------------|-----------------|--------|
| [task_id] | [title] | [source_project_id] | [target_project_id] | [status] |
...

### Routing Events

| Task ID | From | To | Routed By | Reason | When |
|---------|------|----|-----------|--------|------|
| [task_id] | [from_project] | [to_project] | [employee_id] | [reason] | [timestamp] |
...

### Summary

| Metric | Count |
|--------|-------|
| Total Cross-Project Tasks | X |
| Active | X |
| Completed | X |
| Blocked | X |

=====================================================================

### Quick Commands

- `/company-route [task-id] [target-project] "reason"` — Route a task
- `/company-route --suggest [task-id]` — Get routing suggestions
- `/company-projects` — List all projects
```

**If no cross-project tasks:**

```
## Cross-Project Task Routing History

=====================================================================
 ROUTING HISTORY                                              [empty]
=====================================================================

No cross-project tasks found.

Cross-project tasks are created when:
- A task is explicitly routed to another project
- A task has dependencies across projects
- A task is shared between multiple projects

### Getting Started

To route a task to another project:
  /company-route [task-id] [target-project] "reason"

To see available tasks:
  uv run .claude/hooks/company/work_allocator.py list --all-projects

To see available projects:
  /company-projects

=====================================================================
```

Exit without changes.

## Step 4: Handle Suggest Operation

If operation is "suggest":

### 4.1 Get Task Details

```bash
uv run .claude/hooks/company/work_allocator.py get --task-id [task-id]
```

**If task not found:**
```
## Task Not Found

No task with ID "[task-id]" exists in the work queue.

To see available tasks:
  uv run .claude/hooks/company/work_allocator.py list --all-projects

To search for a task by title:
  uv run .claude/hooks/company/work_allocator.py list --status pending
```

Exit without changes.

### 4.2 Get Routing Suggestions

```bash
uv run .claude/hooks/company/project_orchestrator.py suggest --task-id [task-id]
```

Parse the response and display:

```
## Routing Suggestions

=====================================================================
 SUGGESTIONS                                            [task-id]
=====================================================================
 Task: [task_title]
 Current Project: [current_project_id or "unassigned"]
 Status: [status]
=====================================================================

### Recommended Projects

| Rank | Project | Confidence | Reasons |
|------|---------|------------|---------|
| 1 | [project_id] ([project_name]) | [confidence]% | [reasons] |
| 2 | [project_id] ([project_name]) | [confidence]% | [reasons] |
| 3 | [project_id] ([project_name]) | [confidence]% | [reasons] |

### Task Details

| Field | Value |
|-------|-------|
| Task ID | [task_id] |
| Title | [title] |
| Description | [description] |
| Required Capabilities | [capabilities] |
| Department | [department] |
| Priority | [priority] |
| Complexity | [complexity] |

### Project Capabilities Comparison

| Project | Tech Stack | Domains | Employees |
|---------|------------|---------|-----------|
| [project_id] | [tech_stack] | [domains] | [count] |
...

=====================================================================

### Next Steps

To route this task:
  /company-route [task-id] [suggested-project] "Based on [reasons]"

To preview the route:
  /company-route --dry-run [task-id] [suggested-project]

=====================================================================
```

**If no suggestions found:**

```
## Routing Suggestions

=====================================================================
 SUGGESTIONS                                            [task-id]
=====================================================================
 Task: [task_title]
 Current Project: [current_project_id or "unassigned"]
=====================================================================

### No Strong Matches Found

The task content does not strongly match any specific project's tech stack
or domain areas.

**Possible reasons:**
- Task is generic and could fit multiple projects
- Task uses technologies not registered with any project
- Task description lacks specific technical keywords

### All Projects

| Project | Tech Stack | Employees | Status |
|---------|------------|-----------|--------|
| [project_id] | [tech_stack] | [count] | [status] |
...

### Manual Assignment

You can manually route the task:
  /company-route [task-id] [project-id] "Manual assignment: [reason]"

=====================================================================
```

Exit without changes.

## Step 5: Handle Dry-Run Operation

If operation is "dry-run":

### 5.1 Validate Task Exists

```bash
uv run .claude/hooks/company/work_allocator.py get --task-id [task-id]
```

**If task not found:**
```
## Task Not Found

No task with ID "[task-id]" exists in the work queue.
```

Exit without changes.

### 5.2 Validate Target Project Exists

Check if target-project is in `$PROJECTS`:

**If project not found:**
```
## Project Not Found

No project with ID "[target-project]" is registered.

**Available Projects:**
| ID | Name | Path |
|----|------|------|
| [id] | [name] | [path] |
...

To list all projects:
  /company-projects
```

Exit without changes.

### 5.3 Validate Employee Access

```bash
uv run .claude/hooks/company/project_orchestrator.py validate-access \
    --employee-id [employee-id] \
    --target-project [target-project]
```

### 5.4 Display Dry-Run Results

```
## Dry-Run: Task Routing Preview

=====================================================================
 DRY-RUN                                               [no changes]
=====================================================================

### Routing Details

| Field | Value |
|-------|-------|
| Task ID | [task-id] |
| Task Title | [title] |
| Source Project | [source_project_id or "unassigned"] |
| Target Project | [target-project] |
| Requested By | [employee-id] |

### Access Validation

| Check | Result |
|-------|--------|
| Task exists | PASS |
| Target project exists | PASS |
| Employee has access | [PASS/FAIL] |

### What Would Happen

If executed, this routing would:
1. Set task's `target_project_id` to "[target-project]"
2. Mark task as `cross_project = true`
3. Add routing event to task's `routing_history`
4. Task would become visible to employees assigned to [target-project]
5. Task would remain in current status: [status]

### Affected Employees

**Source Project Employees:** (would no longer see task unless also assigned to target)
| Employee | Department | Status |
|----------|------------|--------|
| [id] | [dept] | [status] |
...

**Target Project Employees:** (would now see task)
| Employee | Department | Status |
|----------|------------|--------|
| [id] | [dept] | [status] |
...

=====================================================================

### Execute Routing?

To execute this routing:
  /company-route [task-id] [target-project] "your reason here"

=====================================================================
```

**If access validation fails:**

```
## Dry-Run: Access Denied

=====================================================================
 DRY-RUN                                               [would fail]
=====================================================================

### Access Validation Failed

Employee "[employee-id]" does not have access to project "[target-project]".

| Check | Result |
|-------|--------|
| Task exists | PASS |
| Target project exists | PASS |
| Employee has access | FAIL |

### Employee's Current Assignments

| Project ID | Project Name |
|------------|--------------|
| [project_id] | [project_name] |
...

### Resolution Options

1. **Assign employee to target project first:**
   /company-assign [employee-id] [target-project]

2. **Use a different employee who has access:**
   /company-status --project=[target-project]

3. **Route from target project context:**
   cd [target-project-path]
   /company-route [task-id] [target-project] "reason"

=====================================================================
```

Exit without changes.

## Step 6: Execute Route Operation

If operation is "route":

### 6.1 Validate Task Exists

```bash
uv run .claude/hooks/company/work_allocator.py get --task-id [task-id]
```

**If task not found:**
```
## Task Not Found

No task with ID "[task-id]" exists in the work queue.

To see available tasks:
  uv run .claude/hooks/company/work_allocator.py list --all-projects
```

Exit without changes.

### 6.2 Validate Target Project Exists

Search for target-project in `$PROJECTS`:

**If not found:**
```
## Project Not Found

No project with ID "[target-project]" is registered.

**Available Projects:**
| ID | Name | Path |
|----|------|------|
| [id] | [name] | [path] |
...

To register a new project:
  /company-add-project [path]
```

Exit without changes.

### 6.3 Validate Employee Access

```bash
uv run .claude/hooks/company/project_orchestrator.py validate-access \
    --employee-id [employee-id] \
    --target-project [target-project]
```

**If access denied:**

```
## Access Denied

=====================================================================
 ROUTING FAILED                                              [error]
=====================================================================

Employee "[employee-id]" does not have access to project "[target-project]".

### Access Requirements

To route tasks to a project, you must be assigned to that project.

### Resolution Options

1. **Get assigned to the target project:**
   /company-assign [employee-id] [target-project]

2. **Have an authorized employee route the task:**
   Ask someone assigned to [target-project] to execute the routing.

3. **View project assignments:**
   /company-status --project=[target-project]

=====================================================================
```

Exit without changes.

### 6.4 Execute the Route

```bash
uv run .claude/hooks/company/work_allocator.py route \
    --task-id [task-id] \
    --target-project-id [target-project] \
    --employee-id [employee-id] \
    --reason "[reason]"
```

### 6.5 Display Success Output

```
## Task Routed Successfully

=====================================================================
 ROUTING COMPLETE                                          [success]
=====================================================================
 Task: [task-id]
 From: [source_project_id or "unassigned"] -> To: [target-project]
=====================================================================

### Routing Details

| Field | Value |
|-------|-------|
| Task ID | [task-id] |
| Task Title | [title] |
| Source Project | [source_project_id or "unassigned"] |
| Target Project | [target-project] ([project_name]) |
| Routed By | [employee-id] |
| Reason | [reason] |
| Routed At | [timestamp] |

### Task Status

| Field | Value |
|-------|-------|
| Status | [status] |
| Priority | [priority] |
| Assigned To | [assignee or "unassigned"] |
| Cross-Project | Yes |

### Routing History

| # | From | To | By | Reason | When |
|---|------|----|-------|--------|------|
| 1 | [from] | [to] | [by] | [reason] | [when] |
...

### Eligible Employees (Target Project)

| Employee | Department | Status | Capabilities |
|----------|------------|--------|--------------|
| [id] | [dept] | [status] | [capabilities] |
...

=====================================================================

### Next Steps

1. **View task details:**
   uv run .claude/hooks/company/work_allocator.py get --task-id [task-id]

2. **Assign task to an employee:**
   uv run .claude/hooks/company/work_allocator.py pull --agent-id [employee-id] --capabilities "[caps]"

3. **View target project status:**
   /company-status --project=[target-project]

4. **View all cross-project tasks:**
   /company-route --list

=====================================================================
```

**If route operation fails:**

```
## Routing Failed

=====================================================================
 ROUTING FAILED                                              [error]
=====================================================================

Failed to route task "[task-id]" to project "[target-project]".

**Error:** [error message from work_allocator]

### Troubleshooting

1. Verify the task exists and is in a routable status:
   uv run .claude/hooks/company/work_allocator.py get --task-id [task-id]

2. Verify the target project is registered:
   /company-projects

3. Check your access to the target project:
   /company-assign --list [employee-id]

4. Try a dry-run to diagnose:
   /company-route --dry-run [task-id] [target-project]

=====================================================================
```

## Rules

1. **Always validate access before routing.** Never route a task to a project the requesting employee doesn't have access to. This ensures accountability and prevents unauthorized cross-project work.

2. **Preserve routing history.** Every route operation adds to the task's `routing_history` array. This provides audit trail for compliance and debugging.

3. **Dry-run shows full impact.** The `--dry-run` option must show exactly what would change, including affected employees, without making any modifications.

4. **Suggest uses project capabilities.** Routing suggestions are based on matching task requirements against project tech stacks and domains.

5. **List shows all cross-project work.** The `--list` option shows both active and completed cross-project tasks with full routing history.

6. **Access validation is enforced.** Even if the work_allocator route function has its own validation, the command should validate first to provide better error messages.

7. **Reason is required for audit.** All route operations require a reason to maintain clear accountability in the routing history.

8. **Multi-project mode required.** Cross-project routing is only meaningful in multi-project mode. In single-project mode, provide clear guidance on upgrading.

9. **Show eligible employees after routing.** After a successful route, show which employees in the target project can now work on the task.

10. **Graceful handling of edge cases.** Handle missing tasks, invalid projects, and access denied with clear, actionable error messages.
