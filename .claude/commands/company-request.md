# /company-request — Submit Work to the Company

Submit a natural language request to the virtual company for decomposition, allocation, and execution. This is the primary human-to-company interface.

**Source Tracking:** This command routes all work items through `input_channel.py` to ensure consistent `source="human"` tagging. This enables the system to distinguish human-submitted work from agent-generated tasks for prioritization and reporting.

## Input
$ARGUMENTS

The request should describe work you want done. Examples:
- "Build a REST API for user authentication"
- "Refactor the payment module to use the new gateway"
- "Add dark mode support to the dashboard"

### Optional Flags
- `--project=<project-id>` — Explicitly specify which project this request is for (overrides auto-detection)

## Step 0: Validate Company State and Detect Project

### 0.1 Find Company Root

Determine if operating in multi-project or single-project mode:

```bash
# Using company_resolver.py to find company root
uv run .claude/hooks/company/company_resolver.py find
```

**If no company found (neither multi-project root nor legacy .company/):**
```
## Company Not Initialized

Run `/company-init` first to set up the organizational structure.
Or run `/company-create` to create a multi-project company root.
```
Exit without changes.

### 0.2 Auto-Detect Current Project (Multi-Project Mode)

If in multi-project mode, automatically detect the current project:

```bash
# Get current project context from working directory
uv run .claude/hooks/company/company_resolver.py project
```

This returns:
- `project_id` — Unique identifier for this project
- `project_path` — Path to the project directory
- `company_root` — Path to the company root
- `multi_project_mode` — Whether multi-project mode is active

### 0.3 Handle --project Flag Override

Parse `$ARGUMENTS` for `--project=<project-id>` flag:

```python
# Extract --project flag from arguments
import re
match = re.search(r'--project=(\S+)', arguments)
if match:
    explicit_project_id = match.group(1)
    # Remove the flag from the request text
    request_text = re.sub(r'\s*--project=\S+', '', arguments).strip()
else:
    explicit_project_id = None
    request_text = arguments
```

**Project Resolution Priority:**
1. If `--project=<id>` provided, use that project ID
2. Else if in multi-project mode and current directory is a registered project, use auto-detected project
3. Else if in single-project mode, proceed without project tagging

**If explicit project specified but not found:**
```
## Project Not Found

The specified project does not exist: [project-id]

To see available projects:
  /company-projects

To register this directory as a project:
  /company-add-project .
```
Exit without changes.

### 0.4 Validate Request Provided

**If no request provided (after removing flags):**
```
## Usage

/company-request <your request in natural language> [--project=<project-id>]

Examples:
  /company-request Build a REST API for user authentication
  /company-request Refactor the payment module --project=api-service
  /company-request Add dark mode support to the dashboard

The company will decompose your request, assign it to appropriate agents,
and coordinate execution.

### Multi-Project Mode
In multi-project mode, requests are automatically tagged with the current
project based on your working directory. Use --project to override.

Current project: [auto-detected project or "none detected"]
```
Exit without changes.

## Step 1: Load Context

Read the organizational context:
- `.company/org.json` — organization structure and employee availability
- `.company/config.json` — work allocation configuration
- `.planning/PROJECT.md` — project context (if exists)

### 1.1 Load Project Assignment Data (Multi-Project Mode)

If in multi-project mode with a resolved project:

```bash
# Load project assignment file
cat [company_root]/.company/assignments/[project_id].json
```

This provides:
- `assignments` — List of employees assigned to this project
- `project_name` — Display name for the project
- `metadata` — Project capabilities and configuration

### 1.2 Filter Available Employees by Project

When operating in multi-project mode, filter employees based on project assignments:

```python
# Get employees assigned to this project
def get_project_employees(org_data, project_id):
    """Filter employees to those assigned to the specified project."""
    all_employees = org_data.get('employees', [])

    if project_id is None:
        # Single-project mode: all employees are available
        return all_employees

    # Multi-project mode: filter by project assignment
    return [
        emp for emp in all_employees
        if project_id in emp.get('projectAssignments', [])
        or emp.get('currentProject') == project_id
    ]
```

**If no employees assigned to project:**
```
## Warning: No Employees Assigned to Project

No employees are currently assigned to project: [project_name] ([project_id])

Options:
1. Assign employees to this project:
   /company-assign [employee-id] [project_id]

2. Submit work without project restriction (may be handled by any available employee):
   /company-request "your request" --project=none

3. View available employees:
   /company-status
```

## Step 2: Decompose the Request

Analyze the request and break it into discrete work items. This is the **work decomposition** phase.

### 2.1 Analyze Request Scope

Determine:
- **Type**: feature / bugfix / refactor / documentation / research
- **Complexity**: trivial / standard / complex / epic
- **Departments involved**: engineering / product / design (which are needed?)
- **Cross-cutting concerns**: security / performance / accessibility

### 2.2 Create Work Breakdown

Decompose into a structured work breakdown:

```markdown
## Work Breakdown

**Request:** [original request text]
**Request ID:** REQ-[timestamp]
**Project:** [project_name] ([project_id]) — or "N/A (single-project mode)"
**Type:** [feature/bugfix/refactor/documentation/research]
**Complexity:** [trivial/standard/complex/epic]

### Scope Analysis
[1-2 sentences describing what this request involves]

### Affected Areas
| Area | Impact | Departments |
|------|--------|-------------|
| [module/feature] | [high/medium/low] | [engineering, design, etc.] |

### Work Items

| ID | Title | Department | Dependencies | Effort |
|----|-------|------------|--------------|--------|
| WI-001 | [title] | [dept] | [IDs or none] | S/M/L |
| WI-002 | [title] | [dept] | WI-001 | S/M/L |

### Work Item Details

#### WI-001: [Title]
- **Department:** [engineering/product/design]
- **Project:** [project_id]
- **Description:** [detailed description]
- **Deliverable:** [what completion looks like]
- **Dependencies:** [other work items that must complete first]
- **Acceptance Criteria:**
  - [ ] [Criterion 1]
  - [ ] [Criterion 2]

#### WI-002: [Title]
...

### Execution Waves

**Wave 1 (parallel):** WI-001, WI-003 — independent items
**Wave 2 (parallel):** WI-002, WI-004 — depends on Wave 1
**Wave 3 (sequential):** WI-005 — integration

### Risk Assessment
- [Identified risks and mitigations]

### Estimated Timeline
- [Based on complexity and work item count]
```

## Step 3: Present for Confirmation

Display the work breakdown to the user for approval:

```
════════════════════════════════════════════════════════════════════════════════
 WORK REQUEST ANALYSIS                                           [awaiting approval]
════════════════════════════════════════════════════════════════════════════════

Request: "[original request text]"
Request ID: REQ-[timestamp]
Project: [project_name] ([project_id]) — or "Single-Project Mode"

┌─────────────────────────────────────────────────────────────────────────────┐
│ SUMMARY                                                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│ Project:     [project_name] ([project_id])                                   │
│ Type:        [feature/bugfix/refactor]                                       │
│ Complexity:  [trivial/standard/complex/epic]                                 │
│ Work Items:  [count]                                                         │
│ Waves:       [count]                                                         │
│ Departments: [list]                                                          │
│ Eligible:    [count] employees assigned to this project                      │
└─────────────────────────────────────────────────────────────────────────────┘

### Work Items

| # | Work Item | Department | Depends On | Effort |
|---|-----------|------------|------------|--------|
| 1 | [title]   | Engineering | -          | M      |
| 2 | [title]   | Design      | 1          | S      |
| 3 | [title]   | Engineering | 1, 2       | L      |

### Execution Plan

Wave 1: WI-001 (Engineering)
         └── Parallel execution, no dependencies

Wave 2: WI-002 (Design)
         └── Depends on Wave 1 completion

Wave 3: WI-003 (Engineering)
         └── Integration, depends on all prior waves

### Project-Assigned Employees (Available for This Request)

| Employee | Department | Status | Capabilities |
|----------|------------|--------|--------------|
| [id] | Engineering | available | [skills] |
| [id] | Design | busy | [skills] |

════════════════════════════════════════════════════════════════════════════════

**Proceed with this work breakdown?**
- Type `yes` or `proceed` to allocate and start execution
- Type `no` or `cancel` to abort
- Type `adjust` followed by changes to modify the breakdown
```

**Wait for user confirmation before proceeding.**

## Step 4: Allocate Work to Employees

Once confirmed, perform **work allocation** — assign each work item to employees based on project assignments and capabilities.

**IMPORTANT:** All work items must be submitted through `input_channel.py` to ensure consistent source tracking. This ensures `source="human"` is set for all human-submitted work.

### 4.0 Submit Work Items via Input Channel

For each work item from the work breakdown, submit through the input channel:

```bash
# Submit each work item through the input channel
uv run .claude/hooks/company/input_channel.py submit \
    --title "WI-001: [work item title]" \
    --description "[detailed description from work breakdown]" \
    --priority [1-4] \
    --department [engineering|design|product] \
    --complexity [trivial|standard|complex|epic] \
    --project-id [project_id if in multi-project mode] \
    --requested-by human
```

This ensures:
- All tasks have `source="human"` set automatically
- Tasks flow through the standard work queue
- Consistent tracking across all human-to-company interfaces

### 4.1 Check Employee Availability (Project-Scoped)

In multi-project mode, allocation is **project-scoped**:

```python
def get_eligible_employees(org_data, project_id, department_filter=None):
    """Get employees eligible to work on this project and department."""
    employees = org_data.get('employees', [])

    eligible = []
    for emp in employees:
        # Check project assignment (if multi-project mode)
        if project_id:
            project_assignments = emp.get('projectAssignments', [])
            if project_id not in project_assignments:
                continue

        # Check department filter
        if department_filter and emp.get('department') != department_filter:
            continue

        eligible.append(emp)

    return eligible
```

Read `.company/org.json` to determine:
- Which employees are assigned to the target project
- Employee current status (available/busy/blocked)
- Employee capabilities matching work item requirements

### 4.2 Assign Work Items (Project-Aware)

For each work item:
1. **Filter by project** — Only consider employees assigned to this project (in multi-project mode)
2. **Match capabilities** — Find employees with matching skills
3. **Prefer available** — Prefer available employees over busy ones
4. **Consider specializations** — Weight by past performance on similar work
5. **Fallback to department head** — If no direct match, assign to department head for delegation

**Project assignment is enforced:** Work items tagged with a project_id will ONLY be assigned to employees who have that project in their `projectAssignments` array.

### 4.3 Create Allocation Report

```markdown
## Work Allocation

**Request ID:** REQ-[timestamp]
**Project:** [project_name] ([project_id])
**Allocated:** [timestamp]

### Assignments

| Work Item | Assigned To | Department | Project | Status |
|-----------|-------------|------------|---------|--------|
| WI-001 | [employee-id] or [dept]-head | [dept] | [project_id] | assigned |
| WI-002 | [employee-id] or [dept]-head | [dept] | [project_id] | pending (blocked) |

### Allocation Notes
- [Any notes about assignment decisions]
- [Employees that may need to be assigned to this project]
- [Specialists that should be hired for missing capabilities]
```

### 4.4 Handle Project-Scoped Allocation Failures

**If no eligible employees for a work item:**

```
## Allocation Warning

Work item WI-002 requires the Design department, but no Design employees
are assigned to project [project_id].

**Current project assignments:**
| Employee | Department | Projects |
|----------|------------|----------|
| [id] | Engineering | [project_id], [other] |
| [id] | Design | [other_project] |

**Options:**
1. Assign a Design employee to this project:
   /company-assign [employee-id] [project_id]

2. Proceed with limited allocation (Design work will be queued)

3. Remove project restriction (allow any available employee):
   /company-request "request" --project=none
```

## Step 5: Spawn Coordinator for Execution

Spawn a coordinator agent to orchestrate the work:

```
Task(subagent_type="general-purpose", description="Coordinate work request execution")
```

Pass to the coordinator:
- The work breakdown from Step 2
- The allocation from Step 4
- The original request for context
- Instructions: "You are the coordinator. Read .claude/agents/company/department-head.md for coordination patterns. Delegate work items to assigned agents, track progress, handle blockers, and report completion."

The coordinator's responsibilities:
1. Notify assigned agents/departments of their work items
2. Track progress across all work items
3. Handle blockers and escalations
4. Enforce execution wave dependencies
5. Report completion status

## Step 6: Update Organization State

**Note:** Work items submitted via `input_channel.py` (Step 4.0) are automatically tracked in the work queue with `source="human"`. The following state updates provide additional request-level tracking.

Update `.company/org.json` with the active work request (linking to input channel tasks):

Add to `work.active`:
```json
{
  "id": "REQ-[timestamp]",
  "title": "[brief request summary]",
  "projectId": "[project_id or null for single-project mode]",
  "projectName": "[project_name or null]",
  "status": "in_progress",
  "started": "[ISO timestamp]",
  "source": "human",
  "workItems": [
    {
      "id": "WI-001",
      "taskId": "[task_id from input_channel.py submit response]",
      "title": "[title]",
      "assignee": "[employee-id or dept-head]",
      "projectId": "[project_id]",
      "status": "in_progress",
      "source": "human"
    }
  ],
  "coordinator": "coordinator-[timestamp]"
}
```

Update `work.lastUpdated` to current timestamp.

### 6.1 Update Project Assignment File (Multi-Project Mode)

If in multi-project mode, also update the project's assignment file at `.company/assignments/[project_id].json`:

Add to `activeWork`:
```json
{
  "requestId": "REQ-[timestamp]",
  "title": "[brief request summary]",
  "workItemCount": 3,
  "assignedEmployees": ["emp-001", "emp-002"],
  "started": "[ISO timestamp]"
}
```

This links the work request to the specific project for tracking and reporting.

## Step 7: Display Execution Status

```
════════════════════════════════════════════════════════════════════════════════
 WORK REQUEST SUBMITTED                                              [in progress]
════════════════════════════════════════════════════════════════════════════════

Request ID: REQ-[timestamp]
Project: [project_name] ([project_id]) — or "Single-Project Mode"
Request: "[original request text]"

### Status
┌─────────────────────────────────────────────────────────────────────────────┐
│ EXECUTION STARTED                                                            │
├─────────────────────────────────────────────────────────────────────────────┤
│ Project:      [project_name] ([project_id])                                  │
│ Coordinator:  Active                                                         │
│ Work Items:   [count] assigned                                               │
│ Current Wave: 1 of [total]                                                   │
│ Progress:     ░░░░░░░░░░░░░░░░ 0%                                           │
└─────────────────────────────────────────────────────────────────────────────┘

### Assignments

| Work Item | Employee | Project | Status |
|-----------|----------|---------|--------|
| WI-001: [title] | [employee] | [project_id] | in_progress |
| WI-002: [title] | [employee] | [project_id] | pending |
| WI-003: [title] | [employee] | [project_id] | pending |

════════════════════════════════════════════════════════════════════════════════

### Next Steps
- `/company-status` — Check progress at any time
- `/company-status --project=[project_id]` — Check this project specifically
- `/company-standup` — Get detailed status from all employees
- The coordinator will report when work is complete

Work is now in progress. The coordinator agent is orchestrating execution.
```

## Rules

1. **Always decompose before allocating.** Never assign unanalyzed work directly to employees. The decomposition step ensures clarity and enables wave-based parallelization.

2. **Require confirmation for non-trivial work.** For standard complexity and above, always show the breakdown and wait for user approval before proceeding.

3. **Respect employee availability.** Check `.company/org.json` for employee status. Do not overload busy employees — queue work or escalate if no employees are available.

4. **Track all active work.** Every submitted request must be recorded in `work.active` in org.json. This enables status queries and prevents lost work.

5. **Spawn coordinator, don't become coordinator.** The /company-request command submits work; it does not execute it. A separate coordinator agent handles orchestration.

6. **Handle missing employees gracefully.** If a work item requires capabilities no employee has, either:
   - Assign to department head for delegation
   - Note that a specialist should be hired
   - Do NOT fail silently

7. **Escalate if blocked.** If work cannot be allocated (no company, no employees, all employees busy), report to user with options rather than blocking silently.

8. **Enforce project boundaries in multi-project mode.** Work items tagged with a project_id must ONLY be assigned to employees who have that project in their `projectAssignments` array. This ensures accountability and prevents cross-project confusion.

9. **Auto-detect project from working directory.** In multi-project mode, automatically detect the current project based on the working directory. Allow explicit override via `--project` flag.

10. **Tag all work items with project_id.** In multi-project mode, every work item must include the `projectId` field for tracking, reporting, and filtering.

11. **Route all work through input_channel.py.** All work items must be submitted via `input_channel.py submit` to ensure `source="human"` is set consistently. This enables source tracking across all human-to-company interfaces and distinguishes human work from agent-generated tasks.

## Error Handling

### No Company Initialized
```
## Error: Company Not Initialized

The `.company/` directory does not exist.

For a single-project setup:
  /company-init

For a multi-project company:
  /company-create   (in parent directory)
  /company-add-project .   (in each project)
```

### No Request Provided
Show usage information (see Step 0).

### No Available Employees
```
## Warning: Limited Employee Availability

All employees are currently busy. Options:
1. Queue this request (will execute when employees free up)
2. Wait and try again later
3. Use `/company-hire` to add more employees

Current employee status:
[table of employees and their status]
```

### Department Not Staffed
```
## Warning: Department Not Staffed

Work item WI-002 requires the Design department, but no Design employees exist.

Options:
1. Proceed without design work (if optional)
2. Run `/company-hire --department=design` to add Design employees
3. Modify the request to exclude design work
```

### Project Not Found (Multi-Project Mode)
```
## Error: Project Not Found

The specified project does not exist: [project-id]

**Available projects:**
| Project ID | Name | Path |
|------------|------|------|
| [id] | [name] | [path] |

To register this directory as a project:
  /company-add-project .

To see all projects:
  /company-projects
```

### Project Not Detected
```
## Warning: Project Not Detected

You are in multi-project mode, but the current directory is not a registered project.

**Current directory:** [path]
**Company root:** [company_root]

Options:
1. Register this directory as a project:
   /company-add-project .

2. Specify a project explicitly:
   /company-request "your request" --project=[project-id]

3. Submit without project (work may be assigned to any employee):
   /company-request "your request" --project=none
```

### No Employees Assigned to Project
```
## Warning: No Project Employees

No employees are assigned to project: [project_name] ([project_id])

This project cannot receive work until employees are assigned.

**Available employees (not assigned to this project):**
| Employee | Department | Current Projects |
|----------|------------|------------------|
| [id] | [dept] | [projects] |

To assign an employee:
  /company-assign [employee-id] [project_id]

To submit without project restriction:
  /company-request "your request" --project=none
```
