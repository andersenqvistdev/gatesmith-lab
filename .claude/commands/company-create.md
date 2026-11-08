# /company-create — Create Multi-Project Company Root

Create a new multi-project company root structure. This is different from `/company-init` which creates a single-project company. The multi-project structure enables employees to work across multiple projects with shared knowledge and coordinated work queues.

## Input
$ARGUMENTS

Optional arguments:
- `--name "Company Name"` — Set the company name (default: directory name)
- `--departments eng,product,design` — Custom department selection (default: engineering, product, design)
- `--with-projects` — Create a `projects/` subdirectory for organizing projects
- `--force` — Reinitialize even if already exists (WARNING: overwrites existing config)

## Step 0: Check Existing State

Check if this is already a company root or inside an existing company:

```bash
ls -la .forge-company-root 2>/dev/null
```

Also check for existing `.company/` directory:

```bash
ls -la .company/ 2>/dev/null
```

**If `.forge-company-root` exists and no `--force` flag:**
```
## Company Root Already Exists

A multi-project company root already exists at this location.

| File | Status |
|------|--------|
| .forge-company-root | [exists] |
| .company/ | [exists/missing] |
| .company/config.json | [exists/missing] |
| .company/employees/ | [exists/missing] |
| .company/assignments/ | [exists/missing] |

To reinitialize (WARNING: may overwrite config), run:
  /company-create --force

To view current configuration:
  /company-status
```

Exit without changes.

**If inside an existing company (found `.forge-company-root` in parent directories):**
```
## Already Inside a Company

Found company root at: [path to root]
Company name: [company name from marker]

You cannot create a nested company structure.

Options:
1. Use `/company-add-project` to add the current directory as a project
2. Create the new company in a different location outside this company
```

Exit without changes.

## Step 1: Parse Arguments

Parse `$ARGUMENTS` for options:
- Extract `--name` value or use current directory name
- Extract `--departments` list or use default: `["engineering", "product", "design"]`
- Check for `--with-projects` flag
- Check for `--force` flag

## Step 2: Create Directory Structure

Create the full multi-project company directory tree:

```
./
├── .forge-company-root            # Marker file (JSON)
├── .company/                      # Global company state
│   ├── config.json                # Runtime configuration (mode: multi-project)
│   ├── org.json                   # Organization structure
│   ├── manifest.json              # Extension manifest
│   ├── work_queue.json            # Company-level work queue
│   ├── employees/                 # All employees by department
│   │   ├── README.md              # Employee directory guide
│   │   ├── TEMPLATE/              # Template for new employees
│   │   │   ├── memory.md          # Working memory template
│   │   │   └── learnings.md       # Long-term learnings template
│   │   ├── engineering/           # Engineering department employees
│   │   ├── product/               # Product department employees
│   │   └── design/                # Design department employees
│   ├── assignments/               # Project assignments
│   │   ├── README.md              # Assignments guide
│   │   └── _index.json            # Project index
│   └── knowledge/                 # Shared knowledge base
│       ├── README.md              # Knowledge base guide
│       ├── decisions.md           # Architecture Decision Records
│       └── patterns.md            # Implementation patterns
└── projects/                      # (optional) Projects subdirectory
```

## Step 3: Create .forge-company-root Marker

Create the marker file at the root:

```json
{
  "version": "1.0",
  "company_name": "[Company Name]",
  "created_at": "[ISO 8601 timestamp]",
  "config": {
    "work_queue_mode": "company-level",
    "strict_mode": false
  }
}
```

This marker file:
- Identifies this directory as a multi-project company root
- Is used by `company_resolver.py` for upward path resolution
- Contains basic company metadata
- `strict_mode: false` means projects must explicitly join (manual registration)

## Step 4: Create Core Configuration Files

### config.json

```json
{
  "mode": "multi-project",
  "enabledDepartments": ["engineering", "product", "design"],
  "workAllocationMode": "pull",
  "workQueueMode": "company-level",
  "strictMode": false,
  "escalation": {
    "tier1Timeout": 15,
    "tier2Timeout": 30,
    "tier3Timeout": 60,
    "tier4Timeout": 120
  },
  "agents": {
    "maxConcurrentTasks": 2,
    "maxConcurrentAgents": 10,
    "autoArchiveConsultants": true,
    "consultantIdleTimeout": 24
  },
  "memory": {
    "maxLinesPerFile": 1000,
    "archiveRetentionDays": 30
  },
  "metrics": {
    "rollingWindowDays": 7,
    "enabled": true
  }
}
```

Update `enabledDepartments` if custom departments specified via `--departments`.

### manifest.json

```json
{
  "name": "company",
  "version": "1.2.0",
  "description": "Multi-project company extension for Gatesmith providing organizational patterns, team configurations, and cross-project coordination.",
  "forgeVersion": ">=1.0.0",
  "mode": "multi-project",
  "features": [
    "team-configs",
    "org-templates",
    "custom-hooks",
    "enterprise-auth",
    "multi-project",
    "company-root",
    "cross-project-knowledge"
  ]
}
```

### org.json

```json
{
  "version": "1.2",
  "mode": "multi-project",
  "company": {
    "name": "[Company Name]",
    "description": "Multi-project company",
    "created": "[ISO 8601 timestamp]"
  },
  "departments": [
    {
      "id": "engineering",
      "name": "Engineering",
      "teams": [
        {"id": "core", "name": "Core Platform", "members": []},
        {"id": "integrations", "name": "Integrations", "members": []},
        {"id": "devops", "name": "DevOps", "members": []}
      ],
      "head": null,
      "lead": null
    },
    {
      "id": "product",
      "name": "Product",
      "teams": [
        {"id": "product-strategy", "name": "Product Strategy", "members": []},
        {"id": "user-research", "name": "User Research", "members": []}
      ],
      "head": null,
      "lead": null
    },
    {
      "id": "design",
      "name": "Design",
      "teams": [
        {"id": "ux", "name": "UX Design", "members": []},
        {"id": "visual", "name": "Visual Design", "members": []}
      ],
      "head": null,
      "lead": null
    }
  ],
  "employees": [],
  "projects": []
}
```

Generate departments based on enabled departments list. Each department gets:
- Unique ID (lowercase, hyphenated)
- Display name
- Default teams (varies by department type)
- Empty head/lead/members (to be filled by hiring)

Standard department templates:

**engineering:**
- Teams: core, integrations, devops

**product:**
- Teams: product-strategy, user-research

**design:**
- Teams: ux, visual

**Custom departments:** Create with single "general" team.

### work_queue.json

```json
{
  "version": "1.0",
  "items": []
}
```

## Step 5: Create Employees Directory

### employees/README.md

```markdown
# Company Employees

This directory contains all employees across the company, organized by department.

## Structure

```
employees/
├── TEMPLATE/           # Template for new employees
│   ├── memory.md       # Working memory template
│   └── learnings.md    # Long-term learnings template
├── engineering/        # Engineering department
├── product/            # Product department
└── design/             # Design department
```

## Employee Files

Each employee has a directory containing:
- `memory.md` — Current working context, active assignments, recent interactions
- `learnings.md` — Long-term learnings, patterns, expertise gained over time

## Creating New Employees

Use `/company-hire` to create new employees. The meta-agent will generate
appropriate agent definitions and create memory files from templates.

## Project Assignments

Employees can be assigned to multiple projects. Assignments are tracked in
`.company/assignments/{project_id}.json`. Use `/company-assign` to manage
project assignments.
```

### employees/TEMPLATE/memory.md

```markdown
# Working Memory

## Current Context
**Active Project:** [unassigned]
**Current Task:** [none]
**Last Updated:** [date]

## Active Assignments
| Project | Role | Started | Focus |
|---------|------|---------|-------|
| - | - | - | - |

## Recent Interactions
<!-- Last 5-10 significant interactions -->

## Project Experience
<!-- Accumulated per-project context -->

## Preferences
<!-- Learned preferences and working style -->

## Scratchpad
<!-- Temporary notes, ideas, work in progress -->
```

### employees/TEMPLATE/learnings.md

```markdown
# Long-Term Learnings

## Mistakes & Lessons
<!-- What went wrong and what was learned -->

## Successful Patterns
<!-- Approaches that worked well -->

## Domain Expertise
<!-- Technical knowledge accumulated -->

## Cross-Project Insights
<!-- Patterns that apply across multiple projects -->

## Collaboration Notes
<!-- Working style with other agents -->

## Meta-Learnings
<!-- Insights about self-improvement -->

## Knowledge Gaps
<!-- Areas for future learning -->
```

## Step 6: Create Department Employee Directories

For each enabled department, create the directory:

```bash
mkdir -p .company/employees/{department-id}
```

## Step 7: Create Assignments Directory

### assignments/README.md

```markdown
# Project Assignments

This directory tracks which employees are assigned to which projects.

## Structure

```
assignments/
├── _index.json         # Index of all registered projects
├── {project-id}.json   # Assignment file for each project
└── README.md           # This file
```

## Schema

### _index.json
```json
{
  "projects": ["project-id-1", "project-id-2"],
  "updated_at": "ISO 8601 timestamp"
}
```

### {project-id}.json
```json
{
  "project_id": "string",
  "project_path": "relative path from company root",
  "assignments": [
    {
      "employee_id": "string",
      "role": "lead|contributor|reviewer",
      "start_date": "ISO 8601",
      "end_date": "ISO 8601 or null",
      "active": true
    }
  ],
  "updated_at": "ISO 8601"
}
```

## Commands

- `/company-add-project` — Register a new project with the company
- `/company-assign` — Assign an employee to a project
- `/company-projects` — List all registered projects
```

### assignments/_index.json

```json
{
  "projects": [],
  "updated_at": "[ISO 8601 timestamp]"
}
```

## Step 8: Create Knowledge Base

### knowledge/README.md

```markdown
# Company Knowledge Base

Shared knowledge that compounds across all projects and employees.

## Structure

- `decisions.md` — Architecture Decision Records (ADRs)
- `patterns.md` — Implementation patterns and best practices

## Contributing

Employees contribute knowledge through:
1. Automatic capture during work (via knowledge_capture hook)
2. Manual documentation of decisions and patterns
3. Retrospective learnings from projects

## Querying

Use `/company-knowledge` to search the knowledge base:
- `/company-knowledge search "authentication patterns"`
- `/company-knowledge --project forge-framework`
```

### knowledge/decisions.md

```markdown
# Architecture Decision Records

## Template

```
## ADR-NNNN: [Title]

**Status:** [Proposed | Accepted | Deprecated | Superseded]
**Date:** [YYYY-MM-DD]
**Project:** [project-id or "company-wide"]

### Context
[What is the issue that we're seeing that motivates this decision?]

### Decision
[What is the change that we're proposing and/or doing?]

### Consequences
[What becomes easier or more difficult to do because of this change?]
```

---

## ADR-0001: Use ADR Format for Architecture Decisions

**Status:** Accepted
**Date:** [current date]
**Project:** company-wide

### Context
We need a consistent format for documenting significant architecture decisions
across all projects in the company.

### Decision
Use Architecture Decision Records (ADRs) in this file to document all
significant technical decisions that affect multiple projects or establish
company-wide patterns.

### Consequences
- Decisions are documented and searchable
- Historical context is preserved
- New employees can understand past choices
- Requires discipline to document decisions as they're made
```

### knowledge/patterns.md

```markdown
# Implementation Patterns

## Template

```
## Pattern: [Name]

**Category:** [Architecture | DevOps | Testing | Security | etc.]
**Source:** [project-id or "company-wide"]

### Problem
[What problem does this pattern solve?]

### Solution
[How does it solve it?]

### Example
[Code or configuration example]

### When to Use
[Conditions where this pattern applies]

### Related Patterns
[Links to related patterns]
```

---

## Pattern: Builder-Validator Loop

**Category:** Architecture
**Source:** company-wide

### Problem
Complex plans often have flaws that aren't caught until implementation.

### Solution
Use a two-agent pattern: Architect creates plan, Checker validates it.
Loop until the plan passes validation.

### When to Use
- Complex or epic features
- Changes affecting multiple systems
- High-stakes modifications

---

## Pattern: Atomic Commits

**Category:** DevOps
**Source:** company-wide

### Problem
Large commits are hard to review and impossible to bisect.

### Solution
One task = one commit. Each commit should be self-contained and revertable.

Format: `feat(phase-N): task-name [task-id]`

### When to Use
- Always during `/build` workflow
- Any structured implementation work
```

## Step 9: Create Projects Directory (Optional)

If `--with-projects` flag is specified:

```bash
mkdir -p projects
```

Create a placeholder README:

```markdown
# Projects

This directory contains projects belonging to the company.

## Adding Projects

Use `/company-add-project` to register projects with the company:

```bash
/company-add-project ./projects/my-project
```

Or add existing projects from anywhere:

```bash
/company-add-project /path/to/existing/project
```

## Structure

Each project is a self-contained directory with its own:
- `.planning/` — Project-specific planning documents
- `.claude/` — Project-specific Claude configuration
- Source code and assets

Projects share:
- Company employees (via assignments)
- Company knowledge base
- Company work queue
```

## Step 10: Display Summary

```
## Company Root Created

═══════════════════════════════════════════════════════════════
 MULTI-PROJECT COMPANY                               [created]
═══════════════════════════════════════════════════════════════
 Company: [Company Name]
 Mode: multi-project
 Created: [timestamp]
═══════════════════════════════════════════════════════════════

### Root Marker
| File | Status | Description |
|------|--------|-------------|
| .forge-company-root | created | Company root marker (JSON) |

### Company Directory
| File | Status | Description |
|------|--------|-------------|
| .company/config.json | created | Runtime configuration |
| .company/org.json | created | Organization structure |
| .company/manifest.json | created | Extension manifest |
| .company/state/work_queue.json | created | Company-level work queue |

### Employees Directory
| Directory | Status | Description |
|-----------|--------|-------------|
| .company/employees/TEMPLATE/ | created | Employee templates |
| .company/employees/engineering/ | created | Engineering employees |
| .company/employees/product/ | created | Product employees |
| .company/employees/design/ | created | Design employees |

### Assignments Directory
| File | Status | Description |
|------|--------|-------------|
| .company/assignments/_index.json | created | Project index |
| .company/assignments/README.md | created | Assignments guide |

### Knowledge Base
| File | Status | Description |
|------|--------|-------------|
| .company/knowledge/README.md | created | Knowledge base guide |
| .company/knowledge/decisions.md | created | Architecture decisions |
| .company/knowledge/patterns.md | created | Implementation patterns |

[If --with-projects used:]
### Projects Directory
| Directory | Status |
|-----------|--------|
| projects/ | created |

═══════════════════════════════════════════════════════════════

### Key Differences from Single-Project Company

| Feature | Single-Project | Multi-Project |
|---------|---------------|---------------|
| Company root | .company/ in project | .forge-company-root above projects |
| Employees | Per-project agents | Company-level employees |
| Work queue | Project-scoped | Company-level with project tags |
| Knowledge | Project knowledge | Cross-project knowledge |
| Assignments | N/A | Explicit project assignments |

### Next Steps

1. **Add your first project:**
   ```bash
   /company-add-project ./path/to/project
   ```
   Or if using the projects/ directory:
   ```bash
   cd projects && git clone [repo] my-project
   /company-add-project ./projects/my-project
   ```

2. **Hire employees:**
   ```bash
   /company-hire senior backend engineer --department=engineering
   ```

3. **Assign employees to projects:**
   ```bash
   /company-assign [employee-id] [project-id]
   ```

4. **View company status:**
   ```bash
   /company-status
   ```

### Available Commands
- `/company-status` — View company-wide organization status
- `/company-add-project` — Register a project with the company
- `/company-projects` — List all registered projects
- `/company-hire` — Hire new employees
- `/company-assign` — Assign employees to projects
- `/company-dismiss` — Dismiss an employee
- `/company-request` — Submit work request to the company
```

## Rules

1. **Never create nested companies.** If already inside a company root, refuse and suggest alternatives.

2. **Never overwrite without --force.** Existing config represents customization that should be preserved.

3. **Validate department names.** Must be lowercase, alphanumeric with hyphens only.

4. **Create all directories atomically.** Either all succeed or none (rollback on failure).

5. **Always create .forge-company-root first.** This marker identifies the company root for upward resolution.

6. **Set mode: "multi-project" in config.json.** This distinguishes from single-project v1.1 companies.

7. **Initialize empty but valid JSON files.** All JSON files should be valid and parseable even when empty.

8. **Use ISO 8601 timestamps.** All timestamps should be in ISO 8601 format for consistency.

## Error Handling

### Permission Denied

```
## Permission Denied

Cannot create company structure. Check directory permissions.

**Path:** [path]
**Error:** [error details]

Try:
1. Check you have write permissions to this directory
2. Run from a different location
```

### Invalid Department Name

```
## Invalid Department Name

Department names must be lowercase, alphanumeric with hyphens only.

**Invalid:** [invalid name]
**Suggested:** [suggested fix]

Examples of valid names: engineering, front-end, dev-ops, qa-team
```

### Partial Failure

If creation fails midway:

```
## Partial Creation Failed

Some files were created but the process failed.

**Created:**
- [list of created files]

**Failed:**
- [file that failed]: [error]

**Cleanup:**
The partially created structure has been removed.

Please fix the underlying issue and try again.
```
