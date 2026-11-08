# /dashboard — Quick Operational Dashboard

Display a quick operational health snapshot showing company health, progress, workforce, and alerts.

## Input
$ARGUMENTS

Optional arguments:

**View Mode Flags (Scope):**
- `--unified` — Company-wide aggregated view (all projects combined)
- `--projects` — All projects summary table
- `--compare` — Side-by-side project comparison
- `--project [id]` — Specific project dashboard

**Section Flags (Filter):**
- `--health` — Show health section only
- `--progress` — Show progress section only
- `--agents` — Show workforce/agent status only
- `--alerts` — Show active alerts only

**Auto-Detection (default behavior):**
- In project directory: Show project dashboard
- In company root: Show unified dashboard

---

## Step 0: Parse Arguments and Detect Context

### Parse Command Arguments

Parse `$ARGUMENTS` to determine:
- `view_mode` — One of: `unified`, `projects`, `compare`, `project`, or `auto` (default)
- `section_filter` — One of: `health`, `progress`, `agents`, `alerts`, or `all` (default)
- `project_id` — Specific project ID (if `--project [id]` was used)

### Detect Company Mode

Use the company_resolver to find the company root and determine operating mode:

```bash
# Find company root (searches upward for .forge-company-root)
uv run .claude/hooks/company/company_resolver.py find

# Check if in multi-project mode
uv run .claude/hooks/company/company_resolver.py mode

# Get current project context (if in multi-project mode)
uv run .claude/hooks/company/company_resolver.py project
```

Store the results:
- `company_root` — Path to company root (multi-project) or current directory (legacy)
- `company_dir` — Path to `.company/` directory
- `is_multi_project` — Boolean indicating multi-project mode
- `current_project` — Current project context (if multi-project)

### Auto-Detection Logic

If `view_mode` is `auto` (no explicit flag provided):

```python
if is_multi_project:
    if current_project:
        # We're inside a project directory
        view_mode = "project"
        project_id = current_project["id"]
    else:
        # We're at company root
        view_mode = "unified"
else:
    # Single project mode (legacy)
    view_mode = "project"
```

### Check Company Exists

```bash
ls [company_dir]/org.json 2>/dev/null
```

**If not exists:**
```
No company initialized. Run /company-init or /company-bootstrap first.
```
Exit without further processing.

---

## Step 1: Gather Data

Fetch dashboard data based on `view_mode`:

### For `view_mode = unified` (Company-Wide)

```bash
# Get unified dashboard data (aggregates all projects)
uv run .claude/hooks/company/dashboard_aggregator.py unified

# Get active alerts
uv run .claude/hooks/company/alert_rules.py list
```

### For `view_mode = projects` (All Projects Table)

```bash
# Get all projects summary data
uv run .claude/hooks/company/dashboard_aggregator.py all-projects

# Get active alerts
uv run .claude/hooks/company/alert_rules.py list
```

### For `view_mode = compare` (Side-by-Side)

```bash
# Get project comparison data
uv run .claude/hooks/company/dashboard_aggregator.py compare

# Get active alerts
uv run .claude/hooks/company/alert_rules.py list
```

### For `view_mode = project` (Single Project)

```bash
# Get full dashboard data for specific project
uv run .claude/hooks/company/dashboard_aggregator.py full --project [project_id]

# Get active alerts for project
uv run .claude/hooks/company/alert_rules.py list --project [project_id]
```

### Parse JSON Output

Parse the JSON output and extract:
- `health` — Health score and factors
- `progress` — Task completion and velocity
- `workforce` — Agent counts and utilization
- `risks` — Identified risks
- `alerts` — Active alerts with severity
- `projects` — Project list (for unified/projects/compare modes)
- `comparison` — Comparison data (for compare mode)

Also read company metadata:
```bash
# Get company name from org.json
cat [company_dir]/org.json
```

Extract `company_name` from the organization config.

---

## Step 2: Render Dashboard

Rendering depends on `view_mode`. Skip to the appropriate section:
- `unified` → Unified Company Dashboard
- `projects` → All Projects Summary
- `compare` → Project Comparison View
- `project` → Single Project Dashboard (existing full dashboard)

---

### Unified Company Dashboard (`--unified` flag or auto-detected at company root)

```
===============================================================
  GATESMITH UNIFIED DASHBOARD
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
  Mode: Company-Wide Aggregation ([project_count] projects)
===============================================================

[COMPANY HEALTH] Score: XX/100 [health_bar] ([health_status])
  Aggregated across [project_count] projects
  [bullet_points_from_aggregated_health_factors]

[COMPANY PROGRESS]
  Total: XX | Done: XX | In Progress: XX | Blocked: XX
  [progress_bar] XX% complete
  Company Velocity: X.X tasks/day
  Est. completion: YYYY-MM-DD (all projects)

[PROJECT ROLLUP]
| Project | Health | Progress | Active | Blocked | Status |
|---------|--------|----------|--------|---------|--------|
| [proj_id] | XX/100 | XX% | X | X | [status] |
| [proj_id] | XX/100 | XX% | X | X | [status] |
| ... | ... | ... | ... | ... | ... |

[WORKFORCE] X employees across all projects
  Active: X | Idle: X | Utilization: XX%
  By Department: [department_breakdown]

[ALERTS] X active (company-wide)
  [list_of_alerts_with_project_context]

===============================================================
Tip: Use /dashboard --project [id] for project-specific view
===============================================================
```

---

### All Projects Summary (`--projects` flag)

```
===============================================================
  GATESMITH DASHBOARD: ALL PROJECTS
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[PROJECTS OVERVIEW] X total projects

| Project | Health | Completion | Active | Blocked | Velocity | Status |
|---------|--------|------------|--------|---------|----------|--------|
| [id] | XX/100 | XX% (Y/Z) | X | X | X.X/day | [emoji] |
| [id] | XX/100 | XX% (Y/Z) | X | X | X.X/day | [emoji] |
| ... | ... | ... | ... | ... | ... | ... |

Status Legend: [checkmark] Healthy | [warning] Warning | [x] Critical | [dash] Idle

[TOTALS]
  Total Tasks: XX | Completed: XX | In Progress: XX | Blocked: XX
  Average Health: XX/100 | Average Completion: XX%

[TOP CONCERNS]
| Project | Issue | Severity | Action |
|---------|-------|----------|--------|
| [id] | [description] | [CRITICAL/WARNING] | [recommendation] |

===============================================================
Tip: Use /dashboard --project [id] for details on specific project
===============================================================
```

---

### Project Comparison View (`--compare` flag)

```
===============================================================
  GATESMITH DASHBOARD: PROJECT COMPARISON
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[SIDE-BY-SIDE COMPARISON]

                    | [Project A]    | [Project B]    | [Project C]    |
--------------------|----------------|----------------|----------------|
Health Score        | XX/100 [bar]   | XX/100 [bar]   | XX/100 [bar]   |
Completion          | XX% (Y/Z)      | XX% (Y/Z)      | XX% (Y/Z)      |
Active Tasks        | X              | X              | X              |
Blocked Tasks       | X              | X              | X              |
Velocity (tasks/day)| X.X            | X.X            | X.X            |
Utilization         | XX%            | XX%            | XX%            |
Est. Completion     | YYYY-MM-DD     | YYYY-MM-DD     | YYYY-MM-DD     |
Status              | [status]       | [status]       | [status]       |

[COMPARATIVE ANALYSIS]
- Best Health: [project_id] (XX/100)
- Best Progress: [project_id] (XX%)
- Highest Velocity: [project_id] (X.X tasks/day)
- Most Blocked: [project_id] (X tasks)
- Needs Attention: [project_id] — [reason]

[RESOURCE DISTRIBUTION]
| Project | Employees | Active | Utilization |
|---------|-----------|--------|-------------|
| [id] | X | X | XX% |
| [id] | X | X | XX% |

===============================================================
Tip: Use /dashboard --unified for aggregated company metrics
===============================================================
```

---

### Single Project Dashboard (`--project [id]` flag or auto-detected in project dir)

When showing a specific project (either via `--project [id]` or auto-detected when running from within a project directory):

Generate the ASCII dashboard with all sections:

```
===============================================================
  GATESMITH PROJECT DASHBOARD
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
  Project: [project_name] ([project_id])
===============================================================

[HEALTH] Score: XX/100 [health_bar] ([health_status])
  [bullet_points_from_health_factors]

[PROGRESS]
  Total: XX | Done: XX | In Progress: XX | Blocked: XX
  [progress_bar] XX% complete
  Est. completion: YYYY-MM-DD (based on current velocity)

[WORKFORCE] X employees
  Active: X ([department_breakdown])
  Idle: X
  Utilization: XX%

[AUTONOMY] (from autonomy_audit; if not available: "not calibrated — /calibrate")
  Claimed: XX% | Verified: XX% | Trust: XX% | Phantom: XX% (N) | build [build_sha]

[ALERTS] X active
  [list_of_alerts]

===============================================================
Tip: Use /dashboard --unified for company-wide view
===============================================================
```

---

### Section Filters (Combinable with View Modes)

Section filters (`--health`, `--progress`, `--agents`, `--alerts`) can be combined with any view mode:

```bash
/dashboard --unified --health      # Company-wide health only
/dashboard --projects --progress   # All projects progress summary
/dashboard --project abc --alerts  # Project abc alerts only
```

When a section filter is applied to a unified or projects view, show that section with company-wide or multi-project context.

### Health Section Only (`--health` flag)

If `--health` flag is present, show only:

```
===============================================================
  GATESMITH DASHBOARD: HEALTH
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[HEALTH] Score: XX/100 [health_bar] ([health_status])

### Health Factors
| Factor | Score | Status | Value | Target |
|--------|-------|--------|-------|--------|
| Agent Utilization | XX | [status] | XX% | 60-80% |
| Blocked Ratio | XX | [status] | XX% | <10% |
| Active Escalations | XX | [status] | X | 0-2 |
| Queue Age | XX | [status] | XX min | <60 min |

### Factor Summary
- Optimal: X | Acceptable: X | Warning: X | Critical: X

===============================================================
```

### Progress Section Only (`--progress` flag)

If `--progress` flag is present, show only:

```
===============================================================
  GATESMITH DASHBOARD: PROGRESS
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[PROGRESS]
  Total: XX | Done: XX | In Progress: XX | Blocked: XX
  [progress_bar] XX% complete

### Velocity
- Daily Average: X.X tasks/day
- Completed Today: X
- Avg Duration: X min/task

### Delivery Forecast
- Remaining Tasks: X
- Estimated Days: X.X
- Est. Completion: YYYY-MM-DD
- Confidence: [high/medium/low]

[If multi-project mode:]
### Per-Project Progress
| Project | Total | Done | Remaining | Completion |
|---------|-------|------|-----------|------------|
| [id] | X | X | X | XX% |

===============================================================
```

### Workforce Section Only (`--agents` flag)

If `--agents` flag is present, show only:

```
===============================================================
  GATESMITH DASHBOARD: WORKFORCE
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[WORKFORCE] X employees
  Active: X
  Idle: X
  Blocked: X

### Utilization
  [utilization_bar] XX% ([status])
  Target Range: 60-80%

### By Department
| Department | Total | Active | Utilization |
|------------|-------|--------|-------------|
| [dept_name] | X | X | XX% |

===============================================================
```

### Alerts Section Only (`--alerts` flag)

If `--alerts` flag is present, show only:

```
===============================================================
  GATESMITH DASHBOARD: ALERTS
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[ALERTS] X active (X critical, X warning)

### Critical Alerts
| ID | Message | Triggered |
|----|---------|-----------|
| [alert_id] | [message] | X min ago |

### Warning Alerts
| ID | Message | Triggered |
|----|---------|-----------|
| [alert_id] | [message] | X min ago |

### Alert Actions
- To clear an alert: uv run .claude/hooks/company/alert_rules.py clear --alert-id <id>
- To configure thresholds: uv run .claude/hooks/company/alert_rules.py configure --rule <rule> --threshold <value>

===============================================================
```

---

## Step 3: Render Progress Bars

### Health Bar (10 characters)

Use filled and empty block characters:

```python
def render_health_bar(score: int, width: int = 10) -> str:
    """
    Render a health bar like: Score: 78/100 [filled][empty]
    """
    filled = int(score / 100 * width)
    return chr(9619) * filled + chr(9617) * (width - filled)  # Use: filled + empty
```

**Output examples:**
- Score 78/100: `Score: 78/100 ▓▓▓▓▓▓▓▓░░ (Warning)`
- Score 95/100: `Score: 95/100 ▓▓▓▓▓▓▓▓▓▓ (Healthy)`
- Score 45/100: `Score: 45/100 ▓▓▓▓░░░░░░ (Critical)`

### Progress Bar (24 characters)

```python
def render_progress_bar(percent: float, width: int = 24) -> str:
    """
    Render a progress bar like: [filled][empty] XX%
    """
    filled = int(percent / 100 * width)
    return chr(9608) * filled + chr(9617) * (width - filled)  # Use: block + empty
```

**Output examples:**
- 62% complete: `████████████████░░░░░░░░ 62% complete`
- 100% complete: `████████████████████████ 100% complete`
- 25% complete: `██████░░░░░░░░░░░░░░░░░░ 25% complete`

### Health Status Mapping

| Score Range | Status | Color Indicator |
|-------------|--------|-----------------|
| 80-100 | Healthy | (green implied) |
| 60-79 | Warning | (yellow implied) |
| 0-59 | Critical | (red implied) |

---

## Step 4: Multi-Project Mode Enhancements

If `is_multi_project` is true, add a `[PROJECTS]` section to the full dashboard:

```
[PROJECTS] X registered
| Project | Health | Progress | Active Work |
|---------|--------|----------|-------------|
| [proj_id] | XX/100 | XX% | X |
| > [current] | XX/100 | XX% | X |

Current: [current_project_name] ([current_project_id])
```

Highlight the current project with `>` prefix.

---

## Step 5: Format Timestamps

Convert ISO timestamps to relative times:

| Duration | Display |
|----------|---------|
| < 60 min | "X min ago" |
| 1-24 hours | "X hours ago" |
| 1-7 days | "X days ago" |
| > 7 days | "YYYY-MM-DD" |

For generation timestamp, use: `YYYY-MM-DD HH:MM UTC`

---

## Step 6: Handle Empty States

### No Alerts
```
[ALERTS] 0 active
  All systems nominal. No alerts triggered.
```

### No Active Work
```
[PROGRESS]
  Total: 0 | Done: 0 | In Progress: 0 | Blocked: 0
  ░░░░░░░░░░░░░░░░░░░░░░░░ 0% complete
  No active work. Use /company-request to submit work.
```

### No Agents
```
[WORKFORCE] 0 employees
  No agents hired yet.
  Use /company-hire to add employees.
```

### Zero Velocity
```
[PROGRESS]
  ...
  Est. completion: Unable to estimate (no velocity data)
```

---

## Rules

- **Always fetch fresh data.** Don't cache between invocations.
- **Parse JSON carefully.** Handle missing fields gracefully with defaults.
- **Respect view mode flags.** If `--unified`, `--projects`, `--compare`, or `--project [id]` is passed, show the appropriate view.
- **Auto-detect context.** If no view mode flag is provided, detect based on current directory (company root = unified, project dir = project).
- **Respect section flags.** If `--health`, `--progress`, `--agents`, or `--alerts` is passed, show ONLY that section with more detail.
- **Section flags work with view modes.** Combining `--unified --health` shows company-wide health details.
- **Use ASCII art consistently.** Box-drawing characters for borders, block characters for bars.
- **Handle errors gracefully.** If a data source fails, show "Data unavailable" for that section.
- **Include generation timestamp.** Always show when the dashboard was generated.
- **Show context in header.** Always indicate the current view mode and scope in the dashboard header.
- **Quick operation.** This should complete in under 2 seconds for typical use.
- **Multi-project aware.** Show per-project breakdown when in multi-project mode.
- **Highlight concerns.** Critical items should stand out with appropriate symbols (X for error, ! for warning).
- **Include navigation tips.** Each view should include a tip footer suggesting related views.
