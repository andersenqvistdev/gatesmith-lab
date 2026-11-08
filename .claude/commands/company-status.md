# /company-status — Display Company Organization Status

Display the current state of the virtual company including org chart, agent statuses, active work items, and knowledge base metrics. In multi-project mode, shows company-wide aggregated view by default.

## Input
$ARGUMENTS

Optional arguments:
- `--agents` — Show detailed agent information only
- `--work` — Show active work items only
- `--knowledge` — Show knowledge base metrics only
- `--project=<id>` — Show status for a specific project only (multi-project mode)
- `--projects` — Show projects summary table (multi-project mode)

## Step 0: Resolve Company Root and Mode

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

## Step 0.1: Check Company Exists

Check if the resolved `.company/` directory exists:

```bash
ls -la [company_dir]/ 2>/dev/null
```

**If not exists and in multi-project mode:**
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

**If not exists and in legacy mode:**
```
## Company Not Initialized

No company directory found at `.company/`.

To initialize a new company structure, run:
  /company-init

This will create:
- Organization structure (departments, teams)
- Knowledge base (decisions, patterns)
- Agent memory templates
```

Exit without further processing.

## Step 1: Load Configuration

Read the following files from `[company_dir]`:
- `org.json` — Organization structure
- `config.json` — Runtime configuration
- `knowledge/decisions.md` — Decision records
- `knowledge/patterns.md` — Implementation patterns

**In multi-project mode, also load:**
- `assignments/_index.json` — Project index
- `assignments/[project_id].json` — Per-project assignments (for each registered project)

Parse the JSON files and extract:
- Company name and description
- Departments and teams
- Agent list with statuses
- Active work items
- **Registered projects (multi-project mode)**
- **Project assignments (multi-project mode)**

### 1.1 A fresh install is complete

The installer writes a minimal `org.json` on purpose: an `employees` list with one worker
(`senior-engineer`) and nothing else. There is no company name, no departments, no `config.json`
and no `knowledge/` yet, and none of them is needed to work. When `org.json` has employees but no
`company` or `departments` key, this is that state:

- Show it as **"Single-project setup: 1 worker ready"** (count the employees), not as a partial or
  broken setup, and do not list the missing files as problems.
- Skip the empty department, team, knowledge and configuration sections.
- Point to the next step: `/company-request "..."` to queue work.
- Never suggest `/company-init --force` or any other reset for this state.

## Step 2: Display Company Header

**In multi-project mode:**
```
═══════════════════════════════════════════════════════════════════════════════
 COMPANY STATUS                                              [Gatesmith Labs]
═══════════════════════════════════════════════════════════════════════════════
 Company: {company.name}
 Description: {company.description}
 Mode: Multi-Project
 Company Root: {company_root}
 Projects: {projects.count} registered
 Current Project: {current_project.name} ({current_project.id})
 Created: {company.created}
 Last Updated: {work.lastUpdated}
═══════════════════════════════════════════════════════════════════════════════
```

**In legacy (single-project) mode:**
```
═══════════════════════════════════════════════════════════════════════════════
 COMPANY STATUS                                              [Gatesmith Labs]
═══════════════════════════════════════════════════════════════════════════════
 Company: {company.name}
 Description: {company.description}
 Mode: Single-Project (Legacy)
 Created: {company.created}
 Last Updated: {work.lastUpdated}
═══════════════════════════════════════════════════════════════════════════════
```

## Step 3: Display Organization Chart (ASCII)

Build an ASCII org chart showing the hierarchy:

```
## Organization Chart

                           ┌─────────────────────┐
                           │     Gatesmith Labs      │
                           │      (Company)      │
                           └──────────┬──────────┘
                                      │
         ┌────────────────────────────┼────────────────────────────┐
         │                            │                            │
         ▼                            ▼                            ▼
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│   Engineering   │         │     Product     │         │     Design      │
│   [X agents]    │         │   [X agents]    │         │   [X agents]    │
└────────┬────────┘         └────────┬────────┘         └────────┬────────┘
         │                           │                           │
    ┌────┴────┬────┐            ┌────┴────┐               ┌──────┴──────┐
    │         │    │            │         │               │             │
    ▼         ▼    ▼            ▼         ▼               ▼             ▼
┌───────┐ ┌────┐ ┌────┐    ┌────────┐ ┌───────┐     ┌─────────┐ ┌─────────┐
│ Core  │ │Int │ │Dev │    │Strategy│ │ UX Res│     │   UX    │ │ Visual  │
│ (N)   │ │(N) │ │Ops │    │  (N)   │ │  (N)  │     │   (N)   │ │   (N)   │
│       │ │    │ │(N) │    │        │ │       │     │         │ │         │
└───────┘ └────┘ └────┘    └────────┘ └───────┘     └─────────┘ └─────────┘
```

For teams with no agents, show `(0)` or `(empty)`.

For agents assigned as heads or leads, show their name in the box.

### Simplified ASCII Format (when terminal is narrow or many departments):

```
## Organization Chart

Gatesmith Labs
├── Engineering (X agents)
│   ├── Core Platform (N)
│   ├── Integrations (N)
│   └── DevOps (N)
├── Product (X agents)
│   ├── Product Strategy (N)
│   └── User Research (N)
└── Design (X agents)
    ├── UX Design (N)
    └── Visual Design (N)
```

## Step 3.5: Display Projects Table (Multi-Project Mode)

**Only shown in multi-project mode or when `--projects` flag is used.**

If `--project=<id>` is specified, skip this section and show project-specific view in subsequent steps.

### Projects Overview

```
## Registered Projects

| Project ID | Name | Path | Employees | Active Work | Status |
|------------|------|------|-----------|-------------|--------|
| forge-framework | Gatesmith Framework | ./projects/forge | 3 | 2 | active |
| api-service | API Service | ./services/api | 1 | 0 | idle |
| docs-site | Documentation | ./docs | 0 | 0 | unassigned |
| ► current ► | tasks | . | 2 | 1 | active |

### Project Summary
| Status | Count | Percentage |
|--------|-------|------------|
| Active | X | XX% |
| Idle | X | XX% |
| Unassigned | X | XX% |
| TOTAL | X | 100% |
```

**Notes:**
- Current project (where command was run from) is highlighted with `► current ►`
- Path is relative to company root
- Employee count is from assignments
- Active Work count is from project-specific work items
- Status: `active` (work in progress), `idle` (no recent work), `unassigned` (no employees)

### No Projects Registered

If no projects exist in the index:

```
## Registered Projects

No projects have been registered with this company yet.

### Getting Started

To add an existing project:
  /company-add-project ./path/to/project

To add the current directory as a project:
  /company-add-project .

### Quick Commands
- `/company-projects` — List all projects with details
- `/company-add-project` — Register a project
```

## Step 4: Display Agent Status Table

### Project-Specific View (--project flag)

If `--project=<id>` was specified, filter agents to show only those assigned to that project:

```
## Agent Roster — Project: [project_name] ([project_id])

| Agent ID | Name | Department | Team | Type | Status | Current Work |
|----------|------|------------|------|------|--------|--------------|
| eng-lead | Alice | Engineering | Core | persistent | available | - |
| fe-dev-1 | Bob | Engineering | Core | persistent | busy | task-1.2 |

Showing 2 agents assigned to project [project_id].
Total company agents: X
```

### Company-Wide View (default in multi-project mode)

If agents exist in `org.json`:

```
## Agent Roster

| Agent ID | Name | Department | Team | Type | Status | Current Work |
|----------|------|------------|------|------|--------|--------------|
| eng-lead | Alice | Engineering | Core | persistent | available | - |
| fe-dev-1 | Bob | Engineering | Core | persistent | busy | task-1.2 |
| ux-res | Carol | Design | UX | consultant | blocked | waiting on API |

### Status Summary
| Status | Count | Percentage |
|--------|-------|------------|
| Available | X | XX% |
| Busy | X | XX% |
| Blocked | X | XX% |
| Offline | X | XX% |
| TOTAL | X | 100% |
```

If no agents exist:

```
## Agent Roster

No agents have been hired yet.

To hire agents, use:
  /company-hire <role> --department=<dept> --team=<team>

Available roles: architect, implementer, reviewer, tester, security-auditor
```

## Step 5: Display Active Work Items

### Project-Specific View (--project flag)

If `--project=<id>` was specified, filter work items to show only those for that project:

```
## Active Work — Project: [project_name] ([project_id])

| Work ID | Title | Assignee | Status | Started | Duration |
|---------|-------|----------|--------|---------|----------|
| task-1.1 | Setup auth module | eng-lead | in_progress | 2h ago | 2h |

### Project Work Summary
| Status | Count |
|--------|-------|
| Active | X |
| Pending | X |
| Completed (Today) | X |

Showing work for project [project_id].
Company-wide work items: X active, X pending
```

### Company-Wide View (default in multi-project mode)

If work items exist in `org.json`:

```
## Active Work

| Work ID | Title | Project | Assignee | Status | Started | Duration |
|---------|-------|---------|----------|--------|---------|----------|
| task-1.1 | Setup auth module | forge-framework | eng-lead | in_progress | 2h ago | 2h |
| task-1.2 | Add middleware | api-service | fe-dev-1 | review | 1h ago | 1h |
| task-2.1 | Update docs | docs-site | - | pending | - | - |

### Work Distribution by Project (Multi-Project Mode)
| Project | Active | Pending | Completed Today |
|---------|--------|---------|-----------------|
| forge-framework | X | X | X |
| api-service | X | X | X |
| docs-site | X | X | X |
| **Company Total** | **X** | **X** | **X** |

### Work Distribution by Department
| Department | Active | Pending | Completed Today |
|------------|--------|---------|-----------------|
| Engineering | X | X | X |
| Product | X | X | X |
| Design | X | X | X |

### Work Queue
Pending items waiting for assignment: X
```

If no work items:

```
## Active Work

No active work items.

To assign work, use:
  /company-assign "<work description>" --to=<agent-id>

Or delegate to a department:
  /company-assign "<work description>" --department=<dept>

In multi-project mode, you can also specify a project:
  /company-request "feature description" --project=<project-id>
```

## Step 6: Display Knowledge Base Metrics

Count entries in the knowledge base files:

```
## Knowledge Base

### Metrics
| Category | Count | Last Updated |
|----------|-------|--------------|
| Architecture Decisions (ADRs) | X | YYYY-MM-DD |
| Implementation Patterns | X | YYYY-MM-DD |
| Agent Learnings | X files | YYYY-MM-DD |

### Recent Decisions
| ADR | Title | Status | Date |
|-----|-------|--------|------|
| ADR-0001 | Use ADR Format | Accepted | 2024-01-01 |
| ADR-NNNN | ... | ... | ... |

### Active Patterns
| Pattern | Category | Usage Count |
|---------|----------|-------------|
| Builder-Validator Loop | Architecture | - |
| Atomic Commits | DevOps | - |
```

If knowledge base is minimal:

```
## Knowledge Base

Knowledge base initialized with templates.

| Category | Count |
|----------|-------|
| Architecture Decisions | 1 (template) |
| Implementation Patterns | 2 (core patterns) |

To add knowledge:
- Create ADRs for significant decisions
- Document patterns as they emerge
- Agent learnings accumulate automatically
```

## Step 7: Display Configuration Summary

```
## Configuration

| Setting | Value |
|---------|-------|
| Work Allocation | pull |
| Max Concurrent Agents | 10 |
| Max Tasks per Agent | 2 |
| Consultant Idle Timeout | 24h |
| Metrics Window | 7 days |

### Escalation Timeouts
| Tier | Timeout |
|------|---------|
| Tier 1 | 15 min |
| Tier 2 | 30 min |
| Tier 3 | 60 min |
| Tier 4 | 120 min |
```

## Step 8: Display Recent Activity (Optional)

If activity log exists (from hooks):

```
## Recent Activity (Last 24h)

| Time | Agent | Action | Details |
|------|-------|--------|---------|
| 2h ago | eng-lead | task_complete | Setup auth module |
| 3h ago | eng-lead | task_start | Setup auth module |
| 4h ago | system | agent_created | eng-lead hired |

Activity: X actions in last 24h
```

If no activity log:

```
## Recent Activity

Activity logging not yet configured or no recent activity.
```

## Step 9: Final Summary

### Multi-Project Mode Summary

```
═══════════════════════════════════════════════════════════════════════════════
 SUMMARY                                                    [Multi-Project]
═══════════════════════════════════════════════════════════════════════════════
 Company Root: [company_root]
 Projects: X registered │ X active │ X idle
 Departments: X active │ Teams: X total │ Agents: X hired
 Work: X active, X pending, X completed today
 Knowledge: X ADRs, X patterns
 Current Project: [project_name] ([project_id])
═══════════════════════════════════════════════════════════════════════════════

### Quick Commands
- `/company-hire` — Hire new agents
- `/company-assign` — Assign work
- `/company-standup` — Run daily standup
- `/company-projects` — List all projects
- `/company-add-project` — Register new project
- `/company-status --project=<id>` — View specific project status
```

### Project-Specific Summary (when --project flag used)

```
═══════════════════════════════════════════════════════════════════════════════
 PROJECT SUMMARY                                            [project_name]
═══════════════════════════════════════════════════════════════════════════════
 Project: [project_name] ([project_id])
 Path: [relative_path]
 Agents Assigned: X │ Work Active: X │ Work Pending: X
═══════════════════════════════════════════════════════════════════════════════

### Quick Commands
- `/company-assign <agent-id> [project_id]` — Assign agent to project
- `/company-request "task" --project=[project_id]` — Submit work request
- `/company-status` — View full company status
```

### Legacy Mode Summary

```
═══════════════════════════════════════════════════════════════════════════════
 SUMMARY
═══════════════════════════════════════════════════════════════════════════════
 Departments: X active │ Teams: X total │ Agents: X hired
 Work: X active, X pending, X completed today
 Knowledge: X ADRs, X patterns
═══════════════════════════════════════════════════════════════════════════════

### Quick Commands
- `/company-request "..."` — Queue work
- `/company-hire` — Hire new agents
- `/company-standup` — Run daily standup
```

## Rules

- **Handle missing files gracefully.** If any file is missing, show appropriate message and continue.
- **Count entries accurately.** Parse markdown files to count ADRs and patterns.
- **Show relative times.** Convert timestamps to human-readable relative times (e.g., "2h ago").
- **Adapt display to data.** If sections are empty, show helpful guidance instead of empty tables.
- **Respect --flags.** If specific flags passed, show only that section with more detail.
- **Use company_resolver for root detection.** Always use the company_resolver.py utility to find the company root and determine mode.
- **In multi-project mode, default to company-wide view.** Show aggregated data across all projects unless `--project` is specified.
- **Highlight current project.** When displaying the projects table, clearly indicate which project the user is currently in.
- **Support --project flag filtering.** When specified, filter agents, work items, and summaries to that project only.
- **Works from any directory in company.** The command should function correctly regardless of which subdirectory the user runs it from, as long as they are within the company root hierarchy.
