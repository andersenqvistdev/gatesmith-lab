# /help — Command Reference and Discoverability

Display organized command reference with 36+ commands grouped into 7 categories. Supports filtering by category and detailed help for specific commands.

## Input
$ARGUMENTS

## Command Syntax

```
/help                     # Show all commands by category
/help [category]          # Show commands in a specific category
/help [command-name]      # Show detailed help for a command
```

**Categories:** workflow, quick, company, multi-project, visibility, agents, advanced

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the help mode:

| Pattern | Mode | Go to |
|---------|------|-------|
| (empty) | FULL | Step 1 |
| `workflow` | CATEGORY | Step 2A |
| `quick` | CATEGORY | Step 2B |
| `company` | CATEGORY | Step 2C |
| `multi-project` | CATEGORY | Step 2D |
| `visibility` | CATEGORY | Step 2E |
| `agents` | CATEGORY | Step 2F |
| `advanced` | CATEGORY | Step 2G |
| `[command-name]` | COMMAND | Step 3 |

---

## Step 1: Full Command Reference

Display all commands organized by category:

```
================================================================================
  GATESMITH COMMAND REFERENCE                                         [25 core + 52 experimental]
================================================================================

  Gatesmith combines three frameworks:
  - Gatesmith Security: deterministic hooks, secret scanning, trust tiers
  - GSD Execution: persistent planning, atomic commits, session resume
  - BMAD Planning: scale-adaptive intelligence, planner-checker loops

  Use /help [category] for details or /help [command] for specific help.

================================================================================

+------------------------------------------------------------------------------+
|  1. CORE WORKFLOW (GSD Pipeline)                                             |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command         Description                                                 |
|  --------------- ----------------------------------------------------------- |
|  /new-project    Initialize project with context detection and .planning/   |
|  /discuss        Capture requirements and preferences before planning       |
|  /plan           Planner-Checker loop with XML task format                   |
|  /build          Wave-based execution with atomic commits (full pipeline)   |
|  /review         Code review with reviewer agent                             |
|  /gate           Human security checkpoint before push                       |
|  /verify         Verify all planned work is complete                         |
|  /complete       Mark milestone, update planning docs                        |
|  /continue       Resume previous session from STATE.md                       |
|                                                                              |
|  Typical flow: /new-project -> /discuss -> /plan -> /build                   |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  2. QUICK ACTIONS                                                            |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command         Description                                                 |
|  --------------- ----------------------------------------------------------- |
|  /feature        Fully autonomous: plan->build->review->test->commit         |
|  /docs           Autonomous documentation generation                         |
|  /prime          Load full project context (token-optimized)                 |
|  /map-codebase   Parallel codebase exploration with multiple agents          |
|                                                                              |
|  For quick work: /feature "description" handles everything                   |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  3. COMPANY EXTENSION (Single-Project)                                       |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command              Description                                            |
|  -------------------- ------------------------------------------------------ |
|  /company-bootstrap   Intelligent setup - detects domain, hires team         |
|  /company-init        Initialize .company/ directory structure               |
|  /company-status      Display org chart, agent statuses, work items          |
|  /company-hire        Hire employee with specific skills                     |
|  /company-assign      Assign employee to project                             |
|  /company-dismiss     Dismiss employee, extract learnings, archive           |
|  /company-request     Submit work to the organization                        |
|  /company-retro       Run organizational retrospective                       |
|  /company-reorg       Reorganize departments, teams, roles                   |
|  /company-knowledge   Query ADRs and implementation patterns                 |
|  /knowledge-search    Semantic knowledge search (Claude-powered)             |
|  /company-upgrade     Upgrade v1.1 to multi-project structure                |
|                                                                              |
|  Quick start: /company-bootstrap "Building a SaaS analytics platform"        |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  HUMAN INPUT                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command         Description                                                 |
|  --------------- ----------------------------------------------------------- |
|  /submit         Submit work request to organization                         |
|  /respond        Respond to escalation requiring human input                 |
|  /pending        List items requiring human attention                        |
|  /run-loop       Autonomous execution loop with human checkpoints            |
|                                                                              |
|  Example: /submit "Add OAuth2 integration" --priority=high                   |
|  Example: /run-loop --max-tasks=5 --until-idle                               |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  4. MULTI-PROJECT COMPANY                                                    |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command              Description                                            |
|  -------------------- ------------------------------------------------------ |
|  /company-create      Create multi-project company root                      |
|  /company-add-project Link existing project to company                       |
|  /company-projects    List all registered projects                           |
|  /company-init        (with --upgrade) Migrate to multi-project              |
|                                                                              |
|  Multi-project enables shared employees across projects.                     |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  5. OPERATIONAL VISIBILITY                                                   |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command           Description                                               |
|  ----------------- --------------------------------------------------------- |
|  /dashboard        Quick operational health snapshot                         |
|  /company-health   Deep management insights report                           |
|  /employee-status  Workforce overview and individual status                  |
|  /lifecycle        Company lifecycle phase management                        |
|                                                                              |
|  Daily check: /dashboard for quick status                                    |
|  Deep dive: /company-health for strategic insights                           |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  6. AGENT CREATION                                                           |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command      Description                                                    |
|  ------------ -------------------------------------------------------------- |
|  /agent       Create, list, activate, or archive agents (company-aware)     |
|  /add-agent   [DEPRECATED] Create specialized agent (use /agent instead)    |
|                                                                              |
|  Examples:                                                                   |
|    /agent "GraphQL specialist for schema design"                             |
|    /agent --list                                                             |
|    /agent --activate graphql-specialist                                      |
|                                                                              |
+------------------------------------------------------------------------------+

+------------------------------------------------------------------------------+
|  7. ADVANCED                                                                 |
+------------------------------------------------------------------------------+
|                                                                              |
|  Command          Description                                                |
|  --------------- ----------------------------------------------------------- |
|  /security-audit  Full OWASP security audit                                  |
|  /workshop        Collaborative multi-participant sessions                   |
|  /align           Verify organizational alignment with vision                |
|                                                                              |
|  /security-audit runs dependency audit tools and checks OWASP Top 10         |
|  /workshop runs discussion, planning, learning, or brainstorm sessions       |
|  /align identifies knowledge gaps and triggers educational interventions     |
|                                                                              |
+------------------------------------------------------------------------------+

================================================================================
  TIPS
================================================================================

  Getting Started:
    - Fresh install? Start with /prime to see what's available
    - New project? Use /new-project to set up .planning/ files
    - Quick feature? Use /feature "description" for autonomous execution

  Company Extension:
    - /company-bootstrap is the easiest way to set up a company
    - Use /company-request to submit work to your AI organization
    - /dashboard gives quick health status; /company-health for deep insights

  Workflow:
    - The GSD pipeline: discuss -> plan -> build -> review -> gate -> complete
    - /build runs the FULL autonomous pipeline including verify, gate, complete
    - /continue resumes from where you left off

  More Help:
    - /help [category]  - Show commands in a category
    - /help [command]   - Show detailed help for a command

================================================================================
```

---

## Step 2A: Workflow Category

```
================================================================================
  CORE WORKFLOW (GSD Pipeline)                                      [9 commands]
================================================================================

  The GSD (Get Stuff Done) pipeline provides structured autonomy:
  discuss -> plan -> build -> review -> gate -> verify -> complete

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /new-project
  -----------
  Initialize a new project with full context detection.
  Creates .planning/ directory with PROJECT.md, REQUIREMENTS.md, ROADMAP.md,
  STATE.md, and DISCUSS.md. Detects complexity and recommends workflow.

  Usage: /new-project [description]

  /discuss
  --------
  Capture requirements and preferences BEFORE planning. Prevents rework by
  ensuring alignment on goals, constraints, and preferences.

  Usage: /discuss [topic]

  /plan
  -----
  Planner-Checker loop with XML task format. Spawns Architect agent to design
  plan, then Plan Checker to validate. Groups tasks into dependency waves.

  Usage: /plan [feature description]

  /build
  ------
  Full autonomous pipeline: build -> verify -> gate -> complete.
  Executes tasks wave by wave with atomic commits. Each task = one commit.
  Includes CTO technical review and CEO phase validation.

  Usage: /build

  /review
  -------
  Spawn Reviewer agent to analyze code changes. Reviews staged/unstaged
  changes or specified files. Highlights critical issues.

  Usage: /review [files]

  /gate
  -----
  Human security checkpoint. Presents changes for approval before push.
  Quick security scan, operation count, and action choices.
  On APPROVE, unlocks GitHub operations (PR create, push to feature branch).

  Usage: /gate

  /verify
  -------
  Verify all planned work is complete. Checks each task in ROADMAP.md,
  verifies commits exist, runs quality checks and security scan.

  Usage: /verify

  /complete
  ---------
  Mark milestone complete. Updates planning docs, creates summary commit.
  Reports accomplishments and recommends next steps.

  Usage: /complete

  /continue
  ---------
  Resume previous session from STATE.md. Loads context, verifies git state,
  and continues with next task.

  Usage: /continue

================================================================================
  TYPICAL WORKFLOW
================================================================================

  1. /new-project "My app"     # Initialize project
  2. /discuss                  # Capture requirements
  3. /plan "Build auth system" # Design implementation
  4. /build                    # Execute (runs full pipeline)
  5. git push                  # Push changes

================================================================================
```

---

## Step 2B: Quick Actions Category

```
================================================================================
  QUICK ACTIONS                                                     [4 commands]
================================================================================

  For fast, autonomous execution without the full workflow ceremony.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /feature
  --------
  Fully autonomous feature development. From a single description:
  plan -> build -> review -> test -> commit

  Detects complexity and adjusts pipeline:
  - Trivial: implement directly
  - Standard: spawn Architect, then implement
  - Complex: full planner-checker loop with specialists

  Usage: /feature "Add user authentication with JWT"
         /feature "Refactor payment module"

  /docs
  -----
  Autonomous documentation generation. Explores codebase with parallel agents,
  creates docs-writer agent if needed, generates API docs, architecture guides.

  Usage: /docs                    # Full project documentation
         /docs "API endpoints"    # Scoped documentation

  /prime
  ------
  Load full project context before complex work. Token-optimized: detects
  task type (planning/building/debugging/reviewing) and loads only needed docs.

  Usage: /prime                   # Auto-detect task type
         /prime --full            # Load all documents
         /prime --building        # Force building mode

  /map-codebase
  -------------
  Deep codebase mapping with parallel agents. Maps structure, APIs, models,
  test patterns. Updates .planning/PROJECT.md with findings.

  Usage: /map-codebase

================================================================================
  QUICK START EXAMPLES
================================================================================

  "Add dark mode to the dashboard"
    -> /feature "Add dark mode to the dashboard"

  "Document the API"
    -> /docs "API endpoints"

  "I need to understand this codebase"
    -> /prime or /map-codebase

================================================================================
```

---

## Step 2C: Company Extension Category

```
================================================================================
  COMPANY EXTENSION (Single-Project)                               [15 commands]
================================================================================

  Create an AI organization with persistent employees, accumulated knowledge,
  and coordinated work queues.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /company-bootstrap
  ------------------
  Intelligent company setup. Analyzes project goals, detects domain, selects
  template, and hires appropriate team automatically.

  Usage: /company-bootstrap "Building a SaaS analytics dashboard"
         /company-bootstrap --template saas_platform
         /company-bootstrap --discover  # Research mode
         /company-bootstrap --list-templates

  /company-init
  -------------
  Initialize .company/ directory structure. Creates org.json, knowledge base,
  employee directories. Use /company-bootstrap for intelligent setup.

  Usage: /company-init
         /company-init --departments=eng,product,design

  /company-status
  ---------------
  Display org chart, agent statuses, active work items, knowledge metrics.

  Usage: /company-status
         /company-status --agents
         /company-status --work

  /company-hire
  -------------
  Hire new employee with specific skills. Generated by Meta-Agent, assigned
  to department, given working memory.

  Usage: /company-hire "performance optimization specialist"
         /company-hire "database expert" --department=engineering

  /company-assign
  ---------------
  Assign employee to project. Employees can work across multiple projects.

  Usage: /company-assign [employee-id] [project-id]
         /company-assign --unassign [employee-id] [project-id]
         /company-assign --list [employee-id]

  /company-dismiss
  ----------------
  Dismiss employee, extract learnings, archive work. Core employees require
  --force flag.

  Usage: /company-dismiss [employee-id]
         /company-dismiss [employee-id] --force

  /company-request
  ----------------
  Submit work to the organization. Primary human-to-company interface.
  Request is decomposed, allocated, and executed by employees.

  Usage: /company-request "Build REST API for user authentication"
         /company-request "Add dark mode" --project=dashboard

  /company-retro
  --------------
  Run organizational retrospective. Analyzes completed work, identifies
  patterns and issues, generates action items, updates knowledge base.

  Usage: /company-retro
         /company-retro --since=2024-01-01
         /company-retro --company  # Company-wide (multi-project)

  /company-reorg
  --------------
  Reorganize company structure. Add/archive departments, reassign agents,
  change roles.

  Usage: /company-reorg add-dept security
         /company-reorg reassign [agent] [new-team]
         /company-reorg promote [agent] [new-role]

  /company-knowledge
  ------------------
  Query organizational knowledge base. View ADRs, implementation patterns.

  Usage: /company-knowledge list
         /company-knowledge search "authentication"
         /company-knowledge category security

  /knowledge-search
  -----------------
  Semantic knowledge search using Claude's understanding. Finds conceptually
  related entries, ranks by relevance with scores and reasoning.

  Usage: /knowledge-search "authentication patterns"
         /knowledge-search "error handling" --type=pattern
         /knowledge-search "API design" --project=gatesmith

  /company-upgrade
  ----------------
  Upgrade single-project company (v1.1) to multi-project structure (v1.2).

  Usage: /company-upgrade
         /company-upgrade --dry-run
         /company-upgrade --rollback

+------------------------------------------------------------------------------+
|  HUMAN INPUT                                                                 |
+------------------------------------------------------------------------------+

  /submit
  -------
  Submit a work request to the organization. Creates a tracked request that
  flows through the organization's work queue for decomposition and execution.

  Usage: /submit "Add OAuth2 integration"
         /submit "Fix login page bug" --priority=high
         /submit "Refactor database layer" --assignee=db-specialist

  /respond
  --------
  Respond to an escalation requiring human input. When employees encounter
  decisions that need human judgment, they escalate and wait for response.

  Usage: /respond [escalation-id]
         /respond ESC-001 "Approve the database schema change"
         /respond --list  # Show pending escalations

  /pending
  --------
  List all items requiring human attention. Shows pending escalations,
  blocked work items, and requests awaiting approval.

  Usage: /pending
         /pending --escalations  # Only escalations
         /pending --approvals    # Only pending approvals
         /pending --all          # Include resolved items

  /run-loop
  ---------
  Autonomous execution loop with human checkpoints. Processes work queue
  continuously until idle, task limit reached, or human intervention needed.

  Usage: /run-loop                      # Run until idle
         /run-loop --max-tasks=5        # Stop after 5 tasks
         /run-loop --until-idle         # Run until no pending work
         /run-loop --checkpoint=3       # Human review every 3 tasks

================================================================================
  GETTING STARTED
================================================================================

  The easiest way to set up a company:

    /company-bootstrap "Building a B2B analytics SaaS"

  This will:
  1. Detect your domain (SaaS platform)
  2. Select appropriate template
  3. Create company structure
  4. Hire core team (architect, developer, PM, designer)
  5. Show next steps

================================================================================
```

---

## Step 2D: Multi-Project Category

```
================================================================================
  MULTI-PROJECT COMPANY                                             [4 commands]
================================================================================

  Enable employees to work across multiple projects with shared knowledge
  and coordinated work queues.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /company-create
  ---------------
  Create a multi-project company root. Different from /company-init which
  creates single-project company. Creates .forge-company-root marker.

  Usage: /company-create
         /company-create --name "Acme Corp"
         /company-create --with-projects  # Create projects/ subdirectory

  /company-add-project
  --------------------
  Register existing Gatesmith project with multi-project company. Creates
  assignment file linking project to company's employee and work queues.

  Usage: /company-add-project ./path/to/project
         /company-add-project .  # Current directory
         /company-add-project --name "Dashboard App"

  /company-projects
  -----------------
  List all projects registered with the company.

  Usage: /company-projects
         /company-projects --detail  # Show employee assignments
         /company-projects --json    # Machine-readable output

  /company-init --upgrade
  -----------------------
  Migrate existing single-project company to multi-project structure.

  Usage: /company-init --upgrade

================================================================================
  MULTI-PROJECT SETUP
================================================================================

  1. Create company root (in parent directory):
     cd ~/projects
     /company-create --name "My Company" --with-projects

  2. Add existing projects:
     /company-add-project ./project-a
     /company-add-project ./project-b

  3. Employees now work across all projects:
     /company-assign dev-001 project-a
     /company-assign dev-001 project-b

================================================================================
```

---

## Step 2E: Visibility Category

```
================================================================================
  OPERATIONAL VISIBILITY                                            [4 commands]
================================================================================

  Monitor organizational health, workforce status, and lifecycle phase.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /dashboard
  ----------
  Quick operational health snapshot. Shows health score, progress, workforce
  utilization, and active alerts.

  Usage: /dashboard
         /dashboard --health     # Health section only
         /dashboard --progress   # Progress section only
         /dashboard --agents     # Workforce section only
         /dashboard --alerts     # Alerts section only

  /company-health
  ---------------
  Deep management insights report. Executive summary, delivery forecasts,
  workforce health, risk assessment, historical trends, AI recommendations.

  Usage: /company-health
         /company-health --delivery   # Focus on delivery
         /company-health --workforce  # Focus on team health
         /company-health --risks      # Focus on risk assessment
         /company-health --trends     # Focus on historical trends

  /employee-status
  ----------------
  Workforce overview and individual employee details. Shows assignments,
  workload, activity history.

  Usage: /employee-status                    # All employees
         /employee-status [employee-id]      # Specific employee
         /employee-status --department eng   # Filter by department
         /employee-status --idle             # Only idle employees
         /employee-status --overloaded       # Overloaded employees

  /lifecycle
  ----------
  Company lifecycle phase management. Display current phase, metrics,
  request transitions, view history.

  Phases: startup -> growth -> scale -> mature -> decline_pivot

  Usage: /lifecycle                       # Current phase and metrics
         /lifecycle status                # Detailed phase status
         /lifecycle transition growth     # Request phase transition
         /lifecycle history               # Phase transition history

================================================================================
  DAILY OPERATIONS
================================================================================

  Quick check:
    /dashboard

  Deep dive:
    /company-health

  Team status:
    /employee-status

================================================================================
```

---

## Step 2F: Agents Category

```
================================================================================
  AGENT CREATION                                                    [2 commands]
================================================================================

  Create specialized AI agents for specific domains or tasks.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /agent
  ------
  Unified agent management with company awareness. Create, list, activate,
  or archive agents. In company mode, agents are tracked with persistent
  memory and skill-based reuse.

  Create new agent:
    /agent "GraphQL specialist for schema design and resolver optimization"
    /agent "Database migration expert for PostgreSQL"

  List agents:
    /agent --list

  Reactivate archived agent:
    /agent --activate graphql-specialist

  Archive active agent:
    /agent --archive db-migration-expert

  Features:
  - Searches for matching existing consultants before creating new ones
  - Preserves memory across activations (company mode)
  - Skill-based matching for reuse
  - Automatic registration in company org chart

  /add-agent
  ----------
  [DEPRECATED] Use /agent instead.

  This command creates specialized agents but without company integration.
  Will continue to work but may be removed in future versions.

  Usage: /add-agent "A database migration specialist..."

================================================================================
  AGENT EXAMPLES
================================================================================

  Performance specialist:
    /agent "Performance profiler for identifying N+1 queries, unnecessary
    re-renders, and unbounded operations"

  Documentation writer:
    /agent "Documentation writer that reads source code and generates
    API docs following JSDoc conventions"

  Security auditor:
    /agent "Security specialist for OWASP vulnerabilities and
    dependency auditing"

================================================================================
```

---

## Step 2G: Advanced Category

```
================================================================================
  ADVANCED                                                          [3 commands]
================================================================================

  Security auditing, collaborative workshops, and organizational alignment.

+------------------------------------------------------------------------------+
|  COMMANDS                                                                    |
+------------------------------------------------------------------------------+

  /security-audit
  ---------------
  Full OWASP Top 10 security audit. Spawns Security Auditor agent, runs
  dependency audit tools (npm audit, pip-audit), checks git history for
  leaked secrets.

  Usage: /security-audit                  # Full project audit
         /security-audit src/auth/        # Audit specific directory
         /security-audit --fix            # Audit and fix critical issues

  Findings are categorized: CRITICAL, HIGH, MEDIUM, LOW

  /workshop
  ---------
  Collaborative multi-participant sessions. Engages employees for problem-
  solving, knowledge sharing, planning, and educational sessions.

  Types:
  - discussion: Open-ended exploration
  - planning: Sprint/milestone planning
  - learning: Educational sessions
  - alignment: Vision and roadmap alignment
  - brainstorm: Creative ideation
  - decision: Decision-making with ADR output

  Usage: /workshop "API Design Standards"
         /workshop "Sprint Planning" --scope=team:core --type=planning
         /workshop "GraphQL Best Practices" --type=learning
         /workshop "Company Vision Q1" --type=alignment --scope=company

  /align
  ------
  Verify organizational alignment with vision and roadmap. Identifies
  knowledge gaps, misalignments, triggers educational interventions.

  Checks:
  - Vision understanding
  - Roadmap awareness
  - Values alignment

  Usage: /align                                   # Company-wide check
         /align --scope=department:engineering    # Department check
         /align --scope=employee:alice-eng        # Individual check
         /align --auto-workshop                   # Schedule workshops for gaps

================================================================================
  SECURITY WORKFLOW
================================================================================

  Before releasing:
    /security-audit

  If critical issues found:
    /security-audit --fix

  After fixing:
    /gate

================================================================================
```

---

## Step 3: Command-Specific Help

If the argument matches a command name, display detailed help for that command.

**Parse the argument and map to command file:**

| Argument | Command File |
|----------|--------------|
| new-project | .claude/commands/new-project.md |
| discuss | .claude/commands/discuss.md |
| plan | .claude/commands/plan.md |
| build | .claude/commands/build.md |
| review | .claude/commands/review.md |
| gate | .claude/commands/gate.md |
| verify | .claude/commands/verify.md |
| complete | .claude/commands/complete.md |
| continue | .claude/commands/continue.md |
| feature | .claude/commands/feature.md |
| docs | .claude/commands/docs.md |
| prime | .claude/commands/prime.md |
| map-codebase | .claude/commands/map-codebase.md |
| company-bootstrap | .claude/commands/company-bootstrap.md |
| company-init | .claude/commands/company-init.md |
| company-status | .claude/commands/company-status.md |
| company-hire | .claude/commands/company-hire.md |
| company-assign | .claude/commands/company-assign.md |
| company-dismiss | .claude/commands/company-dismiss.md |
| company-request | .claude/commands/company-request.md |
| company-retro | .claude/commands/company-retro.md |
| company-reorg | .claude/commands/company-reorg.md |
| company-knowledge | .claude/commands/company-knowledge.md |
| knowledge-search | .claude/commands/knowledge-search.md |
| company-upgrade | .claude/commands/company-upgrade.md |
| submit | .claude/commands/submit.md |
| respond | .claude/commands/respond.md |
| pending | .claude/commands/pending.md |
| run-loop | .claude/commands/run-loop.md |
| company-create | .claude/commands/company-create.md |
| company-add-project | .claude/commands/company-add-project.md |
| company-projects | .claude/commands/company-projects.md |
| dashboard | .claude/commands/dashboard.md |
| company-health | .claude/commands/company-health.md |
| employee-status | .claude/commands/employee-status.md |
| lifecycle | .claude/commands/lifecycle.md |
| agent | .claude/commands/agent.md |
| add-agent | .claude/commands/add-agent.md |
| security-audit | .claude/commands/security-audit.md |
| workshop | .claude/commands/workshop.md |
| align | .claude/commands/align.md |

**Read the command file and display the first ~60 lines** (title, description, input, usage examples):

```
================================================================================
  /[command-name]
================================================================================

[First portion of command file showing:
 - Title and description
 - Input format
 - Usage examples
 - Key options/flags]

================================================================================
  FULL DOCUMENTATION
================================================================================

  The complete command documentation is available at:
  .claude/commands/[command-name].md

================================================================================
```

---

## Step 4: Unknown Command or Category

If the argument doesn't match a known category or command:

```
================================================================================
  UNKNOWN: [argument]
================================================================================

  "[argument]" is not a recognized command or category.

  Did you mean one of these?

  Categories:
    workflow, quick, company, multi-project, visibility, agents, advanced

  Popular commands:
    /help feature
    /help build
    /help company-bootstrap
    /help agent

  Show all commands:
    /help

================================================================================
```

---

## Rules

- **Use box-drawing characters** for visual structure (lines, corners)
- **Keep output scannable** with clear section headers
- **Show typical workflows** in each category to guide users
- **Include examples** wherever possible
- **Link to full documentation** for command-specific help
- **Handle unknown arguments gracefully** with suggestions
