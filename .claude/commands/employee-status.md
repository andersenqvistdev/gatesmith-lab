# /employee-status — View Individual Employee or Workforce Status

Display status of individual employees or the entire workforce, including current assignments, workload, and activity history.

## Input
$ARGUMENTS

Optional arguments:
- `[employee-id]` — View specific employee details
- `--department [id]` — Filter by department (engineering, product, design)
- `--idle` — Show only idle employees (no active tasks)
- `--overloaded` — Show overloaded employees (>2 active tasks)
- `--project [id]` — Filter employees by project assignment

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

## Step 0.1: Check Company Exists

Check if the resolved `.company/` directory exists:

```bash
ls -la [company_dir]/ 2>/dev/null
```

**If not exists:**
```
## Company Not Initialized

No company directory found at [company_dir].

To initialize the company structure, run:
  /company-init

This will create the organizational structure needed for workforce management.
```

Exit without further processing.

## Step 1: Load Organization Data

Read the following files from `[company_dir]`:

```bash
cat [company_dir]/org.json
cat [company_dir]/work_queue.json 2>/dev/null || echo '{"pending":[],"in_progress":[],"blocked":[]}'
```

Parse the JSON and extract:
- `employees` — Employee list (or `agents` for backward compatibility)
- `departments` — Department structure with teams
- `work.active` — Active work items from org.json
- `in_progress` — In-progress tasks from work_queue.json

## Step 2: Determine View Mode

Parse `$ARGUMENTS` to determine the display mode:

### 2.1 Individual Employee View

If an employee ID is provided (first argument without `--`):
- Set mode to "individual"
- Store employee-id for lookup

### 2.2 Filtered List View

If filter flags are present:
- `--department [id]` — Filter by department
- `--idle` — Show only idle employees
- `--overloaded` — Show only overloaded employees (>2 active tasks)
- `--project [id]` — Show only employees assigned to project

### 2.3 Summary View (Default)

If no arguments provided:
- Show all employees grouped by department

## Step 3: Gather Employee Data

For each employee in the list:

### 3.1 Get Current Status

Determine employee status from work_queue.json and org.json:

```
Status Logic:
- Active: Has in_progress tasks
- Blocked: Has blocked tasks and no in_progress tasks
- Idle: No active, blocked, or pending tasks
- Error: status == "offline" or configuration issue
```

### 3.2 Get Current Task Assignment

From work_queue.json, find tasks where `assigned_to` matches employee ID:

```json
{
  "task_id": "...",
  "title": "...",
  "started_at": "ISO timestamp",
  "status": "in_progress"
}
```

### 3.3 Calculate Time on Task

If `started_at` is available:

```python
duration = now - started_at
format as: "Xh Ym" (e.g., "2h 15m")
```

### 3.4 Get Employee Memory (Individual View Only)

For individual employee view, read the employee's memory file:

**Multi-project mode:**
```bash
cat [company_dir]/employees/[department]/[employee-id].md
```

**Single-project mode:**
```bash
cat [company_dir]/agents/[employee-id]/memory.md
```

Parse the memory file to extract:
- Current context notes
- Active assignments table
- Learnings section
- Preferences section

## Step 4: Calculate Workload Assessment

For each employee, calculate workload metrics:

### 4.1 Current Workload

Count active tasks:
- `in_progress` tasks assigned to employee
- `blocked` tasks assigned to employee

### 4.2 Historical Metrics (from metrics_tracker.py)

```bash
uv run .claude/hooks/company/metrics_tracker.py report --days 7 2>/dev/null | jq '.report'
```

From the report, extract per-employee statistics:
- Tasks completed in last 7 days
- Average task duration
- Patterns captured (from learnings)

### 4.3 Workload Status

Based on current vs historical workload:

| Current | vs 7-day Avg | Status |
|---------|--------------|--------|
| 0 tasks | < 0.5x | Idle |
| 1-2 tasks | 0.5x - 1.5x | Normal |
| 2-3 tasks | 1.5x - 2x | Heavy |
| >3 tasks | > 2x | Overloaded |

## Step 5: Apply Filters

If filter flags are present, filter the employee list:

### --department [id]
```python
employees = [e for e in employees if e['department'] == department_id]
```

### --idle
```python
employees = [e for e in employees if active_task_count(e) == 0]
```

### --overloaded
```python
employees = [e for e in employees if active_task_count(e) > 2]
```

### --project [id]
```python
employees = [e for e in employees if project_id in e.get('projectAssignments', [])]
```

## Step 6: Render Output

### 6.1 Summary View (All Employees)

```
===============================================================
  WORKFORCE STATUS | [N] employees
===============================================================

ENGINEERING ([N])
  [status] [employee-id]     [Status]   [[task-title]]     [duration]
  [status] [employee-id]     [Status]   [[task-title]]     [duration]
  ...

PRODUCT ([N])
  [status] [employee-id]     [Status]   [[task-title]]     [duration]
  ...

DESIGN ([N])
  [status] [employee-id]     [Status]   [[task-title]]     [duration]
  ...

Legend: * Active  o Idle  ! Blocked  x Error
===============================================================
```

**Status Indicators:**
- `*` = Active (currently working on task)
- `o` = Idle (no active tasks)
- `!` = Blocked (waiting on dependencies)
- `x` = Error (offline or configuration issue)

**Example:**
```
===============================================================
  WORKFORCE STATUS | 6 employees
===============================================================

ENGINEERING (4)
  * senior-engineer-1    Active   [auth-refactor]     2h 15m
  * backend-dev-1        Active   [api-endpoints]     45m
  o frontend-dev-1       Idle     -                   -
  o devops-1             Idle     -                   -

DESIGN (1)
  * ux-designer-1        Active   [onboarding-flow]   1h 30m

QA (1)
  * qa-engineer-1        Active   [test-coverage]     3h 10m

Legend: * Active  o Idle  ! Blocked  x Error
===============================================================
```

### 6.2 Individual Employee View

```
===============================================================
  EMPLOYEE: [employee-id]
===============================================================

Status: [Active | Idle | Blocked | Offline]
Department: [department-name]
Team: [team-name or "Unassigned"]
Current Task: [task-title] ([duration]) or "None"

CAPABILITIES
* [capability-1]
* [capability-2]
* [capability-3]

RECENT ACTIVITY (7 days)
* Tasks completed: [N]
* Patterns captured: [N]
* Learnings added: [N]
* Avg task duration: [Xm]

KNOWLEDGE CONTRIBUTIONS
* Pattern: "[pattern-title]"
* Pattern: "[pattern-title]"
* Learning: "[learning-summary]"

WORKLOAD ASSESSMENT
* Current: [Idle | Normal | Heavy | Overloaded]
* 7-day avg: [X.X] tasks/day
* Trend: [Stable | Increasing | Decreasing]
===============================================================
```

**Example:**
```
===============================================================
  EMPLOYEE: senior-engineer-1
===============================================================

Status: Active
Department: Engineering
Team: Backend
Current Task: auth-refactor (2h 15m)

CAPABILITIES
* python, go, postgres, redis, security

RECENT ACTIVITY (7 days)
* Tasks completed: 12
* Patterns captured: 3
* Learnings added: 5
* Avg task duration: 45m

KNOWLEDGE CONTRIBUTIONS
* Pattern: "JWT refresh token rotation"
* Pattern: "Rate limiting with Redis"
* Learning: "Use connection pooling for Postgres"

WORKLOAD ASSESSMENT
* Current: Normal
* 7-day avg: 4.2 tasks/day
* Trend: Stable
===============================================================
```

### 6.3 Filtered View

When filters are applied, use the summary format but show only matching employees:

```
===============================================================
  WORKFORCE STATUS | [N] idle employees
===============================================================
  (filtered by: --idle)

ENGINEERING ([N])
  o frontend-dev-1       Idle     -                   -
  o devops-1             Idle     -                   -

Legend: * Active  o Idle  ! Blocked  x Error
===============================================================
```

### 6.4 Department View

When `--department [id]` is used:

```
===============================================================
  DEPARTMENT: Engineering | [N] employees
===============================================================

TEAMS
+----------------+----------+--------+
| Team           | Members  | Active |
+----------------+----------+--------+
| Core Platform  | 3        | 2      |
| Integrations   | 2        | 1      |
| DevOps         | 2        | 0      |
+----------------+----------+--------+

EMPLOYEES
  * senior-engineer-1    Core      Active   [auth-refactor]     2h 15m
  * backend-dev-1        Core      Active   [api-endpoints]     45m
  o frontend-dev-1       Core      Idle     -                   -
  * integrations-dev-1   Int       Active   [api-sync]          1h 20m
  o devops-1             DevOps    Idle     -                   -
  o devops-2             DevOps    Idle     -                   -

WORKLOAD SUMMARY
* Total Tasks Active: [N]
* Average per Employee: [X.X]
* Department Status: [Normal | Strained | Overloaded]

Legend: * Active  o Idle  ! Blocked  x Error
===============================================================
```

## Step 7: Handle Edge Cases

### No Employees

```
===============================================================
  WORKFORCE STATUS | 0 employees
===============================================================

No employees have been hired yet.

To hire employees, use:
  /company-hire [role description] --department=[dept]

Examples:
  /company-hire backend engineer --department=engineering
  /company-hire ux researcher --department=design

Available departments:
  - engineering
  - product
  - design
===============================================================
```

### Employee Not Found

```
## Employee Not Found

No employee with ID "[employee-id]" exists in the organization.

**Available Employees:**
| ID | Name | Department | Status |
|----|------|------------|--------|
| [id] | [name] | [dept] | [status] |
...

Use `/employee-status` without arguments to see all employees.
```

### Department Not Found

```
## Department Not Found

No department with ID "[department-id]" exists.

**Available Departments:**
- engineering (Engineering)
- product (Product)
- design (Design)

Use `/employee-status --department=engineering` with a valid department.
```

### No Work Queue Data

If work_queue.json is missing or empty:

```
Note: Work queue data unavailable. Employee status may be incomplete.
Task assignments are showing as "Unknown".

To initialize the work queue, assign tasks using:
  /company-request "[task description]"
```

## Step 8: Display Quick Actions

At the end of any view, show relevant quick actions:

### Summary View Actions
```
### Quick Commands
- `/employee-status [id]` - View specific employee
- `/employee-status --idle` - Show idle employees
- `/employee-status --overloaded` - Show overloaded employees
- `/company-assign [task] --to=[id]` - Assign work
- `/company-hire` - Hire new employee
```

### Individual View Actions
```
### Quick Commands
- `/company-assign [task] --to=[employee-id]` - Assign work
- `/company-status` - View full company status
- `/employee-status` - View all employees
```

### Filtered View Actions
```
### Quick Commands (for idle employees)
- `/company-assign [task] --to=[id]` - Assign work to idle employee
- `/company-request [task]` - Submit task for automatic assignment
```

## Rules

1. **Handle missing files gracefully.** If work_queue.json or memory files are missing, show appropriate defaults and continue.

2. **Calculate durations accurately.** Use ISO 8601 timestamps and format durations as "Xh Ym" (hours and minutes).

3. **Backward compatibility.** Check for both `employees` and `agents` arrays in org.json.

4. **Use status indicators consistently.** Always use the same symbols for status across all views.

5. **Show useful defaults.** When data is unavailable, show "Unknown" or "-" rather than errors.

6. **Filter efficiently.** When multiple filters are specified, apply all of them (AND logic).

7. **Group by department.** In summary views, always group employees by department for clarity.

8. **Include workload context.** When showing employee status, include workload assessment to help with task assignment decisions.

9. **Respect multi-project mode.** When in multi-project mode, employees may be assigned to multiple projects. Show all relevant assignments.

10. **Format time consistently.** Always show duration as "Xh Ym" format. For durations under 1 hour, show "0h Xm".

11. **Show patterns and learnings.** In individual view, extract and display knowledge contributions to help understand employee expertise.

12. **Provide actionable suggestions.** Based on the view (idle employees, overloaded, etc.), suggest relevant next steps.
