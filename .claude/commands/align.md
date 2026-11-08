# /align — Verify Organizational Alignment with Vision and Roadmap

Verify that the organization understands and is aligned with company vision, roadmap, and strategic direction. Identifies knowledge gaps, misalignments, and triggers educational interventions where needed.

**Purpose:** Ensure competence and understanding is spread across the organization. Every employee should understand what we're building, why, and their role in achieving it.

## Input
$ARGUMENTS

Supported arguments:
- `--scope=<scope>` — Who to check: `employee:<id>`, `team:<id>`, `department:<id>`, or `company` (default: company)
- `--focus=<area>` — Focus area: `vision`, `roadmap`, `values`, `all` (default: all)
- `--depth=<level>` — Check depth: `quick`, `standard`, `deep` (default: standard)
- `--auto-workshop` — Automatically schedule workshops for gaps found

Examples:
- `/align` — Company-wide alignment check
- `/align --scope=department:engineering --focus=roadmap`
- `/align --scope=employee:alice-eng --depth=deep`
- `/align --auto-workshop` — Check alignment and schedule workshops for gaps

## Step 0: Load Alignment Context

### 0.1: Check Company Exists

```bash
ls -la .company/org.json 2>/dev/null
```

**If not exists:**
```
## Alignment Check Not Available

No company structure found. Alignment checks require:
- `.company/org.json` — Organization structure
- `.company/vision.md` — Company vision (recommended)
- `.planning/ROADMAP.md` — Current roadmap

To initialize:
  /company-init

To create vision document:
  Create `.company/vision.md` with your company vision and values.
```

Exit without further processing.

### 0.2: Load Vision & Roadmap

Read alignment source documents:

| Document | Location | Purpose |
|----------|----------|---------|
| Vision | `.company/vision.md` | Company mission, vision, values |
| Roadmap | `.planning/ROADMAP.md` | Current priorities and phases |
| Org Structure | `.company/org.json` | Departments, teams, employees |
| Knowledge Base | `.company/knowledge/` | Decisions, patterns |
| Employee Learnings | `.company/employees/*/learnings.md` | Individual knowledge |

### 0.3: Check Vision Document Exists

If `.company/vision.md` doesn't exist:

```
## Vision Document Missing

No company vision document found at `.company/vision.md`.

A vision document helps ensure organizational alignment. It should contain:
- **Mission:** Why the company exists
- **Vision:** What success looks like
- **Values:** How we work
- **Strategic Goals:** What we're trying to achieve

Would you like to create one now? [Y/n]
```

If yes, guide creation of vision document.

## Step 1: Determine Scope & Participants

### 1.1: Parse Scope

| Scope | Description | Participants |
|-------|-------------|--------------|
| company | All employees | Everyone in org.json |
| department:<id> | Single department | All department members |
| team:<id> | Single team | All team members |
| employee:<id> | Individual | Single employee |

### 1.2: Load Participants

For each participant, gather:
- Role and responsibilities
- Department/team context
- Project assignments
- Previous learnings
- Workshop participation history

## Step 2: Define Alignment Criteria

### 2.1: Vision Alignment

Employees should understand:
- [ ] Company mission and purpose
- [ ] Long-term vision (where we're headed)
- [ ] Core values and how they apply to work
- [ ] Strategic goals for current period

### 2.2: Roadmap Alignment

Employees should understand:
- [ ] Current phase/milestone
- [ ] Their role in current work
- [ ] Dependencies on/from other teams
- [ ] Success criteria for current phase
- [ ] What comes next

### 2.3: Values Alignment

Employees should demonstrate:
- [ ] Understanding of company values
- [ ] Application of values in decisions
- [ ] Alignment with team/company culture

## Step 3: Conduct Alignment Assessment

### 3.1: Quick Assessment

For each participant, evaluate based on:
- Recent work alignment with roadmap
- Participation in relevant workshops
- Knowledge base contributions
- Learning records

### 3.2: Standard Assessment

Quick assessment PLUS:
- Simulate alignment questions
- Check understanding of adjacent team work
- Verify awareness of recent decisions (ADRs)

### 3.3: Deep Assessment

Standard assessment PLUS:
- Detailed understanding of strategic rationale
- Ability to explain vision to others
- Cross-functional awareness
- Historical context understanding

## Step 4: Generate Alignment Scores

### 4.1: Score Each Participant

```
### Individual Alignment Scores

| Employee | Role | Vision | Roadmap | Values | Overall |
|----------|------|--------|---------|--------|---------|
| alice-eng | Senior Engineer | 85% | 90% | 80% | 85% |
| bob-design | UX Lead | 90% | 75% | 95% | 87% |
| carol-pm | Product Manager | 95% | 95% | 90% | 93% |
| dave-dev | Developer | 60% | 70% | 75% | 68% |
```

### 4.2: Score Aggregations

```
### Team/Department Alignment

| Group | Vision | Roadmap | Values | Overall | Trend |
|-------|--------|---------|--------|---------|-------|
| Engineering | 78% | 85% | 82% | 82% | +5% |
| Design | 90% | 75% | 95% | 87% | +2% |
| Product | 95% | 95% | 90% | 93% | 0% |
| **Company** | **85%** | **85%** | **87%** | **86%** | **+3%** |
```

### 4.3: Identify Gaps

```
### Alignment Gaps

| Gap | Severity | Affected | Recommendation |
|-----|----------|----------|----------------|
| Roadmap awareness | High | dave-dev | 1:1 or workshop |
| Vision clarity | Medium | Engineering | Team workshop |
| Cross-team context | Medium | Design | Cross-functional sync |
```

## Step 5: Generate Recommendations

### 5.1: Individual Interventions

For employees with <70% alignment:

```
### Individual Development Needs

#### dave-dev (Overall: 68%)

**Gaps:**
- Limited understanding of Q1 strategic goals
- Unaware of recent API architecture decisions
- Hasn't participated in team workshops

**Recommendations:**
1. 1:1 with team lead to review roadmap (30 min)
2. Read ADR-0005, ADR-0007 (architecture decisions)
3. Attend next Engineering workshop

**Timeline:** Complete within 1 week
```

### 5.2: Team Interventions

For teams with <80% alignment:

```
### Team Development Needs

#### Engineering (Overall: 82%, Vision: 78%)

**Gaps:**
- Vision clarity below threshold
- Inconsistent understanding of strategic rationale

**Recommendations:**
1. Schedule vision alignment workshop (60 min)
2. CEO/CTO to present company direction
3. Follow-up: Each member writes vision summary

**Timeline:** Complete within 2 weeks
```

### 5.3: Company-Wide Interventions

If company alignment <85%:

```
### Company-Wide Development

**Gaps:**
- Cross-functional awareness low
- Strategic rationale not well understood

**Recommendations:**
1. All-hands alignment session (90 min)
2. Create "Company Direction" document for reference
3. Monthly vision sync (recurring)
4. Update onboarding to include vision module

**Timeline:** Schedule within 1 week
```

## Step 6: Output Alignment Report

```
## Organizational Alignment Report

═══════════════════════════════════════════════════════════════════════════════
 ALIGNMENT CHECK                                              [YYYY-MM-DD]
═══════════════════════════════════════════════════════════════════════════════
 Scope: <scope>
 Focus: <focus>
 Depth: <depth>
 Participants: <count>
═══════════════════════════════════════════════════════════════════════════════

### Executive Summary

| Metric | Score | Status | Trend |
|--------|-------|--------|-------|
| Overall Alignment | 86% | GOOD | +3% |
| Vision | 85% | GOOD | +2% |
| Roadmap | 85% | GOOD | +5% |
| Values | 87% | GOOD | +1% |

**Health Status:** [ALIGNED / NEEDS ATTENTION / MISALIGNED]

### Alignment by Group

[Table of department/team scores]

### Individual Scores

[Table of individual scores, sorted by alignment]

### Gaps Identified

| Priority | Gap | Scope | Impact | Intervention |
|----------|-----|-------|--------|--------------|
| High | Roadmap awareness | dave-dev | Individual | 1:1 coaching |
| Medium | Vision clarity | Engineering | Team | Workshop |
| Low | Cross-team context | Design | Team | Sync meeting |

### Recommended Interventions

#### Immediate (This Week)
1. [Highest priority intervention]

#### Short-Term (This Month)
1. [Medium priority interventions]

#### Ongoing
1. [Recurring alignment activities]

### Workshops to Schedule

| Workshop | Scope | Type | Priority | Suggested Date |
|----------|-------|------|----------|----------------|
| Vision Alignment | Engineering | alignment | High | YYYY-MM-DD |
| Roadmap Deep-Dive | company | learning | Medium | YYYY-MM-DD |

═══════════════════════════════════════════════════════════════════════════════
 NEXT ALIGNMENT CHECK: [recommended date, typically 2-4 weeks]
═══════════════════════════════════════════════════════════════════════════════
```

## Step 7: Auto-Schedule Workshops (if --auto-workshop)

If `--auto-workshop` flag is set:

```
### Auto-Scheduled Workshops

Based on gaps identified, the following workshops have been scheduled:

| ID | Workshop | Date | Scope | Facilitator |
|----|----------|------|-------|-------------|
| WS-0012 | Vision Alignment | YYYY-MM-DD | Engineering | CEO |
| WS-0013 | Roadmap Deep-Dive | YYYY-MM-DD | company | CTO |

To run a workshop:
  /workshop WS-0012

To modify schedule:
  Edit `.company/knowledge/workshops.md`
```

## Step 8: Update Records

### 8.1: Append to Alignment History

Create/append to `.company/knowledge/alignment.md`:

```markdown
---

## Alignment Check: YYYY-MM-DD

**Scope:** <scope>
**Focus:** <focus>
**Depth:** <depth>
**Participants:** <count>

### Scores
| Group | Vision | Roadmap | Values | Overall |
|-------|--------|---------|--------|---------|
| Company | 85% | 85% | 87% | 86% |
| Engineering | 78% | 85% | 82% | 82% |
| Design | 90% | 75% | 95% | 87% |
| Product | 95% | 95% | 90% | 93% |

### Gaps Identified
- dave-dev: Roadmap awareness (68% overall)
- Engineering: Vision clarity (78%)

### Interventions Scheduled
- WS-0012: Vision Alignment (Engineering)
- WS-0013: Roadmap Deep-Dive (company)

### Previous Check
- Date: YYYY-MM-DD
- Overall: 83%
- Change: +3%
```

### 8.2: Update Employee Records

For employees with gaps, add to their development notes.

## Step 9: Integration with Learning System

### 9.1: Connect to Project Learnings

Check `.planning/LEARNINGS.md` for relevant project learnings that should be shared.

### 9.2: Connect to Employee Mistakes

Review employee learning files for recurring issues that indicate alignment gaps.

### 9.3: Feed Knowledge Base

If alignment check reveals knowledge that should be documented:
- Suggest new ADRs for undocumented decisions
- Suggest new patterns for undocumented practices
- Identify knowledge base gaps

## Rules

- **Vision document is critical.** Guide creation if missing.
- **Score objectively.** Base scores on evidence (workshop participation, work alignment, learning records).
- **Prioritize interventions.** Not all gaps are equal - focus on high-impact fixes.
- **Track trends.** Alignment should improve over time.
- **Don't overwhelm.** Limit concurrent interventions to avoid fatigue.
- **Connect to existing systems.** Leverage workshops, knowledge base, and learning records.
- **Automate where safe.** --auto-workshop reduces friction for scheduling.
- **Regular cadence.** Recommend alignment checks every 2-4 weeks.
- **Celebrate progress.** Highlight alignment improvements, not just gaps.
- **Escalate persistent gaps.** Chronic misalignment may indicate deeper issues.
