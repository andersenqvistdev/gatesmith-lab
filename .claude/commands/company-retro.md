# /company-retro — Run Organizational Retrospective

Run a structured retrospective for the company or a specific project. Gathers completed work since the last retro, analyzes patterns (what worked), identifies issues (escalations, failures, slow tasks), generates action items, and updates the knowledge base with findings.

Supports two modes:
- **Project-level retro** (default): Analyzes work within the current project context
- **Company-wide retro** (`--company`): Aggregates learnings across all registered projects

## Input
$ARGUMENTS

Optional arguments:
- `--since=YYYY-MM-DD` — Start date for retro period (default: since last retro or 7 days)
- `--scope=all|department|team` — Scope of retro (default: all)
- `--department=<id>` — Specific department (required if scope=department)
- `--team=<id>` — Specific team (required if scope=team)
- `--project=<id>` — Run retro for a specific project (multi-project mode only)
- `--company` — Run company-wide retro aggregating all projects (multi-project mode)
- `--dry-run` — Preview retro without updating knowledge base

## Step 0: Check Company Exists and Determine Mode

Check if `.company/` directory exists:

```bash
ls -la .company/ 2>/dev/null
```

**If not exists:**
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

### 0.1: Determine Operating Mode

Read `.company/org.json` and check the `mode` field:

```
| Mode | Description |
|------|-------------|
| single-project | Company operates on a single codebase (classic mode) |
| multi-project | Company manages multiple registered projects |
```

### 0.2: Validate Arguments for Mode

**If mode is `single-project`:**
- `--company` flag is ignored (already company-wide)
- `--project` flag is invalid (no projects registered)

**If mode is `multi-project`:**
- Without `--company` or `--project`: Run retro for current project (detect from cwd)
- With `--project=<id>`: Run retro for specified project
- With `--company`: Run company-wide aggregated retro

### 0.3: Resolve Project Context (Multi-Project Mode)

If multi-project mode and not `--company`:

1. If `--project=<id>` provided, find project in `org.json.projects[]` by id
2. Otherwise, detect current project from working directory:
   - Match `cwd` against registered project paths
   - If no match found, show error:

```
## Project Not Found

Current directory is not a registered project.

Registered projects:
| ID | Name | Path |
|----|------|------|
| web-app | Web Application | /path/to/web-app |
| api-server | API Server | /path/to/api-server |

To run a project-level retro:
  cd /path/to/project && /company-retro
  OR
  /company-retro --project=<id>

To run a company-wide retro:
  /company-retro --company
```

Exit without further processing.

## Step 1: Determine Retro Period and Context

### 1.1: Determine Retro Scope

**For Project-Level Retro:**
Check for last retro date in project's `.planning/RETROS.md` or `.company/knowledge/retros.md`:

1. If project-specific retro file exists, parse most recent retro date
2. If `--since` argument provided, use that date
3. Otherwise, default to last 7 days

**For Company-Wide Retro:**
Check for last company retro in `.company/knowledge/retros.md`:

1. Look for entries with `Scope: company-wide`
2. Use most recent company-wide retro date, or default to 7 days

```
## Retrospective Period

| Parameter | Value |
|-----------|-------|
| Mode | project-level / company-wide |
| Project(s) | <project-name> / ALL (N projects) |
| Start Date | YYYY-MM-DD |
| End Date | YYYY-MM-DD (today) |
| Duration | N days |
| Scope | all / department / team |
| Last Retro | YYYY-MM-DD or "First retro" |
```

## Step 2: Gather Completed Work

Collect completed work from multiple sources.

**For Project-Level Retro:**
Gather from single project context.

**For Company-Wide Retro:**
Iterate over all registered projects in `org.json.projects[]` and aggregate data.

### 2.1: Git History

**Project-Level:**
```bash
git log --oneline --since="<start-date>" --until="<end-date>"
```

**Company-Wide (for each project):**
```bash
git -C "<project-path>" log --oneline --since="<start-date>" --until="<end-date>"
```

Extract per project:
- Number of commits
- Commit messages (features, fixes, refactors)
- Authors (agent IDs)
- Files changed

For company-wide, aggregate totals:
```
| Project | Commits | Features | Fixes | Refactors |
|---------|---------|----------|-------|-----------|
| web-app | 45 | 12 | 8 | 5 |
| api-server | 32 | 8 | 6 | 3 |
| **Total** | **77** | **20** | **14** | **8** |
```

### 2.2: Organization Work Items
Read `.company/org.json` and extract:
- Completed work items with timestamps
- Work item durations
- Assignees

**For company-wide retro:**
- Filter by `workItem.projectId` to group by project
- Show cross-project work items

### 2.3: Planning Docs (if exists)

**Project-Level:**
Check `<project-path>/.planning/ROADMAP.md` for:
- Completed tasks with `[x]`
- Phase completions

**Company-Wide:**
Check each project's `.planning/ROADMAP.md`:
```
| Project | Completed Tasks | Phases Done | In Progress |
|---------|-----------------|-------------|-------------|
| web-app | 23 | 2 | Phase 3 |
| api-server | 15 | 1 | Phase 2 |
```

### 2.4: Agent/Employee Activity

**Project-Level:**
Scan `.company/employees/*/memory.md` for:
- Recent completed assignments in this project
- Noted challenges or blockers

**Company-Wide:**
Scan all employee memories and aggregate:
- Assignments across all projects
- Cross-project collaboration
- Shared blockers

```
| Employee | Projects Touched | Tasks Completed | Cross-Project Work |
|----------|------------------|-----------------|-------------------|
| eng-lead | 3 | 12 | API contract alignment |
| frontend-dev | 1 | 8 | None |
```

## Step 3: Analyze Patterns (What Worked)

Identify positive patterns from the gathered data.

### 3.1: Velocity Metrics

**Project-Level:**
| Metric | Value | Trend |
|--------|-------|-------|
| Commits | N | +X% vs last period |
| Tasks Completed | N | +X% vs last period |
| Avg Task Duration | N hours | -X% (faster) |
| On-time Completion | X% | +X% vs last period |

**Company-Wide (aggregated + per-project breakdown):**
| Metric | Company Total | web-app | api-server | mobile |
|--------|---------------|---------|------------|--------|
| Commits | N | n1 | n2 | n3 |
| Tasks Completed | N | n1 | n2 | n3 |
| Avg Task Duration | N hours | n1 | n2 | n3 |
| On-time Completion | X% | x1% | x2% | x3% |

### 3.2: Success Patterns
Analyze commit messages and completed work for patterns:

```
## What Worked Well

### Effective Practices
| Practice | Evidence | Impact | Projects |
|----------|----------|--------|----------|
| Atomic commits | 95% of commits single-purpose | Easier reviews, bisect | ALL |
| Early testing | Test commits preceded impl | Fewer regressions | web-app |
| Clear task specs | Low revision rate | First-time quality | api-server |

### High-Performing Areas
| Area | Project | Achievements |
|------|---------|--------------|
| Authentication | web-app | Completed 3 days early, zero bugs |
| API Design | api-server | Clean interfaces, reusable patterns |

### Notable Wins
- [Feature X] delivered under budget (web-app)
- [Agent Y] achieved 100% test coverage (api-server)
- [Team Z] resolved blocker in 2 hours (cross-project)
```

### 3.3: Collaboration Highlights

**Project-Level:**
Identify cross-team or cross-agent successes within the project.

**Company-Wide:**
Additionally identify cross-project collaboration:

```
### Collaboration Wins
| Employees | Collaboration | Outcome | Scope |
|-----------|---------------|---------|-------|
| eng-lead + ux-design | Early design review | No rework needed | web-app |
| api-dev + frontend | Contract-first API | Parallel development | cross-project |
| devops | Shared CI/CD pipeline | 3 projects use same workflow | company-wide |

### Cross-Project Synergies
| Pattern | Projects | Benefit |
|---------|----------|---------|
| Shared component library | web-app, mobile | 40% code reuse |
| Unified API contracts | api-server, web-app, mobile | Zero integration bugs |
| Common logging format | ALL | Simplified debugging |
```

## Step 4: Identify Issues

Analyze data for problems and areas needing improvement.

### 4.1: Escalations
Check for escalation indicators:
- Blocked items with long durations
- Multiple reassignments
- Timeout triggers
- Manual interventions

**Project-Level:**
```
## Issues & Challenges

### Escalations
| Item | Duration Blocked | Escalation Level | Resolution |
|------|------------------|------------------|------------|
| task-2.3 | 4 hours | Tier 2 | Awaiting external API |
| task-3.1 | 2 days | Tier 3 | Scope clarification needed |
```

**Company-Wide:**
```
### Escalations (All Projects)
| Project | Item | Duration Blocked | Escalation Level | Resolution |
|---------|------|------------------|------------------|------------|
| web-app | task-2.3 | 4 hours | Tier 2 | Awaiting external API |
| api-server | task-3.1 | 2 days | Tier 3 | Scope clarification needed |
| mobile | task-1.2 | 6 hours | Tier 2 | Missing design specs |

### Escalation Summary
| Project | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
|---------|--------|--------|--------|--------|
| web-app | 2 | 1 | 0 | 0 |
| api-server | 0 | 0 | 1 | 0 |
| mobile | 3 | 1 | 0 | 0 |
```

### 4.2: Failures & Reverts
Identify failed work:
- Reverted commits
- Failed tests that blocked progress
- Rejected reviews

```
### Failures
| Type | Count | Project | Examples | Root Cause |
|------|-------|---------|----------|------------|
| Reverts | 2 | web-app | feat: auth middleware | Incomplete requirements |
| Test Failures | 5 | api-server | API integration tests | Environment mismatch |
| Review Rejections | 3 | web-app | PR #42, #45 | Code style issues |
```

### 4.3: Slow Tasks
Identify tasks that took significantly longer than estimated:

```
### Slow Tasks (>2x Estimated)
| Task | Project | Estimated | Actual | Delay Factor | Cause |
|------|---------|-----------|--------|--------------|-------|
| task-1.5 | web-app | 2h | 8h | 4x | Undocumented dependency |
| task-2.2 | api-server | 4h | 12h | 3x | Scope creep mid-task |
```

### 4.4: Blockers & Dependencies
Track blocking relationships:

**Project-Level:**
```
### Blocking Analysis
| Blocker | Tasks Affected | Total Blocked Time |
|---------|----------------|-------------------|
| External API down | task-2.3, task-2.4 | 6 hours |
| Missing spec | task-3.1, task-3.2, task-3.3 | 12 hours |
```

**Company-Wide (includes cross-project blockers):**
```
### Cross-Project Blocking Analysis
| Blocker | Projects Affected | Tasks Affected | Total Blocked Time |
|---------|-------------------|----------------|-------------------|
| API contract change | web-app, mobile | 5 tasks | 18 hours |
| Shared auth service down | ALL | 12 tasks | 4 hours |
| Design system update | web-app, mobile | 3 tasks | 8 hours |
```

### 4.5: Knowledge Gaps
Identify areas where employees struggled:

```
### Knowledge Gaps Identified
| Area | Project(s) | Evidence | Impact |
|------|------------|----------|--------|
| GraphQL subscriptions | api-server | 3 tasks required research | +4h total |
| Docker networking | ALL | Multiple failed attempts | Blocked deployment |
| React Native animations | mobile | Repeated trial-and-error | +6h total |
```

### 4.6: Cross-Project Issues (Company-Wide Only)

**Only shown for `--company` retros:**

```
### Cross-Project Issues
| Issue | Projects | Impact | Recommendation |
|-------|----------|--------|----------------|
| Inconsistent error formats | api-server, web-app | Frontend parsing errors | Standardize error schema |
| Duplicate utility code | web-app, mobile | Maintenance burden | Extract shared library |
| Conflicting dependency versions | ALL | Build failures | Unified version policy |
```

## Step 5: Generate Action Items

Based on issues identified, generate specific, actionable improvements.

**For Project-Level Retro:**
Generate project-specific action items.

**For Company-Wide Retro:**
Generate both project-specific AND company-wide action items. Include cross-project improvements.

```
## Action Items

### High Priority (Address This Week)
| ID | Action | Owner | Project | Due | Related Issue |
|----|--------|-------|---------|-----|---------------|
| AI-001 | Document GraphQL subscription pattern | eng-lead | api-server | 3 days | Knowledge gap |
| AI-002 | Add pre-commit hook for style | devops | ALL | 2 days | Review rejections |
| AI-003 | Create external dependency checklist | architect | company-wide | 5 days | Blocker analysis |

### Medium Priority (Address This Month)
| ID | Action | Owner | Project | Due | Related Issue |
|----|--------|-------|---------|-----|---------------|
| AI-004 | Improve estimation process | team-lead | company-wide | 2 weeks | Slow tasks |
| AI-005 | Set up API mock server | integrations | api-server | 2 weeks | External API blocker |

### Low Priority (Backlog)
| ID | Action | Owner | Project | Related Issue |
|----|--------|-------|---------|---------------|
| AI-006 | Training: Docker networking | all-eng | company-wide | Knowledge gap |
| AI-007 | Review scope change process | product | company-wide | Scope creep |

### Process Improvements
| Category | Current | Proposed | Benefit | Scope |
|----------|---------|----------|---------|-------|
| Code Review | Manual style checks | Automated linting | Save 30min/day | ALL projects |
| Dependencies | Ad-hoc discovery | Pre-task checklist | Reduce blocks 50% | company-wide |
| Estimation | Single point | Range with confidence | Better planning | company-wide |
```

### Company-Wide Action Items (Only for `--company` retros)

```
### Cross-Project Initiatives
| ID | Initiative | Projects | Owner | Timeline | Expected Benefit |
|----|------------|----------|-------|----------|------------------|
| CPI-001 | Shared component library | web-app, mobile | frontend-lead | 1 month | 40% code reuse |
| CPI-002 | Unified API error schema | api-server, web-app, mobile | api-lead | 2 weeks | Zero parsing errors |
| CPI-003 | Common dependency matrix | ALL | devops | 1 week | No version conflicts |

### Knowledge Sharing Actions
| ID | Knowledge Area | From Project | To Project(s) | Format |
|----|----------------|--------------|---------------|--------|
| KS-001 | GraphQL subscription pattern | api-server | company-wide | Doc + brown bag |
| KS-002 | React Native animation | mobile | web-app | Pair session |
| KS-003 | CI/CD optimization | devops | ALL | Template update |
```

## Step 6: Update Knowledge Base

**If not `--dry-run`:**

### 6.1: Create/Update Retro Log

**For Project-Level Retro:**
Append to `.company/knowledge/retros.md` with project context:

```markdown
---

## Retro: YYYY-MM-DD

**Type:** project-level
**Project:** <project-id> (<project-name>)
**Period:** YYYY-MM-DD to YYYY-MM-DD
**Scope:** all | department | team
**Facilitator:** system

### Summary
- Commits: N
- Tasks Completed: N
- Velocity: +X% vs last period
- Issues Identified: N
- Action Items: N

### Key Wins
- [Top 3 wins from analysis]

### Key Issues
- [Top 3 issues from analysis]

### Action Items Created
- AI-XXX: [description]
- AI-XXX: [description]
- AI-XXX: [description]

### Follow-up from Previous Retro
| Action Item | Status | Notes |
|-------------|--------|-------|
| AI-prev-001 | Complete | Implemented in PR #X |
| AI-prev-002 | In Progress | 70% done |
| AI-prev-003 | Deferred | Deprioritized |
```

**For Company-Wide Retro:**
Append to `.company/knowledge/retros.md` with company-wide context:

```markdown
---

## Company-Wide Retro: YYYY-MM-DD

**Type:** company-wide
**Projects Included:** N
  - web-app (45 commits)
  - api-server (32 commits)
  - mobile (28 commits)
**Period:** YYYY-MM-DD to YYYY-MM-DD
**Scope:** all | department | team
**Facilitator:** system

### Company Summary
| Metric | Total | Best Project | Needs Attention |
|--------|-------|--------------|-----------------|
| Commits | 105 | web-app (45) | mobile (28) |
| Tasks | 42 | api-server (18) | mobile (10) |
| Velocity | +15% | web-app (+25%) | mobile (-5%) |

### Project Summaries
<details>
<summary>web-app</summary>

- Commits: 45
- Key wins: Auth system, UI refresh
- Issues: Test coverage gaps
</details>

<details>
<summary>api-server</summary>

- Commits: 32
- Key wins: API performance
- Issues: Documentation debt
</details>

### Cross-Project Wins
- Unified CI/CD saved 2h/week across all projects
- Shared component library reduced frontend duplication by 40%

### Cross-Project Issues
- API contract changes caused downstream breaks
- Inconsistent error handling across services

### Company-Wide Action Items
- CPI-XXX: [cross-project initiative]
- AI-XXX: [company-wide action]

### Knowledge Transfer
| From | To | Topic |
|------|-----|-------|
| api-server | company-wide | GraphQL patterns |
| mobile | web-app | Animation techniques |
```

### 6.2: Update Patterns

If new successful patterns identified, add to `.company/knowledge/patterns.md`:

```markdown
### [New Pattern Name]

**Category:** [Category]
**Discovered:** YYYY-MM-DD (Retro)
**Origin Project:** <project-id> | company-wide

**Context:** When this pattern applies

**Pattern:** Description of the approach

**Evidence:** How we know this works (from retro data)

**Applicable To:** [specific project | ALL projects]

**See also:** Related patterns
```

### 6.3: Update Employee Learnings

For employees with notable learnings, update `.company/employees/<department>/<employee-id>/learnings.md`:

```markdown
## Retro Learning: YYYY-MM-DD

**Retro Type:** project-level | company-wide
**Project(s):** <project-id(s)>

### What Worked
- [Employee-specific wins]

### What to Improve
- [Employee-specific improvements]

### Cross-Project Insights (company-wide retro only)
- [Insights from working across projects]

### Action Items Assigned
- AI-XXX: [description]
```

### 6.4: Update Project-Specific Learnings (Company-Wide Only)

For company-wide retros, also update each project's learning file at `<project-path>/.planning/LEARNINGS.md`:

```markdown
## Company Retro: YYYY-MM-DD

### Project Performance vs Company
| Metric | This Project | Company Avg | Delta |
|--------|--------------|-------------|-------|
| Velocity | +25% | +15% | +10% |
| Quality | 95% | 92% | +3% |

### Learnings to Adopt from Other Projects
- [pattern from project-X]
- [practice from project-Y]

### Learnings to Share with Company
- [this project's successful pattern]

### Project-Specific Action Items from Company Retro
- AI-XXX: [action specific to this project]
```

## Step 7: Output Retrospective Report

### 7.1: Project-Level Retro Report

```
## Project Retrospective

===============================================================================
 PROJECT RETRO REPORT                                            [YYYY-MM-DD]
===============================================================================
 Project: <project-name> (<project-id>)
 Path: /path/to/project
 Period: YYYY-MM-DD to YYYY-MM-DD (N days)
 Scope: all | department: X | team: X
 Previous Retro: YYYY-MM-DD | First retro
===============================================================================

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Total Commits | N | [+/-X%] |
| Tasks Completed | N | [+/-X%] |
| Avg Velocity | N/day | [+/-X%] |
| Issues Identified | N | - |
| Action Items Created | N | - |

### Health Score: [X/10]

| Category | Score | Notes |
|----------|-------|-------|
| Velocity | X/10 | [assessment] |
| Quality | X/10 | [assessment] |
| Collaboration | X/10 | [assessment] |
| Knowledge | X/10 | [assessment] |

===============================================================================
 WHAT WORKED WELL
===============================================================================

[Success patterns section from Step 3]

===============================================================================
 ISSUES & CHALLENGES
===============================================================================

[Issues section from Step 4]

===============================================================================
 ACTION ITEMS
===============================================================================

[Action items from Step 5]

===============================================================================
 FOLLOW-UP FROM PREVIOUS RETRO
===============================================================================

| Previous Action | Status | Resolution |
|-----------------|--------|------------|
| AI-prev-001 | Complete | [notes] |
| AI-prev-002 | In Progress | [notes] |
| AI-prev-003 | Not Started | [notes] |

===============================================================================
 KNOWLEDGE BASE UPDATES
===============================================================================

| Update Type | Count | Details |
|-------------|-------|---------|
| Retro Log Entry | 1 | .company/knowledge/retros.md |
| New Patterns | N | [pattern names] |
| Employee Learnings | N | [employee IDs] |

===============================================================================

### Recommendations

1. **Immediate:** [Most urgent action item]
2. **This Week:** [High priority items]
3. **This Month:** [Medium priority items]

### Next Retro

Suggested date: YYYY-MM-DD (in N days)
Scope: [recommendation based on current retro]

===============================================================================
 END OF PROJECT RETRO REPORT
===============================================================================
```

### 7.2: Company-Wide Retro Report

```
## Company-Wide Retrospective

===============================================================================
 COMPANY RETRO REPORT                                            [YYYY-MM-DD]
===============================================================================
 Company: <company-name>
 Projects: N registered, N active
 Period: YYYY-MM-DD to YYYY-MM-DD (N days)
 Scope: all | department: X | team: X
 Previous Company Retro: YYYY-MM-DD | First company retro
===============================================================================

### Projects Included

| ID | Name | Status | Commits | Tasks | Health |
|----|------|--------|---------|-------|--------|
| web-app | Web Application | active | 45 | 15 | 8/10 |
| api-server | API Server | active | 32 | 12 | 7/10 |
| mobile | Mobile App | active | 28 | 10 | 6/10 |
| **TOTAL** | - | - | **105** | **37** | **7/10** |

===============================================================================
 COMPANY EXECUTIVE SUMMARY
===============================================================================

| Metric | Company Total | Best | Needs Attention |
|--------|---------------|------|-----------------|
| Total Commits | 105 | web-app (45) | mobile (28) |
| Tasks Completed | 37 | web-app (15) | mobile (10) |
| Avg Velocity | 5.3/day | web-app (6.4) | mobile (4.0) |
| Issues | 12 | api-server (2) | mobile (6) |

### Company Health Score: [X/10]

| Category | Score | Best Project | Needs Work |
|----------|-------|--------------|------------|
| Velocity | X/10 | web-app | mobile |
| Quality | X/10 | api-server | web-app |
| Collaboration | X/10 | - | - |
| Knowledge Sharing | X/10 | - | - |
| Cross-Project Alignment | X/10 | - | - |

===============================================================================
 PROJECT SUMMARIES
===============================================================================

### web-app (Health: 8/10)
| Metric | Value | Trend |
|--------|-------|-------|
| Commits | 45 | +25% |
| Tasks | 15 | +20% |
| Key Win | Auth system complete | - |
| Top Issue | Test coverage gaps | - |

### api-server (Health: 7/10)
| Metric | Value | Trend |
|--------|-------|-------|
| Commits | 32 | +10% |
| Tasks | 12 | +15% |
| Key Win | API performance | - |
| Top Issue | Documentation debt | - |

### mobile (Health: 6/10)
| Metric | Value | Trend |
|--------|-------|-------|
| Commits | 28 | -5% |
| Tasks | 10 | 0% |
| Key Win | Launch ready | - |
| Top Issue | Animation performance | - |

===============================================================================
 CROSS-PROJECT ANALYSIS
===============================================================================

### Cross-Project Wins
| Win | Projects | Impact |
|-----|----------|--------|
| Shared CI/CD pipeline | ALL | 2h/week saved |
| Component library reuse | web-app, mobile | 40% code reuse |
| Unified API contracts | ALL | Zero integration bugs |

### Cross-Project Issues
| Issue | Projects Affected | Impact | Priority |
|-------|-------------------|--------|----------|
| Inconsistent error handling | api-server, web-app | Frontend errors | High |
| Dependency version conflicts | ALL | Build failures | High |
| Duplicate utility code | web-app, mobile | Tech debt | Medium |

### Knowledge Sharing Opportunities
| Knowledge | Source | Target | Format |
|-----------|--------|--------|--------|
| GraphQL patterns | api-server | company-wide | Doc |
| Animation techniques | mobile | web-app | Pair session |
| CI optimization | devops | ALL | Template |

===============================================================================
 WHAT WORKED WELL (COMPANY-WIDE)
===============================================================================

[Aggregated success patterns from Step 3]

===============================================================================
 ISSUES & CHALLENGES (COMPANY-WIDE)
===============================================================================

[Aggregated issues from Step 4]

===============================================================================
 ACTION ITEMS
===============================================================================

### Project-Specific Actions
[Per-project action items from Step 5]

### Company-Wide Initiatives
[Cross-project initiatives from Step 5]

### Knowledge Transfer Actions
[Knowledge sharing actions from Step 5]

===============================================================================
 FOLLOW-UP FROM PREVIOUS COMPANY RETRO
===============================================================================

| Previous Action | Project | Status | Resolution |
|-----------------|---------|--------|------------|
| CPI-prev-001 | company-wide | Complete | Shared library deployed |
| AI-prev-001 | web-app | In Progress | 70% done |
| AI-prev-002 | api-server | Not Started | Blocked by spec |

===============================================================================
 KNOWLEDGE BASE UPDATES
===============================================================================

| Update Type | Count | Details |
|-------------|-------|---------|
| Company Retro Log | 1 | .company/knowledge/retros.md |
| New Patterns | N | [pattern names] |
| Employee Learnings | N | [employee IDs] |
| Project Learnings | N | [project IDs] |

===============================================================================

### Recommendations

1. **Immediate (This Week):**
   - [Most urgent cross-project action]
   - [Critical project-specific action]

2. **Short-Term (This Month):**
   - [Knowledge transfer priority]
   - [Process improvement]

3. **Strategic (This Quarter):**
   - [Cross-project initiative]
   - [Company-wide improvement]

### Attention Required

| Project | Issue | Owner | Urgency |
|---------|-------|-------|---------|
| mobile | Velocity decline | mobile-lead | Immediate |
| web-app | Test coverage | qa-lead | This week |

### Next Retros

| Type | Suggested Date | Scope |
|------|----------------|-------|
| Company-wide | YYYY-MM-DD | All projects |
| web-app | YYYY-MM-DD | Project |
| api-server | YYYY-MM-DD | Project |
| mobile | YYYY-MM-DD | Project (focus: velocity) |

===============================================================================
 END OF COMPANY RETRO REPORT
===============================================================================
```

## Step 8: Handle Dry Run

If `--dry-run` was specified:

### 8.1: Project-Level Dry Run

```
## Dry Run Mode (Project-Level)

===============================================================================
 RETRO PREVIEW (NO CHANGES MADE)
===============================================================================

[Full project-level report as above]

===============================================================================
 CHANGES THAT WOULD BE MADE
===============================================================================

### Files to Create/Update
| File | Action | Changes |
|------|--------|---------|
| .company/knowledge/retros.md | append | New project retro entry |
| .company/knowledge/patterns.md | append | N new patterns |
| .company/employees/engineering/eng-lead/learnings.md | append | Learning entry |

### To Apply Changes
Run without --dry-run flag:
  /company-retro --since=YYYY-MM-DD

===============================================================================
```

### 8.2: Company-Wide Dry Run

```
## Dry Run Mode (Company-Wide)

===============================================================================
 RETRO PREVIEW (NO CHANGES MADE)
===============================================================================

[Full company-wide report as above]

===============================================================================
 CHANGES THAT WOULD BE MADE
===============================================================================

### Company Knowledge Base
| File | Action | Changes |
|------|--------|---------|
| .company/knowledge/retros.md | append | Company-wide retro entry |
| .company/knowledge/patterns.md | append | N new patterns |

### Employee Learnings
| File | Action |
|------|--------|
| .company/employees/engineering/eng-lead/learnings.md | append |
| .company/employees/engineering/frontend-dev/learnings.md | append |
| .company/employees/design/ux-lead/learnings.md | append |

### Project-Specific Updates
| Project | File | Action |
|---------|------|--------|
| web-app | .planning/LEARNINGS.md | append/create |
| api-server | .planning/LEARNINGS.md | append/create |
| mobile | .planning/LEARNINGS.md | append/create |

### To Apply Changes
Run without --dry-run flag:
  /company-retro --company --since=YYYY-MM-DD

===============================================================================
```

## Rules

- **Always show previous retro follow-up.** Track action items across retros for accountability.
- **Be data-driven.** Base all observations on actual evidence from git, work items, and employee logs.
- **Prioritize action items.** Every identified issue should have a corresponding action item with owner and due date.
- **Update knowledge incrementally.** Only add patterns that showed clear evidence of success.
- **Respect scope filters.** If filtering by department or team, only include relevant data.
- **Handle missing data gracefully.** If git history or work items are sparse, note limitations and proceed.
- **Support dry-run for safety.** Let users preview changes before committing to knowledge base.
- **Calculate trends when possible.** Compare against previous retro period for meaningful insights.
- **Generate unique action item IDs.** Use format AI-NNNN where NNNN increments globally.
- **Assign owners to all action items.** Default to relevant team lead or department head.

### Multi-Project Mode Rules

- **Default to project-level.** Without `--company` flag, always run project-level retro.
- **Auto-detect project context.** Match current working directory to registered project paths.
- **Aggregate intelligently.** For company-wide retros, aggregate metrics but preserve project-level insights.
- **Track cross-project patterns.** Identify and highlight successful patterns that span projects.
- **Identify cross-project blockers.** Surface dependencies and blockers that affect multiple projects.
- **Generate cross-project initiatives (CPI).** Use CPI-NNNN format for company-wide action items.
- **Enable knowledge transfer.** Identify opportunities to share learnings between projects.
- **Compare project health.** Show relative performance to highlight best practices and areas needing attention.
- **Update project-specific learnings.** For company-wide retros, write learnings back to each project.
- **Support `--project=<id>` flag.** Allow running retro for specific project without navigating to it.

### Single-Project Mode Behavior

When `org.json.mode` is `single-project`:
- Ignore `--company` and `--project` flags (they don't apply)
- Run retro as if it's a project-level retro (classic behavior)
- All data gathering is local to the single codebase
- No cross-project analysis needed
