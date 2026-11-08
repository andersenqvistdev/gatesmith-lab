# /strategy — Strategic Planning Command

Run strategic planning operations: assess goals, identify gaps, generate initiatives.

## Input

$ARGUMENTS

Supported subcommands:
- `assess` — Assess all goal progress
- `gaps` — Show strategic gaps between current state and targets
- `propose` — Generate initiative proposals to close gaps
- `plan` — Run full planning cycle (assess → gaps → propose → queue)
- `active` — Show active initiatives
- `approve [id]` — Approve a proposed initiative
- `weekly` — Run weekly planning cycle manually
- `daily` — Run daily planning cycle manually
- `hierarchy` — Display goal hierarchy tree
- `velocity` — Show goal velocity report (completion rates over time)

## Step 1: Parse Command

Parse `$ARGUMENTS` to extract subcommand:

```bash
subcommand=$(echo "$ARGUMENTS" | awk '{print $1}')
initiative_id=$(echo "$ARGUMENTS" | awk '{print $2}')
```

If no subcommand, default to `assess`.

## Step 2: Execute Subcommand

### assess — Goal Progress Assessment

```bash
uv run .claude/hooks/company/goal_tracker.py assess
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 STRATEGIC ASSESSMENT                             [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 GOAL PROGRESS

 G1: Quality      [████████░░░░░░░░░░░░] 48%  ! at_risk
     Coverage at 48%, needs attention
     Next: Run /test-sprint to improve coverage

 G2: Adoption     [██░░░░░░░░░░░░░░░░░░] 33%  ! at_risk
     1/3 tutorials, in progress
     Next: Create tutorial docs

 G3: Stability    [████████████████████] 100% ✓ complete
     Tests pass, no critical issues found

 G4: Enterprise   [████████████████████] 100% ✓ complete
     Enterprise features complete: audit-export, sbom

 G5: Autonomy     [██████████████░░░░░░] 70%  → on_track
     Daemon running, infrastructure ready

 G6: Economics    [████████████████████] 100% ✓ complete
     Economics fully implemented

═══════════════════════════════════════════════════════════════
 SUMMARY: 3 complete | 1 on_track | 2 at_risk | 0 blocked
═══════════════════════════════════════════════════════════════
```

### gaps — Strategic Gaps

```bash
uv run .claude/hooks/company/strategic_planner.py gaps
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 STRATEGIC GAPS                                   [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 [!!!!!] G1: Quality
         Progress: 48% → Target: 100% (Gap: 52%)
         Impact: Important - needs attention to meet targets
         Actions: Run /test-sprint to improve coverage

 [!!!  ] G2: Adoption
         Progress: 33% → Target: 100% (Gap: 67%)
         Impact: Important - needs attention to meet targets
         Actions: Create tutorial docs

═══════════════════════════════════════════════════════════════
 URGENCY LEGEND: !!!!! = critical | !!! = important | ! = monitor
═══════════════════════════════════════════════════════════════
```

### propose — Generate Initiatives

```bash
uv run .claude/hooks/company/strategic_planner.py propose
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 INITIATIVE PROPOSALS                             [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 [P15-G1] Close Quality Gap (52%)
   Size: large | Approval: human
   Owner: forge-cto | Priority: 80
   Goals: G1
   Tasks (3):
     1. Run coverage analysis
     2. Add tests for critical paths
     3. Verify coverage target met

 [P15-G2] Close Adoption Gap (67%)
   Size: medium | Approval: human
   Owner: marketing-lead | Priority: 60
   Goals: G2
   Tasks (2):
     1. Create tutorial outline
     2. Write tutorial content

═══════════════════════════════════════════════════════════════
 To approve: /strategy approve P15-G1
═══════════════════════════════════════════════════════════════
```

### plan — Full Planning Cycle

```bash
uv run .claude/hooks/company/strategic_planner.py plan
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 STRATEGIC PLANNING CYCLE                         [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 PHASE 1: Assessment
   Goals Assessed: 6

 PHASE 2: Gap Analysis
   Gaps Identified: 2

 PHASE 3: Initiative Generation
   Initiatives Proposed: 2
   Auto-Approved: 0

 PHASE 4: Task Queuing
   Tasks Queued: 0 (pending approval)

═══════════════════════════════════════════════════════════════
 RESULT
   Active Initiatives: 2
   Next Planning Run: YYYY-MM-DD (7 days)

 NEXT STEPS
   • Review proposals: /strategy active
   • Approve initiative: /strategy approve [id]
   • View work queue: cat .company/state/work_queue.json
═══════════════════════════════════════════════════════════════
```

### active — Show Active Initiatives

```bash
uv run .claude/hooks/company/strategic_planner.py active
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 ACTIVE INITIATIVES                               [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 ○ [P15-G1] Close Quality Gap (52%)
   Status: proposed | Size: large
   Tasks: 3 | Owner: forge-cto

 ○ [P15-G2] Close Adoption Gap (67%)
   Status: proposed | Size: medium
   Tasks: 2 | Owner: marketing-lead

═══════════════════════════════════════════════════════════════
 STATUS: ○ proposed | → approved | ⏳ in_progress | ✓ complete
═══════════════════════════════════════════════════════════════
```

### approve — Approve Initiative

```bash
uv run .claude/hooks/company/strategic_planner.py approve --initiative $initiative_id
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 INITIATIVE APPROVED                              [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 Initiative: P15-G1
 Title: Close Quality Gap (52%)
 Owner: forge-cto

 Tasks Queued: 3
   • WQ-P15-G1-1: Run coverage analysis
   • WQ-P15-G1-2: Add tests for critical paths
   • WQ-P15-G1-3: Verify coverage target met

 The daemon will execute these tasks automatically.

═══════════════════════════════════════════════════════════════
```

### weekly — Run Weekly Planning Cycle

```bash
uv run .claude/hooks/company/strategic_planner.py weekly
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 WEEKLY PLANNING CYCLE                            [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 PHASE 1: Goal Assessment
   Goals Assessed: 6
   Complete: 4 | On Track: 1 | At Risk: 1

 PHASE 2: Gap Analysis
   Gaps Identified: 2
   Critical: 1 | Important: 1

 PHASE 3: Initiative Generation
   Initiatives Created: 2
   Auto-Approved: 1

 PHASE 4: Task Queuing
   Tasks Queued: 3

═══════════════════════════════════════════════════════════════
 RESULT
   Active Initiatives: 3
   Next Weekly Run: YYYY-MM-DD (7 days)

 NEXT STEPS
   • Review proposals: /strategy active
   • Approve initiative: /strategy approve [id]
   • Run daily cycle: /strategy daily
═══════════════════════════════════════════════════════════════
```

### daily — Run Daily Planning Cycle

```bash
uv run .claude/hooks/company/strategic_planner.py daily
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 DAILY PLANNING CYCLE                             [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 PHASE 1: Progress Update
   Initiatives Updated: 2
   Status Changes:
     • P15-G1: approved → in_progress (1/3 tasks done)
     • P15-G3: in_progress → completed (2/2 tasks done)

 PHASE 2: Daily Task Queuing
   Daily Tasks Created: 4
   From Initiatives:
     • P15-G1: 2 tasks queued
     • P15-G2: 2 tasks queued

═══════════════════════════════════════════════════════════════
 SUMMARY
   Active Initiatives: 2
   Tasks Pending Today: 4

 The daemon will process these tasks automatically.
═══════════════════════════════════════════════════════════════
```

### hierarchy — Display Goal Hierarchy

Read the strategic state and display the goal hierarchy as a tree.

```bash
# Load state from .company/strategic_state.json
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 GOAL HIERARCHY                                   [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 ANNUAL GOALS
 ├── G1: Quality (100%)
 │   ├── G1-q1: Q1 Quality Target (100%)
 │   └── G1-q2: Q2 Quality Target (pending)
 │
 ├── G2: Adoption (100%)
 │   └── G2-q1: Tutorial Creation (100%)
 │
 ├── G3: Stability (30%) ! at_risk
 │   ├── G3-w1: Fix Failing Tests (queued)
 │   └── G3-w2: Address Critical TODOs (pending)
 │
 ├── G4: Enterprise (100%)
 │
 ├── G5: Autonomy (80%) → on_track
 │   ├── G5-w1: Start Daemon (completed)
 │   └── G5-w2: Test Autonomous Work (in_progress)
 │
 └── G6: Economics (100%)

═══════════════════════════════════════════════════════════════
 LEGEND: ✓ complete | → on_track | ! at_risk | ✗ blocked
═══════════════════════════════════════════════════════════════
```

If no hierarchy exists yet:

```
═══════════════════════════════════════════════════════════════
 GOAL HIERARCHY                                   [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 No goal hierarchy defined yet.

 Goal hierarchy is built automatically as:
   1. Weekly cycles decompose annual → quarterly → weekly goals
   2. Daily cycles create daily tasks from weekly goals
   3. Initiatives track progress toward goals

 Current Goals (from goal_tracker):
   G1: Quality      [████████████████████] 100% ✓
   G2: Adoption     [████████████████████] 100% ✓
   G3: Stability    [██████░░░░░░░░░░░░░░] 30%  !
   G4: Enterprise   [████████████████████] 100% ✓
   G5: Autonomy     [████████████████░░░░] 80%  →
   G6: Economics    [████████████████████] 100% ✓

═══════════════════════════════════════════════════════════════
```

### velocity — Goal Velocity Report

Read goal snapshots from strategic state and calculate velocity (rate of progress over time).

```bash
# Load state from .company/strategic_state.json
# Calculate velocity from goal_snapshots history
```

Display formatted output:

```
═══════════════════════════════════════════════════════════════
 GOAL VELOCITY REPORT                             [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 COMPLETION VELOCITY (last 7 days)

 G1: Quality
     Progress: 48% → 100% (+52%)
     Velocity: +7.4%/day
     Trend: ▲ accelerating

 G2: Adoption
     Progress: 33% → 100% (+67%)
     Velocity: +9.6%/day
     Trend: ▲ accelerating

 G3: Stability
     Progress: 100% → 30% (-70%)
     Velocity: -10%/day
     Trend: ▼ regressing (NEEDS ATTENTION)

 G4: Enterprise
     Progress: 100% → 100% (0%)
     Velocity: 0%/day
     Trend: — stable (complete)

 G5: Autonomy
     Progress: 60% → 80% (+20%)
     Velocity: +2.9%/day
     Trend: → steady

 G6: Economics
     Progress: 100% → 100% (0%)
     Velocity: 0%/day
     Trend: — stable (complete)

═══════════════════════════════════════════════════════════════
 SUMMARY
   Goals Improving: 3 | Stable: 2 | Regressing: 1

 PROJECTIONS (at current velocity)
   G3: Stability → 100% in ~7 days (if trend reverses)
   G5: Autonomy  → 100% in ~7 days

 ALERTS
   ⚠ G3 velocity negative — investigate blockers
═══════════════════════════════════════════════════════════════
```

If no snapshots exist:

```
═══════════════════════════════════════════════════════════════
 GOAL VELOCITY REPORT                             [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════

 No velocity data available yet.

 Velocity tracking requires multiple goal snapshots.
 Run /strategy plan or /strategy weekly to create snapshots.

 Current snapshot count: 0
 Required for velocity: 2+

═══════════════════════════════════════════════════════════════
```

## Step 3: Error Handling

### No Company

```
═══════════════════════════════════════════════════════════════
 STRATEGY                                            [ERROR]
═══════════════════════════════════════════════════════════════

No company structure found.

To initialize:
  /company-init

═══════════════════════════════════════════════════════════════
```

### Initiative Not Found

```
═══════════════════════════════════════════════════════════════
 STRATEGY                                            [ERROR]
═══════════════════════════════════════════════════════════════

Initiative 'XYZ' not found or not in proposed state.

Active initiatives:
  • P15-G1 (proposed)
  • P15-G2 (proposed)

═══════════════════════════════════════════════════════════════
```

## Rules

- **Always run goal_tracker for assess** — This provides real-time goal progress
- **Limit active initiatives to 3** — Prevents initiative overload
- **Auto-approve only small initiatives** — Medium+ require human review
- **Show next steps** — Guide user to approve/execute workflow
- **Format output consistently** — Use ASCII box drawing
